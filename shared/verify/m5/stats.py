# -*- coding: utf-8 -*-
"""M5 · 统计内核（**纯标准库**，无任何第三方依赖）。

目标环境实测：``numpy`` / ``scipy`` / ``pandas`` 均**不存在**（只有 PyYAML 6.0.3）
⇒ docs/06 §6.2 要求的全部检验与效应量**自己实现**，且必须确定性、可复现。

按 docs/06 §6.2 落地：
  * 二值（根因命中/未命中）      → **McNemar 检验**（小样本用**精确二项**）+ **Cohen's h**
  * 连续（elapsed_s / ttft_s / tokens）→ **Wilcoxon 符号秩检验** + **Cliff's delta**
  * 集合（路径 F1）              → **配对 bootstrap 差值的 95% CI**
  * CI：bootstrap **10000** 次重采样，**固定随机种子**（可复现）
  * 多重比较：**Bonferroni**，**6 族**，α = 0.05/6 ≈ 0.0083（docs/06 §6.2）

★ docs/06 §6.1：同一用例、同一 repeat 下两组输入完全相同 ⇒ **配对样本**。
  本模块的主检验一律**配对**；独立样本口径只作为 ``independent_reference`` 对照值出现，
  且**不得**进入任何结论字段（任务书负控 ⑤）。

★ docs/06 §6.3 样本量纪律（机器化于 :func:`build_discipline`）：
  本设计**只能**对 **≥20 个百分点**的差异给出结论；不足 20pp 时结论字段只能是
  字符串 ``未检出显著差异``，**不得**写成「两者相当」。
"""
from __future__ import annotations

import math
from statistics import mean, median

BOOTSTRAP_B = 10000
BOOTSTRAP_SEED = 20261007          # 固定种子 ⇒ 可复现
ALPHA = 0.05
BONFERRONI_FAMILIES = 6            # docs/06 §6.2「分族检验时用 Bonferroni（6 族）」
BONFERRONI_ALPHA = ALPHA / BONFERRONI_FAMILIES
RESOLUTION_PP = 20.0               # docs/06 §6.3：可检出 ≥20 个百分点
MANDATED_NO_DIFF_LABEL = "未检出显著差异"
SIGNIFICANT_LABEL = "显著差异"
# ★ 以下禁用表述**只出现在机器可读输出里**，绝不写进 report.md 正文 ——
#   这样 report.md 可以做「全文扫描」级别的自查（见 reporting.audit_forbidden_wording）
FORBIDDEN_LABELS_WHEN_BELOW_RESOLUTION = ("两者相当", "两者等同", "无差异", "等价", "不相上下")
TEN_PP_HINT = "要检出 10pp 需 ~180 条（docs/06 §6.3；本次不做）"

_Z_975 = 1.959963984540054
_Z_80 = 0.8416212335729143


# ── 基本分布函数（纯标准库）────────────────────────────────────────────────

