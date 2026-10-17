#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4 公平性门禁 · F0–F5（`docs\\05-编排层设计（Dify）.md` §1.3 的机械执行体）

用途
----
在**任何评测开跑之前**，机械证明「两个主对比组（`cc-kg-agent` ↔ `cc-onto-agent`）
发给模型的提示面逐字相同、且载体词零泄漏」。本脚本是 M4 阶段的反作弊判据：
它只**读**真实 Dify 回读面，不写任何东西（唯一 POST = `/console/api/login` 会话登录）。

判据（与 `docs\\05` §1.3 F0–F5 原文对齐；行号会随文档批次变动，故以判据原文为准）
--------------------------------------------------------------------------------
F0  两个主对比组的模型面 tool definitions 逐字相同。
    比较面（**回读值**，非声明值）= 每个工具的
        工具名（`operation_id`，= 模型面 function name）
        工具描述（human 与 llm 两处 —— 见下方「描述来源」）
        参数列表（参数名 / 类型 / 必填 / 描述，逐字）
    ★ 唯一允许的差异 = provider 名 与 `credentials`（含 `server_url`）。
      二者都**不得**进入 F0 比较面（R-31：provider 名不进模型面）。
    ★ 描述来源（R-40 源码定盘，`core/tools/utils/parser.py:211-220` →
      `core/tools/custom_tool/provider.py:153-154`）：模型面描述 = 该 operation 的
      `description`（有则优先）否则 `summary`；工具名 = `operationId`。
      Console 回读面把工具级描述**只暴露为 `tools[].summary`**，Dify 把它同时赋给
      `description.human` 与 `description.llm` ⇒ F0 对「两处」的比较落到
      `summary`（等价于 human==llm==summary）+ 原样存回的 `openapi.description`/`summary`。
F0.f 静默失效防线（R-31，单 workspace 特有）：provider → `server_url` → 载体端口。
    `kg` 只许 `:8301`、`onto` 只许 `:8302`、`kgt` 只许 `:8303`、`live-facts` 只许 `:8304`；且两个主 App 的工具集合
    按 `provider_id` **交集为空**（否则实验静默退化成「KG vs KG」）。
F1  两个主 App 的 `pre_prompt` 逐字相同；且模型面全字符串（`pre_prompt` ∪ 已绑工具的
    `tool_name`/描述/参数名/参数描述）对禁用词表**零命中**（R-34 扩大扫描面）。
F2  两个主 App 的 `model` 块（含 `temperature`）与 `agent_mode`
    （`enabled`/`strategy`/`max_iteration`）回读后逐字相同；`strategy` 必须 `react`；
    两侧每个工具的 `tool_parameters` 均为 `{}`；**不得出现 `function_calling_type`**
    （该字段在 Dify 1.17.0 全 `/app/api` 零命中，属更早版本）。
F3  `agent_mode.enabled=true`、`strategy` 存在且两侧同值、`max_iteration` 两侧同值；
    每个已绑工具条目 `enabled=true`（`manager.py:70-84` 会丢弃 `enabled` 非真的条目
    ⇒ 否则工具静默不生效）。
F4  未使用字段**同为默认值**（`dataset_configs`/`file_upload`/`prompt_type`/
    `external_data_tools`/`agent_mode.prompt` 等）。默认值的证据来源写在
    `F4_DEFAULT_PROVENANCE` 常量里；**无法取证默认值的键一律进 `uncovered`**，
    不放过、也不假装查过。
F5  `cc-kgt-agent` **排除在 F0/F1 之外**（它必须有提示词变体），但要求：
    其 `pre_prompt` 禁用词零命中；其注册工具**恰为 binding 声明的 4 个**且**不含 `cc_query`**。
    ★ 注意 F5 的禁用词扫描**只作用于 `pre_prompt`**：KG-T 的**工具面**含「图」属预期
      （拓扑子图），R-34/F5 明确豁免 —— 把工具面也算进去会得到假 FAIL。

退出码（★ 整体优先级 BLOCKED(2) > FAIL(1) > PASS(0)）
------------------------------------------------------
0 = PASS     全部判据满足
1 = FAIL     判据不满足（数据取到了，但内容不合规）
2 = BLOCKED  前置条件缺失（凭据缺失 / 网络不可达 / provider 或 App 尚未创建 /
             配置尚未落地）。**BLOCKED 绝不等于 FAIL，也绝不写成 PASS。**

诚实边界（本脚本**不**检查什么）
--------------------------------
1. 不检查两侧服务自己的 `/openapi.json`（R-40：注册面由我方生成，两侧服务自述文档**本就不同**）。
   本脚本比较的是**已注册工具的模型面对模型面**回读值。
2. 不检查 Dify 运行期真正注入模型的 prompt 文本（框架侧 ReAct 脚手架由 Dify 注入；
   在 `model`/`mode`/`pre_prompt` 逐字相同的前提下按源码推定为逐字相同 —— 这是**源码阅读证据，非抓包证据**）。
3. 不检查模型 provider 的配额 / 限流 / 版本漂移（只比 `model` 块逐字相同）。
4. 不检查 `created_at`/`created_by`/`updated_at`/`updated_by`（元数据，显式排除）。
5. 不检查 KG-T 的**工具面**禁用词（F5 豁免，见上）。
6. ★ **R-52 起检查 R 臂**（原诚实边界为「不检查 R 臂」）：`fairness.compare_pairs` 现含
   **两个对照对**（Q 对 + R 对），本脚本对**每一个**对照对跑 App 级判据 F1–F4，
   结果标签加后缀 `@<对>`（如 `F1@cc-kg-r-agent+cc-onto-r-agent`）；并新增 **F3.e**
   「已绑工具集合 == 声明集合，且不含 `tools_removed_from_setb`」。
   ★ F0 比的是 **provider 面**（两对共用同一对 provider）⇒ 只跑一次；F5 是方案3 专属，
   与对照对无关。★ R 臂的「工具面 = Q 臂去掉 cc_query」在**契约面**由 G24-10 断言，
   在**声明/回读面**由 `deploy_agents.py --check` 断言，与本脚本的 F3.e 互为独立证据。
7. 不检查插件面（`neo4j_query`）的入参 schema 与凭据可用性（属契约门禁 M4-C7–C9）。
8. 不修改任何东西：写操作面为零（除登录）。

用法
----
  python check_fairness.py                      # 真机运行（默认 binding / 默认 .env）
  python check_fairness.py --selftest           # 离线自证（含 6 条负控 + 1 条正控），不联网
  python check_fairness.py --only F0,F1         # 只跑指定判据
  python check_fairness.py --json out.json      # 落证据
  python check_fairness.py --tee out.txt        # 控制台原文另存（UTF-8，报告引用用）
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"

HERE = Path(__file__).resolve().parent          # .../shared/deploy
REPO_ROOT = HERE.parent.parent                  # .../  (仓库根)
DEFAULT_BINDING = HERE / "dify" / "binding.yaml"
# ★ R-62（2026-10-16）：默认凭据改为**本仓库根 `.env`**（与 m5_run.py / deploy_agents.py 同源）。
#   原值 = 硬编码的另一项目路径 `<workspace>\upstream-ops-agent\.env`，该文件写的是
#   `DIFY_BASE_URL=http://192.0.2.10`（**迁址前的旧主机**，R-58 已登记现网 = 192.0.2.11）
#   ⇒ 不带 `--env` 运行时本门禁会**静默判错实例**：登录成功（旧实例同样在线）但
#   `apps=[]` ⇒ F0–F5 全部 BLOCKED（假失败），或更糟 —— 拿旧实例的旧配置判出**假 PASS**。
#   与 R-56「没配只落本机、绝不静默指向别的机器」同一纪律：候选文件缺失即 BLOCKED，不回落。
DEFAULT_ENV = REPO_ROOT / ".env"

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
RANK = {PASS: 0, FAIL: 1, BLOCKED: 2}
EXIT_FOR = {PASS: 0, FAIL: 1, BLOCKED: 2}
ALL_LABELS = ["F0", "F1", "F2", "F3", "F4", "F5"]

# ===========================================================================
# ★ 项目级不变量：载体 carrier → 唯一允许的端口（R-31 静默失效防线）
#   这些**数值**是项目常量（R-31 裁定 + `tmp\\_m4_worklist.md` §H.1 现场取证），
#   不是从 binding 猜出来的；binding 的声明必须与它一致，
#   不一致 = 「声明与项目不变量冲突」⇒ FAIL（不是 BLOCKED：这是被门禁的工件自身有缺陷）。
#   provider 名 / App 名 / 端口**一律从 binding 读**，本脚本不把它们硬编码为判据。
# ===========================================================================
# ★ U-N 裁定 = A（2026-10-08）：KG-T-dyn 的独立端口 = 8305（新 carrier kgt-dyn）。
PORT_BY_CARRIER = {"kg": 8301, "onto": 8302, "kgt": 8303, "live-facts": 8304, "kgt-dyn": 8305}

# ===========================================================================
# F4：未使用字段的默认值
#   ★ 证据来源（provenance）：Dify 1.17.0 真机 · 最小配置的既有 agent-chat App
#     `88069788-714f-46ac-bffb-0a8ef7f79f85`（`agent_mode.enabled=true` 且工具数 0、
#     无知识库绑定）的 `GET /console/api/apps/{uuid}` 回读值。
#   ★ 只断言**结构性/假值**默认（false / [] / "" / "simple" / None）——
#     这类值与 App 的创建路径（UI 或 Console API）无关，误判风险最低。
#   ★ `retriever_resource` / `suggested_questions_after_answer` 的模板默认是
#     `{"enabled": true}` 且后者内嵌 model 块 ⇒ **只做两侧相等**，默认值口径进 uncovered。
# ===========================================================================
F4_DEFAULT_PROVENANCE = (
    "Dify 1.17.0 真机回读 · 最小配置既有 agent-chat App "
    "88069788-714f-46ac-bffb-0a8ef7f79f85（agent_mode.enabled=true、工具数 0、未绑数据集）"
)
# (dot-path, 期望默认值, 说明)
F4_DEFAULT_ASSERTED = [
    ("prompt_type", "simple", "提示词模式：simple（非 jinja/advanced）"),
    ("external_data_tools", [], "无外部数据工具"),
    ("agent_mode.prompt", None, "agent_mode.prompt 为空（保留原样：None 或空串都算空）"),
    ("file_upload.enabled", False, "文件上传关闭"),
    ("sensitive_word_avoidance.enabled", False, "敏感词规避关闭"),
    ("annotation_reply.enabled", False, "标注回复关闭"),
    ("more_like_this.enabled", False, "更多类似关闭"),
    ("opening_statement", "", "开场白为空"),
    ("suggested_questions", [], "建议问题为空"),
    ("speech_to_text.enabled", False, "语音转文字关闭"),
    ("text_to_speech.enabled", False, "文字转语音关闭"),
    ("dataset_query_variable", "", "数据集查询变量为空"),
    ("dataset_configs.datasets.datasets", [], "★ 未绑定任何知识库（F4 的核心语义项）"),
]
# 只做「两侧逐字相等」，不做默认值断言（默认值口径进 uncovered）
F4_EQUALITY_ONLY = [
    "retriever_resource",
    "suggested_questions_after_answer",
    "chat_prompt_config",
    "completion_prompt_config",
    "user_input_form",
    "dataset_configs",
    "file_upload",
    "text_to_speech",
    "dataset_query_variable",
]
# F4 覆盖的 model_config 顶层键（两侧相等 + 默认值断言的对象）
F4_COVERED_KEYS = [
    "dataset_configs", "dataset_query_variable", "external_data_tools", "file_upload",
    "prompt_type", "retriever_resource", "sensitive_word_avoidance", "user_input_form",
    "annotation_reply", "more_like_this", "opening_statement", "suggested_questions",
    "suggested_questions_after_answer", "speech_to_text", "text_to_speech",
    "chat_prompt_config", "completion_prompt_config",
]
# F1–F4 合计覆盖的键（用于算「未覆盖字段」）
COVERED_MODEL_CONFIG_KEYS = sorted(set(F4_COVERED_KEYS) | {"pre_prompt", "model", "agent_mode"})
# 显式排除的元数据键（非行为配置；两侧本就允许不同）
METADATA_MODEL_CONFIG_KEYS = ["created_at", "created_by", "updated_at", "updated_by"]
# Dify 1.17.0 真机观测到的 agent-chat App model_config 顶层键模板（24 键；来源 = 既有 App
# 2c1a708e-60ba-44e1-87c8-de3ab830fb40 的回读面）。作用：即使「App 尚未创建」而拿不到
# 观测键集，也能给出**预期的**未覆盖键清单，而不是给一个空表。
MODEL_CONFIG_TEMPLATE_KEYS_OBSERVED = [
    "agent_mode", "annotation_reply", "chat_prompt_config", "completion_prompt_config",
    "created_at", "created_by", "dataset_configs", "dataset_query_variable",
    "external_data_tools", "file_upload", "model", "more_like_this", "opening_statement",
    "pre_prompt", "prompt_type", "retriever_resource", "sensitive_word_avoidance",
    "speech_to_text", "suggested_questions", "suggested_questions_after_answer",
    "text_to_speech", "updated_at", "updated_by", "user_input_form",
]

# 参数面比较字段（回读 `tools[].parameters[]` 的全部模型面 + 展示面字段）
PARAM_FACE_FIELDS = [
    "name", "type", "required", "llm_description", "human_description",
    "label", "placeholder", "form", "multiple", "options", "default",
    "min", "max", "precision", "scope", "auto_generate", "template", "input_schema",
]
# provider 级元数据比较面（★ 不含 credentials / provider 名 —— R-31 允许其不同）
PROVIDER_META_FIELDS = [
    "description", "custom_disclaimer", "icon", "labels", "privacy_policy", "schema_type",
]
LOGIN_PATH = "/console/api/login"


# ===========================================================================
# 小工具
# ===========================================================================
def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return sha256_bytes(s.encode("utf-8"))


def stable_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def count_lf(b: bytes) -> int:
    """字节级 LF 计数（`Get-Content .Count` 对中文文件会给伪值）。"""
    return b.count(b"\n")


