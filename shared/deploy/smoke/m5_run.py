#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5 / W19 · 运行台：把「用例集 × 被测实验组」在**真实 Dify** 上跑一遍，产出标准答案文件。

本文件只做**采集**，不做**判分**。判据器（W18 `shared/verify/m5_judge.py`）消费本文件的
`answers.jsonl`；两侧共用队长定义的同一份行契约。

行契约（JSONL，一行一条 ``(case_id, group, repeat)``）
----------------------------------------------------
必需键：``case_id / group / repeat / question / answer_text``
可选键：``abstain / elapsed_s / ttft_s / input_tokens / output_tokens / tool_events /
iterations_used / snapshot_id / captured_at_utc / source``（``tool_events`` 每条含 ``input``/``obs_len``）

★ **``abstain`` 本运行台有意不写**：它属**判据维度**（docs\\06 §1.2「运行时产生」），
  由 W18 依 `answer_text` 判定。W19 预判就会伪造判据、并与 W18 产生两个真相源。
  该决定写进 `run_manifest.json#honesty.fields_intentionally_absent`，不靠口头约定。

  ★ **R-54 口径修复（2026-10-07）**：此前 W18 侧**只读** ``rec["abstain"]``、**从不推导**
  ⇒ 本运行台不写 + W18 不推 = ``abstain`` **恒为 False**，弃答与失败被静默计入**答错**
  （违反 docs\\05 §4.3「不得悄悄丢弃」，且 ``abstain_rate`` 恒 0）。现已补上推导：
  ``shared/verify/m5/judge.py::derive_abstain``，把四类**分开**登记在 ``abstain_kind`` ——
  ``declared``（记录里显式写）/ ``explicit_refusal``（docs\\06 §2.1 明确拒绝定位）/
  ``no_answer``（docs\\05 §4.3 调用了但没产出答案）/ ``not_measured``（``status∈{blocked,skipped}``，
  没测到；报告侧可单独剔除）。本运行台的立场**不变**：仍不写 ``abstain``。

三态退出码（与 `e2e_smoke.py` 同源）
------------------------------------
``PASS = 0``（全部行都取到回答） / ``FAIL = 1``（测到了、是坏的） /
``BLOCKED = 2``（**没测到**：缺凭据 / App 非 agent-chat / 组集合不一致 / 用例契约坏）。
优先级 ``BLOCKED(2) > FAIL(1) > PASS(0)``。**BLOCKED 不得写成 FAIL，反之亦然。**

复用（不重造）
--------------
* ``shared/deploy/smoke/e2e_smoke.py`` —— ``Console``（Console 登录 + 临时 api-key 建/删/
  回读、201 建 204 删口径，:532-598）、``parse_sse``（SSE 全量解析，:198-271）、
  ``health_audit_len`` 的内存环形缓冲注意事项（:627-644）、``fp`` 凭据指纹（:118-125）、
  ``clip``。真机 Console 调用**整体委托**给 ``e2e_smoke.Console``（见本文件 ``Console._call``）。
* ``shared/deploy/dify/common.py`` —— ``load_env``（只 setdefault、绝不打印值）、
  ``sha256_bytes`` / ``sha256_file`` / ``file_facts``（规格与指纹）、``stable_json`` /
  ``dump_pretty``、``DifyError``、``CookieJar``。
* ``shared/deploy/dify/binding.yaml`` —— **实验组集合与 App uuid 的唯一来源**（``apps`` 段）。
  组名一律从该文件推导，本文件**不含任何组名 / uuid 字面量**。

用法
----
::

    python m5_run.py --selftest                       # 离线自检（假 transport，不联网）
    python m5_run.py --cases <yaml|json> --dry-run    # 只打印矩阵，不联网
    python m5_run.py --cases <yaml|json> --check      # 真机前置不变量（App 模式/工具面/端口）
    python m5_run.py --cases <yaml|json> --out tmp\\m5_run\\run1
    python m5_run.py --verify-answers <answers.jsonl> # fail-closed 复核答案文件契约
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import http.client
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------
# 路径与模块装载
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent            # <repo>/shared/deploy/smoke
DIFY_DIR = HERE.parent / "dify"                   # <repo>/shared/deploy/dify
REPO_ROOT = HERE.parents[2]                       # <repo>
BINDING_PATH = DIFY_DIR / "binding.yaml"
E2E_PATH = HERE / "e2e_smoke.py"
COMMON_PATH = DIFY_DIR / "common.py"


def _load_module(name: str, path: Path):
    """按**绝对路径**装载既有件，避免依赖 sys.path（也就不会撞到同名模块）。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError("无法装载模块 %s（%s）" % (name, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


E2E = _load_module("e2e_smoke", E2E_PATH)          # 复用 SSE 解析 / Console 口径 / 指纹
DIFY = _load_module("dify_common", COMMON_PATH)    # 复用 load_env / sha256 / stable_json

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
EXIT = {PASS: 0, FAIL: 1, BLOCKED: 2}

#: 行状态（写进 answers.jsonl 的 ``status``；``skipped`` 只出现在 manifest，不进行文件）
ST_OK, ST_FAILED, ST_BLOCKED, ST_SKIPPED = "ok", "failed", "blocked", "skipped"

REQUIRED_ROW_KEYS = ("case_id", "group", "repeat", "question", "answer_text")
SOURCE_TAG = "dify-sse"

#: 容器目录里 console/api 前缀（与 binding.dify.api_prefix 一致，仅作默认值）
DEFAULT_API_PREFIX = "console/api"
DEFAULT_REPEAT = 3          # docs\\06 §4 P8「3 次重复」＝公平性硬约束
DEFAULT_TIMEOUT = 300
DEFAULT_USER = "m5-run"

APP_MODE_EXPECTED = "agent-chat"
#: R-41（deploy_agents.py:18-19）：mode ≠ agent-chat ⇒ GET /apps/{uuid} 的 model_config 整体为空。
#: 那是**取不到配置面** ⇒ 永久 BLOCKED，不是 FAIL。

#: docs\\06 §1.2 的用例字段全集（--strict 时逐条要求）
CASE_STRICT_KEYS = (
    "id", "cls", "chain", "scope", "type", "query", "injected",
    "expect_root_cause", "expect_layer", "expect_path", "expect_objects",
    "pos", "neg", "nor", "evidence_facts", "detectors", "difficulty",
    "discriminator",
)
CLS_ALLOWED = ("A", "B", "D", "N", "Z", "T")
DIFFICULTY_ALLOWED = ("easy", "medium", "hard")
#: docs\\06 §1.1 的族条数台账（--strict 只做**台账提示**，完整性判定归 W18）
CLS_QUOTA = {"A": 14, "B": 14, "D": 6, "N": 6, "Z": 6, "T": 4}

#: 冻结的采样参数（R-37 `deploy_agents.py:98-105` + docs\\06 §4 P2）——只作 **advisory** 记录，
#: 不改变本运行台退出码（判据归队长 / 门禁，运行台不越权改判据）。
FROZEN_SAMPLING = {
    "model_name": "deepseek_v4",
    "temperature": 0.3,
    "max_iteration": 40,   # ★ R-60（2026-10-15）：20 → 40（与 deploy_agents.R37_AGENT_CORE 同步）
    "strategy": "react",
    "agent_mode_prompt": None,
    "agent_mode_enabled": True,
}

CMD_SELFTEST = "selftest"
CMD_DRY_RUN = "dry-run"
CMD_CHECK = "check"
CMD_RUN = "run"
CMD_VERIFY = "verify-answers"


class M5Blocked(RuntimeError):
    """前置不变量失败 ⇒ BLOCKED（没测到）。"""


class M5ContractError(RuntimeError):
    """已取证但不一致 ⇒ FAIL（测到了、是坏的）。"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def iso_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha12_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def sha12_text(text: str) -> str:
    return sha12_bytes(text.encode("utf-8"))


def safe_name(name: str) -> str:
    """把 case_id 变成安全的文件名片段（不改变 JSONL 里的原值）。"""
    return re.sub(r"[^0-9A-Za-z_.\-]", "_", str(name)) or "_"


def clip(s, n: int = 200):
    return E2E.clip(s, n)


