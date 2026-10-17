#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G19 · **图级快照身份一致性门禁**（只读；退出码 0 PASS / 1 ERROR / 2 BLOCKED=目标不可达 或 参数错误）。

## 为什么需要它（M2.12 实测缺陷）
KG 载体（Neo4j）的图级元数据 `:CC_Meta` 曾与全图 ABox **不是同一份快照**：
  * `MATCH (m:CC_Meta) RETURN properties(m)` ⇒ `snapshotId = 'ccf-20260928T134833Z-eb634e'`（M1.6 冻结值）；
  * 而全图 ABox（258 节点 / 225 关系）的 `ccfSnapshotId` **全部** = `'ccf-20260928T144216Z-b920b5'`。
根因：`scheme1-kg/ontology/01-schema.cypher` 把具体快照 id 写死成 `SET m.snapshotId = '…'`，
ABox 重投（live3）不刷新它，而每次重装 TBox 又会把它**写回去** ⇒ 图级元数据说谎。
`scheme1-kg/service/topology_reader.py` 把 `:CC_Meta.snapshotId` 当快照 id 权威
（`fetch_graph_meta()` / `census()` / `get_facts().extra.snapshotSource`），于是下游拿到错快照，
越过 `docs/05` §5.1「同一 snapshotId」与硬约束 C1/C5。既有 A10 自测只做 `CC_Meta` **自比对**
（`facts.meta()["snapshotId"] == census["snapshotId"]`），从不与 ABox 比对 ⇒ 一路 PASS。

## 为什么有第二版（M2.12 独立对抗性复核 = needs_revision，4 条真绕过）
第一版（22412 B）KG 半与 ONTO 半**完全独立**，且下限口径可被命令行关闭。复核者给出 4 条真绕过，
本版逐条封堵（`tmp/m212_verify_g19_bypass.py` 是**修复前**的绕过证据，`tmp/g19_bypass_counterexample.py`
是修复后的反例）：
  1. **跨载体身份从不比对** ⇒ 新增 **O4**（当前快照图必须存在）与命名图归类
     （current / history 白名单 / unclassified）+ `--allow-history` / `--strict`；
  2. **反恒真下限是全局和** ⇒ 拆成 **分侧下限** A1（合计）+ A2（KG 节点）+ A3（KG 关系）+ A4（ONTO 主语），
     并把 `labelledNodes` 作为 KG 半分母纳入判据；
  3. **下限可被参数关闭 + `--selftest` 结构盲区** ⇒ `main` **一律以 cfg 值为准**（消除双口径，
     `load_cfg` 不再用 `or` 吞掉 0），任一 `<= 0` ⇒ **参数错误（exit 2，无 PASS 输出）**，
     `--selftest` 新增覆盖该路径的断言；
  4. **K3 只看带属性的主体** ⇒ 新增 **K5**（普查自洽）+ **W1** 显式可观测告警
     `未标注主体数 = labelledNodes − kg_nodes`（> 0 ⇒ WARN；`--strict` ⇒ ERROR）。

## 为什么有第三版（第 6 轮：ONTO 载体自述 ⇒ O3 收窄，**不**动数据侧）
专用于 TBox 的命名图（`…/graph/tbox`）是方案2 的**既定设计**，而第一/二版的 O3 隐含假设
「仓库里每张命名图都是快照图」⇒ 它必然对 TBox 图报「0 主语」。第 6 轮裁定**缺陷在门禁的
隐含假设、不在数据侧**，故改为**载体自述驱动**：图内含 ≥1 个 `?s a owl:Ontology` 主语
⇒ 归类 `ontologyGraphs`（真机 `…/graph/tbox` 由 7 个模块本体自述 `…/onto/{topology#,core#,
metric#,fault#,causal#,action#,prov#} a owl:Ontology` 满足），不参与 O3；判别只看**图内三元组**，
**禁止**把图名（如 `tbox`）写进代码（真机实测：以图 IRI 为主语的 `owl:Ontology` 三元组 = 0 条）。
O3 因此收窄为「快照图必须有 `ccfSnapshotId` 主语」；未自述又非快照图的命名图**仍 ERROR**。
报告与 `--json` 增列 `ontologyGraphs` / `ontologyGraphCount`，人类输出把 ONTO 图分
「快照图（时间序）/ 本体图 / 未归类」三类；**不新增** `--allow-non-snapshot-graph` 开关
（有了自述判据就不需要放宽开关，少一个开关就少一条被滥用的路）。

## 判定规则（两半都必须成立，且都必须**真的检查到东西**）
KG 半（Neo4j）：
  K1 `:CC_Meta` 节点数 == 1；
  K2 其 `snapshotId` 满足 `^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$`
     —— 占位串（如 `UNSET-TBOX-LOAD`）一律 ERROR，**不得**冒充合法快照；
  K3 `:CC_Meta.snapshotId` == 所有 `CC_` 前缀 ABox **节点**与 ABox **关系**上出现的
     `ccfSnapshotId` 取值集合的**唯一元素**；
  K4 该取值集合非空，且 ABox 节点侧必须检查到 > 0 条（禁止「检查 0 条也算通过」）；
  K5 普查自洽：`labelledNodes >= kg_nodes`（两次普查口径矛盾 ⇒ ERROR）；
  W1 未标注主体数 = `labelledNodes − kg_nodes`（> 0 ⇒ WARN；`--strict` ⇒ ERROR）。
ONTO 半（GraphDB）：
  O1 枚举仓库（默认 `cc-ops`）所有命名图（0 张 ⇒ ERROR）；
  O2 每图 IRI 末段必须满足 K2 的格式，且 == 该图内主语断言的
     `<https://credit-card-ops.ai/onto/topology#ccfSnapshotId>` 取值集合的**唯一元素**；
  O3 每张**快照图**（IRI 末段匹配快照 id 格式）断言该属性的主语数必须 > 0
     （0 主语 ⇒ ERROR，不得静默通过）；图内**自述**为 `?s a owl:Ontology` 的命名图
     归 `ontologyGraphs`（本体/模式图，见下「载体自述」），不参与 O3 —— 但未自述、
     又非快照图的命名图仍按 O3 / O2 报 ERROR（fail-closed）；
  O4 **跨载体（存在）**：至少存在一张命名图的 IRI 末段 == `:CC_Meta.snapshotId`（「当前快照图必须存在」）；
     其余图必须显式白名单化（`--allow-history 末段,末段`）才算「历史快照图」，
     既不等于当前快照、又不在白名单 ⇒ W2 未归类（WARN；`--strict` ⇒ ERROR）。
  O5 **跨载体（时序）**：ONTO 命名图是**按快照累积**的，故 O4 的「存在即通过」会漏掉
     「KG 停在旧快照、ONTO 已前进到更新快照」（真机现状）这一**真不一致**；因此要求
     **最新**（快照 id 词法序最大；格式定宽 ⇒ 等于时间序）那张命名图的 IRI 末段
     == `:CC_Meta.snapshotId`，否则 ERROR。历史图容差只能经 `--allow-history` 显式声明，
     而白名单只影响 W2 归类，**不能救 O4 / O5**。两侧快照 id 与 ONTO 命名图清单
     （快照时间序）都打印在人类可读输出与 `--json`
     （`kgSnapshotId` / `latestOntoSnapshotId` / `latestOntoGraphIri` /
     `crossCarrierAgree` / `ontoSnapshotGraphs` / `ontoNonSnapshotGraphIris`）。
反恒真（**分侧下限**，任一 `<= 0` = 参数错误，不得用 0/负数关闭）：
  A1 实际检查到的 ABox 主体 = KG 节点数 + KG 关系数 + ONTO 逐图主语数 >= `--min-abox-subjects`（默认 100）；
  A2 KG `CC_` 前缀 ABox 节点数 >= `--min-kg-nodes`（默认 100）—— ONTO 侧主语**不得替 KG 侧顶满下限**；
  A3 KG `ccfSnapshotId` 关系数 >= `--min-kg-rels`（默认 100）；
  A4 ONTO 逐图断言主语合计 >= `--min-onto-subjects`（默认 100）。
报告里每侧**实际值与下限都打印**（人类可读 + `--json`）。

## 判别力自证
`--selftest`：对**注入式假行集**跑同一套 `evaluate()`，含第一版 10 个用例
（meta 与 ABox 不一致 / ABox 两个快照 / 占位串 / ONTO 某图 IRI 与逐主语取值不一致 /
某图 0 主语 / 反恒真下限 / :CC_Meta 缺失 / 双节点 / ABox 无快照）+ 新增结构性突变
（O4 无当前快照图 / 陌生快照图 / 0 命名图 / **O5 最新图非当前快照 + 其阳性对照** /
KG 节点空壳 / KG 关系空壳 / ONTO 主语空壳 / 未归类 WARN / 白名单归类 / W1 /
`--strict` 升级 / **第 6 轮载体自述 5 条：本体图不报 O3 + 其关键负控（删自述 ⇒ 变未归类 ⇒
O3 报错）/ 未归类怪图仍报 O3 / 快照图缺 ccfSnapshotId 仍报 O3 / 三计数分离**）
+ **参数校验路径**（0 / 负数 ⇒ 参数错误），逐一断言，并打印 `N/N` 计数。
自证用例脚本另见 `tmp/g19_counterexample.py`（真机版阳性对照）与
`tmp/g19_bypass_counterexample.py`（4 条绕过 + 「存在即通过」容差 ⇒ 共 5 条，
含与 `tmp/g19_gate_o4only_snapshot.py`（O4-only 留档）的 A/B 对照 + CLI 矩阵）。

用法：
  set CC_NEO4J_PASSWORD=...
  python shared\\verify\\graph_meta_gate.py [--repo cc-ops] [--allow-history t1,t2] [--strict]
                                          [--json] [--out 报告.json] [--selftest] [--quiet]
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

