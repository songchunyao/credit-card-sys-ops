#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shared/verify/t10_native_parity.py —— T10「两侧原生实现覆盖的一致性」自检。

规格来源（唯一权威，本文件不另立口径）
    * `docs/07-实施计划与验收.md` M2.8 规格块（7 条）：中性条目集、逐条判定、defined 口径、
      排除清单、规则维度取证形态、落点与退出码。
    * `docs/00-总体设计与裁决记录.md` §6.3 的 **④**：①②③ 管「实例与回答」（ABox 已由同一份
      CCF 强制统一、不计成本）；**④ = T10 管「原生实现覆盖」**（两侧 TBox 各自从零建、计入成本）。
      缺 ④，两侧准确率差异无法归因于载体。

判据
    对契约里的每条中性条目 N（类型 / 关系 / 逆关系 / 故障模式 / 阈值 / 因果边 / 指标 / 结论码），
    机械判定 `defined_kg(N)` 与 `defined_onto(N)`，逐条给
    `defined_both | kg_only | onto_only | neither`；**任一条非 defined_both 即 FAIL**。

    * KG 侧 = 只读真机 Neo4j（`SHOW CONSTRAINTS` / 节点标签 / 关系类型 / 节点字符串属性值 /
      边上 `contractId`）**∪** 载体 `.cypher` 里的声明模板（节点模板 / 关系模板 / contractId /
      中性码字面量）——只读文件，不写图。
    * ONTO 侧 = 只读 GraphDB 仓库里 TBox 命名空间的声明（`owl:Class` / `owl:ObjectProperty` /
      具名个体；中性码字面量作为补充证据）。**不要求有实例**。
    * 结论码（F-001…F-007）维度按规格第 5 条取证：只读两侧载体上**可机读的产出声明**注释里的
      结论码集合，**不比较两侧规则 id 命名**。

退出码（三态；规格只写 0/1，本实现按任务要求强化为三态）
    0 = PASS     全部 defined_both
    1 = FAIL     真有不一致（任一中性条目非 defined_both）
    2 = BLOCKED  某一侧 TBox 尚未装载 / 无法取证（口令缺失、连接失败、载体目录未给、仓库为空）
    ★ 绝不允许把「某一侧未装载 / 无法取证」报成 FAIL —— 那会伪报「两侧不等价」。

纪律
    * 中性条目与计数**全部从 `shared/model`、`shared/mapping` 数据文件读出**，代码里不写死
      14/15/4/14/12/18/58/7。
    * 载体目录一律由 CLI 数据参数给出，本文件不出现任何方案目录名与载体命名空间字面量
      （标签前缀 / 关系类型前缀 / IRI 基址都取自映射表），以同时满足门禁 G15（L2/L6）与
      「映射是数据不是代码」。
    * 只读：真机只跑 `SHOW` / `MATCH` / `RETURN` / SPARQL `SELECT`·`ASK`，不写任何一侧。
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                       # pragma: no cover
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2

DEF = "defined"
UNDEF = "undefined"
UNK = "unknown"

OWL = "http://www.w3.org/2002/07/owl#"

# W3C 内建命名空间：GraphDB 等三元组库会自带 owl:Thing / owl:Nothing /
# owl:bottomObjectProperty 之类声明，它们不能当作「该侧真的声明了这个中性条目」。
BUILTIN_NS = (
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2001/XMLSchema#",
)

# 退出码含义（供 equivalence.py 与自证脚本共用）
EXIT_MEANING = {0: "PASS", 1: "FAIL", 2: "BLOCKED"}


# ------------------------------------------------------------------ 基础工具
def is_builtin_iri(iri):
    """True 表示该 IRI 属于 W3C 内建词汇（owl:/rdfs:/rdf:/xsd:）。"""
    return any((iri or "").startswith(ns) for ns in BUILTIN_NS)


def _as_int(text):
    try:
        return int(text)
    except Exception:                                   # noqa: BLE001
        return None


