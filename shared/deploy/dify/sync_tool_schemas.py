#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.2 / W1 · 独立第三方比对器：Dify live 回读 ↔ binding.yaml + 模型文件声明。

与 `register_tools.py` 的独立性（这是本文件存在的全部理由）
---------------------------------------------------------
* 本文件 **不 import register_tools**：声明侧的投影与比对全部另写一遍。
* 比对面改用**扁平元组**（`(name, type, required, description)` 的有序元组），
  而不是注册器用的「嵌套 dict 归一化相等」—— 注册器把 schema 写错又自证正确时，
  这里的口径不同，才有机会抓到。
* 额外校验注册器的 leg A / leg B **都不覆盖**的独立不变量：
  `tools[].server_url == providers[].base + path`，专门抓
  「provider → server_url → 端口」映射错位（任务书 R-31 新增的静默失效防线）。
* 只读：本文件**没有任何写路径**（不调用 add / update / delete / 插件接口）。

用法
----
    python sync_tool_schemas.py --check        # 真机只读比对；漂移即非零退出
    python sync_tool_schemas.py --selftest     # 离线自检（不需要 Dify、不需要网络）
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common as C  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BINDING = HERE / "binding.yaml"
DOC_FIELD_PROBE = "carrier_specific_value_semantics"


# ===========================================================================
# 1. 声明侧投影（独立实现，扁平元组口径）
# ===========================================================================
def _alias(binding) -> dict:
    return dict(binding["registration_face"]["type_alias"] or {})


def _resolve_ref(entry) -> dict:
    """★ S3a：独立重实现 D47 的 `neutral_tool_ref` 解析（**不 import register_tools**）。

    引用条目的正文（params / returns / transport）不在登记文件里复制，权威在
    `shared/model/<fname>#<顶层键>`。本投影照同一条 resolution_contract 自行解析：
    取被引用形状的深拷贝，只把对外 `name` 换成登记条目的名字（别名只换名、
    形状逐字同源）。无引用 ⇒ 原样返回（与注册器口径一致）。
    """
    ref = entry.get("neutral_tool_ref")
    if not ref:
        return entry
    if "#" not in str(ref):
        raise C.DifyError("引用格式非法（应 <文件名>#<顶层键>）：%r" % ref)
    fname, key = str(ref).split("#", 1)
    src = C.load_binding(str(C.REPO_ROOT / "shared" / "model" / fname))
    if key not in src:
        raise C.DifyError("引用指向的顶层键不存在：%s#%s" % (fname, key))
    body = copy.deepcopy(src[key])
    name = entry.get("name") or entry.get("tool_name")
    if name:
        body["name"] = name
    return body


def _transport(entry, body) -> tuple:
    """条目自带 path/method 优先，否则取形状的 transport（与注册器 entry_transport 同口径）。"""
    tr = body.get("transport") or {}
    path = entry.get("path") or tr.get("path")
    method = entry.get("method") or tr.get("method")
    return path, str(method or "").lower()


def _params_of(binding, body) -> tuple:
    """三种形态 → 同一扁平元组口径：① params 是 mapping + 顶层 required（形状权威）

    ② params 是 list（setb，逐项自带 required） ③ params 缺席、参数在 request.properties。
    """
    a = _alias(binding)
    raw = body.get("params")
    if isinstance(raw, dict):
        req = body.get("required") or []
        return tuple(sorted(
            (pn, a.get(pd.get("type"), pd.get("type")), pn in req, pd.get("description") or "")
            for pn, pd in raw.items()))
    if isinstance(raw, list):
        return tuple(sorted(
            (p["name"], a.get(p.get("type"), p.get("type")), bool(p.get("required")),
             p.get("description") or "")
            for p in raw))
    req_src = body.get("request") or {}
    props = req_src.get("properties") or {}
    req = req_src.get("required") or []
    return tuple(sorted(
        (k, a.get(v.get("type"), v.get("type")), k in req, v.get("description") or "")
        for k, v in props.items()))


