#!/bin/sh
# =============================================================================
# deploy/build_images.sh —— 在**有 Docker 的机器上**构建 5 个应用镜像
#
# 什么时候用它
# ------------
# 构建镜像需要一个**真能构建镜像的 Docker 环境**。共享式容器化 CI 常见的
# docker-in-docker 配置（执行器未开 privileged）往往起不来 dind
#   · 未挂宿主 /var/run/docker.sock
#   · 声明 dind 服务后起不来（mount: permission denied /
#     Could not mount /sys/kernel/security）
#   ⇒ 镜像构建改在**真有 Docker** 的机器上做更省事：
#        · 任一装了 Docker 的机器（可直接在宿主上跑本脚本）；
#        · ★ **架构必须与目标机一致**（目标机 aarch64 ⇒ 构建机也要 aarch64）。
#
# ★ POSIX sh（不依赖 bash）—— 便于在任意精简环境里跑。
#
# 用法
# ----
#     sh deploy/build_images.sh                 # tag 默认取 git 短 sha
#     sh deploy/build_images.sh --tag v1.0.0
#     sh deploy/build_images.sh --tag v1.0.0 --bundle     # 构建完顺手打离线包
#
# 构建完的下一步
# --------------
#     sh deploy/make_bundle.sh --tag <同一个 tag> --out dist/release
# =============================================================================
set -eu

TAG=""
DO_BUNDLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag)    TAG="${2:-}"; shift 2 ;;
    --tag=*)  TAG="${1#*=}"; shift ;;
    --bundle) DO_BUNDLE=1; shift ;;
    -h|--help) sed -n '2,32p' "$0" | sed -e 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── 前置：必须有可用的 docker ────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  echo "错误：本机没有 docker —— 构建镜像必须在**有 Docker** 的机器上做。" >&2
  echo "      开发机（x86_64、无 Docker）**不能**构建本项目镜像。" >&2
  exit 2
fi
if ! docker version >/dev/null 2>&1; then
  echo "错误：docker 守护进程不可达（无特权 / 未启动 / DOCKER_HOST 指错）。" >&2
  echo "      若你在容器化 CI 作业里看到这一条，那是 runner 未开 privileged 或未挂" >&2
  echo "      /var/run/docker.sock —— 见本脚本头部说明与 docs/11 §11.2。" >&2
  exit 2
fi

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64|x86_64|amd64) ;;
  *) echo "警告：非常见架构 $ARCH，请确认与目标机一致" >&2 ;;
esac

if [ -z "$TAG" ]; then
  TAG="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
fi

echo "════════════════════════════════════════════════════════════════"
echo " 构建 5 个应用镜像"
echo "   构建机架构：$ARCH   （目标机必须与之相同；本项目目标机为 aarch64）"
echo "   镜像标签  ：$TAG"
echo "════════════════════════════════════════════════════════════════"

FAILED=""
for s in collect kg onto kgt live-facts; do
  echo ""
  echo "── docker build -f Dockerfile.$s -t cc-ops/$s:$TAG . ──"
  if docker build -f "Dockerfile.$s" -t "cc-ops/$s:$TAG" . ; then
    echo "[ok  ] cc-ops/$s:$TAG"
  else
    echo "[FAIL] cc-ops/$s:$TAG" >&2
    FAILED="$FAILED $s"
  fi
done

echo ""
echo "── 产物 ──"
docker images 'cc-ops/*' || true

if [ -n "$FAILED" ]; then
  echo "" >&2
  echo "错误：以下镜像构建失败：$FAILED" >&2
  echo "      **不得**用不完整的镜像集去打离线包。" >&2
  exit 1
fi

echo ""
echo "5 个镜像全部构建成功。下一步："
echo "    sh deploy/make_bundle.sh --tag $TAG --out dist/release"

if [ "$DO_BUNDLE" = "1" ]; then
  echo ""
  echo "── --bundle：继续打离线包 ──"
  sh deploy/make_bundle.sh --tag "$TAG" --out dist/release
fi
