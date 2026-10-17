#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T1 阈值标定器（用户裁定 ①(A)：按真机值域标定 `fault-modes.yaml` 的阈值）。

规则（用户 2026-10-10 裁定 ①「选（A）」）
----------------------------------------
``warning  = clamp(max(健康期 P95 × k_warn, 域下限), …, 域上限)``    （``k_warn = 2``）
``critical = clamp(warning × k_crit, …, 域上限)``                    （``k_crit = 3``）

* **域上限**取自契约自身：``metrics.yaml#metrics[].range[1]``（凡声明了 range 者）⇒ 不另发明；
* **域下限**是**声明值**（见 ``threshold-observations.yaml#domain_bounds``），必须写明来源；
* **rationale 必含**：样本数 / 观测时点 / P95 / k / 域下限来源（用户裁定的硬要求）；
* **宁缺勿造**：样本数 < ``MIN_N``（默认 5）的指标**不标**，保持原值并把原因写进报告。

三态退出码：``PASS=0`` / ``FAIL=1``（契约或输入坏） / ``BLOCKED=2``（**没有可用观测 ⇒ 没有任何阈值可标**）。

用法
----
    python shared/verify/threshold_calibrate.py --selftest
    python shared/verify/threshold_calibrate.py --propose          # 只打印提案
    python shared/verify/threshold_calibrate.py --apply            # 写入 fault-modes.yaml
