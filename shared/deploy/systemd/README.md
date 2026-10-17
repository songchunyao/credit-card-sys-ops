# `shared/deploy/systemd/` —— 知识服务的开机自启（systemd unit）

本目录是 `192.0.2.10` 上**四个知识服务**的 systemd unit **权威副本**（与主机上
`/etc/systemd/system/*.service` 逐字一致）。纳入仓库的理由：**它们是"服务能否被访问"的一部分，
不该只存在于某台机器的 `/etc` 里**。

| unit | 端口 | 启动脚本（复用项目自带） | PIDFile | 特殊环境 |
|---|---|---|---|---|
| `cc-tools-kg.service` | **8301** | `/opt/cc-tools-kg/start_cc_tools.sh` | `/opt/cc-tools-kg/cc_tools.pid` | — |
| `cc-tools-onto.service` | **8302** | `/opt/cc-tools-onto/start_cc_tools_onto.sh` | `/opt/cc-tools-onto/cc_tools_onto.pid` | — |
| `cc-tools-kgt.service` | **8303** | `…/kgt/scheme1-kg/service/start_cc_tools_kgt.sh` | `/opt/cc-tools-kg/kgt/cc_tools_kgt.pid` | ★ `CC_KGT_DIR=/opt/cc-tools-kg/kgt` |
| `cc-live-facts.service` | **8304** | `/opt/cc-live-facts/shared/collect/start_cc_live_facts.sh` | `/opt/cc-live-facts/cc_live_facts.pid` | ★ `M5_CASE_ID=A-01` |

---

## 1. 为什么会有这套 unit（真事故，2026-10-08）

`192.0.2.10` 于 **09:45:25 重启**，而四个知识服务**当时完全没有自启机制**
（核过：无 systemd unit（连 disabled 的都没有）、无 `crontab @reboot`、无 `/etc/rc.local`，
只有手工 `start_*.sh`）⇒ **09:45–11:27 之间 `:8301` 等端口是死的**。

用户在此期间手动跑 Dify agent，得到：

```
检查 cc_guide 服务状态异常：目标地址为 192.0.2.10:8301
```

**机制（四步，均有证据）**：

1. 服务没起来 ⇒ 端口无监听；
2. Dify 的工具出站走 **SSRF 代理**（`ssrf_proxy:3128`，白名单含 `192.0.2.10/32`）；
3. 上游不可达时**代理返回 503** —— 而 `cc_tools.py` 里**根本没有 503 这条路径**（已 grep 确认），
   所以该 503 **是代理发的，不是我们的服务**；
4. Dify 日志原样：`2026-10-08 03:23:46 POST http://192.0.2.10:8301/guide "HTTP/1.1 503 Service Unavailable"`
   （`03:23:46Z` = 本地 **11:23:46**，而手工重启发生在 **11:27:35**，只差 4 分钟）。

⇒ **症状是"某个工具的服务状态异常"，根因却在"主机重启后没人把服务拉起来"。**
这套 unit 就是把这个根因消除。

---

## 2. 安装（在一台新机器上复现）

```bash
# 1) 把四个 unit 放到 /etc/systemd/system/（本目录即权威副本）
cp shared/deploy/systemd/cc-tools-kg.service     /etc/systemd/system/
cp shared/deploy/systemd/cc-tools-onto.service   /etc/systemd/system/
cp shared/deploy/systemd/cc-tools-kgt.service    /etc/systemd/system/
cp shared/deploy/systemd/cc-live-facts.service   /etc/systemd/system/
chmod 644 /etc/systemd/system/cc-*.service

# 2) 让 systemd 看到它们
systemctl daemon-reload

# 3) 开机自启
systemctl enable cc-tools-kg cc-tools-onto cc-tools-kgt cc-live-facts

# 4) 首次启动（若服务已由手工脚本起着，**必须先停**，否则启动脚本会因端口被占而拒绝）
systemctl start cc-tools-kg cc-tools-onto cc-tools-kgt cc-live-facts
```

---

## 3. 验证

```bash
# 状态
systemctl is-enabled cc-tools-kg cc-tools-onto cc-tools-kgt cc-live-facts   # 期望 enabled
systemctl is-active  cc-tools-kg cc-tools-onto cc-tools-kgt cc-live-facts   # 期望 active
for p in 8301 8302 8303 8304; do
  echo ":$p -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$p/health)"
done                                                                        # 期望全 200

# ★ :8303 的审计必须落在**声明路径**（否则见 docs/00 的 R-54-DEV）
pid=$(systemctl show -p MainPID --value cc-tools-kgt.service)
tr '\0' '\n' < /proc/$pid/environ | grep -E 'CC_KGT_DIR|CC_AUDIT_LOG'
# 期望 CC_AUDIT_LOG=/opt/cc-tools-kg/kgt/cc_tools_kgt_audit.jsonl
```

