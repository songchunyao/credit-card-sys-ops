#!/usr/bin/env bash
# =============================================================================
# deploy/install.sh —— credit-card-sys-ops 一键部署（目标 Linux 服务器上运行）
#
# 一条命令完成：环境预检 → 载入镜像 → 外部依赖体检/补装 → 契约装载 →
#               起栈 → 等待健康 → 验收矩阵。
#
# 用法
# ----
#   # ① 复用既有 Neo4j/GraphDB（**192.0.2.10 现网必须用这档**）
#   sudo bash deploy/install.sh --mode attached
#
#   # ② 自带 Neo4j + GraphDB（任意新 Linux 服务器）
#   sudo bash deploy/install.sh --mode standalone
#
#   # ③ 从**全量**离线包安装（断网客户环境；载体与 Dify 都随包）
#   sudo bash install.sh --offline --bundle /opt/ccops/ccops-offline-<版本> --mode standalone
#
# 参数
# ----
#   --mode attached|standalone   拓扑档（默认读 .env 的 CC_DEPLOY_MODE，缺省 attached）
#   --deps check|install         外部依赖策略（默认读 CC_DEPS_MODE，缺省 install）
#   --bundle DIR                 离线包目录（含 images/*.tar.gz 与 VERSION）
#   --offline                    断网模式：只 `docker load`、绝不 pull；Dify 用包内 vendor/dify
#                                （配合 images/IMAGES.lock 逐条核 image Id）
#   --skip-verify                跳过验收矩阵（**不推荐**；仅用于排障中途）
#   --no-init                    跳过契约装载（TBox 已装过时用，需自行确认）
#   -h|--help                    显示帮助
#
# 退出码（沿用本项目四态语义）
#   0=PASS  1=FAIL（有检查项失败）  2=BLOCKED（环境不满足，不是代码缺陷）
#
# 设计纪律
#   · **绝不抢占既有服务**：端口占用即拒绝（含 neo4j/graphdb 载体端口）。
#   · **绝不静默通过**：缺凭据/缺依赖一律显式报出，区分 FAIL 与 BLOCKED。
#   · **幂等**：可反复执行；契约装载本身幂等，compose up 本身幂等。
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# shellcheck source=lib/common.sh
. "$HERE/lib/common.sh"
# shellcheck source=lib/preflight.sh
. "$HERE/lib/preflight.sh"
# shellcheck source=lib/deps.sh
. "$HERE/lib/deps.sh"
# shellcheck source=lib/offline.sh
. "$HERE/lib/offline.sh"

# ── 默认参数 ─────────────────────────────────────────────────────────────────
MODE=""
DEPS_ACTION=""
BUNDLE_DIR=""
SKIP_VERIFY=0
NO_INIT=0
OFFLINE=0

usage() { sed -n '2,37p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)      MODE="${2:-}"; shift 2 ;;
    --mode=*)    MODE="${1#*=}"; shift ;;
    --deps)      DEPS_ACTION="${2:-}"; shift 2 ;;
    --deps=*)    DEPS_ACTION="${1#*=}"; shift ;;
    --bundle)    BUNDLE_DIR="${2:-}"; shift 2 ;;
    --bundle=*)  BUNDLE_DIR="${1#*=}"; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    --no-init)   NO_INIT=1; shift ;;
    --offline)   OFFLINE=1; shift ;;
    -h|--help)   usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

# 兼容写法：CC_OFFLINE=1 等价于 --offline（install-offline.sh 会同时导出它）
if [ "${CC_OFFLINE:-0}" = "1" ]; then
  OFFLINE=1
fi

# ── .env 与档位决议 ──────────────────────────────────────────────────────────
ENVF="$HERE/.env"
# ★ 离线包**刻意不携带** deploy/.env（它含口令，且要让 MANIFEST 在安装后仍校验得通）。
#   故离线模式先按模板生成一份**空的**，再由 offline_prepare_env 填入离线默认值。
#   先前把"存在性检查"放在最前面 ⇒ 离线一键安装第一步就 blocked —— 这是必经路径，不能出错。
if [ ! -f "$ENVF" ] && [ "$OFFLINE" = "1" ]; then
  cp "$HERE/.env.example" "$ENVF" \
    || blocked "无法从 $HERE/.env.example 生成 deploy/.env（目录只读？）"
  chmod 600 "$ENVF" 2>/dev/null || true
  ENVF_GENERATED=1
  info "离线模式：已从 .env.example 生成 deploy/.env（随后会自动填充离线默认值）"
fi
if [ ! -f "$ENVF" ]; then
  blocked "找不到 $ENVF。请先执行：cp $HERE/.env.example $ENVF && vi $ENVF"
fi
if [ -n "$MODE" ]; then
  :
elif [ "$OFFLINE" = "1" ] && [ "${ENVF_GENERATED:-0}" = "1" ]; then
  # 刚由模板生成 ⇒ 模板里的 CC_DEPLOY_MODE=attached 不是用户的意图；
  # 离线包的目标机通常是全新服务器（无既有载体），缺省 standalone。
  MODE="standalone"
else
  MODE="$(env_get "$ENVF" CC_DEPLOY_MODE attached)"