def has_bom(b: bytes) -> bool:
    return b.startswith(b"\xef\xbb\xbf")


def file_fingerprint(p: Path) -> dict:
    b = p.read_bytes()
    return {"path": str(p), "bytes": len(b), "lf_lines": count_lf(b),
            "cr_bytes": b.count(b"\r"), "bom": has_bom(b), "sha256": sha256_bytes(b)}


_MISSING = object()


def dig(obj, dotpath, default=_MISSING):
    """按点号取嵌套键；缺失/类型不符返回 default（默认抛 _MISSING 哨兵语义见调用方）。"""
    cur = obj
    for part in dotpath.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def trunc(v, n=240):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "…(truncated)"


def diff_json(a, b, path="$"):
    """逐字段 diff，返回 [(path, left, right)]（用于 F0 的「逐字段 diff」要求）。"""
    out = []
    if type(a) is not type(b):
        return [(path, trunc(a), trunc(b))]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(("%s.%s" % (path, k), "<absent>", trunc(b[k])))
            elif k not in b:
                out.append(("%s.%s" % (path, k), trunc(a[k]), "<absent>"))
            else:
                out.extend(diff_json(a[k], b[k], "%s.%s" % (path, k)))
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(("%s.length" % path, str(len(a)), str(len(b))))
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff_json(x, y, "%s[%d]" % (path, i)))
    else:
        if a != b:
            out.append((path, trunc(a), trunc(b)))
    return out


# ===========================================================================
# 结果载体
# ===========================================================================
def sub(sid, title, verdict, detail, evidence=None):
    return {"id": sid, "title": title, "verdict": verdict, "detail": detail,
            "evidence": list(evidence or [])}


def worst(verdicts):
    v = PASS
    for x in verdicts:
        if RANK[x] > RANK[v]:
            v = x
    return v


class Reporter:
    """控制台 + `--tee` 文件（UTF-8/LF）双写。"""

    def __init__(self, tee_path=None):
        self.lines = []
        self.tee = None
        if tee_path:
            self.tee = open(tee_path, "w", encoding="utf-8", newline="\n")

    def line(self, s=""):
        s = str(s)
        self.lines.append(s)
        print(s)
        if self.tee:
            self.tee.write(s + "\n")

    def close(self):
        if self.tee:
            self.tee.flush()
            self.tee.close()
            self.tee = None


# ===========================================================================
# binding.yaml（唯一事实源；本脚本**只读**它取 provider / app / 端口 / 禁用词）
# ===========================================================================
def load_binding(path: Path) -> dict:
    import yaml  # PyYAML 6.x：本机已装（非新增依赖）
    with open(path, "r", encoding="utf-8") as fh:
        b = yaml.safe_load(fh)
    if not isinstance(b, dict):
        raise ValueError("binding.yaml 顶层不是 mapping")
    return b


def binding_provider(binding, name):
    for p in binding.get("providers") or []:
        if p.get("provider") == name:
            return p
    return None


def carrier_of_provider(binding, name):
    p = binding_provider(binding, name)
    return (p or {}).get("carrier")


def provider_base(binding, name):
    p = binding_provider(binding, name) or {}
    if p.get("base"):
        return p["base"].rstrip("/")
    c = binding.get("carriers", {}).get(p.get("carrier"), {})
    return (c.get("service_base") or "").rstrip("/")


def provider_port(binding, name):
    p = binding_provider(binding, name) or {}
    if p.get("port"):
        return int(p["port"])
    base = provider_base(binding, name)
    m = re.match(r"^[a-zA-Z]+://[^/]+:(\d+)$", base)
    return int(m.group(1)) if m else None


def port_from_base(base: str):
    m = re.match(r"^[a-zA-Z]+://[^/]+:(\d+)", base or "")
    return int(m.group(1)) if m else None


def comparison_pairs_of(binding):
    """binding.fairness.compare_pairs 的规范化读取（★ R-52：可含多个对照对）。

    返回 [ [a,b], … ]。形状非法时抛 ValueError（调用方按 BLOCKED 处理）。
    """
    pairs = (binding.get("fairness") or {}).get("compare_pairs") or []
    if not pairs:
        raise ValueError("binding.fairness.compare_pairs 未声明任何对照对")
    for i, p in enumerate(pairs):
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError("binding.fairness.compare_pairs[%d] 必须是长度 2 的列表" % i)
    return [list(p) for p in pairs]


def main_pair(binding, idx: int = 0):
    """返回第 idx 个对照对的 (appA, appB, provA, provB)；绑定面不自洽则返回 (None, 原因)。

    ★ R-52：`idx` 用于遍历**全部**对照对（Q 对照对 = 0，R 对照对 = 1）。
    """
    pairs = (binding.get("fairness") or {}).get("compare_pairs") or []
    if (not pairs or idx >= len(pairs)
            or not isinstance(pairs[idx], list) or len(pairs[idx]) != 2):
        return None, "binding.fairness.compare_pairs 未声明长度为 2 的第 %d 个对照对" % idx
    a, b = pairs[idx]
    apps = binding.get("apps") or {}
    for x in (a, b):
        if x not in apps:
            return None, "binding.apps 缺少对照对成员 %r" % x
    pa = (apps[a] or {}).get("provider")
    pb = (apps[b] or {}).get("provider")
    if not pa or not pb:
        return None, "对照对成员未声明 provider（%r / %r）" % (pa, pb)
    return (a, b, pa, pb), None


def forbid_regex(binding):
    toks = (binding.get("fairness") or {}).get("forbid_tokens") or []
    if not toks:
        raise ValueError("binding.fairness.forbid_tokens 为空 —— 门禁不能在没有禁用词表时声称扫过")
    return re.compile("|".join(re.escape(str(t)) for t in toks), re.IGNORECASE), list(toks)


def exempt_providers(binding):
    return set((binding.get("fairness") or {}).get("model_face_projection", {}).get(
        "exempt_providers") or [])


# ===========================================================================
# 只读 Console 客户端（★ 硬闸：除 login 外只允许 GET）
# ===========================================================================
class ReadOnlyViolation(RuntimeError):
    pass


class Console:
    def __init__(self, base, timeout=30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.token = None
        self.wrote_anything = False
        self.notes = []

    # ---- 硬闸：任何非 GET（除登录）一律拒绝 ----
    def _headers(self):
        h = {"Content-Type": "application/json"}
        c = {ck.name: ck.value for ck in self.cj}
        if c.get("csrf_token"):
            h["X-CSRF-TOKEN"] = c["csrf_token"]
        tok = self.token or c.get("access_token")
        if tok:
            h["Authorization"] = "Bearer " + tok
        return h

    def call(self, method, path, payload=None):
        if method != "GET" and path != LOGIN_PATH:
            raise ReadOnlyViolation(
                "门禁脚本只读：拒绝 %s %s（唯一允许的 POST 是 %s 会话登录）"
                % (method, path, LOGIN_PATH))
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self._headers())
        try:
            with self.op.open(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"_raw": body[:300]}
        except Exception as e:  # noqa: BLE001
            return 0, {"_err": "%s: %s" % (type(e).__name__, str(e)[:200])}

    def get(self, path):
        return self.call("GET", path)

    def login(self, email, password):
        st, d = self.call("POST", LOGIN_PATH, {
            "email": email,
            "password": base64.b64encode(password.encode()).decode(),
            "language": "zh-Hans", "remember_me": True})
        if st == 200:
            self.token = {ck.name: ck.value for ck in self.cj}.get("access_token")
            return True, st
        return False, st


def load_env_file(path: Path) -> dict:
    env = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ===========================================================================
# 采集：live 面（只 GET）
# ===========================================================================
def resolve_app_rows(binding, items):
    """声明键 → 真机 App 行。**uuid 优先、名字兜底**（W20）。

    返回 `(rows, how)`：`rows = {声明键: 真机行}`，`how = {声明键: "uuid"|"name"|"missing"}`。

    ★ 为什么必须 uuid 优先：真机 App 的 `name` 已按 binding 声明改为中文 display_name
      （见 shared/deploy/dify/app_meta.py），旧的「按名字取真机」在改名后会
      `by_name.get(声明键) → None → continue`，即**静默少读 App**（读到 0 个却不报错）。
      uuid 是绑定不变量，按 uuid 取行与 App 是否改名无关。
    ★ 名字兜底只为兼容「尚未登记 uuid」的历史声明，且命中后由 F0.n 的
      「真机 name == 声明 display_name」断言兜住（fail-closed），不会掩盖漂移。
    """
    rows, how = {}, {}
    by_id, by_name = {}, {}
    for a in items or []:
        if not isinstance(a, dict):
            continue
        if a.get("id"):
            by_id.setdefault(a["id"], a)
        if a.get("name"):
            by_name.setdefault(a["name"], a)
    for key, cfg in (binding.get("apps") or {}).items():
        uuid = (cfg or {}).get("uuid") if isinstance(cfg, dict) else None
        a = by_id.get(uuid) if isinstance(uuid, str) and uuid else None
        if a is not None:
            rows[key], how[key] = a, "uuid"
            continue
        a = by_name.get(key)
        if a is not None:
            rows[key], how[key] = a, "name"
            continue
        how[key] = "missing"
    return rows, how


def collect_live(binding, env_path: Path, timeout=30, reporter=None):
    """采集 live 面。返回 live dict；**凭据值绝不落盘/不打印**。"""
    live = {
        "collected_at_utc": now_utc(),
        "base_url": None,
        "env_file": str(env_path),
        "env_present": env_path.exists(),
        "login_ok": False,
        "login_http": None,
        "providers": {},
        "provider_list": [],
        "provider_ids": {},
        "apps": {},
        "app_ids": {},
        "app_resolve": {},
        "notes": [],
        "network_error": None,
    }
    if not env_path.exists():
        live["notes"].append("凭据文件不存在：%s" % env_path)
        return live
    env = load_env_file(env_path)
    base = env.get("DIFY_BASE_URL")
    email = env.get("DIFY_EMAIL")
    pwd = env.get("DIFY_PASSWORD")
    missing = [k for k, v in (("DIFY_BASE_URL", base), ("DIFY_EMAIL", email),
                              ("DIFY_PASSWORD", pwd)) if not v]
    if missing:
        live["notes"].append("凭据文件缺少键：%s" % ", ".join(missing))
        return live
    live["base_url"] = base
    c = Console(base, timeout=timeout)
    ok, st = c.login(email, pwd)
    live["login_ok"] = ok
    live["login_http"] = st
    if not ok:
        live["notes"].append("登录失败（HTTP %s）——凭据值不打印" % st)
        return live

    # provider 清单（provider_id → name 解析用）
    s, d = c.get((binding.get("dify") or {}).get(
        "tool_provider_list", "/console/api/workspaces/current/tool-providers?type=api"))
    if s == 200 and isinstance(d, list):
        live["provider_list"] = d
        live["provider_ids"] = {it.get("name"): it.get("id") for it in d if it.get("name")}
    else:
        live["notes"].append("tool-providers 列表读取失败：HTTP %s" % s)

    # 每个 api provider 的解析后工具束
    for p in binding.get("providers") or []:
        if p.get("kind") != "api":
            continue
        name = p.get("provider")
        tpl = (binding.get("dify") or {}).get(
            "tool_provider_get",
            "/console/api/workspaces/current/tool-provider/api/get?provider={provider}")
        path = tpl.replace("{provider}", urllib.parse.quote(name))
        st2, e = c.get(path)
        if st2 == 200 and isinstance(e, dict) and "tools" in e:
            live["providers"][name] = e
        else:
            live["notes"].append("provider %s 回读失败：HTTP %s" % (name, st2))

    # App 面（W20：uuid 优先，名字只作兜底 —— App 名已改为中文 display_name）
    s, d = c.get("/console/api/apps?page=1&limit=100")
    items = (d.get("data") if isinstance(d, dict) else d) or []
    live["app_list_count"] = len(items) if isinstance(items, list) else None
    rows, how = resolve_app_rows(binding, items)
    live["app_resolve"] = how
    for app_name, a in rows.items():
        st3, det = c.get("/console/api/apps/%s" % a.get("id"))
        if st3 == 200 and isinstance(det, dict):
            live["apps"][app_name] = det
            live["app_ids"][app_name] = a.get("id")
        else:
            live["notes"].append("app %s 回读失败：HTTP %s（解析来源 %s）"
                                 % (app_name, st3, how.get(app_name)))
    for app_name, src in how.items():
        if src == "missing":
            live["notes"].append("app %s 在真机不存在（uuid 与名字都没命中）" % app_name)
    return live


# ===========================================================================
# 投影：模型面
# ===========================================================================
def project_tool_face(entity, base):
    """把一个 provider 回读实体投影成**模型面比较对象**。

    ★ 排除项（R-31 唯一允许的差异）：provider 名；`credentials`；每个工具的
      `server_url` 的 base 前缀（只保留 path）。
    ★ `description_human` / `description_llm` 两处都写出来 —— 依 R-40，
      Dify 把 `tools[].summary` 同时赋给 human 与 llm；此处显式展开，
      使「两处都逐字相同」在证据里可见，而不是靠读者脑补。
    """
    out = []
    for t in entity.get("tools") or []:
        url = t.get("server_url") or ""
        path = url[len(base):] if base and url.startswith(base) else url
        params = []
        for p in t.get("parameters") or []:
            params.append({k: p.get(k) for k in PARAM_FACE_FIELDS})
        params.sort(key=lambda x: (x.get("name") or ""))
        out.append({
            "tool_name": t.get("operation_id"),          # 模型面 function name
            "description_human": t.get("summary"),       # = summary（i18n 双语同串）
            "description_llm": t.get("summary"),         # = summary
            "summary": t.get("summary"),
            "openapi_description": (t.get("openapi") or {}).get("description"),
            "openapi_summary": (t.get("openapi") or {}).get("summary"),
            "openapi": t.get("openapi"),
            "method": t.get("method"),
            "path": path,
            "author": t.get("author"),
            "icon": t.get("icon"),
            "output_schema": t.get("output_schema"),
            "params": params,
        })
    out.sort(key=lambda x: x.get("tool_name") or "")
    return out


