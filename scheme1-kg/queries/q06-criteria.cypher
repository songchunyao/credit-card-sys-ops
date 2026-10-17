// ============================================================================
// Q06 · 故障 → 判据指标与阈值（可判性检查）  scheme1-kg/queries/q06-criteria.cypher
// ----------------------------------------------------------------------------
// 意图 ：docs/03 §8 Q06 —— 取某故障模式的全部判据，并把阈值**以数据形式**解析出来（不解析表达式文本）。
// 参数 ：$fmCode — 故障模式码
// 返回 ：每行一条判据：role/op/operandKind + 指标元数据 + 命中阈值个体的 thrWarning/thrCritical，
//        thresholdState ∈ {calibrated, uncalibrated, not_threshold_criterion}。
//        ★ thresholdState='uncalibrated' 表示契约档位为 null（未标定）——该判据当前**不可机判**，
//          必须由上层显式降级而不是当 0 处理（对应 docs/00 §12 的 3 条未标定档位）。
// 只读 ：纯 MATCH/OPTIONAL MATCH/RETURN；不执行任何规则、不做数值比较。
// ============================================================================
MATCH (fm:CC_FaultMode {fmCode: $fmCode})-[j:CC_JUDGED_BY]->(m:CC_Metric)
OPTIONAL MATCH (th:CC_Threshold)
  WHERE j.operandKind = 'threshold'
    AND th.thrCode = replace(split(j.operandRef, '#')[0], 'threshold:', '')
RETURN fm.fmCode                AS fmCode,
       m.metricCode             AS metricCode,
       m.metricObject           AS metricObject,
       m.metricLayer            AS metricLayer,
       m.metricUnit             AS metricUnit,
       m.metricKgProperty       AS metricKgProperty,
       j.role                   AS role,
       j.op                     AS op,
       j.operandKind            AS operandKind,
       j.operandValue           AS operandValue,
       j.operandRef             AS operandRef,
       th.thrCode               AS thrCode,
       th.thrWarning            AS thrWarning,
       th.thrCritical           AS thrCritical,
       CASE WHEN th.thrCode IS NULL THEN 'not_threshold_criterion'
            WHEN th.thrWarning IS NULL AND th.thrCritical IS NULL THEN 'uncalibrated'
            ELSE 'calibrated' END AS thresholdState
ORDER BY metricCode, role;
