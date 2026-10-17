#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
G24 —— Set B 工具面「实现级」一致性门禁（硬约束 C3 / 评测要件 P7；横幅里的工具数从契约 tools 条数派生，不写死）

契约：shared/model/tools-setb.yaml
门禁：shared/verify/toolset_parity_gate.py（本文件，只读、纯标准库、退出码 0/1/2）

================================================================================
为什么需要本门禁
================================================================================
硬约束 C3（docs/00-总体设计与裁决记录.md §3.1，★ v1.4 重述）只严格要求**同一对照对
内部**的工具面（工具名 / 入参 / 出参 schema）逐字一致；跨组只要求「信息可及性对齐」。
此前 C3/P7 只有**契约级**检查（门禁 G8 读 shared/model/info-products.yaml 比对工具
**名列表**）；两侧服务各写一套实现时，「同一个工具名之下参数名差一个字符 / 必填性不同 /
返回字段名不同」不会有任何门禁变红。本门禁补这一段，并且：

★ 数据源必须是**从实现生成的 manifest**（两侧服务以 --emit-manifest 产出），且 manifest
  必须带 source + sourceSha256 ⇒ 本门禁校验「清单与实现同源」。手写清单可被伪造成
  「两侧一致」，一律不采信。
★ 本门禁**不产出** manifest（门禁自产清单等于自证）。

================================================================================
三态语义（★ 不得混读）
================================================================================
  0 = PASS     两侧**声明的**工具面逐字一致（且契约自检 + 三源交叉一致通过）。
                ★ 只证「声明的」工具面一致，**不证**行为/语义一致（行为一致性属 M4/M5）。
  1 = FAIL     两侧**声明的**工具面**真实分歧**，或清单与实现不同源，或契约自检/三源交叉失败。
  2 = BLOCKED  前置条件不满足（契约 / 某一侧 manifest / manifest 声明的实现文件缺失）
                ⇒ 本门禁未执行；既非 PASS 亦非 FAIL。
  2 = PARAM_ERROR  命令行参数非法（如 --min-tools 非 ≥1 整数、两侧 manifest 同一路径）。

★ BLOCKED ≠ FAIL：FAIL 是「两侧真的不一致」，BLOCKED 是「本次比对本身不可采信」。
  缺输入时**绝不打印 PASS**（fail-closed）。

================================================================================
诚实边界（★ 逐条读）
================================================================================
  HB1  只证明两侧**声明的**工具面一致；不证明行为/语义一致。
  HB2  sourceSha256 只证明「清单出自该实现文件的当前版本」；**不证明**该文件真的实现了
       清单内容（需 M3.2/M3.3 自证，另议）。
  HB3  契约里 carrier_specific / c3_participation: excluded 的项越多，C3 可比性越弱 ——
       本门禁每次打印 compared / excluded 计数（排除必须由**契约标志**驱动，不得按侧写白名单）。
  HB4  只比对**顶层**返回字段名 + 参数 (name, type, required)；数组项内部字段名不比对。
  HB5  G24-10（三源交叉一致）是**今天就有真机判别力**的判据：它比对
       shared/model/tools-setb.yaml ↔ shared/model/info-products.yaml#access.<arm>.tools ↔
       shared/model/kgt-tools.yaml §三 禁令，不依赖任何 manifest。
  HB6  Set B 的工具在本仓库**没有任何实现**（两侧服务属后续 M3.2/M3.3）⇒ 真机运行
       本门禁的预期结果是 **BLOCKED(exit 2)**，缺的是「两侧 manifest / 实现源文件」。
       ★ 判别力由 --selftest（离线断言）与 tmp/m3_1_counterexample.py（合成夹具）证明。
  HB7  ★ S3a：契约里带 neutral_tool_ref 的条目是**引用形态**（D47 单一定义源），**不是**
       「零参数/零返回工具」：比对面按 shared/model/<文件名>#<顶层键> 展开 params 与
       returns.tool_level（别名只换名，形状同源；描述仍由登记条目自持）。被引用形状的
       returns.envelope 键名必须与本契约公共信封（return_envelope 里 c3_participation
       != excluded 的字段名）**集合相等**，否则 BLOCKED(exit 2) —— 该断言正是
       「中性取数服务的线上信封键名与公共信封不一致」这类缺陷的机械检出点。
       ★ G24-11 的计数口径仍是契约**登记**面（引用条目按登记的 params 字面计数），展开只
       作用于比对面 ⇒ 静态声明与重算值同源可比，冻结的 boundary_counts 不被引用展开改写。

================================================================================
用法
================================================================================
  # 契约自检 + 三源交叉一致（不需要 manifest；今天能真跑）
  python shared/verify/toolset_parity_gate.py --contract-only

  # 真机比对（M3.2/M3.3 交付后）
  python shared/verify/toolset_parity_gate.py \
      --kg-manifest   tmp/toolface-kg.json \
      --onto-manifest tmp/toolface-onto.json \
      --repo-root     .

  # 离线自证（≥6 条断言，含 3 条真能失败的负控）
  python shared/verify/toolset_parity_gate.py --selftest

  # 机器可读输出（人类可读报告 + '--- JSON ---' 分隔行 + JSON）
  python shared/verify/toolset_parity_gate.py --contract-only --json

  # ★ 引用形态（D47 单一定义源）：契约条目里若只登记 neutral_tool_ref: "<文件名>#<顶层键>"，
  #   该工具的 params / returns 由 shared/model/<文件名> 的形状权威展开后再比对 ——
  #   写「params: []」是**引用形态**，不是在声明「零参数工具」。

★ 输入溯源显式打印：本门禁运行时**总是**先打印「契约路径 + 两份 manifest 路径 + repo-root
  + --min-tools 取值 + 每个 source 的实际/声明 sha256」，以消除「省略参数仍 BLOCKED 而
  计数被误读」的歧义（先例：shared/verify/t10_native_parity.py:1026 与 :356 的 argparse
  默认值 None 陷阱）。
================================================================================
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT_DEFAULT = os.path.dirname(os.path.dirname(HERE))          # shared/verify -> 仓库根
CONTRACT_DEFAULT = os.path.join("shared", "model", "tools-setb.yaml")
INFO_PRODUCTS_DEFAULT = os.path.join("shared", "model", "info-products.yaml")
KGT_TOOLS_DEFAULT = os.path.join("shared", "model", "kgt-tools.yaml")

# 禁用词正则（★ 逐字取自 docs/05-编排层设计（Dify）.md §5.2 的 BANNED；只用于**契约自检**
# 的 description 字段，不对本文件其它文本生效）
BANNED_DESC = re.compile(
    r"(cypher|sparql|rdf|owl|三元组|属性图|property\s*graph|知识图谱|本体|neo4j|graphdb"
    r"|turtle|ttl|shacl|推理机|reasoner)",
    re.I,
)

SETB_ARMS = ["KG-Q", "ONTO-Q", "KG-R", "ONTO-R"]
SIDES = ("kg", "onto")

# ★ S3a：D47 单一定义源 —— 契约条目里只登记「文件名#顶层键」，形状取自 shared/model/。
REF_KEY = "neutral_tool_ref"
MODEL_DIR_REL = os.path.join("shared", "model")
REF_BODY_KEYS = ("params", "returns")


class Blocked(Exception):
    """前置条件不满足 ⇒ exit 2，且**不**打印 PASS/FAIL。"""


class ParamError(Exception):
    """命令行参数非法 ⇒ exit 2。"""


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8-sig")


def load_yaml(path):
    try:
        import yaml  # noqa: F401
    except Exception as exc:                                    # pragma: no cover
        raise Blocked("本机缺 PyYAML（import yaml 失败：%s）⇒ 无法读契约" % exc)
    import yaml

    with open(path, "r", encoding="utf-8-sig") as fh:
        return yaml.safe_load(fh)


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def err(code, detail, **extra):
    d = {"code": code, "detail": detail}
    d.update(extra)
    return d


def warn(code, detail, **extra):
    d = {"code": code, "detail": detail}
    d.update(extra)
    return d