def normal_sf(z: float) -> float:
    """标准正态右尾 P(Z > z)。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """标准正态分位函数（Acklam 有理逼近，最大相对误差 ~1.15e-9）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须在 (0,1) 内")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def percentile(sorted_values: list, q: float):
    """线性插值分位数（与 numpy 默认 'linear' 同口径）。``q`` 取 0–100。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_values[0])
    pos = (n - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[int(pos)])
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))


def summarise(values: list) -> dict:
    """连续量的描述统计（docs/06 §2.2 / §4 P8 要求均值 + 标准差，不只报均值）。"""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0, "status": "BLOCKED", "note": "无可用观测"}
    sv = sorted(vals)
    m = mean(vals)
    var = mean([(v - m) ** 2 for v in vals]) if len(vals) > 1 else 0.0
    return {
        "n": len(vals), "status": "PASS",
        "mean": round(m, 6), "sd": round(math.sqrt(var), 6),
        "median": round(median(vals), 6),
        "p50": round(percentile(sv, 50), 6), "p95": round(percentile(sv, 95), 6),
        "min": round(min(vals), 6), "max": round(max(vals), 6),
    }


# ── 二值：McNemar + Cohen's h ───────────────────────────────────────────────

def binom_two_sided_p(k: int, n: int, p: float = 0.5) -> float:
    """精确二项双尾 p 值（用 math.comb 精确整数，再转浮点）。"""
    if n <= 0:
        return 1.0
    k = max(0, min(int(k), n))
    tail = sum(math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(0, k + 1))
    return min(1.0, 2.0 * tail)


def mcnemar_exact(b: int, c: int) -> dict:
    """McNemar **精确**检验（不和谐对 b/c，H0: b|(b+c) ~ Binomial(b+c, 0.5)）。

    ``p_chi2`` 只作参考值（1 自由度卡方 + 连续性校正），**不作主判据**。
    """
    n = b + c
    if n == 0:
        return {"b": b, "c": c, "n_discordant": 0, "p_value": 1.0,
                "p_method": "mcnemar_exact_degenerate",
                "note": "无不和谐对 ⇒ 两组在该指标上完全一致，p 定义为 1.0（不是 FAIL）"}
    k = min(b, c)
    p_exact = binom_two_sided_p(k, n, 0.5)
    chi2 = ((abs(b - c) - 1) ** 2) / n if n else 0.0
    return {"b": b, "c": c, "n_discordant": n, "p_value": p_exact,
            "p_method": "mcnemar_exact_binomial",
            "p_chi2_reference": round(normal_sf(math.sqrt(chi2)) * 2, 12),
            "chi2_continuity": round(chi2, 6)}


def cohens_h(p1: float, p2: float) -> float:
    """Cohen's h = 2·asin(√p1) − 2·asin(√p2)（二值效应量，docs/06 §6.2）。"""
    f = lambda p: 2.0 * math.asin(math.sqrt(min(1.0, max(0.0, float(p)))))  # noqa: E731
    return f(p1) - f(p2)


def cohens_h_magnitude(h: float) -> str:
    a = abs(h)
    if a < 0.2:
        return "negligible"
    if a < 0.5:
        return "small"
    if a < 0.8:
        return "medium"
    return "large"


# ── 连续：Wilcoxon 符号秩 + Cliff's delta ───────────────────────────────────

def wilcoxon_signed_rank(diffs: list) -> dict:
    """Wilcoxon 符号秩检验（配对）。零差被丢弃（标准做法，明确记录条数）。

    * 无并列且 n_eff ≤ 50 ⇒ **精确**（子集和 DP，非 2^n 枚举）
    * 存在并列或 n_eff > 50 ⇒ 正态近似 + 并列校正 + 连续性校正
    """
    nz = [float(d) for d in diffs if d is not None and d != 0.0]
    zeros = len([d for d in diffs if d is not None]) - len(nz)
    n = len(nz)
    if n == 0:
        return {"n_eff": 0, "zeros_dropped": zeros, "w_plus": 0.0, "w_minus": 0.0,
                "p_value": 1.0, "p_method": "wilcoxon_degenerate",
                "note": "全部差值为 0 ⇒ 无法拒绝 H0，p 定义为 1.0"}
    order = sorted(range(n), key=lambda i: abs(nz[i]))
    ranks = [0.0] * n
    tie_sizes = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(nz[order[j + 1]]) == abs(nz[order[i]]):
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        if j > i:
            tie_sizes.append(j - i + 1)
        i = j + 1
    w_plus = sum(ranks[i] for i in range(n) if nz[i] > 0)
    w_minus = sum(ranks[i] for i in range(n) if nz[i] < 0)
    w = min(w_plus, w_minus)
    has_ties = bool(tie_sizes)
    if not has_ties and n <= 50:
        total_max = int(n * (n + 1) / 2)
        counts = [0] * (total_max + 1)
        counts[0] = 1
        for r in range(1, n + 1):
            for s in range(total_max, r - 1, -1):
                if counts[s - r]:
                    counts[s] += counts[s - r]
        c = sum(counts[:int(round(w)) + 1])
        p = min(1.0, 2.0 * (c / (2 ** n)))
        method = "wilcoxon_exact_subset_sum"
        extra = {"exact_subsets_counting_le_w": int(c)}
    else:
        mu = n * (n + 1) / 4.0
        tie_corr = sum(t ** 3 - t for t in tie_sizes) / 48.0
        var = n * (n + 1) * (2 * n + 1) / 24.0 - tie_corr
        if var <= 0:
            return {"n_eff": n, "zeros_dropped": zeros, "w_plus": w_plus, "w_minus": w_minus,
                    "p_value": 1.0, "p_method": "wilcoxon_var_zero", "ties": tie_sizes,
                    "note": "并列校正后方差为 0 ⇒ 退化为不可判（BLOCKED 语义）"}
        z = (w_plus - mu - (0.5 if w_plus > mu else -0.5)) / math.sqrt(var)
        p = min(1.0, 2.0 * normal_sf(abs(z)))
        method = "wilcoxon_normal_approx_tie_corrected"
        extra = {"z": round(z, 6), "mu": mu, "var": round(var, 6)}
    out = {"n_eff": n, "zeros_dropped": zeros, "ties": tie_sizes,
           "w_plus": round(w_plus, 6), "w_minus": round(w_minus, 6),
           "w_stat": round(w, 6), "p_value": p, "p_method": method}
    out.update(extra)
    return out


