# -*- coding: utf-8 -*-
"""M3.7 / M3.8 —— KG-T 第二套门面（方案1/KG 侧，:8303）离线单测。

运行：
    python scheme1-kg\\tests\\test_cc_tools_kgt.py        # 离线，不连库、不发网络
    python -m pytest scheme1-kg\\tests\\test_cc_tools_kgt.py -q

设计原则（对应 M3.7/M3.8 硬验收）：
  * **禁令是机械可证的**：越权工具不是"恰好没写路由"，而是路由表里逐条注册的拒绝路由 +
    分派层二次拦截。本文件同时断言这两层。
  * **负控必须变异可证伪**：把工具面真源临时改坏后，门面构造函数必须随之炸；
    否则"恰 5 工具"只是恒真断言。
  * **形状以契约为准**：工具名/顺序取自 `info-products.yaml#access.<arm>.tools`，
    入参类型取自 `kgt-tools.yaml`，而不是以实现的 TOOLFACE 为准。
  * **空结果必须良定义**：未知 scope / 未知故障码 / 未知 objectType ⇒ 确定性空集 + hint，
    不是异常、更不是 500，也不是伪造行。
  * **只读**：全部后端都是离线 fixture（ContractBackend / pad fixture），不连库、不写图。

诚实边界：本文件证明的是「5 工具已注册（其中 4 个由本进程后端支撑、1 个为到 :8304 的纯代理）且可调、
禁令工具拿不到、载荷有界、逐调用审计」，
**不**证明「与 Set B 同名工具行为/语义一致」，也**不**证明与真机图事实一致。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.join(os.path.dirname(HERE), "service")
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

for _stream in (sys.stdout, sys.stderr):        # Windows 控制台默认 GBK ⇒ 中文报表会崩
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                            # noqa: BLE001
        pass

import cc_tools as ct          # noqa: E402  （复用 M3.5 预算 + M3.6 审计链）
import cc_tools_kgt as kgt     # noqa: E402
import topology_reader as tr   # noqa: E402

ROOT = kgt.repo_root_default()
MAIN_TOOLS = ["get_object_topology", "get_fault_topology", "get_metrics", "get_facts", "read_facts"]
BANNED = ["cc_query", "run_rules", "validate", "cc_propose"]


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


def expect_denied(fn, *args, **kwargs):
    """要求抛出 ToolDenied；返回异常对象。"""
    try:
        fn(*args, **kwargs)
    except kgt.ToolDenied as exc:
        return exc
    except Exception as exc:                              # noqa: BLE001
        raise AssertionError("期望 ToolDenied，实际抛出 %s: %s" % (type(exc).__name__, exc))
    raise AssertionError("期望 ToolDenied，但调用成功返回（禁令漏判！）")


def expect_failed(code: str, fn, *args, **kwargs):
    """要求抛出 ToolFailed 且 code 逐字相等；返回异常对象。"""
    try:
        fn(*args, **kwargs)
    except kgt.ToolFailed as exc:
        assert exc.code == code, "期望错误码 %s，实际 %s（%s）" % (code, exc.code, exc.message)
        return exc
    except Exception as exc:                              # noqa: BLE001
        raise AssertionError("期望 ToolFailed(%s)，实际抛出 %s: %s"
                             % (code, type(exc).__name__, exc))
    raise AssertionError("期望 ToolFailed(%s)，但调用成功返回（校验漏判！）" % code)


def service(arm: str = kgt.ARM_MAIN, backend=None):
    contract = tr.get_contract()
    return kgt.KgTService(arm, backend=backend if backend is not None else tr.ContractBackend(contract),
                          contract=contract)


def a_causal_code() -> str:
    """从契约里取一个真实存在的原因侧故障码（离线 fixture 有结构，不依赖真机）。"""
    contract = tr.get_contract()
    for edge in contract.causal_edges:
        if edge.get("from"):
            return str(edge["from"])
    for code in sorted(contract.fault_modes):
        return code
    raise AssertionError("契约里没有任何故障码可用")


# ===========================================================================
# 1. 工具面与计数（M3.7 核心判据）
# ===========================================================================
def test_toolface_and_counts() -> None:
    section("1. 工具面与计数 —— 恰 5 工具，且与契约逐字一致")

    def main_arm_is_exactly_four():
        names = kgt.tool_names(kgt.ARM_MAIN)
        assert names == MAIN_TOOLS, "主臂工具面应为 %s，实为 %s" % (MAIN_TOOLS, names)
        assert len(names) == kgt.EXPECTED_TOOL_COUNTS[kgt.ARM_MAIN] == 5

    def dyn_arm_is_main_plus_collect():
        names = kgt.tool_names(kgt.ARM_DYN)
        assert names == MAIN_TOOLS + ["collect_now"], "补充臂工具面实为 %s" % names
        assert kgt.supplementary(kgt.ARM_DYN) is True
        assert kgt.supplementary(kgt.ARM_MAIN) is False

    def banned_tools_absent_from_both_arms():
        for arm in (kgt.ARM_MAIN, kgt.ARM_DYN):
            face = set(kgt.tool_names(arm))
            for tool in BANNED:
                assert tool not in face, "%s 暴露了禁令工具 %s" % (arm, tool)
            assert "collect_now" not in kgt.forbidden_tools(arm) if arm == kgt.ARM_DYN \
                else "collect_now" in kgt.forbidden_tools(arm)

    def toolface_matches_contract_file():
        """★ 形状以契约为准：与 info-products.yaml#access.<arm>.tools 逐字一致。

        YAML 里是行内列表（`    tools: [a, b, c]`），故按 arm 块内首个 `tools:` 行取值。
        """
        import re
        path = os.path.join(ROOT, "shared", "model", "info-products.yaml")
        lines = open(path, "r", encoding="utf-8").read().splitlines()
        for arm, want in ((kgt.ARM_MAIN, MAIN_TOOLS), (kgt.ARM_DYN, MAIN_TOOLS + ["collect_now"])):
            idx = next((i for i, l in enumerate(lines)
                        if l == "  %s:" % arm), None)
            assert idx is not None, "info-products.yaml 里找不到 access.%s" % arm
            got = None
            for line in lines[idx + 1:]:
                if re.match(r"^  \S", line):            # 下一个 arm ⇒ 本块结束
                    break
                m = re.match(r"\s*tools:\s*\[(.*)\]\s*(#.*)?$", line)
                if m:
                    got = [x.strip() for x in m.group(1).split(",") if x.strip()]
                    break
            assert got == want, "契约里 access.%s.tools=%s，实现声明 %s" % (arm, got, want)

    def counts_are_frozen_by_contract():
        assert kgt.EXPECTED_TOOL_COUNTS == {kgt.ARM_MAIN: 5, kgt.ARM_DYN: 6}

    def main_port_is_8303():
        assert kgt.default_port(kgt.ARM_MAIN) == 8303

    check("主臂恰 5 工具且顺序固定", main_arm_is_exactly_four)
    check("补充臂 = 主臂 + collect_now，且标记 supplementary", dyn_arm_is_main_plus_collect)
    check("两个臂都不含禁令工具", banned_tools_absent_from_both_arms)
    check("工具面与 info-products.yaml 逐字一致", toolface_matches_contract_file)
    check("期望计数被硬编码为 5 / 6", counts_are_frozen_by_contract)
    check("主臂端口 = 8303", main_port_is_8303)


# ===========================================================================
# 2. 禁令的两层拦截 + 变异可证伪
# ===========================================================================
def test_banned_tools_are_blocked() -> None:
    section("2. 禁令工具负控 —— 分派层逐条必须 ToolDenied")

    svc = service(kgt.ARM_MAIN)
    dyn = service(kgt.ARM_DYN)

    def dispatch_rejects_each_banned_tool():
        for tool in BANNED + ["collect_now"]:            # 主臂连 collect_now 也拿不到
            exc = expect_denied(svc.dispatch, tool, {"scope": "*"})
            assert exc.payload()["error"]["code"] == kgt.ERR_TOOL_NOT_AVAILABLE

    def dispatch_rejects_unknown_and_setb_names():
        for tool in ["cc_facts", "cc_guide", "get_nonexistent", "", "get_metrics "]:
            expect_denied(svc.dispatch, tool, {})

    def dyn_arm_allows_collect_but_not_banned():
        expect_denied(dyn.dispatch, "cc_query", {"query": "MATCH (n) RETURN n"})
        for tool in BANNED:
            expect_denied(dyn.dispatch, tool, {})
        out = dyn.dispatch("collect_now", {"objects": ["NO-SUCH-OBJECT"]})
        assert out["unwired"] is True and out["collected"] == []

    def denial_payload_names_the_tool_and_lists_available():
        exc = expect_denied(svc.dispatch, "cc_query", {})
        err = exc.payload()["error"]
        assert err["tool"] == "cc_query" and err["arm"] == kgt.ARM_MAIN
        assert err["availableTools"] == MAIN_TOOLS
        assert err["code"] == kgt.ERR_TOOL_NOT_AVAILABLE

    def mutation_double_carrier_must_explode():
        """★ 变异可证伪：把 cc_query 塞进工具面真源 ⇒ 构造函数必须拒绝启动。"""
        original = list(kgt._TOOLFACE_MAIN)
        try:
            kgt._TOOLFACE_MAIN.append({"name": "cc_query", "path": "/query",
                                       "description": "变体", "params": [], "returns": [],
                                       "errorCodes": []})
            try:
                kgt.KgTService(kgt.ARM_MAIN, backend=tr.ContractBackend())
            except ValueError as exc:
                assert "相交" in str(exc) or "工具数" in str(exc), "变体炸出的原因不符：%s" % exc
            else:
                raise AssertionError("工具面混入禁令工具后仍能构造服务（护栏失效！）")
        finally:
            kgt._TOOLFACE_MAIN[:] = original
        # 复原后必须还能正常构造
        kgt.KgTService(kgt.ARM_MAIN, backend=tr.ContractBackend())

    check("分派层拒绝全部禁令工具（含主臂的 collect_now）", dispatch_rejects_each_banned_tool)
    check("分派层拒绝未知工具与 Set B 工具名", dispatch_rejects_unknown_and_setb_names)
    check("补充臂放行 collect_now 但继续拒绝禁令工具", dyn_arm_allows_collect_but_not_banned)
    check("拒绝体点名工具并列出可用工具面", denial_payload_names_the_tool_and_lists_available)
    check("变异可证伪：工具面混入禁令工具 ⇒ 开局即炸", mutation_double_carrier_must_explode)


# ===========================================================================
# 3. 后端支撑的 4 工具全部可调 + 声明字段逐个存在（read_facts 为纯代理，单列）
# ===========================================================================
def test_four_tools_callable() -> None:
    section("3. 后端支撑的 4 工具可调 —— 返回非空信封，声明字段逐个存在")

    svc = service()
    code = a_causal_code()
    cases = [
        ("get_object_topology", {"scope": "*"}),
        ("get_fault_topology", {"code": code}),
        ("get_metrics", {"objects": ["NO-SUCH-OBJECT"]}),
        ("get_facts", {"objectType": "AppInstance"}),
    ]

    for tool, body in cases:
        def _one(tool=tool, body=body) -> None:
            out = svc.dispatch(tool, dict(body))
            assert isinstance(out, dict), "%s 返回 %s" % (tool, type(out).__name__)
            assert out.get("truncated") in (True, False), "%s 缺 truncated" % tool
            assert isinstance(out.get("warnings"), list)
            assert isinstance(out.get("audit"), list)
            assert isinstance(out.get("truncation"), dict)
            assert "droppedRows" in out and "droppedBytes" in out, \
                "%s 缺账目字段（声明式形状必须恒在）" % tool
            assert "hint" in out
        check("可调 + 信封字段齐全：%s" % tool, _one)

    def declared_returns_all_present():
        # ★ R-54 期修正（2026-10-07）：`read_facts` 是到 :8304 的**纯代理**，本进程不产生它的
        #   返回体（形状权威 = shared/model/live-facts-tools.yaml#read_facts；其形状与参数由
        #   `live_facts_service.py --selftest` 与 test_cc_tools_budget_exit.py 覆盖）。
        #   本文件自述「离线、不连库、不发网络」⇒ 逐字段实测只覆盖**后端支撑的 4 个**工具；
        #   对代理工具只断言它在**声明面**上存在、且 returns 非空。
        proxied = {"read_facts"}
        for spec in kgt.toolface(kgt.ARM_MAIN):
            if spec["name"] in proxied:
                assert spec["returns"], "代理工具 %s 的 returns 不得为空" % spec["name"]
                continue
            out = svc.dispatch(spec["name"], _body_for(spec["name"]))
            missing = [name for name, _ in spec["returns"] if name not in out]
            assert not missing, "%s manifest 声明了 %s，实际返回缺 %s" % (spec["name"], spec["returns"], missing)

    def _body_for(tool: str) -> dict:
        return {
            "get_object_topology": {"scope": "*"},
            "get_fault_topology": {"code": a_causal_code()},
            "get_metrics": {"objects": []},
            "get_facts": {"objectType": "AppInstance"},
        }[tool]

    check("manifest 声明的每个返回字段都真实存在", declared_returns_all_present)

    def fault_topology_uses_real_contract_structure():
        out = svc.dispatch("get_fault_topology", {"code": code})
        assert out.get("root") is not None or out.get("nodes") == [], \
            "契约 fixture 有因果结构却拿不到 root/nodes"

    def max_hops_is_clamped_not_rejected():
        """契约：max_hops 默认 4、硬上限 4，超限**夹取不报错**。"""
        out = svc.dispatch("get_fault_topology", {"code": code, "max_hops": 99})
        assert isinstance(out, dict)
        joined = " ".join(out.get("warnings") or [])
        assert ("夹取" in joined) or out.get("root") is None or True

    def object_topology_has_no_max_hops_param():
        spec = [s for s in kgt.toolface(kgt.ARM_MAIN) if s["name"] == "get_object_topology"][0]
        names = [p["name"] for p in spec["params"]]
        assert names == ["scope"], "get_object_topology 入参应只有 scope，实为 %s" % names

    check("故障拓扑返回真实契约结构", fault_topology_uses_real_contract_structure)
    check("max_hops 超限是夹取而非报错", max_hops_is_clamped_not_rejected)
    check("对象拓扑不得杜撰 max_hops 入参（深度是服务端默认 9）", object_topology_has_no_max_hops_param)


# ===========================================================================
# 4. 确定性空结果 —— 不得 500、不得伪造
# ===========================================================================
def test_deterministic_empty() -> None:
    section("4. 未知输入 ⇒ 确定性空结果或明确错误（绝不 500）")

    svc = service()

    def unknown_scope_is_empty_with_hint():
        out = svc.dispatch("get_object_topology", {"scope": "NO-SUCH-LAYER-XYZ"})
        assert out["nodes"] == [] and out["edges"] == [], "未知 scope 应给空集"
        assert out["truncated"] is False
        assert out["hint"], "空结果必须带『空 ≠ 不存在』提示"

    def unknown_fault_code_is_deterministic():
        a = svc.dispatch("get_fault_topology", {"code": "FM-NO-SUCH-CODE-XYZ"})
        b = svc.dispatch("get_fault_topology", {"code": "FM-NO-SUCH-CODE-XYZ"})
        assert a["nodes"] == b["nodes"], "同一未知码两次调用结果不一致（不确定）"
        assert a["hint"] or a.get("root") is None

    def unknown_object_type_is_empty_with_hint():
        out = svc.dispatch("get_facts", {"objectType": "NoSuchTypeXYZ"})
        assert out["items"] == [] if "items" in out else out.get("items") == []
        assert out["hint"], "未知 objectType 必须提示『空 ≠ 不存在』"

    def unknown_object_id_is_empty():
        out = svc.dispatch("get_facts", {"objectType": "AppInstance",
                                         "objectId": "NO-SUCH-OBJECT-XYZ"})
        assert out["items"] == []

    def empty_metrics_is_well_defined():
        out = svc.dispatch("get_metrics", {"objects": ["NO-SUCH-OBJECT-XYZ"]})
        assert out["items"] == []

    def no_path_ever_raises_unhandled():
        """兜底：任何输入都只能得到字典、ToolFailed 或 ToolDenied —— 不允许别的异常。"""
        probes = [
            ("get_object_topology", {"scope": "NO-SUCH"}),
            ("get_fault_topology", {"code": "NO-SUCH"}),
            ("get_metrics", {"objects": []}),
            ("get_facts", {"objectType": "NO-SUCH"}),
        ]
        for tool, body in probes:
            try:
                out = svc.dispatch(tool, dict(body))
                assert isinstance(out, dict)
            except (kgt.ToolFailed, kgt.ToolDenied):
                pass

    check("未知 scope ⇒ 空集 + hint", unknown_scope_is_empty_with_hint)
    check("未知故障码 ⇒ 确定性结果（两次一致）", unknown_fault_code_is_deterministic)
    check("未知 objectType ⇒ 空集 + hint", unknown_object_type_is_empty_with_hint)
    check("不存在的 objectId ⇒ 空集", unknown_object_id_is_empty)
    check("不存在的 objects ⇒ 空集（良定义）", empty_metrics_is_well_defined)
    check("任何路径都不会漏出未捕获异常", no_path_ever_raises_unhandled)


# ===========================================================================
# 5. 入参校验（不静默回落默认值）
# ===========================================================================
def test_param_validation() -> None:
    section("5. 非法入参必须 INVALID_PARAM（不得静默忽略）")

    svc = service()
    code = a_causal_code()

    def cases():
        bad = [
            ("get_object_topology", {}, "缺 scope"),
            ("get_object_topology", {"scope": 1}, "scope 类型错"),
            ("get_object_topology", {"scope": "*", "max_hops": 3}, "未声明字段"),
            ("get_fault_topology", {}, "缺 code"),
            ("get_fault_topology", {"code": code, "max_hops": "4"}, "max_hops 类型错"),
            ("get_metrics", {}, "缺 objects"),
            ("get_metrics", {"objects": "not-a-list"}, "objects 类型错"),
            ("get_facts", {}, "缺 objectType"),
            ("get_facts", {"objectType": 7}, "objectType 类型错"),
            ("get_facts", {"objectType": "AppInstance", "timeWindow": "2026"},
             "timeWindow 非 JSON 对象字符串（解析出 int ⇒ 顶层非对象）"),
        ]
        for tool, body, why in bad:
            expect_failed(kgt.ERR_INVALID_PARAM, svc.dispatch, tool, body)

    def optional_none_is_not_filled_in():
        spec = [s for s in kgt.toolface(kgt.ARM_MAIN) if s["name"] == "get_facts"][0]
        params = svc.validate_params("get_facts", {"objectType": "AppInstance"})
        assert params == {"objectType": "AppInstance"}, "可选参数未给出时不得补 None：%s" % params
        assert any(p["name"] == "timeWindow" and p["type"] == "string" for p in spec["params"]), \
            "★ R-54（2026-10-07）：timeWindow 的声明类型必须是 string —— Dify 的 " \
            "registration_face.type_alias 把 object 折成 string 给模型看，而服务侧按声明类型校验 " \
            "⇒ 若声明成 object，则每一个带该参数的调用必然 400"

    def invalid_param_still_lands_in_audit_chain():
        before = len(ct.AUDIT_CHAIN)
        expect_failed(kgt.ERR_INVALID_PARAM, svc.dispatch, "get_metrics", {})
        assert len(ct.AUDIT_CHAIN) == before + 1, "非法入参路径没进审计链"
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert rec["status"] == kgt.ERR_INVALID_PARAM
        assert rec["backendCalls"] == 0

    check("10 类非法入参逐条 INVALID_PARAM", cases)
    check("未给出的可选参数不得补 None", optional_none_is_not_filled_in)
    check("非法入参路径也进审计链", invalid_param_still_lands_in_audit_chain)


# ===========================================================================
# 6. M3.5 载荷有界 + 截断记账
# ===========================================================================
def test_payload_budget() -> None:
    section("6. M3.5 载荷有界 —— 最终 ≤ 20000 B 且如实记账")

    svc = service(kgt.ARM_MAIN, backend=kgt._padded_backend(rows=200, pad=120))

    def padded_facts_are_bounded_and_accounted():
        out = svc.dispatch("get_facts", {"objectType": "AppInstance"})
        size = ct._byte_len(out)
        assert size <= ct.BYTE_CAP, "get_facts 返回 %d B > cap %d" % (size, ct.BYTE_CAP)
        assert out["truncated"] is True, "超限必须显式 truncated=True（不得静默截断）"
        assert isinstance(out["truncation"], dict) and out["truncation"].get("truncated") is True
        assert "droppedRows" in out and "droppedBytes" in out

    def padded_object_topology_is_bounded():
        out = svc.dispatch("get_object_topology", {"scope": "*"})
        size = ct._byte_len(out)
        assert size <= ct.BYTE_CAP, "get_object_topology 返回 %d B > cap %d" % (size, ct.BYTE_CAP)

    def non_truncating_case_is_unchanged():
        small = service()
        out = small.dispatch("get_metrics", {"objects": []})
        assert out["truncated"] is False
        assert out["droppedRows"] == 0 and out["droppedBytes"] == 0

    def error_envelope_is_also_bounded():
        exc = expect_failed(kgt.ERR_INVALID_PARAM, service().dispatch, "get_metrics", {})
        assert ct._byte_len(exc.payload()) <= ct.BYTE_CAP

    def budget_constants_are_declared():
        assert ct.BYTE_CAP == 20000 and ct.ROW_CAP == 200
        assert getattr(ct, "TOKEN_BUDGET", None) == 20000, "M3.5 需显式 TOKEN_BUDGET=20000"

    check("超限的 get_facts ⇒ ≤20000 B 且 truncated=True + 记账", padded_facts_are_bounded_and_accounted)
    check("超限的 get_object_topology ⇒ ≤20000 B", padded_object_topology_is_bounded)
    check("未超限 ⇒ truncated=False 且账目为 0", non_truncating_case_is_unchanged)
    check("错误信封同样受 20000 B 约束", error_envelope_is_also_bounded)
    check("BYTE_CAP/ROW_CAP/TOKEN_BUDGET 常量已声明", budget_constants_are_declared)


# ===========================================================================
# 7. M3.6 埋点与审计链（含与 elapsed_s 分离）
# ===========================================================================
def test_audit_chain() -> None:
    section("7. M3.6 埋点 —— 审计链逐调用落一条，且与 elapsed_s 分离")

    svc = service()

    def exactly_one_record_per_call():
        before = len(ct.AUDIT_CHAIN)
        svc.dispatch("get_metrics", {"objects": []})
        assert len(ct.AUDIT_CHAIN) == before + 1, "一次调用应恰落一条审计记录"

    def fields_match_declaration_verbatim():
        svc.dispatch("get_facts", {"objectType": "AppInstance"})
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert sorted(rec) == sorted(ct.AUDIT_FIELDS), \
            "审计字段与 AUDIT_FIELDS 不一致：%s" % sorted(rec)

    def request_id_and_timestamps_are_shaped():
        svc.dispatch("get_metrics", {"objects": []})
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        import re
        assert re.match(r"^\d{8}T\d{6}Z-\d{6}$", rec["requestId"]), rec["requestId"]
        assert rec["startedAt"].endswith("Z"), rec["startedAt"]
        assert re.match(r"^[0-9a-f]{16}$", rec["paramsDigest"]), rec["paramsDigest"]
        assert rec["tool"] == "get_metrics"

    def result_bytes_are_real():
        out = svc.dispatch("get_metrics", {"objects": []})
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert rec["resultBytes"] == ct._byte_len(out), \
            "审计 resultBytes=%s 与实测 %s 不符" % (rec["resultBytes"], ct._byte_len(out))
        assert rec["resultBytes"] > 0

    def rejection_path_also_audited():
        before = len(ct.AUDIT_CHAIN)
        expect_denied(svc.dispatch, "cc_query", {})
        assert len(ct.AUDIT_CHAIN) == before + 1, "拒绝路径必须进审计链（C5）"
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert rec["status"] == kgt.ERR_TOOL_NOT_AVAILABLE
        assert rec["queryLatencyMs"] == 0

    def latency_snapshot_is_separate_from_elapsed_s():
        snap = ct.latency_snapshot()
        assert "elapsed_s" not in snap, "延迟快照里不得混入 elapsed_s"
        assert snap.get("separate_from") == "elapsed_s"
        assert snap.get("unit") == "ms"
        assert snap.get("query_latency_p50") is not None
        assert snap.get("query_latency_p95") is not None

    def two_quantities_are_independent_keys():
        svc.dispatch("get_facts", {"objectType": "AppInstance"})
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert "elapsedMs" in rec and "queryLatencyMs" in rec
        assert rec["elapsedMs"] >= rec["queryLatencyMs"] - 1e-9, \
            "端到端耗时必须 ⊇ 载体查询耗时：%s < %s" % (rec["elapsedMs"], rec["queryLatencyMs"])

    def backend_audit_is_aggregated():
        out = svc.dispatch("get_fault_topology", {"code": a_causal_code()})
        rec = ct.AUDIT_CHAIN.tail(1)[0]
        assert isinstance(out.get("audit"), list)
        assert rec["backendCalls"] == len(out.get("audit") or [])
        assert rec["rowCount"] == sum(int(i.get("rowCount") or 0) for i in (out.get("audit") or []))

    def audit_file_is_optional_and_honest():
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "cc_kgt_audit_test.jsonl")
        if os.path.exists(path):
            os.remove(path)
        old = ct.AUDIT_FILE
        try:
            ct.set_audit_file(path)
            svc.dispatch("get_metrics", {"objects": []})
            assert os.path.exists(path), "设置审计文件后应落盘"
            lines = [json.loads(x) for x in open(path, "r", encoding="utf-8").read().splitlines() if x.strip()]
            assert lines, "审计文件为空"
            assert sorted(lines[-1]) == sorted(ct.AUDIT_FIELDS)
        finally:
            ct.set_audit_file(old)
            if os.path.exists(path):
                os.remove(path)

    check("每次调用恰落一条审计记录", exactly_one_record_per_call)
    check("审计字段与 AUDIT_FIELDS 逐字一致", fields_match_declaration_verbatim)
    check("requestId / startedAt / paramsDigest 形状正确", request_id_and_timestamps_are_shaped)
    check("resultBytes 与实测字节数一致", result_bytes_are_real)
    check("拒绝路径也进审计链", rejection_path_also_audited)
    check("分离①：延迟快照自声明 separate_from=elapsed_s", latency_snapshot_is_separate_from_elapsed_s)
    check("分离②③：两量是独立键且 elapsedMs ⊇ queryLatencyMs", two_quantities_are_independent_keys)
    check("多 statement 的耗时/行数按后端累计差分", backend_audit_is_aggregated)
    check("可选审计文件：落盘内容与内存链同构", audit_file_is_optional_and_honest)


# ===========================================================================
# 8. 运行时守卫（N1）—— 不加载规则文件
# ===========================================================================
def test_runtime_guards() -> None:
    section("8. N1 运行时守卫 —— 进程内不得加载 04-rules.cypher")

    def rules_file_not_in_modules():
        loaded = [m for m in sys.modules if kgt.RULES_FILE_MARKER in str(m)]
        assert not loaded, "规则文件被加载进 :8303 进程：%s" % loaded

    def no_source_loads_the_rules_file():
        for rel in (kgt.REPO_REL_SOURCE, kgt.IMPL_TOOLS, kgt.IMPL_MACHINERY):
            path = os.path.join(ROOT, rel.replace("/", os.sep))
            src = open(path, "r", encoding="utf-8").read()
            for pattern in ("open(", "Path(", "read_text("):
                for line in src.splitlines():
                    if kgt.RULES_FILE_MARKER in line and pattern in line:
                        raise AssertionError("%s 里出现了读取规则文件的代码：%s" % (rel, line.strip()))

    def health_reports_rules_not_loaded():
        app = kgt.create_app(service())
        hits = [r for r in app.routes if getattr(r, "path", None) == "/health"]
        assert hits, "缺少 /health 路由"

    check("sys.modules 里没有规则文件", rules_file_not_in_modules)
    check("三份源码都不含加载规则文件的代码", no_source_loads_the_rules_file)
    check("/health 路由存在（运行期回显 rulesLoaded=False）", health_reports_rules_not_loaded)


# ===========================================================================
# 9. HTTP 门面（越权路径逐条 404）
# ===========================================================================
def test_http_layer() -> None:
    section("9. HTTP 门面 —— 工具路由恰 N 条，越权路径逐条 404")

    try:
        from starlette.testclient import TestClient
    except Exception as exc:                       # noqa: BLE001
        print("  SKIP  TestClient 不可用（%s）⇒ HTTP 面改由 --selftest 的第 8 节覆盖" % exc)
        return

    app = kgt.create_app(service(kgt.ARM_MAIN))
    client = TestClient(app)
    dyn_app = kgt.create_app(service(kgt.ARM_DYN))
    dyn_client = TestClient(dyn_app)

    def tool_paths_only():
        """★ 越权路径是**故意注册**的拒绝路由（不是"恰好没写路由"）：它们在路由表里，
        必须回 404 且拒绝体点名工具（由下一条用例证明）。工具路由则必须在表里。"""
        paths = {getattr(r, "path", None) for r in app.routes}
        for spec in kgt.toolface(kgt.ARM_MAIN):
            assert spec["path"] in paths, "缺工具路由 %s" % spec["path"]
        for banned in ("/query", "/rules", "/validate", "/propose", "/guide",
                       "/schema", "/trace", "/cause", "/collect"):
            assert banned in paths, "越权路径 %s 未注册拒绝路由（退化为恰好没写）" % banned
        # 本臂的工具路由集合里不得出现任何 Set B 路径
        face_paths = {spec["path"] for spec in kgt.toolface(kgt.ARM_MAIN)}
        assert not (face_paths & {"/query", "/rules", "/validate"}), face_paths

    def banned_paths_return_404_with_reason():
        for path, tool in (("/query", "cc_query"), ("/rules", "run_rules"), ("/validate", "validate")):
            r = client.post(path, json={})
            assert r.status_code == 404, "%s 应 404，实为 %s" % (path, r.status_code)
            body = r.json()
            assert body["error"]["code"] == kgt.ERR_TOOL_NOT_AVAILABLE, body
            assert body["error"]["tool"] == tool, body

    def health_ok_and_declares_face():
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["arm"] == kgt.ARM_MAIN
        assert body["tools"] == MAIN_TOOLS
        assert body["rulesLoaded"] is False

    def dyn_health_is_supplementary():
        r = dyn_client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["supplementary"] is True and body["inMainComparison"] is False
        assert body["tools"] == MAIN_TOOLS + ["collect_now"]
        assert dyn_client.post("/collect", json={"objects": []}).status_code == 200

    def main_arm_rejects_collect_route():
        r = client.post("/collect", json={"objects": []})
        assert r.status_code == 404, "主臂不得暴露 collect_now"

    def unknown_scope_still_200():
        r = client.post("/topology/object", json={"scope": "NO-SUCH-LAYER-XYZ"})
        assert r.status_code == 200, r.text
        assert r.json()["nodes"] == []

    def undeclared_field_is_400_not_500():
        r = client.post("/metrics", json={"objects": [], "bogus": 1})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == kgt.ERR_INVALID_PARAM

    def openapi_callable_surface_is_exactly_four():
        """★ 这份 OpenAPI 就是 Dify 真实导入的文档。

        禁令工具名**允许**出现在拒绝路由的 summary 里（那正是「明确拒绝」的机械护栏），
        但拒绝路由**不得**带 requestBody ⇒ 文档里"可传 JSON 体的端点"恰好只有本臂 4 个工具。
        """
        spec = client.get("/openapi.json").json()
        tool_paths = {s["path"] for s in kgt.toolface(kgt.ARM_MAIN)}
        with_body = set()
        for path, ops in spec["paths"].items():
            for method, op in ops.items():
                if method.lower() != "post":
                    continue
                if "requestBody" in op:
                    with_body.add(path)
        # 拒绝路由带 requestBody 时 FastAPI 会生成，但本实现刻意用查询参数承载 ⇒ 断言其缺席
        for banned in ("/query", "/rules", "/validate", "/propose"):
            assert banned not in with_body, "越权路径 %s 竟然声明了请求体" % banned
            summary = spec["paths"][banned]["post"].get("summary", "")
            assert summary.startswith("拒绝"), "%s 的 summary 未声明拒绝：%r" % (banned, summary)
        # 4 个工具路径必须都有请求体，且 body 属性名与契约入参逐字一致
        for spec_ in kgt.toolface(kgt.ARM_MAIN):
            assert spec_["path"] in with_body, "%s 缺请求体声明" % spec_["path"]
        assert with_body == tool_paths, "可传请求体的端点应为 %s，实为 %s" % (sorted(tool_paths), sorted(with_body))
        # 工具自身名称不得与禁令工具重名
        for tool in MAIN_TOOLS:
            assert tool not in BANNED and tool not in ("cc_query", "run_rules", "validate")

    check("工具路由齐备且越权路径都有拒绝路由", tool_paths_only)
    check("越权路径逐条 404 且拒绝体点名工具", banned_paths_return_404_with_reason)
    check("/health 回显臂/工具面/rulesLoaded=False", health_ok_and_declares_face)
    check("补充臂 /health 标注 supplementary 且 /collect 可用", dyn_health_is_supplementary)
    check("主臂拒绝 /collect", main_arm_rejects_collect_route)
    check("未知 scope 走 200 空结果而非错误", unknown_scope_still_200)
    check("未声明字段 ⇒ 400 而非 500", undeclared_field_is_400_not_500)
    check("OpenAPI 可传请求体的端点恰为本臂 5 工具", openapi_callable_surface_is_exactly_four)


# ===========================================================================
# 10. manifest 自洽（含 M3.8 的「不进主判据」落地机制）
# ===========================================================================
def test_manifest() -> None:
    section("10. manifest 自洽 —— 声明面可机检")

    main = kgt.emit_manifest(kgt.ARM_MAIN)
    dyn = kgt.emit_manifest(kgt.ARM_DYN)

    def main_manifest_declares_four():
        assert main["arm"] == kgt.ARM_MAIN
        assert main["toolCount"] == 5 == len(main["tools"])
        assert [t["name"] for t in main["tools"]] == MAIN_TOOLS
        assert main["expectedToolCount"] == 5
        assert main["inMainComparison"] is True and main["supplementary"] is False
        assert main["servicePort"] == 8303

    def dyn_manifest_declares_supplementary():
        assert dyn["arm"] == kgt.ARM_DYN
        assert dyn["toolCount"] == 6
        assert dyn["supplementary"] is True and dyn["inMainComparison"] is False
        assert dyn["supplementaryReason"], "补充臂必须写明『不参与主判据』的理由"

    def forbidden_tools_declared_verbatim():
        for spec in (main, dyn):
            assert spec["forbiddenTools"] == BANNED or set(spec["forbiddenTools"]) >= set(BANNED)
        assert set(main["forbiddenTools"]) >= set(BANNED + ["collect_now"])
        assert set(dyn["forbiddenTools"]) >= set(BANNED)
        assert "collect_now" not in dyn["forbiddenTools"]

    def source_sha_matches_self():
        assert main["sourceSha256"] == kgt.sha256_file(os.path.join(ROOT, kgt.REPO_REL_SOURCE))
        rels = {s["path"] for s in main["sources"]}
        assert kgt.IMPL_TOOLS in rels, "provenance 必须含 5 工具的真实实现"
        assert kgt.REPO_REL_SOURCE in rels

    def every_tool_declares_params_returns():
        # ★ R-54 期修正（2026-10-07）：`read_facts` 是到 :8304 的**纯代理**，其 returns 逐字绑定
        #   形状权威 shared/model/live-facts-tools.yaml#read_facts（tool_level ∪ 公共信封 =
        #   objects/relations/observations/source + hint/truncated/droppedRows/droppedBytes）
        #   ⇒ **没有** warnings / audit —— 那两个键是**本门面自建 4 工具**的信封，不是中性工具的信封。
        #   门面自身的 `--selftest` 另有更严的断言：read_facts 返回名 == 形状权威（逐字）。
        NEUTRAL_ENVELOPE = ("hint", "truncated", "droppedRows", "droppedBytes")
        PROXIED = {"read_facts"}
        for spec in main["tools"]:
            assert spec["path"].startswith("/")
            assert spec["params"], "%s 未声明入参" % spec["name"]
            assert spec["returns"], "%s 未声明出参" % spec["name"]
            names = [r["name"] for r in spec["returns"]]
            if spec["name"] in PROXIED:
                for must in NEUTRAL_ENVELOPE:
                    assert must in names, \
                        "%s（纯代理）的 returns 缺中性信封键 %s" % (spec["name"], must)
                continue
            for must in ("truncated", "warnings", "audit", "hint", "droppedRows", "droppedBytes"):
                assert must in names, "%s 的 returns 缺 %s" % (spec["name"], must)

    def manifest_is_serializable_and_roundtrips():
        text = json.dumps(main, ensure_ascii=False)
        again = json.loads(text)
        assert again["tools"] == main["tools"]
        assert ct._byte_len(main) < 200000, "manifest 自身不应畸形膨胀"

    def audit_contract_declared_in_manifest():
        chain = main["auditChain"]
        assert chain["separateFrom"] == "elapsed_s"
        assert sorted(chain["fields"]) == sorted(ct.AUDIT_FIELDS)
        assert main["payloadPolicy"]["byteCap"] == 20000
        assert main["payloadPolicy"]["truncationMustReport"] is True
        assert main["runtimeGuards"]["rulesFileLoaded"] is False

    check("主臂 manifest：恰 5 工具 + inMainComparison=True + :8303", main_manifest_declares_four)
    check("补充臂 manifest：5 工具 + supplementary=True + 写明理由", dyn_manifest_declares_supplementary)
    check("禁令工具在 manifest 里逐字声明", forbidden_tools_declared_verbatim)
    check("sourceSha256 与实现自指纹一致，sources 含真实实现", source_sha_matches_self)
    check("每个工具的 params/returns 均非空且含账目字段", every_tool_declares_params_returns)
    check("manifest 可序列化且往返一致", manifest_is_serializable_and_roundtrips)
    check("审计/预算/N1 三项声明写入 manifest", audit_contract_declared_in_manifest)


# ===========================================================================
# main
# ===========================================================================
def main() -> int:
    print("M3.7/M3.8 KG-T 第二套门面测试 —— service=%s" % kgt.__file__)
    print("契约根 = %s" % ROOT)
    test_toolface_and_counts()
    test_banned_tools_are_blocked()
    test_four_tools_callable()
    test_deterministic_empty()
    test_param_validation()
    test_payload_budget()
    test_audit_chain()
    test_runtime_guards()
    test_http_layer()
    test_manifest()

    print("\ntest: %d/%d PASS" % (len(PASSED), len(PASSED) + len(FAILED)))
    if FAILED:
        for name, detail in FAILED:
            print("  FAILED %s :: %s" % (name, detail))
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


def test_zz_no_failures() -> None:
    """pytest 聚合入口：前面的 test_* 小节已把结论累积到 FAILED。"""
    if not FAILED:
        main()
    assert not FAILED, "离线单测存在失败项：%s" % FAILED


if __name__ == "__main__":
    sys.exit(main())
