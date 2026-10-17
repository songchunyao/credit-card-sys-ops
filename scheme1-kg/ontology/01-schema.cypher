// ============================================================================
// scheme1-kg/ontology/01-schema.cypher
// 方案1（KG-Q / KG-R / KG-T / KG-T-free 共用同一张图）· LPG Schema DDL
// ----------------------------------------------------------------------------
// 规格权威（只读）：
//   docs/03-方案1设计（知识图谱+LLM）.md  §2.1/§2.2/§2.3/§2.4
//   docs/00-总体设计与裁决记录.md        裁决 13（CC_ 命名空间前缀、cc_uq_* 与既有 gdo_uq_* 区分）
//   shared/mapping/type-map.yaml         namespaces.kg（label_prefix / constraint_prefix / 属性风格）
//   shared/mapping/rel-map.yaml          namespaces / relations[].kg_reltype
//   shared/model/topology.yaml           14 类型（13 具体 + 1 抽象）的 key_attrs
//
// 命名纪律（§2.3）：cc_uq_* 只用于 CREATE CONSTRAINT（唯一约束）；
//                   cc_idx_* 只用于 CREATE INDEX（普通索引）。二者不得混用。
//
// ★ 与投影器 P1 的关系（M1.5 校准，docs/03 §2.3 注）：
//   P1（scheme1-kg/project/p1_neo4j.py）装载时按映射表另建 13 条 `cc_uq_cc_<标签小写>`
//   约束，键为映射表 namespaces.kg.instance_id_property = `ccfId`（投影级实例 id）。
//   本文件建的是**业务键**约束（topology.yaml 的 key_attrs），两者键不同、可并存，
//   约束名不重叠（真机实测：既有 cc_uq_cc_* 13 条；本文件新增 20 条 cc_uq_*）。
//
// 幂等（P8）：全部 IF NOT EXISTS / MERGE，重复执行不报错、不改既有数据。
// ============================================================================


// ────────────────────────────────────────────────────────────────────────────
// 1. 唯一约束 · 业务拓扑（12 个**具体业务**类型各一条 + 派生注释型 Evidence 1 条 = 13 条；
//    键取 topology.yaml#key_attrs）
//    ★ 类型口径：topology.yaml 共 14 类 = 12 具体业务（CreditCardApp / AppInstance / AppEndpoint /
//      JdbcDataSource / Container / Host / GaussDBCluster / GaussDBInstance / DatabaseSessionPool /
//      Session / Query / Table）+ 1 抽象 ConnectionPool（abstract: true，无实例 ⇒ **不建约束**）
//      + 1 派生注释型 Evidence（annotation: true）。
//      `FaultMode` **不是** topology 类型，而是故障域标签（见第 2 节）。
// ────────────────────────────────────────────────────────────────────────────

// ---- L1 应用层 ----
CREATE CONSTRAINT cc_uq_app_code     IF NOT EXISTS FOR (n:CC_CreditCardApp)       REQUIRE n.appCode    IS UNIQUE;
CREATE CONSTRAINT cc_uq_appinst_id   IF NOT EXISTS FOR (n:CC_AppInstance)         REQUIRE n.instanceId IS UNIQUE;
// ★ AppEndpoint 的 key_attrs = [path, protocol]（复合键），topology.yaml 无 endpointId 属性；
//   docs/03 §2.1 手写清单写作 `n.endpointId`，与契约冲突，此处以契约（映射表/ topology.yaml）为准。
//   ★ 复合键写法：真机为 Neo4j 5.26.30 **Community**，实测 `IS NODE KEY` 直接报
//     "Node Key constraint requires Neo4j Enterprise Edition"（见报告 3.2 错误原文），
//     而复合 `IS UNIQUE` 可建（同一事务内实测 OK）⇒ 用复合唯一约束，存在性由 P1 的
//     cc_uq_cc_appendpoint(ccfId) 与投影器必写 key 属性共同保证（见 GAP-DOC-05）。
CREATE CONSTRAINT cc_uq_endpoint_key IF NOT EXISTS FOR (n:CC_AppEndpoint)         REQUIRE (n.path, n.protocol) IS UNIQUE;
CREATE CONSTRAINT cc_uq_ds_code      IF NOT EXISTS FOR (n:CC_JdbcDataSource)      REQUIRE n.dsCode     IS UNIQUE;

