"""GaussDB 运维 Agent — 四层采集探针包

层定义:
  - gaussdb : GaussDB 连接库 SQL 采集（会话/锁/SQL/复制/表统计）
  - os      : OS 层 SSH 采集（UltraPath/NVMe/RoCE/CPU/内存）
  - storage : Storage 层 DeviceManager RESTful 采集（Dorado 设备侧）
  - tpops   : TPOPS 平台采集（告警/日志/慢SQL/监控指标）

接口契约:
  所有探针继承 BaseProbe，实现 collect() 和 identity 属性。

──────────────────────────────────────────────────────────────────────────
v6.2 · D1 采集段名契约修复（2026-09-11）
──────────────────────────────────────────────────────────────────────────
问题：`ontology_collect_guide` 返回的 collect_sections 名与探针 _SQL 键
      交集几乎为空（原实测交集 5/11），Agent 严格按指引采集 → GaussDB 层 0 事实。

修复分两层（缺一不可）：
  1. 源头修正：guide 的 _SECTIONS_BY_SCENARIO 只输出探针真实支持的段键
     （见 service/main.py）。
  2. 兜底归一：本模块 SECTION_ALIASES + canonical_section() 把历史遗留的
     guide 段名（active_sessions / wait_status / top_sql / slow_sql_history）
     映射到探针段键，使**任何**入参写法都能命中真实采集实现。
  两者由 tests/test_contracts.py 的契约断言锁定，防止再次漂移。
──────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── 采集段别名表（历史 guide 段名 → 探针真实段键）──
# 左：可能被外部（旧版 guide / LLM / 历史文档）使用的段名
# 右：探针 _SQL 中真实存在的键
SECTION_ALIASES: Dict[str, str] = {
    # guide 侧历史命名 → 探针键
    "active_sessions": "session_detail",
    "wait_status": "wait_events",
    "top_sql": "slow_sql",
    "slow_sql_history": "slow_sql_detail",
    "slow_sql_detail": "slow_sql_detail",
    "connections": "connections",
    "connection_count": "connections",
    "pg_stat_activity": "sessions",
    "lock_waits": "locks",
    "blocking": "blocking_sessions",
    "long_transactions": "long_tx",
    "table_bloat": "table_stats",
    "replication_lag": "replication",
    "active_session": "session_detail",
    "wait_events": "wait_events",
    "waits": "wait_events",
    "asp": "asp",
}

# 已确认无采集实现的段名（历史上 guide 提过但探针从未支持，也无对应替代）
# 保留显式记录，使「引用了不存在的段」这件事变成可观测，而不是静默空结果。
NO_IMPL_SECTIONS: tuple = ()


def canonical_section(name: str) -> str:
    """把任意段名写法归一为探针真实段键；未识别则原样返回（由调用方报错）"""
    if not name:
        return name
    key = str(name).strip().lower()
    return SECTION_ALIASES.get(key, key)


@dataclass
class ProbeResult:
    """统一探针采集结果"""
    layer: str                          # "gaussdb" | "os" | "storage" | "tpops"
    facts: List[Dict[str, Any]]         # 标准化 Fact 数组
    raw: Dict[str, Any]                 # 原始采集数据
    alerts: List[Dict[str, Any]]        # 探针级告警
    elapsed_ms: int                     # 采集耗时
    error: Optional[str] = None         # 非 None 表示探针级故障
    mode: str = "normal"                # "normal" | "local_disk" (storage退化)
    section_errors: Dict[str, str] = field(default_factory=dict)  # 段级失败（D2）
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())


class BaseProbe(abc.ABC):
    """探针基类"""

    @abc.abstractmethod
    def collect(self, **kwargs) -> ProbeResult:
        """执行采集，返回统一结果"""
        ...

    @property
    @abc.abstractmethod
    def identity(self) -> str:
        """探针标识，如 'gaussdb', 'os', 'storage', 'tpops'"""
        ...

    @staticmethod
    def _elapsed(start: float) -> int:
        return int((time.time() - start) * 1000)

    @staticmethod
    def _make_fact(path: str, value: Any, obj_id: str, obj_type: str,
                   source: str, ts: Optional[str] = None) -> dict:
        return {
            "path": path,
            "value": value,
            "obj_id": obj_id,
            "obj_type": obj_type,
            "source": source,
            "ts": ts or datetime.now().isoformat(),
        }

    @staticmethod
    def _make_alert(severity: str, title: str, detail: str, entity: str) -> dict:
        return {
            "severity": severity,
            "title": title,
            "detail": detail,
            "entity": entity,
            "source": "probe",
            "timestamp": datetime.now().isoformat(),
        }

    @staticmethod
    def _extract_section_errors(raw: Any) -> Dict[str, str]:
        """从 raw 中提取 `<section>__error__` 键（D2 显式失败协议）"""
        out: Dict[str, str] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(k, str) and k.endswith("__error__"):
                    out[k[: -len("__error__")]] = str(v)
        return out


# ── 探针注册表 ──
_PROBE_REGISTRY: Dict[str, BaseProbe] = {}


def register_probe(probe: BaseProbe):
    """注册探针到全局注册表"""
    _PROBE_REGISTRY[probe.identity] = probe


def get_probe(layer: str) -> Optional[BaseProbe]:
    """按层名获取探针实例"""
    return _PROBE_REGISTRY.get(layer)


def get_all_probes() -> Dict[str, BaseProbe]:
    """获取所有已注册探针"""
    return dict(_PROBE_REGISTRY)


# ★ 层间并行：各层探针**互相独立**，层与层之间没有数据依赖。
#   实测（2026-09-21，生产机）四层串行的耗时约等于各层之和：
#       gaussdb 1.1s + tpops 0.2s + os 5.1s + storage 6.1s
#   并行后理论上限 = **最慢的那一层**。
#
#   ★ 为什么并行是安全的（逐条核实过，不是"应该没事"）：
#     · 探针实例**无状态**：不缓存连接/游标 ——
#       gaussdb 每次 collect 内 `asyncio.new_event_loop()` + 新建连接；
#       tpops / storage 每次请求各自 urllib / 新建客户端；
#       os 层自身已经是多线程（命令级 9 并发 + 节点级并行）。
#     · 各层写自己的 `results[layer]`，互不共享可变状态。
#   ★ 保持**结果字典的键顺序**与 layers 入参一致（消费方可能按序展示）。
#   ★ 单层异常仍只影响该层（与串行版逐字同语义），不因并行而扩散。
def collect_layers(layers: List[str], *, parallel: bool = True,
                   **kwargs) -> Dict[str, ProbeResult]:
    """按层列表执行采集，返回 {layer: ProbeResult}

    段级失败（D2）不再静默：探针 raw 中的 `<section>__error__` 会被提取到
    ProbeResult.section_errors，并保证每个失败段都有一条告警。

    parallel=True（默认）时**层间并行**；置 False 可回到串行（排障用）。
    两种模式的结果逐字相同 —— 只改耗时，不改内容。
    """
    def _one(layer: str) -> ProbeResult:
        probe = get_probe(layer)
        if probe is None:
            return ProbeResult(
                layer=layer, facts=[], raw={}, alerts=[],
                elapsed_ms=0, error=f"未知探针: {layer}",
            )
        try:
            res = probe.collect(**kwargs)
        except Exception as e:
            return ProbeResult(
                layer=layer, facts=[], raw={}, alerts=[],
                elapsed_ms=0, error=str(e),
            )

        # ★ D2：把段级失败提升为结构化字段 + 保证至少一条告警
        sec_err = BaseProbe._extract_section_errors(getattr(res, "raw", None))
        if sec_err:
            res.section_errors = sec_err
            titled = {a.get("title", "") for a in (res.alerts or [])}
            for sec, err in sec_err.items():
                if f"采集段失败: {sec}" in titled:
                    continue
                res.alerts.append({
                    "severity": "warning",
                    "title": f"采集段失败: {sec}",
                    "detail": f"{layer} 层采集段 `{sec}` 执行失败，该段事实为 0：{err}",
                    "entity": layer,
                    "source": f"{layer}_probe",
                    "timestamp": datetime.now().isoformat(),
                    "section": sec,
                    "error": err,
                })
        return res

    order = list(layers)
    if not parallel or len(order) <= 1:
        return {ly: _one(ly) for ly in order}

    from concurrent.futures import ThreadPoolExecutor

    # ★ 并发数 = 层数（最多 4），不再放大：层内各自已有并发，
    #   再叠加会去争抢远端 sshd / 设备端的连接配额。
    try:
        with ThreadPoolExecutor(max_workers=len(order)) as ex:
            got = list(ex.map(_one, order))
    except Exception:                                            # noqa: BLE001
        # 并行框架本身出问题 ⇒ 回落串行（结果不变，只是慢）
        return {ly: _one(ly) for ly in order}
    return {ly: r for ly, r in zip(order, got)}


# ── ★ .env 必须在本目录探针被导入之前加载 ──────────────────────────
# 各探针在**模块级**读环境变量并即时实例化：
#   gaussdb_probe.py  register_probe(GaussdbProbe()) ← __init__ 读 GAUSSDB_DSN
#   os_probe.py       _SSH_PASS = os.environ.get("GAUSSDB_SSH_PASSWORD")   （模块级）
#   tpops_probe.py    _DEFAULT_TPOPS_PASS = os.environ.get("TPOPS_PASSWORD")（模块级）
# 这些值一旦求值即**冻结**，之后加载 .env 也不会自愈。
#
# 实测事故（2026-09-19 拆分部署后，静默）：
#   旧单体 main.py 的导入顺序凑巧是「先 _load_env_file、后导入探针」——
#   因为 service/__init__.py 用 pkgutil 扫目录，且字母序 main < probes。
#   拆成显式导入（from .probes import ...）后该巧合消失：任何模块只要先
#   `import service.common`，就会触发本包导入，而那时 .env 尚未加载 ——
#       gaussdb 探针 host=None → 连 127.0.0.1:8000 → **gaussdb 层 0 事实**
#       tpops  探针密码为空   → 登录失败
#   症状是静默的：/health 走 collect_api 自己解析的 _conn_kwargs，仍报 ok=true。
#
# 修法：把 .env 加载放进本包，使探针取值**不再依赖调用方的导入顺序**。
#   （不用 from service.common import _load_env_file —— 会绕回 service/__init__
#    造成循环导入；本包自持一份同语义的极简加载器。）
def _probes_load_env_file(_path: str) -> None:
    """极简 .env 读取器（与 service.common._load_env_file 同语义）"""
    if not _os.path.isfile(_path):
        return
    with open(_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _os.environ[_k.strip()] = _v.strip().strip('"').strip("'")


# ── 自动导入所有探针模块（确保 register_probe 被触发） ──
import importlib as _il
import os as _os
import pkgutil as _pu

_PKG_DIR = _os.path.dirname(_os.path.abspath(__file__))

# ★ .env 加载必须在此处、**早于**下面的 import_module —— 探针模块级读 env 的时机
_probes_load_env_file(_os.path.join(_os.path.dirname(_PKG_DIR), ".env"))                     # service/.env
_probes_load_env_file(_os.path.join(_os.path.dirname(_os.path.dirname(_PKG_DIR)), ".env"))   # 仓库根 .env
for _l, _mn, _ip in _pu.iter_modules([_PKG_DIR]):
    if _mn == "__init__":
        continue
    try:
        _il.import_module(f".{_mn}", __package__)
    except Exception:
        pass  # 单个探针失败不影响其他
