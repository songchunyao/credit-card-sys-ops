# 离线（断网）部署手册 —— 全量离线包

> 面向**交付/运维**：在能联网的构建机上一次出包，在**完全断网**的 Linux 服务器上
> 一条命令装完。载体（Neo4j / GraphDB）与编排层（Dify）都随包携带，安装期**不拉任何镜像**。
>
> 与 `deploy/README.md` §6 的区别：那里的离线包只带**自建 5 个应用镜像**，
> Neo4j / GraphDB / Dify 属"安装环境依赖"、需要目标机联网补装。本文的包是**全量包**，
> 适用场景正是"目标机连不上外网、也拿不到这些组件的官方镜像"。

---

## 0. 一分钟版

**构建机**（有 Docker + 能上网 + 与目标同架构）：

```bash
sh deploy/build_images.sh --tag v1.0.0                    # 先构建 5 个应用镜像
sh deploy/offline/make_offline_bundle.sh --tag v1.0.0 --archive
```

**目标机**（断网 Linux，已装 Docker + compose v2）：

```bash
tar xzf ccops-offline-v1.0.0.tar.gz && cd ccops-offline-v1.0.0
sha256sum -c MANIFEST.sha256
sudo bash install-offline.sh
```

装完看末行：`PASS=… FAIL=0 BLOCKED=…`。`BLOCKED` 是"没测到"，不是"通过"，需人看。

---

## 1. 设计：为什么是"打包镜像"而不是"在目标机装组件"

离线环境的硬约束只有一条：**任何需要下载的动作都不可用**。于是所有"下载"必须在
构建机完成，目标机只做本地动作：

| 组件 | 联网部署的做法 | 离线包的做法 |
|---|---|---|
| 应用层 5 镜像 | 本地 `docker build` | 构建机 build → `docker save` → 目标机 `docker load` |
| Neo4j / GraphDB | `docker pull` 官方镜像 | 构建机 pull → save → load（**已载入才允许起栈**） |
| Dify（约 10+ 镜像） | `git clone` + `docker compose up`（隐式 pull） | 构建机取 `docker/` 源码树 + 按 compose 解析出的清单逐个 pull/save；目标机 `--pull never` |
| Dify 源码树 | `git clone` | `vendor/dify/docker/` 随包 |
| 口令/密钥 | `cp .env.example .env` | 目标机首次安装时**随机生成**，0600，不打印 |

配套的三道闸：

1. **`--pull never`**：应用层、载体、Dify 三处起栈都禁止 pull。断网时 pull 会卡在
   超时上，症状看起来像"compose 起不来"，排查方向全错。
2. **`images/IMAGES.lock`**：记录每个 `ref` 的 image Id / 架构 / 字节数。`docker load`
   后逐条比对 image Id，不一致**直接拒绝继续**（tag 可以 retag，Id 不能）。
3. **不打包任何填好的凭据**：`deploy/.env`、`deploy/credentials`、Dify 的 `docker/.env`
   都**不入包**；Dify 只带 `.env.offline-template`（含 `__CC_GENERATE__` 占位符）。

---

## 2. 构建机：出包

### 2.1 机器要求

| 项 | 要求 | 为什么 |
|---|---|---|
| 操作系统 | Linux（有 `tar` / `gzip` / `sha256sum`） | 出包脚本是 POSIX sh |
| Docker | 可用（`docker info` 通） | 要 pull / save / 解析 compose |
| Docker Compose | **v2.20+**（`docker compose config --images`） | 用它枚举 Dify 实际引用的镜像，避免人工清单漂移 |
| 网络 | 能到 Docker Hub / 你们的镜像源 / GitHub | Dify 源码与官方镜像 |
| 架构 | **与目标机一致**（本项目目标机为 aarch64） | 应用层镜像不能交叉构建；载体/Dify 可用 `--platform` 交叉拉取 |
| 磁盘 | 建议 ≥ 60 GB 空闲 | 镜像 + 中间 tar + 归档 |
| 内存 | ≥ 4 GB | — |

