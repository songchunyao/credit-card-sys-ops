// ============================================================================
// Q04 · 根因溯因 → 上游候选 + 否定性证据     scheme1-kg/queries/q04-upstream.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q04 —— 从给定故障模式出发，沿 CC_CAUSES **逆向**溯源至上游候选根因（最多 4 跳），
//        并带回各上游 FM 的 role='exclusion' 判据（排除法所需的**否定性证据**，§4.2）。
// 参数 ：$fmCode — 观测到的故障模式码
// 返回 ：每个上游候选 FM 一行：depth、经边 contractId/chain/description、negativeEvidence 数组。
// ★ 契约优先（captain 裁定）：`weight/probability/delaySec/learnCount/chainPosition` 不在
//    causal-edges.yaml 内 ⇒ 本查询不再返回它们。
// 只读 ：纯 MATCH/OPTIONAL MATCH/UNWIND/RETURN。
// ★ 环规避同 Q03：不写变长路径，固定 4 跳 + 逐跳「不回到已访问节点」。
// ============================================================================
MATCH (start:CC_FaultMode {fmCode: $fmCode})
OPTIONAL MATCH (u1:CC_FaultMode)-[r1:CC_CAUSES]->(start)
OPTIONAL MATCH (u2:CC_FaultMode)-[r2:CC_CAUSES]->(u1)
  WHERE NOT u2 IN [start, u1]
OPTIONAL MATCH (u3:CC_FaultMode)-[r3:CC_CAUSES]->(u2)
  WHERE NOT u3 IN [start, u1, u2]
OPTIONAL MATCH (u4:CC_FaultMode)-[r4:CC_CAUSES]->(u3)
  WHERE NOT u4 IN [start, u1, u2, u3]
WITH start,
     [x IN [u1, u2, u3, u4] WHERE x IS NOT NULL] AS hops,
     [x IN [r1, r2, r3, r4] WHERE x IS NOT NULL] AS rels
UNWIND range(0, size(hops) - 1) AS i
WITH DISTINCT start, hops[i] AS upstream, i + 1 AS depth, rels[i] AS viaRel
OPTIONAL MATCH (upstream)-[neg:CC_JUDGED_BY]->(negMetric:CC_Metric)
  WHERE neg.role = 'exclusion'
RETURN start.fmCode            AS startFmCode,
       upstream.fmCode         AS upstreamFmCode,
       upstream.fmName         AS upstreamFmName,
       upstream.fmLayer        AS upstreamFmLayer,
       upstream.fmSeverity     AS upstreamFmSeverity,
       depth,
       viaRel.contractId       AS viaEdgeId,
       viaRel.chain            AS viaChain,
       viaRel.description      AS viaDescription,
       collect({metricCode: negMetric.metricCode,
                op: neg.op,
                operandKind: neg.operandKind,
                operandRef: neg.operandRef}) AS negativeEvidence
ORDER BY depth, upstreamFmCode;
