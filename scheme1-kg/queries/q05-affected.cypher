// ============================================================================
// Q05 · 故障 → 受影响对象（跨层影响面）      scheme1-kg/queries/q05-affected.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q05 —— 取故障模式的跨层影响面：FM 经 CC_AFFECTS 指向的对象实例。
// 参数 ：$fmCode — 指定故障模式码；传 null 则返回全部故障模式的影响面（便于全量报告）。
// 返回 ：每行一条 (FM, 受影响对象) 对：affectedId/affectedLabels/affectedLayer + 两端 severity。
// 只读 ：纯 MATCH/RETURN。
// 说明 ：CC_AFFECTS 的目标类型 = 该 FM 的 fmComponentType（§4.3 门禁），本查询把 labels() 一并返回，
//        以便上层核对「标签与 fmComponentType 一致」而无需再解析类型映射。
// ============================================================================
MATCH (fm:CC_FaultMode)-[a:CC_AFFECTS]->(obj)
WHERE $fmCode IS NULL OR fm.fmCode = $fmCode
RETURN fm.fmCode            AS fmCode,
       fm.fmComponentType   AS fmComponentType,
       fm.fmSeverity        AS fmSeverity,
       a.severity           AS affectsSeverity,
       labels(obj)          AS affectedLabels,
       obj.ccfId            AS affectedId,
       obj.ccfLayer         AS affectedLayer
ORDER BY fmCode, affectedId;