> ★ 若构建机也不能访问 Docker Hub / GitHub（例如内网只允许发布到私有 registry），
> 用 `--dify-src <已解压的 Dify 源码树>` 跳过 GitHub；镜像则需先同步到私有 registry
> （脚本仍用 `docker pull --platform`，把 `neo4j:5-community` 等 ref 改成你的 registry 地址即可）。
>
> ★ **aarch64 的镜像清单风险**：个别第三方镜像可能没有 `arm64` 清单（典型是
> `ubuntu/squid` 这类）。`docker pull --platform linux/arm64` 会直接失败并**点名镜像**，
> 不会静默出半套包。处置：在私有 registry 放同功能的多架构镜像，并把 Dify
> `.env.example` 里对应的 `*_IMAGE` 变量改成该地址（配合 `--dify-src` 指向改好的源码树）。

### 2.2 先构建应用层镜像（同架构机器）

```bash
sh deploy/build_images.sh --tag v1.0.0
```

产物：`cc-ops/{collect,kg,onto,kgt,live-facts}:v1.0.0`。
这一步不能交叉构建（Dockerfile 里有 `pip install` 与架构相关的 wheel）。
构建机有 Docker 且能装 pip 依赖时，也可让 §2.3 顺手做掉：加 `--build`。

### 2.3 出全量离线包

```bash
sh deploy/offline/make_offline_bundle.sh --tag v1.0.0 --archive
# 一条命令（构建 + 出包）：
sh deploy/offline/make_offline_bundle.sh --tag v1.0.0 --build --archive
```

常用选项：

```bash
--platform linux/arm64        # 目标 aarch64（默认按构建机；交叉出包时显式给）
--dify-version 1.17.0         # 默认取 deploy/.env.example 的 CC_DIFY_VERSION
--dify-src /data/dify-1.17.0  # 内网无 GitHub 时，指向已解压源码树（需含 docker/）
--neo4j-image neo4j:5-community
--graphdb-image ontotext/graphdb:10.8.0
--skip-dify                   # 只带应用层 + 载体（复用既有 Dify）
--build                       # 先构建 5 个应用镜像（等价于先跑 build_images.sh）
--archive                     # 额外打 ccops-offline-<tag>.tar.gz
--skip-selfcheck              # 构建机无 python3 时显式跳过包自检（默认拒绝出包）
```

脚本会依次：

1. 断言 5 个应用镜像存在且架构 = 目标架构；
2. 调 `deploy/make_bundle.sh` 生成应用层基础包（源码树 / MANIFEST / VERSION / 基底快照）；
3. `docker pull` 载体镜像 → `docker save` 成 `images/20-carriers-images.tar.gz`；
4. 取 Dify 源码树 → 生成 `vendor/dify/docker/.env`（口令占位符、关插件市场）→
   `docker compose config --images` 枚举 → 逐个 pull → 一包 save →
   把 `.env` 换成 `.env.offline-template`（**填好的 .env 不入包**）；
5. 生成 `images/IMAGES.lock`（ref / image Id / arch / size / digest）；
6. 写 `VERSION`（含 `CC_MIN_DISK_GB_OFFLINE`，由镜像总大小推算）、`README.md`、
   `install-offline.sh`；
7. 算全量 `MANIFEST.sha256`；
8. 跑 `deploy/offline/check_offline_bundle.py --hash` 自检（不通过即**拒绝交付**）。

产物：

```
dist/offline/ccops-offline-v1.0.0/
├── install-offline.sh
├── images/{ccops-app-images,20-carriers-images,30-dify-images}.tar.gz + IMAGES.lock
├── vendor/dify/{docker/, IMAGES.txt}
├── deploy/ shared/ scheme1-kg/ scheme2-onto/ dist/collect-layer/ ...
├── VERSION  MANIFEST.sha256  README.md
```

