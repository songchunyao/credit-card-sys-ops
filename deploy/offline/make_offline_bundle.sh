#!/bin/sh
# =============================================================================
# deploy/offline/make_offline_bundle.sh —— 生成**全量离线包**（断网可装）
#
# 与 deploy/make_bundle.sh 的关系
# -------------------------------
#   make_bundle.sh 产出的是「应用层离线包」：只带自建的 5 个镜像，Neo4j / GraphDB /
#   Dify 被裁定为「安装环境依赖」，由目标机联网补装。
#   本脚本在它**之上**补齐这三块，产出「**全量**离线包」：
#
#       ① 应用层 5 镜像        ← 复用 make_bundle.sh（含源码树 / MANIFEST / VERSION）
#       ② 载体 2 镜像          ← neo4j:5-community、ontotext/graphdb:10.8.0
#       ③ Dify 编排树 + 全镜像 ← 官方 docker/ 源码树 + 按 compose 实际引用逐个拉取
#       ④ images/IMAGES.lock   ← 每个 ref 的 image Id / 架构，装载后逐条核对
#
# ★ 本脚本是 **POSIX sh**（与 make_bundle.sh 同因：CI 镜像 Alpine 无 bash）。
#
# 在**哪台机器**上跑
# ------------------
#   · 有 Docker、能联网（拉镜像、取 Dify 源码）；
#   · **架构必须与目标机一致**（本项目目标机为 aarch64）：
#       同架构构建机 → 默认即可
#       交叉出包     → --platform linux/arm64（docker pull --platform 支持）
#     应用层 5 镜像**无法**交叉构建，仍需在与目标同架构的机器上 build_images.sh。
#
# 用法
# ----
#     sh deploy/offline/make_offline_bundle.sh --tag <镜像标签> [选项]
#
#     --tag TAG              必填。与 deploy/build_images.sh 用的标签一致
#     --out DIR              输出目录（默认 dist/offline）
#     --platform PLATFORM    linux/arm64 | linux/amd64（默认按本机）
#     --dify-version VER     Dify 版本（默认取 deploy/.env.example 的 CC_DIFY_VERSION）
#     --dify-src DIR         用本地已解压的 Dify 源码树（离线/内网镜像源场景；目录需含 docker/）
#     --build                先调 deploy/build_images.sh 构建 5 个应用镜像（需 Docker + 能装 pip 依赖）
#     --neo4j-image IMG      默认 neo4j:5-community
#     --graphdb-image IMG    默认 ontotext/graphdb:10.8.0
#     --skip-dify            只带应用层 + 载体（目标机复用既有 Dify）
#     --archive              额外打一个 tar.gz（>4 GB，FAT32 传输需再 split）
#     --skip-selfcheck       构建机无 python3 时**显式**跳过包自检（自担风险；默认拒绝出包）
#     -h|--help
#
# 产物
# ----
#     <out>/ccops-offline-<tag>/
#     ├── install-offline.sh          ← 目标机一键入口
#     ├── images/
#     │   ├── ccops-app-images.tar.gz     5 个应用镜像（make_bundle.sh 产出）
#     │   ├── 20-carriers-images.tar.gz   Neo4j + GraphDB
#     │   ├── 30-dify-images.tar.gz       Dify 全部镜像（合一包，共享层只存一份）
#     │   └── IMAGES.lock                 ref|imageId|arch|size|digest
#     ├── vendor/dify/
#     │   ├── docker/                     Dify 官方 docker/ 编排树（**不含 .env**）
#     │   │   └── .env.offline-template   安装时生成 .env 的模板（含 __CC_GENERATE__）
#     │   └── IMAGES.txt                  出包时解析到的镜像清单（可复核）
#     ├── deploy/ shared/ scheme*/ ...    ← 同 make_bundle.sh
#     ├── VERSION                         离线包权威声明
#     ├── MANIFEST.sha256                 全量校验（相对路径）
#     └── README.md                       目标机照着做即可
# =============================================================================
set -eu

