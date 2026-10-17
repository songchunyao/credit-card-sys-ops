# -*- coding: utf-8 -*-
"""M3.2 —— 方案1（知识图谱）侧 Set B 七工具知识服务：实现 + `--emit-manifest`。

设计约束（逐条来自任务契约，不得违反）：
  * 依赖面：只用 `requests` + 标准库 + `fastapi`（**不依赖 neo4j 驱动、不依赖 PyYAML**），
    因此本文件必须能跑在真机 `/opt/cc-venv`（py3.12.3，有 uvicorn/fastapi/requests）。
  * 只读：对本文件产生的每一条图查询都是 MATCH / 只读过程调用 + RETURN；绝无写图语句。
  * 口令只从环境变量（或远端 .env 解析）取，绝不出现在任何返回、日志、清单里。
  * 工具面（name/description/params/returns/error_codes）逐字取自唯一权威契约
    `shared/model/tools-setb.yaml` v1.0.1；本文件里的 `TOOLFACE` 是**实现与清单的唯一真源**，
    FastAPI 端点与 `--emit-manifest` 都由它驱动（清单因此「产自实现自身」）。

CLI：
  --serve [--host H] [--port P]        启动 HTTP 服务（默认 0.0.0.0:8301，自动 /openapi.json）
  --emit-manifest OUT.json [--repo-root R]   由实现自身产出工具面清单
  --selftest                           离线自检（守卫 + 形状），不连数据库
  --call TOOL --args JSON              直接调用某工具并打印 JSON（便于无 HTTP 的核验）

环境变量：
  CC_NEO4J_HTTP_URL  默认 http://127.0.0.1:7475
  CC_NEO4J_USER      默认 neo4j
  CC_NEO4J_PASSWORD  必填（缺失时 = 服务不可用，不会回落成匿名/默认口令）
  CC_NEO4J_DATABASE  默认 neo4j
  CC_NEO4J_ENV_FILE  可选，口令缺失时从该文件的 NEO4J_AUTH=user/pass 解析
  CC_AUDIT_LOG       可选，审计链 JSONL 落盘路径（M3.6；空/未设 = 只落 stderr）。
                     ★ R-27：主臂 :8301 由 `start_cc_tools.sh` 显式导出为
                     `$RUN_DIR/cc_tools_audit.jsonl`（追加式，重启后继续追加）。
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import itertools
import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:                                    # 3.11+ / 3.12 真机
    import requests
except Exception:                       # pragma: no cover - 只在缺依赖时报错
    requests = None                     # type: ignore[assignment]

# ===========================================================================
# 0. 载体身份与硬边界（与契约逐字对齐）
# ===========================================================================
CARRIER = "kg"
ARM = "KG-Q"
REPO_REL_SOURCE = "scheme1-kg/service/cc_tools.py"
MANIFEST_SCHEMA = "dsh-toolface-manifest/1"

ROW_CAP = 200                       # bounds.row_cap
BYTE_CAP = 20000                    # bounds.byte_cap
# ★ M3.5 显式落地契约 bounds.token_budget=20000。
# 诚实说明：契约里 byte_cap 与 token_budget 同值 20000，而**本实现按字节记账**
# （字节数可离线精确核验；token 数取决于具体 tokenizer，本服务不做也无法离线核验）。
# 因此 M3.5 的判据取「任一工具返回 ≤ 20000 B」，并在报告里如实标明口径。
TOKEN_BUDGET = 20000                # bounds.token_budget（★M3.5）
# ★ P1（2026-10-14）：**元数据端点**独立字节预算。
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
METADATA_ROW_CAP = 600           # 元数据端点上限（需容纳完整目录；留足余量）
METADATA_TOOLS = ("cc_guide", "cc_schema")
# ★ P2c（2026-10-14）：**`cc_cause` 单列预算**。
#   契约 `rankBasis` ⑥ 明写「**并列全部返回**，不合并为单一分数」—— 候选集必须完整。
#   实测（`BYTE_CAP=20000`）：`Ledger.cap_bytes()` **从列表尾部 pop**，于是
#   `FM-APP-POOL-EXHAUSTED`（原始 31,913 B）丢 9 个候选、
#   `FM-APP-ENDPOINT-SLOW`（原始 31,523 B）丢 10 个 ⇒ **Agent 拿到的候选集是残缺的**，
#   且 `droppedRows` 仍为 0（**记账看不出它**）。两侧丢弃量还不同（载荷大小不同）
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
QUERY_TIMEOUT_S = 4.0               # guards.R-TIMEOUT.captain_ruling = 4（CF-1）
MAX_CAUSE_DEPTH = 4                 # guards.R-DEPTH-CAP（沿 CAUSES ≤ 4）
MAX_TRACE_HOPS = 9                  # 契约 cc_trace.maxHops ≤ 9
DEFAULT_TRACE_HOPS = 2              # OQ-5：契约不冻结默认值，载体侧自定
DEFAULT_DIRECTION = "both"          # OQ-1：取值域未冻结，载体侧自定 out|in|both

KG_LABEL_PREFIX = "CC_"
# 契约 return_envelope.hint：「无结果时返回 hint 字段说明可能原因」的兜底文案
EMPTY_RESULT_HINT = (
    "本次调用返回空结果：可能是该对象/故障码/条件在当前快照中不存在，"
    "或过滤范围过窄。可先用 cc_schema / cc_guide 确认可用的对象类型与故障码范围。"
)
# 仅用于「拓扑/事实」遍历：证据边与判据边另经 observations / rules 呈现，避免淹没行预算
NON_TOPOLOGY_RELS = ("CC_HAS_EVIDENCE", "CC_JUDGED_BY")

# 契约 error_codes.list 的 8 个码（逐字）
ERR_INVALID_PARAM = "INVALID_PARAM"
ERR_FORBIDDEN_TOKEN = "FORBIDDEN_TOKEN"
ERR_QUERY_FORM_REJECTED = "QUERY_FORM_REJECTED"
ERR_MULTI_STATEMENT_REJECTED = "MULTI_STATEMENT_REJECTED"
ERR_QUERY_TIMEOUT = "QUERY_TIMEOUT"
ERR_EXTERNAL_IO_FORBIDDEN = "EXTERNAL_IO_FORBIDDEN"
ERR_DEPTH_LIMIT_EXCEEDED = "DEPTH_LIMIT_EXCEEDED"
ERR_DRY_RUN_ONLY = "DRY_RUN_ONLY"

# ===========================================================================
# 1. 工具面（实现与清单的唯一真源）
# ===========================================================================
# return_envelope.fields —— 4 个字段，全部 c3_participation: compared
ENVELOPE_FIELDS: List[Tuple[str, str]] = [
    ("hint", "string"),
    ("truncated", "boolean"),
    ("droppedRows", "integer"),
    ("droppedBytes", "integer"),
]

# 每条 returns 项的 (name, type)；type 逐字取自契约，进入 manifest 的 types 面
_TOOLFACE: List[Dict[str, Any]] = [
    {
        "name": "cc_guide",
        "path": "/guide",
        "description": (
            "列出本环境可用的指标目录、对象类型与规则清单，用于规划后续调用（不含实例数据）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [],
        "returns": [("metrics", "array"), ("objectTypes", "array"), ("rules", "array")],
        "errorCodes": [ERR_INVALID_PARAM],
    },
    {
        "name": "cc_schema",
        "path": "/schema",
        "description": (
            "返回本环境的对象类型、关系类型与属性清单（只读，不含数据）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [],
        "returns": [("objectTypes", "array"), ("relationTypes", "array"),
                    ("attributes", "array"), ("naming_discipline", "string")],
        "errorCodes": [ERR_INVALID_PARAM],
    },
    {
        "name": "cc_query",
        "path": "/query",
        "description": (
            "提交一个只读知识查询，返回匹配的对象与属性。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [{"name": "query", "type": "string", "required": True}],
        "returns": [("rows", "array"), ("rowCount", "integer")],
        "errorCodes": [ERR_INVALID_PARAM, ERR_FORBIDDEN_TOKEN, ERR_QUERY_FORM_REJECTED,
                       ERR_MULTI_STATEMENT_REJECTED, ERR_QUERY_TIMEOUT,
                       ERR_EXTERNAL_IO_FORBIDDEN, ERR_DEPTH_LIMIT_EXCEEDED],
    },
    {
        "name": "cc_facts",
        "path": "/facts",
        "description": (
            "取指定对象类型（可限定某个对象与若干指标码）的事实记录。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "objectType", "type": "string", "required": True},
            {"name": "objectId", "type": "string", "required": False},
            {"name": "metricCodes", "type": "array", "required": False},
        ],
        "returns": [("objects", "array"), ("relations", "array"), ("observations", "array")],
        "errorCodes": [ERR_INVALID_PARAM],
    },
    {
        "name": "cc_trace",
        "path": "/trace",
        "description": (
            "沿指定起始对象返回有界跳数的拓扑链路。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "startId", "type": "string", "required": True},
            {"name": "direction", "type": "string", "required": False},
            {"name": "maxHops", "type": "integer", "required": False},
        ],
        "returns": [("nodes", "array"), ("edges", "array"), ("hopsUsed", "integer")],
        "errorCodes": [ERR_INVALID_PARAM],
    },
    {
        "name": "cc_cause",
        "path": "/cause",
        "description": (
            "根据症状码返回候选根因及其路径，并给出排序依据（确定性口径，不给概率）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "symptomCodes", "type": "array", "required": True},
            {"name": "maxHops", "type": "integer", "required": False},
        ],
        "returns": [("candidates", "array"), ("rankBasis", "string")],
        "errorCodes": [ERR_INVALID_PARAM, ERR_DEPTH_LIMIT_EXCEEDED],
    },
    {
        "name": "cc_propose",
        "path": "/propose",
        "description": (
            "基于根因与证据路径给出建议动作（仅建议，不执行）。"
            "最多返回 200 行，超出会被截断并回传 droppedRows。无结果时返回 hint 字段说明可能原因。"
        ),
        "params": [
            {"name": "rootCause", "type": "string", "required": True},
            {"name": "evidencePath", "type": "array", "required": False},
        ],
        "returns": [("actions", "array"), ("executed", "boolean"), ("dryRun", "boolean")],
        "errorCodes": [ERR_INVALID_PARAM, ERR_DRY_RUN_ONLY],
    },
]

TOOL_NAMES: List[str] = [t["name"] for t in _TOOLFACE]
TOOLFACE: Dict[str, Dict[str, Any]] = {t["name"]: t for t in _TOOLFACE}

# naming_discipline 的取值：只用载体中性词，说明「为什么字段名不用载体词汇」。
NAMING_DISCIPLINE = (
    "字段名一律使用载体中性词：objectTypes / relationTypes / attributes，"
    "不使用具体载体的原生词汇。理由：服务端字段名会随工具返回进入模型上下文，"
    "禁用词纪律要求模型可见文本不含载体词汇，命名纪律是纪律的一部分。"
)

# 请求体里若出现「要求执行变更」的参数名 ⇒ 显式 DRY_RUN_ONLY，绝不执行
EXECUTION_INTENT_PARAMS = ("dryRun", "dry_run", "execute", "exec", "apply", "commit", "confirm")


# ===========================================================================
# 2. 错误与信封
# ===========================================================================
class ToolError(Exception):
    """契约 error_codes 中的一条：拒绝执行并回传码与命中详情。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def payload(self, tool: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "tool": tool,
            }
        }
        if self.detail:
            body["error"]["detail"] = self.detail
        body["hint"] = error_hint(self.code)
        body["truncated"] = False
        body["droppedRows"] = 0
        body["droppedBytes"] = 0
        return body


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _byte_len(obj: Any) -> int:
    return len(_dumps(obj).encode("utf-8"))


def audit_log(tool: str, query_text: str, elapsed_ms: float, row_count: int,
              status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """guards.R-AUDIT：每次调用记录 queryText / elapsedMs / rowCount（字段名逐字）。"""
    line = {
        "audit": {
            "tool": tool,
            "queryText": query_text,
            "elapsedMs": round(float(elapsed_ms), 3),
            "rowCount": int(row_count),
            "status": status,
        }
    }
    if extra:
        line["audit"].update(extra)
    sys.stderr.write("AUDIT " + _dumps(line) + "\n")
    try:
        sys.stderr.flush()
    except Exception:
        pass


# ===========================================================================
# 2b. M3.6 埋点：载体侧延迟直方图 + 审计链
#     ★ 与 `elapsed_s` 分离的落地机制见 LatencyRecorder 的 docstring
# ===========================================================================
def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """nearest-rank 百分位（不引入 numpy；窗口内精确，样本数 = capacity）。"""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    k = int(math.ceil(float(pct) / 100.0 * len(data)))
    k = max(1, min(k, len(data)))
    return round(data[k - 1], 3)


class LatencyRecorder:
    """载体侧「纯查询延迟」直方图（毫秒）。

    ★ 与 `elapsed_s` 分离的**落地机制**（三件事同时成立，缺一不算分离）：
      1. **量不同**：本类只接收 `queryLatencyMs`（本服务到图后端的往返耗时），
         `elapsed_s` 是评测 harness 的端到端墙钟（含 LLM 随机性；docs/06 §2.2
         记于 `upstream-eval/harness.py:118-132`）。本类**不读取、不换算、不混入**它。
      2. **来源不同**：`source: "service-side"`，读数由服务进程自己产出
         （docs/05:340「知识服务侧埋点（非 Dify）」）。
      3. **快照自述**：快照里带 `separate_from: "elapsed_s"` 与 `attribution`，
         报告读者无法把两者误当同一个量。
    """

    def __init__(self, capacity: int = 4096) -> None:
        self.capacity = int(capacity)
        self._samples: List[float] = []
        self._count = 0
        self._lock = threading.Lock()

    def record(self, ms: Any) -> None:
        try:
            value = float(ms)
        except (TypeError, ValueError):
            return
        if value < 0:
            return
        with self._lock:
            self._count += 1
            self._samples.append(value)
            if len(self._samples) > self.capacity:
                del self._samples[: len(self._samples) - self.capacity]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
            count = self._count
        return {
            "query_latency_p50": percentile(samples, 50.0),
            "query_latency_p95": percentile(samples, 95.0),
            "query_latency_n": count,
            "query_latency_sample_window": len(samples),
            "unit": "ms",
            "source": "service-side",
            "metric_scope": "载体侧纯查询延迟（不含 LLM、不含编排层）",
            "separate_from": "elapsed_s",
            "separation_note": (
                "elapsed_s = 评测 harness 端到端墙钟（含 LLM 随机性，不可归因于载体）；"
                "query_latency_* = 服务端查询往返，可归因于载体。两者是**两个量**，不得相加或互相替代。"
            ),
            "attribution": "载体能力（可归因）",
        }


class AuditChain:
    """审计链内存环形缓冲（同时逐调用落 stderr，可选再落 JSONL 文件）。"""

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = int(capacity)
        self._records: "collections.deque" = collections.deque(maxlen=self.capacity)
        self._total = 0
        self._lock = threading.Lock()

    def append(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self._total += 1
            self._records.append(record)

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._records)
        return items[-int(n):] if n else []

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


# M3.6 审计链的**字段清单**（docs/07:216「审计链」的落地清单；README 与报告同源）
AUDIT_FIELDS: Tuple[str, ...] = (
    "requestId",         # ① 请求 id
    "tool",              # ② 工具名
    "paramsDigest",      # ③ 入参摘要（sha256 前 16 位；审计链不落明文入参）
    "startedAt",         # ④ 发起时刻（UTC ISO8601）
    "elapsedMs",         # ⑤ 本服务侧总耗时（分派 + 编码）
    "queryLatencyMs",    # ⑥ ★ 载体侧纯查询延迟（差分累计量），与 elapsedMs 分列
    "backendCalls",      # ⑦ 本次请求发出的图后端 statement 数（解释 ⑥ 的量纲）
    "queryText",         # ⑧ 实际发给图后端的语句（契约 R-AUDIT 要求）
    "rowCount",          # ⑨ 返回行数
    "resultBytes",       # ⑩ ★ 返回字节数（M3.5 载荷记账）
    "truncated",         # ⑪ ★ 是否截断（M3.5：不得静默截断）
    "status",            # ⑫ ok / 契约错误码 / GRAPH_<kind>
    "originalBytes",     # ⑬ ★ 出口复核前的原始载荷字节数（M3.5 统一记账口径）
    "droppedEntries",    # ⑭ ★ 因**字节上限**移除的条目数（与 droppedRows 分列，队长裁定）
)

QUERY_LATENCY = LatencyRecorder()
AUDIT_CHAIN = AuditChain()
AUDIT_FILE = os.environ.get("CC_AUDIT_LOG", "")
_AUDIT_SEQ = itertools.count(1)


def set_audit_file(path: str) -> None:
    """设置审计链 JSONL 落盘路径（空串 = 只落 stderr）。"""
    global AUDIT_FILE
    AUDIT_FILE = path or ""


def new_request_id() -> str:
    """请求 id：UTC 时刻 + 进程内单调序号（唯一、可读、可排序）。"""
    return "%s-%06d" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), next(_AUDIT_SEQ))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def params_digest(params: Any) -> str:
    """入参摘要：规范化 JSON 的 sha256 前 16 位（审计链有意不落明文入参）。"""
    try:
        blob = _dumps(params if params is not None else {})
    except Exception:
        blob = repr(params)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def backend_call_stats(backend: Any) -> Tuple[int, float]:
    """(图后端调用次数, 累计往返毫秒)。测试用的 stub 后端没有这两个属性 ⇒ (0, 0.0)。"""
    try:
        calls = int(getattr(backend, "call_count", 0) or 0)
    except Exception:
        calls = 0
    try:
        total = float(getattr(backend, "total_elapsed_ms", 0.0) or 0.0)
    except Exception:
        total = 0.0
    return calls, total


def _append_audit_file(record: Dict[str, Any]) -> None:
    """M3.6 / R-27：把**同一条**审计记录追加写一行 JSON。

    * 一行 = 一次工具调用；`open(..., "a")` 是追加语义 ⇒ 服务重启后继续追加，
      绝不清空/截断既有内容。
    * 落盘内容与内存环 `AUDIT_CHAIN` 逐字同值（同一个 record 字典写两处）。
    * 空串 = 未落盘（保持默认行为，不报错）。
    """
    if not AUDIT_FILE:
        return
    try:
        parent = os.path.dirname(AUDIT_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_dumps(record) + "\n")
    except Exception as exc:
        # 审计落盘失败既不吞掉请求、也不谎报成功：退化为仅 stderr；
        # 但**必须留痕**（合规审计链不许有静默缺口，R-27 要求 1/6）。
        try:
            sys.stderr.write("AUDIT_FILE_ERROR %s: %s\n" % (AUDIT_FILE, exc))
            sys.stderr.flush()
        except Exception:
            pass


