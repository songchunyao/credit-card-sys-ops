# -*- coding: utf-8 -*-
"""方案2（ONTO 侧）Set B 七工具知识服务 — 与方案1（KG 侧）逐字同形的工具面。

载体重述（载体中性）：本服务只读暴露「指标 / 对象类型 / 关系类型 / 属性 / 事实 / 链路 /
候选根因 / 建议动作」七类信息，供编排层调用；不执行任何写操作，不返回任何凭据。

同形约束（硬约束 C3 / 契约 ``shared/model/tools-setb.yaml``）：
  * 七个工具名、每个工具的参数 (name, type, required) 集合、每个工具的**顶层**返回字段名集合
    与 KG 侧逐字一致；
  * 唯一允许的差异是 ``cc_query.query`` 的**取值语言**（该差异已在契约的
    ``carrier_specific`` 中声明，``c3_participation: excluded``）；
  * ``cc_schema.naming_discipline`` 属被比对字段，两侧逐字相同。

本文件是 ONTO 侧 manifest 的唯一来源（``--emit-manifest``），
不变量是 ``sourceSha256``（``emittedAt`` 每次 emit 会变，不参与比对）。

编码：UTF-8 无 BOM，LF。依赖：requests + 标准库 + fastapi（可选，仅在 --serve 时需要）。
"""
import argparse
import collections
import contextvars
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# ---------------------------------------------------------------------------
# 常量：与 KG 侧同形的上限与口径
# ---------------------------------------------------------------------------
CARRIER = "onto"
ARM = "ONTO-Q"
MANIFEST_SCHEMA = "dsh-toolface-manifest/1"
REPO_REL_SOURCE = "scheme2-onto/service/cc_tools_onto.py"

ROW_CAP = 200
BYTE_CAP = 20000
# ★ P1（2026-10-14）：**元数据端点**独立字节预算（与 KG 侧逐字同语义）。
#   为什么必须分开：`cc_guide`（指标目录/对象类型/规则）与 `cc_schema`（对象/关系/属性
#   清单）是 Agent **能做聚焦的前提**。实测 `/guide` 原始载荷 22,920 B > 20,000 B ⇒
#   静默丢掉 11 个条目，**恰好是全部 `host.*` 指标**（字典序最后）⇒ Agent 不知道 Host
#   承载哪些指标 ⇒ 无法聚焦主机层 ⇒ 退化为逐个对象类型穷举 `cc_facts`（17 类 ≈ 31,938
#   tokens/趟）⇒ 轮数耗尽。**元数据体积小、缺了代价极大**，与数据面分离预算；
#   数据面（`cc_facts`/`cc_query`…）维持 BYTE_CAP=20000 的载荷纪律**不变**
#   （`cc_cause` 另有 CAUSE_BYTE_CAP，理由见其定义处）。
#   ★ 上限只负责「给足空间」；完整性由门禁断言（服务返回的目录 == 图里的实体数）。
METADATA_BYTE_CAP = 65536           # 元数据端点字节上限
#: 元数据端点行上限。`/schema` 实测被 ROW_CAP=200 砍掉 57 行 attributes ⇒
#: 目录不完整；与字节上限同理，元数据的完整性优先于行数纪律。
METADATA_ROW_CAP = 600
METADATA_TOOLS = ("cc_guide", "cc_schema")
# ★ P2c（2026-10-14）：**`cc_cause` 单列预算**（与 KG 侧逐字同语义）。
#   契约 `rankBasis` ⑥ 明写「**并列全部返回**，不合并为单一分数」—— 候选集必须完整。
#   实测（`BYTE_CAP=20000`）：`cap_bytes()` **从列表尾部 pop**，实测丢 7–9 个候选，
#   而 `droppedRows` 仍为 0 ⇒ 残缺**在账上看不出来**。两侧丢弃量不同（载荷大小不同）
#   ⇒ A/B 的候选集差异被污染成「载荷大小差异」，而非图覆盖或推理差异。
#   取 49152 B = 实测最大值 31,913 B 的 1.54 倍（含 23 个候选理论上限的余量）。
CAUSE_BYTE_CAP = 49152


def byte_cap_for(tool: str) -> int:
    """按端点类别取字节上限：元数据 > 根因候选 > 其余数据面。"""
    if tool in METADATA_TOOLS:
        return METADATA_BYTE_CAP
    if tool == "cc_cause":
        return CAUSE_BYTE_CAP
    return BYTE_CAP


def _trim_focus_attributes(attrs: Dict[str, Any], metric_codes) -> Dict[str, Any]:
    """聚焦属性的裁剪：只留**身份键** + **与聚焦指标对应者**（与 KG 侧同语义）。

    依据（实测）：单实例 `attributes` 里 `jvmVersion`/`startTime`/`pid`/`ccfSnapshotId`
    等对根因判定无贡献；而 `heapUsedRatio`/`gcPauseMs` 恰好对应聚焦指标
    `app.heap_used_ratio` / `app.gc_pause_ms`（末段 snake→camel 匹配）。
    """
    if not attrs:
        return {}
    identity = ("ccfId", "instanceId", "id", "name", "ccfType", "ccfLayer")
    wanted = set()
    for mc in metric_codes or ():
        seg = str(mc).rsplit(".", 1)[-1]
        if not seg:
            continue
        parts = [p for p in re.split(r"[._]", seg) if p]
        camel = (parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])) if parts else ""
        wanted.add(camel.lower())
        wanted.add(re.sub(r"[^a-z0-9]", "", seg.lower()))
    out = {}
    for k, v in attrs.items():
        kn = re.sub(r"[^a-z0-9]", "", str(k).lower())
        if k in identity or kn in wanted:
            out[k] = v
    return out
QUERY_TIMEOUT_S = 4.0          # CF-1 裁定的 G4/G5 值：用户提交查询的硬超时
MAX_CAUSE_DEPTH = 4            # 因果链沿 causes/conditional_causes 的硬上限（R-DEPTH-CAP）
MAX_TRACE_HOPS = 9             # 链路 BFS 上界
DEFAULT_TRACE_HOPS = 2
DEFAULT_DIRECTION = "both"

DEFAULT_PORT = 8302            # :8301 已被 KG 侧占用（含他人服务）
DEFAULT_GRAPHDB_URL = "http://127.0.0.1:7200"
DEFAULT_REPOSITORY = "cc-ops"
INTERNAL_TIMEOUT_S = 30.0      # 服务自身生成的有界查询的传输超时（非用户查询）
INTERNAL_ROW_CAP = 20000       # 服务自身生成的查询的行数安全阀（只用于内部聚合）
TYPE_CACHE_TTL_S = 60.0

ERROR_CODES = (
    "INVALID_PARAM",
    "FORBIDDEN_TOKEN",
    "QUERY_FORM_REJECTED",
    "MULTI_STATEMENT_REJECTED",
    "QUERY_TIMEOUT",
    "EXTERNAL_IO_FORBIDDEN",
    "DEPTH_LIMIT_EXCEEDED",
    "DRY_RUN_ONLY",
)

EXECUTION_INTENT_PARAMS = ("dryRun", "dry_run", "execute", "exec", "apply", "commit", "confirm")

ENVELOPE_FIELDS: List[Tuple[str, str]] = [
    ("hint", "string"),
    ("truncated", "boolean"),
    ("droppedRows", "integer"),
    ("droppedBytes", "integer"),
]

# 与 KG 侧逐字相同的命名纪律字段（被比对，两侧必须一字不差）
NAMING_DISCIPLINE = (
    "字段名一律使用载体中性词：objectTypes / relationTypes / attributes，"
    "不使用具体载体的原生词汇。理由：服务端字段名会随工具返回进入模型上下文，"
    "禁用词纪律要求模型可见文本不含载体词汇，命名纪律是纪律的一部分。"
)

# 排序依据（确定性口径；与 KG 侧语义等价，载体特有名词按本载体改写）
RANK_BASIS = (
    "确定性排序（不合成权重、不给概率）："
    "① 命中的判据条款数降序；"
    "② 同级内：有判定结果的排在不可判定之前；"
    "③ 因果路径跳数升序；"
    "④ 入边关系优先级 causes > conditional_causes（relates_to 属共现，不参与根因收敛）；"
    "⑤ 并列按故障码字典序（保证跨载体逐字可比）；"
    "⑥ 并列全部返回，不合并为单一分数。"
    "★ 2026-10-13 修正：原实现把「有判定结果」放在**第一位**，与 ① 冲突，"
    "并让「零命中但条款全为 FALSE」的候选举压「有命中但部分条款不可判」的候选 ⇒ top1 退化为常量。"
    "规则来源：契约 shared/model/tools-setb.yaml#tools.cc_cause 与 guards.R-DEPTH-CAP"
    "（沿 causes 深度 ≤ 4）；"
    "判据来源：模型层判据个体（属性 jRole / jOp / jOperandKind / jOperandValue，"
    "逐条对应 shared/model/fault-modes.yaml#fault_modes[].detect）；"
    "阈值来源：阈值档位的 tierReference 与 thresholdValue，未标定的档位判为不可判定；"
    "指标观测来源：证据记录（recordId 可溯源，满足硬约束 C5）；"
    "路径关系来源：因果链个体的链接种类与契约边 id（E1..E18，见 shared/model/causal-edges.yaml）。"
)

EMPTY_RESULT_HINT = "本次查询没有返回任何数据：可能是当前快照里没有匹配的对象，或过滤条件过窄。"

GUARD_HINT = "请求被只读守卫拒绝，未执行任何查询；请按 error.code 修正后重试。"

# ---------------------------------------------------------------------------
# 本体 IRIS（真机实测；base = https://credit-card-ops.ai/onto/）
# ---------------------------------------------------------------------------
ONTO_BASE = "https://credit-card-ops.ai/onto/"

PREFIXES: List[Tuple[str, str]] = [
    ("cc", ONTO_BASE + "topology#"),
    ("ccf", ONTO_BASE + "fault#"),
    ("ccm", ONTO_BASE + "metric#"),
    ("cca", ONTO_BASE + "action#"),
    ("ccc", ONTO_BASE + "causal#"),
    ("cco", ONTO_BASE + "core#"),
    ("ccp", ONTO_BASE + "prov#"),
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
    ("owl", "http://www.w3.org/2002/07/owl#"),
    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
    ("skos", "http://www.w3.org/2004/02/skos/core#"),
    ("prov", "http://www.w3.org/ns/prov#"),
    ("dcterms", "http://purl.org/dc/terms/"),
]
PREFIX_BLOCK = "\n".join("PREFIX %s: <%s>" % (k, v) for k, v in PREFIXES)

# 统一的标识解析：属性 coalesce → IRI 本地名兜底（载体特有嵌套项 #idResolution）
# ★ 一次性诊断开关（CC_DEBUG_CAUSE=1 时把 cc_cause 的**中间值**打到 stderr）。
#   存在理由：连续四次从外部脚本『模拟』服务输入来定位 ONTO 侧 sat=0，全部因输入不忠实而作废；
#   正确做法是让**服务自己说话**。故此处只打日志，不改任何响应字段形态（对门禁透明）。
_DEBUG_CAUSE = bool(os.environ.get("CC_DEBUG_CAUSE"))

ID_PROPS: List[str] = [
    "cc:ccfId",
    "cc:recordId",
    "ccf:faultModeCode",
    "ccf:findingCode",
    "ccm:metricCode",
    "ccm:thresholdName",
    "cca:actionCode",
    "cca:proposalCode",
    "cca:riskCode",
    "ccp:ruleCode",
    "cco:neutralCode",
]

# 统一的对象类型解析：ccfType 字面量 → 类 IRI 本地名
TYPE_FALLBACK_CLASSES = (
    "NamedIndividual",
    "Class",
    "Restriction",
    "Ontology",
    "DatatypeProperty",
    "ObjectProperty",
    "AnnotationProperty",
    "FunctionalProperty",
)

# 不作为「关系」暴露的谓语（聚合谓语，另以 observations / 判据形式呈现）
NON_TOPOLOGY_PROPS = ("hasEvidence", "judgedBy", "hasJudgmentGroup", "hasJudgment")

# 因果入边种类与优先级
RELATION_PRIORITY = {"causes": 0, "conditional_causes": 1}
UNRANKED_PRIORITY = 9
CAUSE_LINK_KINDS = ("causes", "conditional_causes")

TRUTHY = ("1", "true", "t", "yes", "y", "up", "on", "ok")