def norm_triples(params):
    """参数 → 排序后的 (name, type, required) 三元组列表。"""
    out = []
    for p in params or []:
        if not isinstance(p, dict):
            raise Blocked("manifest/契约的参数项不是对象：%r" % (p,))
        out.append(
            (
                str(p.get("name", "")),
                str(p.get("type", "")),
                bool(p.get("required", False)),
            )
        )
    return sorted(out)


def norm_return_names(returns):
    """返回项 → 字段名列表（支持字符串项与 {name,type} 对象项）。"""
    names = []
    for r in returns or []:
        if isinstance(r, str):
            names.append(r)
        elif isinstance(r, dict):
            names.append(str(r.get("name", "")))
        else:
            raise Blocked("manifest/契约的返回项既不是字符串也不是对象：%r" % (r,))
    return names


def norm_return_types(returns):
    out = {}
    for r in returns or []:
        if isinstance(r, dict) and r.get("type"):
            out[str(r.get("name", ""))] = str(r["type"])
    return out


def resolve(repo_root, rel):
    return rel if os.path.isabs(rel) else os.path.join(repo_root, rel)


# ---------------------------------------------------------------------------
# 引用条目展开（★ S3a：D47 单一定义源；见 HB7）
# ---------------------------------------------------------------------------
def _ref_shape(ref, repo_root):
    """解析 `<文件名>#<顶层键>` ⇒ 被引用形状；任何不可解析一律 Blocked（fail-closed）。"""
    if not isinstance(ref, str) or "#" not in ref:
        raise Blocked("引用格式非法（应为 <文件名>#<顶层键>）：%r" % (ref,))
    fname, key = ref.split("#", 1)
    if not fname.endswith((".yaml", ".yml")) or ("/" in fname) or ("\\" in fname):
        raise Blocked("引用格式非法（文件名必须是同目录 .yaml/.yml，且不含路径分隔符）：%r" % (ref,))
    path = os.path.join(repo_root or REPO_ROOT_DEFAULT, MODEL_DIR_REL, fname)
    if not os.path.isfile(path):
        raise Blocked("引用文件不存在：%s（引用 %s）" % (path, ref))
    src = load_yaml(path)
    if not isinstance(src, dict) or key not in src:
        raise Blocked("引用键不存在：%s 里没有顶层键 %r（引用 %s）" % (path, key, ref))
    shape = src[key]
    if not isinstance(shape, dict):
        raise Blocked("被引用形状不是对象：%s#%s" % (fname, key))
    for k in REF_BODY_KEYS:
        if k not in shape:
            raise Blocked("被引用形状缺必需字段 %s：%s#%s" % (k, fname, key))
    return shape


def _ref_params(shape, ref):
    """被引用形状的 params → 契约条目形态（mapping(name→定义)+顶层 required，或 list 形态）。"""
    raw = shape.get("params")
    out = []
    if isinstance(raw, dict):
        req = shape.get("required")
        if req is None:
            req = []
        if not isinstance(req, list):
            raise Blocked("被引用形状的顶层 required 必须是列表：%s（拿到 %s）"
                          % (ref, type(req).__name__))
        reqset = set(str(x) for x in req)
        for name, d in raw.items():
            if not isinstance(d, dict):
                raise Blocked("被引用形状的参数定义不是对象：%s（参数 %r）" % (ref, name))
            p = {"name": str(name), "type": str(d.get("type", "")), "required": str(name) in reqset}
            for k in ("c3_participation", "excluded_reason"):
                if d.get(k) is not None:
                    p[k] = d[k]
            out.append(p)
        return out
    if isinstance(raw, list):
        for p in raw:
            if not isinstance(p, dict):
                raise Blocked("被引用形状的参数项不是对象：%s" % ref)
            q = dict(p)
            q["name"] = str(p.get("name", ""))
            q["type"] = str(p.get("type", ""))
            q["required"] = bool(p.get("required", False))
            out.append(q)
        return out
    raise Blocked("被引用形状的 params 既不是 mapping 也不是 list：%s" % ref)


def _ref_returns(shape, ref, env_cmp):
    """被引用形状的 returns → 契约条目形态；★ 信封键名集合必须与公共信封**集合相等**。"""
    ret = shape.get("returns")
    if not isinstance(ret, dict):
        raise Blocked("被引用形状的 returns 不是对象：%s" % ref)
    tl = ret.get("tool_level")
    if not isinstance(tl, dict):
        raise Blocked("被引用形状缺 returns.tool_level（mapping）：%s" % ref)
    env = ret.get("envelope")
    if not isinstance(env, dict):
        raise Blocked("被引用形状缺 returns.envelope（mapping）：%s" % ref)
    got = set(str(k) for k in env.keys())
    want = set(env_cmp)
    if got != want:
        raise Blocked("被引用形状的信封键名与本契约公共信封**不等**（G24-05 会把信封并入每个工具的"
                      "比对面 ⇒ 任一侧都**不可能**诚实声明）：引用 %s 的信封键 = %s；"
                      "契约 return_envelope 里 c3_participation != excluded 的字段 = %s"
                      % (ref, sorted(got), sorted(want)))
    out = []
    for name, d in tl.items():
        r = {"name": str(name), "c3_participation": "compared"}
        if isinstance(d, dict):
            for k in ("c3_participation", "excluded_reason"):
                if d.get(k) is not None:
                    r[k] = d[k]
        out.append(r)
    return out


def expand_refs(tools, repo_root, env_cmp):
    """把带 REF_KEY 的登记条目展开为可比对的面；只替换 params/returns，绝不改写契约对象。"""
    out = {}
    for tname, spec in tools.items():
        if not isinstance(spec, dict):
            raise Blocked("契约 tools.%s 不是对象" % tname)
        ref = spec.get(REF_KEY)
        if ref is None:
            out[tname] = spec
            continue
        for k in REF_BODY_KEYS:
            if spec.get(k):
                raise Blocked("契约 tools.%s 既带 %s 又带非空 %s ⇒ 引用与正文冲突（一半引用一半"
                              "正文必须硬失败，与 register_tools 的 ERR_REF_CONFLICT 同规则）"
                              % (tname, REF_KEY, k))
        shape = _ref_shape(ref, repo_root)
        merged = dict(spec)
        merged["params"] = _ref_params(shape, ref)
        merged["returns"] = _ref_returns(shape, ref, env_cmp)
        out[tname] = merged
    return out


# ---------------------------------------------------------------------------
# 面（face）抽取
# ---------------------------------------------------------------------------
def contract_face(contract, repo_root=None):
    """从契约抽取「参与比对」的面；排除项由契约标志 c3_participation: excluded 驱动。

    ★ S3a：带 neutral_tool_ref 的条目是**引用形态**（不是「零参数工具」）——先按形状权威
      （shared/model/<文件名>#<顶层键>）把 params / returns 两面展开，再照原逻辑抽取面。
      description 仍由登记条目自持（两种说法的漂移由 register_tools 的
      ERR_REF_DESCRIPTION_DRIFT 负责，不在本门禁职责内）。
    """
    env = ((contract.get("return_envelope") or {}).get("fields")) or []
    env_cmp = [f["name"] for f in env if f.get("c3_participation", "compared") != "excluded"]
    env_exc = [f["name"] for f in env if f.get("c3_participation", "compared") == "excluded"]
    tools = expand_refs(contract.get("tools") or {}, repo_root, env_cmp)

    face = {"tools": list(tools.keys()), "params": {}, "excluded_params": {},
            "returns": {}, "excluded_returns": {}, "envelope": env_cmp}
    for tname, spec in tools.items():
        pl, pex = [], []
        for p in spec.get("params") or []:
            if p.get("c3_participation", "compared") == "excluded":
                pex.append(p.get("name"))
            else:
                pl.append(p)
        face["params"][tname] = norm_triples(pl)
        face["excluded_params"][tname] = pex
        if face["excluded_params"][tname] and not all(
            (p.get("excluded_reason") or "").strip()
            for p in (spec.get("params") or [])
            if p.get("c3_participation") == "excluded"
        ):
            raise Blocked("契约 tools.%s 的排除参数缺 excluded_reason（排除必须可审）" % tname)
        rl, rex = [], []
        for r in spec.get("returns") or []:
            if r.get("c3_participation", "compared") == "excluded":
                rex.append(r.get("name"))
            else:
                rl.append(r.get("name"))
        if rex and not all(
            (r.get("excluded_reason") or "").strip()
            for r in (spec.get("returns") or [])
            if r.get("c3_participation") == "excluded"
        ):
            raise Blocked("契约 tools.%s 的排除返回项缺 excluded_reason" % tname)
        face["returns"][tname] = sorted(set(rl) | set(env_cmp))
        face["excluded_returns"][tname] = rex + env_exc
    return face


