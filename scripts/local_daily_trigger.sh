#!/usr/bin/env bash
# 每日文档包的兜底触发器。
#
# GitHub Actions 的 schedule 触发器偶尔不按时触发（甚至整天不触发），
# 本脚本由本机 cron 在每天早上调用：如果「今天（北京时间）还没有成功构建」，
# 就用 workflow_dispatch 手动触发一次 Daily docs mirror 工作流。
#
# 安装（本机 crontab，每天 9:17–13:17 每小时检查一次，成功即跳过）：
#   17 9-13 * * * /home/yfqing/Projects/workspace/hermes-showcase/scripts/local_daily_trigger.sh >> $HOME/.local/state/hermes-docs-trigger.log 2>&1
set -euo pipefail

REPO="floudk/hermes-showcase"
WORKFLOW="docs-daily.yml"
REF="main"
LOG_TAG="[docs-trigger $(TZ=Asia/Shanghai date '+%F %T')]"

# 北京时间今天 0 点，换算成 GitHub API 使用的 UTC 时间
start_utc="$(TZ=Asia/Shanghai date -d 'today 00:00' -u '+%Y-%m-%dT%H:%M:%SZ')"

runs_json="$(gh api "repos/${REPO}/actions/runs?per_page=50")" || {
  echo "${LOG_TAG} 查询运行记录失败，退出（下次再试）"
  exit 1
}

success_count="$(jq "[.workflow_runs[] | select(.created_at >= \"${start_utc}\" and .conclusion == \"success\")] | length" <<<"${runs_json}")"
pending_count="$(jq "[.workflow_runs[] | select(.created_at >= \"${start_utc}\" and .status != \"completed\")] | length" <<<"${runs_json}")"

if [ "${success_count:-0}" -gt 0 ]; then
  echo "${LOG_TAG} 今天已有 ${success_count} 次成功构建，跳过"
  exit 0
fi
if [ "${pending_count:-0}" -gt 0 ]; then
  echo "${LOG_TAG} 今天已有构建在进行中（${pending_count} 个），等它跑完，跳过"
  exit 0
fi

echo "${LOG_TAG} 今天还没有成功构建，触发 ${WORKFLOW}"
gh workflow run "${WORKFLOW}" --repo "${REPO}" --ref "${REF}" -f commit_status=true
