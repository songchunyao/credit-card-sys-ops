#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""G22 · **跨载体 ABox 身份集等价门禁**（只读；退出码 0 PASS / 1 FAIL / 2 BLOCKED·PARAM_ERROR）。

## 为什么需要它（M2.16 / M2.17 实测盲区）
G14 `shared/verify/parity.py` 只比**计数**（byType / byRelation / metricValueCounts /
notCollectedCounts / collectedEmptyCounts），G19 `shared/verify/graph_meta_gate.py` 只比
**图级快照 id**。两者都抓不到「两侧计数逐条相同、但身份键完全不同」的情形：
真机上 KG 停在旧快照 live3、ONTO 最新是 live5 时，67 对象 / 34 关系 / 191 观测两侧计数相同
⇒ G14 PASS；而 M2.11 改过 `Table` 的身份（新增 `schemaName`、`ccfId` 由裸表名改为
`tbl-<schema>.relname`）⇒ `Table` 这一类的身份键两侧**没有一条交集**。
本门禁按 **type-map 的 `key_attrs` 身份元组集合** 逐类型比对两侧，回答：
「两套载体里的 ABox 是不是**同一个** ABox」，而不是「看起来一样大」。

## M2.19 追加：关系维度（同一个盲区的另一半）
G14 对关系**只比计数**（`count_kg(R) == count_onto(R)`）；真机 live3↔live5 的关系条数
逐关系型都相同（两侧各 34 条），但涉及 `Table` 的边的**端点身份**已经不同
（`tbl-new_order` → `tbl-root.new_order`）⇒ 计数相等、边不同。故本门禁同时按
**rel-map 的 `asserted: true` 关系契约**（14 条；`HAS_EVIDENCE` 派生 1 条与 4 条逆关系排除）
逐关系型比对**边身份集合** `{(源端点身份元组, 目标端点身份元组)}`，
端点身份**复用同一套 `key_attrs` 口径**（同一段代码，不另写一份），见 G22-07 / G22-08。

## 口径来源（必须与 `tmp/_abox_identity_probe.py` 一致；该脚本是队长真机验证过的参考实现）
* `types` 按 typeCode 键控；每个 typeCode 读 `kg_label` / `onto_class` / `key_attrs` / `attrs` / `abstract`。
* ★ 构造身份元组时，取 **`attrs` 的键（属性名）∈ `key_attrs`** 的那条属性，再用它的
  `kg_property` / `onto_property`；**不能**拿 `kg_property` / `onto_property` 的值去和 `key_attrs` 比
  （探测脚本第一版正是在这里踩坑）。
* CURIE 展开：`type-map.yaml#prefixes` + `namespaces.onto.base`；`<...>` 形式直接用。
* 左侧（KG）= Neo4j 活体图 `MATCH (n:<kg_label>) RETURN n.<kg_property>…`，身份元组 = 键属性值的有序元组；
  全为 `None` 时记为 `("<NO_KEY>",)`。
* 右侧（ONTO）= 指定命名图 `SELECT DISTINCT ?s ?k0 … WHERE { GRAPH <g> { ?s a <onto_class> .
  OPTIONAL { ?s <onto_property> ?k0 } … } }`。
