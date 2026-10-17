# -*- coding: utf-8 -*-
"""S2a · 中性取数服务 ``read_facts``（第 4 个只读服务，默认 :8304）。

数据面只做读数：静态结构留在结构层，实时读数一律「现取、内存组装、用完即弃」（裁定 D30）。
本服务把 ``shared/collect/live_facts.py`` 已经封好的这条纯函数链

    基底 →（覆盖门禁，只作用于基底）→ 注入 → 请求级工作集 → 有界表示

暴露成一个**中性、只读、无跨请求可变状态**的取数接口。四组 App 对称注册同一 provider 的
同一工具，id 空间一律是 CCF 归一化 id（裁定 D37 / D40 / D45）。

两种运行模式（方案 §12）——★ 用例选择是**进程级**的（队长裁定 D48）：

* **判决模式**（进程启动时 ``--case <case_id>`` 或 env ``M5_CASE_ID`` 非空）：从
  ``shared/model/m5-input-facts.yaml`` 读该用例的「基底引用 + 注入差分」，确定性重建工作集；
  **零网络**——采集函数在判决模式下被换成 fail-closed 钩子，一调用即报错（``NETWORK_REFUSED``）；
* **运行模式**（进程启动时未指定用例）：向采集面真采一次（``POST /tools/gather``，地址与入参
  一律沿用 ``live_facts`` 既有默认与既有入参），请求级内存组装，``finally`` 清空。

★ 用例**不是**请求级参数：工具 schema 里没有任何 case 参数（模型面零 M5 专有参数，生产同形），
HTTP 请求体里出现 ``case_id`` 按未知入参拒绝（``UNKNOWN_PARAM``，走既有 c17 路径）；进程生命周期
内用例不可变，服务仍无跨请求可变状态。``--dump-case`` 是保留的内部确定性工具（离线重建，不经 HTTP）。

只读：本服务不暴露任何写操作；请求体里出现写意图字段即拒绝。本文件**不重写**归一化 /
注入 / 覆盖判据，全部转调 ``live_facts`` 的公开入口。

CLI：``--selftest`` / ``--dump-case`` / ``--serve`` / ``--emit-schema``；进程级用例 ``--case``（env ``M5_CASE_ID``）。
"""

from __future__ import annotations

import argparse
import copy
import datetime
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _HERE not in sys.path:  # 直接以脚本方式运行也能 import 同级封装层
    sys.path.insert(0, _HERE)

import live_facts as LF  # noqa: E402  —— 唯一的实现面：归一化 / 注入 / 覆盖判据都从这里来


# --------------------------------------------------------------------- 1) 冻结常量

#: 工具名（中性名，零载体词）。端点路径、`--selftest` 抬头、帮助文本、schema 全部由它拼出，
#: 日后改名只改这一处。
TOOL_NAME = "read_facts"
TOOL_PATH = "/tools/" + TOOL_NAME
HEALTH_PATH = "/health"

SERVICE_NAME = "shared-collect-read-facts"
SERVICE_VERSION = "1.0.0"
INPUT_FACTS_SCHEMA = LF.INPUT_FACTS_SCHEMA          # "m5-input-facts/1"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8304

DEFAULT_LAYERS = ("gaussdb", "os", "container", "app")
DEFAULT_SECTIONS = ()
DEFAULT_MAX_RECORDS = 200
DEFAULT_MAX_BYTES = 20000
MAX_RECORDS_LIMIT = 5000
MAX_BYTES_LIMIT = 200000
MAX_BODY_BYTES = 262144                             # 请求正文上限（256 KiB）

DEFAULT_PROFILE = "l3-only"                         # 与 freeze.py:1386 / load_base 既有默认一致
COLLECT_TIMEOUT_S = 60.0

#: 可接受的采集**层名**（★ R-56/A，2026-10-12）。
#: 权威来源 = 采集面探针注册表的层名（真机实测 `get_all_probes()` =
#: ``['app','container','gaussdb','os','storage','tpops']``）。
#: 为什么必须显式校验：模型面上 `layers` 只声明为 `array of string`（无 enum、无取值说明），
#: 于是模型会**猜**层名 —— 实测真机发出过 ``["app","container","db","host"]``、
#: ``["App","Runtime","Data"]``；这些名字会被原样转发到采集面，最终以
#: 「层 db 事实数为 0」这类**覆盖门禁**错误收场，既不是调用方能修的错，也说不清怎么改。
#: ⇒ fail-closed 不变（本来就是错），但**错得更准确、可自纠**。
KNOWN_LAYERS = ("gaussdb", "os", "container", "app", "storage", "tpops")

#: 运行模式下``layers``**必须覆盖到**的层（缺一层 ⇒ 覆盖门禁 G5/X8 必然拒绝）。
#: 依据 = ``shared/model/v3-ccf-map.yaml#expected_profiles.<profile>.types`` / ``.metrics`` 的落点反查：
#:   ``l3-only`` 的 6 个类型与 18 个指标全部落在 ``gaussdb``（``db.*``）与 ``os``（``host.*``）两层；
#:   ``l1-l3`` 在其上再加 ``container`` / ``app``。
#: ★ 实测标定（2026-10-12，12 个层组合逐例跑 :8304）：
#:   含 ``gaussdb+os`` 的 6 个组合（含四层、含四层+storage）**全 PASS**；
#:   缺任一层的 6 个组合（``["gaussdb"]``/``["os"]``/``["app"]``/``["container"]``/
#:   ``["container","app"]``/``["storage"]``）**全 BLOCKED_BASE（覆盖门禁）** ⇒ 与下表逐例相符。
#: ★ 本服务只用 ``DEFAULT_PROFILE``；``l1-l3`` 一行是给 CLI/未来档位留的口径（按 types 落点推导，本服务不消费）。
#: ★ 私有（`_` 前缀）是**硬约束**：自检 c25_no_cross_request_state 断言「模块级不得存在
#:   **公开的可变容器**」（dict/list）。对外只经 :func:`required_layers` 以**元组**暴露。
_PROFILE_REQUIRED_LAYERS = {
    "l3-only": ("gaussdb", "os"),
    "l1-l3": ("gaussdb", "os", "container", "app"),
}


def required_layers(profile=None):
    """``expected_profile`` 在运行模式下必需的采集层（元组；未知档位 ⇒ 空元组 + fail-closed 语义）。

    ★ 未知档位返回空元组 = **不做多余的更严限制**，而不是「放行一切」：
      真正的档位校验仍在 ``freeze.build``（`v3-ccf-map.yaml` 没有该 profile 就报错）。
    """
    name = DEFAULT_PROFILE if profile is None else profile
    return tuple(_PROFILE_REQUIRED_LAYERS.get(name, ()))


INPUT_FACTS_REL = "shared/model/m5-input-facts.yaml"
#: ★ R-58：形状权威（模型面参数说明的唯一真源）——自检 c19c 用它做「声明面 ↔ 实现自述」同源断言。
SHAPE_REL = "shared/model/live-facts-tools.yaml"
DEFAULT_INPUT_FACTS = os.path.join(_ROOT, "shared", "model", "m5-input-facts.yaml")

MODE_JUDGEMENT = "judgement"
MODE_RUNTIME = "runtime"

SOURCE_REQUEST = "request"
SOURCE_EMPTY = "empty"

#: ★ D48：case 选择信道 = **进程级**。本次运行消费哪个用例由启动参数 ``--case <case_id>``
#: 或环境变量 ``M5_CASE_ID`` 固定；进程生命周期内不可变，**没有按请求的用例参数**——
#: 工具 schema 里不含任何 case 参数，HTTP 请求体里出现 ``case_id`` 按未知入参拒绝。
CASE_ENV_VAR = "M5_CASE_ID"

#: 三段行容器（objects+relations+observations）的键名与顺序，填充顺序固定 = 对象→关系→观测。
ROW_KEYS = ("objects", "relations", "observations")

MAX_STR_LADDER = (float(LF.AGG_MAX_STR), 64, 32, 16, 8)


def _layer_guidance():
    """层名相关的**可自纠**提示。

    ★ 为什么把说明塞进报错里：模型面上 ``layers`` 只有名字与类型、**没有任何取值说明**
      （``cc_tools*.py`` 的 ``READ_FACTS_PARAMS`` 不含 description），而 Dify 会把
      **响应体**（含 ``error.message``）回灌给模型 ⇒ **报错文本是模型唯一能学到的信道**。
      故这里必须点名合法取值与「要收窄请用 scope」。
    """
    return ("本部署可用的层名只有 %s；运行模式（本进程未固定用例）还必须含 %s"
            "（expected_profile=%r 的覆盖门禁 G5/X8 会拒绝任何缺这些层的请求，实测必失败）。"
            "★ 若要按对象类型 / 对象 id / 层**收窄返回结果**，请改用 scope 参数"
            "（键只能取 object_ids / types / layers / metric_codes），**不要**用 layers 去收窄。"
            % (list(KNOWN_LAYERS), list(required_layers()), DEFAULT_PROFILE))

#: S2a 施工基线：上游两个**禁改**文件的指纹。与基线不一致时本服务的自检报 BLOCKED
#: （提示「上游封装层已换代，需要重新基线」，而不是把它当成本服务的缺陷）。
#:
#: ★ R-56 重新基线（2026-10-12）：``live_facts.py`` 由 ``fae46283…`` → ``e9c819c6…``。
#:   这次改动**是有意的、且经用户侧故障驱动**：``fetch_live`` 的采集面基址此前只读
#:   ``GDO_COLLECT_URL``（部署只给了 ``CC_GATHER_URL``）⇒ 静默回落到模块内硬编码的
#:   另一台主机 ``192.0.2.10:8187``（其库为备机、且会用自己的 ``_loop_guard`` 熔断）
#:   ⇒ Dify 上 ``read_facts`` 恒报不可用。修法见 ``live_facts.collect_base_url``
#:   与 ``fetch_live`` 的 docstring（取值链 + 端点归一化 + ``X-GDO-Internal``）。
#:   ``freeze.py`` **未被改动**，其指纹逐字保持原基线。
#:
#: ★ 开源导出重新基线（2026-10-16）：导出期脱敏把这两个文件里的内网地址改写成
#:   文档示例地址（RFC 5737 ``192.0.2.0/24``）与占位路径 ⇒ 字节与 sha256 必然改变。
#:   按 R-56 同一惯例**同步基线、不回退文件**（脱敏不可回退，回退即把内网拓扑写回公开仓）。
#:   本文件此前基线 = ``e9c819c6…``；``freeze.py`` 此前基线 = ``dea509b4…``。
#:
#: ★ 第二次重新基线（2026-10-17）：内网主机简写（``81.83``/``81.84``）改写成
#:   「部署宿主」/「旧载体机」时，``freeze.py`` 的**注释行**（``:882``）也在其中 ⇒
#:   字节与 sha256 再次变化。同一惯例：**同步基线、不回退文件**。
#:   ``freeze.py`` 此前基线 = ``329b7a0a…``。
_UPSTREAM_BASELINE = {
    os.path.join("shared", "collect", "live_facts.py"):
        "e5ee28b9b5c56225bc0eb9b8686c5d9ac970abc56072276202da384de9881089",
    os.path.join("shared", "collect", "freeze.py"):
        "b68af3015630948fa31e68d7e1d482a5cfe31a2f7399da86f91ab1a30d0a827a",
}


# --------------------------------------------------------------------- 2) 错误面

class ServiceError(Exception):
    """服务层错误的公共基类：``code`` 是机读码，``http`` 是状态码。"""

    code = "INTERNAL"
    http = 500

    def __init__(self, message, hint=""):
        Exception.__init__(self, message)
        self.message = str(message)
        self.hint = str(hint or "")

    def to_body(self):
        return {"error": {"code": type(self).code, "http": type(self).http,
                          "message": self.message, "hint": self.hint}}


class BadJson(ServiceError):
    code, http = "BAD_JSON", 400


class BadParam(ServiceError):
    code, http = "BAD_PARAM", 400


class UnknownParam(ServiceError):
    code, http = "UNKNOWN_PARAM", 400


class ReadOnlyRefused(ServiceError):
    code, http = "READ_ONLY_REFUSED", 400


class PayloadTooLarge(ServiceError):
    code, http = "PAYLOAD_TOO_LARGE", 413


class NetworkRefused(ServiceError):
    code, http = "NETWORK_REFUSED", 403


class NotFound(ServiceError):
    code, http = "NOT_FOUND", 404


class MethodNotAllowed(ServiceError):
    code, http = "METHOD_NOT_ALLOWED", 405


class BlockedNoInputFacts(ServiceError):
    code, http = "BLOCKED_NO_INPUT_FACTS", 409


class BlockedUnknownCase(ServiceError):
    code, http = "BLOCKED_UNKNOWN_CASE", 409


class BlockedBase(ServiceError):
    code, http = "BLOCKED_BASE", 409


class BlockedCoverage(ServiceError):
    code, http = "BLOCKED_COVERAGE", 409


class BlockedInjection(ServiceError):
    code, http = "BLOCKED_INJECTION", 409


class UpstreamUnavailable(ServiceError):
    code, http = "UPSTREAM_UNAVAILABLE", 502


# --------------------------------------------------------------------- 3) 入参校验

#: 写意图字段黑名单：出现在请求体（或 ``scope``）里即拒绝，**不论取值**。
WRITE_INTENT_KEYS = (
    "write", "writer", "writable", "mutate", "mutation", "mutating",
    "insert", "update", "delete", "remove", "drop", "create", "upsert", "replace",
    "patch", "commit", "rollback", "execute", "exec", "apply", "set", "put", "post",
    "grant", "revoke", "truncate", "alter", "ddl", "dml", "admin", "force", "force_write",
)

