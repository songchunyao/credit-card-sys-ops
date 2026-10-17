# -*- coding: utf-8 -*-
"""M2.9 自测：topology_reader 的真机只读自测 + 离线 fixture 契约用例（T2–T6）。

用法（本机系统 python 带 neo4j 驱动）：
    python scheme1-kg/service/_selftest_topology_reader.py --password '<口令>'
口令优先级：--password > 环境变量 CC_NEO4J_PASSWORD。

★ 本脚本只读：它只调用 topology_reader 的接口（模块内部只发 MATCH），自身不含任何
  写语句；A 段结束时重跑一次普查并与开头对比，机械证明本次自测没有改动图。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import topology_reader as tr  # noqa: E402

RESULTS: list = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — %s" % (detail,)) if detail != "" else ""))
    return bool(ok)


def dump(label: str, payload: object, indent: int | None = None) -> None:
    print("--- %s ---" % label)
    print(json.dumps(payload, ensure_ascii=False, indent=indent, default=str))


def timed(label: str, func, *args, **kwargs):
    start = time.perf_counter()
    out = func(*args, **kwargs)
    elapsed = (time.perf_counter() - start) * 1000.0
    print("... %s 耗时 %.1f ms" % (label, elapsed))
    return out, elapsed


def _type_map(result: dict) -> dict:
    return {node["objId"]: node["objType"] for node in result["nodes"]}


def _edges(result: dict) -> set:
    return {(edge["from"], edge["relation"], edge["to"]) for edge in result["edges"]}


def _find(result: dict, obj_type: str, relation: str | None = None) -> list:
    types = _type_map(result)
    out = []
    for edge in result["edges"]:
        if types.get(edge["to"]) != obj_type:
            continue
        if relation and edge["relation"] != relation:
            continue
        out.append(edge)
    return out


# ★ 2026-10-06 边属性契约修正：这 5 个键在 causal-edges.yaml 里不存在，出参不得出现。
BANNED_EDGE_KEYS = ("weight", "probability", "delaySec", "chainPosition", "learnCount")
ALLOWED_FAULT_EDGE_KEYS = {"from", "relation", "to", "condition", "evidence", "description",
                           "provenance", "contractId", "chain",
                           "condMetric", "condOp", "condKind", "condRef"}
# 快照 id 一律读 :CC_Meta.snapshotId，源码里不得出现快照字面量
SNAPSHOT_LITERAL = re.compile(r"ccf-\d{8}T\d{6}Z-[0-9a-f]+")


def _banned_key_hits(payload: object, path: str = "") -> list:
    """递归扫描出参里是否出现被撤回的 5 个边属性键。"""
    hits = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in BANNED_EDGE_KEYS:
                hits.append("%s/%s" % (path, key))
            hits.extend(_banned_key_hits(value, "%s/%s" % (path, key)))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            hits.extend(_banned_key_hits(value, "%s[%d]" % (path, index)))
    return hits


# ---------------------------------------------------------------------------
# A. 真机只读自测
# ---------------------------------------------------------------------------

def live_section(contract, uri, user, password, database) -> None:
    print("\n================ A. 真机只读自测（bolt） ================")
    start = time.perf_counter()
    backend = tr.GraphBackend.connect(uri, user, password, database, contract)
    print("... GraphBackend.connect 耗时 %.1f ms（uri=%s user=%s db=%s）"
          % ((time.perf_counter() - start) * 1000.0, uri or "env/targets.yaml", user, backend.database))
    try:
        # ---- A1 普查 ------------------------------------------------------
        census_before, ms = timed("census()", backend.census)
        dump("A1 census（只读普查）", census_before)
        dump("A1 census 审计", backend.audit)
        check("A1 真机 CC_ 图非空", census_before["nodeTotal"] > 0 and census_before["edgeTotal"] > 0,
              "nodes=%d edges=%d" % (census_before["nodeTotal"], census_before["edgeTotal"]))

        # ---- A2 对象拓扑（默认 9 跳）--------------------------------------
        topo, ms = timed("get_object_topology('app:cc-pay')",
                         tr.get_object_topology, "app:cc-pay", backend=backend, contract=contract)
        dump("A2 nodes（objId/objType/layer）",
             [{k: node[k] for k in ("objId", "objType", "layer")} for node in topo["nodes"]])
        dump("A2 edges", topo["edges"])
        dump("A2 摘要", {k: topo[k] for k in ("hopsUsed", "appliedMaxHopDepth", "truncated", "includeDerived")})
        dump("A2 mustReach（must_reach_paths 的机械核验）", topo["mustReach"])
        dump("A2 warnings", topo["warnings"])
        dump("A2 audit（逐查询 queryText/elapsedMs/rowCount）", topo["audit"])

        edges = _edges(topo)
        tmap = _type_map(topo)
        cross = [edge for edge in topo["edges"] if edge["crossLayer"]]
        dump("A2 跨层边（crossLayer=true）", cross)
        hop_checks = [
            ("HAS_APP_INSTANCE → AppInstance", ("app:cc-pay", "HAS_APP_INSTANCE", "appinst:appinst-cc-pay-1")),
            ("USES_DATASOURCE → JdbcDataSource", ("appinst:appinst-cc-pay-1", "USES_DATASOURCE", "jdbsrc:ds-cc-pay-gaussdb")),
        ]
        for label, item in hop_checks:
            check("A2 链路 %s" % label, item in edges, item)
        for label, obj_type, relation in (("AppInstance -RUNS_IN-> Container", "Container", "RUNS_IN"),
                                          ("Container -DEPLOYED_ON-> Host", "Host", "DEPLOYED_ON"),
                                          ("JdbcDataSource -CONNECTS_TO-> GaussDBCluster", "GaussDBCluster", "CONNECTS_TO"),
                                          ("JdbcDataSource -RESOLVES_TO-> GaussDBInstance", "GaussDBInstance", "RESOLVES_TO")):
            found = _find(topo, obj_type, relation)
            check("A2 链路 %s" % label, bool(found), found[:2])
        cross_types = {edge["relation"] for edge in cross}
        declared = {code for code, spec in contract.relations.items() if (spec or {}).get("cross_layer")}
        dump("A2 跨层关系类型（契约声明 vs 真机 out 方向可达）",
             {"declared": sorted(declared), "observed": sorted(cross_types),
              "unreachableOut": sorted(declared - cross_types),
              "reason": "HOSTS 声明方向是 Host -> GaussDBInstance（指向库实例），从 App 层顶点 out 遍历不可达；"
                        "真机 app:cc-pay 到 Host 走的是 Container -DEPLOYED_ON-> Host"})
        check("A2 跨层标记与契约一致（无漏标、无误标）",
              cross_types <= declared
              and all((edge["relation"] in declared) == bool(edge["crossLayer"]) for edge in topo["edges"]),
              sorted(cross_types))
        check("A2 真机 out 方向覆盖 3 条跨层关系（RUNS_IN/CONNECTS_TO/RESOLVES_TO）",
              cross_types == {"RUNS_IN", "CONNECTS_TO", "RESOLVES_TO"}, sorted(cross_types))
        check("A2 默认排除派生注释边 HAS_EVIDENCE",
              all(edge["relation"] != "HAS_EVIDENCE" for edge in topo["edges"]))
        print("[INFO] A2 可达节点类型集合（真机缺 CC_HAS_SESSION/CC_RUNS_QUERY/CC_ACCESSES，Table/Session/Query 孤立）: %s"
              % sorted({t for t in tmap.values() if t}))

        # ---- A3 深度夹取（请求 99 → 9）------------------------------------
        deep, ms99 = timed("get_object_topology('app:cc-pay', max_hops=99)",
                           tr.get_object_topology, "app:cc-pay", max_hops=99, backend=backend, contract=contract)
        dump("A3 摘要", {k: deep[k] for k in ("hopsUsed", "appliedMaxHopDepth", "truncated")})
        dump("A3 truncation", deep["truncation"])
        dump("A3 warnings", deep["warnings"])
        check("A3 max_hops=99 被夹到 9", deep["appliedMaxHopDepth"] == contract.business_hop_depth == 9,
              deep["appliedMaxHopDepth"])
        check("A3 夹取事实被显式上报（truncation.clamped + warning）",
              bool((deep["truncation"] or {}).get("clamped")) and any("夹取" in w for w in deep["warnings"]),
              (deep["truncation"] or {}).get("clamped"))
        check("A3 夹取后与默认深度结果一致（夹取不报错、不改变拓扑）",
              _edges(deep) == edges and len(deep["nodes"]) == len(topo["nodes"]), None)

        # ---- A4 派生边开关 ------------------------------------------------
        wide, ms_wide = timed("get_object_topology('*')",
                              tr.get_object_topology, "*", backend=backend, contract=contract)
        dump("A4a scope='*' 摘要", {k: wide[k] for k in ("truncated", "hopsUsed", "appliedMaxHopDepth")})
        dump("A4a truncation", wide["truncation"])
        check("A4a 默认无 HAS_EVIDENCE 边",
              all(item["relation"] != "HAS_EVIDENCE" for item in wide["edges"]), len(wide["edges"]))
        check("A4a 默认无 Evidence 节点", "Evidence" not in set(_type_map(wide).values()), None)

        facts_seed = []
        for candidate in ("Container", "Host", "Table", "Query", "JdbcDataSource", "GaussDBInstance"):
            facts_seed = tr.get_facts(candidate, backend=backend, contract=contract)
            if facts_seed:
                print("... 用于派生开关的样本对象类型：%s（%d 条证据）" % (candidate, len(facts_seed)))
                break
        seed_obj = facts_seed[0]["payload"]["objectId"] if facts_seed else None
        derived, ms_derived = timed("get_object_topology(<有证据的对象>, include_derived=True)",
                                    tr.get_object_topology, seed_obj, include_derived=True,
                                    backend=backend, contract=contract)
        dump("A4b 派生开关（scope=%s）edges" % seed_obj, derived["edges"][:4])
        dump("A4b nodes", [{k: node[k] for k in ("objId", "objType", "layer")} for node in derived["nodes"]])
        check("A4b include_derived=True 时有 HAS_EVIDENCE 边",
              any(item["relation"] == "HAS_EVIDENCE" for item in derived["edges"]))
        check("A4b 派生目标节点类型为 Evidence（未被冒充成业务对象）",
              "Evidence" in set(_type_map(derived).values()), sorted(set(_type_map(derived).values())))

        # ---- A5 指标直读 ---------------------------------------------------
        metrics, ms_metrics = timed("get_metrics(['*'])", tr.get_metrics, ["*"], backend=backend, contract=contract)
        dump("A5 meta", metrics.meta())
        dump("A5 前 8 行", metrics[:8])
        field_sets = {tuple(sorted(row.keys())) for row in metrics}
        dump("A5 行字段集合", [list(item) for item in field_sets])
        check("A5 取到指标行", len(metrics) > 0, len(metrics))
        check("A5 行字段 == §3.3 item_fields 七项",
              field_sets <= {tuple(sorted(("object", "metric_code", "value", "unit", "ts", "threshold_ref", "source")))},
              field_sets)
        with_ref = [row for row in metrics if row.get("threshold_ref")]
        dump("A5 带 threshold_ref 的行", with_ref)
        check("A5 只给 threshold_ref 引用、不给阈值数字",
              all("threshold" not in key or key == "threshold_ref" for row in metrics for key in row), None)

        # ---- A6 事实溯源（IP-FACT / C5）------------------------------------
        facts, ms_facts = timed("get_facts('Container')", tr.get_facts, "Container",
                                backend=backend, contract=contract)
        dump("A6 meta", facts.meta())
        dump("A6 前 3 行", facts[:3])
        check("A6 取到 :CC_Evidence 记录", len(facts) > 0, len(facts))
        check("A6 每条记录可回溯到 CCF 记录 id（recordId）",
              all(str(row.get("record_id") or "").startswith("obs-") for row in facts),
              [row.get("record_id") for row in facts[:3]])
        check("A6 记录带 snapshot_id 与 objectId",
              all(row.get("snapshot_id") and row["payload"].get("objectId") for row in facts), None)
        dump("A6 审计", facts.audit)

        # ---- A8 故障拓扑真机（T2/T3/T4/T5/T6 的真机复核）-------------------
        f_top, ms_f2 = timed("get_fault_topology('FM-APP-POOL-EXHAUSTED', 4)",
                             tr.get_fault_topology, "FM-APP-POOL-EXHAUSTED", 4,
                             backend=backend, contract=contract)
        dump("A8/T2 摘要", {k: f_top[k] for k in ("root", "appliedMaxHops", "truncated", "cyclesEncountered")})
        dump("A8/T2 nodes", f_top["nodes"])
        dump("A8/T2 edges", f_top["edges"])
        dump("A8/T2 audit", f_top["audit"])
        check("A8/T2 真机根节点取到 CC_FaultMode 属性（fmCode/fmName/fmLayer/fmSeverity/detectionMode）",
              bool(f_top["nodes"]) and all(node.get("fmCode") for node in f_top["nodes"]), f_top["nodes"][:1])
        check("A8/T2 真机 cyclesEncountered 非空（环被记录并上报）", bool(f_top["cyclesEncountered"]),
              f_top["cyclesEncountered"])
        check("A8/T2 真机无白名单外环告警（唯一环在白名单内）",
              not any("白名单外" in item for item in f_top["warnings"]), f_top["warnings"])
        check("A8/T2 边关系名原样透出 causes/conditional_causes/relates_to（§3.2 relation 字段）",
              {edge["relation"] for edge in f_top["edges"]} <= {"causes", "conditional_causes", "relates_to"},
              sorted({edge["relation"] for edge in f_top["edges"]}))

        f_rel, ms_f4 = timed("get_fault_topology('FM-APP-HEAP-PRESSURE', 2)",
                             tr.get_fault_topology, "FM-APP-HEAP-PRESSURE", 2,
                             backend=backend, contract=contract)
        dump("A8/T4-T5 摘要", {k: f_rel[k] for k in ("depths", "observedOnly", "truncated")})
        dump("A8/T4-T5 的 relates_to 边", [e for e in f_rel["edges"] if e["relation"] == "relates_to"])
        check("A8/T4 真机存在 relation=='relates_to' 的边",
              any(edge["relation"] == "relates_to" for edge in f_rel["edges"]), None)
        check("A8/T5 真机沿 relates_to 到达的节点只观察不展开",
              "FM-HOST-MEM-PRESSURE" in f_rel["observedOnly"]
              and "FM-HOST-MEM-PRESSURE" not in f_rel["depths"]
              and not any(edge["from"] == "FM-HOST-MEM-PRESSURE" for edge in f_rel["edges"]),
              {"observedOnly": f_rel["observedOnly"], "depths": f_rel["depths"]})

        f_shallow, ms_f3a = timed("get_fault_topology('FM-APP-DS-CONNECT-FAIL', 2)",
                                  tr.get_fault_topology, "FM-APP-DS-CONNECT-FAIL", 2,
                                  backend=backend, contract=contract)
        f_full, ms_f3b = timed("get_fault_topology('FM-APP-DS-CONNECT-FAIL', 4)",
                               tr.get_fault_topology, "FM-APP-DS-CONNECT-FAIL", 4,
                               backend=backend, contract=contract)
        dump("A8/T3 摘要", {"max_hops=2": {k: f_shallow[k] for k in ("truncated", "depths")},
                            "max_hops=4": {k: f_full[k] for k in ("truncated", "depths")}})
        check("A8/T3 真机 max_hops=2 截断 / max_hops=4 对 F5 叶子根因不截断",
              f_shallow["truncated"] is True and f_full["truncated"] is False,
              (f_shallow["truncated"], f_full["truncated"]))

        f_clamp, ms_f6 = timed("get_fault_topology('FM-APP-POOL-EXHAUSTED', 99)",
                               tr.get_fault_topology, "FM-APP-POOL-EXHAUSTED", 99,
                               backend=backend, contract=contract)
        dump("A8/T6 夹取", {"appliedMaxHops": f_clamp["appliedMaxHops"],
                            "clamped": (f_clamp["truncation"] or {}).get("clamped"),
                            "warnings": f_clamp["warnings"]})
        check("A8/T6 真机 max_hops=99 夹到 4 且不报错",
              f_clamp["appliedMaxHops"] == contract.fault_max_hops == 4
              and bool((f_clamp["truncation"] or {}).get("clamped")), f_clamp["appliedMaxHops"])

        # ---- A9 边属性契约修正（★ 2026-10-06：撤回 5 个边属性）------------
        banned_hits = (_banned_key_hits(f_top) + _banned_key_hits(f_rel)
                       + _banned_key_hits(f_shallow) + _banned_key_hits(f_full))
        check("A9 故障拓扑出参不含 weight/probability/delaySec/chainPosition/learnCount",
              banned_hits == [], banned_hits)
        # 排序方法名是裁定指定的（`deterministic-no-weight-composition`，自身含 `weight` 子串），
        # 逐字剔除该串后再做子串扫描，保证「出参里不含被撤回属性名」这句话与裁定不冲突。
        def _sanitized(payload: object) -> str:
            return json.dumps(payload, ensure_ascii=False).replace("deterministic-no-weight-composition", "")

        subst_hits = sorted({name for name in BANNED_EDGE_KEYS
                             for payload in (f_top, f_rel, f_shallow, f_full)
                             if name in _sanitized(payload)})
        check("A9 出参里连 weight/probability/... 子串都不出现（裁定方法名已逐字剔除）",
              subst_hits == [], subst_hits)
        check("A9 ranking 用 rankingMethod/compositionApplied，且方法名不含 probability 子串",
              (f_top.get("ranking") or {}).get("rankingMethod") == "deterministic-no-weight-composition"
              and (f_top.get("ranking") or {}).get("compositionApplied") is False
              and "probability" not in json.dumps(f_top.get("ranking") or {}, ensure_ascii=False)
              and "method" not in (f_top.get("ranking") or {})
              and "probabilityComposition" not in (f_top.get("ranking") or {}),
              {k: v for k, v in (f_top.get("ranking") or {}).items() if k != "rootCandidates"})
        edge_keys = sorted({key for edge in f_top["edges"] for key in edge})
        check("A9 边仅保留 contractId/chain/description/provenance/condition(+cond*)",
              set(edge_keys) <= ALLOWED_FAULT_EDGE_KEYS, edge_keys)
        check("A9 每条边都带 contractId / chain / description（= evidence 原文）",
              bool(f_top["edges"]) and all(edge.get("contractId") and edge.get("chain") is not None
                                           and edge.get("description") for edge in f_top["edges"]),
              f_top["edges"][:1])
        cond_edges = [edge for edge in f_top["edges"] if edge.get("condRef") or edge.get("condMetric")]
        check("A9 条件边透出 condMetric/condOp/condKind/condRef（与 condition.operand 同源）",
              bool(cond_edges) and all(edge.get("condMetric") and edge.get("condOp")
                                       and edge.get("condition", {}).get("operand") for edge in cond_edges),
              cond_edges[:1])

        # ---- A10 出参形状（门面契约 / traversalScope / 取值来源标注）------
        facade_metrics = tr.to_facade(metrics)
        dump("A10 to_facade(get_metrics) 形状", {k: facade_metrics[k] for k in ("items", "truncated", "truncation")
                                                 if k in facade_metrics})
        check("A10 to_facade 把数组出参包成 {items,truncation,...}（must_report 可机械核验）",
              isinstance(facade_metrics.get("items"), list) and "truncation" in facade_metrics,
              sorted(facade_metrics))
        facade_topo = tr.to_facade(topo)
        check("A10 to_facade 对 dict 出参原样补齐 truncation/warnings",
              "truncation" in facade_topo and "warnings" in facade_topo
              and facade_topo["nodes"] == topo["nodes"], sorted(facade_topo)[:8])
        scope = topo.get("traversalScope") or {}
        dump("A10 traversalScope", scope)
        check("A10 traversalScope 明写方向 out + 排除派生边 + HOSTS 方向说明",
              scope.get("direction") == "out"
              and scope.get("excludesDerivedRelations") == ["HAS_EVIDENCE"]
              and bool(scope.get("hostsNote")) and bool(scope.get("inverseRelations")),
              {k: scope.get(k) for k in ("direction", "excludesDerivedRelations", "hostsNote")})
        meta = metrics.meta()
        sources = {row.get("source") for row in metrics}
        check("A10 指标行 source 标注元数据来源（CC_Metric / object_property）",
              sources <= {"CC_Metric", "object_property"} and bool(sources), sorted(sources))
        check("A10 指标行仍严格 §3.3 七字段（source 只是取值来源标注）",
              all(set(row) == {"object", "metric_code", "value", "unit", "ts", "threshold_ref", "source"}
                  for row in metrics), sorted(set(metrics[0]) if metrics else []))
        check("A10 get_metrics 元数据来源与 :CC_Metric 目录基数一致（两者必须同源）",
              meta["metricMetadata"]["catalogNodes"] == len(backend.fetch_metric_catalog()),
              meta["metricMetadata"])
        check("A10 get_facts 的 snapshotId 读自 :CC_Meta.snapshotId（图上无 CC_Meta 时为 null + 回退标注）",
              facts.meta()["snapshotId"] == (census_before.get("snapshotId") or None),
              {"facts": facts.meta()["snapshotId"], "graph": census_before.get("snapshotId")})
        snap_live = census_before.get("snapshotId")
        check("A10 真机快照 id 从 CC_Meta 读出（不是硬编码；装载前为 None 亦通过）",
              snap_live == (backend.fetch_graph_meta() or {}).get("snapshotId"),
              {"census": snap_live, "fetch_graph_meta": (backend.fetch_graph_meta() or {}).get("snapshotId")})

        # ---- A11 TBox 读取（CC_Metric / CC_Threshold，正反两态都能过）-----
        catalog = backend.fetch_metric_catalog()
        thresholds_live = backend.fetch_thresholds()
        labels = census_before.get("nodesByLabel") or {}
        dump("A11 TBox 计数", {"catalogNodes": len(catalog), "dirCC_Metric": labels.get("CC_Metric", 0),
                              "thresholds": len(thresholds_live), "dirCC_Threshold": labels.get("CC_Threshold", 0),
                              "CC_FaultMode": labels.get("CC_FaultMode", 0), "CC_Meta": labels.get("CC_Meta", 0)})
        check("A11 :CC_Metric 目录条数 == label 普查计数（同一真相源）",
              len(catalog) == labels.get("CC_Metric", 0), (len(catalog), labels.get("CC_Metric", 0)))
        check("A11 :CC_Threshold 条数 == label 普查计数",
              len(thresholds_live) == labels.get("CC_Threshold", 0),
              (len(thresholds_live), labels.get("CC_Threshold", 0)))
        check("A11 有 catalog 时指标行标注 CC_Metric、无 catalog 时退回 object_property（不用空值冒充）",
              (sources == {"CC_Metric"}) if catalog else (sources <= {"object_property"}),
              {"catalogNodes": len(catalog), "sources": sorted(sources)})
        check("A11 行数受 200 行 / 20000 字节双上限约束且截断显式上报",
              len(metrics) <= tr.ROW_LIMIT and isinstance(meta["truncation"]["truncated"], bool),
              {"rows": len(metrics), "truncated": meta["truncation"]["truncated"]})
        check("A11 :CC_Threshold 未标定级别读出来仍是 null（不得被折叠成 0）",
              all(all(spec.get(key) is None or isinstance(spec.get(key), (int, float))
                      for key in ("thrWarning", "thrCritical", "warning", "critical"))
                  for spec in thresholds_live.values()),
              {name: {k: spec.get(k) for k in ("thrWarning", "thrCritical")}
               for name, spec in list(thresholds_live.items())[:3]})

        # ---- A12 图侧边属性装载状态（结构不变量做断言；装载态只回显）-------
        rel_indexes = backend._run(
            "SHOW INDEXES YIELD name, entityType, labelsOrTypes, properties, state "
            "WHERE entityType = 'RELATIONSHIP' RETURN name, labelsOrTypes, properties, state")
        cause_rows = backend._run(
            "MATCH ()-[r:%s]->() RETURN count(r) AS total, "
            "count(r.contractId) AS withContractId" % contract.causes_reltype)
        relates_rows = backend._run(
            "MATCH ()-[r:%s]->() RETURN count(r) AS total, count(r.contractId) AS withContractId, "
            "count(r.chain) AS withChain, count(r.description) AS withDescription, "
            "count(r.provenance) AS withProvenance" % contract.relates_reltype)
        # 被撤回的 5 个属性在**两种关系类型上**都必须为 0（不只 CC_CAUSES）
        stale_clause = (" WHERE r.weight IS NOT NULL OR r.probability IS NOT NULL "
                        "OR r.delaySec IS NOT NULL OR r.chainPosition IS NOT NULL OR r.learnCount IS NOT NULL")
        stale_rows = backend._run(
            "MATCH ()-[r:%s]->()%s RETURN count(r) AS stale" % (contract.causes_reltype, stale_clause))
        stale_relates_rows = backend._run(
            "MATCH ()-[r:%s]->()%s RETURN count(r) AS stale" % (contract.relates_reltype, stale_clause))
        print("[INFO] A12 图侧边属性装载状态（结构不变量做断言；索引装载态只回显：索引 "
              "cc_idx_causes_edge_id_idx 应建在 contractId 上、两类关系的被撤回属性应为 0）")
        dump("A12 关系索引", rel_indexes)
        dump("A12 因果边计数", {"CC_CAUSES": cause_rows, "CC_RELATES_TO": relates_rows})
        dump("A12 仍带被撤回属性的边数", {"CC_CAUSES": stale_rows, "CC_RELATES_TO": stale_relates_rows})
        cause_row = cause_rows[0] if cause_rows else {}
        relates_row = relates_rows[0] if relates_rows else {}
        indexed_names = {row.get("name") for row in rel_indexes}
        check("A12 被撤回的 5 个边属性在 CC_CAUSES / CC_RELATES_TO 两类关系上均为 0",
              (stale_rows[0] if stale_rows else {}).get("stale") == 0
              and (stale_relates_rows[0] if stale_relates_rows else {}).get("stale") == 0,
              {"CC_CAUSES": stale_rows, "CC_RELATES_TO": stale_relates_rows})
        check("A12 CC_CAUSES 每条边都带 contractId（contractId 索引的建索引前提）",
              cause_row.get("total") == cause_row.get("withContractId") and bool(cause_row.get("total")),
              cause_row)
        check("A12 CC_RELATES_TO 每条边都带 contractId/chain/description/provenance",
              bool(relates_row.get("total"))
              and all(relates_row.get(key) == relates_row.get("total")
                      for key in ("withContractId", "withChain", "withDescription", "withProvenance")),
              relates_row)
        check("A12 契约 18 条有向边 = CC_CAUSES + CC_RELATES_TO（E16/E17 是 relates_to，本就不在 CC_CAUSES）",
              (cause_row.get("total") or 0) + (relates_row.get("total") or 0) == len(contract.causal_edges) == 18,
              {"CC_CAUSES": cause_row.get("total"), "CC_RELATES_TO": relates_row.get("total"),
               "contractEdges": len(contract.causal_edges)})
        check("A12 contractId 上的关系索引存在（cc_idx_causes_edge_id_idx，装载态由 SHOW INDEXES 回显）",
              "cc_idx_causes_edge_id_idx" in indexed_names, sorted(indexed_names))

        # ---- A7 用后普查：证明本次自测没有写图 -----------------------------
        census_after, ms_after = timed("census() [自测后]", backend.census)
        dump("A7 census 对比", {"before": census_before, "after": census_after})
        check("A7 自测前后图完全一致（只读自证；graphMeta.updatedAt 由装载者刷新，不计入比较）",
              all(census_before[key] == census_after[key]
                  for key in ("nodesByType", "edgesByType", "nodesByLabel",
                              "nodeTotal", "edgeTotal", "nodeTotalByLabel")),
              {"nodeTotal": (census_before["nodeTotal"], census_after["nodeTotal"]),
               "nodeTotalByLabel": (census_before["nodeTotalByLabel"], census_after["nodeTotalByLabel"])})
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# B. 离线 fixture 契约用例
# ---------------------------------------------------------------------------

class _NonWhitelistedCycleBackend(tr.ContractBackend):
    """在契约 fixture 上人为加一个白名单外的有向环，验证「记录 + 停止展开」而不是死循环。"""

    def fetch_causal_edges(self):
        rows = super().fetch_causal_edges()
        extra = [("FM-DB-LOCK-WAIT", "FM-CTR-OOM-KILLED"),
                 ("FM-CTR-OOM-KILLED", "FM-HOST-CPU-CONTENTION"),
                 ("FM-HOST-CPU-CONTENTION", "FM-DB-LOCK-WAIT")]
        for src, dst in extra:
            rows.append({"from": src, "reltype": self.contract.causes_reltype, "to": dst,
                         "props": {"contractId": "TEST-NONWHITELIST", "relation": "causes"}})
        return rows


def fixture_section(contract) -> None:
    print("\n================ B. 离线 fixture 契约用例（不连库） ================")
    backend = tr.ContractBackend(contract)
    dump("B0 fixture census", backend.census())
    dump("B0 深度语义（X16 机读核对）", {
        "topology_facts.max_hop_depth": (contract.topology.get("topology_facts") or {}).get("max_hop_depth"),
        "len(hop_path)": len(contract.hop_path),
        "business_hop_depth": contract.business_hop_depth,
        "tool.hard_limits.maxHopDepth": contract.contract_object_max_depth,
        "tool.defaults.maxHopDepth": contract.contract_object_default_depth,
        "depth_drift": contract.depth_drift(),
        "causal_transitive": contract.causal_transitive,
        "cycle_whitelist": contract.cycle_whitelist,
    })
    check("B0 X16 深度语义一致（hard_limits == defaults == len(hop_path)-1 == 9）",
          contract.business_hop_depth == contract.contract_object_max_depth
          == contract.contract_object_default_depth == 9 and not contract.depth_drift(), contract.depth_drift())
    check("B0 因果传递性被声明为 false", contract.causal_transitive is False, None)

    # T2：白名单环
    t2, ms_t2 = timed("T2 get_fault_topology('FM-APP-POOL-EXHAUSTED', 4)",
                      tr.get_fault_topology, "FM-APP-POOL-EXHAUSTED", 4, backend=backend, contract=contract)
    dump("T2 摘要", {k: t2[k] for k in ("root", "appliedMaxHops", "truncated", "cyclesEncountered", "depths")})
    dump("T2 nodes", t2["nodes"])
    dump("T2 edges", t2["edges"])
    dump("T2 warnings", t2["warnings"])
    check("T2 cyclesEncountered 非空", bool(t2["cyclesEncountered"]), t2["cyclesEncountered"])
    check("T2 环在白名单内（无白名单外告警）",
          not any("白名单外" in w for w in t2["warnings"]), t2["warnings"])

    # T3：深度截断（F5 叶子根因）
    t3a, ms_t3a = timed("T3a get_fault_topology('FM-APP-DS-CONNECT-FAIL', 2)",
                        tr.get_fault_topology, "FM-APP-DS-CONNECT-FAIL", 2, backend=backend, contract=contract)
    dump("T3a 摘要", {k: t3a[k] for k in ("appliedMaxHops", "truncated", "depths")})
    t3b, ms_t3b = timed("T3b get_fault_topology('FM-APP-DS-CONNECT-FAIL', 4)",
                        tr.get_fault_topology, "FM-APP-DS-CONNECT-FAIL", 4, backend=backend, contract=contract)
    dump("T3b 摘要", {k: t3b[k] for k in ("appliedMaxHops", "truncated", "depths")})
    check("T3 max_hops=2 截断（truncated=true）", t3a["truncated"] is True, t3a["truncated"])
    check("T3 max_hops=4 对 F5 叶子根因不截断（truncated=false）", t3b["truncated"] is False, t3b["truncated"])

    # T4/T5：relates_to 只返回、不展开
    t4, ms_t4 = timed("T4/T5 get_fault_topology('FM-APP-HEAP-PRESSURE', 2)",
                      tr.get_fault_topology, "FM-APP-HEAP-PRESSURE", 2, backend=backend, contract=contract)
    dump("T4 edges", t4["edges"])
    dump("T4/T5 摘要", {"depths": t4["depths"], "observedOnly": t4["observedOnly"]})
    rel = [edge for edge in t4["edges"] if edge["relation"] == "relates_to"]
    check("T4 存在 relation=='relates_to' 的边", bool(rel), rel)
    check("T5 沿 relates_to 到达的节点只观察不展开（无 depth，且其出边不出现）",
          "FM-HOST-MEM-PRESSURE" in t4["observedOnly"]
          and "FM-HOST-MEM-PRESSURE" not in t4["depths"]
          and not any(edge["from"] == "FM-HOST-MEM-PRESSURE" for edge in t4["edges"]),
          {"observedOnly": t4["observedOnly"], "depths": t4["depths"]})

    # T6：深度夹取
    t6, ms_t6 = timed("T6 get_fault_topology('FM-APP-POOL-EXHAUSTED', 99)",
                      tr.get_fault_topology, "FM-APP-POOL-EXHAUSTED", 99, backend=backend, contract=contract)
    dump("T6 摘要", {k: t6[k] for k in ("appliedMaxHops", "truncated", "cyclesEncountered")})
    dump("T6 truncation", t6["truncation"])
    dump("T6 warnings", t6["warnings"])
    check("T6 max_hops=99 夹到 4 且不报错", t6["appliedMaxHops"] == 4, t6["appliedMaxHops"])
    check("T6 夹取事实显式上报", bool((t6["truncation"] or {}).get("clamped")) and any("夹取" in w for w in t6["warnings"]),
          (t6["truncation"] or {}).get("clamped"))

    # 白名单外的环：记录 + 停止，不死循环
    odd = _NonWhitelistedCycleBackend(contract)
    t7, ms_t7 = timed("T7 白名单外环 get_fault_topology('FM-DB-LOCK-WAIT', 4)",
                      tr.get_fault_topology, "FM-DB-LOCK-WAIT", 4, backend=odd, contract=contract)
    dump("T7 cyclesEncountered", t7["cyclesEncountered"])
    dump("T7 warnings", t7["warnings"])
    check("T7 白名单外环被记录并上报（不死循环、不抛异常）",
          bool(t7["cyclesEncountered"]) and any("白名单外" in w for w in t7["warnings"]), t7["warnings"])

    dump("B 计时汇总", {"T2ms": round(ms_t2, 1), "T3a_ms": round(ms_t3a, 1), "T3b_ms": round(ms_t3b, 1),
                       "T4ms": round(ms_t4, 1), "T6ms": round(ms_t6, 1), "T7ms": round(ms_t7, 1)})

    # 对象接口对 fixture 的诚实降级
    empty, _ = timed("get_object_topology('app:cc-pay') [fixture]",
                     tr.get_object_topology, "app:cc-pay", backend=backend, contract=contract)
    dump("B8 fixture 对象拓扑（诚实降级）", {"nodes": empty["nodes"], "edges": empty["edges"],
                                            "warnings": empty["warnings"]})
    check("B8 fixture 下对象接口返回空 + 显式告警",
          empty["nodes"] == [] and any("fixture" in w for w in empty["warnings"]), empty["warnings"])

    # ---- B10 边属性契约修正（离线同样不得出现被撤回的 5 个键）----------
    fixture_banned = _banned_key_hits(t2) + _banned_key_hits(t7)
    check("B10 fixture 故障拓扑不含 weight/probability/delaySec/chainPosition/learnCount",
          fixture_banned == [], fixture_banned)
    fixture_edge_keys = sorted({key for edge in t2["edges"] for key in edge})
    check("B10 fixture 边仅保留 contractId/chain/description/provenance/condition(+cond*)",
          set(fixture_edge_keys) <= ALLOWED_FAULT_EDGE_KEYS, fixture_edge_keys)
    check("B10 fixture 边都带 contractId / chain / description",
          bool(t2["edges"]) and all(edge.get("contractId") and edge.get("chain") is not None
                                    and edge.get("description") for edge in t2["edges"]),
          t2["edges"][:1])

    # ---- B11 fixture 下的确定性排序与门面形状 -------------------------
    ranking = t2.get("ranking") or {}
    dump("B11 fixture ranking", {k: v for k, v in ranking.items() if k != "rootCandidates"})
    dump("B11 fixture rootCandidates", ranking.get("rootCandidates"))
    ranking_json = json.dumps(ranking, ensure_ascii=False).replace("deterministic-no-weight-composition", "")
    check("B11 fixture ranking 明写不做权重/概率合成，键名不含被撤回属性名",
          ranking.get("compositionApplied") is False
          and ranking.get("rankingMethod") == "deterministic-no-weight-composition"
          and "method" not in ranking and "probabilityComposition" not in ranking
          and not _banned_key_hits(ranking)
          and not [name for name in BANNED_EDGE_KEYS if name in ranking_json],
          {k: v for k, v in ranking.items() if k != "rootCandidates"})
    check("B11 fixture 候选按 (determined, -命中条款, 跳数, 关系优先级) 稳定排序",
          [c["rank"] for c in ranking.get("rootCandidates", [])] == list(range(1, len(ranking.get("rootCandidates", [])) + 1))
          and all(a["determined"] >= b["determined"] for a, b in
                  zip(ranking["rootCandidates"], ranking["rootCandidates"][1:])),
          [(c["rank"], c["fmCode"], c["determined"], c["satisfiedClauses"]) for c in ranking.get("rootCandidates", [])])
    check("B11 fixture 无实例数据时候选全为 undetermined（不用 0 冒充）",
          all(c["satisfiedClauses"] is None and c["determined"] is False
              for c in ranking.get("rootCandidates", [])),
          [(c["fmCode"], c["satisfiedClauses"], c["determined"]) for c in ranking.get("rootCandidates", [])])
    check("B11 fixture to_facade 包成 {items,truncation}",
          isinstance(tr.to_facade(t2).get("truncation"), dict)
          and tr.to_facade(getattr(tr, "ReportList")([], truncation={}, warnings=[]))["items"] == [],
          sorted(tr.to_facade(t2)))


# ---------------------------------------------------------------------------
# C. 静态自证：零变长路径 / 零写语句
# ---------------------------------------------------------------------------

def static_section() -> None:
    print("\n================ C. 静态自证（零变长路径 / 零写语句） ================")
    sources = {"topology_reader.py": tr.__file__,
               "_selftest_topology_reader.py": str(Path(__file__).resolve())}
    var_pat = re.compile(r"\*[0-9]*\.\.")
    write_pat = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|CALL|LOAD)\b")
    for name, path in sources.items():
        text = Path(path).read_text(encoding="utf-8")
        lines = text.splitlines()
        var_hits = [(index + 1, line.strip()) for index, line in enumerate(lines) if var_pat.search(line)]
        # 本扫描器自身的正则行会包含这些词，按构造排除（它不发出任何查询）
        write_hits = [(index + 1, line.strip()) for index, line in enumerate(lines)
                      if write_pat.search(line) and "re.compile" not in line and "re.findall" not in line]
        print("--- %s: 变长路径命中 %d 条；写关键字命中 %d 条 ---" % (name, len(var_hits), len(write_hits)))
        if var_hits:
            print(json.dumps(var_hits, ensure_ascii=False))
        if write_hits:
            print(json.dumps(write_hits, ensure_ascii=False))
        check("C %s 零变长路径" % name, not var_hits, len(var_hits))
        check("C %s 零写语句关键字" % name, not write_hits, len(write_hits))
    print("--- topology_reader.py 语句头（只读 MATCH 自证）---")
    text = Path(tr.__file__).read_text(encoding="utf-8")
    heads = sorted(set(re.findall(r'"((?:MATCH|MERGE|CREATE|UNWIND|CALL|DELETE|SET)\b[^"]{0,40})', text)))
    print(json.dumps(heads, ensure_ascii=False, indent=1))
    check("C 语句头全部是 MATCH", bool(heads) and all(item.startswith("MATCH") for item in heads), heads)

    # ---- 静态：不得硬编码 snapshotId；不得出现被撤回的 5 个边属性键字面量 ----
    # 口径说明（必须写清，否则「命中 0」与源码里出现这些词看起来自相矛盾）：
    #   key_pat 锚定的是**被引号包住、且紧跟冒号的键字面量** `"weight":` 这类形态，
    #   即「出参键名」。文档字符串里为说明「该属性已撤回」而提到的词、以及裁定指定的
    #   排序方法名（deterministic-no-weight-composition，见下）不计入——它们不是键。
    key_pat = re.compile(r'"(weight|probability|delaySec|chainPosition|learnCount)"\s*:')
    for name, path in sources.items():
        text = Path(path).read_text(encoding="utf-8")
        snap_hits = [(index + 1, line.strip()) for index, line in enumerate(text.splitlines())
                     if SNAPSHOT_LITERAL.search(line) and "SNAPSHOT_LITERAL" not in line]
        print("--- %s: snapshotId 字面量命中 %d 条 ---" % (name, len(snap_hits)))
        if snap_hits:
            print(json.dumps(snap_hits, ensure_ascii=False))
        check("C %s 零 snapshotId 字面量（快照一律读 CC_Meta）" % name, not snap_hits, len(snap_hits))
    text = Path(tr.__file__).read_text(encoding="utf-8")
    key_hits = [(index + 1, line.strip()) for index, line in enumerate(text.splitlines()) if key_pat.search(line)]
    print("--- topology_reader.py: 被撤回边属性键字面量（\"name\": 形态）命中 %d 条 ---" % len(key_hits))
    if key_hits:
        print(json.dumps(key_hits, ensure_ascii=False))
    check("C topology_reader.py 零 weight/probability/delaySec/chainPosition/learnCount 键字面量",
          not key_hits, len(key_hits))
    # 残余子串普查（非断言，只回显，用来让「命中 0」这句话站得住脚）
    subst_pat = re.compile(r"(weight|probability|delaySec|chainPosition|learnCount)")
    subst_lines = [(index + 1, line.strip()) for index, line in enumerate(text.splitlines())
                   if subst_pat.search(line)]
    print("--- topology_reader.py: 上述 5 个名的**残余子串**出现 %d 行（逐行列出；"
          "应为：裁定指定的排序方法名 + 说明撤回的文档字符串，均非出参键）---" % len(subst_lines))
    print(json.dumps(subst_lines, ensure_ascii=False, indent=1))
    check("C topology_reader.py 零 weight/probability/delaySec/chainPosition/learnCount 键字面量",
          not key_hits, len(key_hits))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="M2.9 自测（只读）")
    parser.add_argument("--uri", default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--database", default=None)
    parser.add_argument("--skip-live", action="store_true")
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("CC_NEO4J_PASSWORD")
    contract = tr.get_contract()
    static_section()
    fixture_section(contract)
    if args.skip_live:
        print("\n（--skip-live：跳过真机段）")
    else:
        if not password:
            print("\n（无口令：跳过真机段；可设 CC_NEO4J_PASSWORD 或传 --password）")
        else:
            try:
                live_section(contract, args.uri, args.user, password, args.database)
            except Exception:
                traceback.print_exc()
                check("A 真机段执行", False, "异常见上方 traceback")

    print("\n================ 自测汇总 ================")
    failed = [item for item in RESULTS if not item[1]]
    for name, ok, detail in RESULTS:
        if not ok:
            print("FAIL: %s — %s" % (name, detail))
    print("总计 %d 项：PASS %d / FAIL %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