def read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def read_text(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def expand_curie(prefixes, curie):
    """把映射表里的 `cc:Table` 形态展开成完整 IRI；前缀不在表里则返回 None。"""
    if not curie or not isinstance(curie, str) or ":" not in curie:
        return None
    pfx, local = curie.split(":", 1)
    ns = (prefixes or {}).get(pfx)
    if not ns:
        return None
    return ns + local


class ConfigError(RuntimeError):
    """契约 / 映射表不可用 ⇒ 无法取证（BLOCKED），不是不一致（FAIL）。"""


# ------------------------------------------------------------------ 中性条目集
def build_items(model_dir, mapping_dir, findings_path):
    """从契约 + 映射表现算中性条目集（计数不写死）。"""
    tmap_path = os.path.join(mapping_dir, "type-map.yaml")
    rmap_path = os.path.join(mapping_dir, "rel-map.yaml")
    want = [os.path.join(model_dir, "topology.yaml"),
            os.path.join(model_dir, "fault-modes.yaml"),
            os.path.join(model_dir, "causal-edges.yaml"),
            os.path.join(model_dir, "metrics.yaml"),
            findings_path, tmap_path, rmap_path]
    for p in want:
        if not os.path.isfile(p):
            raise ConfigError("契约 / 映射文件不存在：%s" % p)

    topo = read_yaml(os.path.join(model_dir, "topology.yaml"))
    fmodes = read_yaml(os.path.join(model_dir, "fault-modes.yaml"))
    cedges = read_yaml(os.path.join(model_dir, "causal-edges.yaml"))
    mets = read_yaml(os.path.join(model_dir, "metrics.yaml"))
    fnds = read_yaml(findings_path)
    tmap = read_yaml(tmap_path)
    rmap = read_yaml(rmap_path)

    prefixes = dict(tmap.get("prefixes") or {})
    for need, obj, key in (("topology.types", topo, "types"),
                           ("topology.relations", topo, "relations"),
                           ("topology.inverse_relations", topo, "inverse_relations"),
                           ("fault-modes.fault_modes", fmodes, "fault_modes"),
                           ("fault-modes.thresholds", fmodes, "thresholds"),
                           ("causal-edges.edges", cedges, "edges"),
                           ("metrics.metrics", mets, "metrics"),
                           ("findings.findings", fnds, "findings"),
                           ("type-map.types", tmap, "types"),
                           ("rel-map.relations", rmap, "relations"),
                           ("rel-map.inverse_relations", rmap, "inverse_relations")):
        if key not in (obj or {}) or obj.get(key) in (None, {}, []):
            raise ConfigError("契约 / 映射结构不符：%s 缺少非空 `%s`" % (need, key))

    fault_ns = prefixes.get("ccf")
    causal_ns = prefixes.get("ccc")
    metric_ns = prefixes.get("ccm")
    tbox_ns = [v for k, v in prefixes.items()
               if isinstance(v, str) and v.startswith(tmap["namespaces"]["onto"]["base"])] \
        if tmap.get("namespaces", {}).get("onto", {}).get("base") else []

    def kg_label_of(neutral_type):
        """中性类型 → KG 标签；契约 / 映射表没给就返回 None（不强加载体约定）。"""
        ent = (tmap["types"] or {}).get(neutral_type) or {}
        return ent.get("kg_label")

    items = []

    # 契约里 types / relations / inverse_relations 是 **列表**（每项带 code），映射表是 **字典**
    # ★ 排除项必须由**契约标志**驱动（裁决：docs/00 §6.3 ④ + docs/07 M2 规格排除清单）：
    #   类型看 topology.yaml#types[].abstract == true；反向关系看 inverse_relations[].asserted == false。
    #   不按侧写白名单、不硬编码名字或计数 —— 契约翻转标志，该条目自动变回可判。
    flag_disagreements = []
    for e in topo["types"]:
        code = e["code"]
        ent = tmap["types"].get(code)
        if not ent:
            raise ConfigError("type-map.types 缺中性类型 %s" % code)
        abstract = bool(e.get("abstract"))
        if bool(ent.get("abstract")) != abstract:
            flag_disagreements.append("types/%s：契约 abstract=%s 与映射表 abstract=%s 不一致（以契约为准）"
                                      % (code, abstract, bool(ent.get("abstract"))))
        items.append(dict(group="types", code=code, kind="iri_label",
                          kg_iri=ent["kg_label"],
                          onto_iris=[expand_curie(prefixes, ent.get("onto_class"))],
                          literal=None,
                          excluded_by_contract=abstract,
                          exclusion_basis=("契约标志 topology.yaml#types[] `%s`.abstract == true"
                                           "（契约自声明为抽象类型）" % code if abstract else None),
                          meta=dict(abstract=abstract,
                                    mappingAbstract=bool(ent.get("abstract")),
                                    annotation=bool(ent.get("annotation")))))

    for e in topo["relations"]:
        code = e["code"]
        ent = (rmap["relations"] or {}).get(code)
        if not ent:
            raise ConfigError("rel-map.relations 缺中性关系 %s" % code)
        items.append(dict(group="relations", code=code, kind="iri_reltype",
                          kg_iri=ent["kg_reltype"],
                          onto_iris=[expand_curie(prefixes, ent.get("onto_property"))],
                          literal=None, meta=dict(asserted=bool(ent.get("asserted", True)))))

    for e in topo["inverse_relations"]:
        code = e["code"]
        ent = (rmap["inverse_relations"] or {}).get(code)
        if not ent:
            raise ConfigError("rel-map.inverse_relations 缺中性关系 %s" % code)
        asserted = bool(e.get("asserted", False))
        if bool(ent.get("asserted", False)) != asserted:
            flag_disagreements.append("inverse_relations/%s：契约 asserted=%s 与映射表 asserted=%s 不一致（以契约为准）"
                                      % (code, asserted, bool(ent.get("asserted", False))))
        items.append(dict(group="inverse_relations", code=code, kind="iri_reltype",
                          kg_iri=ent["kg_reltype"],
                          onto_iris=[expand_curie(prefixes, ent.get("onto_property"))],
                          literal=None,
                          excluded_by_contract=not asserted,
                          exclusion_basis=("契约标志 topology.yaml#inverse_relations[] `%s`.asserted == false"
                                           "（契约自声明不进图）" % code if not asserted else None),
                          meta=dict(asserted=asserted,
                                    mappingAsserted=bool(ent.get("asserted", False)))))

    fm_label = kg_label_of("FaultMode")
    for e in fmodes["fault_modes"]:
        items.append(dict(group="fault_modes", code=e["code"], kind="value_label",
                          kg_iri=fm_label, onto_iris=([fault_ns + e["code"]] if fault_ns else []),
                          literal=e["code"], meta=dict()))

    thr_label = kg_label_of("Threshold")
    for e in fmodes["thresholds"]:
        # 载体侧阈值个体 IRI 约定 `ccm:t_<name>`（与既有权威 shared/verify/onto_tbox_check.py 同源）
        onto = ([metric_ns + "t_" + e["name"]] if metric_ns else [])
        items.append(dict(group="thresholds", code=e["name"], kind="value_label",
                          kg_iri=thr_label, onto_iris=onto,
                          literal=e["name"], meta=dict(metric=e.get("metric"))))

    for e in cedges["edges"]:
        items.append(dict(group="causal_edges", code=e["id"], kind="edge_contract",
                          kg_iri=None,
                          onto_iris=([causal_ns + e["id"]] if causal_ns else []),
                          literal=e["id"], meta=dict(relation=e.get("relation"))))

    met_label = kg_label_of("Metric")
    for e in mets["metrics"]:
        items.append(dict(group="metrics", code=e["code"], kind="value_label",
                          kg_iri=met_label,
                          onto_iris=([metric_ns + "m_" + e["code"].replace(".", "_")]
                                     if metric_ns else []),
                          literal=e["code"], meta=dict()))

    for e in fnds["findings"]:
        items.append(dict(group="findings", code=e["code"], kind="rule_decl",
                          kg_iri=None, onto_iris=[], literal=e["code"], meta=dict()))

    for it in items:
        it.setdefault("excluded_by_contract", False)
        it.setdefault("exclusion_basis", None)

    groups = {}
    for it in items:
        groups[it["group"]] = groups.get(it["group"], 0) + 1
    excluded_items = ["%s/%s" % (it["group"], it["code"]) for it in items
                      if it["excluded_by_contract"]]
    info = dict(counts=groups, items=len(items), prefixes=prefixes, tbox_ns=tbox_ns,
                assertable_items=len(items) - len(excluded_items),
                excluded=dict(count=len(excluded_items), items=excluded_items,
                              basis=sorted(set(it["exclusion_basis"] for it in items
                                               if it["exclusion_basis"]))),
                flag_disagreements=flag_disagreements,
                contractFiles={"model_dir": model_dir, "mapping_dir": mapping_dir,
                               "findings": findings_path},
                unknown_onto_iris=[it["group"] + "/" + it["code"] for it in items
                                   if it["kind"] != "rule_decl" and not it["onto_iris"]])
    return items, info


# ------------------------------------------------------------------ KG 侧：真机（只读）
def kg_live_collect(uri, user, password, database, label_prefix, timeout=60):
    """只读真机 Neo4j：SHOW / MATCH+RETURN，不写。失败 ⇒ ok=False（BLOCKED，不报 FAIL）。"""
    out = dict(ok=False, reason=None, uri=uri, user=user, database=database,
               labels=[], constraint_labels=[], constraint_count=None,
               constraint_error=None, reltypes=[], label_string_values={},
               contract_ids=[], edge_string_values=[])
    if not label_prefix:
        out["reason"] = ("标签前缀未取到（映射表 namespaces.kg.label_prefix 缺失且未传 "
                         "--kg-label-prefix）⇒ 拒绝探测并报 BLOCKED")
        return out
    try:
        from neo4j import GraphDatabase
    except Exception as exc:                            # pragma: no cover
        out["reason"] = "neo4j 驱动不可用：%s" % exc
        return out
    try:
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password),
                                          notifications_min_severity="OFF")
        except (TypeError, ValueError):
            driver = GraphDatabase.driver(uri, auth=(user, password))
    except Exception as exc:                            # noqa: BLE001
        out["reason"] = "Neo4j 会话初始化失败：%s" % exc
        return out
    try:
        with driver.session(database=database) as sess:
            out["labels"] = [r["label"] for r in sess.run(
                "CALL db.labels() YIELD label RETURN label")]
            try:
                rows = [dict(r) for r in sess.run(
                    "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties RETURN *")]
                out["constraint_count"] = len(rows)
                cl = set()
                for r in rows:
                    for x in (r.get("labelsOrTypes") or []):
                        cl.add(x)
                out["constraint_labels"] = sorted(cl)
            except Exception as exc:                    # noqa: BLE001
                out["constraint_error"] = str(exc)
            out["reltypes"] = [r["relationshipType"] for r in sess.run(
                "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")]
            rows = sess.run(
                "MATCH (n) UNWIND labels(n) AS l WITH n, l WHERE l STARTS WITH $p "
                "UNWIND keys(n) AS k WITH l, n, k WHERE n[k] IS :: STRING "
                "RETURN DISTINCT l AS label, n[k] AS v", p=label_prefix)
            vals = {}
            for r in rows:
                vals.setdefault(r["label"], set()).add(r["v"])
            out["label_string_values"] = vals
            out["contract_ids"] = [r["cid"] for r in sess.run(
                "MATCH (a)-[r]->(b) WHERE r.contractId IS NOT NULL "
                "RETURN DISTINCT r.contractId AS cid")]
            out["edge_string_values"] = [r["v"] for r in sess.run(
                "MATCH ()-[r]->() UNWIND keys(r) AS k WITH r, k WHERE r[k] IS :: STRING "
                "RETURN DISTINCT r[k] AS v")]
        out["ok"] = True
    except Exception as exc:                            # noqa: BLE001
        out["reason"] = "Neo4j 只读查询失败：%s" % exc
    finally:
        try:
            driver.close()
        except Exception:                               # pragma: no cover
            pass
    return out


