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


def collect_layers(layers: List[str], **kwargs) -> Dict[str, ProbeResult]:
    """按层列表执行采集，返回 {layer: ProbeResult}

    段级失败（D2）不再静默：探针 raw 中的 `<section>__error__` 会被提取到
    ProbeResult.section_errors，并保证每个失败段都有一条告警。
    """
    results = {}
    for layer in layers:
        probe = get_probe(layer)
        if probe is None:
            results[layer] = ProbeResult(
                layer=layer, facts=[], raw={}, alerts=[],
                elapsed_ms=0, error=f"未知探针: {layer}",
            )
            continue
        try:
            res = probe.collect(**kwargs)
        except Exception as e:
            results[layer] = ProbeResult(
                layer=layer, facts=[], raw={}, alerts=[],
                elapsed_ms=0, error=str(e),
            )
            continue

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
        results[layer] = res
    return results


# ── 探针注册（显式，替代原先的目录扫描）──────────────────────────────
# 原实现用 pkgutil 扫描本目录并 import_module **所有**模块。拆分后
# （docs/50）本目录还包含 collect_api / knowledge_api，扫描会：
#   · 让 import service.collect_api 连带加载 knowledge_api（分层失效）
#   · 产生循环导入（knowledge_api → collect_api → __init__ → …）
# 故改为显式导入：只需要触发各探针模块的 register_probe() 副作用。
from .probes import (  # noqa: F401,E402
    gaussdb_probe, os_probe, storage_probe, tpops_probe,
)
