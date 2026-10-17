#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5 判决集 · 合成夹具自测与反作弊负控（``m5_judge.py --selftest`` 的实现）。

★ 全部数据都是**合成夹具**，每条用例与每个夹具文件都显式带 ``synthetic: true``；
  **绝不**冒充真实用例、真实答案或真实故障注入（docs/06 §1.4 步骤 1 / §9 U7：
  真机注入工具由用户自研，本项目不构造模拟故障数据）。

六个反作弊负控（任务书 ①–⑥，**每个都记录真实返回码与证据**）：
  ① `什么都说` 合成答案 ⇒ ``false_claim_rate`` > 0 且族C5 正常态不得高分
  ② `错误答案` ⇒ 总分显著低（docs/06 §1.4 步骤 4 / §5 G6）；并给 G6 区分度读数
  ③ `标准答案` ⇒ 必须满分
  ④ abstain 答案只进 abstain 列（既不计对也不计错）
  ⑤ 配对 vs 独立样本口径不同（不得把配对数据当独立样本检验）；配对子集为空 ⇒ BLOCKED ≠ FAIL
  ⑥ 两组完全等价 ⇒ 结论必须是「未检出显著差异」，且报告正文零禁用表述

另有补充负控 ⑦–⑯（fail-closed 牙齿、辅助通道、可复现性、报告审查牙齿等）。

退出码：``0`` 全 PASS / ``1`` 存在 FAIL / ``2`` 无 FAIL 但存在 BLOCKED。
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import yaml

from . import contract as C, judge as J, reporting as R, stats as S

FIXED_TS = "2026-10-07T00:00:00Z"

# ── 合成故障模式（层取值 = docs/06 §2.3 的 App/Runtime/Data）────────────────
MODES = [
    {"code": "FM-SYN-APP-THREAD-POOL-EXHAUSTED", "layer": "App",
     "cn": "应用线程池耗尽", "metric": "app.pool.active_threads",
     "pos": ["线程池", "活跃线程数", "排队长度"], "obj": "inst:app-cc-01"},
    {"code": "FM-SYN-DB-SLOW-QUERY", "layer": "Data",
     "cn": "数据库慢查询", "metric": "db.query.mean_exec_ms",
     "pos": ["慢查询", "会话占用", "执行耗时"], "obj": "inst:db-cc-01"},
    {"code": "FM-SYN-RT-MEM-PRESSURE", "layer": "Runtime",
     "cn": "容器内存压力过高", "metric": "container.mem.available_mb",
     "pos": ["内存压力", "容器内存", "页回收"], "obj": "inst:rt-cc-01"},
    {"code": "FM-SYN-DB-POOL-FULL", "layer": "Data",
     "cn": "数据库连接池占满", "metric": "db.session.active",
     "pos": ["连接池", "会话数", "等待队列"], "obj": "inst:db-cc-01"},
    {"code": "FM-SYN-RT-DISK-IO-SATURATED", "layer": "Runtime",
     "cn": "宿主机磁盘IO饱和", "metric": "host.disk.io_util",
     "pos": ["磁盘IO", "iowait", "队列深度"], "obj": "inst:rt-cc-01"},
    {"code": "FM-SYN-APP-ENDPOINT-SLOW", "layer": "App",
     "cn": "应用端点响应缓慢", "metric": "app.endpoint.p99_ms",
     "pos": ["端点", "响应时间", "P99"], "obj": "inst:app-cc-01"},
    {"code": "FM-SYN-DB-INSTANCE-DOWN", "layer": "Data",
     "cn": "数据库实例下线", "metric": "db.instance.up",
     "pos": ["实例下线", "主备切换", "连接失败"], "obj": "inst:db-cc-01"},
]
ALIAS = {"App": "应用层", "Runtime": "运行时", "Data": "数据库层"}
WRONG_ALIAS = {"App": "运行时", "Runtime": "应用层", "Data": "运行时"}

# 每条故障链的传播路径（= 注入顺序 ⋈ 因果边模型的合成版）
PATHS = {
    0: [0, 5], 1: [1, 3, 5], 2: [2, 5], 3: [3, 5],
    4: [4, 1, 5], 5: [5], 6: [6, 5],
}

NEG_PHRASES = [
    "应用自身内存泄漏", "宿主机CPU争抢", "容器OOM", "应用线程枯竭",
    "磁盘坏道", "网络抖动", "上游限流", "代码热更新",
    "数据库主键冲突", "应用GC停顿",
]

NOR_PHRASES = ["宿主机CPU空闲", "实例连接数充裕", "磁盘健康度正常"]

TYPE_BY_FAMILY = {"C1": "单层定位", "C2": "跨层定界", "C3": "同症状区分",
                  "C4": "否定性证据", "C5": "正常态", "C6": "时间对齐"}

# (族, 前缀, 条数, 难度, 模式索引循环) —— 刻意满足任务书强制的**边际**构成
# 族 14/14/6/6/6/4、层 16/20/14（★ docs/06 §1.3 的族→层交叉表本身不自洽，见报告 §5）
LAYOUT = [
    ("C1", "A", 14, "easy", [0, 1, 2, 3, 4, 5, 6, 0, 1, 2, 3, 4, 5, 6]),
    ("C2", "B", 14, "mixed", [0, 2, 3, 5, 0, 2, 3, 5, 1, 4, 6, 1, 4, 6]),
    ("C3", "D", 6, "medium", [1, 3, 5, 0, 2, 6]),
    ("C4", "N", 6, "mixed2", [1, 3, 4, 2, 6, 0]),
    ("C5", "Z", 6, "mixed3", [None] * 6),
    ("C6", "T", 4, "hard", [6, 1, 4, 2]),
]
# LAYOUT 的难度切分（族C2 前 8 = medium 的 F1/F3/F4/F6，后 6 = hard 的 F2/F5/F7；
# 族C4 前 2 medium、后 4 hard；族C5 前 2 easy、后 4 medium）⇒ 16/20/14
DIFF_SPLIT = {"C2": (8, "medium", "hard"), "C4": (2, "medium", "hard"),
              "C5": (2, "easy", "medium")}


def _ccf(n: int) -> str:
    return "ccf-20261001-%06d" % n


def _diff_for(family, idx, n):
    if family in DIFF_SPLIT:
        cut, d1, d2 = DIFF_SPLIT[family]
        return d1 if idx < cut else d2
    for fam, _p, _n, diff, _m in LAYOUT:
        if fam == family:
            return diff
    raise KeyError(family)


def build_cases():
    """生成 50 条合成用例（显式 synthetic）。"""
    cases, ccf = [], 100
    for family, prefix, n, _diff, modes in LAYOUT:
        for i in range(n):
            mid = modes[i]
            cid = f"{prefix}-{i + 1:02d}"
            diff = _diff_for(family, i, n)
            ccf += 1
            if mid is None:  # 族C5 正常态：无故障
                cases.append({
                    "id": cid, "cls": prefix, "chain": "F0",
                    "scope": ["App", "Data", "Runtime"],
                    "type": TYPE_BY_FAMILY[family],
                    "query": (f"（合成夹具）2026-10-01T14:0{i}:00Z 窗口内，信用卡支付交易各层指标"
                              f"均未出现异常。请判断是否存在故障；若不存在请说明依据。"),
                    "injected": [],
                    "expect_root_cause": C.NONE_SENTINEL,
                    "expect_layer": C.NONE_SENTINEL,
                    "expect_path": [], "expect_objects": [],
                    "pos": [],
                    "neg": [NEG_PHRASES[(i * 3) % len(NEG_PHRASES)],
                            NEG_PHRASES[(i * 3 + 1) % len(NEG_PHRASES)]],
                    "nor": list(NOR_PHRASES),
                    "evidence_facts": [_ccf(ccf)],
                    "detectors": [r"各项指标正常|未发现异常|无故障"],
                    "difficulty": diff, "discriminator": None, "synthetic": True,
                })
                continue
            m = MODES[mid]
            path = [MODES[k]["code"] for k in PATHS[mid]]
            cases.append({
                "id": cid, "cls": prefix,
                "chain": f"F{mid + 1}",
                "scope": sorted({m["layer"], "App"}),
                "type": TYPE_BY_FAMILY[family],
                "query": (f"（合成夹具）信用卡支付交易自 14:20 起 P99 由 180ms 升至 "
                          f"{4200 + 137 * i}ms，{m['cn']}相关指标告警。请定位根因、给出依据，"
                          f"并说明影响范围。"),
                "injected": [m["code"]] + ([] if len(path) < 2 else []),
                "expect_root_cause": m["code"],
                "expect_layer": m["layer"],
                "expect_path": path,
                "expect_objects": [m["obj"]],
                "pos": list(m["pos"]),
                "neg": [NEG_PHRASES[(i * 3) % len(NEG_PHRASES)],
                        NEG_PHRASES[(i * 3 + 1) % len(NEG_PHRASES)]],
                "nor": [],
                "evidence_facts": [_ccf(ccf)],
                "detectors": [re.escape(m["cn"]), m["metric"]],
                "difficulty": diff,
                "discriminator": (m["metric"] if family == "C3" else None),
                "synthetic": True,
            })
    meta = {"synthetic": True,
            "synthetic_note": ("合成夹具：模式编码以 FM-SYN- 开头、实例 id 均为虚构；"
                               "仅用于自测判据与统计，不得当作真实用例（docs/06 §1.4 步骤 1）"),
            "total": len(cases)}
    return cases, meta