# 快照 id 格式（生成侧 = shared/collect/freeze.py；此处是只读校验口径，两处必须一致）
SNAPSHOT_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")
# 本体/模式图的**载体自述**谓词类：图内 ≥1 个 `?s a owl:Ontology` 主语（只看图内三元组，不看图名）
OWL_ONTOLOGY = "http://www.w3.org/2002/07/owl#Ontology"

DEFAULTS = {
    "label_prefix": "CC_",
    "meta_label": "CC_Meta",
    "meta_snapshot_field": "snapshotId",
    "snapshot_prop": "ccfSnapshotId",
    "onto_prop_iri": "https://credit-card-ops.ai/onto/topology#ccfSnapshotId",
    "graph_template": "https://credit-card-ops.ai/onto/graph/{snapshotId}",
    "bolt_uri": "bolt://127.0.0.1:7688",  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
    "user": "neo4j",
    "password_env": "CC_NEO4J_PASSWORD",
    "database": "neo4j",
    "gdb_url": "http://127.0.0.1:7200",  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
    "repo": "cc-ops",
}
# 反恒真下限：**分侧**，各自独立生效（0 / 负数 = 参数错误，不得用来关闭判据）
MIN_ABOX_SUBJECTS = 100        # A1 合计
MIN_KG_NODES = 100             # A2 KG 节点侧
MIN_KG_RELS = 100              # A3 KG 关系侧
MIN_ONTO_SUBJECTS = 100        # A4 ONTO 主语侧
FLOOR_KEYS = (("min_abox_subjects", "--min-abox-subjects", "A1 合计受检 ABox 主体"),
              ("min_kg_nodes", "--min-kg-nodes", "A2 KG 节点侧"),
              ("min_kg_rels", "--min-kg-rels", "A3 KG 关系侧"),
              ("min_onto_subjects", "--min-onto-subjects", "A4 ONTO 主语侧"))
# 明确点名的占位串（K2 已由格式正则兜住；这里只用于把报错写得更好读）
PLACEHOLDER_MARKERS = ("UNSET", "PLACEHOLDER", "TODO", "TBD", "NONE")


# ---------------------------------------------------------------------------
# 配置：载体名/属性名一律优先从映射表与部署清单读，读不到才退回上面的默认值
# ---------------------------------------------------------------------------
def _read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _floor(value, default):
    """下限取值：**只**在「未给出」时回落默认值。

    ★ 修复绕过③：旧实现是 `getattr(args, 'min_abox_subjects', None) or MIN_ABOX_SUBJECTS`，
    `0` falsy ⇒ 被悄悄换成 100 写进 cfg，而 `main` 又直接用 args 原值（`0` / `-5`）⇒
    命令行可以关闭下限，且 cfg 与 args 双口径让 `--selftest` 结构上发现不了。
    """
    if value is None:
        return int(default)
    return int(value)


def _as_tail(entry):
    """把 `--allow-history` 条目归一成命名图 IRI 末段（既接受末段也接受整条 IRI）。"""
    return str(entry).strip().rstrip("/").rsplit("/", 1)[-1]


def load_cfg(args):
    cfg = dict(DEFAULTS)
    tg = os.path.join(ROOT, "shared", "deploy", "targets.yaml")
    if os.path.isfile(tg):
        try:
            t = _read_yaml(tg)
            cfg["bolt_uri"] = t["neo4j"]["bolt_uri"]
            cfg["user"] = t["neo4j"]["auth"]["user"]
            cfg["password_env"] = t["neo4j"]["auth"]["password_env"]
            cfg["gdb_url"] = t["graphdb"]["url"]
            cfg["repo"] = t["graphdb"]["isolation"]["repository"]
        except Exception as exc:
            print("[WARN] 读 %s 失败，退回默认载体地址：%s" % (tg, exc))
    tm = os.path.join(getattr(args, "mapping_dir", None) or
                      os.path.join(ROOT, "shared", "mapping"), "type-map.yaml")
    if os.path.isfile(tm):
        try:
            ns = _read_yaml(tm)["namespaces"]
            kg = ns.get("kg") or {}
            onto = ns.get("onto") or {}
            prefix = kg.get("label_prefix") or cfg["label_prefix"]
            cfg["label_prefix"] = prefix
            cfg["meta_label"] = prefix + "Meta"
            cfg["snapshot_prop"] = kg.get("snapshot_property") or cfg["snapshot_prop"]
            cc_ns = onto.get("cc") or cfg["onto_prop_iri"].rsplit("#", 1)[0] + "#"
            snap_prop = (onto.get("snapshot_property") or "cc:ccfSnapshotId").split(":")[-1]
            cfg["onto_prop_iri"] = cc_ns + snap_prop
            if onto.get("graph_iri_template"):
                cfg["graph_template"] = onto["graph_iri_template"]
        except Exception as exc:
            print("[WARN] 读 %s 失败，退回默认命名空间：%s" % (tm, exc))
    if getattr(args, "repo", None):
        cfg["repo"] = args.repo
    if getattr(args, "neo4j_uri", None):
        cfg["bolt_uri"] = args.neo4j_uri
    if getattr(args, "user", None):
        cfg["user"] = args.user
    if getattr(args, "database", None):
        cfg["database"] = args.database
    # 分侧下限：单一口径（cfg 是唯一权威，main 不再读 args 原值）
    cfg["min_abox_subjects"] = _floor(getattr(args, "min_abox_subjects", None),
                                     MIN_ABOX_SUBJECTS)
    cfg["min_kg_nodes"] = _floor(getattr(args, "min_kg_nodes", None), MIN_KG_NODES)
    cfg["min_kg_rels"] = _floor(getattr(args, "min_kg_rels", None), MIN_KG_RELS)
    cfg["min_onto_subjects"] = _floor(getattr(args, "min_onto_subjects", None),
                                      MIN_ONTO_SUBJECTS)
    raw_hist = getattr(args, "allow_history", None) or []
    entries = []
    for item in raw_hist:
        entries.extend(x.strip() for x in str(item).split(",") if x.strip())
    cfg["allow_history"] = entries
    cfg["strict"] = bool(getattr(args, "strict", False))
    return cfg


def history_tails(cfg):
    """白名单归一化后的末段集合。"""
    return {_as_tail(h) for h in (cfg.get("allow_history") or [])}


def validate_params(cfg):
    """参数校验：任一违规即**参数错误**（exit 2，绝不产生 PASS 输出）。

    这是绕过③的正面修法：`0` / 负数**不能**用来关闭反恒真下限，而是被判为用法错误。
    """
    errs = []
    for key, flag, human in FLOOR_KEYS:
        raw = cfg.get(key)
        try:
            iv = int(raw)
        except (TypeError, ValueError):
            errs.append("参数错误：%s（%s）必须是整数，实得 %r" % (flag, human, raw))
            continue
        if iv <= 0:
            errs.append("参数错误：%s（%s）必须 > 0，实得 %d"
                        " ⇒ 禁止用 0/负数关闭反恒真下限（0 会退化成「检查 0 条也算通过」）"
                        % (flag, human, iv))
    bad = [h for h in (cfg.get("allow_history") or [])
           if not SNAPSHOT_RE.match(_as_tail(h))]
    if bad:
        errs.append("参数错误：--allow-history 条目必须是合法快照 id（末段），"
                    "或以其为末段的命名图 IRI；实得 %s，期望 %s"
                    % (bad, SNAPSHOT_RE.pattern))
    return errs


# ---------------------------------------------------------------------------
# 采集（KG / ONTO 两侧独立回读；失败 = BLOCKED，绝不当作 PASS）
# ---------------------------------------------------------------------------
def collect_kg(cfg, password=None):
    from neo4j import GraphDatabase
    pwd = password or os.environ.get(cfg["password_env"])
    if not pwd:
        raise RuntimeError("缺少口令：请设置环境变量 %s（G19 不接受无凭据的「通过」）"
                           % cfg["password_env"])
    try:
        driver = GraphDatabase.driver(cfg["bolt_uri"], auth=(cfg["user"], pwd),
                                      notifications_min_severity="OFF")
    except TypeError:
        driver = GraphDatabase.driver(cfg["bolt_uri"], auth=(cfg["user"], pwd))
    prefix = cfg["label_prefix"]
    sp = cfg["snapshot_prop"]
    ml = cfg["meta_label"]
    out = {"metaLabel": ml, "snapshotProp": sp, "metaCount": 0, "meta": None,
           "labelledNodes": 0, "nodeSnaps": {}, "relSnaps": {}}
    try:
        with driver.session(database=cfg["database"]) as sess:
            sess.run("RETURN 1").consume()
            rows = list(sess.run("MATCH (m:`%s`) RETURN properties(m) AS props" % ml))
            out["metaCount"] = len(rows)
            if rows:
                out["meta"] = dict(rows[0]["props"])
            out["labelledNodes"] = sess.run(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH $p) "
                "RETURN count(n) AS c", p=prefix).single()["c"]
            for rec in sess.run(
                    "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH $p) "
                    "AND n.`%s` IS NOT NULL RETURN toString(n.`%s`) AS snap, count(*) AS c"
                    % (sp, sp), p=prefix):
                out["nodeSnaps"][rec["snap"]] = rec["c"]
            for rec in sess.run(
                    "MATCH ()-[e]->() WHERE e.`%s` IS NOT NULL "
                    "RETURN toString(e.`%s`) AS snap, count(*) AS c" % (sp, sp)):
                out["relSnaps"][rec["snap"]] = rec["c"]
    finally:
        driver.close()
    return out


def sparql(cfg, query, timeout=120):
    url = "%s/repositories/%s" % (cfg["gdb_url"].rstrip("/"),
                                  urllib.parse.quote(cfg["repo"]))
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Accept": "application/sparql-results+json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]


