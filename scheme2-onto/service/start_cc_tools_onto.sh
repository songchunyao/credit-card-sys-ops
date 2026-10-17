#!/usr/bin/env bash
# ===========================================================================
# M3.3 —— Set B 八工具知识服务（方案2 / ONTO 侧）一键启动脚本
#
# 用途：把 `cc_tools_onto.py` 以 FastAPI + uvicorn 后台启动在 :8302，
#       供编排层按 docs/05 §2.1 绑定，与方案1（KG 侧 :8301）工具面同形。
#
# 用法：
#     bash start_cc_tools_onto.sh                 # 默认 0.0.0.0:8302
#     CC_TOOLS_ONTO_PORT=8302 bash start_cc_tools_onto.sh
#     CC_TOOLS_ONTO_PYTHON=/opt/cc-venv/bin/python bash start_cc_tools_onto.sh
#
# 环境变量（全部可选）：
#     CC_TOOLS_ONTO_HOST     默认 0.0.0.0
#     CC_TOOLS_ONTO_PORT     默认 8302（★ 契约端口，绝不占用 8301/7200/7475 等既有端口）
#     CC_TOOLS_ONTO_PYTHON   默认：有 /opt/cc-venv/bin/python 就用它，否则 python3
#     CC_TOOLS_ONTO_LOG      默认 /opt/cc-tools-onto/cc_tools_onto.log
#     CC_TOOLS_ONTO_PIDFILE  默认 /opt/cc-tools-onto/cc_tools_onto.pid
#     CC_AUDIT_LOG           ★ R-27 审计链落盘（追加式 JSONL）。默认
#                            <服务目录>/cc_tools_onto_audit.jsonl；本脚本**显式
#                            export** 给服务进程，保证重启后路径不变、内容继续追加。
#                            非空 = 绝对路径；服务侧未设/空 ⇒ 不落盘（默认行为）。
#     CC_GRAPHDB_URL         默认 http://127.0.0.1:7200（可以不在本脚本里给）
#     CC_GRAPHDB_REPOSITORY  默认 cc-ops
#     CC_GRAPHDB_TBOX_GRAPH  默认 <ONTO base>graph/tbox
#     CC_GRAPHDB_SNAPSHOT_ID 默认自动解析最新快照图
#
# 只读约定：服务对 GraphDB **只读**（只发 SELECT/ASK/CONSTRUCT/DESCRIBE）；
#   `targets.yaml` 实测该 GraphDB `auth_type: none` ⇒ 服务不需要任何凭据，
#   本脚本**不**接收、**不**打印、**不**落盘任何口令。
# ===========================================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/cc_tools_onto.py"

HOST="${CC_TOOLS_ONTO_HOST:-0.0.0.0}"
PORT="${CC_TOOLS_ONTO_PORT:-8302}"
if [ -n "${CC_TOOLS_ONTO_PYTHON:-}" ]; then
  PYTHON="$CC_TOOLS_ONTO_PYTHON"
elif [ -x /opt/cc-venv/bin/python ]; then
  PYTHON=/opt/cc-venv/bin/python
else
  PYTHON=python3
fi

RUN_DIR="$(dirname "$APP")"
LOG="${CC_TOOLS_ONTO_LOG:-$RUN_DIR/cc_tools_onto.log}"
PIDFILE="${CC_TOOLS_ONTO_PIDFILE:-$RUN_DIR/cc_tools_onto.pid}"
if ! ( : >>"$LOG" ) 2>/dev/null; then
  LOG=/tmp/cc-tools-onto.log
  PIDFILE=/tmp/cc-tools-onto.pid
  : >>"$LOG"
fi

# -- R-27：审计链落盘路径（显式 export 给服务进程；追加语义，绝不 truncate）--
AUDIT="${CC_AUDIT_LOG:-$RUN_DIR/cc_tools_onto_audit.jsonl}"
if ! ( : >>"$AUDIT" ) 2>/dev/null; then
  AUDIT=/tmp/cc-tools-onto-audit.jsonl
  : >>"$AUDIT"
fi
export CC_AUDIT_LOG="$AUDIT"

if [ ! -f "$APP" ]; then
  echo "!! 找不到服务实现：$APP" >&2
  exit 2
fi

# -- 端口占用检查：绝不抢占既有服务 --------------------------------------
if command -v ss >/dev/null 2>&1; then
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
    echo "!! 端口 $PORT 已被占用（既有服务不得重启/占用）：" >&2
    ss -lntp 2>/dev/null | grep -E "[:.]${PORT}\b" >&2 || true
    exit 2
  fi
fi
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "!! 已有实例在跑（PID $(cat "$PIDFILE") ⇒ $PIDFILE）；先停掉再启动。" >&2
  exit 2
fi

cd "$RUN_DIR" || exit 2
echo "== 启动 Set B 八工具知识服务（ONTO 侧）=="
echo "  python    : $PYTHON"
echo "  app       : $APP"
echo "  监听      : $HOST:$PORT"
echo "  日志      : $LOG"
echo "  PID 文件  : $PIDFILE"
echo "  审计 JSONL: $CC_AUDIT_LOG  (R-27 追加式；服务侧未设则回 \"\")"

nohup "$PYTHON" "$APP" --serve --host "$HOST" --port "$PORT" \
  </dev/null >>"$LOG" 2>&1 &
PID=$!
echo "$PID" >"$PIDFILE"
echo "  PID       : $PID"

# -- 等待 /health 就绪（最多 ~30s；首查要解析快照图 + 建类型索引）-------
HEALTH="http://127.0.0.1:${PORT}/health"
for _ in $(seq 1 60); do
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
      echo "  openapi: http://127.0.0.1:${PORT}/openapi.json"
      exit 0
    fi
  else
    echo "== 已启动（本机无 curl，未探活）：$HEALTH =="
    exit 0
  fi
done

echo "!! 30s 内 /health 未就绪；日志尾部：" >&2
tail -n 30 "$LOG" >&2
exit 1