* 标量字符串归一（`None` 保持 `None`，布尔统一 `true`/`false`）：真机 13 类的键属性
  `xsd` 全是 `xsd:string ⇒ 本归一与探测脚本口径逐字等价；归一只为防「KG 侧 int 8000 vs
  ONTO 侧字面量 "8000"」这类纯 datatype 噪声冒充身份差异（datatype 另有门禁负责）。
* ★ 与探测脚本的**唯一**偏差（仅影响未行使的兜底路径）：`key_attrs` 为空（身份未定义）时，
  探测脚本兜底成 KG `ccfId` / ONTO `cc:recordId` 这一**不对称**组合（会凭空报差异）；
  本门禁改用**契约自己声明的实例 id**（`common_properties.kg.instance_id` / `.onto.instance_id`，
  兜底 `namespaces.kg.instance_id_property`），并把它记成 W1 告警（绝不静默）。
  真机 13 个具体类型的 `key_attrs` 全非空 ⇒ 该兜底在真机上从不触发。

## 判据（逐条编号，报告里逐条打印）
  G22-00 **可达性**：KG 侧 `:CC_Meta` 可读且恰好 1 个节点、两侧都真的取到被比较类型的主体；
          任一不成立 ⇒ **BLOCKED**（不是 PASS，也不是 FAIL）。
  G22-01 **目标图选择**：默认选 `末段 == :CC_Meta.snapshotId` 的 ONTO 命名图；找不到 ⇒ **BLOCKED**
          （fail-closed：**不得**退化成「随便挑一张最新图」而静默通过）。
          给了 `--onto-graph` 就用它，并注明它与 `:CC_Meta.snapshotId` 是否一致、ONTO 最新快照图末段是多少；
          显式图与 KG 快照不一致 ⇒ W0 告警（`--strict` 升 ERROR），因为它正是本门禁要**暴露**的 A/B 场景。
  G22-02 **身份集相等**：逐非抽象 typeCode，两侧身份元组集合必须**相等**；不等 ⇒ **ERROR**，
          并给出 `onlyKg` / `onlyOnto` 计数与最多 3 条样例（单个值截断到 80 字符）。相等也打一行。
  G22-03 **身份唯一性**：任一侧「去重后身份元组数 < 该类型主体数」⇒ **ERROR**
          （重复键 = 身份不唯一；同时抓「键全空 ⇒ 塌缩成 `<NO_KEY>`」这类退化）。
  G22-04 **反恒真下限**：两侧被比较类型的主体数合计（取**较小侧**）< `--min-abox-subjects` ⇒ **ERROR**；
          实际参与比较的非抽象类型数 < 3 ⇒ **ERROR**。`--min-abox-subjects` 非法（非整数 / `<=0`）
          ⇒ `PARAM_ERROR` 且 exit 2。
  G22-05 **抽象类按契约排除**：`abstract: true` 的类型排除，报告里列出排除项与依据（契约标志，
          **不是**硬编码类型名单）。
  G22-06 **契约自洽**（fail-closed 的**加法**，不放松任何既有判据）：具体类型的 `key_attrs` 必须是
          非空列表、且每个键名都真的出现在 `attrs` 里并带 `kg_property` / `onto_property`；
          否则 **ERROR**（否则一个写错的键名会被静默丢掉 ⇒ 身份元组悄悄少一维）。
  W1 `key_attrs` 为空 ⇒ 退化为契约声明的实例 id 属性（默认 WARN；`--strict` 升 ERROR）。
  W2 某键属性在一侧非单值（一个主体多个键值 ⇒ 去重后元组数 **>** 主体数）⇒ 默认 WARN（`--strict` 升 ERROR）；
          这一形态 G22-03 按定义抓不到，故显式可观测，不静默。

  G22-07 **关系身份集等价**（M2.19）：对每条 `asserted: true` 的关系型，两侧边身份集合
          `{(源端点身份元组, 目标端点身份元组)}` 必须**相等**；不等 ⇒ **ERROR**，给出该关系型
          `onlyKg` / `onlyOnto` 计数与最多 3 条样例（形如 `(src) -CODE-> (dst)`，端点元组复用
          对象维度同一套 `key_attrs` 口径）；相等也打一行（供反恒真核对）。
          端点身份取不到（键全空 ⇒ `<NO_KEY>`）或端点类型不匹配 ⇒ 记入 `unresolvedEdges`
          （W4；`--strict` 升 ERROR），**绝不**用空串代替、**绝不**静默跳过。
          ★ **可选扩展口径** `--include-derived-relations`（**默认关闭**）：把 rel-map 里带
          `derived_from` 标志的派生关系也纳入比较面（真机只有 `HAS_EVIDENCE`，`from: "*"` ⇒
          按**每个非抽象源端类型**扇出为独立规格 `code = <CODE>@<源类型>`，故报告里会出现如
          `HAS_EVIDENCE@Table` 的行）。默认口径严格等于契约（14 条 asserted）；派生关系在
          `excludedRelations` 里逐条列出并注明可用该开关纳入。
  G22-08 **关系反恒真下限**（M2.19）：两侧被比较关系型的边数合计（取**较小侧**）<
          `--min-abox-relations`（默认 30）⇒ **ERROR**；实际参与比较的关系型数（asserted + 派生扇出）
          < 5 ⇒ **ERROR**；非法值（非整数 / `<=0`）⇒ `PARAM_ERROR` 且 exit 2。
          **某个关系型两侧都为空 ⇒ 单列 W5 告警**（`--strict` 升 ERROR），不得因为「空集相等」判 PASS。
  W4 关系端点身份不可解析：任一侧存在端点键取不到值、或端点类型与契约不符的边 ⇒ 默认 WARN
          （`--strict` 升 ERROR）；报告列出逐关系型计数与 ≤3 条样例。
  W5 某关系型两侧皆空（`edgeTyped == 0`）⇒ 默认 WARN（`--strict` 升 ERROR）：集合相等但**无证据**，
          显式可观测，不静默通过。

## 纯只读
只执行 `MATCH` / `RETURN` 形式的 Cypher 与 `SELECT` 形式的 SPARQL；每条查询在发送前都要过
**正向白名单**（`_assert_readonly`），不在白名单头部的查询**拒绝执行**。
不创建 / 不删除 / 不写入任何仓库，不做任何预检仓库，不重启任何远端服务。
口令只从环境变量读（默认 `CC_NEO4J_PASSWORD`，与 `parity.py` / `p1_neo4j.py` 同一约定），
**缺失时直接 BLOCKED 并非 0 退出，绝不回退到硬编码口令**。

## 用法
  set CC_NEO4J_PASSWORD=...
  python shared\\verify\\abox_identity_gate.py [--onto-graph <IRI>] [--min-abox-subjects 100]
                                             [--min-abox-relations 30] [--rel-map <path>]
                                             [--topology <path>] [--include-derived-relations]
                                             [--json] [--out 报告.json]
                                             [--strict] [--selftest]
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
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 快照 id 格式（生成侧 = shared/collect/freeze.py；只读校验口径，两处必须一致）
SNAPSHOT_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")
# 只读正向白名单（**正向**允许列表：写操作没有机会出现在这里，也就不需要写出写关键字）
CYPHER_READONLY_HEADS = ("MATCH", "WITH", "UNWIND", "RETURN", "OPTIONAL")
SPARQL_READONLY_HEADS = ("SELECT", "PREFIX", "BASE")

DEFAULTS = {
    "bolt_uri": "bolt://127.0.0.1:7688",  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
    "user": "neo4j",
    "password_env": "CC_NEO4J_PASSWORD",
    "database": "neo4j",
    "meta_label": "CC_Meta",
    "meta_snapshot_field": "snapshotId",
    "gdb_url": "http://127.0.0.1:7200",  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
    "repo": "cc-ops",
    "graph_template": "https://credit-card-ops.ai/onto/graph/{snapshotId}",
    # 关系契约（M2.19）：名称映射的权威表 = rel-map.yaml；asserted 集合的交叉核对 = topology.yaml
    "rel_map": "shared/mapping/rel-map.yaml",
    "topology": "shared/model/topology.yaml",
}
MIN_ABOX_SUBJECTS = 100   # 两侧被比较类型主体数合计（较小侧）下限
MIN_COMPARED_TYPES = 3    # 实际参与比较的非抽象类型数下限（无 CLI 开关 ⇒ 不能被关掉）
MIN_ABOX_RELATIONS = 30   # 两侧被比较关系型边数合计（较小侧）下限（真机 34 条）
MIN_COMPARED_RELATIONS = 5  # 实际参与比较的 asserted 关系型数下限（真机 14 条；无 CLI 开关）
SAMPLE_LIMIT = 3          # 每侧样例条数上限
VALUE_MAX = 80            # 样例里单个值的最大显示字符数
NO_KEY = "<NO_KEY>"

HONESTY_NOTES = [
    "本门禁只判「**身份集**是否等价」：对象维度 = 逐 typeCode 比较 type-map `key_attrs` 构成的键元组集合",
    "（G22-02 / G22-03）；关系维度 = 逐 asserted 关系型比较边身份集合 `{(源端点键元组, 目标端点键元组)}`（G22-07）。",
    "**不**比较键以外的属性值（值差异由 G14/G21 等负责）、**不**比较边上的属性值、**不**比较边的重数",
    "（边身份是**集合** ⇒ 两侧各有 2 条完全相同的边时只算 1 条）、**不**比较 Evidence→对象 的归属、",
    "**不**比较 XSD datatype、**不**证明「ABox 完全等价」。",
    "身份集的正确性完全依赖 type-map 的 `key_attrs` 真的是该类型的身份；若契约把非唯一属性当键，",
    "门禁只会更强（G22-03 会报重复），但若契约把**过粗**的属性当键（多个主体同键），",
    "G22-03 会报「去重后元组数 < 主体数」⇒ ERROR，而不是静默通过。",
    "两侧同源陈旧（例如 KG 与 ONTO 都停在 live3）时本门禁 PASS —— 它只回答「两侧是不是同一份 ABox」，",
    "不回答「这份 ABox 是不是最新」。后者由 G19（O4/O5）负责；`:CC_Meta` 自身说谎也由 G19 K3 负责。",
    "采集不是原子快照：另一名施工者并发重投时（本任务取证期间正在把 KG 重投到 live5），",
    "同一份报告里的两侧读数可能取自不同瞬间；报告必须记录取证时刻的 `:CC_Meta.snapshotId`。",
    "ONTO 侧读数走 GraphDB 仓库默认推理口径（真机 `cc-ops` = owl2-rl、rdfsSubClassReasoning=true）：",
    "`?s a <onto_class>` 的类归属在命名图内可能是推理得到的；键属性取值仍来自显式字面量，",
    "但若生成侧改变推理口径，逐类型主体数会漂移（身份集等价性判据不受影响）。",
    "样例只给最多 %d 条、单个值最多 %d 字符（截断以 `…` 结尾）⇒ 样例**不是**完整差异清单，"
    "完整清单要看 `--json` 里的计数与两侧主体总数。" % (SAMPLE_LIMIT, VALUE_MAX),
    "口径来自 `tmp/_abox_identity_probe.py`（队长真机验证过的只读探测脚本，本门禁**只读它、不改它**）；",
    "唯一偏差是 `key_attrs` 为空时的兜底属性（探测脚本用不对称的 `ccfId` vs `cc:recordId`，",
    "本门禁改用契约声明的实例 id），该路径在真机 13 类上从不触发。",
    "关系维度只覆盖 rel-map 里 `asserted: true` 的 14 条关系型；派生关系 `HAS_EVIDENCE`",
    "（真机 KG / ONTO 各 191 条）与 4 条逆关系（`asserted: false`）按契约排除，报告里逐条列出依据，",
    "它们**不在** G22-07 的比较面内。",
    "KG 是**共享图**：`shared/deploy/targets.yaml` 的 `colliding_relationship_types: [ACCESSES, DEPLOYED_ON]`",
    "说明裸名 `ACCESSES` / `DEPLOYED_ON` 属于既有 v2 图（真机 1 / 4 条，端点标签是 v2 的 `ManagedEntity` 等）",
    "⇒ 边身份一律按**端点标签**过滤（`(a:CC_From)-[r:CC_Rel]->(b:CC_To)`）；v2 同名边只计数、不比较，",
    "并作为「被排除的 v2 数据」写进报告。",
    "端点类型与契约不符的边（`edgeUnmatchedType`）无法表达成身份对 ⇒ 只计数并计入 `unresolvedEdges`（W4），",
    "**不**进入集合比较；因此「集合相等」**不**蕴含「两侧该关系型的边一条不多一条不少」。",
    "ONTO 侧端点键非单值时同一个 (s, o) 会出多行 ⇒ `edgeTyped` 可能大于去重后的边数；集合比较不受影响，",
    "但 G22-08 的边数下限读数按可解析的边数计（两侧取**较小侧**）。",
    "★ 默认口径 = 契约（asserted: true 的 14 条）；`--include-derived-relations` 是**非默认**的扩展口径，",
    "把 `derived_from` 标出的派生边（真机只有 `HAS_EVIDENCE`：对象 → 自己的观测证据）也纳入 G22-07。",
    "**为什么需要它**：真机上以 `Table` 为端点的 asserted 边**一条都没有**（14 条里只有 ACCESSES Query→Table，",
    "而真机 KG 无 `CC_ACCESSES`、ONTO 两图 `cc:accesses` 也都是 0；KG 那条 1 条裸名 v2 `ACCESSES` 端点标签不符被排除）",
    "⇒ 默认口径下 live3↔live5 的 `Table` 身份变化在关系维度**物理上不可观测**（不是口径被计数骗过）。",
    "唯一承载该变化的边就是被契约排除的派生边 `HAS_EVIDENCE`（KG/ONTO 各 Table→Evidence 120 条）⇒ 开启本开关后",
    "该差异**能**被判出（门禁自证第 21 条 + 真机 B 组读数）。是否永久纳入属**契约/口径裁定**，本门禁默认不擅自纳入。",
]


# ---------------------------------------------------------------------------
# 只读护栏 / 通用小工具
# ---------------------------------------------------------------------------
def _assert_readonly(query, heads, lang):
    """正向白名单：查询头不在允许集合内 ⇒ 拒绝执行（G22 是只读门禁）。"""
    text = (query or "").strip()
    head = text.split(None, 1)[0].upper() if text else ""
    if head not in heads:
        raise RuntimeError("%s 查询头 %r 不在只读白名单 %s 内 ⇒ 拒绝执行"
                           "（G22 只允许只读查询）" % (lang, head, list(heads)))


def _cut(value, limit=VALUE_MAX):
    """样例显示截断：单个值最多 `limit` 字符，超长以 `…` 结尾（总长仍 <= limit）。"""
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s
    return s[:limit - 1] + "…"


def _fmt_tuple(t):
    return "(" + ", ".join(_cut(v) for v in t) + ")"


def _fmt_edge(edge, code):
    """边身份样例的人类可读形态：(源端点键元组) -CODE-> (目标端点键元组)。"""
    src, dst = edge
    return "%s -%s-> %s" % (_fmt_tuple(src), code, _fmt_tuple(dst))


def _norm(value):
    """身份分量归一：None 保持 None；布尔统一 true/false；其余 str()（对 xsd:string 键是无操作）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def tail_of(iri):
    return str(iri or "").rstrip("/").rsplit("/", 1)[-1]


def read_yaml(path):
    import yaml
    with io.open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# 契约（type-map.yaml）→ 身份规格
# ---------------------------------------------------------------------------
def load_type_map(path):
    m = read_yaml(path)
    if not isinstance(m.get("types"), dict) or not m["types"]:
        raise RuntimeError("type-map 缺 `types` 块或为空：%s" % path)
    onto_ns = ((m.get("namespaces") or {}).get("onto") or {})
    onto_base = onto_ns.get("base") or ""
    prefixes = dict(m.get("prefixes") or {})
    return m, onto_base, prefixes


def expand(curie, prefixes, onto_base):
    """CURIE 展开：`<...>` 直接用；已知前缀用 prefix 表；否则用 onto base。"""
    curie = str(curie)
    if curie.startswith("<") and curie.endswith(">"):
        return curie[1:-1]
    if ":" not in curie:
        return curie
    p, local = curie.split(":", 1)
    if p in prefixes:
        return prefixes[p] + local
    return onto_base + local


def build_specs(m, prefixes, onto_base):
    """把 `types` 块编译成身份规格清单（含契约自洽证据）。

    返回 (specs, excluded_abstract)。`specs` 只含**非抽象**类型；
    `excluded_abstract` 只由契约标志 `abstract: true` 驱动（不是硬编码类型名单）。
    """
    types = m["types"]
    cp = m.get("common_properties") or {}
    kg_ns = (m.get("namespaces") or {}).get("kg") or {}
    onto_ns = (m.get("namespaces") or {}).get("onto") or {}
    kg_inst = ((cp.get("kg") or {}).get("instance_id")
               or kg_ns.get("instance_id_property") or "ccfId")
    onto_inst = ((cp.get("onto") or {}).get("instance_id")
                 or onto_ns.get("instance_id") or "cc:ccfId")

    specs, excluded = [], []
    for tc in sorted(types):
        spec = types[tc] or {}
        if spec.get("abstract"):
            excluded.append({"typeCode": tc, "kgLabel": spec.get("kg_label"),
                             "ontoClass": spec.get("onto_class"),
                             "keyAttrs": spec.get("key_attrs"),
                             "basis": "type-map.yaml types.%s.abstract == true（契约标志）" % tc})
            continue
        raw_keys = spec.get("key_attrs")
        keys = list(raw_keys) if isinstance(raw_keys, (list, tuple)) else []
        attrs = spec.get("attrs") or {}
        kg_keys, onto_keys, used, missing = [], [], [], []
        for name in keys:
            a = attrs.get(name)
            if not isinstance(a, dict):
                missing.append(name)
                continue
            kp, op = a.get("kg_property"), a.get("onto_property")
            if not kp or not op:
                missing.append(name)
                continue
            kg_keys.append(str(kp))
            onto_keys.append(expand(op, prefixes, onto_base))
            used.append(name)
        fallback = not used
        if fallback:
            kg_keys = [str(kg_inst)]
            onto_keys = [expand(onto_inst, prefixes, onto_base)]
        specs.append({
            "typeCode": tc,
            "layer": spec.get("layer"),
            "annotation": bool(spec.get("annotation")),
            "kgLabel": spec.get("kg_label"),
            "ontoClass": expand(spec["onto_class"], prefixes, onto_base)
            if spec.get("onto_class") else None,
            "keyAttrs": list(keys),
            "keyAttrsRaw": raw_keys,
            "keyAttrsUsed": used,
            "keyAttrsMissing": missing,
            "keysFallback": fallback,
            "fallbackKgKey": str(kg_inst) if fallback else None,
            "fallbackOntoKey": expand(onto_inst, prefixes, onto_base) if fallback else None,
            "kgKeys": kg_keys,
            "ontoKeys": onto_keys,
        })
    return specs, excluded


# ---------------------------------------------------------------------------
# 关系契约（M2.19）：rel-map.yaml = 名称映射的权威表；topology.yaml = asserted 集合交叉核对
# ---------------------------------------------------------------------------
def load_relation_map(path):
    """读 rel-map.yaml（`relations` 是按关系码键控的 dict；`inverse_relations` 单列）。"""
    m = read_yaml(path)
    rels = m.get("relations")
    if not isinstance(rels, dict) or not rels:
        raise RuntimeError("rel-map 缺 `relations` 块（应为按关系码键控的 dict）或为空：%s" % path)
    return m


def topology_relation_facts(path):
    """从 topology.yaml 取 asserted 集合、派生/逆关系（**只用于交叉核对**，名称映射仍以 rel-map 为准）。"""
    t = read_yaml(path)
    rels = t.get("relations")
    if not isinstance(rels, list) or not rels:
        raise RuntimeError("topology 缺 `relations` 块（应为列表）或为空：%s" % path)
    asserted, excluded = [], []
    for item in rels:
        item = item or {}
        code = item.get("code")
        if not code:
            continue
        derived = (item.get("asserted_in_ccf") is False) or bool(item.get("derived_from"))
        (excluded if derived else asserted).append(code)
    inverse = [str((i or {}).get("code")) for i in (t.get("inverse_relations") or [])
               if (i or {}).get("code")]
    return {"asserted": asserted, "excluded": excluded, "inverse": inverse,
            "facts": t.get("topology_facts") or {}}


def build_relations(rel_map, topo, prefixes, onto_base, specs, include_derived=False):
    """编译关系契约 → 关系规格。

    返回 (relspecs, excluded_relations, included_derived, issues)。

    - 默认比较面 = rel-map `asserted: true` 的关系型（**契约标志驱动**，不是硬编码名单）。
    - `include_derived=True`（CLI `--include-derived-relations`）时**追加**带 `derived_from`
      标志的派生关系（真机只有 `HAS_EVIDENCE`：观测证据边，`from: "*"` → Evidence）。
      其 `from: "*"` 按**每个非抽象源端类型**扇出成独立规格（`code = <CODE>@<源类型>`），
      因为端点身份键依赖源端类型，不能一套键硬套；扇出时排除目标端类型本身（避免自环噪声）。
    - `excluded_relations` **只由契约标志驱动**（rel-map `asserted != true` /
      `inverse_relations` / topology `asserted_in_ccf: false` 或 `derived_from`），不是硬编码名单。
    - 端点身份键**复用 `build_specs` 已编译好的同一套 `kgKeys` / `ontoKeys`**（不另写一份口径）。
    """
    by_tc = {s["typeCode"]: s for s in specs}
    rels = rel_map.get("relations") or {}
    inv_rels = rel_map.get("inverse_relations") or {}
    relspecs, excluded, included_derived, issues = [], [], [], []

    def _one(code, r, f_tc, t_tc, derived):
        """编译单条关系规格；端点类型取不到 ⇒ 记 issue 并返回 None（**不静默跳过**）。"""
        kg_rt, onto_p = r.get("kg_reltype"), r.get("onto_property")
        probs = []
        if not kg_rt:
            probs.append("缺 kg_reltype")
        if not onto_p:
            probs.append("缺 onto_property")
        fs, ts = by_tc.get(f_tc), by_tc.get(t_tc)
        if fs is None:
            probs.append("源端点类型 %r 不在 type-map 的非抽象类型里（无法构造端点身份）" % (f_tc,))
        if ts is None:
            probs.append("目标端点类型 %r 不在 type-map 的非抽象类型里（无法构造端点身份）" % (t_tc,))
        if probs:
            issues.append("关系 %s 契约不自洽：%s" % (code, "；".join(probs)))
            return None
        spec = {
            "code": code, "fromType": f_tc, "toType": t_tc,
            "kgRelType": str(kg_rt), "ontoProperty": expand(onto_p, prefixes, onto_base),
            "kgFromLabel": fs["kgLabel"], "kgToLabel": ts["kgLabel"],
            "ontoFromClass": fs["ontoClass"], "ontoToClass": ts["ontoClass"],
            "kgFromKeys": list(fs["kgKeys"]), "kgToKeys": list(ts["kgKeys"]),
            "ontoFromKeys": list(fs["ontoKeys"]), "ontoToKeys": list(ts["ontoKeys"]),
            "fromKeyAttrs": list(fs["keyAttrs"]), "toKeyAttrs": list(ts["keyAttrs"]),
            "cardinality": r.get("cardinality"), "crossLayer": r.get("cross_layer"),
            "inverseCode": r.get("inverse_code"),
            "ontoInverseProperty": r.get("onto_inverse_property"),
            "derived": bool(derived),
            "baseCode": (str(code).split("@")[0] if derived else code),
        }
        if derived:
            spec["derivedFrom"] = r.get("derived_from")
            spec["wildcardFrom"] = (r.get("from") == "*")
        return spec

    for code in sorted(rels):
        r = rels[code] or {}
        if r.get("asserted") is True:
            continue
        if include_derived and r.get("derived_from"):
            f_tc = r.get("from")
            sources = ([s["typeCode"] for s in specs if s["typeCode"] != r.get("to")]
                       if f_tc == "*" else [f_tc])
            got = []
            for f in sources:
                spec = _one(("%s@%s" % (code, f)) if f_tc == "*" else code,
                            r, f, r.get("to"), True)
                if spec is not None:
                    relspecs.append(spec)
                    got.append(spec["code"])
            included_derived.append({
                "code": code, "from": r.get("from"), "to": r.get("to"),
                "kgRelType": r.get("kg_reltype"), "ontoProperty": r.get("onto_property"),
                "derivedFrom": r.get("derived_from"), "wildcardFrom": (f_tc == "*"),
                "fanOutCount": len(got), "codes": got,
                "basis": "rel-map.yaml relations.%s.derived_from = %s（派生边；仅因 "
                         "--include-derived-relations 才进入比较面）"
                         % (code, r.get("derived_from"))})
        else:
            excluded.append({
                "code": code, "from": r.get("from"), "to": r.get("to"),
                "kgRelType": r.get("kg_reltype"),
                "ontoProperty": r.get("onto_property"),
                "basis": "rel-map.yaml relations.%s.asserted != true（契约标志）%s"
                         % (code, ("；派生边 " + str(r.get("derived_from"))
                                   + " 可用 --include-derived-relations 纳入比较面")
                            if r.get("derived_from") else "")})
    for code in sorted(inv_rels):
        r = inv_rels[code] or {}
        excluded.append({"code": code, "from": r.get("from"), "to": r.get("to"),
                         "kgRelType": r.get("kg_reltype"),
                         "ontoProperty": r.get("onto_property"),
                         "basis": "rel-map.yaml inverse_relations.%s（逆关系，asserted: false）" % code})

    asserted_topology = set((topo or {}).get("asserted") or [])
    relmap_asserted = {c for c in rels if (rels[c] or {}).get("asserted") is True}
    only_topo = sorted(asserted_topology - relmap_asserted)
    only_map = sorted(relmap_asserted - asserted_topology)
    if only_topo or only_map:
        issues.append("关系契约两侧不一致：topology.yaml 断言但 rel-map 未标 asserted = %s；"
                      "rel-map 标 asserted 但 topology 未断言 = %s"
                      % (only_topo or "—", only_map or "—"))
    facts = (topo or {}).get("facts") or {}
    if facts.get("relation_count") is not None and facts["relation_count"] != len(rels):
        issues.append("关系契约计数不一致：topology_facts.relation_count = %s 但 rel-map relations = %d"
                      % (facts["relation_count"], len(rels)))
    if facts.get("inverse_relation_count") is not None \
            and facts["inverse_relation_count"] != len(inv_rels):
        issues.append("逆关系计数不一致：topology_facts.inverse_relation_count = %s 但 rel-map "
                      "inverse_relations = %d" % (facts["inverse_relation_count"], len(inv_rels)))

    for code in sorted(relmap_asserted):
        r = rels[code] or {}
        spec = _one(code, r, r.get("from"), r.get("to"), False)
        if spec is not None:
            relspecs.append(spec)
    return relspecs, excluded, included_derived, issues


# ---------------------------------------------------------------------------
# 采集（KG / ONTO 各自独立回读；任何失败 = BLOCKED，绝不当作 PASS）
# ---------------------------------------------------------------------------
def _kg_one_relation(sess, rs, inv):
    """KG 半单条关系型：端点身份集合 = {(源端点键元组, 目标端点键元组)}。

    端点一律**按标签过滤**（`(a:CC_From)-[r:CC_Rel]->(b:CC_To)`），因为 KG 是与既有 v2 图
    共存的共享图：裸名 `ACCESSES` / `DEPLOYED_ON`（`targets.yaml#colliding_relationship_types`）
    属于 v2 数据，只计数、不比较。
    """
    rt, code = rs["kgRelType"], rs["code"]
    aSel = ", ".join("a.`%s` AS s%d" % (k, i) for i, k in enumerate(rs["kgFromKeys"]))
    bSel = ", ".join("b.`%s` AS t%d" % (k, i) for i, k in enumerate(rs["kgToKeys"]))
    out = {"code": code, "kgRelType": rt, "ontoProperty": rs["ontoProperty"],
           "fromType": rs["fromType"], "toType": rs["toType"],
           "relTypePresent": rt in inv, "edgeTotal": int(inv.get(rt, 0)),
           "bareRelType": code, "bareNameEdges": 0, "identities": set(),
           "edgeTyped": 0, "edgeUnmatchedType": 0, "unmatchedTypeSamples": [],
           "unresolvedEdges": 0, "unresolvedSamples": [], "unresolvedReasons": {}}
    if code != rt and code in inv:
        out["bareNameEdges"] = int(inv[code])
        out["bareNameBasis"] = ("裸名关系类型 %r 属于既有 v2 图（shared/deploy/targets.yaml "
                                "colliding_relationship_types），端点标签无 CC_ 前缀 ⇒ 按端点类型"
                                "过滤后不进入比较，只计数" % code)
    q = ("MATCH (a:`%s`)-[r:`%s`]->(b:`%s`) RETURN %s, %s"
         % (rs["kgFromLabel"], rt, rs["kgToLabel"], aSel, bSel))
    _assert_readonly(q, CYPHER_READONLY_HEADS, "Cypher")
    rows = list(sess.run(q))
    nf, nt = len(rs["kgFromKeys"]), len(rs["kgToKeys"])
    for rec in rows:
        st = tuple(_norm(rec.get("s%d" % i)) for i in range(nf))
        tt = tuple(_norm(rec.get("t%d" % i)) for i in range(nt))
        bad = []
        if all(v is None for v in st):
            st, bad = (NO_KEY,), bad + ["src-key-missing"]
        if all(v is None for v in tt):
            tt, bad = (NO_KEY,), bad + ["dst-key-missing"]
        out["identities"].add((st, tt))
        if bad:
            out["unresolvedEdges"] += 1
            for b in bad:
                out["unresolvedReasons"][b] = out["unresolvedReasons"].get(b, 0) + 1
            if len(out["unresolvedSamples"]) < SAMPLE_LIMIT:
                out["unresolvedSamples"].append(
                    "%s -%s-> %s" % (_fmt_tuple(st), code, _fmt_tuple(tt)))
    out["edgeTyped"] = len(rows)
    remainder = max(0, out["edgeTotal"] - len(rows))
    if rs.get("wildcardFrom"):
        # 通配源端（derived `from: "*"`）扇出：该关系型总边数含**其它源端类型**的边，
        # 它们由各自的扇出规格计数 ⇒ 不计入本规格的「端点类型不符」（否则 W4 会被凭空放大）。
        out["edgeOtherSources"] = remainder
        out["edgeUnmatchedType"] = 0
    else:
        out["edgeUnmatchedType"] = remainder
    if out["edgeUnmatchedType"]:
        q2 = ("MATCH (a)-[r:`%s`]->(b) RETURN labels(a) AS la, labels(b) AS lb, count(*) AS c"
              " ORDER BY c DESC" % rt)
        _assert_readonly(q2, CYPHER_READONLY_HEADS, "Cypher")
        out["unmatchedTypeSamples"] = [
            "%s -%s-> %s ×%d" % (list(rec["la"]), code, list(rec["lb"]), int(rec["c"]))
            for rec in list(sess.run(q2))[:SAMPLE_LIMIT]]
    return out


def collect_onto_relations(cfg, relspecs, graph):
    """ONTO 半：在指定命名图内逐关系型取边身份集合（只读 SELECT / COUNT）。"""
    out = {"targetGraph": graph, "relations": {}}
    for rs in relspecs:
        code, prop = rs["code"], rs["ontoProperty"]
        fk, tk = rs["ontoFromKeys"], rs["ontoToKeys"]
        sSel = " ".join("?s%d" % i for i in range(len(fk)))
        oSel = " ".join("?o%d" % i for i in range(len(tk)))
        sOpt = " ".join("OPTIONAL { ?s <%s> ?s%d }" % (k, i) for i, k in enumerate(fk))
        oOpt = " ".join("OPTIONAL { ?o <%s> ?o%d }" % (k, i) for i, k in enumerate(tk))
        q_any = ("SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?s <%s> ?o } }" % (graph, prop))
        any_n = int(sparql(cfg, q_any)[0]["n"]["value"])
        q = ("SELECT DISTINCT ?s ?o %s %s WHERE { GRAPH <%s> { ?s a <%s> . ?o a <%s> ."
             " ?s <%s> ?o . %s %s } }"
             % (sSel, oSel, graph, rs["ontoFromClass"], rs["ontoToClass"], prop, sOpt, oOpt))
        rows = sparql(cfg, q)
        row = {"code": code, "ontoProperty": prop, "kgRelType": rs["kgRelType"],
               "fromType": rs["fromType"], "toType": rs["toType"],
               "relTypePresent": None, "edgeTotal": any_n, "edgeTyped": 0,
               "edgeUnmatchedType": 0, "unmatchedTypeSamples": [],
               "unresolvedEdges": 0, "unresolvedSamples": [], "unresolvedReasons": {},
               "identities": set()}
        for b in rows:
            st = tuple(_norm(b.get("s%d" % i, {}).get("value")) for i in range(len(fk)))
            tt = tuple(_norm(b.get("o%d" % i, {}).get("value")) for i in range(len(tk)))
            bad = []
            if all(v is None for v in st):
                st, bad = (NO_KEY,), bad + ["src-key-missing"]
            if all(v is None for v in tt):
                tt, bad = (NO_KEY,), bad + ["dst-key-missing"]
            row["identities"].add((st, tt))
            if bad:
                row["unresolvedEdges"] += 1
                for x in bad:
                    row["unresolvedReasons"][x] = row["unresolvedReasons"].get(x, 0) + 1
                if len(row["unresolvedSamples"]) < SAMPLE_LIMIT:
                    row["unresolvedSamples"].append(
                        "%s -%s-> %s" % (_fmt_tuple(st), code, _fmt_tuple(tt)))
        row["edgeTyped"] = len(rows)
        remainder = max(0, any_n - len(rows))
        if rs.get("wildcardFrom"):
            # 同上：通配源端的余量属于**其它扇出规格**的边，不计入本规格的「端点类型不符」。
            row["edgeOtherSources"] = remainder
            row["edgeUnmatchedType"] = 0
        else:
            row["edgeUnmatchedType"] = remainder
        out["relations"][code] = row
    return out


def collect_kg(cfg, password, specs, relspecs=None):
    """KG 半：读 `:CC_Meta`、逐类型身份元组（只读 MATCH/RETURN），可选再读关系边身份。"""
    from neo4j import GraphDatabase
    pwd = password or os.environ.get(cfg["password_env"])
    if not pwd:
        raise RuntimeError(
            "缺少口令：请设置环境变量 %s（G22 只读门禁不接受无凭据的「通过」，"
            "也**不回退**到任何硬编码口令；dry-run 到此为止）" % cfg["password_env"])
    try:
        driver = GraphDatabase.driver(cfg["bolt_uri"], auth=(cfg["user"], pwd),
                                      notifications_min_severity="OFF")
    except TypeError:
        driver = GraphDatabase.driver(cfg["bolt_uri"], auth=(cfg["user"], pwd))
    ml = cfg["meta_label"]
    out = {"metaLabel": ml, "metaCount": 0, "snapshotId": None, "metaProps": None,
           "types": {}, "relations": {}, "relTypeInventory": {},
           "boltUri": cfg["bolt_uri"], "database": cfg["database"]}
    try:
        with driver.session(database=cfg["database"]) as sess:
            q_meta = "MATCH (m:`%s`) RETURN properties(m) AS props" % ml
            _assert_readonly(q_meta, CYPHER_READONLY_HEADS, "Cypher")
            rows = list(sess.run(q_meta))
            out["metaCount"] = len(rows)
            if rows:
                props = dict(rows[0]["props"])
                out["metaProps"] = {k: _norm(v) for k, v in props.items()}
                sid = props.get(cfg["meta_snapshot_field"])
                out["snapshotId"] = str(sid) if sid is not None else None
            for spec in specs:
                tc = spec["typeCode"]
                ret = ", ".join("n.`%s` AS k%d" % (k, i) for i, k in enumerate(spec["kgKeys"]))
                q = "MATCH (n:`%s`) RETURN %s" % (spec["kgLabel"], ret)
                _assert_readonly(q, CYPHER_READONLY_HEADS, "Cypher")
                recs = list(sess.run(q))
                counts = Counter()
                for rec in recs:
                    t = tuple(_norm(rec.get("k%d" % i)) for i in range(len(spec["kgKeys"])))
                    if all(v is None for v in t):
                        t = (NO_KEY,)
                    counts[t] += 1
                out["types"][tc] = {
                    "label": spec["kgLabel"], "keys": list(spec["kgKeys"]),
                    "nodeCount": len(recs), "subjectCount": len(recs),
                    "identities": set(counts), "identityCounts": counts,
                    "multiValuedSubjects": 0,   # Neo4j 节点属性恒单值（键不会是数组）
                }
            if relspecs:
                q_inv = "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c"
                _assert_readonly(q_inv, CYPHER_READONLY_HEADS, "Cypher")
                inv = {}
                for rec in sess.run(q_inv):
                    inv[str(rec["t"])] = int(rec["c"])
                out["relTypeInventory"] = dict(sorted(inv.items()))
                for rs in relspecs:
                    out["relations"][rs["code"]] = _kg_one_relation(sess, rs, inv)
    finally:
        driver.close()
    return out


def sparql(cfg, query, timeout=180):
    _assert_readonly(query, SPARQL_READONLY_HEADS, "SPARQL")
    url = "%s/repositories/%s" % (cfg["gdb_url"].rstrip("/"),
                                  urllib.parse.quote(cfg["repo"]))
    data = urllib.parse.urlencode({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Accept": "application/sparql-results+json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode("utf-8", "replace")
        raise RuntimeError("SPARQL -> HTTP %s：%s\n%s" % (exc.code, body, query))


def list_named_graphs(cfg):
    q = "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } } ORDER BY ?g"
    rows = sparql(cfg, q)
    return [{"iri": r["g"]["value"], "tail": tail_of(r["g"]["value"])} for r in rows]


def collect_onto_identities(cfg, specs, graph):
    """ONTO 半：在**指定命名图**内逐类型取身份元组（只读 SELECT）。"""
    out = {"targetGraph": graph, "types": {}}
    for spec in specs:
        keys = spec["ontoKeys"]
        sel = " ".join("?k%d" % i for i in range(len(keys)))
        opt = " ".join("OPTIONAL { ?s <%s> ?k%d }" % (k, i) for i, k in enumerate(keys))
        q = ("SELECT DISTINCT ?s %s WHERE { GRAPH <%s> { ?s a <%s> . %s } }"
             % (sel, graph, spec["ontoClass"], opt))
        rows = sparql(cfg, q)
        subjects, row_wise, counts = set(), Counter(), Counter()
        for b in rows:
            s = b["s"]["value"]
            subjects.add(s)
            row_wise[s] += 1
            t = tuple(_norm(b.get("k%d" % i, {}).get("value")) for i in range(len(keys)))
            if all(v is None for v in t):
                t = (NO_KEY,)
            counts[t] += 1
        out["types"][spec["typeCode"]] = {
            "class": spec["ontoClass"], "keys": list(keys),
            "subjectCount": len(subjects), "nodeCount": len(rows),
            "identities": set(counts), "identityCounts": counts,
            "multiValuedSubjects": sum(1 for _s, c in row_wise.items() if c > 1),
        }
    return out


# ---------------------------------------------------------------------------
# G22-01 目标图选择（fail-closed：绝不「随便挑一张最新图」）
# ---------------------------------------------------------------------------
def latest_snapshot_graph(named):
    snaps = sorted({g["tail"] for g in named if SNAPSHOT_RE.match(g["tail"])})
    if not snaps:
        return None, None
    tail = snaps[-1]
    iri = next((g["iri"] for g in named if g["tail"] == tail), None)
    return tail, (iri or (DEFAULTS["graph_template"].format(snapshotId=tail)))


def select_target_graph(kg_snapshot, named, explicit):
    """返回 (iri, source, matches_kg, detail_lines, blocked_reason)。"""
    detail = []
    latest_tail, latest_iri = latest_snapshot_graph(named)
    detail.append("ONTO 命名图 %d 张；快照图末段（时间序）= %s"
                  % (len(named), sorted({g["tail"] for g in named
                                         if SNAPSHOT_RE.match(g["tail"])}) or "（无）"))
    detail.append("ONTO **最新** 快照图末段 = %r%s"
                  % (latest_tail, ("（图 %s）" % latest_iri) if latest_iri else ""))
    if explicit:
        want = str(explicit)
        hit = next((g for g in named if g["iri"] == want), None)
        if hit is None:
            hit = next((g for g in named if g["tail"] == tail_of(want)), None)
        if hit is None:
            return (None, "--onto-graph（显式）", False,
                    detail + ["显式指定的命名图 %r 不在该仓库的命名图清单里 ⇒ BLOCKED"
                              % (want,)],
                    "G22-01 显式指定的 ONTO 命名图不存在：%r" % want)
        matches = bool(kg_snapshot) and hit["tail"] == kg_snapshot
        detail.append("显式 --onto-graph 命中图 %s（末段 %r）；与 `:CC_Meta.%s` = %r 一致 = %s"
                      % (hit["iri"], hit["tail"], "snapshotId", kg_snapshot, matches))
        return hit["iri"], "--onto-graph（显式）", matches, detail, None
    if not kg_snapshot:
        return (None, "auto(:CC_Meta.snapshotId)", False, detail,
                "G22-01 无法自动选图：KG `:CC_Meta.snapshotId` 缺失或为空")
    hit = next((g for g in named if g["tail"] == kg_snapshot), None)
    if hit is None:
        detail.append("要求「图 IRI 末段 == `:CC_Meta.snapshotId` = %r」的命名图；实到末段 %s"
                      % (kg_snapshot, sorted({g["tail"] for g in named}) or "（无）"))
        return (None, "auto(:CC_Meta.snapshotId)", False, detail,
                "G22-01 找不到末段 == `:CC_Meta.snapshotId` = %r 的 ONTO 命名图 ⇒ BLOCKED"
                "（fail-closed：不得退化为「随便挑一张最新图」而静默通过）" % kg_snapshot)
    detail.append("自动选中图 %s（末段 %r == `:CC_Meta.snapshotId`）" % (hit["iri"], hit["tail"]))
    return hit["iri"], "auto(:CC_Meta.snapshotId)", True, detail, None


# ---------------------------------------------------------------------------
# 判定（纯函数：真机与 --selftest 走同一套代码）
# ---------------------------------------------------------------------------
def evaluate(kg, onto, specs, cfg, strict=None, contract_issues=None):
    """返回 (blocked, errors, warnings, stats)。

    `blocked` 非空 ⇒ BLOCKED（exit 2），此时**不产生** PASS/FAIL。
    KG 侧不可达由调用方在采集异常时传出（`kg=None`），本函数只处理「能读到但内容不成立」。
    """
    if strict is None:
        strict = bool(cfg.get("strict", False))
    min_subjects = cfg.get("min_abox_subjects")
    crit, errs, warns, blocked = [], [], [], []
    named = (onto or {}).get("namedGraphs") or []
    onto_types = (onto or {}).get("types") or {}
    kg_types = (kg or {}).get("types") or {}
    meta_count = int((kg or {}).get("metaCount") or 0)
    sid = (kg or {}).get("snapshotId")

    def add(cid, title, status, lines):
        crit.append({"id": cid, "title": title, "status": status,
                     "detail": [str(x) for x in lines]})

    # ---- 参数校验（与 CLI 同一套；0 / 负数不得用来关掉下限）----
    try:
        floor = int(min_subjects)
    except (TypeError, ValueError):
        floor = None
        errs.append("参数错误：--min-abox-subjects 必须是整数，实得 %r" % (min_subjects,))
    if floor is not None and floor <= 0:
        errs.append("参数错误：--min-abox-subjects 必须 > 0，实得 %d ⇒ 禁止用 0/负数关闭反恒真下限"
                    "（0 会退化成「检查 0 条也算通过」）" % floor)

    compared_types = [s["typeCode"] for s in specs]
    kg_total = sum(int((kg_types.get(s["typeCode"]) or {}).get("subjectCount") or 0) for s in specs)
    onto_total = sum(int((onto_types.get(s["typeCode"]) or {}).get("subjectCount") or 0) for s in specs)
    total_subjects = min(kg_total, onto_total)

    target, source, matches, sel_detail, sel_block = select_target_graph(
        sid, named, cfg.get("onto_graph_explicit"))
    latest_tail, latest_iri = latest_snapshot_graph(named)

    # ---- G22-00 可达性 ----
    l00 = ["KG `:%s` 节点数 = %d（需恰好 1）" % (cfg.get("meta_label"), meta_count),
           "KG `:%s.%s` = %r" % (cfg.get("meta_label"), cfg.get("meta_snapshot_field"), sid),
           "KG 侧被比较类型主体合计 = %d" % kg_total,
           "ONTO 侧命名图 %d 张；目标图 = %r" % (len(named), target),
           "ONTO 侧被比较类型主体合计 = %d" % onto_total]
    if (kg or {}).get("metaProps"):
        l00.append("KG `:%s` 属性 = %s" % (cfg.get("meta_label"), (kg or {}).get("metaProps")))
    if meta_count != 1:
        blocked.append("G22-00 KG 侧读不到唯一的 `:%s`（节点数 = %d）⇒ BLOCKED"
                       % (cfg.get("meta_label"), meta_count))
    if kg_total <= 0:
        blocked.append("G22-00 KG 侧未取到任何被比较类型的主体（合计 0）⇒ BLOCKED")
    if not named:
        blocked.append("G22-00 ONTO 侧仓库 %s 没有任何命名图 ⇒ BLOCKED" % cfg.get("repo"))
    if sel_block:
        blocked.append(sel_block)
    if not blocked and onto_total <= 0:
        blocked.append("G22-00 ONTO 侧目标图 %s 里未取到任何被比较类型的主体（合计 0）⇒ BLOCKED"
                       "（禁止「检查 0 条也算通过」）" % target)
    add("G22-00", "可达性（两侧都真的取到数据）", "BLOCKED" if blocked else "OK", l00)

    # ---- G22-01 目标图选择 ----
    l01 = list(sel_detail) + [
        "目标图 = %r（来源：%s）" % (target, source),
        "与 `:%s.%s` 一致 = %s" % (cfg.get("meta_label"), cfg.get("meta_snapshot_field"), matches),
        "ONTO **最新** 快照图 = %r（末段 %r）" % (latest_iri, latest_tail)]
    st01 = "OK"
    if sel_block:
        st01 = "BLOCKED"
    elif not matches:
        st01 = "WARN"
        warns.append("W0 目标图与 KG 快照不一致：ONTO 图末段 %r ≠ `:%s.%s` = %r"
                     "（显式 --onto-graph 允许这种 A/B 取证；默认口径下该不一致是真实缺陷，"
                     "`--strict` 升 ERROR）"
                     % (tail_of(target), cfg.get("meta_label"),
                        cfg.get("meta_snapshot_field"), sid))
    if sid and not SNAPSHOT_RE.match(str(sid)):
        warns.append("W3 KG `:%s.%s` = %r 不是合法快照 id ⇒ 无法核对目标图身份"
                     % (cfg.get("meta_label"), cfg.get("meta_snapshot_field"), sid))
    add("G22-01", "目标图选择（默认要求末段 == :CC_Meta.snapshotId；找不到 ⇒ BLOCKED）", st01, l01)

    # ---- 逐类型身份集（G22-02 / G22-03 / W2）----
    rows, l02, l03 = [], [], []
    diff_types, collapse_types, fan_types = [], [], []
    if blocked:
        add("G22-02", "身份集相等（逐类型）", "SKIPPED(BLOCKED)", ["未评估：G22-00 / G22-01 已 BLOCKED"])
        add("G22-03", "身份唯一性（去重后元组数 < 主体数 ⇒ ERROR）", "SKIPPED(BLOCKED)",
            ["未评估：G22-00 / G22-01 已 BLOCKED"])
    else:
        for spec in specs:
            tc = spec["typeCode"]
            k = kg_types.get(tc) or {}
            o = onto_types.get(tc) or {}
            ks, os_ = set(k.get("identities") or ()), set(o.get("identities") or ())
            k_nodes = int(k.get("subjectCount") or 0)
            o_subs = int(o.get("subjectCount") or 0)
            only_kg, only_onto = ks - os_, os_ - ks
            same = (ks == os_)
            k_multi = int(k.get("multiValuedSubjects") or 0)
            o_multi = int(o.get("multiValuedSubjects") or 0)
            k_collapse = len(ks) < k_nodes
            o_collapse = len(os_) < o_subs
            k_fan = len(ks) > k_nodes or k_multi > 0
            o_fan = len(os_) > o_subs or o_multi > 0
            row = {
                "typeCode": tc, "kgLabel": spec["kgLabel"], "ontoClass": spec["ontoClass"],
                "keyAttrs": spec["keyAttrs"], "kgKeys": spec["kgKeys"],
                "ontoKeys": spec["ontoKeys"], "keysFallback": spec["keysFallback"],
                "kgNodes": k_nodes, "ontoSubjects": o_subs,
                "kgDistinct": len(ks), "ontoDistinct": len(os_),
                "identitySetsEqual": same,
                "onlyKgCount": len(only_kg), "onlyOntoCount": len(only_onto),
                "onlyKgSample": sorted(_fmt_tuple(t) for t in only_kg)[:SAMPLE_LIMIT],
                "onlyOntoSample": sorted(_fmt_tuple(t) for t in only_onto)[:SAMPLE_LIMIT],
                "kgCollapse": k_collapse, "ontoCollapse": o_collapse,
                "kgFanOut": k_fan, "ontoFanOut": o_fan,
                "kgMultiValuedSubjects": k_multi, "ontoMultiValuedSubjects": o_multi,
            }
            rows.append(row)
            flag = "SAME" if same else "DIFF"
            line = ("[%s] %-20s keys=%s  kg %d/键%d  onto %d/键%d  onlyKg=%d onlyOnto=%d"
                    % (flag, tc, spec["keyAttrs"], k_nodes, len(ks), o_subs, len(os_),
                       len(only_kg), len(only_onto)))
            if only_kg:
                line += "  kgOnly=%s" % row["onlyKgSample"]
            if only_onto:
                line += "  ontoOnly=%s" % row["onlyOntoSample"]
            l02.append(line)
            if not same:
                diff_types.append(tc)
                errs.append("G22-02 身份集不等价：%s（keys=%s）onlyKg=%d onlyOnto=%d；"
                            "样例 onlyKg=%s onlyOnto=%s（单值截断 %d 字符，最多 %d 条）"
                            % (tc, spec["keyAttrs"], len(only_kg), len(only_onto),
                               row["onlyKgSample"], row["onlyOntoSample"],
                               VALUE_MAX, SAMPLE_LIMIT))
            if k_collapse or o_collapse:
                collapse_types.append(tc)
                dup_k = [t for t, c in (k.get("identityCounts") or {}).items() if c > 1]
                dup_o = [t for t, c in (o.get("identityCounts") or {}).items() if c > 1]
                errs.append("G22-03 身份不唯一：%s KG 主体 %d ⇒ 去重键 %d%s；ONTO 主体 %d ⇒ 去重键 %d%s"
                            "；重复键样例 KG=%s ONTO=%s"
                            % (tc, k_nodes, len(ks),
                               "（**塌缩**）" if k_collapse else "",
                               o_subs, len(os_),
                               "（**塌缩**）" if o_collapse else "",
                               sorted(_fmt_tuple(t) for t in dup_k)[:SAMPLE_LIMIT],
                               sorted(_fmt_tuple(t) for t in dup_o)[:SAMPLE_LIMIT]))
            if k_fan or o_fan:
                fan_types.append(tc)
                warns.append("W2 键属性非单值：%s KG 去重键 %d / 主体 %d（多值主体 %d）；"
                             "ONTO 去重键 %d / 主体 %d（多值主体 %d）⇒ 一个主体有多个键值，"
                             "G22-03 的「去重 < 主体」抓不到这一形态（默认 WARN；--strict 升 ERROR）"
                             % (tc, len(ks), k_nodes, k_multi, len(os_), o_subs, o_multi))
        add("G22-02", "身份集相等（逐非抽象 typeCode，左右身份元组集合必须相等）",
            "ERROR" if diff_types else "OK",
            l02 + ["⇒ 参与比较 %d 类；身份集不等价 %d 类：%s"
                   % (len(rows), len(diff_types), ",".join(diff_types) or "—")])
        add("G22-03", "身份唯一性（去重后身份元组数 < 该类型主体数 ⇒ ERROR）",
            "ERROR" if collapse_types else "OK",
            l03 + ["逐类型：主体数 / 去重键数 / 是否塌缩"] +
            ["  %-20s kg %d→键%d%s  onto %d→键%d%s"
             % (r["typeCode"], r["kgNodes"], r["kgDistinct"],
                " 塌缩" if r["kgCollapse"] else "",
                r["ontoSubjects"], r["ontoDistinct"],
                " 塌缩" if r["ontoCollapse"] else "") for r in rows] +
            ["⇒ 塌缩（重复键）类型 %d 类：%s" % (len(collapse_types),
                                                 ",".join(collapse_types) or "—")])

    # ---- G22-04 反恒真下限 ----
    l04 = ["两侧被比较类型主体合计 = min(KG %d, ONTO %d) = %d（下限 --min-abox-subjects = %s）"
           % (kg_total, onto_total, total_subjects, floor),
           "实际参与比较的非抽象类型数 = %d（下限 %d，无 CLI 开关，不可关闭）"
           % (len(compared_types), MIN_COMPARED_TYPES),
           "参与比较类型：%s" % (",".join(compared_types) or "（无）")]
    st04 = "OK"
    if floor is None or floor <= 0:
        st04 = "PARAM_ERROR"
    else:
        if total_subjects < floor:
            st04 = "ERROR"
            errs.append("G22-04 反恒真下限未达：两侧被比较类型主体合计（取较小侧）= "
                        "min(KG %d, ONTO %d) = %d < --min-abox-subjects %d ⇒ ERROR"
                        "（禁止「检查 0 条也算通过」；合计取较小侧 ⇒ 任一侧是空壳都不许通过）"
                        % (kg_total, onto_total, total_subjects, floor))
        if len(compared_types) < MIN_COMPARED_TYPES:
            st04 = "ERROR"
            errs.append("G22-04 参与比较的非抽象类型数 = %d < %d ⇒ ERROR"
                        "（比较面被裁剪到过小 ⇒ 禁止通过；无 CLI 开关可关掉该下限）"
                        % (len(compared_types), MIN_COMPARED_TYPES))
    add("G22-04", "反恒真下限（主体数合计 + 参与比较类型数）", st04, l04)

    # ---- G22-05 抽象类按契约排除 ----
    excluded = list(cfg.get("excluded_abstract") or [])
    add("G22-05", "抽象类按契约（abstract: true）排除",
        "OK",
        ["排除 %d 类（依据：契约标志 abstract: true，**不是**硬编码类型名单）：" % len(excluded)] +
        ["  %s  kg_label=%s onto_class=%s key_attrs=%s  依据：%s"
         % (e.get("typeCode"), e.get("kgLabel"), e.get("ontoClass"),
            e.get("keyAttrs"), e.get("basis")) for e in excluded])

    # ---- G22-06 契约自洽（fail-closed 的加法）----
    l06, bad06 = [], []
    for spec in specs:
        tc = spec["typeCode"]
        probs = []
        raw = spec.get("keyAttrsRaw")
        if spec["keysFallback"]:
            probs.append("key_attrs 为空 ⇒ 退化为契约声明的实例 id（%s / %s）"
                         % (spec.get("fallbackKgKey"), spec.get("fallbackOntoKey")))
            warns.append("W1 %s 的 key_attrs 为空 ⇒ 身份未由契约定义，退化为实例 id 属性 %s / %s"
                         "（默认 WARN；--strict 升 ERROR）"
                         % (tc, spec.get("fallbackKgKey"), spec.get("fallbackOntoKey")))
        if not isinstance(raw, (list, tuple)):
            probs.append("key_attrs 不是列表：%r" % (raw,))
        if not spec["kgLabel"]:
            probs.append("缺 kg_label")
        if not spec["ontoClass"]:
            probs.append("缺 onto_class")
        if spec["keyAttrsMissing"]:
            probs.append("key_attrs 里有 %s 不在 attrs 中（或该属性缺 kg_property/onto_property）"
                         % spec["keyAttrsMissing"])
        if not spec["keyAttrsUsed"] and not spec["keysFallback"]:
            probs.append("没有任何可用键属性")
        if probs:
            bad06.append(tc)
            errs.append("G22-06 契约不自洽：%s —— %s ⇒ ERROR（键名写错会被静默丢掉，"
                        "使身份元组悄悄少一维）" % (tc, "；".join(probs)))
        l06.append("  %-20s key_attrs=%s → 用到的键=%s%s"
                   % (tc, spec["keyAttrs"], spec["keyAttrsUsed"],
                      "（兜底实例 id）" if spec["keysFallback"] else ""))
    for issue in (contract_issues or []):
        bad06.append("(外部)")
        errs.append("G22-06 契约不自洽：%s" % issue)
    add("G22-06", "契约自洽（key_attrs ⊆ attrs 且带双侧属性名）",
        "ERROR" if bad06 else "OK", l06 + ["⇒ 不自洽类型 %d 个：%s"
                                           % (len(bad06), ",".join(bad06) or "—")])

    # ---- 关系身份集（G22-07 / G22-08 / W4 / W5；M2.19）----
    relspecs = list(cfg.get("relations") or [])
    kg_rels = dict((kg or {}).get("relations") or {})
    onto_rels = dict((onto or {}).get("relations") or {})
    rel_enabled = bool(cfg.get("relations_enabled")) or bool(relspecs)
    rel_floor_raw = cfg.get("min_abox_relations", MIN_ABOX_RELATIONS)
    excluded_rels = list(cfg.get("excluded_relations") or [])
    rel_rows, rel_diff, rel_empty, rel_unres = [], [], [], {}
    kg_rel_total = onto_rel_total = rel_edges_unresolved = 0
    total_relations = None
    rel_floor = None
    if not rel_enabled:
        add("G22-07", "关系身份集等价（逐 asserted 关系型）", "SKIPPED(未启用)",
            ["未评估：本次调用未注入关系契约 / 关系采集结果"
             "（只有 --selftest 的对象夹具走这条路径；真机路径恒为启用）"])
        add("G22-08", "关系反恒真下限（已解析边数合计 + 参与比较关系型数）", "SKIPPED(未启用)",
            ["未评估：同上"])
    else:
        try:
            rel_floor = int(rel_floor_raw)
        except (TypeError, ValueError):
            rel_floor = None
            errs.append("参数错误：--min-abox-relations 必须是整数，实得 %r" % (rel_floor_raw,))
        if rel_floor is not None and rel_floor <= 0:
            errs.append("参数错误：--min-abox-relations 必须 > 0，实得 %d ⇒ 禁止用 0/负数关闭"
                        "反恒真下限（0 会退化成「检查 0 条也算通过」）" % rel_floor)
        derived_specs = [r for r in relspecs if r.get("derived")]
        l07 = ["比较面 = rel-map `asserted: true` 的 %d 条关系型%s；端点身份键**复用**对象维度同一套 "
               "`key_attrs` 口径（不是另写一份）"
               % (len(relspecs) - len(derived_specs),
                  ("" if not derived_specs else
                   " + 派生关系扇出 %d 条（--include-derived-relations）：%s"
                   % (len(derived_specs), ",".join(r["code"] for r in derived_specs)))),
               "排除的契约关系 %d 条（依据：契约标志，**不是**硬编码名单）：%s"
               % (len(excluded_rels), ",".join(e["code"] for e in excluded_rels) or "—")]
        for rs in relspecs:
            code = rs["code"]
            k = kg_rels.get(code) or {}
            o = onto_rels.get(code) or {}
            ks = set(k.get("identities") or ())
            os_ = set(o.get("identities") or ())
            only_kg, only_onto = ks - os_, os_ - ks
            same = (ks == os_)
            k_typed, o_typed = int(k.get("edgeTyped") or 0), int(o.get("edgeTyped") or 0)
            k_un, o_un = int(k.get("unresolvedEdges") or 0), int(o.get("unresolvedEdges") or 0)
            k_unm = int(k.get("edgeUnmatchedType") or 0)
            o_unm = int(o.get("edgeUnmatchedType") or 0)
            k_res, o_res = max(0, k_typed - k_un), max(0, o_typed - o_un)
            kg_rel_total += k_res
            onto_rel_total += o_res
            rel_edges_unresolved += k_un + o_un + k_unm + o_unm
            empty_both = (k_typed == 0 and o_typed == 0)
            row = {
                "code": code, "fromType": rs["fromType"], "toType": rs["toType"],
                "kgRelType": rs["kgRelType"], "ontoProperty": rs["ontoProperty"],
                "fromKeyAttrs": rs["fromKeyAttrs"], "toKeyAttrs": rs["toKeyAttrs"],
                "cardinality": rs.get("cardinality"), "crossLayer": rs.get("crossLayer"),
                "derived": bool(rs.get("derived")), "baseCode": rs.get("baseCode"),
                "derivedFrom": rs.get("derivedFrom"), "wildcardFrom": rs.get("wildcardFrom"),
                "kgRelTypePresent": k.get("relTypePresent"),
                "bareNameEdges": int(k.get("bareNameEdges") or 0),
                "bareNameBasis": k.get("bareNameBasis"),
                "kgEdgeTotal": int(k.get("edgeTotal") or 0), "kgEdgeTyped": k_typed,
                "kgUnmatchedType": k_unm, "kgUnresolved": k_un,
                "ontoEdgeTotal": int(o.get("edgeTotal") or 0), "ontoEdgeTyped": o_typed,
                "ontoUnmatchedType": o_unm, "ontoUnresolved": o_un,
                "kgOtherSources": int(k.get("edgeOtherSources") or 0),
                "ontoOtherSources": int(o.get("edgeOtherSources") or 0),
                "kgResolved": k_res, "ontoResolved": o_res,
                "kgDistinct": len(ks), "ontoDistinct": len(os_),
                "identitySetsEqual": same, "emptyBothSides": empty_both,
                "onlyKgCount": len(only_kg), "onlyOntoCount": len(only_onto),
                "onlyKgSample": sorted(_fmt_edge(t, code) for t in only_kg)[:SAMPLE_LIMIT],
                "onlyOntoSample": sorted(_fmt_edge(t, code) for t in only_onto)[:SAMPLE_LIMIT],
                "unresolvedSamples": (list(k.get("unresolvedSamples") or [])
                                      + list(o.get("unresolvedSamples") or [])
                                      + list(k.get("unmatchedTypeSamples") or [])
                                      + list(o.get("unmatchedTypeSamples") or []))[:SAMPLE_LIMIT],
            }
            rel_rows.append(row)
            flag = "EMPTY" if empty_both else ("SAME" if same else "DIFF")
            line = ("[%s] %-22s %s→%s  kg %d/边%d  onto %d/边%d  onlyKg=%d onlyOnto=%d"
                    % (flag, code, rs["fromType"], rs["toType"], k_res, len(ks), o_res, len(os_),
                       len(only_kg), len(only_onto)))
            if only_kg:
                line += "  kgOnly=%s" % row["onlyKgSample"]
            if only_onto:
                line += "  ontoOnly=%s" % row["onlyOntoSample"]
            if k_un or o_un:
                line += "  [键取不到] kg=%d onto=%d" % (k_un, o_un)
            if k_unm or o_unm:
                line += "  [端点类型不符] kg=%d onto=%d" % (k_unm, o_unm)
            if row["kgOtherSources"] or row["ontoOtherSources"]:
                line += ("  [通配源端扇出：其它源端类型的边由各自扇出规格计数 kg=%d onto=%d，"
                         "不计入端点类型不符]" % (row["kgOtherSources"], row["ontoOtherSources"]))
            if row["bareNameEdges"]:
                line += "  [v2 裸名同名边已排除 %d 条]" % row["bareNameEdges"]
            l07.append(line)
            if not same:
                rel_diff.append(code)
                errs.append("G22-07 关系身份集不等价：%s（%s→%s，kg_reltype=%s）onlyKg=%d onlyOnto=%d；"
                            "样例 onlyKg=%s onlyOnto=%s（端点元组单值截断 %d 字符，最多 %d 条）"
                            % (code, rs["fromType"], rs["toType"], rs["kgRelType"],
                               len(only_kg), len(only_onto), row["onlyKgSample"],
                               row["onlyOntoSample"], VALUE_MAX, SAMPLE_LIMIT))
            if empty_both:
                rel_empty.append(code)
                warns.append("W5 关系型 %s 两侧皆空（%s→%s，kg 0 条 / onto 0 条）⇒ 集合相等但"
                             "**无证据**，不得据此判 PASS（默认 WARN；--strict 升 ERROR）"
                             % (code, rs["fromType"], rs["toType"]))
            if k_un or o_un or k_unm or o_unm:
                rel_unres[code] = {
                    "kgUnresolved": k_un, "ontoUnresolved": o_un,
                    "kgUnmatchedType": k_unm, "ontoUnmatchedType": o_unm,
                    "kgReasons": dict(k.get("unresolvedReasons") or {}),
                    "ontoReasons": dict(o.get("unresolvedReasons") or {}),
                    "samples": row["unresolvedSamples"],
                }
                warns.append("W4 关系型 %s 端点身份不可解析：KG 键取不到 %d / 端点类型不符 %d；"
                             "ONTO 键取不到 %d / 端点类型不符 %d ⇒ 这些边不进集合比较，"
                             "绝不用空串代替、绝不静默跳过（默认 WARN；--strict 升 ERROR）；样例 %s"
                             % (code, k_un, k_unm, o_un, o_unm, row["unresolvedSamples"]))
        total_relations = min(kg_rel_total, onto_rel_total)
        add("G22-07",
            "关系身份集等价（逐 asserted 关系型：比较 {(源端点键元组, 目标端点键元组)}）",
            "ERROR" if rel_diff else "OK",
            l07 + ["⇒ 参与比较 %d 条关系型；边身份集不等价 %d 条：%s"
                   % (len(rel_rows), len(rel_diff), ",".join(rel_diff) or "—")])
        l08 = ["两侧被比较关系型**已解析**边数合计 = min(KG %d, ONTO %d) = %s"
               "（下限 --min-abox-relations = %s；派生关系是否纳入由 "
               "--include-derived-relations 决定）"
               % (kg_rel_total, onto_rel_total, total_relations, rel_floor),
               "实际参与比较的关系型数 = %d（其中 asserted %d 条 / 派生扇出 %d 条；"
               "下限 %d，无 CLI 开关，不可关闭）"
               % (len(relspecs), len(relspecs) - len(derived_specs), len(derived_specs),
                  MIN_COMPARED_RELATIONS),
               "参与比较关系型：%s" % (",".join(r["code"] for r in rel_rows) or "（无）"),
               "两侧皆空的关系型 %d 条（W5 告警 ⇒ 不算证据）：%s"
               % (len(rel_empty), ",".join(rel_empty) or "—"),
               "未解析边合计 = %d（端点键取不到 + 端点类型不符；W4 告警）" % rel_edges_unresolved]
        st08 = "OK"
        if rel_floor is None or rel_floor <= 0:
            st08 = "PARAM_ERROR"
        else:
            if total_relations < rel_floor:
                st08 = "ERROR"
                errs.append("G22-08 关系反恒真下限未达：两侧已解析边数合计（取较小侧）= "
                            "min(KG %d, ONTO %d) = %d < --min-abox-relations %d ⇒ ERROR"
                            "（禁止「检查 0 条也算通过」；合计取较小侧 ⇒ 任一侧是空壳都不许通过）"
                            % (kg_rel_total, onto_rel_total, total_relations, rel_floor))
            if len(relspecs) < MIN_COMPARED_RELATIONS:
                st08 = "ERROR"
                errs.append("G22-08 参与比较的关系型数（asserted + 派生扇出）= %d < %d ⇒ ERROR"
                            "（关系比较面被裁剪到过小 ⇒ 禁止通过；无 CLI 开关可关掉该下限）"
                            % (len(relspecs), MIN_COMPARED_RELATIONS))
        add("G22-08", "关系反恒真下限（已解析边数合计 + 参与比较关系型数；两侧皆空 ⇒ W5）",
            st08, l08)

    # ---- --strict：WARN 升 ERROR（只升不降）----
    if warns and strict:
        errs.extend("--strict 升级为 ERROR：%s" % w for w in warns)

    stats = {
        "metaLabel": cfg.get("meta_label"), "metaCount": meta_count,
        "kgSnapshotId": sid, "kgSnapshotIdValid": bool(sid and SNAPSHOT_RE.match(str(sid))),
        "kgTotalSubjects": kg_total, "ontoTotalSubjects": onto_total,
        "ontoNamedGraphs": named,
        "ontoSnapshotTails": sorted({g["tail"] for g in named
                                     if SNAPSHOT_RE.match(g["tail"])}),
        "latestOntoSnapshotTail": latest_tail, "latestOntoSnapshotGraph": latest_iri,
        "ontoGraphUsed": target, "ontoGraphSource": source,
        "ontoGraphMatchesKgSnapshot": matches,
        "comparedTypes": compared_types, "comparedTypeCount": len(compared_types),
        "excludedAbstract": excluded,
        "totalSubjects": total_subjects,
        "minAboxSubjects": floor, "minComparedTypes": MIN_COMPARED_TYPES,
        "diffTypes": diff_types, "collapseTypes": collapse_types, "fanTypes": fan_types,
        "contractInconsistentTypes": bad06,
        # --- 关系维度（M2.19：G22-07 / G22-08）---
        "relationsEnabled": rel_enabled,
        "relMap": cfg.get("rel_map"), "topology": cfg.get("topology"),
        "comparedRelations": [r["code"] for r in rel_rows],
        "comparedRelationCount": (len(relspecs) if rel_enabled else 0),
        "minAboxRelations": rel_floor, "minComparedRelations": MIN_COMPARED_RELATIONS,
        "kgTotalRelations": kg_rel_total, "ontoTotalRelations": onto_rel_total,
        "totalRelations": total_relations,
        "diffRelations": rel_diff, "emptyRelations": rel_empty,
        "unresolvedEdges": rel_edges_unresolved,
        "unresolvedEdgeByRelation": rel_unres,
        "excludedRelations": excluded_rels,
        "includeDerivedRelations": bool(cfg.get("include_derived_relations")),
        "includedDerivedRelations": list(cfg.get("included_derived_relations") or []),
        "relations": rel_rows,
        "identitySummary": {
            "object": {"identitySetsEqual": not diff_types,
                       "verdict": ("BLOCKED" if blocked else ("ERROR" if diff_types else "OK")),
                       "diffTypes": diff_types, "collapseTypes": collapse_types,
                       "comparedTypeCount": len(compared_types),
                       "kgTotalSubjects": kg_total, "ontoTotalSubjects": onto_total},
            "relation": {"identitySetsEqual": (rel_enabled and not rel_diff),
                         "verdict": ("SKIPPED(未启用)" if not rel_enabled
                                     else ("ERROR" if rel_diff else "OK")),
                         "diffRelations": rel_diff, "emptyRelations": rel_empty,
                         "comparedRelationCount": (len(relspecs) if rel_enabled else 0),
                         "kgTotalRelations": kg_rel_total, "ontoTotalRelations": onto_rel_total},
        },
        "types": rows, "criteria": crit,
        "strict": bool(strict),
    }
    return blocked, errs, warns, stats


# ---------------------------------------------------------------------------
# 判别力自证（--selftest：**无网络**，注入式夹具，走同一套 evaluate）
# ---------------------------------------------------------------------------
GRAPH = "https://credit-card-ops.ai/onto/graph/"
SNAP_A = "ccf-20260928T144216Z-b920b5"   # live3（真机 KG 现状快照）
SNAP_C = "ccf-20260928T155516Z-937b79"   # live5（真机 ONTO 最新命名图）
CC = "https://credit-card-ops.ai/onto/topology#"


def _spec(tc, key_names):
    return {"typeCode": tc, "layer": None, "annotation": False,
            "kgLabel": "CC_" + tc, "ontoClass": CC + tc,
            "keyAttrs": list(key_names), "keyAttrsRaw": list(key_names),
            "keyAttrsUsed": list(key_names), "keyAttrsMissing": [],
            "keysFallback": False, "fallbackKgKey": None, "fallbackOntoKey": None,
            "kgKeys": list(key_names), "ontoKeys": [CC + k for k in key_names]}


SELFTEST_SPECS = [_spec("Table", ["schemaName", "tableName"]),
                  _spec("Host", ["hostname"]),
                  _spec("Session", ["sessionId"])]


def _base_fixture():
    """两侧完全相同的合成 ABox：Table 40 / Host 30 / Session 40（合计 110 >= 100）。"""
    kg_types, onto_types = {}, {}
    for tc, n, mk in (("Table", 40, lambda i: ("public", "tbl_%02d" % i)),
                      ("Host", 30, lambda i: ("host-%02d" % i,)),
                      ("Session", 40, lambda i: ("sess-%03d" % i,))):
        tuples = [mk(i) for i in range(n)]
        kg_types[tc] = {"label": "CC_" + tc, "keys": SELFTEST_SPECS[0]["kgKeys"],
                        "nodeCount": n, "subjectCount": n, "identities": set(tuples),
                        "identityCounts": Counter(tuples), "multiValuedSubjects": 0}
        onto_types[tc] = {"class": CC + tc, "keys": SELFTEST_SPECS[0]["ontoKeys"],
                          "nodeCount": n, "subjectCount": n, "identities": set(tuples),
                          "identityCounts": Counter(tuples), "multiValuedSubjects": 0}
    kg = {"metaLabel": "CC_Meta", "metaCount": 1, "snapshotId": SNAP_A,
          "metaProps": {"snapshotId": SNAP_A}, "types": kg_types}
    onto = {"namedGraphs": [{"iri": GRAPH + SNAP_A, "tail": SNAP_A},
                            {"iri": GRAPH + "tbox", "tail": "tbox"}],
            "targetGraph": GRAPH + SNAP_A, "types": onto_types}
    return kg, onto


# --- 关系夹具（M2.19：5 条关系型 / 12 条边，两侧默认完全相同）-----------------
def _relspec(code, from_tc, to_tc):
    return {"code": code, "fromType": from_tc, "toType": to_tc,
            "kgRelType": "CC_" + code, "ontoProperty": CC + code.lower(),
            "kgFromLabel": "CC_" + from_tc, "kgToLabel": "CC_" + to_tc,
            "ontoFromClass": CC + from_tc, "ontoToClass": CC + to_tc,
            "fromKeyAttrs": ["k"], "toKeyAttrs": ["k"],
            "kgFromKeys": ["k"], "kgToKeys": ["k"],
            "ontoFromKeys": [CC + "k"], "ontoToKeys": [CC + "k"],
            "cardinality": "1:N", "crossLayer": False}


SELFTEST_REL_SPECS = [_relspec("HAS_SESSION", "DatabaseSessionPool", "Session"),
                      _relspec("RUNS_QUERY", "Session", "Query"),
                      _relspec("HOSTS", "Host", "DatabaseSessionPool"),
                      _relspec("ACCESSES", "Query", "Table"),
                      _relspec("USES_DATASOURCE", "AppInstance", "DatabaseSessionPool")]


def _base_rel_fixture():
    """在 `_base_fixture()` 之上加 3 个端点类型与 12 条边（5 条关系型，两侧相同）。"""
    kg, onto = _base_fixture()
    tbl = [("public", "tbl_%02d" % i) for i in range(40)]
    host = [("host-%02d" % i,) for i in range(30)]
    sess = [("sess-%03d" % i,) for i in range(40)]
    q = [("q-%02d" % i,) for i in range(4)]
    pool = [("pool-%02d" % i,) for i in range(2)]
    app = [("app-%02d" % i,) for i in range(2)]
    for side in (kg["types"], onto["types"]):
        for tc, tup in (("Query", q), ("DatabaseSessionPool", pool), ("AppInstance", app)):
            side[tc] = {"label": "CC_" + tc, "class": CC + tc, "keys": ["k"],
                        "nodeCount": len(tup), "subjectCount": len(tup),
                        "identities": set(tup), "identityCounts": Counter(tup),
                        "multiValuedSubjects": 0}
    edges = {"HAS_SESSION": [(pool[0], sess[i]) for i in range(4)],
             "RUNS_QUERY": [(sess[0], q[i]) for i in range(3)],
             "HOSTS": [(host[0], pool[0]), (host[1], pool[1])],
             "ACCESSES": [(q[0], tbl[0]), (q[1], tbl[1])],
             "USES_DATASOURCE": [(app[0], pool[0])]}
    return kg, onto, edges


def _rel_dict(code, edges, unresolved=0, unmatched=0, samples=(), reasons=None):
    return {"code": code, "identities": set(edges), "edgeTyped": len(edges),
            "edgeTotal": len(edges) + unmatched, "edgeUnmatchedType": unmatched,
            "unmatchedTypeSamples": list(samples) if unmatched else [],
            "unresolvedEdges": unresolved,
            "unresolvedSamples": list(samples) if unresolved else [],
            "unresolvedReasons": dict(reasons or {}),
            "relTypePresent": True, "bareNameEdges": 0}


def _install_edges(side, edges):
    side["relations"] = {code: _rel_dict(code, lst) for code, lst in edges.items()}
    return side


def _cfg():
    return {"meta_label": "CC_Meta", "meta_snapshot_field": "snapshotId",
            "repo": "cc-ops", "gdb_url": "http://127.0.0.1:7200",  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
            "onto_graph_explicit": None, "min_abox_subjects": MIN_ABOX_SUBJECTS,
            "min_abox_relations": MIN_ABOX_RELATIONS,
            "relations": [], "relations_enabled": False, "excluded_relations": [],
            "included_derived_relations": [], "include_derived_relations": False,
            "rel_map": None, "topology": None,
            "strict": False, "excluded_abstract": []}


def _assert(name, ok, detail, quiet=False):
    print("[%s] %-38s %s" % (" OK " if ok else "FAIL", name, detail))
    return bool(ok)


def _capture_main(argv):
    """跑 `main(argv)` 并捕获其 stdout ⇒ (exit_code, 文本)。仅自证用；参数非法时**不联网**。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


