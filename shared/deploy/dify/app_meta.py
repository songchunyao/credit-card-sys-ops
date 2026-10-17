#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""应用元数据（name / description）声明↔真机收敛器 · W20。

职责边界（红线）
----------------
* 只碰 App 的 ``name`` 与 ``description``；**模型面零改动**：工具集、``pre_prompt``、
  ``agent_mode``、模型参数一律不读不写（本文件不含 ``model-config`` 调用）。
* 不改 App 的 uuid / 机器键 / 载体绑定；不建 App、不删 App、不碰插件与签名闸。
* 只读用 ``GET /console/api/apps/<uuid>``；改名用 ``POST /console/api/apps/<uuid>/name``；
  改描述用 ``PUT /console/api/apps/<uuid>``，且 ``icon_type`` / ``icon`` / ``icon_background`` /
  ``use_icon_as_answer_icon`` / ``max_active_requests`` **必须从真机读回后原值回填**。

依据（源码级，已由队长证实；本文件按此语义实现读-改-写）
------------------------------------------------------
* ``PUT /console/api/apps/<uuid>`` handler 写 ``req_data.icon or ""``、
  ``icon_background or ""``、``use_icon_as_answer_icon or False``、``max_active_requests or 0``
  ⇒ **省略这些字段会清空图标**，故本文件一律「先读真机、再原值回填、再逐字回读」。
* ★ **写入后回读断言的口径（W24 修正，真机实测确证）**：``max_active_requests`` 上的
  ``req_data.max_active_requests or 0`` 是**不可逆的平台强制转换**——真机读回 ``null``，
  我方忠实原值回填 ``null``，平台仍落成 ``0``（日志形如 ``回填 ... max_active_requests=null``
  而回读为 ``0``）。这不是我方写错，故该字段的回读断言做**归一化比较**：
  等价类取平台自身的 ``v or 0``（即 ``null ↔ 0`` 等价），**且原始 before/after 两值一律
  打印留证**；``max_active_requests`` 的**真实数值改动**（如 ``0 → 5``）仍判 FAIL。
  其余图标字段 ``icon_type`` / ``icon`` / ``icon_background`` /
  ``use_icon_as_answer_icon`` **继续逐字严格比较，一个字符都不放宽**。
* ``POST /console/api/apps/<uuid>/name``（``AppNamePayload``）只改名，不动图标。
* App 的 name/description **不进模型面**（``/app/api/core/`` 内 0 处 ``app_model.name``），
  改名前后的模型面字符串逐字不变（证据见 tmp/_m4_w20_report.md）。

退出码
------
====  ==========================================================
0     收敛（--check 无漂移 / --apply 全部达成并回读断言通过 / 自检 PASS）
1     漂移或写入后回读断言失败（FAIL；图标四件套逐字严格，仅 ``max_active_requests`` 归一 ``null↔0``）
2     BLOCKED：前置缺失（App 不存在 / 模式非 agent-chat / 声明缺 display_* 字段）
3     参数或环境错误（binding 不可读、--apps 未知名、声明值非法如描述 > 400 字、HTTP 非 2xx）
====  ==========================================================

优先级：BLOCKED(2) > FAIL(1) > PASS(0)（与 shared/deploy/check_fairness.py 的 RANK 一致）。