fi
[ -n "$DEPS_ACTION" ] || DEPS_ACTION="$(env_get "$ENVF" CC_DEPS_MODE install)"
case "$MODE" in attached|standalone) ;; *) die "--mode 只能是 attached 或 standalone（收到 '$MODE'）" ;; esac
case "$DEPS_ACTION" in check|install) ;; *) die "--deps 只能是 check 或 install（收到 '$DEPS_ACTION'）" ;; esac

# ── 离线模式：**先**把 .env / Dify .env 准备成可起栈状态，再进预检 ───────────
#   ★ 顺序不能反：预检会断言 standalone 档的 CC_NEO4J_PASSWORD 非空，而离线包的
#     口令是"首次安装时生成"的；若先生成后预检，预检那一关永远过不了。
#   ★ offline_prepare_env 只填空值，部署者已填的 GaussDB/SSH 等一律不动。
if [ "$OFFLINE" = "1" ]; then
  export CC_OFFLINE=1
  [ -n "$BUNDLE_DIR" ] || BUNDLE_DIR="$REPO_ROOT"
  # 离线根目录：`--bundle` 指到别处时以它为准（VERSION 在那里），否则就是本包根。
  CC_OFFLINE_ROOT="$REPO_ROOT"
  if [ -n "$BUNDLE_DIR" ] && [ -f "$BUNDLE_DIR/VERSION" ]; then
    CC_OFFLINE_ROOT="$(cd "$BUNDLE_DIR" && pwd)"
  fi
  export CC_OFFLINE_ROOT
  # ★ 显式传入的 CC_SKIP_DIFY=1（来自 install-offline.sh --no-dify）**必须**胜过
  #   .env 里的值：刚生成的 .env 来自模板（CC_SKIP_DIFY=0），否则 --no-dify 会被静默吃掉。
  if [ "${CC_SKIP_DIFY:-}" != "1" ]; then
    CC_SKIP_DIFY="$(env_get "$ENVF" CC_SKIP_DIFY 0)"
  fi
  export CC_SKIP_DIFY
  offline_prepare_env "$ENVF" "$MODE" "$CC_OFFLINE_ROOT"
fi

IMAGE_TAG="$(env_get "$ENVF" CC_IMAGE_TAG latest)"
[ -n "$IMAGE_TAG" ] || IMAGE_TAG=latest

# compose 文件组合（**档位切换必须响亮**：少给文件会立刻报错，不会被静默跳过）
COMPOSE_ARGS=(-f "$HERE/compose.yml")
if [ "$MODE" = "standalone" ]; then
  COMPOSE_ARGS+=(-f "$HERE/compose.standalone.yml")
fi
compose() { docker compose "${COMPOSE_ARGS[@]}" "$@"; }

CC_SERVICES="collect kg onto kgt kgt-dyn live-facts"
# ★ 镜像清单与容器清单一**必须**分开（2026-10-12 真机复核 F4 时踩到）：
#   kgt-dyn 是**第二个 kgt 实例**（同一镜像、另一条臂、另一端口），并不存在
#   `cc-ops/kgt-dyn:<tag>` 这个镜像。把 6 个服务名直接塞进镜像断言循环 ⇒
#   "缺少镜像： cc-ops/kgt-dyn:e2e-main-e8c3fdd ⇒ 缺少必需镜像，拒绝继续"。
#   所以：CC_IMAGES = 离线包里的 5 个镜像（② 载入并断言）；
#         CC_SERVICES = 起栈后的 6 个容器（⑥ 健康门限）。
CC_IMAGES="collect kg onto kgt live-facts"

printf '%s\n' "$C_BLD"
cat <<'BANNER'
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  credit-card-sys-ops · 信用卡跨层运维智能体 · 容器化一键部署          ║
  ║  应用层 6 服务：collect(8187) kg(8301) onto(8302) kgt(8303)           ║
  ║                live-facts(8304) kgt-dyn(8305 补充臂)                  ║
  ╚══════════════════════════════════════════════════════════════════════╝
BANNER
printf '%s\n' "$C_RST"
info "部署目录：$HERE"
info "档位    ：$MODE（$([ "$MODE" = standalone ] && echo '自带 Neo4j+GraphDB' || echo '复用既有 Neo4j+GraphDB')）"
info "依赖策略：$DEPS_ACTION"
info "镜像标签：$IMAGE_TAG"
if [ "$OFFLINE" = "1" ]; then
  info "离线模式：开启（只 docker load，绝不 pull；Dify 用 $(env_get "$ENVF" CC_DIFY_DIR "$REPO_ROOT/vendor/dify")）"
fi

