#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5 判决集 · 机器判据 + 统计运行器（单一入口）。

判据与统计口径的**唯一契约来源**：``docs\\06-评测设计（A-B对照）.md``
（§1.1 用例族 / §1.2 用例结构 / §1.3 难度分层 / §1.4 标注流程与 G6 /
§2.1 指标与分母 / §2.2 效率指标 / §6.1 配对样本 / §6.2 检验与效应量 /
§6.3 样本量论证与报告纪律 / §7 结论模板）。

复用（**只复用逻辑，不复用文本**，docs/06 §1.5）：
  * ``<workspace>\\upstream-eval\\score.py`` 的 FALSE_CLAIMS **否定邻窗消歧**逻辑
    —— 字典在 ``:20-42``、否定词表 ``NEG`` 在 ``:44``、消歧实现在 ``:65-76``（``_false_claims``）、
    调用面与 ``mixed_precision`` 公式在 ``:112-113``（``score_record`` 内）。见 ``m5\\judge.py``。
  * ``<workspace>\\upstream-eval\\harness.py:118-132`` 的遥测字段口径，
    以及 ``:109-117`` 的工具调用去重口径。见 ``m5\\judge.py:efficiency_from_record``。

用法::

    python shared/verify/m5_judge.py --cases <yaml|json> --answers <jsonl> --out <dir>
        [--groups a,b,c] [--expect-groups N] [--strict] [--expect-full-suite]
    python shared/verify/m5_judge.py --selftest [--out <dir>]

输出（``--out`` 目录）：
  ``verdicts.jsonl`` 逐用例判定明细（含每个命中断言的匹配窗口，便于复核）
  ``metrics.json``   分族/分层/分组的指标与分母
  ``stats.json``     配对检验、效应量、bootstrap CI、Bonferroni、**样本量纪律**
  ``report.md``      docs/06 §7 模板报告

退出码：
  ``0`` 通过（可含 BLOCKED 小节——**BLOCKED 绝不写成 FAIL**）
  ``1`` fail-closed 校验失败 / 运行失败
  ``2`` ``--selftest`` 专有：无 FAIL 但存在 BLOCKED

★ R-49：stdout/stderr 一律显式 UTF-8（宿主 GBK 代码页曾崩
  ``UnicodeEncodeError: 'gbk' codec can't encode character '\\u21d2'``）。
★ 纯离线、确定性、**只读输入**：不联网、不调用 Dify、不修改 ``docs\\**`` /
  ``scheme1-kg\\**`` / ``scheme2-onto\\**`` / ``shared\\model\\**`` / ``shared\\deploy\\**`` /
  ``tmp\\_m4_worklist.md``，只在 ``--out`` 目录内写文件。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非重定向环境
    pass

# ★ M6.3 容器侧效率的**臂级聚合**（由 shared/verify/m6/carrier_efficiency.py 产出）：
#   记录缺 query_latency_p50/p95/facts_consumed 时按臂回退；**来源逐字段记录**，仍不补 0。
_CARRIER: dict = {}

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from m5 import PKG_VERSION, contract as C, judge as J, reporting as R, stats as S, selftest as ST  # noqa: E402

EFF_FIELDS = ("elapsed_s", "ttft_s", "input_tokens", "output_tokens", "tool_calls_n",
              "obs_total_bytes", "iterations_used", "query_latency_p50",
              "query_latency_p95", "facts_consumed")

# 纪律表逐行指标：key -> (取值函数名, 是否二值)
DISC_METRICS = (
    ("root_cause_accuracy", "_hit", True),
    ("layer_accuracy", "_layer_hit", True),
    ("object_accuracy", "_object_all", True),
    ("path_f1", "_path_f1", False),
    ("false_claim_rate", "_false_claim_any", True),
    ("mixed_precision", "mixed_precision", False),
    ("evidence_accuracy", "_evidence_hit", True),
    ("abstain_rate", "abstain", True),
)

DEFS = {
    "root_cause_accuracy": "expect_root_cause 被命中 / ★固定分母（每条 repeat = 用例数；abstain 与缺记录都计未命中）；一题多答只取第一个候选（docs/06 §2.1 约定 3）",
    "layer_accuracy": "答案声明的根因层命中 expect_layer / ★固定分母（abstain 与缺记录都计未命中）（层判定 = 距第一候选最近的层别名）",
    "object_accuracy": "expect_objects 全命中（hit_all 为主判据；hit_any 为次判据）/ ★固定分母（abstain 与缺记录都计未命中）",
    "path_f1": "detected 与 expect_path 的集合 F1（不可用观测按 0 计 / 固定分母 = 用例数×repeat 数；docs/06 §2.1）",
    "false_claim_rate": "出现 neg 中错误归因的用例数 / ★固定分母（abstain 不是错误归因 ⇒ 进分母不进分子；越低越好）",
    "mixed_precision": "pos 命中 / (pos 命中 + 错误归因数)；聚合口径 = 逐用例均值 macro（另报 micro）；分母是 docs/06 规定的适用子集，另给固定分母参考列",
    "evidence_accuracy": "evidence_facts id 至少命中一个的用例数 / docs/06 规定的适用子集（有用例带 evidence_facts）；另给固定分母参考列",
    "abstain_rate": "abstain 记录数 / ★固定分母（用例数×repeat 数；docs/06 §2.1「既不计对也不计错」，单列）",
}

ABSTAIN_DEF = ("主口径（队长裁定 2026-09-29；docs/06 §2.1 + 契约 shared/model/m5-cases.yaml "
               "scoring.abstain）：**固定分母**（每条 repeat 的分母 = 用例数），"
               "**abstain 计未命中**——abstain 既不进分子、也不从分母扣除（银行运维看端到端可用性，"
               "保守方向只会降低读数）。缺记录与 abstain 分别单列（n_missing / n_abstain），都不静默剔除。"
               "旧「可用记录数」口径降为诊断列 rate_excluding_abstain / denominator_excluding_abstain"
               "（仅供参考，不得当作主口径）；rate_abstain_as_error 与主口径 rate 恒等（同一尺度的另一种写法）。")


# ── 指标单元 ────────────────────────────────────────────────────────────────

def _flag_cell(verdicts, flag, key, *, n_cases, definition=""):
    """二值指标单元：★ 主口径 = 固定分母（每条 repeat 分母 = 用例数），abstain 计未命中。

    参考列：``rate_excluding_abstain``（旧口径，只按非 abstain 记录算）与
    ``rate_abstain_as_error``（与主口径 ``rate`` 同值，保留键名兼容既有读者）。
    缺记录（``n_records < n_expected``）与 abstain 分别计数，都不静默剔除。
    该组零记录 ⇒ BLOCKED（无观测，不是 FAIL）。
    """
    repeats = sorted({v["repeat"] for v in verdicts})
    per_repeat, hits_total, denom_total, abstain_total, missing_total = [], 0, 0, 0, 0
    for rep in repeats:
        sub = [v for v in verdicts if v["repeat"] == rep]
        app = [v for v in sub if v.get(flag) is not None]
        h = sum(1 for v in app if v[flag] is True)
        d = int(n_cases)          # ★ 固定分母：不随记录数、也不随 abstain 数变化
        n_missing = max(0, d - len(sub)) if d else 0
        per_repeat.append({
            "repeat": rep, "n_expected": d, "n_records": len(sub),
            "numerator": h, "denominator": d,
            "abstain_n": len(sub) - len(app), "n_missing": n_missing,
            "rate": round(h / d, 8) if d else None,
            "rate_excluding_abstain": round(h / len(app), 8) if app else None,
            "rate_abstain_as_error": round(h / d, 8) if d else None,
        })
        hits_total += h
        denom_total += d
        abstain_total += len(sub) - len(app)
        missing_total += n_missing
    n_records = len(verdicts)
    n_avail = n_records - abstain_total
    cell = {"metric": key, "definition": definition or DEFS.get(key, ""),
            "n_cases": n_cases, "n_records": n_records, "repeats": repeats,
            "per_repeat": per_repeat,
            "numerator": hits_total, "denominator": denom_total,
            "n_abstain": abstain_total, "n_missing": missing_total,
            "denominator_excluding_abstain": n_avail,
            "rate_excluding_abstain": (round(hits_total / n_avail, 8) if n_avail else None),
            "denominator_abstain_as_error": denom_total,
            "rate_abstain_as_error": (round(hits_total / denom_total, 8)
                                      if denom_total else None),
            "abstain_policy": ABSTAIN_DEF}
    rates = [p["rate"] for p in per_repeat if p["rate"] is not None]
    if n_records == 0 or denom_total == 0 or not rates:
        cell.update({"status": "BLOCKED", "rate": None,
                     "note": "该组零记录（无观测）⇒ 不可算（BLOCKED，不是 FAIL）"})
        return cell
    st = S.summarise(rates)
    cell.update({"status": "PASS",
                 "rate": round(sum(rates) / len(rates), 8),
                 "rate_mean_over_repeats": round(sum(rates) / len(rates), 8),
                 "rate_sd_over_repeats": st.get("sd"),
                 "rate_pooled": round(hits_total / denom_total, 8)})
    return cell


