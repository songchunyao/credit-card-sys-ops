#!/usr/bin/env bash
# ===========================================================================
# M3.7 —— KG-T 门面（方案3 侧）一键启动脚本：:8303，主臂 5 工具（含 read_facts）
#
# 用途：把 `cc_tools_kgt.py` 以 FastAPI + uvicorn 后台启动在 :8303，
#       供编排层（Dify）按 docs/05 §2.5 绑定（App `cc-kgt-agent`）。
#
# ★ 与 `start_cc_tools.sh`（:8301 Set B 八工具）是**两个独立进程**：
#   进程隔离本身就是 KG-T 的纪律护栏（docs/09:72）——共进程会让 `cc_query`
#   出现在同一个 OpenAPI 文档里，KG-T 立刻退化成 KG-Q。
#
# 用法：
#     bash start_cc_tools_kgt.sh                       # 默认 0.0.0.0:8303，主臂 KG-T
#     CC_KGT_ARM=KG-T-dyn CC_KGT_PORT=8305 bash start_cc_tools_kgt.sh
#     CC_KGT_PYTHON=/opt/cc-venv/bin/python bash start_cc_tools_kgt.sh
#
# 环境变量（全部可选）：
#     CC_KGT_HOST     默认 0.0.0.0
#     CC_KGT_PORT     默认 8303（主臂契约端口）/ 8305（补充臂载体侧暂定；契约仍为 TBD）
#     CC_KGT_ARM      默认 KG-T（★ 补充臂必须显式指定；它不进主判据）
#     CC_KGT_PYTHON   默认：有 /opt/cc-venv/bin/python 就用它，否则 python3
#     CC_KGT_DIR      默认 /opt/cc-tools-kg（与 :8301 同目录、不同日志/PID 文件）
#     CC_KGT_LOG      默认 $CC_KGT_DIR/cc_tools_kgt.log
#     CC_KGT_PIDFILE  默认 $CC_KGT_DIR/cc_tools_kgt.pid
#     CC_KGT_AUDIT_LOG 默认 $CC_KGT_DIR/cc_tools_kgt_audit.jsonl（★ R-27 审计链落盘）
#                      ⇒ 本脚本**显式导出** `CC_AUDIT_LOG` 为该路径（追加式 JSONL，
#                      一行 = 一次工具调用，重启后继续追加）。`cc_tools.py` 侧语义：
#                      空/未设为「只落 stderr」（保持默认行为、不报错）。
#
# 只读约定：本门面对图**只读**（`collect_now` 亦为 UNWIRED 回执，不写图）；
#   图口令由 cc_tools.py 自己从 $CC_NEO4J_PASSWORD 或 /opt/upstream-agent/.env 的
#   `NEO4J_AUTH=` 读取，本脚本**不**接收、**不**打印、**不**落盘任何口令。
# ===========================================================================
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ★ 真机部署时脚本与实现在同一个 `scheme1-kg/service/` 子树下（见 CC_KGT_APP 覆盖）。
APP="${CC_KGT_APP:-$HERE/cc_tools_kgt.py}"

ARM="${CC_KGT_ARM:-KG-T}"
if [ "$ARM" = "KG-T" ]; then
  DEF_PORT=8303
else
  DEF_PORT=8305
fi
HOST="${CC_KGT_HOST:-0.0.0.0}"
PORT="${CC_KGT_PORT:-$DEF_PORT}"
if [ -n "${CC_KGT_PYTHON:-}" ]; then
  PYTHON="$CC_KGT_PYTHON"
elif [ -x /opt/cc-venv/bin/python ]; then
  PYTHON=/opt/cc-venv/bin/python
else
  PYTHON=python3
fi

# 日志/PID 与 :8301 那个进程**分开**，避免互相覆盖；写入失败退到 /tmp。
if [ -n "${CC_KGT_DIR:-}" ] && [ -w "$CC_KGT_DIR" -o -w "$(dirname "$CC_KGT_DIR")" ]; then
  RUN_DIR="$CC_KGT_DIR"
else
  RUN_DIR=/opt/cc-tools-kg
