# -*- coding: utf-8 -*-
"""M3.2 —— Set B 八工具知识服务（方案1/KG 侧）离线单测 + 真机 smoke。

运行：
    python scheme1-kg\\tests\\test_cc_tools.py              # 离线（不连数据库）
    CC_M32_LIVE=1 python scheme1-kg\\tests\\test_cc_tools.py   # 附加真机 smoke（只读）

设计原则（对应 M3.2 硬验收）：
  * 守卫的每一条拒绝路径都有**负控**：给定输入 ⇒ 期望的逐字错误码。
  * 至少一条负控是**变异可证伪的**：把守卫的正则/上限临时改坏后，
    该负控必须随之失败 —— 否则「负控」只是恒真断言。
  * 形状断言以契约面（tools-setb.yaml）为准，而不是以实现的 TOOLFACE 为准：
    第二节用**真·G24 门禁模块**的 contract_face/manifest_face/compare_faces 交叉验证。
  * 空结果必须是**良定义**的空返回 + hint，不是异常、不是伪造行。

只读保证：本文件对图的每一条查询都经 cc_tools 的只读通道；除离线 fake 外不写任何图。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess, tempfile
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.join(os.path.dirname(HERE), "service")
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK ⇒ 中文报表会崩
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                            # noqa: BLE001
        pass

import cc_tools as ct  # noqa: E402


# ===========================================================================
# 迷你测试框架（本文件可独立运行，不依赖 pytest）
# ===========================================================================
PASSED: list = []
FAILED: list = []


def check(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, "ASSERT: %s" % exc))
        print("  FAIL  %-46s %s" % (name, exc))
    except Exception as exc:                              # noqa: BLE001
        FAILED.append((name, "%s: %s" % (type(exc).__name__, exc)))
        print("  FAIL  %-46s %s: %s" % (name, type(exc).__name__, exc))
        traceback.print_exc()
    else:
        PASSED.append(name)
        print("  PASS  %s" % name)


def section(title: str) -> None:
    print("\n== %s ==" % title)


def expect_tool_error(code: str, fn, *args, **kwargs):
    """调用 fn(*args)，要求抛出 ToolError 且 code 逐字相等；返回异常对象。"""
    try:
        fn(*args, **kwargs)
    except ct.ToolError as exc:
        assert exc.code == code, "期望错误码 %s，实际 %s（%s）" % (code, exc.code, exc.message)
        return exc
    except Exception as exc:                              # noqa: BLE001
        raise AssertionError("期望 ToolError(%s)，实际抛出 %s: %s"
                             % (code, type(exc).__name__, exc))
    raise AssertionError("期望 ToolError(%s)，但调用成功返回（守卫漏判！）" % code)


# ===========================================================================
# 离线假后端：按语句片段路由 + 自动补齐 RETURN 别名
# ===========================================================================
ALIAS_RE = re.compile(r"AS\s+([A-Za-z_][A-Za-z_0-9]*)")


class FakeBackend:
    """只读假后端：绝无写图；记录每条语句与 timeout，便于断言守卫行为。"""

    kind = "fake"

    def __init__(self, routes=None, default_rows=None, error=None, delay=0.0) -> None:
        self.routes = list(routes or [])
        self.default_rows = default_rows
        self.error = error
        self.delay = float(delay)
        self.calls: list = []
        self.unmatched: list = []
        self.last_query = ""

    def cypher(self, statement, parameters=None, timeout=None):
        self.calls.append((statement, parameters, timeout))
        self.last_query = statement
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        aliases = ALIAS_RE.findall(statement)
        rows = None
        for needle, spec in self.routes:
            if needle in statement:
                rows = spec
                break
        if rows is None:
            if self.default_rows is None:
                self.unmatched.append(statement)
                return {"columns": [], "rows": [], "elapsedMs": 1.0}
            rows = self.default_rows
        if parameters and "frontier" in (parameters or {}):
            frontier = {str(x) for x in (parameters.get("frontier") or [])}
            if rows and all(isinstance(r, dict) and "src" in r for r in rows):
                rows = [r for r in rows if str(r.get("src")) in frontier]
        if not aliases:                     # 语句没有 AS 别名（如 "RETURN n"）⇒ 原样保留行
            filled = [dict(r) for r in rows]
        else:
            filled = [{a: r.get(a) for a in aliases} for r in rows]
        return {"columns": sorted(set(aliases)), "rows": filled, "elapsedMs": 1.0}

    # 便捷断言 ---------------------------------------------------------------
    def statements(self):
        return [c[0] for c in self.calls]

    def timeouts(self):
        return [c[2] for c in self.calls]

    def assert_no_unmatched(self):
        assert not self.unmatched, "有 %d 条语句没有路由（fake 未覆盖）：\n%s" % (
            len(self.unmatched), "\n---\n".join(self.unmatched))


# --- 小图 fixture（形状取自真机活体图口径，非杜撰字段名） -------------------
FM_A = "FM-APP-POOL-EXHAUSTED"
FM_B = "FM-APP-ENDPOINT-SLOW"
FM_C = "FM-DB-SLOW-QUERY"
FM_D = "FM-DB-LOCK-WAIT"
POOL = "dbpool:dn_6001-pool"
JDB = "jdbsrc:ds-cc-pay-gaussdb"

FM_ROWS = [
    {"fmCode": FM_A, "fmName": "应用连接池耗尽", "fmLayer": "app",
     "fmSeverity": "critical", "fmComponentType": "DatabaseSessionPool",
     "fmDetectionMode": "metric", "fmReversible": True, "fmChain": "F1",
     "fmDescription": "池内可用连接为 0"},
    {"fmCode": FM_B, "fmName": "应用端点变慢", "fmLayer": "app", "fmSeverity": "major",
     "fmComponentType": "AppEndpoint", "fmDetectionMode": "metric",
     "fmReversible": True, "fmChain": "F1", "fmDescription": "P99 上升"},
    {"fmCode": FM_C, "fmName": "数据库慢查询", "fmLayer": "db", "fmSeverity": "major",
     "fmComponentType": "Query", "fmDetectionMode": "metric",
     "fmReversible": True, "fmChain": "F2", "fmDescription": "单条 SQL 变慢"},
    {"fmCode": FM_D, "fmName": "数据库锁等待", "fmLayer": "db", "fmSeverity": "major",
     "fmComponentType": "Query", "fmDetectionMode": "metric",
     "fmReversible": True, "fmChain": "F2", "fmDescription": "行锁等待"},
]

CAUSE_EDGES = [
    {"src": FM_D, "rel": "CC_CAUSES", "dst": FM_C, "contractId": "E5", "chain": "F2"},
    {"src": FM_C, "rel": "CC_CAUSES", "dst": FM_A, "contractId": "E6", "chain": "F2"},
    {"src": FM_A, "rel": "CC_CAUSES", "dst": FM_B, "contractId": "E7", "chain": "F1"},
]

JUDGED = [
    {"fmCode": FM_C, "metricCode": "DB_SLOW_QUERY_COUNT", "op": ">", "role": "necessary",
     "operandKind": "literal", "operandRef": None, "operandValue": 0, "operandValues": None},
    {"fmCode": FM_D, "metricCode": "DB_LOCK_WAIT_COUNT", "op": ">", "role": "necessary",
     "operandKind": "threshold", "operandRef": "threshold:THR-LOCK#warning",
     "operandValue": None, "operandValues": None},
]

OBS_ROWS = [
    {"objectId": POOL, "metricCode": "DB_SLOW_QUERY_COUNT", "rawValue": "7",
     "capturedAt": "2026-09-28T15:55:00Z", "recordId": "ev-1"},
    {"objectId": POOL, "metricCode": "DB_LOCK_WAIT_COUNT", "rawValue": "3",
     "capturedAt": "2026-09-28T15:55:00Z", "recordId": "ev-2"},
]

AFFECTS_CAUSE = [
    {"fmCode": FM_C, "objectId": POOL, "objectType": "DatabaseSessionPool"},
    {"fmCode": FM_D, "objectId": "query:q-1", "objectType": "Query"},
]

GRAPH_ROUTES = [
    # --- 通用（注意顺序：越具体越靠前）-----------------------------------
    ("= $startId", [{"objectId": POOL, "objectType": "DatabaseSessionPool",
                     "layer": "gaussdb"}]),
    ("properties(n) AS props", [{"props": {"activeConnections": 0}}]),
    ("= $t", [{"objectId": "query:q-1"}]),
    ("IS NOT NULL RETURN DISTINCT", [{"objectType": "DatabaseSessionPool"},
                                     {"objectType": "Query"}]),
    ("properties(n) AS attributes",
     [{"objectId": POOL, "objectType": "DatabaseSessionPool",
       "attributes": {"ccfId": POOL, "ccfType": "DatabaseSessionPool", "maxSize": 20}}]),
    ("AS src, coalesce(b.ccfId",
     [{"relationType": "CC_HAS_SESSION", "src": POOL, "dst": "session:s-1"}]),
    ("WHERE e.metricCode IN $codes", OBS_ROWS),
    ("CC_HAS_EVIDENCE]->(e:CC_Evidence) WHERE",
     [{"recordId": "ev-1", "metricCode": "DB_SLOW_QUERY_COUNT", "rawValue": "7",
       "rawUnit": "count", "capturedAt": "2026-09-28T15:55:00Z"}]),
    ("RECOMMENDS]->(a)", [{"runbookId": "RB-LOCK-01", "runbookTitle": "锁等待处置",
                           "actionId": "ACT-KILL-SESSION", "actionName": "终止阻塞会话",
                           "actionRiskLevel": "medium",
                           "commandTemplate": "SELECT pg_terminate_backend(:pid)",
                           "approvalRequirement": "required", "auditRequired": True,
                           "reversible": False}]),
    ("n.actionId IS NOT NULL",
     [{"actionId": "ACT-KILL-SESSION", "actionName": "终止阻塞会话",
       "actionRiskLevel": "medium", "commandTemplate": "SELECT pg_terminate_backend(:pid)",
       "approvalRequirement": "required", "auditRequired": True, "reversible": False},
      {"actionId": "ACT-ANALYZE-STATS", "actionName": "更新统计信息",
       "actionRiskLevel": "low", "commandTemplate": "ANALYZE :table",
       "approvalRequirement": "required", "auditRequired": True, "reversible": True}]),
    ("e.recordId IN $ids", [{"recordId": "ev-1", "metricCode": "DB_SLOW_QUERY_COUNT"}]),
    ("RETURN DISTINCT f.fmCode", [{"fmCode": FM_C}]),
    ("AS objectId ORDER BY fmCode", AFFECTS_CAUSE),
    ("CC_AFFECTS]->(n) RETURN f.fmCode AS fmCode, coalesce(n.ccfId",
     AFFECTS_CAUSE),
    ("CC_JUDGED_BY]->(m:CC_Metric)", JUDGED),
    ("type(r) AS rel, b.fmCode AS dst", CAUSE_EDGES),
    ("f.fmName AS fmName", FM_ROWS),
    ("MATCH (t:CC_Threshold)", [{"thrCode": "THR-LOCK", "thrWarning": None,
                                 "thrCritical": 30}]),
    ("count(r) AS edgeCount", [{"relationType": "CC_CAUSES", "edgeCount": 16,
                                "fromType": "CC_FaultMode", "toType": "CC_FaultMode",
                                "cardinality": "N:M"}]),
    ("UNWIND keys(n) AS attribute",
     [{"owner": "CC_FaultMode", "attribute": "fmCode", "samples": ["FM-DB-LOCK-WAIT"]}]),
    ("UNWIND keys(r) AS attribute",
     [{"owner": "CC_JUDGED_BY", "attribute": "operandKind", "samples": ["threshold"]}]),
    ("AS keys, coalesce(n.ccfLayer,'') AS layer",
     [{"objectType": "DatabaseSessionPool",
       "keys": ["ccfId", "ccfType", "maxSize", "dbInstanceId"], "layer": "gaussdb",
       "nativeLabels": ["CC_DatabaseSessionPool"]}]),
    ("AS nativeLabels ORDER BY objectType",
     [{"objectType": "DatabaseSessionPool",
       "keys": ["ccfId", "ccfType", "maxSize", "dbInstanceId"],
       "nativeLabels": ["CC_DatabaseSessionPool"]}]),
    ("MATCH (m:CC_Metric)",
     [{"metricCode": "DB_SLOW_QUERY_COUNT", "unit": "count", "object": "DatabaseSessionPool",
       "kgProperty": "slowQueryCount", "valueType": "integer", "source": "db"}]), 
    ("m.snapshotId AS snapshotId", [{"snapshotId": "ccf-20260928T155516Z-937b79"}]),
    ("CC_CAUSES|CC_CONDITIONAL_CAUSES",
     [{"src": r["src"], "dst": r["dst"], "rel": r["rel"], "contractId": r["contractId"],
       "chain": r["chain"], "description": "authored", "provenance": "authored:"
       "shared/model/causal-edges.yaml#%s" % r["contractId"]} for r in CAUSE_EDGES]),
    # --- cc_trace 的邻居展开（方向不同 ⇒ needle 不同）---------------------
    ("-[r]->(b) WHERE type(r) STARTS WITH 'CC_' AND NOT type(r) IN $skipRels",
     [{"src": POOL, "relationType": "CC_HAS_SESSION", "dst": "session:s-1",
       "dstType": "Session", "dstLayer": "gaussdb", "edgeFrom": POOL,
       "edgeTo": "session:s-1"},
      {"src": JDB, "relationType": "CC_DEPENDS_ON", "dst": POOL,
       "dstType": "DatabaseSessionPool", "dstLayer": "gaussdb", "edgeFrom": JDB,
       "edgeTo": POOL}]),
    ("<-[r]-(b) WHERE type(r) STARTS WITH 'CC_' AND NOT type(r) IN $skipRels",
     [{"src": POOL, "relationType": "CC_DEPENDS_ON", "dst": JDB,
       "dstType": "JdbcDataSource", "dstLayer": "app", "edgeFrom": JDB,
       "edgeTo": POOL}]),
    # --- cc_query 的夹具语句（放最后，避免吃掉其它路由）-------------------
    ("MATCH (n) RETURN n", [{"n": "1"}]),
]


def graph_backend(**kwargs) -> FakeBackend:
    return FakeBackend(routes=GRAPH_ROUTES, **kwargs)


# 负控夹具：故意让后端**不**返回原生标签列（语句照旧投影，取值缺 ⇒ None）
NO_NATIVE_ROUTES = [
    (needle, [{k: v for k, v in row.items() if k != "nativeLabels"} for row in rows])
    for needle, rows in GRAPH_ROUTES
]


def no_native_backend(**kwargs) -> FakeBackend:
    return FakeBackend(routes=NO_NATIVE_ROUTES, **kwargs)


EMPTY_BACKEND_ROUTES = [("IS NOT NULL RETURN DISTINCT", [{"objectType": "Host"}])]


def service_with(backend) -> ct.CcToolsService:
    return ct.CcToolsService(backend)


def expected_keys(tool: str) -> set:
    spec = ct.TOOLFACE[tool]
    return {n for n, _t in spec["returns"]} | {n for n, _t in ct.ENVELOPE_FIELDS}


# ===========================================================================
# 1. cc_query 只读守卫 —— 每条拒绝路径一个负控
# ===========================================================================
def test_guards() -> None:
    section("1. cc_query 只读守卫（每条拒绝路径一个负控）")

    def write_token_toplevel():
        exc = expect_tool_error(ct.ERR_FORBIDDEN_TOKEN, ct.guard_query,
                                "MATCH (n) SET n.x = 1 RETURN n")
        assert exc.detail.get("matchedToken"), "FORBIDDEN_TOKEN 必须回传命中 token"
        assert "SET" in str(exc.detail.get("matchedToken")).upper(), exc.detail
    check("guard/write-token-toplevel", write_token_toplevel)

    def write_token_subquery():
        # 写操作藏在 CALL {} 子查询里 —— R-READONLY-TOKENS 必须同样命中
        exc = expect_tool_error(
            ct.ERR_FORBIDDEN_TOKEN, ct.guard_query,
            "MATCH (n) CALL { WITH n CREATE (m:Evil) RETURN m } RETURN n")
        assert "CREATE" in str(exc.detail.get("matchedToken")).upper(), exc.detail
    check("guard/write-token-subquery", write_token_subquery)

    def write_token_detach_delete():
        expect_tool_error(ct.ERR_FORBIDDEN_TOKEN, ct.guard_query,
                          "MATCH (n) DETACH DELETE n")
    check("guard/detach-delete", write_token_detach_delete)

    def multi_statement():
        # 语句中部的分号（1 个）⇒ 视为多语句
        exc = expect_tool_error(ct.ERR_MULTI_STATEMENT_REJECTED, ct.guard_query,
                                "MATCH (n) RETURN n; MATCH (m) RETURN m")
        assert exc.detail.get("semicolonCount") == 1, exc.detail
        assert ct.count_semicolons("MATCH (n) RETURN n; MATCH (m) RETURN m") == 1
        # 分号计数 > 1 ⇒ 直接拒绝
        exc2 = expect_tool_error(ct.ERR_MULTI_STATEMENT_REJECTED, ct.guard_query,
                                 "MATCH (a) RETURN a; MATCH (b) RETURN b; MATCH (c) RETURN c")
        assert exc2.detail.get("semicolonCount") == 2, exc2.detail
    check("guard/multi-statement", multi_statement)

    def trailing_semicolon_ok():
        # 单语句 + 结尾分号 ⇒ 允许（分号计数 1，不是多语句）
        out = ct.guard_query("MATCH (n) RETURN n;")
        assert "MATCH (n) RETURN n" in out
        assert ct.count_semicolons("MATCH (n) RETURN n;") == 1
    check("guard/single-statement-trailing-semicolon-ok", trailing_semicolon_ok)

    def external_io():
        exc = expect_tool_error(
            ct.ERR_EXTERNAL_IO_FORBIDDEN, ct.guard_query,
            "MATCH (n) CALL apoc.load.json('http://evil/x') YIELD value RETURN value")
        assert "apoc" in str(exc.detail.get("matchedToken") or exc.message).lower()
    check("guard/external-io-apoc-load", external_io)

    def external_import():
        expect_tool_error(ct.ERR_EXTERNAL_IO_FORBIDDEN, ct.guard_query,
                          "CALL apoc.import.csv('http://evil/x') YIELD value RETURN value")
    check("guard/external-io-apoc-import", external_import)

    def form_rejected():
        expect_tool_error(ct.ERR_QUERY_FORM_REJECTED, ct.guard_query,
                          "EXPLAIN MATCH (n) RETURN n")
    check("guard/form-not-whitelisted", form_rejected)

    def unbounded_depth():
        exc = expect_tool_error(
            ct.ERR_DEPTH_LIMIT_EXCEEDED, ct.guard_query,
            "MATCH p=(a:CC_FaultMode)-[:CC_CAUSES*1..5]->(b) RETURN b")
        assert exc.detail.get("upperBound") == 5, exc.detail
        assert exc.detail.get("limit") == 4, exc.detail
    check("guard/depth-bounded-5-exceeds-4", unbounded_depth)

    def unbounded_path():
        expect_tool_error(ct.ERR_DEPTH_LIMIT_EXCEEDED, ct.guard_query,
                          "MATCH p=(a:CC_FaultMode)-[:CC_CAUSES*]->(b) RETURN b")
    check("guard/unbounded-path-star", unbounded_path)

    def star_in_count_ok():
        # count(*) 的 * 不在方括号里 ⇒ 不得误判为无界路径
        out = ct.guard_query("MATCH (n) RETURN count(*) AS c")
        assert "count(*)" in out
    check("guard/count-star-not-false-positive", star_in_count_ok)

    def forced_limit():
        bounded, injected = ct.apply_force_limit("MATCH (n) RETURN n")
        assert injected is True and bounded.endswith(" LIMIT %d" % (ct.ROW_CAP + 1)), bounded
        kept, injected2 = ct.apply_force_limit("MATCH (n) RETURN count(n) AS c")
        assert injected2 is False and kept == "MATCH (n) RETURN count(n) AS c"
    check("guard/force-limit-injects-not-rejects", forced_limit)

    def query_param_required():
        expect_tool_error(ct.ERR_INVALID_PARAM, ct.validate_params, "cc_query", {})
    check("params/missing-required-query", query_param_required)

    def query_param_type():
        expect_tool_error(ct.ERR_INVALID_PARAM, ct.validate_params, "cc_query",
                          {"query": 123})
    check("params/query-wrong-type", query_param_type)

    def unknown_param():
        exc = expect_tool_error(ct.ERR_INVALID_PARAM, ct.validate_params, "cc_facts",
                                {"objectType": "Host", "bogus": 1, "limit": 5})
        assert sorted(exc.detail.get("unknownParams")) == ["bogus", "limit"], exc.detail
        assert sorted(exc.detail.get("acceptedParams")) == ["metricCodes", "objectId",
                                                            "objectType"], exc.detail
    check("params/unknown-param-rejected-not-defaulted", unknown_param)

    def trace_hop_bounds():
        # ≤9 是契约域上界：由 build_trace 拒绝（不是静默截断）
        expect_tool_error(ct.ERR_INVALID_PARAM, ct.build_trace, FakeBackend(), "x", "out", 10)
        # 类型strict：字符串数字不得被当 int 接受
        exc = expect_tool_error(ct.ERR_INVALID_PARAM, ct.validate_params, "cc_trace",
                                {"startId": "x", "maxHops": "3"})
        assert "integer" in exc.message, exc.message
        # 边界值 9 必须放行
        out = ct.build_trace(FakeBackend(), "x", "out", 9)
        assert out["hopsUsed"] == 0
    check("params/trace-maxHops-9-exceeds", trace_hop_bounds)

    def direction_domain():
        expect_tool_error(ct.ERR_INVALID_PARAM, ct.build_trace,
                          FakeBackend(), "x", "sideways", 1)
    check("params/trace-direction-domain", direction_domain)

    def cause_depth():
        exc = expect_tool_error(ct.ERR_DEPTH_LIMIT_EXCEEDED, ct.build_cause,
                                FakeBackend(), [FM_A], 5)
        assert exc.detail.get("limit") == 4, exc.detail
        # ≤4 必须放行（拒绝而非截断，但合法值不得被拒）
        ct.build_cause(graph_backend(), [FM_A], 4)
    check("params/cause-maxHops-5-rejected-4-accepted", cause_depth)

    def propose_dry_run_only():
        for key in ("dryRun", "dry_run", "execute", "exec", "apply", "commit", "confirm"):
            exc = expect_tool_error(ct.ERR_DRY_RUN_ONLY, ct.validate_params, "cc_propose",
                                    {"rootCause": FM_A, key: True})
            assert exc.detail.get("rejectedParam") == key, exc.detail
            assert exc.detail.get("executed") is False and exc.detail.get("dryRun") is True
        # 未知参数（非执行语义）仍走 INVALID_PARAM，不被 DRY_RUN_ONLY 吞掉
        expect_tool_error(ct.ERR_INVALID_PARAM, ct.validate_params, "cc_propose",
                          {"rootCause": FM_A, "nonsense": 1})
    check("params/propose-dry-run-only", propose_dry_run_only)

    def propose_always_dry():
        out = ct.build_propose(graph_backend(), FM_C, [])
        assert out["dryRun"] is True and out["executed"] is False, out
        assert out["actions"], "应给出动作建议（环境级动作目录）"
        assert all(a.get("approvalRequirement") for a in out["actions"])
    check("propose/executed-false-dryRun-true", propose_always_dry)


# ===========================================================================
# 2. 负控的「可证伪性」（变异测试）—— 证明上面的负控不是恒真断言
# ===========================================================================
def test_negative_controls_are_falsifiable() -> None:
    section("2. 负控可证伪性（故意改坏守卫 ⇒ 负控必须随之失败）")

    def mutation_forbidden_re():
        saved = ct.FORBIDDEN_RE
        ct.FORBIDDEN_RE = re.compile(r"(?!x)x")            # 永不匹配
        try:
            try:
                ct.guard_query("MATCH (n) DELETE n")
            except ct.ToolError:
                survived = False
            else:
                survived = True                            # 守卫失效 ⇒ 负控应失败
        finally:
            ct.FORBIDDEN_RE = saved
        assert survived, "变异后仍被拒绝 ⇒ 该负控与守卫实现无因果（恒真）"
        # 复原后必须恢复拒绝
        expect_tool_error(ct.ERR_FORBIDDEN_TOKEN, ct.guard_query, "MATCH (n) DELETE n")
    check("mutation/forbidden-regex-neutered-control-fails", mutation_forbidden_re)

    def mutation_row_cap():
        saved = ct.ROW_CAP
        ct.ROW_CAP = 10 ** 9
        try:
            backend = FakeBackend(routes=[("RETURN count(*) AS total", [{"total": 251}]),
                                          ("MATCH (n) RETURN n",
                                           [{"n": "x"} for _ in range(201)])])
            out = ct.build_query(backend, "MATCH (n) RETURN n")
            not_truncated = out["truncated"] is False and out["rowCount"] == 201
        finally:
            ct.ROW_CAP = saved
        assert not_truncated, "变异行上限后仍报截断 ⇒ 行上限负控与实现无因果"
    check("mutation/row-cap-neutered-control-fails", mutation_row_cap)

    def mutation_semicolon():
        saved = ct.count_semicolons
        ct.count_semicolons = lambda _text: 0                 # 假装永远没有分号
        try:
            try:
                ct.guard_query("MATCH (n) RETURN n; MATCH (m) RETURN m")
            except ct.ToolError:
                survived = False
            else:
                survived = True
        finally:
            ct.count_semicolons = saved
        assert survived, "多语句负控与 count_semicolons 无因果（恒真）"
    check("mutation/semicolon-counter-neutered-control-fails", mutation_semicolon)


# ===========================================================================
# 3. 截断 / 上限 / 超时
# ===========================================================================
def test_bounds_and_timeout() -> None:
    section("3. 行上限 / 字节上限 / 超时（含真实 4s 客户端超时）")

    def row_cap_truncates():
        backend = FakeBackend(routes=[
            ("RETURN count(*) AS total", [{"total": 251}]),
            ("MATCH (n) RETURN n", [{"n": str(i)} for i in range(201)]),
        ])
        out = ct.build_query(backend, "MATCH (n) RETURN n")
        assert out["rowCount"] == ct.ROW_CAP == 200, out["rowCount"]
        assert out["droppedRows"] == 51, out["droppedRows"]
        assert out["truncated"] is True
        assert "行上限" in out["hint"], out["hint"]
        assert len(out["rows"]) == 200
    check("bounds/row-cap-200-exact-droppedRows", row_cap_truncates)

    def byte_cap_truncates():
        big = "x" * 500
        backend = FakeBackend(default_rows=[{"n": big} for _ in range(200)])
        out = ct.build_query(backend, "MATCH (n) RETURN n")
        assert out["truncated"] is True
        assert out["droppedBytes"] > 0, out
        assert ct._byte_len(out) <= ct.BYTE_CAP, ct._byte_len(out)
        assert "字节上限" in out["hint"], out["hint"]
    check("bounds/byte-cap-20000", byte_cap_truncates)

    def ledger_is_shared_contract():
        ledger = ct.Ledger(["rows"])
        ledger.put("rows", [{"n": 1}])
        out = ledger.finish()
        for key in ("hint", "truncated", "droppedRows", "droppedBytes"):
            assert key in out, key
        assert out["truncated"] is False and out["droppedRows"] == 0
    check("bounds/envelope-always-present", ledger_is_shared_contract)

    def timeout_mapping():
        backend = graph_backend(error=ct.GraphError("timeout", "boom"))
        exc = expect_tool_error(ct.ERR_QUERY_TIMEOUT, ct.build_query, backend,
                                "MATCH (n) RETURN n")
        assert exc.detail.get("timeoutSeconds") == ct.QUERY_TIMEOUT_S == 4.0, exc.detail
    check("timeout/grapherror-maps-to-QUERY_TIMEOUT", timeout_mapping)

    def syntax_mapping():
        backend = graph_backend(error=ct.GraphError("syntax", "invalid input"))
        expect_tool_error(ct.ERR_QUERY_FORM_REJECTED, ct.build_query, backend,
                          "MATCH (n) RETURN n")
    check("timeout/syntax-maps-to-QUERY_FORM_REJECTED", syntax_mapping)

    def timeout_is_four_seconds():
        assert ct.QUERY_TIMEOUT_S == 4.0, ct.QUERY_TIMEOUT_S
        captured = {}

        class FakeSession:
            def post(self, url, **kwargs):
                captured.update(kwargs)
                raise AssertionError("stop-here")

        backend = ct.Neo4jHttpBackend("http://127.0.0.1:1", "neo4j", "pw")
        backend._session = FakeSession()
        try:
            backend.cypher("RETURN 1")
        except Exception:                                     # noqa: BLE001
            pass
        assert captured.get("timeout") == 4.0, captured.get("timeout")
    check("timeout/client-read-timeout-is-4.0s", timeout_is_four_seconds)

    def real_client_timeout_fires():
        """真实 HTTP 端点故意慢 8s ⇒ 客户端 4s 读超时必须触发 QUERY_TIMEOUT。"""
        import http.server
        import socketserver

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                                # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                time.sleep(8.0)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"results":[{"columns":[],"data":[]}],"errors":[]}')

            def log_message(self, *_args):                    # 静音
                return

        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SlowHandler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            backend = ct.Neo4jHttpBackend("http://127.0.0.1:%d" % port, "neo4j", "pw",
                                          timeout=ct.QUERY_TIMEOUT_S)
            svc = service_with(backend)
            started = time.time()
            exc = expect_tool_error(ct.ERR_QUERY_TIMEOUT, svc.dispatch, "cc_query",
                                    {"query": "MATCH (n) RETURN n"})
            elapsed = time.time() - started
            assert 3.0 <= elapsed <= 7.0, "实际耗时 %.2fs，未在 4s 超时附近" % elapsed
            assert "超时" in exc.message, exc.message
        finally:
            httpd.shutdown()
            httpd.server_close()
    check("timeout/real-4s-client-timeout-fires", real_client_timeout_fires)


# ===========================================================================
# 4. 形状（顶层键 == 契约面）与空结果良定义
# ===========================================================================
def test_shapes_and_empty() -> None:
    section("4. 工具形状 + 空结果良定义")

    def shape_cc_guide():
        out = service_with(graph_backend()).dispatch("cc_guide", {})
        assert set(out) == expected_keys("cc_guide"), sorted(set(out))
        assert out["metrics"] and out["objectTypes"] and out["rules"], out
    check("shape/cc_guide-keys-and-nonempty", shape_cc_guide)

    def shape_cc_schema():
        out = service_with(graph_backend()).dispatch("cc_schema", {})
        assert set(out) == expected_keys("cc_schema"), sorted(set(out))
        assert out["objectTypes"] and out["relationTypes"] and out["attributes"]
        assert isinstance(out["naming_discipline"], str) and out["naming_discipline"]
        banned = re.compile(r"(cypher|sparql|rdf|owl|neo4j|graphdb|知识图谱|本体)", re.I)
        assert not banned.search(out["naming_discipline"]), out["naming_discipline"]
    check("shape/cc_schema-keys-and-naming-discipline", shape_cc_schema)

    def shape_cc_query():
        out = service_with(graph_backend()).dispatch("cc_query",
                                                     {"query": "MATCH (n) RETURN n"})
        assert set(out) == expected_keys("cc_query"), sorted(set(out))
        assert out["rowCount"] == len(out["rows"])
    check("shape/cc_query-keys", shape_cc_query)

    def shape_cc_facts():
        out = service_with(graph_backend()).dispatch(
            "cc_facts", {"objectType": "DatabaseSessionPool", "objectId": POOL})
        assert set(out) == expected_keys("cc_facts"), sorted(set(out))
        assert out["objects"]
    check("shape/cc_facts-keys", shape_cc_facts)

    def shape_cc_trace():
        out = service_with(graph_backend()).dispatch(
            "cc_trace", {"startId": POOL, "direction": "both", "maxHops": 2})
        assert set(out) == expected_keys("cc_trace"), sorted(set(out))
        assert out["nodes"] and out["edges"], out
        assert out["hopsUsed"] >= 1
    check("shape/cc_trace-keys-and-expansion", shape_cc_trace)

    def shape_cc_cause():
        out = service_with(graph_backend()).dispatch(
            "cc_cause", {"symptomCodes": [FM_A], "maxHops": 4})
        assert set(out) == expected_keys("cc_cause"), sorted(set(out))
        assert out["candidates"], out
        assert isinstance(out["rankBasis"], str) and "确定性排序" in out["rankBasis"]
        for key in ("tools-setb.yaml", "fault-modes.yaml", "CC_Evidence",
                    "causal-edges.yaml"):
            assert key in out["rankBasis"], "排序依据必须可溯源到 %s" % key
    check("shape/cc_cause-keys-and-rankBasis", shape_cc_cause)

    def shape_cc_propose():
        out = service_with(graph_backend()).dispatch(
            "cc_propose", {"rootCause": FM_C, "evidencePath": ["ev-1"]})
        assert set(out) == expected_keys("cc_propose"), sorted(set(out))
    check("shape/cc_propose-keys", shape_cc_propose)

    def no_uncovered_statements():
        backend = graph_backend()
        svc = service_with(backend)
        for tool, body in (("cc_guide", {}), ("cc_schema", {}),
                           ("cc_query", {"query": "MATCH (n) RETURN n"}),
                           ("cc_facts", {"objectType": "DatabaseSessionPool"}),
                           ("cc_trace", {"startId": POOL, "maxHops": 2}),
                           ("cc_cause", {"symptomCodes": [FM_A]}),
                           ("cc_propose", {"rootCause": FM_C})):
            svc.dispatch(tool, body)
        backend.assert_no_unmatched()
        assert backend.timeouts() == [None] * len(backend.timeouts())
    check("shape/all-statements-covered-by-fixture", no_uncovered_statements)

    def empty_results_are_well_defined():
        empty = FakeBackend(routes=EMPTY_BACKEND_ROUTES)
        svc = service_with(empty)
        bodies = {
            "cc_guide": {},
            "cc_schema": {},
            "cc_query": {"query": "MATCH (n) RETURN n"},
            "cc_facts": {"objectType": "Host"},
            "cc_cause": {"symptomCodes": []},
            "cc_propose": {"rootCause": FM_C},
        }
        for tool, body in bodies.items():
            out = svc.dispatch(tool, body)
            assert set(out) == expected_keys(tool), (tool, sorted(set(out)))
            assert out["truncated"] is False, tool
            assert out["droppedRows"] == 0 and out["droppedBytes"] == 0, tool
            data_keys = expected_keys(tool) - {"hint", "truncated", "droppedRows",
                                               "droppedBytes", "rankBasis",
                                               "naming_discipline",
                                               # 恒定标志（契约要求 cc_propose 恒 dry_run）：
                                               # 它们是执行保证，不是「本次取回的数据」。
                                               "executed", "dryRun"}
            for name in data_keys:
                value = out[name]
                if isinstance(value, (list, dict)):
                    assert not value, "%s.%s 空图下非空：%r" % (tool, name, value)
                elif isinstance(value, bool):
                    assert value is False, "%s.%s=%r" % (tool, name, value)
                elif isinstance(value, (int, float)):
                    assert value == 0, "%s.%s=%r" % (tool, name, value)
                elif isinstance(value, str):
                    assert value == "", "%s.%s=%r" % (tool, name, value)
                else:
                    assert value is None, "%s.%s=%r" % (tool, name, value)
            assert isinstance(out["hint"], str) and out["hint"].strip(), \
                "%s 空结果必须给 hint" % tool
    check("empty/results-well-defined-with-hint", empty_results_are_well_defined)

    def trace_unknown_start_is_empty_not_error():
        out = service_with(FakeBackend()).dispatch("cc_trace", {"startId": "nope:1"})
        assert out["nodes"] == [] and out["edges"] == [] and out["hopsUsed"] == 0
        assert "nope:1" in out["hint"], out["hint"]
    check("empty/cc_trace-unknown-startId-is-well-defined", trace_unknown_start_is_empty_not_error)

    def cause_empty_symptom_list():
        out = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": []})
        assert out["candidates"] == []
        assert "没有找到上游根因候选" in out["hint"], out["hint"]
    check("empty/cc_cause-empty-symptomCodes", cause_empty_symptom_list)

    def cause_unknown_symptom_code():
        exc = expect_tool_error(
            ct.ERR_INVALID_PARAM, service_with(graph_backend()).dispatch,
            "cc_cause", {"symptomCodes": ["NOT-A-CODE"]})
        assert "NOT-A-CODE" in str(exc.detail.get("unknownSymptomCodes")), exc.detail
        assert FM_A in exc.detail.get("acceptedSymptomCodes"), exc.detail
    check("params/cc_cause-unknown-code-lists-accepted", cause_unknown_symptom_code)

    def facts_unknown_object_type():
        expect_tool_error(ct.ERR_INVALID_PARAM, service_with(graph_backend()).dispatch,
                          "cc_facts", {"objectType": "NoSuchType"})
    check("params/cc_facts-unknown-objectType", facts_unknown_object_type)


# ===========================================================================
# 4b. 活体暴露缺陷的回归（这些断言在修复前**真的会失败**）
# ===========================================================================
def test_identifier_space_regressions() -> None:
    section("4b. 活体缺陷回归（统一标识空间 / 密钥属性 / 无 ccfId 邻居）")

    def key_attrs_are_not_empty():
        # 活体 D1：schema/guide 的 keyAttrs 曾恒为 []，因为查询别名是 `ks`
        # 而 Python 读的是 row["keys"]（别名不一致 ⇒ 恒 None）。
        prio = list(ct.KEY_ATTR_PRIORITY)
        assert len(set(prio)) == len(prio), "keyAttrs 优先级表含重复项：%r" % (prio,)
        for tool in ("cc_guide", "cc_schema"):
            out = service_with(graph_backend()).dispatch(tool, {})
            assert out["objectTypes"], tool
            for item in out["objectTypes"]:
                assert item["keyAttrs"], "%s 的 %s.keyAttrs 为空：%r" % (
                    tool, item["objectType"], item)
                assert len(set(item["keyAttrs"])) == len(item["keyAttrs"]), (
                    "%s 的 %s.keyAttrs 有重复项：%r" % (tool, item["objectType"], item["keyAttrs"]))
    check("regress/keyAttrs-non-empty", key_attrs_are_not_empty)

    def schema_and_facts_share_one_identifier_space():
        # 活体 D2：/schema 曾报 CC_DatabaseSessionPool 而 /facts 只认
        # DatabaseSessionPool —— 照抄 schema 会被 INVALID_PARAM 拒。
        backend = graph_backend()
        svc = service_with(backend)
        schema_types = [t["objectType"] for t in svc.dispatch("cc_schema", {})["objectTypes"]]
        guide_types = [t["objectType"] for t in svc.dispatch("cc_guide", {})["objectTypes"]]
        assert schema_types and guide_types
        assert set(schema_types) == set(guide_types), (schema_types, guide_types)
        for name in schema_types:
            assert not name.startswith("CC_"), "objectType 不得是 KG 标签名：%r" % name
            out = svc.dispatch("cc_facts", {"objectType": name})   # 不得抛 INVALID_PARAM
            assert set(out) == expected_keys("cc_facts")
    check("regress/schema-facts-one-identifier-space",
          schema_and_facts_share_one_identifier_space)

    def ids_resolve_through_the_shared_resolver():
        # 活体 D3/D4：裸 `X.ccfId AS src/objectId` 会让没有 ccfId 的目录级节点
        # （CC_FaultMode 等）退化成 null。
        backend = graph_backend()
        svc = service_with(backend)
        for tool, body in (("cc_guide", {}), ("cc_schema", {}),
                           ("cc_facts", {"objectType": "DatabaseSessionPool"}),
                           ("cc_trace", {"startId": POOL, "direction": "both"}),
                           ("cc_cause", {"symptomCodes": [FM_A]}),
                           ("cc_propose", {"rootCause": FM_D})):
            svc.dispatch(tool, body)
        for stmt in backend.statements():
            assert not re.search(r"[A-Za-z_][A-Za-z_0-9]*\.ccfId AS ", stmt), stmt
            if "ccfId" in stmt and re.search(r"AS (objectId|src|dst)\b", stmt):
                assert "coalesce(" in stmt, stmt
        assert not [s for s in backend.statements() if re.search(r"AS ks\b", s)]
    check("regress/ids-via-coalesce-resolver", ids_resolve_through_the_shared_resolver)

    def trace_never_emits_None_or_self_loop():
        # 活体 D4：入边邻居是 CC_FaultMode（无 ccfId）时曾产生
        # {"objectId": "None"} 节点 + 自环边 dn_6001→dn_6001。
        needle = "<-[r]-(b) WHERE type(r) STARTS WITH 'CC_' AND NOT type(r) IN $skipRels"
        routes = list(GRAPH_ROUTES)
        assert needle in [n for n, _ in routes], "夹具失效：找不到入边路由"
        routes[[n for n, _ in routes].index(needle)] = (
            needle,
            [{"src": POOL, "relationType": "CC_AFFECTS", "dst": FM_C,
              "dstType": "FaultMode", "dstLayer": "", "edgeFrom": FM_C, "edgeTo": POOL}])
        out = service_with(FakeBackend(routes=routes)).dispatch(
            "cc_trace", {"startId": POOL, "direction": "in", "maxHops": 1})
        ids = [n["objectId"] for n in out["nodes"]]
        assert "None" not in ids and None not in ids, ids
        assert FM_C in ids, ids
        assert all(e["from"] != e["to"] for e in out["edges"]), out["edges"]
        assert {e["from"] for e in out["edges"]} == {FM_C}, out["edges"]
    check("regress/trace-fault-mode-neighbour", trace_never_emits_None_or_self_loop)

    def trace_drops_unresolvable_neighbour():
        # 负控：邻居 id 无法解析（None）时**丢掉该行**，绝不 stringify 成 "None"。
        needle = "<-[r]-(b) WHERE type(r) STARTS WITH 'CC_' AND NOT type(r) IN $skipRels"
        routes = list(GRAPH_ROUTES)
        routes[[n for n, _ in routes].index(needle)] = (
            needle,
            [{"src": POOL, "relationType": "CC_DANGLING", "dst": None, "dstType": None,
              "dstLayer": "", "edgeFrom": None, "edgeTo": POOL}])
        out = service_with(FakeBackend(routes=routes)).dispatch(
            "cc_trace", {"startId": POOL, "direction": "in", "maxHops": 1})
        assert [n["objectId"] for n in out["nodes"]] == [POOL], out["nodes"]
        assert out["edges"] == [], out["edges"]
    check("regress/trace-drops-unresolvable-neighbour",
          trace_drops_unresolvable_neighbour)


# ===========================================================================
# 5. cc_cause 确定性排序 / 判据三值逻辑
# ===========================================================================
def test_determinism_and_logic() -> None:
    section("5. cc_cause 确定性 + 并列不合并 + 不可判定不用 False 冒充")

    def cause_is_deterministic():
        first = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": [FM_A]})
        second = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": [FM_A]})
        assert json.dumps(first, sort_keys=True, ensure_ascii=False) == \
            json.dumps(second, sort_keys=True, ensure_ascii=False), "同入不同出"
        codes = [c["faultCode"] for c in first["candidates"]]
        assert codes == [FM_C, FM_D], codes
        assert [c["rank"] for c in first["candidates"]] == [1, 2]
        assert [c["hops"] for c in first["candidates"]] == [1, 2]
    check("cause/deterministic-same-input-same-output", cause_is_deterministic)

    def undetermined_not_false():
        out = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": [FM_A]})
        lock = [c for c in out["candidates"] if c["faultCode"] == FM_D][0]
        assert lock["determined"] is False, lock
        assert lock["undeterminedClauses"] >= 1, lock
        record = lock["detectEvidence"][0]
        assert record["outcome"] is None, "未标定阈值必须 outcome=None，不得用 False 冒充"
        assert record["reason"], "不可判定必须给出 reason"
    check("cause/uncalibrated-threshold-undetermined", undetermined_not_false)

    def satisfied_clauses_rank_first():
        out = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": [FM_A]})
        assert out["candidates"][0]["faultCode"] == FM_C
        assert out["candidates"][0]["determined"] is True
        assert out["candidates"][0]["satisfiedClauses"] == 1
    check("cause/determined-with-satisfied-clause-ranks-first", satisfied_clauses_rank_first)

    def path_is_traceable():
        out = service_with(graph_backend()).dispatch("cc_cause", {"symptomCodes": [FM_A]})
        first = out["candidates"][0]
        assert first["path"] == [FM_C, FM_A], first["path"]
        assert first["causalContractIds"] == ["E6"], first["causalContractIds"]
        assert first["pathEdges"][0]["contractId"] == "E6"
        assert first["viaRelation"] == "CC_CAUSES"
    check("cause/path-and-contractId-traceable", path_is_traceable)

    def ties_not_merged():
        rows = [
            {"faultCode": "FM-X", "determined": True, "satisfiedClauses": 2, "hops": 1,
             "relationPriority": "causes"},
            {"faultCode": "FM-Y", "determined": True, "satisfiedClauses": 2, "hops": 1,
             "relationPriority": "causes"},
        ]
        out = ct.rank_candidates(rows)
        assert [r["faultCode"] for r in out] == ["FM-X", "FM-Y"], out
        assert [r["rank"] for r in out] == [1, 2]
        assert len(out) == 2, "并列必须全部返回，不得合并"
    check("rank/ties-not-merged-both-returned", ties_not_merged)

    def relation_priority():
        rows = [
            {"faultCode": "FM-COND", "determined": True, "satisfiedClauses": 1, "hops": 1,
             "relationPriority": "conditional_causes"},
            {"faultCode": "FM-CAUSES", "determined": True, "satisfiedClauses": 1, "hops": 1,
             "relationPriority": "causes"},
        ]
        out = ct.rank_candidates(rows)
        assert [r["faultCode"] for r in out] == ["FM-CAUSES", "FM-COND"], out
    check("rank/CAUSES-before-CONDITIONAL_CAUSES", relation_priority)

    def predicate_needs_two_points():
        clause = {"metricCode": "M", "op": "increased", "role": "distinguishing",
                  "operandKind": "predicate"}
        one = ct.evaluate_clause(clause, {}, {}, {("o", "M"): [("t1", "1")]}, "o")
        assert one["outcome"] is None and one["reason"], one
        two = ct.evaluate_clause(clause, {}, {},
                                 {("o", "M"): [("t1", "1"), ("t2", "5")]}, "o")
        assert two["outcome"] is True, two
        down = ct.evaluate_clause(clause, {}, {},
                                  {("o", "M"): [("t1", "5"), ("t2", "1")]}, "o")
        assert down["outcome"] is False, down
    check("logic/predicate-increased-needs-two-points", predicate_needs_two_points)

    def uncalibrated_threshold_is_none():
        value, source, meta = ct.resolve_threshold("threshold:THR-LOCK#warning",
                                                   {"THR-LOCK": {"thrWarning": None}})
        assert value is None and meta["calibrated"] is False, (value, meta)
        value2, _s2, meta2 = ct.resolve_threshold("threshold:THR-LOCK#critical",
                                                 {"THR-LOCK": {"thrCritical": 30}})
        assert value2 == 30 and meta2["calibrated"] is True
        value3, _s3, meta3 = ct.resolve_threshold("threshold:NOPE#warning", {})
        assert value3 is None and meta3["calibrated"] is False
    check("logic/resolve-threshold-uncalibrated", uncalibrated_threshold_is_none)

    def bool_is_not_a_number():
        assert ct.to_number(True) is None, "bool 不得当数字"
        assert ct.to_number("3.5") == 3.5
        assert ct.truthy(None) is None
        assert ct.truthy(0) is False and ct.truthy(2) is True
    check("logic/to_number-rejects-bool", bool_is_not_a_number)

    def instance_selection_is_stable():
        # 同一故障码多个受影响实例 ⇒ 必须选 (命中数优先, objectId 字典序) 且可复现
        rows = [dict(r) for r in AFFECTS_CAUSE if r["fmCode"] == FM_D]
        routes = list(GRAPH_ROUTES)
        # 受影响实例查询是 cause 专用的那条（带 ccfType）：追加第二个同码实例 q-0，
        # 若实现按「字典序」而非「到达顺序」挑实例，就必须选 q-0。
        needle = "CC_AFFECTS]->(n) RETURN f.fmCode AS fmCode, coalesce(n.ccfId"
        routes[[n for n, _ in routes].index(needle)] = (
            needle,
            [dict(r) for r in AFFECTS_CAUSE if r["fmCode"] == FM_C] + rows +
            [{"fmCode": FM_D, "objectId": "query:q-0", "objectType": "Query"}])
        first = service_with(FakeBackend(routes=routes)).dispatch(
            "cc_cause", {"symptomCodes": [FM_A]})["candidates"]
        lock = [c for c in first if c["faultCode"] == FM_D][0]
        assert lock["componentInstance"] == "query:q-0", lock["componentInstance"]
    check("cause/instance-selection-deterministic", instance_selection_is_stable)


# ===========================================================================
# 6. 载体隔离：绝不把 ONTO 侧标签/关系当 KG 工具面
# ===========================================================================
def test_carrier_isolation() -> None:
    section("6. 载体隔离（CC_ / ccfType 划界，描述不泄漏查询语言）")

    def queries_are_cc_scoped():
        backend = graph_backend()
        svc = service_with(backend)
        for tool, body in (("cc_guide", {}), ("cc_schema", {}),
                           ("cc_facts", {"objectType": "DatabaseSessionPool"}),
                           ("cc_trace", {"startId": POOL}), ("cc_cause", {"symptomCodes": [FM_A]}),
                           ("cc_propose", {"rootCause": FM_C})):
            svc.dispatch(tool, body)
        # 每个枚举型查询都必须带**显式划界**：
        #   KG ABox  ⇒ CC_ 标签前缀 / ccfType / ccfId
        #   环境级动作目录 ⇒ actionId / runbookId（这是 KG 与 ONTO 共享的环境知识，
        #                    不是 KG ABox，故不能用 ccfType 划界；此豁免必须窄）
        for statement in backend.statements():
            if "Symptom" in statement or "OntClass" in statement or "OntProperty" in statement:
                raise AssertionError("工具面查询触及 ONTO 侧标签：%s" % statement)
            if not re.search(r"MATCH \(n\)$|MATCH \(n\) ", statement):
                continue
            scoped_kg = any(k in statement for k in ("ccfType", "CC_", "ccfId"))
            scoped_env = any(k in statement for k in ("actionId", "runbookId"))
            if not (scoped_kg or scoped_env):
                raise AssertionError("查询未按载体划界：%s" % statement)
            if scoped_env and not scoped_kg:      # 豁免只对目录主键查询开放
                assert "n.actionId IS NOT NULL" in statement or ":Runbook" in statement, statement
    check("carrier/statements-scoped-to-CC_-and-ccfType", queries_are_cc_scoped)

    def descriptions_have_no_banned_words():
        banned = re.compile(
            r"(cypher|sparql|rdf|owl|三元组|属性图|property\s*graph|知识图谱|本体|neo4j|"
            r"graphdb|turtle|ttl|shacl|推理机|reasoner)", re.I)
        for name, spec in ct.TOOLFACE.items():
            hit = banned.search(spec["description"] or "")
            assert hit is None, "工具 %s 描述含禁用词 %r" % (name, hit.group(0) if hit else "")
    check("carrier/descriptions-free-of-banned-words", descriptions_have_no_banned_words)

    def params_match_contract_shape():
        assert ct.CARRIER == "kg" and ct.ARM == "KG-Q"
        assert ct.MANIFEST_SCHEMA == "dsh-toolface-manifest/1"
        total_params = sum(len(s["params"]) for s in ct.TOOLFACE.values())
        assert total_params == 16, total_params
        tool_returns = sum(len(s["returns"]) for s in ct.TOOLFACE.values())
        assert tool_returns == 24, tool_returns
        assert len(ct.TOOLFACE) == 8
    check("carrier/contract-boundary-counts-8-16-24", params_match_contract_shape)


# ===========================================================================
# 6b. nativeRef：条目级原生标识（逐条取自本机返回值；缺口必须显式记账）
# ===========================================================================
def test_native_ref_disclosure() -> None:
    section("6b. nativeRef（条目级原生标识 + 缺口负控）")

    def schema_three_lists_carry_native_ref():
        out = service_with(graph_backend()).dispatch("cc_schema", {})
        assert out["objectTypes"], out
        for item in out["objectTypes"]:
            assert item["nativeRef"] == "CC_" + item["objectType"], item
        rels = out["relationTypes"]
        assert rels and all(i["nativeRef"] == i["relationType"] for i in rels), rels
        attrs = out["attributes"]
        assert attrs and all(i["nativeRef"] == i["attribute"] for i in attrs), attrs[:3]
    check("nativeRef/schema-three-lists-carry-native-id", schema_three_lists_carry_native_ref)

    def guide_object_types_carry_native_ref():
        out = service_with(graph_backend()).dispatch("cc_guide", {})
        assert out["objectTypes"], out
        for item in out["objectTypes"]:
            assert item["nativeRef"] == "CC_" + item["objectType"], item
        assert "nativeRef=null" not in (out["hint"] or ""), out["hint"]
    check("nativeRef/guide-objectTypes-carry-native-id", guide_object_types_carry_native_ref)

    def missing_is_null_and_accounted():
        # 负控：后端不返回原生标签 ⇒ 必须置 null，且缺口写进 hint，绝不静默、绝不按规则拼
        schema = service_with(no_native_backend()).dispatch("cc_schema", {})
        assert schema["objectTypes"], schema
        assert all(i["nativeRef"] is None for i in schema["objectTypes"]), schema["objectTypes"]
        hint = schema["hint"] or ""
        assert "nativeRef=null" in hint, hint
        assert str(len(schema["objectTypes"])) in hint, hint
        guide = service_with(no_native_backend()).dispatch("cc_guide", {})
        assert guide["objectTypes"], guide
        assert all(i["nativeRef"] is None for i in guide["objectTypes"]), guide["objectTypes"]
        assert "nativeRef=null" in (guide["hint"] or ""), guide["hint"]
    check("nativeRef/missing-is-null-and-accounted", missing_is_null_and_accounted)

    def field_set_is_additive_only():
        out = service_with(graph_backend()).dispatch("cc_schema", {})
        expect = {
            "objectTypes": {"objectType", "keyAttrs", "nativeRef"},
            "relationTypes": {"relationType", "from", "to", "cardinality", "edgeCount",
                              "nativeRef"},
            "attributes": {"owner", "attribute", "valueType", "nativeRef"},
        }
        for name, keys in expect.items():
            assert out[name], name
            for item in out[name]:
                assert set(item) == keys, (name, sorted(item))
        guide = service_with(graph_backend()).dispatch("cc_guide", {})
        for item in guide["objectTypes"]:
            assert set(item) == {"objectType", "layer", "keyAttrs", "nativeRef"}, sorted(item)
    check("nativeRef/field-set-additive-only", field_set_is_additive_only)

    def helpers_never_guess():
        assert ct._native_ref(None) is None and ct._native_ref("") is None
        assert ct._native_ref("CC_Host") == "CC_Host"
        assert ct._native_label_of({"objectType": "Host", "nativeLabels": ["CC_Host"]}) == "CC_Host"
        for empty in ({"objectType": "Host", "nativeLabels": []},
                      {"objectType": "Host", "nativeLabels": None},
                      {"objectType": "Host"}):
            assert ct._native_label_of(empty) is None, empty
        assert ct._native_ref_hint([]) == ""
        text = ct._native_ref_hint([("objectTypes", "Meta")])
        assert "nativeRef=null" in text and "objectTypes:Meta" in text, text
    check("nativeRef/helpers-never-guess", helpers_never_guess)


# ===========================================================================
# 7. manifest 产出 + 真·G24 门禁 loader 交叉验证
# ===========================================================================
def repo_root() -> str:
    return os.path.dirname(os.path.dirname(HERE))


def load_gate_module():
    path = os.path.join(repo_root(), "shared", "verify", "toolset_parity_gate.py")
    spec = importlib.util.spec_from_file_location("m32_gate_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_against_real_gate() -> None:
    section("7. --emit-manifest 产物被真·G24 门禁 loader 解析")

    def manifest_shape():
        manifest = ct.emit_manifest(repo_root())
        assert manifest["schema"] == "dsh-toolface-manifest/1"
        assert manifest["carrier"] == "kg" and manifest["arm"] == "KG-Q"
        assert manifest["source"] == "scheme1-kg/service/cc_tools.py", manifest["source"]
        assert len(manifest["tools"]) == 8 and all(t["params"] or t["name"] in ("cc_guide", "cc_schema") for t in manifest["tools"]), [t["name"] for t in manifest["tools"]]
        for tool in manifest["tools"]:
            names = [r["name"] for r in tool["returns"]]; assert tool["name"] != "read_facts" or names == ["objects", "relations", "observations", "source", "hint", "truncated", "droppedRows", "droppedBytes"], (tool["name"], names)
            for env in ("hint", "truncated", "droppedRows", "droppedBytes"):
                assert env in names, (tool["name"], env)
            # 契约里 cc_guide / cc_schema 的参数表为空 —— 必须如实为空，不得凑数。
            if tool["name"] in ("cc_guide", "cc_schema"):
                assert tool["params"] == [], (tool["name"], tool["params"])
            elif tool["name"] == "read_facts":
                assert [p["name"] for p in tool["params"]] == ["layers", "sections", "scope", "max_records", "max_bytes"] and all(p["required"] is False for p in tool["params"]), tool["params"]
        text = json.dumps(manifest, ensure_ascii=False)
        for secret in ("password", "NEO4J_AUTH", "7475", "http://"):
            assert secret not in text, "清单不得含端点/凭据：%s" % secret
    check("manifest/shape-and-no-secrets", manifest_shape)

    def manifest_sha_matches_implementation():
        manifest = ct.emit_manifest(repo_root())
        actual = ct.sha256_file(os.path.join(repo_root(), "scheme1-kg", "service",
                                             "cc_tools.py"))
        assert manifest["sourceSha256"] == actual, (manifest["sourceSha256"], actual)
        assert manifest["sources"][0]["sha256"] == actual
    check("manifest/sourceSha256-measured-at-runtime", manifest_sha_matches_implementation)

    def write_manifest_bytes():
        out = os.path.join(tempfile.mkdtemp(prefix="m32_manifest_"), "_m32_manifest.json")
        out = os.path.abspath(out)
        ct.write_manifest(out, repo_root())
        raw = open(out, "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf"), "清单不得有 BOM"
        assert b"\r\n" not in raw, "清单必须 LF"
        assert raw.endswith(b"\n")
        json.loads(raw.decode("utf-8"))
    check("manifest/written-utf8-no-bom-lf", write_manifest_bytes)

    def gate_loads_manifest_and_faces_match():
        gate = load_gate_module()
        contract = gate.load_yaml(os.path.join(repo_root(), "shared", "model",
                                              "tools-setb.yaml"))
        manifest = ct.emit_manifest(repo_root())
        cface = gate.contract_face(contract)
        fkg = gate.manifest_face(manifest, "kg")            # ★ 真 loader
        errors, prov = gate.verify_sources(manifest, "kg", repo_root(), [])
        assert errors == [], errors
        assert all(row[4] == "OK" for row in prov), prov
        # 两侧槽位用同一份 KG 面：工具名/参数/返回集合的「两侧相等」自动成立，
        # 而 G24-04/05 对**契约**的逐项比对才是真检查（绝不伪造方案2 清单）。
        errors2, warns2, info = gate.compare_faces(cface, fkg, fkg, 8)
        assert errors2 == [], [e for e in errors2]
        assert (info["compared_tools"], info["compared_params"], info["compared_return_fields"]) == (8, 16, 56), info
        # 契约侧静态计数由门禁自己的 API 现算（compare_faces 的 info 只含跨源检查键），
        # 再与契约 g24.boundary_counts 的声明逐键对账 —— 这一步才是真检查。
        counts = gate.contract_counts(contract, cface)
        decl = (contract.get("g24") or {}).get("boundary_counts") or {}
        assert counts["compared_tools"] == 8, counts
        assert counts["compared_params"] == 11, counts
        assert counts["compared_return_fields"] == 52, counts
        assert counts["tool_level_return_fields"] == 20, counts
        assert counts["envelope_fields"] == 4, counts
        assert counts["carrier_specific_divergences"] == 1, counts
        assert counts == {k: decl[k] for k in counts}, (counts, decl)
        assert gate.boundary_check(contract, cface) == [], gate.boundary_check(contract, cface)
    check("manifest/g24-loader-parses-and-compares-clean", gate_loads_manifest_and_faces_match)

    def gate_cli_single_side_is_blocked():
        """只给 --kg-manifest：门必须 BLOCKED exit 2（单侧无法比对）。"""
        out_path = os.path.join(tempfile.mkdtemp(prefix="m32_gate_cli_"), "_m32_manifest.json")
        ct.write_manifest(out_path, repo_root())
        gate_path = os.path.join(repo_root(), "shared", "verify", "toolset_parity_gate.py")
        proc = subprocess.run(
            [sys.executable, gate_path, "--repo-root", repo_root(),
             "--kg-manifest", out_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert proc.returncode == 2, "exit=%s\n%s" % (proc.returncode, proc.stdout)
        assert "RESULT: BLOCKED" in proc.stdout, proc.stdout[-1500:]
        assert "未提供 --onto-manifest" in proc.stdout, proc.stdout[-1500:]
    check("manifest/g24-cli-single-side-BLOCKED-exit2", gate_cli_single_side_is_blocked)

    def gate_cli_same_file_is_param_error():
        out_path = os.path.join(tempfile.mkdtemp(prefix="m32_gate_same_"), "_m32_manifest.json")
        gate_path = os.path.join(repo_root(), "shared", "verify", "toolset_parity_gate.py")
        proc = subprocess.run(
            [sys.executable, gate_path, "--repo-root", repo_root(),
             "--kg-manifest", out_path, "--onto-manifest", out_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert proc.returncode == 2, proc.stdout
        assert "PARAM_ERROR" in proc.stdout, proc.stdout[-800:]
    check("manifest/g24-cli-same-file-PARAM_ERROR", gate_cli_same_file_is_param_error)

    def gate_selftest_still_passes():
        gate_path = os.path.join(repo_root(), "shared", "verify", "toolset_parity_gate.py")
        proc = subprocess.run([sys.executable, gate_path, "--selftest", "--repo-root",
                               repo_root()], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        assert proc.returncode == 0, proc.stdout[-1200:]
    check("manifest/g24-selftest-unaffected", gate_selftest_still_passes)


# ===========================================================================
# 8. FastAPI 形状（本机若无 fastapi 则跳过，不算失败）
# ===========================================================================
def test_http_layer() -> None:
    section("8. HTTP 层（fastapi 可用时）")
    try:
        import fastapi  # noqa: F401
    except Exception as exc:                                  # noqa: BLE001
        print("  SKIP  HTTP 层（本机无 fastapi：%s）—— 真机 /opt/cc-venv 有 fastapi" % exc)
        return

    def openapi_has_eight_paths():
        from fastapi.testclient import TestClient
        app = ct.create_app(service_with(graph_backend()))
        client = TestClient(app)
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        for name, tool_spec in ct.TOOLFACE.items():
            assert tool_spec["path"] in paths, tool_spec["path"]
        assert "/health" in paths
        assert len([p for p in paths if p != "/health"]) == 8
    check("http/openapi-eight-tools", openapi_has_eight_paths)

    def health_and_error_envelope():
        from fastapi.testclient import TestClient
        client = TestClient(ct.create_app(service_with(graph_backend())))
        health = client.get("/health").json()
        assert health["carrier"] == "kg" and health["status"] == "ok"
        assert health["snapshotId"] == "ccf-20260928T155516Z-937b79"
        bad = client.post("/query", json={"query": "MATCH (n) DELETE n"})
        assert bad.status_code == 400, bad.status_code
        body = bad.json()
        assert body["error"]["code"] == ct.ERR_FORBIDDEN_TOKEN, body
        missing = client.post("/facts", json={})
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == ct.ERR_INVALID_PARAM
        unknown = client.post("/facts", json={"objectType": "Host", "zzz": 1})
        assert unknown.status_code == 400
        assert unknown.json()["error"]["code"] == ct.ERR_INVALID_PARAM
    check("http/error-envelope-400", health_and_error_envelope)


# ===========================================================================
# 9. 真机 smoke（CC_M32_LIVE=1 时执行；默认跳过）
# ===========================================================================
LIVE_TOOL_BODIES = [
    ("cc_guide", {}),
    ("cc_schema", {}),
    ("cc_query", {"query": "MATCH (n) WHERE n.ccfId IS NOT NULL RETURN n.ccfId AS id, "
                           "n.ccfType AS objectType LIMIT 5"}),
    ("cc_facts", {"objectType": "DatabaseSessionPool"}),
    ("cc_trace", {"startId": "dbpool:dn_6001-pool", "direction": "both", "maxHops": 2}),
    ("cc_cause", {"symptomCodes": ["FM-APP-POOL-EXHAUSTED"], "maxHops": 4}),
    ("cc_propose", {"rootCause": "FM-DB-LOCK-WAIT"}),
    # 第 8 个工具（中性取数面）。body 必须为空：`case_id` 由服务进程的环境/快照决定，
    # 请求体里带 `case_id` 会 400 UNKNOWN_PARAM（D42/D48 已登记的进程级不可变语义）。
    ("read_facts", {}),
]

LIVE_NEGATIVE_CONTROLS = [
    ("write-token", ct.ERR_FORBIDDEN_TOKEN, {"query": "MATCH (n) SET n.x = 1 RETURN n"}),
    ("multi-statement", ct.ERR_MULTI_STATEMENT_REJECTED,
     {"query": "MATCH (n) RETURN n; MATCH (m) RETURN m"}),
    ("external-io", ct.ERR_EXTERNAL_IO_FORBIDDEN,
     {"query": "MATCH (n) CALL apoc.load.json('http://e/x') YIELD value RETURN value"}),
    ("form", ct.ERR_QUERY_FORM_REJECTED, {"query": "EXPLAIN MATCH (n) RETURN n"}),
    ("deep-chain", ct.ERR_DEPTH_LIMIT_EXCEEDED,
     {"query": "MATCH p=(a:CC_FaultMode)-[:CC_CAUSES*1..5]->(b) RETURN b"}),
    ("cause-depth", ct.ERR_DEPTH_LIMIT_EXCEEDED, {"symptomCodes": ["X"], "maxHops": 5,
                                                  "_tool": "cc_cause"}),
]


class HttpSmokeClient:
    """真机 smoke 的 HTTP 传输：只打已部署的服务（:8301），

    这样本机**不需要**持有任何图口令 —— 口令只存在于真机服务进程的环境里。
    """

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        import requests                                     # noqa: PLC0415
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def health(self) -> dict:
        resp = self.session.get(self.base_url + "/health", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def dispatch(self, tool: str, body) -> dict:
        path = ct.TOOLFACE[tool]["path"]
        resp = self.session.post(self.base_url + path, json=body, timeout=self.timeout)
        try:
            payload = resp.json()
        except Exception as exc:                            # noqa: BLE001
            raise AssertionError("HTTP %s 非 JSON：%s" % (resp.status_code, exc))
        if resp.status_code != 200:
            err = payload.get("error") or {}
            detail = err.get("detail") or {}
            if not isinstance(detail, dict):
                detail = {"detail": detail}
            raise ct.ToolError(err.get("code") or ct.ERR_INVALID_PARAM,
                               err.get("message") or ("HTTP %s" % resp.status_code), **detail)
        return payload


def live_smoke() -> None:
    section("9. 真机 smoke（快照 ccf-20260928T155516Z-937b79，只读）")
    url = (os.environ.get("CC_M32_SMOKE_URL") or "").strip()
    if url:
        transport = HttpSmokeClient(url)
        health = transport.health()
        print("  transport: HTTP %s（已部署服务；本机不持有图口令）" % url)
        print("  /health: status=%s carrier=%s arm=%s snapshotId=%r database=%s "
              "rowCap=%s byteCap=%s queryTimeoutS=%s"
              % (health.get("status"), health.get("carrier"), health.get("arm"),
                 health.get("snapshotId"), health.get("database"), health.get("rowCap"),
                 health.get("byteCap"), health.get("queryTimeoutS")))
        snapshot = health.get("snapshotId")
    else:
        backend = ct.build_backend_from_env()
        transport = service_with(backend)
        print("  transport: 进程内直连 %s db=%s timeout=%.1fs"
              % (backend.endpoint, backend.database, backend.timeout))
        snapshot = transport.snapshot_id()
    print("  :CC_Meta.snapshotId = %r" % snapshot)
    assert snapshot == "ccf-20260928T155516Z-937b79", \
        "活体快照与任务书不符：%r（不要继续断言真机读数）" % snapshot

    for tool, body in LIVE_TOOL_BODIES:
        started = time.time()
        try:
            out = transport.dispatch(tool, body)
        except ct.ToolError as exc:
            print("  %-11s ERROR %s: %s" % (tool, exc.code, exc.message))
            raise
        elapsed = (time.time() - started) * 1000.0
        keys = sorted(out)
        shape = {k: (len(v) if isinstance(v, (list, str, dict)) else v)
                 for k, v in out.items() if k != "rankBasis"}
        print("  %-11s %7.1fms keys=%s summary=%s" % (tool, elapsed, keys,
                                                      json.dumps(shape, ensure_ascii=False)))
        if tool == "cc_cause":
            print("    rankBasis[:160]=%r" % out["rankBasis"][:160])
            for cand in out["candidates"][:3]:
                print("    cand rank=%s code=%s hops=%s satisfied=%s determined=%s "
                      "instance=%s path=%s" % (cand["rank"], cand["faultCode"], cand["hops"],
                                               cand["satisfiedClauses"], cand["determined"],
                                               cand["componentInstance"], cand["path"]))
        assert set(out) == expected_keys(tool), (tool, sorted(set(out)))
        assert out["truncated"] in (True, False)
        if tool in ("cc_guide", "cc_schema", "cc_facts"):
            non_empty = [k for k, v in out.items()
                         if isinstance(v, (list, dict)) and v]
            print("    non-empty data fields: %s" % (non_empty or "（全部为空 ⇒ 需良定义空返回）"))

    for name, code, body in LIVE_NEGATIVE_CONTROLS:
        body = dict(body)
        tool = body.pop("_tool", "cc_query")
        started = time.time()
        exc = expect_tool_error(code, transport.dispatch, tool, body)
        print("  negative/%-15s %6.1fms -> %s | %s" % (name, (time.time() - started) * 1000.0,
                                                       exc.code, exc.message[:120]))


# ===========================================================================
# main
# ===========================================================================
def main() -> int:
    print("M3.2 Set B 八工具（KG 侧）测试 —— service=%s" % ct.__file__)
    test_guards()
    test_negative_controls_are_falsifiable()
    test_bounds_and_timeout()
    test_shapes_and_empty()
    test_identifier_space_regressions()
    test_determinism_and_logic()
    test_carrier_isolation()
    test_native_ref_disclosure()
    test_manifest_against_real_gate()
    test_http_layer()

    live = os.environ.get("CC_M32_LIVE") == "1"
    if live:
        try:
            live_smoke()
        except Exception as exc:                              # noqa: BLE001
            FAILED.append(("live/smoke", "%s: %s" % (type(exc).__name__, exc)))
            print("  FAIL  live/smoke  %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
        else:
            PASSED.append("live/smoke")
            print("  PASS  live/smoke")
    else:
        print("\n== 9. 真机 smoke 已跳过（设 CC_M32_LIVE=1 执行；只读）==")

    print("\ntest: %d/%d PASS" % (len(PASSED), len(PASSED) + len(FAILED)))
    if FAILED:
        for name, detail in FAILED:
            print("  FAILED %s :: %s" % (name, detail))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
