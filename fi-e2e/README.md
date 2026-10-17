# fi-e2e · 端到端故障注入「故障—根因匹配性」测试

> 被测环境：`192.0.2.11`（cardsim 1.5.11 + GaussDB + 双载体 RCA 栈）
> 注入台：`http://192.0.2.11:8090`（cardsim `monitor.py`）
> 契约：`shared/model/fault-modes.yaml` v1.0.3（23 detect 模式 + 1 exclusion 叶 = 24 码）
> 本目录：`fault-map.yaml`（真值表）· `fi.py`（测试器）· `out/`（逐用例证据）

---

## 1. 先回答：什么叫「故障与根因匹配」

一次匹配性用例 = **注入一个已知故障 → 让被测 RCA 系统回答 → 比对它答的根因是不是我注入的那个**。

难点全在「比对」这一步。有四个坑，踩任何一个都会得到**看似合理但不可归因**的结论：

| # | 坑 | 后果 | 本测试的处置 |
|---|---|---|---|
| K1 | 把「判据指标采不到」当成「系统没判出来」 | 把**观测能力缺口**记成**算法失败**，结论方向完全错 | 契约层三态判定：缺 delta / 缺阈值档 / 缺指标 ⇒ `UNJUDGEABLE`，**永不当 miss**（`fault-modes.yaml:36-39`） |
| K2 | 不设阴性对照 | 健康态本就成立的结论（如 `FM-DB-UNUSED-INDEX` 实测 14 张表）被当成本次故障的信号 | **差分判定**：先跑 `baseline` 取背景结论集 `chronic`，再算 `incremental = detect(故障) − chronic` |
| K3 | 故障没真生效就开始判 | 注入无效 ⇒ 空结果 ⇒ 假阴性 | 每例先 `clear`、确认活动故障为空、起流量、注入后静置、再冻结；无效则判 `BLOCKED`（**没测到 ≠ 测到是坏的**） |
| K4 | 只测「有没有答对」，不测「答的是什么」 | 答错一个**别的根因**（false claim）与「什么都没答」被混为一谈 | 逐载体记录 `top1 / expect_hit_k / false_claim / parity` 四个量 |

---

## 2. 四层判据（分开测，禁止混算）

```
注入故障 T（38 类之一）
   │
   ├─ L0 注入有效性   : A 侧确认 T 已注入且物理生效         → 否则 BLOCKED
   │
   ├─ L1 判据层(契约) : 用 fault-modes.yaml 的 detect 规则在冻结 CCF 上机判
   │                    载体无关；三态 TRUE/FALSE/UNK；算 chronic 差分
   │
   ├─ L2 根因层(载体) : 把 L1 检出的症状码喂给 :8301(KG) 与 :8302(ONTO) 的 /cause
   │                    取候选根因排序 → top1 / hit@k / false_claim
   │
   └─ L3 载体一致性   : KG 与 ONTO 的 top1 是否相同（对照实验的核心量）
```

**为什么必须分层**：若合成一个「准确率」，一次失败就无法归因——是故障没生效？是探针没采到？是判据写错？还是载体答错了？分层后每种失败都有自己的判词。

判词词表：

| 判词 | 含义 |
|---|---|
| `HIT` | 期望根因码**就在候选里**（含 top1） |
| `HIT_PARTIAL` | 判据层检出期望码，但 /cause 没把它排进候选 |
| `HIT_SYMPTOM_ONLY` | 只命中症状级码（合法，但**不是**根因命中） |
| `NOT_EXPRESSIBLE` | 期望码被判据层检出，但**不在 `/cause` 词表内** ⇒ 根因接口结构性表达不了（见 §8.9） |
| `MISS` | 可判、且故障确已生效，但给出的根因不对 |
| `NO_EFFECT` | 签名指标一个都没动 ⇒ **故障没产生**，不记 MISS |
| `UNJUDGEABLE` | 期望码的判据指标本次快照无观测 ⇒ 按契约不可判 |
| `BLOCKED` | 没测到（注入失效/冻结失败/投影失败/服务不可达） |
| `NOT_APPLICABLE` | 该注入在本环境是 no-op |
| `BASELINE_CLEAN` / `BASELINE_SIGNAL` | 阴性对照：健康态有无增量信号 |

