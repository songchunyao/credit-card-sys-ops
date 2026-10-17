# -*- coding: utf-8 -*-
"""M5 · 逐用例判据（机判优先，docs/06 §3.2）。

判据与 docs/06 的逐条映射：
  ``root_cause_accuracy``   §2.1（**只取第一个候选**）      :func:`judge_record`
  ``layer_accuracy``        §1.2 表（答案中声明的层命中）
  ``path_f1``               §1.2 表 / §2.1（集合 F1）
  ``object_accuracy``       §1.2 表（D1 现象定位）——★W46 起 = ``M-ID-SET v1``：
                            按 id 记号抽取 + **集合成员**判定（旧「归一化子串命中」
                            会让 ``session:sess-0`` 假命中 ``session:sess-01``）
  ``false_claim_rate``      §2.1 + **移植自 ``<workspace>\\upstream-eval\\score.py``**
  ``mixed_precision``       §2.1「同 upstream-eval\\score.py」
  ``abstain``               §2.1（单列：既不计对也不计错）
  证据命中                   §1.2 表（``evidence_facts`` id 成员判定；``detectors`` 辅助通道）

★★ 移植声明（任务书要求标明来源行号）：
  否定邻窗消歧逻辑**忠实移植**自 ``<workspace>\\upstream-eval\\score.py``：
    * ``score.py:44``      ``NEG = r"(无|没有|未发现|未检测到|不存在|正常|为\\s*0|=\\s*0|很低|不高|充足|充裕|未见|不构成|无异常)"``
    * ``score.py:65-76``   ``_false_claims()``：对每个 claim 模式 ``re.finditer``，取
                           ``text[max(0,m.start()-25) : min(len,m.end()+25)]`` 邻窗，
                           邻窗内命中 ``NEG`` ⇒ ``continue``（被否定/报为正常 ⇒ **不是**虚假归因），
                           否则记该 id 并 ``break``（每个 id 只记一次）
    * ``score.py:89-114``  ``score_record()``：调用关系与 ``mixed_precision`` 公式
                           ``pos_hit / (pos_hit + FC)``（``score.py:112-113``）
  ★ 只移植**逻辑**，**不移植文本**——docs/06 §1.5 明确「原 39 条规则的**文本**不复用」，
    故 ``FALSE_CLAIMS`` 的 5 条正则（GaussDB/存储域）在本模块中一律丢去，
    改为对**用例自带的 ``neg`` 词表**施加同一套否定邻窗消歧。

符号消歧陷阱（必须记录）：``score.py`` 的 ``NEG`` 是**否定词正则**，
而 docs/06 §1.2 用例的 ``neg`` 字段是**错误归因词表**；两者**同名不同义**。
本模块把前者改名 ``NEGATION_RE``，后者沿用 ``neg``。
"""
from __future__ import annotations

import functools
import hashlib
import json
import re

from .contract import (NONE_SENTINEL, OBJECT_HIT_RULE_VERSION, extract_id_tokens,
                       id_hits)

# ── 移植常量（来源行号见模块 docstring）─────────────────────────────────────
NEGATION_RE = r"(无|没有|未发现|未检测到|不存在|正常|为\s*0|=\s*0|很低|不高|充足|充裕|未见|不构成|无异常)"  # score.py:44
FALSE_CLAIM_WINDOW = 25  # score.py:69-71 的 ±25 字符
#: ★ M-NORM-CONCL v1（2026-10-14）虚假归因判据版本。存在理由见
#:   `_split_mentions_vs_claims` 的 docstring：区分「枚举被检查的模式」与「断言故障存在」。
FALSE_CLAIM_RULE_VERSION = "M-NORM-CONCL v1"
#: `case.neg` 里含 `|` 的词条 = **断言式**（如 `确认为故障|存在故障|定位到根因`）；
#: 其余为**裸故障码**。「断言式是否命中」是本修正的核心判别量。
ASSERTION_PHRASE_MARK = "|"

