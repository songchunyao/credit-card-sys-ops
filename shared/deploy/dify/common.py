#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4.2 / W1 · Dify Console API 公共层（登录、读、写、回读、指纹与小工具）。

设计约束
--------
1. **凭据不落盘、不打印。** 本模块只从环境变量或 .env 文件读取
   ``DIFY_BASE_URL`` / ``DIFY_EMAIL`` / ``DIFY_PASSWORD``，任何返回值、日志、
   异常文本里都不含口令明文；只允许打印长度或是否存在的布尔量。
2. **传输可注入。** ``Console`` 接受任意 ``transport`` 对象（只要实现
   ``request(method, url, json=..., timeout=...)`` 且返回带 ``status_code`` /
   ``text`` 的对象），离线 ``--selftest`` 因此不需要网络。
3. 本模块不做任何工具名 / 端口 / URL 的业务判断，那是 binding.yaml 的职责。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

try:  # pragma: no cover - 环境相关
    import requests
except Exception:  # pragma: no cover
    requests = None

HERE = Path(__file__).resolve().parent
# HERE = <repo>/shared/deploy/dify  ->  parents[2] = <repo>
REPO_ROOT = HERE.parents[2]

ENV_BASE = "DIFY_BASE_URL"
ENV_EMAIL = "DIFY_EMAIL"
ENV_PASSWORD = "DIFY_PASSWORD"
ENV_PREFIX = "DIFY_API_PREFIX"
ENV_FILE_OVERRIDE = "M4_DIFY_ENV_FILE"


class DifyError(RuntimeError):
    """Dify 侧不可用 / 响应不符合契约。"""


# ---------------------------------------------------------------------------
# .env 装载（只 setdefault，绝不覆盖调用者已有的环境变量）
# ---------------------------------------------------------------------------
def env_file_candidates(repo_root: Path | None = None) -> list[Path]:
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    cands: list[Path] = []
    override = os.environ.get(ENV_FILE_OVERRIDE)
    if override:
        cands.append(Path(override))
    cands.append(root / ".env")
    cands.append(root.parent / "upstream-ops-agent" / ".env")
    cands.append(HERE / ".env")
    out: list[Path] = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def load_env_file(path: Path | str) -> int:
    """把 KEY=VALUE 行 setdefault 进 os.environ。返回装载的键数。不打印值。"""
    p = Path(path)
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def load_env(repo_root: Path | None = None) -> dict:
    """装载候选 .env（只补缺失键）。返回装载来源的**路径名**与键数，不含值。"""
    loaded = []
    for c in env_file_candidates(repo_root):
        n = load_env_file(c)
        if n:
            loaded.append({"path": str(c), "keys_set": n})
    return {"loaded": loaded}