def collect_onto(cfg):
    prop = cfg["onto_prop_iri"]
    graphs = sorted(r["g"]["value"] for r in
                    sparql(cfg, "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }"))
    out = []
    for g in graphs:
        cnt = sparql(cfg, "SELECT (COUNT(DISTINCT ?s) AS ?c) WHERE { GRAPH <%s> { ?s <%s> ?v } }"
                     % (g, prop))
        vals = sparql(cfg, "SELECT DISTINCT ?v WHERE { GRAPH <%s> { ?s <%s> ?v } }" % (g, prop))
        # 载体**自述**：该命名图内 ≥1 个 `?s a owl:Ontology` 主语 ⇒ 本体/模式图（O3 收窄用）。
        # 只看图内三元组，**不看图名**（真机 `…/graph/tbox` 的图 IRI 自身不是 owl:Ontology 主语）。
        onto_cnt = sparql(cfg, "SELECT (COUNT(DISTINCT ?s) AS ?c) WHERE "
                               "{ GRAPH <%s> { ?s a <%s> } }" % (g, OWL_ONTOLOGY))
        out.append({"graph": g,
                    "subjects": int(cnt[0]["c"]["value"]) if cnt else 0,
                    "values": sorted({r["v"]["value"] for r in vals}),
                    "ontologySubjects": int(onto_cnt[0]["c"]["value"]) if onto_cnt else 0})
    return out


# ---------------------------------------------------------------------------
# 判定（纯函数：真机与 --selftest 走同一套代码）
# ---------------------------------------------------------------------------
def evaluate(kg, onto, cfg, min_subjects, strict=None):
    """返回 (errors, stats)。errors 非空 ⇒ ERROR（退出码 1）。

    * `strict` 为 None 时取 `cfg["strict"]`；`--strict` 把 WARN 升为 ERROR。
    * 下限一律从 cfg（分侧）读，`min_subjects` 保留为 A1 合计下限的位置参数
      （旧调用方 `tmp/m212_verify_g19_bypass.py` 按 4 个位置参数调用，签名不变）。
    """
    errs = []
    warns = []
    ml = cfg["meta_label"]
    sp = cfg["snapshot_prop"]
    fld = cfg["meta_snapshot_field"]
    prop_iri = cfg["onto_prop_iri"]
    if strict is None:
        strict = bool(cfg.get("strict", False))
    min_kg_nodes = int(cfg.get("min_kg_nodes", MIN_KG_NODES))
    min_kg_rels = int(cfg.get("min_kg_rels", MIN_KG_RELS))
    min_onto_subjects = int(cfg.get("min_onto_subjects", MIN_ONTO_SUBJECTS))
    allow = history_tails(cfg)
    meta_n = int(kg.get("metaCount") or 0)
    meta = kg.get("meta") or {}
    sid = meta.get(fld)
    sid = str(sid) if sid is not None else None

    # ---- 参数校验（与 CLI 同一套判据；函数级调用同样不得用 0/负数关下限）----
    for key, flag, human in FLOOR_KEYS:
        want = int(min_subjects) if key == "min_abox_subjects" else int(cfg.get(key, 0))
        if want <= 0:
            errs.append("参数错误：%s（%s）必须 > 0，实得 %d"
                        " ⇒ 禁止用 0/负数关闭反恒真下限（0 会退化成「检查 0 条也算通过」）"
                        % (flag, human, want))

    # ---- KG 半 ----
    if meta_n != 1:
        errs.append("K1 KG `:%s` 应恰好 1 个节点，实得 %d" % (ml, meta_n))
    if not isinstance(sid, str) or not sid:
        errs.append("K2 KG `:%s` 缺 `%s`（硬约束 C1/C5：缺快照 id 的图不得进评测）" % (ml, fld))
        sid = None
    elif not SNAPSHOT_RE.match(sid):
        hint = "（疑似 TBox 装载占位串）" if any(m in sid.upper() for m in PLACEHOLDER_MARKERS) else ""
        errs.append("K2 KG `:%s.%s` = %r 不是合法快照 id%s，期望 %s"
                    % (ml, fld, sid, hint, SNAPSHOT_RE.pattern))

    node_snaps = {str(k): int(v) for k, v in (kg.get("nodeSnaps") or {}).items()}
    rel_snaps = {str(k): int(v) for k, v in (kg.get("relSnaps") or {}).items()}
    kg_nodes = sum(node_snaps.values())
    kg_rels = sum(rel_snaps.values())
    labelled = int(kg.get("labelledNodes") or 0)
    unlabelled = labelled - kg_nodes
    vals = sorted(set(node_snaps) | set(rel_snaps))
    if kg_nodes <= 0:
        errs.append("K4 KG 侧 `%s` 前缀 ABox 节点上未检查到任何 `%s` ⇒ 拒绝 PASS"
                    "（禁止「检查 0 条也算通过」）" % (cfg["label_prefix"], sp))
    if not vals:
        errs.append("K4 KG 侧 ABox 的 `%s` 取值集合为空 ⇒ 无法判定图级身份" % sp)
    if len(vals) > 1:
        errs.append("K3 KG 侧 ABox 出现 %d 个不同 `%s`：%s ⇒ 跨快照混装"
                    % (len(vals), sp,
                       ", ".join("%s(节点%d/关系%d)" % (v, node_snaps.get(v, 0), rel_snaps.get(v, 0))
                                 for v in vals)))
    elif len(vals) == 1 and sid is not None and vals[0] != sid:
        errs.append("K3 KG 图级元数据说谎：`:%s.%s` = %r ≠ ABox 唯一 `%s` = %r"
                    % (ml, fld, sid, sp, vals[0]))
    # K5 普查自洽 + W1 未标注主体（绕过④：K3 只看带属性的主体，未标注主体必须**显式可观测**）
    if labelled < kg_nodes:
        errs.append("K5 KG 普查口径矛盾：带 `%s` 前缀标签的节点数 = %d < 带 `%s` 的 ABox 节点数 = %d"
                    " ⇒ 两次普查不可能同时成立（并发写入或采集口径漂移）"
                    % (cfg["label_prefix"], labelled, sp, kg_nodes))
    if unlabelled > 0:
        warns.append("W1 KG 未标注主体数 = labelledNodes − kgNodes = %d − %d = %d > 0"
                     " ⇒ 这些主体没有 `%s`，K3 的「唯一取值」判据看不到它们"
                     "（默认 WARN；--strict 升 ERROR）"
                     % (labelled, kg_nodes, unlabelled, sp))

    # ---- ONTO 半 ----
    onto_subjects = 0
    cur_iris, hist_iris, unclass_iris = [], [], []
    ont_iris, ont_rows = [], []
    graph_classes = []
    for g in onto:
        iri = str(g.get("graph") or "")
        tail = iri.rstrip("/").rsplit("/", 1)[-1]
        subs = int(g.get("subjects") or 0)
        gvals = sorted({str(v) for v in (g.get("values") or [])})
        ont_subs = int(g.get("ontologySubjects") or 0)
        is_snap = bool(SNAPSHOT_RE.match(tail))
        # 载体自述（队长第 6 轮裁定）：图内 ≥1 个 `?s a owl:Ontology` 主语 ⇒ 本体/模式图。
        # 只在「图名不是快照 id」时生效 —— 快照图仍必须自证 `ccfSnapshotId`（防误放宽）。
        is_onto = bool(ont_subs > 0) and not is_snap
        onto_subjects += subs
        if sid is not None and tail == sid:
            cls = "current"
            cur_iris.append(iri)
        elif is_onto:
            cls = "ontology"
            ont_iris.append(iri)
        elif tail in allow:
            cls = "history"
            hist_iris.append(iri)
        else:
            cls = "unclassified"
            unclass_iris.append(iri)
        ok = bool(subs > 0 and len(gvals) == 1 and gvals[0] == tail and is_snap)
        graph_classes.append({"iri": iri, "tail": tail, "subjects": subs,
                              "ontologySubjects": ont_subs,
                              "values": gvals, "classification": cls,
                              "selfConsistent": ok})
        if is_onto:
            # O3 收窄：本体图由**图内自述**识别，不要求 `ccfSnapshotId` 主语
            # （真机 `…/graph/tbox` 装 7 个模块本体自述、0 个快照主语）。
            ont_rows.append({"iri": iri, "tail": tail, "subjects": subs,
                             "ontologySubjects": ont_subs})
            continue
        if subs <= 0 or not gvals:
            errs.append("O3 ONTO 命名图 %s 中断言 `%s` 的主语数 = %d、取值 %s "
                        "⇒ 0 主语不得静默通过（该图自述 `owl:Ontology` 主语数 = %d，"
                        "即既不自述为本体图、又不是带快照身份的图）"
                        % (iri, prop_iri, subs, gvals, ont_subs))
            continue
        if not is_snap:
            errs.append("O2 ONTO 命名图 IRI 末段 %r 不是合法快照 id，期望 %s"
                        % (tail, SNAPSHOT_RE.pattern))
            continue
        if len(gvals) > 1:
            errs.append("O2 ONTO 命名图 %s 内 `%s` 有 %d 个不同取值：%s"
                        % (iri, prop_iri, len(gvals), gvals))
            continue
        if gvals[0] != tail:
            errs.append("O2 ONTO 命名图身份不一致：图 IRI 末段 %r ≠ 逐主语 `%s` 取值 %r"
                        % (tail, prop_iri, gvals[0]))
    if not onto:
        errs.append("O1 ONTO 仓库 %s 没有任何命名图 ⇒ 拒绝 PASS（禁止「检查 0 张图也算通过」）"
                    % cfg["repo"])
    # O4 跨载体身份（绕过①的正面修法）：KG 自报的当前快照必须真的有一张 ONTO 命名图
    sid_ok = bool(sid is not None and SNAPSHOT_RE.match(sid))
    if sid is not None and sid_ok and not cur_iris:
        errs.append("O4 跨载体身份不成立：ONTO 仓库 %s 里没有任何命名图的 IRI 末段 == "
                    "`:%s.%s` = %r（实际末段 %s）⇒ KG 与 ONTO 不是同一份快照，"
                    "禁止两半各自自洽就算通过"
                    % (cfg["repo"], ml, fld, sid,
                       sorted({c["tail"] for c in graph_classes}) or "（无命名图）"))
    # O5 跨载体**时序**身份（队长追加要求）：ONTO 命名图是**按快照累积**的，故 O4 的
    # 「存在一张同身份图」会漏掉「KG 停在旧快照、ONTO 已前进到更新快照」这一真不一致
    # （真机现状：KG = live3，ONTO 最新 = live5）。快照 id 定宽 ⇒ 词法序最大 == 时间序最新。
    # 历史图容差只能经 `--allow-history` 显式声明（且只体现在 W2 归类），**不能**救 O4/O5。
    snap_tails = sorted({c["tail"] for c in graph_classes if SNAPSHOT_RE.match(c["tail"])})
    latest_tail = snap_tails[-1] if snap_tails else None
    latest_iri = next((c["iri"] for c in graph_classes if c["tail"] == latest_tail), None)
    cross_ok = bool(sid_ok and latest_tail == sid)
    if sid is not None and sid_ok and cur_iris and latest_tail and latest_tail != sid:
        errs.append("O5 跨载体身份不一致（最新快照图）：ONTO 仓库 %s 的**最新**命名图末段 = %r"
                    "（图 %s）≠ `:%s.%s` = %r ⇒ 一侧停在旧快照而另一侧已前进，"
                    "禁止「历史里曾有一张同身份图」就算通过"
                    % (cfg["repo"], latest_tail, latest_iri, ml, fld, sid))
    for iri in unclass_iris:
        warns.append("W2 ONTO 命名图 %s 既非当前快照图（末段 ≠ `:%s.%s` = %r）"
                     "也不在 --allow-history 白名单 %s ⇒ 未归类（默认 WARN；--strict 升 ERROR）"
                     % (iri, ml, fld, sid, sorted(allow) or "[]"))

    # ---- 反恒真（分侧下限）----
    checked = kg_nodes + kg_rels + onto_subjects
    if checked < int(min_subjects):
        errs.append("A1 反恒真下限未达：检查到的 ABox 主体 = KG 节点 %d + KG 关系 %d + "
                    "ONTO 逐图主语 %d = %d < %d ⇒ ERROR（禁止「检查 0 条也算通过」）"
                    % (kg_nodes, kg_rels, onto_subjects, checked, int(min_subjects)))
    if kg_nodes < min_kg_nodes:
        errs.append("A2 KG 侧反恒真下限未达：KG `%s` 前缀 ABox 节点数 = %d < --min-kg-nodes %d"
                    " ⇒ ERROR（KG 半不得是空壳；ONTO 侧主语不能替 KG 侧顶满合计下限）"
                    % (cfg["label_prefix"], kg_nodes, min_kg_nodes))
    if kg_rels < min_kg_rels:
        errs.append("A3 KG 侧反恒真下限未达：KG `%s` 关系数 = %d < --min-kg-rels %d"
                    " ⇒ ERROR（KG 关系侧不得是空壳）" % (sp, kg_rels, min_kg_rels))
    if onto_subjects < min_onto_subjects:
        errs.append("A4 ONTO 侧反恒真下限未达：ONTO 逐图断言 `%s` 的主语合计 = %d < "
                    "--min-onto-subjects %d ⇒ ERROR（ONTO 侧不得是空壳）"
                    % (prop_iri, onto_subjects, min_onto_subjects))

    # ---- --strict：WARN 升 ERROR（只升不降；默认口径下 WARN 不改退出码）----
    if warns and strict:
        errs.extend("--strict 升级为 ERROR：%s" % w for w in warns)

    stats = {"metaCount": meta_n, "metaSnapshotId": sid,
             "kgNodes": kg_nodes, "kgRels": kg_rels, "kgSnapshots": vals,
             "kgLabelledNodes": labelled, "kgUnlabelledSubjects": unlabelled,
             "ontoGraphs": len(onto), "ontoSubjects": onto_subjects,
             "checkedAboxSubjects": checked, "minAboxSubjects": int(min_subjects),
             "minKgNodes": min_kg_nodes, "minKgRels": min_kg_rels,
             "minOntoSubjects": min_onto_subjects,
             "currentGraphCount": len(cur_iris), "currentGraphIris": cur_iris,
             "historyGraphCount": len(hist_iris), "historyGraphIris": hist_iris,
             "unclassifiedGraphCount": len(unclass_iris),
             "unclassifiedGraphIris": unclass_iris,
             # 载体自述的本体/模式图（O3 收窄；队长第 6 轮裁定）
             "ontologyGraphCount": len(ont_iris), "ontologyGraphIris": ont_iris,
             "ontologyGraphs": ont_rows,
             "historyTails": sorted(allow), "strict": strict,
             "graphClasses": graph_classes,
             "ontoSnapshotTails": snap_tails,
             "ontoSnapshotCount": len(snap_tails),
             "latestOntoSnapshotId": latest_tail,
             "latestOntoGraphIri": latest_iri,
             "crossCarrierAgree": cross_ok,
             "ontoSnapshotGraphs": [
                 {"iri": c["iri"], "tail": c["tail"], "subjects": c["subjects"],
                  "classification": c["classification"], "selfConsistent": c["selfConsistent"]}
                 for c in sorted(graph_classes, key=lambda c: c["tail"])
                 if SNAPSHOT_RE.match(c["tail"])],
             "ontoNonSnapshotGraphIris": [c["iri"] for c in graph_classes
                                          if not SNAPSHOT_RE.match(c["tail"])],
             "warnings": warns, "errors": errs,
             "sidChecked": sid_ok}
    return errs, stats


