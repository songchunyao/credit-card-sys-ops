# credit-card-sys-ops · 容器化部署指南

面向**运维/交付人员**。目标：在任意 Linux 服务器上，用尽量少的步骤把信用卡跨层运维
智能体的 5 个应用服务跑起来，并**证明它真的能用**（而不是"容器起来了"）。

> 设计依据与取舍记录见 `docs/11-容器化打包与发布方案.md`（给架构评审看）。
> 本文件只讲**怎么做**。

---

## 0. 一分钟上手

```bash
cd <项目根或离线包根>
cp deploy/.env.example deploy/.env
vi deploy/.env                      # 至少填：载体地址、口令、GaussDB DSN/NODES
sudo bash deploy/install.sh --mode standalone    # 新服务器，自带 Neo4j+GraphDB
```

> **完全断网（拿不到 neo4j/graphdb/dify 官方镜像）**：用全量离线包，一条命令装完 ——
> 见 **[deploy/offline/README.md](offline/README.md)**。本文其余章节按"目标机可联网"写。

`install.sh` 会自动走完 7 步并最后打印总账与验收结果。

### 凭据只填一份：`deploy/credentials`

所有需要口令/账号/令牌的地方已收敛成**一份填单**，不必翻遍配置去找：

```bash
cp deploy/credentials.example deploy/credentials
vi deploy/credentials          # 只填标了【需你提供】的项
# 校验「填了没」并**实测「能不能用」**，然后合并进 .env：
python3 deploy/credentials_check.py --sheet deploy/credentials \
        --base http://127.0.0.1 --merge-into deploy/.env
```

校验器对每项分三级判定，**绝不打印口令本体**（只给长度与掩码）：

| 判定 | 含义 |
|---|---|
| ✅ 已提供且**实测可用** | 真的拨了一次（例如 DSN 会去问采集层的 `gaussdb.ok`） |
| ⚠️ 已提供但**未能确认** | 没有可用探测手段或目标不可达 ⇒「没测到」，**不是「测得坏」** |
| ⛔ 未提供 | 并**点名它会卡住哪一项验收**（例如 `GAUSSDB_DSN` → C3c/C3f BLOCKED） |
| ✗ 已提供但**实测失败** | 拨通了但被拒 —— 这才是真的配错了 |

> 该填单的模板可以入库；你填好的 `deploy/credentials` 已被 `.gitignore` 覆盖，**永不入库**。
> 它同样**不会进离线包**：`make_bundle.sh` 出包前会删掉它，并对**其它**除模板以外的凭据类文件
> 直接 `exit 1`（拒绝出包）——`.gitignore` 只挡得住 git，挡不住 `tar` 出来的发布物。
> （2026-10-12 真机 E2E 实测：修之前的包里有 854 B / 22 个非空键的填单，还被登记进了
> `MANIFEST.sha256` 当成正式发布物。该缺陷由 `deploy/selfcheck.py` 的 S9 断言与
> `make_bundle.sh` 的 fail-closed 分支共同锁死，见 S9 的 `s9_bundle_hygiene` 用例。）

---

## 1. 这套东西由什么组成

### 1.1 应用层 5 个服务（本项目的镜像）

| 服务 | 端口 | 作用 | 镜像 |
|---|---|---|---|
| `collect` | **8187** | 采集层数据面：四层探针（gaussdb/os/storage/tpops）+ container/app | `cc-ops/collect` |
| `kg` | **8301** | 方案1 知识服务（KG-Q，Neo4j + Cypher 规则） | `cc-ops/kg` |
| `onto` | **8302** | 方案2 知识服务（ONTO-Q，GraphDB + OWL 2 RL） | `cc-ops/onto` |
| `kgt` | **8303** | 方案3 门面（KG-T，LLM 直引知识图谱，5 工具） | `cc-ops/kgt` |
| `live-facts` | **8304** | 中性取数服务 `read_facts`（三载体与 Dify 免费臂共用） | `cc-ops/live-facts` |

