#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scheme2-onto/tools/load_tbox.py — 方案2（本体侧）TBox 真机装载证据工具。

用途
----
把方案2 的 TBox（`scheme2-onto/ontology/cc-*.ttl`）装进 GraphDB 专用仓库
`cc-ops` 的**独立命名图** `https://credit-card-ops.ai/onto/graph/tbox`，并留下
「装载前 / 装载后」的可复核证据；同时提供 `rollback`（只删该图）与 `probe`（只读）。

三模式（`--mode` 或第一个位置参数：probe | persist | rollback），FAIL-CLOSED：
任何异常 / HTTP 非 2xx / 计数不可解析 ⇒ 打印 `RESULT: FAIL` 并以 exit 1 结束。

★ 装配口径：**排除** `scheme2-onto/shapes/shacl-constraints.ttl`
------------------------------------------------------------------------
本工具只装配 `scheme2-onto/ontology/` 下的 7 个 `cc-*.ttl`
（cc-action / cc-audit / cc-causal / cc-core / cc-fault / cc-metric / cc-topology），
**不含** `scheme2-onto/shapes/shacl-constraints.ttl`。依据（只读引用，本工具不修改它们）：

  * `docs/00-总体设计与裁决记录.md:347` —— 「④ 的显式排除清单（载体特有、**不计入** ④）：
    … 方案2 的 SHACL 形状（**T11**）与 OntoClean 元属性（**T12**）…」。
  * `docs/07-实施计划与验收.md:171`（排除清单第 4 条）—— 「Cypher 约束与索引、
    SHACL 形状（**T11**）、OntoClean 元属性（**T12**）、规则的**可执行性**」。
  * `docs/04-方案2设计（本体+LLM）.md:429` —— 「| **T11** | **SHACL 全量校验零违例**
    （方案2 特有） |」⇒ SHACL 形状由 T11 承载，不属于 T10 的 ONTO「defined」口径。
  * T10 的 ONTO「defined」口径本体的实现位置：`shared/verify/t10_native_parity.py:469-537`
    （`onto_collect`，就绪门在 529-537）与 `:644-673`（`judge_onto`）—— 只判
    TBox 命名空间内**非内建**的 `owl:Class` / `owl:ObjectProperty` 与具名候选 IRI /
    中性字面量。SHACL 形状里 `owl:Class` 0 个、`owl:ObjectProperty` 0 个
    （实测 709 三元组，谓词以 `shacl#path` / `shacl#property` 为主），故排除它
    **不改变** T10 的就绪门，只影响「TBox 命名空间三元组数」这一原始计数。

★ 独立命名图 vs 默认图
------------------------------------------------------------------------
`?context=<IRI>` 上传（**尖括号不可省**，否则 GraphDB 返回 400）。T10 的 ONTO 侧 SPARQL
不带 `GRAPH` 子句（见 `shared/verify/t10_native_parity.py:475-476`、`:505`、`:515`），
而 GraphDB 的默认数据集是**命名图并集**，故装入独立命名图即可被 T10 看见；
装进默认图同样能被看见，但会污染 `parity.py`(G14) 等按图作用域的检查口径。本工具
坚持装进独立命名图，并在 `persist` 里用**实测**证明 6 个快照图的逐图三元组数不变。

只读边界
--------
本工具只写两处：① GraphDB `cc-ops` 的 tbox 命名图；② `tmp/m2_15_*` 证据物。
临时仓库 `cc-tbox-probe-*` 用完即删（无论成败），删除前后都打印仓库列表证明既有仓库
（`coexisting_repositories`）未被触碰。不写任何项目内既有文件。
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import time
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

TARGETS_DEFAULT = os.path.join(ROOT, "shared", "deploy", "targets.yaml")
TYPEMAP_DEFAULT = os.path.join(ROOT, "shared", "mapping", "type-map.yaml")
ONTOLOGY_DIR_DEFAULT = os.path.join(ROOT, "scheme2-onto", "ontology")
TMP_DIR = os.path.join(ROOT, "tmp")

# 目标命名图（独立于快照图）。`tbox` 不是合法 snapshotId，见 SNAPSHOT_ID_RE。
TBOX_GRAPH = "https://credit-card-ops.ai/onto/graph/tbox"

# 与 `shared/verify/t10_native_parity.py:74-79` 的 BUILTIN_NS 逐字一致。
# 此处复制常量而不 import（该模块以文件路径被 equivalence.py 加载，且 `:326-335`
# 明写「不使用 sys.path，避免触发分层门禁 G15」）——复制可保证两侧口径一致，
# 又不引入 import 耦合。若该常量日后变动，本文件须同步。
BUILTIN_NS = (
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2001/XMLSchema#",
)
OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"
OWL_OBJPROP = "http://www.w3.org/2002/07/owl#ObjectProperty"
RDFS_CLASS = "http://www.w3.org/2000/01/rdf-schema#Class"
RDF_PROPERTY = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property"
OWL_NOTHING = "http://www.w3.org/2002/07/owl#Nothing"
OWL = "http://www.w3.org/2002/07/owl#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

# 快照 id 口径：`shared/model/ccf.schema.json` → properties.snapshotId.pattern
SNAPSHOT_ID_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")

# ★ 不以「快照图必须恰好 N 个」作硬门：实测期间有并行施工者正在往 cc-ops 装载新快照
#   （13109 条命名图逐图之和里快照图从 6 个变成 7 个），写死数量会把并发装载误判成基线异常。
#   正确的结构性判据是：**每个命名图要么匹配 named_graph_template，要么就是 tbox 图**。
EXPECTED_SNAPSHOT_GRAPHS = 6  # 仅作装载时的观测记录/提示，不作门禁
TMP_REPO_PREFIX = "cc-tbox-probe-"
ABOX_PRECHECK_MAX_TRIPLES = 200000          # 超过则跳过「含 ABox」预检并要求人工裁决


class Fail(Exception):
    """FAIL-CLOSED：任何失败一律经由本异常走到 `RESULT: FAIL` + exit 1。"""


# --------------------------------------------------------------------------
# 配置（数据不是代码）
# --------------------------------------------------------------------------
def read_yaml(path):
    try:
        import yaml
    except ImportError as exc:                          # pragma: no cover
        raise Fail("需要 PyYAML 读取 %s：%s" % (path, exc))
    if not os.path.isfile(path):
        raise Fail("配置文件不存在：%s" % path)
    with io.open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise Fail("配置文件不是映射：%s" % path)
    return doc


def load_config(args):
    tg = read_yaml(args.targets)
    gdb = tg.get("graphdb") or {}
    iso = gdb.get("isolation") or {}
    base = (gdb.get("url") or "").rstrip("/")
    repo = iso.get("repository")
    template = iso.get("named_graph_template")
    ruleset = iso.get("ruleset")
    missing = [n for n, v in (("graphdb.url", base), ("graphdb.isolation.repository", repo),
                              ("graphdb.isolation.named_graph_template", template),
                              ("graphdb.isolation.ruleset", ruleset)) if not v]
    if missing:
        raise Fail("targets.yaml 缺少键：%s" % ", ".join(missing))
    if "{snapshotId}" not in template:
        raise Fail("named_graph_template 不含 {snapshotId}：%s" % template)

    tmap = read_yaml(args.type_map)
    nsobj = tmap.get("namespaces") or {}
    onto_base = ((nsobj.get("onto") or {}).get("base") or "").strip()
    if not onto_base:
        raise Fail("type-map.yaml 缺少 namespaces.onto.base")
    prefixes = tmap.get("prefixes") or {}
    # 与 `shared/verify/t10_native_parity.py:164-166` 同口径：以 onto base 开头的**前缀值**。
    tbox_ns = sorted(v for v in prefixes.values()
                     if isinstance(v, str) and v.startswith(onto_base))
    if not tbox_ns:
        raise Fail("type-map.yaml 里没有以 %s 开头的 onto 前缀值" % onto_base)

    coexisting = iso.get("coexisting_repositories") or []
    return {
        "base": base, "repo": repo, "template": template, "ruleset": ruleset,
        "onto_base": onto_base, "tbox_ns": tbox_ns,
        "coexisting": [str(x) for x in coexisting],
        "targets_path": os.path.abspath(args.targets),
    }


