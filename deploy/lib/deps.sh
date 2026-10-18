#!/usr/bin/env bash
# =============================================================================
# deploy/lib/deps.sh —— 「外部环境依赖」体检与自动安装
#
# 本项目的镜像覆盖**应用层 6 个服务**（5 个镜像，其中 kgt 起两个实例：kgt :8303 与
# kgt-dyn :8305 补充臂）；下列三个组件属**安装环境依赖**，
# 由本脚本负责「先检查、缺了再从官方镜像源装」：
#
#   ① Neo4j 5-community   KG 侧知识载体       契约端口 7475(HTTP) / 7688(Bolt)
#   ② GraphDB 10.8.0      ONTO 侧知识载体     契约端口 7200
#   ③ Dify 1.17.0         编排层（15 容器）    契约端口 80
#
# 档位语义（**这是本脚本最容易误解的一处，先读**）：
#   · standalone 档：Neo4j/GraphDB 由 compose 叠加文件用**官方镜像**拉起 ——
#     这本身就是"自动安装"，故本脚本对这两个**只登记、不重复装**（避免双份容器）。
#   · attached  档：语义是"复用既有载体"。此时：
#       - 已存在（端口占用 + 探活通过）→ 登记 OK，什么都不做；
#       - 不存在且 CC_DEPS_MODE=install → 在宿主上用官方镜像补装（走契约端口），
#         应用容器经 host.docker.internal 访问，**无需**改 compose；
#       - 不存在且 CC_DEPS_MODE=check   → 判为受阻，并提示改用 standalone 档。
#
#   ★ 补装前置由 preflight 保证：载体端口被占用时 preflight 已 die，
#     所以这里**不可能**覆盖既有载体（targets.yaml#must_not 的硬约束因此成立）。
#
#   · Dify 在**两档下都是外部依赖**：它体量大（4–6 GB 内存、15 容器），
#     且 192.0.2.10 现网已装（内存仅余 ~5 GB，不得新增常驻容器）⇒
#     本脚本带**内存硬闸**：余量不足时拒绝自动安装并判受阻，绝不硬闯。
# =============================================================================

# ── 官方镜像坐标（可用 .env 覆盖）────────────────────────────────────────────
CC_NEO4J_IMAGE_DEFAULT="neo4j:5-community"
CC_GRAPHDB_IMAGE_DEFAULT="ontotext/graphdb:10.8.0"
CC_DIFY_VERSION_DEFAULT="1.17.0"

# 收集体检结论：missing 列表交给 install.sh 决定是否致命
DEPS_MISSING=""

_dep_record_missing() { DEPS_MISSING="$DEPS_MISSING $1"; }

# ── 内存余量（MB）：用于 Dify 的硬闸 ─────────────────────────────────────────
_mem_available_mb() {
  [ -r /proc/meminfo ] || { printf '0'; return; }
  awk '/^MemAvailable:/{printf "%d", $2/1024}' /proc/meminfo
}

# ─────────────────────────────────────────────────────────────────────────────
# ① Neo4j
# ─────────────────────────────────────────────────────────────────────────────
_dep_neo4j_url_host() { _to_host_url "$(env_get "$1" CC_NEO4J_HTTP_URL 'http://127.0.0.1:7475')"; }

dep_neo4j_check() {
  local ddir="$1" mode="${2:-attached}" url
  # ★ standalone 档：载体由 compose 在本机拉起 ⇒ 必须看**本机契约端口**，
  #   而不是 .env 里指向别处的 CC_NEO4J_HTTP_URL。
  #   （实测踩过：standalone 档下探到的是 192.0.2.10 的远程 Neo4j，于是报
  #     "① Neo4j 已存在且可达：http://192.0.2.10:7475" —— 既误导，
  #     又让 install.sh 的"载体缺失"闸门被绕过。）
  if [ "$mode" = "standalone" ]; then
    url="http://127.0.0.1:7475"
  else
    url="$(_dep_neo4j_url_host "$ddir/.env")"
  fi
  # 内容感知：Neo4j HTTP 根返回 JSON 且带 neo4j_version。
  # 只看"有响应"会被同端口上的别的服务骗过（见 common.sh 的 _json_has_key 说明）。
  if _json_has_key "$url/" neo4j_version 5; then
    local ver; ver="$(_http_get "$url/" 5 | tr -d '\n' | sed -n 's/.*"neo4j_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    ok "① Neo4j 已存在且可达：$url ${ver:+（版本 $ver）}"
    return 0
  fi
  warn "① Neo4j 不可达或端口上是别的服务：$url/"
  return 1
}