# ── 层词表（★ judge 内部常量，**不是** docs/06 内容 ⇒ 需契约方复核）─────────
# docs/06 只给出 layer 取值 App / Runtime / Data（§2.3「改动文件覆盖的 App/Runtime/Data 层数」），
# **未**给出自然语言别名表 ⇒ 本表是机器判据的**新增假设**，版本化并全部披露。
LAYER_LEXICON_VERSION = "L-LAYER v1"
LAYER_LEXICON = {
    "App": ("应用层", "应用服务", "应用", "App", "业务层", "服务层"),
    "Runtime": ("运行时", "容器", "宿主机", "主机层", "Runtime", "Pod", "K8s"),
    "Data": ("数据层", "数据库层", "数据库", "数据服务", "Data", "GaussDB", "存储层"),
}
LAYER_ALIAS_TO_CODE = {alias.lower(): code for code, aliases in LAYER_LEXICON.items()
                       for alias in aliases}
LAYER_CODES = tuple(LAYER_LEXICON)


@functools.lru_cache(maxsize=8192)
def _rx(pattern: str):
    return re.compile(pattern, re.I)


def normalise_layer_value(value):
    """把 ``expect_layer`` 归一为层代码（App/Runtime/Data）或原值。"""
    if value in LAYER_CODES:
        return value
    low = str(value).strip().lower()
    if low in LAYER_ALIAS_TO_CODE:
        return LAYER_ALIAS_TO_CODE[low]
    return None  # 词表外的取值 ⇒ 判据退回「字面命中」并记录 WARN


def build_vocabulary(cases: list) -> dict:
    """构建故障模式编码闭词表（来自用例集自身）。

    词表 = 全部 ``expect_root_cause`` ∪ 全部 ``injected`` ∪ 全部 ``expect_path`` 成员
    （去掉 ``NONE`` 哨兵）。**闭词表**意味着不猜词表外的名称；自然语言表述由
    ``detectors`` 正则辅助通道覆盖（docs/06 §1.2「正则，用于无法靠 id 匹配的自然语言表述」）。
    """
    codes = set()
    for c in cases:
        rc = c["expect_root_cause"]
        if rc and rc != NONE_SENTINEL:
            codes.add(rc)
        for f in c.get("injected") or []:
            if f != NONE_SENTINEL:
                codes.add(f)
        for f in c.get("expect_path") or []:
            if f != NONE_SENTINEL:
                codes.add(f)
    ordered = sorted(codes)
    return {
        "codes": ordered,
        "codes_n": len(ordered),
        "normalised": {re.sub(r"[-_\s]", "", c).lower(): c for c in ordered},
    }


def _code_hits(text: str, vocab: dict) -> list:
    """按文本位置找出词表编码命中；并施加同一套否定邻窗消歧。

    返回 ``[{code, start, end, negated, window}]``，按 (start, code) 排序。
    """
    hits = []
    for code in vocab["codes"]:
        for m in _rx(re.escape(code)).finditer(text):
            window = text[max(0, m.start() - FALSE_CLAIM_WINDOW):
                          min(len(text), m.end() + FALSE_CLAIM_WINDOW)]
            negated = bool(_rx(NEGATION_RE).search(window))
            hits.append({"code": code, "start": m.start(), "end": m.end(),
                         "negated": negated, "window": window})
    # 归一化形式（`-`/`_`/空格 不敏感）——只在逐字未命中时补充
    if not hits:
        for norm, code in vocab["normalised"].items():
            for m in _rx(re.escape(norm)).finditer(re.sub(r"[-_\s]", "", text).lower()):
                hits.append({"code": code, "start": m.start(), "end": m.end(),
                             "negated": False, "window": "", "normalised_form": True})
    hits.sort(key=lambda h: (h["start"], h["code"]))
    return hits


def _detector_hits(text: str, cases: list) -> list:
    """辅助通道：用例 ``detectors`` 正则在答案上的命中（含跨用例归属）。

    只纳入 ``expect_root_cause != NONE`` 的用例（族C5 的 detectors 描述**正常态**，
    不是故障候选）。返回按 (start, -len(detector), case_id) 排序的命中列表。
    """
    hits = []
    for c in cases:
        rc = c["expect_root_cause"]
        if rc == NONE_SENTINEL:
            continue
        for det in c["detectors"]:
            m = _rx(det).search(text)
            if m:
                hits.append({"case_id": c["id"], "code": rc, "detector": det,
                             "start": m.start(), "end": m.end(), "matched": m.group(0)})
    hits.sort(key=lambda h: (h["start"], -len(h["detector"]), h["case_id"]))
    return hits