### 2.4 出包后自检（可单独重跑）

```bash
python3 deploy/offline/check_offline_bundle.py \
        --bundle dist/offline/ccops-offline-v1.0.0 --hash
```

检查项：关键文件、`VERSION` 必填键、镜像包非空、`IMAGES.lock` 格式与 5 应用镜像已登记、
Dify 编排树/占位符/插件市场关闭/`volumes` 无数据、**无凭据与运行期产物**、
`MANIFEST` 覆盖全量并逐文件 sha256、文本文件无 CRLF、`bash -n` 语法、
`install.sh --offline` 与 `deps.sh` 离线分支确实接通。

> 退出码：`0` PASS / `1` FAIL / `2` BLOCKED（例如找不到 bash 无法做语法检查）。
> **只要不是 0 就不要交付**。

---

## 3. 传输

镜像包合计通常 **6–12 GB**（取决于 Dify 版本与是否含向量库），归档后更大。

```bash
# ① 推荐：rsync（可断点续传）
rsync -avP dist/offline/ccops-offline-v1.0.0/ user@target:/opt/ccops/

# ② 或 tar 流转
tar -C dist/offline -czf - ccops-offline-v1.0.0 | ssh user@target 'tar -C /opt/ccops -xzf -'

# ③ U 盘 / FAT32（单文件 ≤ 4 GB）：先 split
tar -C dist/offline -czf - ccops-offline-v1.0.0 | split -b 3G - ccops-v1.0.0.tar.gz.part-
# 目标机合并后必须校验 sha256（分片本身没有校验和）
cat ccops-v1.0.0.tar.gz.part-* | tar -xzf -
```

**到目标机后第一件事永远是**：

```bash
cd ccops-offline-v1.0.0 && sha256sum -c MANIFEST.sha256
```

这能抓住传输截断/损坏 —— 而 `docker load` 对**截断的 tar** 有时会先成功一部分，
真正失败发生在起栈时，表现为莫名其妙的 `No such image`。

---

## 4. 目标机：安装

### 4.1 前置

```bash
docker info                 # 守护进程可达
docker compose version      # 必须是 v2（本项目编排用了 name: 与 depends_on.condition）
df -h /var/lib/docker       # 可用 ≥ 包内 VERSION 声明的 CC_MIN_DISK_GB_OFFLINE
free -g                     # 建议 ≥ 16 GB（Dify 4–6 GB + 载体 ~3 GB + 应用 ~1 GB）
python3 --version           # **必需**：契约装载的 GraphDB 建仓脚本 + 验收矩阵都靠它
command -v curl || command -v wget   # 宿主侧探活（至少一个）
```

> **端口冲突**：`:7475/:7688/:7200` 若被别的服务占用，`standalone` 档会**拒绝安装**
> （禁止新建第二个 Neo4j / 改写既有 GraphDB 仓库）。这时用 `--mode attached`
> 复用既有载体，或先释放端口。

### 4.2 一键安装

```bash
cd ccops-offline-v1.0.0
sudo bash install-offline.sh
```

默认 `--mode standalone`（自带 Neo4j + GraphDB）+ `--deps install`
（这里的 install 指"用**包内镜像**把 Dify 起起来"，**不联网**）。

参数：

| 参数 | 作用 |
|---|---|
| `--mode attached` | 复用既有 Neo4j/GraphDB（宿主 7475/7688/7200） |
| `--no-dify` | 只交付应用层 6 服务；Dify 用你们既有的实例 |
| `--skip-verify` | 跳过验收（**该部署不得视为已验收**） |
| `--no-init` | 跳过契约装载（TBox 已装过时用） |
| `--deps check` | 只体检、不启动 Dify |

