# -*- coding: utf-8 -*-
"""M1.5 · **P1 投影器：CCF → Neo4j（LPG / Cypher）**。

契约（`docs\\01` §1.1、`docs\\00` §6.3、裁决 13）：
  * 输入 = 一份 CCF 快照；载体名**只从 `shared/mapping/*.yaml` 读**，本文件不得硬编码
    `CC_` / `cc:` 字面量（`CC_` 前缀的取值来自 `namespaces.kg.label_prefix`）
  * 只投影 `relations[]`；4 条 `inverse_relations` 两侧都不建边（保证实例数可直接对账）
  * `status != collected` 落成 `ccfNotCollected` / `ccfCollectedEmpty` 两个**多值属性**；
    **空清单不落属性**（ONTO 侧同样不落空三元组，两侧必须一致）
  * 隔离：只碰带 `CC_` 前缀的标签，绝不触碰既有 v2 图（423 节点）
  * ★ M2.12：**ABox 装载即刷新图级元数据** `:<prefix>Meta {key:'graph'}`
    （`SET snapshotId = <CCF 的 snapshotId>` + 刷新 `updatedAt`；`createdAt` 首建才写、
    `schemaVersion`/`contractRef`/`ccfVersion` 缺则补），因为 TBox（01-schema.cypher）
    自 M2.12 起只写非法占位串 `UNSET-TBOX-LOAD` 且不覆盖 —— 图级快照 id 的权威在本投影器。
    对 CCF 出现多个 / 缺失 snapshotId **fail-closed**（非零退出，绝不部分装载）。

用法：
  set CC_NEO4J_PASSWORD=...
  python scheme1-kg/project/p1_neo4j.py --ccf tmp/ccf_live1.json \\
      --report tmp/p1_report_live.json [--wipe] [--dry-run]
"""

import argparse
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for rel in ("shared/project", "shared/verify"):
    sys.path.insert(0, os.path.join(ROOT, rel))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ccf_plan as cp  # noqa: E402

# ── M2.12 · 图级快照身份（`:CC_Meta`＝标签前缀 + Meta）─────────────────────────
# 标签名一律由映射表 `namespaces.kg.label_prefix` 拼装（G15/L6：命名空间字面量只许出现在
# 映射表里，投影器源码不得出现 `CC_`）。属性名不在 L6 的命名空间正则口径内，且
# `shared/mapping/type-map.yaml` 没有 Meta 的属性映射（Meta 只存在于 01-schema 的 TBox），
# 故按常量写死 —— 与 `scheme1-kg/ontology/01-schema.cypher` 的 §2.4 块逐字对齐。
META_KEY = "graph"
META_PROP_SNAPSHOT = "snapshotId"
META_PROP_CREATED = "createdAt"
META_PROP_UPDATED = "updatedAt"
META_PROP_SCHEMA = "schemaVersion"
META_PROP_CONTRACT = "contractRef"
META_PROP_CCFVER = "ccfVersion"
META_SCHEMA_VERSION = "kg-1.0.0"
META_CONTRACT_REF = "shared/model/*.yaml"
# 快照 id 格式（生成侧 = shared/collect/freeze.py；与 G19 graph_meta_gate.py 同口径）
SNAPSHOT_ID_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")

# CCF 的行级载体（顶层 `snapshotId` 的权威性由这三个段落交叉校验；见 resolve_snapshot_id）
CCF_SECTIONS = ("objects", "relations", "observations")


