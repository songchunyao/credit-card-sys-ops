#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G21 / X20 · 共享 ABox 实例级契约门禁（ccf_instance_gate）

================================================================================
为什么需要这道门禁（动机 · M2.14）
================================================================================
真机取证暴露了一个**不对称缺陷**：

  · `shared/model/topology.yaml:189` 声明 `Table.key_attrs: [schemaName, tableName]`，
    `shared/model/topology.yaml:191` 声明 `schemaName: {type: string, unit: null}`
    —— 契约要求**每个 Table 实例**都必须能给出 schemaName；
  · 但 `tmp/ccf_live4.json` 里 **30 个 Table 对象的 attrs 全为 `{}`**：`schemaName`
    在 `attrs` 与 `key` 中零命中（仅 `key.tableName` 有值）。

这个缺陷**只被 GraphDB 侧的 SHACL 抓到**：方案2 `tests/test_integrity.py` T11b 在真机
ABox 上报告违例 `topology#schemaName × 30`。而**共享 ABox 这一层** —— KG 侧与 ONTO 侧
共同消费的那份事实（CCF）—— **没有任何实例级校验**。于是 KG 侧会对着一份自己都不知道
不完整的 ABox 做推理：它消费了 30 个缺 `schemaName` 的 Table 却毫不知情，而 ONTO 侧
因为"恰好"启用了 SHACL 就报了出来。

这正是风险 R4「Cypher vs SPARQL 不对称」的实例化：**同一份事实，一侧有校验、另一侧
没有**，缺陷能否被发现取决于用的是哪套工具。本门禁在**中性事实层（CCF）**补上这道
对称校验，使两侧都能自己发现自己消费的 ABox 不完整。

================================================================================
检查项
================================================================================
A  **对象 key_attrs 完整性**：`--model-dir/topology.yaml` 的 `types`（list of dict，
   条目键 `code`/`layer`/`abstract`/`annotation`/`attrs`/`key_attrs`）是权威来源。
   只检查**非抽象、非标注、且声明了 key_attrs** 的类型；对其每个 CCF 实例对象，
   断言每个 key_attr 都存在于该对象的 `attrs`（或 `key`）且非空。
   缺失 ⇒ ERROR；同一 (类型, key_attr) 的多个对象**合并为一条**并报「共 N 个对象」。
B  **观测 delta 契约（X-7 裁定）同进同出 + 值域**：契约 `$defs.observation.allOf[1]`
   （`if required delta then required deltaWindowS`）只锁同现，本门禁在**实例层**再锁值域：
   `delta` 必须是**有限数值**（int / float 皆可；bool 不算数值；NaN·Inf 不算有限），
   `deltaWindowS` 必须是**正整数**（≥ 1 秒；bool 不算 int）。
   为什么 delta 允许小数：采集侧 `service/probes/app_probe.py` 的 `_counter_delta()`
   返回 `after - before` 原样差值、**不取整**（只挡 bool / 非数 / 负值），其累计量含
   `app.uptime_s`（秒），上游以浮点秒返回时 delta 就是小数；只有
   `container_probe.py` 的整数事件计数器才走 `int(round(d))`。故小数 delta 是契约合法值，
   把它判成 ERROR 会在合法 CCF 上误报。
   行号引用**不写死**：`schema_anchor()` 在运行时定位 `ccf.schema.json` 的锚点行，
   契约增删行后引用自动跟随（曾因 schema 收紧 deltaWindowS 值域而整体漂移）。
   空过必须显式化：无 delta 观测时打 WARN，不得静默算作通过。
C  **反恒真下限（★ 本门禁的关键设计）**：实际核对的 (对象, key_attr) 配对数、
   扫描的 observations 条数必须达到下限，否则判 ERROR「检查规模不足 ⇒ 疑似解析
   口径失配」。教训来自 C18 恒真门禁：**解析口径失配后门禁会静默通过**。真机上
   CCF 的对象类型字段名是 `type`（不是 `typeCode`），一旦按 `typeCode` 解析，
   检查 A 会匹配到 0 条而给出假绿 —— 这是最坏的失败模式。本门禁宁可报错，
   也绝不在规模塌缩时给 PASS。
D  **CCF 自洽最小集**：snapshotId 形态、对象/观测 id 非空且各自唯一、观测 objectId
   必须能解析到本快照对象（孤儿观测）。**不重算 canonicalSha256** —— 那是
   `shared/verify/equivalence.py` R1 的职责，不在此重复。