> ⚠ `kg`(:8301) 与 `kgt`(:8303) 是**两个独立容器**。这不是冗余 —— 进程隔离本身
> 就是 KG-T 的纪律护栏：共进程会让 `cc_query` 出现在同一个 OpenAPI 文档里，
> KG-T 立刻退化成 KG-Q。**不要**为了省资源把它们合并。

### 1.2 外部环境依赖（**不在镜像里**，由安装脚本体检/安装）

| 组件 | 端口 | 角色 | 缺失后果 |
|---|---|---|---|
| Neo4j 5-community | 7475(HTTP) / 7688(Bolt) | KG 侧知识载体 | 方案1、方案3 不可用 |
| GraphDB 10.8.0 | 7200 | ONTO 侧知识载体 | 方案2 不可用 |
| Dify 1.17.0 | 80 | 编排层（15 容器） | 编排层不可用；**不阻塞**应用层起栈 |

> ⚠ **端口与惯例不符**：Neo4j 契约用 HTTP **7475**（非 7474）、Bolt **7688**（非 7687）。

---

## 2. 前置条件

| 项 | 要求 | 为什么 |
|---|---|---|
| 操作系统 | Linux | 部署脚本用 `/proc`、`ss`、`sha256sum` 等 |
| 架构 | 与镜像**同架构**（本项目目标机均为 **aarch64**） | 架构不符的镜像跑不起来（exec format error） |
| Docker | 已装且守护进程可达 | — |
| Docker Compose | **v2 插件**（`docker compose` 子命令） | 编排用了 `name:` 与 `depends_on.condition`，v1 不支持 |
| 磁盘 | ≥ 20 GB（`CC_MIN_DISK_GB`） | 镜像与数据卷 |
| 内存 | ≥ 7 GB（`CC_MIN_MEM_MB`）；standalone 档另需 ~6 GB | 5 应用 ≈ 0.6 GB；Neo4j+GraphDB ≈ 6 GB |
| 网络 | 首次部署需能拉官方镜像（neo4j/graphdb/dify）；GaussDB/被观测对象可达 | 完全断网请改用**全量离线包**（[deploy/offline/README.md](offline/README.md)）：载体与 Dify 的镜像随包携带，安装期只 `docker load` |

`install.sh` 的预检会逐项核对，**不达标即拒绝继续**（不会"装到一半才发现磁盘不够"）。

---

## 3. 两种拓扑档：先选对档

```bash
# 档 1：复用既有 Neo4j / GraphDB
sudo bash deploy/install.sh --mode attached

# 档 2：自带 Neo4j + GraphDB
sudo bash deploy/install.sh --mode standalone
```

| | `attached` | `standalone` |
|---|---|---|
| 载体来源 | **既有**实例（可跨机） | compose 用官方镜像拉起 |
| 载体端口 | 必须已被占用（预检会核对） | 必须空闲（预检会拒绝抢端口） |
| 适用 | **192.0.2.11 现网**；已有载体集群 | 任意新服务器；靶场；演示环境 |
| Neode4j 口令 | 可留空，从 `CC_NEO4J_ENV_FILE` 解析 | **必填** `CC_NEO4J_PASSWORD` |

### 为什么 192.0.2.11 必须用 `attached`

三条硬约束（来源 `shared/deploy/targets.yaml` 实测与裁决 13）：

1. 宿主内存仅余约 **5 GB**、swap 已用约 3 GB ⇒ **不得新增常驻容器**；
2. 该机已有 `neo4j-gaussdb`，其中是**既有 423 节点 v2 图**，`must_not` 明列
   「新建第二个 Neo4j 容器」「清空或改动既有 423 节点 v2 图」；
3. 该机已有 `gdo-graphdb`，含 `gdo*` / `starwars` 仓库，`must_not` 明列「改写既有仓库」。

`install.sh` 的预检会在 `standalone` 档检出这些端口被占用并**直接拒绝**，不会硬闯。

---

## 4. 配置 `deploy/.env`

```bash
cp deploy/.env.example deploy/.env && vi deploy/.env
```

### 4.1 必须放在对的位置