def expected_setb(binding, src) -> dict:
    out = {}
    for name, tool in (src.get("tools") or {}).items():
        body = _resolve_ref(tool)
        path, method = _transport(tool, body)
        # ★ FIX-1：真零参工具**不注入**占位符（params 保持 []，与注册器口径一致）
        out[name] = {
            "tool_name": name,
            "path": path,
            "method": method,
            "description": body.get("description") or "",
            "params": _params_of(binding, body),
        }
    return out


def expected_kgt(binding, src, names) -> dict:
    by_name = {t["tool_name"]: t for t in (src.get("tools") or [])}
    out = {}
    for n in names:
        t = by_name[n]
        body = _resolve_ref(t)
        path, method = _transport(t, body)
        # ★ kgt 的模型面描述取 summary（缺则回落到被引用形状的 description）
        desc = t.get("summary") or body.get("description") or ""
        out[n] = {
            "tool_name": n,
            "path": path,
            "method": method,
            "description": desc,
            "params": _params_of(binding, body),
        }
    return out


def expected_livefacts(binding, src) -> dict:
    """形状权威（`shared/model/live-facts-tools.yaml`）→ 注册面投影（独立实现）。

    ★ S3a 新增：该 provider 的形状源是**第三种形态**——顶层键即定义体
    （`read_facts`），别名另用 `aliases.<名>.of` 指回同一份定义体。
    主名与别名逐字同源，只换对外名（与注册器 `spec_from_shape` 的口径一致：
    别名先合成引用条目再走同一解析器，故 path / method / description / params 全取自
    被引用定义体）。`required` 是定义体顶层单列，不是逐参数自带。
    """
    a = _alias(binding)
    names = src.get("names") or {}
    primary = names.get("primary")
    bodies = []
    if primary and isinstance(src.get(primary), dict):
        bodies.append((primary, src[primary]))
    for alias, spec in (src.get("aliases") or {}).items():
        of = (spec or {}).get("of")
        body = src.get(of) if of else None
        if not isinstance(body, dict):
            raise C.DifyError("别名 %s 的 of=%r 在形状权威里没有对应顶层键" % (alias, of))
        bodies.append((alias, body))
    out = {}
    for name, body in bodies:
        path, method = _transport({}, body)
        out[name] = {
            "tool_name": name,
            "path": path,
            "method": method,
            "description": body.get("description") or "",
            "params": _params_of(binding, body),
        }
    return out


def expected_for(binding, srcs, provider) -> dict:
    if provider["tools_from"] == "setb":
        allspec = expected_setb(binding, srcs["setb"])
    elif provider["tools_from"] in ("kgt", "kgt-dyn"):
        # ★ U-N 裁定 = A：kgt-dyn 与 kgt 共用同一投影，工具子集由 provider["tools"] 决定
        allspec = expected_kgt(binding, srcs["kgt"], provider["tools"])
    elif provider["tools_from"] == "livefacts":
        allspec = expected_livefacts(binding, srcs["livefacts"])
    elif provider["tools_from"] == "neutral":
        # ★ R-55：BASE 的**载体中性**数据面（cc-base-tools，:8304）。与 livefacts 共用同一份
        #   形状权威；`expected_livefacts` 同时给出主名与免费臂别名，再由下一行的
        #   `provider["tools"]`（=[read_facts]）筛出本 provider 实际暴露的名字。
        allspec = expected_livefacts(binding, srcs["livefacts"])
    else:
        raise C.DifyError("未知 tools_from：%s" % provider["tools_from"])
    return {n: allspec[n] for n in provider["tools"]}


def api_providers(binding) -> list:
    return [p for p in binding["providers"] if p.get("kind") == "api"]


# ===========================================================================
# 2. live 抓取（只读）
# ===========================================================================
def fetch_live(binding, console) -> dict:
    live = {}
    for p in api_providers(binding):
        name = p["provider"]
        entity = console.get_provider(name)
        tools = {}
        for t in entity.get("tools") or []:
            op = t.get("openapi") or {}
            params = tuple(sorted((q.get("name"), q.get("type"), bool(q.get("required")),
                                   q.get("llm_description") or "")
                                  for q in (t.get("parameters") or [])))
            tools[t.get("operation_id")] = {
                "tool_name": t.get("operation_id"),
                "server_url": t.get("server_url"),
                "method": t.get("method"),
                "description": op.get("description") or "",
                "params": params,
            }
        live[name] = {
            "tools": tools,
            "schema_text": entity.get("schema") if isinstance(entity.get("schema"), str)
            else json.dumps(entity.get("schema"), ensure_ascii=False),
        }
    return live