dep_neo4j_install() {
  local ddir="$1" envf="$ddir/.env"
  local img pass
  img="$(env_get "$envf" CC_NEO4J_IMAGE "$CC_NEO4J_IMAGE_DEFAULT")"
  pass="$(env_get "$envf" CC_NEO4J_PASSWORD '')"
  if [ -z "$pass" ]; then
    err "  想补装 Neo4j，但 CC_NEO4J_PASSWORD 为空 —— 本项目**绝不起免密图库**。"
    _dep_record_missing neo4j
    return 1
  fi
  info "  补装 Neo4j：$img（契约端口 7475/7688）"
  # 端口占用已在 preflight 拦下；这里再断言一次，防御"绕过 install.sh 直接调用"
  if port_busy 7475 || port_busy 7688; then
    err "  :7475/:7688 已被占用 ⇒ 拒绝补装（可能已有 Neo4j；新建第二个被 targets.yaml 明令禁止）"
    _dep_record_missing neo4j
    return 1
  fi
  docker run -d \
    --name cc-ops-neo4j \
    --restart unless-stopped \
    -p 7475:7474 -p 7688:7687 \
    -e "NEO4J_AUTH=neo4j/${pass}" \
    -e "NEO4J_server_memory_heap_max__size=$(env_get "$envf" CC_NEO4J_HEAP '1G')" \
    -e "NEO4J_server_memory_pagecache_size=$(env_get "$envf" CC_NEO4J_PAGECACHE '512m')" \
    -v ccops-neo4j-data:/data \
    "$img" >/dev/null || { err "  docker run 失败（镜像拉取不到？）"; _dep_record_missing neo4j; return 1; }
  wait_http "$(_dep_neo4j_url_host "$envf")/" 180 "Neo4j" || { _dep_record_missing neo4j; return 1; }
}

# ─────────────────────────────────────────────────────────────────────────────
# ② GraphDB
# ─────────────────────────────────────────────────────────────────────────────
_dep_graphdb_url_host() {
  local u; u="$(env_get "$1" CC_GRAPHDB_URL 'http://127.0.0.1:7200')"
  _to_host_url "$u" | sed -e 's#/*$##'
}

dep_graphdb_check() {
  local ddir="$1" mode="${2:-attached}" url
  if [ "$mode" = "standalone" ]; then
    url="http://127.0.0.1:7200"
  else
    url="$(_dep_graphdb_url_host "$ddir/.env")"
  fi
  # 内容感知：GraphDB /rest/repositories 返回 **JSON 数组**。
  # 只看"有响应"会被同端口上的别的服务骗过（本项目实测踩过：:8080 上的 GitLab
  # 对任意路径回 302 到登录页，非空 body ⇒ 被判成"组件已存在"）。
  if _json_is_array "$url/rest/repositories" 5; then
    local n; n="$(_http_get "$url/rest/repositories" 5 | grep -o '"id"' | wc -l | tr -d ' ')"
    ok "② GraphDB 已存在且可达：$url（仓库数 $n）"
    return 0
  fi
  warn "② GraphDB 不可达或端口上是别的服务：$url/rest/repositories"
  return 1
}

