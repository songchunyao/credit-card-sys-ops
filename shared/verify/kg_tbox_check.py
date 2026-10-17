#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shared/verify/kg_tbox_check.py — 方案1（知识图谱+LLM）原生 TBox 的**只读静态校验器**

设计约束（任务书）：
  · **纯标准库、零外部依赖**（不 import yaml / neo4j / requests）⇒ 自带 YAML 子集解析器，
    并对契约条数做**自检断言**（14 类型 / 15 关系 / 23 故障模式 / 27 因果边 / 13 阈值 / 91 指标），
    自检不过即 exit 1 —— 绝不允许"解析失败"被当成"没有违规"。
  · **只读**：只打开文件读取；不连数据库、不写任何文件。
  · 退出码：0 = 全部 ERROR 项通过；1 = 有 ERROR（WARN 不影响退出码，但逐条打印）。
  · 违规输出格式：`<相对路径>:<行号>: ERROR|WARN <检查号> <说明>`

检查清单（与 docs/03 + 任务书硬约束一一对应）：
  C01 标签 ∈ 契约具体类型/故障域标签          C10 02-topology 零实例值 + 模板条数
  C02 关系类型 ∈ 契约 reltype ∪ 故障域关系     C11 CC_Metric 集合 == metrics.yaml（58）
  C03 FM id 集合 == fault-modes.yaml          C12 CC_Threshold 集合 == thresholds（12）
  C04 边 id 集合 == causal-edges.yaml         C13 判据(JUDGED_BY) 与 YAML detect 逐子句一致
  C05 规则体零数字字面量（non_zero 豁免）      C14 AFFECTS 覆盖 14 个 FM 且目标类型/severity 一致
  C06 无变长路径 `*1..n`                      C15 规则内属性名在图属性空间内（T5 静态版）
  C07 有向环 ⊆ 契约白名单                     C16 CC_Meta 单例 + snapshotId
  C08 阈值引用可解（档位必填；未标定→WARN）   C17 命名前缀纪律（cc_uq_/cc_idx_/CC_，不碰 gdo_）
  C09 GAP 台账与真实缺失一致                  C18 01-schema 约束/索引计数与命名
