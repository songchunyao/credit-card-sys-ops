#!/usr/bin/env bash
# =============================================================================
# deploy/offline/install_offline.sh —— 全量离线包的一键安装入口（目标机运行）
#
# 它**不做**实际部署，只做三件事：
#   ① 确认自己在离线包里（不是被单独拷出来的一个脚本）；
#   ② 把用户参数翻译成 deploy/install.sh 的 `--offline` 调用；
#   ③ 结束后打印**离线特有的**下一步（Dify 控制台、工具注册、剩余人工项）。
#
# 真正的编排仍在 deploy/install.sh（同一套预检/契约/验收，不另起炉灶）：
#   预检 → 载入全部镜像 → 载体体检 → 起自带载体 → 契约装载 → 起栈 → 等健康
#   → 验收矩阵 → 用包内源码树起 Dify（--pull never）
#
# 用法
# ----
#   sudo bash deploy/offline/install_offline.sh
#   sudo bash deploy/offline/install_offline.sh --mode attached
#   sudo bash deploy/offline/install_offline.sh --no-dify      # 复用既有 Dify / 只要应用层
#   sudo bash deploy/offline/install_offline.sh --skip-verify
#
# 退出码：0=PASS　1=FAIL　2=BLOCKED（沿用项目四态语义）
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(cd "$HERE/../.." && pwd)"

# shellcheck source=../lib/common.sh
. "$BUNDLE/deploy/lib/common.sh"

usage() { sed -n '2,22p' "${BASH_SOURCE[0]}" | sed -e 's/^# \{0,1\}//'; exit 0; }

MODE=""
SKIP_VERIFY=0
NO_INIT=0
DEPS=""
SKIP_DIFY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)        MODE="${2:-}"; shift 2 ;;
    --mode=*)      MODE="${1#*=}"; shift ;;
    --deps)        DEPS="${2:-}"; shift 2 ;;
    --deps=*)      DEPS="${1#*=}"; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    --no-init)     NO_INIT=1; shift ;;
    --no-dify|--skip-dify) SKIP_DIFY=1; shift ;;
    -h|--help)     usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

# ── ① 确认我们在离线包里 ───────────────────────────────────────────────────
if [ ! -f "$BUNDLE/VERSION" ]; then
  blocked "找不到 $BUNDLE/VERSION —— 本脚本必须从**离线包根目录**调用（不要在别处单独拷贝执行）。"
fi
SCHEMA="$(env_get "$BUNDLE/VERSION" CC_BUNDLE_SCHEMA '')"
if [ "$SCHEMA" != "ccops-offline/1" ]; then
  blocked "这不是**全量**离线包（CC_BUNDLE_SCHEMA='${SCHEMA:-空}'）。"
fi
if [ ! -f "$BUNDLE/deploy/install.sh" ]; then
  blocked "离线包不完整：缺 $BUNDLE/deploy/install.sh。"
fi

[ -n "$MODE" ] || MODE="standalone"
case "$MODE" in attached|standalone) ;; *) die "--mode 只能是 attached 或 standalone（收到 '$MODE'）" ;; esac
[ -n "$DEPS" ] || DEPS="install"
case "$DEPS" in check|install) ;; *) die "--deps 只能是 check 或 install（收到 '$DEPS'）" ;; esac

if [ "$SKIP_DIFY" = "1" ]; then
  export CC_SKIP_DIFY=1
  # 不装 Dify 时"依赖策略=check"才是诚实的：本包没有可安装的编排层
  [ "$DEPS" = "install" ] && DEPS="check"
  info "已选择 --no-dify：只交付应用层 6 服务（Dify 由你复用既有实例）"
fi
export CC_OFFLINE=1

printf '%s\n' "$C_BLD"
cat <<'BANNER'
  ╔══════════════════════════════════════════════════════════════════════╗
  ║  credit-card-sys-ops · 全量离线包安装（断网可装）                    ║
  ║  应用层 6 服务 + Neo4j + GraphDB + Dify（15 容器）                    ║
  ╚══════════════════════════════════════════════════════════════════════╝
BANNER
printf '%s\n' "$C_RST"
info "离线包：$BUNDLE"
info "镜像标签：$(env_get "$BUNDLE/VERSION" CC_IMAGE_TAG '?')"
info "目标平台：$(env_get "$BUNDLE/VERSION" CC_PLATFORM '?')"
info "档位    ：$MODE"

# ── ② 交给 install.sh（--offline 会关掉一切 pull/git clone）────────────────
ARGS=(--offline --bundle "$BUNDLE" --mode "$MODE" --deps "$DEPS")
[ "$SKIP_VERIFY" = "1" ] && ARGS+=(--skip-verify)
[ "$NO_INIT" = "1" ] && ARGS+=(--no-init)

bash "$BUNDLE/deploy/install.sh" "${ARGS[@]}"
RC=$?

# ── ③ 离线特有的收尾说明（不重复 install.sh 已打印的内容）──────────────────
ENVF="$BUNDLE/deploy/.env"
printf '\n'
hdr "离线安装 · 后续动作"
if [ "$RC" = "2" ]; then
  warn "结果是 BLOCKED（环境不满足，**不是**代码缺陷，也不等于装好了）。"
  warn "  按上方逐条提示处理后重跑：sudo bash install-offline.sh --mode $MODE"
elif [ "$RC" = "1" ]; then
  err "结果是 FAIL（有检查项不达标）。**不得**据此次结果宣称部署成功。"
  err "  诊断顺序：docker compose -f deploy/compose.yml ps → logs → 再跑 deploy/verify_deploy.py"
else
  ok "应用层部署与验收已完成。"
fi

if [ "$SKIP_DIFY" != "1" ] && [ -f "$ENVF" ]; then
  DBASE="$(env_get "$ENVF" DIFY_BASE_URL '')"
  # 内容感知：别把"配置里有 URL"当成"Dify 起来了"（本项目踩过被别的服务骗过的假阳）
  if [ -n "$DBASE" ] && _json_has_key "$DBASE/console/api/setup" step 8; then
    printf '  Dify 控制台：%s\n' "$DBASE"
    printf '  管理员账号：%s（口令见 deploy/.env 的 DIFY_PASSWORD，**不打印**）\n' \
      "$(env_get "$ENVF" DIFY_EMAIL '')"
    cat <<'TIP'

  编排层还需要三件**只能人工/联网**的事（离线包不替你做，也不假装做了）：
    ① 在 Dify 控制台配置模型供应商（DeepSeek / OpenAI 兼容端点）。断网环境请指向
       内网 LLM 网关；没有模型，Agent 无法工作。
    ② 注册工具面（把 :8301–:8305 注册成 Dify provider）：
         sudo bash deploy/offline/wire_dify.sh
       先跑 --check 看差异，再按脚本提示执行 --apply。
    ③ KG-T-free 组需要的本地 fork 插件（shared/deploy/plugins/*.difypkg）：
       插件守护进程默认开启签名校验，内网自签包会被拒。处置与证据见
       deploy/offline/README.md §7.4（**一次性**放宽，装完立即恢复）。
TIP
  else
    warn "Dify 未起来（或未启用）：应用层与验收不受影响，但**总账会记 FAIL**（容器没起来是实测坏结果）。"
    warn "  若本次只交付应用层，请改用 --no-dify 重跑，让编排层明确记为「跳过」。"
    warn "  诊断：cd vendor/dify/docker && docker compose logs --tail=80 api"
  fi
fi

if [ "$RC" != "0" ]; then
  exit "$RC"
fi
exit 0