`MISS` / `NO_EFFECT` / `UNJUDGEABLE` / `BLOCKED` 四者**必须分开**：它们分别指向
「算法答错」「注入没生效」「观测能力不足」「测试自身没跑通」——混成一个「未命中」会让报告失去全部信息量。

---

## 3. 真值表：38 类注入 → 24 个契约码

`fault-map.yaml` 是本测试的**唯一真值来源**。此前仓库里**不存在**任何机读映射（只有 `fault-modes.yaml:266-267` 一条自由文本注释，提到 4 组对应）。

`match_kind` 词表（设计事实，人裁定）：

| match_kind | 类数 | 含义 |
|---|---|---|
| `exact` | 23 | 契约中存在与该注入机制一一对应的根因码 |
| `symptom_only` | 4 | 契约无对应根因码，只能命中症状级码 |
| `no_counterpart` | 10 | 契约词表里没有任何对应码（= **契约覆盖缺口**，本身是结论） |
| `not_applicable` | 1 | `db_down` 在 GaussDB 模式下是 no-op（`monitor.py:1191-1194`） |

另有 3 对 `ambiguous_with`（判据同源、机判不可区分）：
`conn_kill↔net_partition`、`sql_full_scan↔sql_fuzzy_like`、`biz_hot_row↔biz_lock_range`。
它们的「分不开」不是被测系统的缺陷，是**契约判据粒度**的事实，报告必须如实写。

---

## 4. 闭环（每用例一次完整跑）

```
 0  POST /api/fault {action:clear}            清基线（★ 重复注入同类会 400，必须先清）
 1  POST /api/sim   {action:start,duration}   起流量（无流量则多数 app/db 类故障不产生）
 2  POST /api/fault {action:inject,type:T}    注入，记录墙钟 UTC 时刻
 3  sleep(settle≈45s)                         等生效（JVM 2s 轮询 + tick%N 节流，最长需 50 tick）
 4  POST /tools/gather ×1 → sleep(gap≈12s)    建立 delta 窗口（increased/decreased 判据的前提）
 5  shared/collect/freeze.py                  冻结 CCF（4 层，--expected-profile l1-l3）
 6  p1_neo4j.py --wipe / p2_graphdb.py --wipe 双载体投影
 7  fi.py 判据层：24 个模式逐个三态机判 + chronic 差分
 8  POST :8301/cause 、 :8302/cause            症状码 → 候选根因排序（两侧各一次）
 9  评分 → 落盘 → clear + sim stop             ★ finally 保证，成败都清
```

**为什么第 4 步不能省**：`increased`/`decreased` 类判据（`FM-DB-DEADLOCK` / `FM-DB-ROLLBACK-STORM` / `FM-DB-SEQ-SCAN` / `FM-DB-IDLE-IN-TX` / `FM-CTR-OOM-KILLED` 等）全部依赖观测的 `delta` 字段。没有两次采样就没有 delta ⇒ 这些模式恒 UNK。契约对 delta 缺失的规定是**不可判**，不是 false。

---

## 5. 闭卷原则（不可违反）

cardsim 的 `docs/SYSTEM-SUMMARY.md:14-26,§8` 明确：**被测侧（B 端）禁止读 monitor 的 `/api/*`**，因为 `snapshot.faults` 与 `faultDefs` 会直接泄露答案。

本测试严格遵守：

* **A 侧（注入/评测）**：可以用 `:8090` 的 `/api/fault`、`/api/snapshot` —— 它本来就该知道真值。
* **被测侧（RCA）**：唯一取数口是采集层 `POST :8187/tools/gather` → CCF → 图/本体。链路里没有任何一处读 `:8090`。