# ─────────────────────────────────────────────────────────────────────────────
# ② 载入应用镜像
# ─────────────────────────────────────────────────────────────────────────────
load_images() {
  hdr "② 载入应用镜像（5 个）"
  if [ "$OFFLINE" = "1" ]; then
    info "离线模式：本次会载入 images/ 下**全部** tar.gz（应用 + 载体 + Dify），且不会 pull"
  fi

  # 自动识别"就地离线包"：release 包根 = 仓库相对布局 ⇒ images/ 与 deploy/ 同级。
  # 这样从包里执行 `bash deploy/install.sh` 时**不需要**再手写 --bundle。
  if [ -z "$BUNDLE_DIR" ] && [ -d "$REPO_ROOT/images" ] \
     && [ -n "$(find "$REPO_ROOT/images" -maxdepth 1 -name '*.tar.gz' 2>/dev/null | head -n1)" ]; then
    BUNDLE_DIR="$REPO_ROOT"
    info "检测到就地离线包（$REPO_ROOT/images/*.tar.gz）⇒ 自动按 --bundle 处理"
  fi

  local tars=() t
  if [ -n "$BUNDLE_DIR" ]; then
    [ -d "$BUNDLE_DIR" ] || die "--bundle 指定的目录不存在：$BUNDLE_DIR"
    # 离线包里的 tag 是权威（避免"装的是旧镜像"这类静默错配）
    if [ -f "$BUNDLE_DIR/VERSION" ]; then
      local btag; btag="$(env_get "$BUNDLE_DIR/VERSION" CC_IMAGE_TAG '')"
      [ -n "$btag" ] && IMAGE_TAG="$btag"
      info "离线包 VERSION 声明的镜像标签：$IMAGE_TAG"
    fi
    local d
    for d in "$BUNDLE_DIR/images"; do
      [ -d "$d" ] || die "离线包里缺 images/ 目录：$d"
      while IFS= read -r t; do tars+=("$t"); done < <(find "$d" -maxdepth 1 -name '*.tar.gz' | sort)
    done
    [ "${#tars[@]}" -gt 0 ] || die "离线包 images/ 下没有 *.tar.gz"
  fi

  # ── 幂等重跑的优化：全部镜像已在本地且与锁一致 ⇒ 跳过 8 GB 的重载 ──────
  #   ★ 判据是 image Id（内容摘要），不是 tag；不一致就照常重载（load 会覆盖）。
  if [ "$OFFLINE" = "1" ] && [ -n "$BUNDLE_DIR" ] && [ "${#tars[@]}" -gt 0 ] \
     && offline_images_match "$BUNDLE_DIR"; then
    info "全部镜像已在本机且与 IMAGES.lock 一致 ⇒ 跳过 docker load（幂等重跑）"
    tars=()
  fi

  if [ "${#tars[@]}" -gt 0 ]; then
    # 先校验再载入（校验不过即停，不把半截镜像推进本地库）
    if [ -f "$BUNDLE_DIR/MANIFEST.sha256" ]; then
      info "校验离线包 sha256（$BUNDLE_DIR/MANIFEST.sha256）"
      local sumf="$BUNDLE_DIR/MANIFEST.sha256"
      if ( cd "$BUNDLE_DIR" && sha256sum -c "$(basename "$sumf")" >/dev/null 2>&1 ); then
        ok "离线包校验通过"
      elif ( cd "$BUNDLE_DIR" && sha256sum -c "$sumf" >/dev/null 2>&1 ); then
        ok "离线包校验通过（绝对路径清单）"
      else
        die "离线包 sha256 校验失败 —— 传输损坏或被篡改。**拒绝载入**。请重新拷贝该包。"
      fi
    else
      warn "离线包里没有 MANIFEST.sha256 ⇒ 无法证明镜像未损坏（建议用 make_bundle.sh 重新打包）"
    fi

    local t
    for t in "${tars[@]}"; do
      info "载入：$(basename "$t")"
      gzip -dc "$t" | docker load || die "docker load 失败：$t"
    done
    ok "镜像载入完成（${#tars[@]} 个 tar.gz）"
  else
    if [ "$OFFLINE" != "1" ]; then
      info "未指定 --bundle ⇒ 假定镜像已在本地（由 CI 构建或先前已 load）"
    fi
  fi

  # ── 断言 5 个镜像都在，且标签与预期一致（按 CC_IMAGES：kgt-dyn 无独立镜像）──
  local svc img missing=""
  for svc in $CC_IMAGES; do
    img="cc-ops/${svc}:${IMAGE_TAG}"
    if docker image inspect "$img" >/dev/null 2>&1; then
      ok "镜像就绪：$img"
    else
      missing="$missing $img"
    fi
  done
  if [ -n "$missing" ]; then
    err "缺少镜像：$missing"
    err "  → 本机是开发机（x86_64、无 Docker）时**无法**本地构建；"
    err "    镜像必须在与目标同架构的机器上构建（本项目目标机与 CI 均为 aarch64）。"
    err "  → 正确做法：在 CI/ARM 机构建后用 deploy/make_bundle.sh 打离线包，"
    err "    再在本机用 install.sh --bundle <离线包> 安装。"
    die "缺少必需镜像，拒绝继续（绝不用旧镜像冒充）。"
  fi

  # ── 离线：按 IMAGES.lock 逐条核 image Id / 架构 ──────────────────────────
  # ★ 这一步覆盖**载体与 Dify**：只核 5 个应用镜像是不够的 —— 断网时 Neo4j/GraphDB/Dify
  #   一旦缺一个，失败会延迟到 ③.5 或 ⑧，而那时的报错指向"载体起不来"。
  if [ "$OFFLINE" = "1" ]; then
    offline_assert_images "$BUNDLE_DIR"
  fi

  # ── 把权威标签落回 .env，并 export 给本次 compose ───────────────────────
  # ★ 不写回的话：compose 从 .env 读 ${CC_IMAGE_TAG:-latest}，会按**旧标签**
  #   去找镜像 ⇒ "image not found"。这个失败看起来像"镜像没载入"，
  #   实际是"标签口径不一致"，属于最难查的那类。
  local cur; cur="$(env_get "$ENVF" CC_IMAGE_TAG '')"
  if [ "$cur" != "$IMAGE_TAG" ]; then
    info ".env 的 CC_IMAGE_TAG='$cur' ≠ 权威标签 '$IMAGE_TAG' ⇒ 写回 .env"
    env_set "$ENVF" CC_IMAGE_TAG "$IMAGE_TAG" \
      || die "写不回 $ENVF 的 CC_IMAGE_TAG（文件只读？）—— 拒绝用不一致的标签继续"
    ok ".env 已更新：CC_IMAGE_TAG=$IMAGE_TAG"
  fi
  export CC_IMAGE_TAG="$IMAGE_TAG"   # 双保险：export 胜过 .env 文件
}