// ---- L2 承载层 ----
CREATE CONSTRAINT cc_uq_ctr_id       IF NOT EXISTS FOR (n:CC_Container)           REQUIRE n.containerId IS UNIQUE;
CREATE CONSTRAINT cc_uq_host_name    IF NOT EXISTS FOR (n:CC_Host)                REQUIRE n.hostname   IS UNIQUE;

// ---- L3 数据层 ----
CREATE CONSTRAINT cc_uq_cluster_name IF NOT EXISTS FOR (n:CC_GaussDBCluster)      REQUIRE n.clusterName IS UNIQUE;
CREATE CONSTRAINT cc_uq_dbinst_id    IF NOT EXISTS FOR (n:CC_GaussDBInstance)     REQUIRE n.instanceId IS UNIQUE;
CREATE CONSTRAINT cc_uq_pool_id      IF NOT EXISTS FOR (n:CC_DatabaseSessionPool) REQUIRE n.poolId    IS UNIQUE;
CREATE CONSTRAINT cc_uq_session_id   IF NOT EXISTS FOR (n:CC_Session)             REQUIRE n.sessionId  IS UNIQUE;
CREATE CONSTRAINT cc_uq_query_id     IF NOT EXISTS FOR (n:CC_Query)               REQUIRE n.queryId    IS UNIQUE;
// ★ Table 的 key_attrs = [schemaName, tableName]（复合键）⇒ 复合唯一约束（同 GAP-DOC-05：Community 无 NODE KEY）
CREATE CONSTRAINT cc_uq_table_key    IF NOT EXISTS FOR (n:CC_Table)               REQUIRE (n.schemaName, n.tableName) IS UNIQUE;

// ---- 派生注释型（annotation: true，layer 属性**不得**写）----
CREATE CONSTRAINT cc_uq_evid_id      IF NOT EXISTS FOR (n:CC_Evidence)            REQUIRE n.recordId   IS UNIQUE;


// ────────────────────────────────────────────────────────────────────────────
// 2. 唯一约束 · 故障模型侧（docs/03 §2.2 逐字）
//    ★ 这 7 个标签**不在** topology.yaml 的 14 个中性类型里 ⇒ 不进映射表，
//      由方案1 自己的 Cypher 原生建（CC_ 前缀由裁决 13 统一规定）。
//      例外：CC_Evidence 在映射表内（由投影器 P1 建），其约束在此一并声明。
// ────────────────────────────────────────────────────────────────────────────
CREATE CONSTRAINT cc_uq_fm_code      IF NOT EXISTS FOR (n:CC_FaultMode) REQUIRE n.fmCode      IS UNIQUE;
CREATE CONSTRAINT cc_uq_metric_code  IF NOT EXISTS FOR (n:CC_Metric)    REQUIRE n.metricCode  IS UNIQUE;
CREATE CONSTRAINT cc_uq_thr_code     IF NOT EXISTS FOR (n:CC_Threshold) REQUIRE n.thrCode     IS UNIQUE;
CREATE CONSTRAINT cc_uq_rule_code    IF NOT EXISTS FOR (n:CC_Rule)      REQUIRE n.ruleCode    IS UNIQUE;
CREATE CONSTRAINT cc_uq_finding_code IF NOT EXISTS FOR (n:CC_Finding)   REQUIRE n.findingCode IS UNIQUE;
CREATE CONSTRAINT cc_uq_action_code  IF NOT EXISTS FOR (n:CC_Action)    REQUIRE n.actionCode  IS UNIQUE;
CREATE CONSTRAINT cc_uq_meta_key     IF NOT EXISTS FOR (n:CC_Meta)      REQUIRE n.key         IS UNIQUE;


