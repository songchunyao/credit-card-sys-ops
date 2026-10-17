# -*- coding: utf-8 -*-
"""fi-e2e/validate_probe_fix.py —— **免部署**验证采集层探针修复。

为什么需要它：探针跑在共享主机的 `cc-ops-collect` 容器里（源码打进了镜像），
改一行就要重建镜像 + 重启服务，风险与代价都高。而 `shared/collect/freeze.py`
支持 `--gather-json` 离线固化 ⇒ 可以先在**真实 gather 回执**上把「修复后的取值」
打进去，跑完整契约判据，证明「修完能不能判」，再决定要不要真的部署。

判据（fail-closed）：
  · 对照组（原样重放）        → 期望 `FM-DB-LOCK-WAIT = FALSE`（当前探针判不出）
  · 修复组（打了三处补丁）    → 期望 `FM-DB-LOCK-WAIT = TRUE` （修完能判出）
  两者不满足即 rc=1。

三处补丁（对应 FINDINGS 的 M1/M2/M3）：
  M1 `transaction.duration_ms`  −28797184 → 1200       （真实值应来自 SQL 的 now()-xact_start）
  M2 `gaussdb.session.lock_wait`      0 → 6            （真实值应来自 pg_locks WHERE NOT granted）
  M3 `session.wait_event`(obj_type=session) "lock_wait" → "tuple"（真实值应来自 pg_locks.locktype）

用法：
    python fi-e2e/validate_probe_fix.py --dir fi-e2e/out/fixcheck
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from fi import Contract, CCFView, detect_all, judgeability  # noqa: E402

M1_MS = 600_000      # 600 s，必须 > 阈值 xact_age_s_light#warning = 300 s
M2_LOCK_WAITS = 6     # pg_locks WHERE NOT granted 实测值
M3_WAIT_EVENT = "tuple"   # pg_locks.locktype 实测值（在契约 enum 内）

RETRACTED = """\
★ 撤回说明（2026-10-13 复核）：本文件早先版本把 `container.status` / `db.session.state` /
`db.session.wait_event` 的 `value=null` 报为缺陷，那是**本测试器读错字段**——这三者是
text/enum 类指标，值在 `valueText`（实测 `Running` / `idle` / `lock_wait` 均在）。
体检脚本与 L0 取值已修为「value 为空则读 valueText」，该条已撤回。
"""


def freeze_offline(gather, health, out):
    cmd = [sys.executable, os.path.join(ROOT, "shared", "collect", "freeze.py"),
           "--gather-json", gather, "--health-json", health,
           "--layers", "gaussdb,os,container,app", "--expected-profile", "l1-l3",
           "--out", out]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def patch_facts(g, m1=True, m2=True, m3=True):
    n = {"M1": 0, "M2": 0, "M3": 0}
    for f in g.get("all_facts") or []:
        p = str(f.get("path") or "")
        if m1 and p == "transaction.duration_ms":
            f["value"] = M1_MS
            n["M1"] += 1
        elif m2 and p == "gaussdb.session.lock_wait":
            f["value"] = M2_LOCK_WAITS
            n["M2"] += 1
        elif m3 and p == "session.wait_event" and f.get("obj_type") == "session":
            f["value"] = M3_WAIT_EVENT
            n["M3"] += 1
    return n


def run_one(tag, gather_doc, health, outdir):
    gather = os.path.join(outdir, "gather_%s.json" % tag)
    ccf = os.path.join(outdir, "ccf_%s.json" % tag)
    io.open(gather, "w", encoding="utf-8").write(json.dumps(gather_doc, ensure_ascii=False))
    rc, out = freeze_offline(gather, health, ccf)
    res = {"tag": tag, "freezeRc": rc, "ccf": ccf}
    if rc != 0:
        res["error"] = out[-600:]
        return res
    doc = json.load(io.open(ccf, encoding="utf-8"))
    view = CCFView(doc)
    det = detect_all(view, Contract())
    j = judgeability(Contract(), view.metrics_present())
    res["snapshotId"] = doc.get("snapshotId")
    res["obsCount"] = len(doc.get("observations") or [])
    res["FM-DB-LOCK-WAIT"] = {"state": det["FM-DB-LOCK-WAIT"]["state"],
                              "detail": [t for t in det["FM-DB-LOCK-WAIT"]["trace"]
                                         if t.get("state") != "FALSE"][:6]}
    res["trueCodes"] = sorted(c for c, v in det.items() if v["state"] == "TRUE")
    res["judgeability_lock"] = j.get("FM-DB-LOCK-WAIT")
    # 落盘逐子句轨迹，便于复算
    io.open(os.path.join(outdir, "detect_%s.json" % tag), "w", encoding="utf-8").write(
        json.dumps(det["FM-DB-LOCK-WAIT"], ensure_ascii=False, indent=1))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "out", "fixcheck"))
    a = ap.parse_args()
    d = a.dir
    gp = os.path.join(d, "gather_fault.json")
    hp = os.path.join(d, "health.json")
    if not (os.path.exists(gp) and os.path.exists(hp)):
        raise SystemExit("缺少 %s / %s（先用 fi.py 的注入窗口抓一份）" % (gp, hp))

    print("=" * 78)
    print("免部署验证：采集层探针三处修复能否让 FM-DB-LOCK-WAIT 可判")
    print("=" * 78)
    print(RETRACTED)

    base = json.load(io.open(gp, encoding="utf-8"))
    print("原始 gather：all_facts=%d" % len(base.get("all_facts") or []))
    print("补丁取值：M1 duration_ms=%d ms（须 > 阈值 300 s） M2 lock_wait=%d M3 wait_event=%s"
          % (M1_MS, M2_LOCK_WAITS, M3_WAIT_EVENT))

    ctrl_doc = json.loads(json.dumps(base))
    fix_doc = json.loads(json.dumps(base))
    counters = patch_facts(fix_doc)
    print("补丁命中：M1(duration_ms)=%d 条  M2(lock_wait)=%d 条  M3(wait_event)=%d 条"
          % (counters["M1"], counters["M2"], counters["M3"]))

    ctrl = run_one("control", ctrl_doc, hp, d)
    fix = run_one("fixed", fix_doc, hp, d)

    for r in (ctrl, fix):
        print()
        print("── %s ──" % r["tag"])
        if r.get("error"):
            print("  ✗ freeze 失败 rc=%s\n%s" % (r["freezeRc"], r["error"]))
            continue
        print("  snapshotId=%s  obs=%d" % (r["snapshotId"], r["obsCount"]))
        print("  FM-DB-LOCK-WAIT = %s" % r["FM-DB-LOCK-WAIT"]["state"])
        for t in r["FM-DB-LOCK-WAIT"]["detail"]:
            print("     clause %s %s observed=%s operand=%s -> %s"
                  % (t.get("group"), t.get("metric"), t.get("observed"),
                     t.get("operand"), t.get("state")))
        print("  可判性=%s" % json.dumps(r["judgeability_lock"], ensure_ascii=False))
        print("  全 TRUE 码: %s" % ", ".join(r["trueCodes"]))

    ok = (not ctrl.get("error") and not fix.get("error")
          and ctrl["FM-DB-LOCK-WAIT"]["state"] == "FALSE"
          and fix["FM-DB-LOCK-WAIT"]["state"] == "TRUE")
    print()
    print("=" * 78)
    print("结论：%s" % ("PASS —— 三处修复使 FM-DB-LOCK-WAIT 从 FALSE 变 TRUE（修完可判）"
                      if ok else "FAIL —— 未达成预期翻转，见上"))
    print("=" * 78)
    io.open(os.path.join(d, "validate_result.json"), "w", encoding="utf-8").write(
        json.dumps({"control": ctrl, "fixed": fix, "pass": ok},
                   ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
