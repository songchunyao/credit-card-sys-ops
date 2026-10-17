#!/usr/bin/env bash
# =============================================================================
# deploy/lib/common.sh —— 部署脚本公共层（日志 / 断言 / 探活 / .env 读取）
#
# 被 install.sh 与 lib/*.sh source。**不要**直接执行本文件。
# 编码：UTF-8 无 BOM，LF。
# =============================================================================

# ── 颜色（非 TTY 自动关闭，避免写进日志时全是转义序列）──────────────────────
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
  C_BLU=$'\033[36m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_RST=""
fi

# ── 记账（install.sh 结束时报总账；任一 FAIL 即非零退出）────────────────────
CC_STEP_OK=0
CC_STEP_FAIL=0
CC_STEP_SKIP=0

_ts() { date '+%Y-%m-%d %H:%M:%S'; }

info()  { printf '%s[信息]%s %s\n'   "$C_BLU" "$C_RST" "$*"; }
ok()    { printf '%s[通过]%s %s\n'   "$C_GRN" "$C_RST" "$*"; CC_STEP_OK=$((CC_STEP_OK+1)); }
warn()  { printf '%s[警告]%s %s\n'   "$C_YEL" "$C_RST" "$*" >&2; }
err()   { printf '%s[失败]%s %s\n'   "$C_RED" "$C_RST" "$*" >&2; CC_STEP_FAIL=$((CC_STEP_FAIL+1)); }
skip()  { printf '%s[跳过]%s %s\n'   "$C_YEL" "$C_RST" "$*"; CC_STEP_SKIP=$((CC_STEP_SKIP+1)); }

# die：致命错误，**立刻中断**并给退出码（不给"继续跑下去"的机会）
die() { err "$*"; exit 1; }

# blocked：环境不满足（不是代码缺陷）—— 与 FAIL 区分，退出码 2
blocked() { printf '%s[受阻]%s %s\n' "$C_YEL" "$C_RST" "$*" >&2; exit 2; }

hdr() {
  printf '\n%s%s%s\n' "$C_BLD" "────────────────────────────────────────────────────────────────────────────" "$C_RST"
  printf '%s%s%s\n'   "$C_BLD" "$*" "$C_RST"
  printf '%s%s%s\n'   "$C_BLD" "────────────────────────────────────────────────────────────────────────────" "$C_RST"
}

# ── 依赖命令 ─────────────────────────────────────────────────────────────────
require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "缺少必需命令：$c（请先安装后重试）"
  done
}

# ── HTTP 探活 ────────────────────────────────────────────────────────────────
# 优先 curl，退到 wget；两者都无 → BLOCKED（而不是"当作不可达"）
_http_get() {
  local url="$1" timeout="${2:-5}"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time "$timeout" "$url" 2>/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O - --timeout="$timeout" "$url" 2>/dev/null
  else
    return 127
  fi
}

# http_ok URL [timeout] → 0 可达且 2xx/3xx；非 0 不可达
http_ok() {
  local url="$1" timeout="${2:-5}" out rc
  out="$(_http_get "$url" "$timeout")"; rc=$?
  if [ "$rc" = "127" ]; then
    blocked "宿主上既无 curl 也无 wget ⇒ 无法做就绪探活。请先安装 curl 再重试。"
  fi
  [ "$rc" = "0" ] && [ -n "$out" ]
}

# ── 内容感知探活（**防"端口上恰好有别的服务"**）─────────────────────────────
# ★ 来自一次真实假阴/假阳：联调机 192.0.2.11 的 :8080 上跑着 **GitLab 的 puma**，
#   `curl http://127.0.0.1:8080/console/api/setup` 返回 **302 → /users/sign_in**（一段 HTML）。
#   而 `http_ok` 用的是 `curl -f`（只在 ≥400 时失败）⇒ 302 + 非空 body ⇒ 判"可达"
#   ⇒ 外部依赖体检报出 **"③ Dify 已存在且可达"**，而实际根本没有 Dify。
#   **一个被别的服务骗过的探针，比没有探针更糟。**
#
# ⇒ 凡"判断某组件是否已安装"的场合，一律要求**响应内容符合该组件的形状**：
#   · 必须是 JSON（不是 HTML 登录页）；
#   · 必须含该组件特有的键。
# _json_has_key URL KEY [TIMEOUT]
_json_has_key() {
  local url="$1" key="$2" t="${3:-5}" body
  body="$(_http_get "$url" "$t")" || return 1
  [ -n "$body" ] || return 1
  # HTML 一律不接受（外来服务的典型形态：登录页/门户页）
  case "$body" in
    *"<html"*|*"<!DOCTYPE"*|*"<HTML"*) return 1 ;;
  esac
  printf '%s' "$body" | grep -q "\"${key}\"" || return 1
  return 0
}