dep_graphdb_install() {
  local ddir="$1" envf="$ddir/.env" img
  img="$(env_get "$envf" CC_GRAPHDB_IMAGE "$CC_GRAPHDB_IMAGE_DEFAULT")"
  info "  补装 GraphDB：$img（契约端口 7200）"
  if port_busy 7200; then
    err "  :7200 已被占用 ⇒ 拒绝补装（既有 gdo* / starwars 仓库不得改写）"
    _dep_record_missing graphdb
    return 1
  fi
  docker run -d \
    --name cc-ops-graphdb \
    --restart unless-stopped \
    -p 7200:7200 \
    -e "GDB_JAVA_OPTS=-Xms512m -Xmx$(env_get "$envf" CC_GRAPHDB_HEAP '4g')" \
    -v ccops-graphdb-data:/opt/graphdb/home \
    "$img" >/dev/null || { err "  docker run 失败（镜像拉取不到？）"; _dep_record_missing graphdb; return 1; }
  wait_http "$(_dep_graphdb_url_host "$envf")/rest/repositories" 240 "GraphDB" || { _dep_record_missing graphdb; return 1; }
}

# ─────────────────────────────────────────────────────────────────────────────
# ③ Dify（两档都是外部依赖；带内存硬闸）
# ─────────────────────────────────────────────────────────────────────────────
_dep_dify_url() { printf '%s' "$(env_get "$1" DIFY_BASE_URL '')" | sed -e 's#/*$##'; }

dep_dify_check() {
  local ddir="$1" url; url="$(_dep_dify_url "$ddir/.env")"
  if [ -z "$url" ]; then
    warn "③ DIFY_BASE_URL 未配置 ⇒ 编排层状态未知（本项目不把它算作起栈前置）"
    return 1
  fi
  # ★ 内容感知（本节最重要的一处加固）：Dify 的 /console/api/setup 返回
  #   **JSON 且含 `step`**（not_started / finished）。
  #   实测踩过：联调机 :8080 上跑的是 **GitLab 的 puma**，它对
  #   /console/api/setup 回 **302 → /users/sign_in**（HTML）。
  #   旧实现用 `http_ok`（curl -f 只在 ≥400 失败）⇒ 302 + 非空 body ⇒ 判"可达"
  #   ⇒ 报出 **"③ Dify 已存在且可达"** 的假阳。**被别的服务骗过的探针比没有探针更糟**，
  #   因为它会让"没装"看起来像"装好了"，直接跳过安装。
  if _json_has_key "$url/console/api/setup" step 8; then
    local step; step="$(_http_get "$url/console/api/setup" 8 | tr -d '\n' | sed -n 's/.*"step"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    ok "③ Dify 已存在且可达：$url（setup step=${step:-?}）"
    return 0
  fi
  warn "③ Dify 不可达，或端口上是别的服务（不是 Dify 的 setup JSON）：$url"
  return 1
}

# ── Dify 对外端口：默认 80/443，被占用则必须让路 ─────────────────────────────
# ★ 这是真实环境的常态：目标机上 :80 往往已经被别的东西占了
#   （本项目的联调机 192.0.2.11 上就是 GitLab 的 nginx 占着 :80）。
#   Dify 官方编排把 nginx 发布在 EXPOSE_NGINX_PORT（默认 80）
#   ⇒ 不让路的话 `docker compose up` 会直接端口冲突失败。
_dep_dify_pick_ports() {
  local envf="$1" http https i
  http="$(env_get "$envf" CC_DIFY_HTTP_PORT '')"
  https="$(env_get "$envf" CC_DIFY_HTTPS_PORT '')"

  if [ -z "$http" ]; then
    if port_busy 80; then
      http=8080
      i=0
      while port_busy "$http" && [ "$i" -lt 40 ]; do http=$((http + 1)); i=$((i + 1)); done
      warn "  :80 已被占用 ⇒ Dify 对外 HTTP 端口自动改用 :$http"
      warn "    （占用者：$(port_owner 80 2>/dev/null | head -n1 | cut -c1-90)）"
    else
      http=80
    fi
  fi

  if [ -z "$https" ]; then
    if port_busy 443; then
      https=8443
      i=0
      while port_busy "$https" && [ "$i" -lt 40 ]; do https=$((https + 1)); i=$((i + 1)); done
      warn "  :443 已被占用 ⇒ Dify 对外 HTTPS 端口自动改用 :$https"
    else
      https=443
    fi
  fi
  printf '%s %s' "$http" "$https"
}

