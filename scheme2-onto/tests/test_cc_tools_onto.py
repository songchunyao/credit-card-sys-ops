# -*- coding: utf-8 -*-
"""M3.3 —— Set B 八工具知识服务（方案2/ONTO 侧）离线单测 + 可选真机 smoke。

运行：
    python scheme2-onto\\tests\\test_cc_tools_onto.py               # 离线（不连 GraphDB）
    CC_M33_LIVE=1 python scheme2-onto\\tests\\test_cc_tools_onto.py    # 附加真机 smoke（只读）

设计原则（对应 M3.3 硬验收）：
  * 守卫的每一条拒绝路径都有**负控**：给定输入 ⇒ 期望的逐字错误码。
  * 负控**真能失败**：用「源码级变异 + 重新加载」把守卫阉掉（写 token 表清空、
    外部 IO 表清空、深度上限放宽、D4 行过滤短路、数组元素类型检查短路），
    对应负控必须随之失败 —— 否则该负控只是恒真断言。
  * 「声明↔接受」闭环：把 /schema 声明的 objectType 原样喂给 /facts，
    必须不出现 INVALID_PARAM；不在声明内的类型必须被拒。
  * 形状断言以**真·G24 门禁模块**的 contract_face/manifest_face/compare_faces 为准，
    而不是以本实现的 TOOLFACE 为准（禁止自证）。

只读保证：除离线 FakeBackend 外不碰任何数据库；变异文件只写在 tmp/_m33_mutant_*.py。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
import traceback

import yaml
from fastapi.testclient import TestClient

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SERVICE_DIR = os.path.join(REPO_ROOT, "scheme2-onto", "service")
SERVICE_PATH = os.path.join(SERVICE_DIR, "cc_tools_onto.py")
TMP_DIR = os.path.join(REPO_ROOT, "tmp")
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK ⇒ 中文报表会崩
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                            # noqa: BLE001
        pass

import cc_tools_onto as ct  # noqa: E402

TBOX = ct.ONTO_BASE + "graph/tbox"
SNAP = ct.ONTO_BASE + "graph/ccf-TEST"


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
        print("  FAIL  %-52s %s" % (name, exc))
    except Exception as exc:                              # noqa: BLE001
        FAILED.append((name, "%s: %s" % (type(exc).__name__, exc)))
        print("  FAIL  %-52s %s: %s" % (name, type(exc).__name__, exc))
        traceback.print_exc()
    else:
        PASSED.append(name)
        print("  PASS  %s" % name)


def section(title: str) -> None:
    print("\n== %s ==" % title)


def raises_code(mod, code: str, fn, *args, **kwargs):
    """调用 fn(*args)，返回 (是否抛出, 实际码, 异常对象)。"""
    try:
        fn(*args, **kwargs)
    except mod.ToolError as exc:
        return True, exc.code, exc
    except Exception as exc:                              # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc), exc
    return False, None, None


def expect_code(mod, code: str, fn, *args, **kwargs):
    ok, actual, exc = raises_code(mod, code, fn, *args, **kwargs)
    assert ok, "期望 ToolError(%s)，实际未抛出" % code
    assert actual == code, "期望错误码 %s，实际 %s（%s）" % (code, actual, getattr(exc, "message", ""))
    return exc


def expect_ok(mod, fn, *args, **kwargs):
    ok, actual, exc = raises_code(mod, None, fn, *args, **kwargs)
    assert not ok, "期望正常返回，实际抛出 %s（%s）" % (actual, getattr(exc, "message", ""))
    return kwargs.pop("__unused", None)


# ===========================================================================
# 离线替身：不产生任何网络 IO
# ===========================================================================
class FakeBackend:
    """记录查询文本的假后端；responder(query, calls) 返回行列表。"""

    def __init__(self, responder=None):
        self.calls: list = []
        self.responder = responder

    def query(self, sparql, dataset=None, accept=None, timeout=None):
        self.calls.append({"sparql": sparql, "dataset": list(dataset or []), "accept": accept})
        rows = []
        if self.responder is not None:
            rows = self.responder(sparql, self.calls) or []
        return {"columns": [], "rows": rows, "elapsedMs": 0.0}


def make_service(mod=ct, responder=None):
    backend = FakeBackend(responder)
    service = mod.CcToolsService(backend, TBOX, SNAP, "ccf-TEST")
    return service, backend


def injected_index(names, key_attrs=None):
    return {
        "counts": {name: 1 for name in names},
        "declared": sorted(names),
        "keyAttrs": key_attrs or {name: ["ccfId"] for name in names},
        "attrRows": [],
    }


# ===========================================================================
# 1. 只读守卫负控（SPARQL 侧）
# ===========================================================================
Q_WRITE_INSERT = "INSERT DATA { <urn:a> <urn:b> <urn:c> }"
Q_WRITE_DELETE = "DELETE WHERE { ?s ?p ?o }"
Q_WRITE_CLEAR = "CLEAR GRAPH <urn:g>"
Q_WRITE_DROP = "DROP GRAPH <urn:g>"
Q_WRITE_LOAD = "LOAD <http://example.org/x.ttl> INTO GRAPH <urn:g>"
Q_WRITE_SERVICE = "SELECT * WHERE { SERVICE <http://evil.example/sparql> { ?s ?p ?o } }"
Q_WRITE_GRAPH = "SELECT * WHERE { GRAPH <urn:g> { ?s ?p ?o } }"
Q_WRITE_FROM = "SELECT * FROM <urn:g> WHERE { ?s ?p ?o }"
Q_FORM_MODIFY = "MODIFY <urn:g> { ?s ?p ?o }"
Q_FORM_DELETE_DATA = "DELETE DATA { <urn:a> <urn:b> <urn:c> }"
Q_MULTI = "SELECT * WHERE { ?s ?p ?o } ; SELECT * WHERE { ?s ?p ?o }"
Q_PATH_PLUS = "SELECT ?o WHERE { <urn:a> <urn:p>+ ?o }"
Q_PATH_STAR = "SELECT ?o WHERE { <urn:a> <urn:p>* ?o }"
Q_PATH_OPEN = "SELECT ?o WHERE { <urn:a> <urn:p>{1,} ?o }"
Q_PATH_BIG = "SELECT ?o WHERE { <urn:a> <urn:p>{1,9} ?o }"
Q_PATH_OK = "SELECT ?o WHERE { <urn:a> <urn:p>{1,4} ?o }"
Q_SELECT_OK = "SELECT ?s WHERE { ?s ?p ?o }"
Q_PREFIX_OK = "PREFIX cc: <https://credit-card-ops.ai/onto/topology#> SELECT ?s WHERE { ?s a cc:Container }"
Q_TRAILING_SEMI = "SELECT ?s WHERE { ?s ?p ?o } ;"
# M3.4 本轮补的缺口：`{,n}`（低界缺省 = 0）必须与 `{0,n}` 共用同一上界判据
Q_PATH_OPEN_LOW_OK = "SELECT ?o WHERE { <urn:a> <urn:p>{,4} ?o }"
Q_PATH_OPEN_LOW_BIG = "SELECT ?o WHERE { <urn:a> <urn:p>{,5} ?o }"
Q_PATH_OPEN_LOW_SPACED = "SELECT ?o WHERE { <urn:a> <urn:p>{ , 5 } ?o }"
Q_PATH_OPEN_LOW_HUGE = "SELECT ?o WHERE { <urn:a> <urn:p>{,99} ?o }"


def run_guard_controls(mod, label="ct"):
    print("  -- 守卫负控（模块=%s）" % label)
    cases = [
        ("write/INSERT", "FORBIDDEN_TOKEN", Q_WRITE_INSERT),
        ("write/DELETE", "FORBIDDEN_TOKEN", Q_WRITE_DELETE),
        ("write/CLEAR GRAPH", "FORBIDDEN_TOKEN", Q_WRITE_CLEAR),
        ("write/DROP GRAPH", "FORBIDDEN_TOKEN", Q_WRITE_DROP),
        ("write/DELETE DATA", "FORBIDDEN_TOKEN", Q_FORM_DELETE_DATA),
        ("external/LOAD", "EXTERNAL_IO_FORBIDDEN", Q_WRITE_LOAD),
        ("external/SERVICE", "EXTERNAL_IO_FORBIDDEN", Q_WRITE_SERVICE),
        ("cross/GRAPH", "EXTERNAL_IO_FORBIDDEN", Q_WRITE_GRAPH),
        ("cross/FROM", "EXTERNAL_IO_FORBIDDEN", Q_WRITE_FROM),
        ("form/MODIFY", "QUERY_FORM_REJECTED", Q_FORM_MODIFY),
        ("multi/semicolon", "MULTI_STATEMENT_REJECTED", Q_MULTI),
        ("depth/plus", "DEPTH_LIMIT_EXCEEDED", Q_PATH_PLUS),
        ("depth/star", "DEPTH_LIMIT_EXCEEDED", Q_PATH_STAR),
        ("depth/open", "DEPTH_LIMIT_EXCEEDED", Q_PATH_OPEN),
        ("depth/upper>4", "DEPTH_LIMIT_EXCEEDED", Q_PATH_BIG),
    ]
    lines = []
    for name, code, query in cases:
        exc = expect_code(mod, code, mod.guard_query, query)
        lines.append("%s=%s" % (name, exc.code))
    # 允许的形式不得被误杀
    for query in (Q_SELECT_OK, Q_PREFIX_OK, Q_TRAILING_SEMI, Q_PATH_OK,
                  "ASK { ?s ?p ?o }", "DESCRIBE <urn:a>", "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"):
        expect_ok(mod, mod.guard_query, query)
    print("     逐字错误码：%s" % " | ".join(lines))
    return lines


def test_guard_controls():
    run_guard_controls(ct, "ct")


def test_guard_force_limit_and_count():
    query, injected = ct.apply_force_limit(Q_SELECT_OK, "SELECT")
    assert injected is True, "无 LIMIT 的 SELECT 必须注入 LIMIT"
    assert re.search(r"LIMIT\s+%d\b" % (ct.ROW_CAP + 1), query), query
    query2, injected2 = ct.apply_force_limit("SELECT ?s WHERE { ?s ?p ?o } LIMIT 5", "SELECT")
    assert injected2 is False, "已有 LIMIT 不得改写"
    query3, injected3 = ct.apply_force_limit("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", "SELECT")
    assert injected3 is False, "聚合查询不得注入 LIMIT"
    _, injected4 = ct.apply_force_limit("ASK { ?s ?p ?o }", "ASK")
    assert injected4 is False
    counted = ct.build_count_query("PREFIX cc: <urn:cc#>\nSELECT ?s WHERE { ?s a cc:Container }")
    assert counted.startswith("PREFIX cc: <urn:cc#>"), "用户 PREFIX 必须留在计数查询顶层"
    assert "COUNT(*) AS ?totalRows" in counted


def test_guard_static_assertions():
    """守卫的结构性事实（改坏即失败）。"""
    assert set(ct.SPARQL_QUERY_FORMS) == {"SELECT", "ASK", "CONSTRUCT", "DESCRIBE"}
    assert "SERVICE" in ct.EXTERNAL_IO_KEYWORDS and "LOAD" in ct.EXTERNAL_IO_KEYWORDS
    assert "INSERT" in ct.WRITE_KEYWORDS and "DELETE" in ct.WRITE_KEYWORDS
    assert set(ct.ERROR_CODES) >= {
        "INVALID_PARAM", "FORBIDDEN_TOKEN", "QUERY_FORM_REJECTED", "MULTI_STATEMENT_REJECTED",
        "QUERY_TIMEOUT", "EXTERNAL_IO_FORBIDDEN", "DEPTH_LIMIT_EXCEEDED", "DRY_RUN_ONLY",
    }
    assert ct.MAX_CAUSE_DEPTH == 4 and ct.ROW_CAP == 200 and ct.BYTE_CAP == 20000
    assert ct.QUERY_TIMEOUT_S == 4.0 and ct.DEFAULT_PORT == 8302
    # 引号内的写 token 不得被误判（注释/字面量剥离）
    assert ct.guard_query('SELECT ?s WHERE { ?s <p> "INSERT DATA" }')
    assert ct.guard_query("SELECT ?s WHERE { ?s ?p ?o } # DELETE 无关\n")


# ===========================================================================
# 2. 参数校验负控
# ===========================================================================
def test_param_controls():
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_nope", {})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_guide", [])
    exc = expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {"objectType": "Table", "foo": 1})
    assert exc.detail.get("unknownParams") == ["foo"]
    assert "objectType" in exc.detail.get("acceptedParams", [])
    exc2 = expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {})
    assert exc2.detail.get("missingParam") == "objectType"
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {"objectType": "   "})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {"objectType": 5})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {"objectType": "Table", "objectId": 7})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts",
                {"objectType": "Table", "metricCodes": "not-a-list"})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts",
                {"objectType": "Table", "metricCodes": [1]})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_cause", {"symptomCodes": ["  "]})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_trace", {"startId": "x", "maxHops": True})
    expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_trace", {"startId": "x", "maxHops": "3"})
    # cc_propose 的执行意图参数：DRY_RUN_ONLY（只出建议，绝不执行）
    for name in ("execute", "dry_run", "apply", "commit", "confirm", "exec", "dryRun"):
        exc3 = expect_code(ct, "DRY_RUN_ONLY", ct.validate_params, "cc_propose",
                           {"rootCause": "FM-X", name: True})
        assert exc3.detail.get("executed") is False and exc3.detail.get("dryRun") is True
    expect_ok(ct, ct.validate_params, "cc_propose", {"rootCause": "FM-X", "evidencePath": ["E1"]})


def test_error_payload_shape():
    exc = expect_code(ct, "INVALID_PARAM", ct.validate_params, "cc_facts", {})
    payload = exc.payload("cc_facts")
    assert set(payload.keys()) == {"error", "hint", "truncated", "droppedRows", "droppedBytes"}
    assert payload["error"]["tool"] == "cc_facts"
    assert payload["error"]["code"] == "INVALID_PARAM"
    assert payload["truncated"] is False and payload["droppedRows"] == 0
    # 审计不进返回面
    assert "audit" not in json.dumps(payload)


# ===========================================================================
# 3. 信封上限（Ledger）
# ===========================================================================
def test_ledger_limits():
    ledger = ct.Ledger(["rows"])
    ledger.put("rows", [{"i": i} for i in range(ct.ROW_CAP + 50)])
    payload = ledger.finish()
    assert len(payload["rows"]) == ct.ROW_CAP
    assert payload["droppedRows"] == 50
    assert payload["truncated"] is True
    assert "已达行上限" in payload["hint"]

    ledger2 = ct.Ledger(["rows"])
    big = "x" * 5000
    ledger2.put("rows", [{"i": i, "pad": big} for i in range(10)])
    payload2 = ledger2.finish()
    assert payload2["droppedBytes"] > 0, "超过字节上限必须回传 droppedBytes"
    assert ct._byte_len(payload2) <= ct.BYTE_CAP + 4096, "信封体积必须有界"

    ledger3 = ct.Ledger(["rows"])
    ledger3.put("rows", [])
    payload3 = ledger3.finish()
    assert payload3["hint"] == ct.EMPTY_RESULT_HINT
    assert set(payload3.keys()) == {"rows", "hint", "truncated", "droppedRows", "droppedBytes"}


# ===========================================================================
# 4. 参数/守卫的纯函数
# ===========================================================================
def test_pure_helpers():
    assert ct.to_number("498") == 498.0 and ct.to_number("0.0413") == 0.0413
    assert ct.to_number(None) is None and ct.to_number("abc") is None
    assert ct.truthy("true") is True and ct.truthy("up") is True and ct.truthy("false") is False
    assert ct.truthy(None) is None
    text = ct.RANK_BASIS
    assert "CAUSES" not in text and "CC_CAUSES" not in text, "载体词汇不得出现在 compared 文本"
    assert "causes > conditional_causes" in text and "跳数升序" in text
    assert "relates_to" in text and "不参与根因收敛" in text
    names = [t["name"] for t in ct.TOOLFACE]
    assert names == ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace", "cc_cause", "cc_propose",
                     "read_facts"]
    assert ct.CARRIER == "onto" and ct.ARM == "ONTO-Q"
    assert ct.REPO_REL_SOURCE == "scheme2-onto/service/cc_tools_onto.py"


def test_rank_candidates_order():
    items = [
        {"faultCode": "B", "determined": False, "satisfiedClauses": 0, "hops": 1,
         "viaRelation": "causes"},
        {"faultCode": "A", "determined": True, "satisfiedClauses": 0, "hops": 2,
         "viaRelation": "conditional_causes"},
        {"faultCode": "C", "determined": True, "satisfiedClauses": 2, "hops": 3,
         "viaRelation": "causes"},
        {"faultCode": "D", "determined": False, "satisfiedClauses": 0, "hops": 2,
         "viaRelation": "relates_to"},
        {"faultCode": "E", "determined": False, "satisfiedClauses": 0, "hops": 2,
         "viaRelation": "causes"},
    ]
    ranked = ct.rank_candidates([dict(item) for item in items])
    assert [item["faultCode"] for item in ranked] == ["C", "A", "B", "E", "D"], ranked
    assert [item["rank"] for item in ranked] == [1, 2, 3, 4, 5], "rank 必须 1-based 且不合并并列"
    assert ct.RELATION_PRIORITY["causes"] < ct.RELATION_PRIORITY["conditional_causes"]
    assert "relates_to" not in ct.RELATION_PRIORITY


def test_evaluate_clause_unjudgeable_is_none():
    """未标定阈值 ⇒ outcome=None（绝不以 False 冒充不可判定）。"""
    clause = {"metricCode": "m", "op": ">", "role": "detect", "operandKind": "threshold_reference",
              "operandValue": "thr#critical"}
    record = ct.evaluate_clause(clause, {}, {"thr": {"uncalibrated": True}}, {}, "x")
    assert record["outcome"] is None, record
    assert record.get("reason"), "不可判定必须给出理由"
    record2 = ct.evaluate_clause(dict(clause), {}, {"thr": {"critical": 100.0}},
                                 {("x", "m"): [("t1", "150"), ("t2", "160")]}, "x")
    assert record2["outcome"] is True, record2
    assert record2["operand"] == 100.0
    record3 = ct.evaluate_clause(dict(clause), {}, {"thr": {"critical": 100.0}}, {}, "x")
    assert record3["outcome"] is None, "缺观测值 ⇒ 不可判定"


# ===========================================================================
# 5. 源码级变异负控：把守卫阉掉 ⇒ 对应负控必须失败
# ===========================================================================
_MUTANT_COUNTER = [0]


def load_mutant(mutations):
    with io.open(SERVICE_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    for old, new in mutations:
        assert source.count(old) >= 1, "变异锚点不存在：%r" % old
        source = source.replace(old, new, 1)
    _MUTANT_COUNTER[0] += 1
    if not os.path.isdir(TMP_DIR):
        os.makedirs(TMP_DIR, exist_ok=True)
    path = os.path.join(TMP_DIR, "_m33_mutant_cc_tools_onto_%d.py" % _MUTANT_COUNTER[0])
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    spec = importlib.util.spec_from_file_location("m33_mutant_%d" % _MUTANT_COUNTER[0], path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, path


def _trace_responder(sparql, calls):
    if "?s ?s_id ?s_type ?layer" in sparql:
        return [{"s": ct.ONTO_BASE + "inst/app/cc-pay", "s_id": "app:cc-pay",
                 "s_type": "CreditCardApp", "layer": "App"}]
    if "?srcId ?dstId" in sparql:
        return [
            {"srcId": None, "dstId": "endpoint:ep-x", "relationType": "exposes",
             "dstType": "AppEndpoint", "dstLayer": "App"},
            {"srcId": "cc-pay", "dstId": "cc-pay", "relationType": "selfLoop",
             "dstType": "CreditCardApp", "dstLayer": "App"},
        ]
    return []


def control_d4(mod):
    service, _ = make_service(mod, _trace_responder)
    payload = service.build_trace({"startId": "app:cc-pay", "maxHops": 1})
    ids = [node["objectId"] for node in payload["nodes"]]
    assert "None" not in ids, "D4：str(None) 造出了字面量 None 节点"
    assert all(edge["from"] != edge["to"] for edge in payload["edges"]), "D4：自环边未被丢弃"
    assert payload["edges"] == [], "D4：无法解析端点的行必须整行丢弃"


def control_write_token(mod):
    ok, code, _ = raises_code(mod, "FORBIDDEN_TOKEN", mod.guard_query, Q_WRITE_INSERT)
    assert ok and code == "FORBIDDEN_TOKEN", "写操作 token 未被逐字拒绝（实得 %s）" % code


def control_external_io(mod):
    ok, code, _ = raises_code(mod, "EXTERNAL_IO_FORBIDDEN", mod.guard_query, Q_WRITE_SERVICE)
    assert ok and code == "EXTERNAL_IO_FORBIDDEN", "外部 IO（SERVICE）未被逐字拒绝（实得 %s）" % code


def control_depth_cap(mod):
    ok, code, _ = raises_code(mod, "DEPTH_LIMIT_EXCEEDED", mod.guard_query, Q_PATH_BIG)
    assert ok and code == "DEPTH_LIMIT_EXCEEDED", "超上限属性路径未被逐字拒绝（实得 %s）" % code


def control_array_element(mod):
    ok, code, _ = raises_code(mod, "INVALID_PARAM", mod.validate_params, "cc_cause",
                              {"symptomCodes": [1]})
    assert ok and code == "INVALID_PARAM", "数组元素类型不符未被逐字拒绝（实得 %s）" % code


def control_open_low_range(mod):
    """M3.4 本轮补的缺口：`{,n}`（低界缺省=0）上界检查。"""
    ok, code, _ = raises_code(mod, "DEPTH_LIMIT_EXCEEDED", mod.guard_query, Q_PATH_OPEN_LOW_BIG)
    assert ok and code == "DEPTH_LIMIT_EXCEEDED", \
        "`{,n}` 上界未被逐字拒绝（实得 %s）" % code


MUTATIONS = [
    ("写 token 表清空",
     [('WRITE_KEYWORDS = ("INSERT", "DELETE", "CLEAR", "DROP", "CREATE", "WITH", "MOVE", "COPY", "ADD")',
       'WRITE_KEYWORDS = ("ZZZ_NEVER_MATCH",)')],
     control_write_token),
    ("外部 IO 表清空",
     [('EXTERNAL_IO_KEYWORDS = ("SERVICE", "LOAD")', 'EXTERNAL_IO_KEYWORDS = ("ZZZ_NEVER_MATCH",)')],
     control_external_io),
    ("深度上限放宽到 99",
     [("MAX_CAUSE_DEPTH = 4", "MAX_CAUSE_DEPTH = 99")],
     control_depth_cap),
    ("D4 行过滤短路",
     [("                if src is None or dst is None:",
       "                if False:")],
     control_d4),
    ("数组元素类型检查短路",
     [("if not isinstance(item, str) or not item.strip():", "if False:")],
     control_array_element),
    ("`{,n}` 上界检查短路",
     [("    for m in _OPEN_LOW_RANGE_RE.finditer(depth_view):", "    for m in []:")],
     control_open_low_range),
]


def test_mutations_are_falsifiable():
    for name, mutations, control in MUTATIONS:
        control(ct)                                  # 正常模块：负控必须通过
        module, path = load_mutant(mutations)
        failed = False
        detail = ""
        try:
            control(module)
        except AssertionError as exc:
            failed = True
            detail = str(exc)[:80]
        except Exception as exc:                     # noqa: BLE001
            failed = True
            detail = "%s: %s" % (type(exc).__name__, str(exc)[:60])
        assert failed, "变异「%s」后负控仍然通过 ⇒ 该负控不可证伪" % name
        print("  -- 变异「%s」⇒ 负控按预期失败（%s）" % (name, detail))
        try:
            os.remove(path)
        except OSError:
            pass


# ===========================================================================
# 6. 声明↔接受闭环（离线注入 type_index）
# ===========================================================================
DECLARED = ["AppEndpoint", "Container", "CreditCardApp", "DatabaseSessionPool", "Evidence",
            "GaussDBCluster", "GaussDBInstance", "Host", "JdbcDataSource", "Query", "Session",
            "AppInstance", "Table"]


def _service_with_index(mod=ct):
    service, backend = make_service(mod)
    service._type_cache = injected_index(DECLARED)
    service._type_cache_at = time.time()
    return service, backend


def test_schema_declaration_feeds_facts():
    service, _ = _service_with_index()
    schema = service.build_schema({})
    declared = [item["objectType"] for item in schema["objectTypes"]]
    assert declared == sorted(DECLARED), declared
    for name in declared:                                   # 原样喂回 ⇒ 不得 INVALID_PARAM
        params = ct.validate_params("cc_facts", {"objectType": name})
        payload = service.build_facts(params)
        assert payload["objects"] == [] and payload["truncated"] is False
    # 反向：未在 /schema 声明的类型必须被拒（同一标识空间）
    exc = expect_code(ct, "INVALID_PARAM", service.build_facts, {"objectType": "NoSuchType"})
    assert exc.detail.get("declaredObjectTypes") == sorted(DECLARED)
    # /guide 与 /schema 声明的对象类型必须来自同一集合
    guide = service.build_guide({})
    assert [item["objectType"] for item in guide["objectTypes"]] == sorted(DECLARED)


def test_declaration_acceptance_same_source():
    """形态闭合断言：三个工具的「类型」取值域都只用 type_index().declared 一处来源。"""
    service, backend = _service_with_index()
    service.build_schema({})
    service.build_guide({})
    for name in DECLARED:
        service.build_facts({"objectType": name})
    queries = " ".join(call["sparql"] for call in backend.calls)
    assert "ccfType" in queries
    # /schema、/guide 的类型清单必须逐字相同（否则 /facts 会出现第二套标识空间）
    schema = service.build_schema({})
    guide = service.build_guide({})
    assert [i["objectType"] for i in schema["objectTypes"]] == sorted(DECLARED)
    assert [i["objectType"] for i in guide["objectTypes"]] == sorted(DECLARED)


# ===========================================================================
# 7. D1：SPARQL 投影别名必须与 Python 读取的键名一致
# ===========================================================================
EXPECTED_PROJECTIONS = [
    # (工具, 该条子查询的锚点, 代码读取的键名 —— 必须由同一条查询投影出来)
    ("cc_guide", "?metricCode ?unit ?objectType", ["?metricCode", "?unit", "?objectType"]),
    ("cc_guide", "AS ?edgeId",
     ["?edgeId", "?causeCode", "?effectCode", "?chainCode", "?linkKind", "?evidence"]),
    ("cc_schema", "?relationType ?fromType ?toType",
     ["?relationType", "?fromType", "?toType", "?edges", "?srcs", "?dsts"]),
    ("cc_trace", "?s ?s_id ?s_type ?layer", ["?s", "?s_id", "?s_type", "?layer"]),
    ("cc_trace", "?srcId ?dstId",
     ["?srcId", "?dstId", "?relationType", "?dstType", "?dstLayer"]),
    ("cc_cause", "?faultCode ?fmName ?layer",
     ["?faultCode", "?fmName", "?layer", "?severity", "?reversible", "?detectionMode",
      "?componentType"]),
    ("cc_cause", "?link ?edgeId",
     ["?edgeId", "?causeCode", "?effectCode", "?linkKind", "?chainCode"]),
    ("cc_cause", "?faultCode ?judgmentId ?role",
     ["?faultCode", "?judgmentId", "?role", "?metricCode", "?op", "?kind", "?value",
      "?predicate", "?enumValue", "?field", "?exclusionRequirement", "?unjudgeablePolicy"]),
    ("cc_cause", "?thresholdName ?level", ["?thresholdName", "?level", "?value", "?uncalibrated"]),
    ("cc_cause", "?objectId ?metricCode ?value ?ts", ["?objectId", "?metricCode", "?value", "?ts"]),
    ("cc_cause", "?s_id ?s_type", ["?s_id", "?s_type"]),
    ("cc_facts", "?s_id ?attribute ?value ?kind",
     ["?s_id", "?attribute", "?value", "?kind"]),
    ("cc_facts", "?a_id ?b_id ?relationType", ["?a_id", "?b_id", "?relationType"]),
    ("cc_facts", "?a_id ?recordId ?metricCode ?value ?unit ?ts",
     ["?a_id", "?recordId", "?metricCode", "?value", "?unit", "?ts"]),
]


def _d1_responder(sparql, calls):
    """给 D1 用例喂最少但足够的行，保证每条子查询的执行路径都被走到。"""
    rows = _trace_responder(sparql, calls)
    if rows:
        return rows
    if "?faultCode ?fmName ?layer" in sparql:
        return [{"faultCode": "FM-APP-POOL-EXHAUSTED", "fmName": "应用侧连接池耗尽",
                 "layer": "App", "severity": "auto", "reversible": True,
                 "detectionMode": "deterministic", "componentType": "JdbcDataSource"}]
    if "?link ?edgeId" in sparql:
        return [{"link": ct.ONTO_BASE + "causal#E9", "edgeId": "E9",
                 "causeCode": "FM-HOST-MEM-PRESSURE", "effectCode": "FM-APP-POOL-EXHAUSTED",
                 "linkKind": "causes", "chainCode": "F4"}]
    if "?faultCode ?judgmentId ?role" in sparql:
        return [{"faultCode": "FM-APP-POOL-EXHAUSTED", "judgmentId": "J-1", "role": "detect",
                 "metricCode": "app.pool.acquire_wait_ms", "op": ">",
                 "kind": "threshold_reference", "value": "thr#critical",
                 "predicate": None, "enumValue": None, "field": None,
                 "exclusionRequirement": None, "unjudgeablePolicy": "unjudgeable"}]
    if "?thresholdName ?level" in sparql:
        return [{"thresholdName": "thr", "level": "critical", "value": "100",
                 "uncalibrated": None}]
    if "?objectId ?metricCode ?value ?ts" in sparql:
        return [{"objectId": "dbpool:dn_6001-pool", "metricCode": "app.pool.acquire_wait_ms",
                 "value": "150", "ts": "2026-09-28T23:55:15Z"}]
    if "?s_id ?s_type" in sparql:
        return [{"s_id": "dbpool:dn_6001-pool", "s_type": "DatabaseSessionPool"}]
    return []


def test_d1_projection_alias_match():
    """D1 缺陷类：查询里的别名与代码读取的键名不一致 ⇒ 字段恒空。

    对每条**真正发出**的子查询：锚点命中 ⇒ 该查询必须把代码读取的键名全部投影出来。
    """
    exercised = 0
    for tool in ("cc_guide", "cc_schema", "cc_trace", "cc_cause", "cc_facts"):
        service, backend = make_service(responder=_d1_responder)
        service._type_cache = injected_index(DECLARED)
        service._type_cache_at = time.time()
        try:
            if tool == "cc_guide":
                service.build_guide({})
            elif tool == "cc_schema":
                service.build_schema({})
            elif tool == "cc_trace":
                service.build_trace({"startId": "app:cc-pay", "maxHops": 1})
            elif tool == "cc_cause":
                service.build_cause({"symptomCodes": ["FM-APP-POOL-EXHAUSTED"], "maxHops": 4})
            elif tool == "cc_facts":
                service.build_facts({"objectType": "Table"})
        except ct.ToolError:
            pass                                        # 空后端 ⇒ 允许守卫性拒绝，查询已被记录
        queries = [call["sparql"] for call in backend.calls]
        for group_tool, anchor, expected in EXPECTED_PROJECTIONS:
            if group_tool != tool:
                continue
            hits = [query for query in queries if anchor in query]
            assert hits, "锚点未命中任何已发出查询：%s / %r" % (tool, anchor)
            for query in hits:
                missing = [var for var in expected
                           if ("AS %s" % var) not in query and ("%s " % var) not in query]
                assert not missing, "%s 的查询 %r 缺少投影变量 %s" % (tool, anchor, missing)
            exercised += 1
    assert exercised == len(EXPECTED_PROJECTIONS), \
        "仅校验了 %d/%d 组投影（有锚点未被执行路径覆盖）" % (exercised, len(EXPECTED_PROJECTIONS))


def test_d1_type_index_projection_alias():
    """/schema 与 /guide 的对象类型/属性清单来自同一处 type_index() ⇒ 变量名必须与读取键一致。"""
    service, backend = make_service()
    index = service.type_index(force=True)
    assert index["declared"] == []
    text = " ".join(call["sparql"] for call in backend.calls)
    for var in ("?objectType", "?attribute", "?kind", "?n", "?lay"):
        assert var in text, "type_index 缺少投影变量 %s" % var


def test_d1_guide_causal_link_edge_id_roundtrip():
    """别名↔读取键的端到端闭环：投影 ?edgeId ⇒ 代码必须读 edgeId（不是 ?link 全 IRI）。"""
    def responder(sparql, calls):
        if "AS ?edgeId" in sparql:
            return [{"edgeId": "E9", "causeCode": "FM-HOST-MEM-PRESSURE",
                     "effectCode": "FM-CTR-OOM-KILLED", "chainCode": "F4",
                     "linkKind": "causes", "evidence": "样本证据"}]
        return []

    service, _ = make_service(responder=responder)
    guide = service.build_guide({})
    links = [item for item in guide["rules"] if item["ruleKind"] == "causal_link"]
    assert links and links[0]["ruleCode"] == "E9", links
    assert "#" not in links[0]["ruleCode"], "不得直出完整 IRI"


def test_d3_id_expression_has_coalesce_and_iri_fallback():
    """D3：端点 id 必须 coalesce（属性 → IRI 本地名），并丢弃无法解析的行。"""
    pattern = ct.id_pat("s")
    assert "FILTER(isIRI(?s))" in pattern
    assert "COALESCE" in pattern and "AS ?s_id" in pattern
    assert 'REPLACE(STR(?s), "^.*[#/]", "")' in pattern, "缺少 IRI 本地名兜底"
    for prop in ct.ID_PROPS:
        assert prop in pattern, "id 表达式缺少属性 %s" % prop
    assert "STRLEN(STR(?s_id))" in pattern.replace(" ", "") or "STRLEN" in pattern
    # 声明≠ABox：类型块只认 cc:ccfType（ABox），避免 TBox 混入第二套标识空间
    block = ct.typed_subject_block("s")
    assert "cc:ccfType" in block and "isLiteral" in block
    assert "UNION" not in block, "类型块不得 UNION TBox 类（否则 /schema 与 /facts 标识空间分裂）"


# ===========================================================================
# 8. 工具面与契约（用真·G24 门禁模块，禁止自证）
# ===========================================================================
def _load_gate():
    path = os.path.join(REPO_ROOT, "shared", "verify", "toolset_parity_gate.py")
    spec = importlib.util.spec_from_file_location("m33_gate", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_toolface_contract_conformance():
    gate = _load_gate()
    contract = gate.load_yaml(os.path.join(REPO_ROOT, "shared", "model", "tools-setb.yaml"))
    cface = gate.contract_face(contract)
    manifest = ct.build_manifest(repo_root=REPO_ROOT, emitted_at="2026-01-01T00:00:00Z")
    face = gate.manifest_face(manifest, "onto")
    errors, warnings, info = gate.compare_faces(cface, face, face, 8)
    assert not errors, "契约自检失败：%s" % json.dumps(errors, ensure_ascii=False)[:600]
    tools = {tool["name"]: tool for tool in manifest["tools"]}
    assert len(tools) == 8
    assert set(tools) == {"cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace",
                          "cc_cause", "cc_propose", "read_facts"}
    params = sum(len(tool["params"]) for tool in tools.values())
    returns = sum(len(tool["returns"]) for tool in tools.values())
    assert params == 16, params
    assert returns == 56, returns
    assert (info["compared_tools"], info["compared_params"],
            info["compared_return_fields"]) == (8, 16, 56), info
    # 描述文本必须与 KG 侧逐字一致（G24-05），且不得泄露载体词汇（G24-08）
    banned = gate.BANNED_DESC
    for name, tool in tools.items():
        assert not banned.search(tool["description"]), "%s 描述命中载体禁词" % name
        assert name == "read_facts" or tool["description"].count("最多返回 200 行") == 1
    # read_facts 的描述权威是 shared/model/live-facts-tools.yaml#read_facts（非本载体图工具文本）
    shape = gate.load_yaml(os.path.join(REPO_ROOT, "shared", "model", "live-facts-tools.yaml"))
    assert tools["read_facts"]["description"] == shape["read_facts"]["description"].rstrip("\n")


def test_manifest_invariants():
    path = SERVICE_PATH
    with io.open(path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    first = ct.build_manifest(repo_root=REPO_ROOT, emitted_at="2026-01-01T00:00:00Z")
    second = ct.build_manifest(repo_root=REPO_ROOT, emitted_at="2026-01-01T00:00:00Z")
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    assert first["schema"] == "dsh-toolface-manifest/1"
    assert first["carrier"] == "onto" and first["arm"] == "ONTO-Q"
    assert first["sourceSha256"] == digest, "sourceSha256 必须是交付物字节的不变量"
    assert first["source"] == ct.REPO_REL_SOURCE
    assert first["sources"] == [{"path": ct.REPO_REL_SOURCE, "sha256": digest}]
    for tool in first["tools"]:
        assert tool["source"] == ct.REPO_REL_SOURCE and tool["sourceSha256"] == digest
    assert set(first.keys()) >= {"schema", "carrier", "arm", "tool", "source", "sourceSha256",
                                 "sources", "tools", "emittedAt"}


def test_tool_paths_and_envelope_fields():
    assert set(ct.TOOL_PATHS) == {"cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace",
                                  "cc_cause", "cc_propose", "read_facts"}
    assert [name for name, _ in ct.ENVELOPE_FIELDS] == ["hint", "truncated", "droppedRows",
                                                        "droppedBytes"]
    for spec in ct.TOOLFACE:
        names = [item["name"] for item in spec["returns"]]
        for envelope, _ in ct.ENVELOPE_FIELDS:
            assert envelope in names, "%s 缺少信封字段 %s" % (spec["name"], envelope)


def test_read_facts_face():
    """read_facts（M5/S3a-1）：5 入参全 optional、返回名 = 形状 tool_level 4 ∪ 公共信封 4、
    描述与形状权威逐字相同、上游不可达 ⇒ 结构化错误且 UPSTREAM_UNAVAILABLE（绝不静默空成功）。
    ★ 本节只覆盖**模块级纯代理函数**；dispatch/HTTP 面的开放缺陷见
    test_read_facts_dispatch_open_defect。"""
    spec = next(item for item in ct.TOOLFACE if item["name"] == "read_facts")
    triples = [(p["name"], p["type"], bool(p.get("required"))) for p in spec["params"]]
    assert triples == [("layers", "array", False), ("sections", "array", False),
                       ("scope", "string", False), ("max_records", "integer", False),
                       ("max_bytes", "integer", False)], triples
    assert ct.TOOL_PATHS["read_facts"] == "/tools/read_facts"
    envelope = [name for name, _ in ct.ENVELOPE_FIELDS]
    assert envelope == ["hint", "truncated", "droppedRows", "droppedBytes"], envelope
    names = [r["name"] for r in spec["returns"]]
    assert names == ["objects", "relations", "observations", "source"] + envelope, names
    assert sum(len(t["params"]) for t in ct.TOOLFACE) == 16
    assert sum(len(t["returns"]) for t in ct.TOOLFACE) == 56
    shape_path = os.path.join(REPO_ROOT, "shared", "model", "live-facts-tools.yaml")
    with io.open(shape_path, encoding="utf-8") as handle:
        shape = yaml.safe_load(handle)
    assert spec["description"] == shape["read_facts"]["description"].rstrip("\n")
    assert spec["description"] == ct.READ_FACTS_DESCRIPTION
    # 上游不可达（模块级纯代理）：必须是结构化错误，不得静默返回空成功。
    # ★ dispatch/HTTP 面当前不可用 ⇒ 见下方开放缺陷钉。
    previous_url = os.environ.get(ct.LIVE_FACTS_URL_ENV)
    os.environ[ct.LIVE_FACTS_URL_ENV] = "http://127.0.0.1:1"
    try:
        raised = None
        try:
            ct.build_read_facts({"max_records": 1})
        except ct.ToolError as exc:              # noqa: BLE001
            raised = exc
        assert raised is not None, "上游不可达必须抛 ToolError，不得静默返回空成功"
        assert raised.code == ct.ERR_UPSTREAM_UNAVAILABLE == "UPSTREAM_UNAVAILABLE", raised.code
        assert raised.detail.get("upstream"), raised.detail
    finally:
        if previous_url is None:
            os.environ.pop(ct.LIVE_FACTS_URL_ENV, None)
        else:
            os.environ[ct.LIVE_FACTS_URL_ENV] = previous_url


def test_read_facts_dispatch_contract():
    """read_facts（HTTP/dispatch 面，:8302）：上游不可达 ⇒ 结构化错误 + UPSTREAM_UNAVAILABLE（502），
    绝不静默返回空成功。

    ★ 历史（写入报告，不再以「钉住缺陷」的方式表达）：S3a-1 落地的实现曾用裸赋值
      `CcToolsService.build_read_facts = build_read_facts`（模块级函数，首形参是 params）
      ⇒ dispatch 的 `handler(params)` 取到绑定方法 ⇒ 实际调用 build_read_facts(self, params)
      ⇒ TypeError（dispatch 只捕获 ToolError/GraphError ⇒ 未捕获 ⇒ HTTP 500 且无结构化错误体）。
      本批实测该处已被并发写入者改为 `staticmethod(build_read_facts)`
      （cc_tools_onto.py mtime 2026-09-30 01:30:27，+534 B / LF 不变），本节的契约断言因此成立。
    """
    service, _ = make_service()
    previous_url = os.environ.get(ct.LIVE_FACTS_URL_ENV)
    previous_audit = os.environ.pop("CC_AUDIT_LOG", None)
    os.environ[ct.LIVE_FACTS_URL_ENV] = "http://127.0.0.1:1"
    try:
        client = TestClient(ct.create_app(service))
        resp = client.post(ct.TOOL_PATHS["read_facts"], json={"max_records": 1})
        assert resp.status_code == 502, resp.status_code
        body = resp.json()
        assert body["error"]["code"] == ct.ERR_UPSTREAM_UNAVAILABLE, body
        assert body["hint"] and "Traceback" not in resp.text, body
    finally:
        if previous_url is None:
            os.environ.pop(ct.LIVE_FACTS_URL_ENV, None)
        else:
            os.environ[ct.LIVE_FACTS_URL_ENV] = previous_url
        if previous_audit is not None:
            os.environ["CC_AUDIT_LOG"] = previous_audit


def test_read_facts_upstream_error_face():
    """★ R-56/B：read_facts 的**上游错误面透传** + hint 与码相符。

    纯代理不得把「上游收下请求但拒绝执行」（BLOCKED_*）说成「上游不可用」：真机证据
    （Dify `message_agent_thoughts`）里模型把 `layers` 传成 `["app"]`，上游正确 fail-closed
    报覆盖门禁，模型收到的却是「上游返回 HTTP 409 / 上游不可用」⇒ 放弃取数。
    """
    section("8b. read_facts：上游业务码原样透传 + hint 与码相符")

    def passthrough_registered_codes():
        cases = [
            ({"error": {"code": "BAD_PARAM", "message": "layers 含未知层名 ['host']"}}, 400),
            ({"error": {"code": "BLOCKED_BASE", "message": "覆盖门禁拒绝"}}, 409),
            ({"error": {"code": "UNKNOWN_PARAM", "message": "未知入参"}}, 400),
        ]
        for body, status in cases:
            code, message = ct.upstream_error_of(json.dumps(body).encode("utf-8"), status)
            assert code == body["error"]["code"], (code, body)
            assert body["error"]["message"] in message, message
            assert str(status) in message, message
    check("read-facts/upstream-code-passthrough", passthrough_registered_codes)

    def unregistered_and_garbage_fall_back():
        code, _m = ct.upstream_error_of(json.dumps({"error": {"code": "WHAT"}}).encode(), 400)
        assert code == ct.ERR_UPSTREAM_UNAVAILABLE, code
        code, _m = ct.upstream_error_of(b"<html>502 Bad Gateway</html>", 502)
        assert code == ct.ERR_UPSTREAM_UNAVAILABLE, code
    check("read-facts/unregistered-code-falls-back", unregistered_and_garbage_fall_back)

    def hint_matches_code():
        assert ct.error_hint("INVALID_PARAM") == ct.GUARD_HINT
        assert ct.error_hint(ct.ERR_UPSTREAM_UNAVAILABLE) == ct.UPSTREAM_DOWN_HINT
        for c in ("BLOCKED_BASE", "BLOCKED_COVERAGE", "BLOCKED_INJECTION"):
            assert ct.error_hint(c) == ct.UPSTREAM_REFUSED_HINT, c
            assert "只读守卫" not in ct.error_hint(c), c
        for c in ("BAD_PARAM", "UNKNOWN_PARAM", "BAD_JSON"):
            assert ct.error_hint(c) == ct.UPSTREAM_PARAM_HINT, c
    check("read-facts/hint-matches-code", hint_matches_code)


def test_response_keys_match_declared_face():
    """真机交叉核对现场：早返回/空结果路径会丢掉已声明的顶层键。

    证据：真机 cc_trace 传 startId="cc-pay"（该 id 在 ONTO 快照不存在）时，
    我的返回面缺 nodes/edges 两键，而 KG 侧同一调用键齐全。
    """
    declared = {spec["name"]: [item["name"] for item in spec["returns"]] for spec in ct.TOOLFACE}
    bodies = [
        ("cc_guide", {}),
        ("cc_schema", {}),
        ("cc_query", {"query": Q_SELECT_OK}),
        ("cc_facts", {"objectType": "Table"}),
        ("cc_trace", {"startId": "app:cc-pay", "maxHops": 2}),
        ("cc_cause", {"symptomCodes": ["FM-APP-POOL-EXHAUSTED"], "maxHops": 4}),
        ("cc_propose", {"rootCause": "FM-DB-SESSION-POOL-FULL", "evidencePath": []}),
    ]
    answered = []
    for tool, body in bodies:
        service, _ = make_service(responder=_d1_responder)
        service._type_cache = injected_index(DECLARED)
        service._type_cache_at = time.time()
        try:
            payload = service.dispatch(tool, body)
        except ct.ToolError as exc:
            print("  -- %s 在最小行集下未给 200（%s），跳过键断言" % (tool, exc.code))
            continue
        missing = [key for key in declared[tool] if key not in payload]
        assert not missing, "%s 返回面缺少声明过的顶层键 %s" % (tool, missing)
        answered.append(tool)
    assert len(answered) >= 6, "仅 %d 个工具进入键断言：%s" % (len(answered), answered)

    service, _ = make_service(responder=None)
    service._type_cache = injected_index(DECLARED)
    service._type_cache_at = time.time()
    empty = service.build_trace({"startId": "cc-pay", "maxHops": 2})
    for key in declared["cc_trace"]:
        assert key in empty, "cc_trace 空起点早返回缺键 %r" % (key,)
    assert empty["nodes"] == [] and empty["edges"] == []
    assert empty["hopsUsed"] == 0 and empty["truncated"] is False


# ===========================================================================
# 9. 真机 smoke（CC_M33_LIVE=1 时才跑；只读）
# ===========================================================================
def test_live_smoke():
    if os.environ.get("CC_M33_LIVE") != "1":
        print("  -- 跳过真机 smoke（设置 CC_M33_LIVE=1 启用）")
        return
    service = ct.build_service()
    print("  -- snapshotId=%s tbox=%s snap=%s"
          % (service.snapshot_id, service.tbox_graph, service.snapshot_graph))
    schema = service.dispatch("cc_schema", {})
    guide = service.dispatch("cc_guide", {})
    assert schema["objectTypes"] and guide["objectTypes"]
    # 声明↔接受闭环（真机）
    for item in schema["objectTypes"]:
        payload = service.dispatch("cc_facts", {"objectType": item["objectType"]})
        assert "objects" in payload
    facts = service.dispatch("cc_facts", {"objectType": "Table"})
    assert facts["objects"], "Table 快照非空"
    for obj in facts["objects"][:5]:
        service.dispatch("cc_facts", {"objectType": obj["objectType"], "objectId": obj["objectId"]})
        service.dispatch("cc_trace", {"startId": obj["objectId"], "maxHops": 1})
    trace = service.dispatch("cc_trace", {"startId": "app:cc-pay", "maxHops": 2})
    ids = [node["objectId"] for node in trace["nodes"]]
    assert "None" not in ids and all(edge["from"] != edge["to"] for edge in trace["edges"])
    assert all(node.get("objectType") for node in trace["nodes"]), "D1/D3：节点类型不得为空"
    cause = service.dispatch("cc_cause", {"symptomCodes": ["FM-APP-POOL-EXHAUSTED"], "maxHops": 4})
    assert cause["candidates"], "成因候选不得为空"
    for candidate in cause["candidates"]:
        assert candidate.get("faultCode") and candidate.get("rankBasis") is None
        assert candidate.get("evidenceSourceIds"), "证据来源不得为空"
    propose = service.dispatch("cc_propose", {"rootCause": cause["candidates"][0]["faultCode"],
                                             "evidencePath": []})
    assert propose["actions"] and propose["executed"] is False and propose["dryRun"] is True
    assert propose["actions"][0]["actionCode"]
    keys = set(cause.keys()) | set(propose.keys())
    assert "audit" not in keys, "审计不得进返回面"
    print("  -- live 七工具：guide.metrics=%d schema.objectTypes=%d trace.nodes=%d "
          "cause.candidates=%d propose.actions=%d"
          % (len(guide["metrics"]), len(schema["objectTypes"]), len(trace["nodes"]),
             len(cause["candidates"]), len(propose["actions"])))


# ===========================================================================
# 10. M3.4 / M3.5 / M3.6 硬化（本轮新增）
# ===========================================================================
def test_m34_open_low_range_bound():
    """M3.4 缺口补齐：`{,n}`（低界缺省 = 0）必须与 `{0,n}` 共用同一上界判据（G4/G8）。"""
    for query in (Q_PATH_OPEN_LOW_OK, "SELECT ?o WHERE { <urn:a> <urn:p>{0,4} ?o }",
                  "SELECT ?o WHERE { <urn:a> <urn:p>{4} ?o }"):
        expect_ok(ct, ct.guard_query, query)
    for query in (Q_PATH_OPEN_LOW_BIG, Q_PATH_OPEN_LOW_SPACED, Q_PATH_OPEN_LOW_HUGE,
                  "SELECT ?o WHERE { <urn:a> <urn:p>{0,5} ?o }",
                  "SELECT ?o WHERE { <urn:a> <urn:p>{2,} ?o }",
                  "SELECT ?o WHERE { <urn:a> <urn:p>{,} ?o }",
                  "SELECT ?o WHERE { <urn:a> <urn:p>* ?o }",
                  "SELECT ?o WHERE { <urn:a> <urn:p>+ ?o }"):
        expect_code(ct, "DEPTH_LIMIT_EXCEEDED", ct.guard_query, query)
    # 上界 = 4 是**含**边界的：5 拒、4 放行（不是 >4 写成 >=4 的越界放宽）
    expect_ok(ct, ct.guard_query, "SELECT ?o WHERE { <urn:a> <urn:p>{,4} ?o }")
    assert ct._OPEN_LOW_RANGE_RE.findall("{,5}") == ["5"]
    assert ct._OPEN_LOW_RANGE_RE.findall("{ , 5 }") == ["5"]
    # 结构性事实：上界判据锚定同一常量
    assert ct.MAX_CAUSE_DEPTH == 4


def test_m35_truncation_accounting():
    """M3.5：截断必须如实可查（不静默截断冒充完整），且返回面键集逐字不变。"""
    ledger = ct.Ledger(["attributes", "objectTypes"])
    ledger.put("attributes", ["a%d" % i for i in range(500)])
    ledger.put("objectTypes", ["T%d" % i for i in range(5)])
    payload = ledger.finish()
    assert set(payload.keys()) == {"attributes", "objectTypes", "hint", "truncated",
                                   "droppedRows", "droppedBytes"}, "返回面键集必须逐字不变"
    assert payload["truncated"] is True and payload["droppedRows"] > 0
    assert ct._byte_len(payload) <= ct.BYTE_CAP
    account = ledger.accounting()
    assert account["originalRows"]["attributes"] == 500
    assert account["returnedRows"]["attributes"] == ct.ROW_CAP
    assert "attributes" in account["truncatedFields"]
    assert account["originalBytes"] > 0 and account["keptBytes"] > 0
    assert account["truncated"] is True and account["droppedEntries"] >= 300
    # hint 必须如实披露：原始字节 / 被截断条目数 / 每个被截断列表的 原始→返回
    assert "原始载荷" in payload["hint"] and "本次返回**不完整**" in payload["hint"]
    assert "attributes 500→%d" % ct.ROW_CAP in payload["hint"]

    # 字节回收路径：记账文案不得把载荷顶过上限（finish 内二次回收）
    ledger2 = ct.Ledger(["rows"])
    ledger2.put("rows", [{"i": i, "pad": "x" * 4000} for i in range(20)])
    payload2 = ledger2.finish()
    assert ct._byte_len(payload2) <= ct.BYTE_CAP, "信封必须严格 ≤ 字节上限（含记账文案）"
    assert payload2["droppedBytes"] > 0 and payload2["truncated"] is True
    account2 = ledger2.accounting()
    assert account2["byteCappedFields"] == ["rows"]
    assert account2["droppedEntries"] == ledger2.dropped_entries > 0
    assert account2["fullBytesBeforeByteRecovery"] > ct.BYTE_CAP
    assert set(payload2.keys()) == {"rows", "hint", "truncated", "droppedRows", "droppedBytes"}

    # 未截断时不得谎报截断（诚实性反面对照）
    ledger3 = ct.Ledger(["rows"])
    ledger3.put("rows", [{"i": 1}])
    payload3 = ledger3.finish()
    assert payload3["truncated"] is False
    assert payload3["droppedRows"] == 0 and payload3["droppedBytes"] == 0
    assert "截断记账" not in payload3["hint"]


def test_m35_exit_budget_guard():
    """M3.5 出口复核：finish() **之后**追加的字段也计入上限（真机 B01–B04 实测缺口）。"""
    payload = {"rows": [{"i": i, "pad": "x" * 100} for i in range(30)],
               "hint": "信封内层", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
    payload["rowCount"] = len(payload["rows"])            # finish() 之后追加（真实代码路径）
    full = ct._byte_len(payload)
    assert full > 800
    capped = ct.enforce_payload_budget(dict(payload), byte_cap=800)
    assert ct._byte_len(capped) <= 800, "出口复核必须把载荷收进上限（含记账文案）"
    assert capped["truncated"] is True and capped["droppedRows"] == 0 and capped["droppedBytes"] > 0, \
        "R-23：字节路径不得改写 droppedRows（droppedRows 只由行上限驱动）"
    assert "不完整" in capped["hint"] and "出口字节复核" in capped["hint"]
    assert ("原始载荷 %d B" % full) in capped["hint"], "hint 须含原始载荷大小（字节上限路径）"
    assert set(capped.keys()) == set(payload.keys()), "出口复核不得增删返回面字段名"
    assert capped["rowCount"] == payload["rowCount"], "非列表字段不得被改动"
    inside = {"rows": [{"i": 1}], "hint": "x", "truncated": False, "droppedRows": 0,
              "droppedBytes": 0, "rowCount": 1}
    assert ct.enforce_payload_budget(dict(inside), byte_cap=ct.BYTE_CAP) == inside, "未超限时一字不改"
    # 真实规模：满行载荷在 finish() 之后追加 rowCount，出口仍需收进 BYTE_CAP
    ledger = ct.Ledger(["rows"], row_cap=10 ** 6)
    ledger.put("rows", [{"i": i, "pad": "x" * 90} for i in range(600)])
    real = ledger.finish()
    real["rowCount"] = len(real["rows"])
    before = ct._byte_len(real)
    real = ct.enforce_payload_budget(real)
    assert ct._byte_len(real) <= ct.BYTE_CAP, "%d → %d" % (before, ct._byte_len(real))
    # 无可回收列表时如实标注未达标，不假装通过
    stuck = {"hint": "x" * (ct.BYTE_CAP + 100), "truncated": False, "droppedRows": 0,
             "droppedBytes": 0}
    stuck_out = ct.enforce_payload_budget(dict(stuck), byte_cap=ct.BYTE_CAP)
    assert "未达标" in stuck_out["hint"], "无可回收列表时必须如实披露仍超限"


def test_m35_accounting_dropped_rows_strict():
    """★ R-23 严格口径：droppedRows 只由**行数上限**驱动；字节路径只记 droppedBytes/droppedEntries。

    负控 id 对齐 KG 侧同源项 `accounting/droppedRows-untouched-by-byte-path`。
    """
    # ---- 负控：字节路径截断 ⇒ droppedRows 保持 0（不得被字节路径改写）----------
    over = {"rows": [{"i": i, "pad": "x" * 100} for i in range(400)],
            "hint": "信封内层", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
    full = ct._byte_len(over)
    n_before = len(over["rows"])          # dict(...) 是浅拷贝，列表会被原地削减 ⇒ 先记原始行数
    token = ct._REQUEST_CTX.set({})
    try:
        out = ct.enforce_payload_budget(dict(over), byte_cap=800)
        ctx = ct._REQUEST_CTX.get()
    finally:
        ct._REQUEST_CTX.reset(token)
    removed = n_before - len(out["rows"])
    assert removed > 0 and ct._byte_len(out) <= 800, "字节路径必须把载荷收进上限"
    assert out["droppedRows"] == 0, "字节路径不得把移除的条目累加进 droppedRows"
    assert out["truncated"] is True and out["droppedBytes"] > 0
    assert ctx["droppedEntries"] == removed, "字节路径移除的条目应计入 droppedEntries"
    assert ctx["truncatedFields"] == ["rows"]
    assert ("原始载荷 %d B" % full) in out["hint"] and "不完整" in out["hint"]
    assert set(out.keys()) == set(over.keys()), "出口复核不得增删返回面字段名"

    # ---- 正控：行上限驱动的 droppedRows 经出口复核后如实保留（不被抹掉/改写）-------
    row_capped = {"rows": [{"i": i, "pad": "x" * 100} for i in range(400)],
                  "hint": "行上限", "truncated": True, "droppedRows": 200, "droppedBytes": 0}
    keep = ct.enforce_payload_budget(dict(row_capped), byte_cap=800)
    assert keep["droppedRows"] == 200, "行上限驱动的 droppedRows 必须如实保留"
    assert keep["droppedBytes"] > 0 and keep["truncated"] is True

    # ---- 键面兜底：载荷缺 droppedRows 时只补 0，绝不承接字节路径的计数 -------------
    no_key = {"rows": [{"i": i, "pad": "x" * 100} for i in range(400)], "hint": "x",
              "truncated": False, "droppedBytes": 0}
    fixed = ct.enforce_payload_budget(dict(no_key), byte_cap=800)
    assert set(fixed.keys()) == set(no_key.keys()) | {"droppedRows"}
    assert fixed["droppedRows"] == 0


def _audit_capture(fn):
    """跑 fn 并解析它写到 stderr 的 AUDIT 行（审计链不进返回面）。"""
    buffer = io.StringIO()
    saved = sys.stderr
    sys.stderr = buffer
    try:
        result = fn()
    finally:
        sys.stderr = saved
    records = [json.loads(line[len("AUDIT "):])["audit"]
               for line in buffer.getvalue().splitlines() if line.startswith("AUDIT ")]
    return result, records


def test_m36_audit_chain_fields():
    """M3.6：审计链至少含 请求 id/工具名/入参摘要/发起时刻/耗时/返回字节数/是否截断。"""
    assert re.fullmatch(r"[0-9a-f]{16}", ct.new_request_id())
    assert ct.new_request_id() != ct.new_request_id()
    assert ct.utc_now_iso().endswith("Z") and "T" in ct.utc_now_iso()
    assert re.fullmatch(r"[0-9a-f]{16}", ct.params_digest({"a": 1}))
    assert ct.params_digest({"a": 1, "b": 2}) == ct.params_digest({"b": 2, "a": 1})
    assert ct.params_digest({"a": 1}) != ct.params_digest({"a": 2})
    summary = ct.params_summary({"objectType": "T" * 400, "metricCodes": [1, 2, 3]})
    assert len(summary) <= 400 and "objectType=" in summary and "metricCodes=[3 项" in summary

    token = ct._REQUEST_CTX.set({"requestId": "0123456789abcdef",
                                 "startedAt": "2026-01-01T00:00:00.000Z",
                                 "paramsDigest": "fedcba9876543210",
                                 "paramsSummary": "objectType='Table'"})
    try:
        _, records = _audit_capture(lambda: ct.audit_log(
            "cc_trace", "SELECT ?s WHERE { ?s ?p ?o }", 12.5, 3, "ok",
            result_bytes=1234, truncated=True, dropped_rows=2, dropped_bytes=99,
            truncated_fields=["nodes"], original_bytes=4096, dropped_entries=2))
    finally:
        ct._REQUEST_CTX.reset(token)
    assert len(records) == 1
    record = records[0]
    for key in ("requestId", "tool", "paramsDigest", "paramsSummary", "queryText", "startedAt",
                "elapsedMs", "elapsed_s", "rowCount", "resultBytes", "truncated", "droppedRows",
                "droppedBytes", "truncatedFields", "originalBytes", "droppedEntries", "status",
                "query_latency_p50_ms", "query_latency_p95_ms", "query_latency_p50_s",
                "query_latency_p95_s", "query_latency_sample_count"):
        assert key in record, "审计链缺字段 %s" % key
    assert record["requestId"] == "0123456789abcdef"
    assert record["startedAt"] == "2026-01-01T00:00:00.000Z"
    assert record["paramsDigest"] == "fedcba9876543210"
    assert record["tool"] == "cc_trace" and record["status"] == "ok"
    assert record["elapsedMs"] == 12.5 and record["elapsed_s"] == 0.0125
    assert record["rowCount"] == 3 and record["resultBytes"] == 1234
    assert record["truncated"] is True and record["droppedRows"] == 2
    assert record["truncatedFields"] == ["nodes"] and record["droppedEntries"] == 2
    assert record["queryText"].startswith("SELECT")


def test_m36_latency_separated_from_elapsed_s():
    """M3.6：query_latency_p50/p95 与 elapsed_s 分离（互不派生，两个方向各证一次）。"""
    registry = ct.LatencyRegistry()
    for value in (10.0, 20.0, 30.0, 40.0):
        registry.observe("cc_query", value, "ok")
    assert ct.LatencyRegistry.percentile([10.0, 20.0, 30.0, 40.0], 50.0) == 20.0
    assert ct.LatencyRegistry.percentile([10.0, 20.0, 30.0, 40.0], 95.0) == 40.0
    # 空样本必须 null，不得用 0 冒充「没有样本」
    assert ct.LatencyRegistry.percentile([], 50.0) is None
    assert ct.LatencyRegistry().snapshot()["query_latency_p50"] is None
    # 滑窗有界（定长）
    window = ct.LatencyRegistry(capacity=3)
    for value in (1.0, 2.0, 3.0, 4.0):
        window.observe("cc_query", value)
    assert window.tool_latency("cc_query")["sampleCount"] == 3
    snapshot = registry.snapshot()
    assert snapshot["percentileMethod"].startswith("nearest-rank")
    assert "elapsed" in snapshot["separation"]
    assert snapshot["query_latency_p50"] == snapshot["query_latency_p50_ms"] == 20.0
    assert snapshot["query_latency_p95_ms"] == 40.0
    assert snapshot["perTool"]["cc_query"]["sampleCount"] == 4
    live = registry.tool_latency("cc_query")
    assert live["p50_ms"] == 20.0 and live["p95_ms"] == 40.0
    assert live["count"] == 4 and live["sampleCount"] == 4

    def one(elapsed_ms):
        current = registry.tool_latency("cc_query")
        token = ct._REQUEST_CTX.set({})
        try:
            _, records = _audit_capture(lambda: ct.audit_log(
                "cc_query", "", elapsed_ms, 0, "ok",
                latency_p50_ms=current["p50_ms"], latency_p95_ms=current["p95_ms"],
                latency_p50_s=current["p50_s"], latency_p95_s=current["p95_s"],
                latency_sample_count=current["sampleCount"]))
        finally:
            ct._REQUEST_CTX.reset(token)
        return records[0]

    # 方向一：改单次 elapsed（1ms → 999999ms）⇒ 聚合字段一字不动
    low, high = one(1.0), one(999999.0)
    assert low["elapsedMs"] == 1.0 and high["elapsedMs"] == 999999.0
    assert low["query_latency_p50_ms"] == high["query_latency_p50_ms"] == 20.0
    assert low["query_latency_p95_ms"] == high["query_latency_p95_ms"] == 40.0
    assert low["query_latency_sample_count"] == high["query_latency_sample_count"] == 4
    assert low["elapsed_s"] == 0.001 and high["elapsed_s"] == 999.999
    # 方向二：改聚合样本 ⇒ 单次 elapsed 字段一字不动
    for _ in range(100):
        registry.observe("cc_query", 777.0, "ok")
    after = one(1.0)
    assert after["elapsedMs"] == 1.0 and after["elapsed_s"] == 0.001
    assert after["query_latency_p95_ms"] == 777.0
    assert registry.tool_latency("cc_query")["p95_ms"] == 777.0
    # 遥测默认不落盘（无 CC_TOOLS_ONTO_METRICS 时绝不产生副作用文件）
    assert ct.metrics_path() is None
    assert ct.write_metrics_file(force=True) is None


def test_m36_dispatch_wires_latency_without_touching_response():
    """聚合埋点不得进入任何工具的返回面（返回面键集由 G24 逐字比对）。"""
    ct.LATENCY.reset()
    service, _ = make_service(ct)
    payload, records = _audit_capture(lambda: service.dispatch("cc_guide", {}))
    assert not [key for key in payload if "latency" in key.lower()], sorted(payload.keys())
    assert records, "dispatch 必须落审计链"
    last = records[-1]
    assert last["tool"] == "cc_guide" and last["status"] == "ok"
    assert last["query_latency_sample_count"] == 1
    assert last["requestId"] and last["startedAt"] and last["paramsDigest"]
    assert last["resultBytes"] == ct._byte_len(payload)
    assert last["elapsedMs"] >= 0.0
    assert last["elapsed_s"] == round(last["elapsedMs"] / 1000.0, 6)
    # 参数校验失败也必须留痕（拒绝路径同样进审计链），且响应不受埋点影响
    try:
        service.dispatch("cc_facts", {})
    except ct.ToolError:
        pass
    assert ct.LATENCY.tool_latency("cc_facts")["count"] == 1, "参数拒绝路径必须记一次时延样本"


# ===========================================================================
# R-27：审计链落盘（CC_AUDIT_LOG → 追加式 JSONL，一次工具调用 = 一行）
# ===========================================================================
def _r27_tmp(name: str) -> str:
    return os.path.join(TMP_DIR, "_m33_r27_%s_%d" % (name, os.getpid()))


def test_r27_audit_disabled_without_env():
    """R-27 负控：未设/空 CC_AUDIT_LOG ⇒ 不落盘、不报错、/health.auditFile 回 ""。"""
    saved = os.environ.pop("CC_AUDIT_LOG", None)
    try:
        assert ct.audit_path() is None
        assert ct.persist_audit_record({"requestId": "x"}) is None
        assert ct.audit_health()["auditFile"] == ""
        os.environ["CC_AUDIT_LOG"] = ""
        assert ct.audit_path() is None, "空串必须与未设同义"
        assert ct.persist_audit_record({"requestId": "x"}) is None
        assert not os.path.exists(os.path.join(SERVICE_DIR, "cc_tools_onto_audit.jsonl")), \
            "未设 env 时不得产生默认审计文件（默认行为不变）"
    finally:
        if saved is None:
            os.environ.pop("CC_AUDIT_LOG", None)
        else:
            os.environ["CC_AUDIT_LOG"] = saved


def test_r27_audit_jsonl_append_and_same_record():
    """R-27 正控：追加（从不 truncate）且「落盘行 ≡ stderr 行 ≡ 同一条 record」。"""
    path = _r27_tmp("audit") + ".jsonl"
    saved = os.environ.get("CC_AUDIT_LOG")
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"preExisting": true}\n')
        os.environ["CC_AUDIT_LOG"] = path
        result, records = _audit_capture(lambda: ct.audit_log(
            "cc_guide", "guide", 12.5, 0, "ok", request_id="UNIT-R27-1",
            started_at="2026-01-01T00:00:00.000Z"))
        assert ct.audit_path() == path
        assert ct.audit_health()["auditFile"] == path, "非空 env ⇒ /health 必须暴露实际路径"
        assert len(records) == 1 and result == records[0], \
            "audit_log 必须返回与 stderr 同一条 record"
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        assert len(lines) == 2, lines
        assert json.loads(lines[0]) == {"preExisting": True}, "既有行不得被清空/截断（追加语义）"
        assert json.loads(lines[1]) == records[0], "落盘行必须与 stderr 记录逐字同值"
        assert lines[1] == ct._dumps(records[0])
        _audit_capture(lambda: ct.audit_log("cc_query", "SELECT 1", 1.0, 0, "ok",
                                            request_id="UNIT-R27-2"))
        with open(path, "r", encoding="utf-8") as handle:
            lines2 = handle.read().splitlines()
        assert len(lines2) == 3 and lines2[:2] == lines, "一次调用 = 一行且旧行一字不动"
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        assert 'open(path, "a", encoding="utf-8", newline="\\n")' in source, "必须用追加模式打开"
        assert len(ct.AUDIT_FIELDS) == 22 and len(set(ct.AUDIT_FIELDS)) == 22
        assert set(ct.AUDIT_FIELDS) <= set(records[0]), "22 键不得删除/缩写"
        snapshot = ct.AUDIT_CHAIN.snapshot(tail=5)
        assert set(snapshot) == {"length", "total", "capacity", "fields", "tail"}
        assert snapshot["capacity"] == 500 and snapshot["fields"] == list(ct.AUDIT_FIELDS)
        assert snapshot["tail"][-1]["requestId"] == "UNIT-R27-2"
        assert snapshot["total"] >= 2 and snapshot["length"] >= 2
    finally:
        if saved is None:
            os.environ.pop("CC_AUDIT_LOG", None)
        else:
            os.environ["CC_AUDIT_LOG"] = saved
        try:
            os.remove(path)
        except OSError:
            pass


def test_r27_audit_write_failure_contained():
    """R-27 负控：审计写盘失败不得冒泡/不得改响应，但必须如实记错；恢复后清零。"""
    blocker = _r27_tmp("blocker") + ".jsonl"
    ok_path = _r27_tmp("ok") + ".jsonl"
    saved = os.environ.get("CC_AUDIT_LOG")
    try:
        with open(blocker, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("")            # 用「文件」占住目录位置 ⇒ 落盘必然失败
        os.environ["CC_AUDIT_LOG"] = os.path.join(blocker, "sub", "audit.jsonl")
        assert ct.persist_audit_record({"requestId": "boom"}) is None, "写盘失败不得抛异常"
        assert ct._AUDIT_STATE.get("error"), "写盘失败必须如实记进 auditWriteError"
        result, records = _audit_capture(lambda: ct.audit_log(
            "cc_guide", "guide", 1.0, 0, "ok", request_id="UNIT-R27-3"))
        assert len(records) == 1, "落盘失败不得影响 stderr 审计面"
        assert result == records[0], "落盘失败不得改变 audit_log 返回值/工具面"
        os.environ["CC_AUDIT_LOG"] = ok_path
        assert ct.persist_audit_record({"requestId": "ok"}) == ok_path
        assert ct._AUDIT_STATE.get("error") is None, "恢复后错误必须清零"
    finally:
        if saved is None:
            os.environ.pop("CC_AUDIT_LOG", None)
        else:
            os.environ["CC_AUDIT_LOG"] = saved
        for leftover in (blocker, ok_path):
            try:
                os.remove(leftover)
            except OSError:
                pass


def test_r27_health_audit_keys_additive():
    """R-27：/health 只**新增** auditFile/auditChain/queryLatency 三键，既有键不减不改。"""
    keys = set(ct.audit_health())
    assert keys == {"auditFile", "auditChain", "queryLatency"}, sorted(keys)
    with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("**audit_health(),") == 2, \
        "两处健康载荷（/health 与 --check-health）必须同步带上审计三键"
    assert source.count('"tools": [spec["name"] for spec in TOOLFACE],') == 2, \
        "既有健康键（tools）必须原样保留"
    assert '"capability": "readonly",' in source
    assert 'handle.write(_dumps(record) + "\\n")' in source


def test_r27_one_line_per_call_dispatch():
    """R-27 正控：一次真实 dispatch = 一行落盘；内层视图只写 stderr（不进尾环、不落盘）。

    「一次工具调用 = 一行」的离线可回归判据：改前（内层视图也落盘）该断言必失败。
    """
    path = _r27_tmp("oneline") + ".jsonl"
    saved = os.environ.get("CC_AUDIT_LOG")
    try:
        os.environ["CC_AUDIT_LOG"] = path
        service, _ = make_service(ct)
        payload, records = _audit_capture(lambda: service.dispatch("cc_guide", {}))
        assert records, "dispatch 必须写 stderr 审计行"
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        assert len(lines) == 1, "一次 dispatch 只允许落盘一行，实际 %d 行" % len(lines)
        persisted = json.loads(lines[0])
        assert persisted == ct.AUDIT_CHAIN.snapshot(tail=1)["tail"][-1], \
            "落盘行必须与尾环最后一条逐字同值（同一 record，不是另造一份）"
        assert persisted["tool"] == "cc_guide" and persisted["status"] == "ok"
        assert persisted["resultBytes"] == ct._byte_len(payload)
        assert persisted["queryText"], "信封视图必须继承内层 queryText（R-27 不得丢语义文本）"
        assert persisted["rowCount"] >= 0
        assert set(ct.AUDIT_FIELDS) <= set(persisted), "22 键不得缺项"
        # 内层视图（persist=False）：仍写 stderr，但不进尾环、不落盘
        before_total = ct.AUDIT_CHAIN.snapshot(tail=1)["total"]
        _, stderr_records = _audit_capture(lambda: ct.audit_log(
            "cc_guide", "INNER-VIEW", 3.0, 7, "ok", persist=False,
            request_id="UNIT-R27-ONELINE"))
        assert len(stderr_records) == 1, "persist=False 仍必须写 stderr（既有审计面不变）"
        assert ct.AUDIT_CHAIN.snapshot(tail=1)["total"] == before_total, \
            "persist=False 不得进内存尾环"
        with open(path, "r", encoding="utf-8") as handle:
            assert len(handle.read().splitlines()) == 1, "persist=False 不得落盘"
        assert ct.audit_health()["auditFile"] == path
    finally:
        if saved is None:
            os.environ.pop("CC_AUDIT_LOG", None)
        else:
            os.environ["CC_AUDIT_LOG"] = saved
        try:
            os.remove(path)
        except OSError:
            pass


# ===========================================================================
# 12. M3.9（R-20/R-30）声明形态：请求体声明指向的组件名 == 代码实际模型名
# ===========================================================================
# ★ 下面三张表是**冻结字面量**，刻意不从本实现的 TOOLFACE 推导（禁止自证）：
#   任何模型改名 / 请求体 Optional 化 / 端点响应注解漂移都必须让本负控失败。
M39_TOOL_PATHS = {
    "cc_guide": "/guide",
    "cc_schema": "/schema",
    "cc_query": "/query",
    "cc_facts": "/facts",
    "cc_trace": "/trace",
    "cc_cause": "/cause",
    "cc_propose": "/propose",
    "read_facts": "/tools/read_facts",
}
M39_PARAM_TOOLS = ("cc_query", "cc_facts", "cc_trace", "cc_cause", "cc_propose", "read_facts")
M39_FROZEN_SCHEMAS = {
    "HTTPValidationError",
    "ValidationError",
    "cc_causeIn",
    "cc_factsIn",
    "cc_guideIn",
    "cc_proposeIn",
    "cc_queryIn",
    "cc_schemaIn",
    "cc_traceIn",
    "read_factsIn",
}


def _m39_app(mod):
    """离线构造 OpenAPI（替身后端不产生任何网络 IO；base_url/repository 仅用于 /health 载荷）。"""
    service, backend = make_service(mod=mod)
    backend.base_url = "stub://offline"
    backend.repository = "cc-ops"
    return mod.create_app(service)


def control_m39_declaration(mod):
    """声明形态负控：请求体 `$ref` 的组件名必须等于冻结的模型名，形态必须与 KG 侧一致。"""
    doc = _m39_app(mod).openapi()
    paths = doc["paths"]
    assert set(paths) == {"/health"} | set(M39_TOOL_PATHS.values()), \
        "路径集合漂移（不得增删路由）：%s" % sorted(paths)
    schemas = sorted(doc["components"]["schemas"])
    assert set(schemas) == M39_FROZEN_SCHEMAS, \
        "组件名集合漂移（模型改名会让本断言失败）：%s" % schemas
    for tool, path in sorted(M39_TOOL_PATHS.items()):
        ref = "#/components/schemas/%sIn" % tool
        body = paths[path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        if tool in M39_PARAM_TOOLS:
            assert body.get("$ref") == ref, \
                "%s 带参工具的请求体必须直接 $ref 到 %s，实际 %s" % (tool, ref, body)
            assert "anyOf" not in body, \
                "%s 带参工具的请求体不得为 Optional（anyOf），实际 %s" % (tool, body)
        else:
            assert body.get("anyOf") == [{"$ref": ref}, {"type": "null"}], \
                "%s 无参工具必须为 anyOf[$ref, null]，实际 %s" % (tool, body)
        resp = paths[path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert resp.get("type") == "object" and resp.get("additionalProperties") is True, \
            "%s 的 200 声明必须为自由对象（type=object / additionalProperties=true），实际 %s" % (tool, resp)
    health = paths["/health"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert not health.get("type") and not health.get("additionalProperties"), \
        "/health 的 200 声明不得带返回注解（应为空 schema），实际 %s" % health


def test_m39_openapi_declaration_alignment():
    control_m39_declaration(ct)


# M3.9 声明形态负控的变异项：必须在本节的 control_m39_declaration 定义**之后**追加
# （MUTATIONS 字面量在 §5 处求值，此处用 += 追加可保证直接函数引用而非前向名）。
MUTATIONS += [
    ("请求体模型名后缀改名（%sIn → %sRequest）",
     [('"%sIn" % spec["name"]', '"%sRequest" % spec["name"]')],
     control_m39_declaration),
    ("带参工具请求体注解改回 Optional（$ref → anyOf[$ref,null]）",
     [('endpoint.__annotations__ = {"payload": model, "return": dict}',
       'endpoint.__annotations__ = {"payload": Optional[model], "return": dict}')],
     control_m39_declaration),
    ("/health 补回返回注解（200 声明 {} → 自由对象）",
     [("def _health():  # M3.9", "def _health() -> Dict[str, Any]:  # M3.9")],
     control_m39_declaration),
]


# ===========================================================================
# 13. M4：nativeRef（条目级原生标识；缺口/歧义必须显式记账，绝不按规则拼）
# ===========================================================================
CLASS_BASE = ct.ONTO_BASE + "topology#"


def _responder_with_prop_iri(sparql, calls):
    if "?relationType" in sparql and "AS ?propIri" in sparql:
        return [{"relationType": "deployedOn", "fromType": "CreditCardApp",
                 "toType": "Host", "edges": 2, "srcs": 1, "dsts": 1,
                 "propIri": CLASS_BASE + "deployedOn"}]
    return []


def _index_with_refs(prop_iri="__default__"):
    idx = injected_index(DECLARED)
    idx["classIris"] = {name: CLASS_BASE + name for name in DECLARED}
    row = {"owner": "Host", "attribute": "hostName", "kind": "string", "support": 2}
    if prop_iri is not None:
        row["propIri"] = CLASS_BASE + "hostName" if prop_iri == "__default__" else prop_iri
    idx["attrRows"] = [row]
    return idx


def _service_with_refs(prop_iri="__default__"):
    service, backend = make_service(ct, _responder_with_prop_iri)
    service._type_cache = _index_with_refs(prop_iri)
    service._type_cache_at = time.time()
    return service, backend


def test_m4_native_ref_disclosure():
    section("13. M4 nativeRef（条目级原生标识 + 缺口/歧义负控）")

    def three_lists_carry_native_ref():
        service, _ = _service_with_refs()
        schema = service.build_schema({})
        assert schema["objectTypes"] and schema["relationTypes"] and schema["attributes"], schema
        for item in schema["objectTypes"]:
            assert item["nativeRef"] == "cc:" + item["objectType"], item
        for item in schema["relationTypes"]:
            assert item["nativeRef"] == "cc:" + item["relationType"], item
        for item in schema["attributes"]:
            assert item["nativeRef"] == "cc:" + item["attribute"], item
        guide = service.build_guide({})
        assert guide["objectTypes"], guide
        for item in guide["objectTypes"]:
            assert item["nativeRef"] == "cc:" + item["objectType"], item
        assert "nativeRef=null" not in (schema.get("hint") or ""), schema.get("hint")
        assert "nativeRef=null" not in (guide.get("hint") or ""), guide.get("hint")
    check("m4/nativeRef-three-lists", three_lists_carry_native_ref)

    def missing_is_null_and_accounted():
        # 负控一：本机一个原生 IRID 也没返回 ⇒ 全部置 null，且缺口逐条写进 hint
        service, _ = make_service(ct)
        service._type_cache = injected_index(DECLARED)
        service._type_cache_at = time.time()
        service._type_cache["attrRows"] = [
            {"owner": "Host", "attribute": "hostName", "kind": "string", "support": 1}]
        schema = service.build_schema({})
        assert schema["objectTypes"] and schema["attributes"], schema
        assert all(i["nativeRef"] is None for i in schema["objectTypes"]), schema["objectTypes"]
        assert all(i["nativeRef"] is None for i in schema["attributes"]), schema["attributes"]
        hint = schema["hint"] or ""
        assert "nativeRef=null" in hint, hint
        assert "有 %d 条" % (len(DECLARED) + 1) in hint, hint
        assert "objectTypes:" in hint, hint
        guide = service.build_guide({})
        assert all(i["nativeRef"] is None for i in guide["objectTypes"]), guide["objectTypes"]
        assert "nativeRef=null" in (guide["hint"] or ""), guide["hint"]

        # 负控二：只缺属性侧 ⇒ 缺口必须点名到 attributes，且对象类型侧不受影响
        service2, _ = _service_with_refs(prop_iri=None)
        schema2 = service2.build_schema({})
        assert all(i["nativeRef"] for i in schema2["objectTypes"]), schema2["objectTypes"]
        assert schema2["attributes"] and schema2["attributes"][0]["nativeRef"] is None
        hint2 = schema2["hint"] or ""
        assert "有 1 条" in hint2 and "attributes:Host.hostName" in hint2, hint2
    check("m4/nativeRef-missing-null-accounted", missing_is_null_and_accounted)

    def field_set_is_additive_only():
        service, _ = _service_with_refs()
        schema = service.build_schema({})
        expect = {
            "objectTypes": {"objectType", "keyAttrs", "nativeRef"},
            "relationTypes": {"relationType", "from", "to", "cardinality", "edgeCount",
                              "nativeRef"},
            "attributes": {"owner", "attribute", "valueType", "nativeRef"},
        }
        for name, keys in expect.items():
            assert schema[name], name
            for item in schema[name]:
                assert set(item) == keys, (name, sorted(item))
        guide = service.build_guide({})
        for item in guide["objectTypes"]:
            assert set(item) == {"objectType", "layer", "keyAttrs", "nativeRef"}, sorted(item)
    check("m4/nativeRef-field-set-additive-only", field_set_is_additive_only)

    def ambiguous_ref_is_disclosed_not_guessed():
        # 同一本地名在本机对应两个真实取值 ⇒ 取最小者并在 hint 里如实披露，不静默挑一个
        service, _ = make_service(ct, _responder_with_prop_iri)
        idx = injected_index(DECLARED)
        idx["classIris"] = {name: CLASS_BASE + name for name in DECLARED}
        idx["attrRows"] = [
            {"owner": "Host", "attribute": "hostName", "kind": "string", "support": 1,
             "propIri": CLASS_BASE + "hostName"},
            {"owner": "Host", "attribute": "hostName", "kind": "string", "support": 1,
             "propIri": ct.ONTO_BASE + "action#hostName"},
        ]
        service._type_cache = idx
        service._type_cache_at = time.time()
        schema = service.build_schema({})
        item = schema["attributes"][0]
        assert item["nativeRef"] == "cca:hostName", item      # 最小者（action# < topology#）
        hint = schema["hint"] or ""
        assert "有 1 条" in hint and "多个取值" in hint, hint
    check("m4/nativeRef-ambiguous-disclosed", ambiguous_ref_is_disclosed_not_guessed)

    def helpers_never_guess():
        assert ct.native_ref(None) is None and ct.native_ref("") is None
        assert ct.short_iri(None) is None and ct.short_iri("") is None
        assert ct.short_iri(CLASS_BASE + "Host") == "cc:Host"
        assert ct.short_iri(ct.ONTO_BASE + "metric#m_x") == "ccm:m_x"
        assert ct.short_iri(ct.ONTO_BASE + "prov#ruleCode") == "ccp:ruleCode"
        unknown = "http://example.org/ns#Thing"
        assert ct.short_iri(unknown) == unknown          # 不在前缀表内 ⇒ 原样，不拼写
        inst = ct.ONTO_BASE + "inst/dbcluster/x"
        assert ct.short_iri(inst) == inst
        assert ct.native_ref_hint([]) == ""
        text = ct.native_ref_hint([("objectTypes", "Metric")])
        assert "nativeRef=null" in text and "objectTypes:Metric" in text, text
    check("m4/nativeRef-helpers-never-guess", helpers_never_guess)

    def type_index_projects_native_iris():
        # D1 纪律：Python 读的键名必须与查询投影别名逐字一致（曾因别名不一致静默恒空）
        service, backend = make_service(ct)
        index = service.type_index(force=True)
        queries = "\n".join(c["sparql"] for c in backend.calls)
        assert "SAMPLE(STR(?s_c)) AS ?classIri" in queries, queries
        assert "SAMPLE(STR(?p)) AS ?propIri" in queries, queries
        assert index.get("classIris") == {}, index
        assert index.get("attrRows") == [], index
    check("m4/type-index-projects-native-iris", type_index_projects_native_iris)


# ===========================================================================
def main() -> int:
    section("1. 只读守卫负控（每条拒绝路径逐字错误码）")
    check("guard/controls", test_guard_controls)
    check("guard/force-limit-and-count", test_guard_force_limit_and_count)
    check("guard/static-assertions", test_guard_static_assertions)

    section("2. 参数校验负控")
    check("params/controls", test_param_controls)
    check("params/error-payload-shape", test_error_payload_shape)

    section("3. 信封上限（行/字节/空结果）")
    check("ledger/limits", test_ledger_limits)

    section("4. 纯函数（数值/排序/判据不可判定）")
    check("pure/helpers", test_pure_helpers)
    check("pure/rank-order", test_rank_candidates_order)
    check("pure/unjudgeable", test_evaluate_clause_unjudgeable_is_none)

    section("5. 源码级变异负控（阉掉守卫 ⇒ 负控必须失败）")
    check("mutation/falsifiable", test_mutations_are_falsifiable)

    section("6. 声明↔接受闭环（/schema → /facts）")
    check("loop/schema-feeds-facts", test_schema_declaration_feeds_facts)
    check("loop/single-identifier-space", test_declaration_acceptance_same_source)

    section("7. D1/D3 自查（投影别名、id coalesce、ABox 定界）")
    check("d1/projection-alias", test_d1_projection_alias_match)
    check("d1/type-index-alias", test_d1_type_index_projection_alias)
    check("d1/edge-id-roundtrip", test_d1_guide_causal_link_edge_id_roundtrip)
    check("d3/id-expression", test_d3_id_expression_has_coalesce_and_iri_fallback)

    section("8. 工具面与契约（真·G24 门禁模块交叉验证）")
    check("face/contract-conformance", test_toolface_contract_conformance)
    check("face/manifest-invariants", test_manifest_invariants)
    check("face/tool-paths", test_tool_paths_and_envelope_fields)
    check("face/read-facts", test_read_facts_face)
    check("face/read-facts-dispatch-contract", test_read_facts_dispatch_contract)
    check("face/read-facts-upstream-error-face", test_read_facts_upstream_error_face)
    check("face/response-keys-superset", test_response_keys_match_declared_face)

    section("9. 真机 smoke（只读，CC_M33_LIVE=1）")
    check("live/smoke", test_live_smoke)

    section("10. M3.4/M3.5/M3.6 硬化（本轮新增）")
    check("m34/open-low-range-bound", test_m34_open_low_range_bound)
    check("m35/truncation-accounting", test_m35_truncation_accounting)
    check("m35/exit-budget-guard", test_m35_exit_budget_guard)
    check("accounting/droppedRows-untouched-by-byte-path", test_m35_accounting_dropped_rows_strict)
    check("m36/audit-chain-fields", test_m36_audit_chain_fields)
    check("m36/latency-separated-from-elapsed-s", test_m36_latency_separated_from_elapsed_s)
    check("m36/dispatch-wiring", test_m36_dispatch_wires_latency_without_touching_response)

    section("11. R-27 审计链落盘（CC_AUDIT_LOG → 追加式 JSONL）")
    check("r27/disabled-without-env", test_r27_audit_disabled_without_env)
    check("r27/jsonl-append-and-same-record", test_r27_audit_jsonl_append_and_same_record)
    check("r27/write-failure-contained", test_r27_audit_write_failure_contained)
    check("r27/health-keys-additive", test_r27_health_audit_keys_additive)
    check("r27/one-line-per-call-dispatch", test_r27_one_line_per_call_dispatch)

    section("12. M3.9 声明形态（R-20/R-30：请求体/响应声明与模型名对齐）")
    check("m39/openapi-declaration-alignment", test_m39_openapi_declaration_alignment)

    # §13 自带节头与逐条 check（与 KG 侧 test_native_ref_disclosure 同构）
    test_m4_native_ref_disclosure()

    total = len(PASSED) + len(FAILED)
    print("\n测试：%d/%d PASS" % (len(PASSED), total))
    if FAILED:
        for name, detail in FAILED:
            print("  FAILED %s :: %s" % (name, detail))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
