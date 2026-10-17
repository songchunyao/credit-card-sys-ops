#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M4.2 / P2 —— 把 Neo4j 凭据落库到已装 Dify 插件 provider `neo4j_query 0.2.0`。

为什么需要它：R-32 裁定方案3 复用「已装插件 neo4j_query」作为 KG 读取通道；插件侧
租户凭据（NEO4J_URL / NEO4J_USERNAME / NEO4J_PASSWORD）必须写进 Dify 租户，否则 W3 的
`cc-kgt-free-agent` 调不到该工具。本脚本是该写入动作的唯一入口（幂等 + 读改写 + 回读 + 取证）。

实例契约（shared/deploy/targets.yaml:28-33，必须同一实例，否则方案3 会在另一张图上评测）：
    bolt_uri = 取 env `CC_NEO4J_URI`（未设 ⇒ `bolt://192.0.2.11:7688`，现网部署主机）
    ★ R-58：host = 现网部署主机（真机注册态实测值）；.84 是 R-56 事故的旧主机，已彻底移除。
      ★ 该值**写进 Dify 凭据**、由**容器内**插件求值 ⇒ 绝不落 127.0.0.1（那是 Dify 自己）。
    password_env = CC_NEO4J_PASSWORD          source = 生产机 /opt/upstream-agent/.env 的 NEO4J_AUTH

────────────────────────── 源码级路由证据（dify-api-1 容器，Dify 1.17.0）──────────────────────────
controllers/console/workspace/tool_providers.py:582
    @console_ns.route("/workspaces/current/tool-provider/builtin/<path:provider>/add")
    class ToolBuiltinProviderAddApi -> BuiltinToolManageService.add_builtin_tool_provider(
        user_id, tenant_id, provider, credentials=req_data.credentials, name=req_data.name,
        api_type=CredentialType.of(req_data.type), visibility=req_data.visibility)
controllers/console/workspace/tool_providers.py:613
    @console_ns.route(".../builtin/<path:provider>/update")
    class ToolBuiltinProviderUpdateApi -> update_builtin_tool_provider(credential_id=req_data.credential_id,
        credentials=req_data.credentials, name=req_data.name or "")
controllers/console/workspace/tool_providers.py:641
    @console_ns.route(".../builtin/<path:provider>/credentials")
    class ToolBuiltinProviderGetCredentialsApi —— 只有 `def get`，这是**读回**路由，不是写入路由
controllers/console/workspace/tool_providers.py:112  class BuiltinToolAddPayload
    credentials: dict[str, Any] / name: str|None(<=30) / type: CredentialType / visibility: str|None
controllers/console/workspace/tool_providers.py:119  class BuiltinToolUpdatePayload
    credential_id: str / credentials: dict[str, Any]|None / name: str|None(<=30)
controllers/console/workspace/tool_providers.py:418  class ToolProviderCredentialListResponse(RootModel[list[ToolProviderCredentialApiEntity]])
core/plugin/entities/plugin_daemon.py:273  class CredentialType(enum.StrEnum)
    API_KEY = "api-key" / OAUTH2 = "oauth2" / UNAUTHORIZED = "unauthorized"
core/plugin/entities/plugin_daemon.py:290  def is_validate_allowed(self): return self == CredentialType.API_KEY
services/tools/builtin_tools_manage_service.py:219  add_builtin_tool_provider
services/tools/builtin_tools_manage_service.py:346  get_builtin_tool_provider_credentials
services/tools/builtin_tools_manage_service.py:419
    decrypt_credential = encrypter.mask_plugin_credentials(encrypter.decrypt(provider.credentials))
    —— 结论：GET 回读必然掩码，因此「回读相等」只能作为弱证据，不能证明口令一致。

──────────────────────────────────── 纪律 / 边界 ────────────────────────────────────
* 口令只存在于进程内存：不打印、不落盘、不进报告；报告只写「已取得」+ 账号名 + 来源路径。
* 只走 Console API 与生产机只读 SSH；不裸插数据库；不增删 provider/App/插件。
* 账号 `neo4j` 与 shared/model/kgt-tools.yaml:84 的 `read_only_role_required: true` 存在偏离，
  本轮如实登记，待用户提供只读账号后替换（脚本不改契约、不改文档）。