def selftest(quiet=False):
    good = 0
    total = 0

    def run(name, kg, onto, cfg=None, strict=False, specs=None, contract_issues=None):
        c = _cfg()
        if cfg:
            c.update(cfg)
        if "excluded_abstract" not in (cfg or {}):
            c["excluded_abstract"] = []
        bl, er, wa, st = evaluate(kg, onto, specs or SELFTEST_SPECS, c,
                                  strict=strict, contract_issues=contract_issues)
        return bl, er, wa, st

    # ① 两侧完全相同的合成集合 ⇒ PASS
    kg, onto = _base_fixture()
    bl, er, wa, st = run("identical", kg, onto)
    total += 1
    good += 1 if _assert("1-identical-sets ⇒ PASS", not bl and not er and st["totalSubjects"] == 110,
                         "blocked=%d errors=%d warnings=%d totalSubjects=%d compared=%d"
                         % (len(bl), len(er), len(wa), st["totalSubjects"],
                            st["comparedTypeCount"]), quiet) else 0

    # ② 只把一条身份元组改掉 ⇒ FAIL，且 onlyKg=1 / onlyOnto=1 精确报出
    kg, onto = _base_fixture()
    onto["types"]["Table"]["identities"] = set(
        kg["types"]["Table"]["identities"] - {("public", "tbl_00")}) | {("public", "tbl_99")}
    onto["types"]["Table"]["identityCounts"] = Counter(onto["types"]["Table"]["identities"])
    bl, er, wa, st = run("one-tuple-changed", kg, onto)
    row = next(r for r in st["types"] if r["typeCode"] == "Table")
    total += 1
    good += 1 if _assert("2-one-identity-tuple-changed ⇒ FAIL(1/1)",
                         (not bl) and er and row["onlyKgCount"] == 1 and row["onlyOntoCount"] == 1
                         and st["diffTypes"] == ["Table"],
                         "onlyKg=%d onlyOnto=%d diffTypes=%s sample=%s"
                         % (row["onlyKgCount"], row["onlyOntoCount"], st["diffTypes"],
                            row["onlyKgSample"]), quiet) else 0

    # ③ 一侧出现重复身份元组 ⇒ G22-03 ERROR（且身份**集合**仍相等 ⇒ 只有 G22-03 抓得住）
    kg, onto = _base_fixture()
    dup = ("public", "tbl_07")
    kg["types"]["Table"]["nodeCount"] = 41
    kg["types"]["Table"]["subjectCount"] = 41
    kg["types"]["Table"]["identityCounts"] = Counter(
        list(kg["types"]["Table"]["identities"]) + [dup])
    onto["types"]["Table"]["nodeCount"] = 41
    onto["types"]["Table"]["subjectCount"] = 41
    onto["types"]["Table"]["identityCounts"] = Counter(
        list(onto["types"]["Table"]["identities"]) + [dup])
    bl, er, wa, st = run("duplicate-key-both-sides", kg, onto)
    row = next(r for r in st["types"] if r["typeCode"] == "Table")
    total += 1
    good += 1 if _assert("3-duplicate-identity ⇒ G22-03 ERROR",
                         (not bl) and row["identitySetsEqual"] and row["kgCollapse"]
                         and row["ontoCollapse"]
                         and any("G22-03" in e for e in er)
                         and not any("G22-02" in e for e in er),
                         "setsEqual=%s kgCollapse=%s ontoCollapse=%s errors=%d"
                         % (row["identitySetsEqual"], row["kgCollapse"], row["ontoCollapse"],
                            len(er)), quiet) else 0

    # ④ 主体数低于 --min-abox-subjects ⇒ G22-04 ERROR
    kg, onto = _base_fixture()
    for tc in list(kg["types"]):
        t = {("public", "x"), ("public", "y")}
        for side in (kg["types"], onto["types"]):
            side[tc]["identities"] = set(t)
            side[tc]["identityCounts"] = Counter(t)
            side[tc]["nodeCount"] = 2
            side[tc]["subjectCount"] = 2
    bl, er, wa, st = run("below-floor", kg, onto)
    total += 1
    good += 1 if _assert("4-subjects-below-floor ⇒ G22-04 ERROR",
                         (not bl) and st["totalSubjects"] == 6
                         and any("G22-04" in e and "< --min-abox-subjects 100" in e for e in er),
                         "totalSubjects=%d errors=%d" % (st["totalSubjects"], len(er)),
                         quiet) else 0

    # ⑤ 目标图不存在 ⇒ BLOCKED（而不是 PASS）
    kg, onto = _base_fixture()
    onto["namedGraphs"] = [{"iri": GRAPH + SNAP_C, "tail": SNAP_C}]
    bl, er, wa, st = run("target-graph-missing", kg, onto)
    total += 1
    good += 1 if _assert("5-target-graph-missing ⇒ BLOCKED",
                         bool(bl) and any("G22-01" in b for b in bl) and st["ontoGraphUsed"] is None,
                         "blocked=%s" % bl, quiet) else 0

    # ⑥ 参数非法（--min-abox-subjects 0）⇒ exit 2 PARAM_ERROR（CLI 端到端）
    for raw in ("0", "-5", "abc"):
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            rc = main(["--min-abox-subjects", raw])
        finally:
            sys.stdout = real
        txt = buf.getvalue()
        total += 1
        good += 1 if _assert("6-param-invalid(%s) ⇒ exit 2 PARAM_ERROR" % raw,
                             rc == 2 and "PARAM_ERROR" in txt and "RESULT: PASS" not in txt,
                             "exit=%d 含PARAM_ERROR=%s" % (rc, "PARAM_ERROR" in txt),
                             quiet) else 0

    # ⑦ 键全空 ⇒ 塌缩成 <NO_KEY>（两侧都塌缩 ⇒ 集合相等、只有 G22-03 抓得住）
    kg, onto = _base_fixture()
    for side in (kg["types"], onto["types"]):
        t = {(NO_KEY,)}
        side["Table"]["identities"] = set(t)
        side["Table"]["identityCounts"] = Counter({(NO_KEY,): 40})
        side["Table"]["nodeCount"] = 40
        side["Table"]["subjectCount"] = 40
    bl, er, wa, st = run("no-key-collapse", kg, onto)
    row = next(r for r in st["types"] if r["typeCode"] == "Table")
    total += 1
    good += 1 if _assert("7-all-keys-null ⇒ <NO_KEY> collapse ⇒ G22-03",
                         (not bl) and row["kgCollapse"] and row["ontoCollapse"]
                         and any("G22-03" in e for e in er),
                         "kgDistinct=%d ontoDistinct=%d" % (row["kgDistinct"], row["ontoDistinct"]),
                         quiet) else 0

    # ⑧ 真契约：13 个具体类型被比较、ConnectionPool 按 abstract 标志排除
    try:
        m, onto_base, prefixes = load_type_map(
            os.path.join(ROOT, "shared", "mapping", "type-map.yaml"))
        specs, excluded = build_specs(m, prefixes, onto_base)
        exc_tc = [e["typeCode"] for e in excluded]
        total += 1
        good += 1 if _assert("8-contract: 13 concrete / abstract excluded by flag",
                             len(specs) == 13 and exc_tc == ["ConnectionPool"]
                             and all(not s["keyAttrsMissing"] for s in specs),
                             "specs=%d excluded=%s" % (len(specs), exc_tc), quiet) else 0
        total += 1
        good += 1 if _assert("8b-contract: Table keys = (schemaName, tableName)",
                             next(s for s in specs if s["typeCode"] == "Table")["keyAttrs"]
                             == ["schemaName", "tableName"],
                             "Table.key_attrs=%s"
                             % next(s for s in specs if s["typeCode"] == "Table")["keyAttrs"],
                             quiet) else 0
    except Exception as exc:  # pragma: no cover
        total += 1
        good += 1 if _assert("8-contract: load type-map", False, "异常：%s" % exc, quiet) else 0

    # ⑨ 契约不自洽（键名不在 attrs 里）⇒ G22-06 ERROR
    bad_spec = _spec("Bad", ["notInAttrs"])
    bad_spec["keyAttrsMissing"] = ["notInAttrs"]
    bad_spec["keyAttrsUsed"] = []
    bad_spec["kgKeys"] = ["ccfId"]
    bad_spec["ontoKeys"] = [CC + "ccfId"]
    kg, onto = _base_fixture()
    bl, er, wa, st = run("contract-inconsistent", kg, onto, specs=SELFTEST_SPECS + [bad_spec])
    total += 1
    good += 1 if _assert("9-contract-inconsistent ⇒ G22-06 ERROR",
                         any("G22-06" in e for e in er), "errors=%d" % len(er), quiet) else 0

    # ⑩ --strict 把 W0（显式目标图与 KG 快照不一致）升 ERROR
    #     （显式指定的图必须**真的存在**在命名图清单里，否则走的是 G22-01 的 BLOCKED 分支）
    kg, onto = _base_fixture()
    onto["namedGraphs"] = onto["namedGraphs"] + [{"iri": GRAPH + SNAP_C, "tail": SNAP_C}]
    bl, er, wa, st = run("explicit-mismatch", kg, onto, cfg={"onto_graph_explicit": GRAPH + SNAP_C})
    total += 1
    good += 1 if _assert("10-explicit-graph-mismatch ⇒ WARN(非 strict 无 ERROR)",
                         (not bl) and not er and any("W0" in w for w in wa)
                         and st["ontoGraphMatchesKgSnapshot"] is False
                         and st["ontoGraphUsed"] == GRAPH + SNAP_C,
                         "errors=%d warnings=%d" % (len(er), len(wa)), quiet) else 0
    kg, onto = _base_fixture()
    onto["namedGraphs"] = onto["namedGraphs"] + [{"iri": GRAPH + SNAP_C, "tail": SNAP_C}]
    bl, er, wa, st = run("explicit-mismatch-strict", kg, onto,
                         cfg={"onto_graph_explicit": GRAPH + SNAP_C}, strict=True)
    total += 1
    good += 1 if _assert("10b-same-input --strict ⇒ ERROR",
                         any("W0" in e for e in er), "errors=%d" % len(er), quiet) else 0

    # ⑪ W2：一侧键属性非单值（去重键数 > 主体数）⇒ WARN；--strict 升 ERROR
    kg, onto = _base_fixture()
    onto["types"]["Host"]["identityCounts"] = Counter(
        list(onto["types"]["Host"]["identities"]) + [("host-00",)])
    onto["types"]["Host"]["identities"] = set(onto["types"]["Host"]["identityCounts"])
    onto["types"]["Host"]["multiValuedSubjects"] = 1
    bl, er, wa, st = run("fan-out-key", kg, onto)
    total += 1
    good += 1 if _assert("11-fan-out-key ⇒ W2 WARN（默认不改退出码）",
                         (not bl) and any("W2" in w for w in wa)
                         and st["fanTypes"] == ["Host"],
                         "warnings=%d fanTypes=%s" % (len(wa), st["fanTypes"]), quiet) else 0
    bl, er, wa, st = run("fan-out-key-strict", kg, onto, strict=True)
    total += 1
    good += 1 if _assert("11b-fan-out-key --strict ⇒ ERROR",
                         any("W2" in e for e in er), "errors=%d" % len(er), quiet) else 0

    # ⑫ 样例截断：单值最多 80 字符
    kg, onto = _base_fixture()
    long_key = ("x" * 200, "tbl_00")
    onto["types"]["Table"]["identities"] = (
        set(onto["types"]["Table"]["identities"]) - {("public", "tbl_00")}) | {long_key}
    bl, er, wa, st = run("sample-truncation", kg, onto)
    row = next(r for r in st["types"] if r["typeCode"] == "Table")
    samples = row["onlyOntoSample"] + row["onlyKgSample"]
    total += 1
    good += 1 if _assert("12-sample-truncated<=80 chars/<=3 items",
                         bool(samples) and all(len(s) <= 400 for s in samples)
                         and max((len(part) for s in samples
                                  for part in s.strip("()").split(", ")), default=0) <= VALUE_MAX,
                         "maxPart=%d" % max((len(part) for s in samples
                                             for part in s.strip("()").split(", ")), default=0),
                         quiet) else 0

    # ⑬ 空/零主体的两侧 ⇒ G22-00 BLOCKED（不是 PASS）
    kg, onto = _base_fixture()
    for side in (kg["types"], onto["types"]):
        for tc in side:
            side[tc]["identities"] = set()
            side[tc]["identityCounts"] = Counter()
            side[tc]["nodeCount"] = 0
            side[tc]["subjectCount"] = 0
    bl, er, wa, st = run("both-sides-empty", kg, onto)
    total += 1
    good += 1 if _assert("13-both-sides-empty ⇒ BLOCKED",
                         bool(bl) and any("G22-00" in b for b in bl),
                         "blocked=%s" % bl, quiet) else 0

    # ===================== M2.19：关系维度（G22-07 / G22-08 / W4 / W5）=====
    rcfg = {"relations": SELFTEST_REL_SPECS, "relations_enabled": True,
            "min_abox_relations": 12}

    # ⑭ 两侧边身份集合完全相同 ⇒ G22-07 全 SAME（关系维度 PASS）
    kg, onto, edges = _base_rel_fixture()
    _install_edges(kg, edges)
    _install_edges(onto, edges)
    bl, er, wa, st = run("rel-identical", kg, onto, cfg=rcfg)
    total += 1
    good += 1 if _assert("14-relation-identity-identical ⇒ OK",
                         (not bl) and not er and st["diffRelations"] == []
                         and st["totalRelations"] == 12 and st["comparedRelationCount"] == 5
                         and st["identitySummary"]["relation"]["verdict"] == "OK",
                         "errors=%d warnings=%d totalRelations=%s comparedRelations=%s"
                         % (len(er), len(wa), st["totalRelations"],
                            st["comparedRelationCount"]), quiet) else 0

    # ⑮ 只改一条边的**端点身份键**（ONTO 侧 ACCESSES）⇒ G22-07 ERROR 且 onlyKg=1/onlyOnto=1
    kg, onto, edges = _base_rel_fixture()
    _install_edges(kg, edges)
    _install_edges(onto, edges)
    src, dst = edges["ACCESSES"][0]
    onto["relations"]["ACCESSES"]["identities"] = (
        set(edges["ACCESSES"]) - {(src, dst)}) | {(src, ("public", "tbl_39"))}
    bl, er, wa, st = run("rel-one-endpoint-key-changed", kg, onto, cfg=rcfg)
    rrow = next(r for r in st["relations"] if r["code"] == "ACCESSES")
    total += 1
    good += 1 if _assert("15-one-endpoint-key-changed ⇒ G22-07①/①",
                         (not bl) and st["diffRelations"] == ["ACCESSES"]
                         and rrow["onlyKgCount"] == 1 and rrow["onlyOntoCount"] == 1
                         and any("G22-07" in e for e in er)
                         and st["diffTypes"] == []
                         and st["identitySummary"]["object"]["verdict"] == "OK",
                         "diffRelations=%s onlyKg=%d onlyOnto=%d ontoSample=%s"
                         % (st["diffRelations"], rrow["onlyKgCount"], rrow["onlyOntoCount"],
                            rrow["onlyOntoSample"]), quiet) else 0

    # ⑯ 某关系型两侧皆空 ⇒ W5 告警（"空集相等"不算证据），--strict ⇒ ERROR
    kg, onto, edges = _base_rel_fixture()
    empty_edges = dict(edges)
    empty_edges["RUNS_QUERY"] = []
    _install_edges(kg, empty_edges)
    _install_edges(onto, empty_edges)
    rcfg9 = dict(rcfg, min_abox_relations=9)
    bl, er, wa, st = run("rel-empty-both-sides", kg, onto, cfg=rcfg9,)
    _, er_s, _, _ = run("rel-empty-both-sides-strict", kg, onto, cfg=rcfg9,
                        strict=True)
    total += 1
    good += 1 if _assert("16-both-sides-empty-rel ⇒ W5/非ERROR",
                         (not bl) and not any("G22-07" in e for e in er)
                         and any("W5" in w for w in wa)
                         and st["emptyRelations"] == ["RUNS_QUERY"]
                         and any("W5" in e for e in er_s)
                         and any("--strict" in e for e in er_s),
                         "emptyRelations=%s warns=%d strictErrors=%d"
                         % (st["emptyRelations"], len(wa), len(er_s)), quiet) else 0

    # ⑰ 端点身份解析不出 ⇒ 记 unresolvedEdges 且**不静默跳过、不用空串代替**（W4；strict ⇒ ERROR）
    kg, onto, edges = _base_rel_fixture()
    _install_edges(kg, edges)
    _install_edges(onto, edges)
    a0 = edges["ACCESSES"][0]
    nokey_edge = ((NO_KEY,), a0[1])
    for side in (kg, onto):
        side["relations"]["ACCESSES"] = _rel_dict(
            "ACCESSES", (set(edges["ACCESSES"]) - {a0}) | {nokey_edge},
            unresolved=1,
            samples=["(%s) -ACCESSES-> %s" % (NO_KEY, _fmt_tuple(a0[1]))],
            reasons={"src-key-missing": 1})
    bl, er, wa, st = run("rel-unresolved-endpoint", kg, onto, cfg=rcfg,)
    _, er_s, _, _ = run("rel-unresolved-endpoint-strict", kg, onto, cfg=rcfg,
                        strict=True)
    rrow = next(r for r in st["relations"] if r["code"] == "ACCESSES")
    total += 1
    good += 1 if _assert("17-unresolved-endpoint ⇒ W4/不静默跳过",
                         (not bl) and st["diffRelations"] == []
                         and st["unresolvedEdges"] == 2
                         and "ACCESSES" in st["unresolvedEdgeByRelation"]
                         and any("W4" in w for w in wa)
                         and rrow["kgDistinct"] == 2 and rrow["onlyOntoCount"] == 0
                         and any(NO_KEY in s for s in rrow["unresolvedSamples"])
                         and all(s and s.strip() != "()" for s in rrow["unresolvedSamples"])
                         and any("W4" in e for e in er_s),
                         "unresolvedEdges=%d byRel=%s kgDistinct=%s warns=%d strictErrors=%d"
                         % (st["unresolvedEdges"], st["unresolvedEdgeByRelation"],
                            rrow["kgDistinct"], len(wa), len(er_s)), quiet) else 0

    # ⑱ G22-08 反恒真下限：边数低于 --min-abox-relations ⇒ ERROR
    kg, onto, edges = _base_rel_fixture()
    _install_edges(kg, edges)
    _install_edges(onto, edges)
    bl, er, wa, st = run("rel-floor", kg, onto, cfg=dict(rcfg, min_abox_relations=13),)
    total += 1
    good += 1 if _assert("18-relations-below-floor ⇒ G22-08 ERROR",
                         (not bl) and any("G22-08" in e for e in er)
                         and st["totalRelations"] == 12 and st["minAboxRelations"] == 13,
                         "totalRelations=%s floor=%s errors=%s"
                         % (st["totalRelations"], st["minAboxRelations"],
                            [e.split("：")[0] for e in er]), quiet) else 0

    # ⑲ 非法 --min-abox-relations ⇒ exit 2 PARAM_ERROR（与 G22-04 同风格）
    rc, out = _capture_main(["--min-abox-relations", "0"])
    rc2, out2 = _capture_main(["--min-abox-relations", "-5"])
    rc3, out3 = _capture_main(["--min-abox-relations", "abc"])
    total += 1
    good += 1 if _assert("19-min-abox-relations-invalid ⇒ PARAM_ERROR(2)",
                         rc == 2 and rc2 == 2 and rc3 == 2
                         and all("PARAM_ERROR" in o for o in (out, out2, out3)),
                         "exit=%s/%s/%s" % (rc, rc2, rc3), quiet) else 0

    # ⑳ 派生关系 opt-in（--include-derived-relations）：`from: "*"` 按非抽象源端类型扇出
    rel_map_fx = {
        "relations": {
            "ACCESSES": {"from": "Query", "to": "Table", "kg_reltype": "CC_ACCESSES",
                         "onto_property": "cc:accesses", "asserted": True},
            "HAS_EVIDENCE": {"from": "*", "to": "Evidence", "kg_reltype": "CC_HAS_EVIDENCE",
                             "onto_property": "cc:hasEvidence", "asserted": False,
                             "derived_from": "observations"},
        },
        "inverse_relations": {},
    }
    topo_fx = {"asserted": ["ACCESSES"], "excluded": ["HAS_EVIDENCE"], "inverse": [], "facts": {}}
    fx_specs = [_spec("Query", ["queryId"]), _spec("Table", ["schemaName", "tableName"]),
                _spec("Evidence", ["recordId"])]
    rs_off, ex_off, inc_off, is_off = build_relations(
        rel_map_fx, topo_fx, {"cc": CC}, "https://credit-card-ops.ai/onto/", fx_specs)
    rs_on, ex_on, inc_on, is_on = build_relations(
        rel_map_fx, topo_fx, {"cc": CC}, "https://credit-card-ops.ai/onto/", fx_specs,
        include_derived=True)
    on_codes = sorted(r["code"] for r in rs_on)
    total += 1
    good += 1 if _assert(
        "20-derived-opt-in ⇒ wildcard fan-out",
        [r["code"] for r in rs_off] == ["ACCESSES"]
        and on_codes == ["ACCESSES", "HAS_EVIDENCE@Query", "HAS_EVIDENCE@Table"]
        and [e["code"] for e in ex_off] == ["HAS_EVIDENCE"] and not ex_on
        and len(inc_on) == 1 and inc_on[0]["fanOutCount"] == 2
        and not is_off and not is_on,
        "off=%s on=%s excluded(off)=%s excluded(on)=%s fanOut=%s issues=%s"
        % ([r["code"] for r in rs_off], on_codes, [e["code"] for e in ex_off],
           [e["code"] for e in ex_on], inc_on[0]["fanOutCount"] if inc_on else None,
           is_off + is_on), quiet) else 0

    # ㉑ 派生的 Table 侧边身份变化：默认口径看不见（ACCESSES 两侧皆空），opt-in 后 G22-07 报出
    kg3, onto3 = _base_fixture()
    _install_edges(kg3, {"HAS_EVIDENCE@Table": [(("root", "new_order"), ("obs-000073",))]})
    _install_edges(onto3, {"HAS_EVIDENCE@Table": [(("", "tbl-new_order"), ("obs-000073",))]})
    bl_h, er_h, wa_h, st_h = run("derived-hidden", kg3, onto3, cfg=dict(rcfg, relations=rs_off))
    bl_s, er_s, wa_s, st_s = run("derived-shown", kg3, onto3,
                                 cfg=dict(rcfg, relations=rs_on,
                                          include_derived_relations=True,
                                          included_derived_relations=inc_on))
    total += 1
    good += 1 if _assert(
        "21-derived-fan ⇒ Table 端点键变化可见",
        (not bl_h) and st_h["diffRelations"] == [] and not any("G22-07" in e for e in er_h)
        and (not bl_s) and st_s["diffRelations"] == ["HAS_EVIDENCE@Table"]
        and any("G22-07" in e and "HAS_EVIDENCE@Table" in e for e in er_s)
        and any("root, new_order" in x for x in
                [r for r in st_s["relations"] if r["code"] == "HAS_EVIDENCE@Table"][0]["onlyKgSample"])
        and any("tbl-new_order" in x for x in
                [r for r in st_s["relations"] if r["code"] == "HAS_EVIDENCE@Table"][0]["onlyOntoSample"]),
        "默认口径 diffRelations=%s（G22-07 报错=%s）；opt-in diffRelations=%s 样例 onlyKg=%s onlyOnto=%s"
        % (st_h["diffRelations"], any("G22-07" in e for e in er_h), st_s["diffRelations"],
           [r for r in st_s["relations"] if r["code"] == "HAS_EVIDENCE@Table"][0]["onlyKgSample"],
           [r for r in st_s["relations"] if r["code"] == "HAS_EVIDENCE@Table"][0]["onlyOntoSample"]),
        quiet) else 0

    print("-" * 72)
    print("判别力自证（G22 --selftest）：%d/%d 断言成立" % (good, total))
    if good != total:
        print("RESULT: FAIL（G22 --selftest：%d/%d）" % (good, total))
        return 1
    print("RESULT: PASS（G22 --selftest：%d/%d）" % (good, total))
    return 0


