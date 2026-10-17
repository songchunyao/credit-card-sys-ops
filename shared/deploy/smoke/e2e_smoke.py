#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W7 端到端冒烟：证明「模型真被调用」与「工具真被执行」。

设计原则
--------
1. **三态退出码**（与本仓库既有 gate 一致）::

       PASS    = 0
       FAIL    = 1     # 事情确实没发生（模型没答 / SSE 无工具事件 / 审计链没长）
       BLOCKED = 2     # 证据面取不到（缺凭据 / 载体审计链不可读 / 第三平面全灭）

   优先级 ``BLOCKED(2) > FAIL(1) > PASS(0)``：只要有任何一项 BLOCKED，整体就是
   BLOCKED。**BLOCKED ≠ FAIL**——前者是「没测到」，后者是「测到了、是坏的」。

2. **多平面判据**，每个 App 的每次调用都要同时满足：

   * 平面 0（模型真被调用）：HTTP 200 + 响应元数据里的模型名/token 用量 + 非空回答。
   * 平面 1（Dify 侧工具真被调用）：SSE 出现 ``agent_thought`` 且 ``tool`` 非空，
     且 ``tool`` ∈ 该 App **真机回读**的绑定工具集（不照抄任何清单）。
   * 平面 2（载体侧工具真被执行）：该次调用的时间窗内，对应载体（:8301/:8302/:8303）
     的审计链 JSONL **新增 ≥1 行**。
   * 平面 3（仅 ``cc-kgt-free-agent``）：插件直连 Neo4j，绕过载体，审计链不适用。
     依次尝试 (a) 主机 ``dify-plugin_daemon-1`` 日志里该窗口的 ``dispatch/tool/invoke``，
     (b) Neo4j 侧读数，(c) 用只读凭据直接查 Neo4j 与回答对齐。三者全灭 ⇒ BLOCKED。

3. **离线自检** ``--selftest``：不发任何网络请求，用内置假响应/假审计行做
   正控 + 负控，覆盖 SSE 无工具事件、审计链无新增、HTTP 非 200、缺凭据等。

4. **临时凭据纪律**：每个 App 现建一个 api-key，用完立即删除并**回读确认**。
   key 的值绝不落盘/打印，只写 ``len`` 与 ``sha256[:12]``。

5. **FAIL 的两类分类**（W31 阶段 1，**只加不减**）：FAIL 时除原有 ``failed`` 文本外，
   再按**证据**追加一个类别字段 ``fail_class``，用来回答「是载体坏了还是模型不用工具」：

   * ``FAIL-CARRIER``（载体类）：工具面本身缺失/漂移 —— 工具面错误串、真机回读绑定集为空、
     运行时工具名不在绑定集、载体取数请求本身失败（HTTP 非 200）等；
   * ``FAIL-MODEL``（模型类）：载体面无上述任何信号，而模型从不调用工具 / 调用了但
     observation 全空 / **伪称调用**（答案自称调了工具而 SSE 无任何工具事件）；
   * ``FAIL-UNCLASSIFIED``（无法分类）：证据面不足以区分 ⇒ **单列计数**，
     **不并入**任何成功率的分子/分母。

   ★ 分类**不新增、不放宽任何判据/阈值**，也不改三态退出码与
   ``BLOCKED(2) > FAIL(1) > PASS(0)`` 优先级：它只读 ``judge_call`` 已有的读数，
   由纯函数 :func:`classify_fail` 计算，``--selftest`` 可直接喂假数据。
   匹配串全部抄自既有真机证据（出处见各常量注释）。

6. **「类别不可判定」标注**（W37 阶段 1，**只加不减**）：W35 已证明，在
   「SSE 文本 + App 配置回读」这一**证据面内**，「运行时工具面被静默丢弃（载体故障）
   + 模型伪称调用」与「模型从未尝试调用工具」**不可区分**：W7 同一次真机窗口里，
   同一 App、同一回读状态（绑定集非空 ``['neo4j_query']``、回读无错）的两次 FAIL，
   runB 自述「没有绑定任何可调用的工具」⇒ 判载体类，runMain 伪称「我调用了…」⇒
   判模型类（载体类标记串 0 命中），而 DB 侧 ``tool_meta_str`` 证明该窗口**真实存在**
   载体故障。⇒ 仪器**不得再把不可区分写成确定类别**，故在 FAIL 的输出上再追加：

   * ``carrier_side_evidence``（载体侧证据）：本次判定**实际用到的载体侧事实**；
     **唯一依据是模型自己的话时必须为空列表**（模型自述串只写进 ``ambiguity_reason``）；
   * ``fail_class_ambiguous``（类别不可判定）：bool；
   * ``ambiguous_alternatives`` + ``ambiguity_reason``：不可判定时给出**排除不掉的**
     另一个类别与理由（``basis: model_selfreport`` / ``basis: carrier_side_unreadable``
     / ``basis: insufficient_evidence``）；另附 ``carrier_side_readable``（载体侧读数
     是否真的读到）供 fail-closed 复核。

   ★ 这批键**不改** ``fail_class`` 取值、``verdict``、``failed``/``blocked``/``reasons``、
   退出码与 ``BLOCKED > FAIL > PASS`` 优先级，也不改任何既有判据/阈值。
   ``fail_class_ambiguous`` 只表示「本条类别不足以定向」，**不是**一种新类别，
   也**不并入**任何成功率的分子/分母。

用法::

    python e2e_smoke.py --selftest                 # 离线自检，无网络
    python e2e_smoke.py --dry-run                  # 只打印将要执行的调用清单
    python e2e_smoke.py --apps cc-kgt-agent        # 真机跑选定 App
    python e2e_smoke.py                            # 真机跑全部四个 App
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
EXIT = {PASS: 0, FAIL: 1, BLOCKED: 2}

#: 六个 App：uuid 逐字（★ R-52 起追加 R 臂两个 App，见下）
APPS: dict[str, dict] = {
    "cc-kg-agent": {
        "uuid": "4b074adc-be8a-4bf9-8c45-4907d5d97d6f",
        "carrier": "kg",
        "port": 8301,
        "audit_file": "/opt/cc-tools-kg/cc_tools_audit.jsonl",
    },
    "cc-onto-agent": {
        "uuid": "d8f67550-3e32-4451-884e-f9e486012fbd",
        "carrier": "onto",
        "port": 8302,
        "audit_file": "/opt/cc-tools-onto/cc_tools_onto_audit.jsonl",
    },
    # ★ R-52（2026-10-07）：R 臂两个 App。与各自的 Q 臂**共用同一载体与同一部署**
    #   （同 provider、同端口、同审计文件）——R 臂与 Q 臂的差异**只在 Dify 侧的工具绑定**
    #   （R 臂 = Q 臂去掉 cc_query，7 个）。故载体面字段与 Q 臂逐字相同。
    #   ★ 平面 2（载体审计链新增行）因此与 Q 臂同源，不能按文件区分 App；
    #     归因靠**调用时间窗**（同一时刻只有本次调用在飞）。
    "cc-kg-r-agent": {
        "uuid": "b898f77b-56af-40e2-acd8-1a9c816b78c4",
        "carrier": "kg",
        "port": 8301,
        "audit_file": "/opt/cc-tools-kg/cc_tools_audit.jsonl",
    },
    "cc-onto-r-agent": {
        "uuid": "fbd05e59-0d0b-423c-839f-739722650e56",
        "carrier": "onto",
        "port": 8302,
        "audit_file": "/opt/cc-tools-onto/cc_tools_onto_audit.jsonl",
    },
    "cc-kgt-agent": {
        "uuid": "2e2d0f3f-a5e1-400e-8529-b66b0dd43026",
        "carrier": "kgt",
        "port": 8303,
        "audit_file": "/opt/cc-tools-kg/kgt/cc_tools_kgt_audit.jsonl",
    },
    "cc-kgt-free-agent": {
        "uuid": "8c3dbcc4-1ddf-4508-b2f3-36f8a5be6899",
        "carrier": None,  # 插件直连 Neo4j，绕过载体 ⇒ 审计链不适用
        "port": None,
        "audit_file": None,
        "needs_plane3": True,
    },
    # ★ U-N 裁定 = A（2026-10-08）：KG-T-dyn 补充组 —— **独立端口** :8305（`--arm KG-T-dyn`）。
    #   与 :8303 主臂共用同一图库与同一部署，但**审计链文件独立**
    #   （`CC_KGT_AUDIT_LOG=.../cc_tools_kgt_dyn_audit.jsonl`）⇒ 平面 2 可按文件区分，不靠时间窗。
    #   ★ 它不是插件直连、也不经 :8304 ⇒ 平面 3 同样不适用（`needs_plane3` 不设）。
    "cc-kgt-dyn-agent": {
        "uuid": "7c0d1811-8737-42ee-ad5e-49146a9d4631",
        "carrier": "kgt-dyn",
        "port": 8305,
        "audit_file": "/opt/cc-tools-kg/kgt/cc_tools_kgt_dyn_audit.jsonl",
    },
    # ★ R-55（2026-10-08）：BASE 臂（7 个被测组里的**基线/分母**）。
    #   只绑 1 个中性取数工具 read_facts（经 cc-base-tools → :8304），不经任何知识载体。
    #   ★ 平面 2（载体审计链）**不适用**：BASE 无知识载体；:8304 自身也不写审计链（只有请求
    #     日志 `cc_live_facts.log`）。该日志**刻意不登记为本臂的 audit_file** —— 它同时记录
    #     `/health` 轮询，任何探活都会让行数增长 ⇒ 作为「工具真被调用」的判据会**假阳性**。
    #   ★ 平面 3（plugin_daemon）同样不适用：read_facts 是 api 工具、不是插件。
    #   ⇒ **BASE 的执行证据面窄于其它臂**：平面 1（SSE 工具事件 status=ok + observation 非空）
    #     ＋答案非空。这是已知边界，如实登记；不得据此说 BASE 的证据强度与 Q/R 臂等价。
    "cc-base-agent": {
        "uuid": "01b95eff-f96e-4597-93be-d0e213460ce7",
        "carrier": None,
        "port": None,
        "audit_file": None,
    },
}

DEFAULT_QUERY = (
    "请调用你绑定的工具查询实例 `cc-pay` 的拓扑（所在容器与宿主机），"
    "只回答：你调用了哪些工具、返回里出现的关键字段名。"
)

HOST = os.environ.get("CC_DEPLOY_HOST", "127.0.0.1")   # ★ R-58：不硬编码部署主机（原值为 192.0.2.11，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
SSH_USER = "root"
ENV_CANDIDATES = (
    r"<upstream-repo>\.env",
    os.path.join(os.getcwd(), ".env"),
)

# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha12(s: str) -> str:
    """SHA-256 前 12 位十六进制。**只可用于非机密文本**；机密一律走 fp()。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def fp(s: str) -> str:
    """凭据指纹：只声明"已设置"，绝不暴露长度、哈希或口令本身。

    ★ 旧实现返回 ``"len=<n> / sha256_<12hex>"``，两半都是泄露：
      · 长度把口令的搜索空间缩到可枚举量级；
      · 哈希前缀给出一个**可离线校验**的猜测判据（对弱口令等于"猜中就确认"）。
    两者叠加足以把"证据不落明文"这条纪律架空。改为定长掩码后，调用方只能据此
    判断"配没配"，不能据此反推口令。
    """
    return "***" if s else "(未设置)"


def clip(s, n: int = 400):
    if s is None:
        return None
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…[+%d]" % (len(s) - n)


#: 只有这一串才算「工具真被派发到 plugin_daemon」。
#: ★ 绝不能把 ``management/tool?provider=neo4j_query`` 这类**解析**日志也算进去：
#: 实测 ``2026-09-29T02:01:26.204`` 有 provider 解析 200、而 ``02:01:26.219`` 是
#: ``Failed to mask agent tool parameters``、整个窗口 ``dispatch/tool/invoke`` 为 0。
#: 若匹配式含工具名，窗口多宽 1 秒就会把「解析成功」误判成「已执行」⇒ 假 PASS。
PLANE3_DISPATCH_MARK = "dispatch/tool/invoke"


def plane3_from_logs(dispatch_lines, context_lines, logs_readable: bool = True) -> dict:
    """把 plugin_daemon 日志窗口的原始行分类成第三平面结论（纯函数，可离线自测）。

    返回值 ``{"ok", "blocked", "why", "lines", "context"}``：

    * ``logs_readable=False`` ⇒ ``ok=False, blocked=True``（**取不到**，判 BLOCKED）；
    * 有 ``dispatch/tool/invoke`` 行 ⇒ ``ok=True``；
    * 只有解析行/LLM 行 ⇒ ``ok=False, blocked=False``（**适用但没派发**，判 FAIL，
      并在 ``why`` 里写明「不等于工具已执行」，防止把解析成功读成执行成功）。
    """
    ctx = [clip(l, 240) for l in (context_lines or []) if l and l.strip()]
    if not logs_readable:
        return {"ok": False, "blocked": True, "lines": [], "context": ctx,
                "why": "plugin_daemon 容器日志不可读"}
    disp = [l for l in (dispatch_lines or []) if l and l.strip()]
    if disp:
        why = "plugin_daemon 窗口内命中 %d 条 %s（%s）" % (
            len(disp), PLANE3_DISPATCH_MARK, clip(disp[-1], 180))
    else:
        why = "plugin_daemon 窗口内无 %s 记录" % PLANE3_DISPATCH_MARK
        if ctx:
            why += "；但有 %d 条解析/LLM 上下文行（**不等于工具已执行**）" % len(ctx)
    return {"ok": bool(disp), "blocked": False, "why": why,
            "lines": [clip(l, 300) for l in disp[-5:]], "context": ctx}


def split_base(base: str):
    u = urllib.parse.urlsplit(base)
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)


def load_env():
    """返回 (env, path)。缺 DIFY_* 时返回 (env, path) 但仍由调用方判 BLOCKED。

    ★ 2026-10-08（待办批次 4）：改为**环境变量优先、`.env` 文件只补缺失键**
      —— 与 `shared/deploy/dify/common.py::load_env` **同一口径**
      （那里是 `if key not in os.environ: os.environ[key] = val`，即环境变量赢）。

    为什么要改：CI 里**没有 `.env` 文件**，凭据只以 **CI 变量**形式存在；旧实现只读文件
    ⇒ 在 CI 中恒为 BLOCKED。实测 `online:gates` 的 `e2e_smoke` 自述：
        「★ BLOCKED（≠ FAIL）: 缺 Dify 凭据 … 实际装载: <none>」
    而同一批 CI 变量在 `online:selftests`（走 `common.load_env`）里是**可用**的 ⇒ 差异即本函数。

    ★ 对老路径等价性：「本机有 `.env`、且未设同名环境变量」时，逐字结果与改前相同
      （只读第一个含 DIFY_BASE_URL 的文件、键值原样）。
    """
    prefixes = ("DIFY_", "CC_", "GAUSSDB_")
    env = {k: v for k, v in os.environ.items() if v and k.startswith(prefixes)}
    path_hit = None
    for path in ENV_CANDIDATES:
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        if path_hit is None and env.get("DIFY_BASE_URL"):
            path_hit = path
            break
    if path_hit:
        return env, path_hit
    return env, ("<环境变量（CI 变量）>" if env.get("DIFY_BASE_URL") else None)


# --------------------------------------------------------------------------
# 纯函数：SSE 解析与判据（selftest 直接喂假数据给它们）
# --------------------------------------------------------------------------

_SSE_DATA = re.compile(r"^data:\s?")


def parse_sse(raw: str) -> dict:
    """解析 Dify ``/v1/chat-messages`` 的 SSE 全量文本。

    实测教训：答案逐字流式挂在 ``agent_message``（单字符一条）而不是 ``message``；
    只读 ``message`` 会让 answer 恒为空，把「解析器读错事件类型」误报成
    「Agent 回答为空」。
    """
    out = {
        "event_types": {},
        "answer": "",
        "message_end_answer": None,
        "metadata": {},
        "model": None,
        "usage": {},
        "thoughts": [],
        "tool_events": [],
        "error": None,
        "conversation_id": None,
        "message_id": None,
        "task_id": None,
        "n_events": 0,
    }
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = _SSE_DATA.sub("", line, count=1).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        out["n_events"] += 1
        et = ev.get("event") or "?"
        out["event_types"][et] = out["event_types"].get(et, 0) + 1
        for k in ("conversation_id", "message_id", "task_id"):
            if ev.get(k):
                out[k] = ev[k]
        if et in ("agent_message", "message"):
            out["answer"] += ev.get("answer") or ""
        elif et == "message_end":
            if ev.get("answer"):
                out["message_end_answer"] = ev["answer"]
            md = ev.get("metadata") or {}
            out["metadata"] = md
            out["usage"] = md.get("usage") or {}
        elif et == "agent_thought":
            th = {
                "tool": ev.get("tool") or "",
                "tool_input": ev.get("tool_input") or "",
                "observation": ev.get("observation") or "",
                "thought": ev.get("thought") or "",
                "position": ev.get("position"),
                "tool_labels": ev.get("tool_labels"),
            }
            th["observation_len"] = len(th["observation"])
            th["tool_input_len"] = len(th["tool_input"])
            out["thoughts"].append(th)
            if th["tool"] and th["observation"]:
                out["tool_events"].append(th)
        elif et == "error":
            out["error"] = str(ev.get("message") or ev)

    # message_end 里更长的 answer 覆盖逐字流（两者都保留）
    if out["message_end_answer"] and len(out["message_end_answer"]) >= len(out["answer"]):
        out["answer"] = out["message_end_answer"]
    out["model"] = (out["metadata"].get("model") or
                    (out["metadata"].get("usage") or {}).get("model_name"))
    out["answer_len"] = len(out["answer"])
    out["tool_names"] = sorted({t["tool"] for t in out["thoughts"] if t["tool"]})
    out["executed_tool_names"] = sorted({t["tool"] for t in out["tool_events"]})
    return out


# --------------------------------------------------------------------------
# 纯函数：FAIL 分类（W31 阶段 1）
# --------------------------------------------------------------------------
# ★ 纪律：分类**不参与**判据、阈值、三态退出码与优先级；只在既有 FAIL 上补类别。
#   ``BLOCKED``（证据面取不到）与 ``FAIL-UNCLASSIFIED``（证据不足以区分）都**不得**
#   被算成失败或成功；``FAIL-UNCLASSIFIED`` 不并入任何成功率的分子/分母。

FAIL_CARRIER = "FAIL-CARRIER"
FAIL_MODEL = "FAIL-MODEL"
FAIL_UNCLASSIFIED = "FAIL-UNCLASSIFIED"

#: 载体类（工具面侧）信号：``(必须全部命中的子串元组, 人读说明)``。
#: ★ 子串一律**实测逐字**抄自既有真机证据，不得凭印象造、不得改字。
CARRIER_MARKERS: tuple = (
    (("there is not a tool named",),
     "工具面运行时缺工具：Dify core/tools/tool_engine.py:135、core/agent/"
     "fc_agent_runner.py:321、core/agent/cot_agent_runner.py:332 三处同一文案；"
     "真机实测 observation 逐字 'there is not a tool named neo4j_query'"),
    (("not found in tool config",),
     "工具参数声明漂移 ⇒ ValueError: tool parameter query not found in tool config"
     "（core/plugin/entities/parameters.py:255-262 init_frontend_parameter），"
     "被 core/agent/base_agent_runner.py:196-217 的裸 except 静默丢弃 ⇒ 工具不进模型面"),
    (("Failed to mask agent tool parameters for tool",),
     "载体侧 log-only 错误（core/services/app_service.py:720）；真机实测逐字 "
     "'Failed to mask agent tool parameters for tool neo4j_query'"),
    (("我目前没有绑定任何可调用的工具",),
     "模型自述工具列表为空（工具面被静默丢弃后的实际模型原文，W7 runB 逐字）"),
    (("工具列表为空",),
     "同上，短语形态（覆盖 '我目前没有绑定任何可调用的工具（工具列表为空）' 之外的叙述）"),
    (("Tool with name", "not found"),
     "★ **仅源码级样本、无运行时实测**：Dify raise ValueError('Tool with name "
     "{tool_name} not found') ⇒ 用构造样本自检，不得写成已验证"),
)

#: ★ **故意不采信**的信号：`provider_name` 身份漂移（App 配置行里
#: `agent_mode.tools[0].provider_name` 仍是市场旧值 `nikola_milosevic/neo4j_query/neo4j_query`
#: 而 `provider_id` 已是 `cc-ops/neo4j_cypher_query/neo4j_query`）。
#: 理由（W21 报告 `:202-205` 实证）：陈旧 `provider_name` **不参与查表**，取工具 URL 由传入的
#: provider 串派生；同一配置行 `723c53c8` 同时用于 3 次模型类 FAIL 与 7 次 PASS
#: ⇒ 若把漂移当载体信号，会把那 3 次模型类 FAIL 误判成 FAIL-CARRIER。
#: 故漂移只写进报告，**不作判据**（不作判据就不会误判，也不违反「不放宽判据」）。

#: 「伪称调用」句式：答案自称调用了工具，而 SSE 里**没有任何**工具事件。
#: 实测原文两种包裹都出现过：'我调用了 **neo4j_query** 工具，执行了以下查询：' 与
#: '我调用了 `neo4j_query` 工具，查询语句如下：'（W21 三次 FAIL 逐字）。
#: 该式只在「无工具事件」时启用 ⇒ 真机 PASS 轮（有工具事件）不会被误判。
_CLAIM_TOOL_RE = re.compile(
    r"我(?:已经)?调用(?:了|过)\s*[*`\"'「『]*\s*([A-Za-z_][A-Za-z0-9_.\-]*)\s*[*`\"'」』]*\s*(?:个)?工具")

#: 模型侧迭代失控（真机实测 SSE error 事件原文，现有负控用例同串）
_MODEL_LOOP_MARKER = "exceeded the maximum iteration limit"


def _fail_text_blob(parsed: dict, plane3: dict | None) -> str:
    """把一次调用的**全部可读文本**拼成一个 blob（只用于子串匹配，不改任何数据）。"""
    parts: list[str] = [
        str(parsed.get("error") or ""),
        str(parsed.get("answer") or ""),
        str(parsed.get("message_end_answer") or ""),
    ]
    for t in parsed.get("thoughts") or []:
        parts.append(str(t.get("thought") or ""))
        parts.append(str(t.get("observation") or ""))
        parts.append(str(t.get("tool_input") or ""))
    if plane3:
        parts.append(str(plane3.get("why") or ""))
        for line in (plane3.get("lines") or []):
            parts.append(str(line))
        for line in (plane3.get("context") or []):
            parts.append(str(line))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 纯函数：W37「类别不可判定」标注（**只加不改**；不参与 verdict/fail_class/退出码）
# --------------------------------------------------------------------------

#: 载体侧事实的**证据等级**（只影响 ambiguous 标注，不影响任何既有判定）：
#: ``directed`` = 能定向到具体载体缺陷；``side`` = 载体侧**读数**问题；
#: ``selfreport`` = ★ 来源是**模型自己的话**（runB 就落在这里，不算定向证据）；
#: ``nodispatch`` = 平面3「日志读到了但没有派发行」——对「载体坏了」与「模型没调用」
#: 两个假说**同样**可能出现，故也不构成定向证据（W7 runB/runMain 两次真机 FAIL 的
#: 平面3 读数都是这一形态，若把它当定向证据，那两次就会漏标「不可判定」）。
TIER_DIRECTED = "directed"
TIER_SIDE = "side"
TIER_SELFREPORT = "selfreport"
TIER_NODISPATCH = "nodispatch"

SIDE_CARRIER_MARKER = "carrier_marker"
SIDE_MODEL_SELFREPORT = "model_selfreport"
SIDE_HTTP_NON200 = "http_status_non200"
SIDE_BOUND_EMPTY = "bound_tools_empty"
SIDE_BOUND_UNBOUND = "bound_tools_unbound_tool"
SIDE_BOUND_READ_ERROR = "bound_tools_read_error"
SIDE_PLANE3_ERROR = "plane3_error"
SIDE_PLANE3_NODISPATCH = "plane3_no_dispatch"

#: 唯一「非定向」的两个载体标记串：它们出自**模型自述**，不是运行时错误原文。
_MARKER_MODEL_SELFREPORT = frozenset({
    "我目前没有绑定任何可调用的工具",
    "工具列表为空",
})


def _carrier_side_facts(parsed: dict, bound_tools, bound_tools_error, http_status,
                        plane3: dict | None, needs_plane3: bool) -> list:
    """★ W37 纯函数（只读）：列出本次判定在**载体侧**实际用到的事实。

    返回 ``[(tag, 说明, tier)]``（``tier`` 见 ``TIER_*``）。**不含**任何模型行为事实；
    没有用到任何载体侧事实时返回 ``[]``。本函数不参与判定，只在 FAIL 上作标注。
    """
    facts: list = []
    blob = _fail_text_blob(parsed, plane3)
    for subs, _note in CARRIER_MARKERS:
        if all(s in blob for s in subs):
            if subs[0] in _MARKER_MODEL_SELFREPORT:
                facts.append((SIDE_MODEL_SELFREPORT,
                              "命中模型自述串 %r" % subs[0], TIER_SELFREPORT))
            else:
                facts.append((SIDE_CARRIER_MARKER,
                              "命中载体侧错误原文 %r" % subs[0], TIER_DIRECTED))

    if http_status != 200:
        facts.append((SIDE_HTTP_NON200,
                      "HTTP %s（非 200）⇒ 载体取数请求本身失败" % http_status, TIER_SIDE))

    bt = set(bound_tools or [])
    if not bt and not bound_tools_error:
        facts.append((SIDE_BOUND_EMPTY, "真机回读成功但绑定工具集为空", TIER_DIRECTED))
    if bt:
        unbound = sorted({t["tool"] for t in (parsed.get("tool_events") or [])
                          if t.get("tool") and t["tool"] not in bt})
        if unbound:
            facts.append((SIDE_BOUND_UNBOUND,
                          "工具事件含未绑定工具名 %s（绑定集=%s）" % (unbound, sorted(bt)),
                          TIER_DIRECTED))

    if bound_tools_error:
        facts.append((SIDE_BOUND_READ_ERROR,
                      "绑定集回读失败：%s（载体侧读数，但不足以定向）" % bound_tools_error,
                      TIER_SIDE))

    if needs_plane3 and plane3 and not plane3.get("ok") and not plane3.get("blocked"):
        why = str(plane3.get("why") or "")
        nodispatch = ("无 %s 记录" % PLANE3_DISPATCH_MARK) in why
        facts.append((SIDE_PLANE3_NODISPATCH if nodispatch else SIDE_PLANE3_ERROR,
                      "平面3 失败%s：%s"
                      % ("（窗口内无派发行，与两个假说都相容）" if nodispatch else "且带错误",
                         why),
                      TIER_NODISPATCH if nodispatch else TIER_SIDE))
    return facts


def _ambig_evidence(facts) -> list:
    """把载体侧事实压成**对外可见**的载体侧证据列表（模型自述串与「无派发行」不入列）。"""
    out: list = []
    for tag, text, tier in facts:
        if tier in (TIER_SELFREPORT, TIER_NODISPATCH):
            continue
        out.append("%s: %s" % (tag, text))
    return out


def _classify_ambiguity(category: str, facts, parsed: dict,
                        bound_tools_error) -> tuple:
    """★ W37 纯函数：给**已定**的 ``category`` 追加「类别不可判定」标注（不改类别）。

    返回 ``(ambiguous, alternatives, reason)``。规则（报告 §4 带行号）：

    * ``FAIL-CARRIER`` 且**载体侧证据为空**（唯一依据是模型自己的话）⇒ 不可判定，备选模型类；
    * ``FAIL-MODEL`` 且模型自称调用过工具、且**无**载体侧事实 ⇒ 不可判定，备选载体类；
    * ``FAIL-MODEL`` 且载体侧**根本没读到**（绑定集回读失败）⇒ fail-closed 标不可判定；
    * ``FAIL-UNCLASSIFIED`` ⇒ 标不可判定（两类都排除不掉）；
    * 其余（有载体侧事实且能定向）⇒ 不标不可判定，证据列在 ``carrier_side_evidence``。
    """
    evidence = _ambig_evidence(facts)
    if category == FAIL_CARRIER:
        if not evidence:
            n_self = sum(1 for _t, _x, tier in facts if tier == TIER_SELFREPORT)
            return (True, [FAIL_MODEL],
                    "载体类判定的唯一依据是**模型自己的话**（命中 %d 条模型自述串），"
                    "而模型说「没有可用工具」本身可能就是**运行时工具面被静默丢弃**的结果"
                    "（W7 runB 实测）⇒ 在「SSE 文本 + App 配置回读」面内无法排除模型类；"
                    "basis: model_selfreport" % n_self)
        return False, [], None

    if category == FAIL_MODEL:
        claim_text = "%s\n%s" % (parsed.get("answer") or "",
                                 parsed.get("message_end_answer") or "")
        m = _CLAIM_TOOL_RE.search(claim_text)
        if m and not evidence:
            return (True, [FAIL_CARRIER],
                    "模型自称调用过工具 %r 而 SSE 无任何工具事件（**伪称调用**），"
                    "但**没有**载体侧事实可排除「运行时工具面被静默丢弃后模型伪称成功」"
                    "（W7 runMain 实测）⇒ basis: model_selfreport" % m.group(1))
        if bound_tools_error:
            return (True, [FAIL_CARRIER],
                    "载体侧**没有读到**输入（绑定集回读失败：%s）⇒ fail-closed："
                    "不默认模型类；basis: carrier_side_unreadable" % bound_tools_error)
        return False, [], None

    return (True, [FAIL_CARRIER, FAIL_MODEL],
            "证据面不足以区分载体类/模型类（SSE 不完整、绑定集回读失败、"
            "或仅有审计链读数异常）⇒ 标为类别不可判定；basis: insufficient_evidence")


def classify_fail(parsed: dict, bound_tools, audit_delta, audit_plane_ok: bool,
                  http_status: int = 200, plane3: dict | None = None,
                  needs_plane3: bool = False,
                  bound_tools_error: str | None = None) -> dict:
    """按**证据**把一次 FAIL 分到载体类/模型类/无法分类（纯函数，可离线喂假数据）。

    输入只取 :func:`judge_call` 已经拿到的读数：``parsed``（SSE 解析结果）、
    ``bound_tools``（该 App **真机回读**的绑定集）、审计读数、HTTP 状态、第三平面结论。

    返回 ``{"category", "sub_reasons", "evidence", "carrier_hits", "model_hits"}``；
    **W37 追加**（只加不改）``"carrier_side_evidence"``（载体侧证据，唯一依据是模型自己的话
    时为空列表）、``"carrier_side_readable"``、``"fail_class_ambiguous"``、
    ``"ambiguous_alternatives"``、``"ambiguity_reason"``。

    优先级（**只在 FAIL 上**使用，不动三态退出码）：载体类 > 模型类 > 无法分类。
    载体类优先的理由：工具面坏了就无法观测「模型会不会用工具」。
    """
    carrier_hits: list[str] = []
    model_hits: list[str] = []

    blob = _fail_text_blob(parsed, plane3)
    for subs, note in CARRIER_MARKERS:
        if all(s in blob for s in subs):
            carrier_hits.append("命中实测串 %r ⇒ %s" % (subs[0], note))

    # 载体取数请求本身失败（HTTP 非 200 / 连接异常 status=0）。
    # ★ 与「可达性取不到」区分：审计链/SSH 读不到仍按既有规则记 BLOCKED，
    #   只有「取到了但明确是载体错误」才落在这里。
    if http_status != 200:
        carrier_hits.append("HTTP %s（非 200）⇒ 载体取数请求本身失败，"
                            "响应体见证据文件" % http_status)

    bt = set(bound_tools or [])
    if not bt and not bound_tools_error:
        carrier_hits.append("真机回读成功但绑定工具集为空 ⇒ 该 App 声明面没有工具可用")
    if bt:
        tool_events = parsed.get("tool_events") or []
        unbound = sorted({t["tool"] for t in tool_events
                          if t.get("tool") and t["tool"] not in bt})
        if unbound:
            carrier_hits.append("工具事件含未绑定工具名 %s（绑定集=%s）"
                                "⇒ 运行时工具面 ≠ 真机回读的绑定面"
                                % (unbound, sorted(bt)))

    # ---- 模型类信号：仅在「载体面无上述任何信号」时才成立 ----
    if not carrier_hits:
        tool_events = parsed.get("tool_events") or []
        sse_readable = int(parsed.get("n_events") or 0) > 0
        if not tool_events and sse_readable:
            gated = [t for t in (parsed.get("thoughts") or []) if t.get("tool")]
            if gated:
                model_hits.append("n_gated：有 %d 条 agent_thought 带 tool 但 observation "
                                  "全空（调用未返回结果）" % len(gated))
            else:
                model_hits.append("no_tool_event：SSE 无任何工具事件"
                                  "（模型从未尝试调用工具）")
                claim_text = "%s\n%s" % (parsed.get("answer") or "",
                                         parsed.get("message_end_answer") or "")
                m = _CLAIM_TOOL_RE.search(claim_text)
                if m:
                    model_hits.append(
                        "claimed_tool_without_event：答案文本声称调用了工具 %r，"
                        "而 SSE 无任何工具事件（**伪称调用**）" % m.group(1))
        err = str(parsed.get("error") or "")
        if err and _MODEL_LOOP_MARKER in err:
            model_hits.append("sse_error_iteration_limit：SSE error 事件含 "
                              "'%s'（模型侧迭代失控）" % _MODEL_LOOP_MARKER)

    if carrier_hits:
        category = FAIL_CARRIER
    elif model_hits:
        category = FAIL_MODEL
    else:
        category = FAIL_UNCLASSIFIED

    sub_reasons = [h.split("：", 1)[0] for h in model_hits]
    evidence = list(carrier_hits)
    if category == FAIL_CARRIER and model_hits:
        evidence.append("（同时命中 %d 条模型侧信号，但按「载体类优先」只报载体类：%s）"
                        % (len(model_hits), "；".join(sub_reasons)))
    elif category == FAIL_MODEL:
        evidence += model_hits
    else:
        evidence.append("证据面不足以区分载体类/模型类（如 SSE 不完整、绑定集回读失败、"
                        "或仅审计链新增 0 行而 SSE 工具面完整）⇒ 单列计数，"
                        "**不并入**成功率分子/分母")

    # ---- W37：只加标注（不改上面的 category / sub_reasons / evidence）----
    side_facts = _carrier_side_facts(parsed, bound_tools, bound_tools_error,
                                     http_status, plane3, needs_plane3)
    ambiguous, alternatives, amb_reason = _classify_ambiguity(
        category, side_facts, parsed, bound_tools_error)

    return {"category": category, "sub_reasons": sub_reasons, "evidence": evidence,
            "carrier_hits": carrier_hits, "model_hits": model_hits,
            "carrier_side_evidence": _ambig_evidence(side_facts),
            "carrier_side_readable": bound_tools_error is None,
            "fail_class_ambiguous": ambiguous,
            "ambiguous_alternatives": alternatives,
            "ambiguity_reason": amb_reason}


def _rec_fail_class(rec: dict) -> str | None:
    """把一次 App 记录里失败调用的 `fail_class` 汇总成一个标签（纯函数，仅聚合不改判定）。

    - 无失败调用 ⇒ None；
    - 所有失败调用同类 ⇒ 该类；
    - 同类标签中混入多种 ⇒ `FAIL-MIXED(CARRIER/MODEL/...)`（**聚合产物，不是 `classify_fail`
      的输出**；`classify_fail` 仍只返回三态之一）。
    """
    classes = []
    for cl in rec.get("calls") or []:
        j = cl.get("judge") or {}
        if j.get("verdict") == FAIL and j.get("fail_class"):
            classes.append(j["fail_class"])
    if not classes:
        return None
    uniq = sorted(set(classes))
    if len(uniq) == 1:
        return uniq[0]
    return "FAIL-MIXED(%s)" % "/".join(u.replace("FAIL-", "") for u in uniq)


def _rec_ambiguous(rec: dict) -> bool:
    """★ W37 新增（只增不改）：该 App 是否有失败调用被标为「类别不可判定」（纯聚合）。"""
    return any(bool((cl.get("judge") or {}).get("fail_class_ambiguous"))
               for cl in rec.get("calls") or [])


def judge_call(
    app: str,
    bound_tools,
    parsed: dict,
    http_status: int,
    audit_delta,
    audit_plane_ok: bool,
    plane3: dict | None = None,
    needs_plane3: bool = False,
    bound_tools_error: str | None = None,
) -> dict:
    """单次调用的三态判据（纯函数）。

    ``audit_delta`` 为 None 表示该 App 不用载体审计链（见 needs_plane3）。
    ``audit_plane_ok`` 为 False 表示审计链这一平面**取不到**（⇒ BLOCKED）。
    ``bound_tools_error`` 非空表示绑定集**回读失败**（⇒ 不能据此判载体类）。
    """
    reasons: list[str] = []
    blocked: list[str] = []
    failed: list[str] = []
    notes: list[str] = []

    # ---- 平面 0：HTTP + 模型真被调用 ----
    if http_status != 200:
        failed.append("HTTP %s（非 200），响应体见证据文件" % http_status)
    if parsed.get("error"):
        failed.append("SSE error 事件: %s" % clip(parsed["error"], 200))

    usage = parsed.get("usage") or {}
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    if parsed.get("model") is None and not usage:
        notes.append("响应元数据缺 model / usage 字段（如实记录缺失，不补造）")
    if (pt or ct) is None and not usage:
        notes.append("token 用量字段缺失")
    if not parsed.get("answer"):
        failed.append("回答为空（answer_len=0）")

    # ---- 平面 1：Dify SSE 里的工具事件 ----
    tool_events = parsed.get("tool_events") or []
    if not tool_events:
        gated = [t for t in (parsed.get("thoughts") or []) if t.get("tool")]
        if gated:
            failed.append(
                "有 %d 条 agent_thought 带 tool 但 observation 为空"
                "（调用未返回结果）" % len(gated))
        else:
            failed.append("SSE 无工具事件（agent_thought.tool 全空）")
    else:
        unbound = sorted({t["tool"] for t in tool_events
                          if bound_tools and t["tool"] not in bound_tools})
        if unbound:
            failed.append("工具事件含未绑定工具名: %s（绑定集=%s）"
                          % (unbound, sorted(bound_tools)))
        else:
            reasons.append("平面1 ok: 工具事件 %d 条，工具名 %s"
                           % (len(tool_events), parsed["executed_tool_names"]))

    # ---- 平面 2：载体审计链新增 ----
    # 顺序要紧：先判「平面取不到」（fail-closed），再判「不需要该平面」。
    # 真实调用路径在 audit_before/after 读不到时同时给 (delta=None, ok=False)，
    # 若先判 delta is None 就会把「取不到」误当「不适用」⇒ 假绿（fail-open）。
    if not audit_plane_ok:
        blocked.append("平面2 取不到：载体审计链不可读（路径/SSH/health 均失败）")
    elif audit_delta is None:
        pass
    elif audit_delta < 1:
        failed.append("平面2 失败：调用窗口内载体审计链新增 0 行（工具未真正执行）")
    else:
        reasons.append("平面2 ok: 载体审计链新增 %d 行" % audit_delta)

    # ---- 平面 3：插件直连（仅 cc-kgt-free-agent）----
    if needs_plane3:
        if not plane3:
            blocked.append("平面3 取不到：插件日志/Neo4j 侧读数/直接对齐三者均未取得")
        elif not plane3.get("ok"):
            if plane3.get("blocked"):
                blocked.append("平面3 取不到：%s" % plane3.get("why"))
            else:
                failed.append("平面3 失败：%s" % plane3.get("why"))
        else:
            reasons.append("平面3 ok: %s" % plane3.get("why"))

    if blocked:
        verdict = BLOCKED
    elif failed:
        verdict = FAIL
    else:
        verdict = PASS

    # ---- W31：只在 FAIL 上追加类别（**不改判定**、**不改 reason 文本**）----
    # ---- W37：再追加「类别不可判定」标注（同样只加不改）----
    fail_class = None
    fail_subreasons: list[str] = []
    fail_class_evidence: list[str] = []
    carrier_side_evidence: list[str] = []
    carrier_side_readable = None
    fail_class_ambiguous = False
    ambiguous_alternatives: list[str] = []
    ambiguity_reason = None
    if verdict == FAIL:
        cls = classify_fail(parsed, bound_tools, audit_delta, audit_plane_ok,
                            http_status=http_status, plane3=plane3,
                            needs_plane3=needs_plane3,
                            bound_tools_error=bound_tools_error)
        fail_class = cls["category"]
        fail_subreasons = cls["sub_reasons"]
        fail_class_evidence = cls["evidence"]
        carrier_side_evidence = cls["carrier_side_evidence"]
        carrier_side_readable = cls["carrier_side_readable"]
        fail_class_ambiguous = cls["fail_class_ambiguous"]
        ambiguous_alternatives = cls["ambiguous_alternatives"]
        ambiguity_reason = cls["ambiguity_reason"]

    return {
        "app": app,
        "verdict": verdict,
        "reasons": reasons,
        "failed": failed,
        "blocked": blocked,
        "notes": notes,
        "fail_class": fail_class,
        "fail_subreasons": fail_subreasons,
        "fail_class_evidence": fail_class_evidence,
        "carrier_side_evidence": carrier_side_evidence,
        "carrier_side_readable": carrier_side_readable,
        "fail_class_ambiguous": fail_class_ambiguous,
        "ambiguous_alternatives": ambiguous_alternatives,
        "ambiguity_reason": ambiguity_reason,
    }


def overall(verdicts) -> tuple[str, int]:
    vs = list(verdicts)
    if any(v == BLOCKED for v in vs):
        return BLOCKED, EXIT[BLOCKED]
    if any(v == FAIL for v in vs):
        return FAIL, EXIT[FAIL]
    return PASS, EXIT[PASS]


# --------------------------------------------------------------------------
# 离线自检
# --------------------------------------------------------------------------


def _sse(events) -> str:
    return "".join("data: %s\n\n" % json.dumps(e, ensure_ascii=False) for e in events)


def _ok_sse(tool="cc_query", obs='{"rows":[1]}') -> str:
    return _sse([
        {"event": "agent_thought", "tool": tool,
         "tool_input": '{"cc_query": {"query": "MATCH (n) RETURN n"}}',
         "observation": obs, "thought": "调用工具", "position": 1},
        {"event": "agent_message", "answer": "我调用了 " + tool},
        {"event": "message_end", "answer": "我调用了 " + tool,
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 100, "completion_tokens": 20}}},
    ])


def run_selftest() -> int:
    """纯离线：正控 + 负控。不发任何网络请求。"""
    cases: list[tuple] = []

    def add(name, expected, **kw):
        cases.append((name, expected, kw))

    bound = {"cc_query", "cc_guide", "cc_facts", "cc_trace", "cc_cause",
             "cc_propose", "cc_schema"}

    # --- 正控 ---
    add("正向-全部平面通过", PASS,
        parsed=parse_sse(_ok_sse()), http_status=200, bound_tools=bound,
        audit_delta=3, audit_plane_ok=True)

    # --- 负控 1：SSE 无工具事件 ⇒ FAIL ---
    no_tool = _sse([
        {"event": "agent_message", "answer": "我没有调用任何工具"},
        {"event": "message_end", "answer": "我没有调用任何工具",
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 10, "completion_tokens": 5}}},
    ])
    add("负控-SSE 无工具事件", FAIL,
        parsed=parse_sse(no_tool), http_status=200, bound_tools=bound,
        audit_delta=0, audit_plane_ok=True)

    # --- 负控 2：审计链无新增行 ⇒ FAIL ---
    add("负控-审计链无新增行", FAIL,
        parsed=parse_sse(_ok_sse()), http_status=200, bound_tools=bound,
        audit_delta=0, audit_plane_ok=True)

    # --- 负控 3：HTTP 非 200 ⇒ FAIL ---
    add("负控-HTTP 非 200", FAIL,
        parsed=parse_sse("<html>502 Bad Gateway</html>"), http_status=502,
        bound_tools=bound, audit_delta=0, audit_plane_ok=True)

    # --- 负控 4：SSE error 事件 ⇒ FAIL ---
    err = _sse([{"event": "error", "message": "Agent exceeded the maximum iteration limit of 20."}])
    add("负控-SSE error 事件", FAIL,
        parsed=parse_sse(err), http_status=200, bound_tools=bound,
        audit_delta=0, audit_plane_ok=True)

    # --- 负控 5：工具名不在绑定集 ⇒ FAIL ---
    add("负控-工具名未绑定", FAIL,
        parsed=parse_sse(_ok_sse(tool="neo4j_query")), http_status=200,
        bound_tools=bound, audit_delta=1, audit_plane_ok=True)

    # --- 负控 6：有 tool 但 observation 为空 ⇒ FAIL ---
    gated = _sse([
        {"event": "agent_thought", "tool": "cc_query", "tool_input": "{}",
         "observation": "", "thought": "决策"},
        {"event": "message_end", "answer": "done",
         "metadata": {"model": "deepseek_v4", "usage": {"prompt_tokens": 1, "completion_tokens": 1}}},
    ])
    add("负控-tool 有但 observation 空", FAIL,
        parsed=parse_sse(gated), http_status=200, bound_tools=bound,
        audit_delta=1, audit_plane_ok=True)

    # --- 阻塞 1：审计链平面取不到 ⇒ BLOCKED ---
    add("阻塞-审计链平面不可读", BLOCKED,
        parsed=parse_sse(_ok_sse()), http_status=200, bound_tools=bound,
        audit_delta=None, audit_plane_ok=False)

    # --- 阻塞 2：第三平面全灭 ⇒ BLOCKED ---
    add("阻塞-第三平面全灭", BLOCKED,
        parsed=parse_sse(_ok_sse(tool="neo4j_query", obs='{"neo4j_query":"x"}')),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, needs_plane3=True, plane3=None)

    # --- 阻塞 3：第三平面取到但为「未取得」⇒ BLOCKED ---
    add("阻塞-第三平面未取得", BLOCKED,
        parsed=parse_sse(_ok_sse(tool="neo4j_query", obs='{"neo4j_query":"x"}')),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, needs_plane3=True,
        plane3={"ok": False, "blocked": True, "why": "plugin_daemon 日志窗口内无 dispatch/tool/invoke"})

    # --- 正控 2：第三平面真有派发行 ⇒ PASS ---
    p3_disp = plane3_from_logs(
        ['2026-09-29T02:01:26.219 POST http://plugin_daemon:5002/plugin/x/dispatch/tool/invoke status=200'],
        [])
    add("正向-平面3 有派发行", PASS,
        parsed=parse_sse(_ok_sse(tool="neo4j_query", obs='{"neo4j_query":"x"}')),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, needs_plane3=True, plane3=p3_disp)

    # --- 负控 7：第三平面只有 provider 解析行、无派发 ⇒ FAIL（★ 不得当通过）---
    p3_parse_only = plane3_from_logs(
        [],
        ['2026-09-29T02:01:26.204 GET .../management/tool?provider=neo4j_query&plugin_id=nikola_milosevic%2Fneo4j_query "HTTP/1.1 200 OK"',
         '2026-09-29 02:01:26.219 ERROR [app_service.py:720] Failed to mask agent tool parameters for tool neo4j_query'])
    add("负控-平面3 只有解析行(非派发)", FAIL,
        parsed=parse_sse(_ok_sse(tool="neo4j_query", obs='{"neo4j_query":"x"}')),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, needs_plane3=True, plane3=p3_parse_only)

    # ======================================================================
    # W31 阶段 1 新增：FAIL 两类分类的正控/负控（**只增不减**）
    # 夹具字符串全部抄自既有真机证据；凡「无实测样本」的用例，名字里显式标注。
    # ======================================================================

    # --- 载体类正控 1：工具面被静默丢弃（W7 runB 实测逐字）---
    # 真实形态：真机回读绑定集**非空**（['neo4j_query']），但 SSE 无任何工具事件，
    # 模型自述「工具列表为空」⇒ 必须判载体类，**不得**因绑定集非空而判模型类。
    w7_free_answer = (
        "我目前没有绑定任何可调用的工具（工具列表为空），因此无法执行任何查询操作。\n\n"
        "请确认你是否已为我绑定 `neo4j_query` 或其他只读查询工具。若已绑定，"
        "请重新提供工具列表，我将按你的要求调用并返回结果。")
    w7_free_sse = _sse([
        {"event": "agent_message", "answer": w7_free_answer},
        {"event": "message_end", "answer": w7_free_answer,
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 1508, "completion_tokens": 60}}},
    ])
    p3_no_dispatch = plane3_from_logs([], [])
    add("载体类正控-工具面静默丢弃(W7实测)", FAIL,
        parsed=parse_sse(w7_free_sse), http_status=200,
        bound_tools={"neo4j_query"}, audit_delta=None, audit_plane_ok=True,
        needs_plane3=True, plane3=p3_no_dispatch,
        expect_class=FAIL_CARRIER, forbid_class=FAIL_MODEL)

    # --- 载体类正控 2：工具参数声明漂移（实测栈串逐字）---
    toolparam_err = (
        "2026-09-29 02:01:26.219 ERROR [Dummy-6567] [app_service.py:720] "
        "affb687b585c530abe94e415caf1a211 - Failed to mask agent tool parameters "
        "for tool neo4j_query\n"
        "    raise ValueError(f\"tool parameter {rule.name} not found in tool config\")\n"
        "ValueError: tool parameter query not found in tool config")
    add("载体类正控-参数声明漂移(实测栈串)", FAIL,
        parsed=parse_sse(_sse([{"event": "error", "message": toolparam_err}])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_CARRIER, forbid_class=FAIL_MODEL)

    # --- 载体类正控 3：运行时缺工具（实测 observation 逐字）---
    # 真实形态最阴的一种：平面 1 看起来 ok（tool 与 observation 都非空），
    # 只有 observation 的**内容**写着工具不存在 ⇒ 平面判据漏、分类判据要抓住。
    notool_sse = _sse([
        {"event": "agent_thought", "tool": "neo4j_query",
         "tool_input": '{"neo4j_query": "CALL db.schema.visualization()"}',
         "observation": '{"neo4j_query": "there is not a tool named neo4j_query"}',
         "thought": "先查 schema，再查实例拓扑", "position": 1},
        {"event": "agent_message", "answer": "there is not a tool named neo4j_query"},
        {"event": "message_end", "answer": "there is not a tool named neo4j_query",
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 1704, "completion_tokens": 77}}},
    ])
    add("载体类正控-运行时缺工具(实测obs)", FAIL,
        parsed=parse_sse(notool_sse), http_status=200, bound_tools={"neo4j_query"},
        audit_delta=None, audit_plane_ok=True, needs_plane3=True,
        plane3=p3_no_dispatch, expect_class=FAIL_CARRIER, forbid_class=FAIL_MODEL)

    # --- 载体类正控 4：工具名查无 ★ 无运行时实测样本，构造自检 ---
    add("载体类正控-工具名查无(仅源码级,构造)", FAIL,
        parsed=parse_sse(_sse([{"event": "error",
                                "message": "ValueError: Tool with name neo4j_query not found"}])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_CARRIER, forbid_class=FAIL_MODEL)

    # --- 模型类正控 1：无工具事件 + 非空散文答案（★ 构造样本）---
    # 说明：W21 三次真实模型类 FAIL 都是**伪称调用**形态；「无 claim 的纯散文」
    # 未找到实测样本 ⇒ 本用例是构造样本，报告里如实标注。
    prose = ("我需要先查询知识图谱的 schema，了解与「交易」相关的节点标签与关系类型，"
             "然后才能进一步查询具体的交易对象。")
    add("模型类正控-无工具事件(构造)", FAIL,
        parsed=parse_sse(_sse([
            {"event": "agent_message", "answer": prose},
            {"event": "message_end", "answer": prose,
             "metadata": {"model": "deepseek_v4",
                          "usage": {"prompt_tokens": 1823, "completion_tokens": 204}}},
        ])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_MODEL,
        expect_subreason="no_tool_event", forbid_class=FAIL_CARRIER)

    # --- 模型类正控 2：伪称调用（W21 实测逐字，粗体包裹）---
    fake_bold = ("我调用了 **neo4j_query** 工具，执行了以下查询：\n\n"
                 "MATCH (inst:Instance {name: 'cc-pay'})-[:RUNS_ON]->(c:Container)"
                 "-[:HOSTED_BY]->(h:Host) RETURN inst, c, h")
    add("模型类正控-伪称调用(粗体,W21实测)", FAIL,
        parsed=parse_sse(_sse([
            {"event": "agent_message", "answer": fake_bold},
            {"event": "message_end", "answer": fake_bold,
             "metadata": {"model": "deepseek_v4",
                          "usage": {"prompt_tokens": 1823, "completion_tokens": 204}}},
        ])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_MODEL,
        expect_subreason="claimed_tool_without_event", forbid_class=FAIL_CARRIER)

    # --- 模型类正控 3：伪称调用（W21 实测逐字，反引号包裹）---
    fake_tick = "我调用了 `neo4j_query` 工具，查询语句如下：\n\nMATCH (n) RETURN n"
    add("模型类正控-伪称调用(反引号,W21实测)", FAIL,
        parsed=parse_sse(_sse([
            {"event": "agent_message", "answer": fake_tick},
            {"event": "message_end", "answer": fake_tick,
             "metadata": {"model": "deepseek_v4",
                          "usage": {"prompt_tokens": 1823, "completion_tokens": 177}}},
        ])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_MODEL,
        expect_subreason="claimed_tool_without_event", forbid_class=FAIL_CARRIER)

    # --- 模型类正控 4：有 tool 但 observation 空（n_gated 子判据）---
    add("模型类正控-调用未返回结果(n_gated)", FAIL,
        parsed=parse_sse(_sse([
            {"event": "agent_thought", "tool": "neo4j_query", "tool_input": "{}",
             "observation": "", "thought": "决策", "position": 1},
            {"event": "message_end", "answer": "已经调用完成",
             "metadata": {"model": "deepseek_v4",
                          "usage": {"prompt_tokens": 1823, "completion_tokens": 85}}},
        ])),
        http_status=200, bound_tools={"neo4j_query"}, audit_delta=None,
        audit_plane_ok=True, expect_class=FAIL_MODEL,
        expect_subreason="n_gated", forbid_class=FAIL_CARRIER)

    # --- 负控 A：证据不足 ⇒ FAIL-UNCLASSIFIED（且不得被算成 PASS）---
    add("负控-证据不足(空SSE+绑定回读失败)", FAIL,
        parsed=parse_sse(""), http_status=200, bound_tools=set(),
        audit_delta=1, audit_plane_ok=True,
        bound_tools_error="app detail HTTP 502",
        expect_class=FAIL_UNCLASSIFIED, forbid_class=FAIL_CARRIER,
        forbid_verdict=PASS)

    # --- 负控 B：只有审计链新增 0 行、而 SSE 工具面完整 ⇒ 不得归入任一类 ---
    add("负控-仅审计链无新增(工具面完整)", FAIL,
        parsed=parse_sse(_ok_sse()), http_status=200, bound_tools={"cc_query"},
        audit_delta=0, audit_plane_ok=True,
        expect_class=FAIL_UNCLASSIFIED, forbid_class=(FAIL_CARRIER, FAIL_MODEL))

    # --- 负控 C：BLOCKED（证据面取不到）不得附任何 FAIL 类别 ---
    add("负控-BLOCKED不附FAIL类别", BLOCKED,
        parsed=parse_sse(_ok_sse()), http_status=200, bound_tools={"cc_query"},
        audit_delta=None, audit_plane_ok=False, expect_no_class=True)

    # --- 优先级：FAIL 与 BLOCKED 同时存在 ⇒ BLOCKED ---
    cases.append(("__priority__", BLOCKED, {}))

    # ======================================================================
    # W37 阶段 1 新增：「类别不可判定」标注的正控/负控（**只增不减**）
    # ①②③④ 逐条对应派工要求的四条夹具；夹具读数一律抄自 W35 已归档真机快照
    # （tmp/m4_w35_evidence/w35_cases_manifest.json 的 readings，非本次新造）。
    # ======================================================================

    # W7 runB / runMain 的**共同回读状态**：绑定集非空、回读无错、
    # 平面3 = 「窗口内无派发行」（真机两次 FAIL 都是这一形态）。
    w7_read_state = dict(http_status=200, bound_tools={"neo4j_query"},
                         audit_delta=None, audit_plane_ok=True,
                         needs_plane3=True, plane3=p3_no_dispatch)

    claim_only_sse = _sse([
        {"event": "agent_message", "answer": fake_bold},
        {"event": "message_end", "answer": fake_bold,
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 1823, "completion_tokens": 204}}},
    ])

    # --- ① 同回读状态 + 模型自称缺失工具（W7 runB 实测逐字）⇒ 载体类，但载体侧证据为空 ---
    add("歧义①-载体类仅凭模型自述(W7 runB)", FAIL,
        parsed=parse_sse(w7_free_sse), **w7_read_state,
        expect_class=FAIL_CARRIER, expect_ambiguous=True, expect_alt=FAIL_MODEL,
        expect_basis="model_selfreport", expect_evidence_empty=True)

    # --- ② 同回读状态 + 模型自称调用成功（W21 伪称原文）⇒ 模型类，且无载体侧事实 ---
    add("歧义②-伪称调用无载体侧事实(runMain 同态)", FAIL,
        parsed=parse_sse(claim_only_sse), **w7_read_state,
        expect_class=FAIL_MODEL, expect_ambiguous=True, expect_alt=FAIL_CARRIER,
        expect_basis="model_selfreport", expect_evidence_empty=True)

    # --- ③ 绑定集回读失败 + 载体侧实测错误串 ⇒ 载体类且**不**标不可判定 ---
    add("定向③-回读失败+载体实测串⇒不歧义", FAIL,
        parsed=parse_sse(notool_sse), http_status=200, bound_tools=set(),
        audit_delta=None, audit_plane_ok=True, needs_plane3=True,
        plane3=p3_no_dispatch, bound_tools_error="app detail HTTP 502",
        expect_class=FAIL_CARRIER, expect_ambiguous=False,
        expect_evidence_has=("carrier_marker", "bound_tools_read_error"))

    # --- ④ 平面3 **带错误**失败（≠「窗口内无派发行」）⇒ 不标不可判定 ---
    p3_err = {"ok": False, "blocked": False, "lines": [], "context": [],
              "why": "Neo4j 只读直连失败: ConnectionRefusedError: "
                     "[WinError 10061] 目标计算机积极拒绝"}
    add("定向④-平面3带错误失败⇒不歧义", FAIL,
        parsed=parse_sse(claim_only_sse), http_status=200,
        bound_tools={"neo4j_query"}, audit_delta=None, audit_plane_ok=True,
        needs_plane3=True, plane3=p3_err,
        expect_class=FAIL_MODEL, expect_ambiguous=False,
        expect_evidence_has="plane3_error")

    # --- ⑤ fail-closed：回读失败 + 模型从未调用（无伪称）⇒ 标不可判定（不默认模型类）---
    prose_sse = _sse([
        {"event": "agent_message", "answer": prose},
        {"event": "message_end", "answer": prose,
         "metadata": {"model": "deepseek_v4",
                      "usage": {"prompt_tokens": 1823, "completion_tokens": 204}}},
    ])
    add("fail-closed⑤-回读失败+从未调用⇒不可判定", FAIL,
        parsed=parse_sse(prose_sse), http_status=200, bound_tools=set(),
        audit_delta=None, audit_plane_ok=True,
        bound_tools_error="app detail HTTP 502",
        expect_class=FAIL_MODEL, expect_ambiguous=True, expect_alt=FAIL_CARRIER,
        expect_basis="carrier_side_unreadable")

    # --- ⑥ 回读成功且绑定集为空（真·载体故障）⇒ 载体类且**不**标不可判定（防「一律标歧义」）---
    add("定向⑥-回读成功但绑定集为空⇒不歧义", FAIL,
        parsed=parse_sse(_sse([
            {"event": "agent_message", "answer": "没有可用工具"},
            {"event": "message_end", "answer": "没有可用工具",
             "metadata": {"model": "deepseek_v4",
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}},
        ])),
        http_status=200, bound_tools=set(), audit_delta=None, audit_plane_ok=True,
        expect_class=FAIL_CARRIER, expect_ambiguous=False,
        expect_evidence_has="bound_tools_empty")

    # --- ⑦ 证据不足（UNCLASSIFIED）⇒ 标不可判定，且两类都排除不掉 ---
    add("歧义⑦-证据不足⇒不可判定(两类都排除不掉)", FAIL,
        parsed=parse_sse(""), http_status=200, bound_tools=set(),
        audit_delta=1, audit_plane_ok=True,
        bound_tools_error="app detail HTTP 502",
        expect_class=FAIL_UNCLASSIFIED, expect_ambiguous=True,
        expect_alt=(FAIL_CARRIER, FAIL_MODEL),
        expect_basis="insufficient_evidence")

    print("=" * 78)
    print("W7 e2e_smoke 离线自检（--selftest，不发任何网络请求）")
    print("=" * 78)
    n_pass = 0
    n_class_checks = 0
    n_ambig_checks = 0
    for name, expected, kw in cases:
        if name == "__priority__":
            verdict, code = overall([FAIL, BLOCKED, PASS])
            got = verdict
            extra = "exit=%d" % code
            cls = None
            print("  [%s] %-38s expect=%-7s got=%-7s  %s"
                  % ("OK " if got == expected else "BAD", name, expected, got, extra))
            if got == expected:
                n_pass += 1
            continue

        # W31：分类断言键必须在喂给 judge_call 之前弹出（judge_call 不认识它们）
        exp_cls = kw.pop("expect_class", None)
        forbid_cls = kw.pop("forbid_class", None)
        expect_no_class = kw.pop("expect_no_class", False)
        exp_sub = kw.pop("expect_subreason", None)
        forbid_v = kw.pop("forbid_verdict", None)
        # W37：「类别不可判定」标注的断言键（同样在处理前弹出）
        exp_amb = kw.pop("expect_ambiguous", None)
        exp_alt = kw.pop("expect_alt", None)
        exp_basis = kw.pop("expect_basis", None)
        exp_ev_empty = kw.pop("expect_evidence_empty", None)
        exp_ev_has = kw.pop("expect_evidence_has", None)

        res = judge_call(name, kw.pop("bound_tools"), kw.pop("parsed"),
                         kw.pop("http_status"), kw.pop("audit_delta"),
                         kw.pop("audit_plane_ok"), kw.pop("plane3", None),
                         kw.pop("needs_plane3", False),
                         bound_tools_error=kw.pop("bound_tools_error", None))
        got = res["verdict"]
        cls = res.get("fail_class")
        subs = res.get("fail_subreasons") or []
        amb = res.get("fail_class_ambiguous")
        alts = res.get("ambiguous_alternatives") or []
        amb_reason = res.get("ambiguity_reason") or ""
        cse = res.get("carrier_side_evidence") or []
        extra = "; ".join(res["failed"] + res["blocked"])[:110]

        checks = [got == expected]
        if exp_cls is not None:
            n_class_checks += 1
            checks.append(cls == exp_cls)
        if forbid_cls is not None:
            bad = (forbid_cls,) if isinstance(forbid_cls, str) else tuple(forbid_cls)
            n_class_checks += 1
            checks.append(cls not in bad)
        if expect_no_class:
            n_class_checks += 1
            checks.append(not cls)
        if exp_sub is not None:
            n_class_checks += 1
            checks.append(exp_sub in subs)
        if forbid_v is not None:
            n_class_checks += 1
            checks.append(got != forbid_v)
        # ---- W37：「类别不可判定」标注断言（只增不减）----
        if exp_amb is not None:
            n_ambig_checks += 1
            checks.append(bool(amb) == bool(exp_amb))
        if exp_alt is not None:
            want_alts = (exp_alt,) if isinstance(exp_alt, str) else tuple(exp_alt)
            n_ambig_checks += 1
            checks.append(set(want_alts) <= set(alts))
        if exp_basis is not None:
            n_ambig_checks += 1
            checks.append(exp_basis in amb_reason)
        if exp_ev_empty is not None:
            n_ambig_checks += 1
            checks.append((len(cse) == 0) == bool(exp_ev_empty))
        if exp_ev_has is not None:
            wants = (exp_ev_has,) if isinstance(exp_ev_has, str) else tuple(exp_ev_has)
            n_ambig_checks += 1
            checks.append(all(any(w in e for e in cse) for w in wants))

        ok = all(checks)
        mark = "OK " if ok else "BAD"
        if ok:
            n_pass += 1
        print("  [%s] %-38s expect=%-7s got=%-7s cls=%-17s amb=%-2s %s"
              % (mark, name, expected, got, cls or "-",
                 (("是" if amb else "否") if cls else "-"), extra))
    total = len(cases)
    concl = PASS if n_pass == total else FAIL
    print("-" * 78)
    print("selftest: %d/%d 项通过；结论=%s；退出码=%d" % (n_pass, total, concl, EXIT[concl]))
    print("（BRANCH-BLOCKED-EXIT2 分支已覆盖：优先级 BLOCKED(2) > FAIL(1) > PASS(0)）")
    print("（W31 两类 FAIL 分类：本自检含 %d 项分类断言；FAIL-CARRIER / FAIL-MODEL / "
          "FAIL-UNCLASSIFIED 三态均已覆盖；分类不改任何判据/阈值/退出码/优先级）"
          % n_class_checks)
    print("（FAIL-UNCLASSIFIED 单列计数，**不并入**任何成功率的分子/分母；BLOCKED ≠ FAIL）")
    print("（W37「类别不可判定」标注：本自检含 %d 项 ambiguity 断言；载体侧证据为空且只凭"
          "模型自述 ⇒ 标不可判定（备选模型类）；模型伪称调用且无载体侧事实 ⇒ 标不可判定"
          "（备选载体类）；载体侧读不到 ⇒ fail-closed 标不可判定。**标注不改** "
          "fail_class / verdict / failed / blocked / 退出码）" % n_ambig_checks)
    return EXIT[concl]


# --------------------------------------------------------------------------
# 网络侧：Console 客户端 / SSE 调用 / 审计链 / SSH
# --------------------------------------------------------------------------


class Console:
    """Dify Console 只读客户端（除 login 与 api-key 建删外全 GET）。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cj = CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))

    def _cookie(self, name):
        for c in self.cj:
            if c.name == name:
                return c.value
        return None

    def call(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        csrf = self._cookie("csrf_token")
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        tok = self._cookie("access_token")
        if tok:
            headers["Authorization"] = "Bearer " + tok
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with self.op.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:800]
        except Exception as exc:  # noqa: BLE001
            return 0, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    def login(self, email, password):
        b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
        st, _ = self.call("POST", "/console/api/login",
                          {"email": email, "password": b64,
                           "language": "zh-Hans", "remember_me": True})
        return st

    def bound_tools(self, uuid):
        st, d = self.call("GET", "/console/api/apps/" + uuid)
        if st != 200 or not isinstance(d, dict):
            return set(), "app detail HTTP %s" % st
        am = ((d.get("model_config") or {}).get("agent_mode")) or {}
        tools = {t.get("tool_name") for t in (am.get("tools") or [])
                 if t.get("enabled", True)}
        return {t for t in tools if t}, None

    def create_key(self, uuid):
        st, d = self.call("POST", "/console/api/apps/%s/api-keys" % uuid, {})
        if st not in (200, 201) or not isinstance(d, dict):
            return None, None, st, d
        return d.get("id"), d.get("token"), st, None

    def list_keys(self, uuid):
        st, d = self.call("GET", "/console/api/apps/%s/api-keys" % uuid)
        if st != 200:
            return st, None
        data = d.get("data") if isinstance(d, dict) else d
        return st, (data or [])

    def delete_key(self, uuid, key_id):
        return self.call("DELETE", "/console/api/apps/%s/api-keys/%s" % (uuid, key_id))