def read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_snapshot_id(ccf):
    """★ M2.12 fail-closed：CCF 必须**恰好**确定一个 snapshotId，否则拒绝投影。

    口径（docs/03 §2.4 硬约束 C1/R5「缺 snapshotId 的图不得进评测」的投影侧执行点）：
      * 顶层 `snapshotId` 是权威（采集层 `shared/collect/freeze.py` 生成，格式
        ccf-YYYYMMDDThhmmssZ-xxxxxx）；
      * 若 objects / relations / observations 的行内也带 `snapshotId`，其取值必须 ∈ {顶层值}；
        （真机 CCF（M1.6 起）的行级**不带**该字段，但这条交叉校验必须活着——否则一旦行级
        真的出现第二个快照，检查就成了死的"恒真门禁"。）
      * 顶层缺失 / 格式非法 / 全体出现取值集合 ≠ 1 个元素（>1 或空）⇒ 返回非空 errors，
        调用方 `sys.exit` 非零退出并**绝不部分装载**。
    返回 (snapshot_id, errors)。
    """
    errs = []
    raw = getattr(ccf, "raw", None) or {}
    top = raw.get("snapshotId")
    if not isinstance(top, str) or not top:
        errs.append("CCF 顶层 `snapshotId` 缺失/为空（硬约束 C1/R5：缺 snapshotId 的快照不得进图）")
    elif not SNAPSHOT_ID_RE.match(top):
        errs.append("CCF 顶层 `snapshotId`=%r 不满足格式 %s" % (top, SNAPSHOT_ID_RE.pattern))

    rows_seen = {}   # 行级取值 -> 出现的段落
    for section in CCF_SECTIONS:
        for row in (raw.get(section) or []):
            if not isinstance(row, dict):
                continue
            val = row.get("snapshotId")
            if val is None or val == "":
                continue
            rows_seen.setdefault(str(val), set()).add(section)
    for val in sorted(rows_seen):
        if top and val != str(top):
            errs.append("CCF 行级 `snapshotId`=%r（出现在 %s）与顶层 %r 不一致 ⇒ 跨快照混装"
                        % (val, "+".join(sorted(rows_seen[val])), top))

    all_vals = set(rows_seen)
    if isinstance(top, str) and top:
        all_vals.add(top)
    if not all_vals:
        errs.append("CCF 的 %s 里未发现任何 `snapshotId` ⇒ 拒绝投影"
                    % "/".join("`%s`" % s for s in CCF_SECTIONS))
    elif len(all_vals) > 1:
        errs.append("CCF 出现 %d 个不同 `snapshotId`：%s ⇒ 拒绝投影（绝不部分装载）"
                    % (len(all_vals), ", ".join(sorted(all_vals))))
    return (top if isinstance(top, str) and top else None), errs


def load_driver(uri, user, password):
    try:
        from neo4j import GraphDatabase
    except ImportError:
        sys.exit("缺 neo4j 驱动：pip install neo4j")
    try:
        return GraphDatabase.driver(uri, auth=(user, password),
                                    notifications_min_severity="OFF")
    except (TypeError, ValueError):
        return GraphDatabase.driver(uri, auth=(user, password))


def build_rows(plan, maps):
    """把 Plan 折成三类可批量写入的行。"""
    reserved = {
        "id": maps.ns["kg"]["instance_id_property"],
        "snapshot": maps.ns["kg"]["snapshot_property"],
        "layer": maps.ns["kg"]["layer_property"],
        "type": maps.ns["kg"]["neutral_type_property"],
    }
    nc = maps.obs_policy["kg"]["not_collected"]
    ce = maps.obs_policy["kg"]["collected_empty"]

    nodes_by_label = {}
    for n in plan.nodes:
        label = "`%s`" % maps.kg_label(n.type)
        props = {}
        for k, v in n.key.items():
            props[k] = v
        for k, v in n.attrs.items():
            props[k] = v
        for code, value, _unit, _oid in n.values:
            props[maps.metric(code)["kg_property"]] = value
        if n.not_collected:
            props[nc] = list(n.not_collected)
        if n.collected_empty:
            props[ce] = list(n.collected_empty)
        props[reserved["id"]] = n.id
        props[reserved["type"]] = n.type
        props[reserved["layer"]] = n.layer
        props[reserved["snapshot"]] = plan.ccf.snapshot_id
        nodes_by_label.setdefault(label, []).append(props)

    edges_by_key = {}
    by_id = {n.id: n for n in plan.nodes}
    for e in plan.edges:
        spec = maps.relation(e.type)
        fl = "`%s`" % maps.kg_label(by_id[e.from_id].type)
        tl = "`%s`" % maps.kg_label(by_id[e.to_id].type)
        rl = "`%s`" % spec["kg_reltype"]
        key = (rl, fl, tl)
        edges_by_key.setdefault(key, []).append(
            {"from": e.from_id, "to": e.to_id, "rid": e.id,
             "snap": plan.ccf.snapshot_id})

    # ★ M1.5c：派生证据投影（每条 CCF 观测一个证据节点 + 一条 对象→证据 边）。
    #   粒度 = U-Q 裁定 A（不聚合），用途 = C5 逐条溯源 + `false_claim` 判定。
    #   注意：annotation 型的 layer 为 null ⇒ **不得**写 layer 属性（两侧一致，
    #   否则 Neo4j 拒 null 而 GraphDB 静默丢三元组，等价性对账立刻破）。
    ev_type = maps.evidence_type()
    ev_rel = maps.evidence_relation()
    ev_label = "`%s`" % maps.kg_label(ev_type)
    ev_reltype = "`%s`" % maps.relation(ev_rel)["kg_reltype"]
    for ev in plan.evidence:
        props = dict(ev["props"])
        props[reserved["id"]] = ev["recordId"]
        props[reserved["type"]] = ev_type
        props[reserved["snapshot"]] = plan.ccf.snapshot_id
        nodes_by_label.setdefault(ev_label, []).append(props)
    for ev in plan.evidence_edges:
        owner = by_id.get(ev["objectId"])
        if owner is None:
            continue
        fl = "`%s`" % maps.kg_label(owner.type)
        key = (ev_reltype, fl, ev_label)
        edges_by_key.setdefault(key, []).append(
            {"from": ev["objectId"], "to": ev["recordId"], "rid": ev["recordId"],
             "snap": plan.ccf.snapshot_id})
    return nodes_by_label, edges_by_key, reserved