> **`--mode attached` 前先在 `deploy/.env` 填 `CC_NEO4J_HTTP_URL` / `CC_GRAPHDB_URL`**
> （显式 IP：`host.docker.internal` 指向跑 docker 的那台机器，不是目标机）。
> 不填就会沿用模板里的 `host.docker.internal`，表现为"载体连不上"。

### 4.3 安装过程做了什么

| 步 | 内容 | 离线特性 |
|---|---|---|
| ①.0 | 离线包自检（结构/架构/磁盘/CRLF） | **不访问网络** |
| ①.5 | 生成 `deploy/.env` 与 Dify 的 `docker/.env`（随机口令、0600） | 口令不打印 |
| ① | 环境预检（OS/架构/docker/compose/端口/磁盘/内存/`.env` 键） | — |
| ② | `docker load` **全部** tar.gz，再按 `IMAGES.lock` 核 image Id | **绝不 pull** |
| ③ | 外部依赖体检 | 载体由 ③.5 起；Dify 推迟到 ⑧ |
| ③.5 | `compose up -d --pull never neo4j graphdb` | 镜像已在本地 |
| ④ | 建 GraphDB 仓库 → 投影基底快照 → 装 TBox（幂等） | 用包内镜像里的 loader |
| ⑤ | `compose up -d --pull never`（应用层 6 服务） | 绝不 pull |
| ⑥ | 等 6 个容器 healthy | — |
| ⑦ | 验收矩阵 `deploy/verify_deploy.py` | — |
| ⑧ | 用 `vendor/dify/docker` 起 Dify（`--pull never`）并建管理员 | 不 clone、不联网 |

### 4.4 装完拿什么

生成的口令**只在** `deploy/.env`（0600）里：

```bash
grep -E '^(DIFY_BASE_URL|DIFY_EMAIL|CC_DIFY_HTTP_PORT|CC_DIFY_PUBLIC_HOST)=' deploy/.env
# DIFY_PASSWORD、CC_NEO4J_PASSWORD 同文件；按本单位口令管理规定保存/轮换
```

> ⚠ **两个 `.env` 要一起备份**：`deploy/.env`（应用层与 Neo4j 口令）与
> `vendor/dify/docker/.env`（Dify 的 DB/Redis/签名密钥）。后者一旦丢失而
> `vendor/dify/docker/volumes/db/data` 还在，新生成的口令与旧库不匹配 ⇒ Dify 会以
> "数据库连接失败"crash-loop，报错不会点名"口令变了"。恢复办法只有：找回旧 `.env`，
> 或 `down -v` 后重装（会丢 Dify 的知识库/会话/文件）。

服务入口与运维命令见包内 `README.md` §4/§5。

---

## 5. 配置矩阵：哪些是自动的，哪些必须人工

### 5.1 安装时自动写入（**不要手改**，除非知道后果）

| 键 | 值 | 说明 |
|---|---|---|
| `CC_OFFLINE` | `1` | 离线模式总开关 |
| `CC_IMAGE_TAG` | 包内 `VERSION` | 权威标签；防止"装成旧镜像" |
| `CC_NEO4J_IMAGE` / `CC_GRAPHDB_IMAGE` | 包内 `VERSION` | 必须与 `IMAGES.lock` 一致 |
| `CC_DIFY_DIR` | `<包根>/vendor/dify` | Dify 源码树 |
| `CC_DOCKER_GID` | `stat -c '%g' /var/run/docker.sock` | 写错会让采集层**静默采空**（宿主 docker 组权限） |
| `CC_DIFY_HTTP_PORT` / `DIFY_BASE_URL` | 自动挑空闲端口（:80 被占则 8080+） | — |
| `CC_DIFY_PUBLIC_HOST` | 本机业务地址（识别失败则留空并告警） | Dify 用它回连 `:8301–:8305` |
| `DIFY_EMAIL` / `DIFY_PASSWORD` | 首次安装随机生成 | 不打印 |
| `CC_NEO4J_PASSWORD` | standalone 档首次安装随机生成 | 不打印；attached 档留空则从 `CC_NEO4J_ENV_FILE` 取 |