def first_present(d: dict, keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def write_text_utf8(path: Path, text: str) -> None:
    """R-49：显式 UTF-8 + LF，无 BOM。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def write_jsonl_utf8(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    body = "\n".join(lines) + ("\n" if lines else "")
    with io.open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def read_jsonl(path: Path, errors: list | None = None):
    """读 JSONL。

    * 以 ``utf-8-sig`` 打开：**容忍 Windows/PowerShell 5.1 写出的 UTF-8 BOM**
      （PS 5.1 的 ``Set-Content -Encoding UTF8`` / ``Out-File`` 默认带 BOM；
      带 BOM 的文件用 ``utf-8`` + ``json.loads`` 会直接抛 "Unexpected UTF-8 BOM"）。
    * ``errors`` 给定时，坏行进它并继续（**不抛异常**）——复核器必须 fail-closed
      地报出坏行，而不是把 traceback 甩给调用方。
    """
    rows = []
    with io.open(str(path), encoding="utf-8-sig", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((i, json.loads(line)))
            except ValueError as exc:
                if errors is None:
                    raise
                errors.append("第 %d 行不是合法 JSON：%s" % (i, clip(str(exc), 120)))
    return rows


def three_state(blocked, failed) -> tuple[str, int]:
    if blocked:
        return BLOCKED, EXIT[BLOCKED]
    if failed:
        return FAIL, EXIT[FAIL]
    return PASS, EXIT[PASS]


# --------------------------------------------------------------------------
# 声明面：组集合一律从 binding.yaml 推导（不得硬编码）
# --------------------------------------------------------------------------


def load_binding(path: Path) -> dict:
    try:
        binding = DIFY.load_binding(path)
    except Exception as exc:  # noqa: BLE001
        raise M5Blocked("binding.yaml 不可读：%s" % exc)
    if not isinstance(binding, dict):
        raise M5Blocked("binding.yaml 顶层不是映射：%s" % path)
    return binding


def provider_decls(binding: dict) -> dict:
    """binding.providers（列表或 mapping 两种写法都支持）→ {name: decl}。

    写法兼容与 `deploy_agents.py:284-296` 一致。
    """
    raw = binding.get("providers")
    out: dict[str, dict] = {}
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict) and it.get("provider"):
                out[it["provider"]] = it
    elif isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):
                d = dict(v)
                d.setdefault("provider", k)
                out[k] = d
    return out


def declared_groups(binding: dict) -> dict:
    """从 ``binding.apps`` 推导实验组 → ``{组名: {uuid, carrier, provider, tools, ...}}``。

    **唯一来源**：本函数是本文件里唯一产生组名的地方；任何组名 / uuid 字面量都不得写死。
    任一声明不完整 ⇒ ``M5Blocked``（fail-closed：宁可不跑，也不跑一个不是声明的实验）。
    """
    apps = binding.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise M5Blocked("binding.apps 段缺失或为空 ⇒ 无法推导实验组集合（fail-closed）")
    provs = provider_decls(binding)
    carriers = binding.get("carriers") or {}
    out: dict[str, dict] = {}
    for name, cfg in apps.items():
        if not isinstance(cfg, dict):
            raise M5Blocked("binding.apps.%s 不是映射" % name)
        uuid = cfg.get("uuid")
        if not uuid or not isinstance(uuid, str):
            raise M5Blocked("binding.apps.%s 未登记 uuid ⇒ 组不可运行" % name)
        provider = cfg.get("provider")
        if not provider:
            raise M5Blocked("binding.apps.%s 未登记 provider" % name)
        pdecl = provs.get(provider)
        if pdecl is None:
            raise M5Blocked("binding.apps.%s 声明的 provider %r 未在 binding.providers 登记"
                            % (name, provider))
        tools = cfg.get("tools")
        if not isinstance(tools, list) or not tools:
            raise M5Blocked("binding.apps.%s 的 tools 未声明（工具面不可判）" % name)
        carrier = cfg.get("carrier") or pdecl.get("carrier")
        if not carrier:
            raise M5Blocked("binding.apps.%s 未登记 carrier ⇒ 无法定位其审计平面" % name)
        kind = pdecl.get("kind") or "api"
        # carrier 是否是 binding.carriers 里声明的**载体边车**：
        #   kind=api   ⇒ 工具经载体端口，carrier 必须在 carriers 里（否则审计/健康面不可判）⇒ 缺失即 BLOCKED；
        #   kind=plugin（carrier=kgt-free）⇒ 插件直连 Neo4j、**绕过载体**（README §3 平面 3：审计链不适用），
        #     carrier 只是台账名（`deploy_agents.py:80-85` 的 CARRIER_COUNT_KEY 用它映射 counts.kgt_free_tools），
        #     不在 carriers 里是设计如此，不是声明缺陷。
        carrier_declared = carrier in carriers
        if kind == "api" and not carrier_declared:
            raise M5Blocked("binding.apps.%s 是 api 组、carrier %r 却不在 binding.carriers 里"
                            "（载体面不可判 ⇒ fail-closed）" % (name, carrier))
        out[name] = {
            "group": name,
            "uuid": uuid,
            "carrier": carrier,
            "carrier_declared": bool(carrier_declared),
            "carrier_mediated": bool(carrier_declared and kind == "api"),
            "carrier_service_base": (carriers.get(carrier) or {}).get("service_base"),
            "provider": provider,
            "provider_kind": kind,
            "declared_tools": sorted(str(t) for t in tools),
            "declared_tools_sha256_12": sha12_text("|".join(sorted(str(t) for t in tools))),
            "declared_status": cfg.get("status"),
        }
    return out


def resolve_groups(declared: dict, wanted) -> list[str]:
    """解析 ``--groups``：缺省＝全部声明组（保持 binding 声明顺序）。

    任何**未声明**的组名 ⇒ ``M5Blocked``（沿用 `e2e_smoke.py:1032-1036` 对未知 App 的处理）。
    声明集合与实跑集合不一致 ⇒ 答案文件的组集合就会与声明不一致 ⇒ fail-closed 拒绝。
    """
    if not wanted:
        return list(declared)
    names = [w.strip() for w in wanted if w and w.strip()]
    unknown = [n for n in names if n not in declared]
    if unknown:
        raise M5Blocked("未声明的实验组 %s；binding.apps 声明的是 %s"
                        % (unknown, list(declared)))
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    if not out:
        raise M5Blocked("--groups 解析后为空")
    return out


def selected_map(declared: dict, names) -> dict:
    """把 ``resolve_groups`` 的**组名列表**映射成 ``{组名: 声明配置}``。

    两个入参面不可混用：``plan_matrix`` 只吃组名列表，``Runner`` / ``build_manifest``
    只吃 ``{组名: 配置}`` 映射。首轮真机自证就是在 ``Runner.prepare_group`` 上撞到
    ``TypeError: list indices must be integers or slices, not str``（自检当时全部用现成
    dict，掩盖了这条接线）—— 这里把接线收敛成一个函数，并让自检（L 段）钉住它。
    """
    return {g: declared[g] for g in names}


def parse_host_port(url: str):
    u = urllib.parse.urlsplit(url)
    return u.hostname, (u.port or (443 if u.scheme == "https" else 80))


def check_port_invariants(binding: dict, groups: dict) -> list[dict]:
    """纯本地（不联网）的端口 / 主机 / 工具面**声明侧**不变量。

    判据全部取自 `binding.yaml` 自己的白名单段：``whitelist.allowed_ports`` /
    ``allowed_hosts`` / ``counts.*`` / ``providers[*].port``。
    """
    whitelist = binding.get("whitelist") or {}
    allowed_ports = [int(p) for p in (whitelist.get("allowed_ports") or [])]
    allowed_hosts = [str(h) for h in (whitelist.get("allowed_hosts") or [])]
    counts = binding.get("counts") or {}
    provs = provider_decls(binding)
    recs: list[dict] = []

    def rec(name, ok, detail):
        recs.append({"check": name, "ok": bool(ok), "detail": detail})

    rec("端口白名单非空", bool(allowed_ports),
        "allowed_ports=%s" % allowed_ports)
    rec("主机白名单非空", bool(allowed_hosts),
        "allowed_hosts=%s" % allowed_hosts)

    for pname, pdecl in sorted(provs.items()):
        base = pdecl.get("base")
        kind = pdecl.get("kind") or "api"
        if base:
            host, port = parse_host_port(base)
            rec("provider %s base 主机在白名单" % pname, host in allowed_hosts,
                "base=%s host=%s" % (base, host))
            rec("provider %s base 端口在白名单" % pname, port in allowed_ports,
                "base=%s port=%s" % (base, port))
            rec("provider %s base 端口 == 声明 port" % pname, port == pdecl.get("port"),
                "base_port=%s declared=%s" % (port, pdecl.get("port")))
        else:
            # kind=plugin 无 service port（= 直连插件面，不经载体端口）
            rec("provider %s 无 service base（plugin）" % pname, kind == "plugin",
                "kind=%s base=%r" % (kind, base))
            rec("provider %s 声明 plugin_id" % pname, bool(pdecl.get("plugin_id")),
                "plugin_id=%s" % pdecl.get("plugin_id"))

    for cname, cdecl in sorted((binding.get("carriers") or {}).items()):
        base = cdecl.get("service_base")
        if not base:
            rec("carrier %s 有 service_base" % cname, False, "service_base 缺失")
            continue
        host, port = parse_host_port(base)
        rec("carrier %s 主机在白名单" % cname, host in allowed_hosts, "host=%s" % host)
        rec("carrier %s 端口在白名单" % cname, port in allowed_ports, "port=%s" % port)
        rec("carrier %s app 在 binding.apps" % cname, cdecl.get("app") in groups,
            "app=%s" % cdecl.get("app"))

    # 组 → provider 的工具数与 counts 台账一致。
    # ★ R-52（2026-10-07）：**App 级 `count_key` 优先**，否则才回落 carrier → counts 键的映射
    #   （与 deploy_agents.py 的 tool_count_key 同口径）。R 臂的工具面 = Q 臂去掉 cc_query（7 个），
    #   其台账键是 counts.setb_tools_r；只用 carrier 映射会把它误判成 8 而 FAIL。
    carrier_count_key = {"kg": "setb_tools", "onto": "setb_tools",
                         "kgt": "kgt_main_tools", "kgt-free": "kgt_free_tools"}
    apps_decl = binding.get("apps") or {}
    for gname, g in sorted(groups.items()):
        key = ((apps_decl.get(gname) or {}).get("count_key")
               or carrier_count_key.get(g.get("carrier")))
        if key and counts.get(key) is not None:
            rec("组 %s 工具数 == counts.%s" % (gname, key),
                len(g["declared_tools"]) == int(counts[key]),
                "tools=%d counts.%s=%s" % (len(g["declared_tools"]), key, counts[key]))
        # kind=api 的 provider 声明了权威工具数 ⇒ 组的工具面必须与它逐数相等
        # （api provider 本身不声明工具名单，工具名面只能真机回读；这里只判**数量**台账）
        #
        # ★ R-52（2026-10-07）：provider 的 `expected_tool_count` 描述的是**该 provider 暴露的
        #   全集**，而 App 可以**声明式地绑定一个子集**（R 臂 = Q 臂去掉 cc_query）。此时
        #   权威是本行的 App 级 `count_key`（上面那条已判），不得再用 provider 全集去比
        #   —— 否则 R 臂必然 FAIL（tools=7 vs expected=8）。
        _appd = apps_decl.get(gname) or {}
        if _appd.get("count_key") or _appd.get("tools_removed_from_setb"):
            rec("组 %s 绑定的是**声明子集**（count_key=%s / removed=%s）⇒ 跳过 provider 全集比对"
                % (gname, _appd.get("count_key"), _appd.get("tools_removed_from_setb")),
                True, "tools=%d" % len(g["declared_tools"]))
            continue
        pdecl = provs.get(g.get("provider")) or {}
        exp = pdecl.get("expected_tool_count")
        if exp is not None:
            rec("组 %s 工具数 == provider %s expected_tool_count" % (gname, g.get("provider")),
                len(g["declared_tools"]) == int(exp),
                "tools=%d expected=%s" % (len(g["declared_tools"]), exp))
    return recs


# --------------------------------------------------------------------------
# 用例面
# --------------------------------------------------------------------------


def load_cases_document(path: Path):
    """读用例文件（.yaml/.yml/.json）。返回 ``(cases, meta)``。

    ★ W46 缺陷②：旧 ``load_cases`` 在 ``{"cases": [...]}`` 分支**丢弃 meta**，
    于是用例集声明的 ``meta.snapshot_ids`` 在运行台里不可见 ⇒ 「用例集写快照 A、
    载体在线服务快照 B」没有任何门禁报错。本函数把 meta 一并带出（不改既有调用面）。
    """
    if not path.is_file():
        raise M5Blocked("用例文件不存在：%s" % path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    suffix = path.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            import yaml
            obj = yaml.safe_load(text)
        else:
            obj = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise M5Blocked("用例文件解析失败（%s）：%s" % (suffix or "?", exc))
    meta = {}
    if isinstance(obj, dict):
        raw_meta = obj.get("meta")
        if isinstance(raw_meta, dict):
            meta = raw_meta
        for key in ("cases", "testcases", "items"):
            if isinstance(obj.get(key), list):
                obj = obj[key]
                break
    if not isinstance(obj, list) or not obj:
        raise M5Blocked("用例文件顶层既不是非空列表，也没有 cases 列表：%s" % path)
    cases = []
    for i, c in enumerate(obj):
        if not isinstance(c, dict):
            raise M5Blocked("用例文件第 %d 项不是映射" % (i + 1))
        cases.append(c)
    return cases, meta


def load_cases(path: Path):
    """读用例文件，只返回用例列表（既有调用面，签名不变）。"""
    return load_cases_document(path)[0]


def validate_cases(cases) -> dict:
    """用例契约校验 → ``{hard, strict, warn}`` 三档。

    * ``hard``   ：任何模式下都 fail-closed（缺 id/query、id 重复、query 非字符串/空）
    * ``strict`` ：仅 ``--strict`` 时 fail-closed（§1.2 全字段 / cls 越界 / 难度越界 /
                   ``expect_root_cause`` 空 / ``detectors`` 空）
    * ``warn``   ：只登记，不拦（族条数台账提示等）
    """
    hard: list[str] = []
    strict: list[str] = []
    warn: list[str] = []
    seen: dict[str, int] = {}
    for i, c in enumerate(cases, 1):
        cid = c.get("id")
        if not isinstance(cid, str) or not cid.strip():
            hard.append("第 %d 项缺 id（或非字符串）" % i)
            continue
        if cid in seen:
            hard.append("id 重复：%r（第 %d 项与第 %d 项）" % (cid, seen[cid], i))
        seen[cid] = i
        q = c.get("query")
        if not isinstance(q, str) or not q.strip():
            hard.append("用例 %s 缺 query（或空/非字符串）" % cid)
        missing = [k for k in CASE_STRICT_KEYS if k not in c]
        if missing:
            strict.append("用例 %s 缺 §1.2 字段 %s" % (cid, missing))
        cls = c.get("cls")
        if cls is not None and cls not in CLS_ALLOWED:
            strict.append("用例 %s 的 cls=%r 越界（允许 %s）" % (cid, cls, list(CLS_ALLOWED)))
        diff = c.get("difficulty")
        if diff is not None and diff not in DIFFICULTY_ALLOWED:
            strict.append("用例 %s 的 difficulty=%r 越界（允许 %s）"
                          % (cid, diff, list(DIFFICULTY_ALLOWED)))
        rc = c.get("expect_root_cause")
        if rc is not None and (not isinstance(rc, str) or not rc.strip()):
            strict.append("用例 %s 的 expect_root_cause 为空" % cid)
        det = c.get("detectors")
        if det is not None and (not isinstance(det, list) or not det):
            strict.append("用例 %s 的 detectors 为空" % cid)
    n = len(cases)
    if n != sum(CLS_QUOTA.values()):
        warn.append("用例数 %d ≠ 50（docs\\06 §1.1 的判决集规模）；--limit/子集运行属预期"
                    % n)
    tally: dict[str, int] = {}
    for c in cases:
        tally[str(c.get("cls"))] = tally.get(str(c.get("cls")), 0) + 1
    for cls_name, quota in sorted(CLS_QUOTA.items()):
        if tally.get(cls_name, 0) != quota:
            warn.append("族 %s 实际 %d 条 ≠ 台账 %d 条" % (cls_name, tally.get(cls_name, 0), quota))
    return {"hard": hard, "strict": strict, "warn": warn}


def case_index(cases) -> dict:
    return {c["id"]: c for c in cases}


def plan_matrix(cases, groups, repeat: int, limit: int = 0):
    """生成执行矩阵：**轮转顺序 = repeat → case → group**（docs\\06 §4 P1，消除时序漂移）。

    ``limit > 0`` 时只取矩阵前 ``limit`` 条为「计划执行」，其余作 ``skipped``（不接受不等于已跑）。
    返回 ``(plan, skipped)``；plan 内每项含 ``order/repeat/case_id/group/case``。
    """
    plan, skipped = [], []
    n = 0
    for rep in range(1, int(repeat) + 1):
        for c in cases:
            for g in groups:
                n += 1
                row = {"order": n, "repeat": rep, "case_id": c["id"], "group": g, "case": c}
                if limit and n > limit:
                    skipped.append(row)
                else:
                    plan.append(row)
    return plan, skipped


def plan_key(row) -> tuple:
    return (row["case_id"], row["group"], row["repeat"])


def assert_unique_plan(plan) -> None:
    seen = {}
    for r in plan:
        k = plan_key(r)
        if k in seen:
            raise M5ContractError("(case_id, group, repeat) 重复：%s" % (k,))
        seen[k] = r


# --------------------------------------------------------------------------
# Console（口径承 e2e_smoke.py；传输可注入 ⇒ 离线自检不需要网络）
# --------------------------------------------------------------------------


class Console:
    """Dify Console 客户端：登录 + App 回读 + 临时 api-key 建/删/回读。

    ``transport=None`` ⇒ **真机路径整体委托** ``e2e_smoke.Console``（口径逐字一致）；
    传入 transport ⇒ 离线自检路径（transport 只需实现
    ``call(method, path, body=None, timeout=60) -> (status, body)``）。
    """

    def __init__(self, base: str, transport=None, prefix: str = DEFAULT_API_PREFIX):
        self.base = base.rstrip("/")
        self.prefix = prefix.strip("/")
        self.transport = transport
        self._real = None if transport is not None else E2E.Console(self.base)
        # ★ B1（R-54）：登录凭据只在内存中留一份，供会话过期时**重登录一次**使用。
        #   绝不落盘、绝不打印（本类其余部分也不打印凭据）。
        self._cred = None
        #: 最近一次「401 ⇒ 重登录 ⇒ 重试」的读数（None = 未发生）；供证据面登记。
        self.last_reauth = None

    # -- 传输 -------------------------------------------------------------
    def _call(self, method, path, body=None, timeout=60):
        if self._real is not None:
            return self._real.call(method, path, body, timeout)
        st, d = self.transport.call(method, path, body, timeout)
        return st, d

    def _p(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return "/%s%s" % (self.prefix, path)

    # -- 登录 -------------------------------------------------------------
    def login(self, email: str, password: str) -> int:
        if self._real is not None:
            self._cred = (email, password)
            return self._real.login(email, password)
        b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
        st, _ = self._call("POST", self._p("/login"),
                           {"email": email, "password": b64,
                            "language": "zh-Hans", "remember_me": True})
        if st == 200:
            self._cred = (email, password)
            jar = getattr(self.transport, "cookies", None)
            if jar is not None:
                jar.set("csrf_token", "selftest-csrf")
                jar.set("access_token", "selftest-access")
        return st

    # -- 只读回读 ---------------------------------------------------------
    def app_detail(self, uuid: str):
        return self._call("GET", self._p("/apps/%s" % uuid))

    def bound_tools(self, uuid: str):
        """启用中的工具名集合。抽取式逐字承 `e2e_smoke.py:575-582`。"""
        st, d = self.app_detail(uuid)
        if st != 200 or not isinstance(d, dict):
            return set(), "app detail HTTP %s" % st
        am = ((d.get("model_config") or {}).get("agent_mode")) or {}
        tools = {t.get("tool_name") for t in (am.get("tools") or [])
                 if isinstance(t, dict) and t.get("enabled", True)}
        return {t for t in tools if t}, None

    def list_keys(self, uuid: str):
        st, d = self._call("GET", self._p("/apps/%s/api-keys" % uuid))
        if st != 200:
            return st, None
        data = d.get("data") if isinstance(d, dict) else d
        return st, (data or [])

    # -- 临时 api-key 建 / 删（e2e_smoke.py:584-598 口径）-------------------
    def create_key(self, uuid: str):
        """返回 ``(key_id, token, http_status, error)``。

        2026-09-29 真机实测：该路由**不接受任何请求体**（`e2e_smoke/README.md:94-96`），
        成功返回 **201**，失败时 body 是错误文本。

        ★ B1（R-54）：**401 ⇒ 重登录一次并重试**。
        根因（真机实测）：Console 会话只在本组开跑时登录一次，而本运行台**每行都新建一个
        临时 api-key**（一组 150 行 = 150 次建 + 150 次删）⇒ 会话过期后 ``create_key``
        一律 401，原实现**没有任何重登录路径**，于是连续 blocked 触发驱动器中止
        （M5 主跑 ``tmp\\m5_matrix\\m1`` 实测：**150 行只产出 26 行**）。
        证据与因果链见 ``tmp/_m5_wall_report.md`` §3。
        重试**只做一次**；仍失败即如实返回 401（不掩盖、不循环）。
        """
        st, d = self._call("POST", self._p("/apps/%s/api-keys" % uuid), {})
        if st == 401 and self._cred is not None:
            relogin_http = self.login(self._cred[0], self._cred[1])
            st2, d2 = self._call("POST", self._p("/apps/%s/api-keys" % uuid), {})
            #: 登记三态读数，便于证据面区分「没发生 / 重登录救回来了 / 重登录也没救回来」
            self.last_reauth = {"trigger_http": st, "relogin_http": relogin_http,
                                "retry_http": st2, "recovered": st2 in (200, 201)}
            st, d = st2, d2
        else:
            self.last_reauth = None
        if st not in (200, 201) or not isinstance(d, dict):
            return None, None, st, d
        return d.get("id"), d.get("token"), st, None

    def delete_key(self, uuid: str, key_id):
        return self._call("DELETE", self._p("/apps/%s/api-keys/%s" % (uuid, key_id)))


# --------------------------------------------------------------------------
# 采集面：/v1/chat-messages 流式
# --------------------------------------------------------------------------

_DATA_PREFIX = "data:"


def _line_meta(line: str):
    """单行 SSE → ``(event, answer, has_payload)``（只做 ttft 需要的最轻解析）。"""
    if not line.startswith(_DATA_PREFIX):
        return None, "", False
    payload = line[len(_DATA_PREFIX):].strip()
    if not payload or payload == "[DONE]":
        return None, "", False
    try:
        ev = json.loads(payload)
    except ValueError:
        return None, "", False
    if not isinstance(ev, dict):
        return None, "", False
    ans = ev.get("answer") or ""
    return ev.get("event"), (ans if isinstance(ans, str) else ""), True


def http_post_chat(base, token, query, user, timeout, on_line):
    """真机 opener：POST /v1/chat-messages（streaming），逐行 readline 以便测 TTFT。

    与 `e2e_smoke.chat_messages`（:601-624）同路由、同 payload、同响应模式；
    唯一差别是**逐行读取**（要首 token 时延，不能一次 read() 到底）。
    每次调用 ``conversation_id=""`` ⇒ P4「无跨用例记忆」。
    """
    scheme, host, port = E2E.split_base(base)
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
    parts: list[str] = []
    t0 = time.perf_counter()
    try:
        conn.request("POST", "/v1/chat-messages", body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        while True:
            chunk = resp.readline()
            if not chunk:
                break
            s = chunk.decode("utf-8", "replace")
            parts.append(s)
            try:
                on_line(s, time.perf_counter() - t0)
            except Exception:  # noqa: BLE001 —— 回调不得影响读取
                pass
        return status, "".join(parts)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def capture_chat(opener, base, token, query, user=DEFAULT_USER, timeout=DEFAULT_TIMEOUT) -> dict:
    """一次流式调用 → 原始 SSE + 耗时 + 首 token 时延。

    ``ttft_s`` 定义：从请求发出到**第一个携带非空 answer 的 `agent_message`/`message`
    事件**的秒数；若整轮没有该事件但 `message_end` 带 answer，则取该事件时刻并记
    ``ttft_basis="message_end"``；两者都没有 ⇒ ``ttft_s=None, ttft_basis="absent"``
    （**如实记缺，不补 0**）。时基由 opener 提供（真机路径 = ``perf_counter`` 差值）。
    """
    st = {"ttft": None, "basis": "absent", "first_event_s": None}

    def on_line(line, t_rel):
        ev, ans, has = _line_meta(line)
        if not has:
            return
        if st["first_event_s"] is None:
            st["first_event_s"] = t_rel
        if st["ttft"] is None:
            if ev in ("agent_message", "message") and ans:
                st["ttft"] = t_rel
                st["basis"] = "first_answer_delta"
            elif ev == "message_end" and ans:
                st["ttft"] = t_rel
                st["basis"] = "message_end"

    t0 = time.perf_counter()
    status, raw = opener(base, token, query, user, timeout, on_line)
    elapsed = time.perf_counter() - t0
    return {
        "http_status": status,
        "raw": raw,
        "elapsed_s": round(elapsed, 3),
        "ttft_s": None if st["ttft"] is None else round(st["ttft"], 3),
        "ttft_basis": st["basis"],
        "first_event_s": None if st["first_event_s"] is None else round(st["first_event_s"], 3),
        "raw_bytes": len(raw.encode("utf-8")),
        "raw_sha256_12": sha12_text(raw),
    }


def row_fields_from_parsed(parsed: dict) -> dict:
    """把 ``parse_sse`` 的结果映射到行契约字段（**只搬证据，不解释**）。

    ``tool_events`` 每条的 ``input``（``tool_input`` 原文）与 ``obs_len``（observation 的
    UTF-8 字节数）是 W18 效率表的**必需输入**（``m5_judge.efficiency_from_record``：去重键
    ``(name, input)``、字节数求和 ``obs_len``）⇒ 本函数只照抄原文 / 字节长度，不做解释。
    ``iterations_used`` = ``agent_thought`` 事件里 ``position`` 的**最大值**（Dify 的轮次计数，
    1-based，含**没有工具调用**的轮次）；**无 position 事件 ⇒ None**，绝不补 0 —— 与本文件
    及 ``m5_judge`` 的「宁缺勿造」纪律一致（``None`` ≠ 「用了 0 轮」）。
    """
    usage = parsed.get("usage") or {}
    md = parsed.get("metadata") or {}
    thoughts = parsed.get("thoughts") or []
    positions = [t.get("position") for t in thoughts
                 if isinstance(t.get("position"), int) and not isinstance(t.get("position"), bool)]
    tool_events = []
    for t in thoughts:
        if not t.get("tool"):
            continue
        obs = t.get("observation") or ""
        tool_events.append({
            "name": t["tool"],
            "status": "ok" if obs else "no_observation",
            "tool_input_len": t.get("tool_input_len"),
            "observation_len": t.get("observation_len"),
            "input": t.get("tool_input"),
            "obs_len": len(obs.encode("utf-8")),
        })
    return {
        "answer_text": parsed.get("answer") or "",
        "input_tokens": first_present(usage, ("prompt_tokens", "input_tokens")),
        "output_tokens": first_present(usage, ("completion_tokens", "output_tokens")),
        "usage_raw": dict(usage) if isinstance(usage, dict) else None,
        "tool_events": tool_events,
        # ★ 响应侧模型名：可能为 None（`smoke/README.md:118-121` 实测 model/model_provider 均为 None）。
        #   绝不拿 App 配置里的 deepseek_v4 冒充响应证据 ⇒ 字段名显式带 _from_response。
        "model_from_response": parsed.get("model"),
        "model_provider_from_response": md.get("model_provider"),
        "n_sse_events": parsed.get("n_events"),
        "event_types": parsed.get("event_types"),
        "sse_error": parsed.get("error"),
        "conversation_id": parsed.get("conversation_id"),
        "message_id": parsed.get("message_id"),
        "executed_tool_names": parsed.get("executed_tool_names") or [],
        "iterations_used": max(positions) if positions else None,
    }


# --------------------------------------------------------------------------
# 模型 / 采样参数回读（结构范本承 deploy_agents.py:662-689 face_summary）
# --------------------------------------------------------------------------


def model_readback(mc: dict) -> dict:
    """从 App 详情真机回读的 ``model_config`` 里取**模型与采样参数**（只读，不改配置）。"""
    mc = mc if isinstance(mc, dict) else {}
    model = mc.get("model") if isinstance(mc.get("model"), dict) else {}
    cp = model.get("completion_params") if isinstance(model.get("completion_params"), dict) else {}
    am = mc.get("agent_mode") if isinstance(mc.get("agent_mode"), dict) else {}
    tools = am.get("tools") if isinstance(am.get("tools"), list) else []
    pp = mc.get("pre_prompt")
    return {
        "model": {
            "name": model.get("name"),
            "provider": model.get("provider"),
            "mode": model.get("mode"),
        },
        "completion_params": dict(cp),
        "temperature": cp.get("temperature"),
        "agent_mode_core": {k: am.get(k) for k in ("enabled", "strategy", "max_iteration", "prompt")},
        "max_iteration": am.get("max_iteration"),
        "strategy": am.get("strategy"),
        "pre_prompt_len": len(pp) if isinstance(pp, str) else None,
        "pre_prompt_sha256": sha12_text(pp) if isinstance(pp, str) else None,
        "tool_count": len([t for t in tools if isinstance(t, dict) and t.get("enabled", True)]),
        "model_sha256_full": DIFY.sha256_bytes(DIFY.stable_json(model).encode("utf-8")) if model else None,
        "agent_mode_core_sha256_full": DIFY.sha256_bytes(
            DIFY.stable_json({k: am.get(k) for k in ("enabled", "strategy", "max_iteration", "prompt")}
                             ).encode("utf-8")) if am else None,
        "model_config_keys": sorted(mc.keys()),
        "model_config_sha256_full": DIFY.sha256_bytes(DIFY.stable_json(mc).encode("utf-8")) if mc else None,
    }


def sampling_drift(rb: dict) -> list[str]:
    """与冻结采样参数（R-37 / docs\\06 §4 P2）的偏差 —— **advisory，不改退出码**。"""
    drift = []
    model = rb.get("model") or {}
    if model.get("name") not in (None, FROZEN_SAMPLING["model_name"]):
        drift.append("model.name=%r ≠ %r" % (model.get("name"), FROZEN_SAMPLING["model_name"]))
    if rb.get("temperature") not in (None, FROZEN_SAMPLING["temperature"]):
        drift.append("completion_params.temperature=%r ≠ %r"
                     % (rb.get("temperature"), FROZEN_SAMPLING["temperature"]))
    if rb.get("max_iteration") not in (None, FROZEN_SAMPLING["max_iteration"]):
        drift.append("agent_mode.max_iteration=%r ≠ %r"
                     % (rb.get("max_iteration"), FROZEN_SAMPLING["max_iteration"]))
    if rb.get("strategy") not in (None, FROZEN_SAMPLING["strategy"]):
        drift.append("agent_mode.strategy=%r ≠ %r" % (rb.get("strategy"), FROZEN_SAMPLING["strategy"]))
    core = rb.get("agent_mode_core") or {}
    if core.get("enabled") not in (None, FROZEN_SAMPLING["agent_mode_enabled"]):
        drift.append("agent_mode.enabled=%r ≠ %r"
                     % (core.get("enabled"), FROZEN_SAMPLING["agent_mode_enabled"]))
    if core.get("prompt") != FROZEN_SAMPLING["agent_mode_prompt"]:
        drift.append("agent_mode.prompt=%r ≠ %r"
                     % (core.get("prompt"), FROZEN_SAMPLING["agent_mode_prompt"]))
    return drift


# --------------------------------------------------------------------------
# 采集源指纹（docs\\06 §4 P9：从载体 /health 取采集源回执，跑前断言 simulated=false）
# --------------------------------------------------------------------------


def read_health(service_base: str, timeout: int = 15) -> dict:
    url = service_base.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        obj = json.loads(body)
        return {"ok": True, "url": url, "health": obj if isinstance(obj, dict) else None}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


def capture_source(service_base: str) -> dict:
    """读一个载体的 /health 并抽取采集源指纹。

    ★ 诚实边界：``auditChain.length`` 是**内存环形缓冲**长度，与磁盘 JSONL 行数不等
      （`e2e_smoke.py:627-644`）——此处只登记，不当判据。
    ``simulated`` 字段若不存在，**如实记「未能机械断言」**，不推断为 false。
    """
    h = read_health(service_base)
    rec = {"service_base": service_base, "url": h["url"], "ok": bool(h.get("ok"))}
    if not h.get("ok"):
        rec["error"] = h.get("error")
        rec["simulated_assert"] = "BLOCKED：/health 取不到 ⇒ 采集源指纹未取到"
        return rec
    obj = h.get("health") or {}
    chain = obj.get("auditChain")
    n = chain.get("length") if isinstance(chain, dict) else (len(chain) if isinstance(chain, list) else None)
    rec["audit_file"] = obj.get("auditFile")
    rec["audit_chain_len_memory"] = n
    rec["audit_chain_len_caveat"] = "内存环形缓冲长度，≠ 磁盘 JSONL 行数"
    rec["status"] = obj.get("status")
    rec["carrier"] = obj.get("carrier")
    sim = first_present(obj, ("simulated",))
    if sim is None:
        cp = obj.get("collectorProvenance") or obj.get("integrity") or {}
        if isinstance(cp, dict):
            inner = cp.get("collectorProvenance") if isinstance(cp.get("collectorProvenance"), dict) else cp
            sim = first_present(inner, ("simulated",)) if isinstance(inner, dict) else None
    if sim is None:
        rec["simulated"] = None
        rec["simulated_assert"] = "未断言：/health 响应里没有 simulated 字段（不得推断为 false）"
    else:
        rec["simulated"] = sim
        rec["simulated_assert"] = ("ok：simulated=false" if sim is False
                                   else "★ FAIL：simulated=%r（P9 要求 false）" % (sim,))
    return rec


# --------------------------------------------------------------------------
# 用例集 ↔ 载体在线快照 同源断言（W46 缺陷②，fail-closed）
# --------------------------------------------------------------------------


def declared_case_snapshot(cases, meta) -> tuple:
    """取用例集声明的「同源快照」。返回 ``(snapshot_id | None, problems: list)``。

    口径（与 ``m5_cases_gate`` 的 ``E-CASES-OBJECT-SNAPSHOT`` 同一套）：
      * ``meta.snapshot_ids`` 必须恰 1 项（C1 单一输入；W45 实测 id 集会随快照漂移——
        相隔 47 分钟的两次冻结 12 类计数逐类相同，但有 3 个 Session + 1 个 Table 不同）；
      * 用例内 ``snapshot_id`` 若出现，必须与 meta 声明的那个逐字相同。
    """
    problems: list[str] = []
    snaps = meta.get("snapshot_ids") if isinstance(meta, dict) else None
    decl = None
    if isinstance(snaps, list):
        if len(snaps) != 1:
            problems.append("meta.snapshot_ids=%r，长度必须恰 1（C1 单一输入）" % (snaps,))
        else:
            decl = str(snaps[0])
    else:
        problems.append("meta.snapshot_ids 缺失或不是列表：%r" % (snaps,))
    inner = sorted({str(c.get("snapshot_id")) for c in cases
                    if isinstance(c, dict) and c.get("snapshot_id")})
    if decl is not None and inner and inner != [decl]:
        problems.append("用例内 snapshot_id 集合 %r 与 meta.snapshot_ids 声明 %r 不一致"
                        % (inner, decl))
    return decl, problems


def declared_canonical_sha(meta):
    """用例集侧声明的 ``canonicalSha256``（若有）。真机 ``/health`` 不暴露该字段 ⇒
    一侧给不出时**不**据此 BLOCKED，只在两侧都给得出时比对（见调用处注释）。"""
    if not isinstance(meta, dict):
        return None
    idx = meta.get("object_index")
    for src in (meta, idx if isinstance(idx, dict) else {}):
        for key in ("canonicalSha256", "canonical_sha256", "canonicalSha"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def online_snapshot_of(service_base: str, health_reader=None) -> dict:
    """读一个载体 ``/health`` 的在线快照指纹。

    返回 ``{ok, url, snapshot_id, canonical_sha256, error}``。★ 只读既有载体，
    不连任何新地址、不需要凭据；取不到 ⇒ ``ok=False``，调用方按 **BLOCKED** 处理
    （离线取不到在线快照 = 没测到，不是测得坏结果）。
    """
    reader = health_reader or read_health
    h = reader(service_base)
    rec = {"ok": bool(h.get("ok")), "url": h.get("url"),
           "snapshot_id": None, "canonical_sha256": None, "error": h.get("error")}
    if not rec["ok"]:
        return rec
    obj = h.get("health") or {}
    snap = first_present(obj, ("snapshotId", "snapshot_id"))
    canon = first_present(obj, ("canonicalSha256", "canonical_sha256"))
    if snap is None or canon is None:
        inner = obj.get("collectorProvenance") or obj.get("integrity") or {}
        if isinstance(inner, dict):
            if snap is None:
                snap = first_present(inner, ("snapshotId", "snapshot_id"))
            if canon is None:
                canon = first_present(inner, ("canonicalSha256", "canonical_sha256"))
    rec["snapshot_id"] = str(snap) if snap is not None else None
    rec["canonical_sha256"] = str(canon) if canon is not None else None
    return rec


def assert_case_snapshot_binding(cases, meta, selected, *, health_reader=None, out=print):
    """开跑前 fail-closed 断言「用例集声明的快照 == 各载体在线快照」。

    返回 ``None``（放行）或 ``EXIT[BLOCKED]``（=2）。★ 顺序契约：本函数只在
    **输入合法之后**被调用（用例文件缺失/形态非法仍在 ``load_cases_document`` 抛
    ``M5Blocked``，由 main 打印既有的 ``★ BLOCKED（≠ FAIL）…`` 语义），因此新断言
    不会抢在既有输入校验之前报错。★ ``--no-health`` 不豁免本断言：该开关只控制
    逐条采集的健康回执，读不到在线快照就是没测到，如实 BLOCKED。
    """
    out("M5 运行台 --snapshot-bind：用例集 ↔ 载体在线快照 同源断言（fail-closed，不写文件）")
    decl, problems = declared_case_snapshot(cases, meta)
    wants_canon = declared_canonical_sha(meta)
    out("  用例集声明：meta.snapshot_ids=%r ⇒ 权威同源快照=%s；canonicalSha256=%s"
        % ((meta or {}).get("snapshot_ids") if isinstance(meta, dict) else None,
           decl if decl is not None else "<未声明>", wants_canon or "<未声明>"))
    if problems:
        # ★ 声明面不合法 ⇒ **立即** BLOCKED，连 /health 都不读（无对象可比，读了也无意义）
        out("★ BLOCKED（≠ FAIL）：用例集未声明唯一快照 ⇒ 无法断言与载体在线快照同源"
            "（未测量 ≠ 测得坏结果）")
        for p in problems:
            out("    - %s" % p)
        out("  fail-closed ⇒ 退出码 2（不跑一个与用例集不同源的实验）")
        return EXIT[BLOCKED]

    carriers, skipped = [], []
    for name, cfg in _as_selected_mapping(selected).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        base = cfg.get("carrier_service_base")
        if cfg.get("carrier_mediated") and base:
            carriers.append((name, cfg.get("carrier"), base))
        else:
            skipped.append((name, cfg.get("carrier"),
                            "非载体边车（carrier_mediated=%r，无 /health 面）" % cfg.get("carrier_mediated")))
    for name, carrier, why in skipped:
        out("  · 组 %s 跳过：载体 %s %s" % (name, carrier, why))
    if not carriers:
        out("  · 本次所选组里没有可比的载体边车 ⇒ 无在线快照可比（不 BLOCKED）")

    fails: list[str] = []
    observes: list[str] = []
    canon_seen: list[str] = []
    for name, carrier, base in carriers:
        rec = online_snapshot_of(base, health_reader=health_reader)
        if not rec["ok"]:
            fails.append("组 %s：载体 %s 的 /health 取不到（%s）⇒ 在线快照未取到"
                         % (name, base, rec.get("error")))
            observes.append("组 %s  载体 %s  base %s  在线 snapshotId=<取不到：%s>"
                            % (name, carrier, base, rec.get("error")))
            continue
        snap = rec["snapshot_id"]
        observes.append("组 %s  载体 %s  base %s  在线 snapshotId=%s"
                        % (name, carrier, base, snap if snap is not None else "<响应无该字段>"))
        if snap is None:
            fails.append("组 %s：载体 %s 的 /health 响应里没有 snapshotId/snapshot_id ⇒ 未取到在线快照"
                         % (name, base))
        elif decl is not None and snap != decl:
            fails.append("组 %s：载体 %s 在线快照 %s ≠ 用例集声明 %s ⇒ 用例集与在线载体不同源"
                         % (name, base, snap, decl))
        if rec["canonical_sha256"] and wants_canon and rec["canonical_sha256"] != wants_canon:
            fails.append("组 %s：载体 %s 在线 canonicalSha256 %s ≠ 用例集声明 %s"
                         % (name, base, rec["canonical_sha256"], wants_canon))
        canon_seen.append(rec["canonical_sha256"] or "")
    if wants_canon and carriers and not any(canon_seen):
        out("  · canonicalSha256 未比对：载体 /health 未暴露该字段（真机实测如此）"
            " ⇒ 不据此 BLOCKED（能力缺失不得误判成不同源）")

    for line in observes:
        out("  " + line)
    if fails:
        out("★ BLOCKED（≠ FAIL）：用例集 ↔ 载体在线快照 同源断言未通过 —— 未测量 ≠ 测得坏结果")
        out("  双方取值：用例集 meta.snapshot_ids=%r（权威同源快照 %s）"
            % ((meta or {}).get("snapshot_ids") if isinstance(meta, dict) else None,
               decl if decl is not None else "<未声明>"))
        for line in observes:
            out("            " + line)
        out("  判因（%d 项）：" % len(fails))
        for f in fails:
            out("    - %s" % f)
        out("  fail-closed ⇒ 退出码 2（不跑一个与用例集不同源的实验）")
        return EXIT[BLOCKED]
    if carriers:
        out("  OK：用例集声明快照与 %d 个载体在线快照同源" % len(carriers))
    else:
        out("  OK：本次所选组没有载体边车面 ⇒ 该断言无对象（如实登记，不推断同源）")
    return None


# --------------------------------------------------------------------------
# 运行台
# --------------------------------------------------------------------------


class Runner:
    """用例 × 组 × repeat 的采集循环。**单条失败不中断整批**，逐条记 status 与原因。"""

    def __init__(self, binding, groups, selected, out_dir: Path, console, chat_opener,
                 repeat: int, limit: int, timeout: int, user: str, snapshot_id=None,
                 pace_s: float = 0.0, max_key_failures: int = 3, read_health_flag: bool = True):
        self.binding = binding
        self.declared = groups
        self.selected = selected
        self.out_dir = Path(out_dir)
        self.console = console
        self.chat_opener = chat_opener
        self.repeat = int(repeat)
        self.limit = int(limit)
        self.timeout = int(timeout)
        self.user = user
        self.snapshot_id = snapshot_id
        self.pace_s = float(pace_s)
        self.max_key_failures = int(max_key_failures)
        self.read_health_flag = read_health_flag
        self.group_state: dict[str, dict] = {}
        self.key_log: list[dict] = []
        self.capture_sources: list[dict] = []

    # -- 每组一次的只读回读面 -------------------------------------------
    def prepare_group(self, gname: str) -> dict:
        cfg = self.selected[gname]
        st = {
            "group": gname, "uuid": cfg["uuid"], "carrier": cfg["carrier"],
            "provider": cfg["provider"], "provider_kind": cfg["provider_kind"],
            "declared_tools": list(cfg["declared_tools"]),
            "declared_tools_sha256_12": cfg["declared_tools_sha256_12"],
            "captured_at_utc": iso_utc(),
            "app_mode": None, "blocked": False, "fail": False, "reasons": [],
        }
        status, det = self.console.app_detail(cfg["uuid"])
        st["app_detail_http"] = status
        if status != 200 or not isinstance(det, dict):
            st["blocked"] = True
            st["reasons"].append("GET /console/api/apps/<uuid> HTTP %s ⇒ App 详情取不到（BLOCKED）"
                                 % status)
            return st
        st["app_name"] = det.get("name")
        mode = det.get("mode")
        st["app_mode"] = mode
        mc = det.get("model_config") if isinstance(det.get("model_config"), dict) else {}
        st["model_config_keys"] = sorted(mc.keys())
        if mode != APP_MODE_EXPECTED:
            # R-41：非 agent-chat 的 App 回读 model_config 必为空 ⇒ 取不到配置面 ⇒ 永久 BLOCKED
            st["blocked"] = True
            st["reasons"].append("App mode=%r ≠ %r ⇒ model_config 必为空、无法取到模型/采样面 ⇒ "
                                 "永久 BLOCKED（R-41，deploy_agents.py:18-19）"
                                 % (mode, APP_MODE_EXPECTED))
            return st
        if not mc:
            st["blocked"] = True
            st["reasons"].append("agent-chat 但 model_config 回读为空 ⇒ 配置面取不到（BLOCKED）")
            return st
        bound, err = self.console.bound_tools(cfg["uuid"])
        st["readback_tools"] = sorted(bound)
        st["readback_tools_sha256_12"] = sha12_text("|".join(sorted(bound)))
        st["bound_tools_error"] = err
        declared = set(cfg["declared_tools"])
        st["declared_equals_readback"] = (bound == declared) and bool(bound)
        if not st["declared_equals_readback"]:
            st["fail"] = True
            st["reasons"].append(
                "工具面与 binding 声明不一致：仅声明有 %s；仅真机有 %s（fail-closed）"
                % (sorted(declared - bound), sorted(bound - declared)))
        rb = model_readback(mc)
        rb["drift_vs_frozen"] = sampling_drift(rb)
        st["model_readback"] = rb
        st["sampling_drift_advisory"] = list(rb["drift_vs_frozen"])
        return st

    # -- 单次调用 --------------------------------------------------------
    def run_one(self, p) -> tuple[dict, dict]:
        """一次 ``(case, group, repeat)``。

        临时 api-key 的生命周期是**每次调用一副**（建 → 用 → 删，``finally`` 里删并回读确认）；
        删除结果无论成功失败都记进 ``key_log``。
        """
        cfg = self.selected[p["group"]]
        case = p["case"]
        uuid = cfg["uuid"]
        question = case["query"]
        row = {
            "case_id": p["case_id"],
            "group": p["group"],
            "repeat": p["repeat"],
            "question": question,
            "answer_text": "",
            "elapsed_s": None,
            "ttft_s": None,
            "ttft_basis": "absent",
            "first_event_s": None,
            "input_tokens": None,
            "output_tokens": None,
            "usage_raw": None,
            "tool_events": [],
            "iterations_used": None,
            "snapshot_id": self.snapshot_id if self.snapshot_id is not None else case.get("snapshot_id"),
            "captured_at_utc": iso_utc(),
            "source": SOURCE_TAG,
            "status": ST_FAILED,
            "reason": None,
            "order": p["order"],
        }
        if row["question"] != case["query"]:      # 逐字题面：不接受任何加工
            row["status"] = ST_BLOCKED
            row["reason"] = "题面被加工（内部不变量破裂）"
            return row, {}
        row["query_verbatim_ok"] = True
        row["question_sha256_12"] = sha12_text(question)

        created = iso_utc()
        key_id, token, kst, kerr = self.console.create_key(uuid)
        krec = {
            "case_id": p["case_id"], "group": p["group"], "repeat": p["repeat"],
            "api_key_id": key_id, "create_http": kst,
            "token_fp": E2E.fp(token) if token else None,
            "created_at_utc": created, "delete_http": None,
            "deleted_at_utc": None, "relist_http": None, "n_keys_after": None,
            "removed_confirmed": None, "delete_attempted": False,
            # ★ B1（R-54）：本次建 key 是否经历了「401 ⇒ 重登录 ⇒ 重试」；None = 未发生。
            "console_reauth": self.console.last_reauth,
        }
        if not token:
            row["status"] = ST_BLOCKED
            row["reason"] = "建临时 api-key 失败 HTTP %s（%s）⇒ 无凭据，未发起调用" % (kst, clip(kerr, 160))
            self.key_log.append(krec)
            return row, krec

        try:
            try:
                res = capture_chat(self.chat_opener, self._base(), token, question,
                                   user=self.user, timeout=self.timeout)
            except Exception as exc:  # noqa: BLE001 —— 单条异常不中断整批
                row["status"] = ST_FAILED
                row["reason"] = "调用异常：%s: %s" % (type(exc).__name__, clip(str(exc), 200))
                self.key_log.append(krec)
                return row, krec

            row["elapsed_s"] = res["elapsed_s"]
            row["ttft_s"] = res["ttft_s"]
            row["ttft_basis"] = res["ttft_basis"]
            row["first_event_s"] = res["first_event_s"]
            row["http_status"] = res["http_status"]
            row["sse_bytes"] = res["raw_bytes"]
            row["sse_sha256_12"] = res["raw_sha256_12"]
            row["captured_at_utc"] = created
            # 原始 SSE 原样落盘（第三方可复算，不依赖本脚本的解析结论）
            sse_path = (self.out_dir / "sse" /
                        ("%s__%s__r%d.txt" % (safe_name(p["group"]), safe_name(p["case_id"]),
                                              p["repeat"])))
            write_text_utf8(sse_path, res["raw"])
            row["sse_file"] = str(sse_path.relative_to(self.out_dir)).replace("\\", "/")

            parsed = E2E.parse_sse(res["raw"]) if res["http_status"] == 200 else E2E.parse_sse("")
            row.update(row_fields_from_parsed(parsed))
            if res["http_status"] != 200:
                row["status"] = ST_FAILED
                row["reason"] = "HTTP %s（非 200）" % res["http_status"]
            elif parsed.get("error"):
                row["status"] = ST_FAILED
                row["reason"] = "SSE error 事件：%s" % clip(parsed["error"], 200)
            elif not row["answer_text"]:
                row["status"] = ST_FAILED
                row["reason"] = "回答为空（answer_len=0）"
            else:
                row["status"] = ST_OK
                row["reason"] = None
            return row, krec
        finally:
            # ★ 无论成功失败都删 key，并回读确认
            dst, dbody = self.console.delete_key(uuid, key_id)
            krec["delete_attempted"] = True
            krec["delete_http"] = dst
            krec["delete_body"] = clip(dbody, 120) if isinstance(dbody, str) else None
            krec["deleted_at_utc"] = iso_utc()
            lst, keys = self.console.list_keys(uuid)
            remaining = [k.get("id") for k in (keys or []) if isinstance(k, dict)]
            krec["relist_http"] = lst
            krec["n_keys_after"] = len(remaining)
            krec["removed_confirmed"] = (key_id not in remaining) and (dst in (200, 204))
            self.key_log.append(krec)

    def _base(self) -> str:
        return getattr(self, "base_url", "") or ""

    # -- 整批 ------------------------------------------------------------
    def run(self, plan, skipped) -> dict:
        rows: list[dict] = []
        plan_rows_meta: list[dict] = []

        for g in self.selected:
            self.group_state[g] = self.prepare_group(g)
        if self.read_health_flag:
            for cname, cdecl in sorted((self.binding.get("carriers") or {}).items()):
                sb = cdecl.get("service_base")
                if sb:
                    rec = capture_source(sb)
                    rec["carrier"] = cname
                    self.capture_sources.append(rec)

        consecutive_key_failures: dict[str, int] = {}
        short_circuited: dict[str, bool] = {}
        n_ok = n_failed = n_blocked = 0
        for p in plan:
            gname = p["group"]
            gst = self.group_state[gname]
            if gst.get("blocked"):
                row = {
                    "case_id": p["case_id"], "group": gname, "repeat": p["repeat"],
                    "question": p["case"]["query"], "answer_text": "",
                    "elapsed_s": None, "ttft_s": None, "ttft_basis": "absent",
                    "input_tokens": None, "output_tokens": None, "tool_events": [],
                    "iterations_used": None,
                    "snapshot_id": self.snapshot_id if self.snapshot_id is not None
                    else p["case"].get("snapshot_id"),
                    "captured_at_utc": iso_utc(), "source": SOURCE_TAG,
                    "status": ST_BLOCKED, "reason": "组级 BLOCKED：" + "；".join(gst["reasons"]),
                    "order": p["order"],
                }
                kwargs = {}
            elif short_circuited.get(gname):
                row = None  # 短路后不再发起调用
                kwargs = {}
            else:
                row, krec = self.run_one(p)
                kwargs = {"api_key": krec}
            if row is None:
                plan_rows_meta.append({"order": p["order"], "case_id": p["case_id"],
                                       "group": gname, "repeat": p["repeat"],
                                       "status": ST_SKIPPED,
                                       "reason": "组 %s 连续 %d 次建 key 失败 ⇒ 短路，未发起调用"
                                                 % (gname, self.max_key_failures)})
                continue
            if row.get("status") == ST_BLOCKED and kwargs.get("api_key") is not None \
                    and kwargs["api_key"].get("create_http") not in (200, 201):
                consecutive_key_failures[gname] = consecutive_key_failures.get(gname, 0) + 1
                if consecutive_key_failures[gname] >= self.max_key_failures:
                    short_circuited[gname] = True
            rows.append(row)
            status = row["status"]
            if status == ST_OK:
                n_ok += 1
            elif status == ST_BLOCKED:
                n_blocked += 1
            else:
                n_failed += 1
            plan_rows_meta.append({"order": p["order"], "case_id": p["case_id"],
                                   "group": gname, "repeat": p["repeat"],
                                   "status": status, "reason": row.get("reason")})
            if self.pace_s:
                time.sleep(self.pace_s)

        for p in skipped:
            plan_rows_meta.append({"order": p["order"], "case_id": p["case_id"],
                                   "group": p["group"], "repeat": p["repeat"],
                                   "status": ST_SKIPPED,
                                   "reason": "--limit %d：本行不在本次执行矩阵内" % self.limit})

        counts = {"ok": n_ok, "failed": n_failed, "blocked": n_blocked,
                  "skipped": len(skipped), "attempted": len(rows),
                  "planned": len(plan) + len(skipped)}
        rows_sorted = sorted(rows, key=lambda r: r["order"])
        # group_state / key_log / capture_sources 是 build_manifest 的输入面：
        # 必须随 run_out 一起回传（否则 build_manifest 会 KeyError —— 自检 K 段钉住这一点）。
        return {"rows": rows_sorted, "rows_meta": plan_rows_meta, "counts": counts,
                "group_state": self.group_state, "key_log": self.key_log,
                "capture_sources": self.capture_sources}


# --------------------------------------------------------------------------
# manifest / report
# --------------------------------------------------------------------------


def build_manifest(binding, declared, selected, cases, plan, skipped, run_out, args,
                   env_source, base_url, answers_sha256_12=None) -> dict:
    groups_manifest = {}
    for g in selected:
        st = run_out["group_state"][g]
        groups_manifest[g] = {
            "uuid": st["uuid"],
            "carrier": st["carrier"],
            "provider": st["provider"],
            "provider_kind": st["provider_kind"],
            "app_mode": st.get("app_mode"),
            "app_mode_expected": APP_MODE_EXPECTED,
            "app_detail_http": st.get("app_detail_http"),
            "declared_tools": st["declared_tools"],
            "declared_tools_sha256_12": st["declared_tools_sha256_12"],
            "readback_tools": st.get("readback_tools"),
            "readback_tools_sha256_12": st.get("readback_tools_sha256_12"),
            "toolface_declared_equals_readback": st.get("declared_equals_readback"),
            "model_readback": st.get("model_readback"),
            "sampling_drift_advisory": st.get("sampling_drift_advisory"),
            "group_blocked": bool(st.get("blocked")),
            "group_fail": bool(st.get("fail")),
            "reasons": st.get("reasons"),
            "captured_at_utc": st["captured_at_utc"],
        }

    runs = []
    for g in selected:
        for rep in range(1, int(args.repeat) + 1):
            keys = [k for k in run_out["key_log"] if k["group"] == g and k["repeat"] == rep]
            rs = [r for r in run_out["rows"] if r["group"] == g and r["repeat"] == rep]
            runs.append({
                "group": g,
                "repeat": rep,
                "app_uuid": selected[g]["uuid"],
                "carrier": selected[g]["carrier"],
                "provider": selected[g]["provider"],
                "captured_at_utc": (rs[0]["captured_at_utc"] if rs else iso_utc()),
                "n_ok": sum(1 for r in rs if r["status"] == ST_OK),
                "n_failed": sum(1 for r in rs if r["status"] == ST_FAILED),
                "n_blocked": sum(1 for r in rs if r["status"] == ST_BLOCKED),
                "api_keys": keys,
                "api_key_count": len(keys),
                "api_keys_all_removed": all(k.get("removed_confirmed") for k in keys) if keys else None,
            })

    return {
        "schema": "m5-run-manifest/v1",
        "generated_at_utc": iso_utc(),
        "runner": {
            "script": "shared/deploy/smoke/m5_run.py",
            "sha256_full": DIFY.sha256_file(Path(__file__)),
            "reuses": ["shared/deploy/smoke/e2e_smoke.py",
                       "shared/deploy/dify/common.py",
                       "shared/deploy/dify/binding.yaml"],
        },
        "env_source": env_source,
        "dify_base_url": base_url,
        "binding_file": DIFY.file_facts(BINDING_PATH),
        "declared_groups": sorted(declared),
        "selected_groups": list(selected),
        "case_file": (DIFY.file_facts(Path(args.cases)) if getattr(args, "cases", None) else None),
        "cases": {
            "n": len(cases),
            "ids_sha256_12": sha12_text("|".join(c["id"] for c in cases)),
            "cls_tally": _tally(cases, "cls"),
            "difficulty_tally": _tally(cases, "difficulty"),
        },
        "plan": {
            "repeat": int(args.repeat),
            "limit": int(args.limit or 0),
            "rotation": "repeat → case → group（docs/06 §4 P1 轮转执行）",
            "invocations_planned": len(plan) + len(skipped),
            "invocations_attempted": len(plan),
            "invocations_skipped": len(skipped),
            "timeout_s": int(args.timeout),
            "user": args.user,
            "conversation_id": "每次调用均为空串 ⇒ P4 无跨用例记忆",
        },
        "groups": groups_manifest,
        "runs": runs,
        "counts": run_out["counts"],
        "rows": run_out["rows_meta"],
        "capture_source": run_out["capture_sources"],
        "outputs": {
            "answers_jsonl_sha256_12": answers_sha256_12,
            "answers_jsonl": "answers.jsonl",
            "report_md": "report.md",
            "raw_sse_dir": "sse/",
        },
        "honesty": {
            "real_machine_matrix_run": None,   # 由 run() 的真实分支覆写为 True
            "answers_are_measurements_not_judgements": (
                "answers.jsonl 只承载**采集结果**；一切判据（root_cause_accuracy 等）由 W18 计算"),
            "fields_intentionally_absent": {
                "abstain": "判据维度（docs/06 §1.2 运行时产生）⇒ 由 W18 依 answer_text 判定；"
                           "W19 不预判，避免伪造判据与双真相源",
            },
            "efficiency_telemetry": (
                "行内效率遥测由本运行台采集：elapsed_s / ttft_s / input_tokens / output_tokens / "
                "iterations_used（= SSE ``agent_thought.position`` 最大值；无该事件则 null，不补 0）"
                "与 tool_events[].{name,input,obs_len}（input = tool_input 原文、obs_len = observation "
                "UTF-8 字节数，供 W18 按 (name,input) 去重并求 obs_total_bytes）。"
                "query_latency_p50/p95 与 facts_consumed 属**容器侧**指标，本运行台不产 ⇒ W18 表6 "
                "按「宁缺勿造」显示 BLOCKED，本运行台**不**用估算值填充"),
            "model_name_in_rows": (
                "行内 model_from_response 取自**响应元数据**，实测可能为 None；"
                "App 配置里的模型名只出现在 manifest.groups[*].model_readback，**不得**当作响应证据"),
            "blocked_vs_failed": "BLOCKED(2)=没测到；FAIL(1)=测到了、是坏的；两者不得互写",
            "temp_key_lifecycle": (
                "临时 api-key 建/删口径承 e2e_smoke.py（201 建 / DELETE + relist 回读确认）；"
                "本轮是否在真机重复验证见报告"),
            "not_verified": [],
        },
    }


def _tally(cases, key) -> dict:
    out: dict[str, int] = {}
    for c in cases:
        out[str(c.get(key))] = out.get(str(c.get(key)), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def render_report(manifest, rows, verdict, extra=None) -> str:
    counts = manifest.get("counts") or {}
    L: list[str] = []
    L.append("# M5 运行台 · 采集报告")
    L.append("")
    L.append("- 生成时刻（UTC）：`%s`" % manifest["generated_at_utc"])
    L.append("- 结论三态：**%s**（退出码 %d）" % (verdict, EXIT[verdict]))
    L.append("- 计划调用 **%s** 次；实际发起 **%s** 次；跳过 **%s** 次"
             % (manifest["plan"]["invocations_planned"], manifest["plan"]["invocations_attempted"],
                manifest["plan"]["invocations_skipped"]))
    L.append("- 轮转：%s" % manifest["plan"]["rotation"])
    L.append("- repeat=%s；timeout=%ss；user=`%s`"
             % (manifest["plan"]["repeat"], manifest["plan"]["timeout_s"], manifest["plan"]["user"]))
    L.append("- 答案文件：`answers.jsonl`（sha256[:12]=`%s`）"
             % manifest["outputs"].get("answers_jsonl_sha256_12"))
    L.append("")
    L.append("## 1. 计数（成功 / 失败 / 阻塞 / 跳过）")
    L.append("")
    L.append("| 组 | ok | failed | blocked | 计划 | App 模式 | 工具面 == 声明 |")
    L.append("|---|---|---|---|---|---|---|")
    for g, gs in manifest["groups"].items():
        n_ok = n_f = n_b = 0
        for r in rows:
            if r["group"] != g:
                continue
            n_ok += 1 if r["status"] == ST_OK else 0
            n_f += 1 if r["status"] == ST_FAILED else 0
            n_b += 1 if r["status"] == ST_BLOCKED else 0
        own = sum(1 for m in manifest["rows"] if m["group"] == g)
        L.append("| `%s` | %d | %d | %d | %d | %s | %s |"
                 % (g, n_ok, n_f, n_b, own, gs.get("app_mode"),
                    gs.get("toolface_declared_equals_readback")))
    L.append("| **合计** | **%d** | **%d** | **%d** | **%d** | | |"
             % (counts.get("ok", 0), counts.get("failed", 0), counts.get("blocked", 0),
                counts.get("planned", 0)))
    L.append("")
    L.append("## 2. 模型与采样参数（真机回读值）")
    L.append("")
    L.append("| 组 | App uuid | provider | model.name | temperature | max_iteration | 冻结偏差(advisory) |")
    L.append("|---|---|---|---|---|---|---|")
    for g, gs in manifest["groups"].items():
        rb = gs.get("model_readback") or {}
        model = rb.get("model") or {}
        drift = gs.get("sampling_drift_advisory") or []
        L.append("| `%s` | `%s` | `%s` | %s | %s | %s | %s |"
                 % (g, gs.get("uuid"), gs.get("provider"), model.get("name"),
                    rb.get("temperature"), rb.get("max_iteration"),
                    "；".join(drift) if drift else "无"))
    L.append("")
    L.append("## 3. 临时 api-key 生命周期")
    L.append("")
    L.append("| 组 | repeat | 建/删次数 | 全部回读确认已删 | 建失败次数 |")
    L.append("|---|---|---|---|---|")
    for r in manifest["runs"]:
        keys = r.get("api_keys") or []
        n_create_fail = sum(1 for k in keys if k.get("create_http") not in (200, 201))
        L.append("| `%s` | %s | %s | %s | %s |"
                 % (r["group"], r["repeat"], r["api_key_count"],
                    r.get("api_keys_all_removed"), n_create_fail))
    L.append("")
    L.append("## 4. 采集源指纹（载体 /health）")
    L.append("")
    if not manifest.get("capture_source"):
        L.append("（未采集：本次未读载体 /health，或未启用）")
    else:
        L.append("| carrier | URL | ok | status | simulated 断言 |")
        L.append("|---|---|---|---|---|")
        for c in manifest["capture_source"]:
            L.append("| `%s` | `%s` | %s | %s | %s |"
                     % (c.get("carrier"), c.get("url"), c.get("ok"), c.get("status"),
                        c.get("simulated_assert")))
    L.append("")
    L.append("## 5. 失败 / 阻塞明细（最多 40 条）")
    L.append("")
    bad = [r for r in manifest["rows"] if r["status"] in (ST_FAILED, ST_BLOCKED)]
    if not bad:
        L.append("（无）")
    else:
        L.append("| # | case_id | group | repeat | status | 原因 |")
        L.append("|---|---|---|---|---|---|")
        for m in bad[:40]:
            L.append("| %s | `%s` | `%s` | %s | %s | %s |"
                     % (m["order"], m["case_id"], m["group"], m["repeat"], m["status"],
                        clip(m.get("reason"), 200)))
        if len(bad) > 40:
            L.append("")
            L.append("（另有 %d 条，见 `run_manifest.json#rows`）" % (len(bad) - 40))
    L.append("")
    L.append("## 6. 诚实边界")
    L.append("")
    L.append("- 本报告只声明**采集**结果；准确率 / 路径 F1 / false_claim 等判据**一律不在本文件**，")
    L.append("  由 W18 判据器消费 `answers.jsonl` 得出。")
    L.append("- 行内 `model_from_response` 取自响应元数据，实测**可能为 None**；")
    L.append("  App 配置里的模型名（见 manifest.groups）**不是**响应证据，两者不得混用冒充。")
    L.append("- `BLOCKED` 表明**没测到**（缺凭据 / App 非 agent-chat / 配置面为空 / 建 key 失败），")
    L.append("  **不得**写成 `FAIL`；`FAIL` 表明测到了、是坏的。")
    L.append("- `abstain` 由 W18 判定，本运行台**有意不写**该键（见 `run_manifest.json#honesty`）。")
    L.append("- 采样参数偏差只作 advisory 记录，**不改变退出码**（判据归门禁 / 队长）。")
    L.append("- `/health` 的 `auditChain.length` 是内存环形缓冲长度，与磁盘 JSONL 行数不等，")
    L.append("  只登记、不作判据。")
    if extra:
        L.append("")
        L.extend(extra)
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------
# 答案文件 fail-closed 复核
# --------------------------------------------------------------------------


def validate_answers(path: Path, declared) -> dict:
    """复核 `answers.jsonl`：必需键 / 组集合 / 重复键 / 类型。返回 ``{errors, stats}``。

    判据来源 = 队长契约（``tmp/_m4_worklist.md`` §Z.4）+ `docs/06` §1.2。
    """
    errors: list[str] = []
    stats = {"lines": 0, "rows": 0, "groups": {}, "cases": set(), "repeats": set(),
             "status": {}, "empty_answer": 0, "source": {}}
    if not path.is_file():
        return {"errors": ["答案文件不存在：%s" % path], "stats": stats}
    seen = {}
    for lineno, obj in read_jsonl(path, errors):
        stats["lines"] += 1
        if not isinstance(obj, dict):
            errors.append("第 %d 行不是 JSON 对象" % lineno)
            continue
        missing = [k for k in REQUIRED_ROW_KEYS if k not in obj]
        if missing:
            errors.append("第 %d 行缺必需键 %s" % (lineno, missing))
            continue
        if not isinstance(obj["case_id"], str) or not obj["case_id"]:
            errors.append("第 %d 行 case_id 非非空字符串" % lineno)
        if not isinstance(obj["group"], str) or not obj["group"]:
            errors.append("第 %d 行 group 非非空字符串" % lineno)
        elif obj["group"] not in declared:
            errors.append("第 %d 行 group=%r 不在 binding 声明集合 %s 内（fail-closed）"
                          % (lineno, obj["group"], sorted(declared)))
        rep = obj["repeat"]
        if not isinstance(rep, int) or isinstance(rep, bool) or rep < 1:
            errors.append("第 %d 行 repeat=%r 不是 ≥1 的整数" % (lineno, rep))
        if not isinstance(obj["question"], str) or not obj["question"].strip():
            errors.append("第 %d 行 question 为空/非字符串" % lineno)
        if not isinstance(obj["answer_text"], str):
            errors.append("第 %d 行 answer_text 非字符串" % lineno)
        elif not obj["answer_text"]:
            stats["empty_answer"] += 1
        key = (obj["case_id"], obj["group"], rep)
        if key in seen:
            errors.append("第 %d 行与第 %d 行重复键 (case_id,group,repeat)=%s"
                          % (lineno, seen[key], (key,)))
        seen[key] = lineno
        stats["rows"] += 1
        stats["groups"][obj["group"]] = stats["groups"].get(obj["group"], 0) + 1
        stats["cases"].add(obj["case_id"])
        stats["repeats"].add(rep)
        stats["status"][obj.get("status")] = stats["status"].get(obj.get("status"), 0) + 1
        stats["source"][obj.get("source")] = stats["source"].get(obj.get("source"), 0) + 1
    stats["cases"] = sorted(stats["cases"])
    stats["repeats"] = sorted(stats["repeats"])
    stats["n_cases"] = len(stats["cases"])
    return {"errors": errors, "stats": stats}


# --------------------------------------------------------------------------
# 离线自检（假 transport，绝不联网）
# --------------------------------------------------------------------------


class FakeConsoleTransport:
    """假 Console 传输：只需 ``call(method, path, body=None, timeout=60)``。"""

    def __init__(self, apps=None, create_st=201, delete_st=204, app_detail_st=200,
                 fail_create_for=(), fail_delete_for=(), unauthorized_times=0):
        self.calls: list[tuple] = []
        self.cookies = DIFY.CookieJar()
        self.apps = apps if apps is not None else {}
        self.create_st = create_st
        self.delete_st = delete_st
        self.app_detail_st = app_detail_st
        self.fail_create_for = set(fail_create_for)
        self.fail_delete_for = set(fail_delete_for)
        self.keys: dict[str, dict] = {}
        self._n = 0
        # ★ B1（R-54）：前 N 次 POST api-keys 返回 401，用于覆盖「会话过期 ⇒ 重登录 ⇒ 重试」。
        self.unauthorized_times = int(unauthorized_times)
        self.unauthorized_left = int(unauthorized_times)
        self.login_count = 0

    def call(self, method, path, body=None, timeout=60):
        self.calls.append((method, path, body))
        if path.endswith("/login"):
            self.login_count += 1
            return 200, {"result": "success"}
        m = re.match(r"^/console/api/apps/([^/]+)/api-keys/([^/]+)$", path)
        if m and method == "DELETE":
            uuid, kid = m.group(1), m.group(2)
            if uuid in self.fail_delete_for:
                return self.delete_st or 500, {"message": "delete refused"}
            self.keys.pop((uuid, kid), None)
            return self.delete_st, None
        m = re.match(r"^/console/api/apps/([^/]+)/api-keys$", path)
        if m:
            uuid = m.group(1)
            if method == "POST":
                if self.unauthorized_left > 0:
                    self.unauthorized_left -= 1
                    return 401, {"code": "unauthorized",
                                 "message": "Invalid Authorization token."}
                if uuid in self.fail_create_for:
                    return 403, {"message": "no permission"}
                self._n += 1
                kid, tok = "key-%d" % self._n, "sk-fake-%d" % self._n
                self.keys[(uuid, kid)] = tok
                return self.create_st, {"id": kid, "token": tok}
            rows = [{"id": k} for (u, k) in self.keys if u == uuid]
            return 200, {"data": rows}
        m = re.match(r"^/console/api/apps/([^/]+)$", path)
        if m:
            uuid = m.group(1)
            if uuid not in self.apps:
                return 404, {"message": "not found"}
            return self.app_detail_st, self.apps[uuid]
        return 404, {"message": "unknown route"}


class FakeChat:
    """假 /v1/chat-messages：``plan`` 每项 = SSE 文本 | (http_status, text) | Exception。"""

    def __init__(self, plan):
        self.plan = list(plan)
        self.i = 0
        self.calls: list[dict] = []

    def __call__(self, base, token, query, user, timeout, on_line):
        self.calls.append({"base": base, "token": token, "query": query, "user": user,
                           "timeout": timeout})
        item = self.plan[min(self.i, len(self.plan) - 1)]
        self.i += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, tuple):
            status, text = item
        else:
            status, text = 200, item
        for k, line in enumerate(text.splitlines(keepends=True)):
            on_line(line, round(0.01 * (k + 1), 4))
        return status, text


def sse(*events) -> str:
    return "".join("data: %s\n\n" % json.dumps(e, ensure_ascii=False) for e in events)


def sse_ok(tool="cc_query", obs='{"rows":[1]}', answer="答案是：慢查询", tokens=(100, 20)) -> str:
    return sse(
        {"event": "agent_thought", "tool": tool, "tool_input": '{"q":1}',
         "observation": obs, "thought": "调用工具", "position": 1},
        {"event": "agent_message", "answer": answer},
        {"event": "message_end", "answer": answer,
         "metadata": {"usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}},
    )


def fake_app(mode="agent-chat", tools=("cc_guide", "cc_query"), enabled=True,
             temperature=0.3, max_iteration=40) -> dict:
    return {
        "id": "u", "name": "fake", "mode": mode,
        "model_config": {} if mode != "agent-chat" else {
            "model": {"name": "deepseek_v4", "provider": "p", "mode": "chat",
                      "completion_params": {"temperature": temperature, "stop": []}},
            "agent_mode": {"enabled": True, "strategy": "react", "max_iteration": max_iteration,
                           "prompt": None,
                           "tools": [{"tool_name": t, "enabled": enabled} for t in tools]},
            "pre_prompt": "system",
        },
    }


def make_group_state(console, group, uuid, declared_tools):
    """用假 binding / 假 console 走一遍 prepare_group，供自检复用。"""
    groups = {group: {"group": group, "uuid": uuid, "carrier": "kg", "provider": "p",
                      "provider_kind": "api", "declared_tools": sorted(declared_tools),
                      "declared_tools_sha256_12": sha12_text("|".join(sorted(declared_tools)))}}
    rn = Runner({}, {}, groups, Path("."), console, FakeChat([sse_ok()]), 1, 0, 30, "t",
                read_health_flag=False)
    return rn, rn.prepare_group(group), groups


def run_selftest() -> int:
    checks: list[tuple[str, bool, str]] = []

    def add(name, fn):
        """登记**并立即求值**。

        惰性求值会让闭包读到之后被重新赋值的同名局部变量（例如 ``v`` / ``plan``），
        前面的正控就会读到后面用例的状态而假红/假绿 —— 自检必须与阅读顺序一致。
        """
        try:
            ok, detail = fn()
        except BaseException as exc:  # noqa: BLE001
            ok, detail = False, "EXC %s: %s" % (type(exc).__name__, exc)
        checks.append((name, bool(ok), str(detail)))

    def _ok(cond, detail=""):
        return bool(cond), detail

    # ---------------- A. SSE 解析 ----------------
    answer = "信用卡支付交易从 14:20 开始变慢，根因是 FM-DB-SLOW-QUERY。"
    p = E2E.parse_sse(sse(
        {"event": "agent_thought", "tool": "cc_cause", "tool_input": "{}",
         "observation": "{}", "thought": "t", "position": 1},
        {"event": "agent_message", "answer": answer[:10]},
        {"event": "agent_message", "answer": answer[10:]},
        {"event": "message_end", "answer": answer,
         "metadata": {"usage": {"prompt_tokens": 123, "completion_tokens": 45}}},
    ))
    add("SSE: agent_message 逐字流拼接（answer 完整）", lambda: _ok(
        p["answer"] == answer, "answer_len=%d" % len(p["answer"])))
    add("SSE: 非 data: 行 / [DONE] / 非法 JSON 全部忽略", lambda: _ok(
        E2E.parse_sse("event: ping\n\ndata: [DONE]\n\ndata: {bad json\n\ndata: 3\n\n")["n_events"] == 0,
        "n_events=%d" % E2E.parse_sse("event: ping\n\ndata: [DONE]\n\ndata: {bad\n\ndata: 3\n\n")["n_events"]))
    add("SSE: error 事件被捕获", lambda: _ok(
        "maximum iteration" in (E2E.parse_sse(sse({"event": "error", "message": "Agent exceeded the maximum iteration limit of 20."}))["error"] or ""),
        "err=%s" % clip(E2E.parse_sse(sse({"event": "error", "message": "x"}))["error"], 40)))

    # ---------------- B. 工具事件抽取 ----------------
    fields = row_fields_from_parsed(p)
    add("工具事件: tool+observation ⇒ name/status=ok", lambda: _ok(
        fields["tool_events"] and fields["tool_events"][0]["name"] == "cc_cause"
        and fields["tool_events"][0]["status"] == "ok", str(fields["tool_events"])))
    gated = E2E.parse_sse(sse(
        {"event": "agent_thought", "tool": "cc_query", "tool_input": "{}", "observation": ""},
        {"event": "message_end", "answer": "x", "metadata": {"usage": {}}}))
    gf = row_fields_from_parsed(gated)
    add("工具事件: 有 tool 但 observation 空 ⇒ no_observation 且不计入 executed", lambda: _ok(
        gf["tool_events"] and gf["tool_events"][0]["status"] == "no_observation"
        and gf["executed_tool_names"] == [], str(gf["tool_events"])))
    add("工具事件: 无工具 ⇒ 空列表", lambda: _ok(
        row_fields_from_parsed(E2E.parse_sse(sse({"event": "message_end", "answer": "x"})))["tool_events"] == [],
        "[]"))
    add("效率遥测: 每条带 input 原文与 obs_len（UTF-8 字节）", lambda: _ok(
        fields["tool_events"][0].get("input") == "{}"
        and fields["tool_events"][0].get("obs_len") == len("{}".encode("utf-8")),
        str(fields["tool_events"][0])))
    mfixture = E2E.parse_sse(sse(
        {"event": "agent_thought", "tool": "cc_query", "tool_input": "{}",
         "observation": "中", "thought": "调用工具", "position": 3},
        {"event": "agent_thought", "thought": "无工具的一轮", "position": 4},
        {"event": "message_end", "answer": "x", "metadata": {"usage": {}}}))
    mfields = row_fields_from_parsed(mfixture)
    add("效率遥测: iterations_used = max(position)（含无工具的轮次）", lambda: _ok(
        mfields["iterations_used"] == 4, "iterations_used=%s" % mfields["iterations_used"]))
    add("效率遥测: obs_len 按 UTF-8 字节（中文 1 字 = 3 字节）", lambda: _ok(
        mfields["tool_events"] and mfields["tool_events"][0]["obs_len"] == 3,
        str(mfields["tool_events"])))
    add("效率遥测: 无 position 事件 ⇒ iterations_used=None（不补 0）", lambda: _ok(
        row_fields_from_parsed(E2E.parse_sse(sse(
            {"event": "agent_thought", "tool": "x", "observation": "y"},
            {"event": "message_end", "answer": "x"})))["iterations_used"] is None, "None"))

    # ---------------- C. token / 耗时 / 模型 ----------------
    add("token: usage.prompt/completion ⇒ input/output_tokens", lambda: _ok(
        fields["input_tokens"] == 123 and fields["output_tokens"] == 45,
        "in=%s out=%s" % (fields["input_tokens"], fields["output_tokens"])))
    add("token: usage 缺失 ⇒ None（不补 0）", lambda: _ok(
        row_fields_from_parsed(E2E.parse_sse(sse({"event": "message_end", "answer": "x"}))
                               )["input_tokens"] is None, "None"))
    add("模型: 响应元数据无 model ⇒ model_from_response 为 None（不拿 App 配置冒充）", lambda: _ok(
        fields["model_from_response"] is None
        and "model_readback" not in fields, "None"))

    t = capture_chat(FakeChat([sse(
        {"event": "agent_thought", "tool": "x", "observation": "y"},
        {"event": "agent_message", "answer": "ab"},
        {"event": "message_end", "answer": "ab"})]), "http://x", "tok", "q")
    add("耗时: ttft_s = 首个 answer delta 时刻（basis=first_answer_delta）", lambda: _ok(
        abs((t["ttft_s"] or 0) - 0.03) < 1e-9 and t["ttft_basis"] == "first_answer_delta",
        "ttft=%s basis=%s" % (t["ttft_s"], t["ttft_basis"])))
    t2 = capture_chat(FakeChat([sse({"event": "agent_thought", "tool": "x", "observation": "y"},
                                    {"event": "message_end", "answer": "ab"})]),
                      "http://x", "tok", "q")
    add("耗时: 无 answer delta 但 message_end 有 ⇒ basis=message_end", lambda: _ok(
        t2["ttft_basis"] == "message_end" and abs((t2["ttft_s"] or 0) - 0.03) < 1e-9,
        "ttft=%s basis=%s" % (t2["ttft_s"], t2["ttft_basis"])))
    t3 = capture_chat(FakeChat([sse({"event": "agent_thought", "tool": "x", "observation": "y"})]),
                      "http://x", "tok", "q")
    add("耗时: 整轮无 answer ⇒ ttft_s=None basis=absent（如实记缺）", lambda: _ok(
        t3["ttft_s"] is None and t3["ttft_basis"] == "absent",
        "ttft=%s basis=%s" % (t3["ttft_s"], t3["ttft_basis"])))
    add("耗时: elapsed_s 非负浮点；raw_bytes>0", lambda: _ok(
        isinstance(t["elapsed_s"], float) and t["elapsed_s"] >= 0 and t["raw_bytes"] > 0,
        "elapsed=%s bytes=%s" % (t["elapsed_s"], t["raw_bytes"])))

    # ---------------- D. 组集合一致性 / 声明面 ----------------
    real_binding = load_binding(BINDING_PATH)
    real_declared = declared_groups(real_binding)
    add("声明面: 真 binding.yaml 能推导出非空组集合（无硬编码）", lambda: _ok(
        len(real_declared) >= 1 and all(g["uuid"] for g in real_declared.values()),
        "%d 组: %s" % (len(real_declared), list(real_declared))))
    add("声明面: 每个声明组都有 provider 且 provider 已登记", lambda: _ok(
        all(g["provider"] in provider_decls(real_binding) for g in real_declared.values()),
        ",".join(sorted({g["provider"] for g in real_declared.values()}))))
    add("组集合: --groups 未声明组 ⇒ M5Blocked（fail-closed）", lambda: _ok(
        _raises(M5Blocked, lambda: resolve_groups(real_declared, ["cc-not-declared"])),
        "raised"))
    add("组集合: --groups 缺省 = 全部声明组（保持声明顺序）", lambda: _ok(
        resolve_groups(real_declared, None) == list(real_declared), str(list(real_declared))))
    add("组集合: binding.apps 为空 ⇒ M5Blocked", lambda: _ok(
        _raises(M5Blocked, lambda: declared_groups({"apps": {}})), "raised"))
    add("组集合: app 无 uuid ⇒ M5Blocked", lambda: _ok(
        _raises(M5Blocked, lambda: declared_groups(
            {"apps": {"g": {"provider": "p", "tools": ["t"]}}, "providers": [{"provider": "p", "kind": "api"}]})),
        "raised"))
    add("组集合: app.provider 未登记 ⇒ M5Blocked", lambda: _ok(
        _raises(M5Blocked, lambda: declared_groups(
            {"apps": {"g": {"uuid": "u", "provider": "ghost", "tools": ["t"]}}, "providers": []})),
        "raised"))

    # ---------------- E. 端口不变量 ----------------
    recs = check_port_invariants(real_binding, real_declared)
    add("端口不变量: 真 binding 全部通过", lambda: _ok(
        all(r["ok"] for r in recs),
        "；".join("%s(%s)" % (r["check"], r["detail"]) for r in recs if not r["ok"]) or "0 失败"))
    bad_binding = json.loads(json.dumps(real_binding))
    bad_binding["providers"][0]["port"] = 9999
    add("端口不变量: provider port 改成白名单外 ⇒ 命中", lambda: _ok(
        any(not r["ok"] for r in check_port_invariants(bad_binding, real_declared)),
        "命中"))
    bad2 = json.loads(json.dumps(real_binding))
    bad2["whitelist"]["allowed_hosts"] = ["127.0.0.1"]
    add("端口不变量: allowed_hosts 不含真实主机 ⇒ 命中", lambda: _ok(
        any(not r["ok"] for r in check_port_invariants(bad2, real_declared)), "命中"))

    # ---------------- F. 用例契约 ----------------
    good_cases = [{"id": "A01", "cls": "A", "chain": "F1", "scope": ["App"], "type": "单层",
                   "query": "为什么慢？", "injected": ["FM-X"], "expect_root_cause": "FM-X",
                   "expect_layer": "App", "expect_path": ["FM-X"], "expect_objects": ["inst:x"],
                   "pos": ["a"], "neg": ["b"], "nor": [], "evidence_facts": ["ccf-1"],
                   "detectors": ["慢"], "difficulty": "easy", "discriminator": None}]
    v = validate_cases(good_cases)
    add("用例: §1.2 全字段正控 ⇒ 无 hard/strict 错", lambda: _ok(
        not v["hard"] and not v["strict"], str(v["hard"] + v["strict"])))
    v = validate_cases([dict(good_cases[0]), dict(good_cases[0])])
    add("用例: id 重复 ⇒ hard 错（fail-closed）", lambda: _ok(
        any("id 重复" in e for e in v["hard"]), str(v["hard"])))
    v = validate_cases([{"id": "A01", "cls": "A", "query": "q"}])
    add("用例: 非 strict 下缺 §1.2 字段 ⇒ 只在 strict 档", lambda: _ok(
        not v["hard"] and any("缺 §1.2 字段" in e for e in v["strict"]),
        "hard=%d strict=%d" % (len(v["hard"]), len(v["strict"]))))
    v = validate_cases([{"id": "A01", "cls": "X", "query": "q"}])
    add("用例: cls 越界 ⇒ strict 错", lambda: _ok(
        any("cls=" in e for e in v["strict"]), str(v["strict"])))
    v = validate_cases([{"id": "A01", "query": ""}])
    add("用例: query 空 ⇒ hard 错", lambda: _ok(
        any("缺 query" in e for e in v["hard"]), str(v["hard"])))
    v = validate_cases([{"id": "A01", "query": "q", "difficulty": "extreme"}])
    add("用例: difficulty 越界 ⇒ strict 错", lambda: _ok(
        any("difficulty" in e for e in v["strict"]), str(v["strict"])))

    # ---------------- G. 矩阵 / 轮转 ----------------
    cases3 = [{"id": "A01", "query": "q1"}, {"id": "A02", "query": "q2"}]
    plan, skipped = plan_matrix(cases3, ["g1", "g2"], 2)
    add("矩阵: 轮转顺序 = repeat → case → group（P1）", lambda: _ok(
        [plan_key(r) for r in plan] ==
        [("A01", "g1", 1), ("A01", "g2", 1), ("A02", "g1", 1), ("A02", "g2", 1),
         ("A01", "g1", 2), ("A01", "g2", 2), ("A02", "g1", 2), ("A02", "g2", 2)],
        str([plan_key(r) for r in plan])))
    add("矩阵: 规模 = cases × groups × repeat", lambda: _ok(
        len(plan) == 2 * 2 * 2 and not skipped, "%d" % len(plan)))
    p2, s2 = plan_matrix(cases3, ["g1", "g2"], 2, limit=3)
    add("矩阵: --limit 截断为 skip 且键唯一", lambda: _ok(
        len(p2) == 3 and len(s2) == 5 and len({plan_key(r) for r in p2}) == 3,
        "plan=%d skipped=%d" % (len(p2), len(s2))))
    add("矩阵: 重复键 ⇐ 被拒绝（assert_unique_plan）", lambda: _ok(
        _raises(M5ContractError, lambda: assert_unique_plan(p2 + p2)), "raised"))

    # ---------------- H. 运行循环（假 transport） ----------------
    tr = FakeConsoleTransport(apps={
        "u1": fake_app(tools=("cc_guide", "cc_query")),
        "u2": fake_app(tools=("cc_guide", "cc_query")),
        "u3": fake_app(mode="chat"),
        "u4": fake_app(tools=("cc_guide",)),
    })
    console = Console("http://fake", transport=tr)
    st_login = console.login("a@b", "pw")
    selected = {
        "g-ok": {"group": "g-ok", "uuid": "u1", "carrier": "kg", "provider": "p",
                 "provider_kind": "api", "declared_tools": ["cc_guide", "cc_query"],
                 "declared_tools_sha256_12": "x"},
        "g-mode": {"group": "g-mode", "uuid": "u3", "carrier": "kg", "provider": "p",
                   "provider_kind": "api", "declared_tools": ["cc_guide", "cc_query"],
                   "declared_tools_sha256_12": "x"},
        "g-drift": {"group": "g-drift", "uuid": "u4", "carrier": "kg", "provider": "p",
                    "provider_kind": "api", "declared_tools": ["cc_guide", "cc_query"],
                    "declared_tools_sha256_12": "x"},
        "g-missing": {"group": "g-missing", "uuid": "u-missing", "carrier": "kg", "provider": "p",
                      "provider_kind": "api", "declared_tools": ["cc_guide"],
                      "declared_tools_sha256_12": "x"},
    }
    add("Console: 假 transport 登录 200 且写入 cookie", lambda: _ok(
        st_login == 200 and tr.cookies.get("access_token") is not None, "st=%s" % st_login))
    add("Console: app_detail HTTP 404 ⇒ bound_tools 报错而非抛异常", lambda: _ok(
        console.bound_tools("u-missing")[1] is not None, "err=%s" % console.bound_tools("u-missing")[1]))
    add("Console: 201 建 key 返回 id/token；DELETE 后回读已消失", lambda: _ok(
        (lambda kid, tok, st, err: (kid is not None and tok and
                                    console.delete_key("u1", kid)[0] == 204 and
                                    console.list_keys("u1")[1] == []))(*console.create_key("u1")),
        "ok"))

    # ★ B1（R-54）：Console 会话过期 ⇒ create_key 遇 401 **重登录一次并重试**
    tr_auth = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide",))},
                                   unauthorized_times=1)
    c_auth = Console("http://fake", transport=tr_auth)
    c_auth.login("a@b", "pw")
    _k1, _t1, _s1, _e1 = c_auth.create_key("u1")
    add("B1 正控：首次建 key 401 ⇒ 重登录一次并重试成功（token 非空 / recovered / login 共 2 次）",
        lambda: _ok(_t1 is not None and _s1 in (200, 201)
                    and (c_auth.last_reauth or {}).get("recovered") is True
                    and (c_auth.last_reauth or {}).get("trigger_http") == 401
                    and tr_auth.login_count == 2,
                    "st=%s tok=%s reauth=%s logins=%d"
                    % (_s1, bool(_t1), c_auth.last_reauth, tr_auth.login_count)))

    tr_auth2 = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide",))},
                                    unauthorized_times=99)
    c_auth2 = Console("http://fake", transport=tr_auth2)
    c_auth2.login("a@b", "pw")
    _k2, _t2, _s2, _e2 = c_auth2.create_key("u1")
    _posts2 = sum(1 for (_m, _p, _b) in tr_auth2.calls
                  if _m == "POST" and _p.endswith("/api-keys"))
    add("B1 负控：重登录后仍 401 ⇒ 如实返回 401（不掩盖），且**只重试一次**（POST api-keys 恰 2 次）",
        lambda: _ok(_t2 is None and _s2 == 401 and _posts2 == 2
                    and (c_auth2.last_reauth or {}).get("recovered") is False,
                    "st=%s tok=%s posts=%d reauth=%s"
                    % (_s2, bool(_t2), _posts2, c_auth2.last_reauth)))

    add("B1 负控：未发生 401 时 last_reauth 必须为 None（不得虚报重登录）", lambda: _ok(
        (lambda kid, tok, st, err: (c_auth.last_reauth is None))(*c_auth.create_key("u1")),
        "last_reauth=%s" % c_auth.last_reauth))

    # 正控跑批：3 条用例 × 1 组 × 1 repeat，全 ok
    out = Path(".")
    cases_run = [{"id": "A01", "query": "题面一 带 空格\n与换行"},
                 {"id": "A02", "query": "题面二"},
                 {"id": "A03", "query": "题面三"}]
    plan, skipped = plan_matrix(cases_run, ["g-ok"], 1)
    chat = FakeChat([sse_ok(), sse_ok(), sse_ok()])
    rn = Runner({}, {}, {"g-ok": selected["g-ok"]}, out, console, chat, 1, 0, 30, "u",
                read_health_flag=False)
    res = rn.run(plan, skipped)
    add("跑批: 3 条全 ok；answers 行数 = 3", lambda: _ok(
        res["counts"]["ok"] == 3 and len(res["rows"]) == 3,
        str(res["counts"])))
    add("跑批: 每行必需键齐全（含空 answer 也有键）", lambda: _ok(
        all(all(k in r for k in REQUIRED_ROW_KEYS) for r in res["rows"]),
        "keys ok"))
    add("跑批: 题面逐字（含首尾空白与换行）传给 opener", lambda: _ok(
        chat.calls[0]["query"] == "题面一 带 空格\n与换行",
        repr(chat.calls[0]["query"])))
    add("跑批: 行内 question 与用例 query 字节相等", lambda: _ok(
        res["rows"][0]["question"] == cases_run[0]["query"]
        and res["rows"][0]["query_verbatim_ok"] is True, "ok"))
    add("跑批: source 恒为 dify-sse；answer_text 与 SSE 解析一致", lambda: _ok(
        all(r["source"] == SOURCE_TAG for r in res["rows"])
        and res["rows"][0]["answer_text"] == "答案是：慢查询", res["rows"][0]["answer_text"]))
    add("跑批: 每次调用一副临时 key，且全部回读确认已删", lambda: _ok(
        len(rn.key_log) == 3 and all(k["removed_confirmed"] for k in rn.key_log),
        "keys=%d" % len(rn.key_log)))
    add("跑批: 每次调用后 App 上不留 key（回读 list 为空）", lambda: _ok(
        console.list_keys("u1")[1] == [], "empty"))
    add("跑批: 零硬编码组名 —— selected 全部来自参数", lambda: _ok(
        all(r["group"] == "g-ok" for r in res["rows"]), "g-ok"))

    # 失败隔离：3 条里第 2 条抛异常，第 3 条照跑
    tr2 = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide", "cc_query"))})
    c2 = Console("http://fake", transport=tr2)
    c2.login("a@b", "pw")
    chat2 = FakeChat([sse_ok(), RuntimeError("boom"), sse_ok()])
    rn2 = Runner({}, {}, {"g-ok": selected["g-ok"]}, out, c2, chat2, 1, 0, 30, "u",
                 read_health_flag=False)
    res2 = rn2.run(*plan_matrix(cases_run, ["g-ok"], 1))
    add("失败隔离: 第 2 条异常 ⇒ 该条 failed，其余 2 条 ok，批次不中断", lambda: _ok(
        [r["status"] for r in res2["rows"]] == [ST_OK, ST_FAILED, ST_OK],
        str([r["status"] for r in res2["rows"]])))
    add("失败隔离: 异常行也记 reason，且临时 key 仍被删除", lambda: _ok(
        "boom" in (res2["rows"][1]["reason"] or "")
        and all(k["removed_confirmed"] for k in rn2.key_log),
        "reason=%s keys=%d" % (clip(res2["rows"][1]["reason"], 40), len(rn2.key_log))))
    add("失败隔离: 失败计数=1，ok 计数=2", lambda: _ok(
        res2["counts"]["failed"] == 1 and res2["counts"]["ok"] == 2, str(res2["counts"])))

    # 建 key 失败 ⇒ blocked（不是 failed）且不发调用
    tr3 = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide", "cc_query"))},
                              fail_create_for=("u1",))
    c3 = Console("http://fake", transport=tr3)
    c3.login("a@b", "pw")
    chat3 = FakeChat([sse_ok()])
    rn3 = Runner({}, {}, {"g-ok": selected["g-ok"]}, out, c3, chat3, 2, 0, 30, "u",
                 read_health_flag=False)
    res3 = rn3.run(*plan_matrix([{"id": "A01", "query": "q"}], ["g-ok"], 2))
    add("建 key 失败: 状态记 blocked（BLOCKED ≠ FAIL）", lambda: _ok(
        [r["status"] for r in res3["rows"]] == [ST_BLOCKED, ST_BLOCKED], str(res3["counts"])))
    add("建 key 失败: 不发起 chat 调用（0 次）", lambda: _ok(
        len(chat3.calls) == 0, "calls=%d" % len(chat3.calls)))
    add("建 key 失败: 连续 3 次后短路，余下行记 skipped", lambda: _ok(
        res3["counts"]["blocked"] == 2 and res3["counts"]["skipped"] == 0, str(res3["counts"])))

    # 删除失败 ⇒ removed_confirmed=False（如实记录删除结果）
    tr4 = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide", "cc_query"))},
                               fail_delete_for=("u1",), delete_st=500)
    c4 = Console("http://fake", transport=tr4)
    c4.login("a@b", "pw")
    rn4 = Runner({}, {}, {"g-ok": selected["g-ok"]}, out, c4, FakeChat([sse_ok()]), 1, 0, 30, "u",
                 read_health_flag=False)
    res4 = rn4.run(*plan_matrix([{"id": "A01", "query": "q"}], ["g-ok"], 1))
    add("删除失败: 记录 delete_http 与 removed_confirmed=False（不谎报已删）", lambda: _ok(
        rn4.key_log[0]["delete_http"] == 500 and rn4.key_log[0]["removed_confirmed"] is False,
        "delete_http=%s removed=%s" % (rn4.key_log[0]["delete_http"],
                                       rn4.key_log[0]["removed_confirmed"])))

    # 模式非 agent-chat ⇒ 组级 BLOCKED，且不发调用
    tr5 = FakeConsoleTransport(apps={"u3": fake_app(mode="chat")})
    c5 = Console("http://fake", transport=tr5)
    c5.login("a@b", "pw")
    chat5 = FakeChat([sse_ok()])
    rn5 = Runner({}, {}, {"g-mode": selected["g-mode"]}, out, c5, chat5, 1, 0, 30, "u",
                 read_health_flag=False)
    res5 = rn5.run(*plan_matrix([{"id": "A01", "query": "q"}], ["g-mode"], 1))
    add("App 模式: 非 agent-chat ⇒ 组级 blocked（永久 BLOCKED，非 FAIL）", lambda: _ok(
        rn5.group_state["g-mode"]["blocked"] is True
        and res5["rows"][0]["status"] == ST_BLOCKED, "blocked"))
    add("App 模式: 非 agent-chat ⇒ 不发任何调用", lambda: _ok(
        len(chat5.calls) == 0, "calls=%d" % len(chat5.calls)))
    tr5b = FakeConsoleTransport(apps={"u3": fake_app(mode="chat")})
    c5b = Console("http://fake", transport=tr5b)
    rn5b = Runner({}, {}, {"g-mode": selected["g-mode"]}, out, c5b, FakeChat([sse_ok()]),
                  1, 0, 30, "u", read_health_flag=False)
    gs5b = rn5b.prepare_group("g-mode")
    add("App 模式: 理由文本含 R-41 依据（可追溯到 deploy_agents.py:18-19）", lambda: _ok(
        any("R-41" in x for x in gs5b["reasons"]), "; ".join(gs5b["reasons"])))

    # 工具面漂移 ⇒ group_fail（FAIL 而非 BLOCKED）
    tr6 = FakeConsoleTransport(apps={"u4": fake_app(tools=("cc_guide",))})
    c6 = Console("http://fake", transport=tr6)
    _rn6, gs6, _ = make_group_state(c6, "g-drift", "u4", ["cc_guide", "cc_query"])
    add("工具面: 真机少一个工具 ⇒ fail（测到了、不一致）且理由给出差集", lambda: _ok(
        gs6["fail"] is True and gs6["blocked"] is False
        and any("cc_query" in x for x in gs6["reasons"]), "; ".join(gs6["reasons"])))

    # 采样漂移 advisory
    _rn7, gs7, _ = make_group_state(c6, "g-drift", "u4", ["cc_guide"])
    tr7 = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide", "cc_query"),
                                                    temperature=0.7)})
    c7 = Console("http://fake", transport=tr7)
    _rn, gs7b, _ = make_group_state(c7, "g-ok", "u1", ["cc_guide", "cc_query"])
    add("采样漂移: temperature=0.7 ⇒ advisory 命中且不置 fail/blocked", lambda: _ok(
        any("temperature" in d for d in gs7b["sampling_drift_advisory"])
        and not gs7b["fail"] and not gs7b["blocked"], str(gs7b["sampling_drift_advisory"])))

    # ---------------- I. 答案文件复核 ----------------
    # 写入 REPO_ROOT/tmp（不随 CWD 漂移；不污染任意工作目录）
    tmp_ans = REPO_ROOT / "tmp" / "m5_run_evidence" / "_selftest_answers_tmp.jsonl"
    try:
        tmp_ans.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        tmp_ans = Path(tempfile.mkdtemp(prefix="m5_selftest_")) / "_selftest_answers_tmp.jsonl"
    decl_set = {"g-ok"}
    rows_ok = [
        {"case_id": "A01", "group": "g-ok", "repeat": 1, "question": "q1", "answer_text": "a1",
         "status": ST_OK, "source": SOURCE_TAG},
        {"case_id": "A02", "group": "g-ok", "repeat": 1, "question": "q2", "answer_text": "",
         "status": ST_FAILED, "source": SOURCE_TAG},
    ]
    write_jsonl_utf8(tmp_ans, rows_ok)
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 正控无错；空 answer 记 empty_answer=1", lambda: _ok(
        not vres["errors"] and vres["stats"]["empty_answer"] == 1,
        str(vres["errors"]) + str(vres["stats"]["empty_answer"])))
    write_jsonl_utf8(tmp_ans, [{"case_id": "A01", "group": "g-ok", "repeat": 1, "question": "q"}])
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 缺 answer_text ⇒ 报错（fail-closed）", lambda: _ok(
        any("缺必需键" in e for e in vres["errors"]), str(vres["errors"])))
    write_jsonl_utf8(tmp_ans, rows_ok + [dict(rows_ok[0])])
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 重复 (case,group,repeat) ⇒ 报错", lambda: _ok(
        any("重复键" in e for e in vres["errors"]), str(vres["errors"])))
    write_jsonl_utf8(tmp_ans, [dict(rows_ok[0], group="g-undeclared")])
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 组不在声明集合 ⇒ 报错（组集合一致性 fail-closed）", lambda: _ok(
        any("不在 binding 声明集合" in e for e in vres["errors"]), str(vres["errors"])))
    write_jsonl_utf8(tmp_ans, [dict(rows_ok[0], repeat=0)])
    add("答案复核: repeat 非 ≥1 整数 ⇒ 报错", lambda: _ok(
        any("repeat" in e for e in validate_answers(tmp_ans, decl_set)["errors"]), "hit"))
    # Windows/PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM：读侧必须容忍（utf-8-sig）
    tmp_ans.write_bytes(b"\xef\xbb\xbf" + "".join(
        json.dumps(r, ensure_ascii=False) + "\n" for r in rows_ok).encode("utf-8"))
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 带 UTF-8 BOM 的 answers.jsonl 可读（utf-8-sig，不误报）", lambda: _ok(
        not vres["errors"] and vres["stats"]["rows"] == 2, str(vres["errors"])[:120]))
    tmp_ans.write_bytes(b"\xef\xbb\xbf" + b'{"case_id":"A01","group":"g-ok"\n')
    vres = validate_answers(tmp_ans, decl_set)
    add("答案复核: 坏行 ⇒ 记 error 而不抛异常（fail-closed、不崩）", lambda: _ok(
        any("不是合法 JSON" in e for e in vres["errors"]), str(vres["errors"])[:120]))
    cases_bom = Path(str(tmp_ans) + ".cases.json")
    cases_bom.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"cases": [{"id": "A01", "query": "q1"}]}, ensure_ascii=False).encode("utf-8"))
    add("用例: 带 UTF-8 BOM 的 JSON 用例文件可解析（PS 5.1 陷阱）", lambda: _ok(
        [c["id"] for c in load_cases(cases_bom)] == ["A01"], "ok"))
    for _p in (tmp_ans, cases_bom):
        if _p.exists():
            _p.unlink()

    # ---------------- J. dry-run 结构性不联网 ----------------
    dry_tr = FakeConsoleTransport(apps={})
    dry_out = _capture(lambda: cmd_dry_run(_ns(cases=str(BINDING_PATH)), real_declared,
                                          resolve_groups(real_declared, ["cc-kg-agent"]),
                                          cases3))
    add("dry-run: 返回 0 且打印矩阵与调用数", lambda: _ok(
        dry_out["code"] == 0 and "不发送任何网络请求" in dry_out["text"]
        and "合计将发起" in dry_out["text"], "code=%s" % dry_out["code"]))
    add("dry-run: 不构造 Console ⇒ 假 transport 调用日志为空（结构性不联网）", lambda: _ok(
        len(dry_tr.calls) == 0, "calls=%d" % len(dry_tr.calls)))

    # ---------------- K. 产物链（answers.jsonl / run_manifest.json / report.md）----
    # 为什么必须有这一段：writer 面（写答案文件 → 自身复核 → manifest → report）在真实运行里
    # 位于所有网络调用**之后**，此前没有任何自检覆盖它 —— 于是 build_manifest 一直在读
    # run_out["group_state"]/["key_log"]/["capture_sources"]，而 Runner.run 从不回传这三个键，
    # 真实运行必定在写完 answers.jsonl 后 KeyError（已修）。以下用假 transport 把整条链跑通，
    # 并把三件套落在 REPO_ROOT/tmp/m5_run_evidence/offline_selftest_artifacts/ 作为离线证据。
    evi_dir = REPO_ROOT / "tmp" / "m5_run_evidence" / "offline_selftest_artifacts"
    try:
        evi_dir.mkdir(parents=True, exist_ok=True)
    except OSError:                       # 只读文件系统等 ⇒ 退回系统临时目录，不影响结论
        evi_dir = Path(tempfile.mkdtemp(prefix="m5_selftest_"))
    art_decl = {
        "g-a": dict(selected["g-ok"], group="g-a", uuid="u1"),
        "g-b": dict(selected["g-ok"], group="g-b", uuid="u2"),
    }
    art_cases = [{"id": "A01", "query": "q1", "cls": "A"},
                 {"id": "B02", "query": "q2", "cls": "B"}]
    art_tr = FakeConsoleTransport(apps={"u1": fake_app(tools=("cc_guide", "cc_query")),
                                        "u2": fake_app(tools=("cc_guide", "cc_query"))})
    art_c = Console("http://fake", transport=art_tr)
    art_c.login("a@b", "pw")
    art_args = _ns(cases=str(evi_dir / "_cases.json"), out=str(evi_dir), repeat=1, limit=0)
    # manifest 会 file_facts(args.cases) ⇒ 用例文件必须真实存在（证据目录里留一份）
    write_text_utf8(evi_dir / "_cases.json",
                    json.dumps({"cases": art_cases}, ensure_ascii=False, indent=2) + "\n")
    # ★ 与 cmd_run 完全同一条接线：plan_matrix 吃组名列表，Runner/build_manifest 吃 selected_map
    art_names = ["g-a", "g-b"]
    art_sel = selected_map(art_decl, art_names)
    rnK = Runner({}, {}, art_sel, evi_dir, art_c, FakeChat([sse_ok()] * 4), 1, 0, 30, "u",
                 read_health_flag=False)
    planK, skippedK = plan_matrix(art_cases, art_names, 1, 0)
    runK = rnK.run(planK, skippedK)
    add("产物: run() 回传 group_state/key_log/capture_sources（manifest 的输入面）", lambda: _ok(
        set(runK) >= {"rows", "rows_meta", "counts", "group_state", "key_log", "capture_sources"},
        str(sorted(runK))))
    ansK = evi_dir / "answers.jsonl"
    write_jsonl_utf8(ansK, runK["rows"])
    rawK = ansK.read_bytes()
    add("产物: answers.jsonl 为 UTF-8/LF、行数 == 计划数、末行有换行", lambda: _ok(
        b"\r\n" not in rawK and rawK.endswith(b"\n")
        and len([x for x in rawK.decode("utf-8").splitlines() if x.strip()]) == len(planK),
        "bytes=%d planned=%d" % (len(rawK), len(planK))))
    add("产物: 每行必需键齐全且 (case,group,repeat) 顺序 == 计划矩阵", lambda: _ok(
        all(all(k in r for k in REQUIRED_ROW_KEYS) for r in runK["rows"])
        and [plan_key(r) for r in runK["rows"]] == [plan_key(r) for r in planK],
        "n=%d" % len(runK["rows"])))
    vK = validate_answers(ansK, set(art_decl))
    add("产物: 运行器自产的 answers.jsonl 通过自身 fail-closed 复核（0 错）", lambda: _ok(
        not vK["errors"], str(vK["errors"][:3])))
    add("产物: --verify-answers 走 CLI 路径对自产文件返回 0", lambda: _ok(
        _capture(lambda: cmd_verify(_ns(verify_answers=str(ansK)), set(art_decl)))["code"] == 0,
        "code"))
    manK = build_manifest(real_binding, art_decl, art_sel, art_cases, planK, skippedK, runK,
                          art_args, "<fake transport>", "http://fake",
                          answers_sha256_12=sha12_bytes(rawK))
    add("产物: manifest schema/groups/runs/counts/honesty 齐全；真机标记未被提前置 True", lambda: _ok(
        manK["schema"] == "m5-run-manifest/v1" and set(manK["groups"]) == {"g-a", "g-b"}
        and len(manK["runs"]) == 2 and manK["counts"]["ok"] == 4
        and manK["honesty"]["real_machine_matrix_run"] is None,
        "runs=%d counts=%s" % (len(manK["runs"]), manK["counts"])))
    add("产物: manifest.runs 记 App uuid / 每组每次运行 / 临时 key 生命周期与已删确认", lambda: _ok(
        all(r["app_uuid"] in ("u1", "u2") and r["provider"] == "p"
            and r["api_key_count"] == 2 and r["api_keys_all_removed"] is True
            for r in manK["runs"]),
        str([(r["group"], r["app_uuid"], r["api_key_count"]) for r in manK["runs"]])))
    add("产物: manifest.groups 带回读工具面指纹、model_readback（模型/采样回读值）", lambda: _ok(
        all(g["readback_tools"] and g["readback_tools_sha256_12"]
            and (g["model_readback"].get("model") or {}).get("name")
            and g["model_readback"].get("temperature") is not None
            and g["toolface_declared_equals_readback"]
            for g in manK["groups"].values()),
        str({g: (v["model_readback"].get("model") or {}).get("name")
             for g, v in manK["groups"].items()})))
    repK = render_report(manK, runK["rows"], PASS)
    add("产物: report.md 含每组计数、答案文件 sha 与诚实边界段", lambda: _ok(
        "诚实边界" in repK and "g-a" in repK and "g-b" in repK
        and str(manK["outputs"]["answers_jsonl_sha256_12"]) in repK, "len=%d" % len(repK)))
    write_text_utf8(evi_dir / "run_manifest.json", DIFY.dump_pretty(manK) + "\n")
    write_text_utf8(evi_dir / "report.md", repK)
    add("产物: 三件套已落盘且非空（离线证据目录）", lambda: _ok(
        all((evi_dir / f).exists() and (evi_dir / f).stat().st_size > 0
            for f in ("answers.jsonl", "run_manifest.json", "report.md")), str(evi_dir)))

    # ---------------- L. 组选择接线（真机自证踩到的 TypeError 回归钉）----------------
    add("接线: resolve_groups 返回**组名列表**（plan_matrix 的入参面）", lambda: _ok(
        resolve_groups(real_declared, ["cc-kgt-agent"]) == ["cc-kgt-agent"]
        and resolve_groups(real_declared, None) == list(real_declared),
        str(resolve_groups(real_declared, None))))
    add("接线: selected_map 把组名列表映射成 {组名: 声明配置}（Runner 的入参面）", lambda: _ok(
        selected_map(art_decl, ["g-b", "g-a"]) == {"g-b": art_decl["g-b"], "g-a": art_decl["g-a"]}
        and selected_map(art_decl, ["g-a"])["g-a"]["uuid"] == "u1", "ok"))
    add("接线: 把列表直接喂给 Runner ⇒ 必抛 TypeError（旧缺陷的可复现形状）", lambda: _ok(
        _raises(TypeError, lambda: Runner({}, {}, art_names, evi_dir, art_c, FakeChat([]),
                                          1, 0, 30, "u", read_health_flag=False)
                .prepare_group("g-a")), "raised"))

    # ---------------- M. 用例集 ↔ 载体在线快照 同源断言（W46 缺陷②，零网络）----------------
    # ★ 全部走**注入的假 health_reader**：不发任何网络请求。真机 /health 不暴露
    #   canonicalSha256（W45 实测：只有快照侧才有），故只在两侧都给得出时比对。
    snap_decl = "ccf-20260929T113250Z-e53d79"

    def _reader(payload):
        """假 /health 读取器。``payload``：dict（health 体）或 ``callable(base) -> 响应``。"""
        calls = []

        def reader(base, timeout=15):
            calls.append(base)
            if callable(payload):
                return payload(base)
            return {"ok": True, "url": base.rstrip("/") + "/health", "health": payload}

        reader.calls = calls
        return reader

    def _bind(cases, meta, sel, reader):
        return _capture(lambda: assert_case_snapshot_binding(cases, meta, sel,
                                                            health_reader=reader))

    m_cases = [{"id": "A01", "snapshot_id": snap_decl}]
    m_meta = {"snapshot_ids": [snap_decl]}
    m_sel = {"g-a": {"group": "g-a", "carrier": "kg", "carrier_mediated": True,
                     "carrier_service_base": "http://carrier-a"}}

    r_same = _reader({"status": "ok", "snapshotId": snap_decl})
    c_same = _bind(m_cases, m_meta, m_sel, r_same)
    add("同源: 用例集声明 == 载体在线快照 ⇒ 放行（None）且恰好读 1 次 /health", lambda: _ok(
        c_same["code"] is None and len(r_same.calls) == 1
        and "OK：用例集声明快照与 1 个载体在线快照同源" in c_same["text"],
        "code=%r calls=%d" % (c_same["code"], len(r_same.calls))))

    r_no = _reader({"status": "ok"})
    c_no = _bind(m_cases, {}, m_sel, r_no)
    add("fail-closed: meta.snapshot_ids 缺失 ⇒ BLOCKED=2 且**零**网络读（不先联网）", lambda: _ok(
        c_no["code"] == EXIT[BLOCKED] and not r_no.calls
        and "meta.snapshot_ids 缺失或不是列表" in c_no["text"],
        "code=%r calls=%d" % (c_no["code"], len(r_no.calls))))

    r_two = _reader({"status": "ok"})
    c_two = _bind(m_cases, {"snapshot_ids": ["a", "b"]}, m_sel, r_two)
    add("fail-closed: meta.snapshot_ids 长度≠1 ⇒ BLOCKED=2 且零网络读", lambda: _ok(
        c_two["code"] == EXIT[BLOCKED] and not r_two.calls and "长度必须恰 1" in c_two["text"],
        "code=%r calls=%d" % (c_two["code"], len(r_two.calls))))

    r_inc = _reader({"status": "ok"})
    c_inc = _bind([{"id": "A01", "snapshot_id": "ccf-other"}], m_meta, m_sel, r_inc)
    add("fail-closed: 用例内 snapshot_id 与 meta 声明不一致 ⇒ BLOCKED=2 且零网络读", lambda: _ok(
        c_inc["code"] == EXIT[BLOCKED] and not r_inc.calls and "不一致" in c_inc["text"],
        "code=%r calls=%d" % (c_inc["code"], len(r_inc.calls))))

    r_down = _reader(lambda base: {"ok": False, "url": base + "/health", "error": "URLError: 离线"})
    c_down = _bind(m_cases, m_meta, m_sel, r_down)
    add("fail-closed: 载体 /health 取不到（离线）⇒ BLOCKED=2（离线 ≠ 测得坏结果，不是 FAIL）",
        lambda: _ok(c_down["code"] == EXIT[BLOCKED] and "⇒ 在线快照未取到" in c_down["text"],
                    "code=%r" % (c_down["code"],)))

    r_nokey = _reader({"status": "ok"})
    c_nokey = _bind(m_cases, m_meta, m_sel, r_nokey)
    add("fail-closed: /health 响应无 snapshotId ⇒ BLOCKED=2", lambda: _ok(
        c_nokey["code"] == EXIT[BLOCKED] and "没有 snapshotId" in c_nokey["text"],
        "code=%r" % (c_nokey["code"],)))

    r_diff = _reader({"status": "ok", "snapshotId": "ccf-20260928T155516Z-937b79"})
    c_diff = _bind(m_cases, m_meta, m_sel, r_diff)
    add("fail-closed: 在线快照 ≠ 用例集声明 ⇒ BLOCKED=2 且双方取值都打印", lambda: _ok(
        c_diff["code"] == EXIT[BLOCKED] and "ccf-20260928T155516Z-937b79" in c_diff["text"]
        and snap_decl in c_diff["text"] and "不同源" in c_diff["text"],
        "code=%r" % (c_diff["code"],)))

    sha_decl = "111733ec" + "0" * 56
    r_cok = _reader({"snapshotId": snap_decl, "canonicalSha256": sha_decl})
    c_cok = _bind(m_cases, dict(m_meta, canonicalSha256=sha_decl), m_sel, r_cok)
    add("canonical: 两侧都给得出且相等 ⇒ 放行（None）", lambda: _ok(
        c_cok["code"] is None, "code=%r" % (c_cok["code"],)))

    r_cbad = _reader({"snapshotId": snap_decl, "canonicalSha256": sha_decl})
    c_cbad = _bind(m_cases, dict(m_meta, canonicalSha256="f" * 64), m_sel, r_cbad)
    add("canonical: 两侧都给得出但不相等 ⇒ BLOCKED=2", lambda: _ok(
        c_cbad["code"] == EXIT[BLOCKED] and "canonicalSha256" in c_cbad["text"],
        "code=%r" % (c_cbad["code"],)))

    r_conly = _reader({"snapshotId": snap_decl})
    c_conly = _bind(m_cases, dict(m_meta, canonicalSha256=sha_decl), m_sel, r_conly)
    add("canonical: 载体侧未暴露该字段（真机如此）⇒ 不据此 BLOCKED，只登记未比对", lambda: _ok(
        c_conly["code"] is None and "canonicalSha256 未比对" in c_conly["text"],
        "code=%r" % (c_conly["code"],)))

    r_skip = _reader({"snapshotId": snap_decl})
    c_skip = _bind(m_cases, m_meta,
                   {"g-kgt-free": {"group": "g-kgt-free", "carrier": "kgt-free",
                                   "carrier_mediated": False, "carrier_service_base": None}},
                   r_skip)
    add("跳过: 非载体边车组（plugin 直连）⇒ 零网络读、跳过、不放行也不 BLOCKED", lambda: _ok(
        c_skip["code"] is None and not r_skip.calls and "跳过" in c_skip["text"],
        "code=%r calls=%d" % (c_skip["code"], len(r_skip.calls))))

    doc_path = evi_dir / "_cases_with_meta.json"
    write_text_utf8(doc_path, json.dumps({"meta": m_meta, "cases": m_cases},
                                         ensure_ascii=False, indent=2) + "\n")
    got_doc = load_cases_document(doc_path)
    add("接线: load_cases_document 带出 meta（旧实现丢弃 meta ⇒ 缺陷②的根因）", lambda: _ok(
        got_doc == (m_cases, m_meta) and load_cases(doc_path) == m_cases,
        "meta=%r" % (got_doc[1],)))

    real_cases_path = REPO_ROOT / "shared" / "model" / "m5-cases.yaml"
    try:
        load_cases_document(real_cases_path)
        _real_msg = "<未抛>"
    except M5Blocked as exc:
        _real_msg = str(exc)
    add("接线: 契约层用例文件的既有 BLOCKED 文案逐字不变（新断言在其后，不抢跑）", lambda: _ok(
        _real_msg == ("用例文件顶层既不是非空列表，也没有 cases 列表：%s" % real_cases_path),
        clip(_real_msg, 120)))

    # ------------------------------------------------------------------
    print("=" * 78)
    print("M5 / W19 m5_run 离线自检（--selftest，假 transport，不发任何网络请求）")
    print("=" * 78)
    n_ok = 0
    for name, ok, detail in checks:
        n_ok += 1 if ok else 0
        print("  [%s] %-66s %s" % ("OK " if ok else "BAD", name[:66], clip(detail, 150)))
    total = len(checks)
    concl = PASS if n_ok == total else FAIL
    print("-" * 78)
    print("selftest: %d/%d 项通过；结论=%s；退出码=%d" % (n_ok, total, concl, EXIT[concl]))
    print("（覆盖：SSE 解析 / 工具事件 / token 与耗时 / key 建删与失败隔离 / 组集合一致性 / "
          "端口不变量 / 用例契约 / 矩阵轮转 / 答案文件重复键拒绝 / dry-run 不联网 / "
          "产物链 answers.jsonl+run_manifest.json+report.md / "
          "用例集↔载体在线快照同源断言（W46，假 health_reader 零网络））")
    return EXIT[concl]


def _raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _capture(fn) -> dict:
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn()
    return {"code": code, "text": buf.getvalue()}


_NS_DEFAULTS = dict(cases=None, out=None, groups=None, repeat=DEFAULT_REPEAT, limit=0,
                    dry_run=False, strict=False, selftest=False, check=False,
                    verify_answers=None, binding=None, timeout=DEFAULT_TIMEOUT,
                    user="m5-run", snapshot_id=None, pace_s=0.0,
                    max_key_failures=3, no_health=False, dify_only=False)


def _ns(**kw):
    d = dict(_NS_DEFAULTS)
    d.update(kw)
    return argparse.Namespace(**d)


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------


def cmd_dry_run(args, declared, selected, cases) -> int:
    plan, skipped = plan_matrix(cases, selected, args.repeat, args.limit or 0)
    print("=" * 78)
    print("M5 运行台 --dry-run：**不发送任何网络请求**，只打印执行矩阵")
    print("=" * 78)
    print("用例集：%s（%d 条）" % (args.cases, len(cases)))
    print("实验组（由 binding.yaml 推导）：%s" % ", ".join(selected))
    print("repeat=%d；limit=%s；轮转 = %s" % (args.repeat, args.limit or 0,
                                             "repeat → case → group（docs/06 §4 P1）"))
    n_keys = 0
    for g in selected:
        cfg = declared[g]
        print("-" * 78)
        print("组 %s" % g)
        print("  App uuid        : %s" % cfg["uuid"])
        print("  carrier/provider: %s / %s（kind=%s）"
              % (cfg["carrier"], cfg["provider"], cfg["provider_kind"]))
        print("  声明工具面      : %s" % cfg["declared_tools"])
        print("  工具面指纹      : sha256[:12]=%s" % cfg["declared_tools_sha256_12"])
        print("  前置不变量      : App mode 必须 %r；工具面 == 声明；端口 ∈ 白名单"
              % APP_MODE_EXPECTED)
        print("  临时 key        : 每次调用现建一副，finally 删 + 回读确认")
        print("  调用面          : POST <DIFY_BASE_URL>/v1/chat-messages "
              "response_mode=streaming, timeout=%ds, conversation_id=\"\"" % args.timeout)
    print("-" * 78)
    print("矩阵：%d 条用例 × %d 组 × %d repeat = %d 条 (case,group,repeat)"
          % (len(cases), len(selected), args.repeat, len(plan) + len(skipped)))
    print("合计将发起 %d 次调用（每副临时 key 一次）；跳过 %d 次"
          % (len(plan), len(skipped)))
    print("预计临时 api-key：建 %d 副 / 删 %d 副（每副一次调用）" % (len(plan), len(plan)))
    print("dry-run 退出码=0（dry-run 不判定业务结果，也不写 answers.jsonl）")
    return 0


def cmd_check(args, binding, declared, selected) -> int:
    """真机前置不变量：端口（本地）+ App 模式 / 工具面 / 采样（真机只读回读）。"""
    blocked: list[str] = []
    failed: list[str] = []
    print("=" * 78)
    print("M5 运行台 --check：前置不变量（fail-closed）")
    print("=" * 78)

    print("[1] 声明面 / 端口不变量（纯本地，不联网）")
    recs = check_port_invariants(binding, declared)
    for r in recs:
        mark = "ok  " if r["ok"] else "FAIL"
        print("  [%s] %-52s %s" % (mark, r["check"][:52], clip(r["detail"], 90)))
    failed += ["端口不变量失败：%s（%s）" % (r["check"], r["detail"]) for r in recs if not r["ok"]]

    print("[2] 真机只读回读（App 模式 / 工具面 / 模型与采样）")
    env_info = DIFY.load_env(REPO_ROOT)
    base = DIFY.env("DIFY_BASE_URL", required=False)
    email = DIFY.env("DIFY_EMAIL", required=False)
    password = DIFY.env("DIFY_PASSWORD", required=False)
    env_source = ", ".join(x["path"] for x in env_info.get("loaded") or []) or "<none>"
    print("  .env 装载：%s" % env_source)
    if not (base and email and password):
        blocked.append("缺 Dify 凭据（DIFY_BASE_URL / DIFY_EMAIL / DIFY_PASSWORD）⇒ 真机面取不到")
        print("  ★ BLOCKED（≠ FAIL）：缺凭据 ⇒ 真机回读面整块取不到")
    else:
        console = Console(base)
        st = console.login(email, password)
        if st != 200:
            blocked.append("Console login HTTP %s" % st)
            print("  ★ BLOCKED：Console login HTTP %s" % st)
        else:
            print("  Console login ok（base=%s，password %s）" % (base, E2E.fp(password)))
            for g in selected:
                cfg = declared[g]
                gstatus, det = console.app_detail(cfg["uuid"])
                if gstatus != 200 or not isinstance(det, dict):
                    blocked.append("组 %s：GET /apps/<uuid> HTTP %s ⇒ App 详情取不到" % (g, gstatus))
                    print("  [BLK ] %-20s App 详情 HTTP %s" % (g, gstatus))
                    continue
                mode = det.get("mode")
                mc = det.get("model_config") or {}
                if mode != APP_MODE_EXPECTED:
                    blocked.append("组 %s：App mode=%r ≠ %r ⇒ model_config 必为空 ⇒ 永久 BLOCKED"
                                   "（R-41）" % (g, mode, APP_MODE_EXPECTED))
                    print("  [BLK ] %-20s mode=%r（不是 %r）" % (g, mode, APP_MODE_EXPECTED))
                    continue
                if not mc:
                    blocked.append("组 %s：agent-chat 但 model_config 为空" % g)
                    print("  [BLK ] %-20s model_config 为空" % g)
                    continue
                bound, err = console.bound_tools(cfg["uuid"])
                declared_tools = set(cfg["declared_tools"])
                face_ok = bound == declared_tools and bool(bound)
                if not face_ok:
                    failed.append("组 %s：工具面与声明不一致（仅声明 %s；仅真机 %s）"
                                  % (g, sorted(declared_tools - bound), sorted(bound - declared_tools)))
                rb = model_readback(mc)
                drift = sampling_drift(rb)
                print("  [%s] %-20s mode=%s tools=%d 声明==真机=%s model=%s temp=%s "
                      "max_iter=%s 漂移=%s"
                      % ("ok  " if face_ok else "FAIL", g, mode, len(bound), face_ok,
                         (rb.get("model") or {}).get("name"), rb.get("temperature"),
                         rb.get("max_iteration"), drift or "无"))
                if not face_ok:
                    print("        绑定工具面 err=%s" % clip(err, 80))
                if drift:
                    print("        advisory（不改退出码）：%s" % "；".join(drift))
    for cname, cdecl in sorted((binding.get("carriers") or {}).items()):
        sb = cdecl.get("service_base")
        if not sb:
            continue
        rec = capture_source(sb)
        print("  [%s] /health %-24s %s" % ("ok  " if rec.get("ok") else "BLK ", cname,
                                           clip(rec.get("simulated_assert") or rec.get("error"), 100)))
        if not rec.get("ok"):
            blocked.append("载体 %s 的 /health 取不到 ⇒ 采集源指纹未取到（%s）"
                           % (cname, clip(rec.get("error"), 80)))
    verdict, code = three_state(blocked, failed)
    print("-" * 78)
    print("前置不变量结论=%s；退出码=%d" % (verdict, code))
    if blocked:
        print("★ BLOCKED（≠ FAIL）：证据/配置面没取到，不等于被测对象是坏的。")
    if verdict == PASS:
        print("⇒ 允许进入真机采集（仍须逐条记 status；BLOCKED 不得写成 FAIL）。")
    return code


def cmd_verify(args, declared) -> int:
    res = validate_answers(Path(args.verify_answers), declared)
    stats = res["stats"]
    print("=" * 78)
    print("M5 答案文件契约复核（fail-closed）：%s" % args.verify_answers)
    print("=" * 78)
    print("行数=%s 有效行=%s 用例数=%s repeat=%s 空 answer=%s"
          % (stats["lines"], stats["rows"], stats["n_cases"], stats["repeats"],
             stats["empty_answer"]))
    print("分组分布=%s" % stats["groups"])
    print("status 分布=%s" % stats["status"])
    print("source 分布=%s" % stats["source"])
    if res["errors"]:
        print("-" * 78)
        print("★ 契约违规 %d 项（fail-closed）：" % len(res["errors"]))
        for e in res["errors"][:60]:
            print("  - %s" % e)
        print("结论=FAIL；退出码=%d" % EXIT[FAIL])
        return EXIT[FAIL]
    print("结论=PASS；退出码=0")
    return 0


def cmd_run(args, binding, declared, selected, cases) -> int:
    plan, skipped = plan_matrix(cases, selected, args.repeat, args.limit or 0)
    assert_unique_plan(plan)

    env_info = DIFY.load_env(REPO_ROOT)
    base = DIFY.env("DIFY_BASE_URL", required=False)
    email = DIFY.env("DIFY_EMAIL", required=False)
    password = DIFY.env("DIFY_PASSWORD", required=False)
    env_source = ", ".join(x["path"] for x in env_info.get("loaded") or []) or "<none>"
    if not (base and email and password):
        print("★ BLOCKED（≠ FAIL）: 缺 Dify 凭据（DIFY_BASE_URL / DIFY_EMAIL / DIFY_PASSWORD）")
        print("  .env 装载：%s；候选来源见 shared/deploy/dify/common.py:48-61" % env_source)
        print("  fail-closed ⇒ 退出码 2")
        return EXIT[BLOCKED]

    console = Console(base)
    st = console.login(email, password)
    if st != 200:
        print("★ BLOCKED（≠ FAIL）: Console login HTTP %s" % st)
        return EXIT[BLOCKED]
    print("Console login ok；DIFY_BASE_URL=%s；password %s" % (base, E2E.fp(password)))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sel_cfg = selected_map(declared, selected)   # ★ 接线：组名列表 → {组名: 声明配置}
    rn = Runner(binding, declared, sel_cfg, out_dir, console, http_post_chat,
                repeat=args.repeat, limit=args.limit or 0, timeout=args.timeout,
                user=args.user, snapshot_id=args.snapshot_id, pace_s=args.pace_s,
                max_key_failures=args.max_key_failures,
                read_health_flag=not args.no_health)
    rn.base_url = base
    run_out = rn.run(plan, skipped)

    rows = run_out["rows"]
    planned_keys = [(r["case_id"], r["group"], r["repeat"]) for r in rows]
    assert_unique_plan([{"case_id": a, "group": b, "repeat": c} for a, b, c in planned_keys])

    answers_path = out_dir / "answers.jsonl"
    write_jsonl_utf8(answers_path, rows)
    self_check = validate_answers(answers_path, declared)

    counts = run_out["counts"]
    verdict, code = three_state(
        [None] if (counts["blocked"] or any(rn.group_state[g].get("blocked") for g in selected)) else [],
        [None] if counts["failed"] else [])
    if not rows:
        verdict, code = BLOCKED, EXIT[BLOCKED]

    manifest = build_manifest(binding, declared, sel_cfg, cases, plan, skipped, run_out,
                              args, env_source, base,
                              answers_sha256_12=sha12_bytes(answers_path.read_bytes()))
    manifest["group_state_raw"] = {g: {k: v for k, v in rn.group_state[g].items()}
                                  for g in selected}
    manifest["honesty"]["real_machine_matrix_run"] = True
    manifest["self_check"] = {
        "answers_contract_ok": not self_check["errors"],
        "errors": self_check["errors"][:40],
        "stats": {k: v for k, v in self_check["stats"].items() if k != "cases"},
        "n_cases": self_check["stats"].get("n_cases"),
    }
    manifest["honesty"]["not_verified"] = [
        "本次运行的 50 条判决集完整性（用例集若为子集，见 plan.invocations_planned）",
        "判据维度（root_cause_accuracy 等）不在本文件范围，由 W18 计算",
        "载体侧工具真执行（载体审计链）不在本运行台范围，W7 冒烟已另测",
    ]
    if self_check["errors"]:
        manifest["honesty"]["not_verified"].append(
            "★ 答案文件自身违反契约 ⇒ 已记 self_check.errors，判 FAIL（fail-closed）")
        verdict, code = three_state([None], [None])

    write_text_utf8(out_dir / "run_manifest.json", DIFY.dump_pretty(manifest) + "\n")
    write_text_utf8(out_dir / "report.md", render_report(manifest, rows, verdict))

    print("\n" + "=" * 78)
    print("M5 运行台结论")
    print("=" * 78)
    for g in selected:
        gs = rn.group_state[g]
        print("  组 %-22s mode=%-11s 工具面==声明=%-5s blocked=%-5s fail=%s"
              % (g, gs.get("app_mode"), gs.get("declared_equals_readback"),
                 gs.get("blocked"), gs.get("fail")))
    print("  计数：ok=%d failed=%d blocked=%d skipped=%d（计划 %d）"
          % (counts["ok"], counts["failed"], counts["blocked"], counts["skipped"], counts["planned"]))
    print("  答案文件：%s（契约复核 %s）"
          % (answers_path, "通过" if not self_check["errors"] else "★ 违规"))
    for e in self_check["errors"][:5]:
        print("    - %s" % e)
    print("  清单：%s" % (out_dir / "run_manifest.json"))
    print("  报告：%s" % (out_dir / "report.md"))
    print("总体结论=%s；退出码=%d" % (verdict, code))
    if verdict == BLOCKED:
        print("★ BLOCKED（≠ FAIL）：证据面没取全，不等于被测对象是坏的。")
    return code


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="M5 运行台：用例集 × 实验组 × repeat 真机采集（答案文件 JSONL，三态退出码）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", help="用例文件路径（.yaml/.yml/.json）")
    ap.add_argument("--out", default=None, help="输出目录（默认 tmp\\m5_run\\<UTC 戳>）")
    ap.add_argument("--groups", default=None,
                    help="逗号分隔的实验组名（须 ∈ binding.yaml 的 apps 段；缺省＝全部）")
    ap.add_argument("--repeat", type=int, default=DEFAULT_REPEAT,
                    help="重复次数（默认 %d，docs/06 §4 P8）" % DEFAULT_REPEAT)
    ap.add_argument("--limit", type=int, default=0, help="最多执行矩阵前 N 次调用（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只打印矩阵，不联网、不写文件")
    ap.add_argument("--strict", action="store_true",
                    help="用例 schema 严格校验（§1.2 全字段/唯一 id/cls 与难度台账）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（假 transport，绝不联网）")
    ap.add_argument("--check", action="store_true", help="真机前置不变量检查（App 模式/工具面/端口）")
    ap.add_argument("--verify-answers", default=None,
                    help="fail-closed 复核既有 answers.jsonl（必需键/组集合/重复键）")
    ap.add_argument("--binding", default=str(BINDING_PATH), help="binding.yaml 路径")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="/v1/chat-messages 超时秒")
    ap.add_argument("--user", default=DEFAULT_USER, help="Dify 调用的 user 标识")
    ap.add_argument("--snapshot-id", default=None,
                    help="覆盖写进每行的 snapshot_id（缺省取用例的 snapshot_id，再缺省为 null）")
    ap.add_argument("--pace-s", type=float, default=0.0, help="每次调用之间的间隔秒（默认 0）")
    ap.add_argument("--max-key-failures", type=int, default=3,
                    help="同一组连续建 key 失败达此数即短路该组余下行（默认 3）")
    ap.add_argument("--no-health", action="store_true", help="不读载体 /health（不做采集源指纹）")
    return ap


def _as_selected_mapping(selected):
    """W46 同源断言只吃 ``{组名: 声明配置}``；组名列表请先过 :func:`selected_map`。

    形状不对就**立即抛错**（fail-closed）：旧实现走 ``(selected or {}).items()``，
    空列表 / 空字典会静默退化成「本次所选组里没有可比的载体边车 ⇒ 不 BLOCKED」，
    即把「没测到」误读成「同源」，本函数专治这一形状。
    """
    if selected is None:
        return {}
    if not isinstance(selected, dict):
        raise TypeError("assert_case_snapshot_binding: selected 必须是 {组名: 声明配置}"
                        "（收到 %s）；组名列表请先过 selected_map(declared, names)"
                        % type(selected).__name__)
    return selected


def main() -> int:
    args = build_argparser().parse_args()

    if args.repeat < 1:
        print("★ BLOCKED（≠ FAIL）：--repeat 必须 ≥1（收到 %r）" % args.repeat)
        return EXIT[BLOCKED]

    if args.selftest:
        return run_selftest()

    # 声明面（唯一来源）——任何失败都是 fail-closed
    try:
        binding = load_binding(Path(args.binding))
        declared = declared_groups(binding)
    except M5Blocked as exc:
        print("★ BLOCKED（≠ FAIL）：%s" % exc)
        print("  fail-closed ⇒ 退出码 2（不跑一个不是声明的实验）")
        return EXIT[BLOCKED]

    if args.verify_answers:
        return cmd_verify(args, declared)

    try:
        selected = resolve_groups(declared, (args.groups or "").split(",") if args.groups else None)
    except M5Blocked as exc:
        print("★ BLOCKED（≠ FAIL）：%s" % exc)
        print("  binding.apps 声明的组：%s" % ", ".join(declared))
        return EXIT[BLOCKED]

    if args.check:
        return cmd_check(args, binding, declared, selected)

    if not args.cases:
        print("★ 需要 --cases <yaml|json>（或用 --selftest / --check / --verify-answers）")
        return EXIT[BLOCKED]

    try:
        cases, cases_meta = load_cases_document(Path(args.cases))
    except M5Blocked as exc:
        print("★ BLOCKED（≠ FAIL）：%s" % exc)
        return EXIT[BLOCKED]

    v = validate_cases(cases)
    for w in v["warn"]:
        print("  ! 用例提示：%s" % w)
    if v["hard"]:
        print("★ BLOCKED（≠ FAIL）：用例契约硬错误 %d 项（fail-closed）：" % len(v["hard"]))
        for e in v["hard"][:40]:
            print("  - %s" % e)
        return EXIT[BLOCKED]
    if v["strict"]:
        if args.strict:
            print("★ BLOCKED（≠ FAIL）：--strict 下用例契约错误 %d 项（fail-closed）："
                  % len(v["strict"]))
            for e in v["strict"][:40]:
                print("  - %s" % e)
            return EXIT[BLOCKED]
        for e in v["strict"][:20]:
            print("  ! 非 --strict：仅登记 - %s" % e)

    if args.dry_run:
        return cmd_dry_run(args, declared, selected, cases)

    # W46 缺陷②：开跑前的 fail-closed 同源断言（用例集声明快照 == 各载体在线快照）。
    # ★ 放在既有输入校验与 --dry-run **之后**：用例文件缺失/形态非法时保持原
    #   `★ BLOCKED（≠ FAIL）…` 语义与 123 B 输出量级，新断言不得抢先报错；
    #   --dry-run 只打印执行矩阵、不发网络请求，故也不经过这里。
    if assert_case_snapshot_binding(cases, cases_meta, selected_map(declared, selected)) is not None:
        return EXIT[BLOCKED]

    if args.out is None:
        args.out = str(Path("tmp") / "m5_run" / stamp_utc())
    return cmd_run(args, binding, declared, selected, cases)


if __name__ == "__main__":
    sys.exit(main())
