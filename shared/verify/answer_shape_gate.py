#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""G23 · **answer_shape 对齐门禁**（只读；退出码 0 PASS / 1 FAIL / 2 BLOCKED·PARAM_ERROR）。

## 为什么需要它（M2.22 / G23：把悬空契约落成执行点）
`docs/00-总体设计与裁决记录.md:326-341`（§6.3 等价性自检）明写：

    for each 用例 q in 用例集:
        assert answer_shape_kg(q) == answer_shape_onto(q)   # 形状，非内容

> 不自检通过，评测结果**不得采信**。这是防「分母不同」的硬门禁。

`:343` 补充分工：「**①②③ 管实例与回答的一致性（ABox 已被强制统一、不计入成本）**」
⇒ ③ 的期望是**相等**（不适用 ④ 的排除清单）。

现状：`shared/verify/parity.py`（G14）实现了 ①②，但在报告与 `--json` 里**自报
`answerShapeChecked: false`** ⇒ **③ 至今没有任何执行点**，而它是「所有评测结果可采信」
的前置条件。本文件就是补那个洞的执行点。

## M2.26：③ 的比对面 = **契约声明面**（`declared_surface`）
M2.22 的首跑在真机上留下两条**真实** DIFF（Q05 故障影响面 / Q08 证据落地），根因是两个独立的
载体事实：① **边属性在 RDF 里无处安放**（LPG 的边是一等资源可直接带属性；RDF 三元组不可带属性，
只能具体化或改挂个体）；② **「类」的代表形式不同**（LPG 里类只是标签、不是节点 ⇒ `CC_AFFECTS`
落到 42 个实例；RDF 里类是资源 ⇒ `ccf:affects` 落到 14 个类）。两者都是**建模成本**，不是投影
缺陷，故**不改两侧原生形态**（不动 Cypher/P1/P2/映射表、不新增类型节点），改为在**契约声明的
信息面**上比对：`cases[*].declared_surface` 逐列声明该例的 canonical 列（给出两侧原生 token 与
`source`，跨载体项另给 `reason` + `carrier_position` 对照 id），门禁把两侧描述符**投影**到该面，
再用**同一个** `compare_descriptors` 比对；`carrier_specific_columns` 声明某侧独有的列（另一侧
本就没有）。原生形状另存 `native_shape_kg` / `native_shape_onto` 并逐例打印，**不抹平**。

## 判据（逐条编号，报告里逐条打印）
  G23-00 **可达性/前置条件**：任一载体不可达、契约文件缺失/空/不可解析、映射表缺失、
          `CC_NEO4J_PASSWORD` 缺失、ONTO 侧不存在 snapshotId 对应命名图 ⇒ **BLOCKED**
          （措辞与退出码照抄 `shared/verify/parity.py` 的 M2.18 先例）。**绝不打印 PASS**。
  G23-01 **用例层**：逐条执行契约 `cases[*]` 的两侧查询，各自归约成形状描述符后逐字段比对。
  G23-02 **关系粒度层**：对 `shared/mapping/rel-map.yaml` 中每一条 `asserted: true` 的关系型，
          两侧各跑一次「端点身份键」形状查询，**只比形状**（列集合 / 行数量级 / 作用域），
          **不比身份值** —— 值等归 G22 `shared/verify/abox_identity_gate.py`（G22-07 / G22-08）。
          契约口径：**值等 → G22；形等 → G23**。
  G23-03 **反恒真下限**：`--min-cases`（默认 8）非法 ⇒ `PARAM_ERROR` exit 2；实际用例数
          < `--min-cases` ⇒ ERROR。另要求至少 3 个用例的 `rows_class != zero`（至少一侧成立），
          否则报「空集相等」恒真风险 ⇒ 真机默认 WARN、`--strict` 升 ERROR。
  G23-04 **分侧计数与明细**：逐用例给出 shape_kg / shape_onto / SAME|DIFF|unresolved，汇总
          cases_total / same / diff / unresolved；有任何 DIFF ⇒ FAIL exit 1；有 unresolved 且
          无 DIFF ⇒ 视 `--strict`（默认 WARN + exit 0，`--strict` ⇒ exit 1）。
  G23-05 **幂等/只读**：只发 SELECT/MATCH；不得写入任何库；重复运行结果一致。
  G23-06 **归一化 fail-closed**：不可归一化的类型/属性/关系型、无标签的行作用域变量、
          非 `<var>.<prop>` 形态的返回表达式 ⇒ ERROR。
  G23-07 **禁止短路**：任一侧查询报错/超时/HTTP 非 200 ⇒ 该用例记 `unresolved`
          （`--strict` 升 ERROR），**绝不用空描述符代替**。
          （合法空结果 `rows_class=zero` 是**真实描述符**，不属 unresolved。）
  G23-08（未分配：留给后续轮次的新判据，编号不跳用。）
  G23-09 **声明面非空且逐列有据**：每个用例的 `declared_surface.columns` 必须 ≥1 条，且每条必须
          有非空 `source`（文档路径 + §号，不写行号）⇒ 否则 ERROR。声明面 = ③ 的比对面；空声明面
          会让比对退化成恒真，故 fail-closed。
  G23-10 **跨载体对照与排除项逐条有据**：`declared_surface.columns` 里两侧原生 token **不同**的
          条目必须给出非空 `reason`，且 `carrier_position` 必须指向 `neutral_extension` 的
          `carrier_position.entries` 里**存在**的条目 id（该条目登记的两侧列 token 必须逐字一致；
          悬空引用同样 ERROR）；`carrier_specific_columns` 每条必须有非空 `reason` + `source`。
          **表级**：`carrier_position.entries` 本身逐条必须有 `id` / `neutral` / 两侧
          `carrier_position.kg` + `.onto`（写清"不存在"也算有）/ `reason` / `source`，且有列 token
          的一侧必须登记该侧原生名（`kg_native` / `onto_native`）⇒ 缺表或缺项一律 ERROR。
  G23-11 **声明列必须真实存在（反恒真闸门）**：声明条目的 `kg` / `onto` 原生 token 必须分别出现
          在该侧**原生**描述符的 `columns` 里；`carrier_specific_columns` 的 token 必须出现在其
          声明的一侧、且在另一侧**不存在**。否则 ERROR。声明面只在**内部**把两侧对齐到同一粒度：
          **未被声明覆盖的原生 token 不静默丢弃、仍照原样参与比对 ⇒ 如实判 DIFF**。

## 诚实边界（必须与 shared/model/answer-shape.yaml 的 honest_boundaries 一致）
  B1. PASS 的含义**只是**「两款载体的回答结构一致」，**不是**「回答内容正确」。
  B2. ③ 属 ABox 一致性（docs/00:343），**不计入成本**，且 **PASS 不能补偿 T10 的失败**。
  B3. 今天**没有** 50 条正式评测用例集（那是 M5，且受 docs/06 §9 的 U-D 用例规模裁决阻塞）
      ⇒ 契约只**预留**扩展位（`extensions.fifty_case_slot`），本门禁**不发明**那 50 条。
  B4. 两侧载体型号不同（Neo4j 5.26.30 Community / GraphDB 10.8.0）⇒ 某些形态差异
      （空结果 vs 无绑定、多重边 vs 三元组集合、TBox 个体 vs ABox 实例）会表现为形状不同，
      **必须逐条如实记录，不得在归一化里「抹平」**。
  B5. PASS 的含义**只是**「在**契约声明面**（`declared_surface`）上一致」：声明面是契约声明的
      信息粒度与列集合，**不是**两款载体的全部原生信息 —— 两个原生差异（「类」的代表形式、边
      属性的承载位置）仍然存在，本门禁**不**据此宣称两款载体等价，也**不**证明两侧内容/语义
      等价；原生形态逐例打印在 `native_shape_kg` / `native_shape_onto` 与 `--json` 里，不抹平。

## 用法
    # 真机（先设凭据；缺凭据 ⇒ BLOCKED exit 2，不会打印 PASS）
    set CC_NEO4J_PASSWORD=...
    python shared/verify/answer_shape_gate.py
    python shared/verify/answer_shape_gate.py --strict --json --out tmp/g23.json
    python shared/verify/answer_shape_gate.py --min-cases 8 --contract shared/model/answer-shape.yaml

退出码：0 = PASS；1 = FAIL（DIFF / ERROR）；2 = BLOCKED 或 PARAM_ERROR。
**BLOCKED ≠ FAIL。**
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

