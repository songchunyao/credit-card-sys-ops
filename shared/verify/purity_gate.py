#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""G25 · 两实现纯粹性门禁（裁决 15；用户原则 m11121）

本门禁把「方案1（Neo4j / LPG 载体）与方案2（GraphDB / RDF-OWL 载体）两实现
故意并存、且两种实现必须保持纯粹」这条总原则，落成**声明驱动 + 机检**的六个面
（P1 依赖面 / P2 运行期端点面 / P3 语义面 / P4 词表面 / P5 共享面中性 / P6 测量面）。

★ 唯一权威是契约 `shared/model/purity-contract.yaml`：本脚本不硬编码任何载体名、
  端口、文件清单或豁免 —— 契约说什么就检什么，契约缺失/引用不上即 BLOCKED。

退出码（沿用本仓既有门禁的三态风格）：
    0 = PASS      六个面全部通过（豁免全部在盘上复现、无未声明命中）
    1 = FAIL      有非豁免命中 / 未声明命中 / 陈旧豁免 / 豁免超上限 / 声明与实现矛盾
    2 = BLOCKED   契约缺失或引用不上，本次**没有观测**（BLOCKED ≠ FAIL，也 ≠ PASS）

★ 本门禁**不设** `CARRIER_SPECIFIC` 第三态：`guard_conformance.py` 的该态表达的是
  「已登记的载体能力边界」，而纯粹性没有「载体特有而不算偏离」的中间态 ——
  一切命中要么被登记为中性（PASS），要么就是污染（FAIL）。

★ 诚实边界：本门禁**不**证明两侧行为/语义等价，也**不**证明效果差异；
  它只机械地证明「两实现的载体机制没有互相借用」。P2 的运行期断言是对**已留档的
  只读拨号见证**（`ss -tnp` 采样）做重放校验，不在门禁内重新拨号。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ★ stdout/stderr 编码加固（照抄本仓既有门禁范式：guard_conformance.py:72-75 /
#   ref_gate.py:80 / f5_kgt_gate.py:85-86 / ccf_instance_gate.py:77-80）。
#   宿主默认代码页是 GBK，而本门禁会打印 `↔`、`★`、`⇒`、`—` 等非 GBK 字形；
#   不加固时 GBK 控制台抛
#   `UnicodeEncodeError: 'gbk' codec can't encode character '\u2194' in position …`
#   并以 exit 1 **假失败**（本仓历史上专门有过一次「Fix gate stdout encoding robustness」
#   施工轮，此处沿用同一范式）。
#   选型理由：①「同一文件、同样字节，编码不同则结论不同」是不可接受的判别力缺陷，
#   本仓既有加固一律用 `reconfigure(encoding="utf-8", errors="replace")`，
#   与兄弟门禁保持一致；②不用「改写掉非 GBK 字形」的替代法 —— 那样会把
#   `↔`（互斥配对记法）等语义抹平，且掩盖读文件时 `errors="replace"` 产生的
#   `U+FFFD` 这一真实观测；③`errors="replace"` 保证即便流不可重配也不因编码崩掉，
#   `hasattr` 守卫覆盖 pytest / IDE 等非标准流。stderr 同样处理 ⇒ 常态 stderr 0 B。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2

CONTRACT_REL = "shared/model/purity-contract.yaml"
GATE_REL = "shared/verify/purity_gate.py"

REQUIRED_TOP = ("meta", "vocab", "classify", "allowlist", "faces")
REQUIRED_FACES = ("P1", "P2", "P3", "P4", "P5", "P6")


class Blocked(Exception):
    """契约缺失 / 引用不上 ⇒ 本次没有观测（退出码 2）。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def count_lf(data: bytes) -> int:
    return data.count(b"\n")


class Finding(object):
    __slots__ = ("face", "rule", "path", "line", "token", "cls", "detail")

    def __init__(self, face, rule, path, line, token="", cls="", detail=""):
        self.face = face
        self.rule = rule
        self.path = path
        self.line = line
        self.token = token
        self.cls = cls
        self.detail = detail

    def as_dict(self):
        return {
            "face": self.face,
            "rule": self.rule,
            "path": self.path,
            "line": self.line,
            "token": self.token,
            "class": self.cls,
            "detail": self.detail,
        }

    def render(self):
        loc = self.path if not self.line else "%s:%d" % (self.path, self.line)
        tok = (" token=%r" % self.token) if self.token else ""
        cls = (" class=%s" % self.cls) if self.cls else ""
        return "[%s] %s  %s%s%s  %s" % (self.face, self.rule, loc, tok, cls, self.detail)


class FaceResult(object):
    def __init__(self, face_id, name, tier):
        self.face = face_id
        self.name = name
        self.tier = tier
        self.verdict = "PASS"
        self.findings = []
        self.stats = {}
        self.notes = []

    def fail(self, rule, path, line=0, token="", cls="", detail=""):
        self.findings.append(Finding(self.face, rule, path, line, token, cls, detail))
        self.verdict = "FAIL"

    def blocked(self, rule, detail):
        raise Blocked("[%s] %s: %s" % (self.face, rule, detail))

    def note(self, text):
        self.notes.append(text)

    def as_dict(self):
        return {
            "face": self.face,
            "name": self.name,
            "tier": self.tier,
            "verdict": self.verdict,
            "stats": self.stats,
            "notes": self.notes,
            "findings": [f.as_dict() for f in self.findings],
        }


# --------------------------------------------------------------------------
# 注释 / 字符串区段识别（分类器的地基）
# --------------------------------------------------------------------------

def scan_py_line(line, state):
    """返回 (code_end, kind, new_state)。kind ∈ {hash, docstring}。"""
    n = len(line)
    ds = state.get("ds")
    if ds:
        j = line.find(ds)
        if j < 0:
            return 0, "docstring", state
        st = dict(state)
        st["ds"] = None
        rest = j + len(ds)
        k = line.find("#", rest)
        return (k if k >= 0 else n), "docstring", st
    i = 0
    while i < n:
        ch = line[i]
        if ch == "#":
            return i, "hash", state
        if line.startswith('"""', i) or line.startswith("'''", i):
            d = line[i:i + 3]
            j = line.find(d, i + 3)
            if j < 0:
                st = dict(state)
                st["ds"] = d
                return i, "docstring", st
            i = j + 3
            continue
        if ch in "\"'":
            q = ch
            j = i + 1
            closed = False
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == q:
                    closed = True
                    break
                j += 1
            if not closed:
                return n, "hash", state
            i = j + 1
            continue
        i += 1
    return n, "hash", state


def line_code_end(line, ext, leaders, py_state, triple_exts=(".py",)):
    """返回 (code_end, kind, new_state)。kind ∈ {hash, slash, docstring}。

    new_state 必须由调用方在逐行循环里回填，否则 .py 的多行三引号区段
    只在首行被识别、后续行会被误判成代码（假 FAIL）。
    """
    if ext in triple_exts:
        return scan_py_line(line, py_state)
    for ld in leaders:
        if ld == "//":
            k = line.find("//")
        else:
            k = line.find(ld)
        if k >= 0:
            return k, ("slash" if ld == "//" else "hash"), py_state
    return len(line), "hash", py_state


def comment_strings(contract):
    """把 classify.comment_leaders 归一化成 {ext: [leader, ...]}（YAML 允许标量或列表）。"""
    out = {}
    raw = contract["classify"].get("comment_leaders") or {}
    for ext, v in raw.items():
        if isinstance(v, str):
            out[ext.lower()] = [v]
        else:
            out[ext.lower()] = list(v or [])
    return out


def triple_quote_exts(contract):
    return tuple(x.lower() for x in (contract["classify"].get("triple_quote_exts") or [".py"]))


def code_only_text(path: Path, ext: str, contract) -> str:
    """把注释/三引号区段替换为空格，只留「代码头」。"""
    leaders = comment_strings(contract)
    code_exts = set(x.lower() for x in (contract["classify"].get("code_only_exts") or []))
    text = read_text(path)
    if ext not in code_exts:
        return text
    state = {}
    tq = triple_quote_exts(contract)
    out = []
    for line in text.splitlines():
        end, _kind, state = line_code_end(line, ext, leaders.get(ext, []), state, tq)
        out.append(line[:end] + " " * max(0, len(line) - end))
    if text.endswith("\n"):
        out.append("")
    return "\n".join(out)


def comment_stripped_text(path: Path, ext: str, contract) -> str:
    """按 comment_leaders 去掉注释（逐文件状态机），用于「只许看真实公理」的判据。

    与 code_only_text 的差别：不要求 ext ∈ classify.code_only_exts，
    只要该扩展名声明了注释前缀就剥掉注释（用于 .ttl 形状文件的纯度判据）。
    """
    leaders = comment_strings(contract)
    tq = triple_quote_exts(contract)
    text = read_text(path)
    state = {}
    out = []
    for line in text.splitlines():
        end, _kind, state = line_code_end(line, ext, leaders.get(ext, []), state, tq)
        out.append(line[:end])
    return "\n".join(out)


