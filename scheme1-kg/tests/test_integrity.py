#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheme1-kg/tests/test_integrity.py — 方案1（KG+LLM）图完整性测试 T1–T9（docs/03 §9）

T10（跨方案内容等价）由 `shared/verify/equivalence.py` 负责，**不在此文件**（任务书明确分工）。

设计：
  · 纯标准库（json / urllib / base64 / re / os / sys），**只读**：仅发 EXPLAIN / MATCH / SHOW / CALL db.schema.*，
    绝不写库（本文件不含任何 CREATE/MERGE/DELETE/SET）。
  · 契约常量**不重复硬编码**：从只读静态校验器 `shared/verify/kg_tbox_check.py` 导入（同一份 YAML 解析结果），
    保证「静态校验」与「真机测试」对 14 类型 / 14 故障模式 / 18 因果边 / 12 阈值 / 70 指标的认知完全一致。
  · 逐条打印 `T1 PASS …` / `T1 FAIL …`，末尾汇总；退出码 0 = T1–T9 全 PASS。

环境变量（缺省即真机 5.26.30 社区版实例；共存的既有 v2 图与本项目 CC_ 图一律只读）：
  CC_NEO4J_HTTP     默认 http://127.0.0.1:7475   # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
  CC_NEO4J_USER     默认 neo4j
  CC_NEO4J_PASSWORD 必填（缺失即 fail-closed，不回落内置默认口令）
  CC_NEO4J_DB       默认 neo4j
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "shared", "verify"))
import kg_tbox_check as K  # noqa: E402  只读静态契约常量（见文件头说明）

HTTP = os.environ.get("CC_NEO4J_HTTP", "http://127.0.0.1:7475").rstrip("/")  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
USER = os.environ.get("CC_NEO4J_USER", "neo4j")
PWD = os.environ.get("CC_NEO4J_PASSWORD") or sys.exit("★ 缺少环境变量 CC_NEO4J_PASSWORD：fail-closed，绝不回落内置默认口令。请从生产机 /opt/upstream-agent/.env 的 NEO4J_AUTH=user/pass 取出口令，设入该变量后重跑。")
DB = os.environ.get("CC_NEO4J_DB", "neo4j")
ONT = os.path.join(ROOT, "scheme1-kg", "ontology")
QRY = os.path.join(ROOT, "scheme1-kg", "queries")

RESULTS = []
QUERY_FILES = [
    ("q01-topology.cypher", {"appCode": "APP-SAMPLE"}),
    ("q02-symptom-match.cypher", {"metricCodes": ["app.heap_used_ratio"]}),
    ("q03-downstream.cypher", {"fmCode": K.FM_IDS[0]}),
    ("q04-upstream.cypher", {"fmCode": K.FM_IDS[0]}),
    ("q05-affected.cypher", {"fmCode": None}),
    ("q06-criteria.cypher", {"fmCode": K.FM_IDS[0]}),
    ("q07-edge-props.cypher", {"fmCodes": None}),
    ("q08-provenance.cypher", {"factId": None, "recordId": None, "metricCode": None}),
]
# 查询里的列表变量（Q03/Q04 的 [h1..h4]/[r1..r4] 折叠结果）→ 元素类型，供 T5 属性解析用
LIST_VARS = {"hops": ["CC_FaultMode"], "rels": ["CC_CAUSES"]}


def cy(args, params=None, timeout=90):
    """执行一条只读 Cypher，返回 list[dict]（列名 → 值）。"""
    body = json.dumps({"statements": [{"statement": args, "parameters": params or {}}]}).encode("utf-8")
    token = base64.b64encode(("%s:%s" % (USER, PWD)).encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        "%s/db/%s/tx/commit" % (HTTP, DB),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:400]))
    if payload.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "") for e in payload["errors"]))
    rows = []
    for res in payload.get("results", []):
        cols = res.get("columns") or []
        for row in res.get("data") or []:
            rows.append(dict(zip(cols, row.get("row"))))
    return rows