TAG=""
OUT="dist/offline"
NAME="ccops-offline"
DIFY_VERSION=""
DIFY_SRC=""
PLATFORM=""
NEO4J_IMAGE=""
GRAPHDB_IMAGE=""
SKIP_DIFY=0
DO_ARCHIVE=0
SKIP_SELFCHECK=0
DO_BUILD=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '[ok  ] %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,60p' "$0" | sed -e 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)            TAG="${2:-}"; shift 2 ;;
    --tag=*)          TAG="${1#*=}"; shift ;;
    --out)            OUT="${2:-}"; shift 2 ;;
    --out=*)          OUT="${1#*=}"; shift ;;
    --platform)       PLATFORM="${2:-}"; shift 2 ;;
    --platform=*)     PLATFORM="${1#*=}"; shift ;;
    --dify-version)   DIFY_VERSION="${2:-}"; shift 2 ;;
    --dify-version=*) DIFY_VERSION="${1#*=}"; shift ;;
    --dify-src)       DIFY_SRC="${2:-}"; shift 2 ;;
    --dify-src=*)     DIFY_SRC="${1#*=}"; shift ;;
    --neo4j-image)    NEO4J_IMAGE="${2:-}"; shift 2 ;;
    --neo4j-image=*)  NEO4J_IMAGE="${1#*=}"; shift ;;
    --graphdb-image)  GRAPHDB_IMAGE="${2:-}"; shift 2 ;;
    --graphdb-image=*) GRAPHDB_IMAGE="${1#*=}"; shift ;;
    --skip-dify)      SKIP_DIFY=1; shift ;;
    --archive)        DO_ARCHIVE=1; shift ;;
    --build)          DO_BUILD=1; shift ;;
    --skip-selfcheck) SKIP_SELFCHECK=1; shift ;;
    -h|--help)        usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# ── 从 .env.example 取默认值（与线上配置同一来源，避免两处漂移）──────────────
file_kv() {
  # file_kv <file> <key>
  [ -f "$1" ] || return 0
  grep -E "^[[:space:]]*$2[[:space:]]*=" "$1" 2>/dev/null \
    | tail -n 1 | cut -d= -f2- \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/[[:space:]]*#.*$//'
}

[ -n "$TAG" ] || { printf '错误：必须给 --tag（与 deploy/build_images.sh 的标签一致）\n' >&2; exit 2; }
# tag 会进文件名、镜像 ref、sed 替换式与 VERSION；限定字符集，避免"出包成功但装不上"。
case "$TAG" in
  *[!0-9A-Za-z._-]*) die "非法 --tag：'$TAG'（只允许字母/数字/._-）" ;;
esac

[ -n "$NEO4J_IMAGE" ]   || NEO4J_IMAGE="$(file_kv deploy/.env.example CC_NEO4J_IMAGE)"
[ -n "$NEO4J_IMAGE" ]   || NEO4J_IMAGE="neo4j:5-community"
[ -n "$GRAPHDB_IMAGE" ] || GRAPHDB_IMAGE="$(file_kv deploy/.env.example CC_GRAPHDB_IMAGE)"
[ -n "$GRAPHDB_IMAGE" ] || GRAPHDB_IMAGE="ontotext/graphdb:10.8.0"
[ -n "$DIFY_VERSION" ]  || DIFY_VERSION="$(file_kv deploy/.env.example CC_DIFY_VERSION)"
[ -n "$DIFY_VERSION" ]  || DIFY_VERSION="1.17.0"

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
trap 'exit 130' INT TERM