def chat_messages(base: str, token: str, query: str, timeout: int = 300,
                  user: str = "w7-smoke") -> tuple[int, str]:
    """POST /v1/chat-messages（streaming），返回 (http_status, 原始 SSE 文本)。"""
    scheme, host, port = split_base(base)
    payload = {"inputs": {}, "query": query, "response_mode": "streaming",
               "conversation_id": "", "user": user}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    if scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat-messages", body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, raw
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def health_audit_len(base: str, port: int) -> tuple[str | None, int | None]:
    """读载体 /health，回传 (auditFile, 内存 auditChain.length)。

    注意：``auditChain.length`` 是**内存环形缓冲**长度，与磁盘 JSONL 行数不等，
    只作降级判据（--no-ssh 时用），报告中须标注为弱平面。
    """
    url = "%s:%d/health" % (base.rstrip("/"), port)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None, None
    af = d.get("auditFile")
    chain = d.get("auditChain") or {}
    n = chain.get("length")
    if n is None and isinstance(chain, list):
        n = len(chain)
    return af, n


class SSH:
    """主机只读 SSH（paramiko）。不可用时所有方法返回 (False, 原因)。"""
    def __init__(self, host=HOST, user=SSH_USER, env=None):
        self.host, self.user = host, user
        self.env = env or {}
        self.cli = None
        self.why = "paramiko 未安装"

    def connect(self):
        try:
            import paramiko  # noqa: PLC0415
        except ImportError as exc:  # noqa: BLE001
            self.why = "paramiko 不可用: %s" % exc
            return False
        keyfiles = [os.path.expanduser("~/.ssh/id_rsa"),
                    os.path.expanduser("~/.ssh/id_ed25519")]
        for kf in keyfiles:
            if not os.path.exists(kf):
                continue
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(self.host, port=22, username=self.user,
                          key_filename=kf, timeout=15)
                self.cli = c
                return True
            except Exception:  # noqa: BLE001
                continue
        pwd = self.env.get("GAUSSDB_SSH_PASSWORD")
        if pwd:
            try:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                c.connect(self.host, port=22, username=self.user,
                          password=pwd, timeout=15)
                self.cli = c
                return True
            except Exception as exc:  # noqa: BLE001
                self.why = "SSH 口令认证失败: %s" % type(exc).__name__
                return False
        self.why = "无可用密钥且无 GAUSSDB_SSH_PASSWORD"
        return False

    def run(self, cmd, timeout=60):
        if self.cli is None:
            return False, "", self.why
        try:
            _in, out, err = self.cli.exec_command(cmd, timeout=timeout)
            o = out.read().decode("utf-8", "replace")
            e = err.read().decode("utf-8", "replace")
            rc = out.channel.recv_exit_status()
            if rc != 0:
                return False, o, (e or "rc=%d" % rc)[:300]
            return True, o, ""
        except Exception as exc:  # noqa: BLE001
            return False, "", "%s: %s" % (type(exc).__name__, str(exc)[:200])

    def line_count(self, path):
        ok, out, err = self.run("wc -l < %s" % path)
        if not ok:
            return None
        try:
            return int(out.strip().split()[0])
        except (ValueError, IndexError):
            return None

    def audit_tail(self, path, n=3):
        ok, out, _ = self.run("tail -n %d %s" % (n, path))
        if not ok:
            return []
        rows = []
        for ln in out.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except ValueError:
                rows.append({"__raw__": ln[:200]})
        return rows

    def plugin_window(self, t0: str, t1: str):
        """第三平面 (a)：plugin_daemon 日志中该窗口的**工具派发**记录。

        先单独确认容器存在（`docker inspect`），再分别取「派发行」与「解析/LLM 上下文行」，
        分类交给纯函数 :func:`plane3_from_logs`，以便离线自检覆盖。
        """
        ok_s, _st, err_s = self.run(
            "docker inspect -f '{{.State.Status}}' dify-plugin_daemon-1")
        base = ("docker logs dify-plugin_daemon-1 --since %s --until %s 2>&1" % (t0, t1))
        _, out_d, _ = self.run(
            "%s | grep -F '%s' | tail -20" % (base, PLANE3_DISPATCH_MARK))
        _, out_c, _ = self.run(
            "%s | grep -E 'management/tool|dispatch/llm/invoke' | tail -10" % base)
        logs_readable = bool(ok_s) and "no such" not in (out_c + out_d).lower()
        res = plane3_from_logs(out_d.splitlines(), out_c.splitlines(),
                               logs_readable=logs_readable)
        if not logs_readable:
            res["why"] = "plugin_daemon 容器日志不可读: %s" % (err_s or "docker inspect 失败")
        return res