def one(args, params=None):
    rows = cy(args, params)
    return list(rows[0].values())[0] if rows else None


def record(tid, ok, detail):
    RESULTS.append((tid, ok))
    print("%s %s %s" % (tid, "PASS" if ok else "FAIL", detail))


def read_query(name):
    with open(os.path.join(QRY, name), "r", encoding="utf-8") as fh:
        return fh.read()


# ── T1 每个契约类型都有 ≥1 实例 ────────────────────────────────────────────
def t1():
    want = list(K.CONCRETE_CODES)  # 12 具体业务类型 + Evidence（annotation 型）= 13
    rows = cy("UNWIND $labs AS lab CALL { WITH lab MATCH (n) WHERE lab IN labels(n) RETURN count(n) AS c } "
              "RETURN lab, c", {"labs": ["CC_" + c for c in want]})
    got = dict((r["lab"], r["c"]) for r in rows)
    zero = [c for c in want if not got.get("CC_" + c)]
    record("T1", not zero,
           "契约类型实例覆盖 %d/%d（缺实例：%s）" % (len(want) - len(zero), len(want), zero or "无"))


# ── T2 每个故障模式 ≥1 条 JUDGED_BY 出边 ──────────────────────────────────
def t2():
    have = set()
    for r in cy("MATCH (fm:CC_FaultMode)-[:CC_JUDGED_BY]->(m:CC_Metric) RETURN DISTINCT fm.fmCode AS c"):
        have.add(r["c"])
    miss = sorted(set(K.FM_IDS) - have)
    record("T2", not miss, "JUDGED_BY 覆盖 %d/%d 个故障模式（缺：%s）" % (len(have & set(K.FM_IDS)),
                                                                          len(K.FM_IDS), miss or "无"))


# ── T3 每个故障模式 ≥1 条 AFFECTS 出边，且目标标签 = CC_<fmComponentType> ──
def t3():
    rows = cy("MATCH (fm:CC_FaultMode)-[a:CC_AFFECTS]->(o) "
              "RETURN fm.fmCode AS fmCode, fm.fmComponentType AS ct, labels(o) AS labs, a.severity AS sev, "
              "fm.fmSeverity AS fmSev")
    have = set(r["fmCode"] for r in rows)
    miss = sorted(set(K.FM_IDS) - have)
    bad = []
    for r in rows:
        if ("CC_" + str(r["ct"])) not in (r["labs"] or []):
            bad.append("%s→%s(应为 CC_%s)" % (r["fmCode"], r["labs"], r["ct"]))
        if r["sev"] != r["fmSev"]:
            bad.append("%s severity %s≠%s" % (r["fmCode"], r["sev"], r["fmSev"]))
    record("T3", (not miss) and (not bad),
           "AFFECTS 覆盖 %d/%d；目标类型/严重度不符 %d 条%s" % (len(have & set(K.FM_IDS)), len(K.FM_IDS),
                                                                len(bad), ("：" + "; ".join(bad[:4])) if bad else ""))


# ── T4 因果边 provenance/contractId 非空且 ∈ 契约 ─────────────────────────
def t4():
    rows = cy("MATCH (a:CC_FaultMode)-[r:CC_CAUSES|CC_RELATES_TO]->(b:CC_FaultMode) "
              "RETURN a.fmCode AS f, b.fmCode AS t, type(r) AS rt, r.contractId AS cid, r.provenance AS prov")
    ids = set(K.EDGE_IDS)
    bad = []
    for r in rows:
        if not r["cid"] or r["cid"] not in ids:
            bad.append("%s→%s contractId=%r" % (r["f"], r["t"], r["cid"]))
        if not r["prov"]:
            bad.append("%s→%s provenance 为空" % (r["f"], r["t"]))
    record("T4", not bad, "因果边 %d 条，contractId/ provenance 不合规 %d 条%s"
           % (len(rows), len(bad), ("：" + "; ".join(bad[:4])) if bad else ""))