### 5.2 必须人工确认（不填的后果已在括号里）

| 键 | 何时必须 | 不填的后果 |
|---|---|---|
| `GAUSSDB_DSN` / `GAUSSDB_NODES` | 要真正采集数据 | 采集层起得来但**事实全空**；验收 C3c 判 BLOCKED |
| `GAUSSDB_SSH_USER/PASSWORD` 或 `GAUSSDB_SSH_KEY` | OS 层探针 | OS 层事实缺失（CPU/内存/load 等） |
| `MCP_STORAGE_DM_BASE_URL/USER/PASSWORD` | 存储域探针 | 存储层不产出事实（未配置即不采，属预期） |
| `TPOPS_BASE/USER/PASSWORD` | TPOPS 域 | 该域事实缺失 |
| `CC_APP_LOG_HOST_DIR` | L1 应用遥测 | 延迟/TPS 类判据拿不到 |
| `CC_APP_CONTAINER` | L2 容器探针 | 容器层事实缺失 |
| `CC_DIFY_PUBLIC_HOST` | Dify 回连工具面 | 工具注册后**容器内连 127.0.0.1 指向自己** ⇒ 工具全不可用 |
| `DIFY_*` 模型供应商 | Agent 运行 | 见 §7.5，**离线包不含模型** |

> 键名权威表见 `deploy/README.md` §4.3。**数值键（`LOG_MSG_MAX` 等）绝不能留空串** ——
> 代码用 `int(os.environ.get(K, default))`，空串会 `ValueError` 让容器 crash-loop。

---

## 6. 升级与回滚

### 6.1 一条必须先知道的事实

| 组件 | 数据存放 | 换一个包目录后 |
|---|---|---|
| 应用层审计链 | **命名卷** `ccops-audit` | 自动保留 |
| Neo4j / GraphDB | **命名卷** `ccops-neo4j-data` / `ccops-graphdb-data` | 自动保留 |
| **Dify** | **包内 bind mount** `vendor/dify/docker/volumes/` | **不会保留**（新包 = 新空目录） |

Dify 官方 compose（1.17.x）用的是 `./volumes/...` 相对路径。所以"解压新包、直接装"会得到
一套**空的 Dify**（要重新初始化、知识库/会话/文件全没），而应用层与载体却还在。
下面第 3 步就是补这个缺口（若你们的 Dify 版本改用了命名卷，该步可跳过）。

### 6.2 升级（保留 Dify 数据）

```bash
OLD=ccops-offline-v1.0.0
NEW=ccops-offline-v1.1.0

# 1) 先停旧 Dify（项目名同为 dify，端口/容器名会冲突）
( cd $OLD/vendor/dify/docker && docker compose down )

# 2) 解新包
tar xzf $NEW.tar.gz

# 3) 迁移两份 .env 与 Dify 的数据目录（**关键**；volumes 不存在说明是命名卷，跳过）
cp -a $OLD/deploy/.env               $NEW/deploy/.env
cp -a $OLD/vendor/dify/docker/.env   $NEW/vendor/dify/docker/.env
if [ -d $OLD/vendor/dify/docker/volumes ]; then
  rm -rf $NEW/vendor/dify/docker/volumes
  cp -a $OLD/vendor/dify/docker/volumes $NEW/vendor/dify/docker/volumes
fi

# 4) 装新包（会自动把 CC_IMAGE_TAG / CC_DIFY_DIR 改写为新包的值）
cd $NEW && sudo bash install-offline.sh
```

> 若有意"全新开始"，跳过第 3 步即可（Dify 会重新初始化；旧数据仍留在 `$OLD` 里）。

### 6.3 回滚

与升级对称：停新包 Dify → 把新包的 `vendor/dify/docker/volumes` 与两份 `.env`
拷回旧包 → 在旧包目录重跑 `install-offline.sh`。
应用层/载体是命名卷，不需要搬。