# ---------------------------------------------------------------------------
# --selftest：注入式假行集上的判别力自证
# ---------------------------------------------------------------------------
SNAP_A = "ccf-20260928T144216Z-b920b5"   # live3（真机 ABox 现状）
SNAP_B = "ccf-20260928T134833Z-eb634e"   # live2（真机修复前 :CC_Meta 的陈旧值）
SNAP_X = "ccf-20260101T000000Z-xxxxxx"
SNAP_Y = "ccf-20260101T010101Z-yyyyyy"
SNAP_C = "ccf-20260928T155516Z-937b79"   # live5：真机 ONTO **最新**命名图，KG 侧尚未跟进（O5 用）
GRAPH = "https://credit-card-ops.ai/onto/graph/"


def _fake():
    kg = {"metaLabel": "CC_Meta", "snapshotProp": "ccfSnapshotId", "metaCount": 1,
          "meta": {"snapshotId": SNAP_A},
          "nodeSnaps": {SNAP_A: 258}, "relSnaps": {SNAP_A: 225}, "labelledNodes": 343}
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + SNAP_B, "subjects": 14, "values": [SNAP_B]}]
    return kg, onto


def _mut_meta_abox_mismatch(kg, onto):
    """① 真机修复前现状：:CC_Meta = live2，ABox = live3。"""
    kg["meta"] = {"snapshotId": SNAP_B}
    return kg, onto, "图级元数据说谎"


def _mut_abox_two_snapshots(kg, onto):
    """② ABox 出现两个快照。"""
    kg["nodeSnaps"][SNAP_B] = 3
    return kg, onto, "出现 2 个不同"


def _mut_placeholder(kg, onto):
    """③ 占位串（且 ABox / ONTO 也整体跟着占位，排除「只是不一致」这条更宽松的判据）。"""
    kg["meta"] = {"snapshotId": "UNSET-TBOX-LOAD"}
    kg["nodeSnaps"] = {"UNSET-TBOX-LOAD": 258}
    kg["relSnaps"] = {"UNSET-TBOX-LOAD": 225}
    onto = [{"graph": GRAPH + "UNSET-TBOX-LOAD", "subjects": 258,
             "values": ["UNSET-TBOX-LOAD"]}]
    return kg, onto, "不是合法快照 id"