def neutralize_schema(entity, base):
    """把 provider 提交的 OpenAPI 文本里的 base 换成占位符后规范化 —— 用于整体逐字比对。

    ★ `schema` 在回读面是 **JSON 字符串**（已实测），不是 dict。
    """
    sc = entity.get("schema")
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except Exception:
            return None, "schema 不是合法 JSON 字符串"
    sc = copy.deepcopy(sc)
    for srv in (sc.get("servers") or []):
        if srv.get("url") == base:
            srv["url"] = "__BASE__"
    return sc, None


def resolve_provider_name(live, provider_id):
    for it in live.get("provider_list") or []:
        if it.get("id") == provider_id:
            return it.get("name"), it
    return None, None


# ===========================================================================
# F0
# ===========================================================================
def _check_f0_body(binding, live):
    subs = []
    pair, why = main_pair(binding, 0)
    if pair is None:
        return [sub("F0.0", "对照对声明", BLOCKED, why)]
    appA, appB, provA, provB = pair
    baseA, baseB = provider_base(binding, provA), provider_base(binding, provB)

    haveA = provA in live["providers"]
    haveB = provB in live["providers"]
    if not (haveA and haveB):
        miss = [n for n, ok in ((provA, haveA), (provB, haveB)) if not ok]
        return [sub("F0.0", "两个主 provider 回读", BLOCKED,
                    "provider 尚未创建或回读失败：%s（binding 声明 %s / %s）"
                    % (", ".join(miss), provA, provB))]

    eA, eB = live["providers"][provA], live["providers"][provB]
    fA = project_tool_face(eA, baseA)
    fB = project_tool_face(eB, baseB)

    # -- F0.a 工具集合（模型面 function name）--
    nA = [t["tool_name"] for t in fA]
    nB = [t["tool_name"] for t in fB]
    ok = nA == nB
    subs.append(sub("F0.a", "工具集合（模型面 function name，逐字且同序）",
                    PASS if ok else FAIL,
                    "两侧各 %d 个工具，%s" % (len(nA), "集合与顺序一致" if ok else "不一致"),
                    ["%s: %s" % (provA, ", ".join(nA)), "%s: %s" % (provB, ", ".join(nB))]))

    # -- F0.b 工具描述（human 与 llm 两处 + 原样存回的 openapi 描述）--
    dA = [{k: t[k] for k in ("tool_name", "description_human", "description_llm",
                             "openapi_description", "openapi_summary")} for t in fA]
    dB = [{k: t[k] for k in ("tool_name", "description_human", "description_llm",
                             "openapi_description", "openapi_summary")} for t in fB]
    diffs = diff_json(dA, dB)
    subs.append(sub("F0.b", "工具描述逐字相同（description.human + description.llm + openapi 存回）",
                    PASS if not diffs else FAIL,
                    "逐字段 diff：%d 处差异" % len(diffs),
                    ["%s -> %s || %s" % (d[0], d[1], d[2]) for d in diffs[:20]]))

    # -- F0.c 参数列表（参数名/类型/必填/描述，逐字）--
    pA = [{k: t[k] for k in ("tool_name", "params")} for t in fA]
    pB = [{k: t[k] for k in ("tool_name", "params")} for t in fB]
    diffs = diff_json(pA, pB)
    subs.append(sub("F0.c", "参数列表逐字相同（参数名/类型/必填/描述/占位符…）",
                    PASS if not diffs else FAIL,
                    "逐字段 diff：%d 处差异" % len(diffs),
                    ["%s -> %s || %s" % (d[0], d[1], d[2]) for d in diffs[:20]]))

    # -- F0.d 整体规范化 schema（base 归一后逐字）--
    scA, errA = neutralize_schema(eA, baseA)
    scB, errB = neutralize_schema(eB, baseB)
    if scA is None or scB is None:
        subs.append(sub("F0.d", "整体 OpenAPI 规范化逐字相同", BLOCKED,
                        errA or errB or "schema 不可解析"))
    else:
        hA, hB = sha256_text(stable_json(scA)), sha256_text(stable_json(scB))
        ok = hA == hB
        subs.append(sub("F0.d", "整体 OpenAPI 规范化逐字相同（base → __BASE__）",
                        PASS if ok else FAIL,
                        "sha256 %s vs %s ⇒ %s" % (hA[:16], hB[:16], "相等" if ok else "不等"),
                        ["%s normalized sha256=%s" % (provA, hA),
                         "%s normalized sha256=%s" % (provB, hB)]))

    # -- F0.e provider 级元数据（不含 credentials / provider 名）--
    mA = {k: eA.get(k) for k in PROVIDER_META_FIELDS}
    mB = {k: eB.get(k) for k in PROVIDER_META_FIELDS}
    diffs = diff_json(mA, mB)
    subs.append(sub("F0.e", "provider 级元数据逐字相同（description/custom_disclaimer/icon/labels/…）",
                    PASS if not diffs else FAIL,
                    "逐字段 diff：%d 处差异（credentials 与 provider 名按 R-31 排除）" % len(diffs),
                    ["%s -> %s || %s" % (d[0], d[1], d[2]) for d in diffs[:10]]))

    # -- F0.f 静默失效防线：provider → server_url → 载体端口 --
    subs.append(_check_carrier_mapping(binding, live))

    # -- F0.g 两个 App 的工具集合按 provider_id 交集为空 --
    subs.append(_check_disjoint(binding, live, appA, appB, provA, provB))

    # -- 模型面投影指纹（W2 验收要求「两侧投影 sha256 相等」）--
    hA = sha256_text(stable_json(fA))
    hB = sha256_text(stable_json(fB))
    subs.append(sub("F0.h", "模型面投影 sha256（两侧相等）", PASS if hA == hB else FAIL,
                    "model_face_projection sha256 %s vs %s" % (hA[:16], hB[:16]),
                    ["%s=%s" % (provA, hA), "%s=%s" % (provB, hB)]))
    return subs


def check_app_names(binding, live):
    """F0.n：真机 App 的 `name` 必须逐字等于声明的 `display_name`（W20 新增，fail-closed）。

    ★ 这是**新增**子判据，F0–F5 既有判据一条未改、未放宽、未删除。
    ★ 为什么必须有：改名后真机 name ≠ binding 机器键；旧解析「按名字取真机」会
      静默少读（0 命中却不报错）。`resolve_app_rows` 用 uuid 修掉了「少读」，
      本断言再兜一层——App 读到了但名字漂移同样 FAIL，不允许静默通过。
    ★ 声明缺 display_name / App 未回读 ⇒ BLOCKED（不把「没测到」写成「已过」）。
    """
    subs = []
    for key, cfg in (binding.get("apps") or {}).items():
        want = (cfg or {}).get("display_name") if isinstance(cfg, dict) else None
        if not (isinstance(want, str) and want.strip()):
            subs.append(sub("F0.n:%s" % key, "真机 name = 声明 display_name", BLOCKED,
                            "声明面 binding.apps.%s 缺 display_name（无法断言）" % key))
            continue
        det = (live.get("apps") or {}).get(key)
        if not isinstance(det, dict):
            subs.append(sub("F0.n:%s" % key, "真机 name = 声明 display_name", BLOCKED,
                            "App 未回读（uuid 优先解析后仍缺失）⇒ 无法断言 name"))
            continue
        got = det.get("name")
        ok = got == want
        subs.append(sub("F0.n:%s" % key, "真机 name = 声明 display_name",
                        PASS if ok else FAIL,
                        "live=%s / declared=%s" % (json.dumps(got, ensure_ascii=False),
                                                   json.dumps(want, ensure_ascii=False))))
    return subs


def check_f0(binding, live, pair_idx: int = 0):
    """F0 = 既有的 `_check_f0_body`（子判据逐条原样）+ W20 新增的名称标识断言 F0.n。

    ★ 包装而非改写：`_check_f0_body` 的所有早退路径（provider/App 未回读 ⇒ BLOCKED）
      都不会再吞掉 F0.n —— 名称断言无条件执行。
    ★ R-52：F0 比的是 **provider 面**（cc-kg-tools ↔ cc-onto-tools），而 Q/R 两个对照对
      共用同一对 provider ⇒ F0 与 pair_idx 无关，只跑一次（pair_idx 仅为统一签名而保留）。
    """
    subs = list(_check_f0_body(binding, live))
    subs.extend(check_app_names(binding, live))
    return subs


def _check_carrier_mapping(binding, live):
    ev, bad = [], []
    # ① binding 声明与项目不变量（carrier → 端口）是否自洽
    for cname, c in (binding.get("carriers") or {}).items():
        exp = PORT_BY_CARRIER.get(cname)
        got = port_from_base(c.get("service_base"))
        ev.append("carriers.%s.service_base=%s port=%s（不变量期望 %s）"
                  % (cname, c.get("service_base"), got, exp))
        if exp is None:
            bad.append("carrier %s 不在项目不变量表 %s 内" % (cname, sorted(PORT_BY_CARRIER)))
        elif got != exp:
            bad.append("carrier %s 声明端口 %s ≠ 项目不变量 %s" % (cname, got, exp))
    # ② provider 自身 base/port/carrier 三者一致 + 白名单
    wl = binding.get("whitelist") or {}
    for p in binding.get("providers") or []:
        if p.get("kind") != "api":
            continue
        name, carrier = p.get("provider"), p.get("carrier")
        base, port = provider_base(binding, name), provider_port(binding, name)
        ev.append("%s: carrier=%s base=%s port=%s" % (name, carrier, base, port))
        exp = PORT_BY_CARRIER.get(carrier)
        if exp is None:
            bad.append("%s 的 carrier %r 不在不变量表内" % (name, carrier))
        elif port != exp:
            bad.append("%s 声明端口 %s ≠ carrier %s 的不变量端口 %s" % (name, port, carrier, exp))
        if port_from_base(base) != port:
            bad.append("%s 的 base(%s) 与 port(%s) 不一致" % (name, base, port))
        if wl.get("allowed_ports") and port not in wl["allowed_ports"]:
            bad.append("%s 的端口 %s 不在 whitelist.allowed_ports %s 内"
                       % (name, port, wl.get("allowed_ports")))
        host = re.match(r"^[a-zA-Z]+://([^/:]+)", base or "")
        if wl.get("allowed_hosts") and (not host or host.group(1) not in wl["allowed_hosts"]):
            bad.append("%s 的 host 不在 whitelist.allowed_hosts %s 内" % (name, wl.get("allowed_hosts")))
    # ③ 不同 carrier 不得共用同一 base（单 workspace 静默退化风险）
    seen = {}
    for p in binding.get("providers") or []:
        if p.get("kind") != "api":
            continue
        b = provider_base(binding, p.get("provider"))
        if b in seen and seen[b] != p.get("carrier"):
            bad.append("base %s 被 carrier %s 与 %s 共用" % (b, seen[b], p.get("carrier")))
        seen[b] = p.get("carrier")
    # ④ live：每个工具的 server_url 是否解析到本 provider 声明的 base
    for p in binding.get("providers") or []:
        if p.get("kind") != "api":
            continue
        name = p.get("provider")
        base = provider_base(binding, name)
        exp_port = PORT_BY_CARRIER.get(p.get("carrier"))
        e = live.get("providers", {}).get(name)
        if e is None:
            ev.append("%s: live 未回读到（无法核对 server_url）" % name)
            continue
        off = []
        for t in e.get("tools") or []:
            url = t.get("server_url") or ""
            if not url.startswith(base + "/") and url != base:
                off.append("%s → %s" % (t.get("operation_id"), url))
            elif port_from_base(url) != exp_port:
                off.append("%s → 端口 %s ≠ %s" % (t.get("operation_id"), port_from_base(url), exp_port))
        ev.append("%s: %d 个工具的 server_url 全部指向 %s（期望端口 %s）：%s"
                  % (name, len(e.get("tools") or []), base, exp_port, "是" if not off else "否"))
        for o in off:
            bad.append("%s 工具 %s" % (name, o))
    if bad:
        return sub("F0.f", "静默失效防线 · provider → server_url → 载体端口", FAIL,
                   "发现 %d 处端口/绑定错配" % len(bad), bad[:20] + ev[:10])
    return sub("F0.f", "静默失效防线 · provider → server_url → 载体端口", PASS,
               "carrier→端口不变量、provider 声明自洽、live server_url 全部命中声明端口", ev[:20])


def _check_disjoint(binding, live, appA, appB, provA, provB):
    if appA not in live.get("apps", {}) or appB not in live.get("apps", {}):
        miss = [a for a in (appA, appB) if a not in live.get("apps", {})]
        return sub("F0.g", "两个 App 工具集合按 provider_id 交集为空", BLOCKED,
                   "App 尚未创建或回读失败：%s（未创建时**无法**断言不相交）" % ", ".join(miss))
    ev, bad = [], []
    sets = {}
    for app_name, declared_prov in ((appA, provA), (appB, provB)):
        tools = dig(live["apps"][app_name].get("model_config") or {}, "agent_mode.tools", []) or []
        ids, names = [], []
        for t in tools:
            pid = t.get("provider_id")
            ids.append(pid)
            rname, _ = resolve_provider_name(live, pid)
            names.append(rname or t.get("provider_name"))
            if rname and rname != declared_prov:
                bad.append("%s 的工具 %s 解析到 provider %s ≠ 声明的 %s"
                           % (app_name, t.get("tool_name"), rname, declared_prov))
            elif not rname:
                bad.append("%s 的工具 %s 的 provider_id=%s 在 provider 清单中解析不到"
                           % (app_name, t.get("tool_name"), pid))
        sets[app_name] = set(x for x in ids if x)
        ev.append("%s: %d 个工具，provider_id=%s，解析 provider=%s"
                  % (app_name, len(tools), sorted(sets[app_name]), sorted(set(names))))
    inter = sets.get(appA, set()) & sets.get(appB, set())
    if inter:
        bad.append("两个 App 的工具集合按 provider_id **相交**：%s ⇒ 实验静默退化为同载体对比" % sorted(inter))
    if set(sets.get(appA, set())) & set(sets.get(appB, set())) == set() and not sets.get(appA):
        bad.append("%s 未绑定任何工具（provider_id 集合为空）" % appA)
    if not sets.get(appB):
        bad.append("%s 未绑定任何工具（provider_id 集合为空）" % appB)
    return sub("F0.g", "两个 App 工具集合按 provider_id 交集为空", FAIL if bad else PASS,
               "交集 = %s" % (sorted(inter) if inter else "空集"), bad[:20] + ev)


