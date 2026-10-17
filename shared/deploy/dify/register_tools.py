#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.2 / W1 · Dify API provider 注册与三样比对（声明 ↔ live 回读 ↔ 模型文件投影）。

用法
----
    python register_tools.py --check                       # 只读比对（默认动作）
    python register_tools.py --apply                       # 按需 add / update（绝不 delete）
    python register_tools.py --selftest                    # 离线自检，不需要 Dify、不需要网络
    python register_tools.py --snapshot-list OUT.json      # 抓 tool-providers?type=api 名单基线
    python register_tools.py --check --baseline IN.json    # 与基线比对：只允许新增

设计要点（对应任务书 §3 红线）
------------------------------
* 工具名 / 端口 / URL / 工具清单**只**来自 binding.yaml；本文件不内联任何业务常量。
* 白名单 fail-closed：不在 binding 声明的 provider、工具名，一律拒绝注册；
  :8303 的 9 个 denied 名字（含 denied_* operationId）显式拒绝。
* 只增不删：本文件**没有任何 delete / rename 路径**；已存在且内容一致 ⇒ 跳过写入。
* 文档性字段（carrier_specific_value_semantics 等）严禁进注册面 ⇒ 写前断言。
* 改声明不改判据：比对器是固定的；比对失败时唯一合法方向是改 binding/strip_fields。
 * neutral_tool_ref（D47）：中性取数工具 read_facts 的 params/returns 正文**只在**
   shared/model/live-facts-tools.yaml 里有一份；本文件按该文件的 resolution_contract
   解析引用（唯一实现 resolve_tool_entry），别名 read_live_facts 只换 name。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

BINDING_PATH = C.HERE / "binding.yaml"
PLACEHOLDER_BASE = "<<BASE>>"
#: ★ R-58：Dify 注册面主机的**唯一** env 覆盖键（已在 deploy/.env / .env.example 登记）。
#:   为什么需要：`binding.yaml#providers[].base` **不是**本进程要去连的地址，而是
#:   **注册进 Dify 的 `servers[].url` / `server_url`**（见本文件 `build_schema` 的
#:   `"servers": [{"url": provider["base"]}]` 与 `FakeTransport._entity` 的
#:   `"server_url": prov["base"] + path` 两处构造）——
#:   对它求值的是**容器里的 Dify**，所以这里绝不能落 `127.0.0.1`（那是 Dify 自己）。
#:   非空 ⇒ 把每个 api provider 的 base 主机换成它（端口不变）并同步 whitelist.allowed_hosts；
#:   未设 ⇒ 用 binding.yaml 里声明的现网主机。
PUBLIC_HOST_ENV = "CC_DIFY_PUBLIC_HOST"


class Fail(RuntimeError):
    pass


# ===========================================================================
# 1. 声明面：binding.yaml + 两个模型契约文件  ->  specs / schemas
# ===========================================================================
def apply_public_host(binding):
    """★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env 覆盖，未设则用声明值。

    `CC_DIFY_PUBLIC_HOST` 非空 ⇒ 逐条改写 api provider 的 `base` 主机（**端口不变**）并把
    `whitelist.allowed_hosts` 同步为它（保持 `assert_declaration_legal` 的主机白名单断言仍成立）。
    ★ 这里**不设** `127.0.0.1` 默认：base 是注册进 Dify 容器的地址，回环会让全部工具不可达。
    """
    host = (os.environ.get(PUBLIC_HOST_ENV) or "").strip()
    if not host:
        return binding
    for p in api_providers(binding):
        base = p.get("base") or ""
        if "//" not in base or ":" not in base.split("//", 1)[-1]:
            continue
        scheme, rest = base.split("//", 1)
        p["base"] = "%s//%s:%s" % (scheme, host, rest.rsplit(":", 1)[-1])
    wl = binding.get("whitelist") or {}
    if wl.get("allowed_hosts"):
        wl["allowed_hosts"] = [host]
    return binding


def load_declaration(binding_path: Path | str = BINDING_PATH):
    binding = C.load_binding(binding_path)
    apply_public_host(binding)      # ★ R-58：env 覆盖（未设 ⇒ 用 binding 声明的现网主机）
    srcs = {}
    for key, meta in binding["tool_sources"].items():
        p = C.REPO_ROOT / meta["path"]
        srcs[key] = C.load_binding(p)
    return binding, srcs


def verify_tool_sources(binding) -> list:
    """模型契约文件指纹核对：与 binding 登记不一致即报问题（不自动改登记）。"""
    problems = []
    for rel, meta in binding["meta"]["tool_source_fingerprints"].items():
        p = C.REPO_ROOT / rel
        if not p.is_file():
            problems.append("缺少模型契约文件 %s" % rel)
            continue
        f = C.file_facts(p)
        if f["bytes"] != meta["bytes"]:
            problems.append("%s 字节数 %s != 登记 %s" % (rel, f["bytes"], meta["bytes"]))
        if f["sha256"] != meta["sha256"]:
            problems.append("%s sha256 %s != 登记 %s" % (rel, f["sha256"], meta["sha256"]))
        if bool(f["bom"]) != bool(meta["bom"]):
            problems.append("%s BOM=%s != 登记 %s" % (rel, f["bom"], meta["bom"]))
        if f["lf_lines"] != meta["lf_lines"]:
            problems.append("%s LF 行数 %s != 登记 %s" % (rel, f["lf_lines"], meta["lf_lines"]))
    return problems


# ---------------------------------------------------------------------------
# 1b. neutral_tool_ref 引用解析（本仓库**唯一实现**）
#     权威协议：shared/model/live-facts-tools.yaml 的 `resolution_contract`
#     （键名 / 格式 <文件名>#<顶层键> / parsed_fields / 冲突与缺失纪律 / 别名只换 name）。
#     ★ 四组（read_facts 的 KG-Q·ONTO-Q·KG-R·ONTO-R·KG-T·KG-T-dyn 六臂 + 免费臂别名
#       read_live_facts）必须走**同一个** resolve_tool_entry；任何「顺手复制一份正文」
#       的分支都是本协议要消灭的对象（D47）。
# ---------------------------------------------------------------------------
MODEL_DIR = C.REPO_ROOT / "shared" / "model"
REF_KEY = "neutral_tool_ref"
BODY_KEYS = ("params", "returns")

# ★ S2c（窗口内留痕）：description 漂移判据 = **去掉所有空白后逐字相同**。
#   形状源 shared/model/live-facts-tools.yaml 的 read_facts.description 当时用了 YAML
#   折行（词间多一个空格），与 shared/model/tools-setb.yaml 的登记文本只差这个空格。
#   空白折叠不改变措辞，因此不构成 ERR_REF_DESCRIPTION_DRIFT 要抓的「改文案」；
#   但它**必须留痕**（每次解析打印一次 REF_WS_FOLD_NORMALIZED + 报告记录）。
#   ★ S3a 窗口收口：形状源已改单行逐字同源 ⇒ 真实产物不再产生该提示；
#   判据与留痕机制原样保留（合成夹具 N26–N29 仍逐条覆盖），不允许静默通过。
_WS_FOLD_SEEN = set()
_WS_FOLD_NOTES = []


def _norm_ws(text):
    return "".join(str(text or "").split())


def _note_ws_fold(name, ref, a, b):
    key = (name, ref)
    if key in _WS_FOLD_SEEN:
        return
    _WS_FOLD_SEEN.add(key)
    _WS_FOLD_NOTES.append((name, ref, a, b))
    print("REF_WS_FOLD_NORMALIZED [tool=%s] %s：登记与形状仅差空白折叠（词间空格），"
          "归一化后判定同源；请在形状源修正折行后复核。" % (name, ref))


REF_ERROR_TEXT = {
    "ERR_REF_FORMAT": "引用格式非法（应为 <文件名>#<顶层键>：同目录、无路径分隔符、.yaml/.yml 结尾）",
    "ERR_REF_MISSING_FILE": "引用指向的模型文件不存在",
    "ERR_REF_MISSING_KEY": "引用指向的顶层键在模型文件里不存在",
    "ERR_REF_CONFLICT": "同一条目既写引用又复制正文（params/returns）——D47 单一真源违规",
    "ERR_REF_SHAPE": "被引用的形状缺少必需字段（params / returns）",
    "ERR_REF_DESCRIPTION_DRIFT": "条目自带 description 与被引用形状的 description 不一致",
}


class RefError(Fail):
    """引用解析硬失败：错误码 + 工具名 + 细节（机检可判、报告可引）。"""

    def __init__(self, code, tool, detail):
        self.code = code
        self.tool = tool
        self.detail = detail
        super().__init__("%s [tool=%s] %s：%s" % (
            code, tool, REF_ERROR_TEXT.get(code, code), detail))


def entry_display_name(entry):
    return entry.get("name") or entry.get("tool_name")


def resolve_tool_entry(entry, model_dir=None, binding=None):
    """照 resolution_contract 解析一个工具条目；无引用 ⇒ 深拷贝原样返回。

    model_dir 仅供自检负控把 MODEL_DIR 指到临时目录；生产调用一律省略，
    与契约伪代码里的 MODEL_DIR（shared/model）同值。
    """
    ref = entry.get(REF_KEY)
    if ref is None:
        return copy.deepcopy(entry)
    name = entry_display_name(entry)
    ref = str(ref)
    if "#" not in ref:
        raise RefError("ERR_REF_FORMAT", name, repr(ref))
    fname, key = ref.split("#", 1)
    if "/" in fname or "\\" in fname or not fname.endswith((".yaml", ".yml")):
        raise RefError("ERR_REF_FORMAT", name, repr(ref))
    base = Path(model_dir) if model_dir else MODEL_DIR
    path = base / fname
    if not path.is_file():
        raise RefError("ERR_REF_MISSING_FILE", name, str(path))
    src = C.load_binding(path)
    if key not in src:
        raise RefError("ERR_REF_MISSING_KEY", name, "%s#%s" % (fname, key))
    shape = src[key]
    # ★ 冲突照**真值**判断：params: [] / returns: [] 是「未复制正文」，不算冲突。
    for k in BODY_KEYS:
        if entry.get(k):
            raise RefError("ERR_REF_CONFLICT", name, k)
    for k in ("params", "returns"):
        if k not in shape:
            raise RefError("ERR_REF_SHAPE", name, k)
    if entry.get("description") and shape.get("description"):
        a = str(entry["description"]).strip()
        b = str(shape["description"]).strip()
        if a != b:
            # ★ S2c：先去空白比一次（catch 计划内登记的折行）；仍不等 ⇒ 硬失败。
            #   即使去空白后相同，也必须**在 binding 里哈希钉住**才放行 ⇒ fail-closed。
            if _norm_ws(a) != _norm_ws(b) or not fold_exempt_ok(binding, ref, name, a, b):
                raise RefError("ERR_REF_DESCRIPTION_DRIFT", name,
                               "登记 %r ≠ 形状 %r" % (a[:40], b[:40]))
            _note_ws_fold(name, ref, a, b)
    out = copy.deepcopy(shape)
    if name is not None:
        out["name"] = name        # ★ 别名只换 name；params/returns/约束逐字同源
    out["returns"] = copy.deepcopy(shape["returns"])
    return out


