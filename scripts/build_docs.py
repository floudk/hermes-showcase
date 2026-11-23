#!/usr/bin/env python3
"""Build the daily documentation mirror pack (Claude Code / Codex / DeepSeek Harness).

Runs on a GitHub Actions runner (or locally, with HTTPS_PROXY set).  Downloads the
official docs, packs them into a browsable markdown tree, zips it and assembles the
GitHub Pages artifact.

Outputs (relative to --out):

    docs/                markdown tree; also the root of codex-docs.zip
    codex-docs.zip       the one-click download bundle
    site/                full GitHub Pages artifact (repo content + docs)
    docs-status.json     machine readable build status

The previous build (restored from --cache by actions/cache) is reused for any
source that fails to download, so a flaky upstream never takes the whole site down.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CST = timezone(timedelta(hours=8))
UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/126.0 Safari/537.36 docs-mirror/1.0 (+https://github.com/floudk/hermes-showcase)'
)
SITE_URL = 'https://floudk.github.io/hermes-showcase/'
ZIP_NAME = 'codex-docs.zip'
SCHEDULE_LOCAL = '每天 08:17（北京时间）'
PAGE_FAIL_LIMIT = 0.10  # tolerate up to 10% of per-page downloads failing

CLAUDE_LABEL = 'Claude Code'
CLAUDE_INDEX = 'https://code.claude.com/docs/llms.txt'
CLAUDE_FULL = 'https://code.claude.com/docs/llms-full.txt'
CLAUDE_PREFIX = 'https://code.claude.com/docs/'
CLAUDE_SITE = 'https://code.claude.com/docs'

CODEX_LABEL = 'OpenAI Codex'
CODEX_PREFIX = 'https://developers.openai.com/codex/'
CODEX_INDEX = CODEX_PREFIX + 'llms.txt'
CODEX_FULL = CODEX_PREFIX + 'llms-full.txt'
CODEX_MANUAL = CODEX_PREFIX + 'codex-manual.md'
CODEX_SITE = 'https://developers.openai.com/codex'

DSH_LABEL = 'DeepSeek Harness (DSH)'
DSH_REPO = 'https://github.com/deepseek-ai/deepseek-harness.git'
DSH_SITE = 'https://github.com/deepseek-ai/deepseek-harness'


# --------------------------------------------------------------------------- utils
def now_local() -> datetime:
    return datetime.now(CST)


def iso(dt: datetime) -> str:
    return dt.astimezone(CST).isoformat(timespec='seconds')


def local_str(dt: datetime) -> str:
    return dt.astimezone(CST).strftime('%Y-%m-%d %H:%M:%S')


def human_size(num: int | float) -> str:
    num = float(num)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return f'{num:.1f} {unit}' if unit != 'B' else f'{int(num)} B'
        num /= 1024
    return f'{num:.1f} GB'


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, *, accept: str = 'text/markdown, text/plain;q=0.9, */*;q=0.1',
             timeout: int = 120, retries: int = 3) -> bytes:
    """Download one URL. Raises on 4xx (no point retrying) and after `retries`."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Accept': accept})
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            if exc.code in (400, 401, 403, 404, 410):
                raise RuntimeError(f'HTTP {exc.code} for {url}') from exc
            last_err = exc
        except Exception as exc:  # URLError, TimeoutError, OSError, ...
            last_err = exc
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'GET {url} failed after {retries} tries: {last_err}')


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith('<!doctype') or head.startswith('<html')


def fetch_pages(jobs: list[tuple[str, str]], workers: int, timeout: int,
                retries: int = 4) -> tuple[dict[str, str], dict[str, str]]:
    """Fetch (url, rel_path) pairs concurrently -> (ok {rel: text}, failed {rel: err})."""
    ok: dict[str, str] = {}
    failed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(http_get, url, timeout=timeout, retries=retries): (url, rel) for url, rel in jobs}
        for fut in as_completed(futures):
            url, rel = futures[fut]
            try:
                text = fut.result().decode('utf-8', 'replace')
            except Exception as exc:  # noqa: BLE001 - report and keep going
                failed[rel] = f'{url}: {exc}'
                continue
            if looks_like_html(text):
                failed[rel] = f'{url}: got HTML instead of markdown'
                continue
            ok[rel] = text
    return ok, failed


def split_anchor(url: str) -> tuple[str, str]:
    if '#' in url:
        base, frag = url.split('#', 1)
        return base, '#' + frag
    return url, ''


def rewrite_links(text: str, mapping: dict[str, str]) -> str:
    """Rewrite absolute markdown links to local relative paths when mapped."""
    def repl(match: re.Match[str]) -> str:
        target = match.group(1)
        base, frag = split_anchor(target)
        local = mapping.get(base)
        if not local:
            return match.group(0)
        return f'({local}{frag})'

    return re.sub(r'\((https?://[^)\s]+)\)', repl, text)