def manifest_face(manifest, side):
    """从 manifest 抽取该侧声明的面。"""
    if not isinstance(manifest, dict):
        raise Blocked("%s manifest 的顶层不是对象" % side)
    carrier = manifest.get("carrier")
    if carrier is not None and str(carrier) != side:
        raise ValueError(
            "%s manifest 自称 carrier=%r，却作为 --%s-manifest 传入（槽位与载体不符）"
            % (side, carrier, side)
        )
    tools = manifest.get("tools")
    if tools is None:
        raise Blocked("%s manifest 缺 tools[]（关键字段）" % side)
    if not isinstance(tools, list):
        raise Blocked("%s manifest 的 tools 不是列表" % side)
    face = {"tools": [], "params": {}, "returns": {}, "types": {},
            "descriptions": {}, "arm": manifest.get("arm"), "carrier": carrier}
    for t in tools:
        if not isinstance(t, dict) or not t.get("name"):
            raise Blocked("%s manifest 的 tools[] 中存在缺 name 的项" % side)
        name = str(t["name"])
        face["tools"].append(name)
        face["params"][name] = norm_triples(t.get("params") or [])
        face["returns"][name] = sorted(set(norm_return_names(t.get("returns") or [])))
        face["types"][name] = norm_return_types(t.get("returns") or [])
        if t.get("description") is not None:
            face["descriptions"][name] = str(t["description"])
    return face


# ---------------------------------------------------------------------------
# 溯源校验（G24-02）
# ---------------------------------------------------------------------------
def collect_sources(manifest):
    """收集 manifest 声明的 (source, sha256) 对（三种形态都支持）。"""
    pairs = []
    if manifest.get("source"):
        pairs.append((manifest.get("source"), manifest.get("sourceSha256")))
    for s in manifest.get("sources") or []:
        if isinstance(s, dict):
            pairs.append((s.get("path") or s.get("source"), s.get("sha256") or s.get("sourceSha256")))
    for t in manifest.get("tools") or []:
        if isinstance(t, dict) and t.get("source"):
            pairs.append((t.get("source"), t.get("sourceSha256")))
    return pairs


def verify_sources(manifest, side, repo_root, prov):
    """返回 (errors, provenance_rows)。实现文件缺失 ⇒ Blocked；sha 不一致 ⇒ ERROR。"""
    pairs = collect_sources(manifest)
    if not pairs:
        raise Blocked(
            "%s manifest 未声明任何 (source, sourceSha256) ⇒ 清单无来源 = 无法验证同源"
            % side
        )
    errors = []
    for rel, claimed in pairs:
        if not claimed:
            errors.append(err("G24-02",
                              "%s manifest 的 source %r 缺 sourceSha256" % (side, rel)))
            prov.append((side, rel, "(缺声明)", "(缺声明)", "NO_SHA"))
            continue
        path = resolve(repo_root, str(rel))
        if not os.path.isfile(path):
            raise Blocked(
                "%s manifest 声明的实现文件不存在：%s（repo-root 下解析为 %s）" % (side, rel, path)
            )
        actual = sha256_file(path)
        ok = actual.lower() == str(claimed).lower()
        prov.append((side, str(rel), actual, str(claimed), "OK" if ok else "MISMATCH"))
        if not ok:
            errors.append(err(
                "G24-02",
                "%s manifest 与实现**不同源**（清单可伪造）：%s 实际 sha256=%s ≠ 声明 sha256=%s"
                % (side, rel, actual, claimed),
            ))
    return errors, prov


# ---------------------------------------------------------------------------
# 比对（G24-03 / 04 / 05 / 06 / 07）
# ---------------------------------------------------------------------------
def compare_faces(cface, mkg, monto, min_tools):
    errors, warns, info = [], [], {}
    cset = list(cface["tools"])

    for side, mf in (("kg", mkg), ("onto", monto)):
        got = list(mf["tools"])
        missing = [t for t in cset if t not in got]
        extra = [t for t in got if t not in cset]
        if missing:
            errors.append(err("G24-03",
                              "%s 侧缺工具：%s（契约声明 %d 个）" % (side, missing, len(cset)),
                              side=side, missing=missing))
        if extra:
            errors.append(err("G24-03",
                              "%s 侧多出契约外工具：%s" % (side, extra), side=side, extra=extra))
        if not got:
            errors.append(err("G24-03", "%s 侧工具集为**空**（空集不得因『两侧都空』而通过）" % side,
                              side=side))

    for t in cset:
        tkg, tonto = mkg["params"].get(t), monto["params"].get(t)
        if tkg is None or tonto is None:
            continue
        if tkg != tonto:
            only_kg = [x for x in tkg if x not in tonto]
            only_onto = [x for x in tonto if x not in tkg]
            errors.append(err("G24-04",
                              "工具 %s 的参数 (name,type,required) 两侧不一致：%s 独有 %s；%s 独有 %s"
                              % (t, "kg", only_kg, "onto", only_onto),
                              tool=t, kg=tkg, onto=tonto))
        want = cface["params"][t]
        for side, got in (("kg", tkg), ("onto", tonto)):
            for x in got:
                if x not in want:
                    errors.append(err("G24-04",
                                      "%s 侧工具 %s 声明了契约外/被排除的参数 %r" % (side, t, x),
                                      tool=t, side=side, param=x[0]))
            for x in want:
                if x not in got:
                    errors.append(err("G24-04",
                                      "%s 侧工具 %s 缺契约参数 %r" % (side, t, x), tool=t, side=side))

        rkg, ronto = mkg["returns"].get(t), monto["returns"].get(t)
        if rkg != ronto:
            errors.append(err("G24-05",
                              "工具 %s 的顶层返回字段名两侧不一致：kg=%s onto=%s" % (t, rkg, ronto),
                              tool=t, kg=rkg, onto=ronto))
        want_r = set(cface["returns"][t])
        for side, got in (("kg", rkg), ("onto", ronto)):
            miss = sorted(want_r - set(got))
            extra = sorted(set(got) - want_r)
            if miss:
                errors.append(err("G24-05", "%s 侧工具 %s 缺返回字段 %s" % (side, t, miss),
                                  tool=t, side=side, missing=miss))
            if extra:
                errors.append(err("G24-05", "%s 侧工具 %s 声明契约外返回字段 %s" % (side, t, extra),
                                  tool=t, side=side, extra=extra))

        for side, mf in (("kg", mkg), ("onto", monto)):
            d = mf["descriptions"].get(t)
            if d is not None and BANNED_DESC.search(d):
                errors.append(err("G24-08",
                                  "%s 侧工具 %s 的 description 命中禁用词（docs/05 §5.2）"
                                  % (side, t), tool=t, side=side))
        if t in mkg["descriptions"] and t in monto["descriptions"]:
            if mkg["descriptions"][t] != monto["descriptions"][t]:
                errors.append(err("G24-05",
                                  "工具 %s 的 description 两侧不逐字相同（F0 检查项）" % t, tool=t))
        elif (t in mkg["descriptions"]) != (t in monto["descriptions"]):
            warns.append(warn("G24-05",
                              "工具 %s 的 description 只有一侧声明 ⇒ 本项不可比（未参与比对）" % t,
                              tool=t))

        for side, mf, other in (("kg", mkg, monto), ("onto", monto, mkg)):
            for f, ty in (mf["types"].get(t) or {}).items():
                oty = (other["types"].get(t) or {}).get(f)
                if oty is not None and oty != ty:
                    errors.append(err("G24-05",
                                      "工具 %s 返回字段 %s 的类型两侧不一致：%s=%s vs %s=%s"
                                      % (t, f, side, ty, "onto" if side == "kg" else "kg", oty),
                                      tool=t, field=f))

    compared = [t for t in cset if t in mkg["params"] and t in monto["params"]]
    if not compared:
        errors.append(err("G24-07", "实际参与比对的工具数为 0（反恒真下限失败）"))
    if len(compared) < min_tools:
        errors.append(err("G24-07",
                          "实际参与比对的工具数 %d < --min-tools %d（反恒真下限失败）"
                          % (len(compared), min_tools)))

    excluded_params = sum(len(v) for v in cface["excluded_params"].values())
    excluded_returns = sum(len(v) for v in cface["excluded_returns"].values())
    compared_params = sum(len(cface["params"][t]) for t in compared)
    compared_returns = sum(len(cface["returns"][t]) for t in compared)
    info.update({
        "compared_tools": len(compared), "compared_params": compared_params,
        "compared_return_fields": compared_returns,
        "excluded_params": excluded_params, "excluded_return_fields": excluded_returns,
    })
    return errors, warns, info