# ===========================================================================
# 模型面字符串（F1 的扫描面）
# ===========================================================================
def model_face_strings(binding, live, app_name):
    """返回 [(where, text)]：该 App 的全部**模型面字符串**。

    = `pre_prompt` ∪ 已绑工具的 `tool_name` ∪ 工具描述 ∪ 参数名 ∪ 参数描述。
    ★ 工具描述取回读 provider 的 `summary`（R-40：= 模型面 human 与 llm），
      参数描述取 `llm_description` 与 `human_description` 的各语言值。
    """
    out = []
    unresolved = []
    app = live.get("apps", {}).get(app_name)
    if app is None:
        return out, "app 未回读", unresolved
    mc = app.get("model_config") or {}
    pp = mc.get("pre_prompt")
    if isinstance(pp, str):
        out.append(("pre_prompt", pp))
    tools = dig(mc, "agent_mode.tools", []) or []
    for t in tools:
        tn = t.get("tool_name")
        out.append(("%s.tool_name" % tn, str(tn)))
        if t.get("tool_label"):
            out.append(("%s.tool_label" % tn, str(t.get("tool_label"))))
        pid = t.get("provider_id")
        pname, _ = resolve_provider_name(live, pid)
        pname = pname or t.get("provider_name")
        ent = live.get("providers", {}).get(pname)
        if ent is None:
            # ★ 扫描面缩小 = 假 PASS 风险（R-34 要求扫「已绑全部工具」的模型面）
            unresolved.append("%s 已绑工具 %s 的 provider %r 未回读 ⇒ 其模型面无法扫描"
                              % (app_name, tn, pname))
            continue
        for pt in ent.get("tools") or []:
            if pt.get("operation_id") != tn:
                continue
            out.append(("%s/%s.summary" % (pname, tn), str(pt.get("summary") or "")))
            oa = pt.get("openapi") or {}
            out.append(("%s/%s.openapi.description" % (pname, tn), str(oa.get("description") or "")))
            out.append(("%s/%s.openapi.summary" % (pname, tn), str(oa.get("summary") or "")))
            for p in pt.get("parameters") or []:
                pn = p.get("name")
                out.append(("%s/%s.param[%s].name" % (pname, tn, pn), str(pn)))
                out.append(("%s/%s.param[%s].llm_description" % (pname, tn, pn),
                            str(p.get("llm_description") or "")))
                for lang, v in (p.get("human_description") or {}).items():
                    out.append(("%s/%s.param[%s].human_description.%s" % (pname, tn, pn, lang), str(v)))
                for lang, v in (p.get("placeholder") or {}).items():
                    out.append(("%s/%s.param[%s].placeholder.%s" % (pname, tn, pn, lang), str(v)))
    return out, None, unresolved


def scan_forbidden(rx, pairs):
    hits = []
    for where, text in pairs:
        if not isinstance(text, str):
            continue
        m = rx.search(text)
        if m:
            hits.append("%s → 命中 %r" % (where, m.group(0)))
    return hits


# ===========================================================================
# F1
# ===========================================================================
def check_f1(binding, live, pair_idx: int = 0):
    subs = []
    pair, why = main_pair(binding, pair_idx)
    if pair is None:
        return [sub("F1.0", "对照对声明", BLOCKED, why)]
    appA, appB = pair[0], pair[1]
    miss = [a for a in (appA, appB) if a not in live.get("apps", {})]
    if miss:
        return [sub("F1.0", "两个主 App 回读", BLOCKED,
                    "App 尚未创建或回读失败：%s" % ", ".join(miss))]
    mcA = live["apps"][appA].get("model_config") or {}
    mcB = live["apps"][appB].get("model_config") or {}
    if not mcA or not mcB:
        return [sub("F1.0", "两个主 App model_config", BLOCKED,
                    "model_config 为空 ⇒ model-config 尚未写入（前置条件缺失，不是内容不合规）")]
    ppA, ppB = mcA.get("pre_prompt"), mcB.get("pre_prompt")
    if "pre_prompt" not in mcA or "pre_prompt" not in mcB:
        missing = [a for a, mc in ((appA, mcA), (appB, mcB)) if "pre_prompt" not in mc]
        return [sub("F1.0", "两个主 App 的 pre_prompt 回读", BLOCKED,
                    "model_config 中缺少 pre_prompt 键：%s ⇒ 提示词尚未写入（前置条件缺失，"
                    "不是内容不合规）" % ", ".join(missing))]
    ok = ppA == ppB and isinstance(ppA, str) and ppA != ""
    subs.append(sub("F1.a", "两个主 App 的 pre_prompt 逐字相同",
                    PASS if ok else FAIL,
                    "len %s vs %s；sha256 %s vs %s" % (
                        len(ppA or ""), len(ppB or ""),
                        sha256_text(ppA or "")[:16], sha256_text(ppB or "")[:16]),
                    sorted(set(["两侧 pre_prompt 均为空串 ⇒ 「逐字相同」在语义上无意义，按 FAIL 处理"]
                                if (ppA == "" and ppB == "") else []))))
    rx, toks = forbid_regex(binding)
    hits = []
    unresolved = []
    for a in (appA, appB):
        pairs, err, unres = model_face_strings(binding, live, a)
        if err:
            subs.append(sub("F1.b", "禁用词扫描面", BLOCKED, "%s: %s" % (a, err)))
            return subs
        unresolved.extend(unres)
        h = scan_forbidden(rx, pairs)
        hits.extend("%s · %s" % (a, x) for x in h)
    if unresolved:
        # ★ 扫描面不完整时**绝不**给 PASS：那会是把「没扫到」当成「没命中」
        subs.append(sub("F1.b", "禁用词零命中（pre_prompt ∪ 已绑工具模型面全字符串）", BLOCKED,
                        "扫描面不完整（%d 处未解析）⇒ 无法主张零命中" % len(unresolved),
                        unresolved[:20]))
        return subs
    subs.append(sub("F1.b", "禁用词零命中（pre_prompt ∪ 已绑工具模型面全字符串）",
                    PASS if not hits else FAIL,
                    "扫描面 token=%s；命中 %d 处" % (toks, len(hits)),
                    hits[:30] or ["零命中"]))
    return subs


# ===========================================================================
# F2 / F3
# ===========================================================================
def _main_apps(binding, live, label, pair_idx: int = 0):
    pair, why = main_pair(binding, pair_idx)
    if pair is None:
        return None, [sub("%s.0" % label, "对照对声明", BLOCKED, why)]
    appA, appB = pair[0], pair[1]
    miss = [a for a in (appA, appB) if a not in live.get("apps", {})]
    if miss:
        return None, [sub("%s.0" % label, "两个主 App 回读", BLOCKED,
                          "App 尚未创建或回读失败：%s" % ", ".join(miss))]
    mcA = live["apps"][appA].get("model_config") or {}
    mcB = live["apps"][appB].get("model_config") or {}
    if not mcA or not mcB or not dig(mcA, "agent_mode") or not dig(mcB, "agent_mode"):
        return None, [sub("%s.0" % label, "两个主 App 配置落地", BLOCKED,
                          "model_config 或 agent_mode 缺失 ⇒ 配置尚未落地（前置条件缺失）")]
    return (appA, appB, mcA, mcB), None


def check_f2(binding, live, pair_idx: int = 0):
    got, blocked = _main_apps(binding, live, "F2", pair_idx)
    if got is None:
        return blocked
    appA, appB, mcA, mcB = got
    subs = []
    # F2.a model 块逐字相同（含 temperature 实际回读值）
    mA, mB = mcA.get("model"), mcB.get("model")
    diffs = diff_json(mA, mB)
    subs.append(sub("F2.a", "model 块逐字相同（含 temperature 回读值）",
                    PASS if not diffs else FAIL,
                    "temperature 回读：%s vs %s（%d 处差异）" % (
                        dig(mA, "completion_params.temperature"), dig(mB, "completion_params.temperature"),
                        len(diffs)),
                    ["%s -> %s || %s" % (d[0], d[1], d[2]) for d in diffs[:20]]))
    # F2.b agent_mode.enabled/strategy/max_iteration 逐字相同
    aA = {k: dig(mcA, "agent_mode.%s" % k) for k in ("enabled", "strategy", "max_iteration")}
    aB = {k: dig(mcB, "agent_mode.%s" % k) for k in ("enabled", "strategy", "max_iteration")}
    diffs = diff_json(aA, aB)
    subs.append(sub("F2.b", "agent_mode(enabled/strategy/max_iteration) 逐字相同",
                    PASS if not diffs else FAIL, "%d 处差异" % len(diffs),
                    ["%s -> %s || %s" % (d[0], d[1], d[2]) for d in diffs[:10]]))
    # F2.c strategy 必须 react
    ok = aA.get("strategy") == "react" and aB.get("strategy") == "react"
    subs.append(sub("F2.c", "strategy 必须为 react", PASS if ok else FAIL,
                    "%s=%r %s=%r" % (appA, aA.get("strategy"), appB, aB.get("strategy"))))
    # F2.d 每个工具的 tool_parameters == {}
    bad = []
    for a, mc in ((appA, mcA), (appB, mcB)):
        for t in dig(mc, "agent_mode.tools", []) or []:
            tp = t.get("tool_parameters")
            if tp != {}:
                bad.append("%s · %s tool_parameters=%s" % (a, t.get("tool_name"), trunc(tp, 80)))
    subs.append(sub("F2.d", "两侧每个工具的 tool_parameters 均为 {}", PASS if not bad else FAIL,
                    "非空条目 %d 个" % len(bad), bad[:20] or ["全部为 {}"]))
    # F2.e 不得出现 function_calling_type（1.17.0 不存在该字段）
    bad = []
    for a, mc in ((appA, mcA), (appB, mcB)):
        for where, _v in _walk_keys(mc):
            if where.endswith("function_calling_type"):
                bad.append("%s · %s" % (a, where))
    subs.append(sub("F2.e", "不得出现 function_calling_type（Dify 1.17.0 无此字段）",
                    PASS if not bad else FAIL,
                    "命中 %d 处（注入该字段属错误结论落地）" % len(bad), bad[:10] or ["零命中"]))
    return subs