def _path_of(provider, entry) -> str:
    url = entry.get("server_url") or ""
    base = provider["base"]
    return url[len(base):] if url.startswith(base) else url


# ===========================================================================
# 3. 比对（判据写死在这里）
# ===========================================================================
def compare(binding, srcs, live) -> tuple:
    problems, notes = [], []
    wl = binding["whitelist"]
    denied_scope = set(wl.get("denied_scope") or [])
    tuples_by_provider = {}

    for p in api_providers(binding):
        name = p["provider"]
        want = expected_for(binding, srcs, p)
        if name not in live:
            problems.append("S1 %s：live 回读里不存在该 provider" % name)
            continue
        got = live[name]["tools"]
        want_names, got_names = set(want), set(got)
        if want_names != got_names:
            problems.append("S2 %s：注册面工具集合 %s != 声明 %s"
                            % (name, sorted(got_names), sorted(want_names)))
        # S4：provider → server_url → 端口 映射（注册器的 leg A/B 都不查这条）
        for tn, entry in sorted(got.items()):
            url = entry.get("server_url") or ""
            expect_url = p["base"] + want[tn]["path"] if tn in want else None
            if expect_url is not None and url != expect_url:
                problems.append("S4 %s/%s：server_url=%s 期望 %s（base+path，端口映射错位）"
                                % (name, tn, url, expect_url))
        for tn in sorted(want_names & got_names):
            a, b = want[tn], got[tn]
            for k in ("method", "description", "params"):
                if a[k] != b[k]:
                    problems.append("S3 %s/%s：%s 不一致\n      declared=%s\n      live    =%s"
                                    % (name, tn, k, json.dumps(a[k], ensure_ascii=False, default=str),
                                       json.dumps(b[k], ensure_ascii=False, default=str)))
            if _path_of(p, b) != a["path"]:
                problems.append("S3 %s/%s：path %s != %s" % (name, tn, _path_of(p, b), a["path"]))
            if b["tool_name"] != tn:
                problems.append("S3 %s/%s：operation_id 与工具名不一致" % (name, tn))
        # S5：live 存回的 schema 文本里不得出现文档性字段名
        text = live[name]["schema_text"] or ""
        if DOC_FIELD_PROBE in text:
            problems.append("S5 %s：注册面残留文档字段 %s" % (name, DOC_FIELD_PROBE))
        # S6：denied 名（只在 denied_scope 内）
        if name in denied_scope:
            for bad in list(wl["denied_tool_names"]) + list(wl["denied_operation_ids"]):
                if bad in got_names:
                    problems.append("S6 %s：注册面出现禁用工具名 %s" % (name, bad))
        tuples_by_provider[name] = {tn: (e["method"], e["description"], e["params"])
                                    for tn, e in got.items()}
        ok_url = sum(1 for tn, e in got.items()
                     if tn in want and e.get("server_url") == p["base"] + want[tn]["path"])
        notes.append("S-OK %-14s tools=%d server_url=%d/%d 符合 base+path"
                     % (name, len(got), ok_url, len(want)))

    # S7：逐字孪生（两侧投影除 provider 名/base 外必须完全相同）
    for p in api_providers(binding):
        twin_of = p.get("verbatim_twin_of")
        if not twin_of:
            continue
        a = tuples_by_provider.get(twin_of)
        b = tuples_by_provider.get(p["provider"])
        if a is None or b is None:
            problems.append("S7 孪生比对缺少一方：%s / %s" % (twin_of, p["provider"]))
            continue
        ha = C.sha256_bytes(C.stable_json(a).encode("utf-8"))
        hb = C.sha256_bytes(C.stable_json(b).encode("utf-8"))
        notes.append("S7 孪生 %s / %s 模型面 sha256 %s / %s 相等=%s"
                     % (twin_of, p["provider"], ha[:16], hb[:16], ha == hb))
        if ha != hb:
            problems.append("S7 两侧投影不逐字相同：%s vs %s" % (twin_of, p["provider"]))
    return problems, notes