fail-closed：任何内部异常 ⇒ 打印 `RESULT: FAIL` 并 exit 1，绝不静默通过。

用法：
    python shared/verify/ccf_instance_gate.py --ccf tmp/ccf_live4.json
    python shared/verify/ccf_instance_gate.py --ccf tmp/ccf_live4.json --json
    python shared/verify/ccf_instance_gate.py --ccf X.json --min-keypairs 40 --min-observations 100
退出码：0 = PASS（ERROR=0）；1 = FAIL（含内部异常、用法错误）。
纯标准库 + PyYAML；只读，零网络，零 Neo4j / GraphDB 依赖。
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
import traceback
from pathlib import Path

# ★ Windows 控制台默认 GBK：本门禁的输出全是中文，不切 UTF-8 会抛 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

GATE_ID = "G21"
GATE_ALIAS = "X20"
GATE_TITLE = "共享 ABox 实例级契约门禁"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[1] / "model"
SNAPSHOT_ID_RE = re.compile(r"^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")
RULE = "-" * 78
BAR = "=" * 78

# Evidence 特例（派生标注类型）的契约依据 —— 输出里逐条引用，不凭猜。
# ccf.schema.json 的行号由 anchor("x_id_prefix") 现场定位（该文件会被别的施工者改动）。
def evidence_basis() -> str:
    return (
        "shared/model/topology.yaml:201-205（annotation: true / layer: null / key_attrs: [recordId]）、"
        "shared/model/topology.yaml:207（契约原文即写明 recordId = CCF observation.id（obs-000001））、"
        "shared/model/ccf.schema.json%s（x-id-prefix-by-type 刻意不含 Evidence）、"
        "shared/project/ccf_plan.py:441-448（_add_evidence 的 rec_id = obs.get(spec[\"id_field\"])，"
        "故 Evidence 的 recordId 只可能由观测 id 提供）"
        % (anchor("x_id_prefix") or "（x-id-prefix-by-type，未定位行号）")
    )

SKIP_FLAGS = ("abstract", "annotation", "derived")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        raise ValueError("%s 带 UTF-8 BOM（契约要求无 BOM）" % path)
    return raw.decode("utf-8")


def load_json_file(path: Path):
    return json.loads(read_text(path))