// ────────────────────────────────────────────────────────────────────────────
// 3. 索引（docs/03 §2.3，6 条，全部 cc_idx_*）
//    ★ P7：不建 from_fm_id / to_fm_id 索引 —— 这两个属性不存在。
// ────────────────────────────────────────────────────────────────────────────
CREATE INDEX cc_idx_fm_layer_idx      IF NOT EXISTS FOR (n:CC_FaultMode)     ON (n.fmLayer, n.fmCode);
CREATE INDEX cc_idx_fm_severity_idx   IF NOT EXISTS FOR (n:CC_FaultMode)     ON (n.fmSeverity);
CREATE INDEX cc_idx_metric_obj_idx    IF NOT EXISTS FOR (n:CC_Metric)        ON (n.metricObject, n.metricLayer);
CREATE INDEX cc_idx_query_elapsed_idx IF NOT EXISTS FOR (n:CC_Query)         ON (n.meanExecMs);
CREATE INDEX cc_idx_session_state_idx IF NOT EXISTS FOR (n:CC_Session)       ON (n.state, n.waitEvent);
// ★ 关系索引建在 `r.contractId`（= causal-edges.yaml 的 `id`）上。**契约里没有 probability 属性**
//   ⇒ 原 `cc_idx_causes_prob_idx ON (r.probability)` 是死索引（前导属性在图与契约中都不存在），
//   按 captain 裁定改名为本行；真机上残留的旧索引由 `scheme1-kg/tools/load_tbox.py mode=cleanup`
//   显式 `DROP INDEX`（见文件尾 GAP-DOC-06）。
CREATE INDEX cc_idx_causes_edge_id_idx IF NOT EXISTS FOR ()-[r:CC_CAUSES]->() ON (r.contractId);


// ────────────────────────────────────────────────────────────────────────────
// 4. 幂等元数据单例（docs/03 §2.4，MERGE 而非 CREATE）
//    ★ snapshotId 是硬要求（equivalence.py 与评测台靠它证明"两侧消费同一份 CCF"）。
//    ★★ M2.12 语义变更 —— **TBox 只写占位、从此不覆盖**：
//      · 本块整段收进 `ON CREATE SET`：节点已存在时**一个属性都不改**（连 updatedAt
//        也不动），因此第 2 次 APPLY 的变化为 0 项（幂等，口径强于旧版的无条件 SET）。
//      · `snapshotId` 首建时写的是**不可能通过快照格式校验**的占位串
//        `UNSET-TBOX-LOAD`。理由：TBox 是**静态 schema**，它无从知道当前 ABox 是哪一份
//        快照；此前写死的 M1.6 冻结值会在每次重装 TBox 时把 ABox 投影器写入的**真实**
//        快照 id 覆盖掉（真机实测缺陷：`:CC_Meta.snapshotId`=ccf-…eb634e 而全图 ABox
//        258 节点 / 225 关系的 ccfSnapshotId=ccf-…b920b5 ⇒ 图级元数据说谎，越过 docs/05
//        §5.1「同一 snapshotId」与硬约束 C1/C5）。
//      · **图级快照 id 的权威 = ABox 投影器** `scheme1-kg/project/p1_neo4j.py`（docs/03
//        §2.4）：每次 ABox 装载都 MERGE 同一单例并 `SET snapshotId = <CCF.snapshotId>`、
//        刷新 `updatedAt`；对 CCF 出现多个 / 缺失 snapshotId **fail-closed**（非零退出，
//        绝不部分装载）。因此"缺 snapshotId 的图不得进评测"由 K2 门禁兜住，而不是靠本文件
//        猜一个看似合法的陈旧值（占位串不是任何一份快照的 id，消费方只能走 fail-closed）。
//      · 判别门禁 = `shared/verify/graph_meta_gate.py`（G19）：占位串一律 ERROR；
//        `:CC_Meta.snapshotId` 必须 == 所有 `CC_` 前缀 ABox 节点 ∪ 关系上
//        `ccfSnapshotId` 取值集合的**唯一元素**。
//    ★ 契约缺口：docs/03 §2.4 把本节点放在 §2（Schema DDL）内，故写在 01；
//      这是**唯一**一处把实例级字面量写进 schema 文件的地方（见文件尾 GAP-DOC-02）；
//      M2.12 起该字面量已是**非法占位串**（不是快照 id），不再伪装成合法快照。
// ────────────────────────────────────────────────────────────────────────────
MERGE (m:CC_Meta {key: 'graph'})
  ON CREATE SET m.createdAt     = datetime(),
                m.updatedAt     = datetime(),
                m.schemaVersion = 'kg-1.0.0',
                m.contractRef   = 'shared/model/*.yaml',
                m.ccfVersion    = '1.0.1',
                m.snapshotId    = 'UNSET-TBOX-LOAD';