* 「凭据已写入」≠「功能已验证」：本脚本能证明的最强功能面是 POST /add 触发的
  provider_controller.validate_credentials（插件侧连接性校验，见 builtin_tools_manage_service.py
  的 add 分支 `if CredentialType.of(api_type).is_validate_allowed(): provider_controller.validate_credentials(...)`）；
  真正的 Cypher 端到端可达性由 W3 的 cc-kgt-free-agent 冒烟负责。

用法：
    python shared\\deploy\\dify\\plugin_bind.py --check
    python shared\\deploy\\dify\\plugin_bind.py --apply --evidence-dir tmp\\m4_p2_evidence
    python shared\\deploy\\dify\\plugin_bind.py --selftest
退出码：0 成功/已收敛；1 写入后未达预期；2 参数或校验拒绝；3 未收敛；4 工具面缺失；5 口令来源不可用。
"""
import argparse
import base64
import http.cookiejar
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ────────────────────────────────── 常量（契约） ──────────────────────────────────
PROVIDER = "nikola_milosevic/neo4j_query/neo4j_query"          # 工具面 provider id（含插件内 provider 名）
PLUGIN_ID = "nikola_milosevic/neo4j_query"
PLUGIN_UNIQUE_IDENTIFIER = (
    "nikola_milosevic/neo4j_query:0.2.0@99ca3d403ae707f83e81992b68354a8aabf5ad5bda1b0e02e5aaaeb763ecea38"
)
TOOL_NAME = "neo4j_query"
CRED_NAME = "cc-kgt-neo4j"                                     # 本仓库写入的凭据条目名（幂等锚点）
CREDENTIAL_TYPE = "api-key"                                    # CredentialType.API_KEY.value
CREDENTIAL_VISIBILITY = "all_team_members"

# ★ R-58：主机 = 现网部署主机（真机注册态实测值）；.84 是 R-56 事故的旧主机，已彻底移除。可用 env CC_NEO4J_URI 覆盖。
#   ★ 为什么不能落 127.0.0.1：本值写进 Dify 凭据、由**容器内**插件求值（回环 = Dify 自己）。
EXPECTED_BOLT = os.environ.get("CC_NEO4J_URI", "bolt://192.0.2.11:7688")   # 口径来源 targets.yaml:28（构建期地址，见 install.sh 的叠加改写）
EXPECTED_USER = "neo4j"                                        # targets.yaml:32
CRED_KEYS = ("NEO4J_URL", "NEO4J_USERNAME", "NEO4J_PASSWORD")

PLACEHOLDER_PASSWORDS = frozenset({
    "", "CHANGE_ME", "changeme", "CHANGEME", "change_me", "<password>", "<PASSWORD>",
    "PLACEHOLDER", "placeholder", "REPLACE_ME", "your_password", "xxx", "TODO",
})

DIFY_ENV_CANDIDATES = (
    r"<workspace>\upstream-ops-agent\.env",
)
PROD_HOST = os.environ.get("CC_DEPLOY_HOST", "127.0.0.1")   # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
PROD_SSH_USER = "root"
PROD_ENV_PATH = "/opt/upstream-agent/.env"
PROD_NEO4J_AUTH_KEY = "NEO4J_AUTH"
SSH_ENV_PATH = r"<workspace>\upstream-ops-agent\.env"
SSH_PASSWORD_ENV_KEY = "GAUSSDB_SSH_PASSWORD"

API = "/console/api"
ROUTE_PROVIDER = API + "/workspaces/current/tool-provider/builtin/%s"
ROUTE_ADD = ROUTE_PROVIDER + "/add"
ROUTE_UPDATE = ROUTE_PROVIDER + "/update"
ROUTE_CREDENTIALS = ROUTE_PROVIDER + "/credentials"
ROUTE_TOOLS = ROUTE_PROVIDER + "/tools"
ROUTE_LIST = API + "/workspaces/current/tool-providers?type=builtin"


class Refused(Exception):
    """校验/负控拒绝：一律不带口令内容。"""


# ────────────────────────────────── 输入校验（负控对象） ──────────────────────────────────
def validate_inputs(provider, url, username, password, key_present=True):
    """把「能拒绝的都必须拒绝」集中在这里，--selftest 直接对它做负控。

    key_present=False 用来模拟「提交体里缺 NEO4J_PASSWORD 键」。
    """
    if provider != PROVIDER:
        raise Refused("provider 不是目标插件 provider：%r" % (provider,))
    if not key_present:
        raise Refused("提交键缺失：NEO4J_PASSWORD")
    if not isinstance(url, str) or not url.strip():
        raise Refused("NEO4J_URL 为空")
    got = url.strip().rstrip("/")
    if got != EXPECTED_BOLT:
        raise Refused(
            "NEO4J_URL 未指向目标实例：期望 %s，得到 %s（换实例会导致方案3 在另一张图上被评测）"
            % (EXPECTED_BOLT, got)
        )
    if (username or "").strip() != EXPECTED_USER:
        raise Refused("NEO4J_USERNAME 不是契约账号：期望 %s" % EXPECTED_USER)
    if password is None or password == "":
        raise Refused("NEO4J_PASSWORD 为空")
    if password in PLACEHOLDER_PASSWORDS:
        raise Refused("NEO4J_PASSWORD 是占位值，拒绝猜测与写入")
    return {
        "NEO4J_URL": EXPECTED_BOLT,
        "NEO4J_USERNAME": EXPECTED_USER,
        "NEO4J_PASSWORD": password,
    }


def build_add_payload(creds):
    return {
        "credentials": dict(creds),
        "name": CRED_NAME,
        "type": CREDENTIAL_TYPE,
        "visibility": CREDENTIAL_VISIBILITY,
    }


def build_update_payload(creds, credential_id):
    return {"credential_id": credential_id, "credentials": dict(creds), "name": CRED_NAME}


# ────────────────────────────────── 掩码 / 取证安全 ──────────────────────────────────
def sanitize_credentials(raw, real_password):
    """回读的凭据面：NEO4J_URL/USERNAME 可原样留证；NEO4J_PASSWORD 只记录「是否掩码 + 掩码形态」。

    若服务端竟然明文回显了真实口令，则**绝不**把它写进证据，只登记这一事实。
    """
    out = {"_masked": False, "_mask_form": None, "_equal_to_submitted": None}
    if not isinstance(raw, dict):
        out["_raw_type"] = type(raw).__name__
        return out
    for k, v in raw.items():
        if k == "NEO4J_PASSWORD":
            if v == real_password:
                out["NEO4J_PASSWORD"] = "<REDACTED:server-echoed-plaintext>"
                out["_masked"] = False
                out["_equal_to_submitted"] = True
            else:
                form = ("empty" if v in ("", None) else
                        "asterisks" if isinstance(v, str) and set(v) <= {"*"} else
                        "len-preserving" if isinstance(v, str) and len(v) == len(real_password) else
                        "other")
                # ★ 绝不落地掩码字面量：Dify 的 len-preserving 掩码会**保留首尾各 2 个字符**，
                #   本轮实测形态 = 口令首 2 字 + 连续 13 个 '*' + 口令末 2 字（共 17 位，与提交口令等长），
                #   那 4 个字符仍是口令的一部分，所以连掩码值也不能写进证据。
                #   只登记「形态 + 长度 + 星号数 + 首尾各暴露几字符」这些结构性事实。
                head = last = None
                if isinstance(v, str) and form == "len-preserving" and len(v) >= 6:
                    head = last = 2
                out["NEO4J_PASSWORD"] = ("<masked:%s len=%s stars=%s reveals_first=%s reveals_last=%s>"
                                         % (form, len(v) if isinstance(v, str) else None,
                                            v.count("*") if isinstance(v, str) else None, head, last))
                out["_masked"] = True
                out["_mask_form"] = form
                out["_equal_to_submitted"] = False
        else:
            out[k] = v
    return out


def redact_credentials_block(obj):
    """只遮蔽「服务端回包里的凭据块」——不能整份遮蔽，否则把掩码形态等证据一起抹掉。"""
    if isinstance(obj, dict):
        return {k: ("<CRED-BLOCK-REDACTED:%s>" % ",".join(sorted(v.keys()))
                    if k == "credentials" and isinstance(v, dict) else redact_credentials_block(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_credentials_block(v) for v in obj]
    return obj


def slots(snap):
    """pre/post 可直接 diff 的紧凑视图。"""
    return {
        "count": snap.get("count"),
        "credential_ids": [c.get("id") for c in snap.get("credentials", [])],
        "names": [c.get("name") for c in snap.get("credentials", [])],
        "urls": [c.get("sanitized", {}).get("NEO4J_URL") for c in snap.get("credentials", [])],
        "users": [c.get("sanitized", {}).get("NEO4J_USERNAME") for c in snap.get("credentials", [])],
        "password_forms": [c.get("sanitized", {}).get("_mask_form") for c in snap.get("credentials", [])],
    }


# ────────────────────────────────── 本地环境 / SSH ──────────────────────────────────
def load_env_file(path):
    env = {}
    if not os.path.exists(path):
        return env
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def dify_env():
    """Dify Console 凭据 → ``(env, 来源标签)``。

    ★ R-59（2026-10-13）：**环境变量优先**（与 ``shared/deploy/dify/common.py::load_env``
      同一口径）。原实现**只**读 ``DIFY_ENV_CANDIDATES`` —— 那是**仓库外**的
      ``..\\upstream-ops-agent\\.env``，里面写着**旧主机** ``http://192.0.2.10``
      ⇒ ① 本工具**无法**指向现网(.83)；② 在现网机上跑它会**静默改到旧部署**（本轮实测踩到）。
      改为：只要 ``DIFY_BASE_URL`` / ``DIFY_EMAIL`` / ``DIFY_PASSWORD`` 三个键在**进程环境**里齐备，
      就用环境里的（来源标签 ``<环境变量>``），否则才回落到候选文件。
    """
    keys = ("DIFY_BASE_URL", "DIFY_EMAIL", "DIFY_PASSWORD")
    if all(os.environ.get(k) for k in keys):
        picked = {k: v for k, v in os.environ.items() if k.startswith("DIFY_")}
        return picked, "<环境变量（env-first，R-59）>"
    for path in DIFY_ENV_CANDIDATES:
        env = load_env_file(path)
        if env.get("DIFY_BASE_URL") and env.get("DIFY_EMAIL") and env.get("DIFY_PASSWORD"):
            return env, path
    raise Refused("找不到可用的 Dify Console 环境文件（候选：%s）；"
                  "也可直接用环境变量 DIFY_BASE_URL / DIFY_EMAIL / DIFY_PASSWORD 指定"
                  % ", ".join(DIFY_ENV_CANDIDATES))


def fetch_password_from_remote(host=PROD_HOST, user=PROD_SSH_USER, env_path=PROD_ENV_PATH):
    """从生产机只读读取现成口令。返回 (password, source)；口令不进日志、不落盘。

    优先密钥（~/.ssh/id_rsa、id_ed25519），否则用 .env 的 GAUSSDB_SSH_PASSWORD。
    若主路径无 NEO4J_AUTH，再在 /opt/*/.env 里定位（命中即用并记录路径）。
    """
    try:
        import paramiko
    except ImportError:
        raise Refused("缺少 paramiko，无法读取生产机口令（可改用 CC_NEO4J_PASSWORD 环境变量）")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    errs = []
    authed = None
    for key in (os.path.expanduser(r"~\.ssh\id_rsa"), os.path.expanduser(r"~\.ssh\id_ed25519")):
        if not os.path.exists(key):
            continue
        try:
            client.connect(host, port=22, username=user, key_filename=key,
                           timeout=10, banner_timeout=20, auth_timeout=20)
            authed = "key:%s" % os.path.basename(key)
            break
        except Exception as exc:  # noqa: BLE001
            errs.append("%s -> %s" % (os.path.basename(key), exc))
    if authed is None:
        pw = load_env_file(SSH_ENV_PATH).get(SSH_PASSWORD_ENV_KEY)
        if pw:
            try:
                client.connect(host, port=22, username=user, password=pw,
                               timeout=10, banner_timeout=20, auth_timeout=20)
                authed = "password:.env"
            except Exception as exc:  # noqa: BLE001
                errs.append("password -> %s" % exc)
    if authed is None:
        raise Refused("SSH 登录失败：%s" % " | ".join(errs))

    def run(cmd):
        _in, out, err = client.exec_command(cmd, timeout=60)
        body = out.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        return code, body

    try:
        key = PROD_NEO4J_AUTH_KEY
        code, body = run("grep -m1 -h %s %s 2>/dev/null" % ("^" + key + "=", env_path))
        source = env_path
        if code != 0 or key + "=" not in body:
            code, listing = run("grep -l -h %s /opt/*/.env 2>/dev/null | head -5" % ("^" + key + "="))
            found = [ln.strip() for ln in listing.splitlines() if ln.strip()]
            if not found:
                raise Refused("生产机 %s 与 /opt/*/.env 均未找到 %s，停手待人工" % (env_path, key))
            source = found[0]
            code, body = run("grep -m1 -h %s %s" % ("^" + key + "=", source))
            if code != 0 or key + "=" not in body:
                raise Refused("定位到 %s 但读不到 %s，停手待人工" % (source, key))
    finally:
        client.close()

    line = body.strip().splitlines()[0] if body.strip() else ""
    value = line.split("=", 1)[1].strip().strip('"').strip("'")
    if "/" not in value:
        raise Refused("%s 不是 user/password 形态（%s），停手待人工" % (PROD_NEO4J_AUTH_KEY, source))
    auth_user, password = value.split("/", 1)
    password = password.strip().strip('"').strip("'")
    return password, {
        "source_path": source,
        "source_key": PROD_NEO4J_AUTH_KEY,
        "auth_user": auth_user,
        "password_len": len(password),
        "ssh_auth": authed,
        "host": host,
    }


