#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6.4 变更投入成本计量器（离线可跑；口径见 shared/model/m6-cost.yaml）。

判据（docs/06 §2.3 裁决 8）
---------------------------
主口径 = **规则修改量**：在**声明范围**内，由 ``git diff`` 客观计量「新增/修改一个故障分析规则」的
声明量（单位 = 行）。本脚本只做计量与登记，**不**决定"该改哪条规则"（那是变更实验设计的事，见
m6-cost.yaml#open_questions）。

三态退出码
----------
``PASS = 0``（量到了）／``FAIL = 1``（量到了、口径不符或范围为空）／``BLOCKED = 2``（**没量到**：
不在 git 仓、范围不存在、git 不可用）。优先级 ``BLOCKED(2) > FAIL(1) > PASS(0)``。

用法
----
    python shared/verify/m6/change_cost.py --selftest
    python shared/verify/m6/change_cost.py --base HEAD~1 --head HEAD [--json out.json]
    python shared/verify/m6/change_cost.py --worktree          # 工作区 vs HEAD
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# shared/verify/m6/change_cost.py → 仓库根需要**四**层 dirname（file → m6 → verify → shared → root）
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONTRACT = os.path.join(ROOT, "shared", "model", "m6-cost.yaml")

PASS, FAIL, BLOCKED = 0, 1, 2


def _yaml():
    import yaml
    return yaml


def load_contract(path=None):
    with open(path or CONTRACT, "r", encoding="utf-8") as fh:
        return _yaml().safe_load(fh)


def in_scope(rel, scope):
    """rel 是否落在声明的 scope（glob）内。"""
    return any(fnmatch.fnmatch(rel, pat) for pat in scope)


def count_diff(diff_text, patterns):
    """纯函数：从 unified diff 文本统计增删行。

    返回 dict：added / deleted / rule_added / rule_deleted / files（去重后的路径列表）。
    ★ 只认 ``+++ b/<path>`` 行的路径与 ``+``/``-`` 前缀的**内容行**；
      ``+++``/``---`` 文件头不计入增删。
    """
    pats = [re.compile(p) for p in patterns]
    out = {"added": 0, "deleted": 0, "rule_added": 0, "rule_deleted": 0, "files": []}
    cur = None
    for line in diff_text.split("\n"):
        if line.startswith("+++ b/"):
            cur = line[6:]
            if cur not in out["files"]:
                out["files"].append(cur)
            continue
        if line.startswith("--- ") or line.startswith("diff --git") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            body = line[1:]
            out["added"] += 1
            if any(p.search(body) for p in pats):
                out["rule_added"] += 1
        elif line.startswith("-"):
            body = line[1:]
            out["deleted"] += 1
            if any(p.search(body) for p in pats):
                out["rule_deleted"] += 1
    return out