def markdown_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('#'):
            return line.lstrip('#').strip()
    return ''


def page_index_markdown(pages: dict[str, str], title: str) -> str:
    lines = [f'# {title}', '', f'共 {len(pages)} 篇。链接均为本地文件，可离线打开。', '']
    for rel in sorted(pages):
        label = rel[:-3] if rel.endswith('.md') else rel
        page_title = markdown_title(pages[rel]) or label
        lines.append(f'- [{page_title}]({rel})')
    return '\n'.join(lines) + '\n'


def tree_index_markdown(root: Path, title: str, link_prefix: str = '') -> str:
    """Build a simple nested index for an arbitrary markdown tree."""
    lines = [f'# {title}', '', '链接均为本地文件，可离线打开。', '']
    counts: dict[str, int] = {}
    grouped: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(root.rglob('*.md')):
        rel = path.relative_to(root)
        group = str(rel.parent) if str(rel.parent) != '.' else ''
        counts[group] = counts.get(group, 0) + 1
        title_line = ''
        try:
            title_line = markdown_title(path.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            pass
        grouped.setdefault(group, []).append((rel.as_posix(), title_line or rel.stem))
    for group in sorted(grouped):
        heading = f'## {group}' if group else '## 根目录'
        lines.append(heading)
        for rel, label in grouped[group]:
            lines.append(f'- [{label}]({link_prefix}{rel})')
        lines.append('')
    return '\n'.join(lines)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def dir_stats(path: Path) -> tuple[int, int]:
    files = 0
    size = 0
    if path.is_dir():
        for item in path.rglob('*'):
            if item.is_file():
                files += 1
                size += file_size(item)
    return files, size


def copy_tree(src: Path, dst: Path, *, skip_suffixes: tuple[str, ...] = ()) -> None:
    for item in src.rglob('*'):
        if not item.is_file():
            continue
        if item.name.endswith(skip_suffixes):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


# ----------------------------------------------------------------- source handlers
def build_claude(dest: Path, workers: int, timeout: int) -> dict:
    log('[claude] downloading index + full text ...')
    index_raw = http_get(CLAUDE_INDEX, timeout=timeout).decode('utf-8', 'replace')
    full_raw = http_get(CLAUDE_FULL, timeout=timeout).decode('utf-8', 'replace')
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'full.md').write_text(full_raw, encoding='utf-8')

    urls = sorted({u for u in re.findall(r'\((https://code\.claude\.com/docs/[^)\s]+\.md)\)', index_raw)})
    jobs: list[tuple[str, str]] = []
    for url in urls:
        rel = url[len(CLAUDE_PREFIX):]
        if rel.startswith('llms'):
            continue
        jobs.append((url, rel))
    log(f'[claude] downloading {len(jobs)} pages ...')
    ok, failed = fetch_pages(jobs, workers, timeout)
    for rel, text in sorted(ok.items()):
        target = dest / 'pages' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    if not jobs:
        raise RuntimeError('claude: llms.txt contained no page links')
    if len(failed) / len(jobs) > PAGE_FAIL_LIMIT:
        raise RuntimeError(f'claude: {len(failed)}/{len(jobs)} pages failed, e.g. {list(failed.items())[:2]}')

    mapping = {url: 'pages/' + rel for url, rel in jobs if rel in ok}
    mapping[CLAUDE_FULL] = 'full.md'
    mapping[CLAUDE_INDEX] = 'index.md'
    (dest / 'index.md').write_text(rewrite_links(index_raw, mapping), encoding='utf-8')
    (dest / 'pages' / 'index.md').write_text(page_index_markdown(ok, 'Claude Code 文档分页索引'), encoding='utf-8')

    files, size = dir_stats(dest)
    return {
        'label': CLAUDE_LABEL,
        'site': CLAUDE_SITE,
        'pages': len(ok),
        'page_failures': len(failed),
        'page_failure_examples': dict(list(failed.items())[:3]),
        'files': files,
        'bytes': size,
        'urls': {'index': CLAUDE_INDEX, 'full': CLAUDE_FULL},
    }


