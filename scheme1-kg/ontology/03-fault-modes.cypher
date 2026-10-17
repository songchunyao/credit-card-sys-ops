// ============================================================================
// scheme1-kg/ontology/03-fault-modes.cypher
// 方案1 · 故障模式节点（14）+ 因果边（18，其中 4 条条件因果、2 条共现）
// ----------------------------------------------------------------------------
// 规格权威（只读，本文件**不得**出现契约里没有的数值）：
//   shared/model/fault-modes.yaml   18 个故障模式（2026-10-12 路线B 由 14 增至 18）+ 12 个阈值个体（v1.0.2 起；v1.0.1 为 11）
//   shared/model/causal-edges.yaml  22 条因果边 E1–E22（2026-10-12 增 E19–E22；meta.transitive: false；
//                                   meta.cycles 白名单 = 唯一有向环 [FM-APP-POOL-EXHAUSTED, FM-APP-ENDPOINT-SLOW]）
//   docs/03-方案1设计（知识图谱+LLM）.md §4（故障模式与判据）§5（因果边建模）
//
// 落地约定（逐条对应 docs/03，不增不减）：
//   · 节点标签 `CC_FaultMode`，主键 `fmCode`（01-schema.cypher 的 cc_uq_fm_code）
//   · 节点属性 = §4.1 列出的键（文档自称"严格 12 键"实列 11 键 ⇒ GAP-DOC-01）
//   · 因果关系类型 = **单一 `CC_CAUSES`**，方向 原因→结果（§5.1）；out=向下游传播、in=向上游溯因，
//     **不建两条关系**
//   · `relates_to` 共现 = **独立关系类型 `CC_RELATES_TO`**，绝不参与因果收敛（§5.4）
//   · 条件因果 `conditional_causes` = 边上一组 `cond*` 属性（§5.3）
//   · **禁止传递性**：本文件（及任何语料）不得出现 `-[:CC_CAUSES*1..n]->` 变长路径；
//     限深 BFS 属服务层（M2.9）。深度上限 = 4（§5.6）
//   · 幂等：全部 `MERGE`（P8）
//
// ★ 三语义的 LPG 区分方式（可机械还原，故**不额外写** `relation` 属性）：
//     CC_RELATES_TO                    ⇔ relates_to
//     CC_CAUSES + 有 cond* 属性        ⇔ conditional_causes
//     CC_CAUSES + 无 cond* 属性        ⇔ causes
//
// ★ 本文件**不写**的契约字段（无来源即留空并登记 GAP，**不编数字**）：
//     probability / delaySec / chainPosition —— 只有 E1（docs/03 §5.1 示例）与 E7（§5.3 示例）
//     在文档里带 contractId 给出了取值；其余 16 条在契约与文档中**均无取值** ⇒ 见文件尾 GAP 台账。
//     weight = 1.00（§5.1/§5.3 示例 + §5.8「不启用学习，weight 恒定」）；
//     learnCount = 0（§5.8 明写）。
// ============================================================================


// ────────────────────────────────────────────────────────────────────────────
// §1 故障模式节点（14 条，与 fault-modes.yaml#fault_modes 的 code 集合**相等**）
//    fmContractRef = 该条在 fault-modes.yaml 的行号（docs/03 §4.1 要求"契约行号，溯源用"）
//    fmDetectionMode：`direct` = 指标直判；`exclusion` = 排除法（§4.1：仅 F5 用）
//      → 唯一取自 exclusion 的是 FM-APP-DS-CONNECT-FAIL（causal-edges.yaml#leaf_root_causes
//        的子型 FM-APP-AUTH-INCOMPATIBLE 标 `detection_mode: exclusion`，其出口节点即本节点）
//    fmDescription：取 fault-modes.yaml 的 `note` 原文；该条**没有** note 的 7 条不写此属性
//      （契约无 description 字段，不自造描述文字 ⇒ GAP-DOC-02）
// ────────────────────────────────────────────────────────────────────────────

// ---- L1 应用层 ----
MERGE (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'})
  SET fm.fmName = '应用侧连接池耗尽', fm.fmLayer = 'App',
      fm.fmComponentType = 'JdbcDataSource', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F1','F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L36',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'})
  SET fm.fmName = '数据源连接建立失败', fm.fmLayer = 'App',
      fm.fmComponentType = 'JdbcDataSource', fm.fmSeverity = 'critical',
      fm.fmReversible = false, fm.fmChain = ['F5','F6','F7'],
      fm.fmDetectionMode = 'exclusion',
      fm.fmDescription = '★ 叶子歧义点：F5(认证)/F6(网络)/F7(切换) 共享此节点，靠子判据区分',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L51',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'})
  SET fm.fmName = '应用端点响应劣化', fm.fmLayer = 'App',
      fm.fmComponentType = 'AppEndpoint', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F1','F2','F3','F4','F5','F6','F7'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '最上层症状，7 条链的共同出口',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L63',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'})
  SET fm.fmName = '应用实例重启', fm.fmLayer = 'App',
      fm.fmComponentType = 'AppInstance', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L75',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'})
  SET fm.fmName = '应用堆内存压力', fm.fmLayer = 'App',
      fm.fmComponentType = 'AppInstance', fm.fmSeverity = 'warning',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L88',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

// ---- L2 承载层 ----
MERGE (fm:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'})
  SET fm.fmName = '容器 OOM 被杀', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Container', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = 'F4 唯一证据源；宿主机指标可能完全正常',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L101',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-CTR-CPU-THROTTLED'})
  SET fm.fmName = '容器被 CPU 限流', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Container', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F3'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '★ 容器 cpu_usage_ratio 可能不高——只看容器会误判为应用自身慢',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L113',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'})
  SET fm.fmName = '容器网络不可达', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Container', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F6'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '与 F5 的区分：超时（网络）而非立即失败（认证）',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L125',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-HOST-CPU-CONTENTION'})
  SET fm.fmName = '宿主机 CPU 争抢', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Host', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F3'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L139',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-HOST-MEM-PRESSURE'})
  SET fm.fmName = '宿主机内存紧张', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Host', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L151',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

// ---- L3 数据层 ----
MERGE (fm:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
  SET fm.fmName = '数据库慢查询', fm.fmLayer = 'Data',
      fm.fmComponentType = 'Query', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '实测样本：平均 30.8s / TOP 51.9s（upstream-eval 真实数据）',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L163',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'})
  SET fm.fmName = '数据库会话池耗尽', fm.fmLayer = 'Data',
      fm.fmComponentType = 'DatabaseSessionPool', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '与 FM-APP-POOL-EXHAUSTED 语义互斥，绝不可混判',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L175',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'})
  SET fm.fmName = '数据库锁等待', fm.fmLayer = 'Data',
      fm.fmComponentType = 'Session', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L188',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'})
  SET fm.fmName = '数据库实例故障/切换', fm.fmLayer = 'Data',
      fm.fmComponentType = 'GaussDBInstance', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F7'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-FULL'})
  SET fm.fmName = '宿主磁盘空间耗尽', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Host', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-IO-ERROR'})
  SET fm.fmName = '宿主磁盘 I/O 错误', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Host', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F4'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-HOST-NET-IFACE-DOWN'})
  SET fm.fmName = '宿主网卡 down', fm.fmLayer = 'Runtime',
      fm.fmComponentType = 'Host', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F6'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-REPLICATION-LAG'})
  SET fm.fmName = '主备复制延迟/中断', fm.fmLayer = 'Data',
      fm.fmComponentType = 'GaussDBCluster', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F7'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-UNUSED-INDEX'})
  SET fm.fmName = '索引未使用（索引失效）', fm.fmLayer = 'Data',
      fm.fmComponentType = 'Table', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L200',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-DEADLOCK'})
  SET fm.fmName = '数据库死锁', fm.fmLayer = 'Data',
      fm.fmComponentType = 'GaussDBCluster', fm.fmSeverity = 'critical',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '累计量（pg_stat_database.deadlocks）⇒ 必须靠采集侧窗口 delta 判定（P0.5）。死于死锁的事务被 DB 主动 abort，会连带抬升回滚率。',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L268',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-IDLE-IN-TX'})
  SET fm.fmName = '持锁不提交堆积（idle in transaction）', fm.fmLayer = 'Data',
      fm.fmComponentType = 'GaussDBInstance', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '与 FM-DB-LOCK-WAIT 互补：本条是**持有**锁却不提交（可能是长事务/慢处理/忘记提交），后者是**等待**锁。漏提交会把后续竞争者拖进锁等待链。',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L280',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-SEQ-SCAN'})
  SET fm.fmName = '全表扫描（索引失效）', fm.fmLayer = 'Data',
      fm.fmComponentType = 'Table', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '逐表判定（P0.5 的 delta 键含 obj_id）。对应注入：函数包裹条件 / 前置通配 LIKE。是 FM-DB-SLOW-QUERY 的**成因**之一，但不等价：慢查询也可能源于锁等待或网络。',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L292',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());

