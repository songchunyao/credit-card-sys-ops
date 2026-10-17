# -*- coding: utf-8 -*-
"""采集层 / 数据面 —— 两栈共用（v2 + v3）

职责（docs/50 §3）：
  * 四层探针采集执行（gaussdb / os / storage / tpops）
  * 事实标准化（_to_facts）、修复提案（_fix_proposals）、真实拓扑发现
  * 采集结果服务端缓存（_CACHED_COLLECT_*，TTL 300s）
  * 数据面端点：/tools/gather /tools/collect /tools/plan /tools/execute
    /tools/table_stats /tools/search_logs /verify /anchor 之外的采集面
  * /health（基础块 + 汇聚知识面贡献块）

边界约束（由 tests/static/test_layer_boundary.py 静态锁定）：
  本模块 **不得** import neo4j、不得含症状词表、不得决定「采什么」。
  采集入口一律由调用方传 layers/sections —— 这正是 v2/v3 的差异所在
  （v2 走 Neo4j 图推理，v3 走 GraphDB 本体推理），放在这里会抹平差异。

与知识面的耦合一律通过 service.common 的注册表钩子反转：
  * get_recent_changes()  —— 由知识面注册（Neo4j Change 节点）
  * collect_health_contributions() —— 由知识面注册（neo4j / rules / 契约块）
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from service.common import (                                          # noqa: E402
    AGENT_DIR, BASE_DIR, _load_env_file, _loop_guard, _run_sync,
    collect_health_contributions, compress_facts, get_recent_changes,
)
from service.probes import collect_layers, get_probe, ProbeResult     # noqa: E402

# ★ 模拟演练打桩（docs/60）。live 下所有 tap 都是**零介入**的直通。
try:
    from service import collect_sim as _sim
except Exception:
    try:
        import collect_sim as _sim
    except Exception:
        _sim = None

_load_env_file(os.path.join(BASE_DIR, ".env"))                        # service/.env
_load_env_file(os.path.join(os.path.dirname(BASE_DIR), ".env"))       # 仓库根 .env

GAUSSDB_DSN = os.environ.get("GAUSSDB_DSN", "")

# 有界采集端点（/tools/gather_bounded）单次回传的慢SQL条数上限。
# slow_sql_detail 每条含 query 全文（实测单条 ~2KB），条数多时同样会撑爆上下文。
_BOUNDED_MAX_SLOW_SQL = 20

router = APIRouter()
def _dsn_to_kwargs(dsn: str) -> Dict[str, Any]:
    kv = dict(re.findall(r"(\w+)=(\S+)", dsn))
    return {
        "host": kv.get("host"),
        "port": int(kv["port"]) if kv.get("port") else 8000,
        "user": kv.get("user"),
        "password": kv.get("password"),
        "database": kv.get("dbname", "postgres"),
        "timeout": int(kv.get("timeout", "10")),
    }


_conn_kwargs = _dsn_to_kwargs(GAUSSDB_DSN)


def _conn_args():
    """主节点（DSN）连接参数；未配置 GAUSSDB_DSN 时 host=None，调用方 try/except 捕获并返回错误"""
    return (_conn_kwargs["host"], _conn_kwargs["port"], _conn_kwargs["user"],
            _conn_kwargs["password"], _conn_kwargs["database"])


def _parse_nodes(spec: str) -> List[Dict[str, Any]]:
    """GAUSSDB_NODES=host:port:node_id:node_type:alias[,host:port:node_id:node_type:alias...]"""
    nodes: List[Dict[str, Any]] = []
    for _item in (spec or "").split(","):
        _item = _item.strip()
        if not _item:
            continue
        try:
            _host, _port, _nid, _ntype, _alias = (x.strip() for x in _item.split(":"))
            nodes.append({"host": _host, "port": int(_port), "node_id": _nid,
                          "node_type": _ntype, "alias": _alias})
        except ValueError:
            continue
    return nodes



class AnchorRequest(BaseModel):
    alert_id: str = "alert-p99"
    user_text: str = ""


class CollectRequest(BaseModel):
    cluster_oid: str = "cluster-prod-01"
    anchor_types: List[str] = []


class VerifyRequest(BaseModel):
    rewritten_sql: str = ""
    cluster_oid: str = "79b62ee45b9e401fb06ca86941d5f419in14"


# ─── 采集结果缓存（内存 dict，替代无意义的 Neo4j 实时写入）───
_COLLECT_CACHE: Dict[str, Any] = {
    "data": None,
    "ts": None,
    "ttl_seconds": 120,  # 缓存 2 分钟，同一用户问题的多次工具调用共享
}


# ────────────────────────────────────────────────────────────
# NEW: 本体驱动的"采集指引"映射表
# 问题类别 → {采集对象, 属性, 关系链, GaussDB SQL模板}
# 这是本体驱动采集的核心：Agent 先查"这个症状需要采集什么"，
# 然后只采集必要的对象/属性，避免全量采集。
# ────────────────────────────────────────────────────────────

# 症状分类关键词映射

_CACHED_COLLECT_RESULT: Dict[str, Any] = {}
_CACHED_COLLECT_AT: float = 0.0
_CACHED_COLLECT_TTL = 300.0  # 5 分钟



def _verify_sql(sql: str) -> Dict[str, Any]:
    if not sql or not sql.strip():
        return {"ok": False, "error": "SQL 为空", "plan": []}
    head = sql.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        return {"ok": False, "error": "仅允许 SELECT/WITH 查询", "plan": []}
    if "?" in sql:
        return {"ok": False, "error": "SQL 含 ? 占位符", "plan": []}
    try:
        rows = _run_sync(_async_query(
            *_conn_args(),
            "EXPLAIN (COSTS OFF) " + sql.strip(),
        ))
        plan_lines = []
        cost_note = ""
        for r in rows:
            p = r.get("QUERY PLAN") or r.get("query plan") or ""
            plan_lines.append(str(p))
            if "cost=" in str(p) and not cost_note:
                cost_note = str(p)
        return {"ok": True, "plan": plan_lines, "cost_note": cost_note, "error": ""}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "plan": []}


@router.post("/verify")
def verify(req: VerifyRequest):
    return _verify_sql(req.rewritten_sql)


@router.post("/tools/verify")
def tools_verify(req: VerifyRequest):
    return _verify_sql(req.rewritten_sql)


# ─── /health ───

REAL_NODES = _parse_nodes(os.environ.get("GAUSSDB_NODES", ""))
if not REAL_NODES and _conn_kwargs.get("host"):  # 未配置节点列表时，退化为 DSN 主节点单节点拓扑
    REAL_NODES = [{"host": _conn_kwargs["host"], "port": _conn_kwargs["port"],
                   "node_id": "dn_6001", "node_type": "DN", "alias": "dn1"}]
REAL_CLUSTER_ID = os.environ.get("GAUSSDB_CLUSTER_ID", "")


async def _async_query(host, port, user, password, database, sql):
    import async_gaussdb
    conn = await async_gaussdb.connect(host=host, port=port, user=user, password=password,
                                       database=database, ssl="prefer", timeout=8)
    try:
        rows = await conn.fetch(sql)
        return [dict(r) for r in rows] if rows else []
    finally:
        await conn.close()


def _real_topology() -> Dict[str, Any]:
    info = {
        "nodes": [], "version": "unknown", "cluster_id": REAL_CLUSTER_ID,
        "cluster_name": "GaussDB 507 主备集群", "primary_ip": None,
    }
    try:
        for nd in REAL_NODES:
            try:
                rows = _run_sync(_async_query(
                    nd["host"], nd["port"], _conn_kwargs["user"], _conn_kwargs["password"], _conn_kwargs["database"],
                    "SELECT pg_is_in_recovery() AS rec, inet_server_addr() AS ip, inet_server_port() AS port, version() AS ver",
                ))
            except Exception:
                continue
            if not rows:
                continue
            d = rows[0]
            rec = bool(d.get("rec"))
            role = "Standby" if rec else "Primary"
            if not rec and not info["primary_ip"]:
                info["primary_ip"] = str(d.get("ip") or nd["host"])
            if not rec:
                info["version"] = (str(d.get("ver") or "").split(" compiled at")[0] or info["version"])
                try:
                    repl = _run_sync(_async_query(
                        nd["host"], nd["port"], _conn_kwargs["user"], _conn_kwargs["password"], _conn_kwargs["database"],
                        "SELECT application_name, client_addr, state, sync_state FROM pg_stat_replication",
                    ))
                    repl_map = {str(r.get("client_addr")): r for r in repl if r.get("client_addr")}
                except Exception:
                    repl_map = {}
                for rnode in REAL_NODES:
                    if rnode["host"] in repl_map:
                        rnode["status"] = repl_map[rnode["host"]].get("state", "Streaming")
                        rnode["sync_state"] = repl_map[rnode["host"]].get("sync_state", "Quorum")
            info["nodes"].append({
                "node_id": nd["node_id"], "node_type": nd["node_type"], "node_role": role,
                "node_status": nd.get("status", "Normal"), "host_ip": str(d.get("ip") or nd["host"]),
                "host_id": f"host-{nd['node_id']}", "sync_state": nd.get("sync_state", ""),
            })
    except Exception as e:
        info["error"] = str(e)[:150]
    return info


# ─── /anchor ───

_SQL = {
    "sessions": "SELECT state, count(*) AS cnt FROM pg_stat_activity GROUP BY state",
    "active_sessions": (
        "SELECT pid, usename, state, waiting, enqueue, xact_start, query_start, query "
        "FROM pg_stat_activity WHERE pid > 0 AND state = 'active' "
        "AND query NOT LIKE '%pg_stat_activity%' ORDER BY query_start LIMIT 20"
    ),
    "wait_status": (
        "SELECT node_name, thread_name, wait_status, wait_event, locktag, lockmode, block_sessionid "
        "FROM pg_thread_wait_status WHERE wait_status IS NOT NULL AND wait_status != 'none' "
        "AND wait_status != 'wait cmd' ORDER BY wait_status LIMIT 30"
    ),
    "asp": (
        "SELECT event, count(*) AS cnt FROM dbe_perf.local_active_session "
        "WHERE sample_time > now() - interval '1 hour' GROUP BY event ORDER BY cnt DESC LIMIT 10"
    ),
    "top_sql": (
        "SELECT unique_sql_id, n_calls, total_elapse_time, "
        "total_elapse_time / NULLIF(n_calls,0) AS avg_elapse_us "
        "FROM dbe_perf.statement WHERE n_calls > 0 ORDER BY total_elapse_time DESC LIMIT 10"
    ),
    "slow_sql_history": (
        "SELECT unique_query_id, execution_time, cpu_time, data_io_time, "
        "query_plan, start_time, query "
        "FROM dbe_perf.statement_history WHERE is_slow_sql = true "
        "ORDER BY start_time DESC LIMIT 5"
    ),
    "locks": (
        "SELECT locktype, mode, granted, count(*) AS cnt FROM dbe_perf.global_locks "
        "GROUP BY locktype, mode, granted ORDER BY cnt DESC LIMIT 15"
    ),
    "replication": (
        "SELECT application_name, state, sync_state, "
        "pg_xlog_location_diff(sender_sent_location, receiver_replay_location) AS replay_lag_bytes, "
        "pg_xlog_location_diff(sender_sent_location, receiver_write_location) AS write_lag_bytes "
        "FROM pg_stat_replication"
    ),
    "table_stats": (
        "SELECT schemaname, relname, n_live_tup, last_analyze, last_autoanalyze "
        "FROM pg_stat_user_tables WHERE n_live_tup > 100000 OR last_analyze IS NOT NULL "
        "ORDER BY n_live_tup DESC LIMIT 10"
    ),
    "connections": (
        "SELECT count(*) AS total, "
        "(SELECT setting::int FROM pg_settings WHERE name='max_connections') AS max_conn "
        "FROM pg_stat_activity"
    ),
    "long_tx": (
        "SELECT count(*) AS cnt FROM pg_stat_activity "
        "WHERE xact_start IS NOT NULL AND now() - xact_start > interval '5 minutes'"
    ),
}


async def _collect_async() -> Dict[str, List[dict]]:
    import async_gaussdb
    conn = await async_gaussdb.connect(**_conn_kwargs)
    raw: Dict[str, List[dict]] = {}
    try:
        for name, sql in _SQL.items():
            try:
                rows = await conn.fetch(sql)
                raw[name] = [dict(r) for r in rows] if rows else []
            except Exception:
                raw[name] = []
        return raw
    finally:
        await conn.close()


def _tx_duration_ms(xact_start) -> int:
    try:
        if isinstance(xact_start, datetime.datetime):
            return int((datetime.datetime.now(xact_start.tzinfo) - xact_start).total_seconds() * 1000)
        return int((datetime.datetime.now() - datetime.datetime.fromisoformat(str(xact_start))).total_seconds() * 1000)
    except Exception:
        return 0


def _plan_first_node(query_plan: Optional[str]) -> str:
    if not query_plan:
        return ""
    text = str(query_plan)
    if re.search(r"Seq Scan", text):
        return "seq_scan"
    if re.search(r"Index (Only )?Scan", text):
        return "index_scan"
    m = re.search(r"^[ \t]*([A-Z][\w ]*?)\s+\(cost=", text, re.M)
    if m:
        return m.group(1).strip().lower().replace(" ", "_")
    return ""


def _plan_signature(query_plan: Optional[str], query_text: Optional[str] = "") -> Dict[str, Any]:
    out = {"has_seq_scan": False, "seq_scan_tables": [], "has_big_agg": False,
           "has_big_sort": False, "has_distinct": False, "has_nested_loop": False,
           "top_cost": "", "referenced_tables": [], "plan_size": 0}
    plan = str(query_plan or "")
    out["plan_size"] = len(plan)
    if not plan:
        return out
    for m in re.finditer(r"Seq Scan on (\w+)", plan):
        out["has_seq_scan"] = True
        if m.group(1) not in out["seq_scan_tables"]:
            out["seq_scan_tables"].append(m.group(1))
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
    if re.search(r"Nested Loop", plan):
        out["has_nested_loop"] = True
    m = re.search(r"\(cost=([\d.]+)\.\.([\d.]+)", plan)
    if m:
        out["top_cost"] = f"{m.group(1)}..{m.group(2)}"
    if query_text:
        qt = str(query_text).lower()
        for m in re.finditer(r"(?:from|join)\s+([a-z_][\w.]*)", qt):
            t = m.group(1).split(".")[-1]
            if t and len(t) < 64 and t not in out["referenced_tables"]:
                out["referenced_tables"].append(t)
        if re.search(r"count\s*\(\s*distinct", qt):
            out["has_distinct"] = True
    return out



def _fix_proposals(slow_sqls: List[dict], tables: List[dict]) -> List[dict]:
    proposals: List[dict] = []
    big_tables = {t.get("relname", "").lower(): t for t in tables if (t.get("n_live_tup") or 0) > 1_000_000}

    def add(atype, target, command, desc, risk="L2", basis=""):
        proposals.append({"action_type": atype, "target": target, "command": command,
                          "description": desc, "risk": risk, "basis": basis})

    for s in slow_sqls:
        sig = s.get("signature", {})
        tbl_refs = sig.get("referenced_tables", [])
        big_refs = [t for t in tbl_refs if t in big_tables]
        qid = s.get("unique_query_id", "?")
        sid = f"sql-{qid}"
        if sig.get("has_seq_scan") and big_refs:
            for tbl in big_refs[:2]:
                add("create_index", tbl,
                    f"CREATE INDEX /* 针对 {tbl} 的过滤/连接列创建索引 */",
                    f"慢SQL(sid) 对 {tbl} 全表扫描（大表），建议在 WHERE/连接列建索引或复检现有索引适用性",
                    risk="L3", basis=f"plan: Seq Scan on {tbl}")
        if sig.get("has_big_agg") or sig.get("has_distinct"):
            add("materialized_view", ",".join(big_refs[:2]) or "aggregate",
                "CREATE MATERIALIZED VIEW /* 预聚合中间结果，周期性刷新 */",
                "存在大表上的聚合/COUNT(DISTINCT)，建议物化视图预聚合避免全表重复扫描",
                risk="L3", basis="plan: GroupAggregate/HashAggregate 或 COUNT(DISTINCT)")
        if big_refs and any((t.get("stat_stale_days") or 0) > 7 for t in big_tables.values() if t.get("relname", "").lower() in big_refs):
            for tbl in big_refs[:2]:
                add("analyze", tbl, f"ANALYZE {tbl};",
                    f"大表 {tbl} 统计信息可能过期（>7天），先重建统计信息修正执行计划",
                    risk="L2", basis="stat_stale_days>7")
        if sig.get("has_big_sort"):
            add("rewrite_hint", sid,
                "改写 SQL：减少 ORDER BY / GROUP BY 无谓排序列，或用索引消除排序",
                "大量数据排序导致高耗时，建议改写 SQL 消除/降级排序",
                risk="L2", basis="plan: big Sort")
        if not big_refs and sig.get("top_cost"):
            add("analyze", ",".join(tbl_refs[:2]) or "cluster",
                "ANALYZE;", "存在高代价SQL但未识别出大表，先全库ANALYZE刷新统计信息",
                risk="L2", basis=f"top_cost={sig.get('top_cost')}")

    seen = set()
    dedup = []
    for p in proposals:
        key = (p["action_type"], p["target"])
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup[:12]


def _to_facts(raw: Dict[str, List[dict]], cluster_id: str, changes: List[dict]) -> tuple:
    facts: List[dict] = []
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def add(path, value, obj_id, source):
        facts.append({"path": path, "value": value, "obj_id": obj_id, "ts": ts, "source": source})

    active_cnt = 0
    for r in raw.get("sessions", []):
        if r.get("state") == "active":
            active_cnt = r.get("cnt", 0)
        if r.get("state") in ("active", "idle in transaction"):
            add("session.state", r["state"], f"cluster:{r['state']}", "pg_stat_activity")
    add("cluster.active_sessions", active_cnt, cluster_id, "pg_stat_activity")

    for s in raw.get("active_sessions", []):
        sid = f"sess-{s['pid']}"
        add("session.state", "active", sid, "pg_stat_activity")
        if s.get("waiting"):
            add("session.wait_event", s.get("enqueue") or "wait", sid, "pg_stat_activity")
        if s.get("xact_start"):
            add("transaction.duration_ms", _tx_duration_ms(s["xact_start"]), f"tx-{s['pid']}", "pg_stat_activity")

    for w in raw.get("wait_status", []):
        sid = f"sess-{w.get('block_sessionid') or w.get('thread_name')}"
        add("session.wait_event", w.get("wait_event") or w.get("wait_status"), sid, "pg_thread_wait_status")
        add("session.wait_status", w.get("wait_status"), sid, "pg_thread_wait_status")

    asp_total = 0
    for a in raw.get("asp", []):
        event = a.get("event") or "none"
        cnt = a.get("cnt", 0) or 0
        asp_total += cnt
        add("session.wait_event", event, f"asp:{event}", "dbe_perf.local_active_session")
    if asp_total:
        add("cluster.asp_samples_1h", asp_total, cluster_id, "dbe_perf.local_active_session")

    slow_cnt = 0
    for r in raw.get("top_sql", []):
        avg_ms = (r.get("avg_elapse_us") or 0) / 1000.0
        if avg_ms > 1000:
            slow_cnt += 1
        add("sql.avg_elapsed_ms", round(avg_ms, 1), f"sql-{r.get('unique_sql_id')}", "dbe_perf.statement")
    add("cluster.slow_sql_count", slow_cnt, cluster_id, "dbe_perf.statement")

    slow_durations = []
    slow_sql_sig = []
    for sh in raw.get("slow_sql_history", []):
        exec_ms = (sh.get("execution_time") or 0) / 1000.0
        slow_durations.append(exec_ms)
        sid = f"sql-{sh.get('unique_query_id')}"
        add("sql.slow_exec_ms", round(exec_ms, 1), sid, "dbe_perf.statement_history")
        add("sql.slow_start_time", str(sh.get("start_time") or ""), sid, "dbe_perf.statement_history")
        pt = _plan_first_node(sh.get("query_plan"))
        if pt:
            add("sql.plan_type", pt, sid, "dbe_perf.statement_history")
        if sh.get("cpu_time"):
            add("sql.cpu_time_ms", round(sh["cpu_time"] / 1000.0, 1), sid, "dbe_perf.statement_history")
        sig = _plan_signature(sh.get("query_plan"), sh.get("query"))
        if sh.get("query"):
            add("sql.query", str(sh.get("query")), sid, "dbe_perf.statement_history")
        if sig.get("seq_scan_tables"):
            add("sql.seq_scan_tables", ",".join(sig["seq_scan_tables"]), sid, "dbe_perf.statement_history")
        if sig.get("has_big_agg") or sig.get("has_distinct"):
            add("sql.agg_flag", "big_agg" if sig.get("has_big_agg") else "count_distinct", sid, "dbe_perf.statement_history")
        if sig.get("top_cost"):
            add("sql.top_cost", sig["top_cost"], sid, "dbe_perf.statement_history")
        slow_sql_sig.append({"unique_query_id": sh.get("unique_query_id"),
                             "query": sh.get("query"), "signature": sig,
                             "exec_ms": round(exec_ms, 1)})
    if slow_durations:
        add("cluster.slow_sql_avg_ms", round(sum(slow_durations) / len(slow_durations), 1),
            cluster_id, "dbe_perf.statement_history")

    for r in raw.get("long_tx", []):
        if r.get("cnt", 0) > 0:
            add("transaction.duration_ms", 600000, "tx:long", "pg_stat_activity")
            add("cluster.long_tx_count", r["cnt"], cluster_id, "pg_stat_activity")

    wait_locks = 0
    for lk in raw.get("locks", []):
        if not lk.get("granted", True):
            wait_locks += 1
    if wait_locks:
        add("session.wait_event", "lock", "lock:waiting", "dbe_perf.global_locks")
        add("cluster.wait_lock_count", wait_locks, cluster_id, "dbe_perf.global_locks")

    for r in raw.get("replication", []):
        app = r.get("application_name", "repl")
        lag_bytes = r.get("replay_lag_bytes", 0) or 0
        write_bytes = r.get("write_lag_bytes", 0) or 0
        try:
            lag_mb = round(int(lag_bytes) / 1048576, 2)
            write_mb = round(int(write_bytes) / 1048576, 2)
        except (ValueError, TypeError):
            lag_mb = write_mb = 0
        add("replication.replay_lag_mb", lag_mb, app, "pg_stat_replication")
        add("replication.write_lag_mb", write_mb, app, "pg_stat_replication")
        # ★ FIX(2026-09-12)：原实现是 `int(lag_mb * 500)` —— **凭空的换算**（无任何实测依据，
        #    系数 500 无出处），属于"编造数据点"。GaussDB 507 上拿不到可用的延迟毫秒：
        #    gs_standby_read_delay() 只能在备机执行；gs_hadr_local_replay_delay_stat() 在此
        #    环境全为 0 占位；pg_stat_replication 只有 LSN 位置。故**删除该伪造事实**，
        #    统一以字节为准（规则判据已同步改为字节 + 阈值 153600）。
        add("replication.sync_state", r.get("sync_state"), app, "pg_stat_replication")

    for t in raw.get("table_stats", []):
        tbl = f"tbl-{t.get('relname', 'unknown')}"
        add("table.rows", t.get("n_live_tup", 0), tbl, "pg_stat_user_tables")
        la = t.get("last_analyze")
        if la:
            add("table.last_analyze", str(la), tbl, "pg_stat_user_tables")
            try:
                la_dt = la if isinstance(la, datetime.datetime) else datetime.datetime.fromisoformat(str(la))
                stale_days = (datetime.datetime.now(la_dt.tzinfo) - la_dt).days
                add("table.stat_stale_days", stale_days, tbl, "pg_stat_user_tables")
            except Exception:
                pass

    for r in raw.get("connections", []):
        total, maxc = r.get("total", 0), r.get("max_conn", 0)
        if maxc:
            add("node.connection_usage_ratio", round(total * 100.0 / maxc, 1), cluster_id, "pg_settings")

    for c in changes:
        if c.get("id"):
            ctype = str(c.get("type") or "Change")
            add("change.type", ctype.replace("Change", "").upper() or "DDL", f"change-{c['id']}", "本体库Change实例")
            if c.get("at"):
                add("change.time", str(c["at"]), f"change-{c['id']}", "本体库Change实例")

    return facts, slow_sql_sig


# ── NEW: /tools/ontology_collect_guide ──

class ToolsCollectRequest(BaseModel):
    cluster_oid: str = "79b62ee45b9e401fb06ca86941d5f419in14"
    sections: List[str] = []  # 只采集指定的 section（空=全量）



@router.post("/tools/collect")
def tools_collect(req: ToolsCollectRequest):
    global _CACHED_COLLECT_AT   # ★ 见 _get_or_refresh_cache 注释：缺 global 会静默失效
    try:
        raw = _run_sync(_collect_async())
    except Exception as e:
        raise HTTPException(502, f"GaussDB 采集失败: {e}")

    # 如果指定了 sections，只保留这些 section
    if req.sections:
        raw = {k: v for k, v in raw.items() if k in req.sections}

    changes = get_recent_changes()
    facts, slow_sql_sig = _to_facts(raw, req.cluster_oid, changes)
    big_tables = []
    for t in raw.get("table_stats", []):
        big_tables.append({"relname": t.get("relname"), "n_live_tup": t.get("n_live_tup"),
                           "last_analyze": t.get("last_analyze")})
    fix_proposals = _fix_proposals(slow_sql_sig, big_tables)
    slow_sql_detail = []
    for s in slow_sql_sig:
        slow_sql_detail.append({"unique_query_id": s.get("unique_query_id"),
                                "exec_ms": s.get("exec_ms"), "query": s.get("query"),
                                "signature": s.get("signature", {})})
    real_topo = {"nodes": [], "version": "unknown", "cluster_id": req.cluster_oid,
                 "cluster_name": "", "primary_ip": ""}
    try:
        real_topo = _real_topology()
        ts_now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for n in real_topo.get("nodes", []):
            facts.append({"path": "node.id", "value": n["node_id"], "obj_id": n["node_id"],
                          "ts": ts_now, "source": "pg_is_in_recovery/pg_stat_replication"})
            facts.append({"path": "node.role", "value": n["node_role"], "obj_id": n["node_id"],
                          "ts": ts_now, "source": "pg_is_in_recovery/pg_stat_replication"})
            facts.append({"path": "node.status", "value": n["node_status"], "obj_id": n["node_id"],
                          "ts": ts_now, "source": "pg_stat_replication"})
            facts.append({"path": "node.host", "value": n["host_ip"], "obj_id": n["node_id"],
                          "ts": ts_now, "source": "inet_server_addr"})
        facts.append({"path": "cluster.id", "value": real_topo["cluster_id"], "obj_id": real_topo["cluster_id"],
                      "ts": ts_now, "source": "gaussdb_instance"})
        facts.append({"path": "cluster.version", "value": real_topo["version"], "obj_id": real_topo["cluster_id"],
                      "ts": ts_now, "source": "version()"})
        facts.append({"path": "cluster.primary_ip", "value": real_topo.get("primary_ip") or "",
                      "obj_id": real_topo["cluster_id"], "ts": ts_now, "source": "pg_is_in_recovery"})
    except Exception:
        pass

    # 写入内存缓存
    import time
    _CACHED_COLLECT_RESULT.update({
        "raw": raw,
        "facts": facts,
        "clustertopo": real_topo,
        "slow_sql_detail": slow_sql_detail,
        "fix_proposals": fix_proposals,
    })
    _CACHED_COLLECT_AT = time.time()

    return {
        "cluster_oid": real_topo["cluster_id"],
        "fact_count": len(facts),
        "facts": facts,
        "clustertopo": real_topo,
        "fix_proposals": fix_proposals,
        "slow_sql_detail": slow_sql_detail,
        "collected_at": datetime.datetime.now().isoformat(),
    }


# ── T0 /tools/gather —— 四层采集统一入口（v6 新增） ──

class ToolsGatherRequest(BaseModel):
    layers: List[str] = ["gaussdb"]
    sections: List[str] = []
    verify_sql: str = ""
    cluster_oid: str = ""
    instance_id: str = ""


@router.post("/tools/gather")
async def tools_gather(req: ToolsGatherRequest):
    """四层采集统一入口

    支持层: gaussdb, os, storage, tpops
    返回: 各层采集结果 + SQL校验 + 汇总
    """
    # ★ 缺 global 会让下方 `_CACHED_COLLECT_AT = ...` 只写局部变量，
    #   模块全局恒为 0.0 ⇒ 缓存永远"过期"（详见 _get_or_refresh_cache 注释）。
    global _CACHED_COLLECT_AT
    import time as _time_mod
    import asyncio as _asyncio

    start = _time_mod.time()

    # 1) 各层采集（异步执行，不阻塞事件循环）
    import functools as _ft

    probe_kwargs = {}
    if req.sections:
        probe_kwargs["sections"] = req.sections
    if req.instance_id:
        probe_kwargs["instance_id"] = req.instance_id

    loop = _asyncio.get_event_loop()
    layer_results = await loop.run_in_executor(
        None, _ft.partial(collect_layers, req.layers, **probe_kwargs)
    )

    # 2) SQL 校验（可选）
    sql_verify_result = None
    if req.verify_sql:
        try:
            gp = get_probe("gaussdb")
            if gp:
                sql_verify_result = gp.verify_sql(req.verify_sql)
        except Exception as e:
            sql_verify_result = {"ok": False, "error": str(e)[:200], "plan": ""}

    # 3) 汇总
    all_facts = []
    all_alerts = []
    layer_details = {}
    layer_errors = {}
    section_errors: Dict[str, Any] = {}

    for layer, result in layer_results.items():
        if result.error:
            layer_errors[layer] = result.error
        else:
            all_facts.extend(result.facts)
            all_alerts.extend(result.alerts)

        # ★ D2：段级失败不再静默 —— 透出到响应体
        sec_err = getattr(result, "section_errors", None) or {}
        if sec_err:
            section_errors[layer] = sec_err

        layer_details[layer] = {
            "mode": result.mode,
            "fact_count": len(result.facts),
            "alert_count": len(result.alerts),
            "elapsed_ms": result.elapsed_ms,
            "sections_failed": sorted(sec_err.keys()) if sec_err else [],
        }

    # ★ D3 修复：gather 必须返回 slow_sql_detail / fix_proposals，
    # 否则 propose_actions 永远零输出（原实测：仅传 facts → 0 条建议）。
    slow_sql_detail: List[dict] = []
    fix_proposals: List[dict] = []
    try:
        g_raw = (layer_results.get("gaussdb").raw if layer_results.get("gaussdb") else {}) or {}
        rows = g_raw.get("slow_sql_detail") or g_raw.get("slow_sql") or []
        for sh in rows:
            sig = _plan_signature(sh.get("query_plan"), sh.get("query"))
            slow_sql_detail.append({
                "unique_query_id": sh.get("unique_query_id"),
                "exec_ms": round((sh.get("execution_time") or 0) / 1000.0, 1),
                "db_ms": round((sh.get("db_time") or 0) / 1000.0, 1),
                "cpu_ms": round((sh.get("cpu_time") or 0) / 1000.0, 1),
                "query": sh.get("query"),
                "start_time": str(sh.get("start_time") or ""),
                "signature": sig,
            })
        if slow_sql_detail:
            big_tables = []
            for t in (g_raw.get("table_stats") or []):
                if (t.get("n_live_tup") or 0) <= 1_000_000:
                    continue
                stale = None
                la = t.get("last_analyze")
                if la:
                    try:
                        la_dt = la if isinstance(la, datetime.datetime) \
                            else datetime.datetime.fromisoformat(str(la).replace("+00:00", ""))
                        stale = (datetime.datetime.now() - la_dt).days
                    except Exception:
                        stale = None
                big_tables.append({"relname": t.get("relname"),
                                   "n_live_tup": t.get("n_live_tup"),
                                   "stat_stale_days": stale})
            fix_proposals = _fix_proposals(slow_sql_detail, big_tables)
    except Exception as _e_d3:
        section_errors.setdefault("gaussdb", {})["__slow_sql_detail__"] = str(_e_d3)[:200]

    # 写入缓存（兼容已有下游）
    _CACHED_COLLECT_RESULT.update({
        "raw": {l: r.raw for l, r in layer_results.items()},
        "facts": all_facts,
        "alerts": all_alerts,
        "layer_results": layer_details,
        "slow_sql_detail": slow_sql_detail,
        "fix_proposals": fix_proposals,
    })
    _CACHED_COLLECT_AT = _time_mod.time()

    elapsed = int((_time_mod.time() - start) * 1000)

    # ── 真实拓扑（含节点 IP）+ IP 索引 ──
    # 背景：用户常按 IP 提问（如「192.0.2.12 这套库有什么问题」），但 TPOPS / Dorado / 告警
    # 数据只带 instance_id 与 dn 组件 id，不含 IP。这里用 _real_topology()
    # （inet_server_addr / pg_is_in_recovery / pg_stat_replication）取真实节点 IP，
    # 并建立 ip → {集群/实例/节点/角色} 索引，使按 IP 的提问可被精确定位。
    topo: Dict[str, Any] = {"nodes": [], "cluster_id": REAL_CLUSTER_ID,
                            "cluster_name": "", "primary_ip": ""}
    try:
        topo = _real_topology()
    except Exception as _e_topo:
        layer_errors = list(layer_errors or [])
        layer_errors.append({"layer": "topology", "error": str(_e_topo)[:200]})

    ts_topo = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ip_index: Dict[str, Any] = {}
    for n in topo.get("nodes", []):
        nip = n.get("host_ip") or ""
        nid = n.get("node_id", "")
        all_facts.append({"path": "node.host", "value": nip, "obj_id": nid,
                          "obj_type": "node", "ts": ts_topo, "source": "inet_server_addr"})
        all_facts.append({"path": "node.ip", "value": nip, "obj_id": nid,
                          "obj_type": "node", "ts": ts_topo, "source": "inet_server_addr"})
        if not nip:
            continue
        ip_index[nip] = {
            "cluster_id": topo.get("cluster_id", ""),
            "cluster_name": topo.get("cluster_name", ""),
            "node_id": nid,
            "node_role": n.get("node_role", ""),
            "node_status": n.get("node_status", ""),
            "is_primary": nip == (topo.get("primary_ip") or ""),
        }
    if topo.get("cluster_id"):
        all_facts.append({"path": "cluster.id", "value": topo.get("cluster_id", ""),
                          "obj_id": topo.get("cluster_id", ""), "obj_type": "instance",
                          "ts": ts_topo, "source": "gaussdb_instance"})
    if topo.get("primary_ip"):
        all_facts.append({"path": "cluster.primary_ip", "value": topo.get("primary_ip", ""),
                          "obj_id": topo.get("cluster_id", ""), "obj_type": "instance",
                          "ts": ts_topo, "source": "pg_is_in_recovery"})

    # ★ 集群级事实：NVMe/多路径设备的 IO 调度器一致性
    # （规则 r_st_io_scheduler_inconsistency 依赖 cluster.io_scheduler_consistent）
    # 注意：OS 探针已在 `_cluster_facts()` 里做跨节点聚合并产出该路径（它能看到全部
    # 节点的原始 /sys 数据，比在此处从事实反推更准）。这里**仅当 OS 层未产出时**才兜底，
    # 避免同一 fact 出现两个值（多值语义下会导致判定结果不确定）。
    try:
        _already = any(f.get("path") == "cluster.io_scheduler_consistent" for f in all_facts)
        if not _already:
            _sched: Dict[str, set] = {}
            for _f in all_facts:
                _p = _f.get("path", "")
                if _p.startswith("storage.io_scheduler."):
                    _dev = _p.split(".", 2)[2]
                    _sched.setdefault(_dev, set()).add(str(_f.get("value")))
            _sched_items = {d: v for d, v in _sched.items()
                            if d.startswith(("nvme", "ultrapath"))}
            if _sched_items:
                _vals = {x for v in _sched_items.values() for x in v}
                all_facts.append({"path": "cluster.io_scheduler_consistent",
                                  "value": len(_vals) <= 1,
                                  "obj_id": topo.get("cluster_id", ""), "obj_type": "instance",
                                  "ts": ts_topo, "source": "os:/sys/block/*/queue/scheduler"})
    except Exception as _e_sched:
        section_errors.setdefault("cluster", {})["__io_scheduler_summary__"] = str(_e_sched)[:200]

    out_g = {
        "layers": req.layers,
        "collected_at": datetime.datetime.now().isoformat(),
        "elapsed_ms": elapsed,
        "layer_details": layer_details,
        "layer_errors": layer_errors if layer_errors else None,
        "section_errors": section_errors if section_errors else None,
        "total_facts": len(all_facts),
        "total_alerts": len(all_alerts),
        "all_facts": all_facts,
        "all_alerts": all_alerts,
        "topology": topo,
        "ip_index": ip_index,
        # D3：propose_actions 的上游契约（原缺失导致慢SQL场景零建议）
        "slow_sql_detail": slow_sql_detail,
        "fix_proposals": fix_proposals,
        "sql_verify": {
            "enabled": bool(req.verify_sql),
            "result": sql_verify_result,
        },
        "mode": "probes_v6",
    }
    # ★ M1.6（信用卡跨层运维项目）：L1/L2 两层的**对象身份**由探针段承载。
    #   facts 只带 (path,value,obj_id,obj_type)，表达不了「有哪些对象、彼此什么关系」；
    #   而 App→Container→Host / AppInstance→JdbcDataSource→GaussDB 的拓扑正是本项目的
    #   建模对象 ⇒ 在此把两层的应用拓扑段提升到响应顶层（**纯新增键**，既有字段一字不动）。
    #   两个探针各自只声明自己那半：
    #     container 层 → raw.container_topology = {hosts:[], containers:[]}
    #     app       层 → raw.app_topology       = {apps,app_instances,app_endpoints,datasources}
    _c_raw = ((layer_results.get("container").raw
               if layer_results.get("container") else {}) or {})
    _a_raw = ((layer_results.get("app").raw
               if layer_results.get("app") else {}) or {})
    out_g["container_topology"] = _c_raw.get("container_topology") or {}
    out_g["app_topology"] = _a_raw.get("app_topology") or {}
    if section_errors:
        out_g["hint"] = ("部分采集段执行失败（见 section_errors / all_alerts）—— "
                         "这些段的事实为 0 是**采集失败**，不是真实空结果，请勿反复重采。")
    return _loop_guard("gather", req, out_g)


# ── T0b /tools/gather_bounded —— LLM 专用的有界采集入口 ──
#
# 为什么必须单独一个端点（2026-09-19 契约体检 C2）：
#   /tools/gather 返回**逐条事实**，四层采集实测 3,167 条 ≈ 16.6 万 tokens；
#   ReAct 每轮迭代都会重发历史 ⇒ 上下文必然爆掉、模型网关读超时。
#   而 gather 无法就地改成有界：仓内 **45 个文件**（V3 的本体裁剪、部署验收、
#   取证/bench 脚本）都按逐条形状读 `all_facts`，改默认值会把它们
#   静默打断 —— 拿到 0 条事实后得出"没有数据"的假结论，正是本项目反复
#   踩过的坑（见 contract-drift-repair）。
#   所以：**原始端点零改动**，另开一个默认即有界的端点给 LLM 用。
#   Dify 侧的 provider 仍叫 `gather`（提示词无需改），只把 schema 的 path
#   指到这里 —— 模型拿不到无界形状，不依赖它自觉。
#
# 与 /onto/collect（V3 的本体驱动有界采集）的分工：
#   这里**不做本体裁剪**（v2 侧没有本体），只做「表示压缩」：
#   同路径聚合为一条、全局常量提升、长串截断并记账。信息不减少。
@router.post("/tools/gather_bounded")
async def tools_gather_bounded(req: ToolsGatherRequest):
    """有界采集：逐路径聚合的事实 + 显式丢弃量（给 LLM 读）"""
    # 复用同一采集核（不重复实现），再压缩其返回体
    raw = await tools_gather(req)
    if not isinstance(raw, dict) or "all_facts" not in raw:
        # 熔断/错误体等非采集成功形态：原样返回，不掩盖
        return raw
    facts = raw.get("all_facts") or []
    by_path, meta = compress_facts(facts)
    out = {k: v for k, v in raw.items() if k != "all_facts"}
    out["facts_by_path"] = by_path
    out.update(meta)
    out["facts_schema"] = ("逐路径聚合：{路径: {n:该路径事实条数, v:[不同取值…], "
                           "o:[对象ID…], ot:[对象类型…], s:来源, t:时间}}")
    out["bounded_by"] = "gather_bounded"
    # slow_sql_detail 单条含 query 全文，条数多时同样会爆；截断并记账
    _ss = out.get("slow_sql_detail")
    if isinstance(_ss, list) and len(_ss) > _BOUNDED_MAX_SLOW_SQL:
        out["slow_sql_detail"] = _ss[:_BOUNDED_MAX_SLOW_SQL]
        out["slow_sql_detail_truncated"] = len(_ss) - _BOUNDED_MAX_SLOW_SQL
    return out


# ── T1 /tools/ontology —— neo4j_query ──

class ToolsPlanRequest(BaseModel):
    sql: str = ""
    sql_id: str = ""


@router.post("/tools/plan")
def tools_plan(req: ToolsPlanRequest):
    q = req.sql.strip() or ""
    if not q:
        try:
            raw = _run_sync(_collect_async())
            sh = raw.get("slow_sql_history", [])
            if sh:
                q = str(sh[0].get("query") or "")
                qid = sh[0].get("unique_query_id")
            else:
                return {"success": False, "error": "无 SQL 可分析", "plan": [], "signature": {}}
        except Exception as e:
            return {"success": False, "error": f"采集失败: {str(e)[:200]}", "plan": [], "signature": {}}
    else:
        qid = req.sql_id
    try:
        rows = _run_sync(_async_query(
            *_conn_args(),
            "EXPLAIN (COSTS OFF) " + q,
        ))
        plan_lines = []
        for r in rows:
            p = r.get("QUERY PLAN") or r.get("query plan") or ""
            plan_lines.append(str(p))
        plan_text = "\n".join(plan_lines)
        sig = _plan_signature(plan_text, q)
        return {"success": True, "sql_id": qid, "sql": q,
                "plan": plan_lines, "plan_type": _plan_first_node(plan_text),
                "signature": sig}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "plan": [], "signature": {}}


# ── T5 /tools/runbooks —— runbook_search ──

class ToolsExecuteRequest(BaseModel):
    action_id: str = ""
    target_id: str = ""
    params: Dict[str, Any] = {}
    approval_roles: List[str] = []


_ACTION_DEFS = {
    "a_analyze": {"risk": "L2", "roles": ["oncall"], "name": "重建统计信息",
                  "cmd": "ANALYZE {table_name};"},
    "a_sql_rewrite": {"risk": "L2", "roles": ["oncall"], "name": "SQL 改写建议",
                      "cmd": "应用侧改写 SQL"},
    "a_sql_throttle": {"risk": "L2", "roles": ["oncall"], "name": "SQL 限流",
                       "cmd": "ALTER SYSTEM THROTTLE ..."},
    "a_create_index": {"risk": "L3", "roles": ["oncall", "business"], "name": "创建索引",
                       "cmd": "CREATE INDEX idx_{table_name}_{column} ON {table_name}({column});"},
    "a_kill_session": {"risk": "L3", "roles": ["oncall", "business"], "name": "终止会话",
                       "cmd": "SELECT pg_terminate_backend({pid});"},
    "a_switchover": {"risk": "L3", "roles": ["oncall", "business"], "name": "主备切换",
                     "cmd": "cm_ctl switchover ..."},
    "a_param_change": {"risk": "L3", "roles": ["oncall", "business"], "name": "参数变更",
                       "cmd": "ALTER SYSTEM SET {param_name} = {param_value};"},
    "a_query_topology": {"risk": "L1", "roles": [], "name": "查询本体拓扑", "cmd": "ontology: 对象导航"},
    "a_query_slow_sql": {"risk": "L1", "roles": [], "name": "查询慢SQL", "cmd": "dbe_perf topN 慢SQL"},
    # ══════════════════════════════════════════════════════════════════════
    # ★ step3 补全（2026-09-11）：存储 / Dorado / 容灾域动作
    #   原 _ACTION_DEFS 只有 9 个数据库域动作，存储类规则命中后 propose_actions
    #   生成的 action_id 在 execute 侧**查不到**（"未知 Action"）⇒ L1/L2/L3 断链。
    #   风险分级依据：L1=只读排查（无变更）；L2=可回滚变更/在线操作；
    #                 L3=高风险（涉及路径切换、硬件、网络改动，需双人复核）。
    # ══════════════════════════════════════════════════════════════════════
    # ── L1 只读排查 ──
    "a_check_roce_port": {"risk": "L1", "roles": [], "name": "检查RoCE端口状态",
                          "cmd": "ibdev2netdev -v; for p in /sys/class/infiniband/*/ports/*; do echo \"$p rate=$(cat $p/rate 2>/dev/null) state=$(cat $p/state 2>/dev/null)\"; done"},
    "a_check_kernel_module": {"risk": "L1", "roles": [], "name": "检查内核模块加载状态",
                              "cmd": "lsmod | grep -E 'ultrapath|nvme|mlx|rdma' || true; modinfo ultrapath 2>/dev/null | head -3"},
    "a_check_nvme_subsys": {"risk": "L1", "roles": [], "name": "检查NVMe子系统与控制器",
                            "cmd": "nvme list-subsys; nvme list-subsys -o json 2>/dev/null | head -c 4000"},
    "a_check_driver_version": {"risk": "L1", "roles": [], "name": "核对驱动版本",
                               "cmd": "modinfo nvme / nvme_rdma 2>/dev/null | grep -E '^(version|filename)'; uname -r"},
    "a_check_io_scheduler": {"risk": "L1", "roles": [], "name": "检查IO调度器",
                             "cmd": "for d in /sys/block/*/queue/scheduler; do echo \"$d: $(cat $d)\"; done"},
    "a_check_disk_io": {"risk": "L1", "roles": [], "name": "检查磁盘IO指标",
                        "cmd": "iostat -x 1 2 | tail -20"},
    "a_check_cpu": {"risk": "L1", "roles": [], "name": "检查CPU与负载",
                    "cmd": "uptime; top -bn1 | head -15"},
    "a_check_fs_usage": {"risk": "L1", "roles": [], "name": "检查文件系统使用率",
                         "cmd": "df -hT; pvs; vgs; lvs -o +data_percent"},
    "a_check_block_io": {"risk": "L1", "roles": [], "name": "检查块设备IO错误",
                         "cmd": "cat /sys/block/*/device/ioerr_cnt 2>/dev/null; dmesg | grep -iE 'I/O error|nvme.*error' | tail -20"},
    "a_check_smart": {"risk": "L1", "roles": [], "name": "检查磁盘SMART",
                      "cmd": "smartctl -H -A /dev/sd? 2>/dev/null | grep -E 'SMART|Reallocated|Pending'"},
    "a_check_pcie": {"risk": "L1", "roles": [], "name": "检查PCIe AER错误",
                     "cmd": "dmesg | grep -iE 'AER|pcieport.*error' | tail -20; lspci -vvv 2>/dev/null | grep -A2 'LnkSta:' | head -20"},
    "a_check_dorado_alarm": {"risk": "L1", "roles": [], "name": "查询Dorado当前告警",
                             "cmd": "DeviceManager: GET /alarm/current（按控制器/端口过滤）"},
    "a_check_dorado_ctrl": {"risk": "L1", "roles": [], "name": "查询Dorado控制器状态",
                            "cmd": "DeviceManager: GET /controller（HEALTHSTATUS / RUNNINGSTATUS / 主控归属）"},
    "a_check_dorado_replication": {"risk": "L1", "roles": [], "name": "查询容灾复制对状态",
                                   "cmd": "DeviceManager: GET /replicationpair（RUNNINGSTATUS / ISDATASYNC / 进度）"},
    "a_check_dorado_pool": {"risk": "L1", "roles": [], "name": "查询存储池容量",
                            "cmd": "DeviceManager: GET /storagepool（使用率 / 阈值 / 剩余容量）"},
    "a_check_lun_mapping": {"risk": "L1", "roles": [], "name": "核对LUN→Host映射",
                            "cmd": "DeviceManager: GET /host_lun_map; OS侧对照：upadmin_plus 'show vlun' / lsblk -o NAME,SIZE,MODEL"},
    "a_check_compat_matrix": {"risk": "L1", "roles": [], "name": "核对厂商兼容矩阵",
                              "cmd": "upadmin_plus 'show supportarraylist' + 'show version verbose'，比对华为存储兼容清单"},
    "a_verify_ultrapath_path": {"risk": "L1", "roles": [], "name": "核对UltraPath路径状态",
                                "cmd": "printf 'show path\\nshow vlun\\nquit\\n' | timeout 5 upadmin_plus"},
    # ── L2 可回滚 / 在线操作 ──
    "a_ultrapath_rescan": {"risk": "L2", "roles": ["oncall"], "name": "重扫UltraPath路径",
                           "cmd": "upRescan_plus || upadmin_plus 'rescan'（按厂商指引）"},
    "a_rescan_nvme": {"risk": "L2", "roles": ["oncall"], "name": "重扫NVMe控制器",
                      "cmd": "for c in /sys/class/nvme/nvme*; do echo 1 > $c/rescan_controller 2>/dev/null; done; nvme list-subsys"},
    "a_set_io_scheduler": {"risk": "L2", "roles": ["oncall"], "name": "调整IO调度器",
                           "cmd": "echo {scheduler} > /sys/block/{device}/queue/scheduler（多路径/NVMe 用 none）"},
    "a_expand_lv": {"risk": "L2", "roles": ["oncall"], "name": "扩容LVM逻辑卷",
                    "cmd": "lvextend -L +{size} /dev/mapper/{lv} && resize2fs /dev/mapper/{lv}"},
    "a_dorado_rep_resync": {"risk": "L2", "roles": ["oncall"], "name": "触发容灾复制对重新同步",
                            "cmd": "DeviceManager: POST /replicationpair/synchronize（远端可达后）"},
    # ── L3 高风险（双人复核）──
    "a_reload_kernel_module": {"risk": "L3", "roles": ["oncall", "business"],
                               "name": "重新加载内核模块",
                               "cmd": "（维护窗口）modprobe -r {module} && modprobe {module}，须先确认无在用 IO"},
    "a_set_mtu": {"risk": "L3", "roles": ["oncall", "business"], "name": "统一RoCE MTU",
                  "cmd": "ip link set {dev} mtu 9000（需与交换机侧 MTU 同步调整）"},
    "a_remap_lun_host": {"risk": "L3", "roles": ["oncall", "business"], "name": "补齐LUN→Host映射",
                         "cmd": "DeviceManager: 为 LUN {lun} 增加 Host 映射（双控两侧）后重扫"},
    "a_dorado_ctrl_switchover": {"risk": "L3", "roles": ["oncall", "business"],
                                 "name": "Dorado控制器切换",
                                 "cmd": "（维护窗口）DeviceManager 控制器'切换'操作；切换前确认对端可接管全部 LUN"},
    "a_dorado_pool_expand": {"risk": "L3", "roles": ["oncall", "business"], "name": "扩容存储池",
                             "cmd": "DeviceManager: 为存储池 {pool} 扩容（加盘或迁移冷数据）"},
    "a_replace_disk": {"risk": "L3", "roles": ["oncall", "business"], "name": "更换故障磁盘",
                       "cmd": "（硬件维护）定位故障盘位 → 按 RAID/厂商流程更换 → 阵列重建"},
    # ══ step3 第二批：action_map.yaml 映射目标（补齐规则/runbook 全部引用）══
    # ── 数据库域（此前只有 a_analyze 等基础动作，规则 next_actions 无一可解析）──
    "a_vacuum": {"risk": "L2", "roles": ["oncall"], "name": "执行VACUUM回收死元组",
                 "cmd": "VACUUM (VERBOSE, ANALYZE) {table_name};"},
    "a_reindex": {"risk": "L3", "roles": ["oncall", "business"], "name": "重建索引",
                  "cmd": "REINDEX INDEX CONCURRENTLY {index_name};"},
    "a_drop_index": {"risk": "L3", "roles": ["oncall", "business"], "name": "删除无用索引",
                     "cmd": "DROP INDEX CONCURRENTLY {index_name};"},
    # ── 网络/光模块/交换机（L1 排查 + L3 更换）──
    "a_check_roce_cable": {"risk": "L1", "roles": [], "name": "检查RoCE线缆与光模块",
                           "cmd": "ethtool -m {dev} 2>/dev/null | head -30; ethtool {dev} | grep -E 'Speed|Link detected'"},
    "a_check_fiber": {"risk": "L1", "roles": [], "name": "检查光纤链路",
                      "cmd": "ethtool {dev}; dmesg | grep -iE 'link|fiber|optical' | tail -20"},
    "a_check_optical": {"risk": "L1", "roles": [], "name": "检查光模块质量",
                        "cmd": "ethtool -m {dev} 2>/dev/null（关注 RX power / Temperature / Vendor）"},
    "a_check_switch": {"risk": "L1", "roles": [], "name": "检查交换机端口配置",
                       "cmd": "（网络团队）核对交换机侧 MTU/QoS/PFC 配置与端口状态"},
    # ── 硬件更换 ──
    "a_replace_nic": {"risk": "L3", "roles": ["oncall", "business"], "name": "更换网卡/调整槽位",
                      "cmd": "（硬件维护）确认 AER 报错槽位 → 停机窗口更换网卡 → 验证链路速率"},
    "a_replace_optical": {"risk": "L3", "roles": ["oncall", "business"], "name": "更换光模块",
                          "cmd": "（硬件维护）更换 {dev} 光模块并验证 rate/state 恢复"},
    "a_check_dorado_hw": {"risk": "L1", "roles": [], "name": "检查Dorado控制器硬件指示",
                          "cmd": "（现场/带外）查看控制器前后面板：电源 PSU、风扇 FAN、告警灯、温度传感器"},
    "a_clean_dorado_data": {"risk": "L3", "roles": ["oncall", "business"], "name": "清理快照/冷数据",
                            "cmd": "DeviceManager: 清理过期快照与冷数据（须先确认无业务依赖）"},
    # ── 文件系统清理 / 备份 ──
    "a_clean_fs": {"risk": "L2", "roles": ["oncall"], "name": "清理文件系统冗余数据",
                   "cmd": "定位并清理大文件/旧日志（du -sh * | sort -rh | head -20，确认后删除）"},
    "a_backup_data": {"risk": "L2", "roles": ["oncall"], "name": "备份系统数据",
                      "cmd": "备份关键配置与数据（gs_dump / 配置归档）后再做硬件变更"},
    # ── 驱动/固件升级 ──
    "a_upgrade_driver": {"risk": "L3", "roles": ["oncall", "business"], "name": "升级驱动/固件",
                         "cmd": "（维护窗口）按厂商兼容矩阵升级 nvme-rdma/UltraPath 驱动或 Dorado 固件"},
    "a_rebuild_initramfs": {"risk": "L3", "roles": ["oncall", "business"], "name": "重建initramfs",
                            "cmd": "dracut -f --regenerate-all（改内核模块/主设备号后必须重建，否则重启失联）"},
    "a_escalate": {"risk": "L1", "roles": [], "name": "升级至存储/网络团队联合排障",
                   "cmd": "（流程）按值班升级路径通知存储团队与网络团队，附本次证据链"},
    # ── 补充采集（本质是触发 gather，零变更）──
    "a_collect_detail": {"risk": "L1", "roles": [], "name": "补充采集明细",
                         "cmd": "gather: 按 next_actions 语义补充采集对应 sections（会话/锁/事务/执行计划等）"},
}


@router.post("/tools/execute")
def tools_execute(req: ToolsExecuteRequest):
    act = _ACTION_DEFS.get(req.action_id)
    if not act:
        return {"success": False, "error": f"未知 Action: {req.action_id}", "required_roles": []}
    required = act["roles"]
    if required and not set(required).issubset(set(req.approval_roles)):
        return {"success": False, "error": "审批不足", "required_roles": required,
                "provided_roles": req.approval_roles}
    try:
        cmd = act["cmd"].format(**req.params)
    except (KeyError, IndexError):
        cmd = act["cmd"]
    return {
        "success": True,
        "action_id": req.action_id,
        "action_name": act["name"],
        "target_id": req.target_id,
        "risk": act["risk"],
        "command": cmd,
        "reviewed_by": "+".join(req.approval_roles) if req.approval_roles else "auto(L1)",
        "note": "已通过审批（命令仅供参考，实际执行需进入变更审批流）",
    }


# ── T8 /tools/summarize —— summarize_result ──

class ToolsTableRequest(BaseModel):
    table: str = ""


@router.post("/tools/table_stats")
def tools_table_stats(req: ToolsTableRequest):
    if not req.table:
        return {"success": False, "error": "table 必填", "rows": []}
    try:
        rows = _run_sync(_async_query(
            *_conn_args(),
            f"SELECT relname, n_live_tup, last_analyze FROM pg_stat_user_tables WHERE relname ILIKE '%{req.table}%' ORDER BY n_live_tup DESC LIMIT 5",
        ))
        return {"success": True, "table": req.table, "rows": rows, "count": len(rows)}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "rows": []}


# ── /collect （旧版兼容 ──
@router.post("/collect")
def collect_legacy(req: CollectRequest):
    return tools_collect(ToolsCollectRequest(cluster_oid=req.cluster_oid))


# ── 日志检索工具（GaussDB + Dorado 双源） ──
class SearchLogsRequest(BaseModel):
    query: str = ""
    source: str = "all"          # gaussdb | dorado | all
    node_ip: str = ""
    log_level: str = ""
    time_range: str = "24h"
    size: int = 20
    dedupe: bool = True          # Dorado 告警按 event_id 折叠
    detail: str = "brief"        # brief | full


_SOURCE_INDEX = {
    "gaussdb": "gaussdb-logs-*",
    "dorado": "dorado-logs-*",
    "all": "gaussdb-logs-*,dorado-logs-*",
}

# 精简模式字段上限：华为 suggestion 单条可达 1500+ 字符，50 条会把响应撑到 ~100KB，
# 导致 Agent 一轮读不完而重复调用同一工具。
_MSG_MAX = int(os.environ.get("LOG_MSG_MAX", "200"))
_SUGG_MAX = int(os.environ.get("LOG_SUGG_MAX", "400"))

# 单次返回的载荷预算（tokens）—— 2026-09-19 契约体检 C2 实测标定。
#   brief 模式 size=100 → 23,663 tokens（超 20,000 预算）；
#   full 模式单条 message 不截断（实测可达 2KB），条数固定根本挡不住。
# 所以按**预算**裁而不是按条数裁：结果按时间倒序，从新到旧累加，
# 超出预算即停，并把丢弃量如实写进返回体。
# 契约：既有调用方拿到的 results 语义不变（仍是最新的 N 条），
# 只是 N 可能小于请求的 size —— 且**一定会被告知**。
_LOG_PAYLOAD_BUDGET_TOKENS = int(os.environ.get("LOG_PAYLOAD_BUDGET", "16000"))


def _clip(text, limit: int) -> str:
    """超长字段截断并加省略号"""
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"



@router.post("/tools/search_logs")
def tools_search_logs(req: SearchLogsRequest):
    """检索已采集到 ES 的 GaussDB 日志 / Dorado 存储告警日志

    返回体默认「精简」：message/suggestion 截断、丢弃 highlight、
    Dorado 告警按 event_id 折叠（同一告警反复出现只回一条 + occurrences 计数）。
    """
    k8s_es = os.environ.get("K8S_ES_HOST", "http://192.0.2.20:30920")
    import datetime

    # 时间范围 1h / 30m / 7d / 24h
    unit = req.time_range[-1].lower()
    try:
        val = int(req.time_range[:-1]) if len(req.time_range) > 1 else 1
    except ValueError:
        val = 1
    now = datetime.datetime.utcnow()
    if unit == "h":
        since = now - datetime.timedelta(hours=val)
    elif unit == "d":
        since = now - datetime.timedelta(days=val)
    elif unit == "m":
        since = now - datetime.timedelta(minutes=val)
    else:
        since = now - datetime.timedelta(hours=24)

    must = [
        {"range": {"@timestamp": {"gte": since.isoformat(), "lte": now.isoformat()}}}
    ]
    if req.query:
        must.append({"match": {"message": req.query}})
    if req.node_ip:
        must.append({"term": {"node_ip": req.node_ip}})
    if req.log_level:
        must.append({"term": {"dorado_level_name": req.log_level.upper()}})

    index = _SOURCE_INDEX.get((req.source or "all").lower(), _SOURCE_INDEX["all"])
    full = (req.detail or "brief").lower() == "full"

    body = {
        "size": min(max(req.size, 1), 100),
        "query": {"bool": {"must": must}},
        "sort": [{"@timestamp": "desc"}],
        "_source": [
            "@timestamp", "log_type", "node_ip", "device_name",
            "dorado_event_id", "dorado_level_name", "dorado_name",
            "dorado_event_time", "message", "dorado_suggestion",
        ],
    }
    # Dorado 告警折叠：同一 event_id 反复出现只回一条，出现次数由聚合给出
    # （仅 source=dorado 时启用，避免跨索引 collapse 到不存在的字段）
    if req.dedupe and index == _SOURCE_INDEX["dorado"]:
        body["collapse"] = {"field": "dorado_event_id.keyword"}
        body["aggs"] = {
            "by_event": {"terms": {"field": "dorado_event_id.keyword", "size": 50}}
        }

    try:
        _es_url = f"{k8s_es}/{index}/_search?ignore_unavailable=true"
        if _sim is not None:
            # ★ 模拟演练打桩（docs/60）：ES 日志出口（collect_api 侧）。
            #   es_log_search.py 另有 _es_request 出口 —— 两处都打桩，不留缺口。
            r = _sim.http_requests(lambda: requests.post(_es_url, json=body, timeout=20),
                                   "POST", _es_url, body=body, kind="es")
        else:
            r = requests.post(f"{k8s_es}/{index}/_search?ignore_unavailable=true",
                              json=body, timeout=20)
        if r.status_code != 200:
            return {"success": False, "error": f"ES {r.status_code}: {r.text[:200]}"}
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])
        occ = {
            b.get("key"): b.get("doc_count")
            for b in (data.get("aggregations") or {}).get("by_event", {}).get("buckets", [])
        }

        results = []
        for h in hits:
            src = h.get("_source", {})
            eid = src.get("dorado_event_id", "")
            item = {
                "timestamp": src.get("@timestamp", ""),
                "event_time": src.get("dorado_event_time", ""),
                "source": src.get("log_type", ""),
                "node_ip": src.get("node_ip", ""),
                "device": src.get("device_name", ""),
                "event_id": eid,
                "level": src.get("dorado_level_name", ""),
                "name": src.get("dorado_name", ""),
                "message": src.get("message", "") if full
                           else _clip(src.get("message", ""), _MSG_MAX),
            }
            sugg = src.get("dorado_suggestion", "")
            if sugg:
                item["suggestion"] = sugg if full else _clip(sugg, _SUGG_MAX)
            if eid and eid in occ:
                item["occurrences"] = occ[eid]
            results.append(item)

        out = {
            "success": True,
            "index": index,
            "total": data.get("hits", {}).get("total", {}).get("value", 0),
            "returned": len(results),
            "detail": "full" if full else "brief",
            "results": results,
        }
        # ★ 载荷预算：按 token 预留裁剪（保留最新的若干条）
        #   只裁 results 尾部，其余摘要字段保留 —— "没报"不能被读成"没问题"。
        _budget = _LOG_PAYLOAD_BUDGET_TOKENS
        _kept, _used = [], 0
        for _it in results:
            _t = int(len(json.dumps(_it, ensure_ascii=False)) / 3.5) + 1
            if _kept and _used + _t > _budget:
                break
            _kept.append(_it)
            _used += _t
        if len(_kept) < len(results):
            out["results"] = _kept
            out["returned"] = len(_kept)
            out["dropped"] = len(results) - len(_kept)
            out["payload_budget_tokens"] = _budget
            out["note"] = (f"为控制单次载荷，按预算只回最新 {len(_kept)} 条"
                           f"（丢弃 {out['dropped']} 条）。缩小 time_range 或加关键词"
                           f"可拿到更聚焦的结果。")
        if not out["results"]:
            out["hint"] = ("未检索到匹配日志（属正常结果，可能该时段无相关记录）。"
                           "请勿重复调用 search_logs，可放宽 time_range 或改关键词；"
                           "若仍无结果，直接基于已采集的事实给出根因分析。")
        return _loop_guard("search_logs", req, out)
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("SERVICE_HOST", "0.0.0.0"),
                port=int(os.environ.get("SERVICE_PORT", "8187")), log_level="info")

@router.get("/health")
def health():
    info: Dict[str, Any] = {"service": "upstream-ops-collect", "version": "6.1.0",
                            "ts": datetime.datetime.now().isoformat()}
    # ★ 数据来源必须**在最前面**可见（docs/60）：演练模式下这份 /health 不能看起来
    #   跟生产一样。同时把"未命中的出口"一并暴露（fail-closed 的第 1 条不许静默）。
    if _sim is not None:
        info["collect_source"] = _sim.provenance()
        _misses = _sim.misses()
        if _misses:
            info["collect_source"]["unmatched_exits"] = len(_misses)
            info["collect_source"]["unmatched_detail"] = _misses[:20]
    else:
        info["collect_source"] = {"collect_source": "live", "simulated": False,
                                  "note": "collect_sim 未加载（等价 live）"}
    # replay/record 下装上网络守卫（第 2 道闸：防"漏打桩的出口悄悄走真实环境"）
    if _sim is not None and _sim.mode() != "live":
        _sim.install_network_guard(
            allow_hosts=[h.strip() for h in
                         (os.environ.get("GDO_SIM_ALLOW_HOSTS") or "").split(",") if h.strip()])
    try:
        async def _v():
            import async_gaussdb
            conn = await async_gaussdb.connect(**_conn_kwargs)
            try:
                row = await conn.fetchrow("SELECT version() AS ver, now() AS ts")
                return dict(row) if row else {}
            finally:
                await conn.close()
        ver = _run_sync(_v())
        info["gaussdb"] = {"ok": True, "version": str(ver.get("ver", ""))[:80],
                           "now": str(ver.get("ts"))}
    except Exception as e:
        info["gaussdb"] = {"ok": False, "error": str(e)[:200]}

    # ── 知识面贡献块 ──
    # neo4j / rules / collect_contract 由知识面（service.knowledge_api）注册。
    # 插入位置严格保持 monolith 时期的键序：gaussdb → neo4j → collect_cache
    # → rules → collect_contract，以确保响应 JSON 与拆分前逐字节一致。
    _contrib = collect_health_contributions()
    if "neo4j" in _contrib:
        info["neo4j"] = _contrib["neo4j"]

    import time
    info["collect_cache"] = {"cached": _CACHED_COLLECT_AT > 0,
                             "age_s": round(time.time() - _CACHED_COLLECT_AT)
                             if _CACHED_COLLECT_AT else None}
    for _k in ("rules", "collect_contract"):
        if _k in _contrib:
            info[_k] = _contrib[_k]

    if "rules" not in _contrib:
        info["rules"] = {"ok": False,
                         "error": "知识面未加载（service.knowledge_api 未导入）—— "
                                  "本进程可能只装了采集层"}
    return info