2026-10-08 实测（**冷路径**：按 pidfile 停掉手工进程 → 端口确认为空 → `systemctl start`）：

```
cc-tools-kg      active  MainPID=1116205    :8301 health=200
cc-tools-onto    active  MainPID=1116250    :8302 health=200
cc-tools-kgt     active  MainPID=1116317    :8303 health=200   CC_KGT_DIR/CC_AUDIT_LOG 正确
cc-live-facts    active  MainPID=1116370    :8304 health=200
```

---

## 4. 设计要点（为什么这么写）

- **`Type=forking` + `PIDFile`，而不是 `Type=simple` 直接写 `ExecStart=python …`**：
  启动脚本自己 `nohup` 后台化并写 pidfile，且**必须**由脚本承担这些既有职责 ——
  审计落盘路径（`CC_AUDIT_LOG`）、口令从 `/opt/upstream-agent/.env` 提取、`PYTHONPATH` 拼装，
  以及 **`:8303` 的 `CC_KGT_DIR`**（漏了它审计会写到非声明路径，见 `docs/00` 的 **R-54-DEV**）。
  在 unit 里重写一份等价 `ExecStart` 等于把那些坑重造一遍。
- **`Restart=on-failure` + `RestartSec=10`**：进程意外死亡时自动拉起。
- **`After=network-online.target docker.service`**：`:8301`/`:8302` 依赖 Neo4j、`:8302` 依赖 GraphDB，
  它们都是容器 ⇒ 排在 docker 之后（已核实 `docker.service` 在本机存在且 `enabled`）。
  注意 `After=` **只排序不保证就绪**；知识服务是**请求级惰性连库**，故后端稍后就绪也可接受。
- **★ `ExecStartPre` 后端就绪门（2026-10-08 由真机重启演练发现后新增）**：
  `cc-tools-kg` / `cc-tools-kgt` 等 `:7688`（Neo4j bolt）可连；
  `cc-tools-onto` 轮询 `curl -sf http://127.0.0.1:7200/rest/repositories`（GraphDB REST）直到成功，
  最长等 **300 s**。理由见 §7：**`:8302` 在启动时就要查 GraphDB**，而 GraphDB（JVM）重启后
  需要 >120 s 才答得出查询 —— 没有这道门，服务会被"后端未就绪"直接判死。
  ★ 实现注意：**unit 文件里 `%` 是 systemd 说明符** ⇒ 不要用 `curl -w "%{http_code}"`，
  改用 `curl -sf -o /dev/null <url>` 判退出码（否则会踩说明符解析）。
- **`StartLimitIntervalSec=600` / `StartLimitBurst=30`**：就绪门已覆盖主要竞态；这一档是**安全网**。
  首版曾用 `120`/`5`，而实测 GraphDB 就绪要 ~100 s 以上 ⇒ **窗口在就绪前就耗尽**，
  unit 被置 `failed` 并**停止重试**（这正是 §7 第一轮演练的失败形态）。
- **`:8304` 的 `M5_CASE_ID=A-01`**：该服务是**判决模式**、用例是**进程级信道**
  （`start_cc_live_facts.sh` docstring：CLI `--case` 优先、其次 env `M5_CASE_ID`；
  **切例必须 stop 后重启**）。unit 里显式钉住，避免"重启后悄悄换了用例"。

---

## 5. 回滚

```bash
systemctl disable --now cc-tools-kg cc-tools-onto cc-tools-kgt cc-live-facts
rm -f /etc/systemd/system/cc-tools-{kg,onto,kgt,live-facts}.service
systemctl daemon-reload
# 之后回到手工方式：
#   cd /opt/cc-tools-kg && bash start_cc_tools.sh
#   cd /opt/cc-tools-onto && bash start_cc_tools_onto.sh
#   cd /opt/cc-tools-kg/kgt/scheme1-kg/service && CC_KGT_DIR=/opt/cc-tools-kg/kgt bash start_cc_tools_kgt.sh
#   cd /opt/cc-live-facts && M5_CASE_ID=A-01 bash shared/collect/start_cc_live_facts.sh
```

---

## 6. 诚实边界