MERGE (fm:CC_FaultMode {fmCode:'FM-DB-ROLLBACK-STORM'})
  SET fm.fmName = '事务回滚风暴', fm.fmLayer = 'Data',
      fm.fmComponentType = 'GaussDBCluster', fm.fmSeverity = 'major',
      fm.fmReversible = true, fm.fmChain = ['F2'],
      fm.fmDetectionMode = 'direct',
      fm.fmDescription = '累计量 ⇒ 靠窗口 delta。对应注入：固定幂等键的唯一键冲突（biz_unique_conflict）。与 FM-DB-DEADLOCK 的区别：死锁只是回滚的**一种**成因，纯冲突回滚不产生 deadlock 计数。',
      fm.fmContractRef = 'shared/model/fault-modes.yaml#L304',
      fm.fmCreatedAt = coalesce(fm.fmCreatedAt, datetime());


// ────────────────────────────────────────────────────────────────────────────
// §2 因果边（27 条 = causal-edges.yaml#edges 的 id 集合**相等**）
//    · 无条件因果 12 条：E1–E5 / E8 / E10 / E11 / E13 / E14 / E15 / E18 / E19–E22 → CC_CAUSES
//    · 条件因果  4 条：E6 / E7 / E9 / E12 → CC_CAUSES + cond* 属性
//    · 共现      2 条：E16 / E17 → CC_RELATES_TO（非因果）
//    · ★ 边属性集 = **契约优先**（captain 裁定，2026 本轮）：`shared/model/causal-edges.yaml:39-80`
//      每条边**只有** `id/from/relation/to/chain/evidence/provenance`（E6/E7/E9/E12 另有 `condition`）
//      ⇒ 本文件只写 `contractId`（= `id`）/ `chain` / `description`（= `evidence` 原文）/ `provenance`，
//        条件边再加 `condMetric`/`condOp`/`condKind`/`condRef`。
//      曾按 docs/03 §5.1/§5.3 **示例**写入的 `weight`/`learnCount`/`probability`/`delaySec`/
//      `chainPosition` **在契约里不存在**，已全部撤除（示例只覆盖 E1/E7 两条边，给 2/18 条边赋
//      「概率/权重」会污染排序口径，且方案2 侧无法对称 ⇒ 见 GAP-DOC-03）。
//    · description = causal-edges.yaml 的 `evidence` 原文（逐字）
//    · provenance  = `authored:<契约文件>#<边 id>`（docs/03 §5.1 的格式）
//    · chain       = causal-edges.yaml 的 `chain` 原文（E18 为 ['F5','F6','F7']）
//    · cond* 的阈值一律写**引用**（threshold:<名>#<档位> / entity_property:<类型>.<属性>），
//      不写 0.9 / 5 之类数字（契约 threshold_policy.hardcode_forbidden; §5.3 示例写 `condValue: 0.9`
//      属示例性硬编码，本文件改用 condRef ⇒ 见 GAP-DOC-05）
// ────────────────────────────────────────────────────────────────────────────

// --- F2：核心跨层链（DB → App，方向反直觉） ---
MATCH (a:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'}), (b:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'})
MERGE (a)-[r:CC_CAUSES {contractId:'E1'}]->(b)
  SET r.chain = 'F2',
      r.description = '慢查询长时间占用会话 → 应用侧连接被耗尽',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E1';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'}), (b:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
MERGE (a)-[r:CC_CAUSES {contractId:'E2'}]->(b)
  SET r.chain = 'F2',
      r.description = '锁等待表现为执行时间长',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E2';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'}), (b:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E3'}]->(b)
  SET r.chain = 'F2',
      r.description = '数据库拒绝新会话 → 应用连接失败',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E3';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'}), (b:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E4'}]->(b)
  SET r.chain = 'F2',
      r.description = '慢查询堆积会话',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E4';

// --- F1：应用内部（含反馈环 E6） ---
MATCH (a:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}), (b:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'})
MERGE (a)-[r:CC_CAUSES {contractId:'E5'}]->(b)
  SET r.chain = 'F1',
      r.description = '无可用连接 → 请求排队 → P99 劣化',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E5';

// ★ 条件因果（反馈环；与 E5 共同构成全图唯一有向环，白名单见 causal-edges.yaml#meta.cycles）
MATCH (a:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'}), (b:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'})
MERGE (a)-[r:CC_CAUSES {contractId:'E6'}]->(b)
  SET r.chain = 'F1',
      r.description = '高负载下排队反噬（反馈环，成环案例之一）',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E6',
      r.condMetric = 'app.endpoint.qps', r.condOp = '>',
      r.condKind = 'threshold', r.condRef = 'qps_high#critical';

// --- F3：Host → Container → App ---
MATCH (a:CC_FaultMode {fmCode:'FM-HOST-CPU-CONTENTION'}), (b:CC_FaultMode {fmCode:'FM-CTR-CPU-THROTTLED'})
MERGE (a)-[r:CC_CAUSES {contractId:'E7'}]->(b)
  SET r.chain = 'F3',
      r.description = '宿主机争抢超过阈值时才触发 cgroup 限流',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E7',
      r.condMetric = 'host.cpu_usage_ratio', r.condOp = '>',
      r.condKind = 'threshold', r.condRef = 'cpu_usage_ratio_severe#critical';

MATCH (a:CC_FaultMode {fmCode:'FM-CTR-CPU-THROTTLED'}), (b:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'})
MERGE (a)-[r:CC_CAUSES {contractId:'E8'}]->(b)
  SET r.chain = 'F3',
      r.description = '容器被限流 → 应用线程得不到 CPU → 响应变慢',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E8';

// --- F4：两条并存路径（同一现象、两条根因） ---
MATCH (a:CC_FaultMode {fmCode:'FM-HOST-MEM-PRESSURE'}), (b:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'})
MERGE (a)-[r:CC_CAUSES {contractId:'E9'}]->(b)
  SET r.chain = 'F4',
      r.description = '宿主机内存紧张才触发容器 OOM（路径甲：宿主机紧张）',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E9',
      r.condMetric = 'host.mem_available_ratio', r.condOp = '<',
      r.condKind = 'threshold', r.condRef = 'mem_available_ratio_severe#critical';

MATCH (a:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'}), (b:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'})
MERGE (a)-[r:CC_CAUSES {contractId:'E10'}]->(b)
  SET r.chain = 'F4',
      r.description = '容器被杀 → 应用实例重启',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E10';

MATCH (a:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'}), (b:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'})
MERGE (a)-[r:CC_CAUSES {contractId:'E11'}]->(b)
  SET r.chain = 'F4',
      r.description = '重启期间请求失败',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E11';

// ★ 条件因果（路径乙：容器 limit 过小 —— 与 E9 的宿主机关联不同）
MATCH (a:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'}), (b:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'})
MERGE (a)-[r:CC_CAUSES {contractId:'E12'}]->(b)
  SET r.chain = 'F4',
      r.description = '应用自身堆膨胀触及容器 limit（路径乙：容器 limit 过小）',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E12',
      r.condMetric = 'container.mem_working_set_mb', r.condOp = '>',
      r.condKind = 'entity_property', r.condRef = 'Container.memLimitMb';

