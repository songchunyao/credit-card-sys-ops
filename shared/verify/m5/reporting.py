# -*- coding: utf-8 -*-
"""M5 · report.md 渲染（docs/06 §7 结论模板的执行体）。

必须分别报告（§2.1 约定 4 / §7）：
  * 分层 easy/medium/hard
  * 分族 族C1–族C6（★ 一律带「族」前缀；R-35 的命名消歧）
  * 六族 Bonferroni 表
  * abstain 敏感性表
  * 诚实边界小节

★ 结论措辞纪律：正文**绝不**出现 docs/06 §6.3 禁用的等效/相当性表述；
  这些词只存在于 ``stats.json:conclusion.discipline.forbidden_labels_when_below_resolution``
  ⇒ 报告可以做**全文扫描**级别的自查（:func:`audit_forbidden_wording`）。
"""
from __future__ import annotations

from . import stats as S

DISPLAY = {
    "root_cause_accuracy": "root_cause_accuracy（主）",
    "layer_accuracy": "layer_accuracy",
    "path_f1": "path_f1",
    "object_accuracy": "object_accuracy",
    "false_claim_rate": "false_claim_rate（越低越好）",
    "mixed_precision": "mixed_precision",
    "abstain_rate": "abstain_rate",
    "evidence_accuracy": "evidence_accuracy",
}
DIFF_ORDER = ("easy", "medium", "hard")


def _f(x, nd=4, dash="—"):
    if x is None:
        return dash
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _rate_cell(cell):
    if not cell or cell.get("status") != "PASS":
        return "BLOCKED"
    r = cell.get("rate")
    return "—" if r is None else f"{r:.4f}"


def _n_over_d(cell):
    if not cell or cell.get("status") != "PASS":
        return "—"
    return f"{cell.get('numerator')}/{cell.get('denominator')}"


def _cmp_cell(cmp_, key):
    if not cmp_ or cmp_.get("status") != "PASS":
        return "BLOCKED"
    v = cmp_.get(key)
    return "—" if v is None else v


def audit_forbidden_wording(text: str) -> list:
    """全文扫描 docs/06 §6.3 的禁用表述。返回命中的 (词, 次数) 列表。

    ★ 本函数是**有牙齿**的：``selftest`` 会喂一段含禁用词的毒化文本，
      断言本函数能抓到（否则该自查是空转）。
    """
    hits = []
    for w in S.FORBIDDEN_LABELS_WHEN_BELOW_RESOLUTION:
        n = text.count(w)
        if n:
            hits.append((w, n))
    return hits


