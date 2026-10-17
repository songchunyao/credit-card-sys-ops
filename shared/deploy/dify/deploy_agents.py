#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4.2 / W2 + M4.7 / W3 · 在真机 Dify 上新增主对比组与 KG-T 家族 App，并回读取证。

用法：
    python deploy_agents.py --check        # 只读：核对声明面 / live provider / 已建 App 的回读面（默认动作）
    python deploy_agents.py --apply        # 按需创建（只新增，绝不删除/改名）并整体替换 model-config
    python deploy_agents.py --apply --write-binding   # 追加：把真实 uuid 回写 binding.yaml 的 apps 段
    python deploy_agents.py --selftest     # 离线自检：FakeTransport 端到端 + 负控（不需 Dify、不需网络）

默认目标 = binding.fairness.compare_pairs[0]（主对比组两个 App，W2 口径）。
M4.7/W3 的 KG-T 家族两个 App 显式指定：
    python deploy_agents.py --check --apps cc-kgt-agent,cc-kgt-free-agent
    python deploy_agents.py --apply --write-binding --apps cc-kgt-agent,cc-kgt-free-agent

对应用户口径的硬约束（本文件逐条实现，selftest 逐条负控）：
  * 建 App 只发 6 个字段 name/description/mode/icon_type/icon/icon_background；
  * mode 必须 "agent-chat"（R-41：非 agent-chat 的 GET /apps/{uuid} 返回空 model_config，
    会让公平性门禁永久 BLOCKED，而非 FAIL）；
  * model-config 是整体替换语义：结构范本逐字取自既有 App 2c1a708e-… 的 model_config 回读，
    不凭记忆拼键；
  * model / agent_mode 逐字按 R-37；每个工具 tool_parameters 一律 {}；
  * pre_prompt 逐字节取自声明面指定的文件（不手工粘贴）：主对比组 = shared/deploy/prompts/agent_system.md；
    KG-T 家族 = shared/model/kgt-prompt-variants.yaml 里 variants.<变体>.prompt_file，
    并逐字节校验该文件 bytes/lf_count/sha256；
  * 工具条目 provider 面按**真机回读**解析：kind=api 的 provider_id=uuid、provider_type/type="api"；
    kind=plugin 的 provider_id=真机 builtin provider 的 id（斜杠形态）、provider_type/type=该 provider 的 type；
  * 绝不注入 function_calling_type；
  * 绝不打印口令：凭据只经 common.console_client 从既有候选顺序解析。
