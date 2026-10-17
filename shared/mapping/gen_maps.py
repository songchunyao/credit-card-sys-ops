#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M1.5 映射表生成器（**权威副本在 `shared/mapping/gen_maps.py`**；`tmp/` 下为工作副本）。

生成器本身是**可复现的构建脚本**，不是一次性脚本：映射表由 8 份契约重新生成，
手工只审阅产物。改动契约（`topology.yaml` / `metrics.yaml` / `ccf.schema.json`）后
必须重跑本脚本，而不是手工改表。

读：`shared/model/topology.yaml` + `metrics.yaml` + `ccf.schema.json`
写：`shared/mapping/type-map.yaml` + `shared/mapping/rel-map.yaml`

命名政策（裁决 13 + `01-` §1.1）：
  KG   = Neo4j/LPG：标签与关系类型加命名空间前缀 `CC_`（与既有 v2 图共库共存），
         属性为小驼峰（`topology.yaml` 的 attrs 键原样、指标码取末段转小驼峰）
  ONTO = RDF/OWL：类 `cc:<Type>`、对象属性 `cc:<lowerCamel>`（18 条显式冻结）、
         指标属性 `ccm:<lowerCamel>`
"""
import io
import json
import sys

import yaml

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOPOLOGY = "shared/model/topology.yaml"
METRICS = "shared/model/metrics.yaml"
SCHEMA = "shared/model/ccf.schema.json"
OUT_TYPE = "shared/mapping/type-map.yaml"
OUT_REL = "shared/mapping/rel-map.yaml"

LABEL_PREFIX = "CC_"
RELTYPE_PREFIX = "CC_"
CONSTRAINT_PREFIX = "cc_uq_"

ONTO_BASE = "https://credit-card-ops.ai/onto/"
NS = {
    "cc": ONTO_BASE + "topology#",
    "cco": ONTO_BASE + "core#",
    "ccm": ONTO_BASE + "metric#",
    "ccf": ONTO_BASE + "fault#",
    "ccc": ONTO_BASE + "causal#",
    "cca": ONTO_BASE + "action#",
    "ccp": ONTO_BASE + "prov#",
    "ccs": ONTO_BASE + "shapes#",
}

# 18 条对象属性的 OWL 名（14 条契约关系 + 4 条反向属性）——显式冻结，不得由代码推导。
# 依据：`docs/04` §3.2 / §7 里已经出现的名字；其余按小驼峰补齐。
ONTO_OBJ_PROP = {
    # 契约 relations（asserted: true）
    "HAS_APP_INSTANCE": "hasAppInstance",
    "EXPOSES": "exposes",
    "RUNS_IN": "runsIn",
    "DEPLOYED_ON": "deployedOn",
    "USES_DATASOURCE": "usesDataSource",   # ★ 不是 usesDatasource
    "CONNECTS_TO": "connectsTo",
    "RESOLVES_TO": "resolvesTo",
    "HAS_DB_INSTANCE": "hasDbInstance",
    "HOSTS": "hosts",
    "HAS_SESSION_POOL": "hasSessionPool",
    "HAS_SESSION": "hasSession",
    "RUNS_QUERY": "runsQuery",
    "ACCESSES": "accesses",
    "REPLICATES_TO": "replicatesTo",
    # ★ M1.5c（2026-10-05）：证据关系（from: "*" 域不受限；由 observations[] 派生）
    "HAS_EVIDENCE": "hasEvidence",
    # 契约 inverse_relations（asserted: false；OWL 侧声明为 owl:inverseOf 对端）
    "INSTANCE_OF": "instanceOf",
    "RUNS": "runs",
    "HOSTS_CONTAINER": "hostsContainer",
    "ACCEPTS_CONNECTION_FROM": "acceptsConnectionFrom",
}

XSD_NS = "http://www.w3.org/2001/XMLSchema#"

# ★ M1.5c：派生证据型（observations[] → 证据节点，两侧对称投影）
#   EVIDENCE_ATTRS 的键 = topology.yaml 里 Evidence 声明的属性名；
#   值 = CCF observation 里的**字段路径**（`a|b` = 取第一个存在者；缺字段 ⇒ 不写属性）。
#   这里是唯一授权出现这些字段名的地方，投影器只读产物表。
EVIDENCE_TYPE = "Evidence"
EVIDENCE_REL = "HAS_EVIDENCE"
EVIDENCE_ATTRS = (
    ("recordId", "id"),
    ("metricCode", "metricCode"),
    ("status", "status"),
    ("rawValue", "value|valueText"),
    ("rawUnit", "unit"),
    ("reason", "reason"),
    ("probe", "provenance.probe"),
    ("capturedAt", "observedAt"),
)
XSD = {
    "string": "xsd:string",
    "integer": "xsd:integer",
    "number": "xsd:decimal",
    "decimal": "xsd:decimal",
    "boolean": "xsd:boolean",
    "datetime": "xsd:dateTime",
}


def lower_camel(snake):
    parts = snake.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def load(p):
    with io.open(p, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main():
    topo = load(TOPOLOGY)
    _mlist = load(METRICS)["metrics"]
    if isinstance(_mlist, list):
        metrics = {}
        for _m in _mlist:
            metrics[_m["code"]] = _m
    else:
        metrics = _mlist
    schema = json.load(io.open(SCHEMA, encoding="utf-8"))
    metric_codes = schema["$defs"]["metricCode"]["enum"]
    type_codes = schema["$defs"]["typeCode"]["enum"]
    rel_codes = schema["$defs"]["relationCode"]["enum"]

    problems = []

    # ---------- 类型 ----------
    types = {}
    for spec in topo["types"]:
        code = spec["code"]
        if code in ("ConnectionPool",):
            abstract = True
        else:
            abstract = bool(spec.get("abstract"))
        # ★ M1.5c：`annotation: true` = 派生型（Evidence）——不是 CCF 的对象类型，
        #   不出现在 objects[]，也不进 ccf.schema.json 的 typeCode enum。
        annotation = bool(spec.get("annotation"))
        attrs = spec.get("attrs") or {}
        entry = {
            "layer": spec.get("layer"),
            "abstract": abstract,
            "annotation": annotation,
            "kg_label": LABEL_PREFIX + code,
            "onto_class": "cc:" + code,
            "key_attrs": list((spec.get("key_attrs") or [])),
            "attrs": {},
        }
        if spec.get("parent"):
            entry["onto_superclass"] = "cc:" + spec["parent"]
        for name, meta in attrs.items():
            entry["attrs"][name] = {
                "kg_property": name,
                "onto_property": "cc:" + name,
                "xsd": XSD.get(meta.get("type"), "xsd:string"),
            }
        types[code] = entry

    # ---------- 关系 ----------
    relations = {}
    for spec in topo["relations"]:
        code = spec["code"]
        if code not in ONTO_OBJ_PROP:
            problems.append("关系 %s 缺 OWL 属性名冻结值" % code)
            continue
        entry = {
            "from": spec["from"],
            "to": spec["to"],
            "cardinality": spec["cardinality"],
            "cross_layer": bool(spec["cross_layer"]),
            "kg_reltype": RELTYPE_PREFIX + code,
            "onto_property": "cc:" + ONTO_OBJ_PROP[code],
            # ★ M1.5c：asserted = 该关系是否由 CCF 的 relations[] 承载。
            #   false 的关系（HAS_EVIDENCE）由派生源（observations[]）产生，
            #   投影器照样要写进两侧，但 ccf.schema.json 的 relationCode enum 里不该有它。
            "asserted": bool(spec.get("asserted_in_ccf", True)),
        }
        if not entry["asserted"]:
            entry["derived_from"] = spec.get("derived_from")
        if spec.get("inverse"):
            inv = spec["inverse"]
            entry["inverse_code"] = inv
            entry["onto_inverse_property"] = "cc:" + ONTO_OBJ_PROP.get(inv, "?")
        relations[code] = entry

    inverses = {}
    for spec in topo["inverse_relations"]:
        code = spec["code"]
        if code not in ONTO_OBJ_PROP:
            problems.append("反向关系 %s 缺 OWL 属性名冻结值" % code)
            continue
        inverses[code] = {
            "from": spec["from"],
            "to": spec["to"],
            "inverse_code": spec["inverse"],
            "kg_reltype": RELTYPE_PREFIX + code,
            "onto_property": "cc:" + ONTO_OBJ_PROP[code],
            "asserted": False,
            "asserted_kg": False,
            "asserted_onto": False,
            "note": "OWL 侧由 owl:inverseOf 免费获得；CCF 的 relations[] 不得出现本关系",
        }

    # ---------- 指标 ----------
    metric_map = {}
    seen = {}
    for code in metric_codes:
        spec = metrics.get(code)
        if spec is None:
            problems.append("指标 %s 在 metrics.yaml 里不存在" % code)
            continue
        obj = spec["object"]
        leaf = code.split(".")[-1]
        prop = lower_camel(leaf)
        if obj not in types and obj != "ConnectionPool":
            problems.append("指标 %s 的 object=%s 不在 types 里" % (code, obj))
        key = (obj, prop)
        if key in seen:
            problems.append("指标属性名撞车：%s 与 %s 都映射成 %s.%s"
                            % (code, seen[key], obj, prop))
        seen[key] = code
        metric_map[code] = {
            "object": obj,
            "layer": spec.get("layer"),
            "kg_property": prop,
            "onto_property": "ccm:" + prop,
            "xsd": XSD.get(spec.get("type"), "xsd:string"),
            "unit": spec.get("unit"),
        }

    for code in metrics:
        if code not in metric_codes:
            problems.append("metrics.yaml 的 %s 不在 ccf.schema.json 的 metricCode enum 里"
                            % code)
    for code in type_codes:
        if code not in types:
            problems.append("typeCode %s 不在 topology.yaml.types 里" % code)
    for code in rel_codes:
        if code not in relations:
            problems.append("relationCode %s 不在 topology.yaml.relations 里" % code)

    # ---------- M1.5c 派生证据型的自洽（fail-closed） ----------
    if EVIDENCE_TYPE not in types:
        problems.append("topology.yaml.types 里没有派生证据型 %s" % EVIDENCE_TYPE)
    else:
        ev_attrs = set(types[EVIDENCE_TYPE]["attrs"])
        for k, _path in EVIDENCE_ATTRS:
            if k not in ev_attrs:
                problems.append("evidence.attrs 的 %s 不是 %s 声明的属性（topology.yaml）"
                                % (k, EVIDENCE_TYPE))
        if not types[EVIDENCE_TYPE]["annotation"]:
            problems.append("%s 必须标注 annotation: true（它不是 CCF 的对象类型）"
                            % EVIDENCE_TYPE)
    if EVIDENCE_REL not in relations:
        problems.append("topology.yaml.relations 里没有 %s" % EVIDENCE_REL)
    else:
        if relations[EVIDENCE_REL]["to"] != EVIDENCE_TYPE:
            problems.append("%s 的 to 必须是 %s" % (EVIDENCE_REL, EVIDENCE_TYPE))
        if relations[EVIDENCE_REL]["from"] != "*":
            problems.append("%s 的 from 必须是 \"*\"（域不受限：证据可挂在任何对象上）"
                            % EVIDENCE_REL)

    if problems:
        print("★ 生成前发现 %d 个问题，已中止（不写文件）：" % len(problems))
        for p in problems:
            print("  -", p)
        raise SystemExit(2)

    # ---------- 写出 ----------
    L = []
    a = L.append
    a("# ============================================================================")
    a("# type-map.yaml · 中性名 → KG 名 / ONTO 名的**显式映射表**（是数据不是代码）")
    a("# ----------------------------------------------------------------------------")
    a("# 权威出处：docs\\01-场景建模与范围定义.md §1.1「强制」、docs\\00 §6.3、裁决 13")
    a("# 输入契约：shared/model/topology.yaml + metrics.yaml + ccf.schema.json")
    a("# 消费者：  投影器 P1（Neo4j）/ P2（GraphDB）——**只读本表，不得硬编码载体名**")
    a("# ----------------------------------------------------------------------------")
    a("# ★ 裁决 13：Neo4j 侧标签与关系类型统一加命名空间前缀 `CC_`。")
    a("#   原因：目标实例 `neo4j-gaussdb`（community 单库）已有 v2 本体图 423 节点，")
    a("#   其中 `Host`(4) / `Table`(2) / `Session`(2) 标签与 `ACCESSES` / `DEPLOYED_ON`")
    a("#   关系类型与本项目撞名；ONTO 侧可新建专属仓库 `cc-ops`，故必须让 KG 侧也隔离。")
    a("# ============================================================================")
    a("")
    a("meta:")
    a("  version: \"1.2.0\"")
    a("  date: \"2026-10-05\"")
    a("  revision: \"1.2.0 = 新增派生证据型 Evidence + 关系 HAS_EVIDENCE（M1.5c："
      "每条 CCF 观测一个证据节点，两侧对称投影）；observation_projection 增 evidence 块，"
      "派生规则（记录 id / 指标码 / 状态 / 原始值 / 原始单位 / reason / probe / 采集时刻）"
      "全部以数据形式写在表里\"")
    a("  contract_of: \"01-场景建模与范围定义.md §1.1\"")
    a("  generated_from:")
    a("    - shared/model/topology.yaml")
    a("    - shared/model/metrics.yaml")
    a("    - shared/model/ccf.schema.json")
    a("  counts:")
    a("    types: %d" % len(types))
    a("    concrete_types: %d" % len([t for t in types.values() if not t["abstract"]]))
    a("    annotation_types: %d" % len([t for t in types.values() if t["annotation"]]))
    a("    ccf_object_types: %d"
      % len([t for t in types.values() if not t["abstract"] and not t["annotation"]]))
    a("    metrics: %d" % len(metric_map))
    a("  rules:")
    a("    - \"中性名出现在 shared/ 与评测台；KG 名只出现在 scheme1-kg/；ONTO 名只出现在 scheme2-onto/\"")
    a("    - \"映射表是唯一允许出现三套名字并置的地方\"")
    a("    - \"投影器遇到映射表里没有的中性名必须 fail-closed（禁止猜测或兜底改名）\"")
    a("    - \"属性名两侧同形（小驼峰），差别只在命名空间：KG 靠标签隔离，ONTO 靠 IRI 前缀隔离\"")
    a("    - \"Cypher 与 SPARQL 里出现的目标标签/关系类型/IRI 必须逐字来自本表\"")
    a("")
    a("namespaces:")
    a("  kg:")
    a("    label_prefix: \"%s\"" % LABEL_PREFIX)
    a("    reltype_prefix: \"%s\"" % RELTYPE_PREFIX)
    a("    constraint_prefix: \"%s\"" % CONSTRAINT_PREFIX)
    a("    property_style: lowerCamel")
    a("    instance_id_property: \"ccfId\"")
    a("    snapshot_property: \"ccfSnapshotId\"")
    a("    layer_property: \"ccfLayer\"")
    a("    neutral_type_property: \"ccfType\"")
    a("    note: \"与既有 v2 图的 gdo_uq_* 索引区分，本项目索引名一律 cc_uq_*\"")
    a("  onto:")
    a("    base: \"%s\"" % ONTO_BASE)
    for prefix in ("cc", "cco", "ccm", "ccf", "ccc", "cca", "ccp", "ccs"):
        a("    %s: \"%s\"" % (prefix, NS[prefix]))
    a("    instance_iri_template: \"%sinst/{id}\"" % ONTO_BASE)
    a("    id_separator: \"/\"")
    a("    id_separator_from: \":\"")
    a("    graph_iri_template: \"%sgraph/{snapshotId}\"" % ONTO_BASE)
    a("    neutral_type_property: \"cc:ccfType\"")
    a("    snapshot_property: \"cc:ccfSnapshotId\"")
    a("    layer_property: \"cc:ccfLayer\"")
    a("")
    a("# 前缀表：投影器写 Turtle prologue / 展开 CURIE 时**只读本块**，不得硬编码前缀名")
    a("prefixes:")
    for _p in sorted(list(NS) + ["xsd"]):
        a("  %s: \"%s\"" % (_p, NS.get(_p, XSD_NS)))
    a("")
    a("# 投影器共用的保留属性名（P1 / P2 / parity 三处必须逐字一致，故放在表里）")
    a("common_properties:")
    a("  kg:")
    a("    instance_id: \"ccfId\"")
    a("    snapshot: \"ccfSnapshotId\"")
    a("    layer: \"ccfLayer\"")
    a("    neutral_type: \"ccfType\"")
    a("    relation_id: \"ccfRelId\"")
    a("  onto:")
    a("    instance_id: \"cc:ccfId\"")
    a("    snapshot: \"cc:ccfSnapshotId\"")
    a("    layer: \"cc:ccfLayer\"")
    a("    neutral_type: \"cc:ccfType\"")
    a("")
    a("# 观测（CCF observation）如何落成载体原生属性（`01-` §3.3）")
    a("observation_projection:")
    a("  policy: \"value 只在 status=collected 时落成属性；status != collected 的条目落进两个多值属性\"")
    a("  kg:")
    a("    value: \"<metric.kg_property>\"")
    a("    not_collected: \"ccfNotCollected\"")
    a("    collected_empty: \"ccfCollectedEmpty\"")
    a("  onto:")
    a("    value: \"<metric.onto_property>\"")
    a("    not_collected: \"ccm:notCollected\"")
    a("    collected_empty: \"ccm:collectedEmpty\"")
    a("  rationale:")
    a("    - \"三态语义不同（collected / notCollected / collectedEmpty），只有 notCollected 能否定事实\"")
    a("    - \"Neo4j 无法存 null 属性，故否定性证据必须落成显式清单，否则 C4 否定性证据族失去依据\"")
    a("    - \"单条观测的 recordId 溯源由 M1.5c 的 Evidence 节点承担，不在本表\"")
    a("")
    a("# ★ M1.5c：observations[] → 派生证据节点（每条观测一个节点 + 一条 对象→证据 边）")
    a("#   `attrs` 的值是**观测里的字段路径**（不是载体属性名）：")
    a("#     · `a|b` = 取第一个存在的字段（collected 时 value / valueText 二选一）")
    a("#     · 字段不存在 ⇒ **不写该属性**（两侧一致：ONTO 侧不写空三元组，")
    a("#       Neo4j 侧不写 null —— Neo4j 存不了 null，写空串会污染 C4 判定）")
    a("#   投影器必须据此建属性；**不得**在代码里写 field 名。")
    a("  evidence:")
    a("    from: \"observations\"")
    a("    type: %s" % EVIDENCE_TYPE)
    a("    relation: %s" % EVIDENCE_REL)
    a("    id_field: \"id\"")
    a("    object_field: \"objectId\"")
    a("    attrs:")
    for _k, _v in EVIDENCE_ATTRS:
        a("      %s: \"%s\"" % (_k, _v))
    a("    note: \"粒度 = 每条 CCF 观测一个证据节点（用户裁定 U-Q = A）；两侧对称投影\"")
    a("")
    a("types:")
    for code in sorted(types):
        e = types[code]
        a("  %s:" % code)
        a("    layer: %s" % ("null" if e["layer"] is None else e["layer"]))
        a("    abstract: %s" % ("true" if e["abstract"] else "false"))
        if e["annotation"]:
            a("    annotation: true")
        a("    kg_label: \"%s\"" % e["kg_label"])
        a("    onto_class: \"%s\"" % e["onto_class"])
        if e.get("onto_superclass"):
            a("    onto_superclass: \"%s\"" % e["onto_superclass"])
        if e["key_attrs"]:
            a("    key_attrs: [%s]" % ", ".join(e["key_attrs"]))
        else:
            a("    key_attrs: []")
        if e["attrs"]:
            a("    attrs:")
            for name in sorted(e["attrs"]):
                at = e["attrs"][name]
                a("      %s: {kg_property: %s, onto_property: \"%s\", xsd: %s}"
                  % (name, at["kg_property"], at["onto_property"], at["xsd"]))
        else:
            a("    attrs: {}")
    a("")
    a("# 指标码 → 载体原生属性（58 条；规则只引指标码，投影器负责落成属性）")
    a("metrics:")
    for code in sorted(metric_map):
        e = metric_map[code]
        a("  %s:" % code)
        a("    object: %s" % e["object"])
        a("    kg_property: \"%s\"" % e["kg_property"])
        a("    onto_property: \"%s\"" % e["onto_property"])
        a("    xsd: %s" % e["xsd"])
    a("")
    text = "\n".join(L)

    R = []
    b = R.append
    b("# ============================================================================")
    b("# rel-map.yaml · 关系的中性名 → KG 名 / ONTO 名**显式映射表**（是数据不是代码）")
    b("# ----------------------------------------------------------------------------")
    b("# 权威出处：docs\\01-场景建模与范围定义.md §1.1、docs\\00 §6.3、裁决 13")
    b("# 输入契约：shared/model/topology.yaml")
    b("# 消费者：  投影器 P1（Neo4j）/ P2（GraphDB）——**只读本表**")
    b("# ----------------------------------------------------------------------------")
    b("# ★ OWL 对象属性名**不是** relationCode 的机械小驼峰变换：")
    b("#   `USES_DATASOURCE` 的 OWL 名是 `cc:usesDataSource`（不是 usesDatasource），")
    b("#   所以这 %d 个名字必须**显式冻结**在表里，不得由代码推导。" % len(ONTO_OBJ_PROP))
    b("# ★ M1.5c 追加：`HAS_EVIDENCE`（派生注释关系）——不由 CCF `relations[]` 断言，")
    b("#   由投影器从 `observations[]` 派生（`derived_from: observations`）后写入两库。")
    b("# ============================================================================")
    b("")
    b("meta:")
    b("  version: \"1.1.0\"")
    b("  date: \"2026-10-05\"")
    b("  generated_from:")
    b("    - shared/model/topology.yaml")
    b("  counts:")
    b("    relations: %d" % len(relations))
    b("    inverse_relations: %d" % len(inverses))
    b("    asserted_kg: %d" % len([c for c in relations if relations[c]["asserted"]]))
    b("    derived: %d" % len([c for c in relations if not relations[c]["asserted"]]))
    b("  rules:")
    b("    - \"投影器只投影 relations[]（asserted: true）；inverse_relations 一律不写入 CCF 的 relations[]\"")
    b("    - \"KG 侧 4 条反向关系同样不建边（运行时靠 Cypher 反向匹配），保证两侧实例数可直接对账\"")
    b("    - \"ONTO 侧反向属性由 owl:inverseOf 声明，推理器免费获得，不显式断言\"")
    b("")
    b("relations:")
    for code in sorted(relations):
        e = relations[code]
        b("  %s:" % code)
        b("    from: \"%s\"" % e["from"])
        b("    to: \"%s\"" % e["to"])
        b("    cardinality: \"%s\"" % e["cardinality"])
        b("    cross_layer: %s" % ("true" if e["cross_layer"] else "false"))
        b("    kg_reltype: \"%s\"" % e["kg_reltype"])
        b("    onto_property: \"%s\"" % e["onto_property"])
        b("    asserted: %s" % ("true" if e["asserted"] else "false"))
        if e.get("derived_from"):
            b("    derived_from: %s" % e["derived_from"])
        if e.get("inverse_code"):
            b("    inverse_code: %s" % e["inverse_code"])
            b("    onto_inverse_property: \"%s\"" % e["onto_inverse_property"])
    b("")
    b("inverse_relations:")
    for code in sorted(inverses):
        e = inverses[code]
        b("  %s:" % code)
        b("    from: \"%s\"" % e["from"])
        b("    to: \"%s\"" % e["to"])
        b("    inverse_code: %s" % e["inverse_code"])
        b("    kg_reltype: \"%s\"" % e["kg_reltype"])
        b("    onto_property: \"%s\"" % e["onto_property"])
        b("    asserted: false")
        b("    note: \"OWL 侧由 owl:inverseOf 声明；两侧均不显式建边\"")
    b("")
    text2 = "\n".join(R)

    import os
    os.makedirs("shared/mapping", exist_ok=True)
    for path, body in ((OUT_TYPE, text), (OUT_REL, text2)):
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print("写出 %s  %d 行  %d B" % (path, body.count("\n") + 1,
                                       len(body.encode("utf-8"))))
    print("types=%d concrete=%d metrics=%d relations=%d inverse=%d"
          % (len(types), len([t for t in types.values() if not t["abstract"]]),
             len(metric_map), len(relations), len(inverses)))


if __name__ == "__main__":
    main()