"""

import os
import re
import sys

ERRORS = []
WARNS = []
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def rel(p):
    """把绝对路径转成相对仓库根的可读路径（输出用）。"""
    try:
        return os.path.relpath(p, ROOT).replace("\\", "/")
    except ValueError:
        return p


def err(path, line, code, msg):
    ERRORS.append("%s:%s: ERROR %s %s" % (rel(path), line, code, msg))


def warn(path, line, code, msg):
    WARNS.append("%s:%s: WARN %s %s" % (rel(path), line, code, msg))


# ============================================================================
# 1. 迷你 YAML 子集解析器（block mapping/sequence、flow sequence/mapping、引号、
#    行内注释、null/bool/int/float）。**仅为本校验器服务**，不追求 YAML 全覆盖；
#    契约若出现解析器不支持的构造，会由条数自检当场暴露。
# ============================================================================

class YamlError(Exception):
    pass


def _strip_comment(s):
    out = []
    q = None
    i = 0
    while i < len(s):
        c = s[i]
        if q:
            out.append(c)
            if c == "\\" and q == '"':
                i += 1
                if i < len(s):
                    out.append(s[i])
            elif c == q:
                q = None
        else:
            if c in "'\"":
                q = c
                out.append(c)
            elif c == "#" and (i == 0 or s[i - 1] in " \t"):
                break
            else:
                out.append(c)
        i += 1
    return "".join(out).rstrip()


def _scalar(tok):
    t = tok.strip()
    if t == "":
        return None
    low = t.lower()
    if low in ("null", "~", "none"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        body = t[1:-1]
        if t[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        else:
            body = body.replace("''", "'")
        return body
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _flow(s, i):
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return None, i
    c = s[i]
    if c == "[":
        i += 1
        out = []
        while True:
            while i < len(s) and s[i] in " ,":
                i += 1
            if i >= len(s):
                break
            if s[i] == "]":
                i += 1
                break
            v, i = _flow(s, i)
            out.append(v)
        return out, i
    if c == "{":
        i += 1
        out = {}
        while True:
            while i < len(s) and s[i] in " ,":
                i += 1
            if i >= len(s):
                break
            if s[i] == "}":
                i += 1
                break
            j = i
            while j < len(s) and s[j] not in ":}":
                j += 1
            k = _scalar(s[i:j])
            i = j + 1
            while i < len(s) and s[i] in " :":
                i += 1
            v, i = _flow(s, i)
            out[str(k)] = v
        return out, i
    if c in "'\"":
        q = c
        j = i + 1
        buf = []
        while j < len(s) and s[j] != q:
            if s[j] == "\\" and q == '"':
                j += 1
            if j < len(s):
                buf.append(s[j])
            j += 1
        return "".join(buf), j + 1
    j = i
    while j < len(s) and s[j] not in ",]}":
        j += 1
    return _scalar(s[i:j]), j


def _kv(text):
    q = None
    i = 0
    while i < len(text):
        c = text[i]
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == ":":
            return text[:i].strip(), text[i + 1:].strip()
        i += 1
    raise YamlError("no ':' in mapping entry: %r" % text)


class _Ctx:
    def __init__(self, lines):
        self.lines = lines  # [(indent, text, lineno)]
        self.i = 0


def _inline(v):
    v = v.strip()
    if v[:1] in "[{":
        val, _ = _flow(v, 0)
        return val
    return _scalar(v)


def _entry(ctx, d, text, key_indent):
    k, v = _kv(text)
    if v == "":
        if ctx.i < len(ctx.lines) and ctx.lines[ctx.i][0] > key_indent:
            d[k] = _block(ctx, ctx.lines[ctx.i][0])
        else:
            d[k] = None
    else:
        d[k] = _inline(v)


def _is_map_entry(text):
    try:
        k, _ = _kv(text)
    except YamlError:
        return False
    return bool(re.match(r"^[A-Za-z_][\w.\-]*$", k))


def _block(ctx, indent):
    if ctx.i >= len(ctx.lines):
        return None
    text = ctx.lines[ctx.i][1]
    if text == "-" or text.startswith("- "):
        return _seq(ctx, indent)
    return _map(ctx, indent)


def _map(ctx, indent):
    d = {}
    while ctx.i < len(ctx.lines):
        ind, text, _ln = ctx.lines[ctx.i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError("unexpected indent at line %s: %r" % (_ln, text))
        if text == "-" or text.startswith("- "):
            break
        ctx.i += 1
        _entry(ctx, d, text, indent)
    return d


def _seq(ctx, indent):
    out = []
    while ctx.i < len(ctx.lines):
        ind, text, _ln = ctx.lines[ctx.i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError("unexpected indent in seq at line %s: %r" % (_ln, text))
        if not (text == "-" or text.startswith("- ")):
            break
        rest = text[2:].strip() if text.startswith("- ") else ""
        ctx.i += 1
        if rest == "":
            if ctx.i < len(ctx.lines) and ctx.lines[ctx.i][0] > indent:
                out.append(_block(ctx, ctx.lines[ctx.i][0]))
            else:
                out.append(None)
        elif rest[:1] in "[{":
            out.append(_inline(rest))
        elif _is_map_entry(rest):
            item = {}
            base = indent + 2
            _entry(ctx, item, rest, base)
            while ctx.i < len(ctx.lines) and ctx.lines[ctx.i][0] == base and not (
                ctx.lines[ctx.i][1] == "-" or ctx.lines[ctx.i][1].startswith("- ")
            ):
                t = ctx.lines[ctx.i][1]
                ctx.i += 1
                _entry(ctx, item, t, base)
            out.append(item)
        else:
            out.append(_scalar(rest))
    return out


def load_yaml(path):
    lines = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            stripped = _strip_comment(raw.rstrip("\n").rstrip("\r"))
            if not stripped.strip():
                continue
            if stripped.lstrip().startswith("- ") and stripped.strip() == "-":
                pass
            indent = len(stripped) - len(stripped.lstrip(" "))
            text = stripped.strip()
            if text in ("---", "..."):
                continue
            lines.append((indent, text, n))
    ctx = _Ctx(lines)
    return _block(ctx, 0) if lines else {}


# ============================================================================
# 2. 契约载入 + 自检（自检失败 → 直接 exit 1，绝不静默放行）
# ============================================================================

FAILS = []


def need(cond, msg):
    if not cond:
        FAILS.append(msg)


CONTRACTS = {}
try:
    CONTRACTS["topology"] = load_yaml(os.path.join(ROOT, "shared", "model", "topology.yaml"))
    CONTRACTS["fault"] = load_yaml(os.path.join(ROOT, "shared", "model", "fault-modes.yaml"))
    CONTRACTS["causal"] = load_yaml(os.path.join(ROOT, "shared", "model", "causal-edges.yaml"))
    CONTRACTS["metrics"] = load_yaml(os.path.join(ROOT, "shared", "model", "metrics.yaml"))
    CONTRACTS["typemap"] = load_yaml(os.path.join(ROOT, "shared", "mapping", "type-map.yaml"))
    CONTRACTS["relmap"] = load_yaml(os.path.join(ROOT, "shared", "mapping", "rel-map.yaml"))
except YamlError as exc:  # 解析器自身出错：必须响亮失败
    print("CONTRACT-PARSE-ERROR: %s" % exc)
    sys.exit(1)
except OSError as exc:
    print("CONTRACT-READ-ERROR: %s" % exc)
    sys.exit(1)

TOPO = CONTRACTS["topology"]
FCM = CONTRACTS["fault"]
CAU = CONTRACTS["causal"]
MET = CONTRACTS["metrics"]
TMAP = CONTRACTS["typemap"]
RMAP = CONTRACTS["relmap"]

TOPO_TYPES = TOPO.get("types") or []
TOPO_RELS = TOPO.get("relations") or []
TOPO_INV = TOPO.get("inverse_relations") or []
FM_LIST = FCM.get("fault_modes") or []
THR_LIST = FCM.get("thresholds") or []
EDGE_LIST = CAU.get("edges") or []
MET_LIST = MET.get("metrics") or []
TMAP_TYPES = TMAP.get("types") or []
TMAP_METRICS = TMAP.get("metrics") or {}
RMAP_RELS = RMAP.get("relations") or []
RMAP_INV = RMAP.get("inverse_relations") or []

need(len(TOPO_TYPES) == 14, "topology.yaml types != 14 (got %d)" % len(TOPO_TYPES))
need(len(TOPO_RELS) == 15, "topology.yaml relations != 15 (got %d)" % len(TOPO_RELS))
need(len(TOPO_INV) == 4, "topology.yaml inverse_relations != 4 (got %d)" % len(TOPO_INV))
need(len(FM_LIST) == 23, "fault-modes.yaml fault_modes != 23 (got %d)" % len(FM_LIST))
need(len(THR_LIST) == 13, "fault-modes.yaml thresholds != 13 (got %d)" % len(THR_LIST))
need(len(EDGE_LIST) == 27, "causal-edges.yaml edges != 27 (got %d)" % len(EDGE_LIST))
need(len(MET_LIST) == 91, "metrics.yaml metrics != 91 (got %d)" % len(MET_LIST))
need(len(TMAP_TYPES) == 14, "type-map.yaml types != 14 (got %d)" % len(TMAP_TYPES))
need(len(TMAP_METRICS) == 91, "type-map.yaml metrics != 91 (got %d)" % len(TMAP_METRICS))
need(len(RMAP_RELS) == 15, "rel-map.yaml relations != 15 (got %d)" % len(RMAP_RELS))
need(len(RMAP_INV) == 4, "rel-map.yaml inverse_relations != 4 (got %d)" % len(RMAP_INV))
need((CAU.get("meta") or {}).get("transitive") is False, "causal-edges.yaml meta.transitive is not false")
if FAILS:
    print("CONTRACT-SELFCHECK-FAILED（解析器或契约异常，判定不可信）:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)

NS = (TMAP.get("namespaces") or {}).get("kg") or {}
RMS = RMAP.get("namespaces") or {}
LABEL_PREFIX = NS.get("label_prefix", "CC_")
RELTYPE_PREFIX = RMS.get("reltype_prefix", "CC_")
CONSTRAINT_PREFIX = NS.get("constraint_prefix", "cc_uq_")

# 具体类型码（topology.yaml：非 abstract）
CONCRETE_CODES = [t["code"] for t in TOPO_TYPES if not t.get("abstract")]
ATT_CODES = [t["code"] for t in TOPO_TYPES if t.get("annotation")]
ABSTRACT_CODES = [t["code"] for t in TOPO_TYPES if t.get("abstract")]
TYPE_LABELS = set(LABEL_PREFIX + c for c in CONCRETE_CODES)
FAULT_LABELS = set(
    LABEL_PREFIX + c for c in ("FaultMode", "Metric", "Threshold", "Rule", "Finding", "Action", "Meta")
)
ALL_LABELS = TYPE_LABELS | FAULT_LABELS

REL_CODES = [r["code"] for r in TOPO_RELS]
REL_LABELS = set(RELTYPE_PREFIX + c for c in REL_CODES)
INV_REL_LABELS = set(RELTYPE_PREFIX + r["code"] for r in TOPO_INV)
FAULT_RELS = set(
    RELTYPE_PREFIX + c for c in ("CAUSES", "CONDITIONAL_CAUSES", "RELATES_TO", "JUDGED_BY", "AFFECTS")
)
ALL_RELTYPES = REL_LABELS | INV_REL_LABELS | FAULT_RELS

FM_IDS = [f["code"] for f in FM_LIST]
FM_BY_ID = dict((f["code"], f) for f in FM_LIST)
EDGE_IDS = [e["id"] for e in EDGE_LIST]
EDGE_BY_ID = dict((e["id"], e) for e in EDGE_LIST)
THR_NAMES = [t["name"] for t in THR_LIST]
THR_BY_NAME = dict((t["name"], t) for t in THR_LIST)
MET_CODES = [m["code"] for m in MET_LIST]
MET_BY_CODE = dict((m["code"], m) for m in MET_LIST)
CAUSAL_CYCLES = (CAU.get("meta") or {}).get("cycles") or []

# 叶子根因（排除法）
LEAF = (CAU.get("leaf_root_causes") or [])
LEAF_SRC = LEAF[0] if LEAF else {}

# 谓词枚举
PREDICATE_ENUM = (FCM.get("meta") or {}).get("predicate_enum") or []

# 指标 → KG 属性名；指标 → 对象类型标签
MET_KGPROP = {}
for code in MET_CODES:
    ent = TMAP_METRICS.get(code) or {}
    if ent.get("kg_property"):
        MET_KGPROP[code] = ent["kg_property"]

# 类型 → 属性名集合（key_attrs + type-map attrs）
TYPE_ATTRS = {}
for t in TOPO_TYPES:
    TYPE_ATTRS[t["code"]] = set(t.get("key_attrs") or [])
_tmap_items = TMAP_TYPES.items() if isinstance(TMAP_TYPES, dict) else [
    (t.get("neutral_code") or t.get("code"), t) for t in TMAP_TYPES
]
for _code, _ent in _tmap_items:
    if not _code or not isinstance(_ent, dict):
        continue
    _attrs = _ent.get("attrs") or {}
    _names = set(_attrs.keys()) if isinstance(_attrs, dict) else set(_attrs)
    TYPE_ATTRS.setdefault(_code, set()).update(_names)
    if _ent.get("key_attrs"):
        TYPE_ATTRS[_code].update(_ent["key_attrs"])
# 每个类型的指标属性（kg_property）
METRICS_BY_OBJECT = {}
for code in MET_CODES:
    ent = MET_BY_CODE.get(code) or {}
    obj = ent.get("object")
    if obj:
        METRICS_BY_OBJECT.setdefault(obj, set()).add(MET_KGPROP.get(code, ""))

# non_zero 谓词豁免集合：指标码 → 是
NON_ZERO_METRICS = set()
for fm in FM_LIST:
    det = fm.get("detect") or {}
    for clause in (det.get("all_of") or []) + (det.get("any_of") or []):
        if clause.get("predicate") == "non_zero":
            NON_ZERO_METRICS.add(clause.get("metric"))
NON_ZERO_PROPS = set(MET_KGPROP.get(c, "") for c in NON_ZERO_METRICS)


# ============================================================================
# 3. Cypher 语句抽取（去注释、按 ';' 切分、保留起始行号）
# ============================================================================

def cypher_statements(path):
    """返回 (statements, comment_lines)；statements = [(lineno, text)]。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw_lines = fh.read().split("\n")
    stmts = []
    comments = []
    buf = []
    start = None
    in_block_comment = False
    for n, raw in enumerate(raw_lines, 1):
        line = raw
        if in_block_comment:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block_comment = False
            else:
                comments.append((n, raw))
                continue
        if "/*" in line and line.split("/*", 1)[0].count("'") % 2 == 0:
            # 引号内的 `/*` 不是注释开始（例：m.contractRef = 'shared/model/*.yaml'）
            head = line.split("/*", 1)[0]
            if "*/" in line.split("/*", 1)[1]:
                line = head + line.split("*/", 1)[1]
            else:
                in_block_comment = True
                line = head
                comments.append((n, raw))
        # 行内 // 注释（引号感知）
        q = None
        cut = None
        i = 0
        while i < len(line):
            c = line[i]
            if q:
                if c == q:
                    q = None
            elif c in "'\"":
                q = c
            elif c == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
            i += 1
        code = line if cut is None else line[:cut]
        if cut is not None:
            comments.append((n, raw))
        if code.strip() == "":
            continue
        # 按 ';' 切分（同一行多语句）
        parts = code.split(";")
        for pi, part in enumerate(parts):
            if part.strip():
                if start is None:
                    start = n
                buf.append(part)
            last = pi == len(parts) - 1
            if not last:
                if buf:
                    stmts.append((start, "\n".join(buf)))
                buf = []
                start = None
    if buf and "".join(buf).strip():
        stmts.append((start or 1, "\n".join(buf)))
    return stmts, comments