#: ★ D48：请求体可接受的入参**不含** case；用例由进程级 ``resolve_case_id`` 固定。
ALLOWED_INPUTS = ("layers", "sections", "scope", "max_records", "max_bytes")

#: ``scope`` 允许的收窄键。收窄是「视图」，不是重建：``working_set_sha256`` 不受它影响。
SCOPE_KEYS = ("object_ids", "types", "layers", "metric_codes")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_str_list(name, value, allow_empty):
    if not isinstance(value, list):
        raise BadParam("%s 必须是字符串数组（收到 %s）" % (name, type(value).__name__))
    out = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise BadParam("%s[%d] 必须是非空字符串（收到 %r）" % (name, idx, item))
        out.append(item.strip())
    if not out and not allow_empty:
        raise BadParam("%s 不得为空数组（采集面要求至少一项；要「不限定」就整个省略）" % name)
    return out


def _check_bounded_int(name, value, default, limit):
    if value is None:
        return default
    if not _is_int(value):
        raise BadParam("%s 必须是整数（收到 %s=%r）" % (name, type(value).__name__, value))
    if value < 0:
        raise BadParam("%s 不得为负（收到 %d）；0 是合法边界 = 不返回任何行" % (name, value))
    if value > limit:
        raise BadParam("%s=%d 超过上限 %d（有界语义，不接受无上限请求）" % (name, value, limit))
    return int(value)


def resolve_case_id(explicit=None, environ=None):
    """★ D48：把**进程级**用例选择解析成 case_id 字符串（显式参数优先，其次 env ``M5_CASE_ID``）。

    只在进程启动 / 单条 CLI 调用时求值一次；服务进程内**不可变**、无跨请求状态。
    - 空 ⇒ 运行模式（向采集面真采一次）；非空 ⇒ 判决模式（确定性重建，零网络）。
    - 类型不合法 ⇒ ``BadParam``（不做任何静默回落，避免「以为指定了用例其实没指定」）。
    """
    raw = explicit
    if raw is None:
        env = os.environ if environ is None else environ
        raw = env.get(CASE_ENV_VAR)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise BadParam("case 必须是字符串（收到 %s）；空/缺省 ⇒ 运行模式" % type(raw).__name__)
    return raw.strip()


def normalize_inputs(raw):
    """把请求体 JSON 对象校验并归一化成入参 dict（HTTP 与 CLI 共用同一条路径）。

    ★ D48：入参里**没有** case——用例是进程级的（``resolve_case_id``），请求体里出现
    ``case_id`` 会走下面的未知入参分支被拒绝（``UNKNOWN_PARAM``）。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise BadJson("请求正文必须是 JSON 对象（收到 %s）" % type(raw).__name__)

    for key in raw:
        if str(key).lower() in WRITE_INTENT_KEYS:
            raise ReadOnlyRefused(
                "本工具是只读取数面，不接受写意图字段 %r（不论取值）；"
                "可接受的入参只有 %s" % (key, list(ALLOWED_INPUTS)))
    unknown = [k for k in raw if k not in ALLOWED_INPUTS]
    if unknown:
        raise UnknownParam("未知入参 %s；可接受的入参只有 %s" % (sorted(unknown), list(ALLOWED_INPUTS)))

    layers = raw.get("layers")
    layers = list(DEFAULT_LAYERS) if layers is None else _check_str_list("layers", layers, False)

    # ★ R-56/A（2026-10-12）：层名白名单（**模式无关** —— 判决模式虽忽略 layers，但请求体
    #   里的层名写错同样是调用方的错，早报比晚报好）。未知层名若被原样转发到采集面，
    #   最终以「层 X 事实数为 0」这类覆盖门禁错误收场：调用方无法据此修复。
    unknown_layers = sorted({x for x in layers if x not in KNOWN_LAYERS})
    if unknown_layers:
        raise BadParam("layers 含未知层名 %s（收到 %s）。%s"
                       % (unknown_layers, list(layers), _layer_guidance()))

    sections = raw.get("sections")
    sections = list(DEFAULT_SECTIONS) if sections is None else _check_str_list(
        "sections", sections, True)

    scope = raw.get("scope")
    # ★ R-54 / A1（2026-10-07）：契约把 `scope` 声明为 **string（JSON 对象字符串）**，
    #   因为 Dify 的 type_alias 把 object 折成 string 给模型看，模型只能发字符串；
    #   而本函数原本硬拒非 dict ⇒ 每一个带 scope 的调用必然 400（M5 主跑中止主因）。
    #   ⇒ 这里接受 **两种形态**：
    #     · str  —— 模型面形态：按 JSON 解析；解析失败即 INVALID_PARAM（不静默回落）。
    #     · dict —— 内部/旧调用兼容路径（语义不变）。
    if isinstance(scope, str):
        text = scope.strip()
        if text in ("", "{}", "null", "None"):
            scope = None
        else:
            try:
                scope = json.loads(text)
            except Exception as exc:                       # noqa: BLE001
                raise BadParam(
                    "scope 不是合法 JSON 对象字符串（收到 %r，解析失败：%s）；"
                    "例：{\"types\":[\"Container\"]}" % (text[:120], exc))
            if not isinstance(scope, dict):
                raise BadParam("scope 的 JSON 顶层必须是对象（收到 %s）"
                               % type(scope).__name__)
    if scope is not None:
        if not isinstance(scope, dict):
            raise BadParam("scope 必须是对象或 JSON 对象字符串（收到 %s）"
                           % type(scope).__name__)
        for key in scope:
            if str(key).lower() in WRITE_INTENT_KEYS:
                raise ReadOnlyRefused("scope 里不接受写意图字段 %r（不论取值）" % key)
        bad = [k for k in scope if k not in SCOPE_KEYS]
        if bad:
            raise UnknownParam("scope 里出现未知键 %s；可用键 = %s" % (sorted(bad), list(SCOPE_KEYS)))
        scope = {k: _check_str_list("scope.%s" % k, v, True) for k, v in scope.items()}
        if not any(scope.values()):
            scope = None

    return {
        "layers": layers,
        "sections": sections,
        "scope": scope,
        "max_records": _check_bounded_int("max_records", raw.get("max_records"),
                                          DEFAULT_MAX_RECORDS, MAX_RECORDS_LIMIT),
        "max_bytes": _check_bounded_int("max_bytes", raw.get("max_bytes"),
                                        DEFAULT_MAX_BYTES, MAX_BYTES_LIMIT),
    }


# --------------------------------------------------------------------- 4) 两种模式

def check_runtime_layers(layers):
    """运行模式：``layers`` 必须覆盖 ``expected_profile`` 要求的层（判决模式忽略 layers）。

    ★ R-56/A（2026-10-12）：为什么把它提到「真采之前」而不是留给覆盖门禁去报 ——
      实测（12 个层组合）**缺任一必需层的组合 100% 走 BLOCKED_BASE（覆盖门禁）**，
      而那条报错是「某个类型/某条指标被打到零点」的长散文，**完全没提 layers**：
      调用方（尤其是模型）据此无法知道「该把 layers 补成什么」。
      本检查把同一件事实**提前、准确地**说出来，且 fail-closed 语义不变
      （原本也必然失败，只是失败得更晚、更难懂）。
    """
    required = required_layers()
    missing = [name for name in required if name not in layers]
    if missing:
        raise BadParam("运行模式下 layers 缺少必需的层 %s（收到 %s）。%s"
                       % (missing, list(layers), _layer_guidance()))


def judgement_forbidden_fetcher(layers, sections):
    """判决模式的 fail-closed 采集钩子：一调用就报错（判决模式零网络）。"""
    raise NetworkRefused(
        "判决模式禁止任何采集 / 网络访问：工作集只允许由 %s 的「基底引用 + 注入差分」重建；"
        "本次重建里出现了采集尝试，已 fail-closed 拒绝" % INPUT_FACTS_REL)


def default_fetcher(layers, sections):
    """运行模式的采集：转调 ``live_facts.fetch_live``（地址/入参/超时一律沿用其既有默认）。"""
    return LF.fetch_live(list(layers), sections=list(sections or []) or None,
                         timeout=COLLECT_TIMEOUT_S)


def _classify_live_facts_error(exc, where):
    """把封装层的异常翻译成服务层的机读错误码（保持 fail-closed）。"""
    if isinstance(exc, LF.BaseBlocked):
        return BlockedCoverage("%s：基底覆盖证据不成立 ⇒ BLOCKED（D44 fail-closed）。原始读数：%s"
                               % (where, exc))
    if isinstance(exc, LF.InjectionRefused):
        return BlockedInjection("%s：注入被拒绝 ⇒ BLOCKED（D44 fail-closed）。原始读数：%s"
                                % (where, exc))
    return BlockedBase("%s：基底不可用 ⇒ BLOCKED（fail-closed）。原始读数：%s" % (where, exc))


def _check_declared_working_set(case, ws, case_id):
    """方案 §5：用例声明的 ``working_set_sha256`` 必须与本次重建逐字相同（否则 fail-closed）。"""
    declared = case.get("working_set_sha256")
    if not declared:
        return None
    actual = (ws.get("meta") or {}).get("working_set_sha256")
    if str(declared) != str(actual):
        raise BlockedInjection(
            "case_id=%s 声明的 working_set_sha256=%s 与重建实算 %s 不一致 ⇒ BLOCKED"
            "（基底 + 已声明差分 ≠ 声明的工作集）" % (case_id, declared, actual))
    return actual


def judgement_working_set(case_id, input_facts_path=None, fetch=None):
    """判决模式：``m5-input-facts.yaml`` 的基底 + 该 case 的注入差分 → 工作集（零网络）。"""
    fetch = fetch or judgement_forbidden_fetcher
    path = os.path.abspath(input_facts_path or DEFAULT_INPUT_FACTS)
    if not os.path.isfile(path):
        raise BlockedNoInputFacts(
            "判决模式需要用例文件 %s，该文件当前不存在 ⇒ BLOCKED（fail-closed，不退化成空集、"
            "也不退化成运行模式）。该文件由 S4 的注入构造器产出。" % path)
    try:
        base = LF.load_base(path, expected_profile=DEFAULT_PROFILE)
    except LF.LiveFactsError as exc:
        raise _classify_live_facts_error(exc, "判决模式载入 %s" % path)
    cases = list(base.get("cases") or [])
    case = None
    for item in cases:
        if isinstance(item, dict) and str(item.get("case_id") or "") == str(case_id):
            case = item
            break
    if case is None:
        known = [str(c.get("case_id")) for c in cases if isinstance(c, dict)][:8]
        raise BlockedUnknownCase(
            "case_id=%r 不在 %s 的 cases 里（该文件共 %d 例，前 8 个 = %s）⇒ BLOCKED"
            % (case_id, path, len(cases), known))
    try:
        ws = LF.build_working_set(base, case)
    except LF.LiveFactsError as exc:
        raise _classify_live_facts_error(exc, "判决模式重建 case_id=%s" % case_id)
    _check_declared_working_set(case, ws, case_id)
    ccf = base.get("ccf") or {}
    return ws, ccf.get("capturedAt")


def runtime_working_set(layers, sections, fetch=None):
    """运行模式：真采一次 → 当场归一化成 CCF → 请求级工作集（无注入）。"""
    fetch = fetch or default_fetcher
    try:
        envelope = fetch(list(layers), list(sections or []))
    except LF.LiveFactsError as exc:
        raise UpstreamUnavailable("采集面调用失败 ⇒ 本次读数不可用：%s" % exc)
    except ServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 采集面的任何异常都不许变成 500 之外的静默空集
        raise UpstreamUnavailable("采集面调用异常（%s）：%s" % (type(exc).__name__, exc))
    if not isinstance(envelope, dict):
        raise UpstreamUnavailable("采集面回执不是 JSON 对象（收到 %s）" % type(envelope).__name__)
    tmpdir = tempfile.mkdtemp(prefix="read-facts-")
    try:
        path = os.path.join(tmpdir, "gather.json")
        with io.open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(LF.canonical_json(envelope))
        try:
            base = LF.load_base(path, expected_profile=DEFAULT_PROFILE)
        except LF.LiveFactsError as exc:
            raise _classify_live_facts_error(exc, "运行模式归一化采集回执")
        try:
            ws = LF.build_working_set(base, {"case_id": None, "injections": []})
        except LF.LiveFactsError as exc:
            raise _classify_live_facts_error(exc, "运行模式构建工作集")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    ccf = base.get("ccf") or {}
    return ws, ccf.get("capturedAt")


# --------------------------------------------------------------------- 5) 来源与收窄

def _parse_epoch(text):
    """把 ISO-8601 时间戳解析成 epoch 秒；解析不了返回 None（调用方按 0 秒处理）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        stamp = datetime.datetime.fromisoformat(candidate)
    except ValueError:
        stamp = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                stamp = datetime.datetime.strptime(raw[:26].rstrip("Z"), fmt)
                break
            except ValueError:
                continue
        if stamp is None:
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def facts_source(mode, ws, captured_at, as_of=None):
    """来源可见：``request`` / ``snapshot(Ns_ago)`` / ``empty``。

    ``as_of`` 是**纯函数入参**（不是客户端入参）：

    * 缺省（HTTP 服务）⇒ 用墙钟，``N`` 是「快照捕获时刻距今多少秒」；
    * ``"snapshot"`` ⇒ 时间锚 = 快照自身捕获时刻，``N`` 恒为 0，输出与墙钟无关
      （``--dump-case`` 用它，保证逐字节可复现）。
    """
    count = sum(len(ws.get(key) or []) for key in ROW_KEYS)
    if count == 0:
        return SOURCE_EMPTY
    if mode == MODE_RUNTIME:
        return SOURCE_REQUEST
    age = 0
    if as_of != "snapshot":
        base_epoch = _parse_epoch(captured_at)
        if base_epoch is not None:
            now = time.time() if as_of is None else float(as_of)
            age = max(0, int(now - base_epoch))
    return "snapshot(%ds_ago)" % age


