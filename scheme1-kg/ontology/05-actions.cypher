// ============================================================================
// scheme1-kg/ontology/05-actions.cypher
//   建议动作目录的 LPG 投影（方案1 · 知识图谱）
// ----------------------------------------------------------------------------
// 出处：**shared/model/actions.yaml**（共享契约，2026-10-15 新建）
//
// 为什么补这个文件（实测 2026-10-15）：
//   两侧 `cc_propose` 的输出严重不对称 —— 方案2 返回 7 条建议，方案1 返回 **0 条**，
//   自述「环境动作目录为空：快照里没有带 actionId 的动作对象」。
//   根因不是「投影漏了」，而是**动作目录此前从来没有契约**：
//     `scheme2-onto/ontology/方案2 侧的动作模块文件` 自己的头部就写着「docs/04 对 cc-action
//     **没有给出任何契约**…本模块是方案2 侧**自定的最小封闭集**…动作名与代码由我命名」。
//   ⇒ 处置 = 把该目录**升格为共享契约**（shared/model/actions.yaml）并让**两侧同源投影**；
//     而不是把一侧自造的数据抄给另一侧（那是「造数据让两侧看起来一致」）。
//
// ★ 两处「代码与契约不一致」在此**显式登记**（不藏起来）：
//   ① `cc_propose`（scheme1-kg/service/cc_tools.py::build_propose）查的是
//      `n.actionId IS NOT NULL`，而 `01-schema.cypher:75` 的唯一约束建在 **`actionCode`**、
//      契约 actions.yaml 的键也是 **`actionCode`** ⇒ 二者不一致。
//      本文件**同时写 `actionCode`（权威）与 `actionId`（兼容别名）**，
//      使既有代码可用；**别名仅为过渡，正解是改 build_propose 读 actionCode**（已登记）。
//   ② 同一函数还查 `MATCH (rb:Runbook)-[:RECOMMENDS]->(a)`，而 **`Runbook` 标签
//      在 01-schema.cypher 里根本不存在**、契约也没有它。
//      本文件按该形状补最小 Runbook 壳（rootCauseType = 对应故障码），
//      使「故障→动作」可被既有代码命中；**正解是直接走 actions.yaml#onFault 的直连**（已登记）。
//
// ★ 只提议、不执行：不写 commandTemplate（避免把「建议」写成「操作规程」），
//   与本契约 meta.semantics 一致（executed=false / dryRun=true 由服务层保证）。
// ============================================================================

// ── 1) 唯一约束（幂等；与 01-schema.cypher:75 同源）─────────────────────────
CREATE CONSTRAINT cc_uq_action_code IF NOT EXISTS
FOR (n:CC_Action) REQUIRE n.actionCode IS UNIQUE;

// ── 2) 七个动作（逐条对齐 shared/model/actions.yaml）────────────────────────
MERGE (a:CC_Action {actionCode: 'ACT-EXPAND-APP-POOL'})
  SET a.actionId = 'ACT-EXPAND-APP-POOL',            // ★ 兼容别名（见头部 ①；正解是改代码）
      a.actionName = '扩大应用侧连接池上限',
      a.actionRiskLevel = 'medium',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 app.datasource.active_connections 已达 JdbcDataSource.maxPoolSize；扩上限前须先确认数据库侧未贴顶。',
      a.expectedEffect = 'acquire_wait_ms 回落、pending_threads 归零。',
      a.preconditionNote = 'connectsTo 的下游 DatabaseSessionPool 未达 session_used_ratio_severe#critical；否则扩池只是把压力推给数据库。',
      a.sideEffectNote = '数据库侧连接数同步上升，可能触发 FM-DB-SESSION-POOL-FULL。',
      a.contractSource = 'shared/model/actions.yaml#ACT-EXPAND-APP-POOL',
      a.onFaultCode = 'FM-APP-POOL-EXHAUSTED';

MERGE (a:CC_Action {actionCode: 'ACT-OPTIMIZE-SLOW-QUERY'})
  SET a.actionId = 'ACT-OPTIMIZE-SLOW-QUERY',
      a.actionName = '优化慢查询（改写/加索引/终止长事务）',
      a.actionRiskLevel = 'medium',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 db.query.mean_exec_ms 超过 mean_exec_ms_light#warning。',
      a.expectedEffect = 'db.query.mean_exec_ms 回落；会话堆积缓解。',
      a.preconditionNote = '需先用 Query/Table 链路定位具体 SQL（runsQuery + Table）。',
      a.sideEffectNote = '终止长事务可能中断未提交业务。',
      a.contractSource = 'shared/model/actions.yaml#ACT-OPTIMIZE-SLOW-QUERY',
      a.onFaultCode = 'FM-DB-SLOW-QUERY';

MERGE (a:CC_Action {actionCode: 'ACT-RAISE-CPU-LIMIT'})
  SET a.actionId = 'ACT-RAISE-CPU-LIMIT',
      a.actionName = '提高容器 CPU 配额（或降低同宿主机争抢）',
      a.actionRiskLevel = 'medium',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 container.cpu_throttled_periods 增长，且条件边（host.cpu_usage_ratio > cpu_usage_ratio_severe#critical）成立。',
      a.expectedEffect = '限流周期停止增长，app.endpoint.p99_ms 回落。',
      a.preconditionNote = '必须同时看宿主机（hosts/runsIn 链路）—— 只看容器会误判为应用自身慢。',
      a.sideEffectNote = '抬高配额会加剧同宿主机其他实例的争抢。',
      a.contractSource = 'shared/model/actions.yaml#ACT-RAISE-CPU-LIMIT',
      a.onFaultCode = 'FM-CTR-CPU-THROTTLED';

