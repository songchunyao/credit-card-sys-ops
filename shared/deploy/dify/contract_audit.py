#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4.6 契约门禁 M4-C1 … M4-C9（R-36 指定路径 `shared/deploy/dify/contract_audit.py`）。

判据权威定义
------------
`docs/05-编排层设计（Dify）.md` §6.4「失败模式与处理（逐条对应既有实测）」（正文 :391-401）
与 §6.5（:403-411，`TOKEN_BUDGET = 20000`、`approx_tokens(s) = len(s)/3.5`）；§7 步骤 8（:426）
要求「契约门禁（含 M4-C7–M4-C9，**不跳过**）| contract_audit.py | 全过」。
九条语义同时对齐 §6.4 的表格行：:395 熔断 / :396 载荷爆炸 / :398 空结果 / :399 静默回落 /
:400 静默截断 / :401 配置漂移（M4-C7–C9）。★ docs/05 **没有**单列 M4-C2 与 M4-C5 的正文行，
其语义来自 `<workspace>\\absorb-wt\\dify\\contract_audit.py` 文件头 :6-11，报告须如实说明。

编号纪律（R-35）
----------------
本项目一律写 `M4-Cn`；本模块内不得把裸 `C1`…`C9` 当作判据标识（会与 `docs/00 §3.1`
的项目级 C1–C6 混淆）。判据 id 常量一律形如 `M4-C1`。

与既有实现的差异（本项目口径，禁止照搬）
----------------------------------------
不照搬 `<workspace>\\absorb-wt\\dify\\contract_audit.py`：其端点指 8187/8188 的 `/tools/*`，
M4-C7 硬编码端口白名单 `("8187","8188")`，M4-C2/M4-C6/M4-C9 只进 `warns`，且缺凭据时
`:305-306` **静默跳过** M4-C7–C9。本项目：
  ① M4-C7 端口白名单来自 `binding.yaml` 的 `whitelist.allowed_ports/allowed_hosts`，
     并要求逐个等于该 provider 自己声明的 `port`；
  ② 缺 Console 凭据 / Console 不可用 ⇒ M4-C7/C8/C9 判 **BLOCKED**（三态），绝不静默 PASS；
  ③ 三态退出码：0=全过、1=真差异 FAIL、2=BLOCKED（缺前置，不等于 FAIL）。

只读保证
--------
只发 GET 与幂等只读 POST（三个 API 工具服务的已声明只读端点 + /health，以及 Console GET）。
`NEVER_PROBE` 显式排除写端点（`/collect` = 让采集层重采并写回同一张图）。

用法
----
    python -X utf8 shared/deploy/dify/contract_audit.py --check
    python -X utf8 shared/deploy/dify/contract_audit.py --selftest
    python -X utf8 shared/deploy/dify/contract_audit.py --check --json tmp/out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
BINDING_PATH = HERE / "binding.yaml"
KGT_TOOLS_PATH = REPO_ROOT / "shared" / "model" / "kgt-tools.yaml"

# ---------------------------------------------------------------- 门禁常数
TOKEN_BUDGET = 20000          # docs/05 §6.4/§6.5
TOKENS_PER_CHAR = 3.5         # docs/05 §6.5 approx_tokens(s) = len(s)/3.5
ROW_CAP = 200                 # shared/model/tools-setb.yaml bounds.row_cap
BYTE_CAP = 20000              # shared/model/tools-setb.yaml bounds.byte_cap
ENVELOPE_FIELDS = ("hint", "truncated", "droppedRows", "droppedBytes")
# (label, regex, case_insensitive)。★ 上游熔断指令的签名是**大写** `STOP`（docs/05 §6.4 :395
# 原文「STOP、禁止再调用任何工具」）⇒ 大小写敏感匹配，避免把普通英文 `stopwatch`/`stop-point`
# 误判为泄漏（误报负控见 --selftest）。
LEAK_PATTERNS = (
    ("禁止再调用", "禁止再调用", False),
    ("禁止调用任何工具", "禁止调用任何工具", False),
    ("STOP(大写熔断指令)", r"\bSTOP\b", False),
    ("stop calling", r"stop\s+calling", True),
    ("no further tool", r"no\s+further\s+tool", True),
    ("circuit break", r"circuit\s+break", True),
)
NEVER_PROBE = ("/collect", "/validate")
LIGHT_PROBE = {
    "kg": {"path": "/guide", "payload": {}},
    "onto": {"path": "/guide", "payload": {}},
    "kgt": {"path": "/metrics", "payload": {"objects": []}},
    # ★ S2c：中性取数工具服务的只读轻探针 = 形状契约 shared/model/live-facts-tools.yaml
    #   transport.path（POST /tools/read_facts）；max_records/max_bytes=0 是该契约声明的
    #   「不返回任何行」形态，是 8304 上最轻的只读调用。
    "live-facts": {"path": "/tools/read_facts", "payload": {"max_records": 0, "max_bytes": 0}},
}
FACTS_PROBE_OBJ = "__m4_contract_audit__"
# ★ S2c：只有「实现 KgFacts 信封（POST /facts + objectType）」的载体才跑 M4-C3/C4/C5 的
#   /facts 专用探针；其余载体（如 live-facts，端点为 /tools/read_facts）若被塞进 /facts
#   探针，会在真机上产生**假红**。该集合是各载体形状契约的投影，不是放宽：
#   集合内载体的探针缺失仍照旧 BLOCKED，断言一字不改。
FACTS_FAMILY_CARRIERS = ("kg", "onto", "kgt")
CARRIER_COUNT_KEY = {"kg": "setb_tools", "onto": "setb_tools",
                     "kgt": "kgt_main_tools", "kgt-free": "kgt_free_tools",
                     "live-facts": "livefacts_tools"}
PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"


class Fail(Exception):
    """本地前置条件不成立（声明面/参数问题），不是判据结论。"""


