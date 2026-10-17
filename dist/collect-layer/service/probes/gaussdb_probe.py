"""GaussDB 运维 Agent — GaussDB 层采集探针（v6.2 · GaussDB 507 方言适配版）

从 GaussDB 连接池(SQL)采集运行指标。

数据来源: GaussDB 数据库连接 (DSN)
采集方式: SQL 查询 (async_gaussdb)

──────────────────────────────────────────────────────────────────────────
v6.2 变更（D1 / D2 修复，2026-09-11）
──────────────────────────────────────────────────────────────────────────
D2 · GaussDB Kernel 507.0.0 方言适配 —— 全部 SQL 均已在生产库逐段实测：
  * 聚合 `FILTER (WHERE …)` → `count(CASE WHEN … THEN 1 END)`
      （GaussDB 507 不支持 SQL:2003 FILTER 子句，实测 syntax error at or near "FILTER"）
  * `pg_stat_activity` 无 `backend_type` / `wait_event` / `wait_event_type` 列
      → 去掉过滤条件；等待事件改读 `pg_thread_wait_status`（GaussDB 专有视图）
      → 锁等待判定用 `waiting` / `enqueue` 列
  * `pg_stat_replication` 无 `write_lag` / `flush_lag` / `replay_lag`
      → 改用位置差 `pg_xlog_location_diff(pg_current_xlog_location(), receiver_replay_location)`
      （GaussDB 507 无 PG10+ 的 `pg_current_wal_lsn()`，实测 does not exist）
  * `pg_stat_statements` 扩展未安装
      → 改用 `dbe_perf.statement_history`（自带 `query_plan` 列，无需另发 EXPLAIN）
  * 逐段异常不再静默吞掉（原 `except Exception: raw[name] = []` 会把语法错误
    伪装成"这段时间没有慢SQL"）→ 记录 `raw["<name>__error__"]` 并产出探针级告警
──────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import BaseProbe, ProbeResult, register_probe, canonical_section
# P4：IO 等待中继（条件边的判据面；累计口径，见模块文档为何不用活跃等待快照）
from .io_relay import io_relay_facts

# ★ 模拟演练打桩（docs/60）。live 下所有 tap 都是**零介入**的直通。
try:
    from .. import collect_sim as _sim
except Exception:                                    # 独立执行本文件时无包上下文
    try:
        import collect_sim as _sim
    except Exception:
        _sim = None

# ── SQL 采集字典 ──
# 键名即「采集段名」，是 guide(collect_sections) ↔ gather(sections) 的契约键。
# 新增/改名必须同步 tests/test_contracts.py 的契约断言。
_SQL: Dict[str, str] = {
    # ── 会话 ──
    "sessions": (
        # 原写法用 count(*) FILTER，GaussDB 507 语法不支持 —— 实测已改为 CASE WHEN
        # ★ M2 修复（2026-10-13）：`count(CASE WHEN enqueue IS NOT NULL ...)` 在 GaussDB 上**恒为 0**。
        #   实测证据：注入 biz_hot_row 后 `pg_stat_activity` 有 **7 个 waiting=True** 的会话、
        #   `pg_locks WHERE NOT granted` = **6**（locktype=tuple×6 / transactionid×1），
        #   而同一时刻 `enqueue IS NOT NULL` 计数 = **0** ⇒ GaussDB 不填充 enqueue。
        #   正确口径取自被测系统自身文档（cardsim docs/SYSTEM-SUMMARY.md §5）：
        #   `SELECT count(*) FROM pg_locks WHERE NOT granted`。
        "SELECT count(*) AS total, "
        "count(CASE WHEN state = 'active' THEN 1 END) AS active, "
        "count(CASE WHEN waiting THEN 1 END) AS waiting, "
        "count(CASE WHEN state = 'idle in transaction' THEN 1 END) AS idle_in_xact, "
        "(SELECT count(*) FROM pg_locks WHERE NOT granted) AS lock_wait "
        "FROM pg_stat_activity"
    ),
    "connections": (
        "SELECT count(*) AS total, "
        "current_setting('max_connections')::int AS max_conn, "
        "round(count(*)::numeric / "
        "      GREATEST(current_setting('max_connections')::numeric, 1) * 100, 2) AS used_pct "
        "FROM pg_stat_activity"
    ),
    "connections_by_state": (
        "SELECT state, count(*) AS cnt FROM pg_stat_activity "
        "GROUP BY state ORDER BY cnt DESC"
    ),
    "session_detail": (
        # 原写法含 wait_event 与 backend_type 过滤，两者在 GaussDB 507 均不存在
        # ★ M3 修复（2026-10-13）：新增 lock_wait_type —— 逐会话的**真实**等待锁类型，
        #   取自 pg_locks NOT granted 按 pid 关联（实测取值 tuple / transactionid，
        #   两者都在契约 `FM-DB-LOCK-WAIT.detect.in_enum` 的值域内）。
        #   原实现用 `sd.get("enqueue") or "lock_wait"`，而 enqueue 恒 NULL
        #   ⇒ 每条会话都发同一个字面量 "lock_wait"，永远匹配不上契约 enum。
        # ★ M1 修复（2026-10-13）：新增 xact_age_ms —— 事务年龄**在 SQL 里算**。
        #   原实现把 xact_start 渲染成**不带时区的字符串**（按 DB 会话时区，实测 Etc/GMT-8），
        #   再拿探针宿主机的 datetime.now()（UTC）去减 ⇒ 恒为 **-8 h**（实测 -28797184 ms）。
        #   两个时间都取自 DB（now() 与 xact_start）即天然无时区问题。
        "SELECT pid, sessionid, application_name, state, waiting, enqueue, query, "
        "to_char(query_start, 'YYYY-MM-DD HH24:MI:SS') AS query_start, "
        "to_char(xact_start, 'YYYY-MM-DD HH24:MI:SS') AS xact_start, "
        "CASE WHEN xact_start IS NULL THEN NULL "
        "     ELSE round(EXTRACT(EPOCH FROM (now() - xact_start))::numeric * 1000) END AS xact_age_ms, "
        "(SELECT l.locktype FROM pg_locks l "
        " WHERE l.pid = pg_stat_activity.pid AND NOT l.granted LIMIT 1) AS lock_wait_type, "
        "to_char(backend_start, 'YYYY-MM-DD HH24:MI:SS') AS backend_start, "
        "client_addr, usesysid, unique_sql_id, control_status "
        "FROM pg_stat_activity "
        "ORDER BY query_start DESC NULLS LAST LIMIT 100"
    ),
    # ── 锁 ──
    "locks": (
        "SELECT l.locktype, l.mode, l.granted, l.relation::regclass AS relname, "
        "l.pid, a.query AS blocking_query "
        "FROM pg_locks l LEFT JOIN pg_stat_activity a ON l.pid = a.pid "
        "WHERE NOT l.granted ORDER BY l.locktype"
    ),
    "blocking_sessions": (
        "SELECT a1.pid AS blocked_pid, a1.query AS blocked_query, "
        "a2.pid AS blocking_pid, a2.query AS blocking_query "
        "FROM pg_stat_activity a1 "
        "JOIN pg_locks l1 ON a1.pid = l1.pid AND NOT l1.granted "
        "JOIN pg_locks l2 ON l1.relation = l2.relation AND l2.granted "
        "JOIN pg_stat_activity a2 ON l2.pid = a2.pid"
    ),
    "long_tx": (
        # 原写法含 backend_type 过滤（该列不存在）
        "SELECT count(*) AS cnt FROM pg_stat_activity "
        "WHERE xact_start IS NOT NULL AND now() - xact_start > interval '5 minutes'"
    ),
    # ── 等待事件 ──
    # 原 wait_events 读 pg_stat_activity.wait_event/wait_event_type（两列均不存在）
    # → 改读 GaussDB 专有视图 pg_thread_wait_status
    "wait_events": (
        "SELECT wait_status, wait_event, count(*) AS cnt "
        "FROM pg_thread_wait_status "
        "WHERE wait_status IS NOT NULL AND wait_status <> 'none' "
        "AND wait_status <> 'wait cmd' "
        "GROUP BY wait_status, wait_event ORDER BY cnt DESC LIMIT 20"
    ),
    # ── IO 等待中继（P4，2026-09-24）──
    # ★ 为什么另起两条而不是复用 `wait_events`：上面那条读的是 `pg_thread_wait_status`
    #   （**当前等待快照**），实测 IO 类只有个位数条 —— 它测的是「此刻在等什么」。
    #   本组读 `dbe_perf.wait_events`（**累计口径**），实测 ShareStorageWalWrite 累计
    #   1.34e10 μs、峰值 14.2ms ⇒ 两者给出相反印象。慢盘是「曾经/总体慢」，必须走累计口径。
    "io_wait_relay": (
        "SELECT event, total_wait_time, avg_wait_time, max_wait_time, min_wait_time, type "
        "FROM dbe_perf.wait_events WHERE event IN ("
        "'ShareStorageWalWrite','WALFileSync','WALWrite','ShareStorageCtlInfoWrite',"
        "'CkptWaitPageWriterSync','CkptWaitPageWriterFlush','DataFileRead','DataFileWrite',"
        "'ShareStorageCtlInfoRead','DoubleWriteFileWrite')"
    ),
    # 盘级平均 IO 时间（主机侧）：阵列侧取不到盘级时延（实测 performance/diskiostat 均不可得），
    # 这是「盘慢不慢」的**可得替代**读数。
    "io_file_stats": (
        "SELECT filenum, phyrds, phywrts, avgiotim, maxiowtm, lstiotim "
        "FROM dbe_perf.summary_file_iostat"
    ),
    # ── 慢 SQL ──
    # 原写法读 pg_stat_statements（扩展未安装）→ 改读 dbe_perf.statement_history
    # 该视图自带 query_plan 列，可直接做执行计划签名分析
    "slow_sql": (
        "SELECT unique_query_id, query, query_plan, start_time, finish_time, "
        "db_time, cpu_time, execution_time, data_io_time, n_returned_rows "
        "FROM dbe_perf.statement_history "
        "ORDER BY db_time DESC NULLS LAST LIMIT 20"
    ),
    # ── 按需 EXPLAIN 的输入（方向2）：只取 query 文本，计划由 plan_now 现取 ──
    #   为什么另开一段而不是复用 slow_sql_detail：那一段带 query_plan（大文本），
    #   而本段只需 query；分开可让「只要按需计划」的调用方不付 query_plan 的搬运成本。
    # ★ 必须 DISTINCT：实测 statement_history 对同一条 SQL 会有**多行**
    #   （按执行批次留痕），不去重会拿 5 行同一条语句、白白多发 4 次 EXPLAIN。
    "slow_sql_for_plan": (
        "SELECT DISTINCT unique_query_id, query FROM dbe_perf.statement_history "
        "WHERE query IS NOT NULL AND length(query) > 20 "
        "ORDER BY unique_query_id DESC LIMIT 5"
    ),
    "slow_sql_detail": (
        "SELECT unique_query_id, query, query_plan, "
        "start_time, finish_time, db_time, cpu_time, execution_time, parse_time, "
        "plan_time, data_io_time, n_returned_rows, n_tuples_fetched, "
        "n_blocks_fetched, n_blocks_hit "
        "FROM dbe_perf.statement_history "
        "ORDER BY db_time DESC NULLS LAST LIMIT 20"
    ),
    # ── 复制 ──
    # 原写法读 write_lag/flush_lag/replay_lag（三列均不存在）→ 改用位置差
    "replication": (
        "SELECT application_name, client_addr, state, sync_state, "
        "receiver_replay_location, receiver_flush_location, "
        "pg_xlog_location_diff(pg_current_xlog_location(), receiver_replay_location) "
        "  AS replay_lag_bytes "
        "FROM pg_stat_replication"
    ),
    # ── 表统计 ──
    # ★ M2.11（裁决 14，2026-09-28）：**必须 SELECT schemaname**。
    #   本体契约 shared/model/topology.yaml:189 定 Table 的 key_attrs = [schemaName, tableName]，
    #   而真机同一库确有**跨 schema 同名表**（只读实测：`snapshot.snapshot` 与 `db4ai.snapshot`，
    #   全库 79 张用户表分布 snapshot 63 / root 9 / dbe_profiler 4 / dbe_pldeveloper 2 / db4ai 1）。
    #   此前只查 relname ⇒ 这一对同名表在冻结侧被静默并成同一个对象。
    "table_stats": (
        "SELECT schemaname, relname, n_live_tup, n_dead_tup, "
        "round((n_dead_tup::numeric / GREATEST(n_live_tup + n_dead_tup, 1)) * 100, 2) "
        "  AS dead_pct, "
        "to_char(last_analyze, 'YYYY-MM-DD HH24:MI:SS') AS last_analyze, "
        "to_char(last_autoanalyze, 'YYYY-MM-DD HH24:MI:SS') AS last_autoanalyze, "
        "pg_size_pretty(pg_total_relation_size(relid)) AS total_size, "
        # 2026-09-12：本体声明了 table.size_bytes 但采集侧从未产出（悬空承诺）→ 补此列
        "pg_total_relation_size(relid) AS total_bytes "
        "FROM pg_stat_user_tables "
        "ORDER BY n_dead_tup DESC LIMIT 30"
    ),
    # ── ASP（活动会话采样，1 小时事件分布）──
    "asp": (
        "SELECT event, count(*) AS cnt FROM dbe_perf.local_active_session "
        "WHERE sample_time > now() - interval '1 hour' "
        "GROUP BY event ORDER BY cnt DESC LIMIT 20"
    ),
    # ── 缓冲区/库级统计（规则 r_buffer_cache_low_hit / r_deadlock_detected 的数据源）──
    "buffer_stats": (
        "SELECT sum(blks_hit) AS blks_hit, sum(blks_read) AS blks_read, "
        "sum(deadlocks) AS deadlocks, sum(temp_files) AS temp_files, "
        "sum(temp_bytes) AS temp_bytes, sum(xact_commit) AS xact_commit, "
        "sum(xact_rollback) AS xact_rollback "
        "FROM pg_stat_database"
    ),
    # ── 索引扫描统计（规则 r_index_bloat 的数据源）──
    # ★ 2026-10-13 P0.7：补 `schemaname` 列 —— 原来只有 relname/indexrelname/idx_scan，
    #   导致 `_tbl_id()` 拿不到 schema ⇒ 无法把索引挂到正确的 Table 身份（Table 身份是 schema 限定的）
    #   ⇒ 逐表聚合 table.unused_index_count 发不出事实。
    "index_stats": (
        "SELECT schemaname, relname, indexrelname, idx_scan, idx_tup_read "
        "FROM pg_stat_user_indexes ORDER BY idx_scan DESC NULLS LAST LIMIT 50"
    ),
    # ── 索引定义（列/大小/唯一性）──
    # 2026-09-12 实测：pg_stat_user_indexes 只给 idx_scan（无列/定义/大小），
    # 而 pg_get_indexdef + pg_relation_size 在 GaussDB 507 上**可用**（81/81 行实测通过）。
    # 注意：generate_subscripts/LATERAL 取 attnum 在 507 上不可用
    #（实测 InvalidColumnReferenceError）→ 列清单由 indexdef 文本解析。
    # 本段为 **opt-in**：默认采集不含它，避免改变既有采集载荷与契约时序；
    # 需要索引列/大小时显式传 sections=["index_defs"]。
    "index_defs": (
        "SELECT s.relname AS relname, s.indexrelname AS indexrelname, "
        "pg_get_indexdef(s.indexrelid) AS indexdef, "
        "pg_relation_size(s.indexrelid) AS index_size_bytes, "
        "pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size, "
        "ix.indisunique AS is_unique, ix.indisprimary AS is_primary "
        "FROM pg_stat_user_indexes s "
        "JOIN pg_index ix ON ix.indexrelid = s.indexrelid "
        "ORDER BY pg_relation_size(s.indexrelid) DESC NULLS LAST LIMIT 100"
    ),
    # ── 真实运行时执行计划 ──
    # `dbe_perf.statement_history.query_plan` 是**实际执行过**的计划（含 Datanode 与
    # 真实 cost），比事后 EXPLAIN 重放更可信。原有 slow_sql_detail 虽 SELECT 了该列，
    # 但 main.py 归一化时只留 signature、把计划原文丢掉，故需单独取。
    "slow_sql_plan": (
        "SELECT unique_query_id, query_plan, length(query_plan) AS plan_len "
        "FROM dbe_perf.statement_history "
        "WHERE query_plan IS NOT NULL AND length(query_plan) > 10 "
        "ORDER BY db_time DESC NULLS LAST LIMIT 10"
    ),
    # ── 索引膨胀率（规则 r_index_bloat 的数据源）★ 2026-09-12 新增 ──
    # 实测依据（GaussDB 507 生产库）：
    #   · pgstattuple **不可用**：不在 pg_available_extensions；即便 SET
    #     enable_extension=on（该 GUC 是 user 级、无需重启）仍报
    #     "Failed to open the extension control file: No such file or directory"
    #     —— 控制文件未随发行包提供，属 OS 层部署，非数据库参数可开。
    #   · GaussDB 原生 `gs_stat_freespace` 直接给出 "approximate expansion rate"
    #     （膨胀率），且**支持采样**（sample_ratio），一次几毫秒即可拿到。
    #     实测：16.2GB 索引 sample_ratio=1 仅 0.09s；本段 5 个大索引合计 0.44s。
    # 故本段零安装依赖，纯只读查询。
    "index_bloat": (
        "SELECT s.relname AS relname, s.indexrelname AS indexrelname,"
        "       pg_relation_size(s.indexrelid) AS idx_size_bytes,"
        "       (SELECT substring(f.info from 'expansion rate: ([0-9]+[.][0-9]+)')"
        "        FROM gs_stat_freespace(s.indexrelid, 0::oid, 'i'::\"char\","
        "                               {sample_ratio}::bigint, false) f) AS bloat_ratio,"
        "       (SELECT substring(f.info from 'actual_freespace: ([0-9]+[.][0-9]+)')"
        "        FROM gs_stat_freespace(s.indexrelid, 0::oid, 'i'::\"char\","
        "                               {sample_ratio}::bigint, false) f) AS freespace_bytes,"
        "       (SELECT substring(f.info from 'dead tuple: ([0-9]+[.][0-9]+)')"
        "        FROM gs_stat_freespace(s.indexrelid, 0::oid, 'i'::\"char\","
        "                               {sample_ratio}::bigint, false) f) AS dead_tuple_per_page,"
        "       (SELECT substring(f.info from 'nblocks: ([0-9]+)')"
        "        FROM gs_stat_freespace(s.indexrelid, 0::oid, 'i'::\"char\","
        "                               {sample_ratio}::bigint, false) f) AS nblocks"
        " FROM pg_stat_user_indexes s"
        " WHERE pg_relation_size(s.indexrelid) > {min_bytes}"
        " ORDER BY idx_size_bytes DESC LIMIT {max_rows}"
    ),
    # ── 表级 seq_scan / idx_scan（规则 r_index_bloat 的数据源）──
    # ★ M2.11：加 schemaname —— 本段产出的 table.seq_scan / table.idx_scan 与 table_stats
    #   共用同一套 Table 身份（`tbl-<schema>.<relname>`），少一列就会让 id 退回裸表名。
    "table_scan_stats": (
        "SELECT schemaname, relname, seq_scan, seq_tup_read, idx_scan, n_live_tup "
        "FROM pg_stat_user_tables WHERE seq_scan IS NOT NULL "
        "ORDER BY seq_scan DESC NULLS LAST LIMIT 30"
    ),
    # ── 跨 DN 数据倾斜（规则 r_data_skew 的数据源）──
    # ★ M2.11：加 schemaname。dbe_perf.global_stat_user_tables **确有** schemaname 列
    #   （真机只读实测 22 列，第 3 列即 schemaname）；同名表的不同 schema 必须分开聚合，
    #   否则倾斜比会把两张不同的表算到一起。
    "table_skew": (
        "SELECT schemaname, relname, node_name, n_live_tup FROM dbe_perf.global_stat_user_tables "
        "WHERE n_live_tup IS NOT NULL ORDER BY relname LIMIT 500"
    ),
    # ── WLM 资源池**队列**（规则 r_wlm_queue_congested 的数据源）──
    # ★ 修复 KD-WLM-01（2026-09-23，用户裁定「改采集取真实排队数」）：
    #   原实现从 wlm_user_resource_runtime 比较 used_/total_ 得出一个「超配额标志数」(0..2)，
    #   却发成 wlm.waiting_count；而规则要求 > 50 ⇒ **值域结构性不可达**，规则永不命中
    #   （产出面判据查不出这类问题：路径**有**产出，是值域够不着阈值）。
    #   真实排队数在 pg_session_wlmstat：status='pending'（执行前状态）/ 'queued' 即排队等待，
    #   resource_pool 即「队列」身份。
    #   取证：① pg_get_viewdef('pg_session_wlmstat')：status←内核 current_status、
    #           resource_pool←会话资源池（会话 respool 为 unknown 时取 rolrespool）；
    #         ② openGauss 文档 PG_SESSION_WLMSTAT：status 含 pending（执行前状态）/ running；
    #         ③ 本实例实测该视图可查、列齐（status/enqueue/resource_pool 均存在）。
    #   ⚠ 本实例此刻无排队，只观测到 unknown/active ⇒ **排队态字符串集来自文档而非实测**，
    #     故另采 wlm_status_vocab（观测到的取值域）并对未知状态告警 ——
    #     否则现场写法一旦不同，waiting_count 会恒为 0 而无人发现（同类静默）。
    "wlm_stats": (
        "SELECT resource_pool, "
        "SUM(CASE WHEN status IN ('pending','queued') THEN 1 ELSE 0 END) AS waiting_count, "
        "COUNT(*) AS session_count "
        "FROM pg_session_wlmstat GROUP BY resource_pool"
    ),
    # 观测到的 status 取值域（可见性用：与词表对照，出现未知值即告警）
    "wlm_status_vocab": (
        "SELECT DISTINCT status FROM pg_session_wlmstat WHERE status IS NOT NULL"
    ),
    # 用户级资源运行态（**保留**：wlm.used_spill_space 的语义是「该用户发生过溢写」，
    # 被自撰决策树 dt_r_wlm_queue_congested__authored 的 S1 分支引用，语义不变）
    "wlm_user_stats": (
        "SELECT username, used_memory, total_memory, used_temp_space, total_temp_space, "
        "used_spill_space FROM dbe_perf.wlm_user_resource_runtime"
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # D2-7（2026-09-23）：把吸纳树里高频 no_equivalent 的域**接上采集**
    #
    # 为什么做：吸纳树的 231 个分支经逐条裁定后只有 26 个可机判（11%），瓶颈不是阈值而是
    # 「本项目没有该指标的事实路径」。本批把**库侧确实存在、且语义可对齐**的四个域接上。
    #
    # ★ 三条必须先说的语义纪律（都实测过）：
    #   ① **累计值 ≠ 单次值**：`pg_stat_bgwriter.checkpoint_sync_time` 是**累计**毫秒
    #      （实测 6025844），而源侧 `sync_time_sec > 10` 问的是**单次**检查点同步耗时
    #      ⇒ 直接映射会**恒真**。故本批**不发**该事实，登记里也写明理由（宁可不可判，不要恒真）。
    #   ② **缺列/缺视图**：本实例**没有** `pg_stat_wal`、`pg_stat_archiver`；
    #      `pg_stat_replication` **没有** write_lag/flush_lag/replay_lag 列（GaussDB 用
    #      sender_sent_location / receiver_*_location 表达）⇒ 那几个域只能如实保持不可判。
    #   ③ 有除法的都要**防零除**（无基线时给 0，不给 NaN/异常）。
    # ══════════════════════════════════════════════════════════════════════════
    "bgwriter_stats": (
        "SELECT checkpoints_req, checkpoints_timed, checkpoint_write_time, checkpoint_sync_time, "
        "buffers_checkpoint, buffers_clean, buffers_backend, maxwritten_clean FROM pg_stat_bgwriter"
    ),
    # HOT 更新比例（表级）：源侧 hot_pct = n_tup_hot_upd / n_tup_upd * 100（百分数）
    # ★ M2.11：加 schemaname（Table 身份 schema 限定，理由同 table_stats）
    "hot_update_stats": (
        "SELECT schemaname, relname, n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables WHERE n_tup_upd > 0"
    ),
    # 复制发送侧（本实例无 write/flush/replay_lag 列，用 LSN 差值表达积压）
    # ★ 函数名**实测**（本实例 GaussDB 507 保留旧名，无 pg_current_wal_lsn / pg_wal_lsn_diff）：
    #   `SELECT pg_current_wal_lsn()` → ERR "Function ... does not exist"
    #   `SELECT pg_current_xlog_location()` → OK '42/CC7F01E0'
    #   `pg_wal_lsn_diff(text,text)` → ERR；`pg_xlog_location_diff(...)` → OK
    "replication_detail": (
        "SELECT COUNT(*) AS sender_count, "
        "COALESCE(MAX(pg_xlog_location_diff(pg_current_xlog_location(), sender_sent_location)), 0) AS pending_bytes, "
        "COALESCE(MAX(pg_xlog_location_diff(pg_current_xlog_location(), receiver_flush_location)), 0) AS flush_pending_bytes "
        "FROM pg_stat_replication"
    ),
    # ★ 用 `SUM(CASE WHEN …)` 而非 `COUNT(*) FILTER (WHERE …)`：后者是 PG 方言，
    #   GaussDB 507 不支持（被 tests/test_contracts.py 的 C3b 门禁抓出 —— 本仓已有此护栏）。
    "replication_slots": (
        "SELECT COUNT(*) AS slot_count, "
        "SUM(CASE WHEN active THEN 1 ELSE 0 END) AS slot_active_count FROM pg_replication_slots"
    ),
    # ══════════════════════════════════════════════════════════════════════════
    # D2-8（2026-09-23 夜）：第二批接采集（WAL / 行宽 / 填充因子 / 主备角色 /
    # 预备事务 / 逻辑复制订阅 / 时间线）
    #   ★ 全部先在本实例实测存在性，不存在的域**不编造**（见裁定表里的具体理由）：
    #     不存在：pg_stat_wal、pg_stat_archiver、pg_buffercache、pg_subscription_rel、
    #             pg_stat_activity.backend_xmin
    # ══════════════════════════════════════════════════════════════════════════
    # WAL 目录（实测 2179 个文件 / 34 GB）：源侧 step8_wal_generation_rate 数的是
    #   「WAL 是否有生成」⇒ 用文件数表达，阈值 0 为自然边界。
    "wal_files": (
        "SELECT COUNT(*) AS wal_file_count, COALESCE(SUM(size), 0) AS wal_total_bytes "
        "FROM pg_ls_waldir()"
    ),
    # 行宽（表级）：源侧 bytes_per_row = 关系字节数 / 行数。
    #   ★ 口径说明：源侧分母用 pg_stat_user_tables.n_live_tup（活行数），本项目同口径；
    #     `reltuples` 是**估算值**（可能为 -1/0），故用 n_live_tup 且防零除。
    #   ★ M2.11：加 s.schemaname（Table 身份 schema 限定，理由同 table_stats）
    "table_row_width": (
        "SELECT s.schemaname, c.relname, pg_relation_size(c.oid) AS bytes, s.n_live_tup AS live "
        "FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid "
        "WHERE pg_relation_size(c.oid) > 0 AND s.n_live_tup > 0"
    ),
    # 填充因子（表级）：源侧 COALESCE(reloptions 里的 fillfactor, '100')。
    #   ★ M2.11：加 s.schemaname（Table 身份 schema 限定，理由同 table_stats）
    "table_fillfactor": (
        "SELECT s.schemaname, c.relname, COALESCE((SELECT option_value FROM pg_options_to_table(c.reloptions) "
        "WHERE option_name = 'fillfactor'), '100') AS fillfactor "
        "FROM pg_stat_user_tables s JOIN pg_class c ON c.oid = s.relid"
    ),
    # 主备角色与回放延迟（★ 只在备机上有意义，主库刻意**不发**该事实 ⇒ 表达「无数据」而非「为假」）
    "replication_role": (
        "SELECT pg_is_in_recovery() AS is_standby, "
        "CASE WHEN pg_is_in_recovery() THEN EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())::int "
        "ELSE NULL END AS replay_lag_sec"
    ),
    "prepared_xacts": "SELECT COUNT(*) AS prepared_count FROM pg_prepared_xacts",
    "prepared_stmts": "SELECT COUNT(*) AS prepared_count FROM pg_prepared_statements",
    # 逻辑复制（实测 pg_subscription_rel **不存在**，故只采订阅/发布面）
    "subscriptions": (
        "SELECT (SELECT COUNT(*) FROM pg_subscription) AS subscription_count, "
        "(SELECT COUNT(*) FROM pg_publication_tables) AS publication_table_count"
    ),
    # 时间线（实测 pg_control_checkpoint().timeline_id 可用；整函数取列在其他列上会解码失败）
    "timeline": "SELECT timeline_id FROM pg_control_checkpoint()",

    # 参数类（源侧 step1_memory_config / step2_sync_config 的观测对象）
    "param_stats": (
        "SELECT name, setting, unit FROM pg_settings WHERE name IN "
        "('shared_buffers', 'work_mem', 'maintenance_work_mem', 'checkpoint_timeout', "
        "'synchronous_standby_names', 'synchronous_commit', 'wal_keep_segments', "
        "'max_wal_senders', 'max_connections')"
    ),
}

# ── opt-in 段：仅在**显式**传 sections 时采集 ──
# 这些段的数据量/载荷显著大于常规段（index_defs 最多 100 行含 indexdef 全文；
# slow_sql_plan 每条计划约 2KB）。若纳入默认全量采集，会改变既有 gather 的载荷
# 与既有调用方的时序表现 —— 故默认**不采集**，需用时显式指定段名。
# （实测：默认四层 gather 已达约 9 万 tokens，不宜再叠加。）
# ★ 方向2（按需 EXPLAIN）也是 opt-in：它对每条慢 SQL**额外发一次 EXPLAIN**，
# ── 按需 EXPLAIN（方向2）：把「计划面」从历史留存改成现取 ────────────────────
async def _collect_plan_now(conn, rows) -> List[dict]:
    """对慢 SQL 逐条现取真实计划（**只读**），返回结构化结果

    ★ 为什么需要（实测）：`statement_history.query_plan` 只有 2 条有值
      （`track_stmt_stat_level=OFF,L0`，只有超慢阈值语句才带计划）⇒
      「绝大多数慢 SQL 看不到计划」。而计划是慢 SQL 归因的核心判据面。
      改用按需 EXPLAIN 后，实测 9/9 条都能取到计划（原先 2/9）。

    ★ 三条纪律：
      ① 只读：拒绝非 SELECT/WITH/VALUES 的语句（plan_now.is_readonly_stmt）；
      ② fail-closed：取不到计划 ⇒ **不产出任何计划事实**，只记原因，
         绝不编造 plan_type/cost；
      ③ 保真度必须随事实回传：含占位符的 SQL 用的是**合成样本值**，
         计划**形状**可靠但**行数/选择性是近似** ⇒ 事实值里显式带上保真度，
         避免被当成精确结果呈现。
    """
    import asyncio as _aio
    from . import plan_now as _pn
    out: List[dict] = []

    async def _one(r):
        qid = r.get("unique_query_id")
        q = str(r.get("query") or "")
        if not q.strip():
            return None
        res = await _pn.explain_now(conn, q)
        item = {"unique_query_id": qid, "ok": bool(res.ok),
                "fidelity": res.fidelity, "substituted": res.substituted,
                "retry_used": res.retry_used,
                "sql_used": res.sql[:2000], "reason": res.reason}
        if res.ok and res.features is not None:
            f = res.features
            item["plan"] = {"total_cost": f.total_cost, "plan_type": f.plan_type,
                            "node_types": f.node_types, "seq_scan_tables": f.seq_scan_tables,
                            "indexes_used": f.indexes_used, "max_rows": f.max_rows,
                            "has_big_sort": f.has_big_sort, "has_big_agg": f.has_big_agg,
                            "has_nested_loop": f.has_nested_loop}
            item["risks"] = res.risks
        return item

    for r in rows or []:
        try:
            it = await _one(r)
        except Exception as e:                                   # noqa: BLE001
            it = {"unique_query_id": (r or {}).get("unique_query_id"), "ok": False,
                  "reason": "%s: %s" % (type(e).__name__, str(e)[:200])}
        if it:
            out.append(it)
    return out



#   纳入默认采集会改变既有载荷与时序。用时显式请求该段。
_OPT_IN_SECTIONS = ("index_defs", "slow_sql_plan", "index_bloat", "slow_sql_for_plan")

# 含 {占位符} 的采集段的取值。**仅**对含 "{" 的段做替换，既有段不受影响。
# 膨胀率采样率：实测 16.2GB 索引 sample_ratio=1 用 0.09s、=100 用 7.69s，
# 而两者给出的膨胀率一致（1.20）—— 采样 1 已足够，故取 1 以最小化 I/O。
# 只统计 >100MB 的索引（小索引膨胀无运维价值），最多 200 行。
_SQL_FORMAT_VARS = {
    "sample_ratio": 1,
    "min_bytes": 100_000_000,
    "max_rows": 200,
}

# 供契约测试引用：本探针实际支持的全部段键
SUPPORTED_SECTIONS = tuple(sorted(_SQL.keys()))


def parse_index_columns(indexdef: str) -> List[str]:
    """从 indexdef 文本解析索引列清单。

    为什么解析文本而不是查 `pg_index.indkey`：GaussDB 507 不支持
    `generate_subscripts(...) WITH ORDINALITY` / LATERAL 引用外层表
    （实测 InvalidColumnReferenceError）→ 无法按 attnum 取列名。
    indexdef 形如 `CREATE UNIQUE INDEX x ON t USING ubtree (a, b) WITH (...) TABLESPACE ...`
    实测 81/81 解析成功。
    """
    if not indexdef:
        return []
    m = re.search(r"USING\s+\w+\s*\((.*?)\)\s*(?:WITH|TABLESPACE|$)",
                  str(indexdef), re.S | re.I)
    if not m:
        return []
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


# ── WLM 排队态词表（**必须与文档/内核的写法一致**）──
# pg_session_wlmstat.status 的排队态：pending=执行前状态（排队等待资源）、queued=显式排队。
# ⚠ 这两个字符串来自 openGauss 文档（本实例此刻无排队，实测不到它们）；故词表之外的状态
#   一旦出现就告警 —— 防「现场写法不同 ⇒ waiting_count 恒 0」这类与 KD-WLM-01 同族的静默。
WLM_QUEUE_STATUSES = ("pending", "queued")
WLM_KNOWN_STATUSES = WLM_QUEUE_STATUSES + ("unknown", "active", "running", "idle",
                                           "inactive", "finished", "aborted")


# ══════════════════════════════════════════════════════════════════════════
# 窗口基线（delta）—— 让 `predicate: increased / decreased` 可机判
# ══════════════════════════════════════════════════════════════════════════
# 背景（2026-10-12）：`fault-modes.yaml` 的 meta.predicate_semantics 规定
#   increased = observation.delta 存在且为正；decreased = delta 存在且为负，
#   且 baseline_required 含这两个判据。**没有 delta ⇒ 判据 unjudgeable**（不得当 false）。
#
# 本层的困难：GaussDB 侧给的是**累计值**（pg_stat_database.deadlocks 等），
# 单次采样只有「从建库到现在的总数」，判不出「本轮是否在增长」。
# 与 container 探针同样的解法：用**采集侧状态文件**保存上一次读数，下次做差。
#
# ★ 两类量分开对待（这是本实现最关键的一处判断）：
#   · 单调计数器（MONOTONIC）：只增不减。差值为负 ⇒ **计数器被重置**
#     （实例重启 / pg_stat_reset）⇒ 不产出 delta，也**不取绝对值**（语义不明）。
#   · 量规（GAUGE）：可增可减（如 dead tuples 会被 vacuum 清掉、会话数会回落）。
#     差值为负是**真实信息** ⇒ 照常产出（`decreased` 判据正需要它）。
#
# ★ 窗口长度与「宁缺勿造」与其余两层同一条纪律：
#   · deltaWindowS = **实际两次采样的间隔秒数**（整数 ≥1），不谎称 DELTA_WINDOW_S；
#   · 间隔超过 DELTA_WINDOW_S ⇒ 基线过期，**不产出** delta；
#   · 首次采集没有基线 ⇒ 不产出；
#   · DELTA_WINDOW_S <= 0 ⇒ 整个机制关闭（状态文件也不写）。
#   ★ DELTA_WINDOW_S 与 container/app 两层**同名同义**（P11 共享参数），
#     且绝不能注入空串（`os.environ.get` 空串会让 float('') 在导入期炸）。
#
# ★ 键 = `path|obj_id`：Table 级指标（seq_scan / n_dead_tup）必须**逐表**做差，
#   否则 19 张表的读数会互相污染。
_PATHS_MONOTONIC = frozenset({
    "cluster.deadlocks",
    "cluster.xact_rollback",
    "cluster.slow_sql_count",
    "sql.temp_files_count",
    "table.seq_scan",
    "table.idx_scan",
})
_PATHS_GAUGE = frozenset({
    "table.n_dead_tup",
    "gaussdb.session.active",
    "gaussdb.session.idle_in_xact",
    "gaussdb.session.lock_wait",
    "gaussdb.session.waiting",
})
DELTA_PATHS = _PATHS_MONOTONIC | _PATHS_GAUGE


def _delta_window_s() -> int:
    """窗口基线的**最大**容忍间隔（秒）。缺省/空串/非法/≤0 ⇒ 0 = 关闭。"""
    raw = os.environ.get("DELTA_WINDOW_S", "300")
    try:
        v = float(str(raw).strip() or "300")
    except (TypeError, ValueError):
        return 0
    return int(round(v)) if v > 0 else 0


DELTA_WINDOW_S = _delta_window_s()
DELTA_STATE_PATH = os.environ.get("CC_GDB_PROBE_STATE", "/tmp/cc-gaussdb-probe-state.json")


def _load_delta_state() -> dict:
    try:
        with open(DELTA_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("v"), dict) else {}
    except (OSError, ValueError):
        return {}


def _save_delta_state(state: dict) -> None:
    try:
        tmp = DELTA_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, DELTA_STATE_PATH)
    except OSError:
        pass


class GaussdbProbe(BaseProbe):
    """GaussDB 层采集探针"""

    def __init__(self, dsn: Optional[str] = None, conn_kwargs: Optional[dict] = None):
        self._dsn = dsn or os.environ.get("GAUSSDB_DSN", "")
        self._conn_kwargs = conn_kwargs or self._dsn_to_kwargs(self._dsn)

    @property
    def identity(self) -> str:
        return "gaussdb"

    # ── 公共方法 ──

    def collect(self, sections: Optional[List[str]] = None, **kwargs) -> ProbeResult:
        """执行 GaussDB 采集

        Args:
            sections: 指定采集段，None=全量。段名经 canonical_section() 归一化，
                      两种写法（guide 段名 / 探针段键）都能被接受。

        Returns:
            ProbeResult（raw 中每个失败段额外带 "<name>__error__" 键）
        """
        start = time.time()
        req_sections = sections
        if req_sections:
            req_sections = [canonical_section(s) for s in req_sections]
        raw = self._do_collect(req_sections)
        facts = self._to_facts(raw)
        self._attach_window_deltas(facts)          # ★ 累计计数器/量规的窗口增量
        alerts = self._diagnose(facts, raw)
        alerts.extend(self._section_error_alerts(raw))
        alerts.extend(self._wlm_status_alerts(raw))

        return ProbeResult(
            layer="gaussdb",
            facts=facts,
            raw=raw,
            alerts=alerts,
            elapsed_ms=self._elapsed(start),
        )

    # ── 窗口基线（delta）─────────────────────────────────────────────────
    def _attach_window_deltas(self, facts: List[dict]) -> None:
        """给 `DELTA_PATHS` 里的事实挂上 `delta` + `deltaWindowS`（就地修改）。

        ★ 三条纪律（与 container/app 两层同构，详见模块内 `_PATHS_*` 的说明）：
          ① 基线缺失 / 过期 / 窗口非法 ⇒ **不产出** delta（下游报 unjudgeable，不得当 false）；
          ② 单调计数器差值为负 ⇒ 视为重置，不产出、**不取绝对值**；
          ③ 无论是否产出 delta，都要把本次读数写成新基线（否则永远没有第二次）。
        ★ 键 = `path|obj_id` ⇒ Table 级指标**逐表**独立做差。
        """
        win_max = DELTA_WINDOW_S
        if win_max <= 0:
            return
        prev = _load_delta_state()
        pv = prev.get("v") or {}
        pts = prev.get("ts")
        now = time.time()
        fresh = isinstance(pts, (int, float)) and not isinstance(pts, bool) and 0 < (now - pts) <= win_max
        win = int(round(now - pts)) if fresh else 0
        if win < 1:
            fresh = False

        cur = {}
        for f in facts:
            path = str(f.get("path") or "")
            if path not in DELTA_PATHS:
                continue
            v = f.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            key = "%s|%s" % (path, f.get("obj_id"))
            cur[key] = float(v)
            if not fresh or key not in pv:
                continue
            pvv = pv[key]
            if isinstance(pvv, bool) or not isinstance(pvv, (int, float)):
                continue
            d = float(v) - float(pvv)
            if path in _PATHS_MONOTONIC and d < 0:
                continue                       # 计数器回退 ⇒ 不产出，也不取绝对值
            f["delta"] = int(d) if float(d).is_integer() else d
            f["deltaWindowS"] = win
        _save_delta_state({"ts": now, "v": cur})

    def verify_sql(self, sql: str) -> dict:
        """SQL 语法校验（EXPLAIN）

        Returns:
            {"ok": bool, "error": str, "plan": str}

        ★ 2026-09-23 回归与修复：修「回放不依赖驱动」时（commit afcf359）本方法的
          `import async_gaussdb` 与 `import asyncio` 被**连带删掉** ——
          因为两者当时写在同一个语句块里，我替换 `_do_collect` 的导入时把本处一并命中。
          症状：调用 `verify_sql()` 直接 `NameError: name 'asyncio' is not defined`
          （驱动名甚至都不会被解析到）。**没有测试覆盖这个方法的导入**，是我在
          做 skip 重评、真去调用它时才撞出来的。
        """
        import asyncio
        import async_gaussdb

        async def _verify():
            conn = await async_gaussdb.connect(**self._conn_kwargs)
            try:
                plan = await conn.fetch(f"EXPLAIN {sql}")
                return {"ok": True, "error": "", "plan": "\n".join(
                    str(r.get("QUERY PLAN") or r.get("query plan") or "") for r in plan)}
            except Exception as e:
                return {"ok": False, "error": str(e), "plan": ""}
            finally:
                await conn.close()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_verify())
        finally:
            loop.close()

    def _dsn_to_kwargs(self, dsn: str) -> dict:
        """解析 DSN 连接字符串"""
        kv = dict(re.findall(r"(\w+)=(\S+)", dsn))
        return {
            "host": kv.get("host"),
            "port": int(kv.get("port", 8000)),
            "user": kv.get("user"),
            "password": kv.get("password"),
            "database": kv.get("dbname", "postgres"),
            "timeout": int(kv.get("timeout", "10")),
        }

    # ── 内部方法 ──

    def _do_collect(self, sections: Optional[List[str]] = None) -> Dict[str, Any]:
        """同步执行 SQL 采集

        D2 修复要点：单个段的失败不再被静默吞掉 —— 失败时写入
        raw["<name>__error__"] = "<错误信息>"，由 _section_error_alerts() 转成告警。
        """
        # ★ 回放模式**不需要**真实驱动：打桩本身就是替身（`_SimConn` 不建立任何连接）。
        #   原实现在此处无条件 `import async_gaussdb`，于是「只在生产 venv 装了驱动」的
        #   环境里**回放测试整体失败**（实测：`No module named 'async_gaussdb'`）
        #   —— 既掩盖了回放链本身是否正常，也让演练设施在开发机上不可用。
        #   修法：live/record 必须有驱动（真去连库），replay 允许缺失（连库的是打桩）。
        import asyncio
        _mode_now = "live"
        if _sim is not None:
            try:
                _mode_now = _sim.mode()
            except Exception:                                  # noqa: BLE001
                _mode_now = "live"
        _has_driver = True
        try:
            import async_gaussdb
        except ImportError as _imp:
            _has_driver = False
            async_gaussdb = None
            if _mode_now != "replay":
                raise RuntimeError(
                    "采集驱动 async_gaussdb 不可用（mode=%s 需要真实连接）⇒ "
                    "回放模式不需要驱动，但 live/record 必须有：%s" % (_mode_now, _imp)) from _imp

        if sections:
            unknown = [s for s in sections if s not in _SQL]
            sqls = {k: v for k, v in _SQL.items() if k in sections}
        else:
            unknown = []
            # opt-in 段不纳入默认全量采集（避免改变既有载荷/TTL 时序）
            sqls = {k: v for k, v in _SQL.items() if k not in _OPT_IN_SECTIONS}

        async def _run():
            # ★ 模拟演练打桩（docs/60）：连接与 fetch 走 collect_sim 的 tap。
            #   live 模式下这三个调用**零介入**（直通真实实现，不读盘、不算指纹）。
            #   replay 模式下 real_connect 不会被调用（_SimConn 直接返回假连接）
            #   ⇒ 允许传 None（驱动缺失时即此情形）；live/record 已在上面保证非 None。
            conn = await _sim.connect_async(
                (async_gaussdb.connect if _has_driver else None), self._conn_kwargs)
            raw: Dict[str, Any] = {}
            try:
                for name, sql in sqls.items():
                    try:
                        # 仅含占位符的段做替换（既有段无 {} ，不受影响）
                        if "{" in sql:
                            sql = sql.format(**_SQL_FORMAT_VARS)
                        rows = await conn.fetch(sql)
                        raw[name] = [dict(r) for r in rows] if rows else []
                    except Exception as e:
                        # ★ D2：显式记录失败，不再伪装成"空结果"
                        raw[name] = []
                        raw[f"{name}__error__"] = str(e)[:300]
                for u in unknown:
                    raw[u] = []
                    raw[f"{u}__error__"] = f"未知采集段（探针不支持）: {u}"
                # ── 按需 EXPLAIN（方向2）：对慢 SQL 现取真计划 ──
                #   ★ 只在本段被显式请求时做（opt-in）：每条要多发一次 EXPLAIN，
                #     纳入默认采集会改变既有载荷/时序。
                #   ★ 失败一律写入 raw["plan_now__error__"]，不静默吞（D2 纪律）。
                if "slow_sql_for_plan" in sqls:
                    try:
                        raw["plan_now"] = await _collect_plan_now(
                            conn, raw.get("slow_sql_for_plan") or [])
                    except Exception as _e:                      # noqa: BLE001
                        raw["plan_now"] = []
                        raw["plan_now__error__"] = "%s: %s" % (type(_e).__name__, str(_e)[:250])
                return raw
            finally:
                await conn.close()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    # ── 采集段失败 → 探针级告警（D2 的核心工程改进）──

    def _wlm_status_alerts(self, raw: Dict[str, Any]) -> List[dict]:
        """WLM status 取值域与预期不符时告警

        为什么需要：排队态的**字符串**决定了 waiting_count 能否 > 0。若现场用的是别的写法
        （或该视图在某版本不再产出这些状态），waiting_count 会恒为 0，而「规则永不命中」
        不报任何错 —— 与 KD-WLM-01 同一族（值域不可达），只是从「阈值够不着」换成了
        「过滤条件命不中」。故把观测到的取值域与词表对照。
        """
        seen = raw.get("wlm_status_seen") or []
        novel = [x for x in seen if x not in WLM_KNOWN_STATUSES]
        if not novel:
            return []
        return [{
            "severity": "warning",
            "title": "WLM 状态词表出现未知取值",
            "detail": ("pg_session_wlmstat.status 观测到未知取值 %s（已知: %s）。排队数按 %s 统计；"
                       "若真实排队态写法不同，wlm.waiting_count 会恒为 0 而规则永不命中 —— "
                       "请核对本站版本写法后更新 WLM_QUEUE_STATUSES。"
                       % (novel, list(WLM_KNOWN_STATUSES), list(WLM_QUEUE_STATUSES))),
            "entity": "cluster", "source": "gaussdb_probe",
            "timestamp": datetime.now().isoformat(),
            "observed_statuses": seen,
        }]

    def _section_error_alerts(self, raw: Dict[str, Any]) -> List[dict]:
        """静默失败是本次最难发现的缺陷 —— 它伪装成「这段时间没有慢SQL」。
        这里把每个失败的采集段提升为显式告警。
        """
        alerts = []
        ts = datetime.now().isoformat()
        for key, val in raw.items():
            if not key.endswith("__error__"):
                continue
            section = key[: -len("__error__")]
            alerts.append({
                "severity": "warning",
                "title": f"采集段失败: {section}",
                "detail": f"GaussDB 采集段 `{section}` 执行失败，该段事实为 0（非真实空结果）：{val}",
                "entity": "cluster",
                "source": "gaussdb_probe",
                "timestamp": ts,
                "section": section,
                "error": str(val),
            })
        return alerts

    def _to_facts(self, raw: Dict[str, Any]) -> List[dict]:
        """原始数据 → 标准化 Fact 数组

        D3 修复要点：除汇总指标外，还产出「诊断语义事实」
        （sql.query / sql.plan_type / sql.seq_scan_tables / table.rows / …），
        使规则引擎与 propose_actions 能仅凭 gather 结果完成推理。
        """
        facts: List[dict] = []
        ts = datetime.now().isoformat()
        src = "gaussdb.collect"

        # ── 节点标识（拓扑事实由 main.py 补充，这里给出 cluster 占位）──
        # sessions 汇总
        sess = raw.get("sessions") or []
        if sess and sess[0].get("total") is not None:
            s = sess[0]
            for path, key in [("gaussdb.session.total", "total"),
                              ("gaussdb.session.active", "active"),
                              ("gaussdb.session.waiting", "waiting"),
                              ("gaussdb.session.idle_in_xact", "idle_in_xact"),
                              ("gaussdb.session.lock_wait", "lock_wait")]:
                facts.append(self._make_fact(path, s.get(key, 0), "cluster", "cluster", src, ts))
            # 规则引擎契约路径
            facts.append(self._make_fact("session.state", "active", "cluster:active", "cluster", src, ts))
            if (s.get("waiting") or 0) > 0:
                facts.append(self._make_fact("cluster.wait_lock_count", s["waiting"],
                                             "cluster", "cluster", src, ts))
            facts.append(self._make_fact("node.active_session_count", s.get("active", 0),
                                         "cluster", "cluster", src, ts))

        # 连接数
        for c in raw.get("connections") or []:
            total, maxc = c.get("total", 0), c.get("max_conn", 0)
            if maxc:
                facts.append(self._make_fact("cluster.connection_total", total, "cluster", "cluster", src, ts))
                facts.append(self._make_fact("cluster.connection_max", maxc, "cluster", "cluster", src, ts))
                facts.append(self._make_fact(
                    "node.connection_usage_ratio",
                    round((total or 0) * 100.0 / maxc, 2), "cluster", "cluster", src, ts))

        # 会话明细（锁等待 / 长事务）
        for sd in raw.get("session_detail") or []:
            sid = f"sess-{sd.get('pid')}"
            if sd.get("state"):
                facts.append(self._make_fact("session.state", sd["state"], sid, "session", src, ts))
            # ★ M3 修复：只在该会话**确实**在等锁时发 wait_event，且取真实锁类型。
            #   原实现 `sd.get("enqueue") or "lock_wait"` 会在 enqueue 恒 NULL 的 GaussDB 上
            #   给每个 waiting 会话发同一个字面量 "lock_wait"（不在契约 enum 内）。
            _we = sd.get("lock_wait_type") or sd.get("enqueue")
            if sd.get("waiting") and _we:
                facts.append(self._make_fact("session.wait_event", _we, sid, "session", src, ts))
            # ★ M1 修复：优先用 SQL 里算好的 xact_age_ms（无时区问题）；
            #   xact_start 字符串回退路径保留，但仅供不支持该列的旧库使用。
            _age_ms = None
            if sd.get("xact_age_ms") is not None:
                try:
                    _age_ms = int(float(sd["xact_age_ms"]))
                except (TypeError, ValueError):
                    _age_ms = None
            elif sd.get("xact_start"):
                _age_ms = self._xact_ms(sd["xact_start"])
            if _age_ms is not None:
                facts.append(self._make_fact(
                    "transaction.duration_ms", _age_ms, f"tx-{sd.get('pid')}",
                    "transaction", src, ts))

        # 等待事件
        for w in raw.get("wait_events") or []:
            ev = w.get("wait_event") or w.get("wait_status")
            if ev and w.get("cnt"):
                facts.append(self._make_fact("session.wait_event", ev, f"wait:{ev}",
                                             "wait_event", src, ts))
                facts.append(self._make_fact(
                    "cluster.wait_event_total",
                    w["cnt"], "cluster", "cluster", src, ts))

        # ASP 事件分布
        asp_total = 0
        for a in raw.get("asp") or []:
            ev = a.get("event") or "none"
            cnt = a.get("cnt") or 0
            asp_total += cnt
            if ev != "none":
                facts.append(self._make_fact("session.asp_event", ev, f"asp:{ev}", "asp", src, ts))
        if asp_total:
            facts.append(self._make_fact("cluster.asp_samples_1h", asp_total,
                                         "cluster", "cluster", src, ts))

        # ── IO 等待中继（P4）：条件边的判据面 ──
        # 近期证据取 ASP 采样里 IO 类等待的条数（补「累计均值不敏感」这一局限）
        _io_recent = 0
        for a in raw.get("asp") or []:
            ev = str(a.get("event") or "").lower()
            if ("io" in ev or "wal" in ev or "datafile" in ev or "ckpt" in ev):
                _io_recent += int(a.get("cnt") or 0)
        facts.extend(io_relay_facts(
            raw.get("io_wait_relay") or [],
            raw.get("io_file_stats") or [],
            _io_recent, 60, ts, src))

        # 锁
        wait_locks = sum(1 for lk in (raw.get("locks") or []) if not lk.get("granted", True))
        if wait_locks:
            facts.append(self._make_fact("session.wait_event", "lock", "lock:waiting",
                                         "lock", src, ts))
            facts.append(self._make_fact("cluster.wait_lock_count", wait_locks, "cluster",
                                         "cluster", src, ts))

        # ── 按需计划面（方向2）：从**现取的真计划**产出计划事实 ──
        #   ★ 与上面的 `sql.plan_*`（来自 statement_history.query_plan）是**两条独立的
        #     证据来源**，故用独立路径名，避免把「历史的 2 条」与「现取的 9 条」
        #     混成同一批事实（多值语义下会互相覆盖、判定不确定）。
        #   ★ 含占位符的 SQL 用合成样本值 ⇒ 计划**形状**可靠、**行数/选择性是近似**；
        #     故额外产出 `sql.plan_now_fidelity`，让规则/报告能区分两者。
        #   ★ fail-closed：取不到计划只产出原因，**不产出** plan_type/cost。
        _pn_rows = raw.get("plan_now") or []
        for _pr in _pn_rows:
            _pqid = _pr.get("unique_query_id")
            _pobj = "sql-now-%s" % _pqid
            _pfid = str(_pr.get("fidelity") or "unknown")
            if not _pr.get("ok"):
                facts.append(self._make_fact("sql.plan_unavailable_reason",
                                             str(_pr.get("reason") or "")[:300],
                                             _pobj, "sql", src, "plan_now"))
                continue
            _pl = _pr.get("plan") or {}
            if _pl.get("plan_type"):
                facts.append(self._make_fact("sql.plan_type", _pl["plan_type"], _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("seq_scan_tables"):
                facts.append(self._make_fact("sql.seq_scan_tables",
                                             ",".join(_pl["seq_scan_tables"]), _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("indexes_used"):
                facts.append(self._make_fact("sql.indexes_used",
                                             ",".join(_pl["indexes_used"]), _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("has_big_sort"):
                facts.append(self._make_fact("sql.big_sort", "true", _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("has_big_agg"):
                facts.append(self._make_fact("sql.agg_flag", "big_agg", _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("has_nested_loop"):
                facts.append(self._make_fact("sql.has_nested_loop", "true", _pobj,
                                             "sql", src, "plan_now"))
            if _pl.get("total_cost") is not None:
                facts.append(self._make_fact("sql.plan_total_cost", _pl["total_cost"],
                                             _pobj, "sql", src, "plan_now"))
            facts.append(self._make_fact("sql.plan_now_fidelity", _pfid, _pobj,
                                         "sql", src, "plan_now"))
            for _risk in (_pr.get("risks") or []):
                facts.append(self._make_fact("sql.plan_risk", _risk.get("kind"), _pobj,
                                             "sql", src, "plan_now"))
        if _pn_rows:
            _ok_n = sum(1 for x in _pn_rows if x.get("ok"))
            facts.append(self._make_fact("cluster.plan_now_ok_count", _ok_n,
                                         "cluster", "cluster", src, "plan_now"))
            _syn = sum(1 for x in _pn_rows if x.get("ok")
                       and x.get("fidelity") == "synthesized_binds")
            facts.append(self._make_fact("cluster.plan_now_synthesized_count", _syn,
                                         "cluster", "cluster", src, "plan_now"))

        # ── 慢 SQL（D3：产出诊断语义事实）──
        slow_rows = raw.get("slow_sql_detail") or raw.get("slow_sql") or []
        slow_durations: List[float] = []
        for sh in slow_rows:
            qid = sh.get("unique_query_id")
            sid = f"sql-{qid}"
            exec_ms = self._us_to_ms(sh.get("execution_time"))
            db_ms = self._us_to_ms(sh.get("db_time"))
            slow_durations.append(exec_ms)
            facts.append(self._make_fact("sql.slow_exec_ms", exec_ms, sid,
                                         "sql", src, "dbe_perf.statement_history"))
            facts.append(self._make_fact("sql.db_time_ms", db_ms, sid,
                                         "sql", src, "dbe_perf.statement_history"))
            if sh.get("start_time"):
                facts.append(self._make_fact("sql.slow_start_time", str(sh["start_time"]), sid,
                                             "sql", src, "dbe_perf.statement_history"))
            if sh.get("cpu_time"):
                facts.append(self._make_fact("sql.cpu_time_ms", self._us_to_ms(sh["cpu_time"]),
                                             sid, "sql", src, "dbe_perf.statement_history"))
            if sh.get("data_io_time"):
                facts.append(self._make_fact("sql.data_io_time_ms", self._us_to_ms(sh["data_io_time"]),
                                             sid, "sql", src, "dbe_perf.statement_history"))
            # ── 耗时构成占比（2026-09-24，P1）──────────────────────────────
            # ★ 为什么在探针里算比值、而不是让规则去算：
            #   本仓规则引擎的判据面**只有单一路径 + 阈值**（`fact/op/value/阈值档`），
            #   **没有算术表达式能力**（无属性的 size_bytes → 派生出 size/MB 这类先例即为此）。
            #   若在规则里做占比，只能拆成「cpu > X 且 db > Y」两个独立阈值 ——
            #   而那不是占比，会漏判（小库小耗时的 CPU 占比同样可能很高）。
            #   ⇒ 照既有范式（`table.dead_tuple_ratio` / `data_skew_ratio` 均由探针产出比值）。
            #
            # ★ 量纲纪律（照 deadTupleRatio 的教训）：**产出 0–1 的分数，不是百分数**；
            #   阈值档写 0.9 表示 90%。曾经因为量纲不一致导致规则恒不成立。
            #
            # ★ 分母保护：db_time 为 0（字段缺失）时**不产出**占比 ——
            #   除以 0 无意义，且「产出 0」会让判据把「未知」读成「CPU 占比 0%」。
            _cpu_us, _db_us = sh.get("cpu_time"), sh.get("db_time")
            if _cpu_us is not None and _db_us:
                try:
                    _r = float(_cpu_us) / float(_db_us)
                    if 0.0 <= _r <= 1.0:
                        facts.append(self._make_fact("sql.cpu_time_ratio", round(_r, 4),
                                                     sid, "sql", src, "dbe_perf.statement_history"))
                    else:
                        # 理论不该出现（cpu 不应超过 db）；出现即观测面异常，如实暴露不掩盖
                        facts.append(self._make_fact("sql.cpu_time_ratio__invalid", round(_r, 4),
                                                     sid, "sql", src, "dbe_perf.statement_history"))
                except (TypeError, ValueError):
                    pass
            if sh.get("data_io_time") is not None and _db_us:
                try:
                    _r2 = float(sh["data_io_time"]) / float(_db_us)
                    if 0.0 <= _r2 <= 1.0:
                        facts.append(self._make_fact("sql.data_io_time_ratio", round(_r2, 4),
                                                     sid, "sql", src, "dbe_perf.statement_history"))
                except (TypeError, ValueError):
                    pass
            if sh.get("query"):
                facts.append(self._make_fact("sql.query", str(sh["query"])[:4000], sid,
                                             "sql", src, "dbe_perf.statement_history"))
            # 执行计划签名（statement_history 自带 query_plan，无需另发 EXPLAIN）
            sig = self._plan_signature(sh.get("query_plan"), sh.get("query"))
            if sig.get("plan_type"):
                facts.append(self._make_fact("sql.plan_type", sig["plan_type"], sid,
                                             "sql", src, "dbe_perf.statement_history"))
            if sig.get("seq_scan_tables"):
                facts.append(self._make_fact("sql.seq_scan_tables",
                                             ",".join(sig["seq_scan_tables"]), sid,
                                             "sql", src, "dbe_perf.statement_history"))
            if sig.get("has_big_agg") or sig.get("has_distinct"):
                facts.append(self._make_fact("sql.agg_flag",
                                             "big_agg" if sig.get("has_big_agg") else "count_distinct",
                                             sid, "sql", src, "dbe_perf.statement_history"))
            if sig.get("has_big_sort"):
                facts.append(self._make_fact("sql.big_sort", "true", sid,
                                             "sql", src, "dbe_perf.statement_history"))
            if sig.get("top_cost"):
                facts.append(self._make_fact("sql.top_cost", sig["top_cost"], sid,
                                             "sql", src, "dbe_perf.statement_history"))
            if str(sh.get("query") or "").lower().count("::") or "cast(" in str(sh.get("query") or "").lower():
                facts.append(self._make_fact("sql.has_type_cast", "true", sid,
                                             "sql", src, "dbe_perf.statement_history"))
        fact_slow = [f for f in slow_durations if f > 1000]
        facts.append(self._make_fact("cluster.slow_sql_count", len(fact_slow),
                                     "cluster", "cluster", src, "dbe_perf.statement_history"))
        if slow_durations:
            facts.append(self._make_fact(
                "cluster.slow_sql_avg_ms",
                round(sum(slow_durations) / len(slow_durations), 1),
                "cluster", "cluster", src, "dbe_perf.statement_history"))

        # ── 表统计（D3：产出 table.rows / 膨胀率，供 propose_actions 用）──
        for t in raw.get("table_stats") or []:
            tname = t.get("relname") or ""
            if not tname:
                continue
            # ★ M2.11（裁决 14）：Table 身份 = (schemaName, tableName) ⇒ obj_id 必须 schema 限定。
            # 无 schema 的行**直接跳过**（不伪造 tbl-.name），冻结侧因缺 key 事实 fail-closed。
            schema = str(t.get("schemaname") or "").strip()
            tbl = self._tbl_id(schema, tname)
            if not tbl:
                continue
            # 供冻结侧 auto_objects.Table 的 key_attrs[schemaName] 取值（本体对账用，不是指标，
            # 映射侧登记为 drop: identity_metadata_not_a_metric）。
            facts.append(self._make_fact("table.schema_name", schema, tbl, "table", src, ts))
            live = t.get("n_live_tup") or 0
            dead = t.get("n_dead_tup") or 0
            facts.append(self._make_fact("table.rows", live, tbl, "table", src, ts))
            facts.append(self._make_fact("table.n_live_tup", live, tbl, "table", src, ts))
            facts.append(self._make_fact("table.n_dead_tup", dead, tbl, "table", src, ts))
            dead_pct = t.get("dead_pct")
            if dead_pct is None:
                tot = live + dead
                dead_pct = round(dead * 100.0 / tot, 2) if tot > 0 else 0.0
            dead_pct = float(dead_pct)
            # 百分比形式（展示/排序用）
            facts.append(self._make_fact("table.dead_tuple_ratio_pct", dead_pct, tbl,
                                         "table", src, ts))
            # ★ 分数形式（规则契约）：规则 r_dead_tuple_ratio_high 的阈值写 0.2 且措辞是
            # 「死元组比例超过 20%」，说明契约语义是**分数**。原来只产出百分比(20.0)，
            # 与 0.2 比较恒为 false → 规则永不命中，属隐蔽的数量级/量纲错配。
            facts.append(self._make_fact("table.dead_tuple_ratio", round(dead_pct / 100.0, 4),
                                         tbl, "table", src, ts))
            # 2026-09-12：本体声明了 table.size_bytes 但采集侧从未产出（悬空承诺）。
            # 本段的 SQL 已加 total_bytes 列（pg_total_relation_size），此处产出事实。
            if t.get("total_bytes") is not None:
                facts.append(self._make_fact("table.size_bytes",
                                             self._to_int(t.get("total_bytes")),
                                             tbl, "table", src, ts))
            if dead_pct > 20:
                # ★ M2.11：原用裸 tname，靠映射侧 obj_id_rewrite 补前缀；现与其它 Table 事实
                # 统一用 schema 限定的 tbl（映射侧 rewrite 对已带 tbl- 的不再改写，向后兼容）。
                facts.append(self._make_fact("gaussdb.table.dead_ratio", dead_pct, tbl,
                                             "table", src, ts))
            la = t.get("last_analyze")
            if la:
                facts.append(self._make_fact("table.last_analyze", str(la), tbl, "table", src, ts))
                stale = self._stale_days(la)
                if stale is not None:
                    facts.append(self._make_fact("table.stat_stale_days", stale, tbl,
                                                 "table", src, ts))

        # ── 复制（位置差 → 字节/毫秒）──
        _lag_max = None
        _lag_max_app = ""
        _repl_not_streaming = 0
        _repl_senders = 0
        for r in raw.get("replication") or []:
            app = r.get("application_name", r.get("client_addr", "repl"))
            lag_bytes = self._to_int(r.get("replay_lag_bytes"))
            lag_mb = round(lag_bytes / 1048576.0, 4)
            facts.append(self._make_fact("replication.replay_lag_bytes", lag_bytes, app,
                                         "replication", src, "pg_stat_replication"))
            facts.append(self._make_fact("replication.replay_lag_mb", lag_mb, app,
                                         "replication", src, "pg_stat_replication"))
            _repl_senders += 1
            if _lag_max is None or (lag_bytes or 0) > _lag_max:
                _lag_max, _lag_max_app = (lag_bytes or 0), app
            if str(r.get("state") or "").strip().lower() != "streaming":
                _repl_not_streaming += 1
            # ★ FIX(2026-09-12)：此处原产出 `replay_lag_ms = lag_bytes` —— 单位错。
            #    规则 r_replication_lag 的判据是 `replay_lag_ms > 5000`（描述"超过5秒"），
            #    于是**任意 >4.88KB 的字节延迟都会被判成"延迟 N 秒"**，且秒数完全错误。
            #    实测 GaussDB 507 没有可用的「延迟毫秒」来源：
            #      · `gs_standby_read_delay()` 只能在 recovery（备机）上执行 → 主机报
            #        "Recovery is not in progress"
            #      · `gs_hadr_local_replay_delay_stat()` 的 delay_replay 在本环境全是 0
            #        （receive_time/replay_time 是 epoch 占位）
            #      · `pg_stat_replication` 无 lag 时间列，只有四个 LSN 位置
            #    故**不再伪造毫秒**：只给真实可得的字节，规则改用字节判据。
            #    实测参考：复制健康时字节延迟 = 0；早期记录健康态约 150KB。
            for k in ("state", "sync_state", "receiver_replay_location"):
                if r.get(k):
                    facts.append(self._make_fact(f"replication.{k}", r[k], app,
                                                 "replication", src, "pg_stat_replication"))

        # ★★ 2026-10-13 P0.7：复制状态的**集群级聚合** ★★
        #   缘由：`replication.*` 的逐 sender 事实 obj_type=replication，契约无此类型 ⇒ 被丢弃。
        #   而「主备是否落后 / 是否还在 Streaming」恰恰是**主备故障**的核心判据。
        #   ⇒ 增发集群级：最坏 lag + 非 Streaming 的 sender 数（由 cluster→GaussDBCluster 映射）。
        if _repl_senders:
            facts.append(self._make_fact("cluster.replay_lag_bytes_max", _lag_max, "cluster",
                                         "cluster", "pg_stat_replication"))
            facts.append(self._make_fact("cluster.replication_not_streaming_count",
                                         _repl_not_streaming, "cluster", "cluster",
                                         "pg_stat_replication"))

        # ── 长事务 ──
        for r in raw.get("long_tx") or []:
            if (r.get("cnt") or 0) > 0:
                facts.append(self._make_fact("cluster.long_tx_count", r["cnt"], "cluster",
                                             "cluster", src, ts))
                facts.append(self._make_fact("transaction.duration_ms", 300000, "tx:long",
                                             "transaction", src, ts))

        # ── 缓冲区命中率（规则 r_buffer_cache_low_hit）──
        # 说明：GaussDB 的 pg_stat_database 是库级汇总，没有 PG 的 node 维度，
        # 因此统一挂到 cluster 对象上，规则以 node.* 的路径名匹配（值语义等价）。
        for b in raw.get("buffer_stats") or []:
            try:
                hit = int(b.get("blks_hit") or 0)
                rd = int(b.get("blks_read") or 0)
            except (TypeError, ValueError):
                continue
            total_blk = hit + rd
            ratio = round(hit * 100.0 / total_blk, 4) if total_blk else 0.0
            facts.append(self._make_fact("node.blks_hit", hit, "cluster", "cluster", src, ts))
            facts.append(self._make_fact("node.blks_read", rd, "cluster", "cluster", src, ts))
            facts.append(self._make_fact("node.buffer_hit_ratio", ratio, "cluster",
                                         "cluster", src, ts))
            if (b.get("temp_bytes") or 0):
                facts.append(self._make_fact("node.temp_bytes", self._to_int(b.get("temp_bytes")),
                                             "cluster", "cluster", src, ts))
                tf = self._to_int(b.get("temp_files"))
                facts.append(self._make_fact("sql.temp_files_count", tf, "cluster",
                                             "cluster", src, ts))
                # ★ 规则契约语义：r_work_mem_insufficient 用 `sql.temp_file_written eq true`，
                # 即**布尔**（是否产生过临时文件），不是计数。原实现发的是计数(int)，
                # 与 `true` 比较恒为 false → 规则永不命中。这里按布尔语义产出。
                facts.append(self._make_fact("sql.temp_file_written", tf > 0, "cluster",
                                             "cluster", src, ts))
            if (b.get("deadlocks") or 0):
                facts.append(self._make_fact("cluster.deadlocks", self._to_int(b.get("deadlocks")),
                                             "cluster", "cluster", src, ts))
                # 供 r_deadlock_detected（其 when 用的是 alert.type / alert.status）
                facts.append(self._make_fact("alert.type", "deadlock", "alert:deadlock",
                                             "alert", src, ts))
                facts.append(self._make_fact("alert.status", "active", "alert:deadlock",
                                             "alert", src, ts))
                # ★★ 2026-10-13 P0.7：把探针的**诊断结论**送进 CCF ★★
                #   缘由：`alert.type` / `alert.status` 的 obj_type=alert，契约无此类型
                #   ⇒ 被 objtype_dropped 丢弃 ⇒ **探针明明已经诊断出死锁，推理层却看不到**。
                #   这不是「缺个指标」，而是**采集层的结论到不了上层**。
                #   ⇒ 同时发一条集群级数字信号（由 cluster→GaussDBCluster 映射进契约）。
                facts.append(self._make_fact("cluster.alert_active_count", 1, "cluster",
                                             "cluster", src, ts))
            if (b.get("xact_rollback") or 0):
                facts.append(self._make_fact("cluster.xact_rollback",
                                             self._to_int(b.get("xact_rollback")),
                                             "cluster", "cluster", src, ts))

        # ── 索引扫描（规则 r_index_bloat：大表 seq_scan 高 / idx_scan 缺失）──
        # ★★ 2026-10-13 P0.7：增加**逐表聚合**（索引未使用的直接判据）★★
        #   缘由：`index.idx_scan` 是逐索引的（obj_type=index），契约无 Index 类型 ⇒ 被丢弃
        #   ⇒ 「索引建了却从没被扫过」这个**索引缺失/冗余**的核心信号进不了 CCF。
        #   逐索引事实保留在 raw；此处按表汇总「从未被扫描的索引数」与「索引扫描合计」。
        _idx_by_tbl = {}
        for ix in raw.get("index_stats") or []:
            tname = ix.get("relname") or ""
            iname = ix.get("indexrelname") or ""
            if not iname:
                continue
            _sc = self._to_int(ix.get("idx_scan"))
            facts.append(self._make_fact("index.idx_scan", _sc,
                                         f"idx-{iname}", "index", src, ts))
            if tname:
                agg = _idx_by_tbl.setdefault(tname, {"n": 0, "unused": 0, "scan": 0,
                                                     "schemaname": ix.get("schemaname")})
                agg["n"] += 1
                agg["scan"] += (_sc or 0)
                if not _sc:
                    agg["unused"] += 1
        for _tn, _a in _idx_by_tbl.items():
            _tid = self._tbl_id(_a.get("schemaname"), _tn)
            if not _tid:
                continue
            facts.append(self._make_fact("table.index_count", _a["n"], _tid, "table", src, ts))
            facts.append(self._make_fact("table.unused_index_count", _a["unused"], _tid,
                                         "table", src, ts))
        for ts_row in raw.get("table_scan_stats") or []:
            tname = ts_row.get("relname") or ""
            if not tname:
                continue
            # ★ M2.11：与 table_stats 共用同一套 Table 身份（schema 限定）
            tbl = self._tbl_id(ts_row.get("schemaname"), tname)
            if not tbl:
                continue
            facts.append(self._make_fact("table.seq_scan", self._to_int(ts_row.get("seq_scan")),
                                         tbl, "table", src, ts))
            ix_scan = ts_row.get("idx_scan")
            if ix_scan is not None:
                facts.append(self._make_fact("table.idx_scan", self._to_int(ix_scan),
                                             tbl, "table", src, ts))
                # idx_scan = 0 且是大表 → 该表所有扫描都是顺序扫描
                if self._to_int(ix_scan) == 0 and self._to_int(ts_row.get("n_live_tup")) > 1_000_000:
                    facts.append(self._make_fact("index.idx_scan", 0, tbl, "table", src, ts))

        # ── 索引膨胀率（opt-in 段 index_bloat）★ 2026-09-12 新增 ──
        # 数据源：GaussDB 原生 gs_stat_freespace（零安装，见 _SQL 内注释）。
        # 本体中 index.bloatRatio 类型为 xsd:decimal，故按 float 产出；
        # 无法解析（索引不支持/函数报错）时**不产出该事实**，避免出现空值把
        # 规则判据搞成"未知"而不是"未测"。
        for ib in raw.get("index_bloat") or []:
            iname = ib.get("indexrelname") or ""
            if not iname:
                continue
            iid = f"idx-{iname}"
            tname = ib.get("relname") or ""
            if tname:
                facts.append(self._make_fact("index.table", tname, iid, "index", src, ts))

            br = self._to_float(ib.get("bloat_ratio"))
            if br is not None:
                facts.append(self._make_fact("index.bloat_ratio", br, iid, "index", src, ts))
            fsb = self._to_float(ib.get("freespace_bytes"))
            if fsb is not None:
                facts.append(self._make_fact("index.freespace_bytes", fsb, iid,
                                             "index", src, ts))
            dtp = self._to_float(ib.get("dead_tuple_per_page"))
            if dtp is not None:
                facts.append(self._make_fact("index.dead_tuple_per_page", dtp, iid,
                                             "index", src, ts))
            nb = self._to_int(ib.get("nblocks"))
            if nb:
                facts.append(self._make_fact("index.nblocks", nb, iid, "index", src, ts))
            sz = self._to_int(ib.get("idx_size_bytes"))
            # 膨胀浪费的字节数 = 实际 - 紧凑应有（= 实际/膨胀率）。供规则直接判"省多少空间"。
            if sz and br and br > 1:
                wasted = int(sz - sz / br)
                facts.append(self._make_fact("index.bloat_wasted_bytes", wasted, iid,
                                             "index", src, ts))

        # ── 索引定义：列 / 大小 / 唯一性（opt-in 段 index_defs）──
        # 2026-09-12：补齐「索引清单只有名字与扫描次数、没有列/定义/大小」的能力缺口。
        for ix in raw.get("index_defs") or []:
            iname = ix.get("indexrelname") or ""
            if not iname:
                continue
            iid = f"idx-{iname}"
            tname = ix.get("relname") or ""
            if tname:
                facts.append(self._make_fact("index.table", tname, iid, "index", src, ts))
            cols = parse_index_columns(ix.get("indexdef"))
            if cols:
                facts.append(self._make_fact("index.columns", ",".join(cols), iid,
                                             "index", src, ts))
                facts.append(self._make_fact("index.column_count", len(cols), iid,
                                             "index", src, ts))
            if ix.get("index_size_bytes") is not None:
                facts.append(self._make_fact("index.size_bytes",
                                             self._to_int(ix.get("index_size_bytes")),
                                             iid, "index", src, ts))
            if ix.get("is_unique") is not None:
                facts.append(self._make_fact("index.is_unique", bool(ix.get("is_unique")),
                                             iid, "index", src, ts))
            if ix.get("is_primary") is not None:
                facts.append(self._make_fact("index.is_primary", bool(ix.get("is_primary")),
                                             iid, "index", src, ts))

        # ── 真实运行时执行计划（opt-in 段 slow_sql_plan）──
        # 与 slow_sql_detail 的 signature（形状标记）互补：这里给计划**原文**。
        # 载荷考虑：单条计划约 2KB，故截到 2000 字符并显式记账截断标志。
        for pl in raw.get("slow_sql_plan") or []:
            qid = pl.get("unique_query_id")
            plan = str(pl.get("query_plan") or "")
            if not qid or not plan:
                continue
            sid = f"sql-{qid}"
            cap = 2000
            facts.append(self._make_fact("sql.query_plan", plan[:cap], sid,
                                         "sql", src, "dbe_perf.statement_history"))
            facts.append(self._make_fact("sql.query_plan_truncated", len(plan) > cap,
                                         sid, "sql", src, "dbe_perf.statement_history"))
            facts.append(self._make_fact("sql.query_plan_length", len(plan), sid,
                                         "sql", src, "dbe_perf.statement_history"))

        # ── 跨 DN 数据倾斜（规则 r_data_skew）──
        skew_buf: Dict[str, List[int]] = {}
        for sk in raw.get("table_skew") or []:
            tname = sk.get("relname") or ""
            try:
                live = int(sk.get("n_live_tup") or 0)
            except (TypeError, ValueError):
                continue
            # ★ M2.11：聚合键必须含 schema —— 同一 relname 的不同 schema 是**两张不同的表**，
            # 只按 relname 聚合会把 `snapshot.snapshot` 与 `db4ai.snapshot` 的实测值混在一起算倾斜比。
            tid = self._tbl_id(sk.get("schemaname"), tname)
            if tid:
                skew_buf.setdefault(tid, []).append(live)
        for tbl, live_list in skew_buf.items():
            if len(live_list) < 2:
                continue
            mx, avg = max(live_list), sum(live_list) / len(live_list)
            if avg <= 0 or mx <= 1_000_000:
                continue
            ratio = round(mx / avg, 3)
            facts.append(self._make_fact("table.data_skew_ratio", ratio, tbl,
                                         "table", src, ts))

        # ── WLM 资源池队列（规则 r_wlm_queue_congested）──
        # ★ 对象身份 = **资源池**（不再用用户名）：排队是「池」这一层的现象，
        #   规则条件 `wlm.queue_name exists` 的语义正是「定位到具体资源池」（见 A3 取证提示）。
        for w in raw.get("wlm_stats") or []:
            pool = str(w.get("resource_pool") or "unknown")
            waiting = self._to_int(w.get("waiting_count"))
            facts.append(self._make_fact("wlm.queue_name", pool, f"wlm-{pool}",
                                         "wlm", src, ts))
            facts.append(self._make_fact("wlm.waiting_count", waiting, f"wlm-{pool}",
                                         "wlm", src, ts))
        # 观测到的 status 取值域 → 留在 raw 里（不新增事实路径），由 _wlm_status_alerts() 转告警
        seen = sorted({str(r.get("status")) for r in (raw.get("wlm_status_vocab") or [])
                       if r.get("status")})
        if seen:
            raw["wlm_status_seen"] = seen
        # ── D2-8：WAL 目录（集群级）──
        #   ★ 用**文件数**表达"WAL 有生成"（源侧 step8_wal_generation_rate 的语义），
        #     不用 pg_current_wal_lsn() 的文本比较 —— 那是字符串不是可比较数值。
        #   与 D2-7 一致：不发「WAL 文件大小」，避免把「留存的 WAL 文件大小」误当成「生成速率」。
        for w in raw.get("wal_files") or []:
            facts.append(self._make_fact("wal.file_count", self._to_int(w.get("wal_file_count")),
                                         "cluster", "cluster", src, ts))

        # ── D2-8：行宽（表级）：源侧 bytes_per_row > 500 ⇒ 表达 span、不表达规模 ──
        #   ★ 为什么不发绝对行宽：源侧那条分支的 name=pg006_table_bloat 且源查询按
        #     **表大小排序取 TOP**，其「异常高」隐含「大表」前提；无该前提时绝对行宽
        #     会把全部表都判成异常（实测 order_line 34MB/3.6亿行 = 0.09 字节显然失真，
        #     故分母用 n_live_tup 并防零除；span = 最大值判定避免"每张表都命中"）。
        # ★ M2.11：obj_id 改成 schema 限定的 tbl-<schema>.<relname>（无 schema 的行丢弃）。
        _rw = [(self._tbl_id(x.get("schemaname"), x.get("relname")),
                self._to_int(x.get("bytes")), self._to_int(x.get("live")))
               for x in (raw.get("table_row_width") or [])]
        _ratios = []
        for _tid, _b, _l in _rw:
            if _tid and _b and _l:
                _ratios.append((round(_b * 1.0 / _l, 4), _tid))
        if _ratios:
            _mx, _mn = max(_ratios), min(_ratios)
            facts.append(self._make_fact("table.row_width_span_pct",
                                         round(_mx[0] * 100.0 / _mn[0], 4) if _mn[0] else 0,
                                         "tables", "tables", src, ts))
            facts.append(self._make_fact("table.row_width_max", _mx[0], _mx[1], "table", src, ts))

        # ── D2-8：填充因子（表级）：源侧 fillfactor eq 100 = 「未设填充因子」──
        for f in raw.get("table_fillfactor") or []:
            # ★ M2.11：obj_id = tbl-<schema>.<relname>（原为裸表名，靠 obj_id_rewrite 补前缀）
            tbl = self._tbl_id(f.get("schemaname"), f.get("relname"))
            if not tbl:
                continue
            facts.append(self._make_fact("table.fillfactor", self._to_int(f.get("fillfactor")) or 100,
                                         tbl, "table", src, ts))

        # ── D2-8：主备角色 / 回放延迟 ──
        #   ★ 主库上**刻意不发** replay_lag_sec：那样表达的是「无数据」而非「为假」，
        #     否则「主库没有回放延迟」会被读成「实测 0 秒」。
        for r in raw.get("replication_role") or []:
            # ★ 2026-10-13 端到端修复：**发真布尔**，不发字符串 "true"/"false"。
            #   缘由：契约里 `db.cluster.is_standby` 的 type = boolean，
            #   而 CCF 校验器对「非分类型指标落成字符串」判 ERROR
            #   （string_value_on_numeric_metric）⇒ **投影器拒绝投影**（fail-closed）。
            #   本文件他处（sql.temp_file_written）本就用真布尔，此处属不一致。
            #   ★ 归一化：驱动可能回 bool / 't'/'f' / 'true'/'false'，逐一处理，
            #     否则字符串 'f' 的真值性为 True ⇒ 会把备机误判成主机。
            _sb = r.get("is_standby")
            if isinstance(_sb, str):
                _sb = _sb.strip().lower() in ("t", "true", "1", "yes", "on")
            else:
                _sb = bool(_sb)
            facts.append(self._make_fact("replication.is_standby", _sb,
                                         "cluster", "cluster", src, ts))
            if r.get("replay_lag_sec") is not None:
                facts.append(self._make_fact("replication.replay_lag_sec",
                                             self._to_int(r.get("replay_lag_sec")),
                                             "cluster", "cluster", src, ts))

        # ── D2-8：预备事务 / 预备语句（集群级）──
        for r in raw.get("prepared_xacts") or []:
            facts.append(self._make_fact("xact.prepared_count",
                                         self._to_int(r.get("prepared_count")), "cluster", "cluster", src, ts))
        for r in raw.get("prepared_stmts") or []:
            facts.append(self._make_fact("xact.prepared_statement_count",
                                         self._to_int(r.get("prepared_count")), "cluster", "cluster", src, ts))

        # ── D2-8：逻辑复制订阅 / 发布时间线 ──
        for r in raw.get("subscriptions") or []:
            facts.append(self._make_fact("subscription.count",
                                         self._to_int(r.get("subscription_count")), "cluster", "cluster", src, ts))
            facts.append(self._make_fact("publication.table_count",
                                         self._to_int(r.get("publication_table_count")), "cluster", "cluster", src, ts))
        for r in raw.get("timeline") or []:
            facts.append(self._make_fact("cluster.timeline_id",
                                         self._to_int(r.get("timeline_id")), "cluster", "cluster", src, ts))

        # ── D2-7：检查点 / bgwriter（集群级）──
        #   ★ 只发**语义可对齐**的事实：checkpoint_sync_time 是累计值，源侧问的是单次
        #     ⇒ 刻意不发（发出去会被映射成恒真判据）。
        for w in raw.get("bgwriter_stats") or []:
            req = self._to_int(w.get("checkpoints_req"))
            timed = self._to_int(w.get("checkpoints_timed"))
            total = req + timed
            b_ck = self._to_int(w.get("buffers_checkpoint"))
            b_cl = self._to_int(w.get("buffers_clean"))
            b_be = self._to_int(w.get("buffers_backend"))
            b_all = b_ck + b_cl + b_be
            facts.append(self._make_fact("checkpoint.req_pct",
                                         round(req * 100.0 / total, 4) if total else 0,
                                         "cluster", "cluster", src, ts))
            facts.append(self._make_fact("bgwriter.buffers_backend", b_be, "cluster", "cluster", src, ts))
            facts.append(self._make_fact("bgwriter.buffers_backend_pct",
                                         round(b_be * 100.0 / b_all, 4) if b_all else 0,
                                         "cluster", "cluster", src, ts))
            facts.append(self._make_fact("bgwriter.maxwritten_clean",
                                         self._to_int(w.get("maxwritten_clean")),
                                         "cluster", "cluster", src, ts))

        # ── D2-7：HOT 更新比例（表级；源侧 hot_pct 是百分数，本项目同口径）──
        for h in raw.get("hot_update_stats") or []:
            upd = self._to_int(h.get("n_tup_upd"))
            hot = self._to_int(h.get("n_tup_hot_upd"))
            if not upd:
                continue
            # ★ M2.11：obj_id = tbl-<schema>.<relname>（原为裸表名/`tbl-unknown` 兜底）
            tbl = self._tbl_id(h.get("schemaname"), h.get("relname"))
            if not tbl:
                continue
            facts.append(self._make_fact("table.hot_update_ratio_pct",
                                         round(hot * 100.0 / upd, 4), tbl, "table", src, ts))

        # ── D2-7：复制发送侧 / 槽（集群级）──
        for r in raw.get("replication_detail") or []:
            facts.append(self._make_fact("replication.sender_count",
                                         self._to_int(r.get("sender_count")), "cluster", "cluster", src, ts))
            facts.append(self._make_fact("replication.pending_bytes",
                                         self._to_int(r.get("pending_bytes")), "cluster", "cluster", src, ts))
            facts.append(self._make_fact("replication.flush_pending_bytes",
                                         self._to_int(r.get("flush_pending_bytes")), "cluster", "cluster", src, ts))
        for r in raw.get("replication_slots") or []:
            facts.append(self._make_fact("replication.slot_count",
                                         self._to_int(r.get("slot_count")), "cluster", "cluster", src, ts))
            facts.append(self._make_fact("replication.slot_active_count",
                                         self._to_int(r.get("slot_active_count")), "cluster", "cluster", src, ts))

        # ── D2-7：参数类（集群级）──
        #   param.memory_setting_count：源侧 step1_memory_config 数的是「查到的内存参数行数」；
        #   param.sync_standby_names_empty：源侧 step2_sync_config 的 `count eq 0` 即「无同步备机配置」，
        #     用**空串判定**表达（比发一个 0 行计数更贴近其语义，也与「未配置」一一对应）。
        _ps = {str(x.get("name")): str(x.get("setting") or "") for x in (raw.get("param_stats") or [])}
        _mem = [k for k in ("shared_buffers", "work_mem", "maintenance_work_mem") if k in _ps]
        facts.append(self._make_fact("param.memory_setting_count", len(_mem),
                                     "cluster", "cluster", src, ts))
        facts.append(self._make_fact("param.sync_standby_names_empty",
                                     "true" if not (_ps.get("synchronous_standby_names") or "").strip() else "false",
                                     "cluster", "cluster", src, ts))

        # 用户级：溢写（语义不变，仍按用户发；对象身份与池级队列事实不同，故 obj_type 分开）
        for w in raw.get("wlm_user_stats") or []:
            user = str(w.get("username") or "unknown")
            if w.get("used_spill_space"):
                facts.append(self._make_fact("wlm.used_spill_space",
                                             self._to_int(w.get("used_spill_space")),
                                             f"wlm-user-{user}", "wlm_user", src, ts))
        return facts

    # ── 工具方法 ──

    @staticmethod
    def _us_to_ms(v: Any) -> float:
        """GaussDB 计时列单位为微秒 → 毫秒"""
        try:
            return round(float(v or 0) / 1000.0, 1)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _to_int(v: Any) -> int:
        try:
            return int(float(v or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_float(v: Any) -> Optional[float]:
        """转 float；**无法解析返回 None**。

        与 _to_int 的 0 兜底语义不同：这里的 None 表示「未测到」，
        调用方据此决定「不产出该事实」—— 而不是产出一个 0 让规则把
        「未知」误判成「健康」。用于 index.bloat_ratio 等可选指标。
        """
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _xact_ms(xact_start: Any) -> int:
        """★ 仅作**回退**：`xact_start` 是不带时区的字符串（按 DB 会话时区渲染），
        而 `datetime.now()` 是探针宿主机本地时间 ⇒ 两者时区不一致时**必然算错**
        （实测：DB 会话时区 Etc/GMT-8 + 容器 UTC ⇒ 恒 -8 h = -28797184 ms）。

        正确做法是让**两个时间都来自 DB**，即 `session_detail` 里新增的
        `xact_age_ms`（`now() - xact_start`）；调用方优先用它。
        本函数只在旧库缺少该列时使用，故结果**如实返回、不取绝对值、不夹紧**——
        错就是错，不能掩盖。
        """
        try:
            if isinstance(xact_start, datetime):
                return int((datetime.now(xact_start.tzinfo) - xact_start).total_seconds() * 1000)
            return int((datetime.now() - datetime.fromisoformat(str(xact_start))).total_seconds() * 1000)
        except Exception:
            return 0

    @staticmethod
    def _stale_days(last_analyze: Any) -> Optional[int]:
        try:
            la = last_analyze if isinstance(last_analyze, datetime) \
                else datetime.fromisoformat(str(last_analyze).replace("+00:00", ""))
            return (datetime.now() - la).days
        except Exception:
            return None

    @staticmethod
    def _tbl_id(schema: Any, relname: Any) -> Optional[str]:
        """Table 侧 obj_id = ``tbl-<schemaname>.<relname>``（★ M2.11 / 裁决 14，2026-09-28）。

        为什么必须 schema 限定：本体契约 shared/model/topology.yaml:189 声明
        Table 的 key_attrs = [schemaName, tableName]，而真机同一库确有跨 schema
        同名表 —— 只读实测 `snapshot.snapshot` 与 `db4ai.snapshot` 共用 relname
        `snapshot`（全库 79 张用户表：snapshot 63 / root 9 / dbe_profiler 4 /
        dbe_pldeveloper 2 / db4ai 1）。此前只取 relname，这两张不同的表在冻结侧
        会被静默并成同一个对象。

        schema 或 relname 为空 ⇒ **返回 None**，调用方跳过该行（不伪造
        `tbl-.name` / `tbl-None.name`）。宁可少报一条，也不产出一个身份错误的
        观测 —— 冻结侧因缺 `table.schema_name` key 事实而 fail-closed。
        """
        s = str(schema or "").strip()
        n = str(relname or "").strip()
        if not s or not n:
            return None
        return "tbl-%s.%s" % (s, n)

    @staticmethod
    def _plan_signature(query_plan: Any, query_text: Any = "") -> Dict[str, Any]:
        """执行计划签名（与 main.py._plan_signature 同构，收敛 D3 的契约口径）"""
        out: Dict[str, Any] = {"plan_type": "", "has_seq_scan": False, "seq_scan_tables": [],
                               "has_big_agg": False, "has_big_sort": False,
                               "has_distinct": False, "top_cost": ""}
        plan = str(query_plan or "")
        qt = str(query_text or "").lower()
        if plan:
            if re.search(r"Seq Scan", plan):
                out["plan_type"] = "seq_scan"
            elif re.search(r"Index (Only )?Scan", plan):
                out["plan_type"] = "index_scan"
            for m in re.finditer(r"Seq Scan on ([\w.]+)", plan):
                t = m.group(1).split(".")[-1]
                if t not in out["seq_scan_tables"]:
                    out["seq_scan_tables"].append(t)
            for m in re.finditer(r"(GroupAggregate|HashAggregate)\s+\(cost=[^)]*rows=([\d.]+)", plan):
                try:
                    if float(m.group(2)) > 1e6:
                        out["has_big_agg"] = True
                except ValueError:
                    pass
            for m in re.finditer(r"Sort\s+\(cost=\S+\.\.\S+\s+rows=([\d.]+)", plan):
                try:
                    if float(m.group(1)) > 1e6:
                        out["has_big_sort"] = True
                except ValueError:
                    pass
            m = re.search(r"\(cost=([\d.]+)\.\.([\d.]+)", plan)
            if m:
                out["top_cost"] = f"{m.group(1)}..{m.group(2)}"
        if re.search(r"count\s*\(\s*distinct", qt):
            out["has_distinct"] = True
        return out

    # ── 诊断 ──

    def _diagnose(self, facts: List[dict], raw: Dict[str, Any]) -> List[dict]:
        """基于采集数据做探针级诊断"""
        alerts = []
        ts = datetime.now().isoformat()

        for f in facts:
            path, val = f.get("path"), f.get("value")
            if path == "gaussdb.session.lock_wait" and (val or 0) > 0:
                alerts.append({
                    "severity": "warning",
                    "title": "锁等待检测",
                    "detail": f"存在 {val} 个锁等待会话",
                    "entity": "cluster",
                    "source": "gaussdb_probe",
                    "timestamp": ts,
                })
            if path == "cluster.slow_sql_count" and (val or 0) > 5:
                alerts.append({
                    "severity": "warning",
                    "title": "慢SQL过多",
                    "detail": f"最近有 {val} 条慢查询（执行耗时 >1s）",
                    "entity": "cluster",
                    "source": "gaussdb_probe",
                    "timestamp": ts,
                })
            if path == "node.connection_usage_ratio" and (val or 0) > 80:
                alerts.append({
                    "severity": "critical",
                    "title": "连接数接近上限",
                    "detail": f"连接使用率 {val}%（>80%）",
                    "entity": "cluster",
                    "source": "gaussdb_probe",
                    "timestamp": ts,
                })
            if path == "table.dead_tuple_ratio" and (val or 0) > 30:
                alerts.append({
                    "severity": "warning",
                    "title": "表膨胀严重",
                    "detail": f"表 {f.get('obj_id')} 死元组占比 {val}%",
                    "entity": f.get("obj_id", ""),
                    "source": "gaussdb_probe",
                    "timestamp": ts,
                })
        return alerts


# ── 注册到全局注册表 ──
register_probe(GaussdbProbe())