# ── 前置：docker / compose v2 ──────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "本机没有 docker —— 出包必须在有 Docker 的机器上做。"
docker version >/dev/null 2>&1 || die "docker 守护进程不可达（未启动 / 无权限 / DOCKER_HOST 指错）。"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose v2 插件（本脚本要用 \`docker compose config --images\`）。"
ok "docker：$(docker --version 2>/dev/null)"

# ── 平台 / 架构 ────────────────────────────────────────────────────────────
HOST_ARCH="$(uname -m)"
if [ -z "$PLATFORM" ]; then
  case "$HOST_ARCH" in
    aarch64|arm64) PLATFORM="linux/arm64" ;;
    x86_64|amd64)  PLATFORM="linux/amd64" ;;
    *) die "无法识别的本机架构 $HOST_ARCH，请显式给 --platform linux/arm64|linux/amd64" ;;
  esac
fi
case "$PLATFORM" in
  linux/amd64|linux/arm64) ;;
  *) die "--platform 只支持 linux/amd64 或 linux/arm64（收到 '$PLATFORM'）" ;;
esac
TARGET_ARCH="${PLATFORM#linux/}"
say ""
say "════════════════════════════════════════════════════════════════"
say " 离线包构建"
say "   构建机架构：$HOST_ARCH"
say "   目标平台  ：$PLATFORM（目标机架构必须是 $TARGET_ARCH）"
say "   镜像标签  ：$TAG"
say "   Dify 版本 ：$([ "$SKIP_DIFY" = 1 ] && echo '（跳过）' || echo "$DIFY_VERSION")"
say "════════════════════════════════════════════════════════════════"

# ── 应用层 5 镜像必须在本地，且架构与目标一致 ──────────────────────────────
if [ "$DO_BUILD" = "1" ]; then
  say ""
  say "── ⓪ 构建应用层镜像（deploy/build_images.sh --tag $TAG）──"
  sh deploy/build_images.sh --tag "$TAG" \
    || die "应用层镜像构建失败 —— **不得**用不完整的镜像集出包。"
fi

for s in collect kg onto kgt live-facts; do
  img="cc-ops/${s}:${TAG}"
  docker image inspect "$img" >/dev/null 2>&1 \
    || die "本地没有镜像 $img。请先用 deploy/build_images.sh --tag $TAG 在**与目标同架构**的机器上构建。"
  a="$(docker image inspect -f '{{.Architecture}}' "$img" 2>/dev/null || true)"
  if [ -n "$a" ] && [ "$a" != "$TARGET_ARCH" ]; then
    die "$img 是 $a，目标平台要求 $TARGET_ARCH。应用层镜像**不能**交叉构建，请换同架构机器。"
  fi
  ok "应用镜像就绪：$img（$a）"
done

# ── ① 复用 make_bundle.sh 产出应用层离线包 ─────────────────────────────────
mkdir -p "$OUT"
say ""
say "── ① 生成应用层基础包（deploy/make_bundle.sh）──"
sh deploy/make_bundle.sh --tag "$TAG" --out "$OUT"

BASE="$OUT/ccops-release-$TAG"
STAGE="$OUT/$NAME-$TAG"
[ -d "$BASE" ] || die "make_bundle.sh 未产出预期目录：$BASE"
rm -rf "$STAGE"
mv "$BASE" "$STAGE"
ok "基础包就位：$STAGE"

# ── 通用：拉镜像（带重试 + 架构断言）───────────────────────────────────────
pull_image() {
  _img="$1"
  _i=0
  if docker image inspect "$_img" >/dev/null 2>&1; then
    _a="$(docker image inspect -f '{{.Architecture}}' "$_img" 2>/dev/null || true)"
    if [ "$_a" = "$TARGET_ARCH" ]; then
      ok "本地已有：$_img（$_a）"
      return 0
    fi
    warn "本地 $_img 是 $_a，需要 $TARGET_ARCH ⇒ 重新拉取"
  fi
  while [ "$_i" -lt 4 ]; do
    _i=$((_i + 1))
    if docker pull --platform "$PLATFORM" "$_img"; then
      _a="$(docker image inspect -f '{{.Architecture}}' "$_img" 2>/dev/null || true)"
      if [ "$_a" != "$TARGET_ARCH" ]; then
        die "拉取到的 $_img 架构是 '$_a'，不是 $TARGET_ARCH（镜像源可能忽略了 --platform）。"
      fi
      return 0
    fi
    warn "拉取失败（第 ${_i}/4 次）：$_img；10s 后重试"
    sleep 10
  done
  die "无法拉取 $_img。请检查构建机网络/镜像源，或先用 docker pull 预置。"
}

# 通用：docker save -o → gzip（**不用管道**：管道里 docker save 失败会被 gzip 掩盖）
save_group() {
  _out="$1"; shift
  _tar="$_out.uncompressed.tar"
  rm -f "$_tar" "$_out"
  docker save -o "$_tar" "$@" || { rm -f "$_tar"; die "docker save 失败：$*"; }
  gzip -9 -c "$_tar" > "$_out" || { rm -f "$_tar" "$_out"; die "gzip 失败：$_out"; }
  rm -f "$_tar"
  ok "镜像包：$_out（$(du -h "$_out" | cut -f1)）"
}

# ── ② 载体镜像 ─────────────────────────────────────────────────────────────
say ""
say "── ② 载体镜像（Neo4j / GraphDB）──"
pull_image "$NEO4J_IMAGE"
pull_image "$GRAPHDB_IMAGE"
save_group "$STAGE/images/20-carriers-images.tar.gz" "$NEO4J_IMAGE" "$GRAPHDB_IMAGE"

# ── ③ Dify：源码树 + 全镜像 ────────────────────────────────────────────────
DIFY_DEST="$STAGE/vendor/dify"
DIFY_IMAGE_COUNT=0

# Dify 的 .env 逐键写入（保留原有注释行，只改活动行；不存在则追加）
set_key() {
  _f="$1"; _k="$2"; _v="$3"; _t="$_f.tmp.$$"
  awk -v k="$_k" -v v="$_v" '
    BEGIN { done = 0 }
    {
      if ($0 ~ "^[[:space:]]*" k "[[:space:]]*=") {
        if (!done) { print k "=" v; done = 1 }
        next
      }
      print
    }
    END { if (!done) print k "=" v }
  ' "$_f" > "$_t" && mv "$_t" "$_f"
}

if [ "$SKIP_DIFY" = "1" ]; then
  say ""
  warn "按 --skip-dify 跳过 Dify（目标机须复用既有 Dify，否则只剩应用层）"
else
  say ""
  say "── ③ Dify 编排树（vendor/dify）──"
  mkdir -p "$DIFY_DEST"

  if [ -n "$DIFY_SRC" ]; then
    [ -f "$DIFY_SRC/docker/docker-compose.yaml" ] \
      || die "--dify-src 里没有 docker/docker-compose.yaml：$DIFY_SRC"
    cp -a "$DIFY_SRC/docker" "$DIFY_DEST/docker"
    [ -f "$DIFY_SRC/LICENSE" ] && cp "$DIFY_SRC/LICENSE" "$DIFY_DEST/" || true
    # ★ 必须赋值：下面统一的"打印 commit"分支会读 $SRC；漏了它在 set -u 下直接
    #   `SRC: unbound variable` 中止（--dify-src 路径专有）。
    SRC="$DIFY_SRC"
    ok "使用本地 Dify 源码树：$DIFY_SRC"
  else
    case "$DIFY_VERSION" in
      *[!0-9A-Za-z._-]*) die "非法 Dify 版本号：'$DIFY_VERSION'" ;;
    esac
    URL="https://github.com/langgenius/dify/archive/refs/tags/${DIFY_VERSION}.tar.gz"
    TGZ="$TMPD/dify-${DIFY_VERSION}.tar.gz"
    EXDIR="$TMPD/dify-x"
    if command -v curl >/dev/null 2>&1; then
      ok "下载 Dify ${DIFY_VERSION} 源码：$URL"
      curl -fL --retry 3 --connect-timeout 20 -o "$TGZ" "$URL" \
        || die "下载失败。内网环境请用 --dify-src 指向已解压的 Dify 源码树（目录需含 docker/）。"
    elif command -v wget >/dev/null 2>&1; then
      ok "下载 Dify ${DIFY_VERSION} 源码：$URL"
      wget -O "$TGZ" "$URL" || die "下载失败（wget）。请改用 --dify-src。"
    elif command -v git >/dev/null 2>&1; then
      ok "git clone Dify ${DIFY_VERSION}"
      git clone --depth 1 --branch "$DIFY_VERSION" \
        https://github.com/langgenius/dify.git "$TMPD/dify-git" \
        || die "git clone 失败。请改用 --dify-src。"
      TGZ=""
    else
      die "构建机需要 curl / wget / git 之一来取 Dify 源码；或给 --dify-src。"
    fi

    if [ -n "$TGZ" ]; then
      mkdir -p "$EXDIR"
      tar -xzf "$TGZ" -C "$EXDIR" || die "解压 Dify 源码失败：$TGZ"
      SRC=""
      for d in "$EXDIR"/*; do
        [ -d "$d" ] || continue
        if [ -f "$d/docker/docker-compose.yaml" ]; then SRC="$d"; fi
      done
      [ -n "$SRC" ] || die "解压后的 Dify 源码里找不到 docker/docker-compose.yaml（版本 $DIFY_VERSION 目录结构可能已变）。"
      cp -a "$SRC/docker" "$DIFY_DEST/docker"
      [ -f "$SRC/LICENSE" ] && cp "$SRC/LICENSE" "$DIFY_DEST/" || true
    else
      cp -a "$TMPD/dify-git/docker" "$DIFY_DEST/docker"
      [ -f "$TMPD/dify-git/LICENSE" ] && cp "$TMPD/dify-git/LICENSE" "$DIFY_DEST/" || true
      SRC="$TMPD/dify-git"
    fi
    if command -v git >/dev/null 2>&1 && [ -d "$SRC/.git" ]; then
      say "   Dify 源码 commit：$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)"
    fi
  fi

  DOCKER_DIR="$DIFY_DEST/docker"
  [ -f "$DOCKER_DIR/.env.example" ] \
    || die "Dify 的 docker/ 里没有 .env.example（版本 $DIFY_VERSION 结构可能已变）。"
  [ -f "$DOCKER_DIR/docker-compose.yaml" ] \
    || die "Dify 的 docker/ 里没有 docker-compose.yaml。"

  # ★★ 真实事故（2026-10-18，第一版包）：--dify-src 指向的 docker/ 里有
  #   `.env.bak_r59_...`（212 个非空键，含 DB/Redis/plugin 真实口令），被 cp -a
  #   原样打进包，而当时的自检只认 `.env` 这个名字，**没拦住**。
  #   ⇒ 这里一律剔除 `.env*` 备份/填单，只保留上游的 `.env.example`。
  ENV_BAKS="$(find "$DOCKER_DIR" -type f -name '.env*' ! -name '.env.example' -print 2>/dev/null || true)"
  if [ -n "$ENV_BAKS" ]; then
    warn "Dify docker/ 里有 .env 备份/填单，已剔除（可能含真实口令）："
    printf '%s\n' "$ENV_BAKS" | sed 's/^/        /'
    printf '%s\n' "$ENV_BAKS" | while IFS= read -r _eb; do rm -f "$_eb"; done
  fi

  # 防事故：--dify-src 指向一套**正在运行**的 Dify 时，volumes/ 里是整个库和文件
  #   ⇒ 清空数据、只留空目录骨架（数据绝不能进发布物，且会把包撑到几十 GB）。
  if [ -d "$DOCKER_DIR/volumes" ]; then
    VN="$(find "$DOCKER_DIR/volumes" -type f 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${VN:-0}" -gt 0 ]; then
      warn "Dify 目录里已有 $VN 个数据文件（像是一套运行中的实例）⇒ 清空 volumes/，只保留空目录"
      rm -rf "$DOCKER_DIR/volumes"
    fi
  fi
  for vd in app/storage db/data weaviate plugin_daemon; do
    mkdir -p "$DOCKER_DIR/volumes/$vd"
    : > "$DOCKER_DIR/volumes/$vd/.gitkeep"
  done

  # Dify 的 .env：上游模板 → 生成态模板
  #   ★ 只装**模板**、不装填好的 .env：.env 在目标机首次安装时生成（0600），
  #     这样它不会进 MANIFEST（否则安装后校验 sha256 必然失败）。
  cp "$DOCKER_DIR/.env.example" "$DOCKER_DIR/.env"
  # 统一行尾：--dify-src 若来自 Windows 检出，.env 里会有 CRLF ⇒ 值尾带 \r，
  #   生成的口令/密钥会变成脏值，报错还完全不提"行尾"。
  sed -i 's/\r$//' "$DOCKER_DIR/.env"
  set_key "$DOCKER_DIR/.env" SECRET_KEY "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" DB_PASSWORD "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" REDIS_PASSWORD "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" SANDBOX_API_KEY "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" CODE_EXECUTION_API_KEY "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" PLUGIN_DAEMON_KEY "__CC_GENERATE__"
  set_key "$DOCKER_DIR/.env" PLUGIN_DIFY_INNER_API_KEY "__CC_GENERATE__"
  # 断网环境必须关掉插件市场：否则 plugin_daemon 反复超时，日志全是噪声
  set_key "$DOCKER_DIR/.env" MARKETPLACE_ENABLED "false"
  grep -qE '^SECRET_KEY=' "$DOCKER_DIR/.env" \
    || die "Dify 的 .env 里没有 SECRET_KEY（版本 $DIFY_VERSION 结构可能已变）。"

  # 解析 compose 实际引用的镜像清单（**由 compose 自己算**，不靠人工维护的清单）
  say "   解析 Dify compose 引用的镜像……"
  if ! ( cd "$DOCKER_DIR" && docker compose --env-file .env config --images ) \
        > "$TMPD/dify-images.raw" 2> "$TMPD/dify-config.err"; then
    cat "$TMPD/dify-config.err" >&2
    die "docker compose config --images 失败（compose 版本过旧？需 v2.20+）。"
  fi
  sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$TMPD/dify-images.raw" \
    | grep -v '^$' | sort -u > "$DIFY_DEST/IMAGES.txt"
  DIFY_IMAGE_COUNT="$(wc -l < "$DIFY_DEST/IMAGES.txt" | tr -d ' ')"
  [ "${DIFY_IMAGE_COUNT:-0}" -ge 6 ] \
    || die "Dify compose 只解析出 $DIFY_IMAGE_COUNT 个镜像（预期 ≥6）—— 解析结果不可信，拒绝出包。"
  ok "Dify 镜像清单：$DIFY_IMAGE_COUNT 个（写入 vendor/dify/IMAGES.txt）"
  sed 's/^/     - /' "$DIFY_DEST/IMAGES.txt"

  say "   拉取 Dify 镜像（可能较慢，单镜像失败会重试 4 次）……"
  while IFS= read -r dimg; do
    [ -n "$dimg" ] || continue
    pull_image "$dimg"
  done < "$DIFY_DEST/IMAGES.txt"

  say "   导出 Dify 镜像为一包（共享层只存一份）……"
  # shellcheck disable=SC2046
  save_group "$STAGE/images/30-dify-images.tar.gz" $(tr '\n' ' ' < "$DIFY_DEST/IMAGES.txt")

  # 生成态模板留下来，填好的 .env **不入包**
  cp "$DOCKER_DIR/.env" "$DOCKER_DIR/.env.offline-template"
  rm -f "$DOCKER_DIR/.env"
  chmod 644 "$DOCKER_DIR/.env.offline-template" 2>/dev/null || true
  ok "Dify .env 模板：vendor/dify/docker/.env.offline-template（安装时再生成 .env）"
fi

# ── ④ IMAGES.lock：所有 ref 的 image Id / 架构（装载后逐条核对）────────────
say ""
say "── ④ 生成 IMAGES.lock ──"
: > "$TMPD/image-list.txt"
for s in collect kg onto kgt live-facts; do
  printf 'cc-ops/%s:%s\n' "$s" "$TAG" >> "$TMPD/image-list.txt"
done
printf '%s\n' "$NEO4J_IMAGE"   >> "$TMPD/image-list.txt"
printf '%s\n' "$GRAPHDB_IMAGE" >> "$TMPD/image-list.txt"
if [ "$SKIP_DIFY" != "1" ] && [ -f "$DIFY_DEST/IMAGES.txt" ]; then
  cat "$DIFY_DEST/IMAGES.txt" >> "$TMPD/image-list.txt"
fi
awk 'NF && !seen[$0]++' "$TMPD/image-list.txt" > "$TMPD/image-list.uniq"

TOTAL_BYTES=0
{
  printf '# =============================================================================\n'
  printf '# images/IMAGES.lock —— 离线包的镜像身份锁（由 make_offline_bundle.sh 生成）\n'
  printf '# 格式：ref|image_id|architecture|size_bytes|repo_digest\n'
  printf '# 用途：docker load 之后由 deploy/lib/offline.sh 逐条核对 image Id；\n'
  printf '#       不一致即拒绝继续（tag 可以 retag，Id 不能）。\n'
  printf '# 生成时间：%s  平台：%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$PLATFORM"
  printf '# =============================================================================\n'
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    iid="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)" \
      || die "IMAGES.lock 生成时找不到镜像：$ref"
    iarch="$(docker image inspect -f '{{.Architecture}}' "$ref" 2>/dev/null || echo unknown)"
    isize="$(docker image inspect -f '{{.Size}}' "$ref" 2>/dev/null || echo 0)"
    idig="$(docker image inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}-{{end}}' "$ref" 2>/dev/null || echo -)"
    printf '%s|%s|%s|%s|%s\n' "$ref" "$iid" "$iarch" "$isize" "$idig"
  done < "$TMPD/image-list.uniq"
} > "$STAGE/images/IMAGES.lock"

IMG_N="$(grep -vc '^#' "$STAGE/images/IMAGES.lock" 2>/dev/null || true)"
[ -n "$IMG_N" ] || IMG_N="$(awk 'NF && $0 !~ /^#/' "$STAGE/images/IMAGES.lock" | wc -l | tr -d ' ')"
TOTAL_BYTES="$(awk -F'|' 'NF>=4 && $1 !~ /^#/ {s+=$4} END{printf "%d", s}' "$STAGE/images/IMAGES.lock")"
ok "IMAGES.lock：$IMG_N 个镜像，解压后合计 $(awk -v b="${TOTAL_BYTES:-0}" 'BEGIN{printf "%.1f", b/1073741824}') GiB"

# 磁盘需求：镜像解压 + 卷 + 审计链 + 余量（经验系数 3 倍 + 15 GB 底座）
DISK_GB="$(awk -v b="${TOTAL_BYTES:-0}" 'BEGIN{v=int(b/1073741824*3)+15; if(v<40) v=40; printf "%d", v}')"

# ── ⑤ VERSION（权威声明）───────────────────────────────────────────────────
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
cat > "$STAGE/VERSION" <<EOF
# 本文件是**全量离线包**的权威声明：install.sh 以 CC_IMAGE_TAG 覆盖 deploy/.env
# CC_BUNDLE_SCHEMA=ccops-offline/1 表示"载体与 Dify 都已随包携带，安装时不得联网"
CC_BUNDLE_SCHEMA=ccops-offline/1
CC_APP_BUNDLE_SCHEMA=ccops-release/1
CC_IMAGE_TAG=$TAG
CC_PLATFORM=$PLATFORM
TARGET_ARCH=$TARGET_ARCH
CC_NEO4J_IMAGE=$NEO4J_IMAGE
CC_GRAPHDB_IMAGE=$GRAPHDB_IMAGE
CC_DIFY_VERSION=$([ "$SKIP_DIFY" = 1 ] && echo '' || echo "$DIFY_VERSION")
CC_DIFY_DIR=vendor/dify
CC_SKIP_DIFY=$SKIP_DIFY
CC_OFFLINE=1
CC_MIN_DISK_GB_OFFLINE=$DISK_GB
CC_IMAGE_COUNT=$IMG_N
SERVICES=collect kg onto kgt live-facts
GIT_SHA=$GIT_SHA
BUILT_AT=$BUILT_AT
BUILD_HOST_ARCH=$HOST_ARCH
CI_JOB_ID=${CI_JOB_ID:-local}
EOF
ok "VERSION 已写（tag=$TAG platform=$PLATFORM images=$IMG_N git=$GIT_SHA）"

# ── ⑥ README / 一键入口 ────────────────────────────────────────────────────
[ -f deploy/offline/BUNDLE-README.md ] \
  || die "缺 deploy/offline/BUNDLE-README.md（离线包 README 模板）。"
if [ "$SKIP_DIFY" = "1" ]; then
  DIFY_SENTENCE='本包按 `--skip-dify` 出包，**不含 Dify**；Dify 需复用既有实例（或改用含 Dify 的包）。'
else
  DIFY_SENTENCE="Dify（${DIFY_VERSION}）的编排树与全部镜像也随包携带。"
fi
sed -e "s|@TAG@|$TAG|g" \
    -e "s|@BUILT_AT@|$BUILT_AT|g" \
    -e "s|@PLATFORM@|$PLATFORM|g" \
    -e "s|@TARGET_ARCH@|$TARGET_ARCH|g" \
    -e "s|@DIFY_VERSION@|$([ "$SKIP_DIFY" = 1 ] && echo '未包含' || echo "$DIFY_VERSION")|g" \
    -e "s|@DIFY_SENTENCE@|$DIFY_SENTENCE|g" \
    -e "s|@DISK_GB@|$DISK_GB|g" \
    deploy/offline/BUNDLE-README.md > "$STAGE/README.md"

cat > "$STAGE/install-offline.sh" <<'EOS'
#!/usr/bin/env bash
# =============================================================================
# install-offline.sh —— 离线包根目录的一键入口（目标 Linux 机）
#
#   sudo bash install-offline.sh                 # 默认 standalone（自带 Neo4j+GraphDB）
#   sudo bash install-offline.sh --mode attached # 复用既有载体
#   sudo bash install-offline.sh --no-dify       # 只交付应用层 6 服务
#
# 等价于：sudo bash deploy/offline/install_offline.sh "$@"
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/deploy/offline/install_offline.sh" "$@"
EOS
chmod +x "$STAGE/install-offline.sh" 2>/dev/null || true
ok "README.md / install-offline.sh 已写"

# ── 收尾：删中间态，避免把排障产物打进发布物 ──────────────────────────────
rm -f "$STAGE/.image-list.txt" 2>/dev/null || true
rm -rf "$STAGE/deploy/.work" "$STAGE/deploy/.deps" "$STAGE/deploy/secrets" 2>/dev/null || true

# 明确的"绝不该出现"清单（除了凭据，还包括**填好的** Dify .env）
if [ -f "$STAGE/deploy/.env" ]; then
  die "包里出现 deploy/.env（填好的凭据）—— 拒绝出包。"
fi
if [ -f "$STAGE/vendor/dify/docker/.env" ]; then
  die "包里出现 vendor/dify/docker/.env（已生成口令）—— 拒绝出包；只应带 .env.offline-template。"
fi

# 全局兜底：除模板外，任何 .env*（含 .env.bak_* / .env.local / .env.save）都不许进包。
# ★ 第一版包正是漏在 `.env.bak_r59_...` 上：名字不是 `.env`，旧检查放行，212 个非空键入包。
ENV_LEAK="$(find "$STAGE" -type f -name '.env*' ! -name '.env.example' ! -name '.env.offline-template' -print 2>/dev/null || true)"
if [ -n "$ENV_LEAK" ]; then
  err "包内出现 .env 备份/填单（可能含真实口令），拒绝出包："
  printf '%s\n' "$ENV_LEAK" | sed 's/^/        /'
  die "请从源码树剔除这些文件（或确认它们只是模板并改名）后重跑。"
fi

# ── ⑦ MANIFEST.sha256（只记相对路径）──────────────────────────────────────
say ""
say "── ⑦ 计算 MANIFEST.sha256（全量，含镜像包；这一步要读一遍所有字节）──"
( cd "$STAGE" && find . -type f ! -name 'MANIFEST.sha256' | sort | sed 's#^\./##' > "$TMPD/manifest-files.txt" )
( cd "$STAGE" && while IFS= read -r mf; do sha256sum "$mf"; done < "$TMPD/manifest-files.txt" > MANIFEST.sha256 )
NMAN="$(wc -l < "$STAGE/MANIFEST.sha256" | tr -d ' ')"
ok "MANIFEST.sha256：$NMAN 个文件"

# ── ⑧ 自检（有 python3 就跑；没有则明确说明未跑，不静默跳过）──────────────
say ""
say "── ⑧ 离线包自检（check_offline_bundle.py）──"
if command -v python3 >/dev/null 2>&1; then
  python3 deploy/offline/check_offline_bundle.py --bundle "$STAGE" --hash \
    || die "离线包自检未通过 —— **拒绝交付**（见上方逐条结论）。"
  ok "离线包自检通过"
elif [ "$SKIP_SELFCHECK" = "1" ]; then
  warn "构建机没有 python3 ⇒ 按 --skip-selfcheck **显式跳过**包自检。"
  warn "  该包未经 check_offline_bundle.py 验证；目标机仍会做 MANIFEST + IMAGES.lock 校验。"
else
  die "构建机没有 python3 ⇒ 无法做离线包自检。请安装 python3，或显式加 --skip-selfcheck（自担风险）。"
fi

# ── ⑨ 可选归档 ─────────────────────────────────────────────────────────────
if [ "$DO_ARCHIVE" = "1" ]; then
  say ""
  say "── ⑨ 归档（tar.gz）──"
  ARCHIVE="$OUT/$NAME-$TAG.tar.gz"
  rm -f "$ARCHIVE"
  tar -czf "$ARCHIVE" -C "$OUT" "$NAME-$TAG" || die "tar 打包失败（磁盘空间？）"
  ok "归档：$ARCHIVE（$(du -h "$ARCHIVE" | cut -f1)）"
fi

# 清理中间目录（make_bundle.sh 的产物已经并进 STAGE；此处只兜底）
rm -rf "$BASE" 2>/dev/null || true

say ""
say "════════════════════════════════════════════════════════════════"
say " 全量离线包已生成：$STAGE"
say " 目录大小        ：$(du -sh "$STAGE" | cut -f1)"
say " 文件数          ：$NMAN"
say " 镜像数          ：$IMG_N"
say " 目标平台        ：$PLATFORM"
say " 目标机磁盘下限  ：${DISK_GB} GB"
say "════════════════════════════════════════════════════════════════"
say ""
say "下一步："
say "  1) 传输前自校验（在构建机）："
say "       ( cd $STAGE && sha256sum -c MANIFEST.sha256 >/dev/null ) && echo OK"
say "  2) 传输（示例）："
say "       rsync -av --progress $STAGE/ user@target:/opt/ccops/"
say "     U 盘 / FAT32（单文件 ≤4 GB）先切分："
say "       tar -C $OUT -czf - $NAME-$TAG | split -b 3G - $OUT/$NAME-$TAG.tar.gz.part-"
say "  3) 目标机（**无需联网**）："
say "       cd $NAME-$TAG && sha256sum -c MANIFEST.sha256 && sudo bash install-offline.sh"
say ""