def _walk_keys(obj, prefix="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield "%s.%s" % (prefix, k), v
            for x in _walk_keys(v, "%s.%s" % (prefix, k)):
                yield x
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for x in _walk_keys(v, "%s[%d]" % (prefix, i)):
                yield x


def check_f3(binding, live, pair_idx: int = 0):
    got, blocked = _main_apps(binding, live, "F3", pair_idx)
    if got is None:
        return blocked
    appA, appB, mcA, mcB = got
    subs = []
    enA, enB = dig(mcA, "agent_mode.enabled"), dig(mcB, "agent_mode.enabled")
    subs.append(sub("F3.a", "agent_mode.enabled 两侧均为 true", 
                    PASS if enA is True and enB is True else FAIL,
                    "%s=%r %s=%r" % (appA, enA, appB, enB)))
    sA, sB = dig(mcA, "agent_mode.strategy"), dig(mcB, "agent_mode.strategy")
    subs.append(sub("F3.b", "strategy 存在且两侧同值",
                    PASS if sA is not None and sA == sB else FAIL,
                    "%s=%r %s=%r" % (appA, sA, appB, sB)))
    iA, iB = dig(mcA, "agent_mode.max_iteration"), dig(mcB, "agent_mode.max_iteration")
    subs.append(sub("F3.c", "max_iteration 两侧同值",
                    PASS if iA is not None and iA == iB else FAIL,
                    "%s=%r %s=%r" % (appA, iA, appB, iB)))
    bad = []
    for a, mc in ((appA, mcA), (appB, mcB)):
        tools = dig(mc, "agent_mode.tools", []) or []
        if not tools:
            bad.append("%s 未绑定任何工具" % a)
        for t in tools:
            if t.get("enabled") is not True:
                bad.append("%s · %s enabled=%r（manager.py:70-84 会丢弃 enabled 非真的条目 ⇒ 静默不生效）"
                           % (a, t.get("tool_name"), t.get("enabled")))
    subs.append(sub("F3.d", "每个已绑工具条目 enabled=true", PASS if not bad else FAIL,
                    "异常条目 %d 个" % len(bad), bad[:20] or ["全部 enabled=true"]))
    # -- ★ R-52 F3.e：已绑工具集合 == 声明集合，且与 tools_removed_from_setb 无交集 --
    #    这是「R 臂结构性排除 cc_query」的**运行期**护栏：即使 provider 侧仍有该工具，
    #    App 没绑 ⇒ 模型拿不到（与 deploy_agents.py --check 的声明-回读比对互为独立证据）。
    apps_decl = binding.get("apps") or {}
    bad = []
    ev = []
    for a, mc in ((appA, mcA), (appB, mcB)):
        decl = list((apps_decl.get(a) or {}).get("tools") or [])
        removed = list((apps_decl.get(a) or {}).get("tools_removed_from_setb") or [])
        bound = [t.get("tool_name") for t in (dig(mc, "agent_mode.tools", []) or [])
                 if isinstance(t, dict)]
        if sorted(bound) != sorted(decl):
            bad.append("%s 已绑工具 %s != 声明 %s" % (a, sorted(bound), sorted(decl)))
        leak = sorted(set(bound) & set(removed))
        if leak:
            bad.append("%s 已绑了 tools_removed_from_setb 里的工具 %s（结构性排除失效）"
                       % (a, leak))
        ev.append("%s: bound=%d declared=%d removed=%s"
                  % (a, len(bound), len(decl), sorted(removed) or "无"))
    subs.append(sub("F3.e", "已绑工具集合 == 声明集合，且不含 tools_removed_from_setb（R-52）",
                    PASS if not bad else FAIL,
                    "偏离 %d 处" % len(bad), bad[:20] + ev))
    return subs


# ===========================================================================
# F4
# ===========================================================================
def check_f4(binding, live, pair_idx: int = 0):
    got, blocked = _main_apps(binding, live, "F4", pair_idx)
    if got is None:
        return blocked
    appA, appB, mcA, mcB = got
    subs = []
    # F4.a 两侧键集合对称
    kA = set(mcA)
    kB = set(mcB)
    asym = sorted(kA ^ kB)
    subs.append(sub("F4.a", "两侧 model_config 键集合对称", PASS if not asym else FAIL,
                    "不对称键：%s" % (asym or "无"), asym[:20]))
    # F4.b 未使用字段两侧逐字相等
    bad = []
    ev = []
    for k in F4_COVERED_KEYS:
        if k not in kA or k not in kB:
            continue
        if stable_json(mcA[k]) != stable_json(mcB[k]):
            d = diff_json(mcA[k], mcB[k], k)
            bad.append("%s 两侧不等：%s" % (k, "; ".join("%s %s||%s" % (x[0], x[1], x[2]) for x in d[:3])))
    ev.append("比较键：%s" % ", ".join(F4_COVERED_KEYS))
    subs.append(sub("F4.b", "未使用字段两侧逐字相同", PASS if not bad else FAIL,
                    "不等键 %d 个" % len(bad), bad[:20] + ev))
    # F4.c 同为「默认值」（仅对已取证的结构性默认值断言）
    bad = []
    ev = ["默认值来源：%s" % F4_DEFAULT_PROVENANCE]
    for path, exp, note in F4_DEFAULT_ASSERTED:
        for a, mc in ((appA, mcA), (appB, mcB)):
            got_v = dig(mc, path, _MISSING)
            if got_v is _MISSING:
                bad.append("%s 缺少键 %s（期望默认 %s）" % (a, path, trunc(exp, 40)))
                continue
            if exp is None:
                okv = got_v is None or got_v == ""
            else:
                okv = got_v == exp
            if not okv:
                bad.append("%s · %s = %s（期望默认 %s；%s）"
                           % (a, path, trunc(got_v, 60), trunc(exp, 40), note))
    subs.append(sub("F4.c", "未使用字段同为默认值（已取证的结构性默认）",
                    PASS if not bad else FAIL,
                    "偏离默认值 %d 处" % len(bad), bad[:30] + ev))
    return subs


# ===========================================================================
# F5
# ===========================================================================
def check_f5(binding, live):
    subs = []
    apps = binding.get("apps") or {}
    kgt_apps = [a for a, cfg in apps.items() if cfg.get("carrier") == "kgt"]
    if not kgt_apps:
        return [sub("F5.0", "KG-T App 声明", BLOCKED, "binding.apps 中没有 carrier=kgt 的 App")]
    main_kgt = None
    for a in kgt_apps:
        prov = apps[a].get("provider")
        if (binding_provider(binding, prov) or {}).get("kind") == "api":
            main_kgt = a
    if main_kgt is None:
        return [sub("F5.0", "KG-T 主组 App 声明", BLOCKED, "找不到 carrier=kgt 且 provider 为 api 的 App")]
    if main_kgt not in live.get("apps", {}):
        subs.append(sub("F5.0", "KG-T 主组 App 回读", BLOCKED,
                        "App %s 尚未创建（F5 的工具面与 pre_prompt 均无从核对）" % main_kgt))
        for extra in _f5_extra_apps(binding, live, kgt_apps, main_kgt):
            subs.append(extra)
        return subs
    mc = live["apps"][main_kgt].get("model_config") or {}
    if not mc or not dig(mc, "agent_mode"):
        subs.append(sub("F5.0", "KG-T 主组 App 配置落地", BLOCKED,
                        "%s 的 model_config / agent_mode 缺失 ⇒ 配置尚未落地" % main_kgt))
        for extra in _f5_extra_apps(binding, live, kgt_apps, main_kgt):
            subs.append(extra)
        return subs
    prov = apps[main_kgt].get("provider")
    declared = list((binding_provider(binding, prov) or {}).get("tools") or [])
    tools = dig(mc, "agent_mode.tools", []) or []
    names = [t.get("tool_name") for t in tools]
    subs.append(sub("F5.a", "★ KG-T 主组排除在 F0/F1 之外（它必须有提示词变体）", PASS,
                    "%s 不参与 F0/F1 判等；其工具面含「图」属 F5 预期豁免" % main_kgt))
    # F5.b 恰为 binding 声明的 N 个工具
    exp_n = (binding_provider(binding, prov) or {}).get("expected_tool_count") or len(declared)
    ok = len(tools) == exp_n
    subs.append(sub("F5.b", "注册工具恰为 %d 个（binding 声明）" % exp_n, PASS if ok else FAIL,
                    "实际 %d 个：%s" % (len(tools), ", ".join(str(x) for x in names)),
                    ["binding expected_tool_count=%s declared=%s" % (exp_n, declared)]))
    # F5.c 不含 cc_query（及任何 denied_*）
    denied = set((binding.get("whitelist") or {}).get("denied_tool_names") or [])
    hits = [n for n in names if n == "cc_query"] + [n for n in names if n in denied]
    subs.append(sub("F5.c", "工具序列不含 cc_query（且无任何 denied_* 工具）",
                    PASS if not hits else FAIL, "命中：%s" % (sorted(set(hits)) or "无"),
                    ["denied_tool_names=%s" % sorted(denied)]))
    # F5.d 工具集合 = binding 声明（注册面 ↔ 声明面零漂移）
    ok = sorted(x for x in names if x) == sorted(declared)
    subs.append(sub("F5.d", "注册工具集合 = binding 声明集合", PASS if ok else FAIL,
                    "live=%s declared=%s" % (sorted(x for x in names if x), sorted(declared))))
    # F5.e pre_prompt 禁用词零命中（★ 只扫 pre_prompt：KG-T 工具面豁免）
    rx, toks = forbid_regex(binding)
    pp = mc.get("pre_prompt")
    hits = scan_forbidden(rx, [("%s.pre_prompt" % main_kgt, pp if isinstance(pp, str) else "")])
    subs.append(sub("F5.e", "KG-T 主组 pre_prompt 禁用词零命中（工具面按 F5 豁免）",
                    PASS if not hits else FAIL,
                    "token=%s；pre_prompt len=%s" % (toks, len(pp or "")),
                    hits or ["零命中"]))
    for extra in _f5_extra_apps(binding, live, kgt_apps, main_kgt):
        subs.append(extra)
    return subs


def _f5_extra_apps(binding, live, kgt_apps, main_kgt):
    """F5 原文之外的附加检查：其它 kgt 系 App（如插件臂）的工具集合是否等于 binding 声明。

    ★ 明确标注为「超出 docs\\05 F5 原文」—— 原文只覆盖 KG-T / KG-T-dyn。
    """
    out = []
    for a in kgt_apps:
        if a == main_kgt:
            continue
        cfg = binding["apps"][a]
        prov = cfg.get("provider")
        p = binding_provider(binding, prov) or {}
        declared = list(p.get("tools") or p.get("bind_tools") or [])
        kind = p.get("kind")
        if a not in live.get("apps", {}):
            out.append(sub("F5.x:%s" % a, "%s 工具集合 = binding 声明（附加检查，非 F5 原文）" % a,
                           BLOCKED, "%s 尚未创建（provider kind=%s）" % (a, kind)))
            continue
        mc = live["apps"][a].get("model_config") or {}
        tools = dig(mc, "agent_mode.tools", []) or []
        names = [t.get("tool_name") for t in tools]
        ok = sorted(x for x in names if x) == sorted(declared)
        out.append(sub("F5.x:%s" % a, "%s 工具集合 = binding 声明（附加检查，非 F5 原文）" % a,
                       PASS if ok else FAIL,
                       "live=%s declared=%s（provider kind=%s）" % (sorted(x for x in names if x),
                                                                   sorted(declared), kind)))
    return out


# ===========================================================================
# 汇总
# ===========================================================================
CHECK_FUNCS = {"F0": check_f0, "F1": check_f1, "F2": check_f2,
               "F3": check_f3, "F4": check_f4, "F5": check_f5}
# ★ R-52：随**对照对**重复的判据（App 级）。F0 比的是 provider 面（两对共用同一对 provider，
#   跑一次即可）；F5 是方案3 专属（无对照对）⇒ 两者都不随对照对重复。
PAIR_SCOPED_LABELS = ("F1", "F2", "F3", "F4")


def evaluate(binding, live, only=None, reporter=None):
    labels = [x for x in (only or ALL_LABELS) if x in CHECK_FUNCS]
    checks = {}
    for lab in labels:
        subs = CHECK_FUNCS[lab](binding, live)
        checks[lab] = {"verdict": worst([s["verdict"] for s in subs]), "subchecks": subs}
    # ★ R-52：对第 2..n 个对照对（如 R 臂）重复跑 App 级判据，结果挂到 `<label>@<对>`。
    #   标签后缀让「哪一对不合格」在报告里一眼可辨；不改任何既有子判据的 id 与口径。
    try:
        _pairs = comparison_pairs_of(binding)
    except ValueError:
        _pairs = []
    for _idx, _pr in enumerate(_pairs[1:], start=1):
        _tag = "@%s" % "+".join(_pr)
        for lab in labels:
            if lab not in PAIR_SCOPED_LABELS:
                continue
            key = lab + _tag
            subs = CHECK_FUNCS[lab](binding, live, pair_idx=_idx)
            checks[key] = {"verdict": worst([s["verdict"] for s in subs]), "subchecks": subs}
            labels.append(key)
    counts = {PASS: 0, FAIL: 0, BLOCKED: 0}
    for c in checks.values():
        counts[c["verdict"]] += 1
    overall = BLOCKED if counts[BLOCKED] else (FAIL if counts[FAIL] else PASS)
    return {"checks": checks, "counts": counts, "overall": overall,
            "exit_code": EXIT_FOR[overall], "labels": labels}


def print_report(rep, res, binding, live, meta):
    rep.line("=" * 78)
    rep.line("M4 公平性门禁 check_fairness.py v%s · %s" % (VERSION, meta.get("mode")))
    rep.line("=" * 78)
    rep.line("脚本指纹: bytes=%s lf=%s cr=%s bom=%s sha256=%s"
             % (meta["script"]["bytes"], meta["script"]["lf_lines"], meta["script"]["cr_bytes"],
                meta["script"]["bom"], meta["script"]["sha256"]))
    rep.line("binding : %s  sha256=%s" % (meta["binding"]["path"], meta["binding"]["sha256"]))
    rep.line("env     : %s（present=%s）" % (meta["env_path"], live.get("env_present")))
    rep.line("采集时点: %s    base=%s   login_ok=%s (HTTP %s)"
             % (live.get("collected_at_utc"), live.get("base_url"),
                live.get("login_ok"), live.get("login_http")))
    rep.line("live    : providers=%s  apps=%s"
             % (sorted(live.get("providers", {})), sorted(live.get("apps", {}))))
    for n in live.get("notes") or []:
        rep.line("  note: %s" % n)
    rep.line("-" * 78)
    for lab in res["labels"]:
        c = res["checks"][lab]
        rep.line("[%s] %s" % (c["verdict"], lab))
        for s in c["subchecks"]:
            rep.line("    [%s] %-6s %s" % (s["verdict"], s["id"], s["title"]))
            rep.line("             %s" % s["detail"])
            for evl in s["evidence"][:8]:
                rep.line("             · %s" % trunc(evl, 300))
    rep.line("-" * 78)
    fails = [(lab, s["id"], s["title"]) for lab in res["labels"]
             for s in res["checks"][lab]["subchecks"] if s["verdict"] == FAIL]
    blocks = [(lab, s["id"], s["title"]) for lab in res["labels"]
              for s in res["checks"][lab]["subchecks"] if s["verdict"] == BLOCKED]
    if fails:
        rep.line("FAIL 明细（%d）：" % len(fails))
        for lab, sid, t in fails:
            rep.line("  - %s/%s %s" % (lab, sid, t))
    if blocks:
        rep.line("BLOCKED 明细（%d）：" % len(blocks))
        for lab, sid, t in blocks:
            rep.line("  - %s/%s %s" % (lab, sid, t))
    rep.line("逐判据结论: %s" % json.dumps(
        {lab: res["checks"][lab]["verdict"] for lab in res["labels"]}, ensure_ascii=False))
    rep.line("计数: PASS=%d FAIL=%d BLOCKED=%d" % (res["counts"][PASS], res["counts"][FAIL],
                                                  res["counts"][BLOCKED]))
    rep.line("★ 整体: %s（退出码 %d）  优先级 BLOCKED(2) > FAIL(1) > PASS(0)"
             % (res["overall"], res["exit_code"]))
    rep.line("=" * 78)


def uncovered_report(binding, live, res):
    """「未覆盖字段 / 未覆盖判据」清单（任务 §4④）。"""
    misc = {}
    models = [live["apps"][a].get("model_config") or {} for a in live.get("apps", {})]
    seen_keys = set()
    for mc in models:
        seen_keys |= set(mc)
    misc["model_config_keys_observed"] = sorted(seen_keys)
    misc["model_config_keys_covered_by_F1_F4"] = COVERED_MODEL_CONFIG_KEYS
    misc["model_config_keys_excluded_as_metadata"] = METADATA_MODEL_CONFIG_KEYS
    misc["model_config_keys_uncovered"] = sorted(seen_keys - set(COVERED_MODEL_CONFIG_KEYS))
    misc["model_config_keys_uncovered_expected"] = sorted(
        set(MODEL_CONFIG_TEMPLATE_KEYS_OBSERVED) - set(COVERED_MODEL_CONFIG_KEYS)
        - set(METADATA_MODEL_CONFIG_KEYS))
    misc["model_config_template_keys_observed_provenance"] = (
        "Dify 1.17.0 真机 · 既有 agent-chat App 2c1a708e-60ba-44e1-87c8-de3ab830fb40 的 "
        "model_config 顶层键（24 键）")
    misc["f4_default_asserted"] = ["%s == %s（%s）" % (p, trunc(v, 40), n)
                                  for p, v, n in F4_DEFAULT_ASSERTED]
    misc["f4_equality_only_no_default_constant"] = F4_EQUALITY_ONLY
    misc["f4_default_provenance"] = F4_DEFAULT_PROVENANCE
    misc["not_checked"] = [
        "两侧服务自身的 /openapi.json（R-40：注册面由我方生成，两侧自述文档本就不同）",
        "运行期真正注入模型的 prompt 文本（无抓包；仅源码推定）",
        "模型 provider 的配额/限流/实际版本漂移",
        "created_at/created_by/updated_at/updated_by（元数据）",
        "KG-T 工具面的禁用词（F5 豁免）",
        "F0/F5 不随对照对重复（F0 = provider 面，两对共用同一对 provider；F5 = 方案3 专属）",
        "插件面 neo4j_query 的入参 schema 与凭据可用性（属契约门禁 M4-C7–C9）",
        "tool_parameters 的语义正确性（只断言两侧同为 {}）",
    ]
    if not live.get("apps"):
        misc["blocked_reason_if_any"] = "两个主对比组 App 尚未创建 ⇒ F1–F5 无法评价"
    return misc


# ===========================================================================
# 离线自检（--selftest）：夹具 + 真跑负控
# ===========================================================================
FIXTURE_TOOLS_SETB = ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace",
                      "cc_cause", "cc_propose"]