# ── T5 Q01–Q08 属性名真实存在（EXPLAIN + db.schema 属性集）───────────────
def t5():
    node_props, rel_props = {}, {}
    for r in cy("CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName"):
        for lab in (r["nodeLabels"] or []):
            node_props.setdefault(lab, set()).add(r["propertyName"])
    for r in cy("CALL db.schema.relTypeProperties() YIELD relType, propertyName"):
        # ★ 实测（tmp/diag_props.log）：本内核把 relType 回成 `` :`CC_JUDGED_BY` `` 形态
        #   （带冒号与反引号），必须归一化，否则属性集恒为空 → 误报「属性不存在」。
        rt = str(r["relType"] or "")
        rt = rt.replace("`", "").replace(":", "").replace("-", "").replace("[", "").replace("]", "").strip()
        rel_props.setdefault(rt, set()).add(r["propertyName"])

    explain_bad, prop_bad, checked, decl_only = [], [], 0, []
    for name, params in QUERY_FILES:
        text = read_query(name)
        try:
            cy("EXPLAIN " + text, params)
        except Exception as exc:  # noqa: BLE001  计划期错误即 FAIL
            explain_bad.append("%s: %s" % (name, str(exc)[:160]))
        node_var, rel_var = {}, {}
        for m in re.finditer(r"\(\s*(\w+)\s*:\s*(CC_\w+)", text):
            node_var.setdefault(m.group(1), set()).add(m.group(2))
        for m in re.finditer(r"\[\s*(\w+)\s*:\s*(CC_\w+)", text):
            rel_var.setdefault(m.group(1), set()).add(m.group(2))
        for m in re.finditer(r"\b([A-Za-z_]\w*)\.(\w+)\b", text):
            var, prop = m.group(1), m.group(2)
            if var in LIST_VARS:
                labs = LIST_VARS[var]
            elif var in node_var:
                labs = sorted(node_var[var])
            elif var in rel_var:
                labs = None
                rts = sorted(rel_var[var])
            else:
                continue  # 未标标签的变量（如 Q05 的 obj、Q08 的 fact）无法静态定型，跳过
            checked += 1
            if labs is None:
                for rt in rts:
                    if prop not in rel_props.get(rt, set()):
                        prop_bad.append("%s: %s.%s（关系 %s 的属性集 %s）"
                                        % (name, var, prop, rt, sorted(rel_props.get(rt, set()))))
            else:
                pool = set()
                for lab in labs:
                    pool |= node_props.get(lab, set())
                # ★ 判据 = 属性必须「图上已落值」**或**「类型契约已声明」。
                #   理由：`ev.reason` 由 type-map 的 Evidence attrs 声明（契约属性），
                #   但 ABox 只在观测带 reason 时才写它（首轮 T5 因「图上无值」误判为不存在）。
                #   真正要挡住的是**契约与图都没有**的名字（拼错/漂移）⇒ 归入 not_found。
                contract = set()
                for lab in labs:
                    code = lab[len("CC_"):] if lab.startswith("CC_") else lab
                    contract |= K.TYPE_ATTRS.get(code, set())
                    contract |= set(p for p in K.METRICS_BY_OBJECT.get(code, set()) if p)
                if prop not in pool and prop not in contract:
                    prop_bad.append("%s: %s.%s（%s 的图上属性集 %s）" % (name, var, prop, labs, sorted(pool)))
                elif prop not in pool:
                    decl_only.append("%s.%s" % (var, prop))
    record("T5", (not explain_bad) and (not prop_bad),
           "EXPLAIN 8/8%s；属性引用 %d 处，契约与图都不存在 %d 处%s；契约已声明但图上未落值 %d 处%s"
           % ("（失败 %d）" % len(explain_bad) if explain_bad else "",
              checked, len(prop_bad),
              ("：" + " | ".join((explain_bad + prop_bad)[:4])) if (explain_bad or prop_bad) else "",
              len(decl_only), ("（%s）" % ", ".join(sorted(set(decl_only))[:4])) if decl_only else ""))