def entry_transport(entry):
    """取 HTTP path/method：条目自带优先，否则取形状的 transport（D47 的新形态）。"""
    tr = entry.get("transport") or {}
    path = entry.get("path") or tr.get("path")
    method = entry.get("method") or tr.get("method")
    return path, (str(method).lower() if method else None)


def params_from_entry(entry) -> list:
    """把（可能已解析的）条目投影成注册面参数列表。

    支持三种形态、产出同一结构 —— 这是「四组形状同源」在投影层的落点：
      ① 形状映射：params 是 mapping(name→定义)，required 单列（live-facts-tools.yaml）
      ② setb 列表：params 是 list，逐项自带 name/type/required
      ③ kgt 原生：params 缺席，参数在 request.properties / request.required
    """
    raw = entry.get("params")
    if isinstance(raw, dict):
        req = entry.get("required") or []
        return [
            {
                "name": pname,
                "type": pdef["type"],
                "required": pname in req,
                "description": pdef.get("description"),
            }
            for pname, pdef in raw.items()
        ]
    if isinstance(raw, list):
        return [
            {
                "name": p["name"],
                "type": p["type"],
                "required": bool(p.get("required")),
                "description": p.get("description"),  # 缺 description 键 ⇒ None（模型面就是没有）
            }
            for p in raw
        ]
    req_src = entry.get("request") or {}
    props = req_src.get("properties") or {}
    req = req_src.get("required") or []
    return [
        {
            "name": k,
            "type": v["type"],
            "required": k in req,
            "description": v.get("description"),
        }
        for k, v in props.items()
    ]


def shape_entry(binding, src, out_name):
    """由**定义源自己的声明**合成「别名条目」，再交给同一个 resolve_tool_entry。

    单一真源：names.primary / names.free_alias / aliases.<别名>.of 全部读自形状权威文件，
    本函数不内联任何工具名常量。合成条目**不带 description** ⇒ 不触发
    ERR_REF_DESCRIPTION_DRIFT（别名只换 name，措辞仍逐字取自形状）。
    """
    names = src.get("names") or {}
    alias_spec = (src.get("aliases") or {}).get(out_name)
    if not isinstance(alias_spec, dict):
        raise Fail("形状权威文件未声明别名 %s（aliases.<名>.of）" % out_name)
    primary = alias_spec.get("of")
    if not primary or not isinstance(src.get(primary), dict):
        raise Fail("别名 %s 的 of=%r 在形状权威文件里没有对应顶层键" % (out_name, primary))
    if names.get("primary") and names["primary"] != primary:
        raise Fail("别名 %s 的 of=%s 与 names.primary=%s 不一致"
                   % (out_name, primary, names["primary"]))
    if names.get("free_alias") and names["free_alias"] != out_name:
        raise Fail("别名 %s 与 names.free_alias=%s 不一致" % (out_name, names.get("free_alias")))
    fname = Path(binding["tool_sources"]["livefacts"]["path"]).name
    return {"name": out_name, REF_KEY: "%s#%s" % (fname, primary)}


def spec_from_shape(binding, src, out_name) -> dict:
    """形状权威 → 注册面 spec（主名直接取条目；别名先合成引用条目，再走同一解析器）。"""
    names = src.get("names") or {}
    entry = src[out_name] if names.get("primary") == out_name else shape_entry(binding, src, out_name)
    r = resolve_tool_entry(entry, binding=binding)
    path, method = entry_transport(r)
    if not path or not method:
        raise Fail("工具 %s 没有 path/method（transport 缺失），不得进 API provider" % out_name)
    return {
        "name": out_name,
        "path": path,
        "method": method,
        "summary": r["description"],
        "description": r["description"],
        "params": params_from_entry(r),
    }


def project_setb(src, binding) -> list:
    out = []
    for key, t in src["tools"].items():
        r = resolve_tool_entry(t, binding=binding)
        name = r.get("name", key)
        path, method = entry_transport(r)
        if not path or not method:
            raise Fail("工具 %s 没有 path/method（transport 也没有），不得进 API provider" % name)
        # ★ FIX-1：真零参工具**不再**注入占位符（原 t["dify_placeholder"] 机制已删）。
        #   params 为空 ⇒ 注册面 properties={} / required=[]。真零参合法（现网 6 例）。
        out.append(
            {
                "name": name,
                "path": path,
                "method": method,
                "summary": r["description"],
                "description": r["description"],
                "params": params_from_entry(r),
            }
        )
    return out

def project_kgt(src, binding, names) -> list:
    by_name = {t["tool_name"]: t for t in src["tools"]}
    out = []
    for n in names:
        if n not in by_name:
            raise Fail("kgt-tools.yaml 中不存在工具 %s" % n)
        t = by_name[n]
        r = resolve_tool_entry(t, binding=binding)
        path, method = entry_transport(r)
        if not path or not method:
            raise Fail("工具 %s 没有 path/method，只能是插件工具，不得进 API provider" % n)
        summary = t.get("summary") or r.get("description")   # ★ kgt 的模型面描述取 summary
        out.append(
            {
                "name": n,
                "path": path,
                "method": method,
                "summary": summary,
                "description": summary,
                "params": params_from_entry(r),
            }
        )
    return out

def api_providers(binding) -> list:
    return [p for p in binding["providers"] if p.get("kind") == "api"]


# ★ U-N 裁定 = A（2026-10-08）：方案3 两个 provider 的 tools_from → (臂名, counts 键)。
#   为什么需要：`kgt` 与 `kgt-dyn` 的服务面不同（5 vs 6 工具，多的那个是 collect_now），
#   而原实现对 `tools_from=kgt` **写死**「臂=KG-T、counts 键=kgt_main_tools」
#   ⇒ 第二个 provider 必被误判。本表把「按臂解析」显式化；**判据本身不放松**。
KGT_TOOLS_FROM = {"kgt": ("KG-T", "kgt_main_tools"),
                  "kgt-dyn": ("KG-T-dyn", "kgt_dyn_tools")}


def specs_for(binding, srcs, provider) -> list:
    tf = provider["tools_from"]
    if tf == "setb":
        allspec = project_setb(srcs["setb"], binding)
    elif tf in KGT_TOOLS_FROM:      # ★ kgt / kgt-dyn 共用同一投影，子集由 provider["tools"] 决定
        allspec = project_kgt(srcs["kgt"], binding, provider["tools"])
    elif tf == "livefacts":
        # ★ D47：形状权威源。主名直接取该源条目；别名由 shape_entry 合成引用条目后
        #   走**同一个** resolve_tool_entry（别名只换 name；正文不复制）。
        allspec = [spec_from_shape(binding, srcs["livefacts"], n) for n in provider["tools"]]
    elif tf == "neutral":
        # ★ R-55：BASE 的**载体中性**数据面（cc-base-tools，:8304）。
        #   与 livefacts 共用**同一份形状权威**（live-facts-tools.yaml），区别只在
        #   暴露的是**主名** `read_facts` 而非免费臂别名 —— 因为 D8-c 把
        #   `tools_from=livefacts` 限定为「恰 1 个且只许别名」，而 BASE 不是免费臂。
        allspec = [spec_from_shape(binding, srcs["livefacts"], n) for n in provider["tools"]]
    else:
        raise Fail("未知 tools_from: %s" % tf)
    by = {s["name"]: s for s in allspec}
    out = []
    for n in provider["tools"]:
        if n not in by:
            raise Fail("provider %s 声明的工具 %s 在 %s 中不存在" % (provider["provider"], n, tf))
        out.append(by[n])
    return out

def build_schema(binding, provider, specs) -> dict:
    reg = binding["registration_face"]
    paths = {}
    for sp in specs:
        props, required = {}, []
        for p in sp["params"]:
            prop = {"type": p["type"]}
            if p.get("description") is not None:
                prop["description"] = p["description"]
            props[p["name"]] = prop
            if p["required"]:
                required.append(p["name"])
        # ★ FIX-1：真零参工具**不再**注入占位符 ⇒ properties={} / required=[]（合法，现网 6 例）。
        #   旧的 reg["empty_params_placeholder"] 注入分支已随 binding.yaml 的该键一并删除。
        paths[sp["path"]] = {
            reg["http_method"]: {
                "summary": sp["summary"],
                "description": sp["description"],   # ★ 模型面工具描述就在这里
                "operationId": sp["name"],
                "requestBody": {
                    "required": bool(required),
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": props, "required": required}
                        }
                    },
                },
                "responses": {"200": copy.deepcopy(reg["responses_200"])},
            }
        }
    return {
        "openapi": reg["openapi_version"],
        "info": {
            "title": reg["info_title"],                 # ★ 常量：保证两侧逐字相同
            "version": reg["schema_version"],
            "description": reg["info_description"],     # ★ 常量：保证两侧逐字相同
        },
        "servers": [{"url": provider["base"]}],
        "paths": paths,
    }