# --------------------------------------------------------------------------
# 契约装载
# --------------------------------------------------------------------------

def load_contract(root: Path, contract_arg) -> tuple:
    path = Path(contract_arg) if contract_arg else (root / CONTRACT_REL)
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        raise Blocked("契约不存在: %s（本门禁是**声明驱动**的：没有契约就没有观测）" % path)
    try:
        import yaml  # noqa
    except Exception as exc:  # pragma: no cover
        raise Blocked("PyYAML 不可用（%s）⇒ 无法读取契约" % exc)
    import yaml
    try:
        data = yaml.safe_load(read_text(path))
    except Exception as exc:
        raise Blocked("契约无法解析: %s（%s）" % (path, exc))
    if not isinstance(data, dict):
        raise Blocked("契约根不是映射: %s" % path)
    for key in REQUIRED_TOP:
        if key not in data:
            raise Blocked("契约缺字段: %s" % key)
    if not isinstance(data.get("faces"), dict):
        raise Blocked("契约 faces 不是映射")
    for fid in REQUIRED_FACES:
        if fid not in data["faces"]:
            raise Blocked("契约缺面: faces.%s" % fid)
    for fid in REQUIRED_FACES:
        face = data["faces"][fid]
        if not isinstance(face, dict) or not face.get("id") or not face.get("name"):
            raise Blocked("契约 faces.%s 缺 id/name" % fid)
    al = data.get("allowlist")
    if not isinstance(al, dict) or "max_allowlist_entries" not in al:
        raise Blocked("契约 allowlist 缺 max_allowlist_entries")
    if not isinstance(al.get("entries"), list):
        raise Blocked("契约 allowlist.entries 不是列表")
    if int(al["max_allowlist_entries"]) < len(al["entries"]):
        pass  # 交由 P1 之外的统一检查报 FAIL（这是污染不是缺观测）
    return path, data


# --------------------------------------------------------------------------
# token 扫描（P1 / P5 共用）
# --------------------------------------------------------------------------

def compile_patterns(patterns):
    out = []
    for p in patterns or []:
        try:
            out.append((p, re.compile(p, re.I)))
        except re.error as exc:
            raise Blocked("契约里的正则非法: %r（%s）" % (p, exc))
    return out


def classify_hit(relpath, ext, line, pos, contract, end=None, kind=None):
    """把一个 token 命中分类。返回 class 名。

    end/kind 由调用方逐行传入（同一个文件的逐行状态机），若缺省则本函数
    自算（单行语义，仅用于独立调用/自检）。
    """
    classify = contract["classify"]
    prose_exts = set(x.lower() for x in (classify.get("prose_exts") or []))
    if ext in prose_exts:
        return "prose"
    for _p, rx in compile_patterns(classify.get("scanner_patterns")):
        if rx.search(line):
            return "scanner"
    if end is None:
        leaders = comment_strings(contract)
        end, kind, _state = line_code_end(line, ext, leaders.get(ext, []), {}, triple_quote_exts(contract))
    if pos >= end:
        for _p, rx in compile_patterns(classify.get("doc_ref_patterns")):
            if rx.search(line):
                return "doc_ref"
        for _p, rx in compile_patterns(classify.get("declaration_patterns")):
            if rx.search(line):
                return "declaration"
        return "docstring" if kind == "docstring" else "comment"
    return "code"


def iter_file_hits(root: Path, rel_root: str, exts, pattern_sets, contract, skip_files):
    """逐文件逐行产出 (relpath, line_no, token, pos, ext, line, cls)。

    ★ 每个文件维护一份逐行状态机（.py 三引号），同一行的 code_end/kind
    既用于定位命中、也用于分类 ⇒ 分类与"代码头"判定不可能互相漂移。
    """
    base = root / rel_root
    if not base.exists():
        raise Blocked("扫描根不存在: %s" % rel_root)
    leaders = comment_strings(contract)
    tq = triple_quote_exts(contract)
    for fp in sorted(base.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root).as_posix()
        if rel in skip_files:
            continue
        ext = fp.suffix.lower()
        if ext not in exts:
            continue
        text = read_text(fp)
        state = {}
        for line_no, line in enumerate(text.splitlines(), 1):
            end, kind, state = line_code_end(line, ext, leaders.get(ext, []), state, tq)
            seen = set()
            for _p, rx in pattern_sets:
                for m in rx.finditer(line):
                    key = (m.start(), m.end())
                    if key in seen:
                        continue
                    seen.add(key)
                    cls = classify_hit(rel, ext, line, m.start(), contract, end=end, kind=kind)
                    yield rel, line_no, m.group(0), m.start(), ext, line, cls


def build_allowlist_index(entries, face):
    """→ (line_level, path_level)；每一项都带 count=0。"""
    line_level = {}
    path_level = {}
    for e in entries:
        if e.get("face") != face:
            continue
        # ★ 必须在**原始条目**上写 count：陈旧豁免检查读的就是 contract 里这一批 dict，
        # 若在此处 dict(e) 复制，count 只落在副本上 ⇒ check_allowlist_stale 永远看不见 0（假 PASS）。
        e["count"] = 0
        e.pop("_over", None)
        ln = int(e.get("line") or 0)
        key = (e.get("scan"), e.get("path"), e.get("token"))
        if ln:
            line_level[(e.get("scan"), e.get("path"), ln, e.get("token"))] = e
        else:
            # 路径级条目按 (scan, path, class) 归并：同一个文件允许同时有
            # code/comment/docstring 多种分类，各占一条（与 --emit-inventory 一一对应）
            path_level[(e.get("scan"), e.get("path"), e.get("class"))] = e
    return line_level, path_level


def attribute_hit(res, entry_index, scan_id, rel, line_no, token, cls):
    line_level, path_level = entry_index
    entry = line_level.get((scan_id, rel, line_no, token))
    if entry is None:
        # token 省略时按 (scan,path,line) 兜底
        for (s, p, ln, tk), cand in line_level.items():
            if s == scan_id and p == rel and ln == line_no and tk is None:
                entry = cand
                break
    if entry is None:
        entry = path_level.get((scan_id, rel, cls))
    if entry is None:
        res.fail("P1-undeclared-hit" if res.face == "P1" else "P5-undeclared-hit",
                 rel, line_no, token, cls, "命中未在契约 allowlist 登记（禁止无声漂移）")
        return
    want = entry.get("class")
    if want and want != cls:
        res.fail("P1-class-mismatch" if res.face == "P1" else "P5-class-mismatch",
                 rel, line_no, token, cls,
                 "契约登记 class=%s，实测 class=%s（分类与登记不符）" % (want, cls))
    entry["count"] += 1
    cap = int(entry.get("max_hits") or 0)
    if cap and entry["count"] > cap and not entry.get("_over"):
        entry["_over"] = True
        res.fail("P1-allowlist-over-max" if res.face == "P1" else "P5-allowlist-over-max",
                 rel, line_no, token, cls,
                 "命中数 %d 超过登记 max_hits=%d（豁免不得无声扩张）" % (entry["count"], cap))


def check_allowlist_hygiene(res, entries, face, max_entries):
    mine = [e for e in entries if e.get("face") == face]
    for e in mine:
        for key in ("scan", "path", "class", "reason"):
            if not e.get(key):
                res.blocked("contract-entry-incomplete",
                            "allowlist 条目缺 %s：%r" % (key, e))


def check_allowlist_stale(res, entries):
    """陈旧豁免 = 盘上已不复现的登记。必须在**所有面跑完之后**统一检查一次。"""
    seen = set()
    for e in entries:
        key = (e.get("face"), e.get("scan"), e.get("path"), int(e.get("line") or 0),
               e.get("token") or "", e.get("class") or "")
        if key in seen:
            continue
        seen.add(key)
        if int(e.get("count") or 0) == 0 and e.get("path"):
            res.findings.append(Finding(
                res.face, "allowlist-stale", e["path"], int(e.get("line") or 0),
                e.get("token") or "", e.get("class") or "",
                "陈旧豁免（face=%s scan=%s）：该条目在盘上已不复现，须删除或修正"
                % (e.get("face"), e.get("scan"))))
            res.verdict = "FAIL"


# --------------------------------------------------------------------------
# P1 依赖面
# --------------------------------------------------------------------------

