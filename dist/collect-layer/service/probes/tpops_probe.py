"""GaussDB 运维 Agent — TPOPS 层采集探针

通过 TPOPS (GaussDB 运维管理平台) 北向 API 采集：
  - 实例列表 / 集群拓扑
  - 告警记录（TPOPS 核心能力）
  - 慢SQL / TOP SQL
  - 实时会话
  - 操作日志/变更记录
  - 性能监控指标

API 基础（从 legacy tpops_collector.py 移植并增强）：
  认证：POST /rds/v3/auth/mini-token → X-Mini-Token JWT
  实例：GET  /rds/v3/instances
  告警：POST /rds/v3/alarm/records
  慢SQL：POST /rds/gaussdb/v3/instances/{id}/slow-sql-list
  TOP SQL：POST /rds/v3/instances/{id}/top-sqls
  会话：POST /rds/gaussdb/v3/instances/{id}/real-time-session
  指标：POST /rds/v3/monitor/metrics
  操作日志：POST /rds/v3/operation/logs（推断路径，需确认）

数据来源: TPOPS RESTful API → GaussDB 运维管理平台
采集方式: HTTP/HTTPS 请求（urllib/requests）
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Any, Dict, List, Optional, Tuple

from . import BaseProbe, ProbeResult, register_probe

# ★ 模拟演练打桩（docs/60）。live 下所有 tap 都是**零介入**的直通。
try:
    from .. import collect_sim as _sim
except Exception:
    try:
        import collect_sim as _sim
    except Exception:
        _sim = None


# ── 默认配置（.env 优先） ──
_DEFAULT_TPOPS_BASE = os.environ.get("TPOPS_BASE", "https://192.0.2.17:8002")
_DEFAULT_TPOPS_USER = os.environ.get("TPOPS_USER", "admin")
_DEFAULT_TPOPS_PASS = os.environ.get("TPOPS_PASSWORD", "")


class TPopsUnavailable(Exception):
    """TPOPS 不可达/认证失败"""
    pass


# ── 全局 SSL 上下文 ──
_CTX: Optional[ssl.SSLContext] = None


def _get_ssl_ctx() -> ssl.SSLContext:
    global _CTX
    if _CTX is None:
        warnings.filterwarnings("ignore")
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _CTX = c
    return _CTX


def _http(url: str, method: str = "GET", body: Optional[dict] = None,
          jwt: Optional[str] = None, timeout: int = 15) -> Tuple[int, Any]:
    """同步 HTTP 请求 TPOPS，返回 (状态码, 解析后的响应)"""
    hd = {"Content-Type": "application/json", "Accept": "application/json"}
    if jwt:
        hd["X-Mini-Token"] = jwt
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hd, method=method)

    def _real() -> Tuple[int, Any]:
        try:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=timeout)
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            try:
                detail = json.loads(detail)
            except (json.JSONDecodeError, TypeError):
                pass
            raise TPopsUnavailable(f"TPOPS {method} {url} -> HTTP {e.code}: {detail}") from e
        except (ConnectionError, TimeoutError, urllib.error.URLError) as e:
            raise TPopsUnavailable(f"TPOPS 不可达 {url}: {e}") from e

    # ★ 模拟演练打桩（docs/60）：TPOPS HTTP 出口。
    #   replay 下**不发起任何真实请求**；未命中录制时由 tap 按 HTTP 语义给 404/{}。
    if _sim is None:
        return _real()
    _fp_key = _sim.fingerprint_http(method, url, body)
    r = _sim.http(method, url, _real, body=body,
                  kind="tpops")
    status, payload = r if isinstance(r, tuple) else (200, r)
    # 把"未命中录制"记为该 TPopsUnavailable，与真实 HTTP 错误同路 ——
    # 避免演练模式下静默拿到空数据（fail-closed 的第 1 条）
    if status == 404 and _sim.mode() == "replay":
        raise TPopsUnavailable(
            "演练模式：TPOPS 出口无录制数据（%s %s，key=%s）。"
            "请补录该出口或在场景中声明其为空。" % (method, url, _fp_key))
    if status >= 400:
        raise TPopsUnavailable(f"TPOPS {method} {url} -> HTTP {status}")
    return status, payload


class TpopsProbe(BaseProbe):
    """TPOPS 层采集探针"""

    def __init__(self, base: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None):
        self._base = (base or _DEFAULT_TPOPS_BASE).rstrip("/")
        self._user = username or _DEFAULT_TPOPS_USER
        self._pass = password or _DEFAULT_TPOPS_PASS
        self._jwt: Optional[str] = None
        self._instance_cache: List[Dict] = []

    @property
    def identity(self) -> str:
        return "tpops"

    # ── 认证 ──

    def login(self) -> str:
        """POST /rds/v3/auth/mini-token → X-Mini-Token JWT"""
        # 用较低层的 HTTPSConnection 拿 response headers
        import http.client as _hc

        u = urllib.parse.urlparse(self._base)
        _login_url = "%s/rds/v3/auth/mini-token" % self._base.rstrip("/")
        _login_body = {"username": self._user, "password": self._pass}

        def _real_login():
            """真实登录：返回 (status, {headers:{...}, body:{}}) —— 必须可序列化"""
            conn = _hc.HTTPSConnection(
                u.hostname, u.port or 8002,
                context=_get_ssl_ctx(), timeout=10,
            )
            try:
                conn.request(
                    "POST", "/rds/v3/auth/mini-token",
                    json.dumps(_login_body).encode(),
                    {"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                resp.read()  # 丢弃 body, header 里取 token
                hdrs = {k.lower(): v for k, v in resp.getheaders()}
                return resp.status, {"headers": hdrs}
            finally:
                conn.close()

        # ★ 模拟演练打桩（docs/60）：登录是 TPOPS 的**第二个出口**（裸 http.client）。
        #   漏打桩会让 replay 真的去连平台 —— 网络守卫虽会拦，但更该在此打桩。
        if _sim is None:
            _status, payload = _real_login()
        else:
            _r = _sim.http("POST", _login_url, _real_login, body=_login_body, kind="tpops_login")
            _status, payload = _r if isinstance(_r, tuple) else (200, _r)
        if _status >= 400:
            raise TPopsUnavailable("TPOPS 登录失败 HTTP %s（%s）" % (_status, _login_url))
        for k, v in ((payload or {}).get("headers") or {}).items():
            if k.lower() == "x-mini-token":
                self._jwt = v
                break

        if not self._jwt:
            raise TPopsUnavailable("TPOPS 登录成功但未返回 X-Mini-Token（密码可能错误）")
        return self._jwt

    def _ensure_jwt(self) -> str:
        if not self._jwt:
            self.login()
        return self._jwt

    def _req(self, method: str, path: str, body: Optional[dict] = None,
             timeout: int = 15) -> Any:
        jwt = self._ensure_jwt()
        url = f"{self._base}{path}"
        _, data = _http(url, method, body, jwt, timeout)
        return data

    # ── 公共方法 ──

    def collect(self, **kwargs) -> ProbeResult:
        """执行 TPOPS 采集

        Args:
            fetch_instances: 是否采集实例列表（默认 True）
            fetch_alarms:    是否采集告警（默认 True）
            fetch_slow_sql:  是否采集慢SQL（默认 False，需指定 instance_id）
            instance_id:     慢SQL/TOP SQL 所属实例 ID

        Returns:
            ProbeResult
        """
        start = time.time()
        raw: Dict[str, Any] = {}
        all_facts: List[dict] = []
        alerts: List[dict] = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        src = "tpops"

        try:
            self._ensure_jwt()
            instance_id = kwargs.get("instance_id", "")

            # 0) 节点 IP ↔ DN 映射（来自 .env 的 GAUSSDB_NODES / GAUSSDB_CLUSTER_ID）
            #    用户常按 IP 提问（如「192.0.2.12 有什么问题」），但 TPOPS / Dorado / 告警
            #    数据只带实例 ID 和 DN 组件 ID，不含 IP —— 这里显式补出映射，使按 IP 的提问可定位。
            #    连接键是 DN 组件 id（dn_6001…）：GAUSSDB_NODES 形如
            #      ip:port:dn_id:type:alias,...
            cluster_id = (os.environ.get("GAUSSDB_CLUSTER_ID") or "").strip()
            nodes_env = (os.environ.get("GAUSSDB_NODES") or "").strip()
            dn2ip: Dict[str, str] = {}
            node_ips: List[str] = []
            if nodes_env:
                for item in nodes_env.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    parts = [x.strip() for x in item.split(":")]
                    nip = parts[0] if parts else ""
                    if not nip:
                        continue
                    nport = parts[1] if len(parts) > 1 else ""
                    ndn = parts[2] if len(parts) > 2 else ""
                    ntype = parts[3] if len(parts) > 3 else "DN"
                    nalias = parts[4] if len(parts) > 4 else ""
                    node_ips.append(nip)
                    if ndn:
                        dn2ip[ndn] = nip
                    oid = "node:" + nip
                    all_facts.append(self._make_fact("tpops.node.ip", nip, oid, "node", src, ts))
                    all_facts.append(self._make_fact("tpops.node.type", ntype, oid, "node", src, ts))
                    if nport:
                        all_facts.append(self._make_fact("tpops.node.port", nport, oid, "node", src, ts))
                    if ndn:
                        all_facts.append(self._make_fact("tpops.node.dn_id", ndn, oid, "node", src, ts))
                    if nalias:
                        all_facts.append(self._make_fact("tpops.node.dn_alias", nalias, oid, "node", src, ts))
                    if cluster_id:
                        all_facts.append(self._make_fact("tpops.node.instance_id", cluster_id, oid, "node", src, ts))
            if cluster_id:
                all_facts.append(self._make_fact("tpops.cluster.id", cluster_id, cluster_id, "instance", src, ts))
            if node_ips:
                all_facts.append(self._make_fact("tpops.cluster.node_ips",
                                                 ",".join(node_ips), cluster_id or "cluster",
                                                 "instance", src, ts))

            # 1) 实例列表（仅本集群 —— GAUSSDB_CLUSTER_ID 指定）
            #    背景：TPOPS 一台服务器上可能有多个 GaussDB 实例。GaussDB 层与 OS 层采集
            #    都只针对本集群节点，TPOPS 层若把其他实例一起采进来，会造成：
            #      a) 其他实例的告警（如 DN Failover）被误当作本集群问题归因；
            #      b) facts 数量膨胀、无关信息稀释。
            #    故这里显式按 GAUSSDB_CLUSTER_ID 过滤，非本集群实例只记数量不采明细。
            if kwargs.get("fetch_instances", True):
                try:
                    lst = self._req("GET", "/rds/v3/instances")
                    all_insts = lst.get("instances", [])
                    if cluster_id:
                        mine = [i for i in all_insts if i.get("id") == cluster_id]
                        others = [i for i in all_insts if i.get("id") != cluster_id]
                        raw["instances"] = mine
                        raw["instances_skipped"] = [
                            {"id": i.get("id", ""), "name": i.get("name", "")} for i in others
                        ]
                        if others:
                            all_facts.append(self._make_fact(
                                "tpops.cluster.skipped_instance_count", len(others),
                                cluster_id, "instance", src, ts))
                            all_facts.append(self._make_fact(
                                "tpops.cluster.skipped_instance_names",
                                ",".join(i.get("name", "") for i in others),
                                cluster_id, "instance", src, ts))
                    else:
                        raw["instances"] = all_insts
                    self._instance_cache = raw["instances"]
                    for inst in raw["instances"]:
                        iid = inst.get("id", "")
                        all_facts.append(self._make_fact("tpops.instance.id", iid, iid, "instance", src, ts))
                        all_facts.append(self._make_fact("tpops.instance.status",
                                                         inst.get("instance_status", ""), iid, "instance", src, ts))
                        all_facts.append(self._make_fact("tpops.instance.name",
                                                         inst.get("name", ""), iid, "instance", src, ts))

                        # 获取节点组件
                        try:
                            comp = self._req("GET", f"/rds/gaussdb/v3/instances/{iid}/components")
                            raw.setdefault("components", {})[iid] = comp
                            for node in (comp.get("nodes") or []):
                                nid = node.get("id", "")
                                all_facts.append(self._make_fact("tpops.node.id", nid, nid, "node", src, ts))
                                if iid == cluster_id:
                                    all_facts.append(self._make_fact("tpops.node.instance_id",
                                                                     iid, nid, "node", src, ts))
                                hn = (node.get("hostname") or "").strip()
                                if hn:
                                    all_facts.append(self._make_fact("tpops.node.hostname",
                                                                     hn, nid, "node", src, ts))
                                for c in (node.get("components") or []):
                                    ctype = c.get("type")
                                    if ctype in ("DN", "CN"):
                                        all_facts.append(self._make_fact(
                                            f"tpops.node.{ctype.lower()}_role",
                                            c.get("role", ""), nid, "node", src, ts))
                                        all_facts.append(self._make_fact(
                                            f"tpops.node.{ctype.lower()}_status",
                                            c.get("status", ""), nid, "node", src, ts))
                                    if ctype == "DN":
                                        cid = str(c.get("id") or "").strip()
                                        if cid:
                                            all_facts.append(self._make_fact(
                                                "tpops.node.dn_component_id", cid, nid, "node", src, ts))
                                            # 用 DN 组件 id 关联出 IP（关键：使节点可被 IP 定位）
                                            ip = dn2ip.get(cid)
                                            if ip:
                                                all_facts.append(self._make_fact(
                                                    "tpops.node.ip", ip, nid, "node", src, ts))
                        except TPopsUnavailable:
                            pass
                except TPopsUnavailable:
                    pass

            # 2) 告警记录（仅本集群 + 系统级；标注 scope 便于区分归属）
            #    scope:
            #      self   —— 本集群（instance_id == GAUSSDB_CLUSTER_ID）
            #      system —— 系统级/平台级告警（instance_id 形如 00000000-…-sys），非某个实例，
            #                但可能与本集群共用同一 TPOPS/操作系统，故保留并单独标注
            #      其他实例 —— 直接跳过（不采集、不入 facts、不生成探针告警）
            if kwargs.get("fetch_alarms", True):
                try:
                    alarm_data = self._req("POST", "/rds/v3/alarm/records",
                                           {"page": 1, "size": 50})
                    all_recs = alarm_data.get("records", [])
                    name_of = {i.get("id", ""): i.get("name", "") for i in raw.get("instances", [])}
                    kept: List[dict] = []
                    skipped_n = 0
                    for a in all_recs:
                        iid = a.get("instance_id", "") or ""
                        if cluster_id:
                            if iid == cluster_id:
                                scope = "self"
                            elif iid.startswith("00000000") or iid.endswith("-sys"):
                                scope = "system"
                            else:
                                skipped_n += 1
                                continue
                        else:
                            scope = "self"
                        kept.append(a)

                        sev = a.get("severity", "info")
                        aid = a.get("alarm_id", "")
                        aname = a.get("alarm_name", "")
                        oid = f"alarm:{aid}:{iid}" if iid else f"alarm:{aid}"
                        iname = name_of.get(iid) or ("系统级" if scope == "system" else "")

                        all_facts.append(self._make_fact("tpops.alarm.severity", sev, oid, "alert", src, ts))
                        all_facts.append(self._make_fact("tpops.alarm.status",
                                                         a.get("status", ""), oid, "alert", src, ts))
                        all_facts.append(self._make_fact("tpops.alarm.name", aname, oid, "alert", src, ts))
                        all_facts.append(self._make_fact("tpops.alarm.scope", scope, oid, "alert", src, ts))
                        if iname:
                            all_facts.append(self._make_fact("tpops.alarm.instance_name",
                                                             iname, oid, "alert", src, ts))
                        if iid:
                            all_facts.append(self._make_fact("tpops.alarm.instance_id",
                                                             iid, oid, "alert", src, ts))
                        if a.get("alarm_time"):
                            all_facts.append(self._make_fact("tpops.alarm.time",
                                                             a.get("alarm_time"), oid, "alert", src, ts))

                        # 严重告警 → 探针告警（detail 中明确归属，避免被误认为本集群问题）
                        if sev in ("major", "critical"):
                            who = ("本集群" if scope == "self"
                                   else "系统级（非本实例）")
                            alerts.append({
                                "severity": "critical" if sev == "critical" else "warning",
                                "title": f"TPOPS 告警: {aname}",
                                "detail": f"严重度={sev}, 归属={who}"
                                          + (f", 实例={iname or iid}" if iname or iid else ""),
                                "entity": f"instance:{iid}" if scope == "self" else "tpops:system",
                                "source": "tpops_probe",
                                "timestamp": ts,
                            })
                    raw["alarms"] = kept
                    if skipped_n:
                        raw["alarms_skipped"] = skipped_n
                        all_facts.append(self._make_fact(
                            "tpops.cluster.skipped_alarm_count", skipped_n,
                            cluster_id or "cluster", "instance", src, ts))
                except TPopsUnavailable:
                    pass

            # 3) 慢SQL（需要指定实例 ID）
            if instance_id and kwargs.get("fetch_slow_sql", False):
                try:
                    slow = self._req("POST",
                                     f"/rds/gaussdb/v3/instances/{instance_id}/slow-sql-list",
                                     {"page": 1, "size": 20})
                    raw.setdefault("slow_sqls", {})[instance_id] = slow
                    rows = slow.get("slow_sql_list", slow.get("records", []))
                    for s in rows[:10]:
                        sql_text = (s.get("query") or s.get("sql_text", ""))[:200]
                        all_facts.append(self._make_fact(
                            "tpops.slow_sql.query", sql_text,
                            f"{instance_id}.slow.{s.get('id', '')}",
                            "slow_sql", src, ts))
                except TPopsUnavailable:
                    pass

            # 4) TOP SQL
            if instance_id and kwargs.get("fetch_top_sql", False):
                try:
                    top = self._req("POST",
                                    f"/rds/v3/instances/{instance_id}/top-sqls",
                                    {"page": 1, "size": 10})
                    raw.setdefault("top_sqls", {})[instance_id] = top
                    for t in (top.get("top_sqls", top.get("records", []))):
                        all_facts.append(self._make_fact(
                            "tpops.top_sql.exec_ms",
                            t.get("total_exec_time", t.get("exec_time", 0)),
                            f"{instance_id}.top.{t.get('id', '')}",
                            "top_sql", src, ts))
                except TPopsUnavailable:
                    pass

        except TPopsUnavailable as e:
            return ProbeResult(
                layer="tpops",
                facts=[],
                raw={},
                alerts=[{
                    "severity": "critical",
                    "title": "TPOPS 不可达",
                    "detail": str(e),
                    "entity": "tpops",
                    "source": "tpops_probe",
                    "timestamp": ts,
                }],
                elapsed_ms=self._elapsed(start),
                error=str(e),
            )

        return ProbeResult(
            layer="tpops",
            facts=all_facts,
            alerts=alerts,
            raw=raw,
            elapsed_ms=self._elapsed(start),
        )

    # ── 单独 API 方法（供 CLI 或 gather 直接调用）──

    def list_instances(self) -> List[Dict]:
        """列出所有 GaussDB 实例"""
        data = self._req("GET", "/rds/v3/instances")
        return data.get("instances", [])

    def list_alarms(self, page: int = 1, size: int = 50) -> List[Dict]:
        """获取告警记录"""
        data = self._req("POST", "/rds/v3/alarm/records", {"page": page, "size": size})
        return data.get("records", [])

    def get_slow_sqls(self, instance_id: str, page: int = 1, size: int = 20) -> List[Dict]:
        """获取慢SQL列表"""
        data = self._req("POST",
                         f"/rds/gaussdb/v3/instances/{instance_id}/slow-sql-list",
                         {"page": page, "size": size})
        return data.get("slow_sql_list", data.get("records", []))

    def get_top_sqls(self, instance_id: str, page: int = 1, size: int = 10) -> List[Dict]:
        """获取 TOP SQL"""
        data = self._req("POST",
                         f"/rds/v3/instances/{instance_id}/top-sqls",
                         {"page": page, "size": size})
        return data.get("top_sqls", data.get("records", []))


# ── 注册到全局注册表 ──
register_probe(TpopsProbe())