# ---------------------------------------------------------------------------
# 报告 / CLI
# ---------------------------------------------------------------------------
def verdict_of(blocked, errs):
    if blocked:
        return "BLOCKED"
    return "FAIL" if errs else "PASS"


def load_cfg(args):
    cfg = dict(DEFAULTS)
    tg = os.path.join(ROOT, "shared", "deploy", "targets.yaml")
    if os.path.isfile(tg):
        try:
            t = read_yaml(tg)
            cfg["bolt_uri"] = t["neo4j"]["bolt_uri"]
            cfg["user"] = t["neo4j"]["auth"]["user"]
            cfg["password_env"] = t["neo4j"]["auth"]["password_env"]
            cfg["gdb_url"] = t["graphdb"]["url"]
            cfg["repo"] = t["graphdb"]["isolation"]["repository"]
        except Exception as exc:
            print("[WARN] 读 %s 失败，退回默认载体地址：%s" % (tg, exc))
    cfg["type_map"] = args.type_map or os.path.join(ROOT, "shared", "mapping", "type-map.yaml")
    if args.graphdb:
        cfg["gdb_url"] = args.graphdb
    if args.repo:
        cfg["repo"] = args.repo
    if args.neo4j_uri:
        cfg["bolt_uri"] = args.neo4j_uri
    if args.user:
        cfg["user"] = args.user
    if args.database:
        cfg["database"] = args.database
    cfg["onto_graph_explicit"] = args.onto_graph or None
    cfg["min_abox_subjects"] = (args.min_abox_subjects
                                if args.min_abox_subjects is not None else MIN_ABOX_SUBJECTS)
    cfg["min_abox_relations"] = (args.min_abox_relations
                                 if args.min_abox_relations is not None else MIN_ABOX_RELATIONS)
    cfg["rel_map"] = args.rel_map or os.path.join(ROOT, "shared", "mapping", "rel-map.yaml")
    cfg["topology"] = args.topology or os.path.join(ROOT, "shared", "model", "topology.yaml")
    cfg["relations"] = []
    cfg["excluded_relations"] = []
    cfg["included_derived_relations"] = []
    cfg["include_derived_relations"] = bool(getattr(args, "include_derived_relations", False))
    cfg["relations_enabled"] = True
    cfg["strict"] = bool(args.strict)
    return cfg


