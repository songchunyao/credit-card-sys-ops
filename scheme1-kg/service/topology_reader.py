# -*- coding: utf-8 -*-
"""M2.9 · 方案3（知识图谱直引 + LLM）· 拓扑直读接口（只读）

权威规格
--------
* docs/09-方案3设计（知识图谱直引+LLM）.md
    §3.1 get_object_topology / §3.2 get_fault_topology / §3.3 get_metrics / §3.4 get_facts
    §3.6 载荷纪律（截断必须显式上报）  §4 诚实降级  §5.2 有界 BFS 三条不可让步性质
* shared/model/kgt-tools.yaml            4 个工具的出/入参 + hard_limits + forbidden_implementations
* shared/model/topology.yaml             14 类型 / 15 关系 / topology_facts.hop_path
* shared/model/causal-edges.yaml         22 条因果边 / meta.cycles（唯一有向环）
* shared/model/fault-modes.yaml          18 条故障模式（节点字段的真源）
* shared/model/metrics.yaml + shared/mapping/type-map.yaml  指标码 → lowerCamel 属性
* shared/mapping/type-map.yaml / rel-map.yaml  命名权威（前缀 / 标签 / kg_reltype）

不可让步的实现约束
------------------
1. 只读：本模块对数据库只下 MATCH，任何写语句都不出现（可被静态检索验证）。
2. 禁变长路径：不使用任何形式的变长路径枚举（kgt-tools.yaml `forbidden_implementations`）。
   深度截断与去环全部在 Python 侧做有界 BFS。
3. 深度语义 X16：对象拓扑的硬上限 = `len(topology_facts.hop_path) - 1`（当前 9），
   **不是** `topology_facts.max_hop_depth`（10，该口径含派生注释边 Table → Evidence）。
4. 派生边默认排除：`traversal_scope.excludes_derived_relations = [HAS_EVIDENCE]`，
   需显式开关才返回（默认关）。
5. 故障拓扑：深度硬上限 4（夹取，不报错）；有向环只允许白名单内的成员，环一律记录上报；
   `relates_to` 边原样返回但绝不沿它继续展开；不做概率合成、不写回任何学习计数。
6. 截断必须上报：任何被行上限 / 字节上限 / 单值长度上限裁掉的内容，
   都在返回体里带「截断项、原始条数、上限值」的报告。
7. 门面形状（可执行契约，不是注释）：数组出参（get_metrics / get_facts）在门面层
   （HTTP `:8303` / Dify 工具）必须经 `to_facade()` 包成
   `{"items": [...], "truncated": bool, "truncation": {...}, "warnings": [...], "audit": [...]}`；
   直接 `json.dumps(list)` 会丢掉截断事实，门面层不得这么做。
8. 快照 id 一律从图上 `:CC_Meta.snapshotId` 读（`get_facts` 逐行取证据节点属性），
   **任何地方都不硬编码** `ccf-…` 字面量 —— 契约重冻结后读数必须自动跟随。

范围隔离：本文件只依赖标准库 + 第三方驱动（`neo4j` / `yaml`）+ 只读的契约 YAML 数据文件；
不导入任何项目内 Python 模块（层间 import 边据此保持 0 条）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[2]

# 载荷上限（冻结值；口径见 docs/03 §7.2 G5/G6 与 docs/09 §3.6）
ROW_LIMIT = 200
BYTE_LIMIT = 20000
DEFAULT_STR_LIMIT = 120

# 条件边的属性键（docs/03 §5.3：条件边靠属性表达，关系类型仍是同一个因果类型）
# `condValue` = 契约/图上的历史属性名，`condRef` = 2026-10-06 边属性契约修正后的图属性名，
# 两者同义（都存 `condition.operand`，如 `threshold:qps_high#critical`），读取时都认。
CONDITION_PROPS = ("condMetric", "condOp", "condValue", "condRef", "condKind")

# 因果关系的 passthrough 名（docs/09 §3.2 relation_passthrough）
REL_CAUSES = "causes"
REL_CONDITIONAL = "conditional_causes"
REL_RELATES = "relates_to"


class ContractError(RuntimeError):
    """契约缺失 / 自相矛盾时抛出（宁可显式失败，不静默降级）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _read_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _dumps(payload: Any) -> str:
    """紧凑无缩进序列化（字节口径以此为准）。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _byte_len(payload: Any) -> int:
    return len(_dumps(payload).encode("utf-8"))


def _clip(value: Any, limit: int) -> Tuple[Any, bool]:
    """单值长度上限（MAX_STR）。非字符串原样返回。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit], True
    return value, False


class ReportList(list):
    """list 子类：保持 §3.3/§3.4 的 array 出参形态，同时携带必须上报的截断清单。

    docs/09 §3.3/§3.4 把出参定义为数组，而 §3.6 与
    kgt-tools.yaml `payload_policy.truncation.must_report: true` 要求截断可被看见：
    数组本身没有放字段的位置，故用 list 子类承载 `truncation` / `warnings` / `audit`，
    既不破坏 array schema，也不让截断静默。
    """

    def __init__(self, items: Iterable[Any] = (), *, truncation: Optional[dict] = None,
                 warnings: Optional[Sequence[str]] = None, audit: Optional[Sequence[dict]] = None,
                 extra: Optional[dict] = None) -> None:
        super().__init__(items)
        self.truncation: Dict[str, Any] = truncation or {"truncated": False}
        self.warnings: List[str] = list(warnings or [])
        self.audit: List[dict] = list(audit or [])
        # 数组出参本身放不下额外事实（如「指标元数据来自 :CC_Metric 还是对象属性」），
        # 统一挂在 meta 上，由门面层（to_facade）透出。
        self.extra: Dict[str, Any] = dict(extra or {})

    @property
    def truncated(self) -> bool:
        return bool(self.truncation.get("truncated"))

    def meta(self) -> Dict[str, Any]:
        payload = {
            "count": len(self),
            "truncated": self.truncated,
            "truncation": dict(self.truncation),
            "warnings": list(self.warnings),
            "audit": [dict(item) for item in self.audit],
        }
        payload.update(self.extra)
        return payload


# ---------------------------------------------------------------------------
# 契约（只读加载；映射是数据不是代码）
# ---------------------------------------------------------------------------