def line_of(text, needle, base=1):
    """在语句文本中定位 needle 的行号（相对语句起始行）。"""
    idx = text.find(needle)
    if idx < 0:
        return base
    return base + text[:idx].count("\n")


# ============================================================================
# 4. 读取交付文件
# ============================================================================

ONT = os.path.join(ROOT, "scheme1-kg", "ontology")
QRY = os.path.join(ROOT, "scheme1-kg", "queries")
FILES = ["01-schema.cypher", "02-topology.cypher", "03-fault-modes.cypher", "04-rules.cypher"]
PATHS = {}
for fn in FILES:
    p = os.path.join(ONT, fn)
    if not os.path.exists(p):
        print("MISSING-DELIVERABLE: %s" % rel(p))
        sys.exit(1)
    PATHS[fn] = p

QUERY_PATHS = []
if os.path.isdir(QRY):
    for fn in sorted(os.listdir(QRY)):
        if fn.endswith(".cypher"):
            QUERY_PATHS.append(os.path.join(QRY, fn))

STMT = {}
COMMENTS = {}
for fn, p in PATHS.items():
    STMT[p], COMMENTS[p] = cypher_statements(p)


# 故障模型侧标签与"03 文件里真正写入的属性名"（用于索引死属性检查）
FAULT_SIDE_LABELS = set(["FaultMode", "Metric", "Threshold", "Rule", "Finding", "Action", "Meta"])
FAULT_WRITTEN_PROPS = set()
for _l, _t in STMT[PATHS["03-fault-modes.cypher"]]:
    FAULT_WRITTEN_PROPS.update(re.findall(r"(\w+):\s*'", _t))
    FAULT_WRITTEN_PROPS.update(re.findall(r"(\w+):\s*\[", _t))
    FAULT_WRITTEN_PROPS.update(re.findall(r"(\w+):\s*-?[0-9]", _t))
    FAULT_WRITTEN_PROPS.update(re.findall(r"\.(\w+)\s*=", _t))
for _l, _t in STMT[PATHS["01-schema.cypher"]]:
    FAULT_WRITTEN_PROPS.update(re.findall(r"m\.(\w+)\s*=", _t))
FAULT_WRITTEN_PROPS.update({"key", "fmCode", "metricCode", "thrCode"})


# ============================================================================
# 5. 检查项
# ============================================================================

LABEL_RE = re.compile(r"\(\s*\w*\s*:\s*(CC_[A-Za-z0-9_]+)")
RELTYPE_RE = re.compile(r"\[\s*\w*\s*:\s*(CC_[A-Za-z0-9_]+)")
PLAIN_RELTYPE_RE = re.compile(r":(CC_[A-Z0-9_]+)\]")


def check_labels_reltypes():
    """C01/C02：标签与关系类型白名单（覆盖 ontology + queries 全部 .cypher）。"""
    scan = list(PATHS.values()) + QUERY_PATHS
    for p in scan:
        stmts, _ = cypher_statements(p)
        for line0, text in stmts:
            for m in LABEL_RE.finditer(text):
                lab = m.group(1)
                if lab not in ALL_LABELS:
                    err(p, line_of(text, m.group(0), line0), "C01", "未知标签 `%s`（不在契约类型/故障域标签内）" % lab)
            for m in RELTYPE_RE.finditer(text):
                rt = m.group(1)
                if rt not in ALL_RELTYPES:
                    err(p, line_of(text, m.group(0), line0), "C02", "未知关系类型 `%s`（不在契约 reltype/故障域关系内）" % rt)
            for m in PLAIN_RELTYPE_RE.finditer(text):
                rt = m.group(1)
                if rt not in ALL_RELTYPES:
                    err(p, line_of(text, m.group(0), line0), "C02", "未知关系类型 `%s`" % rt)


FM_DEF_RE = re.compile(r"MERGE\s*\(\s*(\w+)\s*:\s*CC_FaultMode\s*\{\s*fmCode\s*:\s*'([^']+)'")
EDGE_RE = re.compile(
    r"MERGE\s*\(\s*(\w+)\s*\)\s*-\s*\[\s*(\w*)\s*:\s*(CC_[A-Za-z0-9_]+)\s*\{\s*contractId\s*:\s*'([^']+)'\s*\}\s*\]\s*->\s*\(\s*(\w+)\s*\)"
)
COND_RE = re.compile(r"r\.(cond[A-Z]\w*)\s*=")
FMCODE_LIT_RE = re.compile(r"\{fmCode:\s*'([^']+)'\}")
SEVERITY_LIT_RE = re.compile(r"\{severity:\s*'([^']+)'\}")