def cliffs_delta(x: list, y: list) -> dict:
    """Cliff's delta = (#(x>y) − #(x<y)) / (n_x·n_y)（跨样本支配度，docs/06 §6.2）。"""
    xs = [float(v) for v in x if v is not None]
    ys = [float(v) for v in y if v is not None]
    if not xs or not ys:
        return {"delta": None, "status": "BLOCKED", "note": "样本为空"}
    gt = lt = eq = 0
    for a in xs:
        for b in ys:
            if a > b:
                gt += 1
            elif a < b:
                lt += 1
            else:
                eq += 1
    total = len(xs) * len(ys)
    d = (gt - lt) / total
    a = abs(d)
    mag = "negligible" if a < 0.147 else "small" if a < 0.33 else "medium" if a < 0.474 else "large"
    return {"delta": round(d, 6), "n_x": len(xs), "n_y": len(ys),
            "greater": gt, "less": lt, "equal": eq, "magnitude": mag, "status": "PASS"}


def paired_dominance(x: list, y: list) -> dict:
    """配对支配度（同 (case,repeat) 内比较）：(#x>y − #x<y)/n_pairs。

    doc 未定义配对版效应量；这里与 Cliff's delta 并列输出，**主判据取 Cliff's delta**。
    """
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if a is not None and b is not None]
    if not pairs:
        return {"status": "BLOCKED", "note": "无配对观测"}
    gt = sum(1 for a, b in pairs if a > b)
    lt = sum(1 for a, b in pairs if a < b)
    return {"paired_dominance": round((gt - lt) / len(pairs), 6), "n_pairs": len(pairs),
            "greater": gt, "less": lt, "status": "PASS"}


# ── 配对 bootstrap 95% CI ──────────────────────────────────────────────────

def paired_bootstrap_ci(a_values: list, b_values: list, *, b_iter: int = BOOTSTRAP_B,
                        seed: int = BOOTSTRAP_SEED) -> dict:
    """配对重采样差值的 95% CI（percentile 法）。

    重采样单位 = **(case_id, repeat) 配对**（docs/06 §6.1 的配对单元），
    B = 10000，固定种子 ⇒ 逐位可复现。
    """
    pairs = [(a, b) for a, b in zip(a_values, b_values) if a is not None and b is not None]
    n = len(pairs)
    if n == 0:
        return {"status": "BLOCKED", "n_pairs": 0, "note": "无配对观测 ⇒ CI 不可算"}
    point = mean([a for a, _ in pairs]) - mean([b for _, b in pairs])
    import random
    rng = random.Random(seed)
    stats_ = []
    for _ in range(b_iter):
        sa = sb = 0.0
        for _ in range(n):
            i = rng.randrange(n)
            sa += pairs[i][0]
            sb += pairs[i][1]
        stats_.append(sa / n - sb / n)
    stats_.sort()
    lo = percentile(stats_, 2.5)
    hi = percentile(stats_, 97.5)
    excludes_zero = (lo > 0 and hi > 0) or (lo < 0 and hi < 0)
    return {"status": "PASS", "n_pairs": n, "B": b_iter, "seed": seed,
            "method": "paired_percentile_bootstrap", "point_diff": round(point, 8),
            "ci95_low": lo, "ci95_high": hi, "excludes_zero": bool(excludes_zero)}


