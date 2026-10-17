#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
m5_cases_gate.py — M5 判决集（50 条）契约与用例文件的校验门禁

为什么要有这道门禁
  docs/06 裁定「50 条、族 14/14/6/6/4、难度 16/20/14、分母固定 50、abstain 单列、
  只取第一个候选、分层分族分别报、只能对 >=20pp 下结论」，但**用例文件本身**长什么
  样此前没有任何单一权威落点：W18 判据器（shared/verify/m5/contract.py）与 W19 运行台
  （shared/deploy/smoke/m5_run.py）各自实现了字段校验，录制方却无从知道「缺什么」。
  shared/model/m5-cases.yaml 是那一层契约；本门禁是它的执行者。

扫描对象
  1) 契约文件  shared/model/m5-cases.yaml（可用 --contract 覆盖）
  2) 用例文件  契约 case_file.default_path 指向的文件（可用 --cases 覆盖）
  3) 交叉核对面（只读、只用于核验契约没有自造数字）：
       docs/06-评测设计（A-B对照）.md      §1.1 族构成、§1.3 难度构成
       shared/verify/m5/contract.py        W18 的字段/构成常量（唯一真相源）
       shared/verify/m5/judge.py           W18 的层别名词表 L-LAYER v1
       shared/model/fault-modes.yaml       oracle 词表（18 个故障模式 code / layer / chain）

判定规则（逐条可复现）
  契约侧
    B-CONTRACT-MISSING      契约文件不存在 ⇒ BLOCKED（fail-closed，绝不静默 PASS）
    B-CONTRACT-PARSE        契约不可解析（YAML 缺失/语法错）⇒ BLOCKED
    B-CONTRACT-SECTION      契约必需段缺失（schema/meta/suite/case_schema/scoring/fill_plan/source/open_questions）
                            ⇒ BLOCKED：契约不完整就无法作为判据依据
    E-CONTRACT-SCHEMA       契约 schema 值不是 m5-cases-contract/1 ⇒ FAIL
    E-CONTRACT-SELF-SHA     契约带 sha/滚动标签的 64 位十六进制串 ⇒ FAIL（不得 pin 自身滚动 sha）
    E-CONTRACT-PLACEHOLDER  契约含 TODO/FIXME/TBD/待补/待定 占位符 ⇒ FAIL
    E-SUITE-*               分母 ≠ 50、族和不等于总数、族/难度构成与 W18 常量不一致 ⇒ FAIL
    E-DOC-*                 契约里的族/难度数字与 docs/06 正文行不符 ⇒ FAIL（数字必须来自原文）
    E-RECOMPUTE-*           docs/06 §1.3 构成复算与声明不一致这一事实未被如实登记/已过期 ⇒ FAIL
    E-CASE-SCHEMA-*         18 个既有字段被改名/删除、缺 expect_layer 显式标记、
                            字段缺 introduced_by/filled_by、新字段标注名不符实 ⇒ FAIL
    E-SCORING-*             分母口径/abstain 敏感性/只取第一个候选/分层分族/>=20pp 纪律缺失或被改 ⇒ FAIL
    E-FILLPLAN-*            三权分立缺角色、或有用例字段无人认领/被两个角色同时认领 ⇒ FAIL
    E-REF-*                 出处引用不可解析、指向不存在的文件、指向空行/表格分隔行 ⇒ FAIL
    E-REFS-BELOW-MIN        引用条数低于 --min-refs ⇒ FAIL（反恒真下限：检查了 0 条不算通过）
    E-OQ-REGISTRY           开放问题表缺 docs/06 §1.3 不自洽这条登记 ⇒ FAIL
  用例侧（用例文件不存在 ⇒ BLOCKED，不是 PASS 也不是 FAIL）
    B-CASES-NOT-RECORDED    契约已就位、用例未录制 ⇒ BLOCKED，并打印还缺什么
    E-CASES-*               字段缺失/改名、id 重复、期望层别缺失或越界、
                            oracle 判据为空或越出 fault-modes 词表、快照绑定不可解析、
                            多个快照 id（越过 C1 单一输入）、缺 C4/C5 类用例、
                            分母 ≠ 50、族/难度分布不符、无溯源锚点、合成用例未按夹具模式运行 ⇒ FAIL

已知覆盖边界（诚实声明）
  - 本门禁只校验「结构 + 溯源锚点 + 未声明合成」。它**无法证明**锚点背后的注入真的发生过；
    那属于注入方交接证据（作业单的回滚证据字段），不在机器校验范围。
  - 门禁绿 ≠ 因果结论成立，也 ≠ 判据口径被正确实现（那归 W18 的自测与 G6）。
  - 契约里 docs/06 §1.3 的复算算式是**契约登记的**算式，不是从 docs/06 动态解析出来的；
    本门禁执行登记算式并断言「声明 ≠ 复算」，同时用 live parse 交叉核对声明值本身。
    若 docs/06 改写构成文字而契约未同步，本门禁只会发现「声明值与正文行不符」，
    不会发现「构成文字的语义变了」。
  - `docs/06` 的行号引用只校验「文件存在 + 行号在范围内 + 该行非空且不是表格分隔/水平线」，
    不校验该行内容是否真的支撑被引用的结论。
  - `expect_objects` 的实例 id 前缀**已有权威表** = `shared/model/ccf.schema.json` 的
    `x-id-prefix-by-type`（12 类）：前缀不在表内 ⇒ `E-CASES-OBJECT-UNKNOWN` FAIL；
    Evidence 事实 id（`obs-NNNNNN`，无冒号）属另一栏 `evidence_facts`，写进 `expect_objects` ⇒ 同时报
    `E-CASES-OBJECT-SHAPE` 并提示该迁栏。（W46 收紧：旧文字为「没有权威表，见契约 OQ-M5-05，
    故只做形态校验 + 未登记前缀 WARN」——该 WARN 前提已被真机证伪，故已升级为硬判。）
  - P1-P11 执行协议合规性、payloadPolicy 逐字一致性、G8 信息可及性对齐不在本门禁范围。

退出码
  0 = PASS     契约自洽 + 用例文件（若存在）满足契约
  1 = FAIL     契约自相矛盾 / 用例文件违反契约（真缺陷）
  2 = BLOCKED  没测到：契约缺失、用例未录制、关键依赖缺失（BLOCKED ≠ FAIL）
  判定优先级：BLOCKED(2) > FAIL(1) > PASS(0)，与 shared/deploy/smoke/m5_run.py:98-99
  的 EXIT = {PASS:0, FAIL:1, BLOCKED:2} 同源。
"""

from __future__ import annotations

import argparse
import yaml
import contextlib
import copy
import hashlib
import io
import re
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED = 0, 1, 2
EXIT_LABEL = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_BLOCKED: "BLOCKED"}

HERE = Path(__file__).resolve().parent            # <root>/shared/verify
ROOT = HERE.parent.parent                         # <root>

CONTRACT_SCHEMA = "m5-cases-contract/1"
CASES_SCHEMA = "m5.cases/1"
SUITE_TOTAL = 50
FAMILY_COUNTS = {"C1": 14, "C2": 14, "C3": 6, "C4": 6, "C5": 6, "C6": 4}
DIFFICULTY_COUNTS = {"easy": 16, "medium": 20, "hard": 14}
DIFFICULTY_ORDER = ("easy", "medium", "hard")
FAMILY_ORDER = ("C1", "C2", "C3", "C4", "C5", "C6")
PREFIX_BY_FAMILY = {"C1": "A", "C2": "B", "C3": "D", "C4": "N", "C5": "Z", "C6": "T"}
NONE_SENTINEL = "NONE"
MIN_PP = 20

SNAPSHOT_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")
INJECTION_RE = re.compile(r"^(FIXTURE-)?INJ-[0-9]{8}-[0-9]{3}$")
OBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*:.+$")
# 出处引用：<路径含扩展名>:<行>[-<行>]  与简写 docs/NN:<行>[-<行>]
REF_EXT_RE = re.compile(
    r"(?:(?<=[\s（(,，;；、\"'`|])|^)((?:docs|shared|tmp|tools|upstream-eval)/[^\s:：,，;；\"'`|]*?\.(?:md|py|yaml|yml|json)):(\d+)(?:-(\d+))?",
    re.MULTILINE)
REF_SHORT_RE = re.compile(r"\bdocs/(\d\d):(\d+)(?:-(\d+))?")
SEPARATOR_RE = re.compile(r"^\|[\s\-:|]+\|$")
RULE_RE = re.compile(r"^-{3,}$")
PLACEHOLDER_RE = re.compile(r"(?<![A-Za-z])(TODO|FIXME|TBD|XXX|待补|待定|占位符)(?![A-Za-z])")
PINNED_SHA_RE = re.compile(r"[0-9a-f]{64}")

REQUIRED_PATHS = (
    "schema",
    "meta",
    "meta.real_data_only",
    "meta.self_sha_policy",
    "suite",
    "suite.total",
    "suite.fixed_denominator",
    "suite.families",
    "suite.difficulty",
    "case_schema",
    "case_schema.fields",
    "scoring",
    "scoring.denominator",
    "scoring.abstain",
    "scoring.candidate_selection",
    "scoring.stratification",
    "scoring.conclusion_discipline",
    "fill_plan",
    "fill_plan.roles",
    "fill_plan.per_field_owner",
    "source",
    "open_questions",
)

# 已知的改名嫌疑：这些名字一律不是契约字段（出现了就说明有人另发明了一套）
RENAME_SUSPECTS = (
    "case_id",
    "caseId",
    "testcase_id",
    "family",
    "layer",
    "expected_root_cause",
    "expected_layer",
    "expect_rootcause",
)

# 层别名（judge 内部词表的中文别名）：用例文件里出现别名 = 越界
LAYER_ALIAS_HINTS = ("应用层", "应用服务", "业务层", "服务层", "应用",
                     "运行时", "容器", "宿主机", "主机层", "Pod", "K8s",
                     "数据层", "数据库层", "数据库", "数据服务", "GaussDB", "存储层")


def _utf8_stdio() -> None:
    """R-49：宿主代码页 GBK，stdout/stderr 一律显式 UTF-8（W18 曾在此崩过）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _emit(status: str, code: str, detail: str = "") -> None:
    line = "[%-7s] %s" % (status, code)
    if detail:
        line += "  " + detail
    print(line)