def _false_claims(text: str, neg_phrases: list) -> list:
    """★ 否定邻窗消歧（``upstream-eval\\score.py:65-76`` 的忠实移植）。

    对每个 ``neg`` 词条：``finditer`` → 取 ±25 邻窗 → 邻窗命中 ``NEGATION_RE`` 则跳过
    （被否定 / 报为正常 ⇒ 不是虚假归因），否则记录一次并 ``break``（每个词条只记一次）。
    """
    found = []
    for phrase in neg_phrases:
        for m in _rx(phrase).finditer(text):
            window = text[max(0, m.start() - FALSE_CLAIM_WINDOW):
                          min(len(text), m.end() + FALSE_CLAIM_WINDOW)]
            if _rx(NEGATION_RE).search(window):
                continue  # 被否定 / 报为正常 ⇒ 不是虚假归因
            found.append({"phrase": phrase, "start": m.start(), "end": m.end(),
                          "matched": m.group(0), "window": window})
            break
    return found


def _split_mentions_vs_claims(case: dict, expect_rc, ans: str,
                              false_claims: list, nor_hits: list):
    """★ M-NORM-CONCL v1：把 `false_claims` 拆成 (mentions_not_claims, effective_false_claims)。

    只对 `expect_root_cause == NONE`（族C5 正常态）生效，且要求**三重条件**同时成立：
      ① 答案命中 `nor`  —— 结论已明确「无可确认的故障」；
      ② 未命中任何**断言式**词条（`case.neg` 里含 `|` 者）—— 未作出「确认为故障/存在故障/定位到根因」这类断言；
      ③ 被重分类的条目必须是**裸故障码**（不含 `|`），断言式条目一律保留。
    成立时：裸故障码的出现 = **解释性枚举**（「我检查了这些模式及其判据」），不计为虚假归因。

    为什么必须这样改（实测依据 Z-01 / cc-kg-agent）：
      该答案结论为「当前应用层不存在可确认的故障」，并逐条说明「虽然未触发
      FM-APP-HEAP-PRESSURE（heapUsedRatio 仅 4.13%）…故当前不构成故障」。
      但 ±25 字否定邻窗在**枚举清单**内没有否定词 ⇒ 被判 6 条误报 ⇒ 该用例判错。
      C5 题面要求判断「是否存在**可据判据确认**的故障」，严谨回答必然先枚举后结论，
      故按「是否出现故障码」判归因，等于**奖励含糊、惩罚逐条核对**。

    留痕：被重分类的条目原样返回，判据记录写进 `mentions_not_claims`（可审计、可复算）；
    判据版本号写进 `falseClaimRuleVersion`。
    """
    if expect_rc != NONE_SENTINEL:
        return [], list(false_claims)
    # ★ fail-closed 前提：判据面必须**给出**断言式词条，否则没有区分「枚举」与「断言」的
    #   判别量 ⇒ 一律不重分类，保持原严格语义（避免在别的用例集上过度抑制）。
    assertion_pool = [p for p in case.get("neg") or [] if ASSERTION_PHRASE_MARK in str(p)]
    if not assertion_pool:
        return [], list(false_claims)
    # 答案**命中**了任何断言式词条 ⇒ 它确实作出了「存在故障」这类断言 ⇒ 不重分类。
    if any(_rx(p).search(ans) for p in assertion_pool):
        return [], list(false_claims)
    bare = [c for c in false_claims if ASSERTION_PHRASE_MARK not in str(c.get("phrase"))]
    if not nor_hits or not bare:
        return [], list(false_claims)
    return bare, []


