#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shared/verify/equivalence.py —— CCF 快照的等价性 / 确定性 / 来源门禁机械判据。

定位
    对应 docs/07 §7 CI 清单里 M1 的出口条件：「同一份 CCF 文件能被装入 Neo4j 与
    GraphDB，且 equivalence.py 报告内容等价」。它同时把三条『人工纪律』变成机械门禁：
      R1-canonical  同一输入两次产物的 canonicalSha256 必须相同（确定性自检）
      R9-payload    payloadPolicy 与 collectorProvenance.agg 双向一致（硬约束 C6）
      R10-provenance simulated == false 且 unmatchedExits 为空（fail-closed，裁决 6）
    以及 G5 的机械判据：expectedCoverage.ok 必须为 true（防空图通过）。

用法
    # 自检一份快照：重算 canonicalSha256、引用完整性、覆盖门禁、来源门禁、枚举合法性
    python shared/verify/equivalence.py --a snapshot.json

    # 等价性对比：同一 snapshotId 冻结后喂给两侧的两份 CCF
    python shared/verify/equivalence.py --a left.json --b right.json

    # 显式声明这是 replay/record 快照（效率判据用），此时 simulated 允许为 true
    python shared/verify/equivalence.py --a replay.json --expect-simulated true

    # 机器可读输出
    python shared/verify/equivalence.py --a a.json --b b.json --json

    # [additive] T10 子检查：两侧原生实现覆盖一致性（docs/07 M2.8 规格块 · docs/00 §6.3 ④）
    # 该模式下其余参数转发给 shared/verify/t10_native_parity.py，退出码 0=PASS 1=FAIL 2=BLOCKED
    python shared/verify/equivalence.py --t10 --kg-ontology-dir <kg载体目录> \
        --onto-rules-dir <onto规则目录> --out t10.json

零外部依赖（只用标准库）、纯只读、不碰真机。退出码非零 = FAIL。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SCHEMA = os.path.join(ROOT, "shared", "model", "ccf.schema.json")

TOP_REQUIRED = ["ccfVersion", "snapshotId", "capturedAt", "collector", "payloadPolicy",
                "objects", "relations", "observations", "expected", "integrity"]
CAP_CONST = {"CAP_VALUES": 8, "CAP_OBJS": 4, "MAX_STR": 120}

F = []  # [(severity, code, message)]


def err(code, msg):
    F.append(("ERROR", code, msg))


def warn(code, msg):
    F.append(("WARN", code, msg))


def note(code, msg):
    F.append(("INFO", code, msg))


# ---------------------------------------------------------------- 规范化与散列