def _dig(obj, dotted: str):
    """按点分路径取值；返回 (found, value)。"""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _walk_strings(obj, key_hint=None):
    """遍历对象里所有字符串，产出 (key_path, value)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = "%s.%s" % (key_hint, k) if key_hint else str(k)
            yield from _walk_strings(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, "%s[%d]" % (key_hint, i) if key_hint else "[%d]" % i)
    elif isinstance(obj, str):
        yield key_hint, obj


class Report:
    def __init__(self) -> None:
        self.fails: list[tuple[str, str]] = []
        self.blocks: list[tuple[str, str]] = []
        self.warns: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self.passes = 0

    def ok(self, code: str, detail: str = "") -> None:
        self.passes += 1
        _emit("PASS", code, detail)

    def fail(self, code: str, msg: str) -> None:
        self.fails.append((code, msg))
        _emit("FAIL", code, msg)

    def block(self, code: str, msg: str) -> None:
        self.blocks.append((code, msg))
        _emit("BLOCKED", code, msg)

    def warn(self, code: str, msg: str, strict: bool = False) -> None:
        if strict:
            self.fail(code, msg + "   [--strict 把 WARN 升级为 FAIL]")
        else:
            self.warns.append((code, msg))
            _emit("WARN", code, msg)

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print("         " + msg)


def _guard(rep: Report, name: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # 门禁自己崩了也是「没测到」，fail-closed
        rep.block("B-GATE-CHECK-CRASH", "%s 抛异常：%r" % (name, exc))
        return None


# ---------------------------------------------------------------------------
# 依赖装载
# ---------------------------------------------------------------------------
def load_w18():
    """装载 W18 的契约/判据模块；返回 (C, J, err)。"""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import importlib
        contract_mod = importlib.import_module("m5.contract")
        judge_mod = importlib.import_module("m5.judge")
        return contract_mod, judge_mod, None
    except Exception as exc:
        return None, None, repr(exc)


def load_yaml():
    try:
        import yaml
        return yaml, None
    except Exception as exc:
        return None, repr(exc)


# ---------------------------------------------------------------------------
# 出处引用
# ---------------------------------------------------------------------------
class RefChecker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.refs: list[tuple[str, Path, int, int | None]] = []
        self.problems: list[tuple[str, str]] = []
        self.cache: dict[str, list[str]] = {}
        self._short_cache: dict[str, Path | None] = {}

    def lines(self, path: Path):
        key = str(path)
        if key not in self.cache:
            try:
                self.cache[key] = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                self.cache[key] = []
        return self.cache[key]

    def resolve_short(self, nn: str):
        if nn in self._short_cache:
            return self._short_cache[nn]
        candidates = sorted((self.root / "docs").glob("%s-*.md" % nn))
        self._short_cache[nn] = candidates[0] if len(candidates) == 1 else None
        return self._short_cache[nn]

    def collect(self, obj) -> None:
        for where, text in _walk_strings(obj):
            for m in REF_EXT_RE.finditer(text):
                raw, a, b = m.group(1), int(m.group(2)), m.group(3)
                self.refs.append((raw, self.root / raw.replace("\\", "/"), a, int(b) if b else None))
            for m in REF_SHORT_RE.finditer(text):
                nn, a, b = m.group(1), int(m.group(2)), m.group(3)
                path = self.resolve_short(nn)
                if path is None:
                    self.problems.append(
                        ("E-REF-SHORTHAND", "简写引用 docs/%s 无法唯一解析到 docs/%s-*.md（%s）" % (nn, nn, where)))
                    continue
                self.refs.append(("docs/%s:%d" % (nn, a), path, a, int(b) if b else None))

    def verify(self, rep: Report, min_refs: int, strict: bool) -> None:
        for code, msg in self.problems:
            rep.fail(code, msg)
        seen: set[tuple[str, int]] = set()
        good: set[tuple[str, int]] = set()
        bad = 0
        for raw, path, a, b in self.refs:
            key = (raw, a)
            if key in seen:
                continue
            seen.add(key)
            broken = False
            if not path.is_file():
                bad += 1
                rep.fail("E-REF-FILE-MISSING", "引用指向不存在的文件：%s ⇒ %s" % (raw, path))
                continue
            lines = self.lines(path)
            for ln in ([a] if b is None else [a, b]):
                if ln < 1 or ln > len(lines):
                    bad += 1
                    broken = True
                    rep.fail("E-REF-LINE-RANGE", "引用行号越界：%s（该文件共 %d 行）" % (raw, len(lines)))
                    continue
                text = lines[ln - 1]
                if not text.strip():
                    bad += 1
                    broken = True
                    rep.fail("E-REF-BLANK-LINE", "引用指向空行：%s:%d" % (raw, ln))
                    continue
                if SEPARATOR_RE.match(text.strip()) or RULE_RE.match(text.strip()):
                    bad += 1
                    broken = True
                    rep.fail("E-REF-DECORATION", "引用指向表格分隔/水平线而非内容行：%s:%d" % (raw, ln))
            if not broken:
                good.add(key)
        total = len(good)
        if total < min_refs:
            rep.fail("E-REFS-BELOW-MIN",
                     "契约出处引用只有 %d 条可解析且非空（候选 %d、坏引用 %d），低于下限 --min-refs=%d（反恒真：检查到 0 条不算通过）"
                     % (total, len(seen), bad, min_refs))
        else:
            rep.ok("E-REF-RESOLVABLE", "出处引用 %d 条可解析（下限 %d）" % (total, min_refs))
        rep.note("refs=%d 候选 / %d 合格  bad=%d  min-refs=%d" % (len(seen), total, bad, min_refs))


# ---------------------------------------------------------------------------
# docs/06 live parse（契约不许自造数字）
# ---------------------------------------------------------------------------
def parse_doc06_families(lines) -> tuple[dict | None, int | None, str]:
    counts: dict[str, int] = {}
    total = None
    row_re = re.compile(r"^\|\s*\*\*C(\d)\s+[^*]*\*\*\s*\|\s*`([A-Z])-\*`\s*\|\s*(\d+)\s*\|")
    for i, text in enumerate(lines, start=1):
        m = row_re.match(text.strip())
        if m:
            counts["C%s" % m.group(1)] = int(m.group(3))
        if total is None:
            tm = re.match(r"^\|\s*\|\s*\|\s*\*\*(\d+)\*\*\s*\|", text.strip())
            if tm:
                total = int(tm.group(1))
    if len(counts) != 6:
        return None, total, "§1.1 族表只解析到 %d 行（应为 6）" % len(counts)
    return counts, total, ""


def parse_doc06_difficulty(lines) -> tuple[dict | None, dict | None, str]:
    counts: dict[str, int] = {}
    comps: dict[str, str] = {}
    row_re = re.compile(r"^\|\s*(easy|medium|hard)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*$")
    for text in lines:
        m = row_re.match(text.strip())
        if m:
            counts[m.group(1)] = int(m.group(2))
            comps[m.group(1)] = m.group(3)
    if len(counts) != 3:
        return None, None, "§1.3 难度表只解析到 %d 行（应为 3）" % len(counts)
    return counts, comps, ""


# ---------------------------------------------------------------------------
# 契约侧检查
# ---------------------------------------------------------------------------
def check_contract(rep: Report, contract, text: str, sha: str, root: Path, args, w18, w18j) -> None:
    # --- schema 值 ------------------------------------------------------
    schema = contract.get("schema")
    if schema != CONTRACT_SCHEMA:
        rep.fail("E-CONTRACT-SCHEMA", "契约 schema=%r，应为 %r" % (schema, CONTRACT_SCHEMA))
    else:
        rep.ok("E-CONTRACT-SCHEMA")

    # --- 不得 pin 自身滚动 sha -------------------------------------------
    pinned = []
    for line in text.splitlines():
        if PINNED_SHA_RE.search(line) and re.search(r"sha|滚动|哈希", line, re.I):
            pinned.append(line.strip()[:120])
    if pinned:
        rep.fail("E-CONTRACT-SELF-SHA",
                 "契约里出现带 sha/滚动 标签的 64 位十六进制串 ⇒ 不得 pin（%d 处）：%s" % (len(pinned), pinned[0]))
    else:
        rep.ok("E-CONTRACT-SELF-SHA", "未 pin 任何滚动 sha（sha_policy 已声明）")

    # --- 不得有占位符 ----------------------------------------------------
    hits = PLACEHOLDER_RE.findall(text)
    if hits:
        rep.fail("E-CONTRACT-PLACEHOLDER", "契约含占位符 %s ⇒ 用显式 open_question 表达缺失，不得写 TODO/待补" % sorted(set(hits)))
    else:
        rep.ok("E-CONTRACT-PLACEHOLDER")

    # --- suite：分母 -----------------------------------------------------
    found, denom = _dig(contract, "suite.fixed_denominator")
    if found and isinstance(denom, dict):
        if denom.get("value") != SUITE_TOTAL or denom.get("policy", "fixed") != "fixed":
            rep.fail("E-SUITE-DENOM", "suite.fixed_denominator=%r ⇒ 必须是固定分母 50" % denom.get("value"))
        else:
            rep.ok("E-SUITE-DENOM")

    found, total = _dig(contract, "suite.total")
    if found and total != SUITE_TOTAL:
        rep.fail("E-SUITE-TOTAL", "suite.total=%r，应为 %d" % (total, SUITE_TOTAL))
    elif found:
        rep.ok("E-SUITE-TOTAL")

    # --- suite：族构成 ---------------------------------------------------
    found, families = _dig(contract, "suite.families")
    fam_counts: dict[str, int] = {}
    if found and isinstance(families, list):
        codes = [f.get("code") for f in families if isinstance(f, dict)]
        if codes != list(FAMILY_ORDER):
            rep.fail("E-SUITE-FAMILY-ORDER", "suite.families 的族序/集合=%r，应为 %r" % (codes, list(FAMILY_ORDER)))
        for f in families:
            if isinstance(f, dict) and f.get("code") in FAMILY_ORDER:
                fam_counts[str(f["code"])] = f.get("count")
        if any(not isinstance(v, int) for v in fam_counts.values()):
            rep.fail("E-SUITE-FAMILY-TYPE", "族条数必须全是整数：%r" % fam_counts)
        else:
            if fam_counts != FAMILY_COUNTS:
                rep.fail("E-SUITE-FAMILY-COUNTS", "契约族构成=%r，应为 %r" % (fam_counts, FAMILY_COUNTS))
            elif sum(fam_counts.values()) != SUITE_TOTAL:
                rep.fail("E-SUITE-FAMILY-SUM", "族构成合计=%d，应为 %d" % (sum(fam_counts.values()), SUITE_TOTAL))
            else:
                rep.ok("E-SUITE-FAMILY-COUNTS", "族构成 %s 合计 %d" % (fam_counts, sum(fam_counts.values())))

    # --- suite：难度构成 -------------------------------------------------
    found, diff = _dig(contract, "suite.difficulty")
    diff_declared: dict[str, int] = {}
    if found and isinstance(diff, dict):
        diff_declared = dict(diff.get("declared_counts") or {})
        if diff_declared != DIFFICULTY_COUNTS:
            rep.fail("E-SUITE-DIFFICULTY-COUNTS", "契约难度声明=%r，应为 %r" % (diff_declared, DIFFICULTY_COUNTS))
        elif sum(diff_declared.values()) != SUITE_TOTAL:
            rep.fail("E-SUITE-DIFFICULTY-SUM", "难度声明合计=%d，应为 %d" % (sum(diff_declared.values()), SUITE_TOTAL))
        else:
            rep.ok("E-SUITE-DIFFICULTY-COUNTS", "难度声明 %s 合计 %d" % (diff_declared, sum(diff_declared.values())))

    # --- 与 W18 常量交叉核对（唯一真相源）---------------------------------
    if w18 is None:
        rep.block("B-W18-CONTRACT-MODULE",
                  "无法装载 shared/verify/m5/contract.py ⇒ 无法交叉核对字段与构成常量：%s" % (args._w18_err,))
    else:
        if fam_counts and fam_counts != dict(w18.FAMILY_COUNTS_EXPECTED):
            rep.fail("E-SUITE-FAMILY-DRIFT",
                     "契约族构成与 W18 FAMILY_COUNTS_EXPECTED 不一致：%r vs %r" % (fam_counts, w18.FAMILY_COUNTS_EXPECTED))
        else:
            rep.ok("E-W18-FAMILY-ALIGN")
        if diff_declared and diff_declared != dict(w18.DIFFICULTY_COUNTS_EXPECTED):
            rep.fail("E-SUITE-DIFFICULTY-DRIFT",
                     "契约难度声明与 W18 DIFFICULTY_COUNTS_EXPECTED 不一致：%r vs %r" % (diff_declared, w18.DIFFICULTY_COUNTS_EXPECTED))
        else:
            rep.ok("E-W18-DIFFICULTY-ALIGN")
        found, layers = _dig(contract, "meta.layer_semantics.layers")
        if found:
            if w18j is None:
                rep.block("B-W18-JUDGE-MODULE", "无法装载 shared/verify/m5/judge.py ⇒ 层词表无法核对：%s" % (args._w18_err,))
            elif list(layers or []) != list(getattr(w18j, "LAYER_CODES", ()) or ()):
                rep.fail("E-LAYER-VOCAB-DRIFT",
                         "契约层词表=%r 与 W18 judge LAYER_CODES=%r 不一致" % (layers, list(getattr(w18j, "LAYER_CODES", ()))))
            else:
                rep.ok("E-LAYER-VOCAB-ALIGN", "层词表与 judge LAYER_CODES 一致：%s" % (layers,))

    # --- docs/06 live parse ---------------------------------------------
    doc06 = None
    candidates = sorted((root / "docs").glob("06-*.md"))
    if len(candidates) != 1:
        rep.block("B-DOC06", "无法唯一定位 docs/06-*.md（找到 %d 个）⇒ 契约里的数字无法与正文核对" % len(candidates))
    else:
        doc06 = candidates[0]
    if doc06 is not None:
        lines = doc06.read_text(encoding="utf-8").splitlines()
        live_fam, live_total, err = parse_doc06_families(lines)
        if live_fam is None:
            rep.block("B-DOC06-PARSE", "docs/06 §1.1 族表解析失败：%s" % err)
        else:
            if live_total != SUITE_TOTAL:
                rep.fail("E-DOC-SUITE-TOTAL", "docs/06 §1.1 正文总数=%r，应为 %d" % (live_total, SUITE_TOTAL))
            if fam_counts and live_fam != fam_counts:
                rep.fail("E-DOC-FAMILY-DRIFT", "契约族构成=%r 与 docs/06 正文=%r 不符（数字必须取自正文）" % (fam_counts, live_fam))
            else:
                rep.ok("E-DOC-FAMILY-ALIGN", "docs/06 正文族构成与契约一致：%s" % live_fam)
        live_diff, live_comps, err2 = parse_doc06_difficulty(lines)
        if live_diff is None:
            rep.block("B-DOC06-PARSE", "docs/06 §1.3 难度表解析失败：%s" % err2)
        else:
            if diff_declared and live_diff != diff_declared:
                rep.fail("E-DOC-DIFFICULTY-DRIFT", "契约难度声明=%r 与 docs/06 正文=%r 不符" % (diff_declared, live_diff))
            else:
                rep.ok("E-DOC-DIFFICULTY-ALIGN", "docs/06 正文难度声明与契约一致：%s" % live_diff)
            # 登记构成文字必须逐字来自正文
            if isinstance(diff, dict):
                for tier in diff.get("tiers") or []:
                    if not isinstance(tier, dict):
                        continue
                    code = tier.get("code")
                    if code in (live_comps or {}) and str(tier.get("composition", "")).strip() != live_comps[code]:
                        rep.fail("E-DOC-COMPOSITION-DRIFT",
                                 "契约 %s 的构成文字=%r 与 docs/06 正文=%r 不符（必须逐字照抄）"
                                 % (code, tier.get("composition"), live_comps.get(code)))
            # 复算：声明 ≠ 构成算式
            if isinstance(diff, dict) and fam_counts:
                rec = diff.get("recompute")
                if not isinstance(rec, dict):
                    rep.fail("E-RECOMPUTE-MISSING", "suite.difficulty.recompute 缺失 ⇒ docs/06 §1.3 的不自洽未被登记")
                else:
                    computed: dict[str, int] = {}
                    for op in rec.get("operands") or []:
                        if not isinstance(op, dict):
                            continue
                        tier = str(op.get("tier"))
                        kind = op.get("kind")
                        if kind == "family_all":
                            val = fam_counts.get(str(op.get("family")))
                        elif kind == "chain_subset":
                            chains = op.get("chains") or []
                            per = op.get("per_chain")
                            val = (len(chains) * per) if isinstance(per, int) else None
                        else:
                            rep.fail("E-RECOMPUTE-OP-KIND", "登记算式的 operand kind=%r 不可复算" % kind)
                            val = None
                        if val is None:
                            rep.fail("E-RECOMPUTE-OP-VALUE", "登记算式 operand %r 无法求值" % op.get("term"))
                            continue
                        if op.get("value") != val:
                            rep.fail("E-RECOMPUTE-OP-DRIFT",
                                     "登记算式 %s：登记值=%r 复算值=%r" % (op.get("term"), op.get("value"), val))
                        computed[tier] = computed.get(tier, 0) + val
                    for tier, val in computed.items():
                        declared = diff_declared.get(tier)
                        if declared is not None and val == declared:
                            rep.fail("E-RECOMPUTE-STALE",
                                     "复算 %s=%d 与声明 %d 相等 ⇒ 契约登记的 docs/06 §1.3 不自洽（OQ-M5-01）已过期，需更新契约"
                                     % (tier, val, declared))
                    if computed:
                        rep.note("docs/06 §1.3 复算：%s ｜ 声明：%s（两者不等即 OQ-M5-01 成立）"
                                 % (computed, diff_declared))
                        if sum(computed.values()) != SUITE_TOTAL:
                            rep.fail("E-RECOMPUTE-TOTAL", "复算合计=%d，应为 %d" % (sum(computed.values()), SUITE_TOTAL))
                        else:
                            rep.ok("E-RECOMPUTE-DIVERGENCE", "复算与声明不等且合计均为 50（OQ-M5-01 成立）")

    # --- case_schema -----------------------------------------------------
    found, fields = _dig(contract, "case_schema.fields")
    field_names: list[str] = []
    if found and isinstance(fields, list):
        field_names = [str(f.get("name")) for f in fields if isinstance(f, dict)]
        dups = sorted({n for n in field_names if field_names.count(n) > 1})
        if dups:
            rep.fail("E-CASE-SCHEMA-DUP-FIELD", "case_schema.fields 有重名字段：%s" % dups)
        suspects = [n for n in field_names if n in RENAME_SUSPECTS]
        if suspects:
            rep.fail("E-CASE-SCHEMA-RENAMED", "case_schema 出现非契约字段名（疑似改名/另发明一套）：%s" % suspects)
        if w18 is not None:
            missing = [n for n in w18.CASE_FIELDS if n not in field_names]
            if missing:
                rep.fail("E-CASE-SCHEMA-MISSING-FIELDS",
                         "case_schema 缺 W18 CASE_FIELDS 里的既有字段：%s（既有字段名一律沿用，不得改名/删除）" % missing)
            else:
                rep.ok("E-CASE-SCHEMA-FIELDS", "18 个既有字段逐字齐全")
        # expect_layer 必须显式
        el = next((f for f in fields if isinstance(f, dict) and f.get("name") == "expect_layer"), None)
        if el is None:
            rep.fail("E-CASE-SCHEMA-EXPECT-LAYER", "case_schema 缺 expect_layer（队长裁定：用例文件须带显式期望层别）")
        elif el.get("required") is not True or el.get("explicit") is not True:
            rep.fail("E-CASE-SCHEMA-EXPECT-LAYER",
                     "expect_layer 必须 required=true 且 explicit=true（当前 required=%r explicit=%r）"
                     % (el.get("required"), el.get("explicit")))
        else:
            rep.ok("E-CASE-SCHEMA-EXPECT-LAYER", "expect_layer 显式且必填")
        # 每个字段必须标注出处与填表人
        bad_prov = [str(f.get("name")) for f in fields if isinstance(f, dict)
                    and not str(f.get("introduced_by") or "").strip()]
        bad_owner = [str(f.get("name")) for f in fields if isinstance(f, dict)
                     and not str(f.get("filled_by") or "").strip()]
        if bad_prov:
            rep.fail("E-CASE-SCHEMA-PROVENANCE", "字段缺 introduced_by（无法区分既有字段 vs 缺口）：%s" % bad_prov)
        else:
            rep.ok("E-CASE-SCHEMA-PROVENANCE", "每个字段都标了 introduced_by")
        if bad_owner:
            rep.fail("E-CASE-SCHEMA-OWNER", "字段缺 filled_by（三权分立无法判定）：%s" % bad_owner)
        else:
            rep.ok("E-CASE-SCHEMA-OWNER", "每个字段都标了 filled_by")
        # 契约引入的新字段不得是既有 18 字段之一
        found, newf = _dig(contract, "case_schema.contract_introduced_fields.fields")
        if found and isinstance(newf, list):
            names = [str(f.get("name")) for f in newf if isinstance(f, dict)]
            if w18 is not None:
                mis = [n for n in names if n in w18.CASE_FIELDS]
                if mis:
                    rep.fail("E-CASE-SCHEMA-NEWFIELD-MISLABELED",
                             "这些字段被标为「本契约引入」，其实是 W18 既有字段：%s" % mis)
                else:
                    rep.ok("E-CASE-SCHEMA-NEWFIELDS", "新字段标注名符其实：%s" % names)
            if any(not str(f.get("introduced_by") or "").strip() for f in newf if isinstance(f, dict)):
                rep.fail("E-CASE-SCHEMA-NEWFIELD-PROV", "新增字段缺 introduced_by")
        else:
            rep.fail("E-CASE-SCHEMA-NEWFIELDS-MISSING",
                     "缺 case_schema.contract_introduced_fields ⇒ 既有字段 vs 缺口无法区分")

    # --- scoring ---------------------------------------------------------
    found, denom = _dig(contract, "scoring.denominator")
    if found and isinstance(denom, dict):
        if denom.get("policy") != "fixed" or denom.get("value") != SUITE_TOTAL:
            rep.fail("E-SCORING-DENOM", "scoring.denominator=%r ⇒ 必须是固定分母 50" % denom)
        else:
            rep.ok("E-SCORING-DENOM")
    found, ab = _dig(contract, "scoring.abstain")
    if found and isinstance(ab, dict):
        main_axis = ab.get("main_axis")
        sens = ab.get("sensitivity")
        if not isinstance(main_axis, dict) or not str(main_axis.get("treatment") or "").strip():
            rep.fail("E-SCORING-ABSTAIN", "scoring.abstain.main_axis 缺失或空 ⇒ abstain 单列口径未声明")
        elif not isinstance(sens, dict) or sens.get("required") is not True:
            rep.fail("E-SCORING-ABSTAIN-SENS", "scoring.abstain.sensitivity.required 必须为 true（abstain 计错的敏感性分析必做）")
        else:
            rep.ok("E-SCORING-ABSTAIN", "abstain 单列 + 敏感性分析已声明")
    found, cand = _dig(contract, "scoring.candidate_selection")
    if found and isinstance(cand, dict) and str(cand.get("rule") or "").strip():
        rep.ok("E-SCORING-CANDIDATE")
    else:
        rep.fail("E-SCORING-CANDIDATE", "scoring.candidate_selection.rule 缺失 ⇒ 只取第一个候选的口径未声明")
    found, strat = _dig(contract, "scoring.stratification")
    if found and isinstance(strat, dict):
        tiers = list(strat.get("tiers") or [])
        fams = list(strat.get("families") or [])
        if strat.get("required") is not True or tiers != list(DIFFICULTY_ORDER) or fams != list(FAMILY_ORDER):
            rep.fail("E-SCORING-STRATIFICATION", "scoring.stratification=%r ⇒ 分层与分族必须分别报告" % strat)
        else:
            rep.ok("E-SCORING-STRATIFICATION")
    found, disc = _dig(contract, "scoring.conclusion_discipline")
    if found and isinstance(disc, dict):
        if disc.get("min_pp") != MIN_PP:
            rep.fail("E-SCORING-MINPP", "scoring.conclusion_discipline.min_pp=%r，应为 %d" % (disc.get("min_pp"), MIN_PP))
        elif "未检出显著差异" not in str(disc.get("under_20pp", {}).get("required_phrasing", "")):
            rep.fail("E-SCORING-MINPP-PHRASING", "缺「未检出显著差异」的规定措辞 ⇒ 报告纪律不完整")
        else:
            rep.ok("E-SCORING-MINPP", "只对 >=20pp 下结论 + 规定措辞已声明")
    found, stats = _dig(contract, "scoring.statistics")
    if found and isinstance(stats, dict):
        bs = stats.get("bootstrap") or {}
        mult = stats.get("multiplicity") or {}
        if bs.get("resamples") != 10000:
            rep.fail("E-SCORING-BOOTSTRAP", "bootstrap.resamples=%r，应为 10000" % bs.get("resamples"))
        elif mult.get("families") != 6:
            rep.fail("E-SCORING-BONFERRONI", "multiplicity.families=%r，应为 6 族" % mult.get("families"))
        else:
            rep.ok("E-SCORING-STATS", "bootstrap 10000 + Bonferroni 6 族")

    # --- fill_plan：三权分立 ---------------------------------------------
    found, roles = _dig(contract, "fill_plan.roles")
    if found and isinstance(roles, dict):
        missing_roles = [r for r in ("injector", "judge", "runner") if r not in roles]
        if missing_roles:
            rep.fail("E-FILLPLAN-ROLE-MISSING", "fill_plan.roles 缺角色 %s" % missing_roles)
        else:
            rep.ok("E-FILLPLAN-ROLES", "注入方/判据器/运行器三角色齐备")
        for rname in ("injector", "judge", "runner"):
            r = roles.get(rname)
            if not isinstance(r, dict):
                continue
            fills = list(r.get("fills") or []) + list(r.get("fills_runtime") or [])
            if not fills:
                rep.fail("E-FILLPLAN-EMPTY-FILLS", "角色 %s 未声明 fills/fills_runtime" % rname)
            if not (r.get("does_not_fill") is not None):
                rep.fail("E-FILLPLAN-NO-EXCLUSION", "角色 %s 未声明 does_not_fill（谁不填必须写清）" % rname)
    else:
        rep.fail("E-FILLPLAN-ROLES", "fill_plan.roles 缺失或不是映射")
    found, owner = _dig(contract, "fill_plan.per_field_owner")
    if found and isinstance(owner, dict) and field_names:
        claims: dict[str, list[str]] = {}
        for rname, names in owner.items():
            if not isinstance(names, list):
                rep.fail("E-FILLPLAN-OWNER-SHAPE", "per_field_owner.%s 不是列表" % rname)
                continue
            for n in names:
                claims.setdefault(str(n), []).append(str(rname))
        uncovered = [n for n in field_names if n not in claims]
        multi = {n: rs for n, rs in claims.items() if len(rs) > 1}
        if uncovered:
            rep.fail("E-FILLPLAN-UNCOVERED", "这些用例字段无人认领：%s" % uncovered)
        elif multi:
            rep.fail("E-FILLPLAN-MULTI-OWNER", "这些字段被多个角色认领（三权分立被破坏）：%s" % multi)
        else:
            rep.ok("E-FILLPLAN-COVERAGE", "%d 个用例字段各有一个唯一责任角色" % len(field_names))

    # --- open_questions --------------------------------------------------
    found, oqs = _dig(contract, "open_questions")
    if found and isinstance(oqs, list) and oqs:
        ids = [o.get("id") for o in oqs if isinstance(o, dict)]
        if "OQ-M5-01" not in ids:
            rep.fail("E-OQ-REGISTRY", "open_questions 缺 OQ-M5-01（docs/06 §1.3 族→难度构成不自洽必须登记）")
        elif any(not str(o.get("question") or "").strip() for o in oqs if isinstance(o, dict)):
            rep.fail("E-OQ-SHAPE", "open_questions 有条目缺 question")
        else:
            rep.ok("E-OQ-REGISTRY", "开放问题 %d 条，含 OQ-M5-01" % len(ids))
    else:
        rep.fail("E-OQ-REGISTRY", "open_questions 为空或缺 ⇒ 已知不自洽无处登记")


# ---------------------------------------------------------------------------
# 用例侧检查
# ---------------------------------------------------------------------------
def check_cases(rep: Report, contract, contract_path: Path, cases_name: str, args, w18, w18j, fm) -> tuple[Path | None, int]:
    """返回 (实际使用的用例路径或 None, 用例条数)。"""
    if cases_name is None:
        rep.block("B-CASES-NOT-RECORDED", "契约里没有 case_file.default_path，也没有 --cases ⇒ 无法定位用例文件")
        return None, 0
    path = Path(cases_name)
    if not path.is_absolute():
        path = (args._root / path)
    if not path.is_file():
        rep.block("B-CASES-NOT-RECORDED", "契约已就位、用例尚未录制：%s" % path)
        _print_missing_brief(contract)
        return None, 0
    if w18 is None:
        rep.block("B-W18-CONTRACT-MODULE", "无法装载 shared/verify/m5/contract.py ⇒ 无法校验用例文件")
        return path, 0

    raw_cases, meta, errors = w18.load_case_document(path)
    for e in errors:
        rep.fail("E-CASES-LOAD", e.as_dict().get("message", str(e)))
    if errors:
        return path, 0
    if not isinstance(meta, dict):
        meta = {}

    # ★ W46 缺陷③：权威前缀表（唯一真相源 = shared/model/ccf.schema.json）。
    # fail-closed：读不到 ⇒ BLOCKED（不测 ≠ 测出缺陷），绝不用内置退路表蒙过去。
    prefix_table, prefix_err = w18.load_id_prefixes(args._root)
    if prefix_table is None:
        rep.block("B-CASES-ID-PREFIX-TABLE",
                  "读不到权威前缀表 %s#%s ⇒ 无法判 expect_objects 前缀合法性：%s"
                  % ("/".join(w18.ID_PREFIX_SCHEMA_REL), w18.ID_PREFIX_BY_TYPE_KEY, prefix_err))
        return path, 0
    rep.ok("E-CASES-ID-PREFIX-TABLE",
           "权威前缀表 %s 项：%s" % (len(prefix_table), sorted(prefix_table.values())))
    prefix_ok: set[str] = {str(v).lower() for v in prefix_table.values()}   # 表的值 = 前缀
    if set(prefix_table.values()) != set(w18.ID_PREFIXES_EXPECTED):
        rep.note("权威前缀表与 W45 冻结的 12 类期望值不同（表=%s / 期望=%s）⇒ 以表为准，仅供人工复核"
                 % (sorted(prefix_table.values()), sorted(w18.ID_PREFIXES_EXPECTED)))

    # 顶层 meta 契约键
    for key in ("schema", "synthetic", "snapshot_binding", "snapshot_ids", "fault_injection_ledger"):
        if key not in meta:
            rep.fail("E-CASES-META-KEY", "用例文件顶层 meta 缺键 %r（契约 case_schema.case_level_meta_required）" % key)
    if meta.get("schema") != CASES_SCHEMA:
        rep.fail("E-CASES-META-SCHEMA", "meta.schema=%r，应为 %r" % (meta.get("schema"), CASES_SCHEMA))
    else:
        rep.ok("E-CASES-META-SCHEMA")
    binding = meta.get("snapshot_binding")
    if binding not in ("cli_only", "case_field", "mixed"):
        rep.fail("E-CASES-SNAPSHOT-BINDING", "meta.snapshot_binding=%r，应为 cli_only/case_field/mixed" % binding)

    # 合成禁令 / 夹具模式
    synthetic = bool(meta.get("synthetic"))
    if args.allow_synthetic_fixture:
        if not synthetic:
            rep.fail("E-FIXTURE-MODE-MISUSE", "--allow-synthetic-fixture 只允许用于夹具，但 meta.synthetic 不是 true")
        else:
            rep.ok("E-FIXTURE-MODE", "夹具模式：meta.synthetic=true")
    elif synthetic:
        rep.fail("E-CASES-SYNTHETIC-FORBIDDEN",
                 "meta.synthetic=true ⇒ 正式判决集不得是合成数据（docs/06:124-135 / 用户裁定 m00736）")
    ledger = meta.get("fault_injection_ledger")
    if ledger:
        lp = Path(str(ledger))
        if not lp.is_absolute():
            lp = args._root / lp
        if not lp.is_file():
            rep.warn("W-CASES-LEDGER-ABSENT", "meta.fault_injection_ledger 指向的文件当前不存在：%s" % lp, args.strict)

    # W18 逐条字段校验（构成断言由本门禁自己做，避免重复报错）
    cases, verrors, vwarns = w18.validate_cases(
        raw_cases, meta, expect_full_suite=False, strict=False)

    # ---------------- ★ M5-GA：gold_audit（双人复核台账）机检（2026-10-09 新增） ----------------
    #   依据：docs/06:112-115「A 只拿注入清单写 expect_*；B 只拿 query + CCF 独立给根因；
    #   两者不一致 ⇒ 该用例作废或修正，并记入 gold_audit 台账」+
    #   m5-cases.yaml#case_schema.gold_audit_schema（逐用例字段；形状早由
    #   contract_introduced_fields.gold_audit 的 fields_suggested 定义）。
    #   ★ 判据只机检**可机检的部分**：键完整性 / 独立性声明 / agreed 与两方裁决一致 /
    #     不一致必须写 resolution；**人工位未填** ⇒ WARN（已声明欠账），不得静默通过。
    _ga_need = ("annotator_a", "annotator_b", "root_cause_b", "agreed", "resolution", "ts_utc")
    _ga_meta = {}
    _ga_path = meta.get("gold_audit")
    if _ga_path:
        _gp = Path(_ga_path)
        if not _gp.is_absolute():
            _gp = Path.cwd() / _gp
        if _gp.is_file():
            try:
                _ga_meta = (yaml.safe_load(_gp.read_text(encoding="utf-8")) or {}).get("meta") or {}
            except Exception as exc:  # noqa: BLE001
                rep.fail("M5-GA-LEDGER-UNREADABLE", "gold_audit 台账无法解析：%s" % exc)
        else:
            rep.fail("M5-GA-LEDGER-MISSING", "meta.gold_audit 指向的台账不存在：%s" % _ga_path)
    _ga_cases = cases if isinstance(cases, list) else []
    _ga_pending = 0
    for _c in _ga_cases:
        _cid = _c.get("id")
        _ga = _c.get("gold_audit")
        if not isinstance(_ga, dict):
            rep.fail("M5-GA-MISSING", "%s：缺 gold_audit（或不是 mapping）⇒ 无法复核双人复核" % _cid)
            continue
        _miss = [k for k in _ga_need if k not in _ga]
        if _miss:
            rep.fail("M5-GA-KEYS", "%s：gold_audit 缺键 %s（契约 gold_audit_schema.required_case_keys）" % (_cid, _miss))
        _mc = _ga.get("machine_check") or {}
        _a = (_mc.get("a") or {}); _b = (_mc.get("b") or {})
        _sa, _sb = _a.get("sees"), _b.get("sees")
        if not _sa or not _sb or _sa == _sb:
            rep.fail("M5-GA-INDEPENDENCE",
                     "%s：机器证据两方的输入面必须不同（A=%r / B=%r）⇒ 否则是伪独立复核" % (_cid, _sa, _sb))
        _va = sorted(_a.get("verdict") or []); _vb = sorted(_b.get("verdict") or [])
        if bool(_ga.get("agreed")) != (_va == _vb):
            rep.fail("M5-GA-AGREE-CONSISTENCY",
                     "%s：agreed=%r 与两方裁决不一致（A=%s / B=%s）⇒ 不得自报" % (_cid, _ga.get("agreed"), _va, _vb))
        if _ga.get("agreed") is False and not _ga.get("resolution"):
            rep.fail("M5-GA-RESOLUTION", "%s：agreed=false 但 resolution 为空 ⇒ 未写清作废/修正" % _cid)
        if _ga.get("annotator_a") is None or _ga.get("annotator_b") is None:
            _ga_pending += 1
        elif isinstance(_ga.get("annotator_a"), dict) and isinstance(_ga.get("annotator_b"), dict) \
                and _ga["annotator_a"].get("id") and _ga["annotator_a"].get("id") == _ga["annotator_b"].get("id"):
            rep.fail("M5-GA-SAME-PERSON", "%s：annotator_a 与 annotator_b 是同一人 ⇒ 不构成双人复核" % _cid)
    _hr = (_ga_meta or {}).get("human_review") or {}
    if _ga_pending:
        if _hr.get("status") == "waived":
            if not _hr.get("waived_by") or not _hr.get("limitation"):
                rep.fail("M5-GA-WAIVER-INCOMPLETE",
                         "human_review.status=waived 必须写明 waived_by 与 limitation（否则等于无声放弃复核）")
            else:
                rep.note("M5-GA-HUMAN-WAIVED：人工双人复核**已由裁定豁免**（%s）；"
                         "局限已登记（%s）" % (_hr.get("waived_by"), str(_hr.get("limitation"))[:80]))
        else:
            rep.warn("M5-GA-HUMAN-PENDING",
                     "%d/%d 条用例的**人工**双人复核位未填（机器证据不能替代人工；docs/06:112-115）⇒ 已声明欠账"
                     % (_ga_pending, len(_ga_cases)))
    elif _ga_cases:
        rep.ok("M5-GA-HUMAN", "复核位齐备（人工或已豁免的机器角色）且非同一角色/人")
    if _ga_cases:
        rep.note("gold_audit：%d 条逐用例台账已机检（键/独立性/一致性/resolution）" % len(_ga_cases))
    for e in verrors:
        d = e.as_dict()
        rep.fail("E-CASES-FIELD", "%s%s" % (d.get("code"), ("：" + str(d.get("message"))) if d.get("message") else ""))
    for e in vwarns:
        d = e.as_dict()
        rep.warn("W-CASES-%s" % d.get("code"), str(d.get("message")), args.strict)

    n = len(cases)
    min_cases = args.min_cases if args.min_cases is not None else (1 if args.partial else SUITE_TOTAL)
    if n < min_cases:
        rep.fail("E-CASES-BELOW-MIN", "用例条数=%d，低于下限 %d（反恒真：0 条不算通过）" % (n, min_cases))
    else:
        rep.ok("E-CASES-COUNT-MIN", "用例条数=%d（下限 %d）" % (n, min_cases))

    # id 唯一
    ids = [c.get("id") for c in cases if isinstance(c, dict)]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        rep.fail("E-CASES-DUP-ID", "用例 id 重复：%s" % dups)
    else:
        rep.ok("E-CASES-ID-UNIQUE", "%d 个 id 唯一" % len(ids))

    # 族/难度分布
    fam_tally: dict[str, int] = {}
    diff_tally: dict[str, int] = {}
    for c in cases:
        if not isinstance(c, dict):
            continue
        fam = str(c.get("_family") or "")
        fam_tally[fam] = fam_tally.get(fam, 0) + 1
        d = str(c.get("difficulty") or "")
        diff_tally[d] = diff_tally.get(d, 0) + 1
    if not args.partial:
        if n != SUITE_TOTAL:
            rep.fail("E-CASES-SUITE-TOTAL", "用例条数=%d，契约要求 %d" % (n, SUITE_TOTAL))
        if {k: v for k, v in fam_tally.items()} != FAMILY_COUNTS:
            rep.fail("E-CASES-FAMILY-TALLY", "用例族分布=%r，契约要求 %r" % (fam_tally, FAMILY_COUNTS))
        else:
            rep.ok("E-CASES-FAMILY-TALLY", "族分布 %s" % fam_tally)
        if diff_tally != DIFFICULTY_COUNTS:
            rep.fail("E-CASES-DIFFICULTY-TALLY", "用例难度分布=%r，契约要求 %r" % (diff_tally, DIFFICULTY_COUNTS))
        else:
            rep.ok("E-CASES-DIFFICULTY-TALLY", "难度分布 %s" % diff_tally)
    else:
        rep.note("--partial：不断言 50 条与族/难度分布（族分布 %s ｜ 难度分布 %s）" % (fam_tally, diff_tally))

    # C4/C5 必须存在
    if fam_tally.get("C4", 0) < 1:
        rep.fail("E-CASES-NO-C4", "用例集没有族C4（N-*，否定性证据）⇒ 排除法无从考；契约 anti_cheat 要求 C4 与 C5 都存在")
    if fam_tally.get("C5", 0) < 1:
        rep.fail("E-CASES-NO-C5", "用例集没有族C5（Z-*，正常态）⇒ 无法考「无故障时不得虚构」")
    if fam_tally.get("C4", 0) >= 1 and fam_tally.get("C5", 0) >= 1:
        rep.ok("E-CASES-C4-C5", "族C4=%d 族C5=%d 均存在" % (fam_tally.get("C4", 0), fam_tally.get("C5", 0)))

    # 词表
    fm_codes: set[str] = set()
    fm_chains: set[str] = set()
    fm_layer_by_code: dict[str, str] = {}
    if fm is None:
        rep.block("B-FAULT-MODES", "shared/model/fault-modes.yaml 不可读 ⇒ oracle 词表无法核对")
    else:
        for item in fm.get("fault_modes") or []:
            if isinstance(item, dict) and item.get("code"):
                fm_codes.add(str(item["code"]))
                fm_layer_by_code[str(item["code"])] = str(item.get("layer") or "")
                for ch in item.get("chain") or []:
                    fm_chains.add(str(ch))
        if not fm_codes:
            rep.block("B-FAULT-MODES-EMPTY", "fault-modes.yaml 里读不到任何故障模式 code")
        else:
            rep.ok("E-FAULT-MODES-VOCAB", "故障模式词表 %d 个 code、%d 条链" % (len(fm_codes), len(fm_chains)))

    layer_codes = list(getattr(w18j, "LAYER_CODES", ()) or ())
    snapshots: set[str] = set()
    prefixes: set[str] = set()
    for c in cases:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id"))
        cls = str(c.get("cls"))
        is_z = cls == PREFIX_BY_FAMILY["C5"]

        # 显式期望层别
        layer = c.get("expect_layer")
        if not isinstance(layer, str) or not layer.strip():
            rep.fail("E-CASES-EXPECT-LAYER-EMPTY", "%s：expect_layer 为空 ⇒ 用例文件必须带显式期望层别" % cid)
        elif layer_codes and layer not in layer_codes and layer != NONE_SENTINEL:
            visible = layer in LAYER_ALIAS_HINTS
            rep.fail("E-CASES-EXPECT-LAYER-VOCAB",
                     "%s：expect_layer=%r 不在 %s ∪ {%s}%s"
                     % (cid, layer, layer_codes, NONE_SENTINEL,
                        "（这是 judge 的中文别名，用例文件只许用层码）" if visible else ""))
        elif layer == NONE_SENTINEL and not is_z:
            rep.fail("E-CASES-LAYER-NONE-OUTSIDE-C5", "%s：expect_layer=NONE 只许族C5（正常态）" % cid)

        # oracle 判据非空 + 词表
        rc = c.get("expect_root_cause")
        if not isinstance(rc, str) or not rc.strip():
            rep.fail("E-CASES-ORACLE-EMPTY", "%s：expect_root_cause 为空 ⇒ oracle 判据不可用" % cid)
        elif fm_codes:
            if rc != NONE_SENTINEL and rc not in fm_codes:
                rep.fail("E-CASES-ORACLE-VOCAB", "%s：expect_root_cause=%r 不在 fault-modes 词表内" % (cid, rc))
            if rc == NONE_SENTINEL and not is_z:
                rep.fail("E-CASES-NONE-OUTSIDE-C5", "%s：NONE 哨兵只许族C5" % cid)
        if not isinstance(c.get("detectors"), list) or not c.get("detectors"):
            rep.fail("E-CASES-DETECTORS-EMPTY", "%s：detectors 为空 ⇒ 无法机判" % cid)
        injected = c.get("injected")
        if not isinstance(injected, list):
            rep.fail("E-CASES-INJECTED-TYPE", "%s：injected 不是列表" % cid)
        else:
            if not injected and not is_z:
                rep.fail("E-CASES-INJECTED-EMPTY", "%s：非族C5 用例的 injected 为空 ⇒ 无注入事实可派生真值" % cid)
            for code in injected:
                if fm_codes and str(code) not in fm_codes:
                    rep.fail("E-CASES-INJECTED-VOCAB", "%s：injected 含词表外 code %r" % (cid, code))
        epath = c.get("expect_path")
        if isinstance(epath, list):
            for node in epath:
                if fm_codes and str(node) not in fm_codes and str(node) != NONE_SENTINEL:
                    rep.fail("E-CASES-PATH-VOCAB", "%s：expect_path 含词表外节点 %r" % (cid, node))
        chain = c.get("chain")
        if fm_chains and isinstance(chain, str) and chain not in fm_chains:
            rep.fail("E-CASES-CHAIN-VOCAB", "%s：chain=%r 不在 fault-modes 出现的链集合 %s" % (cid, chain, sorted(fm_chains)))

        # 快照绑定
        snap = c.get("snapshot_id")
        if snap is not None:
            if not isinstance(snap, str) or not SNAPSHOT_RE.match(snap):
                rep.fail("E-CASES-SNAPSHOT-FORMAT",
                         "%s：snapshot_id=%r 不匹配 ^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$（docs/02:830）" % (cid, snap))
            else:
                snapshots.add(snap)
        if binding in ("case_field", "mixed") and not snap:
            rep.fail("E-CASES-SNAPSHOT-MISSING", "%s：meta.snapshot_binding=%s 但该条没有 snapshot_id" % (cid, binding))

        # expect_objects 口径（W46 缺陷③）：只许**权威前缀表**内的对象 id
        # （shared/model/ccf.schema.json#x-id-prefix-by-type，12 类）；
        # Evidence id（真机形态 obs-NNNNNN，无冒号）走 evidence_facts 栏，不得混入本栏。
        objs = c.get("expect_objects")
        if isinstance(objs, list):
            for o in objs:
                so = str(o)
                if not OBJECT_RE.match(so):
                    hint = ""
                    if w18 is not None and w18.EVIDENCE_ID_RE.match(so):
                        hint = ("；该形态是 Evidence id ⇒ 应写在 evidence_facts 栏"
                                "（OQ-M5-05 裁定：expect_objects 只许 12 类对象 id）")
                    rep.fail("E-CASES-OBJECT-SHAPE",
                             "%s：expect_objects 项 %r 不是 <prefix>:<local> 形态%s" % (cid, o, hint))
                elif so.split(":", 1)[0].lower() not in prefix_ok:
                    rep.fail("E-CASES-OBJECT-UNKNOWN",
                             "%s：expect_objects 项 %r 的前缀 %r 不在权威前缀表 %s（%s#%s，W46 起硬判；"
                             "旧 W-CASES-OBJECT-PREFIX 的 WARN 前提「无权威前缀表」已被证伪）"
                             % (cid, o, so.split(":", 1)[0], sorted(prefix_ok),
                                "/".join(w18.ID_PREFIX_SCHEMA_REL), w18.ID_PREFIX_BY_TYPE_KEY))
                else:
                    prefixes.add(so.split(":", 1)[0].lower())

        # 溯源锚点
        anchor = c.get("source_injection_id")
        if not isinstance(anchor, str) or not anchor.strip():
            rep.fail("E-CASES-ANCHOR-MISSING",
                     "%s：缺 source_injection_id ⇒ 无法区分真实注入派生与编造（契约新字段）" % cid)
        elif not INJECTION_RE.match(anchor):
            rep.fail("E-CASES-ANCHOR-FORMAT", "%s：source_injection_id=%r 不匹配 ^(FIXTURE-)?INJ-\\d{8}-\\d{3}$" % (cid, anchor))
        elif args.allow_synthetic_fixture and not anchor.startswith("FIXTURE-"):
            rep.fail("E-CASES-FIXTURE-ANCHOR", "%s：夹具模式下 source_injection_id 必须以 FIXTURE- 开头，防合成数据冒充真实注入" % cid)
        if args.allow_synthetic_fixture and c.get("synthetic") is not True:
            rep.fail("E-CASES-FIXTURE-FLAG", "%s：meta.synthetic=true 但该条未标 synthetic: true（W18 W-CASE-SYNTHETIC-FLAG-MISSING）" % cid)
        if not args.allow_synthetic_fixture and c.get("synthetic") is True:
            rep.fail("E-CASES-SYNTHETIC-CASE", "%s：合成用例不得进入正式判决集" % cid)

    # 快照唯一性（C1 单一输入）
    if snapshots:
        if len(snapshots) > 1:
            rep.fail("E-CASES-SNAPSHOT-MULTI",
                     "用例集绑定了 %d 个快照 id %s ⇒ 越过 C1 单一输入（docs/00:131）" % (len(snapshots), sorted(snapshots)))
        else:
            rep.ok("E-CASES-SNAPSHOT-SINGLE", "唯一快照 %s" % sorted(snapshots)[0])
    meta_snaps = meta.get("snapshot_ids")
    if isinstance(meta_snaps, list):
        if len(meta_snaps) != 1:
            rep.fail("E-CASES-META-SNAPSHOT-IDS", "meta.snapshot_ids=%r，长度必须恰好为 1（C1 单一输入）" % meta_snaps)
        elif snapshots and set(map(str, meta_snaps)) != snapshots:
            rep.fail("E-CASES-META-SNAPSHOT-MISMATCH",
                     "meta.snapshot_ids=%r 与用例内 snapshot_id 集合 %r 不一致" % (meta_snaps, sorted(snapshots)))
    # ★ W46 缺陷③：用例集侧快照必须唯一 —— 且「用例集 ↔ 对象 id 表」必须同源。
    #   W45 实测：相隔 47 分钟的两次冻结对象总数与 12 类计数逐类相同，但具体 id 有
    #   3 个 Session + 1 个 Table 不同 ⇒ id 集合会随快照漂移，跨快照比对无意义。
    if not isinstance(meta_snaps, list):
        rep.fail("E-CASES-OBJECT-SNAPSHOT",
                 "meta.snapshot_ids=%r 不是列表 ⇒ 无法定位对象 id 表的同源快照（必须恰 1 项）" % (meta_snaps,))
    elif len(meta_snaps) != 1:
        rep.fail("E-CASES-OBJECT-SNAPSHOT",
                 "meta.snapshot_ids 长度=%d（要求恰 1 项）⇒ id 集合随快照漂移、跨快照不可比"
                 "（与 E-CASES-META-SNAPSHOT-IDS 有意重叠，如实披露）" % len(meta_snaps))
    else:
        want_snap = str(meta_snaps[0])
        obj_index = meta.get("object_index")
        if obj_index is None:
            rep.note("meta.object_index 未登记 ⇒ 只断言 meta.snapshot_ids 恰 1 项：%s" % want_snap)
        elif not isinstance(obj_index, dict):
            rep.fail("E-CASES-OBJECT-SNAPSHOT", "meta.object_index=%r 不是映射" % (obj_index,))
        else:
            got_snap = obj_index.get("snapshot_id", obj_index.get("snapshot_ids"))
            got_list = got_snap if isinstance(got_snap, list) else [got_snap]
            if len(got_list) != 1 or str(got_list[0]) != want_snap:
                rep.fail("E-CASES-OBJECT-SNAPSHOT",
                         "meta.object_index 快照 %r 与 meta.snapshot_ids=%r 不一致 ⇒ 对象 id 表与用例集不同源"
                         % (got_snap, meta_snaps))
            else:
                rep.ok("E-CASES-OBJECT-SNAPSHOT-SAME", "用例集与对象 id 表同源快照 %s" % want_snap)

    # ★ W46 缺陷③：旧 `W-CASES-OBJECT-PREFIX` 的 WARN 前提是「无权威前缀表」（契约 OQ-M5-05）；
    #   W45 已在真机证伪该前提（ccf.schema.json 的 x-id-prefix-by-type 12 类逐字一致）
    #   ⇒ 保留 WARN 就是拿旧状态当豁免。该 WARN 删除，改由 E-CASES-OBJECT-UNKNOWN 逐条硬判
    #   （比旧的聚合 WARN 更严：带用例 id 与具体前缀）。这里只披露已核对通过的前缀集合。
    if prefixes:
        rep.ok("E-CASES-OBJECT-PREFIX",
               "expect_objects 前缀全部落在权威前缀表内（%s#%s）：%s"
               % ("/".join(w18.ID_PREFIX_SCHEMA_REL), w18.ID_PREFIX_BY_TYPE_KEY, sorted(prefixes)))
    return path, n


def _print_missing_brief(contract) -> None:
    found, cf = _dig(contract, "case_file.default_path")
    print("         ⇒ 还缺什么（正常状态：docs/06:124-135 规定必须等用户自研故障注入工具就位后录制）：")
    print("           - 50 条用例文件，路径 %s（可用 --cases 覆盖）" % (cf if found else "契约未声明"))
    print("           - 每条用例的 case_schema 既有字段（18 个，逐字见契约 §2）")
    print("           - 每条用例的 source_injection_id（本契约引入的溯源锚点，来自注入作业单）")
    print("           - 顶层 meta：schema / synthetic / snapshot_binding / snapshot_ids / fault_injection_ledger")
    print("           - 作业单交接文件（格式见 tmp/_m5_workorder_draft.md）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def gate(args) -> int:
    rep = Report()
    root = Path(args.root).resolve() if args.root else ROOT
    contract_path = Path(args.contract).resolve() if args.contract else (root / "shared" / "model" / "m5-cases.yaml")
    args._root = root

    print("== m5_cases_gate  M5 判决集契约与用例文件校验 ==")
    _emit("INFO", "root", str(root))
    _emit("INFO", "gate", str(Path(__file__).resolve()))
    _emit("INFO", "contract", str(contract_path))

    yaml, yaml_err = load_yaml()
    w18, w18j, w18_err = load_w18()
    args._w18_err = w18_err
    if yaml is None:
        rep.block("B-PYYAML", "PyYAML 不可用 ⇒ 无法解析契约：%s" % yaml_err)

    contract = None
    text = ""
    sha = ""
    if not contract_path.is_file():
        rep.block("B-CONTRACT-MISSING", "契约文件不存在：%s ⇒ fail-closed，无法判定用例文件是否合规" % contract_path)
    else:
        text = contract_path.read_text(encoding="utf-8")
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _emit("INFO", "contract-facts", "bytes=%d sha256=%s" % (len(text.encode("utf-8")), sha))
        if yaml is not None:
            try:
                contract = yaml.safe_load(text)
                if not isinstance(contract, dict):
                    rep.block("B-CONTRACT-PARSE", "契约顶层不是映射（得到 %s）" % type(contract).__name__)
                    contract = None
            except Exception as exc:
                rep.block("B-CONTRACT-PARSE", "契约 YAML 解析失败：%r" % exc)
        else:
            rep.block("B-CONTRACT-PARSE", "契约不可解析（PyYAML 缺失）")

    if contract is not None:
        missing = [p for p in REQUIRED_PATHS if not _dig(contract, p)[0]]
        if missing:
            rep.block("B-CONTRACT-SECTION", "契约缺必需段：%s ⇒ 契约不完整，不能作为判据依据" % missing)
        else:
            rep.ok("B-CONTRACT-SECTIONS", "契约必需段齐备（%d 项）" % len(REQUIRED_PATHS))
        _guard(rep, "check_contract", check_contract, rep, contract, text, sha, root, args, w18, w18j)

        # 出处引用
        refcheck = RefChecker(root)
        refcheck.collect(contract)
        _guard(rep, "refcheck.verify", refcheck.verify, rep, args.min_refs, args.strict)

        # 用例文件
        cases_name = args.cases
        if cases_name is None:
            found, cf = _dig(contract, "case_file.default_path")
            cases_name = cf if found else None
        fm = _load_fault_modes(root)
        res = _guard(rep, "check_cases", check_cases, rep, contract, contract_path, cases_name, args, w18, w18j, fm)
        case_path, case_n = res if isinstance(res, tuple) else (None, 0)
    else:
        case_path, case_n = None, 0

    # --- 结论 ------------------------------------------------------------
    status = EXIT_PASS
    if rep.fails:
        status = EXIT_FAIL
    if rep.blocks:
        status = EXIT_BLOCKED
    print("-" * 78)
    _emit("SUMMARY", "cases", "%s (n=%d)" % (case_path if case_path else "未录制", case_n))
    print("RESULT: %s  (FAIL=%d BLOCKED=%d WARN=%d PASS=%d min-refs=%d cases=%d)"
          % (EXIT_LABEL[status], len(rep.fails), len(rep.blocks), len(rep.warns), rep.passes,
             args.min_refs, case_n))
    if status == EXIT_BLOCKED:
        print("        BLOCKED ≠ FAIL：没测到，不是测出缺陷。exit=2")
        for code, msg in rep.blocks:
            print("        - %s: %s" % (code, msg))
    elif status == EXIT_FAIL:
        print("        FAIL：契约自相矛盾或用例文件违反契约（真缺陷）。exit=1")
    else:
        print("        PASS：契约自洽；用例文件若已录制则满足契约。exit=0")
    return status


def _load_fault_modes(root: Path):
    yaml, _ = load_yaml()
    if yaml is None:
        return None
    path = root / "shared" / "model" / "fault-modes.yaml"
    if not path.is_file():
        return None
    try:
        obj = yaml.safe_load(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# --selftest：内置夹具（离线，不写真机、不碰 Dify）
# ---------------------------------------------------------------------------
FAMILY_PLAN = [("C1", 14), ("C2", 14), ("C3", 6), ("C4", 6), ("C5", 6), ("C6", 4)]
DIFF_PLAN = ["easy"] * 16 + ["medium"] * 20 + ["hard"] * 14
FIXTURE_SNAPSHOT = "ccf-20261007T000000Z-abc123"
FIXTURE_SNAPSHOT_2 = "ccf-20261007T010101Z-def456"


def _fixture_cases() -> list[dict]:
    out: list[dict] = []
    n = 0
    for family, count in FAMILY_PLAN:
        prefix = PREFIX_BY_FAMILY[family]
        for i in range(1, count + 1):
            n += 1
            is_z = family == "C5"
            out.append({
                "id": "%s%02d" % (prefix, i),
                "cls": prefix,
                "chain": "F2",
                "scope": ["App", "Data"],
                "type": "夹具",
                "query": "夹具提问：请定位根因（合成夹具，非真实判决数据）。",
                "injected": [] if is_z else ["FM-DB-SLOW-QUERY"],
                "expect_root_cause": NONE_SENTINEL if is_z else "FM-DB-SLOW-QUERY",
                "expect_layer": NONE_SENTINEL if is_z else "Data",
                "expect_path": [] if is_z else ["FM-DB-SLOW-QUERY"],
                "expect_objects": ["dbpool:dn_6001-pool"],
                "pos": ["慢查询"],
                "neg": ["宿主机CPU争抢"],
                "nor": ["会话池正常"] if is_z else [],
                "evidence_facts": ["ccf-20261001-000123"],
                "detectors": ["慢\\s*SQL"],
                "difficulty": DIFF_PLAN[n - 1],
                "discriminator": "夹具判别项" if family == "C3" else None,
                "snapshot_id": FIXTURE_SNAPSHOT,
                "source_injection_id": "FIXTURE-INJ-20261007-%03d" % n,
                "synthetic": True,
            })
    return out


def _fixture_doc(cases=None) -> dict:
    cs = _fixture_cases() if cases is None else cases
    return {"meta": {"schema": CASES_SCHEMA,
                     "synthetic": True,
                     "snapshot_binding": "case_field",
                     "snapshot_ids": [FIXTURE_SNAPSHOT],
                     "fault_injection_ledger": "tmp/_m5_w29_selftest/ledger.jsonl"},
            "cases": cs}


def selftest() -> int:
    yaml, yaml_err = load_yaml()
    if yaml is None:
        _emit("BLOCKED", "B-SELFTEST-PYYAML", "PyYAML 不可用 ⇒ 无法构造夹具：%s" % yaml_err)
        return EXIT_BLOCKED
    real_contract_path = ROOT / "shared" / "model" / "m5-cases.yaml"
    if not real_contract_path.is_file():
        _emit("BLOCKED", "B-SELFTEST-CONTRACT", "真契约不存在，无法以它为基线构造夹具：%s" % real_contract_path)
        return EXIT_BLOCKED
    base_contract = yaml.safe_load(real_contract_path.read_text(encoding="utf-8"))

    workdir = ROOT / "tmp" / "_m5_w29_selftest"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    entries: list[tuple[str, int, callable]] = []

    def add(name, expect):
        def deco(fn):
            entries.append((name, expect, fn))
            return fn
        return deco

    @add("contract-missing", EXIT_BLOCKED)
    def _e(d):
        return {"contract": None, "cases": None}

    @add("cases-not-recorded", EXIT_BLOCKED)
    def _e(d):
        return {"contract": copy.deepcopy(base_contract), "cases": None}

    @add("contract-section-deleted", EXIT_BLOCKED)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c.pop("suite", None)
        return {"contract": c, "cases": None}

    @add("contract-schema-wrong", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["schema"] = "m5-cases-contract/2"
        return {"contract": c, "cases": None}

    @add("contract-family-sum-off", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["suite"]["families"][0]["count"] = 13
        return {"contract": c, "cases": None}

    @add("contract-difficulty-drift", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["suite"]["difficulty"]["declared_counts"] = {"easy": 15, "medium": 21, "hard": 14}
        return {"contract": c, "cases": None}

    @add("case-schema-expect-layer-removed", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["case_schema"]["fields"] = [f for f in c["case_schema"]["fields"] if f.get("name") != "expect_layer"]
        return {"contract": c, "cases": None}

    @add("case-schema-field-renamed", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        for f in c["case_schema"]["fields"]:
            if f.get("name") == "id":
                f["name"] = "case_id"
        return {"contract": c, "cases": None}

    @add("case-schema-field-dropped", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["case_schema"]["fields"] = [f for f in c["case_schema"]["fields"] if f.get("name") != "nor"]
        return {"contract": c, "cases": None}

    @add("case-schema-provenance-missing", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["case_schema"]["fields"][2]["introduced_by"] = ""
        return {"contract": c, "cases": None}

    @add("scoring-denominator-drift", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["scoring"]["denominator"]["value"] = 49
        return {"contract": c, "cases": None}

    @add("scoring-min-pp-drift", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["scoring"]["conclusion_discipline"]["min_pp"] = 10
        return {"contract": c, "cases": None}

    @add("scoring-abstain-sensitivity-off", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["scoring"]["abstain"]["sensitivity"]["required"] = False
        return {"contract": c, "cases": None}

    @add("fillplan-role-missing", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["fill_plan"]["roles"].pop("judge", None)
        return {"contract": c, "cases": None}

    @add("fillplan-field-uncovered", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["fill_plan"]["per_field_owner"]["injector"] = [x for x in c["fill_plan"]["per_field_owner"]["injector"] if x != "chain"]
        return {"contract": c, "cases": None}

    @add("ref-line-out-of-range", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["source"][0]["ref"] = "docs/06-评测设计（A-B对照）.md:99999"
        return {"contract": c, "cases": None}

    @add("ref-file-missing", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["source"][0]["ref"] = "docs/99-not-a-real-doc.md:1"
        return {"contract": c, "cases": None}

    @add("refs-below-min", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["source"] = c["source"][:3]
        f = _fixture_doc()
        # 契约正文里仍有零散引用（impl_fields 等），单靠截断 source 段不会跌破下限；
        # 用 --min-refs 抬高下限来断言「反恒真」这条闸门本身有效。
        return {"contract": c, "cases": f, "partial": True, "allow_fixture": True, "min_refs": 500}

    @add("open-questions-empty", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["open_questions"] = []
        return {"contract": c, "cases": None}

    @add("contract-self-sha-pinned", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["meta"]["note_pinned"] = ("本文档滚动 sha256: "
                                    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
        return {"contract": c, "cases": None}

    @add("contract-placeholder", EXIT_FAIL)
    def _e(d):
        c = copy.deepcopy(base_contract)
        c["meta"]["note_todo"] = "TODO: 待补"
        return {"contract": c, "cases": None}

    @add("cases-49", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs = cs[:-1]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-dup-id", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[1]["id"] = cs[0]["id"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-missing-expect-layer", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0].pop("expect_layer", None)
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-empty-expect-layer", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["expect_layer"] = ""
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-layer-alias", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["expect_layer"] = "应用层"
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-layer-none-outside-c5", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["expect_layer"] = NONE_SENTINEL
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-rootcause-unknown", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["expect_root_cause"] = "FM-NOT-A-REAL-MODE"
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-detectors-empty", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["detectors"] = []
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-synthetic-forbidden", EXIT_FAIL)
    def _e(d):
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc()}

    @add("fixture-mode-misuse", EXIT_FAIL)
    def _e(d):
        doc = _fixture_doc()
        doc["meta"]["synthetic"] = False
        for c in doc["cases"]:
            c["synthetic"] = False
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("cases-nonfixture-anchor", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["source_injection_id"] = "INJ-20261007-001"
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-anchor-missing", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0].pop("source_injection_id", None)
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-snapshot-malformed", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["snapshot_id"] = "snap-1"
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-snapshot-multi", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["snapshot_id"] = FIXTURE_SNAPSHOT_2
        doc = _fixture_doc(cs)
        doc["meta"]["snapshot_ids"] = [FIXTURE_SNAPSHOT, FIXTURE_SNAPSHOT_2]
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("cases-meta-missing-binding", EXIT_FAIL)
    def _e(d):
        doc = _fixture_doc()
        doc["meta"].pop("snapshot_binding", None)
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("cases-family-tally-off", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["cls"] = "B"
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-difficulty-tally-off", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        for c in cs:
            if c["difficulty"] == "easy" and c["cls"] != "Z":
                c["difficulty"] = "medium"
                break
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-missing-c4", EXIT_FAIL)
    def _e(d):
        cs = [c for c in _fixture_cases() if c["cls"] != "N"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs),
                "allow_fixture": True, "partial": True}

    @add("cases-missing-c5", EXIT_FAIL)
    def _e(d):
        cs = [c for c in _fixture_cases() if c["cls"] != "Z"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs),
                "allow_fixture": True, "partial": True}

    @add("cases-injected-empty-nonc5", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["injected"] = []
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-object-shape", EXIT_FAIL)
    def _e(d):
        cs = _fixture_cases()
        cs[0]["expect_objects"] = ["dbpool"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    # ---- W46 新增：对象前缀硬判 + 用例集↔id 表同源（负控）----
    @add("cases-object-unknown-prefix", EXIT_FAIL)
    def _e(d):
        # docs/06 §1.2 沿用的旧 `inst:` 前缀（真机不存在）⇒ E-CASES-OBJECT-UNKNOWN
        cs = _fixture_cases()
        cs[0]["expect_objects"] = ["inst:db-cc-01"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-object-evidence-in-objects", EXIT_FAIL)
    def _e(d):
        # 真机 Evidence 形态（obs-NNNNNN，无冒号）混进 expect_objects ⇒ E-CASES-OBJECT-SHAPE
        cs = _fixture_cases()
        cs[0]["expect_objects"] = ["obs-000123"]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs), "allow_fixture": True}

    @add("cases-object-index-snapshot-mismatch", EXIT_FAIL)
    def _e(d):
        # id 表快照 ≠ 用例集快照 ⇒ E-CASES-OBJECT-SNAPSHOT（跨快照的 id 集合不可比）
        doc = _fixture_doc()
        doc["meta"]["object_index"] = {"snapshot_id": "ccf-20260101T000000Z-aaaaaa"}
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("cases-object-snapshot-multi", EXIT_FAIL)
    def _e(d):
        # meta.snapshot_ids 长度 ≠ 1 ⇒ E-CASES-OBJECT-SNAPSHOT（C1 单一输入）
        doc = _fixture_doc()
        doc["meta"]["snapshot_ids"] = [FIXTURE_SNAPSHOT, "ccf-20260101T000000Z-bbbbbb"]
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("positive-object-index-same-snapshot", EXIT_PASS)
    def _e(d):
        # 正控：id 表与用例集同源 ⇒ E-CASES-OBJECT-SNAPSHOT-SAME
        doc = _fixture_doc()
        doc["meta"]["object_index"] = {"snapshot_id": FIXTURE_SNAPSHOT}
        return {"contract": copy.deepcopy(base_contract), "cases": doc, "allow_fixture": True}

    @add("cases-empty-set", EXIT_FAIL)
    def _e(d):
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc([]),
                "allow_fixture": True, "partial": True}

    @add("positive-full-fixture", EXIT_PASS)
    def _e(d):
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(), "allow_fixture": True}

    @add("positive-fragment-partial", EXIT_PASS)
    def _e(d):
        want = {"A01", "A02", "B01", "B02", "D01", "N01", "N02", "Z01", "Z02", "T01"}
        cs = [c for c in _fixture_cases() if c["id"] in want]
        return {"contract": copy.deepcopy(base_contract), "cases": _fixture_doc(cs),
                "allow_fixture": True, "partial": True}

    print("== m5_cases_gate --selftest  (夹具目录 %s) ==" % workdir)
    ok = 0
    bad = 0
    for idx, (name, expect, fn) in enumerate(entries, start=1):
        spec = fn(None)
        cdir = workdir / ("%02d-%s" % (idx, name))
        cdir.mkdir(parents=True, exist_ok=True)
        cpath = cdir / "contract.yaml"
        cases_path = None
        if spec.get("contract") is not None:
            cpath.write_text(yaml.safe_dump(spec["contract"], allow_unicode=True, sort_keys=False, width=10 ** 6),
                             encoding="utf-8")
        cases_spec = spec.get("cases")
        allow_fixture = bool(spec.get("allow_fixture"))
        if cases_spec is None and expect == EXIT_FAIL and spec.get("contract") is not None and not spec.get("no_cases"):
            # 契约自身缺陷的负控：附上完整合法夹具用例，
            # 否则「用例尚未录制 ⇒ BLOCKED」会按优先级把契约缺陷的 FAIL 盖成 BLOCKED。
            cases_spec = _fixture_doc()
            allow_fixture = True
        if cases_spec is not None:
            cases_path = cdir / "cases.yaml"
            cases_path.write_text(yaml.safe_dump(cases_spec, allow_unicode=True, sort_keys=False, width=10 ** 6),
                                  encoding="utf-8")
        ns = argparse.Namespace(
            root=str(ROOT),
            contract=str(cpath),
            cases=(str(cases_path) if cases_path is not None else str(cdir / "cases-absent.yaml")),
            partial=bool(spec.get("partial")),
            allow_synthetic_fixture=allow_fixture,
            strict=False,
            min_refs=int(spec.get("min_refs", 20)),
            min_cases=None,
        )
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                got = gate(ns)
        except Exception as exc:
            got = -1
            buf.write("gate 抛异常：%r\n" % (exc,))
        mark = "OK " if got == expect else "NG "
        if got == expect:
            ok += 1
        else:
            bad += 1
        print("[%s] %2d %-34s expect=%-7s got=%-7s"
              % (mark, idx, name, EXIT_LABEL.get(expect, str(expect)), EXIT_LABEL.get(got, str(got))))
        if got != expect:
            for line in buf.getvalue().splitlines():
                if line.startswith("[FAIL") or line.startswith("[BLOCKED") or "抛异常" in line:
                    print("        | " + line)

    print("-" * 78)
    total = len(entries)
    n_pass = sum(1 for _n, _e, _f in entries if _e == EXIT_PASS)
    print("SELFTEST: %d/%d 通过（含负控 %d 组、正控 %d 组）"
          % (ok, total, total - n_pass, n_pass))
    print("RESULT: %s  (selftest ok=%d ng=%d)" % (EXIT_LABEL[EXIT_PASS if bad == 0 else EXIT_FAIL], ok, bad))
    print("        判定以「期望退出码」为准：期望 BLOCKED 的负控断言的就是 BLOCKED（BLOCKED ≠ FAIL）。")
    return EXIT_PASS if bad == 0 else EXIT_FAIL


def main() -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(
        description="M5 判决集契约与用例文件校验门禁（契约 shared/model/m5-cases.yaml）",
        epilog="退出码 0=PASS 1=FAIL 2=BLOCKED；BLOCKED != FAIL；优先级 BLOCKED > FAIL > PASS。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--contract", default=None, help="契约文件（默认 <root>/shared/model/m5-cases.yaml）")
    ap.add_argument("--cases", default=None, help="用例文件（默认取契约 case_file.default_path）")
    ap.add_argument("--root", default=None, help="工作区根（默认由脚本位置推导）")
    ap.add_argument("--partial", action="store_true", help="允许用例集为子集：不断言 50 条与族/难度分布")
    ap.add_argument("--allow-synthetic-fixture", action="store_true",
                    help="夹具模式：要求 meta.synthetic=true 且每条 source_injection_id 以 FIXTURE- 开头；正式判决集不得使用")
    ap.add_argument("--strict", action="store_true", help="把 WARN 升级为 FAIL")
    ap.add_argument("--min-refs", type=int, default=20, help="契约出处引用条数下限（反恒真，默认 20）")
    ap.add_argument("--min-cases", type=int, default=None, help="用例条数下限（默认 50；--partial 时默认 1）")
    ap.add_argument("--selftest", action="store_true", help="跑内置负控/正控夹具（离线，不碰真机）")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return gate(args)


if __name__ == "__main__":
    sys.exit(main())
