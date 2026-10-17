# -*- coding: utf-8 -*-
r"""契约门禁：G3 阈值来源 / G4 因果图 / G5 防空图 / G7 可达性
+ G18（内部编号 X17）故障词表跨契约引用可解析性 + 契约自洽。
只读 shared/model/ 契约文件，不依赖真机、Dify、GaussDB、Neo4j、GraphDB。
用法: python shared/verify/contract_gates.py [--model-dir <契约目录>]
  --model-dir 仅供 G18 判别力自证在不改动真契约的前提下跑突变副本，默认 shared/model。
"""
import difflib, hashlib, io, json, os, re, sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL = os.path.join(ROOT, "shared", "model")
# ★ G18（X17）判别力自证：在不改动真契约的前提下跑突变副本 ⇒ 允许显式指定契约目录。
#   默认仍是 shared/model；它**只换读取根，不放宽任何检查**（同一份代码、同一套断言）。
if "--model-dir" in sys.argv:
    _k = sys.argv.index("--model-dir")
    if _k + 1 >= len(sys.argv) or not os.path.isdir(sys.argv[_k + 1]):
        print("FATAL: --model-dir needs an existing directory argument")
        sys.exit(2)
    MODEL = os.path.abspath(sys.argv[_k + 1])

try:
    import yaml
except ImportError:
    print("FATAL: 需要 PyYAML"); sys.exit(2)

F = []          # (gate, severity, code, message)
def add(gate, sev, msg): F.append((gate, sev, msg))

def rd(n):
    return io.open(os.path.join(MODEL, n), encoding="utf-8", newline="").read()
def y(n):
    return yaml.safe_load(rd(n))

T   = y("topology.yaml")
FM  = y("fault-modes.yaml")
CE  = y("causal-edges.yaml")
MT  = y("metrics.yaml")
IP  = y("info-products.yaml")
KT  = y("kgt-tools.yaml")
KV  = y("kgt-prompt-variants.yaml")
CCF = json.loads(rd("ccf.schema.json"))

# ---------------- 索引 ----------------
types    = {t["code"]: t for t in T["types"]}
layers   = {l["code"]: l for l in T["layers"]}
rel      = {r["code"]: r for r in T["relations"]}
fms      = {f["code"]: f for f in FM["fault_modes"]}
thr      = {x["name"]: x for x in FM["thresholds"]}
metrics  = {m["code"]: m for m in MT["metrics"]}
metset   = set(metrics)
nonabs   = {c for c, t in types.items() if not t.get("abstract")}
layer_of = {c: t["layer"] for c, t in types.items()}
attrs_of = {c: set((t.get("attrs") or {}).keys()) for c, t in types.items()}
# ★ M1.5c（2026-10-05）：类型与关系分成「CCF 承载」与「载体派生」两类。
#   · annotation: true（Evidence）= 派生型：不出现在 CCF objects[]，由 observations[] 派生
#     ⇒ **不得**出现在 ccf.schema.json 的 typeCode enum / x-id-prefix-by-type 里
#   · asserted_in_ccf: false（HAS_EVIDENCE）= 不由 CCF relations[] 断言
#     ⇒ **不得**出现在 ccf.schema.json 的 relationCode enum 里
#   这两条只对「CCF 承载」的那一侧做集合等式校验，否则门禁会把正确的新契约判成错。
objtypes     = {c for c, t in types.items()
                if not t.get("abstract") and not t.get("annotation")}
annotations  = {c for c, t in types.items() if t.get("annotation")}
derived_rel  = {c for c, r in rel.items() if r.get("asserted_in_ccf") is False}
asserted_rel = {c for c in rel if c not in derived_rel}
ANY_TYPE = "*"   # 域不受限（HAS_EVIDENCE.from）

def chk_dup(codes, what, gate="X"):
    d = [k for k, v in Counter(codes).items() if v > 1]
    if d: add(gate, "ERROR", "%s 有重复 code: %s" % (what, d))

chk_dup([t["code"] for t in T["types"]], "topology.types")
chk_dup([r["code"] for r in T["relations"]], "topology.relations")
chk_dup([f["code"] for f in FM["fault_modes"]], "fault_modes")
chk_dup([m["code"] for m in MT["metrics"]], "metrics")
chk_dup([e["id"] for e in CE["edges"]], "causal_edges.id")
chk_dup([x["name"] for x in FM["thresholds"]], "thresholds.name")

invrel = {r["code"]: r for r in T.get("inverse_relations", [])}
allrel = dict(rel); allrel.update(invrel)

# ---------------- X: 契约自洽 ----------------
D = CCF["$defs"]
for k in ("typeCode", "layerCode", "relationCode", "metricCode"):
    if "enum" not in D.get(k, {}):
        add("X1", "ERROR", "ccf.schema.json $defs.%s 缺 enum" % k)

if set(D["typeCode"]["enum"]) != objtypes:
    add("X1", "ERROR", "typeCode enum 与 topology CCF 对象类型不一致: 缺 %s / 多 %s"
        % (sorted(objtypes - set(D["typeCode"]["enum"])), sorted(set(D["typeCode"]["enum"]) - objtypes)))
if set(D["layerCode"]["enum"]) != set(layers):
    add("X1", "ERROR", "layerCode enum 与 topology.layer_enum 不一致")
if set(D["relationCode"]["enum"]) != asserted_rel:
    add("X1", "ERROR", "relationCode enum 与 topology CCF 断言关系不一致: 缺 %s / 多 %s"
        % (sorted(asserted_rel - set(D["relationCode"]["enum"])), sorted(set(D["relationCode"]["enum"]) - asserted_rel)))
if set(D["metricCode"]["enum"]) != metset:
    add("X1", "ERROR", "metricCode enum 与 metrics.yaml 不一致: 缺 %s / 多 %s"
        % (sorted(metset - set(D["metricCode"]["enum"])), sorted(set(D["metricCode"]["enum"]) - metset)))
if set(CCF.get("x-id-prefix-by-type", {})) != objtypes:
    add("X1", "ERROR", "x-id-prefix-by-type 键集 != CCF 对象类型: 缺 %s / 多 %s"
        % (sorted(objtypes - set(CCF.get("x-id-prefix-by-type", {}))),
           sorted(set(CCF.get("x-id-prefix-by-type", {})) - objtypes)))
# ★ M1.5c 反向校验：派生型必须**确实**是派生（有 annotation/派生关系），
#   且派生关系必须自带派生来源说明，避免「用 annotation 绕过 schema 校验」。
for c in annotations:
    if types[c].get("layer") is not None:
        add("X1", "ERROR", "派生型 %s 的 layer 必须是 null（它不属任何业务层）" % c)
for c in derived_rel:
    if not rel[c].get("derived_from"):
        add("X1", "ERROR", "关系 %s 不由 CCF relations[] 断言，但缺 derived_from 说明" % c)
    if rel[c]["to"] not in annotations:
        add("X1", "ERROR", "派生关系 %s 的 to=%s 不是派生型" % (c, rel[c]["to"]))
for m in CCF.get("x-categorical-metrics", []):
    if m not in metset: add("X1", "ERROR", "x-categorical-metrics 含未定义指标 %s" % m)

# X16（★ M1.5c）：**深度口径**必须与契约几何自洽。
#   M1.5c 把 topology_facts.max_hop_depth 由 9 升到 10（Table → 其 Evidence 节点 +1 跳），
#   但**对象拓扑遍历不含派生注释边**：它走的是 hop_path 那 9 跳业务链。
#   若工具面的硬上限与 hop_path 脱钩，就会长出两个「最大深度」的说法（正是
#   docs/09 原文把二者当成同一个数的成因），故在此机检死。
_hop_path = (T.get("topology_facts") or {}).get("hop_path") or []
_biz_depth = len(_hop_path) - 1
_tool = None
for _t in (KT.get("tools") or []):
    if _t.get("tool_name") == "get_object_topology":
        _tool = _t