// --- F6 / F7：连接失败的其余两条来源 ---
MATCH (a:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'}), (b:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E13'}]->(b)
  SET r.chain = 'F6',
      r.description = '容器到 DB 网络不通 → 连接超时',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E13';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'}), (b:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E14'}]->(b)
  SET r.chain = 'F7',
      r.description = '实例故障/切换 → 连接瞬时中断',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E14';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'}), (b:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E15'}]->(b)
  SET r.chain = 'F7',
      r.description = '切换期间会话堆积',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E15';

// --- 共现（非因果，不参与收敛）---
MATCH (a:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'}), (b:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'})
MERGE (a)-[r:CC_RELATES_TO {contractId:'E16'}]->(b)
  SET r.chain = 'F2',
      r.description = '慢查询与锁等待常同时出现，但不互为因果',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E16';

MATCH (a:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'}), (b:CC_FaultMode {fmCode:'FM-HOST-MEM-PRESSURE'})
MERGE (a)-[r:CC_RELATES_TO {contractId:'E17'}]->(b)
  SET r.chain = 'F4',
      r.description = '同一宿主机上常同时出现',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E17';

MATCH (a:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'}), (b:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'})
MERGE (a)-[r:CC_CAUSES {contractId:'E18'}]->(b)
  SET r.chain = ['F5','F6','F7'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E18';

MATCH (a:CC_FaultMode {fmCode:'FM-HOST-DISK-FULL'}), (b:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'})
MERGE (a)-[r:CC_CAUSES {contractId:'E23'}]->(b)
  SET r.chain = ['F4'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E23';

MATCH (a:CC_FaultMode {fmCode:'FM-HOST-DISK-IO-ERROR'}), (b:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'})
MERGE (a)-[r:CC_CAUSES {contractId:'E24'}]->(b)
  SET r.chain = ['F4'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E24';

MATCH (a:CC_FaultMode {fmCode:'FM-HOST-NET-IFACE-DOWN'}), (b:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'})
MERGE (a)-[r:CC_CAUSES {contractId:'E25'}]->(b)
  SET r.chain = ['F6'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E25';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-REPLICATION-LAG'}), (b:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'})
MERGE (a)-[r:CC_CAUSES {contractId:'E26'}]->(b)
  SET r.chain = ['F7'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E26';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-UNUSED-INDEX'}), (b:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
MERGE (a)-[r:CC_CAUSES {contractId:'E27'}]->(b)
  SET r.chain = ['F2'],
      r.description = 'F5/F6/F7 的共同出口',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E27';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-DEADLOCK'}), (b:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
MERGE (a)-[r:CC_CAUSES {contractId:'E19'}]->(b)
  SET r.chain = ['F2'],
      r.description = '死锁检测主动 abort 事务 ⇒ 应用重试 ⇒ 执行时间被拉长',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E19';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-IDLE-IN-TX'}), (b:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'})
MERGE (a)-[r:CC_CAUSES {contractId:'E20'}]->(b)
  SET r.chain = ['F2'],
      r.description = '持锁不提交 ⇒ 后续竞争者排队等锁',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E20';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-SEQ-SCAN'}), (b:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
MERGE (a)-[r:CC_CAUSES {contractId:'E21'}]->(b)
  SET r.chain = ['F2'],
      r.description = '索引失效退化为顺序扫描 ⇒ 单语句执行时间上升',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E21';

MATCH (a:CC_FaultMode {fmCode:'FM-DB-ROLLBACK-STORM'}), (b:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'})
MERGE (a)-[r:CC_CAUSES {contractId:'E22'}]->(b)
  SET r.chain = ['F2'],
      r.description = '大量事务回滚 ⇒ 有效吞吐下降、重试放大 ⇒ 观测到的执行时间上升',
      r.provenance = 'authored:shared/model/causal-edges.yaml#E22';


// ────────────────────────────────────────────────────────────────────────────
// §3 指标节点（`CC_Metric`，**76 条** = shared/model/metrics.yaml 全量；主键 `metricCode`）
//    docs/03:191-196 §4.2 把判据写成 `(fm)-[:CC_JUDGED_BY]->(m:CC_Metric {metricCode:...})`
//    ⇒ 判据的目标是**指标节点**，指标必须以数据形式存在（P6「每条声明必须有数据」）。
//    属性取值**全部来自契约，无一处自造**：
//      metricObject / metricLayer / metricUnit / metricType / metricSource / metricThresholdRef
//        ← shared/model/metrics.yaml（object / layer / unit / type / source / threshold_ref）
//      metricKgProperty ← shared/mapping/type-map.yaml#metrics[<code>].kg_property
//        （= 该指标在**图里的真实属性名**；T5「消费层属性名必须存在」靠它核对）
//      metricContractRef ← 该条在 metrics.yaml 的行号
//    ★ 除 `metricCode` 外的属性**命名** docs/03 未作规定 ⇒ 本文件按上述实现，登记 GAP-DOC-12。
//    ★ 阈值**数值**不在指标节点上，而在 §4 的阈值节点上（阈值即数据，不写进规则）。
//    表驱动：58 行数据 1 条语句（**不是** 58 条语句——成本台账按语句数为主计量，不注水）。
// ────────────────────────────────────────────────────────────────────────────
UNWIND [
  {metricCode:'app.uptime_s',                      metricObject:'AppInstance',          metricLayer:'App',     metricUnit:'s',      metricType:'integer', metricSource:'app',       metricKgProperty:'uptimeS',             metricContractRef:'shared/model/metrics.yaml#L22'},
  {metricCode:'app.heap_used_ratio',               metricObject:'AppInstance',          metricLayer:'App',     metricUnit:'ratio',  metricType:'decimal', metricSource:'app',       metricKgProperty:'heapUsedRatio',       metricContractRef:'shared/model/metrics.yaml#L30'},
  {metricCode:'app.gc_pause_ms',                   metricObject:'AppInstance',          metricLayer:'App',     metricUnit:'ms',     metricType:'decimal', metricSource:'app',       metricKgProperty:'gcPauseMs',           metricContractRef:'shared/model/metrics.yaml#L38'},
  {metricCode:'app.thread_count',                  metricObject:'AppInstance',          metricLayer:'App',     metricUnit:'个',      metricType:'integer', metricSource:'app',       metricKgProperty:'threadCount',         metricContractRef:'shared/model/metrics.yaml#L45'},
  {metricCode:'app.endpoint.p99_ms',               metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'ms',     metricType:'decimal', metricSource:'app',       metricKgProperty:'p99Ms',               metricContractRef:'shared/model/metrics.yaml#L52'},
  {metricCode:'app.endpoint.error_rate',           metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'ratio',  metricType:'decimal', metricSource:'app',       metricKgProperty:'errorRate',           metricContractRef:'shared/model/metrics.yaml#L60'},
  {metricCode:'app.endpoint.qps',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'次/s',    metricType:'decimal', metricSource:'app',       metricKgProperty:'qps',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.tps',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'次/s',    metricType:'decimal', metricSource:'app',       metricKgProperty:'tps',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.p50_ms',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'ms',    metricType:'decimal', metricSource:'app',       metricKgProperty:'p50Ms',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.p95_ms',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'ms',    metricType:'decimal', metricSource:'app',       metricKgProperty:'p95Ms',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.max_ms',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'ms',    metricType:'decimal', metricSource:'app',       metricKgProperty:'maxMs',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.success_total',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'次',    metricType:'integer', metricSource:'app',       metricKgProperty:'successTotal',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.endpoint.fail_total',                  metricObject:'AppEndpoint',          metricLayer:'App',     metricUnit:'次',    metricType:'integer', metricSource:'app',       metricKgProperty:'failTotal',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'host.fs_max_used_pct',                  metricObject:'Host',          metricLayer:'Runtime',     metricUnit:'percent',    metricType:'decimal', metricSource:'os',       metricKgProperty:'fsMaxUsedPct',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'host.disk_io_errors_total',                  metricObject:'Host',          metricLayer:'Runtime',     metricUnit:'次',    metricType:'integer', metricSource:'os',       metricKgProperty:'diskIoErrorsTotal',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'host.net_iface_down_count',                  metricObject:'Host',          metricLayer:'Runtime',     metricUnit:'个',    metricType:'integer', metricSource:'os',       metricKgProperty:'netIfaceDownCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'host.net_iface_total',                  metricObject:'Host',          metricLayer:'Runtime',     metricUnit:'个',    metricType:'integer', metricSource:'os',       metricKgProperty:'netIfaceTotal',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replay_lag_bytes_max',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'bytes',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replayLagBytesMax',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replication_not_streaming_count',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replicationNotStreamingCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.alert_active_count',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'alertActiveCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.table.index_count',                  metricObject:'Table',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'indexCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.table.unused_index_count',                  metricObject:'Table',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'unusedIndexCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replication_pending_bytes',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'bytes',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replicationPendingBytes',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replication_flush_pending_bytes',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'bytes',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replicationFlushPendingBytes',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.walsender_count',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'walsenderCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replication_slot_count',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replicationSlotCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.replication_slot_active_count',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'个',    metricType:'integer', metricSource:'gaussdb',       metricKgProperty:'replicationSlotActiveCount',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'db.cluster.is_standby',                  metricObject:'GaussDBCluster',          metricLayer:'Data',     metricUnit:'',    metricType:'boolean', metricSource:'gaussdb',       metricKgProperty:'isStandby',                 metricContractRef:'shared/model/metrics.yaml#L68'},
  {metricCode:'app.datasource.active_connections', metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'个',      metricType:'integer', metricSource:'app',       metricKgProperty:'activeConnections',   metricContractRef:'shared/model/metrics.yaml#L76',  metricThresholdRef:'JdbcDataSource.maxPoolSize'},
  {metricCode:'app.datasource.idle_connections',   metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'个',      metricType:'integer', metricSource:'app',       metricKgProperty:'idleConnections',     metricContractRef:'shared/model/metrics.yaml#L84'},
  {metricCode:'app.datasource.pending_threads',    metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'个',      metricType:'integer', metricSource:'app',       metricKgProperty:'pendingThreads',      metricContractRef:'shared/model/metrics.yaml#L91'},
  {metricCode:'app.datasource.acquire_wait_ms',    metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'ms',     metricType:'decimal', metricSource:'app',       metricKgProperty:'acquireWaitMs',       metricContractRef:'shared/model/metrics.yaml#L99'},
  {metricCode:'app.datasource.connect_latency_ms', metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'ms',     metricType:'decimal', metricSource:'app',       metricKgProperty:'connectLatencyMs',    metricContractRef:'shared/model/metrics.yaml#L106'},
  {metricCode:'app.datasource.connect_fail_total', metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'次',      metricType:'integer', metricSource:'app',       metricKgProperty:'connectFailTotal',    metricContractRef:'shared/model/metrics.yaml#L114'},
  {metricCode:'app.datasource.timeout_total',      metricObject:'JdbcDataSource',       metricLayer:'App',     metricUnit:'次',      metricType:'integer', metricSource:'app',       metricKgProperty:'timeoutTotal',        metricContractRef:'shared/model/metrics.yaml#L121'},
  {metricCode:'container.cpu_usage_ratio',         metricObject:'Container',            metricLayer:'Runtime', metricUnit:'ratio',  metricType:'decimal', metricSource:'container', metricKgProperty:'cpuUsageRatio',       metricContractRef:'shared/model/metrics.yaml#L129'},
  {metricCode:'container.cpu_throttled_periods',   metricObject:'Container',            metricLayer:'Runtime', metricUnit:'次',      metricType:'integer', metricSource:'container', metricKgProperty:'cpuThrottledPeriods', metricContractRef:'shared/model/metrics.yaml#L137'},
  {metricCode:'container.mem_working_set_mb',      metricObject:'Container',            metricLayer:'Runtime', metricUnit:'MB',     metricType:'integer', metricSource:'container', metricKgProperty:'memWorkingSetMb',     metricContractRef:'shared/model/metrics.yaml#L145', metricThresholdRef:'Container.memLimitMb'},
  {metricCode:'container.oom_kill_total',          metricObject:'Container',            metricLayer:'Runtime', metricUnit:'次',      metricType:'integer', metricSource:'container', metricKgProperty:'oomKillTotal',        metricContractRef:'shared/model/metrics.yaml#L153'},
  {metricCode:'container.restart_total',           metricObject:'Container',            metricLayer:'Runtime', metricUnit:'次',      metricType:'integer', metricSource:'container', metricKgProperty:'restartTotal',        metricContractRef:'shared/model/metrics.yaml#L161'},
  {metricCode:'container.status',                  metricObject:'Container',            metricLayer:'Runtime', metricUnit:null,     metricType:'string',  metricSource:'container', metricKgProperty:'status',              metricContractRef:'shared/model/metrics.yaml#L168'},
  {metricCode:'host.cpu_usage_ratio',              metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'ratio',  metricType:'decimal', metricSource:'os',        metricKgProperty:'cpuUsageRatio',       metricContractRef:'shared/model/metrics.yaml#L176'},
  {metricCode:'host.mem_available_ratio',          metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'ratio',  metricType:'decimal', metricSource:'os',        metricKgProperty:'memAvailableRatio',   metricContractRef:'shared/model/metrics.yaml#L184'},
  {metricCode:'host.load1',                        metricObject:'Host',                 metricLayer:'Runtime', metricUnit:null,     metricType:'decimal', metricSource:'os',        metricKgProperty:'load1',               metricContractRef:'shared/model/metrics.yaml#L192'},
  {metricCode:'host.load5',                        metricObject:'Host',                 metricLayer:'Runtime', metricUnit:null,     metricType:'decimal', metricSource:'os',        metricKgProperty:'load5',               metricContractRef:'shared/model/metrics.yaml#L199'},
  {metricCode:'host.disk_util',                    metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'percent',metricType:'decimal', metricSource:'os',        metricKgProperty:'diskUtil',            metricContractRef:'shared/model/metrics.yaml#L206'},
  {metricCode:'host.disk_io_wait',                 metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'percent',metricType:'decimal', metricSource:'os',        metricKgProperty:'diskIoWait',          metricContractRef:'shared/model/metrics.yaml#L214'},
  {metricCode:'host.conntrack_count',              metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'个',      metricType:'integer', metricSource:'os',        metricKgProperty:'conntrackCount',      metricContractRef:'shared/model/metrics.yaml#L222'},
  {metricCode:'host.net_rx_bytes',                 metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'bytes',  metricType:'integer', metricSource:'os',        metricKgProperty:'netRxBytes',          metricContractRef:'shared/model/metrics.yaml#L229'},
  {metricCode:'host.net_tx_bytes',                 metricObject:'Host',                 metricLayer:'Runtime', metricUnit:'bytes',  metricType:'integer', metricSource:'os',        metricKgProperty:'netTxBytes',          metricContractRef:'shared/model/metrics.yaml#L236'},
  {metricCode:'db.cluster.up_nodes',               metricObject:'GaussDBCluster',       metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'upNodes',             metricContractRef:'shared/model/metrics.yaml#L244'},
  {metricCode:'db.cluster.down_nodes',             metricObject:'GaussDBCluster',       metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'downNodes',           metricContractRef:'shared/model/metrics.yaml#L251'},
  {metricCode:'db.cluster.replication_lag_mb',     metricObject:'GaussDBCluster',       metricLayer:'Data',    metricUnit:'MB',     metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'replicationLagMb',    metricContractRef:'shared/model/metrics.yaml#L258'},
  {metricCode:'db.cluster.switchover_total',       metricObject:'GaussDBCluster',       metricLayer:'Data',    metricUnit:'次',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'switchoverTotal',     metricContractRef:'shared/model/metrics.yaml#L265'},
  {metricCode:'db.instance.cpu_usage_ratio',       metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'ratio',  metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'cpuUsageRatio',       metricContractRef:'shared/model/metrics.yaml#L273'},
  {metricCode:'db.instance.mem_usage_ratio',       metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'ratio',  metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'memUsageRatio',       metricContractRef:'shared/model/metrics.yaml#L281'},
  {metricCode:'db.instance.session_count',         metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'sessionCount',        metricContractRef:'shared/model/metrics.yaml#L289'},
  {metricCode:'db.instance.max_connections',       metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'maxConnections',      metricContractRef:'shared/model/metrics.yaml#L296'},
  {metricCode:'db.instance.waiting_connections',   metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'waitingConnections',  metricContractRef:'shared/model/metrics.yaml#L304'},
  {metricCode:'db.instance.long_txn_age_s',        metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'s',       metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'longTxnAgeS',         metricContractRef:'shared/model/metrics.yaml#L311'},
  {metricCode:'db.instance.io_wait_pct',           metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'percent',metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'ioWaitPct',           metricContractRef:'shared/model/metrics.yaml#L318'},
  {metricCode:'db.instance.buffer_hit_ratio',      metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:'percent',metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'bufferHitRatio',      metricContractRef:'shared/model/metrics.yaml#L327'},
  {metricCode:'db.instance.up',                    metricObject:'GaussDBInstance',      metricLayer:'Data',    metricUnit:null,     metricType:'boolean', metricSource:'gaussdb',   metricKgProperty:'up',                  metricContractRef:'shared/model/metrics.yaml#L335'},
  {metricCode:'db.pool.session_used_ratio',        metricObject:'DatabaseSessionPool',  metricLayer:'Data',    metricUnit:'ratio',  metricType:'decimal', metricSource:'derived',   metricKgProperty:'sessionUsedRatio',    metricContractRef:'shared/model/metrics.yaml#L343'},
  {metricCode:'db.pool.session_waiting',           metricObject:'DatabaseSessionPool',  metricLayer:'Data',    metricUnit:'个',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'sessionWaiting',      metricContractRef:'shared/model/metrics.yaml#L352'},
  {metricCode:'db.pool.conn_rejected_total',       metricObject:'DatabaseSessionPool',  metricLayer:'Data',    metricUnit:'次',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'connRejectedTotal',   metricContractRef:'shared/model/metrics.yaml#L359'},
  {metricCode:'db.session.state',                  metricObject:'Session',              metricLayer:'Data',    metricUnit:null,     metricType:'string',  metricSource:'gaussdb',   metricKgProperty:'state',               metricContractRef:'shared/model/metrics.yaml#L366'},
  {metricCode:'db.session.wait_event',             metricObject:'Session',              metricLayer:'Data',    metricUnit:null,     metricType:'string',  metricSource:'gaussdb',   metricKgProperty:'waitEvent',           metricContractRef:'shared/model/metrics.yaml#L374'},
  {metricCode:'db.session.xact_age_s',             metricObject:'Session',              metricLayer:'Data',    metricUnit:'s',       metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'xactAgeS',            metricContractRef:'shared/model/metrics.yaml#L382'},
  {metricCode:'db.session.query_elapsed_s',        metricObject:'Session',              metricLayer:'Data',    metricUnit:'s',       metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'queryElapsedS',       metricContractRef:'shared/model/metrics.yaml#L389'},
  {metricCode:'db.query.mean_exec_ms',             metricObject:'Query',                metricLayer:'Data',    metricUnit:'ms',      metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'meanExecMs',          metricContractRef:'shared/model/metrics.yaml#L396'},
  {metricCode:'db.query.max_exec_ms',              metricObject:'Query',                metricLayer:'Data',    metricUnit:'ms',      metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'maxExecMs',           metricContractRef:'shared/model/metrics.yaml#L404'},
  {metricCode:'db.query.exec_count',               metricObject:'Query',                metricLayer:'Data',    metricUnit:'次',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'execCount',           metricContractRef:'shared/model/metrics.yaml#L411'},
  {metricCode:'db.query.returned_rows',            metricObject:'Query',                metricLayer:'Data',    metricUnit:'行',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'returnedRows',        metricContractRef:'shared/model/metrics.yaml#L418'},
  {metricCode:'db.query.seq_scan_ratio',           metricObject:'Query',                metricLayer:'Data',    metricUnit:'ratio',   metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'seqScanRatio',        metricContractRef:'shared/model/metrics.yaml#L425'},
  {metricCode:'db.table.live_tuples',              metricObject:'Table',                metricLayer:'Data',    metricUnit:'行',      metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'liveTuples',          metricContractRef:'shared/model/metrics.yaml#L433'},
  {metricCode:'db.table.dead_tuple_ratio',         metricObject:'Table',                metricLayer:'Data',    metricUnit:'ratio',   metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'deadTupleRatio',      metricContractRef:'shared/model/metrics.yaml#L440'},
  {metricCode:'db.table.last_analyze_age_h',       metricObject:'Table',                metricLayer:'Data',    metricUnit:'h',       metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'lastAnalyzeAgeH',     metricContractRef:'shared/model/metrics.yaml#L448'},
  {metricCode:'db.table.size_mb',                  metricObject:'Table',                metricLayer:'Data',    metricUnit:'MB',      metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'sizeMb',              metricContractRef:'shared/model/metrics.yaml#L455'},
  {metricCode:'db.cluster.deadlocks',                             metricObject:'GaussDBCluster',                         metricLayer:'Data',    metricUnit:'次',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'deadlocks',                metricContractRef:'shared/model/metrics.yaml#L463'},
  {metricCode:'db.cluster.xact_rollback',                         metricObject:'GaussDBCluster',                         metricLayer:'Data',    metricUnit:'次',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'xactRollback',             metricContractRef:'shared/model/metrics.yaml#L471'},
  {metricCode:'db.cluster.slow_query_count',                      metricObject:'GaussDBCluster',                         metricLayer:'Data',    metricUnit:'次',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'slowQueryCount',           metricContractRef:'shared/model/metrics.yaml#L479'},
  {metricCode:'db.instance.active_sessions',                      metricObject:'GaussDBInstance',                        metricLayer:'Data',    metricUnit:'个',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'activeSessions',           metricContractRef:'shared/model/metrics.yaml#L487'},
  {metricCode:'db.instance.idle_in_xact',                         metricObject:'GaussDBInstance',                        metricLayer:'Data',    metricUnit:'个',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'idleInXact',               metricContractRef:'shared/model/metrics.yaml#L495'},
  {metricCode:'db.instance.lock_waits',                           metricObject:'GaussDBInstance',                        metricLayer:'Data',    metricUnit:'个',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'lockWaits',                metricContractRef:'shared/model/metrics.yaml#L503'},
  {metricCode:'db.cluster.temp_files',                           metricObject:'GaussDBCluster',                                   metricLayer:'Data',    metricUnit:'个',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'tempFiles',                metricContractRef:'shared/model/metrics.yaml#L511'},
  {metricCode:'db.cluster.temp_file_written',                    metricObject:'GaussDBCluster',                                   metricLayer:'Data',    metricUnit:'',         metricType:'boolean', metricSource:'gaussdb',   metricKgProperty:'tempFileWritten',          metricContractRef:'shared/model/metrics.yaml#L519'},
  {metricCode:'db.table.seq_scan',                                metricObject:'Table',                                  metricLayer:'Data',    metricUnit:'次',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'seqScan',                  metricContractRef:'shared/model/metrics.yaml#L527'},
  {metricCode:'db.table.idx_scan',                                metricObject:'Table',                                  metricLayer:'Data',    metricUnit:'次',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'idxScan',                  metricContractRef:'shared/model/metrics.yaml#L535'},
  {metricCode:'db.table.dead_tuples',                             metricObject:'Table',                                  metricLayer:'Data',    metricUnit:'行',        metricType:'integer', metricSource:'gaussdb',   metricKgProperty:'deadTuples',               metricContractRef:'shared/model/metrics.yaml#L543'},
  {metricCode:'db.table.hot_update_ratio_pct',                    metricObject:'Table',                                  metricLayer:'Data',    metricUnit:'percent',  metricType:'decimal', metricSource:'gaussdb',   metricKgProperty:'hotUpdateRatioPct',        metricContractRef:'shared/model/metrics.yaml#L551'}
] AS m
MERGE (n:CC_Metric {metricCode: m.metricCode})
  SET n += m;


// ────────────────────────────────────────────────────────────────────────────
// §4 阈值节点（`CC_Threshold`，12 条 = fault-modes.yaml#thresholds 全量；主键 `thrCode`）
//    阈值**必须是数据**（G3：规则里零数字字面量 ⇒ 数字只能活在这里）。
//    `thrCode` = fault-modes.yaml 的 `name`（规则与判据用 `threshold:<名>#<warning|critical>` 引用）。
//    `warning: null`（未标定）⇒ **不写该属性**（Neo4j 无 null 属性；属性缺失即"未标定"），
//      ★ 2026-10-12：acquire_wait_ms_light 已标定（520/1560）⇒ 原 3 条 WARN 降为 2 条：
//      load1_per_core_light#warning / qps_high#critical 当前不可机判。
//    ★ 属性命名 docs/03 未规定 ⇒ 登记 GAP-DOC-12。
// ────────────────────────────────────────────────────────────────────────────
UNWIND [
  {thrCode:'acquire_wait_ms_light',      thrMetric:'app.datasource.acquire_wait_ms',  thrWarning:520,  thrCritical:1560, thrRationale:'★ 2026-10-12 标定（原两档 null ⇒ FM-APP-POOL-EXHAUSTED 不可机判）：样本 = 真机历史日志全部轮次的 `连接池获取 … 耗时=Xms`（n=91），按应用自身判据分层剔除启动填充：INFO 层 n=64 健康期 P95=260ms × k=2.0 ⇒ warning=520；warning × k_crit=3.0 ⇒ critical=1560。交叉验证：应用自判『偏慢』最小耗时 532ms（相差 2.3%）。',                                            thrContractRef:'shared/model/fault-modes.yaml#L219'},
  {thrCode:'fs_usage_pct_light',       thrMetric:'host.fs_max_used_pct',           thrWarning:80,   thrCritical:90,   thrRationale:'政策阈值（非统计标定）：容量可用性边界按运维惯例80/90，留 10% 写余量给 WAL/临时文件；实测最小余量 68%（fs_max_used_pct 最大 32.0%）'},
  {thrCode:'session_used_ratio_light',   thrMetric:'db.pool.session_used_ratio',      thrWarning:0.5, thrCritical:1.0, thrRationale:'待标定：沿用工程基线，需按 max_connections 与实际峰值重新定档',            thrContractRef:'shared/model/fault-modes.yaml#L224'},
  {thrCode:'session_used_ratio_severe',  thrMetric:'db.pool.session_used_ratio',      thrWarning:0.8, thrCritical:1.0, thrRationale:'待标定',                                                            thrContractRef:'shared/model/fault-modes.yaml#L229'},
  {thrCode:'heap_used_ratio_light',      thrMetric:'app.heap_used_ratio',             thrWarning:0.75, thrCritical:0.90, thrRationale:'待标定：JVM 常规经验值，需实测 GC 行为后定档',                      thrContractRef:'shared/model/fault-modes.yaml#L234'},
  {thrCode:'gc_pause_ms_light',          thrMetric:'app.gc_pause_ms',                 thrWarning:200,  thrCritical:1000, thrRationale:'待标定',                                                            thrContractRef:'shared/model/fault-modes.yaml#L239'},
  {thrCode:'load1_per_core_light',       thrMetric:'host.load1',                      thrWarning:null, thrCritical:null, thrRationale:'用户裁定 ②(A)：改为按 Host.cpuCores 归一化（判据比较 host.load1 / Host.cpuCores）；当前不可机判（带 load1 的主机缺 cpuCores 属性）⇒ 两档保持 null', thrContractRef:'shared/model/fault-modes.yaml#L244'},
  {thrCode:'cpu_usage_ratio_severe',     thrMetric:'host.cpu_usage_ratio',            thrWarning:0.2284, thrCritical:0.6852, thrRationale:'待标定：旧项目写 node.cpu_usage > 90 而实测 [13.6, 21.01]%，几乎永不命中',   thrContractRef:'shared/model/fault-modes.yaml#L249'},
  {thrCode:'mem_available_ratio_severe', thrMetric:'host.mem_available_ratio',        thrWarning:0.15, thrCritical:0.05, thrRationale:'待标定',                                                            thrContractRef:'shared/model/fault-modes.yaml#L254'},
  {thrCode:'mean_exec_ms_light',         thrMetric:'db.query.mean_exec_ms',           thrWarning:1000, thrCritical:10000, thrRationale:'待标定：既有项目档位 mean_exec_time_ms_light = (100, 1000)ms，但实测慢 SQL 平均 30.8s，档位需重设', thrContractRef:'shared/model/fault-modes.yaml#L259'},
  {thrCode:'dead_tuple_ratio_light',     thrMetric:'db.table.dead_tuple_ratio',       thrWarning:0.1, thrCritical:0.3, thrRationale:'待标定：指标 db.table.dead_tuple_ratio 已在 metrics.yaml（range [0,1]）与 docs/01 §2.4 Table 行声明，但本表原无对应阈值个体；docs/03 §6 R-F2 与 docs/04 §6 r02 均引用 `dead_tuple_ratio_light` —— 属**悬空引用**，由 kg_tbox_check.py（C08）与 scheme1-kg/tests/test_integrity.py（T9）在 M2 期发现，v1.0.2 补齐。两档留 null = WARN「待标定，当前不可机判」，**不得编造数值**（threshold_policy.hardcode_forbidden）', thrContractRef:'shared/model/fault-modes.yaml#L264'},
  {thrCode:'xact_age_s_light',           thrMetric:'db.session.xact_age_s',           thrWarning:300,  thrCritical:900,  thrRationale:'待标定',                                                            thrContractRef:'shared/model/fault-modes.yaml#L269'},
  {thrCode:'qps_high',                   thrMetric:'app.endpoint.qps',                thrWarning:null, thrCritical:null, thrRationale:'待标定：需应用侧真实 QPS 值域。用于 causal-edges.yaml 的 E6 条件边（高并发触发正反馈环）。★ 该条在 v1.0.0 时被 E6 引用但本表未定义——属悬空引用，由 contract_gates.py（G3）发现，v1.0.1 补齐', thrContractRef:'shared/model/fault-modes.yaml#L274'}
] AS t
MERGE (n:CC_Threshold {thrCode: t.thrCode})
  SET n += t;


// ────────────────────────────────────────────────────────────────────────────
// §5 判据关系（`CC_JUDGED_BY`，**32 条**）—— docs/03 §4.2（`:187-202`）
//    形状逐字照 §4.2 示例：`MATCH (fm:CC_FaultMode {...}), (m:CC_Metric {...}) MERGE (fm)-[:CC_JUDGED_BY {...}]->(m)`
//    边属性四元组 = `{op, operandKind, operandValue|operandRef, role}`
//      · operandKind ∈ literal（符号判定，如 non_zero → literal '0'，**§4.2 示例即如此写**）
//                      / entity_property（绑到实体自带配置，operandRef='<类型>.<属性>'）
//                      / threshold（operandRef='threshold:<阈值名>#<warning|critical>'，档位必填）
//                      / predicate（写法 (b) 的符号/状态谓词；无操作数，四元组退化为三元组）
//                      / enum（写法 (c) 的取值枚举；operandValues 为列表）
//      · role ∈ necessary / sufficient / exclusion / distinguishing（§4.2 `:199` 枚举）
//    子句→role 的映射（**机械可核**）：`all_of`→necessary；`any_of`→sufficient；
//      `distinguishing_evidence`→distinguishing；`leaf_root_causes.exclusion_requirements`→exclusion。
//    取值**逐字**来自 fault-modes.yaml#fault_modes[].detect（行号见 fmContractRef）。
//    ★ 谓词型判据没有操作数（operandKind='predicate' 时不写 operandValue）—— docs/03 未规定
//      该退化情形，登记 GAP-DOC-13。
// ────────────────────────────────────────────────────────────────────────────

// --- FM-APP-POOL-EXHAUSTED（all_of ×3 = necessary；distinguishing_evidence ×1）---
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}), (m:CC_Metric {metricCode:'app.datasource.pending_threads'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'literal', operandValue:'0', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}), (m:CC_Metric {metricCode:'app.datasource.active_connections'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>=', operandKind:'entity_property', operandRef:'JdbcDataSource.maxPoolSize', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}), (m:CC_Metric {metricCode:'app.datasource.acquire_wait_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:acquire_wait_ms_light#warning', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}), (m:CC_Metric {metricCode:'db.pool.session_used_ratio'})
MERGE (fm)-[:CC_JUDGED_BY {op:'<', operandKind:'threshold', operandRef:'threshold:session_used_ratio_light#warning', role:'distinguishing'}]->(m);

// --- FM-APP-DS-CONNECT-FAIL（all_of ×1 = necessary；排除法 ×2 = exclusion）---
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'}), (m:CC_Metric {metricCode:'app.datasource.connect_fail_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'}), (m:CC_Metric {metricCode:'db.instance.up'})
MERGE (fm)-[:CC_JUDGED_BY {op:'is_true', operandKind:'predicate', role:'exclusion'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'}), (m:CC_Metric {metricCode:'app.datasource.connect_latency_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'<', operandKind:'entity_property', operandRef:'JdbcDataSource.connectionTimeoutMs', role:'exclusion'}]->(m);

// --- FM-APP-ENDPOINT-SLOW ---
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'}), (m:CC_Metric {metricCode:'app.endpoint.p99_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'entity_property', operandRef:'AppEndpoint.sloP99Ms', role:'necessary'}]->(m);

// --- FM-APP-INSTANCE-RESTART（any_of ×1 = sufficient；all_of ×1 = necessary）---
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'}), (m:CC_Metric {metricCode:'app.uptime_s'})
MERGE (fm)-[:CC_JUDGED_BY {op:'decreased', operandKind:'predicate', role:'sufficient'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'}), (m:CC_Metric {metricCode:'container.restart_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-APP-HEAP-PRESSURE ---
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'}), (m:CC_Metric {metricCode:'app.heap_used_ratio'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:heap_used_ratio_light#warning', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'}), (m:CC_Metric {metricCode:'app.gc_pause_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:gc_pause_ms_light#warning', role:'necessary'}]->(m);

// --- FM-CTR-OOM-KILLED ---
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'}), (m:CC_Metric {metricCode:'container.oom_kill_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-CTR-CPU-THROTTLED ---
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-CPU-THROTTLED'}), (m:CC_Metric {metricCode:'container.cpu_throttled_periods'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-CTR-NET-UNREACHABLE（any_of ×1 = sufficient；all_of ×1 = necessary）---
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'}), (m:CC_Metric {metricCode:'app.datasource.connect_latency_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'entity_property', operandRef:'JdbcDataSource.connectionTimeoutMs', role:'sufficient'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'}), (m:CC_Metric {metricCode:'app.datasource.connect_fail_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-HOST-CPU-CONTENTION ---
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-CPU-CONTENTION'}), (m:CC_Metric {metricCode:'host.load1'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:load1_per_core_light#warning', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-CPU-CONTENTION'}), (m:CC_Metric {metricCode:'host.cpu_usage_ratio'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:cpu_usage_ratio_severe#critical', role:'necessary'}]->(m);

// --- FM-HOST-MEM-PRESSURE ---
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-MEM-PRESSURE'}), (m:CC_Metric {metricCode:'host.mem_available_ratio'})
MERGE (fm)-[:CC_JUDGED_BY {op:'<', operandKind:'threshold', operandRef:'threshold:mem_available_ratio_severe#critical', role:'necessary'}]->(m);

// --- FM-DB-SLOW-QUERY ---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'}), (m:CC_Metric {metricCode:'db.query.mean_exec_ms'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:mean_exec_ms_light#warning', role:'necessary'}]->(m);

// --- FM-DB-SESSION-POOL-FULL ---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'}), (m:CC_Metric {metricCode:'db.pool.session_used_ratio'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:session_used_ratio_severe#critical', role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'}), (m:CC_Metric {metricCode:'db.pool.conn_rejected_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-LOCK-WAIT ---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'}), (m:CC_Metric {metricCode:'db.session.wait_event'})
MERGE (fm)-[:CC_JUDGED_BY {op:'in_enum', operandKind:'enum', operandValues:['LockMgrLock','transactionid','relation','tuple'], role:'necessary'}]->(m);

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'}), (m:CC_Metric {metricCode:'db.session.xact_age_s'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:xact_age_s_light#warning', role:'necessary'}]->(m);