`fi.py` 的注入客户端与被测链路在代码上也是分开的：`Console` 类只被编排层用，判据层只吃 CCF。

---

## 6. 用法

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:CC_NEO4J_PASSWORD = "<neo4j 口令>"

# 只读状态
python fi-e2e/fi.py status

# 离线：判据可判性 + 每类注入的可测性（不需要起故障）
python fi-e2e/fi.py probe --ccf tmp/_e2e_ccf.json --out fi-e2e/out/probe.json

# 离线：在任意 CCF 上跑 24 个模式的契约判据
python fi-e2e/fi.py detect --ccf <ccf.json> --json fi-e2e/out/detect.json

# 阴性对照（不注入）：取健康态背景结论集
python fi-e2e/fi.py case --type baseline --duration 180 --settle 45 --runid r01

# 单用例全闭环
python fi-e2e/fi.py case --type sql_deadlock --duration 180 --settle 45 --gap 12 --runid r01

# 批次（先自动跑基线做差分）
python fi-e2e/fi.py suite --with-baseline --types sql_deadlock,sql_idle_in_tx,... `
       --duration 180 --settle 45 --runid r01
```

关键开关：`--no-project`（只测判据层）、`--no-sim`（不起流量）、`--keep-sim`（保留负载）、
`--chronic-from <case.json>`（复用已有基线）、`--max-hops`、`--carriers KG,ONTO`。

---

## 7. 产物（全部可复算）

```
fi-e2e/out/<runid>/
├── baseline/ 或 <type>/
│   ├── ccf.json            冻结的 CCF（同源判据；含 snapshotId 与 observations.delta）
│   ├── detect.json         24 个模式的逐子句判定轨迹（值、阈值、对象属性、三态）
│   ├── detect_summary.json 判据可判性（每个模式缺哪个指标）
│   ├── p1_report.json / p2_report.json   两侧投影报告
│   ├── cause_KG.json / cause_ONTO.json   两侧 /cause 原始响应（未加工）
│   └── case.json           该用例完整记录：注入时刻、判词、评分、载体对比
├── results.jsonl           逐用例记录
└── summary.json            批次汇总
```

---

## 8. 使用前必读的实测约束

1. **必须按 38 类设计**，不要按「26 类」——`README.md`/`docs/FAULTS.md` 的类数说明已陈旧（实为 infra 10 + app 28）。
2. **`db_down` 是 no-op**：返回 200 但什么都不做。必须标 `not_applicable` 并断言「无观测变化」，否则会把空结果误当检测失败。
3. **必须先 `clear` 再 `inject`**：注入同类型重复会返回 400。
4. **★ 容器有「待命」生命周期**：一轮仿真跑完后容器不再待命，`POST /api/sim {action:start}` 会返回
   400「没有待命容器」。必须**先 `POST /api/scale {delta:1}` 建容器 → `POST /api/spawn {container,threads}` 拉起 JVM
   → 再 `sim start`**。不做这一步，绝大多数 app/db 类故障**根本不会产生**（= 假阴性）。
   实测：`scale` 后 6 s 容器起，`spawn` 后 8 s JVM 起，共约 20 s。
5. **★ 运行中的轮次不在 `results[]` 里**：`snapshot.results` 只含已结束的 `results/simulation-*.json`；
   正在跑的轮次只出现在 `signals` 的『应用层 / 最新 run #N = RUNNING』。判断「有没有流量」必须读 signals。