# ===========================================================================
# 4. 动作
# ===========================================================================
def cmd_check(args) -> int:
    binding = C.load_binding(args.binding)
    srcs = {k: C.load_binding(str(C.REPO_ROOT / v["path"]))
            for k, v in binding["tool_sources"].items()}
    console = C.console_client()
    list_api = console.list_api_providers()
    print("== live API provider 总数 = %d（基线登记 %d）=="
          % (len(list_api), binding["counts"]["baseline_api_providers"]))
    for name in [p["provider"] for p in api_providers(binding)]:
        print("   %-14s 存在=%s" % (name, name in list_api))
    live = fetch_live(binding, console)
    problems, notes = compare(binding, srcs, live)
    for n in notes:
        print("note: %s" % n)
    for name, blk in live.items():
        for tn, e in sorted(blk["tools"].items()):
            print("   face %-14s %-22s %s" % (name, tn,
                                              C.sha256_bytes(C.stable_json(
                                                  [e["method"], e["description"], e["params"]]
                                              ).encode())[:32]))
    if problems:
        print("\n-- SYNC FAIL: %d 项 --" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\n-- SYNC OK: live 回读与声明零漂移 --")
    return 0


def cmd_selftest(args) -> int:
    binding = C.load_binding(args.binding)
    srcs = {k: C.load_binding(str(C.REPO_ROOT / v["path"]))
            for k, v in binding["tool_sources"].items()}

    def synth() -> dict:
        live = {}
        for p in api_providers(binding):
            want = expected_for(binding, srcs, p)
            tools = {}
            for tn, w in want.items():
                tools[tn] = {"tool_name": tn, "server_url": p["base"] + w["path"],
                             "method": w["method"], "description": w["description"],
                             "params": w["params"]}
            live[p["provider"]] = {"tools": tools, "schema_text": json.dumps(
                {"paths": {w["path"]: {"post": {"operationId": tn}}} for tn, w in want.items()},
                ensure_ascii=False)}
        return live

    cases = []

    def case(name, fn):
        cases.append((name, fn))

    def needles(live, *want, binding_=None):
        probs, _ = compare(binding_ or binding, srcs, live)
        text = "\n".join(probs)
        if not probs:
            raise AssertionError("未捕获任何漂移")
        for w in want:
            if w not in text:
                raise AssertionError("漂移原因里没有 %r：\n%s" % (w, text))
        return True

    case("T01 声明侧：setb 8 个工具 / kgt 主组 5 个工具 / livefacts 主名+别名 2 个", lambda: (
        len(expected_setb(binding, srcs["setb"])) == 8
        and len(expected_kgt(binding, srcs["kgt"],
                             [p for p in api_providers(binding) if p["tools_from"] == "kgt"][0]["tools"])) == 5
        and sorted(expected_livefacts(binding, srcs["livefacts"])) == ["read_facts", "read_live_facts"]
        and all(n in expected_livefacts(binding, srcs["livefacts"])
                for n in [p for p in api_providers(binding)
                          if p["tools_from"] == "livefacts"][0]["tools"])
    ) or (_ for _ in ()).throw(AssertionError("工具数/别名集合不符")))
    case("T02 合成 live 与声明一致 ⇒ 零漂移", lambda: (
        compare(binding, srcs, synth())[0] == [] or (_ for _ in ()).throw(
            AssertionError("自洽 fixture 竟然有漂移：%s" % compare(binding, srcs, synth())[0]))))
    case("T03 单侧 description 被改 ⇒ 捕获 S3", lambda: needles(
        _t(synth(), "cc-kg-tools", "cc_cause", "description", "被篡改"), "S3 cc-kg-tools/cc_cause"))
    case("T04 server_url 端口错位（8301→8302）⇒ 捕获 S4", lambda: needles(
        _t(synth(), "cc-kg-tools", "cc_guide", "server_url", "http://192.0.2.11:8302/guide"),   # ★ R-58：主机 = 现网部署主机（真机注册态实测值）；.84 是 R-56 事故的旧主机，已彻底移除。可用 env CC_DIFY_PUBLIC_HOST 覆盖。
        "S4 cc-kg-tools/cc_guide"))
    case("T05 参数 llm_description 被改 ⇒ 捕获 S3 params", lambda: needles(
        _t(synth(), "cc-onto-tools", "cc_query", "params",
           (("query", "string", True, "被改写的参数描述"),)), "S3 cc-onto-tools/cc_query"))
    case("T06 live 缺 provider ⇒ 捕获 S1", lambda: needles(
        _drop(synth(), "cc-kgt-tools"), "S1 cc-kgt-tools"))
    case("T07 live 少一个工具 ⇒ 捕获 S2", lambda: needles(
        _drop_tool(synth(), "cc-kgt-tools", "get_metrics"), "S2 cc-kgt-tools"))
    case("T08 live schema 文本残留文档字段 ⇒ 捕获 S5", lambda: needles(
        _t(synth(), "cc-kg-tools", "__schema_text__", "schema_text",
           '{"x": {"carrier_specific_value_semantics": {"kg": "Cypher"}}}'),
        "S5 cc-kg-tools"))
    case("T09 kgt 注册面出现 cc_query ⇒ 捕获 S6", lambda: needles(
        _add_tool(synth(), "cc-kgt-tools", "cc_query", "/query"), "S6 cc-kgt-tools"))
    case("T10 两侧孪生出现真实差异 ⇒ 捕获 S7", lambda: needles(
        _t(synth(), "cc-onto-tools", "cc_cause", "description", "不一样"), "S7"))
    case("T11 免费臂别名 description 被改 ⇒ 捕获 S3", lambda: needles(
        _t(synth(), "cc-live-facts-tools", "read_live_facts", "description", "被篡改"),
        "S3 cc-live-facts-tools/read_live_facts"))
    case("T12 免费臂别名缺失 ⇒ 捕获 S2", lambda: needles(
        _drop_tool(synth(), "cc-live-facts-tools", "read_live_facts"),
        "S2 cc-live-facts-tools"))

    ok = 0
    for name, fn in cases:
        try:
            fn()
        except Exception as exc:                     # noqa: BLE001
            print("FAIL  %s  ->  %s: %s" % (name, type(exc).__name__, exc))
        else:
            ok += 1
            print("PASS  %s" % name)
    print("\nSELFTEST  total=%d  PASS=%d  FAIL=%d" % (len(cases), ok, len(cases) - ok))
    return 0 if ok == len(cases) else 1


def _t(live, provider, tool, field, value):
    o = copy.deepcopy(live)
    if field == "schema_text":
        o[provider]["schema_text"] = value
    else:
        o[provider]["tools"][tool][field] = value
    return o


def _drop(live, provider):
    o = copy.deepcopy(live)
    o.pop(provider, None)
    return o


def _drop_tool(live, provider, tool):
    o = copy.deepcopy(live)
    o[provider]["tools"].pop(tool, None)
    return o


def _add_tool(live, provider, tool, path):
    o = copy.deepcopy(live)
    o[provider]["tools"][tool] = {"tool_name": tool, "server_url": "http://192.0.2.11:8303" + path,   # ★ R-58：主机 = 现网部署主机（真机注册态实测值）；.84 是 R-56 事故的旧主机，已彻底移除。可用 env CC_DIFY_PUBLIC_HOST 覆盖。
                                  "method": "post", "description": "不该出现",
                                  "params": (("query", "string", True, "只读知识查询语句"),)}
    return o


# ===========================================================================
# 5. CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="独立第三方比对：Dify live 回读 vs 声明（只读）")
    ap.add_argument("--binding", default=str(DEFAULT_BINDING))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="真机只读比对（默认）")
    g.add_argument("--selftest", action="store_true", help="离线自检")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.selftest:
            return cmd_selftest(args)
        return cmd_check(args)
    except C.DifyError as exc:
        print("SYNC ERROR: %s" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