def env(name: str, required: bool = True, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        if required:
            raise DifyError("缺少环境变量 %s（口令一律不从命令行传入）" % name)
        return default
    return val


# ---------------------------------------------------------------------------
# 指纹 / 文本小工具（报告要用，且 LF-only / 无 BOM 要可核）
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def file_facts(path: Path | str) -> dict:
    b = Path(path).read_bytes()
    return {
        "path": str(path),
        "bytes": len(b),
        "lf_lines": b.count(b"\n"),
        "crlf": b.count(b"\r\n"),
        "bom": b.startswith(b"\xef\xbb\xbf"),
        "sha256": sha256_bytes(b),
    }


def stable_json(obj) -> str:
    """规范化 JSON：排序键、无多余空白。用于「规范化后逐字比较」。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def dump_pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CookieJar（离线自检用；requests 可用时用 requests.Session）
# ---------------------------------------------------------------------------
class CookieJar:
    def __init__(self):
        self._c: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self._c[name] = value

    def get(self, name: str, default: str | None = None):
        return self._c.get(name, default)

    def as_dict(self) -> dict:
        return dict(self._c)


class _Response:
    """把任意 transport 的返回统一成 (status_code, text, headers)。"""

    __slots__ = ("status_code", "text", "headers", "json_obj")

    def __init__(self, status_code: int, text: str = "", headers=None, json_obj=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.json_obj = json_obj


class Transport:
    """默认传输：requests.Session + 手工 cookie 注入。"""

    def __init__(self, session=None):
        if session is None:
            if requests is None:
                raise DifyError("requests 不可用，且未注入 transport")
            session = requests.Session()
        self.session = session
        self.cookies = CookieJar()

    def request(self, method: str, url: str, json_body=None, headers=None, timeout=30):
        h = dict(headers or {})
        if self.cookies.as_dict():
            h["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.as_dict().items())
        kw = {"timeout": timeout, "headers": h, "allow_redirects": False}
        if json_body is not None:
            kw["json"] = json_body
        r = self.session.request(method, url, **kw)
        try:
            for k, v in (r.cookies or {}).items():
                self.cookies.set(k, v)
        except Exception:
            pass
        return _Response(r.status_code, r.text, dict(r.headers or {}))


class Console:
    """Dify Console API 客户端。所有方法都不打印凭据。"""

    def __init__(self, base: str, transport=None, prefix: str = "console/api", timeout: int = 30):
        self.base = base.rstrip("/")
        self.prefix = prefix.strip("/")
        self.timeout = timeout
        self.transport = transport if transport is not None else Transport()
        self.headers: dict[str, str] = {"Content-Type": "application/json"}
        self.email_fingerprint: dict = {}

    # -- URL ---------------------------------------------------------------
    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    # -- 登录 --------------------------------------------------------------
    def login(self, email: str, password: str) -> None:
        body = {
            "email": email,
            "password": base64.b64encode(password.encode("utf-8")).decode("ascii"),
            "language": "zh-Hans",
            "remember_me": True,
        }
        r = self.transport.request(
            "POST", self.url("/%s/login" % self.prefix), json_body=body, timeout=self.timeout
        )
        if r.status_code != 200:
            # 绝不回显 body（可能含凭据痕迹）以外的敏感值；body 由调用者决定是否打印
            raise DifyError("登录失败 HTTP %s（base=%s）" % (r.status_code, self.base))
        jar = getattr(self.transport, "cookies", None)
        csrf = jar.get("csrf_token") if jar is not None else None
        token = jar.get("access_token") if jar is not None else None
        if not csrf or not token:
            raise DifyError("登录响应缺少 csrf_token / access_token cookie")
        self.headers["X-CSRF-TOKEN"] = csrf
        self.headers["Authorization"] = "Bearer %s" % token
        self.email_fingerprint = {"email_len": len(email), "password_len": len(password)}

    # -- 通用请求 ----------------------------------------------------------
    def request(self, method: str, path: str, json_body=None, expect=(200,)):
        r = self.transport.request(
            method.upper(),
            self.url(path),
            json_body=json_body,
            headers=self.headers,
            timeout=self.timeout,
        )
        if expect and r.status_code not in expect:
            raise DifyError("HTTP %s %s %s（期望 %s）" % (method.upper(), path, r.status_code, list(expect)))
        return r

    def get_json(self, path: str, expect=(200,)):
        r = self.request("GET", path, None, expect)
        try:
            return json.loads(r.text)
        except Exception as exc:
            raise DifyError("响应不是 JSON：%s %s（%s）" % (path, r.status_code, exc))

    def post_json(self, path: str, payload, expect=(200, 201)):
        r = self.request("POST", path, payload, expect)
        if not r.text.strip():
            return {}
        try:
            return json.loads(r.text)
        except Exception:
            return {"_raw": r.text}

    # -- 业务只读 ----------------------------------------------------------
    def list_api_providers(self) -> dict:
        """返回 {provider_name: provider_id}。"""
        data = self.get_json("/%s/workspaces/current/tool-providers?type=api" % self.prefix)
        out = {}
        seq = data if isinstance(data, list) else data.get("data", data.get("providers", []))
        for p in seq or []:
            if isinstance(p, dict) and p.get("name"):
                out[p["name"]] = p.get("id")
        return out

    def get_provider(self, name: str) -> dict:
        return self.get_json(
            "/%s/workspaces/current/tool-provider/api/get?provider=%s" % (self.prefix, name)
        )

    def add_provider(self, payload: dict) -> dict:
        return self.post_json("/%s/workspaces/current/tool-provider/api/add" % self.prefix, payload)

    def update_provider(self, payload: dict) -> dict:
        return self.post_json("/%s/workspaces/current/tool-provider/api/update" % self.prefix, payload)


def console_client(repo_root: Path | None = None, transport=None) -> Console:
    """装载 .env 并建立一个已登录的 Console。异常文本不含凭据。"""
    load_env(repo_root)
    base = env(ENV_BASE, required=True)
    email = env(ENV_EMAIL, required=True)
    password = env(ENV_PASSWORD, required=True)
    prefix = env(ENV_PREFIX, required=False, default="console/api")
    c = Console(base, transport=transport, prefix=prefix)
    c.login(email, password)
    return c


def load_binding(path: Path | str | None = None):
    try:
        import yaml
    except Exception as exc:  # pragma: no cover
        raise DifyError("需要 PyYAML：%s" % exc)
    p = Path(path) if path else (HERE / "binding.yaml")
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)