def _layer_mentions(text: str) -> list:
    """文本中出现的层别名（按位置排序，同位置长别名优先）。"""
    mentions = []
    low = text.lower()
    for code, aliases in LAYER_LEXICON.items():
        for alias in aliases:
            for m in re.finditer(re.escape(alias.lower()), low):
                mentions.append({"layer": code, "alias": alias,
                                 "start": m.start(), "end": m.end()})
    mentions.sort(key=lambda x: (x["start"], -(x["end"] - x["start"]), x["layer"]))
    return mentions


def _nearest_layer(mentions: list, span):
    if not mentions:
        return None
    if span is None:
        return mentions[0]
    lo, hi = span
    centre = (lo + hi) / 2.0
    best = min(mentions, key=lambda m: (abs((m["start"] + m["end"]) / 2.0 - centre),
                                        m["start"], m["layer"]))
    return best


#: ★ `docs/06` §2.1「明确拒绝定位」的措辞锚点。
#: 纪律：**宁可少判**（漏判 ⇒ 该条按答错计，偏保守），**不可多判**（多判会人为抬高 abstain 列）。
#: 因此除命中措辞外，还要求正文**没有**给出任何根因候选（见 `derive_abstain`）。
REFUSAL_RE = _rx(r"无法定位|不足以判定|无法判定|不能判定|无法确定根因|证据不足")

#: `abstain_kind` 的取值域（报告侧据此分开列示，**不合并**成一个数）
ABSTAIN_KINDS = ("declared", "explicit_refusal", "no_answer", "not_measured")


def derive_abstain(rec: dict, ans: str, live_code_hits: list, det_hits: list):
    """推导一条记录是否 `abstain`，返回 ``(abstain, kind)``。

    ★ 口径修复（2026-10-07，随 R-54 落地）：此前 `judge_record` **只读** ``rec["abstain"]``，
    而运行台**有意不写**该键（`m5_run.py` 的 `run_manifest.honesty` 自述「abstain 由 W18 依
    answer_text 判定」）⇒ `abstain` **恒为 False**：弃答与失败被静默计入**答错**，
    违反 `docs/05` §4.3「步数用满导致 answer 为空，该次必须记为 abstain，**不得悄悄丢弃**」，
    并使 `abstain_rate` 恒为 0、`denominator_abstain_as_error` 敏感性分析失去意义。

    两处权威口径同时落地，类别**分开登记**：

    ==================  ==========================================================
    `declared`          记录里显式写了 ``abstain: true``（夹具 / 外部标注用）
    `explicit_refusal`  `docs/06` §2.1「**明确拒绝定位**」：正文命中拒答措辞，
                        且**没有**任何根因候选。给了候选又加免责话术的 ⇒ **不算**弃答
    `no_answer`         `docs/05` §4.3：**调用了但没产出可判答案** —— 运行台
                        ``status=failed`` / 答案为空 / 有 SSE error
    `not_measured`      运行台 ``status ∈ {blocked, skipped}`` ⇒ **没测到**
                        （BLOCKED ≠ FAIL）。单列，报告侧可从 `abstain_rate` 中剔除
    ==================  ==========================================================
    """
    if rec.get("abstain"):
        return True, "declared"
    status = str(rec.get("status") or "ok")
    if status in ("blocked", "skipped"):
        return True, "not_measured"
    if status != "ok":
        return True, "no_answer"
    if not (ans or "").strip():
        return True, "no_answer"
    if rec.get("sse_error"):
        return True, "no_answer"
    if REFUSAL_RE.search(ans or "") and not live_code_hits and not det_hits:
        return True, "explicit_refusal"
    return False, None


def _carrier_field(rec: dict, fallback: dict, key: str):
    """容器侧字段：记录优先，缺则用臂级聚合、仍缺则 None（**绝不补 0**）。"""
    v = rec.get(key)
    if v is not None:
        return v
    return (fallback or {}).get(key)


