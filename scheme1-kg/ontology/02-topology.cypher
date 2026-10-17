// ============================================================================
// scheme1-kg/ontology/02-topology.cypher
// 方案1 · 业务拓扑层：**类型/关系模板与枚举**（TBox 骨架，不含任何实例数据）
// ----------------------------------------------------------------------------
// 规格权威（只读）：
//   shared/model/topology.yaml    14 类型（12 具体业务 + 1 抽象 ConnectionPool + 1 派生注释型 Evidence）
//                                 / 15 关系 / 4 逆关系（key_attrs 权威）
//   shared/mapping/type-map.yaml  namespaces.kg（label_prefix=CC_、property_style=lowerCamel、
//                                 instance_id_property=ccfId、neutral_type_property=ccfType、
//                                 layer_property=ccfLayer、snapshot_property=ccfSnapshotId）
//   shared/mapping/rel-map.yaml   relations[].kg_reltype（15 条）/ inverse_relations（4 条，asserted:false）
//   docs/03-方案1设计（知识图谱+LLM）.md §3（★ 拓扑来源铁律）
//
// ★★ 拓扑来源铁律（docs/03 §3 原文要点）：
//   本层的所有拓扑事实**只能**来自采集层 `POST /tools/gather` 出参的 `topology{}` + `ip_index{}`
//   （service/collect_api.py 的 `_real_topology()`），**绝不允许**由 LLM 推断、**绝不允许**手工编写。
//   理由：①两侧拓扑若由 LLM 各自"推断"，差异来自模型而非载体，主判据失效；②主备角色直接决定
//   `FM-DB-INSTANCE-DOWN` 判定，猜错等于 oracle 污染；③CCF 的 objects/relations 必须与两侧载体重放
//   出的图逐条对应（equivalence.py 的校验基础）。
//   ⇒ 因此**本文件只写模板（用 `$rows` 参数占位），一个实例值都不写**；实例由图投影器 P1 写入。
//
// ★ 落地口径（与投影器 P1 对齐，逐字可核）：
//   · P1 的节点写入 = `UNWIND $rows AS r MERGE (n:<Label> {ccfId: r.ccfId}) SET n += r`
//     （scheme1-kg/project/p1_neo4j.py:207-208）；本文件的 §D 逐条模板与之一致。
//   · P1 的边写入 = `MATCH (a:L1 {ccfId: r.from}), (b:L2 {ccfId: r.to})
//                    MERGE (a)-[e:CC_X]->(b) SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap`
//     （scheme1-kg/project/p1_neo4j.py:217-221）；本文件 §E 逐条模板与之一致。
//   · 层（App/Runtime/Data）**不靠继承靠注解**（docs/00 §5.3）：作为属性 `ccfLayer` 写在节点上
//     （属性名取自映射表 namespaces.kg.layer_property，**不**在本文件硬编码属性名，统一走 `SET n += r`）。
//   · ★ docs/03 §3 示例里的 `a.layer = 'App'` 与映射表的 `ccfLayer` 冲突 ⇒ 以映射表为准（见文件尾 GAP-DOC）。
//
// 计数（topology.yaml，M1.5c 校正后）：type_count=14 / relation_count=15 / cross_layer_edge_count=4 /
//   max_hop_depth=10；derived 关系 1 条（HAS_EVIDENCE，asserted_in_ccf: false）。
// ============================================================================


// ────────────────────────────────────────────────────────────────────────────
// §A 层枚举（topology.yaml#meta.layer_enum，逐字）
//   App     应用层   ：CreditCardApp / AppInstance / AppEndpoint / JdbcDataSource
//   Runtime 承载层   ：Container / Host
//   Data    数据层   ：GaussDBCluster / GaussDBInstance / ConnectionPool(抽象) /
//                      DatabaseSessionPool / Session / Query / Table
//   （注解型 Evidence 的 layer = null，不属任何业务层 ⇒ 图内**不写** layer 属性）
// ────────────────────────────────────────────────────────────────────────────