def neutral_tool_arms(binding, srcs) -> list:
    """D47/D48 声明驱动机检：同一中性操作在四组里的名字与落地。

    只**加**断言，不放宽任何既有判据：
      * 形状权威文件必须声明 names.primary / names.free_alias，且别名 of 指向主名；
      * 恰好 1 个 tools_from=livefacts 的 provider，且它只暴露**别名**（不得暴露主名）；
      * counts.livefacts_tools == 该 provider 的工具数；
      * setb/kgt 的 provider 只可暴露**主名**（不得暴露别名）；
      * 免费臂 App 必须带别名、不得带主名；其余 App 必须带主名、不得带别名。
    """
    problems = []
    src = srcs.get("livefacts")
    if not isinstance(src, dict):
        return ["声明面缺少形状权威源（binding.tool_sources.livefacts）"]
    names = src.get("names") or {}
    primary, free_alias = names.get("primary"), names.get("free_alias")
    if not primary or not free_alias or primary == free_alias:
        return ["形状权威文件未正确声明 names.primary / names.free_alias"]
    alias_of = ((src.get("aliases") or {}).get(free_alias) or {}).get("of")
    if alias_of != primary:
        problems.append("别名 %s 的 of=%r != names.primary=%s" % (free_alias, alias_of, primary))
    lf = [p for p in api_providers(binding) if p.get("tools_from") == "livefacts"]
    if len(lf) != 1:
        problems.append("tools_from=livefacts 的 provider 应恰为 1 个，实为 %d 个" % len(lf))
    for p in lf:
        tools = [str(t) for t in p["tools"]]
        if primary in tools:
            problems.append("免费臂 provider %s 暴露了主名 %s（D8-c：只可暴露别名）"
                            % (p["provider"], primary))
        if tools != [free_alias]:
            problems.append("provider %s 的工具面应恰为 [%s]，实为 %s"
                            % (p["provider"], free_alias, tools))
        if p.get("carrier") != "live-facts":
            problems.append("provider %s 的 carrier 应为 live-facts，实为 %r"
                            % (p["provider"], p.get("carrier")))
    want = (binding.get("counts") or {}).get("livefacts_tools")
    if want is None:
        problems.append("binding.counts 缺少 livefacts_tools")
    elif lf and want != len(lf[0]["tools"]):
        problems.append("counts.livefacts_tools=%s 与 livefacts provider 工具数 %s 不一致"
                        % (want, len(lf[0]["tools"])))
    for p in api_providers(binding):
        if p.get("tools_from") in ("setb", "kgt", "kgt-dyn", "neutral") \
                and free_alias in [str(t) for t in p["tools"]]:
            problems.append("provider %s（tools_from=%s）不得暴露免费臂别名 %s"
                            % (p["provider"], p.get("tools_from"), free_alias))
    for aname, cfg in (binding.get("apps") or {}).items():
        tools = [str(t) for t in (cfg.get("tools") or [])]
        if cfg.get("carrier") == "kgt-free":
            if primary in tools:
                problems.append("免费臂 App %s 的工具面含主名 %s" % (aname, primary))
            if free_alias not in tools:
                problems.append("免费臂 App %s 必须带别名 %s" % (aname, free_alias))
        else:
            if free_alias in tools:
                problems.append("App %s（carrier=%s）不得带免费臂别名 %s"
                                % (aname, cfg.get("carrier"), free_alias))
            if primary not in tools:
                problems.append("App %s（carrier=%s）必须带中性取数工具 %s"
                                % (aname, cfg.get("carrier"), primary))
    return problems


# ===========================================================================
# 2. 白名单 / 结构校验（fail-closed）
# ===========================================================================
def assert_declaration_legal(binding, srcs) -> list:
    """声明面合法性（fail-closed）。引用解析硬失败 ⇒ 记为该条问题（不外抛）。"""
    try:
        return _assert_declaration_legal(binding, srcs)
    except RefError as exc:
        return ["声明面引用解析硬失败（fail-closed，未继续其余规则）：%s" % exc]


def _assert_declaration_legal(binding, srcs) -> list:
    wl = binding["whitelist"]
    problems = []
    declared_providers = [p["provider"] for p in api_providers(binding)]
    for n in declared_providers:
        if n not in wl["provider_names"]:
            problems.append("provider %s 不在 whitelist.provider_names" % n)
    for p in api_providers(binding):
        if p["port"] not in wl["allowed_ports"]:
            problems.append("provider %s 端口 %s 不在白名单 %s" % (p["provider"], p["port"], wl["allowed_ports"]))
        host = p["base"].split("//", 1)[-1].rsplit(":", 1)[0]
        if host not in wl["allowed_hosts"]:
            problems.append("provider %s 主机 %s 不在白名单" % (p["provider"], host))
        if not p["base"].endswith(":%d" % p["port"]):
            problems.append("provider %s base 与 port 不一致" % p["provider"])
        # ★ denied_* 只对 denied_scope 里的 provider 生效：这 9 个名字是 :8303
        #   门面显式拒绝的路由名，其中 6 个恰恰是 :8301/:8302 的合法工具。
        in_denied_scope = p["provider"] in set(wl.get("denied_scope") or [])
        if len(p["tools"]) != p.get("expected_tool_count"):
            problems.append("provider %s 工具数 %s != expected_tool_count %s"
                            % (p["provider"], len(p["tools"]), p.get("expected_tool_count")))
        for n in p["tools"]:
            if in_denied_scope and n in wl["denied_tool_names"]:
                problems.append("provider %s 试图声明禁用工具 %s" % (p["provider"], n))
            if in_denied_scope and n in wl["denied_operation_ids"]:
                problems.append("provider %s 试图声明禁用 operationId %s" % (p["provider"], n))
        for n in p.get("never_register", []):
            if n in p["tools"]:
                problems.append("provider %s 的 never_register 工具 %s 又出现在 tools 里" % (p["provider"], n))
        # 工具级声明必须来自模型文件且与文件计数一致
        specs = specs_for(binding, srcs, p)
        if list(p["tools"]) != [s["name"] for s in specs]:
            problems.append("provider %s tools 声明与投影顺序/集合不一致" % p["provider"])
        if in_denied_scope:
            for s in specs:
                if s["name"] in wl["denied_tool_names"]:
                    problems.append("provider %s 投影里出现禁用工具名 %s" % (p["provider"], s["name"]))
                if s["name"] in wl["denied_operation_ids"]:
                    problems.append("provider %s 投影里出现禁用 operationId %s" % (p["provider"], s["name"]))
        # kgt 契约交叉核对（以模型文件为准，不看 binding 自述）
        src_kgt = srcs.get("kgt") or {}
        if p["tools_from"] in KGT_TOOLS_FROM:
            _arm, _ck = KGT_TOOLS_FROM[p["tools_from"]]
            banned = sorted(set(p["tools"]) & set(src_kgt.get("forbidden_tools_all_arms") or []))
            if banned:
                problems.append("provider %s 声明了 kgt-tools.yaml forbidden_tools_all_arms：%s"
                                % (p["provider"], banned))
            exp_kgt = (src_kgt.get("expected_tool_counts") or {}).get(_arm)
            if exp_kgt is not None and len(p["tools"]) != exp_kgt:
                problems.append("provider %s 工具数 %s != kgt expected_tool_counts.%s %s"
                                % (p["provider"], len(p["tools"]), _arm, exp_kgt))
    # counts 段 与 模型文件实际计数
    # ★ A′（2026-10-15）：本检查问的是「Set B **契约**声明的工具总数」⇒ 用 `setb_contract_tools`。
    #   它**不是** `setb_tools`——后者是 Q 臂**绑定**的工具数，由 App 级 `count_key` 消费
    #   （`deploy_agents.py::expected_tool_count` / `contract_audit.py::expected_tool_count`）。
    #   A′ 把 Q 臂从「绑满 8 个」收窄到 4 个之前，两种含义偶然同值；复用同一个键会让本检查
    #   自相矛盾（同一行既要求 4 又要求 8），A′ 后必须按键的**真实语义**取数。
    want_all = binding["counts"].get("setb_contract_tools")
    real_setb = len(srcs["setb"]["tools"])
    if want_all is None:
        problems.append("binding.counts.setb_contract_tools 未声明（Set B 契约工具总数无从核对）")
    elif real_setb != want_all:
        problems.append("binding.counts.setb_contract_tools=%s 与 tools-setb.yaml 实际 %s 不一致"
                        % (want_all, real_setb))
    if want_all is not None and want_all != len(project_setb(srcs["setb"], binding)):
        problems.append("tools-setb.yaml 投影后工具数不是 %s" % want_all)
    for _tf, (_arm, _ck) in sorted(KGT_TOOLS_FROM.items()):
        _n = None
        for p in api_providers(binding):
            if p["tools_from"] == _tf:
                _n = len(p["tools"])
        if _n != binding["counts"].get(_ck):
            problems.append("binding.counts.%s=%s 与实际 %s 不一致"
                            % (_ck, binding["counts"].get(_ck), _n))
    if binding["counts"]["api_providers_declared"] != len(api_providers(binding)):
        problems.append("binding.counts.api_providers_declared 与实际不符")
    # 主对比组两侧工具清单必须逐字相同
    twins = [p for p in api_providers(binding) if p.get("verbatim_twin_of")]
    for tw in twins:
        orig = [p for p in api_providers(binding) if p["provider"] == tw["verbatim_twin_of"]]
        if not orig:
            problems.append("verbatim_twin_of 指向不存在的 provider %s" % tw["verbatim_twin_of"])
            continue
        if list(orig[0]["tools"]) != list(tw["tools"]):
            problems.append("逐字孪生组工具清单不一致：%s vs %s" % (orig[0]["provider"], tw["provider"]))
    # ★ S2c（D47/D48）：中性取数工具在四组里的落地（声明驱动，只加断言）
    problems += neutral_tool_arms(binding, srcs)
    return problems