def _path_cell(verdicts, *, n_cases):
    vals = [v["_path_f1"] for v in verdicts if v.get("_path_f1") is not None]
    n_records = len(verdicts)
    n_repeats = len({v["repeat"] for v in verdicts}) or 1
    n_expected = int(n_cases) * n_repeats          # ★ 固定分母（跨 repeat）
    mean_avail = (round(sum(vals) / len(vals), 8) if vals else None)
    cell = {"metric": "path_f1", "definition": DEFS["path_f1"], "n_cases": n_cases,
            "n_records": n_records, "numerator": None,
            "denominator": n_expected, "n_repeats": n_repeats,
            "n_abstain": n_records - len(vals),
            "n_missing": max(0, n_expected - n_records),
            "sum": round(sum(vals), 8) if vals else None,
            "mean": mean_avail,
            "denominator_excluding_abstain": len(vals),
            "rate_excluding_abstain": mean_avail,
            "denominator_abstain_as_error": n_expected,
            "rate_abstain_as_error": (round(sum(vals) / n_expected, 8)
                                      if n_expected else None),
            "abstain_policy": ABSTAIN_DEF}
    if not vals or n_records == 0:
        cell.update({"status": "BLOCKED", "rate": None,
                     "note": "无可用观测 ⇒ 不可算（BLOCKED，不是 FAIL）"})
        cell["rate"] = None
        return cell
    # ★ 主口径 rate = 固定分母（不可用观测按 0 计）；mean 保留为「可用观测均值」参考列
    cell.update({"status": "PASS", "rate": round(sum(vals) / n_expected, 8)})
    return cell


def _mixed_precision_cell(verdicts, *, n_cases):
    app = [v for v in verdicts
           if (not v["abstain"]) and (v["pos"]["n_hit"] + v["false_claim"]["n"]) > 0]
    ph = sum(v["pos"]["n_hit"] for v in app)
    fc = sum(v["false_claim"]["n"] for v in app)
    micro = (ph / (ph + fc)) if (ph + fc) else None
    macros = [v["mixed_precision"] for v in app if v["mixed_precision"] is not None]
    macro = (sum(macros) / len(macros)) if macros else None
    n_non_abstain = sum(1 for v in verdicts if not v["abstain"])
    cell = {"metric": "mixed_precision", "definition": DEFS["mixed_precision"],
            "n_cases": n_cases, "n_records": len(verdicts),
            "numerator": ph, "denominator": ph + fc, "pos_hit": ph, "false_claim_n": fc,
            "micro": None if micro is None else round(micro, 8),
            "macro": None if macro is None else round(macro, 8),
            "coverage": (round(len(app) / n_non_abstain, 6) if n_non_abstain else None),
            "n_applicable": len(app), "n_abstain": len(verdicts) - n_non_abstain,
            "formula_source": ("<workspace>\\upstream-eval\\score.py:112-113"
                               "（pos_hit/(pos_hit+FC)，分母 0 时返回 None）"),
            "aggregation_note": ("docs/06 未规定聚合口径：主口径取逐用例均值 macro，"
                                 "另报 micro=Σpos/(Σpos+ΣFC)，coverage = 适用记录占比")}
    if micro is None and macro is None:
        cell.update({"status": "BLOCKED", "rate": None,
                     "note": "无适用记录（pos 与错误归因同时为 0）⇒ 不可算（BLOCKED）"})
        return cell
    cell["status"] = "PASS"
    cell["rate"] = cell["macro"] if cell["macro"] is not None else cell["micro"]
    return cell


def _evidence_cell(verdicts, *, n_cases):
    app = [v for v in verdicts if v.get("_evidence_hit") is not None]
    hits = sum(1 for v in app if v["_evidence_hit"] is True)
    all_hits = sum(1 for v in app if v["evidence"]["all"] is True)
    n_records = len(verdicts)
    # ★ 适用子集分母（docs/06 规定「有用例带 evidence_facts」）保留；
    # 固定分母参考列（abstain 与缺记录按 0 计）单列，避免与主判据口径混淆。
    n_repeats = len({v["repeat"] for v in verdicts}) or 1
    n_expected = int(n_cases) * n_repeats
    cell = {"metric": "evidence_accuracy", "definition": DEFS["evidence_accuracy"],
            "n_cases": n_cases, "n_records": n_records, "numerator": hits,
            "denominator": len(app), "n_abstain": n_records - len(app),
            "n_missing": max(0, n_expected - n_records),
            "denominator_fixed": n_expected,
            "rate_fixed_denominator": (round(hits / n_expected, 8) if n_expected else None),
            "denominator_excluding_abstain": len(app),
            "rate_excluding_abstain": (round(hits / len(app), 8) if app else None),
            "denominator_abstain_as_error": n_expected,
            "rate_abstain_as_error": (round(hits / n_expected, 8) if n_expected else None),
            "abstain_policy": ABSTAIN_DEF}
    if not app:
        cell.update({"status": "BLOCKED", "rate": None,
                     "note": "无用例带 evidence_facts ⇒ 不可算（BLOCKED，不是 FAIL）"})
        return cell
    cell.update({"status": "PASS", "rate": round(hits / len(app), 8),
                 "all_hit_numerator": all_hits,
                 "all_hit_rate": round(all_hits / len(app), 8),
                 "aux_detector_rate": round(
                     sum(1 for v in app if v["evidence"]["aux_detector_hit"]) / len(app), 8)})
    return cell


def _abstain_cell(verdicts, *, n_cases):
    n = len(verdicts)
    k = sum(1 for v in verdicts if v["abstain"])
    n_repeats = len({v["repeat"] for v in verdicts}) or 1
    n_expected = int(n_cases) * n_repeats          # ★ 固定分母
    cell = {"metric": "abstain_rate", "definition": DEFS["abstain_rate"],
            "n_cases": n_cases, "n_records": n, "numerator": k, "denominator": n_expected,
            "n_abstain": k, "n_missing": max(0, n_expected - n),
            "abstain_policy": ABSTAIN_DEF,
            "denominator_excluding_abstain": n - k,
            "rate_excluding_abstain": None,
            "denominator_abstain_as_error": n_expected,
            "rate_abstain_as_error": (round(k / n_expected, 8) if n_expected else None)}
    if n == 0 or n_expected == 0:
        cell.update({"status": "BLOCKED", "rate": None})
    else:
        cell.update({"status": "PASS", "rate": round(k / n_expected, 8)})
    return cell


def _metrics_for(verdicts, *, n_cases, with_aux=True):
    m = {
        "root_cause_accuracy": _flag_cell(verdicts, "_hit", "root_cause_accuracy",
                                          n_cases=n_cases),
        "layer_accuracy": _flag_cell(verdicts, "_layer_hit", "layer_accuracy",
                                     n_cases=n_cases),
        "object_accuracy": _flag_cell(verdicts, "_object_all", "object_accuracy",
                                      n_cases=n_cases),
        "object_accuracy_any": _flag_cell(verdicts, "_object_any", "object_accuracy_any",
                                          n_cases=n_cases,
                                          definition="expect_objects 至少命中一个（次判据）"),
        "path_f1": _path_cell(verdicts, n_cases=n_cases),
        "false_claim_rate": _flag_cell(verdicts, "_false_claim_any", "false_claim_rate",
                                       n_cases=n_cases),
        "mixed_precision": _mixed_precision_cell(verdicts, n_cases=n_cases),
        "evidence_accuracy": _evidence_cell(verdicts, n_cases=n_cases),
        "abstain_rate": _abstain_cell(verdicts, n_cases=n_cases),
    }
    if with_aux:
        # 反作弊读数：断言了任何故障模式（code 或 detector 通道）的记录占比
        m["fabrication_rate"] = _flag_cell(
            verdicts, "_any_fault", "fabrication_rate", n_cases=n_cases,
            definition=("断言了任一故障模式（第一候选非空）的记录占比；"
                        "族C5 正常态上该值应趋 0（docs/06 §1.1「不得虚构」）"))
        fc_ev = sum(v["false_claim"]["n"] for v in verdicts)
        m["false_claim_rate"]["events"] = fc_ev
    return m