class Contract:
    """把 7 份契约里本模块需要的字段读成一个对象。"""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = Path(root)
        self.reload()

    def reload(self) -> None:
        model = self.root / "shared" / "model"
        mapping = self.root / "shared" / "mapping"
        self.tools = _read_yaml(model / "kgt-tools.yaml")
        self.topology = _read_yaml(model / "topology.yaml")
        self.metrics_contract = _read_yaml(model / "metrics.yaml")
        self.fault_contract = _read_yaml(model / "fault-modes.yaml")
        self.causal_contract = _read_yaml(model / "causal-edges.yaml")
        self.type_map = _read_yaml(mapping / "type-map.yaml")
        self.rel_map = _read_yaml(mapping / "rel-map.yaml")
        with (model / "ccf.schema.json").open("r", encoding="utf-8") as handle:
            self.ccf_schema = json.load(handle)
        self.deploy = _read_yaml(self.root / "shared" / "deploy" / "targets.yaml")

        # ---- 命名（type-map namespaces.kg 是权威）----
        namespaces = (self.type_map.get("namespaces") or {}).get("kg") or {}
        self.label_prefix = namespaces.get("label_prefix", "CC_")
        self.reltype_prefix = namespaces.get("reltype_prefix", "CC_")
        self.id_prop = namespaces.get("instance_id_property", "ccfId")
        self.type_prop = namespaces.get("neutral_type_property", "ccfType")
        self.layer_prop = namespaces.get("layer_property", "ccfLayer")
        self.snapshot_prop = namespaces.get("snapshot_property", "ccfSnapshotId")
        self.reserved_props = (self.id_prop, self.type_prop, self.layer_prop, self.snapshot_prop)

        payload_policy = (self.ccf_schema.get("properties") or {}).get("payloadPolicy") or {}
        payload_props = payload_policy.get("properties") or {}
        self.str_limit = int((payload_props.get("MAX_STR") or {}).get("const", DEFAULT_STR_LIMIT))

        # ---- 类型（topology.yaml#types 是**列表**，以 `code` 为键）----
        self.layers = list(((self.topology.get("meta") or {}).get("layer_enum")) or ["App", "Runtime", "Data"])
        self.type_defs: Dict[str, dict] = {
            str(item.get("code")): item for item in (self.topology.get("types") or []) if item.get("code")
        }
        tm_types = self.type_map.get("types") or {}
        self.label_of: Dict[str, str] = {}
        for type_name in self.type_defs:
            kg_label = (tm_types.get(type_name) or {}).get("kg_label")
            self.label_of[type_name] = kg_label or (self.label_prefix + type_name)
        self.type_of_label = {label: name for name, label in self.label_of.items()}
        self.object_types = [
            name for name, spec in self.type_defs.items()
            if not (spec or {}).get("abstract") and not (spec or {}).get("annotation")
        ]
        self.annotation_types = [name for name, spec in self.type_defs.items() if (spec or {}).get("annotation")]
        self.evidence_type = self.annotation_types[0] if self.annotation_types else "Evidence"
        self.evidence_label = self.label_of.get(self.evidence_type, self.label_prefix + "Evidence")
        self.fault_label = self.label_prefix + "FaultMode"
        # TBox 标签（v1.0.1 起真机已持久化：CC_Metric / CC_Threshold / CC_Meta）。
        # 缺失时相关查询只返回 0 行，不报错；读取方一律「优先 TBox、回退对象属性」并显式标注来源。
        self.metric_label = self.label_prefix + "Metric"
        self.threshold_label = self.label_prefix + "Threshold"
        self.meta_label = self.label_prefix + "Meta"
        # 抽象父类型 → 具体子类型（topology.yaml 的 parent 键；抽象类不落图，只用于按类型核验）
        self.parent_of: Dict[str, str] = {}
        for type_name, spec in self.type_defs.items():
            parent = (spec or {}).get("parent")
            if parent:
                self.parent_of[type_name] = str(parent)
        self.subtypes_of: Dict[str, List[str]] = {}
        for child, parent in self.parent_of.items():
            self.subtypes_of.setdefault(parent, []).append(child)
        for children in self.subtypes_of.values():
            children.sort()

        # ---- 关系（rel-map 是权威）----
        self.relations = self.rel_map.get("relations") or {}
        self.inverse_relations = self.rel_map.get("inverse_relations") or {}
        self.reltype_of: Dict[str, str] = {}
        self.neutral_of: Dict[str, str] = {}
        for code, spec in self.relations.items():
            reltype = (spec or {}).get("kg_reltype") or (self.reltype_prefix + code)
            self.reltype_of[code] = reltype
            self.neutral_of[reltype] = code
        # 契约里显式声明的反向关系（运行时靠反向匹配，不建边）
        self.inverse_reltype_of: Dict[str, str] = {}
        for code, spec in self.inverse_relations.items():
            inverse_reltype = (spec or {}).get("kg_reltype") or (self.reltype_prefix + code)
            self.inverse_reltype_of[code] = inverse_reltype
            self.neutral_of.setdefault(inverse_reltype, code)

        object_tool = self._tool("get_object_topology")
        traversal_scope = object_tool.get("traversal_scope") or {}
        self.derived_codes = list(traversal_scope.get("excludes_derived_relations") or [])
        self.derived_reltypes = {
            self.reltype_of.get(code, self.reltype_prefix + code) for code in self.derived_codes
        }
        self.business_reltypes = {
            reltype for code, reltype in self.reltype_of.items() if code not in self.derived_codes
        }
        self.cross_layer_reltypes = {
            self.reltype_of[code] for code, spec in self.relations.items() if (spec or {}).get("cross_layer")
        }
        # 跨层 / 反向关系的中性名（出参里写中性名，不写 CC_ 载体名）
        self.cross_layer_relations = sorted(
            code for code, spec in self.relations.items() if (spec or {}).get("cross_layer")
        )
        self.inverse_relation_codes = sorted(self.inverse_relations.keys())
        # kgt-tools.yaml#get_object_topology.traversal_scope 原样留档（出参里回显遍历口径）
        self.traversal_scope = dict(traversal_scope)

        # ---- X16：对象拓扑深度语义 ----
        facts = self.topology.get("topology_facts") or {}
        self.hop_path = list(facts.get("hop_path") or [])
        self.business_hop_depth = max(0, len(self.hop_path) - 1)
        hard_limits = object_tool.get("hard_limits") or {}
        defaults = object_tool.get("defaults") or {}
        self.contract_object_max_depth = hard_limits.get("maxHopDepth")
        self.contract_object_default_depth = defaults.get("maxHopDepth")
        self.must_reach_paths = list(object_tool.get("must_reach_paths") or [])

        # ---- 故障拓扑 ----
        fault_tool = self._tool("get_fault_topology")
        self.fault_max_hops = int((fault_tool.get("hard_limits") or {}).get("max_hops", 4))
        self.fault_default_hops = int((fault_tool.get("defaults") or {}).get("max_hops", 4))
        self.relation_passthrough = list(
            fault_tool.get("relation_passthrough") or [REL_CAUSES, REL_CONDITIONAL, REL_RELATES]
        )
        # 因果关系的载体名：causes / conditional_causes 同属一个因果类型，relates_to 独立。
        # 命名规则（前缀 + 中性 UPPER_SNAKE）与 shared/verify/kg_tbox_check.py:647 的映射一致。
        self.causes_reltype = self.reltype_prefix + "CAUSES"
        self.relates_reltype = self.reltype_prefix + "RELATES_TO"
        self.fault_reltypes = [self.causes_reltype, self.relates_reltype]
        self.symmetric_reltypes = [self.relates_reltype]
        causal_meta = self.causal_contract.get("meta") or {}
        self.causal_transitive = bool(causal_meta.get("transitive", False))
        self.cycle_whitelist: List[List[str]] = [list(cycle) for cycle in (causal_meta.get("cycles") or [])]
        self.cycle_whitelist_sets = [frozenset(cycle) for cycle in self.cycle_whitelist]
        self.causal_edges = list(self.causal_contract.get("edges") or [])
        self.causal_edge_of: Dict[str, dict] = {
            str(edge.get("id")): edge for edge in self.causal_edges if edge.get("id")
        }
        self.leaf_root_causes = self.causal_contract.get("leaf_root_causes") or {}

        # ---- 故障模式目录 ----
        self.fault_modes: Dict[str, dict] = {
            str(mode.get("code")): mode for mode in (self.fault_contract.get("fault_modes") or [])
        }
        # 阈值本体（fault-modes.yaml#thresholds；`warning`/`critical` 可为 null = 未标定）。
        # 真机上优先从 :CC_Threshold 读（thrCode/thrWarning/thrCritical），YAML 只作回退。
        self.thresholds: Dict[str, dict] = {
            str(item.get("name")): item
            for item in (self.fault_contract.get("thresholds") or [])
            if item.get("name")
        }

        # ---- 指标目录 ----
        self.metric_contract: Dict[str, dict] = {
            str(metric.get("code")): metric for metric in (self.metrics_contract.get("metrics") or [])
        }
        self.metric_projection: Dict[str, dict] = self.type_map.get("metrics") or {}
        self.metrics_of_type: Dict[str, List[str]] = {}
        for code, projection in self.metric_projection.items():
            self.metrics_of_type.setdefault(str((projection or {}).get("object")), []).append(code)
        for codes in self.metrics_of_type.values():
            codes.sort()

        # ---- 连接参数（targets.yaml 是数据；口令一律从环境变量取）----
        neo4j = (self.deploy.get("neo4j") or {}) if isinstance(self.deploy, dict) else {}
        self.neo4j = neo4j
        self.default_uri = neo4j.get("bolt_uri")
        self.default_user = (neo4j.get("auth") or {}).get("user", "neo4j")
        self.password_env = (neo4j.get("auth") or {}).get("password_env", "CC_NEO4J_PASSWORD")
        databases = ((neo4j.get("limits") or {}).get("databases")) or ["neo4j"]
        self.default_database = databases[0]

    # -- 取工具定义 ---------------------------------------------------------
    def _tool(self, name: str) -> dict:
        """按 kgt-tools.yaml#tools[].tool_name 取工具定义（该文件用 `tool_name`，不是 `name`）。"""
        for tool in self.tools.get("tools") or []:
            if tool.get("tool_name") == name:
                return tool
        return {}

    def expand_types(self, type_name: str) -> List[str]:
        """类型本身 + 其全部具体子类型（沿 `parent` 展开）。

        抽象父类型（`ConnectionPool`，`abstract: true`）不落图，故按类型核验必达链路时
        必须把父类型展开成具体子类型（`JdbcDataSource` / `DatabaseSessionPool`）。
        """
        seen: List[str] = []
        stack = [type_name]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.append(current)
            stack.extend(self.subtypes_of.get(current, []))
        return seen

    # -- X16 自检 -----------------------------------------------------------
    def depth_drift(self) -> List[str]:
        """返回深度口径漂移说明；空列表 = 一致。

        对象拓扑硬上限必须等于 `len(hop_path) - 1`；kgt-tools.yaml 的 hard_limits
        与 defaults 必须与之一致。任一项不等 = 契约漂移，必须显式报出而不是照抄。
        """
        drift: List[str] = []
        if self.contract_object_max_depth != self.business_hop_depth:
            drift.append(
                "kgt-tools.yaml hard_limits.maxHopDepth=%r 与 len(hop_path)-1=%d 不一致"
                % (self.contract_object_max_depth, self.business_hop_depth)
            )
        if self.contract_object_default_depth != self.business_hop_depth:
            drift.append(
                "kgt-tools.yaml defaults.maxHopDepth=%r 与 len(hop_path)-1=%d 不一致"
                % (self.contract_object_default_depth, self.business_hop_depth)
            )
        declared = ((self.topology.get("topology_facts") or {}).get("max_hop_depth"))
        expected_declared = self.business_hop_depth + len(self.derived_codes)
        if declared != expected_declared:
            drift.append(
                "topology_facts.max_hop_depth=%r 与 len(hop_path)-1+派生边数=%d 不一致"
                % (declared, expected_declared)
            )
        return drift


_CONTRACT_CACHE: Dict[str, Contract] = {}


def get_contract(root: Path = ROOT) -> Contract:
    key = str(Path(root))
    if key not in _CONTRACT_CACHE:
        _CONTRACT_CACHE[key] = Contract(Path(root))
    return _CONTRACT_CACHE[key]


# ---------------------------------------------------------------------------
# 后端：真机（Neo4j 只读）
# ---------------------------------------------------------------------------

