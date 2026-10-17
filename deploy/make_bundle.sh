#!/bin/sh
# =============================================================================
# deploy/make_bundle.sh —— 生成**离线发布包**（一次构建、任意 Linux 服务器落地）
#
# ★ 本脚本是 **POSIX sh**，刻意不用 bash。
#   原因是一次真实事故：CI 构建镜像 `docker:29.1.4` 是 Alpine，**没有 bash**，
#   用 bash 语法会在 `bash: not found`（exit 127）处直接断掉。本脚本必须在
#   Alpine 的 busybox sh 下也能跑 ⇒ 不用数组、不用 `[[ ]]`、不用 `local`（可选）。
#
# 用法（在**与目标同架构**的构建机上执行，通常是 CI runner）：
#     sh deploy/make_bundle.sh --tag <镜像标签> --out <输出目录>
#
#   例：sh deploy/make_bundle.sh --tag 7bec5851 --out dist/release
#
# 产物布局（bundle 根 = 仓库相对布局，install.sh 的 REPO_ROOT 解析因此成立）
#     <out>/ccops-release-<tag>/
#     ├── images/ccops-app-images.tar.gz   ← 5 个应用镜像合一包（共享层只存一份）
#     ├── deploy/                           compose / install / verify / 本脚本
#     ├── shared/ scheme1-kg/ scheme2-onto/ dist/collect-layer/   源码树
#     ├── Dockerfile.*                     三个/五个镜像定义（可复现构建）
#     ├── VERSION                          镜像标签 + git sha + 构建时间
#     ├── MANIFEST.sha256                  全量校验（install.sh 第一步强制核对）
#     └── README.md                        五步安装说明
#
# ★ 两条硬纪律：
#   1. MANIFEST 只记**相对文件名**。记绝对路径会在跨机 `sha256sum -c` 时报
#      `FAILED open or read`（真实事故）。
#   2. **不导出官方基础镜像**。neo4j / graphdb / dify 按本项目裁定属「安装环境依赖」，
#      由目标机的 install.sh 体检后从官方镜像源安装；发布包只带自建的 5 个。
# =============================================================================
set -eu

TAG=""
OUT="dist/release"
NAME="ccops-release"

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)   TAG="${2:-}"; shift 2 ;;
    --tag=*) TAG="${1#*=}"; shift ;;
    --out)   OUT="${2:-}"; shift 2 ;;
    --out=*) OUT="${1#*=}"; shift ;;
    --name)  NAME="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,40p' "$0" | sed -e 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[ -n "$TAG" ] || { echo "错误：必须给 --tag <镜像标签>（与 docker build 时用的标签一致）" >&2; exit 2; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

SERVICES="collect kg onto kgt live-facts"

# ── 断言 5 个镜像都在本地（缺一个就打不出完整包，绝不打半截包）──────────────
IMG_LIST=""
for s in $SERVICES; do
  img="cc-ops/${s}:${TAG}"
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "错误：本地没有镜像 $img" >&2
    echo "      请先在与目标同架构的机器上构建：docker build -f Dockerfile.<svc> -t $img ." >&2
    exit 1
  fi
  IMG_LIST="$IMG_LIST $img"
  echo "[ok  ] 找到镜像 $img"
done

STAGE="$OUT/${NAME}-${TAG}"
echo "[信息] 暂存目录：$STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/images"

# ── ① 导出镜像：**合一包**（5 个镜像共享同一 base 层，分开导出会重复占体积）──
echo "[信息] docker save → images/ccops-app-images.tar.gz（gzip -9，这一步最慢）"
# shellcheck disable=SC2086
docker save $IMG_LIST | gzip -9 > "$STAGE/images/ccops-app-images.tar.gz"
echo "[ok  ] 镜像包：$(du -h "$STAGE/images/ccops-app-images.tar.gz" | cut -f1)"

