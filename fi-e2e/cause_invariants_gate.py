# -*- coding: utf-8 -*-
"""fi-e2e/cause_invariants_gate.py —— `cc_cause` 候选的**跨字段一致性门禁**（G-FI-1）。

## 为什么需要它（实证来源）

2026-10-13 定位 ONTO 侧「条款数虚高」时，**定案证据来自响应内部自相矛盾**：

```
FM-DB-LOCK-WAIT   totalClauses = 5   undeterminedClauses = 5
  condition        = detect 0/5 命中FM-DB-LOCK-WAIT      ← 自称 5 条条款
  evidenceSourceIds= ['判据个体:J-lock-1','判据个体:J-lock-2']  ← 只有 2 个判据个体
```

5 条条款却只有 2 个判据个体 ⇒ 3 条是重复的（SPARQL `OPTIONAL` 扇出被当成多条判据）。
**在此之前，两侧所有门禁（G24 工具面 / P3 公理落位 / 纯度六面）都不检查响应内部是否自洽**，
所以这个 bug 让 ONTO 侧「永远没有候选能命中条款」而无人发现。

## 判据集（载体无关的内部不变量；任一条不成立即该候选违规）

| id | 不变量 | 抓什么 |
|---|---|---|
| INV-1 | 若 `evidenceSourceIds` 以「判据个体:」列举逐条款来源，则其**去重数必须 = `totalClauses`** | 条款重复膨胀（本例） |
| INV-2 | `satisfiedClauses + undeterminedClauses <= totalClauses` | 计数越界（剩余应为 FALSE） |
| INV-3 | `determined == (undeterminedClauses == 0)` | `determined`**定义**写错（ONTO 原为「任一可判即算已判定」） |
| INV-4 | 同一次响应内 `rank` 唯一且连续 1..n | 排序不确定 / 名次重复 |
| INV-5 | `satisfiedClauses <= totalClauses` 且 `undeterminedClauses <= totalClauses` | 单值越界 |
| INV-6 | `relationPriority ∈ {causes, conditional_causes, relates_to}`（发关系名，非数字） | 字段形态漂移（N2 已统一，防回归） |

INV-1 的作用域说明：`evidenceSourceIds` 的语义**两侧不同**（KG 列阈值/证据来源，
ONTO 列判据个体），故 INV-1 只在**该字段确以「判据个体:」前缀逐条款列举时**才断言 ——
不假装一个不存在的跨载体约定。

## 用法

```
python fi-e2e/cause_invariants_gate.py --runid suite50        # 离线复核批次产物
python fi-e2e/cause_invariants_gate.py --live                 # 在线打两侧:8301/:8302
```

退出码：`0 = PASS`；`1 = FAIL`（有候选违反不变量）；`2 = BLOCKED`（没测到：无产物/两侧不可达）。
BLOCKED ≠ FAIL：缺输入时**绝不打印 PASS**。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.path.join(HERE, "out")
TOOLS = {"KG": "http://192.0.2.11:8301", "ONTO": "http://192.0.2.11:8302"}
JUDGMENT_PREFIX = "判据个体:"
RELATION_ENUM = {"causes", "conditional_causes", "relates_to", "CC_CAUSES",
                 "CC_CONDITIONAL_CAUSES", "CC_RELATES_TO"}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def check_candidate(code: str, cand: dict) -> list:
    """对单个候选逐条验不变量；返回违规列表 [{inv, detail}]。"""
    bad = []
    total = _int(cand.get("totalClauses"))
    sat = _int(cand.get("satisfiedClauses"))
    und = _int(cand.get("undeterminedClauses"))
    det = cand.get("determined")
    srcs = cand.get("evidenceSourceIds") or []

    # INV-1：逐条款来源（若以「判据个体:」列举）的去重数必须等于 totalClauses
    jmts = [s for s in srcs if str(s).startswith(JUDGMENT_PREFIX)]
    if jmts and total is not None:
        uniq = len(set(str(s) for s in jmts))
        if uniq != total:
            bad.append({"inv": "INV-1",
                        "detail": "条款数 %d ≠ 判据个体系数 %d（→ 疑似重复膨胀：%s）"
                                  % (total, uniq, ", ".join(str(s) for s in jmts[:4]))})

    # INV-2 / INV-5：计数关系
    if None not in (total, sat, und):
        if sat + und > total:
            bad.append({"inv": "INV-2",
                        "detail": "satisfied(%d)+undetermined(%d) > total(%d)" % (sat, und, total)})
        if sat > total or und > total:
            bad.append({"inv": "INV-5",
                        "detail": "单值越界：satisfied=%d undetermined=%d total=%d" % (sat, und, total)})

    # INV-3：determined 的定义（必须「无 UNK」才算已判定）
    if und is not None and det is not None:
        if bool(det) != (und == 0):
            bad.append({"inv": "INV-3",
                        "detail": "determined=%s 但 undeterminedClauses=%d（应为 %s）"
                                  % (det, und, und == 0)})

    # INV-6：relationPriority 形态（发契约关系名，不发数字）
    rp = cand.get("relationPriority")
    if rp is not None and str(rp) not in RELATION_ENUM:
        bad.append({"inv": "INV-6", "detail": "relationPriority=%r 不在契约关系枚举内" % (rp,)})
    return bad


def check_response(carrier: str, raw: dict) -> dict:
    """对一次 /cause 响应做整体检查（含 INV-4 名次连续性）。"""
    cands = (raw or {}).get("candidates") or []
    rows, viol = [], []
    for c in cands:
        b = check_candidate(c.get("faultCode"), c)
        if b:
            rows.append((c.get("faultCode"), b))
            viol.extend(b)
    ranks = [c.get("rank") for c in cands]
    ri = [r for r in ranks if isinstance(r, int)]
    if ri and sorted(ri) != list(range(1, len(ri) + 1)):
        viol.append({"inv": "INV-4",
                     "detail": "rank 非唯一连续 1..%d（实测 %s）" % (len(ri), sorted(ri)[:12])})
        rows.append(("<rank>", [viol[-1]]))
    return {"carrier": carrier, "nCandidates": len(cands), "violations": viol, "byCode": rows}


def collect_live():
    """在线打两侧 /cause（用固定症状集，保证可比）。"""
    if requests is None:
        return None, "缺少 requests 库"
    sym = ["FM-APP-ENDPOINT-SLOW", "FM-CTR-CPU-THROTTLED"]
    out = {}
    for carrier, base in TOOLS.items():
        try:
            requests.post(base + "/guide", json={}, timeout=120)
            j = requests.post(base + "/cause", json={"symptomCodes": sym, "maxHops": 4},
                              timeout=180).json()
            out[carrier] = j
        except Exception as e:  # noqa: BLE001
            return None, "%s 不可达：%s" % (carrier, str(e)[:120])
    return out, None


def collect_run(runid):
    d = os.path.join(OUTROOT, runid)
    if not os.path.isdir(d):
        return None, "无此批次：%s" % d
    cases = []
    for cdir in sorted(glob.glob(os.path.join(d, "*"))):
        if not os.path.isdir(cdir):
            continue
        for carrier in ("KG", "ONTO"):
            p = os.path.join(cdir, "cause_%s.json" % carrier)
            if not os.path.exists(p):
                continue
            try:
                j = json.load(io.open(p, encoding="utf-8"))
            except Exception:
                continue
            if j.get("ok"):
                cases.append((os.path.basename(cdir), carrier, j.get("raw") or {}))
    if not cases:
        return None, "批次 %s 里没有可用的 /cause 产物" % runid
    return cases, None


def main():
    ap = argparse.ArgumentParser(description="cc_cause 候选跨字段一致性门禁（G-FI-1）")
    ap.add_argument("--runid", default=None, help="离线复核该批次产物")
    ap.add_argument("--live", action="store_true", help="在线打两侧 /cause")
    ap.add_argument("--max-print", type=int, default=12)
    a = ap.parse_args()

    print("=" * 88)
    print("G-FI-1  cc_cause 候选跨字段一致性门禁")
    print("=" * 88)

    if a.live:
        data, err = collect_live()
        if err:
            print("★ BLOCKED（前置条件不满足）：%s" % err)
            print("★ BLOCKED ≠ FAIL：缺输入造成的『两侧都空』不是『两侧一致』。")
            return 2
        results = [check_response(c, r) for c, r in data.items()]
        scope = "在线（两侧 :8301 / :8302，固定症状集）"
    else:
        runid = a.runid
        if not runid:
            subs = sorted(x for x in os.listdir(OUTROOT)) if os.path.isdir(OUTROOT) else []
            print("★ BLOCKED：需 --runid 或 --live。现有批次：%s" % (", ".join(subs) or "（无）"))
            return 2
        cases, err = collect_run(runid)
        if err:
            print("★ BLOCKED（前置条件不满足）：%s" % err)
            return 2
        results = [check_response("%s@%s" % (carrier, case), raw) for case, carrier, raw in cases]
        scope = "离线复核批次 %s（%d 份响应）" % (runid, len(cases))

    print("作用域：%s" % scope)
    print("判据：INV-1 条款数=判据个体系数 ｜ INV-2 计数不越界 ｜ INV-3 determined 定义"
          " ｜ INV-4 rank 连续 ｜ INV-5 单值边界 ｜ INV-6 relationPriority 形态")
    print("-" * 88)

    n_resp = len(results)
    n_cand = sum(r["nCandidates"] for r in results)
    allv = [(r["carrier"], v) for r in results for v in r["violations"]]
    by_inv = {}
    for _c, v in allv:
        by_inv[v["inv"]] = by_inv.get(v["inv"], 0) + 1

    print("被检响应 %d 份 ｜ 候选合计 %d 个 ｜ **违规 %d 条**" % (n_resp, n_cand, len(allv)))
    if by_inv:
        print("按判据分布：" + "  ".join("%s×%d" % kv for kv in sorted(by_inv.items())))
    print()
    for carrier, v in allv[:a.max_print]:
        print("  [%s] %-7s %s" % (v["inv"], carrier.split("@")[0], v["detail"][:104]))
    if len(allv) > a.max_print:
        print("  ...（另 %d 条）" % (len(allv) - a.max_print))
    print("-" * 88)

    if allv:
        print("RESULT: FAIL（%d 条候选违反跨字段不变量）" % len(allv))
        print("处置：先查该载体的条款装载/计数逻辑；对照另一侧同码的取值与契约 `detect` 条款数。")
        return 1
    print("RESULT: PASS（%d 份响应 / %d 个候选全部满足跨字段不变量）" % (n_resp, n_cand))
    print("★ 边界：本门禁只证『响应内部自洽』，**不证**判据正确、也不证两侧一致 —— "
          "后者由 carrier-divergences 登记表与 STATS 报告负责。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