# ------------------------------------------------------------------ KG 侧：载体声明（只读文件）
def kg_carrier_scan(ontology_dir, label_prefix, reltype_prefix):
    """只读载体 `.cypher`：节点模板 / 关系模板 / contractId / 中性码字面量。"""
    out = dict(dir=ontology_dir, files=0, labels={}, reltypes={}, contract_ids={},
               text="", error=None)
    if not ontology_dir:
        out["error"] = "未提供 KG 载体目录（--kg-ontology-dir）"
        return out
    if not os.path.isdir(ontology_dir):
        out["error"] = "KG 载体目录不存在：%s" % ontology_dir
        return out
    paths = sorted(glob.glob(os.path.join(ontology_dir, "**", "*.cypher"), recursive=True))
    out["files"] = len(paths)
    if not paths:
        out["error"] = "KG 载体目录里没有 .cypher：%s" % ontology_dir
        return out
    chunks = []
    for p in paths:
        text = read_text(p)
        chunks.append(text)
        rel = os.path.basename(p)
        for m in re.finditer(r":\s*(" + re.escape(label_prefix) + r"[A-Za-z0-9_]+)",
                             text):
            tok = m.group(1)
            if tok.endswith("_") or tok != tok.rstrip("_"):
                continue
            line = text[:m.start()].count("\n") + 1
            out["labels"].setdefault(tok, "%s:%d" % (rel, line))
        for m in re.finditer(r":\s*(" + re.escape(reltype_prefix) + r"[A-Za-z0-9_]+)\s*\]",
                             text):
            tok = m.group(1)
            line = text[:m.start()].count("\n") + 1
            out["reltypes"].setdefault(tok, "%s:%d" % (rel, line))
        for m in re.finditer(r"contractId\s*:\s*['\"]([^'\"]+)['\"]", text):
            line = text[:m.start()].count("\n") + 1
            out["contract_ids"].setdefault(m.group(1), "%s:%d" % (rel, line))
    out["text"] = "\n".join(chunks)
    # 节点模板的判定还要求「模板」形态（`(:Label` 后跟 `{` 或 `)`），把纯注释提及排除在强证据外
    strong = {}
    for p in paths:
        text = read_text(p)
        rel = os.path.basename(p)
        for m in re.finditer(r"\(\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*(" + re.escape(label_prefix)
                             + r"[A-Za-z0-9_]+)\s*[\{\)]", text):
            line = text[:m.start()].count("\n") + 1
            strong.setdefault(m.group(1), "%s:%d" % (rel, line))
        for m in re.finditer(r"FOR\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*:\s*(" + re.escape(label_prefix)
                             + r"[A-Za-z0-9_]+)\s*\)", text):
            line = text[:m.start()].count("\n") + 1
            strong.setdefault(m.group(1), "%s:%d" % (rel, line))
    out["node_templates"] = strong
    return out


# ------------------------------------------------------------------ 产出声明（结论码维度）
RULE_DECL_KG = re.compile(r"\b(?:RULE|R-)\s*F\d")
RULE_DECL_ONTO = re.compile(r"产出")