"""

from __future__ import annotations

import argparse
import re
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import common  # noqa: E402

BINDING_PATH = HERE / "binding.yaml"
PROMPT_PATH = REPO_ROOT / "shared" / "deploy" / "prompts" / "agent_system.md"

# 冻结基准（与任务书给定的 bytes/LF/sha256 一致；不符即拒绝，防止被悄悄改写）
# ★ R-63（2026-10-16，用户裁定 m01006/m01035）：基准提示词追加 §6「最终答复的排版」——
#   6461 B / 91 LF / 60fab0b5… → 7710 B / 105 LF / 5941c0f9…
#   依据：真机 528 条消息里 452 条答复为空，其中 293 条终答步以「前置散文」开头、56 条把结论包进
#   ReAct JSON 信封 ⇒ Dify 的 ReAct 解析器取不到 action ⇒ `final_answer=""`（答复丢失/乱码）。
#   ★ 首版 §6 未写明「本节只约束排版、不豁免取证」⇒ 真机 16 次调用全部退化为 1 轮 0 工具调用
#   （纯编造），已改写并加前置条件。
#   ★ 二次修订（同日）：§6 的取证句去掉了工具名（改为「工具选择遵循 §2」），以便**逐字相同**地
#   追加到 `base_agent_system.md` 与两份 KG-T 变体上 —— 变体的冻结差异条数与重建必须保持
#   （`f5_kgt_gate.py` B2/B3/B4、`contract_gates.py` X18 同步重登记指纹）。
#   四条主对比臂成对同步应用（F2.b/F3.c 只要求对照对内部逐字一致）。
#   ★ 三次修订（2026-10-16，R-64）：端到端 150 次调用实测发现 27 次「零工具调用直接作答」，
#   且这些答复**编造了工具返回**（违反 C5 证据可追溯）。根因：§6 只讲最终答复排版，未区分
#   「中间输出＝工具调用」与「最终答复＝四节正文」，模型因而在第 1 轮就输出结论。
#   §6 改写为《输出形态（硬性）》，新增两条硬性：中间输出必须且只是一个工具调用；
#   **在至少取得一次工具返回之前不得输出最终答复**。
#   7710 B / 105 LF / 5941c0f9… → 7982 B / 106 LF / 8943ee03…
PROMPT_BYTES = 7982
PROMPT_LF = 106
PROMPT_SHA256 = "8943ee034201ab43b0df025415eab74df14b3cf6daaeb46363a882daa10d6f7f"

# 结构范本 App（agent-chat、24 键 model_config）。
# ★ R-59：原实现把 uuid **钉死**为旧部署的 `2c1a708e-…`；该 App 在现网重建后 uuid 已变
#   （现网 `cc-template-agent` = `36e0ee3c-4fb1-4ecf-911a-31d9f3a83f2c`）⇒ `--check/--apply`
#   会在「模板 App 的 model_config 不可用」上硬失败。改为**按名解析**、历史 uuid 仅作兜底。
# ★ 合并（packaging 分支，2026-10-12）：兜底 uuid 允许环境变量覆盖。理由是 fresh Dify 上
#   一个 App 都没有（也就没有 `cc-template-agent` 可解析），此时按名解析失败、只能落到兜底
#   uuid，而报错只是 `HTTP GET /console/api/apps/<uuid> 404`，完全看不出"你还缺一个模板 App"。
#   ⇒ 设 `CC_DIFY_TEMPLATE_APP=<App id>` 指向新建的 agent-chat 应用即可。
#   实测（部署宿主 fresh Dify）：新建的 agent-chat App 会自动带上工作区默认模型，
#   回读 model_config.model 即 {..., "name": "deepseek_v4"}，正合要求。默认值保持原值 ⇒ 向后兼容。
TEMPLATE_APP_ID = os.environ.get("CC_DIFY_TEMPLATE_APP",
                                 "2c1a708e-60ba-44e1-87c8-de3ab830fb40")
TEMPLATE_APP_NAME = "cc-template-agent"

CREATE_FIELDS = ("name", "description", "mode", "icon_type", "icon", "icon_background")
APP_MODE_AGENT_CHAT = "agent-chat"
METADATA_KEYS = ("created_at", "created_by", "updated_at", "updated_by")
TOOL_ENTITY_KEYS = (
    "enabled", "isDeleted", "notAuthor", "provider_id", "provider_name",
    "provider_type", "tool_label", "tool_name", "tool_parameters", "type",
)
FORBIDDEN_KEY_SUFFIX = "function_calling_type"
MAIN_COMPARISON_TOOL_COUNT = 4
# binding.apps 段的 status：本作业（W2）把主对比组两个 App 真正建成并绑好工具后的如实取值。
# 只在回读校验通过时才写回，避免把「未验证」登记成「已建成」。
W2_STATUS = "created_and_bound_in_w2"
# M4.7 / W3：KG-T 家族两个 App 真正建成并绑好工具后的如实取值（同样只在回读通过时写回）。
W3_STATUS = "created_and_bound_in_w3"
# ★ R-52：R 臂（cc-kg-r-agent / cc-onto-r-agent）真正建成并绑好工具后的如实取值。
R52_STATUS = "created_and_bound_in_r52"
# ★ R-55：BASE 臂（cc-base-agent，基线组/无知识库）真正建成并绑好工具后的如实取值。
R55_STATUS = "created_and_bound_in_r55"
# ★ U-N 裁定 = A：KG-T-dyn（补充组）真正建成并绑好工具后的如实取值。
UN_A_STATUS = "created_and_bound_in_u_n_a"

# --- M4.7 / W3：KG-T 家族 prompt 与 provider 面 ------------------------------
# KG-T / KG-T-free 的 pre_prompt 不由 binding.yaml 指定，而由 shared/model/kgt-prompt-variants.yaml
# 的 variants.<变体>.prompt_file + bytes/lf_count/sha256 指定（该文件即 prompt 的单一事实源）。
PROMPT_VARIANTS_PATH = REPO_ROOT / "shared" / "model" / "kgt-prompt-variants.yaml"
# binding.counts 里 载体(carrier) → 权威工具数键 的映射
CARRIER_COUNT_KEY = {
    "kg": "setb_tools",
    "onto": "setb_tools",
    "kgt": "kgt_main_tools",
    "kgt-free": "kgt_free_tools",
    "kgt-dyn": "kgt_dyn_tools",          # ★ U-N 裁定 = A（2026-10-08）
    "live-facts": "livefacts_tools",
}
# kind=plugin 的工具条目 provider_type/type 取值。取真机 builtin provider 的 type 字段（= "builtin"），
# 与 Dify 1.17 前端 zod 枚举 provider_type ∈ {api,app,builtin,dataset-retrieval,mcp,plugin,workflow} 一致。
# 残留不确定性：本租户内没有任何既有 builtin/plugin 工具绑定可参照。
PLUGIN_PROVIDER_TYPE = "builtin"

ICON_TYPE = "emoji"
ICON = "\U0001f527"  # 🔧
ICON_BACKGROUND = "#3B82F6"
# ★ R-59（2026-10-13，用户裁定）：**删除**原 `APP_DESCRIPTION` 常量。
#   原实现把**同一句**描述写进**每一个** App —— 用户实测：现网 Dify 上 `cc-kg-agent` 与
#   `cc-onto-agent` 的描述**逐字相同**。那既说不清各臂差异，也让「描述」这一展示面失真。
#   现改为**逐 App 取声明面** `binding.apps[<key>].display_name / .display_description`
#   （单一事实源；本文件不再内联任何 App 描述文本），并由 selftest 加**反恒真**断言：
#   ① 每个 App 的两项身份字段非空且**两两不同**；② 建/改载荷逐字等于声明值。
APP_DESCRIPTION_MAX = 400      # Dify `UpdateAppPayload.description` 的 max_length（容器内源码实测）

# R-37：model 块逐字
R37_MODEL = {
    "provider": "langgenius/openai_api_compatible/openai_api_compatible",
    "name": "deepseek_v4",
    "mode": "chat",
    "completion_params": {"temperature": 0.3, "stop": []},
}
# R-37：agent_mode 的固定部分（tools 由声明面生成）
# ★ R-60（2026-10-15，用户裁定）：agent_mode.max_iteration 由 20 上调到 40。
#   根因（真机实测）：R 臂在 20 轮上限下存在「跑满上限 ⇒ 无最终答案」的失败 ⇒ 根因准确率被压低。
#   公平性不受影响的理由：四条主对比臂（KG-Q / ONTO-Q / KG-R / ONTO-R）**同时**改同一值，
#   因此 check_fairness 的 F2.b / F3.c「两侧在对照对内部逐字相同」仍由构造保证。
R37_AGENT_CORE = {"enabled": True, "strategy": "react", "max_iteration": 40, "prompt": None}

# 本地复刻 F4 的已取证结构性默认值（判据以 shared/deploy/check_fairness.py 为准，此处只做写前预检）
F4_DEFAULT_ASSERTED = (
    ("prompt_type", "simple"),
    ("external_data_tools", []),
    ("agent_mode.prompt", None),
    ("file_upload.enabled", False),
    ("sensitive_word_avoidance.enabled", False),
    ("annotation_reply.enabled", False),
    ("more_like_this.enabled", False),
    ("opening_statement", ""),
    ("suggested_questions", []),
    ("speech_to_text.enabled", False),
    ("text_to_speech.enabled", False),
    ("dataset_query_variable", ""),
    ("dataset_configs.datasets.datasets", []),
)


class Fail(RuntimeError):
    """不可继续的硬错误。"""


class _Missing:
    pass


MISSING = _Missing()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dig(obj, dotpath, default=MISSING):
    cur = obj
    for part in dotpath.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def same_json(a, b) -> bool:
    return common.stable_json(a) == common.stable_json(b)


# ---------------------------------------------------------------------------
# 声明面
# ---------------------------------------------------------------------------
def load_declaration(binding_path: Path | str = BINDING_PATH) -> dict:
    p = Path(binding_path)
    if not p.exists():
        raise Fail("找不到 binding.yaml：%s" % p)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise Fail("binding.yaml 不是 mapping")
    return data


def prompt_text() -> str:
    """逐字节读取冻结提示词正文（解码为 str，供 JSON 传输）。"""
    if not PROMPT_PATH.exists():
        raise Fail("找不到提示词正文：%s" % PROMPT_PATH)
    return PROMPT_PATH.read_bytes().decode("utf-8")


def verify_prompt_file() -> list:
    problems = []
    if not PROMPT_PATH.exists():
        return ["提示词正文缺失：%s" % PROMPT_PATH]
    raw = PROMPT_PATH.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        problems.append("提示词正文带 UTF-8 BOM（应为无 BOM）")
    if b"\r\n" in raw:
        problems.append("提示词正文含 CRLF（应为纯 LF）")
    if len(raw) != PROMPT_BYTES:
        problems.append("提示词正文 bytes=%d，期望 %d" % (len(raw), PROMPT_BYTES))
    if raw.count(b"\n") != PROMPT_LF:
        problems.append("提示词正文 LF=%d，期望 %d" % (raw.count(b"\n"), PROMPT_LF))
    sha = common.sha256_bytes(raw)
    if sha != PROMPT_SHA256:
        problems.append("提示词正文 sha256=%s，期望 %s" % (sha, PROMPT_SHA256))
    return problems


# --- M4.7 / W3：prompt 变体解析（KG-T 家族） --------------------------------
def rel_repo_path(p) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (REPO_ROOT / q)


def load_prompt_variants(path=None) -> dict:
    p = Path(path or PROMPT_VARIANTS_PATH)
    if not p.exists():
        raise Fail("找不到 prompt 变体声明面：%s" % p)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise Fail("%s 不是 mapping" % p)
    return data


def derive_variant_text(vspec: dict) -> str:
    """按 `derived_from` + `derived_diff` 在**内存里**派生变体正文（**不落文件**）。

    ★ 为什么不落文件：`f5_kgt_gate.py` 的 E 段明确断言 KG-T-dyn「**无独立文件**」
      （以 derived_from + 恰 1 条派生差异机械定义其全文）—— 那是 worklist §E.2 的终裁。
      故给它的 App 供正文只能走派生，**不得**在 `shared/deploy/prompts/` 下新建文件。

    派生规则（与 F5 的 `declared_ops` 同一约定）：`kind=add` ⇒ 把 `variant_text`
      插到 `parent_file` 的第 `parent_line` 行**之后**（`parent_line=0` 表示插到文件头）。
    """
    v = load_prompt_variants()
    parent_name = vspec.get("derived_from")
    parent = ((v.get("variants") or {}).get(parent_name) or {})
    pfile = parent.get("prompt_file")
    if not pfile:
        raise Fail("派生变体 %r 的 derived_from=%r 本身没有 prompt_file，无法派生"
                   % (vspec.get("dify_app"), parent_name))
    lines = (REPO_ROOT / str(pfile)).read_bytes().decode("utf-8").split("\n")
    for d in sorted(vspec.get("derived_diff") or [],
                    key=lambda x: int(x.get("parent_line") or 0), reverse=True):
        if d.get("kind") != "add":
            raise Fail("派生差异 %s 的 kind=%r 暂不支持（本实现只做 add）"
                       % (d.get("id"), d.get("kind")))
        vt = d.get("variant_text")
        newlines = [vt] if isinstance(vt, str) else list(vt or [])
        for x in reversed(newlines):
            lines.insert(int(d.get("parent_line") or 0), x)
    return "\n".join(lines)


def prompt_specs(binding=None) -> dict:
    """app_name → spec{variant,path,bytes,lf,sha256}。

    prompt 的单一事实源 = shared/model/kgt-prompt-variants.yaml：
    baseline.prompt_file 供主对比组两个 App；variants.<变体>.dify_app 指向 KG-T 家族各自 App。
    KG-T-dyn 本轮没有 prompt_file（明确推迟），故不会出现在结果里。
    """
    v = load_prompt_variants()
    out = {}
    base = v.get("baseline") or {}
    if base.get("prompt_file"):
        out["__baseline__"] = {
            "variant": "baseline",
            "path": rel_repo_path(base["prompt_file"]),
            "bytes": base.get("bytes"),
            "lf": base.get("lf_count"),
            "sha256": base.get("sha256"),
            "declared_tools": base.get("expected_tools"),
        }
    for name, spec in (v.get("variants") or {}).items():
        if not isinstance(spec, dict):
            continue
        key = spec.get("dify_app") or name
        if spec.get("prompt_file"):
            out[key] = {
                "variant": name,
                "path": rel_repo_path(spec["prompt_file"]),
                "bytes": spec.get("bytes"),
                "lf": spec.get("lf_count"),
                "sha256": spec.get("sha256"),
                "declared_tools": spec.get("expected_tools"),
            }
        elif spec.get("derived_from") and spec.get("derived_diff"):
            # ★ U-N 裁定 = A：KG-T-dyn 的正文在**内存里派生**（F5 断言它「无独立文件」）
            txt = derive_variant_text(spec)
            out[key] = {
                "variant": name,
                "path": None,
                "derived": True,
                "text": txt,
                "bytes": spec.get("bytes"),
                "lf": spec.get("lf_count"),
                "sha256": spec.get("sha256"),
                "declared_tools": spec.get("expected_tools"),
            }
    b = out.get("__baseline__")
    if b and binding:
        for app in comparison_arms(binding):
            out.setdefault(app, b)
    return out


def prompt_spec_for(app_name: str, binding=None) -> dict:
    spec = prompt_specs(binding).get(app_name)
    if not spec:
        raise Fail("声明面里没有 App %s 的 prompt 规格"
                   "（%s：baseline.prompt_file / variants.*.prompt_file）" % (app_name, PROMPT_VARIANTS_PATH.name))
    return spec


#: ★ A′ 第 ③ 条门禁（2026-10-15，用户裁定）：各臂的**首选入口** —— 提示词必须提及它。
#:   出处：参考项目 `dify/deploy_agent_v3.py:266-268` 的同款检查（「必须提及 onto_diagnose」+
#:   「不得仍要求先调 onto_guide」）。本项目对应 `cc_cause`（它已把候选 + 聚焦证据 + 建议
#:   一次串完，是「一次调用即结论」的载体）。
PRIMARY_ENTRY_TOOL = {
    "cc-kg-agent": "cc_cause",
    "cc-onto-agent": "cc_cause",
    "cc-kg-r-agent": "cc_cause",
    "cc-onto-r-agent": "cc_cause",
}


def all_declared_tool_names(binding) -> set:
    """全仓**声明过的**工具名（由 `providers[].tools` 汇总，声明驱动、不硬编码）。"""
    out: set = set()
    for p in binding.get("providers") or []:
        out |= set(p.get("tools") or [])
    return out


def verify_prompt_file_spec(spec: dict) -> list:
    """通用版 verify_prompt_file：按 spec 的 bytes/lf/sha256 逐字节校验。"""
    problems = []
    path = spec.get("path")
    if path is None:
        # ★ U-N 裁定 = A：派生正文（无文件）。只做可判的检查：BOM / CRLF / 声明值。
        raw = (spec.get("text") or "").encode("utf-8")
        label = "派生正文(%s)" % spec.get("variant")
        if raw[:3] == b"\xef\xbb\xbf":
            problems.append("%s 带 UTF-8 BOM（应为无 BOM）" % label)
        if b"\r\n" in raw:
            problems.append("%s 含 CRLF（应为纯 LF）" % label)
        if spec.get("bytes") is not None and len(raw) != spec["bytes"]:
            problems.append("%s bytes=%d，声明 %s" % (label, len(raw), spec["bytes"]))
        if spec.get("lf") is not None and raw.count(b"\n") != spec["lf"]:
            problems.append("%s LF=%d，声明 %s" % (label, raw.count(b"\n"), spec["lf"]))
        if spec.get("sha256") and common.sha256_bytes(raw) != spec["sha256"]:
            problems.append("%s sha256=%s，声明 %s"
                            % (label, common.sha256_bytes(raw), spec["sha256"]))
        return problems
    if not path.exists():
        return ["提示词正文缺失：%s" % path]
    raw = path.read_bytes()
    if raw[:3] == b"\xef\xbb\xbf":
        problems.append("%s 带 UTF-8 BOM（应为无 BOM）" % path.name)
    if b"\r\n" in raw:
        problems.append("%s 含 CRLF（应为纯 LF）" % path.name)
    if spec.get("bytes") is not None and len(raw) != spec["bytes"]:
        problems.append("%s bytes=%d，声明 %s" % (path.name, len(raw), spec["bytes"]))
    if spec.get("lf") is not None and raw.count(b"\n") != spec["lf"]:
        problems.append("%s LF=%d，声明 %s" % (path.name, raw.count(b"\n"), spec["lf"]))
    sha = common.sha256_bytes(raw)
    if spec.get("sha256") and sha != spec["sha256"]:
        problems.append("%s sha256=%s，声明 %s" % (path.name, sha, spec["sha256"]))
    return problems


def prompt_path_label(spec: dict) -> str:
    """提示词落点标签（供日志/报告使用）。

    ★ U-N 裁定 = A：派生变体**没有文件**（F5 的 E 段断言「无独立文件」）⇒ `path` 为 None，
      此时如实标注「派生·无文件·来自 <该变体>」，**不得**伪造一个文件路径。
    """
    p = spec.get("path")
    if p is None:
        return "（派生·无文件·来自 %s）" % spec.get("variant")
    return str(p.relative_to(REPO_ROOT))


def prompt_text_of(spec: dict) -> str:
    if spec.get("text") is not None:          # ★ 派生变体（无文件）
        return spec["text"]
    if not spec["path"].exists():
        raise Fail("找不到提示词正文：%s" % spec["path"])
    return spec["path"].read_bytes().decode("utf-8")


# --- M4.7 / W3：provider 写入面（api 走 uuid；plugin 走真机 builtin 面） ----
def provider_decls(binding) -> dict:
    """binding.providers → {name: decl}（列表与 mapping 两种写法都支持）。"""
    raw = binding.get("providers")
    out = {}
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict) and it.get("provider"):
                out[it["provider"]] = it
    elif isinstance(raw, dict):
        for k, it in raw.items():
            if isinstance(it, dict):
                d = dict(it)
                d.setdefault("provider", k)
                out[k] = d
    return out


def provider_kind(binding, name: str) -> str:
    return (provider_decls(binding).get(name) or {}).get("kind") or "api"


def find_provider_face(binding, provider_name: str, faces: dict) -> dict:
    """按声明面的 kind 解析真机 provider 面 → {id,type,name,plugin_id,tools}。"""
    decl = provider_decls(binding).get(provider_name) or {}
    kind = decl.get("kind") or "api"
    by_id = (faces or {}).get("by_id") or {}
    by_name = (faces or {}).get("by_name") or {}
    by_plugin_id = (faces or {}).get("by_plugin_id") or {}
    face = None
    if kind == "plugin":
        want = decl.get("plugin_id")
        if want:
            face = by_plugin_id.get(want) or by_id.get(want)
        if face is None and decl.get("plugin_unique_identifier"):
            for f in by_id.values():
                if f.get("plugin_unique_identifier") == decl["plugin_unique_identifier"]:
                    face = f
                    break
    else:
        face = by_name.get(provider_name) or by_id.get(provider_name)
    if not isinstance(face, dict) or not face.get("id"):
        raise Fail("live 中解析不到 provider %s 的面（kind=%s，App 无法绑定）" % (provider_name, kind))
    got = dict(face)
    got["kind"] = kind
    if kind == "plugin":
        got["write_type"] = got.get("type") or PLUGIN_PROVIDER_TYPE
        got["write_name"] = got.get("name") or got["id"]
    else:
        got["write_type"] = "api"
        got["write_name"] = provider_name
    return got


def comparison_pairs(binding) -> list:
    """全部对照对（★ R-52：Q 对照对 + R 对照对）。逐个校验形状，fail-closed。"""
    pairs = ((binding.get("fairness") or {}).get("compare_pairs") or [])
    if not pairs or not isinstance(pairs[0], list) or len(pairs[0]) != 2:
        raise Fail("binding.fairness.compare_pairs[0] 必须是长度 2 的列表")
    out = []
    for i, p in enumerate(pairs):
        if not isinstance(p, list) or len(p) != 2:
            raise Fail("binding.fairness.compare_pairs[%d] 必须是长度 2 的列表" % i)
        out.append(list(p))
    return out


def comparison_arms(binding) -> list:
    """全部对照对成员（去重、保持出现顺序）。F0–F4 对各对照对都必须成立。"""
    seen, out = set(), []
    for pair in comparison_pairs(binding):
        for a in pair:
            if a not in seen:
                seen.add(a)
                out.append(a)
    return out


def is_comparison_arm(binding, app_name: str) -> bool:
    return app_name in comparison_arms(binding)


def main_comparison_arms(binding) -> list:
    """第一个对照对（= Q 对照对）。保留原名：默认目标 App 与基准提示词沿用此口径。"""
    return list(comparison_pairs(binding)[0])


def target_apps(binding, names=None) -> list:
    """返回 [(app_name, cfg), …]；默认取主对比组的两个 App。"""
    apps = binding.get("apps") or {}
    ordered = list(names) if names else main_comparison_arms(binding)
    out = []
    for name in ordered:
        cfg = apps.get(name)
        if not isinstance(cfg, dict):
            raise Fail("binding.apps 缺少条目：%s" % name)
        if not cfg.get("provider"):
            raise Fail("binding.apps.%s 缺少 provider" % name)
        if not cfg.get("tools"):
            raise Fail("binding.apps.%s 缺少 tools" % name)
        out.append((name, cfg))
    return out


def declared_providers(binding, app_name: str, cfg: dict) -> list:
    """App 声明的 provider 集合（单一真源，fail-closed；S2c 多 provider 语义）。

    - `providers` 是权威集合；若同时保留历史单键 `provider`（smoke/e2e/check_fairness
      等禁改脚本仍按单键读），它必须逐字等于 `providers[0]`，否则硬失败（两个真源）。
    - 只有 `provider` ⇒ `[provider]`（主对比对两侧即此形）。
    - 主对比对 App 必须**恰好 1 个** provider：这是 F0/F1「两侧逐字一致、唯一差异是载体」
      的前提 ⇒ 在此单点断言，所有调用方都绕不过。
    """
    single = cfg.get("provider")
    many = cfg.get("providers")
    if many is None:
        if not single:
            raise Fail("binding.apps.%s 缺少 provider" % app_name)
        out = [single]
    else:
        if not isinstance(many, list) or not many:
            raise Fail("binding.apps.%s 的 providers 必须是非空列表" % app_name)
        out = []
        for n in many:
            if not isinstance(n, str) or not n:
                raise Fail("binding.apps.%s 的 providers 含非法项：%r" % (app_name, n))
            if n in out:
                raise Fail("binding.apps.%s 的 providers 有重复项：%s" % (app_name, n))
            out.append(n)
        if single is not None and single != out[0]:
            raise Fail("binding.apps.%s 同时声明 provider=%r 与 providers=%s 且不一致："
                       "providers 是权威集合，provider 必须是其首项" % (app_name, single, out))
    if is_comparison_arm(binding, app_name) and len(out) != 1:
        raise Fail("对照对 App %s 必须恰好声明 1 个 provider（现 %d 个：%s）"
                   % (app_name, len(out), out))
    return out


def provider_tool_names(decl: dict) -> list:
    """provider 声明里暴露的工具名：api 用 `tools`，plugin 用 `bind_tools`（两键并存）。"""
    names = list(decl.get("tools") or [])
    for n in (decl.get("bind_tools") or []):
        if n not in names:
            names.append(n)
    return names


def tool_provider_of(binding, app_name: str, cfg: dict, tool_name) -> str:
    """工具 → 该 App 声明集合中的**唯一** provider（纯声明驱动，不读真机）。"""
    provs = declared_providers(binding, app_name, cfg)
    decls = provider_decls(binding)
    owners = [p for p in provs if tool_name in provider_tool_names(decls.get(p) or {})]
    if len(owners) != 1:
        raise Fail("binding.apps.%s 的工具 %r 在声明集合 %s 中解析到 %d 个 provider"
                   "（必须恰好 1 个）" % (app_name, tool_name, provs, len(owners)))
    return owners[0]


def tool_faces(binding, app_name: str, cfg: dict, faces: dict) -> dict:
    """工具名 → (provider 名, 真机 provider 面)；逐工具解析（S2c 新语义）。"""
    out = {}
    for tool_name in cfg["tools"]:
        pname = tool_provider_of(binding, app_name, cfg, tool_name)
        out[tool_name] = (pname, find_provider_face(binding, pname, faces))
    return out


def tool_count_key(cfg: dict) -> str:
    """工具数判据的 counts 键：App 显式声明 `count_key` 优先（★ R-52：R 臂 = setb_tools_r），
    否则回落到载体映射。"""
    explicit = cfg.get("count_key")
    if explicit:
        return explicit
    carrier = cfg.get("carrier")
    return CARRIER_COUNT_KEY.get(carrier, "setb_tools")


def status_for(cfg: dict) -> str:
    """回写 binding.apps.<name>.status 的如实取值：
    KG-T 家族 = W3，R 臂（★ R-52）= R52，主对比组 = W2。"""
    if cfg.get("count_key") == "setb_tools_r":
        return R52_STATUS
    # ★ R-55：BASE 臂以 count_key=neutral_tools 声明（声明驱动，不按 App 名硬编码）
    if cfg.get("count_key") == "neutral_tools":
        return R55_STATUS
    # ★ U-N 裁定 = A：KG-T-dyn 以 count_key=kgt_dyn_tools 声明
    if cfg.get("count_key") == "kgt_dyn_tools":
        return UN_A_STATUS
    return W3_STATUS if cfg.get("carrier") in ("kgt", "kgt-free") else W2_STATUS


def expected_tool_count(binding, cfg) -> int:
    counts = binding.get("counts") or {}
    declared = len(cfg.get("tools") or [])
    key = tool_count_key(cfg)
    want = counts.get(key)
    if isinstance(want, int) and want != declared:
        raise Fail("声明自相矛盾：counts.%s=%s 但 tools 有 %d 个" % (key, want, declared))
    return declared


def expected_tool_count_source(binding, cfg) -> str:
    """工具数判据的出处（不含该键时为 'len(declared tools)'，如实登记不做假绿）。"""
    key = tool_count_key(cfg)
    want = (binding.get("counts") or {}).get(key)
    return "counts.%s=%s" % (key, want) if isinstance(want, int) else "len(declared tools)（counts.%s 未声明）" % key


def validate_rule_arm_faces(binding) -> list:
    """★ R-52 声明面机械不变量：任何声明了 `tools_removed_from_setb` 的 App（规则臂）必须满足

        tools == (同载体 Q 臂的 tools) − tools_removed_from_setb

    且移除集非空、非全集。这是「R 臂 = Q 臂去掉查询入口」在**声明面**上的护栏
    （契约面另有一道：G24-10 的 `tools ∪ tools_removed_from_setb == Set B 全集` + 白名单）。
    找不到同载体的 Q 臂 ⇒ 报错（fail-closed），不得静默跳过。
    """
    problems = []
    apps = binding.get("apps") or {}
    pairs = comparison_pairs(binding)
    q_pair = pairs[0] if pairs else []
    for name, cfg in apps.items():
        if not isinstance(cfg, dict):
            continue
        removed = cfg.get("tools_removed_from_setb")
        if not removed:
            continue
        carrier = cfg.get("carrier")
        q_name = None
        for cand in q_pair:
            if ((apps.get(cand) or {}).get("carrier")) == carrier:
                q_name = cand
                break
        if q_name is None:
            problems.append("规则臂 %s（carrier=%s）找不到同载体的 Q 臂 ⇒ 无法校验 R-52 不变量"
                            % (name, carrier))
            continue
        q_tools = list((apps.get(q_name) or {}).get("tools") or [])
        want = [t for t in q_tools if t not in set(removed)]
        got = list(cfg.get("tools") or [])
        if got != want:
            problems.append("规则臂 %s 的 tools=%s，期望「%s 的 tools − tools_removed_from_setb(%s)」=%s"
                            % (name, got, q_name, sorted(removed), want))
        if not want:
            problems.append("规则臂 %s 的移除集把工具全部移除（剩余 0 个）⇒ 非法" % name)
    return problems


# ---------------------------------------------------------------------------
# 构造
# ---------------------------------------------------------------------------
def declared_identity(binding, name: str) -> tuple:
    """该 App 的**声明身份** = ``(display_name, display_description)``（★ R-59 单一事实源）。

    fail-closed：缺任一项、或描述超 Dify 上限，一律**拒**——**不回落任何兜底文本**
    （「所有 App 共用一句兜底描述」正是 R-59 要根除的形态）。
    """
    cfg = (binding.get("apps") or {}).get(name) or {}
    disp_name = str(cfg.get("display_name") or "").strip()
    disp_desc = str(cfg.get("display_description") or "").strip()
    if not disp_name or not disp_desc:
        raise Fail("binding.apps.%s 缺 display_name / display_description ⇒ 拒绝写入"
                   "（不回落兜底描述）" % name)
    if len(disp_desc) > APP_DESCRIPTION_MAX:
        raise Fail("binding.apps.%s 的 display_description 长 %d 字 > Dify 上限 %d"
                   % (name, len(disp_desc), APP_DESCRIPTION_MAX))
    return disp_name, disp_desc


def validate_identity_uniqueness(binding) -> list:
    """★ R-59 反恒真：**所有** App 的身份字段必须非空且**两两不同**。

    为什么必须单列：既有的公平性门禁只比「同一对照对**内部**逐字一致」，
    而「**所有** App 共用一句描述」在那种比对面里是**恒真**的（全等）⇒ 门禁抓不到，
    只能由本断言抓（用户实测的缺陷就是这个形态）。
    """
    problems = []
    apps = binding.get("apps") or {}
    for field in ("display_name", "display_description"):
        seen = {}
        for key, cfg in apps.items():
            val = str((cfg or {}).get(field) or "").strip()
            if not val:
                problems.append("binding.apps.%s 缺 %s" % (key, field))
                continue
            if val in seen:
                problems.append("binding.apps.%s 与 %s 的 %s **逐字相同**（%r）⇒ 身份面退化"
                                % (key, seen[val], field, val[:60]))
            else:
                seen[val] = key
    return problems


def build_create_payload(binding, name: str) -> dict:
    """★ 只允许 6 个字段；mode 固定 agent-chat；**name/description 逐字取自声明身份**（R-59）。"""
    disp_name, disp_desc = declared_identity(binding, name)
    return {
        "name": disp_name,
        "description": disp_desc,
        "mode": APP_MODE_AGENT_CHAT,
        "icon_type": ICON_TYPE,
        "icon": ICON,
        "icon_background": ICON_BACKGROUND,
    }


def identity_update_payload(binding, name: str) -> dict:
    """``PUT /console/api/apps/<uuid>`` 的载荷：**只发** Dify ``UpdateAppPayload`` 声明的身份字段。

    ★ 为什么要单独一个函数而不用 create 载荷：create 载荷带 `mode`，而 `UpdateAppPayload`
      不声明 `mode`（容器内源码实测字段 = name/description/icon_type/icon/icon_background/
      use_icon_as_answer_icon/max_active_requests）—— 不发未声明的键，避免依赖 pydantic 对
      未知字段的宽容行为。
    """
    disp_name, disp_desc = declared_identity(binding, name)
    return {"name": disp_name, "description": disp_desc,
            "icon_type": ICON_TYPE, "icon": ICON, "icon_background": ICON_BACKGROUND}


def validate_create_payload(binding, name: str, payload: dict) -> list:
    problems = []
    disp_name, disp_desc = declared_identity(binding, name)
    extra = sorted(set(payload) - set(CREATE_FIELDS))
    missing = sorted(set(CREATE_FIELDS) - set(payload))
    if extra:
        problems.append("%s 建 App payload 含未允许字段：%s" % (name, extra))
    if missing:
        problems.append("%s 建 App payload 缺字段：%s" % (name, missing))
    if payload.get("mode") != APP_MODE_AGENT_CHAT:
        problems.append("%s 建 App mode=%r，必须是 %r（R-41）"
                        % (name, payload.get("mode"), APP_MODE_AGENT_CHAT))
    if payload.get("name") != disp_name:
        problems.append("%s 建 App payload name=%r 与声明 display_name=%r 不一致"
                        % (name, payload.get("name"), disp_name))
    if payload.get("description") != disp_desc:
        problems.append("%s 建 App payload description 与声明 display_description 不一致" % name)
    return problems


def tool_entries(binding, app_name: str, cfg: dict, faces: dict) -> list:
    """构造 agent_mode.tools 条目。

    provider 面全部取真机回读（faces）：kind=api → provider_id=uuid、provider_type/type="api"、
    provider_name=声明名；kind=plugin → provider_id=真机 builtin provider 的 id、
    provider_type/type=该 provider 的 type（"builtin"）、provider_name=该 provider 的 name。
    """
    faces_by_tool = tool_faces(binding, app_name, cfg, faces)
    out = []
    for tool_name in cfg["tools"]:
        face = faces_by_tool[tool_name][1]
        out.append({
            "enabled": True,
            "isDeleted": False,
            "notAuthor": False,
            "provider_id": face["id"],
            "provider_name": face["write_name"],
            "provider_type": face["write_type"],
            "tool_label": tool_name,
            "tool_name": tool_name,
            "tool_parameters": {},
            "type": face["write_type"],
        })
    return out


def _set_default_path(obj, dotpath, value) -> bool:
    """按点路径写入（缺中间层就建）；**只在该叶子缺失或为 None 时**写。返回是否变更。"""
    parts = dotpath.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    last = parts[-1]
    if last in cur and cur[last] is not None:
        return False
    cur[last] = value
    return True


def apply_documented_defaults(mc: dict) -> list:
    """把模板里**缺失或为 None** 的「文档化默认字段」补成默认值（★ R-59）。返回补了哪些。

    为什么必须补：本工具以「真机模板 App 的 model_config」为结构范本、**不凭记忆拼键**；
      但现网 .83 的模板 App 是**最小配置**建出来的 —— `file_upload.enabled` /
      `opening_statement` / `dataset_query_variable` / `dataset_configs.datasets.datasets`
      等**嵌套默认字段缺失或为 None**，而 F4 判据（`check_fairness` 的「未使用字段同为默认值」，
      其默认值来源已取证并登记在本文件的 `F4_DEFAULT_ASSERTED`）要求它们恰为
      `False / "" / []` ⇒ 不补就会**一建即 FAIL F4**（真机实测：apply 被自身 F4 预检拦下）。
    ★ 补进去的值**不是「记忆」**：全部取自 `F4_DEFAULT_ASSERTED`（门禁来源的文档化默认），
      且**只补缺失或 None 的叶子**，不覆盖模板里已有的任何取值。
    """
    changed = []
    for path, exp in F4_DEFAULT_ASSERTED:
        if _set_default_path(mc, path, exp):
            changed.append("%s=%r" % (path, exp))
    return changed


def build_model_config(prompt: str, tools: list, template_mc: dict) -> dict:
    """以真机模板 App 的 model_config 为结构范本，只替换 model/agent_mode/pre_prompt/prompt_type。

    ★ R-59：结构范本可能来自一个**最小配置**的 App（嵌套默认字段缺失/None）⇒ 末尾按
      `F4_DEFAULT_ASSERTED`（门禁来源的文档化默认）**补齐缺失叶子**，使写出的配置满足 F4。
    """
    if not isinstance(template_mc, dict) or not template_mc:
        raise Fail("模板 App 的 model_config 为空（不可凭记忆拼键）")
    mc = {k: copy.deepcopy(v) for k, v in template_mc.items() if k not in METADATA_KEYS}
    for key in ("model", "agent_mode", "pre_prompt", "prompt_type"):
        if key not in mc:
            raise Fail("模板 model_config 缺少结构性键：%s" % key)
    mc["prompt_type"] = "simple"
    mc["model"] = copy.deepcopy(R37_MODEL)
    mc["pre_prompt"] = prompt
    agent_mode = copy.deepcopy(R37_AGENT_CORE)
    agent_mode["tools"] = copy.deepcopy(tools)
    mc["agent_mode"] = agent_mode
    apply_documented_defaults(mc)
    return mc


def walk_key_paths(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = "%s.%s" % (prefix, k) if prefix else str(k)
            yield path
            yield from walk_key_paths(v, path)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_key_paths(v, "%s[%d]" % (prefix, i))


def function_calling_hits(mc: dict) -> list:
    return [p for p in walk_key_paths(mc) if p.split(".")[-1].split("[")[0] == FORBIDDEN_KEY_SUFFIX]


def validate_model_config(binding, app_name: str, cfg: dict, mc: dict,
                          faces: dict, prompt: str) -> list:
    """写前/写后同一套正向校验。返回问题列表（空 = 通过）。"""
    problems = []
    if not isinstance(mc, dict) or not mc:
        return ["%s model_config 为空" % app_name]

    if not same_json(mc.get("model"), R37_MODEL):
        problems.append("%s model 块与 R-37 不符：%s" % (app_name, common.stable_json(mc.get("model"))))
    if mc.get("prompt_type") != "simple":
        problems.append("%s prompt_type=%r，期望 'simple'" % (app_name, mc.get("prompt_type")))

    got_prompt = mc.get("pre_prompt")
    if not isinstance(got_prompt, str):
        problems.append("%s pre_prompt 不是字符串" % app_name)
    elif common.sha256_bytes(got_prompt.encode("utf-8")) != common.sha256_bytes(prompt.encode("utf-8")):
        problems.append("%s pre_prompt 与声明面正文不一致（sha256 %s vs %s）"
                        % (app_name,
                           common.sha256_bytes(got_prompt.encode("utf-8"))[:16],
                           common.sha256_bytes(prompt.encode("utf-8"))[:16]))

    am = mc.get("agent_mode")
    if not isinstance(am, dict):
        problems.append("%s agent_mode 不是对象" % app_name)
        return problems
    for k, v in R37_AGENT_CORE.items():
        if k not in am:
            problems.append("%s agent_mode 缺少键 %s（期望 %s）" % (app_name, k, v))
        elif not same_json(am.get(k), v):
            problems.append("%s agent_mode.%s=%r，期望 %r" % (app_name, k, am.get(k), v))

    tools = am.get("tools")
    want_n = expected_tool_count(binding, cfg)
    if not isinstance(tools, list):
        problems.append("%s agent_mode.tools 不是列表" % app_name)
    else:
        # ★ A′ 第 ③ 条门禁（2026-10-15，用户裁定）—— 照参考项目 `deploy_agent_v3.py:266-268` 的做法：
        #   「提示词与工具面必须一致：①必须提及**首选入口**；②**不得提及本臂没有的工具**」。
        #   为什么必须有：P3 收窄工具面时我把 `cc_guide`/`cc_facts` 从四臂移除，
        #   而提示词第 7-8 行仍要求「先调用 cc_guide … 再调用 cc_facts …」⇒
        #   实测 agent 于是**猜参数、烧轮次、撞满 max_iteration**（Q 臂 avg 20.8 轮、85% 触顶 ⇒ Dify 500）。
        #   参考项目的原话：「**"靠提示词让模型记住多步流程"是把确定性工作交给概率性执行者**」。
        #   ⇒ 把「提示词点名了本臂没有的工具」做成**部署期硬失败**，这类漂移就再也进不了生产。
        _pp = mc.get("pre_prompt") or ""
        _got = {t.get("tool_name") for t in tools if isinstance(t, dict)}
        _all_tools = all_declared_tool_names(binding)
        _named = sorted(n for n in _all_tools if re.search(r"\b%s\b" % re.escape(n), _pp))
        _ghost = [n for n in _named if n not in _got]
        if _ghost:
            problems.append("%s 提示词点名了本臂**没有**的工具 %s（本臂工具面 %s）⇒ "
                            "agent 会去调不存在的工具并烧掉轮次预算"
                            % (app_name, _ghost, sorted(_got)))
        _primary = PRIMARY_ENTRY_TOOL.get(app_name)
        if _primary and _primary not in _pp:
            problems.append("%s 提示词未提及**首选入口** %s ⇒ 与「一次调用即结论」的编排口径冲突"
                            % (app_name, _primary))
        if len(tools) != want_n:
            problems.append("%s tool 数 = %d，期望 %d" % (app_name, len(tools), want_n))
        declared = list(cfg["tools"])
        got_names = [t.get("tool_name") for t in tools if isinstance(t, dict)]
        if sorted(got_names) != sorted(declared):
            problems.append("%s 工具集合 %s 与声明 %s 不一致" % (app_name, sorted(got_names), sorted(declared)))
        # ★ S2c：逐工具解析到**声明的 provider 集合**中的那一个面（不再假设一 App 一 provider）
        faces_by_tool = tool_faces(binding, app_name, cfg, faces)
        for t in tools:
            if not isinstance(t, dict):
                problems.append("%s 工具条目不是对象：%r" % (app_name, t))
                continue
            _own = faces_by_tool.get(t.get("tool_name"))
            # 未解析到面的工具（名字不在声明集合里）已在上面「工具集合」断言里报过；
            # 这里给一个空面继续做完条目级检查（宁可多报，不可漏检）。
            face = _own[1] if _own else {"id": None, "write_name": None, "write_type": None}
            miss = [k for k in TOOL_ENTITY_KEYS if k not in t]
            if miss:
                problems.append("%s 工具 %s 缺键 %s" % (app_name, t.get("tool_name"), miss))
            if t.get("tool_parameters") != {}:
                problems.append("%s 工具 %s 的 tool_parameters 非空：%r"
                                % (app_name, t.get("tool_name"), t.get("tool_parameters")))
            if t.get("enabled") is not True:
                problems.append("%s 工具 %s 的 enabled 非真" % (app_name, t.get("tool_name")))
            if t.get("provider_id") != face["id"]:
                problems.append("%s 工具 %s 的 provider_id=%r，期望 %s"
                                % (app_name, t.get("tool_name"), t.get("provider_id"), face["id"]))
            if t.get("provider_name") not in (None, face["write_name"]):
                problems.append("%s 工具 %s 的 provider_name=%r，期望 %s"
                                % (app_name, t.get("tool_name"), t.get("provider_name"), face["write_name"]))
            if t.get("provider_type") != face["write_type"]:
                problems.append("%s 工具 %s 的 provider_type=%r，期望 %r"
                                % (app_name, t.get("tool_name"), t.get("provider_type"), face["write_type"]))
            if t.get("type") != face["write_type"]:
                problems.append("%s 工具 %s 的 type=%r，期望 %r"
                                % (app_name, t.get("tool_name"), t.get("type"), face["write_type"]))
        # kind=plugin：声明要绑的工具必须真机确有其工具（builtin provider 的 tools 面）
        # ★ S2c：按**每个声明的 provider 各自的**工具面核对（不再假设一 App 一 provider）
        for _pname in declared_providers(binding, app_name, cfg):
            _pface = find_provider_face(binding, _pname, faces)
            exposed = _pface.get("exposed_tools")
            if not isinstance(exposed, list):
                continue
            own = [n for n in cfg["tools"]
                   if tool_provider_of(binding, app_name, cfg, n) == _pname]
            unknown = [n for n in own if n not in exposed]
            if unknown:
                problems.append("%s 声明绑定的工具 %s 不在 provider %s 真机工具面 %s 里"
                                % (app_name, unknown, _pname, sorted(exposed)))

    hits = function_calling_hits(mc)
    if hits:
        problems.append("%s model_config 出现 function_calling_type（不得注入）：%s" % (app_name, hits))

    # 本地 F4 预检（判据以 check_fairness.py 为准）
    for path, exp in F4_DEFAULT_ASSERTED:
        got = dig(mc, path, MISSING)
        if got is MISSING:
            problems.append("%s F4 预检：缺少键 %s（期望默认 %r）" % (app_name, path, exp))
            continue
        ok = (got is None or got == "") if exp is None else (got == exp)
        if not ok:
            problems.append("%s F4 预检：%s=%r，期望默认 %r" % (app_name, path, got, exp))
    return problems


def subset_equal(current: dict, desired: dict) -> bool:
    """current 在 desired 的每个键上都逐字相同（current 允许有额外键）。"""
    for k, v in desired.items():
        if k not in current:
            return False
        if not same_json(current[k], v):
            return False
    return True


# ---------------------------------------------------------------------------
# live 面
# ---------------------------------------------------------------------------
def list_api_provider_ids(console) -> dict:
    data = console.get_json("/console/api/workspaces/current/tool-providers?type=api")
    items = data["data"] if isinstance(data, dict) and "data" in data else data
    return {it["name"]: it["id"] for it in items}


def _provider_rows(console, ptype: str) -> list:
    data = console.get_json("/console/api/workspaces/current/tool-providers?type=%s" % ptype)
    items = data["data"] if isinstance(data, dict) and "data" in data else data
    return [it for it in (items or []) if isinstance(it, dict)]


def list_provider_faces(console) -> dict:
    """真机 provider 面 → {"by_id":…, "by_name":…, "by_plugin_id":…}。

    api 与 builtin 两个命名空间都取；builtin provider 的 id/name 是斜杠形态（不是 uuid），
    另以 plugin_id 建索引，供声明面用 2 段 plugin_id 反查（避免把 installation_id 当 provider id）。
    """
    by_id, by_name, by_plugin_id = {}, {}, {}
    for ptype in ("api", "builtin"):
        for it in _provider_rows(console, ptype):
            face = {
                "id": it.get("id"),
                "type": it.get("type") or ptype,
                "name": it.get("name") or it.get("id"),
                "plugin_id": it.get("plugin_id"),
                "plugin_unique_identifier": it.get("plugin_unique_identifier"),
                "provider_namespace": ptype,
            }
            if not face["id"]:
                continue
            by_id[face["id"]] = face
            by_name.setdefault(face["name"], face)
            if face["plugin_id"]:
                by_plugin_id.setdefault(face["plugin_id"], face)
            if face["plugin_unique_identifier"]:
                by_name.setdefault(face["plugin_unique_identifier"], face)
    return {"by_id": by_id, "by_name": by_name, "by_plugin_id": by_plugin_id}


def builtin_provider_tools(console, provider_id: str) -> list:
    """builtin provider 的工具面（逐 provider 的输入 schema 面）；失败即返回 [] 并由调用方登记。"""
    from urllib.parse import quote
    path = "/console/api/workspaces/current/tool-provider/builtin/%s/tools" % quote(provider_id, safe="")
    data = console.get_json(path)
    items = data if isinstance(data, list) else (data.get("data") or [])
    return [it.get("name") for it in (items or []) if isinstance(it, dict) and it.get("name")]


def fetch_app_rows(console) -> list:
    """真机 App 列表的**原始行**（不做任何索引）。"""
    data = console.get_json("/console/api/apps?page=1&limit=100")
    return [r for r in (data["data"] or []) if isinstance(r, dict)]


def list_apps(console) -> dict:
    """按**真机 name** 索引（语义保持不变）。

    ★ W20：真机 App 的 `name` 已按 binding 声明改为中文 `display_name`，
      所以**不要**再用本函数做「声明键 → 真机」的映射，请改用
      `resolve_declared_apps`（uuid 优先）。
    """
    return {r["name"]: r for r in fetch_app_rows(console) if r.get("name")}


def resolve_template_app_id(console) -> str:
    """结构范本 App 的 uuid：**按名优先**（``TEMPLATE_APP_NAME``）、历史 uuid 兜底（★ R-59）。

    为什么必须按名：模板 App 是「结构范本」，它在现网重建后 uuid 必变；把 uuid 钉死会让
    `--check`/`--apply` 直接硬失败（真机实测正是如此）。名字才是稳定标识。
    """
    for row in fetch_app_rows(console):
        if row.get("name") == TEMPLATE_APP_NAME:
            return str(row.get("id"))
    return TEMPLATE_APP_ID


def resolve_declared_apps(binding, rows) -> tuple:
    """声明键 → 真机 App 行：**uuid → 声明键名 → 声明 display_name**（W20 + ★ R-59）。

    返回 `(by_key, how)`；`how[key] ∈ {"uuid", "name", "display_name", "missing"}`。
    ★ 原因：App 改名后 `name` 不再等于 binding 机器键，旧「按名字索引」会
      静默少读（0 命中却不报错）。uuid 是绑定不变量，与改名无关。
    ★ R-59 补第三级 `display_name`：把身份对齐到声明后，真机 `name` **就是** display_name，
      而「声明键」这一级**永远不会**再命中 ⇒ 若上一次 apply 在**回写 uuid 之前**中断
      （本轮真机实测：F4 预检失败中止），重跑会**当成新 App 再建一个** ⇒ 真机上出现**重复 App**。
      加这一级即从源头堵住该形态（uuid 仍是第一优先，改名不影响绑定）。
    ★ 名字兜底只为兼容未登记 uuid 的历史声明；真机名的正确性由
      `check_fairness.py` 的 F0.n（真机 name == 声明 display_name）与 `--check` 的身份漂移断言兜住。
    """
    by_id, by_name = {}, {}
    for r in rows or []:
        if r.get("id"):
            by_id.setdefault(r["id"], r)
        if r.get("name"):
            by_name.setdefault(r["name"], r)
    by_key, how = {}, {}
    for key, cfg in (binding.get("apps") or {}).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        uuid = cfg.get("uuid")
        r = by_id.get(uuid) if isinstance(uuid, str) and uuid else None
        if r is not None:
            by_key[key], how[key] = r, "uuid"
            continue
        r = by_name.get(key)
        if r is not None:
            by_key[key], how[key] = r, "name"
            continue
        disp = str(cfg.get("display_name") or "").strip()
        r = by_name.get(disp) if disp else None
        if r is not None:
            by_key[key], how[key] = r, "display_name"
            continue
        how[key] = "missing"
    return by_key, how


def get_app(console, app_id: str) -> dict:
    return console.get_json("/console/api/apps/%s" % app_id)


def collect_live(console, binding) -> dict:
    provider_ids = list_api_provider_ids(console)
    faces = list_provider_faces(console)
    # 对声明面里 kind=plugin 的 provider 补 exposed_tools（真机工具面）
    for name, decl in provider_decls(binding).items():
        if (decl.get("kind") or "api") != "plugin":
            continue
        try:
            face = find_provider_face(binding, name, faces)
        except Fail:
            continue
        tools = builtin_provider_tools(console, face["id"])
        face["exposed_tools"] = tools
    by_name = list_apps(console)
    rows = list(by_name.values())
    by_key, how = resolve_declared_apps(binding, rows)
    return {
        "utc": utcnow(),
        "provider_ids": provider_ids,
        "provider_faces": faces,
        "apps_by_name": by_name,
        "apps_by_key": by_key,
        "app_resolve": how,
        # ★ W20：app_ids 现在以**声明键**为键（uuid 优先解析），供调用方按声明键取 uuid；
        #   旧实现以真机名为键，App 改名后会全部取不到（静默少读）。
        "app_ids": {k: (r.get("id") if isinstance(r, dict) else None) for k, r in by_key.items()},
    }


def face_summary(mc: dict, prompt_sha: str) -> dict:
    am = (mc or {}).get("agent_mode") or {}
    tools = am.get("tools") or []
    pp = (mc or {}).get("pre_prompt")
    return {
        "model_sha256": common.sha256_bytes(common.stable_json(mc.get("model")).encode("utf-8")),
        "agent_mode_core": {k: am.get(k) for k in ("enabled", "strategy", "max_iteration", "prompt")},
        "agent_mode_core_sha256": common.sha256_bytes(
            common.stable_json({k: am.get(k) for k in ("enabled", "strategy", "max_iteration", "prompt")})
            .encode("utf-8")),
        "pre_prompt_len": len(pp) if isinstance(pp, str) else None,
        "pre_prompt_sha256": common.sha256_bytes(pp.encode("utf-8")) if isinstance(pp, str) else None,
        "pre_prompt_matches_file": (
            isinstance(pp, str)
            and common.sha256_bytes(pp.encode("utf-8")) == prompt_sha
        ),
        "tool_count": len(tools),
        "tools": [
            {"tool_name": t.get("tool_name"), "provider_id": t.get("provider_id"),
             "provider_name": t.get("provider_name"), "enabled": t.get("enabled"),
             "tool_parameters": t.get("tool_parameters")}
            for t in tools if isinstance(t, dict)
        ],
        "model_config_keys": sorted(mc.keys()) if isinstance(mc, dict) else [],
        "model_config_sha256": common.sha256_bytes(common.stable_json(mc).encode("utf-8"))
        if isinstance(mc, dict) else None,
        "function_calling_type_paths": function_calling_hits(mc) if isinstance(mc, dict) else [],
    }


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
def cmd_check(args) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    rc = 0
    problems = []
    binding = load_declaration(args.binding)
    problems += verify_prompt_file()
    problems += validate_rule_arm_faces(binding)
    # ★ R-59：身份面反恒真（所有 App 的 display_name/display_description 非空且两两不同）
    problems += validate_identity_uniqueness(binding)
    targets = target_apps(binding, args.apps)
    specs = {}
    for name, cfg in targets:
        try:
            spec = prompt_spec_for(name, binding)
        except Fail as exc:
            problems.append(str(exc))
            continue
        specs[name] = spec
        problems += verify_prompt_file_spec(spec)
    if problems:
        print("[check] 声明面/提示词基准不可用：")
        for p in problems:
            print("  - %s" % p)
        return 1
    prompt_sha = {n: s["sha256"] for n, s in specs.items()}

    console = common.console_client(repo_root=REPO_ROOT)
    live = collect_live(console, binding)
    print("[check] utc=%s base=%s" % (live["utc"], console.base))

    # 声明面 → live provider 面
    for name, cfg in targets:
        n = expected_tool_count(binding, cfg)
        for prov in declared_providers(binding, name, cfg):
            try:
                face = find_provider_face(binding, prov, live["provider_faces"])
            except Fail as exc:
                problems.append(str(exc))
                continue
            own = [x for x in cfg["tools"]
                   if tool_provider_of(binding, name, cfg, x) == prov]
            print("  声明 %-18s provider=%-18s kind=%-7s id=%s tools=%d/%d（%s）"
                  % (name, prov, face["kind"], face["id"], len(own), n,
                     expected_tool_count_source(binding, cfg)))
        if specs.get(name):
            # ★ U-N 裁定 = A：派生变体没有文件 ⇒ `path` 为 None，日志须能如实显示「派生」
            _sp = specs[name]
            _label = (_sp["path"].name if _sp.get("path") is not None
                      else "（派生·无文件·来自 %s）" % _sp.get("variant"))
            print("       prompt %-28s variant=%-10s bytes=%s lf=%s sha256=%s"
                  % (_label, _sp["variant"], _sp["bytes"],
                     _sp["lf"], (prompt_sha.get(name) or "")[:16]))

    # 三个注册面是否齐备
    for prov in ((binding.get("whitelist") or {}).get("provider_names") or []):
        if prov not in live["provider_ids"]:
            problems.append("live 缺少白名单 provider %s" % prov)

    # 模板 App（★ R-59：按名解析 —— 模板 App 重建后 uuid 会变）
    tpl_id = resolve_template_app_id(console)
    tpl = get_app(console, tpl_id)
    tmc = tpl.get("model_config") or {}
    if tpl.get("mode") != APP_MODE_AGENT_CHAT or not tmc:
        problems.append("模板 App %s 不可用作结构范本：mode=%r model_config_keys=%d"
                        % (tpl_id, tpl.get("mode"), len(tmc)))
    else:
        print("  模板 %s（name=%s）mode=%s keys=%d"
              % (tpl_id, TEMPLATE_APP_NAME, tpl.get("mode"), len(tmc)))

    missing_apps = []
    for name, cfg in targets:
        app_id = live["app_ids"].get(name)
        if app_id is None:
            missing_apps.append(name)
            print("  [BLOCKED] App %s 尚未创建" % name)
            continue
        det = get_app(console, app_id)
        mc = det.get("model_config") or {}
        entry = {"app": name, "uuid": app_id, "mode": det.get("mode"),
                 "utc": utcnow(), "face": face_summary(mc, prompt_sha.get(name))}
        print("  App %-18s uuid=%s mode=%s tools=%d" % (name, app_id, det.get("mode"), entry["face"]["tool_count"]))
        if det.get("mode") != APP_MODE_AGENT_CHAT:
            problems.append("%s mode=%r 不是 agent-chat（门禁会 BLOCKED，而非 FAIL）" % (name, det.get("mode")))
        # ★ R-59：真机身份（name/description）必须逐字等于声明面
        decl_name, decl_desc = declared_identity(binding, name)
        ident_drift = []
        if det.get("name") != decl_name:
            ident_drift.append("name 真机=%r 声明=%r" % (det.get("name"), decl_name))
        if (det.get("description") or "") != decl_desc:
            ident_drift.append("description 真机=%r 声明=%r"
                               % ((det.get("description") or "")[:60], decl_desc[:60]))
        if ident_drift:
            problems.append("%s 身份与声明不一致：%s" % (name, "; ".join(ident_drift)))
        entry["identity"] = {"live_name": det.get("name"), "declared_name": decl_name,
                             "live_description": det.get("description"),
                             "declared_description": decl_desc, "drift": ident_drift}
        problems += validate_model_config(binding, name, cfg, mc, live["provider_faces"],
                                          prompt_text_of(specs[name]))
        if args.json:
            Path(args.json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json).write_text(json.dumps(entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8", newline="\n")

    if problems:
        print("[check] 问题 %d 条：" % len(problems))
        for p in problems:
            print("  - %s" % p)
        rc = 1
    else:
        print("[check] 一致")
    if missing_apps:
        print("[check] BLOCKED：%d 个 App 未创建（%s）⇒ 退出码 2" % (len(missing_apps), ", ".join(missing_apps)))
        return 2
    return rc


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------
def cmd_apply(args) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    binding = load_declaration(args.binding)
    face_problems = validate_rule_arm_faces(binding)
    if face_problems:
        raise Fail("R-52 规则臂声明面不变量不成立（拒绝写入）：%s" % "; ".join(face_problems))
    # ★ R-59：身份面反恒真（所有 App 的 display_name/display_description 非空且两两不同）
    ident_problems = validate_identity_uniqueness(binding)
    if ident_problems:
        raise Fail("R-59 身份面不变量不成立（拒绝写入）：%s" % "; ".join(ident_problems))
    targets = target_apps(binding, args.apps)
    specs = {}
    for name, cfg in targets:
        spec = prompt_spec_for(name, binding)
        problems = verify_prompt_file_spec(spec)
        if problems:
            raise Fail("提示词正文与声明面不一致：%s" % "; ".join(problems))
        specs[name] = spec

    console = common.console_client(repo_root=REPO_ROOT)
    live = collect_live(console, binding)
    started = utcnow()
    print("[apply] utc=%s base=%s" % (started, console.base))

    # ★ R-59：结构范本按名解析（模板 App 重建后 uuid 会变），历史 uuid 仅兜底
    tpl_id = resolve_template_app_id(console)
    tpl = get_app(console, tpl_id)
    if tpl.get("mode") != APP_MODE_AGENT_CHAT or not (tpl.get("model_config") or {}):
        raise Fail("模板 App %s（name=%s）的 model_config 不可用，拒绝凭记忆拼键"
                   % (tpl_id, TEMPLATE_APP_NAME))
    template_mc = tpl["model_config"]

    report = {
        "utc_start": started, "base": console.base, "template_app": tpl_id,
        "prompt_face": {
            n: {"path": prompt_path_label(s), "variant": s["variant"],
                "bytes": s["bytes"], "lf": s["lf"], "sha256": s["sha256"]}
            for n, s in specs.items()
        },
        "apps": {}, "existing_app_count_before": len(live["apps_by_name"]),
        "existing_app_names_before": sorted(live["apps_by_name"]),
    }

    for name, cfg in targets:
        spec = specs[name]
        prompt = prompt_text_of(spec)
        prompt_sha = common.sha256_bytes(prompt.encode("utf-8"))
        provs = declared_providers(binding, name, cfg)
        row = {"declared_provider": cfg["provider"], "declared_providers": provs,
               "tool_provider": {x: tool_provider_of(binding, name, cfg, x) for x in cfg["tools"]},
               "declared_tools": list(cfg["tools"]),
               "prompt_variant": spec["variant"], "prompt_file": prompt_path_label(spec),
               "prompt_bytes": spec["bytes"], "prompt_lf": spec["lf"], "prompt_sha256": prompt_sha,
               "tool_count_source": expected_tool_count_source(binding, cfg)}
        payload = build_create_payload(binding, name)
        problems = validate_create_payload(binding, name, payload)
        if problems:
            raise Fail("建 App payload 不合法：%s" % "; ".join(problems))
        row["create_payload"] = payload

        app_id = live["app_ids"].get(name)
        if app_id is None:
            resp = console.post_json("/console/api/apps", payload, expect=(200, 201))
            app_id = resp.get("id")
            if not app_id:
                raise Fail("创建 %s 未返回 id：%s" % (name, common.stable_json(resp)[:400]))
            row["action"] = "created"
            row["create_response_utc"] = utcnow()
            print("  + created %s uuid=%s" % (name, app_id))
        else:
            row["action"] = "pre_existing"
            print("  = existing %s uuid=%s（不删除；身份按声明对齐）" % (name, app_id))
        row["uuid"] = app_id

        det = get_app(console, app_id)
        if det.get("mode") != APP_MODE_AGENT_CHAT:
            raise Fail("%s mode=%r 不是 agent-chat；拒绝写入 model-config（R-41）" % (name, det.get("mode")))

        # ★ R-59：**身份对齐**（name/description 逐字取声明面）。
        #   新建的已在 create 载荷里一致；**既有 App 必须在此补齐** —— 原实现声称
        #   「不删除、不改名」而完全不动身份，于是所有 App 共用同一句 APP_DESCRIPTION
        #   （用户实测缺陷）。写入后**回读校验**，不一致即硬失败（不留半完成态）。
        ident_drift = [f for f, cur in (("name", det.get("name")),
                                        ("description", det.get("description") or ""))
                       if cur != payload[f]]
        if ident_drift:
            console.request("PUT", "/console/api/apps/%s" % app_id,
                            identity_update_payload(binding, name), expect=(200, 201))
            back = get_app(console, app_id)
            if (back.get("name") != payload["name"]
                    or (back.get("description") or "") != payload["description"]):
                raise Fail("%s 身份写入后回读不一致：name=%r description=%r"
                           % (name, back.get("name"), (back.get("description") or "")[:60]))
            row["identity_action"] = "updated(%s)" % ",".join(ident_drift)
            print("    身份对齐 %-22s name=%r desc=%r"
                  % ("(%s)" % ",".join(ident_drift), payload["name"], payload["description"][:46]))
        else:
            row["identity_action"] = "unchanged"

        tools = tool_entries(binding, name, cfg, live["provider_faces"])
        want = build_model_config(prompt, tools, template_mc)
        problems = validate_model_config(binding, name, cfg, want, live["provider_faces"], prompt)
        if problems:
            raise Fail("待写入的 model-config 不合法：%s" % "; ".join(problems))
        row["submitted_model_config_sha256"] = common.sha256_bytes(
            common.stable_json(want).encode("utf-8"))

        current = det.get("model_config") or {}
        if subset_equal(current, want):
            row["model_config_action"] = "unchanged"
            print("    model-config 已是目标状态，跳过写入（幂等）")
        else:
            console.post_json("/console/api/apps/%s/model-config" % app_id, want, expect=(200, 201))
            row["model_config_action"] = "replaced"
            row["model_config_post_utc"] = utcnow()
            print("    model-config 已整体替换")

        after = get_app(console, app_id)
        mc_after = after.get("model_config") or {}
        row["readback_utc"] = utcnow()
        row["readback_mode"] = after.get("mode")
        row["readback_model_config"] = mc_after
        row["readback_face"] = face_summary(mc_after, prompt_sha)
        post_problems = validate_model_config(binding, name, cfg, mc_after, live["provider_faces"], prompt)
        row["readback_problems"] = post_problems
        row["readback_ok"] = not post_problems
        if post_problems:
            print("    !! 回读不通过：")
            for p in post_problems:
                print("       - %s" % p)
        report["apps"][name] = row

    # 两侧逐键 diff
    names = [n for n, _ in targets]
    if len(names) == 2:
        a = report["apps"][names[0]]["readback_model_config"]
        b = report["apps"][names[1]]["readback_model_config"]
        keys = sorted(set(a) | set(b))
        diff = {}
        for k in keys:
            ina, inb = k in a, k in b
            if not ina or not inb:
                diff[k] = {"both": False, "in_%s" % names[0]: ina, "in_%s" % names[1]: inb}
            elif not same_json(a[k], b[k]):
                diff[k] = {"both": True, "equal": False,
                           "a_sha256": common.sha256_bytes(common.stable_json(a[k]).encode("utf-8")),
                           "b_sha256": common.sha256_bytes(common.stable_json(b[k]).encode("utf-8"))}
            else:
                diff[k] = {"both": True, "equal": True,
                           "sha256": common.sha256_bytes(common.stable_json(a[k]).encode("utf-8"))}
        # 期望差异面：agent_mode（tools 必然不同）；两侧 prompt 规格不同时 pre_prompt 也算期望差异
        expected_unequal = ["agent_mode"]
        if specs[names[0]]["sha256"] != specs[names[1]]["sha256"]:
            expected_unequal.append("pre_prompt")
        report["pair_diff"] = {
            "pair": names,
            "keys": keys,
            "unequal_keys": sorted(k for k, v in diff.items() if not v.get("equal")),
            "key_sets_symmetric": sorted(set(a) ^ set(b)) == [],
            "only_expected_unequal": expected_unequal,
            "per_key": diff,
            "model_equal": same_json(a.get("model"), b.get("model")),
            "model_sha256": {"a": common.sha256_bytes(common.stable_json(a.get("model")).encode("utf-8")),
                             "b": common.sha256_bytes(common.stable_json(b.get("model")).encode("utf-8"))},
            "pre_prompt_equal": a.get("pre_prompt") == b.get("pre_prompt"),
            "pre_prompt_sha256": {"a": common.sha256_bytes(str(a.get("pre_prompt")).encode("utf-8")),
                                  "b": common.sha256_bytes(str(b.get("pre_prompt")).encode("utf-8"))},
        }

    # 与提交值对比（诚实：报告实际回读值，不报提交值）
    for name, cfg in targets:
        row = report["apps"][name]
        am = (row["readback_model_config"].get("agent_mode") or {})
        row["tool_face_by_provider"] = sorted({t.get("provider_id") for t in (am.get("tools") or [])})

    report["utc_end"] = utcnow()
    report["ok"] = all(report["apps"][n]["readback_ok"] for n, _ in targets)

    if args.evidence:
        out = Path(args.evidence)
        out.mkdir(parents=True, exist_ok=True)
        p = out / "deploy_agents_apply.json"
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8", newline="\n")
        print("[apply] 证据：%s（%d bytes）" % (p, p.stat().st_size))

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8", newline="\n")

    if args.write_binding:
        name_to_uuid = {n: report["apps"][n]["uuid"] for n, _ in targets}
        # status 只在回读通过时写回（未验证不得登记成已建成）
        name_to_status = {n: status_for(cfg) for n, cfg in targets if report["apps"][n]["readback_ok"]}
        changed, new_text, details = update_binding_uuids(
            Path(args.binding), name_to_uuid, name_to_status)
        if changed:
            Path(args.binding).write_text(new_text, encoding="utf-8", newline="")
        facts = common.file_facts(str(args.binding))
        print("[apply] binding.yaml %s：%s" % ("已更新" if changed else "无需更新", details))
        print("[apply] binding.yaml bytes=%d lf=%d sha256=%s"
              % (facts["bytes"], facts["lf_lines"], facts["sha256"]))
        report["binding_update"] = {"changed": changed, "details": details, "facts": facts,
                                    "statuses": name_to_status}
        if args.evidence:
            p = Path(args.evidence) / "deploy_agents_apply.json"
            p.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8", newline="\n")

    if not report["ok"]:
        print("[apply] 回读校验失败 ⇒ 退出码 1")
        return 1
    print("[apply] 完成：%s" % ", ".join("%s=%s" % (n, report["apps"][n]["uuid"]) for n, _ in targets))
    return 0


def update_binding_uuids(path: Path, name_to_uuid: dict, name_to_status: dict = None) -> tuple:
    """在 apps: 段内为每个 App 键插入/替换 uuid 行（行级手术，保留注释、键顺序与缩进）。

    ★ 只动 apps: 段；不在 name_to_uuid 里的 App 子块一字不改（W3 的 KG-T 两组不受影响）。
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "apps:")
    except StopIteration:
        raise Fail("binding.yaml 中找不到顶层的 apps: 行")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if ln and not ln.startswith(" ") and ln.rstrip():
            end = i
            break
    block = list(lines[start:end])
    details = {}
    changed = False
    spans = []
    for i, ln in enumerate(block):
        if ln.startswith("  ") and not ln.startswith("   ") and ln.rstrip().endswith(":"):
            spans.append((i, ln[2:-1]))
    # 逆序处理：先改后面的子块，前面的 span 下标才不会因长度变化而失效
    for idx in range(len(spans) - 1, -1, -1):
        i, app = spans[idx]
        stop = spans[idx + 1][0] if idx + 1 < len(spans) else len(block)
        if app not in name_to_uuid:
            details.setdefault(app, "untouched (not in scope)")
            continue
        uuid_line = '    uuid: "%s"' % name_to_uuid[app]
        new_status = (name_to_status or {}).get(app)
        sub = block[i + 1:stop]
        keys = {}
        for j, s in enumerate(sub):
            if s.startswith("    ") and not s.startswith("     ") and ":" in s:
                keys.setdefault(s.strip().split(":", 1)[0], j)
        bits = []
        if "uuid" in keys:
            if sub[keys["uuid"]] != uuid_line:
                changed = True
            sub[keys["uuid"]] = uuid_line
            bits.append("uuid=%s" % name_to_uuid[app])
        else:
            sub.insert(0, uuid_line)
            keys = {k: v + 1 for k, v in keys.items()}
            bits.append("uuid=%s(inserted)" % name_to_uuid[app])
            changed = True
        if new_status:
            status_line = "    status: %s" % new_status
            if "status" in keys:
                if sub[keys["status"]] != status_line:
                    changed = True
                sub[keys["status"]] = status_line
                bits.append("status=%s" % new_status)
            else:
                sub.insert(keys.get("uuid", 0) + 1, status_line)
                bits.append("status=%s(inserted)" % new_status)
                changed = True
        block[i + 1:stop] = sub
        details[app] = "wrote " + " ".join(bits)
    if not changed:
        return False, text, details
    return True, "\n".join(lines[:start] + block + lines[end:]), details


