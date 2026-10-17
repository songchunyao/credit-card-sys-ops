# -*- coding: utf-8 -*-
"""fi-e2e/stats50.py —— 固定分母批次的**配对检验**与结论纪律（线②）。

为什么必须配对：同一批用例同时喂给 KG 与 ONTO（同一 snapshotId、同一症状集），
两组样本**不是独立的** —— 用独立样本检验会低估方差、把噪声当差异。
本脚本因此只做**配对**分析，并遵守项目既有的结论纪律：
  · 固定分母（退出/不可判/无效应**不缩分母**）；
  · 配对差异 < 20pp **只许写「未检出显著差异」**，不得写成「更好/更差」；
  · `BLOCKED`（没测到）与 `MISS`（答错）分开，绝不合并。

用法：
    python fi-e2e/stats50.py --runid suite50
产物：
    fi-e2e/out/<runid>/STATS.md, STATS.json
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.path.join(HERE, "out")
PRIMARY_METRIC = "root_cause_in_candidates"     # 主判据：期望根因码是否出现在候选集合里
SECONDARY = ("root_cause_top1", "false_claim", "abstain")


# ── 统计工具（不引入 scipy） ──────────────────────────────────────────────
def binom_two_sided_p(b: int, c: int) -> float:
    """McNemar 精确检验：在不一致对 (b, c) 下，H0: p=0.5。返回双侧 p。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def paired_bootstrap_diff(pairs, iters=10000, seed=20261013):
    """配对 bootstrap：对 (kg_flag, onto_flag) 序列重采样，取 (onto-kg) 的 95% CI。

    ★ 重采样的是**用例**（保持配对结构），不是各自的样本 —— 这是配对分析的关键。
    """
    rnd = random.Random(seed)
    n = len(pairs)
    if n == 0:
        return None, None
    diffs = []
    for _ in range(iters):
        s = 0
        for _i in range(n):
            a, b = pairs[rnd.randrange(n)]
            s += (1 if b else 0) - (1 if a else 0)
        diffs.append(s / n)
    diffs.sort()
    lo = diffs[int(0.025 * iters)]
    hi = diffs[int(0.975 * iters) - 1]
    return lo, hi


def cohens_h(p1, p2):
    def phi(p):
        p = min(max(p, 1e-9), 1 - 1e-9)
        return 2 * math.asin(math.sqrt(p))
    return abs(phi(p2) - phi(p1))


# ── 读批次 ────────────────────────────────────────────────────────────────
def load_run(runid):
    d = os.path.join(OUTROOT, runid)
    recs = []
    p = os.path.join(d, "results.jsonl")
    if os.path.exists(p):
        for line in io.open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    if not recs:
        for name in sorted(os.listdir(d)):
            cj = os.path.join(d, name, "case.json")
            if os.path.exists(cj):
                recs.append(json.load(io.open(cj, encoding="utf-8")))
    plan = {}
    pp = os.path.join(d, "suite-plan.json")
    if os.path.exists(pp):
        plan = json.load(io.open(pp, encoding="utf-8"))
    return d, recs, plan


def group_sequence(plan):
    """把 plan 的 `expanded`（按类型 + runs）摊平成**逐用例**的组别序列。

    ★ 踩过的坑：`expanded` 是「按类型」的（每项带 runs），不是逐用例的 ⇒
    直接拿用例序号去索引会错位（实测出现一整组 `?`）。这里按 runs 展开后对齐。
    """
    seq = []
    for e in plan.get("expanded") or []:
        for _ in range(int(e.get("runs") or 1)):
            seq.append(e.get("group") or "?")
    return seq


def group_of(seq, idx, ftype):
    return seq[idx] if idx < len(seq) else "?"


