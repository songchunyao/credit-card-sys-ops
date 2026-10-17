#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M5 S4a 注入构造器（离线、零网络、不改基底）。

职责
----
1. 读入**已冻结**的真实基底（默认 ``tmp/ccf_live5.json``），只读；
2. 按内建用例表构造每例的注入差分（三类：事实值 / 对象存在性 / 关系存在性），
   ``value_before`` **由基底实值自动回填**（不符即 fail-closed，绝不猜）；
3. 用 ``live_facts.build_working_set`` 重建工作集，取 ``working_set_sha256``；
4. 用 ``fault-modes.yaml`` 的 14 条 ``detect`` 子句**独立复算**该工作集里**可确认**的故障模式集合，
   并要求它**逐字等于**用例声明的注入模式集合（不多不少）—— 真值由此**定义性派生**，不是手写；
5. 落盘两个正本：
   * ``shared/model/m5-input-facts.yaml``  基底引用 + 每例注入差分 + 三哈希
   * ``shared/model/m5-cases.recorded.yaml`` 50 条用例（18 字段 + 溯源锚点）
  * ``shared/model/m5-gold-audit.yaml``      gold_audit 台账（A/B 两方裁决 + 工作集哈希 + 人工位待填）

铁律
----
* 幂等：同输入两次运行 ⇒ 逐字节相同（``--selftest`` 机检）；
* 绝不写基底文件、绝不连网、绝不向外部落任何存储（只写本文件头部列出的两个正本）；
* 缺 ``value_before`` 匹配 ⇒ 抛错中止（fail-closed）；
* 单例可 dump 重建（``--dump-case <id>``）。

用法
----
    python shared/collect/m5_inject_build.py --build
    python shared/collect/m5_inject_build.py --check
    python shared/collect/m5_inject_build.py --selftest
    python shared/collect/m5_inject_build.py --dump-case A-01
    python shared/collect/m5_inject_build.py --map-report
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import live_facts as LF  # noqa: E402

FACTS_REL = "shared/model/m5-input-facts.yaml"
CASES_REL = "shared/model/m5-cases.recorded.yaml"
GOLD_REL = "shared/model/m5-gold-audit.yaml"
CONTRACT_REL = "shared/model/m5-cases.yaml"
BASE_REL = "tmp/ccf_live5.json"
FAULT_MODES_REL = "shared/model/fault-modes.yaml"
EDGES_REL = "shared/model/causal-edges.yaml"
SNAPSHOT_ID = "ccf-20260928T155516Z-937b79"
BASE_CANON = "fb287c2ebeacf321254c5b9525b19d7afdbee8da483d7987208e0396d4d7ad1b"
CONSTRUCTED_AT = "2026-10-08T00:00:00Z"
INJ_PREFIX = "INJ-20261008"
NONE = "NONE"