# ── Dify 首次初始化：建管理员账号 ───────────────────────────────────────────
# ★ 为什么必须做：Dify 起来后是**未初始化**状态（没有管理员、没有工作区）。
#   不初始化的话，本项目的 register_tools.py / deploy_agents.py
#   **连登录都做不到** ⇒ "装好了"其实是"装了个不能用"。
#   ⇒ 这一条就是"完成配置"的实质内容。
#   Dify 的初始化是两步式：GET /console/api/setup 看 step；未开始则 POST 同一路径。
dep_dify_init_admin() {
  local base="$1" email="$2" pass="$3" name="${4:-admin}"
  if [ -z "$email" ] || [ -z "$pass" ]; then
    warn "  跳过 Dify 管理员初始化：未提供 DIFY_EMAIL / DIFY_PASSWORD"
    warn "    ⇒ Dify 已起但**未初始化**，后续 register_tools.py 无法登录。"
    warn "    ⇒ 请补 .env 后重跑，或在浏览器打开 Dify 走一次初始化向导。"
    return 1
  fi

  local st body
  st="$(curl -sS -o /tmp/_dify_setup.json -w '%{http_code}' --max-time 15 \
        "$base/console/api/setup" 2>/dev/null || echo 000)"
  body="$(head -c 300 /tmp/_dify_setup.json 2>/dev/null | tr -d '\n')"
  if [ "$st" != "200" ]; then
    warn "  GET /console/api/setup → HTTP $st（无法判定初始化状态）"
    return 1
  fi
  case "$body" in
    *finished*)
      ok "  Dify 已完成初始化（setup step=finished）"
      return 0 ;;
    *not_started*|*"step"*)
      info "  Dify 未初始化（$body）⇒ 创建管理员账号"
      ;;
  esac

  # POST /console/api/setup 建管理员。口令来自 .env，**不打印**。
  local payload
  payload="$(printf '{"email":"%s","name":"%s","password":"%s"}' "$email" "$name" "$pass")"
  st="$(curl -sS -o /tmp/_dify_init.json -w '%{http_code}' --max-time 60 \
        -X POST "$base/console/api/setup" \
        -H 'Content-Type: application/json' \
        --data "$payload" 2>/dev/null || echo 000)"
  local rb; rb="$(head -c 300 /tmp/_dify_init.json 2>/dev/null | tr -d '\n')"
  if [ "$st" = "201" ] || [ "$st" = "200" ]; then
    ok "  Dify 管理员已创建（$email），初始化完成"
    return 0
  fi
  # 已初始化过会返回 400/409 之类；那不必然是故障，再核一次状态
  if http_ok "$base/console/api/setup" 5 && grep -q finished /tmp/_dify_init.json 2>/dev/null; then
    ok "  Dify 已完成初始化"
    return 0
  fi
  err "  Dify 管理员初始化失败：HTTP $st  $rb"
  warn "    ⇒ Dify 已起但未初始化；请打开 $base 走一次初始化向导，或核对 DIFY_EMAIL/DIFY_PASSWORD"
  return 1
}

# ── Dify 对外端口：默认 80/443，被占用则必须让路 ─────────────────────────────
# ★ 这是真实环境的常态：目标机上 :80 往往已经被别的东西占了
#   （本项目的联调机 192.0.2.11 上就是 GitLab 的 nginx 占着 :80）。
#   Dify 官方编排把 nginx 发布在 EXPOSE_NGINX_PORT（默认 80）
#   ⇒ 不让路的话 `docker compose up` 会直接端口冲突失败。
_dep_dify_pick_ports() {
  local envf="$1" http https i
  http="$(env_get "$envf" CC_DIFY_HTTP_PORT '')"
  https="$(env_get "$envf" CC_DIFY_HTTPS_PORT '')"

  if [ -z "$http" ]; then
    if port_busy 80; then
      http=8080
      i=0
      while port_busy "$http" && [ "$i" -lt 40 ]; do http=$((http + 1)); i=$((i + 1)); done
      warn "  :80 已被占用 ⇒ Dify 对外 HTTP 端口自动改用 :$http"
      warn "    （占用者：$(port_owner 80 2>/dev/null | head -n1 | cut -c1-90)）"
    else
      http=80
    fi
  fi

  if [ -z "$https" ]; then
    if port_busy 443; then
      https=8443
      i=0
      while port_busy "$https" && [ "$i" -lt 40 ]; do https=$((https + 1)); i=$((i + 1)); done
      warn "  :443 已被占用 ⇒ Dify 对外 HTTPS 端口自动改用 :$https"
    else
      https=443
    fi
  fi
  printf '%s %s' "$http" "$https"
}