def collect_edges(fn):
    """从 03 收集边：[(lineno, edge_id, reltype, from_fm, to_fm, cond_props)]。"""
    out = []
    for line0, text in STMT[fn]:
        for m in EDGE_RE.finditer(text):
            eid = m.group(4)
            reltype = m.group(3)
            fms = FMCODE_LIT_RE.findall(text)
            conds = [c for c in COND_RE.findall(text)]
            out.append((line_of(text, m.group(0), line0), eid, reltype, fms[0] if fms else None,
                        fms[-1] if fms else None, conds))
    return out


def check_fm_ids():
    """C03：FM id 集合相等。"""
    p = PATHS["03-fault-modes.cypher"]
    found = []
    for line0, text in STMT[p]:
        for m in FM_DEF_RE.finditer(text):
            found.append((line_of(text, m.group(0), line0), m.group(2)))
    got = [x[1] for x in found]
    if len(set(got)) != len(got):
        dup = sorted(set(x for x in got if got.count(x) > 1))
        err(p, 1, "C03", "FM 节点定义重复：%s" % dup)
    missing = sorted(set(FM_IDS) - set(got))
    extra = sorted(set(got) - set(FM_IDS))
    if missing:
        err(p, 1, "C03", "缺少故障模式定义：%s" % missing)
    if extra:
        err(p, 1, "C03", "自创故障模式 id：%s" % extra)
    return found


def check_edge_ids():
    """C04：边 id 集合相等 + (from,relation,to) 与契约一致。"""
    p = PATHS["03-fault-modes.cypher"]
    edges = collect_edges(p)
    got = [e[1] for e in edges]
    if len(set(got)) != len(got):
        err(p, 1, "C04", "边重复定义：%s" % sorted(set(x for x in got if got.count(x) > 1)))
    missing = sorted(set(EDGE_IDS) - set(got))
    extra = sorted(set(got) - set(EDGE_IDS))
    if missing:
        err(p, 1, "C04", "缺少因果边：%s" % missing)
    if extra:
        err(p, 1, "C04", "自创因果边 id：%s" % extra)
    rel_map = {"causes": "CC_CAUSES", "conditional_causes": "CC_CAUSES", "relates_to": "CC_RELATES_TO"}
    for line, eid, reltype, frm, to, conds in edges:
        spec = EDGE_BY_ID.get(eid)
        if not spec:
            continue
        if reltype != rel_map.get(spec.get("relation")):
            err(p, line, "C04", "边 %s 的关系类型 %s 与契约 relation=%s（应为 %s）不符"
                % (eid, reltype, spec.get("relation"), rel_map.get(spec.get("relation"))))
        if frm != spec.get("from") or to != spec.get("to"):
            err(p, line, "C04", "边 %s 的端点 (%s->%s) 与契约 (%s->%s) 不符"
                % (eid, frm, to, spec.get("from"), spec.get("to")))
        if spec.get("relation") == "conditional_causes" and not conds:
            err(p, line, "C04", "条件因果边 %s 未写任何 cond* 属性（§5.3 要求条件必须可机读）" % eid)
        if spec.get("relation") == "causes" and conds:
            err(p, line, "C04", "无条件因果边 %s 出现了 cond* 属性（语义与契约不符）" % eid)
    # 边属性必填 = **契约字段**（captain 裁定：契约优先口径）：contractId(=id) / chain /
    #   description(=evidence) / provenance。`weight`/`learnCount`/`probability`/`delaySec`/
    #   `chainPosition` 不在 causal-edges.yaml 内 ⇒ 既不要求、也不允许（写了由 C09 抓）。
    for line0, text in STMT[p]:
        for m in EDGE_RE.finditer(text):
            eid = m.group(4)
            # contractId 写在 MERGE 模式里（`[r:CC_CAUSES {contractId:'E1'}]`）而非 SET（= MERGE 键）
            if "contractId" not in text:
                err(p, line_of(text, m.group(0), line0), "C04", "边 %s 缺少 r.contractId" % eid)
            for prop in ("r.chain", "r.description", "r.provenance"):
                if prop not in text:
                    err(p, line_of(text, m.group(0), line0), "C04", "边 %s 缺少 %s" % (eid, prop))
            prov = re.search(r"r\.provenance\s*=\s*'([^']*)'", text)
            if not prov or eid not in prov.group(1):
                err(p, line_of(text, m.group(0), line0), "C04",
                    "边 %s 的 provenance 未指向自身契约锚点（应为 authored:shared/model/causal-edges.yaml#%s）" % (eid, eid))
    return edges


def check_no_var_length():
    """C06：禁止变长路径。"""
    scan = list(PATHS.values()) + QUERY_PATHS
    for p in scan:
        stmts, _ = cypher_statements(p)
        for line0, text in stmts:
            for m in re.finditer(r"\*\s*(\d+)", text):
                err(p, line_of(text, m.group(0), line0), "C06",
                    "出现变长路径 `%s`（因果不声明传递性；限深 BFS 属服务层 M2.9）" % m.group(0))
            for m in re.finditer(r"\*\s*\.\.", text):
                err(p, line_of(text, m.group(0), line0), "C06", "出现变长路径 `%s`" % m.group(0))


