#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 断言：离线门禁跑完后，**声明面**与**实测面**必须一致（fail-closed）。

为什么需要这个脚本（而不是只看 ``gate_runner`` 的退出码）：

``gate_runner --offline --allow-partial`` 在「20 个离线门禁全 PASS」时返回 0，
但它**允许**部分运行 —— 于是有一个静默失效：**若有人把某个门禁的 ``class``
从 ``offline`` 改成 ``online``（或新增一条 online），它就会从 CI 里悄悄消失，
而退出码仍然是 0**（"没跑" 被算成 "通过" 的近亲）。

本脚本把这条堵死：把 ``shared/model/gate-registry.yaml`` 里 ``class: online``
的 id 集合 与 ``run_manifest.json`` 的 ``not_evaluated`` 逐字比对，
并要求 ``fail==0``、``blocked==0``、``evaluated`` 数 == 离线 gate 数。

退出码：0 = 一致；1 = 不一致（点名差异）；2 = 前置不可得（读不到注册表/manifest）。
☆ 本脚本**刻意不放在** ``shared/verify/`` 下 —— 该目录有「每个 .py 必须登记进
gate-registry」的覆盖契约，放进去会立刻触发 ``--audit-coverage`` FAIL。

用法：
    python ci/check_offline_gates.py [--registry R] [--manifest M]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def _die(msg: str, code: int = 2):
    print("[BLOCKED] %s" % msg)
    return code


def main(argv=None) -> int:
    here = pathlib.Path(__file__).resolve()
    repo = here.parents[1]
    ap = argparse.ArgumentParser(description="离线门禁的声明面 ↔ 实测面一致性断言")
    ap.add_argument("--registry", default=str(repo / "shared" / "model" / "gate-registry.yaml"))
    ap.add_argument("--manifest",
                    default=str(repo / "tmp" / "gate_runner_evidence" / "run_manifest.json"))
    args = ap.parse_args(argv)

    try:
        import yaml
    except Exception as exc:  # noqa: BLE001
        return _die("读不到 PyYAML（%s）⇒ 无法判定，不当作通过" % exc)

    rp = pathlib.Path(args.registry)
    if not rp.is_file():
        return _die("注册表不存在：%s" % rp)
    try:
        entries = (yaml.safe_load(rp.read_text(encoding="utf-8")) or {}).get("entries") or []
    except Exception as exc:  # noqa: BLE001
        return _die("注册表解析失败：%s" % exc)

    mp = pathlib.Path(args.manifest)
    if not mp.is_file():
        return _die("run_manifest.json 不存在：%s（是否漏传 --json？）" % mp)
    try:
        mf = json.loads(mp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _die("run_manifest.json 解析失败：%s" % exc)

    online = sorted(e["id"] for e in entries if e.get("class") == "online")
    offline_gates = sorted(e["id"] for e in entries
                           if e.get("class") == "offline" and e.get("kind") == "gate")
    not_eval = sorted(x["id"] if isinstance(x, dict) else str(x)
                      for x in (mf.get("not_evaluated") or []))
    counts = mf.get("counts") or {}

    bad = []
    if counts.get("fail"):
        bad.append("counts.fail=%s（离线门禁不该有 FAIL）" % counts.get("fail"))
    if counts.get("blocked"):
        bad.append("counts.blocked=%s（离线门禁不该有 BLOCKED）" % counts.get("blocked"))
    if counts.get("evaluated") != len(offline_gates):
        bad.append("counts.evaluated=%s ≠ 离线 gate 数 %d"
                   % (counts.get("evaluated"), len(offline_gates)))
    if not_eval != online:
        only_manifest = sorted(set(not_eval) - set(online))
        only_registry = sorted(set(online) - set(not_eval))
        if only_manifest:
            bad.append("未评估但注册表**不是** online（脱离 CI！）：%s" % only_manifest)
        if only_registry:
            bad.append("注册表标 online 但**被评估了**了：%s" % only_registry)

    print("注册表：offline gate %d 条 / online gate %d 条" % (len(offline_gates), len(online)))
    print("实测  ：evaluated=%s pass=%s fail=%s blocked=%s"
          % (counts.get("evaluated"), counts.get("pass"),
             counts.get("fail"), counts.get("blocked")))
    print("未评估：%s" % (not_eval or "（无）"))

    if bad:
        print("\n[FAIL] 声明面 ↔ 实测面不一致：")
        for b in bad:
            print("  - %s" % b)
        return 1
    print("\n[PASS] 声明面 ↔ 实测面一致：离线档覆盖 = 注册表全部 offline gate，"
          "未评估 = 注册表全部 online gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