# ── 合成答案生成器 ─────────────────────────────────────────────────────────

def _path_of(case):
    return list(case["expect_path"])


def ans_oracle(case):
    if case["expect_root_cause"] == C.NONE_SENTINEL:
        return (f"未发现异常；各项指标正常。证据事实：{', '.join(case['evidence_facts'])}。"
                f"本次窗口内无故障模式被触发，各层均处于正常态。")
    m = next(x for x in MODES if x["code"] == case["expect_root_cause"])
    return (f"根因 = {case['expect_root_cause']}；所在层 = {ALIAS[m['layer']]}；"
            f"传播路径：{' → '.join(_path_of(case))}；"
            f"涉及对象：{', '.join(case['expect_objects'])}；"
            f"关键指标：{', '.join(case['pos'])}（{m['metric']}）；"
            f"证据事实：{', '.join(case['evidence_facts'])}。")


def ans_nl(case):
    """只用自然语言（不含故障模式编码）⇒ 只能靠 detectors 辅助通道判。"""
    if case["expect_root_cause"] == C.NONE_SENTINEL:
        return (f"本次窗口未发现异常，各项指标正常（各层均为正常态）。"
                f"证据事实：{', '.join(case['evidence_facts'])}。")
    m = next(x for x in MODES if x["code"] == case["expect_root_cause"])
    return (f"根因是{m['cn']}，位于{ALIAS[m['layer']]}；"
            f"关键指标：{', '.join(case['pos'])}（{m['metric']}）；"
            f"涉及对象：{', '.join(case['expect_objects'])}；"
            f"证据事实：{', '.join(case['evidence_facts'])}。")


def _other_code(case, vocab_codes):
    for code in vocab_codes:
        if code != case["expect_root_cause"] and code not in case["expect_path"]:
            return code
    return vocab_codes[0]


def ans_wrong(case, vocab_codes):
    """错误答案：断言 neg 错误归因 + 错误层 + 错误路径编码（G6 的反例）。"""
    m = None
    if case["expect_root_cause"] != C.NONE_SENTINEL:
        m = next(x for x in MODES if x["code"] == case["expect_root_cause"])
    wrong_alias = WRONG_ALIAS.get(m["layer"], "应用层") if m else "应用层"
    return (f"根因编码 {_other_code(case, vocab_codes)}，位于{wrong_alias}；"
            f"判定依据为{case['neg'][0]}。")


def ans_all_yes(cases, vocab_codes):
    """「什么都说」：断言所有故障编码 + 所有 pos/neg 词 + 所有层别名 + 所有证据 id。"""
    parts = ["；".join(vocab_codes)]
    parts.append("；".join(p for c in cases for p in c["pos"]))
    parts.append("；".join(p for c in cases for p in c["neg"]))
    parts.append("；".join(ALIAS.values()))
    parts.append("；".join(i for c in cases for i in c["evidence_facts"]))
    return "；".join(parts) + "。"


def build_answers(cases, vocab_codes, *, groups, repeats=(1,), abstain_ids=()):
    """按组生成记录（question 逐字等于用例 query ⇒ 不触发 W-ANS-QUESTION-MISMATCH）。"""
    out = []
    all_yes = ans_all_yes(cases, vocab_codes)
    for g in groups:
        for rep in repeats:
            for c in cases:
                if g == "oracle":
                    text, ab = ans_oracle(c), False
                elif g == "nl":
                    text, ab = ans_nl(c), False
                elif g == "wrong":
                    text, ab = ans_wrong(c, vocab_codes), False
                elif g == "all_yes":
                    text, ab = all_yes, False
                elif g in ("equiv-a", "equiv-b", "unpaired"):
                    text, ab = ans_oracle(c), False
                elif g == "abstain":
                    if c["id"] in abstain_ids:
                        # ★ 陷阱：其中最后一条给「正确答案文本」但 abstain=true
                        text = ans_oracle(c)
                        ab = True
                    else:
                        text, ab = ans_oracle(c), False
                else:
                    raise ValueError(g)
                out.append({
                    "case_id": c["id"], "group": g, "repeat": rep,
                    "question": c["query"], "answer_text": text,
                    "abstain": ab, "elapsed_s": round(10 + 0.5 * len(text) / 100, 3),
                    "ttft_s": 1.2, "input_tokens": 800 + len(text),
                    "output_tokens": len(text) // 3,
                    "tool_events": [{"name": "cc_query", "status": "ok"}],
                    "snapshot_id": "ccf-syn-0001",
                    "captured_at_utc": "2026-10-07T00:00:00Z",
                    "source": "synthetic",
                    "synthetic": True,
                })
    return out


# ── 夹具落盘 ───────────────────────────────────────────────────────────────

def _dump_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8", newline="\n")


def _dump_yaml(path, doc):
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                                   default_flow_style=False),
                    encoding="utf-8", newline="\n")