try:  # 控制台编码兜底（纪律：证据脚本自身也要在当前控制台编码下跑通）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

try:
    import yaml
except ImportError:  # pragma: no cover
    print("★ 前置条件不满足：本机 Python 缺 PyYAML（import yaml 失败）⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2")
    raise SystemExit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONTRACT = os.path.join(ROOT, "shared", "model", "answer-shape.yaml")
DEFAULT_TARGETS = os.path.join(ROOT, "shared", "deploy", "targets.yaml")

F = []  # [(severity, code, message)]


def err(code, msg):
    F.append(("ERROR", code, msg))
    print("  [ERROR] %s: %s" % (code, msg))


def warn(code, msg):
    F.append(("WARN", code, msg))
    print("  [WARN ] %s: %s" % (code, msg))


def note(code, msg):
    F.append(("NOTE", code, msg))
    print("  [NOTE ] %s: %s" % (code, msg))


def n_err():
    return len([f for f in F if f[0] == "ERROR"])


def n_warn():
    return len([f for f in F if f[0] == "WARN"])


# =============================================================================
# 0. 纯函数层（离线可测；tmp/_m222_selftest.py 与 tmp/g23_counterexample.py 直接 import）
# =============================================================================

def rows_class(n):
    """行数量级档：zero / one / many（契约 descriptor.rows_class_rules 逐字）。"""
    if n == 0:
        return "zero"
    if n == 1:
        return "one"
    return "many"


def rows_band(n):
    """更细的行数分档（只进报告，不参与比对）。"""
    if n == 0:
        return "b0"
    if n == 1:
        return "b1"
    if n <= 4:
        return "b2"
    if n <= 16:
        return "b3"
    if n <= 64:
        return "b4"
    return "b5"


READONLY_BAN = re.compile(
    r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|INSERT|LOAD|CLEAR|ADD|COPY|MOVE|INTO)\b",
    re.IGNORECASE,
)


def assert_readonly(q):
    """G23-05：只接受只读查询。返回 None 表示通过，否则返回命中的写子句。"""
    m = READONLY_BAN.search(q)
    return m.group(0).upper() if m else None


def compare_descriptors(d_kg, d_onto):
    """逐字段比对（契约 descriptor.compared_fields）。返回 (status, diffs)。"""
    if d_kg is None or d_onto is None:
        return "unresolved", []
    diffs = []
    for fld in ("grain_scope", "columns", "bound_columns", "realized_scope", "rows_class"):
        a, b = d_kg.get(fld), d_onto.get(fld)
        if a != b:
            diffs.append({"field": fld, "kg": a, "onto": b})
    return ("SAME" if not diffs else "DIFF"), diffs


def check_min_cases(n_cases, min_cases):
    """G23-03 前半：实际用例数 < --min-cases ⇒ ERROR。"""
    if n_cases < min_cases:
        return "n_cases=%d < --min-cases=%d ⇒ 反恒真下限不满足（用例集被悄悄缩小）" % (n_cases, min_cases)
    return None


def check_nonzero_lower_bound(case_statuses, strict, need=3):
    """G23-03 后半：至少 need 个用例的 rows_class != zero（至少一侧）。

    case_statuses: [(case_id, rows_class_kg, rows_class_onto), ...]
    返回 (severity_or_None, message)。真机默认 WARN；--strict 升 ERROR。
    自证脚本用同一函数把判据触发成 ERROR（证明判据不是恒真）。
    """
    good = [cid for cid, a, b in case_statuses if a != "zero" or b != "zero"]
    if len(good) >= need:
        return None, None
    sev = "ERROR" if strict else "WARN"
    msg = ("仅 %d 个用例的 rows_class != zero（要求 >= %d；非 zero 的用例：%s）"
           " ⇒ 「空集相等」恒真风险：两侧都空时描述符必然相等，比对不具判别力"
           % (len(good), need, ", ".join(good) if good else "无"))
    return sev, msg


# =============================================================================
# 0b. 声明面（declared surface）：③ 的比对面是**契约声明的信息面**
#     —— 为什么需要它：两个载体的原生形态有真实差异（「类」的代表形式、边属性的承载位置），
#        那是建模成本而不是投影缺陷。把两侧**投影**到契约声明的同一粒度后再比对，
#        投影规则全部来自契约数据；**未被声明覆盖的原生 token 一律原样保留**（⇒ DIFF，
#        绝不静默丢弃），因此声明面不能被用来"把差异删掉当解法"。
# =============================================================================

TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_|]*)\.([A-Za-z][A-Za-z0-9_]*)$")


def token_scope(token):
    """`<作用域>.<中性名>` ⇒ 作用域；不合形态 ⇒ None。"""
    m = TOKEN_RE.match(token or "")
    return m.group(1) if m else None


def carrier_position_index(ext):
    """neutral_extension.carrier_position.entries ⇒ {id: entry}（数据驱动；缺表 ⇒ {}）。"""
    blk = (ext or {}).get("carrier_position") or {}
    out = {}
    for e in (blk.get("entries") or []):
        if isinstance(e, dict) and e.get("id"):
            out[e["id"]] = e
    return out


def check_carrier_positions(ext):
    """G23-10（表级）：跨载体对照表本身必须逐条可审计。

    一条 `neutral_extension.carrier_position.entries` 条目必须自带：
    `id` / `neutral` / `carrier_position.kg` + `.onto`（两侧位置都必须有文字，写"不存在"也算文字）/
    `reason` / `source`，且**有列 token 的一侧必须登记该侧的原生名**（`kg_native` / `onto_native`）。
    缺表 ⇒ ERROR（对照表未登记 ⇒ 跨载体声明无从核对）。
    """
    blk = (ext or {}).get("carrier_position") or {}
    entries = blk.get("entries")
    if not isinstance(entries, list) or not entries:
        return [("G23-10", "neutral_extension.carrier_position.entries 缺失或为空 ⇒ 跨载体对照表未登记")]
    out = []
    seen = set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            out.append(("G23-10", "carrier_position.entries[%d] 不是映射条目" % i))
            continue
        eid = e.get("id")
        if not str(eid or "").strip():
            out.append(("G23-10", "carrier_position.entries[%d] 缺 id" % i))
            continue
        if eid in seen:
            out.append(("G23-10", "carrier_position.entries 的 id 重复：%s" % eid))
        seen.add(eid)
        for key in ("neutral", "reason", "source"):
            if not str(e.get(key) or "").strip():
                out.append(("G23-10", "carrier_position %s 缺 %s" % (eid, key)))
        cp = e.get("carrier_position")
        if (not isinstance(cp, dict) or not str(cp.get("kg") or "").strip()
                or not str(cp.get("onto") or "").strip()):
            out.append(("G23-10", "carrier_position %s 缺两侧载体位置（carrier_position.kg / .onto 都要有文字）" % eid))
        for nm in ("kg_native", "onto_native"):
            v = e.get(nm)
            if v is not None and not isinstance(v, list):
                out.append(("G23-10", "carrier_position %s 的 %s 必须是列表" % (eid, nm)))
        if (not (isinstance(e.get("kg_native"), list) and e.get("kg_native"))
                and not (isinstance(e.get("onto_native"), list) and e.get("onto_native"))):
            out.append(("G23-10", "carrier_position %s 两侧原生名都是空 ⇒ 该条目没有登记任何载体形态" % eid))
        for tok_key, nat_key in (("kg", "kg_native"), ("onto", "onto_native")):
            if e.get(tok_key) and not (isinstance(e.get(nat_key), list) and e.get(nat_key)):
                out.append(("G23-10", "carrier_position %s 有 %s 列 token 却缺 %s 原生名（token 必须能回指载体原生形态）"
                            % (eid, tok_key, nat_key)))
    return out