def jsonable_props(props):
    """neo4j 驱动回读的 temporal 值（DateTime/Duration/Point）不是 JSON 可序列化的，
    报告落盘前统一降级为字符串（**只影响报告**，图上的真值不受影响）。"""
    out = {}
    for k, v in (props or {}).items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif hasattr(v, "iso_format"):
            out[k] = v.iso_format()
        else:
            out[k] = str(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccf", required=True)
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"))
    ap.add_argument("--targets", default=os.path.join(ROOT, "shared", "deploy", "targets.yaml"))
    ap.add_argument("--uri", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--report", default=None)
    ap.add_argument("--wipe", action="store_true",
                    help="先删除 ABox 节点（仅 shared/model/topology.yaml#types 的标签 + 本次投影标签；"
                         "TBox 与既有 v2 图不触碰）")
    ap.add_argument("--wipe-tbox", action="store_true",
                    help="显式允许 --wipe 连同删除 TBox 节点（FaultMode/Metric/Threshold/Rule/"
                         "Finding/Action/Meta 等类型，真机当前 85 个）；不给则 TBox 一律保留")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    targets = read_yaml(args.targets)
    neo = targets["neo4j"]
    uri = args.uri or neo["bolt_uri"]
    user = args.user or neo["auth"]["user"]
    password = args.password or os.environ.get(neo["auth"]["password_env"])
    prefix = read_yaml(os.path.join(args.mapping_dir, "type-map.yaml"))["namespaces"]["kg"]["label_prefix"]
    cprefix = read_yaml(os.path.join(args.mapping_dir, "type-map.yaml"))["namespaces"]["kg"]["constraint_prefix"]

    plan = cp.build_plan(args.ccf, args.mapping_dir, args.model_dir)
    plan.print_issues()
    errs = plan.errors()
    if errs:
        sys.exit("★ CCF 未通过校验（%d ERROR），拒绝投影" % len(errs))

    # ★ M2.12 fail-closed：快照身份必须唯一确定，否则**一个节点都不写**。
    snapshot_id, snap_errs = resolve_snapshot_id(plan.ccf)
    if snap_errs:
        sys.exit("★ CCF 快照身份未通过 fail-closed 校验（%d ERROR），拒绝投影（绝不部分装载）：\n  - %s"
                 % (len(snap_errs), "\n  - ".join(snap_errs)))
    print("快照身份 fail-closed 校验通过：snapshotId = %s" % snapshot_id)

    maps = plan.maps
    nodes_by_label, edges_by_key, reserved = build_rows(plan, maps)
    # ★ M1.8a：wipe 的作用域 = ABox 标签（数据来源：topology.yaml#types + 本次投影实际使用的标签），
    #   不是「所有 CC_ 前缀」——后者会把 M2 期持久化的 TBox 节点一并删掉。
    abox_labels = sorted(
        {label.strip("`") for label in nodes_by_label}
        | {maps.kg_label(t["code"]) for t in
           (read_yaml(os.path.join(args.model_dir, "topology.yaml")).get("types") or [])})
    # 关系上的 ccfId / ccfSnapshotId 属性名同样来自映射表（common_properties.kg）
    rel_id_prop = maps.common["kg"]["relation_id"]
    # 三态（notCollected / collectedEmpty）承载属性名来自 observation_projection
    nc = maps.obs_policy["kg"]["not_collected"]
    ce = maps.obs_policy["kg"]["collected_empty"]
    expect = {
        "objects": len(plan.nodes),
        "relations": len(plan.edges),
        "observationsCollected": sum(len(n.values) for n in plan.nodes),
        "observationsNotCollected": sum(len(n.not_collected) for n in plan.nodes),
        "observationsCollectedEmpty": sum(len(n.collected_empty) for n in plan.nodes),
        # ★ M1.5c：派生证据也进期望数（byType/byRelation 回读侧已按 concrete_types /
        #   rel_map["relations"] 自动覆盖，无需另加分支）。
        "evidence": len(plan.evidence),
        "evidenceEdges": len(plan.evidence_edges),
    }

    dry = {"uri": uri, "database": args.database, "labels": sorted(nodes_by_label),
           "aboxLabels": abox_labels,
           "edgeGroups": ["%s (%s -> %s)" % k for k in sorted(edges_by_key)],
           "expect": expect}
    if args.dry_run or not password:
        print(json.dumps(dry, ensure_ascii=False, indent=1))
        if not password:
            print("★ 未取到口令（环境变量 %s），仅做 dry-run" % neo["auth"]["password_env"])
        return 0

    driver = load_driver(uri, user, password)
    t_conn = time.time()
    with driver.session(database=args.database) as sess:
        sess.run("RETURN 1").consume()
        print("已连接 %s（database=%s，%.2fs）" % (uri, args.database, time.time() - t_conn))

        if args.wipe:
            # ★ M1.8a：--wipe 只删 ABox。旧实现用「映射表前缀 + START WITH」一刀切，会把 M2 期
            #   已持久化的 TBox 节点（FaultMode/Metric/Threshold/Rule/Meta 等类型，真机 CC_ 共
            #   343 = ABox 258 + TBox 85）一起删掉。现在：ABox 按 abox_labels 精确匹配；TBox 默认
            #   保留，只有显式 --wipe-tbox 才删。
            tbox_q = ("MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH $p) "
                      "AND NOT any(l IN labels(n) WHERE l IN $abox) ")
            tbox_n = sess.run(tbox_q + "RETURN count(n) AS c",
                              p=prefix, abox=abox_labels).single()["c"]
            if tbox_n and args.wipe_tbox:
                res = sess.run(tbox_q + "DETACH DELETE n RETURN count(n) AS c",
                               p=prefix, abox=abox_labels).single()
                print("wipe-tbox：显式删除 TBox 节点 %d 个（--wipe-tbox 已给出）" % res["c"])
            elif tbox_n:
                print("wipe：检测到 %d 个 TBox 节点（标签不属于 topology.yaml#types）—— **默认保留**；"
                      "确需连同删除时显式加 --wipe-tbox" % tbox_n)
            res = sess.run(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $abox) "
                "DETACH DELETE n RETURN count(n) AS c", abox=abox_labels).single()
            print("wipe：删除 ABox 节点 %d 个（仅 topology.yaml#types + 本次投影标签；"
                  "既有 v2 图与 TBox 未触碰）" % res["c"])

        # 约束（索引名带项目前缀，与既有 gdo_uq_* 区分）
        for type_code in maps.concrete_types():
            label = maps.kg_label(type_code)
            cname = "%s%s" % (cprefix, label.lower())
            sess.run(
                "CREATE CONSTRAINT `%s` IF NOT EXISTS FOR (n:`%s`) REQUIRE n.`%s` IS UNIQUE"
                % (cname, label, reserved["id"])).consume()
        print("约束就绪：%d 条（前缀 %s）" % (len(maps.concrete_types()), cprefix))

        # 节点
        n_nodes = 0
        for label in sorted(nodes_by_label):
            rows = nodes_by_label[label]
            q = "UNWIND $rows AS r MERGE (n:%s {%s: r.`%s`}) SET n += r" % (
                label, reserved["id"], reserved["id"])
            for i in range(0, len(rows), 500):
                sess.run(q, rows=rows[i:i + 500]).consume()
            n_nodes += len(rows)

        # 边
        n_edges = 0
        for (rl, fl, tl) in sorted(edges_by_key):
            rows = edges_by_key[(rl, fl, tl)]
            q = ("UNWIND $rows AS r MATCH (a:%s {%s: r.from}), (b:%s {%s: r.to}) "
                 "MERGE (a)-[e:%s]->(b) SET e.`%s` = r.rid, e.`%s` = r.snap"
                 % (fl, reserved["id"], tl, reserved["id"], rl,
                    rel_id_prop, reserved["snapshot"]))
            sess.run(q, rows=rows).consume()
            n_edges += len(rows)

        # ★ M2.12：ABox 装载即刷新**图级元数据**单例（标签 = 映射表 label_prefix + "Meta"）。
        #   TBox（01-schema.cypher §2.4）自 M2.12 起只写非法占位串 `UNSET-TBOX-LOAD` 且
        #   ON CREATE-only（不覆盖），所以"当前图是哪一份快照"这件事**只能**由本投影器落库。
        #   createdAt 只在首建写；schemaVersion / contractRef / ccfVersion 缺则补（coalesce）。
        #   返回 properties(m) 以便报告留痕与 G19 对账。
        meta_label = prefix + "Meta"
        meta_q = ("MERGE (m:`%s` {key: $key}) "
                  "ON CREATE SET m.`%s` = datetime() "
                  "SET m.`%s` = $snap, "
                  "    m.`%s` = datetime(), "
                  "    m.`%s` = coalesce(m.`%s`, $schemaVersion), "
                  "    m.`%s` = coalesce(m.`%s`, $contractRef), "
                  "    m.`%s` = coalesce(m.`%s`, $ccfVersion) "
                  "RETURN properties(m) AS props"
                  % (meta_label, META_PROP_CREATED,
                     META_PROP_SNAPSHOT, META_PROP_UPDATED,
                     META_PROP_SCHEMA, META_PROP_SCHEMA,
                     META_PROP_CONTRACT, META_PROP_CONTRACT,
                     META_PROP_CCFVER, META_PROP_CCFVER))
        meta_props = jsonable_props(dict(sess.run(
            meta_q, key=META_KEY, snap=snapshot_id,
            schemaVersion=META_SCHEMA_VERSION, contractRef=META_CONTRACT_REF,
            ccfVersion=plan.ccf.ccf_version).single()["props"]))
        print("图级元数据已刷新：`:%s` %s = %s（%s = %s）"
              % (meta_label, META_PROP_SNAPSHOT, meta_props.get(META_PROP_SNAPSHOT),
                 META_PROP_UPDATED, meta_props.get(META_PROP_UPDATED)))

        # 只读回读（不信任写入时的计数）
        actual = {"byType": {}, "byRelation": {}, "ccNodes": 0,
                  "metricValueCounts": {}, "notCollectedCounts": {},
                  "collectedEmptyCounts": {}}
        for type_code in maps.concrete_types():
            label = maps.kg_label(type_code)
            rec = sess.run("MATCH (n:`%s`) RETURN count(n) AS c" % label).single()
            actual["byType"][type_code] = rec["c"]
            actual["ccNodes"] += rec["c"]
        for rel_code in sorted(maps.rel_map["relations"]):
            rl = maps.relation(rel_code)["kg_reltype"]
            rec = sess.run("MATCH ()-[e:`%s`]->() RETURN count(e) AS c" % rl).single()
            actual["byRelation"][rel_code] = rec["c"]

        # 观测投影回读：逐指标码精确计数（与 GraphDB 侧 SPARQL **同口径**）。
        # ★ 口径由 shared/project/ccf_plan.py 唯一定义：按 (对象类型, 属性) 双侧限定。
        #   不能写 `MATCH (n) WHERE n.<prop> IS NOT NULL` —— 本实例与既有 v2 图共库，
        #   全局口径会把 v2 图里同名属性数进来（实测 v2 的 Session/RuntimeEntity 带
        #   `waitEvent`），且跨类型复用的同名属性（如 `cpuUsageRatio`）会串数。
        present = sorted({code for n in plan.nodes for code, _v, _u, _o in n.values})
        for code in present:
            rec = sess.run(maps.kg_metric_count_query(code)).single()
            actual["metricValueCounts"][code] = rec["c"]
        for key, prop in (("notCollectedCounts", nc), ("collectedEmptyCounts", ce)):
            for rec in sess.run(maps.kg_scope_query(prop)):
                actual[key][rec["metricCode"]] = rec["c"]

    driver.close()

    expect["metricValueCounts"] = {}
    expect["notCollectedCounts"] = {}
    expect["collectedEmptyCounts"] = {}
    for n in plan.nodes:
        for code, _v, _u, _o in n.values:
            expect["metricValueCounts"][code] = expect["metricValueCounts"].get(code, 0) + 1
        for code in n.not_collected:
            expect["notCollectedCounts"][code] = expect["notCollectedCounts"].get(code, 0) + 1
        for code in n.collected_empty:
            expect["collectedEmptyCounts"][code] = expect["collectedEmptyCounts"].get(code, 0) + 1

    report = {
        "carrier": "neo4j",
        "uri": uri,
        "database": args.database,
        "snapshotId": plan.ccf.snapshot_id,
        "ccfPath": plan.ccf.path,
        "graphMeta": meta_props,
        "written": {"nodes": n_nodes, "edges": n_edges},
        "expect": expect,
        "actual": actual,
        "elapsedSec": round(time.time() - t0, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.report:
        with io.open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=False)
        print("报告写出：%s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