if _tool is None:
    add("X16", "ERROR", "kgt-tools.yaml 缺 get_object_topology 工具定义")
else:
    _hl = (_tool.get("hard_limits") or {}).get("maxHopDepth")
    if _hl != _biz_depth:
        add("X16", "ERROR", "get_object_topology.hard_limits.maxHopDepth=%r，但契约业务链深度 = len(hop_path)-1 = %d"
            % (_hl, _biz_depth))
    _df = (_tool.get("defaults") or {}).get("maxHopDepth")
    if _df != _hl:
        add("X16", "ERROR", "get_object_topology 默认 maxHopDepth=%r 与硬上限 %r 不一致" % (_df, _hl))
    _ts = _tool.get("traversal_scope") or {}
    if _ts.get("includes_relations") != "business_only":
        add("X16", "ERROR", "get_object_topology.traversal_scope.includes_relations 必须是 business_only"
            "（实际 %r）" % _ts.get("includes_relations"))
    if set(_ts.get("excludes_derived_relations") or []) != derived_rel:
        add("X16", "ERROR", "get_object_topology.traversal_scope.excludes_derived_relations 必须 == 派生关系集 %s（实际 %s）"
            % (sorted(derived_rel), sorted(set(_ts.get("excludes_derived_relations") or []))))
    for _p in (_tool.get("must_reach_paths") or []):
        for _c in derived_rel:
            if _c in _p:
                add("X16", "ERROR", "must_reach_paths 含派生注释关系 %s ⇒ 对象拓扑会退回「证据即业务对象」：%s" % (_c, _p))

# G5：防空图（静态部分）
for c in nonabs:
    if not (attrs_of[c]):
        add("G5", "ERROR", "类型 %s 无任何属性（空节点风险）" % c)
    if not [k for k in (types[c].get("key_attrs") or []) if k in attrs_of[c]]:
        add("G5", "ERROR", "类型 %s 无有效 key_attrs" % c)
    if not [r for r in rel.values() if r["from"] == c or r["to"] == c]:
        add("G5", "ERROR", "类型 %s 未参与任何关系（孤立类型 ⇒ 拓扑图空）" % c)
for e in CE["edges"]:
    for k in ("from", "to"):
        if e[k] not in fms: add("G5", "ERROR", "因果边 %s 的 %s=%s 不是已定义故障模式" % (e["id"], k, e[k]))
for f in FM["fault_modes"]:
    det = f.get("detect") or {}
    if not (det.get("all_of") or det.get("any_of")):
        add("G5", "ERROR", "故障模式 %s 无任何检测条件 ⇒ 永远不可达" % f["code"])

# X：关系一致性
for c, r in rel.items():
    for k in ("from", "to"):
        if r[k] == ANY_TYPE:
            continue          # ★ M1.5c：域不受限（HAS_EVIDENCE.from = "*"）
        if r[k] not in types: add("X7", "ERROR", "关系 %s 的 %s=%s 不是已定义类型" % (c, k, r[k]))
    _fl, _tl = layer_of.get(r["from"]), layer_of.get(r["to"])
    if r["from"] == ANY_TYPE or _fl is None or _tl is None:
        # 端点层不可判（域不受限 / 派生型无层）⇒ 只能要求它不得标成跨层
        if r.get("cross_layer"):
            add("X8", "ERROR", "关系 %s 的端点层不可判（%s→%s），不得标 cross_layer: true"
                % (c, r["from"], r["to"]))
    elif r.get("cross_layer") != (_fl != _tl):
        add("X8", "ERROR", "关系 %s cross_layer=%s 与两端层(%s→%s)不符"
            % (c, r.get("cross_layer"), _fl, _tl))
    inv = r.get("inverse")
    if inv:
        if inv not in allrel:
            add("X7", "ERROR", "关系 %s 声明 inverse=%s 但该名既不在 relations 也不在 inverse_relations（悬空声明）" % (c, inv))
        elif allrel[inv].get("inverse") != c:
            add("X7", "WARN", "inverse 非互指: %s.inverse=%s 但 %s.inverse=%s" % (c, inv, inv, allrel[inv].get("inverse")))
    card = r.get("cardinality")
    if not isinstance(card, str):
        add("X10", "ERROR", "关系 %s 的 cardinality=%r 不是字符串（YAML 把 1:1 解析成了整数 61 ⇒ 基数语义丢失）" % (c, card))
    elif not re.fullmatch(r"[1NM]:[1NM]", card):
        add("X10", "WARN", "关系 %s 的 cardinality=%r 不符合 <1|N|M>:<1|N|M> 形式" % (c, card))

# X7b：inverse_relations 登记块（派生反向关系，不得被 CCF 断言）
for c, r in invrel.items():
    for k in ("from", "to"):
        if r.get(k) not in types:
            add("X7", "ERROR", "inverse_relations %s 的 %s=%s 不是已定义类型" % (c, k, r.get(k)))
    if r.get("asserted") is not False:
        add("X7", "ERROR", "inverse_relations %s 的 asserted=%r ⇒ 必须为 false（否则同一事实被 CCF 断言两次）" % (c, r.get("asserted")))
    if r.get("inverse") not in rel:
        add("X7", "ERROR", "inverse_relations %s 的 inverse=%s 不在 relations 中" % (c, r.get("inverse")))
    elif rel[r["inverse"]].get("inverse") != c:
        add("X7", "ERROR", "inverse_relations %s 与 relations 的 %s 未互指" % (c, r["inverse"]))
    if c in rel:
        add("X7", "ERROR", "inverse_relations %s 与 relations 中的同名关系冲突" % c)

# X11：disjoint_with
import collections
dw = collections.defaultdict(set)
for c, t in types.items():
    v = t.get("disjoint_with") or []
    if isinstance(v, str): v = [v]
    for x in v: dw[c].add(x)
for a, bs in dw.items():
    for b in bs:
        if b not in types: add("X11", "ERROR", "类型 %s 的 disjoint_with 含未定义类型 %s" % (a, b))
        elif a not in dw.get(b, set()): add("X11", "ERROR", "disjoint_with 非对称: %s⊣%s 但 %s 未反向声明" % (a, b, b))

# X12/X14：metrics 与 fault_modes 归属
for m in MT["metrics"]:
    if m["object"] not in types: add("X12", "ERROR", "指标 %s 的 object=%s 不是已定义类型" % (m["code"], m["object"]))
    elif layer_of[m["object"]] != m["layer"]:
        add("X12", "ERROR", "指标 %s layer=%s 与 object %s 所在层 %s 不符" % (m["code"], m["layer"], m["object"], layer_of[m["object"]]))
    if m.get("source") not in ("gaussdb", "os", "app", "container", "derived"):
        add("X12", "ERROR", "指标 %s 的 source=%r 非法" % (m["code"], m.get("source")))
for f in FM["fault_modes"]:
    if f["component_type"] not in types:
        add("X14", "ERROR", "故障模式 %s 的 component_type=%s 未定义" % (f["code"], f["component_type"]))
    elif layer_of[f["component_type"]] != f["layer"]:
        add("X14", "ERROR", "故障模式 %s layer=%s 与 component_type 所在层 %s 不符"
            % (f["code"], f["layer"], layer_of[f["component_type"]]))

# X9：topology_facts 计数
tf = T.get("topology_facts", {})
for key, actual in (("type_count", len(T["types"])), ("relation_count", len(T["relations"])),
                    ("inverse_relation_count", len(invrel)),
                    ("cross_layer_edge_count", sum(1 for r in rel.values() if r.get("cross_layer")))):
    if tf.get(key) != actual:
        add("X9", "ERROR", "topology_facts.%s=%s 与实际 %d 不符" % (key, tf.get(key), actual))