def emit_audit(tool: str, request_id: str, started_at: str, elapsed_ms: float,
               query_latency_ms: float, backend_calls: int, query_text: str,
               row_count: int, status: str, params: Any, result_bytes: int,
               truncated: bool, original_bytes: int = 0,
               dropped_entries: int = 0) -> Dict[str, Any]:
    """M3.6 审计链的一条记录。字段与 AUDIT_FIELDS 逐字一致。"""
    record: Dict[str, Any] = {
        "requestId": request_id,
        "tool": tool,
        "paramsDigest": params_digest(params),
        "startedAt": started_at,
        "elapsedMs": round(float(elapsed_ms), 3),
        "queryLatencyMs": round(float(query_latency_ms), 3),
        "backendCalls": int(backend_calls),
        "queryText": query_text,
        "rowCount": int(row_count),
        "resultBytes": int(result_bytes),
        "truncated": bool(truncated),
        "status": status,
        "originalBytes": int(original_bytes),
        "droppedEntries": int(dropped_entries),
    }
    QUERY_LATENCY.record(query_latency_ms)
    AUDIT_CHAIN.append(record)
    try:
        sys.stderr.write("AUDIT " + _dumps({"audit": record}) + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    _append_audit_file(record)
    return record


def latency_snapshot() -> Dict[str, Any]:
    """M3.6 的 `query_latency_p50/p95`（docs/07:216）。"""
    return QUERY_LATENCY.snapshot()


class Ledger:
    """按契约 bounds 施加行上限 / 字节上限，并如实回传 droppedRows / droppedBytes。"""

    # 默认值在**调用时**解析模块常量（def 期冻结会让运行期改写 ROW_CAP/BYTE_CAP
    # 失效，也会让「上限」出现两个真源）。
    def __init__(self, order: Sequence[str], row_cap: Optional[int] = None,
                 byte_cap: Optional[int] = None) -> None:
        self.order = list(order)
        self.row_cap = int(ROW_CAP if row_cap is None else row_cap)
        self.byte_cap = int(BYTE_CAP if byte_cap is None else byte_cap)
        self.payload: Dict[str, Any] = {}
        self.dropped_rows = 0
        self.dropped_bytes = 0
        self.byte_dropped_rows = 0
        self.byte_original_bytes = 0
        self.truncated = False
        self.hint = ""

    # -- 组装 ---------------------------------------------------------------
    def put(self, name: str, value: Any) -> None:
        self.payload[name] = value

    def cap_rows(self) -> None:
        budget = self.row_cap
        for name in self.order:
            rows = self.payload.get(name)
            if not isinstance(rows, list):
                continue
            if len(rows) > budget:
                self.dropped_rows += len(rows) - budget
                self.payload[name] = rows[:budget]
                self.truncated = True
                budget = 0
            else:
                budget -= len(rows)

    def cap_bytes(self) -> None:
        envelope = {
            "hint": self.hint,
            "truncated": self.truncated,
            "droppedRows": self.dropped_rows,
            "droppedBytes": self.dropped_bytes,
        }
        probe = dict(self.payload)
        probe.update(envelope)
        full = _byte_len(probe)
        self.byte_original_bytes = full
        if full <= self.byte_cap:
            return
        self.truncated = True
        guard = 0
        while _byte_len(probe) > self.byte_cap and guard < 20000:
            guard += 1
            for name in reversed(self.order):
                rows = probe.get(name)
                if isinstance(rows, list) and rows:
                    rows.pop()
                    self.byte_dropped_rows += 1
                    break
            else:
                break
        kept = _byte_len(probe)
        self.dropped_bytes = max(0, full - kept)

    def finish(self, hint: str = "") -> Dict[str, Any]:
        base_hint = hint or ""
        self.cap_rows()
        self.hint = base_hint
        self.cap_bytes()
        parts: List[str] = []
        if base_hint:
            parts.append(base_hint)
        if not parts:
            # 契约 return_envelope.hint：无结果时必须说明可能原因（不得只回空数组）。
            data_lists = [self.payload.get(n) for n in self.order
                          if isinstance(self.payload.get(n), list)]
            if data_lists and not any(data_lists):
                parts.append(EMPTY_RESULT_HINT)
        if self.dropped_rows:
            parts.append("已达行上限 %d，另有 %d 行被丢弃（droppedRows）；请缩小查询范围。"
                         % (self.row_cap, self.dropped_rows))
        if self.dropped_bytes:
            # ★ 统一记账口径（队长裁定）：hint 必须同时说明①本次返回**不完整**、
            # ②原始载荷大小、③被截断的条目数（droppedEntries）。
            parts.append("已达字节上限 %d，原始载荷 %d 字节，移除 %d 个条目共 %d 字节"
                         "（droppedEntries=%d / droppedBytes=%d）；本次返回**不完整**，"
                         "请缩小查询范围。"
                         % (self.byte_cap, self.byte_original_bytes,
                            self.byte_dropped_rows, self.dropped_bytes,
                            self.byte_dropped_rows, self.dropped_bytes))
        out = dict(self.payload)
        out["hint"] = " ".join(p for p in parts if p)
        out["truncated"] = bool(self.truncated)
        out["droppedRows"] = int(self.dropped_rows)
        out["droppedBytes"] = int(self.dropped_bytes)
        if self.truncated:
            # M3.5 记账（`Ledger` 这一段）。出口复核 `enforce_payload_budget` 会在此基础上
            # **累加**，不会覆盖原始大小。
            out["truncation"] = {
                "truncated": True,
                "stage": "ledger",
                "unit": "bytes" if self.dropped_bytes else "rows",
                "limit": int(self.byte_cap if self.dropped_bytes else self.row_cap),
                "bytes": 0,
                "originalBytes": int(self.byte_original_bytes or _byte_len(out)),
                "droppedEntries": int(self.byte_dropped_rows),
                "droppedBytes": int(self.dropped_bytes),
                "droppedRows": int(self.dropped_rows),
                "rowCap": int(self.row_cap),
                "byteCap": int(self.byte_cap),
            }
            for _ in range(3):                     # 自指长度取不动点（2 次内收敛）
                out["truncation"]["bytes"] = _byte_len(out)
        return out


# ===========================================================================
# 2.5 M5 / S3a-1：read_facts —— 中性取数面（:8304）的**纯代理**
# ===========================================================================
# 定义唯一源：shared/model/live-facts-tools.yaml#read_facts（形状权威，只读引用；
# 由 shared/model/tools-setb.yaml 的 neutral_tool_ref 登记，见设计裁定 D47）；
# 实现唯一源：shared/collect/live_facts_service.py（HTTP 服务，默认 :8304）。
# 本载体自包含：只把 5 个入参**原样**转给上游，并把上游 JSON **原样**透传。
# ★ 位置纪律：本块刻意落在**第 452 行之后**，以免平移 shared/deploy/logrotate/README.md
#   引用的本文件行号锚点（:405-407 / :439-440 / :450-451）；本批其余改动同样只在 ≥452 落笔。
LIVE_FACTS_URL_DEFAULT = "http://127.0.0.1:8304/tools/read_facts"
# ★ 快速失败：M5 窗口里 :8304 会按用例起停，代理**绝不能**挂住整个 agent 轮次。
LIVE_FACTS_CONNECT_TIMEOUT_S = 2.0        # 连接超时（秒）
LIVE_FACTS_READ_TIMEOUT_S = 15.0          # 读超时（秒）
ERR_UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"      # 形状 error_codes: 502

# 描述逐字取自 live-facts-tools.yaml#read_facts.description（只去掉 YAML 块标量的尾换行）。
# ★ 与本仓库 ONTO 侧载体**必须逐字相同**：G24-05 会机械比对两侧 description。
READ_FACTS_DESCRIPTION = "只读取数：按本进程固定的用例确定性重建（判决模式），或在未固定用例时向采集面真采一次（运行模式），返回有界的当前读数三段与公共信封。无结果时返回 hint 说明可能原因；返回 0 行或 hint 指出作用域不符时，不得据此断言对象或读数不存在。"

# read_facts 自述面的核对基准 = 形状权威里那条 description 的逐字相等 + 一条 sha256
# 常量断言（后者可离线独立成立）。★ 本载体刻意**不**复制门禁的禁用词表：词表判据的
# 权威在门禁侧（G24-08），且那张表本身就是对侧载体栈的词汇，落进本文件即构成
# P1 依赖面命中（本轮实测 P1-undeclared-hit / P1-non-exempt-hit，见本批报告）。
READ_FACTS_DESCRIPTION_BASIS = "shared/model/live-facts-tools.yaml#read_facts.description"
READ_FACTS_DESCRIPTION_SHA256 = "ee971388239b5210dfdd29367bccc547dc14e84982d3117edfb42ec5bec50237"
# （行数与本块原 7 行一致：保持后续行号锚点不漂移）

# 5 个入参：名字/类型/required 与形状 params mapping 逐项相同（required: [] ⇒ 全 optional）
READ_FACTS_PARAMS: List[Dict[str, Any]] = [
    {"name": "layers", "type": "array", "required": False},
    {"name": "sections", "type": "array", "required": False},
    {"name": "scope", "type": "string", "required": False},
    {"name": "max_records", "type": "integer", "required": False},
    {"name": "max_bytes", "type": "integer", "required": False},
]
# 返回名 = 形状 returns.tool_level 的键名（4 个）；公共信封 4 名由 emit_manifest 追加
READ_FACTS_RETURNS: List[Tuple[str, str]] = [
    ("objects", "array"),
    ("relations", "array"),
    ("observations", "array"),
    ("source", "object"),
]
READ_FACTS_ENTRY: Dict[str, Any] = {
    "name": "read_facts",
    "path": "/tools/read_facts",
    "description": READ_FACTS_DESCRIPTION,
    "params": READ_FACTS_PARAMS,
    "returns": READ_FACTS_RETURNS,
    # errorCodes 不参与任何门禁比对；只登记**本载体自己会返回**的码（诚实面）：
    # 入参违规 INVALID_PARAM；上游不可达/超时/非 200/非法 JSON = UPSTREAM_UNAVAILABLE。
    "errorCodes": [ERR_INVALID_PARAM, ERR_UPSTREAM_UNAVAILABLE],
}
_TOOLFACE.append(READ_FACTS_ENTRY)
TOOLFACE["read_facts"] = READ_FACTS_ENTRY
TOOL_NAMES.append("read_facts")


def _live_facts_endpoint() -> Tuple[str, int, str]:
    """中性取数面的 (host, port, path)；可由 CC_LIVE_FACTS_URL 覆盖（离线自证用）。"""
    import urllib.parse

    raw = os.environ.get("CC_LIVE_FACTS_URL") or LIVE_FACTS_URL_DEFAULT
    parts = urllib.parse.urlsplit(raw)
    return (parts.hostname or "127.0.0.1", int(parts.port or 80),
            parts.path or "/tools/read_facts")


def build_read_facts(params: Dict[str, Any]) -> Dict[str, Any]:
    """把 read_facts 的入参**原样**转发到中性取数面，并把上游 JSON **原样**透传。

    ★ 纯代理：不做二次截断、不改名、不重包信封——上游已按 max_records（默认 200 行）
      与 max_bytes（默认 20000 B）截断，且已自带 hint/truncated/droppedRows/droppedBytes。
    ★ 上游不可达 / 超时 / 非 200 / 非法 JSON ⇒ ToolError(UPSTREAM_UNAVAILABLE)，
      **绝不**静默返回空成功（「取数面无结果」与「取数面故障」必须可区分）。
    """
    import http.client

    host, port, path = _live_facts_endpoint()
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "Content-Length": str(len(body)),
    }
    conn = http.client.HTTPConnection(host, port, timeout=LIVE_FACTS_CONNECT_TIMEOUT_S)
    try:
        conn.connect()                                  # 连接阶段：connect 超时
        if conn.sock is not None:
            conn.sock.settimeout(LIVE_FACTS_READ_TIMEOUT_S)     # 读写阶段：read 超时
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        status = int(resp.status)
        raw = resp.read()
    except Exception as exc:
        raise ToolError(
            ERR_UPSTREAM_UNAVAILABLE,
            "中性取数面上游不可用（%s：%s）。" % (type(exc).__name__, exc),
            upstream="%s:%d" % (host, port),
            connectTimeoutS=LIVE_FACTS_CONNECT_TIMEOUT_S,
            readTimeoutS=LIVE_FACTS_READ_TIMEOUT_S,
        ) from None
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if status != 200:
        # ★ R-56/B：上游**业务码原样透传**（未登记的码仍折成 UPSTREAM_UNAVAILABLE）
        _uc, _um = upstream_error_of(raw, status)
        raise ToolError(
            _uc, _um, upstream="%s:%d" % (host, port), httpStatus=status,
            upstreamBody=raw[:200].decode("utf-8", "replace"),
        )
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ToolError(
            ERR_UPSTREAM_UNAVAILABLE,
            "中性取数面上游返回非法 JSON（%s）。" % type(exc).__name__,
            upstream="%s:%d" % (host, port),
        ) from None
    if not isinstance(out, dict):
        raise ToolError(ERR_UPSTREAM_UNAVAILABLE,
                        "中性取数面上游返回的 JSON 不是对象。",
                        upstream="%s:%d" % (host, port))
    return out


# ===========================================================================
# 3. 参数校验（INVALID_PARAM：未识别/非法 ⇒ 显式报错，绝不回落默认值）
# ===========================================================================
def validate_params(tool: str, body: Any) -> Dict[str, Any]:
    spec = TOOLFACE.get(tool)
    if spec is None:
        raise ToolError(ERR_INVALID_PARAM, "未知工具：%r" % (tool,))
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ToolError(ERR_INVALID_PARAM, "请求体必须是 JSON 对象，收到 %s" % type(body).__name__)
    for key in body:
        if key in EXECUTION_INTENT_PARAMS and tool == "cc_propose":
            raise ToolError(
                ERR_DRY_RUN_ONLY,
                "cc_propose 恒为 dry_run=true / executed=false，不接受任何执行意图参数；"
                "本工具只产出建议，绝不执行变更。",
                rejectedParam=key, executed=False, dryRun=True,
            )
    allowed = {p["name"]: p for p in spec["params"]}
    unknown = sorted(k for k in body if k not in allowed)
    if unknown:
        raise ToolError(ERR_INVALID_PARAM,
                        "未识别的参数名 %s；本工具接受 %s（不得静默回落默认值）。"
                        % (unknown, sorted(allowed) or "无参数"),
                        unknownParams=unknown, acceptedParams=sorted(allowed))
    out: Dict[str, Any] = {}
    for name, pspec in allowed.items():
        present = name in body and body[name] is not None
        if not present:
            if pspec["required"]:
                raise ToolError(ERR_INVALID_PARAM, "缺少必填参数 %s。" % name,
                                missingParam=name)
            continue
        value = body[name]
        out[name] = _check_type(tool, name, pspec["type"], value)
    return out


def _check_type(tool: str, name: str, want: str, value: Any) -> Any:
    if want == "string":
        if not isinstance(value, str):
            raise ToolError(ERR_INVALID_PARAM,
                            "参数 %s 期望 string，收到 %s。" % (name, type(value).__name__))
        if value == "":
            raise ToolError(ERR_INVALID_PARAM, "参数 %s 不得为空字符串。" % name)
        return value
    if want == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(ERR_INVALID_PARAM,
                            "参数 %s 期望 integer，收到 %r。" % (name, value))
        return int(value)
    if want == "array":
        if not isinstance(value, list):
            raise ToolError(ERR_INVALID_PARAM,
                            "参数 %s 期望 array，收到 %s。" % (name, type(value).__name__))
        for item in value:
            if not isinstance(item, str):
                raise ToolError(ERR_INVALID_PARAM,
                                "参数 %s 的元素必须是字符串，收到 %r。" % (name, item))
        if value and all(v == "" for v in value):
            raise ToolError(ERR_INVALID_PARAM, "参数 %s 的元素不得为空白字符串。" % name)
        return list(value)
    if want == "boolean":
        if not isinstance(value, bool):
            raise ToolError(ERR_INVALID_PARAM,
                            "参数 %s 期望 boolean，收到 %r。" % (name, value))
        return bool(value)
    if want == "object":
        # ★ R-54（2026-10-07）：**本分支已无任何工具会走到** —— 声明为 object 的参数已被清除
        # （`read_facts.scope` 与 `get_facts.timeWindow` 都改成了 `string`，因为 Dify 的
        # type_alias 把 object 折成 string 给模型看，而服务侧按声明类型校验 ⇒ 声明成 object 必 400）。
        # **保留本分支**：它是通用校验器的合法类型之一（防御性），且形状权威实现面（:8304）
        # 仍接受 dict 形态的 `scope`（兼容路径）。本层只守「必须是 JSON 对象」；嵌套键的合法性
        # 由实现面判定，载体不重复实现第二套判据（避免两处判据漂移）。
        if not isinstance(value, dict):
            raise ToolError(ERR_INVALID_PARAM,
                            "参数 %s 期望 object，收到 %s。" % (name, type(value).__name__))
        return dict(value)
    raise ToolError(ERR_INVALID_PARAM, "参数 %s 的类型 %r 未知。" % (name, want))


# ===========================================================================
# 4. cc_query 只读守卫（11 个角色中落在 KG 侧的 G1..G10）
# ===========================================================================
# R-READONLY-FORM.kg.whitelist（逐字）
FORM_WHITELIST: Tuple[str, ...] = ("OPTIONAL MATCH", "MATCH", "WITH", "UNWIND", "RETURN")
# R-READONLY-TOKENS.kg.forbidden_regex（逐字取自 docs/03 §7.2），flags=ignorecase
FORBIDDEN_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL\s+apoc\.(create|merge|delete|refactor|periodic)"
    r"|LOAD\s+CSV|FOREACH)\b",
    re.I,
)
# R-NO-EXTERNAL-IO.kg.forbidden
EXTERNAL_IO_RE = re.compile(r"\bapoc\.(load|import)\b", re.I)
# R-FORCE-LIMIT：无 LIMIT 且无聚合时自动注入
AGGREGATION_RE = re.compile(
    r"\b(count|sum|avg|min|max|collect|percentileCont|percentileDisc|stDev|stDevP)\s*\(",
    re.I,
)
LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)", re.I)
# R-BOUNDED-PATH：只认「关系模式方括号里的 `*`」，避免误伤 count(*) / 乘法
BRACKET_RE = re.compile(r"\[([^\]]*)\]")
DEPTH_TOKEN_RE = re.compile(r"\*\s*(?:(\d+)\s*)?(?:\.\.\s*(\d+)?)?")