def check_cycles(edges):
    """C07：有向环 ⊆ 契约白名单 meta.cycles。"""
    p = PATHS["03-fault-modes.cypher"]
    adj = {}
    for _line, eid, reltype, frm, to, _c in edges:
        if reltype == "CC_CAUSES":
            adj.setdefault(frm, set()).add(to)
    # Tarjan SCC
    index = {}
    low = {}
    on = {}
    stack = []
    sccs = []
    counter = [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on[v] = True
        for w in adj.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif on.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on[w] = False
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    sys.setrecursionlimit(10000)
    for v in list(adj):
        if v not in index:
            strong(v)
    whitelist = [set(c) for c in CAUSAL_CYCLES]
    for comp in sccs:
        if len(comp) < 2:
            continue
        cset = set(comp)
        if not any(cset <= wl or wl <= cset for wl in whitelist):
            err(p, 1, "C07", "检测到非白名单有向环：%s（白名单 = %s）" % (sorted(cset), [sorted(w) for w in whitelist]))
    for wl in whitelist:
        if not wl <= set(FM_IDS):
            err(p, 1, "C07", "白名单环含未知故障模式：%s" % sorted(wl - set(FM_IDS)))
    if not whitelist:
        warn(p, 1, "C07", "契约未声明任何白名单环（若图内确有环将报错）")


THR_REF_RE = re.compile(r"threshold:([a-z0-9_]+)#(warning|critical)")


def check_thresholds():
    """C08：阈值引用可解、档位必填、未标定档位 WARN、命名约定。"""
    seen = {}
    for fn, p in PATHS.items():
        stmts, _ = cypher_statements(p)
        for line0, text in stmts:
            for m in THR_REF_RE.finditer(text):
                seen.setdefault(m.group(1) + "#" + m.group(2), []).append((p, line_of(text, m.group(0), line0)))
            # 裸 threshold:<名> 不带档位
            for m in re.finditer(r"threshold:([a-z0-9_]+)(?![\w#])", text):
                if "#" not in text[m.end():m.end() + 1]:
                    err(p, line_of(text, m.group(0), line0), "C08",
                        "阈值引用 `threshold:%s` 未带档位（`#warning` / `#critical` 必填）" % m.group(1))
    for key, locs in sorted(seen.items()):
        name, tier = key.split("#")
        spec = THR_BY_NAME.get(name)
        for p, line in locs:
            if spec is None:
                err(p, line, "C08", "悬空阈值引用 `threshold:%s#%s`（fault-modes.yaml#thresholds 无此个体）" % (name, tier))
                continue
            if spec.get(tier) is None:
                warn(p, line, "C08", "阈值 `%s#%s` 已引用但该档位**未标定**（YAML 为 null）⇒ 规则当前不可机判" % (name, tier))
            if name.endswith("_light") and tier != "warning":
                warn(p, line, "C08", "命名约定 `*_light` 对应 warning，此处引用 %s" % tier)
            if (name.endswith("_severe") or name.endswith("_high")) and tier != "critical":
                warn(p, line, "C08", "命名约定 `*_severe`/`*_high` 对应 critical，此处引用 %s" % tier)
    if not seen:
        err(PATHS["03-fault-modes.cypher"], 1, "C08", "语料里没有任何 threshold:<名>#<档位> 引用")
    return seen


NUM_RE = re.compile(r"(?<![\w'.])(\d+(?:\.\d+)?)(?![\w'])")
CMP_NUM_RE = re.compile(r"([A-Za-z_][\w]*)\.(\w+)\s*(>=|<=|>|<|==)\s*(\d+(?:\.\d+)?)")


def check_rules_no_numeric():
    """C05：规则体零数字字面量（唯豁免 = 契约 predicate: non_zero 的符号判定）。"""
    p = PATHS["04-rules.cypher"]
    stmts, _ = cypher_statements(p)
    for line0, text in stmts:
        allowed_spans = []
        for m in CMP_NUM_RE.finditer(text):
            var, prop, _op, num = m.groups()
            if prop in NON_ZERO_PROPS and num == "0":
                allowed_spans.append((m.start(), m.end()))
        # 去掉允许区间后仍出现数字 → 违规
        chars = list(text)
        for a, b in allowed_spans:
            for i in range(a, b):
                chars[i] = " "
        residue = "".join(chars)
        for m in NUM_RE.finditer(residue):
            token = m.group(1)
            if token == "0" and "non_zero" in residue[max(0, m.start() - 60):m.start()]:
                continue
            err(p, line_of(residue, token, line0), "C05",
                "规则体出现数字字面量 `%s`（阈值必须引用节点/属性；符号谓词 non_zero 除外）" % token)


def check_judged_by():
    """C13：JUDGED_BY 与 YAML detect 逐子句一致；并检查 role 枚举/quadruple 形状。"""
    p = PATHS["03-fault-modes.cypher"]
    role_enum = {"necessary", "sufficient", "exclusion", "distinguishing"}
    actual = {}
    jd_counter = 0
    for line0, text in STMT[p]:
        for m in re.finditer(r"MERGE\s*\(\s*\w+\s*\)\s*-\s*\[\s*\w*\s*:\s*CC_JUDGED_BY\s*\{(.*?)\}\s*\]\s*->\s*\(\s*\w+\s*\)", text, re.S):
            props = m.group(1)
            jd_counter += 1
            fms = FMCODE_LIT_RE.findall(text)
            mets = re.findall(r"metricCode:\s*'([^']+)'", text)
            role = re.search(r"role:\s*'([^']+)'", props)
            op = re.search(r"op:\s*'([^']+)'", props)
            kind = re.search(r"operandKind:\s*'([^']+)'", props)
            lno = line_of(text, m.group(0), line0)
            if not fms:
                err(p, lno, "C13", "JUDGED_BY 边未带 fmCode")
                continue
            fm = fms[0]
            met = mets[0] if mets else None
            if role is None or role.group(1) not in role_enum:
                err(p, lno, "C13", "JUDGED_BY 的 role 缺失或不在枚举 %s" % sorted(role_enum))
                continue
            if op is None or kind is None:
                err(p, lno, "C13", "JUDGED_BY 的 (op, operandKind) 四元组不完整")
                continue
            if met is None:
                err(p, lno, "C13", "JUDGED_BY 未指向 CC_Metric")
                continue
            if met not in MET_BY_CODE:
                err(p, lno, "C13", "JUDGED_BY 指向未知指标码 `%s`" % met)
            k = kind.group(1)
            if k == "threshold":
                ref = re.search(r"operandRef:\s*'([^']+)'", props)
                if not ref or "#" not in ref.group(1):
                    err(p, lno, "C13", "operandKind=threshold 但 operandRef 缺失或未带档位")
                else:
                    tname = ref.group(1).split("#")[0]
                    if tname.startswith("threshold:"):
                        tname = tname[len("threshold:"):]
                    if tname not in THR_BY_NAME:
                        err(p, lno, "C13", "operandRef 指向未定义阈值 `%s`" % ref.group(1))
            elif k == "entity_property":
                ref = re.search(r"operandRef:\s*'([^']+)'", props)
                if not ref or "." not in ref.group(1):
                    err(p, lno, "C13", "operandKind=entity_property 但 operandRef 形如 `<类型>.<属性>` 缺失")
            elif k == "predicate":
                if op.group(1) not in PREDICATE_ENUM:
                    err(p, lno, "C13", "operandKind=predicate 但 op=`%s` 不在 predicate_enum %s" % (op.group(1), PREDICATE_ENUM))
            elif k == "enum":
                if not re.search(r"operandValues:\s*\[", props):
                    err(p, lno, "C13", "operandKind=enum 但缺 operandValues 列表")
            elif k == "literal":
                if not re.search(r"operandValue:\s*'", props):
                    err(p, lno, "C13", "operandKind=literal 但缺 operandValue")
            else:
                err(p, lno, "C13", "operandKind=`%s` 不在 {literal, entity_property, threshold, predicate, enum}" % k)
            actual.setdefault(fm, []).append((met, role.group(1)))

    # 期望：由 fault-modes.yaml detect 结构机械推导
    expected = {}
    for fm in FM_LIST:
        code = fm["code"]
        det = fm.get("detect") or {}
        for clause in (det.get("all_of") or []):
            expected.setdefault(code, []).append((clause.get("metric"), "necessary"))
        for clause in (det.get("any_of") or []):
            expected.setdefault(code, []).append((clause.get("metric"), "sufficient"))
        for clause in (det.get("distinguishing_evidence") or []):
            expected.setdefault(code, []).append((clause.get("metric"), "distinguishing"))
    for code, items in expected.items():
        a = sorted(actual.get(code, []))
        e = sorted(items)
        missing = [x for x in e if x not in a]
        if missing:
            err(p, 1, "C13", "故障模式 %s 的判据缺失（YAML detect → JUDGED_BY）：%s" % (code, missing))
    for code in actual:
        if code not in FM_BY_ID:
            err(p, 1, "C13", "JUDGED_BY 指向未知故障模式 %s" % code)
    # 排除法（leaf_root_causes.exclusion_requirements）→ role=exclusion
    for fm in FM_LIST:
        if fm["code"] == (LEAF_SRC.get("code") or "\0"):
            if not any(r == "exclusion" for _m, r in actual.get(fm["code"], [])):
                err(p, 1, "C13", "排除法叶子根因 %s 无 role=exclusion 判据" % fm["code"])
    for lno, text in STMT[p]:
        if "CC_JUDGED_BY" in text and not text.strip().startswith("//"):
            pass
    return jd_counter, actual


AFFECTS_RE = re.compile(
    r"MATCH\s*\(\s*\w+\s*:\s*CC_FaultMode\s*\{\s*fmCode\s*:\s*'([^']+)'\s*\}\s*\)\s*,\s*\(\s*\w+\s*:\s*(CC_[A-Za-z0-9_]+)\s*\)\s*MERGE\s*\(\s*\w+\s*\)\s*-\s*\[\s*:\s*CC_AFFECTS\s*\{\s*severity\s*:\s*'([^']+)'\s*\}\s*\]\s*->",
    re.S,
)


def check_affects():
    """C14：AFFECTS 覆盖 14 个 FM，目标标签 = CC_<fmComponentType>，severity = fmSeverity。"""
    p = PATHS["03-fault-modes.cypher"]
    seen = {}
    for line0, text in STMT[p]:
        for m in AFFECTS_RE.finditer(text):
            fm, lab, sev = m.group(1), m.group(2), m.group(3)
            seen.setdefault(fm, []).append((lab, sev, line_of(text, m.group(0), line0)))
    for code in FM_IDS:
        spec = FM_BY_ID[code]
        want_lab = LABEL_PREFIX + spec["component_type"]
        rows = seen.get(code)
        if not rows:
            err(p, 1, "C14", "故障模式 %s 无 CC_AFFECTS 出边（§4.3 门禁：每个 FM ≥1 条）" % code)
            continue
        for lab, sev, line in rows:
            if lab != want_lab:
                err(p, line, "C14", "%s 的 AFFECTS 目标标签 %s ≠ fmComponentType 对应标签 %s" % (code, lab, want_lab))
            if sev != spec.get("severity"):
                err(p, line, "C14", "%s 的 AFFECTS severity=%s ≠ fmSeverity=%s" % (code, sev, spec.get("severity")))
    for code in seen:
        if code not in FM_BY_ID:
            err(p, 1, "C14", "AFFECTS 指向未定义故障模式 %s" % code)
    return seen


def check_metric_set():
    """C11：CC_Metric 集合 == metrics.yaml 的 91 个指标码；属性必须与契约一致。"""
    p = PATHS["03-fault-modes.cypher"]
    codes = []
    for _line0, text in STMT[p]:
        if "UNWIND" in text and "metricCode:" in text:
            codes += re.findall(r"metricCode:\s*'([^']+)'", text)
    missing = sorted(set(MET_CODES) - set(codes))
    extra = sorted(set(codes) - set(MET_CODES))
    if missing:
        err(p, 1, "C11", "缺少指标节点（metrics.yaml 有而图中无）：%s" % missing)
    if extra:
        err(p, 1, "C11", "自创指标码：%s" % extra)
    # 表内字段与契约一致性
    for _line0, text in STMT[p]:
        if "UNWIND" not in text or "metricCode:" not in text:
            continue
        for m in re.finditer(r"\{metricCode:'([^']+)'(.*?)\}", text):
            code, rest = m.group(1), m.group(2)
            spec = MET_BY_CODE.get(code)
            if not spec:
                continue
            for key, field in (("metricObject", "object"), ("metricLayer", "layer"), ("metricType", "type"),
                               ("metricSource", "source")):
                mm = re.search(key + r":\s*'([^']*)'", rest)
                got = mm.group(1) if mm else None
                want = spec.get(field)
                if want is not None and got != want:
                    err(p, 1, "C11", "指标 %s 的 %s=%r ≠ 契约 %s=%r" % (code, key, got, field, want))
            mm = re.search(r"metricKgProperty:\s*'([^']*)'", rest)
            want_prop = MET_KGPROP.get(code)
            if want_prop and (not mm or mm.group(1) != want_prop):
                err(p, 1, "C11", "指标 %s 的 metricKgProperty=%r ≠ type-map.kg_property=%r"
                    % (code, mm.group(1) if mm else None, want_prop))
    return codes


def check_threshold_set():
    """C12：CC_Threshold 集合 == fault-modes.yaml#thresholds；数值逐字一致。"""
    p = PATHS["03-fault-modes.cypher"]
    codes = []
    for _line0, text in STMT[p]:
        if "UNWIND" in text and "thrCode:" in text:
            codes += re.findall(r"thrCode:\s*'([^']+)'", text)
    missing = sorted(set(THR_NAMES) - set(codes))
    extra = sorted(set(codes) - set(THR_NAMES))
    if missing:
        err(p, 1, "C12", "缺少阈值节点：%s" % missing)
    if extra:
        err(p, 1, "C12", "自创阈值个体：%s" % extra)
    for _line0, text in STMT[p]:
        if "UNWIND" not in text or "thrCode:" not in text:
            continue
        for m in re.finditer(r"\{thrCode:'([^']+)'(.*?)\}", text):
            code, rest = m.group(1), m.group(2)
            spec = THR_BY_NAME.get(code)
            if not spec:
                continue
            if spec.get("metric"):
                mm = re.search(r"thrMetric:\s*'([^']*)'", rest)
                if not mm or mm.group(1) != spec["metric"]:
                    err(p, 1, "C12", "阈值 %s 的 thrMetric=%r ≠ 契约 %r" % (code, mm.group(1) if mm else None, spec["metric"]))
            for tier in ("warning", "critical"):
                want = spec.get(tier)
                mm = re.search("thr" + tier.capitalize() + r":\s*([0-9.]+|null)", rest)
                got = mm.group(1) if mm else None
                if want is None:
                    if got not in ("null", None):
                        err(p, 1, "C12", "阈值 %s 的 %s 在契约中为 null（未标定），图里却写了 %s" % (code, tier, got))
                else:
                    # ★ 2026-10-10 修：图里写 null 而契约有值 ⇒ 报**不一致 ERROR**，不得抛
                    #   ValueError（原实现直接 float('null') 崩溃，把「不一致」误报成「门禁坏」）。
                    if got is None or got == "null":
                        err(p, 1, "C12", "阈值 %s 的 %s 在图里为 null，但契约已标定为 %r"
                            "（两载体必须逐字一致；标定后须同批更新 KG 侧声明）" % (code, tier, want))
                    elif abs(float(got) - float(want)) > 1e-12:
                        err(p, 1, "C12", "阈值 %s 的 %s=%r ≠ 契约 %r" % (code, tier, got, want))
    return codes


GAP_RE = re.compile(r"//\s*GAP:\s*(E\d+)\s+([A-Za-z,]+)")
GAPDOC_RE = re.compile(r"//\s*GAP-DOC-(\d+)")


def check_gap_ledger():
    """C09：GAP 台账与真实缺失一致（防"漏写用台账掩盖"与"台账过期"）。"""
    p = PATHS["03-fault-modes.cypher"]
    declared = {}
    for line, text in COMMENTS[p]:
        for m in GAP_RE.finditer(text):
            declared.setdefault(m.group(1), set()).update(x.strip() for x in m.group(2).split(",") if x.strip())
    # ★ 反向检查（captain 裁定「契约优先」）：边语句里**不得**出现契约未声明的属性。
    #   「契约字段」= causal-edges.yaml 各边的键（id/from/relation/to/chain/evidence/provenance
    #   + 条件边的 condition）；「图内命名」= contractId(=`id`) / description(=`evidence`) / cond*。
    #   二者之外的 `r.<prop>` 一律 ERROR —— 原先误落的 weight/learnCount/probability/delaySec/
    #   chainPosition 即由此抓出。`// GAP: E* <props>` 台账仍可**显式豁免**（本轮已清空：这些
    #   字段不是"某边漏写"，而是契约根本没有的字段，登记反而会误示"应写而未写"）。
    allowed = {"contractId", "chain", "description", "provenance",
               "condMetric", "condOp", "condKind", "condRef"}
    for _e in EDGE_LIST:
        allowed.update(_e.keys())
    for line0, text in STMT[p]:
        for m in EDGE_RE.finditer(text):
            eid = m.group(4)
            written = set(re.findall(r"\br\.(\w+)\s*=", text))
            for prop in sorted(written - allowed - declared.get(eid, set())):
                err(p, line_of(text, m.group(0), line0), "C09",
                    "边 %s 写了契约未声明的属性 `%s`（契约优先口径：causal-edges.yaml 无此字段）" % (eid, prop))
            for prop in sorted(written & declared.get(eid, set())):
                err(p, line_of(text, m.group(0), line0), "C09",
                    "边 %s 写了 %s 却仍登记为 GAP（台账过期）" % (eid, prop))
    for eid in declared:
        if eid not in EDGE_BY_ID:
            err(p, 1, "C09", "GAP 台账引用了不存在的边 id %s" % eid)
    if not GAPDOC_RE.search("\n".join(t for _l, t in COMMENTS[p])):
        warn(p, 1, "C09", "无 GAP-DOC 台账行")


def check_schema():
    """C18：01-schema 计数 + 命名纪律；C16：CC_Meta 单例。"""
    p = PATHS["01-schema.cypher"]
    cons, idx = [], []
    for line0, text in STMT[p]:
        for m in re.finditer(r"CREATE\s+CONSTRAINT\s+(\w+)", text, re.I):
            cons.append((line_of(text, m.group(0), line0), m.group(1)))
        for m in re.finditer(r"CREATE\s+INDEX\s+(\w+)", text, re.I):
            idx.append((line_of(text, m.group(0), line0), m.group(1)))
    names = [n for _l, n in cons]
    biz = [n for n in names if not n.startswith("cc_uq_fm_") and not n.startswith("cc_uq_metric")
           and not n.startswith("cc_uq_thr") and not n.startswith("cc_uq_rule") and not n.startswith("cc_uq_finding")
           and not n.startswith("cc_uq_action") and not n.startswith("cc_uq_meta")]
    if len(biz) != 13:
        err(p, 1, "C18", "业务拓扑唯一约束应为 12（具体业务类型）+ 1（Evidence）= 13 条，实得 %d 条" % len(biz))
    fault_side = [n for n in names if n not in biz]
    if len(fault_side) != 7:
        err(p, 1, "C18", "故障模型侧唯一约束应为 7 条（§2.2），实得 %d 条" % len(fault_side))
    if len(idx) != 6:
        err(p, 1, "C18", "索引应为 6 条（§2.3），实得 %d 条" % len(idx))
    for line, n in cons:
        if not n.startswith(CONSTRAINT_PREFIX):
            err(p, line, "C18", "约束名 `%s` 未使用契约前缀 %s" % (n, CONSTRAINT_PREFIX))
        if n.startswith("cc_uq_cc_"):
            err(p, line, "C18", "约束名 `%s` 与投影器 P1 既有约束撞名" % n)
        if n.startswith("gdo_"):
            err(p, line, "C18", "约束名 `%s` 侵入既有 v2 图命名空间" % n)
    for line, n in idx:
        if not n.startswith("cc_idx_"):
            err(p, line, "C18", "索引名 `%s` 未使用 cc_idx_ 前缀" % n)
    # 前缀混用
    for line0, text in STMT[p]:
        if re.search(r"CREATE\s+CONSTRAINT\s+cc_idx_", text, re.I):
            err(p, line0, "C18", "cc_idx_* 被用于 CREATE CONSTRAINT（命名纪律混用）")
        if re.search(r"CREATE\s+INDEX\s+cc_uq_", text, re.I):
            err(p, line0, "C18", "cc_uq_* 被用于 CREATE INDEX（命名纪律混用）")
    # C16：CC_Meta 单例 + snapshotId
    META_MERGE_RE = re.compile(r"MERGE\s*\(\s*\w+\s*:\s*CC_Meta\s*\{")
    metas = [t for _l, t in STMT[p] if META_MERGE_RE.search(t)]
    if len(metas) != 1:
        err(p, 1, "C16", "CC_Meta 元数据节点应恰好 1 条 MERGE，实得 %d" % len(metas))
    else:
        if "MERGE (m:CC_Meta {key: 'graph'})" not in metas[0] and "MERGE (m:CC_Meta {key:'graph'})" not in metas[0]:
            err(p, 1, "C16", "CC_Meta 单例键不是 {key:'graph'}（§2.4）")
        if "snapshotId" not in metas[0]:
            err(p, 1, "C16", "CC_Meta 缺 snapshotId（硬约束 C1/R5）")
        if "schemaVersion" not in metas[0] or "contractRef" not in metas[0] or "ccfVersion" not in metas[0]:
            err(p, 1, "C16", "CC_Meta 缺 §2.4 要求的 schemaVersion/contractRef/ccfVersion 之一")
    # 索引属性必须真实存在（T7 静态版：无死索引）
    for line0, text in STMT[p]:
        for m in re.finditer(r"CREATE\s+INDEX\s+\w+\s+IF\s+NOT\s+EXISTS\s+FOR\s+\(\s*\w*\s*:\s*(CC_\w+)\s*\)\s+ON\s*\(([^)]*)\)", text, re.I):
            lab = m.group(1)[3:]
            props = re.findall(r"n\.(\w+)", m.group(2))
            for prop in props:
                pool = set(TYPE_ATTRS.get(lab, set())) | set(METRICS_BY_OBJECT.get(lab, set()))
                if lab in FAULT_SIDE_LABELS:
                    if prop not in FAULT_WRITTEN_PROPS:
                        err(p, line_of(text, m.group(0), line0), "C18",
                            "索引属性 `%s.%s` 在 03-fault-modes.cypher 中从未被写入（死索引；已写入集合=%s）"
                            % (lab, prop, sorted(FAULT_WRITTEN_PROPS)))
                    continue
                if pool and prop not in pool:
                    err(p, line_of(text, m.group(0), line0), "C18",
                        "索引属性 `%s.%s` 不在契约属性空间内（死索引风险；属性空间=%s）" % (lab, prop, sorted(pool)))
        # ★ 关系索引有效性（captain 裁定：前导属性必须 ∈ 契约 ∪ 图）：
        #   「契约」= causal-edges.yaml 各边的键（id/from/relation/to/chain/evidence/provenance/condition）；
        #   「图」的静态抓手 = 03-fault-modes.cypher 中真正被 `SET r.<prop> = …` 写入过的边属性集合
        #   （本轮 = contractId / description / cond*，分别是契约 id / evidence / condition 的图内命名）。
        #   两者都不含 ⇒ 死索引风险 ⇒ ERROR。（原白名单是 docs/03 §5.2 的 6 个非契约字段，已撤。）
        edge_written = set()
        for _l0, _t in STMT[PATHS["03-fault-modes.cypher"]]:
            edge_written.update(re.findall(r"\br\.(\w+)\s*=", _t))
        edge_allowed = {"contractId", "description", "condMetric", "condOp", "condKind", "condRef"}
        for _e in EDGE_LIST:
            edge_allowed.update(_e.keys())
        for m in re.finditer(r"FOR\s+\(\s*\)\s*-\s*\[\s*\w*\s*:\s*(CC_CAUSES|CC_RELATES_TO)\s*\]\s*->\s*\(\s*\)\s+ON\s*\(([^)]*)\)", text, re.I):
            for prop in re.findall(r"\w+\.(\w+)", m.group(2)):
                if prop not in edge_allowed and prop not in edge_written:
                    err(p, line_of(text, m.group(0), line0), "C18",
                        "关系索引属性 `%s` 既不在契约边字段内、也不在 03 的写入集合内（死索引风险；写入集合=%s）"
                        % (prop, sorted(edge_written)))
        # ★ 自纠（本轮由反例测试发现）：上面这条正则首轮写成 `\s*-\s*\(\s*\)`（漏了 `>`），
        #   导致「关系索引」分支**从未命中**（恒真门禁）——旧索引 `ON (r.probability)` 因此一路 PASS。
        #   现改为 `\s*->\s*\(\s*\)`，并已由 tmp/m2_counterexample2.log 的反例证明其有判别力。
    return cons, idx


def check_topology_file():
    """C10：02-topology 模板条数 + 零实例值。"""
    p = PATHS["02-topology.cypher"]
    node_tpl = 0
    rel_tpl = 0
    for line0, text in STMT[p]:
        for m in re.finditer(r"UNWIND\s+\$rows\s+AS\s+\w+\s+MERGE\s*\(\s*\w+\s*:\s*(CC_\w+)\s*\{\s*ccfId\s*:\s*r\.ccfId\s*\}\s*\)\s*SET\s+\w+\s*\+=\s*r", text, re.I):
            lab = m.group(1)[3:]
            node_tpl += 1
            if lab not in [t["code"] for t in TOPO_TYPES]:
                err(p, line_of(text, m.group(0), line0), "C10", "节点模板标签 %s 不在契约类型内" % m.group(1))
            if lab in ABSTRACT_CODES:
                err(p, line_of(text, m.group(0), line0), "C10", "抽象类型 %s 不应有节点模板（无实例）" % lab)
        for m in re.finditer(r"UNWIND\s+\$rows\s+AS\s+\w+\s+MATCH[^\n;]*?MERGE\s*\(\s*\w+\s*\)\s*-\s*\[\s*\w*\s*:\s*(CC_\w+)\s*\]\s*->", text, re.I):
            rt = m.group(1)[3:]
            rel_tpl += 1
            if rt not in REL_CODES:
                err(p, line_of(text, m.group(0), line0), "C10", "关系模板 %s 不在契约 15 关系内" % m.group(1))
        # 实例值扫描：除 $rows 与模板变量外，不应出现字符串字面量
        if "UNWIND $rows" in text:
            for m in re.finditer(r"'([^']*)'", text):
                err(p, line_of(text, m.group(0), line0), "C10",
                    "模板语句出现字符串字面量 `%s`（§3 铁律：02 不得含实例值）" % m.group(1))
    if node_tpl != 13:
        err(p, 1, "C10", "节点模板应为 13 条（12 具体业务 + Evidence），实得 %d" % node_tpl)
    if rel_tpl != 15:
        err(p, 1, "C10", "关系模板应为 15 条，实得 %d" % rel_tpl)
    return node_tpl, rel_tpl


def check_query_props():
    """C15：消费层查询（Q01–Q08）与规则中引用的 `x.prop` 必须真实存在（T5 静态版）。

    属性空间 = 各类型的契约属性（key_attrs + type-map attrs）+ 该类型的指标 kg_property
               + 映射表 namespaces.kg 的投影属性（ccfId/ccfType/ccfLayer/ccfSnapshotId/ccfRelId）
               + 故障域属性（FaultMode/Metric/Threshold/Meta 的已声明属性）。
    """
    proj_props = set()
    for k, v in NS.items():
        if isinstance(v, str) and ("_property" in k or k.endswith("_id_property")):
            proj_props.add(v)
    proj_props.update({"ccfId", "ccfRelId", "ccfSnapshotId", "ccfType", "ccfLayer"})
    fault_props = set()
    for line, text in COMMENTS[PATHS["03-fault-modes.cypher"]]:
        pass
    for _l, t in STMT[PATHS["03-fault-modes.cypher"]]:
        fault_props.update(re.findall(r"\b\w+\.(fm\w+|metric\w+|thr\w+|cond\w+)\s*=", t))
    fault_props.update({"fmCode", "fmName", "fmLayer", "fmComponentType", "fmSeverity", "fmReversible", "fmChain",
                        "fmDescription", "fmDetectionMode", "fmContractRef", "fmCreatedAt",
                        "metricCode", "metricObject", "metricLayer", "metricUnit", "metricType", "metricSource",
                        "metricKgProperty", "metricThresholdRef", "metricContractRef",
                        "thrCode", "thrMetric", "thrWarning", "thrCritical", "thrRationale", "thrContractRef",
                        "key", "schemaVersion", "ccfVersion", "snapshotId", "contractRef", "createdAt", "updatedAt",
                        "op", "operandKind", "operandValue", "operandValues", "operandRef", "role",
                        "severity", "chain",
                        "description", "provenance", "contractId"})
    files = [PATHS["04-rules.cypher"]] + QUERY_PATHS
    var_lab = {}
    for p in files:
        stmts, _ = cypher_statements(p)
        for line0, text in stmts:
            for m in re.finditer(r"\(\s*(\w+)\s*:\s*(CC_\w+)", text):
                var_lab.setdefault(m.group(1), set()).add(m.group(2)[3:])
            for m in re.finditer(r"\b([A-Za-z_]\w*)\.(\w+)\b", text):
                var, prop = m.group(1), m.group(2)
                if var in ("r", "e") or var in ("n",):
                    continue
                labs = var_lab.get(var)
                if not labs:
                    continue
                ok = prop in proj_props or prop in fault_props
                if not ok:
                    pool = set()
                    for lab in labs:
                        pool |= TYPE_ATTRS.get(lab, set()) | METRICS_BY_OBJECT.get(lab, set())
                    ok = prop in pool
                if not ok:
                    err(p, line_of(text, m.group(0), line0), "C15",
                        "属性 `%s.%s` 不在标签 %s 的属性空间内（T5：消费层属性名必须真实存在）"
                        % (var, prop, sorted(labs)))


# ============================================================================
# 6. 主流程
# ============================================================================

def main():
    try:  # Windows 控制台默认 GBK，显式切 UTF-8（纯标准库）
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    check_labels_reltypes()
    check_fm_ids()
    edges = check_edge_ids()
    check_no_var_length()
    check_cycles(edges)
    check_thresholds()
    check_rules_no_numeric()
    jd_count, _jd = check_judged_by()
    aff = check_affects()
    check_metric_set()
    check_threshold_set()
    check_gap_ledger()
    cons, idx = check_schema()
    n_tpl, r_tpl = check_topology_file()
    check_query_props()

    print("=" * 78)
    print("kg_tbox_check.py — 方案1 原生 TBox 静态校验（只读，零外部依赖）")
    print("仓库根：%s" % ROOT)
    print("-" * 78)
    print("契约自检（解析器可信度）：14 类型 / 15 关系 / 23 故障模式 / 27 因果边 / 13 阈值 / 91 指标  →  PASS")
    print("交付文件：%s" % ", ".join(FILES))
    print("查询文件：%s" % (", ".join(os.path.basename(q) for q in QUERY_PATHS) or "(无)"))
    print("-" * 78)
    print("规模：约束 %d 条 / 索引 %d 条 / 节点模板 %d 条 / 关系模板 %d 条 / JUDGED_BY %d 条 / AFFECTS %d 条"
          % (len(cons), len(idx), n_tpl, r_tpl, jd_count, len(aff)))
    print("-" * 78)
    for w in WARNS:
        print(w)
    for e in ERRORS:
        print(e)
    print("-" * 78)
    print("ERROR = %d   WARN = %d" % (len(ERRORS), len(WARNS)))
    if ERRORS:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
