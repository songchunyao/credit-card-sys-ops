#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6.3 容器侧效率指标采集器（审计链 → 判据器字段）。

为什么需要它
------------
``docs/06 §2.2`` 要求 ``query_latency_p50/p95`` **与** ``elapsed_s`` **分开报**（前者是载体能力，
后者混入 LLM 随机性）；而运行台 `shared/deploy/smoke/m5_run.py:1516-1522` 明确声明这些字段属
**容器侧**、它不产，表 6 按「宁缺勿造」显示 BLOCKED。⇒ 本脚本把各臂**审计链**的 per-call 读数
接上（口径与路径见 ``shared/model/m6-carrier-metrics.yaml``）。

三态退出码
----------
``PASS = 0``（至少一臂量到）／``FAIL = 1``（量到了但口径不符）／``BLOCKED = 2``（**一臂都没量到**）。
★ 缺审计链（未声明的臂、或声明为非 null 但文件不在场）**只记 BLOCKED，绝不填 0**。

粒度（诚实声明）
----------------
审计链**没有 case_id** ⇒ 本采集只能做到**臂级**聚合；逐用例归属留给消费端声明其局限（OQ-M6M-01）。

用法
----
    python shared/verify/m6/carrier_efficiency.py --selftest
    python shared/verify/m6/carrier_efficiency.py --answers tmp/m5_run_evidence/<run>/answers.jsonl
    python shared/verify/m6/carrier_efficiency.py --audit-map my.json --window-start ... --window-end ...
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONTRACT = os.path.join(ROOT, "shared", "model", "m6-carrier-metrics.yaml")

PASS, FAIL, BLOCKED = 0, 1, 2


def _yaml():
    import yaml
    return yaml


def load_contract(path=None):
    with io.open(path or CONTRACT, encoding="utf-8") as fh:
        return _yaml().safe_load(fh)


def percentile(vals, p):
    """nearest-rank：index = ceil(p/100 * n) - 1（越界截断）。"""
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    idx = int(math.ceil(p / 100.0 * n)) - 1
    return v[max(0, min(n - 1, idx))]


def _ts(s):
    """ISO-8601 → 可比较的 datetime；容错处理结尾的 Z。"""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def read_audit(path, spec, w0=None, w1=None):
    """读一个审计链文件 ⇒ (values, rows, n_lines, skipped)。values = per-call 延迟(ms)。"""
    values, rows, n, skipped = [], 0, 0, 0
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                d = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if d.get(spec.get("ok_key")) != spec.get("ok_value"):
                continue
            ts = _ts(d.get(spec.get("time_key")))
            if w0 and ts and ts < w0:
                continue
            if w1 and ts and ts > w1:
                continue
            v = d.get(spec.get("latency_key"))
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                values.append(float(v))
            r = d.get(spec.get("rows_key"))
            if isinstance(r, (int, float)) and not isinstance(r, bool):
                rows += float(r)
    return values, rows, n, skipped


def collect(audit_map=None, w0=None, w1=None, contract=None):
    c = load_contract(contract)
    amap = audit_map or {}
    rep = {"window": {"start": w0.isoformat() if w0 else None, "end": w1.isoformat() if w1 else None},
           "percentile_rule": (c.get("meta") or {}).get("percentile"), "arms": {}}
    problems, measured = [], 0
    for arm, spec in (c.get("sources") or {}).items():
        path = amap.get(arm) or spec.get("audit_file")
        if not path:
            rep["arms"][arm] = {"status": "BLOCKED", "why": spec.get("reason") or "未声明审计链"}
            continue
        if not os.path.isfile(path):
            rep["arms"][arm] = {"status": "BLOCKED", "why": "审计链不在场：%s" % path}
            continue
        vals, rows, n, skipped = read_audit(path, spec, w0, w1)
        if not vals:
            rep["arms"][arm] = {"status": "BLOCKED", "why": "窗口内没有 per-call 延迟读数（n_lines=%d）" % n}
            continue
        measured += 1
        rep["arms"][arm] = {
            "status": "PASS",
            "n_samples": len(vals),
            "query_latency_p50": round(percentile(vals, 50.0), 3),
            "query_latency_p95": round(percentile(vals, 95.0), 3),
            "facts_consumed": int(rows),
            "granularity": "arm（审计链无 case_id；见 OQ-M6M-01）",
            "source_file": path,
            "n_lines": n, "parse_skipped": skipped,
        }
    rep["problems"] = problems
    rep["measured_arms"] = measured
    if measured == 0:
        return BLOCKED, rep
    return (FAIL if problems else PASS), rep


FIX_KG = '{"tool":"cc_trace","queryLatencyMs":54.34,"rowCount":17,"status":"ok","startedAt":"2026-10-08T12:05:01Z"}\n' \
         '{"tool":"cc_cause","queryLatencyMs":100.0,"rowCount":3,"status":"ok","startedAt":"2026-10-08T12:06:01Z"}\n' \
         '{"tool":"cc_cause","queryLatencyMs":200.0,"rowCount":0,"status":"error","startedAt":"2026-10-08T12:07:01Z"}\n'