# ★ M3.4 补强（过程调用面）。契约的 `forbidden_regex` 逐字冻结、不得改写，而它只匹配
# `CALL apoc.(create|merge|delete|refactor|periodic)`；实测漏网（补强前）：
#   MATCH (n) CALL dbms.security.createUser('a','b',false) RETURN n
#   MATCH (n) CALL db.createLabel('X') RETURN n
#   MATCH (n) CALL apoc.trigger.add('t','RETURN 1',{}) RETURN n
#   MATCH (n) CALL apoc.schema.assert({},{}) RETURN n
#   MATCH (n) CALL dbms.killQuery('query-1') RETURN n
#   MATCH (n) CALL dbms.security.changePassword('x') RETURN n
# 原因两条：(a) `\bCREATE\b` 不匹配 `createUser`/`createLabel`（词后紧跟字母，无词界）；
# (b) R-READONLY-FORM 只挡「以 CALL 开头」的语句，写在 MATCH 之后的过程调用无人拦。
# 按契约自己的哲学（白名单是唯一防线）补一层：**除显式只读过程白名单外一律拒绝**。
# `CALL { ... }` 是子查询而非过程调用，不在此列（其写操作已由 FORBIDDEN_RE 覆盖）。
CALL_PROC_RE = re.compile(r"\bCALL\s+(?!\{)([A-Za-z_][A-Za-z0-9_.]*)", re.I)
# 只读 introspective 过程（全名精确匹配，大小写不敏感）。默认拒绝其它一切过程。
READONLY_PROCEDURES = frozenset(name.lower() for name in (
    "db.labels", "db.relationshipTypes", "db.propertyKeys",
    "db.indexes", "db.constraints", "db.schema.visualization",
    "db.schema.nodeTypeProperties", "db.schema.relTypeProperties",
    "dbms.components", "dbms.procedures", "dbms.functions",
))


def strip_comments(text: str) -> str:
    """剥掉 `//` 行注释与 `/* */` 块注释（字符串字面量内的不剥）。"""
    out: List[str] = []
    i, n = 0, len(text)
    quote: Optional[str] = None
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def count_semicolons(text: str) -> int:
    """统计字符串字面量之外的 `;` 个数（KG 侧 R-SINGLE-STATEMENT 判据）。"""
    count = 0
    i, n = 0, len(text)
    quote: Optional[str] = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if ch == ";":
            count += 1
        i += 1
    return count


def mask_literals(text: str) -> str:
    """把字符串字面量（'..' / ".." / `..`）的**内容**替换为空格，保留引号与全部位置。

    用途：过程调用检查必须只在「代码」上做，不能在字符串字面量里误判
    （例如 `WHERE n.note = 'CALL db.foo()'` 是数据，不是过程调用）。
    """
    out = list(text)
    i, n = 0, len(text)
    quote = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < n:
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            if ch == quote:
                quote = None
            else:
                out[i] = " "
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        i += 1
    return "".join(out)