# ─────────────────────────────────────────────────────────────────────────────
# ④ 契约装载（GraphDB 建仓 + TBox；Neo4j TBox）
#    ★ 在容器里跑，避免依赖宿主 Python 环境；用的是**同一批镜像**，环境自洽。
# ─────────────────────────────────────────────────────────────────────────────
gen_targets_overlay() {
  # scheme2-onto/tools/load_tbox.py 的 GraphDB 地址**只**来自 --targets 文件
  # （它不读 CC_GRAPHDB_URL）⇒ 这里按"容器内可达地址"生成一份叠加契约。
  # 做法：复制真契约，只**逐条改地址字面量**，其余逐字保留 —— 并打印改动清单
  # （登记式改写：不静默篡改；改了什么一望而知）。
  local src="$REPO_ROOT/shared/deploy/targets.yaml"
  local out="$HERE/.work/targets.deploy.yaml"
  [ -f "$src" ] || die "找不到契约文件：$src（离线包必须带完整源码树）"
  mkdir -p "$HERE/.work"

  local g_url n_http n_bolt
  if [ "$MODE" = "standalone" ]; then
    g_url="http://graphdb:7200"; n_http="http://neo4j:7474"; n_bolt="bolt://neo4j:7687"
  else
    g_url="$(env_get "$ENVF" CC_GRAPHDB_URL http://host.docker.internal:7200)"
    n_http="$(env_get "$ENVF" CC_NEO4J_HTTP_URL http://host.docker.internal:7475)"
    n_bolt="$(env_get "$ENVF" CC_NEO4J_URI bolt://host.docker.internal:7688)"
  fi

  info "生成 targets 叠加契约（仅改写载体地址）："
  info "  graphdb.url      : $g_url"
  info "  neo4j.http_uri   : $n_http"
  info "  neo4j.bolt_uri   : $n_bolt"

  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
  #   契约(shared/deploy/targets.yaml)里的载体地址默认值已由 192.0.2.10 改为 127.0.0.1
  #   ⇒ 这里的 sed 必须匹配「任意主机 + 契约端口」两种写法（新默认 127.0.0.1 与历史写法），
  #     否则叠加契约会静默留着本机地址，容器里去连自己。
  sed -e "s#http://[^\"/]*:7200#${g_url}#g" \
      -e "s#http://[^\"/]*:7475#${n_http}#g" \
      -e "s#bolt://[^\"/]*:7688#${n_bolt}#g" \
      "$src" > "$out"

  # 断言：两处默认来源（新默认 127.0.0.1 / 历史 192.0.2.10）都已不残留在契约端口上
  local left
  left="$(grep -cE '10\.226\.81\.84:(7200|7475|7688)|127\.0\.0\.1:(7200|7475|7688)' "$out" || true)"
  if [ "${left:-0}" -gt 0 ]; then
    err "还有 $left 处载体地址未被改写（契约写法可能已变）—— 拒绝用这份叠加契约"
    die "请检查 $src 里载体地址的写法，并同步更新 deploy/install.sh 的 gen_targets_overlay()。"
  fi

  # ── standalone 档：把"需与既有仓库共存"的声明改成与事实一致 ──
  # ★ 契约里的 `coexisting_repositories: [gdo, gdo-bad, gdo-cq, gdo-test, starwars]`
  #   是给**共享载体**写的保护性声明（防误碰别人的仓库）。
  #   在 standalone 档，载体是我们自己用官方镜像新建的 —— **没有**既有仓库，
  #   所以"应与它们共存"这条断言没有对象。保留它会让装载器去核一堆不存在的仓库。
  #   ⇒ 置空，这不是"放宽校验"，而是**把契约改写成与当时事实一致**。
  #   （实测依据：fresh GraphDB 上 loading 因 repo_list() 为空而 Fail，
  #     报错还把人引向"不可达/权限"，方向全错。）
  if [ "$MODE" = "standalone" ]; then
    sed -i -E 's#^([[:space:]]*coexisting_repositories:).*$#\1 []#' "$out"
    info "  已置空 coexisting_repositories（standalone：载体为新建，无既有仓库需保护）"
  fi
  ok "叠加契约已生成：$out"
}

