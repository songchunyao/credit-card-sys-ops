// ============================================================================
// Q01 · 全栈拓扑（业务 → 承载 → 数据）        scheme1-kg/queries/q01-topology.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q01 —— 从一个业务应用出发，一次取回 App / Runtime / Data 三层承载链。
// 参数 ：$appCode — 业务应用码（CC_CreditCardApp.appCode，来自 type-map key_attrs）
// 返回 ：每行一条「应用→实例→容器→宿主机」+「应用→实例→数据源→集群→实例→连接池→会话→查询→表」
//        的链路坐标；未命中的层为 null（**不吞行**，保证 answer_shape 可逐行比对）。
// 只读 ：纯 MATCH/OPTIONAL MATCH/RETURN；无写子句、无过程调用。
// 注意 ：层级不写死（`ccfLayer` 由投影器按映射表写），本查询只回读，便于跨方案 answer_shape 比对。
// ============================================================================
MATCH (app:CC_CreditCardApp {appCode: $appCode})
OPTIONAL MATCH (app)-[:CC_HAS_APP_INSTANCE]->(inst:CC_AppInstance)
OPTIONAL MATCH (inst)-[:CC_RUNS_IN]->(ctr:CC_Container)
OPTIONAL MATCH (ctr)-[:CC_DEPLOYED_ON]->(host:CC_Host)
OPTIONAL MATCH (inst)-[:CC_USES_DATASOURCE]->(ds:CC_JdbcDataSource)
OPTIONAL MATCH (ds)-[:CC_CONNECTS_TO]->(cl:CC_GaussDBCluster)
OPTIONAL MATCH (cl)-[:CC_HAS_DB_INSTANCE]->(dbi:CC_GaussDBInstance)
OPTIONAL MATCH (dbi)-[:CC_HAS_SESSION_POOL]->(pool:CC_DatabaseSessionPool)
OPTIONAL MATCH (pool)-[:CC_HAS_SESSION]->(sess:CC_Session)
OPTIONAL MATCH (sess)-[:CC_RUNS_QUERY]->(qry:CC_Query)
OPTIONAL MATCH (qry)-[:CC_ACCESSES]->(tbl:CC_Table)
RETURN app.ccfId      AS appId,
       app.ccfLayer   AS appLayer,
       inst.ccfId     AS instanceId,
       inst.ccfLayer  AS instanceLayer,
       ctr.ccfId      AS containerId,
       ctr.ccfLayer   AS containerLayer,
       host.ccfId     AS hostId,
       ds.ccfId       AS datasourceId,
       cl.ccfId       AS clusterId,
       dbi.ccfId      AS dbInstanceId,
       pool.ccfId     AS poolId,
       sess.ccfId     AS sessionId,
       qry.ccfId      AS queryId,
       tbl.ccfId      AS tableId,
       tbl.ccfLayer   AS tableLayer
ORDER BY instanceId, containerId, sessionId, queryId, tableId;