FIXTURE_TOOLS_KGT = ["get_object_topology", "get_fault_topology", "get_metrics", "get_facts"]
FIXTURE_PROMPT = (
    "你是 GaussDB 运维诊断助手。\n"
    "【第一步·理解与取证】先调 cc_guide 取指标目录，再调 cc_facts 取事实。\n"
    "【第二步·收敛与建议】调 cc_cause 做因果收敛，调 cc_trace 补拓扑，最后调 cc_propose 出建议。\n"
    "【硬性要求】每一步都必须引用具体的事实 id 作为依据；证据不足时明确说明「无法定位」，不得猜测。\n"
)


def _fx_param(name, typ, required, desc):
    langs = {"en_US": desc, "zh_Hans": desc, "pt_BR": desc, "ja_JP": desc}
    return {"name": name, "label": dict(langs), "placeholder": dict(langs), "scope": None,
            "auto_generate": None, "template": None, "required": required, "default": None,
            "min": None, "max": None, "precision": None, "options": [], "type": typ,
            "multiple": False, "human_description": dict(langs), "form": "llm",
            "llm_description": desc, "input_schema": None}


def _fx_specs(tools_from):
    if tools_from == "setb":
        return [("cc_guide", "/guide", [], "列出本环境可用的指标目录、对象类型与规则清单用于规划后续调用。最多返回 200 行。"),
                ("cc_schema", "/schema", [], "返回本环境的对象类型、关系类型与属性清单（只读）。"),
                ("cc_query", "/query", [("query", "string", True, "只读知识查询语句")],
                 "提交一个只读知识查询，返回匹配的对象与属性。"),
                ("cc_facts", "/facts", [("objectType", "string", True, "对象类型名"),
                                        ("objectId", "string", False, "对象 id"),
                                        ("metricCodes", "array", False, "指标码列表")],
                 "取指定对象类型的事实与指标。" ),
                ("cc_trace", "/trace", [("startId", "string", True, "起始对象 id"),
                                        ("direction", "string", False, "方向"),
                                        ("maxHops", "number", False, "最大跳数")],
                 "沿指定起始对象返回有界跳数的拓扑链路。"),
                ("cc_cause", "/cause", [("symptomCodes", "array", True, "症状码"),
                                        ("maxHops", "number", False, "最大跳数")],
                 "根据症状码返回候选根因及其路径并给出排序依据。"),
                ("cc_propose", "/propose", [("rootCause", "string", True, "根因"),
                                            ("evidencePath", "array", False, "证据路径")],
                 "基于根因与证据路径给出建议动作（仅建议，不执行）。")]
    return [("get_object_topology", "/topology/object", [("scope", "string", True, "查询 id；支持 * 表示全部")],
             "取对象关系拓扑子集（四层：App / Runtime / Data）。"),
            ("get_fault_topology", "/topology/fault", [("scope", "string", True, "查询 id")],
             "取故障传播拓扑子集。"),
            ("get_metrics", "/metrics", [("metricCodes", "array", True, "指标码")],
             "取指定指标的时间序列。"),
            ("get_facts", "/facts", [("objectId", "string", True, "对象 id")],
             "取对象的事实明细。")]


def _fx_provider_entity(binding, provider, mutate=None):
    """构造与真机回读**同形**的 provider 实体（键名逐一对齐实测）。"""
    p = binding_provider(binding, provider)
    base = provider_base(binding, provider)
    specs = _fx_specs(p.get("tools_from"))
    tools = []
    for name, path, params, summary in specs:
        pl = [_fx_param(*x) for x in params]
        if not pl:
            pl = [_fx_param("_unused", "string", False, "本工具无需参数，请传空字符串")]
        schema_props = {pp["name"]: {"type": pp["type"], "description": pp["llm_description"]}
                        for pp in pl if pp["name"] != "_unused"}
        op = {"summary": summary, "description": summary, "operationId": name,
              "requestBody": {"required": True, "content": {"application/json": {"schema": {
                  "type": "object", "properties": schema_props,
                  "required": [pp["name"] for pp in pl if pp["required"]]}}}},
              "responses": {"200": {"description": "ok", "content": {"application/json": {
                  "schema": {"type": "object"}}}}}}
        tools.append({"author": "", "icon": None, "method": "post", "openapi": op,
                      "operation_id": name, "output_schema": {},
                      "parameters": pl, "server_url": base + path, "summary": summary})
    sc = {"openapi": "3.1.0", "info": {"title": "M4 运维工具体系", "version": "1.0.0",
                                       "description": "M4 工具体系 · 只读运维工具集"},
          "servers": [{"url": base}],
          "paths": {sp[1]: {"post": {"operationId": sp[0], "summary": sp[3], "description": sp[3]}}
                    for sp in specs}}
    ent = {"credentials": {"auth_type": "none"}, "custom_disclaimer": "GaussDB 运维 Agent 工具（M4 工具体系）",
           "description": "M4 工具体系 · 只读运维工具集", "icon": {"background": "#3B82F6", "content": "T"},
           "labels": [], "privacy_policy": "", "schema": json.dumps(sc, ensure_ascii=False),
           "schema_type": "openapi", "tools": tools}
    if mutate:
        ent = mutate(ent)
    return ent


def _fx_model_config(prompt, tools, temperature=0.3, max_iteration=40, strategy="react"):
    return {
        "pre_prompt": prompt, "prompt_type": "simple",
        "model": {"provider": "langgenius/openai_api_compatible/openai_api_compatible",
                  "name": "deepseek_v4", "mode": "chat",
                  "completion_params": {"temperature": temperature, "stop": []}},
        "agent_mode": {"enabled": True, "strategy": strategy, "max_iteration": max_iteration,
                       "prompt": None, "tools": tools},
        "dataset_configs": {"retrieval_model": "multiple", "top_k": 4,
                            "reranking_mode": "reranking_model",
                            "reranking_model": {"reranking_provider_name": "x", "reranking_model_name": "y"},
                            "datasets": {"datasets": []}},
        "dataset_query_variable": "", "external_data_tools": [],
        "file_upload": {"enabled": False, "allowed_file_types": [], "allowed_file_extensions": [],
                        "image": {"enabled": False, "number_limits": 3, "detail": "high",
                                  "transfer_methods": ["remote_url", "local_file"]}},
        "retriever_resource": {"enabled": True}, "sensitive_word_avoidance": {"enabled": False},
        "annotation_reply": {"enabled": False}, "more_like_this": {"enabled": False},
        "opening_statement": "", "suggested_questions": [],
        "suggested_questions_after_answer": {"enabled": True},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False, "voice": "", "language": ""},
        "chat_prompt_config": {"prompt": [{"role": "system", "text": ""}]},
        "completion_prompt_config": {"prompt": {"text": ""},
                                     "conversation_histories_role": {"user_prefix": "", "assistant_prefix": ""}},
        "user_input_form": [],
        "created_at": 1789010333, "created_by": "u-1",
        "updated_at": 1789010333, "updated_by": "u-1",
    }


def _fx_bound_tool(provider_name, provider_id, tool_name, enabled=True, tool_parameters=None):
    return {"enabled": enabled, "isDeleted": False, "notAuthor": False,
            "provider_id": provider_id, "provider_name": provider_name,
            "provider_type": "api", "tool_label": tool_name,
            "tool_name": tool_name,
            "tool_parameters": {} if tool_parameters is None else tool_parameters,
            "type": "api"}


FX_IDS = {"cc-kg-tools": "11111111-1111-1111-1111-111111111111",
          "cc-onto-tools": "22222222-2222-2222-2222-222222222222",
          "cc-kgt-tools": "33333333-3333-3333-3333-333333333333"}


def fixture_binding():
    return {
        "meta": {"contract": "m4-dify-binding", "version": "1.0.0"},
        "dify": {"tool_provider_list": "/console/api/workspaces/current/tool-providers?type=api",
                 "tool_provider_get": "/console/api/workspaces/current/tool-provider/api/get?provider={provider}"},
        "workspace": {"mode": "single", "id": "6dbcf416-0713-48d6-b4f7-a4583bff18de"},
        "carriers": {"kg": {"arms": ["KG-Q"], "service_base": "http://192.0.2.10:8301",
                            "provider": "cc-kg-tools", "app": "cc-kg-agent"},
                     "onto": {"arms": ["ONTO-Q"], "service_base": "http://192.0.2.10:8302",
                              "provider": "cc-onto-tools", "app": "cc-onto-agent"},
                     "kgt": {"arms": ["KG-T"], "service_base": "http://192.0.2.10:8303",
                             "provider": "cc-kgt-tools", "app": "cc-kgt-agent"}},
        "whitelist": {"provider_names": ["cc-kg-tools", "cc-onto-tools", "cc-kgt-tools"],
                      "allowed_ports": [8301, 8302, 8303], "allowed_hosts": ["192.0.2.10"],
                      "denied_scope": ["cc-kgt-tools"],
                      "denied_tool_names": ["cc_query", "run_rules", "validate", "cc_guide", "cc_schema",
                                            "cc_trace", "cc_cause", "cc_propose", "collect_now"]},
        "providers": [
            {"provider": "cc-kg-tools", "kind": "api", "carrier": "kg",
             "base": "http://192.0.2.10:8301", "port": 8301, "tools_from": "setb",
             "tools": list(FIXTURE_TOOLS_SETB), "expected_tool_count": 7},
            {"provider": "cc-onto-tools", "kind": "api", "carrier": "onto",
             "base": "http://192.0.2.10:8302", "port": 8302, "tools_from": "setb",
             "tools": list(FIXTURE_TOOLS_SETB), "expected_tool_count": 7},
            {"provider": "cc-kgt-tools", "kind": "api", "carrier": "kgt",
             "base": "http://192.0.2.10:8303", "port": 8303, "tools_from": "kgt",
             "tools": list(FIXTURE_TOOLS_KGT), "expected_tool_count": 4},
            {"provider": "neo4j_query", "kind": "plugin", "carrier": "kgt-free",
             "bind_tools": ["neo4j_query"], "version": "0.2.0"},
        ],
        "apps": {
            # ★ W20：夹具补齐 uuid 与 display_name，与真机 binding 同形。
            #   uuid 用于 resolve_app_rows 的 uuid 优先解析；display_name 用于 F0.n 断言。
            "cc-kg-agent": {"uuid": "aaaa", "display_name": "方案1·知识图谱+LLM（主对比组）",
                            "carrier": "kg", "provider": "cc-kg-tools", "tools": list(FIXTURE_TOOLS_SETB)},
            "cc-onto-agent": {"uuid": "bbbb", "display_name": "方案2·本体+LLM（主对比组）",
                              "carrier": "onto", "provider": "cc-onto-tools", "tools": list(FIXTURE_TOOLS_SETB)},
            "cc-kgt-agent": {"uuid": "cccc", "display_name": "方案3·知识图谱直引·门面版",
                             "carrier": "kgt", "provider": "cc-kgt-tools", "tools": list(FIXTURE_TOOLS_KGT)},
            "cc-kgt-free-agent": {"uuid": "dddd", "display_name": "方案3·知识图谱直引·插件直连版",
                                  "carrier": "kgt-free", "provider": "neo4j_query", "tools": ["neo4j_query"]},
        },
        "fairness": {
            "compare_pairs": [["cc-kg-agent", "cc-onto-agent"]],
            "model_face_projection": {"fields": ["pre_prompt", "tool_name", "tool_description",
                                                 "param_name", "param_description"],
                                      "exempt_providers": ["cc-kgt-tools"]},
            "forbid_tokens": ["cypher", "sparql", "图", "本体", "三元组", "property graph",
                              "RDF", "OWL", "知识图谱", "图谱"],
        },
        "counts": {"setb_tools": 7, "kgt_main_tools": 4},
    }