def nonempty(value) -> bool:
    """非空判定：None / 空白串 / 空容器 ⇒ 空；数字 0 与 False ⇒ 非空。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def is_int(value) -> bool:
    """严格整数判定：bool 不算 int（Python 里 True 是 int 的子类）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value) -> bool:
    """有限数值判定（X-7 的 delta 用）：int 或 float 皆可 —— **小数是合法值**。

    采集侧 `service/probes/app_probe.py` 的 `_counter_delta()` 返回 `after - before`
    原样差值、不取整（只挡 bool / 非数 / 负值），其累计量含 `app.uptime_s`（秒），
    上游以浮点秒返回时 delta 就是小数；只有 `container_probe.py` 的整数事件计数器
    才走 `int(round(d))`。故「小数 delta」不是缺陷，判成 ERROR 会在合法 CCF 上误报。
    bool 不算数值（True 是 int 子类）；NaN / ±Inf 不是有限值。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# --- ccf.schema.json 锚点行号：运行时定位，不写死 ---------------------------------
# 契约文件会被别的施工者修改（例如把 deltaWindowS 收紧为 integer/minimum 1），
# 一次增删就会让写死的 "ccf.schema.json:437-440" 指向错误的行。这里改为每次运行
# 现场定位锚点行；定位失败时引用退化为字段路径（宁可不给行号，也不给错行号）。
SCHEMA_ANCHOR_PATTERNS = {
    "delta": re.compile(r'^\s*"delta"\s*:\s*\{'),
    "deltaWindowS": re.compile(r'^\s*"deltaWindowS"\s*:\s*\{'),
    "delta_cooccur": re.compile(
        r'^\s*\{\s*"if"\s*:\s*\{\s*"required"\s*:\s*\[\s*"delta"\s*\]'
    ),
    "x_id_prefix": re.compile(r'^\s*"x-id-prefix-by-type"\s*:\s*\{'),
}
_SCHEMA_ANCHORS: dict[str, int] = {}
_SCHEMA_FACTS: dict[str, str] = {}


def _declared_domain(lines: list, start: int, span: int = 6) -> str:
    """从锚点行往后取一小段，摘出契约**实际**声明的 type / minimum。

    这是「引用契约」而非「复述契约」：schema 被别的施工者改了值域，
    这里摘到的就是改后的值，不需要谁记得去同步文案。
    """
    block = "\n".join(lines[start - 1:start - 1 + span])
    m_type = re.search(r'"type"\s*:\s*(\[[^\]]*\]|"[^"]*")', block)
    m_min = re.search(r'"minimum"\s*:\s*(-?\d+)', block)
    if not m_type:
        return ""
    out = "type=%s" % m_type.group(1).replace(" ", "")
    if m_min:
        out += "，minimum=%s" % m_min.group(1)
    return out


def load_schema_anchors(model_dir) -> dict:
    """读取 <model-dir>/ccf.schema.json，记录 delta 契约锚点的**当前**行号与值域。

    只做定位与摘录、不解释 schema（本机无 jsonschema，且结构校验不是本门禁的职责）。
    读不到文件时返回空表 —— 引用降级为字段路径，不抛异常、不影响判定。
    """
    _SCHEMA_ANCHORS.clear()
    _SCHEMA_FACTS.clear()
    try:
        text = read_text(Path(model_dir) / "ccf.schema.json")
    except Exception:
        return {}
    lines = text.splitlines()
    for key, pat in SCHEMA_ANCHOR_PATTERNS.items():
        for i, line in enumerate(lines, 1):
            if pat.match(line):
                _SCHEMA_ANCHORS[key] = i
                break
    for key in ("delta", "deltaWindowS"):
        n = _SCHEMA_ANCHORS.get(key)
        if n:
            _SCHEMA_FACTS[key] = _declared_domain(lines, n)
    return dict(_SCHEMA_ANCHORS)


def anchor(key: str) -> str:
    """`:NNN`（当前 schema 版本的实际行号）；定位失败返回空串。"""
    n = _SCHEMA_ANCHORS.get(key)
    return ":%d" % n if n else ""


def schema_facts() -> dict:
    """现场摘录到的契约声明值域（delta / deltaWindowS），供人读报告与 JSON 出口引用。"""
    return dict(_SCHEMA_FACTS)


def cite(field: str, key: str) -> str:
    """稳定的契约引用：JSON 字段路径（漂移不坏）+ 现场定位到的行号 + 现行声明值域。"""
    n = _SCHEMA_ANCHORS.get(key)
    out = "契约锚点 ccf.schema.json $defs.observation.%s%s" % (field, ":%d" % n if n else "")
    fact = _SCHEMA_FACTS.get(key)
    if fact:
        out += "（现行声明 %s）" % fact
    return out


def type_name_of(spec: dict):
    return spec.get("code") or spec.get("type")


def is_skipped(spec: dict) -> bool:
    return any(spec.get(flag) for flag in SKIP_FLAGS)


class Findings:
    """带检查项标签的 ERROR / WARN 收集器（人读文本 + JSON 双出口）。"""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def error(self, check: str, text: str) -> None:
        self.errors.append({"check": check, "text": text})

    def warn(self, check: str, text: str) -> None:
        self.warnings.append({"check": check, "text": text})


# ---------------------------------------------------------------------------
# 检查 A：对象 key_attrs 完整性
# ---------------------------------------------------------------------------
def check_a(objects: list, type_specs: dict, f: Findings) -> int:
    pair_count = 0
    missing: dict = collections.defaultdict(list)
    unknown_types = collections.Counter()
    weird_layer = []

    for obj in objects:
        if not isinstance(obj, dict):
            f.error("A", "objects[] 存在非对象条目：%r" % (obj,))
            continue
        tname = obj.get("type")
        spec = type_specs.get(tname)
        if spec is None:
            unknown_types[repr(tname)] += 1
            continue
        if is_skipped(spec):
            continue
        if spec.get("layer", "MISSING") is None:
            weird_layer.append(type_name_of(spec))
        key_attrs = spec.get("key_attrs") or []
        if not key_attrs:
            continue
        attrs = obj.get("attrs")
        keyd = obj.get("key")
        attrs = attrs if isinstance(attrs, dict) else {}
        keyd = keyd if isinstance(keyd, dict) else {}
        for ka in key_attrs:
            pair_count += 1
            if ka in attrs and nonempty(attrs.get(ka)):
                continue
            if ka in keyd and nonempty(keyd.get(ka)):
                continue
            missing[(tname, ka)].append(obj.get("id"))

    for (tname, ka), ids in sorted(missing.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        declared = ", ".join(type_specs[tname].get("key_attrs") or [])
        if len(ids) == 1:
            f.error(
                "A",
                "%s 对象 %s 缺 key_attr %s（topology.yaml 声明 key_attrs=[%s]）"
                % (tname, ids[0], ka, declared),
            )
        else:
            f.error(
                "A",
                "%s 对象 缺 key_attr %s（topology.yaml 声明 key_attrs=[%s]）⇒ 共 %d 个对象\n"
                "            受影响对象（%d）：%s"
                % (tname, ka, declared, len(ids), len(ids), ", ".join(str(i) for i in ids)),
            )

    if unknown_types:
        for tname, n in sorted(unknown_types.items()):
            f.warn(
                "A",
                "快照出现 topology.yaml 未声明的对象类型 %s（%d 个对象）⇒ 跳过其 key_attrs 检查"
                % (tname, n),
            )
    if weird_layer:
        f.warn(
            "A",
            "类型 %s 参与实例检查却声明 layer: null ⇒ 契约上反常，建议复核"
            % ", ".join(sorted(set(weird_layer))),
        )
    return pair_count


def check_evidence_special(objects: list, type_specs: dict, f: Findings) -> dict:
    """Evidence 是派生标注类型：按实际契约处理，不靠猜。"""
    ev_spec = type_specs.get("Evidence")
    ev_objs = [o for o in objects if isinstance(o, dict) and o.get("type") == "Evidence"]
    if ev_objs:
        bad = []
        for o in ev_objs:
            attrs = o.get("attrs") if isinstance(o.get("attrs"), dict) else {}
            keyd = o.get("key") if isinstance(o.get("key"), dict) else {}
            if not (nonempty(attrs.get("recordId")) or nonempty(keyd.get("recordId"))):
                bad.append(o.get("id"))
        if bad:
            f.error(
                "A",
                "Evidence 对象缺 key_attr recordId ⇒ 共 %d 个对象（topology.yaml:205 声明 "
                "key_attrs=[recordId]）：%s" % (len(bad), ", ".join(str(i) for i in bad[:5])),
            )
        return {"mode": "enforced", "instances": len(ev_objs), "enforcedBecause": "实例存在"}
    if ev_spec is None:
        f.warn("A", "topology.yaml 未声明 Evidence 类型 ⇒ 无从判定，跳过（依据：%s）" % evidence_basis())
    else:
        f.warn(
            "A",
            "本快照 objects[] 无 type=Evidence 条目（0 个）⇒ 检查 A 对 Evidence 作 WARN 级豁免："
            "Evidence 不是 CCF 的对象类型，而是投影期由 observations[] 逐条派生，recordId 恒由观测 "
            "id 提供、实例层无可校验缺口。依据：%s" % evidence_basis(),
        )
    return {"mode": "exempt", "instances": 0, "enforcedBecause": "派生类型，objects[] 中无实例"}


# ---------------------------------------------------------------------------
# 检查 B：观测 delta 契约（X-7）
# ---------------------------------------------------------------------------
def check_b(observations: list, f: Findings) -> int:
    delta_obs = 0
    for obs in observations:
        if not isinstance(obs, dict):
            f.error("B", "observations[] 存在非对象条目：%r" % (obs,))
            continue
        tag = "检查B(delta) 观测 %s（metric=%s）" % (obs.get("id"), obs.get("metricCode"))
        has_delta = "delta" in obs
        has_window = "deltaWindowS" in obs
        if has_delta:
            delta_obs += 1
            if not is_finite_number(obs.get("delta")):
                f.error(
                    "B",
                    "%s 的 delta=%r 不是有限数值（要求 int 或 float；bool 不算数值、"
                    "NaN/Inf 不算有限。采集侧 app_probe.py 的 _counter_delta() 返回原样差值、"
                    "不取整 ⇒ 小数 delta 合法；%s）"
                    % (tag, obs.get("delta"), cite("delta", "delta")),
                )
            if not has_window:
                f.error(
                    "B",
                    "%s 有 delta=%r 但缺 deltaWindowS（%s 的 if/then 要求同现）"
                    % (tag, obs.get("delta"), cite("allOf[1]", "delta_cooccur")),
                )
            else:
                win = obs.get("deltaWindowS")
                if not is_int(win) or win <= 0:
                    f.error(
                        "B",
                        "%s 的 deltaWindowS=%r 不是正整数（要求 ≥ 1 的整数秒；bool 不算 int；%s）"
                        % (tag, win, cite("deltaWindowS", "deltaWindowS")),
                    )
        elif has_window:
            f.error(
                "B",
                "%s 有 deltaWindowS=%r 但缺 delta（%s 同现契约：出现窗口必须有增量）"
                % (tag, obs.get("deltaWindowS"), cite("allOf[1]", "delta_cooccur")),
            )
    if delta_obs == 0:
        f.warn(
            "B",
            "本快照无带 delta 观测 ⇒ 检查 B 空过（非通过）：X-7 增量契约本次未被实例覆盖，"
            "不得据此判定该契约成立",
        )
    return delta_obs


# ---------------------------------------------------------------------------
# 检查 C：反恒真下限
# ---------------------------------------------------------------------------
def check_c(pair_count: int, obs_count: int, min_keypairs: int, min_obs: int, f: Findings) -> None:
    if pair_count < min_keypairs:
        f.error(
            "C",
            "检查规模不足 ⇒ 疑似解析口径失配（反恒真下限）：实际核对 (对象,key_attr) 配对=%d，"
            "低于下限 %d。若对象类型字段名解析错了（真机 CCF 用 `type`，不是 `typeCode`），"
            "此处会塌缩到 0 —— 本门禁拒绝给出假绿" % (pair_count, min_keypairs),
        )
    if obs_count < min_obs:
        f.error(
            "C",
            "检查规模不足 ⇒ 疑似解析口径失配（反恒真下限）：实际扫描 observations=%d，"
            "低于下限 %d" % (obs_count, min_obs),
        )


# ---------------------------------------------------------------------------
# 检查 D：CCF 自洽最小集
# ---------------------------------------------------------------------------
def check_d(ccf: dict, objects: list, observations: list, f: Findings) -> dict:
    snap = ccf.get("snapshotId")
    snap_ok = isinstance(snap, str) and bool(SNAPSHOT_ID_RE.match(snap))
    if not snap_ok:
        f.error(
            "D",
            "检查D snapshotId 不匹配 ^ccf-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$：%r" % (snap,),
        )

    id_stats = {}
    for label, rows in (("对象", objects), ("观测", observations)):
        ids = [r.get("id") if isinstance(r, dict) else None for r in rows]
        blank = sum(1 for i in ids if not nonempty(i))
        if blank:
            f.error("D", "检查D %s id 为空/缺失 ⇒ 共 %d 条" % (label, blank))
        counter = collections.Counter(i for i in ids if nonempty(i))
        dups = sorted(i for i, n in counter.items() if n > 1)
        if dups:
            f.error(
                "D",
                "检查D %s id 不唯一 ⇒ 共 %d 个重复 id：%s"
                % (label, len(dups), ", ".join(map(str, dups[:5]))),
            )
        id_stats[label] = {"total": len(ids), "blank": blank, "duplicateIds": len(dups)}

    obj_ids = {r.get("id") for r in objects if isinstance(r, dict)}
    orphans = [
        o.get("id")
        for o in observations
        if isinstance(o, dict) and o.get("objectId") not in obj_ids
    ]
    if orphans:
        f.error(
            "D",
            "检查D 孤儿观测 ⇒ 共 %d 条观测的 objectId 无法解析到本快照对象：%s"
            % (len(orphans), ", ".join(map(str, orphans[:5]))),
        )
    id_stats["orphanObservations"] = len(orphans)

    for label, rows in (("objects", objects), ("observations", observations)):
        for r in rows:
            if not isinstance(r, dict):
                continue
            for field in ("attrs", "key"):
                if field in r and r[field] is not None and not isinstance(r[field], dict):
                    f.error(
                        "D",
                        "检查D %s 条目 %s 的 %s 不是对象（JSON object）：%r"
                        % (label, r.get("id"), field, r[field]),
                    )
    return {"snapshotIdValid": snap_ok, **id_stats}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_type_specs(topo: dict) -> dict:
    types = topo.get("types")
    if not isinstance(types, list) or not types:
        raise ValueError("topology.yaml 的 types 缺失或不是非空 list")
    specs: dict = {}
    for entry in types:
        if not isinstance(entry, dict):
            raise ValueError("topology.yaml types 存在非 dict 条目：%r" % (entry,))
        name = type_name_of(entry)
        if not name:
            raise ValueError("topology.yaml types 条目缺 code/type：%r" % (entry,))
        if name in specs:
            raise ValueError("topology.yaml types 重复定义：%s" % name)
        specs[name] = entry
    return specs


def run_gate(args) -> dict:
    ccf_path = Path(args.ccf)
    model_dir = Path(args.model_dir)
    if not ccf_path.is_file():
        raise FileNotFoundError("CCF 不存在或不是文件：%s" % ccf_path)
    topo_path = model_dir / "topology.yaml"
    if not topo_path.is_file():
        raise FileNotFoundError("topology.yaml 不存在：%s" % topo_path)

    import yaml  # PyYAML

    topo = yaml.safe_load(read_text(topo_path))
    if not isinstance(topo, dict):
        raise ValueError("topology.yaml 顶层不是映射：%r" % type(topo).__name__)
    type_specs = build_type_specs(topo)
    schema_anchors = load_schema_anchors(model_dir)

    ccf = load_json_file(ccf_path)
    if not isinstance(ccf, dict):
        raise ValueError("CCF 顶层不是 JSON object：%s" % type(ccf).__name__)
    objects = ccf.get("objects")
    observations = ccf.get("observations")
    relations = ccf.get("relations")
    if not isinstance(objects, list):
        raise ValueError("CCF 缺 objects[]（或不是 list）")
    if not isinstance(observations, list):
        raise ValueError("CCF 缺 observations[]（或不是 list）")
    if not isinstance(relations, list):
        relations = []

    f = Findings()
    pair_count = check_a(objects, type_specs, f)
    evidence = check_evidence_special(objects, type_specs, f)
    delta_obs = check_b(observations, f)
    check_c(pair_count, len(observations), args.min_keypairs, args.min_observations, f)
    detail = check_d(ccf, objects, observations, f)

    by_type = collections.Counter(
        o.get("type") if isinstance(o, dict) else "<non-object>" for o in objects
    )
    return {
        "gate": GATE_ID,
        "alias": GATE_ALIAS,
        "title": GATE_TITLE,
        "ccf": str(ccf_path.resolve()),
        "modelDir": str(model_dir.resolve()),
        "topology": str(topo_path.resolve()),
        "schemaAnchors": schema_anchors,
        "schemaFacts": schema_facts(),
        "snapshotId": ccf.get("snapshotId"),
        "ccfVersion": ccf.get("ccfVersion"),
        "counts": {
            "objects": len(objects),
            "relations": len(relations),
            "observations": len(observations),
            "keyPairs": pair_count,
            "deltaObservations": delta_obs,
        },
        "types": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], str(kv[0])))),
        "thresholds": {
            "minKeyPairs": args.min_keypairs,
            "minObservations": args.min_observations,
        },
        "evidence": evidence,
        "checks": {
            "A": {
                "what": "对象 key_attrs 完整性",
                "keyPairs": pair_count,
                "errors": sum(1 for e in f.errors if e["check"] == "A"),
            },
            "B": {
                "what": "观测 delta 契约同进同出 + 值域（X-7）",
                "judgements": [
                    "有 delta ⇒ 必须有 deltaWindowS（同现）",
                    "delta 必须是有限数值（int/float 皆可；bool 非数值；NaN/Inf 非有限）",
                    "deltaWindowS 必须是正整数（≥1 秒）",
                    "有 deltaWindowS ⇒ 必须有 delta（反向同现）",
                ],
                "deltaObservations": delta_obs,
                "errors": sum(1 for e in f.errors if e["check"] == "B"),
            },
            "C": {
                "what": "反恒真下限",
                "errors": sum(1 for e in f.errors if e["check"] == "C"),
            },
            "D": {
                "what": "CCF 自洽最小集",
                "errors": sum(1 for e in f.errors if e["check"] == "D"),
                **detail,
            },
        },
        "errors": f.errors,
        "warnings": f.warnings,
        "errorCount": len(f.errors),
        "warnCount": len(f.warnings),
        "result": "FAIL" if f.errors else "PASS",
    }


def format_report(res: dict) -> str:
    lines = []
    counts = res["counts"]
    th = res["thresholds"]
    lines.append(BAR)
    lines.append("%s / %s · %s（M2.14）" % (res["gate"], res["alias"], res["title"]))
    lines.append(RULE)
    lines.append("  ccf         : %s" % res["ccf"])
    lines.append("  model-dir   : %s" % res["modelDir"])
    lines.append("  snapshotId  : %s（ccfVersion=%s）" % (res["snapshotId"], res["ccfVersion"]))
    lines.append(
        "  规模         : objects=%d relations=%d observations=%d"
        % (counts["objects"], counts["relations"], counts["observations"])
    )
    lines.append("  对象类型分布  : %s" % "  ".join("%s=%d" % (k, v) for k, v in res["types"].items()))
    lines.append(
        "  检查规模     : (对象,key_attr) 配对=%d（下限 %d）｜扫描 observations=%d（下限 %d）｜delta 观测=%d"
        % (counts["keyPairs"], th["minKeyPairs"], counts["observations"], th["minObservations"],
           counts["deltaObservations"])
    )
    lines.append(
        "  Evidence    : %s（实例 %d）"
        % (res["evidence"]["mode"], res["evidence"]["instances"])
    )
    sa = res.get("schemaAnchors") or {}
    sf = res.get("schemaFacts") or {}
    lines.append(
        "  契约锚点     : ccf.schema.json %s"
        % ("、".join(
            "%s%s%s" % (k, ":%d" % v if v else "（未定位）",
                        "（现行声明 %s）" % sf[k] if sf.get(k) else "")
            for k, v in sorted(sa.items()))
           or "（schema 不可读 ⇒ 引用退化为字段路径）")
    )
    lines.append(RULE)
    if res["errors"]:
        for e in res["errors"]:
            lines.append("[%s/ERROR] %s" % (res["gate"], e["text"]))
    else:
        lines.append("[%s] 无 ERROR。" % res["gate"])
    for w in res["warnings"]:
        lines.append("[%s/WARN] %s" % (res["gate"], w["text"]))
    lines.append(RULE)
    c = res["checks"]
    lines.append(
        "  逐项：A key_attrs 完整性 ERROR=%d（%d 配对）｜B delta 契约 ERROR=%d（%d 条 delta 观测）"
        "｜C 反恒真下限 ERROR=%d｜D 自洽最小集 ERROR=%d"
        % (c["A"]["errors"], c["A"]["keyPairs"], c["B"]["errors"], c["B"]["deltaObservations"],
           c["C"]["errors"], c["D"]["errors"])
    )
    lines.append("RESULT: %s  (ERROR=%d WARN=%d)" % (res["result"], res["errorCount"], res["warnCount"]))
    return "\n".join(lines)


class FailClosedParser(argparse.ArgumentParser):
    """用法错误也必须 fail-closed：打印 RESULT: FAIL 并 exit 1（而不是 argparse 默认的 2）。"""

    def error(self, message):  # noqa: D102
        print("%s\n[%s/ERROR] 用法错误：%s\nRESULT: FAIL  (ERROR=1 WARN=0)\n%s"
              % (BAR, GATE_ID, message, BAR))
        raise SystemExit(1)


def parse_args(argv=None):
    p = FailClosedParser(
        prog="ccf_instance_gate.py",
        description="%s / %s · %s" % (GATE_ID, GATE_ALIAS, GATE_TITLE),
    )
    p.add_argument("--ccf", required=True, help="待校验的 CCF 快照（只读）")
    p.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR),
                   help="契约目录，需含 topology.yaml（默认 shared/model）")
    p.add_argument("--min-keypairs", type=int, default=40,
                   help="检查 A 至少核对的 (对象,key_attr) 配对数下限（反恒真，默认 40）")
    p.add_argument("--min-observations", type=int, default=100,
                   help="至少扫描的 observations 条数下限（反恒真，默认 100）")
    p.add_argument("--json", action="store_true", help="仅打印 JSON 结果（供脚本消费）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        res = run_gate(args)
    except BaseException as exc:  # noqa: BLE001 —— fail-closed：任何异常都不许静默通过
        print(BAR)
        print("[%s/ERROR] 内部异常 ⇒ fail-closed（绝不静默通过）：%s: %s"
              % (GATE_ID, type(exc).__name__, exc))
        traceback.print_exc()
        print("RESULT: FAIL  (ERROR=1 WARN=0)")
        print(BAR)
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(format_report(res))
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