def discover_turtle(ontology_dir):
    """装配 7 个 `cc-*.ttl`；显式排除 `shapes/shacl-constraints.ttl`（见文件头口径）。"""
    if not os.path.isdir(ontology_dir):
        raise Fail("本体目录不存在：%s" % ontology_dir)
    files = sorted(glob.glob(os.path.join(ontology_dir, "cc-*.ttl")))
    if not files:
        raise Fail("本体目录内没有 cc-*.ttl：%s" % ontology_dir)
    excluded = os.path.join(ontology_dir, "shapes", "shacl-constraints.ttl")
    for f in files:
        if os.path.basename(os.path.dirname(f)) == "shapes":
            raise Fail("装配清单里出现了 shapes/ 下的文件：%s" % f)
    return files, (excluded if os.path.isfile(excluded) else None)


# --------------------------------------------------------------------------
# HTTP / SPARQL（纯标准库）
# --------------------------------------------------------------------------
def _req(url, method="GET", data=None, headers=None, timeout=120, allow=()):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:800]
        if exc.code in allow:
            return exc.code, body
        raise Fail("HTTP %s %s -> %s %s\n%s"
                   % (method, url, exc.code, exc.reason, body[:600].decode("utf-8", "replace")))
    except Exception as exc:                            # URLError / timeout / ...
        raise Fail("HTTP %s %s -> %s" % (method, url, exc))


def sparql(cfg, query, repo=None, timeout=180, accept="application/sparql-results+json"):
    repo = repo or cfg["repo"]
    url = "%s/repositories/%s" % (cfg["base"], urllib.parse.quote(repo))
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    st, body = _req(url, "POST", data,
                    {"Accept": accept,
                     "Content-Type": "application/x-www-form-urlencoded"}, timeout)
    if accept != "application/sparql-results+json":
        return body
    try:
        doc = json.loads(body.decode("utf-8"))
        return doc["results"]["bindings"]
    except Exception as exc:
        raise Fail("SPARQL 结果不可解析（%s）；查询=%s\n%s"
                   % (exc, query, body[:300].decode("utf-8", "replace")))


def scalar(cfg, query, repo=None, var="n", timeout=180):
    rows = sparql(cfg, query, repo=repo, timeout=timeout)
    if not rows:
        raise Fail("计数查询返回空结果集；查询=%s" % query)
    cell = rows[0].get(var)
    if not isinstance(cell, dict) or "value" not in cell:
        raise Fail("计数不可解析（缺 ?%s）；查询=%s\n返回=%s" % (var, query, rows[:2]))
    try:
        return int(cell["value"])
    except Exception:
        raise Fail("计数不是整数：%r；查询=%s" % (cell.get("value"), query))


def sparql_update(cfg, update, repo=None, timeout=300):
    repo = repo or cfg["repo"]
    url = "%s/repositories/%s/statements" % (cfg["base"], urllib.parse.quote(repo))
    data = urllib.parse.urlencode({"update": update}).encode("utf-8")
    return _req(url, "POST", data,
                {"Content-Type": "application/x-www-form-urlencoded"}, timeout)


def upload_turtle(cfg, graph_iri, path, repo=None, timeout=300):
    """`?context=<IRI>` —— 尖括号不可省，否则 GraphDB 返回 400。

    `graph_iri is None` ⇒ 不传 context（写默认图），仅用于命名图 / 默认图可见性对照实验。
    """
    repo = repo or cfg["repo"]
    url = "%s/repositories/%s/statements" % (cfg["base"], urllib.parse.quote(repo))
    if graph_iri is not None:
        ctx = urllib.parse.quote("<%s>" % graph_iri, safe="")
        url += "?context=%s" % ctx
    with io.open(path, "rb") as fh:
        payload = fh.read()
    st, _ = _req(url, "POST", payload, {"Content-Type": "text/turtle"}, timeout)
    if st // 100 != 2:
        raise Fail("上传非 2xx：%s -> %s" % (path, st))
    return st


# ---------------------------------------------------------------------------
# ★ 2026-10-14 新增：落地校验（landing verification）
#   动机：persist 曾报 `tbox 图 4380 → 4915` + `RESULT: PASS`，而同一命名图的外部查询
#   计数纹丝未动（4380）、`a ccf:FaultMode` 仍为 14 ⇒ **PASS 判据不校验落地**。
#   本函数把「TTL 里声明的个体数」与「目标命名图里的实际个体数」逐类对账，不等即 Fail。
#
#   ★ 两道防呆（都是为了不让它变成新的「自证式通过」）：
#     ① 前缀表**从 TTL 的 @prefix 声明解析**，不依赖任何外部常量；
#     ② **一个类都没查到 ⇒ 直接 Fail**（空转的检查比没有检查更坏）。
# ---------------------------------------------------------------------------
_DECL_RE = re.compile(
    r"(?m)^[ \t]*([A-Za-z][A-Za-z0-9]*):([A-Za-z0-9_.\-]+)[ \t]+a[ \t]+"
    r"([A-Za-z][A-Za-z0-9]*):([A-Za-z0-9_.\-]+)")
_PREFIX_RE = re.compile(r"(?m)^@prefix[ \t]+([A-Za-z][A-Za-z0-9]*):[ \t]+<([^>]+)>")


def parse_prefixes(ttl_files):
    """从 TTL 的 `@prefix pfx: <IRI> .` 解析前缀表。"""
    out = {}
    for path in ttl_files:
        src = io.open(path, encoding="utf-8", errors="replace").read()
        for m in _PREFIX_RE.finditer(src):
            out.setdefault(m.group(1), m.group(2))
    return out


def declared_class_counts(ttl_files):
    """{('pfx','Class'): 该 TTL 里以 `pfx:local a pfx:Class` 形式声明的主语数}。

    保守统计：只认行首 `<pfx>:<local> a <pfx>:<Class>`；漏计只会让断言更宽松，不会误报失败。
    """
    per = {}
    for path in ttl_files:
        src = io.open(path, encoding="utf-8", errors="replace").read()
        for m in _DECL_RE.finditer(src):
            per.setdefault((m.group(3), m.group(4)), set()).add((m.group(1), m.group(2)))
    return {k: len(v) for k, v in per.items()}


def verify_landing(cfg, ttl_files, graph_iri, repo=None):
    """逐类对账。返回 (ok, rows, bad)。rows=[(类, 声明数, 图中数, 通过)]。

    ★ 若解析出的前缀表为空、或没有查到任何类 ⇒ **直接判不通过**（不允许空转通过）。
    """
    prefixes = parse_prefixes(ttl_files)
    if not prefixes:
        raise Fail("落地校验无法进行：未能从 TTL 解析出任何 @prefix（拒绝空转通过）")
    declared = declared_class_counts(ttl_files)
    if not declared:
        raise Fail("落地校验无法进行：未能从 TTL 解析出任何个体声明（拒绝空转通过）")

    rows, bad, checked = [], [], 0
    for (pfx, cls), n in sorted(declared.items(), key=lambda kv: (-kv[1], kv[0])):
        if n < 2:           # 只对「应该有多个个体」的类对账，单例类噪声大
            continue
        iri = prefixes.get(pfx)
        if not iri:
            continue
        cls_iri = "%s%s" % (iri, cls)
        q = ("SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE { GRAPH <%s> { ?s a <%s> } }"
             % (graph_iri, cls_iri))
        try:
            got = int(scalar(cfg, q, repo=repo, var="n"))
        except Exception as exc:  # noqa: BLE001
            got = -1
            print("    [落地校验] %s:%s 计数异常：%s" % (pfx, cls, str(exc)[:110]))
        checked += 1
        # ★ 口径：owl/rdf 等**推断相关**前缀只要求「图 ≥ 声明」（多出来的是 owl2-rl 推断产物）；
        #   项目自有前缀（ccf/ccm/ccc/ccp/cca/cco/cc）被直接断言，必须**严格相等**。
        inferred_ok = pfx in ("owl", "rdf", "rdfs", "xsd", "skos", "prov")
        ok = (got >= n) if inferred_ok else (got == n)
        rows.append((cls, n, got, ok))
        if not ok:
            bad.append((cls, n, got))
    if checked == 0:
        raise Fail("落地校验无法进行：没有任何类进入对账（拒绝空转通过）")
    return (not bad), rows, bad