1. **已做真机重启演练（两轮，2026-10-08）**，见 §7。第一轮**抓到并修掉了一个启动竞态**；
   第二轮**证明修复有效**。★ 但仍然要记住：**冷启动验证（stop→start）测不出启动竞态** ——
   第一轮就是唯一能暴露它的手段。
2. **`Type=forking` 的固有边界**：systemd 靠 `PIDFile` 跟踪进程。若进程死亡且 `Restart=on-failure`
   拉起成功，pidfile 会被脚本重写 ⇒ 正常；但若脚本在"端口被占"路径上退出，unit 会置 `failed`
   并留旧 pidfile，此时 `systemctl restart` 前需先处理占用者（见 §4 的 `StartLimit` 说明）。
3. **`/opt/cc-tools-kg/cc_tools_kgt.pid` 是历史残留**（pid 588261 已死，来自漏 `CC_KGT_DIR` 的那次启动）。
   §3 的验证用的是 `kgt/` 下**正确**的 pidfile；那个残留文件**无害但会误导**，可手工删。
4. 本目录只解决"**重启后服务会自己起来**"；它**不**解决「模型网关 `Connection error`」
   （LiteLLM → Model Group=`deepseek_v4`，`Available Model Group Fallbacks=None`）。
   **2026-10-08 实测频率（小样本，如实登记）**：
   - 近 12 h 的 Dify 日志里该错误共 **4 次，且全部落在同一个小时**（出现时 = 当时正在发生的上游故障，
     不是稳定背景率）；同窗口模型调用 200 的有 **77** 次；
   - 故障窗口内的样本：6 App × 1 call = **5 PASS / 1 FAIL**；随后 3 App × 1 call = **1 PASS / 2 FAIL**
     （含此前 PASS 的 `cc-onto-agent`）⇒ 窗口内约 2/3 的 App 调用会挂；
   - **每次失败时工具都已调用成功**（载体审计链 +2 / +7）⇒ **载体无辜，是模型侧**。
   - 失败原文：`litellm.InternalServerError: ... OpenAIException - Connection error.. Received
     Model Group=deepseek_v4 / Available Model Group Fallbacks=None`。
   处置（用户裁定）：**先不动**，按 transient 处理 —— 无答案按 `abstain/no_answer` 计入，
   不污染主判据分母（R-54 的 `derive_abstain` 已实现该归类）。
   ★ 若该故障转为持续，应优先考虑给该模型配 **fallback**（`Fallbacks=None` 意味着单点即全线失败）。
   ★ 另注：所用提供方是 `langgenius/deepseek/deepseek`（官方 DeepSeek 插件），
   其上游 base_url 本轮**未取到**（Dify Console API 的 provider 路径与该三段式 id 不匹配）⇒ 未继续深挖。

---

## 7. 真机重启演练（2026-10-08，两轮；这是本文件最重要的证据）

> ★ 元教训：**「冷启动验证」（停掉服务 → `systemctl start`）测不出"重启时的启动竞态"** ——
> 因为那时后端（GraphDB/Neo4j）早已就绪。第一轮演练才把它暴露出来。

### 7.1 第一轮：暴露竞态（旧 unit）

- 重启前基线：17 个容器全部 `restart=always`/`unless-stopped`；4 个 unit `enabled+active`
  （PID 1116205 / 1116250 / 1116317 / 1116370）；4 端口 `health=200`。
- `systemctl reboot`；主机 **~40 s** 后 SSH 恢复（新 boot `16:07:54`）。
- **结果**：
  - ✅ 17/17 容器自己回来；
  - ✅ **4 个服务确实由 systemd 自动拉起**（新 PID 3696 / 3703 / 3706，全是开机后起的）；
  - ❌ **`cc-tools-onto` = `failed`**，`:8302 health=000`。

失败原文（`journalctl -u cc-tools-onto`）：

```
build_service → service.resolve_snapshot() → self.backend.graphs()
  → query('SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }')
GraphError: 查询超过 30.0 秒未返回，已中止。
systemd: cc-tools-onto.service: Scheduled restart job, restart counter is at 5.
systemd: Start request repeated too quickly.  → failed
```

**机制**：`:8302` 在**启动时**就要查 GraphDB 解析快照；GraphDB 是 JVM，重启后**>120 s** 才答得出
该查询，而服务把"查询超时"当**致命启动错误** ⇒ 退出 1 ⇒ systemd 在 `StartLimitBurst=5/120s`
窗口内重试 5 次全败 ⇒ **放弃**（`failed`，且此后**不会**自动再试）。