# ─────────────────────────────────────────────────────────────────────────────
# ③.5 先起载体（**仅 standalone 档**）
#   ★ 顺序是硬要求：契约装载（④）要连 GraphDB/Neo4j 写 TBox，而 standalone 档的
#     两个载体本身就是 compose 服务 —— 若不先把它们起来，`compose run --no-deps`
#     会在"载体不存在"的状态下跑，把**顺序问题**误报成**装载失败**。
#   ★ attached 档载体是外部的，此处不做任何动作（预检已核对端口占用符合预期）。
# ─────────────────────────────────────────────────────────────────────────────
bring_up_carriers() {
  [ "$MODE" = "standalone" ] || return 0
  hdr "③.5 启动自带载体（standalone 档）"
  # ★ `--pull missing` 而非 `--pull never` —— 这是**刻意的**、两档语义不同：
  #   · 应用层 5 个镜像：`--pull never`。它们必须来自本项目构建/离线包，
  #     绝不允许在起栈时被悄悄换成 registry 上的另一个 tag（"验证过的"≠"跑起来的"）。
  #   · 载体（neo4j/graphdb）：standalone 档的**定义**就是"用官方镜像把它们装起来"
  #     ⇒ 首次安装时**必须允许拉取**。
  #   实测踩过：一开始这里也写 `--pull never`，于是在没预拉镜像的机器上
  #     `Error response from daemon: No such image: ontotext/graphdb:10.8.0`
  #     —— 把"该拉没拉"报成了"载体起不来"。
  #   ★ 离线模式（--offline）：载体镜像已在 ② 从离线包载入并由 IMAGES.lock 核过身份，
  #     此时**必须**改回 `--pull never` —— 目标机断网，pull 只会卡在超时上。
  local pullflag="--pull missing"
  if [ "$OFFLINE" = "1" ]; then
    pullflag="--pull never"
  fi
  if ! compose up -d $pullflag neo4j graphdb; then
    err "载体启动失败。诊断：docker compose ${COMPOSE_ARGS[*]} logs --tail=60 neo4j graphdb"
    if [ "$OFFLINE" = "1" ]; then
      err "  → 离线模式下缺失镜像意味着包不完整；**不要**在目标机 docker pull。"
      err "    回构建机用 deploy/offline/make_offline_bundle.sh 重新出包。"
    else
      err "  → 若报 No such image，说明官方镜像拉取失败（网络/registry 不可达）。"
      err "    可先单独验证：docker pull ${CC_NEO4J_IMAGE_DEFAULT} && docker pull ${CC_GRAPHDB_IMAGE_DEFAULT}"
    fi
    blocked "载体起不来 ⇒ 契约装载与两个方案都无法进行。"
  fi
  ok "载体容器已启动"

  # 宿主侧探活用发布到宿主的契约端口。
  # ★★ standalone 档**必须看本机**（127.0.0.1），不能沿用 .env 里的载体地址 ★★
  #   实测踩过：.env 里写的是另一台机器的 `http://192.0.2.10:7475`（attached 档遗留），
  #   而 standalone 档的载体其实在**本机**。旧实现在这里探到了 192.0.2.10，
  #   于是打印「Neo4j 已就绪（0s）」——**本机那个其实正在 crash-loop**。
  #   后果是契约装载（④）紧接着去连本机容器，得到 Connection refused，
  #   把「载体没起来」误报成「装载器有毛病」。
  #   ⇒ standalone 档一律用本机契约端口，与 .env 写什么无关。
  local n_url g_url
  n_url="http://127.0.0.1:7475"
  g_url="http://127.0.0.1:7200"

  wait_http "${n_url}/" 240 "Neo4j（本机 $n_url）" || {
    err "Neo4j 未在期限内就绪。诊断：docker compose ${COMPOSE_ARGS[*]} logs --tail=60 neo4j"
    err "  → 常见原因：口令变量撞了 NEO4J_ 前缀被 Neo4j 当配置项（见 compose.standalone.yml 注释）"
    blocked "Neo4j 未就绪"
  }
  wait_http "${g_url}/rest/repositories" 300 "GraphDB（本机 $g_url）" || {
    err "GraphDB 未在期限内就绪。诊断：docker compose ${COMPOSE_ARGS[*]} logs --tail=60 graphdb"
    blocked "GraphDB 未就绪"
  }
  ok "③.5 自带载体就绪（均已在本机契约端口响应）"
}