def guard_query(raw: Any) -> str:
    """按契约违反顺序执行只读守卫；通过则返回待执行的语句文本（不含注入）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError(ERR_INVALID_PARAM, "参数 query 必须是非空字符串。", param="query")
    semantic = strip_comments(raw)
    body = semantic.strip()

    # R-SINGLE-STATEMENT（MULTI_STATEMENT_REJECTED）
    semis = count_semicolons(body)
    if semis > 1:
        raise ToolError(ERR_MULTI_STATEMENT_REJECTED,
                        "只接受单条只读查询：分号计数 %d > 1。" % semis,
                        semicolonCount=semis)
    if semis == 1 and body.rstrip()[:-1].rstrip().find(";") >= 0:
        raise ToolError(ERR_MULTI_STATEMENT_REJECTED,
                        "分号出现在语句中部 ⇒ 视为多语句请求，拒绝执行。",
                        semicolonCount=semis)
    body = body.rstrip()
    if body.endswith(";"):
        body = body[:-1].rstrip()

    # R-NO-EXTERNAL-IO（EXTERNAL_IO_FORBIDDEN）
    hit = EXTERNAL_IO_RE.search(body)
    if hit:
        raise ToolError(ERR_EXTERNAL_IO_FORBIDDEN,
                        "禁止读外部资源：命中 %r。" % hit.group(0),
                        matchedToken=hit.group(0))

    # R-READONLY-TOKENS（FORBIDDEN_TOKEN；含 CALL {} 子查询内的写操作）
    hit = FORBIDDEN_RE.search(body)
    if hit:
        raise ToolError(ERR_FORBIDDEN_TOKEN,
                        "只读查询不得含写操作 token：命中 %r。" % hit.group(0),
                        matchedToken=hit.group(0))

    # ★ M3.4 补强：过程调用白名单（默认拒绝）。放在冻结正则之后，故原命中的
    # 码与匹配串完全不变，行为只增不改。只在字面量之外检查（mask_literals）。
    masked = mask_literals(body)
    for hit in CALL_PROC_RE.finditer(masked):
        proc = hit.group(1)
        if proc.lower() not in READONLY_PROCEDURES:
            raise ToolError(ERR_FORBIDDEN_TOKEN,
                            "只读查询不得调用过程 %r：过程可能产生写入或越权副作用，"
                            "本服务只放行显式只读过程白名单。" % proc,
                            matchedToken=body[hit.start():hit.end()].strip(),
                            procedure=proc,
                            readonlyProcedureWhitelist=sorted(READONLY_PROCEDURES))

    # R-READONLY-FORM（QUERY_FORM_REJECTED）
    head = re.sub(r"\s+", " ", body.lstrip()).upper()
    if not any(head == w or head.startswith(w + " ") for w in FORM_WHITELIST):
        got = head.split(" ")[0] if head else ""
        raise ToolError(ERR_QUERY_FORM_REJECTED,
                        "语句起始形式 %r 不在白名单 %s 内。" % (got, list(FORM_WHITELIST)),
                        whitelist=list(FORM_WHITELIST), got=got)

    # R-BOUNDED-PATH + R-DEPTH-CAP（DEPTH_LIMIT_EXCEEDED）
    for bracket in BRACKET_RE.findall(body):
        for token in DEPTH_TOKEN_RE.findall(bracket):
            low, high = token
            if "*" not in bracket:
                continue
            if low == "" and high == "":
                raise ToolError(ERR_DEPTH_LIMIT_EXCEEDED,
                                "禁止无上界的路径枚举（变长路径 `*`），成环图会爆炸。",
                                pattern="[%s]" % bracket)
            if high == "":
                raise ToolError(ERR_DEPTH_LIMIT_EXCEEDED,
                                "禁止无上界的路径枚举（`*%s..`），成环图会爆炸。" % low,
                                pattern="[%s]" % bracket, lowerBound=low)
            if int(high) > MAX_CAUSE_DEPTH:
                raise ToolError(ERR_DEPTH_LIMIT_EXCEEDED,
                                "路径枚举上界 %s > 硬上限 %d（沿 CAUSES 的深度 ≤ %d）。"
                                % (high, MAX_CAUSE_DEPTH, MAX_CAUSE_DEPTH),
                                pattern="[%s]" % bracket, upperBound=int(high),
                                limit=MAX_CAUSE_DEPTH)
    return body


def apply_force_limit(query: str) -> Tuple[str, bool]:
    """R-FORCE-LIMIT：无 LIMIT 且无聚合 ⇒ 注入 `LIMIT row_cap+1`（注入而非拒绝）。"""
    if LIMIT_RE.search(query) or AGGREGATION_RE.search(query):
        return query, False
    return query + " LIMIT %d" % (ROW_CAP + 1), True


def build_count_query(query: str) -> str:
    """把原查询包成计数子查询，用于在被截断时给出**精确**的 droppedRows。"""
    return "CALL {\n%s\n}\nRETURN count(*) AS total" % query


def _list_slots(payload: Dict[str, Any]) -> List[Tuple[Dict[str, Any], str]]:
    """可削减的数组槽位：顶层数组 + `error.detail` 下的一层数组。"""
    slots: List[Tuple[Dict[str, Any], str]] = []
    for key, value in payload.items():
        if isinstance(value, list) and value:
            slots.append((payload, key))
        elif key == "error" and isinstance(value, dict):
            detail = value.get("detail")
            if isinstance(detail, dict):
                for dkey, dvalue in detail.items():
                    if isinstance(dvalue, list) and dvalue:
                        slots.append((detail, dkey))
    slots.sort(key=lambda s: -len(s[0][s[1]]))
    return slots


def enforce_payload_budget(payload: Dict[str, Any],
                           byte_cap: Optional[int] = None) -> Dict[str, Any]:
    """M3.5 端到端字节预算：对**最终**要发出的整个响应对象再施加一次上限。

    为什么 `Ledger` 之外还要再做一次：`Ledger.cap_bytes()` 度量的是「payload + 信封」
    的 probe（且此刻 hint 只有 base_hint），而 `finish()` 之后还会把 droppedRows /
    droppedBytes 的说明文字追加进 hint，`build_query()` 还会追加 rowCount —— 这些都是
    **在度量之后**发生的，实测可以超出上限（rows=200,pad=60 ⇒ 20086 B > 20000）。
    本函数度量的是真正要发出去的那个对象，并把削减**如实记账**（契约 C6：不得静默截断）。
    """
    cap = int(BYTE_CAP if byte_cap is None else byte_cap)
    if _byte_len(payload) <= cap:
        return payload

    before = _byte_len(payload)
    out = copy.deepcopy(payload)
    dropped = 0
    _touched_names: List[str] = []
    hint_trimmed = False
    envelope_only = False
    # 预留在记账字段自身的字节上，避免「记完账又超了」。
    reserve = 512
    guard = 0
    while _byte_len(out) > cap - reserve and guard < 20000:
        guard += 1
        slots = _list_slots(out)
        if slots:
            parent, key = slots[0]
            parent[key].pop()
            dropped += 1
            if key not in _touched_names:
                _touched_names.append(key)
            continue
        hint = str(out.get("hint") or "")
        if hint:
            out["hint"] = hint[: max(0, len(hint) - max(24, len(hint) // 4))]
            hint_trimmed = True
            continue
        err = out.get("error")
        if isinstance(err, dict) and str(err.get("message") or ""):
            msg = str(err["message"])
            err["message"] = msg[: max(0, len(msg) - max(24, len(msg) // 4))]
            hint_trimmed = True
            continue
        break

    final = _byte_len(out)
    truncation = dict(out.get("truncation") or {})
    _prior_entries = int(truncation.get("droppedEntries") or 0)
    _prior_original = int(truncation.get("originalBytes") or 0)
    _prior_stage = str(truncation.get("stage") or "")
    truncation.update({
        "truncated": True,
        "unit": "bytes",
        "limit": cap,
        "bytes": final,
        "bytesBeforeBudget": before,
        "originalBytes": _prior_original or before,
        "droppedItemsByBudget": dropped,
        "droppedEntries": _prior_entries + dropped,
        "hintTrimmed": hint_trimmed,
        "envelopeOnly": envelope_only,
        "stage": ("ledger+enforce_payload_budget" if _prior_stage.startswith("ledger")
                  else "enforce_payload_budget"),
    })
    out["truncation"] = truncation
    out["truncated"] = True
    # ★ 统一记账口径（队长裁定）：`droppedRows` 只表示**行数上限**丢弃的行，
    # 字节路径移除的条目一律只记 `droppedEntries`（写进审计链），**不得**混入 droppedRows；
    # `droppedBytes` 只表示因**字节上限**移除的字节数。
    out["droppedBytes"] = int(out.get("droppedBytes") or 0) + max(0, before - final)
    # 信封 hint 必须同时说明：①本次返回**不完整**；②原始载荷大小；③被截断的条目数。
    note = ("出口字节复核：原始载荷 %d B > 上限 %d B，本次再移除 %d 个条目（字段 %s）；"
            "累计 droppedEntries=%d、droppedBytes=%s；本次返回**不完整**，请缩小查询范围。"
            % (before, cap, dropped,
               "/".join(dict.fromkeys(_touched_names)) or "—",
               truncation["droppedEntries"], out["droppedBytes"]))
    out["hint"] = (str(out.get("hint") or "") + " " + note).strip()

    if _byte_len(out) > cap:                      # 兜底：连记账字段都塞不下
        keep: Dict[str, Any] = {}
        if isinstance(out.get("error"), dict):
            err = out["error"]
            keep["error"] = {"code": err.get("code"), "message": err.get("message"),
                             "tool": err.get("tool")}
        for key, value in out.items():
            if key in ("truncation", "truncated", "droppedRows", "droppedBytes", "hint"):
                continue
            if isinstance(value, list):
                continue
            if not isinstance(value, (dict, list)):
                keep[key] = value
        keep["hint"] = ("响应原始载荷 %d B 超出字节上限 %d，已按最小信封返回（M3.5 载荷有界，"
                        "droppedEntries=%d）；本次返回**不完整**，请缩小查询范围。"
                        % (before, cap, dropped))
        keep["truncated"] = True
        keep["droppedRows"] = out["droppedRows"]
        keep["droppedBytes"] = out["droppedBytes"]
        truncation["envelopeOnly"] = True
        truncation["bytes"] = _byte_len(keep)
        keep["truncation"] = truncation
        return keep
    return out


# ===========================================================================
# 5. 图后端（只用 requests + Neo4j HTTP 事务端点；只读）
# ===========================================================================
class GraphError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Neo4jHttpBackend:
    """Neo4j HTTP 事务端点（/db/<db>/tx/commit）。只发 statement，不写图。"""

    def __init__(self, base_url: str, user: str, password: str,
                 database: str = "neo4j", timeout: float = QUERY_TIMEOUT_S) -> None:
        if requests is None:
            raise GraphError("dependency", "缺少 requests 依赖，无法访问图后端。")
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.database = database
        self.timeout = float(timeout)
        self._session = requests.Session()
        self.last_query = ""
        self.last_elapsed_ms = 0.0
        # ★ M3.6：累计量，供 dispatch 按**逐请求**差分出「载体侧纯查询延迟」。
        # 单独一个工具的请求可能发多条 statement（如 cc_trace 的两跳），
        # 用 last_elapsed_ms 只会拿到最后一跳 ⇒ 必须累计再差分。
        self.call_count = 0
        self.total_elapsed_ms = 0.0

    @property
    def endpoint(self) -> str:
        return "%s/db/%s/tx/commit" % (self.base_url, self.database)

    def cypher(self, statement: str, parameters: Optional[Dict[str, Any]] = None,
               timeout: Optional[float] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"statements": [{"statement": statement}]}
        if parameters:
            payload["statements"][0]["parameters"] = parameters
        eff = float(timeout if timeout is not None else self.timeout)
        started = time.time()
        self.last_query = statement
        try:
            resp = self._session.post(
                self.endpoint,
                json=payload,
                auth=(self.user, self.password),
                timeout=eff,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:                        # 超时 / 连接失败
            elapsed = (time.time() - started) * 1000.0
            self.last_elapsed_ms = elapsed
            self.call_count += 1
            self.total_elapsed_ms += elapsed
            name = type(exc).__name__
            if "Timeout" in name or "timeout" in str(exc).lower():
                raise GraphError("timeout",
                                 "查询超时（服务端上限 %.3fs）：%s" % (eff, name))
            raise GraphError("transport", "无法访问图后端：%s: %s" % (name, exc))
        elapsed = (time.time() - started) * 1000.0
        self.last_elapsed_ms = elapsed
        self.call_count += 1
        self.total_elapsed_ms += elapsed
        try:
            body = resp.json()
        except Exception:
            raise GraphError("transport", "图后端返回非 JSON（HTTP %s）。" % resp.status_code)
        errors = body.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors, list) else {}
            code = str(first.get("code") or "")
            msg = str(first.get("message") or first)
            if "Timeout" in code or "timeout" in msg.lower():
                raise GraphError("timeout", "查询超时：%s" % msg)
            if "Syntax" in code or "Syntax" in msg or "Invalid input" in msg:
                raise GraphError("syntax", "查询语法被图后端拒绝：%s" % msg)
            raise GraphError("query", "图后端报错：%s" % msg)
        result = (body.get("results") or [{}])[0]
        cols = result.get("columns") or []
        rows = [dict(zip(cols, item.get("row") or []))
                for item in (result.get("data") or [])]
        return {"columns": cols, "rows": rows, "elapsedMs": elapsed}


# ===========================================================================
# 6. 纯函数：判据求值 + 确定性排序（§5.8 / M2 更正口径）
# ===========================================================================
RELATION_PRIORITY = {"causes": 0, "conditional_causes": 1}
UNRANKED_PRIORITY = 9
CAUSE_REL_PRIORITY = {"CC_CAUSES": "causes", "CC_CONDITIONAL_CAUSES": "conditional_causes"}


def derive_relation_priority(rel_type: str,
                             edge_props: Optional[Dict[str, Any]] = None) -> str:
    """由**关系类型 + 边上 `cond*` 属性**判定契约 relation 名。

    ★ 2026-10-14 修正（原实现只看 `type(r)`）：
      LPG 的落地约定是「`CC_CAUSES` + 有 `cond*` 属性 ⇔ `conditional_causes`」
      —— 见 docs/03 §5.3「条件因果 conditional_causes 的 LPG 落地」，
      以及 ontology/03-fault-modes.cypher 头部同一句说明。
      只读关系类型 ⇒ 4 条条件因果边（E6/E7/E9/E12）一律被报成 `causes`
      ⇒ 契约 `rankBasis` ③「`CAUSES > CONDITIONAL_CAUSES`」的**区分能力失效**，
      并与 ONTO 侧（读 `ccc:linkKind`）产生系统性的 `relationPriority` 分歧。

    保留 `CC_CONDITIONAL_CAUSES` 分支：历史投影若真的按类型分开写，也应正确识别。
    """
    name = CAUSE_REL_PRIORITY.get(str(rel_type))
    if name == "causes":
        props = edge_props or {}
        if any(props.get(k) for k in ("condMetric", "condKind", "condRef", "condOp")):
            return "conditional_causes"
    return name or "causes"
TRUTHY = ("1", "true", "t", "yes", "y", "up", "on", "ok")


def to_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return None


def truthy(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    num = to_number(value)
    if num is not None:
        return num != 0.0
    return str(value).strip().lower() in TRUTHY


def series_of(rows: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
    """{(objectId, metricCode): [(capturedAt, rawValue)…]}，按 capturedAt 升序固定。"""
    series: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for row in rows:
        key = (str(row.get("objectId") or ""), str(row.get("metricCode") or ""))
        series.setdefault(key, []).append(
            (str(row.get("capturedAt") or ""), row.get("rawValue")))
    for key in series:
        series[key].sort(key=lambda item: item[0])
    return series


def resolve_threshold(ref: Any, thresholds: Dict[str, Dict[str, Any]]) -> Tuple[
        Optional[float], str, Dict[str, Any]]:
    """解析 `threshold:NAME#level`；返回 (数值 or None, 来源说明, 详情)。"""
    text = str(ref or "")
    name, _, level = text.partition("#")
    level = level.strip() or "warning"
    if name.startswith("threshold:"):
        name = name.split(":", 1)[1]
    spec = thresholds.get(name)
    if not spec:
        return None, "CC_Threshold:%s（图上无此阈值条目）" % name, {
            "thresholdCode": name, "level": level, "calibrated": False}
    key = "thrWarning" if level == "warning" else (
        "thrCritical" if level == "critical" else level)
    value = spec.get(key)
    if value is None:
        return None, "CC_Threshold:%s.%s（未标定 ⇒ 不可判定）" % (name, key), {
            "thresholdCode": name, "level": level, "property": key, "calibrated": False}
    return to_number(value), "CC_Threshold:%s.%s" % (name, key), {
        "thresholdCode": name, "level": level, "property": key, "value": value,
        "calibrated": True}


def resolve_entity_property(ref: Any, props: Dict[str, Any]) -> Tuple[
        Optional[Any], str, Dict[str, Any]]:
    text = str(ref or "")
    owner, _, prop = text.partition(".")
    value = props.get(prop) if prop else None
    return value, "对象属性 %s（实例 %s）" % (prop, owner), {
        "entityProperty": prop, "declaredOwner": owner,
        "resolved": value is not None}


def evaluate_clause(clause: Dict[str, Any], props: Dict[str, Any],
                    thresholds: Dict[str, Dict[str, Any]],
                    series: Dict[Tuple[str, str], List[Tuple[str, str]]],
                    object_id: str) -> Dict[str, Any]:
    """判定一条判据条款；不可判定 ⇒ outcome=None（**绝不用 False 冒充**）。"""
    metric = str(clause.get("metricCode") or "")
    op = str(clause.get("op") or "")
    kind = str(clause.get("operandKind") or "")
    record: Dict[str, Any] = {
        "metricCode": metric, "op": op, "role": clause.get("role"),
        "operandKind": kind, "outcome": None, "reason": "",
        "objectId": object_id, "metadataSource": "",
    }
    points = series.get((object_id, metric), [])
    if kind == "predicate":
        low = op.lower()
        if low in ("increased", "decreased"):
            if len(points) < 2:
                record["reason"] = ("只有 %d 个时间点，判不出 %s（不得用 False 冒充）"
                                    % (len(points), op))
                return record
            first, last = to_number(points[0][1]), to_number(points[-1][1])
            if first is None or last is None:
                record["reason"] = "观测值非数值，判不出 %s" % op
                return record
            record["outcome"] = (last > first) if low == "increased" else (last < first)
            record["operand"] = None
            record["metadataSource"] = "CC_Evidence:%s,%s" % (points[0][0], points[-1][0])
            return record
        if len(points) == 0:
            record["reason"] = "图上没有 %s 的观测（判据不可判定）" % metric
            return record
        raw = points[-1][1]
        if low in ("is_true", "is_false"):
            verdict = truthy(raw)
            if verdict is None:
                record["reason"] = "观测值 %r 无法判真伪" % (raw,)
                return record
            record["outcome"] = verdict if low == "is_true" else (not verdict)
        elif low == "non_zero":
            num = to_number(raw)
            if num is None:
                record["reason"] = "观测值 %r 非数值，判不出 non_zero" % (raw,)
                return record
            record["outcome"] = num != 0.0
        else:
            record["reason"] = "不认识的谓词 %r" % op
            return record
        record["observed"] = raw
        record["metadataSource"] = "CC_Evidence:%s" % points[-1][0]
        return record

    if kind == "enum":
        values = clause.get("operandValues") or []
        if not isinstance(values, list):
            values = [values]
        if len(points) == 0:
            record["reason"] = "图上没有 %s 的观测（判据不可判定）" % metric
            return record
        raw = points[-1][1]
        record["observed"] = raw
        record["operand"] = list(values)
        record["outcome"] = str(raw) in [str(v) for v in values]
        record["metadataSource"] = "CC_Evidence:%s" % points[-1][0]
        return record

    if kind == "literal":
        operand, source, meta = to_number(clause.get("operandValue")), "字面量", {}
    elif kind == "threshold":
        operand, source, meta = resolve_threshold(clause.get("operandRef"), thresholds)
    elif kind == "entity_property":
        operand, source, meta = resolve_entity_property(clause.get("operandRef"), props)
        operand = to_number(operand)
    else:
        record["reason"] = "不认识的判据取值来源 %r" % kind
        return record
    record["metadataSource"] = source
    record["operand"] = operand
    if meta:
        record["operandDetail"] = meta
    if len(points) == 0:
        record["reason"] = "图上没有 %s 的观测（判据不可判定）" % metric
        return record
    observed = to_number(points[-1][1])
    record["observed"] = points[-1][1]
    if observed is None or operand is None:
        record["reason"] = ("比较不可判定（观测值 %r / 阈值 %r）"
                            % (points[-1][1], operand))
        return record
    if op == ">":
        record["outcome"] = observed > operand
    elif op == ">=":
        record["outcome"] = observed >= operand
    elif op == "<":
        record["outcome"] = observed < operand
    elif op == "<=":
        record["outcome"] = observed <= operand
    elif op in ("==", "="):
        record["outcome"] = observed == operand
    elif op in ("!=", "<>"):
        record["outcome"] = observed != operand
    else:
        record["reason"] = "不认识的比较运算符 %r" % op
        return record
    return record


def rank_candidates(candidates: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """§5.8 确定性排序：①命中判据数↓ ②跳数↑ ③入边关系优先级 ④并列全部返回不合并。

    ★★ 2026-10-13 修正（fi-e2e 端到端实测驱动）★★
    原实现的排序键把 `determined` 放在**第一位**，即「**全部条款都已判定**」优先于「命中条款数」。
    这与契约自述的 `rankBasis` **①「命中的判据条款数降序」相冲突**，并造成一个实测可见的病态：

        一个 `determined=True` 但 `satisfiedClauses=0` 的候选（= 所有条款都被判为 **FALSE**，
        **零正向证据**）会排在 `satisfiedClauses=1` 但 `determined=False` 的候选（**有正向证据**）之前。

    实测后果（suite50 固定分母 50 条）：KG 的 top1 在 12 例里 **11 例** 是
    `FM-HOST-MEM-PRESSURE`（sat=0）、ONTO 的 top1 在 12 例里 **10 例** 是
    `FM-APP-POOL-EXHAUSTED`（sat=0）—— **top1 退化成常量，A/B 对照失去区分度**。

    现按契约字面把 ① 提为第一键；`determined` 降为**同命中数之内**的次序，
    仍满足契约「不可判定的候选排在有判定结果的候选之后」的意图。
    """
    def key(item: Dict[str, Any]):
        determined = 0 if item.get("determined") else 1
        satisfied = -(int(item.get("satisfiedClauses") or 0))
        hops = item.get("hops")
        hops_key = int(hops) if isinstance(hops, int) else 10 ** 6
        prio = RELATION_PRIORITY.get(str(item.get("relationPriority")), UNRANKED_PRIORITY)
        return (satisfied, determined, hops_key, prio, str(item.get("faultCode") or ""))

    ordered = sorted(candidates, key=key)
    for index, item in enumerate(ordered, start=1):
        item["rank"] = index
    return ordered


RANK_BASIS = (
    "确定性排序（不合成权重、不给概率）："
    "① 命中的判据条款数降序；"
    "② 同级内：有判定结果的排在不可判定之前；"
    "③ 因果路径跳数升序；"
    "④ 入边关系优先级 CAUSES > CONDITIONAL_CAUSES（RELATES_TO 属共现，不参与根因收敛）；"
    "⑤ 并列按故障码字典序（保证跨载体逐字可比）；"
    "⑥ 并列全部返回，不合并为单一分数。"
    "★ 2026-10-13 修正：原实现把「有判定结果」放在**第一位**，与 ① 冲突，"
    "并让「零命中但条款全为 FALSE」的候选举压「有命中但部分条款不可判」的候选 ⇒ top1 退化为常量。"
    "规则来源：契约 shared/model/tools-setb.yaml#tools.cc_cause 与 "
    "guards.R-DEPTH-CAP（沿 CAUSES 深度 ≤ 4）；"
    "判据来源：图上的判据边 CC_JUDGED_BY（边属性 op/role/operandKind/operandRef，"
    "逐条对应 shared/model/fault-modes.yaml#fault_modes[].detect）；"
    "阈值来源：CC_Threshold.thrWarning/thrCritical 与对象属性；"
    "指标观测来源：CC_Evidence（recordId 可溯源，满足硬约束 C5）；"
    "路径关系来源：CC_CAUSES/CC_CONDITIONAL_CAUSES 边的 contractId（E5..E18，"
    "见 shared/model/causal-edges.yaml）。"
)


# ===========================================================================
# 7. 七个工具的实现（只读图查询 + 契约形状）
# ===========================================================================
def _rows(backend: Neo4jHttpBackend, statement: str,
          parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return backend.cypher(statement, parameters).get("rows") or []


# -- KG 侧统一标识空间 --------------------------------------------------------
# 节点 id 的解析顺序：ABox 对象用 ccfId；目录级节点（故障模式/指标/阈值/动作/预案/
# 规则/证据）没有 ccfId，只有各自的业务码。用同一个 coalesce 表达式取 id，避免出现
# null —— Python 侧 str(None) 会变成字符串 "None"，污染拓扑结果（活体曾复现）。
KG_ID_PROPERTIES = ("ccfId", "fmCode", "metricCode", "thrCode", "actionId",
                    "runbookId", "ruleCode", "recordId")

# objectTypes[].keyAttrs 的展示优先级（**不得含重复项**：重复会让同一属性出现两次）。
KEY_ATTR_PRIORITY = ("recordId", "ccfId", "metricCode", "thrCode", "fmCode", "key",
                     "poolId", "containerId", "instanceId")


def _id_expr(alias: str) -> str:
    return "coalesce(%s)" % ", ".join("%s.%s" % (alias, p) for p in KG_ID_PROPERTIES)


def _type_expr(alias: str) -> str:
    """对象类型标识空间 = `ccfType` 值优先，否则 `CC_` 标签去前缀（FaultMode/Metric/…）。

    这样 cc_schema / cc_guide 声明的 objectType 与 cc_facts 接受的 objectType
    落在**同一个空间**（活体曾出现 schema 报 CC_Host、facts 只认 Host 的自相矛盾）。
    """
    return ("coalesce(%s.ccfType, substring(head([l IN labels(%s) "
            "WHERE l STARTS WITH 'CC_']), 3))" % (alias, alias))


def _native_labels_expr(alias: str) -> str:
    """本条记录在本机**实际**带有的原生标签列表（逐条取自本机返回，不按类型名反向拼写）。"""
    return "[l IN labels(%s) WHERE l STARTS WITH 'CC_']" % alias


def _native_ref(value: Any) -> Optional[str]:
    """原生标识落位：只接受本机实际返回的取值；取不到 ⇒ None（由调用方记账，绝不猜测）。"""
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _native_label_of(item: Dict[str, Any]) -> Optional[str]:
    """从本机实际返回的标签集合里挑出与该条目同名的那个；本机没有可用标签 ⇒ None。

    注意这里**只**在「本机真实返回值」之间做选择，不参与任何命名规则推导：
    标签集合本身就是上一跳查询的返回列。
    """
    labels = sorted(str(x) for x in (item.get("nativeLabels") or ()) if x)
    if not labels:
        return None
    want = str(item.get("objectType") or "")
    for label in labels:
        if label.startswith("CC_") and label[3:] == want:
            return label
    return labels[0]


def _native_ref_hint(missing: Sequence[Tuple[str, str]]) -> str:
    """原生标识缺口如实记账（不猜测、不静默）：缺一条就在 hint 里说一条。"""
    if not missing:
        return ""
    shown = "、".join("%s:%s" % (kind, name) for kind, name in list(missing)[:5])
    tail = "…" if len(missing) > 5 else ""
    return ("有 %d 条未取到原生标识，已置 nativeRef=null（不按命名规则猜测）：%s%s"
            % (len(missing), shown, tail))


def _key_attrs(keysets: Sequence[Sequence[str]], limit: int = 8) -> List[str]:
    if not keysets:
        return []
    common = set(keysets[0])
    for ks in keysets[1:]:
        common &= set(ks)
    priority = KEY_ATTR_PRIORITY
    ordered = list(dict.fromkeys(k for k in priority if k in common))
    ordered += sorted(k for k in common if k not in priority and k != "ccfSnapshotId")
    return ordered[:limit]


def _collapse_naming(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        name = row.get("objectType")
        if name is None:
            continue
        item = seen.setdefault(name, {"objectType": name, "count": 0, "layers": set(),
                                      "keySets": [], "nativeLabels": set()})
        item["count"] += 1
        layer = row.get("layer")
        if layer:
            item["layers"].add(str(layer))
        labels = row.get("nativeLabels")
        if isinstance(labels, list):
            for label in labels:
                if label:
                    item["nativeLabels"].add(str(label))
        ks = row.get("keys")
        if ks is None:
            ks = row.get("ks")                     # 兼容旧别名（活体曾因别名不一致恒空）
        if isinstance(ks, list):
            item["keySets"].append([str(k) for k in ks])
    return [seen[k] for k in sorted(seen)]


def build_guide(backend: Neo4jHttpBackend) -> Dict[str, Any]:
    # ★ P1：元数据端点用独立上限（行 + 字节），见 METADATA_BYTE_CAP 的说明
    ledger = Ledger(["metrics", "objectTypes", "rules"],
                    row_cap=METADATA_ROW_CAP, byte_cap=METADATA_BYTE_CAP)
    metric_rows = _rows(
        backend,
        "MATCH (m:CC_Metric) "
        "RETURN m.metricCode AS metricCode, m.metricUnit AS unit, "
        "m.metricObject AS objectType ORDER BY metricCode",
    )
    ledger.put("metrics", [
        {"metricCode": r.get("metricCode"), "unit": r.get("unit"),
         "objectType": r.get("objectType")} for r in metric_rows])

    type_rows = _rows(
        backend,
        "MATCH (n) WHERE %s IS NOT NULL "
        "RETURN %s AS objectType, keys(n) AS keys, "
        "coalesce(n.ccfLayer,'') AS layer, %s AS nativeLabels ORDER BY objectType"
        % (_type_expr("n"), _type_expr("n"), _native_labels_expr("n")),
    )
    collapsed = _collapse_naming(type_rows)
    missing_refs: List[Tuple[str, str]] = []
    guide_types = []
    for item in collapsed:
        native = _native_label_of(item)
        if native is None:
            missing_refs.append(("objectTypes", str(item["objectType"])))
        guide_types.append({
            "objectType": item["objectType"],
            "layer": sorted(item["layers"])[0] if item["layers"] else "",
            "keyAttrs": _key_attrs(item["keySets"]),
            "nativeRef": native,
        })
    ledger.put("objectTypes", guide_types)

    rule_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode)-[r:CC_JUDGED_BY]->(m:CC_Metric) "
        "RETURN f.fmCode AS fmCode, m.metricCode AS metricCode, r.op AS op, "
        "r.role AS role, r.operandKind AS operandKind, r.operandRef AS operandRef, "
        "r.operandValue AS operandValue, r.operandValues AS operandValues "
        "ORDER BY fmCode, metricCode, role",
    )
    causal_rows = _rows(
        backend,
        "MATCH (a:CC_FaultMode)-[r:CC_CAUSES|CC_CONDITIONAL_CAUSES]->(b:CC_FaultMode) "
        "RETURN a.fmCode AS src, b.fmCode AS dst, type(r) AS rel, "
        "r.contractId AS contractId, r.chain AS chain, r.description AS description "
        "ORDER BY src, dst",
    )
    rules: List[Dict[str, Any]] = []
    for row in rule_rows:
        rules.append({
            "ruleCode": "judge:%s|%s" % (row.get("fmCode"), row.get("metricCode")),
            "ruleKind": "detect_clause",
            "description": _clause_text(row),
        })
    for row in causal_rows:
        rules.append({
            "ruleCode": "cause:%s" % (row.get("contractId") or
                                      "%s-%s" % (row.get("src"), row.get("dst"))),
            "ruleKind": "causal_link",
            "description": "因果关系 %s → %s（链 %s）：%s"
                           % (row.get("src"), row.get("dst"), row.get("chain"),
                              row.get("description") or ""),
        })
    ledger.put("rules", rules)
    hint = ""
    if not (metric_rows or collapsed or rules):
        hint = "本环境未返回指标/对象类型/规则：可能快照未装载（检查 CC_Meta.snapshotId）。"
    hint = " ".join(part for part in (hint, _native_ref_hint(missing_refs)) if part)
    return ledger.finish(hint)


def _clause_text(row: Dict[str, Any]) -> str:
    operand = ""
    if row.get("operandKind") == "threshold":
        operand = "阈值 %s" % row.get("operandRef")
    elif row.get("operandKind") == "entity_property":
        operand = "对象属性 %s" % row.get("operandRef")
    elif row.get("operandKind") == "literal":
        operand = "字面量 %s" % row.get("operandValue")
    elif row.get("operandKind") == "enum":
        operand = "枚举 %s" % (row.get("operandValues") or [])
    elif row.get("operandKind") == "predicate":
        operand = "时间序列判定"
    return "判据：%s 的 %s 条件 —— %s %s %s" % (
        row.get("fmCode"), row.get("role"), row.get("metricCode"),
        row.get("op"), operand)


def build_schema(backend: Neo4jHttpBackend) -> Dict[str, Any]:
    # ★ P1：同 cc_guide —— 元数据目录必须完整
    ledger = Ledger(["objectTypes", "relationTypes", "attributes"],
                    row_cap=METADATA_ROW_CAP, byte_cap=METADATA_BYTE_CAP)
    type_rows = _rows(
        backend,
        "MATCH (n) WHERE %s IS NOT NULL "
        "RETURN %s AS objectType, keys(n) AS keys, "
        "%s AS nativeLabels ORDER BY objectType"
        % (_type_expr("n"), _type_expr("n"), _native_labels_expr("n")),
    )
    collapsed = _collapse_naming(type_rows)
    missing_refs: List[Tuple[str, str]] = []
    schema_types = []
    for item in collapsed:
        native = _native_label_of(item)
        if native is None:
            missing_refs.append(("objectTypes", str(item["objectType"])))
        schema_types.append({
            "objectType": item["objectType"],
            "keyAttrs": _key_attrs(item["keySets"]),
            "nativeRef": native,
        })
    ledger.put("objectTypes", schema_types)

    rel_rows = _rows(
        backend,
        "MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH 'CC_' "
        "AND a.ccfType IS NOT NULL AND b.ccfType IS NOT NULL "
        "RETURN type(r) AS relationType, count(r) AS edgeCount, "
        "count(DISTINCT a) AS fromNodes, count(DISTINCT b) AS toNodes, "
        "collect(DISTINCT a.ccfType) AS fromTypes, collect(DISTINCT b.ccfType) AS toTypes "
        "ORDER BY relationType",
    )
    relation_types = []
    for row in rel_rows:
        edges = int(row.get("edgeCount") or 0)
        from_nodes = int(row.get("fromNodes") or 0)
        to_nodes = int(row.get("toNodes") or 0)
        if edges and edges == from_nodes == to_nodes:
            cardinality = "1:1"
        elif edges and edges == from_nodes and edges > to_nodes:
            cardinality = "N:1"
        elif edges and edges > from_nodes and edges == to_nodes:
            cardinality = "1:N"
        else:
            cardinality = "N:M"
        native_rel = _native_ref(row.get("relationType"))
        if native_rel is None:
            missing_refs.append(("relationTypes", str(row.get("relationType"))))
        relation_types.append({
            "relationType": row.get("relationType"),
            "from": sorted(str(t) for t in (row.get("fromTypes") or [])),
            "to": sorted(str(t) for t in (row.get("toTypes") or [])),
            "cardinality": cardinality,
            "edgeCount": edges,
            "nativeRef": native_rel,
        })
    ledger.put("relationTypes", relation_types)

    attr_rows = _rows(
        backend,
        "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'CC_') "
        "UNWIND [l IN labels(n) WHERE l STARTS WITH 'CC_'] AS owner "
        "UNWIND keys(n) AS attribute "
        "WITH owner, attribute, collect(DISTINCT n[attribute]) AS samples "
        "RETURN owner, attribute, samples[0..6] AS samples ORDER BY owner, attribute",
    )
    rel_attr_rows = _rows(
        backend,
        "MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH 'CC_' "
        "UNWIND keys(r) AS attribute "
        "WITH 'relation:' + type(r) AS owner, attribute, "
        "collect(DISTINCT r[attribute]) AS samples "
        "RETURN owner, attribute, samples[0..6] AS samples ORDER BY owner, attribute",
    )
    attributes = []
    for row in list(attr_rows) + list(rel_attr_rows):
        native_attr = _native_ref(row.get("attribute"))
        if native_attr is None:
            missing_refs.append(("attributes", "%s.%s" % (row.get("owner"), row.get("attribute"))))
        attributes.append({
            "owner": row.get("owner"),
            "attribute": row.get("attribute"),
            "valueType": _value_type(row.get("samples") or []),
            "nativeRef": native_attr,
        })
    ledger.put("attributes", attributes)
    ledger.put("naming_discipline", NAMING_DISCIPLINE)
    hint = ""
    if not collapsed and not relation_types and not attributes:
        hint = "本环境未返回对象类型/关系类型/属性：可能快照未装载或未建索引。"
    hint = " ".join(part for part in (hint, _native_ref_hint(missing_refs)) if part)
    return ledger.finish(hint)


def _value_type(samples: Sequence[Any]) -> str:
    kinds = set()
    for sample in samples:
        if sample is None:
            kinds.add("null")
        elif isinstance(sample, bool):
            kinds.add("boolean")
        elif isinstance(sample, int):
            kinds.add("integer")
        elif isinstance(sample, float):
            kinds.add("decimal")
        elif isinstance(sample, str):
            kinds.add("string")
        elif isinstance(sample, list):
            kinds.add("array")
        elif isinstance(sample, dict):
            kinds.add("object")
        else:
            kinds.add(type(sample).__name__)
    kinds.discard("null")
    if not kinds:
        return "unknown"
    if len(kinds) == 1:
        return kinds.pop()
    return "mixed(%s)" % ",".join(sorted(kinds))


def build_query(backend: Neo4jHttpBackend, query: str) -> Dict[str, Any]:
    guarded = guard_query(query)
    bounded, injected = apply_force_limit(guarded)
    try:
        result = backend.cypher(bounded)
    except GraphError as exc:
        if exc.kind == "timeout":
            raise ToolError(ERR_QUERY_TIMEOUT, exc.message, timeoutSeconds=QUERY_TIMEOUT_S,
                            forcedLimitInjected=injected)
        if exc.kind == "syntax":
            raise ToolError(ERR_QUERY_FORM_REJECTED, exc.message, forcedLimitInjected=injected)
        raise
    rows = result.get("rows") or []
    dropped = 0
    hint_parts: List[str] = []
    if len(rows) > ROW_CAP:
        total = _exact_total(backend, guarded, len(rows))
        dropped = max(0, total - ROW_CAP)
        rows = rows[:ROW_CAP]
        hint_parts.append("行上限触发：精确总行数 %d，返回 %d 行。" % (total, len(rows)))
    ledger = Ledger(["rows"])
    ledger.put("rows", rows)
    if dropped:
        ledger.dropped_rows = dropped
        ledger.truncated = True
    if not rows:
        hint_parts.append("查询无匹配结果：可能是快照里没有该对象/关系，或过滤条件过窄。")
    if injected:
        hint_parts.append("服务端已按守卫自动注入行上限（R-FORCE-LIMIT；等效动作，不改变语义）。")
    final = ledger.finish(" ".join(hint_parts))
    final["rowCount"] = len(final.get("rows") or [])
    return final


def _exact_total(backend: Neo4jHttpBackend, guarded_query: str, fallback: int) -> int:
    """被截断时用计数子查询给出精确总行数；失败则回落到已知下界（不伪造）。"""
    try:
        rows = _rows(backend, build_count_query(guarded_query))
        if rows and rows[0].get("total") is not None:
            return int(rows[0]["total"])
    except Exception:
        pass
    return fallback


def build_facts(backend: Neo4jHttpBackend, object_type: str,
                object_id: Optional[str] = None,
                metric_codes: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    declared = [str(r.get("objectType")) for r in _rows(
        backend, "MATCH (n) WHERE %s IS NOT NULL "
                 "RETURN DISTINCT %s AS objectType ORDER BY objectType"
                 % (_type_expr("n"), _type_expr("n")))]
    if object_type not in declared:
        raise ToolError(ERR_INVALID_PARAM,
                        "未声明的对象类型 %r；本环境已声明的对象类型为 %s（不得回落默认值）。"
                        % (object_type, declared),
                        objectType=object_type, declaredObjectTypes=declared)

    ledger = Ledger(["objects", "relations", "observations"])
    where = ["%s = $objectType" % _type_expr("n")]
    params: Dict[str, Any] = {"objectType": object_type}
    if object_id is not None:
        where.append("%s = $objectId" % _id_expr("n"))
        params["objectId"] = object_id
    predicate = " AND ".join(where)
    obj_rows = _rows(
        backend,
        "MATCH (n) WHERE %s "
        "RETURN %s AS objectId, %s AS objectType, properties(n) AS attributes "
        "ORDER BY objectId" % (predicate, _id_expr("n"), _type_expr("n")),
        params,
    )
    ledger.put("objects", [
        {"objectId": r.get("objectId"), "objectType": r.get("objectType"),
         "attributes": _clean_attrs(r.get("attributes"))} for r in obj_rows])

    rel_rows = _rows(
        backend,
        "MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH 'CC_' "
        "AND NOT type(r) IN $skipRels "
        "AND ((%s = $objectType %s) OR (%s = $objectType %s)) "
        "RETURN type(r) AS relationType, %s AS src, %s AS dst "
        "ORDER BY relationType, src, dst"
        % (_type_expr("a"),
           "AND %s = $objectId" % _id_expr("a") if object_id is not None else "",
           _type_expr("b"),
           "AND %s = $objectId" % _id_expr("b") if object_id is not None else "",
           _id_expr("a"), _id_expr("b")),
        dict(params, skipRels=list(NON_TOPOLOGY_RELS)),
    )
    ledger.put("relations", [
        {"relationType": r.get("relationType"), "from": r.get("src"), "to": r.get("dst")}
        for r in rel_rows])

    obs_where = (["%s = $objectType" % _type_expr("n")] if object_id is None
                 else ["%s = $objectId" % _id_expr("n")])
    obs_params: Dict[str, Any] = {"objectType": object_type}
    if object_id is not None:
        obs_params["objectId"] = object_id
    if metric_codes:
        obs_where.append("e.metricCode IN $metricCodes")
        obs_params["metricCodes"] = list(metric_codes)
    obs_rows = _rows(
        backend,
        "MATCH (n)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence) WHERE %s "
        "RETURN e.recordId AS recordId, e.metricCode AS metricCode, "
        "e.rawValue AS value, e.rawUnit AS unit, e.capturedAt AS ts "
        "ORDER BY metricCode, recordId" % " AND ".join(obs_where),
        obs_params,
    )
    ledger.put("observations", [
        {"recordId": r.get("recordId"), "metricCode": r.get("metricCode"),
         "value": r.get("value"), "unit": r.get("unit"), "ts": r.get("ts")}
        for r in obs_rows])

    hint = ""
    if not obj_rows:
        hint = ("没有 %s 类型的对象%s：该类型在快照中可能无实例，或 objectId 不存在。"
                % (object_type, "" if object_id is None else "（objectId=%s）" % object_id))
    final = ledger.finish(hint)
    if not final.get("objects") and not final.get("relations") and not final.get("observations"):
        final["hint"] = "该对象类型/对象在快照中没有事实记录：请先用 cc_guide 核对对象类型与对象 id。"
    return final


def _clean_attrs(attrs: Any) -> Dict[str, Any]:
    if not isinstance(attrs, dict):
        return {}
    return {k: v for k, v in attrs.items()}


def build_trace(backend: Neo4jHttpBackend, start_id: str,
                direction: Optional[str] = None,
                max_hops: Optional[int] = None) -> Dict[str, Any]:
    hops = DEFAULT_TRACE_HOPS if max_hops is None else int(max_hops)
    if hops < 0:
        raise ToolError(ERR_INVALID_PARAM, "maxHops 不得为负数：%d。" % hops,
                        maxHops=hops)
    if hops > MAX_TRACE_HOPS:
        raise ToolError(ERR_INVALID_PARAM,
                        "maxHops=%d 超出上界 %d。" % (hops, MAX_TRACE_HOPS),
                        maxHops=hops, limit=MAX_TRACE_HOPS)
    dirn = DEFAULT_DIRECTION if direction is None else str(direction).strip().lower()
    if dirn not in ("out", "in", "both"):
        raise ToolError(ERR_INVALID_PARAM,
                        "direction=%r 非法；取值域为 out|in|both（OQ-1）。" % direction,
                        direction=direction, allowed=["out", "in", "both"])

    start_rows = _rows(
        backend,
        "MATCH (n) WHERE %s = $startId "
        "RETURN %s AS objectId, %s AS objectType, "
        "coalesce(n.ccfLayer,'') AS layer ORDER BY objectId LIMIT 1"
        % (_id_expr("n"), _id_expr("n"), _type_expr("n")),
        {"startId": start_id},
    )
    ledger = Ledger(["nodes", "edges"])
    if not start_rows:
        ledger.put("nodes", [])
        ledger.put("edges", [])
        ledger.put("hopsUsed", 0)
        return ledger.finish(
            "起始对象 %r 不存在：请先用 cc_query 或 cc_facts 确认可用的对象 id。" % start_id)

    nodes: Dict[str, Dict[str, Any]] = {}
    first = start_rows[0]
    nodes[str(first.get("objectId"))] = {
        "objectId": first.get("objectId"), "objectType": first.get("objectType"),
        "layer": first.get("layer")}
    edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    frontier = [str(first.get("objectId"))]
    used_hops = 0
    for depth in range(1, hops + 1):
        if not frontier:
            break
        found = _expand(backend, frontier, dirn)
        if not found:
            break
        used_hops = depth
        next_frontier: List[str] = []
        for row in found:
            raw_src, raw_dst = row.get("src"), row.get("dst")
            if raw_src is None or raw_dst is None:
                # 兜底：绝不把 None 变成字符串 "None" 混进 nodes/edges
                continue
            src, dst = str(raw_src), str(raw_dst)
            # edgeFrom/edgeTo 是**真实图方向**：direction=in 时邻居才是边的起点，
            # 若直接用 (src, dst) 记录会把入边方向写反（拓扑工具不可接受）。
            edge_from = str(row.get("edgeFrom") or src)
            edge_to = str(row.get("edgeTo") or dst)
            key = (edge_from, str(row.get("relationType")), edge_to)
            edges.setdefault(key, {"relationType": row.get("relationType"),
                                   "from": edge_from, "to": edge_to})
            if dst not in nodes:
                nodes[dst] = {"objectId": dst, "objectType": row.get("dstType"),
                              "layer": row.get("dstLayer") or ""}
                next_frontier.append(dst)
        frontier = sorted(set(next_frontier))
    ledger.put("nodes", [nodes[k] for k in sorted(nodes)])
    ledger.put("edges", [edges[k] for k in sorted(edges)])
    ledger.put("hopsUsed", used_hops)
    # ★ P3-fix：随成功响应回传**参数枚举与实际上限**（实测 agent 会猜 `direction='downstream'`）。
    #   ★ 注意用 `MAX_TRACE_HOPS`（契约 cc_trace.maxHops ≤ 9）—— trace 的上限与
    #   `cc_cause` 的 `MAX_CAUSE_DEPTH`(=4) **不是同一个**，误用会给 agent 报更严的假上限。
    ledger.put("directionEnum", ["out", "in", "both"])
    ledger.put("maxHopsLimit", MAX_TRACE_HOPS)
    return ledger.finish()


def _expand(backend: Neo4jHttpBackend, frontier: Sequence[str],
            direction: str) -> List[Dict[str, Any]]:
    """BFS 一层的邻居展开。

    邻居 id 用 `_id_expr`（不含 ccfId 的目录级节点 —— 如 CC_FaultMode —— 退回业务码），
    否则 `str(None)` 会产生字面量 `"None"` 节点、并让入边退化成自环。
    邻居 id 无法解析的行直接不返回（不以伪造 id 冒充，也不静默改变方向）。
    """
    params = {"frontier": list(frontier), "skipRels": list(NON_TOPOLOGY_RELS)}
    out: List[Dict[str, Any]] = []
    if direction in ("out", "both"):
        out += _rows(
            backend,
            "MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH 'CC_' "
            "AND NOT type(r) IN $skipRels AND %s IN $frontier "
            "AND %s IS NOT NULL "
            "RETURN %s AS src, type(r) AS relationType, %s AS dst, "
            "%s AS dstType, coalesce(b.ccfLayer,'') AS dstLayer, "
            "%s AS edgeFrom, %s AS edgeTo "
            "ORDER BY src, relationType, dst"
            % (_id_expr("a"), _id_expr("b"), _id_expr("a"), _id_expr("b"),
               _type_expr("b"), _id_expr("a"), _id_expr("b")),
            params,
        )
    if direction in ("in", "both"):
        out += _rows(
            backend,
            "MATCH (a)<-[r]-(b) WHERE type(r) STARTS WITH 'CC_' "
            "AND NOT type(r) IN $skipRels AND %s IN $frontier "
            "AND %s IS NOT NULL "
            "RETURN %s AS src, type(r) AS relationType, %s AS dst, "
            "%s AS dstType, coalesce(b.ccfLayer,'') AS dstLayer, "
            "%s AS edgeFrom, %s AS edgeTo "
            "ORDER BY src, relationType, dst"
            % (_id_expr("a"), _id_expr("b"), _id_expr("a"), _id_expr("b"),
               _type_expr("b"), _id_expr("b"), _id_expr("a")),
            params,
        )
    return out


def build_cause(backend: Neo4jHttpBackend, symptom_codes: Sequence[str],
                max_hops: Optional[int] = None) -> Dict[str, Any]:
    hops = MAX_CAUSE_DEPTH if max_hops is None else int(max_hops)
    if hops > MAX_CAUSE_DEPTH:
        raise ToolError(ERR_DEPTH_LIMIT_EXCEEDED,
                        "maxHops=%d 超出因果链硬上限 %d（不得截断，只能拒绝）。"
                        % (hops, MAX_CAUSE_DEPTH),
                        maxHops=hops, limit=MAX_CAUSE_DEPTH)
    if hops < 0:
        raise ToolError(ERR_INVALID_PARAM, "maxHops 不得为负数：%d。" % hops, maxHops=hops)

    fm_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode) "
        "RETURN f.fmCode AS fmCode, f.fmName AS fmName, f.fmLayer AS fmLayer, "
        "f.fmSeverity AS fmSeverity, f.fmComponentType AS fmComponentType, "
        "f.fmDetectionMode AS fmDetectionMode, f.fmReversible AS fmReversible, "
        "f.fmChain AS fmChain, f.fmDescription AS fmDescription ORDER BY fmCode",
    )
    by_code = {str(r.get("fmCode")): r for r in fm_rows}
    alias: Dict[str, str] = {}
    for row in fm_rows:
        alias[str(row.get("fmCode"))] = str(row.get("fmCode"))
        if row.get("fmName"):
            alias.setdefault(str(row.get("fmName")), str(row.get("fmCode")))
    unknown = [c for c in symptom_codes if c not in alias]
    if unknown:
        raise ToolError(ERR_INVALID_PARAM,
                        "未识别的症状/故障码 %s；本环境接受 %s（不得回落默认值）。"
                        % (unknown, sorted(by_code)),
                        unknownSymptomCodes=unknown, acceptedSymptomCodes=sorted(by_code))
    starts = sorted({alias[c] for c in symptom_codes})

    edge_rows = _rows(
        backend,
        "MATCH (a:CC_FaultMode)-[r:CC_CAUSES|CC_CONDITIONAL_CAUSES]->(b:CC_FaultMode) "
        "RETURN a.fmCode AS src, type(r) AS rel, b.fmCode AS dst, "
        "r.contractId AS contractId, r.chain AS chain, "
        # ★ 2026-10-14 修正：必须把 cond* 取回来 —— LPG 的落地约定是
        #   「CC_CAUSES + 有 cond* 属性 ⇔ conditional_causes」（docs/03 §5.3、
        #   ontology/03-fault-modes.cypher 的头部说明），故**关系类型不足以判定**
        #   条件性；原实现只读 type(r) ⇒ 4 条条件因果边（E6/E7/E9/E12）一律被报成
        #   causes ⇒ 契约 rankBasis ③「causes > conditional_causes」的区分能力失效，
        #   并与 ONTO 侧（读 ccc:linkKind）产生 relationPriority 分歧。
        "r.condMetric AS condMetric, r.condKind AS condKind, r.condRef AS condRef "
        "ORDER BY src, dst",
    )
    upstream: Dict[str, List[Tuple[str, str, str]]] = {}
    for row in edge_rows:
        src = str(row.get("src"))
        rel = derive_relation_priority(str(row.get("rel")), row)
        upstream.setdefault(str(row.get("dst")), []).append(
            (src, rel, str(row.get("contractId") or "")))
    for key in upstream:
        upstream[key].sort(key=lambda item: (item[0], item[1]))

    depths: Dict[str, int] = {}
    parents: Dict[str, Tuple[str, str, str]] = {}
    frontier = list(starts)
    for code in frontier:
        depths[code] = 0
    for depth in range(1, hops + 1):
        nxt: List[str] = []
        for node in sorted(frontier):
            for src, rel, contract in upstream.get(node, []):
                if src in depths:
                    continue
                depths[src] = depth
                parents[src] = (node, rel, contract)
                nxt.append(src)
        frontier = sorted(set(nxt))
        if not frontier:
            break

    candidates_codes = sorted(c for c in depths if c not in starts)
    # ★★ A″（2026-10-15）：把「症状码自身即根因」的**源点**纳入候选集 ★★
    #   图论事实（工具级已核验 **23/23**，两载体一致）：
    #     `/cause` 返回 0 候选 ⟺ 该码**没有** `CC_CAUSES`/`CC_CONDITIONAL_CAUSES` 入边
    #     ⟺ 它是因果图的**源点**（没有任何上游成因）。
    #   （粗看「入度」会把 `CC_RELATES_TO` 共现边算进去 ⇒ `FM-HOST-MEM-PRESSURE`
    #    会被误判成「有入边却 0 候选」；按契约 rankBasis ④，共现边不参与根因收敛。）
    #   **源点被观测到 ⇒ 它自身就是根因**，而不是「找不到根因」。
    #
    #   原实现此处**直接返回空候选 + 一句 hint**。实测后果（m6 A-05/A-06，四组同时
    #   发生）：agent 拿到 2.8 KB 空载荷、无判据可用 ⇒ 转为反向工程数据模型
    #   （`MATCH (n) RETURN DISTINCT labels(n)`）⇒ 15–21 轮、撞顶、Dify 500。
    #   50 条用例中 **22 条（44%）** 的期望根因落在源点集内。
    #   ⇒ 把源点**自身**作为 0 跳候选返回，使其走**完全相同**的
    #     条款求值 / 实例选择 / 焦点回传管线（**不新增特例分支**）。
    #   注：规则与「候选集是否为空」**无关** —— 只要入参里含源点就纳入，
    #   避免写出一个依赖空集的特例（这类特例正是本项目反复出错的地方）。
    self_root_codes: List[str] = [c for c in starts if c not in upstream]
    for code in starts:
        if code not in upstream and code not in candidates_codes:
            candidates_codes.append(code)
    candidates_codes = sorted(candidates_codes)

    clause_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode)-[r:CC_JUDGED_BY]->(m:CC_Metric) "
        "RETURN f.fmCode AS fmCode, m.metricCode AS metricCode, r.op AS op, "
        "r.role AS role, r.operandKind AS operandKind, r.operandRef AS operandRef, "
        "r.operandValue AS operandValue, r.operandValues AS operandValues "
        "ORDER BY fmCode, metricCode, role",
    )
    clauses: Dict[str, List[Dict[str, Any]]] = {}
    for row in clause_rows:
        clauses.setdefault(str(row.get("fmCode")), []).append(row)

    affected_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode)-[:CC_AFFECTS]->(n) "
        "RETURN f.fmCode AS fmCode, %s AS objectId, %s AS objectType "
        "ORDER BY fmCode, objectId" % (_id_expr("n"), _type_expr("n")),
    )
    affected: Dict[str, List[Tuple[str, str]]] = {}
    for row in affected_rows:
        affected.setdefault(str(row.get("fmCode")), []).append(
            (str(row.get("objectId")), str(row.get("objectType"))))

    codes = sorted({str(row.get("metricCode")) for row in clause_rows})
    obs_rows = _rows(
        backend,
        "MATCH (n)-[:CC_HAS_EVIDENCE]->(e:CC_Evidence) WHERE e.metricCode IN $codes "
        "RETURN %s AS objectId, e.metricCode AS metricCode, "
        "e.rawValue AS rawValue, e.capturedAt AS capturedAt, e.recordId AS recordId "
        "ORDER BY objectId, metricCode, capturedAt" % _id_expr("n"),
        {"codes": codes},
    )
    series = series_of(obs_rows)
    threshold_rows = _rows(
        backend,
        "MATCH (t:CC_Threshold) RETURN t.thrCode AS thrCode, "
        "t.thrWarning AS thrWarning, t.thrCritical AS thrCritical ORDER BY thrCode",
    )
    thresholds = {str(r.get("thrCode")): r for r in threshold_rows}

    instance_cache: Dict[str, Dict[str, Any]] = {}

    def props_of(object_id: str) -> Dict[str, Any]:
        if object_id not in instance_cache:
            rows = _rows(backend,
                         "MATCH (n) WHERE %s = $objectId "
                         "RETURN properties(n) AS props LIMIT 1" % _id_expr("n"),
                         {"objectId": object_id})
            instance_cache[object_id] = (rows[0].get("props") or {}) if rows else {}
        return instance_cache[object_id]

    type_instances: Dict[str, List[str]] = {}

    def instances_for(code: str) -> List[Tuple[str, str]]:
        if affected.get(code):
            return affected[code]
        ctype = str(by_code.get(code, {}).get("fmComponentType") or "")
        if not ctype:
            return []
        if ctype not in type_instances:
            rows = _rows(backend,
                         "MATCH (n) WHERE %s = $t RETURN %s AS objectId "
                         "ORDER BY objectId" % (_type_expr("n"), _id_expr("n")),
                         {"t": ctype})
            type_instances[ctype] = [str(r.get("objectId")) for r in rows]
        return [(oid, ctype) for oid in type_instances[ctype]]

    leaf_roots = {str(r.get("fmCode")) for r in fm_rows if str(r.get("fmCode")) not in upstream}

    candidates: List[Dict[str, Any]] = []
    for code in candidates_codes:
        meta = by_code.get(code, {})
        # ★★ 2026-10-13 修正：只取与契约 `detect` **主体**对位的角色计入条款集 ★★
        #   契约 `fault-modes.yaml#fault_modes[].detect` 只声明 `all_of`/`any_of`，
        #   在 KG 侧对应 `necessary`/`sufficient`。而 `exclusion`（排除条款）与
        #   `distinguishing`（区分条款）**不是** detect 主体，不能计入 `totalClauses` ——
        #   否则 `FM-APP-DS-CONNECT-FAIL` 会算出 **3** 条（1 necessary + 2 exclusion），
        #   而契约只有 1 条 ⇒ `all_of` 的 AND 面被凭空放大，UNK 概率随之上升。
        #   （实测：两侧 `totalClauses` 有 19/122 对不同，其中该码固定 KG 3 → ONTO 1。）
        #   注：`clauses` 全集**仍保留**给 `condition` 文案展示各角色命中情况，此处只收窄用途。
        _DETECT_ROLES = ("necessary", "sufficient")
        cand_clauses = [c for c in clauses.get(code, [])
                        if str(c.get("role")) in _DETECT_ROLES]
        inst_list = instances_for(code)
        best: Optional[Dict[str, Any]] = None
        best_key: Optional[tuple] = None
        for object_id, object_type in inst_list:
            records = [evaluate_clause(c, props_of(object_id), thresholds, series, object_id)
                       for c in cand_clauses]
            satisfied = sum(1 for r in records if r.get("outcome") is True)
            undetermined = [r for r in records if r.get("outcome") is None]
            determined = bool(records) and not undetermined
            summary = {
                "componentInstance": object_id,
                "componentType": object_type,
                "satisfiedClauses": satisfied,
                "totalClauses": len(records),
                "determined": determined,
                "undeterminedClauses": len(undetermined),
                # ★ N3 修复（2026-10-13）：与 ONTO 侧统一 —— `undeterminedClauses` 只装**整数计数**
                #   （同 satisfiedClauses / totalClauses 的类型），原因明细挂 `undeterminedReasons`。
                #   原实现两侧同名不同物（KG 整数 / ONTO 原因列表）⇒ 违反 C3，跨载体不可比。
                "undeterminedReasons": [str(r.get("reason")) for r in undetermined
                                        if r.get("reason") is not None],
                "records": records,
            }
            # ★★ 2026-10-13 修正：实例选择键（与 ONTO 侧**逐字同语义**）★★
            #   原键 = (satisfiedClauses↓, objectId↑)，即当该码的全部实例 `satisfiedClauses` 都是 0 时，
            #   **完全由 id 字典序决定**，与被选实例**有没有观测**毫无关系。
            #   实测后果（suite50）：KG 选 `host:build-host`（有观测 ⇒ 条款可判 FALSE）、
            #   ONTO 选 `host:host-3`（无观测 ⇒ UNK）⇒ 两侧 `determined` 相反，
            #   ONTO 侧 126 个候选**无一命中条款**、top1 退化为常量。
            #   新键在 id 之前插入「UNK 少者优先」⇒ 优先选中**判据能真正求值**的实例。
            inst_key = (-satisfied, len(undetermined), -len(records), str(object_id))
            if best_key is None or inst_key < best_key:
                best_key, best = inst_key, summary
        if best is None:
            best = {"componentInstance": None, "componentType":
                    meta.get("fmComponentType"), "satisfiedClauses": 0,
                    "totalClauses": len(cand_clauses), "determined": False,
                    "undeterminedClauses": len(cand_clauses),
                    "undeterminedReasons": ["图上没有 %s 的实例" % meta.get("fmComponentType")
                                            for _c in cand_clauses],
                    "records": [
                        {"metricCode": c.get("metricCode"), "op": c.get("op"),
                         "role": c.get("role"), "outcome": None,
                         "reason": "图上没有 %s 的实例" % meta.get("fmComponentType")}
                        for c in cand_clauses]}
        parent, rel, contract = parents.get(code, (None, "causes", ""))
        path = [code]
        cursor = code
        guard = 0
        while cursor in parents and guard < 16:
            cursor = parents[cursor][0]
            path.append(cursor)
            guard += 1
        path_contracts = []
        cursor = code
        guard = 0
        while cursor in parents and guard < 16:
            nxt, rel2, contract2 = parents[cursor]
            path_contracts.append({"from": cursor, "to": nxt, "relation": rel2,
                                   "contractId": contract2})
            cursor = nxt
            guard += 1
        condition_bits = []
        for role in ("necessary", "sufficient", "distinguishing", "exclusion"):
            role_records = [r for r in best["records"] if str(r.get("role")) == role]
            if not role_records:
                continue
            hit = sum(1 for r in role_records if r.get("outcome") is True)
            unknown_role = sum(1 for r in role_records if r.get("outcome") is None)
            condition_bits.append("%s %d/%d 命中%s" % (
                role, hit, len(role_records),
                "，%d 条不可判定" % unknown_role if unknown_role else ""))
        evidence_ids = sorted({str(r.get("metadataSource"))
                               for r in best["records"] if r.get("metadataSource")})
        candidate = {
            "faultCode": code,
            "fmName": meta.get("fmName"),
            "fmLayer": meta.get("fmLayer"),
            "fmSeverity": meta.get("fmSeverity"),
            "fmReversible": meta.get("fmReversible"),
            "fmDetectionMode": meta.get("fmDetectionMode"),
            "path": path,
            "pathEdges": path_contracts,
            "condition": "；".join(condition_bits) or "该故障码在图上没有判据条款",
            "hops": depths.get(code),
            "relationPriority": rel,
            "viaRelation": "CC_CAUSES" if rel == "causes" else "CC_CONDITIONAL_CAUSES",
            "isLeafRootCandidate": code in leaf_roots,
            "componentInstance": best["componentInstance"],
            "componentType": best["componentType"],
            "satisfiedClauses": best["satisfiedClauses"],
            "totalClauses": best["totalClauses"],
            "determined": best["determined"],
            "undeterminedClauses": best["undeterminedClauses"],
            "detectEvidence": best["records"],
            "evidenceSourceIds": evidence_ids,
            "causalContractIds": [e["contractId"] for e in path_contracts if e["contractId"]],
        }
        candidates.append(candidate)

    ordered = rank_candidates(candidates)
    # ★ P2c：`cc_cause` 用**自己的**预算（契约 rankBasis ⑥「并列全部返回」要求候选集完整）
    ledger = Ledger(["candidates"], byte_cap=CAUSE_BYTE_CAP)
    ledger.put("candidates", ordered)
    ledger.put("rankBasis", RANK_BASIS)
    out = ledger.finish()
    # ★ A″ 留痕（与 ONTO 侧逐字同语义）：显式说明哪些候选是「症状码自身」，
    #   以及依据是什么 —— 不靠在载荷里猜。
    if self_root_codes:
        out["selfRootCauseCodes"] = sorted(self_root_codes)
        out["selfRootCauseBasis"] = (
            "这些症状码在因果图上没有上游成因（即因果图源点），"
            "故其自身即根因，以 hops=0 返回；"
            "其判据条款、实例选择、焦点证据与其它候选同法求值。")
        _prev_hint = str(out.get("hint") or "")
        out["hint"] = (_prev_hint + " " if _prev_hint else "") + (
            "★ %s 在因果图上**没有上游成因**，因此它自身就是根因（已作为 hops=0 候选返回）。"
            % "、".join(sorted(self_root_codes)))
    # ★ P2c 记账修复：`Ledger.cap_bytes()` 是**从列表尾部 pop**，而它只累加
    #   `byte_dropped_rows`、**不写进 `droppedRows`** ⇒ 候选被丢掉却在
    #   `truncated/droppedRows` 上看不出来（契约 C6「不得静默截断」）。
    #   此处把「丢了几个、丢了谁」显式写回，使残缺**可见可复算**。
    returned = out.get("candidates") or []
    if len(returned) < len(ordered):
        lost = [str(c.get("faultCode")) for c in ordered[len(returned):]]
        out["droppedCandidates"] = lost
        out["droppedCandidateCount"] = len(lost)
        out["candidatesTruncated"] = True
        tr = dict(out.get("truncation") or {})
        tr["droppedCandidateCount"] = len(lost)
        out["truncation"] = tr
        _prev = str(out.get("hint") or "")
        out["hint"] = (_prev + " " if _prev else "") + (
            "★ 候选集被字节上限截断：缺失 %d 个候选（%s）。契约 rankBasis ⑥ 要求"
            "「并列全部返回」，故本次返回**不完整**；请缩小症状范围或降低 maxHops 重试，"
            "不要据残缺候选集下「唯一根因」的结论。"
            % (len(lost), "、".join(lost))).strip()
    # ★ P3-fix（2026-10-15）：把**合法入参词表**随**成功**响应一起回传。
    #   实测动机（m3 中期信号）：收窄工具面后 agent 失去 cc_guide/cc_schema 这个
    #   「词表与参数枚举」来源，于是**猜**参数并烧轮次：
    #     · `未识别的症状/故障码 ['LATENCY_SPIKE']`（它自己编了个英文症状名）
    #     · `maxHops=5 超出因果链硬上限 4（不得截断，只能拒绝）`
    #     · `direction='downstream' 非法；取值域为 out|in|both`
    #   每猜错一次 400 ⇒ 重试 ⇒ 撞满 max_iteration ⇒ Dify 500、无最终答案。
    #   ⇒ 把这些**随成功响应**回传（体积很小），agent 一次就能看到，无需靠试错学契约。
    out["acceptedSymptomCodes"] = sorted(by_code)
    out["maxHopsLimit"] = MAX_CAUSE_DEPTH
    # ★ P3-fix2（2026-10-15）：把「建议动作」也**服务端串进来** —— 使 `cc_cause` 成为
    #   **一次调用即完整诊断**（候选 + 聚焦证据 + 建议）。
    #   参考项目 `onto_diagnose` 的 docstring 把这条讲透了（逐字）：
    #     「原设计把流程拆成 onto_guide → onto_collect → onto_rules → propose_actions 四步，
    #       全靠提示词约束模型逐步执行。**实测该约束不可靠**…"靠提示词让模型记住四步流程"
    #       是把确定性工作交给概率性执行者。本端点把四步收敛成**一次调用**。」
    #   实测佐证（本项目 m3）：即便给了 `focus[]`，agent 仍会在 20 轮预算里反复探索
    #   （Q 臂 avg position 20.8、85% 撞顶 ⇒ Dify 500、无最终答案）。
    #   ⇒ 与其靠提示词约束，不如让**服务端**把确定性部分一次做完。
    try:
        if ordered:
            _top = str(ordered[0].get("faultCode"))
            _pr = build_propose(backend, _top,
                                [str(x) for x in (ordered[0].get("path") or [])] or None)
            out["proposedActions"] = _pr.get("actions") or []
            out["proposedFor"] = _top
            out["executed"] = False
            out["dryRun"] = True
    except Exception as _exc:            # noqa: BLE001 —— **不静默**：失败也如实回传
        out["proposedActions"] = []
        out["proposeError"] = str(_exc)[:200]
    # ★ P3：并入**聚焦证据**（把「候选 → 佐证」收进同一工具）
    #   实测动机：Agent 为拿佐证会逐个对象类型穷举 `cc_facts`（17 类 ≈ 31,938 tokens/趟），
    #   累计 425k–510k tokens、撞满 max_iteration ⇒ 7–10/50 例无最终答案。
    try:
        _fe = focus_evidence(backend, ordered)
        out["focus"] = _fe["focus"]
        out["focusLedger"] = _fe["focusLedger"]
    except Exception as _exc:            # noqa: BLE001 —— **不静默**：失败也如实回传
        out["focus"] = []
        out["focusLedger"] = {"error": str(_exc)[:200], "focusInstances": 0}
    return out


def _trim_focus_attributes(attrs: Dict[str, Any], metric_codes) -> Dict[str, Any]:
    """聚焦属性的裁剪：只留**身份键** + **与聚焦指标对应者**。

    依据（实测）：单实例 `attributes` 12 个键里，`jvmVersion`/`startTime`/`pid`/
    `ccfSnapshotId` 等对根因判定无贡献；而 `heapUsedRatio`/`gcPauseMs` 恰好对应
    聚焦指标 `app.heap_used_ratio` / `app.gc_pause_ms`（末段 snake→camel 匹配）。
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


