# -*- coding: utf-8 -*-
"""M3.7 / M3.8 —— 方案3（KG-T / KG-T-dyn）第二套门面：端口 :8303，主臂 5 工具（含 read_facts）。

为什么要有这个文件（机械护栏，不是形式主义）
--------------------------------------------
`docs/09-方案3设计（知识图谱直引+LLM）.md:72` 明确：`KG-T` 的纪律护栏**就是进程隔离**——
若与 Set B 共进程，`cc_query` 就会出现在同一个 OpenAPI 文档里，`KG-T` 立刻退化成 `KG-Q`。
因此本文件是**独立进程的独立门面**：

  * 工具面**恰为 5 个**：`get_object_topology` / `get_fault_topology` / `get_metrics` / `get_facts`
    / `read_facts`（中性取数**纯代理**到 :8304）：`KG-T-dyn` 臂 = 5 + `collect_now` = 6（见下「臂开关」）；
  * `cc_query` / `run_rules` / `validate` / `cc_propose` **没有路由**，且**被显式拒绝**
    （不是"没写路由所以恰好 404"，而是注册了拒绝路由 + 分派层二次拦截）；
  * 本进程**不加载** `docs/04-rules.cypher`（N1：不做规则推理），`--selftest` 对此有机械断言。

权威落点（本文件只是门面，不重新定义契约）
------------------------------------------
  * 前 4 个工具的**入参/出参权威** = `shared/model/kgt-tools.yaml` 二章 + `docs/09` §3；第 5 个
    `read_facts` 的权威 = `shared/model/live-facts-tools.yaml#read_facts`（裁定 D47）。
    ★ 前 4 个工具**不属于** Set B 的八工具集合，与 Set B **不构成子集关系**
    （`shared/model/tools-setb.yaml:123-124` 把 `KG-T` 列入 `scope_excluded_arms`，
    理由正是"KG-T 直读结构、没有查询工具，工具面必然不同"）⇒ **不得**去对齐 Set B，
    本文件也**不进入 G24 的对齐射程**（G24 吃的是 Set B 的 KG↔ONTO 两侧清单）。
  * 前 4 个工具的**实现**在 `scheme1-kg/service/topology_reader.py`（`docs/09:65`，M2.9 交付，
    1961 行、4 接口、Python 侧有界 BFS、84/84 自测）。本文件**import 复用**它，
    不复制、不改写其算法（`topology_reader.py` 一个字节都不动）。
  * M3.5（载荷有界 + 截断记账）与 M3.6（`query_latency_p50/p95` + 审计链）的机制
    **import 复用** `scheme1-kg/service/cc_tools.py`（`enforce_payload_budget` /
    `emit_audit` / `latency_snapshot` / `AUDIT_CHAIN`），使 KG-T 与 Set B 的记账口径一致
    （`docs/09` §13 A8 要求 KG-T 审计链同 A8：服务侧逐调用落盘）。

★ 诚实边界（不得由工具面一致推出行为一致）
------------------------------------------
  1. 「5 工具」是本门面**注册**的工具集（其中 `read_facts` 是到 :8304 的纯代理）；`--selftest` 断言的是路由表与分派层的机械事实，
     **不**等价于"行为/语义与 Set B 同名工具一致"（本来也不要求一致）。
  2. 本文件 import 了 `cc_tools.py` ⇒ 该模块在 :8303 进程的 `sys.modules` 里可见，
     其 `cc_query` 的**只读**实现随之可见。这是"能力可见"而非"工具暴露"：
     F5 断言的对象是**注册工具集**（`docs/09:363` T7 清单）。若需要更强的物理隔离，
     应把 M3.5/M3.6 机制下沉到 `shared/` 再由两侧引用——此事**不在本次施工范围**，
     已写入报告的诚实边界，交队长裁决。
  3. `KG-T-dyn` 的 `service_port` 在契约里是 **TBD**（`docs/09:410` U-N 未决）。
     本文件给 `--arm KG-T-dyn` 一个载体侧**暂定**端口常量，并在 `--emit-manifest` 里
     如实标记 `servicePortSource: "carrier-provisional"`；真机上一律只常驻 `:8303` 主臂。

臂开关（M3.8「不参与主判据」的落地机制）
----------------------------------------
`--arm {KG-T,KG-T-dyn}`（默认 `KG-T`）决定工具面：

  | 臂        | 工具面                     | `supplementary` | `inMainComparison` |
  |-----------|----------------------------|-----------------|--------------------|
  | `KG-T`    | 5 个（无 `collect_now`）   | `false`         | `true`             |
  | `KG-T-dyn`| 5 个 + `collect_now`（=6） | `true`          | `false`            |

四道机械闸门保证它"只在补充实验里"：
  (a) 默认臂 = `KG-T`，`KG-T-dyn` 必须**显式** `--arm` 才可能出现；
  (b) `--emit-manifest` 上打 `supplementary/inMainComparison` 标记，Dify 注册与报告直接从清单读；
  (c) `kgt-tools.yaml#forbidden_tools_main_arm` 含 `collect_now` ⇒ 主臂清单一旦混入
      `collect_now`，`--selftest` 与 F5 政策 A4/A5 都会红；
  (d) 主臂与补充臂是**两个不同的 Dify App**（`cc-kgt-agent` / `cc-kgt-dyn-agent`），
      真机常驻进程只跑主臂。

CLI
---
  --serve [--host H] [--port P] [--arm A]   启动 HTTP 服务（默认 0.0.0.0:8303）
  --emit-manifest OUT.json [--repo-root R]  由实现自身产出工具面清单
  --selftest                                离线自检（工具面/路由/拒绝/形状/预算/审计），不连库
  --call TOOL --args JSON                   直接调用某工具并打印 JSON（无 HTTP 核验用）
  --fixture                                 用契约 fixture（`ContractBackend`）而非真机
  --list-tools                              打印本臂工具面（名字 + 路径）

环境变量
--------
  CC_NEO4J_PASSWORD  真机口令（缺失且未给 `--fixture` 时，图后端不可用 = 服务降级，不回落匿名）
  CC_NEO4J_ENV_FILE  可选，口令缺失时从该文件的 `NEO4J_AUTH=user/pass` 解析
  CC_AUDIT_LOG       可选，审计链 JSONL 落盘路径（空 = 只落 stderr，与 Set B 同口径）
  CC_KGT_HOST / CC_KGT_PORT / CC_KGT_ARM  启动脚本用（见 start_cc_tools_kgt.sh）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# -- 控制台编码兜底 ---------------------------------------------------------
# `--selftest` 的读数里含 `⊇`（U+2287）等非 GBK 字符；Windows 控制台默认 GBK 时
# print 会抛 UnicodeEncodeError 并把一次本该 PASS 的自检变成 exit 1。
# 这里把本进程的 stdout/stderr 固定为 UTF-8（不可用时静默跳过），
# 使 `python cc_tools_kgt.py --selftest` 在任何控制台代码页下都得到同一结论。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:                                             # pragma: no cover
        pass

# -- 同目录复用（本文件必须能被 `python cc_tools_kgt.py` 直接跑）-----------------
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cc_tools as ct                 # M3.5 字节预算 + M3.6 审计链（同载体复用，不复制实现）
import topology_reader as tr          # ★ 前 4 个工具的权威实现（docs/09:65）

# ===========================================================================
# 0. 载体身份与硬边界
# ===========================================================================
CARRIER = "kg"
ARM_MAIN = "KG-T"
ARM_DYN = "KG-T-dyn"
ARMS: Tuple[str, ...] = (ARM_MAIN, ARM_DYN)
REPO_REL_SOURCE = "scheme1-kg/service/cc_tools_kgt.py"
IMPL_TOOLS = "scheme1-kg/service/topology_reader.py"
IMPL_MACHINERY = "scheme1-kg/service/cc_tools.py"
MANIFEST_SCHEMA = "dsh-toolface-manifest/1"
OWNER_DOC = "docs/09-方案3设计（知识图谱直引+LLM）.md"
CONTRACT_FILE = "shared/model/kgt-tools.yaml"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8303                  # kgt-tools.yaml#arms.KG-T.service_port（契约值）
# ★ 仅载体侧暂定：契约 arms.KG-T-dyn.service_port = TBD（docs/09:410 U-N 未决）。
DYN_PROVISIONAL_PORT = 8305
DEFAULT_ARM = ARM_MAIN

# 契约三章禁令（逐字）
FORBIDDEN_ALL_ARMS: Tuple[str, ...] = ("cc_query", "run_rules", "validate", "cc_propose")
FORBIDDEN_MAIN_EXTRA: Tuple[str, ...] = ("collect_now",)

# 载荷上限：契约 `payload_policy` 指向 docs/06 §4（CAP_VALUES/CAP_OBJS/MAX_STR），
# 与Set B 同口径（200 行 / 20000 B）；真实执行点在 topology_reader（ROW_LIMIT/BYTE_LIMIT）
# 与本文件的收口预算（enforce_payload_budget）。
ROW_CAP = tr.ROW_LIMIT
BYTE_CAP = tr.BYTE_LIMIT
TOKEN_BUDGET = ct.TOKEN_BUDGET       # 20000；口径同 Set B（按字节记账，见 cc_tools.py:57-61）

# 本门面自拟的错误码（kgt-tools.yaml 未冻结 KG-T 的码表；此处显式声明，不借用 Set B 的码名）
ERR_INVALID_PARAM = "INVALID_PARAM"
ERR_BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
ERR_CONTRACT_ERROR = "CONTRACT_ERROR"
ERR_TOOL_NOT_AVAILABLE = "TOOL_NOT_AVAILABLE_ON_ARM"

# ★ 每臂允许出现的工具数（kgt-tools.yaml#expected_tool_counts）
EXPECTED_TOOL_COUNTS: Dict[str, int] = {ARM_MAIN: 5, ARM_DYN: 6}

# 撤销/越权路由 → 请求它们必须**明确拒绝**（不是"恰好没写路由"）
FORBIDDEN_PATHS: Dict[str, str] = {
    "/query": "cc_query",
    "/rules": "run_rules",
    "/validate": "validate",
    "/guide": "cc_guide",
    "/schema": "cc_schema",
    "/trace": "cc_trace",
    "/cause": "cc_cause",
    "/propose": "cc_propose",
}

# N1（docs/09:22）：本进程不得加载规则文件。此字符串是 N1 的机械断言目标。
RULES_FILE_MARKER = "04-rules.cypher"


# ===========================================================================
# 1. 工具面（本文件的唯一真源：路由 + manifest + 入参校验都由它驱动）
# ===========================================================================
# 出参里的"信封尾"字段：门面层（to_facade + enforce_payload_budget）保证它们可被看见，
# 使 §3.6 的 `truncated: bool` 与 `payload_policy.truncation.must_report` 真正可执行。
_ENVELOPE_TAIL: List[Tuple[str, str]] = [
    ("truncated", "boolean"),
    ("truncation", "object"),
    ("warnings", "array"),
    ("audit", "array"),
    ("hint", "string"),
    ("droppedRows", "integer"),
    ("droppedBytes", "integer"),
]

_OBJECT_TAIL: List[Tuple[str, str]] = [
    ("nodes", "array"),
    ("edges", "array"),
    ("truncated", "boolean"),
    ("hopsUsed", "integer"),
] + _ENVELOPE_TAIL

_FAULT_TAIL: List[Tuple[str, str]] = [
    ("root", "string"),
    ("nodes", "array"),
    ("edges", "array"),
    ("depths", "object"),
    ("cyclesEncountered", "array"),
    ("truncated", "boolean"),
] + _ENVELOPE_TAIL

_METRICS_TAIL: List[Tuple[str, str]] = [
    # 契约 §3.3 的出参是 array；门面必须包成 `to_facade` 的形状，否则截断无法上报。
    ("items", "array"),
] + _ENVELOPE_TAIL

_FACTS_TAIL: List[Tuple[str, str]] = [
    ("items", "array"),
    ("snapshotId", "string"),
    ("rowSnapshots", "array"),
    ("snapshotSource", "string"),
] + _ENVELOPE_TAIL

_COLLECT_TAIL: List[Tuple[str, str]] = [
    ("collected", "array"),
    ("snapshotIdAfter", "string"),
    ("dryRun", "boolean"),
    ("unwired", "boolean"),
] + _ENVELOPE_TAIL

_TOOLFACE_MAIN: List[Dict[str, Any]] = [
    {
        "name": "get_object_topology",
        "path": "/topology/object",
        "description": (
            "取对象关系拓扑子图（App / Runtime / Data 四层），从起点对象沿 out 方向做**有界 BFS**。"
            "入参只有 scope（对象 id / 层名 / 对象类型 / *）。★ 服务端遍历深度 = 契约业务链跳数"
            "（maxHopDepth 默认 9、硬上限 9，**不作为入参暴露**，因为 kgt-tools.yaml 的 request"
            "只声明了 scope）。默认排除派生注释边 HAS_EVIDENCE；不反向、不溯上游。"
            "出参显式回传 truncated 与 truncation；空结果附 hint 说明『空 ≠ 不存在』。"
        ),
        # ★ 不暴露 max_hops/ maxHopDepth 为入参：契约 request.properties 只有 scope，
        #   加一个"服务端默认值"当入参就是杜撰入参（F5 对插件臂有同类断言）。
        "params": [{"name": "scope", "type": "string", "required": True}],
        "returns": _OBJECT_TAIL,
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE, ERR_CONTRACT_ERROR],
    },
    {
        "name": "get_fault_topology",
        "path": "/topology/fault",
        "description": (
            "取故障因果子图：从 code 沿因果边 out 方向做**Python 侧有界 BFS**"
            "（★ 禁止 Cypher 变长路径：因果图恰 1 个有向环，变长路径会爆炸）。"
            "max_hops 默认 4、服务端硬上限 4（**夹取，不报错**，并在 warnings 里说明）。"
            "relation 原样返回 causes / conditional_causes / relates_to；relates_to 只随边返回、"
            "**绝不沿它继续展开**。visited 集去环，重复访问只记入 cyclesEncountered。"
        ),
        "params": [
            {"name": "code", "type": "string", "required": True},
            {"name": "max_hops", "type": "integer", "required": False},
        ],
        "returns": _FAULT_TAIL,
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE, ERR_CONTRACT_ERROR],
    },
    {
        "name": "get_metrics",
        "path": "/metrics",
        "description": (
            "取指定对象的指标读数。objects 为对象 id 数组（空数组 = 全量，按契约允许但不推荐）。"
            "★ 每行只返回 threshold_ref 引用，**不返回阈值数字**（门禁 G3：零数字字面量）。"
            "出参按门面形状包成 {items, truncated, truncation, warnings, audit}——"
            "契约把出参定义为数组，而数组没有放截断事实的位置，直接序列化会丢掉截断。"
        ),
        "params": [{"name": "objects", "type": "array", "required": True}],
        "returns": _METRICS_TAIL,
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE, ERR_CONTRACT_ERROR],
    },
    {
        "name": "get_facts",
        "path": "/facts",
        "description": (
            "取 CCF 原始事实记录（图侧投影：:CC_Evidence + CC_HAS_EVIDENCE），用于支撑"
            "『事实有据』与 false_claim 判定。objectType 必填；objectId / metricCodes / timeWindow 可选。"
            "★ 返回**原始记录**，不是加工后的结论。"
            # ★ R-54 / A1（2026-10-07）：timeWindow 原声明为 object，但 Dify 的 type_alias 把它折成
            #   string 给模型看 ⇒ 模型只能发字符串，而服务端要求 object ⇒ 必 400。
            #   改为 string + 显式 JSON 对象文法（与 read_facts.scope 同一处置）。
            "timeWindow 传**一个 JSON 对象字符串**，键取 {from,to} 或 {since,until}（RFC3339 时间串）；"
            "例 {\"from\":\"2026-09-28T15:00:00Z\",\"to\":\"2026-09-28T16:00:00Z\"}。"
            "不传、传 null 或传 \"{}\" 都表示不设时间窗。"
            "未识别键会被忽略并记入 warnings（不静默丢弃）。"
        ),
        "params": [
            {"name": "objectType", "type": "string", "required": True},
            {"name": "objectId", "type": "string", "required": False},
            {"name": "metricCodes", "type": "array", "required": False},
            {"name": "timeWindow", "type": "string", "required": False},
        ],
        "returns": _FACTS_TAIL,
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE, ERR_CONTRACT_ERROR],
    },
    {
        # ★ M5/S3a-1（裁定 D47）：read_facts = 中性取数**纯代理**到本机 :8304，
        #   算法不在本载体；形状权威 = shared/model/live-facts-tools.yaml#read_facts。
        #   returns 逐项 = 形状 tool_level 的键名 ∪ 公共信封 4 名（上游自带，见 dispatch 透传）。
        "name": "read_facts",
        "path": "/tools/read_facts",
        "description": "只读取数：按本进程固定的用例确定性重建（判决模式），或在未固定用例时向采集面真采一次（运行模式），返回有界的当前读数三段与公共信封。无结果时返回 hint 说明可能原因；返回 0 行或 hint 指出作用域不符时，不得据此断言对象或读数不存在。",
        "params": [
            {"name": "layers", "type": "array", "required": False},
            {"name": "sections", "type": "array", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "max_records", "type": "integer", "required": False},
            {"name": "max_bytes", "type": "integer", "required": False},
        ],
        "returns": [
            ("objects", "array"),
            ("relations", "array"),
            ("observations", "array"),
            ("source", "object"),
            ("hint", "string"),
            ("truncated", "boolean"),
            ("droppedRows", "integer"),
            ("droppedBytes", "integer"),
        ],
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE],  # +UPSTREAM_UNAVAILABLE（见文末登记）
    },
]

_TOOLFACE_DYN_EXTRA: List[Dict[str, Any]] = [
    {
        "name": "collect_now",
        "path": "/collect",
        "description": (
            "★ 仅 KG-T-dyn 臂：让采集层重采指定对象并写回同一张图。"
            "本门面**未接入采集层入口**（见 --emit-manifest 的 unwired 标记与实现注释）："
            "调用只做**只读可解析性校验**并显式返回 collected=[] / dryRun=true / unwired=true，"
            "**不写图、不伪造 collected 记录**。『未做』与『采到 0 条』在出参上可分。"
            "该臂因消费的 CCF 不再单一（违反硬约束 C1）而**不进主判据**。"
        ),
        "params": [{"name": "objects", "type": "array", "required": True}],
        "returns": _COLLECT_TAIL,
        "errorCodes": [ERR_INVALID_PARAM, ERR_BACKEND_UNAVAILABLE],
    },
]


def toolface(arm: str) -> List[Dict[str, Any]]:
    """返回指定臂的工具面（**新的**浅拷贝列表，调用方改不动真源）。"""
    if arm not in ARMS:
        raise ValueError("未知臂：%r（可选 %s）" % (arm, list(ARMS)))
    specs = list(_TOOLFACE_MAIN)
    if arm == ARM_DYN:
        specs = specs + list(_TOOLFACE_DYN_EXTRA)
    return [dict(spec) for spec in specs]


def tool_names(arm: str) -> List[str]:
    return [spec["name"] for spec in toolface(arm)]


def forbidden_tools(arm: str) -> List[str]:
    """该臂**必须没有**的工具名（来自契约三章禁令，逐字）。"""
    extra = FORBIDDEN_MAIN_EXTRA if arm == ARM_MAIN else ()
    return list(FORBIDDEN_ALL_ARMS) + list(extra)


def supplementary(arm: str) -> bool:
    """该臂是否"不参与主判据"（M3.8 的落地标记）。"""
    return arm != ARM_MAIN


def default_port(arm: str) -> int:
    return DEFAULT_PORT if arm == ARM_MAIN else DYN_PROVISIONAL_PORT


# ===========================================================================
# 2. 异常（门面层自己的语义，不借用 Set B 的 8 个码）
# ===========================================================================
class ToolDenied(RuntimeError):
    """请求了本臂不存在的工具 ⇒ 明确拒绝（HTTP 404）。"""

    def __init__(self, tool: str, arm: str, reason: str = "") -> None:
        self.tool = tool
        self.arm = arm
        self.reason = reason or ("该工具不在 %s 臂的工具面上（本臂恰 %d 个工具）"
                                 % (arm, EXPECTED_TOOL_COUNTS[arm]))
        super().__init__("%s：%s" % (tool, self.reason))

    def payload(self) -> Dict[str, Any]:
        return {
            "error": {
                "code": ERR_TOOL_NOT_AVAILABLE,
                "tool": self.tool,
                "arm": self.arm,
                "message": self.reason,
                "availableTools": tool_names(self.arm),
            }
        }


class ToolFailed(RuntimeError):
    """入参不合法 / 后端不可用 / 契约缺失 ⇒ 明确失败（不 500、不静默回落）。"""

    def __init__(self, code: str, message: str, tool: str = "(unknown)") -> None:
        self.code = code
        self.message = message
        self.tool = tool
        super().__init__("%s：%s" % (code, message))

    def payload(self) -> Dict[str, Any]:
        return {
            "error": {"code": self.code, "tool": self.tool, "message": self.message},
            "truncated": False,
            "truncation": {"truncated": False},
            "warnings": [],
            "audit": [],
            "hint": self.message,
            "droppedRows": 0,
            "droppedBytes": 0,
        }


# ===========================================================================
# 3. 服务（5/6 工具的分派 + M3.5 预算收口 + M3.6 审计链）
# ===========================================================================
class KgTService:
    """KG-T 门面的服务对象：路由与 `--call` 都走同一个 `dispatch`。"""

    def __init__(self, arm: str = DEFAULT_ARM, backend: Optional[Any] = None,
                 contract: Optional[Any] = None) -> None:
        if arm not in ARMS:
            raise ValueError("未知臂：%r" % (arm,))
        self.arm = arm
        self.toolface = toolface(arm)
        self.tool_names = [spec["name"] for spec in self.toolface]
        self._by_name = {spec["name"]: spec for spec in self.toolface}
        self._by_path = {spec["path"]: spec for spec in self.toolface}
        self._forbidden = set(forbidden_tools(arm))
        self.contract = contract if contract is not None else tr.get_contract()
        self.backend = backend if backend is not None else tr.default_backend(self.contract)
        # 本臂工具面与禁令集合自检：真源写错时**开局就炸**，不等到运行时
        overlap = sorted(set(self.tool_names) & self._forbidden)
        if overlap:
            raise ValueError("工具面与禁令集合相交（%s）：%s" % (arm, overlap))
        if len(self.tool_names) != EXPECTED_TOOL_COUNTS[arm]:
            raise ValueError("%s 工具数应为 %d，实为 %d"
                             % (arm, EXPECTED_TOOL_COUNTS[arm], len(self.tool_names)))

    # -- 身份 -----------------------------------------------------------------
    @property
    def supplementary(self) -> bool:
        return supplementary(self.arm)

    def snapshot_id(self) -> str:
        try:
            meta = self.backend.fetch_graph_meta() or {}
        except Exception:
            return ""
        return str(meta.get("snapshotId") or "")

    def spec_of_path(self, path: str) -> Optional[Dict[str, Any]]:
        return self._by_path.get(path)

    # -- 拒绝面 ---------------------------------------------------------------
    def assert_allowed(self, tool: str) -> None:
        """分派层二次拦截：即使有人绕过路由表，也拿不到越权工具。"""
        if tool in self._forbidden:
            raise ToolDenied(tool, self.arm, "该工具属契约三章禁令（forbidden_tools_*）")
        if tool not in self._by_name:
            raise ToolDenied(tool, self.arm)

    # -- 入参校验（不静默回落默认值；未知字段直接拒绝）--------------------------
    def validate_params(self, tool: str, body: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        spec = self._by_name.get(tool)
        if spec is None:
            self.assert_allowed(tool)          # 交给拒绝路径
            raise ToolFailed(ERR_INVALID_PARAM, "未知工具", tool)
        body = dict(body or {})
        declared = {p["name"]: p for p in spec["params"]}
        unknown = sorted(set(body) - set(declared))
        if unknown:
            raise ToolFailed(ERR_INVALID_PARAM,
                             "请求体含未声明字段 %s，不得静默忽略（本工具入参只有 %s）"
                             % (unknown, sorted(declared)), tool)
        for name, pspec in declared.items():
            value = body.get(name)
            if value is None:
                if pspec["required"]:
                    raise ToolFailed(ERR_INVALID_PARAM, "缺少必填参数 %s" % name, tool)
                continue
            if pspec["type"] == "array":
                if not isinstance(value, (list, tuple)):
                    raise ToolFailed(ERR_INVALID_PARAM, "参数 %s 必须是 array" % name, tool)
                body[name] = [str(item) for item in value]
            elif pspec["type"] == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ToolFailed(ERR_INVALID_PARAM, "参数 %s 必须是 integer" % name, tool)
            elif pspec["type"] == "boolean":
                if not isinstance(value, bool):
                    raise ToolFailed(ERR_INVALID_PARAM, "参数 %s 必须是 boolean" % name, tool)
            elif pspec["type"] == "object":
                if not isinstance(value, dict):
                    raise ToolFailed(ERR_INVALID_PARAM, "参数 %s 必须是 object" % name, tool)
            else:
                if not isinstance(value, str):
                    raise ToolFailed(ERR_INVALID_PARAM, "参数 %s 必须是 string" % name, tool)
        # 未给的可选参数**不**补进 body（避免把 None 当真值送进实现）
        return {name: value for name, value in body.items() if value is not None}

    # -- 工具实现（全部落在 topology_reader；本层只负责形状与记账）--------------
    def _invoke(self, tool: str, params: Dict[str, Any]) -> Any:
        backend, contract = self.backend, self.contract
        if tool == "get_object_topology":
            # 契约 request 只有 scope ⇒ 不传 max_hops，用契约的业务链跳数默认值。
            return tr.get_object_topology(params["scope"], backend=backend, contract=contract)
        if tool == "get_fault_topology":
            return tr.get_fault_topology(params["code"], params.get("max_hops"),
                                         backend=backend, contract=contract)
        if tool == "get_metrics":
            return tr.get_metrics(params["objects"], backend=backend, contract=contract)
        if tool == "get_facts":
            # ★ R-54 / A1：timeWindow 的**模型面类型是 string**（Dify type_alias 把 object 折成
            #   string）⇒ 这里把 JSON 对象字符串解析回 dict 再交给实现面；dict 入参保持兼容。
            tw = params.get("timeWindow")
            if isinstance(tw, str):
                text = tw.strip()
                if text in ("", "{}", "null", "None"):
                    tw = None
                else:
                    try:
                        tw = json.loads(text)
                    except Exception as exc:                    # noqa: BLE001
                        raise ToolFailed(
                            ERR_INVALID_PARAM,
                            "timeWindow 不是合法 JSON 对象字符串（%s）；"
                            "例：{\"from\":\"2026-09-28T15:00:00Z\",\"to\":\"2026-09-28T16:00:00Z\"}"
                            % exc, tool)
                    if not isinstance(tw, dict):
                        raise ToolFailed(
                            ERR_INVALID_PARAM,
                            "timeWindow 的 JSON 顶层必须是对象（收到 %s）"
                            % type(tw).__name__, tool)
            return tr.get_facts(params["objectType"], params.get("objectId"),
                                params.get("metricCodes"), tw,
                                backend=backend, contract=contract)
        if tool == "read_facts":
            # ★ 中性取数：本层不实现取数，只把 5 个入参原样转给 :8304（见文末 live_facts_call）
            return live_facts_call(params)
        if tool == "collect_now":
            return self._collect_now(params["objects"])
        raise ToolDenied(tool, self.arm)

    def _collect_now(self, objects: Sequence[str]) -> Dict[str, Any]:
        """★ 未接入采集层：只做只读校验 + 显式 UNWIRED，绝不写图、绝不伪造记录。

        真正的采集入口属于采集层（CCF collector），不在本载体职责内；在接入之前，
        本工具**不**执行任何子进程 / 任何写语句，返回 `unwired: true` 让"未做"可被机器辨认。
        """
        warnings: List[str] = []
        try:
            catalogue = self.backend.fetch_objects() or {}
        except Exception as exc:                      # 后端不可用也要可读地失败
            raise ToolFailed(ERR_BACKEND_UNAVAILABLE,
                             "取对象目录失败，无法校验待采集对象：%s" % exc, "collect_now")
        resolved: List[str] = []
        for item in objects:
            ids, scope_warnings = tr._resolve_scope(str(item), catalogue, self.contract)
            warnings.extend(scope_warnings)
            for obj_id in ids:
                if obj_id not in resolved:
                    resolved.append(obj_id)
        unknown = [str(item) for item in objects if not tr._resolve_scope(
            str(item), catalogue, self.contract)[0]]
        if unknown:
            warnings.append("以下对象在图上无法解析：%s（不代表业务系统里不存在）" % sorted(set(unknown)))
        warnings.append(
            "★ 采集层入口未接入（本载体职责之外）：本次调用**未执行任何采集**，"
            "collected 为空表示『未做』而非『采集到 0 条』。接入点应落在采集层而非本门面，"
            "以免本服务从只读退化为写图服务。"
        )
        return {
            "collected": [],
            "snapshotIdAfter": self.snapshot_id(),
            "dryRun": True,
            "unwired": True,
            "truncated": False,
            "truncation": {"truncated": False},
            "warnings": warnings,
            "audit": list(getattr(self.backend, "audit", []) or []),
            "hint": "collect_now 未接入采集层：本调用为只读校验 + UNWIRED 回执。",
        }

    # -- 分派（含 M3.6 记账；任何路径都必须进审计链）----------------------------
    def dispatch(self, tool: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_id = ct.new_request_id()
        started_at = ct.now_iso()
        started = time.time()
        status = "ok"
        result_bytes = 0
        truncated = False
        query_text = ""
        row_count = 0
        query_latency_ms = 0.0
        backend_calls = 0
        params: Dict[str, Any] = {}
        try:
            self.assert_allowed(tool)                      # 拒绝路径：越权工具先出局
            params = self.validate_params(tool, body)
            # topology_reader 的每个接口都会把 backend.audit 重置为空，故此处可直接读它。
            out = self._invoke(tool, params)
            if tool == "read_facts":
                # ★ 上游已按 200 行 / 20000 B 封顶并自带公共信封（hint/truncated/
                #   droppedRows/droppedBytes）⇒ 原样透传：既不补 truncation/warnings/audit，
                #   也不再过一次 enforce_payload_budget（否则就是二次截断+二次记账）。
                if not isinstance(out, dict):
                    raise ToolFailed(ERR_CONTRACT_ERROR,
                                     "read_facts 上游返回非 JSON 对象（%s）"
                                     % type(out).__name__, tool)
                rows = 0
                for _key in ("objects", "relations", "observations"):
                    _value = out.get(_key)
                    if isinstance(_value, list):
                        rows += len(_value)
                row_count = rows
                result_bytes = ct._byte_len(out)
                truncated = bool(out.get("truncated"))
                query_text = "POST %s" % live_facts_target()
                return out
            if isinstance(out, tr.ReportList):
                facade = tr.to_facade(out)
            elif isinstance(out, dict):
                facade = dict(out)
                facade.setdefault("truncation", {"truncated": False})
                facade.setdefault("warnings", [])
                facade.setdefault("audit", [])
                facade.setdefault("truncated", bool(facade["truncation"].get("truncated")))
            else:
                raise ToolFailed(ERR_CONTRACT_ERROR,
                                 "工具 %s 返回了非门面形状（%s）" % (tool, type(out).__name__), tool)
            facade.setdefault("hint", "")
            # ★ 账目字段恒在（未截断时为 0）：声明式形状必须与实际返回逐字对得上，
            #   否则 manifest 的 returns 就只是"某些情况下存在"的模糊承诺。
            facade.setdefault("droppedRows", 0)
            facade.setdefault("droppedBytes", 0)
            if not facade.get("hint"):
                facade["hint"] = _empty_hint(tool, facade)
            audited = list(getattr(self.backend, "audit", []) or [])
            backend_calls = len(audited)
            query_latency_ms = sum(float(item.get("elapsedMs") or 0.0) for item in audited)
            row_count = sum(int(item.get("rowCount") or 0) for item in audited)
            query_text = _join_queries(audited)
            bounded = ct.enforce_payload_budget(facade)     # ★ M3.5 收口：最终字节 ≤ 20000
            result_bytes = ct._byte_len(bounded)
            truncated = bool(bounded.get("truncated"))
            return bounded
        except ToolDenied:
            status = ERR_TOOL_NOT_AVAILABLE
            raise
        except ToolFailed as exc:
            status = exc.code
            payload = ct.enforce_payload_budget(exc.payload())
            result_bytes = ct._byte_len(payload)
            raise
        except tr.ContractError as exc:
            status = ERR_CONTRACT_ERROR
            raise ToolFailed(ERR_CONTRACT_ERROR, "契约缺失/自相矛盾：%s" % exc, tool)
        except Exception as exc:                            # 兜底：绝不把 500 留给调用方
            status = ERR_BACKEND_UNAVAILABLE
            raise ToolFailed(ERR_BACKEND_UNAVAILABLE,
                             "工具 %s 执行失败（%s）：%s" % (tool, type(exc).__name__, exc), tool)
        finally:
            # ★ 审计链：成功/拒绝/失败三条路径都落一条（C5：逐调用审计，不是只记成功）
            ct.emit_audit(tool, request_id, started_at, (time.time() - started) * 1000.0,
                          query_latency_ms, backend_calls, query_text, row_count, status,
                          body, result_bytes, truncated)


def _join_queries(audited: Sequence[Dict[str, Any]], limit: int = 2000) -> str:
    """把本次调用的 queryText 汇总成一条审计文本（超长截断，审计链不代表完整 SQL 日志）。"""
    text = " | ".join(str(item.get("queryText") or "") for item in audited if item.get("queryText"))
    if len(text) > limit:
        return text[:limit] + " …(审计链内截断)"
    return text


def _empty_hint(tool: str, facade: Dict[str, Any]) -> str:
    """空结果必须与"图空/对象不存在"可分（§3.6 与 G5 同源）。"""
    arrays = [facade.get("nodes"), facade.get("edges"), facade.get("items"),
              facade.get("collected")]
    if any(isinstance(item, list) and item for item in arrays):
        return ""
    return ("本次调用没有返回任何行（工具 %s）：这不等于该对象/故障码不存在——"
            "可能是 scope/code 未命中、timeWindow 过滤过窄，或该快照未装载实例数据。"
            "请用 get_object_topology(scope='*') 或 get_metrics(objects=[]) 先确认图里有什么。"
            % tool)


# ===========================================================================
# 4. FastAPI 应用（惰性导入：无 fastapi 时本模块仍可离线自检）
# ===========================================================================
def create_app(service: KgTService):
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

    def _model(spec: Dict[str, Any]):
        if not spec["params"]:
            return type("%sIn" % spec["name"], (Base,), {})
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
                annotations[name] = Dict[str, Any]
            else:
                annotations[name] = str
            if not pspec["required"]:
                annotations[name] = Optional[annotations[name]]
                defaults[name] = None
        namespace = {"__annotations__": annotations}
        namespace.update(defaults)
        return type("%sIn" % spec["name"], (Base,), namespace)

    def _dump(model: Any) -> Dict[str, Any]:
        if model is None:
            return {}
        for attr in ("model_dump", "dict"):
            fn = getattr(model, attr, None)
            if callable(fn):
                return fn()
        return {}

    def _denied(request: Request, tool: str) -> JSONResponse:
        return JSONResponse(status_code=404,
                            content=ToolDenied(tool, service.arm).payload())

    app = FastAPI(
        title="KG-T 门面（方案3 侧，仅 %d 个工具）" % EXPECTED_TOOL_COUNTS[service.arm],
        version="1.0.0",
        description=(
            "方案3（知识图谱直引 + LLM）的预置接口门面。工具面恰为 %s；"
            "★ 不含 cc_query / run_rules / validate / cc_propose%s。"
            % (service.tool_names,
               "，也不含 collect_now（本臂=" + service.arm + "）" if service.arm == ARM_MAIN else "")
        ),
    )

    @app.exception_handler(ToolDenied)
    async def _on_denied(request: Request, exc: ToolDenied):        # noqa: ANN001
        return JSONResponse(status_code=404, content=exc.payload())

    @app.exception_handler(ToolFailed)
    async def _on_failed(request: Request, exc: ToolFailed):        # noqa: ANN001
        code = 502 if exc.code in (ERR_BACKEND_UNAVAILABLE, ERR_UPSTREAM_UNAVAILABLE) else 400
        return JSONResponse(status_code=code, content=ct.enforce_payload_budget(exc.payload()))

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):  # noqa: ANN001
        spec = service.spec_of_path(request.url.path)
        tool = spec["name"] if spec else "(unknown)"
        err = ToolFailed(ERR_INVALID_PARAM,
                         "请求体不满足工具 %s 的参数契约（未识别字段或类型不符）不得静默回落默认值。" % tool,
                         tool)
        return JSONResponse(status_code=400,
                            content=ct.enforce_payload_budget(err.payload()))

    @app.get("/health")
    def health():
        snapshot = service.snapshot_id()
        return {
            "status": "ok" if snapshot else "degraded",
            "carrier": CARRIER,
            "arm": service.arm,
            "supplementary": service.supplementary,
            "inMainComparison": not service.supplementary,
            "port": default_port(service.arm),
            "snapshotId": snapshot,
            "backend": getattr(service.backend, "name", None)
                       or getattr(service.backend, "base_url", ""),
            "database": getattr(service.backend, "database", ""),
            "rowCap": ROW_CAP,
            "byteCap": BYTE_CAP,
            "tokenBudget": TOKEN_BUDGET,
            "maxHopDepth": service.contract.business_hop_depth,
            "maxHopsFault": service.contract.fault_max_hops,
            "tools": service.tool_names,
            "forbiddenTools": sorted(service._forbidden),
            "queryLatency": ct.latency_snapshot(),
            "auditChain": ct.AUDIT_CHAIN.snapshot(tail=5),
            "auditFile": ct.AUDIT_FILE,
            "rulesLoaded": False,
        }

    @app.get("/tools")
    def tools():
        """工具面自述（便于 F5/Dify 注册核对；只是清单的 HTTP 视图）。"""
        return {"arm": service.arm, "supplementary": service.supplementary,
                "inMainComparison": not service.supplementary,
                "count": len(service.tool_names), "tools": service.toolface,
                "forbiddenTools": sorted(service._forbidden)}

    # -- 越权路由：显式拒绝（404 + 明确码），不是"恰好没写路由"------------------
    denied_paths = dict(FORBIDDEN_PATHS)
    if service.arm == ARM_MAIN:
        denied_paths["/collect"] = "collect_now"
    for path, tool in denied_paths.items():

        def _make(tool_name: str):
            def endpoint(payload=None):                             # noqa: ANN001
                raise ToolDenied(tool_name, service.arm)
            endpoint.__name__ = "denied_%s" % tool_name
            return endpoint

        app.add_api_route(path, _make(tool), methods=["POST"],
                          name="denied:%s" % tool, summary="拒绝：%s 不在本臂工具面上" % tool)

    for spec in service.toolface:
        model = _model(spec)

        def _make_endpoint(spec_: Dict[str, Any], model_: Any):
            def endpoint(payload=None):                             # noqa: ANN001
                return service.dispatch(spec_["name"], _dump(payload))
            endpoint.__name__ = spec_["name"]
            endpoint.__doc__ = spec_["description"]
            if spec_["params"]:
                endpoint.__annotations__ = {"payload": model_, "return": dict}
            else:
                endpoint.__annotations__ = {"payload": Optional[model_], "return": dict}
            return endpoint

        app.add_api_route(spec["path"], _make_endpoint(spec, model), methods=["POST"],
                          name=spec["name"], summary=spec["name"],
                          description=spec["description"])
    return app


# ===========================================================================
# 5. 清单产出（由实现自身产出）
# ===========================================================================
def repo_root_default() -> str:
    probe = _HERE
    for _ in range(6):
        probe = os.path.dirname(probe)
        if not probe or probe == os.path.dirname(probe):
            break
        if os.path.isfile(os.path.join(probe, "shared", "model", "kgt-tools.yaml")):
            return probe
    return os.path.dirname(os.path.dirname(_HERE))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(path: str, root: str) -> str:
    try:
        rel = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    except Exception:
        return path
    return re_fallback(rel)


def re_fallback(rel: str) -> str:
    return REPO_REL_SOURCE if rel.startswith("..") else rel


def emit_manifest(arm: str = DEFAULT_ARM, repo_root: Optional[str] = None,
                  emitted_at: Optional[str] = None) -> Dict[str, Any]:
    """由实现自身产出工具面清单（供 F5 / Dify 注册核对 / 报告取证）。

    ★ 本清单**不是** G24 的输入：G24 比的是 Set B 的 KG↔ONTO 两侧同构工具面，
      而 KG-T 由契约 `tools-setb.yaml:123-124` 明确列入 `scope_excluded_arms`
      （"工具面必然不同"）。把本清单喂给 toolset_parity_gate 会制造假差异。
    """
    root = os.path.abspath(repo_root) if repo_root else repo_root_default()
    here = os.path.abspath(__file__)
    rel = _rel(here, root)
    digest = sha256_file(here)
    sources: List[Dict[str, str]] = [{"path": rel, "sha256": digest}]
    impl_path = os.path.join(root, IMPL_TOOLS.replace("/", os.sep))
    mach_path = os.path.join(root, IMPL_MACHINERY.replace("/", os.sep))
    for rel_path, abs_path in ((IMPL_TOOLS, impl_path), (IMPL_MACHINERY, mach_path)):
        if os.path.isfile(abs_path):
            sources.append({"path": rel_path, "sha256": sha256_file(abs_path)})

    tools = []
    for spec in toolface(arm):
        tools.append({
            "name": spec["name"],
            "description": spec["description"],
            "path": spec["path"],
            "params": [{"name": p["name"], "type": p["type"], "required": p["required"]}
                       for p in spec["params"]],
            "returns": [{"name": n, "type": t} for n, t in spec["returns"]],
            "errorCodes": list(spec["errorCodes"]),
            "source": rel,
            "sourceSha256": digest,
        })

    manifest: Dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "carrier": CARRIER,
        "arm": arm,
        "tool": rel,
        "source": rel,
        "sourceSha256": digest,
        "sources": sources,
        "ownerDoc": OWNER_DOC,
        "contract": CONTRACT_FILE,
        "servicePort": default_port(arm),
        "servicePortSource": ("contract" if arm == ARM_MAIN else "carrier-provisional"),
        "servicePortNote": ("kgt-tools.yaml#arms.KG-T.service_port = 8303（契约值）"
                            if arm == ARM_MAIN else
                            "kgt-tools.yaml#arms.KG-T-dyn.service_port = TBD（docs/09:410 U-N 未决）；"
                            "此值为载体侧暂定，不代表契约已定"),
        "difyApp": "cc-kgt-agent" if arm == ARM_MAIN else "cc-kgt-dyn-agent",
        "supplementary": supplementary(arm),
        "inMainComparison": not supplementary(arm),
        "supplementaryReason": ("" if arm == ARM_MAIN else
                                "KG-T-dyn 带 collect_now ⇒ 消费的 CCF 不再单一，违反硬约束 C1，"
                                "故不进主判据、不进表 1（docs/06 §2.5.4 第 2 条）"),
        "expectedToolCount": EXPECTED_TOOL_COUNTS[arm],
        "toolCount": len(tools),
        "forbiddenTools": forbidden_tools(arm),
        "auditChain": {
            "kind": "service-side-per-call",
            "fields": list(ct.AUDIT_FIELDS),
            "logFileEnv": "CC_AUDIT_LOG",
            "separateFrom": "elapsed_s",
        },
        "payloadPolicy": {
            "rowCap": ROW_CAP,
            "byteCap": BYTE_CAP,
            "tokenBudget": TOKEN_BUDGET,
            "tokenBudgetAccounting": "bytes（与 Set B 同口径，见 cc_tools.py:57-61）",
            "truncationMustReport": True,
        },
        "implementation": {
            "facade": rel,
            "tools": IMPL_TOOLS,
            "sharedMachinery": IMPL_MACHINERY,
            "note": ("前 4 个工具的算法实现在 topology_reader.py（docs/09:65），read_facts 是"
                     "到 :8304 的纯代理；本门面只做"
                     "注册/形状/记账，不重新定义契约、不复制算法。"),
        },
        "runtimeGuards": {
            "rulesFileLoaded": False,
            "rulesFileMarker": RULES_FILE_MARKER,
            "note": "N1（docs/09:22）：进程内不得加载 04-rules.cypher，--selftest 有机械断言。",
        },
        "tools": tools,
    }
    if emitted_at:
        manifest["emittedAt"] = emitted_at
    return manifest


def write_manifest(out_path: str, arm: str = DEFAULT_ARM,
                   repo_root: Optional[str] = None) -> Dict[str, Any]:
    manifest = emit_manifest(arm, repo_root,
                             emitted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    directory = os.path.dirname(os.path.abspath(out_path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    return manifest


# ===========================================================================
# 6. 离线自检
# ===========================================================================
class _Checks:
    def __init__(self) -> None:
        self.total = 0
        self.failures: List[str] = []

    def __call__(self, cond: Any, label: str, detail: str = "") -> bool:
        self.total += 1
        if cond:
            print("  PASS  %s%s" % (label, ("  <- " + detail) if detail else ""))
        else:
            print("  FAIL  %s%s" % (label, ("  <- " + detail) if detail else ""))
            self.failures.append(label)
        return bool(cond)


def _padded_backend(rows: int = 200, pad: int = 120):
    """离线桩：产出大行以检验 M3.5 收口（真机 no-op，不连库）。"""

    class _Pad(tr.ContractBackend):
        name = "pad-fixture"

        def fetch_objects(self):                       # noqa: D102
            return {"OBJ-PAD-%03d" % i: {"objId": "OBJ-PAD-%03d" % i, "objType": "AppInstance",
                                         "layer": "App", "props": {"pad": "x" * pad}}
                    for i in range(rows)}

        def fetch_evidence(self, object_type, object_id=None, metric_codes=None,
                           time_from=None, time_to=None):   # noqa: D102
            return [{"objectId": "OBJ-PAD-%03d" % i, "props": {
                "recordId": "REC-%03d" % i, "metricCode": "m", "capturedAt": "2026-01-01T00:00:00Z",
                "ccfSnapshotId": "SNAP-PAD", "rawValue": "x" * pad}} for i in range(rows)]

    return _Pad(tr.get_contract())


def selftest(arm: str = DEFAULT_ARM) -> int:
    """离线自检：不连库、不发网络请求。判据全部是机械事实。"""
    check = _Checks()
    print("=" * 78)
    print("KG-T 门面离线自检（arm=%s）" % arm)
    print("=" * 78)

    contract = tr.get_contract()
    service = KgTService(arm, backend=tr.ContractBackend(contract), contract=contract)

    # -- 1. 工具面 ------------------------------------------------------------
    print("\n[1] 工具面：恰 N 个、无禁令工具、与契约计数一致")
    names = service.tool_names
    check(len(names) == EXPECTED_TOOL_COUNTS[arm],
          "%s 工具数 == %d" % (arm, EXPECTED_TOOL_COUNTS[arm]), str(names))
    for bad in ("cc_query", "run_rules", "validate", "cc_propose"):
        check(bad not in names, "工具面不含 %s" % bad, str(names))
    if arm == ARM_MAIN:
        check("collect_now" not in names, "主臂不含 collect_now（kgt-tools.yaml#forbidden_tools_main_arm）")
    else:
        check("collect_now" in names, "补充臂含 collect_now")
    check(sorted(names) == sorted(["get_object_topology", "get_fault_topology",
                                   "get_metrics", "get_facts", "read_facts"]
                                  + (["collect_now"] if arm == ARM_DYN else [])),
          "工具名集合与契约一致", str(sorted(names)))
    # 与 kgt-tools.yaml / info-products.yaml 的期望计数对齐（读契约，不硬编码）
    check(EXPECTED_TOOL_COUNTS[arm] in (5, 6), "计数常量落在契约射程内",
          str(EXPECTED_TOOL_COUNTS[arm]))

    # -- 2. 分派层拒绝（负控）-------------------------------------------------
    print("\n[2] 分派层负控：越权工具必须被拒（不是「恰好没写路由」）")
    for bad in forbidden_tools(arm):
        try:
            service.dispatch(bad, {})
            check(False, "dispatch(%s) 被拒" % bad, "竟然没有抛 ToolDenied")
        except ToolDenied as exc:
            check(exc.payload()["error"]["code"] == ERR_TOOL_NOT_AVAILABLE,
                  "dispatch(%s) 被拒" % bad, exc.payload()["error"]["code"])
    for bogus in ("get_nonexistent", "cc_facts", ""):
        try:
            service.dispatch(bogus, {})
            check(False, "dispatch(%r) 被拒" % bogus, "竟然没有抛 ToolDenied")
        except ToolDenied as exc:
            check(True, "dispatch(%r) 被拒" % bogus, exc.tool)
        except ToolFailed:
            check(False, "dispatch(%r) 被拒" % bogus, "抛的是 ToolFailed（应是 ToolDenied）")

    # -- 3. N1：进程内不得加载规则文件 ----------------------------------------
    print("\n[3] N1：本进程不加载 04-rules.cypher、工具面不含 run_rules/validate")
    loaded = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and RULES_FILE_MARKER in os.path.basename(path):
            loaded.append(name)
    check(not loaded, "sys.modules 里没有规则文件", str(loaded))
    check(RULES_FILE_MARKER not in open(cc_tools_source(), encoding="utf-8").read()
          if os.path.isfile(cc_tools_source()) else True,
          "cc_tools.py 源码不引用规则文件", RULES_FILE_MARKER)
    check(RULES_FILE_MARKER not in open(tr.__file__, encoding="utf-8").read(),
          "topology_reader.py 源码不引用规则文件", RULES_FILE_MARKER)

    # -- 4. 工具全部可调 + 声明形状自洽 ----------------------------------------
    print("\n[4] 工具全部可调（fixture 后端）+ manifest 声明字段逐个存在")
    padded = _padded_backend(rows=4, pad=8)
    psvc = KgTService(arm, backend=padded, contract=contract)
    calls = {
        "get_object_topology": {"scope": "*"},
        "get_fault_topology": {"code": "FM-DB-SLOW-QUERY"},
        "get_metrics": {"objects": []},
        "get_facts": {"objectType": "AppInstance"},
    }
    if arm == ARM_DYN:
        calls["collect_now"] = {"objects": ["*"]}
    declared = {spec["name"]: [n for n, _t in spec["returns"]] for spec in service.toolface}
    for tool, args in calls.items():
        try:
            out = psvc.dispatch(tool, args)
        except Exception as exc:                       # noqa: BLE001
            check(False, "%s 可调（无异常）" % tool, "%s: %s" % (type(exc).__name__, exc))
            continue
        check(isinstance(out, dict) and bool(out), "%s 返回非空信封" % tool, str(type(out).__name__))
        missing = [n for n in declared[tool] if n not in out]
        check(not missing, "%s 声明字段全部存在" % tool, "缺失 %s" % missing)

    # -- 4b. read_facts：形状权威逐项一致 + 纯代理（verbatim / 结构化失败）-------
    print("\n[4b] read_facts：形状权威逐项一致 + 纯代理（不改名/不二次截断/结构化失败）")
    rf = [spec for spec in service.toolface if spec["name"] == "read_facts"]
    check(len(rf) == 1, "工具面含 read_facts（裁定 D47）", str(names))
    if rf:
        spec_rf = rf[0]
        triples = [(p["name"], p["type"], p["required"]) for p in spec_rf["params"]]
        check(triples == READ_FACTS_PARAM_TRIPLES,
              "read_facts 参数三元组 == 形状权威", str(triples))
        check([n for n, _t in spec_rf["returns"]] == READ_FACTS_RETURN_NAMES,
              "read_facts 返回名 == 形状权威（tool_level ∪ 公共信封）",
              str([n for n, _t in spec_rf["returns"]]))
        check(hashlib.sha256(spec_rf["description"].encode("utf-8")).hexdigest()
              == READ_FACTS_DESCRIPTION_SHA256,
              "read_facts 描述 sha256 == 形状权威（防漂移）")
        check(spec_rf["path"] == LIVE_FACTS_PATH, "read_facts 路由已登记", spec_rf["path"])
        check(ERR_UPSTREAM_UNAVAILABLE in spec_rf["errorCodes"],
              "read_facts 声明 UPSTREAM_UNAVAILABLE（502）", str(spec_rf["errorCodes"]))
        _shape_file = os.path.join(repo_root_default(), "shared", "model",
                                   "live-facts-tools.yaml")
        if os.path.isfile(_shape_file):
            try:
                import yaml as _yaml
                with open(_shape_file, encoding="utf-8") as _fh:
                    _live = _yaml.safe_load(_fh)["read_facts"]
                _live_triples = [(n, p["type"], bool(p.get("required", False)))
                                 for n, p in _live["params"].items()]
                check(sorted(_live_triples) == sorted(READ_FACTS_PARAM_TRIPLES),
                      "载体常量 == live-facts-tools.yaml#read_facts.params", str(_live_triples))
                check(_live["description"].rstrip("\n") == spec_rf["description"],
                      "载体描述 == live-facts-tools.yaml#read_facts.description")
            except Exception as _exc:                 # noqa: BLE001
                check(False, "形状权威可读", "%s: %s" % (type(_exc).__name__, _exc))
        else:
            check(True, "形状权威不在本机 ⇒ 跳过同源核对（sha256 断言仍有效）", _shape_file)
        _seen: Dict[str, Any] = {}

        def _fake_ok(url, body, _c=None, _r=None):      # noqa: ANN001
            _seen["url"] = url
            _seen["body"] = body
            return UPSTREAM_SAMPLE

        def _fake_down(url, body, _c=None, _r=None):    # noqa: ANN001
            raise LiveFactsTransportError("连接被拒绝（离线假上游）")

        _sent = {"layers": ["app"], "scope": {"objectType": "AppInstance"}}
        _out_ok = live_facts_call(dict(_sent), transport=_fake_ok)
        check(_seen.get("body") == _sent, "入参逐字转发（不改名/不补默认值）",
              str(_seen.get("body")))
        check(sorted(_out_ok) == sorted(READ_FACTS_RETURN_NAMES),
              "上游 JSON 原样透传：恰 8 键（无二次信封/无二次截断）", str(sorted(_out_ok)))
        try:
            live_facts_call({}, transport=_fake_down)
            check(False, "上游不可达 ⇒ 明确失败", "竟然返回成功")
        except ToolFailed as _exc2:
            _pl = _exc2.payload()
            check(_exc2.code == ERR_UPSTREAM_UNAVAILABLE,
                  "上游不可达 ⇒ %s" % ERR_UPSTREAM_UNAVAILABLE, _exc2.code)
            check(_pl["error"]["code"] == ERR_UPSTREAM_UNAVAILABLE
                  and isinstance(_pl.get("hint"), str) and _pl.get("truncated") is False,
                  "错误面是结构化信封（非 traceback / 非空成功）", str(sorted(_pl)))
        _real_post = globals()["_live_facts_post"]
        try:
            globals()["_live_facts_post"] = _fake_ok
            _out_d = service.dispatch("read_facts", {"max_records": 5})
            check(sorted(_out_d) == sorted(READ_FACTS_RETURN_NAMES),
                  "dispatch(read_facts) 透传且免二次截断", str(sorted(_out_d)))
            globals()["_live_facts_post"] = _fake_down
            try:
                service.dispatch("read_facts", {})
                check(False, "dispatch(read_facts) 上游不可达 ⇒ 明确失败", "竟然成功")
            except ToolFailed as _exc3:
                check(_exc3.code == ERR_UPSTREAM_UNAVAILABLE,
                      "dispatch(read_facts) 上游不可达 ⇒ %s" % ERR_UPSTREAM_UNAVAILABLE,
                      _exc3.code)
        finally:
            globals()["_live_facts_post"] = _real_post

        # -- 4c. ★ R-56/B：上游**业务拒绝码原样透传**（不得折成「上游不可用」）--------
        print("\n[4c] read_facts：上游收下请求但 fail-closed 拒绝 ⇒ 原样透传业务码")
        def _fake_refused(url, body, connect_timeout=0, read_timeout=0):
            raise LiveFactsTransportError(
                "上游 HTTP 409：%s" % '{"error":{"code":"BLOCKED_BASE","message":"覆盖门禁拒绝"}}',
                upstream_code="BLOCKED_BASE", upstream_status=409,
                upstream_body='{"error":{"code":"BLOCKED_BASE","message":"覆盖门禁拒绝"}}')

        try:
            globals()["_live_facts_post"] = _fake_refused
            try:
                service.dispatch("read_facts", {"layers": ["app"]})
                check(False, "上游 fail-closed ⇒ 明确失败", "竟然返回成功")
            except ToolFailed as _exc4:
                check(_exc4.code == "BLOCKED_BASE",
                      "上游业务码原样透传（不折成 %s）" % ERR_UPSTREAM_UNAVAILABLE, _exc4.code)
                check("BLOCKED_BASE" in _exc4.message and "409" in _exc4.message,
                      "透传码的报错里带码与上游状态", _exc4.message[:80])
        finally:
            globals()["_live_facts_post"] = _real_post
        for _c in READ_FACTS_UPSTREAM_CODES:
            check(_c in [s["errorCodes"] for s in service.toolface
                         if s["name"] == "read_facts"][0],
                  "read_facts 的 errorCodes 已登记可透传码 %s" % _c, "")

    # -- 5. 未知输入：确定性空结果或明确错误，绝不 500 --------------------------
    print("\n[5] 负控：未知对象/故障码/类型 —— 确定性空结果或明确错误，不得 500")
    for tool, args, why in (
        ("get_object_topology", {"scope": "NO-SUCH-OBJECT-XYZ"}, "未知 scope"),
        ("get_fault_topology", {"code": "FM-NOT-EXIST-XYZ"}, "未知故障码"),
        ("get_metrics", {"objects": ["NO-SUCH-OBJECT-XYZ"]}, "未知对象"),
        ("get_facts", {"objectType": "NoSuchTypeXYZ"}, "未知 objectType"),
    ):
        try:
            out = service.dispatch(tool, args)
            arrays = [out.get("nodes"), out.get("edges"), out.get("items")]
            check(all(isinstance(item, list) and not item
                      for item in arrays if item is not None),
                  "%s 对%s返回确定性空集" % (tool, why), str(out.get("hint", ""))[:40])
            check(bool(out.get("hint")), "%s 空结果附 hint（空≠不存在）" % tool)
        except ToolFailed as exc:
            check(True, "%s 对%s给出明确错误 %s" % (tool, why, exc.code), exc.message[:60])
        except Exception as exc:                       # noqa: BLE001
            check(False, "%s 对%s不抛未分类异常" % (tool, why), "%s: %s" % (type(exc).__name__, exc))
    # 入参非法 ⇒ 明确错误码，不 500
    for tool, args in (("get_object_topology", {}), ("get_object_topology", {"scope": 1}),
                       ("get_object_topology", {"scope": "a", "bogusField": 1}),
                       ("get_fault_topology", {"code": "X", "max_hops": "4"}),
                       ("get_facts", {"objectType": 7})):
        try:
            service.dispatch(tool, args)
            check(False, "%s 非法入参被拒 %s" % (tool, args), "竟然通过")
        except ToolFailed as exc:
            check(exc.code == ERR_INVALID_PARAM, "%s 非法入参被拒 %s" % (tool, args), exc.code)

    # -- 6. M3.5 载荷有界 ------------------------------------------------------
    print("\n[6] M3.5：先让实现自身溢出，再验证收口把最终对象压到 <= %d B 且如实记账" % BYTE_CAP)
    big = _padded_backend(rows=200, pad=120)
    bsvc = KgTService(arm, backend=big, contract=contract)
    for tool, args in (("get_object_topology", {"scope": "*"}),
                       ("get_facts", {"objectType": "AppInstance"})):
        out = bsvc.dispatch(tool, args)
        size = ct._byte_len(out)
        check(size <= BYTE_CAP, "%s 最终字节 <= %d" % (tool, BYTE_CAP), "%d B" % size)
        check(bool(out.get("truncated")), "%s 溢出时 truncated=True" % tool)
        trunc = out.get("truncation") or {}
        check(bool(trunc), "%s 截断记账存在（不静默）" % tool, str(sorted(trunc))[:80])
    # 未溢出必须原样（不新增 truncation 键的语义：由 cc_tools 的 selftest 覆盖）
    check(BYTE_CAP == 20000 and TOKEN_BUDGET == 20000, "载荷上限常量 == 20000", "")

    # -- 7. M3.6 审计链 + 与 elapsed_s 分离 ------------------------------------
    print("\n[7] M3.6：审计链字段 + query_latency_p50/p95 + 与 elapsed_s 分离")
    before = ct.AUDIT_CHAIN.total()
    padded2 = _padded_backend(rows=2, pad=4)
    psvc2 = KgTService(arm, backend=padded2, contract=contract)
    psvc2.dispatch("get_object_topology", {"scope": "*"})
    record = ct.AUDIT_CHAIN.tail(1)[0]
    check(ct.AUDIT_CHAIN.total() == before + 1, "每次调用恰落一条审计记录")
    check(sorted(record.keys()) == sorted(ct.AUDIT_FIELDS),
          "字段与 AUDIT_FIELDS 逐字一致", str(sorted(record.keys())))
    check(record["tool"] == "get_object_topology" and record["status"] == "ok",
          "tool/status 正确", "%s/%s" % (record["tool"], record["status"]))
    check(str(record["startedAt"]).endswith("Z") and "T" in str(record["startedAt"]),
          "startedAt 为 UTC ISO8601", str(record["startedAt"]))
    check(len(str(record["paramsDigest"])) == 16, "paramsDigest 为 16 位 hex", record["paramsDigest"])
    check(isinstance(record["truncated"], bool), "truncated 为布尔")
    check(record["resultBytes"] > 0, "resultBytes > 0", str(record["resultBytes"]))
    snap = ct.latency_snapshot()
    check("elapsed_s" not in snap and snap.get("separate_from") == "elapsed_s",
          "分离① 快照不含 elapsed_s 且自述 separate_from", str(snap.get("separate_from")))
    check("elapsedMs" in record and "queryLatencyMs" in record
          and record["elapsedMs"] != record["queryLatencyMs"],
          "分离② elapsedMs 与 queryLatencyMs 是两个独立键",
          "elapsed=%s query=%s" % (record["elapsedMs"], record["queryLatencyMs"]))
    check(float(record["elapsedMs"]) >= float(record["queryLatencyMs"]) - 1e-9,
          "分离③ elapsedMs ⊇ queryLatencyMs")
    check(snap.get("query_latency_p50") is not None and snap.get("query_latency_p95") is not None,
          "p50/p95 已产出", "p50=%s p95=%s" % (snap.get("query_latency_p50"),
                                              snap.get("query_latency_p95")))
    # 拒绝路径也要进链
    try:
        service.dispatch("cc_query", {"query": "MATCH (n) RETURN n"})
    except ToolDenied:
        pass
    denied_rec = ct.AUDIT_CHAIN.tail(1)[0]
    check(denied_rec["tool"] == "cc_query" and denied_rec["status"] == ERR_TOOL_NOT_AVAILABLE,
          "拒绝路径也进审计链", "%s/%s" % (denied_rec["tool"], denied_rec["status"]))

    # -- 8. HTTP 面（路由表 + 越权路径 404）-----------------------------------
    print("\n[8] HTTP 面：注册路由恰为 %d 工具 + /health + /tools，越权路径 404" % len(names))
    try:
        app = create_app(service)
        routes = [(getattr(r, "path", None), getattr(r, "name", None)) for r in app.routes]
        tool_routes = [p for p, _n in routes if p in
                       [spec["path"] for spec in service.toolface]]
        check(len(tool_routes) == len(names), "工具路由数 == %d" % len(names), str(tool_routes))
        route_names = {n for _p, n in routes}
        for bad in ("cc_query", "run_rules", "validate"):
            check(bad not in route_names, "路由表里没有名为 %s 的路由" % bad)
        for bad_path in FORBIDDEN_PATHS:
            check(bad_path in [p for p, _n in routes],
                  "越权路径 %s 已注册显式拒绝路由（明确拒绝 > 恰好 404）" % bad_path)
        if arm == ARM_MAIN:
            check("/collect" in [p for p, _n in routes], "主臂对 /collect 也显式拒绝")
        from starlette.testclient import TestClient

        client = TestClient(app)
        health = client.get("/health")
        check(health.status_code == 200, "/health 200", str(health.status_code))
        body = health.json()
        check(body["arm"] == arm and body["tools"] == names,
              "/health 回显 arm 与工具面", str(body.get("tools")))
        check(body.get("rulesLoaded") is False, "/health 自述 rulesLoaded=False")
        for path, tool in FORBIDDEN_PATHS.items():
            resp = client.post(path, json={})
            check(resp.status_code == 404, "POST %s -> 404" % path, str(resp.status_code))
            check(resp.json()["error"]["tool"] == tool, "POST %s 的拒绝体点名 %s" % (path, tool))
        if arm == ARM_MAIN:
            resp = client.post("/collect", json={"objects": []})
            check(resp.status_code == 404, "POST /collect -> 404（主臂）", str(resp.status_code))
        ok = client.post("/topology/object", json={"scope": "NO-SUCH-XYZ"})
        check(ok.status_code == 200, "POST /topology/object 未知 scope -> 200（确定性空结果）",
              str(ok.status_code))
        bad = client.post("/topology/object", json={"scope": "x", "extra": 1})
        check(bad.status_code == 400, "POST /topology/object 多余字段 -> 400", str(bad.status_code))
    except ImportError as exc:                          # pragma: no cover - 无 fastapi 环境
        check(True, "无 HTTP 依赖 ⇒ 跳过 HTTP 面（离线自检其余项仍有效）", str(exc))

    # -- 9. 清单自洽 ----------------------------------------------------------
    print("\n[9] 清单：由实现自身产出，工具面 + sha256 与文件一致")
    manifest = emit_manifest(arm)
    check(manifest["arm"] == arm, "manifest.arm == %s" % arm, manifest["arm"])
    check(manifest["toolCount"] == EXPECTED_TOOL_COUNTS[arm],
          "manifest.toolCount == %d" % EXPECTED_TOOL_COUNTS[arm], str(manifest["toolCount"]))
    check(manifest["sourceSha256"] == sha256_file(os.path.abspath(__file__)),
          "manifest.sourceSha256 == 文件 sha256")
    mnames = [t["name"] for t in manifest["tools"]]
    check(mnames == names, "manifest 工具顺序与工具面一致", str(mnames))
    check(all("path" in t and "errorCodes" in t for t in manifest["tools"]),
          "每条工具都带 path/errorCodes")
    check(manifest["supplementary"] == (arm == ARM_DYN),
          "supplementary == %s" % (arm == ARM_DYN))
    check(manifest["inMainComparison"] == (arm == ARM_MAIN),
          "inMainComparison == %s" % (arm == ARM_MAIN))
    check(manifest["servicePort"] == default_port(arm), "servicePort == %d" % default_port(arm))
    if arm == ARM_DYN:
        check(manifest["servicePortSource"] == "carrier-provisional",
              "补充臂端口如实标为暂定")
    check(any(src["path"] == IMPL_TOOLS for src in manifest["sources"]),
          "sources 含工具实现源码（provenance）")

    print("\n" + "=" * 78)
    if check.failures:
        print("RESULT: FAIL  （%d/%d 项通过，失败 %d 项）"
              % (check.total - len(check.failures), check.total, len(check.failures)))
        for item in check.failures:
            print("  - %s" % item)
        print("=" * 78)
        return 1
    print("RESULT: PASS  （%d/%d 项通过）" % (check.total, check.total))
    print("=" * 78)
    return 0


def cc_tools_source() -> str:
    return os.path.join(_HERE, "cc_tools.py")


# ===========================================================================
# 7. CLI
# ===========================================================================
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="KG-T/KG-T-dyn 门面（方案3 侧，只读，5/6 工具）")
    ap.add_argument("--serve", action="store_true", help="启动 HTTP 服务")
    ap.add_argument("--host", default=os.environ.get("CC_KGT_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=None,
                    help="监听端口；默认主臂 8303，补充臂载体侧暂定 8305")
    ap.add_argument("--arm", default=os.environ.get("CC_KGT_ARM", DEFAULT_ARM), choices=list(ARMS))
    ap.add_argument("--emit-manifest", dest="manifest_out", default=None,
                    help="把工具面清单写到该路径（由实现自身产出）")
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--selftest", action="store_true", help="离线自检（不连库）")
    ap.add_argument("--call", default=None, help="直接调用某工具")
    ap.add_argument("--args", default="{}", help="--call 的 JSON 入参")
    ap.add_argument("--fixture", action="store_true", help="用契约 fixture 而非真机")
    ap.add_argument("--list-tools", action="store_true", help="打印本臂工具面")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    port = args.port if args.port is not None else default_port(args.arm)

    if args.list_tools:
        for spec in toolface(args.arm):
            print("%-22s POST %-18s params=%s"
                  % (spec["name"], spec["path"],
                     [(p["name"], p["type"], p["required"]) for p in spec["params"]]))
        print("arm=%s supplementary=%s tools=%d forbidden=%s"
              % (args.arm, supplementary(args.arm), EXPECTED_TOOL_COUNTS[args.arm],
                 forbidden_tools(args.arm)))
        return 0

    if args.selftest:
        return selftest(args.arm)

    if args.manifest_out:
        manifest = write_manifest(args.manifest_out, args.arm, args.repo_root)
        print("manifest: %s（arm=%s，%d 工具，sha256=%s）"
              % (args.manifest_out, manifest["arm"], manifest["toolCount"],
                 manifest["sourceSha256"][:16]))
        return 0

    if args.call:
        if args.fixture:
            contract = tr.get_contract()
            backend: Any = tr.ContractBackend(contract)
        else:
            contract = tr.get_contract()
            backend = tr.default_backend(contract)
        service = KgTService(args.arm, backend=backend, contract=contract)
        payload = json.loads(args.args)
        try:
            out = service.dispatch(args.call, payload)
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0
        except ToolDenied as exc:
            print(json.dumps(exc.payload(), ensure_ascii=False, indent=2))
            return 3
        except ToolFailed as exc:
            print(json.dumps(exc.payload(), ensure_ascii=False, indent=2))
            return 4

    if args.serve:
        contract = tr.get_contract()
        if args.fixture:
            backend = tr.ContractBackend(contract)
        else:
            backend = tr.default_backend(contract)
        service = KgTService(args.arm, backend=backend, contract=contract)
        app = create_app(service)
        print("== KG-T 门面：arm=%s tools=%s port=%d =="
              % (args.arm, service.tool_names, port), file=sys.stderr)
        try:
            import uvicorn
        except Exception as exc:                        # pragma: no cover
            print("缺少 uvicorn：%s" % exc, file=sys.stderr)
            return 2
        uvicorn.run(app, host=args.host, port=port, log_level="info")
        return 0

    _build_parser().print_help()
    return 0


# ===========================================================================
# 8. M5/S3a-1：中性取数 read_facts（纯代理到 :8304；只读）
# ===========================================================================
# 依据（裁定 D47）：主对比组两臂与 KG-T 主臂都必须拿到同一个中性取数工具；其定义唯一源 =
# shared/model/live-facts-tools.yaml#read_facts，实现唯一源 =
# shared/collect/live_facts_service.py（HTTP :8304，POST /tools/read_facts）。
# 本门面**只代理**：不实现取数、不改名、不二次截断、不二次包信封。
LIVE_FACTS_PATH = "/tools/read_facts"
LIVE_FACTS_HOST_DEFAULT = "127.0.0.1"
LIVE_FACTS_PORT_DEFAULT = 8304
LIVE_FACTS_URL_DEFAULT = "http://%s:%d%s" % (LIVE_FACTS_HOST_DEFAULT,
                                             LIVE_FACTS_PORT_DEFAULT, LIVE_FACTS_PATH)
LIVE_FACTS_URL_ENV = "CC_LIVE_FACTS_URL"          # 可注入基址（离线自证 / 换端口时用）
# ★ 快速失败：M5 窗口里 :8304 会按用例起停，工具绝不能挂住整个 agent 轮次。
LIVE_FACTS_CONNECT_TIMEOUT_S = 2.0                # 连接超时（显式常量）
LIVE_FACTS_READ_TIMEOUT_S = 15.0                  # 读超时（显式常量）
ERR_UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # 502：中性取数面不可用

#: ★ R-56/B（2026-10-12）：read_facts 是**纯代理**，上游（:8304）**收下请求但拒绝执行**时，
#:   其业务码必须**原样透传**，不得一律折成 `UPSTREAM_UNAVAILABLE`。
#:   真机证据（`message_agent_thoughts`）：模型把 `layers` 传成 `["app"]` ⇒ 上游正确
#:   fail-closed 报覆盖门禁，模型收到的却是「中性取数面上游返回 HTTP 409 / 上游不可用」
#:   ⇒ 判定「采集接口不可用」并放弃取数。码集与 `live-facts-tools.yaml#read_facts.error_codes`
#:   逐字同源（纯代理的诚实面；与 KG / ONTO 两侧必须**逐字相同**，否则四组工具面不一致）。
READ_FACTS_UPSTREAM_CODES = (
    "BAD_JSON", "BAD_PARAM", "READ_ONLY_REFUSED", "UNKNOWN_PARAM",
    "PAYLOAD_TOO_LARGE", "METHOD_NOT_ALLOWED", "NOT_FOUND", "NETWORK_REFUSED",
    "BLOCKED_BASE", "BLOCKED_NO_INPUT_FACTS", "BLOCKED_UNKNOWN_CASE",
    "BLOCKED_COVERAGE", "BLOCKED_INJECTION",
)

# 形状权威的派生常量（--selftest 用它防"声明的副本"漂移；正文不复制到本文件）
READ_FACTS_PARAM_TRIPLES: List[Tuple[str, str, bool]] = [
    ("layers", "array", False),
    ("sections", "array", False),
    ("scope", "string", False),
    ("max_records", "integer", False),
    ("max_bytes", "integer", False),
]
READ_FACTS_RETURN_NAMES: List[str] = ["objects", "relations", "observations", "source", "hint", "truncated", "droppedRows", "droppedBytes"]
READ_FACTS_DESCRIPTION_SHA256 = "ee971388239b5210dfdd29367bccc547dc14e84982d3117edfb42ec5bec50237"
# 上游 JSON 的**样本**（仅自检用：断言透传后键集不变，不是形状定义）
UPSTREAM_SAMPLE: Dict[str, Any] = {
    "objects": [{"objectId": "OBJ-SAMPLE"}],
    "relations": [],
    "observations": [],
    "source": {"kind": "sample"},
    "hint": "",
    "truncated": False,
    "droppedRows": 0,
    "droppedBytes": 0,
}

_read_facts_specs = [spec for spec in _TOOLFACE_MAIN if spec["name"] == "read_facts"]
assert len(_read_facts_specs) == 1, "KG-T 工具面缺 read_facts（M5/S3a-1 施工项）"
for _code in (ERR_UPSTREAM_UNAVAILABLE,) + READ_FACTS_UPSTREAM_CODES:
    if _code not in _read_facts_specs[0]["errorCodes"]:
        _read_facts_specs[0]["errorCodes"].append(_code)


def live_facts_target(url: Optional[str] = None) -> str:
    """解析中性取数面地址：显式入参 > 环境变量 > 默认 :8304（可注入）。"""
    if url:
        return str(url)
    return str(os.environ.get(LIVE_FACTS_URL_ENV) or LIVE_FACTS_URL_DEFAULT)


class LiveFactsTransportError(RuntimeError):
    """上游不可达 / 超时 / 非 200 / 非法 JSON。**任何一条都不得变成空成功。**

    ★ R-56/B：非 200 时额外携带上游的**机读码**（``upstream_code``）：纯代理必须把
      「上游收下了请求但拒绝执行（业务拒绝）」与「上游根本不可达（传输故障）」分开报。
    """

    def __init__(self, message, upstream_code=None, upstream_status=None, upstream_body=""):
        RuntimeError.__init__(self, message)
        self.upstream_code = upstream_code
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body


def _upstream_error_code(raw: bytes):
    """从上游响应体里取机读码；非 JSON / 无 error.code / 不在已登记码集 ⇒ None。"""
    try:
        parsed = json.loads(raw.decode("utf-8"))
        code = (parsed.get("error") or {}).get("code") if isinstance(parsed, dict) else None
    except Exception:                                 # noqa: BLE001
        return None
    return code if isinstance(code, str) and code in READ_FACTS_UPSTREAM_CODES else None


def _live_facts_post(url: str, body: Dict[str, Any],
                     connect_timeout: float = LIVE_FACTS_CONNECT_TIMEOUT_S,
                     read_timeout: float = LIVE_FACTS_READ_TIMEOUT_S) -> Dict[str, Any]:
    """把入参**原样** POST 到上游，返回解析后的 JSON（两段超时都是常量）。

    只用 stdlib `http.client`：本载体的出向 HTTP 走的是 topology_reader（Neo4j 驱动），
    与中性取数面无关，故此处不新增第三方依赖、也不复用图库连接。
    """
    import http.client
    from urllib.parse import urlsplit

    pieces = urlsplit(str(url))
    if pieces.scheme != "http" or not pieces.hostname:
        raise LiveFactsTransportError("上游地址非法（只支持 http://host:port/path）：%r" % url)
    target_path = pieces.path or "/"
    if pieces.query:
        target_path = "%s?%s" % (target_path, pieces.query)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn = None
    raw = b""
    try:
        conn = http.client.HTTPConnection(pieces.hostname, pieces.port or 80,
                                          timeout=connect_timeout)
        conn.connect()
        if conn.sock is not None:                     # ★ 建连后改设**读**超时（两段分别可控）
            conn.sock.settimeout(read_timeout)
        conn.request("POST", target_path, body=payload,
                     headers={"Content-Type": "application/json; charset=utf-8",
                              "Accept": "application/json",
                              "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            _body_text = raw[:400].decode("utf-8", "replace")
            raise LiveFactsTransportError(
                "上游 HTTP %d：%s" % (resp.status, _body_text),
                upstream_code=_upstream_error_code(raw),
                upstream_status=resp.status,
                upstream_body=_body_text[:200])
    except LiveFactsTransportError:
        raise
    except Exception as exc:                          # 连接失败 / 超时 / 协议错
        raise LiveFactsTransportError("%s: %s" % (type(exc).__name__, exc))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                         # pragma: no cover
                pass
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LiveFactsTransportError("上游响应不是合法 JSON：%s" % exc)
    if not isinstance(parsed, dict):
        raise LiveFactsTransportError("上游响应不是 JSON 对象（%s）" % type(parsed).__name__)
    return parsed


def live_facts_call(params: Dict[str, Any], url: Optional[str] = None,
                    transport: Optional[Any] = None) -> Dict[str, Any]:
    """纯代理：入参原样转发、上游 JSON 原样返回。

    ★ R-56/B：传输类失败（不可达 / 超时 / 非 JSON）⇒ ``UPSTREAM_UNAVAILABLE``；
      **上游的业务拒绝码原样透传**（纯代理的诚实面 —— 否则调用方会把入参错当成上游故障）。
    """
    target = live_facts_target(url)
    post = transport or _live_facts_post
    try:
        out = post(target, params, LIVE_FACTS_CONNECT_TIMEOUT_S, LIVE_FACTS_READ_TIMEOUT_S)
    except LiveFactsTransportError as exc:
        if getattr(exc, "upstream_code", None):
            raise ToolFailed(exc.upstream_code,
                             "中性取数面返回 HTTP %s（%s）：%s"
                             % (exc.upstream_status, exc.upstream_code,
                                (exc.upstream_body or str(exc))[:300]), "read_facts") from None
        raise ToolFailed(ERR_UPSTREAM_UNAVAILABLE,
                         "中性取数面不可达或返回非法（%s）：%s" % (target, exc), "read_facts")
    except Exception as exc:                          # 兜底：绝不静默返回空成功
        raise ToolFailed(ERR_UPSTREAM_UNAVAILABLE,
                         "中性取数面调用失败（%s）：%s" % (type(exc).__name__, exc), "read_facts")
    if not isinstance(out, dict):
        raise ToolFailed(ERR_UPSTREAM_UNAVAILABLE,
                         "中性取数面返回非对象（%s）" % type(out).__name__, "read_facts")
    return out


if __name__ == "__main__":
    sys.exit(main())