def write_fixtures(out_dir: Path):
    cases, meta = build_cases()
    vocab = J.build_vocabulary(cases)
    codes = vocab["codes"]
    f = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    f["cases"] = out_dir / "cases_syn.yaml"
    _dump_yaml(f["cases"], {"meta": meta, "cases": cases})

    # 主答案集：oracle / nl / wrong / all_yes / equiv-a / equiv-b / abstain / unpaired
    # ★ 陷阱：A-03 给「正确答案文本」但 abstain=true ⇒ 必须只进 abstain 列，不得计对
    abstain_ids = ["A-01", "A-02", "A-03", "B-01", "D-01", "N-01", "Z-01"]
    recs = build_answers(cases, codes, groups=["oracle", "nl", "wrong", "all_yes",
                                               "equiv-a", "equiv-b"], repeats=(1,))
    recs += build_answers(cases, codes, groups=["abstain"], repeats=(1,),
                          abstain_ids=abstain_ids)
    # 未配对组：只跑 repeat=2 ⇒ 与 repeat=1 的组配对子集为空（BLOCKED 证据）
    recs += build_answers(cases, codes, groups=["unpaired"], repeats=(2,))
    f["answers"] = out_dir / "answers.jsonl"
    _dump_jsonl(f["answers"], recs)

    # 3 次重复（docs/06 §4 P8）
    r3 = build_answers(cases, codes, groups=["oracle", "wrong"], repeats=(1, 2, 3))
    f["answers_r3"] = out_dir / "answers_r3.jsonl"
    _dump_jsonl(f["answers_r3"], r3)

    # ── 破损夹具（fail-closed 牙齿）
    def broken_yaml(key, mutate, name):
        doc = copy.deepcopy({"meta": meta, "cases": cases})
        mutate(doc["cases"])
        p = out_dir / name
        _dump_yaml(p, doc)
        f[key] = p

    def _drop_detectors(cs):
        next(c for c in cs if c["id"] == "A-05")["detectors"] = []

    def _dup_id(cs):
        next(c for c in cs if c["id"] == "A-06")["id"] = "A-05"

    def _bad_difficulty(cs):
        next(c for c in cs if c["id"] == "A-07")["difficulty"] = "trivial"

    def _bad_cls(cs):
        next(c for c in cs if c["id"] == "A-08")["cls"] = "C9"

    def _missing_rootcause(cs):
        del next(c for c in cs if c["id"] == "A-09")["expect_root_cause"]

    broken_yaml("cases_no_detectors", _drop_detectors, "cases_no_detectors.yaml")
    broken_yaml("cases_dup_id", _dup_id, "cases_dup_id.yaml")
    broken_yaml("cases_bad_difficulty", _bad_difficulty, "cases_bad_difficulty.yaml")
    broken_yaml("cases_bad_cls", _bad_cls, "cases_bad_cls.yaml")
    broken_yaml("cases_no_rootcause", _missing_rootcause, "cases_no_rootcause.yaml")

    p49 = out_dir / "cases_49.yaml"
    _dump_yaml(p49, {"meta": meta, "cases": cases[:-1]})
    f["cases_49"] = p49

    cb = copy.deepcopy(cases)
    next(c for c in cb if c["id"] == "A-01")["difficulty"] = "hard"
    pcb = out_dir / "cases_counts_broken.yaml"
    _dump_yaml(pcb, {"meta": meta, "cases": cb})
    f["cases_counts_broken"] = pcb

    am = [dict(r) for r in recs if r["group"] == "oracle"]
    del am[0]["answer_text"]
    f["answers_missing_key"] = out_dir / "answers_missing_key.jsonl"
    _dump_jsonl(f["answers_missing_key"], am)

    ad = [dict(r) for r in recs if r["group"] == "oracle"]
    ad.append(dict(ad[0]))
    f["answers_dup"] = out_dir / "answers_dup_record.jsonl"
    _dump_jsonl(f["answers_dup"], ad)

    return f, cases, meta


# ── 断言与负控执行 ─────────────────────────────────────────────────────────

def A(name, ok, expected, actual):
    return {"name": name, "expected": expected, "actual": actual,
            "status": "PASS" if ok else "FAIL"}


def _as_dict(x):
    if isinstance(x, dict):
        return x
    if hasattr(x, "as_dict"):
        return x.as_dict()
    return {"code": "E-UNKNOWN", "message": str(x)}


def _run(run_pipeline, cases, answers, out, *, groups=None, expect_full_suite=False,
         strict=False, diag=None):
    gen = None
    if diag is not None:
        diag.clear()
        gen = diag
    rc = run_pipeline(cases, answers, out, groups=groups, strict=strict,
                      expect_full_suite=expect_full_suite, generated_at=FIXED_TS,
                      quiet=True, diag=gen)
    return rc


def _codes(diag):
    return sorted({_as_dict(e).get("code") for e in (diag or {}).get("errors", [])})


def _load(out, name):
    p = Path(out) / name
    if not p.exists():
        return None
    if name.endswith(".jsonl"):
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if name.endswith(".json"):
        return json.loads(p.read_text(encoding="utf-8"))
    return p.read_text(encoding="utf-8")


# ── 主入口 ─────────────────────────────────────────────────────────────────

