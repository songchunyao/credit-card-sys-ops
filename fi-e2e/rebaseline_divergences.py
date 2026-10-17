# -*- coding: utf-8 -*-
"""fi-e2e/rebaseline_divergences.py —— 从某个批次的产物**自动重建**载体差异登记表条目。

## 何时需要它

每当我们**改动任一侧载体**（修 bug、对齐字段、补投影），两侧的分歧格局就会变：
原先登记的 (码,字段) 对可能不再出现，同时冒出一批**新的**未声明对。
此时「未声明 N 行」这个回归信号会一直红着，失去判别力 —— 必须在**新基线**上重新登记。

## 它做什么

1. 读批次里每例保存的 `cause_KG.json` / `cause_ONTO.json`（**原始响应**，不是当时记录的结论）；
2. 用**当前**登记表重跑 `classify_divergences()`（与 `fi.py` 同一份实现，避免口径分叉）；
3. 收集未声明/已过期的 (码,字段) 对及其出现次数；
4. `--write` 时把新条目追加进 `carrier-divergences.yaml`（`value_check: kind_only`，
   取值随症状集变化，故只登记「存在分歧」这一事实、把实测取值写进 evidence）。

## 用法

```
python fi-e2e/rebaseline_divergences.py --runid suite50b            # 只报告，不改文件
python fi-e2e/rebaseline_divergences.py --runid suite50b --write    # 追加登记
```
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import io
import json
import os
import sys
from collections import Counter

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUTROOT = os.path.join(HERE, "out")
REG = os.path.join(HERE, "carrier-divergences.yaml")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_fi():
    spec = importlib.util.spec_from_file_location("fi", os.path.join(HERE, "fi.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runid", required=True)
    ap.add_argument("--write", action="store_true", help="把新条目追加进登记表")
    ap.add_argument("--max-rows", type=int, default=40)
    a = ap.parse_args()

    fi = load_fi()
    dec = fi.load_declared_divergences()
    print("=" * 84)
    print("载体差异**重新登记**（rebaseline）")
    print("=" * 84)
    print("当前登记表：%s（%d 条）" % (REG, len(dec["entries"])))

    d = os.path.join(OUTROOT, a.runid)
    if not os.path.isdir(d):
        print("★ BLOCKED：无此批次 %s" % d)
        return 2
    dirs = sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
    undeclared, stale, n_ok = Counter(), Counter(), 0
    examples = {}
    for name in dirs:
        cdir = os.path.join(d, name)
        fk, fo = os.path.join(cdir, "cause_KG.json"), os.path.join(cdir, "cause_ONTO.json")
        if not (os.path.exists(fk) and os.path.exists(fo)):
            continue
        try:
            kg = json.load(io.open(fk, encoding="utf-8"))
            on = json.load(io.open(fo, encoding="utf-8"))
        except Exception:
            continue
        if not (kg.get("ok") and on.get("ok")):
            continue
        r = fi.classify_divergences({"ok": True, "raw": kg.get("raw")},
                                    {"ok": True, "raw": on.get("raw")}, dec)
        n_ok += 1
        for row in r["declared"]:
            if not row.get("matchesDeclared"):
                stale["%s.%s" % (row["code"], row["field"])] += 1
                examples.setdefault("%s.%s" % (row["code"], row["field"]),
                                    (row["kg"], row["onto"], row.get("declaredId")))
        for row in r["undeclared"]:
            k = "%s.%s" % (row["code"], row["field"])
            undeclared[k] += 1
            examples.setdefault(k, (row["kg"], row["onto"], None))

    print("可重分类用例 = %d" % n_ok)
    print()
    print("**未声明（新基线需要登记）：%d 行 / %d 个 (码,字段) 对**"
          % (sum(undeclared.values()), len(undeclared)))
    for k, v in undeclared.most_common(a.max_rows):
        kg_v, on_v, _ = examples.get(k, (None, None, None))
        print("   %-52s ×%-3d  kg=%r onto=%r" % (k, v, kg_v, on_v))
    if stale:
        print()
        print("**已登记但取值不符（exact 口径下须更新）：%d 行**" % sum(stale.values()))
        for k, v in stale.most_common(10):
            kg_v, on_v, did = examples.get(k, (None, None, None))
            print("   %-52s ×%-3d  登记=%s  实测 kg=%r onto=%r" % (k, v, did, kg_v, on_v))

    if not a.write:
        print()
        print("（只报告；加 --write 才会追加进登记表）")
        return 0

    data = yaml.safe_load(io.open(REG, encoding="utf-8"))
    have = {(e["code"], e["field"]) for e in data["entries"]}
    nxt = max([int(e["id"].split("-")[1]) for e in data["entries"]] or [0]) + 1
    added = []
    for k in sorted(undeclared):
        code, field = k.split(".", 1)
        if (code, field) in have:
            continue
        kg_v, on_v, _ = examples.get(k, (None, None, None))
        data["entries"].append({
            "id": "CD-%d" % nxt, "code": code, "field": field,
            "kg": None, "onto": None, "value_check": "kind_only",
            "disposition": "to_investigate",
            "claim": "两侧该字段不等（新基线自动登记，%d 例）" % undeclared[k],
            "evidence": {"measured_in": "fi-e2e/out/%s" % a.runid,
                         "occurrences": undeclared[k],
                         "sample_values": {"kg": kg_v, "onto": on_v},
                         "note": "取值随症状集变化，故 kg/onto 留空、按 kind_only 口径登记"},
            "hypothesis": "待定位（由 rebaseline 自动登记，尚未逐一分析）",
            "next": "逐一核对：是否载体原生差异、是否契约真空、是否实现缺陷",
        })
        added.append("CD-%d(%s)" % (nxt, k))
        nxt += 1
    data["meta"]["version"] = "1.2.0"
    data["meta"]["rebaseline_%s" % a.runid] = {
        "note": "自该批次基线重新登记；此前版本反映的是修复前格局",
        "added": added,
    }
    io.open(REG, "w", encoding="utf-8", newline="").write(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000))
    print()
    print("已追加 **%d** 条：%s" % (len(added), ", ".join(added[:12])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