def build_codex(dest: Path, workers: int, timeout: int) -> dict:
    log('[codex] downloading index + full text + manual ...')
    index_raw = http_get(CODEX_INDEX, timeout=timeout).decode('utf-8', 'replace')
    full_raw = http_get(CODEX_FULL, timeout=timeout).decode('utf-8', 'replace')
    manual_raw = http_get(CODEX_MANUAL, timeout=timeout).decode('utf-8', 'replace')
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'full.md').write_text(full_raw, encoding='utf-8')
    (dest / 'manual.md').write_text(manual_raw, encoding='utf-8')

    raw_links = sorted(set(re.findall(r'\((https?://[^)\s]+\.md)\)', index_raw)))
    page_links: list[tuple[str, str]] = []  # (canonical url, rel)
    fixed_mapping: dict[str, str] = {
        CODEX_FULL: 'full.md',
        CODEX_INDEX: 'index.md',
        'https://learn.chatgpt.com/docs/llms-full.txt': 'full.md',
        'https://learn.chatgpt.com/docs/codex-manual.md': 'manual.md',
    }
    for url in raw_links:
        base, _ = split_anchor(url)
        if base.startswith('https://learn.chatgpt.com/docs/'):
            rel = base[len('https://learn.chatgpt.com/docs/'):]
        elif base.startswith(CODEX_PREFIX):
            rel = base[len(CODEX_PREFIX):]
        else:
            continue
        if base.endswith('codex-manual.md'):
            fixed_mapping[base] = 'manual.md'
            continue
        if rel.startswith('llms') or not rel.endswith('.md'):
            continue
        page_links.append((base, rel))

    jobs = [(CODEX_PREFIX + rel, rel) for _url, rel in page_links]
    log(f'[codex] downloading {len(jobs)} pages ...')
    ok, failed = fetch_pages(jobs, workers, timeout)
    if failed:
        log(f'[codex] retrying {len(failed)} failed pages against learn.chatgpt.com ...')
        retry_jobs = [(f'https://learn.chatgpt.com/docs/{rel}', rel) for rel in sorted(failed)]
        ok2, failed = fetch_pages(retry_jobs, workers, timeout)
        ok.update(ok2)
    mapping = dict(fixed_mapping)
    for url, rel in page_links:
        if rel in ok:
            mapping[url] = 'pages/' + rel
    for rel, text in sorted(ok.items()):
        target = dest / 'pages' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    if not jobs:
        raise RuntimeError('codex: llms.txt contained no page links')
    if len(failed) / len(jobs) > PAGE_FAIL_LIMIT:
        raise RuntimeError(f'codex: {len(failed)}/{len(jobs)} pages failed, e.g. {list(failed.items())[:2]}')

    (dest / 'index.md').write_text(rewrite_links(index_raw, mapping), encoding='utf-8')
    (dest / 'pages' / 'index.md').write_text(page_index_markdown(ok, 'Codex 文档分页索引'), encoding='utf-8')

    files, size = dir_stats(dest)
    return {
        'label': CODEX_LABEL,
        'site': CODEX_SITE,
        'pages': len(ok),
        'page_failures': len(failed),
        'page_failure_examples': dict(list(failed.items())[:3]),
        'files': files,
        'bytes': size,
        'urls': {'index': CODEX_INDEX, 'full': CODEX_FULL, 'manual': CODEX_MANUAL},
    }


def _proxy_args() -> list[str]:
    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') \
        or os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
    if not proxy:
        return []
    return ['-c', f'http.proxy={proxy}', '-c', f'https.proxy={proxy}']


def fetch_dsh_repo(workdir: Path) -> tuple[Path, str]:
    """Sparse-clone the DSH repo and return (checkout_dir, commit_sha)."""
    checkout = workdir / 'deepseek-harness'
    if checkout.exists():
        shutil.rmtree(checkout, ignore_errors=True)
    git = ['git'] + _proxy_args()
    log('[dsh] sparse clone of deepseek-harness ...')
    subprocess.run(
        git + ['clone', '--depth', '1', '--filter=blob:none', '--sparse', DSH_REPO, str(checkout)],
        check=True,
    )
    subprocess.run(['git', '-C', str(checkout), 'sparse-checkout', 'set', 'docs'], check=True)
    sha = subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip()
    return checkout, sha


def build_dsh(dest: Path, workdir: Path, workers: int, timeout: int) -> dict:  # noqa: ARG001 - workers kept for signature symmetry
    checkout, sha = fetch_dsh_repo(workdir)
    src_docs = checkout / 'docs'
    if not src_docs.is_dir():
        raise RuntimeError('dsh: docs/ directory missing in repo')
    dest.mkdir(parents=True, exist_ok=True)
    copy_tree(src_docs, dest / 'docs', skip_suffixes=('.i18n.yaml',))

    root_files = (
        'README.md', 'README.zh.md', 'AGENTS.md', 'BENCHMARK.md', 'BRAND_GUIDELINES.md',
        'BRAND_GUIDELINES.zh.md', 'CLAUDE.md', 'CONTRIBUTING.md', 'CONTRIBUTING.zh.md',
        'LICENSE', 'SAFETY.md', 'SAFETY.zh.md', 'THIRD_PARTY_NOTICES.md',
    )
    for name in root_files:
        try:
            content = subprocess.check_output(
                ['git', '-C', str(checkout), 'show', f'HEAD:{name}'], text=True
            )
            (dest / name).write_text(content, encoding='utf-8')
        except subprocess.CalledProcessError:
            log(f'[dsh] warning: {name} not found in repo')
            continue

    (dest / 'index.md').write_text(
        tree_index_markdown(dest / 'docs', 'DeepSeek Harness 官方文档索引', 'docs/'),
        encoding='utf-8',
    )
    files, size = dir_stats(dest)
    return {
        'label': DSH_LABEL,
        'site': DSH_SITE,
        'pages': sum(1 for _ in (dest / 'docs').rglob('*.md')),
        'page_failures': 0,
        'version': sha[:12],
        'files': files,
        'bytes': size,
        'urls': {'repo': DSH_REPO, 'docs': f'{DSH_SITE}/tree/{sha}/docs'},
    }