def run_selftest(*, run_pipeline, out_dir, repo_root):
    out_dir = Path(out_dir)
    fx, cases, meta = write_fixtures(out_dir)
    controls = []
    print(f"M5 --selftest：合成夹具 {len(cases)} 条 → {out_dir}")
    print(f"  cases={fx['cases']}")
    print(f"  answers={fx['answers']}")

    def record(cid, title, rc, exp_rc, asserts, evidence=None, note=""):
        status = "PASS"
        if any(a["status"] == "FAIL" for a in asserts):
            status = "FAIL"
        elif rc != exp_rc:
            status = "FAIL"
        controls.append({"id": cid, "title": title, "exit_code": rc,
                         "expected_exit_code": exp_rc, "status": status,
                         "assertions": asserts, "evidence": evidence or {},
                         "note": note})
        bad = [a for a in asserts if a["status"] == "FAIL"]
        print(f"  [{cid}] exit={rc}/{exp_rc} {status}"
              f"{'' if not bad else '  ← ' + '; '.join(a['name'] for a in bad)}")
        return controls[-1]

    diag = {}
    mk = lambda name: out_dir / name  # noqa: E731

    # ── ③ 标准答案必须满分 ────────────────────────────────────────────────
    rc = _run(run_pipeline, fx["cases"], fx["answers"], mk("run_primary"),
              groups=["oracle", "wrong", "nl", "all_yes", "abstain"],
              expect_full_suite=True, diag=diag)
    m = _load(mk("run_primary"), "metrics.json")
    st = _load(mk("run_primary"), "stats.json")
    ver = _load(mk("run_primary"), "verdicts.jsonl")
    ok_rc = rc == 0
    if not m or not st:
        record("N13", "主运行产出", rc, 0, [A("产出存在", False, "4 文件", "缺失")])
        return _finish(controls, out_dir)
    om = m["groups"]["oracle"]["metrics"]
    a3 = [
        A("退出码", ok_rc, 0, rc),
        A("--expect-full-suite 通过（50 条 + 边际构成）", True, "无 E-SUITE-*", _codes(diag)),
        A("0 WARN（干净夹具）", m["validation"]["warnings"] == [], [],
          m["validation"]["warnings"]),
        A("标准答案 root_cause_accuracy=1.0", om["root_cause_accuracy"]["rate"] == 1.0,
          1.0, om["root_cause_accuracy"]["rate"]),
        A("标准答案 layer_accuracy=1.0", om["layer_accuracy"]["rate"] == 1.0, 1.0,
          om["layer_accuracy"]["rate"]),
        A("标准答案 path_f1=1.0", om["path_f1"]["rate"] == 1.0, 1.0, om["path_f1"]["rate"]),
        A("标准答案 object_accuracy=1.0", om["object_accuracy"]["rate"] == 1.0, 1.0,
          om["object_accuracy"]["rate"]),
        A("标准答案 evidence_accuracy=1.0", om["evidence_accuracy"]["rate"] == 1.0, 1.0,
          om["evidence_accuracy"]["rate"]),
        A("标准答案 mixed_precision=1.0", om["mixed_precision"]["rate"] == 1.0, 1.0,
          om["mixed_precision"]["rate"]),
        A("标准答案 false_claim_rate=0.0", om["false_claim_rate"]["rate"] == 0.0, 0.0,
          om["false_claim_rate"]["rate"]),
        A("标准答案 abstain_rate=0.0", om["abstain_rate"]["rate"] == 0.0, 0.0,
          om["abstain_rate"]["rate"]),
    ]
    for d in C.DIFFICULTY_ORDER:
        a3.append(A(f"标准答案[{d}] root_cause=1.0",
                    m["groups"]["oracle"]["by_difficulty"][d]["metrics"]
                    ["root_cause_accuracy"]["rate"] == 1.0, 1.0,
                    m["groups"]["oracle"]["by_difficulty"][d]["metrics"]
                    ["root_cause_accuracy"]["rate"]))
    for fam in C.FAMILY_ORDER:
        a3.append(A(f"标准答案[族{fam}] root_cause=1.0",
                    m["groups"]["oracle"]["by_family"][fam]["metrics"]
                    ["root_cause_accuracy"]["rate"] == 1.0, 1.0,
                    m["groups"]["oracle"]["by_family"][fam]["metrics"]
                    ["root_cause_accuracy"]["rate"]))
    record("N03", "③ 标准答案必须满分（docs/06 §1.4 步骤 4）", rc, 0, a3,
           evidence={"oracle_metrics": om})

    # ── ① 什么都说 ────────────────────────────────────────────────────────
    ay = m["groups"]["all_yes"]["metrics"]
    c5 = m["groups"]["all_yes"]["by_family"]["C5"]["metrics"]
    a1 = [
        A("false_claim_rate > 0", (ay["false_claim_rate"]["rate"] or 0) > 0, ">0",
          ay["false_claim_rate"]["rate"]),
        A("族C5 正常态 root_cause_accuracy 不得高分（≤0.2）",
          (c5["root_cause_accuracy"]["rate"] or 0) <= 0.2, "<=0.2",
          c5["root_cause_accuracy"]["rate"]),
        A("族C5 fabrication_rate=1.0（断言了故障模式）",
          c5["fabrication_rate"]["rate"] == 1.0, 1.0, c5["fabrication_rate"]["rate"]),
        A("族C5 false_claim_rate=1.0", c5["false_claim_rate"]["rate"] == 1.0, 1.0,
          c5["false_claim_rate"]["rate"]),
        A("其总体 root_cause_accuracy 明显低于标准答案",
          (ay["root_cause_accuracy"]["rate"] or 0) < om["root_cause_accuracy"]["rate"],
          "< oracle", ay["root_cause_accuracy"]["rate"]),
    ]
    record("N01", "① 「什么都说」⇒ false_claim_rate>0 且族C5 不得高分", rc, 0, a1,
           evidence={"all_yes_overall": ay, "all_yes_C5": c5})

    # ── ② 错误答案显著低分 + G6 区分度 ───────────────────────────────────
    wm = m["groups"]["wrong"]["metrics"]
    cmp_ow = st["comparisons"]["root_cause_accuracy"]["oracle|wrong"]
    g6 = next(p for p in m["g6_discrimination"]["pairs"]
              if {p["group_a"], p["group_b"]} == {"oracle", "wrong"})
    d_ow = next(e for e in st["conclusion"]["discipline"]["per_comparison"]
                if e["metric"] == "root_cause_accuracy")
    a2 = [
        A("错误答案 root_cause_accuracy=0.0", wm["root_cause_accuracy"]["rate"] == 0.0, 0.0,
          wm["root_cause_accuracy"]["rate"]),
        A("错误答案 false_claim_rate=1.0", wm["false_claim_rate"]["rate"] == 1.0, 1.0,
          wm["false_claim_rate"]["rate"]),
        A("差值 ≥20pp", abs(cmp_ow["diff_pp_paired"]) >= 20.0, ">=20",
          cmp_ow["diff_pp_paired"]),
        A("McNemar p<0.05", (cmp_ow["p_value"] or 1) < 0.05, "<0.05", cmp_ow["p_value"]),
        A("p 方法 = McNemar 精确（二项）", cmp_ow["p_method"] == "mcnemar_exact_binomial",
          "mcnemar_exact_binomial", cmp_ow["p_method"]),
        A("不和谐对 b=50, c=0", (cmp_ow["b"], cmp_ow["c"]) == (50, 0), (50, 0),
          (cmp_ow["b"], cmp_ow["c"])),
        A("G6 零区分度用例数 = 0（用例不作废）", g6["n_zero_discrimination"] == 0, 0,
          g6["n_zero_discrimination"]),
        A("G6 最小区分度 > 0", (g6["min_gap"] or 0) > 0, ">0", g6["min_gap"]),
        A("G6 适用标志 g6_applicable=True（本对是标准答案 vs 错误答案）",
          g6.get("g6_applicable") is True, True, g6.get("g6_applicable")),
        A("G6 判定文案 = 通过（无零区分度用例）",
          g6["g6_verdict"] == J.G6_OK_LABEL, J.G6_OK_LABEL, g6["g6_verdict"]),
        A("★ 非 G6 适用对（含 abstain/nl）一律不得出现「作废」文案",
          all(("作废" not in p["g6_verdict"]) for p in m["g6_discrimination"]["pairs"]
              if not p.get("g6_applicable")),
          "0 处「作废」",
          [p["group_a"] + "|" + p["group_b"] for p in m["g6_discrimination"]["pairs"]
           if ("作废" in p["g6_verdict"] and not p.get("g6_applicable"))] or "0 处"),
        A("纪律结论 = 显著差异（|Δ|≥20pp ∧ p<α ∧ CI 不跨 0）",
          d_ow["conclusion"] == S.SIGNIFICANT_LABEL, S.SIGNIFICANT_LABEL,
          d_ow["conclusion"]),
    ]
    record("N02", "② 错误答案显著低分 + G6 区分度（§1.4 步骤 4 / §5 G6）", rc, 0, a2,
           evidence={"cmp": cmp_ow, "g6": g6, "discipline": d_ow})

    # ── ⑤ 配对 vs 独立样本 + 未配对 ⇒ BLOCKED ≠ FAIL ─────────────────────
    ind = st["pairing"]["independent_reference"]
    differ = [x for x in ind if x["differ"]]
    cross = st["pairing"]["internal_cross_check"]
    diag_u = {}
    rc_u = _run(run_pipeline, fx["cases"], fx["answers"], mk("run_unpaired"),
                groups=["oracle", "unpaired"], diag=diag_u)
    mu = _load(mk("run_unpaired"), "metrics.json")
    stu = _load(mk("run_unpaired"), "stats.json")
    cu = stu["comparisons"]["root_cause_accuracy"]["oracle|unpaired"]
    du = next(e for e in stu["conclusion"]["discipline"]["per_comparison"]
              if e["metric"] == "root_cause_accuracy")
    a5 = [
        A("所有比较 test_form=paired",
          all(c.get("test_form") == "paired" for per in st["comparisons"].values()
              for c in per.values()), "paired", "见 stats.json"),
        A("配对检验 p ≠ 独立样本 p（口径确实不同）", bool(differ), "≥1 处不同",
          [(x["pair"], x["paired_p_value"], x["independent_p_value"]) for x in ind]),
        A("独立样本口径只出现在 reference 里（method=two_proportion_z_pooled）",
          bool(ind) and ind[0]["independent_method"] == "two_proportion_z_pooled", True,
          ind[0]["independent_method"] if ind else None),
        A("内部交叉验证：本机二值配对 == compare_binary",
          all(c["agree"] for c in cross) and bool(cross), True,
          [(c["pair"], c["agree"]) for c in cross]),
        A("未配对运行退出码 0（BLOCKED 不是 FAIL）", rc_u == 0, 0, rc_u),
        A("未配对：配对子集为空", cu["status"] == "BLOCKED", "BLOCKED", cu["status"]),
        A("未配对：n_pairs=0", cu["n_pairs"] == 0, 0, cu["n_pairs"]),
        A("未配对：metrics.sections.stats = BLOCKED", mu["sections"]["stats"] == "BLOCKED",
          "BLOCKED", mu["sections"]["stats"]),
        A("未配对：metrics.status 仍为 PASS（无 FAIL）", mu["status"] == "PASS", "PASS",
          mu["status"]),
        A("未配对：纪律结论 = 未检出显著差异", du["conclusion"] == S.MANDATED_NO_DIFF_LABEL,
          S.MANDATED_NO_DIFF_LABEL, du["conclusion"]),
        A("未配对：产生 W-ANS-REPEAT-SPREAD 警告",
          "W-ANS-REPEAT-SPREAD" in {_as_dict(w).get("code")
                                    for w in mu["validation"]["warnings"]}, True,
          sorted({_as_dict(w).get("code") for w in mu["validation"]["warnings"]})),
    ]
    record("N05", "⑤ 配对≠独立口径；未配对 ⇒ BLOCKED（不得写成 FAIL）", rc_u, 0, a5,
           evidence={"independent_reference": ind, "cross_check": cross,
                     "unpaired_cmp": cu, "unpaired_sections": mu["sections"]})

    # ── ④ abstain 只进 abstain 列 ────────────────────────────────────────
    abm = m["groups"]["abstain"]["metrics"]
    trap_v = next(v for v in ver if v["group"] == "abstain" and v["case_id"] == "A-03"
                  and v["repeat"] == 1)
    a4 = [
        A("abstain_rate=0.14（7/50，固定分母）", abm["abstain_rate"]["rate"] == 7 / 50, 7 / 50,
          abm["abstain_rate"]["rate"]),
        A("主判据分子 = 43（陷阱记录的正确答案文本未被计入）",
          abm["root_cause_accuracy"]["numerator"] == 43, 43,
          abm["root_cause_accuracy"]["numerator"]),
        A("★ 主判据分母 = 50（固定分母 = 用例数；abstain 不从分母扣除）",
          abm["root_cause_accuracy"]["denominator"] == 50, 50,
          abm["root_cause_accuracy"]["denominator"]),
        A("★ 主判据 rate = 43/50 = 0.86（abstain 计未命中）",
          abm["root_cause_accuracy"]["rate"] == 43 / 50, 43 / 50,
          abm["root_cause_accuracy"]["rate"]),
        A("★ 负控（模拟回退到旧口径）：旧口径读数是 (43, 43)，新断言只在新口径下成立",
          (abm["root_cause_accuracy"]["numerator"],
           abm["root_cause_accuracy"]["denominator"]) != (43, 43), "≠(43, 43)",
          (abm["root_cause_accuracy"]["numerator"],
           abm["root_cause_accuracy"]["denominator"])),
        A("诊断列 rate_excluding_abstain = 43/43 = 1.0（旧口径降为诊断，不是主口径）",
          abm["root_cause_accuracy"]["rate_excluding_abstain"] == 1.0, 1.0,
          abm["root_cause_accuracy"]["rate_excluding_abstain"]),
        A("诊断列 denominator_excluding_abstain = 43",
          abm["root_cause_accuracy"]["denominator_excluding_abstain"] == 43, 43,
          abm["root_cause_accuracy"]["denominator_excluding_abstain"]),
        A("rate_abstain_as_error 与主口径 rate 恒等（同一尺度的另一种写法）",
          abm["root_cause_accuracy"]["rate_abstain_as_error"]
          == abm["root_cause_accuracy"]["rate"], "同值",
          abm["root_cause_accuracy"]["rate_abstain_as_error"]),
        A("缺记录 n_missing = 0（本组记录齐全）",
          abm["root_cause_accuracy"]["n_missing"] == 0, 0,
          abm["root_cause_accuracy"]["n_missing"]),
        A("abstain 数 n_abstain = 7（与缺记录分列，不混算）",
          abm["root_cause_accuracy"]["n_abstain"] == 7, 7,
          abm["root_cause_accuracy"]["n_abstain"]),
        A("陷阱记录 verdict: abstain=true", trap_v["abstain"] is True, True,
          trap_v["abstain"]),
        A("陷阱记录 verdict: _hit=None（不计对）", trap_v["_hit"] is None, None,
          trap_v["_hit"]),
        A("陷阱记录 answer_text 是正确的标准答案", trap_v["root_cause"]["expect"]
          == trap_v["root_cause"]["first_candidate"], "expect==first_candidate",
          (trap_v["root_cause"]["expect"], trap_v["root_cause"]["first_candidate"])),
    ]
    record("N04", "④ abstain 计未命中 + 固定分母（主口径已裁决，旧口径降诊断列）", rc, 0, a4,
           evidence={"abstain_metrics": abm, "trap_verdict": trap_v})

    # ── ★ ⑲ 跨件一致性断言：分母/abstain 主口径（W30；含 fail-closed 负控）────
    base_text = fx["cases"].read_text(encoding="utf-8")
    decl_ok = (
        "\n"
        "suite:\n"
        "  fixed_denominator:\n"
        "    value: 50\n"
        "    statement: \"分母固定 50（合成夹具声明）\"\n"
        "scoring:\n"
        "  denominator:\n"
        "    policy: fixed\n"
        "    value: 50\n"
        "  abstain:\n"
        "    main_axis:\n"
        "      denominator: \"固定分母 50（每条 repeat = 用例数）\"\n"
        "      treatment: \"abstain 计未命中：既不进分子，也不缩小分母\"\n"
        "    sensitivity:\n"
        "      required: true\n"
    )
    declared_ok = mk("cases_declared.yaml")
    declared_ok.write_text(base_text + decl_ok, encoding="utf-8", newline="\n")
    declared_bad = mk("cases_policy_bad.yaml")
    declared_bad.write_text(
        base_text + decl_ok.replace("固定分母 50（每条 repeat = 用例数）",
                                    "可用记录数（用例数 − abstain 数）"),
        encoding="utf-8", newline="\n")
    diag_d_ok, diag_d_bad = {}, {}
    rc_d_ok = _run(run_pipeline, declared_ok, fx["answers"], mk("run_denom_ok"),
                   groups=["oracle", "abstain"], expect_full_suite=True, diag=diag_d_ok)
    md_ok = _load(mk("run_denom_ok"), "metrics.json")
    rc_d_bad = _run(run_pipeline, declared_bad, fx["answers"], mk("run_denom_bad"),
                    groups=["oracle", "abstain"], expect_full_suite=True, diag=diag_d_bad)
    om_ok = ((md_ok or {}).get("groups", {}).get("abstain", {}).get("metrics", {})
             .get("root_cause_accuracy", {}))
    pol = (md_ok or {}).get("denominator_policy", {})
    bad_errs = [_as_dict(e) for e in (diag_d_bad.get("errors") or [])]
    a19 = [
        A("三处声明一致 ⇒ 退出码 0", rc_d_ok == 0, 0, rc_d_ok),
        A("用例文件声明的口径被回读（case_document.main_policy=fixed(declared)）",
          (pol.get("case_document") or {}).get("main_policy") == "fixed(declared)",
          "fixed(declared)", (pol.get("case_document") or {}).get("main_policy")),
        A("★ 注册契约三处声明被单独回读（contract.main_policy=fixed(declared)）",
          (pol.get("contract") or {}).get("main_policy") == "fixed(declared)",
          "fixed(declared)", (pol.get("contract") or {}).get("main_policy")),
        A("声明的固定分母值 = 50（契约 + 用例文件同值）",
          (pol.get("contract") or {}).get("suite_fixed_denominator") == 50
          and (pol.get("case_document") or {}).get("suite_fixed_denominator") == 50, 50,
          ((pol.get("contract") or {}).get("suite_fixed_denominator"),
           (pol.get("case_document") or {}).get("suite_fixed_denominator"))),
        A("★ 判据器实现口径 = fixed+abstain-as-miss（与契约声明一致）",
          pol.get("main_policy") == "fixed+abstain-as-miss", "fixed+abstain-as-miss",
          pol.get("main_policy")),
        A("★ 契约声明与本次用例数自洽（runtime_matches_contract）",
          (pol.get("contract") or {}).get("runtime_matches_contract") is True, True,
          (pol.get("contract") or {}).get("runtime_matches_contract")),
        A("固定分母贯穿到指标单元：abstain 组主分母 = 50",
          om_ok.get("denominator") == 50, 50, om_ok.get("denominator")),
        A("★ 负控：主口径声明改回「可用记录数」⇒ 退出码 1（fail-closed）",
          rc_d_bad == 1, 1, rc_d_bad),
        A("★ 负控：报出 E-DENOM-POLICY（不静默 PASS）",
          "E-DENOM-POLICY" in _codes(diag_d_bad), "E-DENOM-POLICY", _codes(diag_d_bad)),
        A("★ 负控：报错点名排除性措辞「可用记录数」",
          any("可用记录数" in str(e.get("message") or "") for e in bad_errs), True,
          [e.get("message") for e in bad_errs if e.get("code") == "E-DENOM-POLICY"]),
        A("★ 负控：正确措辞「既不进分子，也不缩小分母」不得被判成排除性（否定语境守卫）",
          rc_d_ok == 0 and (pol.get("case_document") or {}).get("declared") is True, True,
          (rc_d_ok, (pol.get("case_document") or {}).get("abstain_main_treatment"))),
    ]
    record("N19", "⑲ 跨件一致性：分母/abstain 主口径（三处声明 + fail-closed 负控）",
           rc_d_ok, 0, a19, evidence={"policy_info": pol, "bad_errors": bad_errs})

    # ── ★ ⑳ 记录缺失 ≠ abstain（分列、都计未命中、都不静默）────────────────
    all_recs = [json.loads(ln) for ln in
                fx["answers"].read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept_recs = [r for r in all_recs
                 if not (r.get("group") == "oracle" and r.get("case_id") == "A-01"
                         and r.get("repeat") == 1)]
    ans_missing = mk("answers_missing_record.jsonl")
    ans_missing.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept_recs),
        encoding="utf-8", newline="\n")
    diag_d_missing = {}
    rc_m = _run(run_pipeline, fx["cases"], ans_missing, mk("run_missing_record"),
                groups=["oracle", "abstain"], expect_full_suite=True, diag=diag_d_missing)
    mm = _load(mk("run_missing_record"), "metrics.json")
    om_m = ((mm or {}).get("groups", {}).get("oracle", {}).get("metrics", {})
            .get("root_cause_accuracy", {}))
    ab_m = ((mm or {}).get("groups", {}).get("abstain", {}).get("metrics", {})
            .get("root_cause_accuracy", {}))
    cov_m = (mm or {}).get("records_coverage", {}).get("oracle", {})
    warn_codes = sorted({_as_dict(w).get("code")
                         for w in (mm or {}).get("validation", {}).get("warnings", [])})
    a20 = [
        A("缺 1 条记录仍退出码 0（缺记录不是 FAIL）", rc_m == 0, 0, rc_m),
        A("★ 缺记录进分母不进分子：denominator=50、numerator=49",
          (om_m.get("denominator"), om_m.get("numerator")) == (50, 49), (50, 49),
          (om_m.get("denominator"), om_m.get("numerator"))),
        A("★ 主口径 rate = 49/50 = 0.98", om_m.get("rate") == 0.98, 0.98, om_m.get("rate")),
        A("★ 缺记录单列 n_missing = 1（不静默剔除）", om_m.get("n_missing") == 1, 1,
          om_m.get("n_missing")),
        A("★ 缺记录 ≠ abstain：n_abstain = 0", om_m.get("n_abstain") == 0, 0,
          om_m.get("n_abstain")),
        A("★ 对照：abstain 组 n_missing=0 且 n_abstain=7（两个计数互不混算）",
          (ab_m.get("n_missing"), ab_m.get("n_abstain")) == (0, 7), (0, 7),
          (ab_m.get("n_missing"), ab_m.get("n_abstain"))),
        A("★ 旧口径诊断列仍为 1.0（49/49），主口径不是它",
          om_m.get("rate_excluding_abstain") == 1.0, 1.0, om_m.get("rate_excluding_abstain")),
        A("records_coverage 显式报告缺 1 条与缺失用例 id",
          cov_m.get("n_missing") == 1 and cov_m.get("cases_without_record") == ["A-01"], 1,
          {"n_missing": cov_m.get("n_missing"),
           "cases_without_record": cov_m.get("cases_without_record")}),
        A("★ 报出 W-ANS-RECORD-MISSING（缺记录不被静默）",
          "W-ANS-RECORD-MISSING" in warn_codes, "W-ANS-RECORD-MISSING", warn_codes),
    ]
    record("N20", "⑳ 记录缺失 vs abstain：分列、都计未命中、都不静默", rc_m, 0, a20,
           evidence={"oracle_cell": om_m, "abstain_cell": ab_m, "coverage": cov_m,
                     "warning_codes": warn_codes})

    # ── ⑥ 完全等价 ⇒ 未检出显著差异，且报告正文零禁用表述 ──────────────
    diag_e = {}
    rc_e = _run(run_pipeline, fx["cases"], fx["answers"], mk("run_equiv"),
                groups=["equiv-a", "equiv-b"], expect_full_suite=True, diag=diag_e)
    ste = _load(mk("run_equiv"), "stats.json")
    me = _load(mk("run_equiv"), "metrics.json")
    rpt_e = _load(mk("run_equiv"), "report.md") or ""
    ce = ste["comparisons"]["root_cause_accuracy"]["equiv-a|equiv-b"]
    disc_e = ste["conclusion"]["discipline"]["per_comparison"]
    a6 = [
        A("点估计差值 = 0.00pp", ce["diff_pp_paired"] == 0.0, 0.0, ce["diff_pp_paired"]),
        A("McNemar 不和谐对 b=c=0", (ce["b"], ce["c"]) == (0, 0), (0, 0), (ce["b"], ce["c"])),
        A("p = 1.0", ce["p_value"] == 1.0, 1.0, ce["p_value"]),
        A("全部纪律结论 = 未检出显著差异",
          all(e["conclusion"] == S.MANDATED_NO_DIFF_LABEL for e in disc_e) and bool(disc_e),
          S.MANDATED_NO_DIFF_LABEL, sorted({e["conclusion"] for e in disc_e})),
        A("无一结论写成「显著差异」",
          not any(e["conclusion"] == S.SIGNIFICANT_LABEL for e in disc_e), False,
          [e["metric"] for e in disc_e if e["conclusion"] == S.SIGNIFICANT_LABEL]),
        A("报告正文禁用表述零命中", me["report_audit"]["forbidden_wording_hits"] == [], [],
          me["report_audit"]["forbidden_wording_hits"]),
        A("报告正文无「优于」/「更好」等优越性表述",
          ("优于" not in rpt_e) and ("更好" not in rpt_e), False,
          ("优于" in rpt_e, "更好" in rpt_e)),
        A("样本量提示「10pp 需 ~180 条」已机器化",
          ste["conclusion"]["sample_size"]["ten_pp_hint"] == S.TEN_PP_HINT, S.TEN_PP_HINT,
          ste["conclusion"]["sample_size"]["ten_pp_hint"]),
        A("§6.3 二项 CI 半宽复算 ≈0.14",
          ste["conclusion"]["sample_size"]["binomial_ci_matches_contract"] is True, True,
          ste["conclusion"]["sample_size"]["binomial_ci_halfwidth_at_p060_n50"]),
        A("纪律 policy.resolution_pp = 20",
          ste["conclusion"]["discipline"]["policy"]["resolution_pp"] == 20.0, 20.0,
          ste["conclusion"]["discipline"]["policy"]["resolution_pp"]),
    ]
    record("N06", "⑥ 两组完全等价 ⇒ 「未检出显著差异」", rc_e, 0, a6,
           evidence={"cmp": ce, "discipline": disc_e})

    # ── ⑮ 辅助通道（detectors）有效性 ───────────────────────────────────
    nl = m["groups"]["nl"]["metrics"]
    a15 = [A("纯自然语言答案 root_cause_accuracy=1.0（靠 detectors 通道）",
             nl["root_cause_accuracy"]["rate"] == 1.0, 1.0,
             nl["root_cause_accuracy"]["rate"]),
           A("层判定（最近候选别名）也满分", nl["layer_accuracy"]["rate"] == 1.0, 1.0,
             nl["layer_accuracy"]["rate"])]
    record("N15", "⑮ detectors 辅助通道对自然语言表述有效", rc, 0, a15,
           evidence={"nl_metrics": nl})

    # ── ⑦–⑫ fail-closed 牙齿（期望 exit=1 + 具体 ERROR 码）───────────────
    teeth = [
        ("N07", "缺 answer_text 键 ⇒ ERROR", fx["answers_missing_key"], fx["cases"],
         ["oracle"], [], "E-ANS-MISSING-KEY"),
        ("N08", "重复 (case_id,group,repeat) ⇒ ERROR", fx["answers_dup"], fx["cases"],
         ["oracle"], [], "E-ANS-DUPLICATE"),
        ("N09", "detectors 为空 ⇒ ERROR", fx["answers"], fx["cases_no_detectors"],
         ["oracle"], [], "E-CASE-DETECTORS-EMPTY"),
        ("N10", "未知 difficulty ⇒ ERROR", fx["answers"], fx["cases_bad_difficulty"],
         ["oracle"], [], "E-CASE-DIFFICULTY-UNKNOWN"),
        ("N11", "cls 不在 C1–C6 ⇒ ERROR", fx["answers"], fx["cases_bad_cls"],
         ["oracle"], [], "E-CASE-CLS-UNKNOWN"),
        ("N11b", "缺 expect_root_cause ⇒ ERROR", fx["answers"], fx["cases_no_rootcause"],
         ["oracle"], [], "E-CASE-MISSING-FIELD"),
        ("N11c", "id 重复 ⇒ ERROR", fx["answers"], fx["cases_dup_id"],
         ["oracle"], [], "E-CASE-ID-DUPLICATE"),
        ("N12", "--expect-full-suite: 49 条 ⇒ ERROR", fx["answers"], fx["cases_49"],
         ["oracle"], ["--expect-full-suite"], "E-SUITE-TOTAL"),
        ("N12b", "--expect-full-suite: 分层 15/21/14 ⇒ ERROR", fx["answers"],
         fx["cases_counts_broken"], ["oracle"], ["--expect-full-suite"],
         "E-SUITE-DIFFICULTY-COUNTS"),
    ]
    for cid, title, ans_p, cases_p, grp, flags, want in teeth:
        d = {}
        rc_t = run_pipeline(cases_p, ans_p, mk(f"run_{cid.lower()}"), groups=grp,
                            strict=False,
                            expect_full_suite=("--expect-full-suite" in flags),
                            generated_at=FIXED_TS, quiet=True, diag=d)
        got = _codes(d)
        record(cid, f"⑦–⑫ fail-closed：{title}", rc_t, 1,
               [A("退出码=1（fail-closed）", rc_t == 1, 1, rc_t),
                A(f"含 {want}", want in got, want, got)],
               evidence={"error_codes": got})

    # ── ⑬ 真实进程退出码（端到端 subprocess）────────────────────────────
    e2e_dir = mk("run_e2e")
    cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "m5_judge.py"),
           "--cases", str(fx["cases"]), "--answers", str(fx["answers"]),
           "--out", str(e2e_dir), "--groups", "oracle,wrong", "--expect-full-suite"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=1800)
        rc13 = p.returncode
        a13 = [A("真实进程退出码 = 0", rc13 == 0, 0, rc13),
               A("真实进程 stdout 含 PASS 行", "PASS:" in (p.stdout or ""), True,
                 (p.stdout or "").strip().splitlines()[-1:] or None),
               A("真实进程产出 4 个文件",
                 all((e2e_dir / n).exists()
                     for n in ("verdicts.jsonl", "metrics.json", "stats.json",
                               "report.md")), True,
                 sorted(x.name for x in e2e_dir.glob("*")) if e2e_dir.exists() else [])]
        record("N13", "⑬ 真实子进程端到端退出码（非 in-process 返回码）", rc13, 0, a13,
               evidence={"cmd": cmd, "stdout_tail": (p.stdout or "")[-1200:]})
    except OSError as exc:
        controls.append({"id": "N13", "title": "⑬ 真实子进程端到端退出码", "exit_code": None,
                         "expected_exit_code": 0, "status": "BLOCKED",
                         "assertions": [A("子进程可运行", False, "可运行", repr(exc))],
                         "evidence": {"cmd": cmd, "oserror": repr(exc)},
                         "note": "宿主沙箱禁止管道 stdio ⇒ BLOCKED（不是 FAIL）"})
        print(f"  [N13] BLOCKED（{exc!r}）")

    # ── ⑭ 报告审查函数有牙齿（喂毒化文本）──────────────────────────────
    poison = "本表结论：两者相当。另一处：两组等价。"
    hits = R.audit_forbidden_wording(poison)
    a14 = [A("毒化文本被捕获", len(hits) >= 2, ">=2 个禁用词", hits),
           A("干净正文零命中", R.audit_forbidden_wording(
               "结论：未检出显著差异（点估计 Δ=+3.00pp）") == [], [], "[]")]
    record("N14", "⑭ 禁用表述审查函数非空转（毒化文本必被抓）", 0, 0, a14,
           evidence={"poison_hits": hits})

    # ── ⑯ 确定性：两次运行逐字相同 ─────────────────────────────────────
    _run(run_pipeline, fx["cases"], fx["answers"], mk("run_det_a"),
         groups=["oracle", "wrong"], expect_full_suite=True)
    _run(run_pipeline, fx["cases"], fx["answers"], mk("run_det_b"),
         groups=["oracle", "wrong"], expect_full_suite=True)
    same = {n: (mk("run_det_a") / n).read_bytes() == (mk("run_det_b") / n).read_bytes()
            for n in ("verdicts.jsonl", "metrics.json", "stats.json")}
    a16 = [A(f"两次运行 {n} 逐字节相同", v, True, v) for n, v in same.items()]
    record("N16", "⑯ 确定性（固定时间戳；report.md 因内嵌 out 路径不参与比对）", 0, 0, a16,
           evidence={"byte_identical": same})

    # ── 多 repeat（§4 P8）────────────────────────────────────────────────
    diag_r3 = {}
    rc_r3 = _run(run_pipeline, fx["cases"], fx["answers_r3"], mk("run_r3"),
                 groups=["oracle", "wrong"], expect_full_suite=True, diag=diag_r3)
    m3 = _load(mk("run_r3"), "metrics.json")
    st3 = _load(mk("run_r3"), "stats.json")
    a17 = [
        A("3 次重复退出码 0", rc_r3 == 0, 0, rc_r3),
        A("每组建 150 条记录", m3["groups"]["oracle"]["records"] == 150, 150,
          m3["groups"]["oracle"]["records"]),
        A("重复集 = [1,2,3]", m3["groups"]["oracle"]["repeats"] == [1, 2, 3], [1, 2, 3],
          m3["groups"]["oracle"]["repeats"]),
        A("配对对数 = 150", st3["comparisons"]["root_cause_accuracy"]["oracle|wrong"]
          ["n_pairs"] == 150, 150,
          st3["comparisons"]["root_cause_accuracy"]["oracle|wrong"]["n_pairs"]),
        A("逐 repeat 均值/SD 已给",
          m3["groups"]["oracle"]["metrics"]["root_cause_accuracy"]
          ["rate_sd_over_repeats"] is not None, "非 None",
          m3["groups"]["oracle"]["metrics"]["root_cause_accuracy"]["rate_sd_over_repeats"]),
        A("Wilcoxon 用于连续指标（path_f1）",
          st3["comparisons"]["path_f1"]["oracle|wrong"].get("p_method") is not None,
          "非 None", st3["comparisons"]["path_f1"]["oracle|wrong"].get("p_method")),
    ]
    record("N17", "⑰ 3 次重复（docs/06 §4 P8）与连续指标检验", rc_r3, 0, a17,
           evidence={"root_cause_cell": m3["groups"]["oracle"]["metrics"]
                     ["root_cause_accuracy"]})

    # ── 夹具构成自查（族/层边际）──────────────────────────────────────────
    def _fam_of(c):
        fam, _pre = C.normalise_cls(c.get("cls"))
        return fam

    fam_n = {f: sum(1 for c in cases if _fam_of(c) == f) for f in C.FAMILY_ORDER}
    dif_n = {d: sum(1 for c in cases if c["difficulty"] == d) for d in C.DIFFICULTY_ORDER}
    a18 = [
        A("族构成 14/14/6/6/6/4", fam_n == C.FAMILY_COUNTS_EXPECTED,
          C.FAMILY_COUNTS_EXPECTED, fam_n),
        A("层构成 16/20/14", dif_n == C.DIFFICULTY_COUNTS_EXPECTED,
          C.DIFFICULTY_COUNTS_EXPECTED, dif_n),
        A("总数 50", len(cases) == 50, 50, len(cases)),
        A("每条用例都标 synthetic", all(c.get("synthetic") is True for c in cases), True,
          sum(1 for c in cases if c.get("synthetic") is not True)),
        A("每个 neg 词都不含否定词（保证 ① 的 false_claim 不被邻窗消歧掉）",
          all(not re.search(J.NEGATION_RE, p) for p in NEG_PHRASES), True,
          [p for p in NEG_PHRASES if re.search(J.NEGATION_RE, p)]),
    ]
    record("N18", "⑱ 合成夹具构成与反作弊前提自查", 0, 0, a18,
           evidence={"families": fam_n, "difficulties": dif_n})

    return _finish(controls, out_dir, fx=fx, metrics=m, stats=st)