def render_report(*, metrics: dict, stats_doc: dict, mode: dict) -> str:
    L: list[str] = []
    A = L.append
    groups = metrics["groups_order"]
    pa, pb = metrics["primary_contrast"]
    cons = stats_doc["comparisons"]

    def cmp_of(metric):
        return (cons.get(metric) or {}).get(f"{pa}|{pb}")

    A("# M5 判决集 · 机器判据与统计报告")
    A("")
    A(f"- 生成器：`{metrics['generated_by']}`（机器版本 `{mode.get('version', '?')}`）")
    A(f"- 生成时刻（UTC）：`{metrics.get('generated_at_utc')}`")
    A(f"- 主对比：**{pa}** vs **{pb}**（组顺序决定；用 `--groups` 调整）")
    A(f"- 样本口径：用例 {metrics['denominators']['cases_total']} 条；"
      f"各组 repeat = {metrics['denominators']['repeats_by_group']}")
    A(f"- 输入指纹：cases `{metrics['inputs']['cases']['sha256'][:16]}…` "
      f"({metrics['inputs']['cases']['n_cases']} 条"
      f"{'，synthetic=true' if metrics['inputs']['cases'].get('synthetic') else ''})；"
      f"answers `{metrics['inputs']['answers']['sha256'][:16]}…` "
      f"({metrics['inputs']['answers']['n_records']} 条)")
    A(f"- 校验状态：**{metrics['validation']['status']}**"
      f"（ERROR {len(metrics['validation']['errors'])} / WARN {len(metrics['validation']['warnings'])}）")
    if metrics["inputs"]["cases"].get("synthetic"):
        A("")
        A("> ★ **合成夹具**：本次输入的用例集与答案均为 `synthetic: true` 的**合成数据**，"
          "**不是**真实用例、**不是**真实模型答案。本报告的任何数值只证明**判据与统计机器可运行**，"
          "**不构成**对任何被测组的性能结论（docs/06 §1.4 步骤 1 明确 50 条正式用例依赖真机故障注入清单）。")
    A("")

    # ── 表 1 · 主判据 ────────────────────────────────────────────────────
    A("## 表 1 · 主判据（docs/06 §7 表 1）")
    A("")
    head = ["指标"] + groups + [f"差值 {pa}−{pb} [95% CI] (pp)", f"p（McNemar 精确）"]
    A("| " + " | ".join(head) + " |")
    A("|" + "---|" * len(head))
    rows = [("root_cause_accuracy", "root_cause_accuracy"),
            ("root_cause_accuracy[easy]", "├ easy (16)"),
            ("root_cause_accuracy[medium]", "├ medium (20)"),
            ("root_cause_accuracy[hard]", "├ hard (14)"),
            ("layer_accuracy", "layer_accuracy"),
            ("path_f1", "path_f1"),
            ("object_accuracy", "object_accuracy"),
            ("false_claim_rate", "false_claim_rate（越低越好）"),
            ("mixed_precision", "mixed_precision"),
            ("evidence_accuracy", "evidence_accuracy（证据命中）"),
            ("abstain_rate", "abstain_rate")]
    for key, label in rows:
        cells = [_rate_cell(_cell_for(metrics, g, key)) for g in groups]
        c = cmp_of(key)
        if c and c.get("status") == "PASS":
            diff = f"{c['diff_pp_paired']:+.2f}pp [{_f(c['ci95_pp'][0], 2)}, {_f(c['ci95_pp'][1], 2)}]"
            if "b" in c and "c" in c:
                pv = f"{c['p_value']:.3g}（b={c['b']}, c={c['c']}）"
            else:
                pv = f"{c['p_value']:.3g}（{c.get('p_method')}）"
        else:
            diff = pv = "BLOCKED"
        A("| " + " | ".join([label] + cells + [diff, pv]) + " |")
    A("")
    A("> `abstain` 按 docs/06 §2.1「既不计对也不计错」单列：主判据分母为 "
      "`用例数 − abstain 数`；「abstain 计错」敏感性分母固定为用例总数（见表 4）。")
    A("")

    # ── 表 2 · 分层 ──────────────────────────────────────────────────────
    A("## 表 2 · 分层（easy / medium / hard，docs/06 §1.3 / §2.1 约定 4）")
    A("")
    A("| 分层 | " + " | ".join(f"{g} 根因acc" for g in groups) + " | 用例数 |")
    A("|" + "---|" * (len(groups) + 2))
    for d in DIFF_ORDER:
        cells = [_rate_cell((metrics["groups"][g]["by_difficulty"].get(d) or {})
                            .get("metrics", {}).get("root_cause_accuracy")) for g in groups]
        n = metrics["groups"][groups[0]]["by_difficulty"].get(d, {}).get("n_cases", 0)
        A(f"| {d} | " + " | ".join(cells) + f" | {n} |")
    A("")
    A("> ★ 契约侧算术不自洽（**已登记，未改 docs**）：docs/06 §1.3 的族→层构成 "
      "（easy = 族C1 全部 + 族C5 全部 = 14+6 = 20；medium = 族C2 的 F1/F3/F4/F6 + 族C3 = 8+6 = 14；"
      "hard = 族C2 的 F2/F5/F7 + 族C4 + 族C6 = 6+6+4 = 16）与它自己声明的 16/20/14 **不一致**"
      "（20/14/16）；三者的**总和**都是 50。"
      "`--expect-full-suite` 因此只断言**边际构成**（族 14/14/6/6/4、层 16/20/14），"
      "**不断言** §1.3 的族→层交叉表。上表用例数为输入实测值。")
    A("")

    # ── 表 3 · 分族 ──────────────────────────────────────────────────────
    A("## 表 3 · 分族（族C1–族C6；★ R-35 命名消歧：此「族」指 docs/06 §1.1 用例族，"
      "非 docs/00 §3.1 架构约束 C1–C6、非 M4-C1…M4-C9）")
    A("")
    A("| 族 | " + " | ".join(f"{g} 根因acc" for g in groups) + " | 用例数 |")
    A("|" + "---|" * (len(groups) + 2))
    for fam in metrics["families_order"]:
        cells = [_rate_cell((metrics["groups"][g]["by_family"].get(fam) or {})
                            .get("metrics", {}).get("root_cause_accuracy")) for g in groups]
        n = metrics["groups"][groups[0]]["by_family"].get(fam, {}).get("n_cases", 0)
        A(f"| 族{fam} | " + " | ".join(cells) + f" | {n} |")
    A("")

    # ── 表 4 · abstain 敏感性 ────────────────────────────────────────────
    A("## 表 4 · abstain 敏感性（docs/06 §2.1 约定 2）")
    A("")
    A("| 组 | abstain 数 / 用例数 | root_cause_accuracy（abstain 单列，主） | "
      "root_cause_accuracy（abstain 计错，敏感性） | abstain_rate |")
    A("|---|---|---|---|---|")
    for g in groups:
        gm = metrics["groups"][g]
        c = gm["metrics"]["root_cause_accuracy"]
        ab = gm["metrics"]["abstain_rate"]
        A(f"| {g} | {c['n_abstain']}/{metrics['denominators']['cases_total']} | "
          f"{_rate_cell(c)} | {_f(c.get('rate_abstain_as_error'), 4)} | {_rate_cell(ab)} |")
    A("")

    # ── 表 5 · 六族 Bonferroni ───────────────────────────────────────────
    bon = stats_doc.get("bonferroni") or {}
    A("## 表 5 · 六族 Bonferroni（docs/06 §6.2：α = 0.05/6 ≈ "
      f"{bon.get('alpha_adjusted', S.BONFERRONI_ALPHA):.4f}）")
    A("")
    A(f"对比 = {pa} vs {pb}；指标 = root_cause_accuracy。"
      f"族数 = {bon.get('families')}，α = {bon.get('alpha')}，"
      f"α_adj = {bon.get('alpha_adjusted')}")
    A("")
    A("| 族 | n_pairs | b | c | p（McNemar 精确） | p < α(0.05) | p < α_adj |")
    A("|---|---|---|---|---|---|---|")
    for r in (bon.get("rows") or []):
        A(f"| 族{r['family']} | {_f(r.get('n_pairs'), 0)} | {_f(r.get('b'), 0)} | {_f(r.get('c'), 0)} | "
          f"{_f(r.get('p_value'), 4)} | {'是' if r.get('significant_raw') else '否'} | "
          f"{'是' if r.get('significant_bonferroni') else '否'} |")
    A("")

    # ── 表 6 · 效率 ──────────────────────────────────────────────────────
    A("## 表 6 · 效率（docs/06 §2.2；口径来源 `upstream-eval/harness.py:118-132`）")
    A("")
    eff_fields = ["elapsed_s", "ttft_s", "input_tokens", "output_tokens",
                  "tool_calls_n", "obs_total_bytes", "iterations_used"]
    A("| 指标 | " + " | ".join(f"{g} 均值±SD (n)" for g in groups) + " |")
    A("|" + "---|" * (len(groups) + 1))
    for fld in eff_fields:
        cells = []
        for g in groups:
            s = metrics["groups"][g]["efficiency"].get(fld) or {}
            cells.append("BLOCKED（缺失，宁缺勿造）" if s.get("status") != "PASS"
                         else f"{s['mean']:.4f}±{s['sd']:.4f} ({s['n']})")
        A(f"| {fld} | " + " | ".join(cells) + " |")
    A("")
    A("> 缺失遥测一律 `null`，**绝不补 0**（对齐 docs/06 §4 P11「宁缺勿造」）。"
      "`tool_calls_n` 的去重口径移植自 `harness.py:109-117`。")
    A("")

    # ── 样本量纪律 ───────────────────────────────────────────────────────
    disc = stats_doc["conclusion"]["discipline"]
    pol = disc["policy"]
    A("## 样本量纪律（★ docs/06 §6.3 机器化）")
    A("")
    A(f"- 政策来源：`{pol['policy_source']}`")
    A(f"- 分辨率下限：**{pol['resolution_pp']:.0f} 个百分点**；α = {pol['alpha']}；"
      f"Bonferroni 族数 = {pol['bonferroni_families']}，α_adj ≈ {pol['alpha_bonferroni']:.4f}")
    A(f"- 不足 20pp 时结论字段**只能**是：**「{pol['mandated_no_difference_label']}」**"
      f"（禁止的等效/相当性表述见 `stats.json:conclusion.discipline."
      f"forbidden_labels_when_below_resolution`，本报告正文零出现）")
    A("")
    A("| 指标 | 组 A | 组 B | 全样本 Δ(pp) | 配对 Δ(pp) | 95% CI (pp) | p | "
      "|Δ|≥20pp | p<α | CI 不跨 0 | **结论字段** |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    for e in disc["per_comparison"]:
        A(f"| {e['metric']} | {e['group_a']} | {e['group_b']} | {_f(e.get('diff_pp'), 2)} | "
          f"{_f(e.get('diff_pp_paired'), 2)} | "
          f"[{_f((e.get('ci95_pp') or [None, None])[0], 2)}, "
          f"{_f((e.get('ci95_pp') or [None, None])[1], 2)}] | "
          f"{_f(e.get('p_value'), 3)} | {'是' if e.get('meets_resolution') else '否'} | "
          f"{'是' if e.get('significant') else '否'} | "
          f"{'是' if e.get('ci95_excludes_zero') else '否'} | **{e['conclusion']}** |")
    A("")
    for e in disc["per_comparison"]:
        A(f"- `{e['metric']}`（{e['group_a']} vs {e['group_b']}）：**{e['conclusion']}** — "
          f"{e['conclusion_reason']}")
    A("")
    ss = stats_doc["conclusion"]["sample_size"]
    A("### 样本量论证（docs/06 §6.3）")
    A("")
    A(f"- 二项比例 p≈0.6、n=50：95% CI 半宽 = "
      f"**{ss['binomial_ci_halfwidth_at_p060_n50']}**（契约声称 ≈0.14 ⇒ "
      f"吻合：{ss['binomial_ci_matches_contract']}）")
    A(f"- 配对 McNemar：不和谐对 ≥ {ss['mcnemar_min_discordant_pairs']} 时可检出 "
      f"~{ss['detectable_pp']:.0f} 个百分点差异（α=0.05, power=0.8）")
    A(f"- **{ss['ten_pp_hint']}**")
    A(f"- ⚠ {ss['ten_pp_hint_note']}")
    A(f"- 本机器按 `{ss['formula']}` 自行给出的每组样本量估计：")
    A("")
    A("| 目标可检出 Δ | p1 | p2 | 每组所需 n |")
    A("|---|---|---|---|")
    for r in ss["required_n_per_group_estimates"]:
        A(f"| {r['pp']:.0%} | {r['p1']} | {r['p2']} | {r['n_per_group']} |")
    A("")

    # ── 文字结论（固定句式 §7）───────────────────────────────────────────
    A("## 文字结论（docs/06 §7 固定句式）")
    A("")
    for e in disc["per_comparison"]:
        if e["status"] != "PASS":
            A(f"- （{e['metric']}）**{e['conclusion']}** — {e['conclusion_reason']}")
            continue
        direction = "优于" if e["diff_pp"] > 0 else "低于"
        if e["conclusion"] == S.SIGNIFICANT_LABEL:
            A(f"- 在全部用例上，{e['group_a']} 的 {e['metric']} {direction} {e['group_b']}，"
              f"差值为 {e['diff_pp']:+.2f}pp（95% CI "
              f"[{_f((e.get('ci95_pp') or [None, None])[0], 1)}, "
              f"{_f((e.get('ci95_pp') or [None, None])[1], 1)}], p = {e.get('p_value'):.3g}）。")
        else:
            A(f"- 在全部用例上，{e['group_a']} 与 {e['group_b']} 的 {e['metric']} "
              f"**{e['conclusion']}**（点估计 Δ={e['diff_pp']:+.2f}pp；"
              f"{e['conclusion_reason']}）—— 本样本量下**不得**下优越性结论。")
    A("")

    # ── 诚实边界 ─────────────────────────────────────────────────────────
    A("## 诚实边界")
    A("")
    for line in mode.get("honest_boundaries", []):
        A(f"- {line}")
    A("")

    A("## 复现命令")
    A("")
    for line in mode.get("reproduce", []):
        A(f"```")
        A(line)
        A("```")
    A("")
    return "\n".join(L) + "\n"


def _cell_for(metrics: dict, group: str, key: str):
    """按 ``key`` 取指标单元；``key`` 为 ``metric`` 或 ``metric[difficulty]``。"""
    gm = metrics["groups"][group]
    if "[" in key:
        metric, stratum = key.split("[", 1)
        stratum = stratum.rstrip("]")
        bucket = gm["by_difficulty"].get(stratum)
        if stratum in gm["by_family"]:
            bucket = gm["by_family"][stratum]
        return (bucket or {}).get("metrics", {}).get(metric)
    return gm["metrics"].get(key)