# --------------------------------------------------------------------------
# 真机执行
# --------------------------------------------------------------------------


def plan(apps, query, calls_per_app):
    rows = []
    for a in apps:
        cfg = APPS[a]
        rows.append({
            "app": a, "uuid": cfg["uuid"], "calls": calls_per_app,
            "carrier_port": cfg["port"], "audit_file": cfg["audit_file"],
            "plane3": bool(cfg.get("needs_plane3")), "query": query,
        })
    return rows


def do_dry_run(apps, query, calls_per_app, base) -> int:
    print("=" * 78)
    print("W7 e2e_smoke --dry-run：以下为将执行的调用清单（本次不发任何请求）")
    print("=" * 78)
    for r in plan(apps, query, calls_per_app):
        print("App  %s" % r["app"])
        print("  uuid         : %s" % r["uuid"])
        print("  calls        : %d 次 POST %s/v1/chat-messages"
              % (r["calls"], base.rstrip("/")))
        print("  response_mode: streaming, timeout=300s")
        print("  平面1        : SSE agent_thought.tool ∈ 该 App 真机回读的绑定工具集")
        if r["audit_file"]:
            print("  平面2        : %s 行数增量 ≥ 1（载体 :%s）"
                  % (r["audit_file"], r["carrier_port"]))
        else:
            print("  平面2        : 不适用（插件直连 Neo4j，绕过载体）")
        if r["plane3"]:
            print("  平面3        : plugin_daemon dispatch/tool/invoke 日志 "
                  "→ Neo4j 侧读数 → 只读直查对齐（三选一，全灭 ⇒ BLOCKED）")
        print("  凭据         : Console 现建临时 api-key，用完立即 DELETE + 回读确认")
        print("  query        : %s" % clip(r["query"], 120))
        print("-" * 78)
    total = len(apps) * calls_per_app
    print("合计将发起 %d 次 /v1/chat-messages 调用。" % total)
    print("dry-run 退出码=0（dry-run 不判定业务结果）")
    return 0