# ── Dify 首次初始化：建管理员账号 ───────────────────────────────────────────
# ★ 为什么必须做：Dify 起来后是**未初始化**状态（没有管理员、没有工作区）。
#   不初始化的话，本项目的 register_tools.py / deploy_agents.py
#   **连登录都做不到** ⇒ "装好了"其实是"装了个不能用"。
#   ⇒ 这一条就是"完成配置"的实质内容。
#   Dify 的初始化是两步式：GET /console/api/setup 看 step；未开始则 POST 同一路径。
dep_dify_init_admin() {
  local base="$1" email="$2" pass="$3" name="${4:-admin}"
  if [ -z "$email" ] || [ -z "$pass" ]; then
    warn "  跳过 Dify 管理员初始化：未提供 DIFY_EMAIL / DIFY_PASSWORD"
    warn "    ⇒ Dify 已起但**未初始化**，后续 register_tools.py 无法登录。"
    warn "    ⇒ 请补 .env 后重跑，或在浏览器打开 Dify 走一次初始化向导。"
    return 1
  fi

  local st body
  st="$(curl -sS -o /tmp/_dify_setup.json -w '%{http_code}' --max-time 15 \
        "$base/console/api/setup" 2>/dev/null || echo 000)"
  body="$(head -c 300 /tmp/_dify_setup.json 2>/dev/null | tr -d '\n')"
  if [ "$st" != "200" ]; then
    warn "  GET /console/api/setup → HTTP $st（无法判定初始化状态）"
    return 1
  fi
  case "$body" in
    *finished*)
      ok "  Dify 已完成初始化（setup step=finished）"
      return 0 ;;
    *not_started*|*"step"*)
      info "  Dify 未初始化（$body）⇒ 创建管理员账号"
      ;;
  esac

  # POST /console/api/setup 建管理员。口令来自 .env，**不打印**。
  local payload
  payload="$(printf '{"email":"%s","name":"%s","password":"%s"}' "$email" "$name" "$pass")"
  st="$(curl -sS -o /tmp/_dify_init.json -w '%{http_code}' --max-time 60 \
        -X POST "$base/console/api/setup" \
        -H 'Content-Type: application/json' \
        --data "$payload" 2>/dev/null || echo 000)"
  local rb; rb="$(head -c 300 /tmp/_dify_init.json 2>/dev/null | tr -d '\n')"
  if [ "$st" = "201" ] || [ "$st" = "200" ]; then
    ok "  Dify 管理员已创建（$email），初始化完成"
    return 0
  fi
  # 已初始化过会返回 400/409 之类；那不必然是故障，再核一次状态
  if http_ok "$base/console/api/setup" 5 && grep -q finished /tmp/_dify_init.json 2>/dev/null; then
    ok "  Dify 已完成初始化"
    return 0
  fi
  err "  Dify 管理员初始化失败：HTTP $st  $rb"
  warn "    ⇒ Dify 已起但未初始化；请打开 $base 走一次初始化向导，或核对 DIFY_EMAIL/DIFY_PASSWORD"
  return 1
}