# ── ② 复制部署与源码树（离线包必须自包含：契约装载要用源码树里的 targets.yaml）──
echo "[信息] 复制部署树与源码树"
copy_tree() {
  # 用 tar 管道复制，自然排除 __pycache__/*.pyc 等构建产物
  src="$1"; dst="$2"
  [ -e "$src" ] || { echo "[警告] 缺 $src（跳过）" >&2; return 0; }
  mkdir -p "$dst"
  ( cd "$REPO_ROOT" && tar cf - \
      --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
      --exclude='.pytest_cache' --exclude='*.log' --exclude='*.pid' \
      "$src" ) | ( cd "$dst" && tar xf - )
}
for d in deploy shared scheme1-kg scheme2-onto dist/collect-layer; do
  copy_tree "$d" "$STAGE"
done
# 镜像定义一并带上（让客户/后续可复核与复现构建）
for f in Dockerfile.collect Dockerfile.kg Dockerfile.onto Dockerfile.kgt Dockerfile.live-facts .dockerignore; do
  [ -f "$f" ] && cp "$f" "$STAGE/" || true
done
# ★ 基底快照必须进包（F2，2026-10-12 真机 E2E 实测）。
#   install.sh ④.0b 要拿它投影「快照命名图」：干净载体上少了它 ⇒ load_tbox.py persist
#   拒绝写入（「没有实测到任何快照命名图，基线不符合预期」）、cc_tools_onto.py 启动即崩
#   ⇒ onto 容器 crash-loop，而报错里一个字都没提"你还没投影"。
#   它与 live-facts 镜像里那份**同源**（Dockerfile.live-facts:70 从同一路径 COPY）。
#   先前版本只 copy_tree deploy/shared/scheme*/dist/collect-layer，`tmp/` 不在其中 ⇒
#   包里根本没有它；部署机上 GraphDB 卷恰好存着上次部署的快照图，把这个缺口**掩盖**了。
if [ -f tmp/ccf_live5.json ]; then
  mkdir -p "$STAGE/tmp"
  cp tmp/ccf_live5.json "$STAGE/tmp/"
  echo "[ok  ] 基底快照已入包：tmp/ccf_live5.json（$(du -h tmp/ccf_live5.json | cut -f1)）"
else
  echo "[警告] 缺 tmp/ccf_live5.json ⇒ 离线包里没有基底快照；standalone 首装时 ONTO 可能起不来" >&2
fi

# 顶层文档（若存在）
[ -f README.md ] && cp README.md "$STAGE/README-project.md" || true

# 清掉复制过来的临时/工作目录（部署过程产物不入包）
rm -rf "$STAGE/deploy/.work" "$STAGE/deploy/.deps" "$STAGE/deploy/secrets" 2>/dev/null || true
# 源码树里可能夹带本地 .env（**绝不能进包**）
find "$STAGE" -name '.env' -type f -delete 2>/dev/null || true
find "$STAGE" -name '.env.*' -type f ! -name '.env.example' -delete 2>/dev/null || true

# ★ 凭据填单也**绝不能进包**（F1，2026-10-12 真机 E2E 实测）。
#   `.gitignore` 只挡住了 git，挡不住 `tar` 出来的发布物；而 deploy/README.md 推荐的
#   流程恰恰是「cp credentials.example credentials → 填完 → 再打包」⇒ 那张填好的
#   会被上面的 copy_tree 原样打进包，并被 MANIFEST.sha256 登记成"发布物的一部分"。
#   实测那次包里是 854 B / 22 个非空键（GaussDB DSN、各口径口令）。
#   处置：删掉约定名 `deploy/credentials`；对**其余**除模板外的凭据类文件 **fail-closed**
#   ——宁可这次不出包，也不把口令发出去（漏了会有人手填进去，静默放行不会有人发现）。
rm -f "$STAGE/deploy/credentials" 2>/dev/null || true
SHEETS="$(find "$STAGE" -type f \( -name 'credentials' -o -name 'credentials.*' \) \
            ! -name 'credentials.example' 2>/dev/null | sort || true)"
if [ -n "$SHEETS" ]; then
  echo "" >&2
  echo "[错误] 离线包里仍有凭据填单，拒绝出包（只允许 deploy/credentials.example 模板）：" >&2
  printf '        %s\n' $SHEETS >&2
  echo "        处理：把填好的单子移出源码树，或改名成 credentials.example 以外的模板名" >&2
  exit 1