fi
LOG="${CC_KGT_LOG:-$RUN_DIR/cc_tools_kgt.log}"
PIDFILE="${CC_KGT_PIDFILE:-$RUN_DIR/cc_tools_kgt.pid}"
if ! ( mkdir -p "$RUN_DIR" && : >>"$LOG" ) 2>/dev/null; then
  RUN_DIR=/tmp
  LOG="${CC_KGT_LOG:-/tmp/cc-tools-kgt-$PORT.log}"
  PIDFILE="${CC_KGT_PIDFILE:-/tmp/cc-tools-kgt-$PORT.pid}"
  : >>"$LOG"
fi

# -- M3.6 / R-27：审计链必须落盘（显式导出，重启后路径不变）----------------
# 与 `start_cc_tools.sh`（:8301 主臂）同口径：追加式 JSONL，一行 = 一次工具调用。
AUDIT="${CC_KGT_AUDIT_LOG:-$RUN_DIR/cc_tools_kgt_audit.jsonl}"
export CC_AUDIT_LOG="$AUDIT"

if [ ! -f "$APP" ]; then
  echo "!! 找不到服务实现：$APP" >&2
  exit 2
fi

# -- 依赖隔离 -------------------------------------------------------------
# `topology_reader.py` 走 Bolt 驱动（`neo4j` 包），而共享 venv `/opt/cc-venv`
# **没有**该包。为了不污染别人的共享 venv，真机部署用 `pip install --target $RUN_DIR/pylib`
# 装到本目录下，再用 PYTHONPATH 挂上（纯追加、可整体删除、不影响其它服务）。
PYTHONPATH_EXTRA="${CC_KGT_PYTHONPATH:-}"
if [ -z "$PYTHONPATH_EXTRA" ] && [ -d "$RUN_DIR/pylib" ]; then
  PYTHONPATH_EXTRA="$RUN_DIR/pylib"
fi
if [ -n "$PYTHONPATH_EXTRA" ]; then
  export PYTHONPATH="$PYTHONPATH_EXTRA${PYTHONPATH:+:$PYTHONPATH}"
fi

# -- 口令引导（只读来源；绝不打印口令本体）--------------------------------
# 契约 `shared/deploy/targets.yaml` 声明：用户 `neo4j`，口令环境变量 `CC_NEO4J_PASSWORD`，
# 来源 = 生产机 `/opt/upstream-agent/.env` 的 `NEO4J_AUTH=user/pass`。
# 本脚本只在环境里没有该变量时，从该文件按契约声明的来源解析一次。
PW_SOURCE="（未设置）"
if [ -z "${CC_NEO4J_PASSWORD:-}" ]; then
  ENVF="${CC_NEO4J_ENV_FILE:-/opt/upstream-agent/.env}"
  if [ -r "$ENVF" ]; then
    CC_NEO4J_PASSWORD="$(sed -n 's/^NEO4J_AUTH=[^/]*\///p' "$ENVF" 2>/dev/null | head -1)"
    if [ -n "$CC_NEO4J_PASSWORD" ]; then
      export CC_NEO4J_PASSWORD
      PW_SOURCE="$ENVF 的 NEO4J_AUTH=（已导出到子进程环境，未打印、未落盘）"
    else
      PW_SOURCE="（$ENVF 里没有可解析的 NEO4J_AUTH ⇒ 服务将退回契约 fixture 并显式告警）"
    fi
  else
    PW_SOURCE="（环境变量缺失且 $ENVF 不可读 ⇒ 服务将退回契约 fixture 并显式告警）"
  fi
else
  PW_SOURCE="（已由调用方环境提供）"
fi

# -- 端口占用检查：绝不抢占既有服务（:8301/:8302 属他人）-------------------
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

cd "$HERE" || exit 2
echo "== 启动 KG-T 门面（方案3 侧，5 工具 + 禁令工具显式拒绝）=="
echo "  python    : $PYTHON"
echo "  app       : $APP"
echo "  arm       : $ARM （补充臂 = 不进主判据）"
echo "  监听      : $HOST:$PORT"
echo "  日志      : $LOG"
echo "  PID 文件  : $PIDFILE"
echo "  PYTHONPATH: ${PYTHONPATH_EXTRA:-（未追加）}"
echo "  审计落盘  : ${AUDIT:-（空 = 只落 stderr）}"
echo "  口令来源  : $PW_SOURCE"

nohup "$PYTHON" "$APP" --serve --arm "$ARM" --host "$HOST" --port "$PORT" \
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
      echo "  tools : http://127.0.0.1:${PORT}/tools"
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
