# credit-card-sys-ops · 全量离线部署包（@TAG@）

> 构建时间：@BUILT_AT@　目标平台：@PLATFORM@（@TARGET_ARCH@）
> 本包**不需要目标机访问互联网**：应用层 5 镜像、Neo4j、GraphDB 全部随包携带。
> @DIFY_SENTENCE@
> 安装脚本在整个流程里**只执行 `docker load`，不会执行 `docker pull`**。

---

## 1. 包里有什么

| 路径 | 内容 |
|---|---|
| `install-offline.sh` | **一键入口**（推荐只用这个） |
| `images/ccops-app-images.tar.gz` | 自建应用层 5 镜像：`collect` / `kg` / `onto` / `kgt` / `live-facts` |
| `images/20-carriers-images.tar.gz` | `neo4j:5-community` + `ontotext/graphdb:10.8.0` |
| `images/30-dify-images.tar.gz` | Dify @DIFY_VERSION@ 编排层全部镜像（合一包，共享层只存一份） |
| `images/IMAGES.lock` | 每个镜像的 `ref｜image Id｜架构｜字节数｜digest`；装载后逐条核对 |
| `vendor/dify/docker/` | Dify 官方 `docker/` 编排树（含 nginx / ssrf_proxy 配置） |
| `vendor/dify/IMAGES.txt` | 出包时由 `docker compose config --images` 解析出的镜像清单（可复核） |
| `deploy/` `shared/` `scheme1-kg/` `scheme2-onto/` | 源码树与部署脚本（契约装载要读 `shared/deploy/targets.yaml`） |
| `VERSION` | 权威声明：镜像标签、平台、Dify 版本、磁盘下限 |
| `MANIFEST.sha256` | 全量校验（相对路径；`sha256sum -c` 可直接用） |

**不在包内（也不该在）**：任何 `deploy/.env`、任何填好的凭据、Dify 的 `.env`
（Dify 的 `.env` 在安装时用 `vendor/dify/docker/.env.offline-template` 现场生成，
权限 0600，口令随机且**不打印**）。

---

## 2. 目标机要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux（x86_64 或 aarch64，须与 `@PLATFORM@` 一致） | 脚本用 `/proc`、`ss`、`sha256sum` |
| Docker | 已安装且守护进程可达 | `docker info` 能通 |
| Docker Compose | **v2 插件**（`docker compose` 子命令） | 本编排用了 `name:` / `depends_on.condition` |
| bash | ≥4（`install.sh` 与 `install-offline.sh` 都是 bash） | 目标机通常自带；Alpine 类精简系统需先装 |
| 磁盘 | **≥ @DISK_GB@ GB** 可用（docker 数据盘） | 镜像解压 + 载体卷 + Dify 库 |
| 内存 | 建议 ≥ 16 GB（Dify 约 4–6 GB、载体约 3 GB、应用层约 1 GB） | 低于 6 GB 可用内存时 Dify 会自动拒绝安装并判 BLOCKED |
| Python | **python3（≥3.8）必需** | `install.sh` ④.0 在宿主上跑 GraphDB 建仓脚本；验收矩阵也是 Python |
| HTTP 客户端 | `curl` 或 `wget` 至少一个 | 宿主侧探活载体 / Dify 就绪 |
| 网络 | **不需要外网**；但需要能访问被观测对象（GaussDB / Dorado / TPOPS 等） | 采集层没配这些会"起来但采不到事实" |

> ⚠ 若目标机 :80 / :443 已被占用，安装脚本会自动把 Dify 换到 8080 / 8443。
> 若 :7475 / :7688 / :7200 已被**别的服务**占用，`standalone` 档会**拒绝安装**
> （本项目明令禁止新建第二个 Neo4j / 改写既有 GraphDB 仓库）——此时请改用 `--mode attached`。

---

## 3. 一键安装（三步）

```bash
# ① 校验（不通过就停；不要跳过）
cd ccops-offline-@TAG@
sha256sum -c MANIFEST.sha256

# ② 一键安装（自带 Neo4j + GraphDB；默认 standalone）
sudo bash install-offline.sh

# ③ 看验收结果
#    末行应为 PASS=… FAIL=0 BLOCKED=…（BLOCKED 是"没测到"，不是"通过"，需人看）
```

安装会依次做：离线包自检 → 环境预检 → 载入全部镜像（并核对 `IMAGES.lock`）→
起自带载体 → 装 GraphDB 仓库/快照/TBox、Neo4j TBox → 起应用层 6 容器 → 等健康 →
跑验收矩阵 → 用包内源码树起 Dify → 初始化 Dify 管理员。

### 常用参数

```bash
sudo bash install-offline.sh --mode attached     # 复用既有 Neo4j/GraphDB（宿主 7475/7688/7200）
sudo bash install-offline.sh --no-dify           # 只交付应用层 6 服务（Dify 复用既有实例）
sudo bash install-offline.sh --skip-verify       # 跳过验收（**该部署不得视为已验收**）
sudo bash install-offline.sh --no-init           # 跳过契约装载（TBox 已装过）
```

退出码：`0`=PASS　`1`=FAIL　`2`=BLOCKED（环境不满足，不是代码缺陷）。

---

## 4. 装完以后

| 入口 | 地址 |
|---|---|
| 采集层数据面 | `http://<IP>:8187/health` |
| 方案1 KG-Q | `http://<IP>:8301/health` |
| 方案2 ONTO-Q | `http://<IP>:8302/health` |
| 方案3 KG-T | `http://<IP>:8303/health` |
| 中性取数 `read_facts` | `http://<IP>:8304/health` |
| 方案3 补充臂 KG-T-dyn | `http://<IP>:8305/health` |
| Dify 控制台 | `http://<IP>:<CC_DIFY_HTTP_PORT>`（见 `deploy/.env`） |