def _finish(controls, out_dir, fx=None, metrics=None, stats=None):
    # ── ㉑ ★ M-NORM-CONCL v1：区分「枚举被检查的模式」与「断言故障存在」 ──────
    #   背景（实测 Z-01 / cc-kg-agent）：答案结论明写「不存在可确认的故障」，
    #   并逐条解释「虽然未触发 FM-APP-HEAP-PRESSURE（heapUsedRatio 仅 4.13%）…故当前不构成故障」，
    #   但 ±25 字否定邻窗在**枚举清单**内没有否定词 ⇒ 被判 6 条误报 ⇒ C5 判错。
    #   本负控固化三条路径，防止该修正被后续改动悄悄破坏。
    _bare1 = "FM-APP-ENDPOINT-SLOW"
    _bare2 = "FM-APP-HEAP-PRESSURE"
    _assert_phrase = r"确认为故障|存在故障|定位到根因"
    _c21_ok = {"expect_root_cause": C.NONE_SENTINEL,
               "neg": [_bare1, _bare2, _assert_phrase]}
    _c21_no_disc = {"expect_root_cause": C.NONE_SENTINEL, "neg": [_bare1, _bare2]}
    _c21_not_c5 = {"expect_root_cause": "FM-CTR-OOM-KILLED",
                   "neg": [_bare1, _bare2, _assert_phrase]}
    _fc = [{"phrase": _bare1, "matched": _bare1, "window": "…故障模式判据有：" + _bare1 + "（端点响应慢）…"},
           {"phrase": _bare2, "matched": _bare2, "window": "…" + _bare2 + "（堆内存压力）…"}]
    _fc_with_assert = _fc + [{"phrase": _assert_phrase, "matched": "存在故障",
                              "window": "…确认存在故障…"}]
    _ans_enum = ("我逐条核对了 " + _bare1 + " 与 " + _bare2 + " 的判据，二者均未触发。"
                 + NOR_PHRASES[0] + "，当前不构成故障。")
    _ans_assert = ("已" + _bare1 + "，且确认存在故障。")

    _m1, _e1 = J._split_mentions_vs_claims(_c21_ok, C.NONE_SENTINEL, _ans_enum, _fc, ["x"])
    _m2, _e2 = J._split_mentions_vs_claims(_c21_ok, C.NONE_SENTINEL, _ans_assert,
                                          _fc_with_assert, ["x"])
    _m3, _e3 = J._split_mentions_vs_claims(_c21_no_disc, C.NONE_SENTINEL, _ans_enum, _fc, ["x"])
    _m4, _e4 = J._split_mentions_vs_claims(_c21_not_c5, "FM-CTR-OOM-KILLED", _ans_enum, _fc, ["x"])
    _m5, _e5 = J._split_mentions_vs_claims(_c21_ok, C.NONE_SENTINEL, _ans_enum, _fc, [])
    a21 = [
        A("枚举+结论 ⇒ 裸码重分类为「解释性枚举」（2 条）",
          len(_m1) == 2 and not _e1, "mentions=2 / effective=0",
          {"mentions": len(_m1), "effective": len(_e1)}),
        A("★ 答案断言「存在故障」⇒ 不重分类（仍计误报）",
          len(_m2) == 0 and len(_e2) == 3, "mentions=0 / effective=3",
          {"mentions": len(_m2), "effective": len(_e2)}),
        A("★ fail-closed：neg 无断言式词条 ⇒ 不重分类（无判别量）",
          len(_m3) == 0 and len(_e3) == 2, "mentions=0 / effective=2",
          {"mentions": len(_m3), "effective": len(_e3)}),
        A("非 C5（expect≠NONE）⇒ 一律不重分类",
          len(_m4) == 0 and len(_e4) == 2, "mentions=0 / effective=2",
          {"mentions": len(_m4), "effective": len(_e4)}),
        A("未命中 nor（结论未明确无故障）⇒ 不重分类",
          len(_m5) == 0 and len(_e5) == 2, "mentions=0 / effective=2",
          {"mentions": len(_m5), "effective": len(_e5)}),
        A("判据版本号已登记", J.FALSE_CLAIM_RULE_VERSION == "M-NORM-CONCL v1",
          "M-NORM-CONCL v1", J.FALSE_CLAIM_RULE_VERSION),
    ]
    #   ★ 落点说明：本块在 `_finish` 内，而 `record` 是 `run_selftest` 的嵌套助手 ⇒
    #     复刻其结构内联 append（字段与判分逻辑逐字一致），使负控同样进汇总。
    _st21 = "FAIL" if any(a["status"] == "FAIL" for a in a21) else "PASS"
    controls.append({
        "id": "N21",
        "title": "㉑ M-NORM-CONCL v1：枚举 vs 断言（含 fail-closed 与越界负控）",
        "exit_code": 0, "expected_exit_code": 0, "status": _st21,
        "assertions": a21,
        "evidence": {"reclassified": [c["matched"] for c in _m1],
                     "kept_when_asserted": [c["matched"] for c in _e2]},
        "note": "",
    })
    _bad21 = [a for a in a21 if a["status"] == "FAIL"]
    print(f"  [N21] exit=0/0 {_st21}"
          f"{'' if not _bad21 else '  ← ' + '; '.join(a['name'] for a in _bad21)}")

    n_fail = sum(1 for c in controls if c["status"] == "FAIL")
    n_pass = sum(1 for c in controls if c["status"] == "PASS")
    n_block = sum(1 for c in controls if c["status"] == "BLOCKED")
    rc = 1 if n_fail else (2 if n_block else 0)
    summary = {"schema": "m5.selftest/1", "controls": controls,
               "PASS": n_pass, "FAIL": n_fail, "BLOCKED": n_block,
               "exit_code": rc,
               "synthetic": True,
               "note": ("BLOCKED 与 FAIL 严格区分：BLOCKED 表示「不可算」，"
                        "既不等于通过也不等于失败")}

    (Path(out_dir) / "selftest_evidence.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    (Path(out_dir) / "selftest_evidence.md").write_text(
        render_evidence_md(summary), encoding="utf-8", newline="\n")
    print(f"PASS={n_pass} FAIL={n_fail} BLOCKED={n_block}  退出码={rc}")
    print(f"  证据：{out_dir}\\selftest_evidence.json / selftest_evidence.md")
    print(("FAIL: selftest 存在失败断言" if n_fail else
           "BLOCKED: selftest 存在不可算负控（不是 FAIL）" if n_block else
           "PASS: selftest 全部负控通过"))
    return rc