def validate_params(cfg):
    """参数校验：违规 ⇒ PARAM_ERROR（exit 2，绝不产生 PASS 输出）。"""
    errs = []
    raw = cfg.get("min_abox_subjects")
    try:
        iv = int(str(raw).strip())
    except (TypeError, ValueError):
        errs.append("参数错误：--min-abox-subjects 必须是整数，实得 %r" % (raw,))
    else:
        if iv <= 0:
            errs.append("参数错误：--min-abox-subjects 必须 > 0，实得 %d"
                        " ⇒ 禁止用 0/负数关闭反恒真下限（0 会退化成「检查 0 条也算通过」）" % iv)
        else:
            cfg["min_abox_subjects"] = iv
    if cfg.get("onto_graph_explicit") is not None and not str(cfg["onto_graph_explicit"]).strip():
        errs.append("参数错误：--onto-graph 不能是空串（不给该参数表示自动选图）")
    raw_r = cfg.get("min_abox_relations")
    try:
        ivr = int(str(raw_r).strip())
    except (TypeError, ValueError):
        errs.append("参数错误：--min-abox-relations 必须是整数，实得 %r" % (raw_r,))
    else:
        if ivr <= 0:
            errs.append("参数错误：--min-abox-relations 必须 > 0，实得 %d"
                        " ⇒ 禁止用 0/负数关闭关系维度反恒真下限" % ivr)
        else:
            cfg["min_abox_relations"] = ivr
    return errs