# ---------------------------------------------------------------------------
# 三源交叉一致（G24-10）
# ---------------------------------------------------------------------------
def cross_check(cface, info_products, kgt_tools, contract):
    errors, info = [], {}
    cset = list(cface["tools"])
    cset_s = set(cset)
    access = (info_products or {}).get("access") or {}
    # ★ R-52：规则臂（KG-R/ONTO-R）的工具面 = 契约全集 − 该臂声明的 tools_removed_from_setb。
    #   允许移除的工具集**声明驱动**（不硬编码工具名 —— HB6）：契约 c3.rule_arm_removable_tools。
    removable = (contract.get("c3") or {}).get("rule_arm_removable_tools")
    if not isinstance(removable, list) or not removable:
        errors.append(err("G24-10",
                          "契约 c3.rule_arm_removable_tools 缺失/为空 —— 无法判定规则臂的移除集是否合法"
                          "（fail-closed：不得回落成「任意移除都放行」）"))
        removable = None
    removable_s = set(removable) if removable else None
    for arm in SETB_ARMS:
        entry = access.get(arm) or {}
        got = entry.get("tools")
        if got is None:
            errors.append(err("G24-10",
                              "info-products.yaml#access.%s.tools 缺失（C3 的机械依据不完整）" % arm))
            continue
        removed = list(entry.get("tools_removed_from_setb") or [])
        want = [t for t in cset if t not in set(removed)]
        if list(got) != want:
            errors.append(err("G24-10",
                              "info-products.yaml#access.%s.tools 与「契约 tools − tools_removed_from_setb」"
                              "不一致：%s != %s" % (arm, list(got), want), arm=arm))
            continue
        union = set(got) | set(removed)
        if union != cset_s:
            errors.append(err("G24-10",
                              "info-products.yaml#access.%s 的 tools ∪ tools_removed_from_setb "
                              "!= 契约全集（缺 %s / 多 %s）"
                              % (arm, sorted(cset_s - union), sorted(union - cset_s)), arm=arm))
            continue
        if removable_s is not None:
            illegal = sorted(set(removed) - removable_s)
            if illegal:
                errors.append(err("G24-10",
                                  "info-products.yaml#access.%s 的 tools_removed_from_setb 含非白名单工具 %s"
                                  "（允许移除 %s，见契约 c3.rule_arm_removable_tools）"
                                  % (arm, illegal, sorted(removable_s)), arm=arm))
                continue
        info.setdefault("arms_matched", []).append(arm)
        if removed:
            info.setdefault("arm_removed_tools", {})[arm] = sorted(removed)

    allowed_free = list((kgt_tools or {}).get("allowed_tools_kgt_free") or [])
    forbidden_free = list((kgt_tools or {}).get("forbidden_tools_kgt_free") or [])
    inter = sorted(set(allowed_free) & set(cset))
    if inter:
        errors.append(err("G24-10",
                          "kgt-tools.yaml#allowed_tools_kgt_free 与契约工具集有交集 %s（方案3 插件组不得拿到 Set B 工具）"
                          % inter))
    missed = sorted(set(cset) - set(forbidden_free))
    if missed:
        errors.append(err("G24-10",
                          "kgt-tools.yaml#forbidden_tools_kgt_free 未包含 Set B 工具 %s" % missed))
    info["kgt_allowed_intersection"] = inter
    info["kgt_forbidden_superset"] = not missed
    # 信息性记录（方案3 的禁令，不是冲突）
    info["kgt_forbidden_main_arm_intersection"] = sorted(
        set(kgt_tools.get("forbidden_tools_main_arm") or []) & set(cset))
    info["kgt_forbidden_all_arms_intersection"] = sorted(
        set(kgt_tools.get("forbidden_tools_all_arms") or []) & set(cset))
    attrib = (kgt_tools or {}).get("attribution_axis_forbidden") or {}
    if attrib:
        info["attribution_axis_forbidden"] = {
            "tool": attrib.get("tool"),
            "applies_to_arms": attrib.get("applies_to_arms"),
            "tool_in_contract": attrib.get("tool") in cset,
        }
    return errors, info


# ---------------------------------------------------------------------------
# 契约自检（G24-08，不需要 manifest）
# ---------------------------------------------------------------------------
def contract_checks(contract):
    errors = []
    tools = contract.get("tools") or {}
    if not tools:
        raise Blocked("契约 tools 为空")
    env = contract.get("return_envelope") or {}
    if not env.get("fields"):
        errors.append(err("G24-08", "契约缺 return_envelope.fields（公共信封）"))
    for tname, spec in tools.items():
        if spec.get("name") != tname:
            errors.append(err("G24-08", "契约 tools.%s.name = %r 与键名不符" % (tname, spec.get("name"))))
        if not (spec.get("description") or "").strip():
            errors.append(err("G24-08", "契约 tools.%s 缺 description" % tname))
        elif BANNED_DESC.search(spec["description"]):
            errors.append(err("G24-08", "契约 tools.%s 的 description 命中禁用词（docs/05 §5.2）" % tname))
        for r in spec.get("returns") or []:
            if not isinstance(r, dict) or not r.get("name"):
                errors.append(err("G24-08", "契约 tools.%s 的 returns 项缺 name" % tname))
                continue
            if not r.get("c3_participation"):
                errors.append(err("G24-08",
                                  "契约 tools.%s.returns[%s] 缺 c3_participation（必须显式声明参与/排除）"
                                  % (tname, r.get("name"))))
            if r.get("c3_participation") == "excluded" and not (r.get("excluded_reason") or "").strip():
                errors.append(err("G24-08",
                                  "契约 tools.%s.returns[%s] 被排除但缺 excluded_reason"
                                  % (tname, r.get("name"))))
        for p in spec.get("params") or []:
            if not isinstance(p, dict) or not p.get("name"):
                errors.append(err("G24-08", "契约 tools.%s 的 params 项缺 name" % tname))
                continue
            if not p.get("type"):
                errors.append(err("G24-08", "契约 tools.%s.params[%s] 缺 type" % (tname, p.get("name"))))
            if p.get("c3_participation") == "excluded" and not (p.get("excluded_reason") or "").strip():
                errors.append(err("G24-08",
                                  "契约 tools.%s.params[%s] 被排除但缺 excluded_reason"
                                  % (tname, p.get("name"))))
    return errors


def contract_counts(contract, cface):
    """从**契约现场**重算 g24.boundary_counts 的全部数量键（不依赖 manifest）。

    ★ S3a：口径 = 契约**登记**面（引用条目按登记的 params 字面计数，read_facts 登记
      params: [] ⇒ 0）。引用展开只作用于比对面 contract_face；若把展开后的参数计入此处，
      compared_params 会由冻结声明里的 11 变 16 ⇒ G24-11 误报「契约被改过而计数未同步」。
      cface 入参保留以维持调用契约，本函数只读契约文本。
    """
    tools = contract.get("tools") or {}
    env = ((contract.get("return_envelope") or {}).get("fields")) or []
    env_cmp = len([f for f in env if f.get("c3_participation", "compared") != "excluded"])
    env_exc = len(env) - env_cmp
    n = len(tools)
    cp = ep = 0
    for spec in tools.values():
        for p in (spec.get("params") or []) if isinstance(spec, dict) else []:
            if p.get("c3_participation", "compared") == "excluded":
                ep += 1
            else:
                cp += 1
    tr = er = 0
    for t, spec in tools.items():
        for r in spec.get("returns") or []:
            if r.get("c3_participation", "compared") == "excluded":
                er += 1
            else:
                tr += 1
    return {
        "tools": n,
        "compared_tools": n,
        "compared_params": cp,
        "excluded_params": ep,
        "tool_level_return_fields": tr,
        "envelope_fields": env_cmp,
        "compared_return_fields": tr + env_cmp * n,
        "excluded_return_fields": er + env_exc * n,
        "carrier_specific_divergences": sum(
            len(spec.get("carrier_specific") or []) for spec in tools.values()),
    }