class GraphBackend:
    """Neo4j 只读后端。所有语句都是 MATCH … RETURN，不含任何写子句。"""

    name = "neo4j"

    def __init__(self, driver: Any, database: str = "neo4j", contract: Optional[Contract] = None) -> None:
        self.driver = driver
        self.database = database
        self.contract = contract or get_contract()
        self.audit: List[dict] = []

    # -- 连接 ---------------------------------------------------------------
    @classmethod
    def connect(cls, uri: Optional[str] = None, user: Optional[str] = None,
                password: Optional[str] = None, database: Optional[str] = None,
                contract: Optional[Contract] = None) -> "GraphBackend":
        contract = contract or get_contract()
        try:
            from neo4j import GraphDatabase  # 延迟导入：离线（契约 fixture）路径不需要驱动
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("需要 neo4j 驱动才能连真机：%s" % exc) from exc
        uri = uri or os.environ.get("CC_NEO4J_URI") or os.environ.get("GAUSSDB_NEO4J_URI") or contract.default_uri
        user = user or os.environ.get("CC_NEO4J_USER") or os.environ.get("GAUSSDB_NEO4J_USER") or contract.default_user
        password = password or os.environ.get(contract.password_env)
        if not password:
            raise RuntimeError(
                "缺少口令：请设置环境变量 %s（或 --password）" % contract.password_env
            )
        database = database or os.environ.get("CC_NEO4J_DATABASE") or os.environ.get("GAUSSDB_NEO4J_DB") or contract.default_database
        try:
            driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        except TypeError:  # 老版本驱动没有该关键字
            driver = GraphDatabase.driver(uri, auth=(user, password))
        return cls(driver, database=database, contract=contract)

    def close(self) -> None:
        try:
            self.driver.close()
        except Exception:
            pass

    # -- 执行（审计：queryText / elapsedMs / rowCount，供服务侧逐调用落盘）----
    def _run(self, cypher: str, **params: Any) -> List[dict]:
        start = _now_ms()
        with self.driver.session(database=self.database) as session:
            records = [record.data() for record in session.run(cypher, **params)]
        self.audit.append({
            "queryText": " ".join(cypher.split()),
            "elapsedMs": round(_now_ms() - start, 3),
            "rowCount": len(records),
        })
        return records

    # -- 取数 ---------------------------------------------------------------
    def fetch_objects(self) -> Dict[str, dict]:
        rows = self._run(
            "MATCH (n) WHERE n.%s IN $types "
            "RETURN n.%s AS objId, n.%s AS objType, n.%s AS layer, properties(n) AS props"
            % (self.contract.type_prop, self.contract.id_prop, self.contract.type_prop, self.contract.layer_prop),
            types=sorted(self.contract.object_types),
        )
        return {row["objId"]: row for row in rows if row.get("objId")}

    def fetch_business_edges(self) -> List[dict]:
        return self._run(
            "MATCH (a)-[r]->(b) WHERE type(r) IN $rels AND a.%s IN $types AND b.%s IN $types "
            "RETURN a.%s AS `from`, type(r) AS reltype, b.%s AS `to`, properties(r) AS props"
            % (self.contract.type_prop, self.contract.type_prop, self.contract.id_prop, self.contract.id_prop),
            rels=sorted(self.contract.business_reltypes | self.contract.derived_reltypes),
            types=sorted(self.contract.object_types),
        )

    def fetch_derived_edges(self, obj_ids: Sequence[str]) -> List[dict]:
        if not obj_ids:
            return []
        return self._run(
            "MATCH (o)-[r]->(e) WHERE type(r) IN $rels AND o.%s IN $ids "
            "RETURN o.%s AS `from`, type(r) AS reltype, e.%s AS `to`, e.%s AS objType, properties(e) AS props"
            % (self.contract.id_prop, self.contract.id_prop, self.contract.id_prop, self.contract.type_prop),
            rels=sorted(self.contract.derived_reltypes),
            ids=list(obj_ids),
        )

    def fetch_fault_modes(self, codes: Sequence[str]) -> Dict[str, dict]:
        if not codes:
            return {}
        rows = self._run(
            "MATCH (fm:%s) WHERE fm.fmCode IN $codes "
            "RETURN fm.fmCode AS fmCode, properties(fm) AS props" % self.contract.fault_label,
            codes=list(codes),
        )
        return {row["fmCode"]: row["props"] for row in rows if row.get("fmCode")}

    def fetch_causal_edges(self) -> List[dict]:
        label = self.contract.fault_label
        return self._run(
            "MATCH (a:%s)-[r]->(b:%s) WHERE type(r) IN $rels "
            "RETURN a.fmCode AS `from`, type(r) AS reltype, b.fmCode AS `to`, properties(r) AS props"
            % (label, label),
            rels=sorted(self.contract.fault_reltypes),
        )

    def fetch_metric_timestamps(self, obj_ids: Sequence[str], codes: Sequence[str]) -> Dict[Tuple[str, str], Any]:
        if not obj_ids or not codes:
            return {}
        rows = self._run(
            "MATCH (o)-[r]->(e) WHERE type(r) IN $rels AND o.%s IN $ids AND e.metricCode IN $codes "
            "RETURN o.%s AS objId, e.metricCode AS metricCode, max(e.capturedAt) AS ts"
            % (self.contract.id_prop, self.contract.id_prop),
            rels=sorted(self.contract.derived_reltypes),
            ids=list(obj_ids),
            codes=list(codes),
        )
        return {(row["objId"], row["metricCode"]): row["ts"] for row in rows if row.get("objId")}

    def fetch_evidence(self, object_type: str, object_id: Optional[str] = None,
                       metric_codes: Optional[Sequence[str]] = None,
                       time_from: Optional[str] = None, time_to: Optional[str] = None) -> List[dict]:
        clauses = ["o.%s = $objectType" % self.contract.type_prop, "type(r) IN $rels"]
        params: Dict[str, Any] = {
            "objectType": object_type,
            "rels": sorted(self.contract.derived_reltypes),
        }
        if object_id:
            clauses.append("o.%s = $objectId" % self.contract.id_prop)
            params["objectId"] = object_id
        if metric_codes:
            clauses.append("e.metricCode IN $metricCodes")
            params["metricCodes"] = list(metric_codes)
        if time_from:
            clauses.append("e.capturedAt >= $timeFrom")
            params["timeFrom"] = time_from
        if time_to:
            clauses.append("e.capturedAt <= $timeTo")
            params["timeTo"] = time_to
        return self._run(
            "MATCH (o)-[r]->(e) WHERE %s "
            "RETURN o.%s AS objectId, e.%s AS recordId, properties(e) AS props "
            "ORDER BY e.capturedAt, e.%s"
            % (" AND ".join(clauses), self.contract.id_prop, self.contract.id_prop, self.contract.id_prop),
            **params,
        )

    # -- TBox（v1.0.1 起真机已持久化；只读 MATCH，缺失即 0 行）----------------
    def fetch_metric_catalog(self) -> Dict[str, dict]:
        """`:CC_Metric` 指标目录（元数据；**不含值**——值的权威来源仍是对象属性）。"""
        rows = self._run(
            "MATCH (m:%s) RETURN m.metricCode AS metricCode, properties(m) AS props" % self.contract.metric_label
        )
        return {row["metricCode"]: row["props"] for row in rows if row.get("metricCode")}

    def fetch_thresholds(self) -> Dict[str, dict]:
        """`:CC_Threshold` 阈值本体（thrCode / thrWarning / thrCritical）。"""
        rows = self._run(
            "MATCH (t:%s) RETURN t.thrCode AS thrCode, properties(t) AS props" % self.contract.threshold_label
        )
        return {row["thrCode"]: row["props"] for row in rows if row.get("thrCode")}

    def fetch_graph_meta(self) -> Dict[str, Any]:
        """`:CC_Meta`（快照 id 的权威来源；自测与报告一律读它，不硬编码 snapshotId）。"""
        rows = self._run("MATCH (m:%s) RETURN properties(m) AS props LIMIT 1" % self.contract.meta_label)
        return dict(rows[0]["props"]) if rows and rows[0].get("props") else {}

    def fetch_metric_history(self, codes: Optional[Sequence[str]] = None) -> List[dict]:
        """证据行的观测序列（用于 `increased`/`decreased` 这类需要时间差的条件判定）。"""
        clauses = ["type(r) IN $rels"]
        params: Dict[str, Any] = {"rels": sorted(self.contract.derived_reltypes)}
        if codes:
            clauses.append("e.metricCode IN $codes")
            params["codes"] = sorted(set(codes))
        return self._run(
            "MATCH (o)-[r]->(e) WHERE %s "
            "RETURN o.%s AS objectId, o.%s AS objType, e.metricCode AS metricCode, "
            "e.rawValue AS value, e.capturedAt AS capturedAt, e.recordId AS recordId "
            "ORDER BY e.metricCode, o.%s, e.capturedAt"
            % (" AND ".join(clauses), self.contract.id_prop, self.contract.type_prop, self.contract.id_prop),
            **params,
        )

    # -- 只读自证 -----------------------------------------------------------
    def census(self) -> Dict[str, Any]:
        """节点/边普查（只读），用于自测与「图空 vs 0 行」的区分。"""
        contract = self.contract
        node_rows = self._run(
            "MATCH (n) WHERE n.%s IS NOT NULL RETURN n.%s AS t, count(*) AS c ORDER BY t"
            % (contract.type_prop, contract.type_prop)
        )
        edge_rows = self._run(
            "MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH $prefix RETURN type(r) AS t, count(*) AS c ORDER BY t",
            prefix=contract.reltype_prefix,
        )
        # 标签普查：`nodesByType` 只看得见带 `ccfType` 的 ABox 节点，TBox（Metric/Threshold/
        # FaultMode/Meta）没有该属性 ⇒ 必须另按标签计数，否则会把 TBox 当作不存在。
        label_rows = self._run(
            "MATCH (n) UNWIND labels(n) AS l WITH l, count(*) AS c "
            "WHERE l STARTS WITH $prefix RETURN l AS t, c ORDER BY t",
            prefix=contract.label_prefix,
        )
        meta = self.fetch_graph_meta()
        return {
            "nodesByType": {row["t"]: row["c"] for row in node_rows},
            "edgesByType": {row["t"]: row["c"] for row in edge_rows},
            "nodesByLabel": {row["t"]: row["c"] for row in label_rows},
            "nodeTotal": sum(row["c"] for row in node_rows),
            "edgeTotal": sum(row["c"] for row in edge_rows),
            "nodeTotalByLabel": sum(row["c"] for row in label_rows),
            "snapshotId": meta.get("snapshotId"),
            "graphMeta": meta,
        }


# ---------------------------------------------------------------------------
# 后端：契约 fixture（离线；无实例数据，只覆盖因果结构）
# ---------------------------------------------------------------------------