def assert_no_doc_fields_in_schema(binding, schema, label) -> list:
    """严禁把文档性字段注入注册面（R-34 真陷阱）。"""
    strip = set(binding["whitelist"]["strip_fields"])
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in strip:
                    hits.append("%s: 注册面出现被禁止的文档字段 %s（路径 %s）" % (label, k, path))
                walk(v, "%s.%s" % (path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    walk(schema, "$")
    return hits


def _sha256_text(text) -> str:
    return C.sha256_bytes(str(text).encode("utf-8"))


def fold_exempt_ok(binding, ref, name, a, b) -> bool:
    """「仅差空白折叠」的 description 差异是否**已登记且哈希钉住**（fail-closed）。

    未登记 / ref 或 tool 不匹配 / 任一归一化哈希不符 ⇒ False（调用方继续硬失败）。
    """
    if not isinstance(binding, dict):
        return False
    for e in ((binding.get("meta") or {}).get("ref_description_fold_exemptions") or []):
        if e.get("ref") != ref or e.get("tool") != name:
            continue
        if e.get("norm_sha256_shape") == _sha256_text(_norm_ws(b)) and \
           e.get("norm_sha256_registered") == _sha256_text(_norm_ws(a)):
            return True
    return False


_FORBID_EXEMPT_SEEN = set()


def _note_forbid_exempt(where, tok, tsig):
    key = (tok, tsig)
    if key in _FORBID_EXEMPT_SEEN:
        return
    _FORBID_EXEMPT_SEEN.add(key)
    print("FORBID_TOKEN_TEXT_EXEMPT [token=%s text_sha256=%s] %s：该文本已在 "
          "binding.fairness.model_face_projection.forbid_token_text_exemptions 登记；"
          "S3a 修措辞后必须删除该登记。" % (tok, tsig[:16], where))


def _forbid_text_exempts(binding) -> set:
    """(文本 sha256, token) 的误报豁免登记集合；token 表与扫描面不受影响。"""
    mfp = (binding.get("fairness") or {}).get("model_face_projection") or {}
    return {(str(e.get("text_sha256") or ""), str(e.get("token") or "").lower())
            for e in (mfp.get("forbid_token_text_exemptions") or [])}


def forbidden_hits(binding, model_face_texts) -> list:
    tokens = [str(t).lower() for t in binding["fairness"]["forbid_tokens"]]
    exempt = _forbid_text_exempts(binding)
    hits = []
    for where, text in model_face_texts:
        low = (text or "").lower()
        tsig = _sha256_text(text or "")
        for tok in tokens:
            if tok in low:
                if (tsig, tok) in exempt:
                    _note_forbid_exempt(where, tok, tsig)
                    continue
                hits.append("%s 命中禁用词 %r" % (where, tok))
    return hits


def main_face_texts(binding, provider, schema) -> list:
    """主对比组的禁用词扫描面：全部模型面字符串。"""
    out = []
    info = schema.get("info", {})
    out.append(("%s.info.title" % provider["provider"], info.get("title", "")))
    out.append(("%s.info.description" % provider["provider"], info.get("description", "")))
    for path, item in schema["paths"].items():
        op = item[binding["registration_face"]["http_method"]]
        out.append(("%s%s.summary" % (provider["provider"], path), op.get("summary", "")))
        out.append(("%s%s.description" % (provider["provider"], path), op.get("description", "")))
        out.append(("%s%s.operationId" % (provider["provider"], path), op.get("operationId", "")))
        props = op["requestBody"]["content"]["application/json"]["schema"]["properties"]
        for pname, pdef in props.items():
            out.append(("%s%s.param:%s" % (provider["provider"], path, pname), pname))
            out.append(("%s%s.param:%s description" % (provider["provider"], path, pname),
                        pdef.get("description", "")))
    return out


# ===========================================================================
# 3. 模型面投影：声明侧 / live 回读侧
# ===========================================================================
def declared_face(binding, provider, schema) -> list:
    alias = binding["registration_face"]["type_alias"]
    face = []
    for path, item in schema["paths"].items():
        op = item[binding["registration_face"]["http_method"]]
        body = op["requestBody"]
        props = body["content"]["application/json"]["schema"]["properties"]
        params = []
        for pname, pdef in props.items():
            t = pdef.get("type")
            params.append(
                {
                    "name": pname,
                    "type": alias.get(t, t),
                    "required": pname in body["content"]["application/json"]["schema"]["required"],
                    "description": pdef.get("description", ""),
                }
            )
        face.append(
            {
                "tool_name": op["operationId"],
                "path": path,
                "method": binding["registration_face"]["http_method"],
                "description": op.get("description", ""),
                "params": sorted(params, key=lambda p: p["name"]),
            }
        )
    return sorted(face, key=lambda x: x["tool_name"])


def live_face(binding, provider, entity) -> list:
    face = []
    for t in entity.get("tools") or []:
        op = t.get("openapi") or {}
        params = []
        for p in t.get("parameters") or []:
            params.append(
                {
                    "name": p.get("name"),
                    "type": p.get("type"),
                    "required": bool(p.get("required")),
                    "description": p.get("llm_description") or "",
                }
            )
        face.append(
            {
                "tool_name": t.get("operation_id"),
                "path": (t.get("server_url") or "").replace(provider["base"], "") or None,
                "method": t.get("method"),
                "description": op.get("description", ""),
                "params": sorted(params, key=lambda p: p["name"]),
            }
        )
    return sorted(face, key=lambda x: x["tool_name"] or "")


def live_summary_matches_operation(entity) -> bool:
    """非门禁观测：live tools[].summary 与 openapi.description 是否一致。"""
    for t in entity.get("tools") or []:
        op = t.get("openapi") or {}
        if (t.get("summary") or "") != (op.get("description") or ""):
            return False
    return True


def neutralize_base(schema, base) -> dict:
    s = copy.deepcopy(schema)
    for srv in s.get("servers") or []:
        if srv.get("url") == base:
            srv["url"] = PLACEHOLDER_BASE
    return s


def face_sha256(face) -> str:
    return C.sha256_bytes(C.stable_json(face).encode("utf-8"))


# ===========================================================================
# 4. 比对与写入
# ===========================================================================
def diff_report(left, right, left_name, right_name, limit=8) -> list:
    lines = []
    ln = {x["tool_name"]: x for x in left}
    rn = {x["tool_name"]: x for x in right}
    for k in sorted(set(ln) | set(rn)):
        if k not in ln:
            lines.append("  只有 %s 有工具 %s" % (right_name, k))
            continue
        if k not in rn:
            lines.append("  只有 %s 有工具 %s" % (left_name, k))
            continue
        if C.stable_json(ln[k]) != C.stable_json(rn[k]):
            a, b = ln[k], rn[k]
            for field in ("path", "method", "description"):
                if a.get(field) != b.get(field):
                    lines.append("  工具 %s 字段 %s：%s=%r  vs  %s=%r"
                                 % (k, field, left_name, a.get(field), right_name, b.get(field)))
            pa = {p["name"]: p for p in a.get("params") or []}
            pb = {p["name"]: p for p in b.get("params") or []}
            for pn in sorted(set(pa) | set(pb)):
                if pn not in pa:
                    lines.append("  工具 %s 只有 %s 有参数 %s" % (k, right_name, pn))
                elif pn not in pb:
                    lines.append("  工具 %s 只有 %s 有参数 %s" % (k, left_name, pn))
                elif C.stable_json(pa[pn]) != C.stable_json(pb[pn]):
                    lines.append("  工具 %s 参数 %s：%s=%s  vs  %s=%s"
                                 % (k, pn, left_name, C.stable_json(pa[pn]), right_name, C.stable_json(pb[pn])))
        if len(lines) >= limit:
            lines.append("  …（截断）")
            break
    return lines


def schema_structural_diff(want: dict, got: dict) -> list:
    """leg A：声明 schema 与 Dify 原样存回的 schema 逐字比对（无类型映射）。"""
    out = []
    if (want.get("servers") or [{}])[0].get("url") != (got.get("servers") or [{}])[0].get("url"):
        out.append("  servers[0].url: want=%r got=%r"
                   % ((want.get("servers") or [{}])[0].get("url"), (got.get("servers") or [{}])[0].get("url")))
    for key in ("info", "openapi"):
        if C.stable_json(want.get(key)) != C.stable_json(got.get(key)):
            out.append("  %s: want=%s got=%s" % (key, C.stable_json(want.get(key)), C.stable_json(got.get(key))))
    wp, gp = want.get("paths") or {}, got.get("paths") or {}
    if sorted(wp) != sorted(gp):
        out.append("  paths 集合不同: want=%s got=%s" % (sorted(wp), sorted(gp)))
    for path in sorted(set(wp) & set(gp)):
        if C.stable_json(wp[path]) != C.stable_json(gp[path]):
            wop = (wp[path] or {}).get("post") or {}
            gop = (gp[path] or {}).get("post") or {}
            for field in ("summary", "description", "operationId"):
                if wop.get(field) != gop.get(field):
                    out.append("  %s %s: want=%r got=%r" % (path, field, wop.get(field), gop.get(field)))
            ws = (((wop.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            gs = (((gop.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            if ws != gs:
                out.append("  %s requestBody.schema: want=%s got=%s"
                           % (path, C.stable_json(ws), C.stable_json(gs)))
            if C.stable_json(wop.get("responses")) != C.stable_json(gop.get("responses")):
                out.append("  %s responses 不同" % path)
    return out


def add_payload(binding, provider, schema) -> dict:
    reg = binding["registration_face"]
    return {
        "provider": provider["provider"],
        "icon": copy.deepcopy(reg["icon"]),
        "credentials": {"auth_type": reg["credentials_auth_type"]},
        "schema_type": reg["schema_type"],
        "schema": json.dumps(schema, ensure_ascii=False),
        "privacy_policy": reg["privacy_policy"],
        "custom_disclaimer": reg["custom_disclaimer"],
        "labels": list(reg["labels"]),
    }


def update_payload(binding, provider, schema, current_id) -> dict:
    """★ 三件套 provider / original_provider / provider_id 缺一 422。"""
    p = add_payload(binding, provider, schema)
    p["original_provider"] = provider["provider"]
    p["provider_id"] = current_id
    assert_update_trio(p)
    return p


def assert_update_trio(payload) -> None:
    missing = [k for k in ("provider", "original_provider", "provider_id") if not payload.get(k)]
    if missing:
        raise Fail("update payload 缺少三件套字段 %s（Dify 会返回 422）" % missing)


# ===========================================================================
# 5. 动作
# ===========================================================================
def collect_declared(binding, srcs):
    out = []
    for p in api_providers(binding):
        specs = specs_for(binding, srcs, p)
        schema = build_schema(binding, p, specs)
        out.append({"provider": p, "specs": specs, "schema": schema})
    return out


def cmd_snapshot_list(args) -> int:
    binding, _ = load_declaration(args.binding)
    console = C.console_client()
    providers = console.list_api_providers()
    payload = {
        "collected_at_utc": None,
        "count": len(providers),
        "providers": [{"name": n, "id": i} for n, i in sorted(providers.items())],
    }
    import datetime

    payload["collected_at_utc"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    Path(args.snapshot_list).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已抓取 provider 名单基线：%d 个 → %s（时点 %s）"
          % (len(providers), args.snapshot_list, payload["collected_at_utc"]))
    return 0


def run_check(binding, srcs, declared, console, baseline=None, quiet=False) -> int:
    problems = []
    notes = []
    providers_live = console.list_api_providers()

    # --- 前置：声明面自身合法 ---
    problems += ["声明面非法：%s" % x for x in verify_tool_sources(binding)]
    problems += ["声明面非法：%s" % x for x in assert_declaration_legal(binding, srcs)]

    # --- 禁用词 / 文档字段（projéc 面） ---
    for d in declared:
        prov = d["provider"]
        exempt = prov["provider"] in binding["fairness"]["model_face_projection"].get("exempt_providers", [])
        problems += ["注册面非法字段：%s" % x
                     for x in assert_no_doc_fields_in_schema(binding, d["schema"], prov["provider"])]
        if not exempt:
            problems += ["禁用词：%s" % x
                         for x in forbidden_hits(binding, main_face_texts(binding, prov, d["schema"]))]
        else:
            notes.append("%s 按 F5 豁免禁用词扫描（fairness.model_face_projection.exempt_providers）" % prov["provider"])

    # --- 两侧逐字孪生（声明面） ---
    twins = {}
    for d in declared:
        prov = d["provider"]
        key = prov.get("verbatim_twin_of")
        if not key:
            continue
        base_d = [x for x in declared if x["provider"]["provider"] == key][0]
        ha = C.sha256_bytes(C.stable_json(neutralize_base(base_d["schema"], base_d["provider"]["base"])).encode())
        hb = C.sha256_bytes(C.stable_json(neutralize_base(d["schema"], prov["base"])).encode())
        twins[(key, prov["provider"])] = (ha, hb)
        if ha != hb:
            problems.append("两侧投影归一化 sha256 不等：%s=%s  vs  %s=%s" % (key, ha[:16], prov["provider"], hb[:16]))
            problems += diff_report(declared_face(binding, base_d["provider"], base_d["schema"]),
                                    declared_face(binding, prov, d["schema"]),
                                    key, prov["provider"])

    # --- 逐 provider：三样比对 ---
    live_entities = {}
    for d in declared:
        prov = d["provider"]
        name = prov["provider"]
        if name not in providers_live:
            problems.append("live 中不存在 provider %s（需先 --apply）" % name)
            continue
        entity = console.get_provider(name)
        live_entities[name] = entity
        # leg A：schema 逐字
        try:
            stored = json.loads(entity["schema"]) if isinstance(entity["schema"], str) else entity["schema"]
        except Exception as exc:
            problems.append("%s 回读 schema 不是 JSON：%s" % (name, exc))
            continue
        if C.stable_json(stored) != C.stable_json(d["schema"]):
            problems.append("%s leg A（schema 逐字）漂移" % name)
            problems += schema_structural_diff(d["schema"], stored)
        # leg B：模型面
        want_face = declared_face(binding, prov, d["schema"])
        got_face = live_face(binding, prov, entity)
        if C.stable_json(want_face) != C.stable_json(got_face):
            problems.append("%s leg B（模型面）漂移" % name)
            problems += diff_report(want_face, got_face, "declared", "live")
        if not live_summary_matches_operation(entity):
            notes.append("%s：live tools[].summary 与 openapi.description 不一致（观测，非门禁）" % name)
        # leg D：禁用名不得出现（只对 denied_scope 内的 provider）
        got_names = {t.get("operation_id") for t in (entity.get("tools") or [])}
        if name in set(binding["whitelist"].get("denied_scope") or []):
            for bad in binding["whitelist"]["denied_tool_names"] + binding["whitelist"]["denied_operation_ids"]:
                if bad in got_names:
                    problems.append("%s 注册面出现禁用工具名 %s" % (name, bad))
        if got_names != {s["name"] for s in d["specs"]}:
            problems.append("%s 注册面工具集合 %s != 声明 %s"
                            % (name, sorted(got_names), sorted(s["name"] for s in d["specs"])))

    # --- 两侧逐字孪生（live 面） ---
    for (a, b) in twins:
        if a in live_entities and b in live_entities:
            ea, eb = live_entities[a], live_entities[b]
            try:
                sa = json.loads(ea["schema"])
                sb = json.loads(eb["schema"])
            except Exception:
                continue
            ba = [p for p in api_providers(binding) if p["provider"] == a][0]["base"]
            bb = [p for p in api_providers(binding) if p["provider"] == b][0]["base"]
            ha = C.sha256_bytes(C.stable_json(neutralize_base(sa, ba)).encode())
            hb = C.sha256_bytes(C.stable_json(neutralize_base(sb, bb)).encode())
            notes.append("live 孪生 %s / %s 归一化 sha256 %s / %s（相等=%s）"
                         % (a, b, ha[:16], hb[:16], ha == hb))
            if ha != hb:
                problems.append("live 两侧投影不逐字相同：%s vs %s" % (a, b))

    # --- 只增不删：与基线 diff ---
    if baseline:
        base = json.loads(Path(baseline).read_text(encoding="utf-8"))
        old = {p["name"]: p["id"] for p in base["providers"]}
        removed = sorted(set(old) - set(providers_live))
        changed = sorted(n for n in set(old) & set(providers_live) if old[n] != providers_live[n])
        added = sorted(set(providers_live) - set(old))
        notes.append("基线 diff：基线=%d live=%d 新增=%d 删除=%d id 变更=%d"
                     % (len(old), len(providers_live), len(added), len(removed), len(changed)))
        if added:
            notes.append("  新增：%s" % added)
        if removed:
            problems.append("既有 provider 被删除：%s" % removed)
        if changed:
            problems.append("既有 provider id 变更：%s" % changed)

    count_now = len(providers_live)
    notes.append("live API provider 总数 = %d（基线登记 %d，apply 后预期 %d）"
                 % (count_now, binding["counts"]["baseline_api_providers"],
                    binding["counts"]["expected_api_providers_after_apply"]))

    # --- 输出 ---
    if not quiet:
        print("== leg A 声明 schema 规范化 sha256（含 base）==")
        for d in declared:
            print("   %-16s %s" % (d["provider"]["provider"],
                                   C.sha256_bytes(C.stable_json(d["schema"]).encode())[:32]))
        for (a, b), (ha, hb) in twins.items():
            print("== 两侧逐字孪生（除 base）== %s / %s" % (a, b))
            print("   normalized sha256 %s  %s   相等=%s" % (ha, hb, ha == hb))
        for n in notes:
            print("note: %s" % n)
    if problems:
        print("\n-- FAIL: %d 项 --" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return 1
    print("\n-- OK: 三样比对零漂移 --")
    return 0


def cmd_check(args) -> int:
    binding, srcs = load_declaration(args.binding)
    declared = collect_declared(binding, srcs)
    console = C.console_client()
    return run_check(binding, srcs, declared, console, baseline=args.baseline)


def cmd_apply(args) -> int:
    binding, srcs = load_declaration(args.binding)
    problems = verify_tool_sources(binding) + assert_declaration_legal(binding, srcs)
    if problems:
        for p in problems:
            print("FAIL(声明面): %s" % p)
        return 1
    declared = collect_declared(binding, srcs)
    for d in declared:
        prov = d["provider"]
        bad = assert_no_doc_fields_in_schema(binding, d["schema"], prov["provider"])
        exempt = prov["provider"] in binding["fairness"]["model_face_projection"].get("exempt_providers", [])
        if not exempt:
            bad += forbidden_hits(binding, main_face_texts(binding, prov, d["schema"]))
        if bad:
            for b in bad:
                print("FAIL(注册面): %s" % b)
            return 1

    console = C.console_client()
    live = console.list_api_providers()
    print("apply 前 live API provider = %d" % len(live))
    for d in declared:
        prov = d["provider"]
        name = prov["provider"]
        if name not in binding["whitelist"]["provider_names"]:
            print("REFUSE: %s 不在白名单" % name)
            return 1
        want = d["schema"]
        if name not in live:
            print("ADD    %-16s %s  (%d 工具)" % (name, prov["base"], len(d["specs"])))
            console.add_provider(add_payload(binding, prov, want))
            action = "added"
        else:
            cur = console.get_provider(name)
            # ★ FIX-1 附带修复（非占位符移除本身）：本环境的 api/get 实体**不含 id**
            # （实测顶层键只有 credentials/custom_disclaimer/description/icon/labels/
            #  privacy_policy/schema/schema_type/tools 共 9 个），故 provider_id 必须
            # 回退到 list_api_providers() 的 {name: id}。这是取值来源的补齐，
            # **不放松任何判据**：assert_update_trio 仍强制三件套齐全。
            pid = cur.get("id") or live.get(name)
            if not pid:
                print("FAIL %s：list 与 get 均未给出 provider_id，无法满足 update 三件套" % name)
                return 1
            try:
                stored = json.loads(cur["schema"]) if isinstance(cur["schema"], str) else cur["schema"]
            except Exception:
                stored = None
            face_ok = C.stable_json(live_face(binding, prov, cur)) == C.stable_json(
                declared_face(binding, prov, want))
            if stored is not None and C.stable_json(stored) == C.stable_json(want) and face_ok:
                print("NOOP   %-16s 已存在且逐字一致" % name)
                action = "noop"
            else:
                print("UPDATE %-16s %s  (id=%s)" % (name, prov["base"], pid))
                console.update_provider(update_payload(binding, prov, want, pid))
                action = "updated"
        # 写后回读
        back = console.get_provider(name)
        try:
            stored2 = json.loads(back["schema"]) if isinstance(back["schema"], str) else back["schema"]
        except Exception as exc:
            print("FAIL 回读 %s 的 schema 不是 JSON：%s" % (name, exc))
            return 1
        if C.stable_json(stored2) != C.stable_json(want):
            print("FAIL %s 写后回读 schema 与声明不一致（%s）" % (name, action))
            for line in schema_structural_diff(want, stored2):
                print(line)
            return 1
        f_ok = C.stable_json(live_face(binding, prov, back)) == C.stable_json(declared_face(binding, prov, want))
        if not f_ok:
            print("FAIL %s 写后回读模型面与声明不一致" % name)
            for line in diff_report(declared_face(binding, prov, want), live_face(binding, prov, back),
                                    "declared", "live"):
                print(line)
            return 1
        print("       回读一致（schema 逐字 + 模型面）")
    live2 = console.list_api_providers()
    print("apply 后 live API provider = %d" % len(live2))
    added = sorted(set(live2) - set(live))
    removed = sorted(set(live) - set(live2))
    print("本次新增：%s" % added)
    print("本次删除：%s" % removed)
    if removed:
        print("FAIL: 出现删除，违反只增不删")
        return 1
    console2 = console
    return run_check(binding, srcs, collect_declared(binding, srcs), console2, baseline=args.baseline)


# ===========================================================================
# 6. 离线自检（不需要 Dify）
# ===========================================================================
class FakeTransport:
    """把声明面伪装成一台 Dify：用于离线端到端验证比对器本身。"""

    def __init__(self, binding, declared, tamper=None):
        self.cookies = C.CookieJar()
        self.calls = []
        self.tamper = tamper or {}
        self.store = {}
        for d in declared:
            self.store[d["provider"]["provider"]] = self._entity(binding, d)

    def _entity(self, binding, d):
        prov = d["provider"]
        tools = []
        for path, item in d["schema"]["paths"].items():
            op = item["post"]
            body = op["requestBody"]
            props = body["content"]["application/json"]["schema"]["properties"]
            req = body["content"]["application/json"]["schema"]["required"]
            params = []
            for pname, pdef in props.items():
                t = pdef.get("type")
                params.append(
                    {
                        "name": pname,
                        "type": binding["registration_face"]["type_alias"].get(t, t),
                        "required": pname in req,
                        "llm_description": pdef.get("description", ""),
                        "label": {k: pname for k in ("en_US", "zh_Hans", "pt_BR", "ja_JP")},
                        "form": "llm",
                    }
                )
            tools.append(
                {
                    "server_url": prov["base"] + path,
                    "method": "post",
                    "summary": op.get("description", ""),
                    "operation_id": op["operationId"],
                    "parameters": params,
                    "author": "",
                    "icon": None,
                    "openapi": op,
                    "output_schema": {},
                }
            )
        entity = {
            "id": "fake-" + prov["provider"],
            "name": prov["provider"],
            "schema_type": "openapi",
            "schema": json.dumps(d["schema"], ensure_ascii=False),
            "tools": tools,
            "credentials": {"auth_type": "none"},
        }
        for key, fn in self.tamper.get(prov["provider"], {}).items():
            fn(entity)
        return entity

    def request(self, method, url, json_body=None, headers=None, timeout=None):
        self.calls.append((method.upper(), url))
        if url.endswith("/console/api/login"):
            self.cookies.set("csrf_token", "fake-csrf")
            self.cookies.set("access_token", "fake-token")
            return C._Response(200, "{}")
        if "tool-providers?type=api" in url:
            data = [{"name": n, "id": e["id"]} for n, e in self.store.items()]
            return C._Response(200, json.dumps(data))
        if "tool-provider/api/get" in url:
            name = url.split("provider=", 1)[1].split("&")[0]
            import urllib.parse

            name = urllib.parse.unquote(name)
            if name not in self.store:
                return C._Response(404, "{}")
            return C._Response(200, json.dumps(self.store[name], ensure_ascii=False))
        if "tool-provider/api/add" in url:
            name = json_body["provider"]
            self.store[name] = {"id": "fake-" + name, "name": name, "schema": json_body["schema"],
                               "tools": [], "schema_type": "openapi"}
            return C._Response(200, json.dumps({"result": "success"}))
        if "tool-provider/api/update" in url:
            missing = [k for k in ("provider", "original_provider", "provider_id") if not json_body.get(k)]
            if missing:
                return C._Response(422, json.dumps({"code": "invalid_param", "message": "missing %s" % missing}))
            name = json_body["provider"]
            self.store[name] = {"id": json_body["provider_id"], "name": name, "schema": json_body["schema"],
                                "tools": [], "schema_type": "openapi"}
            return C._Response(200, json.dumps({"result": "success"}))
        return C._Response(404, "{}")


_SHAPE_OK = {
    "f.yaml": "k:\n  description: \"d\"\n  params:\n    a:\n      type: string\n  returns:\n    x: string\n"
}


def _ref_code(entry, files, model_dir=None, binding=None):
    """在临时 MODEL_DIR 上跑一次 resolve_tool_entry，返回抛出错误的码（未抛 ⇒ None）。"""
    import tempfile

    d = Path(model_dir) if model_dir else Path(tempfile.mkdtemp(prefix="m5_s2c_ref_"))
    for fn, text in (files or {}).items():
        (d / fn).write_text(text, encoding="utf-8", newline="\n")
    try:
        resolve_tool_entry(entry, model_dir=d, binding=binding)
    except RefError as exc:
        return exc.code
    return None


def _fold_decl(ref, name, shape_text, registered_text):
    """构造一份「已登记且哈希钉住」的折叠豁免 binding（负控/正控共用）。"""
    return {"meta": {"ref_description_fold_exemptions": [{
        "ref": ref, "tool": name,
        "norm_sha256_shape": _sha256_text(_norm_ws(shape_text)),
        "norm_sha256_registered": _sha256_text(_norm_ws(registered_text)),
    }]}}


def _need_forbid_exempt():
    """豁免只针对钉住的那段文本：同 token 的其他文本必须仍被命中。

    ★ S3a 窗口收口：改用**合成 binding**（自造钉住文本）验证机制本身，不再依赖真
    binding 里的窗口豁免条目 —— 否则豁免一删，本控制项就从「验机制」退化成「验窗口
    凭证」。收口后真 binding 的 forbid_token_text_exemptions 必须为空（由
    register_tools --check 的真机回读腿与离线门禁共同保证，不在此处耦合）。
    """
    probe = "探针文本：收窄是视图，不改工作集。"   # 含单字 token「图」（在「视图」里）
    other = "这是一张图，看图说话"
    mfp = {"forbid_tokens": ["图"]}
    decl = {"fairness": dict(mfp)}
    decl["fairness"]["model_face_projection"] = {
        "forbid_token_text_exemptions": [{"token": "图", "text_sha256": _sha256_text(probe)}]}
    if forbidden_hits(decl, [("probe", probe)]):
        raise Fail("已登记的误报文本仍被命中：%r" % forbidden_hits(decl, [("probe", probe)]))
    if not forbidden_hits(decl, [("probe-other", other)]):
        raise Fail("豁免把整个 token「图」放宽了：同 token 的其他文本未被命中")
    # fail-closed：登记的 sha 与文本不符（换了一段文本）⇒ 仍必须命中，不得按 token 放宽
    decl2 = {"fairness": dict(mfp)}
    decl2["fairness"]["model_face_projection"] = {
        "forbid_token_text_exemptions": [{"token": "图", "text_sha256": _sha256_text("另一段文本")}]}
    if not forbidden_hits(decl2, [("probe", probe)]):
        raise Fail("豁免的 (token, text_sha256) 精确匹配失效：sha 不符的文本仍被放行")
    return True


def _entry_of(binding, srcs, provider_name, tool_name):
    """取某 provider 声明的某工具**解析后**的条目（四组同源证据的唯一取数口）。"""
    p = [x for x in api_providers(binding) if x["provider"] == provider_name]
    if not p:
        raise Fail("binding.providers 里没有 %s" % provider_name)
    p = p[0]
    if p["tools_from"] == "setb":
        return resolve_tool_entry(srcs["setb"]["tools"][tool_name], binding=binding)
    if p["tools_from"] == "kgt":
        t = [x for x in srcs["kgt"]["tools"] if x["tool_name"] == tool_name]
        if not t:
            raise Fail("kgt-tools.yaml 里没有 %s" % tool_name)
        return resolve_tool_entry(t[0], binding=binding)
    if p["tools_from"] == "livefacts":
        return resolve_tool_entry(shape_entry(binding, srcs["livefacts"], tool_name),
                                  binding=binding)
    raise Fail("未知 tools_from: %s" % p["tools_from"])


def _four_arms_same_shape(binding, srcs):
    """四组形状同源：四组条目各过一次 resolve_tool_entry，逐字比 params/returns/required。"""
    arms = [
        ("KG", _entry_of(binding, srcs, "cc-kg-tools", "read_facts")),
        ("ONTO", _entry_of(binding, srcs, "cc-onto-tools", "read_facts")),
        ("KG-T", _entry_of(binding, srcs, "cc-kgt-tools", "read_facts")),
        ("KG-T-free", _entry_of(binding, srcs, "cc-live-facts-tools", "read_live_facts")),
    ]
    seqs = {}
    for label, e in arms:
        seqs[label] = {
            "params": list((e.get("params") or {}).keys()),
            "returns": list((e.get("returns") or {}).keys()),
            "required": list(e.get("required") or []),
            "additional_properties": e.get("additional_properties"),
        }
    ref = seqs["KG"]
    for label, s in seqs.items():
        if s != ref:
            raise Fail("四组形状不同源：%s=%r != KG=%r" % (label, s, ref))
    if arms[0][1].get("name") != "read_facts" or arms[3][1].get("name") != "read_live_facts":
        raise Fail("别名只换 name 未生效：%r / %r" % (arms[0][1].get("name"), arms[3][1].get("name")))
    a = dict(arms[0][1])
    d = dict(arms[3][1])
    a.pop("name", None)
    d.pop("name", None)
    if C.stable_json(a) != C.stable_json(d):
        raise Fail("别名条目除 name 外与主名不同源（D47 要求逐字同源）")
    return True


def _tests(binding, srcs, declared) -> list:
    """返回 [(名字, 期望通过?, 可调用对象)]；可调用对象抛异常即失败。"""
    import copy as _copy

    T = []

    def case(name, fn):
        T.append((name, fn))

    # --- 正控 ---
    case("P01 binding.yaml 可解析且六段齐备", lambda: _need(
        set(["meta", "workspace", "tool_sources", "carriers", "providers", "apps", "fairness"]) <= set(binding)))
    case("P02 模型契约指纹与登记一致", lambda: _need(not verify_tool_sources(binding)))
    case("P03 声明面合法（白名单/端口/计数/孪生）", lambda: _need(not assert_declaration_legal(binding, srcs)))
    case("P04 setb 投影出 8 个工具、kgt 主组 5 个", lambda: _need(
        len(project_setb(srcs["setb"], binding)) == 8
        and len(project_kgt(srcs["kgt"], binding,
                            [p for p in api_providers(binding) if p["tools_from"] == "kgt"][0]["tools"])) == 5))
    case("P05 注册面不含任何被禁止的文档字段", lambda: _need(
        bool(declared) and not any(assert_no_doc_fields_in_schema(binding, d["schema"], d["provider"]["provider"]) for d in declared)))
    case("P06 主对比组模型面禁用词零命中", lambda: _need(bool(declared) and not forbidden_hits(
        binding, [t for d in declared if d["provider"]["carrier"] in ("kg", "onto")
                  for t in main_face_texts(binding, d["provider"], d["schema"])])))
    case("P07 两侧（除 base）归一化 sha256 相等", lambda: _need(bool(declared) and _twins_equal(binding, declared)))
    case("P08 离线端到端 check 通过（FakeTransport）", lambda: _need(
        run_check(binding, srcs, declared, _fake_console(binding, declared), quiet=True) == 0))
    case("P09 类型别名：schema 原文 integer，模型面投影为 number", lambda: _need(
        _alias_probe(binding, declared)))

    case("P10 中性取数工具四组形状同源（params/returns 键序列逐字相同）", lambda: _need(
        _four_arms_same_shape(binding, srcs)))
    case("P11 引用解析正向：格式正确 + 形状齐备 ⇒ 解析成功且别名只换 name", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k"}, _SHAPE_OK) is None))

    # --- 负控 ---
    case("N01 缺 provider_id 的 update payload 被拒（三件套）", lambda: _need_raises(
        Fail, lambda: assert_update_trio({"provider": "x", "original_provider": "x"})))
    case("N02 非白名单 provider 名被拒", lambda: _need(
        "不在 whitelist.provider_names" in " ".join(_assert_refuses_provider(binding, srcs, "cc-evil-tools"))))
    case("N03 白名单外端口被拒", lambda: _need(
        "不在白名单" in " ".join(_assert_refuses_port(binding, srcs, 9999))))
    case("N04 denied 工具名试图注册被拒（kgt 门面）", lambda: _need(
        "禁用工具" in " ".join(_assert_refuses_tool(binding, srcs, "collect_now"))))
    case("N05 denied operationId 试图注册被拒", lambda: _need(
        "禁用 operationId" in " ".join(_assert_refuses_opid(binding, srcs))))
    case("N06 注入禁用词后扫描必须命中", lambda: _need(
        bool(_assert_forbidden_caught(binding, declared))))
    case("N07 注入 carrier_specific_value_semantics 必须被捕获", lambda: _need(
        bool(_assert_doc_field_caught(binding, declared))))
    case("N08 两侧 schema 出现真实差异时孪生比对失败", lambda: _need(
        _assert_twin_diff_caught(binding, srcs)))
    case("N09 binding.counts 与模型文件计数不一致必须被捕获", lambda: _need(
        bool(_assert_count_mismatch_caught(binding, srcs))))
    case("N10 模型文件指纹不符必须被捕获", lambda: _need(
        bool(_assert_fingerprint_caught(binding))))
    case("N11 leg A：live schema 被改动 ⇒ check 失败且报 leg A", lambda: _need_needle(
        _fake_console(binding, declared, tamper={"cc-kg-tools": {
            "schema": lambda e: _tamper_schema_desc(e)}}), binding, srcs, declared, "leg A"))
    case("N12 leg B：live 参数描述被改动 ⇒ check 失败且报 leg B", lambda: _need_needle(
        _fake_console(binding, declared, tamper={"cc-onto-tools": {
            "tools": lambda e: _tamper_llm_desc(e)}}), binding, srcs, declared, "leg B"))
    case("N13 live 里 provider 缺失 ⇒ check 失败且报 provider 不存在", lambda: _need_needle(
        _fake_console(binding, declared, drop="cc-kgt-tools"), binding, srcs, declared, "不存在 provider"))
    case("N14 注册面出现 denied_* 工具名 ⇒ check 失败且报禁用工具名", lambda: _need_needle(
        _fake_console(binding, declared, tamper={"cc-kgt-tools": {
            "tools": lambda e: _tamper_add_denied(e)}}), binding, srcs, declared, "禁用工具名"))
    case("N15 基线里既有 provider 消失 ⇒ check 失败且报被删除", lambda: _need_needle(
        _fake_console(binding, declared), binding, srcs, declared, "被删除",
        baseline=_baseline_with_ghost()))
    # N16 是类型别名有损性的**补偿证明**：声明侧把 get_facts.timeWindow 从 string 篡改成
    # object（★ R-54 后权威类型是 string；原方向 object→string 的"将来"已落地，故翻转），
    # leg B 因别名（object→string）看不见，但 leg A（原样存回的 schema）必须抓住。
    case("N16 别名掩盖的类型改动仍被 leg A 抓住（leg B 看不见）", lambda: _need(
        _assert_alias_compensated(binding, srcs, declared)))
    # --- S2c：neutral_tool_ref 解析纪律（照 resolution_contract 的错误码逐条负控）---
    case("N17 引用缺 '#' ⇒ ERR_REF_FORMAT", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml"}, {}) == "ERR_REF_FORMAT"))
    case("N18 引用带路径分隔符 / 非 yaml ⇒ ERR_REF_FORMAT", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "sub/f.yaml#k"}, {}) == "ERR_REF_FORMAT"
        and _ref_code({"name": "t", REF_KEY: "f.json#k"}, {}) == "ERR_REF_FORMAT"))
    case("N19 引用文件不存在 ⇒ ERR_REF_MISSING_FILE", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "nope.yaml#k"}, {}) == "ERR_REF_MISSING_FILE"))
    case("N20 引用顶层键不存在 ⇒ ERR_REF_MISSING_KEY", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#missing"},
                  {"f.yaml": "k:\n  params: {}\n  returns: {}\n"}) == "ERR_REF_MISSING_KEY"))
    case("N21 同条目既引用又复制正文 ⇒ ERR_REF_CONFLICT", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "params": [{"name": "a"}]}, _SHAPE_OK)
        == "ERR_REF_CONFLICT"
        and _ref_code({"name": "t", REF_KEY: "f.yaml#k", "returns": {"x": 1}}, _SHAPE_OK)
        == "ERR_REF_CONFLICT"))
    case("N22 形状缺 params/returns ⇒ ERR_REF_SHAPE", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k"},
                  {"f.yaml": "k:\n  params: {}\n"}) == "ERR_REF_SHAPE"))
    case("N23 登记与形状 description 漂移 ⇒ ERR_REF_DESCRIPTION_DRIFT", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "description": "甲"},
                  {"f.yaml": "k:\n  description: \"乙\"\n  params: {}\n  returns: {}\n"})
        == "ERR_REF_DESCRIPTION_DRIFT"))
    case("N26 已登记（哈希钉住）的折叠差异 ⇒ 放行且留痕", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "description": "向采集面真采"},
                  {"f.yaml": "k:\n  description: \"向采集面 真采\"\n  params: {}\n  returns: {}\n"},
                  binding=_fold_decl("f.yaml#k", "t", "向采集面 真采", "向采集面真采"))
        is None and any(n == "t" for n, _r, _a, _b in _WS_FOLD_NOTES)))
    case("N27 归一化后仍然不同 ⇒ 仍抛 ERR_REF_DESCRIPTION_DRIFT", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "description": "向采集面真采"},
                  {"f.yaml": "k:\n  description: \"向采集面真猜\"\n  params: {}\n  returns: {}\n"})
        == "ERR_REF_DESCRIPTION_DRIFT"))
    case("N28 未登记的折叠差异 ⇒ 仍抛 ERR_REF_DESCRIPTION_DRIFT（fail-closed）", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "description": "向采集面真采"},
                  {"f.yaml": "k:\n  description: \"向采集面 真采\"\n  params: {}\n  returns: {}\n"})
        == "ERR_REF_DESCRIPTION_DRIFT"))
    case("N29 已登记但归一化哈希不符（文本被改）⇒ 仍硬失败", lambda: _need(
        _ref_code({"name": "t", REF_KEY: "f.yaml#k", "description": "向采集面真采"},
                  {"f.yaml": "k:\n  description: \"向采集面 真猜\"\n  params: {}\n  returns: {}\n"},
                  binding=_fold_decl("f.yaml#k", "t", "向采集面 真采", "向采集面真采"))
        == "ERR_REF_DESCRIPTION_DRIFT"))
    case("N30 禁用词误报豁免（合成 binding）：钉住文本放行、同 token 其他文本仍命中、sha 不符仍命中", lambda: _need(
        _need_forbid_exempt()))
    case("N24 别名 of 指向不存在的主名 ⇒ 硬失败", lambda: _need_raises(
        Fail, lambda: shape_entry(binding, {"names": {"primary": "p", "free_alias": "q"},
                                           "aliases": {}}, "q")))
    case("N25 别名名与 names.free_alias 不一致 ⇒ 硬失败", lambda: _need_raises(
        Fail, lambda: shape_entry(binding, srcs["livefacts"], "not_the_alias")))
    return T