### 6.4 卸载

```bash
# 应用层与载体（★ -v 会删审计链与载体数据，先备份）
docker compose -f deploy/compose.yml -f deploy/compose.standalone.yml down
# 或连卷：... down -v

# Dify
cd vendor/dify/docker && docker compose down          # 加 -v 会删知识库/会话/文件
```

---

## 7. Dify 的离线行为（最容易踩的部分）

### 7.1 镜像与编排

- Dify 的镜像清单**不是人工维护的**：出包时由 `docker compose config --images` 从包内
  `docker-compose.yaml` + `.env` 解析，逐个 pull，再 `save` 成一包。
  ⇒ 换 Dify 版本时只要 `--dify-version`，清单自动跟着变。
- 目标机起 Dify 用 `docker compose up -d --pull never`。若报 `No such image`，
  说明出包时的 compose 解析与目标机的 `.env` 不一致（**不要**在目标机手工 pull）。
- Dify 的 `.env` **不入包**：包内只有 `.env.offline-template`。安装时复制成
  `vendor/dify/docker/.env` 并逐键替换 `__CC_GENERATE__`（每个键**独立**随机值）。
  ⇒ `MANIFEST` 不会因为安装后 `.env` 变化而校验失败。

### 7.2 端口与管理员

- `:80`/`:443` 常被既有服务占用：安装时自动改用 `:8080`/`:8443`，并写进
  Dify 的 `EXPOSE_NGINX_PORT` / `EXPOSE_NGINX_SSL_PORT` 与 `DIFY_BASE_URL`。
- 管理员由 `POST /console/api/setup` 创建（幂等：已初始化则跳过）。
  账号/口令写在 `deploy/.env`，**不打印**。

### 7.3 工具面注册与 Agent 创建

工具注册（把 `:8301–:8305` 变成 Dify 的 API tool provider）：

```bash
sudo bash deploy/offline/wire_dify.sh --check    # 只读
sudo bash deploy/offline/wire_dify.sh --apply    # 幂等写入 + 两套独立比对
```

它借 `cc-ops/live-facts` 镜像里的 python3 + PyYAML 当运行时，**不需要在目标机 pip install**。
口令通过环境变量传入容器（`-e DIFY_PASSWORD` 不带值），不出现在 `docker run` 参数里。

Agent（App）创建需要 Dify 里**已配好模型供应商**：

```bash
cd /opt/ccops/ccops-offline-<tag>
set -a; . deploy/.env; set +a           # 口令含 # 或空格时改用逐项 export
IMG="cc-ops/live-facts:${CC_IMAGE_TAG}"

# 先看 binding 声明了哪些 App
docker run --rm --network host -e PYTHONIOENCODING=utf-8 \
  -v "$PWD:/repo:ro" -w /repo "$IMG" python -B -c \
  'import yaml;d=yaml.safe_load(open("shared/deploy/dify/binding.yaml"));print(",".join(sorted(d["apps"])))'

# 建主对照臂（Q 臂与 R 臂），成功后再建 KG-T 家族
docker run --rm --network host \
  -e PYTHONIOENCODING=utf-8 -e DIFY_BASE_URL -e DIFY_EMAIL -e DIFY_PASSWORD \
  -e CC_DIFY_PUBLIC_HOST -v "$PWD:/repo:ro" -w /repo "$IMG" \
  python -B shared/deploy/dify/deploy_agents.py --apps cc-kg-agent,cc-onto-agent --apply

# 只读核对（任何 --apply 都应能跟一次 --check）
docker run ... python -B shared/deploy/dify/deploy_agents.py --check
```

> `deploy_agents.py --apply` 是"只新增 + 整体替换 model-config"，幂等；它**不删**任何 App。
> `cc-kgt-free-agent` 依赖 §7.4 的插件，插件没装好时**不要**把它一起 apply（会 fail-closed）。