FIX_ONTO = '{"tool":"cc_cause","elapsedMs":101.888,"rowCount":0,"status":"ok","startedAt":"2026-10-09T02:18:11.101Z"}\n' \
           '{"tool":"cc_guide","elapsedMs":10.0,"rowCount":5,"status":"ok","startedAt":"2026-10-09T02:19:11Z"}\n'


def selftest():
    ok = total = 0
    # 1) 百分位最近秩：3 个值 {54.34,100,200} 的 P50 ⇒ index=ceil(0.5*3)-1=1 ⇒ 100；P95 ⇒ 2 ⇒ 200
    total += 1
    if percentile([54.34, 100.0, 200.0], 50.0) == 100.0 and percentile([54.34, 100.0, 200.0], 95.0) == 200.0:
        ok += 1
    else:
        print("  ★ 百分位正控不符")
    # 2) KG 命名：status=error 的行必须被过滤；rows 只累计 ok 行
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "kg.jsonl")
        io.open(p, "w", encoding="utf-8", newline="\n").write(FIX_KG)
        spec = {"latency_key": "queryLatencyMs", "rows_key": "rowCount", "ok_key": "status",
                "ok_value": "ok", "time_key": "startedAt"}
        vals, rows, n, sk = read_audit(p, spec)
        total += 1
        if vals == [54.34, 100.0] and rows == 20 and n == 3:
            ok += 1
        else:
            print("  ★ ok 过滤负控不符：vals=%s rows=%s n=%s" % (vals, rows, n))
        # 3) 窗口过滤
        total += 1
        vals2, _r, _n, _s = read_audit(p, spec, w0=_ts("2026-10-08T12:05:30Z"))
        if vals2 == [100.0]:
            ok += 1
        else:
            print("  ★ 窗口过滤负控不符：%s" % vals2)
        # 4) 两套命名都要能读（ONTO 用 elapsedMs）
        p2 = os.path.join(td, "onto.jsonl")
        io.open(p2, "w", encoding="utf-8", newline="\n").write(FIX_ONTO)
        spec2 = {"latency_key": "elapsedMs", "rows_key": "rowCount", "ok_key": "status",
                 "ok_value": "ok", "time_key": "startedAt"}
        v3, r3, _n3, _s3 = read_audit(p2, spec2)
        total += 1
        if sorted(v3) == [10.0, 101.888] and r3 == 5:   # 按文件顺序返回 ⇒ 比较前排序
            ok += 1
        else:
            print("  ★ ONTO 命名负控不符：%s" % v3)
    # 5) 缺件 ⇒ BLOCKED 而非 0（端到端）
    code, rep = collect(audit_map={"cc-kg-agent": "/nonexistent/x.jsonl"})
    total += 1
    if code == BLOCKED and rep["measured_arms"] == 0:
        ok += 1
    else:
        print("  ★ 缺件负控不符：code=%s measured=%s" % (code, rep.get("measured_arms")))
    print("  SELFTEST %d/%d 项通过" % (ok, total))
    return PASS if ok == total else FAIL


def _window_from_answers(path):
    lo = hi = None
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = _ts(d.get("captured_at_utc"))
            if not t:
                continue
            lo = t if lo is None or t < lo else lo
            hi = t if hi is None or t > hi else hi
    return lo, hi


def main(argv=None):
    ap = argparse.ArgumentParser(description="M6.3 容器侧效率指标采集器（口径见 shared/model/m6-carrier-metrics.yaml）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--answers", default="", help="从该 answers.jsonl 推导时间窗口")
    ap.add_argument("--window-start", default="")
    ap.add_argument("--window-end", default="")
    ap.add_argument("--audit-map", default="", help="JSON：{臂名: 本地审计链路径} —— 覆盖契约里的路径（离线/异地用）")
    ap.add_argument("--contract", default="", help="契约路径（默认 <repo>/shared/model/m6-carrier-metrics.yaml；异地运行用）")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    amap = {}
    if a.audit_map:
        amap = json.loads(io.open(a.audit_map, encoding="utf-8").read())
    w0, w1 = _ts(a.window_start), _ts(a.window_end)
    if a.answers:
        lo, hi = _window_from_answers(a.answers)
        w0, w1 = (lo, hi) if lo else (w0, w1)
    code, rep = collect(amap, w0, w1, a.contract or None)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        with io.open(a.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rep, ensure_ascii=False, indent=1))
    if code == BLOCKED:
        print("  BLOCKED（≠ FAIL）：一臂都没量到 ⇒ 表 6 该列应显示 BLOCKED，不得填 0")
    elif code == FAIL:
        print("  FAIL：量到了但口径不符 —— %s" % rep.get("problems"))
    else:
        print("  PASS：量到 %d 臂（粒度 = 臂级；见 OQ-M6M-01）" % rep["measured_arms"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