init_contracts() {
  hdr "④ 契约装载（幂等；重复执行不改变计数）"
  if [ "$NO_INIT" = 1 ]; then
    skip "按 --no-init 跳过契约装载"
    return 0
  fi

  gen_targets_overlay

  # ★ 叠加契约路径必须**在这里先声明**：④.0b（快照投影）与 ④.1（TBox 装载）都用它。
  #   实测踩过：④.0b 引用了在 ④.1 才 `local overlay=` 的变量 ⇒ 此处展开为空串 ⇒
  #   `-v "$overlay:/app/..."` 变成 `-v ":/app/..."`（非法挂载）⇒ 投影失败，
  #   而报错只说"投影失败"，看不出是变量顺序问题。shell 不会替你做未声明检查。
  local overlay="$HERE/.work/targets.deploy.yaml"

  local n_http_container g_url_container pw
  if [ "$MODE" = "standalone" ]; then
    n_http_container="http://neo4j:7474"; g_url_container="http://graphdb:7200"
  else
    n_http_container="$(env_get "$ENVF" CC_NEO4J_HTTP_URL http://host.docker.internal:7475)"
    g_url_container="$(env_get "$ENVF" CC_GRAPHDB_URL http://host.docker.internal:7200)"
  fi
  pw="$(env_get "$ENVF" CC_NEO4J_PASSWORD '')"

  # ── ④.0 确保 GraphDB 上存在项目仓库（**fresh 载体的必要前置**）──
  # ★ 首次真跑 standalone 才暴露的缺口：
  #   `scheme2-onto/tools/load_tbox.py::persist` **假定目标仓库已存在**
  #   （load_tbox.py:371-372），而它的 `repo_list()`（:267-280）在仓库列表为空时
  #   直接 `raise Fail("仓库列表为空（应含 coexisting_repositories）")`。
  #   那些断言是给**共享载体**（192.0.2.10 上已有 gdo*/starwars）写的；
  #   在"自己用官方镜像起的全新 GraphDB"上，空列表是**正常状态**，不是故障。
  #   ⇒ 装载前先建仓。这本来就是"完成配置"的必要一步。
  if [ "$MODE" = "standalone" ]; then
    info "④.0 确保 GraphDB 仓库存在（deploy/create_graphdb_repo.py）"
    # 宿主侧直接打 —— standalone 档 GraphDB 已发布在 127.0.0.1:7200
    if python3 "$HERE/create_graphdb_repo.py" --base "http://127.0.0.1:7200" \
         --repository "$(env_get "$ENVF" CC_GRAPHDB_REPOSITORY cc-ops)" 2>&1 | sed 's/^/      /'; then
      ok "GraphDB 仓库就绪"
    else
      err "GraphDB 仓库创建失败 ⇒ TBox 装载必然失败（装载器要求仓库先存在）"
      err "  → 诊断：curl -sS http://127.0.0.1:7200/rest/repositories"
    fi
  fi

  # ── ④.0b 投影一份快照图（**fresh 载体的第二个必要前置**）──
  # ★ 首次真跑 standalone 才暴露的顺序缺口。GraphDB 侧的正确顺序是：
  #     ① 建仓 → ② **投影一份 CCF 快照（生成"快照命名图"）** → ③ 装 TBox → ④ 起 onto
  #   少了 ② 会有两个**看起来毫不相干**的失败：
  #     · `load_tbox.py persist` 拒绝写入：
  #         「没有实测到任何快照命名图，基线不符合预期，拒绝写入」
  #       —— 它的基线断言是给**已投影过**的共享载体写的（保护既有快照图不被覆盖）。
  #     · `cc_tools_onto.py:1890` 启动即崩：
  #         GraphError: 未能定位可用的快照命名图。
  #       —— ONTO 服务**按快照命名图作用域**，fresh 载体上没有快照图就起不来，
  #          表现为 onto 容器 crash-loop，而报错里一个字都没提"你还没投影"。
  #   ⇒ 这里补上投影。快照用镜像里烘好的 `tmp/ccf_live5.json`（与 live-facts 同源）。
  #     投影按 snapshotId 幂等：同一份快照重复投影落到同一个命名图，不会堆积。
  if [ "$MODE" = "standalone" ]; then
    local snap="$REPO_ROOT/tmp/ccf_live5.json"
    # ★ 宿主上没有就回退到**镜像里烘好的那份**（Dockerfile.live-facts:70 已把它 COPY 进
    #   /app/tmp/ccf_live5.json）。上面这段注释一直写着"快照用镜像里烘好的"，代码却只认
    #   宿主路径 ⇒ 离线包里没有 tmp/ 时投影被静默跳过（总账里只留一行 warn，
    #   看图看不出 ONTO 会因此起不来）。两条路都堵死才算真的缺件。
    if [ ! -s "$snap" ]; then
      local img_snap="$HERE/.work/ccf_live5.json"
      if compose run --rm --no-deps -T live-facts \
           cat /app/tmp/ccf_live5.json > "$img_snap" 2>/dev/null && [ -s "$img_snap" ]; then
        info "  基底快照取自 live-facts 镜像：/app/tmp/ccf_live5.json → $img_snap"
        snap="$img_snap"
      else
        rm -f "$img_snap" 2>/dev/null || true
      fi
    fi
    if [ ! -s "$snap" ] || [ "$(head -c 1 "$snap" 2>/dev/null)" != "{" ]; then
      warn "  找不到可用的基底快照（试过 $REPO_ROOT/tmp/ccf_live5.json 与 live-facts 镜像）⇒ 跳过投影；ONTO 将因无快照命名图而不可用"
    else
      info "④.0b 投影快照（p2_graphdb.py，生成快照命名图；按 snapshotId 幂等）"
      if compose run --rm --no-deps -T \
           -v "$snap:/snap/ccf_live5.json:ro" \
           -v "$overlay:/app/shared/deploy/targets.deploy.yaml:ro" \
           onto python scheme2-onto/project/p2_graphdb.py \
                --ccf /snap/ccf_live5.json \
                --url "http://graphdb:7200" \
                --repository "$(env_get "$ENVF" CC_GRAPHDB_REPOSITORY cc-ops)" \
                --targets /app/shared/deploy/targets.deploy.yaml 2>&1 | tail -n 6 | sed 's/^/      /'; then
        ok "快照投影完成"
      else
        err "快照投影失败 ⇒ TBox 装载与 ONTO 都会失败（见上方输出）"
      fi
    fi
  fi

  # ── ④.1 GraphDB：建仓（若不存在）+ 装 TBox ──
  # ★ 必须把叠加契约**挂进容器**并显式 --targets 指向它。
  #   先前版本只生成却仍传 /app/shared/deploy/targets.yaml（原契约，写着 192.0.2.10）
  #   ⇒ standalone 档会在容器里去连一台不存在的 192.0.2.10，失败原因还指向"装载器有毛病"。
  local overlay="$HERE/.work/targets.deploy.yaml"
  info "④.1 GraphDB 建仓与 TBox 装载（scheme2-onto/tools/load_tbox.py persist）"
  info "     契约：容器内 /app/shared/deploy/targets.deploy.yaml（由 $overlay 挂入）"
  if compose run --rm --no-deps -T \
        -v "$overlay:/app/shared/deploy/targets.deploy.yaml:ro" \
        -e "CC_GRAPHDB_URL=$g_url_container" \
        onto python scheme2-onto/tools/load_tbox.py persist \
        --targets /app/shared/deploy/targets.deploy.yaml 2>&1 | sed 's/^/      /'; then
    ok "GraphDB TBox 装载完成"
  else
    err "GraphDB TBox 装载失败（见上方输出）"
    err "  → 若 GraphDB 未启用认证，这通常是**不可达**而非权限问题；先确认载体地址。"
  fi

  # ── ④.2 Neo4j：TBox 装载 ──
  info "④.2 Neo4j TBox 装载（scheme1-kg/tools/load_tbox.py persist）"
  if [ -z "$pw" ]; then
    # 与 Neo4j 侧 loader 的 fail-closed 语义一致：没有口令就不猜
    warn "CC_NEO4J_PASSWORD 为空 ⇒ 跳过 Neo4j TBox 装载"
    warn "  （该 loader 本身 fail-closed：绝不回落内置默认口令。attached 档请填口令或提供 CC_NEO4J_ENV_FILE）"
    skip "Neo4j TBox 装载（缺口令）"
  elif compose run --rm --no-deps -T \
        -e "CC_NEO4J_HTTP=$n_http_container" \
        -e "CC_NEO4J_USER=$(env_get "$ENVF" CC_NEO4J_USER neo4j)" \
        -e "CC_NEO4J_DB=$(env_get "$ENVF" CC_NEO4J_DATABASE neo4j)" \
        -e "CC_NEO4J_PASSWORD=$pw" \
        kg python scheme1-kg/tools/load_tbox.py persist 2>&1 | sed 's/^/      /'; then
    ok "Neo4j TBox 装载完成"
  else
    err "Neo4j TBox 装载失败（见上方输出）"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# ⑤ 起栈 + 等健康
# ─────────────────────────────────────────────────────────────────────────────
compose_up() {
  hdr "⑤ 起栈（docker compose up -d --pull never）"
  # --pull never：本项目的交付路径是"离线镜像 + load"，**不允许**在起栈时悄悄拉新镜像
  #   （否则"验证过的镜像"与"实际跑的镜像"可能不是同一物）。
  #   离线模式下这条更是硬要求：目标机断网，pull 会卡到超时。
  if compose up -d --pull never; then
    ok "compose up 完成"
  else
    err "compose up 失败。诊断：docker compose ${COMPOSE_ARGS[*]} logs --tail=80"
    return 1
  fi
  info "容器状态："
  compose ps | sed 's/^/      /'
}

wait_health() {
  hdr "⑥ 等待服务健康"
  # 本轮**只等我们自己的 6 个服务**（含补充臂 kgt-dyn）；Dify 是外部依赖，不在此处 gate
  local deadline=$(( $(date +%s) + ${CC_HEALTH_TIMEOUT_S:-180} ))
  local pending="$CC_SERVICES" svc still
  while [ "$(date +%s)" -lt "$deadline" ]; do
    still=""
    for svc in $pending; do
      local cid state
      cid="$(compose ps -q "$svc" 2>/dev/null)"
      if [ -z "$cid" ]; then still="$still $svc"; continue; fi
      state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null)"
      case "$state" in
        healthy|running) ;;
        *) still="$still $svc" ;;
      esac
    done
    pending="${still# }"
    [ -z "$pending" ] && { ok "全部应用服务已健康：$CC_SERVICES"; return 0; }
    sleep 3
  done
  err "超时（${CC_HEALTH_TIMEOUT_S:-180}s）仍未健康的服务：$pending"
  local svc
  for svc in $pending; do
    err "── $svc 日志尾部 ──"
    compose logs --tail=40 "$svc" 2>&1 | sed 's/^/      /'
  done
  return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# ⑦ 验收矩阵