// --- FM-DB-INSTANCE-DOWN（any_of ×2 = sufficient）---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'}), (m:CC_Metric {metricCode:'db.instance.up'})
MERGE (fm)-[:CC_JUDGED_BY {op:'is_false', operandKind:'predicate', role:'sufficient'}]->(m);






MATCH (fm:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'}), (m:CC_Metric {metricCode:'db.cluster.switchover_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'sufficient'}]->(m);

// --- FM-HOST-DISK-FULL ---
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-FULL'}), (m:CC_Metric {metricCode:'host.fs_max_used_pct'})
MERGE (fm)-[:CC_JUDGED_BY {op:'>', operandKind:'threshold', operandRef:'threshold:fs_usage_pct_light#warning', role:'necessary'}]->(m);

// --- FM-HOST-DISK-IO-ERROR ---
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-IO-ERROR'}), (m:CC_Metric {metricCode:'host.disk_io_errors_total'})
MERGE (fm)-[:CC_JUDGED_BY {op:'non_zero', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-HOST-NET-IFACE-DOWN ---
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-NET-IFACE-DOWN'}), (m:CC_Metric {metricCode:'host.net_iface_down_count'})
MERGE (fm)-[:CC_JUDGED_BY {op:'non_zero', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-REPLICATION-LAG ---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-REPLICATION-LAG'}), (m:CC_Metric {metricCode:'db.cluster.replication_not_streaming_count'})
MERGE (fm)-[:CC_JUDGED_BY {op:'non_zero', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-UNUSED-INDEX ---
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-UNUSED-INDEX'}), (m:CC_Metric {metricCode:'db.table.unused_index_count'})
MERGE (fm)-[:CC_JUDGED_BY {op:'non_zero', operandKind:'predicate', role:'necessary'}]->(m);


// --- FM-DB-DEADLOCK ---

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-DEADLOCK'}), (m:CC_Metric {metricCode:'db.cluster.deadlocks'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-IDLE-IN-TX ---

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-IDLE-IN-TX'}), (m:CC_Metric {metricCode:'db.instance.idle_in_xact'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-SEQ-SCAN ---

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SEQ-SCAN'}), (m:CC_Metric {metricCode:'db.table.seq_scan'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);

// --- FM-DB-ROLLBACK-STORM ---

MATCH (fm:CC_FaultMode {fmCode:'FM-DB-ROLLBACK-STORM'}), (m:CC_Metric {metricCode:'db.cluster.xact_rollback'})
MERGE (fm)-[:CC_JUDGED_BY {op:'increased', operandKind:'predicate', role:'necessary'}]->(m);


// ────────────────────────────────────────────────────────────────────────────
// §6 影响关系（`CC_AFFECTS`：**14 条 MERGE 语句**，目标节点只给标签、**无识别键** ⇒ 在真机 ABox 基数上确定性笛卡尔展开为 **42 条边**；逐 FM 边数/目标类型节点数 = APP-POOL-EXHAUSTED 1、APP-DS-CONNECT-FAIL 1、APP-ENDPOINT-SLOW 2、APP-INSTANCE-RESTART 1、APP-HEAP-PRESSURE 1、CTR-OOM-KILLED 1、CTR-CPU-THROTTLED 1、CTR-NET-UNREACHABLE 1、HOST-CPU-CONTENTION 3、HOST-MEM-PRESSURE 3、DB-SLOW-QUERY 4、DB-SESSION-POOL-FULL 2、DB-LOCK-WAIT **19**（`CC_Session` 19 实例）、DB-INSTANCE-DOWN 2，合计 **42**，**不是「每个 FaultMode 恰好 1 条出边」**；★ 另注：`p1_neo4j.py --wipe` 对 ABox 标签 `DETACH DELETE` 会切断这 42 条边（`CC_AFFECTS` 属 TBox、p1 不重投）⇒ 每次 ABox-only wipe 后必须重跑本文件才恢复）—— docs/03 §4.3
//    形状逐字照 §4.3 示例：`MATCH (fm:CC_FaultMode {...}), (q:CC_Query) MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(q)`
//    门禁断言（§4.3 `:211`）：每个 `FaultMode` ≥1 条 AFFECTS 出边，且**目标类型 = fmComponentType**。
//    ★ 实现方式：按 `fmComponentType` **按类型批量挂**（不写任何实例值 —— 实例由 P1 投影；
//      一条语句覆盖该类型的全部现存实例，故实例数变化不需要改本文件）。
//      `severity` 取该 FM 的 `fmSeverity`（severity_enum 内，逐字来自 fault-modes.yaml）。
// ────────────────────────────────────────────────────────────────────────────
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-POOL-EXHAUSTED'}),   (t:CC_JdbcDataSource)       MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-DS-CONNECT-FAIL'}),  (t:CC_JdbcDataSource)       MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-ENDPOINT-SLOW'}),    (t:CC_AppEndpoint)          MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-INSTANCE-RESTART'}), (t:CC_AppInstance)          MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-APP-HEAP-PRESSURE'}),    (t:CC_AppInstance)          MERGE (fm)-[:CC_AFFECTS {severity:'warning'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-OOM-KILLED'}),       (t:CC_Container)            MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-CPU-THROTTLED'}),    (t:CC_Container)            MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-CTR-NET-UNREACHABLE'}),  (t:CC_Container)            MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-CPU-CONTENTION'}),  (t:CC_Host)                 MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-MEM-PRESSURE'}),    (t:CC_Host)                 MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SLOW-QUERY'}),        (t:CC_Query)                MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SESSION-POOL-FULL'}), (t:CC_DatabaseSessionPool)  MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-LOCK-WAIT'}),         (t:CC_Session)              MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-INSTANCE-DOWN'}),     (t:CC_GaussDBInstance)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-FULL'}),     (t:CC_Host)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-DISK-IO-ERROR'}),     (t:CC_Host)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-HOST-NET-IFACE-DOWN'}),     (t:CC_Host)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-REPLICATION-LAG'}),     (t:CC_GaussDBCluster)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-UNUSED-INDEX'}),     (t:CC_Table)      MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-DEADLOCK'}),     (t:CC_GaussDBCluster)      MERGE (fm)-[:CC_AFFECTS {severity:'critical'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-IDLE-IN-TX'}),     (t:CC_GaussDBInstance)      MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-SEQ-SCAN'}),     (t:CC_Table)      MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);
MATCH (fm:CC_FaultMode {fmCode:'FM-DB-ROLLBACK-STORM'}),     (t:CC_GaussDBCluster)      MERGE (fm)-[:CC_AFFECTS {severity:'major'}]->(t);


