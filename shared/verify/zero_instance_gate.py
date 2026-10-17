#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shared/verify/zero_instance_gate.py — 门禁 G16：**零实例关系 / 零观测指标**与审计台账的一致性

为什么需要它：CCF 是「采集到什么就写什么」的事实层，沉默了的东西（某条契约关系一条实例都没有、
某个契约指标一个观测都没有）在快照里长得和「本来就不该有」完全一样。本门禁强制：
**在册的零值必须登记在台账里并写明原因与证据**，否则 ERROR；已登记但实际已有实例/观测 ⇒ STALE WARN；
登记为「应当有（回归哨兵）」却归零 ⇒ ERROR。

输入契约（只读）：
  shared/mapping/rel-map.yaml      14 条 asserted 业务关系 + 4 条逆关系 + 1 条派生关系（权威）
  shared/model/metrics.yaml        70 个契约指标（code + object）
  shared/model/v3-ccf-map.yaml     expected_profiles.<profile>.metrics（在册指标集）
  shared/model/zero-instance-ledger.yaml  审计台账（本门禁的判据来源之一）

检查项：
  G16-00 契约自检：asserted/inverse 条数与 rel-map 的 meta.counts 逐项吻合（不吻合即 ERROR）
  G16-01 asserted 业务关系零实例且**不在台账** ⇒ ERROR
  G16-02 逆关系 / 派生关系出现在 CCF relations[] ⇒ ERROR（契约规定它们不进 CCF）
  G16-03 在册指标零观测且**不在台账** ⇒ ERROR
  G16-04 台账条目缺 reason / evidence / consumers / planned_fix / milestone 或 status 非法 ⇒ ERROR
  G16-05 台账里的 code 不在契约码空间内（拼写错误 / 凭空造码）⇒ ERROR
  G16-07 台账登记为零、实际已有实例或观测 ⇒ STALE WARN（好新闻：缺口合上了，台账该更新）
  G16-08 台账登记为「应当有」（expect_in_ccf: nonzero）却为零 ⇒ ERROR（回归哨兵响了）
  G16-09 --expected-profile 在 v3-ccf-map.yaml 里不存在 ⇒ ERROR（fail-closed，不放行）

设计约束（与项目其它门禁一致）：
  · **纯标准库，零外部依赖**（不 import yaml / requests / neo4j）⇒ 自带 YAML 子集解析器。
    该解析器与 shared/verify/kg_tbox_check.py 的同类实现同源但独立（那个模块在 import 期就会跑
    完整套静态检查，不能安全 import）；同一份结构另有 `--selftest-parser` 可与 PyYAML 对拍。
  · **只读**：只读文件，不写任何文件，不连任何服务。
  · 退出码：0 = 无 ERROR；1 = 有 ERROR（WARN 不影响退出码，但逐条打印）。
  · 零硬编码阈值：断言条数取自 rel-map 的 meta.counts，在册指标集取自 v3-ccf-map 的 profile。
"""

import argparse
import io
import json
import os
import re
import sys
from collections import Counter, OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ERRORS = []
WARNS = []


def rel(p):
    try:
        return os.path.relpath(p, ROOT).replace("\\", "/")
    except ValueError:
        return p


# ============================================================================
# 1. YAML 子集解析器（纯标准库；只覆盖本项目契约文件实际用到的语法）
#    支持：块映射 / 块序列 / 行内流式 [a, b] 与 {k: v} / 单双引号标量 / # 注释 / 数字与布尔字面量
# ============================================================================

class YamlError(Exception):
    pass


def _strip_comment(raw):
    """去掉行尾注释（引号内与非空白后的 # 不算注释）。"""
    out, q, i = [], None, 0
    while i < len(raw):
        ch = raw[i]
        if q:
            out.append(ch)
            if q == '"' and ch == "\\" and i + 1 < len(raw):
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == q:
                if q == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _readsym(s, i, stops):
    """读一个标量 token（尊重引号），返回 (token, i)。"""
    if i < len(s) and s[i] in "\"'":
        q = s[i]
        start = i
        i += 1
        while i < len(s):
            if q == '"' and s[i] == "\\":
                i += 2
                continue
            if s[i] == q:
                if q == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
                    continue
                i += 1
                return s[start:i], i
            i += 1
        raise YamlError("引号未闭合: %r" % s[start:])
    start = i
    while i < len(s) and s[i] not in stops:
        i += 1
    return s[start:i].strip(), i