def _mut_onto_iri_value_mismatch(kg, onto):
    """④ ONTO 某图 IRI 与逐主语取值不一致。"""
    onto[0] = {"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_B]}
    return kg, onto, "命名图身份不一致"


def _mut_onto_zero_subjects(kg, onto):
    """⑤ ONTO 某图 0 主语。"""
    onto.append({"graph": GRAPH + "ccf-20260928T115458Z-6cf8c5",
                 "subjects": 0, "values": []})
    return kg, onto, "主语数 = 0"


def _mut_below_floor(kg, onto):
    """⑥ 反恒真下限：全链一致但检查到的 ABox 主体太少。"""
    tiny = "ccf-20260101T000000Z-aaaaaa"
    kg["meta"] = {"snapshotId": tiny}
    kg["nodeSnaps"] = {tiny: 1}
    kg["relSnaps"] = {}
    onto = [{"graph": GRAPH + tiny, "subjects": 5, "values": [tiny]}]
    return kg, onto, "反恒真下限未达"


def _mut_meta_missing(kg, onto):
    """⑦ :CC_Meta 节点缺失。"""
    kg["metaCount"] = 0
    kg["meta"] = {}
    return kg, onto, "应恰好 1 个节点"


def _mut_meta_two_nodes(kg, onto):
    """⑧ :CC_Meta 双节点。"""
    kg["metaCount"] = 2
    return kg, onto, "应恰好 1 个节点"


def _mut_abox_missing_snapshot(kg, onto):
    """⑨ ABox 上完全没有 ccfSnapshotId。"""
    kg["nodeSnaps"] = {}
    kg["relSnaps"] = {}
    return kg, onto, "未检查到任何"


CASES = [
    ("base-consistent", None, None),                 # 阳性对照：必须 PASS
    ("mut-meta-abox-mismatch", _mut_meta_abox_mismatch, "图级元数据说谎"),
    ("mut-abox-two-snapshots", _mut_abox_two_snapshots, "出现 2 个不同"),
    ("mut-placeholder", _mut_placeholder, "不是合法快照 id"),
    ("mut-onto-iri-value-mismatch", _mut_onto_iri_value_mismatch, "命名图身份不一致"),
    ("mut-onto-zero-subjects", _mut_onto_zero_subjects, "主语数 = 0"),
    ("mut-below-anti-tautology-floor", _mut_below_floor, "反恒真下限未达"),
    ("mut-meta-missing", _mut_meta_missing, "应恰好 1 个节点"),
    ("mut-meta-two-nodes", _mut_meta_two_nodes, "应恰好 1 个节点"),
    ("mut-abox-missing-snapshot", _mut_abox_missing_snapshot, "未检查到任何"),
]
# 第二版新增断言计数（改动本文件的新增用例时必须同步这两个数，否则 --selftest 会报总数不符）
NEW_STRUCTURAL_CASES = 18
NEW_PARAM_CASES = 7


# ---- 第二版新增：绕过①②④ 的结构性突变（注入式；expect = 必须命中的 ERROR 子串）----
def _mut_onto_no_current_graph(kg, onto):
    """绕过① A1：ONTO 只含**另一**快照的图（KG=A，ONTO 只有 B）⇒ 必须 O4。"""
    onto = [{"graph": GRAPH + SNAP_B, "subjects": 258, "values": [SNAP_B]}]
    return kg, onto, "O4 跨载体身份不成立"


def _mut_onto_stranger_graphs(kg, onto):
    """绕过① A2：ONTO 只含两个陌生快照 X/Y ⇒ 必须 O4。"""
    onto = [{"graph": GRAPH + SNAP_X, "subjects": 300, "values": [SNAP_X]},
            {"graph": GRAPH + SNAP_Y, "subjects": 300, "values": [SNAP_Y]}]
    return kg, onto, "O4 跨载体身份不成立"


def _mut_onto_zero_graphs(kg, onto):
    """O1：ONTO 一张命名图都没有（同时 O4 也不成立）。"""
    return kg, [], ("O1 ONTO 仓库", "O4 跨载体身份不成立")


def _mut_onto_latest_newer(kg, onto):
    """O5（队长追加）：ONTO 含当前快照图 A，但**最新**那张是更晚的快照 C ⇒ 必须 O5。

    这正是真机现状：KG 停在 live3（SNAP_A），ONTO 最新已到 live5（SNAP_C）。
    O4 只查「存在一张同身份图」⇒ 该状态会**误 PASS**，故必须由 O5 拦住。
    """
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + SNAP_B, "subjects": 14, "values": [SNAP_B]},
            {"graph": GRAPH + SNAP_C, "subjects": 258, "values": [SNAP_C]}]
    return kg, onto, "O5 跨载体身份不一致（最新快照图）"


def _mut_onto_latest_is_current(kg, onto):
    """O5 阳性对照：当前快照图 A 就是最新那张（A > B）⇒ 只允许 W2，不得有 O4/O5。"""
    return kg, onto, None


def _mut_kg_nodes_empty_shell(kg, onto):
    """绕过② B3：KG 仅 1 节点 / 0 关系，ONTO 顶满合计下限 ⇒ 必须 A2 + A3。"""
    kg["nodeSnaps"] = {SNAP_A: 1}
    kg["relSnaps"] = {}
    kg["labelledNodes"] = 1
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 896, "values": [SNAP_A]}]
    return kg, onto, ("A2 KG 侧反恒真下限未达", "A3 KG 侧反恒真下限未达")


def _mut_kg_rels_empty_shell(kg, onto):
    """绕过② 变体：KG 节点达标、**关系侧**空壳，合计下限仍被 ONTO 顶满 ⇒ 只剩 A3 能抓。"""
    kg["relSnaps"] = {}
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 896, "values": [SNAP_A]}]
    return kg, onto, "A3 KG 侧反恒真下限未达"


def _mut_onto_subjects_empty_shell(kg, onto):
    """绕过② 反向：KG 达标、**ONTO 主语**空壳，合计下限被 KG 顶满 ⇒ 只剩 A4 能抓。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 1, "values": [SNAP_A]}]
    return kg, onto, "A4 ONTO 侧反恒真下限未达"


def _mut_onto_unclassified(kg, onto):
    """绕过① 的共存形态：B 图既非当前快照、也不在白名单 ⇒ W2 未归类（非 strict 仍 PASS）。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + SNAP_B, "subjects": 14, "values": [SNAP_B]}]
    return kg, onto, None


def _mut_onto_history_whitelisted(kg, onto):
    """白名单化后同一输入不再产生 W2（图数/主语数不变）。"""
    return _mut_onto_unclassified(kg, onto)


def _mut_unlabelled_real_shape(kg, onto):
    """绕过④ 的真机形状：labelledNodes 343 而只有 258 个带属性 ⇒ W1（严格模式才 ERROR）。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]}]
    return kg, onto, None


ONT_GRAPH_IRI = "https://credit-card-ops.ai/onto/graph/tbox"


def _mut_onto_ontology_graph(kg, onto):
    """第 6 轮①：载体**自述**的本体/模式图（真机 `…/graph/tbox` 形态：图内 7 个模块本体
    `?s a owl:Ontology`、0 个 `ccfSnapshotId` 主语）⇒ 归 ontologyGraphs，**不得**报 O3/O2。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": ONT_GRAPH_IRI, "subjects": 0, "values": [],
             "ontologySubjects": 7}]
    return kg, onto, None


def _mut_onto_ontology_graph_no_selfdesc(kg, onto):
    """第 6 轮**关键负控**：把同一张图的 7 条本体自述删光 ⇒ 重新变未归类 ⇒ O3 必须报。

    它证明「本体图豁免」真由图内自述驱动，而不是无条件放行 0 主语的图。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": ONT_GRAPH_IRI, "subjects": 0, "values": [],
             "ontologySubjects": 0}]
    return kg, onto, "O3 ONTO 命名图"


def _mut_onto_strange_graph_unclassified(kg, onto):
    """第 6 轮②：未归类怪图（末段非快照 id、图内无本体自述）⇒ 仍 ERROR（fail-closed 不变）。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + "mystery-graph", "subjects": 0, "values": [],
             "ontologySubjects": 0}]
    return kg, onto, "O3 ONTO 命名图"


