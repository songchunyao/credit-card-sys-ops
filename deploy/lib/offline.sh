#!/usr/bin/env bash
# =============================================================================
# deploy/lib/offline.sh —— 离线（断网）部署的公共层
#
# 谁调用它
# --------
#   · deploy/install.sh 在 `--offline` 时 source 本文件，并在四个位置调用：
#       offline_prepare_env   ← 预检之前（把 .env / Dify .env 准备成可起栈的状态）
#       offline_preflight     ← 预检开头（包完整性、架构、磁盘；**任何联网动作之前**）
#       offline_assert_images ← 载入镜像之后（按 IMAGES.lock 逐条核 image Id）
#       （deps.sh 另用 CC_OFFLINE 关闭一切 pull/git clone）
#   · deploy/offline/install_offline.sh 也复用这里的函数。
#
# 设计纪律（每条都对应一个真实的"装到一半才发现"）
# ------------------------------------------------
#   1. **离线模式只允许 `docker load`，绝不允许 `docker pull`**：
#      目标机断网时 pull 会卡在超时上（几分钟到十几分钟），而症状看起来像
#      "compose 起不来"，排查方向完全错。故载体/应用/Dify 三处起栈一律 `--pull never`。
#   2. **镜像按 image Id 锁定，不按 tag**：
#      tag 是可被 retag 的；同一个 `neo4j:5-community` 在不同机器上可以是不同镜像。
#      包内 `images/IMAGES.lock` 记录 `ref|sha256:imageId|arch`，装载后逐条比对，
#      **不一致即拒绝继续**（"验证过的≠跑起来的"这条纪律在离线场景同样成立）。
#   3. **包里不落任何真实凭据**：
#      口令/密钥一律在目标机首次安装时生成（`_offline_generate_placeholders`），
#      写进 0600 的 `.env`，**只打印"已生成"，绝不打印值**。
#   4. **幂等**：重复执行不会重新生成已存在的口令，也不会覆盖部署者已填的配置。
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# 随机源：openssl → python3 → /dev/urandom + od（三者有一即可）
#   ★ 不用 `date +%s` 之类可预测的东西：这些值会当 Dify SECRET_KEY / DB 口令用。
# ─────────────────────────────────────────────────────────────────────────────
_offline_rand_hex() {
  local n="${1:-24}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$n" 2>/dev/null && return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets,sys; sys.stdout.write(secrets.token_hex($n))" 2>/dev/null && return 0
  fi
  if [ -r /dev/urandom ] && command -v od >/dev/null 2>&1; then
    od -An -N"$n" -tx1 /dev/urandom 2>/dev/null | tr -d ' \n' && return 0
  fi
  return 1
}

# Dify 管理员的临时口令：**16 位十六进制 + 固定大小写数字前缀**。
#   ★ 为什么带前缀：Dify 的 setup 接口对弱口令有形状要求（大小写/数字），
#     单纯 hex 有小概率全字母。前缀让它稳定满足，且不泄漏任何信息。
_offline_rand_pass() { printf 'Cc1%s' "$(_offline_rand_hex 7)"; }

# ─────────────────────────────────────────────────────────────────────────────
# 把文件里的 `__CC_GENERATE__` 逐处替换成独立随机值（每行一个键，故每键一值）
#
# ★ 为什么要"逐处"而不是全局 sed：一个文件里有 SECRET_KEY / DB_PASSWORD /
#   REDIS_PASSWORD / PLUGIN_DAEMON_KEY 等多个键，**共用同一个值**会让一处泄漏
#   连带击穿全部组件。逐行生成才是对的。
# ★ 幂等：文件里没有占位符时直接返回 0（重复安装不会重置已有口令）。
# ─────────────────────────────────────────────────────────────────────────────
_offline_generate_placeholders() {
  local file="$1" tmp line rep rc=0
  [ -f "$file" ] || return 0
  grep -q '__CC_GENERATE__' "$file" 2>/dev/null || return 0

  tmp="$(mktemp)" || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *'__CC_GENERATE__'*)
        if ! rep="$(_offline_rand_hex 24)"; then rc=1; break; fi
        printf '%s\n' "${line//__CC_GENERATE__/$rep}"
        ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$file" > "$tmp"

  if [ "$rc" != "0" ]; then
    rm -f "$tmp"
    return 1
  fi
  # 自证：替换后不得再有占位符（防旧 bash 的 `${var//}` 行为差异）
  if grep -q '__CC_GENERATE__' "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    return 1
  fi
  chmod 600 "$tmp" 2>/dev/null || true
  mv "$tmp" "$file" || { rm -f "$tmp"; return 1; }
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# VERSION 读取（离线包的权威声明）
# ─────────────────────────────────────────────────────────────────────────────
offline_version_get() {
  local root="$1" key="$2" def="${3:-}" val
  val="$(env_get "$root/VERSION" "$key" "")"
  printf '%s' "${val:-$def}"
}