def fixture_live(binding, mutate=None):
    """完全合规的 live 夹具（与真机回读同形）。mutate(live) 可注入缺陷。"""
    live = {"collected_at_utc": "SELFTEST-FIXTURE", "base_url": "http://dify.fixture",
            "env_file": "(fixture)", "env_present": True, "login_ok": True, "login_http": 200,
            "providers": {}, "provider_list": [], "provider_ids": {}, "apps": {}, "app_ids": {},
            "notes": [], "network_error": None}
    for p in binding["providers"]:
        if p.get("kind") == "api":
            live["providers"][p["provider"]] = _fx_provider_entity(binding, p["provider"])
            live["provider_list"].append({"id": FX_IDS[p["provider"]], "name": p["provider"],
                                          "type": "api", "tools": [], "allow_delete": True})
            live["provider_ids"][p["provider"]] = FX_IDS[p["provider"]]
    kg_tools = [_fx_bound_tool("cc-kg-tools", FX_IDS["cc-kg-tools"], n) for n in FIXTURE_TOOLS_SETB]
    onto_tools = [_fx_bound_tool("cc-onto-tools", FX_IDS["cc-onto-tools"], n) for n in FIXTURE_TOOLS_SETB]
    kgt_tools = [_fx_bound_tool("cc-kgt-tools", FX_IDS["cc-kgt-tools"], n) for n in FIXTURE_TOOLS_KGT]
    free_tools = [{"enabled": True, "isDeleted": False, "notAuthor": False,
                   "provider_id": "44444444-4444-4444-4444-444444444444", "provider_name": "neo4j_query",
                   "provider_type": "plugin", "tool_label": "neo4j_query", "tool_name": "neo4j_query",
                   "tool_parameters": {}, "type": "plugin"}]
    # ★ W20：真机 name 恒等于声明的 display_name（夹具与真机同形）；id 取自声明 uuid。
    live["apps"]["cc-kg-agent"] = {"id": "aaaa", "name": "方案1·知识图谱+LLM（主对比组）",
                                   "mode": "agent-chat",
                                   "model_config": _fx_model_config(FIXTURE_PROMPT, kg_tools)}
    live["apps"]["cc-onto-agent"] = {"id": "bbbb", "name": "方案2·本体+LLM（主对比组）",
                                     "mode": "agent-chat",
                                     "model_config": _fx_model_config(FIXTURE_PROMPT, onto_tools)}
    live["apps"]["cc-kgt-agent"] = {"id": "cccc", "name": "方案3·知识图谱直引·门面版",
                                    "mode": "agent-chat",
                                    "model_config": _fx_model_config(FIXTURE_PROMPT, kgt_tools)}
    live["apps"]["cc-kgt-free-agent"] = {"id": "dddd", "name": "方案3·知识图谱直引·插件直连版",
                                        "mode": "agent-chat",
                                        "model_config": _fx_model_config(FIXTURE_PROMPT, free_tools)}
    if mutate:
        live = mutate(live)
    return live


def run_selftest(rep):
    """★ 离线自证：1 条正向对照 + 6 条任务指定负控 + 若干附加负控。

    每条负控都**真跑** `evaluate` 并断言「整体退出码或该判据结论真的变了」，
    只打印结论不算通过。
    """
    binding = fixture_binding()
    results = []

    def run(live):
        return evaluate(binding, live, only=None)

    def neg(name, mutate, expect_label, expect_verdict, only_label=None, expect_exit=None):
        live = fixture_live(binding, mutate)
        res = run(live)
        got_exit = res["exit_code"]
        got_v = res["checks"][expect_label]["verdict"] if expect_label in res["checks"] else None
        ok = True
        why = []
        if expect_verdict and got_v != expect_verdict:
            ok = False
            why.append("判据 %s 期望 %s 实得 %s" % (expect_label, expect_verdict, got_v))
        if expect_exit is not None and got_exit != expect_exit:
            ok = False
            why.append("退出码期望 %s 实得 %s" % (expect_exit, got_exit))
        # ★ 必须相对「正向对照」真的变了
        base = results[0][3]
        if expect_verdict and expect_verdict != PASS:
            if got_v == base["checks"][expect_label]["verdict"] and got_exit == base["exit_code"]:
                ok = False
                why.append("与正向对照的结论/退出码相同 ⇒ 负控未生效")
        results.append((name, ok, why, res))
        return ok

    # ---- 正向对照：完全合规 ⇒ 全 PASS / 退出码 0 ----
    live_ok = fixture_live(binding)
    res_ok = run(live_ok)
    results.append(("POS-1 正向对照（完全合规夹具）",
                    res_ok["overall"] == PASS and res_ok["exit_code"] == 0,
                    [""] if res_ok["overall"] == PASS else
                    ["期望 PASS/0 实得 %s/%s" % (res_ok["overall"], res_ok["exit_code"])], res_ok))

    # ---- P2 W20 新增的 F0.n 名称断言在合规夹具下必须逐条 PASS ----
    p2_subs = [s for s in res_ok["checks"]["F0"]["subchecks"] if s["id"].startswith("F0.n:")]
    results.append(("P2 F0.n「真机 name = 声明 display_name」在合规夹具下 %d 条全 PASS" % len(p2_subs),
                    len(p2_subs) == 4 and all(s["verdict"] == PASS for s in p2_subs),
                    [""] if (len(p2_subs) == 4 and all(s["verdict"] == PASS for s in p2_subs))
                    else ["F0.n 子判据 %d 条：%s" % (len(p2_subs),
                                                    [(s["id"], s["verdict"]) for s in p2_subs])],
                    res_ok))

    # ---- U1 W20 新增：resolve_app_rows 的 uuid 优先解析（不联网纯函数） ----
    def u1_items():
        # 真机在**改名之后**的形态：name 是中文 display_name，id 是声明 uuid。
        # cc-onto-agent 的 id 与声明 uuid 故意不一致（模拟 uuid 漂移/声明过期）⇒ 走名字兜底；
        # cc-kgt-free-agent 两条路都不命中 ⇒ missing。
        return [{"id": "aaaa", "name": "方案1·知识图谱+LLM（主对比组）"},
                {"id": "zzzz", "name": "cc-onto-agent"},
                {"id": "cccc", "name": "方案3·知识图谱直引·门面版"}]

    rows1, how1 = resolve_app_rows(binding, u1_items())
    results.append(("U1.a uuid 优先：改名后（name ≠ 机器键）仍按 uuid 命中",
                    how1.get("cc-kg-agent") == "uuid" and rows1.get("cc-kg-agent", {}).get("id") == "aaaa"
                    and how1.get("cc-kgt-agent") == "uuid",
                    ["how=%s rows=%s" % (how1, sorted(rows1))], res_ok))
    results.append(("U1.b uuid 未命中时才按名字兜底",
                    how1.get("cc-onto-agent") == "name" and rows1.get("cc-onto-agent", {}).get("id") == "zzzz",
                    ["how.cc-onto-agent=%s" % how1.get("cc-onto-agent")], res_ok))
    results.append(("U1.c 两条路都不命中 ⇒ missing（不静默当成已读）",
                    how1.get("cc-kgt-free-agent") == "missing"
                    and "cc-kgt-free-agent" not in rows1,
                    ["how.cc-kgt-free-agent=%s" % how1.get("cc-kgt-free-agent")], res_ok))
    old_hits = [k for k in binding["apps"] if any(it["name"] == k for it in u1_items())]
    results.append(("U1.d 对照：旧「按名字索引」解析在本例命中 %d 个（其中 1 个是 uuid 未命中后的兜底行），"
                    "uuid 优先解析命中 %d 个" % (len(old_hits), len(rows1)),
                    old_hits == ["cc-onto-agent"] and len(rows1) == 3,
                    ["旧=%s 新=%s" % (old_hits, sorted(rows1))], res_ok))

    # ---- U2 对照：真机**全部改名**（name = display_name）时，旧解析命中 0 个 App ----
    renamed_items = [{"id": "aaaa", "name": "方案1·知识图谱+LLM（主对比组）"},
                     {"id": "bbbb", "name": "方案2·本体+LLM（主对比组）"},
                     {"id": "cccc", "name": "方案3·知识图谱直引·门面版"},
                     {"id": "dddd", "name": "方案3·知识图谱直引·插件直连版"}]
    old_hits2 = [k for k in binding["apps"] if any(it["name"] == k for it in renamed_items)]
    rows2, how2 = resolve_app_rows(binding, renamed_items)
    results.append(("U2 改名后旧「按名字索引」命中 %d 个 App（静默少读）；"
                    "uuid 优先解析命中 %d 个且解析来源全为 uuid"
                    % (len(old_hits2), len(rows2)),
                    old_hits2 == [] and len(rows2) == 4
                    and sorted(set(how2.values())) == ["uuid"],
                    ["旧=%s 新=%s how=%s" % (old_hits2, sorted(rows2), how2)], res_ok))

    # ---- N19 真机 App 名漂移 ⇒ F0（F0.n）FAIL ----
    def m19(live):
        det = live["apps"]["cc-kgt-agent"]
        det["name"] = det["name"] + "（漂移）"
        return live
    neg("N19 真机 App 名漂移（name ≠ 声明 display_name）", m19, "F0", FAIL, expect_exit=1)

    # ---- N19b 真机 name 回退成机器键（改名前的静默少读场景）⇒ 仍按 uuid 读到、F0 FAIL ----
    def m19b(live):
        live["apps"]["cc-onto-agent"]["name"] = "cc-onto-agent"
        return live
    live19b = fixture_live(binding, m19b)
    res19b = run(live19b)
    results.append(("N19b 真机 name 回退为机器键（旧路径会静默少读）⇒ uuid 仍读到且 F0 FAIL",
                    res19b["checks"]["F0"]["verdict"] == FAIL and res19b["exit_code"] == 1
                    and "cc-onto-agent" in live19b["apps"],
                    ["F0=%s exit=%s apps=%s" % (res19b["checks"]["F0"]["verdict"],
                                                res19b["exit_code"], sorted(live19b["apps"]))],
                    res19b))

    # ---- N20 声明缺 display_name ⇒ F0 BLOCKED（fail-closed，不退化成 PASS）----
    b20 = fixture_binding()
    del b20["apps"]["cc-kgt-agent"]["display_name"]
    live20 = fixture_live(b20)
    res20 = evaluate(b20, live20, only=None)
    v20 = res20["checks"]["F0"]["verdict"]
    n20_bad = [s["id"] for s in res20["checks"]["F0"]["subchecks"]
               if s["id"].startswith("F0.n:") and s["verdict"] not in (BLOCKED, PASS)]
    n20_blocked = [s["id"] for s in res20["checks"]["F0"]["subchecks"]
                   if s["id"].startswith("F0.n:") and s["verdict"] == BLOCKED]
    results.append(("N20 声明缺 display_name ⇒ 该 App 的 F0.n BLOCKED、其余 3 条仍 PASS、整体 exit 2",
                    v20 == BLOCKED and res20["exit_code"] == 2 and not n20_bad
                    and n20_blocked == ["F0.n:cc-kgt-agent"],
                    ["F0=%s exit=%s BLOCKED=%s 意外=%s"
                     % (v20, res20["exit_code"], n20_blocked, n20_bad)], res20))

    # ---- N1 改一侧某工具描述一个字 ⇒ F0 FAIL ----
    def m1(live):
        e = live["providers"]["cc-onto-tools"]
        e["tools"][1]["summary"] = e["tools"][1]["summary"] + "（改）"
        e["tools"][1]["openapi"]["description"] = e["tools"][1]["summary"]
        return live
    neg("N1 一侧工具描述改一个字", m1, "F0", FAIL)

    # ---- N2 onto provider 的 server_url 端口改成 :8301 ⇒ 静默失效防线 FAIL ----
    def m2(live):
        e = live["providers"]["cc-onto-tools"]
        for t in e["tools"]:
            t["server_url"] = t["server_url"].replace(":8302", ":8301")
        sc = json.loads(e["schema"])
        sc["servers"][0]["url"] = "http://192.0.2.10:8301"
        e["schema"] = json.dumps(sc, ensure_ascii=False)
        return live
    neg("N2 onto server_url 端口改 :8301", m2, "F0", FAIL, expect_exit=1)

    # ---- N2b 只改 tools[].server_url（不动 schema）⇒ 由静默失效防线 F0.f 抓住 ----
    #   这条把「静默失效防线」与 F0 的逐字比较腿**隔离**开：F0.a–F0.e 与 F0.g 必须仍是 PASS
    #   （F0.d 保持 PASS ⇒ schema 腿与端口腿互不依赖）；若 F0.f 失效，剩余防线是 F0.h
    #   （server_url 落到声明 base 之外 ⇒ path 无法剥离 ⇒ 投影 sha 不等），即**双重防线**。
    def m2b(live):
        e = live["providers"]["cc-onto-tools"]
        for t in e["tools"]:
            t["server_url"] = t["server_url"].replace(":8302", ":8301")
        return live
    live2b = fixture_live(binding, m2b)
    res2b = run(live2b)
    tripped = sorted(s["id"] for lab in res2b["labels"]
                     for s in res2b["checks"][lab]["subchecks"] if s["verdict"] != PASS)
    results.append(("N2b 仅改 server_url（schema 不动）⇒ 端口腿 F0.f 抓到、schema 腿 F0.d 保持 PASS",
                    tripped == ["F0.f", "F0.h"] and res2b["exit_code"] == 1,
                    ["期望非 PASS 子判据=['F0.f','F0.h'] 实得 %s exit=%s" % (tripped, res2b["exit_code"])]
                    if tripped != ["F0.f", "F0.h"] else [""], res2b))

    # ---- N3 两个 App 的工具集合出现同一 provider_id ⇒ 静默失效防线 FAIL ----
    def m3(live):
        at = live["apps"]["cc-onto-agent"]["model_config"]["agent_mode"]["tools"]
        for t in at:
            t["provider_id"] = FX_IDS["cc-kg-tools"]
            t["provider_name"] = "cc-kg-tools"
        return live
    neg("N3 两 App 工具集合 provider_id 相交", m3, "F0", FAIL, expect_exit=1)

    # ---- N4 在两侧 pre_prompt 里各塞一个「图」字 ⇒ F1 FAIL（隔离禁用词扫描）----
    def m4(live):
        for a in ("cc-kg-agent", "cc-onto-agent"):
            mc = live["apps"][a]["model_config"]
            mc["pre_prompt"] = mc["pre_prompt"] + "\n注意：不要输出任何图。"
        return live
    neg("N4 两侧 pre_prompt 同塞「图」字（隔离禁用词扫描）", m4, "F1", FAIL, expect_exit=1)

    # ---- N4b 只在一侧 pre_prompt 塞「图」字 ⇒ F1 FAIL（隔离相等性判据）----
    def m4b(live):
        mc = live["apps"]["cc-onto-agent"]["model_config"]
        mc["pre_prompt"] = mc["pre_prompt"] + "\n注意：不要输出任何图。"
        return live
    neg("N4b 单侧 pre_prompt 塞「图」字（隔离相等性判据）", m4b, "F1", FAIL, expect_exit=1)

    # ---- N5 temperature 两侧不同 ⇒ F2 FAIL ----
    def m5(live):
        live["apps"]["cc-onto-agent"]["model_config"]["model"]["completion_params"]["temperature"] = 0.7
        return live
    neg("N5 两侧 temperature 不同（0.3 vs 0.7）", m5, "F2", FAIL, expect_exit=1)

    # ---- N6 出现 function_calling_type ⇒ F2 FAIL ----
    def m6(live):
        live["apps"]["cc-kg-agent"]["model_config"]["agent_mode"]["function_calling_type"] = "no_call"
        return live
    neg("N6 注入 function_calling_type", m6, "F2", FAIL, expect_exit=1)

    # ---- N7 strategy 改成 cot ⇒ F2 FAIL ----
    def m7(live):
        for a in ("cc-kg-agent", "cc-onto-agent"):
            live["apps"][a]["model_config"]["agent_mode"]["strategy"] = "cot"
        return live
    neg("N7 strategy 改为 cot（两侧同值，仅 F2.c 应抓）", m7, "F2", FAIL, expect_exit=1)

    # ---- N8 某工具 tool_parameters 非空 ⇒ F2 FAIL ----
    def m8(live):
        live["apps"]["cc-kg-agent"]["model_config"]["agent_mode"]["tools"][2]["tool_parameters"] = {"x": 1}
        return live
    neg("N8 工具 tool_parameters 非空", m8, "F2", FAIL, expect_exit=1)

    # ---- N9 某工具 enabled=false ⇒ F3 FAIL ----
    def m9(live):
        live["apps"]["cc-onto-agent"]["model_config"]["agent_mode"]["tools"][0]["enabled"] = False
        return live
    neg("N9 已绑工具 enabled=false", m9, "F3", FAIL, expect_exit=1)

    # ---- N10 max_iteration 两侧不同 ⇒ F3 FAIL ----
    def m10(live):
        live["apps"]["cc-onto-agent"]["model_config"]["agent_mode"]["max_iteration"] = 8
        return live
    neg("N10 max_iteration 两侧不同（40 vs 8）", m10, "F3", FAIL, expect_exit=1)

    # ---- N11 未使用字段两侧不同（dataset_configs）⇒ F4 FAIL ----
    def m11(live):
        live["apps"]["cc-onto-agent"]["model_config"]["dataset_configs"]["top_k"] = 9
        return live
    neg("N11 dataset_configs 两侧不同", m11, "F4", FAIL, expect_exit=1)

    # ---- N12 KG-T 主组混入 cc_query ⇒ F5 FAIL ----
    def m12(live):
        mc = live["apps"]["cc-kgt-agent"]["model_config"]
        mc["agent_mode"]["tools"].append(
            _fx_bound_tool("cc-kgt-tools", FX_IDS["cc-kgt-tools"], "cc_query"))
        return live
    neg("N12 KG-T 主组混入 cc_query（工具变 5 个）", m12, "F5", FAIL, expect_exit=1)

    # ---- N13 KG-T 主组 pre_prompt 塞「图」⇒ F5 FAIL ----
    def m13(live):
        mc = live["apps"]["cc-kgt-agent"]["model_config"]
        mc["pre_prompt"] = mc["pre_prompt"] + "\n先看全图再回答。"
        return live
    neg("N13 KG-T 主组 pre_prompt 塞「图」", m13, "F5", FAIL, expect_exit=1)

    # ---- N14 缺一个 provider ⇒ 该判据 BLOCKED 且退出码 2（★ BLOCKED ≠ FAIL）----
    def m14(live):
        live["providers"].pop("cc-onto-tools", None)
        return live
    neg("N14 缺 provider ⇒ BLOCKED(2) 而非 FAIL(1)", m14, "F0", BLOCKED, expect_exit=2)

    # ---- N18 provider 缺失时 F1 的扫描面缩小 ⇒ 必须 BLOCKED，绝不 PASS ----
    def m18(live):
        live["providers"].pop("cc-onto-tools", None)
        return live
    neg("N18 provider 缺失 ⇒ F1 扫描面不完整，必须 BLOCKED（不得把「没扫到」当「零命中」）",
        m18, "F1", BLOCKED, expect_exit=2)

    # ---- N15 两个 App 尚未创建 ⇒ F1–F4 BLOCKED(2)（真机当前态）----
    def m15(live):
        for a in ("cc-kg-agent", "cc-onto-agent", "cc-kgt-agent", "cc-kgt-free-agent"):
            live["apps"].pop(a, None)
        return live
    neg("N15 App 全部未创建 ⇒ BLOCKED(2)", m15, "F1", BLOCKED, expect_exit=2)

    # ---- N16 登录失败 / 凭据缺失 ⇒ BLOCKED(2) ----
    def m16(live):
        live["login_ok"] = False
        live["providers"] = {}
        live["apps"] = {}
        return live
    neg("N16 未登录 / 凭据缺失 ⇒ BLOCKED(2)", m16, "F0", BLOCKED, expect_exit=2)

    # ---- N17 binding 声明与项目不变量冲突（carrier onto 写 :8301）⇒ F0 FAIL ----
    b2 = fixture_binding()
    b2["carriers"]["onto"]["service_base"] = "http://192.0.2.10:8301"
    b2["providers"][1]["base"] = "http://192.0.2.10:8301"
    b2["providers"][1]["port"] = 8301
    live17 = fixture_live(b2)
    res17 = evaluate(b2, live17)
    results.append(("N17 binding 声明端口与不变量冲突（onto→:8301）",
                    res17["checks"]["F0"]["verdict"] == FAIL and res17["exit_code"] == 1,
                    ["F0=%s exit=%s" % (res17["checks"]["F0"]["verdict"], res17["exit_code"])]
                    if res17["checks"]["F0"]["verdict"] != FAIL else [""], res17))

    # ---- META 退出码映射 ----
    results.append(("META 退出码映射 PASS/FAIL/BLOCKED → 0/1/2",
                    EXIT_FOR == {PASS: 0, FAIL: 1, BLOCKED: 2} and RANK[BLOCKED] > RANK[FAIL] > RANK[PASS],
                    [""], None))

    # ---- META-2 只读硬闸：任何非 GET（除会话登录）在任何网络请求之前被拒 ----
    #   base 指向不可达的 127.0.0.1:9：若硬闸失效，这里会拿到「网络错误」而不是异常，
    #   从而暴露「真的发了请求」。
    try:
        c = Console("http://127.0.0.1:9")
        notrejected = []
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD"):
            try:
                c.call(m, "/console/api/apps/x", {"a": 1})
                notrejected.append(m)  # 走到这里说明没抛异常 ⇒ 未被拒
            except ReadOnlyViolation:
                pass
        # 登录 POST 必须放行到「发请求」阶段（本地不可达 ⇒ 返回网络错误，而不是 ReadOnlyViolation）
        login_allowed = True
        try:
            c.call("POST", LOGIN_PATH, {})
        except ReadOnlyViolation:
            login_allowed = False
        # GET 必须放行（同样只到网络错误）
        get_allowed = True
        try:
            c.call("GET", "/console/api/workspaces")
        except ReadOnlyViolation:
            get_allowed = False
        results.append(("META-2 只读硬闸：POST(非login)/PUT/DELETE/PATCH/HEAD 全部被拒且未发请求；"
                        "GET 与 login POST 放行",
                        not notrejected and login_allowed and get_allowed,
                        (["未被拒：%s" % notrejected] if notrejected else []) +
                        ([] if login_allowed else ["login POST 被硬闸误拦"]) +
                        ([] if get_allowed else ["GET 被硬闸误拦"]), None))
    except Exception as ex:  # noqa: BLE001
        results.append(("META-2 只读硬闸", False, ["探测自身异常：%r" % ex], None))

    # ---- META-3 binding 驱动：把 provider/app 名全部改名，判据仍应 PASS ----
    #   证明判据以 binding 为准，而不是把 cc-* 名字硬编码进判据。
    try:
        ren = {"cc-kg-tools": "zz-a-tools", "cc-onto-tools": "zz-b-tools", "cc-kgt-tools": "zz-c-tools",
               "cc-kg-agent": "zz-a-agent", "cc-onto-agent": "zz-b-agent",
               "cc-kgt-agent": "zz-c-agent", "cc-kgt-free-agent": "zz-free-agent"}

        def _rename(obj):
            s = json.dumps(obj, ensure_ascii=False)
            for o, n in ren.items():
                s = s.replace(o, n)
            return json.loads(s)

        b3 = _rename(fixture_binding())
        live3 = _rename(fixture_live(binding))
        res3 = evaluate(b3, live3)
        results.append(("META-3 binding 驱动（provider/app 全部改名后仍 PASS ⇒ 判据未硬编码名字）",
                        res3["overall"] == PASS and res3["exit_code"] == 0,
                        ["期望 PASS/0 实得 %s/%s" % (res3["overall"], res3["exit_code"])]
                        if res3["overall"] != PASS else [""], res3))
    except Exception as ex:  # noqa: BLE001
        results.append(("META-3 binding 驱动", False, ["探测自身异常：%r" % ex], None))

    rep.line("=" * 78)
    rep.line("check_fairness.py --selftest（离线，不联网）")
    rep.line("=" * 78)
    bad = 0
    for name, ok, why, res in results:
        tag = "OK  " if ok else "BAD "
        extra = "" if ok else "  ← " + "; ".join(x for x in why if x)
        if res is not None:
            extra += "   [overall=%s exit=%d | %s]" % (
                res["overall"], res["exit_code"],
                " ".join("%s=%s" % (k, v) for k, v in
                         ((lab, res["checks"][lab]["verdict"]) for lab in res["labels"])))
        rep.line("[%s] %s%s" % (tag, name, extra))
        if not ok:
            bad += 1
    rep.line("-" * 78)
    rep.line("自检用例 %d 条，BAD=%d" % (len(results), bad))
    rep.line("★ 自检结论: %s（退出码 %d）" % ("PASS" if bad == 0 else "FAIL", 0 if bad == 0 else 1))
    rep.line("=" * 78)
    return 0 if bad == 0 else 1


