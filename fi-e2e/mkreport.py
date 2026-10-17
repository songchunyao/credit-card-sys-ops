# -*- coding: utf-8 -*-
"""fi-e2e/mkreport.py —— 把一次匹配性测试批次的产物转成可读报告（Markdown）。

输入：fi-e2e/out/<runid>/results.jsonl（fi.py suite 的产物）
输出：fi-e2e/out/<runid>/REPORT.md

只做「如实汇总」，不引入任何新判据：所有数字都能回指到 case.json / ccf.json / detect.json。
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.path.join(HERE, "out")

VERDICT_DOC = {
    "HIT": "期望根因码在候选内",
    "HIT_PARTIAL": "判据层检出期望码，但 /cause 未排进候选",
    "HIT_SYMPTOM_ONLY": "只命中症状级码（合法，但不是根因命中）",
    "MISS": "可判、故障确已生效，但给出的根因不对",
    "NO_EFFECT": "签名指标无变化 ⇒ 故障未产生，不记 MISS",
    "UNJUDGEABLE": "期望码的判据指标本次快照无观测 ⇒ 按契约不可判",
    "BLOCKED": "没测到（注入未生效/冻结/投影/服务失败）",
    "NOT_APPLICABLE": "该注入在本环境是 no-op",
    "BASELINE_CLEAN": "阴性对照：健康态无增量信号",
    "BASELINE_SIGNAL": "阴性对照：健康态已有增量信号（噪声）",
}


def load(runid):
    d = os.path.join(OUTROOT, runid)
    if not os.path.isdir(d):
        raise SystemExit("无此批次：%s" % d)
    recs = []
    p = os.path.join(d, "results.jsonl")
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    else:  # 回退：扫目录
        for name in sorted(os.listdir(d)):
            cj = os.path.join(d, name, "case.json")
            if os.path.exists(cj):
                recs.append(json.load(io.open(cj, encoding="utf-8")))
    return d, recs


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.4g" % v)
    return str(v)


def cause_of(run_dir, case, carrier):
    p = os.path.join(run_dir, case, "cause_%s.json" % carrier)
    if not os.path.exists(p):
        return None
    return json.load(io.open(p, encoding="utf-8"))


def cand_stats(resp):
    """候选列表规模统计 —— top1 必须连同这个一起读，否则会误读成「准确」。"""
    if not resp or not resp.get("ok"):
        return None
    c = (resp.get("raw") or {}).get("candidates") or []
    det = [x for x in c if x.get("determined")]
    sat = [x for x in c if (x.get("satisfiedClauses") or 0) > 0]
    return {"n": len(c), "det": len(det), "sat": len(sat),
            "top1": c[0].get("faultCode") if c else None,
            "top1sat": c[0].get("satisfiedClauses") if c else None,
            "codes": [x.get("faultCode") for x in c]}


def metric_audit(case_dirs):
    """指标可用性体检（自动、无需人工判断）：
        ① 在某些用例中缺席 ⇒ 该判据在这些用例里必然不可判
        ② collected 但 value 为 null ⇒ 判据恒 UNK
        ③ collected 但 value 恒为负数 ⇒ 量纲/符号可疑，阈值类判据恒不成立
    """
    per, total = {}, len(case_dirs)
    for d in case_dirs:
        p = os.path.join(d, "ccf.json")
        if not os.path.exists(p):
            continue
        ccf = json.load(io.open(p, encoding="utf-8"))
        seen = {}
        for o in ccf.get("observations") or []:
            seen.setdefault(o["metricCode"], []).append(o)
        for m, lst in seen.items():
            r = per.setdefault(m, {"cases": 0, "null": 0, "neg": 0, "vals": []})
            r["cases"] += 1
            o = lst[0]
            # ★ text/enum 类指标的值在 valueText 里，value 为 null —— 必须两者都看，
            #   否则会把「正常采集到的文本指标」误报成「恒 null」。
            v = o.get("value")
            if v is None:
                v = o.get("valueText")
            if v is None:
                r["null"] += 1
            else:
                r["vals"].append(v)
                try:
                    if float(v) < 0:
                        r["neg"] += 1
                except (TypeError, ValueError):
                    pass
    out = {"absent": [], "nullValued": [], "negative": []}
    for m, r in sorted(per.items()):
        if r["cases"] < total:
            out["absent"].append({"metric": m, "presentIn": r["cases"], "of": total})
        if r["null"] and r["null"] >= max(1, r["cases"] // 2):
            out["nullValued"].append({"metric": m, "nullIn": r["null"], "presentIn": r["cases"]})
        if r["neg"] and r["neg"] >= max(1, r["cases"] // 2):
            out["negative"].append({"metric": m, "negativeIn": r["neg"],
                                    "presentIn": r["cases"], "sample": r["vals"][:3]})
    return out


def main():
    if len(sys.argv) < 2:
        subs = sorted(x for x in os.listdir(OUTROOT)) if os.path.isdir(OUTROOT) else []
        raise SystemExit("用法：python fi-e2e/mkreport.py <runid>\n现有批次：%s" % ", ".join(subs))
    runid = sys.argv[1]
    d, recs = load(runid)
    base = next((r for r in recs if r["type"] == "baseline"), None)
    cases = [r for r in recs if r["type"] != "baseline"]

    L = []
    A = L.append
    A("# 故障—根因匹配性测试报告")
    A("")
    A("> 批次：`%s`　生成：%s" % (runid, datetime.now().strftime("%Y-%m-%d %H:%M")))
    A("> 环境：`%s`（cardsim 注入台 `:8090` ＋ 采集层 `:8187` ＋ 载体 `:8301`/`:8302`）"
      % (recs[0].get("host") if recs else "—"))
    A("> 真值表：`fi-e2e/fault-map.yaml`　契约：`shared/model/fault-modes.yaml`")
    A("")

    # ── 0. 判词总览 ──────────────────────────────────────────────
    tally = {}
    for r in cases:
        tally.setdefault(r.get("verdict") or "BLOCKED", []).append(r["type"])
    A("## 0. 判词总览")
    A("")
    A("| 判词 | 数 | 用例 | 含义 |")
    A("|---|---|---|---|")
    for k in sorted(tally, key=lambda x: -len(tally[x])):
        A("| `%s` | %d | %s | %s |" % (k, len(tally[k]), ", ".join(tally[k]),
                                       VERDICT_DOC.get(k, "")))
    n = len(cases)
    hit = sum(1 for r in cases if r.get("verdict") in ("HIT", "HIT_PARTIAL"))
    A("")
    A("**根因命中率 = %d/%d = %s**（分母＝全部用例，含 UNJUDGEABLE/BLOCKED/NO_EFFECT，"
      "与 M5 判决集口径一致）" % (hit, n, ("%.0f%%" % (100.0 * hit / n)) if n else "—"))
    if base:
        A("")
        A("阴性对照背景结论集 `chronic`（%d 条）：%s" %
          (len(base.get("score", {}).get("chronic_true") or []),
           ", ".join("`%s`" % c for c in (base.get("score", {}).get("chronic_true") or [])) or "（空）"))
    A("")

    # ── 1. L0 注入有效性 ─────────────────────────────────────────
    A("## 1. L0 · 注入有效性（故障到底发生了没有）")
    A("")
    A("分离这一层，是为了让「系统判错」与「故障没产生」不混为一谈。")
    A("")
    A("| 用例 | 注入 | 有效性 | 签名指标（基线 → 注入） |")
    A("|---|---|---|---|")
    for r in cases:
        eff = (r.get("score") or {}).get("efficacy") or {}
        if not eff.get("available"):
            A("| `%s` | %s | 不可判 | %s |" % (r["type"], r.get("inject", {}).get("type", "—"),
                                              eff.get("reason", "")))
            continue
        rows = []
        for s in eff.get("signature") or []:
            rows.append("%s: %s→%s%s" % (s["metric"], fmt(s.get("baseline")), fmt(s.get("fault")),
                                         "" if s.get("changed") else "（未变）"))
        A("| `%s` | %s | %s | %s |" % (r["type"], r.get("inject", {}).get("type", "—"),
                                       "**有效**" if eff.get("effective") else "**无变化**",
                                       "<br>".join(rows) or "—"))
    A("")

    # ── 2. 判据层 ────────────────────────────────────────────────
    A("## 2. L1 · 判据层（契约 detect 规则在冻结 CCF 上机判，载体无关）")
    A("")
    A("| 用例 | 期望根因 | 期望码可判性 | 检出(绝对) | 检出(增量) | 图外码 |")
    A("|---|---|---|---|---|---|")
    for r in cases:
        sc = r.get("score") or {}
        jt = sc.get("judgeability") or {}
        jd = ", ".join("%s=%s" % (c, (jt.get(c) or {}).get("status")) for c in
                       (sc.get("expect_codes") or [])) or "—"
        A("| `%s` | %s | %s | %s | %s | %s |" % (
            r["type"],
            ", ".join("`%s`" % c for c in (sc.get("expect_codes") or [])) or "—",
            jd,
            ", ".join("`%s`" % c for c in (sc.get("detect_expected_hit") or [])) or "—",
            ", ".join("`%s`" % c for c in (sc.get("detect_expected_hit_incremental") or [])) or "—",
            ", ".join("`%s`" % c for c in (r.get("notExpressible") or [])) or "—"))
    A("")
    A("检出(绝对) = 期望码在本次快照判为 TRUE；检出(增量) = 再扣掉阴性对照的背景结论集。")
    A("两者不同，说明该码在健康态**本就为真**（`increased` 类判据在运行中的系统上恒有增量），"
      "不构成故障特异性信号。")
    A("")

    # ── 3. 根因层 ────────────────────────────────────────────────
    A("## 3. L2/L3 · 根因层与载体一致性")
    A("")
    A("> ★ 读法：「top1」必须连同**候选列表规模**一起读。"
      "实测 `/cause` 常把几乎整个词表都列为候选（多数 �—determined、命中条款 0），"
      "此时 top1 只反映**确定性排序**，不反映诊断能力。")
    A("")
    A("| 用例 | 期望根因 | 载体 | top1 | 候选数 | 其中有判定 | 命中条款>0 | 期望在候选内 | 一致 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for r in cases:
        sc = r.get("score") or {}
        exp = sc.get("expect_codes") or []
        tops = {}
        for carrier in ("KG", "ONTO"):
            resp = cause_of(d, r["type"], carrier)
            st = cand_stats(resp)
            if not st:
                A("| `%s` | %s | %s | — | — | — | — | — | — |" % (
                    r["type"], ", ".join("`%s`" % c for c in exp) or "—", carrier))
                continue
            tops[carrier] = st["top1"]
            hit = sorted(set(exp) & set(st["codes"]))
            A("| `%s` | %s | %s | %s | %d | %d | %d | %s | — |" % (
                r["type"], ", ".join("`%s`" % c for c in exp) or "—", carrier,
                ("`%s`" % st["top1"]) if st["top1"] else "—",
                st["n"], st["det"], st["sat"],
                ", ".join("`%s`" % c for c in hit) or "—"))
        if len(tops) >= 2:
            same = len(set(tops.values())) <= 1
            A("| `%s` | ← | **一致?** | %s | | | | | %s |" %
              (r["type"], " / ".join(str(v) for v in tops.values()), "✔" if same else "✘"))
    A("")
    pars = []
    for r in cases:
        t = {}
        for carrier in ("KG", "ONTO"):
            st = cand_stats(cause_of(d, r["type"], carrier))
            if st and st["top1"]:
                t[carrier] = st["top1"]
        if len(t) >= 2:
            pars.append(len(set(t.values())) <= 1)
    A("**载体 top1 一致率 = %d/%d = %s**" %
      (sum(1 for p in pars if p), len(pars),
       ("%.0f%%" % (100.0 * sum(1 for p in pars if p) / len(pars))) if pars else "—"))
    A("")

    # ── 3a. top1 稳定性检验（区分「诊断能力」与「恒定输出」） ──────
    A("### 3a. top1 稳定性检验 ——「命中」到底是诊断还是恒定输出？")
    A("")
    A("| 载体 | 有候选的用例数 | 不同 top1 个数 | top1 取值（出现例数 / 该 top1 的命中条款数） | 判读 |")
    A("|---|---|---|---|---|")
    for carrier in ("KG", "ONTO"):
        tops = []
        for r in cases:
            st = cand_stats(cause_of(d, r["type"], carrier))
            if st and st["top1"]:
                tops.append((r["type"], st["top1"], st["top1sat"]))
        distinct = sorted({x[1] for x in tops})
        samples = {c: next((x[2] for x in tops if x[1] == c), None) for c in distinct}
        verdict = ("**top1 恒定 ⇒ 该载体的 top1 不携带故障信息**" if len(distinct) == 1 and tops
                   else ("有区分" if len(distinct) > 1 else "无候选"))
        A("| %s | %d | %d | %s | %s |" % (
            carrier, len(tops), len(distinct),
            "<br>".join("`%s`（%d 例，命中条款 %s）" %
                        (c, sum(1 for x in tops if x[1] == c), samples[c])
                        for c in distinct) or "—", verdict))
    A("")
    A("★ **用途**：若某载体的 top1 在所有用例里都相同，任何「命中」都只是**该载体恒定输出的巧合**，"
      "不能记为诊断能力；报告结论必须据此打折。")
    A("")

    # ── 3b. 指标可用性体检（自动） ────────────────────────────────
    case_dirs = [os.path.join(d, r["type"]) for r in cases]
    aud = metric_audit(case_dirs)
    A("## 3b. 指标可用性体检（脚本自动统计，未做人工取舍）")
    A("")
    A("| 形态 | 含义 | 指标 |")
    A("|---|---|---|")
    A("| 部分用例缺席 | 判据在这些用例里必然不可判 | %s |" %
      ("<br>".join("`%s`（%d/%d）" % (x["metric"], x["presentIn"], x["of"]) for x in aud["absent"])
       or "—"))
    A("| collected 但 value=null | 判据恒 UNK | %s |" %
      ("<br>".join("`%s`（%d/%d）" % (x["metric"], x["nullIn"], x["presentIn"])
                   for x in aud["nullValued"]) or "—"))
    A("| collected 但 value 恒负 | 量纲/符号可疑 ⇒ `>` 类阈值判据恒不成立 | %s |" %
      ("<br>".join("`%s`（%d/%d，样本 %s）" % (x["metric"], x["negativeIn"], x["presentIn"],
                                             x["sample"]) for x in aud["negative"]) or "—"))
    A("")

    # ── 4. 逐用例明细 ────────────────────────────────────────────
    A("## 4. 逐用例明细")
    for r in cases:
        sc = r.get("score") or {}
        A("")
        A("### `%s` — %s" % (r["type"], (r.get("entry") or {}).get("label", "")))
        A("")
        A("- 判词：**%s**　match_kind=`%s`" % (r.get("verdict"), (r.get("entry") or {}).get("match_kind")))
        A("- 机制：%s" % (r.get("entry") or {}).get("mechanism", "—"))
        A("- 注入：%s　快照：`%s`　观测：%s" %
          (json.dumps(r.get("inject", {}), ensure_ascii=False),
           r.get("snapshotId"), r.get("obsCount")))
        A("- 检出症状码：%s" % (", ".join("`%s`" % c for c in (r.get("symptomCodes") or [])) or "（空）"))
        if r.get("notExpressible"):
            A("- ★ 图外码（能检出、无法表达进因果链）：%s" %
              ", ".join("`%s`" % c for c in r["notExpressible"]))
        if sc.get("expect_codes"):
            A("- 期望根因：%s　可判性：%s" % (
                ", ".join("`%s`" % c for c in sc["expect_codes"]),
                json.dumps(sc.get("judgeability"), ensure_ascii=False)))
        for carrier, v in (sc.get("perCarrier") or {}).items():
            A("- `%s`：到达=%s　候选=%s" % (carrier, v.get("reached"),
                                            ", ".join("`%s`" % c for c in (v.get("candidates") or [])) or "（空）"))
        if r.get("error"):
            A("- ⚠ 错误：`%s`" % r["error"])
        A("- 产物：`%s`" % os.path.join("fi-e2e/out", runid, r["type"]))

    # ── 5. 诚实边界 ──────────────────────────────────────────────
    A("")
    A("## 5. 诚实边界")
    A("")
    A("1. **判据可判性是运行时的**：`app.endpoint.*` 等遥测只在有进行中仿真轮次时才有值，"
      "空闲期是 `notCollected`。本报告的判据层结论只对本次各用例自己的快照成立。")
    A("2. **`/cause` 词表 ≠ 契约词表**：`/cause` 只接受因果图节点（14 个码），"
      "图外的码即使被判据层检出也无法表达；这类码在表中单列，不计入根因命中也不计为误报。")
    A("3. **`increased` 类判据不是故障特异性信号**：运行中的系统上任何计数器都恒有增量，"
      "所以绝对判定必然为真。必须看「增量 − 背景」。")
    A("4. **一次用例一次快照**：本测试不声称统计显著性（n 太小）。要显著性须按 M5 判决集口径"
      "扩到固定分母并做配对检验。")
    A("5. **注入台 `injectedAt` 不是时间锚**（只有 HH:MM:SS 且自愈会伪造）；"
      "本测试一律用自记墙钟 UTC 时刻。")

    out = os.path.join(d, "REPORT.md")
    io.open(out, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("已写 %s（%d 行）" % (out, len(L)))


if __name__ == "__main__":
    main()