# ─────────────────────────────────────────────────────────────────────────────
# 本机业务地址（给 Dify 回连工具面用；断网环境下不能靠公网探测）
#   顺序：`ip route get` 的 src → `hostname -I` 的第一个地址
# ─────────────────────────────────────────────────────────────────────────────
_offline_host_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null \
      | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  # ★ 只接受 IPv4：`CC_DIFY_PUBLIC_HOST` 会被拼成 `http://<host>:<port>`，
  #   裸 IPv6 必须加方括号，拼错的结果是"注册上了但调不通"，很难查。
  case "$ip" in
    *:*) ip="" ;;
  esac
  printf '%s' "$ip"
}

_offline_norm_arch() {
  case "$1" in
    aarch64|arm64) printf 'arm64' ;;
    x86_64|amd64|x64) printf 'amd64' ;;
    *) printf '%s' "$1" ;;
  esac
}

# CRLF 自检：本项目的脚本在 Windows 上编辑过再传到 Linux，最常见的事故就是 CRLF
#   （报错形态是 `bash: $'\r': command not found` 或 `: not found`，极具迷惑性）。
_offline_check_lf() {
  local f="$1" label="$2"
  [ -f "$f" ] || return 0
  if head -n 3 "$f" 2>/dev/null | grep -q "$(printf '\r')"; then
    err "$label 含 CRLF 行尾（在 Linux 上会报 \`: not found\`）"
    err "  → 在目标机上执行：sed -i 's/\\r$//' $f   （或重新用二进制模式传输）"
    return 1
  fi
  return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# ①.0 离线包自检（只读；**不访问网络**）
#   退出语义：缺件 / 架构不符 → BLOCKED(2)；包内自相矛盾 → FAIL(1)
# ─────────────────────────────────────────────────────────────────────────────
offline_preflight() {
  local root="$1" schema tars n need cur arch want bad=0

  hdr "①.0 离线包自检（断网模式；只读，不访问网络）"

  [ -d "$root" ] || blocked "离线包目录不存在：$root"

  if [ ! -f "$root/VERSION" ]; then
    blocked "离线包缺 VERSION —— 这不是本项目的离线包（或传输不完整）。"
  fi
  schema="$(offline_version_get "$root" CC_BUNDLE_SCHEMA '')"
  if [ "$schema" != "ccops-offline/1" ]; then
    blocked "离线包 schema 不符：期望 ccops-offline/1，实际 '${schema:-空}'。请用 deploy/offline/make_offline_bundle.sh 重新出包。"
  fi
  ok "离线包 schema：$schema"

  for f in MANIFEST.sha256 deploy/install.sh deploy/lib/offline.sh deploy/.env.example; do
    [ -f "$root/$f" ] || { err "离线包缺件：$f"; bad=1; }
  done
  [ "$bad" = "0" ] || blocked "离线包结构不完整（见上），拒绝继续（避免装到一半才发现）。"
  ok "离线包结构完整（install.sh / lib / MANIFEST / .env.example）"

  tars="$(find "$root/images" -maxdepth 1 -type f -name '*.tar.gz' 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${tars:-0}" -lt 1 ]; then
    blocked "离线包 images/ 下没有 *.tar.gz —— 目标机断网时无法补齐镜像。"
  fi
  [ -f "$root/images/IMAGES.lock" ] \
    || blocked "离线包缺 images/IMAGES.lock（没有它就无法证明载入的是同一批镜像）。"
  ok "镜像包：$tars 个 tar.gz + IMAGES.lock"

  # ── 架构：镜像与目标机必须同架构（不符时 docker load 能成功，但运行期 exec format error）
  want="$(offline_version_get "$root" TARGET_ARCH "$(uname -m)")"
  arch="$(uname -m)"
  if [ "$(_offline_norm_arch "$arch")" != "$(_offline_norm_arch "$want")" ]; then
    blocked "架构不符：离线包为 ${want}，本机为 ${arch}。请在与目标机同架构的机器上重新出包（本项目目标机为 aarch64）。"
  fi
  ok "架构一致：$arch（包声明 $want）"

  # ── Dify（可选）：包内必须自带源码树，离线装不了 marketplace / git
  if [ "${CC_SKIP_DIFY:-0}" != "1" ]; then
    if [ ! -f "$root/vendor/dify/docker/docker-compose.yaml" ]; then
      blocked "离线包缺 vendor/dify/docker/docker-compose.yaml —— 无法在本机启动编排层。若只需应用层，请加 --no-dify（CC_SKIP_DIFY=1）。"
    fi
    if [ ! -f "$root/vendor/dify/docker/.env.offline-template" ]; then
      blocked "离线包缺 vendor/dify/docker/.env.offline-template（Dify 的 .env 模板）。"
    fi
    ok "Dify 编排树：vendor/dify/docker（$(offline_version_get "$root" CC_DIFY_VERSION '?')）"
  else
    skip "按 CC_SKIP_DIFY=1 跳过 Dify（只交付应用层 6 服务）"
  fi

  # ── 行尾：CRLF 会让所有 bash 脚本在 Linux 上直接失败
  local rel
  for rel in deploy/install.sh deploy/lib/offline.sh deploy/offline/install_offline.sh install-offline.sh; do
    _offline_check_lf "$root/$rel" "$rel" || bad=1
  done
  [ "$bad" = "0" ] || die "脚本行尾不规范（见上）—— 拒绝执行。"

  # ── 磁盘：镜像解压 + 卷 + 审计链，20 GB 是普通包的下限，离线全量包更大
  need="$(offline_version_get "$root" CC_MIN_DISK_GB_OFFLINE 40)"
  case "$need" in ''|*[!0-9]*) need=40 ;; esac
  cur="${CC_MIN_DISK_GB:-20}"
  case "$cur" in ''|*[!0-9]*) cur=20 ;; esac
  if [ "$need" -gt "$cur" ]; then
    export CC_MIN_DISK_GB="$need"
    info "离线包要求磁盘 ≥ ${need} GB（已把 CC_MIN_DISK_GB 从 ${cur} 提到 ${need}）"
  fi

  ok "①.0 离线包自检完成（全程未访问网络）"
}