`deploy/.env`。**compose 只用「compose 文件同目录」的 `.env` 做变量插值** ——
放到仓库根，compose 不报错，所有 `${VAR}` 会**静默走默认值**（上游实测踩过这个坑）。

### 4.2 最小必填集

| 场景 | 必填键 |
|---|---|
| 任何档位 | `CC_IMAGE_TAG` |
| `attached` | `CC_NEO4J_HTTP_URL`、`CC_GRAPHDB_URL` |
| `standalone` | `CC_NEO4J_PASSWORD` |
| 要真正采到数据 | `GAUSSDB_DSN`、`GAUSSDB_NODES`（+ 视需要 `GAUSSDB_SSH_PASSWORD`、`MCP_STORAGE_DM_*`、`TPOPS_*`、`CC_APP_URL`、`K8S_PROM_URL`） |

> ★ 只填载体不填 GaussDB，服务会**起得来但采不到事实**。预检会就此大声告警，
> 验收矩阵的 C3c 会判为 `BLOCKED`（未测到，不是测坏）。

### 4.3 采集层环境变量权威表

**以代码实测为准**（`deploy/selfcheck.py` 会断言模板覆盖了代码读取的全部 61 个变量）。
旧的 `dist/collect-layer/.env.example` **已漂移，不要照它配**：

| 域 | 权威键名 | 漂移写法（**不存在**，写了也不报错、只会静默采空） |
|---|---|---|
| 存储域 Dorado | `MCP_STORAGE_DM_BASE_URL` / `_USER` / `_PASSWORD` / `_VERIFY_SSL` | ~~`DORADO_HOST`~~、~~`DORADO_PORT`~~、~~`DORADO_API_BASE`~~ |
| L2 容器 | `K8S_PROM_URL` / `K8S_PROM_TIMEOUT` | ~~`PROMETHEUS_URL`~~ |
| L1 应用 | `CC_APP_URL` / `CC_APP_TIMEOUT` | ~~`CC_APP_HEALTH_URL`~~ |
| 日志检索 | `K8S_ES_HOST` | ~~`K8S_ES_URL`~~ |
| 服务监听 | `SERVICE_HOST` / `SERVICE_PORT` | ~~`SVC_PORT`~~（后者只给 uvicorn 用） |

> ★ 分层后数据面**已无 Neo4j 依赖**（`collect_api.py` 只保留"由知识面注册"的占位）。
> 故 compose **刻意不**注入 `NEO4J_HTTP` / `NEO4J_AUTH` —— 那是分层前的残留。

---

## 5. 一键部署

```bash
sudo bash deploy/install.sh --mode standalone
```

7 个步骤：

| 步 | 内容 | 失败行为 |
|---|---|---|
| ① | 环境预检（OS/架构/Docker/Compose/端口/磁盘/内存/.env 键） | 硬前置缺失 → `FAIL(1)`；环境不满足 → `BLOCKED(2)` |
| ② | 载入 5 个镜像（自动识别就地离线包） | 缺镜像 → 拒绝继续，**绝不用旧镜像冒充** |
| ③ | 外部依赖体检 / 缺了从官方镜像源安装 | 载体缺失 → `BLOCKED`；仅 Dify 缺失 → 告警不阻断 |
| ④ | 契约装载（GraphDB 建仓 + TBox；Neo4j TBox），幂等 | 失败 → 记录 FAIL，继续（便于看全部问题） |
| ⑤ | `docker compose up -d --pull never` | 失败 → 打印诊断命令 |
| ⑥ | 等 5 个服务健康（默认 180 s） | 超时 → 逐个打印日志尾部 |
| ⑦ | 验收矩阵 `verify_deploy.py` | 不达标 → 记录 FAIL |

常用参数：

```bash
--mode attached|standalone     拓扑档（默认读 .env 的 CC_DEPLOY_MODE）
--deps check|install           外部依赖只体检 / 自动装（默认 install）
--bundle DIR                   指定离线包目录（就地离线包会自动识别，无需手写）
--no-init                      跳过契约装载（TBox 已装过）
--skip-verify                  跳过验收 ★ 该部署**不得视为已验收**
```