def resolve_password(args):
    """口令来源：CC_NEO4J_PASSWORD 环境变量 > 生产机 /opt/.../.env（只读 SSH）。"""
    if args.password_env:
        pw = os.environ.get(args.password_env)
        if pw:
            return pw, {"source_path": "env:%s" % args.password_env, "source_key": args.password_env,
                        "auth_user": EXPECTED_USER, "password_len": len(pw),
                        "ssh_auth": None, "host": None}
    env_pw = os.environ.get("CC_NEO4J_PASSWORD")
    if env_pw:
        return env_pw, {"source_path": "env:CC_NEO4J_PASSWORD", "source_key": "CC_NEO4J_PASSWORD",
                        "auth_user": EXPECTED_USER, "password_len": len(env_pw),
                        "ssh_auth": None, "host": None}
    return fetch_password_from_remote()


# ────────────────────────────────── Console 客户端 ──────────────────────────────────
class Console:
    def __init__(self, base, timeout=30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.token = None

    def headers(self):
        h = {"Content-Type": "application/json"}
        c = {ck.name: ck.value for ck in self.cj}
        if c.get("csrf_token"):
            h["X-CSRF-TOKEN"] = c["csrf_token"]
        auth = self.token or c.get("access_token")
        if auth:
            h["Authorization"] = "Bearer " + auth
        return h

    def call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers())
        try:
            with self.op.open(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except Exception:  # noqa: BLE001
                return e.code, {"_raw": body[:300]}
        except Exception as e:  # noqa: BLE001
            return 0, {"_err": "%s: %s" % (type(e).__name__, str(e)[:200])}

    def get(self, p):
        return self.call("GET", p)

    def post(self, p, payload):
        return self.call("POST", p, payload)

    def login(self, email, password):
        st, _ = self.call("POST", API + "/login", {
            "email": email, "password": base64.b64encode(password.encode()).decode(),
            "language": "zh-Hans", "remember_me": True})
        if st == 200:
            self.token = {ck.name: ck.value for ck in self.cj}.get("access_token")
            return True, st
        return False, st


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect_console():
    env, path = dify_env()
    c = Console(env["DIFY_BASE_URL"])
    ok, st = c.login(env["DIFY_EMAIL"], env["DIFY_PASSWORD"])
    return c, ok, st, env["DIFY_BASE_URL"], path


def quoted_provider():
    return urllib.parse.quote(PROVIDER, safe="")


def read_tool_face(c):
    """工具面：该路由回的是 JSON 数组（已由 tmp\\_m4_neoplugin.json 实证），不是 {"tools": [...]}。"""
    st, d = c.get(ROUTE_TOOLS % quoted_provider())
    items = d if isinstance(d, list) else ((d.get("tools") if isinstance(d, dict) else None) or [])
    names, params = [], {}
    for item in items:
        if isinstance(item, dict):
            nm = item.get("name")
            names.append(nm)
            params[nm] = [p.get("name") for p in (item.get("parameters") or []) if isinstance(p, dict)]
    return st, names, params


def read_credentials(c, real_password):
    st, d = c.get(ROUTE_CREDENTIALS % quoted_provider())
    creds = []
    if isinstance(d, list):
        for item in d:
            item = item if isinstance(item, dict) else {}
            creds.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "provider": item.get("provider"),
                "credential_type": item.get("credential_type"),
                "is_default": item.get("is_default"),
                "visibility": item.get("visibility"),
                "created_by": item.get("created_by"),
                "sanitized": sanitize_credentials(item.get("credentials"), real_password),
            })
    return st, {"count": len(creds), "credentials": creds, "_raw_keys": sorted(d.keys()) if isinstance(d, dict) else None}