class ContractBackend:
    """离线后端：用 causal-edges.yaml + fault-modes.yaml 复原因果结构。

    用途：真机尚未装载故障模式节点时（本机实测即如此），仍可端到端验证
    `get_fault_topology` 的有界 BFS、去环、环白名单与深度夹取。
    它**不含任何对象实例数据**，因此对象拓扑接口对它一律返回空 + 显式告警，
    避免把契约级结构冒充成真机事实。
    """

    name = "contract-fixture"

    def __init__(self, contract: Optional[Contract] = None) -> None:
        self.contract = contract or get_contract()
        self.audit: List[dict] = []

    def close(self) -> None:
        return None

    def fetch_objects(self) -> Dict[str, dict]:
        return {}

    def fetch_business_edges(self) -> List[dict]:
        return []

    def fetch_derived_edges(self, obj_ids: Sequence[str]) -> List[dict]:
        return []

    def fetch_fault_modes(self, codes: Sequence[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for code in codes:
            mode = self.contract.fault_modes.get(code)
            if mode:
                out[code] = {
                    "fmCode": code,
                    "fmName": mode.get("name"),
                    "fmLayer": mode.get("layer"),
                    "fmSeverity": mode.get("severity"),
                    "fmComponentType": mode.get("component_type"),
                    "fmChain": mode.get("chain"),
                }
        return out

    def fetch_causal_edges(self) -> List[dict]:
        rows: List[dict] = []
        for edge in self.contract.causal_edges:
            relation = str(edge.get("relation") or REL_CAUSES)
            props: Dict[str, Any] = {"contractId": edge.get("id"), "relation": relation}
            condition = edge.get("condition")
            if isinstance(condition, dict):
                props["condMetric"] = condition.get("metric")
                props["condOp"] = condition.get("op")
                props["condRef"] = condition.get("operand")
                props["condKind"] = condition.get("kind")
            if edge.get("chain") is not None:
                props["chain"] = edge.get("chain")
            if edge.get("evidence") is not None:
                props["evidence"] = edge.get("evidence")
            if edge.get("provenance") is not None:
                props["provenance"] = edge.get("provenance")
            rows.append({
                "from": edge.get("from"),
                "reltype": self.contract.causes_reltype if relation != REL_RELATES else self.contract.relates_reltype,
                "to": edge.get("to"),
                "props": props,
            })
        return rows

    def fetch_metric_timestamps(self, obj_ids: Sequence[str], codes: Sequence[str]) -> Dict[Tuple[str, str], Any]:
        return {}

    def fetch_evidence(self, object_type: str, object_id: Optional[str] = None,
                       metric_codes: Optional[Sequence[str]] = None,
                       time_from: Optional[str] = None, time_to: Optional[str] = None) -> List[dict]:
        return []

    # -- TBox 回退（离线 fixture 无实例数据；阈值仍可从 YAML 读到）------------
    def fetch_metric_catalog(self) -> Dict[str, dict]:
        return {}

    def fetch_thresholds(self) -> Dict[str, dict]:
        return {code: dict(spec) for code, spec in self.contract.thresholds.items()}

    def fetch_graph_meta(self) -> Dict[str, Any]:
        return {}

    def fetch_metric_history(self, codes: Optional[Sequence[str]] = None) -> List[dict]:
        return []

    def census(self) -> Dict[str, Any]:
        return {
            "nodesByType": {},
            "edgesByType": {self.contract.causes_reltype: len(self.contract.causal_edges)},
            "nodesByLabel": {},
            "nodeTotal": len(self.contract.fault_modes),
            "edgeTotal": len(self.contract.causal_edges),
            "nodeTotalByLabel": 0,
            "snapshotId": None,
            "graphMeta": {},
            "mode": "contract-fixture",
        }


# ---------------------------------------------------------------------------
# 截断 / 上限
# ---------------------------------------------------------------------------

def _cap_rows(items: List[Any], limit: int, label: str) -> Tuple[List[Any], Dict[str, Any]]:
    total = len(items)
    if total <= limit:
        return items, {"item": label, "total": total, "limit": limit, "dropped": 0, "truncated": False}
    return items[:limit], {"item": label, "total": total, "limit": limit, "dropped": total - limit, "truncated": True}


def _cap_payload(build: Any, items: List[Any], limit: int, label: str) -> Tuple[List[Any], Dict[str, Any]]:
    """字节上限：从尾部逐条裁掉，直到序列化后的字节数 <= limit。"""
    total = len(items)
    kept = list(items)
    while kept and _byte_len(build(kept)) > limit:
        kept.pop()
    dropped = total - len(kept)
    return kept, {
        "item": label,
        "total": total,
        "limit": limit,
        "unit": "bytes",
        "bytes": _byte_len(build(kept)),
        "dropped": dropped,
        "truncated": dropped > 0,
    }


def _merge_caps(caps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    effective = [cap for cap in caps if cap.get("truncated")]
    return {
        "truncated": bool(effective),
        "reports": [dict(cap) for cap in caps],
        "dropped": sum(int(cap.get("dropped") or 0) for cap in caps),
        "limits": {"rows": ROW_LIMIT, "bytes": BYTE_LIMIT, "str": None},
    }


# ---------------------------------------------------------------------------
# 有界 BFS（对象拓扑 / 故障拓扑）
# ---------------------------------------------------------------------------

def _adjacency(edges: Sequence[dict], direction: str, neutral_of: Dict[str, str]) -> Dict[str, List[dict]]:
    adjacency: Dict[str, List[dict]] = {}
    for edge in edges:
        relation = neutral_of.get(edge["reltype"], edge["reltype"])
        row = {
            "from": edge["from"],
            "relation": relation,
            "reltype": edge["reltype"],
            "to": edge["to"],
            "props": dict(edge.get("props") or {}),
        }
        if direction in ("out", "both"):
            adjacency.setdefault(edge["from"], []).append(dict(row))
        if direction in ("in", "both"):
            flipped = dict(row)
            flipped["from"], flipped["to"] = edge["to"], edge["from"]
            adjacency.setdefault(edge["to"], []).append(flipped)
    for rows in adjacency.values():
        rows.sort(key=lambda item: (item["relation"], item["to"]))
    return adjacency


def _bfs(roots: Sequence[str], adjacency: Dict[str, List[dict]], max_hops: int,
         stop_relations: Optional[set] = None, observed_only: Optional[set] = None) -> Dict[str, Any]:
    """有界 BFS：visited 去环 + 深度截断 + 环记录。

    `stop_relations` 里的关系：边原样返回，但**绝不沿它继续展开**（对象的 depth 也不生成），
    对应 docs/09 §5.2 性质 1（relates_to 共现 ≠ 因果）。
    `observed_only` 是**只被看到、不参与展开**的节点累加器（不是关系集合）。
    """
    stop_relations = stop_relations or set()
    observed_only = observed_only if observed_only is not None else set()
    depths: Dict[str, int] = {root: 0 for root in roots}
    order: List[str] = list(roots)
    visited = set(roots)
    frontier: deque = deque((root, 0) for root in roots)
    used: List[dict] = []
    seen_edges: set = set()
    cycles: List[List[str]] = []
    seen_cycles: set = set()
    parent: Dict[str, str] = {}
    truncated_by_depth = False

    while frontier:
        node, depth = frontier.popleft()
        if depth >= max_hops:
            truncated_by_depth = truncated_by_depth or bool(adjacency.get(node))
            continue
        for edge in adjacency.get(node, []):
            key = (edge["from"], edge["reltype"], edge["to"])
            if key not in seen_edges:
                seen_edges.add(key)
                used.append(edge)
            target = edge["to"]
            relation = edge["relation"]
            if relation in stop_relations or edge["reltype"] in stop_relations:
                # 共现边：可见但不展开（节点若尚未入图，仅登记为观察到的邻居）
                if target not in visited:
                    observed_only.add(target)
                continue
            if target in visited:
                path = _cycle_path(parent, edge["from"], target)
                signature = _cycle_signature(path)
                if signature not in seen_cycles:
                    seen_cycles.add(signature)
                    cycles.append(path)
                continue
            visited.add(target)
            depths[target] = depth + 1
            parent[target] = edge["from"]
            order.append(target)
            frontier.append((target, depth + 1))
    return {
        "visited": order,
        "depths": depths,
        "edges": used,
        "cycles": cycles,
        "observedOnly": sorted(observed_only),
        "truncatedByDepth": truncated_by_depth,
    }


def _cycle_path(parent: Dict[str, str], start: str, target: str) -> List[str]:
    """还原环：[target, …, start, target]（沿 parent 链回溯）。"""
    chain = [start]
    cursor = start
    guard = 0
    while cursor != target and cursor in parent and guard < 64:
        cursor = parent[cursor]
        chain.append(cursor)
        guard += 1
    chain.reverse()
    chain.append(target)
    return chain


def _cycle_signature(path: Sequence[str]) -> Tuple[str, ...]:
    """环的规范化签名（旋转对齐后取字典序最小，避免同环重复登记）。"""
    body = tuple(path[:-1]) if len(path) > 1 and path[0] == path[-1] else tuple(path)
    if not body:
        return tuple(path)
    rotations = [body[index:] + body[:index] for index in range(len(body))]
    return min(rotations)


def _cycle_is_whitelisted(cycle: Sequence[str], whitelist_sets: Sequence[frozenset]) -> bool:
    members = set(cycle)
    return any(members <= entry for entry in whitelist_sets)


# ---------------------------------------------------------------------------
# §3.1 对象拓扑
# ---------------------------------------------------------------------------

def _resolve_scope(scope: str, objects: Dict[str, dict], contract: Contract) -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    if scope in (None, "", "*"):
        return sorted(objects.keys()), warnings
    if scope in contract.layers:
        ids = sorted(oid for oid, row in objects.items() if row.get("layer") == scope)
        return ids, warnings
    if scope in contract.object_types:
        ids = sorted(oid for oid, row in objects.items() if row.get("objType") == scope)
        return ids, warnings
    if scope in objects:
        return [scope], warnings
    warnings.append("scope 未命中任何对象/层名/类型名：%r（返回空子图，不代表该对象不存在于业务系统）" % scope)
    return [], warnings


def _apply_string_limit(nodes: List[dict], limit: int) -> int:
    clipped = 0
    for node in nodes:
        attrs = node.get("attrs") or {}
        for key, value in list(attrs.items()):
            new_value, was_clipped = _clip(value, limit)
            if was_clipped:
                attrs[key] = new_value
                clipped += 1
    return clipped


def get_object_topology(scope: str, *, max_hops: Optional[int] = None, include_derived: bool = False,
                        direction: str = "out", backend: Optional[Any] = None,
                        contract: Optional[Contract] = None) -> Dict[str, Any]:
    """§3.1 对象拓扑直读（有界 BFS，默认排除派生注释边）。

    遍历口径 = 「**从起点对象沿 `out` 方向向下**」（见出参 `traversalScope`）：不反向、不溯上游，
    且 `out` 是从**实例起点**出发，不是从契约里关系的声明方向出发。因此契约声明的 4 条跨层关系
    在同一个查询里可能只看到 3 条 —— `HOSTS` 的方向是 `Host -> GaussDBInstance`，从 App 层起点
    按 out 不可达；到 Host 需走 `RUNS_IN -> DEPLOYED_ON`（`must_reach_paths` 第一条即该链路）。
    这是「声明方向 vs 起点可达性」的正常差异，**不为了凑满 4 种而反向遍历**。
    反向关系（`inverse_relations`，`asserted: false`）在图上不建边，故 out 遍历看不到它们。
    """
    contract = contract or get_contract()
    backend = backend or default_backend(contract)
    backend.audit = []
    warnings: List[str] = list(contract.depth_drift())
    warnings.extend(_unsupported_backend_warnings(backend, "get_object_topology"))

    applied_depth = contract.business_hop_depth
    clamped = None
    if max_hops is not None:
        requested = int(max_hops)
        if requested > applied_depth:
            clamped = {
                "requestedMaxHopDepth": requested,
                "appliedMaxHopDepth": applied_depth,
                "reason": "服务端硬上限 = len(topology_facts.hop_path)-1",
            }
            warnings.append(
                "max_hops=%d 被夹取到对象拓扑硬上限 %d（夹取，不报错）" % (requested, applied_depth)
            )
        elif requested < 0:
            applied_depth = 0
            clamped = {"requestedMaxHopDepth": requested, "appliedMaxHopDepth": 0, "reason": "负数按 0 处理"}
        else:
            applied_depth = requested

    objects = backend.fetch_objects()
    roots, scope_warnings = _resolve_scope(scope, objects, contract)
    warnings.extend(scope_warnings)

    edges = backend.fetch_business_edges()
    if include_derived:
        derived_rows = list(backend.fetch_derived_edges(roots))
        # 派生边的目标（`:CC_Evidence`）不属于 12 个可实例化对象类型，需单独并入节点目录，
        # 否则它们会以 objType=null 出现在 nodes 里、把「注释」冒充成业务对象。
        for edge in derived_rows:
            target = edge.get("to")
            if target and target not in objects:
                props = dict(edge.get("props") or {})
                objects[target] = {
                    "objId": target,
                    "objType": edge.get("objType") or props.get(contract.type_prop),
                    "layer": props.get(contract.layer_prop),
                    "props": props,
                }
        edges = list(edges) + derived_rows
        warnings.append("include_derived=True：已并入派生注释边 %s（默认关闭）" % sorted(contract.derived_reltypes))
    else:
        edges = [edge for edge in edges if edge["reltype"] not in contract.derived_reltypes]

    adjacency = _adjacency(edges, direction, contract.neutral_of)
    traversal = _bfs(roots, adjacency, applied_depth)

    node_payload: List[dict] = []
    for obj_id in sorted(traversal["visited"]):
        row = objects.get(obj_id)
        if row is None:
            row = {"objId": obj_id, "objType": None, "layer": None, "props": {}}
        props = dict(row.get("props") or {})
        attrs = {key: value for key, value in props.items() if key not in contract.reserved_props and value is not None}
        node_payload.append({
            "objId": obj_id,
            "objType": row.get("objType"),
            "layer": row.get("layer"),
            "attrs": attrs,
        })
    observed = [oid for oid in traversal["observedOnly"] if oid not in traversal["depths"]]
    for obj_id in sorted(observed):
        row = objects.get(obj_id)
        if row is None:
            continue
        props = dict(row.get("props") or {})
        attrs = {key: value for key, value in props.items() if key not in contract.reserved_props and value is not None}
        node_payload.append({"objId": obj_id, "objType": row.get("objType"), "layer": row.get("layer"), "attrs": attrs})

    edge_payload: List[dict] = []
    for edge in sorted(traversal["edges"], key=lambda item: (item["from"], item["relation"], item["to"])):
        edge_payload.append({
            "from": edge["from"],
            "relation": edge["relation"],
            "to": edge["to"],
            "crossLayer": edge["reltype"] in contract.cross_layer_reltypes,
        })

    clipped = _apply_string_limit(node_payload, contract.str_limit)

    row_capped_nodes, node_cap = _cap_rows(node_payload, ROW_LIMIT, "nodes")
    row_capped_edges, edge_cap = _cap_rows(
        edge_payload, max(0, ROW_LIMIT - len(row_capped_nodes)), "edges"
    )
    build = lambda nodes, edges: {"nodes": nodes, "edges": edges}  # noqa: E731
    kept_nodes, node_byte_cap = _cap_payload(
        lambda nodes: build(nodes, row_capped_edges), row_capped_nodes, BYTE_LIMIT, "nodes(bytes)"
    )
    kept_edges, edge_byte_cap = _cap_payload(
        lambda edges: build(kept_nodes, edges), row_capped_edges, BYTE_LIMIT, "edges(bytes)"
    )
    truncation = _merge_caps([node_cap, edge_cap, node_byte_cap, edge_byte_cap])
    truncation["limits"]["str"] = contract.str_limit
    truncation["stringsClipped"] = clipped
    truncation["bytes"] = _byte_len(build(kept_nodes, kept_edges))
    truncation["clamped"] = clamped
    # `truncated` 的语义 = 返回体相对「服务端实际采用的深度/上限」被裁过。
    # 深度夹取（请求 99 → 实际 9）单独记在 truncation.clamped 与 warnings 里，
    # 不混进 truncated：否则「没被裁」与「被裁」不可分。
    truncation["truncated"] = bool(truncation["truncated"] or clipped)

    result = {
        "nodes": kept_nodes,
        "edges": kept_edges,
        "truncated": bool(truncation["truncated"]),
        "hopsUsed": max(traversal["depths"].values()) if traversal["depths"] else 0,
        "scope": scope,
        "direction": direction,
        "appliedMaxHopDepth": applied_depth,
        "includeDerived": bool(include_derived),
        "traversalScope": _traversal_scope_report(contract, direction, scope),
        "mustReach": _must_reach_report(contract, backend, include_derived),
        "truncation": truncation,
        "warnings": warnings,
        "audit": list(backend.audit),
    }
    truncation["totalBytes"] = _byte_len({key: value for key, value in result.items() if key != "truncation"})
    return result


def _must_reach_report(contract: Contract, backend: Any, include_derived: bool) -> List[dict]:
    """must_reach_paths 的机械核验（**类型粒度**，只读）。

    契约里的必达路径写的是**类型序列**（例 `CreditCardApp/AppInstance -> Container -> Host`
    与 `… -> ConnectionPool -> Session -> Query -> Table`），所以核验方式 = 真机图上是否存在
    一条实例路径，其节点类型依序命中该序列。抽象父类型（`ConnectionPool`）不落图，按
    `topology.yaml#parent` 展开为具体子类型。走不到就如实报 `reached: false` 并指出断在哪一跳，
    绝不补边、绝不假装可达。
    """
    objects = backend.fetch_objects()
    type_of_id: Dict[str, str] = {
        obj_id: str((row or {}).get("objType")) for obj_id, row in objects.items()
    }
    edges = backend.fetch_business_edges()
    if include_derived:
        edges = list(edges) + list(backend.fetch_derived_edges([]))
    type_pairs: set = set()
    for edge in edges:
        src = type_of_id.get(edge["from"])
        dst = type_of_id.get(edge["to"])
        if src and dst and src != "None" and dst != "None":
            type_pairs.add((src, dst))

    report: List[dict] = []
    for path in contract.must_reach_paths:
        chain = _parse_must_reach(path)
        reached = False
        hops = 0
        missing: List[str] = []
        if len(chain) >= 2:
            candidates = set()
            for type_name in chain[0]:
                candidates.update(contract.expand_types(type_name))
            for index in range(1, len(chain)):
                wanted = set()
                for type_name in chain[index]:
                    wanted.update(contract.expand_types(type_name))
                nxt = {dst for (src, dst) in type_pairs if src in candidates and dst in wanted}
                if not nxt:
                    missing = ["%s -> %s" % ("/".join(chain[index - 1]), "/".join(chain[index]))]
                    break
                candidates = nxt
                hops = index
                if index == len(chain) - 1:
                    reached = True
        report.append({
            "path": path,
            "typeChain": ["/".join(segment) for segment in chain],
            "reached": reached,
            "hops": hops,
            "missingHop": missing,
        })
    return report


def _parse_must_reach(path: str) -> List[List[str]]:
    """把 `A/B -> C -> D` 形状的必达路径解析成类型序列（每段可能是多个候选类型）。"""
    text = str(path).replace("→", "->").replace("—>", "->")
    chain: List[List[str]] = []
    for segment in text.split("->"):
        types = [token.strip() for token in segment.replace("/", "|").split("|") if token.strip()]
        if types:
            chain.append(types)
    return chain


def _traversal_scope_report(contract: Contract, direction: str, scope: str) -> Dict[str, Any]:
    """回显遍历口径（出参自解释：读者不必翻契约就知道 out 意味着什么、什么不可达）。"""
    return {
        "direction": direction,
        "startFrom": scope,
        "semantics": "从起点对象沿 out 方向向下展开（不反向、不溯上游）",
        "includesRelations": contract.traversal_scope.get("includes_relations", "business_only"),
        "excludesDerivedRelations": list(contract.derived_codes),
        "crossLayerRelations": list(contract.cross_layer_relations),
        "inverseRelations": list(contract.inverse_relation_codes),
        "inverseRelationsNote": (
            "契约把这几条声明为 asserted:false（运行时反向匹配，图上不建边），故 out 遍历看不到它们。"
        ),
        "hostsNote": (
            "契约里 HOSTS 的方向是 Host -> GaussDBInstance，从 App 层起点按 out 不可达；"
            "到 Host 需走 RUNS_IN -> DEPLOYED_ON（must_reach_paths 第一条即该链路）。"
        ),
        "basis": contract.traversal_scope.get("basis"),
    }


# ---------------------------------------------------------------------------
# §3.2 补充：detect 条款的机械判定 + 确定性排序（**不做任何概率合成**）
# ---------------------------------------------------------------------------

# 边关系优先级：CAUSES 先于 CONDITIONAL_CAUSES；RELATES_TO 是共现，不参与根因收敛。
RELATION_PRIORITY = {REL_CAUSES: 0, REL_CONDITIONAL: 1}
UNRANKED_PRIORITY = 9


def _to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _compare(op: str, value: Any, operand: Any) -> Optional[bool]:
    """三值比较：数据不足以判定时返回 None（**绝不用 False 冒充「不满足」**）。"""
    left = _to_number(value)
    right = _to_number(operand)
    if left is None or right is None:
        return None
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return None


def _threshold_operand(ref: str, thresholds: Dict[str, dict]) -> Tuple[Optional[float], str]:
    """解析 `threshold:NAME#level`。真机 :CC_Threshold 优先、YAML 回退；两级都是 null ⇒ 未标定。"""
    body = str(ref).split("threshold:", 1)[1] if "threshold:" in str(ref) else str(ref)
    name, _, level = body.partition("#")
    level = (level or "warning").strip()
    spec = thresholds.get(name) or {}
    live_key = "thrWarning" if level == "warning" else "thrCritical"
    yaml_key = "warning" if level == "warning" else "critical"
    for key in (live_key, yaml_key):
        number = _to_number(spec.get(key))
        if number is not None:
            source = "CC_Threshold" if live_key == key else "fault-modes.yaml"
            return number, "%s:%s.%s" % (source, name, key)
    return None, "%s:%s.%s(未标定)" % ("CC_Threshold", name, level)


def _metric_projection(code: str, catalog: Dict[str, dict], contract: Contract) -> Tuple[Dict[str, Any], str]:
    """指标元数据：优先 `:CC_Metric`（权威目录），回退 type-map.metrics 投影。"""
    node = catalog.get(code) or {}
    if node:
        return {
            "object": node.get("metricObject"),
            "kg_property": node.get("metricKgProperty"),
            "unit": node.get("metricUnit"),
            "metric_source": node.get("metricSource"),
            "metric_type": node.get("metricType"),
            "contract_ref": node.get("metricContractRef"),
        }, "CC_Metric"
    spec = contract.metric_projection.get(code) or {}
    metric = contract.metric_contract.get(code) or {}
    return {
        "object": spec.get("object"),
        "kg_property": spec.get("kg_property"),
        "unit": metric.get("unit"),
        "metric_source": metric.get("source"),
        "metric_type": metric.get("type"),
        "contract_ref": None,
    }, "object_property"


def _series_of(rows: Sequence[dict]) -> Dict[Tuple[str, str], List[Tuple[Any, Any]]]:
    series: Dict[Tuple[str, str], List[Tuple[Any, Any]]] = {}
    for row in rows:
        key = (str(row.get("objectId")), str(row.get("metricCode")))
        series.setdefault(key, []).append((row.get("capturedAt"), row.get("value")))
    for points in series.values():
        points.sort(key=lambda item: str(item[0] or ""))
    return series


def _eval_clause(clause: dict, obj: dict, catalog: Dict[str, dict], contract: Contract,
                 thresholds: Dict[str, dict], series: Dict[Tuple[str, str], List[Tuple[Any, Any]]]) -> dict:
    code = str(clause.get("metric"))
    projection, origin = _metric_projection(code, catalog, contract)
    props = dict(obj.get("props") or {})
    obj_type = str(obj.get("objType"))
    kg_property = projection.get("kg_property")
    value = props.get(kg_property) if (kg_property and projection.get("object") == obj_type) else None
    observed = value
    reason: Optional[str] = None

    if clause.get("predicate") in ("increased", "decreased"):
        points = series.get((str(obj.get("objId")), code)) or []
        if len(points) < 2:
            outcome: Optional[bool] = None
            reason = "只有 %d 个时间点，判不出%s（不得用 False 冒充）" % (len(points), clause.get("predicate"))
        else:
            first = _to_number(points[0][1])
            last = _to_number(points[-1][1])
            observed = [points[0][1], points[-1][1]]
            if first is None or last is None:
                outcome, reason = None, "证据值非数值，判不出变化"
            else:
                outcome = last > first if clause.get("predicate") == "increased" else last < first
    elif clause.get("predicate") == "non_zero":
        number = _to_number(value)
        outcome = None if number is None else (number != 0.0)
        if number is None:
            reason = "对象 %s 上没有 %s 的当前值" % (obj.get("objId"), code)
    elif clause.get("op") is not None:
        operand = clause.get("operand")
        operand_note = None
        if isinstance(operand, str) and operand.startswith("threshold:"):
            right, operand_note = _threshold_operand(operand, thresholds)
        elif isinstance(operand, str) and operand.startswith("entity_property:"):
            target = operand.split("entity_property:", 1)[1]
            target_type, _, target_prop = target.partition(".")
            right = props.get(target_prop) if target_type == obj_type else None
            operand_note = "entity_property:%s" % target
            if right is None:
                reason = "对象 %s 上没有 %s" % (obj.get("objId"), target)
        else:
            right = operand
            operand_note = "literal"
        outcome = _compare(str(clause.get("op")), value, right)
        if outcome is None and reason is None:
            reason = "比较不可判定（观测值 %r / 阈值 %r）" % (value, right)
        if operand_note:
            operand = "%s (%s)" % (operand, operand_note)
    else:
        outcome, reason = None, "无法识别的条款形态：%s" % sorted(clause)

    return {
        "metric": code,
        "kind": clause.get("predicate") or clause.get("op"),
        "operand": clause.get("operand"),
        "observed": observed,
        "outcome": outcome,
        "metadataSource": origin,
        "reason": reason,
    }


def _detect_report(mode: dict, objects: Dict[str, dict], catalog: Dict[str, dict], contract: Contract,
                   thresholds: Dict[str, dict], series: Dict[Tuple[str, str], List[Tuple[Any, Any]]]) -> dict:
    """按组件实例机械判定 fault-modes.yaml#detect（全 of / 任一 of）。

    `satisfied` = 命中的 all_of 条款数；缺数据一律 None（`determined: false`），
    **不用 0 冒充**（那种情况下它在排序里单列在已判定候选之后）。
    """
    detect = mode.get("detect") or {}
    all_of = list(detect.get("all_of") or [])
    any_of = list(detect.get("any_of") or [])
    component = str(mode.get("component_type") or "")
    candidates = sorted(
        (obj for obj in objects.values() if str(obj.get("objType")) == component),
        key=lambda obj: str(obj.get("objId")),
    )
    if not candidates:
        return {
            "component": component,
            "componentInstance": None,
            "satisfied": None,
            "total": len(all_of),
            "determined": False,
            "undeterminedClauses": [c.get("metric") for c in all_of + any_of],
            "anyOfSatisfied": None,
            "evidence": [{
                "metric": c.get("metric"), "kind": c.get("predicate") or c.get("op"),
                "operand": c.get("operand"), "observed": None, "outcome": None,
                "metadataSource": None, "reason": "图上没有 %s 的实例" % component,
            } for c in all_of + any_of],
        }
    best: Optional[dict] = None
    for obj in candidates:
        evaluated = [_eval_clause(c, obj, catalog, contract, thresholds, series) for c in all_of]
        any_evaluated = [_eval_clause(c, obj, catalog, contract, thresholds, series) for c in any_of]
        hit = sum(1 for item in evaluated if item["outcome"] is True)
        any_hit = None if not any_evaluated else any(item["outcome"] is True for item in any_evaluated)
        undetermined = [item["metric"] for item in evaluated + any_evaluated if item["outcome"] is None]
        record = {
            "component": component,
            "componentInstance": obj.get("objId"),
            "satisfied": hit,
            "total": len(evaluated),
            "determined": len(evaluated) > 0 and len(undetermined) == 0,
            "undeterminedClauses": undetermined,
            "anyOfSatisfied": any_hit,
            "evidence": evaluated + any_evaluated,
        }
        if best is None or (record["satisfied"], record["componentInstance"]) > (
                best["satisfied"], best["componentInstance"]):
            best = record
    return best or {}


def _leaf_root_codes(contract: Contract) -> List[str]:
    """`causal-edges.yaml#leaf_root_causes` 是**列表**（每项带 `code`），不是映射。"""
    raw = contract.leaf_root_causes
    if isinstance(raw, dict):
        return sorted(str(key) for key in raw)
    codes: List[str] = []
    for item in (raw or []):
        if isinstance(item, dict):
            code = item.get("code") or item.get("fmCode") or item.get("fault_mode")
            if code:
                codes.append(str(code))
        elif item:
            codes.append(str(item))
    return sorted(codes)


def _rank_root_candidates(node_rows: Sequence[dict], depths: Dict[str, int], edge_rows: Sequence[dict],
                          observed_only: Sequence[str], contract: Contract, backend: Any) -> Dict[str, Any]:
    """确定性排序（替代 `docs/03:317` 的概率合成——本阶段不可实现，裁定不做）。

    排序键：(a) 命中的 detect 条款数**降序**；(b) 路径跳数**升序**；(c) 入边关系优先级
    CAUSES > CONDITIONAL_CAUSES（RELATES_TO 不参与根因收敛）；并列全部返回、不合并为单一分数。
    数据不足以判定 (a) 的候选单列在已判定候选之后，并标 `determined: false`。
    """
    objects = backend.fetch_objects()
    catalog = backend.fetch_metric_catalog()
    thresholds = backend.fetch_thresholds() or {}
    series = _series_of(backend.fetch_metric_history())
    observed = {str(code) for code in observed_only}

    candidates: List[dict] = []
    for row in node_rows:
        fm_code = str(row.get("fmCode"))
        mode = contract.fault_modes.get(fm_code) or {}
        detect = _detect_report(mode, objects, catalog, contract, thresholds, series)
        incoming = [edge for edge in edge_rows if str(edge.get("to")) == fm_code]
        cause_relations = [str(edge.get("relation")) for edge in incoming
                           if str(edge.get("relation")) in RELATION_PRIORITY]
        priority = min((RELATION_PRIORITY[rel] for rel in cause_relations), default=None)
        hops = depths.get(fm_code)
        candidates.append({
            "fmCode": fm_code,
            "name": row.get("name"),
            "layer": row.get("layer"),
            "severity": row.get("severity"),
            "detectionMode": row.get("detectionMode"),
            "hops": hops,
            "viaRelation": REL_RELATES if fm_code in observed else REL_CAUSES,
            "satisfiedClauses": detect.get("satisfied"),
            "totalClauses": detect.get("total"),
            "determined": bool(detect.get("determined")),
            "undeterminedClauses": list(detect.get("undeterminedClauses") or []),
            "componentInstance": detect.get("componentInstance"),
            "relationPriority": (REL_CAUSES if priority == 0 else
                                 (REL_CONDITIONAL if priority == 1 else None)),
            "isLeafRootCandidate": not cause_relations,
            "detectEvidence": list(detect.get("evidence") or []),
        })

    candidates.sort(key=lambda item: (
        0 if item["determined"] else 1,
        -(item["satisfiedClauses"] if item["satisfiedClauses"] is not None else 0),
        item["hops"] if item["hops"] is not None else 10 ** 6,
        RELATION_PRIORITY.get(str(item["relationPriority"]), UNRANKED_PRIORITY),
        item["fmCode"],
    ))
    for index, item in enumerate(candidates):
        item["rank"] = index + 1
    return {
        "rankingMethod": "deterministic-no-weight-composition",
        "criteria": [
            "命中的 detect 条款数降序",
            "路径跳数升序",
            "入边关系优先级 CAUSES > CONDITIONAL_CAUSES（RELATES_TO 不参与根因收敛）",
        ],
        "ties": "并列全部返回，不合并为单一分数（保留可能根因集合 + 排序 + 共存标注）",
        "compositionApplied": False,
        "thresholdsSeen": sorted(thresholds.keys()),
        "metricMetadataSource": "CC_Metric" if catalog else "object_property",
        "rootCandidates": candidates,
        "leafRootCauseHint": _leaf_root_codes(contract),
    }


def _cycle_records(cycles: Sequence[Sequence[str]], edge_rows: Sequence[dict], node_rows: Sequence[dict],
                   contract: Contract) -> List[dict]:
    """环的审计明细：环节点/边 + 停止点 + 是否白名单（不抛异常、不静默丢）。"""
    relation_of = {(str(row.get("from")), str(row.get("to"))): str(row.get("relation")) for row in edge_rows}
    info_of = {str(row.get("fmCode")): row for row in node_rows}
    records: List[dict] = []
    for cycle in cycles:
        path = [str(item) for item in cycle]
        nodes = []
        for fm_code in path[:-1] if len(path) > 1 else path:
            info = info_of.get(fm_code) or {}
            nodes.append({"fmCode": fm_code, "name": info.get("name"), "layer": info.get("layer")})
        edges = []
        for index in range(len(path) - 1):
            src, dst = path[index], path[index + 1]
            edges.append({
                "from": src,
                "to": dst,
                "relation": relation_of.get((src, dst)),
            })
        records.append({
            "path": path,
            "whitelisted": bool(_cycle_is_whitelisted(path, contract.cycle_whitelist_sets)),
            "stoppedAt": path[-2] if len(path) >= 2 else None,
            "blockedEdge": ({"from": path[-2], "to": path[-1]} if len(path) >= 2 else None),
            "nodes": nodes,
            "edges": edges,
        })
    return records


# ---------------------------------------------------------------------------
# §3.2 故障拓扑
# ---------------------------------------------------------------------------

def _relation_name(edge: dict, contract: Contract) -> str:
    props = edge.get("props") or {}
    if edge["reltype"] == contract.relates_reltype or str(props.get("relation")) == REL_RELATES:
        return REL_RELATES
    if any(props.get(name) is not None for name in CONDITION_PROPS):
        return REL_CONDITIONAL
    if str(props.get("relation")) == REL_CONDITIONAL:
        return REL_CONDITIONAL
    contract_edge = contract.causal_edge_of.get(str(props.get("contractId")))
    if contract_edge and str(contract_edge.get("relation")) == REL_CONDITIONAL:
        return REL_CONDITIONAL
    return REL_CAUSES


def _edge_detail(edge: dict, contract: Contract) -> Dict[str, Any]:
    """故障边出参（★ 边属性契约优先修正，2026-10-06）。

    契约 `shared\\model\\causal-edges.yaml` 的每条边**只有** `id`/`from`/`relation`/`to`/
    `chain`/`evidence`/`provenance`（条件边另有 `condition`）⇒ `weight`/`probability`/
    `delaySec`/`chainPosition`/`learnCount` 在契约里**不存在**，本出参**不含**这 5 个键。
    保留键：`contractId`/`chain`/`description`(=`evidence` 原文)/`provenance`，
    条件边另加 `condMetric`/`condOp`/`condKind`/`condRef`（= `condition.operand`，
    图属性名 `condRef`，历史名 `condValue`）；`evidence` 与 `description` 同值双键，
    兼容 `docs\\09` §3.2 的字段名。`condition` 子字典的键名与
    `causal-edges.yaml` 原文一致（`metric`/`op`/`operand`/`kind`），图上以 `cond*` 属性表达。
    """
    props = dict(edge.get("props") or {})
    contract_edge = contract.causal_edge_of.get(str(props.get("contractId")), {})
    condition = contract_edge.get("condition")
    provenance = props.get("provenance") or contract_edge.get("provenance")
    evidence = (props.get("description") or props.get("evidence")
                or contract_edge.get("description") or contract_edge.get("evidence"))
    chain = props.get("chain") or contract_edge.get("chain")
    cond_metric = props.get("condMetric")
    cond_op = props.get("condOp")
    cond_kind = props.get("condKind")
    cond_ref = props.get("condRef")
    if cond_ref is None:
        cond_ref = props.get("condValue")
    if cond_metric is None and isinstance(condition, dict):
        cond_metric = condition.get("metric")
        cond_op = condition.get("op")
        cond_ref = condition.get("operand")
        cond_kind = condition.get("kind")
    condition_payload: Any = None
    if cond_metric is not None or isinstance(condition, dict):
        condition_payload = {
            "metric": cond_metric,
            "op": cond_op,
            "operand": cond_ref,
            "kind": cond_kind,
        }
    detail: Dict[str, Any] = {
        "from": edge["from"],
        "relation": _relation_name(edge, contract),
        "to": edge["to"],
        "condition": condition_payload,
        "evidence": evidence,
        "description": evidence,
        "provenance": provenance,
        "contractId": props.get("contractId") or contract_edge.get("id"),
        "chain": chain,
    }
    if condition_payload is not None or cond_metric is not None:
        detail["condMetric"] = cond_metric
        detail["condOp"] = cond_op
        detail["condKind"] = cond_kind
        detail["condRef"] = cond_ref
    return detail


def get_fault_topology(code: str, max_hops: Optional[int] = None, *, backend: Optional[Any] = None,
                       contract: Optional[Contract] = None) -> Dict[str, Any]:
    """§3.2 故障拓扑直读（Python 侧有界 BFS；深度硬上限 4；relates_to 只返回不展开）。

    方向：从 `code` 沿因果边 **out** 方向顺流（原因 → 结果），与 §5.2 伪代码同向。
    `ranking.rootCandidates` 是**确定性**排序（不改写任何权重/概率、不做路径强度合成、
    不写回 learnCount）：命中的 detect 条款数降序 → 跳数升序 → 入边关系优先级
    CAUSES > CONDITIONAL_CAUSES；`relates_to` 是共现，只随边返回、不参与根因收敛。
    并列候选全部返回、不合并为单一分数；数据不足以判定 detect 的候选标 `determined: false`，
    单列在已判定候选之后（不用 0 冒充）。环一律记录在 `cyclesEncountered` 与 `cycles`
    （含环节点/边、停止点、是否白名单），非白名单环记 warning 并停止展开，不抛异常。
    """
    contract = contract or get_contract()
    backend = backend or default_backend(contract)
    backend.audit = []
    warnings: List[str] = list(_unsupported_backend_warnings(backend, "get_fault_topology"))

    applied_hops = contract.fault_default_hops
    clamped = None
    if max_hops is not None:
        requested = int(max_hops)
        if requested > contract.fault_max_hops:
            clamped = {
                "requestedMaxHops": requested,
                "appliedMaxHops": contract.fault_max_hops,
                "reason": "服务端硬上限（最长故障链 F2 = 4 跳）",
            }
            warnings.append("max_hops=%d 被夹取到硬上限 %d（夹取，不报错）" % (requested, contract.fault_max_hops))
        elif requested < 0:
            clamped = {"requestedMaxHops": requested, "appliedMaxHops": 0, "reason": "负数按 0 处理"}
            applied_hops = 0
        else:
            applied_hops = requested

    modes = backend.fetch_fault_modes([code])
    edges = backend.fetch_causal_edges()

    result: Dict[str, Any] = {
        "root": code,
        "nodes": [],
        "edges": [],
        "depths": {},
        "observedOnly": [],
        "cyclesEncountered": [],
        "cycles": [],
        "ranking": {
            "rankingMethod": "deterministic-no-weight-composition",
            "compositionApplied": False,
            "rootCandidates": [],
            "reason": "根节点不存在，无可排序候选（诚实降级）",
        },
        "truncated": False,
        "appliedMaxHops": applied_hops,
        "relationPassthrough": contract.relation_passthrough,
        "warnings": warnings,
        "audit": [],
    }

    if code not in modes:
        warnings.append(
            "故障模式 %r 不在图谱中（诚实降级：返回空子图；不代表该故障不可能发生）" % code
        )
        result["warnings"] = warnings
        result["truncation"] = _merge_caps([])
        result["truncation"]["bytes"] = _byte_len(result)
        result["audit"] = list(backend.audit)
        return result

    # 全部因果边都交给 BFS：可达性由 visited 集与深度决定。
    # 不做「只留根节点相关边」的预过滤 —— 那会把后继节点的出边整片丢掉，
    # 使 2 跳以外的因果结构凭空消失（正是 §9 失败模式 3「截断被无视」的温床）。
    adjacency = _adjacency(edges, "out", contract.neutral_of)
    traversal = _bfs([code], adjacency, applied_hops, stop_relations={contract.relates_reltype})

    detail_of: Dict[Tuple[str, str, str], dict] = {}
    for edge in traversal["edges"]:
        detail_of[(edge["from"], edge["reltype"], edge["to"])] = _edge_detail(edge, contract)
    edge_rows = sorted(detail_of.values(), key=lambda row: (row["relation"], row["from"], row["to"]))

    involved = set(traversal["depths"]) | set(traversal["observedOnly"])
    for extra in edge_rows:
        involved.add(extra["from"])
        involved.add(extra["to"])
    detail_modes = backend.fetch_fault_modes(sorted(involved))
    node_rows: List[dict] = []
    for fm_code in sorted(involved):
        props = dict(detail_modes.get(fm_code) or {})
        contract_mode = contract.fault_modes.get(fm_code) or {}
        node_rows.append({
            "fmCode": fm_code,
            "name": props.get("fmName") or contract_mode.get("name"),
            "layer": props.get("fmLayer") or contract_mode.get("layer"),
            "severity": props.get("fmSeverity") or contract_mode.get("severity"),
            "detectionMode": props.get("fmDetectionMode") or "direct",
        })

    for cycle in traversal["cycles"]:
        if not _cycle_is_whitelisted(cycle, contract.cycle_whitelist_sets):
            warnings.append(
                "遇到白名单外的有向环（已记录并停止展开，未死循环）：%s" % " -> ".join(cycle)
            )

    row_capped_nodes, node_cap = _cap_rows(node_rows, ROW_LIMIT, "nodes")
    row_capped_edges, edge_cap = _cap_rows(edge_rows, max(0, ROW_LIMIT - len(row_capped_nodes)), "edges")
    build = lambda nodes, edges: {"root": code, "nodes": nodes, "edges": edges}  # noqa: E731
    kept_nodes, node_byte_cap = _cap_payload(
        lambda nodes: build(nodes, row_capped_edges), row_capped_nodes, BYTE_LIMIT, "nodes(bytes)"
    )
    kept_edges, edge_byte_cap = _cap_payload(
        lambda edges: build(kept_nodes, edges), row_capped_edges, BYTE_LIMIT, "edges(bytes)"
    )
    truncation = _merge_caps([node_cap, edge_cap, node_byte_cap, edge_byte_cap])
    truncation["clamped"] = clamped
    truncation["bytes"] = _byte_len(build(kept_nodes, kept_edges))
    # 与对象拓扑同口径：深度夹取（99 → 4）不是「载荷被裁」，单记在 clamped 与 warnings，
    # 不混进 truncated，否则「没被裁」与「被裁」不可分（T6 只要求夹取 + 回显实际值）。
    truncation["truncated"] = bool(truncation["truncated"])

    depths = {fm_code: depth for fm_code, depth in traversal["depths"].items()}
    truncated_by_depth = bool(traversal["truncatedByDepth"]) or any(
        depth >= applied_hops for depth in depths.values()
    )
    result = {
        "root": code,
        "nodes": kept_nodes,
        "edges": kept_edges,
        "depths": dict(sorted(depths.items())),
        "observedOnly": list(traversal["observedOnly"]),
        "cyclesEncountered": [list(cycle) for cycle in traversal["cycles"]],
        "cycles": _cycle_records(traversal["cycles"], edge_rows, node_rows, contract),
        "truncated": bool(truncation["truncated"] or truncated_by_depth),
        "appliedMaxHops": applied_hops,
        "relationPassthrough": contract.relation_passthrough,
        "cycleWhitelist": [list(cycle) for cycle in contract.cycle_whitelist],
        "ranking": _rank_root_candidates(
            node_rows, depths, edge_rows, traversal["observedOnly"], contract, backend
        ),
        "truncation": truncation,
        "warnings": warnings,
        "audit": list(backend.audit),
    }
    truncation["totalBytes"] = _byte_len({key: value for key, value in result.items() if key != "truncation"})
    return result


# ---------------------------------------------------------------------------
# §3.3 指标
# ---------------------------------------------------------------------------

def get_metrics(objects: Sequence[str], *, backend: Optional[Any] = None,
                contract: Optional[Contract] = None) -> ReportList:
    """§3.3 指标直读。只返回 `threshold_ref` 引用，不返回任何阈值数字。"""
    contract = contract or get_contract()
    backend = backend or default_backend(contract)
    backend.audit = []
    warnings: List[str] = list(_unsupported_backend_warnings(backend, "get_metrics"))

    catalogue = backend.fetch_objects()
    if not objects:
        target_ids = sorted(catalogue.keys())
        warnings.append("objects 为空数组 ⇒ 按契约取全量（%d 个对象）" % len(target_ids))
    else:
        target_ids = []
        for item in objects:
            ids, scope_warnings = _resolve_scope(str(item), catalogue, contract)
            warnings.extend(scope_warnings)
            for obj_id in ids:
                if obj_id not in target_ids:
                    target_ids.append(obj_id)
    target_ids = sorted(target_ids)

    codes: List[str] = []
    for obj_id in target_ids:
        obj_type = (catalogue.get(obj_id) or {}).get("objType")
        codes.extend(contract.metrics_of_type.get(str(obj_type), []))
    timestamps = backend.fetch_metric_timestamps(target_ids, sorted(set(codes)))

    # 指标元数据优先读 :CC_Metric（权威目录，v1.0.1 起已持久化），读不到才回退
    # metrics.yaml + type-map 的投影；每行用 `source` 显式标注这条元数据来自哪里，
    # 读者据此判断权威性（值本身两种情况下都取对象上的 lowerCamel 属性）。
    catalog = backend.fetch_metric_catalog()
    if not catalog:
        warnings.append(
            "真机没有 :CC_Metric 节点 ⇒ 指标元数据回退 metrics.yaml/type-map（source=object_property）"
        )

    rows: List[dict] = []
    origin_counts: Dict[str, int] = {}
    declared_sources: Dict[str, Any] = {}
    for obj_id in target_ids:
        row = catalogue.get(obj_id) or {}
        props = dict(row.get("props") or {})
        obj_type = str(row.get("objType"))
        codes_for_type = sorted(set(contract.metrics_of_type.get(obj_type, [])) | {
            code for code, node in catalog.items() if str(node.get("metricObject")) == obj_type
        })
        for code in codes_for_type:
            projection, origin = _metric_projection(code, catalog, contract)
            kg_property = projection.get("kg_property")
            if not kg_property or props.get(kg_property) is None:
                continue
            spec = contract.metric_contract.get(code) or {}
            declared_sources[code] = projection.get("metric_source") or spec.get("source")
            origin_counts[origin] = origin_counts.get(origin, 0) + 1
            rows.append({
                "object": obj_id,
                "metric_code": code,
                "value": props.get(kg_property),
                "unit": projection.get("unit") or spec.get("unit"),
                "ts": timestamps.get((obj_id, code)),
                # ★ 只给引用，不给数字（门禁 G3）
                "threshold_ref": spec.get("threshold_ref"),
                "source": origin,
            })

    row_capped, row_cap = _cap_rows(rows, ROW_LIMIT, "metrics")
    kept, byte_cap = _cap_payload(lambda items: items, row_capped, BYTE_LIMIT, "metrics(bytes)")
    truncation = _merge_caps([row_cap, byte_cap])
    truncation["bytes"] = _byte_len(kept)
    truncation["rowFields"] = ["object", "metric_code", "value", "unit", "ts", "threshold_ref", "source"]
    extra = {
        "metricMetadata": {
            "catalogNodes": len(catalog),
            "metadataSource": "CC_Metric" if catalog else "object_property",
            "valueSourceCounts": origin_counts,
            "declaredMetricSources": declared_sources,
            "note": (
                "`source` 标注该行元数据的来源（CC_Metric = 读自 :CC_Metric 目录；"
                "object_property = 回退 metrics.yaml/type-map）；指标**值**一律取对象属性，"
                "`ts` 取该对象-指标最新 :CC_Evidence.capturedAt。"
            ),
        },
        "snapshotId": (backend.fetch_graph_meta() or {}).get("snapshotId"),
    }
    return ReportList(kept, truncation=truncation, warnings=warnings, audit=backend.audit, extra=extra)


# ---------------------------------------------------------------------------
# §3.4 事实溯源（IP-FACT；硬约束 C5）
# ---------------------------------------------------------------------------

def _time_window(window: Any) -> Tuple[Optional[str], Optional[str], List[str]]:
    warnings: List[str] = []
    if window is None:
        return None, None, warnings
    if isinstance(window, str):
        return window, None, ["timeWindow 传了字符串，按起点处理"]
    if not isinstance(window, dict):
        return None, None, ["timeWindow 形态无法识别（期望 {from,to} 或 {since,until}），已忽略"]
    start = window.get("from") or window.get("since") or window.get("start") or window.get("after")
    end = window.get("to") or window.get("until") or window.get("end") or window.get("before")
    unknown = set(window) - {"from", "since", "start", "after", "to", "until", "end", "before"}
    if unknown:
        warnings.append("timeWindow 忽略未识别键：%s" % sorted(unknown))
    return (str(start) if start else None), (str(end) if end else None), warnings


def get_facts(objectType: str, objectId: Optional[str] = None, metricCodes: Optional[Sequence[str]] = None,
              timeWindow: Any = None, *, backend: Optional[Any] = None,
              contract: Optional[Contract] = None) -> ReportList:
    """§3.4 原始事实记录（CCF observations 的图侧投影，`:CC_Evidence` + `CC_HAS_EVIDENCE`）。"""
    contract = contract or get_contract()
    backend = backend or default_backend(contract)
    backend.audit = []
    warnings: List[str] = list(_unsupported_backend_warnings(backend, "get_facts"))

    if objectType not in contract.object_types:
        warnings.append(
            "objectType=%r 不在契约类型表（12 个可实例化类型）中：返回空数组，不代表该对象没有记录" % objectType
        )

    time_from, time_to, window_warnings = _time_window(timeWindow)
    warnings.extend(window_warnings)

    records = backend.fetch_evidence(
        object_type=objectType,
        object_id=objectId,
        metric_codes=list(metricCodes) if metricCodes else None,
        time_from=time_from,
        time_to=time_to,
    )

    rows: List[dict] = []
    for record in records:
        raw = dict(record.get("props") or {})
        record_id = raw.get("recordId") or record.get("recordId")
        payload = {
            "objectId": record.get("objectId"),
            "metricCode": raw.get("metricCode"),
            "status": raw.get("status"),
            "value": raw.get("rawValue"),
            "unit": raw.get("rawUnit"),
            "reason": raw.get("reason"),
            "probe": raw.get("probe"),
        }
        rows.append({
            "record_id": record_id,
            "record_type": "observation",
            "payload": {key: value for key, value in payload.items() if value is not None},
            "ts": raw.get("capturedAt"),
            "snapshot_id": raw.get("ccfSnapshotId"),
        })
    rows.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("record_id") or "")))

    row_capped, row_cap = _cap_rows(rows, ROW_LIMIT, "facts")
    kept, byte_cap = _cap_payload(lambda items: items, row_capped, BYTE_LIMIT, "facts(bytes)")
    truncation = _merge_caps([row_cap, byte_cap])
    truncation["bytes"] = _byte_len(kept)
    graph_meta = backend.fetch_graph_meta() or {}
    row_snapshots = sorted({str(item.get("snapshot_id")) for item in kept if item.get("snapshot_id")})
    if graph_meta.get("snapshotId") and row_snapshots and graph_meta.get("snapshotId") not in row_snapshots:
        warnings.append(
            "证据行的 ccfSnapshotId（%s）与图上 :CC_Meta.snapshotId（%s）不一致 ⇒ 可能跨快照取证"
            % (row_snapshots, graph_meta.get("snapshotId"))
        )
    return ReportList(
        kept, truncation=truncation, warnings=warnings, audit=backend.audit,
        extra={
            "snapshotId": graph_meta.get("snapshotId"),
            "rowSnapshots": row_snapshots,
            "snapshotSource": "CC_Meta.snapshotId" if graph_meta.get("snapshotId") else "CC_Evidence.ccfSnapshotId",
        },
    )