退出码：`0`=PASS　`1`=FAIL　`2`=BLOCKED（环境不满足，不是代码缺陷）

### 三条纪律

1. **绝不抢占既有服务**：应用端口或载体端口被占用即拒绝，并打印占用者。
2. **绝不静默通过**：缺凭据、缺依赖一律显式报出，且区分 `FAIL` 与 `BLOCKED`。
3. **幂等**：可反复执行；契约装载本身幂等，`compose up` 本身幂等。

---

## 6. 从离线包安装（断网客户环境）

本项目有**两种**离线包，先选对：

| | **全量离线包**（断网首选） | 应用层离线包 |
|---|---|---|
| 含什么 | 应用层 5 镜像 + **Neo4j + GraphDB + Dify** 全部镜像与源码树 | 只有自建 5 个镜像 |
| 目标机首次安装要外网？ | **不要**（全程只 `docker load`） | **要**（拉 neo4j/graphdb/dify） |
| 出包命令 | `sh deploy/offline/make_offline_bundle.sh --tag <tag> --archive` | `sh deploy/make_bundle.sh --tag <tag>` |
| 手册 | **[deploy/offline/README.md](offline/README.md)** | 本节 §6.2 |

### 6.1 全量离线包（目标机完全断网）

```bash
# 构建机（有 Docker + 与目标同架构；能访问镜像源）
sh deploy/build_images.sh --tag <tag>
sh deploy/offline/make_offline_bundle.sh --tag <tag> --archive

# 目标机（断网 Linux）
tar xzf ccops-offline-<tag>.tar.gz && cd ccops-offline-<tag>
sha256sum -c MANIFEST.sha256
sudo bash install-offline.sh
```

`install-offline.sh` 只是入口，真正的编排仍在 `deploy/install.sh --offline`。
离线模式下：载体与 Dify 起栈一律 `--pull never`；`docker load` 之后按
`images/IMAGES.lock` 逐条核对 image Id（**tag 可 retag，Id 不能**）；Dify 用包内
`vendor/dify/docker` 启动、不 clone 不联网；口令在目标机首次安装时随机生成（不打印）。
详见 [deploy/offline/README.md](offline/README.md)。

### 6.2 应用层离线包（目标机首次可联网）

```bash
# 1. 传输并解包
tar xzf ccops-release-<tag>.tar.gz && cd ccops-release-<tag>

# 2. 校验（不通过即停）
sha256sum -c MANIFEST.sha256

# 3. 配置
cp deploy/.env.example deploy/.env && vi deploy/.env

# 4. 一键部署（--bundle 可省：就地离线包会被自动识别）
sudo bash deploy/install.sh --mode standalone

# 5. 验收（install.sh 已自动跑；也可单独跑）
python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone
```

这个包里**不含** neo4j / graphdb / dify（它们是「安装环境依赖」）。若目标机完全断网，
请改用 §6.1 的全量离线包；或预先在目标机装好这三者，再用 `--deps check` 部署。

### 发布者：怎么产出离线包

> ⚠ **共享式容器化 runner 通常构建不了镜像（已实测确认）**：docker executor 常配
> `DOCKER_HOST=tcp://docker:2375`，但**未开 privileged**、**未挂**宿主
> `/var/run/docker.sock`（补上 dind 服务后，dind 又因 `mount: permission denied` 起不来）。
> 这类构建作业的正确行为是**打印精确诊断并 `exit 2`（BLOCKED）**，而不是伪装成功。

因此镜像要在**真有 Docker** 的机器上构建，且**架构必须与目标机一致**
（目标机 aarch64 ⇒ 构建机也必须 aarch64）。例如直接在装了 Docker 的构建机上运行：

```bash
# 在 aarch64 + 有 Docker 的机器上（例如 CI 主机 192.0.2.10）
sh deploy/build_images.sh --tag <tag>          # 构建 5 个镜像
sh deploy/make_bundle.sh --tag <tag> --out dist/release   # 打离线包
# 或一步到位：
sh deploy/build_images.sh --tag <tag> --bundle
```

`build_images.sh` 自带 docker 可用性预检与**逐镜像**结果，任一镜像失败即拒绝出包
（不用不完整的镜像集去交付）。