def _assert_alias_compensated(binding, srcs, declared):
    """证明 type_alias 的有损性被 leg A 补偿：leg A 命中且 leg B 不命中。"""
    tampered = json.loads(json.dumps(declared))
    hit = 0
    for d in tampered:
        if d["provider"]["provider"] != "cc-kgt-tools":
            continue
        for _path, item in d["schema"]["paths"].items():
            op = item["post"]
            if op["operationId"] != "get_facts":
                continue
            sch = op["requestBody"]["content"]["application/json"]["schema"]
            # ★ R-54（2026-10-07）：声明侧已由 `object` 改为 **`string`** —— 因为 Dify 的
            #   `registration_face.type_alias` 把 object 折成 string 给模型看，而服务侧按
            #   **声明类型**校验 ⇒ 声明成 object 则**每一个带该参数的调用必然 400**。
            #   本控制原本就写作「`object` → `string` 的单点篡改（模拟将来 yaml 被改）」——
            #   那个"将来"已经落地，故把**方向翻转**：从 `string` 篡改成 `object`。
            #   性质**完全不变**：别名折叠 `object`→`string` ⇒ 该改动在 **leg B（模型面）不可见**，
            #   只有 **leg A（原样存回的 schema 原文）** 抓得住 —— 这正是本控制要证明的补偿关系。
            if sch["properties"]["timeWindow"]["type"] != "string":
                raise Fail("前置条件不成立：声明侧 timeWindow 不是 string（R-54 后的权威类型）")
            sch["properties"]["timeWindow"]["type"] = "object"
            hit += 1
    if hit != 1:
        raise Fail("未能构造出单点篡改（hit=%d）" % hit)
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_check(binding, srcs, tampered, _fake_console(binding, declared), quiet=True)
    text = buf.getvalue()
    if code != 1:
        raise Fail("篡改未被捕获（exit=%s）" % code)
    if "leg A" not in text:
        raise Fail("leg A 未命中；输出尾部：\n%s" % text[-1200:])
    if "leg B" in text:
        raise Fail("leg B 也命中了，说明本用例没有证明「别名有损但被 leg A 补偿」：\n%s" % text[-1200:])
    return True


