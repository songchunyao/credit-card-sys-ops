#!/usr/bin/env bash
# ===========================================================================
# S3a-2 —— 中性取数服务（`read_facts`）一键启停脚本
#
# 用途：把 `shared/collect/live_facts_service.py` 以 HTTP 服务（标准库 `http.server`
#       + 第三方 PyYAML，见下方 CC_LIVE_FACTS_PYTHON 说明）后台启动在 :8304，供两个用途调用：
#         ① 三载体（:8301 KG-Q / :8302 ONTO-Q / :8303 KG-T）的 `read_facts`
#            工具**纯代理透传**到 `http://127.0.0.1:8304/tools/read_facts`（默认基址）；
#         ② Dify API 提供者 `cc-live-facts-tools`（免费臂别名 `read_live_facts`）
#            直连 `http://192.0.2.10:8304/tools/read_facts`。
#       ⇒ 因此**必须绑 0.0.0.0**（Dify 容器从宿主外访问），不能只绑 127.0.0.1。
#
# 用法：
#     bash start_cc_live_facts.sh              # 启动（live 模式：无进程级用例）
#     M5_CASE_ID=A-01 bash start_cc_live_facts.sh   # 启动并**固定该例**（判决模式）
#     bash start_cc_live_facts.sh status        # 看 pid/端口/健康
#     bash start_cc_live_facts.sh stop          # 停（按 pidfile，SIGTERM → 等 10s → SIGKILL）
#
# ★ 用例选择是**进程级信道**（D48）：CLI `--case` 优先，其次 env `M5_CASE_ID`；
#   进程生命周期内不可变，**请求体里没有也不接受用例参数**（带 `case_id` 会 400
#   `UNKNOWN_PARAM`）。故**切例 = 停 + 换 env 再启动**，绝不存在“同一进程服务多例”。
#
# 环境变量（全部可选）：
#     CC_LIVE_FACTS_HOST     默认 0.0.0.0（见上：必须容器可达）
#     CC_LIVE_FACTS_PORT     默认 8304（★ 契约端口，不要占用其它既有端口）
#     CC_LIVE_FACTS_PYTHON   默认：/opt/cc-venv/bin/python（**必须带 PyYAML**），否则 python3
#     CC_LIVE_FACTS_ROOT     默认取脚本所在目录的 ../..（即部署根 DB，镜像布局 shared/… + tmp/…）
#     CC_LIVE_FACTS_LOG      默认 $ROOT/cc_live_facts.log（不可写则退到 /tmp）
#     CC_LIVE_FACTS_PIDFILE  默认 $ROOT/cc_live_facts.pid
#     M5_CASE_ID             非空 ⇒ 以 `--case "$M5_CASE_ID"` 启动（该例不存在 ⇒ 进程拒绝启动/请求 409）
#
# 部署形态（S3a-2，镜像布局，缺任一件都会 fail-closed）：
#     $ROOT/shared/collect/live_facts_service.py
#     $ROOT/shared/collect/live_facts.py
#     $ROOT/shared/collect/freeze.py
#     $ROOT/shared/verify/equivalence.py          # freeze.py:69-70 / live_facts.py:216 依赖
#     $ROOT/shared/model/m5-input-facts.yaml
#     $ROOT/shared/model/ccf.schema.json
#     $ROOT/tmp/ccf_live5.json                    # 基底快照（m5-input-facts.yaml 的 base_ref）
#
# 只读约定：服务对事实面**只读**，不写任何图库/本体；本脚本**不**接收、**不**打印、
#   **不**落盘任何口令。本服务**不写审计链**（S3a-2 登记：审计链声明域只含三载体；
#   免费臂经 Dify 会话日志覆盖）。故本脚本**不**导出 `CC_AUDIT_LOG`。
# ===========================================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CC_LIVE_FACTS_ROOT:-$(cd "$HERE/../.." && pwd)}"
APP="$HERE/live_facts_service.py"

HOST="${CC_LIVE_FACTS_HOST:-0.0.0.0}"
PORT="${CC_LIVE_FACTS_PORT:-8304}"
if [ -n "${CC_LIVE_FACTS_PYTHON:-}" ]; then
  PYTHON="$CC_LIVE_FACTS_PYTHON"
elif [ -x /opt/cc-venv/bin/python ]; then
  PYTHON=/opt/cc-venv/bin/python
else
  PYTHON=python3
fi

LOG="${CC_LIVE_FACTS_LOG:-$ROOT/cc_live_facts.log}"
PIDFILE="${CC_LIVE_FACTS_PIDFILE:-$ROOT/cc_live_facts.pid}"
if ! ( : >>"$LOG" ) 2>/dev/null; then
  LOG=/tmp/cc-live-facts.log
  PIDFILE=/tmp/cc-live-facts.pid
  : >>"$LOG"
fi