6. **生效有延迟**：JVM 2s 轮询 + `tick%N` 节流（`biz_lock_range` 需 10 tick、`java_thread_pile` 需 50 tick）⇒ `--settle` 给足；`--settle 60` 是保守值。
7. **`injectedAt` 不可作时间锚**：它只有 `HH:MM:SS` 无日期，且自愈路径会伪造。本测试一律记**自己的墙钟 UTC 时刻**。
8. **可判性是运行时的**：app 侧遥测（`app.endpoint.*` / `app.datasource.acquire_wait_ms`）只在有进行中的仿真轮次时才有值；空闲期是 `notCollected(telemetry_stale)`。所以 `probe` 的静态结论只是指示，**真正的可判性以每用例实测 CCF 为准**。
9. **★ `/cause` 词表 ≠ 契约词表**（实测，两侧一致）：
   * 服务**实际接受 14 个码**：`FM-APP-DS-CONNECT-FAIL / FM-APP-ENDPOINT-SLOW / FM-APP-HEAP-PRESSURE / FM-APP-INSTANCE-RESTART / FM-APP-POOL-EXHAUSTED / FM-CTR-CPU-THROTTLED / FM-CTR-NET-UNREACHABLE / FM-CTR-OOM-KILLED / FM-DB-INSTANCE-DOWN / FM-DB-LOCK-WAIT / FM-DB-SESSION-POOL-FULL / FM-DB-SLOW-QUERY / FM-HOST-CPU-CONTENTION / FM-HOST-MEM-PRESSURE`
   * 而 `causal-edges.yaml` 的因果图节点是 **23 个**。差集 9 个（`FM-DB-DEADLOCK` / `FM-DB-IDLE-IN-TX` / `FM-DB-REPLICATION-LAG` / `FM-DB-ROLLBACK-STORM` / `FM-DB-SEQ-SCAN` / `FM-DB-UNUSED-INDEX` / `FM-HOST-DISK-FULL` / `FM-HOST-DISK-IO-ERROR` / `FM-HOST-NET-IFACE-DOWN`）**能判据层检出、却无法喂进 `/cause`**（喂了直接 400）。
   * ⇒ 测试器**不猜词表**：`cause_vocabulary()` 用哨兵码打一次 `/cause`，从 400 报文里读服务实际接受的码集并缓存。
   * ⇒ 落到这 9 个码上的用例判 `NOT_EXPRESSIBLE`，**不记为 MISS**——这是根因**接口**的表达能力问题，不是判据或算法问题。
   * ★ 边界：本判词**只针对确定性 `cc_cause` 工具路径**。LLM Agent 自己写 Cypher/SPARQL 读 Finding 的路径是另一个平面，本测试器尚未覆盖（见报告「未覆盖」）。
10. **不要照抄 monitor 的批量 SQL**：`monitor.py:365-367` 的第一条是 `UPDATE simulation_run SET status='ABORTED'`——搬进 harness 会误改数据。
11. **`increased`/`decreased` 类判据不是故障特异性信号**：运行中的系统上任何计数器都恒有增量 ⇒ 绝对判定必然为真。必须用阴性对照算增量（`--with-baseline` 是**默认行为**）。
12. **共享环境纪律**：每例结束 `finally` 清故障；批次串行，绝不并发注入（cardsim 的设计目标是「同一时刻最多一个注入故障」）。仿真时长由 `--duration` 封顶，不会无人值守地长跑。

---

## 9. 本批能测什么（结论摘要在报告里）

`probe` 的结论分三类：

* **TESTABLE**：期望根因码的全部判据指标齐备 ⇒ 能真正测「故障↔根因匹配」。
* **UNJUDGEABLE**：期望码的判据指标在实机**无采集通道**（探针不伪造 0）⇒ **结构性地测不了**，这是**观测能力缺口**，不是算法问题。
* **SYMPTOM_ONLY / no_counterpart**：契约词表里没有对应根因码 ⇒ 只能测「症状是否被看见」。

`fault-map.yaml` 的 `match_kind` 与 `probe` 的 `testable` 是**两个不同的轴**，报告不得混写：
前者是「契约该不该有这个码」，后者是「这个码在当前采集能力下能不能判」。

---

## 10. 试点批次结果（2026-10-13，n=6）

完整报告 [out/pilot01/REPORT.md](out/pilot01/REPORT.md)　缺陷清单 [FINDINGS-pilot01.md](FINDINGS-pilot01.md)