def upload_ntriples(cfg, graph_iri, payload, repo=None, timeout=600):
    repo = repo or cfg["repo"]
    ctx = urllib.parse.quote("<%s>" % graph_iri, safe="")
    url = "%s/repositories/%s/statements?context=%s" % (
        cfg["base"], urllib.parse.quote(repo), ctx)
    st, _ = _req(url, "POST", payload, {"Content-Type": "application/n-triples"}, timeout)
    if st // 100 != 2:
        raise Fail("上传 N-Triples 非 2xx：%s -> %s" % (graph_iri, st))
    return st


def repo_list(cfg):
    st, body = _req("%s/rest/repositories" % cfg["base"], "GET", None, None, 60)
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise Fail("仓库列表不可解析（%s）：%s" % (exc, body[:300]))
    ids = []
    for item in doc:
        rid = item.get("id") if isinstance(item, dict) else None
        if rid:
            ids.append(str(rid))
    if not ids:
        raise Fail("仓库列表为空（应含 coexisting_repositories）")
    return sorted(ids)


def repo_reachable(cfg):
    st, body = _req("%s/rest/repositories" % cfg["base"], "GET", None, None, 60)
    return st == 200 and bool(body)


# --------------------------------------------------------------------------
# 探测（只读）
# --------------------------------------------------------------------------
def named_graphs(cfg, repo=None):
    rows = sparql(cfg, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g",
                  repo=repo, timeout=180)
    return sorted(r["g"]["value"] for r in rows if r.get("g", {}).get("type") == "uri")


def subjects_of_type(cfg, type_iri, repo=None):
    rows = sparql(cfg, "SELECT ?s WHERE { ?s a <%s> } ORDER BY ?s" % type_iri,
                  repo=repo, timeout=180)
    return sorted(r["s"]["value"] for r in rows if r.get("s", {}).get("value"))


def count_subjects_of_type(cfg, type_iri, repo=None):
    return scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s a <%s> }" % type_iri,
                  repo=repo)


def ns_filter_clause(ns_list, var="?s"):
    return " || ".join('STRSTARTS(STR(%s), "%s")' % (var, ns) for ns in ns_list)


IN_NAMED = "FILTER EXISTS { GRAPH ?g { ?s ?p ?o } }"
NOT_IN_NAMED = "FILTER NOT EXISTS { GRAPH ?g { ?s ?p ?o } }"


def t10_gate_visibility(cfg, repo=None):
    """照抄 T10 的 ONTO 就绪门查询口径（`shared/verify/t10_native_parity.py:505` / `:515`，
    **不带 GRAPH 子句**），返回该仓库下 T10 会看见的 classes / properties 计数。

    用于「TBox 装入独立命名图」与「装入默认图」的可见性对照实验。
    """
    def nonbuiltin(type_iri):
        rows = sparql(cfg, "SELECT ?s WHERE { ?s a <%s> }" % type_iri, repo=repo, timeout=180)
        vals = [r["s"]["value"] for r in rows if r.get("s", {}).get("value")]
        nb = sorted(v for v in vals if not any(v.startswith(n) for n in BUILTIN_NS))
        return nb
    cls = nonbuiltin(OWL_CLASS)
    prp = nonbuiltin(OWL_OBJPROP)
    return {
        "classesIncludingBuiltin": len(sparql(
            cfg, "SELECT ?s WHERE { ?s a <%s> }" % OWL_CLASS, repo=repo, timeout=180)),
        "classesNonBuiltin": cls,
        # ★ 每次上传都会生成**新的**匿名 blank node 标签（`node1`/`node38`…），
        #   两轮装载的 blank node 名称必然不同；因此对照实验必须比较**具名 IRI** 部分，
        #   否则会把「同样 65/74、只是 bnode 标签变了」误判成「有差别」。
        "classesNonBuiltinNamed": [c for c in cls if not c.startswith("_:")
                                   and not re.match(r"^node\d+$", c)],
        "classesNonBuiltinAnon": len(cls) - len([c for c in cls if not c.startswith("_:")
                                                 and not re.match(r"^node\d+$", c)]),
        "objectPropertiesNonBuiltin": prp,
        "objectPropertiesNonBuiltinNamed": [p for p in prp if not p.startswith("_:")
                                            and not re.match(r"^node\d+$", p)],
    }


def probe(cfg, label, repo=None):
    """只读探测。返回结构化证据 dict（写进 tmp/m2_15_*.json）。

    ★ 关于「默认图」：GraphDB（RDF4J）不带 `FROM`/`GRAPH` 的查询在**整个仓库**上求值
    （默认数据集 = 默认上下文 ∪ 全部命名图，按**集合语义去重**），因此
    「并集总数 − 逐图之和」**不是**默认图条数（会得到负数）。这里分别实测：
      * `namedGraphDistinctCount`   命名图并集（去重）
      * `defaultGraphOnlyCount`     只存在于默认上下文、不在任何命名图内的条数
      * `unionTotal`                无 GRAPH 子句的总数 = 上两者之和
    """
    repo = repo or cfg["repo"]
    t0 = time.time()
    out = {
        "label": label,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": cfg["base"], "repository": repo, "ruleset": cfg["ruleset"],
        "targets": cfg["targets_path"],
        "namedGraphTemplate": cfg["template"],
        "tboxGraph": TBOX_GRAPH,
        "tboxNamespacePrefixes": cfg["tbox_ns"],
    }
    out["reachable"] = repo_reachable(cfg)
    if not out["reachable"]:
        raise Fail("GraphDB 不可达：%s" % cfg["base"])
    out["repositories"] = repo_list(cfg)
    if repo not in out["repositories"]:
        raise Fail("目标仓库不存在：%s（现有 %s）" % (repo, out["repositories"]))

    graphs = named_graphs(cfg, repo=repo)
    out["namedGraphs"] = graphs
    per = {}
    for g in graphs:
        per[g] = scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?s ?p ?o } }" % g,
                        repo=repo)
    out["namedGraphCounts"] = per
    out["namedGraphSum"] = sum(per.values())
    out["namedGraphDistinctCount"] = scalar(
        cfg, """SELECT (COUNT(*) AS ?n) WHERE {
            { SELECT DISTINCT ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } } } }""", repo=repo)
    out["unionTotal"] = scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", repo=repo)
    out["defaultGraphOnlyCount"] = scalar(
        cfg, """SELECT (COUNT(*) AS ?n) WHERE {
            { SELECT DISTINCT ?s ?p ?o WHERE { ?s ?p ?o %s } } }""" % NOT_IN_NAMED, repo=repo)

    for name, iri, key_all, key_nb in (
            ("owl:Class", OWL_CLASS, "owlClassAll", "owlClassNonBuiltin"),
            ("owl:ObjectProperty", OWL_OBJPROP, "owlObjectPropertyAll", "owlObjectPropertyNonBuiltin"),
            ("rdfs:Class", RDFS_CLASS, "rdfsClassAll", "rdfsClassNonBuiltin"),
            ("rdf:Property", RDF_PROPERTY, "rdfPropertyAll", "rdfPropertyNonBuiltin")):
        subs = subjects_of_type(cfg, iri, repo=repo)
        nb = [s for s in subs if not any(s.startswith(n) for n in BUILTIN_NS)]
        out[key_all] = len(subs)
        out[key_nb] = len(nb)
        if name == "owl:Class":
            out["owlClassNonBuiltinIris"] = nb
        if name == "owl:ObjectProperty":
            out["owlObjectPropertyNonBuiltinIris"] = nb
        out.setdefault("declarationDetail", {})[name] = {
            "all": sorted(subs), "nonBuiltin": nb,
            "builtin": sorted(s for s in subs if s not in set(nb))}

    # 声明来源（默认图独占 vs 命名图内）——用于诚实说明 T10 看到的声明是不是「装进去的」
    out.setdefault("declarationDetail", {})["sourceSplit"] = {}
    for name, iri in (("owl:Class", OWL_CLASS), ("owl:ObjectProperty", OWL_OBJPROP),
                      ("rdfs:Class", RDFS_CLASS), ("rdf:Property", RDF_PROPERTY)):
        out["declarationDetail"]["sourceSplit"][name] = {
            "inNamedGraphs": scalar(
                cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s a <%s> %s }" % (iri, IN_NAMED), repo=repo),
            "defaultGraphOnly": scalar(
                cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s a <%s> %s }" % (iri, NOT_IN_NAMED),
                repo=repo)}

    out["tboxNsTriplesBasePrefix"] = scalar(
        cfg, 'SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(STRSTARTS(STR(?s), "%s")) }'
        % cfg["onto_base"], repo=repo)
    filt = ns_filter_clause(cfg["tbox_ns"])
    out["tboxNsTriplesT10Prefixes"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(%s) }" % filt, repo=repo)
    out["tboxNsTriplesT10PrefixesInNamed"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(%s) %s }" % (filt, IN_NAMED),
        repo=repo)
    out["tboxNsTriplesT10PrefixesDefaultOnly"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o . FILTER(%s) %s }" % (filt, NOT_IN_NAMED),
        repo=repo)
    out["tboxGraphTriples"] = per.get(TBOX_GRAPH, 0)
    # ★ GraphDB/owl2-rl 行为证据：**推断三元组落在哪里**。实测推断全部写入**默认上下文**
    #   （命名图内只有显式上传的语句），因此命名图逐图计数不受推理影响。
    out["defaultGraphOnlyPredicateHistogram"] = [
        {"predicate": r["p"]["value"], "count": int(r["n"]["value"])}
        for r in sparql(cfg, """SELECT ?p (COUNT(*) AS ?n) WHERE { ?s ?p ?o %s }
            GROUP BY ?p ORDER BY DESC(?n) LIMIT 20""" % NOT_IN_NAMED, repo=repo)]
    out["inNamedPredicateIsAllExplicit"] = {
        "namedGraphTriples": out["namedGraphSum"],
        "namedGraphDistinct": out["namedGraphDistinctCount"],
    }
    out["tboxReadyGatePass"] = bool(out["owlClassNonBuiltin"] and out["owlObjectPropertyNonBuiltin"])
    out["elapsedSec"] = round(time.time() - t0, 2)
    return out


