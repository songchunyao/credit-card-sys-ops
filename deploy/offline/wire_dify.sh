#!/usr/bin/env bash
# =============================================================================
# deploy/offline/wire_dify.sh —— 离线安装后的 **Dify 工具面注册**（可选步骤）
#
# 为什么单独一个脚本
# ------------------
#   install.sh 的职责止于"栈起来了 + 服务健康 + 验收过"。把 :8301–:8305 注册成
#   Dify 的 API tool provider 属于**编排层配置**，它依赖 Dify 已经起来、并且
#   部署者已经配好模型供应商；把它塞进 install.sh 会让"应用层交付"被一个
#   非阻塞依赖卡住（这正是本项目 earlier 踩过的坑）。
#
# 为什么用容器跑
# --------------
#   目标机可能只有一个裸 python3（没有 PyYAML）。而这些脚本需要 PyYAML。
#   离线包里**必然**有 `cc-ops/live-facts` 镜像 —— 它基于 python:3.12-slim 且
#   已装 PyYAML ⇒ 直接借它当运行时，**不需要在目标机装任何 pip 包**。
#
# 安全
# ----
#   口令只从 deploy/.env 读，通过**环境变量**传给容器（`-e DIFY_PASSWORD` 不带值），
#   不会出现在 `docker run` 的命令行参数里，也不会被打印。
#
# 用法
# ----
#   sudo bash deploy/offline/wire_dify.sh --check    # 只读比对（默认）
#   sudo bash deploy/offline/wire_dify.sh --apply    # 幂等写入，再回读
#
# 退出码：0=PASS（零漂移）　1=FAIL（有漂移/写入失败）　2=BLOCKED（环境未就绪）
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(cd "$HERE/../.." && pwd)"
ENVF="$BUNDLE/deploy/.env"

# shellcheck source=../lib/common.sh
. "$BUNDLE/deploy/lib/common.sh"

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'; exit 0; }

ACTION="check"
while [ $# -gt 0 ]; do
  case "$1" in
    --check) ACTION="check"; shift ;;
    --apply) ACTION="apply"; shift ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

[ -f "$ENVF" ] || blocked "找不到 $ENVF —— 请先跑 install-offline.sh。"
require_cmd docker
need_root_or_docker_group

BASE="$(env_get "$ENVF" DIFY_BASE_URL '')"
MAIL="$(env_get "$ENVF" DIFY_EMAIL '')"
PASS="$(env_get "$ENVF" DIFY_PASSWORD '')"
PUBH="$(env_get "$ENVF" CC_DIFY_PUBLIC_HOST '')"
TAG="$(env_get "$ENVF" CC_IMAGE_TAG latest)"
IMG="cc-ops/live-facts:${TAG}"

[ -n "$BASE" ] || blocked "deploy/.env 的 DIFY_BASE_URL 为空 ⇒ 无法判断 Dify 在哪（先跑 install-offline.sh）。"
[ -n "$MAIL" ] && [ -n "$PASS" ] \
  || blocked "DIFY_EMAIL / DIFY_PASSWORD 为空 ⇒ 无法登录 Dify 控制台（先跑 install-offline.sh）。"
docker image inspect "$IMG" >/dev/null 2>&1 \
  || blocked "本地没有 $IMG —— 请先完成 install-offline.sh 的镜像载入。"

# ── Dify 就绪（内容感知：必须是 Dify 的 setup JSON，不是别的服务的登录页）────
if ! _json_has_key "$BASE/console/api/setup" step 8; then
  blocked "$BASE 不是 Dify（或未就绪）。若 Dify 刚起，等 1–2 分钟再试；也可 --no-dify 只用应用层。"
fi
ok "Dify 可达：$BASE"

if [ -z "$PUBH" ]; then
  warn "CC_DIFY_PUBLIC_HOST 为空：注册后的 server_url 会沿用 binding.yaml 里的主机。"
  warn "  在**本机部署**时那通常是错的（Dify 容器里 127.0.0.1 指它自己）。"
  warn "  建议在 deploy/.env 填本机可达 IP 后重跑 install-offline.sh（会自动带上）。"
fi

# ── 容器运行时：借 live-facts 镜像的 python3 + PyYAML ──────────────────────
export DIFY_BASE_URL="$BASE"
export DIFY_EMAIL="$MAIL"
export DIFY_PASSWORD="$PASS"
export CC_DIFY_PUBLIC_HOST="$PUBH"

run_py() {
  # --user：容器默认 UID 10001 读不到属主为 root、权限 700 的解包目录；
  #   以调用者身份运行可避免"包就在那儿但容器看不见"的假故障（挂载是只读的）。
  docker run --rm --network host --user "$(id -u):$(id -g)" \
    -e PYTHONIOENCODING=utf-8 \
    -e DIFY_BASE_URL \
    -e DIFY_EMAIL \
    -e DIFY_PASSWORD \
    -e CC_DIFY_PUBLIC_HOST \
    -v "$BUNDLE:/repo:ro" \
    -w /repo \
    "$IMG" python -B "$@"
}

hdr "Dify 工具面注册（$ACTION）"
info "运行时：$IMG（容器内 python3，无需在目标机装 pip 包）"

RC=0
if [ "$ACTION" = "apply" ]; then
  info "① register_tools.py --apply（只增不删；写后回读逐字比对）"
  if run_py shared/deploy/dify/register_tools.py --apply; then
    ok "provider 注册/收敛完成"
  else
    err "register_tools.py --apply 失败（见上方输出）"
    RC=1
  fi
else
  info "① register_tools.py --check（只读）"
fi

info "② register_tools.py --check（零漂移判据）"
if run_py shared/deploy/dify/register_tools.py --check; then
  ok "注册面零漂移"
else
  err "注册面有漂移（$([ "$ACTION" = "check" ] && echo '若尚未注册过，这是预期的：改用 --apply' || echo '写入后仍有漂移，需人工看')）"
  RC=1
fi

info "③ sync_tool_schemas.py --check（独立第三方口径，只读）"
if run_py shared/deploy/dify/sync_tool_schemas.py --check; then
  ok "第三方口径一致"
else
  err "第三方口径不一致（见上方 S1–S7 逐条）"
  RC=1
fi

hdr "结论"
if [ "$RC" = "0" ]; then
  ok "Dify 工具面已收敛（provider 注册 + 两套独立比对均通过）。"
else
  err "工具面未收敛（见上）。**不得**据此宣称编排层可用。"
fi

cat <<'NEXT'

下一步（需要 Dify 里已配好模型供应商）：
  · 创建/收敛 Agent（binding.yaml 的 apps 段，共 8 个）：见 deploy/offline/README.md §7.3 的完整命令。
    它同样借 live-facts 镜像当运行时，**不需要**在目标机装 PyYAML。
  · 只验证不写：把那条命令的 `--apply` 换成 `--check`。
NEXT

exit "$RC"