def render(stats, errs, warns, blocked, cfg, specs):
    L = []
    L.append("=== G22 跨载体 ABox 身份集等价门禁 ===")
    L.append("KG  = %s（database=%s，`:%s`）"
             % (cfg["bolt_uri"], cfg["database"], cfg.get("meta_label")))
    L.append("ONTO= %s 仓库 %s" % (cfg["gdb_url"], cfg["repo"]))
    L.append("type-map = %s" % cfg["type_map"])
    L.append("口径 = 逐非抽象 typeCode 比较 `key_attrs` 身份元组集合；样例 <=%d 条、单值 <=%d 字符%s"
             % (SAMPLE_LIMIT, VALUE_MAX, "；--strict" if cfg.get("strict") else ""))
    isum = stats.get("identitySummary") or {}
    if isum:
        _o, _r = isum.get("object") or {}, isum.get("relation") or {}
        L.append("身份集小结：对象 = %s（%d 类参与比较，主体 kg %s vs onto %s）；"
                 "关系 = %s（%d 条关系型参与比较，边 kg %s vs onto %s）"
                 % (_o.get("verdict"), _o.get("comparedTypeCount") or 0,
                    _o.get("kgTotalSubjects"), _o.get("ontoTotalSubjects"),
                    _r.get("verdict"), _r.get("comparedRelationCount") or 0,
                    _r.get("kgTotalRelations"), _r.get("ontoTotalRelations")))
    if cfg.get("include_derived_relations"):
        _d = stats.get("includedDerivedRelations") or []
        L.append("★ 派生关系已纳入比较面（--include-derived-relations，**非默认契约口径**）：%s"
                 % (",".join("%s（%s→%s）扇出 %d 条" % (x.get("code"), x.get("from"),
                                                       x.get("to"), x.get("fanOutCount") or 0)
                             for x in _d) or "—"))
    for c in stats.get("criteria", []):
        L.append("-- %s %s [%s]" % (c["id"], c["title"], c["status"]))
        for d in c["detail"]:
            L.append("   %s" % d)
    if warns:
        L.append("-- 告警（WARN，默认不改变退出码；--strict 升 ERROR）--")
        for w in warns:
            L.append("  [WARN] %s" % w)
    if errs:
        L.append("-- 错误 --")
        for e in errs:
            L.append("  [ERROR] %s" % e)
    if blocked:
        for b in blocked:
            L.append("  [BLOCKED] %s" % b)
    return L