def boundary_check(contract, cface):
    """G24-11：契约的静态计数声明必须等于现场重算值（只依赖契约，--contract-only 也有判别力）。"""
    errors = []
    computed = contract_counts(contract, cface)
    decl = ((contract.get("g24") or {}).get("boundary_counts")) or {}
    if not decl:
        return [err("G24-11", "契约缺 g24.boundary_counts ⇒ 数量声明不可重算")]
    for k, v in sorted(computed.items()):
        if k not in decl:
            errors.append(err("G24-11", "契约 g24.boundary_counts 缺键 %s（重算值 %s）" % (k, v)))
        elif decl[k] != v:
            errors.append(err("G24-11",
                              "契约 g24.boundary_counts.%s = %r ≠ 现场重算 %r（契约被改过而计数未同步）"
                              % (k, decl[k], v)))
    return errors


# ---------------------------------------------------------------------------
# 可读输出
# ---------------------------------------------------------------------------
LINE = "=" * 78


def declared_tool_count(contract_path=None):
    """★ S2c：横幅里的「Set B N 工具面」从契约派生（tools 条数），避免文案再次过期。

    contract_path 不可读/无 tools 列表时返回 None（横幅退化为不带数字的写法）；
    横幅不是判据，绝不因它抛异常，也不改变任何 PASS/FAIL/BLOCKED 语义。
    """
    path = contract_path or CONTRACT_DEFAULT
    if not os.path.isabs(path):
        path = os.path.join(REPO_ROOT_DEFAULT, path)
    try:
        doc = load_yaml(path)
        tools = (doc or {}).get("tools")
        if isinstance(tools, list) and tools:
            return len(tools)
    except Exception:
        return None
    return None


def face_label(contract_path=None):
    """横幅用「N 工具面」；契约不可读时不带数字（不写死、不再过期）。"""
    n = declared_tool_count(contract_path)
    return ("%d 工具面" % n) if n else "工具面"


def print_provenance(contract_path, contract_sha, kg_path, kg_sha, onto_path, onto_sha,
                     repo_root, min_tools, min_src, prov_rows):
    out = [LINE, "G24 Set B %s 实现级一致性门禁（C3 / P7）" % face_label(contract_path), LINE,
           "★ 输入清单（路径 + sha256）—— 显式打印以消除 argparse 默认值歧义"
           "（先例 t10_native_parity.py:1026/:356）：",
           "  契约          : %s  sha256=%s" % (contract_path, contract_sha),
           "  KG manifest   : %s  sha256=%s" % (kg_path, kg_sha),
           "  ONTO manifest : %s  sha256=%s" % (onto_path, onto_sha),
           "  repo-root     : %s" % repo_root,
           "  --min-tools   : %s" % min_tools,
           ""]
    if prov_rows:
        out.append("★ manifest 声明的实现源文件（同源校验，G24-02）：")
        for side, rel, actual, claimed, status in prov_rows:
            out.append("  [%-8s] %-52s 实际=%s 声明=%s %s"
                       % (status, rel, actual[:16] + "…" if len(actual) > 16 else actual,
                          claimed[:16] + "…" if len(claimed) > 16 else claimed, status))
        out.append("")
    sys.stdout.write("\n".join(out) + "\n")


