#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G8 · 信息可及性对齐门禁（docs/00 §3.1 C3 v1.4 重述 / 裁决 9 / ★ v1.1 裁决 12）

断言：parity_required 中的每一项信息产品 IP，每个**主对比组**都必须至少有 1 条可达路径。
主对比组由契约动态推导（access 中除 BASE / KG-T-dyn 之外的全部），
⇒ v1.1 新增 KG-T-free（Neo4j 插件直连）后**本脚本无需枚举组名**。

为什么需要它：方案3 的组工具面**必然不同**（KG-T 直读拓扑且无 cc_query；
KG-T-free 只有第三方插件 neo4j_query，连 :8303 都不经）。C3 因此由「工具名逐字相同」
改为「信息可及性对齐」。若没有这条机械门禁，「对齐」会退化成人工比对工具名，
而人工比对恰恰是这套 A/B 最该避免的。

★ 本脚本只校验**契约层面**的可达性。它**不能**证明投影器真的把数据落进了图：
KG-T-free 的 IP-FACT 依赖「CCF 记录 → :Evidence 节点」的投影（风险 R16 / 任务 M1.5c）。
带 hard_dependency 字段的组会在输出中单独列出，提示在**实机快照**上复判。

用法：
    python shared/verify/g8_info_parity.py
退出码：0 = 通过；1 = 失败（列出缺口）。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    # ★ 2026-10-10 用户裁定 ⑨：**缺依赖 = 未测量 ⇒ BLOCKED(2)**，不得报 FAIL。
    #   原实现 print("FAIL: ...") + exit 1 ⇒ 把「没测到」误报成「测得坏结果」，
    #   违反本仓铁律 BLOCKED ≠ FAIL，也与同族门禁不一致
    #   （contract_gates.py:26 与 f5_kgt_gate.py:83 缺 PyYAML 时均 exit 2）。
    print("BLOCKED (exit=2)：需要 PyYAML（pip install pyyaml）⇒ 本门禁未执行；"
          "未测量 ≠ 测得坏结果（BLOCKED ≠ FAIL）", file=sys.stderr)
    raise SystemExit(2)

# ★ Windows 控制台默认 GBK：含中文/表情符号的输出必须先切 UTF-8，
#   否则 print 会抛 UnicodeEncodeError（本脚本 v1.1 加了 ⚠ 行后即触发过）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONTRACT = Path(__file__).resolve().parents[1] / "model" / "info-products.yaml"

# ★ R-55：工具名权威的**推导来源**（本门禁不硬编码任何工具名 —— HB6）
MODEL_DIR = Path(__file__).resolve().parents[1] / "model"
VOCAB_SOURCES = ("tools-setb.yaml#tools", "kgt-tools.yaml#tools[].tool_name",
                 "kgt-tools.yaml#allowed_tools_kgt_free", "kgt-tools.yaml#allowed_tools_kgt_dyn",
                 "live-facts-tools.yaml#names")


def tool_name_vocabulary() -> dict:
    """从兄弟契约推导「合法工具名 → 来源」映射。

    ★ 刻意**不含** `forbidden_tools_*`：禁名（`run_rules` / `validate` 等）不得进
    「合法名」集合，否则本门禁会把禁令名单当成可用工具放行。
    """
    vocab: dict = {}

    def add(name, src):
        if isinstance(name, str) and name:
            vocab.setdefault(name, src)

    p = MODEL_DIR / "tools-setb.yaml"
    if p.is_file():
        ts = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k in (ts.get("tools") or {}):
            add(k, "tools-setb.yaml#tools")
    p = MODEL_DIR / "kgt-tools.yaml"
    if p.is_file():
        kt = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for t in (kt.get("tools") or []):
            if isinstance(t, dict):
                add(t.get("tool_name"), "kgt-tools.yaml#tools[].tool_name")
        for key in ("allowed_tools_kgt_free", "allowed_tools_kgt_dyn"):
            for x in (kt.get(key) or []):
                add(x, "kgt-tools.yaml#%s" % key)
    p = MODEL_DIR / "live-facts-tools.yaml"
    if p.is_file():
        lf = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        nm = lf.get("names") or {}
        for k in ("primary", "free_alias"):
            add(nm.get(k), "live-facts-tools.yaml#names.%s" % k)
    return vocab