def build_report(cfg, stats, errs, warns, blocked, verdict, exit_code, captured_at=None):
    st = stats or {}
    return {
        "gate": "G22",
        "title": "跨载体 ABox 身份集等价门禁",
        "verdict": verdict, "exitCode": int(exit_code),
        "capturedAt": captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "typeMap": cfg.get("type_map"),
        "boltUri": cfg.get("bolt_uri"), "database": cfg.get("database"),
        "gdbUrl": cfg.get("gdb_url"), "repo": cfg.get("repo"),
        # --- 契约身份口径 ---
        "kgSnapshotId": st.get("kgSnapshotId"),
        "kgSnapshotIdValid": st.get("kgSnapshotIdValid"),
        "ontoGraphUsed": st.get("ontoGraphUsed"),
        "ontoGraphSource": st.get("ontoGraphSource"),
        "ontoGraphMatchesKgSnapshot": st.get("ontoGraphMatchesKgSnapshot"),
        "latestOntoSnapshotGraph": st.get("latestOntoSnapshotGraph"),
        "latestOntoSnapshotTail": st.get("latestOntoSnapshotTail"),
        "ontoSnapshotTails": st.get("ontoSnapshotTails") or [],
        "ontoNamedGraphs": st.get("ontoNamedGraphs") or [],
        # --- 比较面 ---
        "comparedTypes": st.get("comparedTypes") or [],
        "comparedTypeCount": st.get("comparedTypeCount"),
        "excludedAbstract": st.get("excludedAbstract") or [],
        "totalSubjects": st.get("totalSubjects"),
        "kgTotalSubjects": st.get("kgTotalSubjects"),
        "ontoTotalSubjects": st.get("ontoTotalSubjects"),
        "minAboxSubjects": st.get("minAboxSubjects"),
        "minComparedTypes": st.get("minComparedTypes"),
        "diffTypes": st.get("diffTypes") or [],
        "collapseTypes": st.get("collapseTypes") or [],
        "fanTypes": st.get("fanTypes") or [],
        "contractInconsistentTypes": st.get("contractInconsistentTypes") or [],
        # --- 关系维度（M2.19：G22-07 / G22-08 / W4 / W5）---
        "relMap": st.get("relMap"), "topology": st.get("topology"),
        "relationsEnabled": st.get("relationsEnabled"),
        "comparedRelations": st.get("comparedRelations") or [],
        "comparedRelationCount": st.get("comparedRelationCount"),
        "minAboxRelations": st.get("minAboxRelations"),
        "minComparedRelations": st.get("minComparedRelations"),
        "kgTotalRelations": st.get("kgTotalRelations"),
        "ontoTotalRelations": st.get("ontoTotalRelations"),
        "totalRelations": st.get("totalRelations"),
        "diffRelations": st.get("diffRelations") or [],
        "emptyRelations": st.get("emptyRelations") or [],
        "unresolvedEdges": st.get("unresolvedEdges"),
        "unresolvedEdgeByRelation": st.get("unresolvedEdgeByRelation") or {},
        "excludedRelations": st.get("excludedRelations") or [],
        "includeDerivedRelations": st.get("includeDerivedRelations"),
        "includedDerivedRelations": st.get("includedDerivedRelations") or [],
        "identitySummary": st.get("identitySummary") or {},
        "relations": st.get("relations") or [],
        # --- 逐类型明细 ---
        "types": st.get("types") or [],
        "criteria": st.get("criteria") or [],
        "errors": list(errs or []), "warnings": list(warns or []),
        "blocked": list(blocked or []),
        "strict": bool(cfg.get("strict")),
        "notes": list(HONESTY_NOTES),
    }