# ---------------- G3：阈值来源（规则零数字字面量） ----------------
REF = re.compile(r"^(entity_property|threshold):([\w.]+?)(?:#(warning|critical))?$")
def chk_operand(op, where):
    if isinstance(op, bool):
        add("G3", "ERROR", "%s 的 operand 是布尔字面量 %r" % (where, op)); return
    if isinstance(op, (int, float)):
        add("G3", "ERROR", "%s 的 operand 是数字字面量 %r ⇒ 违反 hardcode_forbidden（应为 threshold:<名>#<档位> 或 entity_property:<类型>.<属性>）" % (where, op)); return
    if isinstance(op, list):
        for x in op: chk_operand(x, where); return
    if not isinstance(op, str):
        add("G3", "ERROR", "%s 的 operand 类型非法 %r" % (where, op)); return
    m = REF.match(op)
    if not m:
        add("G3", "WARN", "%s 的 operand=%r 不是标准引用形式（枚举值可忽略本警告）" % (where, op)); return
    kind, val, lvl = m.groups()
    if kind == "threshold":
        if val not in thr:
            add("G3", "ERROR", "%s 引用 threshold:%s 但 fault-modes.yaml#thresholds 未定义（悬空引用）" % (where, val))
        elif lvl is None:
            add("G3", "ERROR", "%s 的 threshold:%s 未写档位 ⇒ 必须写成 threshold:<名称>#<warning|critical>（档位不得靠隐含约定）" % (where, val))
        elif thr[val].get(lvl) is None:
            add("G3", "WARN", "%s 引用 threshold:%s#%s 但该档值未标定（null）⇒ 该条件当前不可机判" % (where, val, lvl))
    else:
        if lvl is not None:
            add("G3", "ERROR", "%s 的 entity_property 引用不得带 #档位: %r" % (where, op)); return
        if "." not in val: add("G3", "ERROR", "%s 的 entity_property 引用 %r 缺 <类型>.<属性> 形式" % (where, val)); return
        ty, at = val.split(".", 1)
        if ty not in types: add("G3", "ERROR", "%s 引用 entity_property:%s 但类型不存在" % (where, val)); return
        if at not in attrs_of[ty]:
            add("G3", "ERROR", "%s 引用 entity_property:%s 但 %s 无此属性（现有: %s）" % (where, val, ty, sorted(attrs_of[ty])))

PRED = set(FM["meta"].get("predicate_enum") or [])
def chk_cond(c, where):
    """条件分三类（fault-modes.yaml v1.0.1）：阈值/属性引用 | 符号谓词 | 取值枚举。"""
    if not isinstance(c, dict):
        add("G3", "ERROR", "%s 条件不是映射: %r" % (where, c)); return
    m = c.get("metric")
    if m not in metset:
        add("G3", "ERROR", "%s 引用未定义指标 %s" % (where, m))
    has_pred, has_op = ("predicate" in c), ("op" in c or "operand" in c)
    if has_pred and has_op:
        add("G3", "ERROR", "%s 同时给出 predicate 与 op/operand ⇒ 两类条件互斥" % where); return
    if has_pred:
        p = c["predicate"]
        if p not in PRED:
            add("G3", "ERROR", "%s 的 predicate=%r 不在 meta.predicate_enum %s" % (where, p, sorted(PRED)))
        if p == "in_enum" and not isinstance(c.get("values"), list):
            add("G3", "ERROR", "%s 用 predicate=in_enum 但缺 values 列表" % where)
        if p != "in_enum" and "values" in c:
            add("G3", "WARN", "%s 的 predicate=%s 非 in_enum 却带 values" % (where, p))
    elif has_op:
        chk_operand(c.get("operand"), "[%s] %s" % (m, where))
    else:
        add("G3", "ERROR", "%s 既无 predicate 也无 op/operand（条件不完整）" % where)

for f in FM["fault_modes"]:
    det = f.get("detect") or {}
    for k in ("all_of", "any_of"):
        for c in det.get(k) or []:
            chk_cond(c, "%s.detect.%s" % (f["code"], k))
    for d in f.get("distinguishing_evidence") or []:
        chk_cond(d, "%s.distinguishing_evidence" % f["code"])
for e in CE["edges"]:
    cond = e.get("condition")
    if isinstance(cond, dict):
        chk_cond(cond, "edge %s.condition" % e["id"])
for x in FM["thresholds"]:
    if x.get("metric") not in metset:
        add("G3", "ERROR", "threshold:%s 绑定未定义指标 %s" % (x["name"], x.get("metric")))
    if x.get("warning") is None and x.get("critical") is None and not x.get("rationale"):
        add("G3", "ERROR", "threshold:%s 无阈值且无 rationale" % x["name"])

# ---------------- G4：因果图 ----------------
if CE["meta"].get("transitive") is not False:
    add("G4", "ERROR", "causal-edges.meta.transitive=%r ⇒ 必须显式为 false（本图成环，传递闭包无定义）" % CE["meta"].get("transitive"))
if CE.get("topology_facts", {}).get("transitive") is not None:
    add("G4", "ERROR", "causal-edges 出现第二个 transitive 声明")
CAUSAL = {"causes", "conditional_causes"}
for e in CE["edges"]:
    if e.get("relation") not in CE["meta"]["relation_enum"]:
        add("G4", "ERROR", "边 %s 的 relation=%s 不在 relation_enum" % (e["id"], e.get("relation")))
adj = defaultdict(set)
deg = Counter()
for e in CE["edges"]:
    if e["relation"] in CAUSAL:
        adj[e["from"]].add(e["to"]); deg[e["from"]] += 1; deg[e["to"]] += 1
for c in fms:
    if deg[c] == 0:
        add("G4", "ERROR", "孤立故障模式 %s：在 causes/conditional_causes 图中无任何入边或出边" % c)

# 有向环：Tarjan SCC
idx, low, on, stk, comps, cnt = {}, {}, set(), [], [], [0]
def strong(v):
    idx[v] = low[v] = cnt[0]; cnt[0] += 1; stk.append(v); on.add(v)
    for w in adj[v]:
        if w not in idx: strong(w); low[v] = min(low[v], low[w])
        elif w in on: low[v] = min(low[v], idx[w])
    if low[v] == idx[v]:
        c = []
        while True:
            w = stk.pop(); on.discard(w); c.append(w)
            if w == v: break
        comps.append(c)
n = 0
for v in list(adj):
    if v not in idx: strong(v)
for v in fms:
    if v not in idx and v in adj: strong(v)
cyc_scc = [c for c in comps if len(c) > 1]
selfloop = [e["id"] for e in CE["edges"] if e["relation"] in CAUSAL and e["from"] == e["to"]]
if selfloop: add("G4", "ERROR", "自环边: %s" % selfloop)

declared = [list(c) for c in CE["meta"]["cycles"]]
def is_directed_cycle(lst):
    for i in range(len(lst)):
        a, b = lst[i], lst[(i + 1) % len(lst)]
        if b not in adj.get(a, ()): return False, "%s→%s 无边" % (a, b)
    return True, ""
for c in declared:
    for x in c:
        if x not in fms: add("G4", "ERROR", "meta.cycles 含未定义故障模式 %s" % x)
    ok, why = is_directed_cycle(c)
    if not ok:
        add("G4", "ERROR", "meta.cycles 声明的环不是有向环：%s（%s）" % (" → ".join(c), why))
declared_sets = [frozenset(c) for c in declared]
for c in cyc_scc:
    if frozenset(c) not in declared_sets:
        add("G4", "ERROR", "存在未在 meta.cycles 白名单中声明的有向环（SCC）: %s" % sorted(c))
