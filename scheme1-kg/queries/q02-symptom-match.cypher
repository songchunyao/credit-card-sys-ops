// ============================================================================
// Q02 · 症状 → 候选故障模式（结构匹配，非关键词）  scheme1-kg/queries/q02-symptom-match.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q02 —— 给定一组**已出现的指标码**，按 CC_JUDGED_BY 的判据结构反查候选故障模式。
//        匹配是「图结构匹配」（FM—JUDGED_BY→Metric），**不是**对 fmName/fmDescription 做关键词 LIKE。
// 参数 ：$metricCodes — 指标码列表（CC_Metric.metricCode，取自 metrics.yaml 的 58 个 code）
// 返回 ：每个候选 FM 一行 + 命中的判据数组（含 op/operandKind/operandRef/role），按命中数降序。
//        role 取 necessary|sufficient|exclusion|distinguishing（§4.2 枚举），供上层做证据强度分级。
// 只读 ：纯 MATCH/RETURN。
// ============================================================================
MATCH (fm:CC_FaultMode)-[j:CC_JUDGED_BY]->(m:CC_Metric)
WHERE m.metricCode IN $metricCodes
WITH fm, collect({metricCode: m.metricCode,
                  op: j.op,
                  operandKind: j.operandKind,
                  operandValue: j.operandValue,
                  operandRef: j.operandRef,
                  role: j.role}) AS matchedCriteria
RETURN fm.fmCode            AS fmCode,
       fm.fmName            AS fmName,
       fm.fmLayer           AS fmLayer,
       fm.fmComponentType   AS fmComponentType,
       fm.fmSeverity        AS fmSeverity,
       fm.fmDetectionMode   AS fmDetectionMode,
       matchedCriteria,
       size(matchedCriteria) AS matchedCount
ORDER BY matchedCount DESC, fmCode;