# ---------------------------------------------------------------------------
# --selftest（离线；含负控）
# ---------------------------------------------------------------------------
class _Resp:
    __slots__ = ("status_code", "text", "headers", "json_obj")

    def __init__(self, status_code, obj=None, text=None):
        self.status_code = status_code
        self.headers = {}
        self.text = text if text is not None else json.dumps(obj, ensure_ascii=False)
        self.json_obj = obj


def declared_plugin_rows(binding) -> list:
    """由 binding 声明面派生 builtin（插件）provider 的**假面行** —— 夹具必须声明驱动。

    ★ W23 根因修复：旧夹具把市场插件的 id/plugin_id 写死成字面量，
      W12 把 `cc-kgt-free-agent.provider` 重绑到 fork（声明 `neo4j_query_fork`，
      plugin_id `cc-ops/neo4j_cypher_query`）之后，夹具里就**没有那个面**，
      `find_provider_face` 在 kind=plugin 上必然 Fail ⇒ 真 binding 上 `--selftest` 红。
      根因是夹具缺面，不是判据错：本函数改为「声明里有多少 kind=plugin 的 provider，
      夹具就造多少行」，声明与夹具不再可能各说各话。

    行形态与真机 builtin 行同构（见 `list_provider_faces`）：
      id / name = 声明里带工具面的 `provider_id`（斜杠形态，不是 uuid、不是 installation_id）
      plugin_id = 声明里的 2 段插件身份（author/name）
      plugin_unique_identifier = 声明里的 uid（带版本+哈希）
      tools = 声明里 `bind_tools`（真机同名插件的工具面）
    """
    decls = provider_decls(binding)
    rows = []
    for name in sorted(decls):
        decl = decls[name]
        if (decl.get("kind") or "api") != "plugin":
            continue
        pid = decl.get("provider_id") or decl.get("plugin_id")
        if not pid:
            raise Fail("provider %s 声明 kind=plugin 但既无 provider_id 也无 plugin_id" % name)
        rows.append({
            "id": pid,
            "name": pid,
            "type": PLUGIN_PROVIDER_TYPE,
            "plugin_id": decl.get("plugin_id"),
            "plugin_unique_identifier": decl.get("plugin_unique_identifier"),
            "tools": list(decl.get("bind_tools") or decl.get("tools") or []),
        })
    return rows