def _efficiency_for(verdicts):
    out = {}
    for f in EFF_FIELDS:
        vals = [v["telemetry"].get(f) for v in verdicts]
        vals = [x for x in vals if isinstance(x, (int, float)) and not isinstance(x, bool)]
        st = S.summarise(vals)
        st["n_records"] = len(verdicts)
        st["n_missing"] = len(verdicts) - len(vals)
        out[f] = st
    out["_source"] = "upstream-eval/harness.py:118-132（字段口径）+ :109-117（工具去重）"
    out["_missing_policy"] = "缺失即 null（宁缺勿造，不补 0；docs/06 §4 P11）"
    return out


# ── 配对比较辅助 ────────────────────────────────────────────────────────────

def _subset(verdicts, kind, value):
    if kind is None:
        return verdicts
    if kind == "difficulty":
        return [v for v in verdicts if v["difficulty"] == value]
    if kind == "family":
        return [v for v in verdicts if v["family"] == value]
    raise ValueError(kind)


def _aligned(va_list, vb_list, value_fn):
    """按 (case_id, repeat) 对齐取值（docs/06 §6.1 的配对单元）。"""
    ia = {(v["case_id"], v["repeat"]): v for v in va_list}
    ib = {(v["case_id"], v["repeat"]): v for v in vb_list}
    keys = sorted(set(ia) & set(ib))
    out_a, out_b, dropped = [], [], 0
    for k in keys:
        x, y = value_fn(ia[k]), value_fn(ib[k])
        if x is None or y is None:
            dropped += 1
            continue
        out_a.append(x)
        out_b.append(y)
    n_only_a = sum(1 for k in ia if k not in ib)
    n_only_b = sum(1 for k in ib if k not in ia)
    return out_a, out_b, dropped, n_only_a, n_only_b


def _paired_binary_cmp(va, vb, *, metric, alpha=S.ALPHA,
                       dropped=0, only_a=0, only_b=0, with_bootstrap=True):
    base = {"metric": metric, "test_form": "paired", "alpha": alpha}
    n = len(va)
    if n == 0:
        base.update({"status": "BLOCKED", "n_pairs": 0, "n_only_a": only_a,
                     "n_only_b": only_b, "dropped_inapplicable_pairs": dropped,
                     "note": "配对子集为空 ⇒ 配对检验不可算（BLOCKED，不是 FAIL）"})
        return base
    b = sum(1 for x, y in zip(va, vb) if x == 1 and y == 0)
    c = sum(1 for x, y in zip(va, vb) if x == 0 and y == 1)
    both_hit = sum(1 for x, y in zip(va, vb) if x == 1 and y == 1)
    both_miss = sum(1 for x, y in zip(va, vb) if x == 0 and y == 0)
    mc = S.mcnemar_exact(b, c)
    rate_a = sum(va) / n
    rate_b = sum(vb) / n
    boot = (S.paired_bootstrap_ci(va, vb) if with_bootstrap
            else {"status": "SKIPPED", "note": "非主对比的非总体层 ⇒ 省下 bootstrap（可复现性不受影响）"})
    h = S.cohens_h(rate_a, rate_b)
    base.update({
        "status": "PASS", "n_pairs": n, "n_only_a": only_a, "n_only_b": only_b,
        "dropped_inapplicable_pairs": dropped,
        "rate_a_paired": round(rate_a, 6), "rate_b_paired": round(rate_b, 6),
        "diff_paired": round(rate_a - rate_b, 6),
        "diff_pp_paired": round((rate_a - rate_b) * 100, 4),
        "b": mc["b"], "c": mc["c"], "n_discordant": mc["n_discordant"],
        "both_hit": both_hit, "both_miss": both_miss,
        "p_value": mc["p_value"], "p_method": mc["p_method"],
        "p_chi2_reference": mc.get("p_chi2_reference"),
        "cohens_h": round(h, 6), "cohens_h_magnitude": S.cohens_h_magnitude(h),
        "ci95_pp": ([round(boot["ci95_low"] * 100, 4), round(boot["ci95_high"] * 100, 4)]
                    if boot.get("status") == "PASS" else [None, None]),
        "ci95_excludes_zero": boot.get("excludes_zero"),
        "bootstrap": {k: boot[k] for k in ("B", "seed", "method", "n_pairs") if k in boot},
    })
    return base


def _comparison_for(metric, va_list, vb_list, *, binary, with_bootstrap=True,
                    abstain_as_error=True):
    """★ 主口径（``abstain_as_error=True``）：abstain 计未命中（0.0），配对不再因 abstain 被丢弃。

    ``abstain_as_error=False`` 只用于敏感性对照（旧口径：丢弃任一侧 abstain 的配对）。
    """
    if metric == "mixed_precision":
        value_fn = lambda v: (None if v["abstain"] else v.get("mixed_precision"))  # noqa: E731
    elif binary:
        if abstain_as_error:
            value_fn = lambda v: (0.0 if v.get(metric) is None  # noqa: E731
                                  else (1.0 if v[metric] else 0.0))
        else:
            value_fn = lambda v: (None if v.get(metric) is None  # noqa: E731
                                  else (1.0 if v[metric] else 0.0))
    else:
        value_fn = lambda v: v.get(metric)  # noqa: E731
    aa, bb, dropped, oa, ob = _aligned(va_list, vb_list, value_fn)
    if binary:
        cmp_ = _paired_binary_cmp(aa, bb, metric=metric, dropped=dropped,
                                  only_a=oa, only_b=ob, with_bootstrap=with_bootstrap)
        cmp_["abstain_as_error"] = bool(abstain_as_error)
        return cmp_
    cmp_ = S.compare_continuous(aa, bb, label=metric)
    cmp_["dropped_inapplicable_pairs"] = dropped
    cmp_["n_only_a"], cmp_["n_only_b"] = oa, ob
    if cmp_.get("status") == "PASS":
        w = cmp_.get("wilcoxon") or {}
        cmp_["p_value"] = w.get("p_value")
        cmp_["p_method"] = w.get("p_method")
        ci = cmp_.get("ci95")
        cmp_["ci95_pp"] = ([round(ci[0] * 100, 4), round(ci[1] * 100, 4)]
                           if (ci and ci[0] is not None) else [None, None])
        cmp_["diff_pp_paired"] = round((cmp_["mean_a"] - cmp_["mean_b"]) * 100, 4)
    else:
        cmp_["p_value"] = None
        cmp_["p_method"] = None
        cmp_["ci95_pp"] = [None, None]
        cmp_["diff_pp_paired"] = None
    return cmp_


# ── metrics / stats 装配 ────────────────────────────────────────────────────