def classify_graphs(cfg, graphs):
    """把命名图分成：快照图 / 本工具的 tbox 图 / 其它（其它 ⇒ 中止，不猜）。"""
    prefix = cfg["template"].split("{snapshotId}")[0]
    prefix = prefix[:prefix.rfind("/") + 1]
    snaps, mine, other = {}, [], []
    for g in graphs:
        if g == TBOX_GRAPH:
            mine.append(g)
        elif g.startswith(prefix) and SNAPSHOT_ID_RE.match(g[len(prefix):]):
            snaps[g] = g[len(prefix):]
        else:
            other.append(g)
    return snaps, mine, other


def assert_tbox_graph_disjoint(cfg, graphs, snaps):
    """运行时展开 targets.yaml 的 named_graph_template 并断言 tbox 图不与之相撞。

    ★ **允许** tbox 图已经存在：那正是上一次 `persist` 留下的（本工具是重置式装载，
    CLEAR GRAPH <tbox> → 重新上传），再次 persist 必须能成功，否则就不叫幂等。
    这里只断言它**不与任何快照图相撞**、且**不可能是模板展开的产物**。
    """
    if TBOX_GRAPH in snaps.values():
        raise Fail("tbox 图 IRI 等于某个快照图：%s" % TBOX_GRAPH)
    for g, sid in sorted(snaps.items()):
        if cfg["template"].format(snapshotId=sid) != g:
            raise Fail("模板展开与实测图不一致：template(%s) != %s" % (sid, g))
        if g == TBOX_GRAPH:
            raise Fail("tbox 图 IRI 等于快照图 %s" % g)
    # 模板的 {snapshotId} 位必须不可能取到 "tbox"：用 CCF 的 snapshotId 口径证明（反向断言）
    suffix = TBOX_GRAPH[len(cfg["template"].split("{snapshotId}")[0]):]
    if SNAPSHOT_ID_RE.match(suffix):
        raise Fail("tbox 图 IRI 匹配 snapshotId 口径，会与快照图混淆：%s" % TBOX_GRAPH)
    return {"tboxGraphPreExisting": TBOX_GRAPH in graphs,
            "tboxGraphInSnapshots": False,
            "templateSuffix": suffix,
            "templateSuffixMatchesSnapshotId": bool(SNAPSHOT_ID_RE.match(suffix))}


def baseline_is_safe(cfg, pre):
    """persist 前的基线门：命名图拓扑必须与预期一致，否则**中止不写**。

    ★ 不对默认图做「必须为空」的断言：本仓库实测默认图内有 owl2-rl 的**推断**三元组
    （见 probe 的 `defaultGraphOnlyCount` / `declarationDetail.sourceSplit`），
    这是入装载前就存在的既有事实，不是本工具造成的，也不是「基线不符」。
    """
    snaps, mine, other = classify_graphs(cfg, pre["namedGraphs"])
    if other:
        raise Fail("存在非快照、非 tbox 的命名图，基线不符合预期，拒绝写入：%s" % other)
    if not snaps:
        raise Fail("没有实测到任何快照命名图，基线不符合预期，拒绝写入")
    if pre["unionTotal"] != pre["namedGraphDistinctCount"] + pre["defaultGraphOnlyCount"]:
        raise Fail("并集总数 != 命名图并集 + 默认图独占（%d != %d + %d）⇒ 计数口径异常，拒绝写入"
                   % (pre["unionTotal"], pre["namedGraphDistinctCount"],
                      pre["defaultGraphOnlyCount"]))
    # 反向校验：默认图独有里不能有任何三元组同时出现在命名图中（口径自洽性）
    overlap = scalar(cfg, """SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o %s .
        FILTER EXISTS { GRAPH ?g { ?s ?p ?o } } }""" % NOT_IN_NAMED, repo=cfg["repo"])
    if overlap:
        raise Fail("默认图独有集合与命名图并集有 %d 条重叠 ⇒ 计数口径自相矛盾，拒绝写入" % overlap)
    return snaps, mine


# --------------------------------------------------------------------------
# 临时仓库一致性预检（用完即删，绝不碰既有仓库）
# --------------------------------------------------------------------------
def temp_repo_config(cfg, name):
    return """@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rep: <http://www.openrdf.org/config/repository#> .
@prefix sr: <http://www.openrdf.org/config/repository/sail#> .
@prefix sail: <http://www.openrdf.org/config/sail#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .

[] a rep:Repository ;
   rep:repositoryID "%s" ;
   rdfs:label "M2.15 TBox 预检临时仓库（用完即删）" ;
   rep:repositoryImpl [
       rep:repositoryType "graphdb:SailRepository" ;
       sr:sailImpl [
           sail:sailType "graphdb:Sail" ;
           graphdb:ruleset "%s" ;
           graphdb:read-only "false"
       ]
   ] .
""" % (name, cfg["ruleset"])


def create_temp_repo(cfg, name, timeout=180):
    """POST /rest/repositories —— multipart **必须带 filename**，否则 GraphDB 报错。"""
    boundary = "----ccTboxProbeBoundary%s" % int(time.time() * 1000)
    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += (b'Content-Disposition: form-data; name="config"; filename="config.ttl"\r\n')
    body += b"Content-Type: text/turtle\r\n\r\n"
    body += temp_repo_config(cfg, name).encode("utf-8")
    body += ("\r\n--%s--\r\n" % boundary).encode()
    st, _ = _req("%s/rest/repositories" % cfg["base"], "POST", body,
                 {"Content-Type": "multipart/form-data; boundary=%s" % boundary}, timeout)
    if st // 100 != 2:
        raise Fail("创建临时仓库失败：HTTP %s" % st)
    # 等待可查询
    for _ in range(30):
        try:
            scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", repo=name, timeout=30)
            return
        except Fail:
            time.sleep(1)
    raise Fail("临时仓库 %s 创建后不可查询" % name)


def delete_temp_repo(cfg, name, timeout=180):
    st, _ = _req("%s/rest/repositories/%s" % (cfg["base"], urllib.parse.quote(name)),
                 "DELETE", None, None, timeout, allow=(404,))
    if st not in (200, 202, 204, 404):
        raise Fail("删除临时仓库 %s 返回 HTTP %s" % (name, st))


