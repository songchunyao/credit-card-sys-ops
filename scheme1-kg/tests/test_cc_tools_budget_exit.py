# -*- coding: utf-8 -*-
"""M3.5 出口字节上限：**最终 HTTP 响应体** ≤ 该工具所属档位的上限：7 个图工具 + read_facts 自述面。

为什么单独一个文件（而不是并进 test_cc_tools.py）：
`test_cc_tools.py` 测的是 `Ledger`/build_* 里的行/字节上限，度量发生在
`finish()` + `rowCount` 追加**之前**；而契约 `docs/07:215` 的判据是「任一工具**返回**」的字节数，
即**线上真正发出的那个字节串**。真机实测（跨载体门禁 guard_conformance）曾抓到
20098/20039/20110 B 的响应体 —— 差异正来自「信封之后再追加字段」。

★ 分档（P1/P2c 之后的口径；权威 `shared/model/tools-setb.yaml#bounds`）：
  数据面（`cc_query`/`cc_facts`/`cc_trace`/`cc_propose`…）`byte_cap = 20000`；
  元数据面（`cc_guide`/`cc_schema`）`metadata_byte_cap = 65536`（P1：目录被截断会丢掉
  全部 `host.*` 指标 ⇒ Agent 失去聚焦能力）；`cc_cause` `cause_byte_cap = 49152`
  （P2c：`rankBasis` ⑥ 要求候选集完整，20000 会从尾部 pop 掉 9–10 个候选）。
  **数据面的 20000 没有被放宽** —— 本文件用 `tier-caps-pinned-to-contract` 把
  「哪一档、上限多大、哪些工具属于该档」三层全部钉到契约上，防止有人为了绕过 20000
  而把工具悄悄塞进 `metadata_tools`。

本文件因此只做两件事：
  ① 端到端（经 FastAPI 真实序列化）量 7 个图工具的响应体字节数，逐工具按**其所属档位**断言
     （read_facts 是纯代理，出口预算由上游 :8304 负责，见第 4 节）；
  ② 负控可证伪：证明「不设上限时这些用例**确实会**超限」，
     否则上限断言是恒真的（无因果）。

只读：绝不写图、不发任何写语句。
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.join(os.path.dirname(HERE), "service")
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                       # pragma: no cover
        pass

import cc_tools as ct                                          # noqa: E402
import topology_reader as tr                                   # noqa: E402

# 契约里的真实取值（纯 YAML，离线）：假后端必须回真值，否则下游错误体回显全部合法类型。
_CONTRACT = tr.Contract()
REAL_TYPES = sorted(_CONTRACT.object_types) or ["DatabaseSessionPool"]
REAL_CODES = sorted({str(edge.get("from")) for edge in (_CONTRACT.causal_edges or [])}) \
    or ["FM-APP-POOL-EXHAUSTED"]

ALIAS_RE = re.compile(r"AS\s+([A-Za-z_][A-Za-z_0-9]*)")
NUMERIC_ALIASES = {"total", "count", "rowCount", "depth", "maxHops", "hopsUsed",
                   "cursor", "sortKey", "order", "index", "seq"}
# 文本型别名：给长字符串（撑字节数）；其余一律给**长数字串**——
# 数字串同样能撑满字节上限，但下游 `int()` 不会炸（这是本假后端唯一需要照顾的语义）。
TEXT_ALIAS_RE = re.compile(r"name|type|code|label|title|hint|unit|status|layer"
                           r"|severity|direction|reason|snapshot|digest|message", re.I)

PADDED_ROWS = 260          # > ROW_CAP(200)，让行上限与字节上限都需要真的动手
PADDED_CHARS = 400         # 单值长度：260 × 400 B ≈ 100 KB ≫ 20 KB

# 7 个图工具的最小合法请求体（取值来自 test_cc_tools.py 的真数据；read_facts 见第 4 节）
TOOL_BODIES = [
    ("cc_guide", {}),
    ("cc_schema", {}),
    ("cc_query", {"query": "MATCH (n) RETURN n"}),
    ("cc_facts", {"objectType": REAL_TYPES[0]}),
    ("cc_trace", {"startId": "dbpool:dn_6001-pool", "direction": "both", "maxHops": 2}),
    ("cc_cause", {"symptomCodes": [REAL_CODES[0]], "maxHops": 4}),
    ("cc_propose", {"rootCause": "FM-DB-SLOW-QUERY", "evidencePath": ["ev-1"]}),
]

# ===========================================================================
# ★ 分档上限（2026-10-16）：契约 `shared/model/tools-setb.yaml#bounds` 是唯一权威。
#   本文件原先把 7 个工具**一律**按 `BYTE_CAP=20000` 断言 —— 那是 P1 之前的口径，
#   在 `cc_guide` 真实载荷 851 KB（截断后 65 KB）下必然失败，且会逼着实现把
#   「元数据目录必须完整」这条 P1 判据回退掉。现在改为**逐工具按其档位断言**，
#   并把档位成员与三个常量一并钉到契约（见 tier_caps_pinned_to_contract）。
# ===========================================================================
_BOUNDS = (tr._read_yaml(tr.ROOT / "shared" / "model" / "tools-setb.yaml").get("bounds") or {})
TOOL_CAP = {tool: ct.byte_cap_for(tool) for tool, _ in TOOL_BODIES}
#: 数据面 = 既非元数据、也非 cc_cause 的那一档（必须维持 byte_cap=20000）
DATA_PLANE_TOOLS = [t for t, _ in TOOL_BODIES
                    if t not in ct.METADATA_TOOLS and t != "cc_cause"]


class PaddedBackend:
    """只读假后端：对**任何**语句都回大行，使每个工具的载荷都必然超 20 KB。

    只填 RETURN 里出现过的别名；`count(*) AS total` 单独回整数（否则下游 `int()` 会炸）。
    """

    kind = "padded"

    def __init__(self, rows=PADDED_ROWS, pad=PADDED_CHARS):
        self.rows = int(rows)
        self.pad = int(pad)
        self.calls = []
        self.last_query = ""
        self._seq = 0

    def cypher(self, statement, parameters=None, timeout=None):
        self.calls.append((statement, parameters, timeout))
        self.last_query = statement
        aliases = ALIAS_RE.findall(statement)
        if re.search(r"count\(\*\)\s+AS\s+total", statement, re.I):
            return {"columns": ["total"], "rows": [{"total": self.rows}], "elapsedMs": 1.0}
        if not aliases:
            return {"columns": [], "rows": [{"n": "z" * self.pad}] * self.rows,
                    "elapsedMs": 1.0}
        row = {}
        for alias in aliases:
            low = alias.lower()
            if "count" in low or "total" in low or alias in NUMERIC_ALIASES:
                row[alias] = 7
            elif alias == "objectType":
                # ★ 必须回**契约真实类型**：假值会让下游「未知 objectType」错误把
                # 全部合法类型回显进错误体（每个 400+ 字符）⇒ 巨型错误体。
                row[alias] = REAL_TYPES[self._seq % len(REAL_TYPES)]
            elif alias in ("fmCode", "symptomCode", "faultCode"):
                row[alias] = REAL_CODES[self._seq % len(REAL_CODES)]
            elif TEXT_ALIAS_RE.search(alias):
                row[alias] = ("%s-" % alias) + ("z" * self.pad)
            else:
                row[alias] = "9" * self.pad          # 长数字串：撑字节且 int() 安全
        return {"columns": sorted(set(aliases)),
                "rows": [dict(row) for _ in range(self.rows)], "elapsedMs": 1.0}


PASSED = []
FAILED = []


def check(name, fn):
    # ★ 失败消息必须截断：错误体本身可能有几十 KB（把合法类型全部回显），
    #   原样 print 会把 stdout 刷爆并淹没其它用例的读数。
    def _clip(text, limit=300):
        text = str(text)
        return text if len(text) <= limit else text[:limit] + "…（已截断，共 %d 字符）" % len(text)

    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, _clip(exc)))
        print("  FAIL  %-52s %s" % (name, _clip(exc)))
    except Exception as exc:                                # noqa: BLE001
        import traceback
        FAILED.append((name, "%s: %s" % (type(exc).__name__, _clip(exc))))
        print("  FAIL  %-52s %s: %s" % (name, type(exc).__name__, _clip(exc)))
        traceback.print_exc(limit=3)
    else:
        PASSED.append(name)
        print("  PASS  %s" % name)


def section(title):
    print("\n== %s ==" % title)


def _client(backend):
    from fastapi.testclient import TestClient
    return TestClient(ct.create_app(ct.CcToolsService(backend)))


def _wire_bytes(response):
    """线上真正的响应体字节数（不是我们自算的 _byte_len）。"""
    return len(response.content)


def test_exit_budget_all_graph_tools():
    section("1. 出口字节上限：7 个图工具的**最终 HTTP 响应体** ≤ 各自档位上限"
            "（元数据 %d / cc_cause %d / 数据面 %d）"
            % (ct.METADATA_BYTE_CAP, ct.CAUSE_BYTE_CAP, ct.BYTE_CAP))
    try:
        import fastapi  # noqa: F401
    except Exception as exc:                                # noqa: BLE001
        print("  SKIP  本机无 fastapi：%s" % exc)
        return

    measured = {}

    def tier_caps_pinned_to_contract():
        """三层上限、三个档位常量与档位成员，必须**逐项**来自契约；且数据面没有被放宽。"""
        assert _BOUNDS, "tools-setb.yaml#bounds 未读到 ⇒ 分档判据失去权威"
        assert ct.ROW_CAP == _BOUNDS["row_cap"], (ct.ROW_CAP, _BOUNDS.get("row_cap"))
        assert ct.BYTE_CAP == _BOUNDS["byte_cap"], (ct.BYTE_CAP, _BOUNDS.get("byte_cap"))
        assert ct.TOKEN_BUDGET == _BOUNDS["token_budget"], \
            (ct.TOKEN_BUDGET, _BOUNDS.get("token_budget"))
        assert ct.METADATA_BYTE_CAP == _BOUNDS["metadata_byte_cap"], \
            (ct.METADATA_BYTE_CAP, _BOUNDS.get("metadata_byte_cap"))
        assert ct.METADATA_ROW_CAP == _BOUNDS["metadata_row_cap"], \
            (ct.METADATA_ROW_CAP, _BOUNDS.get("metadata_row_cap"))
        assert ct.CAUSE_BYTE_CAP == _BOUNDS["cause_byte_cap"], \
            (ct.CAUSE_BYTE_CAP, _BOUNDS.get("cause_byte_cap"))
        assert tuple(ct.METADATA_TOOLS) == tuple(_BOUNDS["metadata_tools"]), \
            "档位成员与契约不一致（谁都能把工具塞进元数据档以绕过 20000）：%s vs %s" % (
                ct.METADATA_TOOLS, _BOUNDS.get("metadata_tools"))
        # 反向：数据面工具的上限必须**恒等于** byte_cap —— 不得借分档之名放宽。
        for tool in DATA_PLANE_TOOLS:
            assert TOOL_CAP[tool] == ct.BYTE_CAP, (tool, TOOL_CAP[tool], ct.BYTE_CAP)
        assert len(DATA_PLANE_TOOLS) >= 4, \
            "数据面样本太少，覆盖不足：%s" % DATA_PLANE_TOOLS
        print("        分档：元数据%s=%d / cc_cause=%d / 数据面 %d 个=%d" %
              (list(ct.METADATA_TOOLS), ct.METADATA_BYTE_CAP, ct.CAUSE_BYTE_CAP,
               len(DATA_PLANE_TOOLS), ct.BYTE_CAP))
    check("exit-budget/tier-caps-pinned-to-contract", tier_caps_pinned_to_contract)

    def body_cap_holds_for_every_tool():
        for tool, body in TOOL_BODIES:
            backend = PaddedBackend()
            client = _client(backend)
            path = ct.TOOLFACE[tool]["path"]
            resp = client.post(path, json=body)
            size = _wire_bytes(resp)
            payload = resp.json()
            measured[tool] = {
                "status": resp.status_code,
                "wireBytes": size,
                "cap": TOOL_CAP[tool],
                "truncated": bool(payload.get("truncated")),
                "droppedRows": payload.get("droppedRows"),
                "droppedBytes": payload.get("droppedBytes"),
                "hint": payload.get("hint"),
                "truncation": payload.get("truncation") or {},
            }
            assert resp.status_code in (200, 400, 502), \
                "%s 意外状态码 %s" % (tool, resp.status_code)
            cap = TOOL_CAP[tool]
            assert size <= cap, \
                "%s 最终响应体 %d B > 该档上限 %d B" % (tool, size, cap)
    check("exit-budget/graph-tools-wire-bytes<=tier-cap", body_cap_holds_for_every_tool)

    # 逐工具读数表（这本身就是证据，不只是断言）
    print("        %-12s %5s %9s %7s %10s %8s %12s %10s" %
          ("tool", "http", "wireBytes", "cap", "originalB", "dropped", "droppedBytes", "truncated"))
    for tool, _body in TOOL_BODIES:
        m = measured.get(tool) or {}
        t = m.get("truncation") or {}
        print("        %-12s %5s %9s %7s %10s %8s %12s %10s" %
              (tool, m.get("status"), m.get("wireBytes"), m.get("cap"),
               t.get("originalBytes"), t.get("droppedEntries"),
               m.get("droppedBytes"), m.get("truncated")))

    def oversize_cases_account_honestly():
        """凡是被削减过的用例，必须**如实记账**：不得静默截断（契约 C6）。"""
        ok200 = [t for t, m in measured.items() if m.get("status") == 200]
        assert len(ok200) >= 6, "只有 %d 个工具返回 200 ⇒ 覆盖不足：%s" % (len(ok200), ok200)
        cut = [t for t in ok200
               if measured[t]["truncated"] or measured[t]["truncation"]]
        # 覆盖要求：**不能只在 query 路径上验证**（队长第 4 条）——至少 3 个非 query 工具被真削减过。
        non_query_cut = [t for t in cut if t != "cc_query"]
        assert len(non_query_cut) >= 3, \
            "非 query 工具里被削减过的只有 %s ⇒ 覆盖不足" % non_query_cut
        for tool in cut:
            m = measured[tool]
            t = m["truncation"]
            assert m["truncated"] is True, "%s：削减了却没有 truncated=True" % tool
            assert t, "%s：truncated=True 却没有 truncation 记账" % tool
            assert int(m.get("droppedBytes") or 0) > 0, "%s：droppedBytes 没有记账" % tool
            assert int(t.get("droppedEntries") or 0) > 0, "%s：droppedEntries 没有记账" % tool
            assert int(t.get("originalBytes") or 0) > 0, "%s：originalBytes 没有记账" % tool
            assert int(t.get("bytes") or 0) <= TOOL_CAP[tool], (tool, t)
            assert int(t.get("limit") or 0) in (ct.BYTE_CAP, ct.ROW_CAP,
                                                ct.METADATA_BYTE_CAP,
                                                ct.METADATA_ROW_CAP), (tool, t)
            # 信封 hint 必须同时说明「本次返回不完整」+ 被截断条目数
            hint = str(m.get("hint") or "")
            assert "不完整" in hint, "%s：hint 未披露不完整：%s" % (tool, hint)
            assert "droppedEntries=%d" % int(t["droppedEntries"]) in hint, \
                "%s：hint 未给出被截断条目数：%s" % (tool, hint)
        # 反向：没被削减的用例必须**本来就在上限之内**（否则就是静默截断）
        for tool in ok200:
            if tool in cut:
                continue
            assert measured[tool]["wireBytes"] <= TOOL_CAP[tool], (tool, measured[tool])
            assert not measured[tool]["truncation"], \
                "%s 未被削减却有 truncation 记账 ⇒ 记账与事实不符" % tool
        print("        被削减：%s（其中非 query %s）；未削减：%s" %
              (cut, non_query_cut, [t for t in ok200 if t not in cut]))
    check("exit-budget/oversize-cases-account-honestly", oversize_cases_account_honestly)

    def exit_recheck_itself_is_exercised():
        """出口复核（信封之后再复核）必须**真的被触发过**，否则它在 e2e 里等于没测。"""
        hits = []
        for pad in (40, 50, 56, 60, 64, 70, 80, 90, 100, 120):
            backend = PaddedBackend(rows=200, pad=pad)
            service = ct.CcToolsService(backend)
            out = service.dispatch("cc_query", {"query": "MATCH (n) RETURN n"})
            t = out.get("truncation") or {}
            if str(t.get("stage") or "").startswith("ledger+enforce"):
                hits.append((pad, t.get("bytesBeforeBudget"), ct._byte_len(out)))
            assert ct._byte_len(out) <= ct.BYTE_CAP, (pad, ct._byte_len(out))
        assert hits, "在 pad∈[40,120] 里没有任何一轮触发出口复核 ⇒ 该路径未被覆盖"
        print("        出口复核被触发的点（pad, 复核前B, 复核后B）：%s" % hits[:4])
    check("exit-budget/exit-recheck-actually-fires", exit_recheck_itself_is_exercised)


def test_negative_control_is_falsifiable():
    section("2. 负控：不设上限时这些用例**确实会**超限（否则上一条断言恒真）")

    def raw_payload_really_exceeds_cap():
        backend = PaddedBackend()
        out = ct.build_query(backend, "MATCH (n) RETURN n")
        assert ct._byte_len(out) > ct.BYTE_CAP, \
            "未施加出口上限时也只有 %d B ⇒ 用例无法证伪" % ct._byte_len(out)
        before = ct._byte_len(out)
        bounded = ct.enforce_payload_budget(json.loads(json.dumps(out)))
        assert ct._byte_len(bounded) <= ct.BYTE_CAP
        assert ct._byte_len(bounded) < before
    check("negative-control/raw-payload-exceeds-20000", raw_payload_really_exceeds_cap)

    def other_tools_also_exceed_before_bounding():
        """非 query 工具同样要有「削减前确实超限」的证据（不只测 query 路径）。"""
        seen = []
        for tool, body in TOOL_BODIES:
            if tool == "cc_query":
                continue
            backend = PaddedBackend()
            service = ct.CcToolsService(backend)
            out = service.dispatch(tool, body)
            t = out.get("truncation") or {}
            # 负控必须针对**该工具自己的档位**：对元数据工具用 20000 判「超限」会把
            # 「本来就在 65536 之内」说成「削减前超限」——那是拿错尺子。
            if int(t.get("originalBytes") or 0) > TOOL_CAP[tool]:
                seen.append(tool)
            assert ct._byte_len(out) <= TOOL_CAP[tool], "%s 分派出口超限" % tool
        assert len(seen) >= 3, \
            "非 query 工具里只有 %d 个削减前超限 ⇒ 覆盖不足：%s" % (len(seen), seen)

        # 数据面纪律（20000）本身的负控覆盖：cc_query 上面被跳过，但它正是最典型的
        # 数据面样本，单独补一次，再统计「数据面里有几个工具削减前确实超 20000」。
        q_out = ct.CcToolsService(PaddedBackend()).dispatch(
            "cc_query", {"query": "MATCH (n) RETURN n"})
        q_t = q_out.get("truncation") or {}
        assert ct._byte_len(q_out) <= ct.BYTE_CAP, "cc_query 分派出口超限"
        assert int(q_t.get("originalBytes") or 0) > ct.BYTE_CAP, \
            "cc_query 削减前未超 20000 ⇒ 数据面负控失去最常见样本"
        plane_seen = [t for t in ["cc_query"] + seen if t in DATA_PLANE_TOOLS]
        plane_missed = [t for t in DATA_PLANE_TOOLS
                        if t not in plane_seen and t != "cc_query"]
        assert len(plane_seen) >= 3, \
            "数据面里只有 %d 个在 20000 下超限 ⇒ 数据面纪律的负控覆盖不足：%s" % (
                len(plane_seen), plane_seen)
        print("        削减前确实超限的非 query 工具：%s（其中数据面 %s）" %
              (", ".join(seen), ", ".join(plane_seen)))
        print("        数据面负控：%d/%d 个超过 20000；未超过（载荷天然小，边界已知）：%s" %
              (len(plane_seen), len(DATA_PLANE_TOOLS), plane_missed or "无"))
    check("negative-control/non-query-tools-exceed-before", other_tools_also_exceed_before_bounding)


def test_accounting_vocabulary():
    section("3. 统一记账口径：droppedRows（行上限）与 droppedEntries（字节路径）分列")

    def byte_path_does_not_touch_dropped_rows():
        payload = {"rows": [{"n": "z" * 500} for _ in range(300)],
                   "hint": "", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
        before = ct._byte_len(payload)
        assert before > ct.BYTE_CAP, before
        out = ct.enforce_payload_budget(json.loads(json.dumps(payload)))
        assert out["droppedRows"] == 0, "字节路径污染了 droppedRows：%s" % out["droppedRows"]
        assert out["droppedBytes"] > 0, out["droppedBytes"]
        assert out["droppedBytes"] <= before
        t = out["truncation"]
        assert t["bytesBeforeBudget"] == before, t
        assert t["originalBytes"] == before, t
        assert t["droppedEntries"] > 0, t
        assert t["droppedEntries"] == t["droppedItemsByBudget"], t
        assert "不完整" in out["hint"]
    check("accounting/droppedRows-untouched-by-byte-path", byte_path_does_not_touch_dropped_rows)

    def row_cap_drops_still_reported_as_rows():
        backend = PaddedBackend(rows=260, pad=10)
        out = ct.build_query(backend, "MATCH (n) RETURN n")
        assert out["truncated"] is True
        assert out["droppedRows"] == 60, out["droppedRows"]      # 260-200，纯行上限路径
        assert out["truncation"]["droppedRows"] == 60, out["truncation"]
        assert out["truncation"]["droppedEntries"] == 0, out["truncation"]
    check("accounting/row-cap-still-droppedRows", row_cap_drops_still_reported_as_rows)

    def hint_states_incomplete_and_sizes():
        payload = {"rows": [{"n": "z" * 500} for _ in range(300)],
                   "hint": "", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
        before = ct._byte_len(payload)
        out = ct.enforce_payload_budget(json.loads(json.dumps(payload)))
        hint = str(out["hint"])
        assert "不完整" in hint, hint
        assert str(before) in hint, "hint 未给出原始载荷大小：%s" % hint
        assert "droppedEntries=%d" % out["truncation"]["droppedEntries"] in hint, hint
    check("accounting/hint-says-incomplete+original+entries", hint_states_incomplete_and_sizes)

    def audit_chain_carries_entry_count():
        backend = PaddedBackend()
        service = ct.CcToolsService(backend)
        before = len(ct.AUDIT_CHAIN)
        out = service.dispatch("cc_query", {"query": "MATCH (n) RETURN n"})
        assert len(ct.AUDIT_CHAIN) == before + 1
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert sorted(rec) == sorted(ct.AUDIT_FIELDS), sorted(rec)
        assert "droppedEntries" in ct.AUDIT_FIELDS and "originalBytes" in ct.AUDIT_FIELDS
        t = out["truncation"]
        assert rec["droppedEntries"] == t["droppedEntries"], (rec, t)
        assert rec["originalBytes"] == t["originalBytes"], (rec, t)
        assert rec["truncated"] is True
        assert rec["resultBytes"] == ct._byte_len(out)
    check("audit/droppedEntries+originalBytes-recorded", audit_chain_carries_entry_count)


# ===========================================================================
# 4. read_facts 自述面（M5/S3a-1 第 8 个工具：纯代理到 :8304，不连网）
# ===========================================================================
def test_read_facts_face():
    section("4. read_facts：5 入参全 optional / 返回名=tool_level4 ∪ 信封4 / "
            "描述逐字同源 / 上游不可达 ⇒ 结构化错误")

    def params_are_all_optional():
        got = [(p["name"], p["type"], p["required"])
               for p in ct.TOOLFACE["read_facts"]["params"]]
        assert got == [("layers", "array", False), ("sections", "array", False),
                       ("scope", "string", False), ("max_records", "integer", False),
                       ("max_bytes", "integer", False)], got
        assert sum(len(ct.TOOLFACE[t]["params"]) for t in ct.TOOL_NAMES) == 16
    check("read-facts/params-5-all-optional", params_are_all_optional)

    def returns_are_tool_level_union_envelope():
        names = [r[0] for r in ct.TOOLFACE["read_facts"]["returns"]]
        assert names == ["objects", "relations", "observations", "source"], names
        envelope = {n for n, _t in ct.ENVELOPE_FIELDS}
        assert envelope == {"hint", "truncated", "droppedRows", "droppedBytes"}, envelope
        assert set(names) | envelope == {"objects", "relations", "observations", "source",
                                        "hint", "truncated", "droppedRows", "droppedBytes"}
        assert sum(len(ct.TOOLFACE[t]["returns"]) for t in ct.TOOL_NAMES) == 24
    check("read-facts/returns-tool-level4-union-envelope4",
          returns_are_tool_level_union_envelope)

    def description_matches_shape_authority():
        assert hashlib.sha256(ct.READ_FACTS_DESCRIPTION.encode("utf-8")).hexdigest() \
            == ct.READ_FACTS_DESCRIPTION_SHA256 == \
            "ee971388239b5210dfdd29367bccc547dc14e84982d3117edfb42ec5bec50237"
        assert ct.READ_FACTS_ENTRY["description"] == ct.READ_FACTS_DESCRIPTION
        assert ct.READ_FACTS_DESCRIPTION_BASIS == \
            "shared/model/live-facts-tools.yaml#read_facts.description"
        import yaml
        shape = os.path.join(os.path.dirname(os.path.dirname(HERE)), "shared", "model",
                             "live-facts-tools.yaml")
        with open(shape, encoding="utf-8") as fh:
            authority = yaml.safe_load(fh)["read_facts"]["description"]
        assert authority.rstrip("\n") == ct.READ_FACTS_DESCRIPTION, \
            "read_facts 描述与形状权威 live-facts-tools.yaml 不逐字相同"
    check("read-facts/description==shape-authority", description_matches_shape_authority)

    def upstream_unavailable_is_structured():
        saved = os.environ.get("CC_LIVE_FACTS_URL")
        os.environ["CC_LIVE_FACTS_URL"] = "http://127.0.0.1:1/tools/read_facts"
        try:
            exc = None
            try:
                ct.build_read_facts({"max_records": 1})
            except ct.ToolError as err:
                exc = err
            assert exc is not None, "上游不可达必须抛 ToolError，绝不静默返回空成功"
            assert exc.code == ct.ERR_UPSTREAM_UNAVAILABLE == "UPSTREAM_UNAVAILABLE", exc.code
            assert exc.detail.get("upstream") == "127.0.0.1:1", exc.detail
            assert exc.detail.get("connectTimeoutS") == ct.LIVE_FACTS_CONNECT_TIMEOUT_S, \
                exc.detail
            resp = _client(PaddedBackend()).post(
                ct.TOOLFACE["read_facts"]["path"], json={"max_records": 1})
            assert resp.status_code in (400, 502), resp.status_code
            body = resp.json()
            assert body["error"]["code"] == ct.ERR_UPSTREAM_UNAVAILABLE, body
            text = json.dumps(body, ensure_ascii=False)
            assert "Traceback" not in resp.text and "Traceback" not in text, text[:200]
            assert body.get("hint"), "错误面必须带结构化 hint（不是裸 traceback / 非空成功）"
            assert _wire_bytes(resp) <= ct.BYTE_CAP
        finally:
            if saved is None:
                os.environ.pop("CC_LIVE_FACTS_URL", None)
            else:
                os.environ["CC_LIVE_FACTS_URL"] = saved
    check("read-facts/upstream-unavailable-structured-error",
          upstream_unavailable_is_structured)


# ===========================================================================
# 4b. read_facts 的**上游错误面透传**（★ R-56/B）
#     纯代理不得把「上游收下请求但拒绝执行」说成「上游不可用」；
#     错误信封的 hint 必须与码相符（原先对所有码都写「被只读守卫拒绝」，对上游故障不实）。
# ===========================================================================
def test_read_facts_upstream_error_face():
    section("4b. read_facts：上游业务码原样透传 + hint 与码相符 + 声明面覆盖")

    def passthrough_registered_codes():
        cases = [
            ({"error": {"code": "BAD_PARAM", "message": "layers 含未知层名 ['db']"}}, 400),
            ({"error": {"code": "BLOCKED_BASE", "message": "覆盖门禁拒绝"}}, 409),
            ({"error": {"code": "BLOCKED_COVERAGE", "message": "G5-coverage 拒绝"}}, 409),
            ({"error": {"code": "UNKNOWN_PARAM", "message": "未知入参"}}, 400),
        ]
        for body, status in cases:
            code, message = ct.upstream_error_of(json.dumps(body).encode("utf-8"), status)
            assert code == body["error"]["code"], (code, body)
            assert body["error"]["message"] in message, message
            assert str(status) in message, message
    check("read-facts/upstream-code-passthrough", passthrough_registered_codes)

    def unregistered_and_garbage_fall_back():
        # 未登记的码 ⇒ 仍折成 UPSTREAM_UNAVAILABLE（不让未经审阅的码进入错误空间）
        code, _m = ct.upstream_error_of(json.dumps({"error": {"code": "WHAT"}}).encode(), 400)
        assert code == ct.ERR_UPSTREAM_UNAVAILABLE, code
        # 非 JSON（例如反代返回的 HTML）⇒ 传输类故障
        code, _m = ct.upstream_error_of(b"<html>502 Bad Gateway</html>", 502)
        assert code == ct.ERR_UPSTREAM_UNAVAILABLE, code
    check("read-facts/unregistered-code-falls-back", unregistered_and_garbage_fall_back)

    def hint_matches_code():
        assert ct.error_hint("INVALID_PARAM") == ct.GUARD_HINT
        assert ct.error_hint(None) == ct.GUARD_HINT
        assert ct.error_hint(ct.ERR_UPSTREAM_UNAVAILABLE) == ct.UPSTREAM_DOWN_HINT
        for c in ("BLOCKED_BASE", "BLOCKED_COVERAGE", "BLOCKED_INJECTION"):
            assert ct.error_hint(c) == ct.UPSTREAM_REFUSED_HINT, c
            # 真机故障的原话修正：不能再对上游 fail-closed 说「被只读守卫拒绝」
            assert "只读守卫" not in ct.error_hint(c), c
        for c in ("BAD_PARAM", "UNKNOWN_PARAM", "BAD_JSON", "PAYLOAD_TOO_LARGE"):
            assert ct.error_hint(c) == ct.UPSTREAM_PARAM_HINT, c
    check("read-facts/hint-matches-code", hint_matches_code)

    def declared_codes_cover_passthrough():
        declared = set(ct.TOOLFACE["read_facts"]["errorCodes"])
        missing = [c for c in ct.READ_FACTS_UPSTREAM_CODES if c not in declared]
        assert not missing, "read_facts 的 errorCodes 未登记可透传码：%s" % missing
        assert ct.ERR_UPSTREAM_UNAVAILABLE in declared
        assert "BLOCKED_BASE" in declared, "实现会透传它，声明面必须登记（此前漏记）"
    check("read-facts/errorCodes-cover-passthrough", declared_codes_cover_passthrough)


def main() -> int:
    print("M3.5 出口字节上限（最终响应体）离线单测：carrier=kg / BYTE_CAP=%d" % ct.BYTE_CAP)
    test_exit_budget_all_graph_tools()
    test_read_facts_face()
    test_read_facts_upstream_error_face()
    test_negative_control_is_falsifiable()
    test_accounting_vocabulary()
    print("\ntest: %d/%d PASS" % (len(PASSED), len(PASSED) + len(FAILED)))
    if FAILED:
        print("RESULT: FAIL")
        for name, why in FAILED:
            print("  - %s :: %s" % (name, why))
        return 1
    print("RESULT: PASS")
    return 0


def test_zz_no_failures() -> None:
    """pytest 聚合入口：逐个用例重跑，任一失败即让 pytest 失败。"""
    code = main()
    assert code == 0, "exit=%d" % code


if __name__ == "__main__":
    sys.exit(main())