def _norm_numbers(o):
    """数字去掉尾随零：1.0 -> 1，保持其余原样。递归。"""
    if isinstance(o, float):
        if o.is_integer():
            return int(o)
        return o
    if isinstance(o, dict):
        return {k: _norm_numbers(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm_numbers(v) for v in o]
    return o


def _sort_key_id(item):
    if isinstance(item, dict):
        return (str(item.get("id", "")),)
    return (json.dumps(item, sort_keys=True, ensure_ascii=False),)


def canonical_dumps(obj):
    """键按字典序、数组按 id 排序、数字去尾随零、无空格。"""
    return json.dumps(_norm_numbers(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def section_digest(items):
    ordered = sorted(items or [], key=_sort_key_id)
    return sha256_text(canonical_dumps(ordered))


def canonical_snapshot_sha(doc):
    """canonicalSha256 的计算口径（与 ccf.schema.json 的描述一致）。

    两处必须排除，否则哈希自指或无意义：
      * integrity.canonicalSha256 —— 自指字段；
      * x —— schema 明写『两侧投影器都不得读取此处任何内容；放入 x 的数据不参与等价性自检』。
    """
    clone = json.loads(json.dumps(doc))
    integ = clone.get("integrity")
    if isinstance(integ, dict):
        integ.pop("canonicalSha256", None)
    clone.pop("x", None)
    for key in ("objects", "relations", "observations"):
        if isinstance(clone.get(key), list):
            clone[key] = sorted(clone[key], key=_sort_key_id)
    return sha256_text(canonical_dumps(clone))


# ---------------------------------------------------------------- 单份快照自检

def load_schema(path):
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:                                    # noqa: BLE001
        err("SCHEMA-LOAD", "无法读取 schema %s：%s" % (path, exc))
        return {}


def enum_of(schema, name):
    d = (schema.get("$defs") or {}).get(name) or {}
    return set(d.get("enum") or [])


def check_snapshot(doc, label, schema, expect_simulated):
    tag = "[%s] " % label
    for k in TOP_REQUIRED:
        if k not in doc:
            err("X1-required", tag + "缺少必需顶层字段 %s" % k)
    if any(k not in doc for k in TOP_REQUIRED):
        return

    objects = doc.get("objects") or []
    relations = doc.get("relations") or []
    observations = doc.get("observations") or []
    integ = doc.get("integrity") or {}
    pp = doc.get("payloadPolicy") or {}
    exp = doc.get("expected") or {}

    type_enum = enum_of(schema, "typeCode")
    rel_enum = enum_of(schema, "relationCode")
    met_enum = enum_of(schema, "metricCode")
    oid_pat = re.compile(((schema.get("$defs") or {}).get("objectId") or {}).get("pattern") or r"^$")

    # --- 枚举与 id 形态 -------------------------------------------------
    bad_type = sorted({o.get("type") for o in objects if o.get("type") not in type_enum})
    if bad_type:
        err("V1-typeCode", tag + "objects 出现非法 type：%s" % ", ".join(map(str, bad_type)))
    bad_rel = sorted({r.get("type") for r in relations if r.get("type") not in rel_enum})
    if bad_rel:
        err("V2-relationCode", tag + "relations 出现非法 relation：%s" % ", ".join(map(str, bad_rel)))
    bad_met = sorted({v.get("metricCode") for v in observations if v.get("metricCode") not in met_enum})
    if bad_met:
        err("V3-metricCode", tag + "observations 出现非法 metricCode：%s" % ", ".join(map(str, bad_met)))
    bad_oid = sorted({o.get("id") for o in objects if not oid_pat.match(str(o.get("id") or ""))})
    if bad_oid:
        err("V4-objectId", tag + "objects 的 id 不合 objectId 模式：%s" % ", ".join(map(str, bad_oid[:12])))

    # --- 计数自洽 -------------------------------------------------------
    counts = integ.get("counts") or {}
    for key, arr in (("objects", objects), ("relations", relations), ("observations", observations)):
        if counts.get(key) != len(arr):
            err("X9-counts", tag + "integrity.counts.%s=%s 与实际 %d 不符"
                % (key, counts.get(key), len(arr)))
    by_type = Counter(o.get("type") for o in objects)
    if counts.get("byType") is not None:
        declared = {k: int(v) for k, v in (counts.get("byType") or {}).items()}
        if declared != dict(by_type):
            err("X9-byType", tag + "integrity.counts.byType 与实际类型分布不符（declared=%s actual=%s）"
                % (declared, dict(by_type)))
    by_status = Counter(v.get("status") for v in observations)
    if counts.get("byObservationStatus") is not None:
        declared_s = {k: int(v) for k, v in (counts.get("byObservationStatus") or {}).items()}
        if declared_s.get("collected", 0) != by_status.get("collected", 0):
            err("X9-byStatus", tag + "counts.byObservationStatus.collected=%s 与实际 %d 不符"
                % (declared_s.get("collected"), by_status.get("collected", 0)))

    # --- 引用完整性 -----------------------------------------------------
    oids = {o.get("id") for o in objects}
    dang_rel = sorted(str(r.get("id")) for r in relations
                      if r.get("from") not in oids or r.get("to") not in oids)
    dang_obs = sorted(str(v.get("id")) for v in observations if v.get("objectId") not in oids)
    ri = integ.get("referenceIntegrity") or {}
    if ri.get("ok") != (not dang_rel and not dang_obs):
        err("X7-refint", tag + "integrity.referenceIntegrity.ok=%s 与实测不符（danglingRelationEnds=%d danglingObservationRefs=%d）"
            % (ri.get("ok"), len(dang_rel), len(dang_obs)))
    if sorted(ri.get("danglingRelationEnds") or []) != dang_rel:
        err("X7-refint", tag + "integrity.referenceIntegrity.danglingRelationEnds 与实测不符")
    if sorted(ri.get("danglingObservationRefs") or []) != dang_obs:
        err("X7-refint", tag + "integrity.referenceIntegrity.danglingObservationRefs 与实测不符")
    if dang_rel or dang_obs:
        err("X7-refint", tag + "引用完整性实际不成立（relation 悬空 %d，observation 悬空 %d）"
            % (len(dang_rel), len(dang_obs)))

    # --- G5 覆盖门禁（防空图通过）---------------------------------------
    real_types = set(by_type)
    missing_types = sorted((set(exp.get("types") or []) - real_types))
    observed_metrics = {v.get("metricCode") for v in observations}
    missing_metrics = sorted((set(exp.get("metrics") or []) - observed_metrics))
    under_min = sorted(t for t, mn in (exp.get("objectsMin") or {}).items() if by_type.get(t, 0) < int(mn))
    recomputed_ok = not (missing_types or missing_metrics or under_min)
    ec = integ.get("expectedCoverage") or {}
    if bool(ec.get("ok")) != recomputed_ok:
        err("X8-coverage", tag + "integrity.expectedCoverage.ok=%s 与实测不符（missingTypes=%d missingMetrics=%d underMinTypes=%d）"
            % (ec.get("ok"), len(missing_types), len(missing_metrics), len(under_min)))
    if sorted(ec.get("missingTypes") or []) != missing_types:
        err("X8-coverage", tag + "expectedCoverage.missingTypes 与实测不符")
    if sorted(ec.get("missingMetrics") or []) != missing_metrics:
        err("X8-coverage", tag + "expectedCoverage.missingMetrics 与实测不符")
    if sorted(ec.get("underMinTypes") or []) != under_min:
        err("X8-coverage", tag + "expectedCoverage.underMinTypes 与实测不符")
    if not recomputed_ok:
        err("G5-coverage", tag + "★ 覆盖门禁失败（防空图通过）：missingTypes=%s missingMetrics=%s underMinTypes=%s"
            % (missing_types or "-", missing_metrics or "-", under_min or "-"))
    if not objects:
        err("G5-empty", tag + "objects 为空（空图）")

    # --- R9 载荷冻结 ----------------------------------------------------
    for k, v in CAP_CONST.items():
        if pp.get(k) != v:
            err("X3-caps", tag + "payloadPolicy.%s=%s，契约 const=%s（违反即证明两侧不是同一形态）"
                % (k, pp.get(k), v))
    agg = (integ.get("collectorProvenance") or {}).get("agg") or {}
    for k in CAP_CONST:
        if agg.get(k) != pp.get(k):
            err("X5-agg-parity", tag + "collectorProvenance.agg.%s=%s 与 payloadPolicy.%s=%s 不一致（R9 双向一致）"
                % (k, agg.get(k), k, pp.get(k)))
    df = integ.get("droppedFacts") or {}
    if int(pp.get("valuesTruncated") or 0) > 0 and int(df.get("total") or 0) <= 0:
        err("X4-r9-invariant", tag + "payloadPolicy.valuesTruncated>0 但 integrity.droppedFacts.total=0（R9 不变量被击穿）")
    if int(df.get("total") or 0) > 0 and not (df.get("byReason") or {}):
        warn("X4-reason", tag + "droppedFacts.total>0 但 byReason 为空（不可归因）")

    # --- R10 来源门禁 ---------------------------------------------------
    cp = integ.get("collectorProvenance") or {}
    sim = bool(cp.get("simulated"))
    if sim != bool(expect_simulated):
        err("R10-simulated", tag + "collectorProvenance.simulated=%s，本次期望 %s（裁决 6：主判据只接受 live 快照）"
            % (sim, bool(expect_simulated)))
    ux = cp.get("unmatchedExits")
    if ux:
        err("R10-unmatched", tag + "collectorProvenance.unmatchedExits 非空（%s）⇒ fail-closed，该快照不得进入任何判据" % (ux,))
    src = cp.get("collectSource")
    if src not in ("live", "record", "replay"):
        err("R10-source", tag + "collectorProvenance.collectSource=%r 非法" % (src,))
    if not expect_simulated and src != "live":
        err("R10-source", tag + "主判据要求 collectSource=live，实为 %r" % (src,))

    # --- R1 确定性：canonicalSha256 重算 --------------------------------
    stored = integ.get("canonicalSha256")
    actual = canonical_snapshot_sha(doc)
    if stored != actual:
        err("R1-canonical", tag + "canonicalSha256 重算不符：stored=%s actual=%s" % (stored, actual))
    return {
        "label": label,
        "snapshotId": doc.get("snapshotId"),
        "ccfVersion": doc.get("ccfVersion"),
        "canonicalSha256": stored,
        "recomputedSha256": actual,
        "counts": {"objects": len(objects), "relations": len(relations), "observations": len(observations)},
        "byType": dict(by_type),
        "byStatus": dict(by_status),
        "expectedCoverageOk": bool(ec.get("ok")),
        "payloadPolicy": {k: pp.get(k) for k in ("CAP_VALUES", "CAP_OBJS", "MAX_STR", "parallel", "aggMode")},
        "collectSource": src,
        "simulated": sim,
        "digests": {
            "objects": section_digest(objects),
            "relations": section_digest(relations),
            "observations": section_digest(observations),
            "payloadPolicy": sha256_text(canonical_dumps(pp)),
            "expected": sha256_text(canonical_dumps(exp)),
        },
    }


# ---------------------------------------------------------------- 两份等价性

def compare(a, b):
    la, lb = a["label"], b["label"]
    for key in ("snapshotId", "ccfVersion"):
        if a[key] != b[key]:
            err("Y1-identity", "%s.%s=%r 而 %s.%s=%r 不同 ⇒ 不是同一份冻结快照"
                % (la, key, a[key], lb, key, b[key]))
    if a["counts"] != b["counts"]:
        err("Y2-counts", "计数不同：%s=%s vs %s=%s" % (la, a["counts"], lb, b["counts"]))
    for key in ("objects", "relations", "observations"):
        if a["digests"][key] != b["digests"][key]:
            err("Y3-content", "★ %s 段内容不等价（digest %s=%s vs %s=%s）"
                % (key, la, a["digests"][key][:16], lb, b["digests"][key][:16]))
    for key in ("payloadPolicy", "expected"):
        if a["digests"][key] != b["digests"][key]:
            err("Y4-shape", "%s 不等价（%s vs %s）⇒ 两侧不是同一形态" % (key, la, lb))
    if a["recomputedSha256"] != b["recomputedSha256"]:
        err("Y5-canonical", "canonicalSha256 不同（%s=%s vs %s=%s）⇒ 两侧消费的事实不是同一份"
            % (la, a["recomputedSha256"][:16], lb, b["recomputedSha256"][:16]))
    else:
        note("Y5-canonical", "✓ 两侧 canonicalSha256 相同：%s" % a["recomputedSha256"])
    for key in ("collectSource", "simulated"):
        if a[key] != b[key]:
            err("Y6-provenance", "%s 不同：%s=%r vs %s=%r" % (key, la, a[key], lb, b[key]))


# ---------------------------------------------------------------- main

T10_MODULE_FILE = "t10_native_parity.py"


def load_t10_module():
    """按文件路径加载 T10 子检查模块（不使用 sys.path，避免触发分层门禁 G15）。"""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), T10_MODULE_FILE)
    spec = importlib.util.spec_from_file_location("_cc_t10_native_parity", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法按路径加载 %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_t10(argv):
    """把参数转发给 T10 子检查模块，返回它的退出码（0/1/2）。"""
    try:
        mod = load_t10_module()
    except Exception as exc:                                    # noqa: BLE001
        print("RESULT: BLOCKED  (T10 子检查模块加载失败：%s)" % exc)
        return 2
    return int(mod.main(list(argv)))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--t10" in argv:
        return run_t10([tok for tok in argv if tok != "--t10"])

    ap = argparse.ArgumentParser(description="CCF 快照等价性 / 确定性 / 来源门禁检查")
    ap.add_argument("--a", default=None, help="CCF 快照 JSON 路径（左；除 --t10 外必填）")
    ap.add_argument("--b", default=None, help="CCF 快照 JSON 路径（右，可选）")
    ap.add_argument("--schema", default=DEFAULT_SCHEMA, help="ccf.schema.json 路径")
    ap.add_argument("--expect-simulated", default="false", choices=["true", "false"],
                    help="期望的 collectorProvenance.simulated 取值（主判据用 false，效率判据用 true）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结论")
    ap.add_argument("--t10", action="store_true",
                    help="[additive] 执行 T10 两侧原生实现覆盖自检"
                         "（其余参数转发给 shared/verify/t10_native_parity.py；退出码 0/1/2）")
    args = ap.parse_args(argv)

    if not args.a:
        # 与既有行为一致：argparse 报错并以退出码 2 结束
        ap.error("--a 为必填（除非使用 --t10 子检查）")

    expect_sim = args.expect_simulated == "true"
    schema = load_schema(args.schema)
    if not schema:
        print("RESULT: FAIL  (ERROR=%d WARN=%d)" % (len(F), 0))
        return 2

    results = {}
    for label, path in (("A", args.a), ("B", args.b)):
        if not path:
            continue
        try:
            with io.open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as exc:                                # noqa: BLE001
            err("LOAD", "无法读取 %s（%s）：%s" % (label, path, exc))
            continue
        info = check_snapshot(doc, label, schema, expect_sim)
        if info:
            results[label] = info
            results[label]["path"] = path

    if "A" in results and "B" in results:
        compare(results["A"], results["B"])

    n_err = sum(1 for s, _, _ in F if s == "ERROR")
    n_warn = sum(1 for s, _, _ in F if s == "WARN")

    if args.json:
        print(json.dumps({"results": results,
                          "findings": [{"severity": s, "code": c, "message": m} for s, c, m in F],
                          "error": n_err, "warn": n_warn,
                          "ok": n_err == 0}, ensure_ascii=False, indent=2))
        return 0 if n_err == 0 else 1

    print("=" * 78)
    print("CCF 等价性 / 确定性 / 来源门禁检查")
    print("=" * 78)
    for s, c, m in F:
        print("[%s] %s" % (c, m) if s == "INFO" else "[%s/%s] %s" % (c, s, m))
    if not F:
        print("（无发现）")
    print("-" * 78)
    for label, info in results.items():
        print("%s = %s" % (label, info.get("path")))
        print("    snapshotId=%s ccfVersion=%s collectSource=%s simulated=%s"
              % (info["snapshotId"], info["ccfVersion"], info["collectSource"], info["simulated"]))
        print("    counts=%s expectedCoverage.ok=%s" % (info["counts"], info["expectedCoverageOk"]))
        print("    payloadPolicy=%s" % (info["payloadPolicy"],))
        print("    canonicalSha256(recomputed)=%s" % info["recomputedSha256"])
    print("-" * 78)
    print("RESULT: %s  (ERROR=%d WARN=%d)" % ("PASS" if n_err == 0 else "FAIL", n_err, n_warn))
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())