# ---------------------------------------------------------------------------
# 默认后端 / 告警
# ---------------------------------------------------------------------------

_DEFAULT_BACKEND: Dict[str, Any] = {}


def default_backend(contract: Optional[Contract] = None) -> Any:
    """进程级默认后端：优先真机；无口令/无驱动时退回契约 fixture 并显式告警。"""
    contract = contract or get_contract()
    key = str(contract.root)
    if key in _DEFAULT_BACKEND:
        return _DEFAULT_BACKEND[key]
    try:
        _DEFAULT_BACKEND[key] = GraphBackend.connect(contract=contract)
    except Exception as exc:
        fallback = ContractBackend(contract)
        fallback.audit = []
        fallback._fallback_reason = str(exc)  # type: ignore[attr-defined]
        _DEFAULT_BACKEND[key] = fallback
    return _DEFAULT_BACKEND[key]


def set_default_backend(backend: Any, contract: Optional[Contract] = None) -> None:
    contract = contract or get_contract()
    _DEFAULT_BACKEND[str(contract.root)] = backend


FACADE_SHAPE = (
    '数组出参的门面形状：{"items": [...], "truncated": bool, "truncation": {...}, '
    '"warnings": [...], "audit": [...], ...额外事实}；'
    '对象出参：直接返回该 dict（其中保证带 truncation 与 warnings）。'
)