def focus_evidence(backend: Neo4jHttpBackend, ordered: Sequence[Dict[str, Any]],
                   values_per_path: int = 3, max_instances: int = 16) -> Dict[str, Any]:
    """★ P3：由**候选**推出**聚焦证据** —— 只取各候选判据涉及的指标。

    为什么并入 `cc_cause` 而不是新开工具：
      `cc_cause` 的契约本就是「根据症状码返回**候选根因及其路径**」；而 Agent 拿到候选后
      **必然**要去 `cc_facts` 取佐证 —— 实测它为此刻意逐个对象类型穷举 **17 类**
      （31,938 tokens/趟），累计 input tokens 425k–510k，撞满 `max_iteration=20`
      导致 7–10/50 例无最终答案。把「候选 → 佐证」这段收进**同一个工具**，是
      「工具面越窄，模型选错的概率越低」的直接落实，且**不新增 provider / 不动工具清单**。

    聚焦面**不需要新推导**：候选自带 `componentType` / `componentInstance`（根因落在哪个
    实例）与 `detectEvidence`（逐条款 `metricCode`）⇒
      **聚焦对象 = 候选的实例；聚焦指标 = 该候选判据涉及的那几条指标**（不是该对象全量事实）。
    """
    focus: Dict[Tuple[str, str], set] = {}
    for c in ordered:
        ct, ci = c.get("componentType"), c.get("componentInstance")
        if not ct or not ci:
            continue
        ms = {r.get("metricCode") for r in (c.get("detectEvidence") or []) if r.get("metricCode")}
        focus.setdefault((str(ct), str(ci)), set()).update(ms)

    led = {"focusInstances": 0, "factsTotal": 0, "factsKept": 0, "factsDropped": 0,
           "pathsReturned": 0, "valuesPerPath": values_per_path,
           "scopedBy": "cause.componentInstance", "instancesSkipped": 0}
    out: List[Dict[str, Any]] = []
    keys = sorted(focus.items())[:max_instances]
    led["instancesSkipped"] = max(0, len(focus) - len(keys))
    for (ct, ci), ms in keys:
        try:
            f = build_facts(backend, ct, ci, sorted(ms))
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
            "attributes": _trim_focus_attributes((objs[0].get("attributes") if objs else {}),
                                                 sorted(ms)),
            "observations": paths,
            "truncated": bool(f.get("truncated")),
        })
    return {"focus": out, "focusLedger": led}