# _json_is_array URL [TIMEOUT]：响应是 JSON 数组（GraphDB /rest/repositories 的形状）
_json_is_array() {
  local url="$1" t="${2:-5}" body
  body="$(_http_get "$url" "$t")" || return 1
  printf '%s' "$body" | head -c 1 | grep -q '\[' || return 1
  case "$body" in *"<html"*|*"<!DOCTYPE"*) return 1 ;; esac
  return 0
}

# wait_http URL 超时秒 描述 → 轮询直到可达；超时返回 1（调用方决定是否致命）
wait_http() {
  local url="$1" secs="${2:-120}" label="${3:-$1}" i=0
  info "等待 $label 就绪（最多 ${secs}s）：$url"
  while [ "$i" -lt "$secs" ]; do
    if http_ok "$url" 5; then ok "$label 已就绪（${i}s）"; return 0; fi
    i=$((i+3)); sleep 3
  done
  err "$label 在 ${secs}s 内未就绪：$url"
  return 1
}

# ── 端口占用（用于"绝不抢占既有服务"的断言）──────────────────────────────────
# 返回 0 = 被占用。优先 ss → netstat → lsof
port_busy() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1   # 探测工具都没有 ⇒ 当作未占用（并在 preflight 里显式登记这一点）
  fi
}

port_owner() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | grep -E "[:.]${port}\b" || true
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
  fi
}

# ── .env 读取（只读键值，不 export 到全局，避免误覆盖）──────────────────────
# env_get FILE KEY [DEFAULT]
env_get() {
  local file="$1" key="$2" def="${3:-}" line val
  [ -f "$file" ] || { printf '%s' "$def"; return; }
  line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1)"
  [ -n "$line" ] || { printf '%s' "$def"; return; }
  val="${line#*=}"
  # 去掉行尾注释与引号与首尾空白（本项目的值均为简单标量，不需要完整 shell 解析）
  val="$(printf '%s' "$val" | sed -e 's/[[:space:]]*#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'\$//")"
  printf '%s' "${val:-$def}"
}

# ── .env 写入（原地改一个键，不存在则追加）──────────────────────────────────
# ★ 为什么需要它：install.sh 从离线包的 VERSION 解析出**权威镜像标签**，
#   而 compose 是从 .env 读 ${CC_IMAGE_TAG}。只改 shell 变量**没用** ——
#   compose 会继续按 .env 里的旧标签去找镜像，报 "image not found"。
#   这里把解析结果**落回 .env**，让"这一次"和"以后手工跑 compose"口径一致。
#   （另有一条同样的坑：环境变量必须 `export` 才胜过 .env 文件；本函数直接改文件，
#     绕开这个隐式规则，行为更可预期。）
env_set() {
  local file="$1" key="$2" val="$3" tmp
  [ -f "$file" ] || { err "env_set：文件不存在 $file"; return 1; }
  if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$file"; then
    tmp="$(mktemp)"
    # 用 | 作分隔符，避免值里的 / 破坏 sed 表达式
    sed -E "s|^([[:space:]]*${key}[[:space:]]*=).*$|\1${val}|" "$file" > "$tmp" && mv "$tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$val" >> "$file"
  fi
}

# ── 快照/回滚辅助（部署失败时给出可复核的现场）─────────────────────────────
need_root_or_docker_group() {
  if [ "$(id -u)" != "0" ]; then
    if ! docker info >/dev/null 2>&1; then
      die "当前用户既非 root，也不能访问 docker 守护进程。请用 root 运行，或把该用户加入 docker 组后重登。"
    fi
  fi
}
