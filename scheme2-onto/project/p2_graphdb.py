# -*- coding: utf-8 -*-
"""M1.5 · **P2 投影器：CCF → GraphDB（RDF / OWL / SPARQL）**。

契约（`docs\\01` §1.1、`docs\\00` §6.3、裁决 13）：
  * 输入 = **同一份 CCF 快照**（与 P1 逐字节同源，保证 C1 单一输入）
  * 载体名**只从 `shared/mapping/*.yaml` 读**，本文件不得硬编码 `cc:` 字面量
  * 只投影 `relations[]`；4 条 `inverse_relations` 不显式断言（OWL 侧由
    `owl:inverseOf` 声明，留给 M2 的 TBox；本阶段两侧都不建边，便于逐项对账）
  * `status != collected` 落成 `ccm:notCollected` / `ccm:collectedEmpty` 两个**多值
    数据属性**；**空清单不落三元组**（与 P1「空清单不落属性」一致）
  * 隔离：专属仓库 `cc-ops` + **每快照一个命名图**（裁决 13）；绝不写入既有
    `gdo*` / `starwars` 仓库，也不改 `/rest/security`

用法：
  python scheme2-onto/project/p2_graphdb.py --ccf tmp/ccf_live1.json \\
      --report tmp/p2_report_live.json [--wipe] [--dry-run]
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "shared", "project"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ccf_plan as cp  # noqa: E402

SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def iri_segment(text):
    """把中性 id 段转成 IRI 安全段（逐字符百分号编码，规则确定、可复现）。"""
    return "".join(c if c in SAFE else "".join("%%%02X" % b for b in c.encode("utf-8"))
                   for c in text)


def iri_for(template, ident, sep_from, sep_to):
    """★ 分隔符必须**先按段切开再逐段编码**：直接 replace 后整体编码会把 `/` 变成 `%2F`，
    毁掉 IRI 的路径结构。"""
    parts = ident.split(sep_from)
    return template.format(id=sep_to.join(iri_segment(p) for p in parts))


def ttl_string(text):
    out = text.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"%s"' % out


def ttl_literal(value):
    if isinstance(value, bool):
        return '"%s"^^xsd:boolean' % ("true" if value else "false")
    if isinstance(value, int):
        return '"%d"^^xsd:integer' % value
    if isinstance(value, float):
        return '"%r"^^xsd:decimal' % value
    return ttl_string(str(value))


def ttl_literal_xsd(value, xsd):
    """按映射表声明的 xsd 输出字面量（M1.5c 证据属性用）。

    值已在 `shared/project/ccf_plan.py` 侧按 xsd 收敛（xsd:string ⇒ 字符串化），
    这里只负责字面量与类型标注的拼装，不重复推断类型。
    """
    out = str(value).replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"%s"^^%s' % (out, xsd) if xsd else ttl_string(str(value))


class GraphDB(object):
    def __init__(self, base, repository, timeout=120):
        self.base = base.rstrip("/")
        self.repo = repository
        self.timeout = timeout

    def _req(self, url, data=None, method=None, headers=None):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def repository_exists(self):
        code, body = self._req("%s/rest/repositories" % self.base,
                               headers={"Accept": "application/json"})
        if code != 200:
            raise RuntimeError("GET /rest/repositories -> HTTP %s: %s"
                               % (code, body[:300].decode("utf-8", "replace")))
        return any(r.get("id") == self.repo for r in json.loads(body.decode("utf-8")))

    def create_repository(self, title, base_iri):
        cfg = (
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "@prefix rep: <http://www.openrdf.org/config/repository#> .\n"
            "@prefix sr: <http://www.openrdf.org/config/repository/sail#> .\n"
            "@prefix sail: <http://www.openrdf.org/config/sail#> .\n"
            "@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .\n"
            "\n"
            "[] a rep:Repository ;\n"
            '   rep:repositoryID "%s" ;\n'
            '   rdfs:label "%s" ;\n'
            "   rep:repositoryImpl [\n"
            '     rep:repositoryType "graphdb:SailRepository" ;\n'
            "     sr:sailImpl [\n"
            '       sail:sailType "graphdb:Sail" ;\n'
            '       graphdb:ruleset "owl2-rl" ;\n'
            '       graphdb:base-URL "<BASE>"\n'
            "     ]\n"
            "   ] .\n" % (self.repo, title))
        cfg = cfg.replace("<BASE>", base_iri)
        boundary = "----ccopsm15boundary"
        # ★ 必须带 filename：GraphDB 用 Spring 的 `@RequestPart MultipartFile config`
        #   绑定，纯文本部件没有 filename 时绑定失败，报
        #   `Required request part 'config' is not present`（实测 500）。
        body = (
            ("--%s\r\n" % boundary).encode()
            + b'Content-Disposition: form-data; name="config"; '
              b'filename="repo-config.ttl"\r\n'
            + b"Content-Type: text/turtle\r\n\r\n"
            + cfg.encode("utf-8")
            + ("\r\n--%s--\r\n" % boundary).encode())
        code, resp = self._req(
            "%s/rest/repositories" % self.base, data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
        return code, resp.decode("utf-8", "replace")[:400]

    def sparql_update(self, update):
        code, resp = self._req(
            "%s/repositories/%s/statements" % (self.base, urllib.parse.quote(self.repo)),
            data=update.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/sparql-update"})
        if code not in (200, 204):
            raise RuntimeError("SPARQL UPDATE -> HTTP %s: %s"
                               % (code, resp[:400].decode("utf-8", "replace")))

    def upload_ttl(self, ttl, graph_iri):
        # ★ `context` 必须是合法的 N-Triples 资源 ⇒ 尖括号不能省，否则
        #   HTTP 400 `Invalid value for parameter 'context': Not a legal N-Triples resource`
        url = "%s/repositories/%s/statements?context=%s" % (
            self.base, urllib.parse.quote(self.repo),
            urllib.parse.quote("<%s>" % graph_iri, safe=""))
        code, resp = self._req(url, data=ttl.encode("utf-8"), method="POST",
                               headers={"Content-Type": "application/x-turtle"})
        if code not in (200, 204):
            raise RuntimeError("上传 Turtle -> HTTP %s: %s"
                               % (code, resp[:400].decode("utf-8", "replace")))
        return code

    def select(self, select_clause, where_fragment, graph_iri=None, group_by=None):
        """`select_clause` 与 `where_fragment` 分开传：不要用「拼一个 `}` 再补 RETURN」
        的写法，那样会生成 `SELECT * FROM <g> WHERE { ... } RETURN ...}` 的畸形查询。
        ★ 聚合时必须显式 `GROUP BY`，否则 GraphDB 报
        `variable 'm' in projection not present in GROUP BY`。"""
        query = "SELECT %s%s WHERE { %s }%s" % (
            select_clause,
            " FROM <%s>" % graph_iri if graph_iri else "",
            where_fragment,
            " GROUP BY %s" % group_by if group_by else "")
        url = "%s/repositories/%s" % (self.base, urllib.parse.quote(self.repo))
        data = urllib.parse.urlencode({"query": query}).encode("utf-8")
        code, body = self._req(url, data=data, method="POST", headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded"})
        if code != 200:
            raise RuntimeError("SPARQL SELECT -> HTTP %s: %s"
                               % (code, body[:400].decode("utf-8", "replace")))
        return json.loads(body.decode("utf-8"))["results"]["bindings"]


def build_ttl(plan, maps, graph_iri):
    """生成 Turtle 文本（按对象分组，关系独立成块）。"""
    ns = maps.ns["onto"]
    ccf_id = maps.common["onto"]["instance_id"]
    snap_p = maps.common["onto"]["snapshot"]
    layer_p = maps.common["onto"]["layer"]
    type_p = maps.common["onto"]["neutral_type"]
    nc_p = maps.obs_policy["onto"]["not_collected"]
    ce_p = maps.obs_policy["onto"]["collected_empty"]

    L = []
    a = L.append
    a("# 由 scheme2-onto/project/p2_graphdb.py 生成 —— 请勿手工编辑")
    a("# snapshotId: %s" % plan.ccf.snapshot_id)
    a("# 命名图: %s" % graph_iri)
    a("")
    # ★ prologue 的前缀名与 IRI 逐字来自 `type-map.yaml#prefixes`，本文件不硬编码载体名
    for _p in sorted(maps.prefixes):
        a("@prefix %s: <%s> ." % (_p, maps.prefixes[_p]))
    a("")

    iri_of = {}
    for n in sorted(plan.nodes, key=lambda x: x.id):
        iri = iri_for(ns["instance_iri_template"], n.id,
                      ns["id_separator_from"], ns["id_separator"])
        iri_of[n.id] = iri
        lines = ["a %s" % maps.onto_class(n.type),
                 "%s %s" % (ccf_id, ttl_string(n.id)),
                 "%s %s" % (type_p, ttl_string(n.type)),
                 "%s %s" % (layer_p, ttl_string(n.layer or "")),
                 "%s %s" % (snap_p, ttl_string(plan.ccf.snapshot_id))]
        attrs = maps.attrs(n.type)
        merged = dict(n.key)
        merged.update(n.attrs)
        for k in sorted(merged):
            spec = attrs.get(k)
            if spec is None:
                continue
            lines.append("%s %s" % (spec["onto_property"], ttl_literal(merged[k])))
        for code, value, _unit, _oid in sorted(n.values):
            lines.append("%s %s" % (maps.metric(code)["onto_property"], ttl_literal(value)))
        for code in sorted(n.not_collected):
            lines.append("%s %s" % (nc_p, ttl_string(code)))
        for code in sorted(n.collected_empty):
            lines.append("%s %s" % (ce_p, ttl_string(code)))
        a("<%s>" % iri)
        for i, ln in enumerate(lines):
            a("  %s %s" % (ln, "." if i == len(lines) - 1 else ";"))
        a("")

    a("# ---- 关系（只投影 relations[]，4 条反向关系不显式断言）----")
    for e in sorted(plan.edges, key=lambda x: x.id):
        spec = maps.relation(e.type)
        a("<%s> %s <%s> ." % (iri_of[e.from_id], spec["onto_property"], iri_of[e.to_id]))
    a("")

    # ★ M1.5c：派生证据投影（每条 CCF 观测一个证据节点 + 对象→证据 边，粒度 = U-Q 裁定 A）。
    #   与 Neo4j 侧**对称**：同一条 CCF 记录 id、同一组属性、同一条边。
    #   annotation 型 layer 为 null ⇒ **不写** layer 三元组（写了就与 KG 侧不同形）。
    ev_type = maps.evidence_type()
    ev_rel = maps.evidence_relation()
    ev_class = maps.onto_class(ev_type)
    ev_prop = maps.relation(ev_rel)["onto_property"]
    ev_attrs = maps.attrs(ev_type)
    ev_iri_of = {}
    for ev in sorted(plan.evidence, key=lambda x: x["recordId"]):
        iri = iri_for(ns["instance_iri_template"], ev["recordId"],
                      ns["id_separator_from"], ns["id_separator"])
        ev_iri_of[ev["recordId"]] = iri
        lines = ["a %s" % ev_class,
                 "%s %s" % (ccf_id, ttl_string(ev["recordId"])),
                 "%s %s" % (type_p, ttl_string(ev_type)),
                 "%s %s" % (snap_p, ttl_string(plan.ccf.snapshot_id))]
        for k in sorted(ev["props"]):
            spec = ev_attrs.get(k)
            if spec is None:
                continue
            lines.append("%s %s" % (spec["onto_property"],
                                    ttl_literal_xsd(ev["props"][k], spec.get("xsd"))))
        a("<%s>" % iri)
        for i, ln in enumerate(lines):
            a("  %s %s" % (ln, "." if i == len(lines) - 1 else ";"))
        a("")
    a("# ---- 派生证据关系（运维对象 -> 证据，逐条溯源到 CCF 记录 id）----")
    for ev in sorted(plan.evidence_edges, key=lambda x: x["recordId"]):
        owner = iri_of.get(ev["objectId"])
        if owner is None:
            continue
        a("<%s> %s <%s> ." % (owner, ev_prop, ev_iri_of[ev["recordId"]]))
    a("")
    return "\n".join(L), iri_of


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccf", required=True)
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"))
    ap.add_argument("--targets", default=os.path.join(ROOT, "shared", "deploy", "targets.yaml"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--repository", default=None)
    ap.add_argument("--report", default=None)
    ap.add_argument("--ttl-out", default=None)
    ap.add_argument("--wipe", action="store_true", help="只清空本快照的命名图")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    targets = read_yaml(args.targets)
    gdb = targets["graphdb"]
    url = args.url or gdb["url"]
    repo = args.repository or gdb["isolation"]["repository"]
    isolate = gdb["isolation"]

    plan = cp.build_plan(args.ccf, args.mapping_dir, args.model_dir)
    plan.print_issues()
    if plan.errors():
        sys.exit("★ CCF 未通过校验（%d ERROR），拒绝投影" % len(plan.errors()))
    maps = plan.maps

    graph_iri = isolate["named_graph_template"].format(snapshotId=plan.ccf.snapshot_id)
    ttl, iri_of = build_ttl(plan, maps, graph_iri)

    expect = {
        "objects": len(plan.nodes),
        "relations": len(plan.edges),
        "observationsCollected": sum(len(n.values) for n in plan.nodes),
        "observationsNotCollected": sum(len(n.not_collected) for n in plan.nodes),
        "observationsCollectedEmpty": sum(len(n.collected_empty) for n in plan.nodes),
        # ★ M1.5c：派生证据也进期望数（回读侧按 concrete_types / rel_map 自动覆盖）。
        "evidence": len(plan.evidence),
        "evidenceEdges": len(plan.evidence_edges),
        "metricValueCounts": {},
        "notCollectedCounts": {},
        "collectedEmptyCounts": {},
    }
    for n in plan.nodes:
        for code, _v, _u, _o in n.values:
            expect["metricValueCounts"][code] = expect["metricValueCounts"].get(code, 0) + 1
        for code in n.not_collected:
            expect["notCollectedCounts"][code] = expect["notCollectedCounts"].get(code, 0) + 1
        for code in n.collected_empty:
            expect["collectedEmptyCounts"][code] = expect["collectedEmptyCounts"].get(code, 0) + 1

    n_triples = ttl.count(" ;\n") + ttl.count(" .\n")

    if args.ttl_out:
        with io.open(args.ttl_out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ttl)
        print("Turtle 写出：%s（%d B）" % (args.ttl_out, len(ttl.encode("utf-8"))))

    summary = {"url": url, "repository": repo, "graphIri": graph_iri,
               "expect": {k: v for k, v in expect.items() if not k.endswith("Counts")},
               "ttlBytes": len(ttl.encode("utf-8"))}
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print(ttl[:1500])
        return 0

    api = GraphDB(url, repo)
    if not api.repository_exists():
        code, msg = api.create_repository("Credit Card Ops (方案2 ONTO · M1.5)",
                                          maps.ns["onto"]["base"])
        if code not in (201, 200, 204):
            sys.exit("★ 建仓失败 HTTP %s: %s" % (code, msg))
        print("已新建专属仓库 %s（ruleset=owl2-rl）" % repo)
    else:
        print("复用已有专属仓库 %s" % repo)

    if args.wipe:
        api.sparql_update("CLEAR GRAPH <%s>" % graph_iri)
        print("wipe：清空命名图 %s（其他仓库与命名图未触碰）" % graph_iri)

    api.upload_ttl(ttl, graph_iri)
    print("已上传 Turtle（%d B）到命名图 %s" % (len(ttl.encode("utf-8")), graph_iri))

    actual = {"byType": {}, "byRelation": {}, "triples": 0,
              "metricValueCounts": {}, "notCollectedCounts": {}, "collectedEmptyCounts": {}}
    for type_code in maps.concrete_types():
        rows = api.select("(COUNT(DISTINCT ?s) AS ?c)",
                          "?s a <%s> ." % maps.expand(maps.onto_class(type_code)), graph_iri)
        actual["byType"][type_code] = int(rows[0]["c"]["value"]) if rows else 0
    for rel_code in sorted(maps.rel_map["relations"]):
        prop = maps.expand(maps.relation(rel_code)["onto_property"])
        rows = api.select("(COUNT(*) AS ?c)", "?a <%s> ?b ." % prop, graph_iri)
        actual["byRelation"][rel_code] = int(rows[0]["c"]["value"]) if rows else 0
    rows = api.select("(COUNT(*) AS ?c)", "?s ?p ?o .", graph_iri)
    actual["triples"] = int(rows[0]["c"]["value"]) if rows else 0
    for code in sorted(expect["metricValueCounts"]):
        # ★ 口径由 shared/project/ccf_plan.py 唯一定义：按 (对象类型, 属性) 类限定。
        #   本仓库虽是专属仓库，但共享 `ccm:` 属性跨类复用（实测 `ccm:cpuUsageRatio`
        #   由 Container / GaussDBInstance / Host 三个指标码共用），
        #   不加类限定会把三个码串成同一个数。
        rows = api.select("(COUNT(*) AS ?c)", maps.onto_metric_count_clause(code),
                          graph_iri)
        actual["metricValueCounts"][code] = int(rows[0]["c"]["value"]) if rows else 0
    for key, prop in (("notCollectedCounts", maps.obs_policy["onto"]["not_collected"]),
                      ("collectedEmptyCounts", maps.obs_policy["onto"]["collected_empty"])):
        rows = api.select("?m (COUNT(*) AS ?c)", maps.onto_scope_clause(prop), graph_iri,
                          group_by="?m")
        for r in rows:
            actual[key][r["m"]["value"]] = int(r["c"]["value"])

    report = {
        "carrier": "graphdb",
        "url": url,
        "repository": repo,
        "graphIri": graph_iri,
        "snapshotId": plan.ccf.snapshot_id,
        "ccfPath": plan.ccf.path,
        "expect": expect,
        "actual": actual,
        "elapsedSec": round(time.time() - t0, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.report:
        with io.open(args.report, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1)
        print("报告写出：%s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