for ds in declared_sets:
    if not any(ds == frozenset(c) for c in cyc_scc):
        add("G4", "WARN", "meta.cycles 声明的节点集 %s 不构成任何有向环（真实有向环: %s）"
            % (sorted(ds), [sorted(c) for c in cyc_scc]))

# 无向环（仅记录，BFS 无需特判）—— 校验它确实是无向环，且不伪装成有向环
def is_undirected_cycle(lst):
    for i in range(len(lst)):
        a, b = lst[i], lst[(i + 1) % len(lst)]
        if b not in adj.get(a, ()) and a not in adj.get(b, ()):
            return False, "%s—%s 无任何方向的有向边" % (a, b)
    return True, ""
und = [list(c) for c in CE["meta"].get("undirected_cycles", [])]
for c in und:
    for x in c:
        if x not in fms: add("G4", "ERROR", "meta.undirected_cycles 含未定义故障模式 %s" % x)
    ok, why = is_undirected_cycle(c)
    if not ok:
        add("G4", "ERROR", "meta.undirected_cycles 声明的不是无向环：%s（%s）" % (" — ".join(c), why))
    if frozenset(c) in declared_sets:
        add("G4", "ERROR", "同一点集同时出现在 cycles 与 undirected_cycles: %s" % sorted(c))
for ds in declared_sets:          # 有向环的节点集不必再登记为无向环
    if any(frozenset(ds) == frozenset(c) for c in und):
        add("G4", "WARN", "有向环节点集 %s 又登记进 undirected_cycles（冗余）" % sorted(ds))

# ---------------- G7：可达性 / 链编号 ----------------
for f in FM["fault_modes"]:
    ch = f.get("chain")
    if not ch: add("G7", "ERROR", "故障模式 %s 无 chain 标注" % f["code"])
chains = set()
for f in FM["fault_modes"]:
    v = f.get("chain") or []
    chains |= set(v if isinstance(v, list) else [v])
for e in CE["edges"]:
    v = e.get("chain") or []
    chains |= set(v if isinstance(v, list) else [v])
lrc = CE.get("leaf_root_causes", [])
for x in lrc:
    if x.get("code") and x["code"] not in fms: add("G7", "ERROR", "leaf_root_causes.code=%s 未定义" % x["code"])
for d in CE.get("discriminators", []):
    for c in d.get("pair", []):
        base = c if c in fms else None
        if base is None:
            subs = {x.get("subtype") for x in lrc}
            if c not in subs: add("G7", "ERROR", "discriminators.pair 含未定义代码 %s" % c)
    if d.get("discriminator_metric") not in metset:
        add("G7", "ERROR", "discriminator %s 的 discriminator_metric 未定义" % d.get("pair"))