# ─────────────────────────────────────────────────────────────────────────────
run_verify() {
  hdr "⑦ 验收矩阵"
  if [ "$SKIP_VERIFY" = 1 ]; then
    skip "按 --skip-verify 跳过验收（**该部署不得视为已验收**）"
    return 0
  fi
  local py=""
  if command -v python3 >/dev/null 2>&1; then py=python3
  elif command -v python >/dev/null 2>&1; then py=python
  fi
  if [ -z "$py" ]; then
    warn "宿主上没有 python3 ⇒ 无法跑验收矩阵"
    warn "  可在任意有 Python 的机器上跑：python3 $HERE/verify_deploy.py --base <目标IP>"
    return 1
  fi
  # ★ 四态语义要传下去：verify_deploy.py 的退出码 0=PASS / 1=FAIL / 2=BLOCKED。
  #   早先这里把所有非 0 都当"不达标"，于是「只有 BLOCKED、FAIL=0」的部署
  #   也被记成 [失败]，总账上出现"失败 2"的**假失败**——而矩阵明明写着 FAIL=0。
  #   实测踩过：本次 standalone 联调就是 `PASS=17 FAIL=0 BLOCKED=1`，
  #   却因为这一行被计入失败数。
  #   ⇒ 区分开：FAIL 才是失败；BLOCKED 记"受阻（未测到）"。
  "$py" "$HERE/verify_deploy.py" --deploy-dir "$HERE" --mode "$MODE"
  local vrc=$?
  case "$vrc" in
    0) ok "验收矩阵全绿（PASS）"; return 0 ;;
    2) skip "验收矩阵有未测到项（BLOCKED）—— 不是失败，但**该部署不得视为已验收**"
       warn "  逐项读数见上方输出与 $HERE/.work/verify.json"
       return 0 ;;
    *) err "验收矩阵存在**不达标项**（FAIL，见上方逐项读数与 JSON 结果）"
       return 1 ;;
  esac
}

# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
need_root_or_docker_group

preflight_run "$HERE" "$MODE"            # ①
load_images                              # ②
deps_run "$HERE" "$MODE" "$DEPS_ACTION" pre   # ③（前置：只体检；载体按档位处理）

if deps_carrier_missing; then
  err "知识载体（Neo4j/GraphDB）不可用 ⇒ 两个方案都跑不起来。"
  err "  出路：① 用 --mode standalone 让本脚本自带载体；"
  err "        ② 修正 .env 里的载体地址指向真实存在的实例；"
  err "        ③ 加 --deps install 让本脚本用官方镜像补装（断网环境改用 --offline，载体镜像随包载入）。"
  blocked "缺知识载体，拒绝继续（起栈也无法诊断）。"
fi

bring_up_carriers                       # ③.5（仅 standalone 档）
init_contracts                           # ④
compose_up || true                       # ⑤
wait_health || true                      # ⑥
run_verify || true                       # ⑦
deps_run "$HERE" "$MODE" "$DEPS_ACTION" post  # ⑧（后置：补装 Dify，可选且重量级）

# ── 总账 ─────────────────────────────────────────────────────────────────────
hdr "部署总账"
printf '  通过：%s%d%s　失败：%s%d%s　跳过：%s%d%s\n' \
  "$C_GRN" "$CC_STEP_OK" "$C_RST" \
  "$C_RED" "$CC_STEP_FAIL" "$C_RST" \
  "$C_YEL" "$CC_STEP_SKIP" "$C_RST"

cat <<EOF

  服务入口（假定 CC_BIND_IP=$(env_get "$ENVF" CC_BIND_IP 0.0.0.0)）：
    :8187  采集层数据面        curl -s http://127.0.0.1:8187/health
    :8301  方案1 KG-Q          curl -s http://127.0.0.1:8301/health
    :8302  方案2 ONTO-Q        curl -s http://127.0.0.1:8302/health
    :8303  方案3 KG-T          curl -s http://127.0.0.1:8303/health
    :8304  中性取数 read_facts curl -s http://127.0.0.1:8304/health
    :8305  方案3 补充臂      curl -s http://127.0.0.1:8305/health   # KG-T-dyn，health.supplementary=true，不进主判据

  常用运维命令：
    docker compose $(printf '%s ' "${COMPOSE_ARGS[@]}")logs -f kg
    docker compose $(printf '%s ' "${COMPOSE_ARGS[@]}")restart live-facts
    docker compose $(printf '%s ' "${COMPOSE_ARGS[@]}")down          # 停止（保留卷）
    docker compose $(printf '%s ' "${COMPOSE_ARGS[@]}")down -v       # 连卷一起删（载体数据也会没）

EOF

# 编排层（Dify）：**不阻塞**应用层交付，但也不能静默不提
if [ "${CC_SKIP_DIFY:-0}" = "1" ]; then
  printf '  编排层 Dify          按 --no-dify 跳过（复用既有实例）\n'
else
  DIFY_URL="$(env_get "$ENVF" DIFY_BASE_URL '')"
  if [ -n "$DIFY_URL" ]; then
    printf '  编排层 Dify          %s（控制台；管理员见 deploy/.env）\n' "$DIFY_URL"
  else
    printf '  编排层 Dify          未启用或未就绪（BLOCKED；不影响应用层）\n'
  fi
fi
printf '\n'

if [ "$CC_STEP_FAIL" -gt 0 ]; then
  err "部署未完全通过（$CC_STEP_FAIL 项失败）。**不得**据此次结果宣称部署成功。"
  exit 1
fi
ok "部署完成。"
