#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M3.3 工具面差异门禁 —— 对**同一对照对**两侧门面（OpenAPI 文档 或 实现自身产出的工具面清单）
执行「规范化后的结构化逐键比对」。

权威依据
  docs/07-实施计划与验收.md:211  M3.3 判据 ②
      「同一对照对内部 OpenAPI 规范化后除 URL 外字节相同」
  docs/07-实施计划与验收.md:218  M3 出口条件
      「两个实验组 7 个工具的入参名、出参 schema、错误语义逐字相同；toolface_diff.py 零差异」

本工具能读两种输入（按内容自动识别，不看文件名）
  kind=openapi   由两侧服务的 /openapi.json 得到（FastAPI 自动生成）
  kind=manifest  由两侧服务的 `--emit-manifest` 得到（``dsh-toolface-manifest/1``，
                 清单「产自实现自身」，是**字段级**出参 schema 的唯一机读来源）

两种判据口径（两个都算、都打印，不隐藏）
  ① --contract-only ：只判**本任务枚举的可判定维度**
        path、method、入参名 / 必填性 / 类型、响应 schema 的字段名与类型、错误码集合
  ② 默认（strict）   ：在 ① 之上，再要求「剥离 URL 与**必然不同的生成器元数据**之后
        逐字节相同」。被剥离的维度在输出里**逐条显式列出**（见 NORMALIZED_* 常量）。

  ★ 设计取向：**默认即严格**。放宽必须显式写 `--contract-only`——
    命令里看得见、输出里也写明。绝不静默放宽。

三种结果（★ BLOCKED ≠ FAIL）
  RESULT: SAME     exit 0
  RESULT: DIFF     exit 1     存在真差异
  RESULT: BLOCKED  exit 2     取不到 / 读不了 / 解析不了输入；或 --pair 与输入不相符

用法
  python shared/verify/toolface_diff.py --kg A.json --onto B.json
  python shared/verify/toolface_diff.py --kg A.json --onto B.json --contract-only
  python shared/verify/toolface_diff.py --kg A.json --onto B.json --pair KG-Q:ONTO-Q
  python shared/verify/toolface_diff.py --kg A.json --onto B.json --json
  python shared/verify/toolface_diff.py --health            # R-30：运行时 /health 键面核对（新增模式）
  python shared/verify/toolface_diff.py --selftest

★ `--health`（R-30 新增，**只新增、不改上面两个口径**）
  两侧 `/health` 的**真实键集合**由 `shared/model/health-face.yaml` 逐侧声明；本模式分别在
  运行时抓两侧真实 `GET /health`（或用 `--health-kg/--health-onto` 指定的载荷文件）比对：
  采集到的键**未登记** ⇒ ERROR、声明里的键**缺失** ⇒ ERROR、全部相符 ⇒ `SAME` / exit 0；
  只在单侧出现的键必须逐条登记为载体特有键，否则 ERROR；读数里保留采集时点与两侧 PID
  （PID 由 `--pid-kg/--pid-onto` 传入：`/health` 载荷本身不暴露 PID）。
  理由：R-27 前后两侧 `/openapi.json` 逐字节未变，而 `/health` 实际键面已变
  ⇒ OpenAPI 平面对 `/health` 不敏感，键面分歧必须在**运行时**口径下显式登记。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime
import hashlib
import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXIT_SAME, EXIT_DIFF, EXIT_BLOCKED = 0, 1, 2

SAME, DIFF, BLOCKED = "SAME", "DIFF", "BLOCKED"

# 允许的对照对（同一对照对内部必须逐字相同）。
PAIRS = [("KG-Q", "ONTO-Q"), ("KG-R", "ONTO-R")]

# ---------------------------------------------------------------------------
# 被规范化的维度（**显式列出**，禁止隐式忽略）
# ---------------------------------------------------------------------------
NORMALIZED_OPENAPI = [
    "URL 类：servers[].url / host / basePath / schemes / port（判据 ② 字面允许的差异）",
    "info.title / info.version / info.description / contact / license / termsOfService"
    "（两侧是两个不同实现的必然差异）",
    "paths.<p>.<m>.summary —— FastAPI 由函数名自动生成（KG 'cc_query' vs ONTO 'Cc Query'）",
    "paths.<p>.<m>.operationId —— FastAPI 由函数名自动生成（/health 两侧不同）",
    "paths.<p>.<m>.description / tags / externalDocs —— 端点文档性元数据",
    "schema 任意层的 title —— jsonschema 自动生成（如 'Response Cc Query Query Post'）",
    "x-* 扩展键",
    "$ref 解引用：components/schemas 的引用被就地展开，"
    "使「$ref 表达」与「内联 anyOf 表达」可比（两侧请求体风格不同，语义需解引用后才可比）；"
    "内联展开后 components.schemas 的命名与存在性随之被规范化（其唯一作用是承载引用目标）",
    "键序 / 缩进 / 空白（本工具比较解析后的结构，天然不受影响）",
]

NORMALIZED_MANIFEST = [
    "carrier / arm —— 两侧载体与实验组名必然不同",
    "tool / source / sourceSha256 / sources[] —— 指向各自实现文件，必然不同",
    "emittedAt —— 采集时刻",
    "键序 / 缩进 / 空白",
]

# ---------------------------------------------------------------------------
# 输入装载
# ---------------------------------------------------------------------------
class Blocked(Exception):
    """无法取得/解析输入 —— 记 BLOCKED，不得当 FAIL。"""


