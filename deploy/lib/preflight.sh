#!/usr/bin/env bash
# =============================================================================
# deploy/lib/preflight.sh —— 环境预检（**只读，不改任何东西**）
#
# 设计原则：能在起栈前证伪的，绝不留到运行期。
#   · 硬前置（缺了必然装不起来）→ die（退出码 1）
#   · 环境不满足而**不是**代码缺陷 → blocked（退出码 2）
#     （沿用本项目既有的四态语义：0=PASS / 1=FAIL / 2=BLOCKED）
#   · 可能让"装起来了但采不到数据"的配置缺失 → warn，并交给 verify_deploy 去断言
#
# 用法（由 install.sh source 后调用）：
#     preflight_run <deploy_dir> <mode>
# =============================================================================

# 宿主侧探活地址：把容器专用的 host.docker.internal 换回 127.0.0.1
#   ★ 这一步不能省：install.sh 在**宿主**上跑，宿主解析不了 host.docker.internal，
#     直接拿 .env 里的值去探活会得到"载体不可达"的**假故障**。
_to_host_url() {
  printf '%s' "$1" | sed -e 's#//host\.docker\.internal#//127.0.0.1#g'
}

# 本项目容器**自己**发布的端口集合（用于区分"自己的旧实例"与"别人的服务"）
# ★ 为什么需要：install.sh 是可重复执行的（幂等）。第二次跑来时，端口正被
#   **上一轮自己起的容器**占着。若把这也判成"抢占既有服务"而拒绝，
#   就等于"本项目不允许重跑/升级自己" —— 显然不对。
#   纪律的原意是「绝不抢占**别人的**服务」，所以必须把两者区分开。
CC_OWN_PORTS=""

collect_own_ports() {
  CC_OWN_PORTS=""
  command -v docker >/dev/null 2>&1 || return 0
  # 只认本项目的容器名前缀 cc-ops-（以及 compose 服务的容器）
  local lines
  lines="$(docker ps --filter 'name=cc-ops-' --format '{{.Ports}}' 2>/dev/null || true)"
  [ -n "$lines" ] || return 0
  # 形如 0.0.0.0:8187->8187/tcp, 0.0.0.0:7475->7474/tcp ⇒ 取宿主侧端口
  CC_OWN_PORTS="$(printf '%s\n' "$lines" \
    | grep -oE '0\.0\.0\.0:[0-9]+->' | sed -E 's/0\.0\.0\.0:([0-9]+)->/\1/' | sort -u | tr '\n' ' ')"
}