def write_report_json(path, report):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(report, ensure_ascii=False, indent=2))
        fh.write("\n")
    return path


def _emit(args, report, lines, code):
    if args.out:
        write_report_json(args.out, report)
    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write("\n".join(lines) + "\n")
        if args.out:
            sys.stdout.write("JSON 报告：%s\n" % args.out)
    return code


def _blocked_exit(args, cfg, stats, errs, warns, blocked, lines):
    report = build_report(cfg, stats, errs, warns, blocked, "BLOCKED", 2)
    lines = list(lines) + render(stats, errs, warns, blocked, cfg, [])
    lines.append("RESULT: BLOCKED (ERROR=%d WARN=%d)" % (len(errs) + len(blocked), len(warns)))
    return _emit(args, report, lines, 2)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="G22 跨载体 ABox 身份集等价门禁（只读；0 PASS / 1 FAIL / 2 BLOCKED·PARAM_ERROR）")
    ap.add_argument("--type-map", default=None,
                    help="type-map.yaml 路径（默认 shared/mapping/type-map.yaml）")
    ap.add_argument("--graphdb", default=None, help="GraphDB 基址（默认取 targets.yaml）")
    ap.add_argument("--repo", default=None, help="GraphDB 仓库名（默认取 targets.yaml）")
    ap.add_argument("--onto-graph", default=None, metavar="IRI",
                    help="显式指定 ONTO 命名图 IRI（也接受裸快照 id 末段）；"
                         "不给则自动选「末段 == :CC_Meta.snapshotId」的图，找不到 ⇒ BLOCKED")
    ap.add_argument("--min-abox-subjects", default=None, metavar="N",
                    help="反恒真下限：两侧被比较类型主体合计（取较小侧）必须 >= N"
                         "（默认 %d；非整数 / <=0 ⇒ PARAM_ERROR exit 2）" % MIN_ABOX_SUBJECTS)
    ap.add_argument("--min-abox-relations", default=None, metavar="N",
                    help="关系维度反恒真下限：两侧被比较关系边合计（取较小侧）必须 >= N"
                         "（默认 %d；非整数 / <=0 ⇒ PARAM_ERROR exit 2）" % MIN_ABOX_RELATIONS)
    ap.add_argument("--rel-map", default=None,
                    help="rel-map.yaml 路径（关系名映射权威表；默认 shared/mapping/rel-map.yaml）")
    ap.add_argument("--topology", default=None,
                    help="topology.yaml 路径（关系契约按 asserted 标志；默认 shared/model/topology.yaml）")
    ap.add_argument("--include-derived-relations", action="store_true",
                    help="把契约中 `derived_from` 标出的派生关系（真机只有 HAS_EVIDENCE，"
                         "Table/Session/... → Evidence，191 条）**追加**进 G22-07 比较面；"
                         "`from: \"*\"` 按非抽象源端类型扇出（code=<CODE>@<源类型>）。"
                         "默认**不含**（契约口径 = asserted: true 的 14 条）；开启后比较面更大、"
                         "更能看见以 Table 为端点的边身份变化")
    ap.add_argument("--neo4j-uri", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--database", default=None)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结论（结构化结果到 stdout）")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--strict", action="store_true", help="把 WARN 升为 ERROR")
    ap.add_argument("--selftest", action="store_true", help="无网络判别力自证（注入式夹具）")
    ap.add_argument("--quiet", action="store_true", help="--selftest 时保留全部断言行即可")
    args = ap.parse_args(argv)
    cfg = load_cfg(args)

    if args.selftest:
        return selftest(quiet=args.quiet)

    # ★ 参数校验在**任何采集之前**：非整数 / 0 / 负数 ⇒ PARAM_ERROR（exit 2），不是 PASS。
    perr = validate_params(cfg)
    if perr:
        rep = build_report(cfg, {}, perr, [], [], "PARAM_ERROR", 2)
        if args.out:
            write_report_json(args.out, rep)
        if args.json:
            sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n")
        else:
            for e in perr:
                print("[ERROR] %s" % e)
            print("RESULT: PARAM_ERROR (ERROR=%d WARN=0)" % len(perr))
        return 2

    lines = []
    lines.append("=== G22 跨载体 ABox 身份集等价门禁 ===")
    lines.append("KG  = %s（database=%s，`:%s`）"
                 % (cfg["bolt_uri"], cfg["database"], cfg["meta_label"]))
    lines.append("ONTO= %s 仓库 %s" % (cfg["gdb_url"], cfg["repo"]))
    lines.append("type-map = %s" % cfg["type_map"])

    try:
        m, onto_base, prefixes = load_type_map(cfg["type_map"])
        specs, excluded = build_specs(m, prefixes, onto_base)
        cfg["excluded_abstract"] = excluded
    except Exception as exc:
        return _blocked_exit(args, cfg, {}, ["G22 契约不可用：%s" % exc], [],
                            ["BLOCKED type-map 不可用：%s" % exc], lines)

    # 关系契约（M2.19）：rel-map 是名称映射权威表，topology 提供 asserted 标志；
    # 契约加载失败 ⇒ 问题并入 contract_issues 且 relspecs 为空 ⇒ G22-08 因"实比关系型 < 5"报 ERROR（fail-closed）。
    contract_issues = []
    relspecs, excluded_rels, included_rels = [], [], []
    try:
        rel_map = load_relation_map(cfg["rel_map"])
        topo = topology_relation_facts(cfg["topology"])
        relspecs, excluded_rels, included_rels, rel_issues = build_relations(
            rel_map, topo, prefixes, onto_base, specs,
            include_derived=cfg.get("include_derived_relations"))
        contract_issues.extend(rel_issues)
    except Exception as exc:
        contract_issues.append("关系契约不可用（rel-map=%s / topology=%s）：%s"
                               % (cfg.get("rel_map"), cfg.get("topology"), exc))
    cfg["relations"] = relspecs
    cfg["excluded_relations"] = excluded_rels
    cfg["included_derived_relations"] = included_rels

    try:
        kg = collect_kg(cfg, args.password, specs, relspecs)
    except Exception as exc:
        return _blocked_exit(args, cfg, {}, ["BLOCKED KG 侧不可达：%s" % exc], [],
                            ["BLOCKED KG 侧不可达：%s" % exc], lines)

    try:
        named = list_named_graphs(cfg)
    except Exception as exc:
        return _blocked_exit(args, cfg, {"kgSnapshotId": kg.get("snapshotId")},
                            ["BLOCKED ONTO 侧不可达：%s" % exc], [],
                            ["BLOCKED ONTO 侧不可达：%s" % exc], lines)

    target, source, matches, sel_detail, sel_block = select_target_graph(
        kg.get("snapshotId"), named, cfg["onto_graph_explicit"])
    onto = {"namedGraphs": named, "types": {}, "targetGraph": target}
    if target and not sel_block:
        try:
            onto.update(collect_onto_identities(cfg, specs, target))
            if relspecs:
                onto.update(collect_onto_relations(cfg, relspecs, target))
        except Exception as exc:
            return _blocked_exit(args, cfg, {"kgSnapshotId": kg.get("snapshotId")},
                                ["BLOCKED ONTO 侧目标图 %s 不可读：%s" % (target, exc)], [],
                                ["BLOCKED ONTO 侧目标图不可读：%s" % exc], lines)

    blocked, errs, warns, stats = evaluate(kg, onto, specs, cfg,
                                          contract_issues=contract_issues)
    verdict = verdict_of(blocked, errs)
    code = 2 if blocked else (1 if errs else 0)
    lines = render(stats, errs, warns, blocked, cfg, specs)
    lines.append("RESULT: %s (ERROR=%d WARN=%d)"
                 % ("BLOCKED" if blocked else ("FAIL" if errs else "PASS"),
                    len(errs) + len(blocked), len(warns)))
    report = build_report(cfg, stats, errs, warns, blocked, verdict, code)
    return _emit(args, report, lines, code)


if __name__ == "__main__":
    sys.exit(main())
