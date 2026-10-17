#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shared/collect/freeze.py —— CCF 固化器（M1.3/M1.4 的实现）。

职责
    把复用采集层的一次真实采集固化成一份**可审计、可复现、可拒绝**的 CCF 快照。
    它是硬约束 C1（单一输入）在工程上的唯一入口：两侧投影器都只读它写出的快照。

设计原则（逐条对应 docs/02 与 docs/07）
    1. 只走 HTTP（docs/02 §5.1）。进程内 import 会踩 H1（探针模块级读 .env，晚加载
       则静默 0 事实且 /health 仍报 ok=true），HTTP 形态下 _conn_kwargs 由
       collect_api 自己解析，不受导入顺序影响。
    2. 不读采集层的进程内缓存。collect_api._CACHED_COLLECT_RESULT 用 .update()
       累积且永不清空，是跨请求并集（H4 的真实形态）；_CACHED_COLLECT_TTL 是死常量。
    3. 只用 /tools/gather 的 all_facts[]（生产者 A，BaseProbe._make_fact，六键含
       obj_type）。不用 /tools/gather_bounded（它会再采一次，是第二次真实采集）。
    4. fail-closed：来源门禁、层内事实数门禁、覆盖门禁任一不过就**拒绝写出**，
       不产出「看起来正常」的快照。这是 H1 与 G5 的机械处置。
    5. 确定性：事实按全序排序后才编号 id；一切时钟字段进 x 区或固定为快照级时间戳。

零第三方依赖（HTTP 用 urllib）+ PyYAML（读 .yaml 契约；两个解释器都有）。

用法
    # 固化一份四层快照（写入前自检，自检不过不写）
    python shared/collect/freeze.py \
        --collect-url http://192.0.2.10:8187 \
        --layers gaussdb,os \
        --expected-profile l3-only \
        --out snapshots/ccf-l3-001.json

    # 离线固化（用已保存的 /health 与 /tools/gather 回执，供 CI 自测）
    python shared/collect/freeze.py --health-json cap.health.json \
        --gather-json cap.gather.json --expected-profile l3-only --out s.json

    # 看一眼会发生什么，但不写文件
    python shared/collect/freeze.py --collect-url URL --layers gaussdb --dry-run
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

try:
    import yaml
except ImportError:                                             # pragma: no cover
    yaml = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_PATH = os.path.join(ROOT, "shared", "model", "ccf.schema.json")
TOPO_PATH = os.path.join(ROOT, "shared", "model", "topology.yaml")
MAP_PATH = os.path.join(ROOT, "shared", "model", "v3-ccf-map.yaml")
UPSTREAM = r"<workspace>\upstream-ops-agent"

sys.path.insert(0, os.path.join(ROOT, "shared", "verify"))
import equivalence                                            # noqa: E402

CAP_VALUES, CAP_OBJS, MAX_STR = 8, 4, 120
SAFE_LOCAL = re.compile(r"[^A-Za-z0-9._:-]")


class FreezeError(Exception):
    """fail-closed：固化必须中止。"""


# ----------------------------------------------------------------- 基础工具

def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def file_sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def load_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml(path):
    if yaml is None:
        raise FreezeError("需要 PyYAML 才能读 %s" % path)
    with io.open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_snapshot_id():
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "ccf-%s-%s" % (ts, hashlib.sha256(os.urandom(16)).hexdigest()[:6])


def canon_value(v):
    try:
        return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:                                            # noqa: BLE001
        return repr(v)