# ★ 复核角色 B 的**独立实现**（`shared/verify/m5/review_b.py`；见该模块头注：不复用本文件的求值函数）
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(os.path.dirname(_HERE), "verify", "m5"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import review_b  # noqa: E402

FAMILIES = {"C1": 14, "C2": 14, "C3": 6, "C4": 6, "C5": 6, "C6": 4}
CLS_PREFIX = {"C1": "A", "C2": "B", "C3": "D", "C4": "N", "C5": "Z", "C6": "T"}
FAMILY_BY_PREFIX = {v: k for k, v in CLS_PREFIX.items()}

# 未覆盖缺口（诚实登记，写进正本 meta.uncovered_gaps；一条都不许悄悄省略）
UNCOVERED_GAPS = [
    "14 条故障模式里只有 4 条能在本基底上机判确认（FM-APP-ENDPOINT-SLOW / "
    "FM-APP-HEAP-PRESSURE / FM-HOST-MEM-PRESSURE / FM-DB-INSTANCE-DOWN）；"
    "其余 10 条（FM-APP-POOL-EXHAUSTED、FM-APP-DS-CONNECT-FAIL、FM-APP-INSTANCE-RESTART、"
    "FM-CTR-OOM-KILLED、FM-CTR-CPU-THROTTLED、FM-CTR-NET-UNREACHABLE、"
    "FM-HOST-CPU-CONTENTION、FM-DB-SLOW-QUERY、FM-DB-SESSION-POOL-FULL、FM-DB-LOCK-WAIT）"
    "在本批里只作为症状叙述/期望路径节点出现，不是可机判真值。",
    "上述 10 条的不可构造原因分三类：①阈值两档均为 null（acquire_wait_ms_light、load1_per_core_light —— 后者已由用户裁定 ②(A) 改为按 Host.cpuCores 归一化，但因基线里带 load1 的主机缺 cpuCores 属性 ⇒ 仍不可标、仍 unjudgeable）"
    "⇒ unjudgeable；②increased/decreased 判据缺 delta（connect_fail_total、oom_kill_total、"
    "cpu_throttled_periods、restart_total 的 delta 缺失或恒 0）；③基底根本没有该观测且 "
    "set_fact 不能新增 metricCode（db.query.mean_exec_ms、db.pool.conn_rejected_total、"
    "db.session.wait_event）。",
    "跨层因果边（shared/model/causal-edges.yaml 18 条）里两端都能在本基底构造的只有 "
    "E17 FM-APP-HEAP-PRESSURE→FM-HOST-MEM-PRESSURE（F4，App→Runtime）⇒ 除 B-01/B-02 外，"
    "跨层用例的 expect_path 只能退化为单节点（mapping.path_basis=single-node），"
    "本批未能考「真跨层路径」。",
    "该基底无时序信号：191 条观测 observedAt 逐字相同，仅 3 条 Container 观测带 delta 且全为 0 "
    "⇒ F7 时间对齐族（T-*）只能用「单快照内状态共现」近似，未覆盖真实时间窗共现。",
    "host:node-01 在基底里没有任何观测 ⇒ 该宿主上的任何 Runtime 现象不可机判；"
    "被 DEPLOYED_ON 指向的容器因此也只能靠自身观测（container.*）定性。",
    "注入面（D44）只能改已存在观测的值/状态、增删对象与关系，不能新增 metricCode 或观测 "
    "⇒ 「新指标出现」类故障在本框架下不可构造。",
    "C5（Z-01..Z-06）为 NOOP 空注入，其「无故障」真值是定义性派生（不注入即无现象），"
    "不是从一次真实故障采集得到的标签。",
    "docs/06 §1.3 的族×难度联合构成自相矛盾（OQ-M5-01）仍未决 ⇒ 本批只保证族分布与难度分布"
    "两个边际（14/14/6/6/6/4 与 16/20/14），不断言族×难度联合分布。",
]
DIFFICULTY_TALLY = {"easy": 16, "medium": 20, "hard": 14}
TYPE_BY_CLS = {"C1": "单层定位", "C2": "跨层定界", "C3": "同症状区分",
               "C4": "否定性证据", "C5": "正常态", "C6": "时间对齐"}
SCOPE3 = ["App", "Runtime", "Data"]


class BuildError(Exception):
    """构造期 fail-closed。"""


# --------------------------------------------------------------- YAML 读写

def _yaml():
    import yaml
    return yaml


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return _yaml().safe_load(fh)


def dump_yaml_bytes(doc):
    return _yaml().safe_dump(doc, allow_unicode=True, sort_keys=True,
                             default_flow_style=False).encode("utf-8")


def fingerprint_bytes(data):
    return {"bytes": len(data), "lf": data.count(b"\n"),
            "sha256": hashlib.sha256(data).hexdigest()}


# --------------------------------------------------------------- 词表装载

def load_fault_modes():
    doc = load_yaml(os.path.join(ROOT, FAULT_MODES_REL))
    modes = list(doc["fault_modes"])
    thresholds = {t["name"]: t for t in (doc.get("thresholds") or [])}
    return modes, thresholds


def load_edges():
    doc = load_yaml(os.path.join(ROOT, EDGES_REL))
    return list(doc.get("edges") or [])


def causal_path(edges, src, dst, max_hops=4):
    """因果边模型上的有向最短路；无边则 None（不得编造）。"""
    if src == dst:
        return [src]
    adj = {}
    for e in edges:
        if str(e.get("note") or "").startswith("对称边"):
            continue
        adj.setdefault(e["from"], []).append(e["to"])
    frontier = [[src]]
    for _ in range(max_hops):
        nxt = []
        for path in frontier:
            for n in adj.get(path[-1], []):
                if n in path:
                    continue
                if n == dst:
                    return path + [n]
                nxt.append(path + [n])
        frontier = nxt
        if not frontier:
            break
    return None


# --------------------------------------------------------------- 基底索引

class Base(object):
    def __init__(self, path):
        self.source = LF.load_base(path)
        if self.source["kind"] not in ("ccf", "m5-input-facts"):
            raise BuildError("基底类型 %r 不接受" % (self.source["kind"],))
        self.doc = self.source["ccf"]
        if self.source.get("canonical_sha256") != BASE_CANON:
            raise BuildError("基底 canonical_sha256=%s 与期望 %s 不符（fail-closed）"
                             % (self.source.get("canonical_sha256"), BASE_CANON))
        if self.source.get("snapshot_id") != SNAPSHOT_ID:
            raise BuildError("基底 snapshot_id=%s 与期望 %s 不符（fail-closed）"
                             % (self.source.get("snapshot_id"), SNAPSHOT_ID))
        self.fp = self.source.get("fingerprint") or {}
        self.objs = {o["id"]: o for o in self.doc.get("objects") or []}
        self.rels = list(self.doc.get("relations") or [])
        self.by_type = {}
        for o in self.objs.values():
            self.by_type.setdefault(o["type"], []).append(o)
        self.obs_by_obj = {}
        self.obs_by_metric = {}
        for o in self.doc.get("observations") or []:
            self.obs_by_obj.setdefault(o["objectId"], []).append(o)
            self.obs_by_metric.setdefault(o["metricCode"], []).append(o)

    def sym(self, name):
        """符号 → 基底 CCF id（必须唯一命中，否则 fail-closed）。"""
        name = str(name).lstrip("$")

        def one(cands, what):
            if len(cands) != 1:
                raise BuildError("符号 $%s 命中 %d 个（%s）：%s"
                                 % (name, len(cands), what, [c.get("id") for c in cands][:6]))
            return cands[0]

        if name in ("EP_PAY", "EP_SLOW"):
            want = "/pay" if name == "EP_PAY" else "/slow"
            cands = [o for o in self.by_type.get("AppEndpoint", [])
                     if (o.get("key") or {}).get("path") == want]
            return one(cands, "AppEndpoint path=%s" % want)["id"]
        if name == "APP":
            return one(self.by_type.get("CreditCardApp", []), "CreditCardApp")["id"]
        if name == "APPINST":
            return one(self.by_type.get("AppInstance", []), "AppInstance")["id"]
        if name == "CTR":
            return one(self.by_type.get("Container", []), "Container")["id"]
        if name == "JDB":
            return one(self.by_type.get("JdbcDataSource", []), "JdbcDataSource")["id"]
        if name == "CL":
            return one(self.by_type.get("GaussDBCluster", []), "GaussDBCluster")["id"]
        if name in ("H1", "H2", "H3"):
            oid = {"H1": "host:host-dn_6001", "H2": "host:host-dn_6002",
                   "H3": "host:node-01"}[name]
            if oid not in self.objs:
                raise BuildError("符号 $%s 指向的 %s 不在基底" % (name, oid))
            return oid
        if name in ("DN1", "DN2"):
            oid = "dbinst:dn_6001" if name == "DN1" else "dbinst:dn_6002"
            if oid not in self.objs:
                raise BuildError("符号 $%s 指向的 %s 不在基底" % (name, oid))
            return oid
        if name == "POOL1":
            oid = "dbpool:dn_6001-pool"
            if oid not in self.objs:
                raise BuildError("符号 $POOL1 指向的 %s 不在基底" % oid)
            return oid
        if name == "TBL_SNAP":
            cands = [o for o in self.by_type.get("Table", [])
                     if o["id"].startswith("table:tbl-snapshot.")]
            return one(cands[:1] or [], "Table tbl-snapshot.*")["id"]
        if name == "REL_EXPOSES_SLOW":
            cands = [r for r in self.rels if r.get("type") == "EXPOSES"
                     and r.get("to") == self.sym("EP_SLOW")]
            return one(cands, "EXPOSES→/slow")["id"]
        if name == "REL_DEPLOYED_ON":
            cands = [r for r in self.rels if r.get("type") == "DEPLOYED_ON"]
            return one(cands, "DEPLOYED_ON")["id"]
        if name == "REL_HOSTS_DN1":
            cands = [r for r in self.rels if r.get("type") == "HOSTS"
                     and r.get("to") == self.sym("DN1")]
            return one(cands, "HOSTS→dbinst:dn_6001")["id"]
        raise BuildError("未知符号 $%s" % name)

    def obs_value(self, obj_id, metric, field="value"):
        cands = [o for o in self.obs_by_obj.get(obj_id, []) if o.get("metricCode") == metric]
        if len(cands) != 1:
            raise BuildError("寻址观测 %s / %s 命中 %d 条（必须恰 1 条）"
                             % (obj_id, metric, len(cands)))
        o = cands[0]
        if field not in o:
            raise BuildError("观测 %s 没有字段 %r（自带键=%s）" % (o["id"], field, sorted(o.keys())))
        return o, copy.deepcopy(o[field])


# --------------------------------------------------------------- 注入组装

def build_injections(base, plan):
    """把紧凑 plan 展开成 ``live_facts.apply_injections`` 接受的注入清单。

    plan 元素：
      ("fact",  <sym>, <metric>, <after>[, field])
      ("rm_obj", <sym 或字面 id>)
      ("add_obj", <Type>, <id_local>, {key}, {attrs}, <collectedAt>)
      ("rm_rel", <sym 或字面 rel id>)
      ("add_rel", <code>, <from sym>, <to sym>)
    """
    out = []
    for item in plan:
        kind = item[0]
        if kind == "fact":
            sym, metric, after = item[1], item[2], item[3]
            field = item[4] if len(item) > 4 else "value"
            oid = base.sym(sym) if str(sym).startswith("$") else sym
            obs, before = base.obs_value(oid, metric, field)
            entry = {"op": "set_fact", "constructed_at": CONSTRUCTED_AT,
                     "target": {"obj_type": base.objs[oid]["type"], "obj_id": oid, "path": metric},
                     "field": field, "value_before": before, "value_after": after}
            if field == "status":
                entry.pop("value_before"); entry.pop("value_after")
                entry["status_before"] = before
                entry["status_after"] = after
            out.append(entry)
        elif kind == "rm_obj":
            oid = base.sym(item[1]) if str(item[1]).startswith("$") else item[1]
            out.append({"op": "remove_object", "constructed_at": CONSTRUCTED_AT,
                        "target": {"obj_type": base.objs[oid]["type"], "obj_id": oid}})
        elif kind == "add_obj":
            _k, type_code, id_local, key, attrs, stamp = item
            out.append({"op": "add_object", "constructed_at": stamp,
                        "type": type_code, "id_local": id_local, "key": key,
                        "attrs": attrs})
        elif kind == "rm_rel":
            rid = base.sym(item[1]) if str(item[1]).startswith("$") else item[1]
            out.append({"op": "remove_relation", "constructed_at": CONSTRUCTED_AT,
                        "relation_id": rid})
        elif kind == "add_rel":
            _k, code, frm, to = item
            out.append({"op": "add_relation", "constructed_at": CONSTRUCTED_AT,
                        "relation": code,
                        "from": base.sym(frm) if str(frm).startswith("$") else frm,
                        "to": base.sym(to) if str(to).startswith("$") else to})
        else:
            raise BuildError("未知注入类型 %r" % (kind,))
    return out


def canonical_injection(inj):
    """注入条目的落盘形态（只保留重放必需键，逐字节稳定）。"""
    keep = ("op", "target", "field", "value_before", "value_after",
            "status_before", "status_after")
    out = {k: inj[k] for k in keep if k in inj}
    if inj["op"] == "add_object":
        out = {"op": "add_object", "constructed_at": inj["constructed_at"],
               "type": inj["type"], "id_local": inj["id_local"],
               "key": inj["key"], "attrs": inj["attrs"]}
    elif inj["op"] == "remove_object":
        out = {"op": "remove_object", "target": inj["target"]}
    elif inj["op"] == "remove_relation":
        out = {"op": "remove_relation", "relation_id": inj["relation_id"]}
    elif inj["op"] == "add_relation":
        out = {"op": "add_relation", "constructed_at": inj["constructed_at"],
               "relation": inj["relation"], "from": inj["from"], "to": inj["to"]}
    return out


def replay_injection(inj):
    """落盘形态 → 传给 apply_injections 的形态（逐字可重建）。"""
    return copy.deepcopy(inj)


# --------------------------------------------------------------- 真值复算

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
    raise BuildError("未知比较运算符 %r" % (op,))


TRUE, FALSE, UNK = "true", "false", "unk"


def _item_eval(item, ws, thresholds, objs):
    """单条目 ⇒ (TRUE|FALSE|UNK, [(obs_id, obj_id, metric, why)])。"""
    metric = item["metric"]
    cands = [o for o in ws["observations"] if o.get("metricCode") == metric]
    ev = []
    if not cands:
        return UNK, ev
    saw_unk = False
    for o in cands:
        owner = objs.get(o.get("objectId")) or {}
        _nb = None          # ★ 归一化属性名（仅 threshold: 分支会赋值；其他条目保持 None）
        if "predicate" in item:
            pred = item["predicate"]
            if pred in ("increased", "decreased"):
                if "delta" not in o or o.get("delta") is None:
                    saw_unk = True
                    continue
                try:
                    val = float(o["delta"])
                except (TypeError, ValueError):
                    saw_unk = True
                    continue
                hit = val > 0 if pred == "increased" else val < 0
            elif pred == "non_zero":
                if "value" not in o:
                    saw_unk = True
                    continue
                try:
                    hit = float(o["value"]) != 0.0
                except (TypeError, ValueError):
                    hit = bool(o["value"])
            elif pred == "is_true":
                hit = (o.get("value") is True or str(o.get("value")).lower() == "true")
            elif pred == "is_false":
                hit = (o.get("value") is False or str(o.get("value")).lower() == "false")
            elif pred == "in_enum":
                vals = item.get("values") or []
                if "valueText" not in o:
                    saw_unk = True
                    continue
                hit = o.get("valueText") in vals
            else:
                raise BuildError("未知 predicate %r" % (pred,))
            if hit:
                ev.append((o["id"], o.get("objectId"), metric, pred))
            continue
        # op / operand
        operand = item.get("operand")
        if isinstance(operand, str) and operand.startswith("threshold:"):
            tail = operand.split(":", 1)[1]
            tname, _, tier = tail.partition("#")
            t = thresholds.get(tname)
            if not t:
                raise BuildError("未知阈值名 %r" % (tname,))
            bound = t.get(tier or "warning")
            if bound is None:
                saw_unk = True
                continue
            # ★ 用户裁定 ②(A)：阈值可声明 normalize_by（对象属性名）⇒ 用 `观测值 / 属性值` 比较。
            #   属性缺失 ⇒ **UNK**（宁缺勿造，不得当 false、更不得当 0）。
            _nb = t.get("normalize_by")
            if _nb:
                _den = (owner.get("attrs") or {}).get(_nb)
                if not isinstance(_den, (int, float)) or isinstance(_den, bool) or _den == 0:
                    saw_unk = True
                    continue
        elif isinstance(operand, str) and operand.startswith("entity_property:"):
            tail = operand.split(":", 1)[1]
            _type, _, prop = tail.partition(".")
            if not prop or prop not in (owner.get("attrs") or {}):
                saw_unk = True
                continue
            bound = owner["attrs"][prop]
        else:
            bound = operand
        val = o.get("value", o.get("valueText"))
        if _nb:
            try:
                val = float(val) / float((owner.get("attrs") or {})[_nb])
            except (TypeError, ValueError, KeyError):
                saw_unk = True
                continue
        try:
            hit = _cmp(item["op"], val, bound)
        except TypeError:
            saw_unk = True
            continue
        if hit:
            ev.append((o["id"], o.get("objectId"), metric,
                       "%s %s %s" % (val, item["op"], bound)))
    if ev:
        return TRUE, ev
    return (UNK if saw_unk else FALSE), ev


def _group_eval(items, ws, thresholds, objs):
    states = []
    ev = []
    for it in items:
        st, e = _item_eval(it, ws, thresholds, objs)
        states.append(st)
        ev.extend(e)
    if any(s == FALSE for s in states):
        return FALSE, ev
    if any(s == UNK for s in states):
        return UNK, ev
    return TRUE, ev


def confirmable(ws, modes, thresholds):
    """复算工作集里**可确认**的故障模式集合 → {code: evidence}。"""
    objs = {o["id"]: o for o in ws["objects"]}
    out = {}
    for m in modes:
        det = m.get("detect") or {}
        ok = True
        ev = []
        for group in ("all_of", "any_of"):
            items = det.get(group)
            if not items:
                continue
            if group == "all_of":
                _s, e = _group_eval(items, ws, thresholds, objs)
                ev.extend(e)
                if _s != TRUE:
                    ok = False
                    break
            else:
                states = []
                for it in items:
                    st, e = _item_eval(it, ws, thresholds, objs)
                    states.append(st)
                    ev.extend(e)
                if TRUE not in states:
                    ok = False
                    break
        if ok and "none_of" in det:
            _s, _e = _group_eval(det["none_of"], ws, thresholds, objs)
            if _s == TRUE:
                ok = False
        if ok:
            out[m["code"]] = ev
    return out


# --------------------------------------------------------------- 用例表

def case_table():
    """50 条用例的紧凑定义（真值不在表里——真值由 ``confirmable`` 复算后校验）。

    每行 = (case_id, cls, chain, scope, difficulty, root, injected, symptom, plan, query, disc)
    """
    T = []
    A = T.append
    # ── C1 单层定位（14，easy）───────────────────────────────────────────
    A(("A-01", "C1", "F1", ["App"], "easy", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 4180.0)],
       "支付网关 14:20 起 /pay 的 P99 从个位数毫秒抬到 4000ms 以上，同期 QPS 与错误率没有明显变化。"
       "请判断根因在哪一层、落在哪个对象上，并给出你据以判断的指标依据。", None))
    A(("A-02", "C1", "F1", ["App"], "easy", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 1250.0)],
       "对账接口 /slow 的 P99 长时间徘徊在 1200ms 上下，吞吐量维持平稳。"
       "请给出根因所在层与对象，并说明为什么可以排除其他两层。", None))
    A(("A-03", "C1", "F2", ["App"], "easy", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 2600.0)],
       "风控调用 /pay 的尾部延迟连续三个采集窗口超过 2 秒，数据库与会话池读数没有异常波动。"
       "请定位根因，并给出你的分层判断。", None))
    A(("A-04", "C1", "F3", ["App"], "easy", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 880.0)],
       "批量代扣走的 /slow 接口 P99 升到 880ms，宿主与容器的 CPU、内存读数都在常规区间。"
       "请定位根因层与对象。", None))
    A(("A-05", "C1", "F4", ["App"], "easy", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.8841)],
       "支付应用实例的老年代占用率持续走高，伴随单次 GC 停顿明显拉长，但容器与宿主机读数正常。"
       "请判断根因所在层与对象。", None))
    A(("A-06", "C1", "F4", ["App"], "easy", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.9312)],
       "应用实例出现频繁 Full GC，堆占用率一路逼近上限，容器没有被重启过。"
       "请定位根因层与对象，并给出依据指标。", None))
    A(("A-07", "C1", "F4", ["App"], "easy", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.7994)],
       "线上一台应用实例的堆占用率越过警戒线并持续不退，GC 停顿同步变长。"
       "请指出根因层、对象与判据。", None))
    A(("A-08", "C1", "F4", ["Runtime"], "easy", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H1", "host.mem_available_ratio", 0.0212)],
       "承载主库的宿主可用内存比例掉到 2% 出头，应用侧响应时间开始抖动。"
       "请定位根因所在层与对象，并说明证据。", None))
    A(("A-09", "C1", "F4", ["Runtime"], "easy", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H2", "host.mem_available_ratio", 0.0288)],
       "备库所在宿主的可用内存比例跌破 3%，但数据库实例本身仍在正常提供读写。"
       "请判断这属于哪一层的故障，根因对象是谁。", None))
    A(("A-10", "C1", "F4", ["Runtime"], "easy", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H1", "host.mem_available_ratio", 0.0402)],
       "运维巡检发现一台宿主机的可用内存比例长期低于 5% 且无回收迹象，同一宿主上的容器尚未被驱逐。"
       "请定位根因层与对象。", None))
    A(("A-11", "C1", "F4", ["Runtime"], "easy", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H2", "host.mem_available_ratio", 0.0188)],
       "备库宿主的内存水位已进入严重区间，页面缓存回收频繁，数据库与应用的日志均无异常条目。"
       "请给出根因层与对象。", None))
    A(("A-12", "C1", "F7", ["Data"], "easy", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False)],
       "主库实例的存活标记翻转为不可用，应用侧开始出现取连接等待；集群统计尚未更新。"
       "请定位根因层与对象。", None))
    A(("A-13", "C1", "F7", ["Data"], "easy", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN2", "db.instance.up", False),
        ("rm_rel", "$REL_EXPOSES_SLOW")],
       "备库实例的存活标记翻转为不可用，同时发现一条对外暴露关系从拓扑里消失。"
       "请判断根因在哪一层、对象是谁，并说明你的证据。", None))
    A(("A-14", "C1", "F7", ["Data"], "easy", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1)],
       "集群侧看到可写节点数从 2 掉到 1、宕机节点数升到 1，应用侧连接成功率同步下滑。"
       "请定位根因层与对象。", None))

    # ── C2 跨层定界（14）────────────────────────────────────────────────
    A(("B-01", "C2", "F4", SCOPE3, "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE", "FM-HOST-MEM-PRESSURE"], "FM-HOST-MEM-PRESSURE",
       [("fact", "$APPINST", "app.heap_used_ratio", 0.9021),
        ("fact", "$H1", "host.mem_available_ratio", 0.0303)],
       "应用实例堆占用率超过 90%，同一台宿主机的可用内存比例同时掉破 5%。"
       "请给出从首发根因到下游现象的传播路径，并指出首发根因所在层。", None))
    A(("B-02", "C2", "F4", SCOPE3, "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE", "FM-HOST-MEM-PRESSURE"], "FM-HOST-MEM-PRESSURE",
       [("fact", "$APPINST", "app.heap_used_ratio", 0.8453),
        ("fact", "$H2", "host.mem_available_ratio", 0.0341)],
       "一次发布后应用堆占用率稳步抬到 85% 以上，随后备库宿主的内存可用比例跌破 4%。"
       "请判断首发根因是哪一层，并给出跨层传播链路。", None))
    A(("B-03", "C2", "F2", SCOPE3, "medium", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 3100.0)],
       "支付接口 P99 抬到 3 秒量级，业务方怀疑是数据库慢查询，但数据库实例与连接池读数均无异常。"
       "请定界根因所在层并给出依据。", None))
    A(("B-04", "C2", "F3", SCOPE3, "medium", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 4400.0)],
       "对账接口 P99 达到 4.4 秒，值班同学第一反应是宿主 CPU 争抢，但宿主与容器的 CPU 读数都在低位。"
       "请定界根因层并说明排除依据。", None))
    A(("B-05", "C2", "F4", SCOPE3, "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.8766)],
       "应用堆占用率持续高于 87%，GC 停顿同步拉长；宿主内存与数据库实例都没有异常。"
       "请给出根因层与传播影响范围。", None))
    A(("B-06", "C2", "F5", SCOPE3, "medium", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H1", "host.mem_available_ratio", 0.0255)],
       "主库宿主可用内存比例跌破 3%，应用侧偶发超时；应用堆内存、数据库实例读数均正常。"
       "请定界根因层，并说明你是如何排除应用层的。", None))
    A(("B-07", "C2", "F6", SCOPE3, "medium", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False)],
       "主库实例存活标记翻转为不可用，应用出现取连接失败；宿主与应用内存读数正常。"
       "请定界根因层与对象。", None))
    A(("B-08", "C2", "F7", SCOPE3, "medium", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN2", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1)],
       "备库实例判定为不可用，集群节点计数同步变化；应用侧尚未出现大面积报错。"
       "请定界根因层，并说明影响面为什么暂时有限。", None))
    A(("B-09", "C2", "F1", SCOPE3, "medium", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 2900.0)],
       "支付接口 P99 从 180ms 抬到 2900ms，连接池并发与数据库会话计数都没有明显抬升。"
       "请定界根因层并给出证据链。", None))
    A(("B-10", "C2", "F2", SCOPE3, "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.9552)],
       "应用堆占用率冲到 95%，伴有长 GC；有人提出是数据库慢查询，但数据层读数无异常。"
       "请定界根因层与对象，并说明排除数据层的依据。", None))
    A(("B-11", "C2", "F3", SCOPE3, "hard", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H2", "host.mem_available_ratio", 0.0199),
        ("rm_obj", "$TBL_SNAP")],
       "备库宿主可用内存比例跌到 2% 以下，同时巡检发现一张快照表对象从事实集里消失。"
       "请定界根因层与对象，并说明对象缺失是否改变了根因判定。", None))
    A(("B-12", "C2", "F4", SCOPE3, "hard", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 1700.0),
        ("rm_rel", "$REL_DEPLOYED_ON")],
       "对账接口 P99 抬到 1.7 秒，同时拓扑里容器到宿主的部署关系整体消失。"
       "请定界根因层，并说明缺失关系对你的判断有无影响。", None))
    A(("B-13", "C2", "F5", SCOPE3, "hard", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H1", "host.mem_available_ratio", 0.0455),
        ("add_obj", "Host", "m5-host-a",
         {"hostname": "m5-host-a"}, {"hostname": "m5-host-a", "role": "k8s-worker"},
         "2026-09-28T23:55:45.000000")],
       "主库宿主可用内存比例降到 4.5% 附近，巡检同时登记了一台新上线的宿主对象（无读数）。"
       "请定界根因层与对象，并说明新宿主对象是否可信为根因。", None))
    A(("B-14", "C2", "F6", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1)],
       "主库实例变为不可用、集群计数同步下调，应用侧报取连接失败。"
       "请定界根因层并给出跨层影响范围。", None))

    # ── C3 同症状区分（6，medium，3 对）────────────────────────────────
    A(("D-01", "C3", "F4", ["App", "Runtime"], "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 3600.0),
        ("fact", "$APPINST", "app.heap_used_ratio", 0.9183)],
       "支付接口 P99 涨到 3.6 秒。候选解释有两个：应用自身内存压力、宿主内存压力。"
       "请给出你的定论并说明区分依据。", "P1-内存路径: app.heap_used_ratio vs host.mem_available_ratio"))
    A(("D-02", "C3", "F4", ["App", "Runtime"], "medium", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 3600.0),
        ("fact", "$H1", "host.mem_available_ratio", 0.0266)],
       "支付接口 P99 涨到 3.6 秒。候选解释有两个：应用自身内存压力、宿主内存压力。"
       "请给出你的定论并说明区分依据。", "P1-内存路径: app.heap_used_ratio vs host.mem_available_ratio"))
    A(("D-03", "C3", "F4", ["Runtime", "Data"], "medium", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 2750.0),
        ("fact", "$H2", "host.mem_available_ratio", 0.0231)],
       "对账接口 P99 升到 2.75 秒。候选解释有两个：宿主资源不足、数据库实例不可用。"
       "请给出定论与区分依据。", "P2-资源vs可用性: host.mem_available_ratio vs db.instance.up"))
    A(("D-04", "C3", "F7", ["Runtime", "Data"], "medium", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 2750.0),
        ("fact", "$DN1", "db.instance.up", False)],
       "对账接口 P99 升到 2.75 秒。候选解释有两个：宿主资源不足、数据库实例不可用。"
       "请给出定论与区分依据。", "P2-资源vs可用性: host.mem_available_ratio vs db.instance.up"))
    A(("D-05", "C3", "F4", ["App", "Data"], "medium", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 3300.0),
        ("fact", "$APPINST", "app.heap_used_ratio", 0.8874)],
       "批扣接口 P99 抬到 3.3 秒。候选解释有两个：应用内存压力、数据库实例不可用。"
       "请给出定论与区分依据。", "P3-应用内存vs数据库可用性: app.heap_used_ratio vs db.instance.up"))
    A(("D-06", "C3", "F7", ["App", "Data"], "medium", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN", "FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_SLOW", "app.endpoint.p99_ms", 3300.0),
        ("fact", "$DN2", "db.instance.up", False)],
       "批扣接口 P99 抬到 3.3 秒。候选解释有两个：应用内存压力、数据库实例不可用。"
       "请给出定论与区分依据。", "P3-应用内存vs数据库可用性: app.heap_used_ratio vs db.instance.up"))

    # ── C4 否定性证据（6，hard）────────────────────────────────────────
    A(("N-01", "C4", "F4", ["App", "Runtime", "Data"], "hard", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.9054)],
       "应用堆占用率越过 90%。要下结论，你必须先证明另外两条路径不成立。"
       "请给出根因、你据以排除其他路径的**正常读数**，以及分层判断。", None))
    A(("N-02", "C4", "F4", ["App", "Runtime", "Data"], "hard", "FM-APP-HEAP-PRESSURE",
       ["FM-APP-HEAP-PRESSURE"], None,
       [("fact", "$APPINST", "app.heap_used_ratio", 0.8691)],
       "应用堆占用率长期高于 86%，GC 停顿同步拉长。请用排除法收敛到唯一根因，"
       "并明确指出哪些指标处于正常区间。", None))
    A(("N-03", "C4", "F4", SCOPE3, "hard", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H1", "host.mem_available_ratio", 0.0244)],
       "主库宿主可用内存比例跌破 3%。请用正常读数排除应用侧与数据侧，"
       "给出唯一根因、所在层与判据。", None))
    A(("N-04", "C4", "F4", SCOPE3, "hard", "FM-HOST-MEM-PRESSURE",
       ["FM-HOST-MEM-PRESSURE"], None,
       [("fact", "$H2", "host.mem_available_ratio", 0.0177)],
       "备库宿主可用内存比例掉到 2% 以下。请说明你如何用**某指标正常**来定罪，"
       "并给出根因层与对象。", None))
    A(("N-05", "C4", "F7", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1),
        ("rm_rel", "$REL_HOSTS_DN1")],
       "主库实例不可用、集群计数下调，同时一条宿主托管关系从拓扑中消失。"
       "请用排除法给出根因与分层判断，并说明缺失关系是否改变结论。", None))
    A(("N-06", "C4", "F1", SCOPE3, "hard", "FM-APP-ENDPOINT-SLOW",
       ["FM-APP-ENDPOINT-SLOW"], None,
       [("fact", "$EP_PAY", "app.endpoint.p99_ms", 5200.0)],
       "支付接口 P99 飙到 5.2 秒。请用正常读数逐一排除应用内存、宿主资源与数据库三条路径，"
       "给出唯一根因与依据。", None))

    # ── C5 正常态（6）──────────────────────────────────────────────────
    Z_QUERIES = [
        ("Z-01", "easy", "上线前巡检：请判断应用层当前是否存在**可据本项目判据确认**的故障，"
                         "并说明你的依据。注意不要因为单个指标接近警戒线就下结论。"),
        ("Z-02", "easy", "上线前巡检：请判断运行时（容器与宿主）当前是否存在可确认的故障，"
                         "并给出证据与分层判断。"),
        ("Z-03", "medium", "上线前巡检：请判断数据层当前是否存在可确认的故障，"
                           "并说明你如何看待会话侧的长时间事务读数。"),
        ("Z-04", "medium", "巡检发现两个读数偏高：应用 GC 停顿与会话事务时长。"
                           "请判断它们是否构成可确认的故障，并给出你的理由与分层结论。"),
        ("Z-05", "medium", "请检查事实集的拓扑完整性（对象与关系是否齐备），"
                           "并判断是否存在可确认的故障；如认为无不一致请明确说明。"),
        ("Z-06", "medium", "请给出该时间点上系统整体健康的结论：是否存在可确认的故障、"
                           "涉及哪些层、你需要哪些额外证据才能改变结论。"),
    ]
    for cid, diff, q in Z_QUERIES:
        A((cid, "C5", "F4", SCOPE3, diff, NONE, [], None, [], q, None))

    # ── C6 时间对齐（4，hard，F7）──────────────────────────────────────
    A(("T-01", "C6", "F7", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False)],
       "14:20 起主库实例存活标记翻转为不可用，但集群侧的可写节点数仍显示 2。"
       "请判断根因，并说明这个时间窗内两组读数为何不一致、以哪一组为准。", None))
    A(("T-02", "C6", "F7", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN1", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1)],
       "14:20 起主库实例不可用，集群侧可写节点数与宕机节点数在同一窗口内同步变化。"
       "请判断根因，并说明两组读数是否已对齐。", None))
    A(("T-03", "C6", "F7", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("rm_obj", "$DN2"),
        ("fact", "$DN1", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("fact", "$CL", "db.cluster.up_nodes", 1)],
       "14:20 起主库实例不可用、集群计数下调；同时备库实例对象整体从事实集里消失。"
       "请判断根因，并说明对象消失对时间窗对齐判断的影响。", None))
    A(("T-04", "C6", "F7", SCOPE3, "hard", "FM-DB-INSTANCE-DOWN",
       ["FM-DB-INSTANCE-DOWN"], None,
       [("fact", "$DN2", "db.instance.up", False),
        ("fact", "$CL", "db.cluster.down_nodes", 1),
        ("add_obj", "GaussDBInstance", "dn_6003",
         {"instanceId": "dn_6003"}, {"instanceId": "dn_6003", "role": "Standby"},
         "2026-09-28T23:56:00.000000"),
        ("add_rel", "HAS_DB_INSTANCE", "$CL", "dbinst:dn_6003")],
       "14:55 起备库实例不可用，集群宕机节点数升到 1；约 45 秒后拓扑里多出一个实例对象。"
       "请判断根因，并按时间窗说明新出现的对象应如何解读。", None))

    # ── 家族/难度核对 ─────────────────────────────────────────────────
    tally = {}
    diff_tally = {}
    for row in T:
        tally[row[1]] = tally.get(row[1], 0) + 1
        diff_tally[row[4]] = diff_tally.get(row[4], 0) + 1
    if tally != FAMILIES:
        raise BuildError("用例族计数 %r != 契约 %r" % (tally, FAMILIES))
    if diff_tally != DIFFICULTY_TALLY:
        raise BuildError("难度计数 %r != 契约 %r" % (diff_tally, DIFFICULTY_TALLY))
    ids = [r[0] for r in T]
    if len(set(ids)) != len(ids) or len(ids) != 50:
        raise BuildError("用例 id 重复或总数 != 50")
    return T


