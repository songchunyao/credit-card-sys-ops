// ============================================================================
// scheme1-kg/ontology/04-rules.cypher
// 方案1 · 规则库（`KG-R` 组）：7 条故障分析规则，对应 7 条故障链 F1–F7
// ----------------------------------------------------------------------------
// 规格权威（只读）：
//   docs/03-方案1设计（知识图谱+LLM）.md §6（规则库；含 R-F2 完整规则体 + 规则清单表）
//   docs/01-场景建模与范围定义.md §1.2（编码规范）§5.2/§5.3（14 FM / 18 边）§6（F1–F7 详细定义）
//   shared/model/fault-modes.yaml（阈值个体名 + 档位）  shared/model/causal-edges.yaml（边 id）
//
// ★ G3（阈值来源）本文件的实现口径：
//   · 真阈值**一律以引用形式**出现：`(th:CC_Threshold {thrCode:'<名称>'})` + `th.thrWarning|th.thrCritical`，
//     或实体自带属性（`ds.maxPoolSize` / `e.sloP99Ms` / `ds.connectionTimeoutMsMs`…）；
//     **不得**写 0.70 / 0.85 / 200 / 1000 之类字面量。
//   · 例外（契约明文豁免，fault-modes.yaml 头部写法约定 (b)）：符号/状态谓词不是阈值 ——
//     形如 `ds.pendingThreads > 0`（= `predicate: non_zero`）的 `0` 是符号判定，
//     不受 `threshold_policy.hardcode_forbidden` 约束。本文件只允许这一处例外，且校验器
//     会核对「该属性对应的指标在契约里的判据确实是 non_zero」。
//   · 变长路径 `-[:CC_CAUSES*1..n]->` 在本文件同样禁止（因果不声明传递性；限深 BFS 属 M2.9）。
//
// ★ 覆盖度现状（**诚实声明**，逐条见 GAP-DOC）：
//   docs/03 §6 只给出 **R-F2 一条**完整规则体；其余 6 条只以「规则清单表」一行给出
//   （规则名 + 链 + 输出 Finding + 关键否定性证据）。⇒ 本文件把 R-F2 写全，其余 6 条以
//   **不可执行的声明块**如实列出（ruleCode/chain/Finding/否定性证据逐字来自该表），
//   **没有自创规则体**。这直接决定 M2.3 的可执行覆盖度 = 1/7，须由裁决补齐（见 GAP-DOC-09）。
// ============================================================================


// ────────────────────────────────────────────────────────────────────────────
// R-F2 · 数据库慢查询跨层传导 → Finding `F-002`
//   形状逐句来自 docs/03 §6「规则示例：R-F2」（唯一有完整体的规则）
// ────────────────────────────────────────────────────────────────────────────
// ★ 可判性注记（M2.21 · 只加注记：不改规则体、不改任何语句、不改判据）—— 本文档首层的**正确读法**是
//   「**可执行覆盖 1/7，真机可判 0/7**」：GAP-DOC-09 记的 1/7 是**可执行**口径（本文件唯一有规则体的 R-F2），
//   0/7 是**真机可判**口径（下面 ②③）。两个数字并列，勿混为一谈。
//   ① 本规则体可执行：真机内核 Neo4j 5.26.30 上把本语句（MATCH … RETURN，去注释后**只有这一条**）
//      原样只读试跑，**无语法错误**、正常返回（rows=0，见 ③）。
//   ② 但它的输入在当前 CCF 中**从不断言**（零实例）：关系型 `CC_ACCESSES` / `CC_RUNS_QUERY` ——
//      KG 侧真机关系型清单（只读 `MATCH ()-[r]->() RETURN type(r)`，54 个关系型）**无**此二者，
//      既有 v2 数据里只有不带前缀的 `ACCESSES` 1 条 / `RUNS_SQL` 1 条；
//      ONTO 侧 live5 命名图 `ccf-20260928T155516Z-937b79`（2965 三元组，`?infer=false`）内
//      `cc:accesses` = 0、`cc:runsQuery` = 0 ⇒ **两侧一致地为零，不是投影分歧**。
//   ③ 唯一判据指标 `db.query.mean_exec_ms` 在当前快照**零观测**（真机 `CC_Query` 共 4 个、
//      `q.meanExecMs` 非空 0 个）⇒ 本规则在当前快照**恒 0 行**（只读试跑实测 rows = 0），
//      Finding `F-002` **恒不触发**。
//   ④ **不得**为凑覆盖而改判据（换阈值名 / 放宽比较 / 造边或造对象）：本注记与
//      `shared/model/zero-instance-ledger.yaml` 的「证据不足」登记**互相指引** —— 关系型条目
//      `RUNS_QUERY`、`ACCESSES` 与指标条目 `db.query.mean_exec_ms`（status 均 environment_limited、
//      planned_fix 均 none）；本注记不改这三条的 status / planned_fix / 解锁条件语义。
//   ⑤ 证据来源与采集时点：Neo4j 只读 `MATCH` / `SHOW`（192.0.2.10:7475，Kernel 5.26.30，只读不写）
//      + GraphDB `cc-ops` 仓 live5 命名图（2965 三元组，`?infer=false`）
//      + 本仓夹具 `tmp/ccf_live5.json`（按其 `relations[].type` 计数：`RUNS_QUERY` = 0 / `ACCESSES` = 0）；
//      采集时点 **2026-09-29 00:36–00:38（+08:00，本机时钟）**；本项目契约口径日期为 **2026-10-06**。

