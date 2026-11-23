# hermes-showcase

Hermes 的展示页仓库，同时托管一个「每日自动更新」的开发者文档离线包。

## 每日文档包（自动更新）

- **一键下载**：<https://floudk.github.io/hermes-showcase/codex-docs.zip>
- **在线浏览**：<https://floudk.github.io/hermes-showcase/codex-docs/index.html>
- **构建状态**：<https://floudk.github.io/hermes-showcase/docs-status.json>
- **运行记录**：<https://github.com/floudk/hermes-showcase/actions/workflows/docs-daily.yml>

内容：

| 目录 | 文档 | 官方来源 |
| --- | --- | --- |
| `claude/` | Claude Code 全量文档 | <https://code.claude.com/docs> |
| `codex/` | OpenAI Codex 全量文档 | <https://developers.openai.com/codex> |
| `dsh/` | DeepSeek Harness 官方 `docs/` 全量镜像 | <https://github.com/deepseek-ai/deepseek-harness> |

每个目录都带 `index.md`（目录）、`full.md`（全文单文件）和 `pages/`（按官方页面拆分）；`dsh/` 额外含中英文文档。

## 自动更新机制

- 由 [`.github/workflows/docs-daily.yml`](.github/workflows/docs-daily.yml) 驱动：每天 **08:17（北京时间）** 抓取上游文档，重新打包并用 GitHub Pages 部署。
- 生成脚本：[`scripts/build_docs.py`](scripts/build_docs.py)（纯 Python 标准库，无第三方依赖）。
- 任何一次抓取失败都不会让站点挂掉：会沿用上一次成功版本（`actions/cache`），并在 `docs-status.json` 里标记 `stale`；页面超过 36 小时未更新会显示提示。
- 定时任务每天会提交一份很小的 `docs-status.json`，避免 GitHub 因「仓库 60 天无活动」停用定时工作流。
- 站点部署方式已切换为 **GitHub Actions Artifact**（Settings → Pages → Source: GitHub Actions），每日构建产物不再提交到 main 分支，仓库体积不会膨胀。

## 本地手动跑一次

```bash
# 需要能访问 code.claude.com / developers.openai.com / github.com
python3 scripts/build_docs.py --repo . --out dist --cache build/cache
# 产物：dist/codex-docs.zip、dist/site/（可直接用静态服务器预览）
```

> 文档版权归各自官方所有，本镜像仅供个人在无法访问官网的环境下学习参考。