def to_facade(payload: Any) -> Dict[str, Any]:
    """把内部出参包成**门面形状**（HTTP `:8303` / Dify 工具层必须用它）。

    契约 `kgt-tools.yaml#payload_policy.truncation.must_report: true` 要求任何截断都被报告，
    而 §3.3/§3.4 的出参是 array —— 数组没有放 `truncation` 的位置，所以数组出参统一包成
    `{"items": [...], "truncation": {...}, "warnings": [...], "audit": [...], ...}`。
    直接用 `json.dumps(list)` 序列化 `get_metrics()` / `get_facts()` 的返回值会**丢掉截断事实**，
    门面层不得这么做（这是可执行契约，不只是注释）。
    """
    if isinstance(payload, ReportList):
        meta = payload.meta()
        out: Dict[str, Any] = {"items": list(payload)}
        out.update({key: value for key, value in meta.items() if key != "count"})
        return out
    if isinstance(payload, dict):
        out = dict(payload)
        out.setdefault("truncation", {"truncated": False})
        out.setdefault("warnings", [])
        return out
    raise TypeError("to_facade 只接受 ReportList 或 dict，收到 %r" % type(payload).__name__)


def _unsupported_backend_warnings(backend: Any, interface: str) -> List[str]:
    warnings: List[str] = []
    reason = getattr(backend, "_fallback_reason", None)
    if reason:
        warnings.append("真机后端不可用（%s），已退回契约 fixture：无实例数据" % reason)
    if getattr(backend, "name", "") == "contract-fixture" and interface in ("get_object_topology", "get_metrics", "get_facts"):
        warnings.append(
            "契约 fixture 不含对象实例数据：%s 返回空集（图空 ≠ 对象不存在）" % interface
        )
    return warnings