def build_propose(backend: Neo4jHttpBackend, root_cause: str,
                  evidence_path: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """仅建议、绝不执行：executed 恒为 false，dryRun 恒为 true。"""
    fm_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode) RETURN f.fmCode AS fmCode, f.fmName AS fmName, "
        "f.fmComponentType AS fmComponentType, f.fmDescription AS fmDescription "
        "ORDER BY fmCode",
    )
    action_rows = _rows(
        backend,
        "MATCH (n) WHERE n.actionId IS NOT NULL "
        "RETURN n.actionId AS actionId, n.actionName AS actionName, "
        "n.actionRiskLevel AS actionRiskLevel, n.commandTemplate AS commandTemplate, "
        "n.approvalRequirement AS approvalRequirement, n.auditRequired AS auditRequired, "
        "n.reversible AS reversible, labels(n) AS labels ORDER BY actionId",
    )
    runbook_rows = _rows(
        backend,
        "MATCH (rb:Runbook)-[:RECOMMENDS]->(a) WHERE a.actionId IS NOT NULL "
        "RETURN rb.runbookId AS runbookId, rb.runbookTitle AS runbookTitle, "
        "rb.rootCauseType AS rootCauseType, a.actionId AS actionId "
        "ORDER BY runbookId, actionId",
    )
    affected_rows = _rows(
        backend,
        "MATCH (f:CC_FaultMode)-[:CC_AFFECTS]->(n) "
        "RETURN f.fmCode AS fmCode, %s AS objectId ORDER BY fmCode, objectId"
        % _id_expr("n"),
    )
    affected: Dict[str, List[str]] = {}
    for row in affected_rows:
        affected.setdefault(str(row.get("fmCode")), []).append(str(row.get("objectId")))

    needle = str(root_cause).strip()
    matched_fms: List[Dict[str, Any]] = []
    for row in fm_rows:
        code = str(row.get("fmCode"))
        name = str(row.get("fmName") or "")
        desc = str(row.get("fmDescription") or "")
        if needle == code or needle == name or (needle and (needle in name or needle in desc)):
            matched_fms.append(row)
    if not matched_fms:
        matched_fms = [row for row in fm_rows
                       if str(row.get("fmCode")) in needle or str(row.get("fmName") or "") in needle]

    matched_runbooks: List[Dict[str, Any]] = []
    for row in runbook_rows:
        rtype = str(row.get("rootCauseType") or "")
        title = str(row.get("runbookTitle") or "")
        hit = False
        if needle and (needle in rtype or needle in title or rtype in needle):
            hit = True
        for fm in matched_fms:
            name = str(fm.get("fmName") or "")
            if rtype and ((rtype in name) or (name and name in rtype)
                          or (rtype in title)):
                hit = True
        if hit:
            matched_runbooks.append(row)

    action_by_id = {str(r.get("actionId")): r for r in action_rows}
    actions: List[Dict[str, Any]] = []
    used: set = set()
    for row in matched_runbooks:
        action_id = str(row.get("actionId"))
        if action_id in used or action_id not in action_by_id:
            continue
        used.add(action_id)
        actions.append(_action_item(
            action_by_id[action_id],
            target=_propose_target(matched_fms, affected),
            reason="预案 %s《%s》（rootCauseType=%s）经 RECOMMENDS 推荐；"
                   "仅建议，不执行。" % (row.get("runbookId"), row.get("runbookTitle"),
                                        row.get("rootCauseType")),
        ))

    # 证据路径联动：把证据记录 id 反查到判据指标，再取该指标所属故障码的预案
    evidence_hits: List[str] = []
    if evidence_path:
        ev_rows = _rows(
            backend,
            "MATCH (e:CC_Evidence) WHERE e.recordId IN $ids "
            "RETURN e.recordId AS recordId, e.metricCode AS metricCode "
            "ORDER BY recordId",
            {"ids": list(evidence_path)},
        )
        metrics = sorted({str(r.get("metricCode")) for r in ev_rows})
        evidence_hits = sorted({str(r.get("recordId")) for r in ev_rows})
        if metrics:
            judged = _rows(
                backend,
                "MATCH (f:CC_FaultMode)-[:CC_JUDGED_BY]->(m:CC_Metric) "
                "WHERE m.metricCode IN $metrics RETURN DISTINCT f.fmCode AS fmCode "
                "ORDER BY fmCode",
                {"metrics": metrics},
            )
            for fm_row in judged:
                code = str(fm_row.get("fmCode"))
                for row in runbook_rows:
                    rtype = str(row.get("rootCauseType") or "")
                    name = str((fm_rows_by_code(fm_rows).get(code) or {}).get("fmName") or "")
                    if rtype and name and (rtype in name or name in rtype):
                        action_id = str(row.get("actionId"))
                        if action_id in used or action_id not in action_by_id:
                            continue
                        used.add(action_id)
                        actions.append(_action_item(
                            action_by_id[action_id],
                            target=_propose_target(
                                [fm_rows_by_code(fm_rows).get(code) or {}], affected),
                            reason="证据记录 %s 的指标命中故障码 %s 的判据，"
                                   "预案 %s 经 RECOMMENDS 推荐；仅建议，不执行。"
                                   % (",".join(evidence_hits), code, row.get("runbookId")),
                        ))

    fallback = False
    if not actions:
        fallback = True
        for row in action_rows:
            actions.append(_action_item(
                row, target="（未绑定具体实例）",
                reason="环境动作目录条目：未找到与根因 %r 直接关联的处置预案，"
                       "给出全量目录供人工筛选；仅建议，不执行。" % root_cause,
            ))

    ledger = Ledger(["actions"])
    ledger.put("actions", actions)
    ledger.put("executed", False)
    ledger.put("dryRun", True)
    hint = ""
    if not actions:
        hint = ("环境动作目录为空：快照里没有带 actionId 的动作对象；"
                "本工具只给建议，不会执行任何变更。")
    elif fallback:
        hint = ("未找到与根因 %r 直接关联的处置预案，已回落到环境动作目录全量条目（%d 条）；"
                "本工具只给建议，不会执行任何变更。" % (root_cause, len(actions)))
    final = ledger.finish(hint)
    final["executed"] = False
    final["dryRun"] = True
    return final