class ConsoleUnavailable(Exception):
    """Console 侧不可用（含未配置凭据）⇒ M4-C7/C8/C9 判 BLOCKED（fail-closed）。"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- 声明面
def load_yaml(path):
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_binding(path=BINDING_PATH):
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise Fail("binding.yaml 顶层不是映射：%s" % path)
    return data


def declared_providers(binding):
    provs = binding.get("providers") or []
    if isinstance(provs, dict):
        return dict(provs)
    out = {}
    for p in provs:
        if isinstance(p, dict) and p.get("provider"):
            out[p["provider"]] = p
    return out


def allowed_ports(binding):
    return [int(x) for x in (((binding.get("whitelist") or {}).get("allowed_ports")) or [])]


def allowed_hosts(binding):
    return [str(x) for x in (((binding.get("whitelist") or {}).get("allowed_hosts")) or [])]


def compare_pairs(binding):
    return ((binding.get("fairness") or {}).get("compare_pairs") or [])


def expected_tool_count(binding, cfg):
    carrier = cfg.get("carrier")
    key = CARRIER_COUNT_KEY.get(carrier, "setb_tools")
    counts = binding.get("counts") or {}
    want = counts.get(key)
    declared = len(cfg.get("tools") or [])
    if want is not None and want != declared:
        raise Fail("声明自相矛盾：counts.%s=%s 但 tools 有 %d 个" % (key, want, declared))
    return declared


def main_comparison_arms(binding):
    """主对比对两侧的 App 名（binding.fairness.compare_pairs[0]）。"""
    pair = (compare_pairs(binding) or [[]])[0]
    return [str(x) for x in (pair or [])]


def app_declared_providers(binding, app_name, cfg):
    """App 声明的 provider 集合（S2c 多 provider 语义；单一真源 = binding.apps.<app>）。

    规则与 shared/deploy/dify/deploy_agents.py 的同名访问器逐字一致：
      * `providers:` 是权威集合；`provider:` 若同时存在必须 == providers[0]
        （两个键各说一套 ⇒ 不是单一真源 ⇒ Fail）；
      * 只有 `provider:` ⇒ [provider]；
      * **主对比对两侧必须恰好 1 个**（S2c：不得放宽）；
      * 集合里每个名字都必须能在 binding.providers 里解析到登记项。
    """
    provs = cfg.get("providers")
    single = cfg.get("provider")
    if provs is not None:
        if not isinstance(provs, list) or not provs:
            raise Fail("binding.apps.%s 的 providers 必须是非空列表：%r" % (app_name, provs))
        out = [str(x) for x in provs]
        if len(set(out)) != len(out):
            raise Fail("binding.apps.%s 的 providers 有重复项：%s" % (app_name, out))
        if single is not None and str(single) != out[0]:
            raise Fail("binding.apps.%s 的 provider=%s 与 providers[0]=%s 不一致（两个键各说一套）"
                       % (app_name, single, out[0]))
    else:
        if single is None:
            raise Fail("binding.apps.%s 未声明 provider/providers" % app_name)
        out = [str(single)]
    if app_name in main_comparison_arms(binding) and len(out) != 1:
        raise Fail("主对比对 App %s 必须恰好声明 1 个 provider，实际 %d 个：%s"
                   % (app_name, len(out), out))
    decls = declared_providers(binding)
    for name in out:
        if name not in decls:
            raise Fail("binding.apps.%s 声明的 provider %s 未在 binding.providers 里登记"
                       % (app_name, name))
    return out


def provider_tool_names(cfg):
    """某个 provider 声明里属于它的工具名（api 用 tools，plugin 用 bind_tools）。"""
    return [str(x) for x in (list(cfg.get("tools") or []) + list(cfg.get("bind_tools") or []))]


def tool_owner_provider(binding, app_name, cfg, tool_name, decls=None):
    """工具名 → 声明集合中**恰好一个** provider；0 个或 ≥2 个 ⇒ Fail（fail-closed）。"""
    decls = decls if decls is not None else declared_providers(binding)
    name_set = app_declared_providers(binding, app_name, cfg)
    owners = [n for n in name_set if tool_name in provider_tool_names(decls.get(n) or {})]
    if len(owners) != 1:
        raise Fail("App %s 的工具 %s 在声明集合 %s 里归属 %d 个 provider（必须恰好 1）"
                   % (app_name, tool_name, name_set, len(owners)))
    return owners[0]


def plugin_declaration(path=KGT_TOOLS_PATH):
    """shared/model/kgt-tools.yaml 里 neo4j_query 的入参声明（真机 schema 的比对基准）。"""
    data = load_yaml(path)
    for entry in (data.get("tools") or []):
        if entry.get("tool_name") == "neo4j_query":
            req = entry.get("request") or {}
            props = req.get("properties") or {}
            reqd = set(req.get("required") or [])
            out = {"tool_name": "neo4j_query"}
            for name, spec in props.items():
                item = {"type": spec.get("type"),
                        "required": bool(spec.get("required", False)) or name in reqd}
                if "default" in spec:
                    item["default"] = spec["default"]
                out[name] = item
            return out
    raise Fail("shared/model/kgt-tools.yaml 未声明 neo4j_query")


def plugin_tool_extra(path=KGT_TOOLS_PATH):
    """shared/model/kgt-tools.yaml 里 neo4j_query 工具条的身份面 + param_form（R-50 fork 声明）。

    W12/R-50 起 binding.yaml 可以同时声明多个插件（市场 0.2.0 与 cc-ops fork 并存），
    因此「插件面」不再是单一全局量。本函数给出模型契约侧的对照值，让 M4-C7 能断言
    binding 的 param_form 与 shared/model/kgt-tools.yaml **逐参相等**（声明间无声漂移也要红）。
    """
    data = load_yaml(path)
    for entry in (data.get("tools") or []):
        if entry.get("tool_name") == "neo4j_query":
            return {"provider_id": entry.get("provider_id"),
                    "plugin_version": str(entry.get("plugin_version")),
                    "param_form": dict(entry.get("param_form") or {})}
    raise Fail("shared/model/kgt-tools.yaml 未声明 neo4j_query")


def plugin_faces(snap):
    """快照里**每一个**被声明的插件面（R-50 后可能 >1），按声明名排序。

    缺 snap["plugins"] 时退化为旧的单一 snap["plugin"]（兼容只喂旧快照的调用方）。
    """
    plugs = snap.get("plugins")
    if isinstance(plugs, dict) and plugs:
        return [plugs[k] for k in sorted(plugs)]
    plug = snap.get("plugin")
    return [plug] if plug else []


# ---------------------------------------------------------------- HTTP 探针
class ProbeResp(object):
    __slots__ = ("status", "text", "error")

    def __init__(self, status, text, error=None):
        self.status = status
        self.text = text or ""
        self.error = error

    @property
    def nbytes(self):
        return len(self.text.encode("utf-8", "replace"))

    @property
    def nchars(self):
        return len(self.text)


class UrlProbe(object):
    """真机只读探针；异常一律收敛为 `ProbeResp(0, "", error=…)`，绝不抛出。"""

    def request(self, method, url, payload=None, timeout=20):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method.upper(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return ProbeResp(int(resp.status), resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return ProbeResp(int(exc.code), body)
        except Exception as exc:  # noqa: BLE001
            return ProbeResp(0, "", error="%s: %s" % (type(exc).__name__, str(exc)[:160]))


def leak_hits(text):
    out = []
    for label, pat, ci in LEAK_PATTERNS:
        try:
            hit = bool(re.search(pat, text, re.IGNORECASE if ci else 0))
        except re.error:
            hit = pat in text
        if hit:
            out.append(label)
    return out


def probe_summary(resp):
    """把探针响应压成不含原始正文的证据摘要（原文绝不落盘）。"""
    env, obj = None, None
    try:
        obj = json.loads(resp.text)
    except Exception:
        obj = None
    if isinstance(obj, dict):
        env = {k: obj[k] for k in ENVELOPE_FIELDS if k in obj} or None
    rows = None
    caps = {}
    error_code = None
    has_message = False
    if isinstance(obj, dict):
        # 行数既可能是列表（取 len）也可能是整数计数（如 items=0）——两种形态都要认，
        # 否则「200 且 0 行但 hint 为空」这类空结果会漏判（负控 空结果无 hint 即此）。
        for key in ("items", "rows", "records", "data", "rowCount"):
            val = obj.get(key)
            if isinstance(val, list):
                rows = len(val)
                break
            if isinstance(val, int) and not isinstance(val, bool):
                rows = val
                break
        for key in ("rowCap", "byteCap"):
            if isinstance(obj.get(key), int) and not isinstance(obj.get(key), bool):
                caps[key] = obj[key]
        for key in ("errorCode", "error_code"):
            if key in obj and isinstance(obj[key], (str, int)):
                error_code = obj[key]
                break
        if error_code is None and isinstance(obj.get("error"), str):
            error_code = obj["error"]
        has_message = any(isinstance(obj.get(k), str) and obj[k].strip()
                          for k in ("message", "detail", "msg"))
    hint = (env or {}).get("hint")
    return {
        "status": resp.status,
        "bytes": resp.nbytes,
        "chars": resp.nchars,
        "tokens": round(resp.nchars / TOKENS_PER_CHAR, 1),
        "sha256_16": hashlib.sha256(resp.text.encode("utf-8", "replace")).hexdigest()[:16],
        "envelope": env,
        "rows": rows,
        "caps": caps,
        "hint_len": len(hint) if isinstance(hint, str) else None,
        "error_code": error_code,
        "has_message": has_message,
        "leak_hits": leak_hits(resp.text),
        "error": resp.error,
    }


def run_tool_probes(binding, probe=None, timeout=20):
    """对三个 API 工具服务发只读探针；返回 {carrier: {...}}（不含原始正文）。"""
    probe = probe or UrlProbe()
    out = {}
    for name, cfg in sorted(declared_providers(binding).items()):
        if (cfg.get("kind") or "api") != "api":
            continue
        base = (cfg.get("base") or "").rstrip("/")
        if not base:
            continue
        carrier = cfg.get("carrier") or name
        light = LIGHT_PROBE.get(carrier) or {"path": "/facts", "payload": {"objectType": FACTS_PROBE_OBJ}}
        if light["path"] in NEVER_PROBE:
            raise Fail("探针端点 %s 在 NEVER_PROBE 里" % light["path"])
        entry = {"provider": name, "carrier": carrier, "base": base, "port": cfg.get("port"),
                 "declared_tools": list(cfg.get("tools") or []),
                 "probe_family": ("kgfacts" if carrier in FACTS_FAMILY_CARRIERS else "tool"),
                 "light_path": light["path"], "light": [], "other": {}}
        for _i in range(3):  # M4-C1：同参数连调 3 次（审计侧负控，不是评测调用）
            entry["light"].append(probe_summary(
                probe.request("POST", base + light["path"], light["payload"], timeout=timeout)))
        if carrier in FACTS_FAMILY_CARRIERS:
            specs = {
                "facts_empty": {"objectType": FACTS_PROBE_OBJ},
                "facts_missing_required": {},
                "facts_unknown_param": {"objectType": FACTS_PROBE_OBJ, "m4_unknown_param_probe": 1},
            }
            for key, payload in specs.items():
                s = probe_summary(probe.request("POST", base + "/facts", payload, timeout=timeout))
                s["path"] = "/facts"
                s["payload_keys"] = sorted(payload.keys())
                entry["other"][key] = s
        entry["health"] = probe_summary(probe.request("GET", base + "/health", None, timeout=timeout))
        out[carrier] = entry
    return out


# ---------------------------------------------------------------- Console 快照
def _cget(console, path):
    """GET Console；返回 (status, obj|None)。传输层/鉴权失败 ⇒ ConsoleUnavailable。

    ★ common.Console.url(path) 是 `base + path` 直接拼接（不补 prefix），调用方必须给
    完整的 `/console/api/...` 路径，否则请求会落到站点根路径并得到 404。
    """
    prefix = "/" + str(getattr(console, "prefix", "console/api")).strip("/")
    if not path.startswith(prefix + "/"):
        path = prefix + path
    try:
        resp = console.request("GET", path, json_body=None, expect=())
    except Exception as exc:  # noqa: BLE001
        raise ConsoleUnavailable("%s: %s" % (type(exc).__name__, str(exc)[:200]))
    status = getattr(resp, "status_code", 0)
    if status in (401, 403):
        raise ConsoleUnavailable("Console 鉴权失败（HTTP %s）" % status)
    if not status or status >= 500:
        raise ConsoleUnavailable("Console 请求失败（HTTP %s）" % status)
    try:
        return status, json.loads(resp.text)
    except Exception:
        return status, None


def _urlsplit_port(url):
    m = re.match(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?P<host>[^/:]+)(?::(?P<port>\d+))?", url or "")
    if not m:
        return None, None
    port = int(m.group("port")) if m.group("port") else (443 if m.group("scheme") == "https" else 80)
    return m.group("host"), port


def console_providers(console):
    """{by_id, by_name, by_plugin_id} —— 覆盖 api 与 builtin 两类（只读）。"""
    faces = {"by_id": {}, "by_name": {}, "by_plugin_id": {}}
    for ptype in ("api", "builtin"):
        status, rows = _cget(console, "/workspaces/current/tool-providers?type=%s" % ptype)
        if status != 200 or not isinstance(rows, list):
            raise ConsoleUnavailable("tool-providers?type=%s 返回 HTTP %s 且非列表" % (ptype, status))
        for row in rows:
            if not isinstance(row, dict):
                continue
            face = {"id": row.get("id"), "name": row.get("name"),
                    "type": row.get("type") or ptype, "plugin_id": row.get("plugin_id"),
                    "plugin_unique_identifier": row.get("plugin_unique_identifier")}
            if face["id"]:
                faces["by_id"][face["id"]] = face
            if face["name"]:
                faces["by_name"][face["name"]] = face
            if face["plugin_id"]:
                faces["by_plugin_id"][face["plugin_id"]] = face
            if face["plugin_unique_identifier"]:
                faces["by_id"][face["plugin_unique_identifier"]] = face
    return faces


def console_api_provider_detail(console, name, face=None):
    status, obj = _cget(console, "/workspaces/current/tool-provider/api/get?provider=%s" % name)
    out = {"exists": False, "status": status, "id": (face or {}).get("id")}
    if status != 200 or not isinstance(obj, dict):
        return out
    out["exists"] = True
    out["tools"] = len(obj.get("tools") or [])
    schema = obj.get("schema")
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except Exception:
            schema = None
    if isinstance(schema, dict):
        servers = schema.get("servers") or []
        if servers and isinstance(servers[0], dict):
            url = servers[0].get("url")
            host, port = _urlsplit_port(url)
            out["server_url"] = url
            out["server_host"] = host
            out["server_port"] = port
        out["schema_paths"] = sorted((schema.get("paths") or {}).keys())
    return out


def console_app(console, uuid):
    status, obj = _cget(console, "/apps/%s" % uuid)
    if status != 200 or not isinstance(obj, dict):
        return {"exists": False, "status": status}
    mc = obj.get("model_config") or {}
    am = mc.get("agent_mode") or {}
    tools = am.get("tools") if isinstance(am.get("tools"), list) else []
    return {"exists": True, "status": status, "name": obj.get("name"), "mode": obj.get("mode"),
            "enabled": am.get("enabled"), "tool_count": len(tools),
            "tools": [{"provider_id": t.get("provider_id"), "provider_name": t.get("provider_name"),
                       "provider_type": t.get("provider_type"), "type": t.get("type"),
                       "tool_name": t.get("tool_name"), "enabled": t.get("enabled")}
                      for t in tools if isinstance(t, dict)]}


def _mask_form(value):
    """只记录掩码形态（len/stars/reveals_first/reveals_last），绝不记录任何字符。"""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    stars = s.count("*")
    form = {"len": len(s), "stars": stars}
    if stars:
        lead = 0
        while lead < len(s) and s[lead] != "*":
            lead += 1
        tail = 0
        while tail < len(s) and s[len(s) - 1 - tail] != "*":
            tail += 1
        form["reveals_first"] = lead
        form["reveals_last"] = tail
    return form


def console_plugin(console, decl):
    """插件面：provider 是否存在、工具入参 schema、凭据可用性。"""
    plugin_id = decl.get("plugin_id")
    out = {"plugin_id": plugin_id, "exists": False, "provider_id": None, "provider_type": None,
           "tools": [], "params": {}, "credentials": None}
    faces = console_providers(console)
    face = faces["by_plugin_id"].get(plugin_id)
    if not face and plugin_id and plugin_id in faces["by_id"]:
        # 声明把 provider_id 写进了 plugin_id（形如 author/name/tool）时的容错
        face = faces["by_id"][plugin_id]
    if not face:
        ident = decl.get("plugin_unique_identifier")
        if ident and ident in faces["by_id"]:
            face = faces["by_id"][ident]
    if not face and decl.get("provider_id") in faces["by_id"]:
        face = faces["by_id"][decl.get("provider_id")]
    if not face:
        return out
    out["exists"] = True
    out["provider_id"] = face.get("id")
    out["provider_type"] = face.get("type")
    quoted = (face.get("id") or "").replace("/", "%2F")
    status, rows = _cget(console, "/workspaces/current/tool-provider/builtin/%s/tools" % quoted)
    if status == 200 and isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            out["tools"].append(row.get("name"))
            if row.get("name") in (decl.get("bind_tools") or []):
                params = {}
                for p in (row.get("parameters") or []):
                    if isinstance(p, dict) and p.get("name"):
                        params[p["name"]] = {"type": p.get("type"),
                                             "required": bool(p.get("required", False)),
                                             "has_default": "default" in p,
                                             "default": p.get("default"),
                                             "form": p.get("form")}
                out["params"][row.get("name")] = params
    status, rows = _cget(console, "/workspaces/current/tool-provider/builtin/%s/credentials" % quoted)
    if status == 200 and isinstance(rows, list):
        recs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            creds = row.get("credentials")
            creds = creds if isinstance(creds, dict) else {}
            recs.append({"name": row.get("name"), "id": row.get("id"),
                         "credential_type": row.get("credential_type"),
                         "visibility": row.get("visibility"), "is_default": row.get("is_default"),
                         "keys": sorted(creds.keys()),
                         "form": {k: _mask_form(v) for k, v in creds.items()},
                         "_values": creds})
        out["credentials"] = recs
    return out


def build_snapshot(binding, console=None, probe=None, timeout=20, with_console=True):
    snap = {"utc": utcnow(), "tool_probes": {}, "console": {"ok": False, "error": None},
            "api_providers": {}, "apps": {}, "plugin": None, "plugins": {}, "provider_ids": []}
    snap["tool_probes"] = run_tool_probes(binding, probe=probe, timeout=timeout)
    if not with_console or console is None:
        snap["console"]["error"] = "未提供 Console 客户端"
        return snap
    try:
        faces = console_providers(console)
        plug_decl = None
        for name, cfg in declared_providers(binding).items():
            kind = cfg.get("kind") or "api"
            if kind == "api":
                d = console_api_provider_detail(console, name, face=faces["by_name"].get(name))
                d["declared_port"] = cfg.get("port")
                d["declared_base"] = cfg.get("base")
                snap["api_providers"][name] = d
            elif kind == "plugin":
                # R-50：声明可以并存多个插件（市场 0.2.0 + cc-ops fork）⇒ 逐个解析，不再只取第一个。
                plug = console_plugin(console, cfg)
                plug["declaration_name"] = name
                plug["bind_tools"] = list(cfg.get("bind_tools") or [])
                plug["declared_param_form"] = dict(cfg.get("param_form") or {})
                plug["declared_credential"] = cfg.get("credential") or {}
                snap["plugins"][name] = plug
                if plug_decl is None:
                    plug_decl = plug
        snap["plugin"] = plug_decl
        for app_name, cfg in (binding.get("apps") or {}).items():
            uuid = cfg.get("uuid")
            if not uuid:
                snap["apps"][app_name] = {"exists": False, "error": "binding 未登记 uuid"}
                continue
            row = console_app(console, uuid)
            row["carrier"] = cfg.get("carrier")
            row["provider"] = cfg.get("provider")
            row["declared_tools"] = list(cfg.get("tools") or [])
            try:
                row["declared_providers"] = app_declared_providers(binding, app_name, cfg)
            except Fail as exc:
                row["declared_providers"] = None
                row["declared_providers_error"] = str(exc)
            try:
                row["expected_tool_count"] = expected_tool_count(binding, cfg)
            except Fail as exc:
                row["expected_tool_count"] = None
                row["expected_tool_count_error"] = str(exc)
            snap["apps"][app_name] = row
        snap["provider_ids"] = sorted(faces["by_id"].keys())
        snap["console"]["ok"] = True
    except ConsoleUnavailable as exc:
        snap["console"]["error"] = str(exc)
    return snap


def redact_snapshot(snap):
    """去掉一切会泄露口令的值（只保留形态标记），并保证没有原始正文。"""
    def _scrub(obj):
        if isinstance(obj, dict):
            return {k: _scrub(v) for k, v in obj.items() if k != "_values"}
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        return obj
    return _scrub(snap)


# ---------------------------------------------------------------- 判据骨架
def rec(cid, title, basis):
    return {"id": cid, "title": title, "basis": basis, "problems": [],
            "blocked_reason": None, "recheck_by": None, "evidence": []}


def finish(r):
    r["verdict"] = BLOCKED if r["blocked_reason"] else (FAIL if r["problems"] else PASS)
    return r


def blocked(r, reason, by):
    r["blocked_reason"] = reason
    r["recheck_by"] = by
    return finish(r)


def explained(s):
    """(是否给出可读说明, 理由)。2xx 必须有非空 hint；非 2xx 必须有 hint/errorCode/message。"""
    if not s:
        return False, "无探针结果"
    status = s.get("status")
    if not status:
        return False, "探针不可达：%s" % s.get("error")
    hint = (s.get("envelope") or {}).get("hint")
    if status == 200:
        ok = bool(isinstance(hint, str) and hint.strip())
        return ok, ("2xx 且 hint 非空" if ok else "2xx 且 hint 为空")
    if isinstance(hint, str) and hint.strip():
        return True, "HTTP %s + hint" % status
    if s.get("error_code") is not None:
        return True, "HTTP %s + errorCode=%s" % (status, s["error_code"])
    if s.get("has_message"):
        return True, "HTTP %s + message" % status
    return False, "HTTP %s 但无任何可读说明" % status


# ---------------------------------------------------------------- 九条判据
def check_c1_melt_leak(binding, snap):
    r = rec("M4-C1", "熔断/STOP 指令不得泄漏进 tool 返回体",
            "docs/05 §6.4 :395（服务端熔断行）+ absorb contract_audit :51-77 范式（同参数连调 3 次后扫描）")
    for carrier, e in sorted(snap["tool_probes"].items()):
        for i, s in enumerate(e["light"] or []):
            if not s.get("status"):
                return blocked(r, "%s %s 探针不可达：%s" % (carrier, e["light_path"], s.get("error")),
                               "工具服务可达后重跑")
            if s.get("leak_hits"):
                r["problems"].append("%s 第 %d/3 次 %s 返回体命中熔断措辞 %s（sha=%s）"
                                     % (carrier, i + 1, e["light_path"], s["leak_hits"], s["sha256_16"]))
        r["evidence"].append({"carrier": carrier, "path": e["light_path"], "calls": 3,
                              "max_bytes": max([s.get("bytes", 0) for s in e["light"]] or [0]),
                              "leak_hits": sorted({p for s in e["light"] for p in (s.get("leak_hits") or [])}),
                              "sha256_16": [s.get("sha256_16") for s in e["light"]]})
    return finish(r)


def check_c2_payload_budget(binding, snap):
    r = rec("M4-C2", "单次工具返回不得超过 byte_cap 与 TOKEN_BUDGET",
            "docs/05 §6.4 :396（载荷爆炸：run_rules 5,307,133 B ≈ 1,516,323 tokens）"
            " + §6.5 TOKEN_BUDGET=20000、approx_tokens(s)=len(s)/3.5；语义另见 absorb :6-11")
    worst = None
    for carrier, e in sorted(snap["tool_probes"].items()):
        probes = list(e["light"] or []) + list((e["other"] or {}).values())
        for s in probes:
            if s.get("status") != 200:
                continue
            if worst is None or s.get("tokens", 0) > worst.get("tokens", 0):
                worst = dict(s, carrier=carrier)
            if s.get("bytes", 0) > BYTE_CAP:
                r["problems"].append("%s 返回体 %d B > byte_cap %d（sha=%s）"
                                     % (carrier, s["bytes"], BYTE_CAP, s["sha256_16"]))
            if s.get("tokens", 0) > TOKEN_BUDGET:
                r["problems"].append("%s 返回体 %.1f tokens > TOKEN_BUDGET %d（sha=%s）"
                                     % (carrier, s["tokens"], TOKEN_BUDGET, s["sha256_16"]))
        caps = (e.get("health") or {}).get("caps") or {}
        if (e.get("health") or {}).get("status") == 200:
            if caps.get("rowCap") not in (None, ROW_CAP):
                r["problems"].append("%s /health 自述 rowCap=%s ≠ 声明 %d" % (carrier, caps.get("rowCap"), ROW_CAP))
            if caps.get("byteCap") not in (None, BYTE_CAP):
                r["problems"].append("%s /health 自述 byteCap=%s ≠ 声明 %d" % (carrier, caps.get("byteCap"), BYTE_CAP))
            r["evidence"].append({"carrier": carrier, "health_caps": caps})
    if worst is None:
        return blocked(r, "没有任何 2xx 只读返回体可用于载荷预算取证", "工具服务可达且返回 2xx 后重跑")
    r["evidence"].append({"worst_probe": {k: worst.get(k) for k in ("carrier", "bytes", "tokens", "sha256_16")},
                          "token_budget": TOKEN_BUDGET, "byte_cap": BYTE_CAP})
    return finish(r)


def check_c3_silent_fallback(binding, snap):
    r = rec("M4-C3", "未识别参数必须显式报错，不得静默回落默认值",
            "docs/05 §6.4 :399（请求不存在的 rule_set 时 run_rules 曾静默回落 default）"
            " + shared/model/tools-setb.yaml error_codes INVALID_PARAM")
    for carrier, e in sorted(snap["tool_probes"].items()):
        if carrier not in FACTS_FAMILY_CARRIERS:
            r["evidence"].append({"carrier": carrier, "probe_family": e.get("probe_family"),
                                  "not_applicable": "该载体不实现 KgFacts 面（POST /facts + objectType）"})
            continue
        s = (e["other"] or {}).get("facts_unknown_param")
        if not s or not s.get("status"):
            return blocked(r, "%s 的未识别参数探针不可达" % carrier, "工具服务可达后重跑")
        said, why = explained(s)
        if not said:
            r["problems"].append("%s /facts 收到未识别参数后返回 HTTP %s 且无任何说明（疑似静默接受/回落默认值）"
                                 % (carrier, s.get("status")))
        r["evidence"].append({"carrier": carrier, "path": "/facts", "status": s.get("status"),
                              "explained": said, "why": why, "sha256_16": s.get("sha256_16")})
    return finish(r)


def check_c4_empty_result_hint(binding, snap):
    r = rec("M4-C4", "空结果必须带 hint（否则模型会把「没返回」读成「不存在」）",
            "docs/05 §6.4 :398 与 :123（空结果必须有 hint）")
    for carrier, e in sorted(snap["tool_probes"].items()):
        if carrier not in FACTS_FAMILY_CARRIERS:
            r["evidence"].append({"carrier": carrier, "probe_family": e.get("probe_family"),
                                  "not_applicable": "该载体不实现 KgFacts 面（POST /facts + objectType）"})
            continue
        s = (e["other"] or {}).get("facts_empty")
        if not s or not s.get("status"):
            return blocked(r, "%s 的空结果探针不可达" % carrier, "工具服务可达后重跑")
        if s.get("status") == 200:
            hint_len = s.get("hint_len")
            if s.get("rows") == 0 and not hint_len:
                r["problems"].append("%s 返回 200 且 0 行但 hint 为空（sha=%s）" % (carrier, s.get("sha256_16")))
            r["evidence"].append({"carrier": carrier, "status": 200, "rows": s.get("rows"),
                                  "hint_len": hint_len, "sha256_16": s.get("sha256_16")})
        else:
            said, why = explained(s)
            if not said:
                r["problems"].append("%s 空结果返回 HTTP %s 且无说明" % (carrier, s.get("status")))
            r["evidence"].append({"carrier": carrier, "status": s.get("status"),
                                  "explained": said, "why": why, "sha256_16": s.get("sha256_16")})
    return finish(r)


def check_c5_input_contract(binding, snap):
    r = rec("M4-C5", "缺必填参数必须说明原因（不得静默回落，也不得 5xx）",
            "docs/05 §6.4 :399/:400 + absorb :6-11（缺参数须说明原因）；"
            "shared/model/tools-setb.yaml error_codes INVALID_PARAM")
    for carrier, e in sorted(snap["tool_probes"].items()):
        if carrier not in FACTS_FAMILY_CARRIERS:
            r["evidence"].append({"carrier": carrier, "probe_family": e.get("probe_family"),
                                  "not_applicable": "该载体不实现 KgFacts 面（POST /facts + objectType）"})
            continue
        s = (e["other"] or {}).get("facts_missing_required")
        if not s or not s.get("status"):
            return blocked(r, "%s 的缺必填探针不可达" % carrier, "工具服务可达后重跑")
        if s["status"] >= 500:
            r["problems"].append("%s 缺必填参数返回 HTTP %s（契约要求显式 4xx + 说明）" % (carrier, s["status"]))
        else:
            said, _why = explained(s)
            if not said:
                r["problems"].append("%s 缺必填参数返回 HTTP %s 且无说明" % (carrier, s.get("status")))
        r["evidence"].append({"carrier": carrier, "path": "/facts", "status": s.get("status"),
                              "explained": explained(s)[0], "sha256_16": s.get("sha256_16")})
    return finish(r)


def check_c6_truncation_accounting(binding, snap):
    r = rec("M4-C6", "截断必须回传丢弃量（四字段信封齐备，不得静默截断）",
            "docs/05 §6.4 :396 与 :400（search_logs size=1000 静默截断）"
            " + shared/model/tools-setb.yaml return_envelope(4 字段, applies_to all_tools)")
    for carrier, e in sorted(snap["tool_probes"].items()):
        probes = [("light", s) for s in (e["light"] or [])] + list((e["other"] or {}).items())
        seen = 0
        for tag, s in probes:
            if s.get("status") != 200:
                continue
            seen += 1
            env = s.get("envelope")
            if not env:
                r["problems"].append("%s %s 返回 200 但缺 4 字段信封（hint/truncated/droppedRows/droppedBytes）"
                                     % (carrier, tag))
                continue
            for f in ENVELOPE_FIELDS:
                if f not in env:
                    r["problems"].append("%s %s 信封缺字段 %s" % (carrier, tag, f))
            trunc = env.get("truncated")
            dr, db = env.get("droppedRows"), env.get("droppedBytes")
            if not isinstance(trunc, bool):
                r["problems"].append("%s %s envelope.truncated 不是布尔（%r）" % (carrier, tag, trunc))
            if trunc is True:
                if not isinstance(dr, int) or not isinstance(db, int):
                    r["problems"].append("%s %s truncated=true 但 droppedRows/droppedBytes 不是整数" % (carrier, tag))
                elif dr + db <= 0:
                    r["problems"].append("%s %s truncated=true 却未回传任何丢弃量（静默截断）" % (carrier, tag))
                elif not s.get("hint_len"):
                    r["problems"].append("%s %s truncated=true 但 hint 为空（模型无法得知返回不完整）" % (carrier, tag))
        r["evidence"].append({"carrier": carrier, "2xx_probes": seen,
                              "truncated_observed": any((s.get("envelope") or {}).get("truncated") is True
                                                        for _t, s in probes)})
        if seen == 0:
            return blocked(r, "%s 无可用于截断记账取证的 2xx 返回" % carrier, "工具服务可达后重跑")
    return finish(r)


def check_c7_provider_schema(binding, snap):
    r = rec("M4-C7", "provider schema 的 server_url 必须指向白名单活端口；插件入参 schema 须与声明一致",
            "docs/05 §6.4 :401（配置漂移：provider schema 指向未知端口会让漂移永久潜伏）"
            " + binding.whitelist.allowed_ports/allowed_hosts")
    if not snap["console"]["ok"]:
        return blocked(r, "Console 不可用：%s" % snap["console"]["error"],
                       "Console 凭据/网络可用后重跑（M4-C7–C9 不得跳过）")
    ports, hosts = allowed_ports(binding), allowed_hosts(binding)
    for name, d in sorted(snap["api_providers"].items()):
        if not d.get("exists"):
            r["problems"].append("provider %s 在 Console 侧不存在（HTTP %s）" % (name, d.get("status")))
            continue
        url, host, port = d.get("server_url"), d.get("server_host"), d.get("server_port")
        r["evidence"].append({"provider": name, "server_url": url, "host": host, "port": port,
                              "declared_port": d.get("declared_port"),
                              "schema_paths": len(d.get("schema_paths") or [])})
        if not url:
            r["problems"].append("provider %s 的 schema.servers[0].url 缺失" % name)
            continue
        if ports and port not in ports:
            r["problems"].append("provider %s schema 指向端口 %s，不在 whitelist.allowed_ports %s 内"
                                 % (name, port, ports))
        if hosts and host not in hosts:
            r["problems"].append("provider %s schema 指向主机 %s，不在 whitelist.allowed_hosts %s 内"
                                 % (name, host, hosts))
        if d.get("declared_port") is not None and port != d.get("declared_port"):
            r["problems"].append("provider %s schema 端口 %s ≠ binding 声明 %s"
                                 % (name, port, d.get("declared_port")))
    decl = plugin_declaration()
    extra = plugin_tool_extra()
    want = {k: v for k, v in decl.items() if k != "tool_name"}
    faces = plugin_faces(snap)
    if not faces:
        r["problems"].append("binding 未声明任何 kind=plugin 的 provider（插件面不可判）")
    for plug in faces:
        dname = plug.get("declaration_name")
        want_form = dict(plug.get("declared_param_form") or {})
        r["evidence"].append({"plugin_declaration": dname, "plugin_id": plug.get("plugin_id"),
                              "exists": plug.get("exists"), "provider_id": plug.get("provider_id"),
                              "tools": plug.get("tools"),
                              "declared_param_form": want_form,
                              "live_param_names": {k: sorted(v.keys()) for k, v in (plug.get("params") or {}).items()},
                              "live_param_forms": {k: {p: v.get("form") for p, v in (vv or {}).items()}
                                                   for k, vv in (plug.get("params") or {}).items()}})
        if not plug.get("exists"):
            r["problems"].append("插件 provider %s（声明名 %s）在 Console 侧不存在"
                                 % (plug.get("plugin_id"), dname))
            continue
        if not want_form:
            r["problems"].append("插件声明 %s 未登记 param_form ⇒ form 面不可判（R-50 起每个插件声明都必须登记）"
                                 % dname)
        elif plug.get("provider_id") == extra.get("provider_id") and want_form != extra.get("param_form"):
            r["problems"].append("插件声明 %s 的 param_form %s ≠ shared/model/kgt-tools.yaml "
                                 "tools[neo4j_query].param_form %s（两份声明之间无声漂移）"
                                 % (dname, want_form, extra.get("param_form")))
        for tool in (plug.get("bind_tools") or []):
            if tool not in (plug.get("tools") or []):
                r["problems"].append("插件工具 %s 不在 provider %s 的真机工具面 %s 里"
                                     % (tool, plug.get("plugin_id"), plug.get("tools")))
            live = (plug.get("params") or {}).get(tool)
            if live is None:
                continue
            if set(live) != set(want):
                r["problems"].append("插件工具 %s 入参名集合 %s ≠ 声明 %s"
                                     % (tool, sorted(live), sorted(want)))
            for pname, wspec in want.items():
                lspec = live.get(pname)
                if not lspec:
                    continue
                if bool(lspec.get("required")) != bool(wspec.get("required")):
                    r["problems"].append("插件入参 %s.%s required=%s ≠ 声明 %s"
                                         % (tool, pname, lspec.get("required"), wspec.get("required")))
                if "default" in wspec:
                    if not lspec.get("has_default"):
                        r["problems"].append("插件入参 %s.%s 真机无 default，声明要求 %r"
                                             % (tool, pname, wspec["default"]))
                    elif lspec.get("default") != wspec["default"]:
                        r["problems"].append("插件入参 %s.%s default=%r ≠ 声明 %r"
                                             % (tool, pname, lspec.get("default"), wspec["default"]))
                if pname in want_form and (lspec.get("form") or None) != want_form[pname]:
                    r["problems"].append("插件入参 %s.%s form=%r ≠ 声明 %r（R-50：query/parameters 必须 llm，"
                                         "database/max_records/allow_write_queries 必须 form）"
                                         % (tool, pname, lspec.get("form"), want_form[pname]))
    return finish(r)


def check_c8_tool_binding(binding, snap):
    r = rec("M4-C8", "无悬空 provider_id；主对比组 provider_id 交集为空；App 工具族与声明一致",
            "docs/05 §6.4 :401（配置漂移：悬空 provider_id）"
            " + binding.fairness.compare_pairs（两主对比组按 provider_id 交集为空）")
    if not snap["console"]["ok"]:
        return blocked(r, "Console 不可用：%s" % snap["console"]["error"],
                       "Console 凭据/网络可用后重跑（M4-C7–C9 不得跳过）")
    known = set(snap.get("provider_ids") or [])
    decls = declared_providers(binding)
    plugins = snap.get("plugins") or {}
    app_providers = {}
    for app_name, a in sorted((snap.get("apps") or {}).items()):
        if not a.get("exists"):
            r["problems"].append("App %s 在 Console 侧不存在或不可读（%s）"
                                 % (app_name, a.get("error") or ("HTTP %s" % a.get("status"))))
            continue
        # ★ S2c：App 可以声明**多个** provider（免费臂 = 插件 + 中性取数工具服务）；
        #   每个已绑工具必须解析到声明集合里**恰好一个** provider 的真机面。
        #   主对比对两侧仍强制恰好 1 个（见 app_declared_providers，声明与断言双重固定）。
        #   声明面取自 binding.apps.<app>（单一真源），不是快照回读的投影
        #   （快照行只带 `provider` 单键，用它会把多 provider 声明退化成 1 个）。
        decl_cfg = (binding.get("apps") or {}).get(app_name) or {}
        try:
            decl_set = app_declared_providers(binding, app_name, decl_cfg)
        except Fail as exc:
            r["problems"].append("App %s 的 provider 声明不可判：%s" % (app_name, exc))
            decl_set = []
        r["evidence"].append({"app": app_name, "declared_providers": decl_set,
                              "declared_tools": list(a.get("declared_tools") or [])})
        ids = []
        for t in a.get("tools") or []:
            pid = t.get("provider_id")
            tname = t.get("tool_name")
            ids.append(pid)
            if pid not in known:
                r["problems"].append("App %s 的工具 %s 指向悬空 provider_id %s（不在真机 provider 面 %d 项内）"
                                     % (app_name, tname, pid, len(known)))
            if not decl_set:
                continue
            try:
                owner = tool_owner_provider(binding, app_name, decl_cfg, tname, decls)
            except Fail as exc:
                r["problems"].append(str(exc))
                continue
            pdecl = decls.get(owner) or {}
            kind = pdecl.get("kind") or "api"
            if kind == "api":
                want_id = ((snap.get("api_providers") or {}).get(owner) or {}).get("id")
                want_type = "api"
            else:
                # R-50：每个 plugin 声明用它自己的面（不再共用单一 snap["plugin"]）
                face = plugins.get(owner)
                if not face:
                    r["problems"].append("App %s 声明 provider %s（kind=plugin）在快照里没有对应的插件面 ⇒ 无法判定"
                                         % (app_name, owner))
                face = face or {}
                want_id = face.get("provider_id")
                want_type = face.get("provider_type") or "builtin"
            if want_id and pid != want_id:
                r["problems"].append("App %s 的工具 %s provider_id=%s ≠ 声明 provider %s 的真机 id %s"
                                     % (app_name, tname, pid, owner, want_id))
            if t.get("provider_type") and t.get("provider_type") != want_type:
                r["problems"].append("App %s 的工具 %s provider_type=%s ≠ 期望 %s"
                                     % (app_name, tname, t.get("provider_type"), want_type))
        app_providers[app_name] = set(x for x in ids if x)
    for pair in compare_pairs(binding):
        if len(pair) != 2:
            continue
        a, b = pair
        sa, sb = app_providers.get(a, set()), app_providers.get(b, set())
        inter = sa & sb
        r["evidence"].append({"pair": [a, b], "provider_ids_a": sorted(sa),
                              "provider_ids_b": sorted(sb), "intersection": sorted(inter)})
        if inter:
            r["problems"].append("主对比组 %s 与 %s 的 provider_id 交集非空：%s" % (a, b, sorted(inter)))
    return finish(r)


def check_c9_silent_unbind(binding, snap):
    r = rec("M4-C9", "agent_mode.enabled=false 且 0 工具＝静默脱绑；插件凭据须可用",
            "docs/05 §6.4 :401（enabled=false 且 0 工具＝静默脱绑）"
            " + binding.providers[plugin].credential（插件凭据可用性）")
    if not snap["console"]["ok"]:
        return blocked(r, "Console 不可用：%s" % snap["console"]["error"],
                       "Console 凭据/网络可用后重跑（M4-C7–C9 不得跳过）")
    for app_name, a in sorted((snap.get("apps") or {}).items()):
        if not a.get("exists"):
            r["problems"].append("App %s 不可读，无法判定是否静默脱绑（%s）"
                                 % (app_name, a.get("error") or ("HTTP %s" % a.get("status"))))
            continue
        n, want = a.get("tool_count") or 0, a.get("expected_tool_count")
        r["evidence"].append({"app": app_name, "mode": a.get("mode"), "enabled": a.get("enabled"),
                              "tool_count": n, "expected_tool_count": want})
        if a.get("enabled") is False and n == 0:
            r["problems"].append("App %s agent_mode.enabled=false 且 0 工具 ⇒ 静默脱绑" % app_name)
        if a.get("enabled") is not True:
            r["problems"].append("App %s agent_mode.enabled=%r ≠ true" % (app_name, a.get("enabled")))
        if n == 0:
            r["problems"].append("App %s 工具数为 0" % app_name)
        if want is not None and n != want:
            r["problems"].append("App %s 工具数 %d ≠ 声明 %s" % (app_name, n, want))
        off = [t.get("tool_name") for t in (a.get("tools") or []) if t.get("enabled") is not True]
        if off:
            r["problems"].append("App %s 有未启用工具：%s" % (app_name, off))
    for plug in plugin_faces(snap):
        dname = plug.get("declaration_name")
        cred = dict(plug.get("declared_credential") or {})
        want_keys = sorted(cred.get("keys") or [])
        creds = plug.get("credentials")
        if not creds:
            r["problems"].append("插件 provider %s（声明名 %s）无任何 credential 记录（凭据不可用）"
                                 % (plug.get("plugin_id"), dname))
            continue
        c0 = creds[0]
        r["evidence"].append({"plugin": dname, "plugin_id": plug.get("plugin_id"),
                              "plugin_credential": {"records": len(creds), "name": c0.get("name"),
                                                    "credential_type": c0.get("credential_type"),
                                                    "visibility": c0.get("visibility"),
                                                    "keys": c0.get("keys"), "form": c0.get("form")}})
        if not want_keys:
            r["problems"].append("插件声明 %s 未登记 credential.keys ⇒ 凭据面不可判" % dname)
        elif c0.get("keys") != want_keys:
            r["problems"].append("插件 %s 凭据键集合 %s ≠ 声明 %s" % (dname, c0.get("keys"), want_keys))
        pw = (c0.get("form") or {}).get("NEO4J_PASSWORD")
        if not pw or pw.get("stars", 0) <= 0:
            r["problems"].append("插件 %s 凭据 NEO4J_PASSWORD 缺失或未被掩码（形态 %r）" % (dname, pw))
        else:
            vals = c0.get("_values") or {}
            url_ok = vals.get("NEO4J_URL") == cred.get("neo4j_url")
            user_ok = vals.get("NEO4J_USERNAME") == cred.get("neo4j_username")
            r["evidence"].append({"plugin": dname,
                                  "plugin_credential_value_match": {"NEO4J_URL": bool(url_ok),
                                                                    "NEO4J_USERNAME": bool(user_ok)}})
            if not url_ok:
                r["problems"].append("插件 %s 凭据 NEO4J_URL 与 binding 声明不一致（值不外泄）" % dname)
            if not user_ok:
                r["problems"].append("插件 %s 凭据 NEO4J_USERNAME 与 binding 声明不一致（值不外泄）" % dname)
    return finish(r)


CHECKS = (check_c1_melt_leak, check_c2_payload_budget, check_c3_silent_fallback,
          check_c4_empty_result_hint, check_c5_input_contract, check_c6_truncation_accounting,
          check_c7_provider_schema, check_c8_tool_binding, check_c9_silent_unbind)


def run_all(binding, snap):
    return [fn(binding, snap) for fn in CHECKS]


def overall(records):
    if any(r["verdict"] == BLOCKED for r in records):
        return BLOCKED
    if any(r["verdict"] == FAIL for r in records):
        return FAIL
    return PASS


def exit_code(records):
    return {PASS: 0, FAIL: 1, BLOCKED: 2}[overall(records)]


def print_report(records, snap, binding=None):
    binding = binding or {}
    hosts = ",".join(allowed_hosts(binding)) or "?"
    ports = ",".join(str(p) for p in allowed_ports(binding)) or "?"
    print("[contract_audit] utc=%s 生效主机=%s 白名单端口=%s" % (snap.get("utc"), hosts, ports))
    if snap["console"]["ok"]:
        print("[contract_audit] Console=可用 provider 面 %d 项" % len(snap.get("provider_ids") or []))
    else:
        print("[contract_audit] Console=不可用（%s）⇒ M4-C7/C8/C9 判 BLOCKED" % snap["console"]["error"])
    for r in records:
        print("[%s] %s %s" % (r["id"], r["verdict"], r["title"]))
        print("     依据: %s" % r["basis"])
        for p in r["problems"]:
            print("     ✗ %s" % p)
        if r["blocked_reason"]:
            print("     ⧗ BLOCKED: %s（补验责任：%s）" % (r["blocked_reason"], r["recheck_by"] or "未指定"))
        for e in r["evidence"]:
            print("     · %s" % json.dumps(e, ensure_ascii=False))
    counts = {PASS: 0, FAIL: 0, BLOCKED: 0}
    for r in records:
        counts[r["verdict"]] += 1
    print("逐判据结论: %s" % json.dumps({r["id"]: r["verdict"] for r in records}, ensure_ascii=False))
    print("计数: PASS=%d FAIL=%d BLOCKED=%d" % (counts[PASS], counts[FAIL], counts[BLOCKED]))
    print("★ 整体: %s（退出码 %d）" % (overall(records), exit_code(records)))


# ---------------------------------------------------------------- selftest
def _envelope(hint="", truncated=False, dr=0, db=0, **extra):
    d = {"hint": hint, "truncated": truncated, "droppedRows": dr, "droppedBytes": db}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


class FakeProbe(object):
    """离线假探针：按 (port, path) 与 /facts 的 payload 分流返回预置响应。"""

    def __init__(self, light=None, facts_empty=None, facts_missing=None, facts_unknown=None,
                 health=None, metrics=None, default=None, read_facts=None):
        self.light = light if light is not None else ProbeResp(200, _envelope(items=50))
        self.facts_empty = facts_empty if facts_empty is not None else ProbeResp(200, _envelope(items=0))
        self.facts_missing = facts_missing if facts_missing is not None else ProbeResp(
            200 if False else 400, json.dumps({"error": 3}, ensure_ascii=False))
        self.facts_unknown = facts_unknown if facts_unknown is not None else self.facts_missing
        self.health = health if health is not None else ProbeResp(
            200, json.dumps({"status": "ok", "rowCap": ROW_CAP, "byteCap": BYTE_CAP}, ensure_ascii=False))
        self.metrics = metrics if metrics is not None else ProbeResp(200, _envelope(items=89))
        # ★ S2c：live-facts 载体的轻探针（POST /tools/read_facts，max_records=0 ⇒ 0 行 + hint）
        self.read_facts = read_facts if read_facts is not None else ProbeResp(
            200, _envelope(hint="max_records=0：本次调用未返回任何行，这不等于对象不存在。", items=0))
        self.default = default if default is not None else self.light
        self.calls = []

    @staticmethod
    def key_for(url):
        m = re.match(r"^https?://[^/:]+(?::(?P<port>\d+))?(?P<path>/.*)$", url)
        port = (m.group("port") or "80") if m else "0"
        path = m.group("path") if m else url
        return port, path

    def request(self, method, url, payload=None, timeout=20):
        port, path = self.key_for(url)
        self.calls.append((method, port, path))
        if path == "/health":
            return self.health
        if path == "/facts":
            if payload is not None:
                if not payload:
                    return self.facts_missing
                if "m4_unknown_param_probe" in payload:
                    return self.facts_unknown
                return self.facts_empty
        if path == "/tools/read_facts":
            return self.read_facts
        if path == "/metrics":
            return self.metrics
        if path == "/guide":
            return self.light
        return self.default


class FakeResp(object):
    def __init__(self, obj, status=200):
        self.status_code = status
        self.text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)


class FakeConsole(object):
    def __init__(self, apps=None, api_details=None, providers=None, plugin=None, plugins=None,
                 fail=False):
        self.apps = apps or {}
        self.api_details = api_details or {}
        self.providers = providers if providers is not None else []
        self.plugin = plugin or {}
        # R-50：真机可并存多个插件面；给出 plugins 时按 URL 里的 provider_id 逐一分流
        self.plugins = plugins
        self.fail = fail

    def _face(self, path):
        """按 `/tool-provider/builtin/<provider_id>/…` 里的 provider_id 选要服务的插件面。"""
        if self.plugins is None:
            return self.plugin or {}
        head = path.split("/tool-provider/builtin/", 1)[-1]
        face_id = urllib.parse.unquote(head.split("/", 1)[0])
        return self.plugins.get(face_id) or {}

    def request(self, method, path, json_body=None, headers=None, timeout=30, expect=(200,)):
        if self.fail:
            raise RuntimeError("selftest: fake console transport down")
        if path.startswith("/console/api"):
            path = path[len("/console/api"):]
        if path.startswith("/workspaces/current/tool-providers"):
            return FakeResp(self.providers)
        if path.startswith("/workspaces/current/tool-provider/api/get"):
            name = path.split("provider=")[-1]
            return FakeResp(self.api_details.get(name, {}))
        if "/tool-provider/builtin/" in path and path.endswith("/tools"):
            return FakeResp(self._face(path).get("tools", []))
        if "/tool-provider/builtin/" in path and path.endswith("/credentials"):
            return FakeResp(self._face(path).get("credentials", []))
        m = re.match(r"^/apps/([0-9a-fA-F-]+)$", path)
        if m:
            uuid = m.group(1)
            if uuid in self.apps:
                return FakeResp(self.apps[uuid])
            return FakeResp({"message": "not found"}, status=404)
        return FakeResp({}, status=404)


def _fake_binding(free_provider="neo4j_query_fork", free_providers=None):
    return {
        "whitelist": {"provider_names": ["cc-kg-tools", "cc-onto-tools", "cc-kgt-tools",
                                         "cc-live-facts-tools"],
                      "allowed_ports": [8301, 8302, 8303, 8304], "allowed_hosts": ["192.0.2.84"]},
        "providers": [
            {"provider": "cc-kg-tools", "kind": "api", "carrier": "kg",
             "base": "http://192.0.2.84:8301", "port": 8301,
             "tools": ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace", "cc_cause",
                       "cc_propose", "read_facts"],
             "expected_tool_count": 8},
            {"provider": "cc-onto-tools", "kind": "api", "carrier": "onto",
             "base": "http://192.0.2.84:8302", "port": 8302,
             "tools": ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace", "cc_cause",
                       "cc_propose", "read_facts"],
             "expected_tool_count": 8},
            {"provider": "cc-kgt-tools", "kind": "api", "carrier": "kgt",
             "base": "http://192.0.2.84:8303", "port": 8303,
             "tools": ["get_object_topology", "get_fault_topology", "get_metrics", "get_facts",
                       "read_facts"],
             "expected_tool_count": 5},
            {"provider": "cc-live-facts-tools", "kind": "api", "carrier": "live-facts",
             "base": "http://192.0.2.84:8304", "port": 8304,
             "tools": ["read_live_facts"], "expected_tool_count": 1},
            {"provider": "neo4j_query", "kind": "plugin", "carrier": "kgt-free",
             "plugin_id": "nikola_milosevic/neo4j_query", "bind_tools": ["neo4j_query"],
             "provider_id": "nikola_milosevic/neo4j_query/neo4j_query",
             "param_form": {"database": "form", "query": "form", "parameters": "form",
                            "max_records": "form", "allow_write_queries": "form"},
             "credential": {"name": "cc-kgt-neo4j",
                            "keys": ["NEO4J_URL", "NEO4J_USERNAME", "NEO4J_PASSWORD"],
                            "neo4j_url": "bolt://192.0.2.84:7688", "neo4j_username": "neo4j"}},
            {"provider": "neo4j_query_fork", "kind": "plugin", "carrier": "kgt-free",
             "plugin_id": "cc-ops/neo4j_cypher_query", "bind_tools": ["neo4j_query"],
             "provider_id": "cc-ops/neo4j_cypher_query/neo4j_query",
             "plugin_unique_identifier": "cc-ops/neo4j_cypher_query:0.1.0@"
                                         "1b04ef281638ec88b1002c4997c0e8b7d8b4efe8dbb06fe4536c79378372d9f1",
             "installation_id": "01a0eb3f-744a-7559-843b-09243b199714",
             "param_form": {"database": "form", "query": "llm", "parameters": "llm",
                            "max_records": "form", "allow_write_queries": "form"},
             "credential": {"name": "cc-kgt-neo4j-fork",
                            "keys": ["NEO4J_URL", "NEO4J_USERNAME", "NEO4J_PASSWORD"],
                            "neo4j_url": "bolt://192.0.2.84:7688", "neo4j_username": "neo4j"}},
        ],
        "apps": {
            "cc-kg-agent": {"uuid": "aaaa1111-1111-1111-1111-111111111111", "carrier": "kg",
                            "provider": "cc-kg-tools",
                            "tools": ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace",
                                      "cc_cause", "cc_propose", "read_facts"]},
            "cc-onto-agent": {"uuid": "bbbb2222-2222-2222-2222-222222222222", "carrier": "onto",
                              "provider": "cc-onto-tools",
                              "tools": ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace",
                                        "cc_cause", "cc_propose", "read_facts"]},
            "cc-kgt-agent": {"uuid": "cccc3333-3333-3333-3333-333333333333", "carrier": "kgt",
                             "provider": "cc-kgt-tools",
                             "tools": ["get_object_topology", "get_fault_topology", "get_metrics",
                                       "get_facts", "read_facts"]},
            "cc-kgt-free-agent": {"uuid": "dddd4444-4444-4444-4444-444444444444", "carrier": "kgt-free",
                                  "provider": free_provider,
                                  "providers": (free_providers if free_providers is not None
                                                else [free_provider, "cc-live-facts-tools"]),
                                  "tools": ["neo4j_query", "read_live_facts"]},
        },
        "fairness": {"compare_pairs": [["cc-kg-agent", "cc-onto-agent"]]},
        "counts": {"setb_tools": 8, "kgt_main_tools": 5, "kgt_free_tools": 2,
                   "livefacts_tools": 1},
    }


def _mk_app(uuid, mode, enabled, tool_names, provider_id, provider_type, tool_faces=None):
    """tool_faces: {tool_name: (provider_id, provider_type)} —— 多 provider App 的逐工具真机面。"""
    def _ent(t):
        pid, ptype = (tool_faces or {}).get(t) or (provider_id, provider_type)
        return {"provider_id": pid, "provider_name": pid, "provider_type": ptype,
                "type": ptype, "tool_name": t, "enabled": True}
    return {uuid: {"id": uuid, "name": uuid, "mode": mode,
                   "model_config": {"agent_mode": {
                       "enabled": enabled,
                       "tools": [_ent(t) for t in tool_names]}}}}


SETB8 = ["cc_guide", "cc_schema", "cc_query", "cc_facts", "cc_trace", "cc_cause", "cc_propose",
         "read_facts"]
KGT5 = ["get_object_topology", "get_fault_topology", "get_metrics", "get_facts", "read_facts"]
PID_KG = "d9b2b1ff-0000-0000-0000-000000000001"
PID_ONTO = "a8ebb23b-0000-0000-0000-000000000002"
PID_KGT = "e56dfa91-0000-0000-0000-000000000003"
PID_LIVEFACTS = "f7c0a1b2-0000-0000-0000-000000000004"
PID_FREE = "cc-ops/neo4j_cypher_query/neo4j_query"        # R-50 fork：App 实际绑定（真机）
PID_FREE_MARKET = "nikola_milosevic/neo4j_query/neo4j_query"  # 市场 0.2.0：并存、未绑定


def _live_apps(free_enabled=True, free_tools=None, onto_pid=PID_ONTO, kgt_pid=PID_KGT,
               free_pid=PID_FREE, facts_pid=PID_LIVEFACTS):
    apps = {}
    apps.update(_mk_app("aaaa1111-1111-1111-1111-111111111111", "agent-chat", True, SETB8, PID_KG, "api"))
    apps.update(_mk_app("bbbb2222-2222-2222-2222-222222222222", "agent-chat", True, SETB8, onto_pid, "api"))
    apps.update(_mk_app("cccc3333-3333-3333-3333-333333333333", "agent-chat", True, KGT5, kgt_pid, "api"))
    apps.update(_mk_app("dddd4444-4444-4444-4444-444444444444", "agent-chat", free_enabled,
                        (free_tools if free_tools is not None
                         else ["neo4j_query", "read_live_facts"]), free_pid, "builtin",
                        tool_faces={"read_live_facts": (facts_pid, "api")}))
    return apps


def _live_providers():
    return [
        {"id": PID_KG, "name": "cc-kg-tools", "type": "api"},
        {"id": PID_ONTO, "name": "cc-onto-tools", "type": "api"},
        {"id": PID_KGT, "name": "cc-kgt-tools", "type": "api"},
        {"id": PID_LIVEFACTS, "name": "cc-live-facts-tools", "type": "api"},
        {"id": PID_FREE_MARKET, "name": PID_FREE_MARKET, "type": "builtin",
         "plugin_id": "nikola_milosevic/neo4j_query",
         "plugin_unique_identifier": "nikola_milosevic/neo4j_query:0.2.0@deadbeef"},
        {"id": PID_FREE, "name": PID_FREE, "type": "builtin",
         "plugin_id": "cc-ops/neo4j_cypher_query",
         "plugin_unique_identifier": "cc-ops/neo4j_cypher_query:0.1.0@deadbeef"},
    ]


def _live_api_details(kg_port=8301, onto_port=8302, kgt_port=8303, livefacts_port=8304):
    def d(port, paths):
        return {"tools": [{}] * len(paths),
                "schema": json.dumps({"servers": [{"url": "http://192.0.2.84:%d" % port}],
                                      "paths": {p: {} for p in paths}}, ensure_ascii=False)}
    setb = ["/guide", "/schema", "/query", "/facts", "/trace", "/cause", "/propose",
            "/tools/read_facts"]
    return {"cc-kg-tools": d(kg_port, setb), "cc-onto-tools": d(onto_port, setb),
            "cc-kgt-tools": d(kgt_port, ["/topology/object", "/topology/fault", "/metrics", "/facts",
                                         "/tools/read_facts"]),
            "cc-live-facts-tools": d(livefacts_port, ["/tools/read_facts"])}


def _live_plugin(params=None, creds="ok", cred_name="cc-kgt-neo4j",
                 cred_id="3add981d-13e8-4e14-a425-6b0fa3d6b1e8"):
    params = params if params is not None else [
        {"name": "database", "type": "string", "required": False, "form": "form"},
        {"name": "query", "type": "string", "required": True, "form": "form"},
        {"name": "parameters", "type": "string", "required": False, "form": "form"},
        {"name": "max_records", "type": "number", "required": False, "form": "form", "default": 1000},
        {"name": "allow_write_queries", "type": "boolean", "required": False, "form": "form",
         "default": False}]
    out = {"tools": [{"name": "neo4j_query", "parameters": params, "output_schema": {}}]}
    if creds == "ok":
        out["credentials"] = [{"name": cred_name, "id": cred_id,
                               "credential_type": "api-key", "visibility": "all_team_members",
                               "is_default": True,
                               "credentials": {"NEO4J_URL": "bolt://192.0.2.84:7688",
                                               "NEO4J_USERNAME": "neo4j",
                                               "NEO4J_PASSWORD": "ab" + "*" * 13 + "cd"}}]
    elif creds == "missing":
        out["credentials"] = []
    elif creds == "wrongkeys":
        out["credentials"] = [{"name": cred_name,
                               "credentials": {"NEO4J_URL": "bolt://192.0.2.84:7688"}}]
    return out


def _live_plugins(fork_params=None, fork_creds="ok", market_params=None, market_creds="ok"):
    """两个插件面的真机回读夹具：市场 0.2.0（五参全 form）+ R-50 fork（query/parameters=llm）。

    键＝真机 provider_id，FakeConsole 按 URL 里的 provider_id 分流，因此两支可**各自单独触发**。
    """
    market = market_params if market_params is not None else [
        {"name": "database", "type": "string", "required": False, "form": "form"},
        {"name": "query", "type": "string", "required": True, "form": "form"},
        {"name": "parameters", "type": "string", "required": False, "form": "form"},
        {"name": "max_records", "type": "number", "required": False, "form": "form", "default": 1000},
        {"name": "allow_write_queries", "type": "boolean", "required": False, "form": "form",
         "default": False}]
    if fork_params is None:
        fork = [dict(p) for p in market]
        for p in fork:
            if p["name"] in ("query", "parameters"):
                p["form"] = "llm"
    else:
        fork = fork_params
    return {PID_FREE_MARKET: _live_plugin(params=market, creds=market_creds,
                                          cred_name="cc-kgt-neo4j",
                                          cred_id="3add981d-13e8-4e14-a425-6b0fa3d6b1e8"),
            PID_FREE: _live_plugin(params=fork, creds=fork_creds,
                                   cred_name="cc-kgt-neo4j-fork",
                                   cred_id="bd254b70-753b-4e0c-b5e2-8870dfd8eb63")}


def _snap(probe=None, apps=None, api_details=None, providers=None, plugins=None,
          console_ok=True, binding=None, free_provider=None, free_pid=PID_FREE,
          fork_params=None, fork_creds="ok", market_params=None, market_creds="ok",
          free_providers=None, facts_pid=PID_LIVEFACTS):
    b = binding or _fake_binding(free_provider=free_provider or "neo4j_query_fork",
                                 free_providers=free_providers)
    console = None
    if console_ok:
        console = FakeConsole(apps=apps if apps is not None
                              else _live_apps(free_pid=free_pid, facts_pid=facts_pid),
                              api_details=api_details if api_details is not None else _live_api_details(),
                              providers=providers if providers is not None else _live_providers(),
                              plugins=plugins if plugins is not None else _live_plugins(
                                  fork_params=fork_params, fork_creds=fork_creds,
                                  market_params=market_params, market_creds=market_creds))
    snap = build_snapshot(b, console=console, probe=probe or FakeProbe(), with_console=console_ok)
    if not console_ok:
        snap["console"] = {"ok": False, "error": "selftest 注入：Console 凭据不可用"}
    return b, snap


SETB_REJECT = json.dumps({"error": 3, "hint": "请求体不满足工具参数契约（未识别字段或类型不符），不得静默回落默认值。"},
                         ensure_ascii=False)


def _probe_ok(leak=False, giant=False, empty_hint=True, trunc_ok=True, reject=SETB_REJECT):
    pad = ('x' * 30000) if giant else ''
    note = 'STOP、禁止再调用任何工具' if leak else 'ok'
    light = ProbeResp(200, _envelope(items=50, pad=pad, note=note))
    empty = ProbeResp(200, _envelope(hint=("本次调用没有返回任何行：这不等于该对象不存在。" if empty_hint else ""),
                                     items=0))
    metrics = ProbeResp(200, _envelope(hint=("出口字节复核：已截断并记账。" if trunc_ok else ""),
                                       truncated=True, dr=0, db=(3594 if trunc_ok else 0), items=89))
    bad = ProbeResp(400, reject)
    return FakeProbe(light=light, facts_empty=empty, facts_missing=bad, facts_unknown=bad, metrics=metrics)


def cmd_selftest(args):
    groups = []
    failures = []

    def check(label, cond, detail=""):
        groups.append((label, bool(cond)))
        if not cond:
            failures.append("%s  ← %s" % (label, detail))

    def run(binding, snap):
        records = run_all(binding, snap)
        return {r["id"]: r for r in records}, exit_code(records)

    def v(got, cid):
        return "%s=%s" % (cid, got[cid]["verdict"])

    def probs(got, cid):
        return str(got[cid]["problems"][:1])

    b, s = _snap(probe=_probe_ok())
    got, code = run(b, s)
    check("正控 九条全 PASS", all(r["verdict"] == PASS for r in got.values()),
          json.dumps({k: v["verdict"] for k, v in got.items()}, ensure_ascii=False))
    check("正控 退出码 0", code == 0, "rc=%s" % code)

    # ★ S2c：多 provider 免费臂 + live-facts 载体探针族
    _ev_free = [e for e in got["M4-C8"]["evidence"] if e.get("app") == "cc-kgt-free-agent"]
    check("正控 免费臂按声明集合逐工具解析（neo4j_query→插件面 / read_live_facts→cc-live-facts-tools）",
          bool(_ev_free)
          and _ev_free[0].get("declared_providers") == ["neo4j_query_fork", "cc-live-facts-tools"]
          and got["M4-C8"]["verdict"] == PASS,
          json.dumps(_ev_free[:1], ensure_ascii=False))
    _lf = (s.get("tool_probes") or {}).get("live-facts") or {}
    check("正控 live-facts 载体按自身端点探针（/tools/read_facts）且不误发 /facts 专用探针",
          _lf.get("light_path") == "/tools/read_facts" and not (_lf.get("other") or {})
          and [_x.get("status") for _x in (_lf.get("light") or [])] == [200, 200, 200],
          json.dumps({k: _lf.get(k) for k in ("light_path", "probe_family")}, ensure_ascii=False))
    _na = [e for _cid in ("M4-C3", "M4-C4", "M4-C5")
           for e in got[_cid]["evidence"]
           if e.get("carrier") == "live-facts" and e.get("not_applicable")]
    check("正控 非 KgFacts 载体在 M4-C3/C4/C5 记 not_applicable（不静默 PASS、不放宽 KgFacts 断言）",
          len(_na) == 3, json.dumps(_na[:1], ensure_ascii=False))
    _lf2 = ProbeResp(404, json.dumps({"message": "no such path"}, ensure_ascii=False))
    _b, _s = _snap(probe=FakeProbe(read_facts=_lf2))
    _got, _c = run(_b, _s)
    check("负控 live-facts 端点 404（该载体探针无 2xx）⇒ M4-C6 BLOCKED 且 rc=2"
          "（证明该载体确实被探到、不被静默跳过）",
          _got["M4-C6"]["verdict"] == BLOCKED and _c == 2,
          "%s rc=%s %s" % (v(_got, "M4-C6"), _c, probs(_got, "M4-C6")))

    _b4 = _fake_binding()
    _b4["apps"]["cc-kgt-free-agent"]["providers"] = ["cc-live-facts-tools", "neo4j_query_fork"]
    _b4["apps"]["cc-kgt-free-agent"]["provider"] = "neo4j_query_fork"
    _b, _s = _snap(probe=_probe_ok(), binding=_b4)
    _got, _c = run(_b, _s)
    check("负控 provider 与 providers[0] 不一致（两个键各说一套）⇒ M4-C8 FAIL",
          _got["M4-C8"]["verdict"] == FAIL, probs(_got, "M4-C8"))

    _b5 = _fake_binding()
    _b5["apps"]["cc-kg-agent"]["providers"] = ["cc-kg-tools", "cc-onto-tools"]
    _b, _s = _snap(probe=_probe_ok(), binding=_b5)
    _got, _c = run(_b, _s)
    check("负控 主对比对 App 声明 2 个 provider ⇒ M4-C8 FAIL（恰好 1 不得放宽）",
          _got["M4-C8"]["verdict"] == FAIL, probs(_got, "M4-C8"))

    _b, _s = _snap(probe=_probe_ok(), free_providers=["neo4j_query_fork"])
    _got, _c = run(_b, _s)
    check("负控 免费臂工具不属于声明集合里任何 provider ⇒ M4-C8 FAIL",
          _got["M4-C8"]["verdict"] == FAIL, probs(_got, "M4-C8"))

    _b, _s = _snap(probe=_probe_ok(), facts_pid=PID_KG)
    _got, _c = run(_b, _s)
    check("负控 免费臂 read_live_facts 指向另一 provider 的真机 id ⇒ M4-C8 FAIL",
          _got["M4-C8"]["verdict"] == FAIL, probs(_got, "M4-C8"))

    b, s = _snap(probe=_probe_ok(), api_details=_live_api_details(kg_port=9999))
    got, code = run(b, s)
    check("负控 端口错配(9999) ⇒ M4-C7 FAIL + rc=1",
          got["M4-C7"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C7"), code, probs(got, "M4-C7")))

    b, s = _snap(probe=_probe_ok(), apps=_live_apps(onto_pid=PID_KG))
    got, code = run(b, s)
    check("负控 两主对比组 provider_id 交集非空 ⇒ M4-C8 FAIL + rc=1",
          got["M4-C8"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C8"), code, probs(got, "M4-C8")))

    b, s = _snap(probe=_probe_ok(), apps=_live_apps(kgt_pid="deadbeef-0000-0000-0000-000000000000"))
    got, _c = run(b, s)
    check("负控 悬空 provider_id ⇒ M4-C8 FAIL", got["M4-C8"]["verdict"] == FAIL, probs(got, "M4-C8"))

    # R-50：App 声明指向市场插件、真机工具却在 fork 上 ⇒ C8 必须红（这正是 W12 前的真实 FAIL 形态）
    b, s = _snap(probe=_probe_ok(), binding=_fake_binding(free_provider="neo4j_query"))
    got, _c = run(b, s)
    check("负控 App 声明 provider 与真机插件 provider_id 不符（声明落后于重绑）⇒ M4-C8 FAIL",
          got["M4-C8"]["verdict"] == FAIL, probs(got, "M4-C8"))

    # 反向：App 声明 fork、真机工具还在市场插件上 ⇒ 必须红
    b, s = _snap(probe=_probe_ok(), free_pid=PID_FREE_MARKET)
    got, _c = run(b, s)
    check("负控 真机工具仍在市场插件而声明是 fork ⇒ M4-C8 FAIL",
          got["M4-C8"]["verdict"] == FAIL, probs(got, "M4-C8"))

    # 声明的 provider 名字根本没登记 ⇒ 不许静默放过
    b, s = _snap(probe=_probe_ok(), binding=_fake_binding(free_provider="neo4j_query_ghost"))
    got, _c = run(b, s)
    check("负控 App 声明的 provider 未登记 ⇒ M4-C8 FAIL", got["M4-C8"]["verdict"] == FAIL, probs(got, "M4-C8"))

    b, s = _snap(probe=_probe_ok(), api_details={})
    got, code = run(b, s)
    check("负控 provider 缺失 ⇒ M4-C7 FAIL + rc=1",
          got["M4-C7"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C7"), code, probs(got, "M4-C7")))

    b, s = _snap(probe=_probe_ok(),
                 plugins={PID_FREE: {"tools": [], "credentials": []},
                          PID_FREE_MARKET: _live_plugin()})
    got, _c = run(b, s)
    check("负控 fork 插件工具面缺失 ⇒ M4-C7 FAIL", got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    drift = [dict(p) for p in _live_plugin()["tools"][0]["parameters"]]
    for p in drift:
        if p["name"] == "allow_write_queries":
            p["default"] = True
    b, s = _snap(probe=_probe_ok(), fork_params=drift)
    got, _c = run(b, s)
    check("负控 fork 插件入参 default 漂移 ⇒ M4-C7 FAIL", got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    # R-50：C7 必须逐插件覆盖——市场那一支也要能**单独**触发
    mdrift = [dict(p) for p in _live_plugin()["tools"][0]["parameters"]]
    for p in mdrift:
        if p["name"] == "max_records":
            p["default"] = 7
    b, s = _snap(probe=_probe_ok(), market_params=mdrift)
    got, _c = run(b, s)
    check("负控 市场插件入参 default 漂移（fork 正常）⇒ M4-C7 FAIL",
          got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    # fork 的 query 退回 form ⇒ 正是 R-50 要防的回归（写面/门面回退）
    fform = [dict(p) for p in _live_plugin()["tools"][0]["parameters"]]
    for p in fform:
        if p["name"] == "query":
            p["form"] = "form"
    b, s = _snap(probe=_probe_ok(), fork_params=fform)
    got, _c = run(b, s)
    check("负控 fork 入参 query 退回 form ⇒ M4-C7 FAIL", got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    # 声明之间的无声漂移：binding 里 fork 的 param_form 与 kgt-tools.yaml 契约不一致
    b2 = _fake_binding()
    for pv in b2["providers"]:
        if pv.get("provider") == "neo4j_query_fork":
            pv["param_form"] = dict(pv["param_form"], parameters="form")
    b, s = _snap(probe=_probe_ok(), binding=b2)
    got, _c = run(b, s)
    check("负控 binding 的 fork param_form 与 kgt-tools.yaml 契约不一致 ⇒ M4-C7 FAIL",
          got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    # 声明缺 param_form ⇒ form 面不可判，必须红
    b3 = _fake_binding()
    for pv in b3["providers"]:
        if pv.get("provider") == "neo4j_query_fork":
            pv.pop("param_form", None)
    b, s = _snap(probe=_probe_ok(), binding=b3)
    got, _c = run(b, s)
    check("负控 插件声明缺 param_form ⇒ M4-C7 FAIL（不许放过不可判的声明）",
          got["M4-C7"]["verdict"] == FAIL, probs(got, "M4-C7"))

    b, s = _snap(probe=_probe_ok(leak=True))
    got, code = run(b, s)
    check("负控 返回体含 STOP 指令 ⇒ M4-C1 FAIL + rc=1",
          got["M4-C1"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C1"), code, probs(got, "M4-C1")))

    b, s = _snap(probe=_probe_ok(), fork_creds="missing")
    got, code = run(b, s)
    check("负控 fork 插件凭据记录缺失 ⇒ M4-C9 FAIL + rc=1",
          got["M4-C9"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C9"), code, probs(got, "M4-C9")))

    b, s = _snap(probe=_probe_ok(), fork_creds="wrongkeys")
    got, _c = run(b, s)
    check("负控 fork 插件凭据键集合漂移 ⇒ M4-C9 FAIL", got["M4-C9"]["verdict"] == FAIL, probs(got, "M4-C9"))

    # R-50：C9 必须逐插件覆盖——市场那一支也要能**单独**触发（fork 正常）
    b, s = _snap(probe=_probe_ok(), market_creds="missing")
    got, _c = run(b, s)
    check("负控 市场插件凭据记录缺失（fork 正常）⇒ M4-C9 FAIL",
          got["M4-C9"]["verdict"] == FAIL, probs(got, "M4-C9"))

    b, s = _snap(probe=_probe_ok(), market_creds="wrongkeys")
    got, _c = run(b, s)
    check("负控 市场插件凭据键集合漂移 ⇒ M4-C9 FAIL", got["M4-C9"]["verdict"] == FAIL, probs(got, "M4-C9"))

    b, s = _snap(probe=_probe_ok(), apps=_live_apps(free_enabled=False, free_tools=[]))
    got, code = run(b, s)
    check("负控 enabled=false 且 0 工具(静默脱绑) ⇒ M4-C9 FAIL + rc=1",
          got["M4-C9"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C9"), code, probs(got, "M4-C9")))

    b, s = _snap(probe=_probe_ok(giant=True))
    got, code = run(b, s)
    check("负控 载荷超 byte_cap ⇒ M4-C2 FAIL + rc=1",
          got["M4-C2"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C2"), code, probs(got, "M4-C2")))

    b, s = _snap(probe=_probe_ok(reject=json.dumps({"error": 3}, ensure_ascii=False)))
    got, code = run(b, s)
    check("负控 未识别参数/缺必填无说明(静默) ⇒ M4-C3 FAIL + rc=1",
          got["M4-C3"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C3"), code, probs(got, "M4-C3")))
    check("负控 缺必填参数无说明 ⇒ M4-C5 FAIL",
          got["M4-C5"]["verdict"] == FAIL, probs(got, "M4-C5"))

    b, s = _snap(probe=_probe_ok(empty_hint=False))
    got, code = run(b, s)
    check("负控 空结果无 hint ⇒ M4-C4 FAIL + rc=1",
          got["M4-C4"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C4"), code, probs(got, "M4-C4")))

    b, s = _snap(probe=_probe_ok(trunc_ok=False))
    got, code = run(b, s)
    check("负控 truncated=true 却不记账(静默截断) ⇒ M4-C6 FAIL + rc=1",
          got["M4-C6"]["verdict"] == FAIL and code == 1, "%s rc=%s %s" % (v(got, "M4-C6"), code, probs(got, "M4-C6")))

    sw = ProbeResp(200, json.dumps({"hint": "no stopwatch here", "truncated": False,
                                    "droppedRows": 0, "droppedBytes": 0,
                                    "note": "stopwatch / unstoppable / stop-point / nonstop / STOPWATCH"},
                                   ensure_ascii=False))
    b, s = _snap(probe=FakeProbe(light=sw))
    got, _c = run(b, s)
    check("误报负控 stopwatch/unstoppable 不得判 FAIL（非整串假绿）",
          got["M4-C1"]["verdict"] == PASS, "%s %s" % (v(got, "M4-C1"), probs(got, "M4-C1")))

    b, s = _snap(probe=_probe_ok(), console_ok=False)
    got, code = run(b, s)
    check("负控 Console 凭据不可用 ⇒ M4-C7/C8/C9 全 BLOCKED（绝不静默 PASS）",
          all(got[k]["verdict"] == BLOCKED for k in ("M4-C7", "M4-C8", "M4-C9")),
          json.dumps({k: got[k]["verdict"] for k in ("M4-C7", "M4-C8", "M4-C9")}, ensure_ascii=False))
    check("负控 Console 凭据不可用 ⇒ 退出码 2（BLOCKED ≠ FAIL ≠ PASS）", code == 2, "rc=%s" % code)
    check("负控 Console 不可用 ⇒ M4-C1–C6 仍逐条判定（不被连带跳过）",
          all(got[k]["verdict"] in (PASS, FAIL) for k in
              ("M4-C1", "M4-C2", "M4-C3", "M4-C4", "M4-C5", "M4-C6")),
          json.dumps({k: got[k]["verdict"] for k in ("M4-C1", "M4-C2")}, ensure_ascii=False))

    class DeadProbe(object):
        def request(self, method, url, payload=None, timeout=20):
            return ProbeResp(0, "", error="ConnectionRefusedError: selftest")

    b = _fake_binding()
    s = build_snapshot(b, console=None, probe=DeadProbe(), with_console=False)
    got, code = run(b, s)
    check("负控 工具服务不可达 ⇒ 相关判据 BLOCKED 且 rc=2",
          got["M4-C1"]["verdict"] == BLOCKED and code == 2, "%s rc=%s" % (v(got, "M4-C1"), code))

    b, s = _snap(probe=_probe_ok())
    red = json.dumps(redact_snapshot(s), ensure_ascii=False)
    check("负控 证据不含任何掩码口令片段或明文值",
          ("*" * 3) not in red and ("ab" + "*" * 13 + "cd") not in red and '"_values"' not in red,
          "star-run=%s" % (("*" * 3) in red))
    check("负控 证据保留凭据键名（M4-C9 键集合判据需要，键名不是口令）",
          "NEO4J_PASSWORD" in red, "keys 列表缺失")
    check("负控 证据不含原始返回正文", "droppedEntries" not in red and ("x" * 200) not in red)
    check("负控 证据保留形态标记 len/stars/reveals",
          '"stars": 13' in red and '"reveals_first": 2' in red and '"reveals_last": 2' in red,
          red[red.find("stars") - 60: red.find("stars") + 60] if "stars" in red else "no stars key")

    print("SELFTEST 明细（共 %d 项）" % len(groups))
    for label, okv in groups:
        print("  [%s] %s" % ("OK" if okv else "NG", label))
    if failures:
        print("SELFTEST FAIL（%d/%d 项未通过）" % (len(failures), len(groups)))
        for f in failures:
            print("  ✗ %s" % f)
        return 1
    print("SELFTEST PASS（%d 项：正控 2 + 负控 %d）" % (len(groups), len(groups) - 2))
    return 0


# ---------------------------------------------------------------- CLI
def _harden_stream(stream):
    """把文本流切到 UTF-8 + errors=replace，消除「宿主控制台代码页决定门禁成败」。

    取证纪律要求报告可复现：stdout 被重定向（如 Start-Process -RedirectStandardOutput）时
    TextIOWrapper 会按宿主 ANSI 代码页（本机 GBK/CP936）编码，`⇒`/`✗` 等字符直接抛
    UnicodeEncodeError，门禁在打印第 3 项时崩掉、以 rc=1 冒充 FAIL。判定语义不该依赖
    调用方的控制台。

    防御：reconfigure 不存在（Py<3.7 / 非 TextIOWrapper / sys.stdout is None）、流已关闭、
    被重定向到非文本对象——一律静默降级，加固本身绝不新增失败面。返回值仅作诊断，
    不参与任何判据。
    """
    try:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            return False
        reconfigure(encoding="utf-8", errors="replace")
        return True
    except Exception:  # noqa: BLE001 — 编码加固失败不得改变判据结论
        return False


def harden_console_encoding():
    """stdout/stderr 双流加固；返回 (stdout_ok, stderr_ok)，纯诊断值。"""
    return (_harden_stream(sys.stdout), _harden_stream(sys.stderr))


def cmd_check(args):
    binding = load_binding(args.binding)
    console, console_error = None, None
    if not args.no_console:
        try:
            import common
            console = common.console_client(repo_root=REPO_ROOT)
        except Exception as exc:  # noqa: BLE001
            console_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    snap = build_snapshot(binding, console=console, probe=UrlProbe(), timeout=args.timeout,
                          with_console=(console is not None))
    if console is None:
        snap["console"] = {"ok": False, "error": console_error or "未配置 Console 客户端"}
    records = run_all(binding, snap)
    print_report(records, snap, binding)
    out = {"utc": snap["utc"], "records": records,
           "verdicts": {r["id"]: r["verdict"] for r in records},
           "overall": overall(records), "exit_code": exit_code(records),
           "snapshot": redact_snapshot(snap)}
    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print("[contract_audit] 报告写入 %s" % p)
    if args.evidence:
        d = Path(args.evidence)
        d.mkdir(parents=True, exist_ok=True)
        stamp = snap["utc"].replace(":", "").replace("-", "")
        (d / ("contract_audit.%s.json" % stamp)).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        (d / ("contract_audit.%s.txt" % stamp)).write_text(
            "".join("%s %s %s\n" % (r["id"], r["verdict"], json.dumps(r["problems"], ensure_ascii=False))
                    for r in records), encoding="utf-8", newline="\n")
        print("[contract_audit] 证据写入 %s" % d)
    return exit_code(records)


def build_parser():
    ap = argparse.ArgumentParser(
        prog="contract_audit.py",
        description="M4.6 契约门禁 M4-C1…M4-C9（docs/05 §6.4；三态退出码 0=全过/1=FAIL/2=BLOCKED）")
    ap.add_argument("--binding", default=str(BINDING_PATH))
    ap.add_argument("--check", action="store_true", help="只读审计（默认动作）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（含负控）")
    ap.add_argument("--no-console", action="store_true", help="跳过 Console ⇒ M4-C7/C8/C9 BLOCKED")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", default=None)
    ap.add_argument("--evidence", default=None)
    return ap


def main(argv=None):
    harden_console_encoding()   # 必须在 argparse/任何 print 之前，判定语义与代码页解耦
    args = build_parser().parse_args(argv)
    try:
        if args.selftest:
            return cmd_selftest(args)
        return cmd_check(args)
    except Fail as exc:
        sys.stderr.write("[contract_audit] FATAL(前置): %s\n" % exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[contract_audit] FATAL: %s: %s\n" % (type(exc).__name__, exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