def judge_record(rec: dict, cases: list, vocab: dict, *, case_index=None, carrier=None) -> dict:
    """判一条 ``(case_id, group, repeat)`` 答案，返回逐用例判据明细（可复核）。"""
    case = rec["_case"]
    ans = rec.get("answer_text") or ""
    family = case["_family"]
    expect_rc = case["expect_root_cause"]

    code_hits = _code_hits(ans, vocab)
    live_code_hits = [h for h in code_hits if not h["negated"]]
    det_hits = _detector_hits(ans, cases)
    # ★ 必须在拿到候选命中之后再判 abstain：`explicit_refusal` 的判据依赖「无候选」
    abstain, abstain_kind = derive_abstain(rec, ans, live_code_hits, det_hits)
    detected_codes = sorted({h["code"] for h in live_code_hits} |
                            {h["code"] for h in det_hits})

    # ── 第一个候选（★ docs/06 §2.1 约定 3：一题多答只取第一个）──────────────
    if live_code_hits:
        first = live_code_hits[0]
        first_candidate = first["code"]
        candidate_source = "code"
        candidate_span = (first["start"], first["end"])
        candidate_matched = first["code"]
    elif det_hits:
        first = det_hits[0]
        first_candidate = first["code"]
        candidate_source = "detector"
        candidate_span = (first["start"], first["end"])
        candidate_matched = first["matched"]
    else:
        first_candidate = None
        candidate_source = "none"
        candidate_span = None
        candidate_matched = None

    any_fault_asserted = bool(live_code_hits) or bool(det_hits)
    false_claims = _false_claims(ans, case["neg"])
    nor_hits = [p for p in case["nor"] if _rx(p).search(ans)]
    pos_hits = [p for p in case["pos"] if _rx(p).search(ans)]
    det_self_hit = any(_rx(d).search(ans) for d in case["detectors"])
    # ★ M-NORM-CONCL v1：区分「枚举被检查的故障模式」与「断言故障存在」
    mentions_not_claims, false_claims_eff = _split_mentions_vs_claims(
        case, expect_rc, ans, false_claims, nor_hits)

    # ── 层：声明层 = 距「第一个候选」最近的层别名（judge 内部规则，见模块 docstring）
    mentions = _layer_mentions(ans)
    declared = _nearest_layer(mentions, candidate_span)
    declared_layer = declared["layer"] if declared else None
    expect_layer = case["expect_layer"]
    layer_warn = None
    if expect_layer == NONE_SENTINEL:
        layer_hit = not any_fault_asserted
    else:
        norm_expect = normalise_layer_value(expect_layer)
        if norm_expect is None:
            layer_warn = f"expect_layer={expect_layer!r} 不在 {LAYER_LEXICON_VERSION} 词表中，退回字面命中"
            layer_hit = expect_layer.lower() in ans.lower()
        else:
            layer_hit = declared_layer == norm_expect
    layer_any_match = bool(
        expect_layer == NONE_SENTINEL and not any_fault_asserted) or any(
        m["layer"] == (normalise_layer_value(expect_layer) or "") for m in mentions)

    # ── 路径 F1（集合）
    expected_path = [p for p in case["expect_path"] if p != NONE_SENTINEL]
    detected_set = set(detected_codes)
    expected_set = set(expected_path)
    inter = detected_set & expected_set
    if not expected_set:
        path_f1 = 1.0 if not detected_set else 0.0
        path_precision = path_recall = None
    else:
        path_precision = len(inter) / len(detected_set) if detected_set else 0.0
        path_recall = len(inter) / len(expected_set)
        path_f1 = (2 * path_precision * path_recall / (path_precision + path_recall)
                   if (path_precision + path_recall) > 0 else 0.0)

    # ── 对象命中（all = 主判据，any = 次判据）
    # ★ W46 缺陷①：旧实现是 `norm(o) in low_ans` 的**子串**包含 ⇒ `session:sess-0`
    #   会假命中 `session:sess-01`，且抬高幅度取决于答案把 id 写得多啰嗦（威胁 A/B 公平性）。
    #   现改为 M-ID-SET v1：先按 id 记号抽集合，再做**集合成员**判定（口径见
    #   contract.py:ID_PREFIX_BY_TYPE_KEY / OBJECT_HIT_RULE_VERSION）。
    id_tokens = extract_id_tokens(ans)
    matched_objs, matched_via = id_hits(case["expect_objects"], id_tokens)
    if case["expect_objects"]:
        object_hit_all = len(matched_objs) == len(case["expect_objects"])
        object_hit_any = bool(matched_objs)
    else:
        object_hit_all = object_hit_any = (not any_fault_asserted)

    # ── 证据命中（id 成员判定；detectors 辅助通道）
    evidence_ids = case["evidence_facts"]
    ev_hit_ids, _ = id_hits(evidence_ids, id_tokens)
    if evidence_ids:
        evidence_hit = bool(ev_hit_ids)
        evidence_all = len(ev_hit_ids) == len(evidence_ids)
    else:
        evidence_hit = None
        evidence_all = None
    evidence_aux_hit = det_self_hit

    # ── 根因命中
    c5_warn = None
    if expect_rc == NONE_SENTINEL:
        normal_signal = bool(nor_hits) or det_self_hit
        # ★ M-NORM-CONCL v1：故障码的出现若已被判为「解释性枚举」，不再算作断言
        effective_asserted = any_fault_asserted and not mentions_not_claims
        root_cause_hit = ((not effective_asserted) and (not false_claims_eff)
                          and normal_signal)
        if not (bool(nor_hits) or det_self_hit):
            c5_warn = "族C5 既无 nor 命中也无 detector 命中 ⇒ 正常态无正向信号（该用例判据偏严）"
    else:
        root_cause_hit = (first_candidate == expect_rc)

    # ── mixed_precision（★ score.py:112-113 公式逐字）
    fc_n = len(false_claims_eff)
    if (len(pos_hits) + fc_n) > 0:
        mixed_precision = len(pos_hits) / (len(pos_hits) + fc_n)
    else:
        mixed_precision = None  # score.py 在同一情形返回 None（不是 0）

    # ── 复合分（仅用于 G6 区分度；定义完全披露）
    components = [1.0 if root_cause_hit else 0.0,
                  1.0 if layer_hit else 0.0,
                  float(path_f1),
                  1.0 if object_hit_all else 0.0,
                  0.0 if false_claims else 1.0]
    if evidence_hit is not None:
        components.append(1.0 if evidence_hit else 0.0)
    case_score = None if abstain else sum(components) / len(components)

    verdict = {
        "schema": "m5.verdict/1",
        "case_id": case["id"], "group": rec["group"], "repeat": rec["repeat"],
        "family": family, "family_label": f"族{family}",
        "cls": case["cls"], "difficulty": case["difficulty"], "chain": case["chain"],
        "abstain": abstain,
        # ★ 口径修复（2026-10-07）：弃答的**类别**，报告侧据此分开列示（见 derive_abstain）
        "abstain_kind": abstain_kind,
        "run_status": str(rec.get("status") or "ok"),
        "root_cause": {
            "expect": expect_rc, "hit": None if abstain else bool(root_cause_hit),
            "first_candidate": first_candidate, "candidate_source": candidate_source,
            "candidate_matched_text": candidate_matched,
            "candidate_span": list(candidate_span) if candidate_span else None,
            "code_hits": [{"code": h["code"], "start": h["start"], "negated": h["negated"],
                           "window": h["window"]} for h in code_hits],
            "detector_hits": [{"case_id": h["case_id"], "code": h["code"],
                               "detector": h["detector"], "start": h["start"],
                               "matched": h["matched"]} for h in det_hits],
            "warning": c5_warn,
        },
        "layer": {
            "expect": expect_layer, "hit": None if abstain else bool(layer_hit),
            "declared": declared_layer,
            "declared_alias": declared["alias"] if declared else None,
            "declared_span": [declared["start"], declared["end"]] if declared else None,
            "first_mention": mentions[0]["layer"] if mentions else None,
            "any_match": bool(layer_any_match),
            "mentions": [{"layer": m["layer"], "alias": m["alias"], "start": m["start"]}
                         for m in mentions],
            "warning": layer_warn,
        },
        "path": {
            "expect": expected_path, "detected": detected_codes,
            "intersection": sorted(inter), "precision": path_precision,
            "recall": path_recall, "f1": round(float(path_f1), 6),
        },
        "objects": {
            "expect": case["expect_objects"], "matched": matched_objs,
            "hit_all": None if abstain else bool(object_hit_all),
            "hit_any": None if abstain else bool(object_hit_any),
            # ★ W46 披露面：命中通道逐条可见（literal / inst-alias），以及抽取到的
            #   id 记号个数。不落全量 had_ids（answer_text + rule 的纯函数，可复核；
            #   落全量会让 verdicts.jsonl 无谓膨胀）。
            "matched_via": {str(k): v for k, v in matched_via.items()},
            "extracted_n": len(id_tokens),
            "rule": OBJECT_HIT_RULE_VERSION,
        },
        "evidence": {
            "expect": evidence_ids, "hit_ids": ev_hit_ids,
            "hit": None if abstain else evidence_hit,
            "all": None if abstain else evidence_all,
            "aux_detector_hit": bool(evidence_aux_hit),
            "detectors": case["detectors"],
        },
        "false_claim": {
            "neg_phrases": case["neg"], "claims": false_claims_eff, "n": fc_n,
            # ★ M-NORM-CONCL v1 留痕：被重分类为「解释性枚举」的裸故障码条目
            "mentions_not_claims": mentions_not_claims,
            "n_mentions_not_claims": len(mentions_not_claims),
            "claims_raw": false_claims, "n_raw": len(false_claims),
            "ruleVersion": FALSE_CLAIM_RULE_VERSION,
            "any": bool(false_claims),
            "logic_source": ("FALSE_CLAIMS 否定邻窗消歧，移植自 "
                             "<workspace>\\upstream-eval\\score.py:44(NEG) + :65-76(_false_claims) "
                             "+ :89-114(score_record 调用面)；窗口 ±25 字符；文本不复用（docs/06 §1.5）"),
        },
        "pos": {"phrases": case["pos"], "hit_ids": pos_hits, "n_hit": len(pos_hits)},
        "nor": {"phrases": case["nor"], "hit_ids": nor_hits, "n_hit": len(nor_hits)},
        "mixed_precision": None if mixed_precision is None else round(mixed_precision, 6),
        "case_score": None if case_score is None else round(case_score, 6),
        "answer_sha256": hashlib.sha256(ans.encode("utf-8")).hexdigest(),
        "answer_len": len(ans),
        "telemetry": efficiency_from_record(rec, carrier),
        "_hit": None if abstain else bool(root_cause_hit),
        "_layer_hit": None if abstain else bool(layer_hit),
        "_object_all": None if abstain else bool(object_hit_all),
        "_object_any": None if abstain else bool(object_hit_any),
        "_evidence_hit": None if abstain else evidence_hit,
        "_false_claim_any": None if abstain else bool(false_claims_eff),
        # ★ M-NORM-CONCL v1：未修正前的原值（审计用；主指标只看 _false_claim_any）
        "_false_claim_raw_any": None if abstain else bool(false_claims),
        "_path_f1": None if abstain else float(path_f1),
    }
    return verdict