**为什么这比"服务没自启"更隐蔽**：unit 是 enabled 的、容器也回来了，只有 `:8302` 静默死掉 ——
症状仍会是「某个工具的服务状态异常」，和 §1 的事故长得一模一样。

### 7.2 修复

1. 四个 unit 各加 **`ExecStartPre` 后端就绪门**（`cc-tools-kg`/`cc-tools-kgt` 等 Neo4j `:7688`；
   `cc-tools-onto` 轮询 GraphDB `:7200/rest/repositories`），最长 300 s；
2. `StartLimitIntervalSec` `120` → **600**、`StartLimitBurst` `5` → **30**（安全网）；
3. `RestartSec` `10` → **15**、`TimeoutStartSec` `90` → **360**（覆盖就绪门耗时）。

### 7.3 第二轮：证明修复有效（同条件、新 unit）

| 项 | 第一轮（旧） | **第二轮（新）** |
|---|---|---|
| 新 boot | 16:07:54 | **16:20:01** |
| 容器 | 17/17 | **17/17**（`gdo-graphdb`/`neo4j-gaussdb` running） |
| `cc-tools-onto` | **`failed`** | **`activating` ~90 s → `active`** |
| **`NRestarts`（四个）** | onto = 5（耗尽） | **全 0** ← 靠**就绪门等待**成功，不是重试碰运气 |
| 四端口 health | `:8302` = **000** | **全部 200** |
| `:8303` 审计路径 | 声明路径 ✓ | 声明路径 ✓ |
| 失败 unit | 1 个 | **0 个** |

时间线：`16:20:01` boot → `16:20:38~42` 另三个起来（~40 s）→ **`16:22:19` onto 起来（~138 s）**，
多出的 ~100 s **正是就绪门在等 GraphDB**。

### 7.4 演练的运维含义

- **重建这类"启动即依赖后端"的服务时，unit 必须带就绪门**，否则重启后的表现是"某个臂静默死掉"。
- 演练中断面：约 **40–50 s** 内主机恢复 SSH、容器 ~15 s 起来、Dify `api` 健康状态随后转 healthy；
  **`online` 档 CI 作业若在此时跑会得到 BLOCKED** —— 这再次说明"online 档不该阻塞合入"。

## 8. 旧载体机上 v3 两服务（`:8187` 采集层 / `:8188` 本体）的自启（★ 2026-10-09）

### 8.1 为什么加这两个（与 §1 同类问题）

用户要求把 `192.0.2.10:8188` 的**本体服务**起起来。它**依赖 `:8187` 采集层**
（该服务 `/health` 自报"取数服务 = http://192.0.2.10:8187"，见 `/opt/upstream-agent/restart_onto_service.sh` 的 dry-run 输出）。
两个服务在 旧载体机 上都**已安装但未运行**、且**没有任何开机自启**（`/etc/systemd/system/` 里与本项目无关的
`gdo*` 单元当时不存在 ⇒ 重启机器后必掉）。⇒ 按 §1 的同一处置方式补齐 unit。

### 8.2 这两个服务是什么（别与 `cc-tools-*` 混淆）

| 单元 | 提供者 | 端口 | 与既有单元的区别 |
|---|---|---|---|
| `gdo-collect-v3.service` | **本仓采集层**（`dist/collect-layer/`，`service/app_collect:app`；装在旧载体机的 `upstream-ops-agent` 工作区里） | **8187** | 与 `cc-live-facts`（:8304，本项目的**中性取数**边车）**不是**同一个东西 |
| `gdo-onto-v3.service` | **旧载体机上较早部署的** v3 本体服务（`ontology/v3/service/onto_service.py`，不在本仓代码内） | **8188** | 与 `cc-tools-onto`（:8302，本项目的**只读知识**服务）**不是**同一个东西；两者用的 GraphDB repo 也不同（`:8188` 用 `gdo`，本项目用 `cc-ops`） |

### 8.3 安装（与 §2 同一套流程，逐字可复现）

两个 unit 的正文见本目录同名文件；落盘后：

```
sudo install -m 644 shared/deploy/systemd/gdo-collect-v3.service /etc/systemd/system/
sudo install -m 644 shared/deploy/systemd/gdo-onto-v3.service    /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gdo-collect-v3.service
sudo systemctl enable --now gdo-onto-v3.service
```

