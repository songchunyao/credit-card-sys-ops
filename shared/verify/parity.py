# -*- coding: utf-8 -*-
"""M1.5 · **双库等价性对账（`docs\\00` §6.3 三段自检的机械化实现）**。

`docs\\00:326-339` §6.3 要求：
  ① 每个中性类型 `count_kg(T) == count_onto(T)`
  ② 每条关系   `count_kg(R) == count_onto(R)`
  ③ 每个用例 `answer_shape` 相同（形状非内容）——**本阶段（M1.5）无 Agent**，
     前两段可机判；第三段由 M2/M3 的评测台承担，此处只留结构占位并如实标注。
**不自检通过的结果不得采信。**

设计要点：本脚本**不读投影器的自报数**——它自己从 Neo4j 与 GraphDB 回读，
并把两侧与「从 CCF 现算的期望值」做**三方对账**。这样投影器写错、报告写错、
或两侧各错得一样，都不可能蒙混过关。

用法：
  ★ 真机对账**必须先**设 `CC_NEO4J_PASSWORD`（变量名取自
    `shared/deploy/targets.yaml` 的 `neo4j.auth.password_env`）；未设 ⇒ **BLOCKED
    (exit 2)**，因为「没凭据」= **未测量**，不是**测得坏结果**：
    set CC_NEO4J_PASSWORD=...
    python shared/verify/parity.py --ccf tmp/ccf_live1.json \\
        --kg-report tmp/p1_report_live.json --onto-report tmp/p2_report_live.json \\
        --out tmp/parity_live.json

★ M2.18 前置条件与退出码（三态，**不得混读**）：
  0 = PASS    三方对账全 OK。
  1 = FAIL    两侧数据**真实分歧**（建模/投影缺陷）⇒ 拒绝采信投影结果。
  **2 = BLOCKED** 前置条件不满足 ⇒ **本门禁未执行**（既非 PASS 亦非 FAIL）。
      现有两种前置条件缺失：
      (a) 【W43 新增判态修正】Neo4j 口令环境变量未设 ⇒ KG 侧**根本没读**，
          无从对账。原实现 `raise SystemExit("<字符串>")` 会退 **1 = FAIL**，
          把「未测量」误报成「测得坏结果」，与 `docs/00` 铁律 **BLOCKED ≠ FAIL**
          相悖，也与同族门禁 `shared/verify/t10_native_parity.py:1090-1091`
          （缺凭据 ⇒ `T10 RESULT: BLOCKED (exit=2)`）不一致 ⇒ 已改为 exit 2。
      (b) ONTO（GraphDB `cc-ops`）里**不存在**本 CCF `snapshotId` 的命名图 ⇒
          该快照从未被 P2 投影 ⇒ 不再打印「ONTO 全 0」的逐行 MISMATCH 噪声。
  ★ 判态修正**不放宽任何判据**：聚合器 `shared/verify/gate_runner.py:747-748` 中
    任一 BLOCKED ⇒ 总码 2，且无任何路径把 BLOCKED 折算成 PASS ⇒ 本改动**不可能**
    把任何东西变成「通过」；它只是把「未测量」从「测得坏结果」里分离出来。
    真实比对逻辑与 CCF 校验（本文件 :371 `★ CCF 未通过校验 …` 仍判 FAIL）均未改动。
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "shared", "project"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ccf_plan as cp  # noqa: E402


def read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def expected_from_ccf(plan):
    """从 CCF 现算期望计数（投影器不得自报；这是唯一权威来源）。"""
    by_type, by_rel = {}, {}
    metric, nc, ce = {}, {}, {}
    for n in plan.nodes:
        by_type[n.type] = by_type.get(n.type, 0) + 1
        for code, _v, _u, _o in n.values:
            metric[code] = metric.get(code, 0) + 1
        for code in n.not_collected:
            nc[code] = nc.get(code, 0) + 1
        for code in n.collected_empty:
            ce[code] = ce.get(code, 0) + 1
    for e in plan.edges:
        by_rel[e.type] = by_rel.get(e.type, 0) + 1
    # ★ M1.5c：派生证据也是中性类型/中性关系，必须进同一个 §6.3 口径。
    #   这里从 `plan.evidence`（源自 CCF observations）现算，**不读投影器自报数**。
    by_type[plan.maps.evidence_type()] = len(plan.evidence)
    by_rel[plan.maps.evidence_relation()] = len(plan.evidence_edges)
    return {"byType": by_type, "byRelation": by_rel, "metricValueCounts": metric,
            "notCollectedCounts": nc, "collectedEmptyCounts": ce}


# --------------------------------------------------------------------------
# KG 侧（Neo4j）：独立回读
# --------------------------------------------------------------------------
def read_kg(targets, maps, plan):
    from neo4j import GraphDatabase
    neo = targets["neo4j"]
    pwd = os.environ.get(neo["auth"]["password_env"])
    if not pwd:
        # ★★★ W43 判态修正：缺凭据 = **未测量**（BLOCKED），不是**测得坏结果**（FAIL）★★★
        #   原实现 `raise SystemExit("<字符串>")` ⇒ 退出码 1 = FAIL，把「没凭据没测」
        #   误报成「两侧数据真实分歧」，既违本仓铁律「BLOCKED ≠ FAIL」（模块 docstring
        #   :24-39），也与同族门禁 t10_native_parity.py:1090-1091 的
        #   `T10 RESULT: BLOCKED (exit=2)` 不一致。此处只把判态从 1 改为 2。
        #   ★ 不放宽判据：gate_runner.py:747-748「任一 BLOCKED ⇒ 总码 2」，且无任何路径
        #     把 BLOCKED 折算成 PASS ⇒ 本改动不可能把任何东西变成「通过」。
        sys.stderr.write(
            "PARITY RESULT: BLOCKED (exit=2) —— 未取到 %s；未测量 ≠ 测得坏结果\n"
            % neo["auth"]["password_env"])
        raise SystemExit(2)
    try:
        driver = GraphDatabase.driver(neo["bolt_uri"],
                                      auth=(neo["auth"]["user"], pwd),
                                      notifications_min_severity="OFF")
    except (TypeError, ValueError):
        driver = GraphDatabase.driver(neo["bolt_uri"],
                                      auth=(neo["auth"]["user"], pwd))
    reserved = maps.ns["kg"]
    nc_p = maps.obs_policy["kg"]["not_collected"]
    ce_p = maps.obs_policy["kg"]["collected_empty"]
    out = {"byType": {}, "byRelation": {}, "metricValueCounts": {},
           "notCollectedCounts": {}, "collectedEmptyCounts": {}}
    with driver.session(database="neo4j") as sess:
        for t in maps.concrete_types():
            rec = sess.run("MATCH (n:`%s`) RETURN count(n) AS c" % maps.kg_label(t)).single()
            out["byType"][t] = rec["c"]
        for r in sorted(maps.rel_map["relations"]):
            rl = maps.relation(r)["kg_reltype"]
            rec = sess.run("MATCH ()-[e:`%s`]->() RETURN count(e) AS c" % rl).single()
            out["byRelation"][r] = rec["c"]
        # ★ 口径来自 ccf_plan.Maps（唯一权威定义）：按 (对象类型, 属性) 双侧限定。
        #   全局口径会同时踩两个坑：与既有 v2 图共库导致同名属性串数；跨类型复用的
        #   同名属性（如 `cpuUsageRatio` 由 3 个指标码共用）互相合并。
        for code in sorted(expected_from_ccf(plan)["metricValueCounts"]):
            rec = sess.run(maps.kg_metric_count_query(code)).single()
            out["metricValueCounts"][code] = rec["c"]
        for key, prop in (("notCollectedCounts", nc_p), ("collectedEmptyCounts", ce_p)):
            for rec in sess.run(maps.kg_scope_query(prop)):
                out[key][rec["metricCode"]] = rec["c"]
        out["_reserved"] = reserved
    driver.close()
    return out


# --------------------------------------------------------------------------
# ONTO 侧（GraphDB）：独立回读
# --------------------------------------------------------------------------
def onto_repo_url(targets):
    """ONTO 侧仓库端点（地址/仓库**全部沿用 targets.yaml**，不新增写死地址）。"""
    gdb = targets["graphdb"]
    return "%s/repositories/%s" % (gdb["url"].rstrip("/"),
                                   urllib.parse.quote(gdb["isolation"]["repository"]))


def sparql_select(targets, q, timeout=120):
    """只读 SPARQL SELECT（POST），返回 `results.bindings`。"""
    data = urllib.parse.urlencode({"query": q}).encode("utf-8")
    req = urllib.request.Request(
        onto_repo_url(targets), data=data,
        headers={"Accept": "application/sparql-results+json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
    except urllib.error.HTTPError as exc:
        raise RuntimeError("SPARQL -> HTTP %s: %s\n%s"
                           % (exc.code, exc.read()[:300].decode("utf-8", "replace"), q))


# --------------------------------------------------------------------------
# ★ M2.18 前置条件：ONTO 侧是否已投影本快照
# --------------------------------------------------------------------------
def onto_named_graphs(targets):
    """只读列出仓库里**现有**的命名图 IRI（一次查询；不触碰任何图的数据）。"""
    rows = sparql_select(targets, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")
    return sorted({r["g"]["value"] for r in rows})


def onto_snapshot_graph(graphs, snapshot_id):
    """命名图清单里「IRI 末段 == snapshotId」的那一张；命中返回图 IRI，否则 None。"""
    for g in graphs:
        if g.rstrip("/").rsplit("/", 1)[-1] == snapshot_id:
            return g
    return None


def read_onto(targets, maps, plan):
    graph = targets["graphdb"]["isolation"]["named_graph_template"].format(
        snapshotId=plan.ccf.snapshot_id)

    def select(clause, where, group_by=None):
        q = "SELECT %s FROM <%s> WHERE { %s }%s" % (
            clause, graph, where, " GROUP BY %s" % group_by if group_by else "")
        return sparql_select(targets, q)

    ns = maps.ns["onto"]
    out = {"byType": {}, "byRelation": {}, "metricValueCounts": {},
           "notCollectedCounts": {}, "collectedEmptyCounts": {}}
    for t in maps.concrete_types():
        rows = select("(COUNT(DISTINCT ?s) AS ?c)",
                      "?s a <%s> ." % maps.expand(maps.onto_class(t)))
        out["byType"][t] = int(rows[0]["c"]["value"]) if rows else 0
    for r in sorted(maps.rel_map["relations"]):
        prop = maps.expand(maps.relation(r)["onto_property"])
        rows = select("(COUNT(*) AS ?c)", "?a <%s> ?b ." % prop)
        out["byRelation"][r] = int(rows[0]["c"]["value"]) if rows else 0
    for code in sorted(expected_from_ccf(plan)["metricValueCounts"]):
        # ★ 同 KG 侧：类限定，否则跨类复用的 `ccm:` 属性会把多个指标码串成一个数
        rows = select("(COUNT(*) AS ?c)", maps.onto_metric_count_clause(code))
        out["metricValueCounts"][code] = int(rows[0]["c"]["value"]) if rows else 0
    for key, prop in (("notCollectedCounts", maps.obs_policy["onto"]["not_collected"]),
                      ("collectedEmptyCounts", maps.obs_policy["onto"]["collected_empty"])):
        rows = select("?m (COUNT(*) AS ?c)", maps.onto_scope_clause(prop), group_by="?m")
        for row in rows:
            out[key][row["m"]["value"]] = int(row["c"]["value"])
    out["_graph"] = graph
    return out


# --------------------------------------------------------------------------
# 三方对账
# --------------------------------------------------------------------------
def compare(expected, kg, onto):
    rows, bad = [], []
    for dim in ("byType", "byRelation", "metricValueCounts",
                "notCollectedCounts", "collectedEmptyCounts"):
        keys = sorted(set(expected[dim]) | set(kg[dim]) | set(onto[dim]))
        for k in keys:
            e = expected[dim].get(k, 0)
            a = kg[dim].get(k, 0)
            b = onto[dim].get(k, 0)
            ok = (e == a == b)
            rows.append((dim, k, e, a, b, ok))
            if not ok:
                bad.append((dim, k, e, a, b))
    return rows, bad


def human_lines(plan, targets, expected, kg, onto, rows, bad, probes, ok_all,
                graphs, matched):
    """人类可读输出。★ M2.18 **只新增一行**前置条件，其余文案与格式逐字保留。"""
    lines = []
    w = max([len(r[1]) for r in rows] + [10])
    lines.append("=" * 78)
    lines.append("§6.3 双库等价性对账   snapshotId = %s" % plan.ccf.snapshot_id)
    lines.append("  CCF: %s" % plan.ccf.path)
    lines.append("  KG : Neo4j %s (db=neo4j, CC_ 前缀)" % targets["neo4j"]["bolt_uri"])
    lines.append("  ONTO: GraphDB %s repo=%s graph=<%s>"
                 % (targets["graphdb"]["url"],
                    targets["graphdb"]["isolation"]["repository"], onto["_graph"]))
    lines.append("  前置条件：ONTO 命名图 %d 张，含 snapshotId=%s 的命名图 <%s> ⇒ 执行三路对账"
                 % (len(graphs), plan.ccf.snapshot_id, matched))
    lines.append("=" * 78)
    lines.append("%-22s %-*s %6s %6s %6s %s" % ("维度", w, "键", "CCF", "KG", "ONTO", "判定"))
    lines.append("-" * 78)
    last = None
    for dim, k, e, a, b, ok in rows:
        if dim != last:
            lines.append("-- %s" % dim)
            last = dim
        lines.append("  %-20s %-*s %6d %6d %6d %s"
                     % ("", w, k, e, a, b, "OK" if ok else "**MISMATCH**"))
    lines.append("-" * 78)
    lines.append("对象合计：CCF=%d  KG=%d  ONTO=%d"
                 % (sum(expected["byType"].values()), sum(kg["byType"].values()),
                    sum(onto["byType"].values())))
    lines.append("关系合计：CCF=%d  KG=%d  ONTO=%d"
                 % (sum(expected["byRelation"].values()), sum(kg["byRelation"].values()),
                    sum(onto["byRelation"].values())))
    lines.append("观测合计（已采集）：CCF=%d  KG=%d  ONTO=%d"
                 % (sum(expected["metricValueCounts"].values()),
                    sum(kg["metricValueCounts"].values()),
                    sum(onto["metricValueCounts"].values())))
    if probes:
        lines.append("\n★ 投影器自报数与独立回读不一致（报告不可信）：")
        for p in probes:
            lines.append("   - %s" % p)
    if bad:
        lines.append("\n★ 不匹配明细（%d 项）：" % len(bad))
        for dim, k, e, a, b in bad:
            lines.append("   - %s[%s] CCF=%d KG=%d ONTO=%d" % (dim, k, e, a, b))
    lines.append("\n③ answer_shape 对账：本阶段（M1.5）无 Agent，**未执行**，"
                 "留待 M2/M3 评测台；此处只覆盖 §6.3 ①②段。")
    lines.append("\nRESULT: %s" % ("PASS" if ok_all else "FAIL"))
    return lines


def emit_blocked(args, plan, targets, expected, kg, graphs, graph_iri):
    """★ M2.18：**前置条件不满足** ⇒ BLOCKED(exit 2)。

    **不执行任何对账查询**，因此**不打印**逐行 MISMATCH（那 68 行「ONTO 全 0」是
    前置条件缺失造成的假分歧，不是数据不符）。报告里 onto 计数量标 `null`（= 未测），
    **不写 0**（0 会被误读成「测了，是 0」）。
    """
    snap = plan.ccf.snapshot_id
    gdb = targets["graphdb"]
    payload = {
        "snapshotId": snap,
        "ccf": plan.ccf.path,
        "checked": 0,
        "mismatches": [],
        "reportCrossChecks": [],
        "totals": {
            "objects": {"ccf": sum(expected["byType"].values()),
                        "kg": sum(kg["byType"].values()), "onto": None},
            "relations": {"ccf": sum(expected["byRelation"].values()),
                          "kg": sum(kg["byRelation"].values()), "onto": None},
            "observationsCollected": {
                "ccf": sum(expected["metricValueCounts"].values()),
                "kg": sum(kg["metricValueCounts"].values()), "onto": None}},
        "answerShapeChecked": False,
        "result": "BLOCKED",
        "ontoSnapshotGraphPresent": False,
        "ontoSnapshotGraphs": graphs,
        "preconditionBlocked": True,
    }
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 2
    lines = [
        "=" * 78,
        "§6.3 双库等价性对账   snapshotId = %s" % snap,
        "  CCF: %s" % plan.ccf.path,
        "  KG : Neo4j %s (db=neo4j, CC_ 前缀)" % targets["neo4j"]["bolt_uri"],
        "  ONTO: GraphDB %s repo=%s graph=<%s>"
        % (gdb["url"], gdb["isolation"]["repository"], graph_iri),
        "=" * 78,
        "★ 前置条件不满足：ONTO 侧不存在 snapshotId=%s 的命名图（现有 %d 张）"
        "⇒ 该快照未被投影到 GraphDB，本门禁无法对账" % (snap, len(graphs)),
        "  期望命名图 IRI：<%s>" % graph_iri,
        "  ONTO 仓库现有命名图（按 IRI 排序；末段即各快照的 snapshotId）：",
    ]
    for g in graphs:
        lines.append("    - <%s>   末段=%s" % (g, g.rstrip("/").rsplit("/", 1)[-1]))
    lines += [
        "",
        "★ 未执行：三路对账（byType / byRelation / metricValueCounts / notCollectedCounts"
        " / collectedEmptyCounts）与投影器自报数交叉校验。",
        "  原先此处会打印逐行 MISMATCH（ONTO 侧全 0）——那是「前置条件缺失」造成的**假分歧**，",
        "  不是「两侧数据不符」，故不再打印、也不再据此判 FAIL。",
        "",
        "★ BLOCKED ≠ FAIL：",
        "  FAIL    = 两侧数据**真实分歧**（建模/投影缺陷）⇒ 拒绝采信投影结果，去查投影器/映射表。",
        "  BLOCKED = **缺前置条件**（该快照从未被 P2 投影进 GraphDB）⇒ 本次对账本身不可采信；",
        "            处置：先补投影（P2 写入 graph/<snapshotId>），或改用已投影的 CCF，再重跑本门禁。",
        "",
        "RESULT: BLOCKED（前置条件不满足 ⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2）",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    if args.out:
        sys.stdout.write("报告写出：%s\n" % args.out)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccf", required=True)
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"))
    ap.add_argument("--targets", default=os.path.join(ROOT, "shared", "deploy", "targets.yaml"))
    ap.add_argument("--kg-report", default=None)
    ap.add_argument("--onto-report", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--json", action="store_true", help="仅打印 JSON 结果（供脚本消费）")
    args = ap.parse_args(argv)

    targets = read_yaml(args.targets)
    plan = cp.build_plan(args.ccf, args.mapping_dir, args.model_dir)
    if plan.errors():
        sys.exit("★ CCF 未通过校验（%d ERROR），拒绝采信" % len(plan.errors()))
    maps = plan.maps

    expected = expected_from_ccf(plan)
    kg = read_kg(targets, maps, plan)

    # ★★★ M2.18 前置条件判据：**早于任何对账查询** ★★★
    #   若 ONTO 侧根本没有本 CCF `snapshotId` 的命名图（= 该快照从未被 P2 投影），
    #   则逐图查询必然 0 条 ⇒ 会产出逐行「ONTO 全 0」的 MISMATCH 噪声，把
    #   「缺前置条件」伪装成「两侧数据不符」。此处显式判别并 BLOCKED(exit 2)。
    snap = plan.ccf.snapshot_id
    graphs = onto_named_graphs(targets)
    matched = onto_snapshot_graph(graphs, snap)
    graph_iri = targets["graphdb"]["isolation"]["named_graph_template"].format(
        snapshotId=snap)
    if matched is None:
        return emit_blocked(args, plan, targets, expected, kg, graphs, graph_iri)

    onto = read_onto(targets, maps, plan)

    rows, bad = compare(expected, kg, onto)

    # 交叉校验：投影器自报数必须与独立回读一致（否则报告不可信）
    probes = []
    for label, path, side in (("P1", args.kg_report, kg), ("P2", args.onto_report, onto)):
        if not path or not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
        for dim in ("byType", "byRelation", "metricValueCounts",
                    "notCollectedCounts", "collectedEmptyCounts"):
            for k, v in (rep.get("actual", {}).get(dim) or {}).items():
                if side[dim].get(k, 0) != v:
                    probes.append("%s 自报 %s[%s]=%s，独立回读=%s"
                                  % (label, dim, k, v, side[dim].get(k, 0)))

    ok_all = not bad and not probes

    payload = {
        "snapshotId": plan.ccf.snapshot_id,
        "ccf": plan.ccf.path,
        "checked": len(rows),
        "mismatches": [{"dim": d, "key": k, "ccf": e, "kg": a, "onto": b}
                       for d, k, e, a, b in bad],
        "reportCrossChecks": probes,
        "totals": {
            "objects": {"ccf": sum(expected["byType"].values()),
                        "kg": sum(kg["byType"].values()),
                        "onto": sum(onto["byType"].values())},
            "relations": {"ccf": sum(expected["byRelation"].values()),
                          "kg": sum(kg["byRelation"].values()),
                          "onto": sum(onto["byRelation"].values())},
            "observationsCollected": {
                "ccf": sum(expected["metricValueCounts"].values()),
                "kg": sum(kg["metricValueCounts"].values()),
                "onto": sum(onto["metricValueCounts"].values())}},
        "answerShapeChecked": False,
        "result": "PASS" if ok_all else "FAIL",
        # ★ M2.18 纯新增三键（既有键名与语义不动）
        "ontoSnapshotGraphPresent": True,
        "ontoSnapshotGraphs": graphs,
        "preconditionBlocked": False,
    }
    if not args.json:
        sys.stdout.write("\n".join(human_lines(
            plan, targets, expected, kg, onto, rows, bad, probes, ok_all,
            graphs, matched)) + "\n")

    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if args.out and not args.json:
        sys.stdout.write("报告写出：%s\n" % args.out)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