def fm_rows_by_code(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(r.get("fmCode")): r for r in rows}


def _propose_target(matched_fms: Sequence[Dict[str, Any]],
                    affected: Dict[str, List[str]]) -> str:
    ids: List[str] = []
    for fm in matched_fms:
        ids += affected.get(str(fm.get("fmCode")), [])
    ids = sorted(set(ids))
    if ids:
        return ",".join(ids[:5]) + ("" if len(ids) <= 5 else " …(+%d)" % (len(ids) - 5))
    return "（未绑定具体实例）"


def _action_item(row: Dict[str, Any], target: str, reason: str) -> Dict[str, Any]:
    return {
        "actionCode": row.get("actionId"),
        "target": target,
        "reason": reason,
        "actionName": row.get("actionName"),
        "actionRiskLevel": row.get("actionRiskLevel"),
        "approvalRequirement": row.get("approvalRequirement"),
        "auditRequired": row.get("auditRequired"),
        "reversible": row.get("reversible"),
        "commandTemplate": row.get("commandTemplate"),
        "executed": False,
        "dryRun": True,
    }


# ===========================================================================
# 8. 服务外观（依赖注入后端，便于离线测试）
# ===========================================================================
class CcToolsService:
    def __init__(self, backend: Neo4jHttpBackend) -> None:
        self.backend = backend

    def snapshot_id(self) -> str:
        try:
            rows = _rows(self.backend, "MATCH (m:CC_Meta) RETURN m.snapshotId AS snapshotId LIMIT 1")
            if rows and rows[0].get("snapshotId"):
                return str(rows[0]["snapshotId"])
        except Exception:
            pass
        return ""

    def dispatch(self, tool: str, body: Any) -> Dict[str, Any]:
        # ★ M3.6：请求 id / 发起时刻 / 后端累计量的**请求前**基线。
        request_id = new_request_id()
        started_at = now_iso()
        calls_before, total_before = backend_call_stats(self.backend)
        started = time.time()
        status = "ok"
        query_text = ""
        row_count = 0
        try:
            params = validate_params(tool, body)
            if tool == "cc_guide":
                out = build_guide(self.backend)
            elif tool == "cc_schema":
                out = build_schema(self.backend)
            elif tool == "cc_query":
                query_text = params["query"]
                out = build_query(self.backend, params["query"])
                row_count = int(out.get("rowCount") or 0)
            elif tool == "cc_facts":
                out = build_facts(self.backend, params["objectType"],
                                  params.get("objectId"), params.get("metricCodes"))
                row_count = len(out.get("objects") or []) + len(out.get("relations") or []) \
                    + len(out.get("observations") or [])
            elif tool == "cc_trace":
                out = build_trace(self.backend, params["startId"],
                                  params.get("direction"), params.get("maxHops"))
                row_count = len(out.get("nodes") or []) + len(out.get("edges") or [])
            elif tool == "cc_cause":
                out = build_cause(self.backend, params["symptomCodes"],
                                  params.get("maxHops"))
                row_count = len(out.get("candidates") or [])
            elif tool == "cc_propose":
                out = build_propose(self.backend, params["rootCause"],
                                    params.get("evidencePath"))
                row_count = len(out.get("actions") or [])
            elif tool == "read_facts":
                # ★ M5/S3a-1：中性取数面纯代理（信封与截断都由上游自带，见 build_read_facts）
                out = build_read_facts(params)
                row_count = (len(out.get("objects") or []) + len(out.get("relations") or [])
                             + len(out.get("observations") or []))
            else:                                     # pragma: no cover
                raise ToolError(ERR_INVALID_PARAM, "未知工具：%r" % (tool,))
        except ToolError as exc:
            status = exc.code
            self._emit_audit(request_id, started_at, started, tool, query_text, 0, status,
                             body, _byte_len(exc.payload(tool)), False,
                             calls_before, total_before)
            raise
        except GraphError as exc:
            status = "GRAPH_" + exc.kind
            self._emit_audit(request_id, started_at, started, tool, query_text, 0, status,
                             body, 0, False, calls_before, total_before)
            raise
        # ★ M3.5：对整个**最终响应对象**再施加一次字节上限（Ledger 度量发生在
        # finish/rowCount 追加之前，实测可超限至 20086 B）。
        # ★ M5/S3a-1：read_facts 是纯代理 —— 上游已按 max_records/max_bytes 截断并自带
        # 公共信封，本层**不得**二次截断（否则同一响应被截两次、信封被覆盖）。
        # ★ P1：元数据端点用独立上限（见 byte_cap_for 的说明）
        bounded = (out if tool == "read_facts"
                   else enforce_payload_budget(out, byte_cap=byte_cap_for(tool)))
        _trunc = bounded.get("truncation") or {}
        self._emit_audit(request_id, started_at, started, tool, query_text, row_count, "ok",
                         body, _byte_len(bounded), bool(bounded.get("truncated")),
                         calls_before, total_before,
                         original_bytes=int(_trunc.get("originalBytes") or 0),
                         dropped_entries=int(_trunc.get("droppedEntries")
                                             or bounded.get("droppedRows") or 0))
        return bounded

    def _emit_audit(self, request_id: str, started_at: str, started: float, tool: str,
                    query_text: str, row_count: int, status: str, body: Any,
                    result_bytes: int, truncated: bool,
                    calls_before: int, total_before: float,
                    original_bytes: int = 0, dropped_entries: int = 0) -> None:
        calls, total = backend_call_stats(self.backend)
        emit_audit(tool=tool, request_id=request_id, started_at=started_at,
                   elapsed_ms=(time.time() - started) * 1000.0,
                   query_latency_ms=max(0.0, total - total_before),
                   backend_calls=max(0, calls - calls_before),
                   query_text=query_text or getattr(self.backend, "last_query", ""),
                   row_count=row_count, status=status, params=body,
                   result_bytes=result_bytes, truncated=truncated,
                   original_bytes=original_bytes, dropped_entries=dropped_entries)


def build_backend_from_env(timeout: Optional[float] = None) -> Neo4jHttpBackend:
    base_url = os.environ.get("CC_NEO4J_HTTP_URL", "http://127.0.0.1:7475")
    user = os.environ.get("CC_NEO4J_USER", "neo4j")
    database = os.environ.get("CC_NEO4J_DATABASE", "neo4j")
    password = os.environ.get("CC_NEO4J_PASSWORD")
    if not password:
        env_file = os.environ.get("CC_NEO4J_ENV_FILE", "/opt/upstream-agent/.env")
        password = read_password_from_env_file(env_file)
    if not password:
        raise GraphError("config",
                         "缺少 CC_NEO4J_PASSWORD（也未能从 %s 解析 NEO4J_AUTH）；"
                         "本项目绝不回落成默认/匿名口令。"
                         % os.environ.get("CC_NEO4J_ENV_FILE",
                                          "/opt/upstream-agent/.env"))
    return Neo4jHttpBackend(base_url, user, password, database,
                            timeout=timeout if timeout is not None else QUERY_TIMEOUT_S)