def efficiency_from_record(rec: dict, fallback: dict = None) -> dict:
    """效率遥测提取（docs/06 §2.2；口径来源 ``upstream-eval\\harness.py:118-132``）。

    ★ **宁缺勿造**（对齐 docs/06 §4 P11 的纪律）：缺失字段一律 ``None``，
      **绝不补 0**；并逐字段给出覆盖率。
    ``tool_calls_n`` 的**去重口径**移植自 ``harness.py:109-117``：
      连续相同的 (tool, input) 视为「调用/结果对」，只保留 observation 更大者。
      本契约的 ``tool_events`` 只有 ``name``/``status``；仅当条目内出现 ``input`` 时
      才用它做去重键，并把实际用到的键字段记录在 ``dedup_key_fields``。
    """
    te = rec.get("tool_events")
    if te is None:
        raw_n = dedup_n = obs_total = None
        key_fields = None
    else:
        key_fields = ["name"]
        if any("input" in e for e in te):
            key_fields.append("input")
        calls = []
        for e in te:
            if "input" in e:
                key = (e.get("name"), json.dumps(e.get("input"), sort_keys=True,
                                                 ensure_ascii=False))
            else:
                key = (e.get("name"),)
            calls.append({"key": key, "obs_len": e.get("obs_len") or 0})
        uniq = []
        for c in calls:
            if uniq and uniq[-1]["key"] == c["key"]:
                if c["obs_len"] > uniq[-1]["obs_len"]:
                    uniq[-1] = c
                continue
            uniq.append(c)
        raw_n = len(calls)
        dedup_n = len(uniq)
        obs_total = (sum(c["obs_len"] for c in uniq)
                     if any(c["obs_len"] for c in uniq) else None)
    out = {
        "elapsed_s": rec.get("elapsed_s"), "ttft_s": rec.get("ttft_s"),
        "input_tokens": rec.get("input_tokens"), "output_tokens": rec.get("output_tokens"),
        "tool_calls_n": dedup_n, "tool_calls_n_raw": raw_n,
        "dedup_key_fields": key_fields,
        "obs_total_bytes": obs_total,
        "iterations_used": rec.get("iterations_used"),
        # ★ 2026-10-09：容器侧三字段允许**臂级回退**（审计链无 case_id ⇒ 只能臂级；
        #   来源见 shared/model/m6-carrier-metrics.yaml 与 m6/carrier_efficiency.py）。
        #   回退只在记录**缺**该字段时生效，且**逐字段记录来源**（仍不补 0）。
        "query_latency_p50": _carrier_field(rec, fallback, "query_latency_p50"),
        "query_latency_p95": _carrier_field(rec, fallback, "query_latency_p95"),
        "facts_consumed": _carrier_field(rec, fallback, "facts_consumed"),
        "carrier_source": {k: ("record" if rec.get(k) is not None
                               else ("arm_aggregate" if (fallback or {}).get(k) is not None else "absent"))
                           for k in ("query_latency_p50", "query_latency_p95", "facts_consumed")},
        "carrier_granularity": ("arm" if fallback else "record_or_absent"),
        "source_lines": "upstream-eval/harness.py:118-132（字段口径）+ :109-117（工具去重）；臂级回退见 m6-carrier-metrics.yaml",
        "missing_policy": "缺失即 null（宁缺勿造，不补 0）；臂级回退也不得填 0",
    }
    return out