// ★ 语法适配（GAP-DOC-08）：文档示例把阈值子查询写成表达式
//   `q.meanExecMs > (MATCH (th:CC_Threshold {thrCode:'mean_exec_ms_light'}) RETURN th.thrCritical)`。
//   该写法是 Cypher 25 的子查询表达式；真机内核为 Neo4j **5.26.30**（community，见 docs/00 裁决 13）
//   ⇒ 不被支持。本文件改写为等价的「先 MATCH 阈值个体、再 WHERE 比较」，语义不变。
MATCH (q:CC_Query)-[:CC_ACCESSES]->(t:CC_Table)
MATCH (thSlow:CC_Threshold {thrCode: 'mean_exec_ms_light'})
// ★ 悬空引用已由契约补齐（GAP-DOC-07；契约 v1.0.2）：`dead_tuple_ratio_light` 现已是
//   shared/model/fault-modes.yaml#thresholds 的**第 12 个**阈值个体，但 `warning`/`critical`
//   **两档均为 null**（未标定）⇒ 该判据当前**不可机判**（由「悬空」改判为「未标定」）。
//   此处保留文档示例原样，未替换阈值名、未把 0.85 之类数字写进来（threshold_policy.hardcode_forbidden）。
MATCH (thDead:CC_Threshold {thrCode: 'dead_tuple_ratio_light'})
WHERE q.meanExecMs > thSlow.thrCritical
  AND t.deadTupleRatio > thDead.thrWarning

MATCH (db:CC_GaussDBInstance)-[:CC_HAS_SESSION_POOL]->(p:CC_DatabaseSessionPool)
      -[:CC_HAS_SESSION]->(s:CC_Session)-[:CC_RUNS_QUERY]->(q)
MATCH (ds:CC_JdbcDataSource)-[:CC_RESOLVES_TO]->(db)
MATCH (ds)-[:CC_CONNECTS_TO]->(:CC_GaussDBCluster)
WHERE ds.activeConnections >= ds.maxPoolSize
  AND ds.pendingThreads > 0            // 符号谓词 non_zero（契约豁免，非阈值）

MATCH (i:CC_AppInstance)-[:CC_USES_DATASOURCE]->(ds)
MATCH (i)-[:CC_RUNS_IN]->(:CC_Container)-[:CC_DEPLOYED_ON]->(:CC_Host)
MATCH (e:CC_AppEndpoint)<-[:CC_EXPOSES]-(:CC_CreditCardApp)<-[:CC_HAS_APP_INSTANCE]-(i)
WHERE e.p99Ms > e.sloP99Ms

// 证据路径（逆向：症状 → 根因），限深由固定跳数保证（Query→Session→Pool→db→ds→AppInstance）
MATCH path = (q)<-[:CC_RUNS_QUERY]-(:CC_Session)<-[:CC_HAS_SESSION]-(:CC_DatabaseSessionPool)
             <-[:CC_HAS_SESSION_POOL]-(db)<-[:CC_RESOLVES_TO]-(ds)<-[:CC_USES_DATASOURCE]-(i)