HEALTH="http://127.0.0.1:${PORT}/health"
ACTION="${1:-start}"

port_busy() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"
}

pid_alive() {
  [ -f "$PIDFILE" ] || return 1
  local p
  p="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

case "$ACTION" in
  stop)
    if pid_alive; then
      PID="$(cat "$PIDFILE")"
      echo "== 停止 :$PORT（PID $PID）=="
      kill "$PID" 2>/dev/null || true
      for _ in $(seq 1 20); do
        sleep 0.5
        kill -0 "$PID" 2>/dev/null || break
      done
      if kill -0 "$PID" 2>/dev/null; then
        echo "   SIGTERM 后 10s 未退 ⇒ SIGKILL"
        kill -9 "$PID" 2>/dev/null || true
      fi
      rm -f "$PIDFILE"
      echo "== 已停止 =="
    else
      echo "== 无 pidfile 实例（$PIDFILE）=="
    fi
    if port_busy; then
      echo "!! 但 :$PORT 仍被占用（可能是别的进程，**不要**强杀既有服务）：" >&2
      ss -lntp 2>/dev/null | grep -E "[:.]${PORT}\b" >&2 || true
      exit 2
    fi
    exit 0
    ;;
  status)
    echo "  pidfile : $PIDFILE"
    echo "  端口    : $PORT"
    pid_alive && echo "  pid     : $(cat "$PIDFILE")（在跑）" || echo "  pid     : 无"
    if command -v curl >/dev/null 2>&1; then
      echo -n "  /health : "; curl -sS --max-time 5 "$HEALTH" || echo "（不可达）"
      echo
    fi
    exit 0
    ;;
  start) ;;
  *) echo "用法：bash $(basename "$0") [start|stop|status]" >&2; exit 2 ;;
esac

if [ ! -f "$APP" ]; then
  echo "!! 找不到服务实现：$APP" >&2
  exit 2
fi
for f in "$HERE/live_facts.py" "$HERE/freeze.py" "$ROOT/shared/verify/equivalence.py" \
         "$ROOT/shared/model/m5-input-facts.yaml" "$ROOT/shared/model/ccf.schema.json" \
         "$ROOT/shared/model/v3-ccf-map.yaml" "$ROOT/shared/model/topology.yaml" \
         "$ROOT/tmp/ccf_live5.json"; do
  if [ ! -f "$f" ]; then
    echo "!! 部署闭包缺件：$f（缺件会 fail-closed：409 BLOCKED_BASE / 500 INTERNAL）" >&2
    exit 2
  fi
done

# -- 端口占用检查：绝不抢占既有服务（:8301/:8302/:8303 等）-----------------
if port_busy; then
  echo "!! 端口 $PORT 已被占用（既有服务不得重启/占用）：" >&2
  ss -lntp 2>/dev/null | grep -E "[:.]${PORT}\b" >&2 || true
  exit 2
fi
if pid_alive; then
  echo "!! 已有实例在跑（PID $(cat "$PIDFILE") ⇒ $PIDFILE）；先 stop 再启动。" >&2
  exit 2
fi

CASE_ARGS=()
CASE_NOTE="（live 模式：无进程级用例）"
if [ -n "${M5_CASE_ID:-}" ]; then
  CASE_ARGS=(--case "$M5_CASE_ID")
  CASE_NOTE="$M5_CASE_ID（进程级固定；切例必须 stop 后重启）"
fi

cd "$HERE" || exit 2
echo "== 启动中性取数服务 read_facts =="
echo "  python    : $PYTHON"
echo "  app       : $APP"
echo "  部署根    : $ROOT"
echo "  监听      : $HOST:$PORT"
echo "  用例      : $CASE_NOTE"
echo "  日志      : $LOG"
echo "  PID 文件  : $PIDFILE"

nohup "$PYTHON" "$APP" --serve --host "$HOST" --port "$PORT" "${CASE_ARGS[@]+"${CASE_ARGS[@]}"}" \
  </dev/null >>"$LOG" 2>&1 &
PID=$!
echo "$PID" >"$PIDFILE"
echo "  PID       : $PID"

# -- 等待 /health 就绪（最多 ~20s）---------------------------------------
for _ in $(seq 1 40); do
  sleep 0.5
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "!! 进程已退出，日志尾部：" >&2
    tail -n 30 "$LOG" >&2
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1; then
      echo "== 就绪：$HEALTH =="
      curl -sS --max-time 5 "$HEALTH"
      echo
      echo "  取数面: http://127.0.0.1:${PORT}/tools/read_facts（POST）"
      echo "  ★ 用例=$CASE_NOTE"
      exit 0
    fi
  else
    echo "== 已启动（本机无 curl，未探活）：$HEALTH =="
    exit 0
  fi
done

echo "!! 20s 内 /health 未就绪；日志尾部：" >&2
tail -n 30 "$LOG" >&2
exit 1