def judge(snap):
    """converged / missing / drift。"""
    for c in snap.get("credentials", []):
        s = c.get("sanitized", {})
        if c.get("name") == CRED_NAME or s.get("NEO4J_URL") == EXPECTED_BOLT:
            if s.get("NEO4J_URL") == EXPECTED_BOLT and s.get("NEO4J_USERNAME") == EXPECTED_USER \
                    and s.get("NEO4J_PASSWORD") not in (None, ""):
                return "converged", c
            return "drift", c
    return "missing", None


# ────────────────────────────────── 三种模式 ──────────────────────────────────
def cmd_selftest(_args):
    """负控：缺键 / URL 非目标实例 / 口令为空（及占位值）必须被拒；正控必须通过。"""
    good = {"provider": PROVIDER, "url": EXPECTED_BOLT, "username": EXPECTED_USER,
            "password": "not-a-real-secret-xyz"}
    cases = [
        ("neg: 缺 NEO4J_PASSWORD 键", dict(good, key_present=False), False),
        ("neg: URL 指向非目标主机", dict(good, url="bolt://10.0.0.9:7688"), False),
        ("neg: URL 用非目标端口(7687)", dict(good, url="bolt://192.0.2.1:7687"), False),
        ("neg: URL 走 http 端口(bug)", dict(good, url="http://192.0.2.1:7475"), False),
        ("neg: 口令为空", dict(good, password=""), False),
        ("neg: 口令为占位值 CHANGE_ME", dict(good, password="CHANGE_ME"), False),
        ("neg: 账号不是契约账号", dict(good, username="readonly"), False),
        ("neg: provider 不匹配", dict(good, provider="nikola_milosevic/other/other"), False),
        ("pos: 契约输入", good, True),
    ]
    bad = 0
    for title, kw, should_pass in cases:
        try:
            validate_inputs(kw["provider"], kw["url"], kw["username"], kw["password"],
                            key_present=kw.get("key_present", True))
            ok = should_pass
            detail = "accepted"
        except Refused as e:
            ok = not should_pass
            detail = "refused: %s" % e
        bad += 0 if ok else 1
        print("[%s] %s -> %s" % ("PASS" if ok else "FAIL", title, detail))
    # 提交体形态负控：不得把口令放进非 credentials 的顶层键
    p = build_add_payload({"NEO4J_URL": EXPECTED_BOLT, "NEO4J_USERNAME": EXPECTED_USER,
                           "NEO4J_PASSWORD": "x"})
    shape_ok = (set(p) == {"credentials", "name", "type", "visibility"}
                and set(p["credentials"]) == set(CRED_KEYS)
                and p["type"] == CREDENTIAL_TYPE)
    bad += 0 if shape_ok else 1
    print("[%s] shape: /add 提交体键集合 == {credentials,name,type,visibility} 且 credentials 恰为 3 键"
          % ("PASS" if shape_ok else "FAIL"))
    print("\nselftest: %d case(s) failed" % bad)
    return 0 if bad == 0 else 2