`make_bundle.sh` 是 **POSIX sh**（CI 镜像是 Alpine，没有 bash），产物自带
`MANIFEST.sha256`（只记相对路径）与 `VERSION`（其中 `CC_IMAGE_TAG` 是权威）。

包里另带**基底快照** `tmp/ccf_live5.json`：`install.sh` ④.0b 靠它投影「快照命名图」，
而 ONTO **按快照命名图作用域** —— 少了它，干净载体上 `onto` 起不来（报错还不会点名"你还没投影"）。
同时它**拒绝**把填好的凭据单打进包（见上文）。

> ⚠ **开发机不能构建镜像**：本项目开发机是 x86_64 且未安装 Docker，而目标机是 aarch64。

#### 已验证的完整实录（2026-10-11，`192.0.2.10`）

```bash
# 1) 源码落地（用 git archive，只带已提交内容，不涉凭据）
git archive --format=tar.gz -o /tmp/ccops-src.tar.gz HEAD
#   scp 到目标机后：mkdir -p /opt/cc-ops && tar xzf ... -C /opt/cc-ops

# 2) 构建 5 个镜像（aarch64，rc=0；collect 208MB / kg 198MB / onto 198MB / kgt / live-facts）
sh deploy/build_images.sh --tag ci-arm64-v1

# 3) 配置（cp .env.example .env 后填；载体在**另一台**机器时必须写显式 IP，
#    不能用 host.docker.internal —— 那个名字解析到跑 docker 的本机）
#    ★ 数值键（LOG_MSG_MAX 等）**绝不能留空**，见 compose.yml 的「数值键」说明
# 4) 起栈（--no-init：TBox 已在共享载体上，不重复装载）
bash deploy/install.sh --mode attached --deps check --no-init

# 5) 在**宿主上**跑验收（这样 C1 容器健康 / C2 镜像架构 / C9 审计链 才可测）
python3 deploy/verify_deploy.py --deploy-dir deploy --mode attached
```

实测结果：**5/5 容器 healthy**；验收矩阵 **`PASS=17 FAIL=0 BLOCKED=1`**
（唯一 BLOCKED = `GAUSSDB_DSN` 在该环境不可得）。

拆栈：

```bash
cd /opt/cc-ops
docker compose -f deploy/compose.yml down            # 停容器，保留审计卷
docker compose -f deploy/compose.yml down -v         # 连卷一起删
```

---

## 7. 验收矩阵

`deploy/verify_deploy.py` 把「能用」变成可复算的断言：

| 编号 | 检查项 | 判据 |
|---|---|---|
| C1 | 容器健康 | 5 个 `cc-ops-*` 全部 healthy/running |
| C2 | 镜像架构与本机一致 | 不一致直接 FAIL（架构不符根本跑不起来） |
| C3 | 采集层 `/health` 可达 | HTTP 200 |
| C3b | 采集面端点面非空 | `/tools/*` 端点 ≥ 6（真机实测为 8） |
| C3c | GaussDB 可达 | `gaussdb.ok=true`（否则 `BLOCKED`） |
| C3d | **分层边界** | `rules.ok=false` 且理由点名 `service.knowledge_api` —— 这是**正面证据**：数据面没把知识面缠回来（docs/50 §4.1）。若变 `true` 才是 FAIL |
| C3e | 采集源非模拟 | `collect_source.simulated != true`（模拟模式下的读数不得当验收依据） |
| C3f | 真实采集事实非空 | 仅 `--with-gather`：POST `/tools/gather`，断言 `all_facts` > 0。**若被工具输出护栏拦下（`loop_detected=true`）则判 `BLOCKED`** —— 那是设计行为，不是探针故障 |
| C4 | 方案1 KG-Q | `/health` 200；工具面非空（真机实测 8 个） |
| C5 | 方案2 ONTO-Q | `/health` 200；工具面非空；GraphDB 存在 `cc-ops` 仓库 |
| C6 | 方案3 KG-T | `/health` 200；`GET /tools` 工具面非空（真机实测 5 个） |
| C7 | 取数面 | `read_facts` 可调用且**三段非空壳**（不满足于"有返回"） |
| C8 | 工具面同形 | KG 与 ONTO 的非基础设施路由集合**逐条一致**（硬约束 C3；真机实测 8 条） |
| C9 | 审计链落盘（R-27） | 打一次**成功**（2xx）的工具调用，审计行数**必须增长** |
| C10 | 重启存活 | 可选、危险，需 `--with-restart` 显式开启 |