MERGE (a:CC_Action {actionCode: 'ACT-RAISE-MEM-LIMIT'})
  SET a.actionId = 'ACT-RAISE-MEM-LIMIT',
      a.actionName = '提高容器内存 limit（或排查堆泄漏）',
      a.actionRiskLevel = 'medium',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 container.oom_kill_total 增长；两条路径须先用区分器判别（宿主机内存紧张 vs 容器 limit 过小）。',
      a.expectedEffect = 'OOM 计数停止增长，实例重启随之消失。',
      a.preconditionNote = '路径甲（宿主机紧张）时提高单容器 limit 无效，须先在宿主机侧处置。',
      a.sideEffectNote = '加大 limit 可能诱发宿主机级 OOM。',
      a.contractSource = 'shared/model/actions.yaml#ACT-RAISE-MEM-LIMIT',
      a.onFaultCode = 'FM-CTR-OOM-KILLED';

MERGE (a:CC_Action {actionCode: 'ACT-FIX-JDBC-AUTH'})
  SET a.actionId = 'ACT-FIX-JDBC-AUTH',
      a.actionName = '修正 JDBC 连接串/驱动版本/认证方式',
      a.actionRiskLevel = 'high',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症连接失败：只有在「DB 在线 ∧ 网络可达 ∧ 立即失败」全部成立时才收敛到认证/配置类根因（区别于超时类）。',
      a.expectedEffect = 'app.datasource.connect_fail_total 停止增长。',
      a.preconditionNote = '★ 必须先排除「超时而非立即失败」与「切换事件」，否则会把网络问题当成认证问题。',
      a.sideEffectNote = '改认证方式需与应用发布同步，避免滚动重启期间大面积连接失败。',
      a.contractSource = 'shared/model/actions.yaml#ACT-FIX-JDBC-AUTH（★ 已更正其 onFault：原指向 FM-APP-AUTH-INCOMPATIBLE 不在本契约 23 个模式内）',
      a.onFaultCode = 'FM-APP-DS-CONNECT-FAIL';

MERGE (a:CC_Action {actionCode: 'ACT-CHECK-NETWORK-PATH'})
  SET a.actionId = 'ACT-CHECK-NETWORK-PATH',
      a.actionName = '排查容器到数据库的网络路径',
      a.actionRiskLevel = 'low',
      a.approvalRequirement = 'none',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 app.datasource.connect_latency_ms 达到 JdbcDataSource.connectionTimeoutMs（超时而非立即失败）。',
      a.expectedEffect = '定位丢包/ACL/路由问题，恢复连接建立。',
      a.preconditionNote = '与区分器配合：超时 → 网络；立即失败 → 认证。',
      a.sideEffectNote = '只读排查，无副作用。',
      a.contractSource = 'shared/model/actions.yaml#ACT-CHECK-NETWORK-PATH',
      a.onFaultCode = 'FM-CTR-NET-UNREACHABLE';

MERGE (a:CC_Action {actionCode: 'ACT-FAILOVER-DB'})
  SET a.actionId = 'ACT-FAILOVER-DB',
      a.actionName = '数据库实例主备切换/拉起',
      a.actionRiskLevel = 'high',
      a.approvalRequirement = 'operator',
      a.auditRequired = true,
      a.reversible = true,
      a.commandTemplate = null,
      a.rationale = '对症 db.instance.up 为 false 或 db.cluster.switchover_total 增长。',
      a.expectedEffect = '实例恢复在线，连接瞬时中断结束。',
      a.preconditionNote = '需确认备库状态与复制延迟；切换有中断窗口。',
      a.sideEffectNote = '切换瞬间会话全部断开，会叠加 FM-APP-DS-CONNECT-FAIL。',
      a.contractSource = 'shared/model/actions.yaml#ACT-FAILOVER-DB',
      a.onFaultCode = 'FM-DB-INSTANCE-DOWN';

// ── 3) 动作 → 故障模式 的直连边（权威口径 = actions.yaml#onFault）──────────
//    ★ 这是「故障→动作」的**正解载体**；下方 Runbook 仅是为迁就既有代码的兼容壳。
MATCH (a:CC_Action) WHERE a.onFaultCode IS NOT NULL
MATCH (f:CC_FaultMode {fmCode: a.onFaultCode})
MERGE (a)-[:CC_ADDRESSES]->(f);

// ── 4) 最小 Runbook 兼容壳（见头部 ②；正解是改 build_propose 走 CC_ADDRESSES）──
//    build_propose 查 MATCH (rb:Runbook)-[:RECOMMENDS]->(a)，而 Runbook 在
//    01-schema.cypher 与契约里都不存在 ⇒ 补壳使既有代码可命中，并在属性里登记原因。
MATCH (a:CC_Action) WHERE a.onFaultCode IS NOT NULL
MERGE (rb:Runbook {runbookId: 'RB-' + a.onFaultCode})
  SET rb.runbookTitle = '处置：' + a.actionName,
      rb.rootCauseType = a.onFaultCode,
      rb.provenanceNote = '兼容壳：仅为迁就 cc_propose 既有的 Runbook 查询；权威来源是 shared/model/actions.yaml#onFault 与 CC_ADDRESSES 边。'
MERGE (rb)-[:RECOMMENDS]->(a);
