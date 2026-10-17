#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
F5 门禁（v2）· 方案3 所有组：提示词单一事实源 / 工具面 / 归因主体轴

依据
----
docs/00-总体设计与裁决记录.md  裁决 9（方案3 / KG-T）、裁决 10（提示词变体）、
                               ★ 裁决 12（KG-T-free：Neo4j 插件直连）
docs/09-方案3设计（知识图谱直引+LLM）.md  §6.2 / §7 / §13 / §16
docs/05-编排层设计（Dify）.md  §1.3 F5 / §5.1 提示词模板 / §5.2 禁用词 / §5.3 六锚点
shared/model/kgt-tools.yaml            二/三/四/五
shared/model/kgt-prompt-variants.yaml  三/四（v1.2：只登记文件引用与机械差异清单）

★ v2 的核心变化（实施队长裁定 R-38「提示词单一事实源」）
--------------------------------------------------------
v1 把提示词正文内嵌在 kgt-prompt-variants.yaml 的 lines 里，门禁只能做「字符串包含」检查，
造成两个可作弊面：(a) 正文与真实落盘文件可能漂移；(b) 冻结差异只是散文，无法证明变体正文
确实等于「基准 + 恰 N 条差异」。

v2 改为：基准与两份变体各自落盘为独立文件，门禁
  1) 从**文件**读正文（不再读 yaml 内嵌行）；
  2) 用 difflib 对「基准文件 ↔ 变体文件」做**行级差异**，断言差异集合**逐条等于** yaml 的
     frozen_diff 清单（kind / baseline_line / variant_line / variant_text 全等）；
  3) 用「基准 + 声明差异」**重建**变体文本，断言与变体文件逐字节相同（未声明处不得改动）；
  4) 校验基准/两份变体文件的 bytes / LF 数 / sha256 与 yaml 登记值一致（防正文漂移）。

断言条数对照（改造前后）——只增不减，且不放宽任何既有判据：
  v1 : 66 项（无 --dify-readback 时；--selftest 不存在）
  v2 : 见运行末尾汇总（≥66；新增文件级/重建级/负控级断言，v1 的 66 项语义全部保留）
  逐段映射：
    v1 A1  禁用词零命中        -> v2 [A6] 基准 + [D9] KG-T 文件 + [D9] 派生 dyn
    v1 A2  差异数/编号/异于基准 -> v2 [B2][B3] 文件级 hunk 比对 + 重建（更强：连行号与文本）
    v1 A3  L3 逐字保留          -> v2 [D10] 基准 §5.1 硬性要求 1/2 在变体中逐字保留（更强）
    v1 A4/A5 工具数/禁令/dyn    -> v2 [D6][D7][E5][E6][E7]
    v1 A6  info-products 一致   -> v2 [E11][E12]
    v1 A7  冻结文本出现在全文   -> v2 [B3]（逐条文本等同）
    v1 B1  归因轴 cc_cause      -> v2 [E8]
    v1 B2/B3 工具集与 :8303     -> v2 [D6][D8]
    v1 B4/B5 插件身份与入参     -> v2 [E13]
    v1 B6  只读角色             -> v2 [E14]（v1 第 2 项是恒真占位，v2 改为真检查）
    v1 B7  同实例               -> v2 [E15]
    v1 B8  D-5..D-9 与全文一致  -> v2 [B2][B3][B4]
    v1 B9  插件载荷上限记账     -> v2 [E16]
    v1 C1  基准 L3 保留         -> v2 [D10]
    v1 C2  豁免组不误判         -> v2 [D9]（更强：豁免组命中集合必须恰等于声明集合）

可选
----
--dify-readback <json>   形如
    {"KG-Q": {"neo4j_endpoint": "bolt://h:7688", "tools": [...]},
     "KG-T-free": {"pre_prompt": "...", "tools": ["neo4j_query"],
                   "neo4j_endpoint": "bolt://h:7688", "provider_id": "...",
                   "plugin_version": "0.2.0", "allow_write_queries": false}}
    回读是唯一能防住「契约写对了、部署是错的」的手段（docs/05 §3.1 坑 A）。
--selftest               负控自检：把交付文件复制到临时目录后人为制造 5 种破坏，
                         每一种都必须让门禁 FAIL（不修改任何交付文件）。
--baseline / --prompts-dir / --prompts-yaml / --tools-yaml / --infoprod-yaml
                         路径注入（供 --selftest 与排障使用）

用法
----
    python shared/verify/f5_kgt_gate.py
    python shared/verify/f5_kgt_gate.py --selftest
    python shared/verify/f5_kgt_gate.py --dify-readback artifacts/kgt-readback.json
