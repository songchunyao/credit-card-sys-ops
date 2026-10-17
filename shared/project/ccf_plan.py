# -*- coding: utf-8 -*-
"""M1.5 · CCF → 载体原生名的**中性解析层**（归属 shared/）。

只依赖 `shared/model/*`（8 份契约）与 `shared/mapping/*`（2 张映射表）这两组**数据**；
**不得** import `scheme1-kg/` 或 `scheme2-onto/`（约束 C2 无反向依赖）。

职责三段：
  1. 加载并校验 CCF v1.0.1 快照（objects / relations / observations）
  2. 加载两张映射表，提供 **fail-closed** 的名字解析（未映射即抛 UnmappedName）
  3. 把 CCF 归一化成与载体无关的 `Plan`（Node / Edge / Obs），供两个投影器消费

设计约束（`docs\\00` §6.3、`docs\\01` §1.1）：
  * 载体名**只从映射表来**，本模块与两个投影器都不得硬编码 `CC_` / `cc:` 字面量
  * 未列出的中性名一律 fail-closed；投影器不得猜测、不得兜底改名
"""

import io
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class UnmappedName(Exception):
    """中性名在映射表里不存在 —— 一律 fail-closed。"""


class CCFError(Exception):
    """CCF 快照本身不合法（缺键、悬空引用、类型不一致）。"""


PRIMITIVE_TYPES = (bool, int, float, str)


def dig(data, path):
    """按**字段路径**取值（M1.5c 派生证据用）。

    路径语法（来自 `type-map.yaml#observation_projection.evidence.attrs` 的值）：
      * `a.b`  → 嵌套取 `data["a"]["b"]`
      * `a|b`  → 取第一个**存在**的字段（如 collected 时 `value` / `valueText` 二选一）
    返回 `(found, value)`；字段不存在 ⇒ `(False, None)`（调用方据此**不写该属性**，
    两侧一致：ONTO 侧不写空三元组，Neo4j 侧不写 null —— Neo4j 存不了 null）。
    """
    for alt in str(path).split("|"):
        cur = data
        ok = True
        for part in alt.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return True, cur
    return False, None


