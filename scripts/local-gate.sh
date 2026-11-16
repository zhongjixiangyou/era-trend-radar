#!/usr/bin/env bash
# 本地等价门禁 —— 提交前一条命令，跑与 CI 相同的检查。
# 存在的理由：过去多次「本地改完觉得没问题，push 后 CI 红」，代价是一整轮采集作废。
# 用法：./scripts/local-gate.sh [--fast]
#   --fast  跳过构建，只跑内容与草稿校验（改代码/配置时用，约 5 秒）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

fail=0
step=0

run() {
  local name="$1"; shift
  step=$((step + 1))
  printf '\n[%d] %s\n' "$step" "$name"
  if "$@"; then
    printf '    ✓ %s\n' "$name"
  else
    printf '    ✗ %s（这一步没过，后面的不用看了）\n' "$name"
    fail=1
    return 1
  fi
}

run "内容校验（与 CI 同一套契约）" python3 scripts/cli.py validate content/ || exit 1
run "草稿校验" python3 scripts/check_drafts.py || exit 1

if [ "$FAST" -eq 0 ]; then
  run "构建 + 泄漏检查（等价 deploy 产物）" pnpm build || exit 1
fi

printf '\n全部通过。可以提交了。\n'
exit "$fail"