def consistency_checks(cfg, repo, stage):
    """一致性/矛盾检查。返回 (report_dict, ok_bool)。"""
    rep = {"stage": stage, "repository": repo}
    rep["total"] = scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", repo=repo)
    rep["owlClassNonBuiltin"] = len([s for s in subjects_of_type(cfg, OWL_CLASS, repo=repo)
                                     if not any(s.startswith(n) for n in BUILTIN_NS)])
    rep["owlObjectPropertyNonBuiltin"] = len(
        [s for s in subjects_of_type(cfg, OWL_OBJPROP, repo=repo)
         if not any(s.startswith(n) for n in BUILTIN_NS)])
    rep["owlDisjointWithStatements"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?a <%sdisjointWith> ?b }" % "http://www.w3.org/2002/07/owl#",
        repo=repo)
    rep["owlAllDisjointClassesStatements"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?d a <%sAllDisjointClasses> }"
        % "http://www.w3.org/2002/07/owl#", repo=repo)
    rep["selfDisjointClasses"] = [r["c"]["value"] for r in sparql(
        cfg, "SELECT DISTINCT ?c WHERE { ?c <%sdisjointWith> ?c }"
        % "http://www.w3.org/2002/07/owl#", repo=repo)]
    rep["instancesInDisjointPair"] = [
        {"x": r.get("x", {}).get("value"), "c1": r.get("c1", {}).get("value"),
         "c2": r.get("c2", {}).get("value")}
        for r in sparql(cfg, """SELECT DISTINCT ?x ?c1 ?c2 WHERE {
            ?c1 <http://www.w3.org/2002/07/owl#disjointWith> ?c2 .
            ?x a ?c1 ; a ?c2 . FILTER(?c1 != ?c2) } LIMIT 50""", repo=repo)]
    rep["instancesInAllDisjointPair"] = [
        {"x": r.get("x", {}).get("value")}
        for r in sparql(cfg, """SELECT DISTINCT ?x WHERE {
            ?d a <http://www.w3.org/2002/07/owl#AllDisjointClasses> ;
               <http://www.w3.org/2002/07/owl#members> ?l .
            ?l <http://www.w3.org/1999/02/22-rdf-syntax-ns#rest>*/<http://www.w3.org/1999/02/22-rdf-syntax-ns#first> ?m1 .
            ?l <http://www.w3.org/1999/02/22-rdf-syntax-ns#rest>*/<http://www.w3.org/1999/02/22-rdf-syntax-ns#first> ?m2 .
            FILTER(?m1 != ?m2) ?x a ?m1 ; a ?m2 . } LIMIT 50""", repo=repo)]
    rep["owlNothingInstances"] = [
        r["x"]["value"] for r in sparql(
            cfg, "SELECT DISTINCT ?x WHERE { ?x a <%s> } LIMIT 50" % OWL_NOTHING, repo=repo)]
    # ★ `owl:Nothing` 必须排除：OWL 2 RL 含有 `cls-nothing1`（对每个类 C 都蕴含
    #   `owl:Nothing rdfs:subClassOf C`；实测本仓 `owl:Nothing` 的上级 = 全部 65 个类，
    #   而 7 个 TTL 里 `owl:Nothing` 的 subClassOf 显式条数 = 0）。若把 owl:Nothing 算进
    #   不可满足类，任意一条 `owl:disjointWith` 都会凭空产生 2 条"矛盾"，是纯粹的推理伪影，
    #   与被装载的 TTL 无关（证据见 tmp/m2_15_probe_disjoint_detail.py 的输出）。
    rep["owlNothingSubClassOfCount"] = scalar(
        cfg, "SELECT (COUNT(*) AS ?n) WHERE { <%s> <%ssubClassOf> ?c }" % (OWL_NOTHING, RDFS),
        repo=repo)
    rep["owlNothingEquivalents"] = [
        r["c"]["value"] for r in sparql(
            cfg, "SELECT DISTINCT ?c WHERE { ?c <%sequivalentClass> <%s> }"
            % (OWL, OWL_NOTHING), repo=repo)]
    rep["unsatisfiableClasses"] = [
        {"c": r.get("c", {}).get("value"), "c1": r.get("c1", {}).get("value"),
         "c2": r.get("c2", {}).get("value")}
        for r in sparql(cfg, """SELECT DISTINCT ?c ?c1 ?c2 WHERE {
            ?c1 <http://www.w3.org/2002/07/owl#disjointWith> ?c2 .
            ?c <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?c1 ;
               <http://www.w3.org/2000/01/rdf-schema#subClassOf> ?c2 .
            FILTER(?c != <http://www.w3.org/2002/07/owl#Nothing>)
            FILTER(!ISBLANK(?c)) } LIMIT 50""", repo=repo)]
    # ★ 真正的一致性信号（与上面的伪影区分开）：
    #   ① 有**个体**被断言为 owl:Nothing；② 一个**具名非内建类**同时 ⊑ 两个互斥类（上面已排伪影）。
    rep["decisiveInconsistency"] = bool(rep["owlNothingInstances"] or rep["unsatisfiableClasses"]
                                        or rep["instancesInDisjointPair"]
                                        or rep["instancesInAllDisjointPair"])
    ok = not rep["decisiveInconsistency"]
    rep["ok"] = ok
    return rep, ok


def clear_all(cfg, repo):
    sparql_update(cfg, "CLEAR ALL", repo=repo, timeout=300)


def clear_graph(cfg, graph_iri, repo=None):
    """只清**一个**命名图的三元组（保留该图本身）。绝不使用 CLEAR ALL 打 cc-ops。"""
    sparql_update(cfg, "CLEAR GRAPH <%s>" % graph_iri, repo=repo, timeout=300)
    return scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?s ?p ?o } }" % graph_iri,
                  repo=repo)


def t10_visibility_experiment(cfg, ttl_files, temp_repo, log):
    """★ 命名图 vs 默认图：在**空仓库**里做对照实验，用 T10 的原查询口径直接测。

    实验设计（每轮先 `CLEAR ALL`，保证无残留）：
      轮 A：7 个 TTL 装进**默认图**（不传 context）→ 跑 T10 的 `?s a owl:Class` / `owl:ObjectProperty`
      轮 B：7 个 TTL 装进**独立命名图** `<tbox>`（传 context）→ 跑同样的两条查询
    两轮结果相同 ⇒ 对 T10 探测无差别；不同 ⇒ 有差别。
    另附「声明落在默认图独占 / 命名图内」的来源拆分，作为可见性证据。
    """
    res = {"tempRepo": temp_repo, "tboxGraph": TBOX_GRAPH, "rounds": []}
    for label, ctx in (("A-default-graph", None), ("B-named-graph", TBOX_GRAPH)):
        clear_all(cfg, temp_repo)
        for f in ttl_files:
            upload_turtle(cfg, ctx, f, repo=temp_repo)
        vis = t10_gate_visibility(cfg, repo=temp_repo)
        vis["round"] = label
        vis["context"] = "<%s>" % (ctx or "") if ctx else "(默认图，不传 context)"
        vis["unionTotal"] = scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", repo=temp_repo)
        vis["namedGraphs"] = named_graphs(cfg, repo=temp_repo)
        vis["defaultGraphOnlyCount"] = scalar(
            cfg, "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o %s }" % NOT_IN_NAMED, repo=temp_repo)
        vis["inferredNonNamedCount"] = vis["defaultGraphOnlyCount"]
        res["rounds"].append(vis)
        log("  可见性对照 %s：context=%s total=%d 默认图独占=%d 命名图=%s "
            "非内建 owl:Class=%d owl:ObjectProperty=%d"
            % (label, vis["context"], vis["unionTotal"], vis["defaultGraphOnlyCount"],
               vis["namedGraphs"], len(vis["classesNonBuiltin"]),
               len(vis["objectPropertiesNonBuiltin"])))
    clear_all(cfg, temp_repo)
    a, b = res["rounds"][0], res["rounds"][1]
    res["namedClassesEqual"] = a["classesNonBuiltinNamed"] == b["classesNonBuiltinNamed"]
    res["namedPropsEqual"] = (a["objectPropertiesNonBuiltinNamed"]
                              == b["objectPropertiesNonBuiltinNamed"])
    res["identical"] = res["namedClassesEqual"] and res["namedPropsEqual"]
    na = len(a["classesNonBuiltinNamed"])
    pa = len(a["objectPropertiesNonBuiltinNamed"])
    res["verdict"] = (
        "对 T10 探测**无差别**：默认图与独立命名图两轮，T10 就绪门查询（`?s a owl:Class` / "
        "`?s a owl:ObjectProperty`，排除内建）返回**完全相同**的具名 IRI 集合 —— "
        "%d 个类 / %d 个对象属性（各另有 %d 个匿名类，bnode 标签每轮重生成，故按具名部分比较）"
        % (na, pa, a["classesNonBuiltinAnon"])
        if res["identical"] else
        "★ 对 T10 探测**有差别**：默认图 %d/%d vs 命名图 %d/%d（具名 IRI 集合不等）"
        % (na, pa, len(b["classesNonBuiltinNamed"]),
           len(b["objectPropertiesNonBuiltinNamed"])))
    log("  可见性对照结论：%s" % res["verdict"])
    return res