# --------------------------------------------------------------- 词表：pos/neg/nor

ROOT_LEX = {
    "FM-APP-ENDPOINT-SLOW": {
        "pos": ["FM-APP-ENDPOINT-SLOW", "p99", "SLO", "200\\s*ms",
                "应用层|应用服务|App"],
        "neg": ["慢查询|慢\\s*SQL", "宿主.*内存(不足|紧张|耗尽)", "数据库实例.*(不可用|下线)",
                "连接池.*(耗尽|打满)", "容器.*OOM"],
        "nor": [],
    },
    "FM-APP-HEAP-PRESSURE": {
        "pos": ["FM-APP-HEAP-PRESSURE", "堆内存|堆占用|heap_used_ratio", "gc_pause_ms|GC\\s*停顿",
                "应用层|应用实例|App"],
        "neg": ["慢查询|慢\\s*SQL", "宿主.*内存(不足|紧张|耗尽)", "数据库实例.*(不可用|下线)",
                "连接池.*(耗尽|打满)"],
        "nor": ["宿主.*(内存|资源).*正常", "数据库.*正常|实例.*在位|会话池.*正常",
                "容器.*(无|未).*OOM"],
    },
    "FM-HOST-MEM-PRESSURE": {
        "pos": ["FM-HOST-MEM-PRESSURE", "mem_available_ratio|可用内存比例", "宿主机|运行时|Runtime"],
        "neg": ["应用.*(内存泄漏|堆内存)|堆占用", "慢查询|慢\\s*SQL", "连接池.*(耗尽|打满)",
                "数据库实例.*(不可用|下线)"],
        "nor": ["应用.*(堆|内存).*正常", "数据库.*正常|实例.*在位", "连接池.*正常"],
    },
    "FM-DB-INSTANCE-DOWN": {
        "pos": ["FM-DB-INSTANCE-DOWN", "db\\.instance\\.up|实例.*(不可用|下线|停止)",
                "数据层|数据库层|Data|GaussDB"],
        "neg": ["应用.*(内存泄漏|堆内存)|堆占用", "宿主.*内存(不足|紧张|耗尽)",
                "慢查询|慢\\s*SQL", "容器.*OOM"],
        "nor": ["应用.*(堆|内存).*正常", "宿主.*(内存|资源).*正常", "连接池.*正常"],
    },
}