def _apply_scope(rows, scope):
    """按 ``scope`` 收窄三段（视图，不改工作集）：返回 ``(新三段, scope_out 计数)``。"""
    before = sum(len(rows[key]) for key in ROW_KEYS)
    if not scope:
        return rows, 0
    want_ids = set(scope.get("object_ids") or [])
    want_types = set(scope.get("types") or [])
    want_layers = set(scope.get("layers") or [])
    want_metrics = set(scope.get("metric_codes") or [])
    kept = []
    for obj in rows["objects"]:
        if want_ids and str(obj.get("id")) not in want_ids:
            continue
        if want_types and str(obj.get("type")) not in want_types:
            continue
        if want_layers and str(obj.get("layer")) not in want_layers:
            continue
        kept.append(obj)
    live = {str(o.get("id")) for o in kept}
    relations = [r for r in rows["relations"]
                 if str(r.get("from")) in live and str(r.get("to")) in live]
    observations = [o for o in rows["observations"]
                    if str(o.get("objectId")) in live
                    and (not want_metrics or str(o.get("metricCode")) in want_metrics)]
    out = {"objects": kept, "relations": relations, "observations": observations}
    after = sum(len(out[key]) for key in ROW_KEYS)
    return out, before - after


def dangling_refs(rows):
    """三段里的悬挂引用数（返回 ``(悬挂关系数, 悬挂观测数)``）。"""
    ids = {str(o.get("id")) for o in rows.get("objects") or []}
    rel = sum(1 for r in rows.get("relations") or []
              if str(r.get("from")) not in ids or str(r.get("to")) not in ids)
    obs = sum(1 for o in rows.get("observations") or []
              if str(o.get("objectId")) not in ids)
    return rel, obs


# --------------------------------------------------------------------- 6) 有界表示

def _rows_bytes(objects, relations, observations):
    body = {"objects": objects, "relations": relations, "observations": observations}
    return len(LF.canonical_json(body).encode("utf-8"))


def bound_rows(rows, max_records, max_bytes):
    """有界表示：先按 ``max_records`` 限行，再按 ``max_bytes`` 压表示 / 丢尾行。

    语义（冻结）：

    * ``max_records`` = **三段行数之和**的上限，填充顺序固定 ``对象 → 关系 → 观测``。
      这个顺序保证任何被保留的关系，其两端对象一定也被保留 ⇒ 返回值永不悬挂；
      0 是**显式边界**：不返回任何行（不是「无上限」）；
    * ``max_bytes`` = 三段行容器（``objects``+``relations``+``observations`` 的 canonical
      JSON）字节上限。先按 ``MAX_STR_LADDER`` 硬截长字符串（照抄 ``live_facts.aggregate``
      的字符串硬截语义），仍超标再按 ``观测尾 → 关系尾 → 对象尾（连带其观测与关系）``
      的顺序丢行，直到达标或全空；
    * ``droppedRows`` = ``max_records`` 与 ``max_bytes`` 两级一共丢弃的**行数**；
    * ``droppedBytes`` = 相对「已过 ``max_records``、尚未压表示」那一刻的**字节净损失**
      （表示压缩 + 字节驱动的丢行），恒满足 ``droppedBytes == bounded_bytes - final_bytes``；
    * ``truncated`` = 任一丢弃量非零。
    """
    objects = list(rows.get("objects") or [])
    relations = list(rows.get("relations") or [])
    observations = list(rows.get("observations") or [])
    rows_total = len(objects) + len(relations) + len(observations)
    original_bytes = _rows_bytes(objects, relations, observations)

    keep_objects = min(len(objects), max_records)
    remaining = max_records - keep_objects
    keep_relations = min(len(relations), remaining)
    remaining -= keep_relations
    keep_observations = min(len(observations), remaining)

    objects = objects[:keep_objects]
    relations = relations[:keep_relations]
    observations = observations[:keep_observations]
    bounded_bytes = _rows_bytes(objects, relations, observations)
    droppedRows = rows_total - (len(objects) + len(relations) + len(observations))

    counter = [0]
    max_str_used = None
    if bounded_bytes > max_bytes:
        for candidate in MAX_STR_LADDER:
            objects = LF._trunc_deep(objects, candidate, counter)  # noqa: SLF001 —— 复用既有硬截语义
            relations = LF._trunc_deep(relations, candidate, counter)  # noqa: SLF001
            observations = LF._trunc_deep(observations, candidate, counter)  # noqa: SLF001
            max_str_used = candidate
            if _rows_bytes(objects, relations, observations) <= max_bytes:
                break

    if _rows_bytes(objects, relations, observations) > max_bytes:
        while observations and _rows_bytes(objects, relations, observations) > max_bytes:
            observations = observations[:-1]
            droppedRows += 1
        while relations and _rows_bytes(objects, relations, observations) > max_bytes:
            relations = relations[:-1]
            droppedRows += 1
        while objects and _rows_bytes(objects, relations, observations) > max_bytes:
            objects = objects[:-1]
            droppedRows += 1
            live = {str(o.get("id")) for o in objects}
            keep_obs = [o for o in observations if str(o.get("objectId")) in live]
            keep_rel = [r for r in relations
                        if str(r.get("from")) in live and str(r.get("to")) in live]
            droppedRows += (len(observations) - len(keep_obs)) + (len(relations) - len(keep_rel))
            observations, relations = keep_obs, keep_rel

    final_bytes = _rows_bytes(objects, relations, observations)
    droppedBytes = max(0, bounded_bytes - final_bytes)
    return {
        "objects": objects,
        "relations": relations,
        "observations": observations,
        "rows_total": rows_total,
        "rows_out": len(objects) + len(relations) + len(observations),
        "original_bytes": original_bytes,
        "bounded_bytes": bounded_bytes,
        "final_bytes": final_bytes,
        "droppedRows": droppedRows,
        "droppedBytes": droppedBytes,
        "strings_truncated": counter[0],
        "max_str_used": max_str_used,
        "truncated": bool(droppedRows > 0 or droppedBytes > 0),
    }


# --------------------------------------------------------------------- 7) 取数主函数

def _rows_of(ws):
    rows = {}
    for key in ROW_KEYS:
        value = ws.get(key)
        if not isinstance(value, list):
            raise ServiceError("工作集的 %r 不是列表（内部错误）" % key)
        rows[key] = value
    return rows


def _hint(mode, case_id, inputs, bounded, scope_out, facts_src):
    parts = [
        "mode=%s" % mode,
        "case_id=%s" % (case_id or ""),
        "layers=%s" % ",".join(inputs["layers"]),
        "sections=%d" % len(inputs["sections"]),
        "scope_out=%d" % scope_out,
        "facts_source=%s" % facts_src,
        "rows_total=%d" % bounded["rows_total"],
        "rows_out=%d" % bounded["rows_out"],
        "rows_bytes=%d" % bounded["final_bytes"],
        "original_bytes=%d" % bounded["original_bytes"],
        "truncated=%s" % ("true" if bounded["truncated"] else "false"),
        "droppedRows=%d" % bounded["droppedRows"],
        "droppedBytes=%d" % bounded["droppedBytes"],
        "max_records=%d" % inputs["max_records"],
        "max_bytes=%d" % inputs["max_bytes"],
    ]
    if bounded["max_str_used"] is not None:
        parts.append("max_str=%s" % int(bounded["max_str_used"]))
    if bounded["strings_truncated"]:
        parts.append("strings_truncated=%d" % bounded["strings_truncated"])
    if bounded["truncated"]:
        parts.append("note=本次返回不完整：三段被压到 max_records/max_bytes 以内，"
                     "原始 %d 字节、丢弃 %d 行 / %d 字节，丢弃量见 droppedRows/droppedBytes"
                     % (bounded["original_bytes"], bounded["droppedRows"],
                        bounded["droppedBytes"]))
    return "; ".join(parts)


def read_facts(raw=None, input_facts_path=None, as_of=None, fetch=None, case_id=None):
    """纯函数取数：同一入参 ⇒ 同一字节（``as_of`` / 进程级用例固定时）。

    ``raw`` 是请求体 JSON 对象（可缺省 = 全默认，**不含 case 参数**）。
    ``case_id`` / ``as_of`` / ``fetch`` 是**进程级或内部参数**，不经 HTTP 请求体暴露：
    ``case_id`` 由 ``--case`` / env ``M5_CASE_ID`` 在进程启动时固定（本参数为 None ⇒ 回落 env），
    ``as_of`` / ``fetch`` 只服务于 ``--dump-case`` 与自检。
    """
    inputs = normalize_inputs(raw)
    case_id = resolve_case_id(case_id)
    if case_id:
        ws, captured_at = judgement_working_set(case_id, input_facts_path, fetch)
        mode = MODE_JUDGEMENT
    else:
        # ★ R-56/A：运行模式 ⇒ layers 必须覆盖 expected_profile 要求的层（判决模式忽略 layers）
        check_runtime_layers(inputs["layers"])
        ws, captured_at = runtime_working_set(inputs["layers"], inputs["sections"], fetch)
        mode = MODE_RUNTIME

    facts_src = facts_source(mode, ws, captured_at, as_of)
    rows, scope_out = _apply_scope(_rows_of(ws), inputs["scope"])
    bounded = bound_rows(rows, inputs["max_records"], inputs["max_bytes"])
    meta = ws.get("meta") or {}
    return {
        "objects": bounded["objects"],
        "relations": bounded["relations"],
        "observations": bounded["observations"],
        # ★ S3a-2c-1（真机 M4-C6 暴露的产物缺陷修复）：公共信封四字段**平铺在返回体顶层**。
        #   权威依据：shared/model/tools-setb.yaml 的 return_envelope{applies_to: all_tools}
        #   （manifest_rule：每条 tools[] 项的 returns 必须显式含这 4 个字段名），以及
        #   消费方的顶层读法 shared/verify/guard_conformance.py:583-589
        #   （payload.get("truncated") / ("droppedRows") / ("droppedBytes")）。
        #   此前这 4 个字段嵌在 "envelope" 子对象里 ⇒ 四臂 manifest 声明的
        #   returns 是 8 个平铺名字，实现却只平铺 4 个 ⇒ 声明面与实现面真实不符，
        #   真机 contract_audit M4-C6 判「缺 4 字段信封」FAIL。语义一字未改。
        "hint": _hint(mode, case_id, inputs, bounded, scope_out, facts_src),
        "truncated": bounded["truncated"],
        "droppedRows": bounded["droppedRows"],
        "droppedBytes": bounded["droppedBytes"],
        "source": {
            "snapshot_id": meta.get("snapshot_id"),
            "canonical_sha256": meta.get("canonical_sha256"),
            "working_set_sha256": meta.get("working_set_sha256"),
            "facts_source": facts_src,
        },
    }


def dump_bytes(raw=None, input_facts_path=None, as_of="snapshot", fetch=None, case_id=None):
    """``--dump-case`` 的落盘载荷：canonical JSON + 单个 LF，逐字节可复现。"""
    payload = read_facts(raw, input_facts_path=input_facts_path, as_of=as_of, fetch=fetch,
                         case_id=case_id)
    return (LF.canonical_json(payload) + "\n").encode("utf-8")


# --------------------------------------------------------------------- 8) /health

def health_payload(port=None, host=None, started_at=None, input_facts_path=None):
    """自述载荷：服务版本 / 端口 / 模式可用性 / 用例文件状态。**不回显任何凭据与环境变量**。"""
    path = os.path.abspath(input_facts_path or DEFAULT_INPUT_FACTS)
    present = os.path.isfile(path)
    facts = {"path": path, "present": present, "schema": INPUT_FACTS_SCHEMA}
    if present:
        fp = LF.fingerprint_file(path)
        facts.update({"bytes": fp["bytes"], "lf": fp["lf"],
                      "sha256": fp["sha256"], "sha256_prefix": fp["sha256"][:16]})
    # ★ 2026-10-10：/health 暴露**快照身份**，供跑器「用例集 ↔ 载体在线快照」的 fail-closed 同源断言使用。
    #   取值一律来自本服务**已经在加载**的那份 ledger（m5-input-facts.yaml 的 meta），不猜、不编造；
    #   取不到就**不报该字段**（跑器据此 BLOCKED，而不是被一个假值放行）。
    snap = {}
    if present:
        try:
            _base = LF.load_base(path)
            _sid = _base.get("snapshot_id")
            if _sid:
                snap = {"snapshotId": _sid, "snapshot_id": _sid,
                        "canonical_sha256": _base.get("canonical_sha256")}
        except Exception:  # noqa: BLE001 —— 只读自述面：解析失败不影响服务本身
            snap = {}
    started = started_at if started_at is not None else time.time()
    payload = {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "tool": TOOL_NAME,
        "tool_path": TOOL_PATH,
        "health_path": HEALTH_PATH,
        "host": host,
        "port": port,
        "started_at": "%s" % datetime.datetime.fromtimestamp(
            started, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uptime_s": max(0, int(time.time() - started)),
        "modes": {
            "judgement": {"available": bool(present),
                          "requires": INPUT_FACTS_REL,
                          "reason": "" if present else "用例文件不存在 ⇒ 判决模式 fail-closed BLOCKED"},
            "runtime": {"available": True,
                        "requires": "POST /tools/gather（地址与入参沿用 live_facts 既有默认）"},
        },
        "input_facts": facts,
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "default_max_records": DEFAULT_MAX_RECORDS,
            "max_records_limit": MAX_RECORDS_LIMIT,
            "default_max_bytes": DEFAULT_MAX_BYTES,
            "max_bytes_limit": MAX_BYTES_LIMIT,
            "default_layers": list(DEFAULT_LAYERS),
            # ★ R-56/A：把「合法层名」与「运行模式必须覆盖的层」自述出来，
            #   供调用方/门禁核对（此前这两个事实只存在于采集团队的口头知识里）。
            "known_layers": list(KNOWN_LAYERS),
            "required_layers": list(required_layers()),
            "max_str_ladder": [int(x) for x in MAX_STR_LADDER],
        },
        "faces": {
            "read_only": True,
            "write_endpoints": 0,
            "cross_request_state": "none",
            "facts_source_enum": [SOURCE_REQUEST, "snapshot(Ns_ago)", SOURCE_EMPTY],
            "profile": DEFAULT_PROFILE,
            "collect_timeout_s": COLLECT_TIMEOUT_S,
        },
    }
    payload.update(snap)
    return payload