# ---------------------------------------------------------------------------
# 工具面（与 tmp/_m32_manifest.json 逐字一致；描述文本必须与 KG 侧完全相同）
# ---------------------------------------------------------------------------
TOOLFACE: List[Dict[str, Any]] = [
    {
        "name": "cc_guide",
        "description": (
            "列出本环境可用的指标目录、对象类型与规则清单，用于规划后续调用（不含实例数据）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [],
        "returns": [
            {"name": "metrics", "type": "array"},
            {"name": "objectTypes", "type": "array"},
            {"name": "rules", "type": "array"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_schema",
        "description": (
            "返回本环境的对象类型、关系类型与属性清单（只读，不含数据）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [],
        "returns": [
            {"name": "objectTypes", "type": "array"},
            {"name": "relationTypes", "type": "array"},
            {"name": "attributes", "type": "array"},
            {"name": "naming_discipline", "type": "string"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_query",
        "description": (
            "提交一个只读知识查询，返回匹配的对象与属性。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [{"name": "query", "type": "string", "required": True}],
        "returns": [
            {"name": "rows", "type": "array"},
            {"name": "rowCount", "type": "integer"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_facts",
        "description": (
            "取指定对象类型（可限定某个对象与若干指标码）的事实记录。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "objectType", "type": "string", "required": True},
            {"name": "objectId", "type": "string", "required": False},
            {"name": "metricCodes", "type": "array", "required": False},
        ],
        "returns": [
            {"name": "objects", "type": "array"},
            {"name": "relations", "type": "array"},
            {"name": "observations", "type": "array"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_trace",
        "description": (
            "沿指定起始对象返回有界跳数的拓扑链路。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "startId", "type": "string", "required": True},
            {"name": "direction", "type": "string", "required": False},
            {"name": "maxHops", "type": "integer", "required": False},
        ],
        "returns": [
            {"name": "nodes", "type": "array"},
            {"name": "edges", "type": "array"},
            {"name": "hopsUsed", "type": "integer"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_cause",
        "description": (
            "根据症状码返回候选根因及其路径，并给出排序依据（确定性口径，不给概率）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "symptomCodes", "type": "array", "required": True},
            {"name": "maxHops", "type": "integer", "required": False},
        ],
        "returns": [
            {"name": "candidates", "type": "array"},
            {"name": "rankBasis", "type": "string"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
    {
        "name": "cc_propose",
        "description": (
            "基于根因与证据路径给出建议动作（仅建议，不执行）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "rootCause", "type": "string", "required": True},
            {"name": "evidencePath", "type": "array", "required": False},
        ],
        "returns": [
            {"name": "actions", "type": "array"},
            {"name": "executed", "type": "boolean"},
            {"name": "dryRun", "type": "boolean"},
            *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
        ],
    },
]

TOOL_PATHS = {
    "cc_guide": "/guide",
    "cc_schema": "/schema",
    "cc_query": "/query",
    "cc_facts": "/facts",
    "cc_trace": "/trace",
    "cc_cause": "/cause",
    "cc_propose": "/propose",
}


# ---------------------------------------------------------------------------
# 序列化 / 审计 / 错误
# ---------------------------------------------------------------------------
def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _byte_len(obj: Any) -> int:
    return len(_dumps(obj).encode("utf-8"))


# ---------------------------------------------------------------------------
# M3.6 埋点：请求上下文 + query_latency_p50/p95 注册表（与 elapsed_s 分离）
# ---------------------------------------------------------------------------
# ★ 分离口径（M3.6 判据「与 elapsed_s 分离」）：本注册表只接受 dispatch() 的**墙钟样本**
#   （一次调用的 start→return 间隔），它既不从任何响应字段读取 elapsed，也不把聚合值写回
#   任何工具的返回面。单次耗时的落点是审计链的 elapsedMs / elapsed_s，聚合的落点是审计链的
#   query_latency_p50_ms / query_latency_p95_ms（+ 可选遥测文件，见 write_metrics_file）。
#   ⇒ 改单次 elapsed 字段不可能改变 p50/p95；改 p50/p95 也不可能改变任何响应字段。
_REQUEST_CTX: contextvars.ContextVar = contextvars.ContextVar("cc_tools_onto_request", default={})


def new_request_id() -> str:
    """请求 id：16 hex（进程内唯一性由 os.urandom 保证，不含时间/主机信息）。"""
    return os.urandom(8).hex()


def utc_now_iso() -> str:
    """发起时刻（UTC，毫秒精度，ISO-8601，带 Z）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def params_digest(body: Any) -> str:
    """入参摘要：规范化 JSON 的 sha256 前 16 hex（确定性、可比对、不可逆）。"""
    try:
        blob = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # pragma: no cover
        blob = repr(body)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def params_summary(body: Any, limit: int = 400) -> str:
    """入参摘要（可读版）：只给键名与值形状/前 120 字符，不整段回显长载荷。"""
    if not isinstance(body, dict):
        return str(body)[:limit]
    parts: List[str] = []
    for key in sorted(body):
        value = body[key]
        if isinstance(value, str):
            shown = value[:120]
            parts.append("%s=%r" % (key, shown))
        elif isinstance(value, list):
            head = value[0] if value else None
            parts.append("%s=[%d 项%s]" % (key, len(value),
                                           (" 首项=%r" % (head,)) if head is not None else ""))
        else:
            parts.append("%s=%r" % (key, value))
    return (", ".join(parts))[:limit]


class LatencyRegistry:
    """query_latency_p50/p95 埋点：每工具定长滑窗 + 全量计数。

    百分位口径固定为 **nearest-rank**（升序排序后取 index = ceil(p/100*n)-1）：确定性、
    不插值 ⇒ 同一批样本在同一版本上永远给出同一数值（可回归）。
    """

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = int(capacity)
        self._lock = threading.Lock()
        self._samples: Dict[str, List[float]] = {}
        self._counts: Dict[str, int] = {}
        self._status: Dict[str, int] = {}
        self.since = utc_now_iso()

    def observe(self, tool: str, elapsed_ms: float, status: str = "ok") -> None:
        with self._lock:
            bucket = self._samples.setdefault(tool, [])
            bucket.append(float(elapsed_ms))
            if len(bucket) > self.capacity:
                del bucket[0:len(bucket) - self.capacity]
            self._counts[tool] = self._counts.get(tool, 0) + 1
            self._status[status] = self._status.get(status, 0) + 1

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counts.clear()
            self._status.clear()

    @staticmethod
    def percentile(values: Sequence[float], pct: float) -> Optional[float]:
        """nearest-rank 百分位；空样本返回 None（绝不用 0 冒充「没有样本」）。"""
        data = sorted(float(v) for v in values)
        if not data:
            return None
        rank = int(math.ceil(pct / 100.0 * len(data)))
        index = min(max(rank, 1), len(data)) - 1
        return round(data[index], 3)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            samples = {name: list(values) for name, values in self._samples.items()}
            counts = dict(self._counts)
            status = dict(self._status)
        all_samples: List[float] = []
        per_tool: Dict[str, Any] = {}
        for name in sorted(samples):
            values = samples[name]
            all_samples.extend(values)
            p50 = self.percentile(values, 50.0)
            p95 = self.percentile(values, 95.0)
            per_tool[name] = {
                "count": counts.get(name, 0),
                "sampleCount": len(values),
                "p50_ms": p50, "p95_ms": p95,
                "p50_s": None if p50 is None else round(p50 / 1000.0, 6),
                "p95_s": None if p95 is None else round(p95 / 1000.0, 6),
            }
        p50_all = self.percentile(all_samples, 50.0)
        p95_all = self.percentile(all_samples, 95.0)
        return {
            "carrier": CARRIER,
            "arm": ARM,
            "since": self.since,
            "sampleWindowPerTool": self.capacity,
            "requests": {"total": sum(counts.values()), "byTool": counts, "byStatus": status},
            "query_latency_p50": p50_all,
            "query_latency_p95": p95_all,
            "query_latency_p50_ms": p50_all,
            "query_latency_p95_ms": p95_all,
            "query_latency_p50_s": None if p50_all is None else round(p50_all / 1000.0, 6),
            "query_latency_p95_s": None if p95_all is None else round(p95_all / 1000.0, 6),
            "perTool": per_tool,
            "percentileMethod": "nearest-rank：升序排序后取 ceil(p/100*n)-1 位；确定性、不插值；"
                                "空样本为 null（不以 0 冒充）",
            "separation": "本聚合只吃 dispatch() 的墙钟样本；不读、不写任何单次响应的 elapsed 字段"
                          "（elapsedMs / elapsed_s）。与 elapsed_s 相互独立：改单次 elapsed 不可能"
                          "改变 p50/p95，改 p50/p95 也不可能改变任何响应字段。",
        }

    def tool_latency(self, tool: str) -> Dict[str, Any]:
        """单工具实时 p50/p95（审计链埋点用；只读本注册表，与任何响应字段无关）。"""
        with self._lock:
            values = list(self._samples.get(tool, []))
            count = int(self._counts.get(tool, 0))
        p50 = self.percentile(values, 50.0)
        p95 = self.percentile(values, 95.0)
        return {
            "count": count,
            "sampleCount": len(values),
            "p50_ms": p50,
            "p95_ms": p95,
            "p50_s": None if p50 is None else round(p50 / 1000.0, 6),
            "p95_s": None if p95 is None else round(p95 / 1000.0, 6),
        }


LATENCY = LatencyRegistry()


def metrics_path() -> Optional[str]:
    """可选遥测文件路径（M3.6 埋点的第二落点）。

    **默认不写盘**：只有部署方把 CC_TOOLS_ONTO_METRICS 指到某个路径时才落盘。
    这样单测/离线自检不会在仓库里产生副作用文件，也不会因写盘失败影响工具面。
    """
    value = os.environ.get("CC_TOOLS_ONTO_METRICS")
    return value or None


_METRICS_STATE: Dict[str, Any] = {"last": 0.0, "error": None}


def metrics_snapshot() -> Dict[str, Any]:
    """进程内 p50/p95 聚合快照（只读注册表；与 elapsed_s 分离）。"""
    return LATENCY.snapshot()


def write_metrics_file(force: bool = False) -> Optional[str]:
    """把 p50/p95 聚合快照原子写盘（best-effort，节流 1 次/秒）。

    ★ 遥测**不得**影响工具面：任何异常只记进 _METRICS_STATE["error"]，不抛出、不改变响应。
    """
    path = metrics_path()
    if not path:
        return None
    now = time.time()
    interval = float(os.environ.get("CC_TOOLS_ONTO_METRICS_INTERVAL", "1.0"))
    if not force and now - float(_METRICS_STATE["last"]) < interval:
        return None
    _METRICS_STATE["last"] = now
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        snapshot = LATENCY.snapshot()
        snapshot["writtenAt"] = utc_now_iso()
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_dumps(snapshot) + "\n")
        os.replace(tmp_path, path)
        _METRICS_STATE["error"] = None
        return path
    except Exception as exc:  # pragma: no cover - 遥测失败不得冒泡
        _METRICS_STATE["error"] = "%s: %s" % (type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# R-27：审计链**落盘**（追加式 JSONL）
#   银行合规硬要求「完整的事后审计链」：内存环会被冲掉、重启即丢，因此审计记录必须
#   逐调用落盘。落盘文件在本服务重启后**继续追加**，任何情况下都不得清空/截断。
# ---------------------------------------------------------------------------
def audit_path() -> Optional[str]:
    """审计 JSONL 落盘路径（R-27；逐次读环境变量，便于测试与在线改配置）。

    **默认不落盘**：只有部署方把 CC_AUDIT_LOG 指到某个路径时才追加落盘。
    未设 / 空串 ⇒ 返回 None（不落盘、不报错），stderr 审计面保持原样。
    """
    value = os.environ.get("CC_AUDIT_LOG")
    return value or None


_AUDIT_STATE: Dict[str, Any] = {"lines": 0, "error": None, "lastLineAt": None}


def persist_audit_record(record: Dict[str, Any]) -> Optional[str]:
    """把一条审计记录以**追加**方式写成一行 JSON（一次工具调用 = 一行）。

    * 以 "a" 打开 + flush + fsync：进程/机器崩溃后已落盘行仍在；**从不清空/截断**，
      服务重启后继续追加到同一文件；
    * CC_AUDIT_LOG 未设/空 ⇒ 不落盘、不报错（返回 None），默认行为不变；
    * 写入的就是 stderr 审计面上的**同一条记录**（逐字同值，不是另造一份）；
    * ★ 落盘失败**不得**影响工具面：只记进 _AUDIT_STATE["error"]，经 `--metrics`
      （auditWriteError）暴露给运维，绝不抛出、不改响应。
    """
    path = audit_path()
    if not path:
        return None
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_dumps(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _AUDIT_STATE["error"] = None
        _AUDIT_STATE["lines"] = int(_AUDIT_STATE["lines"]) + 1
        _AUDIT_STATE["lastLineAt"] = utc_now_iso()
        return path
    except Exception as exc:  # pragma: no cover - 审计写盘失败不得冒泡
        _AUDIT_STATE["error"] = "%s: %s" % (type(exc).__name__, exc)
        return None


# 审计面字段清单（22 键；R-27 口径 = **不删不缩**）。KG 侧 AUDIT_FIELDS 为 14 项，
# 两侧字段名 camel/snake 的统一归 M3.9，不在 R-27 范围内。
AUDIT_FIELDS: Tuple[str, ...] = (
    "requestId", "tool", "paramsDigest", "paramsSummary", "queryText", "startedAt",
    "elapsedMs", "elapsed_s", "rowCount", "resultBytes", "truncated", "droppedRows",
    "droppedBytes", "truncatedFields", "originalBytes", "droppedEntries",
    "query_latency_p50_ms", "query_latency_p95_ms", "query_latency_p50_s",
    "query_latency_p95_s", "query_latency_sample_count", "status",
)


class AuditChain:
    """进程内审计尾环（容量 500，镜像 KG 侧同类形状）——仅供 /health 运维可见。

    ★ 内存环会被冲掉、重启即丢；**完整的事后审计链由 persist_audit_record()
    追加落盘承担**（R-27）。本类不做任何 IO。
    """

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = int(capacity)
        self._records: "collections.deque" = collections.deque(maxlen=max(1, self.capacity))
        self._total = 0
        self._lock = threading.Lock()

    def append(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)
            self._total += 1

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._records)
        return items[-int(n):] if int(n) > 0 else []

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def total(self) -> int:
        with self._lock:
            return self._total

    def snapshot(self, tail: int = 5) -> Dict[str, Any]:
        return {
            "length": len(self),
            "total": self.total(),
            "capacity": self.capacity,
            "fields": list(AUDIT_FIELDS),
            "tail": self.tail(tail),
        }


AUDIT_CHAIN = AuditChain()


def audit_health() -> Dict[str, Any]:
    """/health 的审计三键（R-27；键名与 KG 侧对齐，取值按 ONTO 自身实现）。

    * auditFile    ：实际落盘路径；未设/空 CC_AUDIT_LOG 时回 ""（与 KG 侧同语义）
    * auditChain   ：进程内尾环快照（length/total/capacity/fields/tail）
    * queryLatency ：与 elapsedMs / elapsed_s 分离的聚合 p50/p95 快照
    """
    return {
        "auditFile": audit_path() or "",
        "auditChain": AUDIT_CHAIN.snapshot(tail=5),
        "queryLatency": metrics_snapshot(),
    }


def audit_log(tool: str, query_text: str, elapsed_ms: float, row_count: int, status: str,
              extra: Optional[Dict[str, Any]] = None, persist: bool = True,
              **fields: Any) -> Dict[str, Any]:
    """审计落点：stderr 一行 + **可选落盘**（CC_AUDIT_LOG 指定时追加一行 JSON）。

    `persist=False`（R-27 用于各 build_* 内部那次 audit）：**内层视图** —— 仍写 stderr
    （既有审计面保持逐字不变），但不进内存尾环、不落盘，并把内层掌握的语义查询文本与
    内层行数并入请求上下文，供 dispatch 的最终记录取用。⇒ JSONL / auditChain 严格
    「一次工具调用 = 一行」，且那一行是信息最全的信封视图。

    M3.6 要求的审计链字段（请求 id / 工具名 / 入参摘要 / 发起时刻 / 耗时 / 返回字节数 /
    是否截断）都在这里落齐；requestId / startedAt / 入参摘要 / 截断记账由 dispatch() 放进
    请求上下文，各 build_* 内部的 audit_log 调用无需逐处改签名即可带上同一份链路字段。
    R-27：同一条 record 再入内存尾环 AUDIT_CHAIN，并在 CC_AUDIT_LOG 非空时追加落盘；
    返回该 record，供自检/单测逐字比对「落盘行 ≡ stderr 行」。
    """
    ctx = _REQUEST_CTX.get() or {}
    record: Dict[str, Any] = {
        "requestId": fields.get("request_id") or ctx.get("requestId") or new_request_id(),
        "tool": tool,
        "paramsDigest": fields.get("params_digest", ctx.get("paramsDigest")),
        "paramsSummary": fields.get("params_summary", ctx.get("paramsSummary")),
        "queryText": (query_text or "")[:4000],
        "startedAt": fields.get("started_at") or ctx.get("startedAt"),
        # 单次耗时（秒 → 毫秒）。★ 这是「单次 elapsed」的唯一落点，与 LATENCY 的聚合互不派生。
        "elapsedMs": round(float(elapsed_ms), 3),
        "elapsed_s": round(float(elapsed_ms) / 1000.0, 6),
        "rowCount": int(row_count),
        "resultBytes": fields.get("result_bytes"),
        "truncated": bool(fields.get("truncated", False)),
        "droppedRows": int(fields.get("dropped_rows", 0)),
        "droppedBytes": int(fields.get("dropped_bytes", 0)),
        "truncatedFields": list(fields.get("truncated_fields") or ctx.get("truncatedFields") or []),
        "originalBytes": fields.get("original_bytes", ctx.get("originalBytes")),
        "droppedEntries": fields.get("dropped_entries", ctx.get("droppedEntries")),
        # M3.6 聚合埋点（**与 elapsedMs / elapsed_s 分离**）：该工具截至本次调用后的实时
        # p50/p95。它们由 LatencyRegistry 的墙钟样本算出，不从上面的 elapsed 字段派生。
        "query_latency_p50_ms": fields.get("latency_p50_ms"),
        "query_latency_p95_ms": fields.get("latency_p95_ms"),
        "query_latency_p50_s": fields.get("latency_p50_s"),
        "query_latency_p95_s": fields.get("latency_p95_s"),
        "query_latency_sample_count": fields.get("latency_sample_count"),
        "status": status,
    }
    if extra:
        record["extra"] = extra
    try:
        sys.stderr.write("AUDIT " + _dumps({"audit": record}) + "\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover
        pass
    # R-27：一次工具调用只让**最终**记录（dispatch 的信封视图）进内存尾环 + 追加落盘
    # （未设 CC_AUDIT_LOG ⇒ 只进环、不落盘）；内层视图（persist=False）只留 stderr。
    if persist:
        AUDIT_CHAIN.append(record)
        persist_audit_record(record)
    else:
        live = _REQUEST_CTX.get()
        if live is not None:
            if query_text:
                live["innerQueryText"] = (query_text or "")[:4000]
            live["innerRowCount"] = int(row_count)
    return record


class ToolError(Exception):
    """请求面错误：只读守卫或参数契约的拒绝。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = {k: v for k, v in detail.items() if v is not None}

    def payload(self, tool: str) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": self.code, "message": self.message, "tool": tool}
        if self.detail:
            err["detail"] = self.detail
        return {
            "error": err,
            "hint": error_hint(self.code),
            "truncated": False,
            "droppedRows": 0,
            "droppedBytes": 0,
        }


class GraphError(Exception):
    """后端错误：超时 / 语法 / 传输 / 查询执行。"""

    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Ledger：行上限 / 字节上限的确定性截断
# ---------------------------------------------------------------------------
class Ledger:
    """按声明顺序消费行预算，再按字节预算从尾部回收，最后拼装信封字段。

    M3.5 截断记账（「不得静默截断冒充完整」）：
      * **返回面字段名不增删**（仍是 hint/truncated/droppedRows/droppedBytes）——工具面被 G24 逐字比对；
      * 「原始大小 / 被截断条目数 / 具体哪些列表被截断」以 **hint 文本**（人可读）如实披露，
        并以 **审计链**（结构化 accounting()）如实可查；
      * 最终拼装后若仍超字节上限，会继续回收行并重新记账，直到严格 ≤ byte_cap
        （或已无可回收列表）⇒ 消除「记账文案把载荷顶过上限」的超限窗口。
      * droppedBytes 的口径 = 回收前探针大小 − 回收后探针大小（两端口径相同，见 _size()）。
    """

    def __init__(self, order: Sequence[str], row_cap: Optional[int] = None,
                 byte_cap: Optional[int] = None) -> None:
        self.order = list(order)
        self.row_cap = ROW_CAP if row_cap is None else int(row_cap)
        self.byte_cap = BYTE_CAP if byte_cap is None else int(byte_cap)
        self.payload: Dict[str, Any] = {}
        self.hint = ""
        self.dropped_rows = 0
        self.dropped_bytes = 0
        self.byte_dropped_rows = 0
        self.truncated = False
        # M3.5 记账（只进 hint 与审计链，不进返回面的字段名集合）
        self.original_rows: Dict[str, int] = {}
        self.original_bytes = 0
        self.full_bytes = 0
        self.truncated_row_fields: List[str] = []
        self.truncated_byte_fields: List[str] = []
        self.dropped_entries = 0
        self.disclosures: Dict[str, str] = {}

    def put(self, name: str, value: Any) -> None:
        self.payload[name] = value

    def _size(self) -> int:
        probe = dict(self.payload)
        probe["hint"] = self.hint
        probe["truncated"] = bool(self.truncated)
        probe["droppedRows"] = int(self.dropped_rows)
        probe["droppedBytes"] = 0
        return _byte_len(probe)

    @property
    def truncated_fields(self) -> List[str]:
        names: List[str] = []
        for name in self.truncated_row_fields + self.truncated_byte_fields:
            if name not in names:
                names.append(name)
        return names

    def cap_rows(self) -> None:
        budget = self.row_cap
        for name in self.order:
            value = self.payload.get(name)
            if not isinstance(value, list):
                continue
            if budget <= 0:
                removed = len(value)
                if removed:
                    self.dropped_rows += removed
                    self.dropped_entries += removed
                    self.truncated_row_fields.append(name)
                    self.payload[name] = []
                    self.truncated = True
                continue
            if len(value) > budget:
                removed = len(value) - budget
                self.dropped_rows += removed
                self.dropped_entries += removed
                self.truncated_row_fields.append(name)
                self.payload[name] = value[:budget]
                self.truncated = True
            budget -= len(self.payload[name])

    def _recover_bytes(self) -> bool:
        """从 reversed(order) 找最后一个非空列表弹尾；无可回收对象时返回 False。"""
        for name in reversed(self.order):
            value = self.payload.get(name)
            if isinstance(value, list) and value:
                value.pop()
                self.byte_dropped_rows += 1
                self.dropped_entries += 1
                if name not in self.truncated_byte_fields:
                    self.truncated_byte_fields.append(name)
                self.truncated = True
                return True
        return False

    def cap_bytes(self) -> None:
        self.full_bytes = self._size()
        guard = 0
        while self._size() > self.byte_cap and guard < 200000:
            if not self._recover_bytes():
                break
            guard += 1
        self.dropped_bytes = max(0, self.full_bytes - self._size())

    def accounting(self) -> Dict[str, Any]:
        """结构化截断记账：只进审计链（stderr），不进返回面。"""
        return {
            "originalBytes": int(self.original_bytes),
            "fullBytesBeforeByteRecovery": int(self.full_bytes),
            "keptBytes": int(self._size()),
            "droppedBytes": int(self.dropped_bytes),
            "droppedRows": int(self.dropped_rows),
            "droppedEntries": int(self.dropped_entries),
            "truncated": bool(self.truncated),
            "originalRows": dict(self.original_rows),
            "returnedRows": {name: len(value) for name, value in self.payload.items()
                             if isinstance(value, list)},
            "truncatedFields": self.truncated_fields,
            "rowCappedFields": list(self.truncated_row_fields),
            "byteCappedFields": list(self.truncated_byte_fields),
        }

    def _publish(self) -> None:
        """把记账写进当前请求上下文，供 dispatch() 的审计链如实落盘。"""
        ctx = _REQUEST_CTX.get()
        if not isinstance(ctx, dict):  # pragma: no cover
            return
        try:
            ctx["truncatedFields"] = self.truncated_fields
            ctx["originalBytes"] = int(self.original_bytes)
            ctx["originalRows"] = dict(self.original_rows)
            ctx["droppedEntries"] = int(self.dropped_entries)
        except Exception:  # pragma: no cover
            pass

    def _envelope(self) -> Dict[str, Any]:
        parts: List[str] = []
        if self.hint:
            parts.append(self.hint)
        if self.dropped_rows:
            parts.append(
                "已达行上限 %d，另有 %d 行被丢弃（droppedRows）；请缩小查询范围。"
                % (self.row_cap, self.dropped_rows)
            )
        if self.dropped_bytes:
            parts.append(
                "已达字节上限 %d，移除 %d 行共 %d 字节（droppedBytes）。"
                % (self.byte_cap, self.byte_dropped_rows, self.dropped_bytes)
            )
        names = self.truncated_fields
        if names:
            detail = "；".join(
                "%s %d→%d" % (name, self.original_rows.get(name, 0),
                              len(self.payload.get(name) or []))
                for name in names
            )
            parts.append(
                "截断记账：原始载荷 %d B、被截断条目 %d 条，被截断列表 %s（原始条目数→返回条目数）。"
                "本次返回**不完整**，缺失部分需缩小范围后重取。"
                % (self.original_bytes, self.dropped_entries, detail)
            )
        for name in names:
            note = self.disclosures.get(name)
            if note:
                parts.append("%s：%s" % (name, note))
        if not parts:
            data_empty = all(not (isinstance(v, list) and v) for v in self.payload.values())
            if data_empty:
                parts.append(EMPTY_RESULT_HINT)
        out = dict(self.payload)
        out["hint"] = " ".join(parts)
        out["truncated"] = bool(self.truncated)
        out["droppedRows"] = int(self.dropped_rows)
        out["droppedBytes"] = int(self.dropped_bytes)
        return out

    def finish(self, hint: str = "") -> Dict[str, Any]:
        self.original_rows = {name: len(value) for name, value in self.payload.items()
                              if isinstance(value, list)}
        self.original_bytes = self._size()
        self.cap_rows()
        self.hint = hint or ""
        self.cap_bytes()
        out = self._envelope()
        # ★ 严格有界：记账文案拼进来之后仍可能超上限 ⇒ 继续回收并重新记账。
        guard = 0
        while _byte_len(out) > self.byte_cap and guard < 200000:
            if not self._recover_bytes():
                break
            guard += 1
            self.dropped_bytes = max(0, self.full_bytes - self._size())
            out = self._envelope()
        self._publish()
        return out


# ---------------------------------------------------------------------------
# M3.5 出口字节护栏：Ledger.finish() 只管**信封**，finish() 之后追加的字段
# （例如 cc_query 的 rowCount）不在其记账范围内 ⇒ 必须在 dispatch 出口再复核一次，
# 才能兑现「任一工具返回 ≤ byteCap」这条契约。**只更新既有 envelope 字段名**，不增删键。
# ---------------------------------------------------------------------------
def _exit_budget_note(cap: int, removed: int, fields: Sequence[str], original_bytes: int) -> str:
    """★ R-23 严格口径：字节路径移除的条目**不**计入 droppedRows（droppedRows 只由行上限驱动）。"""
    return ("出口字节复核：原始载荷 %d B 超上限 %d B，再移除 %d 个条目（%s）并计入 "
            "droppedBytes/droppedEntries；droppedRows 只由行数上限（rowCap）驱动、**不**被字节路径改写。"
            "本次返回**不完整**，请缩小查询范围。"
            % (original_bytes, cap, removed, "/".join(fields) or "—"))


def enforce_payload_budget(payload: Dict[str, Any], byte_cap: Optional[int] = None) -> Dict[str, Any]:
    """出口复核：超限时从**最重的列表**回收条目，并如实更新 truncated/droppedBytes/hint。

    ★ R-23 严格口径：`droppedRows` **只**记因**行数上限**（rowCap）丢弃的行；字节路径移除的条目
    只计入 `droppedBytes`（字节）与 `droppedEntries`（条目数），**不得**改写 wire 的 `droppedRows`。
    ★ 记账文案本身也算字节 ⇒ 回收与记账在**同一个循环**里收敛（否则文案会把载荷重新顶过上限）。
    """
    cap = BYTE_CAP if byte_cap is None else int(byte_cap)
    if not isinstance(payload, dict) or _byte_len(payload) <= cap:
        return payload
    if "droppedRows" not in payload:
        # 工具面逐字比对要求该键必须在；只补 0，绝不承接字节路径的计数（R-23）。
        payload["droppedRows"] = 0
    full = _byte_len(payload)
    base_hint = str(payload.get("hint") or "")
    base_bytes = int(payload.get("droppedBytes") or 0)
    removed = 0
    touched: List[str] = []
    guard = 0
    while guard < 200000:
        guard += 1
        if removed:
            payload["truncated"] = True
            payload["droppedBytes"] = base_bytes + max(0, full - _byte_len(payload))
            payload["hint"] = (base_hint + " " + _exit_budget_note(cap, removed, touched, full)).strip()
        if _byte_len(payload) <= cap:
            break
        best_name: Optional[str] = None
        best_size = -1
        for name, value in payload.items():
            if isinstance(value, list) and value:
                size = _byte_len(value)
                if size > best_size:
                    best_name, best_size = name, size
        if best_name is None:
            break
        payload[best_name].pop()
        if best_name not in touched:
            touched.append(best_name)
        removed += 1
    if removed:
        byte_dropped_rows = removed        # 内部计数：仅供提示/审计，**不**写入 wire 的 droppedRows
        payload["truncated"] = True
        payload["droppedBytes"] = base_bytes + max(0, full - _byte_len(payload))
        payload["hint"] = (base_hint + " " + _exit_budget_note(
            cap, byte_dropped_rows, touched, full)).strip()
        ctx = _REQUEST_CTX.get()
        if isinstance(ctx, dict):
            names = list(ctx.get("truncatedFields") or [])
            for name in touched:
                if name not in names:
                    names.append(name)
            ctx["truncatedFields"] = names
            ctx["droppedEntries"] = int(ctx.get("droppedEntries") or 0) + byte_dropped_rows
    if _byte_len(payload) > cap:
        # 已无更多可回收列表 ⇒ 如实披露，不静默通过（也不假装达标）。
        payload["hint"] = (str(payload.get("hint") or "") +
                           " ★ 出口复核后仍超字节上限，且无可回收列表；如实标注未达标。").strip()
    return payload


# ---------------------------------------------------------------------------
# 参数契约校验（与 KG 侧同码同形）
# ---------------------------------------------------------------------------
def _param_spec(tool: str) -> Optional[List[Dict[str, Any]]]:
    for spec in TOOLFACE:
        if spec["name"] == tool:
            return spec["params"]
    return None


def _check_type(name: str, value: Any, want: str) -> Optional[str]:
    if want == "string":
        if not isinstance(value, str) or not value.strip():
            return "参数 %s 必须是非空字符串。" % name
    elif want == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "参数 %s 必须是整数（拒绝布尔与字符串）。" % name
    elif want == "array":
        if not isinstance(value, list):
            return "参数 %s 必须是字符串数组。" % name
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return "参数 %s 的元素必须是非空字符串。" % name
    elif want == "boolean":
        if not isinstance(value, bool):
            return "参数 %s 必须是布尔值。" % name
    return None


def validate_params(tool: str, body: Any) -> Dict[str, Any]:
    """校验并按契约归一化参数；任何未识别字段/类型不符一律拒绝，不静默回落默认值。"""
    spec = _param_spec(tool)
    if spec is None:
        raise ToolError("INVALID_PARAM", "未识别的工具 %r。" % tool, tool=tool)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ToolError("INVALID_PARAM", "请求体必须是 JSON 对象。", param="body")

    accepted = [p["name"] for p in spec]
    if tool == "cc_propose":
        for name in EXECUTION_INTENT_PARAMS:
            if name in body:
                raise ToolError(
                    "DRY_RUN_ONLY",
                    "本工具只产出建议，不执行任何变更；参数 %s 不被接受。" % name,
                    rejectedParam=name,
                    executed=False,
                    dryRun=True,
                )

    unknown = sorted(k for k in body.keys() if k not in accepted)
    if unknown:
        raise ToolError(
            "INVALID_PARAM",
            "未识别的参数名 %s；本工具接受 %s（不得静默回落默认值）。"
            % (", ".join(unknown), accepted),
            unknownParams=unknown,
            acceptedParams=accepted,
        )

    out: Dict[str, Any] = {}
    for p in spec:
        name, want, required = p["name"], p["type"], bool(p.get("required"))
        if name not in body or body[name] is None:
            if required:
                raise ToolError(
                    "INVALID_PARAM",
                    "缺少必填参数 %s（不得静默回落默认值）。" % name,
                    missingParam=name,
                )
            continue
        problem = _check_type(name, body[name], want)
        if problem:
            raise ToolError("INVALID_PARAM", problem, param=name)
        out[name] = body[name]
    return out


# ---------------------------------------------------------------------------
# 纯函数：数值/布尔/时间序列/判据求值/排序
# ---------------------------------------------------------------------------
def to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def truthy(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in ("0", "false", "f", "no", "n", "down", "off"):
        return False
    return None


def _series_keys(object_id: Any) -> List[str]:
    """对象的观测索引键 —— **精确键**（不做事后归一化）。

    ★★ 2026-10-13 撤回记录 ★★
    本函数一度返回三种等价写法（原样 / 去类型前缀 / 再去类型 slug），
    动机是怀疑「两侧 `id_pat` 产出不同形态的 id ⇒ `series` 键与查找键永不匹配」。
    **该怀疑已被实测证伪**：把 `id_pat` 的**真实输出**跑出来对照，
    证据对象 id（47 个）**全部**是实例 id（309 个）的子集，本就是同一个 id 空间。

    故恢复精确键查找。理由与 §十二 自述一致：**查表处的模糊归一只会掩盖问题** ——
    真正发生「键空间错配」时，模糊匹配会把它静默吞掉，而这正是最该报警的一类故障。
    命中情况仍写进条款记录的 `matchedBy` 供审计。
    """
    return [str(object_id)]


def series_of(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Tuple[str, Any]]]:
    """{(objectId, metricCode): [(capturedAt, rawValue), ...]}，按 capturedAt 升序。

    键为**精确** (objectId, metricCode)；不做等价键展开（见 `_series_keys` 的撤回记录）。
    """
    series: Dict[Tuple[str, str], List[Tuple[str, Any]]] = {}
    for row in rows:
        object_id = row.get("objectId")
        metric_code = row.get("metricCode")
        if object_id is None or metric_code is None:
            continue
        for key_id in _series_keys(object_id):
            series.setdefault((key_id, str(metric_code)), []).append(
                (str(row.get("ts") or ""), row.get("value")))
    for key in series:
        series[key].sort(key=lambda item: item[0])
    return series


def resolve_threshold(ref: Any, thresholds: Dict[str, Dict[str, Any]]) -> Tuple[Optional[float], str, Dict[str, Any]]:
    """解析 ``threshold:NAME`` 或 ``threshold:NAME#level``（level 缺省 warning）。

    ONTO 侧档位未标定（无 thresholdValue）⇒ 返回 (None, ...)，判为**不可判定**。
    """
    text = "" if ref is None else str(ref)
    body = text.split(":", 1)[1] if ":" in text else text
    name, _, level = body.partition("#")
    level = ((level or "warning").strip().rsplit("#", 1)[-1] or "warning")
    entry = thresholds.get(name.strip()) or {}
    value = entry.get(level)
    meta = {
        "thresholdName": name.strip(),
        "level": level,
        "value": value,
        "calibrated": value is not None,
        "uncalibrated": bool(entry.get("uncalibrated")),
    }
    source = "阈值 %s（%s 档）" % (name.strip(), level)
    return (value, source, meta)


def resolve_entity_property(ref: Any, props: Dict[str, Any]) -> Tuple[Optional[float], str, Dict[str, Any]]:
    text = "" if ref is None else str(ref)
    body = text.split(":", 1)[1] if text.startswith("entity_property:") else text
    owner, _, prop = body.partition(".")
    value = props.get(prop)
    number = to_number(value)
    meta = {"entityProperty": prop, "declaredOwner": owner, "resolved": value, "observed": value}
    return (number, "对象属性 %s（对象 %s）" % (prop, owner), meta)


def evaluate_clause(clause: Dict[str, Any], props: Dict[str, Any],
                    thresholds: Dict[str, Dict[str, Any]],
                    series: Dict[Tuple[str, str], List[Tuple[str, Any]]],
                    object_id: Optional[str]) -> Dict[str, Any]:
    """求值单条判据；**不可判定 ⇒ outcome=None（绝不用 False 冒充）**。"""
    metric_code = clause.get("metricCode")
    op = (clause.get("op") or "").strip()
    role = clause.get("role") or ""
    kind = clause.get("operandKind") or ""
    operand_value = clause.get("operandValue")
    predicate = clause.get("predicate")
    enum_values = clause.get("enumValues") or []
    record: Dict[str, Any] = {
        "metricCode": metric_code,
        "op": op,
        "role": role,
        "operandKind": kind,
        "objectId": object_id,
        "predicate": predicate,
        "outcome": None,
    }
    # ★ R2 修正：按等价键依次尝试（原样 → 去类型前缀 → 再去类型 slug），并记录命中方式
    points: List[Tuple[str, Any]] = []
    _matched_by = None
    if object_id is not None:
        for _k in _series_keys(object_id):
            _pts = series.get((_k, str(metric_code)))
            if _pts:
                points, _matched_by = _pts, _k
                break
    observed = to_number(points[-1][1]) if points else None
    record["metadataSource"] = clause.get("metadataSource")
    record["observed"] = observed
    record["matchedBy"] = _matched_by
    record["operand"] = operand_value

    def _undecidable(reason: str) -> Dict[str, Any]:
        record["reason"] = reason
        record["outcome"] = None
        return record

    if role == "exclusion" and kind == "exclusion_clause":
        prop_ok = None
        if predicate == "is_false":
            target = props.get(str(clause.get("predicateProperty") or "")) if clause.get("predicateProperty") else None
            prop_ok = truthy(target)
            if prop_ok is None:
                return _undecidable(
                    "排除条款依赖的属性缺失或不可解析（%s）；按 unjudgeablePolicy 判为不可判定，不冒充 false。"
                    % (clause.get("exclusionRequirement") or clause.get("metadataSource") or "排除条款")
                )
            record["outcome"] = (prop_ok is False)
            record["reason"] = "排除条款：%s 为 false ⇒ 命中该排除条件。" % clause.get("predicateProperty")
            return record
        return _undecidable("排除条款缺少可求值的目标属性；判为不可判定，不冒充 false。")

    if kind == "enum_value":
        if not enum_values:
            return _undecidable("枚举条款没有可用取值集合；判为不可判定。")
        if observed is None:
            return _undecidable("缺少该指标的观测值；判为不可判定。")
        record["operand"] = enum_values
        record["outcome"] = str(points[-1][1]).strip() in {str(v).strip() for v in enum_values}
        record["reason"] = "枚举判定：观测值 %r ∈ %s。" % (points[-1][1], enum_values)
        return record

    if kind == "threshold_reference":
        value, source, meta = resolve_threshold(operand_value, thresholds)
        record["operandDetail"] = meta
        if value is None:
            return _undecidable(
                "阈值档位未标定（%s）；按 unjudgeablePolicy 判为不可判定，不冒充 false。" % source
            )
        if observed is None:
            return _undecidable("缺少该指标的观测值；判为不可判定。")
        record["operand"] = value
        record["reason"] = "%s；观测值 %s，阈值 %s。" % (source, observed, value)
        record["outcome"] = _apply_op(observed, op, value)
        return record

    if kind == "entity_property":
        value, source, meta = resolve_entity_property(operand_value, props)
        record["operandDetail"] = meta
        if value is None:
            return _undecidable("对象属性 %s 未取到数值；判为不可判定。" % source)
        if observed is None:
            return _undecidable("缺少该指标的观测值；判为不可判定。")
        record["operand"] = value
        record["reason"] = "%s；观测值 %s，比较值 %s。" % (source, observed, value)
        record["outcome"] = _apply_op(observed, op, value)
        return record

    if kind == "metric_value":
        value = to_number(operand_value)
        record["operand"] = operand_value
        if value is None and not predicate:
            return _undecidable("字面量操作数不可解析为数值；判为不可判定。")
        if predicate:
            return _eval_predicate(record, predicate, points, clause)
        if observed is None:
            return _undecidable("缺少该指标的观测值；判为不可判定。")
        record["reason"] = "观测值 %s，比较值 %s（%s）。" % (observed, value, op)
        record["outcome"] = _apply_op(observed, op, value)
        return record

    if predicate:
        return _eval_predicate(record, predicate, points, clause)

    if observed is None:
        return _undecidable("缺少该指标的观测值；判为不可判定。")
    value = to_number(operand_value)
    if value is None:
        return _undecidable("操作数不可解析为数值；判为不可判定。")
    record["operand"] = value
    record["reason"] = "观测值 %s，比较值 %s（%s）。" % (observed, value, op)
    record["outcome"] = _apply_op(observed, op, value)
    return record


def _eval_predicate(record: Dict[str, Any], predicate: str,
                    points: List[Tuple[str, Any]], clause: Dict[str, Any]) -> Dict[str, Any]:
    values = [to_number(v) for _, v in points]
    values = [v for v in values if v is not None]
    record["operandDetail"] = dict(record.get("operandDetail") or {}, pointCount=len(values))
    if predicate == "non_zero":
        if not values:
            record["outcome"] = None
            record["reason"] = "缺少观测值；判为不可判定。"
            return record
        record["outcome"] = values[-1] != 0
        record["reason"] = "观测值 %s ≠ 0 判定。" % values[-1]
        return record
    if predicate in ("is_true", "is_false"):
        if record.get("observed") is None and not values:
            record["outcome"] = None
            record["reason"] = "缺少观测值；判为不可判定。"
            return record
        flag = truthy(points[-1][1]) if points else None
        if flag is None:
            record["outcome"] = None
            record["reason"] = "观测值不可解析为布尔；判为不可判定。"
            return record
        record["outcome"] = flag if predicate == "is_true" else (not flag)
        record["reason"] = "布尔判定 %s ⇒ %s。" % (predicate, record["outcome"])
        return record
    if predicate in ("increased", "decreased"):
        if len(values) < 2:
            record["outcome"] = None
            record["reason"] = (
                "趋势判定需要 ≥2 个观测点（当前 %d 个）；判为不可判定，不冒充 false。" % len(values)
            )
            return record
        delta = values[-1] - values[-2]
        record["operandDetail"] = dict(record.get("operandDetail") or {}, delta=delta)
        record["outcome"] = delta > 0 if predicate == "increased" else delta < 0
        record["reason"] = "趋势判定：最近两点差值 %s。" % delta
        return record
    if predicate == "in_enum":
        enum_values = clause.get("enumValues") or []
        if not enum_values or not points:
            record["outcome"] = None
            record["reason"] = "枚举判定缺少取值集合或观测值；判为不可判定。"
            return record
        record["outcome"] = str(points[-1][1]).strip() in {str(v).strip() for v in enum_values}
        record["reason"] = "枚举判定 ∈ %s。" % enum_values
        return record
    record["outcome"] = None
    record["reason"] = "未识别的判定谓词 %r；判为不可判定。" % predicate
    return record


def _apply_op(observed: Optional[float], op: str, expected: Optional[float]) -> Optional[bool]:
    if observed is None or expected is None:
        return None
    if op in (">",):
        return observed > expected
    if op in (">=",):
        return observed >= expected
    if op in ("<",):
        return observed < expected
    if op in ("<=",):
        return observed <= expected
    if op in ("==", "="):
        return observed == expected
    if op in ("!=", "<>"):
        return observed != expected
    return None


def rank_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确定性排序；并列**不合并**，名次写回 rank（1-based）。

    ★★ 2026-10-13 修正（fi-e2e 端到端实测驱动，与 KG 侧逐字同口径）★★
    原键把 `determined` 放在**第一位**，与契约自述 `rankBasis` **①「命中的判据条款数降序」冲突**，
    并使「`determined=True` 但 `satisfiedClauses=0`（零正向证据，条款全为 FALSE）」的候选
    举压「`satisfiedClauses=1` 但 `determined=False`（有正向证据）」的候选。
    实测后果：两侧 top1 各自退化为**常量**（KG 11/12 例同一码、ONTO 10/12 例同一码）⇒ A/B 失去区分度。
    """
    def key(item: Dict[str, Any]) -> Tuple[Any, ...]:
        hops = item.get("hops")
        return (
            -int(item.get("satisfiedClauses") or 0),                        # ① 命中条款数↓（契约第一键）
            0 if item.get("determined") else 1,                             # ② 同级内：有判定结果者在前
            hops if isinstance(hops, int) else 10 ** 6,                     # ③ 跳数↑
            RELATION_PRIORITY.get(str(item.get("viaRelation") or ""), UNRANKED_PRIORITY),  # ④ 入边优先级
            str(item.get("faultCode") or ""),                               # ⑤ 码字典序（跨载体可比）
        )

    ordered = sorted(candidates, key=key)
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered


# ---------------------------------------------------------------------------
# 只读守卫（形名对齐：与 KG 侧逐条语义对应，取值语言按契约声明）
# ---------------------------------------------------------------------------
SPARQL_QUERY_FORMS = ("SELECT", "ASK", "CONSTRUCT", "DESCRIBE")
WRITE_KEYWORDS = ("INSERT", "DELETE", "CLEAR", "DROP", "CREATE", "WITH", "MOVE", "COPY", "ADD")
EXTERNAL_IO_KEYWORDS = ("SERVICE", "LOAD")
CROSS_GRAPH_KEYWORDS = ("GRAPH", "FROM")

_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(WRITE_KEYWORDS) + r")\b", re.I)
_EXTERNAL_IO_RE = re.compile(r"\b(" + "|".join(EXTERNAL_IO_KEYWORDS) + r")\b", re.I)
_CROSS_GRAPH_RE = re.compile(r"\b(" + "|".join(CROSS_GRAPH_KEYWORDS) + r")\b", re.I)
AGGREGATION_RE = re.compile(
    r"\b(count|sum|avg|min|max|group_concat|sample|stdev|stdevp)\s*\(", re.I
)
LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.I)
_SELECT_STAR_RE = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.I)
_COUNT_STAR_RE = re.compile(r"\bCOUNT\s*\(\s*\*\s*\)", re.I)
_UNBOUNDED_RANGE_RE = re.compile(r"\{\s*\d*\s*,\s*\}")
# ★ M3.4 补缺口：SPARQL 1.1 的 `{,n}` 等价 `{0,n}`（Grammar: PathMod ::= '{' ',' Integer '}'）。
#   旧实现的 _UNBOUNDED_RANGE_RE 只认 `,}`、_RANGE_RE 只认 `{` 后紧跟数字，
#   ⇒ `?a <p>{,5}` 这种「低界缺省、高界 5 > 4」的写法整条漏网。此处显式补上。
_OPEN_LOW_RANGE_RE = re.compile(r"\{\s*,\s*(\d+)\s*\}")
_RANGE_RE = re.compile(r"\{\s*(\d+)\s*(?:,\s*(\d+)\s*)?\}")
_IRI_RE = re.compile(r"<[^\s<>\"{}|^`\\]*[:#][^\s<>\"{}|^`\\]*>")


def strip_comments_and_literals(text: str) -> str:
    """把注释与字符串字面量抹成空格（保留 IRI），返回等长视图。

    这样 `#`/引号内的关键字不会被误判为 token。
    """
    out = list(text)
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "<":
            m = _IRI_RE.match(text, i)
            if m:
                i = m.end()
                continue
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            triple = text[i:i + 3] == quote * 3
            delim = quote * 3 if triple else quote
            j = i + len(delim)
            for k in range(i, min(j, n)):
                out[k] = " "
            while j < n:
                if text[j] == "\\":
                    out[j] = " "
                    if j + 1 < n:
                        out[j + 1] = " "
                    j += 2
                    continue
                if text.startswith(delim, j):
                    for k in range(j, min(j + len(delim), n)):
                        out[k] = " "
                    j += len(delim)
                    break
                out[j] = " "
                j += 1
            i = j
            continue
        i += 1
    return "".join(out)


def _query_form(text: str) -> Optional[str]:
    """跳过前导 PREFIX/BASE 声明，返回查询形式关键字（大写）。"""
    body = text.strip()
    while True:
        m = re.match(
            r"^(?:PREFIX\s+[A-Za-z_][\w.\-]*:\s*<[^>]*>|BASE\s*<[^>]*>)\s*",
            body,
            re.I,
        )
        if not m:
            break
        body = body[m.end():]
    m = re.match(r"^\s*([A-Za-z]+)", body)
    return m.group(1).upper() if m else None


def count_statements(text: str) -> int:
    """统计**语句层（花括号深度 0）**的分号个数：SPARQL 的 `;` 是三元组内谓词-宾语表分隔符。"""
    view = strip_comments_and_literals(text)
    return sum(1 for i, ch in enumerate(view) if ch == ";" and view.count("{", 0, i) <= view.count("}", 0, i))


def guard_query(query: Any) -> str:
    """只读守卫。任何一处不通过一律拒绝（只拒绝，不截断、不改写）。"""
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_PARAM", "参数 query 必须是非空字符串。", param="query")

    view = strip_comments_and_literals(query); stripped = view.rstrip()
    marks = [i for i, ch in enumerate(view) if ch == ";" and view.count("{", 0, i) <= view.count("}", 0, i)]
    semicolons = len(marks)
    if semicolons > 1 or (semicolons == 1 and stripped[marks[0] + 1:].strip()):
        raise ToolError(
            "MULTI_STATEMENT_REJECTED",
            "分号出现在语句中部 ⇒ 视为多语句请求，拒绝执行。",
            semicolonCount=semicolons,
        )

    external = _EXTERNAL_IO_RE.search(view)
    if external:
        raise ToolError(
            "EXTERNAL_IO_FORBIDDEN",
            "禁止外部 IO token：命中 '%s'（避免服务端拉取外部数据）。" % external.group(1).upper(),
            matchedToken=external.group(1).upper(),
        )
    forbidden = _FORBIDDEN_RE.search(view)
    if forbidden:
        raise ToolError(
            "FORBIDDEN_TOKEN",
            "只读查询不得含写操作 token：命中 '%s'。" % forbidden.group(1).upper(),
            matchedToken=forbidden.group(1).upper(),
        )

    # G9：禁止显式数据集/命名图子句（跨图访问）。放在写 token 之后，避免
    # "CLEAR GRAPH <g>" 这类写操作被误报成跨图错误（错误码要与 KG 侧语义对齐）。
    cross = _CROSS_GRAPH_RE.search(view)
    if cross:
        raise ToolError(
            "EXTERNAL_IO_FORBIDDEN",
            "禁止显式数据集/命名图子句：命中 '%s'（本服务已按快照作用域注入数据集，"
            "不接受跨图访问）。" % cross.group(1).upper(),
            matchedToken=cross.group(1).upper(),
        )

    form = _query_form(stripped if semicolons == 0 else stripped[:stripped.rfind(";")])
    if form not in SPARQL_QUERY_FORMS:
        raise ToolError(
            "QUERY_FORM_REJECTED",
            "查询起始形式不在只读白名单内：%s。" % ("、".join(SPARQL_QUERY_FORMS)),
            whitelist=list(SPARQL_QUERY_FORMS),
            got=form,
        )

    depth_view = _SELECT_STAR_RE.sub("SELECT ", view)
    depth_view = _COUNT_STAR_RE.sub("COUNT(1)", depth_view)
    for m in re.finditer(r"[*+]", depth_view):
        prev = depth_view[m.start() - 1] if m.start() else " "
        if re.match(r"[A-Za-z0-9_:\]\)\}>]", prev):
            raise ToolError(
                "DEPTH_LIMIT_EXCEEDED",
                "禁止无上界的属性路径重复（`%s`），成环图会爆炸；只接受 {n} / {n,m} 形式的有界重复。"
                % m.group(0),
                pattern=m.group(0),
                limit=MAX_CAUSE_DEPTH,
            )
    if _UNBOUNDED_RANGE_RE.search(depth_view):
        raise ToolError(
            "DEPTH_LIMIT_EXCEEDED",
            "属性路径重复上界缺省 ⇒ 视为无上界，拒绝（只拒绝不截断）。",
            pattern="{n,}",
            limit=MAX_CAUSE_DEPTH,
        )
    for m in _OPEN_LOW_RANGE_RE.finditer(depth_view):
        # `{,n}`：低界缺省为 0，高界是 n（等价 `{0,n}`）⇒ 与 `{n,m}` 同一上界判据。
        high = int(m.group(1))
        if high > MAX_CAUSE_DEPTH:
            raise ToolError(
                "DEPTH_LIMIT_EXCEEDED",
                "路径重复上界 %d > 硬上限 %d（沿因果链的深度 ≤ %d）；"
                "`{,n}` 等价 `{0,n}`，同样受上界约束。"
                % (high, MAX_CAUSE_DEPTH, MAX_CAUSE_DEPTH),
                pattern=m.group(0),
                upperBound=high,
                limit=MAX_CAUSE_DEPTH,
            )
    for m in _RANGE_RE.finditer(depth_view):
        low = int(m.group(1))
        high = int(m.group(2)) if m.group(2) else low
        if high > MAX_CAUSE_DEPTH:
            raise ToolError(
                "DEPTH_LIMIT_EXCEEDED",
                "路径重复上界 %d > 硬上限 %d（沿因果链的深度 ≤ %d）。"
                % (high, MAX_CAUSE_DEPTH, MAX_CAUSE_DEPTH),
                pattern=m.group(0),
                upperBound=high,
                limit=MAX_CAUSE_DEPTH,
            )
    return query


def apply_force_limit(query: str, form: Optional[str]) -> Tuple[str, bool]:
    """没有 LIMIT 且没有聚合 ⇒ 注入 LIMIT (ROW_CAP+1)（等效动作，不改变语义）。"""
    if form == "ASK":
        return query, False
    view = strip_comments_and_literals(query)
    if LIMIT_RE.search(view) or AGGREGATION_RE.search(view):
        return query, False
    return query.rstrip().rstrip(";").rstrip() + " LIMIT %d" % (ROW_CAP + 1), True


PRAGMA_RE = re.compile(
    r"^\s*(PREFIX\s+[A-Za-z][\w.\-]*:\s*<[^>]*>|BASE\s*<[^>]*>)\s*", re.IGNORECASE)


def split_pragmas(query: str) -> Tuple[List[str], str]:
    """拆出前导 PREFIX/BASE 声明与其余查询体。

    计数查询要把用户自带的 PREFIX/BASE 留在顶层：它们放进子查询块是非法的。
    """
    pragmas: List[str] = []
    rest = query.strip()
    while True:
        match = PRAGMA_RE.match(rest)
        if not match:
            break
        pragmas.append(match.group(0).strip())
        rest = rest[match.end():].lstrip()
    return pragmas, rest


def build_count_query(query: str) -> str:
    """把只读查询包成计数子查询（数据集已由协议参数注入，子查询无需重复声明）。"""
    pragmas, rest = split_pragmas(query.rstrip().rstrip(";"))
    head = ("\n".join(pragmas) + "\n") if pragmas else ""
    return "%sSELECT (COUNT(*) AS ?totalRows) WHERE {\n{\n%s\n}\n}" % (head, rest)


# ---------------------------------------------------------------------------
# 后端：GraphDB REST / SPARQL 端点（只读）
# ---------------------------------------------------------------------------
JSON_ACCEPT = "application/sparql-results+json"
RDF_ACCEPT = "application/n-triples"

_NTRIPLE_RE = re.compile(r"^\s*(<[^>]*>|_:[A-Za-z0-9_]+)\s+(<[^>]*>)\s+(.+?)\s*\.\s*$")


def _plain_term(term: Dict[str, Any]) -> Any:
    kind = term.get("type")
    value = term.get("value")
    if kind == "uri":
        return value
    if kind == "bnode":
        return value
    datatype = term.get("datatype") or ""
    local = datatype.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    if local in ("integer", "int", "long", "short", "byte", "nonNegativeInteger",
                 "positiveInteger", "unsignedInt", "unsignedLong"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if local in ("decimal", "double", "float"):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    if local == "boolean":
        return str(value).strip().lower() == "true"
    return value


def _plain_ntriple_object(text: str) -> Any:
    text = text.strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1]
    if text.startswith('"'):
        body = text[1:]
        end = -1
        i = 0
        while i < len(body):
            if body[i] == "\\":
                i += 2
                continue
            if body[i] == '"':
                end = i
                break
            i += 1
        if end >= 0:
            return body[:end]
        return body
    return text


class GraphDBBackend:
    """GraphDB 的只读 SPARQL 客户端（只用 requests，不打 SPARQLWrapper）。"""

    def __init__(self, base_url: str = DEFAULT_GRAPHDB_URL, repository: str = DEFAULT_REPOSITORY,
                 timeout: float = INTERNAL_TIMEOUT_S, user: Optional[str] = None,
                 password: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.repository = repository
        self.endpoint = "%s/repositories/%s" % (self.base_url, repository)
        self.timeout = float(timeout)
        self.user = user
        self.password = password
        self.last_query = ""
        self.last_elapsed_ms = 0.0

    def _auth(self) -> Optional[Tuple[str, str]]:
        if self.user:
            return (self.user, self.password or "")
        return None

    def graphs(self) -> List[str]:
        """列举仓库命名图（内部查询，仅用于定位最新快照图；不受用户查询守卫约束）。"""
        result = self.query("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }",
                            dataset=None, accept=JSON_ACCEPT, timeout=self.timeout)
        names: List[str] = []
        for row in result.get("rows") or []:
            value = row.get("g")
            if value:
                names.append(str(value))
        return names

    def query(self, sparql: str, dataset: Optional[Sequence[str]] = None,
              accept: str = JSON_ACCEPT, timeout: Optional[float] = None) -> Dict[str, Any]:
        # GraphDB/RDF4J 协议实测（tmp/_m33_probe12.py）：一旦给出 dataset 参数，
        # 只有显式列出的图可见 —— 只给 default-graph-uri 时 `GRAPH <iri> {...}` 恒返回 0 行
        # （静默空结果）。因此同一份作用域必须同时作为 default 与 named 图给出：
        # 默认图仍是两图并集（ABox 用户查询不受影响），命名图子句也能命中。
        scope_graphs = list(dataset or [])
        params = ([("default-graph-uri", g) for g in scope_graphs]
                  + [("named-graph-uri", g) for g in scope_graphs])
        effective = self.timeout if timeout is None else float(timeout)
        started = time.time()
        self.last_query = sparql
        try:
            resp = requests.post(
                self.endpoint,
                params=params or None,
                data={"query": sparql},
                headers={"Accept": accept},
                auth=self._auth(),
                timeout=effective,
            )
        except requests.exceptions.Timeout:
            self.last_elapsed_ms = (time.time() - started) * 1000.0
            raise GraphError("timeout", "查询超过 %.1f 秒未返回，已中止。" % effective)
        except requests.exceptions.RequestException as exc:
            self.last_elapsed_ms = (time.time() - started) * 1000.0
            raise GraphError("transport", "无法访问查询端点：%s" % exc)
        elapsed_ms = (time.time() - started) * 1000.0
        self.last_elapsed_ms = elapsed_ms

        if resp.status_code == 400:
            raise GraphError("syntax", (resp.text or "").strip()[:400], status=400)
        if resp.status_code != 200:
            raise GraphError("query", (resp.text or "").strip()[:400], status=resp.status_code)

        if accept == JSON_ACCEPT:
            try:
                body = resp.json()
            except ValueError:
                raise GraphError("transport", "查询端点返回的不是 JSON 结果。")
            if "boolean" in body:
                return {"columns": [], "rows": [{"boolean": bool(body["boolean"])}],
                        "elapsedMs": elapsed_ms, "kind": "boolean"}
            head = body.get("head", {}).get("vars", [])
            rows: List[Dict[str, Any]] = []
            for binding in body.get("results", {}).get("bindings", []):
                rows.append({name: _plain_term(binding[name]) for name in binding})
            return {"columns": list(head), "rows": rows, "elapsedMs": elapsed_ms, "kind": "table"}

        rows = []
        for line in (resp.text or "").splitlines():
            m = _NTRIPLE_RE.match(line)
            if not m:
                continue
            rows.append({
                "subject": _plain_ntriple_object(m.group(1)),
                "predicate": _plain_ntriple_object(m.group(2)),
                "object": _plain_ntriple_object(m.group(3)),
            })
        return {"columns": ["subject", "predicate", "object"], "rows": rows,
                "elapsedMs": elapsed_ms, "kind": "graph"}


# ---------------------------------------------------------------------------
# SPARQL 片段构造
# ---------------------------------------------------------------------------
def sparql_string(value: Any) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return '"%s"' % text


def local_name_expr(var: str) -> str:
    return 'REPLACE(STR(?%s), "^.*[#/]", "")' % var


def class_iri_pat(var: str) -> str:
    """本机**实际**类 IRI 的绑定块（逐条取自本机返回；本机没有 ⇒ 该行不绑定）。

    顺序约束：必须排在引用 `?<var>_c` 的 BIND **之前**。实测：把该块放在
    `BIND(COALESCE(?<var>_tt, REPLACE(STR(?<var>_c), ...)) AS ?objectType)` 之后时，
    本机对该变量一律不绑定（取不到类 IRI）；移到 BIND 之前即可逐行取到。
    """
    return ("    OPTIONAL { ?%s a ?%s_c . FILTER(isIRI(?%s_c)) "
            "FILTER(STRSTARTS(STR(?%s_c), %s)) }"
            % (var, var, var, var, sparql_string(ONTO_BASE)))


def short_iri(iri: Optional[str]) -> Optional[str]:
    """把本机返回的完整 IRI 缩成本服务自身前缀表里的 `<前缀>:<本地名>`。

    依据：这张前缀表就是本服务向本机发起检索时用的同一张表（`PREFIX_BLOCK`），
    `<前缀>:<本地名>` 因此仍然是本机可用的原生标识，只是省字节。
    不在表内 ⇒ **原样**返回完整 IRI，绝不按命名规则拼写。
    """
    if not iri:
        return None
    text = str(iri)
    hit = None
    for prefix, base in PREFIXES:
        if text.startswith(base) and len(text) > len(base):
            if hit is None or len(base) > len(hit[1]):
                hit = (prefix, base)
    if hit is None:
        return text
    return "%s:%s" % (hit[0], text[len(hit[1]):])


def native_ref(value: Optional[str]) -> Optional[str]:
    """原生标识落位：只接受本机实际返回的取值；取不到 ⇒ None（由调用方记账，绝不猜测）。"""
    if not value:
        return None
    text = str(value)
    return text if text else None


def native_ref_hint(missing: Sequence[Tuple[str, str]]) -> str:
    """原生标识缺口如实记账（不猜测、不静默）：缺一条就在 hint 里说一条。"""
    if not missing:
        return ""
    shown = "、".join("%s:%s" % (kind, name) for kind, name in list(missing)[:5])
    tail = "…" if len(missing) > 5 else ""
    return ("有 %d 条未取到原生标识，已置 nativeRef=null（不按命名规则猜测）：%s%s"
            % (len(missing), shown, tail))


def _indent(text: str, spaces: int) -> str:
    """给多行 SPARQL 片段统一加缩进（空行保持原样）。"""
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.split("\n"))


def id_pat(var: str) -> str:
    """统一的标识解析：属性 coalesce → IRI 本地名兜底（丢弃无法解析端点的行）。

    产出的 ?<var>_id 一定是非空字符串；端点不是 IRI（空白节点）时该行不可用。
    """
    lines: List[str] = ["    FILTER(isIRI(?%s))" % var]
    for index, prop in enumerate(ID_PROPS):
        lines.append("    OPTIONAL { ?%s %s ?%s_p%d }" % (var, prop, var, index))
    coalesce = ", ".join("?%s_p%d" % (var, i) for i in range(len(ID_PROPS)))
    lines.append("    BIND(COALESCE(%s, %s) AS ?%s_id)"
                 % (coalesce, local_name_expr(var), var))
    lines.append("    FILTER(BOUND(?%s_id) && STRLEN(STR(?%s_id)) > 0)" % (var, var))
    return "\n".join(lines)


def type_pat(var: str) -> str:
    """统一的对象类型解析：ccfType 字面量 → 类 IRI 本地名。产出 ?<var>_type。"""
    return (
        "    OPTIONAL { ?%s cc:ccfType ?%s_tt }\n"
        "    OPTIONAL { ?%s a ?%s_c . FILTER(isIRI(?%s_c)) "
        "FILTER(STRSTARTS(STR(?%s_c), %s)) }\n"
        "    BIND(COALESCE(?%s_tt, %s) AS ?%s_type)"
        % (var, var, var, var, var, var, sparql_string(ONTO_BASE), var,
           local_name_expr(var + "_c"), var)
    )


def typed_subject_block(var: str) -> str:
    """枚举「本快照环境里的对象」的块。

    划界口径：只认带 `cc:ccfType` 的 ABox 个体（快照环境面）。TBox 类与模型个体
    （Metric/FaultMode/Action/Rule/Chain/... 以及 prov:ProbeAgent 之类的支撑实体）
    不进工具面 —— 否则声明面（/schema、/guide）会给出 /facts 之外的第二套标识空间，
    正是 KG 侧真机 smoke 抓到的 D2 缺陷类。
    """
    return (
        "{ ?%s cc:ccfType ?%s_tt . FILTER(isLiteral(?%s_tt)) "
        "FILTER(STRLEN(STR(?%s_tt)) > 0) }" % (var, var, var, var)
    )


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class CcToolsService:
    def __init__(self, backend: GraphDBBackend, tbox_graph: str, snapshot_graph: str,
                 snapshot_id: str) -> None:
        self.backend = backend
        self.tbox_graph = tbox_graph
        self.snapshot_graph = snapshot_graph
        self.snapshot_id = snapshot_id
        self.scope = [tbox_graph, snapshot_graph]
        # ★ 动态模式 = 未 pin `CC_GRAPHDB_SNAPSHOT_ID`。此时快照要在**每次请求前**重解析，
        #   否则服务启动后新投影的快照永远读不到（事故见 refresh_snapshot_if_dynamic）。
        self.dynamic_snapshot = not bool(snapshot_id)
        self._type_cache: Optional[Dict[str, Any]] = None
        self._type_cache_at = 0.0
        self._props_cache: Dict[str, Dict[str, Any]] = {}
        self._props_cache_at = 0.0
        self._lock = threading.Lock()

    # -- 基础设施 ---------------------------------------------------------
    def run(self, body: str, dataset: Optional[Sequence[str]] = None,
            use_prefix: bool = True) -> List[Dict[str, Any]]:
        sparql = (PREFIX_BLOCK + "\n" if use_prefix else "") + body
        result = self.backend.query(sparql, dataset=dataset or self.scope)
        rows = result.get("rows") or []
        if len(rows) > INTERNAL_ROW_CAP:
            rows = rows[:INTERNAL_ROW_CAP]
        return rows

    def ask(self, pattern: str, dataset: Optional[Sequence[str]] = None) -> bool:
        sparql = PREFIX_BLOCK + "\nASK { %s }" % pattern
        result = self.backend.query(sparql, dataset=dataset or self.scope)
        return bool((result.get("rows") or [{}])[0].get("boolean"))

    def resolve_snapshot(self) -> str:
        """定位最新快照命名图。

        先用仓库图清单（避免把空数据集传给后端），退路是 tbox 图内自述的快照 id。
        """
        prefix = ONTO_BASE + "graph/"
        candidates: List[str] = []
        for name in self.backend.graphs():
            if not name.startswith(prefix):
                continue
            short = name[len(prefix):]
            if not short or short == "tbox" or not short.startswith("ccf-"):
                continue
            candidates.append(short)
        if candidates:
            return sorted(candidates)[-1]
        rows = self.run(
            "SELECT ?sid (COUNT(DISTINCT ?s) AS ?n) WHERE { ?s cc:ccfSnapshotId ?sid } "
            "GROUP BY ?sid ORDER BY DESC(?sid)", dataset=[self.tbox_graph]
        )
        for row in rows:
            if row.get("sid"):
                return str(row["sid"])
        raise GraphError("query", "未能定位可用的快照命名图。")

    def refresh_snapshot_if_dynamic(self) -> bool:
        """★ 动态模式（未 pin `CC_GRAPHDB_SNAPSHOT_ID`）下，**每次工具调用前**重解析最新快照命名图。

        为什么必须这样（实测事故，2026-10-13）：`resolve_snapshot()` 原先**只在进程启动时
        调用一次**（见 `__main__` 的 `if not snapshot_id:`），于是服务启动之后新投影出来的
        快照**永远读不到** —— 实测 ONTO 容器连续 7 个注入用例都返回同一个 3.5 小时前的快照
        `ccf-20261013T044953Z-b82053`，而 KG 侧每一条都是当轮最新快照。
        这违反硬约束 **C1（两侧必须消费同一 `snapshotId`）**，任何「KG vs ONTO」的对比
        **当场作废**（docs/00 §6 亦明文：「两侧消费的快照 ID 不一致即该次测量作废」）。

        显式 pin（env 非空）时**保持原语义不变**，以支持可复现的评测运行。
        返回是否发生了切换（供审计/日志使用）。
        """
        if not self.dynamic_snapshot:
            return False
        try:
            latest = self.resolve_snapshot()
        except Exception:
            return False
        if not latest or latest == self.snapshot_id:
            return False
        self.snapshot_id = latest
        self.snapshot_graph = ONTO_BASE + "graph/" + latest
        self.scope = [self.tbox_graph, self.snapshot_graph]
        # 快照换了 ⇒ 类型/属性索引缓存必须失效，否则会拿旧图的目录回答新图的问题
        self._type_cache = None
        self._type_cache_at = 0.0
        self._props_cache = {}
        self._props_cache_at = 0.0
        return True

    # -- 类型/属性索引（声明面与接受面的唯一来源 ⇒ 保证 D2 闭环） ----------
    def type_index(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            if (not force and self._type_cache is not None
                    and now - self._type_cache_at < TYPE_CACHE_TTL_S):
                return self._type_cache

            counts: Dict[str, int] = {}
            class_iris: Dict[str, str] = {}
            for row in self.run(
                "SELECT ?objectType (COUNT(DISTINCT ?s) AS ?n) (SAMPLE(?layer) AS ?lay) "
                "(SAMPLE(STR(?s_c)) AS ?classIri) WHERE {\n"
                "  " + typed_subject_block("s") + "\n"
                "  " + class_iri_pat("s").strip() + "\n"
                "  BIND(COALESCE(?s_tt, " + local_name_expr("s_c") + ") AS ?objectType)\n"
                "  OPTIONAL { ?s cc:ccfLayer ?layer }\n"
                "} GROUP BY ?objectType"
            ):
                name = row.get("objectType")
                if not name:
                    continue
                counts[str(name)] = int(to_number(row.get("n")) or 0)
                if row.get("classIri"):
                    class_iris[str(name)] = str(row["classIri"])

            attr_rows: List[Dict[str, Any]] = []
            key_attr_candidates: Dict[str, List[str]] = {}
            attr_support: Dict[Tuple[str, str], int] = {}
            for row in self.run(
                "SELECT ?objectType ?attribute ?kind (COUNT(DISTINCT ?s) AS ?n) "
                "(SAMPLE(STR(?p)) AS ?propIri) WHERE {\n"
                "  " + typed_subject_block("s") + "\n"
                "  BIND(COALESCE(?s_tt, " + local_name_expr("s_c") + ") AS ?objectType)\n"
                "  ?s ?p ?o .\n"
                "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?attribute)\n"
                "  BIND(IF(isLiteral(?o), REPLACE(STR(datatype(?o)), \"^.*[#/]\", \"\"), \"iri\") AS ?kind)\n"
                "} GROUP BY ?objectType ?attribute ?kind"
            ):
                owner = row.get("objectType")
                attribute = row.get("attribute")
                if not owner or not attribute:
                    continue
                support = int(to_number(row.get("n")) or 0)
                attr_rows.append({"owner": str(owner), "attribute": str(attribute),
                                  "kind": str(row.get("kind") or "unknown"), "support": support,
                                  "propIri": native_ref(row.get("propIri"))})
                attr_support[(str(owner), str(attribute))] = max(
                    attr_support.get((str(owner), str(attribute)), 0), support
                )

            for owner, total in counts.items():
                candidates = []
                for (row_owner, attribute), support in attr_support.items():
                    if row_owner != owner or support != total:
                        continue
                    if attribute == "ccfSnapshotId":
                        continue
                    kinds = {r["kind"] for r in attr_rows
                             if r["owner"] == owner and r["attribute"] == attribute}
                    if kinds and kinds == {"iri"}:
                        continue
                    candidates.append(attribute)
                key_attr_candidates[owner] = _order_key_attrs(candidates)

            index = {
                "counts": counts,
                "declared": sorted(counts.keys()),
                "keyAttrs": key_attr_candidates,
                "attrRows": attr_rows,
                "classIris": class_iris,
            }
            self._type_cache = index
            self._type_cache_at = now
            return index

    # -- cc_guide ---------------------------------------------------------
    def build_guide(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        # ★ P1：元数据端点用独立上限（行 + 字节），与 KG 侧逐字同语义
        ledger = Ledger(["metrics", "objectTypes", "rules"],
                        row_cap=METADATA_ROW_CAP, byte_cap=METADATA_BYTE_CAP)

        metrics: List[Dict[str, Any]] = []
        for row in self.run(
            "SELECT DISTINCT ?metricCode ?unit ?objectType WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?m a ccm:Metric ; ccm:metricCode ?metricCode .\n"
            "    OPTIONAL { ?m ccm:metricUnit ?unit }\n"
            "    OPTIONAL { ?m ccm:metricObjectType ?ot . "
            "BIND(%s AS ?objectType) }\n"
            "  }\n"
            "} ORDER BY ?metricCode" % (self.tbox_graph, local_name_expr("ot")),
            dataset=[self.tbox_graph],
        ):
            metrics.append({
                "metricCode": row.get("metricCode"),
                "unit": row.get("unit"),
                "objectType": row.get("objectType"),
            })
        ledger.put("metrics", metrics)

        index = self.type_index()
        layers: Dict[str, str] = {}
        for row in self.run(
            "SELECT ?objectType (SAMPLE(?layer) AS ?lay) WHERE {\n"
            "  " + typed_subject_block("s") + "\n"
            "  BIND(COALESCE(?s_tt, " + local_name_expr("s_c") + ") AS ?objectType)\n"
            "  ?s cc:ccfLayer ?layer .\n"
            "} GROUP BY ?objectType"
        ):
            if row.get("objectType"):
                layers[str(row["objectType"])] = row.get("lay")
        object_types = []
        missing_refs: List[Tuple[str, str]] = []
        class_iris = index.get("classIris") or {}
        for name in index["declared"]:
            ref = short_iri(native_ref(class_iris.get(name)))
            if ref is None:
                missing_refs.append(("objectTypes", str(name)))
            object_types.append({
                "objectType": name,
                "layer": layers.get(name),
                "keyAttrs": index["keyAttrs"].get(name, []),
                "nativeRef": ref,
            })
        ledger.put("objectTypes", object_types)

        rules: List[Dict[str, Any]] = []
        for row in self.run(
            "SELECT ?ruleCode ?ruleName ?ruleFile ?faultCode WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?r a ccp:Rule ; ccp:ruleCode ?ruleCode .\n"
            "    OPTIONAL { ?r ccp:ruleName ?ruleName }\n"
            "    OPTIONAL { ?r ccp:ruleFile ?ruleFile }\n"
            "    OPTIONAL { ?r ccp:detectsFaultMode ?fm . BIND(%s AS ?faultCode) }\n"
            "  }\n"
            "} ORDER BY ?ruleCode" % (self.tbox_graph, local_name_expr("fm")),
            dataset=[self.tbox_graph],
        ):
            rules.append({
                "ruleCode": row.get("ruleCode"),
                "ruleKind": "rule",
                "description": "规则 %s：%s；检测 %s；规则文件 %s"
                % (row.get("ruleCode"), row.get("ruleName") or "-",
                   row.get("faultCode") or "-", row.get("ruleFile") or "-"),
            })

        for row in self.run(
            "SELECT ?judgment ?faultCode ?role ?metricCode ?op ?kind ?value ?predicate "
            "?enumValue WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?judgment a ccf:Judgment .\n"
            "    OPTIONAL { ?judgment ccf:judges ?fm . BIND(%s AS ?faultCode) }\n"
            "    OPTIONAL { ?judgment ccf:jRole ?role }\n"
            "    OPTIONAL { ?judgment ccf:jMetricCode ?metricCode }\n"
            "    OPTIONAL { ?judgment ccf:jOp ?op }\n"
            "    OPTIONAL { ?judgment ccf:jOperandKind ?kind }\n"
            "    OPTIONAL { ?judgment ccf:jOperandValue ?value }\n"
            "    OPTIONAL { ?judgment ccf:jPredicate ?predicate }\n"
            "    OPTIONAL { ?judgment ccf:jEnumValue ?enumValue }\n"
            "  }\n"
            "} ORDER BY ?judgment" % (self.tbox_graph, local_name_expr("fm"))
        ):
            fault_code = row.get("faultCode") or "unknown"
            metric_code = row.get("metricCode")
            rules.append({
                "ruleCode": "judge:%s|%s" % (fault_code, metric_code or "-"),
                "ruleKind": "detect_clause",
                "description": clause_text(
                    fault_code, row.get("role"), metric_code, row.get("op"),
                    row.get("kind"), row.get("value"), row.get("predicate"),
                ),
            })

        for row in self.run(
            "SELECT ?edgeId ?causeCode ?effectCode ?chainCode ?linkKind ?evidence WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?link a ccc:CausalLink ; ccc:cause ?c ; ccc:effect ?e .\n"
            "    BIND(%s AS ?edgeId)\n"
            "    BIND(%s AS ?causeCode) BIND(%s AS ?effectCode)\n"
            "    OPTIONAL { ?link ccc:chainCode ?chainCode }\n"
            "    OPTIONAL { ?link ccc:linkKind ?linkKind }\n"
            "    OPTIONAL { ?link ccc:evidenceText ?evidence }\n"
            "  }\n"
            "} ORDER BY ?edgeId"
            % (self.tbox_graph, local_name_expr("link"), local_name_expr("c"),
               local_name_expr("e")),
            dataset=[self.tbox_graph],
        ):
            rules.append({
                "ruleCode": "%s" % row.get("edgeId"),
                "ruleKind": "causal_link",
                "description": "因果关系 %s → %s（链 %s）：%s"
                % (row.get("causeCode"), row.get("effectCode"),
                   row.get("chainCode") or "-", row.get("evidence") or "-"),
            })
        ledger.put("rules", rules)

        payload = ledger.finish(native_ref_hint(missing_refs))
        audit_log("cc_guide", "guide", (time.time() - started) * 1000.0,
                  len(metrics) + len(object_types) + len(rules), "ok", persist=False)
        return payload

    # -- cc_schema --------------------------------------------------------
    def build_schema(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        # ★ P1：同 cc_guide —— 元数据目录必须完整
        ledger = Ledger(["objectTypes", "relationTypes", "attributes"],
                        row_cap=METADATA_ROW_CAP, byte_cap=METADATA_BYTE_CAP)
        # M3.3 记录的 `keyAttrs ⊄ attributes` 是**截断的副作用**而不是数据缺失：
        # 字节回收从 reversed(order) 弹尾，先丢 attributes；objectTypes 内的 keyAttrs 仍指向被丢项。
        # 这里把这件事如实披露到 hint（返回面字段名不变），不再让截断冒充完整。
        ledger.disclosures = {
            "attributes": "objectTypes[].keyAttrs 引用的属性可能不在本次返回的 attributes 内"
                          "（attributes 列表被截断所致；请缩小范围重取，勿按「属性不存在」解读）",
            "objectTypes": "objectTypes 列表被截断：未列出的对象类型本次不可见，"
                           "不等于该类型不存在",
            "relationTypes": "relationTypes 列表被截断：未列出的关系类型本次不可见，"
                             "不等于该关系不存在",
        }
        index = self.type_index()
        missing_refs: List[Tuple[str, str]] = []
        ambiguous_refs: List[Tuple[str, str]] = []
        class_iris = index.get("classIris") or {}

        object_types = []
        for name in index["declared"]:
            ref = short_iri(native_ref(class_iris.get(name)))
            if ref is None:
                missing_refs.append(("objectTypes", str(name)))
            object_types.append({"objectType": name,
                                 "keyAttrs": index["keyAttrs"].get(name, []),
                                 "nativeRef": ref})
        ledger.put("objectTypes", object_types)

        relation_map: Dict[str, Dict[str, Any]] = {}
        for row in self.run(
            "SELECT ?relationType ?fromType ?toType (COUNT(*) AS ?edges) "
            "(COUNT(DISTINCT ?a) AS ?srcs) (COUNT(DISTINCT ?b) AS ?dsts) "
            "(SAMPLE(STR(?p)) AS ?propIri) WHERE {\n"
            "  ?a ?p ?b .\n"
            "  FILTER(isIRI(?a)) FILTER(isIRI(?b)) FILTER(?p != rdf:type)\n"
            "  " + typed_subject_block("a") + "\n"
            "  " + typed_subject_block("b") + "\n"
            "  BIND(COALESCE(?a_tt, " + local_name_expr("a_c") + ") AS ?fromType)\n"
            "  BIND(COALESCE(?b_tt, " + local_name_expr("b_c") + ") AS ?toType)\n"
            "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?relationType)\n"
            "  FILTER(BOUND(?fromType)) FILTER(BOUND(?toType))\n"
            "} GROUP BY ?relationType ?fromType ?toType"
        ):
            relation = row.get("relationType")
            if not relation:
                continue
            entry = relation_map.setdefault(str(relation), {
                "relationType": str(relation), "from": set(), "to": set(),
                "edgeCount": 0, "srcs": 0, "dsts": 0, "propIris": set(),
            })
            entry["from"].add(str(row.get("fromType")))
            entry["to"].add(str(row.get("toType")))
            entry["edgeCount"] += int(to_number(row.get("edges")) or 0)
            entry["srcs"] += int(to_number(row.get("srcs")) or 0)
            entry["dsts"] += int(to_number(row.get("dsts")) or 0)
            if row.get("propIri"):
                entry["propIris"].add(str(row["propIri"]))

        relation_types = []
        for name in sorted(relation_map):
            entry = relation_map[name]
            edges, srcs, dsts = entry["edgeCount"], entry["srcs"], entry["dsts"]
            if edges == srcs == dsts:
                cardinality = "1:1"
            elif edges == srcs:
                cardinality = "N:1"
            elif edges == dsts:
                cardinality = "1:N"
            else:
                cardinality = "N:M"
            iris = sorted(entry["propIris"])
            ref = short_iri(iris[0]) if iris else None
            if ref is None:
                missing_refs.append(("relationTypes", name))
            elif len(iris) > 1:
                ambiguous_refs.append(("relationTypes", "%s（本机同本地名 %d 个 IRI，取最小者）"
                                       % (name, len(iris))))
            relation_types.append({
                "relationType": name,
                "from": sorted(entry["from"]),
                "to": sorted(entry["to"]),
                "cardinality": cardinality,
                "edgeCount": edges,
                "nativeRef": ref,
            })
        ledger.put("relationTypes", relation_types)

        grouped: Dict[Tuple[str, str], List[str]] = {}
        grouped_iris: Dict[Tuple[str, str], set] = {}
        for row in index["attrRows"]:
            grouped.setdefault((row["owner"], row["attribute"]), []).append(row["kind"])
            if row.get("propIri"):
                grouped_iris.setdefault((row["owner"], row["attribute"]), set()).add(row["propIri"])
        attributes = []
        for (owner, attribute) in sorted(grouped):
            kinds = sorted(set(grouped[(owner, attribute)]))
            value_type = kinds[0] if len(kinds) == 1 else "mixed(%s)" % ",".join(kinds)
            iris = sorted(grouped_iris.get((owner, attribute), ()))
            ref = short_iri(iris[0]) if iris else None
            if ref is None:
                missing_refs.append(("attributes", "%s.%s" % (owner, attribute)))
            elif len(iris) > 1:
                ambiguous_refs.append(("attributes", "%s.%s（本机同本地名 %d 个 IRI，取最小者）"
                                       % (owner, attribute, len(iris))))
            attributes.append({"owner": owner, "attribute": attribute,
                               "valueType": value_type, "nativeRef": ref})
        ledger.put("attributes", attributes)
        ledger.put("naming_discipline", NAMING_DISCIPLINE)

        hint_parts = [native_ref_hint(missing_refs)]
        if ambiguous_refs:
            hint_parts.append("有 %d 条原生标识在本机对应多个取值（已取最小者，未猜测）。"
                              % len(ambiguous_refs))
        payload = ledger.finish(" ".join(p for p in hint_parts if p))
        audit_log("cc_schema", "schema", (time.time() - started) * 1000.0,
                  len(object_types) + len(relation_types) + len(attributes), "ok",
                  persist=False)
        return payload

    # -- cc_query ---------------------------------------------------------
    def build_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params["query"]
        started = time.time()
        ledger = Ledger(["rows"])
        guarded = guard_query(query)
        form = _query_form(strip_comments_and_literals(guarded).rstrip().rstrip(";"))
        bounded, injected = apply_force_limit(guarded, form)
        accept = RDF_ACCEPT if form in ("CONSTRUCT", "DESCRIBE") else JSON_ACCEPT
        sparql = PREFIX_BLOCK + "\n" + bounded
        result: Dict[str, Any] = {}
        try:
            result = self.backend.query(sparql, dataset=self.scope, accept=accept,
                                        timeout=QUERY_TIMEOUT_S)
        except GraphError as exc:
            audit_log("cc_query", sparql, (time.time() - started) * 1000.0, 0,
                      "GRAPH_" + exc.kind.upper(), persist=False)
            if exc.kind == "timeout":
                raise ToolError(
                    "QUERY_TIMEOUT",
                    "查询超过 %.1f 秒未返回，已中止（本载体不支持服务端超时，"
                    "由服务端守卫强制）。" % QUERY_TIMEOUT_S,
                    timeoutSeconds=QUERY_TIMEOUT_S,
                    forcedLimitInjected=injected,
                )
            if exc.kind == "syntax":
                raise ToolError(
                    "QUERY_FORM_REJECTED",
                    "查询被查询引擎判为语法/形式错误：%s" % exc.message,
                    engineMessage=exc.message,
                )
            raise

        rows = list(result.get("rows") or [])
        total: Optional[int] = None
        dropped = 0
        if len(rows) > ROW_CAP and form == "SELECT":
            total = self._exact_total(bounded)
            if total is not None:
                dropped = max(0, total - ROW_CAP)
            rows = rows[:ROW_CAP]
        elif len(rows) > ROW_CAP:
            dropped = len(rows) - ROW_CAP
            rows = rows[:ROW_CAP]
        ledger.put("rows", rows)

        hint_parts: List[str] = []
        if total is not None and dropped:
            hint_parts.append("行上限触发：精确总行数 %d，返回 %d 行。" % (total, ROW_CAP))
        elif dropped:
            hint_parts.append(
                "行上限触发：返回前 %d 行，另有至少 %d 行被丢弃（rowCap=%d）。"
                % (ROW_CAP, dropped, ROW_CAP)
            )
        if not rows:
            hint_parts.append("查询无匹配结果：可能是当前快照里没有该对象/关系，或过滤条件过窄。")
        if injected:
            hint_parts.append(
                "服务端已按守卫自动注入行上限（R-FORCE-LIMIT；等效动作，不改变语义）。"
            )
        payload = ledger.finish(" ".join(hint_parts))
        payload["rowCount"] = len(payload.get("rows") or [])
        audit_log("cc_query", sparql, (time.time() - started) * 1000.0,
                  payload["rowCount"], "ok", persist=False)
        return payload

    def _exact_total(self, query: str) -> Optional[int]:
        try:
            rows = self.run(build_count_query(query), use_prefix=False)
        except GraphError:
            return None
        value = to_number(rows[0].get("totalRows")) if rows else None
        if value is None:
            return None
        return int(value)

    # -- P3 聚焦证据（并入 cc_cause）--------------------------------------
    def focus_evidence(self, ordered, values_per_path: int = 3,
                       max_instances: int = 16) -> Dict[str, Any]:
        """★ P3：由**候选**推出**聚焦证据** —— 只取各候选判据涉及的指标。

        为什么并入 `cc_cause` 而不是新开工具（与 KG 侧同一裁定）：
          `cc_cause` 的契约本就是「返回候选根因及其路径」；Agent 拿到候选后**必然**要去
          `cc_facts` 取佐证 —— 实测它为此刻意逐个对象类型穷举（累计 425k–510k tokens、
          撞满 max_iteration ⇒ 7–10/50 例无最终答案）。把「候选 → 佐证」收进同一工具，
          是「工具面越窄越不容易选错」的直接落实，且**不新增 provider / 不动工具清单**。

        聚焦面**不需要新推导**：候选自带 `componentType` / `componentInstance` 与
        `detectEvidence`（逐条款 `metricCode`）⇒
          **聚焦对象 = 候选的实例；聚焦指标 = 该候选判据涉及的那几条指标**。
        """
        focus: Dict[Any, set] = {}
        for c in ordered:
            ct, ci = c.get("componentType"), c.get("componentInstance")
            if not ct or not ci:
                continue
            ms = {r.get("metricCode") for r in (c.get("detectEvidence") or [])
                  if r.get("metricCode")}
            focus.setdefault((str(ct), str(ci)), set()).update(ms)

        led = {"focusInstances": 0, "factsTotal": 0, "factsKept": 0, "factsDropped": 0,
               "pathsReturned": 0, "valuesPerPath": values_per_path,
               "scopedBy": "cause.componentInstance", "instancesSkipped": 0}
        out: List[Dict[str, Any]] = []
        keys = sorted(focus.items())[:max_instances]
        led["instancesSkipped"] = max(0, len(focus) - len(keys))
        for (ct, ci), ms in keys:
            try:
                f = self.build_facts({"objectType": ct, "objectId": ci,
                                      "metricCodes": sorted(ms)})
            except ToolError:
                continue
            obs = f.get("observations") or []
            led["factsTotal"] += len(obs)
            agg: Dict[str, Dict[str, Any]] = {}
            for o in obs:
                mc = str(o.get("metricCode"))
                a = agg.get(mc)
                if a is None:
                    a = {"metricCode": mc, "values": [], "valueCount": 0}
                    agg[mc] = a
                a["valueCount"] += 1
                v = o.get("value")
                if v not in a["values"] and len(a["values"]) < values_per_path:
                    a["values"].append(v)
                else:
                    led["factsDropped"] += 1
            paths = [agg[k] for k in sorted(agg)]
            led["focusInstances"] += 1
            led["factsKept"] += sum(min(a["valueCount"], values_per_path) for a in paths)
            led["pathsReturned"] += len(paths)
            objs = f.get("objects") or []
            out.append({
                "componentType": ct, "componentInstance": ci,
                "metricCodes": sorted(ms),
                "attributes": _trim_focus_attributes(
                    (objs[0].get("attributes") if objs else {}), sorted(ms)),
                "observations": paths,
                "truncated": bool(f.get("truncated")),
            })
        return {"focus": out, "focusLedger": led}

    # -- cc_facts ---------------------------------------------------------
    def build_facts(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        object_type = params["objectType"]
        object_id = params.get("objectId")
        metric_codes = params.get("metricCodes")
        ledger = Ledger(["objects", "relations", "observations"])

        index = self.type_index()
        declared = index["declared"]
        if object_type not in declared:
            raise ToolError(
                "INVALID_PARAM",
                "未声明的对象类型 %r；本环境已声明的对象类型为 %s（不得回落默认值）。"
                % (object_type, declared),
                objectType=object_type,
                declaredObjectTypes=declared,
            )

        statements = [
            "SELECT DISTINCT ?s ?s_id ?s_type ?layer WHERE {",
            "  " + typed_subject_block("s"),
            "  " + id_pat("s"),
            "  " + type_pat("s"),
            "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))",
            "  FILTER(?s_type = %s)" % sparql_string(object_type),
            "  OPTIONAL { ?s cc:ccfLayer ?layer }",
        ]
        if object_id:
            statements.append("  FILTER(?s_id = %s)" % sparql_string(object_id))
        statements.append("} ORDER BY ?s_id")
        id_rows = self.run("\n".join(statements))
        ids = [str(row["s_id"]) for row in id_rows if row.get("s_id")]
        ids = sorted(set(ids))

        attributes: Dict[str, Dict[str, Any]] = {value: {} for value in ids}
        if ids:
            values = ", ".join(sparql_string(value) for value in ids)
            for row in self.run(
                "SELECT ?s_id ?attribute ?value ?kind WHERE {\n"
                "  " + typed_subject_block("s") + "\n"
                "  " + id_pat("s") + "\n"
                "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))\n"
                "  FILTER(?s_id IN (%s))\n"
                "  ?s ?p ?o . FILTER(?p != rdf:type)\n"
                "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?attribute)\n"
                "  BIND(IF(isIRI(?o), %s, STR(?o)) AS ?value)\n"
                "  BIND(IF(isIRI(?o), \"iri\", REPLACE(STR(datatype(?o)), \"^.*[#/]\", \"\")) AS ?kind)\n"
                "} ORDER BY ?s_id ?attribute" % (values, local_name_expr("o"))
            ):
                sid = row.get("s_id")
                if sid is None:
                    continue
                attributes.setdefault(str(sid), {})[str(row.get("attribute"))] = row.get("value")

        objects = [{"objectId": value, "objectType": object_type,
                    "attributes": attributes.get(value, {})} for value in ids]
        ledger.put("objects", objects)

        relations: List[Dict[str, Any]] = []
        if ids:
            values = ", ".join(sparql_string(value) for value in ids)
            for row in self.run(
                "SELECT DISTINCT ?a_id ?b_id ?relationType WHERE {\n"
                "  ?a ?p ?b . FILTER(isIRI(?a)) FILTER(isIRI(?b)) FILTER(?p != rdf:type)\n"
                "  " + typed_subject_block("a") + "\n"
                "  " + id_pat("a") + "\n"
                "  " + id_pat("b") + "\n"
                "  FILTER(?a_id IN (%s))\n"
                "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?relationType)\n"
                "  FILTER(?relationType NOT IN (%s))\n"
                "} ORDER BY ?a_id ?relationType"
                % (values, ", ".join(sparql_string(name) for name in NON_TOPOLOGY_PROPS))
            ):
                if row.get("a_id") is None:
                    continue
                relations.append({
                    "from": row.get("a_id"),
                    "relationType": row.get("relationType"),
                    "to": row.get("b_id") if row.get("b_id") else None,
                })
        relations = [item for item in relations if item["to"] is not None]
        ledger.put("relations", relations)

        observations: List[Dict[str, Any]] = []
        if ids:
            values = ", ".join(sparql_string(value) for value in ids)
            body = [
                "SELECT DISTINCT ?a_id ?recordId ?metricCode ?value ?unit ?ts WHERE {",
                "  ?a cc:hasEvidence ?e .",
                "  " + typed_subject_block("a"),
                "  " + id_pat("a"),
                "  FILTER(isIRI(?a)) FILTER(BOUND(?a_id))",
                "  FILTER(?a_id IN (%s))" % values,
                "  ?e cc:recordId ?recordId ; cc:metricCode ?metricCode ; cc:rawValue ?value .",
                "  OPTIONAL { ?e cc:rawUnit ?unit }",
                "  OPTIONAL { ?e cc:capturedAt ?ts }",
            ]
            if metric_codes:
                body.append("  FILTER(?metricCode IN (%s))"
                            % ", ".join(sparql_string(code) for code in metric_codes))
            body.append("} ORDER BY ?a_id ?recordId")
            for row in self.run("\n".join(body)):
                observations.append({
                    "recordId": row.get("recordId"),
                    "metricCode": row.get("metricCode"),
                    "value": row.get("value"),
                    "unit": row.get("unit"),
                    "ts": row.get("ts"),
                })
        ledger.put("observations", observations)

        hint = ""
        if not objects and not relations and not observations:
            hint = "该对象类型/对象在快照中没有事实记录：请先用 cc_guide 核对对象类型与对象 id。"
        payload = ledger.finish(hint)
        audit_log("cc_facts", "facts(%s)" % object_type, (time.time() - started) * 1000.0,
                  len(objects) + len(relations) + len(observations), "ok", persist=False)
        return payload

    # -- cc_trace ---------------------------------------------------------
    def build_trace(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        start_id = params["startId"]
        direction = params.get("direction") or DEFAULT_DIRECTION
        hops = params.get("maxHops")
        hops = DEFAULT_TRACE_HOPS if hops is None else int(hops)
        if hops < 0:
            raise ToolError("INVALID_PARAM", "maxHops 不能为负。", maxHops=hops)
        if hops > MAX_TRACE_HOPS:
            raise ToolError("INVALID_PARAM", "maxHops=%d 超出上界 %d。" % (hops, MAX_TRACE_HOPS),
                            maxHops=hops, limit=MAX_TRACE_HOPS)
        if direction not in ("out", "in", "both"):
            raise ToolError("INVALID_PARAM",
                            "direction=%r 非法；取值域为 out|in|both（OQ-1）。" % direction,
                            direction=direction, allowed=["out", "in", "both"])

        ledger = Ledger(["nodes", "edges"])
        seeds = self.run(
            "SELECT ?s ?s_id ?s_type ?layer WHERE {\n"
            "  " + id_pat("s") + "\n"
            "  " + type_pat("s") + "\n"
            "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))\n"
            "  FILTER(?s_id = %s)\n"
            "  OPTIONAL { ?s cc:ccfLayer ?layer }\n"
            "} LIMIT 1" % sparql_string(start_id)
        )
        if not seeds:
            ledger.put("nodes", [])
            ledger.put("edges", [])
            payload = ledger.finish(
                "起始对象 %r 不存在：请先用 cc_query 或 cc_facts 确认可用的对象 id。" % start_id
            )
            payload["hopsUsed"] = 0
            audit_log("cc_trace", "trace(%s)" % start_id, (time.time() - started) * 1000.0, 0,
                      "ok", persist=False)
            return payload

        seed = seeds[0]
        nodes: Dict[str, Dict[str, Any]] = {
            str(seed["s_id"]): {
                "objectId": str(seed["s_id"]),
                "objectType": seed.get("s_type"),
                "layer": seed.get("layer"),
            }
        }
        edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        frontier = [str(seed["s_id"])]
        used_hops = 0
        for depth in range(1, hops + 1):
            if not frontier:
                break
            found = self._expand(frontier, direction)
            next_frontier: List[str] = []
            for row in found:
                src, dst = row.get("edgeFrom"), row.get("edgeTo")
                if src is None or dst is None:
                    continue          # D4：绝不 str(None) 造 "None" 节点或自环边
                src, dst = str(src), str(dst)
                if src == dst:
                    continue
                edges[(src, str(row.get("relationType")), dst)] = {
                    "from": src,
                    "relationType": row.get("relationType"),
                    "to": dst,
                }
                if dst not in nodes:
                    nodes[dst] = {
                        "objectId": dst,
                        "objectType": row.get("dstType"),
                        "layer": row.get("dstLayer"),
                    }
                    next_frontier.append(dst)
            frontier = sorted(set(next_frontier))
            if frontier or found:
                used_hops = depth

        ledger.put("nodes", [nodes[key] for key in sorted(nodes)])
        ledger.put("edges", [edges[key] for key in sorted(edges)])
        payload = ledger.finish()
        payload["hopsUsed"] = int(used_hops)
        # ★ P3-fix：随成功响应回传**参数枚举**（实测 agent 会猜 `direction='downstream'`）
        payload["directionEnum"] = ["out", "in", "both"]
        payload["maxHopsLimit"] = MAX_TRACE_HOPS
        audit_log("cc_trace", "trace(%s,%s,%d)" % (start_id, direction, hops),
                  (time.time() - started) * 1000.0, len(nodes) + len(edges), "ok", persist=False)
        return payload

    def _expand(self, frontier: Sequence[str], direction: str) -> List[Dict[str, Any]]:
        """有界一跳扩展：current=前沿端点，nxt=新发现端点（D3：两端都必须可解析 id）。"""
        values = ", ".join(sparql_string(value) for value in frontier)

        def branch(current: str, nxt: str) -> str:
            inner = [
                "    ?%s ?p ?%s . FILTER(isIRI(?%s)) FILTER(isIRI(?%s)) FILTER(?p != rdf:type)"
                % (current, nxt, current, nxt),
                _indent(id_pat(current), 4),
                _indent(id_pat(nxt), 4),
                _indent(type_pat(nxt), 4),
                "    FILTER(?%s_id IN (%s))" % (current, values),
                "    BIND(?%s_id AS ?srcId)" % current,
                "    BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?relationType)",
                "    BIND(?%s_id AS ?dstId)" % nxt,
                "    BIND(?%s_type AS ?dstType)" % nxt,
                "    OPTIONAL { ?%s cc:ccfLayer ?dstLayer }" % nxt,
            ]
            return "  {\n" + "\n".join(inner) + "\n  }"

        branches: List[str] = []
        if direction in ("out", "both"):
            branches.append(branch("a", "b"))
        if direction in ("in", "both"):
            branches.append(branch("b", "a"))
        query = (
            "SELECT DISTINCT ?srcId ?dstId ?relationType ?dstType ?dstLayer WHERE {\n"
            + "\n  UNION\n".join(branches)
            + "\n} LIMIT %d" % INTERNAL_ROW_CAP
        )
        rows = self.run(query)
        return [
            {
                "edgeFrom": row.get("srcId"),
                "edgeTo": row.get("dstId"),
                "relationType": row.get("relationType"),
                "dstType": row.get("dstType"),
                "dstLayer": row.get("dstLayer"),
            }
            for row in rows
        ]

    # -- cc_cause ---------------------------------------------------------
    def build_cause(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        symptom_codes = params["symptomCodes"]
        hops = params.get("maxHops")
        hops = MAX_CAUSE_DEPTH if hops is None else int(hops)
        if hops < 0:
            raise ToolError("INVALID_PARAM", "maxHops 不能为负。", maxHops=hops)
        if hops > MAX_CAUSE_DEPTH:
            raise ToolError(
                "DEPTH_LIMIT_EXCEEDED",
                "maxHops=%d 超出因果链硬上限 %d（不得截断，只能拒绝）。" % (hops, MAX_CAUSE_DEPTH),
                maxHops=hops, limit=MAX_CAUSE_DEPTH,
            )

        # ★ P2c：`cc_cause` 用**自己的**预算（契约 rankBasis ⑥「并列全部返回」要求候选集完整）
        ledger = Ledger(["candidates"], byte_cap=CAUSE_BYTE_CAP)
        fault_rows = self.run(
            "SELECT ?faultCode ?fmName ?layer ?severity ?reversible ?detectionMode "
            "?componentType WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?fm a ccf:FaultMode .\n"
            "    OPTIONAL { ?fm ccf:faultModeCode ?faultCode }\n"
            "    OPTIONAL { ?fm ccf:displayName ?fmName }\n"
            "    OPTIONAL { ?fm ccf:faultLayer ?layer }\n"
            "    OPTIONAL { ?fm ccf:severity ?severity }\n"
            "    OPTIONAL { ?fm ccf:reversible ?reversible }\n"
            "    OPTIONAL { ?fm ccf:fmDetectionMode ?detectionMode }\n"
            "    OPTIONAL { ?fm ccf:fmComponentType ?ct . BIND(%s AS ?componentType) }\n"
            "  }\n"
            "}" % (self.tbox_graph, local_name_expr("ct")),
            dataset=[self.tbox_graph],
        )
        fault_meta: Dict[str, Dict[str, Any]] = {}
        alias: Dict[str, str] = {}
        for row in fault_rows:
            code = row.get("faultCode")
            if not code:
                continue
            code = str(code)
            fault_meta[code] = {
                "faultCode": code,
                "fmName": row.get("fmName"),
                "fmLayer": row.get("layer"),
                "fmSeverity": row.get("severity"),
                "fmReversible": row.get("reversible"),
                "fmDetectionMode": row.get("detectionMode"),
                "componentType": row.get("componentType"),
            }
            alias[code] = code
            if row.get("fmName"):
                alias[str(row["fmName"])] = code

        unknown = [code for code in symptom_codes if code not in alias]
        if unknown:
            raise ToolError(
                "INVALID_PARAM",
                "未识别的症状/故障码 %s；本环境接受 %s（不得回落默认值）。"
                % (unknown, sorted(fault_meta.keys())),
                unknownSymptomCodes=unknown,
                acceptedSymptomCodes=sorted(fault_meta.keys()),
            )
        starts = sorted({alias[code] for code in symptom_codes})

        link_rows = self.run(
            "SELECT ?link ?edgeId ?causeCode ?effectCode ?linkKind ?chainCode WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?link a ccc:CausalLink ; ccc:cause ?c ; ccc:effect ?e .\n"
            "    BIND(%s AS ?edgeId)\n"
            "    BIND(%s AS ?causeCode) BIND(%s AS ?effectCode)\n"
            "    OPTIONAL { ?link ccc:linkKind ?linkKind }\n"
            "    OPTIONAL { ?link ccc:chainCode ?chainCode }\n"
            "  }\n"
            "}" % (self.tbox_graph, local_name_expr("link"), local_name_expr("c"),
                   local_name_expr("e")),
            dataset=[self.tbox_graph],
        )
        upstream: Dict[str, List[Dict[str, Any]]] = {}
        all_edges: List[Dict[str, Any]] = []
        for row in link_rows:
            kind = row.get("linkKind")
            if kind not in CAUSE_LINK_KINDS:
                continue
            source, target = row.get("causeCode"), row.get("effectCode")
            if not source or not target:
                continue
            item = {
                "from": str(target),          # 症状端
                "to": str(source),            # 上游端
                "relation": str(kind),
                "contractId": row.get("edgeId"),
                "chainCode": row.get("chainCode"),
            }
            all_edges.append(item)
            upstream.setdefault(str(target), []).append(item)
        for key in upstream:
            upstream[key].sort(key=lambda item: (RELATION_PRIORITY.get(item["relation"], 9),
                                                 str(item["contractId"])))

        depths: Dict[str, int] = {code: 0 for code in starts}
        parents: Dict[str, Dict[str, Any]] = {}
        frontier = list(starts)
        for depth in range(1, hops + 1):
            next_frontier: List[str] = []
            for code in frontier:
                for edge in upstream.get(code, []):
                    upstream_code = edge["to"]
                    if upstream_code not in depths:
                        depths[upstream_code] = depth
                        parents[upstream_code] = {"from": code, "edge": edge}
                        next_frontier.append(upstream_code)
            frontier = next_frontier
            if not frontier:
                break

        candidate_codes = sorted(code for code in depths if code not in set(starts))
        # ★★ A″（2026-10-15）：把「症状码自身即根因」的**源点**纳入候选集 ★★
        #   （与知识图谱侧的同名实现 `self_root_codes` 逐字同语义；两侧**各自独立实现**，
        #     互不 import、不引用对侧源码 —— 分层边界门禁 L3 要求方案间完全独立）
        #   图论事实（工具级已核验 **23/23**，两载体一致）：`/cause` 返回 0 候选
        #     ⟺ 该码没有 `ccc:CausalLink`（`linkKind ∈ CAUSE_LINK_KINDS`）入边
        #     ⟺ 它是因果图的**源点**（没有上游成因）⇒ 被观测到即自身为根因。
        #   规则与「候选集是否为空」无关：入参含源点就纳入，避免依赖空集的
        #   特例分支。源点自身作为 0 跳候选，走完全相同的条款/实例/焦点管线。
        self_root_codes: List[str] = [c for c in starts if c not in upstream]
        for code in starts:
            if code not in upstream and code not in candidate_codes:
                candidate_codes.append(code)
        candidate_codes = sorted(candidate_codes)
        clause_rows = self.run(
            "SELECT ?faultCode ?judgmentId ?role ?metricCode ?op ?kind ?value ?predicate "
            "?enumValue ?field ?exclusionRequirement ?unjudgeablePolicy WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?judgment a ccf:Judgment .\n"
            "    BIND(%s AS ?judgmentId)\n"
            "    OPTIONAL { ?judgment ccf:judges ?fm . BIND(%s AS ?faultCode) }\n"
            "    OPTIONAL { ?judgment ccf:jRole ?role }\n"
            "    OPTIONAL { ?judgment ccf:jMetricCode ?metricCode }\n"
            "    OPTIONAL { ?judgment ccf:jOp ?op }\n"
            "    OPTIONAL { ?judgment ccf:jOperandKind ?kind }\n"
            "    OPTIONAL { ?judgment ccf:jOperandValue ?value }\n"
            "    OPTIONAL { ?judgment ccf:jPredicate ?predicate }\n"
            "    OPTIONAL { ?judgment ccf:jEnumValue ?enumValue }\n"
            "    OPTIONAL { ?judgment ccf:jOperandEntityProperty ?field }\n"
            "    OPTIONAL { ?judgment ccf:exclusionRequirementText ?exclusionRequirement }\n"
            "    OPTIONAL { ?judgment ccf:unjudgeablePolicy ?unjudgeablePolicy }\n"
            "  }\n"
            "}" % (self.tbox_graph, local_name_expr("judgment"), local_name_expr("fm")),
            dataset=[self.tbox_graph],
        )
        clauses: Dict[str, List[Dict[str, Any]]] = {}
        # ★★ 2026-10-13 修正 P1（判据重复膨胀）★★
        #   上面的 SPARQL 用了一串 `OPTIONAL`；当**一个判据个体对某个 OPTIONAL 属性有多值**时
        #   （实测：`wait_event in_enum` 的判据有 **4 个** `ccf:jEnumValue`），SPARQL 会**扇出成 4 行**。
        #   原实现「一行 = 一条判据」+ `"enumValues": [row.get("enumValue")]` ⇒ **1 条判据被算成 4 条**。
        #   实测后果：`FM-DB-LOCK-WAIT` 契约 2 条、图中 `detect` 判据 2 个，运行时却报 **totalClauses=5**
        #   （4 个枚举值扇出 + 1 条 xact_age_s）—— 而 `evidenceSourceIds` 只列出 2 个判据个体，
        #   正是这一矛盾的直接证据。
        #   修法：**按 (faultCode, judgmentId) 归并**，把同一个个体的多值收进同一个 `enumValues` 列表。
        _by_judgment: Dict[tuple, Dict[str, Any]] = {}
        for row in clause_rows:
            code = row.get("faultCode")
            if not code:
                continue
            jid = str(row.get("judgmentId") or "?")
            key = (str(code), jid)
            ent = _by_judgment.get(key)
            if ent is None:
                ent = {
                    "metricCode": row.get("metricCode"),
                    "op": row.get("op"),
                    "role": row.get("role"),
                    "operandKind": row.get("kind"),
                    "operandValue": row.get("value"),
                    "predicate": row.get("predicate"),
                    "enumValues": [],
                    "predicateProperty": row.get("field"),
                    "exclusionRequirement": row.get("exclusionRequirement"),
                    "unjudgeablePolicy": row.get("unjudgeablePolicy"),
                    "judgmentId": jid,
                    "metadataSource": "判据个体:%s" % jid,
                }
                _by_judgment[key] = ent
                clauses.setdefault(str(code), []).append(ent)
            _ev = row.get("enumValue")
            if _ev is not None and _ev not in ent["enumValues"]:
                ent["enumValues"].append(_ev)

        type_instances: Dict[str, List[str]] = {}
        if candidate_codes:
            for row in self.run(
                "SELECT ?s_id ?s_type WHERE {\n"
                "  " + typed_subject_block("s") + "\n"
                "  " + id_pat("s") + "\n"
                "  BIND(COALESCE(?s_tt, " + local_name_expr("s_c") + ") AS ?s_type)\n"
                "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))\n"
                "}"
            ):
                if row.get("s_type"):
                    type_instances.setdefault(str(row["s_type"]), []).append(str(row["s_id"]))

        thresholds: Dict[str, Dict[str, Any]] = {}
        for row in self.run(
            "SELECT ?thresholdName ?level ?value ?uncalibrated WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?t a ccm:Threshold ; ccm:thresholdName ?thresholdName .\n"
            "    { ?t ccm:warningTier ?tier } UNION { ?t ccm:criticalTier ?tier }\n"
            "    ?tier ccm:tierReference ?level .\n"
            "    OPTIONAL { ?tier ccm:thresholdValue ?value }\n"
            "    OPTIONAL { ?tier ccm:uncalibrated ?uncalibrated }\n"
            "  }\n"
            "}" % self.tbox_graph,
            dataset=[self.tbox_graph],
        ):
            name = row.get("thresholdName")
            level = row.get("level")
            if not name or not level:
                continue
            entry = thresholds.setdefault(str(name), {})
            value = to_number(row.get("value"))
            if value is not None:
                # ★★ 2026-10-13 修正 R2 真因（由服务自己的 CC_DEBUG_CAUSE 诊断指认）★★
                #   `?level` 在 TBox 里绑的是**完整档位引用**（`threshold:acquire_wait_ms_light#warning`），
                #   于是原写作 `entry[str(level)] = value` ⇒ 阈值表的键成了
                #       {'acquire_wait_ms_light': {'threshold:acquire_wait_ms_light#warning': 520.0, ...}}
                #   而 `resolve_threshold()` 是按**裸档位名**（`warning`）查的
                #   （它自己把引用 `rsplit("#",1)[-1]` 归一成档位名）
                #   ⇒ **每一个已标定档位都查不到** ⇒ 阈值类条款恒判「阈值档位未标定 / 不可判定」
                #   ⇒ ONTO 侧 `satisfiedClauses > 0` **恒为 0**（126 个候选无一命中）。
                #
                #   实测证据（`CC_DEBUG_CAUSE=1` + 容器内 `--call cc_cause`）：
                #       thresholds(13)={'acquire_wait_ms_light':
                #                        {'threshold:acquire_wait_ms_light#warning': 520.0, ...}, ...}
                #       FM-APP-POOL-EXHAUSTED: observed=591.0 但
                #       reason='阈值档位未标定（阈值 acquire_wait_ms_light（warning 档））'
                #
                #   修法：**存储端**归一为裸档位名，与 `resolve_threshold()` 的查找端逐字一致。
                #   不在查找端做模糊匹配 —— 那只会掩盖问题（同 §十二 的教训）。
                _lv = str(level).strip().rsplit("#", 1)[-1] or "warning"
                entry[_lv] = value
            if truthy(row.get("uncalibrated")):
                entry["uncalibrated"] = True

        evidence_rows = self.run(
            "SELECT ?objectId ?metricCode ?value ?ts WHERE {\n"
            "  ?a cc:hasEvidence ?e .\n"
            "  " + typed_subject_block("a") + "\n"
            "  " + id_pat("a") + "\n"
            "  FILTER(isIRI(?a)) FILTER(BOUND(?a_id))\n"
            "  ?e cc:metricCode ?metricCode ; cc:rawValue ?value .\n"
            "  OPTIONAL { ?e cc:capturedAt ?ts }\n"
            "  BIND(?a_id AS ?objectId)\n"
            "} ORDER BY ?objectId ?metricCode ?ts"
        )
        series = series_of(evidence_rows)
        if _DEBUG_CAUSE:
            import sys as _sys
            print("[CC_DEBUG] thresholds(%d)=%r" % (len(thresholds), thresholds), file=_sys.stderr)
            print("[CC_DEBUG] evidence_rows=%d series_keys=%d sample=%r"
                  % (len(evidence_rows), len(series), sorted(series)[:4]), file=_sys.stderr)

        candidates: List[Dict[str, Any]] = []
        for code in candidate_codes:
            meta = fault_meta.get(code, {"faultCode": code})
            parent = parents.get(code) or {}
            hop_count = depths.get(code)
            path: List[str] = [code]
            path_edges: List[Dict[str, Any]] = []
            cursor = code
            while cursor in parents:
                edge = parents[cursor]["edge"]
                path_edges.append({
                    "from": edge["from"],
                    "to": edge["to"],
                    "relation": edge["relation"],
                    "contractId": edge["contractId"],
                })
                cursor = parents[cursor]["from"]
                path.append(cursor)
            path.reverse()
            path_edges.reverse()
            via_relation = parent.get("edge", {}).get("relation")
            component_type = meta.get("componentType")
            instances = type_instances.get(str(component_type), []) if component_type else []

            # ★★ 2026-10-13 修正 P2（角色未过滤）★★
            #   契约 `detect` 只声明 `all_of`/`any_of` 主体；ONTO 的角色词表里对应 **`detect`**，
            #   而 `distinguishing`（区分条款）与 `exclusion`（排除条款）**不是**主体。
            #   原实现直接取 `clauses.get(code, [])` 全集 ⇒ `FM-APP-POOL-EXHAUSTED` 多算 1 条
            #   （契约 3 条，实测 4 条 = 3 detect + 1 distinguishing）。
            #   与 KG 侧同口径（KG 只取 necessary/sufficient）；`clauses` 全集仍保留给 condition 文案。
            _DETECT_ROLES = ("detect", "necessary", "sufficient")
            clause_list = [c for c in clauses.get(code, [])
                           if str(c.get("role")) in _DETECT_ROLES]
            if _DEBUG_CAUSE:
                import sys as _sys
                print("[CC_DEBUG] code=%s componentType=%r nClauses=%d instances=%r"
                      % (code, component_type, len(clause_list), list(instances)[:3]),
                      file=_sys.stderr)
                for _c in clause_list:
                    print("[CC_DEBUG]    clause metric=%r kind=%r op=%r operand=%r pred=%r role=%r"
                          % (_c.get("metricCode"), _c.get("operandKind"), _c.get("op"),
                             _c.get("operandValue"), _c.get("predicate"), _c.get("role")),
                          file=_sys.stderr)
            best: Optional[Dict[str, Any]] = None
            best_key: Optional[tuple] = None
            if instances:
                for instance_id in sorted(instances):
                    props = self._props_of(instance_id)
                    records = [evaluate_clause(clause, props, thresholds, series, instance_id)
                               for clause in clause_list]
                    satisfied = sum(1 for record in records if record.get("outcome") is True)
                    undetermined = [r for r in records if r.get("outcome") is None]
                    # ★★ 2026-10-13 修正①②：`determined` 的**定义**原来写错了 ★★
                    #   原：`determined = any(record.get("outcome") is not None ...)`
                    #       ⇒「**任一**条款可判定」就算 determined（把「部分可判」当「已判定」）；
                    #   现：`determined = 全部条款都无 UNK`，与 KG 侧
                    #       `bool(records) and not undetermined` **逐字同语义**。
                    #   契约依据：`rankBasis` 的「不可判定的候选排在有判定结果的候选之后」——
                    #   含 UNK 条款的候选即「不可判定」，故 determined 必须要求**无 UNK**。
                    determined = bool(records) and not undetermined
                    # ★★ 修正②：实例选择键（与 KG 侧逐字同语义）★★
                    #   原键 = `(satisfied, instance_id)` 且用 `>` 比较 ⇒ 平局时取 **id 最大**者；
                    #   KG 侧平局取 **id 最小**者 ⇒ 两侧选中**不同实例**。
                    #   新键在 id 之前插入「UNK 少者优先」⇒ 优先选中**判据能真正求值**的实例。
                    inst_key = (-satisfied, len(undetermined), -len(records), str(instance_id))
                    if best_key is None or inst_key < best_key:
                        best_key = inst_key
                        best = {"instanceId": instance_id, "satisfied": satisfied,
                                "determined": determined, "records": records,
                                "undetermined": undetermined}
            if _DEBUG_CAUSE:
                import sys as _sys
                _b = best or {}
                print("[CC_DEBUG] code=%s bestInst=%r sat=%r total=%r det=%r"
                      % (code, _b.get("instanceId"), _b.get("satisfied"),
                         len(_b.get("records") or []), _b.get("determined")), file=_sys.stderr)
                for _r in (_b.get("records") or []):
                    print("[CC_DEBUG]    -> metric=%r observed=%r operand=%r outcome=%r reason=%s"
                          % (_r.get("metricCode"), _r.get("observed"), _r.get("operand"),
                             _r.get("outcome"), str(_r.get("reason"))[:90]), file=_sys.stderr)
            if best is None:
                records = [evaluate_clause(clause, {}, thresholds, series, None)
                           for clause in clause_list]
                undetermined = [r for r in records if r.get("outcome") is None]
                best = {
                    "instanceId": None,
                    "satisfied": sum(1 for r in records if r.get("outcome") is True),
                    # 与 KG 侧兜底分支同语义：无实例 ⇒ 全部条款不可判 ⇒ determined=False
                    "determined": bool(records) and not undetermined,
                    "records": records,
                    "undetermined": undetermined,
                    "reason": "图上没有 %s 的实例" % (component_type or code),
                }

            total_clauses = len(clause_list)
            satisfied_clauses = best["satisfied"]
            undetermined = [record for record in best["records"] if record.get("outcome") is None]
            detect_evidence = [record for record in best["records"] if record.get("outcome") is not None]
            candidate = dict(meta)
            candidate.update({
                "path": path,
                "pathEdges": path_edges,
                "condition": _condition_summary(best["records"], total_clauses, code),
                "hops": hop_count,
                # ★ N2 修复（2026-10-13）：`relationPriority` 统一发**契约关系名**
                #   （"causes" / "conditional_causes"），与 KG 侧逐字同形。
                #   原实现发的是数字（RELATION_PRIORITY.get(...) = 0/1/9），而 KG 侧发关系名
                #   ⇒ 同一字段在两载体上是**不同量纲**，任何跨载体比较（含并列排序）都不可比，
                #     违反 C3「同名字段一致」。数字优先级仍是**内部**排序键（见 rank_candidates
                #   用的是 viaRelation），不外泄到响应里。
                "relationPriority": via_relation,
                "viaRelation": via_relation,
                "isLeafRootCandidate": not upstream.get(code),
                "componentInstance": best["instanceId"],
                "componentType": component_type,
                "satisfiedClauses": satisfied_clauses,
                "totalClauses": total_clauses,
                "determined": bool(best["determined"]),
                # ★ N3 修复（2026-10-13）：`undeterminedClauses` 统一发**整数计数**（与 KG 同形，
                #   也与同族的 satisfiedClauses / totalClauses 同类型）；原因明细改挂
                #   `undeterminedReasons`（新字段，两侧一致），不再用同一字段名装不同东西。
                "undeterminedClauses": len(undetermined),
                "undeterminedReasons": [str(record.get("reason")) for record in undetermined
                                        if record.get("reason") is not None],
                "detectEvidence": detect_evidence,
                "evidenceSourceIds": sorted({str(record.get("metadataSource"))
                                             for record in best["records"]
                                             if record.get("metadataSource")}),
                "causalContractIds": sorted({edge["contractId"] for edge in path_edges
                                             if edge.get("contractId")}),
            })
            if best.get("reason"):
                candidate["componentInstanceNote"] = best["reason"]
            candidates.append(candidate)

        candidates = rank_candidates(candidates)
        _ordered_full = list(candidates)
        ledger.put("candidates", candidates)
        ledger.put("rankBasis", RANK_BASIS)
        hint = ""
        if not candidates:
            hint = ("在 %d 跳内没有找到上游根因候选：该症状码可能本身就是叶子根因，或因果链未装载。"
                    % hops)
        payload = ledger.finish(hint)
        # ★ P3-fix（2026-10-15）：把**合法入参词表**随**成功**响应一起回传。
        #   实测动机（m3 中期信号）：收窄工具面后 agent 失去 cc_guide/cc_schema 这个
        #   「词表与参数枚举」来源，于是**猜**参数并烧轮次：
        #     · `未识别的症状/故障码 ['LATENCY_SPIKE']`（它自己编了个英文症状名）
        #     · `maxHops=5 超出因果链硬上限 4（不得截断，只能拒绝）`
        #   每猜错一次 400 ⇒ 重试 ⇒ 撞满 max_iteration ⇒ Dify 500、无最终答案。
        #   ⇒ 随成功响应回传（体积很小，23 个码 ≈ 650 B），agent 一次就能看到。
        payload["acceptedSymptomCodes"] = sorted(fault_meta.keys())
        payload["maxHopsLimit"] = MAX_CAUSE_DEPTH
        # ★ A″ 留痕（与 KG 侧**逐字相同**的字段名与文案 —— 载体中立措辞，
        #   不引入新的载体分歧）
        if self_root_codes:
            payload["selfRootCauseCodes"] = sorted(self_root_codes)
            payload["selfRootCauseBasis"] = (
                "这些症状码在因果图上没有上游成因（即因果图源点），"
                "故其自身即根因，以 hops=0 返回；"
                "其判据条款、实例选择、焦点证据与其它候选同法求值。")
            _prev_hint = str(payload.get("hint") or "")
            payload["hint"] = (_prev_hint + " " if _prev_hint else "") + (
                "★ %s 在因果图上**没有上游成因**，因此它自身就是根因"
                "（已作为 hops=0 候选返回）。" % "、".join(sorted(self_root_codes)))
        # ★ P3-fix2（2026-10-15）：把「建议动作」也**服务端串进来** —— 使 `cc_cause` 成为
        #   **一次调用即完整诊断**（候选 + 聚焦证据 + 建议）。与 KG 侧逐字同语义。
        #   参考项目 `onto_diagnose` 的 docstring 把这条讲透了（逐字）：
        #     「…**"靠提示词让模型记住四步流程"是把确定性工作交给概率性执行者。**
        #       本端点把四步收敛成**一次调用**。」
        #   实测佐证（本项目 m3）：即便给了 `focus[]`，agent 仍会在 20 轮预算里反复探索
        #   （Q 臂 avg position 20.8、85% 撞顶 ⇒ Dify 500、无最终答案）。
        try:
            if candidates:
                _top = str(candidates[0].get("faultCode"))
                _pr = self.build_propose({"rootCause": _top,
                                          "evidencePath": [str(x) for x in
                                                           (candidates[0].get("path") or [])]})
                payload["proposedActions"] = _pr.get("actions") or []
                payload["proposedFor"] = _top
                payload["executed"] = False
                payload["dryRun"] = True
        except Exception as _exc:      # noqa: BLE001 —— **不静默**：失败也如实回传
            payload["proposedActions"] = []
            payload["proposeError"] = str(_exc)[:200]
        # ★ P2c 记账修复（与 KG 侧逐字同语义）：`cap_bytes()` 从列表尾部 pop，只累加
        #   `byte_dropped_rows` 而不写 `droppedRows` ⇒ 候选被丢却在账上看不出来
        #   （契约 C6「不得静默截断」）。把「丢了几个、丢了谁」显式写回。
        returned = payload.get("candidates") or []
        if len(returned) < len(_ordered_full):
            lost = [str(c.get("faultCode")) for c in _ordered_full[len(returned):]]
            payload["droppedCandidates"] = lost
            payload["droppedCandidateCount"] = len(lost)
            payload["candidatesTruncated"] = True
            _tr = dict(payload.get("truncation") or {})
            _tr["droppedCandidateCount"] = len(lost)
            payload["truncation"] = _tr
            _prev = str(payload.get("hint") or "")
            payload["hint"] = (_prev + " " if _prev else "") + (
                "★ 候选集被字节上限截断：缺失 %d 个候选（%s）。契约 rankBasis ⑥ 要求"
                "「并列全部返回」，故本次返回**不完整**；请缩小症状范围或降低 maxHops 重试，"
                "不要据残缺候选集下「唯一根因」的结论。"
                % (len(lost), "、".join(lost))).strip()
        # ★ P3：并入**聚焦证据**（把「候选 → 佐证」收进同一工具，替代逐个对象穷举 cc_facts）
        try:
            _fe = self.focus_evidence(candidates)
            payload["focus"] = _fe["focus"]
            payload["focusLedger"] = _fe["focusLedger"]
        except Exception as _exc:      # noqa: BLE001 —— **不静默**：失败也如实回传
            payload["focus"] = []
            payload["focusLedger"] = {"error": str(_exc)[:200], "focusInstances": 0}
        audit_log("cc_cause", "cause(%s)" % ",".join(symptom_codes),
                  (time.time() - started) * 1000.0, len(candidates), "ok", persist=False)
        return payload

    def _props_index(self, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """一次性取回全部 ABox 个体的属性（按对象 id 索引）。

        动机：逐对象查询时每个对象都要重新扫描全库（GraphDB 单查询固定开销约 0.7 s），
        cc_cause 会为每个候选的每个组件实例查一次属性（实测 41 次 ⇒ 28.7 s）。
        改为单次批量查询 + 缓存（TTL 与 type_index 一致）。
        """
        now = time.time()
        with self._lock:
            if (not force and self._props_cache
                    and now - self._props_cache_at < TYPE_CACHE_TTL_S):
                return self._props_cache
            rows = self.run(
                "SELECT ?s_id ?attribute ?value WHERE {\n"
                "  " + typed_subject_block("s") + "\n"
                "  " + id_pat("s") + "\n"
                "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))\n"
                "  ?s ?p ?o . FILTER(?p != rdf:type)\n"
                "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?attribute)\n"
                "  BIND(IF(isIRI(?o), %s, STR(?o)) AS ?value)\n"
                "}" % local_name_expr("o")
            )
            index: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                subject_id = row.get("s_id")
                attribute = row.get("attribute")
                if not subject_id or not attribute:
                    continue
                index.setdefault(str(subject_id), {})[str(attribute)] = row.get("value")
            self._props_cache = index
            self._props_cache_at = now
            return index

    def _props_of(self, object_id: str) -> Dict[str, Any]:
        index = self._props_index()
        if object_id in index:
            return index[object_id]
        rows = self.run(
            "SELECT ?attribute ?value WHERE {\n"
            "  " + id_pat("s") + "\n"
            "  FILTER(isIRI(?s)) FILTER(BOUND(?s_id))\n"
            "  FILTER(?s_id = %s)\n"
            "  ?s ?p ?o . FILTER(?p != rdf:type)\n"
            "  BIND(REPLACE(STR(?p), \"^.*[#/]\", \"\") AS ?attribute)\n"
            "  BIND(IF(isIRI(?o), %s, STR(?o)) AS ?value)\n"
            "}" % (sparql_string(object_id), local_name_expr("o"))
        )
        props: Dict[str, Any] = {}
        for row in rows:
            if row.get("attribute"):
                props[str(row["attribute"])] = row.get("value")
        self._props_cache[object_id] = props
        return props

    # -- cc_propose -------------------------------------------------------
    def build_propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        started = time.time()
        root_cause = params["rootCause"]
        evidence_path = params.get("evidencePath") or []
        ledger = Ledger(["actions"])

        action_rows = self.run(
            "SELECT ?actionCode ?actionName ?riskLevel ?expectedEffect ?rationale "
            "?sideEffect ?precondition ?faultCode WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?a a cca:Action ; cca:actionCode ?actionCode .\n"
            "    OPTIONAL { ?a cca:displayName ?actionName }\n"
            "    OPTIONAL { ?a cco:riskLevel ?riskLevel }\n"
            "    OPTIONAL { ?a cca:expectedEffect ?expectedEffect }\n"
            "    OPTIONAL { ?a cca:rationale ?rationale }\n"
            "    OPTIONAL { ?a cca:sideEffectNote ?sideEffect }\n"
            "    OPTIONAL { ?a cca:preconditionNote ?precondition }\n"
            "    OPTIONAL { ?a cca:actionOnFault ?fm . BIND(%s AS ?faultCode) }\n"
            "  }\n"
            "} ORDER BY ?actionCode" % (self.tbox_graph, local_name_expr("fm"))
        )
        proposal_rows = self.run(
            "SELECT ?proposalCode ?actionCode ?faultCode ?rationale ?executed ?dryRun WHERE {\n"
            "  GRAPH <%s> {\n"
            "    ?p a cca:ActionProposal ; cca:proposalCode ?proposalCode .\n"
            "    OPTIONAL { ?p cca:proposalAction ?a2 . ?a2 cca:actionCode ?actionCode }\n"
            "    OPTIONAL { ?p cca:proposalForFinding ?f . "
            "?f ccp:findingFaultMode ?fm . BIND(%s AS ?faultCode) }\n"
            "    OPTIONAL { ?p cca:rationale ?rationale }\n"
            "    OPTIONAL { ?p cca:executed ?executed }\n"
            "    OPTIONAL { ?p cca:dryRun ?dryRun }\n"
            "  }\n"
            "}" % (self.tbox_graph, local_name_expr("fm"))
        )

        proposals: Dict[str, Dict[str, Any]] = {}
        for row in proposal_rows:
            action_code = row.get("actionCode")
            if not action_code:
                continue
            proposals.setdefault(str(action_code), []).append(row)

        matched: List[Dict[str, Any]] = []
        for row in action_rows:
            action_code = row.get("actionCode")
            if not action_code:
                continue
            fault_code = row.get("faultCode")
            related = proposals.get(str(action_code), [])
            reasons: List[str] = []
            if fault_code and str(fault_code) == str(root_cause):
                reasons.append("该动作声明作用于故障模式 %s" % root_cause)
            elif not fault_code and not evidence_path:
                reasons.append("动作目录未声明作用域，按全量候选返回")
            for proposal in related:
                prop_fault = proposal.get("faultCode")
                if prop_fault and str(prop_fault) == str(root_cause):
                    reasons.append("提案 %s：%s" % (proposal.get("proposalCode"),
                                                    proposal.get("rationale") or "-"))
            if not reasons:
                continue
            matched.append(self._action_item(row, root_cause, "；".join(reasons), related))

        hint = ""
        if not matched:
            for row in action_rows:
                matched.append(self._action_item(
                    row, root_cause, "未在本载体的动作目录中找到与 %s 直接关联的动作，"
                    "按全量目录返回（reason 已登记）。" % root_cause, []))
            hint = ("根因 %r 未匹配到专属动作：本载体的动作目录为环境级目录，"
                    "已按全量返回，请按 reason 与排序自行取舍。" % root_cause)

        ledger.put("actions", matched)
        payload = ledger.finish(hint)
        payload["executed"] = False
        payload["dryRun"] = True
        audit_log("cc_propose", "propose(%s)" % root_cause,
                  (time.time() - started) * 1000.0, len(matched), "ok", persist=False)
        return payload

    @staticmethod
    def _action_item(row: Dict[str, Any], target: str, reason: str,
                     proposals: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        risk = str(row.get("riskLevel") or "").lower()
        approval = {
            "high": "human_approval_required",
            "medium": "operator_confirmation",
            "low": "none",
        }.get(risk, "human_approval_required")
        return {
            "actionCode": row.get("actionCode"),
            "actionName": row.get("actionName"),
            "actionRiskLevel": row.get("riskLevel"),
            "approvalRequirement": approval,
            "auditRequired": True,
            "reversible": risk not in ("high",),
            "commandTemplate": None,
            "target": target,
            "reason": reason,
            "expectedEffect": row.get("expectedEffect"),
            "sideEffectNote": row.get("sideEffect"),
            "preconditionNote": row.get("precondition"),
            "rationale": row.get("rationale"),
            "proposalCodes": sorted(str(p.get("proposalCode")) for p in proposals
                                    if p.get("proposalCode")),
            "executed": False,
            "dryRun": True,
        }

    # -- 统一分发（M3.5 返回字节记账 / M3.6 审计链与时延埋点） --------------
    def dispatch(self, tool: str, body: Any) -> Dict[str, Any]:
        handlers = {
            "cc_guide": self.build_guide,
            "cc_schema": self.build_schema,
            "cc_query": self.build_query,
            "cc_facts": self.build_facts,
            "cc_trace": self.build_trace,
            "cc_cause": self.build_cause, "cc_propose": self.build_propose,
            "read_facts": self.build_read_facts,
        }
        handler = handlers.get(tool)
        if handler is None:
            raise ToolError("INVALID_PARAM", "未识别的工具 %r。" % tool)
        # ★ 动态模式下先对齐到最新快照——否则会拿**过期命名图**回答（违反 C1，实测事故）
        self.refresh_snapshot_if_dynamic()
        # M3.6：请求 id / 发起时刻 / 入参摘要 —— 放进请求上下文，链上每条 audit 记录都带同一份。
        request_id = new_request_id()
        started_at = utc_now_iso()
        digest = params_digest(body)
        summary = params_summary(body)
        try:
            params = validate_params(tool, body)
        except ToolError as exc:
            audit_log(tool, summary, 0.0, 0, exc.code, request_id=request_id,
                      started_at=started_at, params_digest=digest, params_summary=summary,
                      result_bytes=0)
            LATENCY.observe(tool, 0.0, exc.code)
            raise
        token = _REQUEST_CTX.set({"requestId": request_id, "startedAt": started_at,
                                  "paramsDigest": digest, "paramsSummary": summary})
        started = time.time()
        outcome = "ok"
        payload: Optional[Dict[str, Any]] = None
        try:
            try:
                payload = handler(params)
                # M3.5 出口复核（仍在请求上下文内 ⇒ 记账能进审计链）；read_facts 上游已封顶并自带信封 ⇒ 免二次截断
                # ★ P1：元数据端点用独立上限（见 byte_cap_for 的说明）
                payload = (payload if tool == "read_facts"
                           else enforce_payload_budget(payload, byte_cap=byte_cap_for(tool)))
            except ToolError as exc:
                outcome = exc.code
                _inner = _REQUEST_CTX.get() or {}
                audit_log(tool, _inner.get("innerQueryText") or "",
                          (time.time() - started) * 1000.0, 0, exc.code,
                          request_id=request_id, started_at=started_at,
                          params_digest=digest, params_summary=summary, result_bytes=0)
                raise
            except GraphError as exc:
                # 兜底（R-27 一次性落盘）：工具内部的 GraphError 若未被转成 ToolError，
                # 仍必须留下这一条最终记录（状态串与内层视图逐字一致 GRAPH_<KIND>）；
                # 随后原样抛出 ⇒ wire / HTTP 行为不变。
                outcome = "GRAPH_" + exc.kind.upper()
                _inner = _REQUEST_CTX.get() or {}
                audit_log(tool, _inner.get("innerQueryText") or "",
                          (time.time() - started) * 1000.0, 0, outcome,
                          request_id=request_id, started_at=started_at,
                          params_digest=digest, params_summary=summary, result_bytes=0)
                raise
        finally:
            ctx = dict(_REQUEST_CTX.get() or {})
            _REQUEST_CTX.reset(token)
            elapsed_ms = (time.time() - started) * 1000.0
            # ★ 埋点只吃这里的墙钟样本；不读返回面的任何 elapsed 字段（与 elapsed_s 分离）。
            LATENCY.observe(tool, elapsed_ms, outcome)
        result_bytes = _byte_len(payload)
        latency = LATENCY.tool_latency(tool)
        write_metrics_file()
        audit_log(tool, ctx.get("innerQueryText") or "", elapsed_ms,
                  int(payload.get("rowCount") or ctx.get("innerRowCount") or 0), "ok",
                  request_id=request_id, started_at=started_at, params_digest=digest,
                  params_summary=summary, result_bytes=result_bytes,
                  latency_p50_ms=latency["p50_ms"], latency_p95_ms=latency["p95_ms"],
                  latency_p50_s=latency["p50_s"], latency_p95_s=latency["p95_s"],
                  latency_sample_count=latency["sampleCount"],
                  truncated=bool(payload.get("truncated")),
                  dropped_rows=int(payload.get("droppedRows") or 0),
                  dropped_bytes=int(payload.get("droppedBytes") or 0),
                  truncated_fields=ctx.get("truncatedFields"),
                  original_bytes=ctx.get("originalBytes"),
                  dropped_entries=ctx.get("droppedEntries"))
        return payload


def _order_key_attrs(candidates: Iterable[str]) -> List[str]:
    priority = ("recordId", "ccfId", "metricCode", "thresholdName", "faultModeCode",
                "findingCode", "actionCode", "proposalCode", "ruleCode", "key",
                "poolId", "containerId", "instanceId")
    values = sorted(set(candidates))
    ordered: List[str] = []
    for name in priority:
        if name in values:
            ordered.append(name)
            values.remove(name)
    ordered.extend(values)
    return ordered[:8]


def clause_text(fault_code: str, role: Any, metric_code: Any, op: Any,
                kind: Any, value: Any, predicate: Any) -> str:
    if kind == "threshold_reference":
        operand = "阈值 %s" % (value or "-")
    elif kind == "entity_property":
        operand = "对象属性 %s" % (value or "-")
    elif kind == "enum_value":
        operand = "枚举 %s" % (value or "-")
    elif kind == "exclusion_clause":
        operand = "排除条款 %s" % (value or "-")
    elif kind == "metric_value":
        operand = "字面量 %s" % (value or "-")
    else:
        operand = "时间序列判定 %s" % (value or "-")
    if predicate:
        operand = "%s（%s）" % (operand, predicate)
    return "判据：%s 的 %s 条件 —— %s %s %s" % (
        fault_code, role or "-", metric_code or "-", op or "", operand)


def _condition_summary(records: Sequence[Dict[str, Any]], total_clauses: int,
                       fault_code: str) -> str:
    if not total_clauses:
        return "该故障码在图上没有判据条款"
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        buckets.setdefault(str(record.get("role") or "detect"), []).append(record)
    parts: List[str] = []
    for role in ("necessary", "sufficient", "detect", "distinguishing", "exclusion"):
        items = buckets.get(role)
        if not items:
            continue
        hit = sum(1 for item in items if item.get("outcome") is True)
        parts.append("%s %d/%d 命中%s" % (role, hit, len(items), fault_code))
    return "；".join(parts) if parts else "该故障码在图上没有判据条款"


# ---------------------------------------------------------------------------
# HTTP 服务面（FastAPI；可选依赖）
# ---------------------------------------------------------------------------
def create_app(service: CcToolsService):
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import ConfigDict, create_model

    app = FastAPI(
        title="cc-tools-onto",
        version="1.0.0",
        description=(
            "只读知识服务：指标目录 / 对象类型与关系 / 事实记录 / 拓扑链路 / "
            "候选根因 / 建议动作 / 只读查询。"
        ),
    )

    models: Dict[str, Any] = {}
    for spec in TOOLFACE:
        fields: Dict[str, Any] = {}
        for param in spec["params"]:
            annotation = {"string": str, "integer": int, "array": List[str], "object": Dict[str, Any]}[param["type"]]
            if param.get("required"):
                fields[param["name"]] = (annotation, ...)
            else:
                fields[param["name"]] = (Optional[annotation], None)
        model = create_model(
            "%sIn" % spec["name"], __config__=ConfigDict(extra="forbid"), **fields
        )
        models[spec["name"]] = model

    def _health():  # M3.9：去返回注解 ⇒ 200 声明渲染为 {}（与 KG 侧逐字同形；载荷本身不变）
        return {
            "status": "ok",
            "carrier": CARRIER,
            "arm": ARM,
            "snapshotId": service.snapshot_id,
            "backend": service.backend.base_url,
            "repository": service.backend.repository,
            "capability": "readonly",
            "queryTimeoutS": QUERY_TIMEOUT_S,
            "rowCap": ROW_CAP,
            "byteCap": BYTE_CAP,
            "metadataByteCap": METADATA_BYTE_CAP,
            "metadataTools": list(METADATA_TOOLS),
            # ★ P2c：cc_cause 单列预算（契约 bounds.cause_byte_cap）—— 可观测、不静默
            "causeByteCap": CAUSE_BYTE_CAP,
            "maxCauseDepth": MAX_CAUSE_DEPTH,
            "tools": [spec["name"] for spec in TOOLFACE],
            # R-27 新增三键（追加式 JSONL 审计链；键名与 KG 侧对齐）
            **audit_health(),
        }

    app.get("/health")(_health)

    # ------------------------------------------------------------------
    # M3.6 埋点**不新增 HTTP 路由**（★ 硬约束：工具面逐字不变）：
    #   * shared/verify/toolface_diff.py 的 canon_openapi() 比对两侧 /openapi.json 的
    #     **全部** path（不只是 7 个工具路径）⇒ 在 ONTO 侧单方面新增任何路由（如 /metrics）
    #     都会造成 KG↔ONTO 工具面的**真实 DIFF**。本服务因此不挂任何新路由。
    #   * p50/p95 的落点 = ①审计链：每条 AUDIT 记录带该工具的 query_latency_p50_ms/p95_ms
    #     （与单次 elapsedMs / elapsed_s 并列且互不派生）；②可选遥测文件：仅当
    #     CC_TOOLS_ONTO_METRICS 指定路径时才写（见 write_metrics_file）；
    #     ③同一进程内的 `--metrics` CLI。
    #   * R-27 审计链**落盘**：CC_AUDIT_LOG 指定路径时逐调用追加一行 JSON
    #     （见 persist_audit_record）；未设 ⇒ 不落盘、不报错（默认行为不变）。
    # ------------------------------------------------------------------

    @app.exception_handler(ToolError)
    async def _tool_error_handler(request: Request, exc: ToolError):  # pragma: no cover
        tool = request.url.path.strip("/") or "unknown"
        mapping = {path.strip("/"): name for name, path in TOOL_PATHS.items()}
        return JSONResponse(status_code=(502 if exc.code == ERR_UPSTREAM_UNAVAILABLE else 400), content=exc.payload(mapping.get(tool, tool)))

    @app.exception_handler(GraphError)
    async def _graph_error_handler(request: Request, exc: GraphError):  # pragma: no cover
        payload = {
            "error": {
                "code": "BACKEND_ERROR",
                "message": exc.message,
                "tool": request.url.path.strip("/"),
                "detail": {"kind": exc.kind, "status": exc.status},
            },
            "hint": "后端查询未完成；请稍后重试或改用更窄的过滤条件。",
            "truncated": False,
            "droppedRows": 0,
            "droppedBytes": 0,
        }
        return JSONResponse(status_code=502, content=payload)

    def _make_endpoint(name: str):
        spec = next(item for item in TOOLFACE if item["name"] == name)
        model = models[name]
        path = TOOL_PATHS[name]

        def endpoint(payload=None):
            body = payload.model_dump(exclude_none=True) if payload is not None else {}
            try:
                return service.dispatch(name, body)
            except ToolError as exc:
                return JSONResponse(status_code=(502 if exc.code == ERR_UPSTREAM_UNAVAILABLE else 400), content=exc.payload(name))
            except GraphError as exc:  # pragma: no cover
                return JSONResponse(status_code=502, content={
                    "error": {"code": "BACKEND_ERROR", "message": exc.message, "tool": name,
                              "detail": {"kind": exc.kind}},
                    "hint": "后端查询未完成；请稍后重试或改用更窄的过滤条件。",
                    "truncated": False, "droppedRows": 0, "droppedBytes": 0,
                })

        endpoint.__name__ = name
        # M3.9（R-20/R-30）**声明形态**与 KG 侧逐字对齐（只改声明，不改任何运行时处置）：
        #   * 带参工具：请求体注解**非 Optional** ⇒ 渲染 {"$ref": "#/components/schemas/cc_<tool>In"}
        #     缺体 / null 仍落到 payload=None ⇒ 交给 dispatch 走既有 INVALID_PARAM 400；
        #   * 无参工具：保留 Optional[model] ⇒ {"anyOf": [{$ref}, {"type": "null"}], "title": "Payload"}
        #     （两侧本已逐字相同，属反证：残差根因就是 Optional 与否）；
        #   * 7 个工具统一补 `return: dict` ⇒ {"type": "object", "additionalProperties": true}
        #     （title 由 FastAPI 生成，不手写；toolface_diff.py 已按生成器元数据归一）。
        # 手法与对侧载体一致（同在 _make_endpoint() 内注入 __annotations__）。
        # ★ L3/C2：本方案源码**不得**提及对侧方案目录；对侧具体文件与行号记录在 M3.9 报告与台账。
        if spec["params"]:
            endpoint.__annotations__ = {"payload": model, "return": dict}
        else:
            endpoint.__annotations__ = {"payload": Optional[model], "return": dict}
        return path, endpoint

    for spec in TOOLFACE:
        name = spec["name"]
        path, endpoint = _make_endpoint(name)
        app.post(path)(endpoint)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, exc: RequestValidationError):  # pragma: no cover
        """请求体契约不满足 ⇒ 与 KG 侧一致地返回 400 INVALID_PARAM（而非 FastAPI 默认 422）。"""
        mapping = {path.strip("/"): name for name, path in TOOL_PATHS.items()}
        tool = mapping.get(request.url.path.strip("/"), request.url.path.strip("/"))
        return JSONResponse(status_code=400, content={
            "error": {
                "code": "INVALID_PARAM",
                "message": "请求体不满足工具 %s 的参数契约（未识别字段或类型不符），"
                           "不得静默回落默认值。" % tool,
                "tool": tool,
                "detail": {"validationErrors": json.loads(
                    json.dumps(exc.errors(), default=str))},
            },
            "hint": GUARD_HINT,
            "truncated": False, "droppedRows": 0, "droppedBytes": 0,
        })

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        return JSONResponse(status_code=500, content={
            "error": {"code": "INTERNAL_ERROR", "message": str(exc),
                      "tool": request.url.path.strip("/")},
            "hint": "服务内部错误。", "truncated": False, "droppedRows": 0, "droppedBytes": 0,
        })

    return app


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def build_manifest(repo_root: Optional[str] = None,
                   emitted_at: Optional[str] = None) -> Dict[str, Any]:
    source_path = os.path.join(repo_root, REPO_REL_SOURCE) if repo_root else None
    if not source_path or not os.path.isfile(source_path):
        source_path = os.path.abspath(__file__)
    with open(source_path, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    tools = []
    for spec in TOOLFACE:
        entry = dict(spec)
        entry["source"] = REPO_REL_SOURCE
        entry["sourceSha256"] = digest
        tools.append(entry)
    return {
        "schema": MANIFEST_SCHEMA,
        "carrier": CARRIER,
        "arm": ARM,
        "tool": REPO_REL_SOURCE,
        "source": REPO_REL_SOURCE,
        "sourceSha256": digest,
        "sources": [{"path": REPO_REL_SOURCE, "sha256": digest}],
        "tools": tools,
        "emittedAt": emitted_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_manifest(path: str, repo_root: Optional[str] = None,
                   emitted_at: Optional[str] = None) -> Dict[str, Any]:
    manifest = build_manifest(repo_root=repo_root, emitted_at=emitted_at)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        handle.write("\n")
    return manifest


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def selftest() -> int:
    checks: List[Tuple[str, bool, str]] = []
    # ★ R-27：自检进程**不得**写入生产审计文件（部署环境可能已 export CC_AUDIT_LOG）。
    #   整轮自检期间强制关闭落盘；R-27 用例段再临时指向自建临时路径，段末关闭。
    os.environ.pop("CC_AUDIT_LOG", None)

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    # 工具面
    check("工具面恰为 8 个", len(TOOLFACE) == 8, str(len(TOOLFACE)))
    check("工具名与契约一致",
          [t["name"] for t in TOOLFACE] == ["cc_guide", "cc_schema", "cc_query", "cc_facts",
                                            "cc_trace", "cc_cause", "cc_propose", "read_facts"])
    check("参数三元组总数 16",
          sum(len(t["params"]) for t in TOOLFACE) == 16,
          str(sum(len(t["params"]) for t in TOOLFACE)))
    check("返回字段总数 56",
          sum(len(t["returns"]) for t in TOOLFACE) == 56,
          str(sum(len(t["returns"]) for t in TOOLFACE)))
    check("信封字段齐备",
          all([r["name"] for r in t["returns"]][-4:] == ["hint", "truncated", "droppedRows",
                                                         "droppedBytes"] for t in TOOLFACE))
    check("描述不含被禁载体词",
          not any(re.search(r"cypher|sparql|rdf|owl|三元组|属性图|property graph|知识图谱|本体|"
                            r"neo4j|graphdb|turtle|ttl|shacl|推理机|reasoner",
                            t["description"], re.I) for t in TOOLFACE))

    # read_facts（M5/S3a-1）：形状权威 shared/model/live-facts-tools.yaml#read_facts
    # ★ 以下断言全部落在本行之后（禁止上移：:3354 / :3355 被纯度契约按行号锁定）。
    _rf = next((t for t in TOOLFACE if t["name"] == "read_facts"), None)
    check("read_facts 已进工具面", _rf is not None)
    check("read_facts 入参三元组与形状一致",
          _rf is not None and [(p["name"], p["type"], bool(p.get("required")))
                               for p in _rf["params"]]
          == [("layers", "array", False), ("sections", "array", False), ("scope", "string", False),
              ("max_records", "integer", False), ("max_bytes", "integer", False)])
    check("read_facts 返回名与形状一致（含公共信封）",
          _rf is not None and sorted(r["name"] for r in _rf["returns"])
          == ["droppedBytes", "droppedRows", "hint", "objects", "observations", "relations",
              "source", "truncated"])
    check("read_facts 描述与形状权威逐字一致",
          _rf is not None and _rf["description"] == READ_FACTS_DESCRIPTION)
    check("read_facts 路径指向中性取数面",
          TOOL_PATHS.get("read_facts") == "/tools/read_facts")
    check("read_facts 两段超时均为显式常量",
          LIVE_FACTS_CONNECT_TIMEOUT_S == 2.0 and LIVE_FACTS_READ_TIMEOUT_S == 15.0)
    _rf_h = getattr(CcToolsService.__new__(CcToolsService), "build_read_facts")   # 复刻 dispatch 的取法（实例属性）
    check("read_facts 处理器经实例访问不绑 self 且恰接 1 个位置参数（模块函数挂类必须 staticmethod）", callable(_rf_h) and not hasattr(_rf_h, "__self__") and _rf_h.__code__.co_argcount == 1 and ERR_UPSTREAM_UNAVAILABLE == "UPSTREAM_UNAVAILABLE")

    # 参数校验
    check("未识别参数被拒", _raises(lambda: validate_params("cc_query", {"query": "ASK {}",
                                                                       "x": 1}), "INVALID_PARAM"))
    check("缺必填被拒", _raises(lambda: validate_params("cc_trace", {}), "INVALID_PARAM"))
    check("整数拒布尔", _raises(lambda: validate_params("cc_trace", {"startId": "a",
                                                                     "maxHops": True}),
                                "INVALID_PARAM"))
    check("cc_propose 拒绝执行意图",
          _raises(lambda: validate_params("cc_propose", {"rootCause": "x", "execute": True}),
                  "DRY_RUN_ONLY"))

    # 守卫：正向
    for form in ("SELECT * WHERE { ?s ?p ?o }", "ASK { ?s ?p ?o }",
                 "PREFIX cc: <%stopology#> SELECT ?s WHERE { ?s a cc:Host }" % ONTO_BASE,
                 "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
                 "DESCRIBE ?s WHERE { ?s ?p ?o }"):
        check("守卫接受：%s" % form.split()[0], _ok(lambda f=form: guard_query(f)))
    check("路径 {1,3} 接受",
          _ok(lambda: guard_query("SELECT * WHERE { ?a cc:runsIn{1,3} ?b }")))
    check("分号语义（尾部分号 / 谓词-宾语表 / 宾语表）均接受", _ok(lambda: all(guard_query(q) for q in ("SELECT * WHERE { ?s ?p ?o } ;", "SELECT ?s WHERE { ?s a cc:AppEndpoint ; cc:path '/pay' }", "SELECT ?p WHERE { ?s cc:path '/pay', '/refund' }"))))
    check("注释内关键字不误判",
          _ok(lambda: guard_query("SELECT * WHERE { ?s ?p ?o } # DELETE 只是注释")))
    check("字面量内关键字不误判",
          _ok(lambda: guard_query("SELECT * WHERE { ?s ?p ?o . FILTER(STR(?o) = 'DROP') }")))

    # 守卫：负向（变异负控：把守卫阉掉这些断言必然失败）
    negatives = [
        ("写 token INSERT", "INSERT DATA { <a> <b> <c> }", "FORBIDDEN_TOKEN"),
        ("写 token DELETE", "DELETE WHERE { ?s ?p ?o }", "FORBIDDEN_TOKEN"),
        ("写 token CLEAR", "CLEAR GRAPH <g>", "FORBIDDEN_TOKEN"),
        ("写 token DROP", "DROP GRAPH <g>", "FORBIDDEN_TOKEN"),
        ("写 token CREATE", "CREATE GRAPH <g>", "FORBIDDEN_TOKEN"),
        ("写 token LOAD", "LOAD <http://evil/x> INTO GRAPH <g>", "EXTERNAL_IO_FORBIDDEN"),
        ("外部 IO SERVICE", "SELECT * WHERE { SERVICE <http://evil/> { ?s ?p ?o } }",
         "EXTERNAL_IO_FORBIDDEN"),
        ("跨图 GRAPH", "SELECT * WHERE { GRAPH ?g { ?s ?p ?o } }", "EXTERNAL_IO_FORBIDDEN"),
        ("显式数据集 FROM", "SELECT * FROM <http://evil/> WHERE { ?s ?p ?o }",
         "EXTERNAL_IO_FORBIDDEN"),
        ("多语句（首句内含 ; ）", "SELECT ?s WHERE { ?s a cc:AppEndpoint ; cc:path '/pay' } ; SELECT ?a WHERE { ?a ?b ?c }",
         "MULTI_STATEMENT_REJECTED"),
        ("非白名单形式", "MODIFY <g> { ?s ?p ?o }", "QUERY_FORM_REJECTED"),
        ("无界属性路径", "SELECT * WHERE { ?a cc:runsIn* ?b }", "DEPTH_LIMIT_EXCEEDED"),
        ("无界属性路径 +", "SELECT * WHERE { ?a cc:runsIn+ ?b }", "DEPTH_LIMIT_EXCEEDED"),
        ("无上界 {2,}", "SELECT * WHERE { ?a cc:runsIn{2,} ?b }", "DEPTH_LIMIT_EXCEEDED"),
        ("超上限 {1,5}", "SELECT * WHERE { ?a cc:runsIn{1,5} ?b }", "DEPTH_LIMIT_EXCEEDED"),
        # ★ M3.4 补缺口：`{,n}` 是 SPARQL 合法写法（等价 `{0,n}`），旧实现既不匹配 `{n,}` 也不匹配
        #   `{n,m}` ⇒ 上界 > 4 时会被放行。以下三条锁住这个缺口（含无下界的 `{,}` 与超上限 `{,5}`）。
        ("无下界 {,}", "SELECT * WHERE { ?a cc:runsIn{,} ?b }", "DEPTH_LIMIT_EXCEEDED"),
        ("无下界超上限 {,5}", "SELECT * WHERE { ?a cc:runsIn{,5} ?b }",
         "DEPTH_LIMIT_EXCEEDED"),
        ("无下界超上限 {,9}（空格变体）", "SELECT * WHERE { ?a cc:runsIn{ , 9 } ?b }",
         "DEPTH_LIMIT_EXCEEDED"),
        # 写 token 逐字覆盖（G2 的 9 个写 token 全部要有负控，不只 INSERT/DELETE/CLEAR/DROP/CREATE）
        ("写 token WITH", "WITH <urn:g> DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }",
         "FORBIDDEN_TOKEN"),
        ("写 token MOVE", "MOVE <urn:g1> TO <urn:g2>", "FORBIDDEN_TOKEN"),
        ("写 token COPY", "COPY <urn:g1> TO <urn:g2>", "FORBIDDEN_TOKEN"),
        ("写 token ADD", "ADD <urn:g1> TO <urn:g2>", "FORBIDDEN_TOKEN"),
        # 非只读 repository 配置探测（写路径 + 显式配置图 + 联邦）
        ("配置图写入探测",
         "INSERT DATA { <http://www.openrdf.org/config/repository#Repository> "
         "<urn:p> <urn:o> }", "FORBIDDEN_TOKEN"),
        ("配置图清空探测",
         "CLEAR GRAPH <http://www.openrdf.org/config/repository#Repository>",
         "FORBIDDEN_TOKEN"),
        ("配置图跨图探测",
         "SELECT * WHERE { GRAPH <http://www.openrdf.org/config/repository#Repository> "
         "{ ?s ?p ?o } }", "EXTERNAL_IO_FORBIDDEN"),
    ]
    for name, query, expected in negatives:
        check("守卫拒绝 %s ⇒ %s" % (name, expected),
              _raises(lambda q=query: guard_query(q), expected))

    # M3.4 正向：有界的 `{,n}` 与 `{n,m}`（n,m ≤ 4）必须**不能**被误杀
    check("路径 {,4} 接受（有界无下界写法）",
          _ok(lambda: guard_query("SELECT * WHERE { ?a cc:runsIn{,4} ?b }")))
    check("路径 {0,4} 接受", _ok(lambda: guard_query("SELECT * WHERE { ?a cc:runsIn{0,4} ?b }")))
    check("路径 {4} 接受", _ok(lambda: guard_query("SELECT * WHERE { ?a cc:runsIn{4} ?b }")))

    check("SELECT * 不当成无界路径",
          _ok(lambda: guard_query("SELECT * WHERE { ?s ?p ?o }")))
    check("COUNT(*) 不当成无界路径",
          _ok(lambda: guard_query("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")))

    # 强制 LIMIT
    bounded, injected = apply_force_limit("SELECT * WHERE { ?s ?p ?o }", "SELECT")
    check("注入行上限", injected and bounded.endswith("LIMIT 201"), bounded[-20:])
    bounded, injected = apply_force_limit("SELECT * WHERE { ?s ?p ?o } LIMIT 3", "SELECT")
    check("已有 LIMIT 不覆盖", not injected)
    bounded, injected = apply_force_limit("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }", "SELECT")
    check("聚合查询不注入", not injected)

    # Ledger
    ledger = Ledger(["a", "b"])
    ledger.put("a", list(range(150)))
    ledger.put("b", list(range(150)))
    out = ledger.finish()
    check("行上限截断并计数", out["droppedRows"] == 100 and len(out["a"]) == 150
          and len(out["b"]) == 50, str(out["droppedRows"]))
    ledger = Ledger(["a"], row_cap=10 ** 6, byte_cap=400)
    ledger.put("a", [{"payload": "x" * 100} for _ in range(20)])
    out = ledger.finish()
    check("字节上限回收行", out["droppedBytes"] > 0 and out["truncated"] is True,
          str(out["droppedBytes"]))
    ledger = Ledger(["a"])
    ledger.put("a", [])
    out = ledger.finish()
    check("空结果给 hint", bool(out["hint"]))

    # ---- M3.5 截断记账（不得静默截断冒充完整；返回面字段名不增删） --------------
    ledger = Ledger(["a", "b"])
    ledger.put("a", list(range(150)))
    ledger.put("b", list(range(150)))
    out = ledger.finish()
    check("截断记账：原始条目数如实可查",
          ledger.original_rows == {"a": 150, "b": 150}
          and ledger.accounting()["returnedRows"] == {"a": 150, "b": 50},
          _dumps(ledger.accounting().get("returnedRows")))
    check("截断记账：被截断条目数与被截断列表如实可查",
          ledger.dropped_entries == 100 and ledger.truncated_fields == ["b"],
          _dumps(ledger.accounting()))
    check("截断记账：原始载荷字节 ≥ 保留字节且 > 0",
          ledger.accounting()["originalBytes"] > 0
          and ledger.accounting()["keptBytes"] > 0
          and ledger.accounting()["originalBytes"] >= ledger.accounting()["keptBytes"],
          _dumps(ledger.accounting()))
    check("截断记账：hint 明确说明本次返回不完整",
          "截断记账" in out["hint"] and "不完整" in out["hint"]
          and "原始条目数→返回条目数" in out["hint"])
    check("截断记账不新增/不删除返回面字段名",
          set(out.keys()) == {"a", "b", "hint", "truncated", "droppedRows", "droppedBytes"},
          _dumps(sorted(out.keys())))

    ledger = Ledger(["a", "b"])
    ledger.disclosures = {"b": "b 被截断时 a 内的引用可能失效（非数据缺失）"}
    ledger.put("a", [1])
    ledger.put("b", list(range(ROW_CAP + 1)))
    out = ledger.finish()
    check("披露文案随被截断列表进入 hint",
          "非数据缺失" in out["hint"] and "b" in ledger.truncated_fields, out["hint"][:200])

    ledger = Ledger(["rows"], row_cap=10 ** 6, byte_cap=800)
    ledger.put("rows", [{"i": i, "pad": "x" * 100} for i in range(30)])
    payload_big = ledger.finish()
    check("信封严格有界：含记账文案后仍 ≤ byteCap", _byte_len(payload_big) <= 800,
          str(_byte_len(payload_big)))

    # ---- M3.5 出口字节护栏（finish() **之后**追加的字段也计入上限） -----------
    big_payload = {"rows": [{"i": i, "pad": "x" * 100} for i in range(30)],
                   "hint": "信封内层", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
    big_payload["rowCount"] = len(big_payload["rows"])     # finish() 之后追加（真实代码路径）
    size_full = _byte_len(big_payload)
    capped = enforce_payload_budget(dict(big_payload), byte_cap=800)
    check("出口复核：超限载荷被回收到上限内", size_full > 800 and _byte_len(capped) <= 800,
          "%d → %d" % (size_full, _byte_len(capped)))
    check("出口复核：如实回传 truncated/droppedBytes 与不完整说明（含原始大小）",
          capped["truncated"] is True and capped["droppedBytes"] > 0
          and "不完整" in capped["hint"] and ("原始载荷 %d B" % size_full) in capped["hint"],
          capped["hint"][-160:])
    # ★ R-23 负控：字节路径移除条目后，wire 的 droppedRows **不得**被字节路径改写
    check("accounting/droppedRows-untouched-by-byte-path",
          capped["droppedRows"] == 0,
          "droppedRows=%r（字节路径移除 %d 个条目）"
          % (capped["droppedRows"], len(big_payload["rows"]) - len(capped["rows"])))
    # ★ R-23 正控：行上限驱动的 droppedRows 经出口复核后仍如实保留
    mixed = {"rows": [{"i": i, "pad": "x" * 100} for i in range(400)],
             "hint": "行上限", "truncated": True, "droppedRows": 200, "droppedBytes": 0}
    mixed_out = enforce_payload_budget(dict(mixed), byte_cap=800)
    check("accounting/droppedRows-counted-by-row-cap",
          mixed_out["droppedRows"] == 200 and mixed_out["droppedBytes"] > 0,
          "droppedRows=%r droppedBytes=%r" % (mixed_out["droppedRows"], mixed_out["droppedBytes"]))
    check("出口复核：返回面键集不增删", set(capped) == set(big_payload),
          sorted(set(capped) ^ set(big_payload)))
    inside = {"rows": [{"i": 1}], "hint": "x", "truncated": False, "droppedRows": 0,
              "droppedBytes": 0, "rowCount": 1}
    check("出口复核：未超限时一字不改",
          enforce_payload_budget(dict(inside), byte_cap=800) == inside)

    # ---- M3.6 时延埋点（query_latency_p50/p95，与 elapsed_s 分离） --------------
    check("nearest-rank p50/p95 正确",
          LatencyRegistry.percentile(list(range(1, 101)), 50.0) == 50.0
          and LatencyRegistry.percentile(list(range(1, 101)), 95.0) == 95.0)
    check("空样本 p50/p95 为 None（不以 0 冒充）",
          LatencyRegistry.percentile([], 50.0) is None)
    snap = LATENCY.snapshot()
    check("埋点暴露 query_latency_p50/p95（ms 与 s 双口径）",
          all(key in snap for key in ("query_latency_p50_ms", "query_latency_p95_ms",
                                      "query_latency_p50_s", "query_latency_p95_s")))
    check("埋点自述与 elapsed_s 分离",
          isinstance(snap.get("separation"), str) and "elapsed" in snap["separation"])

    class _StubBackend:
        base_url = "stub://"
        repository = "cc-ops"

        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        def query(self, sparql, dataset=None, accept=None, timeout=None):
            self.calls.append({"sparql": sparql, "dataset": list(dataset or []),
                               "accept": accept, "timeout": timeout})
            return {"columns": [], "rows": [], "elapsedMs": 0.0, "kind": "table"}

    class _Buf:
        def __init__(self) -> None:
            self.parts: List[str] = []

        def write(self, text: str) -> int:
            self.parts.append(text)
            return len(text)

        def flush(self) -> None:
            pass

        def value(self) -> str:
            return "".join(self.parts)

    # ---- R-27 审计链落盘（CC_AUDIT_LOG：追加式 JSONL，一次工具调用 = 一行） -----
    _audit_dir = tempfile.mkdtemp(prefix="cc-onto-audit-selftest-")
    _audit_file = os.path.join(_audit_dir, "audit.jsonl")
    try:
        # 负控①：未设 env ⇒ 不落盘、不报错、/health 的 auditFile 回 ""
        check("r27/disabled-without-env（未设 CC_AUDIT_LOG ⇒ 不落盘、不报错、auditFile=\"\"）",
              audit_path() is None and persist_audit_record({"requestId": "x"}) is None
              and audit_health()["auditFile"] == "" and not os.path.exists(_audit_file))
        # 负控②：空串与未设同义
        os.environ["CC_AUDIT_LOG"] = ""
        check("r27/empty-env-is-disabled（空串 = 不落盘）",
              audit_path() is None and persist_audit_record({"requestId": "x"}) is None)
        # 正控：既有行保留（从不 truncate）+ 逐调用 +1 行
        with open(_audit_file, "w", encoding="utf-8", newline="\n") as seed:
            seed.write('{"preExisting": true}\n')
        os.environ["CC_AUDIT_LOG"] = _audit_file
        buf = _Buf()
        _saved_stderr = sys.stderr
        sys.stderr = buf
        try:
            first = audit_log("cc_guide", "guide", 12.5, 0, "ok",
                              request_id="SELFTEST-R27-1",
                              started_at="2026-01-01T00:00:00.000Z")
            second = audit_log("cc_query", "SELECT ?s WHERE { ?s ?p ?o } LIMIT 1", 7.0, 1, "ok",
                               request_id="SELFTEST-R27-2",
                               started_at="2026-01-01T00:00:01.000Z")
        finally:
            sys.stderr = _saved_stderr
        with open(_audit_file, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        check("r27/append-not-truncate（既有行保留且逐调用 +1 行）",
              len(lines) == 3 and json.loads(lines[0]) == {"preExisting": True},
              "lines=%d" % len(lines))
        check("r27/auditFile-exposed-when-set（/health 暴露真实落盘路径）",
              audit_health()["auditFile"] == _audit_file, audit_health()["auditFile"])
        check("r27/persisted-line-equals-stderr-record（同一条 record，逐字同值）",
              [_dumps(record) for record in (first, second)] == [lines[1], lines[2]])
        _stderr_records = [json.loads(line[len("AUDIT "):])["audit"]
                           for line in buf.value().splitlines() if line.startswith("AUDIT ")]
        check("r27/same-record-on-both-faces（不是另造一份）",
              _stderr_records == [first, second], "stderr 记录 %d 条" % len(_stderr_records))
        check("r27/fields-not-shrunk（22 键清单与本条记录完全一致）",
              len(AUDIT_FIELDS) == 22 and set(first) == set(AUDIT_FIELDS)
              and len(AUDIT_FIELDS) == len(set(AUDIT_FIELDS)), "keys=%d" % len(first))
        _chain = AUDIT_CHAIN.snapshot(tail=5)
        check("r27/in-memory-chain-tail（length/total/capacity/fields/tail）",
              set(_chain) == {"length", "total", "capacity", "fields", "tail"}
              and _chain["capacity"] == 500 and _chain["fields"] == list(AUDIT_FIELDS)
              and _chain["tail"][-1]["requestId"] == "SELFTEST-R27-2")
        _health_keys = audit_health()
        check("r27/health-audit-keys（auditFile/auditChain/queryLatency 三键齐备）",
              set(_health_keys) == {"auditFile", "auditChain", "queryLatency"}
              and "query_latency_p50_ms" in _health_keys["queryLatency"])
        # 正控②：内层视图（persist=False）只写 stderr，不进尾环/不落盘 ⇒ 一次调用 = 一行；
        # 且内层掌握的语义查询文本/内层行数并入请求上下文，供最终记录取用（信息不丢）。
        _ctx_token = _REQUEST_CTX.set({"requestId": "SELFTEST-R27-3",
                                       "startedAt": "2026-01-01T00:00:02.000Z"})
        _buf2 = _Buf()
        _saved2 = sys.stderr
        sys.stderr = _buf2
        try:
            audit_log("cc_query", "SELECT ?s WHERE { ?s ?p ?o }", 5.0, 42, "ok",
                      request_id="SELFTEST-R27-3", persist=False)
            _final3 = audit_log("cc_query", "", 9.0, 0, "ok",
                                request_id="SELFTEST-R27-3", result_bytes=123)
            _ctx_seen = dict(_REQUEST_CTX.get() or {})
        finally:
            sys.stderr = _saved2
            _REQUEST_CTX.reset(_ctx_token)
        with open(_audit_file, "r", encoding="utf-8") as handle:
            _lines3 = handle.read().splitlines()
        check("r27/one-line-per-call（内层 persist=False 只留 stderr；落盘只多最终那一条）",
              len(_lines3) == 4 and json.loads(_lines3[3]) == _final3
              and _ctx_seen.get("innerQueryText") == "SELECT ?s WHERE { ?s ?p ?o }"
              and _ctx_seen.get("innerRowCount") == 42
              and len(_buf2.value().splitlines()) == 2,
              "file_lines=%d stderr_lines=%d" % (len(_lines3),
                                                 len(_buf2.value().splitlines())))
        # 负控③：写盘失败不得冒泡、不得改变工具面，但必须如实记错
        os.environ["CC_AUDIT_LOG"] = os.path.join(_audit_file, "sub", "audit.jsonl")
        _failed = persist_audit_record({"requestId": "y"})
        check("r27/write-failure-is-contained-and-recorded（不抛异常但如实记错）",
              _failed is None and bool(_AUDIT_STATE.get("error")),
              str(_AUDIT_STATE.get("error")))
        os.environ["CC_AUDIT_LOG"] = _audit_file
        _recovered = persist_audit_record({"requestId": "z"})
        check("r27/error-cleared-after-success（恢复后错误清零）",
              _recovered == _audit_file and _AUDIT_STATE.get("error") is None)
    finally:
        os.environ.pop("CC_AUDIT_LOG", None)   # 段末关闭：后续自检不写生产审计文件
        try:
            for _name in os.listdir(_audit_dir):
                os.remove(os.path.join(_audit_dir, _name))
            os.rmdir(_audit_dir)
        except Exception:  # pragma: no cover
            pass

    _e2e_dir = tempfile.mkdtemp(prefix="cc-onto-audit-e2e-")
    _e2e_file = os.path.join(_e2e_dir, "audit.jsonl")
    os.environ["CC_AUDIT_LOG"] = _e2e_file
    stub = _StubBackend()
    svc = CcToolsService(stub, ONTO_BASE + "graph/tbox", ONTO_BASE + "graph/ccf-TEST",
                         "ccf-TEST")
    svc._type_cache = {"counts": {}, "declared": [], "keyAttrs": {}, "attrRows": []}
    svc._type_cache_at = time.time()
    buf = _Buf()
    saved_stderr = sys.stderr
    try:
        sys.stderr = buf
        payload = svc.dispatch("cc_query", {"query": "SELECT ?s WHERE { ?s ?p ?o }"})
    finally:
        sys.stderr = saved_stderr
    records: List[Dict[str, Any]] = []
    for line in buf.value().splitlines():
        if not line.startswith("AUDIT "):
            continue
        try:
            records.append(json.loads(line[len("AUDIT "):])["audit"])
        except Exception:  # pragma: no cover
            pass
    chain = [r for r in records if r.get("resultBytes") is not None]
    with open(_e2e_file, "r", encoding="utf-8") as handle:
        _e2e_lines = handle.read().splitlines()
    os.environ.pop("CC_AUDIT_LOG", None)
    try:
        os.remove(_e2e_file)
        os.rmdir(_e2e_dir)
    except Exception:  # pragma: no cover
        pass
    check("r27/e2e-dispatch-one-line（真实 dispatch 后 JSONL 恰 +1 行且 == 最终记录）",
          len(_e2e_lines) == 1 and json.loads(_e2e_lines[0]) == chain[-1],
          "lines=%d" % len(_e2e_lines))
    check("审计链：请求 id / 工具名 / 入参摘要 / 发起时刻",
          bool(chain) and len(str(chain[-1].get("requestId"))) == 16
          and chain[-1].get("tool") == "cc_query"
          and len(str(chain[-1].get("paramsDigest"))) == 16
          and bool(chain[-1].get("paramsSummary"))
          and str(chain[-1].get("startedAt") or "").endswith("Z"),
          _dumps(chain[-1] if chain else records))
    check("审计链：耗时 / 返回字节数 / 是否截断",
          bool(chain) and isinstance(chain[-1].get("elapsedMs"), (int, float))
          and isinstance(chain[-1].get("resultBytes"), int)
          and chain[-1]["resultBytes"] == _byte_len(payload)
          and chain[-1].get("truncated") is False
          and isinstance(chain[-1].get("elapsed_s"), (int, float)),
          _dumps(chain[-1] if chain else records))
    check("审计链：同一次调用各条记录共用同一 requestId",
          bool(chain) and len({r.get("requestId") for r in records
                               if r.get("tool") == "cc_query"}) == 1,
          _dumps(sorted({str(r.get("requestId")) for r in records})))
    check("响应面不含任何时延埋点键（与 elapsed_s 分离）",
          not any(key in payload for key in ("query_latency_p50", "query_latency_p95",
                                             "query_latency_p50_ms", "query_latency_p95_ms",
                                             "p50", "p95", "elapsed_s", "perTool")),
          _dumps(sorted(payload.keys())))
    snap2 = LATENCY.snapshot()
    check("埋点确有样本且按工具分桶",
          snap2["perTool"].get("cc_query", {}).get("count", 0) >= 1
          and snap2["requests"]["total"] >= 1, _dumps(snap2["perTool"]))

    # ---- M3.4 边界：G5/G7/G9 的控制手段确实以参数下传后端（客户端保证） --------
    check("g5 客户端超时以 4.0s 下传后端",
          bool(stub.calls) and stub.calls[-1]["timeout"] == QUERY_TIMEOUT_S,
          _dumps(stub.calls[-1] if stub.calls else None))
    check("g7 无 LIMIT 的 SELECT 被注入行上限",
          bool(stub.calls) and ("LIMIT %d" % (ROW_CAP + 1)) in stub.calls[-1]["sparql"])
    check("g9 数据集作用域下传（tbox+snapshot 两图）",
          bool(stub.calls) and stub.calls[-1]["dataset"]
          == [ONTO_BASE + "graph/tbox", ONTO_BASE + "graph/ccf-TEST"],
          _dumps(stub.calls[-1]["dataset"] if stub.calls else None))

    # 纯函数
    check("不可判定绝不用 False 冒充",
          evaluate_clause({"metricCode": "m", "op": ">", "operandKind": "metric_value",
                           "operandValue": 5}, {}, {}, {}, None)["outcome"] is None)
    check("趋势判定需 ≥2 点",
          evaluate_clause({"metricCode": "m", "operandKind": "metric_value",
                           "predicate": "increased"},
                          {}, {}, {("o", "m"): [("t1", "1")]}, "o")["outcome"] is None)
    check("未标定阈值不可判定",
          evaluate_clause({"metricCode": "m", "op": ">", "operandKind": "threshold_reference",
                           "operandValue": "threshold:nope#critical"},
                          {}, {"nope": {}}, {}, None)["outcome"] is None)
    check("阈值判定成立",
          evaluate_clause({"metricCode": "m", "op": ">", "operandKind": "threshold_reference",
                           "operandValue": "th#critical"},
                          {"x": 1}, {"th": {"critical": 0.5}},
                          {("o", "m"): [("t1", "0.9")]}, "o")["outcome"] is True)
    ranked = rank_candidates([
        {"faultCode": "B", "determined": True, "satisfiedClauses": 1, "hops": 2,
         "viaRelation": "causes"},
        {"faultCode": "A", "determined": True, "satisfiedClauses": 2, "hops": 3,
         "viaRelation": "conditional_causes"},
    ])
    check("排序按命中数降序", [item["faultCode"] for item in ranked] == ["A", "B"]
          and ranked[0]["rank"] == 1)
    # ★ 2026-10-14 修正断言：排序键已按契约 `rankBasis` **①「命中的判据条款数降序」**
    #   的字面口径对齐 —— `determined` 只在**同命中数之内**决定次序。
    #   改前实现把 `determined` 置于首位，使「determined=True 但 satisfiedClauses=0」
    #   （全部条款判为 FALSE ⇒ **零正向证据**）的候选举压「satisfiedClauses>0」
    #   （**有正向证据**）的候选 ⇒ 实测 top1 退化为常量（KG 侧 11/12 例同为同一码）。
    #   契约依据：rankBasis「① 命中的判据条款数降序；② 同级内：有判定结果的排在不可判定之前」。
    ranked = rank_candidates([
        {"faultCode": "A", "determined": False, "satisfiedClauses": 9, "hops": 1,
         "viaRelation": "causes"},
        {"faultCode": "B", "determined": True, "satisfiedClauses": 0, "hops": 9,
         "viaRelation": "causes"},
    ])
    check("命中数优先于判定态（契约 ① 字面口径）",
          [item["faultCode"] for item in ranked] == ["A", "B"])
    ranked = rank_candidates([
        {"faultCode": "C", "determined": False, "satisfiedClauses": 2, "hops": 1,
         "viaRelation": "causes"},
        {"faultCode": "D", "determined": True, "satisfiedClauses": 2, "hops": 1,
         "viaRelation": "causes"},
    ])
    check("同级内：有判定结果的排在不可判定之前",
          [item["faultCode"] for item in ranked] == ["D", "C"])

    # manifest
    manifest = build_manifest(emitted_at="2026-01-01T00:00:00Z")
    check("manifest 必需键", all(key in manifest for key in ("schema", "carrier", "arm",
                                                             "tool", "source", "sourceSha256",
                                                             "sources", "tools", "emittedAt")))
    check("manifest 声明载体槽位", manifest["carrier"] == "onto" and manifest["arm"] == "ONTO-Q")
    check("manifest 每个工具带来源",
          all(t["source"] == REPO_REL_SOURCE and len(t["sourceSha256"]) == 64
              for t in manifest["tools"]))
    check("manifest 可逐字复现",
          _dumps(build_manifest(emitted_at="2026-01-01T00:00:00Z")) == _dumps(manifest))

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        if not ok:
            sys.stdout.write("FAIL %s %s\n" % (name, detail))
    sys.stdout.write("测试：%d/%d PASS\n" % (passed, len(checks)))
    return 0 if passed == len(checks) else 1


def _raises(callable_obj, code: str) -> bool:
    try:
        callable_obj()
    except ToolError as exc:
        return exc.code == code
    except Exception:
        return False
    return False


def _ok(callable_obj) -> bool:
    try:
        callable_obj()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_backend_from_env() -> GraphDBBackend:
    user = os.environ.get("CC_GRAPHDB_USER")
    password = os.environ.get("CC_GRAPHDB_PASSWORD")
    return GraphDBBackend(
        base_url=os.environ.get("CC_GRAPHDB_URL", DEFAULT_GRAPHDB_URL),
        repository=os.environ.get("CC_GRAPHDB_REPOSITORY", DEFAULT_REPOSITORY),
        timeout=float(os.environ.get("CC_GRAPHDB_INTERNAL_TIMEOUT", INTERNAL_TIMEOUT_S)),
        user=user,
        password=password,
    )


def build_service() -> CcToolsService:
    backend = build_backend_from_env()
    tbox_graph = os.environ.get("CC_GRAPHDB_TBOX_GRAPH", ONTO_BASE + "graph/tbox")
    snapshot_id = os.environ.get("CC_GRAPHDB_SNAPSHOT_ID")
    snapshot_graph = (ONTO_BASE + "graph/" + snapshot_id) if snapshot_id else ""
    service = CcToolsService(backend, tbox_graph, snapshot_graph, snapshot_id or "")
    if not snapshot_id:
        service.snapshot_id = service.resolve_snapshot()
        service.snapshot_graph = ONTO_BASE + "graph/" + service.snapshot_id
        service.scope = [service.tbox_graph, service.snapshot_graph]
    else:
        service.scope = [service.tbox_graph, service.snapshot_graph]
    return service


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 GBK，中文/箭头字符会导致 UnicodeEncodeError；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ===========================================================================
# M5 / S3a-1：read_facts —— 中性取数面（:8304）的**纯代理**
# ===========================================================================
# 定义唯一源：shared/model/live-facts-tools.yaml#read_facts（形状权威，只读引用；
# 由 shared/model/tools-setb.yaml 的 neutral_tool_ref 登记，见设计裁定 D47）；
# 实现唯一源：shared/collect/live_facts_service.py（HTTP 服务，默认 :8304）。
# 本载体自包含：只把 5 个入参**原样**转给上游，并把上游 JSON **原样**透传
# （上游已按 200 行 / 20000 B 封顶并自带公共信封字段）⇒ 本侧不二次截断、不二次改名、
# 不二次包信封；只读，不开任何写路径。
# ★ 位置纪律：本块刻意整段落在**第 3356 行之后**，以免平移
#   shared/model/purity-contract.yaml:178-181 按行号锁定的本文件 :3354 / :3355。
LIVE_FACTS_PATH = "/tools/read_facts"
LIVE_FACTS_URL_DEFAULT = "http://127.0.0.1:8304" + LIVE_FACTS_PATH
LIVE_FACTS_URL_ENV = "CC_LIVE_FACTS_URL"          # 可注入基址（离线自证 / 真机换址）
# ★ 快速失败：M5 窗口里 :8304 会按用例起停，代理**绝不能**挂住整个 agent 轮次。
LIVE_FACTS_CONNECT_TIMEOUT_S = 2.0                # 连接超时（秒）
LIVE_FACTS_READ_TIMEOUT_S = 15.0                  # 读超时（秒）
ERR_UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 形状 error_codes: 502

# 描述逐字取自 live-facts-tools.yaml#read_facts.description（只去掉 YAML 块标量的尾换行）。
# ★ 与本仓库 KG 侧载体**必须逐字相同**：G24-05 会机械比对两侧 description。
READ_FACTS_DESCRIPTION = "只读取数：按本进程固定的用例确定性重建（判决模式），或在未固定用例时向采集面真采一次（运行模式），返回有界的当前读数三段与公共信封。无结果时返回 hint 说明可能原因；返回 0 行或 hint 指出作用域不符时，不得据此断言对象或读数不存在。"

READ_FACTS_PARAMS: List[Dict[str, Any]] = [
    {"name": "layers", "type": "array", "required": False},
    {"name": "sections", "type": "array", "required": False},
    {"name": "scope", "type": "string", "required": False},
    {"name": "max_records", "type": "integer", "required": False},
    {"name": "max_bytes", "type": "integer", "required": False},
]

# 返回名 = 形状 returns.tool_level 的键名 ∪ 公共信封四字段（信封沿用本载体既有表）。
READ_FACTS_RETURNS: List[Dict[str, Any]] = [
    {"name": "objects", "type": "array"},
    {"name": "relations", "type": "array"},
    {"name": "observations", "type": "array"},
    {"name": "source", "type": "object"},
    *[{"name": n, "type": t} for n, t in ENVELOPE_FIELDS],
]

READ_FACTS_ENTRY: Dict[str, Any] = {
    "name": "read_facts",
    "description": READ_FACTS_DESCRIPTION,
    "params": READ_FACTS_PARAMS,
    "returns": READ_FACTS_RETURNS,
}

# 工具面本载体是「列表 + 独立 path 表」两处登记，缺一处都会让 create_app 注册失败。
TOOLFACE.append(READ_FACTS_ENTRY)
TOOL_PATHS["read_facts"] = LIVE_FACTS_PATH


def live_facts_endpoint() -> str:
    """中性取数服务的 read_facts 端点；基址可由 CC_LIVE_FACTS_URL 注入。"""
    base = os.environ.get(LIVE_FACTS_URL_ENV, "").strip()
    if not base:
        return LIVE_FACTS_URL_DEFAULT
    if base.endswith(LIVE_FACTS_PATH):
        return base
    return base.rstrip("/") + LIVE_FACTS_PATH


def build_read_facts(params: Dict[str, Any]) -> Dict[str, Any]:
    """纯代理：5 个入参原样 POST 到中性取数面，上游 JSON 原样透传。

    两段超时均为显式常量（connect 2.0s / read 15.0s）且快速失败。上游不可达 / 超时 /
    非 200 / 非法 JSON 一律转成结构化错误 UPSTREAM_UNAVAILABLE，
    **绝不**静默返回空成功。
    """
    url = live_facts_endpoint()
    scope = params.get("scope")
    # ★ R-61（2026-10-16）：本层原先**硬拒非 dict**，而 `read_facts.scope` 自 R-54 起
    #   在模型面声明为 `string`（`shared/model/live-facts-tools.yaml#read_facts.params.scope`：
    #   「传**一个 JSON 对象字符串**」，注册面只把 name/type/required/description 给模型）
    #   ⇒ 模型按契约发的字符串**必然**被本层拒掉（真机实测：官方跑 287 次
    #   `参数 scope 必须是 JSON 对象。`，KG 侧 0 次 —— 同一契约、两载体行为分歧）。
    #   知识图谱侧（同名函数 `build_read_facts`）**原样透传**，
    #   由形状权威实现面 `shared/collect/live_facts_service.py` 解析（string → json.loads
    #   → SCOPE_KEYS 校验；非法 JSON / 顶层非对象 / 未知键 / 值类型不符一律 INVALID_PARAM）。
    #   ⇒ 本层只守「string 或 dict」，其余交实现面 —— 不在载体重复实现第二套判据。
    if scope is not None and not isinstance(scope, (dict, str)):
        raise ToolError("INVALID_PARAM", "参数 scope 必须是 JSON 对象字符串。", param="scope")
    body = {name: value for name, value in params.items() if value is not None}
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Accept": "application/json"},
            timeout=(LIVE_FACTS_CONNECT_TIMEOUT_S, LIVE_FACTS_READ_TIMEOUT_S),
        )
    except requests.exceptions.Timeout:
        raise ToolError(
            ERR_UPSTREAM_UNAVAILABLE,
            "中性取数服务超过 %.1f 秒未响应，已快速失败。" % LIVE_FACTS_READ_TIMEOUT_S,
            upstream=url,
            connectTimeoutSeconds=LIVE_FACTS_CONNECT_TIMEOUT_S,
            readTimeoutSeconds=LIVE_FACTS_READ_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        raise ToolError(ERR_UPSTREAM_UNAVAILABLE, "无法访问中性取数服务：%s" % exc, upstream=url)
    if resp.status_code != 200:
        # ★ R-56/B：上游业务码原样透传（未登记的码仍折成 UPSTREAM_UNAVAILABLE）
        _uc, _um = upstream_error_of(resp.content, resp.status_code)
        raise ToolError(
            _uc, _um, upstream=url, upstreamStatus=resp.status_code,
            upstreamBody=resp.text[:200],
        )
    try:
        out = resp.json()
    except ValueError:
        raise ToolError(ERR_UPSTREAM_UNAVAILABLE, "中性取数服务返回的不是 JSON。", upstream=url)
    if not isinstance(out, dict):
        raise ToolError(ERR_UPSTREAM_UNAVAILABLE, "中性取数服务返回的 JSON 不是对象。", upstream=url)
    return out


CcToolsService.build_read_facts = staticmethod(build_read_facts)   # ★ 必须是 staticmethod：本函数是**模块级**函数（首形参是 params，不是 self），直接挂类会被当成实例方法 ⇒ dispatch 的 `handler(params)` 实际收到 (self, params) 两个位置实参 ⇒ TypeError（S3a-2a 真机穿透冒烟抓获，离线门禁漏）。


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="方案2（ONTO 侧）Set B 七工具只读知识服务")
    parser.add_argument("--serve", action="store_true", help="启动 HTTP 服务（默认动作）")
    parser.add_argument("--host", default=os.environ.get("CC_TOOLS_ONTO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("CC_TOOLS_ONTO_PORT", DEFAULT_PORT)))
    parser.add_argument("--selftest", action="store_true", help="跑内置自检并退出")
    parser.add_argument("--emit-manifest", metavar="OUT", help="写出工具面 manifest 后退出")
    parser.add_argument("--repo-root", default=None, help="manifest/来源哈希的仓库根")
    parser.add_argument("--emitted-at", default=os.environ.get("CC_MANIFEST_EMITTED_AT"),
                        help="固定 emittedAt 以便逐字复现")
    parser.add_argument("--call", metavar="TOOL", help="离线调用单个工具（需 --body）")
    parser.add_argument("--body", default="{}", help="--call 的 JSON 请求体")
    parser.add_argument("--check-health", action="store_true", help="打印 /health 内容后退出")
    parser.add_argument("--metrics", action="store_true",
                        help="打印 M3.6 时延埋点（query_latency_p50/p95）后退出；"
                             "不发查询、不写任何文件")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.metrics:
        # 取数优先级：遥测文件（正在服务的那个进程写的）> 本进程内的注册表。
        path = metrics_path()
        snapshot: Optional[Dict[str, Any]] = None
        source = "process"
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    snapshot = loaded
                    source = "file"
            except Exception:
                snapshot = None
        if snapshot is None:
            snapshot = metrics_snapshot()
        snapshot["metricsSource"] = source
        snapshot["metricsFile"] = path
        snapshot["metricsFileExists"] = bool(path and os.path.isfile(path))
        snapshot["metricsWriteError"] = _METRICS_STATE.get("error")
        # R-27：审计落盘状态（CLI 专属；/health 只暴露 auditFile/auditChain/queryLatency）
        snapshot["auditFile"] = audit_path()
        snapshot["auditLines"] = int(_AUDIT_STATE["lines"])
        snapshot["auditWriteError"] = _AUDIT_STATE.get("error")
        snapshot["auditLastLineAt"] = _AUDIT_STATE.get("lastLineAt")
        sys.stdout.write(_dumps(snapshot) + "\n")
        return 0

    if args.emit_manifest:
        manifest = write_manifest(args.emit_manifest, repo_root=args.repo_root,
                                  emitted_at=args.emitted_at)
        sys.stdout.write("MANIFEST %s carrier=%s arm=%s tools=%d sourceSha256=%s\n"
                         % (args.emit_manifest, manifest["carrier"], manifest["arm"],
                            len(manifest["tools"]), manifest["sourceSha256"]))
        return 0

    service = build_service()

    if args.check_health:
        sys.stdout.write(_dumps({
            "status": "ok",
            "carrier": CARRIER,
            "arm": ARM,
            "snapshotId": service.snapshot_id,
            "backend": service.backend.base_url,
            "repository": service.backend.repository,
            "queryTimeoutS": QUERY_TIMEOUT_S,
            "rowCap": ROW_CAP,
            "byteCap": BYTE_CAP,
            "metadataByteCap": METADATA_BYTE_CAP,
            "metadataTools": list(METADATA_TOOLS),
            # ★ P2c：cc_cause 单列预算（契约 bounds.cause_byte_cap）—— 可观测、不静默
            "causeByteCap": CAUSE_BYTE_CAP,
            "maxCauseDepth": MAX_CAUSE_DEPTH,
            "tools": [spec["name"] for spec in TOOLFACE],
            # R-27 新增三键，与 _health() 两处必须一致
            **audit_health(),
        }) + "\n")
        return 0

    if args.call:
        body = json.loads(args.body)
        try:
            payload = service.dispatch(args.call, body)
        except ToolError as exc:
            sys.stdout.write(_dumps(exc.payload(args.call)) + "\n")
            return 2
        sys.stdout.write(_dumps(payload) + "\n")
        return 0

    import uvicorn  # 局部导入：只有 --serve 需要

    app = create_app(service)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


# ===========================================================================
# ★ R-56/B（2026-10-12）：read_facts 的**上游错误面透传**
#
# 背景（真机证据 = Dify `message_agent_thoughts`）：`read_facts` 是纯代理，但原实现对上游
# **任何**非 200 都报 `UPSTREAM_UNAVAILABLE`（"中性取数服务返回 HTTP 409"）。模型把
# `layers` 传成 `["app"]` 时上游（:8304）是**正确 fail-closed**（覆盖门禁 G5/X8），
# 模型收到的却是「上游不可用」⇒ 判定「采集接口不可用」并放弃取数。
#
# 本块做两件事（**不改任何成功路径、不改任何 HTTP 状态码**）：
#   ① 上游**业务码原样透传**（码集与另一侧载体（方案1）必须逐字相同，否则四组工具面不一致）；
#   ② 错误信封的 `hint` 与码相符（原先对**所有**码都写「被只读守卫拒绝」，
#      对上游故障/上游 fail-closed 是不实的，会把调用方引向错误的修复方向）。
#   ★ 本载体**不为任何工具登记 errorCodes**（其 manifest 一贯不含该键）⇒ 本批**不新增**该键，
#     以免改变 manifest 形状；码集的登记面在形状权威 `live-facts-tools.yaml#read_facts.error_codes`。
#
# ★ 位置纪律：本块**刻意插在 `if __name__ == "__main__"` 之前**（紧贴文件末尾），
#   以免平移既有的行号锚点（:74/:539/:547/:991/:1364/:3054/:3244/:3540 全部不动）。
# ===========================================================================
UPSTREAM_DOWN_HINT = (
    "中性取数服务（:8304）不可达 / 超时 / 返回非法：这是**本载体之外**的上游故障，"
    "不是你入参的问题；可稍后重试，或改用其他工具取证。")
UPSTREAM_REFUSED_HINT = (
    "中性取数服务**收下了请求但 fail-closed 拒绝执行**（原因见 error.message 与 "
    "error.detail.upstreamBody）—— 这**不是**「取数服务不可用」。要收窄返回范围请用 scope 参数；"
    "运行模式下 layers 必须至少含 gaussdb 与 os。")
UPSTREAM_PARAM_HINT = (
    "中性取数服务**拒绝了入参**（原因见 error.message 与 error.detail.upstreamBody）；"
    "请据其修正后重试（要收窄返回范围请用 scope 参数，不要用 layers）。")

#: 上游可透传的业务码（= `live-facts-tools.yaml#read_facts.error_codes` 的键，逐字同源）。
READ_FACTS_UPSTREAM_CODES = (
    "BAD_JSON", "BAD_PARAM", "READ_ONLY_REFUSED", "UNKNOWN_PARAM",
    "PAYLOAD_TOO_LARGE", "METHOD_NOT_ALLOWED", "NOT_FOUND", "NETWORK_REFUSED",
    "BLOCKED_BASE", "BLOCKED_NO_INPUT_FACTS", "BLOCKED_UNKNOWN_CASE",
    "BLOCKED_COVERAGE", "BLOCKED_INJECTION",
)
#: 「上游收下了请求但拒绝执行」的码族（BLOCKED_*）—— hint 要与「上游不可达」区分开。
READ_FACTS_REFUSAL_CODES = frozenset((
    "BLOCKED_BASE", "BLOCKED_NO_INPUT_FACTS", "BLOCKED_UNKNOWN_CASE",
    "BLOCKED_COVERAGE", "BLOCKED_INJECTION",
))


def error_hint(code):
    """错误信封的 hint：**必须与码相符**（R-56/B）。未知码 ⇒ 只读守卫文案（原行为）。"""
    if code in READ_FACTS_REFUSAL_CODES:
        return UPSTREAM_REFUSED_HINT
    if code in READ_FACTS_UPSTREAM_CODES:
        return UPSTREAM_PARAM_HINT
    if code == ERR_UPSTREAM_UNAVAILABLE:
        return UPSTREAM_DOWN_HINT
    return GUARD_HINT


def upstream_error_of(raw, status):
    """从上游响应体取 `(code, message)`；取不到 / 码未登记 ⇒ `UPSTREAM_UNAVAILABLE`。

    ★ 只透传**已登记**的码：未登记的码仍折成 `UPSTREAM_UNAVAILABLE`（fail-closed，
      不让未经审阅的码进入本载体的错误空间）。
    """
    code = message = None
    try:
        parsed = json.loads(raw.decode("utf-8"))
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            code, message = err.get("code"), err.get("message")
    except Exception:                                 # noqa: BLE001 —— 非 JSON 一律按传输故障处理
        code = message = None
    if isinstance(code, str) and code in READ_FACTS_UPSTREAM_CODES:
        return code, ("中性取数服务返回 HTTP %d（%s）：%s"
                      % (status, code, str(message or "")[:300]))
    return ERR_UPSTREAM_UNAVAILABLE, "中性取数服务返回 HTTP %d。" % status


if __name__ == "__main__":
    sys.exit(main())