def temp_repo_precheck(cfg, ttl_files, pre, log):
    """★ 安全顺序第 2 步：先在临时仓库做一致性预检；通过后才允许写 cc-ops。"""
    res = {"tempRepo": None, "created": False, "deleted": False, "stages": [], "ok": False}
    before = repo_list(cfg)
    log("  预检前仓库列表：%s" % before)
    name = "%s%08x" % (TMP_REPO_PREFIX, int(time.time() * 1000) & 0xFFFFFFFF)
    res["tempRepo"] = name
    res["ruleset"] = cfg["ruleset"]
    log("  创建临时仓库 %s（ruleset=%s，与 cc-ops 同）..." % (name, cfg["ruleset"]))
    try:
        create_temp_repo(cfg, name)
        res["created"] = True
        after_create = repo_list(cfg)
        res["repositoriesAfterCreate"] = after_create
        if name not in after_create:
            raise Fail("临时仓库未出现在列表中：%s" % after_create)
        if not set(before).issubset(set(after_create)):
            raise Fail("创建临时仓库后既有仓库发生变化：%s -> %s" % (before, after_create))
        log("  仓库列表（创建后）：%s" % after_create)

        for f in ttl_files:
            upload_turtle(cfg, TBOX_GRAPH, f, repo=name)
        log("  7 个 TTL 已装入临时仓库的 tbox 图")

        rep1, ok1 = consistency_checks(cfg, name, "TBox-only")
        res["stages"].append(rep1)
        log("  预检①(TBox-only)：total=%d owl:Class(nb)=%d owl:ObjectProperty(nb)=%d "
            "disjointWith=%d AllDisjointClasses=%d"
            % (rep1["total"], rep1["owlClassNonBuiltin"], rep1["owlObjectPropertyNonBuiltin"],
               rep1["owlDisjointWithStatements"], rep1["owlAllDisjointClassesStatements"]))
        log("      owl:Nothing 的 subClassOf 条数=%d（OWL 2 RL `cls-nothing1` 蕴含，非矛盾；"
            "已从不可满足类判定中排除）⇒ 决定性矛盾项：selfDisjoint=%d 双属个体=%d "
            "Nothing 个体=%d 具名不可满足类=%d ⇒ %s"
            % (rep1["owlNothingSubClassOfCount"], len(rep1["selfDisjointClasses"]),
               len(rep1["instancesInDisjointPair"]), len(rep1["owlNothingInstances"]),
               len(rep1["unsatisfiableClasses"]), "OK" if ok1 else "★发现矛盾"))

        # 预检②：把 cc-ops 的 ABox 快照图原样复制进临时仓库（真实性更强的 TBox+ABox 预检）
        a = {"stage": "TBox+ABox-copy", "skipped": None}
        total_abox = sum(pre["namedGraphCounts"].get(g, 0) for g in pre["snapshotGraphs"])
        if total_abox > ABOX_PRECHECK_MAX_TRIPLES:
            a["skipped"] = "ABox 三元组数 %d 超过上限 %d" % (total_abox, ABOX_PRECHECK_MAX_TRIPLES)
        else:
            copied = {}
            for g in pre["snapshotGraphs"]:
                payload = sparql(cfg, "CONSTRUCT { ?s ?p ?o } WHERE { GRAPH <%s> { ?s ?p ?o } }" % g,
                                 accept="application/n-triples", timeout=300)
                upload_ntriples(cfg, g, payload, repo=name)
                copied[g] = scalar(cfg, "SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?s ?p ?o } }" % g,
                                   repo=name)
            a["copiedGraphCounts"] = copied
        if not a.get("skipped"):
            rep2, ok2 = consistency_checks(cfg, name, "TBox+ABox-copy")
            res["stages"].append(rep2)
            log("  预检②(TBox+ABox-copy)：total=%d 决定性矛盾项=%s ⇒ %s"
                % (rep2["total"], {
                    "instancesInDisjointPair": len(rep2["instancesInDisjointPair"]),
                    "instancesInAllDisjointPair": len(rep2["instancesInAllDisjointPair"]),
                    "owlNothingInstances": len(rep2["owlNothingInstances"]),
                    "unsatisfiableClasses": len(rep2["unsatisfiableClasses"]),
                }, "OK" if ok2 else "★发现矛盾"))
            ok1 = ok1 and ok2
        else:
            res["aboxPrecheckSkipped"] = a["skipped"]
            log("  预检②跳过：%s" % a["skipped"])
        res["aboxReplication"] = a
        res["ok"] = ok1
        # 预检③：命名图 vs 默认图的 T10 可见性对照实验（同一临时仓库，两轮均 CLEAR ALL 起手）
        res["t10Visibility"] = t10_visibility_experiment(cfg, ttl_files, name, log)
    finally:
        try:
            delete_temp_repo(cfg, name)
            res["deleted"] = True
        except Fail as exc:
            log("  ★ 删除临时仓库失败：%s" % exc)
        after_del = repo_list(cfg)
        res["repositoriesAfterDelete"] = after_del
        res["tempRepoGone"] = name not in after_del
        res["existingReposUntouched"] = set(before).issubset(set(after_del))
        log("  仓库列表（删除后）：%s" % after_del)
        log("  %s 已消失=%s；既有仓库未被触碰=%s"
            % (name, res["tempRepoGone"], res["existingReposUntouched"]))
        if res["deleted"] and not (res["tempRepoGone"] and res["existingReposUntouched"]):
            raise Fail("临时仓库清理核验失败：gone=%s untouched=%s"
                       % (res["tempRepoGone"], res["existingReposUntouched"]))
    if res["deleted"] and not res["tempRepoGone"]:
        raise Fail("临时仓库 %s 删除后仍在列表中" % name)
    return res


# --------------------------------------------------------------------------
# 证据物
# --------------------------------------------------------------------------
def write_json(path, doc):
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        fh.write("\n")
    return os.path.abspath(path)


def short_graph(g, prefix="https://credit-card-ops.ai/onto/graph/"):
    return g[len(prefix):] if g.startswith(prefix) else g