```bash
# 常规（C1–C9；C1/C2/C9 需 docker）
python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone

# 外加一次真实采集（会真的查库/SSH），最强的"探针真能采"证据
python3 deploy/verify_deploy.py --deploy-dir deploy --mode attached \
        --base http://192.0.2.11 --with-gather --out /tmp/verify.json
```

### ⚠ 一个必须知道的形状事实：`collect_contract` 在数据面**不存在**

首次对着真机跑时，本矩阵曾断言 `/health.collect_contract.probe_sections` 非空 —— 那是照
上游 `docs/49` §1.3 写的，而 §1.3 描述的是**分层前的 monolith**。真机实测
`:8187/health` 顶层只有 `collect_cache / collect_source / gaussdb / rules / service / ts / version`。

**没有 `collect_contract` 不是故障，是设计**：
- `app_collect.py` 自述「`/health` 的 neo4j / rules / collect_contract 块为『未加载』提示；不 import `service.knowledge_api` → 不引入 Neo4j 依赖」；
- `collect_api.py` 自述这三块「由知识面（`service.knowledge_api`）注册」。

⇒ 断言它等于断言一个**已被架构演进删掉的东西**（假红）。现改为断言真正有鉴别力的一组：
分层边界 + 采集源模式 + 端点面 + GaussDB 连通（见上表 C3b–C3e）。

### 真机基线（供对照，**非容器化**状态）

2026-10-11 对 `192.0.2.11`（当时仍是 systemd 裸进程部署）实跑：
`PASS=16 FAIL=0 BLOCKED=3`（3 项 BLOCKED 全是需 docker 的 C1/C2/C9，本机无 docker）。
读数：采集面 8 端点 · GaussDB Kernel **507.0.0** · `collect_source=live simulated=false` ·
KG-Q 8 工具 · ONTO-Q 8 工具 · KG-T 5 工具 · GraphDB 6 仓库（含 `cc-ops`）·
`read_facts` 三段 objects=5 · `--with-gather` 采到 **509 条事实**。

> ★ 这是**服务面**基线，用于校验本矩阵自身是否有鉴别力；
> 它**不能**用来证明镜像可用（当时跑的不是容器）。

### ⚠ 连续重复验收会被「工具输出护栏」拦下（不是故障）

`collect` 的 `_loop_guard`（所有端点共用，护栏窗口 **600 s**）会识别"同一参数被连续重复调用"，
并返回 **`{"loop_detected": true, "repeat_count": N, "instruction": "STOP。…"}`** 而**不是**事实。

后果：**在 600 s 内反复跑 `--with-gather`，第 3 次起就会命中护栏** —— 若验收器照旧去读
`all_facts`，会读到"缺失 ⇒ 0 ⇒ FAIL"，把一个**正确行为**判成探针故障。
本矩阵已按设计处理：识别护栏应答并判 **`BLOCKED`（未测到）**，同时打印
`repeat_count` 与"请隔 ≥600 s 重跑"的提示（同一条纪律也用在 C9 的审计触发上）。
⇒ 做发布验收时**不要连打**，或把 `--with-gather` 留到需要出证据的那一次。

### `BLOCKED ≠ FAIL`

- `FAIL` = 测得坏结果（例如工具面不同形、审计链没追加）
- `BLOCKED` = **没测到**（例如无 docker、GaussDB 未连通）

项目纪律：**「没测到」不得当「通过」，也不得当「失败」**。`BLOCKED` 需要人去看一眼，
而不是被当成噪声忽略。

---

## 8. 日常运维