def cmd_check(args):
    report = {"mode": "check", "checked_at_utc": now_utc(), "provider": PROVIDER,
              "plugin_unique_identifier": PLUGIN_UNIQUE_IDENTIFIER}
    pw, src = resolve_password(args)
    report["password_source"] = src
    print("口令：已取得（来源 %s，账号 %s，长度 %d；值不打印）"
          % (src["source_path"], src["auth_user"], src["password_len"]))
    c, ok, st, base, env_path = connect_console()
    report["console"] = {"base": base, "login_status": st, "login_ok": ok, "env_file": env_path}
    print("console login: HTTP %s -> %s" % (st, "OK" if ok else "FAILED"))
    if not ok:
        return 3
    tst, names, tparams = read_tool_face(c)
    report["tool_face"] = {"status": tst, "tool_names": names, "tool_parameters": tparams}
    print("tool face: HTTP %s tools=%s" % (tst, names))
    if tst != 200 or TOOL_NAME not in names:
        report["verdict"] = "tool_face_missing"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 4
    cst, snap = read_credentials(c, pw)
    state, hit = judge(snap)
    report["credentials_face"] = {"status": cst, "count": snap["count"]}
    report["credential_slots"] = slots(snap)
    report["state"] = state
    report["mask_evidence"] = None if not snap["credentials"] else snap["credentials"][0]["sanitized"]
    print("credentials face: HTTP %s count=%s state=%s" % (cst, snap["count"], state))
    print("slots: %s" % json.dumps(report["credential_slots"], ensure_ascii=False))
    if args.evidence_dir:
        write_evidence(args.evidence_dir, "check.json", report, pw)
    return 0 if state == "converged" else 3