FAKE_API_PROVIDER_IDS = {
    "cc-kg-tools": "d9b2b1ff-4aa7-442f-9959-2fde396327e8",
    "cc-onto-tools": "a8ebb23b-4ca7-49ae-a3f0-74f68283be9f",
    "cc-kgt-tools": "e56dfa91-c5cd-4ee1-ab5f-eb27236462ec",
}


def fake_api_provider_id(name: str) -> str:
    """未在 FAKE_API_PROVIDER_IDS 里的 api provider：由**名字派生**稳定 uuid（同名字同 id）。"""
    h = common.sha256_bytes(("s2c-fixture-api:" + name).encode("utf-8"))
    return "%s-%s-%s-%s-%s" % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def declared_api_rows(binding) -> list:
    """由 binding 声明面派生 api provider 的**假面行**（夹具声明驱动，S2c）。

    ★ 与 declared_plugin_rows 同理：注册面每新增一个 api provider（本批 cc-live-facts-tools），
      夹具必须自动有它的面，否则 --selftest 会因夹具缺面而假红。已知三个 provider 沿用历史
      夹具 uuid（不动既有自检语义），新 provider 用名字派生的稳定 uuid。
      顺序按 whitelist.provider_names，未知者排其后并按名排序（稳定、可复现）。
    """
    decls = provider_decls(binding)
    order = list((binding.get("whitelist") or {}).get("provider_names") or [])
    names = [n for n in sorted(decls) if (decls[n].get("kind") or "api") == "api"]
    names.sort(key=lambda n: (order.index(n) if n in order else len(order), n))
    return [{"id": FAKE_API_PROVIDER_IDS.get(n) or fake_api_provider_id(n), "name": n}
            for n in names]