# ── 独立样本对照（★ 只作参考，绝不进结论）───────────────────────────────────

def independent_two_proportion_reference(hits_a: int, n_a: int, hits_b: int, n_b: int) -> dict:
    """合并方差两比例 z 检验（**独立样本口径**）。

    ★ 仅在 ``stats.json`` 里作 ``independent_reference`` 出现，用于证明
      「没有把配对数据当独立样本检验」（任务书负控 ⑤）；**不得**进入任何结论字段。
    """
    if n_a == 0 or n_b == 0:
        return {"status": "BLOCKED", "note": "某组样本为 0"}
    p1, p2 = hits_a / n_a, hits_b / n_b
    p_pool = (hits_a + hits_b) / (n_a + n_b)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return {"status": "BLOCKED", "note": "合并方差为 0（两组全同）",
                "p1": round(p1, 6), "p2": round(p2, 6), "n_a": n_a, "n_b": n_b}
    z = (p1 - p2) / se
    return {"status": "PASS", "test_form": "independent", "method": "two_proportion_z_pooled",
            "p1": round(p1, 6), "p2": round(p2, 6), "n_a": n_a, "n_b": n_b,
            "z": round(z, 6), "p_value": round(2 * normal_sf(abs(z)), 12),
            "warning": "独立样本口径；配对设计的正确判据是 McNemar（docs/06 §6.1）"}


# ── 样本量（docs/06 §6.3）──────────────────────────────────────────────────

def binomial_ci_halfwidth(p: float, n: int, z: float = _Z_975):
    """Wald 95% CI 半宽（docs/06 §6.3 的「p≈0.6、n=50 ⇒ 半宽 ≈ 0.14」）。"""
    return z * math.sqrt(p * (1 - p) / n)


def required_n_two_proportion(pp: float, p1: float, p2: float, *,
                              alpha: float = ALPHA, power: float = 0.8) -> int:
    """两组比较所需的**每组**样本量（正态近似公式，标注假设）。

    n = (z_{1−α/2} + z_power)² · [p1(1−p1) + p2(1−p2)] / (p1−p2)²
    """
    za = norm_ppf(1 - alpha / 2.0)
    zb = norm_ppf(power)
    delta = abs(p2 - p1)
    if delta == 0:
        return -1
    n = ((za + zb) ** 2) * (p1 * (1 - p1) + p2 * (1 - p2)) / (delta ** 2)
    return int(math.ceil(n))


def sample_size_note() -> dict:
    """docs/06 §6.3 的样本量论证（机器可查）。"""
    hw = binomial_ci_halfwidth(0.6, 50)
    return {
        "contract_source": "docs/06-评测设计（A-B对照）.md §6.3",
        "n_cases": 50,
        "binomial_ci_halfwidth_at_p060_n50": round(hw, 4),
        "binomial_ci_halfwidth_claimed": 0.14,
        "binomial_ci_matches_contract": abs(hw - 0.14) <= 0.005,
        "detectable_pp": RESOLUTION_PP,
        "mcnemar_min_discordant_pairs": 15,
        "ten_pp_hint": TEN_PP_HINT,
        "ten_pp_hint_derivation_stated_in_contract": False,
        "ten_pp_hint_note": (
            "★ 诚实边界：docs/06 §6.3 的「~180 条」**未给出推导**。"
            "本机器按标准两比例公式自行给出下列估计值（假设独立、α=0.05 双尾、power=0.8），"
            "并明确标注该估计与契约给的 ~180 不一致；两者都不构成本轮结论。"
        ),
        "required_n_per_group_estimates": [
            {"pp": 0.10, "p1": 0.50, "p2": 0.60, "n_per_group": required_n_two_proportion(0.10, 0.50, 0.60)},
            {"pp": 0.10, "p1": 0.60, "p2": 0.70, "n_per_group": required_n_two_proportion(0.10, 0.60, 0.70)},
            {"pp": 0.10, "p1": 0.70, "p2": 0.80, "n_per_group": required_n_two_proportion(0.10, 0.70, 0.80)},
            {"pp": 0.20, "p1": 0.40, "p2": 0.60, "n_per_group": required_n_two_proportion(0.20, 0.40, 0.60)},
            {"pp": 0.20, "p1": 0.50, "p2": 0.70, "n_per_group": required_n_two_proportion(0.20, 0.50, 0.70)},
        ],
        "formula": "n_per_group = (z_{1-alpha/2} + z_power)^2 * [p1(1-p1)+p2(1-p2)] / (p1-p2)^2",
    }