# ------------------------------------------------------------------- render pages
def render_docs_readme(sources: dict[str, dict], zip_raw_bytes: int, generated: datetime) -> str:
    lines = [
        '# 开发者文档离线包（每日自动更新）',
        '',
        f'- 打包时间：{local_str(generated)}（北京时间）',
        f'- 自动更新：GitHub Actions {SCHEDULE_LOCAL}，站点：{SITE_URL}',
        f'- 内容大小：{human_size(zip_raw_bytes)}（markdown 原始体积）',
        '',
        '包含三份官方文档的全量 Markdown 镜像：',
        '',
        '| 目录 | 文档 | 官方来源 | 抓取时间 | 文件数 |',
        '| --- | --- | --- | --- | --- |',
    ]
    for key, info in sources.items():
        if not info.get('present'):
            continue
        lines.append(
            f'| `{key}/` | {info["label"]} | {info["site"]} | {info.get("fetched_at_local") or "未知"} | {info["files"]} |'
        )
    lines += [
        '',
        '## 怎么读',
        '',
        '- 用支持 Markdown 的编辑器打开（VS Code / Typora / Obsidian 均可）：先看 `index.html` 或各目录下的 `index.md`。',
        '- 解压后直接双击 `index.html` 也能用浏览器离线浏览。',
        '- 每个产品目录下：`index.md` 是目录、`full.md` 是全文单文件（方便全文搜索）、`pages/` 是按页拆分的版本。',
        '- `dsh/` 是 DeepSeek Harness 官方仓库的 `docs/` 全量镜像（含中英文）。',
        '',
        '## 说明',
        '',
        '- 文档版权归各自官方所有，本镜像仅供个人学习、在内网无法访问官网时查阅使用。',
        '- 若某个来源抓取失败，会自动沿用上一次成功抓取的版本，并在 `docs-status.json` 里标记 `stale`。',
    ]
    return '\n'.join(lines) + '\n'