# ── T6 有向环 ⊆ 契约白名单（meta.cycles）────────────────────────────────
def t6():
    rows = cy("MATCH p = (n:CC_FaultMode)-[:CC_CAUSES*2..4]->(n) "
              "RETURN DISTINCT [x IN nodes(p) | x.fmCode] AS cycle")
    whitelist = [set(c) for c in K.CAUSAL_CYCLES]
    cycles = [set(r["cycle"]) for r in rows]
    bad = sorted([sorted(c) for c in cycles if not any(c <= w for w in whitelist)])
    record("T6", not bad, "检出有向环 %d 个，非白名单 %d 个%s（白名单 %s）"
           % (len(cycles), len(bad), ("：" + str(bad[:3])) if bad else "",
              [sorted(w) for w in whitelist]))


# ── T7 索引存在且不是死索引（判据 = **契约 ∪ 图**）────────────────────────
def t7():
    expect = ["cc_idx_fm_layer_idx", "cc_idx_fm_severity_idx", "cc_idx_metric_obj_idx",
              "cc_idx_query_elapsed_idx", "cc_idx_session_state_idx", "cc_idx_causes_edge_id_idx"]
    rows = cy("SHOW INDEXES YIELD name, entityType, labelsOrTypes, properties, state")
    idx = dict((r["name"], r) for r in rows if str(r["name"]).startswith("cc_idx_"))
    missing = [n for n in expect if n not in idx]
    # 关系索引的「契约声明属性」= causal-edges.yaml 各边的**键集合**
    # （id/from/relation/to/chain/evidence/provenance，条件边另有 condition）；图上侧的落库名
    # 是 `contractId`（= 契约 `id`）/`description`（= 契约 `evidence`）/`cond*`。
    # ★ 本索引建在 `r.contractId` 上：该名不在契约键集合内 ⇒ 判活完全靠**图上侧非空计数**
    #   （= captain 裁定的「契约 ∪ 图」中的图半边）；若 contractId 也丢了，索引即被判死。
    rel_pool = set()
    for _e in K.EDGE_LIST:
        rel_pool.update(_e.keys())
    dead, warned, details = [], [], []
    for name in expect:
        r = idx.get(name)
        if not r:
            continue
        props = r["properties"] or []
        if not props:
            dead.append(name)
            details.append("%s 无属性" % name)
            continue
        # ★ 死索引判据（口径 = **契约 ∪ 图**，captain 裁定）：
        #   前导属性**既不在契约声明**（节点：type-map.yaml 的 `key_attrs`/`attrs` 与
        #   `metrics[].kg_property` 按该标签的对象类型；关系：causal-edges.yaml 的边属性）
        #   **也不在图上**（非空值 0）⇒ 属 TBox 编写错误 ⇒ FAIL。
        #   若属性**在契约中已声明**但当前快照零取值 ⇒ 属**采集覆盖缺口**（例如
        #   `shared/model/v3-ccf-map.yaml#path_to_metric` 未给该指标采集路径，该文件 `:30` 自述）
        #   ⇒ **WARN + 台账条目「零填报指标」，不得 FAIL**。
        #   理由（captain）：docs/03:85 的 `cc_idx_query_elapsed_idx` 建在契约属性 `meanExecMs`
        #   （type-map.yaml:493-497，metricCode `db.query.mean_exec_ms`）上，索引本身无错，
        #   零取值的根因是采集覆盖，不是 TBox 编写错误。
        #   Neo4j 5 的 RANGE 索引支持**前导属性**前缀用法（WHERE n.a = … 可只用 (a,b) 索引的 a），
        #   故只看前导属性即可；若要求「全部属性都非空」，会把 (state, waitEvent) 这类合理的
        #   前缀可用索引误判为死（首轮 T7 即如此误报，见 tmp/diag_props.log：state=18、waitEvent=0）。
        lab = (r["labelsOrTypes"] or [""])[0]
        code = lab[len(K.LABEL_PREFIX):] if lab.startswith(K.LABEL_PREFIX) else lab
        if r["entityType"] == "RELATIONSHIP":
            pool = set(rel_pool)
        else:
            pool = set(K.TYPE_ATTRS.get(code, set())) | set(K.METRICS_BY_OBJECT.get(code, set()))
        counts = []
        for p in props:
            if r["entityType"] == "RELATIONSHIP":
                counts.append(one("MATCH ()-[x:%s]->() WHERE x.%s IS NOT NULL RETURN count(x) AS c"
                                  % (r["labelsOrTypes"][0], p)))
            else:
                counts.append(one("MATCH (n:%s) WHERE n.%s IS NOT NULL RETURN count(n) AS c"
                                  % (":".join(r["labelsOrTypes"]), p)))
        details.append("%s[%s]=%s" % (name.replace("cc_idx_", "").replace("_idx", ""),
                                      ",".join(props), "/".join(str(c) for c in counts)))
        if not counts[0]:
            if props[0] in pool:
                warned.append("%s.%s（契约已声明，快照零取值）" % (lab, props[0]))
            else:
                dead.append(name)
    for w in warned:
        print("[台账] 零填报指标 %s —— 采集覆盖缺口，TBox 声明与索引均无错；待采集层补路径后复测" % w)
    record("T7", (not missing) and (not dead),
           "cc_idx_* 索引 %d/%d；缺 %s；死索引（契约与图都没有该属性）%s；零填报指标（WARN，不计 FAIL）%s"
           "（各索引按属性序非空计数：%s）"
           % (len(idx), len(expect), missing or "无", dead or "无", warned or "无", ", ".join(details)))