def render_evidence_md(summary):
    L = ["# M5 判决集 `--selftest` 证据（**合成夹具**，`synthetic: true`）", "",
         f"- PASS={summary['PASS']} FAIL={summary['FAIL']} BLOCKED={summary['BLOCKED']}",
         f"- selftest 退出码：`{summary['exit_code']}`（0 全通过 / 1 有 FAIL / 2 有 BLOCKED）",
         f"- {summary['note']}", ""]
    for c in summary["controls"]:
        L.append(f"## {c['id']} {c['title']}")
        L.append(f"- 真实返回码：`{c['exit_code']}`（期望 `{c['expected_exit_code']}`）"
                 f" · 判定：**{c['status']}**")
        if c.get("note"):
            L.append(f"- 注：{c['note']}")
        for a in c["assertions"]:
            mark = "✅" if a["status"] == "PASS" else ("⛔" if a["status"] == "FAIL" else "⚠️")
            L.append(f"  - {mark} `{a['name']}` — 期望 `{a['expected']}`，实测 `{a['actual']}`")
        ev = c.get("evidence") or {}
        if ev:
            L.append("")
            L.append("<details><summary>证据</summary>")
            L.append("")
            L.append("```json")
            L.append(json.dumps(ev, ensure_ascii=False, indent=2)[:4000])
            L.append("```")
            L.append("</details>")
        L.append("")
    return "\n".join(L)