def scan_rule_decls(dirpath, pattern, file_glob, comment_prefix, label):
    """只读载体上的产出声明注释 → {结论码: 'file:line'}；返回 (hits, loose, files, error)。"""
    hits, loose, files = {}, {}, 0
    if not dirpath:
        return hits, loose, files, "未提供 %s 目录" % label
    if not os.path.isdir(dirpath):
        return hits, loose, files, "%s 目录不存在：%s" % (label, dirpath)
    paths = sorted(glob.glob(os.path.join(dirpath, "**", file_glob), recursive=True))
    files = len(paths)
    if not paths:
        return hits, loose, files, "%s 目录里没有 %s" % (label, file_glob)
    for p in paths:
        for i, line in enumerate(read_text(p).splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(comment_prefix):
                continue
            cmt = stripped[len(comment_prefix):]
            for c in re.findall(r"F-00\d", cmt):
                loose.setdefault(c, "%s:%d" % (os.path.basename(p), i))
            if pattern.search(cmt):
                for c in re.findall(r"F-00\d", cmt):
                    hits.setdefault(c, "%s:%d" % (os.path.basename(p), i))
    return hits, loose, files, None


# ------------------------------------------------------------------ ONTO 侧：仓库（只读）
def onto_http(base, repo, sparql, accept="application/sparql-results+json", timeout=120):
    url = "%s/repositories/%s" % (base.rstrip("/"), urllib.parse.quote(repo))
    data = urllib.parse.urlencode({"query": sparql}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Accept", accept)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), None
    except Exception as exc:                            # noqa: BLE001
        return 0, b"", str(exc)


def onto_select(base, repo, sparql, timeout=120):
    status, body, err = onto_http(base, repo, sparql, timeout=timeout)
    if err:
        return status, None, "连接失败：%s" % err
    if status != 200:
        return status, None, "HTTP %s：%s" % (status, body[:300].decode("utf-8", "replace"))
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception as exc:                            # noqa: BLE001
        return status, None, "返回体非 SPARQL JSON：%s" % exc
    return status, doc.get("results", {}).get("bindings", []), None


# VALUES 分批大小：GraphDB 共享实例对带 DISTINCT 或大 VALUES 的查询会按启发式申请堆内存，
# 实测直接 HTTP 500「Query evaluation error: Insufficient free Heap Memory … for group by and
# distinct」。因此所有批量探测①不用 DISTINCT（本地去重）②VALUES 分批（每批 PROBE_CHUNK 个）。
PROBE_CHUNK = 8


def onto_collect(base, repo, items, tbox_ns, timeout=120):
    """只读 GraphDB：先判 TBox 是否装载，再取类 / 属性 / 中性码字面量 / 候选 IRI 声明集合。"""
    out = dict(base=base, repository=repo, tbox_present=False, tbox_triples=None,
               classes=set(), properties=set(), literals=set(), iris=set(),
               classes_all=set(), properties_all=set(), type_histogram={},
               probes_ok=True, error=None, queries=0, status=None, raw={})
    sparql = "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER( %s ) }" % (
        " || ".join('STRSTARTS(STR(?s), "%s")' % ns for ns in tbox_ns))
    status, rows, err = onto_select(base, repo, sparql, timeout)
    out["queries"] += 1
    out["status"] = status
    out["raw"]["tbox_gate"] = err or "HTTP 200"
    if err:
        out["error"] = "TBox 装载探测失败：%s" % err
        return out
    try:
        out["tbox_triples"] = int(rows[0]["n"]["value"])
    except Exception:                                   # noqa: BLE001
        out["error"] = "TBox 装载探测返回体缺少计数"
        return out
    if out["tbox_triples"] <= 0:
        out["error"] = ("仓库 %s 可达但 TBox 命名空间内 0 条三元组 ⇒ 该侧 TBox 尚未装载"
                        % repo)
        return out

    ns_filter = " || ".join('STRSTARTS(STR(?s), "%s")' % ns for ns in tbox_ns)

    # 诊断证据：TBox 命名空间里 rdf:type 的分布。用于区分「目标态 TBox（owl:Class）」与
    # 「ABox / 早期片段（rdfs:Class、rdf:Property、owl:DatatypeProperty …）」。
    q = ('SELECT ?t (COUNT(*) AS ?n) WHERE { ?s a ?t . FILTER( %s ) } '
         'GROUP BY ?t ORDER BY DESC(?n) LIMIT 12' % ns_filter)
    status, rows, err = onto_select(base, repo, q, timeout)
    out["queries"] += 1
    if not err:
        out["type_histogram"] = dict((r["t"]["value"], _as_int(r["n"]["value"])) for r in rows)

    q = 'SELECT ?s WHERE { ?s a <%sClass> }' % OWL
    status, rows, err = onto_select(base, repo, q, timeout)
    out["queries"] += 1
    if err:
        out["error"] = "owl:Class 集合探测失败：%s" % err
        out["probes_ok"] = False
        return out
    out["classes_all"] = set(r["s"]["value"] for r in rows)
    out["classes"] = set(c for c in out["classes_all"] if not is_builtin_iri(c))

    q = 'SELECT ?s WHERE { ?s a <%sObjectProperty> }' % OWL
    status, rows, err = onto_select(base, repo, q, timeout)
    out["queries"] += 1
    if err:
        out["error"] = "owl:ObjectProperty 集合探测失败：%s" % err
        out["probes_ok"] = False
        return out
    out["properties_all"] = set(r["s"]["value"] for r in rows)
    out["properties"] = set(p for p in out["properties_all"] if not is_builtin_iri(p))

    # 就绪门（强化）：只「有三元组」不足以证明目标态 TBox 已装载 —— 仓库里可能只有 ABox
    # 或早期 ontology 片段（实测 cc-ops 即如此：7476 三元组，但 owl:Class 只有 owl:Thing/
    # owl:Nothing）。规格的 defined 口径就是 owl:Class / owl:ObjectProperty，因此要求这两类
    # 声明各至少出现一次（排除 w3c 内建项）；否则该侧取证能力为 0 ⇒ BLOCKED，绝不报 FAIL。
    if not out["classes"] or not out["properties"]:
        out["error"] = (
            "仓库 %s 的 TBox 命名空间内有 %d 条三元组，但没有非内建的 owl:Class（%d 个）"
            "或 owl:ObjectProperty（%d 个）声明 ⇒ 该侧 TBox 未按 owl:Class/owl:ObjectProperty "
            "口径装载（典型情形：仓内只有 ABox 或早期 ontology 片段）" % (
                repo, out["tbox_triples"], len(out["classes"]),
                len(out["properties"])))
        return out
    out["tbox_present"] = True

    codes = sorted(set(it["literal"] for it in items
                       if it["kind"] in ("value_label", "edge_contract") and it["literal"]))
    if codes:
        got = set()
        for i in range(0, len(codes), PROBE_CHUNK):
            chunk = codes[i:i + PROBE_CHUNK]
            # 主体必须落在 TBox 命名空间内（规格：「TBox 命名空间内 … 具名个体声明」）。
            # 否则仓内 ABox 的属性值（实测 cc-ops 的 ABox 里就有 metricCode 等 456+ 条字面量）
            # 会被误当成「ONTO 侧已声明该中性码」⇒ 伪 PASS。
            q = ('SELECT ?o WHERE { ?s ?p ?o . FILTER( %s ) VALUES ?o { %s } }'
                 % (ns_filter,
                    " ".join('"%s"' % c.replace('\\', '\\\\').replace('"', '\\"')
                             for c in chunk)))
            status, rows, err = onto_select(base, repo, q, timeout)
            out["queries"] += 1
            if err:
                out["error"] = ("中性码字面量探测失败（第 %d 批 / 共 %d 批，%d 个中性码）：%s"
                                % (i // PROBE_CHUNK + 1,
                                   (len(codes) + PROBE_CHUNK - 1) // PROBE_CHUNK,
                                   len(codes), err))
                out["probes_ok"] = False
                return out
            got.update(r["o"]["value"] for r in rows)
        out["literals"] = got

    cand = sorted(set(i for it in items for i in (it["onto_iris"] or []) if i))
    if cand:
        got = set()
        for i in range(0, len(cand), PROBE_CHUNK):
            chunk = cand[i:i + PROBE_CHUNK]
            q = ('SELECT ?s WHERE { VALUES ?s { %s } ?s ?p ?o }'
                 % " ".join("<%s>" % c for c in chunk))
            status, rows, err = onto_select(base, repo, q, timeout)
            out["queries"] += 1
            if err:
                out["error"] = ("候选 IRI 声明探测失败（第 %d 批 / 共 %d 批，%d 个候选 IRI）：%s"
                                % (i // PROBE_CHUNK + 1,
                                   (len(cand) + PROBE_CHUNK - 1) // PROBE_CHUNK,
                                   len(cand), err))
                out["probes_ok"] = False
                return out
            got.update(r["s"]["value"] for r in rows)
        out["iris"] = got
    return out


# ------------------------------------------------------------------ 判定
def judge_kg(item, live, carrier, live_ok, carrier_ok, kg_rule_decls, carrier_needed):
    """返回 (state, evidence, why)。live_ok=False / 必要证据缺失 ⇒ unknown（不伪报 undefined）。"""
    ev = []
    g = item["group"]
    if item["kind"] == "rule_decl":
        if not carrier_ok:
            return UNK, [], "KG 侧产出声明目录不可用（%s）" % carrier.get("error")
        hit = kg_rule_decls.get(item["code"])
        if hit:
            return DEF, ["kgdecl:%s" % hit], None
        return UNDEF, [], None

    if not live_ok:
        return UNK, [], "KG 侧真机不可取证（%s）" % (live.get("reason") or "未知原因")
    hit = None
    if item["kind"] == "iri_label":
        if item["kg_iri"] in live["labels"]:
            hit = "live:label"
        elif item["kg_iri"] in (live.get("constraint_labels") or []):
            hit = "live:constraint"
        elif carrier_ok and item["kg_iri"] in (carrier.get("node_templates") or {}):
            hit = "carrier:node-template(%s)" % carrier["node_templates"][item["kg_iri"]]
    elif item["kind"] == "iri_reltype":
        if item["kg_iri"] in live["reltypes"]:
            hit = "live:reltype"
        elif carrier_ok and item["kg_iri"] in (carrier.get("reltypes") or {}):
            hit = "carrier:rel-template(%s)" % carrier["reltypes"][item["kg_iri"]]
    elif item["kind"] == "value_label":
        # 故障模式 / 指标 / 阈值不是拓扑类型，契约与映射表都不给 KG 标签（那是载体约定），
        # 故按「带唯一约束的节点类型（TBox 声明）上的字符串属性值」取证；标签由实测得出，
        # 不把 CC_FaultMode / CC_Metric / CC_Threshold 之类的载体名写死在这里。
        labels = [item["kg_iri"]] if item["kg_iri"] else \
            sorted(live.get("constraint_labels") or [])
        for lb in labels:
            if item["literal"] in ((live.get("label_string_values") or {}).get(lb) or set()):
                hit = "live:property-value(%s)" % lb
                break
        if not hit and carrier_ok and item["literal"] in (carrier.get("text") or ""):
            hit = "carrier:literal"
    elif item["kind"] == "edge_contract":
        if item["literal"] in (live.get("contract_ids") or []):
            hit = "live:contractId"
        elif item["literal"] in (live.get("edge_string_values") or []):
            hit = "live:edge-string-value"
        elif carrier_ok and item["literal"] in (carrier.get("contract_ids") or {}):
            hit = "carrier:contractId(%s)" % carrier["contract_ids"][item["literal"]]
        elif carrier_ok and re.search(r"\b%s\b" % re.escape(item["literal"]),
                                      carrier.get("text") or ""):
            hit = "carrier:literal"
    if hit:
        return DEF, [hit], None
    # 未命中：只有在「该维度的全部证据源都可用」时才敢判 undefined
    if carrier_needed and not carrier_ok:
        return UNK, [], ("KG 侧载体目录不可用，无法排除「图上未出现但载体已声明」的情形（%s）"
                         % (carrier.get("error") or ""))
    return UNDEF, [], None


def judge_onto(item, onto, onto_ok, onto_rule_decls, onto_rules_ok):
    if item["kind"] == "rule_decl":
        if not onto_rules_ok:
            return UNK, [], "ONTO 侧产出声明目录不可用"
        hit = onto_rule_decls.get(item["code"])
        if hit:
            return DEF, ["ontodecl:%s" % hit], None
        return UNDEF, [], None
    if not onto_ok:
        return UNK, [], "ONTO 侧仓库不可取证（%s）" % (onto.get("error") or "未知原因")
    ev = []
    if item["kind"] in ("iri_label",):
        for iri in item["onto_iris"] or []:
            if iri in onto["classes"]:
                ev.append("onto:owl:Class<%s>" % iri)
                return DEF, ev, None
    if item["kind"] in ("iri_reltype",):
        for iri in item["onto_iris"] or []:
            if iri in onto["properties"]:
                ev.append("onto:owl:ObjectProperty<%s>" % iri)
                return DEF, ev, None
    if item["kind"] in ("value_label", "edge_contract"):
        if item["literal"] in onto["literals"]:
            ev.append("onto:neutral-literal")
            return DEF, ev, None
        for iri in item["onto_iris"] or []:
            if iri in onto["iris"]:
                ev.append("onto:named-individual<%s>" % iri)
                return DEF, ev, None
    return UNDEF, ev, None


def run_check(args):
    """执行 T10 检查，返回 (exit_code, report)。"""
    notes = ["规格：docs/07 M2.8 规格块 7 条 + docs/00 §6.3 ④",
             "退出码三态：0=PASS / 1=FAIL / 2=BLOCKED（规格只写 0/1，三态为本实现的强化）",
             "中性条目与计数全部从契约 + 映射表现算，代码不写死计数",
             "故障模式 / 阈值 / 指标三组：契约与映射表不提供 KG 标签（那是载体约定），"
             "故按「带唯一约束的节点类型（TBox 声明）上的字符串属性值 ∪ 载体声明文本」取证，"
             "载体标签名不写死在代码里",
             "ONTO 侧就绪门（强化）：TBox 命名空间内必须存在 ≥1 个非内建 owl:Class 且 ≥1 个"
             "非内建 owl:ObjectProperty 声明，否则判「该侧 TBox 未按规格口径装载」⇒ BLOCKED；"
             "只「有三元组」不算就绪（仓内可能只有 ABox 或早期 ontology 片段）。"
             "边界：一旦就绪，逐条缺失即 FAIL，不再区分「装载中」——正式执行须以装载完成的仓库为准",
             "契约自声明排除（★ 标志驱动，不按侧写白名单、不硬编码名字或计数）："
             "topology.yaml#types[] 中 abstract == true 的类型、inverse_relations[] 中 asserted == false 的"
             "反向关系不参与两侧判定，单列 excluded_by_contract 并附排除依据（哪个契约字段为真）；"
             "契约一旦翻转这两个标志，对应条目自动变回可判。某维度可判条目为 0 时打 WARN"
             "（只提示、不静默，且不改变退出码）"]
    blockers = []
    report = {"kind": "t10-native-parity",
              "phase": "M2.8-prep",
              "generatedBy": os.path.abspath(__file__),
              "verdict": None, "exitCode": None,
              "notes": notes, "blockers": blockers, "rows": [],
              "groups": {}, "counts": {}, "evidence": {}}

    try:
        items, info = build_items(args.model_dir, args.mapping_dir, args.findings)
    except ConfigError as exc:
        blockers.append("契约 / 映射不可用，无法构成中性条目集：%s" % exc)
        report["verdict"] = "BLOCKED"
        report["exitCode"] = EXIT_BLOCKED
        return EXIT_BLOCKED, report

    prefixes = info["prefixes"]
    report["counts"] = dict(items=info["items"], assertable=info["assertable_items"],
                            groups=info["counts"], excluded=info["excluded"])
    if info.get("flag_disagreements"):
        notes.append("契约 / 映射表标志不一致（以契约为准，仅记录不改判）：%s"
                     % "；".join(info["flag_disagreements"]))
    if info["unknown_onto_iris"]:
        notes.append("映射表展开失败的 ONTO 候选 IRI（按无候选处理）：%s"
                     % ", ".join(info["unknown_onto_iris"]))

    # ---- 反恒真自检：排除计数必须等于「从契约文件再读一遍」独立复算的排除集
    exp_excluded = None
    try:
        topo_re = read_yaml(os.path.join(args.model_dir, "topology.yaml"))
        exp_excluded = sorted(
            ["types/" + e["code"] for e in (topo_re.get("types") or [])
             if bool(e.get("abstract"))]
            + ["inverse_relations/" + e["code"]
               for e in (topo_re.get("inverse_relations") or [])
               if not bool(e.get("asserted", False))])
    except Exception as exc:                                  # pragma: no cover
        notes.append("契约排除项独立复算失败（%s）：本报告缺少「排除计数 == 契约现算计数」自检" % exc)
    got_excluded = sorted("%s/%s" % (it["group"], it["code"]) for it in items
                          if it["excluded_by_contract"])
    if exp_excluded is not None and exp_excluded != got_excluded:
        blockers.append("排除计数自检失败：契约现算排除集 %s ≠ 条目标记排除集 %s（排除必须完全由契约标志驱动）"
                        % (exp_excluded, got_excluded))

    # ---- KG 侧：口令纪律（缺省即拒绝探测并报 BLOCKED）
    password = (os.environ.get(args.kg_password_env) if args.kg_password_env else None)
    live = dict(ok=False, reason=None, uri=args.kg_uri, user=args.kg_user,
                labels=[], constraint_labels=[], reltypes=[], label_string_values={},
                contract_ids=[], edge_string_values=[])
    if not password:
        live["reason"] = ("★ 未取到环境变量 %s：按约定拒绝探测（fail-closed），"
                          "KG 侧一律按「无法取证」处理，绝不伪报 FAIL"
                          % (args.kg_password_env or "(未指定)"))
        blockers.append("KG 侧 BLOCKED：%s" % live["reason"])
    else:
        live = kg_live_collect(args.kg_uri, args.kg_user, password, args.kg_database,
                               args.kg_label_prefix)
        if not live["ok"]:
            blockers.append("KG 侧 BLOCKED：%s" % live["reason"])

    carrier = kg_carrier_scan(args.kg_ontology_dir, args.kg_label_prefix,
                              args.kg_reltype_prefix)
    carrier_ok = not carrier.get("error")
    if not carrier_ok:
        notes.append("KG 载体目录不可用：%s（该维度的未命中项按 unknown 处理）" % carrier["error"])

    kg_decls, kg_loose, kg_files, kg_err = scan_rule_decls(
        args.kg_ontology_dir, RULE_DECL_KG, "*.cypher", "//", "KG 产出声明")
    kg_rules_ok = kg_err is None

    # ---- ONTO 侧
    onto = dict(error=None, tbox_present=False)
    onto_ok = False
    if args.onto_url and args.onto_repository:
        onto = onto_collect(args.onto_url, args.onto_repository, items, info["tbox_ns"])
        onto_ok = bool(onto.get("tbox_present")) and bool(onto.get("probes_ok", True))
        if not onto_ok:
            blockers.append("ONTO 侧 BLOCKED：%s" % onto.get("error"))
            if not onto.get("probes_ok", True):
                notes.append("ONTO 侧取证不完整（部分探测失败）：按 fail-closed 全维度判 unknown，"
                             "绝不因探测失败而伪报 FAIL")
    else:
        onto["error"] = "未提供 --onto-url / --onto-repository"
        blockers.append("ONTO 侧 BLOCKED：%s" % onto["error"])

    onto_decls, onto_loose, onto_files, onto_err = scan_rule_decls(
        args.onto_rules_dir, RULE_DECL_ONTO, "*.rq", "#", "ONTO 产出声明")
    onto_rules_ok = onto_err is None

    # ---- 逐条判定（契约自声明排除的条目不作两侧判定，单列 excluded_by_contract）
    tally = {}
    rows = []
    for it in items:
        if it["excluded_by_contract"]:
            verdict = "excluded_by_contract"
            tally[verdict] = tally.get(verdict, 0) + 1
            rows.append(dict(group=it["group"], code=it["code"], verdict=verdict,
                             excluded_by_contract=True,
                             exclusionBasis=it["exclusion_basis"],
                             kg=None, onto=None, kgEvidence=[], ontoEvidence=[],
                             kgWhy="按契约标志排除，未作两侧判定", ontoWhy=None,
                             kind=it["kind"], kgIri=it["kg_iri"], ontoIris=it["onto_iris"],
                             meta=it["meta"]))
            continue
        kg_state, kg_ev, kg_why = judge_kg(it, live, carrier, live["ok"], carrier_ok,
                                           kg_decls, carrier_needed=carrier is not None)
        onto_state, onto_ev, onto_why = judge_onto(it, onto, onto_ok, onto_decls,
                                                   onto_rules_ok)
        if UNK in (kg_state, onto_state):
            verdict = "unverifiable"
        elif kg_state == DEF and onto_state == DEF:
            verdict = "defined_both"
        elif kg_state == DEF:
            verdict = "kg_only"
        elif onto_state == DEF:
            verdict = "onto_only"
        else:
            verdict = "neither"
        tally[verdict] = tally.get(verdict, 0) + 1
        rows.append(dict(group=it["group"], code=it["code"], verdict=verdict,
                         excluded_by_contract=False, exclusionBasis=None,
                         kg=kg_state, onto=onto_state, kgEvidence=kg_ev, ontoEvidence=onto_ev,
                         kgWhy=kg_why, ontoWhy=onto_why,
                         kind=it["kind"],
                         kgIri=it["kg_iri"], ontoIris=it["onto_iris"], meta=it["meta"]))
    report["rows"] = rows

    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], dict(count=0, assertable_count=0, excluded_count=0,
                                               defined_both=0, kg_only=0, onto_only=0,
                                               neither=0, unverifiable=0,
                                               kgDefinedOntoUnknown=0,
                                               exclusion_basis=[]))
        g["count"] += 1
        if r["excluded_by_contract"]:
            g["excluded_count"] += 1
            if r["exclusionBasis"] not in g["exclusion_basis"]:
                g["exclusion_basis"].append(r["exclusionBasis"])
            continue
        g["assertable_count"] += 1
        g[r["verdict"]] += 1
        if r["kg"] == DEF and r["onto"] == UNK:
            # 仅供「装载 ONTO 后预期」参考的事实性计数：不写成 kg_only（那等于断言方案2 没有定义）
            g["kgDefinedOntoUnknown"] += 1
    report["groups"] = groups
    report["counts"]["verdicts"] = tally

    # 反恒真：某维度可判条目为 0 ⇒ WARN（不静默，但不改退出码）
    for gname, gv in sorted(groups.items()):
        if gv["assertable_count"] == 0:
            notes.append("★ WARN：维度 %s 的可判条目为 0（%d 条全部被契约标志排除）"
                         "⇒ 该维度对本轮不具判别力，请复核契约标志 %s"
                         % (gname, gv["count"], "；".join(gv["exclusion_basis"]) or "(未记录依据)"))

    n_excl = tally.get("excluded_by_contract", 0)
    if tally.get("unverifiable"):
        report["verdict"] = "BLOCKED"
    elif (tally.get("defined_both", 0) + n_excl == len(rows)
          and not any(tally.get(k) for k in ("kg_only", "onto_only", "neither"))):
        report["verdict"] = "PASS"
    else:
        report["verdict"] = "FAIL"
    report["exitCode"] = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL,
                          "BLOCKED": EXIT_BLOCKED}[report["verdict"]]

    report["evidence"] = {
        "kgLive": dict(ok=live["ok"], reason=live.get("reason"), uri=live.get("uri"),
                       user=live.get("user"), database=args.kg_database,
                       labels=len(live.get("labels") or []),
                       constraints=live.get("constraint_count"),
                       constraintError=live.get("constraint_error"),
                       reltypes=len(live.get("reltypes") or []),
                       contractIds=sorted(set(live.get("contract_ids") or [])),
                       labelsWithValues=len(live.get("label_string_values") or {})),
        "kgCarrier": dict(dir=args.kg_ontology_dir, files=carrier.get("files"),
                          error=carrier.get("error"),
                          labels=sorted((carrier.get("labels") or {}).keys()),
                          reltypes=sorted((carrier.get("reltypes") or {}).keys()),
                          nodeTemplates=sorted((carrier.get("node_templates") or {}).keys()),
                          contractIds=sorted((carrier.get("contract_ids") or {}).keys())),
        "ontoRepo": dict(base=args.onto_url, repository=args.onto_repository,
                         tboxPresent=onto.get("tbox_present"),
                         probesOk=onto.get("probes_ok", True),
                         tboxTriples=onto.get("tbox_triples"), error=onto.get("error"),
                         classes=len(onto.get("classes") or []),
                         classesIncludingBuiltin=len(onto.get("classes_all") or []),
                         objectProperties=len(onto.get("properties") or []),
                         objectPropertiesIncludingBuiltin=len(onto.get("properties_all") or []),
                         typeHistogram=onto.get("type_histogram") or {},
                         neutralLiterals=sorted(onto.get("literals") or []),
                         declaredIris=len(onto.get("iris") or []),
                         queries=onto.get("queries")),
        "ruleDeclarations": {
            "kg": dict(dir=args.kg_ontology_dir, files=kg_files, error=kg_err,
                       hits=kg_decls, looseHits=kg_loose),
            "onto": dict(dir=args.onto_rules_dir, files=onto_files, error=onto_err,
                         hits=onto_decls, looseHits=onto_loose)},
    }

    # 反恒真自检（写进报告，便于复核者一眼看出声明扫描是否漏形）
    kg_only_loose = sorted(set(kg_loose) - set(kg_decls))
    onto_only_loose = sorted(set(onto_loose) - set(onto_decls))
    if kg_only_loose:
        notes.append("KG 产出声明：宽松扫描多出的结论码 %s（说明有未识别形态，需人工复核）"
                     % ", ".join(kg_only_loose))
    if onto_only_loose:
        notes.append("ONTO 产出声明：宽松扫描多出的结论码 %s（需人工复核）"
                     % ", ".join(onto_only_loose))
    if info["items"] != sum(info["counts"].values()):   # pragma: no cover
        blockers.append("中性条目计数自相矛盾")
    return report["exitCode"], report