# ─────────────────────────────────────────────────────────────────────────────
# ①.5 把 deploy/.env 准备到"可起栈"状态（幂等）
#
#   只填**空值**：部署者已填的 GaussDB/SSH/Dorado 等一律不动。
#   会覆盖的只有 CC_IMAGE_TAG（离线包的 tag 是权威，避免装旧镜像）。
# ─────────────────────────────────────────────────────────────────────────────
offline_prepare_env() {
  local envf="$1" mode="$2" root="$3"
  local tag neo grf dify_ver http https ports ip gid cur de p

  if [ ! -f "$envf" ]; then
    cp "$root/deploy/.env.example" "$envf" \
      || die "无法从 deploy/.env.example 生成 deploy/.env（目录只读？）"
    chmod 600 "$envf" 2>/dev/null || true
    info "已从 deploy/.env.example 生成 deploy/.env（权限 0600）"
  fi

  env_set "$envf" CC_OFFLINE 1 || die "写不回 $envf 的 CC_OFFLINE（文件只读？）"
  # 把本次档位落回 .env：否则以后有人不带 --mode 重跑时，会按模板里的 attached 走，
  # 与"这台机器其实是 standalone"矛盾（端口/载体行为都会变）。
  env_set "$envf" CC_DEPLOY_MODE "$mode" || die "写不回 CC_DEPLOY_MODE"

  tag="$(offline_version_get "$root" CC_IMAGE_TAG '')"
  [ -n "$tag" ] || die "离线包 VERSION 未声明 CC_IMAGE_TAG —— 拒绝用不一致的镜像标签继续。"
  env_set "$envf" CC_IMAGE_TAG "$tag" || die "写不回 CC_IMAGE_TAG"

  neo="$(offline_version_get "$root" CC_NEO4J_IMAGE '')"
  [ -n "$neo" ] && env_set "$envf" CC_NEO4J_IMAGE "$neo"
  grf="$(offline_version_get "$root" CC_GRAPHDB_IMAGE '')"
  [ -n "$grf" ] && env_set "$envf" CC_GRAPHDB_IMAGE "$grf"
  dify_ver="$(offline_version_get "$root" CC_DIFY_VERSION '')"
  [ -n "$dify_ver" ] && env_set "$envf" CC_DIFY_VERSION "$dify_ver"
  env_set "$envf" CC_DIFY_DIR "$root/vendor/dify"

  if [ "${CC_SKIP_DIFY:-0}" = "1" ]; then
    env_set "$envf" CC_SKIP_DIFY 1
  else
    env_set "$envf" CC_SKIP_DIFY 0
  fi

  # ── Neo4j 口令（standalone 档必填；本项目绝不起免密图库）──
  if [ "$mode" = "standalone" ] && [ -z "$(env_get "$envf" CC_NEO4J_PASSWORD '')" ]; then
    p="$(_offline_rand_hex 16)" || die "没有可用的随机源（openssl/python3//dev/urandom 均不可用）"
    env_set "$envf" CC_NEO4J_PASSWORD "$p" || die "写不回 CC_NEO4J_PASSWORD"
    info "已生成 Neo4j 口令（32 位十六进制；**不打印**，见 deploy/.env）"
  fi

  # ── Dify 管理员（没它 Dify 起了也不能用；口令同样不打印）──
  if [ "${CC_SKIP_DIFY:-0}" != "1" ]; then
    if [ -z "$(env_get "$envf" DIFY_EMAIL '')" ]; then
      env_set "$envf" DIFY_EMAIL "admin@ccops.local" || die "写不回 DIFY_EMAIL"
      info "已生成 Dify 管理员账号：admin@ccops.local（口令见 deploy/.env）"
    fi
    if [ -z "$(env_get "$envf" DIFY_PASSWORD '')" ]; then
      p="$(_offline_rand_pass)" || die "没有可用的随机源（生成 Dify 口令失败）"
      env_set "$envf" DIFY_PASSWORD "$p" || die "写不回 DIFY_PASSWORD"
      info "已生成 Dify 管理员口令（**不打印**，见 deploy/.env）"
    fi
  fi

  # ── Dify 对外端口：:80 常被既有服务占用，留空自动让路 ──
  http="$(env_get "$envf" CC_DIFY_HTTP_PORT '')"
  https="$(env_get "$envf" CC_DIFY_HTTPS_PORT '')"
  if [ -z "$http" ] || [ -z "$https" ]; then
    ports="$(_dep_dify_pick_ports "$envf")"
    http="${ports%% *}"; https="${ports##* }"
    [ -n "$http" ] && env_set "$envf" CC_DIFY_HTTP_PORT "$http"
    [ -n "$https" ] && env_set "$envf" CC_DIFY_HTTPS_PORT "$https"
  fi
  if [ -n "$http" ] && [ -z "$(env_get "$envf" DIFY_BASE_URL '')" ]; then
    env_set "$envf" DIFY_BASE_URL "http://127.0.0.1:$http" || die "写不回 DIFY_BASE_URL"
  fi

  # ── Dify 回连工具面的主机地址（断网环境不能靠公网探测）──
  if [ "${CC_SKIP_DIFY:-0}" != "1" ] && [ -z "$(env_get "$envf" CC_DIFY_PUBLIC_HOST '')" ]; then
    ip="$(_offline_host_ip)"
    if [ -n "$ip" ]; then
      env_set "$envf" CC_DIFY_PUBLIC_HOST "$ip"
      info "已识别本机地址：CC_DIFY_PUBLIC_HOST=$ip（Dify 用它回连 :8301–:8305）"
    else
      warn "未能自动识别本机业务地址 ⇒ CC_DIFY_PUBLIC_HOST 留空。"
      warn "  Dify 注册工具时若用 127.0.0.1，容器内指向 Dify 自己 ⇒ 工具全部不可用。"
      warn "  请手工在 deploy/.env 填本机可达 IP 后重跑，再执行 deploy/offline/wire_dify.sh。"
    fi
  fi

  # ── 采集层要读宿主 docker.sock：组 gid 随机器而变，写错的后果是"静默采空" ──
  if [ -S /var/run/docker.sock ] && command -v stat >/dev/null 2>&1; then
    gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
    cur="$(env_get "$envf" CC_DOCKER_GID '')"
    if [ -n "$gid" ] && [ "$cur" != "$gid" ]; then
      env_set "$envf" CC_DOCKER_GID "$gid"
      warn "CC_DOCKER_GID 已按本机 /var/run/docker.sock 修正：'${cur:-空}' → $gid"
    fi
  fi

  # ── 宿主 Python：standalone 档的 GraphDB 建仓脚本在宿主上跑，缺了这一步就断 ──
  if [ "$mode" = "standalone" ] && ! command -v python3 >/dev/null 2>&1 \
     && ! command -v python >/dev/null 2>&1; then
    warn "宿主上没有 python3："
    warn "  · install.sh ④.0 的 GraphDB 建仓脚本（create_graphdb_repo.py）无法执行"
    warn "    ⇒ fresh GraphDB 上没有 cc-ops 仓库 ⇒ TBox 装载与 onto 都会失败；"
    warn "  · 验收矩阵 deploy/verify_deploy.py 也无法运行。"
    warn "  请先安装 python3（≥3.8）后重跑。"
  fi

  # ── Dify 自己的 docker/.env：模板 → 实文件 → 逐键生成随机值 ──
  if [ "${CC_SKIP_DIFY:-0}" != "1" ]; then
    de="$root/vendor/dify/docker/.env"
    if [ -d "$root/vendor/dify/docker" ]; then
      # ★ 灾难性组合：库卷里有数据（上次装过）但 .env 丢了 ⇒ 新生成的口令与旧库不匹配，
      #   Dify 会以"数据库连接失败"crash-loop，而报错完全不会提"口令变了"。
      if [ ! -f "$de" ] && [ -n "$(ls -A "$root/vendor/dify/docker/volumes/db/data" 2>/dev/null)" ]; then
        warn "检测到 Dify 数据库卷已有数据，但 vendor/dify/docker/.env 不存在。"
        warn "  新生成的口令与旧库不匹配 ⇒ Dify 会连不上库（报错不会点名这个原因）。"
        warn "  处置：① 从备份恢复原来的 docker/.env；或 ② 确认可丢 Dify 数据后执行"
        warn "        cd vendor/dify/docker && docker compose down -v  再重装。"
      fi
      if [ ! -f "$de" ]; then
        if [ -f "$root/vendor/dify/docker/.env.offline-template" ]; then
          cp "$root/vendor/dify/docker/.env.offline-template" "$de" \
            || die "无法生成 Dify 的 docker/.env（目录只读？）"
          chmod 600 "$de" 2>/dev/null || true
          info "已从 .env.offline-template 生成 Dify 的 docker/.env（权限 0600）"
        else
          warn "离线包缺 .env.offline-template ⇒ Dify 的 .env 需人工准备"
        fi
      fi
      if [ -f "$de" ]; then
        _offline_generate_placeholders "$de" \
          || warn "Dify 的 .env 里仍有 __CC_GENERATE__ 占位符未替换（随机源不可用）"
        # 断网环境必须**强制**关掉插件市场（append-only 不够：模板可能写着 true）
        if grep -qE '^[[:space:]]*MARKETPLACE_ENABLED[[:space:]]*=' "$de" 2>/dev/null; then
          sed -i -E 's|^[[:space:]]*MARKETPLACE_ENABLED[[:space:]]*=.*|MARKETPLACE_ENABLED=false|' "$de"
        else
          printf 'MARKETPLACE_ENABLED=false\n' >> "$de"
        fi
      fi
    fi
  fi

  ok "①.5 离线配置已准备（deploy/.env / vendor/dify/docker/.env）"
}