dep_dify_install() {
  local ddir="$1" envf="$ddir/.env" ver src dir avail
  ver="$(env_get "$envf" CC_DIFY_VERSION "$CC_DIFY_VERSION_DEFAULT")"
  src="$(env_get "$envf" CC_DIFY_SOURCE '')"
  dir="$(env_get "$envf" CC_DIFY_DIR "$ddir/../.deps/dify")"

  avail="$(_mem_available_mb)"
  if [ "$avail" -gt 0 ] && [ "$avail" -lt "$(env_get "$envf" CC_DIFY_MIN_MEM_MB '6000')" ]; then
    err "  拒绝自动安装 Dify：宿主可用内存仅 ${avail} MB，低于阈值 $(env_get "$envf" CC_DIFY_MIN_MEM_MB '6000') MB。"
    warn "    Dify 1.17.0 是 15 个容器、约需 4–6 GB —— 内存不足时装上去会连带把既有服务拖垮。"
    warn "    192.0.2.10 现网即属此类（内存仅余 ~5 GB，裁决 13 明令不得新增常驻容器）⇒ 应复用既有 Dify。"
    _dep_record_missing dify
    return 1
  fi

  if [ -n "$src" ]; then
    info "  使用 CC_DIFY_SOURCE 指定的来源：$src"
    warn "  自定义来源不做自动编排（结构未知）⇒ 请按该来源自带说明启动后重跑本脚本"
    _dep_record_missing dify
    return 1
  fi
  if [ "${CC_OFFLINE:-0}" = "1" ]; then
    # ── 离线：Dify 的源码树必须**随包携带**（目标机断网，git clone 必然失败）──
    if [ ! -d "$dir/docker" ]; then
      err "  离线模式要求 Dify 源码树随包携带，但 $dir/docker 不存在。"
      warn "    ⇒ 本包应由 deploy/offline/make_offline_bundle.sh 生成；"
      warn "      若只想交付应用层，请用 install-offline.sh --no-dify（CC_SKIP_DIFY=1）。"
      _dep_record_missing dify
      return 1
    fi
    info "  使用离线包内置 Dify 源码树：$dir（不 clone、不联网）"
  else
    if ! command -v git >/dev/null 2>&1; then
      err "  宿主上没有 git ⇒ 无法自动拉取 Dify 官方编排"
      warn "  两条出路：① 装 git；② 手工部署 Dify 后在 .env 里填 DIFY_BASE_URL（推荐内网镜像源）"
      _dep_record_missing dify
      return 1
    fi

    info "  补装 Dify $ver：git clone 官方仓库 → 配置端口 → docker compose up -d"
    # ★ 判据从 `.git` 改成 `docker/`：用 tarball 解压出来的源码树**没有 .git**，
    #   旧实现会往这个非空目录里再 clone 一次（报"目录非空"，看不出是判断错了）。
    if [ ! -d "$dir/docker" ]; then
      mkdir -p "$(dirname "$dir")"
      git clone --depth 1 --branch "$ver" \
        "$(env_get "$envf" CC_DIFY_GIT_URL 'https://github.com/langgenius/dify.git')" \
        "$dir" || { err "  git clone 失败（网络/镜像源不可达）"; _dep_record_missing dify; return 1; }
    else
      info "  已存在 Dify 源码树：$dir（跳过 clone）"
    fi
  fi

  if [ ! -d "$dir/docker" ]; then
    err "  源码树里没有 docker/ 目录（$dir）—— Dify $ver 的目录结构可能已变"
    _dep_record_missing dify
    return 1
  fi

  # ── 端口让路：写进 Dify 自己的 docker/.env ──
  local ports http https
  ports="$(_dep_dify_pick_ports "$envf")"
  http="${ports%% *}"; https="${ports##* }"
  info "  Dify 对外端口：HTTP=$http  HTTPS=$https"

  ( cd "$dir/docker" || exit 1
    # ★ 离线包只带 .env.offline-template（含 __CC_GENERATE__ 占位符），安装时生成 .env。
    #   这里的兜底顺序：已有 .env → 模板 → 上游 .env.example（最后这条只是为了"能起来"，
    #   占位符未替换时会显式告警，不静默）。
    if [ ! -f .env ]; then
      if [ -f .env.offline-template ]; then
        cp .env.offline-template .env || exit 1
        chmod 600 .env 2>/dev/null || true
      else
        cp .env.example .env
        warn "  缺 .env.offline-template ⇒ 用上游 .env.example 兜底（口令/密钥为上游默认值，不安全）"
      fi
    fi
    if grep -q '__CC_GENERATE__' .env 2>/dev/null; then
      if command -v _offline_generate_placeholders >/dev/null 2>&1; then
        _offline_generate_placeholders .env || warn "  占位符替换失败（随机源不可用）"
      else
        warn "  .env 里仍有 __CC_GENERATE__ 占位符，且 offline.sh 未加载 ⇒ 口令未生成"
      fi
    fi
    # 断网环境必须**强制**关掉插件市场（append-only 不够：模板可能写着 true）
    if [ "${CC_OFFLINE:-0}" = "1" ]; then
      if grep -qE '^[[:space:]]*MARKETPLACE_ENABLED[[:space:]]*=' .env; then
        sed -i -E 's|^[[:space:]]*MARKETPLACE_ENABLED[[:space:]]*=.*|MARKETPLACE_ENABLED=false|' .env
      else
        printf 'MARKETPLACE_ENABLED=false\n' >> .env
      fi
    fi
    # sed 原地改；键不存在则追加（不同版本键名/有无不同）
    _set() {
      if grep -qE "^[#[:space:]]*$1=" .env; then
        sed -i -E "s|^[#[:space:]]*$1=.*|$1=$2|" .env
      else
        printf '%s=%s\n' "$1" "$2" >> .env
      fi
    }
    _set EXPOSE_NGINX_PORT "$http"
    _set EXPOSE_NGINX_SSL_PORT "$https"
    # 让 Dify 自述的对外地址与端口一致（否则控制台里生成的链接会指向 80）
    pub="$(env_get "$envf" CC_DIFY_PUBLIC_HOST '')"
    if [ -n "$pub" ]; then
      _set CONSOLE_API_URL "http://${pub}:${http}"
      _set CONSOLE_WEB_URL "http://${pub}:${http}"
      _set SERVICE_API_URL "http://${pub}:${http}"
      _set APP_WEB_URL   "http://${pub}:${http}"
      _set FILES_URL     "http://${pub}:${http}"
    fi
    grep -nE '^(EXPOSE_NGINX_PORT|EXPOSE_NGINX_SSL_PORT|CONSOLE_API_URL)=' .env || true
  ) || { err "  写 Dify docker/.env 失败"; _dep_record_missing dify; return 1; }

  # ── 起 Dify ──
  # ★ 离线模式一律 --pull never：镜像已在 ② 从包里载入，目标机断网时 pull 只会卡超时。
  local up_pull=""
  if [ "${CC_OFFLINE:-0}" = "1" ]; then
    up_pull="--pull never"
  fi
  ( cd "$dir/docker" && docker compose up -d $up_pull ) \
    || { err "  Dify compose 启动失败。诊断：cd $dir/docker && docker compose logs --tail=60";
         _dep_record_missing dify; return 1; }

  local base="http://127.0.0.1:${http}"
  # 首次启动要跑数据库迁移，冷盘上 600 s 不够（离线包场景常见）；可用 CC_DIFY_WAIT_S 调。
  wait_http "$base/console/api/setup" "$(env_get "$envf" CC_DIFY_WAIT_S '900')" "Dify（:$http）" \
    || { _dep_record_missing dify; return 1; }

  # ── 初始化管理员（"完成配置"的实质）──
  dep_dify_init_admin "$base" \
    "$(env_get "$envf" DIFY_EMAIL '')" \
    "$(env_get "$envf" DIFY_PASSWORD '')" \
    "$(env_get "$envf" CC_DIFY_ADMIN_NAME 'admin')" || true

  # 若 .env 里的 DIFY_BASE_URL 为空或还指向别处，提示应指向本机新装的这个
  local cur; cur="$(_dep_dify_url "$envf")"
  if [ "$cur" != "$base" ]; then
    warn "  .env 的 DIFY_BASE_URL=$cur ≠ 新装实例 $base"
    warn "    ⇒ 若要用这台新装的 Dify，请把 DIFY_BASE_URL 改成 $base（或 http://<本机IP>:${http}）"
  fi
  ok "  Dify 安装与初始化完成：$base"
}