def cmd_apply(args):
    pw, src = resolve_password(args)
    if not pw:
        print("拒绝：口令来源不可用")
        return 5
    try:
        creds = validate_inputs(PROVIDER, EXPECTED_BOLT, EXPECTED_USER, pw)
    except Refused as e:
        print("拒绝写入：%s" % e)
        return 2

    report = {"mode": "apply", "applied_at_utc": now_utc(), "provider": PROVIDER,
              "plugin_id": PLUGIN_ID, "plugin_unique_identifier": PLUGIN_UNIQUE_IDENTIFIER,
              "password_source": src,
              "submitted_keys": sorted(creds.keys()),
              "submitted_values_masked": {k: ("***" if k == "NEO4J_PASSWORD" else v) for k, v in creds.items()},
              "idempotency_anchor": {"name": CRED_NAME, "type": CREDENTIAL_TYPE, "visibility": CREDENTIAL_VISIBILITY}}
    print("提交键：%s（NEO4J_PASSWORD 值掩码 ***，口令不落盘）" % report["submitted_keys"])

    c, ok, st, base, env_path = connect_console()
    report["console"] = {"base": base, "login_status": st, "login_ok": ok, "env_file": env_path}
    print("console login: HTTP %s -> %s" % (st, "OK" if ok else "FAILED"))
    if not ok:
        return 3

    tst, names, tparams = read_tool_face(c)
    report["tool_face"] = {"status": tst, "tool_names": names, "tool_parameters": tparams}
    if tst != 200 or TOOL_NAME not in names:
        print("工具面缺失（HTTP %s, tools=%s），拒绝写入" % (tst, names))
        return 4

    # ── 改前回读 ──
    cst, pre = read_credentials(c, pw)
    state, hit = judge(pre)
    report["pre"] = {"status": cst, "count": pre["count"], "slots": slots(pre), "state": state,
                     "mask_evidence": None if not pre["credentials"] else pre["credentials"][0]["sanitized"]}
    print("改前：HTTP %s count=%s state=%s" % (cst, pre["count"], state))
    print("改前 slots: %s" % json.dumps(report["pre"]["slots"], ensure_ascii=False))

    if state == "converged" and not args.force:
        report["action"] = "noop_already_converged"
        report["post"] = report["pre"]
        report["diff"] = "无变化（幂等命中：%s 已存在且 NEO4J_URL/NEO4J_USERNAME 与契约一致，口令面为掩码）" % CRED_NAME
        print("幂等命中：已收敛，未发起写入（--force 可强制重写）")
        if args.evidence_dir:
            write_evidence(args.evidence_dir, "apply.json", report, pw)
        return 0

    if hit is not None:
        cid = hit["id"]
        act = "update"
        _t0 = time.monotonic()
        rst, rsp = c.post(ROUTE_UPDATE % quoted_provider(), build_update_payload(creds, cid))
        report["action"] = {"op": "POST /update", "route": ROUTE_UPDATE % "<provider>",
                            "credential_id": cid, "request_keys": sorted(build_update_payload(creds, cid).keys())}
    else:
        act = "add"
        pl = build_add_payload(creds)
        _t0 = time.monotonic()
        rst, rsp = c.post(ROUTE_ADD % quoted_provider(), pl)
        report["action"] = {"op": "POST /add", "route": ROUTE_ADD % "<provider>",
                            "request_keys": sorted(pl.keys())}
    report["action"]["elapsed_s"] = round(time.monotonic() - _t0, 2)
    report["action"]["status"] = rst
    report["action"]["response"] = redact_credentials_block(rsp) if isinstance(rsp, dict) else rsp
    print("写入：%s -> HTTP %s elapsed=%ss %s" % (act, rst, report["action"]["elapsed_s"],
                                                 json.dumps(report["action"]["response"], ensure_ascii=False)[:400]))
    if rst != 200:
        report["verdict"] = "write_failed"
        if args.evidence_dir:
            write_evidence(args.evidence_dir, "apply.json", report, pw)
        return 1

    # ── 改后回读 ──
    pst, post = read_credentials(c, pw)
    pstate, phit = judge(post)
    report["post"] = {"status": pst, "count": post["count"], "slots": slots(post), "state": pstate,
                      "mask_evidence": None if not post["credentials"] else post["credentials"][0]["sanitized"]}
    print("改后：HTTP %s count=%s state=%s" % (pst, post["count"], pstate))
    print("改后 slots: %s" % json.dumps(report["post"]["slots"], ensure_ascii=False))

    # ── diff（读改写 + 回读 + diff）──
    pre_ids = set(report["pre"]["slots"]["credential_ids"] or [])
    post_ids = set(report["post"]["slots"]["credential_ids"] or [])
    report["diff"] = {
        "count": "%s -> %s" % (report["pre"]["count"], report["post"]["count"]),
        "added_credential_ids": sorted(post_ids - pre_ids),
        "removed_credential_ids": sorted(pre_ids - post_ids),
        "state": "%s -> %s" % (state, pstate),
        "url": "%s -> %s" % (report["pre"]["slots"]["urls"], report["post"]["slots"]["urls"]),
        "username": "%s -> %s" % (report["pre"]["slots"]["users"], report["post"]["slots"]["users"]),
        "password_form": "%s -> %s" % (report["pre"]["slots"]["password_forms"], report["post"]["slots"]["password_forms"]),
    }
    print("diff: %s" % json.dumps(report["diff"], ensure_ascii=False))

    report["verdict"] = "converged" if pstate == "converged" else "write_done_but_not_converged"
    report["honest_boundaries"] = [
        "回读被服务端掩码（builtin_tools_manage_service.py:417 encrypter.mask_plugin_credentials("
        "encrypter.decrypt(...))），因此「回读相等」不能证明口令逐字一致，只能证明凭据条目存在且非空。",
        "该掩码形态为 len-preserving 且**保留首尾各 2 个字符**，即掩码值本身是口令的一个片段；"
        "本脚本因此只登记形态/长度/星号数，绝不落地掩码字面量。",
        "账号 neo4j 与 shared/model/kgt-tools.yaml:84 read_only_role_required: true 偏离"
        "（该行注明只读账号「用户 m01643 确认可提供」），待用户提供只读账号后替换。",
        "「凭据已写入」≠「功能已验证」：Cypher 端到端可达性由 W3 cc-kgt-free-agent 冒烟负责；"
        "本轮「凭据可用」的弱证据 = 插件侧 validate_credentials 返回 200（见 tmp\\_m4_p2_report.md ③）。",
    ]
    if args.evidence_dir:
        write_evidence(args.evidence_dir, "apply.json", report, pw)
    return 0 if pstate == "converged" else 1