RETURN
  'F-002'                AS findingCode,
  'FM-DB-SLOW-QUERY'     AS rootCause,
  ds.dsCode              AS layerTarget,
  [n IN nodes(path) | coalesce(n.instanceId, n.dsCode, n.poolId, n.sessionId, n.queryId)] AS evidencePath,
  q.meanExecMs           AS triggerValue;


// ────────────────────────────────────────────────────────────────────────────
// ★ P0.9（2026-10-13）：以下 5 个块与 ONTO 侧规则分支对齐（non_zero / 阈值型）
//
//   ★★ 诚实登记：另 4 个模式（FM-DB-DEADLOCK / FM-DB-IDLE-IN-TX / FM-DB-SEQ-SCAN /
//      FM-DB-ROLLBACK-STORM，判据均为 `increased`）**本文件没有对应块**，原因是硬约束：
//        · C05 禁止规则体出现数字字面量 `0`，豁免仅覆盖 `var.<non_zero属性> > 0`；
//        · `increased` 在 ONTO 侧由 `ccm:tierReference "baseline:counter_non_zero"` 节点承载，
//          而 **KG 侧没有该节点** —— CC_Threshold 集合被 C12 锁死为
//          == fault-modes.yaml#thresholds（13 条），其中无符号基线这一档；
//        · ⇒ 在 KG 写 `e.delta > 0` 必然违反 C05；不写则无法表达该判据。
//      ⇒ 这 4 个模式**两侧不对称**（ONTO 能判、KG 不能），属**待裁决项**：
//        要么给 KG 增一个符号基线节点（并放宽 C12），要么在评测里显式扣除这 4 项。
//      ★ 未采取的做法：靠变量名/注释里塞 "non_zero" 触发 C05 的字符串豁免 —— 那是把闸的反面当正面用。
// ────────────────────────────────────────────────────────────────────────────

// FM-DB-UNUSED-INDEX ← tb:CC_Table
// ★ 2026-10-13 端到端修复：**先按属性筛对象，再按指标后缀收窄证据**。
//   原写法 `MATCH (tb)-[:CC_HAS_EVIDENCE]->(e)` 与 WHERE 同层 ⇒ 每个表与它**全部**
//   证据节点做笛卡尔积（实测 F-002 出 121 行，而表只有 14 张）。
//   ★ 后缀过滤串**不含点**（`'unused_index_count'`）——写成 `'db.table.unused_index_count'`
//     会被 C15 当作属性引用（`db` 在全文件作用域已绑定 GaussDBInstance）而误报。
MATCH (tb:CC_Table)
WHERE tb.unusedIndexCount > 0
MATCH (tb)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence)
WHERE e.metricCode ENDS WITH 'unused_index_count'
RETURN 'F-002' AS findingCode, 'FM-DB-UNUSED-INDEX' AS rootCause,
       e.recordId AS layerTarget,
       [e.recordId] AS evidencePath,
       tb.unusedIndexCount AS triggerValue;

// FM-HOST-DISK-FULL ← h:CC_Host
MATCH (h:CC_Host)
MATCH (thFs:CC_Threshold {thrCode: 'fs_usage_pct_light'})
WHERE h.fsMaxUsedPct > thFs.thrWarning
MATCH (h)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence)
WHERE e.metricCode ENDS WITH 'fs_max_used_pct'
RETURN 'F-004' AS findingCode, 'FM-HOST-DISK-FULL' AS rootCause,
       e.recordId AS layerTarget,
       [e.recordId] AS evidencePath,
       h.fsMaxUsedPct AS triggerValue;

// FM-HOST-DISK-IO-ERROR ← h:CC_Host
MATCH (h:CC_Host)
WHERE h.diskIoErrorsTotal > 0
MATCH (h)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence)
WHERE e.metricCode ENDS WITH 'disk_io_errors_total'
RETURN 'F-004' AS findingCode, 'FM-HOST-DISK-IO-ERROR' AS rootCause,
       e.recordId AS layerTarget,
       [e.recordId] AS evidencePath,
       h.diskIoErrorsTotal AS triggerValue;

