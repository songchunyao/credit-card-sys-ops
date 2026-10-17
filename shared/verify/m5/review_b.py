#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M5 复核角色 **B**：独立实现的根因判定（只看工作集事实与契约判定子句）。

为什么要单独实现、不复用 ``m5_inject_build.confirmable()``
--------------------------------------------------------
``docs/06:112-115`` 的双人复核要求「**A 只拿注入清单写 expect_*；B 只拿 query + CCF 独立给根因**」。
复核的价值来自**输入面不同 + 实现不同**：若 B 复用 A 侧那一个函数，等于自证（C5/C6 那类
「自证循环」正是本项目 M5 设计要消除的东西）。

本模块的实现纪律
----------------
* **不看** ``injected`` / ``expect_*`` / ``injections`` —— 只吃 ``ws``（工作集）与契约里的
  ``fault_modes[].detect`` + ``thresholds``；
* **不 import** ``m5_inject_build`` 的任何求值函数（只 import 标准库）；
* 语义必须与契约一致（不是与 A 的代码一致）：``all_of`` 全 TRUE、``any_of`` 至少一个 TRUE、
  ``none_of`` 不得为 TRUE；三类态 TRUE/FALSE/UNK 下 UNK **不算**可确认（宁缺勿造）。

★ 局限（必须随结果披露）：本角色是**机器角色**，不能替代人类对「题目本身是否可判」的判断。
"""

from __future__ import annotations

import hashlib
import io
import os

TRUE, FALSE, UNK = "true", "false", "unk"


def _cmp(op, a, b):
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    raise ValueError("review_b：未知比较运算符 %r" % (op,))


def _truth_of_predicate(obs, pred, item):
    """返回 (TRUE|FALSE|UNK)。predicate 分支。"""
    if pred in ("increased", "decreased"):
        d = obs.get("delta")
        if d is None:
            return UNK
        try:
            f = float(d)
        except (TypeError, ValueError):
            return UNK
        return TRUE if ((f > 0) if pred == "increased" else (f < 0)) else FALSE
    if pred == "non_zero":
        if "value" not in obs:
            return UNK
        v = obs["value"]
        try:
            return TRUE if float(v) != 0.0 else FALSE
        except (TypeError, ValueError):
            return TRUE if v else FALSE
    if pred == "is_true":
        v = obs.get("value")
        return TRUE if (v is True or str(v).lower() == "true") else FALSE
    if pred == "is_false":
        v = obs.get("value")
        return TRUE if (v is False or str(v).lower() == "false") else FALSE
    if pred == "in_enum":
        if "valueText" not in obs:
            return UNK
        return TRUE if obs.get("valueText") in (item.get("values") or []) else FALSE
    raise ValueError("review_b：未知 predicate %r" % (pred,))


def _bound_of(operand, attrs, thresholds):
    """解析 operand ⇒ (bound, normalize_by, ok)。ok=False 表示取不到（⇒ UNK）。"""
    if isinstance(operand, str) and operand.startswith("threshold:"):
        name, _, tier = operand.split(":", 1)[1].partition("#")
        t = thresholds.get(name)
        if not t:
            raise ValueError("review_b：未知阈值名 %r" % (name,))
        b = t.get(tier or "warning")
        nb = t.get("normalize_by")          # ★ 裁定 ②(A)：归一化写在阈值上，两个实现都必须遵守
        return (b, nb, False) if b is None else (b, nb, True)
    if isinstance(operand, str) and operand.startswith("entity_property:"):
        _type, _, prop = operand.split(":", 1)[1].partition(".")
        if not prop or prop not in (attrs or {}):
            return None, None, False
        return attrs[prop], None, True
    return operand, None, True


def _item_state(item, ws_obs, objs, thresholds):
    """单条目 ⇒ (TRUE|FALSE|UNK, [证据串])。"""
    metric = item.get("metric")
    cands = [o for o in ws_obs if o.get("metricCode") == metric]
    if not cands:
        return UNK, []
    saw_unk = False
    hits = []
    for o in cands:
        owner = objs.get(o.get("objectId")) or {}
        if "predicate" in item:
            st = _truth_of_predicate(o, item["predicate"], item)
            if st == UNK:
                saw_unk = True
                continue
            if st == TRUE:
                hits.append("%s@%s %s" % (metric, o.get("objectId"), item["predicate"]))
            continue
        bound, normalize_by, ok = _bound_of(item.get("operand"), owner.get("attrs"), thresholds)
        if not ok:
            saw_unk = True
            continue
        val = o.get("value", o.get("valueText"))
        if normalize_by:
            den = (owner.get("attrs") or {}).get(normalize_by)
            if not isinstance(den, (int, float)) or isinstance(den, bool) or den == 0:
                saw_unk = True
                continue
            try:
                val = float(val) / float(den)
            except (TypeError, ValueError):
                saw_unk = True
                continue
        try:
            hit = _cmp(item.get("op"), val, bound)
        except TypeError:
            saw_unk = True
            continue
        if hit:
            hits.append("%s@%s %s %s %s" % (metric, o.get("objectId"), val, item.get("op"), bound))
    if hits:
        return TRUE, hits
    return (UNK if saw_unk else FALSE), []


def _group_state(items, ws_obs, objs, thresholds):
    """all_of 语义：全 TRUE 才 TRUE；否则 FALSE 优先、UNK 次之。"""
    saw_unk = False
    hits = []
    for it in items:
        st, h = _item_state(it, ws_obs, objs, thresholds)
        if st == FALSE:
            return FALSE, []
        if st == UNK:
            saw_unk = True
        hits.extend(h)
    return (UNK if saw_unk else TRUE), hits


def _any_state(items, ws_obs, objs, thresholds):
    hits = []
    for it in items:
        st, h = _item_state(it, ws_obs, objs, thresholds)
        if st == TRUE:
            hits.extend(h)
    return (TRUE if hits else FALSE), hits


def mode_verdict(mode, ws, thresholds):
    """单个模式在本工作集上是否**可确认** ⇒ (bool, [证据串])。"""
    objs = {o["id"]: o for o in (ws.get("objects") or [])}
    ws_obs = list(ws.get("observations") or [])
    det = mode.get("detect") or {}
    ev = []
    if det.get("all_of"):
        st, h = _group_state(det["all_of"], ws_obs, objs, thresholds)
        ev.extend(h)
        if st != TRUE:
            return False, []
    if det.get("any_of"):
        st, h = _any_state(det["any_of"], ws_obs, objs, thresholds)
        ev.extend(h)
        if st != TRUE:
            return False, []
    if det.get("none_of"):
        st, _h = _group_state(det["none_of"], ws_obs, objs, thresholds)
        if st == TRUE:
            return False, []
    if not (det.get("all_of") or det.get("any_of") or det.get("none_of")):
        return False, []
    return True, ev


def review(ws, modes, thresholds):
    """返回 {mode_code: [证据串]}（只看工作集与契约）。"""
    out = {}
    for m in modes:
        ok, ev = mode_verdict(m, ws, thresholds)
        if ok:
            out[m["code"]] = ev
    return out


def impl_sha256():
    """本实现的源码指纹（记进 gold_audit，便于第三方复核是哪一个版本跑的）。"""
    with io.open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()