# ===========================================================================
# main
# ===========================================================================
def main(argv=None):
    try:  # ★ 必须在 argparse 之前：否则 --help / 报错文本会按控制台代码页编码而乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="M4 公平性门禁 F0–F5（只读；唯一 POST = 会话登录）")
    ap.add_argument("--binding", default=str(DEFAULT_BINDING), help="binding.yaml 路径（唯一事实源）")
    ap.add_argument("--env", default=str(DEFAULT_ENV), help="凭据 .env 路径")
    ap.add_argument("--json", dest="json_out", default=None, help="证据 JSON 输出路径")
    ap.add_argument("--tee", dest="tee_out", default=None, help="控制台原文另存（UTF-8/LF）")
    ap.add_argument("--selftest", action="store_true", help="离线自证（不联网）")
    ap.add_argument("--only", default=None, help="只跑指定判据，逗号分隔，如 F0,F1")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP 超时秒（默认 30）")
    args = ap.parse_args(argv)

    rep = Reporter(args.tee_out)
    try:
        if args.selftest:
            rc = run_selftest(rep)
            if args.json_out:
                with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump({"mode": "selftest", "version": VERSION, "exit_code": rc,
                               "generated_at_utc": now_utc(),
                               "script_fingerprint": file_fingerprint(Path(__file__))},
                              fh, ensure_ascii=False, indent=2)
            return rc

        binding_path = Path(args.binding)
        if not binding_path.is_absolute():
            binding_path = (REPO_ROOT / binding_path).resolve()
        if not binding_path.exists():
            rep.line("[BLOCKED] binding.yaml 不存在：%s（前置条件缺失）" % binding_path)
            rep.line("★ 整体: BLOCKED（退出码 2）")
            return 2
        binding = load_binding(binding_path)
        env_path = Path(args.env)
        live = collect_live(binding, env_path, timeout=args.timeout)
        only = [x.strip().upper() for x in (args.only or "").split(",") if x.strip()] or None
        if only:
            unknown = [x for x in only if x not in CHECK_FUNCS]
            if unknown:
                rep.line("[FAIL] --only 含未知判据：%s（合法：%s）" % (unknown, ALL_LABELS))
                return 1
        if not live.get("login_ok"):
            # 前置条件缺失 ⇒ 全部判据 BLOCKED（绝不写成 PASS）
            checks = {}
            for lab in (only or ALL_LABELS):
                checks[lab] = {"verdict": BLOCKED, "subchecks": [
                    sub("%s.0" % lab, "前置条件（登录/凭据/网络）", BLOCKED,
                        "无法取得回读面：%s" % ("; ".join(live.get("notes") or []) or "未知原因"))]}
            res = {"checks": checks, "counts": {PASS: 0, FAIL: 0, BLOCKED: len(checks)},
                   "overall": BLOCKED, "exit_code": 2, "labels": list(checks)}
        else:
            res = evaluate(binding, live, only=only)
        meta = {"mode": "online", "script": file_fingerprint(Path(__file__)),
                "binding": file_fingerprint(binding_path), "env_path": str(env_path)}
        print_report(rep, res, binding, live, meta)
        uncovered = uncovered_report(binding, live, res)
        rep.line("未覆盖字段/判据（%s）:" % len(uncovered["not_checked"]))
        for x in uncovered["not_checked"]:
            rep.line("  - %s" % x)
        if uncovered["model_config_keys_uncovered"]:
            rep.line("model_config 未被 F1–F4 覆盖的键（实测）: %s"
                     % uncovered["model_config_keys_uncovered"])
        rep.line("model_config 未被 F1–F4 覆盖的键（按 24 键模板预期）: %s"
                 % uncovered["model_config_keys_uncovered_expected"])
        if args.json_out:
            payload = {
                "tool": "check_fairness.py", "version": VERSION, "mode": "online",
                "generated_at_utc": now_utc(),
                "script_fingerprint": meta["script"],
                "binding": meta["binding"], "env_path": meta["env_path"],
                "live": {k: v for k, v in live.items() if k != "apps"} |
                        {"app_model_config_sha256": {a: sha256_text(stable_json(
                            (live["apps"][a].get("model_config") or {}))) for a in live.get("apps", {})}},
                "live_app_model_config": {a: (live["apps"][a].get("model_config") or {})
                                          for a in live.get("apps", {})},
                "checks": res["checks"], "counts": res["counts"],
                "overall": res["overall"], "exit_code": res["exit_code"],
                "uncovered": uncovered,
            }
            with open(args.json_out, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            rep.line("证据 JSON 已写：%s" % args.json_out)
        return res["exit_code"]
    finally:
        rep.close()


if __name__ == "__main__":
    sys.exit(main())