```bash
D="-f deploy/compose.yml"                        # attached 档
D="-f deploy/compose.yml -f deploy/compose.standalone.yml"   # standalone 档

docker compose $D ps
docker compose $D logs -f kg
docker compose $D restart live-facts
docker compose $D down          # 停止（保留卷：审计链与载体数据都在）
docker compose $D down -v       # ★ 连卷一起删 —— 载体数据与审计链都会没
```

### 切换 M5 用例（`live-facts`）

用例是**进程级**信道（裁定 D48）：容器起来后**不可改**，必须重建。

```bash
M5_CASE_ID=A-02 docker compose $D up -d --force-recreate live-facts
# 留空 = live 模式：M5_CASE_ID= docker compose $D up -d --force-recreate live-facts
```

### 审计链

Kg/Onto/KgT 的审计链写在命名卷 `ccops-audit` 的 `/var/lib/cc-ops/audit/*.jsonl`，
**追加式**、跨重启保留。查看：

```bash
docker run --rm -v ccops-audit:/a alpine wc -l /a/cc_tools_audit.jsonl
```

> ★ 刻意**不**把审计路径做成 `.env` 可配项：允许改它，等于允许把审计链写进容器可写层，
> 一重建容器就丢。

---

## 9. 排障手册

| 症状 | 最可能的原因 | 怎么定位 |
|---|---|---|
| 容器起来了但采集全空 | `GAUSSDB_DSN` / `GAUSSDB_NODES` 没填，或键名写错 | `curl -s localhost:8187/health` 看 `gaussdb.ok`；`deploy/selfcheck.py` 查键名漂移 |
| 存储层没有事实 | `MCP_STORAGE_DM_BASE_URL` 空（未配置即不采，属预期） | 同上 |
| `kg`/`kgt` 报缺口令 | `CC_NEO4J_PASSWORD` 与 `CC_NEO4J_ENV_FILE` 都没提供 | 预检会提示；两服务均 **fail-closed**，绝不回落默认口令 |
| `onto` 报载体不可达 | `CC_GRAPHDB_URL` 写错，或 GraphDB 未启用 | `curl -s <url>/rest/repositories` |
| 容器连不上宿主载体 | Linux 上 `host.docker.internal` 默认不存在 | compose 已加 `extra_hosts: host-gateway`；若仍不通查宿主防火墙 |
| `live-facts` 启动即退出 | 部署闭包缺件（基底快照 `tmp/ccf_live5.json` 等 9 件） | 镜像**构建期**已断言这 9 件，故更可能是卷/权限问题 |
| `read_facts` 返回 `UPSTREAM_UNAVAILABLE` | 取数面不可达 | 检查 `CC_LIVE_FACTS_URL` 是否误写成 `127.0.0.1`（容器内指向自己） |
| 工具面同形断言 C8 红 | 两侧框架/Python 版本漂移，或有人改了一侧工具定义 | 两侧镜像**必须**用同一组依赖版本（见 Dockerfile.kg / .onto 注释） |
| `docker load` 后起来就 exit | 架构不符 | 验收 C2 会直接点名 |
| compose 变量像没生效 | `.env` 不在 `deploy/` 下 | 只能放在 compose 文件同目录 |

---

## 10. 安全说明

- **镜像内零密钥**：全部配置运行时注入；`.dockerignore` 排除 `.env`，构建期还会删一遍残留。
- **按需注入**：compose 刻意不用 `env_file:`（那会把整份 `.env` 撒进每个容器），
  各服务只拿到自己在 `environment:` 里声明的那几个键。
- **非 root 运行**：5 个镜像均以 UID 10001 的 `cco` 用户运行。
- **凭据文件**：填充后的 `deploy/.env` 已被 `.gitignore` 覆盖，**永不入库**。
- **端口暴露**：默认绑 `0.0.0.0`。只允许本机访问就设 `CC_BIND_IP=127.0.0.1`
  （但注意 `:8304` 需要 Dify 从宿主外访问，一般保持 `0.0.0.0`）。
- `deploy/selfcheck.py` 的 S6 断言会对发布物做**硬编码口令扫描**，CI 阻塞档每次都会跑。