★ **仓库副本 = 已部署文件（逐字节）**：本目录两个 `.service` 是**从 `/etc/systemd/system/` 直接取回**的
内容，登记 sha256（2026-10-09）：
`gdo-collect-v3.service = 520bde62806eedf1ca41dc564fa470a5bf1af184a7fe23007724e3813f99c9c5`、
`gdo-onto-v3.service = ecb55d6d6fb08ccc0deb8c974c1e22512f098a7de1f30b69962fff6431caf29e`。

### 8.4 设计要点（为什么这么写）

* **`Type=simple`**：这两个服务都是**前台**进程（`uvicorn` / `onto_service.py`），不像 `cc-tools-*`
  走自带 PID 文件的 `start_*.sh`，所以不需要 `Type=forking` + `PIDFile=`。
* **就绪门（`ExecStartPre`）**：`:8188` 有两个门 —— 先等 **GraphDB `:7200`**（`curl /rest/repositories` 成功，
  §7 那轮演练的教训：GraphDB 冷启可超 120 s），再等**采集层 `:8187`** TCP 可连（它还依赖后者取数）。
  `:8187` 不设后端门：它的后端是 GaussDB/SSH，起不来时它自己**降级但不崩**（`/health` 仍 200）。
* **启动顺序**：`:8188` 的 unit 写 `After=network-online.target gdo-collect-v3.service`；两者都
  `WantedBy=multi-user.target`（由 target 同时拉起，`After` 保证先后）。
* **重启上限放宽**（`StartLimitIntervalSec=600` / `StartLimitBurst=30`）：同 §4 与 §7 的教训，
  避免后端慢启时被 systemd 判成"失败过多"而放弃。

### 8.5 验证（2026-10-09 实测，非推理）

| 检查 | 读数 |
|---|---|
| `systemd-analyze verify`（两个单元） | **无告警** |
| `is-enabled` | 双双 `enabled`（`multi-user.target.wants` 符号链接已建 ⇒ 开机自启） |
| **开机路径模拟**（全停 → 按 target 顺序拉起） | 两端口 **6 s 内就绪**；`:8187` `/health` **200**（`collect_source: live`）、`:8188` `/health` **200**（`graphdb_ok: true`，`repo=gdo`，`triples=46589`） |
| `systemctl restart gdo-onto-v3` | 回到 `active`、新 `MainPID`、`/health` **200**（证明单元可重复拉起、就绪门有效） |
| 既有服务未受影响 | `cc-tools-kg` / `cc-tools-onto` / `cc-tools-kgt` / `cc-tools-kgt-dyn` / `cc-live-facts` 全部仍 `active` |
| 端口总览 | `8187 8188 8301 8302 8303 8304 8305` 全部在听（7 个单元 enabled+active） |

★ **功能面**：`:8188` 暴露 22 个端点（`/onto/diagnose`、`/onto/collect`、`/onto/sparql`、`/onto/trace`、
`/onto/symptoms`、`/onto/baseline`、`/onto/correlate`、`/onto/runbook` …）；只读校验
`GET /onto/consistency` ⇒ **200** `{"consistent": true, "owl_nothing_instances": 0, "unsatisfiable_classes": 0}`。
★ **它自带 baseline 调度器**：`/health` 的 `baseline = {"running": true, "attempts": 5, "ok": 4, "failed": 0, "last_ms": 11565}`
⇒ 会**周期性调用 `:8187` 采集层**（该层有防循环护栏，只记"最新一个 key"，交替调用方互相重置，不会误拦）。

### 8.6 诚实边界

* **本轮只做了"开机路径模拟"（全停→按顺序拉起）与 restart 测试，没有做真机 reboot 演练** ——
  §7 的两轮 reboot 是 `cc-tools-*` 的；`gdo-*` 若要同等强度证据，需另做一轮 reboot（重启机器）。
* `:8187`/`:8188` 的**后端**（GaussDB、GraphDB、k8s、应用实例）不在本套 unit 的管辖内：
  本次实测中 `container`/`app` 两层因上游数据源不可达而 **0 事实**（见 `docs/02` §14），
  这与 unit 无关，`/health` 也不会因此报错。
* `:8187` 采集层已并入本仓（即 `dist/collect-layer/`，源出同一作者的 `upstream-ops-agent` 工作区）；`:8188` 本体服务属旧载体机上较早的部署（`<workspace>\upstream-ops-agent` / 旧载体机 的 `/opt/upstream-agent`）；
  本目录只固化"怎么在 旧载体机 上把它们自启起来"，不改变它们的代码。