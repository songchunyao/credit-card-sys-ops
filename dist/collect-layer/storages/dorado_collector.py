"""
华为OceanStor Dorado DeviceManager RESTful采集探针
用于GaussDB运维Agent本体-存算分离扩展的双通道采集（通道2：DeviceManager REST）

API端点（实测对齐 OceanStor/Dorado 5500 V6 V600R005C27）:
  POST   /deviceManager/rest/xxxxx/sessions    登录获取 iBaseToken + deviceid
  GET    /deviceManager/rest/{deviceid}/system/ 设备基本信息
  GET    /deviceManager/rest/{deviceid}/controller 控制器列表
  GET    /deviceManager/rest/{deviceid}/storagepool 存储池
  GET    /deviceManager/rest/{deviceid}/lun     LUN列表
  GET    /deviceManager/rest/{deviceid}/disk    物理硬盘
  GET    /deviceManager/rest/{deviceid}/host    主机定义
  GET    /deviceManager/rest/{deviceid}/replicationpair 容灾复制对
  GET    /deviceManager/rest/{deviceid}/fc_port  FC端口
  GET    /deviceManager/rest/{deviceid}/lun/associate?ASSOCIATEOBJTYPE=21&ASSOCIATEOBJID={id}  主机映射LUN
  DELETE /deviceManager/rest/{deviceid}/sessions 登出释放会话
"""
import os
import sys
import json
import base64
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

import requests
import urllib3

# ★ 模拟演练打桩（docs/60）。live 下 http() 是**零介入**的直通。
try:
    from service import collect_sim as _sim
