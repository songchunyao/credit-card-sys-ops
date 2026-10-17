"""GaussDB 运维 Agent — Storage 层采集探针

通过 DeviceManager RESTful API 采集华为 OceanStor Dorado 存储设备指标。
存算一体场景下自动退化为本地盘扫描。

数据来源: DeviceManager RESTful API → Dorado 设备
采集方式: dorado_collector（封装）
退化模式: SSH → lsblk 扫描本地盘
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
from typing import Any, Dict, List, Optional

from . import BaseProbe, ProbeResult, register_probe

logger = logging.getLogger(__name__)


# ── 共享卷映射完整性判定（模块级纯函数，便于单测）────────────────────────
# 语义：规则 r_st_dorado_shared_xlog_vol 需要「**违规**即命中」的表达，
#       而原判据描述的是**合规状态**（健康也命中）⇒ 基线的 info 永远消不掉。
# ★ 为何不能对所有业务卷"要求覆盖全节点"（实测教训）：
#   14-xlog-11-12-13 / 14-cluster-11-12-13 → 共享卷，映射 Host_11+12+13
#   14-data-11/12/13、14-log-*、14-etcd-*、14-backup-*  → 单节点专有卷，各只映射 1 个
#   若一刀切要求覆盖 3 节点 ⇒ **误报 12 条**。
#   ⇒ 只在「**卷名自身声明了多节点**」时校验：名字里的节点号 = 期望节点集。
#     名字不含节点信息的卷**不参与判定** —— 不臆造期望，也就不臆造告警。
_DECLARED_NODE_RE = re.compile(r"(?:^|[-_])(\d{1,3})(?=[-_]|$)")


def declared_nodes(name: str) -> List[str]:
    """从卷名解析期望节点：`14-xlog-11-12-13` → ['Host_11','Host_12','Host_13']

    约定：首个数字段是数组/集群编号，其余数字段是节点编号（现场命名 14-xlog-11-12-13）。
    解析不出 ≥2 个数字段 ⇒ 返回 []，表示**该卷名不声明多节点**，调用方应跳过校验。
    """
    nums = _DECLARED_NODE_RE.findall(str(name))
    if len(nums) < 2:
        return []
    return ["Host_%d" % int(n) for n in nums[1:]]


def shared_vol_unmapped(luns: List[dict], hosts_by_lun: Dict[str, List[str]]) -> List[str]:
    """返回「声明了多节点却有节点未映射」的卷描述列表（空 = 全部合规）

    Args:
        luns: 采集器 raw['luns']（每项含 NAME/ID/WWN）
        hosts_by_lun: LUN 标识 → 映射到的 host 名列表（来自 host_lun_map）

    为什么返回**描述列表**而不是布尔：命中时要给得出"哪个卷缺哪个节点"，
    否则运维看不懂告警（本项目对"证据必须可追溯"的要求）。
    """
    out: List[str] = []
    for lun in (luns or []):
        if not isinstance(lun, dict):
            continue
        lname = str(lun.get("NAME") or lun.get("ID") or "")
        if not lname:
            continue
        declared = declared_nodes(lname)
        if not declared:
            continue                    # 卷名未声明多节点 ⇒ 无可校验
        mapped = set(hosts_by_lun.get(str(lun.get("ID") or "")) or
                     hosts_by_lun.get(str(lun.get("WWN") or "")) or
                     hosts_by_lun.get(lname) or [])
        missing = [h for h in declared if h not in mapped]
        if missing:
            out.append("%s(缺%s)" % (lname, ",".join(missing)))
            logger.warning("共享卷 %s 声明节点 %s，实际映射 %s ⇒ 缺 %s",
                           lname, declared, sorted(mapped), missing)
    return out

# 导入 Dorado 采集器
_STORAGES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "storages")
if _STORAGES_DIR not in sys.path:
    sys.path.insert(0, _STORAGES_DIR)


def unmapped_count_facts(count: int, src: str, ts: str) -> List[dict]:
    """构造共享卷计数事实 —— ★ 无论 count 是否为 0，都必须产出 1 条

    为什么单独成函数：规则判 `gt 0`，若健康时不产出该事实，则路径**无值**，
    「没采到」与「采到且健康」就分不清了（本项目反复踩的隐性缺失）。
    抽成函数是为了让门禁能**行为断言**这一点 —— 埋在 collect() 里就只能靠现场碰。
    """
    return [{"path": "dorado.dorado_xlog_vol_unmapped_count", "value": count,
             "obj_id": "dorado_vols", "obj_type": "dorado_volume_set",
             "source": src + ".rule", "ts": ts}]


class StorageProbe(BaseProbe):
    """Storage 层采集探针"""

    def __init__(self, base_url: Optional[str] = None,
                 username: Optional[str] = None, password: Optional[str] = None,
                 verify_ssl: Optional[bool] = None):
        self._base_url = base_url or os.environ.get("MCP_STORAGE_DM_BASE_URL", "")
        self._username = username or os.environ.get("MCP_STORAGE_DM_USER", "admin")
        self._password = password or os.environ.get("MCP_STORAGE_DM_PASSWORD", "")
        # DeviceManager 多为自签证书，默认关闭校验；可用 MCP_STORAGE_DM_VERIFY_SSL=true 打开。
        # 2026-09-14 前此开关是死配置（.env 声明了但代码硬编码 False），现改为真实生效。
        if verify_ssl is None:
            verify_ssl = os.environ.get("MCP_STORAGE_DM_VERIFY_SSL", "false").strip().lower() \
                in ("1", "true", "yes", "on")
        self._verify_ssl: bool = bool(verify_ssl)
        self._mode: str = "unknown"  # "dorado" | "local_disk" | "unavailable"
        # ── 跨采集复用会话（2026-09-25 提速）────────────────────────────────
        #   实测（生产 DeviceManager）：每次 `collect()` 要付 **两次 login ≈ 2.4s**
        #     · `_detect_mode()` 为了探活做一次 login + logout
        #     · `_collect_dorado()` 再登一次
        #   而 login 实测 1,119ms/次，storage 层单次采集总耗时 9.2s ⇒ 登录占 26%。
        #   ⇒ 持有一个 collector，token 有效就直接用；失效由 `_ensure_auth` 自动重登。
        #   ★ 安全性：`_request` 全程带 iBaseToken，复用不改变**取到的事实**，
        #     只改变「多久取到」；且探针实例由 `register_probe` 单例持有。
        self._collector = None
        self._collector_key = None

    @property
    def identity(self) -> str:
        return "storage"

    # ── 公共方法 ──

    def collect(self, **kwargs) -> ProbeResult:
        """执行 Storage 层采集

        自动检测模式：尝试连接 DeviceManager → 成功为 dorado 模式，
        失败则退化到本地盘扫描模式。

        Returns:
            ProbeResult
        """
        start = time.time()

        if self._detect_mode():
            return self._collect_dorado(start)
        else:
            return self._collect_local_disks(start)

    # ── 模式检测 ──

    def _detect_mode(self) -> bool:
        """检测是否可连接 DeviceManager"""
        if not self._base_url:
            self._mode = "local_disk"
            return False

        try:
            collector = self._get_collector()
            if collector is None:
                self._mode = "local_disk"
                return False
            # ★ 这里**不再 login+logout 探活**（实测两次登录共 ~2.4s）：
            #   `_get_collector()` 已保证「拿到一个能用的 collector」（必要时已登录），
            #   真连不上时它返回 None / `_collect_dorado` 会走异常分支 ⇒ 模式判定不变。
            ok = bool(getattr(collector, "_authenticated", False))
            if ok:
                self._mode = "dorado"
                return True
            self._mode = "unavailable"
            return False
        except Exception:
            self._mode = "local_disk"
            return False

    def _get_collector(self):
        """取（并按需重建）复用的 Dorado collector。

        ★ 为什么缓存：每次采集两次登录共约 2.4s，占 storage 层 9.2s 的 26%。
        ★ 为什么用**配置指纹**缓存：base_url/账号/SSL 变了必须新建，
          否则会拿着旧配置的连接去取数（**取错设备**比慢得多危险）。
          指纹不一致 ⇒ 丢弃旧 collector 并新建。
        """
        key = (self._base_url, self._username, self._verify_ssl)
        if self._collector is not None and self._collector_key == key:
            return self._collector
        try:
            from dorado_collector import DoradoCollector
        except Exception:                                          # noqa: BLE001
            return None
        c = DoradoCollector(self._base_url, self._username, self._password,
                            verify_ssl=self._verify_ssl)
        if not c.login():
            self._collector, self._collector_key = None, None
            return None
        self._collector, self._collector_key = c, key
        return c

    # ── Dorado 模式（存算分离）──

    def _collect_dorado(self, start: float) -> ProbeResult:
        """DeviceManager 全量发现 + 诊断"""
        try:
            collector = self._get_collector()
            if collector is None:
                raise RuntimeError("DeviceManager 不可用（无法建立会话）")

            raw = collector.full_discovery()
            alerts_raw = collector.diagnose()
            summary = collector.summary()

            # ── P3 告警面（2026-09-24）──────────────────────────────────────
            # 用户主诉：控制器异常能报「异常」但说不出为什么。告警明细就是那层证据，
            # 此前登记为缺口 CG-ST-07（「须在阵列管理面看硬件告警明细」）⇒ 本条把它采上来。
            # ★ 按「采集失败不得静默」处理：取不到时写 scope=none（而非不产出），
            #   使「截断/失败」与「确实没有告警」在事实面上可区分。
            try:
                from service.probes.alarm_probe import fetch_alarms
                _alarm_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                facts_alarm = fetch_alarms(collector, collector._request, _alarm_ts,
                                           "deviceManager/rest/alarm")
                # 逐控制器的告警读数（决策树判据面只能读单路径事实，故在此聚合）
                from service.probes.alarm_probe import controller_alert_facts
                facts_alarm += controller_alert_facts(
                    facts_alarm, raw.get("controllers") or [], _alarm_ts,
                    "deviceManager/rest/alarm+controller")
            except Exception as _ea:
                facts_alarm = [{
                    "path": "storage.dorado.alarm.scope", "value": "none",
                    "obj_id": "dorado:alarm:all", "obj_type": "alarm_scope",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": "deviceManager/rest/alarm:error:%s" % type(_ea).__name__,
                }]
            # ★ 刻意**不 logout**（2026-09-25）：会话留给下次采集复用，
            #   省掉一次 login（1.1s）。会话由 DeviceManager 侧超时回收；
            #   失效时 `_ensure_auth` 会自动重登，不会取到错数据。

            # 转 ProbeResult 格式
            facts = self._dorado_to_facts(raw, summary) + facts_alarm
            alerts = [
                {
                    "severity": a.severity.value,
                    "title": a.title,
                    "detail": a.detail,
                    "entity": a.entity,
                    "source": "storage_probe",
                    "timestamp": a.timestamp,
                }
                for a in alerts_raw
            ]

            return ProbeResult(
                layer="storage",
                facts=facts,
                raw=raw,
                alerts=alerts,
                elapsed_ms=self._elapsed(start),
                mode="dorado",
            )
        except Exception as e:
            return ProbeResult(
                layer="storage",
                facts=[],
                raw={},
                alerts=[{
                    "severity": "critical",
                    "title": "DeviceManager 采集失败",
                    "detail": str(e),
                    "entity": "dorado_storage",
                    "source": "storage_probe",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }],
                elapsed_ms=self._elapsed(start),
                error=str(e),
                mode="dorado_error",
            )

    @staticmethod
    def _dorado_to_facts(raw: dict, summary: dict) -> List[dict]:
        """Dorado 采集数据 → Fact 数组"""
        facts = []
        src = "storage.dorado"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")

        # 系统信息
        sys_info = raw.get("system", {})
        if sys_info:
            for k, v in [("device_name", "NAME"), ("device_model", "productModeString"),
                         ("product_version", "PRODUCTVERSION"), ("wwn", "wwn"),
                         ("health_status", "HEALTHSTATUS"), ("running_status", "RUNNINGSTATUS"),
                         ("total_capacity", "TOTALCAPACITY")]:
                val = sys_info.get(v, "")
                if val:
                    facts.append({
                        "path": f"storage.dorado.{k}",
                        "value": val, "obj_id": sys_info.get("ID", "dorado"),
                        "obj_type": "dorado_storage", "source": src, "ts": ts,
                    })

        # 控制器
        for ctrl in raw.get("controllers", []):
            cid = ctrl.get("ID", "")
            # ★ 2026-09-23 新增：把控制器 id 落成**事实**（此前只当 obj_id 用）。
            #   目的：与 os 探针侧统一身份。`upadmin show path` 报的控制器名
            #   （裸 0A/0B）与本侧设备管理器读到的 ID **完全一致**，两侧落成
            #   同名同值事实后，本体里 gdo:identityKeyPath gdo:DoradoController
            #   就能把它们合并成**一个**控制器；否则同一物理控制器会画成两个
            #   节点（0A 与 ctrl-0A，实测正是如此，连状态颜色都不一样）。
            if cid:
                facts.append({
                    "path": "storage.dorado.controller.id", "value": cid,
                    "obj_id": cid, "obj_type": "dorado_controller",
                    "source": src, "ts": ts,
                })
            for k, v in [("cpu_usage", "CPUUSAGE"), ("mem_usage", "MEMUSAGE"),
                         ("health_status", "HEALTHSTATUS"), ("is_master", "ISMASTER")]:
                val = ctrl.get(v, "")
                if val:
                    facts.append({
                        "path": f"storage.dorado.controller.{k}",
                        "value": val, "obj_id": cid,
                        "obj_type": "dorado_controller", "source": src, "ts": ts,
                    })

        # 存储池
        # ★ 2026-09-20 修复：`collect_storage_pool()` 返回的是 **list**（`storagepool`
        #   在 DoradoCollector._LIST_ENDPOINTS 里 ⇒ `_request` 归一为 list），
        #   而此处原写成 `isinstance(pool, dict) and pool.get("ID")` ⇒ **整段被跳过**，
        #   `storage.dorado.pool.*` 与 `dorado.dorado_pool_usage_pct` 永不产出
        #   ⇒ 规则 `r_st_dorado_pool_capacity`（池容量告警）**在生产永远不可能命中**。
        #   实测取证：生产 gather(storage) 有 79 个 Dorado 路径，**无任何 pool.* 路径**。
        #   两类形态都兼容（dict / list），与 disk/lun 的处理保持一致。
        pool = raw.get("pool", [])
        pool_items = [pool] if isinstance(pool, dict) else (pool or [])
        for _p in pool_items:
            if not isinstance(_p, dict) or not _p.get("ID"):
                continue
            pid = _p.get("ID", "pool0")
            for k, v in [("pool_name", "NAME"), ("total_capacity", "USERTOTALCAPACITY"),
                         ("used_capacity", "USERCONSUMEDCAPACITY"),
                         ("health_status", "HEALTHSTATUS"), ("raid_level", "DESCRIPTION"),
                         ("usage_pct", "")]:
                val = _p.get(v, "")
                if val:
                    facts.append({
                        "path": f"storage.dorado.pool.{k}",
                        "value": val, "obj_id": pid,
                        "obj_type": "dorado_pool", "source": src, "ts": ts,
                    })

        # LUN 统计
        luns = raw.get("luns", [])
        facts.append({
            "path": "storage.dorado.lun.count",
            "value": len(luns), "obj_id": "all",
            "obj_type": "dorado_lun", "source": src, "ts": ts,
        })
        unhealthy_luns = [l for l in luns if l.get("HEALTHSTATUS") and l["HEALTHSTATUS"] != "1"]
        if unhealthy_luns:
            facts.append({
                "path": "storage.dorado.lun.unhealthy_count",
                "value": len(unhealthy_luns),
                "obj_id": "all", "obj_type": "dorado_lun", "source": src, "ts": ts,
            })

        # 容灾复制
        # ★ 2026-09-21 修：这里原来是 `raw.get("replication", [])`，而采集器的
        #   端点名是 **replicationpair**（见 DoradoCollector._LIST_ENDPOINTS）——
        #   键名不匹配 ⇒ `repl` **恒为空** ⇒ `storage.dorado.replication.count`
        #   恒为 0，且下方复制对的 `RUNNINGSTATUS` 永远不会被采纳
        #   ⇒ 别名 `dorado.dorado_rep_running_status` 只能落到"阵列整机状态"
        #   （system 端点，健康值是 `'27'`=`RUNNINGSTATUS` 的"在线"码），
        #   而规则 `r_st_dorado_replication_health` 判据是 `ne "1"`
        #   （`'1'`=复制对 Normal）⇒ **健康阵列恒报 critical**。
        #   ⚠ 这说明该规则原来实际检的是"阵列整机在不在线"，与规则名/结论
        #   （"容灾复制对…数据同步中断"）**根本不是一回事**。
        #   两类键名都接受（兼容采集器的历史口径）。
        repl = raw.get("replicationpair") or raw.get("replication") or []
        facts.append({
            "path": "storage.dorado.replication.count",
            "value": len(repl), "obj_id": "all",
            "obj_type": "dorado_replication", "source": src, "ts": ts,
        })
        # 逐个复制对发 `storage.dorado.replication.status`（**专用路径**）
        #   ⇒ 由别名映射到 `dorado.dorado_rep_running_status`，
        #     与"阵列整机状态"（`storage.dorado.running_status`）分开，互不污染。
        #   `dorado.dorado_rep_count` 仍是"复制对总数"，语义与本体
        #   `gdo:repCount`（域 = DoradoReplicationPair）一致。
        for _rp in repl:
            if not isinstance(_rp, dict):
                continue
            _rs = _rp.get("RUNNINGSTATUS")
            if _rs is None or _rs == "":
                continue
            facts.append({
                "path": "storage.dorado.replication.status",
                "value": _rs, "obj_id": _rp.get("ID", "rep"),
                "obj_type": "dorado_replication", "source": src, "ts": ts,
            })

        # Host Lun Map
        host_lun_map = raw.get("host_lun_map", {})
        for host_name, host_luns in host_lun_map.items():
            facts.append({
                "path": f"storage.dorado.host.{host_name}.lun_count",
                "value": len(host_luns),
                "obj_id": host_name, "obj_type": "host", "source": src, "ts": ts,
            })

        # ── NoF（NVMe over RoCE）端点级事实（2026-09-16 新增）──────────
        # 背景：现场是 NVMe over RoCE，而此前只采 FC 端口，阵列侧端口从未进入事实层；
        # 主机侧 nvme list-subsys 的端点身份也被聚合成计数后丢弃。用户问
        # 「Dorado 控制器上的哪些端口与数据库节点上的多路径进行连接」时无从作答。
        # 转换逻辑放在采集器（纯函数，可单测），此处只负责调用与并入。
        try:
            from dorado_collector import nof_to_facts
            sys_info_nof = raw.get("system") or {}
            array_sn = str(sys_info_nof.get("ID") or "") if isinstance(sys_info_nof, dict) else ""
            facts.extend(nof_to_facts(raw, array_sn))
        except Exception as e:      # 不允许 NoF 转换失败拖垮整个存储层采集
            logger.warning("NoF 事实转换失败（其余存储事实不受影响）: %s", e)

        # ── 规则契约别名（D4 连带修复）──────────────────────────────────
        # 背景：data/rules/{default,advanced}.yaml 的存储层规则使用 dashed 路径
        # （storage.dorado_ctrl_*, storage.dorado_pool_usage_pct …），而探针产出的
        # 是 dotted 路径（storage.dorado.controller.cpu_usage …）。两套命名互不匹配
        # → 规则永远不命中。
        # 修复方式：把已采集到的数值**按规则契约的路径名再发一份**（同一事实，
        # 不额外发起采集），使规则层可直接命中；原 dotted 路径保持不变以兼容下游。
        def _alias(path: str, value: Any, obj: str, otype: str):
            if value is None or value == "":
                return
            facts.append({"path": path, "value": value, "obj_id": obj,
                          "obj_type": otype, "source": src + ".alias", "ts": ts})

        for f in list(facts):
            p, v, oid, otype = f.get("path", ""), f.get("value"), f.get("obj_id", ""), f.get("obj_type", "")
            if p == "storage.dorado.controller.cpu_usage":
                _alias("dorado.dorado_ctrl_cpu_usage", v, oid, otype)
            elif p == "storage.dorado.controller.mem_usage":
                _alias("dorado.dorado_ctrl_mem_usage", v, oid, otype)
            elif p == "storage.dorado.controller.health_status":
                _alias("dorado.dorado_ctrl_health_status", v, oid, otype)
            elif p == "storage.dorado.health_status":
                _alias("dorado.dorado_ctrl_health_status", v, oid, otype)
            elif p == "storage.dorado.pool.usage_pct":
                _alias("dorado.dorado_pool_usage_pct", v, oid, otype)
            elif p == "storage.dorado.pool.raid_level":
                # ★ 2026-09-23 新增（docs/63 §4.4 数字孪生 RAID 冗余度分级）：
                #   池的冗余方案（RAID 级别）此前只以 dotted 原始路径产出，
                #   本体侧**无属性、无绑定、无规则消费** —— 是"孤儿事实"。
                #   现补规范别名，使本体 `gdo:poolRaidLevel` 可绑定。
                #   ★ 实测坐实（P0，docs/63 §9.0）：真实阵列该值就是 RAID 级别字面量，
                #     生产池 DESCRIPTION="RAID_TP"（下划线形式，不是 RAID-TP）。
                #   ★ 消费方注意：容错盘数须由本体 `gdo:raidToleranceDisks` 查得，
                #     **不得在此按字符串猜**（RAID5=1/RAID6=2/RAID_TP=3 是知识，不是代码）。
                _alias("dorado.dorado_pool_raid_level", v, oid, otype)
            elif p == "storage.dorado.pool.total_capacity":
                _alias("storage.dorado_pool_total_capacity", v, oid, otype)
            elif p == "storage.dorado.pool.used_capacity":
                _alias("storage.dorado_pool_used_capacity", v, oid, otype)
            elif p == "storage.dorado.replication.count":
                _alias("dorado.dorado_rep_count", v, oid, otype)
            elif p == "storage.dorado.replication.status":
                # ★ 2026-09-21 修：**复制对状态**专用来源（逐个复制对的 RUNNINGSTATUS）。
                #   原实现把"阵列整机状态"（system 段，健康值 '27'）别名到本路径，
                #   而规则判据是 `ne "1"`（'1'=复制对 Normal）⇒ 健康阵列恒报 critical。
                #   本路径现在只承载复制对状态 ⇒ 与规则语义一致。
                _alias("dorado.dorado_rep_running_status", v, oid, otype)
            elif p == "storage.dorado.running_status":
                # ★ 2026-09-21 修：这里原来把 **阵列整机运行状态**（system 端点，
                #   健康值 '27'=「在线」）别名进 `dorado.dorado_rep_running_status`
                #   —— 与"复制对状态"（'1'=Normal）语义不同，混入后规则
                #   `ne "1"` 必然误报。本路径的语义是 DoradoStorage 整机状态，
                #   与本体 `gdo:repRunningStatus`（domain=DoradoReplicationPair）不符
                #   ⇒ **不再别名**。该整机状态如需规则消费，应另建属性/规则（待裁定）。
                pass
            elif p == "storage.dorado.lun.unhealthy_count":
                _alias("dorado.dorado_vol_health_status", v, oid, otype)
            elif p == "storage.dorado.lun.count":
                _alias("dorado.dorado_vol_count", v, oid, otype)
            elif p == "storage.dorado.product_version":
                _alias("storage.dorado_fw_version", v, oid, otype)
                _alias("storage.dorado_version", v, oid, otype)
            elif p == "storage.dorado.device_model":
                _alias("storage.dorado_dev_model", v, oid, otype)

        # 池使用率：DeviceManager 未必直接给 usage_pct，用 已用/总量 补算
        for f in list(facts):
            if f.get("path") == "storage.dorado.pool.used_capacity":
                used = f.get("value")
                tot = next((x.get("value") for x in facts
                            if x.get("path") == "storage.dorado.pool.total_capacity"), None)
                try:
                    if tot and float(tot) > 0:
                        pct = round(float(used) * 100.0 / float(tot), 2)
                        facts.append({"path": "dorado.dorado_pool_usage_pct", "value": pct,
                                      "obj_id": f.get("obj_id", "pool0"), "obj_type": "dorado_pool",
                                      "source": src + ".alias", "ts": ts})
                except (TypeError, ValueError):
                    pass

        # LUN 名称/健康（规则 r_st_dorado_vol_type_mapping_check / r_st_dorado_vol_health）
        # + 卷类型与 Host 映射（r_st_dorado_shared_xlog_vol，2026-09-11 新增）
        _hosts_by_lun: Dict[str, List[str]] = {}
        for hname, hluns in (raw.get("host_lun_map") or {}).items():
            for l in hluns:
                lid = str(l.get("ID") or l.get("WWN") or l.get("NAME") or "")
                if lid:
                    _hosts_by_lun.setdefault(lid, []).append(str(hname))

        for lun in (raw.get("luns") or []):
            lname = lun.get("NAME") or lun.get("ID") or ""
            if not lname:
                continue
            _alias("dorado.dorado_vol_name", lname, lname, "dorado_lun")
            if lun.get("HEALTHSTATUS"):
                _alias("dorado.dorado_vol_health_status", lun.get("HEALTHSTATUS"),
                       lname, "dorado_lun")
            # 卷类型：从 LUN 名推断（xlog 共享日志卷 / data 数据卷 / log 日志卷 …）
            # 注意顺序：xlog 必须先于 log 判定，否则 "xlog" 会被误判成 "log"
            vl = str(lname).lower()
            vtype = ""
            for t in ("xlog", "data", "log", "backup", "etcd", "cluster", "test"):
                if t in vl:
                    vtype = t
                    break
            if vtype:
                _alias("dorado.dorado_vol_type", vtype, lname, "dorado_lun")
            # Host 映射：反查该 LUN 被哪些 Host 挂载（规则用 contains 判断 Host_11/12/13）
            host_list = _hosts_by_lun.get(str(lun.get("ID") or "")) or \
                _hosts_by_lun.get(str(lun.get("WWN") or "")) or \
                _hosts_by_lun.get(lname) or []
            if host_list:
                _alias("dorado.dorado_vol_mapped_hosts", ",".join(sorted(set(host_list))),
                       lname, "dorado_lun")

        # ── 共享卷映射完整性（2026-09-22 新增）──────────────────────────
        # 背景：`r_st_dorado_shared_xlog_vol` 原判据是
        #   all[ vol_type eq "xlog" ∧ mapped_hosts contains 11/12/13 ]
        # —— 条件是「**已正确映射**」= **合规通过**，命中不是故障；
        #   健康集群上稳定产出一条 info，被读成"检测到异常"。
        # 用户裁定「normal 就该零命中」⇒ 本规则改为**违规检测**。
        # 判定逻辑放在模块级纯函数（shared_vol_unmapped），便于门禁单测：
        # 内联写法无法被测试覆盖，正是"逻辑埋在采集里 ⇒ 只能靠现场验"的老问题。
        _unmapped = shared_vol_unmapped(raw.get("luns") or [], _hosts_by_lun)
        # ★ 必须**无条件**产出该事实（含 0）：规则判 `gt 0`，健康时不产出则路径无值，
        #   「没采到」与「采到且健康」分不清（本项目反复踩的隐性缺失）。
        facts.extend(unmapped_count_facts(len(_unmapped), src, ts))
        if _unmapped:
            _alias("dorado.dorado_xlog_vol_unmapped_names",
                   ";".join(_unmapped[:10]), "dorado_vols", "dorado_volume_set")

        # ── 卷/LUN 明细（跨层映射链，2026-09-13 新增）────────────────────
        # 采集器本就取回了全量 LUN（含 WWN/NGUID/PARENTNAME/CAPACITY），此前
        # 只转成计数与别名 —— 属「数据已有、中间层丢弃」。现在逐卷落成事实：
        #   · nguid：**主机侧 upadmin 的 Lun WWN 与它逐字符相同**（生产实测两例全吻合）
        #   · capacity_sectors：DeviceManager 的 512B 扇区原始值（实测
        #     2147483648×512 = 1.00TiB，与 upadmin 显示一致）；另给 bytes 便于消费
        #   · pool：卷所属存储池（PARENTNAME）——「哪些盘与卷关联」经池建立
        for lun in (raw.get("luns") or []):
            lname = lun.get("NAME") or lun.get("ID") or ""
            if not lname:
                continue
            for _p, _v in (
                    ("storage.dorado.lun.name", lname),
                    ("storage.dorado.lun.id", lun.get("ID")),
                    ("storage.dorado.lun.wwn", lun.get("WWN")),
                    ("storage.dorado.lun.nguid", lun.get("NGUID")),
                    ("storage.dorado.lun.pool", lun.get("PARENTNAME")),
                    ("storage.dorado.lun.capacity_sectors", lun.get("CAPACITY")),
            ):
                if _v is None or _v == "":
                    continue
                facts.append({"path": _p, "value": _v, "obj_id": lname,
                              "obj_type": "dorado_lun", "source": src, "ts": ts})
            try:
                facts.append({"path": "storage.dorado.lun.capacity_bytes",
                              "value": int(lun.get("CAPACITY") or 0) * 512,
                              "obj_id": lname, "obj_type": "dorado_lun",
                              "source": src, "ts": ts})
            except (TypeError, ValueError):
                pass

        # ── 物理硬盘明细（2026-09-13 新增）──────────────────────────────
        # 与 LUN 同理：disks 一直在 full_discovery 里，但从未转成事实。
        # 卷与盘的关联经「存储池」建立（盘记录含 POOLNAME/POOLTIERID）。
        # 单位纪律：容量取 manuCapacity（字节，实测 3840000000000 = 3.84TB）；
        #   remain_life / runtime 为固件原始计数值，**原样保留、不换算**。
        disks_all = raw.get("disks") or []
        facts.append({"path": "storage.dorado.disk.count", "value": len(disks_all),
                      "obj_id": "all", "obj_type": "dorado_disk", "source": src, "ts": ts})
        for d in disks_all:
            loc = d.get("LOCATION") or str(d.get("ID") or "")
            if not loc:
                continue
            for _p, _v in (
                    ("storage.dorado.disk.location", d.get("LOCATION")),
                    ("storage.dorado.disk.model", d.get("MODEL")),
                    ("storage.dorado.disk.manufacturer", d.get("MANUFACTURER")),
                    ("storage.dorado.disk.serial", d.get("SERIALNUMBER")),
                    ("storage.dorado.disk.fw_ver", d.get("FIRMWAREVER")),
                    ("storage.dorado.disk.type_code", d.get("DISKTYPE")),
                    ("storage.dorado.disk.pool", d.get("POOLNAME") or d.get("POOLID")),
                    ("storage.dorado.disk.tier", d.get("POOLTIERID")),
                    ("storage.dorado.disk.temperature", d.get("TEMPERATURE")),
                    ("storage.dorado.disk.remain_life", d.get("REMAINLIFE")),
                    ("storage.dorado.disk.runtime", d.get("RUNTIME")),
                    ("storage.dorado.disk.capacity_bytes", d.get("manuCapacity")),
                    ("storage.dorado.disk.multipath", d.get("MULTIPATH")),
                    ("storage.dorado.disk.health_status", d.get("HEALTHSTATUS")),
                    ("storage.dorado.disk.running_status", d.get("RUNNINGSTATUS")),
            ):
                if _v is None or _v == "":
                    continue
                facts.append({"path": _p, "value": _v, "obj_id": loc,
                              "obj_type": "dorado_disk", "source": src, "ts": ts})

        return facts

    # ── 本地盘模式（存算一体退化）──

    def _collect_local_disks(self, start: float) -> ProbeResult:
        """存算一体退化模式：SSH 扫描本地磁盘"""
        from .os_probe import OsProbe
        os_probe = OsProbe()
        os_result = os_probe.collect()

        facts = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        src = "storage.local_disk"

        for host, node_data in os_result.raw.get("nodes", {}).items():
            disks = node_data.get("local_disks", {})
            facts.append({
                "path": "storage.local.mode",
                "value": "integrated",
                "obj_id": host, "obj_type": "node",
                "source": src, "ts": ts,
            })
            if isinstance(disks, dict) and "lines" in disks:
                for line in disks["lines"]:
                    if "disk" in line or "nvme" in line.lower():
                        parts = line.split()
                        if parts:
                            facts.append({
                                "path": "storage.local.device",
                                "value": parts[0],
                                "obj_id": host, "obj_type": "node",
                                "source": src, "ts": ts,
                            })

        return ProbeResult(
            layer="storage",
            facts=facts,
            raw={"mode": "local_disk", "nodes": os_result.raw.get("nodes", {})},
            alerts=[],
            elapsed_ms=self._elapsed(start),
            mode="local_disk",
        )


# ── 注册到全局注册表 ──
register_probe(StorageProbe())