| 用例 | 期望根因 | 判词 | 关键实测 |
|---|---|---|---|
| `java_pool_starve` | `FM-APP-POOL-EXHAUSTED` | `HIT`※ | `acquire_wait_ms` 593→**4990** |
| `cpu_throttle` | `FM-CTR-CPU-THROTTLED` | `HIT_PARTIAL` | `cpu_throttled_periods` 0→**759**；判据层正确检出 |
| `biz_slow_response` | 症状 `FM-APP-ENDPOINT-SLOW` | `MISS` | `tps` 553→**122**，但 `p99` 仅 33.7→35.2（< SLO 40.4） |
| `sql_long_tx` | `FM-DB-LOCK-WAIT` | `MISS` | `xact_age_s` 恒负、`wait_event` 恒 null ⇒ **不可判** |
| `sql_deadlock` | `FM-DB-DEADLOCK` | `MISS` | `deadlocks` 窗口内 Δ≡0 |
| `net_partition` | `FM-CTR-NET-UNREACHABLE` | `UNJUDGEABLE` | `connect_fail_total` 无通道 |

※ 该 `HIT` 已被报告 §3a 证明是 **ONTO 恒定 top1 的巧合**，**有效根因匹配数 = 0**。

**三条最重要的结论**：
1. **闭环是通的**：注入 → 事实变化 → 契约判据命中（`cpu_throttle` 是干净的正例），链路可信。
2. **根因层不具选择性**：`/cause` 返回 12–13 个候选（词表仅 14），top1 的命中条款恒为 0，
   且 KG 恒答 `FM-HOST-MEM-PRESSURE`、ONTO 恒答 `FM-APP-POOL-EXHAUSTED` ⇒ 载体一致性 **0/3**。
3. **多处指标本身是坏的**：`xact_age_s` 恒负、`wait_event`/`status`/`state` 恒 null、
   `lock_waits`≡0、`deadlocks` Δ≡0；且 3 个模式在健康态恒为真（`increased` 判据非特异）。

**口径声明**：n=6 **不足以**支撑任何准确率对比或显著性结论；本批的用途是**发现问题**与**验证方法**。

---

## 11. 注入系统自身的缺陷（必须先于 RCA 结论处理）

真值表新增 `injection_status` 轴（`ok` / `noop` / `broken`），与 `match_kind`（契约映射）**分开**：

| 注入 | injection_status | 事实 |
|---|---|---|
| `db_lock` | **broken** | `monitor.py:1198` 走 `docker exec cardsim-pg psql …`，而 **`cardsim-pg` 容器不存在**、`PG_DB="cardsim"` 也写错（真实库 `vapp`），且该分支**不检查 err** ⇒ 返回 `200 {"ok":true}` 但**什么都没做**。清除分支反而用可用的 `pg_query()` —— 两条路径不一致。**等价替代：`sql_idle_in_tx`**（实测有效） |
| `db_down` | `noop` | 设计如此（`monitor.py:1191-1194`），GaussDB 模式不支持 |

⇒ **不能只信 `200 {"ok":true}`**。这正是 L0 注入有效性层存在的理由：
若无 L0，`db_lock` 的「RCA 没诊断出来」会被误记为算法失败，实际是**故障从未发生**。

---

## 12. 免部署验证采集层修复（`validate_probe_fix.py`）

探针源码打进 `cc-ops/collect` 镜像，改一行就要重建镜像 + 重启共享服务。
`freeze.py --gather-json` 允许先在**真实 gather 回执**上重放「修复后的取值」，证明「修完能不能判」，再决定要不要部署。

```powershell
# 1) 在故障窗口抓一份真实 gather（fi.py 的 case 产物里就有 ccf.json，
#    但重放需要 gather 原始回执，见 out/fixcheck/gather_fault.json 的抓取方式）
# 2) 免部署重放验证
python fi-e2e/validate_probe_fix.py --dir fi-e2e/out/fixcheck
```

判据 fail-closed：对照组（原样）必须 `FM-DB-LOCK-WAIT = FALSE`，修复组（打三处补丁）必须 `= TRUE`。
**实测结论：PASS**（`FALSE → TRUE`），即三处修复可证让该模式（覆盖 5 类注入）由不可判变可判。