def _need(cond):
    if not cond:
        raise AssertionError("断言失败")


def _need_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError("未抛出 %s" % exc.__name__)


def _need_needle(console, binding, srcs, declared, needle, baseline=None):
    """断言 check 失败，且失败原因里真的出现 needle（避免「因别的错而失败」的假通过）。"""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_check(binding, srcs, declared, console, baseline=baseline, quiet=True)
    text = buf.getvalue()
    if code != 1:
        raise Fail("check 未失败（exit=%s）" % code)
    if needle not in text:
        raise Fail("失败原因不是 %r；实际输出尾部：\n%s" % (needle, text[-1500:]))
    return True


def _alias_probe(binding, declared):
    """证明类型别名只作用于「模型面投影」，schema 原文仍是 integer（未放宽源）。"""
    d = [x for x in declared if x["provider"]["carrier"] == "kg"][0]
    raw = (d["schema"]["paths"]["/trace"]["post"]["requestBody"]["content"]
           ["application/json"]["schema"]["properties"]["maxHops"]["type"])
    face = {f["tool_name"]: f for f in declared_face(binding, d["provider"], d["schema"])}
    got = [p["type"] for p in face["cc_trace"]["params"] if p["name"] == "maxHops"]
    return raw == "integer" and got == ["number"]