"""

from __future__ import annotations

import argparse
import io
import json
import re
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "shared", "model")
OBS = os.path.join(MODEL, "threshold-observations.yaml")
FM = os.path.join(MODEL, "fault-modes.yaml")
METRICS = os.path.join(MODEL, "metrics.yaml")

PASS, FAIL, BLOCKED = 0, 1, 2
K_WARN, K_CRIT = 2.0, 3.0
MIN_N = 5
# ★ 不由本工具标定的阈值（归别的裁定处理；每项写明归属）
DEFERRED = {
    "load1_per_core_light": "★ 用户裁定 ②(A)：改为按 Host.cpuCores 归一化（normalize_by）⇒ 本工具**不按绝对值标**；per-core 标定待采集层为 os 层主机采集核数",
}


def _yaml():
    import yaml
    return yaml


def load_all():
    y = _yaml()
    with io.open(OBS, encoding="utf-8") as fh:
        obs = y.safe_load(fh)
    with io.open(FM, encoding="utf-8") as fh:
        fm = y.safe_load(fh)
    with io.open(METRICS, encoding="utf-8") as fh:
        mt = y.safe_load(fh)
    return obs, fm, mt


def clamp(v, floor, cap):
    if floor is not None:
        v = max(v, floor)
    if cap is not None:
        v = min(v, cap)
    return v


def propose(obs, fm, mt, min_n=MIN_N):
    """⇒ (proposals, skipped, blocked)。proposals = [(threshold_name, warning, critical, rationale)]。"""
    dom = {o["metric"]: o for o in (obs.get("observations") or [])}
    _bents = ((obs.get("domain_bounds") or {}).get("entries") or [])
    bounds = {e["metric"]: e for e in _bents if not e.get("threshold")}          # 按 metric（默认）
    bounds_by_th = {e["threshold"]: e for e in _bents if e.get("threshold")}     # ★ 按阈值名（优先，避免 light/severe 互相覆盖）
    ranges = {m["code"]: (m.get("range") or [None, None])[1] for m in (mt.get("metrics") or [])}
    src = (obs.get("meta") or {}).get("source") or "（未登记时点）"
    props, skipped = [], []
    for th in (fm.get("thresholds") or []):
        name, metric = th.get("name"), th.get("metric")
        o = dom.get(metric)
        # ★ 只跳过**显式**声明不得重标的阈值（如用户裁定 ③ 的 xact_age_s_light）；
        #   不得用 "kind 以『已裁定』开头" 这种松条件 —— 那会误伤本工具自己标的条目。
        ruled = {d.get("threshold") for d in (obs.get("declared_deviations") or [])
                 if d.get("do_not_recalibrate")}
        if name in ruled:
            skipped.append((name, metric, "**已由用户裁定**（见 declared_deviations）⇒ 本工具不得覆盖"))
            continue
        if name in DEFERRED:
            skipped.append((name, metric, DEFERRED[name]))
            continue
        if not o or o.get("p95") is None:
            skipped.append((name, metric, "无观测"))
            continue
        if (o.get("n") or 0) < min_n:
            skipped.append((name, metric, "样本不足（n=%s < %s）" % (o.get("n"), min_n)))
            continue
        p95 = float(o["p95"])
        b = bounds_by_th.get(name) or bounds.get(metric) or {}
        floor = b.get("floor")
        cap = ranges.get(metric)
        w_raw = p95 * K_WARN
        w = clamp(w_raw, floor, cap)
        c = clamp(w * K_CRIT, floor, cap)
        why_floor = b.get("floor_source") or "无域下限声明"
        rationale = ("按用户裁定 ①(A)：健康期 P95=%s（n=%s；来源 %s）× k=%s ⇒ %s；"
                     "域下限=%s（%s）；域上限=%s（metrics.yaml#range[1]）⇒ warning=%s；"
                     "×k_crit=%s ⇒ critical=%s" % (p95, o.get("n"), src, K_WARN, w_raw,
                                                   floor, why_floor, cap, round(w, 6), K_CRIT, round(c, 6)))
        props.append((name, round(w, 6), round(c, 6), rationale))
    blocked = (len(props) == 0)
    return props, skipped, blocked


def apply_props(fm_path, props):
    """把提案写进 fault-modes.yaml —— **按行索引就地替换**，保住全部注释、顺序与缩进。

    为什么不用 yaml.safe_dump 回写：实测 safe_dump **会丢掉文件注释**（本文件有 purity 逐条登记的
    注释条目）⇒ 不可接受的副作用。
    为什么不"补插缺行"：那需要自己算缩进，实测曾把 4 空格块插成 2 空格 ⇒ YAML 坏。目标阈值在原文件里
    都已有 warning/critical/rationale 三行（就地替换即可）；如将来真有缺行阈值要标定，应先修契约文件本身。
    返回改动条数。
    """
    by = {p[0]: p for p in props}
    with io.open(fm_path, encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    # 定位每个目标阈值的块范围 [start, end)
    spans = {}
    cur, cur_start = None, None
    for i, ln in enumerate(lines):
        m = re.match(r"^(\s*)-\s+name:\s*(\S+)\s*$", ln)
        if m:
            if cur is not None:
                spans[cur] = (cur_start, i)
            cur, cur_start = m.group(2), i
    if cur is not None:
        spans[cur] = (cur_start, len(lines))
    changed = 0
    for name, (_n, w, c, why) in by.items():
        if name not in spans:
            continue
        a, b = spans[name]
        indent = re.match(r"^(\s*)", lines[a + 1]).group(1) if a + 1 < b else "    "
        for i in range(a, b):
            st = lines[i].strip()
            if st.startswith("warning:"):
                lines[i] = "%swarning: %s" % (indent, w)
                changed += 1
            elif st.startswith("critical:"):
                lines[i] = "%scritical: %s" % (indent, c)
            elif st.startswith("rationale:"):
                lines[i] = "%srationale: %s" % (indent, _q(why))
    with io.open(fm_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return changed


def _q(s):
    """rationale 用双引号包裹并按 YAML 规则转义。"""
    return '"%s"' % str(s).replace("\\", "\\\\").replace('"', '\\"')


def selftest():
    ok = total = 0
    # 1) clamp 正控
    total += 1
    if clamp(0.0018, 0.5, 1.0) == 0.5 and clamp(9.0, 0.0, 1.0) == 1.0 and clamp(0.23, 0.0, 1.0) == 0.23:
        ok += 1
    else:
        print("  ★ clamp 正控不符")
    # 2) 规则端到端（合成观测：P95=0.1、n=11、cap=1.0 ⇒ warning=0.2、critical=0.6）
    total += 1
    obs = {"meta": {"source": "selftest"}, "observations": [{"metric": "m.x", "n": 11, "p95": 0.1}],
           "domain_bounds": {"entries": [{"metric": "m.x", "floor": 0.0, "floor_source": "selftest"}]}}
    fm = {"thresholds": [{"name": "t_light", "metric": "m.x"}]}
    mt = {"metrics": [{"code": "m.x", "range": [0, 1]}]}
    props, skipped, blocked = propose(obs, fm, mt)
    if len(props) == 1 and abs(props[0][1] - 0.2) < 1e-9 and abs(props[0][2] - 0.6) < 1e-9 and not blocked:
        ok += 1
    else:
        print("  ★ 规则端到端不符：%s" % props)
    # 3) 负控：样本不足 ⇒ 不标（宁缺勿造）
    total += 1
    obs2 = {"observations": [{"metric": "m.x", "n": 1, "p95": 0.1}], "domain_bounds": {"entries": []}}
    p2, s2, b2 = propose(obs2, fm, mt)
    if not p2 and s2 and b2:
        ok += 1
    else:
        print("  ★ 样本不足负控不符：props=%s skipped=%s blocked=%s" % (p2, s2, b2))
    # 4) 负控：无域下限且 P95=0 ⇒ warning 会被 cap 之外的下限缺失放过；此处验 clamp 上限生效
    total += 1
    obs3 = {"observations": [{"metric": "m.x", "n": 11, "p95": 0.9}], "domain_bounds": {"entries": []}}
    p3, _s, _b = propose(obs3, fm, mt)
    if p3 and p3[0][1] == 1.0 and p3[0][2] == 1.0:   # 1.8 → 上限 1.0；critical 也封顶
        ok += 1
    else:
        print("  ★ 上限封顶负控不符：%s" % p3)
    print("  SELFTEST %d/%d 项通过" % (ok, total))
    return PASS if ok == total else FAIL


def main(argv=None):
    ap = argparse.ArgumentParser(description="T1 阈值标定器（用户裁定 ①(A)）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    obs, fm, mt = load_all()
    props, skipped, blocked = propose(obs, fm, mt, a.min_n)
    for name, w, c, why in props:
        print("  %-26s warning=%-12s critical=%-12s" % (name, w, c))
        print("      %s" % why[:200])
    for name, metric, why in skipped:
        print("  %-26s **不标**：%s（metric=%s）" % (name, why, metric))
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"proposals": [{"name": n, "warning": w, "critical": c, "rationale": r} for n, w, c, r in props],
                       "skipped": [{"name": n, "metric": m, "why": y} for n, m, y in skipped]},
                      fh, ensure_ascii=False, indent=1)
    if a.apply and props:
        n = apply_props(FM, props)
        print("  已写入 fault-modes.yaml：%d 条阈值" % n)
    if blocked:
        print("  BLOCKED（≠ FAIL）：没有任何指标达到样本门槛（n≥%d）⇒ 无可标定项" % a.min_n)
        return BLOCKED
    print("  PASS：提案 %d 条；不标 %d 条（宁缺勿造）" % (len(props), len(skipped)))
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
