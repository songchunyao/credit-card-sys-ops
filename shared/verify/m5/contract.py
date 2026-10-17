# -*- coding: utf-8 -*-
"""M5 · 输入契约（用例集 + 答案 JSONL）与 fail-closed 校验。

契约唯一来源 = ``docs/06-评测设计（A-B对照）.md``：
  * §1.1 用例族（族C1–族C6；`A-*`14 / `B-*`14 / `D-*`6 / `N-*`6 / `Z-*`6 / `T-*`4 = 50）
  * §1.2 用例结构（18 字段，逐字）
  * §1.3 难度分层（easy 16 / medium 20 / hard 14）
  * §2.1 分母约定（abstain 单列；一题多答只取第一个）
  答案文件契约由队长定义，W19 同契约（字段名不得改动）。

★ fail-closed（任务书逐条）：缺键 / 重复 ``(case_id,group,repeat)`` / 未知 ``difficulty`` /
  缺 ``expect_root_cause`` / ``detectors`` 为空 / ``id`` 重复 / ``cls`` 不在族C1–族C6
  ⇒ **一律 ERROR**（退出码 1），不猜、不补、不放宽。

★ ``cls`` 记法消歧（实测契约内部两种记法并存）：
  docs/06 §1.2 的示例写 ``"cls": "B"``（族前缀字母），而任务书要求「``cls`` 不在族C1–族C6 ⇒ ERROR」。
  两者同时满足的读法 = **接受 ``C1``–``C6`` 与 ``A``/``B``/``D``/``N``/``Z``/``T`` 两种记法，
  并要求 ``id`` 前缀与所属族逐字一致**（这是比任务书更严的一致性检查，不是放宽）。

★ 命名消歧（R-35）：本模块的 ``C1``–``C6`` 是 **用例族**，与 docs/00 §3.1 的项目级
  架构约束 ``C1``–``C6``、M4 期契约门禁 ``M4-C1``…``M4-C9`` 同名不同义；
  对外输出一律写 ``族C1``…``族C6``。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

try:  # PyYAML 在目标环境可用（实测 6.0.3）；缺失时退回纯 JSON
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SCHEMA_CASES = "m5.cases/1"
SCHEMA_ANSWERS = "m5.answers/1"

# ── 用例族（docs/06 §1.1）────────────────────────────────────────────────────
FAMILIES = {"C1": "A", "C2": "B", "C3": "D", "C4": "N", "C5": "Z", "C6": "T"}
PREFIX_TO_FAMILY = {v: k for k, v in FAMILIES.items()}
FAMILY_ORDER = ("C1", "C2", "C3", "C4", "C5", "C6")
# ★ R-35：对外一律用「族Cx」记法，避免与 docs/00 §3.1 的架构约束 C1–C6 混淆
FAMILY_LABELS = {
    "C1": "C1 单层定位", "C2": "C2 跨层定界", "C3": "C3 同症状区分",
    "C4": "C4 否定性证据", "C5": "C5 正常态", "C6": "C6 时间对齐",
}
FAMILY_COUNTS_EXPECTED = {"C1": 14, "C2": 14, "C3": 6, "C4": 6, "C5": 6, "C6": 4}

DIFFICULTY_ORDER = ("easy", "medium", "hard")
DIFFICULTY_COUNTS_EXPECTED = {"easy": 16, "medium": 20, "hard": 14}
FULL_SUITE_TOTAL = 50

# 族C5 正常态：无根因可命中的哨兵值（判据见 judge.py:judge_normal_case）
NONE_SENTINEL = "NONE"

# ── docs/06 §1.2 的 18 字段（键必须存在；空值允许，detectors 除外）────────────
CASE_FIELDS = (
    "id", "cls", "chain", "scope", "type", "query", "injected",
    "expect_root_cause", "expect_layer", "expect_path", "expect_objects",
    "pos", "neg", "nor", "evidence_facts", "detectors", "difficulty", "discriminator",
)
CASE_LIST_FIELDS = (
    "scope", "injected", "expect_path", "expect_objects", "pos", "neg", "nor",
    "evidence_facts", "detectors",
)
CASE_STR_FIELDS = ("id", "cls", "chain", "type", "query", "expect_root_cause", "expect_layer")

# ── 答案文件契约（队长定义；必需键不得改名、不得缺省）─────────────────────────
ANSWER_REQUIRED = ("case_id", "group", "repeat", "question", "answer_text")
ANSWER_OPTIONAL = (
    "abstain", "elapsed_s", "ttft_s", "input_tokens", "output_tokens",
    "tool_events", "snapshot_id", "captured_at_utc", "source",
    # 效率遥测的可选扩展（口径见 docs/06 §2.2 / upstream-eval harness.py:118-132）
    "obs_total_bytes", "tool_calls_n", "iterations_used", "facts_consumed",
    "query_latency_p50", "query_latency_p95", "payloadPolicy",
)
ANSWER_NUMERIC_OPTIONAL = (
    "elapsed_s", "ttft_s", "input_tokens", "output_tokens", "obs_total_bytes",
    "tool_calls_n", "iterations_used", "facts_consumed",
    "query_latency_p50", "query_latency_p95",
)


class Issue:
    """一条校验结论（ERROR / WARN）。``line`` 为答案文件行号，用例集为 None。"""

    __slots__ = ("code", "message", "case_id", "line")

    def __init__(self, code: str, message: str, case_id=None, line=None):
        self.code = code
        self.message = message
        self.case_id = case_id
        self.line = line

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "case_id": self.case_id, "line": self.line}

    def __repr__(self) -> str:  # pragma: no cover
        loc = f" [case={self.case_id} line={self.line}]" if (self.case_id or self.line) else ""
        return f"{self.code}{loc}: {self.message}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _as_list_of_str(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def normalise_cls(raw):
    """接受 ``C2`` / ``B`` / ``b`` 三种记法，返回 (family, prefix) 或 (None, None)。"""
    if not isinstance(raw, str):
        return None, None
    token = raw.strip().upper()
    if token in FAMILIES:
        return token, FAMILIES[token]
    if token in PREFIX_TO_FAMILY:
        return PREFIX_TO_FAMILY[token], token
    return None, None


# ── 用例集 ───────────────────────────────────────────────────────────────────

def load_case_document(path: Path) -> tuple[list, dict, list]:
    """读用例集（.yaml/.yml/.json）。返回 (raw_cases, meta, errors)。"""
    errors: list[Issue] = []
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    doc = None
    if suffix in (".yaml", ".yml"):
        if yaml is None:
            errors.append(Issue("E-CASES-PYYAML-MISSING", "读取 YAML 用例集需要 PyYAML，但当前解释器未安装"))
            return [], {}, errors
        try:
            doc = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001
            errors.append(Issue("E-CASES-YAML-PARSE",
                                f"YAML 解析失败：{type(exc).__name__}: {exc}"))
            return [], {}, errors
    else:
        try:
            doc = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append(Issue("E-CASES-JSON-PARSE",
                                f"JSON 解析失败：{type(exc).__name__}: {exc}"))
            return [], {}, errors

    if isinstance(doc, list):
        return doc, {}, errors
    if isinstance(doc, dict):
        if "cases" in doc:
            meta = doc.get("meta") or {}
            if not isinstance(meta, dict):
                errors.append(Issue("E-CASES-META-TYPE", "顶层 meta 必须是映射"))
                meta = {}
            cases = doc.get("cases")
            if not isinstance(cases, list):
                errors.append(Issue("E-CASES-CONTAINER", "顶层 cases 必须是列表"))
                return [], meta, errors
            return cases, meta, errors
        if "id" in doc:  # 单条用例
            return [doc], {}, errors
    errors.append(Issue("E-CASES-SHAPE",
                        "用例集必须是列表、或含 cases 列表的映射（可带 meta）"))
    return [], {}, errors


def load_document(path: Path) -> tuple[object, list]:
    """读**整个**用例文档（.yaml/.yml/.json）。返回 ``(doc, errors)``。

    ★ W30 新增：:func:`load_case_document` 只把 ``doc["meta"]`` 交给调用方，顶层
    ``suite`` / ``scoring``（分母与 abstain 口径声明所在）会被丢掉，因此判据器无法
    对契约声明做交叉一致性断言。本函数原样返回整个文档（不做形状校验），
    由调用方按需取键；解析失败返回 ``(None, errors)``。
    """
    errors: list[Issue] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(Issue("E-DOC-READ", f"读取失败：{type(exc).__name__}: {exc}"))
        return None, errors
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        if yaml is None:
            errors.append(Issue("E-CASES-PYYAML-MISSING",
                                "读取 YAML 用例集需要 PyYAML，但当前解释器未安装"))
            return None, errors
        try:
            return yaml.safe_load(text), errors
        except Exception as exc:  # noqa: BLE001
            errors.append(Issue("E-CASES-YAML-PARSE",
                                f"YAML 解析失败：{type(exc).__name__}: {exc}"))
            return None, errors
    try:
        return json.loads(text), errors
    except Exception as exc:  # noqa: BLE001
        errors.append(Issue("E-CASES-JSON-PARSE",
                            f"JSON 解析失败：{type(exc).__name__}: {exc}"))
        return None, errors


def validate_cases(raw_cases: list, meta: dict, *, expect_full_suite: bool,
                   strict: bool) -> tuple[list, list, list]:
    """fail-closed 校验。返回 (cases, errors, warnings)。"""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    cases: list[dict] = []
    seen_ids: dict = {}
    synthetic_meta = bool(meta.get("synthetic")) if isinstance(meta, dict) else False

    for idx, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            errors.append(Issue("E-CASE-NOT-MAP", f"第 {idx} 条用例不是映射", line=idx))
            continue
        cid = raw.get("id") if isinstance(raw.get("id"), str) else None
        tag = cid or f"<#{idx}>"

        # 1) 18 字段键必须齐全（docs/06 §1.2 逐字）
        missing = [f for f in CASE_FIELDS if f not in raw]
        if missing:
            errors.append(Issue("E-CASE-MISSING-FIELD",
                                f"缺字段 {missing}（docs/06 §1.2 的 18 字段）", case_id=tag))
            continue

        # 2) id / 重复 id
        if not isinstance(raw["id"], str) or not raw["id"].strip():
            errors.append(Issue("E-CASE-ID-EMPTY", "id 必须是非空字符串", case_id=tag))
            continue
        if raw["id"] in seen_ids:
            errors.append(Issue("E-CASE-ID-DUPLICATE",
                                f"id 重复：{raw['id']}（首次见第 {seen_ids[raw['id']]} 条）", case_id=tag))
            continue
        seen_ids[raw["id"]] = idx

        # 3) 标量字段类型
        bad_scalar = [f for f in CASE_STR_FIELDS if not isinstance(raw[f], str) or not raw[f].strip()]
        if bad_scalar:
            errors.append(Issue("E-CASE-SCALAR-TYPE",
                                f"字段必须是非空字符串：{bad_scalar}", case_id=tag))
            continue

        # 4) 列表字段类型
        bad_list = [f for f in CASE_LIST_FIELDS if not _as_list_of_str(raw[f])]
        if bad_list:
            errors.append(Issue("E-CASE-LIST-TYPE",
                                f"字段必须是字符串列表：{bad_list}", case_id=tag))
            continue

        # 5) cls 必须在族C1–族C6（并校验 id 前缀一致）
        family, prefix = normalise_cls(raw["cls"])
        if family is None:
            errors.append(Issue("E-CASE-CLS-UNKNOWN",
                                f"cls={raw['cls']!r} 不在族C1–族C6（允许 C1–C6 或 A/B/D/N/Z/T）",
                                case_id=tag))
            continue
        if not re.match(rf"^{re.escape(prefix)}[-_]?\d+$", raw["id"]):
            errors.append(Issue("E-CASE-ID-FAMILY-MISMATCH",
                                f"id={raw['id']!r} 与 cls={raw['cls']!r}（族 {family}，前缀 {prefix}）不一致",
                                case_id=tag))
            continue

        # 6) difficulty 必须在 easy/medium/hard
        if raw["difficulty"] not in DIFFICULTY_COUNTS_EXPECTED:
            errors.append(Issue("E-CASE-DIFFICULTY-UNKNOWN",
                                f"未知 difficulty={raw['difficulty']!r}（只允许 {list(DIFFICULTY_ORDER)}）",
                                case_id=tag))
            continue

        # 7) expect_root_cause 必须非空（键已在步骤 1 保证存在）
        if not raw["expect_root_cause"].strip():
            errors.append(Issue("E-CASE-ROOTCAUSE-EMPTY", "expect_root_cause 为空", case_id=tag))
            continue

        # 8) detectors 必须非空且可编译
        if not raw["detectors"]:
            errors.append(Issue("E-CASE-DETECTORS-EMPTY",
                                "detectors 为空（docs/06 §1.2 要求正则辅助通道）", case_id=tag))
            continue
        bad_re = []
        for rx in raw["detectors"]:
            try:
                re.compile(rx)
            except re.error as exc:
                bad_re.append(f"{rx!r} -> {exc}")
        for field_name in ("pos", "neg", "nor"):
            for rx in raw[field_name]:
                try:
                    re.compile(rx)
                except re.error as exc:
                    bad_re.append(f"{field_name}:{rx!r} -> {exc}")
        if bad_re:
            errors.append(Issue("E-CASE-REGEX-INVALID",
                                f"正则不可编译（fail-closed，不猜）：{bad_re}", case_id=tag))
            continue

        # 9) 语义 WARN（strict 升级为 ERROR）
        if raw["expect_root_cause"] == NONE_SENTINEL and family != "C5":
            warnings.append(Issue("W-CASE-NONE-OUTSIDE-C5",
                                  f"族{family} 使用了 NONE 哨兵；docs/06 §1.1 只在族C5 正常态成立",
                                  case_id=tag))
        if family == "C5" and raw["expect_root_cause"] != NONE_SENTINEL:
            warnings.append(Issue("W-CASE-C5-REAL-ROOTCAUSE",
                                  f"族C5 声明了真实根因 {raw['expect_root_cause']!r}；"
                                  "§1.1 规定族C5 测「不得虚构」", case_id=tag))
        if raw["expect_layer"] == NONE_SENTINEL and family != "C5":
            warnings.append(Issue("W-CASE-LAYER-NONE-OUTSIDE-C5",
                                  f"族{family} 的 expect_layer 使用了 NONE 哨兵", case_id=tag))
        if family == "C5" and not raw["nor"]:
            warnings.append(Issue("W-CASE-C5-NO-NOR",
                                  "族C5 无 nor 词表 ⇒ 正常态判据易退化为不可判", case_id=tag))
        if not raw["evidence_facts"]:
            warnings.append(Issue("W-CASE-NO-EVIDENCE-FACTS",
                                  "evidence_facts 为空 ⇒ 该用例不产出证据命中判据", case_id=tag))
        if synthetic_meta and not raw.get("synthetic"):
            warnings.append(Issue("W-CASE-SYNTHETIC-FLAG-MISSING",
                                  "meta.synthetic=true，但该用例未显式标注 synthetic: true", case_id=tag))

        item = dict(raw)
        item["_family"] = family
        item["_prefix"] = prefix
        cases.append(item)

    # 10) 构成校验
    fam_counts = {f: 0 for f in FAMILY_ORDER}
    dif_counts = {d: 0 for d in DIFFICULTY_ORDER}
    for c in cases:
        fam_counts[c["_family"]] += 1
        dif_counts[c["difficulty"]] += 1

    if expect_full_suite:
        if len(cases) != FULL_SUITE_TOTAL:
            errors.append(Issue("E-SUITE-TOTAL",
                                f"--expect-full-suite：用例数 {len(cases)} != {FULL_SUITE_TOTAL}"))
        bad_fam = {f: (fam_counts[f], FAMILY_COUNTS_EXPECTED[f])
                   for f in FAMILY_ORDER if fam_counts[f] != FAMILY_COUNTS_EXPECTED[f]}
        if bad_fam:
            errors.append(Issue("E-SUITE-FAMILY-COUNTS",
                                f"--expect-full-suite：族条数不符 (实得,期望)={bad_fam}；"
                                "按 docs/06 §1.1 不得自动补用例或放宽"))
        bad_dif = {d: (dif_counts[d], DIFFICULTY_COUNTS_EXPECTED[d])
                   for d in DIFFICULTY_ORDER if dif_counts[d] != DIFFICULTY_COUNTS_EXPECTED[d]}
        if bad_dif:
            errors.append(Issue("E-SUITE-DIFFICULTY-COUNTS",
                                f"--expect-full-suite：难度分层不符 (实得,期望)={bad_dif}"))
    elif len(cases) != FULL_SUITE_TOTAL:
        warnings.append(Issue("W-SUITE-PARTIAL",
                              f"用例数 {len(cases)} != {FULL_SUITE_TOTAL}"
                              "（未加 --expect-full-suite，按部分集运行；分母随之为该子集）"))

    if strict:
        errors.extend(warnings)
        warnings = []
    return cases, errors, warnings


# ── 答案文件 ─────────────────────────────────────────────────────────────────

def load_answers(path: Path) -> tuple[list, list, list]:
    """读答案 JSONL。返回 (records, errors, warnings)（WARN 在 validate 阶段产生）。"""
    records: list[dict] = []
    errors: list[Issue] = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                errors.append(Issue("E-ANS-JSON-PARSE",
                                    f"JSON 解析失败：{type(exc).__name__}: {exc}", line=lineno))
                continue
            if not isinstance(obj, dict):
                errors.append(Issue("E-ANS-NOT-MAP", "每行必须是 JSON 对象", line=lineno))
                continue
            obj["_line"] = lineno
            records.append(obj)
    return records, errors, []


def validate_answers(records: list, cases: list, *, strict: bool,
                     groups_filter=None, expect_groups=None) -> tuple[list, list, list, dict]:
    """fail-closed 校验答案记录。返回 (kept, errors, warnings, info)。"""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    case_index = {c["id"]: c for c in cases}
    seen: dict = {}
    kept: list[dict] = []
    extra_keys: dict = {}

    for rec in records:
        lineno = rec.get("_line")
        tag = rec.get("case_id") if isinstance(rec.get("case_id"), str) else None

        # 1) 必需键齐全（缺键 ⇒ fail-closed ERROR）
        missing = [k for k in ANSWER_REQUIRED if k not in rec]
        if missing:
            errors.append(Issue("E-ANS-MISSING-KEY",
                                f"缺必需键 {missing}（契约：{list(ANSWER_REQUIRED)}）",
                                case_id=tag, line=lineno))
            continue

        # 2) 类型
        bad = []
        for k in ("case_id", "group", "question", "answer_text"):
            if not isinstance(rec[k], str):
                bad.append(f"{k} 必须是字符串（实为 {type(rec[k]).__name__}）")
        if isinstance(rec["repeat"], bool) or not isinstance(rec["repeat"], int) or rec["repeat"] < 1:
            bad.append(f"repeat 必须是 >=1 的整数（实为 {rec['repeat']!r}）")
        if not str(rec["case_id"]).strip():
            bad.append("case_id 为空")
        if not str(rec["group"]).strip():
            bad.append("group 为空")
        if not str(rec["question"]).strip():
            bad.append("question 为空（契约要求逐字题面）")
        if "abstain" in rec and not isinstance(rec["abstain"], bool):
            bad.append(f"abstain 必须是布尔（实为 {type(rec['abstain']).__name__}）")
        for k in ANSWER_NUMERIC_OPTIONAL:
            if k in rec and rec[k] is not None:
                if isinstance(rec[k], bool) or not isinstance(rec[k], (int, float)):
                    bad.append(f"{k} 必须是数值或 null（实为 {type(rec[k]).__name__}）")
        if "tool_events" in rec and rec["tool_events"] is not None:
            te = rec["tool_events"]
            if not isinstance(te, list) or not all(
                    isinstance(e, dict) and isinstance(e.get("name"), str) for e in te):
                bad.append("tool_events 必须是 [{\"name\": str, ...}] 列表")
        if bad:
            errors.append(Issue("E-ANS-TYPE", "；".join(bad), case_id=tag, line=lineno))
            continue

        # 3) 重复 (case_id, group, repeat) ⇒ fail-closed ERROR
        key = (rec["case_id"], rec["group"], rec["repeat"])
        if key in seen:
            errors.append(Issue("E-ANS-DUPLICATE",
                                f"重复 (case_id,group,repeat)={key}，首次见第 {seen[key]} 行",
                                case_id=tag, line=lineno))
            continue
        seen[key] = lineno

        # 4) case_id 必须存在于用例集（孤儿答案 ⇒ fail-closed ERROR）
        case = case_index.get(rec["case_id"])
        if case is None:
            errors.append(Issue("E-ANS-UNKNOWN-CASE",
                                f"case_id={rec['case_id']!r} 不在用例集中", case_id=tag, line=lineno))
            continue

        # 5) WARN：逐字题面 / 空答案
        if rec["question"].strip() != case["query"].strip():
            warnings.append(Issue("W-ANS-QUESTION-MISMATCH",
                                  "question 与用例 query 不是逐字相同（docs/06 §4 P6 要求逐字相同）",
                                  case_id=tag, line=lineno))
        if not rec["answer_text"].strip() and not rec.get("abstain"):
            warnings.append(Issue("W-ANS-EMPTY-TEXT",
                                  "answer_text 为空但 abstain 非 true ⇒ 该用例按未命中计",
                                  case_id=tag, line=lineno))

        for k in rec:
            if k not in ANSWER_REQUIRED and k not in ANSWER_OPTIONAL and k != "_line":
                extra_keys[k] = extra_keys.get(k, 0) + 1

        rec["_case"] = case
        kept.append(rec)

    # 6) 分组过滤 / 组数断言
    all_groups: list[str] = []
    for rec in kept:
        if rec["group"] not in all_groups:
            all_groups.append(rec["group"])

    if groups_filter:
        want = [g for g in groups_filter if g]
        missing_groups = [g for g in want if g not in all_groups]
        if missing_groups:
            errors.append(Issue("E-ANS-GROUP-ABSENT",
                                f"--groups 指定的组在答案文件中不存在：{missing_groups}"
                                f"（文件中有 {all_groups}）"))
        selected = [g for g in want if g in all_groups]
        kept = [r for r in kept if r["group"] in selected]
        all_groups = selected

    if expect_groups is not None and len(all_groups) != expect_groups:
        errors.append(Issue("E-ANS-GROUP-COUNT",
                            f"--expect-groups {expect_groups}：实际组数 {len(all_groups)}（{all_groups}）"))

    if len(all_groups) < 2 and not errors:
        warnings.append(Issue("W-ANS-SINGLE-GROUP",
                              f"只有 {len(all_groups)} 个组 ⇒ 对比统计进入 BLOCKED（不是 FAIL）"))

    # 7) 各组的 repeat 集合（配对完整性）
    repeats_by_group: dict = {}
    cases_by_group: dict = {}
    for rec in kept:
        repeats_by_group.setdefault(rec["group"], set()).add(rec["repeat"])
        cases_by_group.setdefault(rec["group"], set()).add(rec["case_id"])
    spread = {g: sorted(v) for g, v in repeats_by_group.items()}
    distinct = {tuple(v) for v in spread.values()}
    if len(distinct) > 1:
        warnings.append(Issue("W-ANS-REPEAT-SPREAD",
                              f"各组 repeat 集合不一致 {spread} ⇒ (case,repeat) 配对子集将退化"))

    if strict:
        errors.extend(warnings)
        warnings = []

    info = {
        "groups": all_groups,
        "groups_n": len(all_groups),
        "repeats_by_group": spread,
        "cases_by_group": {g: len(v) for g, v in cases_by_group.items()},
        "extra_keys": extra_keys,
        "records_total": len(kept),
    }
    return kept, errors, warnings, info


# ── 对象 id 口径（W46）───────────────────────────────────────────────────────
# 权威前缀表 = ``shared/model/ccf.schema.json`` 的 ``x-id-prefix-by-type``（12 类，
# W45 真机逐字核对一致）。**唯一真相源在 schema，不在本模块**：这里只放期望值用于
# 「表漂移」提示，判定一律读 schema。
#
# ★ W46 缺陷①：``m5/judge.py`` 曾用「归一化后子串命中」判 ``expect_objects``
#   （逐字 ``norm = lambda s: s.strip().lower().removeprefix("inst:")`` 后
#   ``norm(o) in low_ans``）⇒ ``session:sess-0`` 会假命中 ``session:sess-01``；
#   宽松匹配的抬高幅度取决于同组答案把 id 写得多啰嗦 ⇒ **直接威胁 A/B 公平性**。
#   W46 起改为「按 id 记号抽取 + 集合成员判定」：expect id 必须作为**独立 id 记号**
#   出现在答案里（见 :func:`extract_id_tokens`、:data:`OBJECT_HIT_RULE_VERSION`）。
ID_PREFIX_BY_TYPE_KEY = "x-id-prefix-by-type"
ID_PREFIX_SCHEMA_REL = ("shared", "model", "ccf.schema.json")
ID_PREFIXES_EXPECTED = (
    "app", "appinst", "endpoint", "jdbsrc", "ctr", "host",
    "dbcluster", "dbinst", "dbpool", "session", "query", "table",
)
# 旧前缀容忍：docs/06 §1.2 的样例沿用真机不存在的 ``inst:``（W45 已证伪），合成夹具
# （``m5/selftest.py`` 的 MODES）也沿用它。这里**只**把它当归一化别名：
# 仅当 expect 形如 ``<legacy>:<local>`` 且答案里出现 ``<local>`` 这个 id 记号时才算命中，
# 并在 verdict 的 ``objects.matched_via`` 里以 ``inst-alias`` 逐条披露；
# 它不得掩盖失配，也不改变分母与阈值。
LEGACY_ID_PREFIX_ALIASES = ("inst",)
OBJECT_HIT_RULE_VERSION = "M-ID-SET v1"

# 对象 id 形态：``<prefix>:<local>``（前缀须在权威表内，由门禁判）；
# Evidence id 形态：无冒号的单记号（真机 ``obs-NNNNNN`` / 冻结 ``ccf-<stamp>``）。
OBJECT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]*:.+$")
EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
# id 记号：冒号两侧各允许 ``.``/``-``/``_`` 连接（真机 id 形如
# ``ccf-20260929T113250Z-e53d79``、``dn_6001-pool``）；冒号可选（Evidence 无冒号）。
ID_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*(?::[A-Za-z0-9][A-Za-z0-9_.\-]*)?")


# ── 对象 id 判定口径（W46）───────────────────────────────────────────────────

def repo_root() -> Path:
    """本文件位于 ``<root>/shared/verify/m5/contract.py`` ⇒ parents[3] = 仓库根。"""
    return Path(__file__).resolve().parents[3]


def load_id_prefixes(root=None) -> tuple:
    """读权威前缀表 ``x-id-prefix-by-type``。返回 ``({类型: 前缀}, None)`` 或
    ``(None, 原因)``。

    ★ fail-closed：文件缺失 / 解析失败 / 键缺失或为空 / 项非字符串 ⇒ 返回原因，
    **不内置退路表**（内置退路会让「权威表被删」变成不可见 —— W45 已证伪旧理由
    「无权威前缀表」，见 m5-cases.yaml 的 OQ-M5-05）。
    """
    base = Path(root) if root else repo_root()
    path = base.joinpath(*ID_PREFIX_SCHEMA_REL)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "权威前缀表文件不存在：%s" % path
    except (OSError, ValueError) as exc:
        return None, "权威前缀表读取失败：%s: %s" % (type(exc).__name__, exc)
    table = doc.get(ID_PREFIX_BY_TYPE_KEY) if isinstance(doc, dict) else None
    if not isinstance(table, dict) or not table:
        return None, "%s 里没有非空的 %s" % (path, ID_PREFIX_BY_TYPE_KEY)
    out = {}
    for key, value in table.items():
        if not isinstance(value, str) or not value.strip():
            return None, "%s 的 %s 项 %r 不是非空字符串" % (path, ID_PREFIX_BY_TYPE_KEY, key)
        out[str(key)] = value.strip()
    return out, None


def id_prefix_set(root=None) -> tuple:
    """权威前缀集合（小写）。返回 ``(set, None)`` 或 ``(None, 原因)``。"""
    table, err = load_id_prefixes(root)
    if table is None:
        return None, err
    return {v.lower() for v in table.values()}, None


def canonical_id(value) -> str:
    """id 归一化：去空白 + 小写。**不削减前缀**（削减只发生在别名通道，且被披露）。"""
    return str(value).strip().lower()


def extract_id_tokens(text) -> set:
    """从答案文本抽出 id 记号集合（canonical 小写）。

    「命中」的口径 = expect id 作为**独立记号**出现，因此 ``session:sess-0``
    不再命中 ``session:sess-01``（旧子串口径的假命中形状）。
    """
    return {canonical_id(m) for m in ID_TOKEN_RE.findall(text or "")}


def id_hits(expect_ids, tokens) -> tuple:
    """集合成员判定。返回 ``(命中的原始 expect 项列表, {原始项: via})``。

    ``via`` ∈ ``{"literal", "inst-alias"}``：
      * ``literal``    —— canonical(expect) 本身是答案里的一个 id 记号（主通道）；
      * ``inst-alias`` —— expect 带旧前缀 ``inst:``（真机无此前缀）时，只要求
        其**本地名**作为 id 记号出现；这是**披露式容忍**，不是放宽命中语义。
    """
    hits, via = [], {}
    locals_ = set()
    for tok in tokens:
        locals_.add(tok.split(":", 1)[1] if ":" in tok else tok)
    for raw in expect_ids:
        eid = canonical_id(raw)
        if eid in tokens:
            hits.append(raw)
            via[raw] = "literal"
            continue
        head, sep, local = eid.partition(":")
        if sep and head in LEGACY_ID_PREFIX_ALIASES and local and local in locals_:
            hits.append(raw)
            via[raw] = "inst-alias"
    return hits, via