fi

# ── ③ VERSION（install.sh 以它为准改写 CC_IMAGE_TAG，避免"装的是旧镜像"）──
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
BUILT_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
cat > "$STAGE/VERSION" <<EOF
# 本文件的 CC_IMAGE_TAG 是**权威**：install.sh 载入离线包时会用它覆盖 .env 里的值
CC_IMAGE_TAG=$TAG
CC_BUNDLE_SCHEMA=ccops-release/1
GIT_SHA=$GIT_SHA
BUILT_AT=$BUILT_AT
CI_JOB_ID=${CI_JOB_ID:-local}
CI_PIPELINE_ID=${CI_PIPELINE_ID:-local}
SERVICES=$SERVICES
EOF
echo "[ok  ] VERSION 已写（tag=$TAG sha=$GIT_SHA）"

# ── ④ README（五步安装，客户照抄即可）──
cat > "$STAGE/README.md" <<EOF
# credit-card-sys-ops 离线发布包（$TAG）

构建于 $BUILT_AT ／ git $GIT_SHA

## 含什么

| 内容 | 说明 |
|---|---|
| \`images/ccops-app-images.tar.gz\` | **自建 5 个应用镜像**（collect/kg/onto/kgt/live-facts）合一包 |
| \`deploy/\` | compose 编排、一键安装脚本、验收脚本 |
| \`shared/ scheme1-kg/ scheme2-onto/ dist/collect-layer/\` | 源码树（契约装载要读 \`shared/deploy/targets.yaml\`） |
| \`Dockerfile.*\` | 镜像定义（可复核/可复现） |
| \`tmp/ccf_live5.json\` | 基底快照（CCF）。\`install.sh\` ④.0b 用它投影「快照命名图」——ONTO 按快照图作用域，**少了它干净载体的 onto 起不来** |

## 不含什么（**故意**）

\`neo4j\` / \`graphdb\` / \`dify\` **不在包内**——它们被裁定为「安装环境依赖」，
由目标机的 \`deploy/install.sh\` 先体检、缺了再从官方镜像源自动安装。
因此**首次部署需要目标机可达官方镜像源**；若目标机完全断网，请预先在目标机上
装好这三者，再用 \`--deps check\` 部署。

## 五步安装

\`\`\`bash
# 1. 校验（不通过即停）
cd ccops-release-$TAG && sha256sum -c MANIFEST.sha256

# 2. 配置（填外部依赖地址与口令）
cp deploy/.env.example deploy/.env && vi deploy/.env

# 3. 一键部署（自带 Neo4j+GraphDB；复用既有载体则用 --mode attached）
sudo bash deploy/install.sh --mode standalone

# 4. 验收（install.sh 已自动跑；也可单独跑）
python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone

# 5. 收工
\`\`\`

详细说明见 \`deploy/README.md\`。
EOF
echo "[ok  ] README.md 已写"

# ── ⑤ MANIFEST.sha256：**只记相对文件名** ────────────────────────────────────
echo "[信息] 生成 MANIFEST.sha256（相对路径）"
( cd "$STAGE" && find . -type f ! -name 'MANIFEST.sha256' | sort | sed 's#^\./##' > /tmp/_ccops_manifest_files.txt )
( cd "$STAGE" && while IFS= read -r f; do sha256sum "$f"; done < /tmp/_ccops_manifest_files.txt > MANIFEST.sha256 )
rm -f /tmp/_ccops_manifest_files.txt
NFILES="$(wc -l < "$STAGE/MANIFEST.sha256" | tr -d ' ')"
echo "[ok  ] MANIFEST.sha256：$NFILES 个文件"

# ── ⑥ 汇总 ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo " 离线包已生成：$STAGE"
echo " 总大小      ：$(du -sh "$STAGE" | cut -f1)"
echo " 文件数      ：$NFILES"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "下一步（任选其一）："
echo "  · 直接就地安装：sudo bash $STAGE/deploy/install.sh --mode standalone"
echo "  · 打包传输    ：tar czf ${NAME}-${TAG}.tar.gz -C $(dirname "$STAGE") $(basename "$STAGE")"