C5_POS = ["正常|健康|无异常|未发现(可确认的)?故障|不构成.*故障"]
C5_NOR = ["无\\s*(可确认的)?故障", "未发现\\s*(可确认的)?(故障|异常)", "各项?指标(均)?正常",
          "处于?正常(区间|范围)", "不构成.*(故障模式|故障)", "无可确认", "均在(阈值|警戒线)?以内"]
C5_NEG = ["FM-APP-ENDPOINT-SLOW", "FM-APP-HEAP-PRESSURE", "FM-HOST-MEM-PRESSURE",
          "FM-DB-INSTANCE-DOWN", "FM-DB-SLOW-QUERY", "FM-CTR-OOM-KILLED",
          "确认为故障|存在故障|定位到根因"]


def derive_lexicon(root, injected, disc):
    if root == NONE:
        return list(C5_POS), list(C5_NEG), list(C5_NOR)
    base = ROOT_LEX[root]
    pos = [re_escape_code(c) for c in base["pos"]]
    neg = list(base["neg"])
    nor = list(base["nor"])
    return pos, neg, nor


def re_escape_code(text):
    """把词表里出现的故障模式编码转义（其余保持为正则片段）。"""
    for code in ROOT_LEX:
        text = text.replace(code, code.replace("-", "\\-"))
    return text