# ── G6 反例校验的「适用性」判定（docs/06 §1.4 步骤 4 / §5 G6）────────────────
#
# G6 的语义**只**适用于「标准答案 vs 错误答案」这一对被明确指定的组：
# 若把「作废」口径套到任意组对（例如 oracle vs abstain、oracle vs 自然语言组），
# 就会把「两组恰好同分」误报成「用例区分度为零 ⇒ 用例应作废」——这是伪结论。
# 因此只有一组命中「标准答案」别名集、另一组命中「错误答案」别名集时，
# 才启用 G6 的通过/作废结论；其余组对只给信息读数。

G6_ORACLE_ALIASES = ("oracle", "std", "standard", "gold", "golden", "truth",
                     "标准答案", "真值")
G6_WRONG_ALIASES = ("wrong", "err", "error", "bad", "negative", "negctl",
                    "错误答案", "错误")
G6_GATE_LABEL = "区分度为零 ⇒ 相关用例应作废（docs/06 §1.4 步骤 4 / §5 G6）"
G6_OK_LABEL = "无零区分度用例 ⇒ 本组对通过 G6 反例校验"
G6_NA_LABEL = "非 G6 适用组对（仅信息，不作区分度判定）"


def g6_designated(name_a, name_b) -> bool:
    """判定 (name_a, name_b) 是否为 G6 意义下的「标准答案 vs 错误答案」组对。"""
    la, lb = (name_a or "").strip().lower(), (name_b or "").strip().lower()
    return bool((la in G6_ORACLE_ALIASES and lb in G6_WRONG_ALIASES) or
                (lb in G6_ORACLE_ALIASES and la in G6_WRONG_ALIASES))