# --------------------------------------------------------------------- 9) HTTP 面

def _json_bytes(payload):
    return LF.canonical_json(payload).encode("utf-8")


def _headers():
    return {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Service": SERVICE_NAME,
        "X-Service-Version": SERVICE_VERSION,
        "X-Tool": TOOL_NAME,
    }


def handle_request(method, path, body=b"", input_facts_path=None, as_of=None,
                   started_at=None, port=None, host=None, fetch=None, case_id=None):
    """把一个 HTTP 请求映射成 ``(status, headers, body_bytes)``（纯函数，便于离线自检）。

    ★ D48：``case_id`` 是**进程级**参数（由 ``serve`` 在启动时定死），不来自请求体；请求体里
    出现 ``case_id`` 按未知入参拒绝（``UNKNOWN_PARAM``）。
    """
    route = str(path or "").split("?", 1)[0]
    method = str(method or "").upper()
    try:
        if route == TOOL_PATH:
            if method != "POST":
                raise MethodNotAllowed("端点 %s 只接受 POST（收到 %s）；本服务是只读取数面，"
                                       "没有任何写方法" % (TOOL_PATH, method))
            if len(body) > MAX_BODY_BYTES:
                raise PayloadTooLarge("请求正文 %d 字节 > 上限 %d 字节" % (len(body), MAX_BODY_BYTES))
            if body:
                try:
                    raw = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise BadJson("请求正文不是合法 UTF-8 JSON：%s" % exc)
            else:
                raw = {}
            payload = read_facts(raw, input_facts_path=input_facts_path, as_of=as_of,
                                 fetch=fetch, case_id=case_id)
            return 200, _headers(), _json_bytes(payload)
        if route == HEALTH_PATH:
            if method not in ("GET", "HEAD"):
                raise MethodNotAllowed("端点 %s 只接受 GET（收到 %s）" % (HEALTH_PATH, method))
            payload = health_payload(port=port, host=host, started_at=started_at,
                                     input_facts_path=input_facts_path)
            return 200, _headers(), _json_bytes(payload)
        raise NotFound("未知路径 %r；本服务只暴露 %s（POST）与 %s（GET）"
                       % (route, TOOL_PATH, HEALTH_PATH))
    except ServiceError as exc:
        return type(exc).http, _headers(), _json_bytes(exc.to_body())
    except Exception as exc:  # noqa: BLE001 —— 任何未预期异常都不许漏成非 JSON
        err = ServiceError("未预期内部错误（%s）：%s" % (type(exc).__name__, exc),
                           hint="请把这条读数连同 /health 一起回报；不涉及任何写操作。")
        return err.http, _headers(), _json_bytes(err.to_body())


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVICE_NAME + "/" + SERVICE_VERSION

    def _dispatch(self, method):
        cfg = getattr(self.server, "cfg", {})
        too_long = False
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length > MAX_BODY_BYTES:
            too_long = True
            body = b"x" * (MAX_BODY_BYTES + 1)
        elif length > 0:
            body = self.rfile.read(length)
        else:
            body = b""
        status, headers, out = handle_request(method, self.path, body, **cfg)
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(out)
        if too_long:
            self.close_connection = True

    def do_GET(self):      # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self):     # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self):     # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):      # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self):   # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self):    # noqa: N802
        self._dispatch("PATCH")

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[%s] %s %s\n" % (SERVICE_NAME, self.address_string(), fmt % args))
        sys.stderr.flush()


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT, input_facts_path=None, as_of=None,
          started_at=None, case_id=None):
    """启动只读服务（前台阻塞；无任何跨请求状态）。"""
    case_id = resolve_case_id(case_id)          # ★ D48：进程启动时定死，此后不可变
    cfg = {"input_facts_path": input_facts_path, "as_of": as_of, "case_id": case_id,
           "started_at": started_at if started_at is not None else time.time(),
           "port": port, "host": host}
    httpd = _Server((host, port), _Handler)
    httpd.cfg = cfg
    sys.stderr.write("%s %s 已启动：http://%s:%d%s（POST）· http://%s:%d%s（GET）"
                     " · 判决模式=%s · as_of=%s · case=%s\n"
                     % (SERVICE_NAME, SERVICE_VERSION, host, port, TOOL_PATH,
                        host, port, HEALTH_PATH,
                        "available" if os.path.isfile(os.path.abspath(
                            input_facts_path or DEFAULT_INPUT_FACTS)) else "BLOCKED",
                        as_of or "live",
                        case_id or "(未固定 ⇒ 运行模式)"))
    sys.stderr.flush()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------- 10) 工具 schema

def _error_code_table():
    """错误码表**从异常类本身推导**（单一来源），避免契约与实现漂移。"""
    table = {}

    def walk(cls):
        for sub in cls.__subclasses__():
            table[sub.code] = sub.http
            walk(sub)

    walk(ServiceError)
    table[ServiceError.code] = ServiceError.http
    return table


def tool_schema():
    """本工具的机读契约（供 S2b 写门禁 / S3 注册到 App 侧）。字段名一律来自本模块常量。"""
    return {
        "name": TOOL_NAME,
        # ★ S3a-2c：实现自述必须与形状权威（shared/model/live-facts-tools.yaml#read_facts.description，
        #   其文本同时等于 tools-setb.yaml#tools.read_facts.description）逐字同源 —— 该纪律写在形状
        #   文件的 description_discipline 里。此前两处是「同一操作的两种说法」，且「CCF 归一化 …
        #   有界信封」把已平铺到顶层的公共信封说成嵌套对象，属实不符。
        "description": "只读取数：按本进程固定的用例确定性重建（判决模式），或在未固定用例时"
                       "向采集面真采一次（运行模式），返回有界的当前读数三段与公共信封。"
                       "无结果时返回 hint 说明可能原因；返回 0 行或 hint 指出作用域不符时，"
                       "不得据此断言对象或读数不存在。",
        "endpoint": {"method": "POST", "path": TOOL_PATH},
        "health_endpoint": {"method": "GET", "path": HEALTH_PATH},
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "layers": {"type": "array", "items": {"type": "string"},
                           "default": list(DEFAULT_LAYERS), "injected_by": "llm",
                           # ★ R-58 / 方案C：本字段是**模型唯一能读到的取值说明**
                           #   （注册面 = live-facts-tools.yaml#read_facts.params.layers.description
                           #   → register_tools.py 的 params_from_entry）；必须与该文件**逐字相同**，
                           #   否则「声明面 ↔ 实现自述」两处说法不一致。改这里要同批重算 emit_schema。
                           "description": "采集层：只决定「去采集哪些层」，不是收窄返回范围的旋钮。不传 = 四层全采。合法取值只有 gaussdb / os / container / app（另有 storage / tpops，其读数不进事实层，通常无需使用）；★ 运行模式（本进程未固定用例）下必须至少包含 gaussdb 与 os，否则覆盖校验必然失败。★ 要按对象类型 / 对象 id / 层收窄返回结果，请用 scope 参数，不要用本参数。"},
                "sections": {"type": "array", "items": {"type": "string"}, "default": [],
                             "injected_by": "llm", "description": "可选，透传给采集面。"},
                "scope": {"type": "string", "injected_by": "llm",
                          # ★ R-54 / A1：模型面类型必须与服务面一致（Dify type_alias 把 object 折成 string）
                          # ★ R-58：本文本由自检 c19c 断言与形状权威
                          #   `live-facts-tools.yaml#read_facts.params.scope.description` **逐字相同**。
                          #   2026-10-12 首次运行该断言即抓到**既有漂移**：实现自述比声明面少了
                          #   「或 {"object_ids":[…]}.」与「键名未知/值非字符串数组/非合法 JSON ⇒
                          #   INVALID_PARAM（不静默回落默认值）」两句 —— 声明面是注册给模型的那一份，
                          #   故以**声明面为准**把实现自述补齐（YAML 的 `>-` 折行会保留一个空格，
                          #   下面的字符串逐字复刻该折行结果）。
                          "description": "限定对象范围（只做收窄，不改工作集，不影响 working_set_sha256）。 "
                                         "传**一个 JSON 对象字符串**：键只能取 object_ids / types / layers / metric_codes， "
                                         "值为字符串数组；例 {\"types\":[\"Container\"]} 或 "
                                         "{\"object_ids\":[\"appinst:appinst-cc-pay-1\"]}。 "
                                         "不传、传 null 或传 \"{}\" 都表示不收窄。键名未知、值不是字符串数组、"
                                         "或不是合法 JSON ⇒ INVALID_PARAM（**不静默回落默认值**）。"},
                "max_records": {"type": "integer", "default": DEFAULT_MAX_RECORDS,
                                "minimum": 0, "maximum": MAX_RECORDS_LIMIT,
                                "injected_by": "llm", "description": "三段行数之和上限；0 = 不返回任何行。"},
                "max_bytes": {"type": "integer", "default": DEFAULT_MAX_BYTES,
                              "minimum": 0, "maximum": MAX_BYTES_LIMIT,
                              "injected_by": "llm", "description": "三段行容器字节上限。"},
            },
            "required": [],
        },
        "returns": {
            # ★ S3a-2c-1：公共信封四字段与工具级字段**同为顶层名**（8 个平铺名字），
            #   与 shared/model/live-facts-tools.yaml 的 returns{tool_level ∪ envelope}
            #   分类逐名同源；不再有 "envelope" 嵌套对象。
            "objects": "array", "relations": "array", "observations": "array",
            "hint": "string", "truncated": "boolean",
            "droppedRows": "integer", "droppedBytes": "integer",
            "source": {"snapshot_id": "string", "canonical_sha256": "string",
                       "working_set_sha256": "string", "facts_source": "string"},
        },
        "error_codes": _error_code_table(),
        "read_only": True,
    }


# --------------------------------------------------------------------- 11) 自检夹具

FIXTURE_CASE = "S2A-FIXTURE-1"
FIXTURE_CASE_NOOP = "S2A-FIXTURE-NOOP"
FIXTURE_CASE_PARTIAL = "S2A-FIXTURE-PARTIAL"
FIXTURE_CASE_BADVALUE = "S2A-FIXTURE-BADVALUE"
FIXTURE_UNKNOWN_CASE = "S2A-FIXTURE-ABSENT-9"
#: ``live_facts.fixture_base()`` 的三段总行数（objects 14 + relations 15 + observations 31）。
#: 自检用它做截断记账断言；若上游夹具变了，c26 的上游指纹断言会先亮红。
FIXTURE_ROWS_TOTAL = 60


def default_fixture_dir():
    return os.path.join(_ROOT, "tmp", "_m5_s2a_work", "fixture")


def _write_bytes(path, blob):
    with io.open(path, "wb") as handle:
        handle.write(blob)