def run_real(args) -> int:
    env, env_path = load_env()
    base = env.get("DIFY_BASE_URL")
    email = env.get("DIFY_EMAIL")
    pwd = env.get("DIFY_PASSWORD")
    neo4j_pwd = os.environ.get("CC_NEO4J_PASSWORD") or env.get("CC_NEO4J_PASSWORD")

    if not (base and email and pwd):
        print("★ BLOCKED（≠ FAIL）: 缺 Dify 凭据"
              "（需要 DIFY_BASE_URL / DIFY_EMAIL / DIFY_PASSWORD）")
        print("  .env 候选: %s" % ", ".join(ENV_CANDIDATES))
        print("  实际装载: %s" % (env_path or "<none>"))
        print("  fail-closed ⇒ 退出码 2")
        return EXIT[BLOCKED]
    print("凭据装载自 %s；DIFY_BASE_URL=%s；password %s"
          % (env_path, base, fp(pwd)))
    if neo4j_pwd:
        print("CC_NEO4J_PASSWORD 已提供（%s）" % fp(neo4j_pwd))
    else:
        print("CC_NEO4J_PASSWORD 未提供 ⇒ 平面3 的 (b)(c) 两条路不可用，"
              "仅剩 (a) plugin_daemon 日志")

    os.makedirs(args.evidence_dir, exist_ok=True)
    ssh = SSH(env=env)
    ssh_ok = False if args.no_ssh else ssh.connect()
    if not ssh_ok:
        print("! SSH 不可用（%s）⇒ 平面2 降级为 /health 内存计数（弱平面，报告中标注）"
              % ssh.why)

    c = Console(base)
    st = c.login(email, pwd)
    if st != 200:
        print("★ BLOCKED（≠ FAIL）: Console login HTTP %s" % st)
        return EXIT[BLOCKED]
    print("Console login ok")

    results = []
    for app in args.apps:
        cfg = APPS[app]
        uuid = cfg["uuid"]
        rec = {"app": app, "uuid": uuid, "calls": [], "ts": now_utc()}
        results.append(rec)

        bound, err = c.bound_tools(uuid)
        rec["bound_tools"] = sorted(bound)
        rec["bound_tools_error"] = err
        print("\n" + "=" * 78)
        print("APP %s" % app)
        print("  真机回读绑定工具: %s" % (sorted(bound) or "（空）%s" % err))

        key_id, token, kst, kerr = c.create_key(uuid)
        rec["api_key"] = {"id": key_id, "create_http": kst,
                          "token_fp": fp(token) if token else None}
        if not token:
            print("  ★ BLOCKED: 建临时 api-key 失败 HTTP %s %s" % (kst, clip(kerr, 200)))
            rec["verdict"] = BLOCKED
            rec["blocked"] = ["建临时 api-key 失败 HTTP %s" % kst]
            continue
        print("  临时 api-key 已建: id=%s %s" % (key_id, fp(token)))

        try:
            for i in range(args.calls_per_app):
                call = {"i": i + 1, "query": args.query}
                # --- 审计链 before ---
                if cfg["audit_file"]:
                    if ssh_ok:
                        b = ssh.line_count(cfg["audit_file"])
                        call["audit_before"] = b
                        call["audit_plane"] = "ssh_wc_l"
                    else:
                        af, b = health_audit_len(base, cfg["port"])
                        call["audit_before"] = b
                        call["audit_file_from_health"] = af
                        call["audit_plane"] = "health_memory_len(weak)"
                t0 = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                call["window_start_utc"] = t0

                # --- 真机调用 ---
                t_start = time.time()
                try:
                    status, raw = chat_messages(base, token, args.query, args.timeout)
                except Exception as exc:  # noqa: BLE001
                    status, raw = 0, "%s: %s" % (type(exc).__name__, exc)
                call["elapsed_s"] = round(time.time() - t_start, 3)
                call["http_status"] = status
                raw_path = os.path.join(args.evidence_dir,
                                        "%s.call%d.sse.txt" % (app, i + 1))
                with io.open(raw_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(raw)
                call["sse_file"] = os.path.basename(raw_path)
                call["sse_bytes"] = os.path.getsize(raw_path)

                parsed = parse_sse(raw) if status == 200 else parse_sse("")
                call["parsed"] = {
                    "n_events": parsed["n_events"],
                    "event_types": parsed["event_types"],
                    "answer_len": parsed["answer_len"],
                    "answer_head": clip(parsed["answer"], 300),
                    "model": parsed["model"],
                    "usage": parsed["usage"],
                    "error": parsed["error"],
                    "conversation_id": parsed["conversation_id"],
                    "message_id": parsed["message_id"],
                    "tool_names": parsed["tool_names"],
                    "executed_tool_names": parsed["executed_tool_names"],
                    "thoughts": [
                        {"tool": t["tool"], "tool_input": clip(t["tool_input"], 400),
                         "observation_len": t["observation_len"],
                         "observation_head": clip(t["observation"], 400)}
                        for t in parsed["thoughts"]
                    ],
                }
                t1 = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                call["window_end_utc"] = t1

                # --- 审计链 after ---
                if cfg["audit_file"]:
                    if ssh_ok:
                        a = ssh.line_count(cfg["audit_file"])
                        call["audit_after"] = a
                        call["audit_tail"] = [
                            {k: v for k, v in row.items()
                             if k in ("requestId", "tool", "status", "elapsedMs",
                                      "elapsed_s", "rowCount", "startedAt")}
                            for row in ssh.audit_tail(cfg["audit_file"], 3)]
                    else:
                        af, a = health_audit_len(base, cfg["port"])
                        call["audit_after"] = a
                    if call.get("audit_before") is None or a is None:
                        call["audit_delta"] = None
                        call["audit_plane_ok"] = False
                    else:
                        call["audit_delta"] = a - call["audit_before"]
                        call["audit_plane_ok"] = True
                else:
                    call["audit_delta"] = None
                    call["audit_plane_ok"] = True

                # --- 平面 3 ---
                p3 = None
                if cfg.get("needs_plane3"):
                    if ssh_ok:
                        p3 = ssh.plugin_window(t0, t1)
                    else:
                        p3 = {"ok": False, "blocked": True,
                              "why": "SSH 不可用，plugin_daemon 日志取不到；"
                                     "Neo4j 只读口令亦未提供 ⇒ 三路全灭"}
                    call["plane3"] = p3

                res = judge_call(app, bound, parsed, status, call["audit_delta"],
                                 call["audit_plane_ok"], plane3=p3,
                                 needs_plane3=bool(cfg.get("needs_plane3")),
                                 bound_tools_error=rec.get("bound_tools_error"))
                call["judge"] = res
                print("  call%d: HTTP %s | events=%d | answer_len=%d | model=%s | "
                      "usage=%s | tools=%s | audit_delta=%s ⇒ %s%s"
                      % (i + 1, status, parsed["n_events"], parsed["answer_len"],
                         parsed["model"], parsed["usage"],
                         parsed["executed_tool_names"], call["audit_delta"],
                         res["verdict"],
                         (" / %s" % res["fail_class"]) if res.get("fail_class") else ""))
                for r in res["reasons"]:
                    print("      ok   %s" % r)
                for r in res["failed"]:
                    print("      FAIL %s" % r)
                for r in res["blocked"]:
                    print("      BLK  %s" % r)
                if res.get("fail_class"):
                    print("      CLASS %s（sub_reasons=%s）"
                          % (res["fail_class"], res.get("fail_subreasons") or []))
                    for ev in res.get("fail_class_evidence") or []:
                        print("        ↳ %s" % clip(ev, 220))
                    # W37：类别不可判定标注 + 载体侧证据（只加不改）
                    if res.get("fail_class_ambiguous"):
                        print("      AMBIG 类别不可判定（备选 %s）: %s"
                              % ("/".join(res.get("ambiguous_alternatives") or []) or "-",
                                 clip(res.get("ambiguity_reason") or "", 400)))
                    else:
                        print("      载体侧证据 %d 条；类别不可判定=否（载体侧读数已读到=%s）"
                              % (len(res.get("carrier_side_evidence") or []),
                                 res.get("carrier_side_readable")))
                    for ev in res.get("carrier_side_evidence") or []:
                        print("        · 载体侧证据 %s" % clip(ev, 220))
                if parsed["error"]:
                    print("      err  %s" % clip(parsed["error"], 200))
                for t in parsed["thoughts"][:4]:
                    if t["tool"]:
                        print("      tool=%s input=%s obs_chars=%d"
                              % (t["tool"], clip(t["tool_input"], 150),
                                 t["observation_len"]))
                rec["calls"].append(call)
        finally:
            # --- 删 key 并回读确认 ---
            dst, dbody = c.delete_key(uuid, key_id)
            lst, keys = c.list_keys(uuid)
            remaining = [k.get("id") for k in (keys or [])]
            gone = key_id not in remaining
            rec["api_key"]["delete_http"] = dst
            rec["api_key"]["relist_http"] = lst
            rec["api_key"]["n_keys_after"] = len(remaining)
            rec["api_key"]["removed_confirmed"] = gone
            print("  临时 api-key 删除: DELETE HTTP %s；回读 keys=%d；已消失=%s"
                  % (dst, len(remaining), gone))

        verdicts = [cl["judge"]["verdict"] for cl in rec["calls"] if "judge" in cl]
        if not verdicts:
            rec["verdict"] = BLOCKED
        else:
            rec["verdict"] = overall(verdicts)[0]
        # W31：App 级分类标签（仅聚合，不参与任何判定）
        rec["fail_class"] = _rec_fail_class(rec)
        # W37：App 级「类别不可判定」标记（同样只聚合，不参与任何判定）
        rec["fail_class_ambiguous"] = _rec_ambiguous(rec)

    json_path = os.path.join(args.evidence_dir, "smoke_results.json")
    with io.open(json_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"run_utc": now_utc(), "base": base,
                             "env_source": env_path, "results": results},
                            ensure_ascii=False, indent=1, sort_keys=True))
        fh.write("\n")

    for rec in results:
        rec.setdefault("verdict", BLOCKED)
    concl, code = overall([r.get("verdict", BLOCKED) for r in results])

    print("\n" + "=" * 78)
    print("W7 e2e_smoke 真机结论")
    print("=" * 78)
    for rec in results:
        print("  %-20s %-8s %-17s %s" % (rec["app"], rec.get("verdict"),
                                         (_rec_fail_class(rec) or "-")
                                         + ("[类别不可判定]"
                                            if rec.get("fail_class_ambiguous") else ""),
                                         clip("; ".join(
                                             rec.get("blocked") or
                                             [f for cl in rec["calls"]
                                              for f in cl.get("judge", {}).get("failed", [])]) or "ok",
                                             140)))

    # ---- W31：三类 FAIL 分列计数（UNCLASSIFIED 单列，**不并入**成功率分子/分母）----
    tally: dict[str, int] = {}
    n_fail = n_blocked = n_pass = 0
    for rec in results:
        v = rec.get("verdict", BLOCKED)
        if v == BLOCKED:
            n_blocked += 1
        elif v == FAIL:
            n_fail += 1
            c = _rec_fail_class(rec) or FAIL_UNCLASSIFIED
            tally[c] = tally.get(c, 0) + 1
        else:
            n_pass += 1
    extra_keys = tuple(sorted(k for k in tally
                              if k not in (FAIL_CARRIER, FAIL_MODEL, FAIL_UNCLASSIFIED)))
    print("  判定分布: PASS=%d FAIL=%d BLOCKED=%d（共 %d 个 App）"
          % (n_pass, n_fail, n_blocked, len(results)))
    print("  FAIL 分列（按 App 记一次，以该 App 失败调用的分类为准）: "
          + " ".join("%s=%d" % (k, tally.get(k, 0))
                     for k in (FAIL_CARRIER, FAIL_MODEL, FAIL_UNCLASSIFIED) + extra_keys))
    print("  ★ FAIL-UNCLASSIFIED 单列计数，**不并入**任何成功率的分子/分母；"
          "BLOCKED ≠ FAIL（证据取不到不等于被测对象是坏的）。")
    # ---- W37：类别不可判定计数（只加不改；不改任何类别与退出码）----
    n_ambig_calls = sum(1 for rec in results for cl in rec.get("calls") or []
                        if (cl.get("judge") or {}).get("fail_class_ambiguous"))
    n_ambig_apps = sum(1 for rec in results if rec.get("fail_class_ambiguous"))
    print("  ★ W37 类别不可判定: %d 次失败调用被标「不可判定」（涉及 %d 个 App）；"
          "标注**不改**任何类别/判定/退出码，只说明「本条类别不足以定向」。"
          % (n_ambig_calls, n_ambig_apps))
    if n_ambig_calls:
        print("    ↳ 要消除不可判定需引入**新输入面**（DB tool_meta_str / plugin_daemon 日志），"
              "属判据与范围变更，评估见 tmp\\_m4_w37_report.md 末段。")
    print("  ★ 本冒烟只回答「模型是否真的调到工具并拿到非空答案」，**不证诊断准确率**。")
    print("结果快照: %s" % json_path)
    print("总体结论=%s；退出码=%d" % (concl, code))
    if concl == BLOCKED:
        print("★ BLOCKED（≠ FAIL）：证据面没取全，不等于被测对象是坏的。")
    return code


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="W7 端到端冒烟：模型真被调用 + 工具真被执行（三态退出码）")
    ap.add_argument("--selftest", action="store_true", help="离线自检，不发任何网络请求")
    ap.add_argument("--dry-run", action="store_true", help="只打印调用清单，不发请求")
    ap.add_argument("--apps", default=",".join(APPS),
                    help="逗号分隔的 App 名（默认全部四个）")
    ap.add_argument("--query", default=DEFAULT_QUERY, help="提问原文")
    ap.add_argument("--calls-per-app", type=int, default=1, help="每 App 调用次数（默认 1）")
    ap.add_argument("--timeout", type=int, default=300, help="/v1/chat-messages 超时秒")
    ap.add_argument("--evidence-dir", default=os.path.join("tmp", "m4_w7_evidence", "smoke"))
    ap.add_argument("--no-ssh", action="store_true",
                    help="不用 SSH 读审计链文件行数（降级为 /health 内存计数，弱平面）")
    args = ap.parse_args()

    names = [a.strip() for a in args.apps.split(",") if a.strip()]
    unknown = [a for a in names if a not in APPS]
    if unknown:
        print("未知 App: %s；可选: %s" % (unknown, list(APPS)))
        return EXIT[BLOCKED]
    args.apps = names

    if args.selftest:
        return run_selftest()
    env, _ = load_env()
    base = env.get("DIFY_BASE_URL") or "http://127.0.0.1"   # ★ R-58：不硬编码部署主机（原值为 192.0.2.11，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
    if args.dry_run:
        return do_dry_run(args.apps, args.query, args.calls_per_app, base)
    return run_real(args)


if __name__ == "__main__":
    sys.exit(main())