# ── 配对比较：二值 / 连续 ───────────────────────────────────────────────────

def match_pairs(records_a: list, records_b: list, *, abstain_as_error: bool = False):
    """按 (case_id, repeat) 配对（docs/06 §6.1）。

    返回 ``(pairs, dropped_abstain, n_only_a, n_only_b)``；``pairs`` 元素为
    ``(rec_a, rec_b)``。``abstain_as_error=False``（函数默认值，**仅作敏感性对照**）
    丢弃任一侧 abstain 的配对；★ M5 主口径是 ``abstain_as_error=True``（abstain 计未命中，
    既不进分子也不从分母扣除，见 :func:`compare_binary`）。
    """
    index_b = {(r["case_id"], r["repeat"]): r for r in records_b}
    pairs, dropped = [], 0
    matched_keys = set()
    for ra in records_a:
        key = (ra["case_id"], ra["repeat"])
        rb = index_b.get(key)
        if rb is None:
            continue
        matched_keys.add(key)
        if not abstain_as_error and (ra.get("abstain") or rb.get("abstain")):
            dropped += 1
            continue
        pairs.append((ra, rb))
    n_only_b = sum(1 for k in index_b if k not in matched_keys)
    n_only_a = sum(1 for r in records_a if (r["case_id"], r["repeat"]) not in index_b)
    return pairs, dropped, n_only_a, n_only_b


def mcnemar_from_pairs(pairs: list) -> dict:
    """从配对记录算 hit/miss 并做 McNemar（hit 取自记录上的 ``_hit`` 字段）。"""
    b = c = both_hit = both_miss = 0
    for ra, rb in pairs:
        ha, hb = ra["_hit"], rb["_hit"]
        if ha and not hb:
            b += 1
        elif hb and not ha:
            c += 1
        elif ha and hb:
            both_hit += 1
        else:
            both_miss += 1
    out = mcnemar_exact(b, c)
    out.update({"both_hit": both_hit, "both_miss": both_miss, "n_pairs": len(pairs)})
    return out


def compare_binary(records_a: list, records_b: list, *, label: str,
                   alpha: float = ALPHA, abstain_as_error: bool = False) -> dict:
    """二值指标的配对比较（差值 [95% CI] + McNemar + Cohen's h）。

    ``abstain_as_error=True`` = ★ M5 主口径（队长裁定 2026-09-29 / 契约
    ``shared/model/m5-cases.yaml`` ``scoring.abstain``）：**abstain 计未命中**，
    配对集合不丢 abstain 记录，abstain 的 ``_hit`` 为 ``None`` ⇒ 在
    :func:`mcnemar_from_pairs` 里落进 ``both_miss``（既不进 b 也不进 c），
    因此只影响两侧的率/效应量，不制造假的不和谐对。
    ``abstain_as_error=False``（旧口径）只保留作**敏感性对照**，不得当作主口径。
    """
    pairs, dropped, only_a, only_b = match_pairs(records_a, records_b,
                                                 abstain_as_error=abstain_as_error)
    base = {"metric": label, "test_form": "paired", "alpha": alpha,
            "abstain_as_error": bool(abstain_as_error)}
    if not pairs:
        base.update({"status": "BLOCKED", "note": "配对子集为空 ⇒ 配对检验不可算（BLOCKED，不是 FAIL）",
                     "n_pairs": 0, "n_only_a": only_a, "n_only_b": only_b,
                     "dropped_abstain_pairs": dropped})
        return base
    mc = mcnemar_from_pairs(pairs)
    av = [1.0 if ra["_hit"] else 0.0 for ra, _ in pairs]
    bv = [1.0 if rb["_hit"] else 0.0 for _, rb in pairs]
    ci = paired_bootstrap_ci(av, bv)
    p_a, p_b = mean(av), mean(bv)
    base.update({
        "status": "PASS", "n_pairs": len(pairs), "n_only_a": only_a, "n_only_b": only_b,
        "dropped_abstain_pairs": dropped,
        "rate_a_paired": round(p_a, 6), "rate_b_paired": round(p_b, 6),
        "diff_paired": round(p_a - p_b, 6), "diff_pp_paired": round((p_a - p_b) * 100, 4),
        "b": mc["b"], "c": mc["c"], "n_discordant": mc["n_discordant"],
        "p_value": mc["p_value"], "p_method": mc["p_method"],
        "p_chi2_reference": mc.get("p_chi2_reference"),
        "cohens_h": round(cohens_h(p_a, p_b), 6),
        "cohens_h_magnitude": cohens_h_magnitude(cohens_h(p_a, p_b)),
        "ci95_pp": [None if ci["status"] != "PASS" else round(ci["ci95_low"] * 100, 4),
                    None if ci["status"] != "PASS" else round(ci["ci95_high"] * 100, 4)],
        "ci95_excludes_zero": ci.get("excludes_zero"),
        "bootstrap": {k: ci[k] for k in ("B", "seed", "method", "n_pairs") if k in ci},
    })
    return base