def read_password_from_env_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("NEO4J_AUTH="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if "/" in value:
                        return value.split("/", 1)[1]
    except Exception:
        return ""
    return ""


# ===========================================================================
# 9. FastAPI 应用（惰性导入：本模块可无 fastapi 导入，便于离线测试）
# ===========================================================================
def create_app(service: CcToolsService):
    from fastapi import FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    try:                                              # pydantic v2
        from pydantic import ConfigDict

        class Base(BaseModel):                        # type: ignore[misc]
            model_config = ConfigDict(extra="forbid")
    except Exception:                                 # pragma: no cover - pydantic v1
        class Base(BaseModel):                        # type: ignore[no-redef]
            class Config:
                extra = "forbid"

    def _model(tool: str):
        spec = TOOLFACE[tool]
        if not spec["params"]:
            return type("%sIn" % tool, (Base,), {})
        annotations: Dict[str, Any] = {}
        defaults: Dict[str, Any] = {}
        for pspec in spec["params"]:
            name = pspec["name"]
            if pspec["type"] == "array":
                annotations[name] = List[str]
            elif pspec["type"] == "integer":
                annotations[name] = int
            elif pspec["type"] == "boolean":
                annotations[name] = bool
            elif pspec["type"] == "object":
                annotations[name] = Dict[str, Any]      # ★ M5/S3a-1：read_facts.scope
            else:
                annotations[name] = str
            if not pspec["required"]:
                annotations[name] = Optional[annotations[name]]
                defaults[name] = None
        namespace = {"__annotations__": annotations}
        namespace.update(defaults)
        return type("%sIn" % tool, (Base,), namespace)

    def _dump(model: Any) -> Dict[str, Any]:
        if model is None:
            return {}
        for attr in ("model_dump", "dict"):
            fn = getattr(model, attr, None)
            if callable(fn):
                return fn()
        return {}

    def _bounded(payload: Dict[str, Any]) -> Dict[str, Any]:
        # ★ M3.5：错误路径也要过字节上限（否则大 validationErrors 可无界）。
        return enforce_payload_budget(payload)

    app = FastAPI(
        title="Set B 七工具知识服务（方案1 侧）",
        version="1.0.0",
        description=("只读知识服务：工具名/参数/返回字段逐字取自 shared/model/tools-setb.yaml。"
                     "本服务绝不写入底层图存储，cc_propose 恒为只建议不执行。"),
    )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN001
        tool = _tool_of_path(request.url.path)
        err = ToolError(ERR_INVALID_PARAM,
                        "请求体不满足工具 %s 的参数契约（未识别字段或类型不符）不得静默回落默认值。"
                        % tool, validationErrors=_compact_errors(exc.errors()))
        return JSONResponse(status_code=400, content=_bounded(err.payload(tool)))

    @app.exception_handler(ToolError)
    async def _on_tool_error(request: Request, exc: ToolError):  # noqa: ANN001
        return JSONResponse(status_code=400,
                            content=_bounded(exc.payload(_tool_of_path(request.url.path))))

    @app.exception_handler(GraphError)
    async def _on_graph_error(request: Request, exc: GraphError):  # noqa: ANN001
        tool = _tool_of_path(request.url.path)
        if exc.kind == "timeout":
            err = ToolError(ERR_QUERY_TIMEOUT, exc.message)
            return JSONResponse(status_code=502, content=_bounded(err.payload(tool)))
        if exc.kind == "syntax":
            err = ToolError(ERR_QUERY_FORM_REJECTED, exc.message)
            return JSONResponse(status_code=400, content=_bounded(err.payload(tool)))
        # 基础设施故障（连接/配置/非契约类图错误）：不属于契约 8 个 error_codes 的射程，
        # 显式另标一个码，避免把基础设施故障伪装成契约违规。
        err = ToolError("BACKEND_UNAVAILABLE",
                        "图后端不可用（%s）：%s" % (exc.kind, exc.message))
        return JSONResponse(status_code=502, content=_bounded(err.payload(tool)))

    @app.get("/health")
    def health():
        snapshot = service.snapshot_id()
        return {
            "status": "ok" if snapshot else "degraded",
            "carrier": CARRIER,
            "arm": ARM,
            "snapshotId": snapshot,
            "backend": getattr(service.backend, "base_url", ""),
            "database": getattr(service.backend, "database", ""),
            "queryTimeoutS": getattr(service.backend, "timeout", QUERY_TIMEOUT_S),
            "rowCap": ROW_CAP,
            "byteCap": BYTE_CAP,
            "metadataByteCap": METADATA_BYTE_CAP,
            "metadataTools": list(METADATA_TOOLS),
            # ★ P2c：`cc_cause` 单列预算（契约 bounds.cause_byte_cap）—— 可观测、不静默
            "causeByteCap": CAUSE_BYTE_CAP,
            "maxCauseDepth": MAX_CAUSE_DEPTH,
            "tools": TOOL_NAMES,
            # ★ M3.6：载体侧纯查询延迟直方图 + 审计链快照（**只加键、不动既有键**，
            # 故 :8301 的路由集合与既有测试断言不变）。
            "queryLatency": latency_snapshot(),
            "auditChain": AUDIT_CHAIN.snapshot(tail=5),
            "auditFile": AUDIT_FILE,
        }

    for tool_name in TOOL_NAMES:
        spec = TOOLFACE[tool_name]
        model = _model(tool_name)
        endpoint = _make_endpoint(app, tool_name, spec, model, _dump, service)
        app.add_api_route(spec["path"], endpoint, methods=["POST"],
                          name=tool_name, summary=tool_name,
                          description=spec["description"])
    return app


def _make_endpoint(app, tool_name, spec, model, dump, service):
    def endpoint(payload=None):                       # noqa: ANN001
        return service.dispatch(tool_name, dump(payload))

    endpoint.__name__ = tool_name
    endpoint.__doc__ = spec["description"]
    if spec["params"]:
        endpoint.__annotations__ = {"payload": model, "return": dict}
    else:
        endpoint.__annotations__ = {"payload": Optional[model], "return": dict}
    return endpoint


def _tool_of_path(path: str) -> str:
    for tool in TOOL_NAMES:
        if TOOLFACE[tool]["path"] == path:
            return tool
    return "(unknown)"


def _compact_errors(errors: Sequence[Any]) -> List[Dict[str, Any]]:
    out = []
    for item in errors or []:
        if isinstance(item, dict):
            out.append({"loc": [str(x) for x in item.get("loc") or []],
                        "type": str(item.get("type") or ""),
                        "msg": str(item.get("msg") or "")})
    return out


# ===========================================================================
# 10. 清单产出（由实现自身产出，供 G24 读取）
# ===========================================================================
def repo_root_default() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    probe = here
    for _ in range(6):
        probe = os.path.dirname(probe)
        if not probe or probe == os.path.dirname(probe):
            break
        if os.path.isfile(os.path.join(probe, "shared", "model", "tools-setb.yaml")):
            return probe
    return os.path.dirname(os.path.dirname(here))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def emit_manifest(repo_root: Optional[str] = None, emitted_at: Optional[str] = None) -> Dict[str, Any]:
    here = os.path.abspath(__file__)
    root = os.path.abspath(repo_root) if repo_root else repo_root_default()
    try:
        rel = os.path.relpath(here, root).replace(os.sep, "/")
    except Exception:
        rel = REPO_REL_SOURCE
    if rel.startswith(".."):
        rel = REPO_REL_SOURCE
    digest = sha256_file(here)
    tools = []
    for name in TOOL_NAMES:
        spec = TOOLFACE[name]
        returns = [{"name": n, "type": t} for n, t in spec["returns"]]
        returns += [{"name": n, "type": t} for n, t in ENVELOPE_FIELDS]
        tools.append({
            "name": name,
            "description": spec["description"],
            "params": [{"name": p["name"], "type": p["type"], "required": p["required"]}
                       for p in spec["params"]],
            "returns": returns,
            "source": rel,
            "sourceSha256": digest,
        })
    manifest: Dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "carrier": CARRIER,
        "arm": ARM,
        "tool": rel,
        "source": rel,
        "sourceSha256": digest,
        "sources": [{"path": rel, "sha256": digest}],
        "tools": tools,
    }
    if emitted_at:
        manifest["emittedAt"] = emitted_at
    return manifest


def write_manifest(out_path: str, repo_root: Optional[str] = None) -> Dict[str, Any]:
    manifest = emit_manifest(repo_root, emitted_at=time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    return manifest


# ===========================================================================
# 11. CLI
# ===========================================================================
def selftest() -> int:
    """离线自检：守卫 + 排序 + 形状（不连数据库）。"""
    checks: List[Tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    def rejected(query: str, code: str) -> Tuple[bool, str]:
        try:
            guard_query(query)
            return False, "未被拒绝"
        except ToolError as exc:
            return exc.code == code, exc.code

    ok, code = rejected("MATCH (n) SET n.x = 1 RETURN n", ERR_FORBIDDEN_TOKEN)
    check("guard/write-token", ok, code)
    ok, code = rejected("MATCH (n) RETURN n; MATCH (m) RETURN m; RETURN 1",
                        ERR_MULTI_STATEMENT_REJECTED)
    check("guard/multi-statement", ok, code)
    ok, code = rejected("MATCH (n) CALL apoc.load.json('http://x') YIELD value RETURN value",
                        ERR_EXTERNAL_IO_FORBIDDEN)
    check("guard/external-io", ok, code)
    ok, code = rejected("SELECT * WHERE { ?s ?p ?o }", ERR_QUERY_FORM_REJECTED)
    check("guard/form", ok, code)
    ok, code = rejected("MATCH p=(a:CC_FaultMode)-[:CC_CAUSES*1..5]->(b) RETURN b",
                        ERR_DEPTH_LIMIT_EXCEEDED)
    check("guard/depth", ok, code)
    ok, code = rejected("MATCH p=(a:CC_FaultMode)-[:CC_CAUSES*]->(b) RETURN b",
                        ERR_DEPTH_LIMIT_EXCEEDED)
    check("guard/unbounded-path", ok, code)
    bounded, injected = apply_force_limit("MATCH (n) RETURN n")
    check("guard/force-limit", injected and bounded.endswith("LIMIT 201"), bounded)
    check("guard/force-limit-skips-aggregation",
          apply_force_limit("MATCH (n) RETURN count(n)")[1] is False)
    counted = count_semicolons("MATCH (n) RETURN n;")
    check("guard/semicolon-count", counted == 1, str(counted))

    ranked = rank_candidates([
        {"faultCode": "B", "satisfiedClauses": 1, "determined": True, "hops": 2,
         "relationPriority": "causes"},
        {"faultCode": "A", "satisfiedClauses": 1, "determined": True, "hops": 2,
         "relationPriority": "causes"},
        {"faultCode": "C", "satisfiedClauses": 2, "determined": True, "hops": 3,
         "relationPriority": "causes"},
        {"faultCode": "D", "satisfiedClauses": 0, "determined": False, "hops": 1,
         "relationPriority": "causes"},
    ])
    check("rank/order", [c["faultCode"] for c in ranked] == ["C", "A", "B", "D"],
          str([c["faultCode"] for c in ranked]))
    check("rank/ties-not-merged", len(ranked) == 4, str(len(ranked)))

    clause = {"metricCode": "m", "op": "increased", "role": "necessary",
              "operandKind": "predicate"}
    rec = evaluate_clause(clause, {}, {}, {}, "obj")
    check("detect/undetermined-not-false", rec["outcome"] is None and bool(rec["reason"]),
          str(rec.get("reason")))

    try:
        validate_params("cc_facts", {"objectType": "Host", "bogus": 1})
        check("params/unknown-rejected", False, "未被拒绝")
    except ToolError as exc:
        check("params/unknown-rejected", exc.code == ERR_INVALID_PARAM, exc.code)
    try:
        validate_params("cc_propose", {"rootCause": "x", "dryRun": False})
        check("propose/dry-run-only", False, "未被拒绝")
    except ToolError as exc:
        check("propose/dry-run-only", exc.code == ERR_DRY_RUN_ONLY, exc.code)

    manifest = emit_manifest()
    names = [t["name"] for t in manifest["tools"]]
    env_ok = all({"hint", "truncated", "droppedRows", "droppedBytes"} <=
                 {r["name"] for r in t["returns"]} for t in manifest["tools"])
    check("manifest/tools", names == TOOL_NAMES, str(names))
    check("manifest/envelope-in-every-tool", env_ok)
    check("manifest/schema", manifest["schema"] == MANIFEST_SCHEMA)
    check("manifest/carrier-arm", manifest["carrier"] == CARRIER and manifest["arm"] == ARM)
    check("manifest/sha-matches-file", manifest["sourceSha256"] == sha256_file(os.path.abspath(__file__)))
    check("params/total-16", sum(len(TOOLFACE[t]["params"]) for t in TOOL_NAMES) == 16)
    check("returns/tool-level-24",
          sum(len(TOOLFACE[t]["returns"]) for t in TOOL_NAMES) == 24)
    # ★ M5/S3a-1：read_facts 的载体面（不连网：只核对声明面）
    check("read-facts/declared-params",
          [(p["name"], p["type"], p["required"]) for p in TOOLFACE["read_facts"]["params"]]
          == [("layers", "array", False), ("sections", "array", False),
              ("scope", "string", False), ("max_records", "integer", False),
              ("max_bytes", "integer", False)])
    check("read-facts/declared-returns",
          [r[0] for r in TOOLFACE["read_facts"]["returns"]]
          == ["objects", "relations", "observations", "source"])
    check("read-facts/description-sha256",
          hashlib.sha256(TOOLFACE["read_facts"]["description"].encode("utf-8")).hexdigest()
          == READ_FACTS_DESCRIPTION_SHA256)
    _shape_file = os.path.join(repo_root_default(), "shared", "model",
                               "live-facts-tools.yaml")
    if os.path.isfile(_shape_file):
        try:
            import yaml as _yaml
            with open(_shape_file, encoding="utf-8") as _fh:
                _shape_desc = _yaml.safe_load(_fh)["read_facts"]["description"]
            check("read-facts/description==shape-authority",
                  _shape_desc.rstrip("\n") == TOOLFACE["read_facts"]["description"])
        except Exception as _exc:                      # noqa: BLE001
            check("read-facts/shape-authority-readable", False,
                  "%s: %s" % (type(_exc).__name__, _exc))
    else:
        check("read-facts/shape-authority-present", True, "不在本机 ⇒ 跳过同源核对")

    bad = 0
    for name, passed, detail in checks:
        print("%-38s %s %s" % (name, "PASS" if passed else "FAIL", detail if not passed else ""))
        if not passed:
            bad += 1
    print("selftest: %d/%d PASS" % (len(checks) - bad, len(checks)))
    print("RESULT: %s" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        prog="cc_tools.py",
        description="Set B 七工具知识服务（方案1 侧）；退出码 0=成功 1=失败 2=参数/配置错误")
    ap.add_argument("--serve", action="store_true", help="启动 HTTP 服务")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8301)
    ap.add_argument("--emit-manifest", default=None, metavar="OUT.json")
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--call", default=None, metavar="TOOL")
    ap.add_argument("--args", default="{}", metavar="JSON")
    ap.add_argument("--timeout", type=float, default=None,
                    help="查询超时秒数（默认 4.0；仅供测试，生产必须用契约值）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.emit_manifest:
        manifest = write_manifest(args.emit_manifest, args.repo_root)
        print("manifest: %s" % os.path.abspath(args.emit_manifest))
        print("  schema=%s carrier=%s arm=%s tools=%d"
              % (manifest["schema"], manifest["carrier"], manifest["arm"],
                 len(manifest["tools"])))
        print("  source=%s sha256=%s" % (manifest["source"], manifest["sourceSha256"]))
        return 0
    if args.call:
        try:
            body = json.loads(args.args)
        except Exception as exc:
            print("--args 不是合法 JSON：%s" % exc, file=sys.stderr)
            return 2
        try:
            backend = build_backend_from_env(timeout=args.timeout)
        except GraphError as exc:
            print("配置错误：%s" % exc.message, file=sys.stderr)
            return 2
        service = CcToolsService(backend)
        try:
            out = service.dispatch(args.call, body)
        except ToolError as exc:
            print(json.dumps(exc.payload(args.call), ensure_ascii=False, indent=2))
            return 1
        except GraphError as exc:
            print("图后端错误（%s）：%s" % (exc.kind, exc.message), file=sys.stderr)
            return 1
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.serve:
        try:
            import uvicorn
        except Exception as exc:
            print("缺少 uvicorn：%s" % exc, file=sys.stderr)
            return 2
        try:
            backend = build_backend_from_env(timeout=args.timeout)
        except GraphError as exc:
            print("配置错误：%s" % exc.message, file=sys.stderr)
            return 2
        app = create_app(CcToolsService(backend))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    ap.print_help()
    return 0


# ===========================================================================
# ★ R-56/B（2026-10-12）：read_facts 的**上游错误面透传**
#
# 背景（真机证据 = Dify `message_agent_thoughts`）：`read_facts` 是纯代理，但原实现对上游
# **任何**非 200 都报 `UPSTREAM_UNAVAILABLE`（"中性取数面上游返回 HTTP 409"）。模型把
# `layers` 传成 `["app"]` 时上游（:8304）是**正确 fail-closed**（覆盖门禁 G5/X8），
# 模型收到的却是「上游不可用」⇒ 判定「采集接口不可用」并放弃取数。
#
# 本块做三件事（**不改任何成功路径、不改任何 HTTP 状态码**）：
#   ① 上游**业务码原样透传**（码集 = `live-facts-tools.yaml#read_facts.error_codes` 的键，
#      与 KG-T / ONTO 两侧必须逐字相同，否则四组工具面不一致）；
#   ② 错误信封的 `hint` 与码相符（原先对**所有**码都写「被只读守卫拒绝」，
#      对上游故障/上游 fail-closed 是不实的，会把调用方引向错误的修复方向）；
#   ③ 把透传码集登记进 `read_facts` 的 errorCodes（诚实面；errorCodes 不参与任何门禁比对）。
#
# ★ 位置纪律：本块**刻意插在 `if __name__ == "__main__"` 之前**（紧贴文件末尾），
#   以免平移既有的行号锚点（:57/:58/:96/:405/:439/:450/:1068/:2753 全部不动）。
# ===========================================================================
GUARD_HINT = "请求被只读守卫拒绝，未执行任何查询；请按 error.code 修正后重试。"
UPSTREAM_DOWN_HINT = (
    "中性取数面（:8304）不可达 / 超时 / 返回非法：这是**本载体之外**的上游故障，"
    "不是你入参的问题；可稍后重试，或改用其他工具取证。")
UPSTREAM_REFUSED_HINT = (
    "中性取数面**收下了请求但 fail-closed 拒绝执行**（原因见 error.message 与 "
    "error.detail.upstreamBody）—— 这**不是**「采集面不可用」。要收窄返回范围请用 scope 参数；"
    "运行模式下 layers 必须至少含 gaussdb 与 os。")
UPSTREAM_PARAM_HINT = (
    "中性取数面**拒绝了入参**（原因见 error.message 与 error.detail.upstreamBody）；"
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
        return code, ("中性取数面返回 HTTP %d（%s）：%s"
                      % (status, code, str(message or "")[:300]))
    return ERR_UPSTREAM_UNAVAILABLE, "中性取数面上游返回 HTTP %d。" % status


# 把透传码集登记进 read_facts 的 errorCodes（保持原有顺序，只追加缺失项）
READ_FACTS_ENTRY["errorCodes"] = list(READ_FACTS_ENTRY["errorCodes"]) + [
    _c for _c in READ_FACTS_UPSTREAM_CODES if _c not in READ_FACTS_ENTRY["errorCodes"]]


if __name__ == "__main__":
    sys.exit(main())