class FakeTransport:
    """一个最小 Dify Console 假服务器：只实现本次要用的路由。

    ★ W23：必须传 binding —— 插件面由 `declared_plugin_rows(binding)` 声明驱动派生。
      不传 binding 时**不伪造任何插件面**（宁可让调用方拿到空面并 Fail，
      也不要再退回「写死某个插件 id」的老路）。
    """

    def __init__(self, binding=None):
        self.cookies = common.CookieJar()
        self.cookies.set("csrf_token", "fake-csrf")
        self.cookies.set("access_token", "fake-token")
        # ★ S2c：api provider 面同样**声明驱动**（注册面新增 cc-live-facts-tools 后，
        #   夹具必须自动有它的面，否则 --selftest 会因夹具缺面而假红）。
        self.api_rows = declared_api_rows(binding) if binding is not None else []
        self.provider_ids = {r["name"]: r["id"] for r in self.api_rows}
        # kind=plugin 的 provider 面（真机形态：id/name 为斜杠形态的 builtin provider，plugin_id 为 2 段）
        self.builtin_providers = declared_plugin_rows(binding) if binding is not None else []
        self.apps = {
            "GaussDB运维Agent-存算分离版V3": {
                "id": TEMPLATE_APP_ID, "name": "GaussDB运维Agent-存算分离版V3",
                "mode": APP_MODE_AGENT_CHAT,
                "model_config": self._template_mc(),
            },
        }
        self.calls = []
        self._seq = 0

    # -- 假模板：与真机同构的 24 键（元数据键会被控制器忽略，但读取时存在）--
    def _template_mc(self):
        return {
            "agent_mode": {"enabled": True, "max_iteration": 40, "strategy": "react", "prompt": None,
                           "tools": []},
            "annotation_reply": {"enabled": False},
            "chat_prompt_config": {"prompt": [{"role": "system", "text": ""}]},
            "completion_prompt_config": {"conversation_histories_role": {"assistant_prefix": "", "user_prefix": ""},
                                        "prompt": {"text": ""}},
            "created_at": 1790598242,
            "created_by": "u-tpl",
            "dataset_configs": {"datasets": {"datasets": []}, "retrieval_model": "single", "top_k": 4},
            "dataset_query_variable": "",
            "external_data_tools": [],
            "file_upload": {"allowed_file_extensions": [], "allowed_file_types": [],
                            "allowed_file_upload_methods": ["remote_url", "local_file"], "enabled": False,
                            "image": {"detail": "high", "enabled": False, "number_limits": 3,
                                      "transfer_methods": ["remote_url", "local_file"]},
                            "number_limits": 3},
            "model": {"completion_params": {"stop": [], "temperature": 0.7}, "mode": "chat",
                      "name": "gpt-4o-mini", "provider": "openai"},
            "more_like_this": {"enabled": False},
            "opening_statement": "",
            "pre_prompt": "模板提示词（非本任务正文）",
            "prompt_type": "simple",
            "retriever_resource": {"enabled": False},
            "sensitive_word_avoidance": {"enabled": False},
            "speech_to_text": {"enabled": False},
            "suggested_questions": [],
            "suggested_questions_after_answer": {"enabled": True},
            "text_to_speech": {"enabled": False, "language": "", "voice": ""},
            "updated_at": 1790598242,
            "updated_by": "u-tpl",
            "user_input_form": [],
        }

    # -- 假控制器语义：只要 related_config_keys 里的键（这里简化为「我们认识的键」）--
    _PERSISTED = (
        "model", "prompt_type", "pre_prompt", "chat_prompt_config", "completion_prompt_config",
        "agent_mode", "opening_statement", "suggested_questions_after_answer", "speech_to_text",
        "text_to_speech", "retriever_resource", "dataset_configs", "dataset_query_variable",
        "sensitive_word_avoidance", "file_upload", "user_input_form",
    )
    # 回读时由模型/展示层补出的键（与真机 to_dict 行为一致）
    _DISPLAY = {"annotation_reply": {"enabled": False}, "more_like_this": {"enabled": False},
                "external_data_tools": [], "suggested_questions": []}

    def _persist(self, cfg, mid, mby):
        out = {}
        for k in self._PERSISTED:
            if k in cfg:
                out[k] = cfg[k]
        for k, v in self._DISPLAY.items():
            out.setdefault(k, v)
        out.setdefault("retriever_resource", {"enabled": False})
        out["created_at"] = 1790598242
        out["created_by"] = mid
        out["updated_at"] = 1790598243
        out["updated_by"] = mby
        return out

    def request(self, method, url, json_body=None, headers=None, timeout=30):
        method = method.upper()
        self.calls.append((method, url))
        path = "/" + url.split("://", 1)[-1].partition("/")[2]   # ★ R-58：不硬编码部署主机（原值为 192.0.2.11，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
        if path.startswith("/console/api/login"):
            return _Resp(200, {"result": "success"})
        if path.startswith("/console/api/workspaces/current/tool-providers"):
            ptype = "api" if "type=api" in path else ("builtin" if "type=builtin" in path else "api")
            if ptype == "builtin":
                return _Resp(200, {"data": [dict(bp) for bp in self.builtin_providers]})
            return _Resp(200, {"data": [{"name": n, "id": i, "type": "api"}
                                        for n, i in sorted(self.provider_ids.items())]})
        if path.startswith("/console/api/workspaces/current/tool-provider/builtin/"):
            rest = path[len("/console/api/workspaces/current/tool-provider/builtin/"):]
            rest = rest[: -len("/tools")] if rest.endswith("/tools") else rest
            from urllib.parse import unquote
            pid = unquote(rest)
            for bp in self.builtin_providers:
                if bp["id"] == pid:
                    return _Resp(200, [{"name": n} for n in bp["tools"]])
            return _Resp(404, {"message": "unknown builtin provider"})
        if path.startswith("/console/api/apps?"):
            return _Resp(200, {"data": [{"id": a["id"], "name": a["name"], "mode": a["mode"]}
                                       for a in self.apps.values()], "total": len(self.apps)})
        if path == "/console/api/apps" and method == "POST":
            body = dict(json_body or {})
            if body.get("name") in self.apps:
                return _Resp(400, {"message": "duplicate name"})
            self._seq += 1
            uuid = "fake-%04d-0000-0000-0000-000000000000" % self._seq
            self.apps[body.get("name")] = {
                "id": uuid, "name": body.get("name"), "mode": body.get("mode") or "chat",
                "model_config": self._persist({}, "u-fake", "u-fake"),
            }
            return _Resp(201, {"id": uuid, "name": body.get("name"), "mode": body.get("mode")})
        if path.startswith("/console/api/apps/"):
            rest = path[len("/console/api/apps/"):]
            if rest.endswith("/model-config") and method == "POST":
                uuid = rest[: -len("/model-config")]
                for a in self.apps.values():
                    if a["id"] == uuid:
                        a["model_config"] = self._persist(dict(json_body or {}), "u-fake", "u-fake")
                        return _Resp(200, {"result": "success"})
                return _Resp(404, {"message": "not found"})
            if method == "GET":
                for a in self.apps.values():
                    if a["id"] == rest:
                        return _Resp(200, {"id": a["id"], "name": a["name"], "mode": a["mode"],
                                           "model_config": a["model_config"]})
                return _Resp(404, {"message": "not found"})
        return _Resp(404, {"message": "unhandled %s %s" % (method, path)})

    @property
    def faces(self) -> dict:
        """与 list_provider_faces 同构的 provider 面（api + builtin 两个命名空间）。"""
        return list_provider_faces(common.Console("http://127.0.0.1", transport=self))   # ★ R-58：不硬编码部署主机（原值为 192.0.2.11，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。


