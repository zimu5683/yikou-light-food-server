#!/usr/bin/env bash
# R7 独立验收：一键复跑（隔离副本 + 临时数据目录 + 外部网络阻断）。
#
#   bash tools/r7-acceptance/run_all.sh
#
# 设计约束（与 docs/OPTIMIZATION-FINAL-ACCEPTANCE-R7.md 一致）：
#   * 不修改工作区任何业务/测试/配置；所有产物在 $WORK 下；
#   * 不联网：副本根放 sitecustomize.py 劫持 socket.connect/connect_ex/sendto，
#     非回环一律 OSError(101)，子进程经 PYTHONPATH 继承；
#   * 不登录真实平台、不写真实云表：WPS 探针全部使用 tests 里的 FakeCli 合成云表；
#   * 日志写到 tools/r7-acceptance/logs/。
set -u
REPO="${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"
WORK="${WORK:-/tmp/yikou-r7-runall}"
LOGS="$REPO/tools/r7-acceptance/logs"
mkdir -p "$WORK" "$LOGS"

echo "== 1) 隔离副本 =="
rm -rf "$WORK/py" "$WORK/tmp" "$WORK/data"
mkdir -p "$WORK/py" "$WORK/tmp" "$WORK/data"
tar -cf - -C "$REPO" --exclude=.git --exclude=node_modules --exclude=dist \
    --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache . \
  | tar -xf - -C "$WORK/py"

cat > "$WORK/py/sitecustomize.py" <<'PY'
"""R7 acceptance network guard: block any non-loopback egress at the socket layer."""
import ipaddress, socket
_oc, _oce, _os_ = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
def _host(a): return str(a[0]) if isinstance(a, tuple) and a else ""
def _local(h):
    t = str(h or "").strip("[]")
    if t in ("", "localhost", "ip6-localhost"): return True
    try: return ipaddress.ip_address(t).is_loopback
    except ValueError: return False
def _c(self, a):
    if not _local(_host(a)): raise OSError(101, f"R7 guard blocked connect to {_host(a)!r}")
    return _oc(self, a)
def _ce(self, a): return _oce(self, a) if _local(_host(a)) else 101
def _st(self, d, *a):
    h = _host(a[-1]) if a else ""
    if h and not _local(h): raise OSError(101, f"R7 guard blocked sendto {h!r}")
    return _os_(self, d, *a)
socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = _c, _ce, _st
PY

echo "== 2) 网络守卫自检（应 EXTERNAL_BLOCKED 101）=="
( cd "$WORK/py" && PYTHONPATH="$WORK/py" python3 -c "
import socket
try:
    socket.create_connection(('1.1.1.1',80),timeout=3); print('EXTERNAL_ALLOWED')
except OSError as e: print('EXTERNAL_BLOCKED errno=',getattr(e,'errno',None))
" )

export TMPDIR="$WORK/tmp" YIKOU_DATA_DIR="$WORK/data"
export PYTHONPATH="$WORK/py:$WORK/py/tests"

echo "== 3) 后端全量门禁 =="
( cd "$WORK/py" && python3 -m pytest -q -p no:cacheprovider ) \
  > "$LOGS/backend-pytest.log" 2>&1
echo "backend pytest exit=$? -> $(tail -1 "$LOGS/backend-pytest.log")"

echo "== 4) git diff --check =="
( cd "$REPO" && git diff --check ); echo "diff-check exit=$?"

echo "== 5) 独立反证探针 =="
for p in probe_w1_bridge_double_write probe_w1_stale_plan probe_w2w3w4_journal_gate \
         probe_http_gates probe_local_safety; do
  ( cd "$WORK/py" && python3 "$REPO/tools/r7-acceptance/$p.py" ) \
    > "$LOGS/$p.log" 2>&1
  echo "  $p exit=$? -> $(tail -1 "$LOGS/$p.log")"
done

echo "== 6) 变异敏感性（探针必须能抓住被还原的回归）=="
for mut in "--mutate-r6-wiring" "--mutate-revert-w2" "--mutate-open-gates"; do
  case "$mut" in
    --mutate-r6-wiring) p=probe_w1_stale_plan ;;
    --mutate-revert-w2) p=probe_w2w3w4_journal_gate ;;
    --mutate-open-gates) p=probe_http_gates ;;
  esac
  ( cd "$WORK/py" && python3 "$REPO/tools/r7-acceptance/$p.py" "$mut" ) \
    > "$LOGS/$p.mutated.log" 2>&1
  echo "  $p $mut exit=$? -> $(tail -1 "$LOGS/$p.mutated.log")"
done

echo "== 7) 前端门禁（需 node_modules；不在本脚本内安装依赖）=="
if [ -d "$REPO/frontend/node_modules" ]; then
  rm -rf "$WORK/fe"; mkdir -p "$WORK/fe"
  tar -cf - -C "$REPO/frontend" --exclude=node_modules --exclude=dist . \
    | tar -xf - -C "$WORK/fe"
  cp -a "$REPO/frontend/node_modules" "$WORK/fe/node_modules"
  ( cd "$WORK/fe" && { echo "### TEST"; npm test --silent; echo "TEST_EXIT=$?"; } ) \
    > "$LOGS/frontend-test.log" 2>&1
  ( cd "$WORK/fe" && { echo "### LINT"; npm run lint --silent; echo "LINT_EXIT=$?"; } ) \
    > "$LOGS/frontend-lint.log" 2>&1
  ( cd "$WORK/fe" && { echo "### TSC"; npx tsc -p tsconfig.app.json --noEmit; echo "TSC_APP_EXIT=$?"; } ) \
    > "$LOGS/frontend-tsc-app.log" 2>&1
  ( cd "$WORK/fe" && { echo "### BUILD"; npx vite build; echo "BUILD_EXIT=$?"; } ) \
    > "$LOGS/frontend-build.log" 2>&1
  ( cd "$WORK/fe" && { echo "### BROWSER"; node scripts/browser-interaction-check.mjs; echo "BROWSER_EXIT=$?"; } ) \
    > "$LOGS/frontend-browser.log" 2>&1
  for f in test lint tsc-app build browser; do
    echo "  frontend $f -> $(grep -E '_EXIT=' "$LOGS/frontend-$f.log" | tail -1)"
  done
else
  echo "  （跳过：未找到 frontend/node_modules）"
fi

echo "== 完成：日志在 $LOGS =="