修复本身落在 `dist/collect-layer/service/probes/gaussdb_probe.py`（M1 事务年龄时区 / M2 锁等待口径 / M3 逐会话等待事件），
`collect-layer.manifest.yaml` 的指纹已重钉；**已于 2026-10-13 19:27 部署到被测主机并验证生效**
（容器内探针 sha256 一致；注入 `biz_hot_row` 后 `db.instance.lock_waits` 由恒 0 变 **7**、
`db.session.wait_event` 由恒 `lock_wait` 变 **`tuple`/`transactionid`** 且判据为 TRUE）。
回滚点：`cc-ops/collect:ci-arm64-v1-pre-m1m2m3`。全过程见
[FINDINGS-pilot01.md](FINDINGS-pilot01.md) §二与§三。

---

## 13. 两个必须分开报的读数：契约字面 AND vs 服务式条款计分

`fault-modes.yaml` 的 `detect_semantics` 声明的是**布尔代数**（`all_of` 组内全部成立、组间 AND），
但部署侧 `cc_tools.py:2196-2218` 的实现是**按对象逐个求值 → 数命中条款数 → 取最优对象**，**不做严格 AND**。

⇒ 同一份快照上两者可以不一致（实测 `FM-DB-LOCK-WAIT`：契约字面 **FALSE**，服务式 **1/2 且 determined**）。
`fi.py` 的 `detect.json` 现在**两个读数都落盘**：

* `state` —— 契约字面（严格 AND）
* `bestObject.satisfiedClauses / totalClauses / determined` —— 服务式条款计分

**报告与结论不得只引用其中一个。** 契约目前**没有规定跨对象的聚合规则**，这是一处契约真空（见 FINDINGS §三）。

---

## 14. 固定分母 50 条（线②）与载体差异登记表

### 14.1 用例清单：`suite-50.yaml`（可复算，不靠命令行长串）

```powershell
# 展开清单核对分母（不执行）
python -c "import importlib.util as u; s=u.spec_from_file_location('fi','fi-e2e/fi.py'); m=u.module_from_spec(s); s.loader.exec_module(m); t,d,_=m.expand_suite_list('fi-e2e/suite-50.yaml'); print(len(t), len(set(t)))"

# 正式跑（约 2 小时；每例 C1 闸门 + 判据层 + 两侧 /cause + 载体差异核对）
python fi-e2e/fi.py suite --list fi-e2e/suite-50.yaml --runid suite50 `
  --duration 400 --settle 45 --gap 10 --drain 20 --cooldown 10 `
  --chronic-from fi-e2e/out/pilot01/baseline/case.json
```

**分母口径与 M5 判决集一致**：50 条固定，**`UNJUDGEABLE` / `BLOCKED` / `NO_EFFECT` 都不缩分母**
（否则「少说话」会被奖励）。覆盖 **32/38** 个注入类型，分组：

| 组 | 含义 | 条数 |
|---|---|---|
| A | 判据齐备（可真正测「故障↔根因匹配」） | 27 |
| B | 部分可判（缺 1~2 个判据指标） | 4 |
| C | 结构性不可判（判据指标无采集通道；用于**证明观测缺口**） | 7 |
| D | 症状级 / 契约无对应根因码 | 8 |
| E | 本环境不适用（`db_down` no-op） | 1 |
| F | 阴性对照（不注入） | 3 |

### 14.2 载体差异登记表：`carrier-divergences.yaml` + 回归探测

`cc_cause` 候选在两侧载体上**必然**有原生差异（LPG 的边是一等资源、OWL 的关系是一等对象）。
若不登记，同一批差异会**每次运行都被重新发现**，读者也无法区分「已知载体差异」与「新缺陷」。