def _twins_equal(binding, declared):
    pairs = {}
    for d in declared:
        k = d["provider"].get("verbatim_twin_of")
        if k:
            pairs[k] = d
    for k, d in pairs.items():
        a = [x for x in declared if x["provider"]["provider"] == k][0]
        ha = C.sha256_bytes(C.stable_json(neutralize_base(a["schema"], a["provider"]["base"])).encode())
        hb = C.sha256_bytes(C.stable_json(neutralize_base(d["schema"], d["provider"]["base"])).encode())
        if ha != hb:
            return False
    return bool(pairs)


def _fake_console(binding, declared, tamper=None, drop=None):
    tr = FakeTransport(binding, declared, tamper=tamper)
    if drop:
        tr.store.pop(drop, None)
    c = C.Console(binding["dify"]["base_url_from_env"] and "http://127.0.0.1",   # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
                  transport=tr, prefix="console/api")
    c.login("selftest@example.invalid", "not-a-real-password")
    return c


def _assert_refuses_provider(binding, srcs, name):
    b2 = _copy_binding(binding)
    p = [x for x in b2["providers"] if x.get("kind") == "api"][0]
    p["provider"] = name
    probs = assert_declaration_legal(b2, srcs)
    if not probs:
        raise Fail("非白名单 provider 未被拒")
    return probs