def build_metrics(*, cases, records_by_group, groups_order, cases_path, answers_path,
                  cases_meta, errors, warnings, stratum_counts, answers_sha,
                  records_total, groups_info, vocab, generated_at,
                  coverage=None, policy_info=None):
    verdicts_by_group = {}
    for g in groups_order:
        vs = []
        for r in records_by_group[g]:
            # ★ 2026-10-09：容器侧三字段允许**臂级回退**（审计链无 case_id ⇒ 只能臂级）
            v = J.judge_record(r, cases, vocab, carrier=_CARRIER.get(r.get("group")))
            v["_any_fault"] = (None if v["abstain"]
                               else bool(v["root_cause"]["first_candidate"]))
            r["_verdict"] = v
            for k in ("_hit", "_layer_hit", "_object_all", "_object_any",
                      "_evidence_hit", "_false_claim_any", "_path_f1"):
                r[k] = v[k]
            r["_case_score"] = v["case_score"]
            r["_any_fault"] = v["_any_fault"]
            vs.append(v)
        verdicts_by_group[g] = vs

    groups_doc = {}
    for g in groups_order:
        vs = verdicts_by_group[g]
        by_d = {}
        for d in C.DIFFICULTY_ORDER:
            sub = [v for v in vs if v["difficulty"] == d]
            by_d[d] = {"n_cases": stratum_counts["difficulty"].get(d, 0),
                       "n_records": len(sub),
                       "metrics": _metrics_for(sub, n_cases=stratum_counts["difficulty"].get(d, 0))}
        by_f = {}
        for f in C.FAMILY_ORDER:
            sub = [v for v in vs if v["family"] == f]
            by_f[f] = {"n_cases": stratum_counts["family"].get(f, 0),
                       "n_records": len(sub),
                       "metrics": _metrics_for(sub, n_cases=stratum_counts["family"].get(f, 0))}
        groups_doc[g] = {
            "records": len(vs), "n_cases": len({v["case_id"] for v in vs}),
            "abstain_n": sum(1 for v in vs if v["abstain"]),
            "repeats": sorted({v["repeat"] for v in vs}),
            "metrics": _metrics_for(vs, n_cases=stratum_counts["total"]),
            "by_difficulty": by_d, "by_family": by_f,
            "efficiency": _efficiency_for(vs),
        }

    metrics = {
        "schema": "m5.metrics/1",
        "generated_by": "shared/verify/m5_judge.py",
        "package_version": PKG_VERSION,
        "generated_at_utc": generated_at,
        "groups_order": list(groups_order),
        "primary_contrast": list(groups_order[:2]) if len(groups_order) >= 2 else None,
        "families_order": list(C.FAMILY_ORDER),
        "difficulty_order": list(C.DIFFICULTY_ORDER),
        "denominators": {
            "cases_total": stratum_counts["total"],
            "cases_by_difficulty": stratum_counts["difficulty"],
            "cases_by_family": stratum_counts["family"],
            "repeats_by_group": {g: groups_doc[g]["repeats"] for g in groups_order},
            "records_total": records_total,
            "policy": ("★ 主口径（队长裁定 2026-09-29；契约 scoring.abstain）：分母固定为用例集规模"
                       "（每条 repeat = 用例数），abstain 计未命中（既不进分子，也不缩小分母）；"
                       "缺记录同样计未命中但单列 n_missing，绝不静默剔除；"
                       "旧「可用记录数」口径降为诊断列 rate_excluding_abstain / "
                       "denominator_excluding_abstain（仅供参考，不是主口径）"),
        },
        "denominator_policy": dict(policy_info or {}),
        "records_coverage": dict(coverage or {}),
        "inputs": {
            "cases": {"path": str(cases_path), "sha256": C.sha256_file(cases_path),
                      "n_cases": len(cases),
                      "synthetic": bool(cases_meta.get("synthetic"))},
            "answers": {"path": str(answers_path), "sha256": answers_sha,
                        "n_records": records_total, "groups": list(groups_order),
                        "groups_info": groups_info},
            "cases_meta": {k: v for k, v in (cases_meta or {}).items()},
        },
        "validation": {"status": "PASS" if not errors else "FAIL",
                       "errors": list(errors), "warnings": list(warnings)},
        "groups": groups_doc,
    }
    return metrics, verdicts_by_group


def build_stats(*, verdicts_by_group, groups_order, cases_total, stratum_counts,
                generated_at):
    comparisons = {}
    records_view = {g: vs for g, vs in verdicts_by_group.items()}

    def add(metric, a, b, cmp_):
        comparisons.setdefault(metric, {})[f"{a}|{b}"] = cmp_

    strata = ([("", None, None)]
              + [(f"[{d}]", "difficulty", d) for d in C.DIFFICULTY_ORDER]
              + [(f"[{f}]", "family", f) for f in C.FAMILY_ORDER])
    pairs = [(groups_order[i], groups_order[j])
             for i in range(len(groups_order))
             for j in range(i + 1, len(groups_order))]
    pa0, pb0 = (list(groups_order) + [None, None])[:2]

    # 二值 + 连续指标的配对比较（全样本 + 分层 + 分族）
    # bootstrap 只在「总体层」或「主对比的根因分层」上算，控制运行时间（可复现性不受影响）
    for base, flag, is_binary in DISC_METRICS:
        if base == "abstain_rate":
            flag, is_binary = "abstain", True
        for suffix, kind, value in strata:
            metric = base + suffix
            for a, b in pairs:
                need_boot = (kind is None) or (
                    base == "root_cause_accuracy" and (a, b) == (pa0, pb0))
                sa = _subset(records_view[a], kind, value)
                sb = _subset(records_view[b], kind, value)
                add(metric, a, b, _comparison_for(flag, sa, sb, binary=is_binary,
                                                  with_bootstrap=need_boot))

    # 内部一致性交叉验证：本机的配对二值比较 vs stats.compare_binary（读 _hit）
    # ★ 主口径 abstain_as_error=True（abstain 计未命中，配对不丢）；旧口径作敏感性对照
    cross = []
    for a, b in pairs:
        mine = comparisons["root_cause_accuracy"][f"{a}|{b}"]
        ref = S.compare_binary(records_view[a], records_view[b], label="root_cause_accuracy",
                               abstain_as_error=True)
        sens = S.compare_binary(records_view[a], records_view[b], label="root_cause_accuracy",
                                abstain_as_error=False)
        same = (mine.get("status") == ref.get("status")
                and mine.get("b") == ref.get("b") and mine.get("c") == ref.get("c")
                and mine.get("n_pairs") == ref.get("n_pairs"))
        triple_main = (ref.get("n_pairs"), ref.get("b"), ref.get("c"))
        triple_sens = (sens.get("n_pairs"), sens.get("b"), sens.get("c"))
        cross.append({"pair": f"{a}|{b}",
                      "abstain_as_error": True,
                      "mine": {k: mine.get(k) for k in
                               ("status", "n_pairs", "b", "c", "p_value")},
                      "compare_binary": {k: ref.get(k) for k in
                                         ("status", "n_pairs", "b", "c", "p_value")},
                      "sensitivity_dropped_abstain": {
                          k: sens.get(k) for k in ("status", "n_pairs", "b", "c", "p_value")},
                      "sensitivity_differs": bool(triple_main != triple_sens),
                      "agree": bool(same)})

    # 独立样本对照（★ 只作参考，绝不进结论）
    pairing = {
        "test_form": "paired",
        "unit": "(case_id, repeat)",
        "methods": {"binary": "McNemar exact（不和谐对二项精确检验）",
                    "continuous": "Wilcoxon 符号秩（并列/大样本用正态近似+并列校正）",
                    "interval": f"paired percentile bootstrap B={S.BOOTSTRAP_B} seed={S.BOOTSTRAP_SEED}",
                    "reference_alpha": f"Bonferroni 6 族 α={S.BONFERRONI_ALPHA:.6f}"},
        "contract_source": "docs/06 §6.1（配对样本）/ §6.2（检验与效应量）",
        "abstain_policy": ABSTAIN_DEF,
        "abstain_main_axis": {"abstain_as_error": True,
                              "denominator": "固定分母（用例数 × repeat 数）",
                              "note": ("主口径由队长 2026-09-29 裁定；"
                                       "internal_cross_check.sensitivity_dropped_abstain "
                                       "是旧口径（丢弃 abstain 配对）的敏感性对照")},
        "independent_reference": [],
        "independent_reference_note": ("独立样本口径（两比例 z 检验）**只作对照**，"
                                       "用于证明没有把配对数据当独立样本检验；"
                                       "**绝不**进入任何结论字段。"),
        "internal_cross_check": cross,
    }
    for a, b in pairs:
        mine = comparisons["root_cause_accuracy"][f"{a}|{b}"]
        sa, sb = records_view[a], records_view[b]
        ha = sum(1 for r in sa if r.get("_hit") is True)
        na = sum(1 for r in sa if r.get("_hit") is not None)
        hb = sum(1 for r in sb if r.get("_hit") is True)
        nb = sum(1 for r in sb if r.get("_hit") is not None)
        ref = S.independent_two_proportion_reference(ha, na, hb, nb)
        pairing["independent_reference"].append({
            "metric": "root_cause_accuracy", "pair": f"{a}|{b}",
            "paired_p_value": mine.get("p_value"), "paired_method": mine.get("p_method"),
            "independent_p_value": ref.get("p_value"),
            "independent_method": ref.get("method"), "independent_status": ref.get("status"),
            # ★ 只有在独立口径**真的算出** p 值时才比较；参考实现退化（BLOCKED / 无 p）时
            #   比较无意义 ⇒ differ=None（不可比），不得凭 None 报出「口径不同」的伪结论。
            "differ": (None if ref.get("p_value") is None
                       else bool(mine.get("p_value") != ref.get("p_value"))),
        })

    # 六族 Bonferroni（docs/06 §6.2）
    pa, pb = (groups_order + [None, None])[:2]
    bon_rows = []
    if pa and pb:
        for fam in C.FAMILY_ORDER:
            cmp_ = comparisons.get(f"root_cause_accuracy[{fam}]", {}).get(f"{pa}|{pb}", {})
            bon_rows.append({"family": fam, "metric": "root_cause_accuracy",
                             "group_a": pa, "group_b": pb,
                             "n_pairs": cmp_.get("n_pairs"), "b": cmp_.get("b"),
                             "c": cmp_.get("c"), "p_value": cmp_.get("p_value"),
                             "ci95_pp": cmp_.get("ci95_pp"),
                             "diff_pp_paired": cmp_.get("diff_pp_paired"),
                             "status": cmp_.get("status", "BLOCKED")})
    bonferroni = S.bonferroni_table(bon_rows)

    # 样本量纪律（★ 任务书核心）
    per_comp = []
    if pa and pb:
        for metric, flag, _is_bin in DISC_METRICS:
            cmp_ = comparisons.get(metric, {}).get(f"{pa}|{pb}", {})
            cell_a = _metric_rate_in_metrics(metric, pa)
            cell_b = _metric_rate_in_metrics(metric, pb)
            entry = S.build_discipline(
                metric=metric, group_a=pa, group_b=pb,
                rate_a=cell_a, rate_b=cell_b,
                ci95_pp=cmp_.get("ci95_pp"), p_value=cmp_.get("p_value"),
                p_method=cmp_.get("p_method"), n_pairs=cmp_.get("n_pairs") or 0,
                alpha=S.ALPHA, ci_excludes_zero=cmp_.get("ci95_excludes_zero"))
            entry["paired_vs_full"] = {
                "diff_pp_full": entry.get("diff_pp"),
                "diff_pp_paired": cmp_.get("diff_pp_paired"),
                "same_sign": _same_sign(entry.get("diff_pp"), cmp_.get("diff_pp_paired")),
                "n_pairs": cmp_.get("n_pairs"),
                "n_only_a": cmp_.get("n_only_a"), "n_only_b": cmp_.get("n_only_b"),
            }
            entry["test_form"] = "paired"
            # 顶层再放一份配对子集差值，供 report.md 的「配对 Δ(pp)」列直接读取。
            entry["diff_pp_paired"] = cmp_.get("diff_pp_paired")
            entry["comparison_status"] = cmp_.get("status", "BLOCKED")
            if entry["conclusion"] == S.SIGNIFICANT_LABEL and \
                    not entry["paired_vs_full"]["same_sign"]:
                entry["conclusion"] = S.MANDATED_NO_DIFF_LABEL
                entry["conclusion_reason"] += ("；★ 配对子集差值符号与全样本不一致 ⇒ "
                                               "按保守口径降级为不可下结论")
            if cmp_.get("status") != "PASS":
                entry["status"] = "BLOCKED"
                entry["conclusion"] = S.MANDATED_NO_DIFF_LABEL
                entry["conclusion_reason"] = ("配对子集为空或不可算 ⇒ BLOCKED（不是 FAIL）；"
                                              "本样本量下不可下结论")
            per_comp.append(entry)

    stats_doc = {
        "schema": "m5.stats/1",
        "generated_by": "shared/verify/m5_judge.py",
        "package_version": PKG_VERSION,
        "generated_at_utc": generated_at,
        "bootstrap": {"B": S.BOOTSTRAP_B, "seed": S.BOOTSTRAP_SEED,
                      "method": "paired_percentile_bootstrap",
                      "note": "固定种子 ⇒ 逐位可复现（docs/06 §6.2）"},
        "alpha": S.ALPHA,
        "primary_contrast": [pa, pb] if (pa and pb) else None,
        "comparisons": comparisons,
        "bonferroni": bonferroni,
        "pairing": pairing,
        "conclusion": {
            "discipline": {"policy": S.discipline_policy_block(),
                           "per_comparison": per_comp},
            "sample_size": S.sample_size_note(),
        },
    }
    return stats_doc