def _skip(s, i):
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _flow(s, i):
    """解析流式 [..] 或 {..}，返回 (值, 下一个位置)。"""
    if s[i] == "[":
        out, i = [], i + 1
        while True:
            i = _skip(s, i)
            if i >= len(s):
                raise YamlError("流式列表未闭合: %r" % s)
            if s[i] == "]":
                return out, i + 1
            val, i = _flowval(s, i)
            out.append(val)
            i = _skip(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
    out, i = OrderedDict(), i + 1
    while True:
        i = _skip(s, i)
        if i >= len(s):
            raise YamlError("流式映射未闭合: %r" % s)
        if s[i] == "}":
            return out, i + 1
        key, i = _readsym(s, i, ":,}]")
        i = _skip(s, i)
        if i < len(s) and s[i] == ":":
            i += 1
        val, i = _flowval(s, i)
        out[_key(key)] = val
        i = _skip(s, i)
        if i < len(s) and s[i] == ",":
            i += 1


def _flowval(s, i):
    i = _skip(s, i)
    if i < len(s) and s[i] in "[{":
        return _flow(s, i)
    tok, i = _readsym(s, i, ",}]")
    return _scalar(tok), i


def _scalar(tok):
    tok = (tok or "").strip()
    if tok == "":
        return None
    if tok[0] == '"' and tok[-1] == '"' and len(tok) >= 2:
        try:
            return json.loads(tok)
        except ValueError:
            return tok[1:-1]
    if tok[0] == "'" and tok[-1] == "'" and len(tok) >= 2:
        return tok[1:-1].replace("''", "'")
    if tok[0] in "[{":
        val, pos = _flow(tok, 0)
        if _skip(tok, pos) != len(tok):
            raise YamlError("流式值后有残留: %r" % tok)
        return val
    low = tok.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    if re.match(r"^[+-]?\d+$", tok):
        return int(tok)
    if re.match(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$", tok) and "." in tok:
        try:
            return float(tok)
        except ValueError:
            return tok
    return tok


def _key(text):
    """键名：引号包裹的键要去引号并还原转义（如 "^cluster\\\\.connection_(total|max)$"）。"""
    k = (text or "").strip()
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "\"'":
        val = _scalar(k)
        return val if isinstance(val, str) else k
    return k


def _split_key(text):
    """把 `key: value` 切成 (key, 有分隔符, value)；引号与流式括号内的 ':' 不算分隔符。"""
    q, depth, i = None, 0, 0
    while i < len(text):
        ch = text[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
            i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] in " \t"):
            return text[:i].strip(), True, text[i + 1:].strip()
        i += 1
    return text, False, ""


_BLOCK_SCALAR = ("|", "|-", "|+", ">", ">-", ">+")


def _take_block_scalar(lines, i, ind, style):
    """折叠/字面块标量：消费缩进更深的后续行（`note: >-` 那种）。"""
    buf = []
    while i < len(lines) and lines[i][0] > ind:
        buf.append(lines[i][1])
        i += 1
    text = ("\n" if style[0] == "|" else " ").join(buf)
    return (text if style.endswith("+") else text.rstrip()), i


def _take_plain_cont(lines, i, ind):
    """普通标量的跨行续接（缩进更深、且本身不是 key: value / 列表项的行）。"""
    buf = []
    while i < len(lines) and lines[i][0] > ind:
        t = lines[i][1]
        if t == "-" or t.startswith("- "):
            break
        if _split_key(t)[1]:
            break
        buf.append(t)
        i += 1
    return buf, i


def _map(lines, i, indent):
    out = OrderedDict()
    while i < len(lines):
        ind, text, ln = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError("第 %d 行缩进 %d 超出期望 %d: %s" % (ln, ind, indent, text))
        if text == "-" or text.startswith("- "):
            break
        key, sep, rest = _split_key(text)
        if not sep:
            raise YamlError("第 %d 行不是 key: value: %s" % (ln, text))
        key = _key(key)
        if key == "<<":
            raise YamlError("第 %d 行使用了锚点合并（本解析器不支持）" % ln)
        i += 1
        if rest == "":
            if i < len(lines) and lines[i][0] > ind:
                out[key], i = _block(lines, i, lines[i][0])
            elif i < len(lines) and lines[i][0] == ind and lines[i][1].startswith("-"):
                out[key], i = _seq(lines, i, ind)
            else:
                out[key] = None
        elif rest in _BLOCK_SCALAR:
            out[key], i = _take_block_scalar(lines, i, ind, rest)
        else:
            extra, i = _take_plain_cont(lines, i, ind)
            out[key] = _scalar(" ".join([rest] + extra) if extra else rest)
    return out, i


def _seq(lines, i, indent):
    out = []
    while i < len(lines):
        ind, text, ln = lines[i]
        if ind < indent:
            break
        if ind != indent or not (text == "-" or text.startswith("- ")):
            break
        item = text[2:].strip() if text.startswith("- ") else ""
        i += 1
        if item == "":
            if i < len(lines) and lines[i][0] > ind:
                val, i = _block(lines, i, lines[i][0])
            else:
                val = None
            out.append(val)
            continue
        key, sep, rest = _split_key(item)
        if not sep:
            out.append(_scalar(item))
            continue
        key = _key(key)
        entry = OrderedDict()
        if rest == "":
            if i < len(lines) and lines[i][0] > ind + 1:
                entry[key], i = _block(lines, i, lines[i][0])
            else:
                entry[key] = None
        elif rest in _BLOCK_SCALAR:
            entry[key], i = _take_block_scalar(lines, i, ind, rest)
        else:
            extra, i = _take_plain_cont(lines, i, ind)
            entry[key] = _scalar(" ".join([rest] + extra) if extra else rest)
        while i < len(lines) and lines[i][0] == ind + 2 and not lines[i][1].startswith("-"):
            ind2, text2, ln2 = lines[i]
            key2, sep2, rest2 = _split_key(text2)
            if not sep2:
                raise YamlError("第 %d 行不是 key: value: %s" % (ln2, text2))
            key2 = _key(key2)
            i += 1
            if rest2 == "":
                if i < len(lines) and lines[i][0] > ind2:
                    entry[key2], i = _block(lines, i, lines[i][0])
                else:
                    entry[key2] = None
            elif rest2 in _BLOCK_SCALAR:
                entry[key2], i = _take_block_scalar(lines, i, ind2, rest2)
            else:
                extra, i = _take_plain_cont(lines, i, ind2)
                entry[key2] = _scalar(" ".join([rest2] + extra) if extra else rest2)
        out.append(entry)
    return out, i


def _block(lines, i, indent):
    if lines[i][1] == "-" or lines[i][1].startswith("- "):
        return _seq(lines, i, indent)
    return _map(lines, i, indent)


def _balance(text):
    """方括号/花括号的净开合数（引号内的括号不计）。"""
    q, bal, i = None, 0, 0
    while i < len(text):
        ch = text[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "\"'":
            q = ch
        elif ch in "[{":
            bal += 1
        elif ch in "]}":
            bal -= 1
        i += 1
    return bal


def _merge_flow(lines):
    """把跨行的流式集合续接成一行（如 `attrs: [a, b,` + 下一行 `c]`）。"""
    out, i = [], 0
    while i < len(lines):
        ind, text, ln = lines[i]
        bal, j = _balance(text), i + 1
        while bal > 0 and j < len(lines):
            text = text + " " + lines[j][1]
            bal += _balance(lines[j][1])
            j += 1
        if bal > 0:
            raise YamlError("第 %d 行起的流式集合未闭合" % ln)
        out.append((ind, text, ln))
        i = j
    return out


def load_yaml(path):
    """载入 YAML 子集；解析失败一律抛 YamlError（绝不静默返回空）。"""
    lines = []
    with io.open(path, "r", encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            text = _strip_comment(raw.rstrip("\n").rstrip("\r"))
            if not text.strip():
                continue
            if text.strip() in ("---", "..."):
                continue
            lines.append((len(text) - len(text.lstrip(" ")), text.strip(), n))
    if not lines:
        return OrderedDict()
    lines = _merge_flow(lines)
    val, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise YamlError("第 %d 行未能被解析（缩进/结构异常）" % lines[i][2])
    return val


# ============================================================================
# 2. 报告通道
# ============================================================================

def emit(level, path, line, code, msg):
    loc = rel(path) + (":%d" % line if line else "")
    text = "%s: %s %s %s" % (loc, level, code, msg)
    print(text)
    (ERRORS if level == "ERROR" else WARNS).append(text)


def _find_line(text_lines, needle):
    for n, ln in enumerate(text_lines, 1):
        if re.search(r"(^|\s)%s:\s" % re.escape(needle), ln) or (": %s" % needle) in ln:
            return n
    return 0


# ============================================================================
# 3. 自检：解析器与 PyYAML 对拍（可选，仅 --selftest-parser）
# ============================================================================

def selftest_parser(paths):
    try:
        import yaml  # 仅自检时用；门禁本体不依赖它
    except ImportError:
        print("SELFTEST：本机无 PyYAML，跳过对拍（门禁本体不需要它）")
        return 0
    bad = 0
    for p in paths:
        mine = load_yaml(p)
        ref = yaml.safe_load(io.open(p, encoding="utf-8"))
        if not isinstance(ref, dict) or keydiff(mine, ref):
            print("SELFTEST FAIL: %s" % rel(p))
            bad += 1
        else:
            print("SELFTEST OK  : %s  (%d 个顶层键)" % (rel(p), len(ref)))
    return 1 if bad else 0


def keydiff(a, b, path=""):
    """比较结构（只比顶层两层的键/标量列表），返回不一致的描述列表。"""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append("%s/%s 缺失" % (path, k))
            else:
                out.extend(keydiff(a[k], b[k], path + "/" + str(k)))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s 长度 %d != %d" % (path, len(a), len(b)))
            return out
        for n, (x, y) in enumerate(zip(a, b)):
            out.extend(keydiff(x, y, "%s[%d]" % (path, n)))
        return out
    if isinstance(a, dict) or isinstance(b, dict):
        return ["%s 类型不一致" % path]
    if a != b:
        out.append("%s 值 %r != %r" % (path, a, b))
    return out


# ============================================================================
# 4. 主流程
# ============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description="G16 零实例/零观测缺口台账一致性门禁（只读、纯标准库）")
    ap.add_argument("--ccf", required=True, help="待检的 CCF 快照 JSON")
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"))
    ap.add_argument("--ledger", default=os.path.join(ROOT, "shared", "model", "zero-instance-ledger.yaml"))
    ap.add_argument("--expected-profile", default="l1-l3",
                    help="在册指标集取自 v3-ccf-map.yaml 的 expected_profiles.<profile>（真机用 l1-l3）")
    ap.add_argument("--quiet", action="store_true", help="只打印 ERROR/WARN，不打印统计")
    ap.add_argument("--selftest-parser", action="store_true",
                    help="把自带 YAML 子集解析器与 PyYAML 对拍后退出（可选，门禁本体不需要 PyYAML）")
    args = ap.parse_args(argv)

    relmap_p = os.path.join(args.mapping_dir, "rel-map.yaml")
    metrics_p = os.path.join(args.model_dir, "metrics.yaml")
    v3map_p = os.path.join(args.model_dir, "v3-ccf-map.yaml")

    if args.selftest_parser:
        return selftest_parser([relmap_p, metrics_p, v3map_p, args.ledger])

    try:
        relmap = load_yaml(relmap_p)
        metrics = load_yaml(metrics_p)
        v3map = load_yaml(v3map_p)
        ledger = load_yaml(args.ledger)
    except (YamlError, OSError) as exc:
        print("G16-PARSE-ERROR: %s" % exc)
        return 1
    try:
        with io.open(args.ccf, "r", encoding="utf-8") as fh:
            ccf = json.load(fh)
    except (OSError, ValueError) as exc:
        print("G16-CCF-ERROR: 无法读取 %s：%s" % (rel(args.ccf), exc))
        return 1

    ledger_lines = io.open(args.ledger, encoding="utf-8").read().splitlines()

    # ---- 契约侧：三类关系分型 ------------------------------------------------
    rels = relmap.get("relations") or {}
    invs = relmap.get("inverse_relations") or {}
    asserted = [c for c, v in rels.items() if isinstance(v, dict) and v.get("asserted") is True]
    inverse = [c for c, v in rels.items() if not (isinstance(v, dict) and v.get("asserted") is True)]
    inverse += [c for c in invs]
    derived = [c for c in inverse
               if (rels.get(c) or {}).get("derived_from") or (invs.get(c) or {}).get("derived_from")]
    inverse = [c for c in inverse if c not in derived]
    counts = (relmap.get("meta") or {}).get("counts") or {}
    if counts.get("asserted_kg") is not None and int(counts["asserted_kg"]) != len(asserted):
        emit("ERROR", relmap_p, 0, "G16-00",
             "契约自检失败：rel-map.yaml 的 asserted 关系实测 %d 条 != meta.counts.asserted_kg=%s"
             % (len(asserted), counts.get("asserted_kg")))
    if counts.get("inverse_relations") is not None and int(counts["inverse_relations"]) != len(invs):
        emit("ERROR", relmap_p, 0, "G16-00",
             "契约自检失败：inverse_relations 实测 %d 条 != meta.counts.inverse_relations=%s"
             % (len(invs), counts.get("inverse_relations")))

    # ---- CCF 侧：实测计数 ----------------------------------------------------
    rel_cnt = Counter(r.get("type") for r in (ccf.get("relations") or []))
    obs_status = {}
    for o in (ccf.get("observations") or []):
        obs_status.setdefault(o.get("metricCode"), set()).add(o.get("status"))
    observed = {c for c, st in obs_status.items() if "collected" in st}
    types_present = Counter(o.get("type") for o in (ccf.get("objects") or []))

    # ---- 台账侧 --------------------------------------------------------------
    led_rel = OrderedDict((e.get("code"), e) for e in (ledger.get("relations") or []) if isinstance(e, dict))
    led_met = OrderedDict((e.get("code"), e) for e in (ledger.get("metrics") or []) if isinstance(e, dict))
    valid_status = {"planned_fix", "environment_limited", "not_applicable"}
    for kind, entries in (("relations", led_rel), ("metrics", led_met)):
        for code, e in entries.items():
            ln = _find_line(ledger_lines, str(code))
            for field in ("reason", "evidence", "consumers", "planned_fix", "milestone"):
                if not e.get(field):
                    emit("ERROR", args.ledger, ln, "G16-04",
                         "%s 条目 %s 缺 %s" % (kind[:-1], code, field))
            if e.get("status") not in valid_status:
                emit("ERROR", args.ledger, ln, "G16-04",
                     "%s 条目 %s 的 status=%r 非法（应为 %s）"
                     % (kind[:-1], code, e.get("status"), "|".join(sorted(valid_status))))
            if e.get("expect_in_ccf") not in ("zero", "nonzero"):
                emit("ERROR", args.ledger, ln, "G16-04",
                     "%s 条目 %s 的 expect_in_ccf=%r 非法（应为 zero|nonzero）"
                     % (kind[:-1], code, e.get("expect_in_ccf")))
            if kind == "relations" and code not in asserted + inverse + derived:
                emit("ERROR", args.ledger, ln, "G16-05",
                     "台账登记的关系码 %s 不在 rel-map.yaml 的码空间内" % code)
            if kind == "metrics" and code not in {m.get("code") for m in (metrics.get("metrics") or [])}:
                emit("ERROR", args.ledger, ln, "G16-05",
                     "台账登记的指标码 %s 不在 metrics.yaml 的 70 个码内" % code)

    # ---- 检查 1：asserted 零实例必须登记 ------------------------------------
    zero_unregistered_rel = []
    for code in sorted(asserted):
        n = rel_cnt.get(code, 0)
        if n == 0 and code not in led_rel:
            zero_unregistered_rel.append(code)
            emit("ERROR", args.ccf, 0, "G16-01",
                 "asserted 业务关系 %s 零实例且未登记在台账（%s）" % (code, rel(args.ledger)))

    # ---- 检查 2：逆关系 / 派生关系不得出现在 CCF relations[] ----------------
    for code in sorted(inverse + derived):
        if rel_cnt.get(code, 0) > 0:
            emit("ERROR", args.ccf, 0, "G16-02",
                 "%s 是%s关系，按 rel-map.yaml 不得出现在 CCF relations[]（实测 %d 条）"
                 % (code, "派生" if code in derived else "逆", rel_cnt[code]))

    # ---- 检查 3：在册指标零观测必须登记 ------------------------------------
    prof_name = args.expected_profile
    prof = (v3map.get("expected_profiles") or {}).get(prof_name)
    if not prof:
        emit("ERROR", v3map_p, 0, "G16-09",
             "v3-ccf-map.yaml 没有 expected_profile=%r（fail-closed：不放行）" % prof_name)
        prof = {}
    in_register = set(prof.get("metrics") or [])
    for m in (metrics.get("metrics") or []):
        if m.get("object") in types_present:          # 该类型的对象在快照里存在 ⇒ 其指标在册
            in_register.add(m.get("code"))
    zero_unregistered_met = []
    for code in sorted(in_register - observed):
        if code not in led_met:
            zero_unregistered_met.append(code)
            emit("ERROR", args.ccf, 0, "G16-03",
                 "在册指标 %s（profile=%s）零观测且未登记在台账（%s）"
                 % (code, prof_name, rel(args.ledger)))

    # ---- 检查 4：STALE（已登记但实际有实例/观测）与回归哨兵 ------------------
    for code, e in led_rel.items():
        ln = _find_line(ledger_lines, str(code))
        n = rel_cnt.get(code, 0)
        if e.get("expect_in_ccf") == "zero" and n > 0:
            emit("WARN", args.ledger, ln, "G16-07",
                 "STALE：台账登记 %s 为零实例，实测已有 %d 条 ⇒ 缺口已合上，请更新台账" % (code, n))
        if e.get("expect_in_ccf") == "nonzero" and n == 0:
            emit("ERROR", args.ledger, ln, "G16-08",
                 "回归哨兵：台账登记 %s 应当有实例（expect_in_ccf: nonzero），实测为 0 条" % code)
    for code, e in led_met.items():
        ln = _find_line(ledger_lines, str(code))
        got = code in observed
        if e.get("expect_in_ccf") == "zero" and got:
            emit("WARN", args.ledger, ln, "G16-07",
                 "STALE：台账登记 %s 为零观测，实测已有 collected 观测 ⇒ 缺口已合上，请更新台账" % code)
        if e.get("expect_in_ccf") == "nonzero" and not got:
            emit("ERROR", args.ledger, ln, "G16-08",
                 "回归哨兵：台账登记 %s 应当有观测（expect_in_ccf: nonzero），实测零观测" % code)

    # ---- 统计（三类关系分型 + 指标覆盖） ------------------------------------
    if not args.quiet:
        with_rel = [c for c in sorted(asserted) if rel_cnt.get(c, 0) > 0]
        zero_reg = [c for c in sorted(asserted) if rel_cnt.get(c, 0) == 0 and c in led_rel]
        print()
        print("—— G16 统计（%s，profile=%s）——" % (rel(args.ccf), prof_name))
        print("a) asserted 有实例      : %d/%d  %s" % (
            len(with_rel), len(asserted),
            ", ".join("%s=%d" % (c, rel_cnt[c]) for c in with_rel)))
        print("b) asserted 零实例已登记: %d  %s" % (
            len(zero_reg), ", ".join("%s(%s)" % (c, led_rel[c].get("status")) for c in zero_reg)))
        print("c) asserted 零实例未登记: %d  %s" % (len(zero_unregistered_rel),
                                              ", ".join(zero_unregistered_rel) or "-"))
        print("d) 逆关系（不进 CCF）   : %d  %s（CCF 实测 %s）" % (
            len(inverse), ", ".join(sorted(inverse)),
            ", ".join("%s=%d" % (c, rel_cnt[c]) for c in sorted(inverse)) or "0 条"))
        print("e) 派生关系（不进 CCF） : %d  %s（CCF 实测 %s）" % (
            len(derived), ", ".join(sorted(derived)),
            ", ".join("%s=%d" % (c, rel_cnt[c]) for c in sorted(derived)) or "0 条"))
        print("f) 在册指标             : %d 个（其中零观测 %d，已登记 %d，未登记 %s）" % (
            len(in_register), len(in_register - observed),
            len([c for c in sorted(in_register - observed) if c in led_met]),
            ", ".join(zero_unregistered_met) or "-"))
        print("g) 台账规模             : relations %d 条 / metrics %d 条" % (len(led_rel), len(led_met)))

    print()
    print("G16 %s：ERROR %d 条 / WARN %d 条" % ("PASS" if not ERRORS else "FAIL", len(ERRORS), len(WARNS)))
    return 0 if not ERRORS else 1


if __name__ == "__main__":
    sys.exit(main())