### 7.4 本地 fork 插件（`KG-T-free` 组需要）

包内自带源码与产物：

```
shared/deploy/plugins/neo4j_cypher_query/
├── src/                                  插件源码（manifest/provider/tools）
└── dist/cc-ops-neo4j_cypher_query-0.1.0.difypkg   可直接上传的包
```

离线安装时**不需要** marketplace，但 Dify 的插件守护进程默认**校验签名**，
自签包会被拒：

```
plugin verification has been enabled, and the plugin you want to install has a bad signature
```

处置（与 `docs/09` F7 一致，**一次性动作、不可复现、不要写成常规流程**）：
临时给 `plugin_daemon` 注入 `FORCE_VERIFYING_SIGNATURE=false` 重放一次，装完
**立即恢复**原值（`.env` 不改，其余容器不动）。若你们的合规要求不允许放开签名，
则 `KG-T-free` 组离线不可用 —— 这属于**已知边界**，不是安装故障。

### 7.5 模型供应商（离线包不含，也无法含）

Dify 的 Agent 需要一个可达的 LLM 端点。断网环境请指向**内网 LLM 网关**
（DeepSeek / OpenAI 兼容均可）。没有模型时：

- 应用层 6 服务、工具面注册、验收矩阵都**不受影响**；
- Agent 一调用就失败，这属于"环境未就绪"，不是部署缺陷。

### 7.6 插件市场已关闭

出包时已写入 `MARKETPLACE_ENABLED=false`。断网环境若保持开启，`plugin_daemon`
会反复重试市场接口，日志全是超时噪声，还会拖慢启动。

---

## 8. 验收：怎么读结论

`install-offline.sh` 末行会打印 `PASS=x FAIL=y BLOCKED=z`，并跑 `deploy/verify_deploy.py`：

| 结果 | 含义 | 处置 |
|---|---|---|
| `PASS` | 断言全部成立 | 可交付 |
| `FAIL` | **测得坏结果**（工具面不同形、审计链没追加……） | 必须修，不得交付 |
| `BLOCKED` | **没测到**（无 docker 权限、GaussDB 不通、`--with-gather` 被护栏拦下……） | 人看一遍，补齐后重测；**不得当通过** |

单独重跑：

```bash
python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone
python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone --with-gather   # 真实采集
```

> `--with-gather` 有 600 s 护栏窗口：**不要连打**，第 3 次起会被判 `BLOCKED`
> （设计行为，不是探针故障）。

---

## 9. 排障

| 症状 | 最可能的原因 | 处置 |
|---|---|---|
| `镜像身份不符` | tar 被换/截断，或本机已有同名不同内容镜像 | 重拷 + `sha256sum -c`；不要手工 `docker tag` |
| `No such image` + `--pull never` | 该镜像没进包 | 回构建机重出包（先看 `vendor/dify/IMAGES.txt` 是否含它） |
| `exec format error` | 架构不符 | 同架构重出包；`VERSION` 的 `TARGET_ARCH` 必须等于 `uname -m` |
| Dify 启动慢 / 反复重启 | 内存不足 / 端口冲突 / `.env` 占位符未替换 | `docker compose logs`；确认 `deploy/.env` 无 `__CC_GENERATE__` |
| Dify 日志大量 marketplace 超时 | 未关市场 | 确认 `.env` 的 `MARKETPLACE_ENABLED=false` |
| `工具全不可用` | `CC_DIFY_PUBLIC_HOST` 为空或写成 127.0.0.1 | 改成宿主可达 IP，重跑 `install-offline.sh` + `wire_dify.sh --apply` |
| 采集层事实全空 | `GAUSSDB_DSN` / `GAUSSDB_NODES` 空或键名漂移 | `curl :8187/health` 看 `gaussdb.ok` |
| `$'\r': command not found` | CRLF | `sed -i 's/\r$//' <file>`；出包自检 N1 会拦 |
| `MANIFEST` 校验失败但文件看着都在 | 传输时被改写（如 Windows 编辑器） | 重新拷贝；不要在目标机上编辑包内文件 |