def _metric_rate_in_metrics(metric, group):
    """从 metrics 文档里取该组该指标的 rate（供纪律表对齐报告表）。"""
    return _METRICS_CACHE[group]["metrics"][metric].get("rate")


_METRICS_CACHE: dict = {}


def _same_sign(x, y):
    if x is None or y is None:
        return True
    return (x >= 0) == (y >= 0)


# ── G6 区分度（docs/06 §1.4 步骤 4 / §5 G6）──────────────────────────────────
# ★ 「哪些组对可以判作废」与文案常量唯一定义在 m5/judge.py（J.G6_* / J.g6_designated），
#   此处只做汇总，避免该口径在两处漂移。

G6_ORACLE_ALIASES = J.G6_ORACLE_ALIASES
G6_WRONG_ALIASES = J.G6_WRONG_ALIASES
G6_GATE_LABEL = J.G6_GATE_LABEL
G6_OK_LABEL = J.G6_OK_LABEL
G6_NA_LABEL = J.G6_NA_LABEL
_g6_designated = J.g6_designated


def g6_discrimination(verdicts_by_group, groups_order):
    out = []
    for i in range(len(groups_order)):
        for j in range(i + 1, len(groups_order)):
            a, b = groups_order[i], groups_order[j]
            ma = {(v["case_id"], v["repeat"]): v for v in verdicts_by_group[a]}
            mb = {(v["case_id"], v["repeat"]): v for v in verdicts_by_group[b]}
            keys = sorted(set(ma) & set(mb))
            gaps, zero_cases = [], []
            for k in keys:
                sa, sb = ma[k]["case_score"], mb[k]["case_score"]
                if sa is None or sb is None:
                    continue
                gaps.append(sa - sb)
                if abs(sa - sb) == 0:
                    zero_cases.append({"case_id": k[0], "repeat": k[1],
                                       "score_a": sa, "score_b": sb})
            applicable = _g6_designated(a, b)
            entry = {"group_a": a, "group_b": b, "n_pairs_scored": len(gaps),
                     "g6_applicable": applicable,
                     "mean_gap": round(sum(gaps) / len(gaps), 6) if gaps else None,
                     "min_gap": round(min(gaps), 6) if gaps else None,
                     "max_gap": round(max(gaps), 6) if gaps else None,
                     "n_zero_discrimination": len(zero_cases),
                     "zero_discrimination_cases": zero_cases[:20],
                     "status": "PASS" if gaps else "BLOCKED"}
            if not gaps:
                entry["g6_verdict"] = "不可算（BLOCKED）"
            elif not applicable:
                entry["g6_verdict"] = G6_NA_LABEL
            elif entry["n_zero_discrimination"]:
                entry["g6_verdict"] = G6_GATE_LABEL
            else:
                entry["g6_verdict"] = G6_OK_LABEL
            out.append(entry)
    return {
        "definition": ("case_score = mean(root_cause_hit, layer_hit, path_f1, object_hit_all,"
                       " 1−false_claim_any[, evidence_hit])；abstain 记录不计（None）"),
        "contract_source": "docs/06 §1.4 步骤 4（反例校验 G6）/ §5 G6",
        "g6_oracle_aliases": list(G6_ORACLE_ALIASES),
        "g6_wrong_aliases": list(G6_WRONG_ALIASES),
        "gate_semantics": ("「作废」判定**只**对 g6_applicable=True 的组对生效；"
                           "其余组对同为信息读数，不得据此作废用例。"),
        "note": ("G6 正式执行需要喂入「标准答案」与「错误答案」两组；本表对每一对组都给出"
                 "区分度读数，但只在 g6_applicable=True 时给出 G6 通过/作废结论。"),
        "pairs": out,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────

_VOCAB = {"codes": [], "normalised": {}}


def _as_dict(x):
    """把 Issue / str / dict 统一成 dict（便于调用方按 code 断言）。"""
    if isinstance(x, dict):
        return x
    if hasattr(x, "as_dict"):
        try:
            return x.as_dict()
        except Exception:  # pragma: no cover
            pass
    return {"code": "E-UNKNOWN", "message": str(x)}


# ── ★ (c) 跨件一致性断言：分母 / abstain 主口径 ────────────────────────────
# 「排除性」措辞 = 该声明仍把「可用记录数 / 扣除 abstain」当成主口径（旧口径）；
# 「固定」措辞 = 声明了固定分母。判据只扫 `scoring.abstain.main_axis` 的三个键
# （denominator / treatment / note），不扫 sensitivity 与历史注记，避免把敏感性对照误判成主口径。
_EXCLUSIONARY_RES = (
    re.compile(r"可用(记录数|用例数)"),
    re.compile(r"(扣除|减去|剔除)[^。；]{0,12}abstain"),
    re.compile(r"abstain[^。；]{0,12}(扣除|减去|剔除)"),
)
# 否定语境守卫：正确的主口径声明常写成「不缩小分母 / 不计错 / 不剔除」，
# 直接匹配「缩小分母」之类会把它误判成排除性措辞（真实误报，已由 N19 正控捕获）。
_NEG_BEFORE_RE = re.compile(r"[不未无非]$")
_FIXED_MARK_RE = re.compile(r"固定|恒为|始终为|分母为用例数|分母都是用")


def _exclusionary_hit(txt):
    """返回第一个**非否定语境**的「可用记录数 / 扣除 abstain」措辞；无则 ``None``。"""
    for rx in _EXCLUSIONARY_RES:
        for m in rx.finditer(txt or ""):
            pre = (txt or "")[max(0, m.start() - 6):m.start()]
            if _NEG_BEFORE_RE.search(pre) or "不" in pre:
                continue
            return m
    return None

DENOM_POLICY_SOURCES = (
    ("suite.fixed_denominator.value", "suite", "fixed_denominator", "value"),
    ("scoring.denominator.policy", "scoring", "denominator", "policy"),
    ("scoring.denominator.value", "scoring", "denominator", "value"),
    ("scoring.abstain.main_axis.denominator", "scoring.abstain", "main_axis", "denominator"),
)


def default_contract_path() -> Path:
    """注册契约的规范路径（``shared/model/m5-cases.yaml``）。"""
    return Path(__file__).resolve().parents[2] / "shared" / "model" / "m5-cases.yaml"


def _declared_suite_total(doc) -> "int | None":
    total = ((doc or {}).get("suite") or {}).get("total") if isinstance(doc, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def check_denominator_policy(doc, cases=None, meta=None, *, source="",
                             expected_n_cases=None, require_declared=True):
    """跨件一致性断言：(c) 判据器主口径 vs 契约的三处「分母/abstain」声明。

    覆盖 `shared/model/m5-cases.yaml` 的
      1. ``suite.fixed_denominator.value``（固定分母 = 用例集规模）、
      2. ``scoring.denominator.policy`` == ``fixed`` 且 ``value`` 与 (1) 相同、
      3. ``scoring.abstain.main_axis``（主口径必须 = **固定分母 + abstain 计未命中**）。

    任一仍把「可用记录数」写成主口径 ⇒ ``E-DENOM-POLICY``（fail-closed）。
    ``require_declared=True``（对**注册契约**）：三处声明整体缺失 ⇒ ``E-DENOM-POLICY``；
    ``require_declared=False``（对**用例文件**）：不声明口径不算错（口径权威在契约），
    但只要声明了就必须自洽且与本次用例数一致。
    参考用例数取 ``expected_n_cases``，缺省 = ``len(cases)``。
    返回 ``(errors, warnings, info)``。
    """
    errors: list = []
    warnings: list = []
    doc = doc if isinstance(doc, dict) else {}
    meta = meta if isinstance(meta, dict) else {}

    def _sub(*keys):
        cur = doc
        for k in keys:
            if not isinstance(cur, dict):
                return {}
            cur = cur.get(k)
        return cur if isinstance(cur, dict) else {}

    suite_fd = _sub("suite", "fixed_denominator")
    scoring_den = _sub("scoring", "denominator")
    abstain = _sub("scoring", "abstain")
    main_axis = _sub("scoring", "abstain", "main_axis")
    synthetic = bool(meta.get("synthetic"))
    n_cases = expected_n_cases if expected_n_cases is not None else len(cases or [])
    declared = bool(suite_fd or scoring_den or abstain)
    info = {
        "source": source,
        "declared": declared,
        "required": bool(require_declared),
        "synthetic_fixture": synthetic,
        "reference_n_cases": n_cases,
        "suite_fixed_denominator": suite_fd.get("value"),
        "suite_total": _declared_suite_total(doc),
        "scoring_denominator": {"policy": scoring_den.get("policy"),
                                "value": scoring_den.get("value")},
        "abstain_main_denominator": main_axis.get("denominator"),
        "abstain_main_treatment": main_axis.get("treatment"),
        "main_policy": ("fixed(declared)" if declared
                        else ("fixed(contract-authoritative)" if not require_declared
                              else ("fixed(synthetic-default)" if synthetic else "undeclared"))),
        "assertion": ("主口径 = 固定分母 + abstain 计未命中（队长裁定 2026-09-29）；"
                      "判据器实现见 shared/verify/m5_judge.py ABSTAIN_DEF"),
    }
    if not declared:
        if synthetic:
            info["note"] = ("合成夹具未声明分母/abstain 口径 ⇒ 判据器按构造口径落地"
                            "（固定分母 = 夹具用例数），不算不一致。")
            return errors, warnings, info
        if require_declared:
            errors.append(C.Issue(
                "E-DENOM-POLICY",
                "用例契约缺少分母/abstain 主口径声明（suite.fixed_denominator / "
                "scoring.denominator / scoring.abstain.main_axis 全缺）⇒ 判据器主口径无据可依"))
        else:
            info["note"] = ("用例文件未自带口径声明 ⇒ 口径权威取注册契约 "
                            "shared/model/m5-cases.yaml（本项不报错）。")
        return errors, warnings, info

    # ① 固定分母值 == 参考用例数
    if suite_fd:
        v = suite_fd.get("value")
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(C.Issue("E-DENOM-POLICY",
                                  f"suite.fixed_denominator.value 必须是整数，实测 {v!r}"))
        elif v != n_cases:
            errors.append(C.Issue(
                "E-DENOM-POLICY",
                f"suite.fixed_denominator.value={v} 与参考用例数 {n_cases} 不一致"))
    # ② scoring.denominator 也必须是 fixed，且与 ① 同值
    if scoring_den:
        pol = str(scoring_den.get("policy") or "")
        if pol and pol != "fixed":
            errors.append(C.Issue(
                "E-DENOM-POLICY",
                f"scoring.denominator.policy={pol!r} 不是 'fixed'（主口径必须是固定分母）"))
        sv = scoring_den.get("value")
        if (isinstance(sv, int) and not isinstance(sv, bool)
                and isinstance(suite_fd.get("value"), int) and sv != suite_fd.get("value")):
            errors.append(C.Issue(
                "E-DENOM-POLICY",
                f"scoring.denominator.value={sv} 与 suite.fixed_denominator.value="
                f"{suite_fd.get('value')} 不一致"))
    # ③ abstain 主口径不得仍是「可用记录数」
    if abstain:
        if not main_axis:
            errors.append(C.Issue("E-DENOM-POLICY",
                                  "scoring.abstain 缺 main_axis（主口径无处声明）"))
        else:
            txt = " ".join(str(main_axis.get(k) or "")
                           for k in ("denominator", "treatment", "note"))
            hit = _exclusionary_hit(txt)
            if hit:
                errors.append(C.Issue(
                    "E-DENOM-POLICY",
                    "scoring.abstain.main_axis 的主口径仍被写成「可用记录数 / 扣除 abstain」"
                    f"（排除性措辞命中 {hit.group(0)!r}）⇒ 主口径必须是固定分母 + abstain 计未命中"))
            elif not _FIXED_MARK_RE.search(txt):
                errors.append(C.Issue(
                    "E-DENOM-POLICY-ABSTAIN-UNSTATED",
                    "scoring.abstain.main_axis 未声明「固定分母」（既无排除性措辞也无固定措辞）"))
    return errors, warnings, info


def _coverage_for(cases, records_by_group, groups_order):
    """★ (d) 「记录缺失」与「存在但 abstain」分列，两者都不静默剔除。"""
    ids = [c["id"] for c in cases]
    out = {}
    for g in groups_order:
        recs = records_by_group.get(g) or []
        reps = sorted({r["repeat"] for r in recs})
        n_repeats = len(reps)
        n_expected = len(ids) * n_repeats
        seen = {r["case_id"] for r in recs}
        missing_cases = [i for i in ids if i not in seen]
        out[g] = {
            "n_cases": len(ids), "n_repeats": n_repeats, "repeats": reps,
            "n_expected": n_expected, "n_records": len(recs),
            "n_missing": max(0, n_expected - len(recs)),
            "n_abstain": sum(1 for r in recs if r.get("abstain")),
            "cases_without_record": missing_cases,
            "policy": ("缺记录计未命中（保守方向），但与 abstain 分列（n_missing / n_abstain），"
                       "绝不静默剔除；两者都在各 metrics 单元与本节可见"),
        }
    return out


def run_pipeline(cases_path, answers_path, out_dir, *, groups=None, expect_groups=None,
                 strict=False, expect_full_suite=False, generated_at=None, quiet=False,
                 diag=None):
    """离线跑一次判决；返回退出码（0 通过 / 1 fail-closed / 2 无 FAIL 但有 BLOCKED）。

    ``diag``（可选 dict）会被填入 ``errors`` / ``warnings`` / ``out_dir`` / ``stage``，
      供 ``--selftest`` 对 fail-closed 的**具体 ERROR 码**做断言。
    """
    def _fail(stage, errs, warns):
        errs = [_as_dict(e) for e in errs]
        warns = [_as_dict(w) for w in warns]
        if diag is not None:
            diag.update({"stage": stage, "errors": errs, "warnings": warns,
                         "out_dir": str(out_dir)})
        _emit_fail(stage, errs, warns, quiet)
        return 1

    cases_path = Path(cases_path)
    answers_path = Path(answers_path)
    out_dir = Path(out_dir)
    generated_at = generated_at or os.environ.get("M5_FIXED_TS") or \
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    raw_cases, cases_meta, c_err = C.load_case_document(cases_path)
    cases, errors, warnings = C.validate_cases(raw_cases, cases_meta,
                                               expect_full_suite=expect_full_suite,
                                               strict=strict)
    errors = list(c_err) + list(errors)
    # ★ (c) 跨件一致性断言：契约里声明的分母/abstain 主口径必须与判据器主口径一致。
    #   权威 = 注册契约 shared/model/m5-cases.yaml（规格文档，不是用例清单 ⇒ 单独读）；
    #   用例文件自带的声明只作自洽校验（不声明不算错：口径权威在契约）。
    doc, d_err = C.load_document(cases_path)
    errors += d_err
    pol_err, pol_warn, case_pol = check_denominator_policy(
        doc, cases, cases_meta, source=str(cases_path), require_declared=False)
    errors += pol_err
    warnings += pol_warn
    contract_path = default_contract_path()
    if contract_path.exists():
        cdoc, c_err = C.load_document(contract_path)
        errors += c_err
        declared_total = _declared_suite_total(cdoc)
        c_meta = cdoc.get("meta") if isinstance(cdoc, dict) else {}
        c_pol_err, c_pol_warn, contract_pol = check_denominator_policy(
            cdoc, None, c_meta, source=str(contract_path),
            expected_n_cases=declared_total, require_declared=True)
        errors += c_pol_err
        warnings += c_pol_warn
        contract_pol["declared_suite_total"] = declared_total
        contract_pol["runtime_n_cases"] = len(cases)
        contract_pol["runtime_matches_contract"] = (
            None if declared_total is None else len(cases) == declared_total)
        if declared_total is not None and len(cases) != declared_total and expect_full_suite:
            errors.append(C.Issue(
                "E-DENOM-POLICY",
                f"--expect-full-suite：本次用例数 {len(cases)} != 契约 suite.total "
                f"{declared_total} ⇒ 固定分母无据可依"))
    else:
        contract_pol = {"source": str(contract_path), "status": "ABSENT",
                        "main_policy": "fixed(contract-absent)"}
        warnings.append(C.Issue(
            "W-DENOM-CONTRACT-ABSENT",
            f"未找到注册契约 {contract_path} ⇒ 主口径只能由判据器自身保证（不静默）"))
    policy_info = {
        "case_document": case_pol,
        "contract": contract_pol,
        "main_policy": "fixed+abstain-as-miss",
        "main_policy_note": ("判据器实现口径（本文件 ABSTAIN_DEF）；契约三处声明见 contract.*，"
                             "用例文件自带声明见 case_document.*"),
        "authority": "shared/model/m5-cases.yaml",
        "ruling": "队长裁定 2026-09-29：主口径 = 固定分母（每条 repeat = 用例数）、abstain 计未命中",
    }
    records, a_err, a_warn = C.load_answers(answers_path)
    errors += a_err
    warnings += a_warn
    if errors:
        return _fail("cases/answers 校验", errors, warnings)

    kept, errors2, warnings2, info = C.validate_answers(
        records, cases, strict=strict, groups_filter=groups, expect_groups=expect_groups)
    errors += errors2
    warnings += warnings2
    if errors:
        return _fail("answers 校验", errors, warnings)
    if strict and warnings:
        return _fail("--strict：WARN 视为 ERROR", warnings, [])

    groups_order = list(info["groups"])
    if len(groups_order) < 1:
        return _fail("分组", [{"code": "E-ANS-NO-GROUP",
                              "message": "答案文件里没有可用分组"}], warnings)

    # 词表（闭集：全部 expect_root_cause ∪ injected ∪ expect_path）
    vocab = J.build_vocabulary(cases)

    records_by_group = {g: [r for r in kept if r["group"] == g] for g in groups_order}
    # ★ (d) 记录缺失 vs abstain：分列报告，缺记录不静默（--strict 下由既有的「WARN 视为 ERROR」通道升级）
    coverage = _coverage_for(cases, records_by_group, groups_order)
    for g in groups_order:
        n_miss = coverage[g]["n_missing"]
        if n_miss > 0:
            warnings.append(C.Issue(
                "W-ANS-RECORD-MISSING",
                f"组 {g}: 应有 {coverage[g]['n_expected']} 条记录，实得 "
                f"{coverage[g]['n_records']} 条（缺 {n_miss} 条；缺记录用例 "
                f"{coverage[g]['cases_without_record'][:10]}）。固定分母下缺记录计未命中，"
                "但绝不静默剔除（见 metrics.records_coverage 与各 metrics 单元的 n_missing）"))
    if strict and warnings:
        return _fail("--strict：WARN 视为 ERROR（含缺记录）", warnings, [])
    stratum_counts = {
        "total": len(cases),
        "difficulty": {d: sum(1 for c in cases if c["difficulty"] == d)
                       for d in C.DIFFICULTY_ORDER},
        "family": {f: sum(1 for c in cases if c["_family"] == f) for f in C.FAMILY_ORDER},
    }
    answers_sha = C.sha256_file(answers_path)
    metrics, verdicts_by_group = build_metrics(
        cases=cases, records_by_group=records_by_group, groups_order=groups_order,
        cases_path=cases_path, answers_path=answers_path, cases_meta=cases_meta,
        errors=[_as_dict(e) for e in errors], warnings=[_as_dict(w) for w in warnings],
        stratum_counts=stratum_counts,
        answers_sha=answers_sha, records_total=len(kept), groups_info=info,
        vocab=vocab, generated_at=generated_at,
        coverage=coverage, policy_info=policy_info)
    _METRICS_CACHE.clear()
    _METRICS_CACHE.update(metrics["groups"])
    stats_doc = build_stats(verdicts_by_group=verdicts_by_group,
                            groups_order=groups_order, cases_total=len(cases),
                            stratum_counts=stratum_counts, generated_at=generated_at)
    metrics["g6_discrimination"] = g6_discrimination(verdicts_by_group, groups_order)

    pa, pb = (groups_order + [None, None])[:2]
    primary_cmp = (stats_doc["comparisons"].get("root_cause_accuracy", {})
                   .get(f"{pa}|{pb}", {})) if (pa and pb) else {}
    sections = {
        "validation": "PASS",
        "metrics": "PASS" if all(
            metrics["groups"][g]["metrics"]["root_cause_accuracy"].get("status") == "PASS"
            for g in groups_order) else "BLOCKED",
        "stats": "PASS" if primary_cmp.get("status") == "PASS" else "BLOCKED",
        "efficiency": "PASS" if any(
            metrics["groups"][g]["efficiency"][f].get("status") == "PASS"
            for g in groups_order for f in EFF_FIELDS) else "BLOCKED",
    }
    metrics["sections"] = sections
    metrics["status"] = "PASS"

    # ── 写盘（显式 UTF-8，docs R-49）
    out_dir.mkdir(parents=True, exist_ok=True)
    case_order = {c["id"]: i for i, c in enumerate(cases)}
    lines = []
    for g in groups_order:
        vs = sorted(verdicts_by_group[g],
                    key=lambda v: (case_order.get(v["case_id"], 10 ** 6), v["repeat"]))
        for v in vs:
            lines.append(json.dumps(v, ensure_ascii=False))
    (out_dir / "verdicts.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    mode = {
        "version": PKG_VERSION,
        "honest_boundaries": _honest_boundaries(cases_meta, groups_order),
        "reproduce": _reproduce(cases_path, answers_path, out_dir, groups),
    }
    report = R.render_report(metrics=metrics, stats_doc=stats_doc, mode=mode)
    audit = R.audit_forbidden_wording(report)
    metrics["report_audit"] = {
        "forbidden_wording_hits": audit,
        "scope": "report.md 全文扫描（禁用词表只在 stats.json 出现，故正文应零命中）",
        "status": "PASS" if not audit else "FAIL",
    }
    (out_dir / "report.md").write_text(report, encoding="utf-8", newline="\n")
    _dump(out_dir / "metrics.json", metrics)
    _dump(out_dir / "stats.json", stats_doc)

    if not quiet:
        _emit_summary(metrics, stats_doc, out_dir, audit)
    if diag is not None:
        diag.update({"stage": "done", "errors": [],
                     "warnings": [_as_dict(w) for w in warnings],
                     "out_dir": str(out_dir),
                     "sections": dict(sections),
                     "report_audit": dict(metrics["report_audit"])})
    return 0 if not audit else 1


def _dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def _emit_fail(title, errors, warnings, quiet):
    if quiet:
        return
    print(f"FAIL: {title}")
    for e in errors[:40]:
        print(f"  ERROR {e.get('code')}: {e.get('message')}")
    if len(errors) > 40:
        print(f"  …（另有 {len(errors) - 40} 条）")
    for w in warnings[:20]:
        print(f"  WARN  {w.get('code')}: {w.get('message')}")


def _emit_summary(metrics, stats_doc, out_dir, audit):
    print(f"M5 判决集 · 机器判据与统计（{PKG_VERSION}）")
    print(f"  用例 {metrics['denominators']['cases_total']} 条"
          f"（族 {metrics['denominators']['cases_by_family']}；"
          f"层 {metrics['denominators']['cases_by_difficulty']}）"
          f"{'；synthetic=true（合成夹具，非真实数据）' if metrics['inputs']['cases']['synthetic'] else ''}")
    for g in metrics["groups_order"]:
        c = metrics["groups"][g]["metrics"]["root_cause_accuracy"]
        fl = metrics["groups"][g]["metrics"]["false_claim_rate"]
        ab = metrics["groups"][g]["metrics"]["abstain_rate"]
        print(f"  [{g}] root_cause_accuracy={c.get('rate')} "
              f"({c.get('numerator')}/{c.get('denominator')}, abstain={c.get('n_abstain')})"
              f"  false_claim_rate={fl.get('rate')}  abstain_rate={ab.get('rate')}")
    for e in stats_doc["conclusion"]["discipline"]["per_comparison"]:
        print(f"  discipline [{e['metric']}] {e['group_a']} vs {e['group_b']}: "
              f"Δ={e.get('diff_pp')}pp p={e.get('p_value')} ⇒ {e['conclusion']}")
    for s, st in metrics["sections"].items():
        print(f"  section {s}: {st}")
    print(f"  report audit: {metrics['report_audit']['status']}"
          f"{'' if not audit else ' 命中 ' + str(audit)}")
    print(f"  输出：{out_dir}\\verdicts.jsonl / metrics.json / stats.json / report.md")
    print("PASS: m5 判决完成（BLOCKED 小节不是 FAIL）")


def _honest_boundaries(cases_meta, groups_order):
    return [
        "本轮只交付「判据 + 统计」机器；**尚未**在任何真实用例或真实模型答案上运行过——"
        "本次输入为 `synthetic: true` 的合成夹具。",
        "50 条 oracle 依赖用户自研的真机故障注入清单（docs/06 §1.4 步骤 1、§9 U7）；"
        "**不存在为数据**：本机器不构造模拟故障、不派生真值、不生成 oracle。",
        "真值派生（§1.4 步骤 2）、双人复核（步骤 3）、G6 反例校验（步骤 4）的**真实执行**属 M5 流程，"
        "不由本脚本代劳；本脚本只提供 G6 所需的区分度读数与 `gold_audit` 钩子位置。",
        "`detectors` 正则辅助通道、层别名词表 `L-LAYER v1`、以及「距第一候选最近的层」这一层判定规则"
        "均为 **judge 内部约定**（docs/06 未规定）⇒ 已版本化披露，需契约方复核后才能用于正式判决。",
        "`mixed_precision` 的聚合口径 docs/06 未规定 ⇒ 本机器取逐用例均值 macro 为主、micro 另报，属已披露假设。",
        "docs/06 §6.3 的「~180 条」**未给出推导** ⇒ 照录并另给标准两比例公式估计值；两者不一致，均已披露。",
        "docs/06 §1.3 的族→层构成与其自身声明的 16/20/14 不自洽（20/14/16；总和均 50）⇒ "
        "`--expect-full-suite` 只断言边际构成。",
        "效率遥测缺失一律 `null`（宁缺勿造，docs/06 §4 P11）⇒ 合成夹具缺字段处效率表为 BLOCKED，**不是 FAIL**。",
        "LLM 裁判维度（D1 现象 / D4 处置 / D6 验证）与其 rater agreement（§3.2）不在本机器范围；"
        "本机器只实现 §3.2 的**机判**部分。",
        "docs/06 §4 的 P1–P11 执行协议（轮转执行、参数冻结、每 10 次健康检查、采集源指纹、"
        "`ssh_conn_mode`、`DELTA_WINDOW_S`）属执行侧；本机器只读 `snapshot_id`/`captured_at_utc` 等字段，"
        "**不校验**协议合规性。",
        "`payloadPolicy` 的逐字一致性（§2.2：`CAP_VALUES`/`CAP_OBJS`/`MAX_STR`/`parallel` 跨组必须逐字相同）"
        "属执行侧门禁；本机器未校验该项。",
    ]


def _reproduce(cases_path, answers_path, out_dir, groups):
    py = r"python"
    gp = f" --groups {','.join(groups)}" if groups else ""
    return [
        f"{py} shared\\verify\\m5_judge.py --selftest",
        (f"{py} shared\\verify\\m5_judge.py --cases {cases_path} --answers {answers_path} "
         f"--out {out_dir}{gp} --expect-full-suite"),
        (f"$env:PYTHONIOENCODING='utf-8'; {py} shared\\verify\\m5_judge.py --cases "
         f"{cases_path} --answers {answers_path} --out {out_dir}{gp} "
         f"（★ R-49：显式 UTF-8，避免宿主 GBK 代码页崩 UnicodeEncodeError）"),
    ]


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser():
    ap = argparse.ArgumentParser(
        prog="m5_judge.py",
        description="M5 判决集：机器判据 + 配对统计 + 置信区间（纯离线、确定性、只读输入）")
    ap.add_argument("--cases", help="用例集（.yaml/.yml/.json；列表或 {cases:[...],meta:{}}）")
    ap.add_argument("--answers", help="答案 JSONL（必需键 case_id/group/repeat/question/answer_text）")
    ap.add_argument("--out", help="输出目录（verdicts.jsonl / metrics.json / stats.json / report.md）")
    ap.add_argument("--groups", help="只看这些组，逗号分隔（顺序决定主对比：前两组）")
    ap.add_argument("--expect-groups", type=int, default=None, dest="expect_groups",
                    help="断言分组数恰为该值，否则 ERROR")
    ap.add_argument("--strict", action="store_true", help="所有 WARN 升级为 ERROR")
    ap.add_argument("--expect-full-suite", action="store_true", dest="expect_full_suite",
                    help="断言 50 条与族 14/14/6/6/4、层 16/20/14（不满足 ⇒ ERROR，不自动补）")
    ap.add_argument("--carrier-metrics", default="", dest="carrier_metrics",
                    help="M6.3 臂级聚合 JSON（m6/carrier_efficiency.py 的 --json 产物）；缺字段时按臂回退")
    ap.add_argument("--selftest", action="store_true",
                    help="跑合成夹具自测（含 6 个反作弊负控），夹具与证据落到 --out 或默认目录")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "carrier_metrics", ""):
        with io.open(args.carrier_metrics, encoding="utf-8") as _fh:
            _doc = json.load(_fh)
        _CARRIER.clear()
        _CARRIER.update((_doc.get("arms") or {}) if isinstance(_doc, dict) else {})
        print("[judge] 臂级容器指标已载入：%d 臂（缺字段时回退，来源逐字段记录）" % len(_CARRIER))
    if args.selftest:
        out = Path(args.out) if args.out else \
            (Path(__file__).resolve().parents[2] / "tmp" / "m5_judge_selftest")
        return ST.run_selftest(run_pipeline=run_pipeline, out_dir=out,
                               repo_root=Path(__file__).resolve().parents[2])
    if not (args.cases and args.answers and args.out):
        print("FAIL: 需要 --cases / --answers / --out（或 --selftest）")
        return 1
    groups = [g.strip() for g in args.groups.split(",") if g.strip()] if args.groups else None
    try:
        return run_pipeline(args.cases, args.answers, args.out, groups=groups,
                            expect_groups=args.expect_groups, strict=args.strict,
                            expect_full_suite=args.expect_full_suite)
    except Exception as exc:  # pragma: no cover - 兜底：绝不吞异常成静默通过
        print(f"FAIL: 运行异常 {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
