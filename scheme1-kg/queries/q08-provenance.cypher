// ============================================================================
// Q08 · 事实溯源（结论 → 证据 → 事实 id）   scheme1-kg/queries/q08-provenance.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q08 —— 给出任一结论/对象，沿 CC_HAS_EVIDENCE 回到支撑它的证据记录与原始事实 id。
// 参数 ：$factId     — 事实对象 id（任意被投影对象上的 ccfId），null 表示不限
//        $recordId   — 证据记录号（CC_Evidence.recordId，投影器 P1 写入），null 表示不限
//        $metricCode — 只看某指标的证据，null 表示不限
// 返回 ：每行 (事实对象, 证据记录)：factId/factLabels/证据关系 id + 证据的 8 个属性（§12.7）。
// 只读 ：纯 MATCH/RETURN。CC_Evidence 是派生注释型（annotation: true），只由投影器产出，本体侧不写实例。
// ============================================================================
MATCH (fact)-[h:CC_HAS_EVIDENCE]->(ev:CC_Evidence)
WHERE ($factId IS NULL OR fact.ccfId = $factId)
  AND ($recordId IS NULL OR ev.recordId = $recordId)
  AND ($metricCode IS NULL OR ev.metricCode = $metricCode)
RETURN fact.ccfId      AS factId,
       labels(fact)    AS factLabels,
       fact.ccfLayer   AS factLayer,
       h.ccfRelId      AS evidenceRelId,
       ev.recordId     AS recordId,
       ev.metricCode   AS metricCode,
       ev.rawValue     AS rawValue,
       ev.rawUnit      AS rawUnit,
       ev.status       AS status,
       ev.reason       AS reason,
       ev.probe        AS probe,
       ev.capturedAt   AS capturedAt
ORDER BY factId, recordId;