退出码：0 = PASS，1 = FAIL，2 = 环境/入参错误
"""
import argparse
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile

try:
    import yaml
except ImportError:
    sys.stderr.write("需要 PyYAML：pip install pyyaml\n")
    sys.exit(2)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(os.path.dirname(HERE), "model")
REPO = os.path.dirname(os.path.dirname(HERE))
PROMPT_DIR = os.path.join(os.path.dirname(HERE), "deploy", "prompts")

PROMPTS = os.path.join(MODEL, "kgt-prompt-variants.yaml")
TOOLS = os.path.join(MODEL, "kgt-tools.yaml")
INFOPROD = os.path.join(MODEL, "info-products.yaml")

# ★ 官方插件规格（外部事实，此处冻结以便检出契约漂移）
#   来源 https://raw.githubusercontent.com/neo4j-contrib/dify_neo4j/main/tools/neo4j_query.yaml
#   冻结日 2026-10-04；plugin manifest.yaml 记 version 0.2.0 / author nikola_milosevic / verified false
OFFICIAL_PLUGIN = {
    "provider_id": "nikola_milosevic/neo4j_query",
    "tool_name": "neo4j_query",
    "plugin_version": "0.2.0",
    "params": [
        ("database", "string", False),
        ("query", "string", True),
        ("parameters", "string", False),      # ★ 是 JSON 字符串，不是 object
        ("max_records", "number", False),
        ("allow_write_queries", "boolean", False),
    ],
    "query_max_chars": 2000,
    "max_records_default": 1000,
}

# ★ R-50 显式偏离契约（本地 fork）：只允许「身份面」两项偏离官方基线；
#   每一项偏离都必须在 kgt-tools.yaml 的 fork_provenance.deviations_from_official 里
#   逐字登记（field/official/declared/reason）。声明与官方不一致而登记缺失 ⇒ 无声漂移 ⇒ E13-i FAIL。
IDENTITY_FACE_FIELDS = ["provider_id", "plugin_version"]
ALLOWED_DEVIATION_FIELDS = IDENTITY_FACE_FIELDS + ["param_form.query", "param_form.parameters"]
FORK_SRC_DEFAULT = os.path.join(REPO, "shared", "deploy", "plugins", "neo4j_cypher_query")

# ★ docs/05 §5.1 围栏块（冻结原文；ASCII 引号 U+0022，不得写成全角）
#   ★ A′（2026-10-15，用户裁定）：基准已改为「**首选 cc_cause 一次拿全** + 预算纪律」——
#     原句要求 agent 先调 `cc_guide`/`cc_facts`，而这两个工具在工具面收窄中已移除；
#     实测导致 agent 猜参数、烧轮次、撞满 `max_iteration`（Q 臂 avg 20.8 轮、85% 触顶 ⇒ Dify 500）。
#     本块是基准顶部区域的**作者面出处**，故必须同步冻结副本。
BASELINE_REQUIRED_LINES = [
    "你是信用卡系统跨层运维诊断助手。你可以调用若干只读知识工具。",
    "需要对象的当前状态读数时，可调用 read_facts 读取指定范围内的最新读数；它只读且有界。",
    "",
    "【第一步 · 一次定域取证（首选）】",
    "**首选**调用 cc_cause，用问题中出现的症状/故障码做根因收敛。它的**一次**返回同时给出：",
    "  · candidates：候选根因（含层级、判据命中 satisfiedClauses/totalClauses、判定状态 determined、",
    "    组件类型与实例 componentType/componentInstance）；",
    "  · focus[]：**各候选所在实例上、该候选判据涉及的那几条指标**的观测值与聚合",
    "    —— 这就是\"为什么是这个根因\"的佐证，**不需要**再逐个对象类型去取事实；",
    "  · proposedActions：针对最优根因的**建议动作**（干跑形态）；",
    "  · focusLedger：本次裁剪了多少（未列出不等于没有问题）；",
    "  · acceptedSymptomCodes / maxHopsLimit：本环境接受的症状码全集与跳数上限",
    "    —— 合法取值只能取自这里，**不得自行编造症状码，也不得超出上限**。",
    "若某次返回 INVALID_PARAM 或 DEPTH_LIMIT_EXCEEDED，按其给出的合法取值改正后**只重试一次**，",
    "不要以不同写法反复试错。",
    "",
    "【第二步 · 按需补充，然后立即作答】",
    "只有在 cc_cause 的返回**明确缺少**某类证据时才补充：需要拓扑或影响面时调 cc_trace",
    "（其返回给出 directionEnum 与 maxHopsLimit）；需要最新读数时调 read_facts。",
    "补充之后**立即**按 §4 的四节格式输出结论，不要继续探索。",
    "",
    "【预算纪律（硬性）】",
    "工具调用轮次有硬上限。**目标是在 6 轮以内完成**：cc_cause 的一次返回应覆盖绝大部分证据需求；",
    "不要为了“再多查一点”而反复取数。若确实无法在预算内收敛，按 §3.3 明确写“不足以判定”",
    "并列出缺口 —— 这**优于**耗尽预算以致没有任何输出。",
    "",
    "【硬性要求】",
    "1. 每一句话的结论都必须引用具体的事实 id 作为依据；无依据不得下结论。",
    "2. 证据不足时，明确输出\"无法定位\"，并说明缺少哪些必要证据。不得猜测。",
    "3. 若因果上存在多个可能根因，按可能性从高到低列出，并标注它们是否可能同时成立。",
    "4. 只给出建议，不要假设任何动作已被执行。",
]
BASELINE_TOOLS = ["cc_cause", "cc_trace", "read_facts"]

# ★ A′（2026-10-15）：【硬性要求】1/2 的定位改为**按内容**（原为硬编码 [11]/[12]）。
#   旧 §5.1 块恰 15 行时那两个下标正好落在【硬性要求】上；块改成 31 行后
#   下标落到 `focus[]` 区域 ⇒ D10/E7-d 会指向错误的行。判据意图是「变体逐字保留
#   基准的【硬性要求】1/2」，与行号无关。
_HR = [l for l in BASELINE_REQUIRED_LINES if re.match(r"^[12]\. ", l)]
assert len(_HR) == 2, "【硬性要求】1/2 应恰 2 行，实为 %d" % len(_HR)
HR_LINE_1, HR_LINE_2 = _HR[0], _HR[1]


# ★ docs/05 §5.3 六个锚点（工具调用指引按臂判定，见 TOOL_GUIDANCE_RE）
ANCHOR_SHARED = [
    ("证据要求", "1. 每一句话的结论都必须引用具体的事实 id 作为依据；无依据不得下结论。"),
    ("无法定位要求", "2. 证据不足时，明确输出\"无法定位\"，并说明缺少哪些必要证据。不得猜测。"),
    ("多根因要求", "3. 若因果上存在多个可能根因，按可能性从高到低列出，并标注它们是否可能同时成立。"),
    ("不执行要求", "4. 只给出建议，不要假设任何动作已被执行。"),
    ("输出格式", "用简体中文作答，固定按下面四节输出；不要平铺工具返回的原始 JSON（可整理为表格或条目）："),
]
TOOL_GUIDANCE_RE = re.compile(
    r"调(?:用)?\s*(cc_guide|cc_facts|cc_cause|cc_trace|cc_propose"
    r"|get_object_topology|get_fault_topology|get_metrics|get_facts|neo4j_query"
    r"|read_facts|read_live_facts)")

# ★ docs/05 §5.2 禁用词正则（冻结原文）
BANNED_DOCS05_RE = re.compile(
    r"(cypher|sparql|rdf|owl|三元组|属性图|property\s*graph|知识图谱|本体"
    r"|neo4j|graphdb|turtle|ttl|shacl|推理机|reasoner)", re.I)

TOOL_NAME_RE = re.compile(r"\b(cc_[a-z_]+|get_[a-z_]+|run_rules|validate|collect_now|neo4j_query|read_facts|read_live_facts)\b")

PLUGIN_ARM = "KG-T-free"
PRESET_ARMS = ["KG-T", "KG-T-dyn"]
DYN_ARM = "KG-T-dyn"
ALL_SCHEME3_ARMS = ["KG-T", "KG-T-dyn", PLUGIN_ARM]
SERVICE_TOOL_RE = re.compile(r"^(cc_|run_rules$|validate$|get_[a-z_]+$|collect_now$)")


class Gate(object):
    def __init__(self, quiet=False):
        self.checks = 0
        self.failures = []
        self.quiet = quiet

    def check(self, cond, label, detail=""):
        self.checks += 1
        ok = bool(cond)
        if not ok:
            self.failures.append(label)
        if not self.quiet:
            if ok:
                print("  PASS  %s" % label)
            else:
                print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))
        return ok

    def section(self, title):
        if not self.quiet:
            print("\n" + title)


# ---------------------------------------------------------------- 读取与工具
def load_yaml(path):
    with io.open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_text(path):
    """返回 (raw_bytes, text, lines)；lines 不含行尾，且不做任何规范化。"""
    with io.open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return raw, text, lines


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hunks(base_lines, var_lines):
    """difflib 行级差异 -> 归一化 hunk 列表。"""
    sm = difflib.SequenceMatcher(None, base_lines, var_lines, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        kind = {"replace": "replace", "insert": "add", "delete": "delete"}[tag]
        bl = [i1 + 1, i2] if kind == "replace" else i1
        out.append({"kind": kind, "baseline_line": bl,
                    "variant_line": [j1 + 1, j2], "text": list(var_lines[j1:j2])})
    return out


def norm_key(kind, bl, vl):
    """把声明与实际 hunk 归一到同一比较键。"""
    if isinstance(vl, int):
        vl = [vl, vl]
    vl = tuple(vl)
    if kind == "add":
        return (kind, int(bl), vl)
    bl = [bl, bl] if isinstance(bl, int) else bl
    return (kind, tuple(bl), vl)


def declared_ops(frozen):
    """把 yaml 的 `frozen_diff` 清单转成 `rebuild`/B3 用的 ops。

    ★ A′（2026-10-15）：`variant_text` **两种形态都支持** —— 单行时是字符串、多行时是列表。
      原实现写死 `[d["variant_text"]]`，对字符串正确（`["x"]` == 实际的 `["x"]`），
      但对**列表**会变成嵌套列表 `[["a","b"]]` ⇒ B3-c 比对与 B4 重建都会失败。
      历史 yaml 全是「2 行 → 1 行」的单行 hunk，因此该限制一直没暴露；
      A′ 的变体替换块是多行的，必须泛化。字符串行为保持不变。
    """
    ops = []
    for d in frozen:
        vt = d["variant_text"]
        text = list(vt) if isinstance(vt, (list, tuple)) else [vt]
        ops.append({"id": d["id"], "kind": d["kind"], "baseline_line": d.get("baseline_line"),
                    "variant_line": d.get("variant_line"), "text": text})
    return ops


def rebuild(base_lines, ops):
    """用「基准 + 声明差异」重建变体文本（不读变体文件）。"""
    ops = sorted(ops, key=lambda o: o["variant_line"][0])
    out = []
    cur = 0
    for o in ops:
        bl = o["baseline_line"]
        if o["kind"] == "add":
            bstart, bend = int(bl), int(bl)
        else:
            bl = [bl, bl] if isinstance(bl, int) else bl
            bstart, bend = bl[0] - 1, bl[1]
        out.extend(base_lines[cur:bstart])
        out.extend(o["text"])
        cur = bend
    out.extend(base_lines[cur:])
    return out


def banned_hits(text, words):
    hits = []
    for w in words:
        for m in re.finditer(re.escape(w), text, re.IGNORECASE):
            if w.lower() not in [h.lower() for h in hits]:
                hits.append(w)
    return hits


def docs05_hits(text):
    return sorted({m.group(0).lower() for m in BANNED_DOCS05_RE.finditer(text)})


def tool_inventory(text):
    found = []
    for m in TOOL_NAME_RE.finditer(text):
        if m.group(0) not in found:
            found.append(m.group(0))
    return found


def tool_guidance_tools(text):
    return sorted({m.group(1) for m in TOOL_GUIDANCE_RE.finditer(text)})


def dump_yaml(path, obj):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False, default_flow_style=False))


# ------------------------------------------------ 插件面有效视图（声明驱动 + 实现腿 + 回读腿）
def implementation_plugin_identity(src):
    """实现腿（fork 源码树，只读）：推导 Console 的 provider_id 与每参 form。

    Dify Console 的 provider_id 形态 = <author>/<plugin name>/<provider name>：
      author        ← src/provider/neo4j_query.yaml 的 identity.author
      plugin name   ← 打包时登记的插件名（源码树目录名 shared/deploy/plugins/<name>）
      provider name ← src/provider/neo4j_query.yaml 的 identity.name
    刻意用「目录名」推导插件名（不读 manifest.yaml），使「声明改了而实现树没改」这类
    无声漂移能被 E13-i 检出，负控 S7-a/S7-b 正依赖这一点。
    """
    prov_path = os.path.join(src, "src", "provider", "neo4j_query.yaml")
    tool_path = os.path.join(src, "src", "tools", "neo4j_query.yaml")
    out = {"exists": False, "path": src, "provider_id": None, "author": None,
           "plugin_name": None, "provider_name": None, "tool_name": None,
           "param_form": {}, "defaults": {}}
    if not (os.path.isfile(prov_path) and os.path.isfile(tool_path)):
        return out
    prov = load_yaml(prov_path)
    tool = load_yaml(tool_path)
    pident = prov.get("identity") or {}
    tident = tool.get("identity") or {}
    out["exists"] = True
    out["author"] = pident.get("author")
    out["plugin_name"] = os.path.basename(os.path.normpath(src))
    out["provider_name"] = pident.get("name")
    out["tool_name"] = tident.get("name")
    out["provider_id"] = "%s/%s/%s" % (out["author"], out["plugin_name"], out["provider_name"])
    for p in (tool.get("parameters") or []):
        if isinstance(p, dict) and p.get("name"):
            out["param_form"][p["name"]] = p.get("form")
            if "default" in p:
                out["defaults"][p["name"]] = p["default"]
    return out


def effective_plugin_view(tl, pv, fork_src=None):
    """把「声明（kgt-tools.yaml + kgt-prompt-variants.yaml）/ 实现腿 / 真机回读腿」聚合成有效视图。"""
    arm = tl["arms"][PLUGIN_ARM]
    dp = arm.get("dify_plugin") or {}
    fp = dp.get("fork_provenance") or {}
    pv_plugin = ((pv.get("variants") or {}).get(PLUGIN_ARM) or {}).get("dify_plugin") or {}
    tool_def = next(t for t in tl["tools"] if t["tool_name"] == OFFICIAL_PLUGIN["tool_name"])
    devs = {}
    for d in (fp.get("deviations_from_official") or []):
        if isinstance(d, dict) and d.get("field"):
            devs[d["field"]] = d
    return {
        "arm": arm,
        "dify_plugin": dp,
        "fork_provenance": fp,
        "installation_status": dp.get("installation_status"),
        "fork_installation_status": fp.get("installation_status"),
        "provider_id": dp.get("provider_id"),
        "plugin_version": str(dp.get("plugin_version")),
        "pv_provider_id": pv_plugin.get("provider_id"),
        "pv_plugin_version": str(pv_plugin.get("version")),
        "tool_def": tool_def,
        "props": tool_def["request"]["properties"],
        "param_form": dict(tool_def.get("param_form") or {}),
        "readback": dp.get("machine_readback") or {},
        "deviations": devs,
        "implementation": implementation_plugin_identity(fork_src or FORK_SRC_DEFAULT),
    }


# ---------------------------------------------------------------- 门禁主体
def run_checks(P, g):
    """P: dict(baseline, prompts_dir, prompts_yaml, tools_yaml, infoprod_yaml)"""
    pv = load_yaml(P["prompts_yaml"])
    tl = load_yaml(P["tools_yaml"])
    ip = load_yaml(P["infoprod_yaml"])

    variants = pv["variants"]
    banned = pv["gate"]["banned_words"]
    scope = pv["gate"].get("banned_words_scope") or {}
    applies_to = scope.get("applies_to") or list(variants.keys())
    exempt = scope.get("exempt") or []

    base_spec = pv["baseline"]
    base_path = P["baseline"]
    braw, btext, blines = load_text(base_path)

    # ---------- A 基准文件 ----------
    g.section("[A] 基准文件（%s）" % os.path.relpath(base_path, REPO).replace("\\", "/"))
    g.check(os.path.isfile(base_path), "A1-a 基准文件存在")
    g.check(braw[:3] != b"\xef\xbb\xbf" and b"\r" not in braw,
            "A1-b 基准文件 UTF-8 无 BOM 且 LF-only", "BOM=%s CR=%d" % (braw[:3] == b"\xef\xbb\xbf", braw.count(b"\r")))
    g.check(braw.endswith(b"\n"), "A1-c 基准文件以 LF 结尾")
    g.check(len(braw) == base_spec["bytes"], "A2-a 基准 bytes == yaml 登记值",
            "%d vs %s" % (len(braw), base_spec.get("bytes")))
    g.check(braw.count(b"\n") == base_spec["lf_count"], "A2-b 基准 LF 数 == yaml 登记值",
            "%d vs %s" % (braw.count(b"\n"), base_spec.get("lf_count")))
    g.check(sha256_bytes(braw) == base_spec["sha256"], "A2-c 基准 sha256 == yaml 登记值",
            sha256_bytes(braw))
    for i, ln in enumerate(BASELINE_REQUIRED_LINES, 1):
        g.check(ln in blines, "A3-%02d 基准逐字含 docs/05 §5.1 第 %d 行" % (i, i), ln[:32])
    for t in BASELINE_TOOLS:
        g.check(t in btext, "A4 基准点名工具 %s" % t)
    g.check(sorted(tool_inventory(btext)) == sorted(BASELINE_TOOLS),
            "A4-b 基准工具名清单恰为 §5.1 的 3 个",
            "%s vs %s" % (tool_inventory(btext), BASELINE_TOOLS))
    g.check(len(BASELINE_TOOLS) == base_spec.get("tool_count"),
            "A4-c 基准 tool_count == yaml 登记值", str(base_spec.get("tool_count")))
    guide = tool_guidance_tools(btext)
    g.check(bool(guide) and set(guide) <= set(BASELINE_TOOLS),
            "A5-01 锚点「工具调用指引」齐备（按本臂工具名判定）", str(guide))
    for label, s in ANCHOR_SHARED:
        g.check(s in btext, "A5-%s 锚点「%s」齐备" % ({"证据要求": "02", "无法定位要求": "03",
                                                       "多根因要求": "04", "不执行要求": "05",
                                                       "输出格式": "06"}[label], label))
    g.check(not banned_hits(btext, banned), "A6-a 基准禁用词零命中（gate.banned_words）",
            ", ".join(banned_hits(btext, banned)))
    g.check(not docs05_hits(btext), "A6-b 基准禁用词零命中（docs/05 §5.2 正则）",
            ", ".join(docs05_hits(btext)))

    # ---------- B 行级差异（核心） ----------
    for arm in ("KG-T", PLUGIN_ARM):
        spec = variants[arm]
        vpath = os.path.join(P["prompts_dir"], os.path.basename(spec["prompt_file"]))
        g.section("[B] %s 文件级行级差异（%s）" % (arm, os.path.basename(vpath)))
        vraw, vtext, vlines = load_text(vpath)
        frozen = spec["frozen_diff"]
        actual = hunks(blines, vlines)
        ops = declared_ops(frozen)

        g.check(len(actual) == len(frozen),
                "B2 %s 实际 hunk 数 == frozen_diff 条数 (%d)" % (arm, len(frozen)),
                "实际 %d 条：%s" % (len(actual), [h["kind"] for h in actual]))
        akeys = [norm_key(h["kind"], h["baseline_line"], h["variant_line"]) for h in actual]
        dkeys = [norm_key(o["kind"], o["baseline_line"], o["variant_line"]) for o in ops]
        g.check(sorted(akeys) == sorted(dkeys),
                "B3-a %s 差异键集合 == 冻结清单（kind/行号逐条相等）" % arm,
                "实际 %s vs 声明 %s" % (akeys, dkeys))
        amap = {}
        for h, k in zip(actual, akeys):
            amap.setdefault(k, []).append(h["text"])
        for o, k in zip(ops, dkeys):
            g.check(k in amap, "B3-b %s/%s 行号与 kind 命中实际 hunk" % (arm, o["id"]), str(k))
            g.check(k in amap and amap[k][0] == o["text"],
                    "B3-c %s/%s variant_text 逐字等于变体文件该行" % (arm, o["id"]),
                    "%s vs %s" % (amap.get(k), o["text"]))
        g.check(rebuild(blines, ops) == vlines,
                "B4 %s 由「基准 + 冻结清单」重建的文本 == 变体文件逐行相同" % arm, "存在未声明改动")
        g.check(not [ln for ln in (BASELINE_REQUIRED_LINES[3], BASELINE_REQUIRED_LINES[4],
                                   BASELINE_REQUIRED_LINES[7], BASELINE_REQUIRED_LINES[8])
                     if ln in vlines],
                "B5 %s 不含被 D 冻结差异替换掉的基准行" % arm)
        g.check(not banned_hits(vtext, banned) if arm in applies_to else True,
                "B9-a %s 禁用词零命中（gate.banned_words；豁免组另行断言）" % arm
                if arm in applies_to else "B9-a %s 为豁免组，见下" % arm,
                ", ".join(banned_hits(vtext, banned)))
        if arm in applies_to:
            g.check(not docs05_hits(vtext), "B9-b %s 禁用词零命中（docs/05 §5.2 正则）" % arm,
                    ", ".join(docs05_hits(vtext)))
        else:
            declared = (spec.get("banned_words_declared_hits") or {})
            got_gate = sorted({w.lower() for w in banned_hits(vtext, banned)})
            want_gate = sorted({w.lower() for w in (declared.get("gate_list") or [])})
            g.check(got_gate == want_gate,
                    "B9-c %s 豁免组 gate 禁用词命中集合恰等于声明集合" % arm,
                    "%s vs %s" % (got_gate, want_gate))
            got_re = docs05_hits(vtext)
            want_re = sorted({w.lower() for w in (declared.get("docs05_regex") or [])})
            g.check(got_re == want_re,
                    "B9-d %s 豁免组 docs/05 §5.2 命中集合恰等于声明集合" % arm,
                    "%s vs %s" % (got_re, want_re))

    # ---------- D 变体文件属性 / 锚点 / 工具面 ----------
    arm_texts = {}
    for arm in ("KG-T", PLUGIN_ARM):
        spec = variants[arm]
        vpath = os.path.join(P["prompts_dir"], os.path.basename(spec["prompt_file"]))
        vraw, vtext, vlines = load_text(vpath)
        arm_texts[arm] = vtext
        g.section("[D] 变体文件属性与锚点（%s）" % arm)
        g.check(os.path.isfile(vpath), "D1-a %s 文件存在" % arm)
        g.check(vraw[:3] != b"\xef\xbb\xbf" and b"\r" not in vraw,
                "D1-b %s UTF-8 无 BOM 且 LF-only" % arm, "CR=%d" % vraw.count(b"\r"))
        g.check(vraw.endswith(b"\n"), "D1-c %s 以 LF 结尾" % arm)
        g.check(len(vraw) == spec["bytes"], "D2-a %s bytes == yaml 登记值" % arm,
                "%d vs %s" % (len(vraw), spec.get("bytes")))
        g.check(vraw.count(b"\n") == spec["lf_count"], "D2-b %s LF 数 == yaml 登记值" % arm,
                "%d vs %s" % (vraw.count(b"\n"), spec.get("lf_count")))
        g.check(sha256_bytes(vraw) == spec["sha256"], "D2-c %s sha256 == yaml 登记值" % arm,
                sha256_bytes(vraw))
        inv = tool_inventory(vtext)
        g.check(sorted(inv) == sorted(spec["expected_tools"]),
                "D6 %s 文件工具名集合 == expected_tools" % arm,
                "%s vs %s" % (inv, spec["expected_tools"]))
        g.check(len(spec["expected_tools"]) == spec["expected_tool_count"],
                "D6-b %s expected_tool_count == len(expected_tools)" % arm)
        g.check(len(spec["expected_tools"]) == tl["expected_tool_counts"].get(arm),
                "D6-c %s 工具数与 kgt-tools.yaml#expected_tool_counts 一致" % arm)
        if arm == "KG-T":
            forbidden = set(tl["forbidden_tools_main_arm"])
            hit = sorted(set(inv) & forbidden)
            g.check(not hit, "D7 %s 不含 forbidden_tools_main_arm" % arm, ", ".join(hit))
        else:
            stray = [t for t in inv if SERVICE_TOOL_RE.match(t)]
            g.check(not stray, "D8 %s 不含任何 :8303 端点工具" % arm, ", ".join(stray))
            g.check(set(inv) <= set(tl["allowed_tools_kgt_free"]),
                    "D8-b %s 工具集 ⊆ allowed_tools_kgt_free" % arm)
            g.check(not (set(inv) & set(tl["forbidden_tools_kgt_free"])),
                    "D8-c %s 工具集与 forbidden_tools_kgt_free 无交集" % arm)
            g.check("cc_cause" not in inv, "B1-a %s 不含 cc_cause" % arm)
        # ★ A′：按内容定位，不再用硬编码下标
        for i, ln in enumerate([HR_LINE_1, HR_LINE_2], 1):
            g.check(ln in vlines,
                    "D10-%d %s 逐字保留基准【硬性要求】%d（false_claim 锚点）" % (i, arm, i), ln[:32])
        guide = tool_guidance_tools(vtext)
        g.check(bool(guide) and set(guide) <= set(spec["expected_tools"]),
                "D11-01 %s 锚点「工具调用指引」齐备（按本臂工具名判定）" % arm, str(guide))
        for label, s in ANCHOR_SHARED:
            g.check(s in vtext, "D11-%s %s 锚点「%s」齐备" % (arm, label, label))
        g.check(spec.get("banned_words_policy") == ("zero_hits" if arm in applies_to else "exempt"),
                "D12 %s banned_words_policy 与 gate.banned_words_scope 一致" % arm,
                str(spec.get("banned_words_policy")))

    # ---------- E 派生组（KG-T-dyn）/ 契约一致性 / 工具面 ----------
    dyn = variants[DYN_ARM]
    kgt_spec = variants["KG-T"]
    kgt_path = os.path.join(P["prompts_dir"], os.path.basename(kgt_spec["prompt_file"]))
    _, _, kgt_lines = load_text(kgt_path)
    dyn_ops = declared_ops(dyn["derived_diff"])
    for o in dyn_ops:
        o["baseline_line"] = o["baseline_line"] if o["baseline_line"] is not None else None
    for d, o in zip(dyn["derived_diff"], dyn_ops):
        o["baseline_line"] = d.get("parent_line")
    dyn_lines = rebuild(kgt_lines, dyn_ops)
    dyn_text = "\n".join(dyn_lines) + "\n"

    g.section("[E] 派生组 %s（无独立文件）与契约一致性" % DYN_ARM)
    g.check(dyn.get("derived_from") == "KG-T", "E1 %s derived_from == KG-T" % DYN_ARM,
            str(dyn.get("derived_from")))
    actual_dyn = hunks(kgt_lines, dyn_lines)
    g.check(len(actual_dyn) == len(dyn["derived_diff"]) == pv["meta"].get("frozen_diff_count_kgt_dyn_derived"),
            "E2 %s 派生差异数 == 声明数 == meta 登记数" % DYN_ARM,
            "%d vs %d" % (len(actual_dyn), len(dyn["derived_diff"])))
    akey = norm_key(actual_dyn[0]["kind"], actual_dyn[0]["baseline_line"], actual_dyn[0]["variant_line"]) if actual_dyn else None
    dkey = norm_key(dyn_ops[0]["kind"], dyn_ops[0]["baseline_line"], dyn_ops[0]["variant_line"]) if dyn_ops else None
    g.check(akey is not None and akey == dkey, "E3 %s 派生差异键与声明一致" % DYN_ARM,
            "%s vs %s" % (akey, dkey))
    g.check(bool(actual_dyn) and actual_dyn[0]["text"] == [dyn_ops[0]["text"][0]],
            "E4 %s 派生 variant_text 逐字一致" % DYN_ARM,
            str(actual_dyn[0]["text"] if actual_dyn else None))
    g.check(not banned_hits(dyn_text, banned), "E5 %s 禁用词零命中（含派生行）" % DYN_ARM,
            ", ".join(banned_hits(dyn_text, banned)))
    dyn_tools = list(dyn["expected_tools"])
    kgt_tools = set(kgt_spec["expected_tools"])
    g.check(len(dyn_tools) == dyn["expected_tool_count"] == tl["expected_tool_counts"].get(DYN_ARM),
            "E6 %s 工具数 == 5 == kgt-tools.yaml" % DYN_ARM, str(len(dyn_tools)))
    g.check(set(dyn_tools) - kgt_tools == {"collect_now"},
            "E7-a %s 相比 KG-T 恰多 collect_now" % DYN_ARM, str(sorted(set(dyn_tools) - kgt_tools)))
    g.check(not (kgt_tools - set(dyn_tools)), "E7-b %s 未遗漏 KG-T 任何工具" % DYN_ARM,
            str(sorted(kgt_tools - set(dyn_tools))))
    g.check(not (set(dyn_tools) & set(tl["forbidden_tools_main_arm"]) - {"collect_now"}),
            "E7-c %s 除 collect_now 外不含禁令工具" % DYN_ARM)
    for i, ln in enumerate([HR_LINE_1, HR_LINE_2], 1):
        g.check(ln in dyn_lines, "E7-d %s 逐字保留基准【硬性要求】%d" % (DYN_ARM, i))
    g.check(dyn.get("banned_words_policy") == "zero_hits",
            "E7-e %s banned_words_policy == zero_hits" % DYN_ARM, str(dyn.get("banned_words_policy")))

    axis = tl["attribution_axis_forbidden"]
    g.check(axis["tool"] == "cc_cause", "E8-a attribution_axis_forbidden.tool == cc_cause",
            str(axis.get("tool")))
    g.check(set(axis["applies_to_arms"]) == set(ALL_SCHEME3_ARMS),
            "E8-b 该禁令适用于方案3 所有组", str(axis.get("applies_to_arms")))
    for arm in ALL_SCHEME3_ARMS:
        g.check("cc_cause" not in list(variants[arm]["expected_tools"]),
                "E8-c %s 工具集不含 cc_cause" % arm, str(variants[arm]["expected_tools"]))

    for arm in ALL_SCHEME3_ARMS:
        ip_tools = list(ip["access"][arm]["tools"])
        g.check(ip_tools == list(variants[arm]["expected_tools"]),
                "E11 %s 工具列表 info-products 与 prompt-variants 一致" % arm,
                "info-products=%s vs variants=%s" % (ip_tools, variants[arm]["expected_tools"]))
    contract_tools = [t["tool_name"] for t in tl["tools"] if t.get("only_for_arms") != ["KG-T-dyn"]
                      and t["tool_name"] != OFFICIAL_PLUGIN["tool_name"]]
    g.check(sorted(contract_tools) == sorted(ip["access"]["KG-T"]["tools"]),
            "E12 kgt-tools.yaml 的 KG-T 工具定义集 == info-products.yaml KG-T 工具集",
            "%s vs %s" % (sorted(contract_tools), sorted(ip["access"]["KG-T"]["tools"])))

    arm_spec = tl["arms"][PLUGIN_ARM]["dify_plugin"]
    eff = effective_plugin_view(tl, pv, fork_src=P.get("fork_src"))
    dev_pid = eff["deviations"].get("provider_id") or {}
    dev_ver = eff["deviations"].get("plugin_version") or {}
    g.check(eff["provider_id"] == OFFICIAL_PLUGIN["provider_id"]
            or (dev_pid.get("declared") == eff["provider_id"]
                and dev_pid.get("official") == OFFICIAL_PLUGIN["provider_id"]),
            "E13-a provider_id == %s 或已按 R-50 显式登记该身份面偏离（官方=%s）"
            % (OFFICIAL_PLUGIN["provider_id"], OFFICIAL_PLUGIN["provider_id"]),
            str(eff["provider_id"]))
    g.check(eff["plugin_version"] == OFFICIAL_PLUGIN["plugin_version"]
            or (str(dev_ver.get("declared")) == eff["plugin_version"]
                and str(dev_ver.get("official")) == OFFICIAL_PLUGIN["plugin_version"]),
            "E13-b plugin_version pin 为 %s 或已按 R-50 显式登记该身份面偏离（官方=%s）"
            % (OFFICIAL_PLUGIN["plugin_version"], OFFICIAL_PLUGIN["plugin_version"]),
            eff["plugin_version"])
    g.check(arm_spec.get("plugin_verified") is False, "E13-c plugin_verified 如实记为 false",
            str(arm_spec.get("plugin_verified")))
    g.check(arm_spec.get("service_port") is None or tl["arms"][PLUGIN_ARM].get("service_port") is None,
            "E13-d service_port 为空（不经 :8303）")
    tool_def = next(t for t in tl["tools"] if t["tool_name"] == OFFICIAL_PLUGIN["tool_name"])
    props = tool_def["request"]["properties"]
    got = [(n, props[n]["type"], n in tool_def["request"]["required"]) for n, _t, _r in OFFICIAL_PLUGIN["params"]]
    g.check(got == OFFICIAL_PLUGIN["params"], "E13-e 五个入参(name,type,required)逐字对齐官方", "got=%s" % (got,))
    g.check(not sorted(set(props) - {p[0] for p in OFFICIAL_PLUGIN["params"]}),
            "E13-f 契约未杜撰额外入参",
            ", ".join(sorted(set(props) - {p[0] for p in OFFICIAL_PLUGIN["params"]})))
    g.check(props["parameters"]["type"] == "string",
            "E13-g ★ parameters 声明为 string（JSON 字符串），不是 object", props["parameters"]["type"])
    g.check(props["allow_write_queries"].get("default") is False, "E13-h allow_write_queries 默认 false")

    # ---- E13-i 声明驱动 + 显式偏离：无声漂移必须 FAIL（R-50）----
    inst = eff["installation_status"]
    g.check(bool(inst) and inst == eff["fork_installation_status"],
            "E13-i.1 dify_plugin.installation_status 与 fork_provenance 一致（真机已装）",
            "%r vs %r" % (inst, eff["fork_installation_status"]))
    g.check(sorted(eff["deviations"]) == sorted(ALLOWED_DEVIATION_FIELDS),
            "E13-i.2 deviations_from_official 恰为 %d 条且字段集 == 允许集 %s"
            % (len(ALLOWED_DEVIATION_FIELDS), ALLOWED_DEVIATION_FIELDS),
            str(sorted(eff["deviations"])))
    for f in ALLOWED_DEVIATION_FIELDS:
        d = eff["deviations"].get(f) or {}
        g.check(bool(d.get("reason")), "E13-i.3 %s 的偏离登记带 reason" % f)
    for f, off, got in (("provider_id", OFFICIAL_PLUGIN["provider_id"], eff["provider_id"]),
                        ("plugin_version", OFFICIAL_PLUGIN["plugin_version"], eff["plugin_version"]),
                        ("param_form.query", "form", eff["param_form"].get("query")),
                        ("param_form.parameters", "form", eff["param_form"].get("parameters"))):
        d = eff["deviations"].get(f) or {}
        if str(got) == str(off):
            g.check(f not in eff["deviations"],
                    "E13-i.4 %s 与官方一致 ⇒ 不得登记虚假偏离" % f)
            continue
        g.check(str(d.get("official")) == str(off) and str(d.get("declared")) == str(got),
                "E13-i.5 %s 已显式登记且 official/declared 与事实逐字相符（%s -> %s）" % (f, off, got),
                "official=%r declared=%r" % (d.get("official"), d.get("declared")))
    impl = eff["implementation"]
    g.check(impl.get("exists"), "E13-i.6 fork 源码树可读（实现腿可判定）", str(impl.get("path")))
    g.check(impl.get("provider_id") == eff["provider_id"],
            "E13-i.7 实现腿 provider_id == 声明（%s）" % eff["provider_id"],
            "impl=%s（author=%s / plugin=%s / provider=%s）"
            % (impl.get("provider_id"), impl.get("author"), impl.get("plugin_name"), impl.get("provider_name")))
    g.check(str(impl.get("tool_name")) == OFFICIAL_PLUGIN["tool_name"],
            "E13-i.8 实现腿工具名仍为 %s（工具名不可偏离）" % OFFICIAL_PLUGIN["tool_name"],
            str(impl.get("tool_name")))
    rb = eff["readback"]
    g.check(rb.get("provider_id") == eff["provider_id"],
            "E13-i.9 真机回读 provider_id == 声明", "%s vs %s" % (rb.get("provider_id"), eff["provider_id"]))
    g.check(str(rb.get("plugin_version")) == eff["plugin_version"],
            "E13-i.10 真机回读 plugin_version == 声明",
            "%s vs %s" % (rb.get("plugin_version"), eff["plugin_version"]))
    g.check(rb.get("plugin_unique_identifier") == eff["dify_plugin"].get("plugin_unique_identifier"),
            "E13-i.11 真机回读 plugin_unique_identifier == 声明",
            str(rb.get("plugin_unique_identifier")))
    g.check(rb.get("installation_id") == eff["dify_plugin"].get("installation_id"),
            "E13-i.12 真机回读 installation_id == 声明", str(rb.get("installation_id")))

    # ---- E13-j param_form 逐参（R-50：query/parameters=llm，其余停在 form）----
    want_form = eff["param_form"]
    g.check(sorted(want_form) == sorted(p[0] for p in OFFICIAL_PLUGIN["params"]),
            "E13-j.1 param_form 键恰为五个入参名", str(sorted(want_form)))
    g.check(want_form.get("query") == "llm",
            "E13-j.2 query 为 llm（模型必须能发出 Cypher）", str(want_form.get("query")))
    g.check(want_form.get("parameters") == "llm",
            "E13-j.3 parameters 为 llm（JSON 字符串入参，form 会填不上）", str(want_form.get("parameters")))
    for pname in ("database", "max_records", "allow_write_queries"):
        g.check(want_form.get(pname) == "form",
                "E13-j.4 %s 停在 form（写操作与行数上限留在 LLM 不可及处）" % pname,
                "want_form=%s" % (want_form,))
    g.check("query" in (tool_def["request"].get("required") or []),
            "E13-j.5 query 仍 required", str(tool_def["request"].get("required")))
    g.check(eff["implementation"].get("param_form") == want_form,
            "E13-j.6 实现腿每参 form == 声明（逐参相等）",
            "impl=%s vs declared=%s" % (eff["implementation"].get("param_form"), want_form))
    g.check(dict(rb.get("param_form") or {}) == want_form,
            "E13-j.7 真机回读 param_form 逐参 == 声明", "readback=%s" % (rb.get("param_form"),))
    g.check(str(rb.get("max_records_default")) == str(OFFICIAL_PLUGIN["max_records_default"])
            and rb.get("allow_write_queries_default") is False,
            "E13-j.8 真机回读 max_records 默认 %d / allow_write_queries 默认 false"
            % OFFICIAL_PLUGIN["max_records_default"],
            "%s / %s" % (rb.get("max_records_default"), rb.get("allow_write_queries_default")))

    g.check(arm_spec.get("read_only_role_required") is True, "E14-a 契约声明 Neo4j 只读角色必填")
    free_file_text = arm_texts[PLUGIN_ARM]
    ro_line = [d for d in variants[PLUGIN_ARM]["frozen_diff"] if d["id"] == "D-7"][0]["variant_text"]
    g.check(ro_line in free_file_text, "E14-b 变体文件含只读纪律句（D-7）", ro_line[:24])

    pv_plugin = variants[PLUGIN_ARM].get("dify_plugin") or {}
    g.check(pv_plugin.get("provider_id") == arm_spec["provider_id"],
            "E15-a prompt-variants 与 kgt-tools 的 provider_id 一致", str(pv_plugin.get("provider_id")))
    g.check(str(pv_plugin.get("version")) == str(arm_spec["plugin_version"]),
            "E15-b prompt-variants 与 kgt-tools 的 plugin_version 一致", str(pv_plugin.get("version")))
    g.check(str(tl["arms"][PLUGIN_ARM].get("carrier", "")).startswith("neo4j-shared-with-KG-Q"),
            "E15-c 契约声明与 KG-Q 共用同一实例/同一张图",
            str(tl["arms"][PLUGIN_ARM].get("carrier")))

    pl = tl["payload_policy"].get("plugin_owned_limits") or {}
    g.check(pl.get("query_max_chars") == OFFICIAL_PLUGIN["query_max_chars"],
            "E16-a query_max_chars == %d" % OFFICIAL_PLUGIN["query_max_chars"], str(pl.get("query_max_chars")))
    g.check(pl.get("max_records_default") == OFFICIAL_PLUGIN["max_records_default"],
            "E16-b max_records_default == %d" % OFFICIAL_PLUGIN["max_records_default"],
            str(pl.get("max_records_default")))
    g.check(bool(pl.get("not_aligned_with_project_caps")),
            "E16-c 已声明与本项目 CAP/MAX_STR 不可直接比较")

    # ---------- 可选：Dify 回读 ----------
    if P.get("dify_readback"):
        rb = json.load(io.open(P["dify_readback"], encoding="utf-8"))
        g.section("[回读] Dify 部署回读断言")
        for arm in ("KG-T", PLUGIN_ARM, DYN_ARM):
            spec = variants[arm]
            got_rb = rb.get(arm)
            if not g.check(got_rb is not None, "回读里含实验组 %s" % arm):
                continue
            tools = list(got_rb.get("tools") or [])
            g.check(sorted(tools) == sorted(spec["expected_tools"]),
                    "%s 回读工具集与契约一致" % arm,
                    "回读 %s vs 契约 %s" % (sorted(tools), sorted(spec["expected_tools"])))
            prompt = got_rb.get("pre_prompt") or ""
            if arm in exempt:
                if not g.quiet:
                    print("  SKIP  %s 的 pre_prompt 禁用词检查（豁免组）" % arm)
            else:
                hits = banned_hits(prompt, banned)
                g.check(not hits, "%s 回读 pre_prompt 禁用词零命中" % arm, ", ".join(hits))
            frozen = spec.get("frozen_diff") or spec.get("derived_diff")
            for d in frozen:
                g.check(d["variant_text"] in prompt,
                        "%s 回读 pre_prompt 含 %s" % (arm, d["id"]), d["variant_text"][:24])
        fr = rb.get(PLUGIN_ARM) or {}
        if fr:
            g.check(fr.get("provider_id") == eff["provider_id"],
                    "回读 provider_id == 声明的有效值（%s）" % eff["provider_id"], str(fr.get("provider_id")))
            g.check(fr.get("plugin_version") == eff["plugin_version"],
                    "回读 plugin_version == 声明的有效值（%s）" % eff["plugin_version"], str(fr.get("plugin_version")))
            g.check(fr.get("allow_write_queries") is False,
                    "★ 回读 allow_write_queries == false", str(fr.get("allow_write_queries")))
        ep_q = (rb.get("KG-Q") or {}).get("neo4j_endpoint")
        ep_f = (rb.get(PLUGIN_ARM) or {}).get("neo4j_endpoint")
        g.check(bool(ep_q) and bool(ep_f) and ep_q == ep_f,
                "回读：两个实验组 neo4j_endpoint 字符串相等", "%s vs %s" % (ep_q, ep_f))
    return g


# ---------------------------------------------------------------- 负控自检
def _copy_delivery(dst, P):
    os.makedirs(dst, exist_ok=True)
    os.makedirs(os.path.join(dst, "prompts"), exist_ok=True)
    out = dict(P)
    out["baseline"] = os.path.join(dst, "prompts", os.path.basename(P["baseline"]))
    shutil.copyfile(P["baseline"], out["baseline"])
    out["prompts_yaml"] = os.path.join(dst, os.path.basename(P["prompts_yaml"]))
    shutil.copyfile(P["prompts_yaml"], out["prompts_yaml"])
    out["tools_yaml"] = os.path.join(dst, os.path.basename(P["tools_yaml"]))
    shutil.copyfile(P["tools_yaml"], out["tools_yaml"])
    out["infoprod_yaml"] = os.path.join(dst, os.path.basename(P["infoprod_yaml"]))
    shutil.copyfile(P["infoprod_yaml"], out["infoprod_yaml"])
    out["prompts_dir"] = os.path.join(dst, "prompts")
    for arm in ("KG-T", PLUGIN_ARM):
        name = os.path.basename(P["_spec"][arm]["prompt_file"])
        shutil.copyfile(os.path.join(P["prompts_dir"], name), os.path.join(out["prompts_dir"], name))
    return out


def _refresh_yaml(P):
    """把临时目录里三份提示词的真实指纹写回临时 yaml（使指纹类断言不成为唯一拦截者）。"""
    pv = load_yaml(P["prompts_yaml"])
    for key, path in (("baseline", P["baseline"]),):
        raw = io.open(path, "rb").read()
        pv[key]["bytes"] = len(raw)
        pv[key]["lf_count"] = raw.count(b"\n")
        pv[key]["sha256"] = sha256_bytes(raw)
    for arm in ("KG-T", PLUGIN_ARM):
        spec = pv["variants"][arm]
        path = os.path.join(P["prompts_dir"], os.path.basename(spec["prompt_file"]))
        raw = io.open(path, "rb").read()
        spec["bytes"] = len(raw)
        spec["lf_count"] = raw.count(b"\n")
        spec["sha256"] = sha256_bytes(raw)
    with io.open(P["prompts_yaml"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(yaml.safe_dump(pv, allow_unicode=True, sort_keys=False, default_flow_style=False))


def selftest(P, g):
    """5 个负控 + 1 个空跑正控；全部在 tmp/ 下的临时目录内进行，不修改交付文件。"""
    tmp_root = os.path.join(REPO, "tmp")
    if not os.path.isdir(tmp_root):
        tmp_root = tempfile.gettempdir()
    base_dir = tempfile.mkdtemp(prefix="f5-selftest-", dir=tmp_root)
    g.section("[S] 负控自检（临时目录 %s；交付文件只读不动）" % os.path.relpath(base_dir, REPO))

    def fresh(tag):
        d = os.path.join(base_dir, tag)
        return _copy_delivery(d, P)

    def run(paths):
        sub = Gate(quiet=True)
        run_checks(paths, sub)
        return sub

    def evidence(sub):
        if not g.quiet:
            print("        -> 被拦住的是：%s" % ("; ".join(sub.failures[:3]) or "<未拦住>"))

    # 正控：未改动的副本必须 PASS
    p0 = fresh("positive")
    _refresh_yaml(p0)
    sub = run(p0)
    g.check(not sub.failures, "S0 正控：未改动的临时副本 PASS", "; ".join(sub.failures[:4]))

    # N1 变体改一字
    p1 = fresh("n1-variant-one-char")
    v1 = os.path.join(p1["prompts_dir"], os.path.basename(P["_spec"]["KG-T"]["prompt_file"]))
    data = io.open(v1, encoding="utf-8").read()
    io.open(v1, "w", encoding="utf-8", newline="\n").write(data.replace("因果结构最多 4 跳", "因果结构最多 5 跳"))
    _refresh_yaml(p1)
    sub = run(p1)
    evidence(sub)
    g.check(bool(sub.failures), "S1 负控：变体改一字 ⇒ 门禁 FAIL", "未失败（漏检）")

    # N2 删一条冻结差异
    p2 = fresh("n2-drop-one-diff")
    pv2 = load_yaml(p2["prompts_yaml"])
    pv2["variants"]["KG-T"]["frozen_diff"] = [d for d in pv2["variants"]["KG-T"]["frozen_diff"] if d["id"] != "D-3"]
    with io.open(p2["prompts_yaml"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(yaml.safe_dump(pv2, allow_unicode=True, sort_keys=False, default_flow_style=False))
    _refresh_yaml(p2)
    sub = run(p2)
    evidence(sub)
    g.check(bool(sub.failures), "S2 负控：删掉一条冻结差异 ⇒ 门禁 FAIL", "未失败（漏检）")

    # N3 基准里塞进一个禁用词（三份文件同位置追加同一行，故差异集合不变）
    p3 = fresh("n3-banned-word")
    junk = "本轮请勿使用 Cypher。\n"
    for path in (p3["baseline"], os.path.join(p3["prompts_dir"], os.path.basename(P["_spec"]["KG-T"]["prompt_file"])),
                 os.path.join(p3["prompts_dir"], os.path.basename(P["_spec"][PLUGIN_ARM]["prompt_file"]))):
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(junk)
    _refresh_yaml(p3)
    sub = run(p3)
    evidence(sub)
    g.check(bool(sub.failures) and any("禁用词" in f for f in sub.failures),
            "S3 负控：基准注入禁用词 ⇒ 禁用词断言 FAIL",
            "; ".join(sub.failures[:4]) or "未失败（漏检）")

    # N4 指纹失效
    p4 = fresh("n4-bad-sha")
    pv4 = load_yaml(p4["prompts_yaml"])
    pv4["baseline"]["sha256"] = "0" * 64
    with io.open(p4["prompts_yaml"], "w", encoding="utf-8", newline="\n") as fh:
        fh.write(yaml.safe_dump(pv4, allow_unicode=True, sort_keys=False, default_flow_style=False))
    sub = run(p4)
    evidence(sub)
    g.check(bool(sub.failures), "S4 负控：基准 sha256 失效 ⇒ 门禁 FAIL", "未失败（漏检）")

    # N5 基准多一行（只改基准，变体不动）
    p5 = fresh("n5-baseline-extra-line")
    with io.open(p5["baseline"], "a", encoding="utf-8", newline="\n") as fh:
        fh.write("额外约束：无。\n")
    _refresh_yaml(p5)
    sub = run(p5)
    evidence(sub)
    g.check(bool(sub.failures), "S5 负控：基准多一行 ⇒ 门禁 FAIL", "未失败（漏检）")

    # S7（R-50 显式偏离负控）：无声改掉身份面 ⇒ 必须 FAIL；补齐偏离声明 + 实现腿同名 ⇒ 必须 PASS
    def _fork_copy(dst, name):
        src = P.get("fork_src") or FORK_SRC_DEFAULT
        tgt = os.path.join(dst, name)
        shutil.copytree(src, tgt)
        return tgt

    def _drift_provider_id(p, new_pid, new_plugin_name, declare):
        tv = load_yaml(p["tools_yaml"])
        dp = tv["arms"][PLUGIN_ARM]["dify_plugin"]
        dp["provider_id"] = new_pid
        dp["plugin_name"] = new_plugin_name
        dp["machine_readback"]["provider_id"] = new_pid
        if declare:
            for d in dp["fork_provenance"]["deviations_from_official"]:
                if d.get("field") == "provider_id":
                    d["declared"] = new_pid
            dp["installation_status"] = "installed"
            dp["fork_provenance"]["installation_status"] = "installed"
        dump_yaml(p["tools_yaml"], tv)
        pv7 = load_yaml(p["prompts_yaml"])
        pv7["variants"][PLUGIN_ARM]["dify_plugin"]["provider_id"] = new_pid
        dump_yaml(p["prompts_yaml"], pv7)

    p7a = fresh("s7-a-silent-identity-drift")
    _drift_provider_id(p7a, "cc-ops/neo4j_cypher_query_x/neo4j_query", "neo4j_cypher_query_x", False)
    _refresh_yaml(p7a)
    sub = run(p7a)
    evidence(sub)
    hit = [f for f in sub.failures if f.startswith("E13-a") or f.startswith("E13-i")]
    g.check(bool(hit), "S7-a 负控：静默改掉 provider_id（不加偏离声明）⇒ 身份面断言 FAIL",
            "; ".join(sub.failures[:4]) or "未失败（漏检）")

    p7b = fresh("s7-b-declared-identity-drift")
    p7b["fork_src"] = _fork_copy(os.path.join(base_dir, "s7-b-src"), "neo4j_cypher_query_x")
    _drift_provider_id(p7b, "cc-ops/neo4j_cypher_query_x/neo4j_query", "neo4j_cypher_query_x", True)
    _refresh_yaml(p7b)
    sub = run(p7b)
    evidence(sub)
    g.check(not sub.failures, "S7-b 正控：同一改动补齐显式偏离声明 + 实现腿同名 ⇒ 门禁 PASS",
            "; ".join(sub.failures[:4]))

    g.check(os.path.isfile(P["baseline"]) and sha256_bytes(io.open(P["baseline"], "rb").read()) == P["_base_sha"],
            "S6 交付文件未被负控改动（基准 sha256 前后相同）")
    shutil.rmtree(base_dir, ignore_errors=True)
    return g


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dify-readback", default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--prompts-dir", default=None)
    ap.add_argument("--prompts-yaml", default=None)
    ap.add_argument("--tools-yaml", default=None)
    ap.add_argument("--infoprod-yaml", default=None)
    ap.add_argument("--fork-src", default=None)
    args = ap.parse_args()

    prompts_yaml = args.prompts_yaml or PROMPTS
    try:
        pv = load_yaml(prompts_yaml)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("无法读取 %s：%s\n" % (prompts_yaml, exc))
        return 2
    base_path = args.baseline or os.path.join(REPO, pv["baseline"]["prompt_file"])
    prompts_dir = args.prompts_dir or PROMPT_DIR
    P = {"baseline": base_path, "prompts_dir": prompts_dir, "prompts_yaml": prompts_yaml,
         "tools_yaml": args.tools_yaml or TOOLS, "infoprod_yaml": args.infoprod_yaml or INFOPROD,
         "fork_src": args.fork_src or FORK_SRC_DEFAULT,
         "dify_readback": args.dify_readback, "_spec": pv["variants"],
         "_base_sha": sha256_bytes(io.open(base_path, "rb").read())}

    print("=" * 74)
    print("F5 门禁 v2 · 方案3 所有组（提示词单一事实源 / arm policy: per-arm）")
    print("基准文件 : %s" % os.path.relpath(base_path, REPO).replace("\\", "/"))
    print("变体目录 : %s" % os.path.relpath(prompts_dir, REPO).replace("\\", "/"))
    print("=" * 74)

    g = Gate()
    run_checks(P, g)
    if args.selftest:
        selftest(P, g)

    print("\n" + "=" * 74)
    total = g.checks
    if g.failures:
        print("F5 RESULT: FAIL  （%d/%d 项通过，%d 项失败）" % (total - len(g.failures), total, len(g.failures)))
        for f in g.failures:
            print("  - %s" % f)
        print("=" * 74)
        return 1
    print("F5 RESULT: PASS  （%d/%d 项通过）" % (total, total))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