def compare_continuous(pairs_values_a: list, pairs_values_b: list, *, label: str) -> dict:
    """连续指标的配对比较（Wilcoxon 符号秩 + Cliff's delta + 配对 bootstrap CI）。"""
    pairs = [(a, b) for a, b in zip(pairs_values_a, pairs_values_b)
             if a is not None and b is not None]
    if not pairs:
        return {"metric": label, "status": "BLOCKED", "n_pairs": 0,
                "test_form": "paired",
                "note": "无配对观测 ⇒ 不可算（BLOCKED，不是 FAIL）"}
    av = [a for a, _ in pairs]
    bv = [b for _, b in pairs]
    diffs = [a - b for a, b in pairs]
    w = wilcoxon_signed_rank(diffs)
    cd = cliffs_delta(av, bv)
    ci = paired_bootstrap_ci(av, bv)
    return {
        "metric": label, "status": "PASS", "test_form": "paired", "n_pairs": len(pairs),
        "mean_a": round(mean(av), 6), "mean_b": round(mean(bv), 6),
        "median_a": round(median(av), 6), "median_b": round(median(bv), 6),
        "mean_diff": round(mean(diffs), 6),
        "wilcoxon": w,
        "cliffs_delta": cd, "paired_dominance": paired_dominance(av, bv),
        "ci95": [ci.get("ci95_low"), ci.get("ci95_high")] if ci["status"] == "PASS" else None,
        "ci95_excludes_zero": ci.get("excludes_zero"),
        "bootstrap": {k: ci[k] for k in ("B", "seed", "method", "n_pairs") if k in ci},
    }


# ── 样本量纪律机器化（★ 任务书核心）────────────────────────────────────────