def _mut_onto_snapshot_graph_no_ccf(kg, onto):
    """第 6 轮③（防误放宽）：**末段是合法快照 id** 的图缺 `ccfSnapshotId` 主语 ⇒ 仍报 O3。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + "ccf-20260928T115458Z-6cf8c5", "subjects": 0, "values": [],
             "ontologySubjects": 0}]
    return kg, onto, "O3 ONTO 命名图"


def _mut_onto_counts_separated(kg, onto):
    """第 6 轮④：快照图 / 本体图 / 未归类三类计数分离（同一输入里各有一张）。"""
    onto = [{"graph": GRAPH + SNAP_A, "subjects": 258, "values": [SNAP_A]},
            {"graph": GRAPH + SNAP_B, "subjects": 14, "values": [SNAP_B]},
            {"graph": ONT_GRAPH_IRI, "subjects": 0, "values": [],
             "ontologySubjects": 7}]
    return kg, onto, None


def _run_case(name, mut, want, strict=False, cfg_extra=None, cfg=None, quiet=False):
    """跑一条注入式用例：返回 (ok, detail)。`want` 为 None ⇒ 期望**无 ERROR**。"""
    kg, onto = _fake()
    want_from_mut = None
    if mut is not None:
        kg, onto, want_from_mut = mut(kg, onto)
    if want is None:
        want = want_from_mut
    c = dict(cfg)
    if cfg_extra:
        c.update(cfg_extra)
    errs, stats = evaluate(kg, onto, c, c["min_abox_subjects"], strict=strict)
    if want is None:
        ok = not errs
        detail = "无 ERROR" if ok else "意外 ERROR：%s" % errs
    else:
        wants = want if isinstance(want, (list, tuple)) else [want]
        miss = [w for w in wants if not any(w in e for e in errs)]
        ok = not miss
        detail = ("命中 %s" % list(wants)) if ok else "**未命中** %s；实得 %s" % (miss, errs)
    print("[%s] %-32s %s  %s" % (" OK " if ok else "FAIL", name,
                                 "PASS(期望)" if want is None else "ERROR(期望)", detail))
    if not quiet and errs:
        for e in errs:
            print("        └ %s" % e)
    return ok, errs, stats


def _assert(name, ok, detail, quiet=False):
    print("[%s] %-32s %s  %s" % (" OK " if ok else "FAIL", name,
                                 "断言(期望)", detail))
    return bool(ok), detail


def selftest(cfg, quiet=False):
    good = 0
    for name, mut, want in CASES:
        ok, _e, _s = _run_case(name, mut, want, cfg=cfg, quiet=quiet)
        good += 1 if ok else 0

    # ---- 第二版新增：结构性突变（绕过①②④）----
    print("-" * 72)
    print("新增结构性突变（跨载体 O4 + 时序 O5 / 分侧下限 A2-A4 / 未标注 W1 / 未归类 W2 / --strict）：")
    good += 1 if _run_case("mut-onto-no-current-graph", _mut_onto_no_current_graph, None,
                           cfg=cfg, quiet=quiet)[0] else 0
    good += 1 if _run_case("mut-onto-stranger-graphs", _mut_onto_stranger_graphs, None,
                           cfg=cfg, quiet=quiet)[0] else 0
    good += 1 if _run_case("mut-onto-zero-graphs", _mut_onto_zero_graphs, None,
                           cfg=cfg, quiet=quiet)[0] else 0
    # O5（队长追加）：O4 的「存在即通过」会放过「KG 停在旧快照、ONTO 已前进」
    ok4, e4, st4 = _run_case("mut-onto-latest-newer", _mut_onto_latest_newer,
                             "O5 跨载体身份不一致（最新快照图）", cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ O5 最新图非当前快照 ⇒ ERROR",
                         ok4 and st4["crossCarrierAgree"] is False
                         and st4["latestOntoSnapshotId"] == SNAP_C,
                         "latestOntoSnapshotId=%r crossCarrierAgree=%s"
                         % (st4["latestOntoSnapshotId"], st4["crossCarrierAgree"]),
                         quiet)[0] else 0
    ok5, e5, st5 = _run_case("mut-onto-latest-is-current", _mut_onto_latest_is_current, None,
                             cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ O5 阳性对照（最新 == 当前）不报错",
                         ok5 and st5["latestOntoSnapshotId"] == SNAP_A
                         and st5["crossCarrierAgree"] is True
                         and not any("O5" in e for e in e5),
                         "latestOntoSnapshotId=%r crossCarrierAgree=%s"
                         % (st5["latestOntoSnapshotId"], st5["crossCarrierAgree"]),
                         quiet)[0] else 0
    good += 1 if _run_case("mut-kg-nodes-empty-shell", _mut_kg_nodes_empty_shell, None,
                           cfg=cfg, quiet=quiet)[0] else 0
    good += 1 if _run_case("mut-kg-rels-empty-shell", _mut_kg_rels_empty_shell, None,
                           cfg=cfg, quiet=quiet)[0] else 0
    good += 1 if _run_case("mut-onto-subjects-empty-shell", _mut_onto_subjects_empty_shell,
                           None, cfg=cfg, quiet=quiet)[0] else 0
    # 未归类：非 strict 下必须 0 ERROR 且 warnings 里必须有 W2
    ok, _e, st = _run_case("mut-onto-unclassified-warn", _mut_onto_unclassified, None,
                           cfg=cfg, quiet=quiet)
    hit = any("W2" in w and "未归类" in w for w in st["warnings"])
    good += 1 if _assert("  ↳ W2 未归类告警可见", ok and hit,
                         "unclassified=%d warnings=%d" % (st["unclassifiedGraphCount"],
                                                          len(st["warnings"])),
                         quiet)[0] else 0
    # 白名单：同一输入显式白名单化 ⇒ 未归类归零、无 W2
    ok2, _e2, st2 = _run_case("mut-onto-history-whitelisted", _mut_onto_history_whitelisted,
                              None, cfg_extra={"allow_history": [SNAP_B]},
                              cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ 白名单把 B 图归为 history",
                         ok2 and st2["historyGraphCount"] == 1
                         and st2["unclassifiedGraphCount"] == 0
                         and not any("W2" in w for w in st2["warnings"]),
                         "history=%d unclassified=%d 末段=%s"
                         % (st2["historyGraphCount"], st2["unclassifiedGraphCount"],
                            st2["historyTails"]), quiet)[0] else 0
    # W1：真机形状（未标注 85）非 strict 只 WARN；--strict 升 ERROR
    ok3, e3, st3 = _run_case("mut-unlabelled-real-shape", _mut_unlabelled_real_shape, None,
                             cfg=cfg, quiet=quiet)
    w1 = any("未标注主体数" in w for w in st3["warnings"])
    good += 1 if _assert("  ↳ W1 未标注主体数可见且非 strict 不报错",
                         ok3 and w1 and st3["kgUnlabelledSubjects"] == 85
                         and not any("未标注主体数" in e for e in e3),
                         "unlabelled=%d（=343−258）" % st3["kgUnlabelledSubjects"],
                         quiet)[0] else 0
    good += 1 if _run_case("strict-unlabelled-escalation", _mut_unlabelled_real_shape,
                           "未标注主体数", strict=True, cfg=cfg, quiet=quiet)[0] else 0
    good += 1 if _run_case("strict-unclassified-escalation", _mut_onto_unclassified,
                           "未归类", strict=True, cfg=cfg, quiet=quiet)[0] else 0

    # ---- 第 6 轮（队长裁定）：O3 改为「载体自述驱动」，禁止硬编码图名 ----
    ok_o, e_o, st_o = _run_case("mut-onto-ontology-graph", _mut_onto_ontology_graph, None,
                                cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ ①本体图（图内自述 owl:Ontology）不报 O3",
                         ok_o and st_o["ontologyGraphCount"] == 1
                         and st_o["ontologyGraphIris"] == [ONT_GRAPH_IRI]
                         and st_o["unclassifiedGraphCount"] == 0
                         and not any("O3" in e for e in e_o),
                         "ontology=%d current=%d unclassified=%d"
                         % (st_o["ontologyGraphCount"], st_o["currentGraphCount"],
                            st_o["unclassifiedGraphCount"]), quiet)[0] else 0
    ok_n, e_n, st_n = _run_case("mut-onto-ontology-graph-no-selfdesc",
                                _mut_onto_ontology_graph_no_selfdesc,
                                "O3 ONTO 命名图", cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ ★关键负控：删掉 7 条自述 ⇒ 变未归类 ⇒ O3 报错",
                         ok_n and st_n["ontologyGraphCount"] == 0
                         and st_n["unclassifiedGraphCount"] == 1,
                         "ontology=%d unclassified=%d"
                         % (st_n["ontologyGraphCount"],
                            st_n["unclassifiedGraphCount"]), quiet)[0] else 0
    ok_s, e_s, st_s = _run_case("mut-onto-strange-graph-unclassified",
                                _mut_onto_strange_graph_unclassified,
                                "O3 ONTO 命名图", cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ ②未归类怪图（无自述、无快照身份）仍报 O3",
                         ok_s and st_s["ontologyGraphCount"] == 0
                         and st_s["unclassifiedGraphCount"] == 1,
                         "ontology=%d unclassified=%d"
                         % (st_s["ontologyGraphCount"],
                            st_s["unclassifiedGraphCount"]), quiet)[0] else 0
    ok_z, e_z, st_z = _run_case("mut-onto-snapshot-graph-no-ccf",
                                _mut_onto_snapshot_graph_no_ccf,
                                "O3 ONTO 命名图", cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ ③快照图缺 ccfSnapshotId 主语仍报 O3（防误放宽）",
                         ok_z and st_z["ontologyGraphCount"] == 0
                         and any("主语数 = 0" in e for e in e_z),
                         "errors=%d" % len(e_z), quiet)[0] else 0
    ok_c, e_c, st_c = _run_case("mut-onto-counts-separated", _mut_onto_counts_separated,
                                None, cfg=cfg, quiet=quiet)
    good += 1 if _assert("  ↳ ④快照图/本体图/未归类三计数分离",
                         ok_c and st_c["currentGraphCount"] == 1
                         and st_c["ontologyGraphCount"] == 1
                         and st_c["unclassifiedGraphCount"] == 1
                         and st_c["historyGraphCount"] == 0
                         and st_c["ontoSnapshotCount"] == 2
                         and st_c["latestOntoSnapshotId"] == SNAP_A,
                         "current=%d ontology=%d unclassified=%d ontoSnapshots=%d"
                         % (st_c["currentGraphCount"], st_c["ontologyGraphCount"],
                            st_c["unclassifiedGraphCount"], st_c["ontoSnapshotCount"]),
                         quiet)[0] else 0

    # ---- 第二版新增：参数校验路径（绕过③）----
    print("-" * 72)
    print("参数校验路径（下限 0 / 负数 ⇒ 参数错误，绝不被静默关闭）：")
    c_zero = dict(cfg); c_zero["min_abox_subjects"] = 0
    c_neg = dict(cfg); c_neg["min_abox_subjects"] = -5
    kg, onto = _fake()
    e_zero, _s = evaluate(kg, onto, c_zero, 0)
    e_neg, _s = evaluate(kg, onto, c_neg, -5)
    good += 1 if _assert("param-evaluate-floor-zero", any("参数错误" in e for e in e_zero),
                         "errors=%d" % len(e_zero), quiet)[0] else 0
    good += 1 if _assert("param-evaluate-floor-negative", any("参数错误" in e for e in e_neg),
                         "errors=%d" % len(e_neg), quiet)[0] else 0
    c_kg = dict(cfg); c_kg["min_kg_nodes"] = 0
    good += 1 if _assert("param-validate-min-kg-nodes-zero",
                         any("参数错误" in e for e in validate_params(c_kg)),
                         "%s" % validate_params(c_kg), quiet)[0] else 0
    bad_hist = dict(cfg); bad_hist["allow_history"] = ["not-a-snap"]
    good += 1 if _assert("param-validate-bad-history-entry",
                         any("参数错误" in e for e in validate_params(bad_hist)),
                         "%s" % validate_params(bad_hist), quiet)[0] else 0

    # CLI 端到端：`main(["--min-abox-subjects","0"])` 必须 exit 2 + 打印「参数错误」，
    # 且**不得**是 BLOCKED（BLOCKED 也是 2）—— 防止「校验失效后靠连不通的口令假通过」。
    for flag_value in ("0", "-5"):
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            rc = main(["--min-abox-subjects", flag_value])
        finally:
            sys.stdout = real
        txt = buf.getvalue()
        ok = (rc == 2 and "参数错误" in txt and "BLOCKED" not in txt
              and "RESULT: PASS" not in txt)
        good += 1 if _assert("param-cli-min-abox-subjects-%s" % flag_value, ok,
                             "exit=%d 输出含参数错误=%s 含BLOCKED=%s"
                             % (rc, "参数错误" in txt, "BLOCKED" in txt), quiet)[0] else 0
    # 合计下限 5000：必须仍报 A1 且原文含 `< 5000`
    e_big, _s = evaluate(kg, onto, cfg, 5000)
    good += 1 if _assert("aggregate-floor-5000-still-A1",
                         any("A1" in e and "< 5000" in e for e in e_big),
                         "%s" % [e for e in e_big if "A1" in e], quiet)[0] else 0

    total = len(CASES) + NEW_STRUCTURAL_CASES + NEW_PARAM_CASES
    print("-" * 72)
    print("判别力自证（G19 --selftest）：%d/%d 用例断言成立"
          "（第一版阳性对照 1 + 第一版突变 %d + 第二版结构性突变 %d"
          "（含队长追加 O5 2 条、第 6 轮载体自述 5 条）+ 第二版参数校验 %d）"
          % (good, total, len(CASES) - 1, NEW_STRUCTURAL_CASES, NEW_PARAM_CASES))
    if good != total:
        print("RESULT: FAIL（G19 --selftest：%d/%d）" % (good, total))
        return 1
    print("RESULT: PASS（G19 --selftest：%d/%d）" % (good, total))
    return 0


# ---------------------------------------------------------------------------
# 报告（人类可读 + --json；每侧实际值与下限都必须在场）
# ---------------------------------------------------------------------------
HONESTY_NOTES = [
    "ONTO 载体可能被外部并发写入 ⇒ 图数 / 逐图主语数 / KG 节点数会跨时点漂移；"
    "本报告的 capturedAt 是这一次采集的时点，数值不可跨时点复现。",
    "W1 未标注主体数按 `labelledNodes − kgNodes` 计算，分母含共享 `CC_` 前缀的 TBox 节点"
    " ⇒ 真机上结构性 > 0（TBox 不携带 ccfSnapshotId），故默认只 WARN；"
    "`--strict` 在真机会因此 exit 1，属预期而非缺陷。",
    "K3 / K5 / O4 / O5 判的是「载体自报的快照身份」之间的一致性；"
    "对「ABox 内容是否真的等于该快照的采集结果」不做比对（无第二数据源）。",
    "O4 只在 `:CC_Meta.snapshotId` 合法（K2 通过）时才判定：K2 已经 ERROR 的情况下"
    "不再用 O4 重复计数，但退出码仍为 1。O5 以「ONTO 最新命名图」判定；"
    "若 ONTO 连一张合法快照图都没有（O1/O4 已 ERROR）或当前快照图根本不存在"
    "（O4 已 ERROR），O5 不重复计数。",
    "O5 的「最新」按快照 id **词法序**取最大：快照 id 格式定宽"
    "（`ccf-YYYYMMDDTHHMMSSZ-xxxxxx`）⇒ 词法序 == 时间序。"
    "若生成侧改变 id 宽度/时区/后缀长度，此等价关系必须重新论证。",
    # ---- 第 6 轮（队长裁定：O3 改为「载体自述驱动」）----
    "O3 只对**快照图**（命名图 IRI 末段匹配快照 id 格式）判「必须有 `ccfSnapshotId` 主语」；"
    "非快照图的合法载体必须由**图内自述**证明：图内含 ≥1 个 `?s a owl:Ontology` 主语"
    "（真机 `…/graph/tbox` 由 7 个模块本体自述满足 ⇒ 归 `ontologyGraphs`，不参与 O3）。"
    "图 IRI 自身**并不**自述（真机实测：以图 IRI 为主语的 `owl:Ontology` 三元组 = 0 条）"
    "⇒ 判据只看**图内三元组**，绝不硬编码任何图名；未自述、又非快照图的命名图"
    "仍按 O3（0 主语 / 无该属性）或 O2（末段非法）报 ERROR，fail-closed 不变。",
    "`?s a owl:Ontology` 自述计数与其它 SPARQL 读数都取仓库**默认推理口径**：真机 `cc-ops`"
    "规则集 = owl2-rl、`rdfsSubClassReasoning=true`、`queryTimeout=0`（无超时）⇒ 读数可能含"
    "**推理三元组**（同一仓库不带 `GRAPH` 的闭包式读数显著大于逐命名图并集的断言式读数）。"
    "本判据只用「自述数 > 0 / = 0」这种粗粒度，且每图 `ontologySubjects` 都打印在报告与 `--json`"
    "（`graphs[].ontologySubjects`）供复核；若生成侧/Ontology 侧改变自述方式，此判据须重新论证。",
    "真机取证时 `…/graph/tbox` 可能正被外部施工者重载（本会话第 6 轮取证期间即如此）："
    "若该图在枚举时刻三元组为 0，它**不会出现在命名图清单里**，O3 于是不报 —— 这是**数据侧时序**"
    "而非判据放宽。报告必须标注取证时刻的 tbox 三元组数与 `owl:Class` / `owl:ObjectProperty` 计数。",
]


def build_report(cfg, kg, onto, errs, warns, stats, verdict, exit_code,
                 blocked=None, captured_at=None):
    rep = {
        "gate": "G19",
        "title": "图级快照身份一致性门禁",
        "verdict": verdict,
        "exitCode": int(exit_code),
        "capturedAt": captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": cfg.get("repo"),
        "boltUri": cfg.get("bolt_uri"),
        "database": cfg.get("database"),
        "labelPrefix": cfg.get("label_prefix"),
        "snapshotProp": cfg.get("snapshot_prop"),
        "gdbUrl": cfg.get("gdb_url"),
        "ontoPropIri": cfg.get("onto_prop_iri"),
        # KG 半：实际值 + 下限
        "metaCount": stats.get("metaCount"),
        "metaSnapshotId": stats.get("metaSnapshotId"),
        "kgNodes": stats.get("kgNodes"),
        "kgNodeFloor": stats.get("minKgNodes"),
        "kgNodeFloorMet": (stats.get("kgNodes") or 0) >= (stats.get("minKgNodes") or 0),
        "kgRels": stats.get("kgRels"),
        "kgRelFloor": stats.get("minKgRels"),
        "kgRelFloorMet": (stats.get("kgRels") or 0) >= (stats.get("minKgRels") or 0),
        "kgSnapshots": stats.get("kgSnapshots"),
        "kgLabelledNodes": stats.get("kgLabelledNodes"),
        "kgUnlabelledSubjects": stats.get("kgUnlabelledSubjects"),
        # ONTO 半：实际值 + 下限
        "ontoGraphs": stats.get("ontoGraphs"),
        "ontoSubjects": stats.get("ontoSubjects"),
        "ontoSubjectFloor": stats.get("minOntoSubjects"),
        "ontoSubjectFloorMet": (stats.get("ontoSubjects") or 0) >= (stats.get("minOntoSubjects") or 0),
        # 跨载体归类（O4 / W2）
        "currentSnapshotGraphs": stats.get("currentGraphCount"),
        "currentSnapshotGraphIris": stats.get("currentGraphIris"),
        "historyWhitelistedGraphs": stats.get("historyGraphCount"),
        "historyGraphIris": stats.get("historyGraphIris"),
        "unclassifiedGraphs": stats.get("unclassifiedGraphCount"),
        "unclassifiedGraphIris": stats.get("unclassifiedGraphIris"),
        # 载体自述的本体/模式图（O3 只对快照图判；这些图不参与 O3 的 ccfSnapshotId 要求）
        "ontologyGraphs": stats.get("ontologyGraphIris") or [],
        "ontologyGraphCount": stats.get("ontologyGraphCount"),
        "ontologyGraphDetails": stats.get("ontologyGraphs") or [],
        "allowHistory": stats.get("historyTails"),
        "strict": bool(stats.get("strict")),
        # 跨载体**时序**身份（O5）：两侧各自的 snapshotId + ONTO 命名图清单（快照时间序）
        "kgSnapshotId": stats.get("metaSnapshotId"),
        "latestOntoSnapshotId": stats.get("latestOntoSnapshotId"),
        "latestOntoGraphIri": stats.get("latestOntoGraphIri"),
        "crossCarrierAgree": bool(stats.get("crossCarrierAgree")),
        "ontoSnapshotCount": stats.get("ontoSnapshotCount"),
        "ontoSnapshotGraphs": stats.get("ontoSnapshotGraphs") or [],
        "ontoNonSnapshotGraphIris": stats.get("ontoNonSnapshotGraphIris") or [],
        # 反恒真合计（A1）
        "checkedAboxSubjects": stats.get("checkedAboxSubjects"),
        "minAboxSubjects": stats.get("minAboxSubjects"),
        "aggregateFloorMet": (stats.get("checkedAboxSubjects") or 0) >= (stats.get("minAboxSubjects") or 0),
        "graphs": stats.get("graphClasses") or [],
        "errors": list(errs or []),
        "warnings": list(warns or []),
        "notes": list(HONESTY_NOTES),
    }
    if kg is not None:
        rep["kgNodeSnaps"] = kg.get("nodeSnaps") or {}
        rep["kgRelSnaps"] = kg.get("relSnaps") or {}
    if blocked:
        rep["blocked"] = blocked
    return rep


def write_report_json(path, report):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(report, ensure_ascii=False, indent=2))
        fh.write("\n")
    return path


def verdict_of(errs):
    return "FAIL" if errs else "PASS"


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="G19 图级快照身份一致性门禁（只读）")
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--repo", default=None, help="ONTO 仓库名（默认取 targets.yaml，cc-ops）")
    ap.add_argument("--neo4j-uri", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--database", default=None)
    ap.add_argument("--min-abox-subjects", type=int, default=None,
                    help="A1 合计受检 ABox 主体下限（默认 %d；必须 > 0，0/负数 = 参数错误）"
                         % MIN_ABOX_SUBJECTS)
    ap.add_argument("--min-kg-nodes", type=int, default=None,
                    help="A2 KG 节点侧下限（默认 %d；必须 > 0）" % MIN_KG_NODES)
    ap.add_argument("--min-kg-rels", type=int, default=None,
                    help="A3 KG 关系侧下限（默认 %d；必须 > 0）" % MIN_KG_RELS)
    ap.add_argument("--min-onto-subjects", type=int, default=None,
                    help="A4 ONTO 主语侧下限（默认 %d；必须 > 0）" % MIN_ONTO_SUBJECTS)
    ap.add_argument("--allow-history", action="append", default=None,
                    metavar="TAIL[,TAIL...]",
                    help="历史快照命名图白名单（逗号分隔的 IRI 末段或整条 IRI；可重复）。"
                         "既非当前快照、又不在白名单的命名图 ⇒ W2 未归类（WARN）")
    ap.add_argument("--strict", action="store_true",
                    help="把 WARN 升为 ERROR（W1 未标注主体 / W2 未归类历史快照图）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结论（含每侧实际值与下限）")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="--selftest 时只打印汇总判定行")
    args = ap.parse_args(argv)
    cfg = load_cfg(args)

    if args.selftest:
        return selftest(cfg, quiet=args.quiet)

    # ★ 参数校验在**任何输出/采集之前**：0 / 负数 ⇒ 参数错误（exit 2），不是 PASS。
    perr = validate_params(cfg)
    if perr:
        rep = {"gate": "G19", "title": "图级快照身份一致性门禁",
               "verdict": "PARAM_ERROR", "exitCode": 2,
               "capturedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "repo": cfg["repo"],
               "floors": {k: cfg.get(k) for k, _f, _h in FLOOR_KEYS},
               "allowHistory": cfg.get("allow_history"),
               "errors": list(perr), "warnings": [], "notes": list(HONESTY_NOTES)}
        if args.out:
            write_report_json(args.out, rep)
        if args.json:
            sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
        else:
            for e in perr:
                print("[ERROR] %s" % e)
            print("RESULT: PARAM_ERROR（G19：参数错误，未做任何采集；exit=2）")
        return 2

    lines = []
    lines.append("=== G19 图级快照身份一致性门禁 ===")
    lines.append("KG  = %s（database=%s，标签前缀 %s）"
                 % (cfg["bolt_uri"], cfg["database"], cfg["label_prefix"]))
    lines.append("ONTO= %s 仓库 %s（属性 %s）"
                 % (cfg["gdb_url"], cfg["repo"], cfg["onto_prop_iri"]))
    lines.append("下限（分侧，各自 > 0）：合计 %d / KG 节点 %d / KG 关系 %d / ONTO 主语 %d%s"
                 % (cfg["min_abox_subjects"], cfg["min_kg_nodes"], cfg["min_kg_rels"],
                    cfg["min_onto_subjects"], "；--strict" if cfg["strict"] else ""))
    if cfg["allow_history"]:
        lines.append("--allow-history 白名单（末段）：%s" % sorted(history_tails(cfg)))

    try:
        kg = collect_kg(cfg, args.password)
    except Exception as exc:
        rep = {"gate": "G19", "verdict": "BLOCKED", "exitCode": 2,
               "capturedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "repo": cfg["repo"], "blocked": "KG 侧不可达：%s" % exc,
               "errors": ["BLOCKED KG 侧不可达：%s" % exc], "warnings": [],
               "notes": list(HONESTY_NOTES)}
        lines.append("[BLOCKED] KG 侧不可达：%s" % exc)
        lines.append("RESULT: BLOCKED（G19）")
        return _emit(args, rep, lines, 2)
    try:
        onto = collect_onto(cfg)
    except Exception as exc:
        rep = {"gate": "G19", "verdict": "BLOCKED", "exitCode": 2,
               "capturedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "repo": cfg["repo"], "blocked": "ONTO 侧不可达：%s" % exc,
               "errors": ["BLOCKED ONTO 侧不可达：%s" % exc], "warnings": [],
               "notes": list(HONESTY_NOTES)}
        lines.append("[BLOCKED] ONTO 侧不可达：%s" % exc)
        lines.append("RESULT: BLOCKED（G19）")
        return _emit(args, rep, lines, 2)

    errs, stats = evaluate(kg, onto, cfg, cfg["min_abox_subjects"])
    warns = stats["warnings"]
    cls_of = {c["iri"]: c["classification"] for c in stats["graphClasses"]}
    tag = {"current": "当前快照", "ontology": "本体图", "history": "历史白名单",
           "unclassified": "未归类"}
    lines.append("-- KG 半 --")
    lines.append("  :%s 节点数 = %d" % (cfg["meta_label"], stats["metaCount"]))
    lines.append("  :%s.%s = %r" % (cfg["meta_label"], cfg["meta_snapshot_field"],
                                    stats["metaSnapshotId"]))
    lines.append("  ABox 节点带 %s：%s" % (cfg["snapshot_prop"], kg.get("nodeSnaps") or {}))
    lines.append("  ABox 关系带 %s：%s" % (cfg["snapshot_prop"], kg.get("relSnaps") or {}))
    lines.append("  CC_ 前缀节点总数（含 TBox）= %d" % stats["kgLabelledNodes"])
    lines.append("  未标注主体数 = labelledNodes − kgNodes = %d − %d = %d%s"
                 % (stats["kgLabelledNodes"], stats["kgNodes"],
                    stats["kgUnlabelledSubjects"],
                    "（>0 只 WARN；--strict 升 ERROR）" if stats["kgUnlabelledSubjects"] > 0
                    else "（=0，无 W1）"))
    lines.append("  分侧下限：KG 节点 %d / %d %s；KG 关系 %d / %d %s"
                 % (stats["kgNodes"], stats["minKgNodes"],
                    "OK" if stats["kgNodes"] >= stats["minKgNodes"] else "BELOW",
                    stats["kgRels"], stats["minKgRels"],
                    "OK" if stats["kgRels"] >= stats["minKgRels"] else "BELOW"))
    lines.append("-- ONTO 半 --（快照图按 id 时间序升序；本体图（图内自述 owl:Ontology）次之；"
                 "未归类最后）")
    def _tail_of(g):
        return str(g["graph"]).rstrip("/").rsplit("/", 1)[-1]

    def _order(g):
        t = _tail_of(g)
        if SNAPSHOT_RE.match(t):
            return (0, t)
        if int(g.get("ontologySubjects") or 0) > 0:
            return (1, t)
        return (2, t)
    for g in sorted(onto, key=_order):
        iri = g["graph"]
        tail = iri.rstrip("/").rsplit("/", 1)[-1]
        cls_g = cls_of.get(iri, "unclassified")
        ok = (g["subjects"] > 0 and len(g["values"]) == 1 and g["values"][0] == tail
              and bool(SNAPSHOT_RE.match(tail)))
        mark = "OK" if ok else ("— （本体图，不适用）" if cls_g == "ontology" else "MISMATCH")
        lines.append("  %-72s 主语 %4d  取值 %s  末段=%s  [%s]  自述 owl:Ontology=%d  %s"
                     % (iri, g["subjects"], g["values"], tail,
                        tag.get(cls_g, "未归类"),
                        int(g.get("ontologySubjects") or 0), mark))
    lines.append("  分侧下限：ONTO 逐图主语合计 %d / %d %s"
                 % (stats["ontoSubjects"], stats["minOntoSubjects"],
                    "OK" if stats["ontoSubjects"] >= stats["minOntoSubjects"] else "BELOW"))
    lines.append("  图级归类：当前快照图命中 %d 张 / 本体图 %d 张（图内自述 `owl:Ontology`，"
                 "不参与 O3）/ 历史白名单命中 %d 张 / 未归类 %d 张"
                 % (stats["currentGraphCount"], stats["ontologyGraphCount"],
                    stats["historyGraphCount"], stats["unclassifiedGraphCount"]))
    lines.append("  跨载体身份：KG `:%s.%s` = %r ／ ONTO **最新**快照图 = %r%s ⇒ %s"
                 "（O4 查「存在」、O5 查「最新」；白名单不能救这两条）"
                 % (cfg["meta_label"], cfg["meta_snapshot_field"], stats["metaSnapshotId"],
                    stats["latestOntoSnapshotId"],
                    ("（图 %s）" % stats["latestOntoGraphIri"])
                    if stats["latestOntoGraphIri"] else "",
                    "一致" if stats["crossCarrierAgree"] else "**不一致**"))
    lines.append("-- 反恒真（分侧）--")
    lines.append("  检查到的 ABox 主体 = KG 节点 %d + KG 关系 %d + ONTO 逐图主语 %d = %d（下限 %d）"
                 % (stats["kgNodes"], stats["kgRels"], stats["ontoSubjects"],
                    stats["checkedAboxSubjects"], stats["minAboxSubjects"]))
    if warns:
        lines.append("-- 告警（WARN，默认不改变退出码；--strict 升 ERROR）--")
        for w in warns:
            lines.append("  [WARN] %s" % w)
    if errs:
        for e in errs:
            lines.append("  [ERROR] %s" % e)
    trio = ("当前快照图命中 %d 张 / 本体图 %d 张 / 历史白名单命中 %d 张 / 未归类 %d 张；"
            "ONTO 最新快照图 = %r（KG = %r）"
            % (stats["currentGraphCount"], stats["ontologyGraphCount"],
               stats["historyGraphCount"],
               stats["unclassifiedGraphCount"], stats["latestOntoSnapshotId"],
               stats["metaSnapshotId"]))
    if errs:
        lines.append("RESULT: FAIL（G19 图级快照身份一致性门禁：ERROR=%d / WARN=%d；%s）"
                     % (len(errs), len(warns), trio))
    else:
        lines.append("RESULT: PASS（G19 图级快照身份一致性门禁：KG 与 ONTO 两半身份自洽，"
                     "已检查 ABox 主体 %d 条；%s）" % (stats["checkedAboxSubjects"], trio))
    code = 1 if errs else 0
    rep = build_report(cfg, kg, onto, errs, warns, stats, verdict_of(errs), code)
    return _emit(args, rep, lines, code)


def _emit(args, rep, lines, code):
    """按 `--json` / `--out` 惯例输出（与 t10_native_parity.py 同风格）。"""
    if args.out:
        write_report_json(args.out, rep)
    if args.json:
        sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write("\n".join(lines) + "\n")
        if args.out:
            sys.stdout.write("JSON 报告：%s\n" % args.out)
    return code


if __name__ == "__main__":
    sys.exit(main())