// ────────────────────────────────────────────────────────────────────────────
// §B 类型目录（14 条，逐字来自 topology.yaml；`abstract`/`parent`/`disjoint_with` 为契约字段）
//   1  CreditCardApp        应用      key_attrs=[appCode]
//   2  AppInstance          应用      key_attrs=[instanceId]
//   3  AppEndpoint          应用      key_attrs=[path, protocol]        ← 复合键
//   4  JdbcDataSource       应用      key_attrs=[dsCode]   parent=ConnectionPool  disjoint_with=[DatabaseSessionPool]
//   5  Container            承载      key_attrs=[containerId]
//   6  Host                 承载      key_attrs=[hostname]
//   7  GaussDBCluster       数据      key_attrs=[clusterName]
//   8  GaussDBInstance      数据      key_attrs=[instanceId]
//   9  ConnectionPool       数据      abstract: true（无实例 ⇒ §D 无模板、01-schema 无约束）
//  10  DatabaseSessionPool  数据      key_attrs=[poolId]  parent=ConnectionPool  disjoint_with=[JdbcDataSource]
//  11  Session              数据      key_attrs=[sessionId]
//  12  Query                数据      key_attrs=[queryId]
//  13  Table                数据      key_attrs=[schemaName, tableName] ← 复合键
//  14  Evidence             注解型（annotation: true，layer=null）  key_attrs=[recordId]
//      由投影器从 observations 派生（docs/03 §12.7），本文件只给骨架，不派生。
//
//   ★ 其余属性（cardinality / cross_layer / inverse / attrs 明细）**不在本文件重复声明** ——
//     唯一真源是 topology.yaml + 映射表，重复声明会造成两处漂移（P1 单一真源）。
// ────────────────────────────────────────────────────────────────────────────


// ────────────────────────────────────────────────────────────────────────────
// §C 关系目录（15 条 asserted，逐字来自 topology.yaml；`逆关系 4 条` asserted:false 不建边）
//   HAS_APP_INSTANCE    CreditCardApp   → AppInstance        1:N   cross_layer=false
//   EXPOSES             CreditCardApp   → AppEndpoint        1:N   cross_layer=false
//   RUNS_IN             AppInstance     → Container          N:1   cross_layer=true   inverse=RUNS
//   DEPLOYED_ON         Container       → Host               N:1   cross_layer=false  inverse=HOSTS_CONTAINER
//   USES_DATASOURCE     AppInstance     → JdbcDataSource     N:1   cross_layer=false
//   CONNECTS_TO         JdbcDataSource  → GaussDBCluster     N:1   cross_layer=true   inverse=ACCEPTS_CONNECTION_FROM
//   RESOLVES_TO         JdbcDataSource  → GaussDBInstance    N:1   cross_layer=true
//   HAS_DB_INSTANCE     GaussDBCluster  → GaussDBInstance    1:N   cross_layer=false
//   HOSTS               Host            → GaussDBInstance    1:N   cross_layer=true
//   HAS_SESSION_POOL    GaussDBInstance → DatabaseSessionPool 1:1  cross_layer=false
//   HAS_SESSION         DatabaseSessionPool → Session        1:N   cross_layer=false
//   RUNS_QUERY          Session         → Query              1:N   cross_layer=false
//   ACCESSES            Query           → Table              N:M   cross_layer=false
//   REPLICATES_TO       GaussDBInstance → GaussDBInstance    1:1   cross_layer=false
//   HAS_EVIDENCE        "*"（任意业务对象）→ Evidence        1:N   派生（asserted_in_ccf: false, derived_from: observations）
//
//   ★ 4 条逆关系（INSTANCE_OF / RUNS / HOSTS_CONTAINER / ACCEPTS_CONNECTION_FROM，asserted: false）：
//     两侧**均不显式建边**，由正向关系的反向遍历得到 ⇒ 本文件不写它们的模板。
// ────────────────────────────────────────────────────────────────────────────


// ────────────────────────────────────────────────────────────────────────────
// §D 节点模板（13 条 = 13 个具体类型；抽象 ConnectionPool 无实例故无模板）
//   用法：参数 `$rows` = 投影器/采集层产出的行集合，每行已按映射表把属性名改成 KG 名。
//   本文件**不含任何实例值**：模板里的 `r.*` 全部来自 $rows。
// ────────────────────────────────────────────────────────────────────────────
UNWIND $rows AS r MERGE (n:CC_CreditCardApp {ccfId: r.ccfId})       SET n += r;
UNWIND $rows AS r MERGE (n:CC_AppInstance {ccfId: r.ccfId})         SET n += r;
UNWIND $rows AS r MERGE (n:CC_AppEndpoint {ccfId: r.ccfId})         SET n += r;
UNWIND $rows AS r MERGE (n:CC_JdbcDataSource {ccfId: r.ccfId})      SET n += r;
UNWIND $rows AS r MERGE (n:CC_Container {ccfId: r.ccfId})           SET n += r;
UNWIND $rows AS r MERGE (n:CC_Host {ccfId: r.ccfId})                SET n += r;
UNWIND $rows AS r MERGE (n:CC_GaussDBCluster {ccfId: r.ccfId})      SET n += r;
UNWIND $rows AS r MERGE (n:CC_GaussDBInstance {ccfId: r.ccfId})     SET n += r;
UNWIND $rows AS r MERGE (n:CC_DatabaseSessionPool {ccfId: r.ccfId}) SET n += r;
UNWIND $rows AS r MERGE (n:CC_Session {ccfId: r.ccfId})             SET n += r;
UNWIND $rows AS r MERGE (n:CC_Query {ccfId: r.ccfId})               SET n += r;
UNWIND $rows AS r MERGE (n:CC_Table {ccfId: r.ccfId})               SET n += r;
UNWIND $rows AS r MERGE (n:CC_Evidence {ccfId: r.ccfId})            SET n += r;