def build_fixture(fixture_dir=None):
    """手写临时基底 + 注入差分夹具（**只在 tmp 下自建目录**，不碰任何既有文件）。"""
    import yaml  # noqa: PLC0415 —— 只用于写自检夹具

    root = os.path.abspath(fixture_dir or default_fixture_dir())
    if not os.path.isdir(root):
        os.makedirs(root)
    base = LF.fixture_base()
    ccf = copy.deepcopy(base["ccf"])

    base_path = os.path.join(root, "base_ccf.json")
    _write_bytes(base_path, (LF.canonical_json(ccf) + "\n").encode("utf-8"))

    settable = [o for o in ccf["observations"]
                if o.get("status") == "collected" and isinstance(o.get("value"), (int, float))
                and not isinstance(o.get("value"), bool)]
    pick = settable[0]
    obj = next(o for o in ccf["objects"] if o.get("id") == pick.get("objectId"))
    injections = [{
        "op": "set_fact",
        "target": {"obj_type": obj.get("type"), "obj_id": obj.get("id"),
                   "path": pick.get("metricCode")},
        "value_before": pick.get("value"),
        "value_after": pick.get("value") + 1,
        "constructed_at": "2026-01-02T00:00:00Z",
    }]
    # 只删一个对象：传播 = 连删其观测 + 以它为端点的关系。全删会得到空图，而空图带着
    # 「objects 为空（空图）」这个非覆盖族错误码，过不了 build_working_set 的机检
    # （live_facts.py:819 只放行 X8-coverage / G5-coverage）⇒ 夹具只做部分删除。
    victim = next(o for o in ccf["objects"] if o.get("type") == "Session")
    remove_partial = [{"op": "remove_object",
                       "target": {"obj_type": victim.get("type"), "obj_id": victim.get("id")}}]
    inj_by_case = ((FIXTURE_CASE, injections), (FIXTURE_CASE_NOOP, []),
                   (FIXTURE_CASE_PARTIAL, remove_partial))

    in_memory = {}
    for case_id, inj in inj_by_case:
        ws = LF.build_working_set(base, {"case_id": case_id, "injections": inj})
        in_memory[case_id] = ws["meta"]["working_set_sha256"]

    cases = [
        {"case_id": FIXTURE_CASE, "injections": injections,
         "working_set_sha256": in_memory[FIXTURE_CASE], "constructed_at": "2026-01-02T00:00:00Z"},
        {"case_id": FIXTURE_CASE_NOOP, "injections": [],
         "working_set_sha256": in_memory[FIXTURE_CASE_NOOP]},
        {"case_id": FIXTURE_CASE_PARTIAL, "injections": remove_partial,
         "working_set_sha256": in_memory[FIXTURE_CASE_PARTIAL]},
        {"case_id": FIXTURE_CASE_BADVALUE, "injections": [
            {"op": "set_fact",
             "target": {"obj_type": obj.get("type"), "obj_id": obj.get("id"),
                        "path": pick.get("metricCode")},
             "value_before": "基底里并不存在的取值-9e9",
             "value_after": 1}]},
    ]
    doc = {
        "schema": INPUT_FACTS_SCHEMA,
        "meta": {"snapshot_id": ccf.get("snapshotId"),
                 "canonical_sha256": (ccf.get("integrity") or {}).get("canonicalSha256"),
                 "captured_at": ccf.get("capturedAt"),
                 "base_ref": os.path.basename(base_path),
                 "coverage_checked": True,
                 "note": "S2a 自检夹具：确定性基底 + 声明差分，不是交付的用例文件"},
        "cases": cases,
    }
    yaml_path = os.path.join(root, "m5-input-facts.yaml")
    with io.open(yaml_path, "w", encoding="utf-8", newline="") as handle:
        yaml.safe_dump(doc, handle, allow_unicode=True, sort_keys=True, default_flow_style=False)

    # 覆盖门禁负控：把观测切掉一半后重算自洽的 integrity ⇒ 覆盖证据 ok=False。
    nocov = copy.deepcopy(ccf)
    nocov["observations"] = nocov["observations"][:3]
    ids = {o.get("id") for o in nocov["objects"]}
    nocov["relations"] = [r for r in nocov["relations"]
                          if r.get("from") in ids and r.get("to") in ids]
    LF.refresh_ccf_integrity(nocov)
    nocov_coverage = (nocov.get("integrity") or {}).get("expectedCoverage") or {}
    nocov_path = os.path.join(root, "base_ccf_nocoverage.json")
    _write_bytes(nocov_path, (LF.canonical_json(nocov) + "\n").encode("utf-8"))
    nocov_doc = {
        "schema": INPUT_FACTS_SCHEMA,
        "meta": {"snapshot_id": nocov.get("snapshotId"),
                 "canonical_sha256": (nocov.get("integrity") or {}).get("canonicalSha256"),
                 "captured_at": nocov.get("capturedAt"),
                 "base_ref": os.path.basename(nocov_path),
                 "coverage_checked": False,
                 "note": "S2a 覆盖门禁负控夹具：覆盖证据 ok=False（预期 BLOCKED）"},
        "cases": [{"case_id": FIXTURE_CASE_NOOP, "injections": []}],
    }
    nocov_yaml = os.path.join(root, "m5-input-facts-nocoverage.yaml")
    with io.open(nocov_yaml, "w", encoding="utf-8", newline="") as handle:
        yaml.safe_dump(nocov_doc, handle, allow_unicode=True, sort_keys=True,
                       default_flow_style=False)

    return {
        "dir": root,
        "yaml": yaml_path,
        "base": base_path,
        "nocoverage_yaml": nocov_yaml,
        "nocoverage_base": nocov_path,
        "cases": [c["case_id"] for c in cases],
        "coverage_ok": (ccf.get("integrity") or {}).get("expectedCoverage", {}).get("ok"),
        "coverage_ok_nocov": nocov_coverage.get("ok"),
        "rows_total": (len(ccf["objects"]) + len(ccf["relations"]) + len(ccf["observations"])),
        "working_set_sha256": in_memory,
        "base_canonical_sha256": (ccf.get("integrity") or {}).get("canonicalSha256"),
        "pick": {"object": obj.get("id"), "metric": pick.get("metricCode"),
                 "value": pick.get("value")},
    }


# --------------------------------------------------------------------- 12) 自检驱动

class _Checks(object):
    """``OK`` / ``BAD`` / ``BLOCKED`` 三态收集器（FAIL 一律不得写成 OK）。

    ``fixture_ok=False``（夹具没落盘）时，夹具相关控制项抛出的 ``KeyError`` 记
    **BLOCKED** 而不是 BAD —— 那是「没验」，不是「验出问题」，不能混为一谈。
    """

    def __init__(self, fixture_ok=True):
        self.items = []
        self.fixture_ok = bool(fixture_ok)

    def ok(self, name, why=""):
        self.items.append((name, True, why))

    def bad(self, name, why):
        self.items.append((name, False, why))

    def blocked(self, name, why):
        self.items.append((name, None, why))

    def run(self, name, fn):
        try:
            fn()
        except AssertionError as exc:
            self.bad(name, "%s" % exc)
        except Exception as exc:  # noqa: BLE001
            if not self.fixture_ok and isinstance(exc, (KeyError, TypeError, OSError)):
                self.blocked(name, "夹具未落盘 ⇒ 未验：%s: %s" % (type(exc).__name__, exc))
            else:
                self.bad(name, "%s: %s" % (type(exc).__name__, exc))
        else:
            self.ok(name)

    def expect(self, name, fn, exc_type, *needles):
        try:
            fn()
        except exc_type as exc:
            text = getattr(exc, "message", None) or str(exc)
            miss = [n for n in needles if n not in text]
            if miss:
                self.bad(name, "异常文案缺 %r：%s" % (miss, text[:400]))
            else:
                self.ok(name)
        except Exception as exc:  # noqa: BLE001
            self.bad(name, "异常类型不对：%s: %s" % (type(exc).__name__, exc))
        else:
            self.bad(name, "本应抛 %s 却没有抛" % getattr(exc_type, "__name__", exc_type))


def _status_of_handle(method, path, body=b"", **kwargs):
    status, _headers_out, out = handle_request(method, path, body, **kwargs)
    return status, out