def _git(args):
    p = subprocess.run(["git"] + args, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def measure(base, head, worktree=False):
    """按契约的每个 carrier 计量。返回 (exit_code, report)。"""
    c = load_contract()
    rc, _o, err = _git(["rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return BLOCKED, {"blocked": "不在 git 工作树内：%s" % (err or "").strip()[:120]}
    rep = {"base": base, "head": head, "worktree": worktree,
           "unit": (c.get("meta") or {}).get("unit"), "carriers": {}}
    problems = []
    for carrier, spec in (c.get("carriers") or {}).items():
        scope = spec.get("scope") or []
        pats = spec.get("rule_line_patterns") or []
        args = ["diff", "-U0"]
        args += [] if worktree else [base or "HEAD~1", head or "HEAD"]
        args += ["--"] + scope
        rc, out, err = _git(args)
        if rc != 0:
            problems.append("%s：git diff 失败 %s" % (carrier, (err or "").strip()[:120]))
            rep["carriers"][carrier] = {"status": "BLOCKED"}
            continue
        stat = count_diff(out, pats)
        files = [f for f in stat.pop("files") if in_scope(f, scope)]
        rec = dict(stat)
        rec["files_touched"] = len(files)
        rec["files"] = sorted(files)[:20]
        rec["rule_lines_total"] = rec["rule_added"] + rec["rule_deleted"]
        # ★ ② 的层数：按契约声明的 file_layer_map 聚合（声明式；粒度局限见该映射的 granularity_limitation）
        fmap = (c.get("file_layer_map") or {}).get(carrier) or {}
        layers = sorted({L for f in files for L in (fmap.get(f) or [])})
        rec["layers_touched"] = len(layers)
        rec["layers"] = layers
        rec["layer_map_provenance"] = (c.get("file_layer_map") or {}).get("provenance")
        rec["layer_map_limitation"] = (c.get("file_layer_map") or {}).get("granularity_limitation")
        rec["status"] = "PASS" if (rec["files_touched"] or rec["added"] or rec["deleted"]) else "EMPTY"
        if rec["status"] == "EMPTY":
            problems.append("%s：该范围内没有任何改动（口径为空 ⇒ 无法比较）" % carrier)
        rep["carriers"][carrier] = rec
    rep["problems"] = problems
    if any(v.get("status") == "BLOCKED" for v in rep["carriers"].values()):
        return BLOCKED, rep
    return (FAIL if problems else PASS), rep


def gate_cost():
    """④ 门禁代价：跑离线档并记录**逐门禁结论**（改动后是否被自动发现「改错了」）。"""
    runner = os.path.join(ROOT, "shared", "verify", "gate_runner.py")
    if not os.path.isfile(runner):
        return {"blocked": "找不到 gate_runner.py"}
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mf = os.path.join(td, "m.json")
        p = subprocess.run([sys.executable, runner, "--offline", "--allow-partial",
                            "--json", "--quiet", "--evidence-dir", td], cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           encoding="utf-8", errors="replace")
        try:
            with open(os.path.join(td, "run_manifest.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            return {"blocked": "gate_runner 未产出 manifest：%s（exit=%d）" % (exc, p.returncode)}
    res = doc.get("gates") or doc.get("results") or []
    failed = sorted(r.get("id") for r in res if r.get("exit") == 1)
    blocked = sorted(r.get("id") for r in res if r.get("exit") == 2)
    return {"n_gates": len(res), "failed": failed, "blocked_gates": blocked,
            "runner_exit": p.returncode, "counts": doc.get("counts") or {}}


FIXTURE_DIFF = """diff --git a/scheme1-kg/ontology/04-rules.cypher b/scheme1-kg/ontology/04-rules.cypher
index 1111111..2222222 100644
--- a/scheme1-kg/ontology/04-rules.cypher
+++ b/scheme1-kg/ontology/04-rules.cypher
@@ -10,0 +11,3 @@
+MATCH (a:AppInstance)-[:DEPLOYED_ON]->(c:Container)
+WHERE a.heapUsedRatio > 0.9
+SET a.flagged = true
@@ -20 +23,0 @@
-CREATE (f:Fault {code: 'X'})
"""


def selftest():
    """离线自检：计数核的**正控 + 负控**（不需要 git）。"""
    ok = 0
    total = 0
    c = load_contract()
    kg = (c.get("carriers") or {}).get("KG") or {}
    pats = kg.get("rule_line_patterns") or []
    st = count_diff(FIXTURE_DIFF, pats)
    total += 1
    # 正控：3 行新增里有 2 行匹配（MATCH/WHERE→不匹配/SET 匹配），1 行删除是 CREATE ⇒ 匹配
    real_added = st["rule_added"]
    real_deleted = st["rule_deleted"]
    print("  正控：added=%d deleted=%d rule_added=%d rule_deleted=%d files=%s"
          % (st["added"], st["deleted"], real_added, real_deleted, st["files"]))
    if st["added"] == 3 and st["deleted"] == 1 and real_added == 2 and real_deleted == 1 \
            and st["files"] == ["scheme1-kg/ontology/04-rules.cypher"]:
        ok += 1
    else:
        print("  ★ 正控不符预期")
    # 负控：文件头与 hunk 头不得计入增删
    total += 1
    if st["added"] + st["deleted"] == 4:
        ok += 1
    else:
        print("  ★ 负控不符：文件头/hunk 头被计入了")
    # 负控：空 diff ⇒ 全 0
    total += 1
    z = count_diff("", pats)
    if z == {"added": 0, "deleted": 0, "rule_added": 0, "rule_deleted": 0, "files": []}:
        ok += 1
    else:
        print("  ★ 空 diff 负控不符：%s" % z)
    # 负控：非声明范围的文件不得进 files
    total += 1
    scope = kg.get("scope") or []
    if in_scope("scheme1-kg/ontology/04-rules.cypher", scope) and not in_scope("README.md", scope):
        ok += 1
    else:
        print("  ★ 范围负控不符")
    # 6) ② 层数：映射聚合（声明式）
    total += 1
    fmap = ((c.get("file_layer_map") or {}).get("KG") or {})
    ls = sorted({L for f in ["scheme1-kg/ontology/04-rules.cypher"] for L in (fmap.get(f) or [])})
    if ls == ["App", "Data", "Runtime"]:
        ok += 1
    else:
        print("  ★ 层数映射负控不符：%s" % ls)
    print("  SELFTEST %d/%d 项通过" % (ok, total))
    return PASS if ok == total else FAIL


def main(argv=None):
    ap = argparse.ArgumentParser(description="M6.4 变更投入成本计量器（口径见 shared/model/m6-cost.yaml）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--base", default="")
    ap.add_argument("--head", default="")
    ap.add_argument("--worktree", action="store_true", help="工作区 vs HEAD（不指定 base/head）")
    ap.add_argument("--gate-cost", action="store_true", dest="gate_cost",
                    help="④ 门禁代价：跑 gate_runner --offline --json 并记录逐门禁结论（较慢）")
    ap.add_argument("--json", default="", help="把结果写到该路径")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    code, rep = measure(a.base, a.head, a.worktree)
    if a.gate_cost:
        gc = gate_cost()
        rep["gate_cost"] = gc
        if gc.get("blocked"):
            print("  [gate-cost] 未取到：%s" % gc["blocked"])
        else:
            print("  [gate-cost] 触发门禁 %d 项；FAIL=%s BLOCKED=%s" % (
                gc.get("n_gates", 0), gc.get("failed") or [], gc.get("blocked_gates") or []))
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
    if code == BLOCKED:
        print("  BLOCKED（≠ FAIL）：没量到 —— %s" % (rep.get("blocked") or rep.get("problems")))
    elif code == FAIL:
        print("  FAIL：量到了但口径不符 —— %s" % rep.get("problems"))
    else:
        print("  PASS：已按声明范围计量（单位 = %s）" % (rep.get("unit"),))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