# 基线与补充组不作对齐要求（见契约 gate.exemptions）
EXEMPT_ARMS = {"BASE", "KG-T-dyn"}


def main() -> int:
    doc = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))

    products = [p["code"] for p in doc["info_products"]]
    required = list(doc["parity_required"])
    access = doc["access"]
    main_arms = [a for a in access if a not in EXEMPT_ARMS]

    errors: list[str] = []

    # 0) parity_required 必须是 info_products 的子集
    unknown = [p for p in required if p not in products]
    if unknown:
        errors.append(f"parity_required 引用了未定义的信息产品: {unknown}")

    # 1) 每个主对比组必须覆盖全部 required
    for arm in main_arms:
        paths = access[arm].get("paths") or {}
        missing = [p for p in required if not paths.get(p, {}).get("via")]
        if missing:
            errors.append(f"组 {arm} 缺信息产品: {missing}")
        # 1b) via 中引用的工具必须在 tools 列表中声明
        declared = set(access[arm].get("tools") or [])
        for ip, spec in paths.items():
            for tool in spec.get("via") or []:
                if tool not in declared:
                    errors.append(f"组 {arm} 的 {ip} 引用了未声明的工具: {tool}")

    # 1c) ★ R-55（2026-10-08）：每个臂声明的工具名必须**真实存在**。
    #    为什么补这条：1b 只校验「via ⊆ 本臂 tools」—— BASE 当年两边都写同一个名字，
    #    于是**自洽地通过**，而那个名字（`cc_get_facts`）在四个服务里**0 命中**，
    #    是 D47 改名前的幽灵残留。这类缺陷**只有跨契约比对才能发现**。
    #    权威集合由**兄弟契约推导**（本门禁不硬编码任何工具名 —— HB6）：
    #      tools-setb.yaml#tools 的键 ∪ kgt-tools.yaml#tools[].tool_name
    #      ∪ kgt-tools.yaml#allowed_tools_kgt_{free,dyn} ∪ live-facts-tools.yaml#names
    #    ★ 刻意**不含** forbidden_tools_*（禁名不得进「合法名」集合）。
    vocab = tool_name_vocabulary()
    for arm, spec in access.items():
        for tool in (spec.get("tools") or []):
            if tool not in vocab:
                errors.append(
                    f"组 {arm} 声明了**不存在**（未在兄弟契约中定义）的工具名 {tool!r}；"
                    f"权威集合（{len(vocab)} 个）来自 " + "、".join(VOCAB_SOURCES))

    # 2) 每个信息产品至少被一个主对比组覆盖（防空契约）
    for p in required:
        if not any((access[a].get("paths") or {}).get(p, {}).get("via") for a in main_arms):
            errors.append(f"信息产品 {p} 在所有主对比组中均不可达（契约形同虚设）")

    # 3) 同一对照对内部必须逐字相同（C3 保留的严格部分）
    for a, b in (("KG-Q", "ONTO-Q"), ("KG-R", "ONTO-R")):
        if a in access and b in access:
            ta, tb = access[a].get("tools"), access[b].get("tools")
            if ta != tb:
                errors.append(f"同一对照对 {a} / {b} 的工具列表不逐字相同: {ta} != {tb}")

    print(f"G8 信息可及性对齐 | 契约 {doc['meta']['version']} | 主对比组 {main_arms}")
    print(f"  信息产品 {len(products)} 项，要求对齐 {len(required)} 项")
    for arm in access:
        paths = access[arm].get("paths") or {}
        got = sum(1 for p in required if paths.get(p, {}).get("via"))
        flag = "（豁免）" if arm in EXEMPT_ARMS else ""
        print(f"  {arm:10s} {got}/{len(required)} {flag}")

    # 3b) ★ 有前置依赖的组：契约可达，但依赖不在本契约范围内（需实机复判）
    for arm in main_arms:
        if access[arm].get("hard_dependency"):
            print(f"  ⚠ {arm} 有实机前置依赖：{access[arm]['hard_dependency'].strip()[:96]}...")

    if errors:
        print("\nFAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nPASS: 信息可及性对齐成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())