def check_p1(root, contract, res):
    classify = contract["classify"]
    scan_exts = set(x.lower() for x in (classify.get("scan_exts") or []))
    allowed = set(classify.get("allowed_classes") or [])
    entries = contract["allowlist"]["entries"]
    index = build_allowlist_index(entries, "P1")
    total = 0
    by_class = {}
    for scan in contract["faces"]["P1"]["scans"]:
        sid = scan["id"]
        pats = compile_patterns(contract["vocab"][scan["pattern_set"]])
        exts = set(x.lower() for x in (scan.get("exts") or scan_exts))
        skip = set(scan.get("skip_files") or [])
        for rel, line_no, token, pos, ext, line, cls in iter_file_hits(
                root, scan["root"], exts, pats, contract, skip):
            total += 1
            by_class[cls] = by_class.get(cls, 0) + 1
            if cls not in allowed:
                res.fail("P1-non-exempt-hit", rel, line_no, token, cls,
                         "非豁免类命中（%s ∉ allowed_classes）" % cls)
                continue
            attribute_hit(res, index, sid, rel, line_no, token, cls)
    res.stats["hits_total"] = total
    res.stats["by_class"] = by_class
    res.stats["scans"] = [s["id"] for s in contract["faces"]["P1"]["scans"]]
    res.note("判据 = 逐文件 token 扫 + 分类；非豁免类命中即 FAIL（零豁免漂移）")


# --------------------------------------------------------------------------
# P2 运行期端点面
# --------------------------------------------------------------------------

COUNTER_LINE_RE = re.compile(r"pid=(\d+)\s+to_7200=(\d+)\s+to_7475=(\d+)\s+to_7688=(\d+)")


def check_p2(root, contract, res):
    face = contract["faces"]["P2"]
    # --- 静态：环境变量名集合互斥 + 端点常量互斥 ---
    for rule in face.get("env_exclusivity") or []:
        p = root / rule["file"]
        if not p.exists():
            res.blocked("P2-file-missing", "声明的文件不存在: %s" % rule["file"])
        ext = p.suffix.lower()
        code = code_only_text(p, ext, contract)
        for token in rule.get("must_contain") or []:
            if token not in code:
                res.blocked("P2-declared-token-absent",
                            "%s 的**代码头**里没有声明的 %r（契约与实现脱节，本次没有观测）"
                            % (rule["file"], token))
        for token in rule.get("must_not_contain_in_code") or []:
            if token in code:
                res.fail("P2-env-leak", rule["file"], 0, token, "code",
                         "本侧代码头出现对侧载体的环境变量名 ⇒ 运行期配置面不互斥")
    for rule in face.get("endpoint_constants") or []:
        p = root / rule["file"]
        if not p.exists():
            res.blocked("P2-file-missing", "声明的文件不存在: %s" % rule["file"])
        code = code_only_text(p, p.suffix.lower(), contract)
        for token in rule.get("expect") or []:
            if token not in code:
                res.blocked("P2-declared-endpoint-absent",
                            "%s 的代码头里没有声明的 %s（契约与实现脱节）"
                            % (rule["file"], token))
        for token in rule.get("forbid") or []:
            if token in code:
                res.fail("P2-endpoint-leak", rule["file"], 0, token, "code",
                         "本侧代码头出现对侧载体的端点常量 ⇒ 运行期端点面不互斥")

    # --- 运行期：对已留档的只读拨号见证做重放校验 ---
    ev = face.get("evidence") or {}
    files = ev.get("files") or []
    if not files:
        res.blocked("P2-no-evidence", "契约未登记任何拨号见证文件 ⇒ 运行期面没有观测")
    lines = []
    freshness = []
    for rel in files:
        p = root / rel
        if not p.exists():
            res.blocked("P2-evidence-missing",
                        "拨号见证证据缺失: %s（缺真机只读采样留档，本次运行期面没有观测）" % rel)
        data = p.read_bytes()
        if not data.strip():
            res.blocked("P2-evidence-empty", "拨号见证证据为空: %s" % rel)
        age_days = (time.time() - p.stat().st_mtime) / 86400.0
        freshness.append((rel, age_days, count_lf(data)))
        text = read_text(p)
        for m in COUNTER_LINE_RE.finditer(text):
            lines.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))))
    max_age = float(ev.get("max_age_days") or 0)
    if max_age > 0:
        worst = max(a for _r, a, _l in freshness)
        if worst > max_age:
            res.blocked("P2-evidence-stale",
                        "拨号见证最新时点距今 %.1f 天 > 契约 max_age_days=%s ⇒ 运行期面读数是旧观测，"
                        "须重新采集后再判" % (worst, ev.get("max_age_days")))
    if not lines:
        res.blocked("P2-evidence-unparsable",
                    "证据文件里没有可解析的 per-service 计数行（pid=… to_7200=… to_7475=… to_7688=…）")
    res.stats["evidence_lines"] = len(lines)
    res.stats["freshness_days"] = {"%s" % r: round(a, 3) for r, a, _l in freshness}
    matrix = {}
    for pid, c7200, c7475, c7688 in lines:
        slot = matrix.setdefault(pid, {"to_7200": 0, "to_7475": 0, "to_7688": 0})
        slot["to_7200"] = max(slot["to_7200"], c7200)
        slot["to_7475"] = max(slot["to_7475"], c7475)
        slot["to_7688"] = max(slot["to_7688"], c7688)
    res.stats["dial_matrix"] = {"%d" % k: v for k, v in sorted(matrix.items())}
    for exp in ev.get("expectations") or []:
        pid = int(exp["pid"])
        row = matrix.get(pid)
        if row is None:
            res.blocked("P2-pid-not-observed",
                        "声明的 pid %d（%s）在证据里没有任何计数行 ⇒ 该进程本次没有观测"
                        % (pid, exp.get("arm", "?")))
        own = exp.get("own_port")
        min_own = int(exp.get("min_own") or 1)
        key = "to_%s" % own
        if row.get(key, 0) < min_own:
            res.fail("P2-own-carrier-not-observed", "%s" % (exp.get("evidence", "dial-witness")),
                     0, "%s" % own, "", "%s(pid %d) 对本载体 %s 的拨号计数 %d < min_own %d"
                     % (exp.get("arm", "?"), pid, own, row.get(key, 0), min_own))
        for forbid in exp.get("forbid_ports") or []:
            fk = "to_%s" % forbid
            if row.get(fk, 0) > 0:
                res.fail("P2-forbidden-dial", "dial-witness", 0, "%s" % forbid, "",
                         "%s(pid %d) 出现对**对侧载体** %s 的活动连接（计数 %d）⇒ 运行期端点面不互斥"
                         % (exp.get("arm", "?"), pid, forbid, row[fk]))
    res.note("运行期面 = 对只读拨号见证（ss -tnp 采样）的**重放校验**；门禁自身不拨号、不连真机")
    res.note("静态面 = 环境变量名集合互斥 + 端点常量互斥（只看代码头，注释/docstring 不计）")


# --------------------------------------------------------------------------
# P3 语义面
# --------------------------------------------------------------------------