def side_by_side(cfg, pre, post):
    """before/after 并排对账表（返回文本行列表）。"""
    snaps_pre, _, _ = classify_graphs(cfg, pre["namedGraphs"])
    snaps_post, _, _ = classify_graphs(cfg, post["namedGraphs"])
    graphs = sorted(set(pre["namedGraphs"]) | set(post["namedGraphs"]))
    rows = []
    rows.append("%-58s %10s %10s %s" % ("命名图（省去公共前缀 https://credit-card-ops.ai/onto/graph/）",
                                        "before", "after", "判定"))
    rows.append("-" * 96)
    for g in graphs:
        b = pre["namedGraphCounts"].get(g, 0)
        a = post["namedGraphCounts"].get(g, 0)
        if g == TBOX_GRAPH:
            verdict = "tbox 图（本次新增/幂等）" if b == 0 else "tbox 图（重置式重载）"
        else:
            verdict = "OK" if b == a else "★变化"
        rows.append("%-58s %10d %10d %s" % (short_graph(g), b, a, verdict))
    rows.append("-" * 96)
    for key, title in (("namedGraphSum", "命名图逐图之和（含跨图重复）"),
                       ("namedGraphDistinctCount", "命名图并集（去重）"),
                       ("defaultGraphOnlyCount", "默认图独有（不在任何命名图内）"),
                       ("unionTotal", "并集总计（无 GRAPH 子句 = T10 所见）")):
        b, a = pre.get(key, 0), post.get(key, 0)
        ok = "OK" if b == a else ("↑装载后新增" if key in ("unionTotal",) else "★变化")
        rows.append("%-58s %10d %10d %s" % (title, b, a, ok))
    rows.append("-" * 96)
    for key, title in (("owlClassAll", "owl:Class（含内建）"),
                       ("owlClassNonBuiltin", "owl:Class（非内建）"),
                       ("owlObjectPropertyAll", "owl:ObjectProperty（含内建）"),
                       ("owlObjectPropertyNonBuiltin", "owl:ObjectProperty（非内建）"),
                       ("rdfsClassAll", "rdfs:Class"),
                       ("rdfPropertyAll", "rdf:Property"),
                       ("tboxNsTriplesBasePrefix",
                        "TBox 命名空间条数（裸 base %s）" % cfg["onto_base"]),
                       ("tboxNsTriplesT10Prefixes", "TBox 命名空间条数（T10 的 8 个前缀并集）"),
                       ("tboxGraphTriples", "tbox 图三元组数")):
        rows.append("%-58s %10s %10s" % (title, pre.get(key, "-"), post.get(key, "-")))
    extra = []
    for g in graphs:
        if g == TBOX_GRAPH:
            continue
        b, a = pre["namedGraphCounts"].get(g, 0), post["namedGraphCounts"].get(g, 0)
        if b != a:
            extra.append((g, b, a))
    rows.append("-" * 96)
    rows.append("快照图逐图不变：%s（%d 个快照图）"
                % ("是" if not extra else "否 → %s" % extra, len(snaps_post)))
    return rows


# --------------------------------------------------------------------------
# 三个模式
# --------------------------------------------------------------------------
def mode_probe(cfg, args, log):
    pre = probe(cfg, "probe")
    snaps, mine, other = classify_graphs(cfg, pre["namedGraphs"])
    print("  ① 仓库可达性     : %s（%s）" % (pre["reachable"], pre["base"]))
    print("     仓库列表      : %s" % pre["repositories"])
    print("     目标仓库      : %s（ruleset=%s）" % (pre["repository"], pre["ruleset"]))
    print("     命名图模板    : %s（运行时展开）" % pre["namedGraphTemplate"])
    print("  ② 并集总三元组数 : %d（无 GRAPH 子句 = T10 所见）" % pre["unionTotal"])
    print("       = 命名图并集（去重）%d + 默认图独有 %d；命名图逐图之和（含跨图重复）%d"
          % (pre["namedGraphDistinctCount"], pre["defaultGraphOnlyCount"], pre["namedGraphSum"]))
    print("  ③ 逐命名图三元组数（%d 个）：" % len(pre["namedGraphs"]))
    for g in pre["namedGraphs"]:
        tag = "  ← 本工具的 tbox 图" if g == TBOX_GRAPH else (
            "  ← 快照图 (%s)" % snaps.get(g) if g in snaps else "  ← ★非快照图")
        print("       %-64s %8d%s" % (g, pre["namedGraphCounts"][g], tag))
    print("       默认图独有（不在任何命名图内，实测）：%d" % pre["defaultGraphOnlyCount"])
    if other:
        print("       ★ 非快照、非 tbox 的命名图：%s" % other)
    print("  ④ 声明计数（全仓、无图限定，即 T10 所见；括号内为默认图独占数）：")
    for key, title in (("owlClassAll", "owl:Class 全部"), ("owlClassNonBuiltin", "owl:Class 非内建"),
                       ("owlObjectPropertyAll", "owl:ObjectProperty 全部"),
                       ("owlObjectPropertyNonBuiltin", "owl:ObjectProperty 非内建"),
                       ("rdfsClassAll", "rdfs:Class 全部"),
                       ("rdfPropertyAll", "rdf:Property 全部")):
        print("       %-32s %6d" % (title, pre[key]))
    for name, split in sorted(pre["declarationDetail"]["sourceSplit"].items()):
        print("       %-32s 命名图内=%d  默认图独占=%d"
              % (name + "（来源拆分）", split["inNamedGraphs"], split["defaultGraphOnly"]))
    print("  ⑤ TBox 命名空间条数：裸 base `%s` = %d" % (cfg["onto_base"],
                                                      pre["tboxNsTriplesBasePrefix"]))
    print("       T10 的 8 个前缀并集 = %d（其中命名图内 %d、默认图独占 %d）"
          % (pre["tboxNsTriplesT10Prefixes"], pre["tboxNsTriplesT10PrefixesInNamed"],
             pre["tboxNsTriplesT10PrefixesDefaultOnly"]))
    print("     T10 就绪门（非内建 owl:Class ∧ 非内建 owl:ObjectProperty）：%s"
          % ("通过" if pre["tboxReadyGatePass"] else "未通过"))
    print("     本次探测未写入任何内容（只读）。")
    path = write_json(args.out or os.path.join(TMP_DIR, "m2_15_probe.json"),
                      {"mode": "probe", "probe": pre,
                       "classification": {"snapshotGraphs": sorted(snaps),
                                          "tboxGraphs": mine, "otherGraphs": other}})
    print("  证据：%s" % path)
    return 0


def mode_rollback(cfg, args, log):
    pre = probe(cfg, "rollback-before")
    recompute = {"snapshotGraphs": None}
    recompute["snapshotGraphs"] = sorted(classify_graphs(cfg, pre["namedGraphs"])[0])
    pre["snapshotGraphs"] = recompute["snapshotGraphs"]
    if TBOX_GRAPH not in pre["namedGraphs"]:
        print("  tbox 图 %s 不存在 ⇒ 已处于装载前状态，无需操作（未写入任何内容）" % TBOX_GRAPH)
        path = write_json(args.out or os.path.join(TMP_DIR, "m2_15_rollback.json"),
                          {"mode": "rollback", "noop": True, "before": pre})
        print("  证据：%s" % path)
        print("RESULT: PASS")
        return 0
    print("  删除前 tbox 图三元组数：%d" % pre["namedGraphCounts"].get(TBOX_GRAPH, 0))
    print("  执行 CLEAR GRAPH <%s>（只删该图，不触碰任何快照图）..." % TBOX_GRAPH)
    sparql_update(cfg, "CLEAR GRAPH <%s>" % TBOX_GRAPH)
    post = probe(cfg, "rollback-after")
    bad = [(g, pre["namedGraphCounts"].get(g, 0), post["namedGraphCounts"].get(g, 0))
           for g in pre["snapshotGraphs"]
           if pre["namedGraphCounts"].get(g, 0) != post["namedGraphCounts"].get(g, 0)]
    if TBOX_GRAPH in post["namedGraphs"]:
        raise Fail("rollback 后 tbox 图仍存在")
    if bad:
        raise Fail("rollback 改动了快照图（绝不允许）：%s" % bad)
    print("  快照图逐个不变：%d/%d 个" % (len(pre["snapshotGraphs"]), len(pre["snapshotGraphs"])))
    for line in side_by_side(cfg, pre, post):
        print("  " + line)
    path = write_json(args.out or os.path.join(TMP_DIR, "m2_15_rollback.json"),
                      {"mode": "rollback", "noop": False, "before": pre, "after": post,
                       "snapshotGraphsUnchanged": True})
    print("  证据：%s" % path)
    print("RESULT: PASS")
    return 0