def emit_json(payload):
    sys.stdout.write("--- JSON ---\n")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def print_blocked(reason, detail_lines, payload, contract_path=None):
    lines = ["", "★ 前置条件不满足：%s" % reason]
    lines += ["  " + d for d in detail_lines]
    lines += [
        "",
        "★ 未执行：Set B %s 的实现级比对（工具名集合 / 参数 (name,type,required) / 顶层返回字段名）"
        "与三源交叉一致判据中的 manifest 部分。" % face_label(contract_path),
        "  本门禁从设计上 fail-closed：缺输入时**既不打印 PASS，也不打印逐行 diff** ——"
        "缺输入造成的『两侧都空』是**假一致**，不是『两侧一致』。",
        "",
        "★ BLOCKED ≠ FAIL：",
        "  FAIL    = 两侧**声明的**工具面**真实分歧**（工具名/参数/返回字段不一致，或清单与实现不同源）"
        "⇒ 拒绝采信「同一对照对公平」这一前提，去查两侧服务实现与共享注册表。",
        "  BLOCKED = **缺前置条件**（契约 / 某一侧 manifest / manifest 声明的实现文件缺失）"
        "⇒ 本次比对本身不可采信；",
        "            处置：先由两侧服务以 --emit-manifest 产出清单（M3.2/M3.3），或修正路径，再重跑本门禁。",
        "",
        "RESULT: BLOCKED（前置条件不满足 ⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2）",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    payload["result"] = "BLOCKED"
    payload["exitCode"] = 2
    payload["blockedReason"] = reason


def print_param_error(reason, payload):
    sys.stdout.write("\nPARAM_ERROR: %s\n" % reason)
    sys.stdout.write("RESULT: PARAM_ERROR（命令行参数非法 ⇒ 本门禁未执行；既非 PASS 亦非 FAIL，exit=2）\n")
    payload["result"] = "PARAM_ERROR"
    payload["exitCode"] = 2
    payload["blockedReason"] = reason


# ---------------------------------------------------------------------------
# --selftest（离线；含 3 条真能失败的负控）
# ---------------------------------------------------------------------------
def _mini_contract():
    return {
        "return_envelope": {"fields": [
            {"name": "hint", "c3_participation": "compared"},
            {"name": "truncated", "c3_participation": "compared"},
        ]},
        "tools": {
            "cc_a": {
                "name": "cc_a",
                "description": "只读查询，最多返回 200 行。",
                "params": [{"name": "query", "type": "string", "required": True}],
                "returns": [{"name": "rows", "c3_participation": "compared"},
                            {"name": "rowCount", "c3_participation": "compared"},
                            {"name": "internal", "c3_participation": "excluded",
                             "excluded_reason": "载体特有"}],
            },
            "cc_b": {
                "name": "cc_b",
                "description": "列清单，最多返回 200 行。",
                "params": [],
                "returns": [{"name": "items", "c3_participation": "compared"}],
            },
        },
    }


def _mini_manifest(carrier, tools):
    return {"schema": "dsh-toolface-manifest/1", "carrier": carrier, "arm": None, "tools": tools}


def _tool(name, params, returns, desc=None):
    t = {"name": name, "params": params, "returns": returns}
    if desc is not None:
        t["description"] = desc
    return t


def selftest(verbose=True):
    results = []

    def check(label, ok, detail=""):
        results.append((label, bool(ok), detail))

    c = _mini_contract()
    cface = contract_face(c)

    good = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}],
                  ["rows", "rowCount", "hint", "truncated"]),
            _tool("cc_b", [], ["items", "hint", "truncated"])]

    # 1) 正控：两侧完全一致 ⇒ 0 error
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", good), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 2)
    check("S1 两侧一致 ⇒ 无 ERROR（正控）", not e, str(e))
    check("S2 compared/excluded 计数正确（排除由契约标志驱动）",
          i == {"compared_tools": 2, "compared_params": 1, "compared_return_fields": 7,
                "excluded_params": 0, "excluded_return_fields": 1}, str(i))

    # 2) 负控：工具集不等（onto 少一个）⇒ ERROR
    bad = [good[0]]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", good), "kg"),
                            manifest_face(_mini_manifest("onto", bad), "onto"), 2)
    check("S3 两侧工具集不等 ⇒ ERROR（负控，真能失败）",
          any(x["code"] == "G24-03" for x in e) and any("cc_b" in x["detail"] for x in e), str(e))

    # 3) 负控：参数名差一个字符 ⇒ ERROR
    off = [_tool("cc_a", [{"name": "querys", "type": "string", "required": True}],
                 ["rows", "rowCount", "hint", "truncated"]),
           _tool("cc_b", [], ["items", "hint", "truncated"])]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", off), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 2)
    check("S4 参数名差一个字符 ⇒ ERROR（负控，真能失败）",
          any(x["code"] == "G24-04" for x in e), str(e))

    # 4) 负控：空集合被 --min-tools 拦住 ⇒ ERROR
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", []), "kg"),
                            manifest_face(_mini_manifest("onto", []), "onto"), 2)
    check("S5 空工具集被反恒真下限拦住 ⇒ ERROR（负控，真能失败）",
          any(x["code"] == "G24-07" for x in e) and any(x["code"] == "G24-03" for x in e), str(e))

    # 5) 负控：下限抬高到 3 > 实际 2 ⇒ ERROR
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", good), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 3)
    check("S6 实际比对工具数 < --min-tools ⇒ ERROR",
          any(x["code"] == "G24-07" for x in e), str(e))

    # 6) 参数类型不同 ⇒ ERROR
    wrong_ty = [_tool("cc_a", [{"name": "query", "type": "string", "required": False}],
                      ["rows", "rowCount", "hint", "truncated"]),
                _tool("cc_b", [], ["items", "hint", "truncated"])]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", wrong_ty), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 2)
    check("S7 参数必填性/类型不同 ⇒ ERROR", any(x["code"] == "G24-04" for x in e), str(e))

    # 7) 返回字段缺失 / 契约外字段 ⇒ ERROR
    miss = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}], ["rows"]),
            _tool("cc_b", [], ["items", "hint", "truncated"])]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", miss), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 2)
    check("S8 返回字段缺失 ⇒ ERROR", any(x["code"] == "G24-05" for x in e), str(e))
    extra = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}],
                   ["rows", "rowCount", "hint", "truncated", "bogus"]),
             _tool("cc_b", [], ["items", "hint", "truncated"])]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", extra), "kg"),
                            manifest_face(_mini_manifest("onto", good), "onto"), 2)
    check("S9 契约外返回字段 ⇒ ERROR", any(x["code"] == "G24-05" and "bogus" in x["detail"] for x in e),
          str(e))

    # 8) description 逐字不同 ⇒ ERROR；禁用词 ⇒ ERROR
    d1 = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}],
                ["rows", "rowCount", "hint", "truncated"], desc="查询 A"),
          _tool("cc_b", [], ["items", "hint", "truncated"], desc="清单")]
    d2 = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}],
                ["rows", "rowCount", "hint", "truncated"], desc="查询 B"),
          _tool("cc_b", [], ["items", "hint", "truncated"], desc="清单")]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", d1), "kg"),
                            manifest_face(_mini_manifest("onto", d2), "onto"), 2)
    check("S10 description 两侧不逐字相同 ⇒ ERROR（F0 检查项）",
          any("description" in x["detail"] for x in e), str(e))
    d3 = [_tool("cc_a", [{"name": "query", "type": "string", "required": True}],
                ["rows", "rowCount", "hint", "truncated"], desc="用 Cypher 查询"),
          _tool("cc_b", [], ["items", "hint", "truncated"], desc="清单")]
    e, w, i = compare_faces(cface, manifest_face(_mini_manifest("kg", d3), "kg"),
                            manifest_face(_mini_manifest("onto", d1), "onto"), 2)
    check("S11 description 命中禁用词 ⇒ ERROR（docs/05 §5.2）",
          any(x["code"] == "G24-08" for x in e), str(e))

    # 11) G24-11 契约静态计数自洽（不依赖 manifest）
    c3_ = _mini_contract()
    cc = contract_counts(c3_, contract_face(c3_))
    check("S16 契约现场重算计数正确", cc == {"tools": 2, "compared_tools": 2, "compared_params": 1,
                                             "excluded_params": 0, "tool_level_return_fields": 3,
                                             "envelope_fields": 2, "compared_return_fields": 7,
                                             "excluded_return_fields": 1,
                                             "carrier_specific_divergences": 0}, str(cc))
    check("S17 契约缺 g24.boundary_counts ⇒ ERROR",
          any(x["code"] == "G24-11" for x in boundary_check(c3_, contract_face(c3_))))
    c4_ = _mini_contract()
    c4_["g24"] = {"boundary_counts": dict(cc)}
    check("S18 静态声明 == 现场重算 ⇒ 无 ERROR（正控）",
          not boundary_check(c4_, contract_face(c4_)))
    c5_ = _mini_contract()
    bad_decl = dict(cc)
    bad_decl["compared_return_fields"] = 23
    c5_["g24"] = {"boundary_counts": bad_decl}
    check("S19 静态声明 ≠ 现场重算 ⇒ ERROR（负控，真能失败）",
          any(x["code"] == "G24-11" for x in boundary_check(c5_, contract_face(c5_))))

    # 12) ★ S3a 引用形态（neutral_tool_ref，D47 单一定义源）：正控对**真实两文件**，负控用临时夹具
    try:
        real_contract = load_yaml(resolve(REPO_ROOT_DEFAULT, CONTRACT_DEFAULT))
        rf_face = contract_face(real_contract, repo_root=REPO_ROOT_DEFAULT)
        ref_err = None
    except Blocked as exc:
        rf_face, ref_err = {"params": {}, "returns": {}, "envelope": []}, str(exc)
    tri = rf_face["params"].get("read_facts")
    check("S20 引用展开正控：read_facts 5 个入参项 = layers/sections/scope/max_records/max_bytes"
          "（required 全 False，type 依次 array/array/string/integer/integer）",
          ref_err is None and tri == [("layers", "array", False), ("max_bytes", "integer", False),
                                      ("max_records", "integer", False), ("scope", "string", False),
                                      ("sections", "array", False)], ref_err or str(tri))
    expect_r = sorted(set(["objects", "relations", "observations", "source"])
                      | set(rf_face["envelope"]))
    check("S21 引用展开正控：返回名 = tool_level 4 名 ∪ 公共信封，且公共信封恰为 camelCase 四名",
          rf_face["returns"].get("read_facts") == expect_r
          and rf_face["envelope"] == ["hint", "truncated", "droppedRows", "droppedBytes"],
          "%s / env=%s" % (rf_face["returns"].get("read_facts"), rf_face["envelope"]))

    def _blk(fn):
        """跑一次夹具；Blocked ⇒ 返回错误文案，未抛 ⇒ None。"""
        try:
            fn()
        except Blocked as exc:
            return str(exc)
        return None

    tmp_ref = tempfile.mkdtemp(prefix="g24_ref_selftest_")
    try:
        os.makedirs(os.path.join(tmp_ref, MODEL_DIR_REL), exist_ok=True)
        shape_ok = (
            "read_facts:\n"
            "  description: \"夹具形状（只读取数）。\"\n"
            "  params:\n"
            "    layers: { type: array }\n"
            "    max_records: { type: integer }\n"
            "  required: [max_records]\n"
            "  returns:\n"
            "    tool_level:\n"
            "      objects: array\n"
            "      source: { snapshot_id: string }\n"
            "    envelope:\n"
            "      hint: string\n"
            "      truncated: boolean\n"
            "      droppedRows: integer\n"
            "      droppedBytes: integer\n"
        )
        shape_bad_env = shape_ok.replace("droppedRows: integer", "dropped_records: integer") \
                                .replace("droppedBytes: integer", "dropped_bytes: integer")
        for fn, txt in (("shape-ok.yaml", shape_ok), ("shape-bad-env.yaml", shape_bad_env)):
            with open(os.path.join(tmp_ref, MODEL_DIR_REL, fn), "w",
                      encoding="utf-8", newline="\n") as fh:
                fh.write(txt)

        def _ref_contract(ref, **extra):
            entry = {"name": "read_facts", "description": "夹具条目（只读取数）。",
                     "params": [], "returns": [], REF_KEY: ref}
            entry.update(extra)
            return {"return_envelope": {"fields": [
                {"name": "hint", "c3_participation": "compared"},
                {"name": "truncated", "c3_participation": "compared"},
                {"name": "droppedRows", "c3_participation": "compared"},
                {"name": "droppedBytes", "c3_participation": "compared"}]},
                "tools": {"read_facts": entry}}

        msg = _blk(lambda: contract_face(_ref_contract("shape-bad-env.yaml#read_facts"),
                                         repo_root=tmp_ref))
        check("S22 负控：被引用形状的信封键名 ≠ 公共信封 ⇒ BLOCKED（文案逐字列出两个集合）",
              msg is not None and "dropped_records" in msg and "droppedRows" in msg, str(msg))
        msg = _blk(lambda: contract_face(
            _ref_contract("shape-ok.yaml#read_facts",
                          params=[{"name": "layers", "type": "array"}]), repo_root=tmp_ref))
        check("S23 负控：条目既带 %s 又带非空 params ⇒ BLOCKED（ERR_REF_CONFLICT 同规则）" % REF_KEY,
              msg is not None and "冲突" in msg, str(msg))
        msg = _blk(lambda: contract_face(_ref_contract("no-such-shape.yaml#read_facts"),
                                         repo_root=tmp_ref))
        check("S24 负控：引用文件不存在 ⇒ BLOCKED", msg is not None and "引用文件不存在" in msg, str(msg))
        msg = _blk(lambda: contract_face(_ref_contract("shape-ok.yaml#no_such_key"), repo_root=tmp_ref))
        check("S25 负控：引用键不存在 ⇒ BLOCKED", msg is not None and "引用键不存在" in msg, str(msg))
        msg = _blk(lambda: contract_face(_ref_contract("shape-ok.yaml"), repo_root=tmp_ref))
        check("S26 负控：引用格式非法（无 '#'）⇒ BLOCKED", msg is not None and "格式非法" in msg, str(msg))
        msg = _blk(lambda: contract_face(_ref_contract("shape-ok.md#read_facts"), repo_root=tmp_ref))
        check("S27 负控：引用格式非法（.md 非 yaml）⇒ BLOCKED",
              msg is not None and "格式非法" in msg, str(msg))
    finally:
        shutil.rmtree(tmp_ref, ignore_errors=True)

    # 9) 同源校验：sha256 不一致 ⇒ ERROR；缺声明 ⇒ Blocked
    tmp = tempfile.mkdtemp(prefix="g24_selftest_")
    try:
        impl = os.path.join(tmp, "toolface_stub.py")
        with open(impl, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("TOOLS = []\n")
        real = sha256_file(impl)
        mf = _mini_manifest("kg", good)
        mf["source"] = "toolface_stub.py"
        mf["sourceSha256"] = real
        prov = []
        errs, _ = verify_sources(mf, "kg", tmp, prov)
        check("S12 sourceSha256 与实际一致 ⇒ 无 ERROR（正控）", not errs, str(errs))
        mf["sourceSha256"] = "0" * 64
        errs, _ = verify_sources(mf, "kg", tmp, [])
        check("S13 sourceSha256 不一致 ⇒ ERROR（清单与实现不同源）",
              any(x["code"] == "G24-02" and "不同源" in x["detail"] for x in errs), str(errs))
        mf2 = _mini_manifest("kg", good)
        blocked = False
        try:
            verify_sources(mf2, "kg", tmp, [])
        except Blocked:
            blocked = True
        check("S14 manifest 未声明来源 ⇒ BLOCKED（fail-closed，不打印 PASS）", blocked)

        # 10) 契约自检：returns 缺 c3_participation ⇒ ERROR
        c2 = _mini_contract()
        c2["tools"]["cc_b"]["returns"] = [{"name": "items"}]
        e2 = contract_checks(c2)
        check("S15 契约 returns 缺 c3_participation ⇒ ERROR",
              any(x["code"] == "G24-08" and "c3_participation" in x["detail"] for x in e2), str(e2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    npass = sum(1 for _, ok, _ in results if ok)
    if verbose:
        sys.stdout.write("G24 --selftest（离线，无网络）\n" + "-" * 78 + "\n")
        for label, ok, detail in results:
            sys.stdout.write("[%s] %s%s\n" % ("PASS" if ok else "FAIL", label,
                                              "" if ok else "  → " + detail[:400]))
        sys.stdout.write("-" * 78 + "\n")
        sys.stdout.write("selftest: %d/%d PASS\n" % (npass, len(results)))
        sys.stdout.write("RESULT: %s\n" % ("PASS" if npass == len(results) else "FAIL"))
    return 0 if npass == len(results) else 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="toolset_parity_gate.py",
        description=("G24 Set B %s 实现级一致性门禁（C3 / P7）；退出码 0=PASS 1=FAIL 2=BLOCKED/PARAM_ERROR"
                     % face_label(CONTRACT_DEFAULT)),
    )
    ap.add_argument("--contract", default=CONTRACT_DEFAULT)
    ap.add_argument("--kg-manifest", default=None)
    ap.add_argument("--onto-manifest", default=None)
    ap.add_argument("--repo-root", default=REPO_ROOT_DEFAULT)
    ap.add_argument("--min-tools", default=None, help="反恒真下限；默认 = 契约声明的工具数")
    ap.add_argument("--contract-only", action="store_true",
                    help="只做契约自检 + 三源交叉一致（不需要 manifest）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    payload = {"gate": "G24", "contract": args.contract, "repoRoot": args.repo_root}
    repo_root = os.path.abspath(args.repo_root)
    if not os.path.isdir(repo_root):
        payload["result"] = "BLOCKED"
        print_blocked("repo-root 不是目录：%s" % repo_root, [], payload)
        if args.json:
            emit_json(payload)
        return 2
    contract_path = resolve(repo_root, args.contract)
    kg_path = resolve(repo_root, args.kg_manifest) if args.kg_manifest else None
    onto_path = resolve(repo_root, args.onto_manifest) if args.onto_manifest else None

    csha = sha256_file(contract_path) if os.path.isfile(contract_path) else "(缺失)"
    ksha = sha256_file(kg_path) if kg_path and os.path.isfile(kg_path) else "(缺失)"
    osha = sha256_file(onto_path) if onto_path and os.path.isfile(onto_path) else "(缺失)"

    # ---- 参数校验（PARAM_ERROR ⇒ exit 2）----
    try:
        if not os.path.isfile(contract_path):
            raise Blocked("契约文件不存在：%s" % contract_path)
        contract = load_yaml(contract_path)
        if not isinstance(contract, dict) or not contract.get("tools"):
            raise Blocked("契约为空或不可解析：%s" % contract_path)
        tools_declared = len(contract["tools"])
        if args.min_tools is None:
            min_tools = tools_declared
        else:
            try:
                min_tools = int(str(args.min_tools))
            except ValueError:
                raise ParamError("--min-tools 非法（必须为 ≥1 的整数）：%r" % args.min_tools)
            if min_tools < 1:
                raise ParamError("--min-tools 非法（必须为 ≥1 的整数）：%r" % args.min_tools)
        if (not args.contract_only) and kg_path and onto_path and \
                os.path.abspath(kg_path) == os.path.abspath(onto_path):
            raise ParamError("两侧 manifest 指向同一文件：%s（无意义的比对）" % kg_path)
    except ParamError as exc:
        print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                         onto_path or "(未提供)", osha, repo_root, args.min_tools, None, [])
        print_param_error(str(exc), payload)
        if args.json:
            emit_json(payload)
        return 2
    except Blocked as exc:
        print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                         onto_path or "(未提供)", osha, repo_root, args.min_tools, None, [])
        print_blocked(str(exc), [], payload, contract_path=contract_path)
        if args.json:
            emit_json(payload)
        return 2

    payload.update({"contractSha256": csha, "minTools": min_tools,
                    "toolsDeclared": tools_declared})

    # ---- 契约自检 + 三源交叉（不需要 manifest）----
    errors = list(contract_checks(contract))
    try:                       # ★ S3a：引用展开的硬失败也必须走既有 Blocked ⇒ exit 2（fail-closed）
        cface = contract_face(contract, repo_root=repo_root)
    except Blocked as exc:
        print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                         onto_path or "(未提供)", osha, repo_root, min_tools,
                         tools_declared, [])
        print_blocked(str(exc), [], payload, contract_path=contract_path)
        if args.json:
            emit_json(payload)
        return 2
    contract_counts_value = contract_counts(contract, cface)
    errors += boundary_check(contract, cface)
    info_products_path = resolve(repo_root, INFO_PRODUCTS_DEFAULT)
    kgt_tools_path = resolve(repo_root, KGT_TOOLS_DEFAULT)
    cross_info = {}
    try:
        if not os.path.isfile(info_products_path):
            raise Blocked("C3 机械依据缺失：%s" % info_products_path)
        if not os.path.isfile(kgt_tools_path):
            raise Blocked("禁令来源缺失：%s" % kgt_tools_path)
        ip = load_yaml(info_products_path)
        kgt = load_yaml(kgt_tools_path)
        xerr, cross_info = cross_check(cface, ip, kgt, contract)
        errors += xerr
    except Blocked as exc:
        print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                         onto_path or "(未提供)", osha, repo_root, min_tools, tools_declared, [])
        print_blocked(str(exc), [], payload, contract_path=contract_path)
        if args.json:
            emit_json(payload)
        return 2

    payload["contractSelfCheck"] = {
        "toolSet": cface["tools"], "toolCount": len(cface["tools"]),
        "envelope": cface["envelope"],
        "excludedParams": cface["excluded_params"],
        "excludedReturnFields": cface["excluded_returns"],
    }
    payload["crossCheck"] = cross_info
    payload["contractCounts"] = contract_counts_value

    # ---- manifest 比对 ----
    prov_rows = []
    warns, stats = [], {}
    if not args.contract_only:
        if not kg_path or not onto_path:
            print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                             onto_path or "(未提供)", osha, repo_root, min_tools, tools_declared, [])
            miss = []
            if not kg_path:
                miss.append("未提供 --kg-manifest（Set B 的工具在本仓库**没有任何实现**，"
                            "需 M3.2/M3.3 的服务以 --emit-manifest 产出清单）")
            if not onto_path:
                miss.append("未提供 --onto-manifest（同上）")
            print_blocked("两侧 manifest 未齐备", miss, payload, contract_path=contract_path)
            if args.json:
                emit_json(payload)
            return 2
        for label, p in (("--kg-manifest", kg_path), ("--onto-manifest", onto_path)):
            if not os.path.isfile(p):
                print_provenance(contract_path, csha, kg_path, ksha, onto_path, osha,
                                 repo_root, min_tools, tools_declared, [])
                print_blocked("%s 指向的文件不存在：%s" % (label, p), [], payload, contract_path=contract_path)
                if args.json:
                    emit_json(payload)
                return 2
        try:
            mkg = load_json(kg_path)
            monto = load_json(onto_path)
            fkg = manifest_face(mkg, "kg")
            fonto = manifest_face(monto, "onto")
        except Blocked as exc:
            print_provenance(contract_path, csha, kg_path, ksha, onto_path, osha,
                             repo_root, min_tools, tools_declared, [])
            print_blocked(str(exc), [], payload, contract_path=contract_path)
            if args.json:
                emit_json(payload)
            return 2
        except ValueError as exc:
            errors.append(err("G24-03", str(exc)))
            mkg = monto = None

        if mkg is not None:
            try:
                serr, prov_rows = verify_sources(mkg, "kg", repo_root, prov_rows)
                errors += serr
                serr, prov_rows = verify_sources(monto, "onto", repo_root, prov_rows)
                errors += serr
            except Blocked as exc:
                print_provenance(contract_path, csha, kg_path, ksha, onto_path, osha,
                                 repo_root, min_tools, tools_declared, prov_rows)
                print_blocked(str(exc), [], payload, contract_path=contract_path)
                if args.json:
                    emit_json(payload)
                return 2
            pairs = list((contract.get("c3") or {}).get("scope_pairs") or [])
            arm_kg, arm_onto = fkg["arm"], fonto["arm"]
            if arm_kg and arm_onto:
                if [arm_kg, arm_onto] not in pairs and [arm_onto, arm_kg] not in pairs:
                    errors.append(err("G24-03",
                                      "两侧测试组 (%s, %s) 不在契约 c3.scope_pairs %s 内 ⇒ 跨线混比"
                                      % (arm_kg, arm_onto, pairs), armKg=arm_kg, armOnto=arm_onto))
            e2, w2, stats = compare_faces(cface, fkg, fonto, min_tools)
            errors += e2
            warns += w2
            payload["manifests"] = {
                "kg": {"path": kg_path, "sha256": ksha, "arm": arm_kg, "tools": fkg["tools"]},
                "onto": {"path": onto_path, "sha256": osha, "arm": arm_onto, "tools": fonto["tools"]},
            }

    print_provenance(contract_path, csha, kg_path or "(未提供)", ksha,
                     onto_path or "(未提供)", osha, repo_root, min_tools, tools_declared, prov_rows)

    # ---- 人类可读报告 ----
    print("★ 契约自检（G24-08，独立于 manifest）")
    print("  契约工具集（%d）：%s" % (len(cface["tools"]), cface["tools"]))
    print("  公共信封：%s" % cface["envelope"])
    print("  被排除（c3_participation: excluded，由契约标志驱动，不得按侧写白名单）：")
    for t, v in cface["excluded_params"].items():
        if v:
            print("    参数     %s: %s" % (t, v))
    any_exc = False
    for t, v in cface["excluded_returns"].items():
        if v:
            print("    返回字段 %s: %s" % (t, v))
            any_exc = True
    if not any_exc and not any(cface["excluded_params"].values()):
        print("    （无）")
    print("★ 三源交叉一致（G24-10：契约 ↔ info-products.yaml#access ↔ kgt-tools.yaml 禁令）")
    for k in ("arms_matched", "kgt_allowed_intersection", "kgt_forbidden_superset",
              "kgt_forbidden_main_arm_intersection", "kgt_forbidden_all_arms_intersection",
              "attribution_axis_forbidden"):
        if k in cross_info:
            print("  %-42s %s" % (k, json.dumps(cross_info[k], ensure_ascii=False)))
    print("★ 契约现场重算的计数（G24-11 判据的对照基准；不依赖 manifest）")
    print("  " + "  ".join("%s=%s" % (k, contract_counts_value[k])
                           for k in sorted(contract_counts_value)))
    if stats:
        print("★ manifest 侧实际参与比对的计数（C3 可比性口径）")
        print("  compared_tools=%s compared_params=%s compared_return_fields=%s "
              "excluded_params=%s excluded_return_fields=%s min_tools=%s"
              % (stats.get("compared_tools"), stats.get("compared_params"),
                 stats.get("compared_return_fields"), stats.get("excluded_params"),
                 stats.get("excluded_return_fields"), min_tools))
    if warns:
        print("★ WARN（%d）" % len(warns))
        for x in warns:
            print("  [%s] %s" % (x["code"], x["detail"]))
    if errors:
        print("★ ERROR（%d）" % len(errors))
        for x in errors:
            print("  [%s] %s" % (x["code"], x["detail"]))
    print("")
    if errors:
        print("RESULT: FAIL（问题 %d 条：两侧**声明的**工具面存在真实分歧，或契约自检/三源交叉失败）"
              % len(errors))
        payload["result"] = "FAIL"
        payload["exitCode"] = 1
        payload["errors"] = errors
        payload["warnings"] = warns
        if args.json:
            emit_json(payload)
        return 1
    if stats:
        print("RESULT: PASS（Set B %d 工具面在两份 manifest 上逐字一致；★ 仅证『声明的』工具面一致，"
              "**不证**行为/语义一致 —— HB1）" % len(cface["tools"]))
    else:
        print("RESULT: PASS（--contract-only：契约自检 G24-08 + 三源交叉一致 G24-10 + 静态计数自洽 G24-11 "
              "全部通过；★ 本次**未**覆盖两侧实现级比对 G24-03/04/05 —— 那需要两侧 manifest）")
    if not stats:
        print("★ 注意：本次为 --contract-only（未提供 manifest）⇒ 上述 PASS 仅覆盖契约自检与三源交叉一致，"
              "**未**覆盖两侧实现级比对（G24-03/04/05）。")
    payload["result"] = "PASS"
    payload["exitCode"] = 0
    payload["errors"] = []
    payload["warnings"] = warns
    payload["stats"] = stats
    if args.json:
        emit_json(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