def count_occurrences(root, roots, exts, patterns):
    counts = {}
    for rel_root in roots:
        base = root / rel_root
        if not base.exists():
            raise Blocked("P3 声明根不存在: %s" % rel_root)
        for fp in sorted(base.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in exts:
                continue
            text = read_text(fp)
            for name, rx in patterns:
                counts[name] = counts.get(name, 0) + len(rx.findall(text))
    return counts


def check_p3(root, contract, res):
    face = contract["faces"]["P3"]
    onto_roots = face.get("onto_construct_roots") or []
    onto_exts = set(x.lower() for x in (face.get("onto_construct_exts") or [".ttl"]))
    try:
        onto_pats = [(name, re.compile(p)) for name, p in (face.get("onto_construct_regex") or {}).items()]
    except re.error as exc:
        res.blocked("P3-regex-invalid", "onto_construct_regex 非法: %s" % exc)
    observed = count_occurrences(root, onto_roots, onto_exts, onto_pats)
    res.stats["onto_construct_counts"] = observed
    for name, minimum in (face.get("onto_min_construct_counts") or {}).items():
        got = observed.get(name, 0)
        if got < int(minimum):
            res.fail("P3-owl-native-axiom-missing", ",".join(onto_roots), 0, name, "",
                     "声明为 OWL 原生公理的构造 %s 实测 %d < 契约下限 %d（语义面退化）"
                     % (name, got, int(minimum)))

    # ① 声明为「OWL 原生公理」的条目 → 必须有 .ttl 出处，且非 Python 断言冒充
    for ax in face.get("onto_axioms") or []:
        refs = ax.get("ttl_refs") or []
        if not refs:
            res.fail("P3-python-only-claim", ax.get("id", "?"), 0, "", "",
                     "条目 %r 声明为 OWL 原生公理但没有 .ttl 出处（只由 Python 断言冒充）"
                     % ax.get("statement", ax.get("id")))
            continue
        for ref in refs:
            rel = ref["file"]
            p = root / rel
            if not p.exists():
                res.fail("P3-ref-file-missing", rel, int(ref.get("line") or 0), "", "",
                         "声明的 .ttl 出处不存在")
                continue
            inside = any(rel.startswith(r.rstrip("/") + "/") for r in onto_roots)
            if not inside or p.suffix.lower() != ".ttl":
                res.fail("P3-ref-outside-ttl-plane", rel, int(ref.get("line") or 0), "", "",
                         "声明的 OWL 公理出处必须落在 %s 下的 .ttl（禁止指向 .py）" % onto_roots)
                continue
            lines = read_text(p).splitlines()
            ln = int(ref.get("line") or 0)
            pat = ref.get("pattern") or ""
            if ln <= 0 or ln > len(lines):
                res.fail("P3-ref-line-out-of-range", rel, ln, "", "", "声明行号越界")
                continue
            hay = lines[ln - 1] + "\n" + "\n".join(lines[ln:ln + int(ref.get("window") or 1)])
            if pat not in hay:
                res.fail("P3-ref-pattern-absent", rel, ln, pat, "",
                         "声明的 .ttl 出处在盘上找不到该公理（声明↔实现对账失败）")

    # ② 方案1 侧的规则条目 → 必须有 LPG/Cypher 出处
    for ru in face.get("kg_rules") or []:
        refs = ru.get("cypher_refs") or []
        if not refs:
            res.fail("P3-kg-rule-without-cypher", ru.get("id", "?"), 0, "", "",
                     "规则条目 %r 没有任何 Cypher/LPG 出处" % ru.get("statement", ru.get("id")))
            continue
        for ref in refs:
            rel = ref["file"]
            p = root / rel
            if not p.exists():
                res.fail("P3-ref-file-missing", rel, int(ref.get("line") or 0), "", "",
                         "声明的 Cypher 出处不存在")
                continue
            if p.suffix.lower() != ".cypher":
                res.fail("P3-ref-outside-cypher-plane", rel, int(ref.get("line") or 0), "", "",
                         "方案1 的规则出处必须落在 .cypher")
                continue
            lines = read_text(p).splitlines()
            ln = int(ref.get("line") or 0)
            if ln <= 0 or ln > len(lines):
                res.fail("P3-ref-line-out-of-range", rel, ln, "", "", "声明行号越界")
                continue
            if (ref.get("pattern") or "") not in lines[ln - 1]:
                res.fail("P3-ref-pattern-absent", rel, ln, ref.get("pattern") or "", "",
                         "声明的 Cypher 出处在盘上找不到该断言")

    kg_roots = face.get("kg_construct_roots") or []
    kg_exts = set(x.lower() for x in (face.get("kg_construct_exts") or [".cypher"]))
    kg_pats = [(name, re.compile(p)) for name, p in (face.get("kg_construct_regex") or {}).items()]
    got_kg = count_occurrences(root, kg_roots, kg_exts, kg_pats)
    res.stats["kg_construct_counts"] = got_kg
    for name, minimum in (face.get("kg_min_construct_counts") or {}).items():
        got = got_kg.get(name, 0)
        if got < int(minimum):
            res.fail("P3-lpg-assertion-missing", ",".join(kg_roots), 0, name, "",
                     "方案1 侧断言 %s 实测 %d < 契约下限 %d" % (name, got, int(minimum)))

    # ③ SHACL 形状必须保持纯 SHACL（不得夹带 OWL 公理）
    for rule in face.get("purity_of_shape_files") or []:
        rel = rule["file"]
        p = root / rel
        if not p.exists():
            res.blocked("P3-shape-file-missing", "声明的形状文件不存在: %s" % rel)
        text = comment_stripped_text(p, p.suffix.lower(), contract)
        hits = sum(len(re.findall(p_, text)) for p_ in (rule.get("forbid") or []))
        res.stats.setdefault("shape_files", {})[rel] = hits
        if hits > int(rule.get("max_owl_constructs") or 0):
            res.fail("P3-shape-not-pure", rel, 0, "", "",
                     "形状文件夹带 %d 处 OWL 构造 > 上限 %s（SHACL 与 OWL 公理面混装；已剥注释）"
                     % (hits, rule.get("max_owl_constructs")))
    res.note("判据 = 「声明↔实现」对账：声明为 OWL 原生公理的条目必须有 .ttl 出处（非空且非 Python），"
             "方案1 的规则条目必须有 .cypher 出处；两侧构造计数只许增不许减（下限约束）")
    res.note("形状文件纯度判据已剥注释：`owl:` 出现在 # 注释（如纪律说明）不算夹带公理")


# --------------------------------------------------------------------------
# P4 词表面（复用既有闸，不另造判据）
# --------------------------------------------------------------------------

def check_p4(root, contract, res):
    face = contract["faces"]["P4"]
    import yaml
    loaded = {}
    for mech in face.get("mechanisms") or []:
        rel = mech["file"]
        p = root / rel
        if not p.exists():
            res.blocked("P4-mechanism-missing", "既有闸缺失: %s（P4 复用既有机制，不得另造）" % rel)
        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                loaded[rel] = yaml.safe_load(read_text(p))
            except Exception as exc:
                res.blocked("P4-mechanism-unreadable", "%s 无法解析: %s" % (rel, exc))
        else:
            text = read_text(p)
            for token in mech.get("must_contain") or []:
                if token not in text:
                    res.blocked("P4-mechanism-token-absent",
                                "%s 里找不到声明的 %r（既有扫描器已被改动/移除）" % (rel, token))
        if mech.get("consumer"):
            cp = root / mech["consumer"]
            if not cp.exists():
                res.blocked("P4-consumer-missing", "消费者脚本不存在: %s" % mech["consumer"])

    src_rel = face["power_source"]["file"]
    if src_rel not in loaded:
        res.blocked("P4-power-source-unreadable", "判别力来源 %s 未作为 yaml 载入" % src_rel)
    doc = loaded[src_rel]
    gate = (doc or {}).get(face["power_source"].get("pointer") or "gate") or {}
    banned = gate.get("banned_words")
    if not banned:
        res.blocked("P4-banned-words-missing",
                    "%s 缺 banned_words ⇒ 词表机制不存在，本次没有观测" % src_rel)
    regex = gate.get(face["power_source"]["regex_key"])
    if not regex:
        res.blocked("P4-banned-regex-missing",
                    "%s 缺 %s ⇒ 未登记模型面禁用词正则" % (src_rel, face["power_source"]["regex_key"]))
    try:
        rx = re.compile(regex, re.I)
    except re.error as exc:
        res.blocked("P4-banned-regex-invalid", "%s 正则非法: %s" % (regex, exc))
    scope = gate.get("banned_words_scope") or {}
    res.stats["banned_words"] = list(banned)
    res.stats["banned_words_scope"] = scope
    res.stats["docs05_banned_regex"] = regex

    def matched(word):
        w = word.lower()
        if any(w in str(b).lower() or str(b).lower() in w for b in banned):
            return True
        return bool(rx.search(word))

    bp = face.get("blocking_power") or {}
    missed = [w for w in (bp.get("must_match") or []) if not matched(w)]
    if missed:
        res.fail("P4-blocking-power-lost", src_rel, 0, ",".join(missed), "",
                 "既有禁用词闸已拦不住这些对方载体词汇：%s" % missed)
    noisy = [w for w in (bp.get("must_not_match") or []) if matched(w)]
    if noisy:
        res.fail("P4-false-positive", src_rel, 0, ",".join(noisy), "",
                 "既有禁用词闸把中立词汇误判为禁用词（会逼出假 FAIL）：%s" % noisy)
    max_bw = bp.get("max_banned_words")
    if max_bw and len(banned) > int(max_bw):
        res.fail("P4-banned-words-over-limit", src_rel, 0, "", "",
                 "banned_words 条目 %d > 上限 %s" % (len(banned), max_bw))
    want_scope = bp.get("scope_applies_to")
    if want_scope and list(scope.get("applies_to") or []) != list(want_scope):
        res.fail("P4-scope-drift", src_rel, 0, "", "",
                 "banned_words_scope.applies_to 实测 %r ≠ 契约 %r"
                 % (scope.get("applies_to"), want_scope))
    want_exempt = bp.get("scope_exempt")
    if want_exempt is not None and list(scope.get("exempt") or []) != list(want_exempt):
        res.fail("P4-scope-drift", src_rel, 0, "", "",
                 "banned_words_scope.exempt 实测 %r ≠ 契约 %r"
                 % (scope.get("exempt"), want_exempt))
    res.stats["blocking_power_probe"] = len(bp.get("must_match") or [])
    res.note("判据 = **断言既有闸存在且仍有拦截力**（不复制、不新增判据）；"
             "拦截力用 must_match / must_not_match 两组探针实测，而非只查文件是否存在")


# --------------------------------------------------------------------------
# P5 共享面中性
# --------------------------------------------------------------------------

def check_p5(root, contract, res):
    face = contract["faces"]["P5"]
    classify = contract["classify"]
    scan_exts = set(x.lower() for x in (classify.get("scan_exts") or []))
    entries = contract["allowlist"]["entries"]
    index = build_allowlist_index(entries, "P5")
    self_excluded = set(e["path"] for e in (contract["meta"].get("self_excluded") or []))
    total = 0
    by_class = {}
    by_path = {}
    for scan in face.get("scans") or []:
        sid = scan["id"]
        pats = compile_patterns(contract["vocab"][scan["pattern_set"]])
        exts = set(x.lower() for x in (scan.get("exts") or scan_exts))
        skip = set(scan.get("skip_files") or []) | self_excluded
        for rel_root in scan.get("roots") or [scan["root"]]:
            for rel, line_no, token, pos, ext, line, cls in iter_file_hits(
                    root, rel_root, exts, pats, contract, skip):
                total += 1
                by_class[cls] = by_class.get(cls, 0) + 1
                by_path[rel] = by_path.get(rel, 0) + 1
                attribute_hit(res, index, sid, rel, line_no, token, cls)
    # 整体排除根：必须声明，且带 reason；命中数受上限约束（保留漂移探测）
    for ex in face.get("excluded_roots") or []:
        if not ex.get("reason"):
            res.blocked("P5-exclusion-without-reason",
                        "排除根 %s 未给 reason（豁免必须可人工审）" % ex.get("root"))
        base = root / ex["root"]
        if not base.exists():
            res.blocked("P5-excluded-root-missing", "声明的排除根不存在: %s" % ex["root"])
        pats = compile_patterns(contract["vocab"][ex.get("pattern_set") or "carrier_tokens"])
        exts = set(x.lower() for x in (ex.get("exts") or scan_exts))
        n = 0
        for fp in sorted(base.rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in exts:
                continue
            rel = fp.relative_to(root).as_posix()
            if rel in self_excluded:
                continue
            text = read_text(fp)
            for line in text.splitlines():
                for _p, rxp in pats:
                    n += len(rxp.findall(line))
        res.stats.setdefault("excluded_roots", {})[ex["root"]] = n
        cap = ex.get("max_hits")
        if cap is not None and n > int(cap):
            res.fail("P5-excluded-root-growth", ex["root"], 0, "", "",
                     "排除根 %s 命中 %d > 上限 %s（该层的载体词汇漂移超声明）"
                     % (ex["root"], n, cap))

    # 产物面：不得放任一载体独有的推理产物
    dump_exts = set(x.lower() for x in (face.get("materialized_dump_exts") or []))
    dumped = []
    base = root / "shared"
    if not base.exists():
        res.blocked("P5-shared-missing", "shared\\ 不存在")
    for fp in sorted(base.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root).as_posix()
        if rel in self_excluded:
            continue
        if fp.suffix.lower() in dump_exts:
            dumped.append(rel)
    res.stats["materialized_dump_files"] = dumped
    for rel in dumped:
        declared = any(e.get("face") == "P5" and e.get("path") == rel and not int(e.get("line") or 0)
                       for e in entries)
        if not declared:
            res.fail("P5-materialized-inference", rel, 0, Path(rel).suffix, "",
                     "shared\\ 出现图/推理产物文件（%s）：共享面不得放任一载体独有的推理结果"
                     % Path(rel).suffix)
    res.stats["hits_total"] = total
    res.stats["by_class"] = by_class
    res.stats["paths_with_hits"] = len(by_path)
    res.stats["top_paths"] = dict(sorted(by_path.items(), key=lambda kv: -kv[1])[:12])
    res.note("判据 = token/产物扫描 + 人工可审 allowlist（每条含 path/class/reason）；"
             "共享面的双载体声明是**合法中性**，但必须逐文件登记并受上限约束")


# --------------------------------------------------------------------------
# P6 测量面
# --------------------------------------------------------------------------

def check_p6(root, contract, res, run_dependent):
    face = contract["faces"]["P6"]
    for gate in face.get("dependent_gates") or []:
        rel = gate["script"]
        p = root / rel
        if not p.exists():
            res.blocked("P6-gate-missing",
                        "依赖的既有门禁缺失: %s（其结论无从断言）" % rel)
        got = sha256_file(p)
        want = (gate.get("sha256") or "").lower()
        ev = gate.get("evidence")
        ev_path = (root / ev) if ev else None
        marker = gate.get("expect_marker") or ""
        if want and got != want:
            res.blocked("P6-pin-stale",
                        "%s 的 sha256 已变（实测 %s… ≠ 登记 %s…）⇒ 登记的期望结论(%s exit=%s)失效，"
                        "须重跑并把新读数写回契约" % (rel, got[:16], want[:16],
                                                  gate.get("id"), gate.get("expect_exit")))
        if ev_path is None or not ev_path.exists():
            res.blocked("P6-evidence-missing",
                        "%s 的结论留档缺失: %s" % (gate.get("id"), ev))
        text = read_text(ev_path)
        if marker and marker not in text:
            res.blocked("P6-evidence-marker-absent",
                        "%s 的留档里找不到期望结论标记 %r ⇒ 该结论本次没有观测"
                        % (gate.get("id"), marker))
        res.stats.setdefault("gates", {})[gate.get("id") or rel] = {
            "script": rel,
            "sha256": got,
            "expect_exit": gate.get("expect_exit"),
            "marker_ok": bool(marker and marker in text),
        }
        if run_dependent:
            cmd = [sys.executable] + [rel] + list(gate.get("cli") or [])
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True)
            out = proc.stdout.decode("utf-8", errors="replace")
            want_exit = int(gate.get("expect_exit"))
            ok = (proc.returncode == want_exit) and (marker in out if marker else True)
            res.stats["gates"][gate.get("id") or rel]["rerun_exit"] = proc.returncode
            if not ok:
                res.fail("P6-rerun-mismatch", rel, 0, "", "",
                         "重跑实测 exit=%d（期望 %d）且标记 %r%s"
                         % (proc.returncode, want_exit, marker,
                            " 命中" if marker in out else " 未命中"))
    for rule in face.get("parity_assertions") or []:
        if not rule.get("reason"):
            res.blocked("P6-assertion-without-reason", "parity 断言缺 reason")
        res.stats.setdefault("parity_assertions", []).append(rule.get("id") or rule.get("statement"))
    res.note("判据 = 断言既有门禁结论仍成立：默认**对已留档读数 + 脚本 sha256 做 pin 复核**"
             "（pin 失效即 BLOCKED，不静默 PASS）；`--run-dependent-gates` 时逐条重跑真退出码")
    res.note("★ guard_conformance.py 的期望 exit = 3（CARRIER_SPECIFIC 已登记载体边界），"
             "不是 PASS；本门禁如实登记而不改判")


# --------------------------------------------------------------------------
# 统一校验
# --------------------------------------------------------------------------

def run_all(root: Path, contract_path, contract, res_list, run_dependent):
    entries = contract["allowlist"]["entries"]
    max_entries = int(contract["allowlist"]["max_allowlist_entries"])
    check_p1(root, contract, res_list["P1"])
    check_p2(root, contract, res_list["P2"])
    check_p3(root, contract, res_list["P3"])
    check_p4(root, contract, res_list["P4"])
    check_p5(root, contract, res_list["P5"])
    check_p6(root, contract, res_list["P6"], run_dependent)
    for fid in REQUIRED_FACES:
        check_allowlist_hygiene(res_list[fid], entries, fid, max_entries)
    if len(entries) > max_entries:
        res_list["P1"].findings.append(Finding(
            "P1", "allowlist-over-limit", (contract_path.name if contract_path else CONTRACT_REL),
            0, "", "", "allowlist 条目 %d > max_allowlist_entries %d" % (len(entries), max_entries)))
        res_list["P1"].verdict = "FAIL"
    # 陈旧豁免统一检查：必须在**六面全部跑完**之后做一次（行级与路径级都覆盖），
    # 归到该条目所属的面，避免同一份 entries 被重复上报。
    for fid in REQUIRED_FACES:
        check_allowlist_stale(res_list[fid], [e for e in entries if e.get("face") == fid])


def emit_inventory(root: Path, contract, contract_path):
    """把六面的现场读数打成可粘贴的 allowlist 骨架。"""
    classify = contract["classify"]
    scan_exts = set(x.lower() for x in (classify.get("scan_exts") or []))
    out = []
    for face_id in ("P1", "P5"):
        face = contract["faces"][face_id]
        scans = face.get("scans") or []
        if face_id == "P1":
            scans = [{"id": s["id"], "roots": [s["root"]], "pattern_set": s["pattern_set"],
                      "exts": s.get("exts")} for s in scans]
        for scan in scans:
            pats = compile_patterns(contract["vocab"][scan["pattern_set"]])
            exts = set(x.lower() for x in (scan.get("exts") or scan_exts))
            skip = set(scan.get("skip_files") or [])
            skip |= set(e["path"] for e in (contract["meta"].get("self_excluded") or []))
            agg = {}
            for rel_root in scan.get("roots") or [scan["root"]]:
                for rel, line_no, token, pos, ext, line, cls in iter_file_hits(
                        root, rel_root, exts, pats, contract, skip):
                    key = (rel, line_no, token, cls) if face_id == "P1" else (rel, 0, "", cls)
                    agg[key] = agg.get(key, 0) + 1
            out.append("----- face=%s scan=%s : %d distinct keys -----" % (face_id, scan["id"], len(agg)))
            for (rel, line_no, token, cls), n in sorted(agg.items()):
                out.append("- {face: %s, scan: %s, path: %s, line: %d, token: %s, class: %s, max_hits: %d, reason: ''}"
                           % (face_id, scan["id"], rel, line_no,
                              json.dumps(token, ensure_ascii=False) if token else '""',
                              cls, n))
    # 排除根计数
    for ex in contract["faces"]["P5"].get("excluded_roots") or []:
        pats = compile_patterns(contract["vocab"][ex.get("pattern_set") or "carrier_tokens"])
        exts = set(x.lower() for x in (ex.get("exts") or scan_exts))
        skip = set(e["path"] for e in (contract["meta"].get("self_excluded") or []))
        n = 0
        for fp in sorted((root / ex["root"]).rglob("*")):
            if not fp.is_file() or fp.suffix.lower() not in exts:
                continue
            if fp.relative_to(root).as_posix() in skip:
                continue
            text = read_text(fp)
            for line in text.splitlines():
                for _p, rxp in pats:
                    n += len(rxp.findall(line))
        out.append("----- excluded_root=%s : hits=%d -----" % (ex["root"], n))
    return "\n".join(out)


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

SHADOW_ROOTS = ("scheme1-kg", "scheme2-onto", "shared")
# 旧代（M4）证据名：仅作**并集下限**保留，使 S20 真树不变性顺带覆盖它们。
LEGACY_SHADOW_FILES = ("tmp/w28_p2_1.out.txt", "tmp/w28_p2_3b.out.txt", "tmp/w28_p2_5.out.txt",
                       "tmp/w28_check_fairness.out.txt", "tmp/w28_toolface_diff_strict.out.txt",
                       "tmp/w28_guard_conformance.out.txt", "tmp/w28_f5_kgt_gate.out.txt")


def shadow_files(root: Path, contract_arg=None):
    """沙箱须复制的单文件清单 = **契约登记的证据文件** ∪ 旧代遗留名单。

    ★ 修因（S3a-2c 实测）：此清单若硬编码某一代证据名，则每次重冻 P2/P6 证据名之后
    沙箱都缺件 ⇒ P2 立即 `P2-evidence-missing` BLOCKED 短路，其后 S02/S03/S06/…
    负控全部「期望=1 实测=2」，自检整体假红。这是**仪器耦合**，不是判据放松：判据
    一个字没动，只是把「哪些文件是证据」交给契约声明，使自检与被检对象永远同一份
    证据。契约不可解析/字段缺失时回落到旧代名单（契约缺失本身另有 S16/S17 专测）。
    """
    rels = list(LEGACY_SHADOW_FILES)
    try:
        _path, data = load_contract(root, contract_arg)
        faces = data.get("faces") or {}
        cand = []
        ev = (faces.get("P2") or {}).get("evidence")
        if isinstance(ev, dict) and isinstance(ev.get("files"), list):
            cand += [x for x in ev["files"] if isinstance(x, str)]
        for gate in (faces.get("P6") or {}).get("dependent_gates") or []:
            if isinstance(gate, dict):
                one = gate.get("evidence")
                if isinstance(one, str):
                    cand.append(one)
                elif isinstance(one, list):
                    cand += [x for x in one if isinstance(x, str)]
        for rel in cand:
            rel = rel.strip().replace("\\", "/")
            if rel and not rel.startswith("/") and rel not in rels:
                rels.append(rel)
    except Exception:
        pass
    return tuple(rels)


def tree_digest(root: Path, rels):
    h = hashlib.sha256()
    for rel in rels:
        base = root / rel
        if not base.exists():
            continue
        for fp in sorted(base.rglob("*")):
            if not fp.is_file():
                continue
            h.update(fp.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(fp.read_bytes())
    return h.hexdigest()


def tree_digest_map(root: Path, rels):
    """逐文件 sha256 映射 —— **只用于 S20 的诊断**（点名真树在自检期间被谁改了）。

    ★ 与 `tree_digest` 遍历口径保持一致（目录按 `rglob`；单文件路径单独计入，
    因此本映射是 `tree_digest` 文件集合的**超集**，不参与判定、只用于打印）。
    """
    out = {}
    for rel in rels:
        base = root / rel
        if not base.exists():
            continue
        if base.is_file():
            out[base.relative_to(root).as_posix()] = hashlib.sha256(base.read_bytes()).hexdigest()
            continue
        for fp in sorted(base.rglob("*")):
            if fp.is_file():
                out[fp.relative_to(root).as_posix()] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return out


def build_shadow(root: Path, dest: Path, files=None):
    files = shadow_files(root) if files is None else tuple(files)
    for rel in SHADOW_ROOTS:
        src = root / rel
        dst = dest / rel
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    (dest / "tmp").mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = root / rel
        if src.exists():
            shutil.copy2(src, dest / rel)


def patch(path: Path, old: str, new: str, count=1):
    data = read_text(path)
    if old not in data:
        raise RuntimeError("selftest 补丁锚点找不到: %r in %s" % (old[:60], path.name))
    data = data.replace(old, new, count) if count >= 0 else data.replace(old, new)
    path.write_bytes(data.encode("utf-8"))


def selftest(root: Path, gate: Path, contract_rel: str):
    tmp_root = root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix="_w28_selftest_", dir=str(tmp_root)))
    cases = []
    shadow = shadow_files(root, contract_rel)
    digest_before = tree_digest(root, SHADOW_ROOTS + shadow)
    map_before = tree_digest_map(root, SHADOW_ROOTS + shadow)   # ★ 仅 S20 诊断用

    def run(target_root, extra_cli=(), expect=0, label=""):
        cmd = [sys.executable, str(gate), "--root", str(target_root),
               "--contract", str(target_root / contract_rel)] + list(extra_cli)
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        head = ""
        for ln in out.splitlines():
            if ln.startswith("RESULT:") or ln.startswith("BLOCKED:"):
                head = ln.strip()
                break
        return proc.returncode, head, out, err

    def fresh(name):
        d = sandbox / name
        if d.exists():
            shutil.rmtree(d)
        build_shadow(root, d, shadow)
        return d

    def findings_of(out, limit=6):
        """★ 可诊断性：从子进程输出里抽出「哪一面、哪条命中」。

        失败时把它打出来，避免只报「期望=0 实测=1」而看不出原因。
        ★ 只做**输出诊断**，不放宽任何判据（S01 仍要求真树六面 PASS）。
        """
        why = []
        for ln in out.splitlines():
            s = ln.strip()
            if s.startswith("[FAIL]") or s.startswith("[BLOCKED]") or re.match(r"^\[P\d\]\s", s):
                why.append(s)
        return why[:limit]

    def p2_evidence():
        """契约登记的 P2 拨号见证证据（相对路径）—— S05/S06 的靶子由**声明**派生。

        ★ 与 `shadow_files()` 同一修因：靶子若写死某一代证据名，重冻之后
        负控打的就不是被检对象了（S05 删不掉真证据、S06 篡改被忽略）。
        """
        try:
            _p, data = load_contract(root, contract_rel)
            ev = ((data.get("faces") or {}).get("P2") or {}).get("evidence")
            if isinstance(ev, dict) and isinstance(ev.get("files"), list):
                return [x.lstrip("/") for x in ev["files"] if isinstance(x, str)]
        except Exception:
            pass
        return [r for r in LEGACY_SHADOW_FILES if "p2_" in r and r.endswith(".out.txt")]

    def record(name, expect, got, head, why=None):
        ok = (got == expect)
        cases.append((name, expect, got, ok, head, list(why or [])))
        print("  %-5s %-42s 期望=%d 实测=%d  %s" %
              ("PASS" if ok else "FAIL", name, expect, got, head))
        if not ok:
            # ★ 诊断行：点名面 / 规则 / path:line / token（只在失败时打印）
            for w in (why or []):
                print("        \u21b3 %s" % w)

    # ---- ★ 预检：真树自身是否可观测 ----------------------------------------
    # P2 面由**运行期拨号见证**驱动：证据（tmp/w28_p2_*.out.txt，真机只读采样留档）
    # 不随源码分发时，门禁会先判 P2-evidence-missing BLOCKED，随后所有负控都被这条
    # BLOCKED 短路掩蔽（期望=1 实测=2），S05/S06 还会因找不到证据副本直接抛错。
    # 这是**仪器耦合**，不是判据放松：本轮只观测「真树不得判 FAIL」这一条，
    # 其余各组一律不作观测（SKIP），整轮判 2 = BLOCKED（BLOCKED ≠ PASS ≠ FAIL）。
    base_rc, base_head, base_out, _base_err = run(root, label="baseline")
    evidence_absent = (base_rc == 2 and "P2-evidence-missing" in base_head)

    # S01 正控：真树
    if evidence_absent:
        record("S01 正控：真树未判 FAIL（P2 证据未分发 ⇒ BLOCKED）", 2, base_rc, base_head,
               findings_of(base_out))
        shutil.rmtree(sandbox, ignore_errors=True)
        print("-" * 78)
        print("selftest: 1/20 组已观测（S01 PASS）；其余 19 组 SKIP"
              " —— 真树 P2 拨号见证证据缺失导致 BLOCKED 短路")
        print("  补齐路径：把真机只读采样留档放回 tmp/w28_p2_*.out.txt 后可跑全量 20 组")
        print("  退出码 2 = BLOCKED（**没有完整观测**；BLOCKED ≠ PASS ≠ FAIL）")
        print("[沙箱] %s（自检结束已删除；真树未改动）" % sandbox.name)
        return 2
    if base_rc != 0:
        print("  [注意] 真树本身未 PASS（实测=%d %s）：负控结果可能被短路掩蔽，按原口径继续。"
              % (base_rc, base_head))
    record("S01 正控：真树六面 PASS", 0, base_rc, base_head, findings_of(base_out))

    # S02 P1 负控：scheme1-kg 注入真运行期 GraphDB 调用
    d = fresh("s02")
    (d / "scheme1-kg" / "project" / "zz_purity_probe.py").write_text(
        "import requests\n\ndef probe():\n"
        "    return requests.post('http://127.0.0.1:7200/repositories/cc-ops', data={'query': 'ASK {}'})\n",
        encoding="utf-8")
    got, head, _o, _e = run(d, label="s02")
    record("S02 负控：scheme1-kg 注入 GraphDB 运行期调用", 1, got, head)

    # S03 P1 负控：scheme2-onto 注入真运行期 Neo4j 调用
    d = fresh("s03")
    (d / "scheme2-onto" / "project" / "zz_purity_probe.py").write_text(
        "from neo4j import GraphDatabase\n\n"
        "DRIVER = GraphDatabase.driver('bolt://127.0.0.1:7688')\n\n"
        "def probe():\n    with DRIVER.session() as sess:\n        return sess.run('MATCH (n) RETURN count(n)')\n",
        encoding="utf-8")
    got, head, _o, _e = run(d, label="s03")
    record("S03 负控：scheme2-onto 注入 Neo4j(bolt/7688) 调用", 1, got, head)

    # S04 P2 负控：KG 侧代码头泄漏对侧环境变量名
    d = fresh("s04")
    probe_py = d / "scheme1-kg" / "service" / "cc_tools.py"
    probe_py.write_text(read_text(probe_py) + "\nGRAPHDB_ENV_FALLBACK = \"CC_GRAPHDB_URL\"\n",
                        encoding="utf-8")
    got, head, _o, _e = run(d, label="s04")
    record("S04 负控：KG 侧代码头出现 CC_GRAPHDB_*", 1, got, head)

    # S05 P2 BLOCKED：拨号见证证据缺失
    d = fresh("s05")
    removed = []
    for rel in p2_evidence():
        fp = d / rel
        if fp.exists():
            fp.unlink()
            removed.append(rel)
    if not removed:
        for rel in LEGACY_SHADOW_FILES:
            fp = d / rel
            if fp.exists():
                fp.unlink()
                removed.append(rel)
    if not removed:
        raise RuntimeError("selftest S05 假设失效：沙箱里没有任何拨号见证证据副本")
    got, head, _o, _e = run(d, label="s05")
    record("S05 BLOCKED：拨号见证证据缺失（无观测）", 2, got, head)

    # S06 P2 负控：篡改证据——ONTO 进程被伪造成连对侧端口
    d = fresh("s06")
    _p6d, _c6 = load_contract(root, contract_rel)
    _exps = [e for e in (((_c6.get("faces") or {}).get("P2") or {}).get("evidence") or {})
             .get("expectations", []) if isinstance(e, dict)]
    _onto = next((e for e in _exps if e.get("arm") == "ONTO-Q"), _exps[0] if _exps else None)
    if not _onto:
        raise RuntimeError("selftest S06 假设失效：契约 P2 expectations 为空")
    _pid = int(_onto["pid"])
    _own = int(_onto["own_port"])
    _forbid = [int(x) for x in (_onto.get("forbid_ports") or []) if int(x) != _own]
    if not _forbid:
        raise RuntimeError("selftest S06 假设失效：ONTO 期望没有对侧 forbid_port")
    _bad = _forbid[0]
    _line_re = re.compile(r"(?m)^.*pid=%d\s+to_7200=\d+\s+to_7475=\d+\s+to_7688=\d+.*$" % _pid)
    _ports = {7200: 0, 7475: 0, 7688: 0}
    _ports[_own] = 1
    _ports[_bad] = 7
    _tampered = None
    for _rel in p2_evidence():
        _fp = d / _rel
        if not _fp.exists():
            continue
        _txt = read_text(_fp)
        if not _line_re.search(_txt):
            continue
        _new = "  pid=%d  to_7200=%d  to_7475=%d  to_7688=%d" % (_pid, _ports[7200], _ports[7475], _ports[7688])
        _fp.write_text(_line_re.sub(_new, _txt, count=1), encoding="utf-8")
        _tampered = _rel
        break
    if not _tampered:
        raise RuntimeError("selftest S06 假设失效：证据里找不到 pid=%d 的计数行" % _pid)
    got, head, _o, _e = run(d, label="s06")
    record("S06 负控：ONTO 进程被伪造成连 7475", 1, got, head)

    # S07 P3 负控：抽掉一条声明为 OWL 原生公理的公理
    d = fresh("s07")
    patch(d / "scheme2-onto" / "ontology" / "cc-action.ttl",
          "owl:hasKey ( cca:actionCode )", "rdfs:comment ( cca:actionCode )")
    got, head, _o, _e = run(d, label="s07")
    record("S07 负控：抽掉 owl:hasKey 公理（语义面退化）", 1, got, head)

    # S08 P3 负控：把声明的 .ttl 出处改成 .py（Python 断言冒充）
    d = fresh("s08")
    patch(d / contract_rel,
          "file: scheme2-onto/ontology/cc-action.ttl, line: 221",
          "file: scheme2-onto/project/p2_graphdb.py, line: 1")
    got, head, _o, _e = run(d, label="s08")
    record("S08 负控：OWL 公理出处被改指 .py", 1, got, head)

    # S09 P4 BLOCKED：既有词表机制被拆掉
    d = fresh("s09")
    txt = read_text(d / "shared" / "model" / "kgt-prompt-variants.yaml")
    txt = re.sub(r"(?m)^  banned_words:.*(\n(?:    .*)*)?", "  banned_words_removed: true\n", txt, count=1)
    (d / "shared" / "model" / "kgt-prompt-variants.yaml").write_text(txt, encoding="utf-8")
    got, head, _o, _e = run(d, label="s09")
    record("S09 BLOCKED：既有闸缺 banned_words（无观测）", 2, got, head)

    # S10 P4 负控：词表被掏空 ⇒ 拦截力丢失
    d = fresh("s10")
    patch(d / "shared" / "model" / "kgt-prompt-variants.yaml",
          "  banned_words: [Cypher, SPARQL, 图, 本体, 三元组, schema, 查询语句, property graph, RDF, OWL]",
          "  banned_words: [Cypher]")
    got, head, _o, _e = run(d, label="s10")
    record("S10 负控：词表被掏空 ⇒ 拦不住 OWL/RDF/本体", 1, got, head)

    # S11 P5 负控：shared\ 塞进推理产物物化文件
    d = fresh("s11")
    (d / "shared" / "model" / "zz-inferred-ontology.ttl").write_text(
        "# 由 GraphDB ?infer=true 推导出的三元组物化\n"
        "<http://x/a> <http://x/r> <http://x/b> .\n", encoding="utf-8")
    got, head, _o, _e = run(d, label="s11")
    record("S11 负控：shared\\ 出现推理产物物化文件", 1, got, head)

    # S12 P5 负控：shared/model 出现未登记的载体词文件
    d = fresh("s12")
    (d / "shared" / "model" / "zz-carrier-leak.yaml").write_text(
        "carrier:\n  kg: neo4j\n  onto: graphdb\n", encoding="utf-8")
    got, head, _o, _e = run(d, label="s12")
    record("S12 负控：shared\\model 未登记载体词文件", 1, got, head)

    # S13 陈旧豁免负控：让一条已登记的命中在盘上不复现
    d = fresh("s13")
    patch(d / "scheme2-onto" / "project" / "p2_graphdb.py", "Neo4j", "对侧图库")
    got, head, _o, _e = run(d, label="s13")
    record("S13 负控：陈旧豁免（登记命中已不复现）", 1, got, head)

    # S14 豁免超上限负控（锚点从契约**现值**推导，避免与契约数值脱钩 ⇒ 不再写死上限数字）
    d = fresh("s14")
    cp = d / contract_rel
    txt = read_text(cp)
    m = re.search(r"(?m)^(  max_allowlist_entries:\s*)(\d+)\s*$", txt)
    if not m:
        raise RuntimeError("selftest S14 补丁锚点找不到（max_allowlist_entries）")
    cur = int(m.group(2))
    n_entries = len(re.findall(r"(?m)^    - \{face: ", txt))
    tiny = max(1, cur // 20)
    if n_entries == 0 or tiny >= n_entries:
        raise RuntimeError("selftest S14 假设失效：上限现值=%d 登记条数=%d" % (cur, n_entries))
    cp.write_text(txt[:m.start(2)] + str(tiny) + txt[m.end(2):], encoding="utf-8")
    got, head, _o, _e = run(d, label="s14")
    record("S14 负控：豁免条数超 max_allowlist_entries", 1, got, head)

    # S15 豁免分类不符负控
    d = fresh("s15")
    cp = d / contract_rel
    txt = read_text(cp)
    txt2 = re.sub(r'(line: 50, token: "GraphDB", class: )comment', r'\1declaration', txt, count=1)
    if txt2 == txt:
        raise RuntimeError("selftest S15 补丁锚点找不到")
    cp.write_text(txt2, encoding="utf-8")
    got, head, _o, _e = run(d, label="s15")
    record("S15 负控：登记 class 与实测分类不符", 1, got, head)

    # S16 BLOCKED：契约删一个面
    d = fresh("s16")
    txt = read_text(d / contract_rel)
    txt = re.sub(r"(?ms)^  P3:\n.*?(?=^  P4:)", "  P3-removed: {}\n", txt, count=1)
    (d / contract_rel).write_text(txt, encoding="utf-8")
    got, head, _o, _e = run(d, label="s16")
    record("S16 BLOCKED：契约缺 faces.P3 字段", 2, got, head)

    # S17 BLOCKED：契约文件缺失
    d = fresh("s17")
    (d / contract_rel).unlink()
    got, head, _o, _e = run(d, label="s17")
    record("S17 BLOCKED：契约文件缺失", 2, got, head)

    # S18 BLOCKED：P6 pin 失效（依赖的既有门禁脚本字节已变、契约里的 sha256 未回写）
    d = fresh("s18")
    p18 = d / "shared" / "deploy" / "check_fairness.py"
    p18.write_text(read_text(p18) + "\n# tamper：模拟该脚本被改动但契约未回写\n", encoding="utf-8")
    got, head, _o, _e = run(d, label="s18")
    record("S18 BLOCKED：P6 依赖脚本 pin 失效", 2, got, head)

    # S19 BLOCKED：P6 依赖的既有门禁缺失
    d = fresh("s19")
    (d / "shared" / "verify" / "f5_kgt_gate.py").unlink()
    got, head, _o, _e = run(d, label="s19")
    record("S19 BLOCKED：P6/P4 依赖的既有门禁缺失", 2, got, head)

    # S20 真树未被负控改动
    digest_after = tree_digest(root, SHADOW_ROOTS + shadow)
    map_after = tree_digest_map(root, SHADOW_ROOTS + shadow)   # ★ 仅诊断用
    changed = []
    for _rel in sorted(set(map_before) | set(map_after)):
        _b, _a = map_before.get(_rel), map_after.get(_rel)
        if _b != _a:
            changed.append("%s（%s）" % (_rel, "新增" if _b is None else "被删" if _a is None else "内容变化"))
    why20 = (["真树在自检期间发生变化（**并发写入者**动了真树，非本自检所致）——点名："] + changed[:8]) if changed else None
    record("S20 真树在自检前后 sha256 未变", 0,
           0 if digest_before == digest_after else 1,
           "digest=%s" % digest_before[:16], why20)

    shutil.rmtree(sandbox, ignore_errors=True)

    total = len(cases)
    bad = [c for c in cases if not c[3]]
    print("-" * 78)
    print("selftest: %d/%d PASS" % (total - len(bad), total))
    for c in bad:
        print("  BAD %s 期望=%d 实测=%d" % (c[0], c[1], c[2]))
        for _w in (c[5] if len(c) > 5 else []):
            print("        \u21b3 %s" % _w)
    print("[沙箱] %s（自检结束已删除；真树未改动）" % sandbox.name)
    return 0 if not bad else 1


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="G25 · 两实现纯粹性门禁（裁决 15）；退出码 0=PASS 1=FAIL 2=BLOCKED"
                    "（BLOCKED = 契约缺失/引用不上，本次没有观测；BLOCKED ≠ FAIL）")
    ap.add_argument("--root", default=None, help="工作区根（默认由本脚本位置推定）")
    ap.add_argument("--contract", default=None, help="契约路径（默认 %s）" % CONTRACT_REL)
    ap.add_argument("--selftest", action="store_true", help="跑 20 组自检（含负控），在 tmp\\ 沙箱内")
    ap.add_argument("--emit-inventory", action="store_true", help="打印六面现场读数与 allowlist 骨架")
    ap.add_argument("--run-dependent-gates", action="store_true",
                    help="P6 逐条重跑依赖的既有门禁（默认只做留档 + sha256 pin 复核）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（顶层只有一个 purity_gate 键）")
    ap.add_argument("--quiet", action="store_true", help="只打印结论")
    args = ap.parse_args(argv)

    gate_path = Path(__file__).resolve()
    root = Path(args.root).resolve() if args.root else gate_path.parents[2]
    contract_rel = args.contract or CONTRACT_REL

    if args.selftest:
        print("=" * 78)
        print("G25 纯粹性门禁 · --selftest（沙箱在 tmp\\ 下，真树只读）")
        print("=" * 78)
        return selftest(root, gate_path, contract_rel)

    if not args.quiet and not args.json:
        print("=" * 78)
        print("G25 · 两实现纯粹性门禁 | purity_gate.py")
        print("=" * 78)
        print("根目录 : %s" % root)
        print("契约   : %s" % (root / contract_rel if not Path(contract_rel).is_absolute() else contract_rel))

    payload = {"purity_gate": {"gate": GATE_REL, "root": str(root), "contract": contract_rel}}
    try:
        contract_path, contract = load_contract(root, contract_rel)
        if args.emit_inventory:
            print(emit_inventory(root, contract, contract_path))
            return EXIT_PASS
        res_list = {}
        for fid in REQUIRED_FACES:
            f = contract["faces"][fid]
            res_list[fid] = FaceResult(fid, f["name"], f.get("tier") or "")
        run_all(root, contract_path, contract, res_list, args.run_dependent_gates)
        verdict = "PASS"
        for fid in REQUIRED_FACES:
            if res_list[fid].verdict == "FAIL":
                verdict = "FAIL"
        payload["purity_gate"]["faces"] = [res_list[f].as_dict() for f in REQUIRED_FACES]
        payload["purity_gate"]["verdict"] = verdict
        payload["purity_gate"]["exit_code"] = EXIT_FAIL if verdict == "FAIL" else EXIT_PASS
        payload["purity_gate"]["allowlist"] = {
            "entries": len(contract["allowlist"]["entries"]),
            "max_allowlist_entries": contract["allowlist"]["max_allowlist_entries"],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            for fid in REQUIRED_FACES:
                r = res_list[fid]
                print("[%s] %s %s · tier=%s" % (
                    "PASS" if r.verdict == "PASS" else "FAIL", fid, r.name, r.tier or "-"))
                for k, v in r.stats.items():
                    if not args.quiet:
                        print("      %-22s %s" % (k, json.dumps(v, ensure_ascii=False)))
                for n in r.notes:
                    if not args.quiet:
                        print("      note: %s" % n)
                for f in r.findings:
                    print("      " + f.render())
            print("-" * 78)
            allowed = len(contract["allowlist"]["entries"])
            print("allowlist: %d 条（上限 %s）；六面 verdict=%s"
                  % (allowed, contract["allowlist"]["max_allowlist_entries"],
                     "/".join(res_list[f].verdict for f in REQUIRED_FACES)))
            print("RESULT: %s" % verdict)
            print("退出码 %d（0=PASS 1=FAIL 2=BLOCKED；BLOCKED ≠ FAIL）"
                  % (EXIT_FAIL if verdict == "FAIL" else EXIT_PASS))
        return EXIT_FAIL if verdict == "FAIL" else EXIT_PASS
    except Blocked as exc:
        payload["purity_gate"]["verdict"] = "BLOCKED"
        payload["purity_gate"]["exit_code"] = EXIT_BLOCKED
        payload["purity_gate"]["reason"] = str(exc)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("-" * 78)
            print("BLOCKED: %s" % exc)
            print("RESULT: BLOCKED（BLOCKED ≠ FAIL：契约缺失/引用不上 ⇒ 本次**没有观测**，不得当作 PASS）")
            print("退出码 2")
        return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