def _assert_refuses_port(binding, srcs, port):
    b2 = _copy_binding(binding)
    p = [x for x in b2["providers"] if x.get("kind") == "api"][0]
    p["port"] = port
    # ★ R-58：**只改端口**、保留该 provider 自己声明的主机 —— 原写法把主机也一并换成常量，
    #   于是「host 不在白名单」与「port 不在白名单」两条拒绝理由同时命中，
    #   本负控对**端口**这一条的判别力被稀释（门禁判别力比门禁本身更重要）。
    p["base"] = "%s:%d" % (str(p["base"]).rsplit(":", 1)[0], port)
    probs = assert_declaration_legal(b2, srcs)
    if not probs:
        raise Fail("白名单外端口未被拒")
    return probs


def _assert_refuses_tool(binding, srcs, tool):
    """把 tool 加进 kgt provider 的 tools 后必须被拒（tool 需已在 kgt 源里）。"""
    b2 = _copy_binding(binding)
    p = _kgt_provider(b2)
    p["tools"] = list(p["tools"]) + [tool]
    p["expected_tool_count"] = len(p["tools"])
    probs = assert_declaration_legal(b2, srcs)
    if not probs:
        raise Fail("禁用工具名 %s 未被拒" % tool)
    return probs


def _assert_refuses_opid(binding, srcs):
    """合成一条名为 denied_collect_now 的 kgt 源条目并声明它 ⇒ 必须被拒。"""
    s2 = copy.deepcopy(srcs)  # kgt 源里有 YAML date 对象 ⇒ 不能用 json 往返
    s2["kgt"]["tools"].append({
        "tool_name": "denied_collect_now", "method": "post", "path": "/collect2",
        "summary": "自检用合成条目",
        "request": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
    })
    b2 = _copy_binding(binding)
    p = _kgt_provider(b2)
    p["tools"] = list(p["tools"]) + ["denied_collect_now"]
    p["expected_tool_count"] = len(p["tools"])
    probs = assert_declaration_legal(b2, s2)
    if not probs:
        raise Fail("禁用 operationId 未被拒")
    return probs


def _kgt_provider(b2):
    return [x for x in b2["providers"] if x.get("kind") == "api" and x["tools_from"] == "kgt"][0]


def _assert_forbidden_caught(binding, declared):
    d = [x for x in declared if x["provider"]["carrier"] == "kg"][0]
    texts = main_face_texts(binding, d["provider"], d["schema"])
    texts.append(("injected", "这里提到本体与图谱"))
    hits = forbidden_hits(binding, texts)
    if not hits:
        raise Fail("禁用词未被捕获")
    return hits


def _assert_doc_field_caught(binding, declared):
    d = [x for x in declared if x["provider"]["carrier"] == "kg"][0]
    s = json.loads(json.dumps(d["schema"]))
    # ★ FIX-1：真零参工具（cc_guide / cc_schema）现在 properties={}，不能再假设
    #   「第 1 个 operation 必有属性」；改为挑第一个**确有属性**的 operation。
    #   判据不变：注入文档字段仍必须被 assert_no_doc_fields_in_schema 捕获。
    cands = [it["post"] for it in s["paths"].values()
             if it["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]]
    if not cands:
        raise Fail("前置条件不成立：注册面没有任何带属性的 operation，无法构造注入")
    props = cands[0]["requestBody"]["content"]["application/json"]["schema"]["properties"]
    first = list(props)[0]
    props[first]["carrier_specific_value_semantics"] = {"kg": "Cypher 只读查询串"}
    hits = assert_no_doc_fields_in_schema(binding, s, "injected")
    if not hits:
        raise Fail("文档字段未被捕获")
    return hits


def _assert_twin_diff_caught(binding, srcs):
    """★ 两侧必须各建一份 specs，只改其中一份，才能构造出真实的单侧差异。"""
    b2 = _copy_binding(binding)
    provs = [x for x in b2["providers"] if x.get("kind") == "api"]
    specs_a = project_setb(srcs["setb"], b2)
    specs_b = project_setb(srcs["setb"], b2)
    for s in specs_b:
        if s["name"] == "cc_trace":
            s["description"] = s["description"] + "（自检构造的差异）"
    sa = build_schema(b2, provs[0], specs_a)
    sb = build_schema(b2, provs[1], specs_b)
    ha = C.sha256_bytes(C.stable_json(neutralize_base(sa, provs[0]["base"])).encode())
    hb = C.sha256_bytes(C.stable_json(neutralize_base(sb, provs[1]["base"])).encode())
    if ha == hb:
        raise Fail("构造出的两侧差异未被捕获")
    return True


def _assert_count_mismatch_caught(binding, srcs):
    b2 = _copy_binding(binding)
    # ★ S2c：真值已是 8（本批把 read_facts 登记进 Set B）⇒ 负控必须用「真值+1」，
    #   否则该负控变成空操作（原 8 恰好等于真值）。
    # ★ A′（2026-10-15）：本负控针对的是「**契约**工具总数」那条检查 ⇒ 必须改 `setb_contract_tools`
    #   （改 `setb_tools` 只会打中 Q 臂绑定数那条，契约总数那条变成空操作）。
    b2["counts"]["setb_contract_tools"] = len(project_setb(srcs["setb"], b2)) + 1
    probs = assert_declaration_legal(b2, srcs)
    if not probs:
        raise Fail("counts 不一致未被捕获")
    return probs


def _assert_fingerprint_caught(binding):
    b2 = _copy_binding(binding)
    rel = list(b2["meta"]["tool_source_fingerprints"])[0]
    b2["meta"]["tool_source_fingerprints"][rel]["sha256"] = "0" * 64
    if not verify_tool_sources(b2):
        raise Fail("指纹不符未被捕获")
    return True


def _copy_binding(binding):
    return json.loads(json.dumps(binding))


def _tamper_schema_desc(entity):
    s = json.loads(entity["schema"])
    op = list(s["paths"].values())[0]["post"]
    op["description"] = op["description"] + "（被篡改）"
    entity["schema"] = json.dumps(s, ensure_ascii=False)


def _tamper_llm_desc(entity):
    # ★ FIX-1：真零参工具（cc_guide）parameters 为空，不能假设「第 1 个工具必有参数」；
    #   改为篡改第一个**确有参数**的工具。判据不变：leg B 必须抓住参数描述改动。
    for t in entity["tools"]:
        if t["parameters"]:
            t["parameters"][0]["llm_description"] = "被篡改"
            return
    raise Fail("前置条件不成立：注册面没有任何带参数的工具，无法构造 leg B 篡改")


def _tamper_add_denied(entity):
    entity["tools"].append({"operation_id": "denied_cc_query", "server_url": "x", "parameters": [],
                            "openapi": {}, "method": "post", "summary": ""})


def _baseline_with_ghost():
    import tempfile

    p = Path(tempfile.gettempdir()) / "m4_w1_selftest_baseline.json"
    p.write_text(json.dumps({"providers": [{"name": "ghost-provider", "id": "g1"}]}), encoding="utf-8")
    return str(p)


def cmd_selftest(args) -> int:
    binding, srcs = load_declaration(args.binding)
    collect_error = None
    declared = []
    try:
        declared = collect_declared(binding, srcs)
    except Fail as exc:
        # 引用解析/投影硬失败时仍要跑完自检（把根因单独打出来，不吞掉其余用例）。
        collect_error = exc
        print("★ 声明面无法构建注册面（引用解析/投影硬失败）：%s" % exc)
    tests = _tests(binding, srcs, declared)
    if collect_error is not None:
        tests.insert(0, ("P00 声明面可构建（collect_declared）", _reraise(collect_error)))
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  %s" % name)
            passed += 1
        except Exception as exc:
            print("FAIL  %s  ->  %s: %s" % (name, type(exc).__name__, exc))
            failed += 1
    print("\nSELFTEST  total=%d  PASS=%d  FAIL=%d" % (len(tests), passed, failed))
    return 0 if failed == 0 and passed >= 10 else 1


def _reraise(exc):
    def fn():
        raise exc

    return fn

# ===========================================================================
def build_parser():
    ap = argparse.ArgumentParser(description="M4.2/W1 · Dify 工具注册与三样比对")
    ap.add_argument("--binding", default=str(BINDING_PATH))
    ap.add_argument("--check", action="store_true", help="只读比对（默认）")
    ap.add_argument("--apply", action="store_true", help="按需 add / update")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    ap.add_argument("--snapshot-list", metavar="OUT.json", help="抓 tool-providers 名单基线")
    ap.add_argument("--baseline", metavar="IN.json", help="与基线比对：只允许新增")
    return ap


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    try:
        if args.selftest:
            return cmd_selftest(args)
        if args.snapshot_list:
            return cmd_snapshot_list(args)
        if args.apply:
            return cmd_apply(args)
        return cmd_check(args)
    except RefError as exc:
        print("RESULT: FAIL（声明面引用解析硬失败）\n  %s" % exc)
        return 1
    except Fail as exc:
        print("RESULT: FAIL（%s）" % exc)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