# is_own_port PORT → 0 表示该端口属于本项目自己的容器
is_own_port() {
  case " $CC_OWN_PORTS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}
CC_APP_PORTS="8187 8301 8302 8303 8304"
CC_CARRIER_PORTS="7475 7688 7200"

preflight_run() {
  local ddir="$1" mode="$2"
  local envf="$ddir/.env"

  hdr "① 环境预检（只读）"

  # ── 0. 离线包自检：**必须排在任何联网/拉取动作之前** ──────────────────────
  #   · 缺件、架构不符 ⇒ BLOCKED（包与目标机不匹配，不是代码缺陷）
  #   · 行尾 CRLF / 包自相矛盾 ⇒ FAIL（这类问题装到一半才炸，且报错形态会误导）
  #   install.sh 会把 deploy/lib/offline.sh source 进来；直接调用本文件时该函数不存在，
  #   故用 command -v 兜底（避免"source 顺序变了就 command not found"）。
  if [ "${CC_OFFLINE:-0}" = "1" ]; then
    if command -v offline_preflight >/dev/null 2>&1; then
      offline_preflight "${CC_OFFLINE_ROOT:-$(cd "$ddir/.." && pwd)}"
    else
      die "CC_OFFLINE=1 但 deploy/lib/offline.sh 未加载 —— 安装脚本不完整，拒绝继续。"
    fi
  fi

  # ── 1. 操作系统 ──────────────────────────────────────────────────────────
  case "$(uname -s)" in
    Linux) ok "操作系统：$(uname -s) $(uname -r)" ;;
    *)     die "本部署脚本只支持 Linux（当前 $(uname -s)）。Windows/macOS 请用目标 Linux 机执行。" ;;
  esac

  # ── 2. CPU 架构（镜像与架构必须一致，见下方行 3 的 load 校验）────────────
  local arch; arch="$(uname -m)"
  info "CPU 架构：$arch（离线包镜像必须与之一致；不一致会在 docker load 后由 verify 拦下）"
  case "$arch" in
    aarch64|arm64|x86_64|amd64) ;;
    *) warn "非常见架构 $arch —— 请确认离线包镜像确实为本架构构建" ;;
  esac

  # ── 3. Docker 与 Compose ────────────────────────────────────────────────
  require_cmd docker
  docker info >/dev/null 2>&1 \
    || die "docker 守护进程不可达。请启动 docker（systemctl start docker）或修正当前用户权限。"
  ok "docker：$(docker --version 2>/dev/null)"

  # compose v2 子命令（本编排用了 `name:` 与 depends_on.condition，v1 不支持）
  if docker compose version >/dev/null 2>&1; then
    ok "docker compose：$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null | head -n1)"
  else
    die "缺少 docker compose v2 插件（脚本用 \`docker compose\` 子命令形式）。v1 的 docker-compose 不支持本编排。"
  fi

  # ── 4. 端口占用 ─────────────────────────────────────────────────────────
  # 先算出"本项目自己的容器占了哪些端口"，把"重跑自己"与"抢占别人"分开
  collect_own_ports
  [ -n "$CC_OWN_PORTS" ] && info "检测到本项目既有容器占用的端口（将按幂等方式重建）：$CC_OWN_PORTS"

  local p busy="" own=""
  for p in $CC_APP_PORTS; do
    if port_busy "$p"; then
      if is_own_port "$p"; then own="$own $p"; else busy="$busy $p"; fi
    fi
  done
  if [ -n "$busy" ]; then
    err "应用层端口已被**别的服务**占用：$busy"
    for p in $busy; do
      printf '        :%s  ' "$p"; port_owner "$p" | head -n 2
    done
    die "本项目**绝不抢占既有服务**。请先停掉占用者，或在 .env 里改端口（同时要改 compose）。"
  fi
  if [ -n "$own" ]; then
    ok "应用层端口 $own 由**本项目自己的**容器占用 ⇒ 视为幂等重跑，将继续（compose 会重建）"
  else
    ok "应用层端口空闲：$CC_APP_PORTS"
  fi

  # 载体端口：语义**随档位相反**，这是最容易搞错的一处
  local cbusy="" cown=""
  for p in $CC_CARRIER_PORTS; do
    if port_busy "$p"; then
      if is_own_port "$p"; then cown="$cown $p"; else cbusy="$cbusy $p"; fi
    fi
  done
  if [ "$mode" = "standalone" ]; then
    if [ -n "$cbusy" ]; then
      err "standalone 档需要自己拉起载体，但这些端口已被**别的服务**占用：$cbusy"
      case " $cbusy " in
        *" 7475 "*|*" 7688 "*)
          warn "  :7475/:7688 被占用通常说明本机已有 Neo4j。"
          warn "  targets.yaml#neo4j.isolation.must_not 明确禁止新建第二个 Neo4j 容器。" ;;
      esac
      case " $cbusy " in
        *" 7200 "*) warn "  :7200 被占用通常说明本机已有 GraphDB（既有 gdo* 仓库不得改写）。" ;;
      esac
      die "请改用 --mode attached 复用既有载体，或先释放这些端口。"
    fi
    if [ -n "$cown" ]; then
      ok "standalone 档载体端口 $cown 由**本项目自己的**载体容器占用 ⇒ 幂等重跑，继续"
    else
      ok "standalone 档载体端口空闲：$CC_CARRIER_PORTS"
    fi
  else
    if [ -z "$cbusy" ]; then
      warn "attached 档要求复用**既有** Neo4j/GraphDB，但 $CC_CARRIER_PORTS 全部空闲。"
      warn "  ⇒ 请确认载体在**另一台**机器（.env 里写对 IP），否则应用会连不上载体。"
      warn "  ⇒ 若本机就没有载体，应改用 --mode standalone。"
    else
      ok "attached 档检测到既有载体端口占用（符合预期）：$cbusy"
    fi
  fi

  # ── 5. 磁盘与内存 ───────────────────────────────────────────────────────
  local avail_kb avail_gb
  avail_kb="$(df -Pk /var/lib/docker 2>/dev/null || df -Pk / )" 
  avail_kb="$(printf '%s\n' "$avail_kb" | awk 'NR==2{print $4}')"
  if [ -n "$avail_kb" ]; then
    avail_gb=$((avail_kb / 1024 / 1024))
    if [ "$avail_gb" -lt "${CC_MIN_DISK_GB:-20}" ]; then
      err "docker 数据盘可用 ${avail_gb} GB < 要求的 ${CC_MIN_DISK_GB:-20} GB"
      die "空间不足会在 load 镜像中途失败并留下半截镜像。请先清理或扩容。"
    fi
    ok "docker 数据盘可用：${avail_gb} GB"
  else
    warn "未能读取磁盘可用空间（df 异常），请在 load 镜像前自行确认"
  fi

  local mem_mb=0
  if [ -r /proc/meminfo ]; then
    mem_mb="$(awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo)"
  fi
  if [ "$mem_mb" -gt 0 ]; then
    if [ "$mem_mb" -lt "${CC_MIN_MEM_MB:-7000}" ]; then
      warn "宿主内存仅 ${mem_mb} MB。6 个应用服务实测合计约 0.32 GiB（2026-10-16 真机读数，"
      warn "  按 2× 余量估 ~0.8 GB），standalone 档另需 ~6 GB（Neo4j+GraphDB）。"
      warn "  ⇒ 若内存吃紧，请用 attached 档，或先确认既有服务的余量。"
    else
      ok "宿主内存：${mem_mb} MB"
    fi
  fi

  # ── 6. .env 存在性与必填键 ──────────────────────────────────────────────
  if [ ! -f "$envf" ]; then
    blocked "找不到 $envf。请先：cp $ddir/.env.example $envf 并填写。"
  fi
  ok "配置文件：$envf"

  local k missing="" val
  # 所有档位都必需的键
  local common_keys="CC_IMAGE_TAG"
  # 各自档位的关键键
  local mode_keys
  if [ "$mode" = "standalone" ]; then
    mode_keys="CC_NEO4J_PASSWORD"
  else
    mode_keys="CC_NEO4J_HTTP_URL CC_GRAPHDB_URL"
  fi
  for k in $common_keys $mode_keys; do
    val="$(env_get "$envf" "$k" "")"
    [ -n "$val" ] || missing="$missing $k"
  done
  if [ -n "$missing" ]; then
    err "必填键为空：$missing"
    die "请在 $envf 中补齐后重试（键名与说明见 $ddir/.env.example）。"
  fi
  ok "必填键齐备（档位 $mode）"

  # 采集面配置：缺失不阻塞安装，但会让"装起来了却采不到数据" —— 大声告警
  for k in GAUSSDB_DSN GAUSSDB_NODES; do
    val="$(env_get "$envf" "$k" "")"
    if [ -z "$val" ]; then
      warn "$k 为空 ⇒ 采集层会**起来但采不到 GaussDB 事实**（探针层全空）。"
      warn "  这不是安装故障，但验收矩阵会据此判定数据面不达标。"
    fi
  done
  if [ -z "$(env_get "$envf" MCP_STORAGE_DM_BASE_URL "")" ]; then
    info "MCP_STORAGE_DM_BASE_URL 为空 ⇒ 存储层探针不产出事实（未配置即不采，属预期）"
  fi
  if [ -z "$(env_get "$envf" DIFY_BASE_URL "")" ]; then
    info "DIFY_BASE_URL 为空 ⇒ 编排层属外部依赖，由 lib/deps.sh 单独体检/安装"
  fi

  ok "① 环境预检完成"
}