// ────────────────────────────────────────────────────────────────────────────
// ★ 缺口台账（GAP-DOC）—— 校验器 shared/verify/kg_tbox_check.py 逐条机读核对
//   GAP-DOC-01 endpointId：docs/03 §2.1 写 `cc_uq_endpoint_id ... REQUIRE n.endpointId`，
//               但 topology.yaml / type-map.yaml 的 AppEndpoint **没有** endpointId 属性
//               （key_attrs = [path, protocol]）⇒ 本文件按契约建**复合唯一约束**，
//               约束名改 cc_uq_endpoint_key。（★ 已关闭 2026-10-06：docs/03 §2.1 已由 captain 改写为复合 (path, protocol)。）
//   GAP-DOC-05 复合键的约束类型（★ 已澄清 2026-10-06：`IS NODE KEY` 在 docs/ 全库零命中，文档通篇写 `IS UNIQUE`，故非文档缺陷）；
//               真机 Neo4j 5.26.30 Community 实测**不支持 NODE KEY**（Enterprise 专属，报错原文见报告）；
//               改用复合 `IS UNIQUE`（同事务实测可建）。语义差异 = 存在性不由约束强制，
//               由 P1 的 cc_uq_cc_*(ccfId) 唯一约束 + 投影器必写 key 属性兜底。**环境限制，非文档缺陷。**
//   GAP-DOC-02 §2.4 要求 snapshotId 落库（★ 已关闭 2026-10-06 by M2.12）：**权威刷新方 = ABox 投影器**
//               （`scheme1-kg/project/p1_neo4j.py`：每次 ABox 装载 MERGE 同一单例并 `SET snapshotId = <CCF.snapshotId>`
//               + 刷新 updatedAt；CCF 出现多个/缺失 snapshotId 时 fail-closed 非零退出）；本文件（TBox）只**首建**写
//               非法占位串 `UNSET-TBOX-LOAD` 且此后不覆盖。旧写法把 M1.6 冻结值写死 + 无条件 SET，
//               导致每次重装 TBox 都把 ABox 的真实快照 id 覆盖成陈旧值（真机实测：`:CC_Meta` 说 ccf-…eb634e
//               而全图 ABox 说 ccf-…b920b5）。判别门禁 = `shared/verify/graph_meta_gate.py`（G19）。
//   GAP-DOC-03 约束条数口径（★ 已对齐 2026-10-06）：docs/03 §2.1 原只列 12 条业务约束（缺 M1.5c 的 Evidence），
//               本文件按"13 个具体类型各一条 + §2.2 公布的 8 条"：13 + 8 - 1（Evidence 两侧重复）
//               = 20 条。docs/03 §2.1/§2.2 已由 captain 改写为同一分布（§2.1 13 条含 Evidence、§2.2 7 条含 Meta）。
//   GAP-DOC-04 §2.3 索引属性名（★ 已关闭 2026-10-06）：docs/03 原手写 `ON (n.objectType, n.layer)`，但同节 P5 落地又规定
//               「fault 相关属性统一用 fm*/metric* 前缀，**不用**裸 layer」；而 CC_Metric 不在映射表内
//               （§2.3 注 §2「故障模型侧标签不进映射表」）⇒ 两处自相冲突。本文件按 P5 落地规则建
//               `ON (n.metricObject, n.metricLayer)`（与 03-fault-modes.cypher 的 CC_Metric 节点属性一致）。docs/03 §2.3 已由 captain 同步改写；
//   GAP-DOC-06 关系索引前导属性（契约优先口径，captain 裁定）：docs/03 §2.3 原写
//               `cc_idx_causes_prob_idx ON (r.probability)`，但唯一权威 `shared/model/causal-edges.yaml`
//               每条边只有 id/from/relation/to/chain/evidence/provenance（+condition），**没有**
//               probability/weight/delaySec/learnCount/chainPosition ⇒ 原索引前导属性永不被写入，
//               是死索引。本文件改为 `cc_idx_causes_edge_id_idx ON (r.contractId)`（= 契约 `id`）。
//               ★ 已关闭 2026-10-06：docs/03 §2.3 已由 captain 改写为 cc_idx_causes_edge_id_idx ON (r.contractId)。
// ============================================================================