# ---------------- G10 前置：CCF 字段名不得含载体词汇 ----------------
BANNED = ["cypher", "sparql", "rdf", "owl", "triple", "node", "edge", "graph", "ontology", "property graph"]
def walk_names(o, path=""):
    if isinstance(o, dict):
        if "properties" in o and isinstance(o["properties"], dict):
            for k in o["properties"]: yield k, path
        for k, v in o.items(): yield from walk_names(v, path + "/" + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from walk_names(v, path)
for nm, p in walk_names(CCF):
    low = nm.lower()
    for b in BANNED:
        if b in low: add("G10", "ERROR", "CCF 字段名 %r (%s) 含载体词汇 %r（违反 x-ccf-rules.R2）" % (nm, p, b))

# ---------------- IP：parity 覆盖 ----------------
ipc = {x["code"] for x in IP["info_products"]}
for x in IP["parity_required"]:
    if x not in ipc: add("X16", "ERROR", "parity_required 含未定义信息产品 %s" % x)
for arm, a in IP["access"].items():
    if arm not in ("KG-Q", "ONTO-Q", "KG-R", "ONTO-R", "KG-T", "KG-T-free", "KG-T-dyn", "BASE"):
        add("X16", "ERROR", "info-products.access 出现未知实验组 %s" % arm)
    for p in IP["parity_required"]:
        if p not in (a.get("paths") or {}):
            add("G8", "ERROR", "组 %s 缺信息产品路径 %s" % (arm, p))
    for p, d in (a.get("paths") or {}).items():
        if p not in ipc: add("X16", "ERROR", "组 %s 引用了未定义信息产品 %s" % (arm, p))
        for t in d.get("via", []):
            if t not in (a.get("tools") or []):
                add("G8", "ERROR", "组 %s 的 %s 经工具 %s，但该组 tools 列表里没有它" % (arm, p, t))

# ---------------- G18（内部编号 X17）：故障词表跨契约引用可解析性 ----------------
# 裁定 X-10：fault-modes.yaml 的 14 条 fault_modes[].code 之外，还有一条**只出现在
# causal-edges.yaml** 的排除法叶子根因 FM-APP-AUTH-INCOMPATIBLE（leaf_root_causes[0]
# .subtype 与 discriminators[1].pair[1] 都在引用它；方案1/方案2 两侧载体也引用它）。
# 跨契约引用必须可解析 —— 否则一条笔误（例如 discriminators 里写成
# FM-CTR-NET-UNREACHABL）会静默通过：旧 G7 只要求 pair 元素 ∈ fms ∪
# leaf_root_causes[].subtype，而 subtype 自身没有任何词表约束，两个悬空引用可以
# 互相「佐证」。
#
# 故障词表 VOCAB18（现算，不硬编码任何码名）
#   = fault-modes.yaml#fault_modes[].code ∪ meta.fault_vocabulary.exclusion_leaves
#
# 引用集（现算）：
#   ① 结构化载体（字段路径现取，见 _CARRIERS18）——
#      causal-edges.yaml: edges[].from / edges[].to / leaf_root_causes[].code /
#        leaf_root_causes[].subtype / discriminators[].pair[] / meta.cycles[][] /
#        meta.undirected_cycles[][]
#   ② 兜底全量扫描：shared/model/ 下每个 *.yaml 的**原文**跑正则 FM-[A-Z0-9-]+，
#      覆盖 findings.yaml 的规则载体与其余契约里的引用。
#
# ★ 出现位分类（可复现的「说明性文字」排除规则）：
#   A. VOCAB_DEF（定义位，**不计入引用条数**）：YAML 路径恰为
#        fault_modes[<i>].code  或  meta.fault_vocabulary.exclusion_leaves[<i>]
#      它们是词表自身的定义；把它们计进引用条数就是恒真计数。
#   B. DESCRIPTIVE（说明性文字，只告警不判错）：该码**只出现在 YAML 注释里**。
#      判定法：PyYAML 丢弃注释 ⇒「原文里有、但解析树全部标量里都没有」⇔ 它在 `#`
#      注释文本中（即 raw_tokens - tree_tokens）。依据：注释不是机读契约内容 ——
#      topology.yaml 的注释就用 FM-APP-DS-CONNECT-FAIL / FM-APP-POOL-EXHAUSTED
#      作交叉说明。故注释里的悬空码只报 WARN，不影响退出码。
#   C. REFERENCE（跨契约引用，**必须**解析到 VOCAB18）：其余一切解析树内的标量出现。
#      ★ 这里**不**按字段名白名单区分「散文」与「结构字段」（note / rationale /
#      description / planned_fix 一律照查）：解析器无法可靠区分二者，从严的代价为
#      零（散文里的笔误也一并报错），而字段名白名单会在字段改名后静默失效 ——
#      本项目已踩过同类坑（见下）。
#   ★ 反恒真（必须）：n_refs < G18_MIN_REFS 即报 ERROR。本项目的旧坑是「一个正则
#     漏写 `>` 导致某分支自首轮起从未命中，旧内容一路 PASS」；扫描器一旦失效，
#     「检查了 0 条 ⇒ PASS」是最危险的假通过。
G18_MIN_REFS = 10
G18_TOK = re.compile(r"FM-[A-Z0-9-]+")
G18_DEF_PATH = re.compile(r"^\.?(?:fault_modes\[\d+\]\.code"
                          r"|meta\.fault_vocabulary\.exclusion_leaves\[\d+\])$")
_CARRIERS18 = [
    ("causal-edges.yaml", "edges[].from"),
    ("causal-edges.yaml", "edges[].to"),
    ("causal-edges.yaml", "leaf_root_causes[].code"),
    ("causal-edges.yaml", "leaf_root_causes[].subtype"),
    ("causal-edges.yaml", "discriminators[].pair[]"),
    ("causal-edges.yaml", "meta.cycles[][]"),
    ("causal-edges.yaml", "meta.undirected_cycles[][]"),
]

def _scalars18(o, path=""):
    """遍历解析树，产出 (YAML 路径, 标量字符串)。注释不在解析树里 ⇒ 天然被排除。"""
    if isinstance(o, dict):
        for k, v in o.items():
            for x in _scalars18(v, (path + "." + str(k)) if path else str(k)):
                yield x
    elif isinstance(o, list):
        for i, v in enumerate(o):
            for x in _scalars18(v, path + "[%d]" % i):
                yield x
    elif isinstance(o, str):
        yield path, o

def _flat_str18(x):
    """摊平嵌套列表里的全部字符串（供 a[].b[] 这类通配路径使用）。"""
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        out = []
        for i in x:
            out.extend(_flat_str18(i))
        return out
    return []

def _carrier18(doc, path):
    """按 a[].b[] 通配路径取标量里的 FM- 码。
    返回 (码列表, 是否有路径段未命中, 路径末端是否存在标量值)。"""
    parts = [p for p in re.split(r"\[\]|\.", path) if p]
    cur, missing = [doc], False
    for p in parts:
        nxt = []
        for c in cur:
            if isinstance(c, list):
                for it in c:
                    if isinstance(it, dict) and p in it:
                        nxt.append(it[p])
                    else:
                        missing = True
            elif isinstance(c, dict):
                if p in c:
                    nxt.append(c[p])
                else:
                    missing = True
            else:
                missing = True
        cur = nxt
    flat = _flat_str18(cur)
    toks = [t for v in flat for t in G18_TOK.findall(v)]
    return toks, missing, bool(flat)

# ---- 词表 + 一致性断言（现算，不硬编码码名） ----
_fv18 = (FM.get("meta") or {}).get("fault_vocabulary") or {}
_vdet18 = [f["code"] for f in FM["fault_modes"]]
_vleaf18 = list(_fv18.get("exclusion_leaves") or [])
VOCAB18 = set(_vdet18) | set(_vleaf18)
_lrc18 = CE.get("leaf_root_causes") or []
_lsub18 = set(x.get("subtype") for x in _lrc18)
if _fv18.get("detect_modes") != len(_vdet18):
    add("G18", "ERROR", "meta.fault_vocabulary.detect_modes=%r 与 fault_modes 实有 %d 条不一致"
        % (_fv18.get("detect_modes"), len(_vdet18)))
if set(_vleaf18) != _lsub18:
    add("G18", "ERROR",
        "exclusion_leaves 与 leaf_root_causes[].subtype **不双向相等**：exclusion_leaves=%s，"
        "subtype 集=%s（词表缺 %s / 契约多 %s）"
        % (sorted(str(x) for x in _vleaf18), sorted(str(x) for x in _lsub18),
           sorted(str(x) for x in (_lsub18 - set(_vleaf18))),
           sorted(str(x) for x in (set(_vleaf18) - _lsub18))))
_dm18, _tc18 = _fv18.get("detect_modes"), _fv18.get("total_codes")
if not isinstance(_dm18, int) or not isinstance(_tc18, int):
    add("G18", "ERROR", "meta.fault_vocabulary.detect_modes/total_codes 必须是整数（实际 %r / %r）"
        % (_dm18, _tc18))
elif _tc18 != _dm18 + len(_vleaf18):
    add("G18", "ERROR", "meta.fault_vocabulary.total_codes=%d != detect_modes=%d + len(exclusion_leaves)=%d"
        % (_tc18, _dm18, len(_vleaf18)))

# ---- ① 全量扫描 shared/model/*.yaml（原文正则 + 解析树分类） ----
_G18E = None
_G18DOCS = {}
_G18REFS, _G18DEFS = [], []
_G18COMMENT = defaultdict(set)
for _n18 in sorted(os.listdir(MODEL)):
    if not _n18.endswith(".yaml"):
        continue
    _raw18 = io.open(os.path.join(MODEL, _n18), encoding="utf-8", newline="").read()
    try:
        _doc18 = yaml.safe_load(_raw18)
    except Exception as _e18:
        _G18DOCS[_n18] = None
        add("G18", "ERROR", "扫描器无法解析 %s: %s ⇒ 该文件的 FM- 引用本次**未被检查**" % (_n18, _e18))
        continue
    _G18DOCS[_n18] = _doc18
    _intree18 = set()
    for _p18, _s18 in _scalars18(_doc18):
        _ts18 = G18_TOK.findall(_s18)
        if not _ts18:
            continue
        _intree18 |= set(_ts18)
        for _t18 in _ts18:
            if G18_DEF_PATH.match(_p18):
                _G18DEFS.append((_n18, _p18, _t18))
            else:
                _G18REFS.append((_n18, _p18, _t18))
    for _t18 in set(G18_TOK.findall(_raw18)) - _intree18:
        _G18COMMENT[_n18].add(_t18)
_G18CARRIER = []
for _f18, _cp18 in _CARRIERS18:
    _d18 = _G18DOCS.get(_f18)
    if not _d18:
        add("G18", "ERROR", "载体 %s#%s 的契约文件缺失或无法解析" % (_f18, _cp18))
        continue
    _tk18, _miss18, _has18 = _carrier18(_d18, _cp18)
    if _miss18:
        add("G18", "ERROR", "载体路径 %s#%s 存在未命中的段（字段被改名/移动 ⇒ 该检查会静默失效）"
            % (_f18, _cp18))
    elif _has18 and not _tk18:
        add("G18", "ERROR", "载体 %s#%s 有值却产不出任何 FM- 引用（解析器失效？）" % (_f18, _cp18))
    _G18CARRIER.extend((_f18, _cp18, t) for t in _tk18)

# ---- exclusion_leaf_source 指针自洽：指到哪就从哪现取，再与 exclusion_leaves 对齐 ----
_src18 = str(_fv18.get("exclusion_leaf_source") or "")
_msrc18 = re.fullmatch(r"(?:shared/model/)?([\w.\-]+)#(.+)", _src18)
if not _msrc18:
    add("G18", "ERROR", "meta.fault_vocabulary.exclusion_leaf_source=%r 不是 <契约文件>#<字段路径> 形式"
        % (_src18,))
else:
    _sf18, _sp18 = _msrc18.group(1), _msrc18.group(2)
    _sd18 = _G18DOCS.get(_sf18)
    if _sd18 is None:
        if _sf18 not in _G18DOCS:
            add("G18", "ERROR", "exclusion_leaf_source 指向的文件不在扫描范围: %s" % _sf18)
        else:
            add("G18", "ERROR", "exclusion_leaf_source 指向的 %s 无法解析" % _sf18)
    else:
        _sv18, _sm18, _sh18 = _carrier18(_sd18, _sp18)
        if _sm18 or not _sh18:
            add("G18", "ERROR", "exclusion_leaf_source=%s 的字段路径未命中" % _src18)
        elif set(_sv18) != set(_vleaf18):
            add("G18", "ERROR", "exclusion_leaf_source=%s 现取出 %s，与 exclusion_leaves=%s 不一致"
                % (_src18, sorted(_sv18), sorted(str(x) for x in _vleaf18)))

# ---- ② 反恒真 + 逐条解析 ----
_nref18 = len(_G18REFS)
if _nref18 < G18_MIN_REFS:
    add("G18", "ERROR",
        "反恒真：本次实际检查的跨契约引用仅 %d 条（< %d）⇒ 扫描器/解析器可能已失效，"
        "禁止把「检查了 0 条」当成通过" % (_nref18, G18_MIN_REFS))
for _f18, _p18, _t18 in _G18REFS:
    if _t18 not in VOCAB18:
        _sug18 = difflib.get_close_matches(_t18, sorted(VOCAB18), n=3)
        add("G18", "ERROR",
            "跨契约引用不可解析: %s.%s = %s；故障词表（%d 码 = detect %d ∪ exclusion %d）无此码%s"
            % (_f18, _p18, _t18, len(VOCAB18), len(_vdet18), len(_vleaf18),
               ("；最近邻: %s" % ", ".join(_sug18)) if _sug18 else ""))
for _f18 in sorted(_G18COMMENT):
    for _t18 in sorted(_G18COMMENT[_f18]):
        if _t18 not in VOCAB18:
            add("G18", "WARN", "说明性文字（YAML 注释）里的悬空码 %s: %s —— 注释不是机读契约内容，故只告警"
                % (_f18, _t18))
_G18E = {
    "vocab": len(VOCAB18), "detect": len(_vdet18), "leaf": len(_vleaf18),
    "leaves": sorted(str(x) for x in _vleaf18),
    "refs": _nref18, "carrier": len(_G18CARRIER), "defs": len(_G18DEFS),
    "min": G18_MIN_REFS,
    "resolved": sorted({t for _f, _p, t in _G18REFS if t in VOCAB18}),
    "files": sorted({f for f, _p, _t in _G18REFS}),
    "findings": len([1 for f, _p, _t in _G18REFS if f == "findings.yaml"]),
    "comment": sum(len(v) for v in _G18COMMENT.values()),
    "comment_list": ["%s -> %s" % (f, ",".join(sorted(_G18COMMENT[f]))) for f in sorted(_G18COMMENT)],
}
# ---------------- ★ R-55 / X18：提示词变体的指纹与冻结差异（声明驱动） ----------------
#   为什么放本门禁：`contract_gates.py` **离线、阻塞**，且**本就在加载 KV**
#   （`kgt-prompt-variants.yaml`，此前加载后从未使用）；而 `deploy_agents.py --selftest`
#   需要可达 Dify（缺 `DIFY_BASE_URL` 即 fail-closed）⇒ 它只能进 **online 档（手动、非阻塞）**。
#   ⇒ 把「提示词指纹逐字节 + 冻结差异逐条」的复核放到阻塞档，堵住「提示词被静默改写」。
#   ★ 不硬编码变体名：归属由变体自己的 `diff_verified_by` 声明决定。
#     `f5_kgt_gate` ⇒ F5 已负责（方案3 家族；其 hunk 约定与本处不同，**不重复实现**）。
_KV_VERIFIERS = ("f5_kgt_gate", "contract_gates", "deploy_agents.selftest")
_KV_BASE_PF = (KV.get("baseline") or {}).get("prompt_file")
_KV_BASE_TEXT = ""
if _KV_BASE_PF:
    _bp = os.path.join(ROOT, _KV_BASE_PF)
    if os.path.isfile(_bp):
        _KV_BASE_TEXT = io.open(_bp, encoding="utf-8", newline="").read()


def _kv_hunks(base_text, var_text):
    """difflib opcodes → 声明约定：`insert`→`add`；`add` 的 baseline_line 是标量（插入点之前的行数）。"""
    sm = difflib.SequenceMatcher(None, base_text.split("\n"), var_text.split("\n"), autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        vt = var_text.split("\n")[j1:j2]
        if tag == "insert":
            out.append({"kind": "add", "baseline_line": i1,
                        "variant_line": [j1 + 1, j2], "variant_text": vt})
        else:
            out.append({"kind": tag, "baseline_line": [i1 + 1, i2],
                        "variant_line": [j1 + 1, j2], "variant_text": vt})
    return out


for _vn, _vs in (KV.get("variants") or {}).items():
    if not isinstance(_vs, dict):
        add("X18", "ERROR", "变体 %r 的声明不是映射" % _vn)
        continue
    _pf = _vs.get("prompt_file")
    if not _pf:
        if not (_vs.get("derived_from") or _vs.get("derived_diff")):
            add("X18", "ERROR", "变体 %s 既无 prompt_file 也无 derived_from/derived_diff（正文不可判）" % _vn)
            continue
        # ★ U-N 裁定 = A：**派生变体**（无文件）。若它登记了派生正文指纹，就必须自洽 ——
        #   本处按同一约定在内存里复算「父正文 + derived_diff」，与声明逐位比对。
        #   （口径与 `deploy_agents.derive_variant_text` 一致：kind=add ⇒ 插到 parent_line 之后。）
        if _vs.get("sha256") or _vs.get("bytes") is not None or _vs.get("lf_count") is not None:
            _pd = ((KV.get("variants") or {}).get(_vs.get("derived_from")) or {}).get("prompt_file")
            if not _pd:
                add("X18", "ERROR", "派生变体 %s 的 derived_from=%r 没有 prompt_file，无法复算"
                    % (_vn, _vs.get("derived_from")))
                continue
            _pp = os.path.join(ROOT, str(_pd))
            if not os.path.isfile(_pp):
                add("X18", "ERROR", "派生变体 %s 的父正文不存在：%s" % (_vn, _pd))
                continue
            _plines = io.open(_pp, encoding="utf-8", newline="").read().split("\n")
            for _d in sorted(_vs.get("derived_diff") or [],
                             key=lambda x: int(x.get("parent_line") or 0), reverse=True):
                _vt = _d.get("variant_text")
                for _x in reversed([_vt] if isinstance(_vt, str) else list(_vt or [])):
                    _plines.insert(int(_d.get("parent_line") or 0), _x)
            _draw = "\n".join(_plines).encode("utf-8")
            if _vs.get("bytes") is not None and len(_draw) != _vs["bytes"]:
                add("X18", "ERROR", "派生变体 %s 复算 bytes=%d != 声明 %s" % (_vn, len(_draw), _vs["bytes"]))
            if _vs.get("lf_count") is not None and _draw.count(b"\n") != _vs["lf_count"]:
                add("X18", "ERROR", "派生变体 %s 复算 LF=%d != 声明 %s" % (_vn, _draw.count(b"\n"), _vs["lf_count"]))
            if _vs.get("sha256") and hashlib.sha256(_draw).hexdigest() != _vs["sha256"]:
                add("X18", "ERROR", "派生变体 %s 复算 sha256=%s != 声明 %s"
                    % (_vn, hashlib.sha256(_draw).hexdigest()[:16], str(_vs["sha256"])[:16]))
        if _vs.get("diff_verified_by") not in _KV_VERIFIERS:
            add("X18", "ERROR", "派生变体 %s 必须声明 diff_verified_by ∈ %s，实际 %r"
                % (_vn, list(_KV_VERIFIERS), _vs.get("diff_verified_by")))
        continue
    _fp = os.path.join(ROOT, _pf)
    if not os.path.isfile(_fp):
        add("X18", "ERROR", "变体 %s 的 prompt_file 不存在：%s" % (_vn, _pf))
        continue
    _raw = io.open(_fp, "rb").read()
    if _vs.get("bytes") is not None and _vs["bytes"] != len(_raw):
        add("X18", "ERROR", "变体 %s 的 prompt 字节数 %d != 声明 %s" % (_vn, len(_raw), _vs["bytes"]))
    if _vs.get("lf_count") is not None and _vs["lf_count"] != _raw.count(b"\n"):
        add("X18", "ERROR", "变体 %s 的 prompt LF 行数 %d != 声明 %s" % (_vn, _raw.count(b"\n"), _vs["lf_count"]))
    if _vs.get("sha256") and _vs["sha256"] != hashlib.sha256(_raw).hexdigest():
        add("X18", "ERROR", "变体 %s 的 prompt sha256 %s != 声明 %s"
            % (_vn, hashlib.sha256(_raw).hexdigest()[:16], str(_vs["sha256"])[:16]))
    if _raw[:3] == b"\xef\xbb\xbf":
        add("X18", "ERROR", "变体 %s 的 prompt 含 BOM（本项目一律 UTF-8 无 BOM）" % _vn)
    if b"\r\n" in _raw:
        add("X18", "ERROR", "变体 %s 的 prompt 含 CRLF（本项目一律 LF-only）" % _vn)
    _fd = _vs.get("frozen_diff")
    if not _fd:
        continue
    _who = _vs.get("diff_verified_by")
    if _who not in _KV_VERIFIERS:
        add("X18", "ERROR", "变体 %s 有 frozen_diff 但 diff_verified_by=%r 不在 %s"
            "（防「有清单但无人校验」）" % (_vn, _who, list(_KV_VERIFIERS)))
        continue
    if _who == "f5_kgt_gate":
        continue          # 方案3 家族由 F5 复核（本处不重复实现其 hunk 约定）
    if not _KV_BASE_TEXT:
        add("X18", "ERROR", "变体 %s 需用基准正文复算冻结差异，但 baseline.prompt_file 不可读" % _vn)
        continue
    _act = _kv_hunks(_KV_BASE_TEXT, _raw.decode("utf-8"))
    if len(_act) != len(_fd):
        add("X18", "ERROR", "变体 %s 实算 hunk 数 %d != 声明 %d" % (_vn, len(_act), len(_fd)))
        continue
    for _a, _d in zip(_act, _fd):
        _vt = _d.get("variant_text")
        _vt = [_vt] if isinstance(_vt, str) else list(_vt or [])
        for _k in ("kind", "baseline_line", "variant_line"):
            if _a[_k] != _d.get(_k):
                add("X18", "ERROR", "变体 %s 的 %s.%s 实算 %r != 声明 %r"
                    % (_vn, _d.get("id"), _k, _a[_k], _d.get(_k)))
        if _a["variant_text"] != _vt:
            add("X18", "ERROR", "变体 %s 的 %s.variant_text 与实算不一致（实算 %d 行 / 声明 %d 行）"
                % (_vn, _d.get("id"), len(_a["variant_text"]), len(_vt)))

# ---------------- ★ X19：阈值占位值 vs 健康值域实测（声明驱动、双侧带） ----------------
#   为什么需要：T1 标定前，fault-modes.yaml 的阈值多为**占位值**。实测发现占位值与真实健康域
#   可以差两个数量级（session_used_ratio_light warning=0.70 vs 实测 P95=0.00092 ⇒ 761 倍），
#   且**过低**同样有害（xact_age_s_light=300s vs 实测 28802s ⇒ 96 倍 ⇒ 假阳性）。
#   本区块把「实测健康值域」记成契约（threshold-observations.yaml，带 n/时窗/来源），
#   对每个**警告档非 null 且指标有实测**的阈值做**双侧**判定：
#       P95/cap ≤ warning ≤ P95×cap
#   超出即必须在 declared_deviations 里登记（登记 ⇒ WARN；未登记 ⇒ ERROR；登记已不成立 ⇒ ERROR 陈旧）。
TO = y("threshold-observations.yaml") or {}
_tm = {t["name"]: t for t in (y("fault-modes.yaml").get("thresholds") or [])}
_obs = {o.get("metric"): o for o in (TO.get("observations") or [])}
_cap = (TO.get("meta") or {}).get("cap_ratio")
_dev = {(d.get("threshold"), d.get("metric")): d for d in (TO.get("declared_deviations") or [])}
for _o in (TO.get("observations") or []):
    _m = _o.get("metric")
    if not _m or _o.get("n") is None or _o.get("min") is None or _o.get("p95") is None:
        add("X19", "ERROR", "观测 %r 缺 n/min/p95（无可审计性）" % _m)
        continue
    _q = [_o.get("min"), _o.get("median"), _o.get("p95"), _o.get("max")]
    if any(x is None for x in _q) or not (_q[0] <= _q[1] <= _q[2] <= _q[3]):
        add("X19", "ERROR", "观测 %s 的四分位不单调：%s" % (_m, _q))
if not isinstance(_cap, (int, float)) or _cap <= 1:
    add("X19", "ERROR", "threshold-observations.meta.cap_ratio 缺失或不合法：%r" % (_cap,))
else:
    for _n, _t in sorted(_tm.items()):
        _w = _t.get("warning"); _o = _obs.get(_t.get("metric"))
        if _w is None or not _o or not _o.get("p95"):
            continue                      # 未标定 / 无实测 ⇒ 不判（前者另有既有的未标定 WARN）
        _hi = _w / _o["p95"]; _lo = _o["p95"] / _w
        _d = _dev.get((_n, _t.get("metric")))
        if _hi > _cap or _lo > _cap:
            if not _d:
                add("X19", "ERROR",
                    "阈值 %s 的 warning=%s 偏离实测 P95=%s（高比 %.1f / 低比 %.1f，cap=%s）且未登记"
                    % (_n, _w, _o["p95"], _hi, _lo, _cap))
            else:
                add("X19", "WARN", "阈值 %s 为**已登记**偏离（%s）：warning=%s vs 实测 P95=%s（高比 %.1f / 低比 %.1f）"
                    % (_n, _d.get("kind"), _w, _o["p95"], _hi, _lo))
        elif _d:
            add("X19", "ERROR",
                "阈值 %s 的偏离已不成立（高比 %.1f / 低比 %.1f 均在 cap=%s 内）⇒ declared_deviations 陈旧，须删除"
                % (_n, _hi, _lo, _cap))
for (_n, _m) in sorted(_dev):
    if _n not in _tm:
        add("X19", "ERROR", "declared_deviations 指向不存在的阈值 %s" % _n)
    elif _tm[_n].get("metric") != _m:
        add("X19", "ERROR", "declared_deviations 的 %s metric=%s 与契约 %s 不符"
            % (_n, _m, _tm[_n].get("metric")))
if _obs:
    add("X19", "NOTE", "已登记实测值域 %d 个指标；声明偏离 %d 条；cap=%s" % (len(_obs), len(_dev), _cap))

# ---------------- ★ X20：报告**必写**的降级声明（声明驱动 + 双向机检） ----------------
#   为什么：报告的降级声明若靠人记就必然漏。本区块把「漏写」变成 ERROR，并且**双向**：
#     * 登记的证据与现实不符（例如以后真的做出了跨层路径）⇒ ERROR（**强制更新声明**，防陈旧）；
#     * 声明未出现在指定文档里（被删/被淡化）⇒ ERROR。
#   证据的可复算性分级：① 完全可从**已提交**契约复算（path_basis / delta 模式 / enum / 台账豁免）；
#   ② 依赖冻结基底（时间戳唯一性 / delta 计数）⇒ 基底不在场时只记 NOTE（**不得**判 BLOCKED，
#      否则离线档会因环境缺件而变红；这是刻意的取舍，登记里已写明）。
DG = y("m5-degradations.yaml") or {}
_dg_entries = DG.get("entries") or []


def _dg_check(name, ev):
    """按 check 名复算并返回 (ok, detail)。返回 None 表示不可复核（缺件）。"""
    if name == "recompute_path_basis_tally":
        led = y("m5-input-facts.yaml") or {}
        mp = led.get("mapping") or []
        tally = {}
        for m in mp:
            k = "path_basis.%s" % (m.get("path_basis"),)
            tally[k] = tally.get(k, 0) + 1
        for m in mp:
            if m.get("family") == "C2":
                k = "family_C2_%s" % (str(m.get("path_basis")).replace("-", "_"))
                tally[k] = tally.get(k, 0) + 1
        bad = {k: (v, tally.get(k)) for k, v in ev.items() if tally.get(k) != v}
        return (not bad), ("复算 %s" % tally if bad else "复算一致")
    if name == "recompute_delta_dependent_modes":
        fm = y("fault-modes.yaml") or {}
        codes = []
        for m in (fm.get("fault_modes") or []):
            hit = False
            for g in ("any_of", "all_of"):
                for it in ((m.get("detect") or {}).get(g) or []):
                    if it.get("predicate") in ("increased", "decreased"):
                        hit = True
            if hit:
                codes.append(m.get("code"))
        return (len(codes) == ev.get("delta_dependent_modes") and sorted(codes) == sorted(ev.get("modes") or [])), \
               ("复算 %d 个" % len(codes))
    if name == "assert_enum_missing_client_read":
        mt = y("metrics.yaml") or {}
        m = next((x for x in (mt.get("metrics") or []) if x.get("code") == "db.session.wait_event"), None)
        enum = (m or {}).get("enum") or []
        if not enum:
            return False, "契约里找不到 db.session.wait_event 的 enum"
        if "ClientRead" in enum:
            return False, "enum 已包含 ClientRead ⇒ 本条**陈旧**，必须删除该登记条目"
        return True, "enum 仍不含 ClientRead（%d 值）" % len(enum)
    if name == "assert_audit_waiver_disclosed":
        ga = y("m5-gold-audit.yaml") or {}
        hr = (ga.get("meta") or {}).get("human_review") or {}
        if hr.get("status") != "waived":
            return False, "gold-audit.meta.human_review.status=%r ⇒ 豁免状态变了，登记须更新" % hr.get("status")
        if not hr.get("limitation"):
            return False, "human_review.limitation 为空 ⇒ 豁免未附局限"
        return True, "豁免与局限已登记"
    if name == "recompute_time_series_evidence":
        base_rel = ((y("m5-input-facts.yaml") or {}).get("meta") or {}).get("base_ref") or ""
        base = os.path.normpath(os.path.join(ROOT, "shared", "model", base_rel))
        if not os.path.isfile(base):
            return None, "冻结基底不在场（%s）⇒ 本项只记 NOTE" % base_rel
        try:
            doc = json.load(io.open(base, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return None, "基底无法解析：%s" % exc
        obs = doc.get("observations") or []
        ts = {o.get("observedAt") or o.get("capturedAt") for o in obs}
        got = {"observations": len(obs), "distinct_observed_at": len(ts),
               "with_delta": sum(1 for o in obs if o.get("delta") is not None),
               "with_nonzero_delta": sum(1 for o in obs if o.get("delta"))}
        bad = {k: (v, got.get(k)) for k, v in ev.items() if got.get(k) != v}
        return (not bad), ("复算 %s" % got if bad else "复算一致")
    return False, "未知 check %r（登记表与门禁必须同步）" % (name,)


if not _dg_entries:
    add("X20", "ERROR", "m5-degradations.yaml 没有任何条目 ⇒ 报告必写的降级声明无人登记")
for _e in _dg_entries:
    _id = _e.get("id")
    _ok, _det = _dg_check(_e.get("check"), _e.get("evidence") or {})
    if _ok is None:
        add("X20", "NOTE", "%s：%s" % (_id, _det))
    elif not _ok:
        add("X20", "ERROR", "%s：证据复算不符或登记陈旧 ⇒ %s" % (_id, _det))
    _doc_rel = _e.get("must_appear_in") or ""
    _dp = os.path.join(ROOT, _doc_rel)
    _need = _e.get("must_contain") or ""
    if not _doc_rel or not os.path.isfile(_dp):
        add("X20", "ERROR", "%s：must_appear_in 指向的文档不存在：%r" % (_id, _doc_rel))
    elif _need and _need not in io.open(_dp, encoding="utf-8", newline="").read():
        add("X20", "ERROR", "%s：指定文档 %s 里**找不到**必写声明 %r（被删或被淡化）" % (_id, _doc_rel, _need))
if _dg_entries:
    add("X20", "NOTE", "报告必写降级声明 %d 条已机检（证据复算 + 必写性）" % len(_dg_entries))


# ---------------- 汇总 ----------------
print("=" * 78)
print("契约门禁 contract_gates.py  |  shared/model/ 8 份主契约 + *.yaml 全量扫描（G18）  |  只读、无外部依赖")
print("=" * 78)
by = defaultdict(list)
for g, s, m in F: by[(g, s)].append(m)
_known = ["X", "G3", "G4", "G5", "G7", "G8", "G10", "X7", "X8", "X9", "X10", "X11", "X12",
          "X14", "X16", "G18"]
# ★ 显示修复（2026-10-05）：原来只按 _known 列表打印，**不在列表里的门禁码
#   （如 X1/X2/X5/X7b）命中 ERROR 时会计入 nE 却不打印** ⇒ 出现「RESULT 说 ERROR=4
#   但只列出 1 条」的假象。现在把实际出现的门禁码全部并进来（已知顺序优先）。
gates = _known + sorted({g for g, _s in by if g not in _known})
print("%-6s %-7s %s" % ("门禁", "级别", "问题数"))
for g in gates:
    for s in ("ERROR", "WARN"):
        if (g, s) in by: print("%-6s %-7s %d" % (g, s, len(by[(g, s)])))
nE = sum(1 for x in F if x[1] == "ERROR"); nW = len(F) - nE
if F:
    print("-" * 78)
    for g in gates:
        for s in ("ERROR", "WARN"):
            for m in by.get((g, s), []):
                print("[%s/%s] %s" % (g, s, m))
print("-" * 78)
# ★ G18/X17 证据行（不是告警）：必须把「本次实际检查了多少条引用」摆进输出，
#   否则反恒真阈值无法被复核。
if _G18E:
    print("[G18/X17] 故障词表 %d 码 = detect %d（fault-modes.yaml#fault_modes[].code）"
          " ∪ exclusion %d（%s）"
          % (_G18E["vocab"], _G18E["detect"], _G18E["leaf"], ", ".join(_G18E["leaves"])))
    print("[G18/X17] 实际检查跨契约引用 %d 条（阈值 %d ⇒ 反恒真%s）：结构化载体 %d 条 / 其余解析树标量 %d 条；"
          "词表定义位 %d 条不计；说明性文字（YAML 注释）排除 %d 条；覆盖 %d 个 yaml、解析到 %d 个不同码；"
          "findings.yaml 引用 %d 条"
          % (_G18E["refs"], _G18E["min"], "通过" if _G18E["refs"] >= _G18E["min"] else "失败",
             _G18E["carrier"], _G18E["refs"] - _G18E["carrier"], _G18E["defs"],
             _G18E["comment"], len(_G18E["files"]), len(_G18E["resolved"]), _G18E["findings"]))
    if _G18E["comment_list"]:
        print("[G18/X17] 说明性文字排除明细（注释，仅告警不判错）: %s" % "; ".join(_G18E["comment_list"]))
print("RESULT: %s  (ERROR=%d WARN=%d)" % ("FAIL" if nE else "PASS", nE, nW))
sys.exit(1 if nE else 0)