def selftest(verbose=True, fixture_dir=None):
    """真跑自检：每条控制项打印 OK/BAD/BLOCKED，返回退出码（0 全绿 / 1 有 BAD / 3 有 BLOCKED）。"""
    checks = _Checks()
    started = time.time()

    # ---- 控制 1：工具名单一来源
    def c01():
        assert TOOL_NAME == "read_facts", "TOOL_NAME 被改动了：%r" % TOOL_NAME
        assert TOOL_PATH == "/tools/" + TOOL_NAME, TOOL_PATH
        assert HEALTH_PATH == "/health", HEALTH_PATH
        help_text = build_parser().format_help()
        assert TOOL_NAME in help_text, "帮助文本没有由 TOOL_NAME 拼出"
        assert TOOL_PATH in help_text, "帮助文本没有由 TOOL_PATH 拼出"
        schema = tool_schema()
        assert schema["name"] == TOOL_NAME and schema["endpoint"]["path"] == TOOL_PATH
        # 错误码表必须覆盖每一个异常类（含 405），且与类自身的 http 一致
        table = schema["error_codes"]

        def walk(cls):
            for sub in cls.__subclasses__():
                assert table.get(sub.code) == sub.http, \
                    "错误码表缺/错 %s → %r" % (sub.code, sub.http)
                walk(sub)

        walk(ServiceError)
        assert table[ServiceError.code] == 500
        assert set(("METHOD_NOT_ALLOWED", "NOT_FOUND", "READ_ONLY_REFUSED",
                    "BLOCKED_NO_INPUT_FACTS", "BLOCKED_UNKNOWN_CASE")) <= set(table), table
        assert len(table) >= 15, table
    checks.run("c01_tool_name_single_source", c01)

    # ---- 夹具落盘
    fixture = {}
    try:
        fixture = build_fixture(fixture_dir)
    except Exception as exc:  # noqa: BLE001
        checks.bad("c02_fixture_written", "%s: %s" % (type(exc).__name__, exc))
    else:
        def c02():
            import yaml as _yaml  # noqa: PLC0415

            with io.open(fixture["yaml"], "r", encoding="utf-8") as handle:
                doc = _yaml.safe_load(handle)
            assert doc["schema"] == INPUT_FACTS_SCHEMA, doc["schema"]
            assert len(doc["cases"]) == 4, len(doc["cases"])
            loaded = LF.load_base(fixture["base"])
            assert loaded["canonical_sha256"] == fixture["base_canonical_sha256"]
            assert fixture["coverage_ok"] is True, "夹具基底覆盖证据应为 ok=True"
            assert fixture["coverage_ok_nocov"] is False, \
                "覆盖负控夹具的 coverage.ok 应为 False，实为 %r" % (fixture["coverage_ok_nocov"],)
            assert os.path.isfile(fixture["nocoverage_yaml"]), fixture["nocoverage_yaml"]
        checks.run("c02_fixture_written", c02)
    checks.fixture_ok = bool(fixture)

    # ---- 控制 3：判决模式重建确定性（两次 → working_set_sha256 逐字相同）
    def c03():
        a, _ = judgement_working_set(FIXTURE_CASE, fixture["yaml"])
        b, _ = judgement_working_set(FIXTURE_CASE, fixture["yaml"])
        sa = a["meta"]["working_set_sha256"]
        sb = b["meta"]["working_set_sha256"]
        assert sa == sb, "%s != %s" % (sa, sb)
        assert sa == fixture["working_set_sha256"][FIXTURE_CASE], \
            "YAML 路径与内存路径的工作集锚不一致：%s != %s" % (
                sa, fixture["working_set_sha256"][FIXTURE_CASE])
        assert a["meta"]["canonical_sha256"] == fixture["base_canonical_sha256"]
    checks.run("c03_judgement_deterministic_sha256", c03)

    # ---- 控制 4：判决模式载荷逐字节相同（两次）
    def c04():
        raw = {"max_records": 40, "max_bytes": 5000}
        one = dump_bytes(raw, input_facts_path=fixture["yaml"], case_id=FIXTURE_CASE)
        two = dump_bytes(raw, input_facts_path=fixture["yaml"], case_id=FIXTURE_CASE)
        assert one == two, "两次 --dump 载荷不同（%d vs %d 字节）" % (len(one), len(two))
        assert b"\r\n" not in one, "载荷里出现 CRLF"
        assert one.count(b"\n") == 1 and one.endswith(b"\n"), "载荷行尾不是单个 LF"
    checks.run("c04_judgement_payload_byte_identical", c04)

    # ---- 控制 5：判决模式零网络
    real_connect = socket.socket.connect
    calls = []

    def _no_connect(self, address, *args, **kwargs):
        calls.append(address)
        raise AssertionError("判决模式不得发生网络连接：%r" % (address,))

    def c05():
        socket.socket.connect = _no_connect
        try:
            payload = read_facts(input_facts_path=fixture["yaml"],
                                 as_of="snapshot", case_id=FIXTURE_CASE)
        finally:
            socket.socket.connect = real_connect
        assert calls == [], "判决模式发生了 %d 次连接尝试" % len(calls)
        assert payload["source"]["facts_source"] == "snapshot(0s_ago)", \
            payload["source"]["facts_source"]
        assert payload["objects"], "判决模式返回空对象集"
        checks.expect("c05b_judgement_fetch_hook_fail_closed",
                      lambda: judgement_forbidden_fetcher(["gaussdb"], []),
                      NetworkRefused, "判决模式禁止")

    checks.run("c05_judgement_zero_network", c05)

    # ---- 控制 6：--dump-case 两次真进程逐字节相同
    def c06():
        import subprocess  # noqa: PLC0415

        script = os.path.abspath(__file__)
        outs = []
        for tag in ("a", "b"):
            path = os.path.join(fixture["dir"], "dump_%s.json" % tag)
            proc = subprocess.run(
                [sys.executable, script, "--dump-case", FIXTURE_CASE,
                 "--input-facts", fixture["yaml"], "--out", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=180)
            assert proc.returncode == 0, "子进程退出码 %d：%s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace")[-400:])
            with io.open(path, "rb") as handle:
                outs.append(handle.read())
        assert outs[0] == outs[1], "两次 --dump-case 输出不同"
        assert outs[0] == dump_bytes(input_facts_path=fixture["yaml"],
                                     case_id=FIXTURE_CASE), \
            "CLI 落盘载荷 != 进程内载荷"
    _run_subprocess_check(checks, "c06_dump_case_two_processes_identical", c06)

    # ---- 控制 7：缺失用例文件 ⇒ fail-closed BLOCKED
    def c07():
        missing = os.path.join(fixture["dir"], "__absent__", "m5-input-facts.yaml")
        assert not os.path.isfile(missing)
        checks.expect("c07_missing_input_facts_blocked",
                      lambda: read_facts(input_facts_path=missing, case_id=FIXTURE_CASE),
                      BlockedNoInputFacts, missing)
        assert not os.path.exists(missing)
    c07()

    # ---- 控制 8：仓库里那份用例文件的真实状态（未来 S4 产出后本项仍成立）
    def c08():
        status, out = _status_of_handle("POST", TOOL_PATH, _json_bytes({}),
                                        case_id="M5-001")
        body = json.loads(out.decode("utf-8"))
        if os.path.isfile(DEFAULT_INPUT_FACTS):
            assert body.get("error", {}).get("code") != "BLOCKED_NO_INPUT_FACTS", body
        else:
            assert status == 409, status
            assert body["error"]["code"] == "BLOCKED_NO_INPUT_FACTS", body
            assert DEFAULT_INPUT_FACTS in body["error"]["message"], body
    checks.run("c08_repo_input_facts_state", c08)

    # ---- 控制 9：未知 case_id ⇒ BLOCKED 且原因带上该 id
    def c09():
        checks.expect("c09_unknown_case_blocked_with_id",
                      lambda: read_facts(input_facts_path=fixture["yaml"],
                                         case_id=FIXTURE_UNKNOWN_CASE),
                      BlockedUnknownCase, FIXTURE_UNKNOWN_CASE)
    c09()

    # ---- 控制 10：覆盖证据不成立 ⇒ BLOCKED（门禁真的作用在基底上）
    def c10():
        checks.expect("c10_coverage_gate_blocks",
                      lambda: read_facts(input_facts_path=fixture["nocoverage_yaml"],
                                         case_id=FIXTURE_CASE_NOOP),
                      BlockedCoverage, "覆盖")
    c10()

    # ---- 控制 11：注入 value_before 与基底不符 ⇒ BLOCKED 并打印双方取值
    def c11():
        checks.expect("c11_declared_value_mismatch_blocked",
                      lambda: read_facts(input_facts_path=fixture["yaml"],
                                         case_id=FIXTURE_CASE_BADVALUE),
                      BlockedInjection, "基底里并不存在的取值-9e9")
    c11()

    # ---- 控制 12：max_records 截断口径
    def c12():
        full = read_facts(input_facts_path=fixture["yaml"],
                          as_of="snapshot", case_id=FIXTURE_CASE)
        big = read_facts({"max_records": 1000}, input_facts_path=fixture["yaml"],
                         as_of="snapshot", case_id=FIXTURE_CASE)
        total = len(full["objects"]) + len(full["relations"]) + len(full["observations"])
        assert total == FIXTURE_ROWS_TOTAL, \
            "夹具三段总行数应为 %d，实为 %d" % (FIXTURE_ROWS_TOTAL, total)
        assert not full["truncated"], full
        assert big["truncated"] is False, "max_records=1000 不该触发截断"
        small = read_facts({"max_records": 10}, input_facts_path=fixture["yaml"],
                           as_of="snapshot", case_id=FIXTURE_CASE)
        env = small
        rows_out = len(small["objects"]) + len(small["relations"]) + len(small["observations"])
        assert rows_out == 10, rows_out
        assert env["truncated"] is True, env
        assert env["droppedRows"] == total - 10, env
        assert len(small["relations"]) <= len(full["relations"])
        assert dangling_refs(small) == (0, 0), "%r" % (dangling_refs(small),)
        # 填充顺序 = 对象 → 关系 → 观测
        assert [o["id"] for o in small["objects"]] == [o["id"] for o in full["objects"]][:len(small["objects"])]
        assert [r["id"] for r in small["relations"]] == [r["id"] for r in full["relations"]][:len(small["relations"])]
    checks.run("c12_max_records_truncation_identity", c12)

    # ---- 控制 13：max_records=0 的显式边界 = 不返回任何行
    def c13():
        out = read_facts({"max_records": 0}, input_facts_path=fixture["yaml"],
                         as_of="snapshot", case_id=FIXTURE_CASE)
        env = out
        assert out["objects"] == [] and out["relations"] == [] and out["observations"] == [], out
        assert env["truncated"] is True, env
        assert env["droppedRows"] == FIXTURE_ROWS_TOTAL, env
        assert "max_records=0" in env["hint"], env["hint"]
    checks.run("c13_max_records_zero_boundary", c13)

    # ---- 控制 14：max_bytes 截断与字节记账恒等式
    def c14():
        raw = {"max_bytes": 900}
        out = read_facts(raw, input_facts_path=fixture["yaml"], as_of="snapshot",
                         case_id=FIXTURE_CASE)
        rows_bytes = _rows_bytes(out["objects"], out["relations"], out["observations"])
        env = out
        assert rows_bytes <= 900, "三段载荷 %d 字节 > max_bytes 900" % rows_bytes
        assert env["truncated"] is True, env
        assert dangling_refs(out) == (0, 0), "%r" % (dangling_refs(out),)
        # 恒等式：droppedBytes == 已过 max_records、未压表示那一刻的字节 - 最终字节
        zero = read_facts(input_facts_path=fixture["yaml"],
                          as_of="snapshot", case_id=FIXTURE_CASE)
        full_bytes = _rows_bytes(zero["objects"], zero["relations"], zero["observations"])
        assert env["droppedBytes"] == full_bytes - rows_bytes, \
            "droppedBytes=%d 与实测字节差 %d 不一致" % (env["droppedBytes"],
                                                        full_bytes - rows_bytes)
        assert "不完整" in env["hint"], env["hint"]
        # max_bytes=0 ⇒ 三段全空；droppedBytes 按同一恒等式记账（= 原量 - 空容器开销）
        tiny = read_facts({"max_bytes": 0}, input_facts_path=fixture["yaml"],
                          as_of="snapshot", case_id=FIXTURE_CASE)
        assert tiny["objects"] == [] and tiny["relations"] == [] and tiny["observations"] == [], tiny
        tiny_bytes = _rows_bytes(tiny["objects"], tiny["relations"], tiny["observations"])
        assert tiny["droppedRows"] == FIXTURE_ROWS_TOTAL, tiny
        assert tiny["droppedBytes"] == full_bytes - tiny_bytes, tiny
        assert tiny["droppedBytes"] > 0 and tiny_bytes > 0
    checks.run("c14_max_bytes_truncation_identity", c14)

    # ---- 控制 15：facts_source 三值都能出现且语义正确
    def c15():
        snap = read_facts(input_facts_path=fixture["yaml"],
                          as_of="snapshot", case_id=FIXTURE_CASE)
        assert snap["source"]["facts_source"] == "snapshot(0s_ago)", snap["source"]
        ws, captured = judgement_working_set(FIXTURE_CASE, fixture["yaml"])
        epoch = _parse_epoch(captured)
        assert epoch is not None, "夹具捕获时刻解析失败：%r" % (captured,)
        as_of = epoch + 3600.25
        aged = read_facts(input_facts_path=fixture["yaml"],
                          as_of=as_of, case_id=FIXTURE_CASE)
        # 与实现同一算式取值，避免浮点截断把 3600 变成 3599
        assert aged["source"]["facts_source"] == \
            "snapshot(%ds_ago)" % int(as_of - epoch), aged["source"]
        assert aged["source"]["facts_source"] == "snapshot(3600s_ago)", aged["source"]
        env = LF.fixture_envelope()
        run = read_facts({"layers": ["gaussdb", "os", "container", "app"]},
                         fetch=lambda layers, sections: env, case_id="")
        assert run["source"]["facts_source"] == "request", run["source"]
        assert run["source"]["snapshot_id"], "运行模式必须给出 snapshot_id"
        # empty：工作集本身为空（源面没给事实）才判 empty。空工作集过不了
        # build_working_set 的覆盖族机检（见 build_fixture 注释），所以这里直接对分类
        # 函数取值，并断言 scope 收窄**不**改 facts_source。
        assert facts_source(MODE_JUDGEMENT, {"objects": [], "relations": [], "observations": []},
                            captured, "snapshot") == "empty"
        assert facts_source(MODE_RUNTIME, {"objects": [], "relations": [], "observations": []},
                            None, None) == "empty"
        assert facts_source(MODE_JUDGEMENT, {"objects": [1], "relations": [], "observations": []},
                            captured, "snapshot").startswith("snapshot(")
        scoped_empty = read_facts({"scope": {"object_ids": ["ccf:__没有这个对象__"]}},
                                  input_facts_path=fixture["yaml"], as_of="snapshot",
                                  case_id=FIXTURE_CASE)
        assert scoped_empty["objects"] == [] and scoped_empty["observations"] == [], scoped_empty
        assert scoped_empty["source"]["facts_source"] == "snapshot(0s_ago)", \
            "scope 是视图，不得改 facts_source：%r" % (scoped_empty["source"],)
        partial = read_facts(input_facts_path=fixture["yaml"],
                             as_of="snapshot", case_id=FIXTURE_CASE_PARTIAL)
        assert partial["source"]["working_set_sha256"] == \
            fixture["working_set_sha256"][FIXTURE_CASE_PARTIAL]
        assert len(partial["objects"]) == len(snap["objects"]) - 1, \
            (len(partial["objects"]), len(snap["objects"]))
        assert dangling_refs(partial) == (0, 0), "%r" % (dangling_refs(partial),)
    checks.run("c15_facts_source_three_values", c15)

    # ---- 控制 16：只读断言（写意图字段一律拒绝）
    def c16():
        for key in ("write", "delete", "insert", "update", "commit", "admin", "force_write"):
            status, out = _status_of_handle("POST", TOOL_PATH,
                                            _json_bytes({key: True}),
                                            input_facts_path=fixture["yaml"],
                                            case_id=FIXTURE_CASE)
            body = json.loads(out.decode("utf-8"))
            assert status == 400, (key, status)
            assert body["error"]["code"] == "READ_ONLY_REFUSED", (key, body)
        status, out = _status_of_handle("POST", TOOL_PATH,
                                        _json_bytes({"scope": {"set": ["x"]}}),
                                        input_facts_path=fixture["yaml"])
        assert json.loads(out.decode("utf-8"))["error"]["code"] == "READ_ONLY_REFUSED", out
        assert status == 400
        # 值=false 也拒（本工具没有写参数，出现即拒）
        status, out = _status_of_handle("POST", TOOL_PATH,
                                        _json_bytes({"write": False}),
                                        input_facts_path=fixture["yaml"])
        assert json.loads(out.decode("utf-8"))["error"]["code"] == "READ_ONLY_REFUSED", out
        # 代码面：不存在任何写端点
        routes = [TOOL_PATH, HEALTH_PATH]
        assert len(routes) == 2 and all(not r.lower().startswith(("/write", "/set", "/del"))
                                       for r in routes)
        for method in ("PUT", "DELETE", "PATCH"):
            status, out = _status_of_handle(method, TOOL_PATH, b"{}",
                                            input_facts_path=fixture["yaml"])
            assert status == 405, (method, status, out[:120])
    checks.run("c16_read_only_write_intent_refused", c16)

    # ---- 控制 17：未知入参 ⇒ 400 而不是 500
    def c17():
        status, out = _status_of_handle("POST", TOOL_PATH, _json_bytes({"bogus_x": 1}),
                                        input_facts_path=fixture["yaml"])
        body = json.loads(out.decode("utf-8"))
        assert status == 400 and body["error"]["code"] == "UNKNOWN_PARAM", (status, body)
        assert "bogus_x" in body["error"]["message"], body
    checks.run("c17_unknown_param_rejected", c17)

    # ---- 控制 18：载荷大小上限
    def c18():
        huge = _json_bytes({"sections": ["x"] * 70000})
        assert len(huge) > MAX_BODY_BYTES, len(huge)
        status, out = _status_of_handle("POST", TOOL_PATH, huge,
                                        input_facts_path=fixture["yaml"],
                                        case_id=FIXTURE_CASE)
        body = json.loads(out.decode("utf-8"))
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE", (status, body)
        assert str(MAX_BODY_BYTES) in body["error"]["message"], body
        # 恰好等于上限不拒
        ok_body = _json_bytes({})
        assert len(ok_body) < MAX_BODY_BYTES
        status, _out = _status_of_handle("POST", TOOL_PATH, ok_body,
                                         input_facts_path=fixture["yaml"],
                                         case_id=FIXTURE_CASE)
        assert status == 200, status
    checks.run("c18_payload_size_cap", c18)

    # ---- 控制 19：参数校验一律 4xx（含具体码），不是 500
    def c19():
        table = [
            ({"layers": "gaussdb"}, "BAD_PARAM"),
            ({"layers": [1, 2]}, "BAD_PARAM"),
            ({"layers": []}, "BAD_PARAM"),
            ({"sections": "x"}, "BAD_PARAM"),
            ({"sections": [None]}, "BAD_PARAM"),
            ({"scope": "everything"}, "BAD_PARAM"),
            ({"scope": {"typo": ["a"]}}, "UNKNOWN_PARAM"),
            ({"max_records": "200"}, "BAD_PARAM"),
            ({"max_records": True}, "BAD_PARAM"),
            ({"max_records": -1}, "BAD_PARAM"),
            ({"max_records": MAX_RECORDS_LIMIT + 1}, "BAD_PARAM"),
            ({"max_bytes": 1.5}, "BAD_PARAM"),
            ({"max_bytes": -5}, "BAD_PARAM"),
            ({"max_bytes": MAX_BYTES_LIMIT + 1}, "BAD_PARAM"),
            ({"case_id": 7}, "UNKNOWN_PARAM"),
            ({"case_id": FIXTURE_CASE}, "UNKNOWN_PARAM"),
        ]
        for raw, code in table:
            status, out = _status_of_handle("POST", TOOL_PATH, _json_bytes(raw),
                                            input_facts_path=fixture["yaml"])
            body = json.loads(out.decode("utf-8"))
            assert 400 <= status < 500, (raw, status, body)
            assert body["error"]["code"] == code, (raw, body)
        # 合法但缺省 ⇒ 全默认
        inputs = normalize_inputs(None)
        assert inputs["layers"] == list(DEFAULT_LAYERS) and inputs["max_records"] == 200
        assert inputs["max_bytes"] == 20000 and "case_id" not in inputs
        assert normalize_inputs({"layers": None})["layers"] == list(DEFAULT_LAYERS)
        assert normalize_inputs({"sections": []})["sections"] == []
    checks.run("c19_param_validation_4xx", c19)

    # ---- 控制 19b：层名纪律（★ R-56/A）—— 未知层名与「运行模式缺必需层」都必须
    #      以 BAD_PARAM **早报**，且报错文本必须点名合法取值并指向 scope
    #      （模型面上 layers 没有取值说明 ⇒ 报错文本是模型唯一能学到的信道）
    def c19b():
        # ① 自洽：合法层名必须覆盖默认层与 profile 必需层
        assert set(DEFAULT_LAYERS) <= set(KNOWN_LAYERS), DEFAULT_LAYERS
        assert set(required_layers()) <= set(KNOWN_LAYERS), DEFAULT_PROFILE

        # ② 未知层名（真机实测模型真发过的两种形态）⇒ BAD_PARAM + 点名合法取值 + 指向 scope
        for bad_layers in (["app", "container", "db", "host"], ["App", "Runtime", "Data"],
                           ["gaussdb", "nope"]):
            try:
                normalize_inputs({"layers": bad_layers})
            except BadParam as exc:
                assert exc.code == "BAD_PARAM", exc.code
                for name in KNOWN_LAYERS:
                    assert name in exc.message, (name, exc.message)
                assert "scope" in exc.message, exc.message
            else:
                raise AssertionError("未知层名竟然通过：%s" % bad_layers)

        # ③ 运行模式缺必需层 ⇒ BAD_PARAM（真机实测：这是模型最常踩的形态）
        for partial in (["app"], ["gaussdb"], ["container", "app"], ["storage"]):
            try:
                check_runtime_layers(partial)
            except BadParam as exc:
                assert exc.code == "BAD_PARAM", exc.code
                for name in required_layers():
                    assert name in exc.message, (name, exc.message)
                assert "scope" in exc.message, exc.message
            else:
                raise AssertionError("运行模式缺必需层竟然通过：%s" % partial)

        # ④ 正控（fail-closed 不得误拦）：四层 / 四层+storage / gaussdb+os 全部放行
        for good in (list(DEFAULT_LAYERS), list(DEFAULT_LAYERS) + ["storage"], ["gaussdb", "os"]):
            check_runtime_layers(good)
            assert normalize_inputs({"layers": good})["layers"] == list(good)

        # ⑤ 判决模式忽略 layers ⇒ 层子集**不因本条规则**被拒（真机 read_facts 全路径）
        status, out = _status_of_handle("POST", TOOL_PATH, _json_bytes({"layers": ["app"]}),
                                        input_facts_path=fixture["yaml"], case_id=FIXTURE_CASE)
        assert status == 200, (status, out[:200])
    checks.run("c19b_layer_name_discipline", c19b)

    # ---- 控制 19c：★ R-58 —— 模型面「取值说明」的 声明面 ↔ 实现自述 必须逐字同源
    #      为什么单列一条：模型面上参数的**唯一**说明字段就是 description（注册面 =
    #      live-facts-tools.yaml#read_facts.params.*.description → register_tools.py 的
    #      params_from_entry），而实现面另有一份自述（本模块的 tool_schema()）。
    #      两处漂移 = 「声明面说 A、实现自述说 B」，且**没有任何既有门禁比对参数级 description**。
    def c19c():
        path = os.path.join(_ROOT, SHAPE_REL)
        assert os.path.isfile(path), "形状权威文件不在位：%s" % path
        import yaml as _yaml  # noqa: PLC0415
        with io.open(path, encoding="utf-8") as handle:
            shape_params = _yaml.safe_load(handle)["read_facts"]["params"]
        mine = tool_schema()["parameters"]["properties"]
        for name in ("layers", "sections", "scope", "max_records", "max_bytes"):
            assert name in mine, name
            want = (shape_params.get(name) or {}).get("description")
            if want is None:
                continue                     # 形状面没声明 ⇒ 实现面也不必声明
            assert mine[name].get("description") == str(want).rstrip("\n"), (
                "参数 %s 的 description 与形状权威不逐字相同：%r != %r"
                % (name, mine[name].get("description"), want))
        # ★ 反恒真：layers 的说明必须**真的**点名合法取值、必需层与「收窄该用谁」
        #   （否则「两边一起写一句空话」也能让上面的等式成立）
        text = mine["layers"]["description"]
        for token in ("gaussdb", "os", "container", "app", "scope"):
            assert token in text, (token, text)
    checks.run("c19c_param_description_same_source", c19c)

    # ---- 控制 20：路由与方法
    def c20():
        status, out = _status_of_handle("GET", TOOL_PATH, b"", input_facts_path=fixture["yaml"])
        assert status == 405 and json.loads(out.decode("utf-8")), (status, out[:120])
        status, out = _status_of_handle("POST", HEALTH_PATH, b"{}",
                                        input_facts_path=fixture["yaml"])
        assert status == 405, status
        status, out = _status_of_handle("GET", "/tools/nope", b"",
                                        input_facts_path=fixture["yaml"])
        assert status == 404, status
        status, out = _status_of_handle("GET", HEALTH_PATH + "?x=1", b"",
                                        input_facts_path=fixture["yaml"])
        assert status == 200, status
        status, out = _status_of_handle("GET", TOOL_PATH, b"",
                                        input_facts_path=fixture["yaml"])
        assert status == 405, status
        assert json.loads(out.decode("utf-8"))["error"]["code"] == "METHOD_NOT_ALLOWED", out[:160]
    checks.run("c20_route_and_method", c20)

    # ---- 控制 21：/health 形状 + 不回显凭据
    def c21():
        status, out = _status_of_handle("GET", HEALTH_PATH, b"",
                                        input_facts_path=fixture["yaml"], port=8304,
                                        host="0.0.0.0", started_at=started)
        assert status == 200, status
        body = json.loads(out.decode("utf-8"))
        for key in ("service", "version", "tool", "tool_path", "port", "started_at",
                    "modes", "input_facts", "limits", "faces"):
            assert key in body, key
        assert body["tool"] == TOOL_NAME and body["tool_path"] == TOOL_PATH
        assert body["input_facts"]["present"] is True
        fp = LF.fingerprint_file(fixture["yaml"])
        assert body["input_facts"]["sha256"] == fp["sha256"]
        assert body["input_facts"]["sha256_prefix"] == fp["sha256"][:16]
        assert body["modes"]["judgement"]["available"] is True
        text = out.decode("utf-8").lower()
        for bad in ("password", "passwd", "secret", "token", "apikey", "api_key",
                    "authorization", "credential", "private_key", "bearer"):
            assert bad not in text, "health 回显了疑似凭据字段名：%s" % bad
        assert "192.0.2.10" not in text, "health 回显了采集地址"
        assert "gdo_collect_url" not in text, "health 回显了采集地址环境变量名"
    checks.run("c21_health_face_shape_no_credentials", c21)

    # ---- 控制 22：scope 收窄是视图，不改工作集锚
    def c22():
        full = read_facts(input_facts_path=fixture["yaml"],
                          as_of="snapshot", case_id=FIXTURE_CASE)
        types = sorted({o["type"] for o in full["objects"]})
        object_ids = [o["id"] for o in full["objects"] if o["type"] == types[0]]
        scoped = read_facts({"scope": {"object_ids": object_ids}},
                            input_facts_path=fixture["yaml"], as_of="snapshot",
                            case_id=FIXTURE_CASE)
        assert len(scoped["objects"]) == len(object_ids), (len(scoped["objects"]), len(object_ids))
        assert len(scoped["objects"]) < len(full["objects"]), "scope 没有起收窄作用"
        assert dangling_refs(scoped) == (0, 0), "%r" % (dangling_refs(scoped),)
        assert scoped["source"]["working_set_sha256"] == full["source"]["working_set_sha256"], \
            "scope 影响了工作集锚（收窄必须是视图）"
        assert "scope_out=" in scoped["hint"]
        scope_out = int(scoped["hint"].split("scope_out=")[1].split(";")[0])
        rows_full = len(full["objects"]) + len(full["relations"]) + len(full["observations"])
        rows_scope = len(scoped["objects"]) + len(scoped["relations"]) + len(scoped["observations"])
        assert scope_out == rows_full - rows_scope, (scope_out, rows_full, rows_scope)
        metric_filtered = read_facts({"scope": {"metric_codes": ["不存在的指标"]}},
                                     input_facts_path=fixture["yaml"], as_of="snapshot",
                                     case_id=FIXTURE_CASE)
        assert metric_filtered["observations"] == [], metric_filtered["observations"][:2]
    checks.run("c22_scope_filtering_is_view", c22)

    # ---- 控制 23：既有有界语义（aggregate）未被本服务的限行口径污染
    def c23():
        ws, _ = judgement_working_set(FIXTURE_CASE, fixture["yaml"])
        _by_path, meta = LF.aggregate(ws)
        assert meta["facts_dropped"] == 0, meta
        assert meta["observations_in"] == meta["observations_out"], meta
        assert meta["observations_in"] == len(ws["observations"])
    checks.run("c23_aggregate_bounded_semantics_intact", c23)

    # ---- 控制 24：真的转调封装层（不重写归一化 / 注入 / 覆盖）
    def c24():
        calls = {"load_base": 0, "build_working_set": 0}
        real_load, real_build = LF.load_base, LF.build_working_set

        def spy_load(*args, **kwargs):
            calls["load_base"] += 1
            return real_load(*args, **kwargs)

        def spy_build(*args, **kwargs):
            calls["build_working_set"] += 1
            return real_build(*args, **kwargs)

        LF.load_base, LF.build_working_set = spy_load, spy_build
        try:
            read_facts(input_facts_path=fixture["yaml"], case_id=FIXTURE_CASE)
        finally:
            LF.load_base, LF.build_working_set = real_load, real_build
        assert calls["load_base"] >= 1, calls
        assert calls["build_working_set"] >= 1, calls
    checks.run("c24_delegates_to_live_facts", c24)

    # ---- 控制 25：无跨请求可变状态（模块级容器在请求前后逐字相同）
    def c25():
        import sys as _sys  # noqa: PLC0415

        module = _sys.modules[__name__]
        def snapshot():
            out = {}
            for key, value in vars(module).items():
                if key.startswith("__"):
                    continue
                if isinstance(value, (dict, list)):
                    out[key] = LF.canonical_json(value)
            return out

        before = snapshot()
        keys_before = sorted(k for k in vars(module) if not k.startswith("__"))
        for case, raw in ((FIXTURE_CASE, {"max_records": 5}), (FIXTURE_CASE_NOOP, {}),
                          (FIXTURE_CASE, {})):
            read_facts(raw, input_facts_path=fixture["yaml"], as_of="snapshot",
                       case_id=case)
        after = snapshot()
        assert before == after, "模块级容器被请求改动了：%s" % (
            [k for k in before if before[k] != after.get(k)],)
        assert keys_before == sorted(k for k in vars(module) if not k.startswith("__")), \
            "请求期间新增了模块级全局"
        assert not any(isinstance(v, (dict, list)) and not k.startswith("_")
                       for k, v in vars(module).items()), "存在公开的模块级可变容器"
    checks.run("c25_no_cross_request_state", c25)

    # ---- 控制 26：上游封装层指纹与 S2a 基线一致
    def c26():
        drift = []
        for rel, want in sorted(_UPSTREAM_BASELINE.items()):
            path = os.path.join(_ROOT, rel)
            got = LF.fingerprint_file(path)["sha256"]
            if got != want:
                drift.append("%s: 基线 %s != 实算 %s" % (rel, want[:16], got[:16]))
        if drift:
            checks.blocked("c26_upstream_baseline_hashes",
                           "上游封装层已换代，需重新基线（不是本服务缺陷）：%s" % "; ".join(drift))
        else:
            checks.ok("c26_upstream_baseline_hashes")
    c26()

    # ---- 控制 27：HTTP 响应形状（走 handle_request，不 bind 端口）
    def c27():
        body = _json_bytes({"max_records": 12})
        status, headers, out = handle_request("POST", TOOL_PATH, body,
                                             input_facts_path=fixture["yaml"],
                                             case_id=FIXTURE_CASE)
        assert status == 200, status
        assert headers["Content-Type"].startswith("application/json"), headers
        assert headers["Cache-Control"] == "no-store", headers
        assert headers["X-Tool"] == TOOL_NAME, headers
        payload = json.loads(out.decode("utf-8"))
        assert sorted(payload) == ["droppedBytes", "droppedRows", "hint", "objects",
                                   "observations", "relations", "source", "truncated"], \
            sorted(payload)
        assert sorted(payload["source"]) == ["canonical_sha256", "facts_source", "snapshot_id",
                                            "working_set_sha256"], sorted(payload["source"])
        assert isinstance(payload["hint"], str)
        for row in payload["observations"]:
            assert "objectId" in row and "metricCode" in row and "status" in row, row
        for row in payload["objects"]:
            assert "id" in row and "type" in row and "layer" in row, row
        # 空正文 ⇒ 运行模式；此处用桩采集面，避免真机
        env = LF.fixture_envelope()
        status, _h2, out2 = handle_request("POST", TOOL_PATH, b"",
                                           fetch=lambda layers, sections: env, case_id="")
        assert status == 200, (status, out2[:200])
        assert json.loads(out2.decode("utf-8"))["source"]["facts_source"] == "request"
    checks.run("c27_http_response_shape", c27)

    # ---- 控制 28：运行模式上游不可用 ⇒ fail-closed 502（不退化成空集 / 判决模式）
    def c28():
        def boom(layers, sections):
            raise OSError("采集面不可达（自检桩）")

        checks.expect("c28a_runtime_upstream_down_502",
                      # ★ R-56/A：本控制项只验「上游挂了 ⇒ fail-closed 502」，
                      #   故 layers 必须用**合法且满足覆盖**的组合（["gaussdb","os"]）；
                      #   层子集本身已被 check_runtime_layers 提前拦成 BAD_PARAM（另有 c19b 覆盖）。
                      lambda: read_facts({"layers": ["gaussdb", "os"]}, fetch=boom, case_id=""),
                      UpstreamUnavailable, "采集面调用异常", "OSError")
        status, out = _status_of_handle("POST", TOOL_PATH, _json_bytes({}), fetch=boom,
                                        case_id="")
        assert status == 502, status
        body = json.loads(out.decode("utf-8"))
        assert body["error"]["code"] == "UPSTREAM_UNAVAILABLE", body
        assert "objects" not in body and "observations" not in body, "上游挂了却回了事实段"

        def garbage(layers, sections):
            return ["不是对象"]

        checks.expect("c28b_runtime_non_envelope_502",
                      lambda: read_facts({}, fetch=garbage, case_id=""),
                      UpstreamUnavailable, "回执不是 JSON 对象")

        def good(layers, sections):
            return LF.fixture_envelope()

        run = read_facts({"layers": ["gaussdb", "os"]}, fetch=good, case_id="")
        assert run["source"]["facts_source"] == "request", run["source"]
        assert run["objects"] and run["observations"], "运行模式回了空集"
        assert run["truncated"] is False, run
    checks.run("c28_runtime_upstream_fail_closed", c28)

    # ---- 控制 29：进程级用例（--case / env）⇒ 判决模式，真进程两次逐字节相同
    def c29():
        import subprocess  # noqa: PLC0415

        script = os.path.abspath(__file__)
        base_env = dict(os.environ, PYTHONIOENCODING="utf-8")
        base_env.pop(CASE_ENV_VAR, None)

        def run_cli(out_name, extra, env):
            path = os.path.join(fixture["dir"], out_name)
            return subprocess.run(
                [sys.executable, script, "--dump-case", "--input-facts", fixture["yaml"],
                 "--out", path] + list(extra),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=180), path

        outs = []
        for tag in ("a", "b"):
            proc, path = run_cli("proc_case_%s.json" % tag, ["--case", FIXTURE_CASE], base_env)
            assert proc.returncode == 0, "子进程退出码 %d：%s" % (
                proc.returncode, proc.stderr.decode("utf-8", "replace")[-400:])
            with io.open(path, "rb") as handle:
                outs.append(handle.read())
        assert outs[0] == outs[1], "两次 --case 判决模式输出不同"
        assert outs[0] == dump_bytes(input_facts_path=fixture["yaml"],
                                     case_id=FIXTURE_CASE), \
            "CLI --case 载荷 != 进程内载荷"

        # env 回落必须与 --case 逐字节同源（同一进程级信道的两种入口）
        proc, path = run_cli("proc_case_env.json", [],
                             dict(base_env, **{CASE_ENV_VAR: FIXTURE_CASE}))
        assert proc.returncode == 0, "env 回落子进程退出码 %d：%s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace")[-400:])
        with io.open(path, "rb") as handle:
            assert handle.read() == outs[0], \
                "env %s 与 --case 的载荷不同（进程级信道不自洽）" % CASE_ENV_VAR
    _run_subprocess_check(checks, "c29_process_case_judgement_deterministic", c29)

    # ---- 控制 30：请求体里的 case_id 一律按未知入参拒绝（请求级信道已关闭）
    def c30():
        for label, body_case, proc_case in (
                ("同进程级用例", FIXTURE_CASE, FIXTURE_CASE),
                ("异于进程级用例", FIXTURE_UNKNOWN_CASE, FIXTURE_CASE),
                ("运行模式（无进程级用例）", FIXTURE_CASE, ""),
        ):
            status, out = _status_of_handle(
                "POST", TOOL_PATH, _json_bytes({"case_id": body_case}),
                input_facts_path=fixture["yaml"], case_id=proc_case)
            body = json.loads(out.decode("utf-8"))
            assert status == 400, (label, status, body)
            assert body["error"]["code"] == "UNKNOWN_PARAM", (label, body)
            assert "case_id" in body["error"]["message"], (label, body)
            assert "objects" not in body and "observations" not in body, \
                "请求体带 case_id 却回了事实段：%s" % label
        # 合法字段与 case_id 混用时也必须整体拒绝（不得「忽略未知键继续跑」）
        status, out = _status_of_handle(
            "POST", TOOL_PATH, _json_bytes({"max_records": 5, "case_id": FIXTURE_CASE}),
            input_facts_path=fixture["yaml"], case_id=FIXTURE_CASE)
        assert status == 400, status
        assert json.loads(out.decode("utf-8"))["error"]["code"] == "UNKNOWN_PARAM", out[:200]
    checks.run("c30_request_body_case_id_refused", c30)

    # ---- 控制 31：无用例 ⇒ 真走运行模式采集路径；采集面不可达时 fail-closed 而非挂起
    def c31():
        import subprocess  # noqa: PLC0415

        script = os.path.abspath(__file__)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop(CASE_ENV_VAR, None)
        # 把采集面指到本机必然拒连的端口：既验证「真采集路径」，又不碰真机 :8187
        env["GDO_COLLECT_URL"] = "http://127.0.0.1:1"
        try:
            proc = subprocess.run([sys.executable, script, "--dump-case",
                                   "--input-facts", fixture["yaml"]],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, timeout=120)
        except subprocess.TimeoutExpired:
            raise AssertionError("无用例时运行模式未 fail-closed：120s 内没有返回（疑似挂起）")
        err = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 3, "退出码 %d（应 BLOCKED=3）：%s" % (proc.returncode, err[-400:])
        assert proc.stdout == b"", "fail-closed 却往 stdout 写了载荷：%r" % proc.stdout[:120]
        assert "UPSTREAM_UNAVAILABLE" in err, err[-400:]
        assert "127.0.0.1:1" in err, "错误信息里没有采集面地址（无法定位）：%s" % err[-400:]
        assert "BLOCKED_NO_INPUT_FACTS" not in err and "BLOCKED_UNKNOWN_CASE" not in err, \
            "无进程级用例却进了判决模式：%s" % err[-400:]
    _run_subprocess_check(checks, "c31_no_case_runtime_fail_closed_not_hang", c31)

    # ---- 控制 32：进程级用例是未知 id ⇒ BLOCKED 且报错带该 id（不退化成运行模式）
    def c32():
        import subprocess  # noqa: PLC0415

        script = os.path.abspath(__file__)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop(CASE_ENV_VAR, None)
        env["GDO_COLLECT_URL"] = "http://127.0.0.1:1"
        proc = subprocess.run(
            [sys.executable, script, "--case", FIXTURE_UNKNOWN_CASE, "--dump-case",
             "--input-facts", fixture["yaml"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=180)
        err = proc.stderr.decode("utf-8", "replace")
        assert proc.returncode == 3, "退出码 %d（应 BLOCKED=3）：%s" % (proc.returncode, err[-400:])
        assert proc.stdout == b"", "BLOCKED 却往 stdout 写了载荷"
        assert "BLOCKED_UNKNOWN_CASE" in err, err[-400:]
        assert FIXTURE_UNKNOWN_CASE in err, "报错没带未知用例 id：%s" % err[-400:]
        assert "UPSTREAM_UNAVAILABLE" not in err, "未知用例不该退化成运行模式真采：%s" % err[-400:]
    _run_subprocess_check(checks, "c32_process_case_unknown_blocked_with_id", c32)

    # ---- 控制 33：工具 schema 里不得出现任何 case 参数（模型面零 M5 专有入参）
    def c33():
        import subprocess  # noqa: PLC0415

        schema = tool_schema()
        props = sorted(schema["parameters"]["properties"])
        assert props == ["layers", "max_bytes", "max_records", "scope", "sections"], props
        assert schema["parameters"]["required"] == [], schema["parameters"]["required"]
        assert schema["name"] == TOOL_NAME, schema["name"]
        blob = LF.canonical_json(schema)
        assert "case_id" not in blob, "schema 里出现了 case_id 参数"
        assert '"case"' not in blob, "schema 里出现了 case 参数"
        # --emit-schema 的实际输出必须逐字节等于进程内 canonical 形态
        script = os.path.abspath(__file__)
        proc = subprocess.run([sys.executable, script, "--emit-schema"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=180)
        assert proc.returncode == 0, "退出码 %d：%s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace")[-400:])
        want = (blob + "\n").encode("utf-8")
        assert proc.stdout == want, "CLI --emit-schema 与进程内 schema 不同源"
        assert b"case_id" not in proc.stdout, "CLI 输出的 schema 里出现了 case_id"
    _run_subprocess_check(checks, "c33_tool_schema_no_case_param", c33)

    return _report(checks, started, verbose)


def _run_subprocess_check(checks, name, fn):
    """子进程类控制项：环境不允许起子进程时记 BLOCKED（不记 FAIL，也不假装 OK）。"""
    try:
        fn()
    except AssertionError as exc:
        checks.bad(name, "%s" % exc)
    except Exception as exc:  # noqa: BLE001
        checks.blocked(name, "本机无法起子进程：%s: %s" % (type(exc).__name__, exc))
    else:
        checks.ok(name)


def _report(checks, started, verbose):
    bad = [x for x in checks.items if x[1] is False]
    blk = [x for x in checks.items if x[1] is None]
    if verbose:
        for name, state, why in checks.items:
            tag = {True: "OK ", False: "BAD", None: "BLK"}[state]
            line = "%s %s" % (tag, name)
            if why:
                line += " :: %s" % why
            print(line)
    print("-" * 74)
    print("%s %s · 工具名=%s · 端点 %s (POST) / %s (GET) · 控制项 %d（BAD=%d BLOCKED=%d）"
          % (SERVICE_NAME, SERVICE_VERSION, TOOL_NAME, TOOL_PATH, HEALTH_PATH,
             len(checks.items), len(bad), len(blk)))
    print("SELFTEST %s in %.2fs · OK=%d BAD=%d BLOCKED=%d"
          % ("PASS" if not bad and not blk else ("FAIL" if bad else "BLOCKED"),
             time.time() - started, len(checks.items) - len(bad) - len(blk), len(bad), len(blk)))
    print("退出码 0=PASS 1=FAIL 2=USAGE 3=BLOCKED（BLOCKED ≠ FAIL）")
    if bad:
        return 1
    if blk:
        return 3
    return 0


# --------------------------------------------------------------------- 13) CLI

def build_parser():
    parser = argparse.ArgumentParser(
        prog="live_facts_service.py",
        description="中性只读取数服务 %r（第 4 个只读服务，默认端口 %d）：POST %s 取数、"
                    "GET %s 自述；判决模式（--case / env 非空）零网络，运行模式（未指定"
                    "进程级用例）按既有默认真采一次。" % (TOOL_NAME, DEFAULT_PORT,
                                                        TOOL_PATH, HEALTH_PATH),
        epilog="端点路径与帮助文本一律由模块常量 TOOL_NAME=%r 拼出。" % TOOL_NAME)
    parser.add_argument("--selftest", action="store_true",
                        help="真跑自检（控制项逐条 OK/BAD/BLOCKED）")
    parser.add_argument("--case", metavar="CASE_ID", default=None,
                        help="★ 进程级用例选择（env %s 为回落）：非空 ⇒ 判决模式（零网络、"
                             "确定性重建）；空/缺省 ⇒ 运行模式（向采集面真采一次）。"
                             "用例**不是**请求级参数，也不出现在工具 schema 里。" % CASE_ENV_VAR)
    parser.add_argument("--dump-case", metavar="CASE_ID", nargs="?", const="", default=None,
                        help="离线重建某个 case 的载荷（不经 HTTP，逐字节可复现）；"
                             "省略 CASE_ID 时用 --case / env %s 的进程级用例" % CASE_ENV_VAR)
    parser.add_argument("--serve", action="store_true", help="启动只读服务（前台）")
    parser.add_argument("--emit-schema", action="store_true",
                        help="打印该工具（%s）的机读契约，供契约门禁与 App 注册使用" % TOOL_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--input-facts", default=None,
                        help="用例文件路径（默认 %s）" % INPUT_FACTS_REL)
    parser.add_argument("--as-of", choices=("live", "snapshot"), default=None,
                        help="snapshot(age) 的时间锚：live=墙钟（默认，服务模式）；"
                             "snapshot=快照自身捕获时刻（--dump-case 默认，保证逐字节可复现）")
    parser.add_argument("--layers", default=",".join(DEFAULT_LAYERS))
    parser.add_argument("--sections", default="")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--fixture-dir", default=None,
                        help="自检夹具目录（默认 tmp 下自建目录）")
    parser.add_argument("--out", default=None, help="--dump-case 的输出文件（默认 stdout）")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.selftest:
        return selftest(verbose=True, fixture_dir=args.fixture_dir)
    if args.emit_schema:
        # ★ 逐字节输出（LF=0x0A，不做平台换行翻译）：该字节流由 S2c 的注册面消费，
        # 必须跨平台逐字节可复算（文本模式 stdout 在 Windows 上会把 \n 写成 \r\n）。
        sys.stdout.buffer.write(
            (LF.canonical_json(tool_schema()) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0
    if args.dump_case is not None:
        raw = {"max_records": args.max_records,
               "max_bytes": args.max_bytes,
               "layers": [x for x in (args.layers or "").split(",") if x],
               "sections": [x for x in (args.sections or "").split(",") if x]}
        case_id = resolve_case_id(args.dump_case or args.case)   # ★ D48：进程级用例
        as_of = "snapshot" if (args.as_of or "snapshot") == "snapshot" else time.time()
        try:
            blob = dump_bytes(raw, input_facts_path=args.input_facts, as_of=as_of,
                              case_id=case_id)
        except ServiceError as exc:
            sys.stderr.write("\n[%s] HTTP %d %s\n" % (type(exc).code, type(exc).http,
                                                      exc.message))
            return 3
        if args.out:
            with io.open(args.out, "wb") as handle:
                handle.write(blob)
        else:
            sys.stdout.buffer.write(blob)
            sys.stdout.buffer.flush()
        return 0
    if args.serve:
        return serve(host=args.host, port=args.port, input_facts_path=args.input_facts,
                     as_of="snapshot" if args.as_of == "snapshot" else None,
                     case_id=args.case)
    build_parser().print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