def load_doc(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise Blocked(f"输入不存在: {path}")
    if not p.is_file():
        raise Blocked(f"输入不是常规文件: {path}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise Blocked(f"读取失败: {path}: {e}") from e
    if not raw.strip():
        raise Blocked(f"输入为空文件: {path}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Blocked(f"不是合法 UTF-8 JSON: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise Blocked(f"顶层不是 JSON 对象: {path}")
    return doc


def kind_of(doc: dict) -> str:
    if doc.get("schema") == "dsh-toolface-manifest/1" or (
        "tools" in doc and "carrier" in doc and "paths" not in doc
    ):
        return "manifest"
    if "openapi" in doc or "paths" in doc:
        return "openapi"
    raise Blocked("无法识别的输入种类（既非 OpenAPI 文档，也非 dsh-toolface-manifest/1）")


def file_facts(path: str) -> dict:
    p = Path(path)
    try:
        b = p.read_bytes()
    except FileNotFoundError:
        raise Blocked(f"输入不存在: {path}")
    except OSError as e:
        raise Blocked(f"输入不可读: {path}（{e}）")
    return {
        "path": str(p),
        "bytes": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "lf": b.count(b"\n"),
    }


# ---------------------------------------------------------------------------
# 类型签名：把 $ref / anyOf / 内联 type 归一成一个可逐字比较的字符串
# ---------------------------------------------------------------------------
def resolve_ref(doc: dict, spec):
    """单层解引用（递归由调用方处理）。"""
    if not isinstance(spec, dict):
        return spec
    ref = spec.get("$ref")
    if not isinstance(ref, str):
        return spec
    name = ref.rsplit("/", 1)[-1]
    target = ((doc.get("components") or {}).get("schemas") or {}).get(name)
    if target is None:
        return {"__unresolved__": name}
    return target


def type_sig(doc: dict, spec, depth: int = 0) -> str:
    """返回可逐字比较的类型签名。"""
    if not isinstance(spec, dict):
        return "(nonobject)"
    if "$ref" in spec:
        if depth > 8:
            return "(ref-depth)"
        return type_sig(doc, resolve_ref(doc, spec), depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(spec.get(key), list):
            parts = sorted(type_sig(doc, b, depth + 1) for b in spec[key])
            return f"{key}[{','.join(parts)}]"
    t = spec.get("type")
    if isinstance(t, list):
        return f"multi[{','.join(sorted(str(x) for x in t))}]"
    if isinstance(t, str):
        if t == "array":
            items = spec.get("items")
            return f"array<{type_sig(doc, items, depth + 1) if items else 'any'}>"
        return t
    return "(untyped)"


# ---------------------------------------------------------------------------
# 规范化：抽出「工具面契约」的规范形式（判据口径 ①）
# ---------------------------------------------------------------------------
def body_schema(doc: dict, op: dict) -> dict:
    rb = op.get("requestBody") or {}
    content = rb.get("content") or {}
    for mt in ("application/json", "application/*+json"):
        if mt in content:
            return (content[mt] or {}).get("schema") or {}
    return {}


def params_of(doc: dict, op: dict) -> list:
    """入参：名 / 必填性 / 类型。合并 requestBody 与 parameters。"""
    out = []
    sch = body_schema(doc, op)
    # 解 $ref / anyOf 包裹，取全部可解析分支的 properties 并集
    branches = []
    stack = [sch]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if "$ref" in node:
            stack.append(resolve_ref(doc, node))
            continue
        if isinstance(node.get("anyOf"), list):
            stack.extend(node["anyOf"])
            continue
        if isinstance(node.get("allOf"), list):
            stack.extend(node["allOf"])
            continue
        branches.append(node)
    for br in branches:
        props = br.get("properties") or {}
        required = set(br.get("required") or [])
        for name, spec in props.items():
            out.append({
                "name": name,
                "required": name in required,
                "type": type_sig(doc, spec),
                "in": "body",
            })
    for par in op.get("parameters") or []:
        if not isinstance(par, dict):
            continue
        out.append({
            "name": par.get("name"),
            "required": bool(par.get("required")),
            "type": type_sig(doc, par.get("schema") or {}),
            "in": par.get("in"),
        })
    return sorted(out, key=lambda x: (str(x["in"]), str(x["name"])))


def response_of(doc: dict, op: dict) -> tuple:
    """出参：已声明的字段名/类型 + 声明形态。返回 (fields, form)。"""
    resp = op.get("responses") or {}
    chosen = None
    for code in sorted(resp):
        if str(code).startswith("2"):
            chosen = resp[code]
            break
    if not isinstance(chosen, dict):
        return [], "no-2xx-response"
    content = chosen.get("content") or {}
    if not content:
        return [], "no-content"
    mt = "application/json" if "application/json" in content else sorted(content)[0]
    sch = (content.get(mt) or {}).get("schema")
    if sch is None:
        return [], "no-schema"
    if not isinstance(sch, dict) or not sch:
        return [], "empty-schema{}"
    # 解引用
    node = sch
    depth = 0
    while isinstance(node, dict) and "$ref" in node and depth <= 8:
        node = resolve_ref(doc, node)
        depth += 1
    props = (node or {}).get("properties") or {}
    fields = [{"name": n, "type": type_sig(doc, s)} for n, s in props.items()]
    fields.sort(key=lambda x: str(x["name"]))
    if fields:
        form = "object-with-declared-properties"
    elif node.get("type") == "object":
        form = f"freeform-object(additionalProperties={node.get('additionalProperties')})"
    else:
        form = f"declared({json.dumps(node, sort_keys=True, ensure_ascii=False)[:80]})"
    return fields, form


def errors_of(op: dict) -> list:
    """错误语义的机读可判定部分：非 2xx 响应码集合。"""
    return sorted(str(c) for c in (op.get("responses") or {}) if not str(c).startswith("2"))


def canon_openapi(doc: dict) -> dict:
    tools = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "head", "options"):
                continue
            if not isinstance(op, dict):
                continue
            fields, form = response_of(doc, op)
            tools[f"{method.upper()} {path}"] = {
                "path": path,
                "method": method.upper(),
                "params": params_of(doc, op),
                "response_fields": fields,
                "response_form": form,
                "errors": errors_of(op),
            }
    return {"kind": "openapi", "arms": None, "tools": tools}


def canon_manifest(doc: dict) -> dict:
    tools = {}
    for t in doc.get("tools") or []:
        if not isinstance(t, dict):
            continue
        params = [
            {
                "name": p.get("name"),
                "required": bool(p.get("required")),
                "type": str(p.get("type")),
                "in": "body",
            }
            for p in (t.get("params") or [])
        ]
        params.sort(key=lambda x: str(x["name"]))
        rets = [{"name": r.get("name"), "type": str(r.get("type"))} for r in (t.get("returns") or [])]
        rets.sort(key=lambda x: str(x["name"]))
        tools[str(t.get("name"))] = {
            "path": str(t.get("name")),
            "method": "TOOL",
            "params": params,
            "response_fields": rets,
            "response_form": "manifest-declared",
            "errors": [],
            "description": t.get("description"),
        }
    return {"kind": "manifest", "arms": (doc.get("carrier"), doc.get("arm")), "tools": tools}


# ---------------------------------------------------------------------------
# 逐键比对
# ---------------------------------------------------------------------------
def _fmt(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def compare(can_l: dict, can_r: dict) -> tuple:
    """返回 (gating_findings, residual_findings)。

    gating  = 本任务枚举的可判定维度（含 manifest 模式的 description）
    residual= 该口径之外、但**确实存在**的非 URL/非元数据差异（response_form）
    """
    gating, residual = [], []
    tl, tr = can_l["tools"], can_r["tools"]

    for name in sorted(set(tl) - set(tr)):
        gating.append(f"仅左侧有: {name}")
    for name in sorted(set(tr) - set(tl)):
        gating.append(f"仅右侧有: {name}")

    for name in sorted(set(tl) & set(tr)):
        a, b = tl[name], tr[name]
        for key, label in (
            ("path", "path"),
            ("method", "method"),
            ("params", "入参名/必填性/类型"),
            ("response_fields", "响应 schema 字段名与类型"),
            ("errors", "错误码集合"),
            ("description", "工具描述"),
        ):
            if key not in a and key not in b:
                continue
            if a.get(key) != b.get(key):
                gating.append(f"{name} · {label} 不同:\n        L={_fmt(a.get(key))}\n        R={_fmt(b.get(key))}")
        if a.get("response_form") != b.get("response_form"):
            residual.append(
                f"{name} · 响应 schema 声明形态不同（非 URL、非生成器元数据）:\n"
                f"        L={a.get('response_form')}\n        R={b.get('response_form')}"
            )
    return gating, residual


# ---------------------------------------------------------------------------
# strict：剥离 URL 与生成器元数据后逐字节比较
# ---------------------------------------------------------------------------
_STRIP_TOP = {"servers", "host", "basePath", "schemes"}
_STRIP_INFO = {"title", "version", "description", "contact", "license", "termsOfService"}
_STRIP_OP = {"summary", "description", "operationId", "tags", "externalDocs"}
_STRIP_ANYWHERE = {"title"}
_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}


def strip_openapi(node, parent_key: str = ""):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _STRIP_TOP or k in _STRIP_ANYWHERE or k.startswith("x-"):
                continue
            if parent_key == "info" and k in _STRIP_INFO:
                continue
            if isinstance(v, dict):
                out[k] = strip_openapi(v, k)
            elif isinstance(v, list):
                out[k] = [strip_openapi(x, k) for x in v]
            else:
                out[k] = v
        return out
    if isinstance(node, list):
        return [strip_openapi(x, parent_key) for x in node]
    return node


def strip_op_meta(doc: dict) -> dict:
    """剥掉每个 operation 的生成器元数据（summary/description/operationId/tags）。"""
    d = copy.deepcopy(doc)
    for path, item in (d.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() not in _METHODS or not isinstance(op, dict):
                continue
            for k in list(op):
                if k in _STRIP_OP:
                    op.pop(k, None)
    return d


def inline_refs(node, doc: dict, depth: int = 0):
    """把 $ref 就地展开为被引用的 schema（处理循环/深度上限）。

    这是 NORMALIZED_OPENAPI 里声明过的规范化维度：两侧请求体一个用 $ref、
    另一个内联 anyOf，只有展开后才是「同一语义的两种写法」。
    """
    if depth > 24:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and "/" in ref:
            name = ref.rsplit("/", 1)[-1]
            target = ((doc.get("components") or {}).get("schemas") or {}).get(name)
            if target is not None:
                merged = inline_refs(copy.deepcopy(target), doc, depth + 1)
                for k, v in node.items():
                    if k != "$ref":
                        merged.setdefault(k, inline_refs(v, doc, depth + 1))
                return merged
        return {k: inline_refs(v, doc, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [inline_refs(x, doc, depth + 1) for x in node]
    return node


def strict_blob(doc: dict, kind: str) -> bytes:
    if kind == "manifest":
        keep = {
            "schema": doc.get("schema"),
            "tools": [
                {
                    "name": t.get("name"),
                    "params": t.get("params"),
                    "returns": t.get("returns"),
                }
                for t in (doc.get("tools") or [])
            ],
        }
        return json.dumps(keep, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    norm = strip_openapi(strip_op_meta(inline_refs(copy.deepcopy(doc), doc)))
    # 引用已就地展开 ⇒ components.schemas 的命名/存在性不再承载语义，随之规范化
    norm.pop("components", None)
    return json.dumps(norm, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def walk_diffs(a, b, path="$"):
    if type(a) is not type(b):
        yield f"{path}: 类型不同 {type(a).__name__} vs {type(b).__name__}"
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                yield f"{path}.{k}: 仅右侧有"
            elif k not in b:
                yield f"{path}.{k}: 仅左侧有"
            else:
                yield from walk_diffs(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            yield f"{path}: 列表长度 {len(a)} vs {len(b)}"
            return
        for i, (x, y) in enumerate(zip(a, b)):
            yield from walk_diffs(x, y, f"{path}[{i}]")
    elif a != b:
        yield f"{path}: {a!r} vs {b!r}"


# ---------------------------------------------------------------------------
# 主判定
# ---------------------------------------------------------------------------
def evaluate(lhs: dict, rhs: dict, strict: bool) -> dict:
    kl, kr = kind_of(lhs), kind_of(rhs)
    if kl != kr:
        raise Blocked(f"两侧输入种类不同: 左侧={kl} 右侧={kr}（不可比）")
    if kl == "manifest":
        can_l, can_r = canon_manifest(lhs), canon_manifest(rhs)
    else:
        can_l, can_r = canon_openapi(lhs), canon_openapi(rhs)

    gating, residual = compare(can_l, can_r)

    strict_diffs = []
    if strict:
        bl, br = strict_blob(lhs, kl), strict_blob(rhs, kl)
        if bl != br:
            strict_diffs = list(walk_diffs(json.loads(bl), json.loads(br)))

    verdict = SAME
    reasons = []
    if gating:
        verdict = DIFF
        reasons.append(f"枚举维度差异 {len(gating)} 条")
    if strict and strict_diffs:
        verdict = DIFF
        reasons.append(f"strict 残差 {len(strict_diffs)} 条")
    return {
        "kind": kl,
        "mode": "strict" if strict else "contract-only",
        "arms": (can_l.get("arms"), can_r.get("arms")),
        "verdict": verdict,
        "reasons": reasons,
        "gating_findings": gating,
        "strict_diffs": strict_diffs,
        "response_form_residuals": residual,
        "tools_left": sorted(can_l["tools"]),
        "tools_right": sorted(can_r["tools"]),
        "n_tools_left": len(can_l["tools"]),
        "n_tools_right": len(can_r["tools"]),
        "canon_left": can_l["tools"],
        "canon_right": can_r["tools"],
    }


def check_pair(pair: str, res: dict) -> None:
    """--pair 与输入身份必须相符，否则 BLOCKED。"""
    if not pair:
        return
    try:
        l_arm, r_arm = pair.split(":", 1)
    except ValueError:
        raise Blocked(f"--pair 格式应为 LHS:RHS，收到 {pair!r}")
    if (l_arm, r_arm) not in PAIRS:
        raise Blocked(f"未知对照对 {pair!r}；允许 {['%s:%s' % p for p in PAIRS]}")
    arms = res.get("arms")
    if arms and arms[0] and arms[1]:
        def _carrier(ident):
            if isinstance(ident, (tuple, list)) and len(ident) >= 1:
                return ident[0]
            return None

        def _arm(ident):
            if isinstance(ident, (tuple, list)) and len(ident) >= 2:
                return ident[1]
            return None

        exp_l_car = "kg" if l_arm.startswith("KG") else "onto"
        exp_r_car = "onto" if r_arm.startswith("ONTO") else "kg"
        if _carrier(arms[0]) and _carrier(arms[0]) != exp_l_car:
            raise Blocked(f"--pair {pair} 与左侧载体不符: 期望 {exp_l_car!r}，输入 {_carrier(arms[0])!r}（{arms[0]!r}）")
        if _carrier(arms[1]) and _carrier(arms[1]) != exp_r_car:
            raise Blocked(f"--pair {pair} 与右侧载体不符: 期望 {exp_r_car!r}，输入 {_carrier(arms[1])!r}（{arms[1]!r}）")
        if _arm(arms[0]) and _arm(arms[0]) != l_arm:
            raise Blocked(f"--pair {pair} 与左侧输入身份不符: 输入 arm={arms[0]!r}")
        if _arm(arms[1]) and _arm(arms[1]) != r_arm:
            raise Blocked(f"--pair {pair} 与右侧输入身份不符: 输入 arm={arms[1]!r}")


def render(res: dict, facts_l: dict, facts_r: dict, pair: str, strict: bool) -> None:
    print("=" * 78)
    print("M3.3 工具面差异门禁 | toolface_diff.py")
    print("=" * 78)
    print("[输入]")
    print(f"  左侧 : {facts_l['path']}")
    print(f"         bytes={facts_l['bytes']} lf={facts_l['lf']} sha256={facts_l['sha256']}")
    print(f"  右侧 : {facts_r['path']}")
    print(f"         bytes={facts_r['bytes']} lf={facts_r['lf']} sha256={facts_r['sha256']}")
    print(f"  种类 : {res['kind']}    口径: {res['mode']}" + (f"    对照对: {pair}" if pair else ""))
    if res["arms"][0] or res["arms"][1]:
        print(f"  两侧身份: 左={res['arms'][0]} 右={res['arms'][1]}")

    print()
    print("[被规范化的维度]（显式列出，禁止隐式忽略）")
    for line in (NORMALIZED_MANIFEST if res["kind"] == "manifest" else NORMALIZED_OPENAPI):
        print(f"  - {line}")

    print()
    print(f"[工具面] 左 {res['n_tools_left']} 项 / 右 {res['n_tools_right']} 项")
    for name in res["tools_left"]:
        mark = "" if name in res["tools_right"] else "   ← 仅左侧"
        print(f"  L {name}{mark}")
    for name in res["tools_right"]:
        if name not in res["tools_left"]:
            print(f"  R {name}   ← 仅右侧")

    print()
    print("[逐键比对 · 枚举维度]")
    if res["gating_findings"]:
        for f in res["gating_findings"]:
            print(f"  DIFF {f}")
    else:
        print("  无差异（path / method / 入参名·必填性·类型 / 响应字段名·类型 / 错误码）")

    if res["response_form_residuals"]:
        print()
        print("[残差 · 非 URL、非生成器元数据]（枚举维度未覆盖，但确实不同）")
        for f in res["response_form_residuals"]:
            print(f"  RESIDUAL {f}")

    if strict:
        print()
        print("[strict · 剥离 URL 与生成器元数据后逐字节]")
        if res["strict_diffs"]:
            print(f"  仍非逐字节相同，残差 {len(res['strict_diffs'])} 条：")
            for d in res["strict_diffs"][:40]:
                print(f"    {d}")
            if len(res["strict_diffs"]) > 40:
                print(f"    ... 另 {len(res['strict_diffs']) - 40} 条")
        else:
            print("  逐字节相同")

    print()
    print("-" * 78)
    print(f"RESULT: {res['verdict']}" + (f"   （{'；'.join(res['reasons'])}）" if res["reasons"] else ""))
    print("-" * 78)


# ---------------------------------------------------------------------------
# 负控自检
# ---------------------------------------------------------------------------
# R-30：`/health` **运行时键面**核对（`--health` 模式）
#
#   ★ 本模式是**新增**的：既有 strict / --contract-only 的规范化规则与比较逻辑
#     一个字符都没动（R-20）。存在的理由（实证）：R-27 前后两侧 /openapi.json
#     **逐字节未变**（f6e20fed… / 92b64b04…），而 /health 的实际键面已变
#     （ONTO 新增 auditChain / auditFile / queryLatency）⇒ OpenAPI 平面对 /health
#     **不敏感**，键面分歧在既有门禁下不可见、无法强制登记。故单列一个运行时口径：
#       * 采集到的键 **未登记** ⇒ ERROR（多键）
#       * 声明里的键 **缺失**   ⇒ ERROR（不是 WARN）
#       * 全部相符 ⇒ SAME / exit 0
# ---------------------------------------------------------------------------
HEALTH_FACE_SCHEMA = "dsh-health-face/1"
DEFAULT_HEALTH_FACE = Path(__file__).resolve().parent.parent / "model" / "health-face.yaml"
DEFAULT_BASE_KG = "http://127.0.0.1:8301"  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
DEFAULT_BASE_ONTO = "http://127.0.0.1:8302"  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
HEALTH_TIMEOUT_S = 8.0
HEALTH_CARRIERS = ("kg", "onto")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_health_face(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise Blocked(f"health 声明不存在: {path}")
    if not p.is_file():
        raise Blocked(f"health 声明不是常规文件: {path}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise Blocked(f"health 声明读取失败: {path}: {e}") from e
    try:
        import yaml
    except ImportError as e:  # pragma: no cover
        raise Blocked(f"health 声明需要 PyYAML（未安装：{e}）") from e
    try:
        doc = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"health 声明不是合法 YAML: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise Blocked(f"health 声明顶层不是映射: {path}")
    if doc.get("schema") != HEALTH_FACE_SCHEMA:
        raise Blocked(f"health 声明 schema 必须是 {HEALTH_FACE_SCHEMA}，实得 {doc.get('schema')!r}")
    sides = doc.get("sides")
    if not isinstance(sides, list) or not sides:
        raise Blocked("health 声明缺少非空 sides 列表")
    for side in sides:
        if not isinstance(side, dict) or not side.get("carrier"):
            raise Blocked(f"health 声明 sides[] 元素必须含 carrier: {side!r}")
        if not isinstance(side.get("keys"), list) or not side["keys"]:
            raise Blocked(f"health 声明 sides[{side.get('carrier')}].keys 必须是非空列表")
    return doc


def declared_keys_of(side: dict) -> list:
    keys = []
    for item in side.get("keys") or []:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict) and item.get("name"):
            keys.append(str(item["name"]))
        else:
            raise Blocked(f"keys 元素必须是字符串或含 name 的映射: {item!r}")
    if len(set(keys)) != len(keys):
        raise Blocked(f"keys 有重复项: {side.get('carrier')}")
    return keys


def health_roles(face: dict) -> dict:
    """从声明里取出主对比两侧（carrier=kg / carrier=onto 且 inMainComparison 为真）。

    :8303（KG-T 臂）等在声明里标 `inMainComparison: false`、只登记不参与本口径。
    """
    roles = {}
    for side in face["sides"]:
        carrier = str(side["carrier"])
        if not bool(side.get("inMainComparison", True)):
            continue
        if carrier not in HEALTH_CARRIERS:
            raise Blocked(f"主对比侧只能声明为 {HEALTH_CARRIERS}，实得 {carrier!r}")
        if carrier in roles:
            raise Blocked(f"health 声明里 {carrier} 侧重复（主对比内只允许一条）")
        roles[carrier] = side
    for carrier in HEALTH_CARRIERS:
        if carrier not in roles:
            raise Blocked(f"health 声明缺主对比侧: {carrier}")
    return roles


def fetch_health(base_url: str) -> dict:
    import urllib.request

    url = str(base_url).rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as resp:
            status = getattr(resp, "status", 0)
            raw = resp.read()
    except Exception as e:  # noqa: BLE001
        raise Blocked(f"GET {url} 失败: {type(e).__name__}: {e}") from e
    if status != 200:
        raise Blocked(f"GET {url} 返回 HTTP {status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Blocked(f"{url} 不是合法 UTF-8 JSON: {e}") from e
    if not isinstance(payload, dict):
        raise Blocked(f"{url} 顶层不是 JSON 对象")
    return {"url": url, "bytes": len(raw), "payload": payload, "source": "live"}


def load_health_snapshot(path: str) -> dict:
    payload = load_doc(path)  # 复用同一条 UTF-8 JSON 装载/报错路径
    try:
        size = Path(path).stat().st_size
    except OSError:  # pragma: no cover
        size = 0
    return {"url": "file://" + str(Path(path)), "bytes": size, "payload": payload, "source": "file"}


def _declared_field(fields: set, name: str) -> bool:
    if name.endswith("*"):
        return any(f.startswith(name[:-1]) for f in fields)
    return name in fields


def check_audit_records(carrier: str, path: str, core: list, declared: list) -> dict:
    """核对一侧审计 JSONL：**每一行**都必须含最小公共核；出现过的字段都必须已登记。"""
    p = Path(path)
    if not p.exists():
        raise Blocked(f"审计文件不存在: {path}")
    if not p.is_file():
        raise Blocked(f"审计文件不是常规文件: {path}")
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise Blocked(f"审计文件读取失败: {path}: {e}") from e
    raw_lines = [ln for ln in lines if ln.strip()]
    findings, samples, observed = [], [], set()
    for idx, line in enumerate(raw_lines, start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            findings.append(f"ERROR {carrier} 审计第 {idx} 行不是合法 JSON: {e}")
            continue
        if not isinstance(rec, dict):
            findings.append(f"ERROR {carrier} 审计第 {idx} 行不是 JSON 对象")
            continue
        fields = set(rec)
        observed |= fields
        missing = [name for name in core if not _declared_field(fields, name)]
        if missing:
            findings.append(f"ERROR {carrier} 审计第 {idx} 行缺最小公共核字段: {missing}")
        samples.append({
            "line": idx,
            "requestId": rec.get("requestId"),
            "startedAt": rec.get("startedAt"),
            "status": rec.get("status"),
        })
    unregistered = sorted(f for f in observed if not _declared_field(set(declared), f))
    if unregistered:
        findings.append(f"ERROR {carrier} 审计出现未登记字段: {unregistered}")
    return {
        "carrier": carrier,
        "path": str(p),
        "lines": len(raw_lines),
        "observedFields": sorted(observed),
        "unregistered": unregistered,
        "samples": samples[:5],
        "sampleCount": len(samples),
        "coreRequired": list(core),
        "findings": findings,
    }


def evaluate_health(face: dict, snapshots: dict, pids: dict, audited: dict) -> dict:
    findings, reasons, sides_out, observed_by, declared_by = [], [], {}, {}, {}
    for carrier in HEALTH_CARRIERS:
        side = health_roles(face)[carrier]
        declared = declared_keys_of(side)
        declared_by[carrier] = set(declared)
        snap = snapshots.get(carrier)
        if snap is None:  # pragma: no cover - run_health 总是给齐两侧
            sides_out[carrier] = {"carrier": carrier, "declaredKeys": declared, "checked": False}
            continue
        payload = snap["payload"]
        actual = payload.get("carrier")
        if actual != carrier:
            findings.append(f"ERROR {carrier} 载荷自身 carrier={actual!r} 与所在侧不符（可能是取错了实例）")
        observed = set(payload)
        observed_by[carrier] = observed
        unregistered = sorted(observed - set(declared))
        missing = sorted(set(declared) - observed)
        if unregistered:
            findings.append(f"ERROR {carrier} /health 未登记键（多键）: {unregistered}")
        if missing:
            findings.append(f"ERROR {carrier} /health 声明键缺失: {missing}")
        sides_out[carrier] = {
            "carrier": carrier,
            "arm": side.get("arm"),
            "port": side.get("port"),
            "baseUrl": side.get("baseUrl"),
            "url": snap["url"],
            "source": snap["source"],
            "httpBytes": snap["bytes"],
            "declaredKeys": declared,
            "observedKeys": sorted(observed),
            "unregistered": unregistered,
            "missing": missing,
            "inMainComparison": True,
            "pid": pids.get(carrier),
            "checked": True,
        }
    # 登记表自身完整性：登记项必须是该侧声明过的键
    specific = {}
    for item in face.get("carrierSpecific") or []:
        if not isinstance(item, dict) or not item.get("carrier") or not item.get("key"):
            raise Blocked(f"carrierSpecific[] 元素必须含 carrier/key: {item!r}")
        carrier, key = str(item["carrier"]), str(item["key"])
        specific.setdefault(carrier, set()).add(key)
        if carrier in declared_by and key not in declared_by[carrier]:
            findings.append(f"ERROR 登记表项 {carrier}.{key} 不在该侧声明键集合内（登记漂移）")
    # 两侧**只在单侧出现**的键必须逐条登记为载体特有键
    if len(observed_by) == 2:
        only_kg = sorted(observed_by["kg"] - observed_by["onto"])
        only_onto = sorted(observed_by["onto"] - observed_by["kg"])
        for carrier, keys in (("kg", only_kg), ("onto", only_onto)):
            for key in keys:
                if key not in specific.get(carrier, set()):
                    findings.append(f"ERROR 载体特有键未登记: {carrier}.{key}")
    # 审计记录：最小公共核 + 字段登记
    audit_declared = {}
    for item in ((face.get("audit") or {}).get("carrierSpecificFields") or []):
        if not isinstance(item, dict) or not item.get("carrier") or not item.get("field"):
            raise Blocked(f"audit.carrierSpecificFields[] 元素必须含 carrier/field: {item!r}")
        audit_declared.setdefault(str(item["carrier"]), []).append(str(item["field"]))
    core_bindings = {}
    for item in ((face.get("audit") or {}).get("coreBindings") or []):
        if not isinstance(item, dict) or not item.get("canonical"):
            raise Blocked(f"audit.coreBindings[] 元素必须含 canonical: {item!r}")
        for carrier in HEALTH_CARRIERS:
            for name in (item.get(carrier) or []):
                core_bindings.setdefault(carrier, []).append(str(name))
    audit_out = {}
    for carrier, entry in audited.items():
        declared_fields = list(core_bindings.get(carrier, [])) + list(audit_declared.get(carrier, []))
        checked = check_audit_records(carrier, entry, core_bindings.get(carrier, []), declared_fields)
        audit_out[carrier] = checked
        findings.extend(checked["findings"])
    verdict = SAME if not findings else DIFF
    if findings:
        reasons.append(f"键面/登记 ERROR {len(findings)} 条")
    return {
        "mode": "health",
        "schema": face.get("schema"),
        "declaredCollectedAt": face.get("collectedAt"),
        "collectedAt": now_iso(),
        "verdict": verdict,
        "reasons": reasons,
        "findings": findings,
        "sides": sides_out,
        "audit": audit_out,
        "pids": pids,
    }


def render_health(res: dict, face_path: str) -> None:
    print("=" * 78)
    print("R-30 /health 运行时键面核对 | toolface_diff.py --health")
    print("=" * 78)
    print("[声明]")
    print(f"  {face_path}")
    print(f"  schema={res.get('schema')}  声明采集时点={res.get('declaredCollectedAt')}")
    print(f"  本次采集时点={res.get('collectedAt')}（本机 UTC 偏移）")
    for carrier in HEALTH_CARRIERS:
        side = res["sides"].get(carrier) or {}
        print(f"[核对 {carrier}] {side.get('url')}  来源={side.get('source')} "
              f"arm={side.get('arm')} port={side.get('port')} pid={side.get('pid')}")
        if not side.get("checked"):
            print("  未采集")
            continue
        print(f"  httpBytes={side.get('httpBytes')} 键数={len(side.get('observedKeys') or [])}")
        print(f"  observed = {json.dumps(side.get('observedKeys'), ensure_ascii=False)}")
        print(f"  declared = {json.dumps(side.get('declaredKeys'), ensure_ascii=False)}")
    for carrier, entry in (res.get("audit") or {}).items():
        print(f"[审计 {carrier}] {entry['path']} 行数={entry['lines']} 抽样={entry['sampleCount']}")
        print(f"  最小公共核(该侧) = {json.dumps(entry['coreRequired'], ensure_ascii=False)}")
        print(f"  实测字段 = {json.dumps(entry['observedFields'], ensure_ascii=False)}")
    print("-" * 78)
    if res["findings"]:
        for item in res["findings"]:
            print("  " + item)
    else:
        print("  ERROR 0 条：已登记范围内逐键相符（★ 只证**已登记范围**一致，不证两载体可互换）")
    print(f"RESULT: {res['verdict']}"
          + (f"   （{'；'.join(res['reasons'])}）" if res["reasons"] else ""))
    print("-" * 78)


def run_health(args) -> int:
    try:
        face = load_health_face(args.health_face)
        roles = health_roles(face)
        snapshots, pids, audited = {}, {}, {}
        for carrier in HEALTH_CARRIERS:
            override = getattr(args, "health_%s" % carrier)
            if override:
                snapshots[carrier] = load_health_snapshot(override)
            else:
                base = getattr(args, "base_%s" % carrier) or roles[carrier].get("baseUrl")
                if not base:
                    base = DEFAULT_BASE_KG if carrier == "kg" else DEFAULT_BASE_ONTO
                snapshots[carrier] = fetch_health(base)
            pids[carrier] = getattr(args, "pid_%s" % carrier)
            audit_path = getattr(args, "audit_%s" % carrier)
            if audit_path:
                audited[carrier] = audit_path
        res = evaluate_health(face, snapshots, pids, audited)
    except Blocked as e:
        if args.json:
            print(json.dumps({"mode": "health", "verdict": BLOCKED, "reason": str(e)},
                             ensure_ascii=False, indent=2))
        else:
            print("=" * 78)
            print(f"RESULT: BLOCKED   {e}")
            print("（BLOCKED ≠ FAIL：取不到/读不了/解析不了输入或声明，不是判定为不一致）")
            print("=" * 78)
        return EXIT_BLOCKED

    if args.json:
        out = dict(res)
        out["face"] = str(args.health_face)
        out["exit"] = EXIT_SAME if res["verdict"] == SAME else EXIT_DIFF
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        render_health(res, args.health_face)
    return EXIT_SAME if res["verdict"] == SAME else EXIT_DIFF


# ---------------------------------------------------------------------------
def _fixture() -> dict:
    def op(summary, oid, props, required, rprops):
        return {
            "summary": summary,
            "operationId": oid,
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": props,
                            "required": required,
                        }
                    }
                }
            },
            "responses": {
                "200": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": rprops}
                        }
                    }
                },
                "422": {"description": "Validation Error"},
            },
        }

    return {
        "openapi": "3.1.0",
        "info": {"title": "Fixture KG", "version": "1.0.0", "description": "kg"},
        "servers": [{"url": "http://127.0.0.1:8301"}],
        "paths": {
            "/query": {
                "post": op(
                    "cc_query", "cc_query_query_post",
                    {"query": {"type": "string"}}, ["query"],
                    {"rows": {"type": "array"}, "elapsedS": {"type": "number"}},
                )
            },
            "/guide": {
                "post": op(
                    "cc_guide", "cc_guide_guide_post",
                    {}, [],
                    {"metrics": {"type": "array"}, "hint": {"type": "string"}},
                )
            },
        },
        "components": {"schemas": {}},
    }


def _reorder(node):
    """按键名逆序重建，用于制造「仅键序不同」。"""
    if isinstance(node, dict):
        return {k: _reorder(node[k]) for k in sorted(node, reverse=True)}
    if isinstance(node, list):
        return [_reorder(x) for x in node]
    return node


def selftest() -> int:
    print("=" * 78)
    print("toolface_diff.py --selftest · 负控（判别力）自检")
    print("=" * 78)
    base = _fixture()
    results = []

    def run(label, lhs, rhs, expected, strict=True):
        try:
            res = evaluate(lhs, rhs, strict=strict)
            actual = res["verdict"]
            extra = ""
            if actual == DIFF:
                extra = f"  [gating={len(res['gating_findings'])} strict={len(res['strict_diffs'])}]"
            ok = actual == expected
            results.append(ok)
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            print(f"        期望={expected}  实际={actual}{extra}")
            return res
        except Blocked as e:
            results.append(False)
            print(f"  FAIL  {label}")
            print(f"        期望={expected}  实际=BLOCKED ({e})")
            return None

    # 0 正控：完全相同的输入
    run("正控 0：两份文档完全相同", copy.deepcopy(base), copy.deepcopy(base), SAME)

    # 1 仅键序 / 空白差异
    c1a = _reorder(json.loads(json.dumps(base, indent=4)))
    c1b = json.loads(json.dumps(base, sort_keys=True, separators=(" , ", " : "), indent=2))
    run("负控 1：仅键序 / 空白差异（无实质变化）", c1a, c1b, SAME)

    # 2 仅 URL / port 差异（附必然不同的 info 元数据）
    c2 = copy.deepcopy(base)
    c2["servers"] = [{"url": "http://192.0.2.1:9999"}]
    c2["info"] = {"title": "Fixture ONTO", "version": "9.9.9", "description": "onto"}
    run("负控 2：仅 URL / port 差异（+ info 元数据必然不同）", copy.deepcopy(base), c2, SAME)

    # 3 参数名差一个字符
    c3 = copy.deepcopy(base)
    c3["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"] = {
        "querx": {"type": "string"}
    }
    c3["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["required"] = ["querx"]
    run("负控 3：参数名差一个字符（query -> querx）", copy.deepcopy(base), c3, DIFF)

    # 4 响应字段类型差异
    c4 = copy.deepcopy(base)
    c4["paths"]["/query"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["rows"] = {
        "type": "string"
    }
    run("负控 4：响应字段类型差异（rows array -> string）", copy.deepcopy(base), c4, DIFF)

    # 5 缺失一个工具
    c5 = copy.deepcopy(base)
    del c5["paths"]["/guide"]
    run("负控 5：缺失一个工具（删掉 /guide）", copy.deepcopy(base), c5, DIFF)

    # 6 正控：$ref 表达 vs 内联表达（解引用后应 SAME）
    c6 = copy.deepcopy(base)
    c6["components"]["schemas"]["QueryIn"] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    c6["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/QueryIn"
    }
    run("正控 6：$ref 表达 vs 内联表达（解引用后应 SAME）", copy.deepcopy(base), c6, SAME)

    # 7 BLOCKED 语义：两侧输入种类不同
    print()
    print("  -- BLOCKED ≠ FAIL 语义检查 --")
    try:
        evaluate({"schema": "dsh-toolface-manifest/1", "carrier": "kg", "arm": "KG-Q", "tools": []},
                 base, strict=True)
        print("  FAIL  BLOCKED 7：种类不同应 BLOCKED，实际未抛")
        results.append(False)
    except Blocked as e:
        print(f"  PASS  BLOCKED 7：种类不同 → BLOCKED（{e}）")
        results.append(True)

    # 8 BLOCKED 语义：不存在的输入文件（两条路径都要覆盖：
    #   file_facts 是 main() 最先调用的，load_doc 是随后的——只测一条会漏掉真实崩溃）
    for label, fn in (("file_facts", file_facts), ("load_doc", load_doc)):
        try:
            fn("tmp/__definitely_missing__.json")
            print(f"  FAIL  BLOCKED 8：{label} 对不存在输入应 BLOCKED，实际未抛")
            results.append(False)
        except Blocked as e:
            print(f"  PASS  BLOCKED 8：{label} 输入不存在 → BLOCKED（{e}）")
            results.append(True)

    # 9 BLOCKED 语义：--pair 与输入身份不符
    mf_l = {"schema": "dsh-toolface-manifest/1", "carrier": "kg", "arm": "KG-R", "tools": []}
    mf_r = {"schema": "dsh-toolface-manifest/1", "carrier": "onto", "arm": "ONTO-R", "tools": []}
    res9 = evaluate(mf_l, mf_r, strict=True)
    try:
        check_pair("KG-Q:ONTO-Q", res9)
        print("  FAIL  BLOCKED 9：--pair 与输入身份不符应 BLOCKED，实际未抛")
        results.append(False)
    except Blocked as e:
        print(f"  PASS  BLOCKED 9：--pair 与输入身份不符 → BLOCKED（{e}）")
        results.append(True)

    # 10 正控：--pair 与输入身份相符 ⇒ 不抛
    try:
        check_pair("KG-R:ONTO-R", res9)
        print("  PASS  正控 10：--pair 与输入身份相符 → 放行")
        results.append(True)
    except Blocked as e:
        print(f"  FAIL  正控 10：--pair 相符却 BLOCKED（{e}）")
        results.append(False)

    # 11 端到端：走真实 main() 入口，缺输入必须 RESULT: BLOCKED / exit 2（不得抛栈）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc11 = main(["--kg", "tmp/__missing_a__.json", "--onto", "tmp/__missing_b__.json"])
    out11 = buf.getvalue()
    if rc11 == EXIT_BLOCKED and "RESULT: BLOCKED" in out11:
        print(f"  PASS  端到端 11：main() 缺输入 → RESULT: BLOCKED / exit {rc11}")
        results.append(True)
    else:
        print(f"  FAIL  端到端 11：期望 BLOCKED/exit {EXIT_BLOCKED}，实得 rc={rc11} out={out11[:120]!r}")
        results.append(False)

    # 12–15 比较器判别力（R-30 验收 3）：4 条**结构类**人为扰动，都必须 DIFF
    c12 = copy.deepcopy(base)
    c12["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["extraParam"] = {
        "type": "string"
    }
    run("负控 12：增一个入参（extraParam）", copy.deepcopy(base), c12, DIFF)

    c13 = copy.deepcopy(base)
    c13["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["query"] = {
        "type": "integer"
    }
    run("负控 13：改一个参数类型（query: string -> integer）", copy.deepcopy(base), c13, DIFF)

    c14a = copy.deepcopy(base)
    c14a["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["limit"] = {
        "type": "integer"
    }
    c14b = copy.deepcopy(c14a)
    c14b["paths"]["/query"]["post"]["requestBody"]["content"]["application/json"]["schema"]["required"] = [
        "query", "limit"
    ]
    run("负控 14：改一个必填性（limit 可选 -> 必填）", c14a, c14b, DIFF)

    c15 = copy.deepcopy(base)
    c15["paths"]["/query"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["extraField"] = {
        "type": "string"
    }
    run("负控 15：增一个响应字段名（extraField）", copy.deepcopy(base), c15, DIFF)

    # 16–21 R-30 `--health` 模式（纯内存夹具；文件/联网两条路径由验收命令另行取证）
    print()
    print("  -- R-30 --health 运行时键面（正控 + 负控） --")
    face_fixture = {
        "schema": HEALTH_FACE_SCHEMA,
        "collectedAt": "2026-01-01T00:00:00+08:00",
        "sides": [
            {"carrier": "kg", "arm": "KG-Q", "port": 8301, "inMainComparison": True,
             "keys": ["status", "carrier"]},
            {"carrier": "onto", "arm": "ONTO-Q", "port": 8302, "inMainComparison": True,
             "keys": ["status", "carrier", "capability"]},
        ],
        "carrierSpecific": [{"carrier": "onto", "key": "capability", "meaning": "只读能力声明"}],
    }
    ok_kg = {"status": "ok", "carrier": "kg"}
    ok_onto = {"status": "ok", "carrier": "onto", "capability": "readonly"}

    def hrun(label, payloads, expected, face=None):
        snaps = {c: {"url": "fixture://%s/health" % c, "bytes": 0,
                     "payload": copy.deepcopy(payloads[c]), "source": "fixture"}
                 for c in HEALTH_CARRIERS}
        try:
            res = evaluate_health(copy.deepcopy(face or face_fixture), snaps, {}, {})
        except Blocked as e:
            results.append(False)
            print(f"  FAIL  {label}")
            print(f"        期望={expected}  实际=BLOCKED ({e})")
            return
        actual = res["verdict"]
        extra = f"  [ERROR={len(res['findings'])}]" if res["findings"] else ""
        good = actual == expected
        results.append(good)
        print(f"  {'PASS' if good else 'FAIL'}  {label}")
        print(f"        期望={expected}  实际={actual}{extra}")

    hrun("正控 16：声明与实际键面相符 ⇒ SAME", {"kg": ok_kg, "onto": ok_onto}, SAME)
    hrun("负控 17：出现表外多键 ⇒ DIFF（未登记）",
         {"kg": dict(ok_kg, extraKey=1), "onto": ok_onto}, DIFF)
    hrun("负控 18：声明键在载荷里缺失 ⇒ DIFF（不是 WARN）",
         {"kg": ok_kg, "onto": {"status": "ok", "carrier": "onto"}}, DIFF)
    face_19 = copy.deepcopy(face_fixture)
    face_19["sides"][1]["keys"] = ["status", "carrier", "capability", "repository"]
    hrun("负控 19：单侧独有键未登记为载体特有键 ⇒ DIFF",
         {"kg": ok_kg, "onto": dict(ok_onto, repository="cc-ops")}, DIFF, face=face_19)

    face_20 = copy.deepcopy(face_fixture)
    face_20["sides"][1]["carrier"] = "kg"
    face_21 = copy.deepcopy(face_fixture)
    face_21["sides"][0]["keys"] = ["status", "status"]
    for label, bad_face, fn in (
        ("BLOCKED 20：主对比侧重复（两条 kg）", face_20, health_roles),
        ("BLOCKED 21：keys 有重复项", face_21, lambda f: declared_keys_of(f["sides"][0])),
    ):
        try:
            fn(bad_face)
            print(f"  FAIL  {label}：应 BLOCKED，实际未抛")
            results.append(False)
        except Blocked as e:
            print(f"  PASS  {label} → BLOCKED（{e}）")
            results.append(True)

    print()
    print("-" * 78)
    passed = sum(1 for r in results if r)
    print(f"selftest: {passed}/{len(results)} PASS")
    print("-" * 78)
    return EXIT_SAME if passed == len(results) else EXIT_DIFF


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="M3.3 工具面差异门禁（同一对照对内部，规范化后逐键比对）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--kg", metavar="PATH", help="左侧门面（OpenAPI JSON 或 manifest JSON）")
    ap.add_argument("--onto", metavar="PATH", help="右侧门面（OpenAPI JSON 或 manifest JSON）")
    ap.add_argument("--pair", metavar="LHS:RHS", default=None,
                    help=f"只比对指定对照对，如 KG-Q:ONTO-Q；允许 {['%s:%s' % p for p in PAIRS]}")
    ap.add_argument("--contract-only", action="store_true",
                    help="只判枚举维度（放宽 strict）；★ 放宽是显式的，命令里看得见")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑负控自检并退出")
    ap.add_argument("--health", action="store_true",
                    help="R-30 新增模式：核对两侧**真实** /health 的运行时键面是否与"
                         " shared/model/health-face.yaml 声明一致（多键未登记 ⇒ ERROR、声明键缺失 ⇒ ERROR）")
    ap.add_argument("--health-face", metavar="PATH", default=str(DEFAULT_HEALTH_FACE),
                    help=f"/health 键面声明文件（默认 {DEFAULT_HEALTH_FACE}）")
    ap.add_argument("--health-kg", metavar="PATH",
                    help="左侧 /health 载荷文件（离线/负控用；给了就不联网）")
    ap.add_argument("--health-onto", metavar="PATH",
                    help="右侧 /health 载荷文件（离线/负控用；给了就不联网）")
    ap.add_argument("--base-kg", metavar="URL", default=None,
                    help=f"--health 联网采集的左侧基址（默认取声明 baseUrl，兜底 {DEFAULT_BASE_KG}）")
    ap.add_argument("--base-onto", metavar="URL", default=None,
                    help=f"--health 联网采集的右侧基址（默认取声明 baseUrl，兜底 {DEFAULT_BASE_ONTO}）")
    ap.add_argument("--pid-kg", metavar="N", type=int, default=None,
                    help="--health 读数里保留的左侧部署机 PID（/health 载荷不暴露 PID，由采集方从部署机读取）")
    ap.add_argument("--pid-onto", metavar="N", type=int, default=None,
                    help="--health 读数里保留的右侧部署机 PID（同上）")
    ap.add_argument("--audit-kg", metavar="PATH", default=None,
                    help="--health 附带核对的左侧审计 JSONL（最小公共核 + 字段登记）")
    ap.add_argument("--audit-onto", metavar="PATH", default=None,
                    help="--health 附带核对的右侧审计 JSONL（同上）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.health:
        if args.kg or args.onto:
            print("RESULT: BLOCKED   --health 与 --kg/--onto 互斥（两个模式口径不同，不得混用）")
            return EXIT_BLOCKED
        return run_health(args)

    if not args.kg or not args.onto:
        print("RESULT: BLOCKED   缺少输入：需要 --kg PATH 与 --onto PATH（或 --selftest）")
        return EXIT_BLOCKED

    strict = not args.contract_only
    try:
        facts_l = file_facts(args.kg)
        facts_r = file_facts(args.onto)
        lhs = load_doc(args.kg)
        rhs = load_doc(args.onto)
        res = evaluate(lhs, rhs, strict=strict)
        check_pair(args.pair, res)
    except Blocked as e:
        if args.json:
            print(json.dumps({"verdict": BLOCKED, "reason": str(e)}, ensure_ascii=False, indent=2))
        else:
            print("=" * 78)
            print(f"RESULT: BLOCKED   {e}")
            print("（BLOCKED ≠ FAIL：无法取得/校验输入，不是判定为不一致）")
            print("=" * 78)
        return EXIT_BLOCKED

    if args.json:
        out = {k: v for k, v in res.items() if not k.startswith("canon_")}
        out["inputs"] = {"left": facts_l, "right": facts_r, "pair": args.pair}
        out["exit"] = EXIT_SAME if res["verdict"] == SAME else EXIT_DIFF
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        render(res, facts_l, facts_r, args.pair, strict)

    return EXIT_SAME if res["verdict"] == SAME else EXIT_DIFF


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(EXIT_BLOCKED)