// FM-HOST-NET-IFACE-DOWN ← h:CC_Host
MATCH (h:CC_Host)
WHERE h.netIfaceDownCount > 0
MATCH (h)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence)
WHERE e.metricCode ENDS WITH 'net_iface_down_count'
RETURN 'F-006' AS findingCode, 'FM-HOST-NET-IFACE-DOWN' AS rootCause,
       e.recordId AS layerTarget,
       [e.recordId] AS evidencePath,
       h.netIfaceDownCount AS triggerValue;

// FM-DB-REPLICATION-LAG ← cl:CC_GaussDBCluster
MATCH (cl:CC_GaussDBCluster)
WHERE cl.replicationNotStreamingCount > 0
MATCH (cl)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence)
WHERE e.metricCode ENDS WITH 'replication_not_streaming_count'
RETURN 'F-007' AS findingCode, 'FM-DB-REPLICATION-LAG' AS rootCause,
       e.recordId AS layerTarget,
       [e.recordId] AS evidencePath,
       cl.replicationNotStreamingCount AS triggerValue;

// ────────────────────────────────────────────────────────────────────────────
// 其余 6 条规则 —— ★ 文档未给规则体，故**只登记声明、不自创 Cypher**
//   每块的四个字段逐字来自 docs/03 §6「规则清单」表 + docs/01 §6 的链定义。
//   补齐方式（若裁决要求本阶段可执行）：按 R-F2 的形状，逐条把该链的判据（F1–F7 详述见
//   docs/01 §6）翻译为 MATCH/WHERE，并沿用同一套阈值引用与结论字段。
// ────────────────────────────────────────────────────────────────────────────

// RULE F1 · 应用侧连接池耗尽 → `F-001`
//   chain: F1 · 传播: FM-APP-POOL-EXHAUSTED -E5-> FM-APP-ENDPOINT-SLOW
//   关键否定性证据（必写）: 数据库侧 `session_used_ratio` 正常
//   支撑阈值引用: threshold:session_used_ratio_light#warning（fault-modes.yaml#L224-228）
//   判据来源: fault-modes.yaml#L36-49（pending_threads non_zero / active_connections >= maxPoolSize /
//             acquire_wait_ms > acquire_wait_ms_light#warning）
//   ★ 规则体：docs/03 未给 ⇒ 未自创。

// RULE F3 · 容器 CPU 限流 → `F-003`
//   chain: F3 · 传播: FM-HOST-CPU-CONTENTION -E7(条件)-> FM-CTR-CPU-THROTTLED -E8-> FM-APP-ENDPOINT-SLOW
//   关键否定性证据: 容器 `cpu_usage_ratio` **可能不高**（只看容器会误判为应用自身慢）
//   条件边引用: E7 condRef = cpu_usage_ratio_severe#critical（见 03-fault-modes.cypher）
//   判据来源: fault-modes.yaml#L139-149（host.load1 / host.cpu_usage_ratio）
//   ★ 规则体：docs/03 未给 ⇒ 未自创。

// RULE F4 · 容器 OOM（双路径）→ `F-004`
//   chain: F4 · 传播甲: FM-HOST-MEM-PRESSURE -E9(条件)-> FM-CTR-OOM-KILLED -E10-> FM-APP-INSTANCE-RESTART -E11-> FM-APP-ENDPOINT-SLOW
//   chain: F4 · 传播乙: FM-APP-HEAP-PRESSURE -E12(条件)-> FM-CTR-OOM-KILLED -E10-> ...
//   关键否定性证据: 宿主机 `mem_available_ratio` 正常 ⇒ 走 E12（容器 limit 小）路径
//   判据来源: fault-modes.yaml#L101-111 / #L88-98 / #L151-160
//   ★ 规则体：docs/03 未给 ⇒ 未自创。

// RULE F5 · JDBC 驱动/认证不兼容 → `F-005`
//   chain: F5 · 传播: FM-APP-DS-CONNECT-FAIL -E18-> FM-APP-ENDPOINT-SLOW（叶子根因，无出边）
//   关键否定性证据（**排除法**）: DB 在线 ∧ 网络可达 ∧ 立即失败（非超时）
//     —— 排除条件逐字来自 causal-edges.yaml#leaf_root_causes.exclusion_requirements（4 条）
//   实现形态（docs/03 §6 注）: Cypher 用 `NOT EXISTS { ... }`（对侧 SPARQL 用 `FILTER NOT EXISTS`）
//   判据来源: fault-modes.yaml#L51-61（connect_fail_total increased，叶子歧义点）
//   ★ 规则体：docs/03 未给 ⇒ 未自创。