def carrier_flags(rec, carrier):
    """从用例记录里取该载体的三个布尔量（缺失记 None，**不当 False**）。"""
    sc = rec.get("score") or {}
    pc = (sc.get("perCarrier") or {}).get(carrier) or {}
    if not pc:
        return {"inCandidates": None, "top1": None, "falseClaim": None, "abstain": None}
    exp = sc.get("expect_codes") or []
    sym = sc.get("expect_symptoms") or []
    at_k = pc.get("expect_hit_k") or []
    top1 = pc.get("top1")
    cands = pc.get("candidates") or []
    return {
        "inCandidates": (bool(at_k) if exp else (bool(pc.get("symptom_hit_k")) if sym else None)),
        "top1": (top1 in exp) if exp else (top1 in sym if sym else None),
        "falseClaim": bool(pc.get("false_claim")) if exp else None,
        "abstain": (len(cands) == 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runid", required=True)
    ap.add_argument("--metric", default=PRIMARY_METRIC)
    ap.add_argument("--min-effect-pp", type=float, default=20.0,
                    help="结论纪律：配对差异小于该值(pp)时只许写『未检出显著差异』")
    a = ap.parse_args()

    d, recs, plan = load_run(a.runid)
    scored = [r for r in recs if r.get("type") != "baseline"]
    base = [r for r in recs if r.get("type") == "baseline"]
    N = len(scored)
    L = []
    A = L.append

    A("# 固定分母配对检验报告")
    A("")
    A("> 批次：`%s`　**固定分母 n=%d**（另有 %d 条阴性对照）" % (a.runid, N, len(base)))
    A("> 主判据：`%s`（期望根因码是否出现在该载体的候选集合）" % a.metric)
    A("> 生成：fi-e2e/stats50.py　配对检验不引入 scipy（McNemar 精确检验 + 配对 bootstrap）")
    A("")

    # ── 0. 分母与判词 ────────────────────────────────────────────────
    tally = Counter(r.get("verdict") or "BLOCKED" for r in scored)
    A("## 0. 分母与判词（分母不缩）")
    A("")
    A("| 判词 | 条数 | 占比 |")
    A("|---|---|---|")
    for k, v in tally.most_common():
        A("| `%s` | %d | %.1f%% |" % (k, v, 100.0 * v / N))
    A("")
    hits = sum(v for k, v in tally.items() if k.startswith("HIT"))
    A("**命中（含 PARTIAL）= %d/%d = %.1f%%**；`MISS` %d；`NO_EFFECT` %d；`UNJUDGEABLE` %d；`BLOCKED` %d"
      % (hits, N, 100.0 * hits / N, tally.get("MISS", 0), tally.get("NO_EFFECT", 0),
         tally.get("UNJUDGEABLE", 0), tally.get("BLOCKED", 0)))
    A("")
    A("★ 判读纪律：`BLOCKED`（没测到）与 `MISS`（答错）必须分开；`NO_EFFECT`（故障没产生）"
      "既不是算法的错、也不是命中。三者都不缩分母。")
    A("")

    # ── 1. 组别分层 ──────────────────────────────────────────────────
    A("## 1. 按组别分层（组 = 探针可判性 × 契约映射）")
    A("")
    byg = defaultdict(lambda: {"n": 0, "hit": 0, "miss": 0, "other": 0})
    gseq = group_sequence(plan)
    for i, r in enumerate(scored):
        g = group_of(gseq, i, r.get("type"))
        v = r.get("verdict") or "BLOCKED"
        byg[g]["n"] += 1
        if v.startswith("HIT"):
            byg[g]["hit"] += 1
        elif v == "MISS":
            byg[g]["miss"] += 1
        else:
            byg[g]["other"] += 1
    A("| 组 | 含义 | n | 命中 | MISS | 其它 | 命中率 |")
    A("|---|---|---|---|---|---|---|")
    GDESC = {"A": "判据齐备", "B": "部分可判", "C": "结构性不可判", "D": "症状级/无对应码",
             "E": "本环境不适用", "F": "阴性对照"}
    for g in sorted(byg):
        x = byg[g]
        A("| %s | %s | %d | %d | %d | %d | %.0f%% |"
          % (g, GDESC.get(g, "?"), x["n"], x["hit"], x["miss"], x["other"],
             100.0 * x["hit"] / x["n"] if x["n"] else 0))
    A("")

    # ── 2. 配对检验（KG vs ONTO） ────────────────────────────────────
    A("## 2. 配对检验：KG vs ONTO（主判据）")
    A("")
    A("★ 同一批用例同时喂两侧（同一 snapshotId、同一症状集）⇒ **必须配对**。")
    A("")
    pairs = []
    for r in scored:
        k = carrier_flags(r, "KG")[{"root_cause_in_candidates": "inCandidates",
                                    "root_cause_top1": "top1"}.get(a.metric, "inCandidates")]
        o = carrier_flags(r, "ONTO")[{"root_cause_in_candidates": "inCandidates",
                                      "root_cause_top1": "top1"}.get(a.metric, "inCandidates")]
        if k is None and o is None:
            continue
        pairs.append((bool(k), bool(o)))
    both = sum(1 for k, o in pairs if k and o)
    kg_only = sum(1 for k, o in pairs if k and not o)
    on_only = sum(1 for k, o in pairs if not k and o)
    neither = sum(1 for k, o in pairs if not k and not o)
    np_ = len(pairs)
    A("| | ONTO 命中 | ONTO 未命中 |")
    A("|---|---|---|")
    A("| **KG 命中** | %d | %d |" % (both, kg_only))
    A("| **KG 未命中** | %d | %d |" % (on_only, neither))
    A("")
    if np_ == 0:
        A("**无可配对用例**（两侧都无候选/都不可判）⇒ 本节不成立。")
    else:
        acc_kg = (both + kg_only) / np_
        acc_on = (both + on_only) / np_
        p = binom_two_sided_p(kg_only, on_only)
        lo, hi = paired_bootstrap_diff(pairs)
        diff_pp = 100.0 * (acc_on - acc_kg)
        A("- 可配对数 n=%d　KG 命中率 **%.1f%%**　ONTO 命中率 **%.1f%%**" % (np_, 100 * acc_kg, 100 * acc_on))
        A("- 不一致对：KG 独有 %d，ONTO 独有 %d" % (kg_only, on_only))
        A("- **McNemar 精确检验** p = %.4f" % p)
        A("- **配对 bootstrap 95%% CI**（ONTO−KG）= [%.1fpp, %.1fpp]　Cohen's h = %.3f"
          % (100 * lo, 100 * hi, cohens_h(acc_kg, acc_on)))
        A("")
        concl = ("**未检出显著差异**（|Δ|=%.1fpp < %.0fpp 纪律阈值，且 p=%.3f）"
                 % (abs(diff_pp), a.min_effect_pp, p)) if (abs(diff_pp) < a.min_effect_pp or p >= 0.05) \
            else ("**差异超过纪律阈值**：Δ=%.1fpp，p=%.3f（仍须核对载体差异登记是否被违反）"
                  % (diff_pp, p))
        A("### 结论（按项目纪律）")
        A("")
        A(concl)
        A("")
        A("★ 纪律依据：项目规定 **< 20pp 不得下结论**；且 `undeclaredCarrierDivergences` 不为 0 时，"
          "两侧差异无法仅归因于载体 ⇒ 结论须打折。")
    A("")

    # ── 3. 载体一致性 ────────────────────────────────────────────────
    A("## 3. 载体一致性（top1 是否相同）")
    A("")
    par = [r for r in scored
           if (((r.get("score") or {}).get("perCarrier") or {}).get("KG") or {}).get("top1")
           and (((r.get("score") or {}).get("perCarrier") or {}).get("ONTO") or {}).get("top1")]
    same = sum(1 for r in par if (r.get("score") or {}).get("carrier_parity_top1"))
    A("可比用例 %d 条（两侧都给出了 top1）；**top1 相同 %d 条 = %.0f%%**"
      % (len(par), same, 100.0 * same / len(par) if par else 0))
    A("")
    A("★ 注意分母：绝大多数用例只有 0 个候选（检出症状不在 `/cause` 的 14 码词表内，"
      "结构性表达不了）⇒ 「一致率」只在这 %d 条上成立，不得外推。" % len(par))
    A("")

    # ── 4. 假命中体检 ────────────────────────────────────────────────
    A("## 4. 假命中体检（top1 恒定 / 命中条款为 0）")
    A("")
    tops = defaultdict(Counter)
    for r in scored:
        pc = (r.get("score") or {}).get("perCarrier") or {}
        for carrier in ("KG", "ONTO"):
            t = (pc.get(carrier) or {}).get("top1")
            if t:
                tops[carrier][t] += 1
    A("| 载体 | 有 top1 的用例 | 不同 top1 | 分布 |")
    A("|---|---|---|---|")
    for carrier in ("KG", "ONTO"):
        c = tops[carrier]
        A("| %s | %d | %d | %s |" % (carrier, sum(c.values()), len(c),
                                     "；".join("%s×%d" % kv for kv in c.most_common(5)) or "—"))
    A("")
    for carrier in ("KG", "ONTO"):
        c = tops[carrier]
        if len(c) == 1 and sum(c.values()) >= 3:
            A("- ★ **%s 的 top1 恒定**（%s ×%d）⇒ 其「命中」若存在，极可能是恒定输出的巧合，"
              "须连同 `bestObject.satisfiedClauses` 一起看。" % (carrier, list(c)[0], sum(c.values())))
    A("")

    # ── 5. 载体分歧：按**当前**登记表重新分类 ──────────────────────
    A("## 5. 载体分歧：按**当前**登记表重新分类（回归信号）")
    A("")
    A("★ 口径：不用 case.json 里**运行当时**记录的结果（那时登记表可能还没覆盖），"
      "而是读各例保存的 cause_KG/ONTO.json **原始响应**，用**当前**登记表重跑分类 —— "
      "这样「未声明」才是**此刻**的事实，能直接当回归信号。")
    A("")
    sys.path.insert(0, HERE)
    from fi import load_declared_divergences, classify_divergences  # noqa: E402
    dec = load_declared_divergences()
    A("当前登记表条目 = **%d** 条" % len(dec["entries"]))
    A("")
    und = Counter(); declared = Counter(); reclassified = 0
    for name in sorted(os.listdir(d)):
        cdir = os.path.join(d, name)
        if not os.path.isdir(cdir):
            continue
        fk, fo = os.path.join(cdir, "cause_KG.json"), os.path.join(cdir, "cause_ONTO.json")
        if not (os.path.exists(fk) and os.path.exists(fo)):
            continue
        try:
            kg = json.load(io.open(fk, encoding="utf-8"))
            on = json.load(io.open(fo, encoding="utf-8"))
        except Exception:
            continue
        if not (kg.get("ok") and on.get("ok")):
            continue
        reclassified += 1
        r = classify_divergences({"ok": True, "raw": kg.get("raw")},
                                 {"ok": True, "raw": on.get("raw")}, dec)
        for row in r["declared"]:
            key = "%s.%s%s" % (row["code"], row["field"],
                                "" if row.get("matchesDeclared") else "(登记过期)")
            (declared if row.get("matchesDeclared") else und)[key] += 1
        for row in r["undeclared"]:
            und["%s.%s" % (row["code"], row["field"])] += 1
    A("可重分类用例 = **%d**（两侧 /cause 都成功返回的例）" % reclassified)
    A("")
    A("- 命中登记表：**%d 行**" % sum(declared.values()))
    A("- **未声明（须查）：%d 行**，涉及 %d 个 (码,字段) 对" % (sum(und.values()), len(und)))
    A("")
    if und:
        A("| (码.字段) | 出现次数 |"); A("|---|---|")
        for k, v in und.most_common():
            A("| %s | %d |" % (k, v))
        A("")
        A("★ 这些**不在**登记表内 ⇒ 载体对比结论须先查清；查清后按「同一 (code, field) "
          "只登记一条」的纪律**更新**登记表（不得新增重复条目）。")
    else:
        A("✔ **0 处未声明**：本次所有载体字段差异都在登记表内 ⇒ 可作回归探测基线。")
    A("")

    # ── 6. 诚实边界 ──────────────────────────────────────────────────
    A("## 6. 诚实边界")
    A("")
    A("1. **症状集相关**：载体分歧集依赖当轮检出的症状码；「未声明 0」只对**该症状集**成立。")
    A("2. **本批只覆盖确定性 `cc_cause` 路径**；LLM Agent 自己写 Cypher/SPARQL 读 Finding 的路径未覆盖。")
    A("3. **`in_candidates` 是宽口径**：候选集合常含几乎整个词表（实测 12–13 / 14）⇒ "
      "该指标偏高是口径所致，不是诊断能力强。**主判据应以 `root_cause_top1` 复核**。")
    A("4. **未做多重比较校正的场合**：本报告的分层表为描述性，不作逐层显著性声明。")
    A("")

    out_md = os.path.join(d, "STATS.md")
    io.open(out_md, "w", encoding="utf-8").write("\n".join(L) + "\n")
    io.open(os.path.join(d, "STATS.json"), "w", encoding="utf-8").write(json.dumps({
        "runid": a.runid, "n": N, "nBaseline": len(base),
        "verdicts": dict(tally), "hits": hits,
        "paired": {"n": np_, "both": both, "kgOnly": kg_only, "ontoOnly": on_only,
                   "neither": neither, "accKG": acc_kg if np_ else None,
                   "accONTO": acc_on if np_ else None,
                   "mcnemarExactP": binom_two_sided_p(kg_only, on_only) if np_ else None,
                   "bootstrapCI95_on_minus_kg": [lo, hi] if np_ else None,
                   "cohensH": cohens_h(acc_kg, acc_on) if np_ else None},
        "carrierParity": {"comparable": len(par), "same": same},
        "undeclaredDivergences": dict(und),
    }, ensure_ascii=False, indent=1))
    print("已写 %s 与 STATS.json" % out_md)
    print("固定分母 n=%d；命中 %d (%.1f%%)；配对数 %d；未声明分歧 %d 行"
          % (N, hits, 100.0 * hits / N if N else 0, np_, sum(und.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