# ------------------------------------------------------------------ 输出
def format_text(report):
    lines = []
    lines.append("=" * 78)
    lines.append("T10 两侧原生实现覆盖自检（docs/07 M2.8 规格块 · docs/00 §6.3 ④）")
    lines.append("=" * 78)
    c = report.get("counts", {})
    lines.append("中性条目集（唯一来源 = 契约 + 映射表；计数现算不写死）：")
    for g, n in sorted((c.get("groups") or {}).items()):
        lines.append("    %-20s %d" % (g, n))
    lines.append("    %-20s %d  （可判 %d；契约标志排除 %d）"
                 % ("合计", c.get("items", 0), c.get("assertable", 0),
                    (c.get("excluded") or {}).get("count", 0)))
    for b in (c.get("excluded") or {}).get("basis") or []:
        lines.append("        排除依据（契约标志驱动）：%s" % b)
    ev = report.get("evidence", {})
    kgl = ev.get("kgLive", {})
    lines.append("-" * 78)
    lines.append("KG 侧取证：live=%s uri=%s labels=%s constraints=%s reltypes=%s contractIds=%s%s"
                 % ("OK" if kgl.get("ok") else "不可用", kgl.get("uri"), kgl.get("labels"),
                    kgl.get("constraints"), kgl.get("reltypes"),
                    len(kgl.get("contractIds") or []),
                    "" if kgl.get("ok") else "  原因=%s" % kgl.get("reason")))
    kgc = ev.get("kgCarrier", {})
    lines.append("            载体 %s（%s 个 .cypher）labels=%s reltypes=%s nodeTemplates=%s contractIds=%s%s"
                 % (kgc.get("dir"), kgc.get("files"), len(kgc.get("labels") or []),
                    len(kgc.get("reltypes") or []), len(kgc.get("nodeTemplates") or []),
                    len(kgc.get("contractIds") or []),
                    "" if not kgc.get("error") else "  错误=%s" % kgc.get("error")))
    on = ev.get("ontoRepo", {})
    lines.append("ONTO 侧取证：repo=%s@%s TBox装载=%s 三元组=%s classes=%s props=%s 中性字面量=%s%s"
                 % (on.get("repository"), on.get("base"), on.get("tboxPresent"),
                    on.get("tboxTriples"), on.get("classes"), on.get("objectProperties"),
                    len(on.get("neutralLiterals") or []),
                    "" if not on.get("error") else "\n            原因=%s" % on.get("error")))
    rd = ev.get("ruleDeclarations", {})
    lines.append("产出声明取证：KG %s → %s / ONTO %s → %s"
                 % (rd.get("kg", {}).get("dir"), sorted((rd.get("kg", {}).get("hits") or {})),
                    rd.get("onto", {}).get("dir"),
                    sorted((rd.get("onto", {}).get("hits") or {}))))
    lines.append("-" * 78)
    lines.append("逐组（可判 = count − 契约标志排除）：")
    lines.append("    %-20s %5s %8s %8s %12s %8s %9s %8s %12s"
                 % ("group", "count", "assertable", "excluded", "defined_both", "kg_only",
                    "onto_only", "neither", "unverifiable"))
    for g, v in sorted((report.get("groups") or {}).items()):
        lines.append("    %-20s %5d %8d %8d %12d %8d %9d %8d %12d"
                     % (g, v["count"], v["assertable_count"], v["excluded_count"],
                        v["defined_both"], v["kg_only"], v["onto_only"],
                        v["neither"], v["unverifiable"]))
        for b in v.get("exclusion_basis") or []:
            lines.append("        ⊘ 排除依据：%s" % b)
    exc = [r for r in report.get("rows", []) if r.get("excluded_by_contract")]
    if exc:
        lines.append("-" * 78)
        lines.append("按契约标志排除（不作两侧判定，%d 条）：" % len(exc))
        for r in exc:
            lines.append("    [excluded_by_contract] %s/%s  ← %s"
                         % (r["group"], r["code"], r["exclusionBasis"]))
    bad = [r for r in report.get("rows", [])
           if r["verdict"] != "defined_both" and not r.get("excluded_by_contract")]
    if bad:
        lines.append("-" * 78)
        lines.append("非 defined_both 明细（%d 条）：" % len(bad))
        for r in bad:
            lines.append("    [%s] %s/%s  kg=%s%s onto=%s%s"
                         % (r["verdict"], r["group"], r["code"], r["kg"],
                            ("(%s)" % "; ".join(r["kgEvidence"])) if r["kgEvidence"] else "",
                            r["onto"],
                            ("(%s)" % "; ".join(r["ontoEvidence"])) if r["ontoEvidence"] else ""))
            for why in (r["kgWhy"], r["ontoWhy"]):
                if why:
                    lines.append("        · %s" % why)
    else:
        lines.append("-" * 78)
        lines.append("全部中性条目 defined_both。")
    if report.get("blockers"):
        lines.append("-" * 78)
        lines.append("阻断项（BLOCKED 的原因；★ 不作为 FAIL 判据）：")
        for b in report["blockers"]:
            lines.append("    ! %s" % b)
    if report.get("notes"):
        lines.append("-" * 78)
        for n in report["notes"]:
            lines.append("    · %s" % n)
    lines.append("-" * 78)
    lines.append("RESULT: %s  (exit=%d)  条目 %d：%s"
                 % (report["verdict"], report["exitCode"], c.get("items", 0),
                    ", ".join("%s=%d" % (k, v)
                              for k, v in sorted((c.get("verdicts") or {}).items()))))
    return "\n".join(lines)