def http_text(url, payload=None, timeout=300):
    """GET / POST(JSON)。★ 不发 X-GDO-Internal 头（docs/02 H16：它会绕过 _loop_guard）。"""
    data = None
    headers = {"Accept": "application/json", "User-Agent": "ccf-freeze/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:                        # noqa: PERF203
        body = exc.read().decode("utf-8", "replace")[:500]
        raise FreezeError("HTTP %s %s：%s" % (exc.code, url, body))
    except urllib.error.URLError as exc:
        raise FreezeError("无法连接 %s：%s" % (url, exc))


def norm_multi(x):
    """collect_api 的 layer_errors 会在 dict 与 list 之间翻转（:686 vs :771），两种都容忍。"""
    if not x:
        return {}
    if isinstance(x, dict):
        return dict(x)
    if isinstance(x, list):
        return {str(i): str(v) for i, v in enumerate(x)}
    return {"_": str(x)}


# ----------------------------------------------------------------- 契约加载

class Contracts(object):
    def __init__(self, schema_path, topo_path, map_path):
        self.schema = load_json(schema_path)
        self.topo = load_yaml(topo_path)
        self.map = load_yaml(map_path)
        self.type_layer = {}
        for t in (self.topo.get("types") or []):
            code = t.get("code")
            if code and t.get("layer"):
                self.type_layer[code] = t["layer"]
        self.type_enum = set(equivalence.enum_of(self.schema, "typeCode"))
        self.metric_enum = set(equivalence.enum_of(self.schema, "metricCode"))
        self.relation_enum = set(equivalence.enum_of(self.schema, "relationCode"))
        self.prov_enum = {"gaussdb", "os", "container", "app", "derived"}

    def objtype_spec(self, obj_type):
        spec = (self.map.get("objtype_to_type") or {}).get(obj_type)
        if spec is None:
            return None
        return spec if isinstance(spec, dict) else {"type": spec}

    def resolve_type(self, obj_type, path):
        spec = self.objtype_spec(obj_type)
        if spec is None:
            return None, None
        by_path = spec.get("by_path") or {}
        for pat, override in by_path.items():
            if re.match(pat, path):
                merged = dict(spec)
                merged.update(override if isinstance(override, dict) else {})
                return merged, override
        return spec, None

    def metric_spec(self, path):
        return (self.map.get("path_to_metric") or {}).get(path)

    def rewrite_obj_id(self, obj_type, raw_id):
        """obj_id 重写：map 的 obj_id_rewrite 块，按声明顺序依次套用（re.sub，支持反向引用）。

        用途：把上游同一实体的两套命名收敛成一个 CCF 对象，例如
        `sql-now-<qid>` 与 `sql-<qid>`（同一条 SQL 的「计划态」与「历史态」）、
        `tx-<pid>` 与 `sess-<pid>`（同一个会话的事务与状态）；
        以及给 `table` 的裸表名补上 `tbl-` 前缀。
        ★ 只做声明的重写，绝不做通配合并。
        """
        s = str(raw_id)
        for rule in (self.map.get("obj_id_rewrite") or []):
            if rule.get("obj_type") not in (None, obj_type):
                continue
            pat = rule.get("pattern") or "^$"
            if re.match(pat, s):
                s = re.sub(pat, rule.get("replace") or "", s)
        return s

    def auto_object_spec(self, type_code):
        return (self.map.get("auto_objects") or {}).get(type_code)

    def pool_spec(self):
        return self.map.get("pool_objects") or {}


# ----------------------------------------------------------------- 对象 / id

def sanitize_local(raw):
    s = SAFE_LOCAL.sub("_", str(raw)).strip(":")
    return s or None


class IdResolver(object):
    """把采集层的 obj_id 重映射到 CCF objectId（docs/02 §4-A 硬规则 1）。"""

    def __init__(self, cont, gather):
        self.cont = cont
        topo = (gather.get("topology") or {})
        self.cluster_id = str(topo.get("cluster_id") or "unknown")
        self.cluster_name = str(topo.get("cluster_name") or self.cluster_id)
        self.primary_ip = topo.get("primary_ip")
        self.ip_index = gather.get("ip_index") or {}
        self.node_by_id = {}
        for n in (topo.get("nodes") or []):
            nid = str(n.get("node_id") or "")
            if nid:
                self.node_by_id[nid] = n
        # ★ L1/L2 侧的主机（k8s worker）不在 topology{} 里：身份改由应用拓扑段的 hosts[] 提供。
        #   两处权威来源并列、互不覆盖；都不认的主机仍然记账丢弃（host_not_in_topology）。
        #   字段名逐字等于映射表 `app_derive.keys.hosts` 的约定（hostId / ip），不做别名归一。
        self.app_hosts = {}
        self.app_host_ip = {}
        for h in (_app_section(gather).get("hosts") or []):
            if not isinstance(h, dict):
                continue
            hid = str(h.get("hostId") or "")
            if hid:
                self.app_hosts[hid] = h
            hip = str(h.get("ip") or "")
            if hip:
                self.app_host_ip[hip] = h
        self.prefix = (cont.schema.get("x-id-prefix-by-type") or {})
        self.type_by_prefix = {v: k for k, v in self.prefix.items()}
        self.primary_node_id = self._pick_primary_node(topo)

    def _pick_primary_node(self, topo):
        """集群聚合事实（cluster.connection_max / gaussdb.session.*）不带节点身份 ⇒ 一律落到主节点。

        ★ 这不是猜测节点角色：主备判定只认 _real_topology() 给出的 primary_ip / node_role，
        与 docs/02 §5.2「拓扑唯一权威来源」一致。
        """
        cand = None
        for n in (topo.get("nodes") or []):
            nid = str(n.get("node_id") or "")
            if not nid:
                continue
            if self.primary_ip and str(n.get("host_ip")) == str(self.primary_ip):
                return nid
            if cand is None and str(n.get("node_role") or "").lower().startswith("prim"):
                cand = nid
        if cand:
            return cand
        return sorted(self.node_by_id)[0] if self.node_by_id else None

    def _mk(self, type_code, local):
        local = sanitize_local(local)
        if not local:
            return None
        pfx = self.prefix.get(type_code)
        if not pfx:
            return None
        return "%s:%s" % (pfx, local)

    def cluster(self):
        return self._mk("GaussDBCluster", self.cluster_id)

    def node(self, raw):
        """raw 可以是 node_id、节点 IP，或集群级占位符（空 / cluster / unknown）。"""
        raw = str(raw or "")
        if raw in ("", "cluster", "unknown", "primary") or raw == self.cluster_id:
            raw = self.primary_node_id or ""
        if raw in self.node_by_id:
            return self._mk("GaussDBInstance", raw)
        info = self.ip_index.get(raw) or {}
        nid = info.get("node_id")
        if nid:
            return self._mk("GaussDBInstance", nid)
        if raw:
            return self._mk("GaussDBInstance", raw)
        return None

    def host(self, raw):
        """Host 的规范身份取 topology.nodes[].host_id（唯一权威来源）。

        ★ 真机实测（2026-09-28）：os 层的 fact 用**主机 IP** 作 obj_id，而拓扑用的是
        host_id（形如 host-dn_6001）⇒ 不在这里收敛，同一台机器会分裂成两个 Host 对象，
        主机侧 6 个指标会因「对象不存在」被整批丢弃（症状：missingMetrics 全是 host.*）。
        不在权威拓扑里的主机返回 None，由调用方记账丢弃（host_not_in_topology）。
        """
        raw = str(raw or "")
        for n in self.node_by_id.values():
            if raw and raw == str(n.get("host_id") or ""):
                return self._mk("Host", raw)
        info = self.ip_index.get(raw) or {}
        nid = info.get("node_id")
        if not nid and raw in self.node_by_id:
            nid = raw
        if nid:
            n = self.node_by_id.get(nid) or {}
            hid = str(n.get("host_id") or "")
            return self._mk("Host", hid or nid)
        # L1/L2 侧的主机（k8s worker）：身份来自 app_topology.hosts[]
        ah = self.app_hosts.get(raw) or self.app_host_ip.get(raw)
        if ah:
            return self._mk("Host", str(ah.get("host_id") or raw))
        return None

    def literal(self, type_code, raw):
        return self._mk(type_code, raw)

    def pool(self, instance_oid):
        """每个 GaussDBInstance 一个 DatabaseSessionPool（map 的 pool_objects 块）。"""
        spec = self.cont.pool_spec()
        if not spec or not instance_oid:
            return None
        local = instance_oid.split(":", 1)[1] if ":" in instance_oid else instance_oid
        return self._mk(spec.get("type") or "DatabaseSessionPool",
                        "%s%s" % (local, spec.get("id_suffix") or ""))


# ----------------------------------------------------------------- 事实映射

def fact_sort_key(f):
    return (str(f.get("path") or ""), str(f.get("obj_id") or ""),
            canon_value(f.get("value")), str(f.get("source") or ""))


def dedupe_and_sort(facts):
    """层内并发（ThreadPoolExecutor）⇒ 事实顺序不确定，哈希前必须全序排序并去重。"""
    seen = set()
    out = []
    dup = 0
    for f in sorted(facts, key=fact_sort_key):
        k = fact_sort_key(f)
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        out.append(f)
    return out, dup


def delta_from_fact(f):
    """从采集事实里取「窗口基线（delta）」，返回 (delta, delta_window_s) 或 (None, None)。

    ★ M2.10：`predicate: increased / decreased` 的可机判前提。
      fault-modes.yaml 的 meta.predicate_semantics 规定：increased = observation.delta 存在且为正，
      decreased = delta 存在且为负，且 baseline_required 含这两个判据。

    ★ 宁缺勿造（本函数的全部意义）：
      * 没有 delta 键 ⇒ 返回 (None, None)，**不补 0**。
      * 有 delta 而缺 deltaWindowS ⇒ 也返回 (None, None)：没有窗口长度就无法解释增量，
        宁可让下游报 unjudgeable，也不产出一个语义不完整的 delta。
      * deltaWindowS <= 0 ⇒ 同样不产出（0 秒窗口没有意义，等同于「未启用窗口基线」）。
      * delta 非数值（bool / str / None）⇒ 不产出，不当成 0。
      这样 CCF observation 里「有 delta」与「无 delta」是二值的，下游可以据此把判据判成
      unjudgeable 而不是静默 false。

    ★ 为什么在 freeze 侧再校验一次：探针是新代码，可能被别的上游（或旧版本探针）绕过；
      契约校验点必须落在「写入 CCF 的位置」而不是采集侧。
    """
    if "delta" not in f:
        return None, None
    d = f.get("delta")
    if isinstance(d, bool) or not isinstance(d, (int, float)):
        return None, None
    w = f.get("deltaWindowS")
    if isinstance(w, bool) or not isinstance(w, (int, float)):
        return None, None
    if float(w) <= 0:
        return None, None
    return d, w


def map_fact(f, cont, idr, drop, examples):
    path = str(f.get("path") or "")
    obj_type = str(f.get("obj_type") or "")
    raw_id = f.get("obj_id")
    if not obj_type:
        drop["no_obj_type"] += 1
        examples.append("%s %s (no_obj_type)" % (obj_type or "-", path))
        return None, None
    spec, _ = cont.resolve_type(obj_type, path)
    if spec is None:
        drop["objtype_unmapped"] += 1
        examples.append("%s %s (objtype_unmapped)" % (obj_type, path))
        return None, None
    if spec.get("drop"):
        drop["objtype_dropped:%s" % obj_type] += 1
        examples.append("%s %s (objtype_dropped)" % (obj_type, path))
        return None, None
    type_code = spec.get("type")
    if type_code not in cont.type_enum:
        drop["type_not_in_ccf"] += 1
        examples.append("%s %s (type_not_in_ccf=%s)" % (obj_type, path, type_code))
        return None, None

    # 伪对象（取值桶）必须处置，不得直传（H3：否则 objectsMin 被虚高满足 = 假通过 G5）
    for rule in (cont.map.get("pseudo_objects") or []):
        if rule.get("obj_type") in (None, obj_type) and re.match(rule.get("pattern", "^$"), str(raw_id)):
            if rule.get("rewrite") == "owner":
                raw_id = rule.get("owner_local")
            else:
                drop["pseudo_object"] += 1
                examples.append("%s %s (pseudo_object=%s)" % (obj_type, path, raw_id))
                return None, None
            break

    # 同一实体的多套命名收敛成一个 CCF 对象：sql-now-<qid>→sql-<qid>、tx-<pid>→sess-<pid>、裸表名→tbl-<name>
    if raw_id is not None:
        raw_id = cont.rewrite_obj_id(obj_type, raw_id)

    if type_code == "GaussDBCluster":
        oid = idr.cluster()
    elif type_code == "GaussDBInstance":
        oid = idr.node(raw_id)
    elif type_code == "Host":
        oid = idr.host(raw_id)
    else:
        oid = idr.literal(type_code, raw_id)
    if not oid:
        if type_code == "Host":
            drop["host_not_in_topology"] += 1
            examples.append("%s %s (host_not_in_topology=%r)" % (obj_type, path, raw_id))
            return None, None
        drop["empty_obj_id"] += 1
        examples.append("%s %s (empty_obj_id=%r)" % (obj_type, path, raw_id))
        return None, None

    m = cont.metric_spec(path)
    if m is None:
        drop["path_unmapped"] += 1
        examples.append("%s %s (path_unmapped)" % (obj_type, path))
        return None, None
    if m.get("drop"):
        drop["path_dropped:%s" % m.get("reason", "no_reason")] += 1
        examples.append("%s %s (path_dropped)" % (obj_type, path))
        return None, None
    metric = m.get("metric")
    if metric not in cont.metric_enum:
        drop["metric_not_in_ccf"] += 1
        examples.append("%s %s (metric_not_in_ccf=%s)" % (obj_type, path, metric))
        return None, None

    value = f.get("value")
    raw_value, raw_unit = value, m.get("raw_unit")
    unit = m.get("unit")
    conv = (cont.map.get("unit_convert") or {}).get(path)
    if conv:
        try:
            value = float(value) * float(conv.get("factor", 1.0))
            if conv.get("round") is not None:
                value = round(value, int(conv["round"]))
            if float(value).is_integer():
                value = int(value)
        except (TypeError, ValueError):
            drop["unit_convert_failed"] += 1
            examples.append("%s %s (unit_convert_failed=%r)" % (obj_type, path, value))
            return None, None
        raw_unit = conv.get("from_unit")

    src = str(spec.get("provenance") or "gaussdb")
    if src not in cont.prov_enum:
        drop["bad_provenance"] += 1
        return None, None

    obs = {
        "objectId": oid,
        "metricCode": metric,
        "provenance": {"source": src, "probe": str(f.get("source") or "")[:120],
                       "collectedAt": None},
        "_kind": m.get("kind", "number"),
    }
    if value is None:
        obs["status"] = "notCollected"
        obs["reason"] = m.get("null_reason", "no_value")
    elif m.get("kind") == "text":
        obs["status"] = "collected"
        obs["valueText"] = str(value)
        obs["unit"] = unit
    else:
        obs["status"] = "collected"
        obs["value"] = value
        obs["unit"] = unit
    if raw_value is not None:
        obs["rawValue"] = raw_value
        if raw_unit:
            obs["rawUnit"] = raw_unit
    # ★ M2.10：窗口基线（delta）透传 —— **纯新增可选字段，条件性写入**。
    #   无 delta 的事实一个键都不加（⇒ 既有夹具的 canonicalSha256 逐字节不变）。
    #   delta 与 deltaWindowS 同进同出（ccf.schema.json $defs.observation 的 if/then 已锁定）。
    d_val, d_win = delta_from_fact(f)
    if d_val is not None:
        obs["delta"] = d_val
        obs["deltaWindowS"] = d_win
    return obs, path


# ----------------------------------------------------------------- 拓扑派生

def status_is_up(s, rule):
    """up/down 判定只按 map 声明的取值表，不做模糊匹配。"""
    if s is None:
        return False
    return s not in (rule.get("up_exclude") or [])


def derived_obs(object_id, metric, rule, status):
    """按 derive_topology 规则算一个值。status 是标量（per_node）或列表（集群聚合）。"""
    if rule.get("boolean"):
        vals = status if isinstance(status, list) else [status]
        val = all(status_is_up(s, rule) for s in vals)
    else:
        vals = status if isinstance(status, list) else [status]
        if rule.get("down_values") is not None:
            val = sum(1 for s in vals if s in (rule.get("down_values") or []))
        else:
            val = sum(1 for s in vals if status_is_up(s, rule))
    return {"objectId": object_id, "metricCode": metric, "value": val,
            "unit": rule.get("unit"), "kind": "number"}


def derive_topo_objects(cont, idr, gather, objects, relations, derived):
    """由 /tools/gather 的 topology{} 建立 L3 的对象与关系（★ 唯一权威来源，见 docs/02 §5.2）。

    只建「拓扑能证明存在」的对象：集群、实例（含角色）、实例所在 Host、每实例一个会话池。
    Session/Query/Table 由观测侧补建（见 ensure_objects）。
    """
    topo = gather.get("topology") or {}
    cid = idr.cluster()
    if not cid:
        raise FreezeError("topology.cluster_id 缺失，无法建立 GaussDBCluster")
    nodes = list(topo.get("nodes") or [])
    objects[cid] = {
        "id": cid, "type": "GaussDBCluster",
        "layer": cont.type_layer.get("GaussDBCluster", "Data"),
        "key": {"clusterName": idr.cluster_name},
        "attrs": {"clusterName": idr.cluster_name, "nodeCount": len(nodes)},
    }
    primary_oid = None
    node_pairs = []
    for n in nodes:
        nid = str(n.get("node_id") or "")
        oid = idr.node(nid) or idr.node(n.get("host_ip"))
        if not oid:
            continue
        objects[oid] = {
            "id": oid, "type": "GaussDBInstance",
            "layer": cont.type_layer.get("GaussDBInstance", "Data"),
            "key": {"instanceId": nid or str(n.get("host_ip"))},
            "attrs": {"instanceId": nid or None,
                      "hostName": n.get("host_name") or n.get("host_id"),
                      "port": n.get("port"),
                      "role": n.get("node_role"),
                      "datanodeId": n.get("datanode_id")},
        }
        relations.add(("HAS_DB_INSTANCE", cid, oid))
        host_raw = n.get("host_id") or n.get("host_ip")
        hoid = idr.host(host_raw)
        if hoid:
            objects[hoid] = {
                "id": hoid, "type": "Host",
                "layer": cont.type_layer.get("Host", "Runtime"),
                "key": {"hostname": str(host_raw)},
                "attrs": {"hostname": str(host_raw), "ip": n.get("host_ip")},
            }
            relations.add(("HOSTS", hoid, oid))
        node_pairs.append((n, oid))
        if idr.primary_ip and str(n.get("host_ip")) == str(idr.primary_ip):
            primary_oid = oid
    if primary_oid:
        for n, oid in node_pairs:
            if oid != primary_oid and str(n.get("node_role") or "").lower().startswith("stand"):
                relations.add(("REPLICATES_TO", primary_oid, oid))

    # 会话池：每个 GaussDBInstance 一个（map 的 pool_objects 块）
    pool_spec = cont.pool_spec() or {}
    pools = []
    if pool_spec:
        ptype = pool_spec.get("type") or "DatabaseSessionPool"
        for inst in sorted(objects.values(), key=lambda v: v["id"]):
            if inst["type"] != "GaussDBInstance":
                continue
            poid = idr.pool(inst["id"])
            if not poid:
                continue
            objects[poid] = {
                "id": poid, "type": ptype,
                "layer": cont.type_layer.get(ptype, "Data"),
                "key": {pool_spec.get("key_attr") or "poolId": inst["id"].split(":", 1)[-1]},
                "attrs": {},
            }
            relations.add((pool_spec.get("relation") or "HAS_SESSION_POOL", inst["id"], poid))
            pools.append(poid)

    # 由拓扑派生的观测：角色与存活不得由 LLM 猜，必须来自这里
    for rule in (cont.map.get("derive_topology") or []):
        metric, ttype = rule.get("metric"), rule.get("type")
        if metric not in cont.metric_enum or ttype not in cont.type_enum:
            raise FreezeError("derive_topology 条目非法：metric=%r type=%r" % (metric, ttype))
        if rule.get("per_node"):
            for n, oid in node_pairs:
                derived.append(derived_obs(oid, metric, rule, n.get("node_status")))
        else:
            for tid in sorted(v["id"] for v in objects.values() if v["type"] == ttype):
                derived.append(derived_obs(tid, metric, rule,
                                           [n.get("node_status") for n, _ in node_pairs]))
    return objects, relations, pools


# ----------------------------------------------------------------- 对象补建与派生指标

def _app_section(gather):
    """合并 L1/L2 两层的应用拓扑段（M1.6）。

    容器探针只声明自己那半（`container_topology`：hosts + containers），应用探针声明
    另一半（`app_topology`：apps / app_instances / app_endpoints / datasources）；
    两者由 `service/collect_api.py` 的 `/tools/gather` 分别提升到响应顶层。
    这里做**纯拼接**：同一子键取并集，不做类型归一、不做去重、不替调用方猜默认值。
    """
    out = {}
    for key in ("container_topology", "app_topology"):
        blk = gather.get(key) or {}
        if not isinstance(blk, dict):
            continue
        for k, v in blk.items():
            if isinstance(v, list):
                out.setdefault(k, []).extend(v)
            elif isinstance(v, dict):
                out.setdefault(k, {}).update(v)
            else:
                out[k] = v
    return out


def derive_app_objects(cont, idr, gather, objects, relations, derived):
    """由采集面 app_topology{} 建立 L1/L2 的对象与关系（规则全在 map 的 app_derive 块）。

    权威性：见 map 的 app_derive 段头。要点：
      - 只建「探针能证明存在」的对象；段里出现但不在 keys 清单里的子键一律不建；
      - 字段名逐字等于 topology.yaml 里该类型的 attrs 名，不做别名/大小写归一；
      - key 由 key_attrs 从该条记录现取，缺任一 key_attr 即 raise（fail-closed），不回退不猜；
      - 关系只按 relations 清单建，双方都必须已是本函数建出的对象（否则不建、静默少边），
        ⇒ 覆盖门禁（expected.relationsMin）会发现少边，不会假通过。
      - 显式映射：不按命名习惯猜类型/关系（docs/02 §4-A 硬规则 1）。
    """
    spec = cont.map.get("app_derive") or {}
    if not spec:
        return {}
    sec = _app_section(gather)
    keys = spec.get("keys") or {}
    made = Counter()
    by_local = {}                      # (type_code, local_id) -> objectId

    def put(tcode, local, item, attrs_names, key_attrs):
        oid = idr.literal(tcode, local)
        if not oid:
            raise FreezeError("app_derive.keys: 类型 %s 的本地 id %r 生成不出对象 id" % (tcode, local))
        o = objects.get(oid)
        if o is None:
            o = {"id": oid, "type": tcode, "layer": cont.type_layer.get(tcode),
                 "key": {}, "attrs": {}}
            objects[oid] = o
        for name in attrs_names:
            v = item.get(name)
            if v in (None, ""):
                continue
            o["attrs"][name] = v if isinstance(v, (int, float, bool)) else str(v)[:MAX_STR]
        for ka in key_attrs:
            v = item.get(ka)
            if v in (None, ""):
                raise FreezeError(
                    "app_derive.keys: %s 的记录 local=%r 缺 key_attr %r"
                    "（fail-closed：key 只从 key_attrs 现取，不回退、不猜）" % (tcode, local, ka))
            o["key"][ka] = v if isinstance(v, (int, float, bool)) else str(v)[:MAX_STR]
        return oid

    for list_name in sorted(keys):
        kspec = keys.get(list_name) or {}
        tcode, idf = kspec.get("type"), kspec.get("id_field")
        if not tcode or not idf:
            raise FreezeError("app_derive.keys.%s 缺 type/id_field" % list_name)
        if tcode not in cont.type_enum:
            raise FreezeError("app_derive.keys.%s.type=%r 不在 CCF typeCode 里" % (list_name, tcode))
        attrs_names = list(kspec.get("attrs") or [])
        key_attrs = list(kspec.get("key_attrs") or [])
        if not key_attrs:
            raise FreezeError("app_derive.keys.%s 缺 key_attrs" % list_name)
        for name in key_attrs:
            if name not in attrs_names:
                attrs_names.append(name)
        for item in (sec.get(list_name) or []):
            if not isinstance(item, dict):
                continue
            local = str(item.get(idf) or "")
            if not local:
                continue
            oid = put(tcode, local, item, attrs_names, key_attrs)
            by_local[(tcode, local)] = oid
            made[tcode] += 1

    for rspec in (spec.get("relations") or []):
        code = rspec.get("code")
        if code not in cont.relation_enum:
            raise FreezeError("app_derive.relations 的 code=%r 不在 CCF relationCode 里" % (code,))
        lst = sec.get(rspec.get("list")) or []
        kind = rspec.get("to_kind") or "same"
        if kind not in ("same", "cluster", "instance_by_ip"):
            raise FreezeError("app_derive.relations.to_kind=%r 非法" % (kind,))
        for item in lst:
            if not isinstance(item, dict):
                continue
            frm = by_local.get((rspec.get("from_type"),
                                str(item.get(rspec.get("from_field")) or "")))
            if not frm:
                continue
            to = None
            if kind == "same":
                to = by_local.get((rspec.get("to_type"),
                                   str(item.get(rspec.get("to_field")) or "")))
            elif kind == "cluster":
                to = idr.cluster()
            else:
                info = idr.ip_index.get(str(item.get(rspec.get("to_field")) or "")) or {}
                nid = info.get("node_id")
                to = idr.node(nid) if nid else None
            if to and to != frm:
                relations.add((code, frm, to))
    return dict(made)


def build_fact_index(cont, facts):
    """按 `(path, obj_id 的 local 段)` 建**原始事实**索引，供 auto_objects.key_fact 取值。

    为什么从原始事实取、而不是从观测取：key 事实（如 `table.schema_name`）在映射表里
    登记为 `drop: identity_metadata_not_a_metric`（它不是指标）⇒ 根本不会变成观测；
    而对象键必须在**补建对象的那一刻**就拿到它，所以这里直接扫冻结输入的全部事实。
    local 段与观测的 objectId 用同一套归一（rewrite_obj_id + sanitize_local），
    保证 `key_fact` 的 path 能和具体对象对上号。
    """
    idx = {}
    for f in (facts or []):
        path = f.get("path")
        if not path:
            continue
        local = sanitize_local(cont.rewrite_obj_id(f.get("obj_type"), f.get("obj_id")))
        if not local:
            continue
        idx[(path, local)] = f.get("value")
    return idx


def auto_object_key(cont, type_code, spec, local, fact_index):
    """按 map 的 auto_objects 声明，为一个自动补建对象算 `key`。

    ★ M2.11（裁决 14，2026-09-28）：支持**多 key**，并且**缺任一 key_attr 即 FreezeError**
      （fail-closed）。为什么必须硬失败：Table 的键是 (schemaName, tableName)，真机确实存在
      跨 schema 同名表（`snapshot.snapshot` 与 `db4ai.snapshot`）；此时若「少一个键就退化成
      单键」，两张不同的表会被静默并成同一个对象 —— 这正是本裁决要消灭的失真。宁可整个
      冻结失败并报出是哪张表缺哪个键，也不产出一个看起来正常、实际身份错误的图谱。

    取值来源逐键声明（数据，不硬编码）：
      key_fact[attr]     {path, obj_type} → 取该对象在该 path 上的**实测事实值**
      key_id_pattern     具名捕获组 → 从 obj_id 的 local 段解析（如 `tbl-<schema>.<relname>`）
      key_attr（单数）   向后兼容写法：键值 = local 段本身（Session/Query 仍是这条路径）
    """
    key_attrs = spec.get("key_attrs")
    if not key_attrs:
        return {(spec.get("key_attr") or "id"): local}

    key_fact = spec.get("key_fact") or {}
    pat = spec.get("key_id_pattern")
    m = re.match(pat, local) if pat else None
    if pat and not m:
        raise FreezeError(
            "★ auto_objects.%s 的 key_id_pattern 匹配不上 obj_id 的 local 段：pattern=%r local=%r"
            "（对象身份无法解析 ⇒ fail-closed，不猜、不回落）" % (type_code, pat, local))

    key = {}
    for attr in key_attrs:
        fspec = key_fact.get(attr)
        if fspec:
            path = fspec.get("path")
            val = fact_index.get((path, local))
            if val is None or str(val).strip() == "":
                raise FreezeError(
                    "★ auto_objects.%s 的 key_attr `%s` 声明取自事实 %r，但该对象（local=%r）没有这条事实"
                    "（缺失或空值）。契约要求键属性齐全（shared/model/topology.yaml），"
                    "缺键合表会静默合并不同实体 ⇒ fail-closed：不回退成单键、不补空串。"
                    "请检查采集侧是否产出了该 path（上游 gaussdb_probe.py 加列后需重新 gather），"
                    "或在 shared/collect/v3-fact-inventory.json 登记该 path。"
                    % (type_code, attr, path, local))
            key[attr] = str(val).strip()
            continue
        groups = m.groupdict() if m is not None else {}
        if attr in groups:
            val = groups.get(attr)
            if val is None or str(val).strip() == "":
                raise FreezeError(
                    "★ auto_objects.%s 的 key_attr `%s` 从 obj_id local=%r 解析为空值"
                    "（fail-closed）" % (type_code, attr, local))
            key[attr] = str(val).strip()
            continue
        if len(key_attrs) == 1:
            key[attr] = local
            continue
        raise FreezeError(
            "★ auto_objects.%s 声明了 key_attr `%s`，但既没有 key_fact 也没有"
            " key_id_pattern 的具名捕获组 ⇒ 无法取值（fail-closed：绝不猜）"
            % (type_code, attr))
    return key


def ensure_objects(cont, idr, objects, relations, pools, observations, captured_at,
                   fact_index=None):
    """给观测里出现、但还没有对象的 Session/Query/Table 补建对象（map 的 auto_objects 块）。

    上游只发事实不发对象表；不补建，L3 的会话/查询/表级观测会全部被
    observation_on_missing_object 丢掉，图谱就只剩集群与主机。

    ★ M2.11：key 由 auto_object_key() 按 map 声明现算（支持多键，缺键 ⇒ FreezeError）。
    """
    if fact_index is None:
        fact_index = {}
    for o in observations:
        oid = o["objectId"]
        if oid in objects:
            continue
        prefix, _, local = oid.partition(":")
        tname = idr.type_by_prefix.get(prefix)
        spec = cont.auto_object_spec(tname) if tname else None
        if not spec:
            continue                     # 未声明 ⇒ 留给 observation_on_missing_object 记账
        objects[oid] = {
            "id": oid, "type": tname,
            "layer": cont.type_layer.get(tname, "Data"),
            "key": auto_object_key(cont, tname, spec, local, fact_index),
            "attrs": {},
            "provenance": {"source": "derived", "probe": "auto_objects/%s" % tname,
                           "collectedAt": captured_at},
        }
    # ★ M1.8a：会话→池的挂边**不在这里**（旧实现是 `if len(pools) == 1:`，真机 2 池 ⇒ 一条边都不建）。
    #   归因改为有证据的显式规则，见 pool_objects.session_attribution 与 attribute_sessions()。
    return objects, relations


def _dotted(obj, path):
    """按 `a.b.c` 取嵌套字典里的值（映射表里声明的取值路径，不做别名归一）。"""
    cur = obj
    for part in str(path or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def attribute_sessions(cont, idr, gather, objects, relations, pools, drop):
    """HAS_SESSION 建边：把会话归到「本次采集所连实例」的会话池（map 的 pool_objects.session_attribution）。

    ★ M1.8a 修的是什么：旧实现 `if len(pools) == 1:`（freeze.py v1.2.0 的 :735）在真机两池时
      一条 HAS_SESSION 都不建 ⇒ L3 深链 `Pool → Session` 整条缺失、19 个 Session 全孤立（只读复核实证）。
    ★ 依据（为什么会话只可能属于所连实例）：一次 /tools/gather = 一个探针连接 = 一个 DSN，
      探针只可能看到它所连实例上的会话；所连实例由**采集面自报的拓扑**现算。
      外部佐证（只读，不进本函数）：旧载体机 的 `.env` 里 `GAUSSDB_DSN` 的 host = 192.0.2.12
      = ip_index 的 dn_6001/ Primary / is_primary=true。
    ★ 规则全在 map：本函数只实现 evidence_order 里**具名的**证据读取器，不猜类型/角色、不读 .env、不挑池。
    ★ 归因不到 ⇒ **不挂边**，按 unattributed_reason 记账（宁可缺边，不可错边）。

    返回 (证据字典, 本次建出的关系三元组集合)；证据字典写进 CCF `x.sessionAttribution`。
    """
    sessions = sorted(oid for oid in objects if objects[oid].get("type") == "Session")
    pool_spec = cont.pool_spec() or {}
    rel_code = pool_spec.get("session_relation") or "HAS_SESSION"
    spec = pool_spec.get("session_attribution") or {}
    if not pools:                       # 没有池（拓扑缺实例）⇒ 无可归因对象，也无需记账
        return None, set()
    if not spec:
        raise FreezeError(
            "pool_objects.session_attribution 缺失：有 %d 个会话池时必须有归因规则 —— "
            "否则「会话属于哪个池」只能靠猜（M1.8a 前的 `len(pools) == 1` 守卫即此错）" % len(pools))

    ev = {"strategy": spec.get("strategy"), "sessionRelation": rel_code,
          "pools": len(pools), "sessions": len(sessions)}
    target_raw = _dotted(gather, spec.get("target_from"))
    ev["probeTargetIp"] = target_raw

    evidence = spec.get("evidence_order")
    if not isinstance(evidence, list) or not evidence:
        raise FreezeError("pool_objects.session_attribution.evidence_order 必须是非空列表")

    target_oid, basis = None, None
    for rule in evidence:
        use = (rule or {}).get("use")
        if use == "ip_index_is_primary":
            info = idr.ip_index.get(str(target_raw or "")) or {}
            nid = info.get("node_id")
            if nid and info.get("is_primary") is True:
                target_oid, basis = idr.node(nid), rule.get("basis")
                ev["ipIndex"] = {"nodeId": nid, "nodeRole": info.get("node_role"),
                                 "isPrimary": True}
        elif use == "node_role_prefix":
            pfx = str(rule.get("role_prefix") or "")
            for n in ((gather.get("topology") or {}).get("nodes") or []):
                if str(n.get("host_ip") or "") != str(target_raw or ""):
                    continue
                if pfx and str(n.get("node_role") or "").lower().startswith(pfx):
                    target_oid = idr.node(str(n.get("node_id") or ""))
                    basis = rule.get("basis")
                    ev["nodeRole"] = {"nodeId": n.get("node_id"), "nodeRole": n.get("node_role")}
        else:
            raise FreezeError("session_attribution.evidence_order 的 use=%r 非法"
                              "（只认 ip_index_is_primary / node_role_prefix）" % (use,))
        if target_oid:
            break

    parent_type = spec.get("require_parent_type")
    if target_oid and parent_type and (objects.get(target_oid) or {}).get("type") != parent_type:
        target_oid = None
    ev["targetInstance"] = target_oid
    ev["basis"] = basis if target_oid else (spec.get("unattributed_basis") or "unattributed")
    target_pool = idr.pool(target_oid) if target_oid else None
    if target_pool not in pools:
        target_pool = None
    ev["targetPool"] = target_pool

    made = set()
    if target_pool:
        for oid in sessions:
            relations.add((rel_code, target_pool, oid))
            made.add((rel_code, target_pool, oid))
    else:
        if sessions:
            drop[spec.get("unattributed_reason") or "session_pool_unattributed"] += len(sessions)
        ev["reason"] = "no_probe_target_evidence"
    ev["sessionsAttributed"] = len(made)
    ev["sessionsUnattributed"] = len(sessions) - len(made)
    return ev, made


def fact_index(facts, obj_type, path):
    idx = {}
    for f in facts:
        if str(f.get("obj_type") or "") != str(obj_type or ""):
            continue
        if str(f.get("path") or "") != str(path or ""):
            continue
        try:
            idx[str(f.get("obj_id"))] = float(f.get("value"))
        except (TypeError, ValueError):
            continue
    return idx


def ratio_value(rule, num, den):
    try:
        v = float(num) / float(den)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if rule.get("round") is not None:
        v = round(v, int(rule["round"]))
    return v


def apply_derive_metrics(cont, idr, objects, observations, facts):
    """map 的 derive_metrics 块。

    fact_ratio = 上游两条原始事实的比值（例如 free_kb/total_kb）；
    ccf_ratio  = 两条已映射观测的比值（例如 session_count/max_connections）。
    """
    out = []
    by_metric = {}
    for o in observations:
        by_metric.setdefault(o["metricCode"], []).append(o)
    for rule in (cont.map.get("derive_metrics") or []):
        metric, kind = rule.get("metric"), rule.get("kind")
        if metric not in cont.metric_enum:
            raise FreezeError("derive_metrics 条目 metric 非法：%r" % (metric,))
        targets = sorted(v["id"] for v in objects.values() if v["type"] == rule.get("target_type"))
        if not targets:
            continue
        if kind == "fact_ratio":
            nums = fact_index(facts, rule.get("fact_obj_type"), rule.get("num_path"))
            dens = fact_index(facts, rule.get("fact_obj_type"), rule.get("den_path"))
            for raw, num in sorted(nums.items()):
                den = dens.get(raw)
                if not den:
                    continue
                if rule.get("target_type") == "Host":
                    oid = idr.host(raw)
                else:
                    oid = idr.literal(rule.get("target_type"), raw)
                if not oid or oid not in objects:
                    continue
                v = ratio_value(rule, num, den)
                if v is not None:
                    out.append({"objectId": oid, "metricCode": metric, "value": v,
                                "unit": rule.get("unit"), "kind": "number"})
        elif kind == "ccf_ratio":
            den = None
            for o in (by_metric.get(rule.get("den_metric")) or []):
                den = o.get("value")
                if den:
                    break
            if not den:
                continue
            for o in (by_metric.get(rule.get("num_metric")) or []):
                v = ratio_value(rule, o.get("value"), den)
                if v is None:
                    continue
                for tid in targets:
                    out.append({"objectId": tid, "metricCode": metric, "value": v,
                                "unit": rule.get("unit"), "kind": "number"})
        else:
            raise FreezeError("derive_metrics 的 kind 非法：%r" % (kind,))
    return out


def inventory_paths(path):
    """读 v3-fact-inventory.json 的 gaussdb/os 两段，返回 (精确 path 集合, 模板正则列表)。

    模板段（形如 `storage.io_scheduler.{dev}`）必须转成正则：否则运行时的具体设备名
    （sda / nvme0n1）会被误判成上游漂移。
    """
    exact, pats = set(), []
    for layer in ("gaussdb", "os", "app"):
        for r in (load_json(path).get(layer) or []):
            p = str(r.get("path") or "")
            if not p:
                continue
            if "{" in p:
                parts = re.split(r"\{[^}]*\}", p)
                pats.append(re.compile("^" + ".+".join(re.escape(x) for x in parts) + "$"))
            else:
                exact.add(p)
    return exact, pats


# ----------------------------------------------------------------- 主流程

def build(args):
    cont = Contracts(SCHEMA_PATH if not args.schema else args.schema,
                     TOPO_PATH if not args.topology else args.topology,
                     MAP_PATH if not args.map else args.map)

    # ---- 1) /health（采集源回执）----------------------------------
    if args.health_json:
        with io.open(args.health_json, "r", encoding="utf-8") as f:
            raw_health = f.read()
        health = json.loads(raw_health)
    else:
        raw_health = http_text(args.collect_url.rstrip("/") + "/health", timeout=60)
        try:
            health = json.loads(raw_health)
        except ValueError as exc:
            raise FreezeError("/health 不是合法 JSON：%s" % exc)
    health_sha = sha256_text(raw_health)

    cs = health.get("collect_source") or {}
    collect_source = str(cs.get("collect_source") or "live")
    simulated = bool(cs.get("simulated", False))
    unmatched = cs.get("unmatched_exits")
    expect_sim = args.expect_simulated == "true"

    # ---- 2) R10 来源门禁（fail-closed，裁决 6）--------------------
    if unmatched:
        raise FreezeError("collect_source.unmatched_exits=%r 非空 ⇒ 录制不完整，该快照不得进入任何判据（R10 fail-closed）" % (unmatched,))
    if collect_source not in ("live", "record", "replay"):
        raise FreezeError("collect_source=%r 非法" % (collect_source,))
    if simulated != expect_sim:
        raise FreezeError("collect_source.simulated=%s，本次期望 %s（主判据只接受 live）" % (simulated, expect_sim))
    if not expect_sim and collect_source != "live":
        raise FreezeError("主判据要求 collect_source=live，实为 %r" % (collect_source,))
    gdb = health.get("gaussdb") or {}
    if gdb and not gdb.get("ok"):
        raise FreezeError("/health.gaussdb.ok=false（%s）" % str(gdb.get("error"))[:200])

    # ---- 3) /tools/gather（唯一采集入口）--------------------------
    layers = [x.strip() for x in args.layers.split(",") if x.strip()]
    if args.gather_json:
        with io.open(args.gather_json, "r", encoding="utf-8") as f:
            resp = json.load(f)
    else:
        resp = json.loads(http_text(
            args.collect_url.rstrip("/") + "/tools/gather",
            payload={"layers": layers, "sections": [], "verify_sql": "",
                     "cluster_oid": "", "instance_id": ""},
            timeout=args.timeout))
    if not isinstance(resp, dict) or resp.get("mode") != "probes_v6" or "all_facts" not in resp:
        raise FreezeError("gather 回执不是采集面正常响应（可能被 _loop_guard 熔断替换）：keys=%s"
                          % (sorted(resp.keys())[:12] if isinstance(resp, dict) else type(resp).__name__))

    layer_details = resp.get("layer_details") or {}
    layer_errors = norm_multi(resp.get("layer_errors"))
    section_errors = norm_multi(resp.get("section_errors"))

    # ---- 4) H1 症状门禁：每层事实数必须 > 0 -----------------------
    if int(resp.get("total_facts") or 0) <= 0:
        raise FreezeError("★ total_facts=0：采集层整体返回空。H1 形态（探针连接目标为 None/127.0.0.1 时静默 0 事实而 /health 仍报 ok=true）")
    for L in layers:
        d = layer_details.get(L) or {}
        n = int(d.get("fact_count") or 0)
        if n <= 0:
            raise FreezeError("★ 层 %s 事实数为 %d ⇒ 拒绝写出（H1：症状静默但该层整层为空）"
                              "；/health.gaussdb.ok=%s（注意 /health 走 collect_api 自己的 DSN，不能证明探针正常）"
                              % (L, n, gdb.get("ok")))
    for L, e in layer_errors.items():
        raise FreezeError("layer_errors[%s]=%s" % (L, e))
    for k, e in section_errors.items():
        raise FreezeError("section_errors[%s]=%s" % (k, e))

    facts, dup = dedupe_and_sort(resp.get("all_facts") or [])

    # ---- 4b) 上游漂移门禁（H18：线上产物 ≠ 本地检出）-------------
    if args.inventory and not args.allow_upstream_drift:
        inv = args.inventory if os.path.isabs(args.inventory) else os.path.join(ROOT, args.inventory)
        if os.path.isfile(inv):
            exact, pats = inventory_paths(inv)
            seen = {str(f.get("path") or "") for f in facts}
            drift = sorted(p for p in seen
                           if p and p not in exact and not any(rx.match(p) for rx in pats))
            if drift:
                raise FreezeError(
                    "★ 上游采集层事实清单漂移（docs/02 §7 H18：线上部署产物 ≠ 本地检出）：%d 条 path 不在 %s 的清单里：%s。"
                    "确属上游新增时先更新清单再冻结（--allow-upstream-drift 仅供排查）"
                    % (len(drift), args.inventory, drift[:10]))

    # ---- 5) 映射 --------------------------------------------------
    idr = IdResolver(cont, resp)
    drop = Counter()
    examples = []
    objects = {}
    relations = set()
    derived = []
    _, _, pools = derive_topo_objects(cont, idr, resp, objects, relations, derived)
    app_made = derive_app_objects(cont, idr, resp, objects, relations, derived)

    obs_seed = []
    for f in facts:
        obs, _ = map_fact(f, cont, idr, drop, examples)
        if obs:
            obs_seed.append(obs)

    # 同一 (objectId, metricCode) 出现多次 ⇒ 保留排序后的第一条，其余记账
    obs_seed.sort(key=lambda o: (o["objectId"], o["metricCode"], canon_value(o.get("value"))))
    seen = set()
    final_obs = []
    for o in obs_seed:
        k = (o["objectId"], o["metricCode"])
        if k in seen:
            drop["duplicate_metric"] += 1
            if len(examples) < 50:
                examples.append("%s %s (duplicate_metric)" % (o["objectId"], o["metricCode"]))
            continue
        seen.add(k)
        final_obs.append(o)

    captured_at = str(resp.get("collected_at") or utc_now())

    # 补建 Session/Query/Table 对象（否则这些观测会被下面那段全部丢弃）
    # ★ 顺序要紧：必须先补建再过滤，否则「对象不存在」的观测会先被删掉，补建就永远看不到它们。
    # ★ M2.11：补建 Table 需要它的 key 事实（table.schema_name），而该 path 在映射表里是 drop
    #   ⇒ 不会变成观测；所以从**原始事实**建索引传进去（缺键 ⇒ auto_object_key 抛 FreezeError）。
    ensure_objects(cont, idr, objects, relations, pools, final_obs, captured_at,
                   build_fact_index(cont, facts))

    # ★ M1.8a：会话 → 会话池的归因建边（HAS_SESSION）。规则在 map 的 pool_objects.session_attribution，
    #   这里只调用；归因依据（basis/ipIndex/目标实例与池/条数）写进 x.sessionAttribution 供人复核。
    session_attr, session_attr_rels = attribute_sessions(
        cont, idr, resp, objects, relations, pools, drop)

    # 观测引用的对象必须存在（否则 G5/X7 会拒绝）
    for o in list(final_obs):
        if o["objectId"] not in objects:
            drop["observation_on_missing_object"] += 1
            if len(examples) < 50:
                examples.append("%s %s (observation_on_missing_object)" % (o["objectId"], o["metricCode"]))
            final_obs.remove(o)

    # 由拓扑与比值派生的观测（provenance.source = derived）
    for d in derived + apply_derive_metrics(cont, idr, objects, final_obs, facts):
        if d["objectId"] not in objects:
            continue
        if any(o["objectId"] == d["objectId"] and o["metricCode"] == d["metricCode"]
               for o in final_obs):
            continue
        final_obs.append({
            "objectId": d["objectId"], "metricCode": d["metricCode"], "status": "collected",
            "value": d["value"], "unit": d.get("unit"),
            "provenance": {"source": "derived", "probe": "derive/%s" % d["metricCode"],
                           "collectedAt": captured_at},
        })

    for o in final_obs:
        o.setdefault("provenance", {})
        o["provenance"]["collectedAt"] = captured_at
        o["observedAt"] = captured_at
        o.pop("_kind", None)

    obs_sorted = sorted(final_obs, key=lambda o: (o["objectId"], o["metricCode"]))
    for i, o in enumerate(obs_sorted, 1):
        o["id"] = "obs-%06d" % i

    rel_sorted = sorted(relations, key=lambda r: (r[0], r[1], r[2]))
    attr_basis = (session_attr or {}).get("basis")
    attr_rel_code = (session_attr or {}).get("sessionRelation")
    rel_objs = []
    for i, (rtype, frm, to) in enumerate(rel_sorted, 1):
        # 归因建出的边逐条带归因依据（读者不必猜这条 HAS_SESSION 是怎么来的）
        probe = "topology/_real_topology"
        if attr_basis and (rtype, frm, to) in session_attr_rels:
            probe = "session_attribution/%s" % attr_basis
        rel_objs.append({"id": "rel-%06d" % i, "type": rtype, "from": frm, "to": to,
                         "provenance": {"source": "derived", "probe": probe,
                                        "collectedAt": captured_at}})
    if session_attr is not None:
        session_attr["relationIds"] = sorted(r["id"] for r in rel_objs if r["type"] == attr_rel_code)

    obj_list = []
    for oid in sorted(objects):
        o = objects[oid]
        o["attrs"] = {k: v for k, v in (o.get("attrs") or {}).items() if v is not None}
        o["provenance"] = {"source": "derived", "probe": "topology/_real_topology",
                           "collectedAt": captured_at}
        obj_list.append(o)

    # ---- 6) expected / 覆盖 ---------------------------------------
    prof_name = args.expected_profile
    prof = (cont.map.get("expected_profiles") or {}).get(prof_name)
    if not prof:
        raise FreezeError("v3-ccf-map.yaml 没有 expected_profile=%r" % prof_name)
    expected = {"types": list(prof.get("types") or []),
                "metrics": list(prof.get("metrics") or []),
                "objectsMin": dict(prof.get("objectsMin") or {})}
    # ★ relationsMin 只在 profile 显式声明时写进 expected：老 profile 的 canonicalSha256 必须逐字节不变
    if prof.get("relationsMin"):
        expected["relationsMin"] = {str(k): int(v) for k, v in prof["relationsMin"].items()}

    # ---- 7) 组装 + integrity --------------------------------------
    prov_env = env_fingerprint(args.upstream)
    agg = {"CAP_VALUES": CAP_VALUES, "CAP_OBJS": CAP_OBJS, "MAX_STR": MAX_STR}
    doc = {
        "ccfVersion": "1.0.1",
        "snapshotId": args.snapshot_id if args.snapshot_id != "auto" else make_snapshot_id(),
        "capturedAt": captured_at,
        "collector": {
            "entrypoint": "POST /tools/gather",
            "upstream": args.upstream,
            "layers": layers,
            "collectUrl": args.collect_url,
            "freezer": "shared/collect/freeze.py",
            "mapVersion": str((cont.map.get("meta") or {}).get("version") or "unknown"),
        },
        "payloadPolicy": {
            "CAP_VALUES": CAP_VALUES, "CAP_OBJS": CAP_OBJS, "MAX_STR": MAX_STR,
            "parallel": bool(args.parallel == "true"),
            "aggMode": "perPath",
            "factsTotal": int(resp.get("total_facts") or len(facts)),
            "pathsReturned": int(resp.get("total_facts") or len(facts)),
            "valuesTruncated": int(resp.get("values_truncated") or 0) + int(dup),
        },
        "objects": obj_list,
        "relations": rel_objs,
        "observations": obs_sorted,
        "expected": expected,
        "integrity": {},
        "x": {
            "healthSha256": health_sha,
            "healthGaussdbOk": gdb.get("ok"),
            "collectCache": health.get("collect_cache"),
            "layerDetails": layer_details,
            "rawFacts": len(facts),
            "duplicateFacts": dup,
            "unmappedOk": True,
        },
    }
    if session_attr is not None:
        # ★ M1.8a 的归因依据（放在 x 里：ccf.schema.json 根 additionalProperties=false，
        #   顶层不许加新键；x 允许自由扩展，且不参与 canonicalSha256 规范化）
        session_attr["collectedAt"] = captured_at
        doc["x"]["sessionAttribution"] = session_attr

    integ = {
        "counts": {
            "objects": len(obj_list), "relations": len(rel_objs), "observations": len(obs_sorted),
            "byType": dict(Counter(o["type"] for o in obj_list)),
            "byObservationStatus": dict(Counter(o["status"] for o in obs_sorted)),
        },
        "referenceIntegrity": {},
        "expectedCoverage": {},
        "droppedFacts": {
            "total": int(sum(drop.values())),
            "byReason": dict(drop),
            "examples": examples[:50],
        },
        "collectorProvenance": {
            "collectSource": collect_source,
            "simulated": simulated,
            "unmatchedExits": [],
            "parallel": bool(args.parallel == "true"),
            "agg": dict(agg),
            "healthSha256": health_sha,
            "envFingerprint": prov_env,
        },
        "canonicalSha256": "0" * 64,
    }

    oids = {o["id"] for o in obj_list}
    dang_rel = ["%s" % r["id"] for r in rel_objs if r["from"] not in oids or r["to"] not in oids]
    dang_obs = ["%s" % o["id"] for o in obs_sorted if o["objectId"] not in oids]
    integ["referenceIntegrity"] = {"ok": not (dang_rel or dang_obs),
                                   "danglingRelationEnds": dang_rel,
                                   "danglingObservationRefs": dang_obs}
    by_type = Counter(o["type"] for o in obj_list)
    # ★ 只有 status=collected 才算「观测到了」：notCollected / collectedEmpty 恰恰是
    #    「该项未被观测」的证据（docs/02 §7 三状态语义），不能拿去满足覆盖门禁。
    observed = {o["metricCode"] for o in obs_sorted if o.get("status") == "collected"}
    missing_types = sorted(set(expected["types"]) - set(by_type))
    missing_metrics = sorted(set(expected["metrics"]) - observed)
    under_min = sorted(t for t, mn in expected["objectsMin"].items() if by_type.get(t, 0) < int(mn))
    by_rel = Counter(r["type"] for r in rel_objs)
    under_rel = sorted(c for c, mn in (expected.get("relationsMin") or {}).items()
                       if by_rel.get(c, 0) < int(mn))
    integ["expectedCoverage"] = {
        "ok": not (missing_types or missing_metrics or under_min or under_rel),
        "missingTypes": missing_types, "missingMetrics": missing_metrics,
        "underMinTypes": under_min,
    }
    if expected.get("relationsMin"):
        integ["expectedCoverage"]["underMinRelations"] = under_rel
    doc["integrity"] = integ
    doc["integrity"]["canonicalSha256"] = equivalence.canonical_snapshot_sha(doc)

    # ---- 8) 自检（写出前必须通过）---------------------------------
    equivalence.F = []
    equivalence.check_snapshot(doc, "self", cont.schema, expect_sim)
    errs = [(c, m) for s, c, m in equivalence.F if s == "ERROR"]

    if not integ["expectedCoverage"]["ok"] and not args.force_write:
        errs.append(("G5-coverage",
                     "★ 覆盖门禁未通过，拒绝写出：missingTypes=%s missingMetrics=%s underMinTypes=%s"
                     " underMinRelations=%s。"
                     "（若当前里程碑尚未实现 N1/N2，请改用更窄的 --expected-profile，"
                     "而不要用 --force-write 掩盖。）"
                     % (missing_types or "-", missing_metrics or "-", under_min or "-", under_rel or "-")))
    if errs:
        raise FreezeError("固化自检失败，未写出：\n  - " + "\n  - ".join("%s: %s" % e for e in errs))

    return doc, drop, obj_list, rel_objs, obs_sorted


def env_fingerprint(upstream):
    """H1/H10/H13 的机械处置：把两个 .env 的位置与内容 hash 记进 CCF（不记明文）。"""
    if not upstream or not os.path.isdir(upstream):
        return None
    parts = []
    for rel in (".env", os.path.join("service", ".env")):
        p = os.path.join(upstream, rel)
        if os.path.isfile(p):
            parts.append("%s=%s" % (rel.replace("\\", "/"), (file_sha256(p) or "")[:16]))
    return ";".join(parts) if parts else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="CCF 固化器（fail-closed）")
    ap.add_argument("--collect-url", default=os.environ.get("GDO_COLLECT_URL", "http://127.0.0.1:8187"))
    ap.add_argument("--layers", default="gaussdb,os")
    ap.add_argument("--parallel", default="true", choices=["true", "false"])
    ap.add_argument("--expected-profile", default="l3-only")
    ap.add_argument("--expect-simulated", default="false", choices=["true", "false"])
    ap.add_argument("--snapshot-id", default="auto")
    ap.add_argument("--out", default=None)
    ap.add_argument("--map", default=None)
    ap.add_argument("--schema", default=None)
    ap.add_argument("--topology", default=None)
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("--inventory", default="shared/collect/v3-fact-inventory.json",
                    help="上游事实清单；用于 H18 漂移门禁（默认仓库内清单）")
    ap.add_argument("--allow-upstream-drift", action="store_true",
                    help="★ 仅用于排查：允许出现清单外 path（写出的快照不得进入评测）")
    ap.add_argument("--health-json", default=None)
    ap.add_argument("--gather-json", default=None)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-write", action="store_true",
                    help="★ 仅用于排查：覆盖门禁未过也写出快照（写出的快照不得进入评测）")
    args = ap.parse_args(argv)

    try:
        doc, drop, objs, rels, obs = build(args)
    except FreezeError as exc:
        print("FREEZE REFUSED: %s" % exc)
        return 2

    print("snapshotId      = %s" % doc["snapshotId"])
    print("objects         = %d %s" % (len(objs), doc["integrity"]["counts"]["byType"]))
    print("relations       = %d" % len(rels))
    print("observations    = %d %s" % (len(obs), doc["integrity"]["counts"]["byObservationStatus"]))
    print("expectedCoverage= %s" % doc["integrity"]["expectedCoverage"])
    print("droppedFacts    = %d %s" % (doc["integrity"]["droppedFacts"]["total"], dict(drop)))
    print("canonicalSha256 = %s" % doc["integrity"]["canonicalSha256"])
    if drop:
        print("drop examples   = %s" % doc["integrity"]["droppedFacts"]["examples"][:8])

    if args.dry_run or not args.out:
        print("DRY-RUN：未写出（--out 未给或 --dry-run）")
        return 0
    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    with io.open(out, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("WRITTEN %s (%d bytes)" % (out, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())