# ─────────────────────────────────────────────────────────────────────────────
# 汇总入口
# ─────────────────────────────────────────────────────────────────────────────
deps_run() {
  local ddir="$1" mode="$2" action="$3" phase="${4:-pre}"   # phase = pre | post
  DEPS_MISSING=""

  if [ "$phase" = "post" ]; then
    # ── 后置阶段：只补装 Dify（可选、重量级）─────────────────────────────
    if [ "${CC_SKIP_DIFY:-0}" = "1" ]; then
      skip "按 CC_SKIP_DIFY=1 跳过编排层（Dify 由部署方复用既有实例）"
      return 0
    fi
    # ★ 为什么要分两阶段：Dify 1.17.0 是 15 个镜像、通常 2–6 GB。
    #   早先把它的安装放在**应用层起栈之前**，结果是
    #   **一个"不阻塞起栈"的依赖把整个栈卡住了**（实测：Dify 镜像下载慢，
    #   载体与应用层迟迟不起，看起来像"部署卡死"）。
    #   ⇒ 前置阶段只做**体检**；Dify 的安装挪到应用层起来并验收之后。
    #   这样"缺 Dify 不阻塞应用层"这句话在**实现上**也成立，而不只是写在注释里。
    if dep_dify_check "$ddir"; then
      return 0
    fi
    if [ "${CC_OFFLINE:-0}" = "1" ]; then
      hdr "⑧ 启动离线包内置的编排层（Dify）—— 可选、重量级，**前面已经交付的部分不受影响**"
    else
      hdr "⑧ 补装编排层（Dify）—— 可选、重量级，**前面已经交付的部分不受影响**"
    fi
    if [ "$action" != "install" ]; then
      warn "  CC_DEPS_MODE=$action ⇒ 只体检不安装。Dify 不可用，仅影响编排层。"
      _dep_record_missing dify
      return 0
    fi
    dep_dify_install "$ddir" || true
    return 0
  fi

  # ── 前置阶段：体检三项；载体（attached 档）缺失则补装；**不装 Dify** ──
  hdr "③ 外部环境依赖体检（Neo4j / GraphDB / Dify）"
  info "档位=$mode　策略=$action"
  if [ "$mode" = "standalone" ]; then
    info "standalone 档：Neo4j/GraphDB 由 compose 叠加文件用官方镜像拉起，本步只登记不重复装"
  fi

  # ── Neo4j ──
  if ! dep_neo4j_check "$ddir" "$mode"; then
    if [ "$mode" = "standalone" ]; then
      info "  → 交由 compose.standalone.yml 安装（官方镜像 $CC_NEO4J_IMAGE_DEFAULT）"
    elif [ "$action" = "install" ]; then
      dep_neo4j_install "$ddir" || true
    else
      _dep_record_missing neo4j
    fi
  fi

  # ── GraphDB ──
  if ! dep_graphdb_check "$ddir" "$mode"; then
    if [ "$mode" = "standalone" ]; then
      info "  → 交由 compose.standalone.yml 安装（官方镜像 $CC_GRAPHDB_IMAGE_DEFAULT）"
    elif [ "$action" = "install" ]; then
      dep_graphdb_install "$ddir" || true
    else
      _dep_record_missing graphdb
    fi
  fi

  # ── Dify：**前置阶段只体检，安装留到 ⑧**（见上）──
  if ! dep_dify_check "$ddir"; then
    info "  → Dify 的安装挪到 ⑧（应用层起来并验收之后），避免拖住核心交付"
    _dep_record_missing dify
  fi

  if [ -n "$DEPS_MISSING" ]; then
    warn "仍缺失的外部依赖：$DEPS_MISSING"
    warn "  （Dify 缺失**不阻塞**应用层起栈与诊断能力，只影响编排层；Neo4j/GraphDB 缺失会阻断对应方案）"
  else
    ok "③ 外部依赖体检完成：三项均可用"
  fi
}

# 供 install.sh 判定：Neo4j / GraphDB 缺失是否致命
deps_carrier_missing() {
  case " $DEPS_MISSING " in
    *" neo4j "*|*" graphdb "*) return 0 ;;
    *) return 1 ;;
  esac
}