def write_report_json(path, report):
    doc = json.loads(json.dumps(report, ensure_ascii=False, default=list))
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2))
        fh.write("\n")


# ------------------------------------------------------------------ CLI
def add_arguments(ap):
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"),
                    help="契约目录（topology/fault-modes/causal-edges/metrics.yaml）")
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"),
                    help="映射表目录（type-map.yaml / rel-map.yaml）")
    ap.add_argument("--findings", default=os.path.join(ROOT, "shared", "model", "findings.yaml"),
                    help="中性结论码契约（默认 shared/model/findings.yaml）")
    ap.add_argument("--targets", default=os.path.join(ROOT, "shared", "deploy", "targets.yaml"),
                    help="部署目标参数（取 Neo4j bolt/用户/口令环境变量名、GraphDB url/仓库）")
    ap.add_argument("--kg-uri", default=None, help="Neo4j bolt URI（默认取 --targets）")
    ap.add_argument("--kg-user", default=None, help="Neo4j 用户（默认取 --targets）")
    ap.add_argument("--kg-password-env", default=None,
                    help="Neo4j 口令的环境变量名（默认取 --targets，通常 CC_NEO4J_PASSWORD）")
    ap.add_argument("--kg-database", default="neo4j", help="Neo4j 数据库（默认 neo4j）")
    ap.add_argument("--kg-label-prefix", default=None,
                    help="KG 标签前缀（默认取映射表 namespaces.kg.label_prefix）")
    ap.add_argument("--kg-reltype-prefix", default=None,
                    help="KG 关系类型前缀（默认取映射表 namespaces.kg.reltype_prefix）")
    ap.add_argument("--kg-ontology-dir", default=None,
                    help="KG 侧载体声明目录（.cypher：节点模板 / 关系模板 / contractId / 产出声明）")
    ap.add_argument("--onto-url", default=None, help="GraphDB 基址（默认取 --targets）")
    ap.add_argument("--onto-repository", default=None,
                    help="GraphDB 仓库（默认取 --targets 的隔离仓库）")
    ap.add_argument("--onto-rules-dir", default=None,
                    help="ONTO 侧规则载体目录（.rq：产出声明注释）")
    ap.add_argument("--ccf", default=None,
                    help="（可选，保留兼容）CCF 快照路径；T10 不要求有实例，故当前不参与判定")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结论")
    ap.add_argument("--quiet", action="store_true", help="只在 stderr 输出一行结论")
    return ap