def mode_persist(cfg, args, log):
    ttl_files, excluded = discover_turtle(args.ontology_dir)
    expected = len(ttl_files)
    print("  装配清单（%d 个）：%s" % (expected, [os.path.basename(f) for f in ttl_files]))
    print("  显式排除        ：%s（SHACL 形状属 T11，见文件头依据）"
          % (excluded or "scheme2-onto/shapes/shacl-constraints.ttl（未找到）"))

    # ① 基线探测
    pre = probe(cfg, "before")
    pre["snapshotGraphs"] = sorted(classify_graphs(cfg, pre["namedGraphs"])[0])
    print("  ① 基线探测完成：并集 %d 条（命名图并集 %d + 默认图独有 %d）、命名图 %d 个、tbox 图 %s"
          % (pre["unionTotal"], pre["namedGraphDistinctCount"], pre["defaultGraphOnlyCount"],
             len(pre["namedGraphs"]),
             "已存在" if TBOX_GRAPH in pre["namedGraphs"] else "不存在"))
    snaps, mine, other = classify_graphs(cfg, pre["namedGraphs"])
    assert_tbox_graph_disjoint(cfg, pre["namedGraphs"], snaps)
    snaps2, mine2 = baseline_is_safe(cfg, pre)
    print("  ★ 命名图模板运行时展开断言：template(`%s`) == 实测图 == `%s`（%d 个快照图）"
          % (cfg["template"], sorted(snaps2)[0], len(snaps2)))
    print("  ★ tbox 图 `%s` 与全部 %d 个快照图 IRI 都不相等；`tbox` 不匹配 snapshotId 口径"
          "（%s，取自 shared/model/ccf.schema.json）" % (TBOX_GRAPH, len(snaps2), SNAPSHOT_ID_RE.pattern))
    before_path = write_json(args.before_out or os.path.join(TMP_DIR, "m2_15_before.json"),
                             {"mode": "persist-before", "probe": pre,
                              "assembly": [os.path.basename(f) for f in ttl_files],
                              "excluded": os.path.basename(excluded) if excluded else None})
    print("  基线证据：%s" % before_path)

    # ② 临时仓库一致性预检（不通过 ⇒ 立即停止，不碰 cc-ops）
    precheck = temp_repo_precheck(cfg, ttl_files, pre, log)
    if not (precheck["created"] and precheck["ok"] and precheck["deleted"] and precheck["tempRepoGone"]):
        raise Fail("临时仓库一致性预检未通过（created=%s ok=%s deleted=%s gone=%s）⇒ 不写 cc-ops"
                   % (precheck["created"], precheck["ok"], precheck["deleted"],
                      precheck["tempRepoGone"]))
    print("  ② 预检通过，临时仓库已删除且既有仓库未被触碰。")

    # ③ 装载（两轮，用第二轮证明幂等）
    #
    # ★ 为什么必须「先 CLEAR GRAPH tbox 再上传」，而不是「重复上传」：
    #   7 个 TTL 里有 17 个**匿名 blank node 类**（`owl:Class [ ... ]` 形式的 restriction）。
    #   Turtle 的 blank node 标签不是稳定标识符 —— 每次解析/上传都会生成**新的** bnode id，
    #   于是「同一份 TTL 传第二次」会**新增** 339 条三元组（4033 → 4372，实测），
    #   并让 owl2-rl 的推断三元组随之再涨一轮。即：GraphDB 层面对含 bnode 的 RDF 而言，
    #   "重复 POST 同一份 turtle" **本身就不是幂等操作**，这是 RDF 语义的事实，不是缺陷。
    #   真正幂等的装载语义 = 「把目标图重置为这份 TTL 的内容」：
    #       CLEAR GRAPH <tbox>  →  上传 7 个 TTL
    #   这样第 N 次执行的结果恒等于第 1 次（图内 4033 条）。只清 tbox 图，绝不触碰快照图。
    rounds = []
    for rnd in (1, 2):
        cleared = clear_graph(cfg, TBOX_GRAPH)
        for f in ttl_files:
            upload_turtle(cfg, TBOX_GRAPH, f)
        rounds.append(probe(cfg, "after-round%d" % rnd))
        print("  ③ 第 %d 轮（CLEAR GRAPH tbox → 上传，cleared=%s）：tbox 图 %d 条、并集 %d 条"
              % (rnd, cleared, rounds[-1]["namedGraphCounts"].get(TBOX_GRAPH, 0),
                 rounds[-1]["unionTotal"]))
    post = rounds[-1]
    idem = (rounds[0]["namedGraphCounts"].get(TBOX_GRAPH, 0)
            == rounds[1]["namedGraphCounts"].get(TBOX_GRAPH, 0)
            and rounds[0]["unionTotal"] == rounds[1]["unionTotal"])
    print("  ③ 幂等性（重置式装载：两轮后 tbox 图三元组数/并集总数相等）：%s（%d → %d）"
          % (idem, rounds[0]["namedGraphCounts"].get(TBOX_GRAPH, 0),
             rounds[1]["namedGraphCounts"].get(TBOX_GRAPH, 0)))

    # ④ 对账：6 个快照图逐图不变
    changed = [(g, pre["namedGraphCounts"].get(g, 0), post["namedGraphCounts"].get(g, 0))
               for g in pre["snapshotGraphs"]
               if pre["namedGraphCounts"].get(g, 0) != post["namedGraphCounts"].get(g, 0)]
    if not post["tboxReadyGatePass"]:
        raise Fail("装载后 T10 就绪门仍未通过（非内建 owl:Class=%d / owl:ObjectProperty=%d）"
                   % (post["owlClassNonBuiltin"], post["owlObjectPropertyNonBuiltin"]))
    print("  ④ T10 就绪门（非内建 owl:Class=%d ∧ owl:ObjectProperty=%d）：通过"
          % (post["owlClassNonBuiltin"], post["owlObjectPropertyNonBuiltin"]))
    if changed:
        print("  ★★ 警告：快照图逐图三元组数发生变化（parity G14 走 FROM <快照图> 作用域，"
              "必须复核）：%s" % changed)
    else:
        print("  ★ %d 个快照图逐图三元组数与装载前逐一相同（实测，非推理）。" % len(snaps))

    print("\n  【before / after 并排对账】")
    for line in side_by_side(cfg, pre, post):
        print("  " + line)

    after_path = write_json(args.after_out or os.path.join(TMP_DIR, "m2_15_after.json"),
                            {"mode": "persist-after", "probe": post, "beforeProbe": pre,
                             "rounds": rounds, "precheck": precheck,
                             "snapshotGraphsUnchanged": not changed,
                             "idempotent": idem})
    print("\n  装载后证据：%s" % after_path)
    if changed:
        raise Fail("装载改变了快照图的逐图三元组数 ⇒ 不接受（需人工裁决）：%s" % changed)
    # ★ 落地校验：声明的个体数必须 == 目标命名图里的实际数（见 verify_landing 的动机）
    _ok, _rows, _bad = verify_landing(cfg, ttl_files, TBOX_GRAPH)
    print("\n  【落地校验】声明个体数 vs 目标命名图实际数（%s）"
          "｜项目自有类须相等，owl/rdf 推断类容许图中更多" % TBOX_GRAPH)
    for _cls, _n, _got, _p in _rows:
        print("    %-28s 声明 %-5d 图中 %-5d %s" % (_cls, _n, _got, "OK" if _p else "★不符"))
    if not _ok:
        raise Fail("落地校验不通过：声明的个体数与目标命名图不一致 %s "
                   "⇒ 本次装载未真正落地，拒绝 PASS。" % _bad)
    print("RESULT: PASS")
    return 0


MODES = {"probe": mode_probe, "persist": mode_persist, "rollback": mode_rollback}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="load_tbox",
        description="方案2 TBox 装载入 GraphDB（cc-ops 的独立命名图）；probe 只读 / "
                    "persist 幂等装载 / rollback 只删 tbox 图。FAIL-CLOSED：异常 ⇒ exit 1")
    ap.add_argument("mode", nargs="?", default="probe", choices=sorted(MODES))
    ap.add_argument("--ontology-dir", default=ONTOLOGY_DIR_DEFAULT,
                    help="装配目录（默认 scheme2-onto/ontology，只取 cc-*.ttl）")
    ap.add_argument("--targets", default=TARGETS_DEFAULT)
    ap.add_argument("--type-map", default=TYPEMAP_DEFAULT)
    ap.add_argument("--out", default=None, help="探测/往返 JSON 输出路径")
    ap.add_argument("--before-out", default=None, help="persist 的 before JSON 路径")
    ap.add_argument("--after-out", default=None, help="persist 的 after JSON 路径")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg)

    try:
        cfg = load_config(args)
        rc = MODES[args.mode](cfg, args, log)
        return rc or 0
    except Fail as exc:
        print("\n★ 失败：%s" % exc)
        print("RESULT: FAIL")
        return 1
    except Exception as exc:                            # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\n★ 意外异常：%s: %s" % (type(exc).__name__, exc))
        print("RESULT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