// ────────────────────────────────────────────────────────────────────────────
// §E 关系模板（15 条）
// ────────────────────────────────────────────────────────────────────────────
UNWIND $rows AS r MATCH (a:CC_CreditCardApp {ccfId: r.from}), (b:CC_AppInstance {ccfId: r.to})         MERGE (a)-[e:CC_HAS_APP_INSTANCE]->(b)  SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_CreditCardApp {ccfId: r.from}), (b:CC_AppEndpoint {ccfId: r.to})         MERGE (a)-[e:CC_EXPOSES]->(b)           SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_AppInstance {ccfId: r.from}),   (b:CC_Container {ccfId: r.to})           MERGE (a)-[e:CC_RUNS_IN]->(b)           SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_Container {ccfId: r.from}),     (b:CC_Host {ccfId: r.to})                MERGE (a)-[e:CC_DEPLOYED_ON]->(b)       SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_AppInstance {ccfId: r.from}),   (b:CC_JdbcDataSource {ccfId: r.to})      MERGE (a)-[e:CC_USES_DATASOURCE]->(b)   SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_JdbcDataSource {ccfId: r.from}),(b:CC_GaussDBCluster {ccfId: r.to})      MERGE (a)-[e:CC_CONNECTS_TO]->(b)       SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_JdbcDataSource {ccfId: r.from}),(b:CC_GaussDBInstance {ccfId: r.to})     MERGE (a)-[e:CC_RESOLVES_TO]->(b)       SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_GaussDBCluster {ccfId: r.from}),(b:CC_GaussDBInstance {ccfId: r.to})     MERGE (a)-[e:CC_HAS_DB_INSTANCE]->(b)   SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_Host {ccfId: r.from}),          (b:CC_GaussDBInstance {ccfId: r.to})     MERGE (a)-[e:CC_HOSTS]->(b)             SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_GaussDBInstance {ccfId: r.from}),(b:CC_DatabaseSessionPool {ccfId: r.to}) MERGE (a)-[e:CC_HAS_SESSION_POOL]->(b)  SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_DatabaseSessionPool {ccfId: r.from}), (b:CC_Session {ccfId: r.to})       MERGE (a)-[e:CC_HAS_SESSION]->(b)       SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_Session {ccfId: r.from}),       (b:CC_Query {ccfId: r.to})               MERGE (a)-[e:CC_RUNS_QUERY]->(b)        SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_Query {ccfId: r.from}),         (b:CC_Table {ccfId: r.to})               MERGE (a)-[e:CC_ACCESSES]->(b)          SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
UNWIND $rows AS r MATCH (a:CC_GaussDBInstance {ccfId: r.from}),(b:CC_GaussDBInstance {ccfId: r.to})    MERGE (a)-[e:CC_REPLICATES_TO]->(b)     SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;
// HAS_EVIDENCE：from = "*"（任意业务对象）⇒ 无固定源标签，按 ccfId 全局定位（ccfId 由 01-schema 的唯一约束索引）
UNWIND $rows AS r MATCH (a {ccfId: r.from}), (b:CC_Evidence {ccfId: r.to})                             MERGE (a)-[e:CC_HAS_EVIDENCE]->(b)      SET e.ccfRelId = r.rid, e.ccfSnapshotId = r.snap;


// ────────────────────────────────────────────────────────────────────────────
// ★ 缺口台账（GAP-DOC）
//   GAP-DOC-01 layer 属性名：docs/03 §3 示例写 `SET a.layer='App'`；映射表 namespaces.kg 定
//               `layer_property: ccfLayer` ⇒ 图内属性名是 `ccfLayer`。本文件**不硬编码**任一属性名
//               （统一 `SET n += r`），实际属性名由共享投影器按映射表落。**未就地改契约/文档。**
//   GAP-DOC-02 §D/§E 模板的 `$rows` 字段名（from/to/rid/snap）沿用 P1 既有用法；契约未规定参数名，
//               本文件与 P1 对齐以免新增第二套约定。
// ============================================================================