def build_discipline(*, metric: str, group_a: str, group_b: str, rate_a, rate_b,
                     ci95_pp, p_value, p_method, n_pairs: int, alpha: float = ALPHA,
                     ci_excludes_zero=None, note: str = "") -> dict:
    """把 docs/06 §6.3 的样本量纪律做成**机器可读判定**。

    判定规则（全部满足才允许写「显著差异」）：
      1. ``|Δ| ≥ 20pp``（docs/06 §6.3 的分辨率下限）
      2. ``p < α``（α 默认 0.05；分族比较用 Bonferroni 校正值）
      3. 95% CI 不跨 0
    否则结论字段**只能**是字符串 ``未检出显著差异``。
    """
    out = {
        "metric": metric, "group_a": group_a, "group_b": group_b,
        "rate_a": None if rate_a is None else round(float(rate_a), 6),
        "rate_b": None if rate_b is None else round(float(rate_b), 6),
        "resolution_pp": RESOLUTION_PP,
        "alpha": alpha,
        "n_pairs": n_pairs,
        "p_value": p_value, "p_method": p_method,
        "ci95_pp": ci95_pp,
        "ci95_excludes_zero": ci_excludes_zero,
    }
    if rate_a is None or rate_b is None:
        out.update({"status": "BLOCKED", "diff_pp": None, "meets_resolution": False,
                    "significant": False, "conclusion": MANDATED_NO_DIFF_LABEL,
                    "conclusion_reason": "指标不可算（BLOCKED，不是 FAIL）"})
        return out
    diff_pp = (float(rate_a) - float(rate_b)) * 100.0
    meets = abs(diff_pp) >= RESOLUTION_PP
    significant = (p_value is not None) and (p_value < alpha)
    ci_ok = bool(ci_excludes_zero) if ci_excludes_zero is not None else False
    out.update({"diff_pp": round(diff_pp, 4), "meets_resolution": bool(meets),
                "significant": bool(significant), "status": "PASS"})
    if not meets:
        out.update({"conclusion": MANDATED_NO_DIFF_LABEL,
                    "conclusion_reason": (
                        f"|Δ|={abs(diff_pp):.2f}pp < {RESOLUTION_PP:.0f}pp"
                        "（docs/06 §6.3 分辨率下限）⇒ 本样本量下不可判定")})
    elif not significant:
        out.update({"conclusion": MANDATED_NO_DIFF_LABEL,
                    "conclusion_reason": (
                        f"点估计 |Δ|={abs(diff_pp):.2f}pp 达 {RESOLUTION_PP:.0f}pp，"
                        f"但检验未达显著（p={p_value} ≥ α={alpha}）⇒ 不可下结论")})
    elif not ci_ok:
        out.update({"conclusion": MANDATED_NO_DIFF_LABEL,
                    "conclusion_reason": (
                        f"点估计 |Δ|={abs(diff_pp):.2f}pp 达 20pp 且 p={p_value} 显著，"
                        f"但 95% CI {ci95_pp} 跨 0 ⇒ 不可下结论")})
    else:
        out.update({"conclusion": SIGNIFICANT_LABEL,
                    "conclusion_reason": (
                        f"|Δ|={abs(diff_pp):.2f}pp ≥ {RESOLUTION_PP:.0f}pp，p={p_value} < α={alpha}，"
                        "95% CI 不跨 0")})
    if note:
        out["note"] = note
    return out


def discipline_policy_block() -> dict:
    """纪律的政策头（机器可读；禁用表述只在这里出现，不进 report.md 正文）。"""
    return {
        "policy_source": "docs/06-评测设计（A-B对照）.md §6.3 / §7",
        "resolution_pp": RESOLUTION_PP,
        "mandated_no_difference_label": MANDATED_NO_DIFF_LABEL,
        "significant_label": SIGNIFICANT_LABEL,
        "forbidden_labels_when_below_resolution": list(FORBIDDEN_LABELS_WHEN_BELOW_RESOLUTION),
        "alpha": ALPHA,
        "alpha_bonferroni": BONFERRONI_ALPHA,
        "bonferroni_families": BONFERRONI_FAMILIES,
        "rule": ("|Δ| < 20pp ⇒ 结论字段只能是「" + MANDATED_NO_DIFF_LABEL + "」；"
                 "禁止任何等效/相当性表述（禁用词表见本块 forbidden_labels_when_below_resolution）"),
    }


def bonferroni_table(rows: list, *, families: int = BONFERRONI_FAMILIES,
                     alpha: float = ALPHA) -> dict:
    """6 族 Bonferroni 校正表（docs/06 §6.2）。"""
    adj = alpha / families
    out_rows = []
    for r in rows:
        p = r.get("p_value")
        out_rows.append({
            **r,
            "alpha_bonferroni": round(adj, 10),
            "significant_raw": (p is not None and p < alpha),
            "significant_bonferroni": (p is not None and p < adj),
        })
    return {"families": families, "alpha": alpha, "alpha_adjusted": round(adj, 10),
            "note": "α_adj = 0.05/6 ≈ 0.0083（docs/06 §6.2）", "rows": out_rows}