// RULE F6 · 容器网络不可达 → `F-006`
//   chain: F6 · 传播: FM-CTR-NET-UNREACHABLE -E13-> FM-APP-DS-CONNECT-FAIL -E18-> FM-APP-ENDPOINT-SLOW
//   关键否定性证据: `connect_latency_ms` 达 `connectionTimeoutMs`（超时 ≠ 立即失败）
//   判据来源: fault-modes.yaml#L125-137（app.datasource.connect_latency_ms > entity_property:JdbcDataSource.connectionTimeoutMs）
//   ★ 规则体：docs/03 未给 ⇒ 未自创。

// RULE F7 · DB 实例故障/切换 → `F-007`
//   chain: F7 · 传播: FM-DB-INSTANCE-DOWN -E14-> FM-APP-DS-CONNECT-FAIL -E18-> FM-APP-ENDPOINT-SLOW（另有 E15）
//   关键否定性证据: `switchover_total` 有增量 ∧ `up = false`
//   时间对齐（docs/01 §6 F7 难点）: KG 侧只能靠属性 + WHERE 过滤时间窗
//   判据来源: fault-modes.yaml#L200-210
//   ★ 规则体：docs/03 未给 ⇒ 未自创。


// ────────────────────────────────────────────────────────────────────────────
// ★ 缺口台账（GAP-DOC）—— 校验器 shared/verify/kg_tbox_check.py 逐条机读核对
//   GAP-DOC-07 悬空阈值引用【已由契约补齐，改判为"未标定"】：R-F2 体引用 `dead_tuple_ratio_light`，
//              该个体在契约 v1.0.1 时**不存在**（原登记的缺口），契约 **v1.0.2** 已把它补为
//              `fault-modes.yaml#thresholds` 的**第 12 个**个体，但 `warning`/`critical` **两档均为 null**
//              ⇒ 该判据当前**不可机判**（口径与 docs/00 §12 的 WARN 类一致）。原样保留、**未替换、未编造数值**。
//   GAP-DOC-08 语法适配：docs/03 §6 的阈值子查询表达式写法需 Cypher 25；真机内核 5.26.30
//              ⇒ 改写为等价 MATCH+WHERE。若裁决要求逐字照抄，则该规则在真机上**无法装载**。
//   GAP-DOC-09 覆盖度：7 条规则中只有 1 条有文档规则体（R-F2）；其余 6 条仅有一行清单
//              ⇒ 本文件可执行覆盖 1/7。补齐需裁决（否则 M2 出口条件①的 T 系列门禁无法全过）。
//   GAP-DOC-10 规则编码规范冲突：docs/01 §1.2 规定规则编码为 `RULE-<两位序号>`（例 `RULE-07`），
//              而 docs/03 §6 的规则清单用 `R-F1`…`R-F7`。本文件两个标识**都保留**
//              （块头写 `R-Fn`，并注明它对应 `RULE-0n`），未擅自删掉任一命名。
//   GAP-DOC-11 阈值个体→节点的映射：本文件引用的 `thrCode` **由 03-fault-modes.cypher 的
//              `CC_Threshold` UNWIND 表创建**（M2.2 交付物；契约 v1.0.2 起 **12 个**个体，
//              真机实测已落 12 个节点，见 tmp/load_persist.log「CC_Threshold 12」）。
//              原担心的"无人创建 ⇒ 引用悬空"已消除。
//   GAP-DOC-12 规则是否要落成 `CC_Rule` 节点：docs/03 §2.2 为 `CC_Rule` 定义了 `ruleCode` 唯一约束，
//              但全文**没有**规定 `CC_Rule` 的属性集与写入语句；§6 只给规则体/清单 ⇒ 本文件
//              不自创节点属性，也不写 `MERGE (r:CC_Rule)`（真机上 `CC_Rule = 0`）。若裁决要求
//              规则元数据落库，需先由文档给出 `CC_Rule` 的属性 schema。
// ============================================================================