# ---------------------------------------------------------------------------
# CLI（自测 / 门面调试用；本模块仍只读）
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="方案3 拓扑直读接口（只读）")
    parser.add_argument("--uri", default=None, help="bolt URI（默认取 shared/deploy/targets.yaml）")
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None, help="口令；未给则读环境变量 CC_NEO4J_PASSWORD")
    parser.add_argument("--database", default=None)
    parser.add_argument("--fixture", action="store_true", help="使用离线契约 fixture（不连库）")
    parser.add_argument("--object", dest="object_scope", default=None, help="对象 id / 层名 / *")
    parser.add_argument("--max-hops", dest="max_hops", type=int, default=None)
    parser.add_argument("--include-derived", action="store_true")
    parser.add_argument("--fault", default=None, help="故障模式码")
    parser.add_argument("--metrics", default=None, help="逗号分隔的对象 id（空串=全量）")
    parser.add_argument("--facts", default=None, help="objectType")
    parser.add_argument("--object-id", default=None)
    parser.add_argument("--metric-codes", default=None, help="逗号分隔的指标码")
    parser.add_argument("--census", action="store_true", help="只读普查")
    parser.add_argument("--facade", action="store_true",
                        help="按门面形状输出（数组出参包成 {items,truncation,warnings,audit}）")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    contract = get_contract()
    if args.fixture:
        backend: Any = ContractBackend(contract)
    else:
        backend = GraphBackend.connect(args.uri, args.user, args.password, args.database, contract)

    def emit(payload: Any) -> None:
        if args.facade:
            print(json.dumps(to_facade(payload), ensure_ascii=False, indent=2, default=str))
        elif isinstance(payload, ReportList):
            print(json.dumps({"items": list(payload), "meta": payload.meta()}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    try:
        if args.census:
            emit(backend.census())
        if args.object_scope is not None:
            emit(get_object_topology(args.object_scope, max_hops=args.max_hops,
                                     include_derived=args.include_derived,
                                     backend=backend, contract=contract))
        if args.fault:
            emit(get_fault_topology(args.fault, args.max_hops, backend=backend, contract=contract))
        if args.metrics is not None:
            objects = [item for item in args.metrics.split(",") if item]
            emit(get_metrics(objects, backend=backend, contract=contract))
        if args.facts:
            codes = [item for item in (args.metric_codes or "").split(",") if item] or None
            emit(get_facts(args.facts, args.object_id, codes, backend=backend, contract=contract))
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