---

## 10. 安全与合规

- **包内零凭据**：`deploy/.env`、`deploy/credentials`、Dify 的 `docker/.env` 都不入包；
  自检 S1 会扫 `*.pem` / `*.key` / `credentials*` / `id_rsa*`，以及**任何** `.env*`
  （只放行 `.env.example` 与 `.env.offline-template`）。
- **`.env` 备份是真实事故点**：`--dify-src` 指向的源码树里可能残留 `docker/.env.bak_*`，
  其中是 DB / Redis / plugin 的真实口令。出包器会**删除它们并在仍有残留时拒绝出包**；
  S1 也会把 `.env.bak_*` / `.env.local` 判为泄漏（2026-10-18 第一版包曾因此中招，已修复）。
- **口令不打印**：安装只打印"已生成"，值只落 `deploy/.env`（0600）。
- **镜像按 Id 锁定**：`IMAGES.lock` + `MANIFEST.sha256` 双层校验，防"同名不同物"。
- **最小注入**：compose 刻意不用 `env_file:`，各服务只拿到自己声明的键。
- **非 root 运行**：5 个应用镜像均以 UID 10001 运行；采集层只读挂载宿主观测面。
- **审计链**：`ccops-audit` 命名卷、追加式 JSONL，`down` 不丢、`down -v` 才丢。

> **再分发许可**：Neo4j Community（GPLv3）、Ontotext GraphDB（其免费版有独立许可条款）、
> Dify（Apache-2.0 + 附加条件）、以及各基础镜像的许可，**随包分发前请法务确认**。
> 本包只是把你们有权使用的镜像打成一个离线包，不改变其许可。

---

## 11. 已知边界（离线包不能解决问题的部分）

1. **LLM 端点**：不含模型与服务；需内网 LLM 网关（§7.5）。
2. **被观测对象**：GaussDB / Dorado / TPOPS / 被测应用的地址与凭据，必须现场提供。
3. **Dify 市场插件**：不含 marketplace 内容；本项目只自带一个 fork 插件（§7.4）。
4. **应用层镜像不能离线重建**：Dockerfile 里的 `pip install` 需要外网；离线只支持
   `docker load` 已构建好的镜像。要"可重建"，需另外做 pip wheelhouse（不在本包范围）。
5. **Neo4j community 无 RBAC**：`docs/07` 的 U-E 偏离在离线环境同样成立
   （不能靠"只读账号"限制插件写入，只能靠插件层收紧）。
6. **CI runner 不能构建镜像**：仍受 `docs/11` §11.2 约束；出包必须在有 Docker 的机器上做。
7. **单机拓扑**：`standalone` 档把载体与应用放在同一台宿主；跨机部署请用 `attached`，
   并把 `CC_NEO4J_HTTP_URL` / `CC_GRAPHDB_URL` 写成**显式 IP**（`host.docker.internal`
   在 Linux 上指向跑 docker 的那台机器，不是目标机）。

---

## 12. 附录：复核镜像清单

```bash
# 包里声明了哪些镜像
awk -F'|' '!/^#/ && NF>=5 {printf "%-48s %-8s %s\n", $1, $3, $2}' \
    images/IMAGES.lock

# Dify 的镜像清单（出包时由 compose 解析）
cat vendor/dify/IMAGES.txt

# 本机实际载入了哪些 cc-ops 镜像
docker images 'cc-ops/*'
```

`IMAGES.lock` 的 5 个字段：`ref|image_id|architecture|size_bytes|repo_digest`。
`install.sh` 在 `docker load` 之后逐条比对 `image_id`（`sha256:…`，内容摘要），
**不认 tag** —— 这是"验证过的镜像 = 跑起来的镜像"在离线场景下的唯一保证。