def check_declared_surface(ds, cp_index):
    """G23-09 / G23-10（静态，不需要任何结果集）。返回 [(code, message), ...]。"""
    if not isinstance(ds, dict):
        return [("G23-09", "declared_surface 缺失（③ 的比对面未声明）⇒ fail-closed，不做比对")]
    out = []
    cols = ds.get("columns")
    if not isinstance(cols, list) or not cols:
        out.append(("G23-09", "declared_surface.columns 为空 ⇒ 声明面不得为空集（空集会让比对退化成恒真）"))
        cols = []
    for i, e in enumerate(cols):
        if not isinstance(e, dict):
            out.append(("G23-09", "declared_surface.columns[%d] 不是映射条目" % i))
            continue
        canon, kgt, ontt = e.get("canonical"), e.get("kg"), e.get("onto")
        if not canon or not kgt or not ontt:
            out.append(("G23-09", "declared_surface.columns[%s] 缺 canonical/kg/onto" % (canon or i)))
            continue
        if not str(e.get("source") or "").strip():
            out.append(("G23-09", "声明列 %s 缺 source（必须是文档路径 + §号）" % canon))
        cpid = e.get("carrier_position")
        if kgt != ontt:
            if not str(e.get("reason") or "").strip():
                out.append(("G23-10", "跨载体声明列 %s（kg=%s ↔ onto=%s）缺 reason" % (canon, kgt, ontt)))
            if not cpid:
                out.append(("G23-10", "跨载体声明列 %s 缺 carrier_position 引用" % canon))
        if cpid:
            if cpid not in cp_index:
                out.append(("G23-10", "声明列 %s 引用的 carrier_position=%s 在 neutral_extension.carrier_position.entries 里不存在（悬空引用）" % (canon, cpid)))
            else:
                cp = cp_index[cpid]
                if cp.get("kg") not in (None, kgt) or cp.get("onto") not in (None, ontt):
                    out.append(("G23-10", "声明列 %s 的 kg/onto（%s / %s）与 carrier_position %s 登记（%s / %s）逐字不一致"
                                % (canon, kgt, ontt, cpid, cp.get("kg"), cp.get("onto"))))
    for i, e in enumerate(ds.get("carrier_specific_columns") or []):
        if not isinstance(e, dict) or not e.get("token") or e.get("side") not in ("kg", "onto"):
            out.append(("G23-10", "carrier_specific_columns[%d] 缺 token 或 side（kg|onto）" % i))
            continue
        if not str(e.get("reason") or "").strip():
            out.append(("G23-10", "carrier_specific 列 %s 缺 reason" % e["token"]))
        if not str(e.get("source") or "").strip():
            out.append(("G23-10", "carrier_specific 列 %s 缺 source" % e["token"]))
        cpid = e.get("carrier_position")
        if cpid and cpid not in cp_index:
            out.append(("G23-10", "carrier_specific 列 %s 引用的 carrier_position=%s 不存在（悬空引用）" % (e["token"], cpid)))
        elif cpid:
            cp = cp_index[cpid]
            side = e["side"]
            other = "onto" if side == "kg" else "kg"
            if cp.get(side) != e["token"]:
                out.append(("G23-10", "carrier_specific 列 %s（side=%s）与 carrier_position %s 登记的 %s 列 %r 逐字不一致"
                            % (e["token"], side, cpid, side, cp.get(side))))
            if cp.get(other) is not None:
                out.append(("G23-10", "carrier_specific 列 %s 声明为 side=%s 专有，但 carrier_position %s 登记了 %s 列 %r ⇒ 载体专有性不成立"
                            % (e["token"], side, cpid, other, cp.get(other))))
        for c in cols:
            if isinstance(c, dict) and e["token"] in (c.get("canonical"), c.get("kg"), c.get("onto")):
                out.append(("G23-10", "列 %s 同时出现在 declared_surface.columns 与 carrier_specific_columns（自相矛盾）" % e["token"]))
                break
    return out


def check_declared_against_native(ds, cols_kg, cols_onto):
    """G23-11：声明列必须真实存在于该侧**原生** columns。返回 [(code, message), ...]。"""
    out = []
    ds = ds if isinstance(ds, dict) else {}
    sk, so = set(cols_kg or []), set(cols_onto or [])
    for e in (ds.get("columns") or []):
        if not isinstance(e, dict):
            continue
        canon = e.get("canonical")
        if e.get("kg") not in sk:
            out.append(("G23-11", "声明列 %s 的 kg 原生 token %r 不在 KG 原生 columns 里（声明面与查询结构不一致）"
                        % (canon, e.get("kg"))))
        if e.get("onto") not in so:
            out.append(("G23-11", "声明列 %s 的 onto 原生 token %r 不在 ONTO 原生 columns 里（声明面与查询结构不一致）"
                        % (canon, e.get("onto"))))
    for e in (ds.get("carrier_specific_columns") or []):
        if not isinstance(e, dict):
            continue
        tok, side = e.get("token"), e.get("side")
        own, other = (sk, so) if side == "kg" else (so, sk)
        if tok not in own:
            out.append(("G23-11", "carrier_specific 列 %s 声明的 side=%s 侧原生 columns 里没有它" % (tok, side)))
        if tok in other:
            out.append(("G23-11", "carrier_specific 列 %s 声明为 side=%s 专有，但它也出现在另一侧原生 columns 里"
                        % (tok, side)))
    return out


def declared_surface_summary(ds):
    """--json / 报告用的声明面摘要（含计数）。"""
    ds = ds if isinstance(ds, dict) else {}
    cols = [e for e in (ds.get("columns") or []) if isinstance(e, dict)]
    csc = [e for e in (ds.get("carrier_specific_columns") or []) if isinstance(e, dict)]
    return {
        "columns_count": len(cols),
        "carrier_specific_columns_count": len(csc),
        "columns": cols,
        "carrier_specific_columns": csc,
    }


def project_canonical(desc, ds, side):
    """把**原生**描述符投影到契约声明的信息面（canonical）。规则逐条可审计：

      - 声明列：原生 token → canonical（`<作用域>.<中性名>`）。
      - `carrier_specific_columns`：本侧声明的**载体专有列**，排除（另一侧本就没有）。
      - **未在声明面覆盖的原生 token 原样保留** ⇒ 会如实判 DIFF，绝不静默丢弃。
      - grain_scope = canonical 列的作用域 ∪ 「未被任何已返回列承载的原生 grain 项」（原样保留）。
      - bound_columns / realized_scope：由 canonical 列集合同法投影。
      - rows / rows_class / rows_band：沿用原生（行数量级不因声明面而改变）。
    """
    ds = ds if isinstance(ds, dict) else {}
    cmap = {}
    for e in (ds.get("columns") or []):
        if not isinstance(e, dict):
            continue
        tok = e.get("kg") if side == "kg" else e.get("onto")
        if tok and e.get("canonical"):
            cmap[tok] = e["canonical"]
    excl = set(e.get("token") for e in (ds.get("carrier_specific_columns") or [])
               if isinstance(e, dict) and e.get("side") == side)
    native_cols = list(desc.get("columns") or [])
    native_bound = list(desc.get("bound_columns") or [])
    cols = sorted(set(cmap.get(t, t) for t in native_cols if t not in excl))
    bound = sorted(set(cmap.get(t, t) for t in native_bound if t not in excl))
    carried = set(s for s in (token_scope(t) for t in native_cols) if s)
    grain = set(s for s in (token_scope(c) for c in cols) if s)
    for g in (desc.get("grain_scope") or []):
        if g not in carried:
            grain.add(g)
    out = dict(desc)
    out["columns"] = cols
    out["bound_columns"] = bound
    out["grain_scope"] = sorted(grain)
    out["realized_scope"] = sorted(set(s for s in (token_scope(c) for c in bound) if s))
    return out


# =============================================================================
# 1. 载入契约与两张映射表（映射是数据不是代码）
# =============================================================================

