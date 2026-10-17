// ============================================================================
// Q03 · 故障传播 → 下游受影响故障模式      scheme1-kg/queries/q03-downstream.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q03 —— 从给定故障模式出发，沿 CC_CAUSES 取**下游**传播链。
// 参数 ：$fmCode — 起点故障模式码；$maxDepth 概念上固定 4（§5.6 深度上限），本查询按 4 跳显式展开。
// 返回 ：每个可达下游 FM 一行，带 depth(1..4)、经边 contractId/chain/description/provenance（契约字段）。
// ★ 契约优先（captain 裁定）：`weight/probability/delaySec/learnCount/chainPosition` 不在
//    causal-edges.yaml 内 ⇒ 本查询不再返回它们（原返回 4 个非契约字段已撤除）。
// 只读 ：纯 MATCH/OPTIONAL MATCH/UNWIND/RETURN。
// ★ 环规避（§5.7）：**不使用变长路径**（因果不声明传递性，§5.5），而用固定 4 跳 + 逐跳「不回到已访问节点」
//    的过滤器；重复访问被剔除，白名单环 FM-APP-POOL-EXHAUSTED ↔ FM-APP-ENDPOINT-SLOW 因此仍可得两条边。
// ★ 深度上限 4（§5.6）：本查询最大深度即 4，超出者不属于本查询职责（递进由服务层再次调用实现）。
// ============================================================================
MATCH (start:CC_FaultMode {fmCode: $fmCode})
OPTIONAL MATCH (start)-[r1:CC_CAUSES]->(h1:CC_FaultMode)
OPTIONAL MATCH (h1)-[r2:CC_CAUSES]->(h2:CC_FaultMode)
  WHERE NOT h2 IN [start, h1]
OPTIONAL MATCH (h2)-[r3:CC_CAUSES]->(h3:CC_FaultMode)
  WHERE NOT h3 IN [start, h1, h2]
OPTIONAL MATCH (h3)-[r4:CC_CAUSES]->(h4:CC_FaultMode)
  WHERE NOT h4 IN [start, h1, h2, h3]
WITH start,
     [x IN [h1, h2, h3, h4] WHERE x IS NOT NULL] AS hops,
     [x IN [r1, r2, r3, r4] WHERE x IS NOT NULL] AS rels
UNWIND range(0, size(hops) - 1) AS i
RETURN DISTINCT start.fmCode              AS startFmCode,
       hops[i].fmCode                     AS downstreamFmCode,
       hops[i].fmName                     AS downstreamFmName,
       hops[i].fmLayer                    AS downstreamFmLayer,
       hops[i].fmComponentType            AS downstreamComponentType,
       hops[i].fmSeverity                 AS downstreamSeverity,
       i + 1                              AS depth,
       rels[i].contractId                 AS viaEdgeId,
       rels[i].chain                      AS viaChain,
       rels[i].description                AS viaDescription,
       rels[i].provenance                 AS viaProvenance
ORDER BY depth, downstreamFmCode;