`fi.py` 逐码比对两侧的 4 个可机读量（`determined` / `totalClauses` /
`undeterminedClauses` / `relationPriority`）：命中登记表 ⇒ `declared`，否则 ⇒ **`undeclared`（fail-loud）**，
并在逐例日志与批次汇总的 `undeclaredCarrierDivergences` 里显式给出。

**★ 只对某个症状集成立**：实测同一对载体换一组症状码就会暴露新的 `(码,字段)` 分歧
（`FM-HOST-MEM-PRESSURE` 在 A 症状集下无分歧，在 `cpu_throttle` 自产的 6 个症状码下有 4 处分歧）
⇒ 登记条目**连同症状集一起记**，报告必须同时给出「本次症状集」与未声明数。

当前登记 **13 条**（CD-01…CD-13），`(code, field)` 唯一；`disposition` 三值：
`carrier_native`（按设计保留，不抹平）/ `to_investigate`（已登记待查，**不得当已知无害**）/
`harness_artifact`（由测试器口径造成，非载体属性）。

### 14.3 收集结果

```powershell
python fi-e2e/mkreport.py suite50          # 读 results.jsonl（批次结束才写）；未结束则回退扫目录
Get-Content fi-e2e/out/suite50/summary.json   # verdict 分布 / 命中率 / 载体一致率 / 未声明分歧数
```

**判读纪律**：n=50 才够做**配对检验**（同一用例两侧配对，McNemar + 配对 bootstrap）；
在此之前任何「KG 比 ONTO 准」的说法都不成立。且 `HIT` 必须连同
`bestObject.satisfiedClauses` 一起看 —— 实测存在「top1 恒定、命中条款 0」的假命中。

---

## 15. G-FI-1 · `cc_cause` 候选的**跨字段一致性门禁**

```powershell
python fi-e2e/cause_invariants_gate.py --runid suite50   # 离线复核批次产物
python fi-e2e/cause_invariants_gate.py --live            # 在线打两侧 :8301/:8302
```

退出码 `0=PASS / 1=FAIL / 2=BLOCKED`（缺输入绝不打印 PASS）。

**为什么加它**：定位 ONTO「条款数虚高」时，**定案证据来自响应内部自相矛盾** ——
`totalClauses=5` 却只有 2 个「判据个体」来源。而此前**所有门禁都不检查响应内部是否自洽**
（G24 比工具面、P3 比公理落位、纯度六面比载体词汇），于是一个让 ONTO 侧
**永远没有候选能命中条款**的 bug 长期无人发现。

| id | 不变量 | 抓什么 |
|---|---|---|
| INV-1 | 若 `evidenceSourceIds` 以「判据个体:」逐条款列举，其**去重数必须 = `totalClauses`** | 条款重复膨胀（SPARQL `OPTIONAL` 扇出被当成多条判据） |
| INV-2 | `satisfied + undetermined <= total` | 计数越界 |
| INV-3 | `determined == (undeterminedClauses == 0)` | `determined` **定义**写错 |
| INV-4 | 同一响应内 `rank` 唯一且连续 1..n | 排序不确定 |
| INV-5 | `satisfied <= total` 且 `undetermined <= total` | 单值越界 |
| INV-6 | `relationPriority ∈ 契约关系枚举`（发关系名而非数字） | 字段形态漂移（防回归） |

**INV-1 的作用域**：`evidenceSourceIds` 的语义**两侧不同**（KG 列阈值/证据来源，ONTO 列判据个体），
故 INV-1 只在该字段**确以「判据个体:」前缀逐条款列举时**才断言 —— 不假装一个不存在的跨载体约定。

**自我验证（实测）**：对**修复前**的 `suite50` 产物（100 份响应 / 248 个候选）报
**20 条违规**（`INV-1×10` + `INV-3×10`，正是 R1 修掉的两个 bug）；
对**修复后**的在线两侧报 **0 条 → PASS**。

**边界**：本门禁只证「响应内部自洽」，**不证**判据正确、也不证两侧一致 ——
后者分别由 `cause_invariants_gate` 之外的 `carrier-divergences.yaml` 登记表与 `stats50.py` 负责。