全程显式 UTF-8 输出（R-49：门禁判定不得依赖宿主代码页）；任何分支不打印口令 / token 字面量
（R-47：只许长度与 sha 前缀）。
"""
import argparse
import contextlib
import io
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
REPO_ROOT = HERE.parents[2]

import common  # noqa: E402

BINDING_PATH = HERE / "binding.yaml"

APP_DETAIL = "/console/api/apps/%s"
APP_RENAME = "/console/api/apps/%s/name"
APP_UPDATE = "/console/api/apps/%s"
AGENT_CHAT_MODE = "agent-chat"
DESC_MAX = 400
#: PUT 必须原值回填的图标/限额字段（省略即被真机控制器清空）
ICON_FIELDS = ("icon_type", "icon", "icon_background",
               "use_icon_as_answer_icon", "max_active_requests")
#: 疑似受平台 ``or 默认`` 强制转换的字段（**只此一个**允许归一化比较）
LIMIT_FIELD = "max_active_requests"
#: 逐字严格比较、**永不归一**的图标字段（W24 不许放宽的就是这四个）
STRICT_ICON_FIELDS = ("icon_type", "icon", "icon_background", "use_icon_as_answer_icon")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_USAGE = 3


def utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class Blocked(Exception):
    """前置缺失 ⇒ exit 2。"""


class Fail(Exception):
    """断言失败 ⇒ exit 1。"""


class UsageError(Exception):
    """参数 / 声明非法 ⇒ exit 3。"""


class EnvError(Exception):
    """环境或 HTTP 层错误 ⇒ exit 3。"""


# ---------------------------------------------------------------------------
# 声明面
# ---------------------------------------------------------------------------
def load_declaration(path) -> dict:
    p = Path(path)
    if not p.exists():
        raise UsageError("binding 不存在：%s" % p)
    try:
        return common.load_binding(p)
    except Exception as exc:
        raise UsageError("binding 不可解析：%s（%s）" % (p, exc))


def select_keys(binding: dict, only=None) -> list:
    apps = binding.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise UsageError("binding.apps 缺失或为空")
    keys = list(apps)
    if only:
        want = [s.strip() for s in str(only).split(",") if s.strip()]
        missing = [k for k in want if k not in apps]
        if missing:
            raise UsageError("--apps 含未知名：%s（已声明：%s）" % (missing, keys))
        keys = want
    return keys


def declared_entry(key: str, cfg) -> tuple:
    """(uuid, display_name, display_description)；缺字段 ⇒ Blocked，值非法 ⇒ UsageError。"""
    if not isinstance(cfg, dict):
        raise Blocked("apps.%s 不是映射" % key)
    uuid = cfg.get("uuid")
    if not (isinstance(uuid, str) and uuid.strip()):
        raise Blocked("apps.%s 未登记 uuid" % key)
    name = cfg.get("display_name")
    desc = cfg.get("display_description")
    if not (isinstance(name, str) and name.strip()):
        raise Blocked("apps.%s 缺 display_name" % key)
    if not (isinstance(desc, str) and desc.strip()):
        raise Blocked("apps.%s 缺 display_description" % key)
    if "\n" in name or "\n" in desc:
        raise UsageError("apps.%s 的 display_* 不得含换行" % key)
    if name != name.strip() or desc != desc.strip():
        raise UsageError("apps.%s 的 display_* 不得含首尾空白" % key)
    if len(name) > 64:
        raise UsageError("apps.%s 的 display_name 长度 %d > 64" % (key, len(name)))
    if len(desc) > DESC_MAX:
        raise UsageError("apps.%s 的 display_description 长度 %d > %d（Dify 侧会拒绝）"
                         % (key, len(desc), DESC_MAX))
    return uuid, name, desc


# ---------------------------------------------------------------------------
# 真机（只读 + 两个写点）
# ---------------------------------------------------------------------------
def build_console(transport=None) -> "common.Console":
    """transport 非空 ⇒ 离线（假 transport，不读 .env、不联网）；否则走既有登录设施。"""
    if transport is not None:
        c = common.Console("http://dify.selftest", transport=transport, prefix="console/api")
        c.headers["X-CSRF-TOKEN"] = "selftest-csrf"
        c.headers["Authorization"] = "Bearer selftest-token"
        c.email_fingerprint = {"email_len": 0, "password_len": 0}
        return c
    return common.console_client(REPO_ROOT)


def live_app(console, uuid: str) -> dict:
    r = console.request("GET", APP_DETAIL % uuid, None, expect=None)
    if r.status_code == 404:
        raise Blocked("App uuid=%s 不存在（GET HTTP 404）" % uuid)
    if r.status_code != 200:
        raise EnvError("GET %s HTTP %s" % (APP_DETAIL % uuid, r.status_code))
    try:
        obj = json.loads(r.text)
    except Exception as exc:
        raise EnvError("GET %s 响应非 JSON（%s）" % (APP_DETAIL % uuid, exc))
    if not isinstance(obj, dict):
        raise EnvError("GET %s 响应不是对象" % (APP_DETAIL % uuid))
    return obj


def plan(args, transport=None) -> dict:
    """两阶段前置：先校验全部声明，再只读真机。返回 {console, rows, blocked, keys}。"""
    binding = load_declaration(args.binding)
    keys = select_keys(binding, getattr(args, "apps", None))
    blocked = []
    decls = []
    for key in keys:
        try:
            decls.append((key,) + declared_entry(key, binding["apps"][key]))
        except Blocked as exc:
            blocked.append((key, str(exc)))
    console = build_console(transport)
    rows = []
    for key, uuid, want_name, want_desc in decls:
        try:
            obj = live_app(console, uuid)
            mode = obj.get("mode")
            if mode != AGENT_CHAT_MODE:
                raise Blocked("apps.%s（uuid=%s）mode=%r ≠ %s；只允许改 name/description，"
                              "不改模式" % (key, uuid, mode, AGENT_CHAT_MODE))
            rows.append({
                "key": key, "uuid": uuid,
                "want_name": want_name, "want_desc": want_desc,
                "live": obj,
                "live_name": obj.get("name"), "live_desc": obj.get("description"),
                "drift": [f for f, live_v, want_v in (
                    ("name", obj.get("name"), want_name),
                    ("description", obj.get("description"), want_desc)) if live_v != want_v],
            })
        except Blocked as exc:
            blocked.append((key, str(exc)))
    return {"console": console, "rows": rows, "blocked": blocked, "keys": keys}


def _icon_snapshot(obj: dict) -> dict:
    return {k: obj.get(k) for k in ICON_FIELDS}


def _fmt(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def platform_limit(value):
    """复刻平台 PUT handler 的 ``req_data.max_active_requests or 0``。

    这是**平台强制转换**（非我方写入）：真机读回 ``null``、我方原值回填 ``null``，
    平台仍落成 ``0``。等价类 = ``v or 0``，故 ``null ↔ 0`` 视为等价；
    ``0 → 5`` 之类**真实数值改动**不落在同一等价类，仍判不等。
    """
    return value or 0


def icon_field_diff(before: dict, after: dict) -> tuple:
    """写入后回读的图标/限额面比对。返回 ``(strict, coerced)``。

    * ``strict``  —— 逐字严格比较不通过的条目（``icon_type`` / ``icon`` /
      ``icon_background`` / ``use_icon_as_answer_icon`` 任一不同，以及
      ``max_active_requests`` 的真实数值改动）。**非空 ⇒ FAIL**。
    * ``coerced`` —— 仅 ``max_active_requests`` 命中平台 ``or 0`` 强制转换的条目，
      **原始 before/after 两值都写进字符串**留证；归一后等价，不判 FAIL。
    """
    strict, coerced = [], []
    for field in ICON_FIELDS:
        b, a = before.get(field), after.get(field)
        if b == a:
            continue
        if field == LIMIT_FIELD and platform_limit(b) == platform_limit(a):
            coerced.append("%s 平台 `or 0` 强制转换（非我方写入）：before=%s after=%s ⇒ 归一等价"
                           % (field, _fmt(b), _fmt(a)))
            continue
        strict.append("%s before=%s after=%s" % (field, _fmt(b), _fmt(a)))
    return strict, coerced


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
def cmd_check(args, transport=None) -> int:
    p = plan(args, transport)
    print("[check] binding=%s apps=%d" % (args.binding, len(p["keys"])))
    drift_n = 0
    for row in p["rows"]:
        flag = "DRIFT" if row["drift"] else "ok"
        print("  %-19s uuid=%s mode=%s %s" % (row["key"], row["uuid"],
                                              row["live"].get("mode"), flag))
        if row["drift"]:
            drift_n += 1
            for field in row["drift"]:
                live_v = row["live_name"] if field == "name" else row["live_desc"]
                want_v = row["want_name"] if field == "name" else row["want_desc"]
                print("    DRIFT %s: live=%s" % (field, _fmt(live_v)))
                print("    DRIFT %s: want=%s" % (field, _fmt(want_v)))
    for key, why in p["blocked"]:
        print("BLOCKED: %s" % why)
        print("  app=%s" % key)
    if p["blocked"]:
        print("RESULT: BLOCKED（前置缺失 %d 个 / 声明 %d 个；漂移 %d 个）"
              % (len(p["blocked"]), len(p["keys"]), drift_n))
        return EXIT_BLOCKED
    if drift_n:
        print("RESULT: DRIFT（%d 个 App 与声明不一致 / 共 %d）" % (drift_n, len(p["keys"])))
        return EXIT_FAIL
    print("RESULT: PASS（%d 个 App 的 name/description 与声明逐字一致）" % len(p["keys"]))
    return EXIT_PASS


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------
def cmd_apply(args, transport=None) -> int:
    p = plan(args, transport)
    console = p["console"]
    print("[apply] binding=%s apps=%d" % (args.binding, len(p["keys"])))
    failed = []
    renamed = []
    redescribed = []
    coercions = []
    writes = []
    for row in p["rows"]:
        key, uuid = row["key"], row["uuid"]
        if not row["drift"]:
            print("  %-19s 已是目标状态（不发请求）" % key)
            continue
        # 1) 改名：POST /console/api/apps/<uuid>/name（只改名，不动图标）
        if "name" in row["drift"]:
            r = console.request("POST", APP_RENAME % uuid,
                                {"name": row["want_name"]}, expect=(200,))
            writes.append("POST %s" % key)
            print("  %-19s POST name → HTTP %s" % (key, r.status_code))
            renamed.append(key)
            row["live"] = live_app(console, uuid)
        # 2) 改描述：PUT /console/api/apps/<uuid>，图标/限额字段从真机读回后原值回填
        if "description" in row["drift"]:
            fresh = live_app(console, uuid)
            before_icons = _icon_snapshot(fresh)
            body = {"name": row["want_name"], "description": row["want_desc"]}
            for f in ICON_FIELDS:
                body[f] = fresh.get(f)
            r = console.request("PUT", APP_UPDATE % uuid, body, expect=(200,))
            writes.append("PUT %s" % key)
            print("  %-19s PUT description → HTTP %s（回填 %s）"
                  % (key, r.status_code, ", ".join("%s=%s" % (f, _fmt(body[f])) for f in ICON_FIELDS)))
            redescribed.append(key)
            # 3) 写入后回读断言：图标四件套逐字严格；仅 max_active_requests 归一 null↔0
            after = live_app(console, uuid)
            row["live"] = after
            if after.get("name") != row["want_name"]:
                failed.append("%s 名称回读=%s ≠ 声明=%s" % (key, _fmt(after.get("name")), _fmt(row["want_name"])))
            if after.get("description") != row["want_desc"]:
                failed.append("%s 描述回读=%s ≠ 声明=%s"
                              % (key, _fmt(after.get("description")), _fmt(row["want_desc"])))
            after_icons = _icon_snapshot(after)
            strict_diff, coerced = icon_field_diff(before_icons, after_icons)
            for note in coerced:
                coercions.append("%s %s" % (key, note))
                print("  %-19s COERCED（平台强制转换，归一后等价，不判 FAIL）%s" % (key, note))
            if strict_diff:
                failed.append("%s 图标字段被改动（逐字严格比较，原始两值已保留）：%s；before=%s after=%s"
                              % (key, "；".join(strict_diff), _fmt(before_icons), _fmt(after_icons)))
        else:
            # 只改名：逐字回读断言
            after = live_app(console, uuid)
            row["live"] = after
            if after.get("name") != row["want_name"]:
                failed.append("%s 名称回读=%s ≠ 声明=%s" % (key, _fmt(after.get("name")), _fmt(row["want_name"])))
    for key, why in p["blocked"]:
        print("BLOCKED: %s" % why)
        print("  app=%s" % key)
    print("  改名 %d 个：%s" % (len(renamed), renamed))
    print("  改描述 %d 个：%s" % (len(redescribed), redescribed))
    print("  写请求数：%d（POST name %d / PUT description %d）%s"
          % (len(writes), len(renamed), len(redescribed), writes))
    print("  平台 or 0 归一记录 %d 条（max_active_requests null↔0；原始两值见上）" % len(coercions))
    for msg in failed:
        print("FAIL: %s" % msg)
    if p["blocked"]:
        print("RESULT: BLOCKED（前置缺失 %d 个；FAIL %d 条）" % (len(p["blocked"]), len(failed)))
        return EXIT_BLOCKED
    if failed:
        print("RESULT: FAIL（写入后回读断言 %d 条不通过）" % len(failed))
        return EXIT_FAIL
    print("RESULT: PASS（%d 个 App 的 name/description 已是声明值；图标四件套逐字未变；"
          "max_active_requests 归一 %d 条）" % (len(p["keys"]), len(coercions)))
    return EXIT_PASS


# ---------------------------------------------------------------------------
# --selftest（离线假 transport；含负控；断言真实退出码）
# ---------------------------------------------------------------------------
class _Resp:
    __slots__ = ("status_code", "text", "headers", "json_obj")

    def __init__(self, status_code, obj=None, text=None):
        self.status_code = status_code
        self.headers = {}
        self.text = text if text is not None else json.dumps(obj, ensure_ascii=False)
        self.json_obj = obj


class FakeAppServer:
    """最小假 Console：**忠实复刻** PUT handler 的 ``x or 默认`` 清空语义。

    ``put_hook(app, body)`` 可选：在标准写入之后调用，用于构造「写入成功但回读值
    被平台改动」的负控（例如真机把 ``max_active_requests`` 从 0 改成 5）。
    """

    def __init__(self, apps: dict, drop_description_on_put: bool = False, put_hook=None):
        self.apps = {k: dict(v) for k, v in apps.items()}
        self.drop_description_on_put = drop_description_on_put
        self.put_hook = put_hook
        self.calls = []

    def request(self, method, url, json_body=None, headers=None, timeout=30):
        method = method.upper()
        path = url.split("http://dify.selftest", 1)[-1]
        self.calls.append((method, path, json_body))
        if path == "/console/api/login":
            return _Resp(200, {"result": "success"})
        m = re.match(r"^/console/api/apps/([^/]+)/name$", path)
        if m and method == "POST":
            app = self.apps.get(m.group(1))
            if app is None:
                return _Resp(404, {"message": "not found"})
            body = dict(json_body or {})
            if not body.get("name"):
                return _Resp(400, {"message": "name required"})
            app["name"] = body["name"]
            return _Resp(200, {"result": "success"})
        m = re.match(r"^/console/api/apps/([^/]+)$", path)
        if m and method == "GET":
            app = self.apps.get(m.group(1))
            if app is None:
                return _Resp(404, {"message": "not found"})
            return _Resp(200, dict(app))
        if m and method == "PUT":
            app = self.apps.get(m.group(1))
            if app is None:
                return _Resp(404, {"message": "not found"})
            body = dict(json_body or {})
            if not body.get("name"):
                return _Resp(400, {"message": "name required"})
            # ★ app.py 的 `req_data.icon or ""` 语义：省略即清空
            app["name"] = body["name"]
            if not self.drop_description_on_put:
                app["description"] = body.get("description")
            app["icon_type"] = body.get("icon_type") or ""
            app["icon"] = body.get("icon") or ""
            app["icon_background"] = body.get("icon_background") or ""
            app["use_icon_as_answer_icon"] = body.get("use_icon_as_answer_icon") or False
            app["max_active_requests"] = body.get("max_active_requests") or 0
            if self.put_hook is not None:
                self.put_hook(app, body)
            return _Resp(200, {"result": "success"})
        return _Resp(404, {"message": "unhandled %s %s" % (method, path)})


FX_ICON = {"icon_type": "emoji", "icon": "🧪", "icon_background": "#123456",
           "use_icon_as_answer_icon": True, "max_active_requests": 7}
FX_LEGACY_DESC = "M4 主对比组 · 只读运维工具集 Agent（声明面 shared/deploy/dify/binding.yaml）"
FX_APPS = {
    "aaaa-1111": {"id": "aaaa-1111", "name": "cc-kg-agent", "description": FX_LEGACY_DESC,
                  "mode": "agent-chat", **FX_ICON},
    "bbbb-2222": {"id": "bbbb-2222", "name": "cc-kgt-agent", "description": FX_LEGACY_DESC,
                  "mode": "agent-chat", **FX_ICON},
}
#: 真机现状口径：``max_active_requests`` 读回 ``null``（平台读侧给 null，写侧落 0）
FX_APPS_NULL_LIMIT = {k: dict(v, max_active_requests=None) for k, v in FX_APPS.items()}
#: 真机写入后口径：``max_active_requests`` 已被平台强制落成 ``0``
FX_APPS_ZERO_LIMIT = {k: dict(v, max_active_requests=0) for k, v in FX_APPS.items()}
FX_DECL = {
    "cc-kg-agent": {"uuid": "aaaa-1111", "carrier": "kg", "provider": "cc-kg-tools",
                    "display_name": "方案1·知识图谱+LLM（主对比组）",
                    "display_description": "载体＝知识图谱（Neo4j）。挂 8 个载体中性只读工具，经 :8301 知识图谱工具服务。"},
    "cc-kgt-agent": {"uuid": "bbbb-2222", "carrier": "kgt", "provider": "cc-kgt-tools",
                     "display_name": "方案3·知识图谱直引·门面版",
                     "display_description": "5 个原生只读工具，经 :8303 知识图谱门面服务。"},
}


def _write_decl(td, apps: dict, name: str = "binding.yaml") -> str:
    text = ("meta:\n  contract: m4-app-meta-selftest\n"
            "apps:\n")
    for key, cfg in apps.items():
        text += "  %s:\n" % key
        for k, v in cfg.items():
            text += "    %s: %s\n" % (k, json.dumps(v, ensure_ascii=False))
    p = Path(td) / name
    p.write_text(text, encoding="utf-8", newline="")
    return str(p)


def _argv(binding: str, apps=None):
    return argparse.Namespace(binding=binding, apps=apps)


def _run(fn, ns, transport, want_code, label, checks, must_contain=None):
    """跑真实入口函数并断言**真实退出码**；异常路径按 main() 的映射折算为退出码。"""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = fn(ns, transport)
    except UsageError as exc:
        code = EXIT_USAGE
        buf.write("USAGE: %s\n" % exc)
    except EnvError as exc:
        code = EXIT_USAGE
        buf.write("ENV: %s\n" % exc)
    except Blocked as exc:
        code = EXIT_BLOCKED
        buf.write("BLOCKED: %s\n" % exc)
    out = buf.getvalue()
    if code != want_code:
        raise AssertionError("%s：退出码 %s ≠ 期望 %s\n--- stdout ---\n%s" % (label, code, want_code, out))
    if must_contain and must_contain not in out:
        raise AssertionError("%s：stdout 缺 %r\n--- stdout ---\n%s" % (label, must_contain, out))
    checks.append("%s → exit %s%s" % (label, code, "" if not must_contain else "，含 %r" % must_contain))
    return out


def cmd_selftest(args, transport=None) -> int:
    import tempfile
    checks = []
    try:
        # ── 结构守卫（先于一切）：宽严面不得被后续改动悄悄挪动 ──
        if set(STRICT_ICON_FIELDS) != set(ICON_FIELDS) - {LIMIT_FIELD}:
            raise AssertionError("STRICT_ICON_FIELDS 与 ICON_FIELDS 不一致（逐字严格面被改动）")
        if LIMIT_FIELD in STRICT_ICON_FIELDS:
            raise AssertionError("max_active_requests 不得进入逐字严格面")
        if not (platform_limit(None) == platform_limit(0) == 0):
            raise AssertionError("platform_limit 未把 null 与 0 归一到同一等价类")
        if platform_limit(0) == platform_limit(5):
            raise AssertionError("platform_limit 误把 0 与 5 归为等价（会放过真实改动）")
        if platform_limit(7) != 7:
            raise AssertionError("platform_limit 不得改动非空真值")
        checks.append("结构守卫：逐字严格面 = icon_type/icon/icon_background/use_icon_as_answer_icon；"
                      "归一化只许碰 max_active_requests（等价类 {null,0}；0≠5；真值原样）")
        with tempfile.TemporaryDirectory() as td:
            # ── 正控 0：全部漂移（name+description）→ --check 判红，--apply 收敛 ──
            binding = _write_decl(td, FX_DECL)
            srv = FakeAppServer(FX_APPS)
            out = _run(cmd_check, _argv(binding), srv, EXIT_FAIL, "N① 名字/描述漂移 → check 判红",
                       checks, must_contain="DRIFT name")
            if out.count("DRIFT") < 4:
                raise AssertionError("漂移明细应同时含 name 与 description 的 live/want")
            srv = FakeAppServer(FX_APPS)
            _run(cmd_apply, _argv(binding), srv, EXIT_PASS, "P0 漂移 → apply 收敛", checks,
                 must_contain="RESULT: PASS")
            posts = [c for c in srv.calls if c[0] == "POST" and c[1].endswith("/name")]
            puts = [c for c in srv.calls if c[0] == "PUT"]
            if len(posts) != 2 or len(puts) != 2:
                raise AssertionError("应各发 2 次 POST name / PUT，实际 %d/%d" % (len(posts), len(puts)))
            if srv.apps["aaaa-1111"]["name"] != FX_DECL["cc-kg-agent"]["display_name"]:
                raise AssertionError("真机 name 未变成声明值")
            if srv.apps["aaaa-1111"]["description"] != FX_DECL["cc-kg-agent"]["display_description"]:
                raise AssertionError("真机 description 未变成声明值")
            checks.append("P0 真机回读：name/description 均为声明值")

            # ── N③ 省略图标字段会清空 ⇒ 读改写必须阻止 ──
            for fid, fv in FX_ICON.items():
                if srv.apps["aaaa-1111"][fid] != fv:
                    raise AssertionError("图标字段 %s 被 PUT 改动：%r ≠ %r"
                                         % (fid, srv.apps["aaaa-1111"][fid], fv))
            for (m, path, body) in puts:
                if any(body.get(f) != FX_ICON[f] for f in ICON_FIELDS):
                    raise AssertionError("PUT body 未原值回填图标字段：%s" % json.dumps(body, ensure_ascii=False))
            # 反向对照：真的省略 icon 时假服务器确实会清空（证明该负控是「真能红」的）
            srv_direct = FakeAppServer(FX_APPS)
            srv_direct.request("PUT", "http://dify.selftest/console/api/apps/aaaa-1111",
                               {"name": "x", "description": "y"})
            if srv_direct.apps["aaaa-1111"]["icon"] != "":
                raise AssertionError("负控③ 假服务器未能复刻 `icon or \"\"` 清空语义")
            checks.append("N③ 省略图标字段会清空（假服务器已复刻）；读改写后真机图标逐字未变；PUT body 含真机 icon 值")

            # ── N⑥ 幂等：第二次 apply 不发 POST/PUT ──
            before = list(srv.calls)
            _run(cmd_apply, _argv(binding), srv, EXIT_PASS, "N⑥ 幂等：第二次 apply 收敛", checks,
                 must_contain="已是目标状态")
            new_writes = [c for c in srv.calls[len(before):] if c[0] in ("POST", "PUT")]
            if new_writes:
                raise AssertionError("幂等失败：第二次仍发写请求 %s" % new_writes)
            checks.append("N⑥ 第二次 apply 写请求数 = 0")

            # ── 正控：check 收敛 exit 0 ──
            _run(cmd_check, _argv(binding), srv, EXIT_PASS, "P1 收敛后 check", checks,
                 must_contain="RESULT: PASS")

            # ── N① 名字漂移（只改真机 name 回机器键）→ check 判红 ──
            srv2 = FakeAppServer(FX_APPS)
            srv2.apps["aaaa-1111"]["name"] = "cc-kg-agent"          # 名字回退成机器键
            srv2.apps["aaaa-1111"]["description"] = FX_DECL["cc-kg-agent"]["display_description"]
            srv2.apps["bbbb-2222"]["name"] = FX_DECL["cc-kgt-agent"]["display_name"]
            srv2.apps["bbbb-2222"]["description"] = FX_DECL["cc-kgt-agent"]["display_description"]
            _run(cmd_check, _argv(binding), srv2, EXIT_FAIL, "N① 仅名字漂移 → check 判红", checks,
                 must_contain="DRIFT name")

            # ── N① b 仅描述漂移 → check 判红 ──
            srv3 = FakeAppServer(FX_APPS)
            srv3.apps["aaaa-1111"]["name"] = FX_DECL["cc-kg-agent"]["display_name"]
            srv3.apps["aaaa-1111"]["description"] = FX_DECL["cc-kg-agent"]["display_description"]
            srv3.apps["bbbb-2222"]["name"] = FX_DECL["cc-kgt-agent"]["display_name"]
            _run(cmd_check, _argv(binding), srv3, EXIT_FAIL, "N① b 仅描述漂移 → check 判红", checks,
                 must_contain="DRIFT description")

            # ── N② 描述超 400 字 → 拒（exit 3，且不发任何请求）──
            too_long = dict(FX_DECL)
            too_long["cc-kg-agent"] = dict(FX_DECL["cc-kg-agent"], display_description="长" * (DESC_MAX + 1))
            bad_binding = _write_decl(td, too_long, "too_long.yaml")
            srv4 = FakeAppServer(FX_APPS)
            _run(cmd_apply, _argv(bad_binding), srv4, EXIT_USAGE, "N② 描述 %d 字 → apply 拒" % (DESC_MAX + 1),
                 checks, must_contain="> 400")
            if srv4.calls:
                raise AssertionError("声明非法时不得发出任何请求，实际 %s" % srv4.calls)
            _run(cmd_check, _argv(bad_binding), srv4, EXIT_USAGE, "N② b 描述超长 → check 亦拒", checks)
            # 边界：恰 400 字应被接受（漂移 ⇒ exit 1，而非 3）
            edge = dict(FX_DECL)
            edge["cc-kg-agent"] = dict(FX_DECL["cc-kg-agent"], display_description="长" * DESC_MAX)
            edge_binding = _write_decl(td, edge, "edge.yaml")
            _run(cmd_check, _argv(edge_binding), FakeAppServer(FX_APPS), EXIT_FAIL,
                 "N② c 恰 %d 字 → 接受（判漂移 exit 1）" % DESC_MAX, checks)

            # ── N④ App 不存在 → BLOCKED(2) ──
            _run(cmd_check, _argv(binding), FakeAppServer({}), EXIT_BLOCKED,
                 "N④ App 不存在 → BLOCKED", checks, must_contain="不存在")
            _run(cmd_apply, _argv(binding), FakeAppServer({}), EXIT_BLOCKED,
                 "N④ b App 不存在 → apply BLOCKED", checks)
            # 一半存在：存在的那半仍被收敛，整体 exit 2（BLOCKED 优先）
            srv5 = FakeAppServer({"aaaa-1111": dict(FX_APPS["aaaa-1111"])})
            _run(cmd_apply, _argv(binding), srv5, EXIT_BLOCKED, "N④ c 半缺 → 仍收敛存在者，exit 2", checks)
            if srv5.apps["aaaa-1111"]["name"] != FX_DECL["cc-kg-agent"]["display_name"]:
                raise AssertionError("存在的那半应被收敛")

            # ── N⑤ 非 agent-chat → BLOCKED(2) ──
            srv6 = FakeAppServer(FX_APPS)
            srv6.apps["bbbb-2222"]["mode"] = "chat"
            _run(cmd_check, _argv(binding), srv6, EXIT_BLOCKED, "N⑤ 模式非 agent-chat → BLOCKED", checks,
                 must_contain="agent-chat")
            srv7 = FakeAppServer(FX_APPS)
            srv7.apps["bbbb-2222"]["mode"] = "chat"
            _run(cmd_apply, _argv(binding), srv7, EXIT_BLOCKED, "N⑤ b 模式非 agent-chat → apply BLOCKED", checks)
            if any(c[0] in ("POST", "PUT") and "bbbb-2222" in c[1] for c in srv7.calls):
                raise AssertionError("被 BLOCK 的 App 不得被写")

            # ── N⑦ 声明缺 display_name → BLOCKED(2) ──
            missing = {k: {kk: vv for kk, vv in v.items() if kk != "display_name"} for k, v in FX_DECL.items()}
            miss_binding = _write_decl(td, missing, "missing.yaml")
            _run(cmd_check, _argv(miss_binding), FakeAppServer(FX_APPS), EXIT_BLOCKED,
                 "N⑦ 声明缺 display_name → BLOCKED", checks, must_contain="缺 display_name")

            # ── N⑧ 只改名字 ⇒ 只发 POST name；只改描述 ⇒ 只发 PUT ──
            srv8 = FakeAppServer(FX_APPS)
            for uuid, key in (("aaaa-1111", "cc-kg-agent"), ("bbbb-2222", "cc-kgt-agent")):
                srv8.apps[uuid]["description"] = FX_DECL[key]["display_description"]
            _run(cmd_apply, _argv(binding), srv8, EXIT_PASS, "N⑧ 仅名字漂移 → apply", checks)
            if [c for c in srv8.calls if c[0] == "PUT"]:
                raise AssertionError("仅名字漂移时不得发 PUT（PUT 会重写描述/图标面）")
            if len([c for c in srv8.calls if c[0] == "POST"]) != 2:
                raise AssertionError("仅名字漂移时应发 2 次 POST name")
            srv9 = FakeAppServer(FX_APPS)
            for uuid, key in (("aaaa-1111", "cc-kg-agent"), ("bbbb-2222", "cc-kgt-agent")):
                srv9.apps[uuid]["name"] = FX_DECL[key]["display_name"]
            _run(cmd_apply, _argv(binding), srv9, EXIT_PASS, "N⑧ b 仅描述漂移 → apply", checks)
            if [c for c in srv9.calls if c[0] == "POST"]:
                raise AssertionError("仅描述漂移时不得发 POST name")
            if len([c for c in srv9.calls if c[0] == "PUT"]) != 2:
                raise AssertionError("仅描述漂移时应发 2 次 PUT")

            # ── N⑨ PUT 后回读不一致（假服务器故意不持久化描述）⇒ FAIL(1) ──
            _run(cmd_apply, _argv(binding), FakeAppServer(FX_APPS, drop_description_on_put=True),
                 EXIT_FAIL, "N⑨ 写入后回读不一致 → FAIL", checks, must_contain="FAIL: ")

            # ── P2 正控：平台 `or 0` 强制转换 ⇒ 归一化后 PASS（证明归一化生效）──
            #   真机读回 null ⇒ 我方原值回填 null ⇒ 平台落成 0；旧口径会误判 FAIL。
            srv_null = FakeAppServer(FX_APPS_NULL_LIMIT)
            out_p2 = _run(cmd_apply, _argv(binding), srv_null, EXIT_PASS,
                          "P2 正控：回填 null ⇒ 平台落 0（`or 0`）⇒ 归一 PASS", checks,
                          must_contain="RESULT: PASS")
            if "COERCED" not in out_p2:
                raise AssertionError("P2 应打印 COERCED 归一记录（含原始两值）")
            if "before=null after=0" not in out_p2:
                raise AssertionError("P2 的 COERCED 记录必须保留 before=null / after=0 原始两值")
            if srv_null.apps["aaaa-1111"]["max_active_requests"] != 0:
                raise AssertionError("假服务器未复刻 `max_active_requests or 0` 语义")
            puts_null = [c for c in srv_null.calls if c[0] == "PUT"]
            if not puts_null or any(c[2].get("max_active_requests") is not None for c in puts_null):
                raise AssertionError("P2 PUT body 应原值回填 null（真机读回值），实际 %s" % puts_null)
            checks.append("P2 假服务器已复刻 `or 0`：PUT body 回填 null ⇒ 真机回读 0；图标四件套仍未变")

            # ── N⑫ 负控 A：图标面仍逐字严格 —— 回读 icon_background 被改 ⇒ FAIL(1) ──
            out_n12 = _run(cmd_apply, _argv(binding),
                           FakeAppServer(FX_APPS, put_hook=lambda app, body: app.__setitem__("icon_background", "#000000")),
                           EXIT_FAIL, "N⑫ 负控A：icon_background 被改 → FAIL", checks,
                           must_contain="icon_background")
            if "RESULT: FAIL" not in out_n12:
                raise AssertionError("N⑫ 应判 RESULT: FAIL")
            if "COERCED" in out_n12:
                raise AssertionError("N⑫ 是图标面真实改动，不得被当成平台归一放行")

            # ── N⑫ b 负控 A2：icon 被清空 ⇒ FAIL(1) ──
            _run(cmd_apply, _argv(binding),
                 FakeAppServer(FX_APPS, put_hook=lambda app, body: app.__setitem__("icon", "")),
                 EXIT_FAIL, "N⑫ b 负控A2：icon 被清空 → FAIL", checks, must_contain="icon before=")

            # ── N⑬ 负控 B：归一化不得放过真实改动 —— 真机 0 被改成 5 ⇒ FAIL(1) ──
            out_n13 = _run(cmd_apply, _argv(binding),
                           FakeAppServer(FX_APPS_ZERO_LIMIT,
                                         put_hook=lambda app, body: app.__setitem__("max_active_requests", 5)),
                           EXIT_FAIL, "N⑬ 负控B：max_active_requests 0 → 5（真实改动）→ FAIL", checks,
                           must_contain="max_active_requests")
            if "RESULT: FAIL" not in out_n13:
                raise AssertionError("N⑬ 应判 RESULT: FAIL")
            if "COERCED" in out_n13:
                raise AssertionError("N⑬ 的 0→5 不得被归一化放行")
            if "before=0 after=5" not in out_n13:
                raise AssertionError("N⑬ FAIL 明细必须保留 before=0 / after=5 原始两值")

            # ── N⑬ b 负控 B2：null 直接落到 5（跨等价类）⇒ FAIL(1) ──
            _run(cmd_apply, _argv(binding),
                 FakeAppServer(FX_APPS_NULL_LIMIT,
                               put_hook=lambda app, body: app.__setitem__("max_active_requests", 5)),
                 EXIT_FAIL, "N⑬ b 负控B2：null → 5（跨等价类）→ FAIL", checks,
                 must_contain="max_active_requests")

            # ── N⑩ 参数错：--apps 未知名 → exit 3 ──
            _run(cmd_check, _argv(binding, apps="no-such-app"), FakeAppServer(FX_APPS), EXIT_USAGE,
                 "N⑩ --apps 未知名 → exit 3", checks, must_contain="未知名")
            _run(cmd_check, _argv(str(Path(td) / "nope.yaml")), FakeAppServer(FX_APPS), EXIT_USAGE,
                 "N⑩ b binding 不存在 → exit 3", checks, must_contain="不存在")

            # ── N⑪ 不打印口令/token 字面量（R-47）──
            secret = "S3cr3t-Pa55w0rd-XYZ"
            srv10 = FakeAppServer(FX_APPS)
            srv10.headers = {"Authorization": secret}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_check(_argv(binding), srv10)
            if secret in buf.getvalue():
                raise AssertionError("stdout 出现口令/token 字面量（违反 R-47）")
            checks.append("N⑪ stdout 不含口令/token 字面量（只许 len/sha 前缀）")
    except AssertionError as exc:
        print("SELFTEST FAIL: %s" % exc)
        return EXIT_FAIL
    for c in checks:
        print("  ok  %s" % c)
    print("SELFTEST PASS（%d 组）" % len(checks))
    return EXIT_PASS


# ---------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description="W20 · App 元数据（name/description）声明↔真机收敛器")
    ap.add_argument("--binding", default=str(BINDING_PATH))
    ap.add_argument("--apps", help="可选：逗号分隔的 binding.apps 键（默认全部）")
    ap.add_argument("--check", action="store_true", help="只读比对（0 收敛 / 1 漂移 / 2 BLOCKED / 3 参数环境错）")
    ap.add_argument("--apply", action="store_true", help="幂等收敛（改名 POST name；改描述 PUT + read-modify-write；图标四件套逐字回读，仅 max_active_requests 归一 null↔0）")
    ap.add_argument("--selftest", action="store_true", help="离线假 transport 自检（含图标面/归一化正负控）")
    return ap


def main(argv=None) -> int:
    utf8_stdout()
    args = build_parser().parse_args(argv)
    picks = [bool(args.check), bool(args.apply), bool(args.selftest)]
    if sum(picks) != 1:
        print("BLOCKED: 必须且只能指定 --check / --apply / --selftest 之一")
        return EXIT_USAGE
    try:
        if args.selftest:
            return cmd_selftest(args)
        if args.check:
            return cmd_check(args)
        return cmd_apply(args)
    except UsageError as exc:
        print("USAGE: %s" % exc)
        return EXIT_USAGE
    except EnvError as exc:
        print("ENV: %s" % exc)
        return EXIT_USAGE
    except Blocked as exc:
        print("BLOCKED: %s" % exc)
        return EXIT_BLOCKED
    except common.DifyError as exc:
        print("ENV: %s" % exc)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