def write_evidence(directory, filename, report, secret=None):
    """落证据：UTF-8 无 BOM + LF。落盘前用真口令做硬扫描，命中即拒绝写（宁可失败也不泄漏）。

    写两份：目录下的稳定名（最新一次，便于引用）+ history/<时点>_<稳定名>（归档，防止后续
    运行覆盖掉「首次写入」这类不可复现的证据——本脚本首轮 POST /add 的 JSON 就是这样丢的）。
    """
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if secret and secret in text:
        raise Refused("证据文本里出现口令明文（%s），拒绝落盘" % filename)
    stamp = (report.get("applied_at_utc") or report.get("checked_at_utc") or now_utc())
    stamp = stamp.replace("-", "").replace(":", "").replace("T", "").replace("Z", "")
    paths = [os.path.join(directory, filename),
             os.path.join(directory, "history", "%s_%s" % (stamp, filename))]
    for path in paths:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("证据已写：%s" % path)


def main():
    ap = argparse.ArgumentParser(description="把 Neo4j 凭据落库到已装 Dify 插件 provider neo4j_query 0.2.0")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只读：核工具面 + 核凭据面 + 判定是否已收敛")
    g.add_argument("--apply", action="store_true", help="幂等写入：读改写 + 回读 + diff")
    g.add_argument("--selftest", action="store_true", help="负控自检，不联网")
    ap.add_argument("--force", action="store_true", help="已收敛时也强制 POST /update 重写")
    ap.add_argument("--evidence-dir", default=None, help="证据输出目录（UTF-8 无 BOM，LF）")
    ap.add_argument("--password-env", default=None, help="从该环境变量取口令（覆盖默认来源）")
    a = ap.parse_args()
    if a.selftest:
        return cmd_selftest(a)
    if a.check:
        return cmd_check(a)
    return cmd_apply(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refused as exc:
        print("拒绝：%s" % exc)
        sys.exit(2)