def _as_text(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else ("%s" % (value,))


def _read_json(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_yaml(path):
    if yaml is None:
        raise RuntimeError("需要 PyYAML 才能读映射表：%s" % path)
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# 映射表
# ---------------------------------------------------------------------------
class Maps(object):
    """两张映射表的只读门面。任何查不到的名字都抛 UnmappedName。"""

    def __init__(self, type_map, rel_map, schema=None, source_paths=None):
        self.type_map = type_map
        self.rel_map = rel_map
        self.schema = schema or {}
        self.source_paths = source_paths or {}
        self._types = type_map["types"]
        self._metrics = type_map["metrics"]
        self._relations = rel_map["relations"]
        self._inverses = rel_map["inverse_relations"]
        self.ns = type_map["namespaces"]
        self.obs_policy = type_map["observation_projection"]
        # v1.1.0 起：前缀表与保留属性名也是**表里的数据**，投影器不得硬编码
        self.prefixes = type_map.get("prefixes") or {}
        self.common = type_map.get("common_properties") or {}
        if not self.prefixes or not self.common:
            raise CCFError("type-map.yaml 缺 prefixes / common_properties 块"
                           "（需要 v1.1.0 及以上，请重跑 shared/mapping/gen_maps.py）")
        # ★ M1.5c：派生证据投影（observations[] → 证据节点 + 对象→证据 边）。
        #   粒度 = 每条 CCF 观测一个证据节点（用户裁定 U-Q = A），两侧对称投影。
        #   属性名与**源字段路径**全是表里的数据，本模块与投影器都不许硬编码。
        self.evidence = self.obs_policy.get("evidence")
        if not self.evidence:
            raise CCFError("type-map.yaml 缺 observation_projection.evidence 块"
                           "（需要 v1.2.0 及以上，请重跑 shared/mapping/gen_maps.py）")
        # `ccf.schema.json#x-categorical-metrics`：分类型指标必须用 valueText 而非 value
        self.categorical = set(self.schema.get("x-categorical-metrics") or [])

        # 反向索引：object 类型 → 该类型可承载的指标码集合
        self.metrics_by_object = {}
        for code, spec in self._metrics.items():
            self.metrics_by_object.setdefault(spec["object"], set()).add(code)
        # 反向索引：类型 → 涉及的关系码
        self.relations_by_type = {}
        for code, spec in self._relations.items():
            self.relations_by_type.setdefault(spec["from"], set()).add(code)
            self.relations_by_type.setdefault(spec["to"], set()).add(code)

    # ---- 类型 ----
    def type_spec(self, type_code):
        spec = self._types.get(type_code)
        if spec is None:
            raise UnmappedName("typeCode 未在 type-map.yaml 里映射：%s" % type_code)
        return spec

    def kg_label(self, type_code):
        return self.type_spec(type_code)["kg_label"]

    def onto_class(self, type_code):
        return self.type_spec(type_code)["onto_class"]

    def attrs(self, type_code):
        return self.type_spec(type_code).get("attrs") or {}

    def key_attrs(self, type_code):
        return list(self.type_spec(type_code).get("key_attrs") or [])

    def concrete_types(self):
        return sorted(t for t, s in self._types.items() if not s.get("abstract"))

    # ---- 派生证据（M1.5c） ----
    def evidence_spec(self):
        """派生证据的投影说明（type / relation / id_field / object_field / attrs）。"""
        return self.evidence

    def evidence_type(self):
        return self.evidence["type"]

    def evidence_relation(self):
        return self.evidence["relation"]

    def evidence_attrs(self):
        """{属性名: 源字段路径}——路径支持 `a|b`（取第一个存在的字段）。"""
        return dict(self.evidence.get("attrs") or {})

    # ---- 指标 ----
    def metric(self, metric_code):
        spec = self._metrics.get(metric_code)
        if spec is None:
            raise UnmappedName("metricCode 未在 type-map.yaml 里映射：%s" % metric_code)
        return spec

    def metric_object(self, metric_code):
        """该指标码挂在哪个对象类型上（权威：metrics.yaml 的 object）。"""
        return self.metric(metric_code)["object"]

    def expand(self, curie):
        """`ccm:state` → 完整 IRI。前缀名只从 `prefixes` 块取。"""
        if curie.startswith("<") or "://" in curie:
            return curie
        prefix, _, local = curie.partition(":")
        base = self.prefixes.get(prefix)
        if base is None:
            raise UnmappedName("CURIE 前缀 %s: 不在 type-map.yaml 的 prefixes 块里（%s）"
                               % (prefix, curie))
        return base + local

    # ---- ★ 指标计数的**唯一权威口径**（P1 / P2 / parity 三处共用，禁止各写一套）----
    #
    # 为什么必须按 (对象类型, 属性) 双侧限定（v1.1.0 修复）：
    #   1. Neo4j 侧是与既有 v2 图**共库共存**的。全局口径 `MATCH (n) WHERE n.<prop>
    #      IS NOT NULL` 会把 v2 图里恰好同名的属性一起数进来 —— 实测 v2 的
    #      `Session`/`RuntimeEntity` 节点带 `waitEvent`，使 fixture 上
    #      `db.session.wait_event` 数出 3 而非 1。
    #   2. 同名属性跨类型复用是**合法**的（LPG 常态；OWL 侧共享 `ccm:` 属性同样是
    #      常态）——本表实测 70 个指标码只有 68 个不同属性名，`cpuUsageRatio` 由
    #      `container.` / `db.instance.` / `host.cpu_usage_ratio` 三个码共用。
    #      不加类型限定，这三条计数会互相串成同一个数。
    #   同对象撞名才是真缺陷，已由 `shared/verify/mapping_gate.py` 机械拦截。
    def kg_metric_count_query(self, metric_code):
        spec = self.metric(metric_code)
        return ("MATCH (n:`%s`) WHERE n.`%s` IS NOT NULL RETURN count(n) AS c"
                % (self.kg_label(spec["object"]), spec["kg_property"]))

    def onto_metric_count_clause(self, metric_code):
        """返回 SPARQL 的 WHERE 片段（含类限定），供 `SELECT (COUNT(*) AS ?c)`。"""
        spec = self.metric(metric_code)
        return "?s a <%s> ; <%s> ?v ." % (self.expand(self.onto_class(spec["object"])),
                                          self.expand(spec["onto_property"]))

    def kg_scope_query(self, prop):
        """按保留属性 `ccfId` 把回读限定在本项目的子图上（不依赖标签前缀字面量）。"""
        iid = self.common["kg"]["instance_id"]
        return ("MATCH (n) WHERE n.`%s` IS NOT NULL AND n.`%s` IS NOT NULL "
                "UNWIND n.`%s` AS m RETURN m AS metricCode, count(*) AS c"
                % (iid, prop, prop))

    def onto_scope_clause(self, prop):
        iid = self.expand(self.common["onto"]["instance_id"])
        return "?s <%s> ?x ; <%s> ?m ." % (iid, self.expand(prop))

    # ---- 关系 ----
    def relation(self, rel_code):
        spec = self._relations.get(rel_code)
        if spec is None:
            raise UnmappedName("relationCode 未在 rel-map.yaml 里映射：%s" % rel_code)
        return spec

    def counter_types(self):
        """所有出现过的载体名，供 parity 反查（中性名 → 载体名）。"""
        out = []
        for t in self.concrete_types():
            out.append((t, self.kg_label(t), self.onto_class(t)))
        return out


def load_maps(mapping_dir, model_dir=None):
    model_dir = model_dir or "shared/model"
    tp = os.path.join(mapping_dir, "type-map.yaml")
    rp = os.path.join(mapping_dir, "rel-map.yaml")
    sp = os.path.join(model_dir, "ccf.schema.json")
    return Maps(_read_yaml(tp), _read_yaml(rp), _read_json(sp),
                {"type_map": os.path.abspath(tp),
                 "rel_map": os.path.abspath(rp),
                 "schema": os.path.abspath(sp)})


# ---------------------------------------------------------------------------
# CCF 快照
# ---------------------------------------------------------------------------
REQUIRED_TOP = ("ccfVersion", "snapshotId", "capturedAt", "collector",
                "payloadPolicy", "objects", "relations", "observations",
                "expected", "integrity")

STATUS_ENUM = ("collected", "notCollected", "collectedEmpty")


class Node(object):
    __slots__ = ("id", "type", "layer", "key", "attrs", "values",
                 "not_collected", "collected_empty", "provenance")

    def __init__(self, raw):
        self.id = raw["id"]
        self.type = raw["type"]
        self.layer = raw.get("layer")
        self.key = dict(raw.get("key") or {})
        self.attrs = dict(raw.get("attrs") or {})
        self.provenance = raw.get("provenance") or {}
        self.values = []          # [(metricCode, value, unit, obsId)]
        self.not_collected = []   # [metricCode]
        self.collected_empty = []  # [metricCode]


class Edge(object):
    __slots__ = ("id", "type", "from_id", "to_id", "provenance")

    def __init__(self, raw):
        self.id = raw["id"]
        self.type = raw["type"]
        self.from_id = raw["from"]
        self.to_id = raw["to"]
        self.provenance = raw.get("provenance") or {}


class CCF(object):
    def __init__(self, raw, path=None):
        self.raw = raw
        self.path = path
        self.snapshot_id = raw.get("snapshotId")
        self.ccf_version = raw.get("ccfVersion")
        self.captured_at = raw.get("capturedAt")
        self.payload_policy = raw.get("payloadPolicy") or {}
        self.objects = [Node(o) for o in raw.get("objects") or []]
        self.relations = [Edge(r) for r in raw.get("relations") or []]
        self.observations = list(raw.get("observations") or [])
        self.expected = raw.get("expected") or {}
        self.integrity = raw.get("integrity") or {}


def load_ccf(path):
    raw = _read_json(path)
    missing = [k for k in REQUIRED_TOP if k not in raw]
    if missing:
        raise CCFError("CCF 缺顶层必需键：%s" % ", ".join(missing))
    return CCF(raw, os.path.abspath(path))


# ---------------------------------------------------------------------------
# Plan：与载体无关的归一化结果
# ---------------------------------------------------------------------------
class Plan(object):
    def __init__(self, ccf, maps):
        self.ccf = ccf
        self.maps = maps
        self.nodes = ccf.objects
        self.edges = ccf.relations
        self.observations = ccf.observations
        # ★ M1.5c：派生证据（每条 CCF 观测一个证据节点 + 一条 对象→证据 边）。
        #   元素 = {"recordId", "objectId", "props": {属性名: 值}}
        self.evidence = []
        self.evidence_edges = []
        self.issues = []      # [(severity, code, message)]

    # ---- 校验 ----
    def _issue(self, sev, code, msg):
        self.issues.append((sev, code, msg))

    def build(self):
        m = self.maps
        node_by_id = {}
        for n in self.nodes:
            if n.id in node_by_id:
                self._issue("ERROR", "duplicate_object_id", "对象 id 重复：%s" % n.id)
            node_by_id[n.id] = n
            try:
                spec = m.type_spec(n.type)
            except UnmappedName as exc:
                self._issue("ERROR", "type_unmapped", str(exc))
                continue
            if spec.get("abstract"):
                self._issue("ERROR", "abstract_type_instance",
                            "实例化了抽象类型：%s (%s)" % (n.type, n.id))
            for k in n.key:
                if k not in spec.get("attrs", {}):
                    self._issue("WARN", "key_attr_unmapped",
                                "%s 的 key 属性 %s 未在 type-map 的 attrs 里" % (n.type, k))
            for k in n.attrs:
                if k not in spec.get("attrs", {}):
                    self._issue("WARN", "attr_unmapped",
                                "%s 的属性 %s 未在 type-map 的 attrs 里" % (n.type, k))

        for e in self.edges:
            try:
                spec = m.relation(e.type)
            except UnmappedName as exc:
                self._issue("ERROR", "relation_unmapped", str(exc))
                continue
            for role, oid in (("from", e.from_id), ("to", e.to_id)):
                if oid not in node_by_id:
                    self._issue("ERROR", "dangling_relation_end",
                                "关系 %s 的 %s=%s 不是本快照里的对象"
                                % (e.id, role, oid))
                    continue
                other = node_by_id[oid]
                want = spec[role]
                if other.type != want:
                    self._issue("ERROR", "relation_end_type_mismatch",
                                "关系 %s(%s) 的 %s 期望 %s，实得 %s"
                                % (e.id, e.type, role, want, other.type))
            self._issue("INFO", "relation_ok", "关系 %s %s→%s" % (e.type, e.from_id, e.to_id))

        for obs in self.observations:
            oid = obs.get("objectId")
            code = obs.get("metricCode")
            status = obs.get("status")
            node = node_by_id.get(oid)
            if node is None:
                self._issue("ERROR", "dangling_observation_ref",
                            "观测 %s 指向不存在的对象 %s" % (obs.get("id"), oid))
                continue
            if status not in STATUS_ENUM:
                self._issue("ERROR", "bad_observation_status",
                            "观测 %s 的 status=%r 非法" % (obs.get("id"), status))
                continue
            try:
                mspec = m.metric(code)
            except UnmappedName as exc:
                self._issue("ERROR", "metric_unmapped", str(exc))
                continue
            if mspec["object"] != node.type:
                self._issue("ERROR", "metric_object_mismatch",
                            "观测 %s 的 %s 归属 %s，但对象 %s 是 %s"
                            % (obs.get("id"), code, mspec["object"], oid, node.type))
            if status == "collected":
                # ccf.schema.json 的 $defs.observation.allOf —— status=collected ⇒ anyOf[value, valueText]
                # （刻意不写行号：schema 是可增长契约，行号会随新增字段漂移）
                has_v = "value" in obs
                has_t = "valueText" in obs
                if not has_v and not has_t:
                    self._issue("ERROR", "collected_without_value",
                                "观测 %s status=collected 但既无 value 也无 valueText" % obs.get("id"))
                    continue
                if has_v and has_t:
                    self._issue("WARN", "collected_with_both",
                                "观测 %s 同时带 value 与 valueText，取 value（数值优先）" % obs.get("id"))
                raw = obs["value"] if has_v else obs["valueText"]
                if not isinstance(raw, PRIMITIVE_TYPES):
                    self._issue("ERROR", "non_primitive_value",
                                "观测 %s 的取值不是标量：%r" % (obs.get("id"), raw))
                    continue
                is_cat = code in self.maps.categorical
                if is_cat and not has_t:
                    self._issue("ERROR", "categorical_needs_valueText",
                                "分类型指标 %s 的观测 %s 未用 valueText（ccf.schema.json"
                                "#x-categorical-metrics）" % (code, obs.get("id")))
                if not is_cat and has_t:
                    self._issue("ERROR", "non_categorical_uses_valueText",
                                "非分类型指标 %s 的观测 %s 使用了 valueText（应为 value）"
                                % (code, obs.get("id")))
                if isinstance(raw, bool) is False and isinstance(raw, str) and not is_cat:
                    self._issue("ERROR", "string_value_on_numeric_metric",
                                "非分类型指标 %s 的观测 %s 落成字符串 %r"
                                % (code, obs.get("id"), raw))
                node.values.append((code, raw, obs.get("unit"), obs.get("id")))
            else:
                # status != collected ⇒ required reason
                if not obs.get("reason"):
                    self._issue("ERROR", "non_collected_without_reason",
                                "观测 %s status=%s 但缺 reason（如 probe_unavailable / "
                                "object_absent / metric_not_applicable / empty_result_set）"
                                % (obs.get("id"), status))
                if "value" in obs or "valueText" in obs:
                    self._issue("ERROR", "non_collected_with_value",
                                "观测 %s status=%s 却带了取值（schema：status != collected "
                                "时不得出现）" % (obs.get("id"), status))
                if status == "notCollected":
                    node.not_collected.append(code)
                else:
                    node.collected_empty.append(code)

            # ★ M1.5c：无论三态如何，**每条观测都派生一个证据节点**。
            #   否定性证据（notCollected）也必须留痕：`reason` 与 `status` 就是它的内容，
            #   这正是 C4 否定性证据族与 `false_claim` 判定所需要的那一级粒度。
            self._add_evidence(obs, node)

        return self

    def _add_evidence(self, obs, node):
        spec = self.maps.evidence_spec()
        rec_id = obs.get(spec["id_field"])
        if not rec_id:
            self._issue("ERROR", "evidence_without_record_id",
                        "观测缺 %s，无法派生证据节点（观测=%r）"
                        % (spec["id_field"], obs.get("id")))
            return
        declared = self.maps.attrs(self.maps.evidence_type()) or {}
        props = {}
        for name, path in self.maps.evidence_attrs().items():
            found, val = dig(obs, path)
            if not found or val is None:
                continue          # 字段不存在 ⇒ 不写该属性（两侧一致）
            # 值按**类型表里声明的 xsd**收敛：声明的 xsd:string 一律字符串化，
            # 使两侧落盘的字面量完全同形（如数值 0.85 → "0.85"）。
            axsd = (declared.get(name) or {}).get("xsd")
            if axsd == "xsd:string":
                val = _as_text(val)
            props[name] = val
        self.evidence.append({"recordId": rec_id, "objectId": node.id, "props": props})
        self.evidence_edges.append({"recordId": rec_id, "objectId": node.id})

    def errors(self):
        return [i for i in self.issues if i[0] == "ERROR"]

    def report(self):
        by_type = {}
        for n in self.nodes:
            by_type[n.type] = by_type.get(n.type, 0) + 1
        by_rel = {}
        for e in self.edges:
            by_rel[e.type] = by_rel.get(e.type, 0) + 1
        # ★ M1.5c：派生证据也是「中性类型 + 中性关系」，必须进同一口径
        #   （§6.3 ① 每个中性类型 count_kg(T)==count_onto(T) 才算过）。
        ev_type = self.maps.evidence_type()
        ev_rel = self.maps.evidence_relation()
        by_type[ev_type] = by_type.get(ev_type, 0) + len(self.evidence)
        by_rel[ev_rel] = by_rel.get(ev_rel, 0) + len(self.evidence_edges)
        by_status = {}
        for obs in self.observations:
            s = obs.get("status")
            by_status[s] = by_status.get(s, 0) + 1
        sev = {}
        for s, _c, _m in self.issues:
            sev[s] = sev.get(s, 0) + 1
        return {
            "snapshotId": self.ccf.snapshot_id,
            "ccfVersion": self.ccf.ccf_version,
            "ccfPath": self.ccf.path,
            "mapping": self.maps.source_paths,
            "counts": {
                "objects": len(self.nodes),
                "relations": len(self.edges),
                "observations": len(self.observations),
                "byType": by_type,
                "byRelation": by_rel,
                "byObservationStatus": by_status,
                "evidence": {
                    "type": ev_type,
                    "relation": ev_rel,
                    "nodes": len(self.evidence),
                    "edges": len(self.evidence_edges),
                },
            },
            "issues": [{"severity": s, "code": c, "message": m}
                       for s, c, m in self.issues if s != "INFO"],
            "issueCounts": sev,
        }

    def print_issues(self, stream=None):
        stream = stream or sys.stderr
        for s, c, msg in self.issues:
            if s == "INFO":
                continue
            stream.write("[%s] [%s] %s\n" % (s, c, msg))


def build_plan(ccf_path, mapping_dir, model_dir=None):
    ccf = load_ccf(ccf_path)
    maps = load_maps(mapping_dir, model_dir)
    return Plan(ccf, maps).build()