except Exception:
    try:
        import collect_sim as _sim
    except Exception:
        _sim = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DoradoAlert:
    """Dorado采集告警数据"""
    id: str
    severity: Severity
    title: str
    detail: str
    entity: str
    source: str = "dorado_collector"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class DoradoCollector:
    """OceanStor Dorado DeviceManager RESTful采集器

    用法:
        with DoradoCollector(base_url, username, password) as collector:
            if collector.login():
                data = collector.full_discovery()
                alerts = collector.diagnose()
                print(json.dumps(collector.summary(), indent=2, ensure_ascii=False))
    """

    # 列表型端点（出错时应回 [] 而非 {}）。
    # ★ 2026-09-16 修复：原先是内联字面量元组，新增端点极易漏加，而漏加**不报错** ——
    #   返回的 `{}` 被下游 `for x in r` 迭代时会遍历 **dict 的键名字符串**，
    #   于是产出"路径对、取值是端点名字"的垃圾事实。提为类常量，新增端点只改一处。
    _LIST_ENDPOINTS = (
        "controller", "disk", "lun", "storagepool", "host",
        "replicationpair", "fc_port",
        # 2026-09-16 NoF（NVMe over RoCE）建模新增
        "eth_port", "host_link",
    )

    def __init__(self, base_url: str, username: str, password: str, verify_ssl: bool = False):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._session = requests.Session()
        self._session.verify = verify_ssl
        self._device_id: Optional[str] = None
        self._token: Optional[str] = None
        self._authenticated = False
        # 采集缓存：full_discovery 填充后供 diagnose/summary 复用
        self._cache: Dict[str, Any] = {}

    # ============================================================
    # 会话管理
    # ============================================================

    def login(self) -> bool:
        """登录DeviceManager获取token

        POST /deviceManager/rest/xxxxx/sessions
        body: {"username": ..., "password": ..., "scope": 0}
        从响应提取 iBaseToken 和 deviceid
        """
        try:
            _login_url = f"{self.base_url}/deviceManager/rest/xxxxx/sessions"
            _login_body = {"username": self.username, "password": self.password, "scope": 0}
            if _sim is not None:
                # ★ 模拟演练打桩（docs/60）：登录是 DeviceManager 的第二个出口，
                #   漏打桩会让 replay 真的去连设备（网络守卫会拦，但更该在此打桩）。
                resp = _sim.http_requests(
                    lambda: self._session.post(_login_url, json=_login_body, timeout=30),
                    "POST", _login_url, body=_login_body, key_hint="sessions")
            else:
                resp = self._session.post(
                    f"{self.base_url}/deviceManager/rest/xxxxx/sessions",
                    json={"username": self.username, "password": self.password, "scope": 0},
                    timeout=30,
                )
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", {})
            device_id = data.get("deviceid")
            token = data.get("iBaseToken")
            if not device_id or not token:
                logger.error("登录失败: 响应缺少 deviceid 或 iBaseToken: %s", body.get("error"))
                return False
            self._device_id = device_id
            self._token = token
            self._session.headers["iBaseToken"] = token
            self._authenticated = True
            logger.info("DeviceManager 登录成功, deviceid=%s", device_id)
            return True
        except requests.RequestException as e:
            logger.error("登录请求失败: %s", e)
            return False
        except (ValueError, json.JSONDecodeError) as e:
            logger.error("登录响应解析失败: %s", e)
            return False

    def logout(self) -> bool:
        """登出DeviceManager释放会话

        DELETE /deviceManager/rest/{deviceid}/sessions
        """
        if not self._authenticated or not self._device_id:
            return False
        try:
            # ★ 模拟演练打桩（docs/60）：登出是 DeviceManager 的**第三个出口**
            #   （`_request` / `login` / `logout`）—— 漏打桩时 replay 会真去连设备，
            #   被探针的 `except Exception` 吞掉后**静默退化成 local_disk 模式**
            #   （实测踩过：存储层 0 事实，且无任何报错）。故必须打桩。
            _lo_url = f"{self.base_url}/deviceManager/rest/{self._device_id}/sessions"
            if _sim is not None:
                resp = _sim.http_requests(
                    lambda: self._session.delete(_lo_url, timeout=30),
                    "DELETE", _lo_url, key_hint="sessions")
            else:
                resp = self._session.delete(
                    f"{self.base_url}/deviceManager/rest/{self._device_id}/sessions",
                    timeout=30,
                )
            resp.raise_for_status()
            self._authenticated = False
            self._device_id = None
            self._token = None
            logger.info("DeviceManager 登出成功")
            return True
        except requests.RequestException as e:
            logger.warning("登出请求失败(会话可能已超时): %s", e)
            self._authenticated = False
            return False

    def _ensure_auth(self):
        """确保已认证，未认证时自动登录"""
        if not self._authenticated:
            ok = self.login()
            if not ok:
                raise RuntimeError("DeviceManager 未登录，自动登录失败")

    def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """带token验证的HTTP请求

        自动拼接 URL: {base_url}/deviceManager/rest/{device_id}/{endpoint}
        自动调用 _ensure_auth()
        自动处理HTTP错误
        返回 JSON 的 data 字段
        容错：遇到任何异常返回空dict或空list
        """
        try:
            self._ensure_auth()
            # 确保 timeout 存在
            kwargs.setdefault("timeout", 30)
            url = f"{self.base_url}/deviceManager/rest/{self._device_id}/{endpoint.lstrip('/')}"
            if _sim is not None:
                # ★ 模拟演练打桩（docs/60）：Storage/REST 出口。
                #   用 http_requests（而非低层 http）—— 录制不能存 requests.Response，
                #   它会在 record 时归一成 (status, json)，replay 时组装回替身响应。
                resp = _sim.http_requests(
                    lambda: self._session.request(method, url, **kwargs),
                    method, url, body=kwargs.get("json"), key_hint=endpoint)
            else:
                resp = self._session.request(method, url, **kwargs)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data")
            return data
        except requests.RequestException as e:
            logger.warning("请求失败 [%s %s]: %s", method, endpoint, e)
            return [] if endpoint.startswith(self._LIST_ENDPOINTS) else {}
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("响应解析失败 [%s %s]: %s", method, endpoint, e)
            return [] if endpoint.startswith(self._LIST_ENDPOINTS) else {}
        except RuntimeError as e:
            logger.warning("请求前认证失败 [%s %s]: %s", method, endpoint, e)
            return [] if endpoint.startswith(self._LIST_ENDPOINTS) else {}

    # ============================================================
    # 全量采集
    # ============================================================

    def collect_system(self) -> dict:
        """GET /system/ — 设备基本信息

        返回: {NAME, ID, HEALTHSTATUS, RUNNINGSTATUS, TOTALCAPACITY,
               PRODUCTVERSION, wwn, productModeString, ...}
        """
        return self._request("GET", "system/")

    def collect_controllers(self) -> list:
        """GET /controller — 控制器健康/CPU/内存

        每条: {ID, CPUUSAGE, MEMUSAGE, HEALTHSTATUS, ISMASTER, CPUINFO, BIOSVER}
        """
        return self._request("GET", "controller")

    def collect_storage_pool(self) -> list:
        """GET /storagepool — 存储池详情

        每条: {ID, NAME, USERTOTALCAPACITY, USERCONSUMEDCAPACITY,
               USERFREECAPACITY, HEALTHSTATUS, RUNNINGSTATUS, ...}
        """
        return self._request("GET", "storagepool")

    def collect_luns(self) -> list:
        """GET /lun — 全量LUN列表

        每条: {ID, NAME, WWN, CAPACITY, HEALTHSTATUS, PARENTNAME, CTRL, ...}
        """
        return self._request("GET", "lun")

    def collect_disks(self) -> list:
        """GET /disk — 物理硬盘清单

        每条: {ID, LOCATION, MODEL, HEALTHSTATUS, TEMPERATURE, ...}
        """
        return self._request("GET", "disk")

    def collect_hosts(self) -> list:
        """GET /host — 主机定义列表（含未命名FC主机）

        每条: {ID, NAME, HEALTHSTATUS, ...}
        """
        return self._request("GET", "host")

    def collect_replication_pairs(self) -> list:
        """GET /replicationpair — 容灾复制对

        每条: {ID, LOCALRESNAME, LOCALRESID, REMOTERESNAME, REMOTERESID,
               REMOTEDEVICENAME, REMOTEDEVICESN, HEALTHSTATUS, RUNNINGSTATUS,
               REPLICATIONPROGRESS, REPLICATIONMODEL, RECOVERYPOLICY}
        """
        return self._request("GET", "replicationpair")

    def collect_fc_ports(self) -> list:
        """GET /fc_port — FC端口信息"""
        return self._request("GET", "fc_port")

    # ── NoF（NVMe over RoCE）端点（2026-09-16 新增）──────────────────
    # 为什么需要：现场是 NVMe over RoCE 而非 FC，阵列侧端口从未采集
    # （full_discovery 只调 fc_port，FC 时代遗留）⇒ 用户问「控制器端口与节点
    # 多路径怎么连」时本体侧无从作答。这两个端点是实测可用的：
    #   · /port  /ether*/roce_port/host_port 全 404，真正可用的是 eth_port
    #   · host_link 是阵列自己维护的「主机↔端口」链路表，是配对关系的**唯一来源**
    #     （eth_port 的 IPV4ADDR 在 4 个数据口上全为空，不能作为关联键）

    def collect_eth_ports(self) -> list:
        """GET /eth_port — 以太端口清单

        实测字段: LOCATION(CTE0.A.IOM3.P1) NAME SPEED RUNNINGSTATUS HEALTHSTATUS
                 OWNINGCONTROLLER(0A) numberOfInitiators IPV4ADDR MACADDRESS MTU
        ⚠️ 数据口(USEDTYPE=0 且 numberOfInitiators>0) 的 IPV4ADDR 为空 ——
           阵列的 RoCE 目标由 TARGET_PORT_WWN 表达，不要用 IPV4ADDR 做关联。
        """
        return self._request("GET", "eth_port")

    def collect_host_links(self) -> list:
        """GET /host_link — 主机↔端口链路表（每个 initiator×target 一条）

        实测字段: PARENTNAME(阵列登记主机名，**未登记则为空**) hostIP
                 TARGET_PORT_WWN(目标 IP) INITIATOR_PORT_WWN(initiator NQN)
                 port(阵列端口 LOCATION) CTRL_ID(0A/0B) HEALTHSTATUS
                 RUNNINGSTATUS(27=正常) ultraPathVersion ID
        实测规模: 24 条 = 12 条具名主机 + 12 条未具名（hostIP 落在 target 网段，
                 非计算节点）⇒ 消费侧必须按 PARENTNAME 非空过滤。
        """
        return self._request("GET", "host_link")

    # ============================================================
    # 增量采集
    # ============================================================

    def get_lun_by_id(self, lun_id: str) -> Optional[dict]:
        """GET /lun/{id} — 单个LUN详情

        返回单个LUN dict，或 None（未找到/请求失败）
        """
        luns = self._request("GET", f"lun/{lun_id}")
        if isinstance(luns, dict):
            return luns
        if isinstance(luns, list) and len(luns) == 1:
            return luns[0]
        return None

    def get_luns_by_host(self, host_id: str) -> list:
        """GET /lun/associate?ASSOCIATEOBJTYPE=21&ASSOCIATEOBJID={host_id}

        返回指定主机映射的LUN列表
        """
        return self._request(
            "GET",
            "lun/associate",
            params={"ASSOCIATEOBJTYPE": 21, "ASSOCIATEOBJID": host_id},
        )

    # ============================================================
    # 全量发现
    # ============================================================

    def full_discovery(self) -> dict:
        """全量发现：采集所有端点并返回结构化数据

        同时构建 host_lun_map：只对有名字的host调用 get_luns_by_host()
        记录采集耗时
        """
        import time
        start = time.time()

        system = self.collect_system()
        controllers = self.collect_controllers()
        pool = self.collect_storage_pool()
        luns = self.collect_luns()
        disks = self.collect_disks()
        hosts = self.collect_hosts()
        replication = self.collect_replication_pairs()
        fc_ports = self.collect_fc_ports()
        # ── NoF（NVMe over RoCE）端点（2026-09-16 新增）──
        # 两次 GET，与既有一批同会话同认证；实测无 404 风险（已单独验证过）。
        eth_ports = self.collect_eth_ports()
        host_links = self.collect_host_links()

        # host_lun_map: 只对有名字的host调用关联查询
        #   ★ 并发取（2026-09-25 提速）：实测生产 18 个 host **串行 1,387ms**
        #     （单次 ~77ms，全是网络往返）。
        #   ★ 与串行**逐字节等价**：结果按 host 名排序后写回，
        #     顺序与内容都与串行一致（由 `tmp_equiv_luns.py` 在生产上实测比对）。
        #   ★ 为什么可以共享一个 session：`_request` 是**只读 GET** 且 token 在
        #     会话头里（不每次改头），并发读不会改变各自取到的事实。
        #     若将来改成写请求，必须改为每线程独立会话。
        host_lun_map: dict = {}
        if isinstance(hosts, list):
            _targets = [(h.get("ID"), h.get("NAME", "")) for h in hosts
                        if isinstance(h, dict) and h.get("ID") and h.get("NAME")]
            if len(_targets) > 1:
                from concurrent.futures import ThreadPoolExecutor
                _res: dict = {}
                with ThreadPoolExecutor(max_workers=min(8, len(_targets))) as _ex:
                    _fut = {_ex.submit(self.get_luns_by_host, hid): hname
                            for hid, hname in _targets}
                    for _f, _hn in _fut.items():
                        try:
                            _res[_hn] = _f.result(timeout=60)
                        except Exception:                          # noqa: BLE001
                            _res[_hn] = None      # 单个失败不拖垮整批（与串行同语义）
                # ★ 按 `_targets` 的**原始顺序**写回，不按名字排序 ——
                #   实测：串行实现的键顺序 = hosts 列表顺序，而按名排序会得到
                #   另一种顺序（`Host_11/12/13` vs 一串负数名）。
                #   值虽逐字节相同，但**顺序**可能在消费方 first-wins 去重时产生
                #   不同结果 ⇒ 「等价」必须是**逐字节（含顺序）**的，不是「等价到排序为止」。
                for _hid, _hn in _targets:
                    _ll = _res.get(_hn)
                    if isinstance(_ll, list) and _ll:
                        host_lun_map[_hn] = _ll
            else:
                for hid, hname in _targets:
                    lun_list = self.get_luns_by_host(hid)
                    if isinstance(lun_list, list) and lun_list:
                        host_lun_map[hname] = lun_list

        elapsed = time.time() - start

        result = {
            "system": system if isinstance(system, dict) else {},
            "controllers": controllers if isinstance(controllers, list) else [],
            "pool": pool if isinstance(pool, list) else [],
            "luns": luns if isinstance(luns, list) else [],
            "disks": disks if isinstance(disks, list) else [],
            "hosts": hosts if isinstance(hosts, list) else [],
            "replication": replication if isinstance(replication, list) else [],
            "fc_ports": fc_ports if isinstance(fc_ports, list) else [],
            # ── NoF（NVMe over RoCE）（2026-09-16 新增）──
            "eth_ports": eth_ports if isinstance(eth_ports, list) else [],
            "host_links": host_links if isinstance(host_links, list) else [],
            "host_lun_map": host_lun_map,
            "_elapsed_seconds": round(elapsed, 2),
        }

        # 写入缓存供 diagnose/summary 复用
        self._cache = result

        logger.info("全量发现完成, 耗时 %.2fs", elapsed)
        return result

    # ============================================================
    # 诊断
    # ============================================================

    def diagnose(self) -> List[DoradoAlert]:
        """基于采集数据执行健康诊断，返回告警列表

        检查项:
        - 控制器 HEALTHSTATUS != "1" → CRITICAL
        - 控制器 CPUUSAGE > 80 → WARNING
        - 控制器 MEMUSAGE > 75 → WARNING
        - 存储池使用率 > 85% → WARNING
        - 容灾复制对 HEALTHSTATUS != "1" → CRITICAL
        - 容灾复制对 RUNNINGSTATUS != "1" → CRITICAL
        - LUN HEALTHSTATUS != "1" → CRITICAL
        - 磁盘 HEALTHSTATUS != "1" → WARNING
        """
        alerts: List[DoradoAlert] = []
        aid = [0]

        def _next_id() -> str:
            aid[0] += 1
            return f"dorado-{aid[0]:04d}"

        data = self._cache if self._cache else self.full_discovery()

        # —— 控制器检查 ——
        controllers = data.get("controllers", [])
        for ctrl in controllers:
            cid = ctrl.get("ID", "?")
            health = ctrl.get("HEALTHSTATUS")
            if health is not None and str(health) != "1":
                alerts.append(DoradoAlert(
                    id=_next_id(),
                    severity=Severity.CRITICAL,
                    title="控制器健康异常",
                    detail=f"控制器 {cid} HEALTHSTATUS={health}, 预期=1",
                    entity=f"controller:{cid}",
                ))
            cpu_str = ctrl.get("CPUUSAGE", "0")
            try:
                cpu = float(cpu_str)
                if cpu > 80:
                    alerts.append(DoradoAlert(
                        id=_next_id(),
                        severity=Severity.WARNING,
                        title="控制器CPU使用率过高",
                        detail=f"控制器 {cid} CPUUSAGE={cpu}% > 80%",
                        entity=f"controller:{cid}",
                    ))
            except (ValueError, TypeError):
                pass
            mem_str = ctrl.get("MEMUSAGE", "0")
            try:
                mem = float(mem_str)
                if mem > 75:
                    alerts.append(DoradoAlert(
                        id=_next_id(),
                        severity=Severity.WARNING,
                        title="控制器内存使用率过高",
                        detail=f"控制器 {cid} MEMUSAGE={mem}% > 75%",
                        entity=f"controller:{cid}",
                    ))
            except (ValueError, TypeError):
                pass

        # —— 存储池检查 ——
        pools = data.get("pool", [])
        for p in pools:
            pname = p.get("NAME", "?")
            total_str = p.get("USERTOTALCAPACITY", "0")
            used_str = p.get("USERCONSUMEDCAPACITY", "0")
            try:
                total = float(total_str)
                used = float(used_str)
                if total > 0:
                    pct = used / total * 100
                    if pct > 85:
                        alerts.append(DoradoAlert(
                            id=_next_id(),
                            severity=Severity.WARNING,
                            title="存储池使用率过高",
                            detail=f"存储池 {pname} 使用率={pct:.1f}% > 85%",
                            entity=f"storagepool:{pname}",
                        ))
            except (ValueError, TypeError):
                pass

        # —— 容灾复制对检查 ——
        reps = data.get("replication", [])
        for rep in reps:
            rid = rep.get("ID", "?")
            h = rep.get("HEALTHSTATUS")
            if h is not None and str(h) != "1":
                alerts.append(DoradoAlert(
                    id=_next_id(),
                    severity=Severity.CRITICAL,
                    title="容灾复制对健康异常",
                    detail=f"复制对 {rid} HEALTHSTATUS={h}, 预期=1",
                    entity=f"replicationpair:{rid}",
                ))
            rs = rep.get("RUNNINGSTATUS")
            if rs is not None and str(rs) != "1":
                alerts.append(DoradoAlert(
                    id=_next_id(),
                    severity=Severity.CRITICAL,
                    title="容灾复制对运行状态异常",
                    detail=f"复制对 {rid} RUNNINGSTATUS={rs}, 预期=1",
                    entity=f"replicationpair:{rid}",
                ))

        # —— LUN检查 ——
        luns = data.get("luns", [])
        for lun in luns:
            lid = lun.get("ID", "?")
            h = lun.get("HEALTHSTATUS")
            if h is not None and str(h) != "1":
                alerts.append(DoradoAlert(
                    id=_next_id(),
                    severity=Severity.CRITICAL,
                    title="LUN健康异常",
                    detail=f"LUN {lun.get('NAME', lid)} ID={lid} HEALTHSTATUS={h}, 预期=1",
                    entity=f"lun:{lid}",
                ))

        # —— 磁盘检查 ——
        disks = data.get("disks", [])
        for disk in disks:
            did = disk.get("ID", "?")
            h = disk.get("HEALTHSTATUS")
            if h is not None and str(h) != "1":
                alerts.append(DoradoAlert(
                    id=_next_id(),
                    severity=Severity.WARNING,
                    title="磁盘健康异常",
                    detail=f"磁盘 {disk.get('LOCATION', did)} ID={did} HEALTHSTATUS={h}, 预期=1",
                    entity=f"disk:{did}",
                ))

        return alerts

    # ============================================================
    # 统计摘要
    # ============================================================

    def summary(self) -> dict:
        """生成采集数据摘要

        返回:
            device_name, product_model,
            controller_count, controller_health_ok,
            pool_name, pool_usage_pct,
            lun_count, lun_health_ok,
            disk_count, disk_health_ok,
            replication_pairs, replication_health_ok
        """
        data = self._cache if self._cache else self.full_discovery()

        system = data.get("system", {}) or {}
        controllers = data.get("controllers", []) or []
        pools = data.get("pool", []) or []
        luns = data.get("luns", []) or []
        disks = data.get("disks", []) or []
        reps = data.get("replication", []) or []

        # 设备信息
        device_name = system.get("NAME", "N/A")
        product_model = system.get("productModeString") or system.get("PRODUCTVERSION", "N/A")

        # 控制器
        controller_count = len(controllers)
        controller_health_ok = sum(
            1 for c in controllers if str(c.get("HEALTHSTATUS", "")) == "1"
        )

        # 存储池（取第一个）
        pool_name = pools[0].get("NAME", "N/A") if pools else "N/A"
        pool_usage_pct = 0.0
        if pools:
            p = pools[0]
            try:
                total = float(p.get("USERTOTALCAPACITY", "0"))
                used = float(p.get("USERCONSUMEDCAPACITY", "0"))
                if total > 0:
                    pool_usage_pct = round(used / total * 100, 1)
            except (ValueError, TypeError):
                pass

        # LUN
        lun_count = len(luns)
        lun_health_ok = sum(
            1 for l in luns if str(l.get("HEALTHSTATUS", "")) == "1"
        )

        # 磁盘
        disk_count = len(disks)
        disk_health_ok = sum(
            1 for d in disks if str(d.get("HEALTHSTATUS", "")) == "1"
        )

        # 容灾复制对
        replication_pairs = len(reps)
        replication_health_ok = sum(
            1 for r in reps if str(r.get("HEALTHSTATUS", "")) == "1"
        ) if reps else 0

        return {
            "device_name": device_name,
            "product_model": product_model,
            "controller_count": controller_count,
            "controller_health_ok": f"{controller_health_ok}/{controller_count}",
            "pool_name": pool_name,
            "pool_usage_pct": pool_usage_pct,
            "lun_count": lun_count,
            "lun_health_ok": f"{lun_health_ok}/{lun_count}",
            "disk_count": disk_count,
            "disk_health_ok": f"{disk_health_ok}/{disk_count}",
            "replication_pairs": replication_pairs,
            "replication_health_ok": f"{replication_health_ok}/{replication_pairs}" if reps else "N/A",
        }

    # ============================================================
    # 资源管理
    # ============================================================

    def close(self):
        """关闭会话，释放资源"""
        if self._authenticated:
            try:
                self.logout()
            except Exception:
                pass
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================
# NoF（NVMe over RoCE）事实转换（2026-09-16 新增）
# ============================================================
def nof_to_facts(raw: dict, array_sn: str = "") -> List[dict]:
    """把 DeviceManager 的 eth_ports / host_links 原始记录转成事实条目

    为什么单独放在采集器而不写在探针里：这是**纯函数**（无 IO、无状态），
    可在本地对固定输入做单测；探针只负责调用它。

    两条硬规则（均为实测踩出来的）：
      R1 **host_link 必须按 PARENTNAME 非空过滤**。实测 24 条里有 12 条未登记主机名
         （hostIP 落在 target 网段 188.168.1.201/202/203），它们不是计算节点；
         不过滤会把「阵列口挂 6 个 initiator」讲成「6 个数据库节点」。
      R2 **obj_id 必须带阵列 SN**。实测一个节点的 16 个 nvme 控制器横跨 3 个阵列，
         短名（如 nvme1）跨阵列重名；阵列端口同理（不同阵列可以都有 CTE0.A.IOM3.P1）。

    obj_id 约定（与既有「节点|vlun」复合键同构）：
      DoradoEthPort : <阵列SN>|<LOCATION>
      NofPathView   : <阵列SN>|<hostIP>|<targetIP>
    """
    facts: List[dict] = []
    src = "storage.dorado.nof"
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    sn = (array_sn or "").strip() or (raw.get("system") or {}).get("ID", "") or "unknown"

    def _f(path, value, obj_id, obj_type):
        if value is None or value == "":
            return
        facts.append({"path": path, "value": value, "obj_id": obj_id,
                      "obj_type": obj_type, "source": src, "ts": ts})

    eth_ports = raw.get("eth_ports") or []
    host_links = raw.get("host_links") or []

    # ── 端口清单 ──
    _f("storage.dorado.eth_port.count", len(eth_ports), sn, "dorado_array")
    data_ports = 0
    named_init_by_port: Dict[str, int] = {}
    for h in host_links:
        if str(h.get("PARENTNAME") or "").strip():
            p = str(h.get("port") or "").strip()
            if p:
                named_init_by_port[p] = named_init_by_port.get(p, 0) + 1

    for p in eth_ports:
        loc = str(p.get("LOCATION") or "").strip()
        if not loc:
            continue
        oid = f"{sn}|{loc}"
        try:
            n_init = int(p.get("numberOfInitiators") or 0)
        except (TypeError, ValueError):
            n_init = 0
        if n_init > 0:
            data_ports += 1
        # ★ arrPortLocation 就是 obj_id 的第二段；在此重发一次便于按对象取值
        _f("storage.dorado.eth_port.location", loc, oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.name", p.get("NAME"), oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.speed_mbps", p.get("SPEED"), oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.running_status", p.get("RUNNINGSTATUS"),
           oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.health_status", p.get("HEALTHSTATUS"),
           oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.owning_controller", p.get("OWNINGCONTROLLER"),
           oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.num_initiators", n_init, oid, "dorado_eth_port")
        # ★ 口径分离：raw 计数含未登记 initiator；本项只数**已登记主机**的
        #   （面向「哪些数据库节点连了这个口」的提问必须用本项，用 raw 会多算）
        if loc in named_init_by_port:
            _f("storage.dorado.eth_port.num_named_initiators",
               named_init_by_port[loc], oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.physical_type", p.get("physicalType"),
           oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.mac", p.get("MACADDRESS"), oid, "dorado_eth_port")
        _f("storage.dorado.eth_port.mtu", p.get("MTU"), oid, "dorado_eth_port")
    _f("storage.dorado.eth_port.data_port_count", data_ports, sn, "dorado_array")

    # ── 主机↔端口链路（配对关系的唯一来源）──
    named = [h for h in host_links if str(h.get("PARENTNAME") or "").strip()]
    _f("storage.dorado.host_link.count", len(named), sn, "dorado_array")
    _f("storage.dorado.host_link.raw_count", len(host_links), sn, "dorado_array")
    for h in named:
        host_ip = str(h.get("hostIP") or "").strip()
        tgt_ip = str(h.get("TARGET_PORT_WWN") or "").strip()
        oid = f"{sn}|{host_ip}|{tgt_ip}"
        _f("storage.dorado.host_link.array_sn", sn, oid, "nof_path_view")
        _f("storage.dorado.host_link.host_name", h.get("PARENTNAME"), oid, "nof_path_view")
        _f("storage.dorado.host_link.host_ip", host_ip, oid, "nof_path_view")
        _f("storage.dorado.host_link.target_ip", tgt_ip, oid, "nof_path_view")
        _f("storage.dorado.host_link.array_port", h.get("port"), oid, "nof_path_view")
        _f("storage.dorado.host_link.ctrl_id", h.get("CTRL_ID"), oid, "nof_path_view")
        _f("storage.dorado.host_link.health_status", h.get("HEALTHSTATUS"),
           oid, "nof_path_view")
        _f("storage.dorado.host_link.running_status", h.get("RUNNINGSTATUS"),
           oid, "nof_path_view")
        _f("storage.dorado.host_link.initiator_nqn", h.get("INITIATOR_PORT_WWN"),
           oid, "nof_path_view")
        _f("storage.dorado.host_link.ultra_path_version", h.get("ultraPathVersion"),
           oid, "nof_path_view")
    _f("storage.dorado.controller.port_count",
       len({str(h.get("port")) for h in named if h.get("port")}), sn, "dorado_array")

    # ★★ 2026-09-23 新增：**被登记的目标端口里有多少个链路断开**（单值计数事实）。
    #   为什么必须是"单值计数"而不是让规则去关联两个事实 —— 实测规则引擎对
    #   `when` 是**跨对象全局求值**，编译出的 SPARQL 形如：
    #     VALUES ?v1 { 所有端口的 num_initiators }   # 平铺成一个袋子
    #     VALUES ?v2 { 所有端口的 running_status }
    #     FILTER(?v2 = "11" && ?v1 > 0)              # 「某处有11」且「某处有3」⇒ 真
    #   **没有任何"同一对象"的约束**，于是「A 口有主机 + B 口断了」也会命中。
    #   而实测 40 个端口中 **32 个未插线本就 link down**（speed=-1 与 running=11
    #   是完全同一集合），所以那种规则对**健康数据误报**（已实测 1 命中）。
    #
    #   ⇒ 相关性必须**下沉到事实**。一个端口只有出现在 host_link（阵列自己维护的
    #     「主机↔端口」链路表）里，才算「**应当 up 的目标端口**」；没登记 = 未插线，
    #     不管它 down 不 down（与用户裁定②「未插线网口不管」同源）。
    #     据此产出计数，规则退化为单事实条件 `> 0` —— 此时全局求值**恰好就是
    #     正确语义**，既不需要跨对象关联，也不会把未插线口算进来。
    _run_status = {str(p.get("LOCATION") or "").strip():
                   str(p.get("RUNNINGSTATUS") or "").strip()
                   for p in eth_ports}
    _target_ports = {str(h.get("port") or "").strip()
                     for h in named if str(h.get("port") or "").strip()}
    # 11=link down、28=offline（码义取自厂商接口文档，见 onto_service 枚举表）
    _down = sorted(tp for tp in _target_ports
                   if _run_status.get(tp) in ("11", "28"))
    _f("storage.dorado.eth_port.target_port_count", len(_target_ports),
       sn, "dorado_array")
    _f("storage.dorado.eth_port.target_port_down_count", len(_down),
       sn, "dorado_array")
    for _tp in _down:
        _f("storage.dorado.eth_port.target_port_down", _tp, sn, "dorado_array")

    return facts


# ============================================================
# 命令行接口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="华为OceanStor Dorado DeviceManager RESTful采集探针",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default=None,
                        help="DeviceManager基础URL (env: MCP_STORAGE_DM_BASE_URL)")
    parser.add_argument("--username", default=None,
                        help="DeviceManager用户名 (env: MCP_STORAGE_DM_USER)")
    parser.add_argument("--password", default=None,
                        help="DeviceManager密码 (env: MCP_STORAGE_DM_PASSWORD)")
    parser.add_argument("--action", choices=["discovery", "diagnose", "summary",
                                             "lun-by-id", "luns-by-host"],
                        default="summary",
                        help="采集动作")
    parser.add_argument("--lun-id", default=None, help="LUN ID (lun-by-id 动作)")
    parser.add_argument("--host-id", default=None, help="Host ID (luns-by-host 动作)")
    parser.add_argument("--verify-ssl", action="store_true", default=False,
                        help="验证SSL证书 (默认关闭)")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="输出调试日志")

    args = parser.parse_args()

    # 日志
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s %(message)s")

    # 从环境变量或参数取值
    base_url = args.base_url or os.environ.get("MCP_STORAGE_DM_BASE_URL")
    username = args.username or os.environ.get("MCP_STORAGE_DM_USER")
    password = args.password or os.environ.get("MCP_STORAGE_DM_PASSWORD")

    if not base_url or not username or not password:
        print("错误: 需要提供 --base-url / --username / --password 或设置环境变量 "
              "MCP_STORAGE_DM_BASE_URL / MCP_STORAGE_DM_USER / MCP_STORAGE_DM_PASSWORD",
              file=sys.stderr)
        sys.exit(1)

    collector = DoradoCollector(base_url, username, password, verify_ssl=args.verify_ssl)
    try:
        if not collector.login():
            print("登录失败", file=sys.stderr)
            sys.exit(1)

        if args.action == "discovery":
            result = collector.full_discovery()
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif args.action == "diagnose":
            collector.full_discovery()
            alerts = collector.diagnose()
            for alert in alerts:
                print(f"[{alert.severity.value.upper()}] {alert.title}: {alert.detail}")
            print(f"\n共 {len(alerts)} 条告警")
        elif args.action == "summary":
            collector.full_discovery()
            print(json.dumps(collector.summary(), indent=2, ensure_ascii=False))
        elif args.action == "lun-by-id":
            if not args.lun_id:
                print("错误: --lun-id 是必需的", file=sys.stderr)
                sys.exit(1)
            lun = collector.get_lun_by_id(args.lun_id)
            print(json.dumps(lun, indent=2, ensure_ascii=False))
        elif args.action == "luns-by-host":
            if not args.host_id:
                print("错误: --host-id 是必需的", file=sys.stderr)
                sys.exit(1)
            luns = collector.get_luns_by_host(args.host_id)
            print(json.dumps(luns, indent=2, ensure_ascii=False))
    finally:
        collector.close()