# ── T8 CC_Meta 单例 + snapshotId ─────────────────────────────────────────
def t8():
    n = one("MATCH (m:CC_Meta) RETURN count(m) AS c")
    row = cy("MATCH (m:CC_Meta) RETURN m.key AS k, m.schemaVersion AS v, m.snapshotId AS s, "
             "m.contractRef AS cr, m.ccfVersion AS cv")
    r = row[0] if row else {}
    ok = (n == 1) and r.get("k") == "graph" and bool(r.get("s"))
    record("T8", ok, "CC_Meta 节点数=%s（须 1）、key=%r、snapshotId=%r、schemaVersion=%r、ccfVersion=%r"
           % (n, r.get("k"), r.get("s"), r.get("v"), r.get("cv")))


# ── T9 规则零数字字面量 + 阈值引用必带档位且可解析 ───────────────────────
def t9():
    rules = os.path.join(ONT, "04-rules.cypher")
    with open(rules, "r", encoding="utf-8") as fh:
        text = fh.read()
    stmts, _ = K.cypher_statements(rules)
    bad = []
    for line0, body in stmts:
        residue = list(body)
        for m in K.CMP_NUM_RE.finditer(body):
            _var, prop, _op, num = m.groups()
            if prop in K.NON_ZERO_PROPS and num == "0":
                for i in range(m.start(), m.end()):
                    residue[i] = " "
        res = "".join(residue)
        for m in K.NUM_RE.finditer(res):
            bad.append("L%s:%s" % (line0, m.group(1)))
    refs = set((m.group(1), m.group(2)) for m in K.THR_REF_RE.finditer(text))
    nontier = [m.group(0) for m in re.finditer(r"threshold:([a-z0-9_]+)(?![\w#])", text)]
    # ★ 规则体里的**阈值个体引用**（`(th:CC_Threshold {thrCode:'…'})`）也要能解析到图上节点：
    #   这类引用没有 `threshold:…#<档位>` 形态，只查 THR_REF_RE 会漏（首轮即漏掉 GAP-DOC-07
    #   的 `dead_tuple_ratio_light`）⇒ 一并纳入可解析性检查。
    codes = sorted(set(re.findall(r"CC_Threshold\s*\{[^}]*thrCode\s*:\s*'([^']+)'",
                                  "\n".join(b for _l, b in stmts))))
    #   ★ 只在**可执行语句体**里扫（K.cypher_statements 已剥注释）：否则会命中注释里的
    #     占位符 `{thrCode:'<名称>'}`（首轮即把占位符当成悬空引用，见 tmp/test_integrity3.log）。
    names = sorted(set(n for n, _t in refs))
    rows = cy("MATCH (th:CC_Threshold) WHERE th.thrCode IN $names "
              "RETURN th.thrCode AS c, th.thrWarning AS w, th.thrCritical AS k",
              {"names": sorted(set(names) | set(codes))})
    live = dict((r["c"], r) for r in rows)
    TIER_COL = {"warning": "w", "critical": "k"}   # ★ 查回列名是 w/k，首轮误用档位名取值 → KeyError 'warning'
    unresolved = sorted(set([n for n in names if n not in live] + [c for c in codes if c not in live]))
    uncal = ["%s#%s" % (n, t) for n, t in sorted(refs) if n in live and live[n][TIER_COL[t]] is None]
    # ★ 阈值节点**集合相等**：图上 `CC_Threshold` 必须恰等于契约 `thresholds`（v1.0.2 起 = 12 个）。
    #   数量取自契约常量 K.THR_NAMES（与静态校验器 C12 同源），不硬编码数字。
    graph_thr = sorted(r["c"] for r in cy("MATCH (th:CC_Threshold) RETURN th.thrCode AS c"))
    thr_missing = sorted(set(K.THR_NAMES) - set(graph_thr))
    thr_extra = sorted(set(graph_thr) - set(K.THR_NAMES))
    ok = (not bad) and (not nontier) and (not unresolved) and (not thr_missing) and (not thr_extra)
    record("T9", ok, "规则数字字面量 %d 处；带档位阈值引用 %d 个（无档位 %d）；thrCode 个体引用 %d 个；"
                     "指向图上不存在的阈值 %d %s；命中未标定档位 %d %s；阈值节点 %d 个（契约 %d；缺 %s；多 %s）"
           % (len(bad), len(refs), len(nontier), len(codes), len(unresolved),
              ("（悬空：%s）" % ", ".join(unresolved)) if unresolved else "",
              len(uncal),
              ("（未标定：%s，与契约同为 null ⇒ 不可机判）" % ", ".join(uncal)) if uncal else "",
              len(graph_thr), len(K.THR_NAMES), thr_missing or "无", thr_extra or "无"))


TESTS = [("T1", t1), ("T2", t2), ("T3", t3), ("T4", t4), ("T5", t5), ("T6", t6), ("T7", t7), ("T8", t8), ("T9", t9)]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print("=" * 90)
    print("方案1 图完整性测试 T1–T9（docs/03 §9）  目标：%s  数据库：%s" % (HTTP, DB))
    print("契约常量来源：shared/verify/kg_tbox_check.py（14 类型 / 14 故障模式 / 18 因果边 / 12 阈值 / 70 指标）")
    print("-" * 90)
    for tid, fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            record(tid, False, "执行异常：%s" % str(exc)[:300])
    print("-" * 90)
    passed = sum(1 for _t, ok in RESULTS if ok)
    failed = [t for t, ok in RESULTS if not ok]
    print("T1–T9 结果：%d/9 PASS%s" % (passed, "" if not failed else "，FAIL：%s" % failed))
    print("RESULT: %s" % ("ALL PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