// ────────────────────────────────────────────────────────────────────────────
// ★ 契约缺口台账（GAP）—— 格式固定，供 shared/verify/kg_tbox_check.py 机读核对。
//   语义：该边**确实没有**这些属性，且契约/文档中不存在其取值；校验器据此确认
//         「缺的属性被显式登记」且「登记了的属性确实没写」，防止用台账掩盖漏写。
//   ★ 边属性缺口的**统一说明**（原 16 行逐边 `// GAP: E* probability,delaySec,chainPosition`
//     已合并为这一条）：`weight` / `learnCount` / `probability` / `delaySec` / `chainPosition`
//     这 5 个字段在 `shared/model/causal-edges.yaml`（唯一权威）里**根本不存在**；其取值只出现在
//     docs/03 §5.1/§5.3 的**两个文档示例**中，且只覆盖 E1/E7 两条边。给 2/18 条边赋「概率/权重」
//     会污染排序口径、方案2 侧也无法对称 ⇒ captain 裁定撤回：**本图不落地这 5 个字段**。
//     它们不是"某条边漏写"，而是**契约没有的字段**，故不再逐边登记为 GAP（登了会误示"应写而未写"）。
// ────────────────────────────────────────────────────────────────────────────
// ★ 文档级缺口（GAP-DOC，非本文件可控）
//   GAP-DOC-01 §4.1 自称"节点属性（严格 12 键）"，正文只列 11 键
//               （fmCode/fmName/fmLayer/fmComponentType/fmSeverity/fmReversible/fmChain/
//                fmDescription/fmDetectionMode/fmContractRef/fmCreatedAt）⇒ 本文件按 11 键写。
//   GAP-DOC-02 §4.1 有 `fmDescription` 键，但 fault-modes.yaml **无** description 字段；
//               本文件取该条 `note` 原文，无 note 的 7 条（POOL-EXHAUSTED / INSTANCE-RESTART /
//               HEAP-PRESSURE / HOST-CPU-CONTENTION / HOST-MEM-PRESSURE / DB-LOCK-WAIT /
//               DB-INSTANCE-DOWN）**不写**该属性，未自造描述。
//   GAP-DOC-03 ★ 已关闭（原缺口 → 契约优先口径）：docs/03 §5.2 自称"边属性契约"，表格列出
//               probability/weight/delaySec/learnCount/chainPosition/description/provenance/
//               contractId 八项；但**唯一权威** `shared/model/causal-edges.yaml:39-80` 每条边只有
//               id/from/relation/to/chain/evidence/provenance（E6/E7/E9/E12 另有 condition）
//               ⇒ 本文件按契约写入 **contractId / chain / description / provenance**（+ 条件边 cond*），
//               上述 5 个非契约字段一律不写（captain 已把 docs/03 §5.1/§5.3 改为契约优先口径）。
//   GAP-DOC-04 ★ 已关闭：随 weight 一并撤除（原「§5.2 夹紧 [0.1,0.9] vs §5.1/§5.3 示例 1.00」
//               的冲突因不再落地 weight 而消失）。
//   GAP-DOC-05 §5.3 示例把条件写成 `condValue: 0.9`（数字）；契约里 E7 的 operand 是
//               `threshold:cpu_usage_ratio_severe#critical` ⇒ 本文件改写成
//               `condKind:'threshold', condRef:'cpu_usage_ratio_severe#critical'`，不写数字。
//   GAP-DOC-06 ★ 已关闭（原缺口 → 按补充规格实现）：`docs/07` 没有任何 M 行覆盖 §4.2 `CC_JUDGED_BY`、
//               §4.3 `CC_AFFECTS`、`CC_Metric`（58）与 `CC_Threshold`（12；v1.0.1 时 11，v1.0.2 补齐
//               `dead_tuple_ratio_light`）；补充规格指示「`docs/09:180`
//               要求 03-fault-modes.cypher 必须有 JUDGED_BY 与 fmDetectionMode」后，本节按 docs/03
//               §4.2/§4.3 实现（§3–§6）。**仍未做**的是 `CC_Rule`/`CC_Finding`/`CC_Action` 节点：
//               docs/03 只给了它们的唯一约束（§2.2），**没有**任何一节规定其属性集 ⇒ 自创属性
//               会污染 M2.8/T10 等价性，故本文件不建（只保留约束）。
// ============================================================================