# ─────────────────────────────────────────────────────────────────────────────
# ②.4 静默判定：**全部**镜像都已在本地且与锁一致吗？
#   仅用于"幂等重跑时跳过 docker load"（8 GB 的包重载一次要几分钟）。
#   与 offline_assert_images 的区别：本函数**不打印错误、不计入失败数**，
#   它只回答"能不能省掉这一步"；真正的断言仍由 offline_assert_images 负责。
# ─────────────────────────────────────────────────────────────────────────────
offline_images_match() {
  local root="${1:-}" lock ref want _arch _size _digest got n=0
  [ -n "$root" ] || return 1
  lock="$root/images/IMAGES.lock"
  [ -f "$lock" ] || return 1
  while IFS='|' read -r ref want _arch _size _digest; do
    case "$ref" in
      ''|'#'*) continue ;;
    esac
    n=$((n + 1))
    got="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)" || return 1
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
      return 1
    fi
  done < "$lock"
  [ "$n" -gt 0 ]
}

# ─────────────────────────────────────────────────────────────────────────────
# ②.5 镜像身份校验：按 IMAGES.lock 逐条核 image Id 与架构
#
# ★ 为什么核 Id 而不是核 tag：`docker load` 对同名同 tag 的镜像会**覆盖**本地旧镜像，
#   若包里的 tar 被换过，仅看 tag 是发现不了的。image Id 是内容摘要，装错必暴露。
# ★ 为什么核架构：架构不符时 `docker load` 成功、`compose up` 也"成功"，
#   但容器会 Exit 255 / `exec format error`。验收器 C2 虽能抓到，但那时已经装了一半。
# ─────────────────────────────────────────────────────────────────────────────
offline_assert_images() {
  local root="${1:-}" lock ref want arch got gotarch n=0 bad=0
  [ -n "$root" ] || die "内部错误：offline_assert_images 未收到离线包根目录"
  lock="$root/images/IMAGES.lock"
  [ -f "$lock" ] || die "离线包缺 images/IMAGES.lock；无法证明载入的镜像与出包时一致，拒绝继续。"

  while IFS='|' read -r ref want arch _size _digest; do
    case "$ref" in
      ''|'#'*) continue ;;
    esac
    n=$((n + 1))
    if ! got="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)"; then
      err "缺少镜像：$ref"
      bad=$((bad + 1))
      continue
    fi
    if [ -n "$want" ] && [ "$got" != "$want" ]; then
      err "镜像身份不符：$ref"
      err "    包声明 $want"
      err "    本机实际 $got"
      bad=$((bad + 1))
      continue
    fi
    if [ -n "$arch" ]; then
      gotarch="$(docker image inspect -f '{{.Architecture}}' "$ref" 2>/dev/null || true)"
      if [ -n "$gotarch" ] && [ "$gotarch" != "$arch" ]; then
        err "镜像架构不符：$ref 是 $gotarch，包声明 $arch"
        bad=$((bad + 1))
      fi
    fi
  done < "$lock"

  [ "$n" -gt 0 ] || die "IMAGES.lock 为空（出包脚本可能中断过）—— 拒绝继续。"
  if [ "$bad" -gt 0 ]; then
    err "有 $bad 个镜像与离线包声明不一致。"
    err "  → 若刚 docker load 过，说明 tar 被替换/截断：重新拷贝离线包并核对 MANIFEST.sha256。"
    err "  → 若本机已存在同名镜像，docker load 会覆盖它；出现本错误说明覆盖没有发生（tar 里不含该 ref）。"
    die "拒绝用与出包声明不一致的镜像继续（绝不用旧/别的镜像冒充）。"
  fi
  ok "镜像身份校验通过：$n 个（image Id 与 IMAGES.lock 逐条一致）"
}