def detectors_for(root, objs, metrics):
    if root == NONE:
        return ["(正常|健康|无异常|未发现故障|不构成故障)"]
    code = root.replace("-", "\\-")
    out = [code]
    for m in metrics:
        out.append(m.replace(".", "\\."))
    for oid in objs:
        out.append(oid.split(":", 1)[1].replace(".", "\\."))
    seen = []
    for d in out:
        if d not in seen:
            seen.append(d)
    return seen[:4]


# --------------------------------------------------------------- 组装交付物

def build_all(base_path=None, verbose=True):
    base = Base(base_path or os.path.join(ROOT, BASE_REL))
    modes, thresholds = load_fault_modes()
    edges = load_edges()
    mode_by_code = {m["code"]: m for m in modes}
    rows = case_table()

    ledger_cases = []
    cases = []
    mapping = []
    gold = []          # ★ gold_audit 台账（D-项：双人复核的持久化）
    problems = []

    for idx, row in enumerate(rows, start=1):
        (cid, cls, chain, scope, diff, root, injected, symptom, plan, query, disc) = row
        injections = build_injections(base, plan)
        case = {"case_id": cid, "injections": injections}
        ws = LF.build_working_set(base.source, case)
        conf = confirmable(ws, modes, thresholds)
        conf_codes = sorted(conf)
        declared = sorted(injected)
        if conf_codes != declared:
            problems.append("%s：可确认集合 %s != 声明注入集合 %s"
                            % (cid, conf_codes, declared))
        if root != NONE and root not in conf:
            problems.append("%s：声明的根因 %s 不在可确认集合 %s" % (cid, root, conf_codes))
        b_codes = sorted(review_b.review(ws, modes, thresholds).keys())
        if sorted(declared) != b_codes:
            problems.append("%s：A（注入清单）%s 与 B（独立复算 review_b）%s 不一致 ⇒ 该用例作废或修正"
                            % (cid, sorted(declared), b_codes))
        if root == NONE and conf_codes:
            problems.append("%s：正常态用例却复算出可确认故障 %s" % (cid, conf_codes))

        # 证据派生
        ev_obs, ev_objs, ev_metrics = [], [], []
        for code in conf_codes:
            for (obs_id, obj_id, metric, _why) in conf[code]:
                if obs_id not in ev_obs:
                    ev_obs.append(obs_id)
                if obj_id and obj_id not in ev_objs:
                    ev_objs.append(obj_id)
                if metric not in ev_metrics:
                    ev_metrics.append(metric)
        # 对象缺失类注入的“受影响对象”（只登记不参与 expect_objects 派生）
        removed = [i["target"]["obj_id"] for i in injections if i["op"] == "remove_object"]

        # expect_path：真实因果边上的最短路；无边 ⇒ 单节点（不得编造）
        if root == NONE:
            expect_path = []
        elif symptom:
            p = causal_path(edges, root, symptom)
            expect_path = p if p else [root]
        else:
            expect_path = [root]

        # 期望层
        expect_layer = NONE if root == NONE else mode_by_code[root]["layer"]

        pos, neg, nor = derive_lexicon(root, injected, disc)
        det = detectors_for(root, ev_objs, ev_metrics)

        ws_sha = (ws.get("meta") or {}).get("working_set_sha256")
        if not ws_sha:
            raise BuildError("%s：工作集缺 working_set_sha256" % cid)
        inj_id = "%s-%03d" % (INJ_PREFIX, idx)

        ledger_cases.append({
            "case_id": cid,
            "source_injection_id": inj_id,
            "constructed_at": CONSTRUCTED_AT,
            "injections": [canonical_injection(i) for i in injections],
            "working_set_sha256": ws_sha,
        })

        rec = {
            "id": cid,
            "cls": CLS_PREFIX[cls],  # 契约 m5-cases.yaml:137 要求族码字母 A/B/D/N/Z/T
            "chain": chain,
            "scope": list(scope),
            "type": TYPE_BY_CLS[cls],
            "query": query,
            "injected": list(injected),
            "expect_root_cause": root,
            "expect_layer": expect_layer,
            "expect_path": list(expect_path),
            "expect_objects": list(ev_objs),
            "pos": pos,
            "neg": neg,
            "nor": nor,
            "evidence_facts": list(ev_obs),
            "detectors": det,
            "difficulty": diff,
            "discriminator": disc,
            "snapshot_id": SNAPSHOT_ID,
            "source_injection_id": inj_id,
            # ★ 契约 m5-cases.yaml#fill_plan 要求出题人**显式**赋值（不得省略）；正式判决集=非夹具
            "synthetic": False,
            # ★ gold_audit（契约 case_schema.contract_introduced_fields 已定义；键取自其 fields_suggested）：
            #   annotator_a / annotator_b 是**人工位**（docs/06:112-113 的标注员 A/B）⇒ 这里**留空**，
            #   本脚本只填机器证据 machine_check（A=注入算子；B=独立复算的可确认集），不冒充人工复核。
            "gold_audit": {
                # ★ 用户裁定（2026-10-09）：不做真人双人复核，改由**两个机器角色**承担。
                #   诚实标注：kind=machine_role（**不是人名**）；meta.human_review 里登记豁免与局限。
                "annotator_a": {
                    "kind": "machine_role",
                    "id": "role:A@injection-ledger",
                    "sees": "注入清单（cases[].injections）",
                    "method": "注入算子集合（canonical_injection 的 op/target）",
                    "decision": list(declared),
                    "reviewed_at": CONSTRUCTED_AT,
                    "impl": "shared/collect/m5_inject_build.py",
                },
                "annotator_b": {
                    "kind": "machine_role",
                    "id": "role:B@independent-review",
                    "sees": "query + CCF（由基底 + 注入差分重建的工作集，**不看注入算子声明**）",
                    "method": "review_b.mode_verdict()：独立实现对 fault-modes.detect 求值",
                    "decision": b_codes,
                    "reviewed_at": CONSTRUCTED_AT,
                    "impl": "shared/verify/m5/review_b.py",
                    "impl_sha256": review_b.impl_sha256(),
                },
                "root_cause_b": (b_codes[0] if len(b_codes) == 1 else None),
                "agreed": sorted(declared) == b_codes,
                "resolution": (None if sorted(declared) == b_codes
                               else "A/B 不一致 ⇒ 该用例作废或修正（见 problems）"),
                "ts_utc": CONSTRUCTED_AT,
                "machine_check": {
                    "a": {"sees": "注入清单", "verdict": list(declared),
                          "method": "注入算子集合（canonical_injection 的 op/target）"},
                    "b": {"sees": "query + CCF（重建工作集）", "verdict": list(conf_codes),
                          "method": "confirmable()：对工作集逐条求值 fault-modes.detect"},
                    "independent": True,
                    "note": "机器侧不能替代人工 B：其判据与 A 同源，只能证明注入产生了预期可观测。",
                },
            },
        }
        cases.append(rec)
        gold.append({
            "case_id": cid,
            "source_injection_id": inj_id,
            "working_set_sha256": ws_sha,
            "auditor_A": {
                "role": "injector（我方；D35 在冻结基底上构造注入差分）",
                "sees": "注入清单（cases[].injections）",
                "method": "注入算子集合（canonical_injection 的 op/target）",
                "verdict": list(declared),
                "n_injections": len(injections),
                "injection_ops": sorted({i["op"] for i in injections}) or ["noop"],
            },
            "auditor_B": {
                "role": "machine（独立复算，不看注入算子）",
                "sees": "query + CCF（由基底 + 注入差分重建的工作集）",
                "method": "confirmable()：对工作集逐条求值 fault-modes.yaml#fault_modes[].detect",
                "verdict": list(conf_codes),
                "n_confirmable": len(conf_codes),
            },
            # ★ agree 不是自报：两者不一致时上面的 problems 已经抛错（fail-closed），
            #   台账只是把「构造期断言成立」这件事持久化，便于第三方复核。
            "agree": sorted(declared) == sorted(conf_codes),
            "disposition": ("accepted" if sorted(declared) == sorted(conf_codes) else "must_review"),
        })
        mapping.append({
            "case_id": cid, "family": cls, "chain": chain, "difficulty": diff,
            "injected": declared, "confirmable": conf_codes,
            "expect_root_cause": root, "expect_layer": expect_layer,
            "expect_path": expect_path,
            "path_basis": ("causal-edge" if (symptom and len(expect_path) > 1) else
                           ("none" if root == NONE else "single-node")),
            "symptom": symptom,
            "expect_objects": ev_objs,
            "evidence_facts": ev_obs,
            "evidence_metrics": ev_metrics,
            "injections_n": len(injections),
            "injection_ops": sorted({i["op"] for i in injections}) or ["noop"],
            "removed_objects": removed,
            "working_set_sha256": ws_sha,
        })

    if problems:
        raise BuildError("真值复算与用例声明不一致（fail-closed）：\n  - " + "\n  - ".join(problems))

    ledger = {
        "schema": "m5-input-facts/1",
        "meta": {
            "base_ref": "../../" + BASE_REL.replace(os.sep, "/"),
            "snapshot_id": SNAPSHOT_ID,
            "canonical_sha256": BASE_CANON,
            "captured_at": (base.doc.get("capturedAt") or "")[:26],
            "coverage_checked": True,
            "case_count": len(ledger_cases),
            "injector": "shared/collect/m5_inject_build.py",
            "oracle": "shared/model/fault-modes.yaml#fault_modes[].detect 复算",
            "case_selection_channel": ("--case / 环境变量 M5_CASE_ID（进程级信道）；"
                                       "工具 schema 里没有任何用例参数（D48）"),
            "note": ("基底引用 + 每例注入差分 + 三哈希；value_before 逐字取自基底实值，"
                     "不符即 BLOCKED。工作集是 (case_id) 的纯函数。"),
            "uncovered_gaps": UNCOVERED_GAPS,
        },
        "cases": ledger_cases,
        "mapping": mapping,
    }

    cases_doc = {
        "meta": {
            "schema": "m5.cases/1",
            "synthetic": False,
            "snapshot_binding": "case_field",
            "snapshot_ids": [SNAPSHOT_ID],
            "fault_injection_ledger": FACTS_REL,
            "gold_audit": GOLD_REL,   # ★ 双人复核台账（docs/06 §1.4 步骤 3）
        },
        "cases": cases,
    }

    # 固化文件自身 digest：口径 = sha256(canonical_json(整份正本去掉 meta 下两键))，
    # 不自我指涉，且任何第三方都能用同一配方复现（校验器 m5_facts_check.py 即按此复算）。
    DIGEST_KEYS = ("frozen_digest_sha256", "frozen_digest_policy")
    pre = copy.deepcopy(ledger)
    for _k in DIGEST_KEYS:
        pre["meta"].pop(_k, None)
    self_sha = hashlib.sha256(json.dumps(
        pre, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()
    ledger["meta"]["frozen_digest_sha256"] = self_sha
    ledger["meta"]["frozen_digest_policy"] = (
        "整份正本（schema+meta+cases+mapping）先移除 meta 下的 %s 两键，再取 "
        "sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',',':')))"
        % "+".join(DIGEST_KEYS))
    gold_doc = {
        "schema": "m5-gold-audit/1",
        "meta": {
            "role": ("gold_audit 台账：docs/06 §1.4 步骤 3「双人复核」的持久化。"
                     "A 只拿注入清单写 expect_*；B 只拿 query + CCF 独立给根因；不一致 ⇒ 该用例作废或修正。"),
            "spec": "docs/06-评测设计（A-B对照）.md:112-115",
            "case_count": len(gold),
            "builder": "shared/collect/m5_inject_build.py",
            "mechanical_only": True,
            "human_review": {
                "status": "waived",
                "waived_by": "用户裁定（2026-10-09）：不需要双人复核，由两个机器角色完成",
                "limitation": ("★ 机器角色**不能替代人类**：B 的判据（fault-modes.detect）与 A 的派生同源，"
                               "两个角色联合只能证明「注入产生了预期可观测、且两个独立实现得出同一结论」；"
                               "**不能**证明「题目本身对模型可判」。任何对外结论都必须带上这条局限。"),
                "annotator_A": None,
                "annotator_B": None,
                "note": ("★ 机器侧（auditor_B）**不能替代人工 B**：它的判据（fault-modes.detect）与 A 的派生同源，"
                         "只能证明「注入产生了预期可观测」；人工 B 要证明的是「题目本身可判」。"
                         "两个人工位必须是**不同**的自然人，并各自记录 reviewed_at 与结论。"),
            },
        },
        "cases": gold,
    }
    ledger_bytes = dump_yaml_bytes(ledger)
    cases_bytes = dump_yaml_bytes(cases_doc)
    gold_bytes = dump_yaml_bytes(gold_doc)

    if verbose:
        print("[build] base      %s" % base.source["path"])
        print("[build] base fp   %s" % json.dumps(base.fp, ensure_ascii=False))
        print("[build] cases     %d" % len(cases))
        print("[build] ledger    %s" % json.dumps(fingerprint_bytes(ledger_bytes)))
        print("[build] recorded  %s" % json.dumps(fingerprint_bytes(cases_bytes)))
        print("[build] gold      %s" % json.dumps(fingerprint_bytes(gold_bytes)))
    return {"ledger": ledger, "ledger_bytes": ledger_bytes,
            "cases": cases_doc, "cases_bytes": cases_bytes,
            "gold": gold_doc, "gold_bytes": gold_bytes,
            "mapping": mapping, "base": base}


# --------------------------------------------------------------- CLI

def write_deliverables(result, apply=False):
    outs = {}
    for rel, key in ((FACTS_REL, "ledger_bytes"), (CASES_REL, "cases_bytes"), (GOLD_REL, "gold_bytes")):
        path = os.path.join(ROOT, rel)
        data = result[key]
        outs[rel] = dict(fingerprint_bytes(data), path=path,
                         existed=os.path.isfile(path))
        if apply:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
    return outs


def cmd_check(result):
    bad = []
    for rel, key in ((FACTS_REL, "ledger_bytes"), (CASES_REL, "cases_bytes"), (GOLD_REL, "gold_bytes")):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            bad.append("%s 不存在" % rel)
            continue
        with open(path, "rb") as fh:
            disk = fh.read()
        want = result[key]
        if disk != want:
            bad.append("%s 与重建逐字节不一致（disk %d B / sha %s ≠ rebuilt %d B / sha %s）"
                       % (rel, len(disk), hashlib.sha256(disk).hexdigest()[:16],
                          len(want), hashlib.sha256(want).hexdigest()[:16]))
    return bad


def selftest():
    ok = 0
    total = 0
    notes = []

    def check(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print("  [ok]   %s" % name)
        else:
            print("  [FAIL] %s %s" % (name, detail))
            notes.append(name)

    print("[selftest] 1) 幂等：两次构造逐字节相同")
    r1 = build_all(verbose=False)
    r2 = build_all(verbose=False)
    check("ledger-idempotent", r1["ledger_bytes"] == r2["ledger_bytes"])
    check("cases-idempotent", r1["cases_bytes"] == r2["cases_bytes"])

    print("[selftest] 2) 工作集纯函数性：同 case 重建两次 sha 相同")
    base = r1["base"]
    sample = [c["case_id"] for c in r1["ledger"]["cases"][:6]]
    for cid in sample:
        rec = next(c for c in r1["ledger"]["cases"] if c["case_id"] == cid)
        inj = [replay_injection(i) for i in rec["injections"]]
        a = LF.build_working_set(base.source, {"case_id": cid, "injections": inj})
        b = LF.build_working_set(base.source, {"case_id": cid, "injections": inj})
        check("pure-%s" % cid,
              a["meta"]["working_set_sha256"] == b["meta"]["working_set_sha256"]
              == rec["working_set_sha256"])

    print("[selftest] 3) 基底未被就地改写")
    for cid in sample:
        rec = next(c for c in r1["ledger"]["cases"] if c["case_id"] == cid)
        inj = [replay_injection(i) for i in rec["injections"]]
        ws = LF.build_working_set(base.source, {"case_id": cid, "injections": inj})
        rep = ws.get("applied_report") or {}
        if not rep.get("base_unchanged", False):
            check("base-unchanged-%s" % cid, False, json.dumps(rep, ensure_ascii=False)[:200])
        else:
            check("base-unchanged-%s" % cid, True)

    print("[selftest] 4) value_before 不符 ⇒ fail-closed 拒绝")
    rec = next(c for c in r1["ledger"]["cases"]
               if c["injections"] and c["injections"][0]["op"] == "set_fact")
    bad = copy.deepcopy(rec["injections"])
    bad[0]["value_before"] = "基底里并不存在的取值-9e9"
    try:
        LF.build_working_set(base.source, {"case_id": "SELFTEST-BADVALUE", "injections": bad})
        check("value-before-fail-closed", False, "错误 value_before 竟被接受")
    except Exception as exc:  # noqa: BLE001
        check("value-before-fail-closed", "value_before" in str(exc) or "取值" in str(exc),
              str(exc)[:200])

    print("[selftest] 5) 未声明差分 fail-closed")
    rec = next(c for c in r1["ledger"]["cases"] if c["case_id"] == "A-05")
    inj = [replay_injection(i) for i in rec["injections"]]
    ws_full = LF.build_working_set(base.source, {"case_id": "A-05", "injections": inj})
    # 5a 静默丢弃该例注入 ⇒ 工作集 sha 必须与固化值不同（不得等价通过）
    ws_empty = LF.build_working_set(base.source, {"case_id": "A-05", "injections": []})
    check("dropped-injection-not-equivalent",
          ws_empty["meta"]["working_set_sha256"] != rec["working_set_sha256"],
          ws_empty["meta"]["working_set_sha256"])
    # 5b 谎报 declared（空声明）⇒ verify_declared_diff 必须抛错
    ws_doc = copy.deepcopy(base.doc)
    for k in ("objects", "relations", "observations", "integrity"):
        ws_doc[k] = copy.deepcopy(ws_full[k])
    try:
        LF.verify_declared_diff(base.doc, ws_doc, LF._declared_empty(), label="selftest-lie")
        check("undeclared-diff-fail-closed", False, "空声明的非空差分竟被接受")
    except Exception as exc:  # noqa: BLE001
        check("undeclared-diff-fail-closed", "未声明差分" in str(exc), str(exc)[:200])
    # 5c 逐字声明的差分必须通过（正控）
    try:
        LF.verify_declared_diff(base.doc, ws_doc, ws_full["diff"]["declared"],
                                label="selftest-ok")
        check("declared-diff-passes", True)
    except Exception as exc:  # noqa: BLE001
        check("declared-diff-passes", False, str(exc)[:200])

    print("[selftest] 6) 契约计数：50 / 族 / 难度 / id 唯一 / 锚点格式")
    import re as _re
    codes = set()
    for c in r1["cases"]["cases"]:
        codes.add(c["expect_root_cause"])
        codes.update(c["injected"])
        codes.update(c["expect_path"])
    codes.discard("NONE")
    check("suite-50", len(r1["cases"]["cases"]) == 50)
    fam = {}
    dif = {}
    for c in r1["cases"]["cases"]:
        fam[FAMILY_BY_PREFIX[c["cls"]]] = fam.get(FAMILY_BY_PREFIX[c["cls"]], 0) + 1
        dif[c["difficulty"]] = dif.get(c["difficulty"], 0) + 1
    check("family-tally", fam == FAMILIES, json.dumps(fam))
    check("difficulty-tally", dif == DIFFICULTY_TALLY, json.dumps(dif))
    check("id-prefix", all(
        _re.match(r"^%s[-_]?\d+$" % c["cls"], c["id"])
        for c in r1["cases"]["cases"]))
    check("anchor-format", all(
        _re.match(r"^(FIXTURE-)?INJ-\d{8}-\d{3}$", c["source_injection_id"])
        for c in r1["cases"]["cases"]))
    check("snapshot-single", r1["cases"]["meta"]["snapshot_ids"] == [SNAPSHOT_ID])
    check("detectors-compile", all(
        all(_re.compile(d) for d in c["detectors"]) for c in r1["cases"]["cases"]))
    check("lexicon-compile", all(
        all(_re.compile(p) for p in c["pos"] + c["neg"] + c["nor"])
        for c in r1["cases"]["cases"]))
    check("oracle-in-enum", codes <= set(ROOT_LEX) | set(), json.dumps(sorted(codes)))

    print("[selftest] 7) 三哈希齐备 + 单例 dump 往返")
    for c in r1["ledger"]["cases"]:
        if not _re.match(r"^[0-9a-f]{64}$", c["working_set_sha256"] or ""):
            check("ws-sha-%s" % c["case_id"], False, str(c["working_set_sha256"]))
            break
    else:
        check("ws-sha-all", True)
    check("ledger-self-sha", _re.match(r"^[0-9a-f]{64}$",
                                       r1["ledger"]["meta"]["frozen_digest_sha256"]) is not None)
    tmp = os.path.join(os.environ.get("TEMP") or "/tmp", "s4a_gates", "_dump_case.json")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    dump_case(base.source, "A-05", tmp)
    again = dump_case(base.source, "A-05", tmp + ".2")
    with open(tmp, "rb") as f1, open(tmp + ".2", "rb") as f2:
        check("dump-case-identical", f1.read() == f2.read())

    print("SELFTEST: %d/%d" % (ok, total))
    return 0 if ok == total else 1


def dump_case(source, case_id, out_path):
    """单例重建并 dump 工作集（证明可逐字重建）。"""
    with open(os.path.join(ROOT, FACTS_REL), "r", encoding="utf-8") as fh:
        doc = _yaml().safe_load(fh)
    rec = None
    for c in doc.get("cases") or []:
        if c.get("case_id") == case_id:
            rec = c
            break
    if rec is None:
        raise BuildError("case_id=%s 不在 %s 的 cases 里" % (case_id, FACTS_REL))
    inj = [replay_injection(i) for i in rec.get("injections") or []]
    ws = LF.build_working_set(source, {"case_id": case_id, "injections": inj})
    actual = ws["meta"]["working_set_sha256"]
    if actual != rec.get("working_set_sha256"):
        raise BuildError("case_id=%s 重建 working_set_sha256=%s ≠ 固化值 %s（fail-closed）"
                         % (case_id, actual, rec.get("working_set_sha256")))
    payload = {"case_id": case_id, "working_set_sha256": actual,
               "objects": ws["objects"], "relations": ws["relations"],
               "observations": ws["observations"], "meta": ws["meta"]}
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="M5 S4a 注入构造器（离线）")
    ap.add_argument("--build", action="store_true", help="构造并写入两个正本")
    ap.add_argument("--dry-run", action="store_true", help="构造但不落盘")
    ap.add_argument("--check", action="store_true", help="磁盘正本 vs 重建 逐字节比对")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dump-case", default="", help="单例重建并 dump 到 --out")
    ap.add_argument("--out", default="")
    ap.add_argument("--map-report", action="store_true", help="打印 注入→现象→用例 映射")
    ap.add_argument("--base", default="")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.dump_case:
        base = Base(args.base or os.path.join(ROOT, BASE_REL))
        out = args.out or os.path.join(os.environ.get("TEMP") or "/tmp",
                                       "s4a_gates", "dump_%s.json" % args.dump_case)
        data = dump_case(base.source, args.dump_case, out)
        print("DUMP %s -> %s (%d B, sha256 %s)"
              % (args.dump_case, out, len(data), hashlib.sha256(data).hexdigest()))
        return 0
    result = build_all(args.base or None)
    if args.map_report:
        print(json.dumps(result["mapping"], ensure_ascii=False, indent=1))
        return 0
    if args.build:
        outs = write_deliverables(result, apply=True)
        for rel, info in outs.items():
            print("[write] %-40s %d B  LF %d  sha256 %s"
                  % (rel, info["bytes"], info["lf"], info["sha256"]))
        return 0
    if args.dry_run:
        print("[dry-run] 未落盘；三哈希与计数见上")
        return 0
    bad = cmd_check(result)
    if bad:
        for b in bad:
            print("[FAIL] %s" % b)
        return 1
    print("[check] 两个正本与重建逐字节一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