账号与口令**不打印**，全部在 `deploy/.env`（0600）里。**另有一份** `vendor/dify/docker/.env`
（Dify 自己的库/Redis/签名密钥）—— 升级/回滚时两份要一起备份，否则新口令配旧库会起不来。

```bash
grep -E '^(DIFY_EMAIL|DIFY_BASE_URL|CC_DIFY_HTTP_PORT)=' deploy/.env
# DIFY_PASSWORD / CC_NEO4J_PASSWORD 也在同一文件里，请按本单位口令管理规定保管
```

### 还差三件"只能人工"的事

1. **配模型供应商**：Dify 控制台 → 设置 → 模型供应商（DeepSeek / OpenAI 兼容端点）。
   断网环境请指向内网 LLM 网关；没有模型，Agent 无法工作。
2. **注册工具面**（把 `:8301`–`:8305` 注册成 Dify provider）：
   ```bash
   sudo bash deploy/offline/wire_dify.sh --check   # 先只读看差异
   sudo bash deploy/offline/wire_dify.sh --apply   # 再幂等写入
   ```
3. **本地 fork 插件**（仅 `KG-T-free` 组需要）：见 `deploy/offline/README.md` §7.4。
   插件守护进程默认校验签名，内网自签包会被拒；处置是**一次性**放宽并在装完立即恢复。

---

## 5. 运维命令

```bash
D="-f deploy/compose.yml -f deploy/compose.standalone.yml"   # standalone 档
# attached 档去掉第二个 -f

docker compose $D ps
docker compose $D logs -f kg
docker compose $D restart live-facts
docker compose $D down          # 停止（保留卷：审计链与载体数据都在）
docker compose $D down -v       # ★ 连卷一起删 —— 载体数据与审计链都会没

# Dify（独立 compose 工程）
cd vendor/dify/docker && docker compose ps
cd vendor/dify/docker && docker compose logs -f api
```

---

## 6. 排障（只列离线特有的）

| 症状 | 原因 | 处置 |
|---|---|---|
| `镜像身份不符：… 包声明 sha256:… 本机实际 sha256:…` | tar 被替换/截断，或本机已有同名不同内容的镜像 | 重新拷贝本包并 `sha256sum -c MANIFEST.sha256`；不要手工 `docker tag` |
| `exec format error` / 容器 Exit 255 | 镜像架构与目标机不符 | 在**同架构**机器上重新出包（`--platform`） |
| `No such image` 且提示 `--pull never` | 该镜像没进包（出包时 compose 解析漏了） | 回构建机用同一版本重出包；不要在目标机手工 pull |
| Dify 反复重试插件市场 / 日志大量超时 | 断网但未关市场 | 确认 `vendor/dify/docker/.env` 有 `MARKETPLACE_ENABLED=false` |
| Dify 起不来且 `deploy/.env` 的 `CC_DIFY_MIN_MEM_MB` 过高 | 内存不足被硬闸拒绝 | 降低阈值前先确认余量；或 `--no-dify` 复用既有 Dify |
| Dify 报数据库连接失败，而 `volumes/db/data` 有数据 | `vendor/dify/docker/.env` 被删/换过 ⇒ 新口令与旧库不匹配 | 找回旧 `.env`；或确认可丢数据后 `cd vendor/dify/docker && docker compose down -v` 重装 |
| 采集层起来但事实全空 | `GAUSSDB_DSN` / `GAUSSDB_NODES` 没填 | 填 `deploy/.env` 后重跑；验收 C3c 会判 BLOCKED |
| 脚本报 `$'\r': command not found` | 文件被转成 CRLF | `sed -i 's/\r$//' <file>`；本包自检 N1 会提前发现 |

---

## 7. 回滚 / 升级

> ⚠ **Dify 的数据是"包内 bind mount"（`vendor/dify/docker/volumes/`）**，换一个包目录
> 就等于换了一套空数据；应用层审计链与 Neo4j/GraphDB 用的是**命名卷**，换目录不受影响。

- **升级（保留 Dify 数据）**：

  ```bash
  OLD=ccops-offline-<旧tag>; NEW=ccops-offline-<新tag>
  ( cd $OLD/vendor/dify/docker && docker compose down )      # 同项目名 dify，先停
  tar xzf $NEW.tar.gz
  cp -a $OLD/deploy/.env             $NEW/deploy/.env
  cp -a $OLD/vendor/dify/docker/.env $NEW/vendor/dify/docker/.env
  if [ -d $OLD/vendor/dify/docker/volumes ]; then
    rm -rf $NEW/vendor/dify/docker/volumes
    cp -a $OLD/vendor/dify/docker/volumes $NEW/vendor/dify/docker/volumes
  fi
  cd $NEW && sudo bash install-offline.sh
  ```

  `install.sh` 会用新包 `VERSION` 覆盖 `.env` 的 `CC_IMAGE_TAG`，不会"装成旧镜像"。
- **回滚**：与升级对称（把新包的 volumes 与两份 `.env` 拷回旧包目录，再跑旧包）。
- **彻底清理**：`docker compose ... down -v` 会删掉载体数据与审计链，**不可恢复**，先备份。

更多细节（出包、镜像清单、Dify 离线行为、安全边界）见包内
`deploy/offline/README.md`。