def _need(cond, label):
    if not cond:
        raise AssertionError("自检失败：%s" % label)


def _need_raises(exc, fn, label):
    try:
        fn()
    except exc:
        return
    except Exception as other:  # noqa: BLE001
        raise AssertionError("自检失败：%s 抛出了 %r 而非 %s" % (label, other, exc.__name__))
    raise AssertionError("自检失败：%s 未拒绝（没有抛 %s）" % (label, exc.__name__))


# --- ★ R-55：BASE 臂与「冻结差异」的机械复核 --------------------------------
def recompute_frozen_diff(base_text: str, var_text: str) -> list:
    """按 F5 的同一约定（difflib opcodes）复算变体相对基准的行级差异。

    返回只含非 equal 的 hunk：``{kind, baseline_line:[i1,i2], variant_line:[j1,j2],
    variant_text:[...]}``；行号 1-based、含两端 —— 与 `kgt-prompt-variants.yaml` 的约定一致。
    """
    import difflib

    sm = difflib.SequenceMatcher(None, base_text.split("\n"), var_text.split("\n"),
                                 autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        out.append({"kind": tag, "baseline_line": [i1 + 1, i2],
                    "variant_line": [j1 + 1, j2],
                    "variant_text": var_text.split("\n")[j1:j2]})
    return out


def _normalize_hunk(h: dict) -> dict:
    """把 difflib 的原始 opcode 归一到 `kgt-prompt-variants.yaml` 的**声明约定**。

    两条约定差异（文件头已写明，实测确认）：
      * `insert` → `add`（v1.1 记为 added，本版统一记 add）；
      * `add` 的 `baseline_line` 是**标量** = 「插入点之前的基准行数」（0 = 插在文件头），
        而 difflib 给的是 `[i1+1, i2]`（i1 == i2）⇒ 取 `i1`。
    `replace` / `delete` 仍用 `[起始, 结束]`（1-based 含两端）。
    """
    if h["kind"] == "insert":
        return {"kind": "add", "baseline_line": h["baseline_line"][0] - 1,
                "variant_line": h["variant_line"], "variant_text": h["variant_text"]}
    return dict(h)


def frozen_diff_problems(base_text: str, var_text: str, declared: list) -> list:
    """声明清单 vs 实算 hunk 的逐条比对（kind / 行号区间 / 正文全等）。

    ★ R-55：`variant_text` 允许为**字符串**（单行；方案3 家族既有约定）或**行列表**
    （多行等长替换）。两者都归一化为行列表后比较。
    """
    actual = [_normalize_hunk(h) for h in recompute_frozen_diff(base_text, var_text)]
    declared = declared or []
    if len(actual) != len(declared):
        return ["实算 hunk 数 %d != 声明 %d" % (len(actual), len(declared))]
    problems = []
    for a, d in zip(actual, declared):
        vt = d.get("variant_text")
        vt = [vt] if isinstance(vt, str) else list(vt or [])
        for k in ("kind", "baseline_line", "variant_line"):
            if a[k] != d.get(k):
                problems.append("%s 的 %s：实算 %r != 声明 %r"
                                % (d.get("id"), k, a[k], d.get(k)))
        if a["variant_text"] != vt:
            problems.append("%s 的 variant_text 与实算不一致（实算 %d 行 / 声明 %d 行）"
                            % (d.get("id"), len(a["variant_text"]), len(vt)))
    return problems


def cmd_selftest(args) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    binding = load_declaration(args.binding)
    checks = []

    # 0) 提示词基准（真机文件在本地即可核对）
    p = verify_prompt_file()
    _need(not p, "提示词基准应可用：%s" % p)
    prompt = prompt_text()
    prompt_sha = common.sha256_bytes(prompt.encode("utf-8"))
    checks.append("prompt baseline %d bytes/%d LF/%s" % (PROMPT_BYTES, PROMPT_LF, prompt_sha[:16]))

    # 1) 声明面
    targets = target_apps(binding, args.apps)
    _need(len(targets) == 2, "默认目标应为两个主对比组 App")
    for name, cfg in targets:
        _need(expected_tool_count(binding, cfg) == MAIN_COMPARISON_TOOL_COUNT,
              "%s 工具数应为 %d" % (name, MAIN_COMPARISON_TOOL_COUNT))
    for _n, _cfg in targets:
        _need(declared_providers(binding, _n, _cfg) == [_cfg["provider"]],
              "主对比对 %s 应恰好声明 1 个 provider（不得放宽）" % _n)
    checks.append("declaration: %s" % [n for n, _ in targets])

    # 2) 建 App payload：恰好 6 字段、agent-chat、**身份逐字取自声明面**（★ R-59）
    for name, _cfg in targets:
        pl = build_create_payload(binding, name)
        _need(sorted(pl) == sorted(CREATE_FIELDS), "%s payload 应恰好 6 字段" % name)
        _need(not validate_create_payload(binding, name, pl), "%s payload 应合法" % name)
        dn, dd = declared_identity(binding, name)
        _need(pl["name"] == dn, "%s payload name 应逐字等于声明 display_name" % name)
        _need(pl["description"] == dd,
              "%s payload description 应逐字等于声明 display_description" % name)
        _need(len(dd) <= APP_DESCRIPTION_MAX, "%s 描述应在 Dify 上限内" % name)
    checks.append("create payload: 6 fields, mode=agent-chat, identity verbatim from declaration")

    # 2b) ★ R-59 反恒真：身份面唯一性（正控）
    all_apps = target_apps(binding, sorted((binding.get("apps") or {}).keys()))
    _need(not validate_identity_uniqueness(binding),
          "声明面所有 App 的身份应非空且两两不同：%s" % validate_identity_uniqueness(binding))
    descs = [build_create_payload(binding, n)["description"] for n, _ in all_apps]
    _need(len(set(descs)) == len(descs),
          "所有 App 的建载荷描述应两两不同（实际 %d 个唯一 / %d 个 App）" % (len(set(descs)), len(descs)))
    names = [build_create_payload(binding, n)["name"] for n, _ in all_apps]
    _need(len(set(names)) == len(names), "所有 App 的建载荷 name 应两两不同")
    checks.append("identity uniqueness: %d apps 的描述两两不同（用户实测缺陷的反恒真断言）" % len(all_apps))

    # 2c) ★ R-59 负控：把两个 App 的描述改成同一句 ⇒ 必须被唯一性断言抓住
    b_dup = copy.deepcopy(binding)
    keys = sorted((b_dup.get("apps") or {}).keys())
    b_dup["apps"][keys[1]]["display_description"] = b_dup["apps"][keys[0]]["display_description"]
    _need(bool(validate_identity_uniqueness(b_dup)),
          "负控：两个 App 描述相同时也应被拒绝")
    # 2d) ★ R-59 负控：清空一个 App 的描述 ⇒ declared_identity 必须 fail-closed（不回落兜底）
    b_empty = copy.deepcopy(binding)
    b_empty["apps"][keys[0]]["display_description"] = ""
    try:
        declared_identity(b_empty, keys[0])
        _need(False, "负控：缺 display_description 时应 fail-closed")
    except Fail:
        pass
    _need(bool(validate_identity_uniqueness(b_empty)), "负控：空描述应被唯一性断言点出")
    checks.append("neg(R-59): 重复描述 / 空描述 均被拒（不回落兜底文本）")

    # 3) 离线端到端：FakeTransport → 建两个 App → 写 model-config → 回读
    # ★ W23：夹具声明驱动 —— 插件面由 binding 的 kind=plugin 声明派生（不写死任何插件 id）
    transport = FakeTransport(binding)
    console = common.console_client(repo_root=REPO_ROOT, transport=transport)
    live = collect_live(console, binding)
    template_mc = get_app(console, resolve_template_app_id(console))["model_config"]
    made = {}
    for name, cfg in targets:
        pl = build_create_payload(binding, name)
        _need(not validate_create_payload(binding, name, pl), "payload 合法")
        resp = console.post_json("/console/api/apps", pl, expect=(200, 201))
        uuid = resp["id"]
        made[name] = uuid
        det = get_app(console, uuid)
        _need(det["mode"] == APP_MODE_AGENT_CHAT, "%s 应为 agent-chat" % name)
        # ★ R-59：建 App 后真机 name 应是**声明 display_name**（不再是声明键）
        _need(det.get("name") == pl["name"],
              "%s 建后 name=%r 应等于声明 display_name=%r" % (name, det.get("name"), pl["name"]))
        tools = tool_entries(binding, name, cfg, live["provider_faces"])
        want = build_model_config(prompt, tools, template_mc)
        _need(not validate_model_config(binding, name, cfg, want, live["provider_faces"], prompt),
              "%s 待写 model-config 应合法" % name)
        console.post_json("/console/api/apps/%s/model-config" % uuid, want, expect=(200, 201))
        after = get_app(console, uuid)["model_config"]
        probs = validate_model_config(binding, name, cfg, after, live["provider_faces"], prompt)
        _need(not probs, "%s 回读应合法：%s" % (name, probs))
    checks.append("e2e(fake): created %s" % made)

    # 3b) 幂等：第二次 apply 不重复 POST
    writes = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/model-config")]
    _need(len(writes) == 2, "首次应写 2 次 model-config，实际 %d" % len(writes))
    for name, cfg in targets:
        uuid = made[name]
        want = build_model_config(prompt, tool_entries(binding, name, cfg, live["provider_faces"]), template_mc)
        _need(subset_equal(get_app(console, uuid)["model_config"], want), "%s 应已处于目标状态" % name)
    checks.append("idempotent: subset_equal 为真 ⇒ 第二次不再写")

    # 3c) 两侧 diff：只有 agent_mode 允许不同
    a = get_app(console, made[targets[0][0]])["model_config"]
    b = get_app(console, made[targets[1][0]])["model_config"]
    _need(sorted(a) == sorted(b), "两侧键集合应相同")
    uneq = [k for k in a if not same_json(a[k], b[k])]
    _need(uneq == ["agent_mode"], "两侧仅 agent_mode 应不同，实际 %s" % uneq)
    _need(same_json(a["model"], b["model"]), "两侧 model 应逐字相同")
    _need(a["pre_prompt"] == b["pre_prompt"], "两侧 pre_prompt 应逐字相同")
    checks.append("pair diff: only agent_mode differs")

    # 4) 负控
    def _payload_with(**over):
        pl = build_create_payload(binding, targets[0][0])
        pl.update(over)
        return pl

    _need(validate_create_payload(binding, targets[0][0], _payload_with(mode="advanced-chat")),
          "负控：非 agent-chat 模式应被拒绝")
    _need(validate_create_payload(binding, targets[0][0], _payload_with(mode="workflow")),
          "负控：workflow 模式应被拒绝")
    _need(validate_create_payload(binding, targets[0][0], _payload_with(extra_field=1)),
          "负控：建 App 多字段应被拒绝")
    _need(validate_create_payload(binding, targets[0][0], _payload_with(description="被篡改的描述")),
          "负控：description 偏离声明面应被拒绝（★ R-59）")
    checks.append("neg: mode!=agent-chat / extra create field 被拒")

    good_cfg = targets[0][1]
    good_tools = tool_entries(binding, "x", good_cfg, transport.faces)
    good_mc = build_model_config(prompt, good_tools, template_mc)
    _need(not validate_model_config(binding, "x", good_cfg, good_mc, transport.faces, prompt),
          "正控：目标 model-config 应合法")

    bad = copy.deepcopy(good_mc)
    bad["pre_prompt"] = bad["pre_prompt"] + "x"
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：pre_prompt 与文件不一致应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["model"] = dict(R37_MODEL, completion_params={"temperature": 0.7, "stop": []})
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：model 块与 R-37 不符应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["model"] = dict(R37_MODEL, name="other-model")
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：model.name 不符应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["tools"] = good_tools[:-2]        # ★ P3：收窄后为 5 ⇒ 截到 3 才算真截断
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：tool 数 ≠ %d 应被拒绝" % MAIN_COMPARISON_TOOL_COUNT)

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["tools"] = good_tools + [copy.deepcopy(good_tools[0])]
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：tool 数 > %d 应被拒绝" % MAIN_COMPARISON_TOOL_COUNT)

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["tools"][0]["tool_parameters"] = {"secret": "x"}
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：tool_parameters 非空应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["tools"][0]["enabled"] = False
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：工具未启用应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["strategy"] = "cot"
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：strategy 非 react 应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["max_iteration"] = 8
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：max_iteration 非 40 应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["prompt"] = "想想再答"
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：agent_mode.prompt 非空应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["function_calling_type"] = "no_call"
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：注入 function_calling_type 应被拒绝")
    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["function_calling_type"] = "no_call"
    _need(function_calling_hits(bad) == ["agent_mode.function_calling_type"],
          "负控：嵌套 function_calling_type 应被检出")
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：嵌套注入 function_calling_type 应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["agent_mode"]["tools"][0]["provider_id"] = "00000000-0000-0000-0000-000000000000"
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：tool provider_id 与声明面不符应被拒绝")

    bad = copy.deepcopy(good_mc)
    bad["dataset_configs"] = {"datasets": {"datasets": [{"id": "d1"}]}}
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：dataset_configs 偏离默认应被拒绝")

    bad = copy.deepcopy(good_mc)
    del bad["agent_mode"]["prompt"]
    _need(validate_model_config(binding, "x", good_cfg, bad, transport.faces, prompt),
          "负控：agent_mode 缺 prompt 键应被拒绝（F4.c 会记缺失）")

    _need(len(good_mc["agent_mode"]["tools"]) == MAIN_COMPARISON_TOOL_COUNT,
          "正控：工具应恰为 %d" % MAIN_COMPARISON_TOOL_COUNT)
    checks.append("neg: prompt/model/toolcount/toolparams/enabled/strategy/maxiter/"
                  "prompt-nonempty/fct/provider_id/dataset/缺失键 共 16 项")

    # 5) tool_entries 缺 provider id ⇒ 拒绝
    _need_raises(Fail, lambda: tool_entries(binding, "x", good_cfg, {}),
                 "负控：live provider id 缺失")

    # 6) 模板 model_config 为空 ⇒ 拒绝（不得凭记忆拼键）
    _need_raises(Fail, lambda: build_model_config(prompt, good_tools, {}),
                 "负控：模板 model_config 为空")

    # 7) binding apps 段回写（临时文件，不碰真 binding）
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "b.yaml"
        src = Path(args.binding).read_text(encoding="utf-8")
        tmp.write_text(src, encoding="utf-8", newline="")
        uuids = {n: "11111111-2222-3333-4444-555555555555" for n, _ in targets}
        statuses = {n: W2_STATUS for n, _ in targets}
        changed, new_text, details = update_binding_uuids(tmp, uuids, statuses)
        _need(changed, "uuid 回写应产生变更")
        back = yaml.safe_load(new_text)
        for n, _ in targets:
            _need(back["apps"][n]["uuid"] == "11111111-2222-3333-4444-555555555555",
                  "%s uuid 应被写入" % n)
            _need(back["apps"][n]["status"] == W2_STATUS, "%s status 应被更新" % n)
        _need(back["counts"]["setb_tools"] == binding["counts"]["setb_tools"],
              "回写不应破坏其它段（真值 %s）" % binding["counts"]["setb_tools"])
        _need(sorted(back["apps"]) == sorted(binding["apps"]), "回写不应增删 app 条目")
        # ★ R-52：compare_pairs 现含多个对照对（Q 对 + R 对）⇒ 断言「回写后与源文件逐字相同」。
        #   这比按 targets 重建那份单一对照对更强，且不随对照对数量变化而失效。
        _need(back["fairness"] == binding["fairness"], "回写不应触碰 fairness 段")
        for n in set(binding["apps"]) - set(uuids):
            _need(back["apps"][n]["status"] == binding["apps"][n]["status"],
                  "范围外 App %s 的 status 不应被改" % n)
            # ★ 与源文件逐字比对（而不是断言「必须为 None」）：真 binding 里 KG-T 家族等
            # 范围外 App 在 W3 落真机后已带 uuid，断言 None 会把合法状态误判为自检失败。
            _need(back["apps"][n].get("uuid") == binding["apps"][n].get("uuid"),
                  "范围外 App %s 的 uuid 不应被改" % n)
        # 幂等：对自身输出再跑一次，必须完全无变更
        tmp.write_text(new_text, encoding="utf-8", newline="")
        again, new_text2, _d2 = update_binding_uuids(tmp, uuids, statuses)
        _need(not again, "负控反向：对自身输出再回写必须 changed=False（幂等）")
        _need(new_text2 == new_text, "幂等回写文本应逐字不变")
        # 负控：不给 status 时不得吞掉既有 status 行
        _c3, t3, _d3 = update_binding_uuids(
            tmp, {"cc-kgt-agent": "99999999-0000-0000-0000-000000000000"}, None)
        b3 = yaml.safe_load(t3)
        _need(b3["apps"]["cc-kgt-agent"]["status"] == binding["apps"]["cc-kgt-agent"]["status"],
              "未给 status 时应原样保留既有 status 行")
        _need(b3["apps"]["cc-kgt-agent"]["uuid"] == "99999999-0000-0000-0000-000000000000",
              "应写入 uuid")
        _need(b3["apps"]["cc-kg-agent"]["uuid"] == "11111111-2222-3333-4444-555555555555",
              "其它 App 的 uuid 不应被动")
    checks.append("binding apps writeback: uuid+status 写入 / 幂等 / 范围外不动 / YAML 可解析")

    # 7b) ★ R-52：规则臂（R 臂）声明面不变量 —— 1 条正控 + 3 条负控（负控必须真能失败）
    _need(not validate_rule_arm_faces(binding), "正控：真 binding 的规则臂不变量应成立")
    r52_neg = []
    for r52_label, r52_mut in (
        ("R 臂 tools 仍含 cc_query（没有真正移除）",
         lambda b: b["apps"]["cc-kg-r-agent"].__setitem__("tools", list(b["apps"]["cc-kg-agent"]["tools"]))),
        ("R 臂 tools 与移除集不自洽（少给了工具）",
         lambda b: b["apps"]["cc-onto-r-agent"].__setitem__("tools", ["cc_guide", "cc_schema", "cc_facts"])),
        ("R 臂移除集新增了未从 tools 移除的工具（口径自相矛盾）",
         lambda b: b["apps"]["cc-kg-r-agent"].__setitem__(
             "tools_removed_from_setb", ["cc_query", "cc_trace"])),
    ):
        bb = copy.deepcopy(binding)
        r52_mut(bb)
        _need(validate_rule_arm_faces(bb), "负控：%s 应被拒绝" % r52_label)
        r52_neg.append(r52_label)
    checks.append("R-52 规则臂不变量：1 正控 + %d 负控" % len(r52_neg))

    # 8) M4.7 / W3：KG-T 家族（cc-kgt-agent = 4 原生工具；cc-kgt-free-agent = 1 插件工具）
    # ★ U-N 裁定 = A（2026-10-08）：KG-T 家族现有**三个** App（主臂 / 免费臂 / 动态补采补充组）
    kgt_names = ["cc-kgt-agent", "cc-kgt-free-agent", "cc-kgt-dyn-agent"]
    kgt = target_apps(binding, kgt_names)
    _need([n for n, _ in kgt] == kgt_names, "KG-T 家族目标应为三个 App（含 dyn 补充组）")
    vface = load_prompt_variants()
    _need("prompt_file" not in ((vface.get("variants") or {}).get("KG-T-dyn") or {}),
          "KG-T-dyn 本轮必须没有 prompt_file（明确推迟，不得凭空造正文）")
    specs2 = prompt_specs(binding)
    for name, cfg in kgt:
        _need(name in specs2, "%s 必须在 kgt-prompt-variants.yaml 里有 prompt 规格" % name)
        spec = prompt_spec_for(name, binding)
        _need(not verify_prompt_file_spec(spec), "%s prompt 文件应逐字节符合声明" % name)
        _need(spec["sha256"] != PROMPT_SHA256, "%s prompt 必须不是主对比组 baseline" % name)
        t = prompt_text_of(spec)
        _need(common.sha256_bytes(t.encode("utf-8")) == spec["sha256"], "%s prompt sha256 应自洽" % name)
        ents = tool_entries(binding, name, cfg, transport.faces)
        _need(len(ents) == expected_tool_count(binding, cfg),
              "%s 工具数应 %d" % (name, expected_tool_count(binding, cfg)))
        _need(all(e["tool_parameters"] == {} and e["enabled"] is True for e in ents), "%s 工具条目默认值" % name)
    _need(prompt_spec_for("cc-kgt-agent", binding)["variant"] == "KG-T", "cc-kgt-agent 应用 KG-T 变体")
    _need(prompt_spec_for("cc-kgt-free-agent", binding)["variant"] == "KG-T-free",
          "cc-kgt-free-agent 应用 KG-T-free 变体")

    main_arm = tool_entries(binding, "cc-kgt-agent", kgt[0][1], transport.faces)
    _need(sorted(e["tool_name"] for e in main_arm)
          == sorted(["get_object_topology", "get_fault_topology", "get_metrics", "get_facts",
                     "read_facts"]),
          "cc-kgt-agent 应绑 5 个原生工具（本批新增 read_facts）")
    _need(all(e["provider_type"] == "api" and e["type"] == "api" for e in main_arm),
          "cc-kgt-agent 的 provider 面应全为 api")
    _need("cc_query" not in [e["tool_name"] for e in main_arm], "cc-kgt-agent 不得含 cc_query")

    free_arm = tool_entries(binding, "cc-kgt-free-agent", kgt[1][1], transport.faces)
    _need(sorted(e["tool_name"] for e in free_arm) == sorted(["neo4j_query", "read_live_facts"]),
          "cc-kgt-free-agent 应恰绑 2 个工具：插件 neo4j_query + 中性取数 read_live_facts")
    # ★ W23：插件面取值**对声明比对**（不再写死某个插件的字面量）
    free_prov = kgt[1][1]["provider"]
    free_decl = provider_decls(binding)[free_prov]
    _need(free_decl.get("kind") == "plugin",
          "cc-kgt-free-agent 的历史单键 provider %s 应声明为 kind=plugin" % free_prov)
    free_provs = declared_providers(binding, "cc-kgt-free-agent", kgt[1][1])
    _need(free_provs == [free_prov, "cc-live-facts-tools"],
          "免费臂应声明 2 个 provider（插件 + 中性取数 api），实际 %s" % free_provs)
    _free_by_name = {e["tool_name"]: e for e in free_arm}
    _plug = _free_by_name["neo4j_query"]
    _need(_plug["provider_id"] == free_decl["provider_id"]
          and _plug["provider_type"] == PLUGIN_PROVIDER_TYPE
          and _plug["type"] == PLUGIN_PROVIDER_TYPE,
          "插件条目的 provider 面应取真机 builtin 回读值：%s"
          % common.stable_json({k: _plug[k] for k in ("provider_id", "provider_type", "type")}))
    _api_entry = _free_by_name["read_live_facts"]
    _api_face = find_provider_face(binding, "cc-live-facts-tools", transport.faces)
    _need(_api_entry["provider_id"] == _api_face["id"]
          and _api_entry["provider_name"] == "cc-live-facts-tools"
          and _api_entry["provider_type"] == "api" and _api_entry["type"] == "api",
          "read_live_facts 条目应取 cc-live-facts-tools 的 api 面：%s"
          % common.stable_json({k: _api_entry[k]
                                for k in ("provider_id", "provider_name", "provider_type", "type")}))
    _need(tool_provider_of(binding, "cc-kgt-free-agent", kgt[1][1], "neo4j_query") == free_prov
          and tool_provider_of(binding, "cc-kgt-free-agent", kgt[1][1], "read_live_facts")
          == "cc-live-facts-tools",
          "逐工具 provider 归属应分别解析到插件面与 :8304 api 面")
    _need(sorted(r["name"] for r in transport.api_rows)
          == sorted(n for n, d in provider_decls(binding).items()
                    if (d.get("kind") or "api") == "api"),
          "夹具 api 面应与声明的 kind=api provider 集合逐条相同（声明驱动，不写死个数）")
    checks.append("kgt arms: cc-kgt-agent=5 native(api) / "
                  "cc-kgt-free-agent=neo4j_query(builtin)+read_live_facts(api@8304) / "
                  "fixture api rows 声明驱动")

    # 8b) KG-T 家族负控
    _need_raises(Fail, lambda: tool_entries(binding, "x", kgt[1][1],
                                            {"by_id": {}, "by_name": {}, "by_plugin_id": {}}),
                 "负控：真机缺插件 provider")
    # ★ U-N 裁定 = A（2026-10-08）：**本条负控已按设计失效并被替换** ——
    #   原断言是「无 prompt 规格的 App（KG-T-dyn）应被拒绝」，因为那时 dyn 臂**没有正文**。
    #   现在 dyn 的正文由 `derive_variant_text()` 在**内存里派生**（F5 的 E 段仍断言它
    #   「无独立文件」）⇒ 它**应当**能解析出规格。故改为：
    #     ① 正控：dyn 规格存在、标记 derived、且派生指纹与声明一致；
    #     ② 负控：换成**真正**没有规格的名字，仍须被拒（fail-closed 不放宽）。
    _dynspec = prompt_spec_for("cc-kgt-dyn-agent", binding)
    _need(_dynspec.get("derived") is True and _dynspec.get("path") is None,
          "KG-T-dyn 的 prompt 规格应为**派生**（无文件）")
    _need(not verify_prompt_file_spec(_dynspec),
          "KG-T-dyn 的派生正文指纹应符合声明：%s" % verify_prompt_file_spec(_dynspec))
    _need(common.sha256_bytes(prompt_text_of(_dynspec).encode("utf-8")) == _dynspec["sha256"],
          "KG-T-dyn 派生正文 sha256 应自洽")
    _need_raises(Fail, lambda: prompt_spec_for("cc-no-such-agent", binding),
                 "负控：真正无 prompt 规格的 App 名仍应被拒绝")
    bad_spec = dict(prompt_spec_for("cc-kgt-agent", binding), bytes=5034)
    _need(verify_prompt_file_spec(bad_spec), "负控：prompt bytes 与声明不符应被检出")
    bad_spec = dict(prompt_spec_for("cc-kgt-agent", binding), sha256="0" * 64)
    _need(verify_prompt_file_spec(bad_spec), "负控：prompt sha256 与声明不符应被检出")
    _need_raises(Fail, lambda: prompt_spec_for("cc-kg-agent"),
                 "负控：baseline 规格必须经 binding 展开（不传 binding 时应拒绝）")

    faces2 = transport.faces
    # ★ W23：用**声明里的** plugin_id 定位真机工具面（不再写死市场插件字面量）
    _need(free_decl["plugin_id"] in faces2["by_plugin_id"],
          "真机面里应能按声明 plugin_id %s 命中插件面" % free_decl["plugin_id"])
    faces2["by_plugin_id"][free_decl["plugin_id"]]["exposed_tools"] = list(kgt[1][1]["tools"])
    free_mc = build_model_config(prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding)),
                                 free_arm, template_mc)
    _need(not validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], free_mc, faces2,
                                    prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))),
          "正控：插件臂 model-config 应合法")
    bad = copy.deepcopy(free_mc)
    bad["agent_mode"]["tools"][0]["provider_type"] = "api"
    _need(validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], bad, faces2,
                                prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))),
          "负控：插件条目写成 api 形态应被拒绝")
    bad = copy.deepcopy(free_mc)
    bad["agent_mode"]["tools"][0]["tool_name"] = "cc_query"
    bad["agent_mode"]["tools"][0]["tool_label"] = "cc_query"
    _need(validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], bad, faces2,
                                prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))),
          "负控：插件臂声明外来工具应被拒绝")
    bad = copy.deepcopy(free_mc)
    bad["agent_mode"]["tools"][0]["provider_id"] = "00000000-0000-0000-0000-000000000000"
    _need(validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], bad, faces2,
                                prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))),
          "负控：插件 provider_id 非真机值应被拒绝")
    bad = copy.deepcopy(free_mc)
    bad["agent_mode"]["tools"].append(copy.deepcopy(main_arm[0]))
    _need(validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], bad, faces2,
                                prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))),
          "负控：插件臂混入 API 工具应被拒绝")
    # ★ S2c：多 provider 语义的四种非法形态都必须 Fail（fail-closed）
    #   （注意：provider == providers[0] 是**合法**形态；负控必须让两者不一致）
    _providers_mismatch = {"provider": "cc-kgt-tools",
                           "providers": ["neo4j_query_fork", "cc-kgt-tools"],
                           "tools": ["neo4j_query"]}
    _need_raises(Fail, lambda: declared_providers(binding, "cc-kgt-free-agent",
                                                  _providers_mismatch),
                 "负控：provider 与 providers 不一致应被拒绝")
    _two_p = {"provider": "cc-kg-tools", "providers": ["cc-kg-tools", "cc-onto-tools"]}
    _need_raises(Fail, lambda: declared_providers(binding, "cc-kg-agent", _two_p),
                 "负控：主对比对 App 声明 2 个 provider 应被拒绝（恰好 1 不得放宽）")
    _none_own = {"provider": "cc-live-facts-tools", "tools": ["cc_query"]}
    _need_raises(Fail, lambda: tool_provider_of(binding, "x", _none_own, "cc_query"),
                 "负控：工具不属于声明集合里任何 provider 时应被拒绝")
    _two_own = {"providers": ["cc-kg-tools", "cc-onto-tools"], "tools": ["cc_query"]}
    _need_raises(Fail, lambda: tool_provider_of(binding, "x", _two_own, "cc_query"),
                 "负控：工具同时属于 2 个声明 provider 时应被拒绝")
    _empty_p = {"providers": [], "tools": ["cc_query"]}
    _need_raises(Fail, lambda: declared_providers(binding, "x", _empty_p),
                 "负控：providers 为空列表应被拒绝")
    _dup_p = {"providers": ["cc-kg-tools", "cc-kg-tools"], "tools": ["cc_query"]}
    _need_raises(Fail, lambda: declared_providers(binding, "x", _dup_p),
                 "负控：providers 重复项应被拒绝")
    checks.append("kgt neg: 缺 provider/缺 prompt 规格/bytes/sha256/plugin-as-api/"
                  "外来工具/provider_id/混入 API 工具/多 provider 六种非法形态 共 13 项")

    # 8c) ★ W23：插件 face 分支（声明驱动夹具的正控 + 三条负控）
    #     —— 这一组的存在意义：证明夹具是**由声明派生**的，而不是把插件分支短路掉。
    free_prompt = prompt_text_of(prompt_spec_for("cc-kgt-free-agent", binding))
    fixture_rows = transport.builtin_providers
    decl_rows = declared_plugin_rows(binding)
    _need(common.stable_json(sorted(fixture_rows, key=lambda r: r["id"]))
          == common.stable_json(sorted(decl_rows, key=lambda r: r["id"])),
          "夹具 builtin 面应逐条等于 binding 的 kind=plugin 声明（声明驱动，不得写死）"
          "：fixture=%s decl=%s" % (sorted(r["id"] for r in fixture_rows),
                                    sorted(r["id"] for r in decl_rows)))
    # 声明里**每一个** kind=plugin 的 provider 都必须能被夹具解析出带工具面的面（不写死个数）
    for _pname, _pdecl in provider_decls(binding).items():
        if (_pdecl.get("kind") or "api") != "plugin":
            continue
        _f = find_provider_face(binding, _pname, transport.faces)
        _need(_f["kind"] == "plugin" and _f["id"] == _pdecl["provider_id"],
              "声明的插件 %s 应能被夹具解析出 plugin 面且 id=%s" % (_pname, _pdecl["provider_id"]))

    live_faces = transport.faces
    _need(live_faces["by_plugin_id"].get(free_decl["plugin_id"], {}).get("id") == free_decl["provider_id"],
          "by_plugin_id 应命中声明 plugin_id %s" % free_decl["plugin_id"])
    _need(live_faces["by_id"].get(free_decl["provider_id"], {}).get("plugin_id") == free_decl["plugin_id"],
          "by_id 应命中声明 provider_id %s" % free_decl["provider_id"])
    _need(live_faces["by_name"].get(free_decl["provider_id"], {}).get("plugin_id") == free_decl["plugin_id"],
          "by_name 应命中声明 provider_id %s" % free_decl["provider_id"])
    _need(live_faces["by_name"].get(free_decl["plugin_unique_identifier"], {}).get("id")
          == free_decl["provider_id"],
          "by_name 应能按 plugin_unique_identifier 命中")
    # uid 反查分支（find_provider_face 第 3 条路径）：只带 uid 的声明也必须落到同一个面
    uid_only = {"providers": [{"provider": "synthetic", "kind": "plugin",
                               "plugin_unique_identifier": free_decl["plugin_unique_identifier"]}]}
    got_uid = find_provider_face(uid_only, "synthetic", live_faces)
    _need(got_uid["id"] == free_decl["provider_id"] and got_uid["kind"] == "plugin",
          "只带 plugin_unique_identifier 的 kind=plugin 声明应反查到同一插件面")

    # 负控①（回归 W23 根因）：夹具缺该插件的面时必须判红，而不是静默变绿
    market_only = {"by_id": {}, "by_name": {}, "by_plugin_id": {}}
    for r in decl_rows:
        if r["id"] == free_decl["provider_id"]:
            continue
        market_only["by_id"][r["id"]] = r
        market_only["by_name"][r["name"]] = r
        market_only["by_plugin_id"][r["plugin_id"]] = r
        market_only["by_name"][r["plugin_unique_identifier"]] = r
    _need_raises(Fail, lambda: find_provider_face(binding, free_prov, market_only),
                 "负控：夹具只含市场插件（W23 之前的夹具）时 fork 声明必须判红")

    # 负控②：插件 face 存在，但该插件**未提供**被声明的 tool 名 ⇒ 按设计 fail-closed
    no_tool_faces = transport.faces
    _need(free_decl["plugin_id"] in no_tool_faces["by_plugin_id"], "定位插件面（by_plugin_id）")
    no_tool_faces["by_plugin_id"][free_decl["plugin_id"]]["exposed_tools"] = ["__not_declared__"]
    p_no_tool = validate_model_config(binding, "cc-kgt-free-agent", kgt[1][1], free_mc,
                                      no_tool_faces, free_prompt)
    _need(len(p_no_tool) == 1 and "不在 provider" in p_no_tool[0] and "真机工具面" in p_no_tool[0],
          "负控：插件未提供被声明工具时应 fail-closed 且只报该问题，实际 %s" % p_no_tool)

    # 负控③：声明 kind=plugin 却只解析到 api 面（无 plugin_id / uid）⇒ 判红，不得退化成 api 绑定
    api_face = {"id": "11111111-2222-3333-4444-555555555555", "type": "api", "name": free_prov,
                "plugin_id": None, "plugin_unique_identifier": None, "provider_namespace": "api"}
    api_only = {"by_id": {api_face["id"]: api_face}, "by_name": {free_prov: api_face},
                "by_plugin_id": {}}
    _need_raises(Fail, lambda: find_provider_face(binding, free_prov, api_only),
                 "负控：声明 kind=plugin 却只有 api 面时不得退化成 api 绑定")
    _need_raises(Fail, lambda: find_provider_face(
        {"providers": [{"provider": "synthetic", "kind": "plugin",
                        "plugin_unique_identifier": "no/such_plugin:0.0.0@%s" % ("0" * 64)}]},
        "synthetic", live_faces),
        "负控：kind=plugin 的 uid 在真机面无对应行时必须判红")
    checks.append("plugin face(W23): 夹具声明驱动一致 / by_id+by_name+by_plugin_id+uid 四路命中；"
                  "负控：缺 fork 面 / 插件未提供被声明 tool / kind=plugin 只有 api 面 / uid 未知 共 4 项")

    # --- ★ R-55：BASE 臂（基线组/无知识库）与「冻结差异」的机械复核 ---------------
    base_spec = prompt_spec_for("cc-base-agent", binding)
    _need(base_spec["variant"] == "BASE", "cc-base-agent 应用 BASE 变体")
    _need(not verify_prompt_file_spec(base_spec), "cc-base-agent 的 prompt 应逐字节符合声明")
    _need(base_spec.get("declared_tools") == ["read_facts"],
          "BASE 声明的工具面应恰为 [read_facts]（无知识库 ⇒ 只有中性取数这一条数据面）")
    bcfg = (binding.get("apps") or {}).get("cc-base-agent") or {}
    _need(bool(bcfg), "binding.apps 必须有 cc-base-agent")
    _need(list(bcfg.get("tools") or []) == ["read_facts"], "cc-base-agent 只应绑 read_facts")
    _need(bcfg.get("provider") == "cc-base-tools", "cc-base-agent 的 provider 应为 cc-base-tools")
    _need(bcfg.get("count_key") == "neutral_tools", "cc-base-agent 应以 count_key=neutral_tools 声明")
    b_ents = tool_entries(binding, "cc-base-agent", bcfg, transport.faces)
    _need([e["tool_name"] for e in b_ents] == ["read_facts"],
          "cc-base-agent 应恰绑 1 个工具 read_facts，实际 %s" % [e["tool_name"] for e in b_ents])
    _need(b_ents[0]["provider_type"] == "api" and b_ents[0]["type"] == "api",
          "read_facts 条目应取 cc-base-tools 的 api 面")
    # ★ 通用：凡声明了 frozen_diff 的变体，实算差异必须与声明**逐条**相等
    base_text = PROMPT_PATH.read_bytes().decode("utf-8")
    n_mine, n_other = 0, 0
    for vname, vspec in (load_prompt_variants().get("variants") or {}).items():
        if not isinstance(vspec, dict) or not vspec.get("frozen_diff") or not vspec.get("prompt_file"):
            continue
        # ★ R-55：每个带 frozen_diff 的变体都必须**声明**由谁机械复核（fail-closed；
        #   防「有差异清单但无人校验」）。F5 负责方案3 家族；本脚本负责其余（如 BASE）。
        who = vspec.get("diff_verified_by")
        _need(who in ("f5_kgt_gate", "deploy_agents.selftest", "contract_gates"),
              "变体 %s 必须声明 diff_verified_by ∈ {f5_kgt_gate, deploy_agents.selftest,"
              " contract_gates}，实际 %r" % (vname, who))
        if who == "f5_kgt_gate":
            n_other += 1        # 由 F5 复核（其 hunk 约定与本处不同，不重复实现）
            continue
        vpath = REPO_ROOT / str(vspec["prompt_file"])
        _need(vpath.is_file(), "变体 %s 的 prompt_file 存在：%s" % (vname, vspec["prompt_file"]))
        probs = frozen_diff_problems(base_text, vpath.read_bytes().decode("utf-8"),
                                     vspec["frozen_diff"])
        _need(not probs, "变体 %s 的冻结差异应与实算逐条相等：%s" % (vname, probs[:3]))
        n_mine += 1
    _need(n_mine >= 1, "本脚本应至少复核 1 个变体（BASE 等非 F5 变体），实际 %d" % n_mine)
    _need(n_other >= 2, "F5 应负责至少 2 个变体（KG-T / KG-T-free），实际 %d" % n_other)
    # ★ BASE 专项：§1–§5 必须与基准**逐字相同**（公平性关键面）。
    #   ★ P3-fix（2026-10-15）：边界**不再硬编码「第 20 行」**——改为从 BASE 的
    #     `frozen_diff` **末条** hunk 的结束行推出。原写法把边界写成常量、并断言
    #     「BASE 与基准总行数相同（差异全为等长替换）」；一旦基准顶部增删行（本次
    #     把【第一步/第二步】改为「cc_cause 一次拿全 + 预算纪律」），这两条都会**假红**，
    #     而实际上是声明没跟着走。改成推导后，改基准顶部只需重算 frozen_diff 即可。
    _bfd = [d for d in ((load_prompt_variants().get("variants") or {})
                        .get("BASE", {}).get("frozen_diff") or []) if isinstance(d, dict)]
    _bl = [d.get("baseline_line") for d in _bfd]
    _vl = [d.get("variant_line") for d in _bfd]
    _cut = max((x[1] if isinstance(x, list) else x) for x in _bl) if _bl else 0
    # ★ 两侧起点**各自**取末条 hunk 的结束行：基准端 hunk 多为 replace（等行数），
    #   而本变体的替换块**不等长**（如基准 11 行 ↔ 变体 3 行）⇒ 不能用同一个下标切两侧。
    _vcut = max((x[1] if isinstance(x, list) else x) for x in _vl) if _vl else 0
    _tail_base = base_text.split("\n")[_cut:]
    _tail_var = prompt_text_of(base_spec).split("\n")[_vcut:]
    _need(_tail_base == _tail_var,
          "BASE 的 §1–§5（基准第 %d 行 / 变体第 %d 行起）必须与基准逐字相同"
          % (_cut + 1, _vcut + 1))
    checks.append("BASE arm(R-55): prompt 指纹复核 + %d 条冻结差异（difflib 实算逐条比对）"
                  " + 唯绑 read_facts@cc-base-tools(:8304) + §1–§5（基准第 %d 行起）与基准逐字相同；"
                  "每个带 frozen_diff 的变体均声明复核方（本脚本 %d / F5 %d）"
                  % (len(_bfd), _cut + 1, n_mine, n_other))

    for c in checks:
        print("  ok  %s" % c)
    print("SELFTEST PASS（%d 组）" % len(checks))
    return 0


# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description="M4.2/W2 + M4.7/W3 · Dify 主对比组与 KG-T 家族 App 创建与回读")
    ap.add_argument("--binding", default=str(BINDING_PATH))
    ap.add_argument("--apps", help="逗号分隔的 App 名（默认取 fairness.compare_pairs[0]）")
    ap.add_argument("--check", action="store_true", help="只读比对（默认）")
    ap.add_argument("--apply", action="store_true", help="按需创建 + 整体替换 model-config（只新增）")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--write-binding", action="store_true", help="--apply 时把真实 uuid 回写 binding.yaml")
    ap.add_argument("--evidence", metavar="DIR", help="证据输出目录")
    ap.add_argument("--json", metavar="OUT.json", help="机器可读结果输出路径")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.apps:
        args.apps = [s.strip() for s in args.apps.split(",") if s.strip()]
    try:
        if args.selftest:
            return cmd_selftest(args)
        if args.apply:
            return cmd_apply(args)
        return cmd_check(args)
    except Fail as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 1
    except common.DifyError as exc:
        print("DIFY ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