def resolve_from_targets(args):
    """把未显式给出的连接参数从 --targets 数据文件补齐（数据不是代码）。"""
    need = [not args.kg_uri, not args.kg_user, not args.kg_password_env,
            not args.onto_url, not args.onto_repository]
    if not any(need) or not args.targets or not os.path.isfile(args.targets):
        return
    try:
        tg = read_yaml(args.targets) or {}
    except Exception:                                   # noqa: BLE001
        return
    neo = (tg.get("neo4j") or {})
    auth = (neo.get("auth") or {})
    if not args.kg_uri:
        args.kg_uri = neo.get("bolt_uri")
    if not args.kg_user:
        args.kg_user = auth.get("user")
    if not args.kg_password_env:
        args.kg_password_env = auth.get("password_env")
    gdb = (tg.get("graphdb") or {})
    if not args.onto_url:
        args.onto_url = gdb.get("url")
    if not args.onto_repository:
        iso = (gdb.get("isolation") or {})
        args.onto_repository = iso.get("repository") or gdb.get("repository")
    args.kg_database = args.kg_database or "neo4j"


def run_cli(argv=None):
    ap = argparse.ArgumentParser(
        prog="t10_native_parity",
        description="T10 两侧原生实现覆盖自检（中性条目：类型/关系/逆关系/故障模式/阈值/"
                    "因果边/指标/结论码；退出码 0=PASS 1=FAIL 2=BLOCKED）")
    add_arguments(ap)
    args = ap.parse_args(argv)
    resolve_from_targets(args)
    if not args.kg_label_prefix or not args.kg_reltype_prefix:
        try:
            tmap = read_yaml(os.path.join(args.mapping_dir, "type-map.yaml")) or {}
            nskg = (tmap.get("namespaces") or {}).get("kg") or {}
            args.kg_label_prefix = args.kg_label_prefix or nskg.get("label_prefix")
            args.kg_reltype_prefix = args.kg_reltype_prefix or nskg.get("reltype_prefix")
        except Exception:                               # noqa: BLE001
            pass
    code, report = run_check(args)
    if args.out:
        write_report_json(args.out, report)
    if args.json:
        sys.stdout.write(json.dumps(json.loads(json.dumps(report, default=list)),
                                    ensure_ascii=False, indent=2) + "\n")
    elif args.quiet:
        sys.stderr.write("T10 RESULT: %s (exit=%d)\n" % (report["verdict"], code))
    else:
        sys.stdout.write(format_text(report) + "\n")
        if args.out:
            sys.stdout.write("JSON 报告：%s\n" % args.out)
    return code


def main(argv=None):
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
