#!/usr/bin/env bash
# ===========================================================================
# M3.2 —— Set B 八工具知识服务（方案1 / KG 侧）一键启动脚本
#
# 用途：把 `cc_tools.py` 以 FastAPI + uvicorn 后台启动在 :8301，
#       供编排层（Dify）按 docs/05 §2.1 绑定（App `cc-kg-agent`，工具提供者 `cc-kb-tools`）。
#
# 用法：
#     bash start_cc_tools.sh                 # 默认 0.0.0.0:8301，日志 $LOG，PID $PIDFILE
#     CC_TOOLS_PORT=8301 bash start_cc_tools.sh
#     CC_TOOLS_PYTHON=/opt/cc-venv/bin/python bash start_cc_tools.sh
#
# 环境变量（全部可选）：
#     CC_TOOLS_HOST     默认 0.0.0.0
#     CC_TOOLS_PORT     默认 8301（★ 契约端口，不要占用其它既有端口）
#     CC_TOOLS_PYTHON   默认：有 /opt/cc-venv/bin/python 就用它，否则 python3
#     CC_TOOLS_LOG      默认 /opt/cc-tools-kg/cc_tools.log（不可写则退到 /tmp）
#     CC_TOOLS_PIDFILE  默认 /opt/cc-tools-kg/cc_tools.pid
#     CC_TOOLS_AUDIT_LOG 默认 /opt/cc-tools-kg/cc_tools_audit.jsonl（★ R-27 审计链落盘）
#                       ⇒ 本脚本**显式导出** `CC_AUDIT_LOG` 为该路径（追加式 JSONL，
#                       一行 = 一次工具调用，重启后继续追加）。cc_tools.py 侧语义：
#                       空/未设为「只落 stderr」；直接起 app 想关掉就传空串。
#
# 只读约定：服务对图**只读**；图口令由 cc_tools.py 自己从
#   $CC_NEO4J_PASSWORD 或 /opt/upstream-agent/.env 的 NEO4J_AUTH= 读取，
#   本脚本**不**接收、**不**打印、**不**落盘任何口令。
# ===========================================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/cc_tools.py"

HOST="${CC_TOOLS_HOST:-0.0.0.0}"
PORT="${CC_TOOLS_PORT:-8301}"
if [ -n "${CC_TOOLS_PYTHON:-}" ]; then
  PYTHON="$CC_TOOLS_PYTHON"
elif [ -x /opt/cc-venv/bin/python ]; then
  PYTHON=/opt/cc-venv/bin/python
else
  PYTHON=python3
fi

RUN_DIR="$(dirname "$APP")"
LOG="${CC_TOOLS_LOG:-$RUN_DIR/cc_tools.log}"
PIDFILE="${CC_TOOLS_PIDFILE:-$RUN_DIR/cc_tools.pid}"
if ! ( : >>"$LOG" ) 2>/dev/null; then
  LOG=/tmp/cc-tools-kg.log
  PIDFILE=/tmp/cc-tools-kg.pid
  : >>"$LOG"
fi

# -- M3.6 / R-27：审计链必须落盘（显式导出，重启后路径不变）----------------
# 一行 = 一次工具调用，`cc_tools.py` 以 `open(..., "a")` 追加写 ⇒ 重启只追加、
# 不清空既有审计行。放在 LOG 回退之后算，保证 $RUN_DIR 已是最终值。
AUDIT="${CC_TOOLS_AUDIT_LOG:-$RUN_DIR/cc_tools_audit.jsonl}"
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
echo "== 启动 Set B 八工具知识服务 =="
echo "  python    : $PYTHON"
echo "  app       : $APP"
echo "  监听      : $HOST:$PORT"
echo "  日志      : $LOG"
echo "  PID 文件  : $PIDFILE"
echo "  审计落盘  : ${AUDIT:-（空 = 只落 stderr）}"

nohup "$PYTHON" "$APP" --serve --host "$HOST" --port "$PORT" \
  </dev/null >>"$LOG" 2>&1 &
PID=$!
echo "$PID" >"$PIDFILE"
echo "  PID       : $PID"

# -- 等待 /health 就绪（最多 ~20s）---------------------------------------
HEALTH="http://127.0.0.1:${PORT}/health"
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
      echo "  openapi: http://127.0.0.1:${PORT}/openapi.json"
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
