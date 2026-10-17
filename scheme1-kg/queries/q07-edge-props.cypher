// ============================================================================
// Q07 · 因果边属性批量取（契约字段 + 溯源）   scheme1-kg/queries/q07-edge-props.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q07 —— 批量取因果边的**契约字段**（edgeId/chain/description/provenance
//        与条件边的 cond*），供上层做溯源、展示与人工核对。
// 参数 ：$fmCodes — 起点故障模式码列表；传 null 则返回全部因果边。
// 返回 ：每行一条边：from→to、relType、edgeId、chain、description、provenance，
//        条件因果边另带 condMetric/condOp/condKind/condRef。
// ★ 契约优先口径（captain 裁定）：唯一权威 `shared/model/causal-edges.yaml:39-80` 每条边**只有**
//    `id/from/relation/to/chain/evidence/provenance`（E6/E7/E9/E12 另有 `condition`）
//    ⇒ `probability`/`weight`/`delaySec`/`learnCount`/`chainPosition` **不在契约内**，
//      本图不落地、本查询不返回（docs/03 §5.1/§5.3 曾以示例写入它们，且只覆盖 E1/E7 两条边，
//      文档已由 captain 改为契约优先口径）。
//    ⇒ 因此也不存在 `strength = Πweight × Πprobability` 的连乘口径：本查询**不做**任何连乘，
//      不解析传递闭包（传递性未声明，§5.5/§5.8）。
// 只读 ：纯 MATCH/RETURN。
// ============================================================================
MATCH (a:CC_FaultMode)-[r:CC_CAUSES|CC_RELATES_TO]->(b:CC_FaultMode)
WHERE $fmCodes IS NULL OR a.fmCode IN $fmCodes
RETURN a.fmCode           AS fromFmCode,
       b.fmCode           AS toFmCode,
       type(r)            AS relType,
       r.contractId       AS edgeId,
       r.chain            AS chain,
       r.description      AS description,
       r.provenance       AS provenance,
       r.condMetric       AS condMetric,
       r.condOp           AS condOp,
       r.condKind         AS condKind,
       r.condRef          AS condRef
ORDER BY fromFmCode, toFmCode, relType;