def read_text(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def read_yaml(path):
    return yaml.safe_load(read_text(path))


class Mapping(object):
    """由 shared/mapping/type-map.yaml + rel-map.yaml 构造的双向查表器。

    代码里**不写死任何载体专有列名**：全部来自 yaml。
    """

    def __init__(self, tm, rm, ext):
        self.tm = tm
        self.rm = rm
        self.ext = ext or {}
        self.prefixes = dict(tm.get("prefixes") or {})
        self.base = ((tm.get("namespaces") or {}).get("onto") or {}).get("base") or ""
        self.kg2neutral_type = {}
        self.onto2neutral_type = {}
        for name, spec in (tm.get("types") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("kg_label"):
                self.kg2neutral_type[spec["kg_label"]] = name
            if spec.get("onto_class"):
                self.onto2neutral_type[self.expand(spec["onto_class"])] = name
        for k, v in (self.ext.get("kg_label") or {}).items():
            self.kg2neutral_type.setdefault(k, v)
        for k, v in (self.ext.get("onto_class") or {}).items():
            self.onto2neutral_type.setdefault(self.expand(k), v)

        self.kg1 = {}      # 载体属性名 -> 中性属性名
        self.onto1 = {}
        for _name, spec in (tm.get("types") or {}).items():
            if not isinstance(spec, dict):
                continue
            for attr, aspec in (spec.get("attrs") or {}).items():
                if isinstance(aspec, dict):
                    if aspec.get("kg_property"):
                        self.kg1.setdefault(aspec["kg_property"], attr)
                    if aspec.get("onto_property"):
                        self.onto1.setdefault(self.expand(aspec["onto_property"]), attr)
        for mname, mspec in (tm.get("metrics") or {}).items():
            if not isinstance(mspec, dict):
                continue
            if mspec.get("kg_property"):
                self.kg1.setdefault(mspec["kg_property"], mname)
            if mspec.get("onto_property"):
                self.onto1.setdefault(self.expand(mspec["onto_property"]), mname)
        for neutral, carrier in ((tm.get("common_properties") or {}).get("kg") or {}).items():
            self.kg1.setdefault(carrier, neutral)
        for neutral, carrier in ((tm.get("common_properties") or {}).get("onto") or {}).items():
            self.onto1.setdefault(self.expand(carrier), neutral)

        self.kg_rel = {}
        self.onto_rel = {}
        for name, spec in (rm.get("relations") or {}).items():
            if isinstance(spec, dict) and spec.get("kg_reltype"):
                self.kg_rel[spec["kg_reltype"]] = name
            if isinstance(spec, dict) and spec.get("onto_property"):
                self.onto_rel[self.expand(spec["onto_property"])] = name
        for name, spec in (rm.get("inverse_relations") or {}).items():
            if isinstance(spec, dict) and spec.get("kg_reltype"):
                self.kg_rel[spec["kg_reltype"]] = name
            if isinstance(spec, dict) and spec.get("onto_property"):
                self.onto_rel[self.expand(spec["onto_property"])] = name
        for k, v in (self.ext.get("kg_reltype") or {}).items():
            self.kg_rel.setdefault(k, v)
        for k, v in (self.ext.get("onto_reltype") or {}).items():
            self.onto_rel.setdefault(self.expand(k), v)
        self.kg_rel_attr = dict(self.ext.get("kg_property") or {})
        self.onto_rel_attr = {}
        for k, v in (self.ext.get("onto_property") or {}).items():
            self.onto_rel_attr[self.expand(k)] = v
        # M2.26：跨载体「原生名 ↔ 载体位置」对照表（同样是**数据**，代码不写死任何条目的 id/名字）
        self.carrier_positions = carrier_position_index(self.ext)

    def expand(self, curie):
        if curie.startswith("<") and curie.endswith(">"):
            return curie[1:-1]
        if curie.startswith("http://") or curie.startswith("https://"):
            return curie
        if ":" in curie:
            p, local = curie.split(":", 1)
            if p in self.prefixes:
                return self.prefixes[p] + local
        return self.base + curie

    # ---- 归一化入口（fail-closed：查不到返回 None，调用方必须报 ERROR）----
    def neutral_type_kg(self, label):
        return self.kg2neutral_type.get(label)

    def neutral_type_onto(self, iri):
        return self.onto2neutral_type.get(iri)

    def attr_kg(self, prop):
        return self.kg1.get(prop)

    def attr_onto(self, iri):
        return self.onto1.get(iri)


# =============================================================================
# 2. 查询文本解析（静态部分：grain_scope / columns）
# =============================================================================

class ParseError(Exception):
    pass


def _split_top(s, sep=","):
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


CY_NODE = re.compile(r"\(\s*(\w+)\s*:\s*(CC_[A-Za-z0-9_]+)")
CY_REL = re.compile(r"\[\s*(\w+)\s*:\s*(CC_[A-Za-z0-9_|]+)")
CY_RET = re.compile(r"^(\w+)\.(\w+)\s+AS\s+(\w+)$", re.IGNORECASE | re.DOTALL)
CY_ITEMS = re.compile(r"(\w+\.\w+\s+AS\s+\w+)", re.IGNORECASE)


def parse_cypher(q, mp, overrides):
    """返回 {'scope': {var: neutral_type}, 'cols': [{'alias','neutral','var'}]}。

    只处理契约写法的只读 Cypher：`<var>.<prop> AS <alias>` 的 RETURN 项。
    """
    scope = {}
    for m in CY_NODE.finditer(q):
        var, lab = m.group(1), m.group(2)
        nt = mp.neutral_type_kg(lab)
        if nt is None:
            raise ParseError("unmappable_kg_label: %s（type-map 与 answer-shape.yaml#neutral_extension 都查不到）" % lab)
        scope[var] = nt
    for m in CY_REL.finditer(q):
        var, rels = m.group(1), m.group(2)
        codes = []
        for r in rels.split("|"):
            rc = mp.kg_rel.get(r)
            if rc is None:
                raise ParseError("unmappable_kg_reltype: %s" % r)
            codes.append(rc)
        scope[var] = "|".join(sorted(codes))

    m = None
    for m in re.finditer(r"\bRETURN\b", q, re.IGNORECASE):
        pass
    if not m:
        raise ParseError("no_return_clause")
    tail = q[m.end():]
    tail = re.split(r"\bORDER\s+BY\b|\bLIMIT\b", tail, flags=re.IGNORECASE)[0]
    # M2.26：容忍 RETURN 的 DISTINCT / ALL 修饰符。契约 Q05 用 DISTINCT 把实例行归一到类型码
    # （一条 AFFECTS 断言在 LPG 里的**实例级**展开会产生多行；按类型码去重后才是 ONTO 的类级
    # 粒度）。修饰符只影响**行数**、不影响静态列集合 ⇒ 不改变任何比对逻辑。
    tail = re.sub(r"^\s*(DISTINCT|ALL)\b", "", tail, count=1, flags=re.IGNORECASE)
    cols = []
    for item in _split_top(re.sub(r"\s+", " ", tail)):
        mm = CY_RET.match(item.strip())
        if not mm:
            raise ParseError("unnormalizable_return_expr: %r（只接受 `<var>.<prop> AS <alias>`）" % item)
        var, prop, alias = mm.group(1), mm.group(2), mm.group(3)
        neutral = mp.attr_kg(prop)
        if neutral is None:
            neutral = mp.kg_rel_attr.get(prop)
        if neutral is None:
            raise ParseError("unmappable_kg_property: %s（type-map/metrics/common_properties 与 neutral_extension 都查不到）" % prop)
        cols.append({"alias": alias, "neutral": neutral, "var": var})
    return {"scope": scope, "cols": cols}


SP_TYPE = re.compile(r"\?(\w+)\s+a\s+([A-Za-z_]\w*:[A-Za-z0-9_]+|https?://\S+)")
SP_TOK = re.compile(r"\?(\w+)|([A-Za-z_]\w*:[A-Za-z0-9_]+)|(IRI\d+)|(a)\b|([{};.])")


def sparql_owners(q):
    """扫出 `?alias` 的绑定来源 (subject_var, predicate)，支持 `;` 谓宾列表与 OPTIONAL 块。

    例：`?app a cc:CreditCardApp ; cc:ccfId ?appId .` ⇒ {'appId': ('app', 'cc:ccfId')}
    """
    iri_map = {}

    def _prot(m):
        k = "IRI%d" % len(iri_map)
        iri_map[k] = m.group(0)
        return k

    mw = re.search(r"\bWHERE\b", q, re.IGNORECASE)
    body = q[mw.end():] if mw else q
    body = re.sub(r"<[^>]*>", _prot, body)
    owner = {}
    subj = None
    pred = None
    for m in SP_TOK.finditer(body):
        var, curie, iri, kw, punct = (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        if punct:
            pred = None
            if punct != ";":
                subj = None
            continue
        if kw == "a":
            continue
        if var is not None:
            if pred is not None and subj is not None:
                owner.setdefault(var, (subj, pred))
                pred = None
            else:
                subj = var
                pred = None
            continue
        if subj is not None:
            pred = curie or iri
    return owner, iri_map


def parse_sparql(q, mp, overrides):
    """返回 {'scope': {var: neutral_type}, 'cols': [{'alias','neutral','var'}]}。"""
    scope = {}
    for m in SP_TYPE.finditer(q):
        var, cls = m.group(1), m.group(2)
        iri = mp.expand(cls.rstrip(";,"))
        nt = mp.neutral_type_onto(iri)
        if nt is None:
            raise ParseError("unmappable_onto_class: %s（type-map 与 answer-shape.yaml#neutral_extension 都查不到）" % cls)
        scope[var] = nt

    m = re.search(r"\bSELECT\b(.*?)\bWHERE\b", q, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ParseError("no_select_clause")
    aliases = re.findall(r"\?(\w+)", m.group(1))
    if not aliases:
        raise ParseError("no_select_vars")

    owner, _iri_map = sparql_owners(q)

    cols = []
    for alias in aliases:
        if alias not in owner:
            raise ParseError("unnormalizable_return_expr: ?%s 在 WHERE 里没有 `?var <prop> ?%s` 形态的绑定" % (alias, alias))
        sub, pred = owner[alias]
        iri = mp.expand(pred.rstrip(";,"))
        neutral = mp.attr_onto(iri)
        if neutral is None:
            neutral = mp.onto_rel_attr.get(iri)
        if neutral is None:
            raise ParseError("unmappable_onto_property: %s（type-map/metrics/common_properties 与 neutral_extension 都查不到）" % pred)
        cols.append({"alias": alias, "neutral": neutral, "var": sub})
    return {"scope": scope, "cols": cols}


def reduce_descriptor(static, rows, overrides, side):
    """把「静态解析结果 + 结果集」归约成形状描述符（契约 descriptor.fields 逐字）。"""
    scope = dict(static["scope"])
    for var, nt in (overrides or {}).items():
        scope[var] = nt
    used = set(c["var"] for c in static["cols"])
    missing = sorted(v for v in used if v not in scope)
    if missing:
        raise ParseError("untyped_row_var: %s（行作用域变量无载体标签/类，且未在 cases[*].var_type_overrides 声明）" % ",".join(missing))

    grain_scope = sorted(set(scope[v] for v in used))
    cols = sorted(set(scope[c["var"]] + "." + c["neutral"] for c in static["cols"]))
    bound = sorted(set(scope[c["var"]] + "." + c["neutral"] for c in static["cols"]
                       if any(r.get(c["alias"]) is not None for r in rows)))
    realized = []
    for v in sorted(used):
        vcols = [c for c in static["cols"] if c["var"] == v]
        if any(any(r.get(c["alias"]) is not None for r in rows) for c in vcols):
            realized.append(scope[v])
    return {
        "rows": len(rows),
        "rows_class": rows_class(len(rows)),
        "rows_band": rows_band(len(rows)),
        "grain_scope": grain_scope,
        "columns": cols,
        "bound_columns": bound,
        "realized_scope": sorted(set(realized)),
    }


# =============================================================================
# 3. 载体访问（只读）
# =============================================================================

class Blocked(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)
        self.msg = msg


class Neo4j(object):
    def __init__(self, targets, password):
        n = targets["neo4j"]
        self.url = n["http_uri"].rstrip("/") + "/db/neo4j/tx/commit"
        self.auth = "Basic " + base64.b64encode(("neo4j:" + password).encode("utf-8")).decode("ascii")

    def query(self, cypher, params=None, timeout=180):
        body = json.dumps({"statements": [{"statement": cypher, "parameters": params or {}}]}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("errors"):
            raise RuntimeError("Neo4j 报错: %s" % json.dumps(j["errors"], ensure_ascii=False)[:400])
        res = j["results"][0]
        cols = res["columns"]
        out = []
        for d in res["data"]:
            out.append(dict(zip(cols, d["row"])))
        return out


class GraphDB(object):
    def __init__(self, targets):
        g = targets["graphdb"]
        repo = urllib.parse.quote(g["isolation"]["repository"])
        self.url = g["url"].rstrip("/") + "/repositories/" + repo + "?infer=false"

    def select(self, sparql, timeout=180):
        data = urllib.parse.urlencode({"query": sparql}).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers={
            "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8"))
        out = []
        for b in j["results"]["bindings"]:
            out.append({k: v.get("value") for k, v in b.items()})
        return out


def build_prefix_block(mp):
    lines = []
    for p, iri in sorted(mp.prefixes.items()):
        lines.append("PREFIX %s: <%s>" % (p, iri))
    lines.append("PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>")
    return "\n".join(lines) + "\n"


# =============================================================================
# 4. 主流程
# =============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="G23 answer_shape 对齐门禁（只读）。PASS 只表示两款载体的回答结构一致，"
                    "不表示回答内容正确；③ 不计入成本，PASS 不能补偿 T10 的失败。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="诚实边界（与 shared/model/answer-shape.yaml #meta.honest_boundaries 逐字一致）：\n"
               "  B1 PASS 只表示两款载体的回答结构一致，不表示回答内容正确。\n"
               "  B2 ③ 属 ABox 一致性（docs/00-总体设计与裁决记录.md §6.3 第 343 行），不计入成本；PASS 不能补偿 T10 的失败。\n"
               "  B3 今天没有 50 条正式评测用例集（那是 M5，且受 docs/06-方案2设计（本体+SPARQL）.md §9 的 U-D 用例规模裁决阻塞）；\n"
               "     本契约只预留 extensions.fifty_case_slot，G23 不自己发明那 50 条。\n"
               "  B4 两侧载体型号不同（Neo4j 5.26.30 Community / GraphDB 10.8.0）：空结果 vs 无绑定、Neo4j 多重边 vs\n"
               "     RDF 三元组集合、TBox 个体 vs ABox 实例等形态差异必须逐条如实记录，不得在归一化里「抹平」。\n"
               "  B5 PASS 只表示「在契约声明面 declared_surface 上一致」：声明面是契约声明的信息粒度与列集合，\n"
               "     不是两款载体的全部原生信息 —— 两个原生差异（「类」的代表形式、边属性的承载位置）仍然存在，\n"
               "     门禁不据此宣称两载体等价，也不证明内容/语义等价；原生形态逐例打印，不抹平。\n"
               "契约：shared/model/answer-shape.yaml（形状口径，数据驱动）；\n"
               "      docs/00-总体设计与裁决记录.md §6.3（第 326-341 行）为契约原文，§6.3 第 343 行为分工补充。\n"
               "退出码 0=PASS 1=FAIL 2=BLOCKED/PARAM_ERROR。BLOCKED ≠ FAIL。\n"
               "边界：值等 → G22 shared/verify/abox_identity_gate.py；形等 → 本门禁。")
    ap.add_argument("--contract", default=DEFAULT_CONTRACT)
    ap.add_argument("--targets", default=DEFAULT_TARGETS)
    ap.add_argument("--snapshot", default=None, help="默认取 KG :CC_Meta.snapshotId")
    ap.add_argument("--onto-graph", default=None, help="默认取末段==snapshotId 的 ONTO 命名图")
    ap.add_argument("--min-cases", default="8")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    # ---- G23-03 前半：参数合法性（先于任何 I/O）----
    try:
        min_cases = int(args.min_cases)
    except (TypeError, ValueError):
        print("RESULT: PARAM_ERROR (--min-cases=%r 不是整数；合法值 = 正整数，默认 8)" % (args.min_cases,))
        return 2
    if min_cases <= 0:
        print("RESULT: PARAM_ERROR (--min-cases=%d <= 0；合法值 = 正整数，默认 8)" % min_cases)
        return 2

    print("=" * 78)
    print("G23 · answer_shape 对齐门禁（§6.3 ③ 的执行点）")
    print("=" * 78)
    print("诚实边界 B1: PASS 只表示两款载体的回答结构一致，不表示回答内容正确。")
    print("诚实边界 B2: ③ 属 ABox 一致性（docs/00:343），不计入成本；PASS 不能补偿 T10 的失败。")
    print("诚实边界 B3: 今天没有 50 条正式评测用例集（M5）；契约只预留扩展位。")
    print("诚实边界 B4: 两侧载体型号不同（Neo4j 5.26.30 / GraphDB 10.8.0），形态差异逐条如实记录，不抹平。")
    print("诚实边界 B5: PASS 只表示「在契约声明面 declared_surface 上一致」；不宣称两载体等价、不证明内容正确；原生形态逐例打印不抹平。")
    print("边界：值等 → G22 shared/verify/abox_identity_gate.py；形等 → 本门禁。")

    # ---- G23-00：契约与映射表 ----
    if not os.path.isfile(args.contract):
        return emit_blocked("契约文件不存在：%s" % args.contract, args, min_cases)
    raw = read_text(args.contract)
    if not raw.strip():
        return emit_blocked("契约文件为空：%s" % args.contract, args, min_cases)
    try:
        contract = yaml.safe_load(raw)
    except Exception as exc:
        return emit_blocked("契约文件不可解析（YAML 报错：%s）：%s" % (exc, args.contract), args, min_cases)
    if not isinstance(contract, dict) or not (contract.get("case_set") or {}).get("cases"):
        return emit_blocked("契约缺 case_set.cases（无任何用例条目）：%s" % args.contract, args, min_cases)
    if not os.path.isfile(args.targets):
        return emit_blocked("targets 文件不存在：%s" % args.targets, args, min_cases)

    srcdir = os.path.join(ROOT, "shared", "mapping")
    tm_path = os.path.join(ROOT, "shared", "mapping", "type-map.yaml")
    rm_path = os.path.join(ROOT, "shared", "mapping", "rel-map.yaml")
    for p in (tm_path, rm_path):
        if not os.path.isfile(p):
            return emit_blocked("映射表缺失：%s ⇒ 归一化无数据来源" % p, args, min_cases)
    del srcdir
    tm = read_yaml(tm_path)
    rm = read_yaml(rm_path)
    targets = read_yaml(args.targets)
    mp = Mapping(tm, rm, contract.get("neutral_extension") or {})
    if not mp.kg2neutral_type or not mp.onto2neutral_type or not mp.kg_rel or not mp.onto_rel:
        return emit_blocked("映射表归一化面为空（types/relations 未取到）⇒ 归一化不可用", args, min_cases)

    # ---- G23-00：凭据（先于任何网络）----
    pw = os.environ.get("CC_NEO4J_PASSWORD")
    if not pw:
        return emit_blocked(
            "环境变量 CC_NEO4J_PASSWORD 缺失（targets.neo4j.auth.password_env 要求）", args, min_cases)

    # ---- G23-00：可达性 ----
    tg = contract.get("sources") or {}
    del tg
    kg = Neo4j(targets, pw)
    try:
        metas = kg.query("MATCH (m:CC_Meta) RETURN m.snapshotId AS snapshotId")
    except Exception as exc:
        return emit_blocked("Neo4j 不可达或 :CC_Meta 不可读：%s" % exc, args, min_cases)
    if len(metas) != 1 or not metas[0].get("snapshotId"):
        return emit_blocked("KG 侧 :CC_Meta 不唯一或 snapshotId 为空（取到 %d 个）" % len(metas), args, min_cases)
    snapshot = args.snapshot or metas[0]["snapshotId"]

    gdb = GraphDB(targets)
    try:
        graphs = [b["g"] for b in gdb.select("SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }")]
    except Exception as exc:
        return emit_blocked("GraphDB 不可达或命名图列举失败：%s" % exc, args, min_cases)
    graph_template = targets["graphdb"]["isolation"]["named_graph_template"]
    want_iri = args.onto_graph or graph_template.format(snapshotId=snapshot)
    matched = None
    for g in graphs:
        if g.rstrip("/").rsplit("/", 1)[-1] == snapshot.rstrip("/").rsplit("/", 1)[-1]:
            matched = g
    if args.onto_graph:
        matched = args.onto_graph if args.onto_graph in graphs else None
    if matched is None:
        return emit_blocked(
            "ONTO 侧不存在 snapshotId=%s 的命名图" % snapshot, args, min_cases,
            snapshot=snapshot, graphs=graphs, want_iri=want_iri)

    tbox_iri = (contract.get("graphs") or {}).get("tbox")
    if tbox_iri and tbox_iri not in graphs:
        return emit_blocked("契约声明的 TBox 命名图不存在：<%s>" % tbox_iri, args, min_cases,
                            snapshot=snapshot, graphs=graphs, want_iri=want_iri)
    try:
        snap_triples = gdb.select("SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?a ?b ?c } }" % matched)[0]["n"]
        tbox_triples = (gdb.select("SELECT (COUNT(*) AS ?n) WHERE { GRAPH <%s> { ?a ?b ?c } }" % tbox_iri)[0]["n"]
                        if tbox_iri else None)
    except Exception as exc:
        return emit_blocked("ONTO 目标图三元组计数失败：%s" % exc, args, min_cases,
                            snapshot=snapshot, graphs=graphs, want_iri=want_iri)

    print("\n[G23-00] 前置条件")
    print("  KG 活体快照 :CC_Meta.snapshotId = %s" % snapshot)
    print("  ONTO 目标命名图 = <%s>  三元组数（?infer=false）= %s" % (matched, snap_triples))
    print("  ONTO TBox 命名图 = <%s>  三元组数（?infer=false）= %s" % (tbox_iri, tbox_triples))
    print("  ONTO 仓库现有命名图 %d 张" % len(graphs))

    prefix_block = build_prefix_block(mp)
    graph_of = {"snapshot": matched, "tbox": tbox_iri}

    # ---- G23-10（表级）：跨载体对照表逐条可审计（缺表/缺字段 ⇒ ERROR，跨载体声明无从核对）----
    cp_errs = check_carrier_positions(mp.ext)
    if cp_errs:
        print("\n[G23-10b] 跨载体对照表（neutral_extension.carrier_position.entries）")
    for code, msg in cp_errs:
        err(code, msg)

    # ---- G23-01：用例层 ----
    cases = contract["case_set"]["cases"]
    case_rows = []
    print("\n[G23-01] 用例层（Q01–Q08）")
    for cs in cases:
        cid = cs.get("id")
        which = (cs.get("graph") or "snapshot")
        g_iri = graph_of.get(which)
        if not g_iri:
            warn("G23-01", "%s: graph=%s 未解析到命名图 ⇒ unresolved" % (cid, which))
            case_rows.append({"id": cid, "title": cs.get("title"), "status": "unresolved",
                              "shape_kg": None, "shape_onto": None, "error": "no_graph"})
            continue
        overrides = cs.get("var_type_overrides") or {}
        try:
            st_kg = parse_cypher(cs["kg"], mp, overrides.get("kg") or {})
            st_onto = parse_sparql(cs["onto"], mp, overrides.get("onto") or {})
        except ParseError as exc:
            err("G23-06", "%s: 归一化 fail-closed：%s" % (cid, exc))
            case_rows.append({"id": cid, "title": cs.get("title"), "status": "unresolved",
                              "shape_kg": None, "shape_onto": None, "error": str(exc)})
            continue
        # ---- 声明面（G23-09 / G23-10）：③ 的比对面必须非空、逐条有据（静态，先于任何查询）----
        ds = cs.get("declared_surface")
        ds_errs = check_declared_surface(ds, mp.carrier_positions)
        for code, msg in ds_errs:
            err(code, "%s: %s" % (cid, msg))
        if ds_errs:
            case_rows.append({"id": cid, "title": cs.get("title"), "status": "unresolved",
                              "shape_kg": None, "shape_onto": None, "error": "declared_surface",
                              "declared_surface": declared_surface_summary(ds)})
            continue
        for side, q in (("KG", cs["kg"]), ("ONTO", cs["onto"])):
            bad = assert_readonly(q)
            if bad:
                err("G23-05", "%s/%s: 契约查询含写子句 %s ⇒ 拒绝执行" % (cid, side, bad))
                case_rows.append({"id": cid, "title": cs.get("title"), "status": "unresolved",
                                  "shape_kg": None, "shape_onto": None, "error": "write_clause"})
                break
        else:
            r_kg = run_side(lambda: kg.query(cs["kg"], cs.get("params")), "Neo4j")
            r_onto = run_side(lambda: gdb.select(prefix_block + cs["onto"]
                                                 .replace("{{GRAPH}}", matched).replace("{{TBOX}}", tbox_iri)),
                              "GraphDB")
            try:
                d_kg = reduce_descriptor(st_kg, r_kg["rows"], overrides.get("kg"), "kg") if r_kg["ok"] else None
                d_onto = reduce_descriptor(st_onto, r_onto["rows"], overrides.get("onto"), "onto") if r_onto["ok"] else None
            except ParseError as exc:
                err("G23-06", "%s: 归约 fail-closed：%s" % (cid, exc))
                d_kg = d_onto = None
            # ---- 声明面（G23-11）：声明列必须真实存在于该侧**原生**columns（反恒真闸门）----
            if d_kg is not None and d_onto is not None:
                nat_errs = check_declared_against_native(ds, d_kg.get("columns"), d_onto.get("columns"))
                for code, msg in nat_errs:
                    err(code, "%s: %s" % (cid, msg))
                if nat_errs:
                    bad = {"id": cid, "title": cs.get("title"), "status": "unresolved",
                           "shape_kg": None, "shape_onto": None, "error": "declared_surface_native",
                           "native_shape_kg": d_kg, "native_shape_onto": d_onto,
                           "declared_surface": declared_surface_summary(ds)}
                    case_rows.append(bad)
                    print_case(bad)
                    continue
            # ---- 投影到契约声明面，再用**同一个** compare_descriptors 比对（比对逻辑未改）----
            c_kg = project_canonical(d_kg, ds, "kg") if d_kg is not None else None
            c_onto = project_canonical(d_onto, ds, "onto") if d_onto is not None else None
            status, diffs = compare_descriptors(c_kg, c_onto)
            row = {"id": cid, "title": cs.get("title"), "status": status,
                   "shape_kg": c_kg, "shape_onto": c_onto,
                   "native_shape_kg": d_kg, "native_shape_onto": d_onto,
                   "declared_surface": declared_surface_summary(ds),
                   "diffs": diffs,
                   "errors": {"kg": r_kg["error"], "onto": r_onto["error"]}}
            case_rows.append(row)
            print_case(row)
            if status == "unresolved":
                msg = "%s: unresolved（kg=%s onto=%s）⇒ 禁止短路，绝不用空描述符代替" % (
                    cid, r_kg["error"] or "ok", r_onto["error"] or "ok")
                (err if args.strict else warn)("G23-07", msg)
            elif status == "DIFF":
                err("G23-04", "%s: DIFF %s" % (cid, json.dumps(diffs, ensure_ascii=False)))
            if (c_kg and c_onto and c_kg["rows"] != c_onto["rows"]
                    and c_kg["rows_class"] == c_onto["rows_class"]):
                warn("G23-04", "%s: 行数不等但同档（kg=%d onto=%d，rows_class=%s；rows_band %s vs %s）"
                     " ⇒ 按契约只比分档判 SAME；精确计数归 G14，差异如实记录"
                     % (cid, c_kg["rows"], c_onto["rows"], c_kg["rows_class"],
                        c_kg["rows_band"], c_onto["rows_band"]))

    # ---- G23-02：关系粒度层 ----
    rels = [(k, v) for k, v in (rm.get("relations") or {}).items()
            if isinstance(v, dict) and v.get("asserted") is True]
    print("\n[G23-02] 关系粒度层（rel-map asserted: true，共 %d 条；只比形状，值等归 G22）" % len(rels))
    idk = ((tm.get("common_properties") or {}).get("kg") or {})
    ido = ((tm.get("common_properties") or {}).get("onto") or {})
    kg_id = idk.get("instance_id", (tm.get("namespaces") or {}).get("kg", {}).get("instance_id_property"))
    kg_layer = idk.get("layer")
    onto_id = mp.expand(ido.get("instance_id")) if ido.get("instance_id") else None
    onto_layer = mp.expand(ido.get("layer")) if ido.get("layer") else None
    rel_rows = []
    for code, spec in sorted(rels):
        nf, nt = spec.get("from"), spec.get("to")
        if nf == "*" or nt == "*":
            warn("G23-02", "%s: from/to 含通配符（%s→%s），契约 star_type_note 排除 ⇒ 跳过" % (code, nf, nt))
            continue
        ft = (tm.get("types") or {}).get(nf) or {}
        tt = (tm.get("types") or {}).get(nt) or {}
        kg_flab, kg_tlab = ft.get("kg_label"), tt.get("kg_label")
        onto_fcls, onto_tcls = ft.get("onto_class"), tt.get("onto_class")
        if not kg_flab or not kg_tlab or not onto_fcls or not onto_tcls:
            err("G23-06", "关系 %s: 端点中性类型 %s→%s 不能在 type-map#types 里解析出 kg_label/onto_class"
                "（kg: %s→%s；onto: %s→%s）⇒ fail-closed" % (code, nf, nt, kg_flab, kg_tlab, onto_fcls, onto_tcls))
            rel_rows.append({"code": code, "status": "unresolved", "shape_kg": None, "shape_onto": None,
                             "diffs": [], "error": "unmappable_endpoint_type", "kg_from_to": [nf, nt]})
            continue
        if not spec.get("kg_reltype") or not spec.get("onto_property"):
            err("G23-06", "关系 %s: rel-map 缺 kg_reltype/onto_property ⇒ fail-closed" % code)
            rel_rows.append({"code": code, "status": "unresolved", "shape_kg": None, "shape_onto": None,
                             "diffs": [], "error": "unmappable_reltype", "kg_from_to": [nf, nt]})
            continue
        rel_rows.append(run_relation(code, spec, mp, kg, gdb, matched, prefix_block,
                                     kg_id, kg_layer, onto_id, onto_layer, args,
                                     kg_flab, kg_tlab, mp.expand(onto_fcls), mp.expand(onto_tcls)))

    # ---- 汇总 ----
    total = len(case_rows)
    same = len([c for c in case_rows if c["status"] == "SAME"])
    diff = len([c for c in case_rows if c["status"] == "DIFF"])
    unres = len([c for c in case_rows if c["status"] == "unresolved"])
    msgs = check_min_cases(total, min_cases)
    if msgs:
        err("G23-03", msgs)
    statuses = []
    for c in case_rows:
        a = (c.get("shape_kg") or {}).get("rows_class") or "?unresolved"
        b = (c.get("shape_onto") or {}).get("rows_class") or "?unresolved"
        statuses.append((c["id"], a, b))
    sev, msg = check_nonzero_lower_bound(statuses, args.strict)
    if sev == "ERROR":
        err("G23-03", msg)
    elif sev == "WARN":
        warn("G23-03", msg)
    else:
        note("G23-03", "非 zero 用例数满足下限（>=3）")

    rsame = len([r for r in rel_rows if r["status"] == "SAME"])
    rdiff = len([r for r in rel_rows if r["status"] == "DIFF"])
    runres = len([r for r in rel_rows if r["status"] == "unresolved"])
    for r in rel_rows:
        if r["status"] == "DIFF":
            err("G23-02", "关系 %s: DIFF %s" % (r["code"], json.dumps(r["diffs"], ensure_ascii=False)))
        elif r["status"] == "unresolved":
            (err if args.strict else warn)("G23-07", "关系 %s: unresolved（%s）" % (r["code"], r["error"]))

    nE, nW = n_err(), n_warn()
    fail = bool(nE) or diff > 0
    result = "FAIL" if fail else "PASS"
    print("\n[G23-04] 汇总")
    print("  用例: cases_total=%d same=%d diff=%d unresolved=%d (--min-cases=%d)"
          % (total, same, diff, unres, min_cases))
    print("  关系: relations_total=%d same=%d diff=%d unresolved=%d"
          % (len(rel_rows), rsame, rdiff, runres))
    for c in case_rows:
        print("    %-4s %-11s rows_kg=%-6s rows_onto=%-6s" % (
            c["id"], c["status"],
            (c.get("shape_kg") or {}).get("rows", "-"),
            (c.get("shape_onto") or {}).get("rows", "-")))
    for r in rel_rows:
        print("    %-24s %-11s rows_kg=%-6s rows_onto=%-6s" % (
            r["code"], r["status"],
            (r.get("shape_kg") or {}).get("rows", "-"),
            (r.get("shape_onto") or {}).get("rows", "-")))

    payload = {
        "gate": "G23",
        "snapshotId": snapshot,
        "ontoSnapshotGraph": matched,
        "ontoSnapshotGraphTriples": int(snap_triples),
        "ontoTboxGraph": tbox_iri,
        "ontoTboxGraphTriples": int(tbox_triples) if tbox_triples else None,
        "ontoNamedGraphs": sorted(graphs),
        "contract": os.path.relpath(args.contract, ROOT).replace("\\", "/"),
        "strict": bool(args.strict),
        "minCases": min_cases,
        "cases": case_rows,
        "relations": rel_rows,
        "cases_total": total, "same": same, "diff": diff, "unresolved": unres,
        "relations_total": len(rel_rows), "relations_same": rsame,
        "relations_diff": rdiff, "relations_unresolved": runres,
        "answerShapeChecked": True,
        "preconditionBlocked": False,
        "errorCount": nE, "warnCount": nW,
        "result": result,
        "findings": [{"severity": s, "code": c, "message": m} for s, c, m in F],
    }
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n  --json 明细已写：%s" % args.out)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\nRESULT: %s  (cases_total=%d same=%d diff=%d unresolved=%d; relations_total=%d same=%d diff=%d unresolved=%d; ERROR=%d WARN=%d)"
          % (result, total, same, diff, unres, len(rel_rows), rsame, rdiff, runres, nE, nW))
    return 1 if fail else 0


def run_side(fn, tag):
    try:
        return {"ok": True, "rows": fn(), "error": None}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "rows": [], "error": "%s HTTP %s" % (tag, exc.code)}
    except Exception as exc:
        return {"ok": False, "rows": [], "error": "%s %s: %s" % (tag, type(exc).__name__, exc)}


def print_case(row):
    k, o = row.get("shape_kg"), row.get("shape_onto")
    print("  %-4s %-11s" % (row["id"], row["status"]))
    dss = row.get("declared_surface")
    if dss:
        print("       声明面: columns=%d carrier_specific_columns=%d"
              % (dss.get("columns_count", 0), dss.get("carrier_specific_columns_count", 0)))
        for e in dss.get("carrier_specific_columns") or []:
            print("               carrier_specific[side=%s] %s ⇒ %s"
                  % (e.get("side"), e.get("token"), e.get("reason")))
    for tag, d in (("kg  ", k), ("onto", o)):
        if d is None:
            print("       %s : <unresolved>" % tag)
            continue
        print("       %s : rows=%d rows_class=%s grain_scope=%s" % (
            tag, d["rows"], d["rows_class"], d["grain_scope"]))
        print("              columns=%s" % d["columns"])
        print("              bound_columns=%s realized_scope=%s" % (d["bound_columns"], d["realized_scope"]))
    if row.get("diffs"):
        print("       diffs=%s" % json.dumps(row["diffs"], ensure_ascii=False))
    nk, no = row.get("native_shape_kg"), row.get("native_shape_onto")
    if nk is not None or no is not None:
        print("       原生（未投影；差异如实登记、不抹平）: kg columns=%d grain_scope=%s / "
              "onto columns=%d grain_scope=%s"
              % (len((nk or {}).get("columns") or []), (nk or {}).get("grain_scope"),
                 len((no or {}).get("columns") or []), (no or {}).get("grain_scope")))


def run_relation(code, spec, mp, kg, gdb, matched, prefix_block,
                 kg_id, kg_layer, onto_id, onto_layer, args,
                 kg_flab, kg_tlab, onto_fcls, onto_tcls):
    nf, nt = spec.get("from"), spec.get("to")
    kq = ("MATCH (a:%s)-[:%s]->(b:%s) RETURN a.%s AS fromId, a.%s AS fromLayer, "
          "b.%s AS toId, b.%s AS toLayer"
          % (kg_flab, spec["kg_reltype"], kg_tlab, kg_id, kg_layer, kg_id, kg_layer))
    oq = ("SELECT ?fromId ?fromLayer ?toId ?toLayer WHERE { GRAPH <{{GRAPH}}> { "
          "?a <%s> ?b . OPTIONAL { ?a <%s> ?fromId } OPTIONAL { ?a <%s> ?fromLayer } "
          "OPTIONAL { ?b <%s> ?toId } OPTIONAL { ?b <%s> ?toLayer } } }"
          % (mp.expand(spec["onto_property"]), onto_id, onto_layer, onto_id, onto_layer))
    oq = prefix_block + oq.replace("{{GRAPH}}", matched)
    r_kg = run_side(lambda: kg.query(kq), "Neo4j")
    r_onto = run_side(lambda: gdb.select(oq), "GraphDB")
    # 静态描述符：两侧同构 —— rel-map 声明了端点中性类型，故 scope 取自 rel-map 的 from/to，
    # 列 = 两端点的 instance_id / layer。⇒ 本层的判别力在 rows_class / rows / bound_columns /
    # realized_scope（数据相关），静态 columns 天然对称。报告里如实说明这一点。
    static = {"scope": {"a": nf, "b": nt},
              "cols": [{"alias": "fromId", "neutral": "instance_id", "var": "a"},
                       {"alias": "fromLayer", "neutral": "layer", "var": "a"},
                       {"alias": "toId", "neutral": "instance_id", "var": "b"},
                       {"alias": "toLayer", "neutral": "layer", "var": "b"}]}
    try:
        d_kg = reduce_descriptor(static, r_kg["rows"], None, "kg") if r_kg["ok"] else None
        d_onto = reduce_descriptor(static, r_onto["rows"], None, "onto") if r_onto["ok"] else None
    except ParseError as exc:
        err("G23-06", "关系 %s: 归约 fail-closed：%s" % (code, exc))
        d_kg = d_onto = None
    status, diffs = compare_descriptors(d_kg, d_onto)
    if (d_kg is not None and d_onto is not None and d_kg["rows"] != d_onto["rows"]
            and d_kg["rows_class"] == d_onto["rows_class"]):
        warn("G23-02", "关系 %s: 条数不等但同档（kg=%d onto=%d，两侧 %s→%s，rows_class=%s）"
             " ⇒ 按契约只比分档判 SAME；精确计数归 G14，差异如实记录"
             % (code, d_kg["rows"], d_onto["rows"], kg_flab, kg_tlab, d_kg["rows_class"]))
    return {"code": code, "status": status, "shape_kg": d_kg, "shape_onto": d_onto, "diffs": diffs,
            "rows_kg": (d_kg or {}).get("rows"), "rows_onto": (d_onto or {}).get("rows"),
            "error": (r_kg["error"] or "") + (r_onto["error"] or ""),
            "kg_from_to": [nf, nt],
            "kg_labels": [kg_flab, kg_tlab],
            "onto_classes": [onto_fcls, onto_tcls],
            "kg_reltype": spec["kg_reltype"], "onto_property": spec["onto_property"]}


def emit_blocked(msg, args, min_cases, snapshot=None, graphs=None, want_iri=None):
    """G23-00 前置条件不满足 ⇒ BLOCKED，exit 2。措辞照抄 shared/verify/parity.py 的 M2.18 先例。"""
    print("\n★ 前置条件不满足：%s ⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2" % msg)
    if want_iri is not None:
        print("  期望命名图 IRI：<%s>" % want_iri)
    if graphs:
        print("  ONTO 仓库现有命名图（按 IRI 排序；末段即各快照的 snapshotId）：")
        for g in sorted(graphs):
            print("    - <%s>   末段=%s" % (g, g.rstrip("/").rsplit("/", 1)[-1]))
    print("★ 未执行：§6.3 ③ answer_shape 两侧比对（Q01–Q08 用例层 + rel-map asserted 关系粒度层）。")
    print("  原先此处会打印逐行 DIFF（ONTO 侧全空）——那是「前置条件缺失」造成的**假分歧**，")
    print("  不是「两侧形状不符」，故不再打印、也不再据此判 FAIL。")
    print("★ BLOCKED ≠ FAIL：")
    print("  FAIL    = 两侧数据结构**真实分歧**（建模/投影缺陷）⇒ 拒绝采信评测结果，去查投影器/映射表/契约。")
    print("  BLOCKED = **缺前置条件**（契约/凭据/载体/命名图）⇒ 本次自检本身不可采信；")
    print("            处置：先补前置条件（或改用已投影的 CCF），再重跑本门禁。")
    print("  诚实边界：BLOCKED 绝不等于 PASS，本门禁在缺凭据时**绝不**打印 PASS（fail-closed）。")
    payload = {
        "gate": "G23", "snapshotId": snapshot, "contract": os.path.relpath(args.contract, ROOT).replace("\\", "/"),
        "minCases": min_cases, "strict": bool(args.strict),
        "cases": [], "relations": [],
        "cases_total": 0, "same": 0, "diff": 0, "unresolved": 0,
        "relations_total": 0, "relations_same": 0, "relations_diff": 0, "relations_unresolved": 0,
        "answerShapeChecked": False, "preconditionBlocked": True,
        "ontoSnapshotGraphPresent": False, "ontoNamedGraphs": sorted(graphs or []),
        "errorCount": 0, "warnCount": 0,
        "result": "BLOCKED", "blockedReason": msg,
    }
    if args.out:
        with io.open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\nRESULT: BLOCKED（前置条件不满足 ⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
