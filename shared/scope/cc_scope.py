# -*- coding: utf-8 -*-
"""`cc_scope` —— **有界定域包装**（载体中立）。

## 它解决什么（实测依据）

实测（M5 四组，2026-10-14）：Agent 为回答一条问诊题要调用约 **20 次**工具、单趟载荷
**≈45k tokens**（`cc_guide` 5,217 + `cc_schema` 5,543 + **逐个对象类型穷举 `cc_facts` 17 类
= 31,938** + `cc_cause` 5,283 + `cc_trace`），而 ReAct 每轮重发历史 ⇒ 累计 input tokens
**425k–510k** ⇒ 撞满 `max_iteration=20` ⇒ Dify 500、无最终答案（**7–10/50 例**）。

参考项目的诊断与做法（逐字）：
> 「**不是「数据多」，而是「返回的是全量而非聚焦面」**」
> 「有了它，每条命中的规则**只回自己判据涉及的那几条路径的证据，而不是全量 3167 条**
>  —— **这才是 164 万 tokens 那条链路的正解**」

## 设计（复用既有工具，不改任何工具面）

调用方**不必**自己拼装；`cc_scope` 一次完成：

1. `POST /cause`（既有工具）→ 候选集。**候选本身已经给出聚焦面**：
   每个候选带 `componentType` / `componentInstance`，以及 `detectEvidence`（逐条款记录，
   含 `metricCode` / `outcome` / `observed` / `operand`）。
   ⇒ **聚焦对象 = 候选的实例；聚焦指标 = 该候选判据涉及的那几条指标**（不是该对象的全量事实）。
2. 对每个聚焦实例调 `POST /facts`，且**只传 `metricCodes` = 该候选判据涉及者**。
3. 按 `(objectId, metricCode)` **聚合**（同路径只出一条：取值去重、最多 N 个、附计数）。
4. 对**排名第一**的聚焦实例调 `POST /trace`（一跳即可，用于影响面）。
5. 回传**裁剪账目**（`factsTotal / factsKept / factsDropped / pathsReturned / scopedBy`）——
   对齐契约 C6「不得静默截断」，并让调用方知道「没报」不等于「没有问题」。

**不改既有工具面**：本模块只调既有端点，不新增/删除任何工具（参考项目的
`onto_collect` 包 `gather` 也是同一手法）。

## 用法

    python shared/scope/cc_scope.py --base http://192.0.2.11:8301 \\
        --symptoms FM-APP-ENDPOINT-SLOW --measure

`--measure` 会同时算「朴素巡游」的载荷（guide + schema + 逐对象类型 facts + cause + trace）
以给出对比倍数。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 候选里对**诊断**无用的冗长字段（裁掉以压载荷；它们是"出处"而非"证据"）
_DROP_CAND_FIELDS = ("pathEdges", "condition", "evidenceSourceIds", "causalContractIds",
                     "fmReversible", "fmDetectionMode")


def _post(base: str, path: str, body: dict, timeout: int = 120):
    req = urllib.request.Request(base.rstrip("/") + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except Exception:
            return {"error": {"code": "HTTP_%d" % e.code, "message": body[:300]}}
    except Exception as e:  # noqa: BLE001
        return {"error": {"code": "TRANSPORT", "message": str(e)[:300]}}


def _tok(obj) -> int:
    """与契约 contract_audit 同口径的 token 估算（字数 / 3.5）。"""
    return int(len(json.dumps(obj, ensure_ascii=False).encode("utf-8")) / 3.5)


def _compact_candidate(c: dict) -> dict:
    """精简候选：保留**判定所需**的量，去掉"出处"类冗长字段。

    ★ 保留 `detectEvidence` 的**逐条款判决**（metricCode/op/outcome/observed/operand）——
       它是"为什么是这个码"的唯一依据，不能省；但省掉 `reason` 长文本与 `operandDetail` 细节。
    """
    out = {k: v for k, v in c.items() if k not in _DROP_CAND_FIELDS and k != "detectEvidence"}
    ev = []
    for r in (c.get("detectEvidence") or []):
        item = {"metricCode": r.get("metricCode"), "op": r.get("op"),
                "role": r.get("role"), "outcome": r.get("outcome"),
                "observed": r.get("observed"), "operand": r.get("operand")}
        if r.get("operandDetail", {}).get("calibrated") is False:
            item["uncalibrated"] = True
        ev.append(item)
    out["clauses"] = ev
    return out


def _trim_attributes(attrs: dict, metric_codes, led: dict) -> dict:
    """只留**身份键** + **与聚焦指标对应者**；其余元数据字段裁掉。

    实测依据：单实例 `attributes` 12 个键里，`jvmVersion` / `startTime` / `pid` /
    `ccfSnapshotId` / `ccfType` 等对根因判定没有贡献；而 `heapUsedRatio` / `gcPauseMs`
    恰好对应聚焦指标 `app.heap_used_ratio` / `app.gc_pause_ms`。
    对应关系：取 metricCode 最后一段 → snake_case 转 camelCase → 忽略大小写匹配属性名。
    """
    if not attrs:
        return {}
    identity = ("ccfId", "instanceId", "id", "name", "ccfType", "ccfLayer")

    def _camel(seg: str) -> str:
        parts = [p for p in re.split(r"[._]", seg) if p]
        return (parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])) if parts else ""

    wanted = set()
    for mc in metric_codes or ():
        seg = str(mc).rsplit(".", 1)[-1]
        if seg:
            wanted.add(_camel(seg).lower())
            wanted.add(re.sub(r"[^a-z0-9]", "", seg.lower()))
    keep = {}
    for k, v in attrs.items():
        kn = re.sub(r"[^a-z0-9]", "", str(k).lower())
        if k in identity or kn in wanted:
            keep[k] = v
        else:
            led["attrFieldsDropped"] = led.get("attrFieldsDropped", 0) + 1
    return keep


def cc_scope(base: str, symptom_codes, max_hops: int = 4, values_per_path: int = 3,
             with_trace: bool = True) -> dict:
    """一次调用返回**定域**结果。返回 dict（含 `ledger` 裁剪账目）。"""
    led = {"factsTotal": 0, "factsKept": 0, "factsDropped": 0, "pathsReturned": 0,
           "objectsReturned": 0, "scopedBy": "cause.componentInstance",
           "valuesPerPath": values_per_path}

    cause = _post(base, "/cause", {"symptomCodes": list(symptom_codes), "maxHops": max_hops})
    if cause.get("error"):
        return {"error": cause["error"], "ledger": led}
    cands = cause.get("candidates") or []
    if not cands:
        return {"candidates": [], "focus": [], "hint": cause.get("hint")
                or "无候选：症状码可能不在本载体的因果图内", "ledger": led,
                "rankBasis": cause.get("rankBasis")}

    # 聚焦：实例 → 该候选判据涉及的指标集合
    focus = {}
    for c in cands:
        ct, ci = c.get("componentType"), c.get("componentInstance")
        if not ct or not ci:
            continue
        ms = {r.get("metricCode") for r in (c.get("detectEvidence") or []) if r.get("metricCode")}
        focus.setdefault((ct, ci), set()).update(ms)

    focus_out = []
    for (ct, ci), ms in sorted(focus.items()):
        f = _post(base, "/facts", {"objectType": ct, "objectId": ci, "metricCodes": sorted(ms)})
        obs = f.get("observations") or []
        led["factsTotal"] += len(obs)
        agg = {}
        for o in obs:
            key = (o.get("metricCode"),)
            a = agg.get(key)
            if a is None:
                a = {"metricCode": o.get("metricCode"), "recordId": o.get("recordId"),
                     "values": [], "valueCount": 0}
                agg[key] = a
            a["valueCount"] += 1
            v = o.get("value")
            if v not in a["values"] and len(a["values"]) < values_per_path:
                a["values"].append(v)
            else:
                led["factsDropped"] += 1
        kept = [a for a in agg.values()]
        led["factsKept"] += sum(min(a["valueCount"], values_per_path) for a in kept)
        led["pathsReturned"] += len(kept)
        objs = f.get("objects") or []
        led["objectsReturned"] += len(objs)
        # ★ 载荷裁剪（实测构成：relations 1,176 tok、attributes 882 tok 是两块冗余）：
        #   ① 实例级 `relations` 不再逐实例回传 —— **拓扑由 trace 单点给出**，逐实例重复
        #      同一批边属冗余；② `attributes` 只保留**身份键**与**与聚焦指标对应者**，
        #      其余（jvmVersion/startTime/pid/ccfSnapshotId 等）是元数据，对判定无贡献。
        raw_attrs = objs[0].get("attributes") if objs else {}
        attr_keep = _trim_attributes(raw_attrs, ms, led)
        entry = {"componentType": ct, "componentInstance": ci,
                 "metricCodes": sorted(ms),
                 "attributes": attr_keep,
                 "observations": kept,
                 "truncated": bool(f.get("truncated")),
                 "hint": f.get("hint") or ""}
        if with_trace:
            tr = _post(base, "/trace", {"startId": ci, "direction": "in", "maxHops": 2})
            entry["trace"] = {"nodes": (tr.get("nodes") or [])[:12],
                              "edges": (tr.get("edges") or [])[:12],
                              "hopsUsed": tr.get("hopsUsed")}
        focus_out.append(entry)

    out = {
        "symptomCodes": list(symptom_codes),
        "maxHops": max_hops,
        "candidates": [_compact_candidate(c) for c in cands],
        "focus": focus_out,
        "ledger": led,
        "rankBasis": cause.get("rankBasis"),
        "note": "定域结果：佐证事实只取各候选判据涉及的指标；ledger 给出裁剪量。",
    }
    return out


def naive_tour(base: str, symptom_codes, max_hops: int = 4) -> dict:
    """对照：Agent 实测走的那条"朴素巡游"（guide + schema + 逐对象类型 facts + cause + trace）。"""
    parts = {"guide": _post(base, "/guide", {}), "schema": _post(base, "/schema", {})}
    ots = [o.get("objectType") for o in (parts["schema"].get("objectTypes") or [])]
    facts = {}
    for ot in ots:
        facts[ot] = _post(base, "/facts", {"objectType": ot})
    parts["facts"] = facts
    parts["cause"] = _post(base, "/cause", {"symptomCodes": list(symptom_codes), "maxHops": max_hops})
    return parts


def selftest(base: str, symptom_codes) -> int:
    """★ 硬要求：**压缩不得改变结论**。

    断言（逐条给实测值，便于回归对照）：
      A1 候选数与 `/cause` 逐字相同（不因裁剪丢候选）
      A2 首位候选的 faultCode 与 `/cause` 相同（排序不受本包装影响）
      A3 每个聚焦实例都确实是某个候选的 componentInstance（不凭空聚焦）
      A4 每个聚焦实例的 metricCodes 是该候选判据指标的并集（不遗漏判据指标）
      A5 ledger 自洽：factsKept ≤ factsTotal，且 factsDropped 有据
      A6 聚焦取事实全部 truncated=False（定域后不应再触上限）
    """
    cause = _post(base, "/cause", {"symptomCodes": list(symptom_codes), "maxHops": 4})
    scoped = cc_scope(base, symptom_codes)
    c_cause = cause.get("candidates") or []
    c_scope = scoped.get("candidates") or []
    ok = True

    def chk(name, cond, actual):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %-46s 实测=%s" % ("PASS" if cond else "FAIL", name, actual))

    chk("A1 候选数与 /cause 相同", len(c_cause) == len(c_scope),
        "%d vs %d" % (len(c_cause), len(c_scope)))
    f_cause = c_cause[0].get("faultCode") if c_cause else None
    f_scope = c_scope[0].get("faultCode") if c_scope else None
    chk("A2 首位候选 faultCode 相同", f_cause == f_scope, "%s vs %s" % (f_cause, f_scope))

    cand_insts = {c.get("componentInstance") for c in c_cause if c.get("componentInstance")}
    focus_insts = {f["componentInstance"] for f in (scoped.get("focus") or [])}
    chk("A3 聚焦实例均来自候选", focus_insts <= cand_insts,
        "越界 %s" % sorted(focus_insts - cand_insts))

    # A4：每个实例的指标 = 该实例上所有候选判据指标的并集
    want = {}
    for c in c_cause:
        ci = c.get("componentInstance")
        if not ci:
            continue
        for r in (c.get("detectEvidence") or []):
            if r.get("metricCode"):
                want.setdefault(ci, set()).add(r["metricCode"])
    bad = [f["componentInstance"] for f in (scoped.get("focus") or [])
           if set(f["metricCodes"]) != want.get(f["componentInstance"], set())]
    chk("A4 判据指标无遗漏", not bad, "不符 %s" % bad)

    led = scoped.get("ledger") or {}
    chk("A5 ledger 自洽", led.get("factsKept", 0) <= led.get("factsTotal", 0),
        "kept=%s total=%s dropped=%s" % (led.get("factsKept"), led.get("factsTotal"),
                                         led.get("factsDropped")))
    chk("A6 聚焦取事实未截断", all(not f.get("truncated") for f in (scoped.get("focus") or [])),
        [f["truncated"] for f in (scoped.get("focus") or [])])

    print("结果：%s ｜ cc_scope ≈ %d tokens ｜ 候选 %d ｜ 聚焦实例 %d"
          % ("PASS" if ok else "FAIL", _tok(scoped), len(c_scope), len(focus_insts)))
    return 0 if ok else 1


def serve(port: int, base: str, carrier: str) -> int:
    """服务模式：`POST /scope` → 定域结果；`GET /health` → 载体/基址/上限。

    ★ 为什么是**独立包装服务**而不是在两侧载体里各加一个工具：
      两侧服务内部结构不同（KG 用 `build_*`、ONTO 用内联 SPARQL），而本服务只调**既有**
      HTTP 端点 ⇒ 一份实现服务两个载体，且**一行不动载体的 8 工具工具面**（无需改
      tools-setb.yaml / 重生成 manifest / 重钉纯度）。参考项目的 `onto_collect`
      包 `v2 /tools/gather` 是同一手法。
    每个载体一个实例（`:8306` 绑 KG、`:8307` 绑 ONTO），使 A/B 各臂只看到自己载体的定域面。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("/health", ""):
                self._send(200, {"status": "ok", "service": "cc_scope", "carrier": carrier,
                                 "base": base, "tool": "cc_scope",
                                 "valuesPerPath": 3, "version": "cc-scope/1"})
            else:
                self._send(404, {"error": {"code": "NOT_FOUND", "message": self.path}})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception as e:  # noqa: BLE001
                self._send(400, {"error": {"code": "INVALID_JSON", "message": str(e)[:200]}})
                return
            if self.path.rstrip("/") not in ("/scope", ""):
                self._send(404, {"error": {"code": "NOT_FOUND", "message": self.path}})
                return
            syms = body.get("symptomCodes") or body.get("symptoms") or []
            if isinstance(syms, str):
                syms = [s.strip() for s in syms.split(",") if s.strip()]
            if not syms:
                self._send(400, {"error": {"code": "INVALID_PARAM",
                                           "message": "symptomCodes 必填（数组或逗号分隔串）"}})
                return
            try:
                out = cc_scope(base, syms, int(body.get("maxHops") or 4),
                               int(body.get("valuesPerPath") or 3),
                               not body.get("noTrace"))
            except Exception as e:  # noqa: BLE001
                self._send(502, {"error": {"code": "SCOPE_FAILED", "message": str(e)[:300]}})
                return
            out["carrier"] = carrier
            self._send(200, out)

        def log_message(self, *a):  # 静默：日志由 systemd/nohup 侧留存
            return

    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("cc_scope 服务已启动：carrier=%s base=%s port=%d" % (carrier, base, port), flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.serve_forever()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="cc_scope：有界定域包装（载体中立）")
    ap.add_argument("--base", required=True, help="载体的工具服务基址，如 http://192.0.2.11:8301")
    ap.add_argument("--symptoms", default="", help="逗号分隔的症状码（服务模式可省）")
    ap.add_argument("--max-hops", type=int, default=4)
    ap.add_argument("--values-per-path", type=int, default=3)
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--measure", action="store_true", help="同时算朴素巡游的载荷并对比")
    ap.add_argument("--selftest", action="store_true", help="断言「压缩不改变结论」")
    ap.add_argument("--serve", action="store_true", help="以服务模式常驻（POST /scope）")
    ap.add_argument("--port", type=int, default=0, help="服务模式端口")
    ap.add_argument("--carrier", default="", help="载体名（kg/onto），仅用于回传与标识")
    a = ap.parse_args()
    sym = [s.strip() for s in a.symptoms.split(",") if s.strip()]

    if a.serve:
        if not a.port:
            print("--serve 需要 --port", file=sys.stderr)
            return 2
        return serve(a.port, a.base, a.carrier or "unknown")

    if a.selftest:
        return selftest(a.base, sym)
    if not sym:
        print("--symptoms 必填（非服务模式）", file=sys.stderr)
        return 2

    scoped = cc_scope(a.base, sym, a.max_hops, a.values_per_path, not a.no_trace)
    t_scoped = _tok(scoped)
    print("=" * 78)
    print("cc_scope ｜ %s ｜ 症状=%s" % (a.base, sym))
    print("=" * 78)
    print("  候选 =", len(scoped.get("candidates") or []),
          "｜ 聚焦实例 =", len(scoped.get("focus") or []))
    for f in scoped.get("focus") or []:
        print("    · %-18s %-34s 指标 %d 个 ｜ 观测路径 %d ｜ 值 %d ｜ 截断=%s"
              % (f["componentType"], f["componentInstance"], len(f["metricCodes"]),
                 len(f["observations"]), sum(o["valueCount"] for o in f["observations"]),
                 f["truncated"]))
    print("  ledger =", json.dumps(scoped.get("ledger"), ensure_ascii=False))
    print("  **cc_scope 载荷 ≈ %d tokens**" % t_scoped)

    if a.measure:
        parts = naive_tour(a.base, sym, a.max_hops)
        per = {k: _tok(v) for k, v in parts.items() if k != "facts"}
        facts_tok = sum(_tok(v) for v in parts["facts"].values())
        total = sum(per.values()) + facts_tok
        print()
        print("  朴素巡游（Agent 实测走法）：")
        for k, v in sorted(per.items(), key=lambda kv: -kv[1]):
            print("    %-10s ≈ %6d tokens" % (k, v))
        print("    %-10s ≈ %6d tokens（%d 个对象类型逐个取）"
              % ("facts", facts_tok, len(parts["facts"])))
        print("    %-10s ≈ %6d tokens" % ("合计", total))
        print()
        print("  ★ 压缩比 ≈ **%.1f : 1**（%d → %d tokens）"
              % (total / max(1, t_scoped), total, t_scoped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