DOCS_INDEX_TEMPLATE = Template('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>开发者文档离线包 · $generated_local</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--card2:#1d212b;--txt:#e8eaf0;--sub:#9aa3b2;--accent:#58a6ff;--border:#2a2f3a;--ok:#3fb950;--warn:#d29922;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font-family:-apple-system,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;line-height:1.6;padding:24px 16px 60px}
  .wrap{max-width:860px;margin:0 auto}
  header{margin-bottom:20px}
  h1{font-size:1.5rem;margin-bottom:6px}
  .meta{color:var(--sub);font-size:.85rem}
  .note{color:var(--sub);font-size:.8rem;background:var(--card);border:1px dashed var(--border);border-radius:8px;padding:10px 14px;margin:14px 0}
  .warn{color:#f0c674;font-size:.85rem;background:rgba(210,153,34,.1);border:1px solid var(--warn);border-radius:8px;padding:10px 14px;margin:14px 0}
  h2{font-size:1.02rem;margin:26px 0 4px;color:var(--txt)}
  h2 .src{font-size:.72rem;color:var(--sub);font-weight:400;margin-left:8px}
  .srcmeta{color:var(--sub);font-size:.78rem;margin-bottom:10px}
  a.item{display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--card);border:1px solid var(--border);border-radius:10px;padding:11px 16px;text-decoration:none;color:var(--txt);margin-bottom:8px;transition:border-color .12s,transform .12s}
  a.item:hover{border-color:var(--accent);transform:translateX(3px)}
  a.item .t{font-weight:600;color:var(--accent)}
  a.item .d{color:var(--sub);font-size:.82rem}
  .tag{display:inline-block;font-size:.7rem;padding:1px 7px;border-radius:999px;margin-left:6px;vertical-align:2px}
  .tag.ok{background:rgba(63,185,80,.15);color:var(--ok);border:1px solid rgba(63,185,80,.4)}
  .tag.stale{background:rgba(210,153,34,.15);color:var(--warn);border:1px solid rgba(210,153,34,.4)}
  footer{margin-top:30px;color:var(--sub);font-size:.75rem;border-top:1px solid var(--border);padding-top:12px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📚 开发者文档离线包</h1>
    <div class="meta">Claude Code · OpenAI Codex · DeepSeek Harness 官方文档镜像 · 打包于 $generated_local</div>
    <div class="note">🤖 由 GitHub Actions $schedule_local 自动抓取官方文档并重新打包。整包下载入口在站点首页的 <b>codex-docs.zip</b>；本页随 zip 一起分发，解压后可直接打开。</div>
    <div id="stale" class="warn" hidden></div>
  </header>
  $sections
  <footer>来源：$source_summary · 仅供个人学习参考 · 自动更新：$schedule_local</footer>
</div>
<script>
(async () => {
  try {
    const r = await fetch('docs-status.json', {cache: 'no-store'});
    if (!r.ok) return;
    const s = await r.json();
    const age = (Date.now() - Date.parse(s.generated_at)) / 3600000;
    if (age > 36) {
      const el = document.getElementById('stale');
      el.hidden = false;
      el.textContent = '⚠️ 这份文档包已经 ' + Math.round(age) + ' 小时没有更新（最后打包 ' + s.generated_at_local + '），自动更新可能已经失效，请到站点首页确认。';
    }
  } catch (e) { /* offline: ignore */ }
})();
</script>
</body>
</html>
''')

SOURCE_SECTION_TEMPLATE = Template('''  <section>
    <h2>$icon $label <span class="src">$site</span>$tags</h2>
    <div class="srcmeta">抓取时间 $fetched_local · $files 个文件 · $size$extra</div>
    $items
  </section>
''')


def render_docs_index(docs_root: Path, sources: dict[str, dict], generated: datetime) -> str:
    sections = []
    source_summary = ' · '.join(
        f'{info["label"]}' for info in sources.values() if info.get('present')
    )
    for key, info in sources.items():
        if not info.get('present'):
            continue
        sections.append(_section_html_real(docs_root, key, info))
    return DOCS_INDEX_TEMPLATE.substitute(
        generated_local=local_str(generated),
        schedule_local=SCHEDULE_LOCAL,
        sections='\n'.join(sections),
        source_summary=html.escape(source_summary or '无'),
    )


def _section_html_real(docs_root: Path, key: str, info: dict) -> str:
    icon = {'claude': '🟠', 'codex': '🔵', 'dsh': '🟢'}.get(key, '📘')
    tags = ''
    if info.get('stale'):
        tags = '<span class="tag stale">沿用了上次版本</span>'
    elif info.get('present'):
        tags = '<span class="tag ok">本次已更新</span>'
    layout = {
        'claude': [
            ('index.md', '📑 文档目录', '官方 llms.txt 索引，链接已改成本地文件'),
            ('full.md', '📕 全文单文件', '适合全文搜索 / 直接丢给 AI'),
            ('pages/index.md', '📗 分页索引', '按官方页面拆分，逐一阅读'),
        ],
        'codex': [
            ('index.md', '📑 文档目录', '官方 llms.txt 索引，链接已改成本地文件'),
            ('full.md', '📕 全文单文件', '适合全文搜索 / 直接丢给 AI'),
            ('manual.md', '📘 Codex Manual', '官方机器可读手册'),
            ('pages/index.md', '📗 分页索引', '按官方页面拆分，逐一阅读'),
        ],
        'dsh': [
            ('README.md', '📖 项目 README', 'DeepSeek Harness 仓库首页'),
            ('README.zh.md', '📖 项目 README（中文）', 'DeepSeek Harness 仓库首页中文版'),
            ('index.md', '📑 文档索引', 'docs/ 全量目录，含中英文文档'),
        ],
    }.get(key, [])
    items: list[str] = []
    for rel, title, desc in layout:
        path = docs_root / key / rel
        if not path.is_file():
            continue
        size = human_size(file_size(path))
        items.append(
            '    <a class="item" href="' + key + '/' + rel + '">'
            '<div><div class="t">' + html.escape(title) + '</div>'
            '<div class="d">' + html.escape(desc) + '</div></div>'
            '<div class="d">' + size + '</div></a>'
        )
    extra = ''
    if info.get('page_failures'):
        extra += f' · {info["page_failures"]} 页下载失败已跳过'
    if info.get('version'):
        extra += f' · commit {info["version"]}'
    return SOURCE_SECTION_TEMPLATE.substitute(
        icon=icon,
        label=info['label'],
        site=html.escape(info.get('site', '')),
        tags=tags,
        fetched_local=info.get('fetched_at_local') or '（沿用缓存）',
        files=info.get('files', 0),
        size=human_size(info.get('bytes', 0)),
        extra=extra,
        items='\n'.join(items),
    )


MAIN_SECTION_TEMPLATE = Template('''  <!-- DOCS:BEGIN -->
  <!-- 本区间由 scripts/build_docs.py 在每次部署时自动生成，请勿手改 -->
  <section id="docs-pack">
    <h2>⚡ 每日文档包 · 自动更新</h2>
    <a class="item" href="codex-docs.zip">
      <div>
        <div class="t">⬇️ 一键下载全部文档 · codex-docs.zip（$zip_size）</div>
        <div class="d">Claude Code + Codex + DeepSeek Harness 官方文档全量镜像 · 打包于 $generated_local</div>
      </div>
      <div class="dt">$generated_day</div>
    </a>
    <a class="item" href="codex-docs/index.html">
      <div>
        <div class="t">📚 离线文档目录（按产品分册）</div>
        <div class="d">浏览器直接浏览 $source_summary · 单页 / 全文 / 分页三种入口</div>
      </div>
      <div class="dt">$generated_day</div>
    </a>
    <a class="item" href="codex-docs/claude/index.md">
      <div>
        <div class="t">📗 Claude Code 文档</div>
        <div class="d">code.claude.com 全量镜像 · 目录 / 全文 / 分页</div>
      </div>
      <div class="dt">$generated_day</div>
    </a>
    <a class="item" href="codex-docs/codex/index.md">
      <div>
        <div class="t">📘 OpenAI Codex 文档</div>
        <div class="d">developers.openai.com/codex 全量镜像 · 目录 / 全文 / 手册 / 分页</div>
      </div>
      <div class="dt">$generated_day</div>
    </a>
    <a class="item" href="codex-docs/dsh/index.md">
      <div>
        <div class="t">📙 DeepSeek Harness 文档</div>
        <div class="d">github.com/deepseek-ai/deepseek-harness docs/ 全量镜像（中英文）</div>
      </div>
      <div class="dt">$generated_day</div>
    </a>
    <div class="note">🤖 由 GitHub Actions $schedule_local 自动抓取并重新打包；任何一次抓取失败会沿用上次版本，页面顶部会在超过 36 小时未更新时提示。<a href="https://github.com/$repo_slug/actions/workflows/docs-daily.yml" style="color:var(--accent)">运行记录</a> · <a href="docs-status.json" style="color:var(--accent)">构建状态</a></div>
    <div id="docs-stale" style="display:none"></div>
  </section>
  <script>
  (async () => {
    try {
      const r = await fetch('docs-status.json', {cache: 'no-store'});
      if (!r.ok) return;
      const s = await r.json();
      const age = (Date.now() - Date.parse(s.generated_at)) / 3600000;
      if (age > 36) {
        const el = document.getElementById('docs-stale');
        el.style.display = 'block';
        el.className = 'note';
        el.style.borderColor = '#d29922';
        el.style.color = '#f0c674';
        el.textContent = '⚠️ 文档包已经 ' + Math.round(age) + ' 小时未更新（最后打包 ' + s.generated_at_local + '），自动更新可能失效。';
      }
    } catch (e) {}
  })();
  </script>
  <!-- DOCS:END -->
''')


def patch_site_index(site_index: Path, docs_root: Path, status: dict) -> bool:
    if not site_index.is_file():
        log('WARNING: site index.html not found, skip patch')
        return False
    text = site_index.read_text(encoding='utf-8')
    begin, end = '<!-- DOCS:BEGIN -->', '<!-- DOCS:END -->'
    if begin not in text or end not in text:
        log('WARNING: DOCS markers not found in index.html, skip patch')
        return False
    sources = status['sources']
    summary = ' · '.join(info['label'] for info in sources.values() if info.get('present'))
    generated = datetime.fromisoformat(status['generated_at'].replace('Z', '+00:00'))
    block = MAIN_SECTION_TEMPLATE.substitute(
        zip_size=human_size(status['zip']['bytes']),
        generated_local=local_str(generated),
        generated_day=generated.astimezone(CST).strftime('%m-%d'),
        source_summary=html.escape(summary),
        schedule_local=SCHEDULE_LOCAL,
        repo_slug=status['repo'],
    )
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    patched = head.rstrip() + '\n' + block + tail
    site_index.write_text(patched, encoding='utf-8')
    return True


# ----------------------------------------------------------------------- assemble
def make_zip(docs_root: Path, zip_path: Path) -> tuple[int, str, int]:
    files = sorted(p for p in docs_root.rglob('*') if p.is_file())
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            zf.write(path, path.relative_to(docs_root).as_posix())
    data = zip_path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest(), len(files)


def build_site(repo: Path, out: Path, docs_root: Path, zip_path: Path, status: dict) -> Path:
    site = out / 'site'
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)

    skip_names = {'.git', '.github', 'build', 'dist', 'codex-docs', 'codex-docs.zip', 'scripts'}
    for item in sorted(repo.iterdir()):
        if item.name in skip_names:
            continue
        if item.is_dir():
            shutil.copytree(item, site / item.name)
        else:
            shutil.copy2(item, site / item.name)

    shutil.copytree(docs_root, site / 'codex-docs')
    shutil.copy2(zip_path, site / ZIP_NAME)

    # the zip itself carries a sources-only status file; the site copy is richer.
    write_json(site / 'docs-status.json', status)
    write_json(site / 'codex-docs' / 'docs-status.json', status)

    patch_site_index(site / 'index.html', docs_root, status)
    return site


def load_cached_source(cache: Path, key: str) -> tuple[Path, dict] | tuple[None, None]:
    src = cache / 'docs' / key
    meta_file = cache / 'meta' / f'{key}.json'
    if not src.is_dir():
        return None, None
    meta: dict = {}
    if meta_file.is_file():
        try:
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            meta = {}
    return src, meta


def build_sources(docs_root: Path, cache: Path, workdir: Path, args: argparse.Namespace) -> dict[str, dict]:
    builders = {
        'claude': lambda dest: build_claude(dest, args.workers, args.timeout),
        'codex': lambda dest: build_codex(dest, args.workers, args.timeout),
        'dsh': lambda dest: build_dsh(dest, workdir, args.workers, args.timeout),
    }
    if args.sources:
        wanted = [s.strip() for s in args.sources.split(',') if s.strip()]
        builders = {k: v for k, v in builders.items() if k in wanted}

    results: dict[str, dict] = {}
    for key, builder in builders.items():
        dest = docs_root / key
        if dest.exists():
            shutil.rmtree(dest)
        fetched = now_local()
        try:
            info = builder(dest)
        except Exception as exc:  # noqa: BLE001 - fall back to cache
            log(f'[{key}] FAILED: {exc}')
            cached_src, cached_meta = load_cached_source(cache, key)
            if cached_src is not None:
                shutil.copytree(cached_src, dest)
                info = dict(cached_meta)
                info.update({
                    'label': {'claude': CLAUDE_LABEL, 'codex': CODEX_LABEL, 'dsh': DSH_LABEL}[key],
                    'stale': True,
                    'fallback_used': True,
                    'error': str(exc),
                })
                log(f'[{key}] using cached copy from {info.get("fetched_at_local", "unknown")}')
            else:
                info = {
                    'label': {'claude': CLAUDE_LABEL, 'codex': CODEX_LABEL, 'dsh': DSH_LABEL}[key],
                    'present': False,
                    'stale': False,
                    'error': str(exc),
                }
        else:
            info = dict(info)
            info['stale'] = False
            info['present'] = True
            info['fetched_at'] = iso(fetched)
            info['fetched_at_local'] = local_str(fetched)
            # persist metadata for future cache fallbacks
            write_json(cache / 'meta' / f'{key}.json', info)
        info.setdefault('present', True)
        info['key'] = key
        results[key] = info
    return results


def update_cache(docs_root: Path, cache: Path) -> None:
    target = cache / 'docs'
    if target.exists():
        shutil.rmtree(target)
    if docs_root.is_dir():
        shutil.copytree(docs_root, target)
    (cache / 'meta').mkdir(parents=True, exist_ok=True)


def write_summary(status: dict) -> None:
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if not path:
        return
    lines = [
        '## 文档包构建结果',
        '',
        f'- 打包时间：{status["generated_at_local"]}',
        f'- zip：`{status["zip"]["name"]}` · {human_size(status["zip"]["bytes"])} · {status["zip"]["entries"]} 个文件',
        '',
        '| 文档 | 状态 | 抓取时间 | 文件数 | 大小 |',
        '| --- | --- | --- | --- | --- |',
    ]
    for info in status['sources'].values():
        if not info.get('present'):
            state = '❌ 失败（无缓存）'
        elif info.get('stale'):
            state = '⚠️ 沿用缓存'
        else:
            state = '✅ 已更新'
        lines.append(
            f'| {info["label"]} | {state} | {info.get("fetched_at_local") or "-"} | '
            f'{info.get("files", 0)} | {human_size(info.get("bytes", 0))} |'
        )
    if status.get('errors'):
        lines += ['', '错误：', '```', *status['errors'], '```']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.', help='repository checkout (default: .)')
    parser.add_argument('--out', default='dist', help='output directory (default: dist)')
    parser.add_argument('--cache', default='build/cache', help='previous build cache directory')
    parser.add_argument('--workers', type=int, default=8, help='parallel downloads (default: 8)')
    parser.add_argument('--timeout', type=int, default=120, help='per-request timeout seconds')
    parser.add_argument('--sources', default='', help='comma separated subset: claude,codex,dsh')
    parser.add_argument('--reuse-cache', action='store_true',
                        help='skip network for sources present in cache, only repack')
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    out = Path(args.out).resolve()
    cache = Path(args.cache).resolve()
    workdir = out / '.work'

    started = now_local()
    out.mkdir(parents=True, exist_ok=True)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    docs_root = out / 'docs'
    if docs_root.exists():
        shutil.rmtree(docs_root)
    docs_root.mkdir(parents=True)

    if args.reuse_cache:
        cached = cache / 'docs'
        if not cached.is_dir():
            log(f'ERROR: --reuse-cache given but {cached} missing')
            return 1
        shutil.copytree(cached, docs_root, dirs_exist_ok=True)
        sources: dict[str, dict] = {}
        for key, label, site in (
            ('claude', CLAUDE_LABEL, CLAUDE_SITE),
            ('codex', CODEX_LABEL, CODEX_SITE),
            ('dsh', DSH_LABEL, DSH_SITE),
        ):
            dest = docs_root / key
            if not dest.is_dir():
                continue
            meta_file = cache / 'meta' / f'{key}.json'
            meta = {}
            if meta_file.is_file():
                meta = json.loads(meta_file.read_text(encoding='utf-8'))
            files, size = dir_stats(dest)
            meta.update({'label': label, 'site': site, 'files': files, 'bytes': size,
                         'stale': False, 'present': True, 'key': key})
            meta.setdefault('fetched_at_local', local_str(started))
            sources[key] = meta
    else:
        sources = build_sources(docs_root, cache, workdir, args)
        update_cache(docs_root, cache)

    present = [s for s in sources.values() if s.get('present')]
    if not present:
        log('ERROR: every documentation source failed and no cache is available')
        write_summary({'generated_at': iso(now_local()), 'generated_at_local': local_str(now_local()),
                       'sources': sources, 'zip': {'name': ZIP_NAME, 'bytes': 0, 'entries': 0},
                       'errors': [str(s.get('error')) for s in sources.values()]})
        return 1

    generated = now_local()
    zip_raw_bytes = sum(int(info.get('bytes', 0)) for info in present)

    (docs_root / 'README.md').write_text(render_docs_readme(sources, zip_raw_bytes, generated), encoding='utf-8')
    (docs_root / 'index.html').write_text(render_docs_index(docs_root, sources, generated), encoding='utf-8')
    write_json(docs_root / 'docs-status.json', {
        'generated_at': generated.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'generated_at_local': local_str(generated),
        'schedule_local': SCHEDULE_LOCAL,
        'sources': {k: {kk: vv for kk, vv in v.items() if kk not in ('key',)} for k, v in sources.items()},
    })

    log('[pack] building zip ...')
    zip_path = out / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()
    zip_bytes, zip_sha, zip_entries = make_zip(docs_root, zip_path)

    status = {
        'generated_at': generated.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        'generated_at_local': local_str(generated),
        'schedule_local': SCHEDULE_LOCAL,
        'site_url': SITE_URL,
        'repo': os.environ.get('GITHUB_REPOSITORY', 'floudk/hermes-showcase'),
        'run': (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY', 'floudk/hermes-showcase')}/actions/runs/"
                f"{os.environ.get('GITHUB_RUN_ID', '')}").rstrip('/'),
        'sources': sources,
        'zip': {
            'name': ZIP_NAME,
            'bytes': zip_bytes,
            'sha256': zip_sha,
            'entries': zip_entries,
            'raw_bytes': zip_raw_bytes,
        },
        'errors': [f'{k}: {v.get("error")}' for k, v in sources.items() if v.get('error') and not v.get('present')],
    }
    write_json(out / 'docs-status.json', status)

    log('[site] assembling Pages artifact ...')
    site = build_site(repo, out, docs_root, zip_path, status)
    site_files, site_size = dir_stats(site)
    log(f'[site] {site_files} files, {human_size(site_size)}')

    write_summary(status)

    log('')
    log(f'OK: zip {human_size(zip_bytes)} · {zip_entries} entries · sha256 {zip_sha[:16]}…')
    for key, info in sources.items():
        state = 'cached' if info.get('stale') else ('ok' if info.get('present') else 'FAILED')
        log(f'  - {key:7s} {state:7s} {info.get("files", 0):5d} files  {human_size(info.get("bytes", 0)):>9s}  {info.get("fetched_at_local", "")}')
    log(f'  took {int((now_local() - started).total_seconds())}s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
