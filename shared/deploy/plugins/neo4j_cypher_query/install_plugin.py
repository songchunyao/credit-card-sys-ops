#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""install_plugin.py — 把本地 fork 的 cc-ops/neo4j_cypher_query 0.1.1 装进 Dify 并建凭据。

只走 Console API，**不裸插数据库**。登录/session/CSRF/口令来源/掩码纪律/证据落盘全部复用
shared\\deploy\\dify\\plugin_bind.py（本脚本 import 它，不复制一套凭据处理）。

三段式：
  --selftest   只用本地夹具与假 session，绝不联网、绝不碰真机
  --check      只读：核「新 provider 已装 + 工具面 form 取值 + 市场插件仍在 + 凭据已收敛」
  --apply      幂等写入：upload/pkg -> install/pkg -> 轮询任务 -> 回读 -> 建/改凭据 -> 回读 diff

关键判据（本次修复的唯一目的）
  tools 面里 query 与 parameters 的 form 必须是 llm，max_records 与 allow_write_queries 必须是 form。

安装任务判据来源（2026-09-29 修复：判据未放宽，只换来源）
  真机实测：任务成功后 daemon 立即回收 task 记录 ⇒ 再轮询同一 task_id 得
  `PluginDaemonInternalServerError: record not found`，Console 把它映射成 **HTTP 500**。
  故 `wait_task` 遇 5xx 先做有界重试（TASK_HTTP_5XX_RETRIES=3 次），仍 5xx 才走只读权威
  回读（builtin provider + plugin/list uid **逐字符**相等 + 工具面 form，三条全真）
  ⇒ 结论 `success_via_readback`；任一条不真则仍判失败停手（fail-closed）。
  4xx（401/403…）保持立即停手，不触发回读。

退出码
  0 成功/已收敛   1 写入后未达预期   2 参数或校验拒绝（含「仅官方插件」拒绝）
  3 未收敛/登录失败   4 工具面缺失或 form 判据不通过   5 口令来源不可用

安全
  * 口令只从环境变量或只读 SSH 取，不落盘、不打印、不进日志；证据落盘前用真口令硬扫描。
  * 任何非 2xx：先清洗（真口令替换 + 敏感键遮蔽）再截断存档，然后非零退出，绝不静默继续。
  * 遇到 PluginInstallationForbiddenError / restricted to official / 403：**立即停止上报**，
    不试绕过（不改 enable_marketplace，不动 plugin_installation_scope）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))  # shared/deploy/plugins/<x> -> repo
sys.path.insert(0, os.path.join(REPO, "shared", "deploy", "dify"))
import plugin_bind as pb  # noqa: E402  （复用凭据/口令/掩码/证据层）

# ────────────────────────────────── 契约常量 ──────────────────────────────────
# 冻结契约（队长纠偏，不得再动）：
#   plugin_id   = <author>/<plugin name>        = cc-ops/neo4j_cypher_query
#   provider_id = <author>/<plugin name>/<provider identity.name>
#               = cc-ops/neo4j_cypher_query/neo4j_query
#   tool_name   = tools yaml 的 identity.name   = neo4j_query  ← 必须与上游同名
#     （shared/model/kgt-prompt-variants.yaml 的 KG-T-free.expected_tools=[neo4j_query] 与冻结提示词
#       shared/deploy/prompts/kgt_free_agent_system.md 逐字引用 neo4j_query；
#       shared/deploy/check_fairness.py:1148-1174 按 tool_name 集合判等 ⇒ 改 tool 名会直接 FAIL）
PLUGIN_ID = "cc-ops/neo4j_cypher_query"
PLUGIN_VERSION = "0.1.1"
# ★ R-59：包选择改为**可参数化**（默认仍是 0.1.1，保持既有行为不变）。
#   为什么需要：`binding.yaml` / `kgt-tools.yaml` 声明的 uid 是 **0.1.0**；
#   装 0.1.0 与声明一致（零契约涟漪），装 0.1.1 则需同批更新两处声明与 M4-C7/C8/C9 钉值。
DIST_DIR = os.path.join(HERE, "dist")


def pkg_path_for(version):
    return os.path.join(DIST_DIR, "cc-ops-neo4j_cypher_query-%s.difypkg" % version)


PKG_PATH = pkg_path_for(PLUGIN_VERSION)


def select_package(args):
    """按 ``--pkg`` / ``--plugin-version`` 选定要上传的包（默认 0.1.1）。"""
    global PKG_PATH, PLUGIN_VERSION
    if getattr(args, "pkg", None):
        PKG_PATH = os.path.abspath(args.pkg)
    elif getattr(args, "plugin_version", None) and args.plugin_version != PLUGIN_VERSION:
        PLUGIN_VERSION = str(args.plugin_version)
        PKG_PATH = pkg_path_for(PLUGIN_VERSION)
        _refresh_uid_re()                    # ★ R-59：uid 正则随版本刷新
    if not os.path.isfile(PKG_PATH):
        raise Refused("包不存在：%s（dist 下现有：%s）"
                      % (PKG_PATH, sorted(os.listdir(DIST_DIR)) if os.path.isdir(DIST_DIR) else []))
    return PKG_PATH, PLUGIN_VERSION
PROVIDER = "cc-ops/neo4j_cypher_query/neo4j_query"
TOOL_NAME = "neo4j_query"
PROVIDER_QUOTED = urllib.parse.quote(PROVIDER, safe="")

# 与新 provider 并存的市场插件（红线：不得卸载/修改；本脚本只回读它还在）
MARKET_PLUGIN_ID = "nikola_milosevic/neo4j_query"
MARKET_PROVIDER = "nikola_milosevic/neo4j_query/neo4j_query"
MARKET_TOOL_NAME = "neo4j_query"

# ★ R-59：uid 形态正则**跟着 PLUGIN_VERSION 走**（原先硬编码 `0\.1\.1` ⇒ 装 0.1.0 会被
#   `parse_unique_identifier` 判「形态不符」而拒）。`select_package()` 改版本后会刷新它。
UID_RE = None


def _refresh_uid_re():
    global UID_RE
    UID_RE = re.compile(r"^cc-ops/neo4j_cypher_query:%s@[0-9a-f]{64}$"
                        % re.escape(str(PLUGIN_VERSION)))
    return UID_RE


_refresh_uid_re()

CRED_NAME = "cc-kgt-neo4j-fork"
CREDENTIAL_TYPE = pb.CREDENTIAL_TYPE              # "api-key"
CREDENTIAL_VISIBILITY = pb.CREDENTIAL_VISIBILITY  # "all_team_members"
EXPECTED_BOLT = pb.EXPECTED_BOLT
EXPECTED_USER = pb.EXPECTED_USER
CRED_KEYS = pb.CRED_KEYS

API = pb.API
ROUTE_UPLOAD_PKG = API + "/workspaces/current/plugin/upload/pkg"
ROUTE_INSTALL_PKG = API + "/workspaces/current/plugin/install/pkg"
ROUTE_TASKS = API + "/workspaces/current/plugin/tasks"
ROUTE_TASK = ROUTE_TASKS + "/%s"
ROUTE_PLUGIN_LIST = API + "/workspaces/current/plugin/list"
ROUTE_BUILTIN_LIST = pb.ROUTE_LIST                              # tool-providers?type=builtin
ROUTE_PROVIDER = pb.ROUTE_PROVIDER                              # .../tool-provider/builtin/%s
ROUTE_ADD = pb.ROUTE_ADD
ROUTE_UPDATE = pb.ROUTE_UPDATE
ROUTE_CREDENTIALS = pb.ROUTE_CREDENTIALS
ROUTE_TOOLS = pb.ROUTE_TOOLS

# ★ 关键判据：只有 query / parameters 允许 LLM 填
EXPECTED_FORMS = {
    "database": "form",
    "query": "llm",
    "parameters": "llm",
    "max_records": "form",
    "allow_write_queries": "form",
}

FORBIDDEN_MARKERS = (
    "PluginInstallationForbiddenError",
    "restricted to official",
    "plugin_installation_scope",
    "only support official",
    "official plugin only",
)

# 「必须带 Dify 签名」这道闸（与「仅官方插件」是两道不同的闸，不得混为一谈）
# 真机亲测（2026-09-29）：本部署 dify-api 与 dify-plugin_daemon 都设 FORCE_VERIFYING_SIGNATURE=true，
#   上游原封已签名包 tmp/upstream_neo4j_query.pkg → upload/pkg HTTP 200（manifest.verified=true）
#   我方本地构建包（zip comment 长度 0，无签名） → HTTP 400 bad signature
SIGNATURE_MARKERS = (
    "bad signature",
    "plugin verification has been enabled",
)
FORCE_VERIFYING_SIGNATURE_ENV = "FORCE_VERIFYING_SIGNATURE"
THIRD_PARTY_VERIFICATION_ENV = "THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED"

MAX_BODY_ARCHIVE = 2000
COLD_START_LOCK_HINT = "Cannot release a lock that's no longer owned"

# ── 安装任务轮询的判据来源（本次修复，2026-09-29）──────────────────────────────
# 真机实测：安装任务**成功**的那一刻，plugin_daemon 会回收该 task 记录；之后再轮询同一
# task_id 必得 `PluginDaemonInternalServerError: record not found`，而 Console
# (controllers/console/workspace/plugin.py:960 get → plugin_service.py:959 fetch_install_task)
# 把它映射成 **HTTP 500**（不是 404/gone）。
# ⇒ **HTTP 5xx 不等于「安装失败」**：5xx 先做有界重试，仍 5xx 再走只读权威回读；
#    判据没有放宽，只是把「成功」的证据来源从「任务接口」正当化为「平台既成事实的回读」。
TASK_HTTP_5XX_RETRIES = 3                     # 5xx 在同一间隔内最多重试 3 次，仍 5xx 才回读
TASK_ERROR_RECORD_NOT_FOUND = "record not found"
VERDICT_SUCCESS_VIA_READBACK = "success_via_readback"
TASK_CONTINUE_VERDICTS = ("success", VERDICT_SUCCESS_VIA_READBACK)


def task_verdict_allows_continue(verdict):
    """任务判据是否允许继续（往下建凭据）。

    只有两种算成功：接口直报 `success`，或 5xx 之后三条只读判据全真的
    `success_via_readback`。其它一律停手（fail-closed）。
    """
    return verdict in TASK_CONTINUE_VERDICTS


class Refused(Exception):
    """参数/校验/凭据拒绝（不带动任何口令内容）。"""


class Forbidden(Exception):
    """该部署拒绝安装非官方插件 —— 立即停止上报，不试绕过。"""


# ────────────────────────────────── 传输层（在 pb.Console 上补 multipart） ──────────────────────────────────
BOUNDARY = "----ccopsfork0a1b2c3d4e5f"


def build_multipart(field: str, filename: str, payload: bytes,
                    content_type: str = "application/octet-stream") -> bytes:
    """构造 multipart/form-data 体。字段名与文件名都要显式给出（Console 侧是 request.files["pkg"]）。"""
    crlf = b"\r\n"
    out = bytearray()
    out += b"--" + BOUNDARY.encode() + crlf
    out += ('Content-Disposition: form-data; name="%s"; filename="%s"' % (field, filename)).encode("utf-8") + crlf
    out += ("Content-Type: %s" % content_type).encode("utf-8") + crlf + crlf
    out += payload + crlf
    out += b"--" + BOUNDARY.encode() + b"--" + crlf
    return bytes(out)


class PluginConsole(pb.Console):
    """继承 pb.Console 的 cookie/CSRF/登录写法，只补一个原始体 POST。"""

    def post_multipart(self, path: str, field: str, filename: str, payload: bytes):
        body = build_multipart(field, filename, payload)
        headers = dict(self.headers())
        headers["Content-Type"] = "multipart/form-data; boundary=%s" % BOUNDARY
        headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=headers)
        try:
            with self.op.open(req, timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
                return r.status, _loads(text), text
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            return e.code, _loads(text), text
        except Exception as e:  # noqa: BLE001
            return 0, {"_err": "%s: %s" % (type(e).__name__, str(e)[:200])}, ""


def _loads(text: str):
    try:
        return json.loads(text) if text and text.strip() else {}
    except Exception:  # noqa: BLE001
        return {"_raw": text[:MAX_BODY_ARCHIVE]}


# ────────────────────────────────── 清洗 / 归档 ──────────────────────────────────
def scrub(obj, secret=None):
    """真口令硬替换 + 敏感键遮蔽。递归，返回新对象。"""
    if isinstance(obj, str):
        if secret and secret in obj:
            obj = obj.replace(secret, "<REDACTED-REALPASSWORD>")
        return obj[:MAX_BODY_ARCHIVE] if len(obj) > MAX_BODY_ARCHIVE else obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(t in kl for t in ("token", "cookie", "authorization", "csrf")) and not isinstance(v, (dict, list)):
                out[k] = "<REDACTED:%s>" % type(v).__name__
            elif kl in ("password", "secret") and not isinstance(v, (dict, list)):
                out[k] = "<REDACTED:%s>" % type(v).__name__
            else:
                out[k] = scrub(v, secret)
        return out
    if isinstance(obj, list):
        return [scrub(v, secret) for v in obj]
    return obj


def archive_failure(evidence_dir, name, report, secret=None):
    """非 2xx / 失败一律存档（先清洗）。失败也不得静默。"""
    if not evidence_dir:
        return
    try:
        pb.write_evidence(evidence_dir, name, report, secret)
    except Exception as exc:  # noqa: BLE001
        print("警告：失败证据落盘被拒（%s）——已在 stdout 打印关键字段" % exc)


def detect_forbidden(status, text_or_obj):
    """识别「仅官方插件」类拒绝。命中即抛 Forbidden（不试绕过）。

    两类都算：① 响应体含 PluginInstallationForbiddenError / restricted to official 等标记；
    ② HTTP 403 本身。命中后立即停止上报，不改 enable_marketplace、不动 plugin_installation_scope。
    """
    blob = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj, ensure_ascii=False)
    for m in FORBIDDEN_MARKERS:
        if m.lower() in blob.lower():
            raise Forbidden("Console 以「仅官方插件」拒绝安装：命中 %r（HTTP %s）" % (m, status))
    if status == 403:
        raise Forbidden("Console 以 HTTP 403 拒绝安装（响应体未含已知标记，但 403 即拒绝）：%s"
                        % blob[:300])


class BlockedSignature(Exception):
    """本部署强制插件签名校验，而本地构建的包无法携带 Dify 的 RSA 签名 —— 立即停止上报。

    这是一道**与「仅官方插件」不同的闸**：它不是权限/范围设置，而是密码学校验。
    不试绕过（不改 FORCE_VERIFYING_SIGNATURE、不改 THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED、
    不重启 api/daemon、不往 daemon 的存储里塞文件）。
    """


class NoSignature(Exception):
    """我方包本地自检就发现没有签名（zip comment 为空）—— 上传前就该知道结果。"""


def detect_signature_gate(status, text_or_obj):
    """识别「强制签名校验 + 包无有效签名」这道闸。命中即抛 BlockedSignature。"""
    blob = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj, ensure_ascii=False)
    for m in SIGNATURE_MARKERS:
        if m.lower() in blob.lower():
            raise BlockedSignature(
                "本部署启用了插件签名强制校验（%s=true），而本地构建的包没有 Dify 的 RSA 签名：命中 %r（HTTP %s）"
                % (FORCE_VERIFYING_SIGNATURE_ENV, m, status))
    return False


def pkg_signature_facts(path):
    """量出包有没有签名。Dify 把 RSA 签名（JSON，含 signature/time）放在 zip 的 comment 里。

    签名覆盖包内容 ⇒ 改了 manifest 就必然失效；而没有 Dify 私钥就无法重新生成。
    """
    b = open(path, "rb").read()
    z = zipfile.ZipFile(path)
    com = z.comment
    return {"path": os.path.basename(path), "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(),
            "zip_comment_len": len(com), "has_signature": len(com) > 0,
            "zip_comment_head": com[:120].decode("utf-8", "replace"), "entries": len(z.namelist())}


def diagnosis_signature_blocked(pkg_facts, env=None):
    """给报告用的机制说明（不猜、只列已亲测的事实 + 明确的选项）。"""
    env = env or {}
    return {
        "gate": "plugin_signature_verification",
        "distinct_from": "official_only_gate（本部署未以「仅官方插件」拒绝，闸不是它）",
        "daemon_env": {FORCE_VERIFYING_SIGNATURE_ENV: env.get(FORCE_VERIFYING_SIGNATURE_ENV),
                       THIRD_PARTY_VERIFICATION_ENV: env.get(THIRD_PARTY_VERIFICATION_ENV)},
        "our_package": pkg_facts,
        "control_experiment": {
            "what": "上传上游原封未动的已签名包 tmp/upstream_neo4j_query.pkg",
            "result": "HTTP 200，manifest.verified=true，unique_identifier=nikola_milosevic/neo4j_query:0.2.0@99ca3d40…",
            "meaning": "闸就是「必须带 Dify 签名」；包缺签名是 upload 被拒的唯一原因",
        },
        "why_fork_cannot_be_signed": (
            "签名是 Dify 用其私钥对包内容做的 RSA 签名，放在 zip 的 comment 里（上游包 comment=718 B）。"
            "fork 改了 manifest.yaml/两个 yaml ⇒ 内容变了，原签名失效；且没有 Dify 私钥 ⇒ 无法重新签名。"),
        "in_scope_options": [
            "把 fork 交付为源码树/构建产物（本仓库现状），不做真机安装 —— 不动任何部署配置。",
            "由有权限方决定是否放宽该闸（需改 dify-api 与 dify-plugin_daemon 的 "
            "FORCE_VERIFYING_SIGNATURE / THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED 并重启这两个容器）—— "
            "超出本任务 inScope，必须另行授权。",
        ],
        "not_done": [
            "未修改 enable_marketplace / plugin_installation_scope",
            "未修改 FORCE_VERIFYING_SIGNATURE（未绕过）",
            "未重启任何容器",
            "未向 daemon 存储写入任何文件",
            "未卸载或修改市场插件",
        ],
    }


def read_env_value(env_key, env=None):
    """只用于报告：从已加载的 Dify 环境文件里读一个非敏感开关值（不读任何口令/密钥）。"""
    env = env if env is not None else {}
    return env.get(env_key)


# ────────────────────────────────── 校验 ──────────────────────────────────
def validate_inputs(provider, url, username, password, key_present=True):
    """与 plugin_bind.validate_inputs 同构，只把 provider 换成 fork 的 provider。"""
    if provider != PROVIDER:
        raise Refused("provider 不是目标插件 provider：%r" % (provider,))
    if not key_present:
        raise Refused("提交键缺失：NEO4J_PASSWORD")
    if not isinstance(url, str) or not url.strip():
        raise Refused("NEO4J_URL 为空")
    got = url.strip().rstrip("/")
    if got != EXPECTED_BOLT:
        raise Refused("NEO4J_URL 未指向目标实例：期望 %s，得到 %s" % (EXPECTED_BOLT, got))
    if (username or "").strip() != EXPECTED_USER:
        raise Refused("NEO4J_USERNAME 不是契约账号：期望 %s" % EXPECTED_USER)
    if password is None or password == "":
        raise Refused("NEO4J_PASSWORD 为空")
    if password in pb.PLACEHOLDER_PASSWORDS:
        raise Refused("NEO4J_PASSWORD 是占位值，拒绝猜测与写入")
    return {"NEO4J_URL": EXPECTED_BOLT, "NEO4J_USERNAME": EXPECTED_USER, "NEO4J_PASSWORD": password}


def build_add_payload(creds):
    return {"credentials": dict(creds), "name": CRED_NAME,
            "type": CREDENTIAL_TYPE, "visibility": CREDENTIAL_VISIBILITY}


def build_update_payload(creds, credential_id):
    return {"credential_id": credential_id, "credentials": dict(creds), "name": CRED_NAME}


# ────────────────────────────────── 步骤：upload / install / 轮询 ──────────────────────────────────
def parse_unique_identifier(resp):
    """从 upload/pkg 的响应里读出 unique_identifier（**不自己猜校验和**）。"""
    if not isinstance(resp, dict):
        raise Refused("upload/pkg 响应不是对象：%r" % type(resp).__name__)
    uid = resp.get("unique_identifier")
    if not uid:
        inner = resp.get("plugin") if isinstance(resp.get("plugin"), dict) else None
        uid = (inner or {}).get("unique_identifier")
    if not uid:
        raise Refused("upload/pkg 响应里没有 unique_identifier（键：%s）" % sorted(resp.keys()))
    if not UID_RE.match(str(uid)):
        raise Refused("unique_identifier 形态不符 %s：%r" % (UID_RE.pattern, uid))
    return str(uid)


def install_pkg(c, uid):
    """POST install/pkg。返回 (status, response)。"""
    return c.post(ROUTE_INSTALL_PKG, {"plugin_unique_identifiers": [uid]})


def extract_task_id(resp):
    if isinstance(resp, dict):
        for k in ("task_id", "taskId", "id"):
            if resp.get(k):
                return str(resp[k])
    return None


def classify_task(obj):
    """把任务回读分类成 running / success / failed（宽容字段名，实测后按需收紧）。"""
    if not isinstance(obj, dict):
        return "running", {"why": "not-a-dict"}
    st = str(obj.get("status") or "").lower()
    plugins = obj.get("plugins")
    sub = []
    if isinstance(plugins, list):
        for p in plugins:
            if isinstance(p, dict):
                sub.append({"id": p.get("plugin_unique_identifier") or p.get("plugin_id"),
                            "status": p.get("status"),
                            "message": (p.get("message") or "")[:400]})
    subs = [str(s.get("status") or "").lower() for s in sub]
    if st in ("failed", "failure", "error") or any(s in ("failed", "failure", "error") for s in subs):
        return "failed", {"status": st, "plugins": sub}
    if st in ("success", "completed", "succeeded") or (subs and all(s in ("success", "installed", "completed") for s in subs)):
        return "success", {"status": st, "plugins": sub}
    return "running", {"status": st, "plugins": sub}


def resolve_task_http_error(task_id, http_status, body, polls, readback):
    """5xx（有界重试耗尽）后的判据来源切换：**只用只读回读**判成功，判不了就保持失败。

    `readback` 是注入的无参回调，返回 (confirmed, evidence)。未注入 / 抛异常 / 判据不真
    ⇒ 一律 `http_error`（★ 绝不在回读不可用时把 http_error 当成成功）。
    """
    blob = json.dumps(scrub(body), ensure_ascii=False).lower() if isinstance(body, (dict, list)) \
        else str(scrub(body)).lower()
    err = {"task_id": task_id, "http": http_status,
           "marker": TASK_ERROR_RECORD_NOT_FOUND if TASK_ERROR_RECORD_NOT_FOUND in blob else None,
           "meaning": ("daemon 已回收该 task 记录（说明任务多半已终结）——"
                       "但这是**平台侧缺信号**，本身不是成功证据，必须由只读回读补证")}
    if readback is None:
        err["readback"] = {"available": False,
                           "why": "未注入回读回调 ⇒ 5xx 保持失败（不把 http_error 当成功）"}
        return "http_error", {"http": http_status, "body": scrub(body), "task_route_error": err}, polls
    try:
        confirmed, ev = readback()
    except Exception as exc:  # noqa: BLE001
        err["readback"] = {"available": False, "why": "回读抛异常：%s: %s"
                           % (type(exc).__name__, str(exc)[:200]),
                           "traceback_tail": traceback.format_exc()[-400:]}
        return "http_error", {"http": http_status, "body": scrub(body), "task_route_error": err}, polls
    err["readback"] = {"available": True, "confirmed": bool(confirmed)}
    detail = {"http": http_status, "body": scrub(body), "task_route_error": err,
              "readback_evidence": scrub(ev)}
    if confirmed:
        return VERDICT_SUCCESS_VIA_READBACK, detail, polls
    return "http_error", detail, polls


def wait_task(c, task_id, timeout_s=300.0, interval=3.0, sleep=time.sleep, mono=time.monotonic,
              readback=None, http_5xx_retries=TASK_HTTP_5XX_RETRIES):
    """轮询安装任务到终态。返回 (verdict, detail, polls)。

    判据来源（本次修复；**判据未放宽，只改了来源**）：
      * 200 + success/failed → 原判据，直接采纳；
      * 200 + running        → 继续轮询到超时；
      * 非 200 且非 5xx（401/403/404…）→ **立即停手**，不看回读；
      * HTTP 5xx             → 同一间隔内有界重试 N=`http_5xx_retries` 次；仍 5xx 才走
                               `readback` 权威回读：三条判据全真 ⇒ `success_via_readback`，
                               否则保持 `http_error`（fail-closed）。

    真机依据：任务成功后 daemon 回收 task 记录，api 把 "record not found" 映射成 500
    ⇒ 「任务成功」这一刻平台恰好销毁了成功信号。
    """
    t0 = mono()
    polls = []
    fivexx = 0
    while True:
        st, obj = c.get(ROUTE_TASK % urllib.parse.quote(str(task_id), safe=""))
        if st == 200:
            fivexx = 0
            verdict, detail = classify_task(obj)
            polls.append({"http": st, "verdict": verdict, "at_s": round(mono() - t0, 2),
                          "body": scrub(obj)})
            if verdict in ("success", "failed"):
                return verdict, detail, polls
        else:
            is_5xx = 500 <= int(st) < 600
            fivexx = fivexx + 1 if is_5xx else 0
            polls.append({"http": st,
                          "verdict": "http_5xx_retry" if is_5xx else "http_error",
                          "at_s": round(mono() - t0, 2), "body": scrub(obj),
                          "http_5xx_streak": fivexx if is_5xx else 0,
                          "retries_budget": http_5xx_retries,
                          "retry_or_readback": ("retry" if is_5xx and fivexx <= http_5xx_retries
                                                else ("authoritative_readback" if is_5xx else "stop"))})
            if not is_5xx:
                # 4xx / 传输层异常（st=0）：认证、权限、路由类硬错误 —— 立即停手，不看回读
                return "http_error", {"http": st, "body": scrub(obj),
                                      "why": "非 200 且非 5xx ⇒ 立即停手（不回读）"}, polls
            if fivexx > http_5xx_retries:
                return resolve_task_http_error(task_id, st, obj, polls, readback)
        if mono() - t0 > timeout_s:
            return "timeout", {"timeout_s": timeout_s}, polls
        sleep(interval)


# ────────────────────────────────── 回读 ──────────────────────────────────
def read_builtin_providers(c):
    st, d = c.get(ROUTE_BUILTIN_LIST)
    seq = d if isinstance(d, list) else ((d.get("data") if isinstance(d, dict) else None) or [])
    rows = [{"id": p.get("id"), "name": p.get("name"), "label": (p.get("label") or {}).get("en_US"),
             "type": p.get("type")} for p in seq if isinstance(p, dict)]
    return st, rows


def read_installed_plugins(c):
    """GET plugin/list（实测无需参数）→ 每个已装插件的 installation_id / plugin_unique_identifier。

    实测形状：{"plugins": [{"id","installation_id","plugin_id","plugin_unique_identifier",
    "name","version","checksum","source","verified",...}], "total": N}
    """
    st, d = c.get(ROUTE_PLUGIN_LIST)
    seq = (d.get("plugins") if isinstance(d, dict) else None) or (d if isinstance(d, list) else []) or []
    rows = []
    for p in seq:
        if not isinstance(p, dict):
            continue
        rows.append({"id": p.get("id"), "installation_id": p.get("installation_id"),
                     "plugin_id": p.get("plugin_id"), "plugin_unique_identifier": p.get("plugin_unique_identifier"),
                     "name": p.get("name"), "version": p.get("version"), "checksum": p.get("checksum"),
                     "source": p.get("source"), "verified": p.get("verified")})
    total = d.get("total") if isinstance(d, dict) else None
    return st, {"status": st, "total": total, "count": len(rows), "plugins": rows}


def pick_plugin(snap, plugin_id):
    for p in snap.get("plugins", []):
        if p.get("plugin_id") == plugin_id:
            return p
    return None


def read_tool_face(c, provider_quoted, route_tools=ROUTE_TOOLS):
    st, d = c.get(route_tools % provider_quoted)
    items = d if isinstance(d, list) else ((d.get("tools") if isinstance(d, dict) else None) or [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        params = []
        for p in (it.get("parameters") or []):
            if isinstance(p, dict):
                params.append({"name": p.get("name"), "form": p.get("form"), "type": p.get("type"),
                               "required": p.get("required"), "default": p.get("default")})
        out.append({"name": it.get("name"), "parameters": params})
    return st, out


def judge_forms(tools, tool_name=TOOL_NAME, expected_forms=None):
    """★ 关键判据。返回 (ok, detail)。"""
    want = dict(expected_forms or EXPECTED_FORMS)
    detail = {"tool_name": tool_name, "found": None, "forms": {}, "expected": dict(want), "errors": []}
    if not tools:
        detail["errors"].append("工具面为空")
        return False, detail
    hit = None
    for t in tools:
        if t.get("name") == tool_name:
            hit = t
            break
    if hit is None:
        detail["errors"].append("工具面里没有 %s（有：%s）" % (tool_name, [t.get("name") for t in tools]))
        return False, detail
    detail["found"] = tool_name
    got = {p["name"]: p.get("form") for p in hit["parameters"]}
    detail["forms"] = got
    for name, want_v in want.items():
        if name not in got:
            detail["errors"].append("缺少参数 %s" % name)
        elif got[name] != want_v:
            detail["errors"].append("参数 %s 的 form 是 %r，必须是 %r" % (name, got[name], want_v))
    return (not detail["errors"]), detail


# ──────────────────── 权威回读：5xx 时的成功证据来源（本次修复核心） ────────────────────
def exact_str_match(observed, expected):
    """**逐字符**比对。返回 (ok, mismatch_indexes, note)。

    ★ 只用逐字符相等；**不得**退化成 `startswith` / 「前缀相同」/「同版本」。

    - `observed` 是 plugin/list 回读到的 `plugin_unique_identifier`；
    - `expected` 是本次 upload/pkg 返回的 uid（64-hex 校验和必须完全一致）。
    """
    e = expected if isinstance(expected, str) else ""
    o = observed if isinstance(observed, str) else ""
    if not e:
        return False, [], "expected 为空 ⇒ 拒绝比对（fail-closed，不接受任何 observed）"
    if not o:
        return False, [0], "observed 为空（plugin/list 里没有该字段）⇒ 不通过"
    n = min(len(o), len(e))
    idx = [i for i in range(n) if o[i] != e[i]]
    if len(o) != len(e):
        idx = (idx + [n])[:8]
        return False, idx, "长度不同（observed %d vs expected %d）⇒ 不通过" % (len(o), len(e))
    if idx:
        return False, idx[:8], "逐字符不一致：diff_count=%d，首个差异位 index=%d（observed %r vs expected %r）" \
            % (len(idx), idx[0], o[idx[0]], e[idx[0]])
    return True, [], "逐字符一致（长度 %d，含 64-hex 校验和）" % len(e)


def uid_checksum(uid):
    """取 unique_identifier 尾部的 64-hex 校验和；形态不符返回 None。"""
    m = re.search(r"@([0-9a-fA-F]{64})$", str(uid or ""))
    return m.group(1).lower() if m else None


def readback_install_state(c, uid, provider=PROVIDER, plugin_id=PLUGIN_ID, tool_name=TOOL_NAME,
                           expected_forms=None):
    """★ 权威只读回读：安装任务链路不可用时的**成功证据来源**。

    三条判据必须**同时**为真（任一条不真 ⇒ confirmed=False，fail-closed）：

      (a) builtin providers 里出现目标 provider（逐字符等于 `provider`）；
      (b) `plugin/list` 里存在 `plugin_id == plugin_id` 的条目，且其
          `plugin_unique_identifier` **逐字符等于**本次 upload 返回的 uid
          —— 64-hex 校验和必须完全一致；**不得**用「uid 前缀相同」「provider 名字相同」
          「版本相同」放宽本条；
      (c) 工具面 `judge_forms` 通过（5 个参数的 form 与期望逐个一致）。

    返回 `(confirmed, evidence)`。evidence 只含 provider / uid / form 这类非敏感值，
    不含任何口令（与 scrub / sanitize_credentials 同一纪律）。
    """
    want_forms = dict(expected_forms or EXPECTED_FORMS)
    uid = uid if isinstance(uid, str) else ""
    ev = {"source": "authoritative_readback", "at_utc": pb.now_utc(),
          "trigger": "install task route returned HTTP 5xx after bounded retries",
          "expected": {"provider": provider, "plugin_id": plugin_id, "tool_name": tool_name,
                       "unique_identifier": uid, "unique_identifier_len": len(uid),
                       "uid_checksum_64hex": uid_checksum(uid), "forms": want_forms},
          "criteria": {}, "failed_criteria": [], "all_true": False}

    # (a) builtin providers
    pst, rows = read_builtin_providers(c)
    ids = [r.get("id") for r in rows]
    a_ok = provider in ids
    ev["criteria"]["a_provider_in_builtin"] = {
        "http": pst,
        "observed": {"provider_present": a_ok, "provider_count": len(rows),
                     "matched": [i for i in ids if i == provider]},
        "expected": {"provider": provider, "provider_present": True},
        "ok": a_ok}

    # (b) plugin/list：plugin_id 相同 **且** uid 逐字符相同
    ist, snap = read_installed_plugins(c)
    fork = pick_plugin(snap, plugin_id)
    obs_uid = (fork or {}).get("plugin_unique_identifier")
    uid_ok, uid_diff, uid_note = exact_str_match(obs_uid, uid)
    b_ok = bool(fork) and uid_ok
    ev["criteria"]["b_plugin_list_uid_exact"] = {
        "http": ist,
        "observed": {"plugin_id": (fork or {}).get("plugin_id"),
                     "installation_id": (fork or {}).get("installation_id"),
                     "name": (fork or {}).get("name"),
                     "version": (fork or {}).get("version"),
                     "unique_identifier": obs_uid,
                     "unique_identifier_len": len(obs_uid) if isinstance(obs_uid, str) else None,
                     "uid_checksum_64hex": uid_checksum(obs_uid),
                     "checksum_matches_expected": uid_checksum(obs_uid) == uid_checksum(uid),
                     "match_mode": "exact_char_by_char",
                     "diff_count": len(uid_diff), "mismatch_indexes": uid_diff, "note": uid_note},
        "expected": {"plugin_id": plugin_id, "unique_identifier": uid,
                     "unique_identifier_len": len(uid), "uid_checksum_64hex": uid_checksum(uid),
                     "match_mode": "exact_char_by_char"},
        "ok": b_ok}

    # (c) 工具面 form
    tst, tools = read_tool_face(c, urllib.parse.quote(provider, safe=""))
    c_ok, form_detail = judge_forms(tools, tool_name=tool_name, expected_forms=want_forms)
    ev["criteria"]["c_tool_face_forms"] = {
        "http": tst,
        "observed": {"tool_found": form_detail.get("found"), "forms": form_detail.get("forms"),
                     "tool_names": [t.get("name") for t in tools]},
        "expected": {"forms": want_forms},
        "ok": c_ok, "errors": form_detail.get("errors")}

    ev["all_true"] = bool(a_ok and b_ok and c_ok)
    ev["failed_criteria"] = [k for k, v in ev["criteria"].items() if not v.get("ok")]
    return ev["all_true"], ev


def read_credentials(c, provider_quoted, real_password):
    st, d = c.get(ROUTE_CREDENTIALS % provider_quoted)
    creds = []
    if isinstance(d, list):
        for item in d:
            item = item if isinstance(item, dict) else {}
            creds.append({"id": item.get("id"), "name": item.get("name"),
                          "provider": item.get("provider"),
                          "credential_type": item.get("credential_type"),
                          "is_default": item.get("is_default"),
                          "visibility": item.get("visibility"),
                          "sanitized": pb.sanitize_credentials(item.get("credentials"), real_password)})
    return st, {"count": len(creds), "credentials": creds}


def judge_credentials(snap):
    for c in snap.get("credentials", []):
        s = c.get("sanitized", {})
        if c.get("name") == CRED_NAME or s.get("NEO4J_URL") == EXPECTED_BOLT:
            if s.get("NEO4J_URL") == EXPECTED_BOLT and s.get("NEO4J_USERNAME") == EXPECTED_USER \
                    and s.get("NEO4J_PASSWORD") not in (None, ""):
                return "converged", c
            return "drift", c
    return "missing", None


def slots(snap):
    """R-47：只登记「条目名 / id / URL / 用户名 / 口令长度」，绝不登记口令的掩码形态、
    stars、首尾揭示位数或任何截断片段。"""
    return {"count": snap.get("count"),
            "credential_ids": [c.get("id") for c in snap.get("credentials", [])],
            "names": [c.get("name") for c in snap.get("credentials", [])],
            "urls": [c.get("sanitized", {}).get("NEO4J_URL") for c in snap.get("credentials", [])],
            "users": [c.get("sanitized", {}).get("NEO4J_USERNAME") for c in snap.get("credentials", [])],
            "password_lens": [c.get("sanitized", {}).get("_password_len") for c in snap.get("credentials", [])]}


# ────────────────────────────────── 凭据写入（含冷启动锁重试） ──────────────────────────────────
def write_credential(c, creds, existing, do_write=print, sleep=time.sleep, attempts=2):
    """返回 (status, response, action, elapsed, retried_cold_start)。"""
    if existing is not None:
        act, route, payload = "update", ROUTE_UPDATE, build_update_payload(creds, existing["id"])
    else:
        act, route, payload = "add", ROUTE_ADD, build_add_payload(creds)
    url = route % PROVIDER_QUOTED
    last = None
    for i in range(attempts):
        t0 = time.monotonic()
        st, rsp = c.post(url, payload)
        el = round(time.monotonic() - t0, 2)
        blob = json.dumps(rsp, ensure_ascii=False) if isinstance(rsp, (dict, list)) else str(rsp)
        if st == 200:
            return st, rsp, act, el, i > 0
        last = (st, rsp, act, el)
        if COLD_START_LOCK_HINT in blob and i + 1 < attempts:
            do_write("凭据写入命中冷启动锁超时假故障（HTTP %s: %s），等 30 s 重试一次（%s）"
                     % (st, COLD_START_LOCK_HINT, act))
            sleep(30)
            continue
        break
    return last[0], last[1], last[2], last[3], False


# ────────────────────────────────── --selftest ──────────────────────────────────
FIXTURE_UID = "cc-ops/neo4j_cypher_query:0.1.1@" + "a" * 64


def _fixture_tools(bad=False):
    return [{"name": TOOL_NAME, "parameters": [
        {"name": "database", "form": "form", "type": "string", "required": False, "default": None},
        {"name": "query", "form": "form" if bad else "llm", "type": "string", "required": True, "default": None},
        {"name": "parameters", "form": "llm", "type": "string", "required": False, "default": None},
        {"name": "max_records", "form": "form", "type": "number", "required": False, "default": 1000},
        {"name": "allow_write_queries", "form": "form", "type": "boolean", "required": False, "default": False},
    ]}]


class FakeConsole(pb.Console):
    """假 session：全部响应来自本地夹具，绝不联网。"""

    def __init__(self, routes):
        super().__init__("http://fixture.invalid")
        self.routes = routes
        self.calls = []

    def get(self, p):
        self.calls.append(("GET", p))
        return self.routes.get(("GET", p), (404, {}))

    def post(self, p, payload):
        self.calls.append(("POST", p, payload))
        v = self.routes.get(("POST", p), (404, {}))
        return v

    def post_multipart(self, path, field, filename, payload):
        self.calls.append(("POST-MP", path, field, filename))
        v = self.routes.get(("POST-MP", path), (404, {}, ""))
        return v


def cmd_selftest(_args):
    bad = 0

    def check(title, ok, detail=""):
        nonlocal bad
        bad += 0 if ok else 1
        print("[%s] %s%s" % ("PASS" if ok else "FAIL", title, ("  -> " + str(detail)) if detail else ""))

    # 1) multipart 体形态：字段名必须叫 pkg，必须有 boundary 与结尾串
    body = build_multipart("pkg", "cc-ops-neo4j_cypher_query-0.1.1.difypkg", b"PK\x03\x04payload")
    check("multipart: 字段名是 pkg", b'name="pkg"' in body)
    check("multipart: 带 filename", b'filename="cc-ops-neo4j_cypher_query-0.1.1.difypkg"' in body)
    check("multipart: 以 boundary 收尾", body.rstrip().endswith(b"--" + BOUNDARY.encode() + b"--"))
    check("multipart: 载入原始字节未被改写", b"PK\x03\x04payload" in body)

    # 2) unique_identifier 解析与形态负控
    ok_uid = None
    try:
        ok_uid = parse_unique_identifier({"unique_identifier": FIXTURE_UID})
    except Refused as e:
        ok_uid = "refused: %s" % e
    check("uid: 正控解析", ok_uid == FIXTURE_UID, ok_uid)
    for title, resp in [("缺 unique_identifier", {}),
                        ("形态不符（版本错）", {"unique_identifier": "cc-ops/neo4j_cypher_query:0.2.0@" + "a" * 64}),
                        ("形态不符（校验和非 64hex）", {"unique_identifier": "cc-ops/neo4j_cypher_query:0.1.1@zz"}),
                        ("形态不符（换了作者）", {"unique_identifier": "someone/neo4j_cypher_query:0.1.1@" + "a" * 64})]:
        try:
            parse_unique_identifier(resp)
            check("uid 负控: %s" % title, False, "竟然接受了")
        except Refused:
            check("uid 负控: %s" % title, True)

    # 3) 任务分类与轮询
    check("task: running", classify_task({"status": "running"})[0] == "running")
    check("task: success", classify_task({"status": "success"})[0] == "success")
    check("task: failed", classify_task({"status": "failed"})[0] == "failed")
    check("task: 子项 installed 视为 success",
          classify_task({"status": "running", "plugins": [{"status": "installed"}]})[0] == "success")
    check("task: 子项 failed 视为 failed",
          classify_task({"status": "running", "plugins": [{"status": "failed"}]})[0] == "failed")

    seq = {"n": 0}

    def fake_get(path):
        seq["n"] += 1
        if seq["n"] < 3:
            return 200, {"status": "running"}
        return 200, {"status": "success"}

    class FC(FakeConsole):
        def get(self, p):
            return fake_get(p)

    fc = FC({})
    verdict, detail, polls = wait_task(fc, "task-1", timeout_s=5, interval=0.0, sleep=lambda s: None,
                                       mono=lambda: 0.0 if seq["n"] < 3 else 99.0)
    check("wait_task: running->success", verdict == "success", "%s / polls=%d" % (verdict, len(polls)))

    class FC2(FakeConsole):
        def get(self, p):
            return 200, {"status": "failed"}

    v2, _d2, _p2 = wait_task(FC2({}), "t", timeout_s=5, interval=0, sleep=lambda s: None, mono=lambda: 0.0)
    check("wait_task: failed 传播", v2 == "failed", v2)

    class FC3(FakeConsole):
        def get(self, p):
            return 500, {"code": "plugin_error", "message": "boom"}

    v3, _d3, _p3 = wait_task(FC3({}), "t", timeout_s=5, interval=0, sleep=lambda s: None, mono=lambda: 0.0)
    check("wait_task: 非 200 -> http_error（不静默）", v3 == "http_error", v3)

    # 4) 关键判据 judge_forms
    ok, det = judge_forms(_fixture_tools())
    check("judge_forms: 正控 (llm/llm/form/form/form) 通过", ok, det["errors"])
    ok2, det2 = judge_forms(_fixture_tools(bad=True))
    check("judge_forms: 负控 query=form 必须判不通过", (not ok2) and any("query" in e for e in det2["errors"]),
          det2["errors"])
    ok3, det3 = judge_forms([{"name": "other", "parameters": []}])
    check("judge_forms: 工具不存在必须判不通过", not ok3)

    # 5) 仅官方插件拒绝必须被识别成 Forbidden（且不重试绕过）
    for title, st, blob in [("PluginInstallationForbiddenError", 403,
                             '{"code":"PluginInstallationForbiddenError","message":"x"}'),
                            ("restricted to official", 403, '{"message":"restricted to official plugins only"}'),
                            ("裸 403", 403, '{"message":"forbidden"}'),
                            ("install_required 标记", 400, '{"message":"plugin_installation_scope blocked"}')]:
        try:
            detect_forbidden(st, blob)
            check("forbidden 识别: %s" % title, False, "竟然没触发")
        except Forbidden:
            check("forbidden 识别: %s" % title, True)
    try:
        detect_forbidden(200, {"unique_identifier": FIXTURE_UID})
        check("forbidden 识别: 200 正常响应不误报", True)
    except Forbidden:
        check("forbidden 识别: 200 正常响应不误报", False, "误报")

    # 6) 凭据提交体形态
    p = build_add_payload({"NEO4J_URL": EXPECTED_BOLT, "NEO4J_USERNAME": EXPECTED_USER, "NEO4J_PASSWORD": "x"})
    check("shape: /add 键集合 == {credentials,name,type,visibility}",
          set(p) == {"credentials", "name", "type", "visibility"} and set(p["credentials"]) == set(CRED_KEYS),
          sorted(p.keys()))

    # 7) 口令与 provider 负控
    good = {"provider": PROVIDER, "url": EXPECTED_BOLT, "username": EXPECTED_USER, "password": "not-a-real-secret"}
    cases = [("缺键", dict(good, key_present=False), False),
             ("URL 非目标实例", dict(good, url="bolt://10.0.0.9:7688"), False),
             ("端口错", dict(good, url="bolt://192.0.2.1:7687"), False),
             ("口令空", dict(good, password=""), False),
             ("口令占位", dict(good, password="CHANGE_ME"), False),
             ("账号错", dict(good, username="readonly"), False),
             ("provider 不匹配（拿市场 provider 试探）",
              dict(good, provider=MARKET_PROVIDER), False),
             ("正控", good, True)]
    for title, kw, should_pass in cases:
        try:
            validate_inputs(kw["provider"], kw["url"], kw["username"], kw["password"],
                            key_present=kw.get("key_present", True))
            got_ok, det = True, "accepted"
        except Refused as e:
            got_ok, det = False, "refused: %s" % e
        check("校验: %s" % title, got_ok == should_pass, det)

    # 8) 清洗：真口令不得留在证据里
    secret = "S3cr3t-xyz"
    s = scrub({"nested": [{"note": "pw=" + secret}], "access_token": "abc", "password": "abc"}, secret)
    blob = json.dumps(s, ensure_ascii=False)
    check("scrub: 真口令被替换", secret not in blob)
    check("scrub: token/password 键被遮蔽",
          s["access_token"].startswith("<REDACTED") and s["password"].startswith("<REDACTED"))

    # 9) plugin/list 回读与 installation_id 提取（下游 W9 依赖这四个字段）
    fixture_inst = {"plugins": [
        {"id": "inst-market", "installation_id": "inst-market", "plugin_id": MARKET_PLUGIN_ID,
         "plugin_unique_identifier": "nikola_milosevic/neo4j_query:0.2.0@" + "b" * 64,
         "name": "neo4j_query", "version": "0.2.0", "checksum": "b" * 64, "source": "marketplace"},
        {"id": "inst-fork", "installation_id": "inst-fork", "plugin_id": PLUGIN_ID,
         "plugin_unique_identifier": FIXTURE_UID, "name": "neo4j_cypher_query", "version": "0.1.1",
         "checksum": "a" * 64, "source": "package"},
    ], "total": 2}
    fc_inst = FakeConsole({("GET", ROUTE_PLUGIN_LIST): (200, fixture_inst)})
    st_i, snap = read_installed_plugins(fc_inst)
    check("plugin/list: 解析 plugins/total", st_i == 200 and snap["count"] == 2 and snap["total"] == 2, snap["count"])
    fk = pick_plugin(snap, PLUGIN_ID)
    mk = pick_plugin(snap, MARKET_PLUGIN_ID)
    check("plugin/list: 取到 fork installation_id", (fk or {}).get("installation_id") == "inst-fork", fk)
    check("plugin/list: 取到 market installation_id（并存可证）",
          (mk or {}).get("installation_id") == "inst-market", mk)
    check("plugin/list: 未装插件返回 None", pick_plugin(snap, "nobody/nothing") is None)
    check("plugin/list: 非 200 不炸", read_installed_plugins(FakeConsole({("GET", ROUTE_PLUGIN_LIST): (404, {})}))[1]["count"] == 0)

    # 10) 冷启动锁重试（第 1 次 400 锁错、第 2 次 200）
    state = {"n": 0}

    class FC4(FakeConsole):
        def post(self, p, payload):
            state["n"] += 1
            if state["n"] == 1:
                return 400, {"code": "plugin_error", "message": COLD_START_LOCK_HINT}
            return 200, {"id": "cred-1"}

    st, rsp, act, el, retried = write_credential(FC4({}), good, None, do_write=lambda *a: None,
                                                sleep=lambda s: None, attempts=2)
    check("冷启动锁: 重试后成功且标记 retried", st == 200 and retried and act == "add", (st, retried, act))

    # 11) 签名闸识别（本部署真实闸；与「仅官方插件」是两道不同的闸）
    sig_body = ('{"code":"plugin_error","message":"plugin verification has been enabled, '
                'and the plugin you want to install has a bad signature"}')
    raised = None
    try:
        detect_signature_gate(400, sig_body)
    except BlockedSignature as e:
        raised = str(e)
    check("签名闸: bad signature 必须识别为 BlockedSignature", raised is not None and "bad signature" in raised,
          raised)
    raised2 = None
    try:
        detect_signature_gate(400, {"message": "plugin verification has been enabled"})
    except BlockedSignature:
        raised2 = True
    check("签名闸: verification has been enabled 也识别", raised2 is True)
    check("签名闸: 正常 200 响应不误报", detect_signature_gate(200, {"unique_identifier": "x"}) is False)
    check("签名闸: 仅官方插件文案不误判为签名闸",
          detect_signature_gate(400, {"message": "restricted to official plugins"}) is False)

    # 12) 包签名事实（真文件：我方包无签名、上游包有 718 B 签名 comment）
    our = pkg_signature_facts(PKG_PATH)
    check("包签名: 我方构建包无签名（zip comment 为空）",
          our["has_signature"] is False and our["zip_comment_len"] == 0 and our["entries"] == 15, our)
    up_pkg = os.path.join(REPO, "tmp", "upstream_neo4j_query.pkg")
    if os.path.exists(up_pkg):
        ups = pkg_signature_facts(up_pkg)
        check("包签名: 上游已签名包含签名 comment（对照正控）",
              ups["has_signature"] is True and ups["zip_comment_len"] > 0, ups["zip_comment_len"])
    diag = diagnosis_signature_blocked(our, {FORCE_VERIFYING_SIGNATURE_ENV: True})
    check("签名闸: 诊断块含 gate/对照实验/不能自签的原因/未做清单",
          diag["gate"] == "plugin_signature_verification" and "control_experiment" in diag
          and "why_fork_cannot_be_signed" in diag and len(diag["not_done"]) >= 4, sorted(diag.keys()))

    # 13) ★ 本次修复：安装任务 5xx（daemon "record not found"）时的判据来源切换
    #     真机 500 响应体形态：PluginDaemonInternalServerError: record not found
    NOT_FOUND_BODY = {"code": "plugin_error", "message":
                      "PluginDaemonInternalServerError: record not found"}

    def readback_console(provider_present=True, uid_override=None, tools=None):
        """离线回读夹具：三条判据可分别打破，用于正控/三条负控。"""
        rb_uid = FIXTURE_UID if uid_override is None else uid_override
        rows = [{"id": MARKET_PROVIDER, "name": TOOL_NAME, "label": {"en_US": "Neo4j"},
                 "type": "builtin"}]
        if provider_present:
            rows.append({"id": PROVIDER, "name": TOOL_NAME, "label": {"en_US": "Neo4j (cc-ops fork)"},
                         "type": "builtin"})
        inst = {"plugins": [
            {"id": "inst-fork", "installation_id": "inst-fork", "plugin_id": PLUGIN_ID,
             "plugin_unique_identifier": rb_uid, "name": "neo4j_cypher_query", "version": "0.1.1",
             "checksum": uid_checksum(rb_uid), "source": "package", "verified": False},
        ], "total": 1}
        return FakeConsole({("GET", ROUTE_BUILTIN_LIST): (200, rows),
                            ("GET", ROUTE_PLUGIN_LIST): (200, inst),
                            ("GET", ROUTE_TOOLS % PROVIDER_QUOTED):
                                (200, _fixture_tools() if tools is None else tools)})

    class FCPoll5xx(FakeConsole):
        """第 1 次轮询 200 running，之后一律 500 record not found（真机实测序列）。"""

        def __init__(self, state):
            super().__init__({})
            self.state = state

        def get(self, p):
            self.calls.append(("GET", p))
            self.state["n"] += 1
            if self.state["n"] == 1:
                return 200, {"status": "running"}
            return 500, dict(NOT_FOUND_BODY)

    ONE_CHAR_OFF_UID = FIXTURE_UID[:-1] + "b"      # 与 FIXTURE_UID 只差最后 1 个字符

    rb_cases = [
        ("正控: 5xx 而回读三条全真 ⇒ success_via_readback",
         readback_console(), VERDICT_SUCCESS_VIA_READBACK, True, None),
        ("负控1: 5xx 且 builtin 里没有目标 provider ⇒ 仍失败停手",
         readback_console(provider_present=False), "http_error", False, "a_provider_in_builtin"),
        ("负控2: 5xx 且 plugin/list 的 uid 只差 1 个字符 ⇒ 仍失败停手",
         readback_console(uid_override=ONE_CHAR_OFF_UID), "http_error", False, "b_plugin_list_uid_exact"),
        ("负控3: 5xx 且工具面 form 不符（query=form）⇒ 仍失败停手",
         readback_console(tools=_fixture_tools(bad=True)), "http_error", False, "c_tool_face_forms"),
    ]
    for title, rb_console, want_v, want_continue, want_failed in rb_cases:
        st13 = {"n": 0}
        c13 = FCPoll5xx(st13)
        v13, d13, p13 = wait_task(c13, "01a0eb3f-4079-7669-a722-a6b3bb91cde1", timeout_s=10,
                                  interval=0, sleep=lambda s: None, mono=lambda: 0.0,
                                  readback=lambda rb=rb_console: readback_install_state(rb, FIXTURE_UID))
        ev13 = d13.get("readback_evidence") or {}
        n500 = len([p for p in p13 if p.get("http") == 500])
        check("任务 5xx 判据: %s" % title, v13 == want_v and task_verdict_allows_continue(v13) is want_continue,
              "verdict=%s continue=%s readback=%s" % (v13, task_verdict_allows_continue(v13),
                                                       json.dumps((d13.get("task_route_error") or {}).get("readback"),
                                                                  ensure_ascii=False)[:300]))
        check("任务 5xx 判据: %s —— 5xx 轮询 %d 次（重试 %d 次后才回读）"
              % (title.split(":")[0], n500, TASK_HTTP_5XX_RETRIES),
              n500 == TASK_HTTP_5XX_RETRIES + 1 and p13[-1].get("retry_or_readback") == "authoritative_readback",
              [p.get("retry_or_readback") for p in p13])
        check("任务 5xx 判据: %s —— task_route_error 记录 500 + record not found 标记"
              % title.split(":")[0],
              (d13.get("task_route_error") or {}).get("http") == 500
              and (d13.get("task_route_error") or {}).get("marker") == "record not found",
              d13.get("task_route_error", {}).get("marker"))
        check("任务 5xx 判据: %s —— readback_evidence.all_true=%s failed_criteria=%s"
              % (title.split(":")[0], ev13.get("all_true"), ev13.get("failed_criteria")),
              (ev13.get("all_true") is True and not ev13.get("failed_criteria")) if want_continue
              else (ev13.get("all_true") is False and want_failed in (ev13.get("failed_criteria") or [])),
              ev13.get("failed_criteria"))
        if want_continue:
            crit = ev13.get("criteria") or {}
            check("任务 5xx 判据: 正控 —— 三条判据各自 observed/expected 齐全、uid 用完整 64 位",
                  all(crit.get(k, {}).get("ok") is True for k in
                      ("a_provider_in_builtin", "b_plugin_list_uid_exact", "c_tool_face_forms"))
                  and ev13["expected"]["unique_identifier"] == FIXTURE_UID
                  and len(ev13["expected"]["uid_checksum_64hex"]) == 64
                  and crit["b_plugin_list_uid_exact"]["observed"]["match_mode"] == "exact_char_by_char"
                  and crit["b_plugin_list_uid_exact"]["observed"]["checksum_matches_expected"] is True,
                  sorted(crit.keys()))
        if not want_continue:
            check("任务 5xx 判据: %s —— 该条判据 ok=False 且 observed/expected 都在"
                  % title.split(":")[0],
                  (ev13.get("criteria") or {}).get(want_failed, {}).get("ok") is False
                  and "observed" in (ev13.get("criteria") or {}).get(want_failed, {})
                  and "expected" in (ev13.get("criteria") or {}).get(want_failed, {}),
                  json.dumps((ev13.get("criteria") or {}).get(want_failed, {}).get("observed"),
                             ensure_ascii=False)[:200])
        if want_failed == "b_plugin_list_uid_exact" and "criteria" in ev13:
            obs = ev13["criteria"][want_failed]["observed"]
            check("任务 5xx 判据: 负控2 逐字符比对 —— 前缀完全相同仍判否（diff_count=1，差异位在末位）",
                  obs["diff_count"] == 1 and obs["mismatch_indexes"] == [len(FIXTURE_UID) - 1]
                  and obs["checksum_matches_expected"] is False,
                  (obs["diff_count"], obs["mismatch_indexes"]))

    # 逐字符比对器本身的负控（防「前缀相同/同名/同版本」被当成相同）
    m_ok, m_diff, m_note = exact_str_match("abc", "abd")
    check("exact_str_match: 前缀相同但末位不同 ⇒ 否", (not m_ok) and m_diff == [2] and "逐字符不一致" in m_note,
          (m_ok, m_diff))
    m_ok2, _d, _n = exact_str_match("a" * 64, "a" * 64 + "b")
    check("exact_str_match: 一方是另一方的前缀（长度不同）⇒ 否", not m_ok2)
    m_ok3, _d3, _n3 = exact_str_match(FIXTURE_UID, FIXTURE_UID)
    check("exact_str_match: 完全相同 ⇒ 是", m_ok3)
    check("exact_str_match: expected 为空 ⇒ 一律否（fail-closed）", not exact_str_match("abc", "")[0])
    check("exact_str_match: observed 为空 ⇒ 否", not exact_str_match(None, "abc")[0])

    # 4xx 必须立即停手，且**不得**触发回读
    rb_called = {"n": 0}

    def _counting_readback():
        rb_called["n"] += 1
        return True, {"all_true": True}

    class FC401(FakeConsole):
        def get(self, p):
            return 401, {"message": "unauthorized"}

    v4xx, _d4, p4xx = wait_task(FC401({}), "t", timeout_s=5, interval=0, sleep=lambda s: None,
                                mono=lambda: 0.0, readback=_counting_readback)
    check("4xx: 立即停手（1 次轮询）且不触发回读",
          v4xx == "http_error" and len(p4xx) == 1 and rb_called["n"] == 0,
          "verdict=%s polls=%d readback_calls=%d" % (v4xx, len(p4xx), rb_called["n"]))

    # 回读不可用（未注入）时，5xx 绝不当作成功
    class FC5xxNoRb(FakeConsole):
        def get(self, p):
            return 500, dict(NOT_FOUND_BODY)

    v_norb, d_norb, _p = wait_task(FC5xxNoRb({}), "t", timeout_s=5, interval=0, sleep=lambda s: None,
                                   mono=lambda: 0.0)
    check("5xx 且无回读 ⇒ 仍 http_error（不把 http_error 当成功）",
          v_norb == "http_error" and (d_norb.get("task_route_error") or {}).get("readback", {}).get("available") is False,
          v_norb)

    # 有界重试次数可参数化（证明是「有界」而不是无限重试）
    st_r = {"n": 0}

    class FC5xxR1(FakeConsole):
        def get(self, p):
            st_r["n"] += 1
            return 500, dict(NOT_FOUND_BODY)

    seen_rb = {"n": 0}

    def _rb_r1():
        seen_rb["n"] += 1
        return False, {"all_true": False, "failed_criteria": ["a_provider_in_builtin"]}

    v_r1, _dr1, p_r1 = wait_task(FC5xxR1({}), "t", timeout_s=5, interval=0, sleep=lambda s: None,
                                 mono=lambda: 0.0, readback=_rb_r1, http_5xx_retries=1)
    check("5xx 有界重试: http_5xx_retries=1 ⇒ 共 2 次 5xx 后回读 1 次",
          v_r1 == "http_error" and len(p_r1) == 2 and seen_rb["n"] == 1,
          "polls=%d readback=%d" % (len(p_r1), seen_rb["n"]))

    # 14) 端到端正控：install 任务 500 ⇒ 回读补证 ⇒ **流程继续**建凭据（全程离线，不联网）
    class FakeApplyConsole(FakeConsole):
        def __init__(self):
            super().__init__({})
            self.poll_n = 0
            self.cred_written = False

        def get(self, p):
            self.calls.append(("GET", p))
            if p.startswith(ROUTE_TASKS + "/"):
                self.poll_n += 1
                if self.poll_n == 1:
                    return 200, {"status": "running"}
                return 500, dict(NOT_FOUND_BODY)
            if p == ROUTE_BUILTIN_LIST:
                return 200, [{"id": MARKET_PROVIDER, "name": TOOL_NAME, "label": {"en_US": "Neo4j"},
                              "type": "builtin"},
                             {"id": PROVIDER, "name": TOOL_NAME, "label": {"en_US": "Neo4j (fork)"},
                              "type": "builtin"}]
            if p == ROUTE_PLUGIN_LIST:
                return 200, {"plugins": [
                    {"id": "inst-market", "installation_id": "inst-market", "plugin_id": MARKET_PLUGIN_ID,
                     "plugin_unique_identifier": "nikola_milosevic/neo4j_query:0.2.0@" + "b" * 64,
                     "name": "neo4j_query", "version": "0.2.0", "checksum": "b" * 64,
                     "source": "marketplace", "verified": True},
                    {"id": "inst-fork", "installation_id": "inst-fork", "plugin_id": PLUGIN_ID,
                     "plugin_unique_identifier": FIXTURE_UID, "name": "neo4j_cypher_query",
                     "version": "0.1.1", "checksum": uid_checksum(FIXTURE_UID),
                     "source": "package", "verified": False}], "total": 2}
            if p == ROUTE_TOOLS % PROVIDER_QUOTED:
                return 200, _fixture_tools()
            if p == ROUTE_TOOLS % pb.quoted_provider():
                return 200, [{"name": TOOL_NAME, "parameters":
                              [{"name": n, "form": "form"} for n in EXPECTED_FORMS]}]
            if p == ROUTE_CREDENTIALS % PROVIDER_QUOTED:
                if self.cred_written:
                    return 200, [{"id": "cred-fork-1", "name": CRED_NAME, "provider": PROVIDER,
                                  "credential_type": CREDENTIAL_TYPE, "is_default": True,
                                  "visibility": CREDENTIAL_VISIBILITY,
                                  "credentials": {"NEO4J_URL": EXPECTED_BOLT,
                                                  "NEO4J_USERNAME": EXPECTED_USER,
                                                  "NEO4J_PASSWORD": "***masked***"}}]
                return 200, []
            return 404, {}

        def post(self, p, payload):
            self.calls.append(("POST", p, payload))
            if p == ROUTE_INSTALL_PKG:
                # 真机 task_id：01a0eb3f-4079-7669-a722-a6b3bb91cde1
                return 200, {"task_id": "01a0eb3f-4079-7669-a722-a6b3bb91cde1"}
            if p == ROUTE_ADD % PROVIDER_QUOTED:
                self.cred_written = True
                return 200, {"id": "cred-fork-1"}
            return 404, {}

        def post_multipart(self, path, field, filename, payload):
            self.calls.append(("POST-MP", path, field, filename))
            return 200, {"unique_identifier": FIXTURE_UID}, ""

    e2e_console = FakeApplyConsole()
    real_dify_env = pb.dify_env
    pb.dify_env = lambda: ({"DIFY_BASE_URL": "http://fixture.invalid", "DIFY_EMAIL": "fixture@invalid",
                            "DIFY_PASSWORD": "fixture"}, "fixture:.env")
    os.environ["CC_M4_SELFTEST_PASSWORD"] = "fixture-not-a-real-secret"
    try:
        e2e_args = argparse.Namespace(selftest=False, check=False, apply=True, force=False,
                                      force_install=True, skip_install=False, evidence_dir=None,
                                      password_env="CC_M4_SELFTEST_PASSWORD", task_timeout=10.0,
                                      task_interval=0.0)
        rc_e2e = cmd_apply(e2e_args, console_factory=lambda: e2e_console)
    finally:
        pb.dify_env = real_dify_env
        os.environ.pop("CC_M4_SELFTEST_PASSWORD", None)
    check("端到端正控: 任务 500 + 回读全真 ⇒ cmd_apply 退出码 0（判据切换后流程继续）", rc_e2e == 0, "rc=%s" % rc_e2e)
    check("端到端正控: 确实继续走到凭据写入（POST /add 被调用）", e2e_console.cred_written is True,
          "cred_written=%s" % e2e_console.cred_written)
    check("端到端正控: 确有一次 500 轮询（判据来源切换确被触发）",
          e2e_console.poll_n == TASK_HTTP_5XX_RETRIES + 2, "poll_n=%s" % e2e_console.poll_n)

    print("\nselftest: %d case(s) failed" % bad)
    return 0 if bad == 0 else 2


# ────────────────────────────────── --check ──────────────────────────────────
def cmd_check(args, console_factory=None):
    select_package(args)                     # ★ R-59：按 --pkg / --plugin-version 选定包
    report = {"mode": "check", "checked_at_utc": pb.now_utc(), "plugin_id": PLUGIN_ID,
              "plugin_version": PLUGIN_VERSION, "provider": PROVIDER, "pkg": PKG_PATH}
    try:
        pw, src = pb.resolve_password(args)
    except pb.Refused as e:
        print("拒绝：口令来源不可用（%s）" % e)
        return 5
    report["password_source"] = src
    print("口令：已取得（来源 %s，账号 %s，长度 %d；值不打印）"
          % (src["source_path"], src["auth_user"], src["password_len"]))

    env, env_path = pb.dify_env()
    c = (console_factory or (lambda: _connect(env)))()
    report["console"] = {"base": env["DIFY_BASE_URL"], "env_file": env_path}
    if isinstance(c, tuple):
        c, ok, st = c
        report["console"].update({"login_status": st, "login_ok": ok})
        print("console login HTTP %s ok=%s" % (st, ok))
        if not ok:
            archive_failure(args.evidence_dir, "check.json", report, pw)
            return 3

    pst, rows = read_builtin_providers(c)
    ids = [r["id"] for r in rows]
    report["builtin_providers"] = {"status": pst, "count": len(rows), "ids": ids}
    print("builtin providers HTTP %s n=%d" % (pst, len(rows)))
    report["market_plugin_present"] = MARKET_PROVIDER in ids
    report["fork_plugin_present"] = PROVIDER in ids
    print("market plugin %s present=%s" % (MARKET_PROVIDER, MARKET_PROVIDER in ids))
    print("fork   plugin %s present=%s" % (PROVIDER, PROVIDER in ids))

    ist, inst = read_installed_plugins(c)
    report["installed_plugins"] = inst
    report["fork_installation"] = pick_plugin(inst, PLUGIN_ID)
    report["market_installation"] = pick_plugin(inst, MARKET_PLUGIN_ID)
    print("plugin/list HTTP %s total=%s count=%s" % (ist, inst.get("total"), inst.get("count")))
    print("fork   installation: %s" % json.dumps(report["fork_installation"], ensure_ascii=False))
    print("market installation: %s" % json.dumps(report["market_installation"], ensure_ascii=False))
    if not report["market_plugin_present"]:
        report["verdict"] = "market_plugin_missing_runbook_redline_violated"
        archive_failure(args.evidence_dir, "check.json", report, pw)
        print("异常：市场插件不在 builtin 列表里（红线要求并存）——停手上报")
        return 1
    if not report["fork_plugin_present"]:
        report["verdict"] = "fork_plugin_not_installed"
        print("fork 插件尚未安装 ⇒ --check 非零（用 --apply 安装）")
        archive_failure(args.evidence_dir, "check.json", report, pw)
        return 4

    tst, tools = read_tool_face(c, PROVIDER_QUOTED)
    report["fork_tool_face"] = {"status": tst, "tools": tools}
    report["fork_provider_name"] = PROVIDER
    report["fork_tool_names"] = [t["name"] for t in tools]
    ok_forms, detail = judge_forms(tools)
    report["form_judgement"] = detail
    report["form_judgement_ok"] = ok_forms
    print("fork tools face HTTP %s" % tst)
    for t in tools:
        print("   tool=%s" % t["name"])
        for p in t["parameters"]:
            print("      %-22s form=%r required=%r default=%r"
                  % (p["name"], p["form"], p["required"], p["default"]))
    print("★ form 判据: %s %s" % ("PASS" if ok_forms else "FAIL", detail["errors"]))

    cst, snap = read_credentials(c, PROVIDER_QUOTED, pw)
    state, hit = judge_credentials(snap)
    report["fork_credentials"] = {"status": cst, "count": snap["count"], "slots": slots(snap),
                                  "state": state,
                                  "credential_id": (hit or {}).get("id"),
                                  "credential_name": (hit or {}).get("name"),
                                  "password_len": src.get("password_len"),
                                  "mask_evidence": None if not snap["credentials"] else snap["credentials"][0]["sanitized"]}
    print("fork credentials HTTP %s count=%s state=%s credential_id=%s"
          % (cst, snap["count"], state, (hit or {}).get("id")))
    print("slots: %s" % json.dumps(report["fork_credentials"]["slots"], ensure_ascii=False))

    mst, mtools = read_tool_face(c, pb.quoted_provider())
    report["market_tool_face"] = {"status": mst, "tools": mtools}
    print("market tools face HTTP %s tools=%s" % (mst, [t["name"] for t in mtools]))

    if not ok_forms:
        report["verdict"] = "form_criterion_failed"
        archive_failure(args.evidence_dir, "check.json", report, pw)
        return 4
    if state != "converged":
        report["verdict"] = "credentials_not_converged"
        archive_failure(args.evidence_dir, "check.json", report, pw)
        return 3
    report["verdict"] = "converged"
    if args.evidence_dir:
        pb.write_evidence(args.evidence_dir, "check.json", report, pw)
    return 0


def _connect(env):
    c = PluginConsole(env["DIFY_BASE_URL"])
    ok, st = c.login(env["DIFY_EMAIL"], env["DIFY_PASSWORD"])
    return c, ok, st


# ────────────────────────────────── --apply ──────────────────────────────────
def cmd_apply(args, console_factory=None):
    select_package(args)                     # ★ R-59：按 --pkg / --plugin-version 选定包
    try:
        pw, src = pb.resolve_password(args)
    except pb.Refused as e:
        print("拒绝：口令来源不可用（%s）" % e)
        return 5
    try:
        creds = validate_inputs(PROVIDER, EXPECTED_BOLT, EXPECTED_USER, pw)
    except Refused as e:
        print("拒绝写入：%s" % e)
        return 2

    package = None
    pkg_sig = None
    if not args.skip_install:
        b = open(PKG_PATH, "rb").read()
        package = {"path": PKG_PATH, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest(),
                   "filename": os.path.basename(PKG_PATH)}
        print("待装包：%s bytes=%d sha256=%s" % (package["path"], package["bytes"], package["sha256"]))
        pkg_sig = pkg_signature_facts(PKG_PATH)
        print("包签名自检：zip_comment_len=%d has_signature=%s entries=%d"
              % (pkg_sig["zip_comment_len"], pkg_sig["has_signature"], pkg_sig["entries"]))
        if not pkg_sig["has_signature"]:
            print("注意：本包不带 Dify 签名（zip comment 为空）。若目标部署 %s=true，upload 必被拒 —— "
                  "这是部署侧策略，不是包的问题。" % FORCE_VERIFYING_SIGNATURE_ENV)

    report = {"mode": "apply", "applied_at_utc": pb.now_utc(), "plugin_id": PLUGIN_ID,
              "plugin_version": PLUGIN_VERSION, "provider": PROVIDER, "package": package,
              "package_signature": pkg_sig,
              "password_source": src, "submitted_keys": sorted(creds.keys()),
              "submitted_values_masked": {k: ("<provided>" if k == "NEO4J_PASSWORD" else v)
                                          for k, v in creds.items()},
              "idempotency_anchor": {"name": CRED_NAME, "type": CREDENTIAL_TYPE,
                                     "visibility": CREDENTIAL_VISIBILITY},
              "expected_forms": dict(EXPECTED_FORMS)}

    env, env_path = pb.dify_env()
    c = (console_factory or (lambda: _connect(env)))()
    if isinstance(c, tuple):
        c, ok, st = c
        report["console"] = {"base": env["DIFY_BASE_URL"], "env_file": env_path, "login_status": st, "login_ok": ok}
        print("console login HTTP %s ok=%s" % (st, ok))
        if not ok:
            report["verdict"] = "login_failed"
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            return 3
    else:
        report["console"] = {"base": env["DIFY_BASE_URL"], "env_file": env_path, "login_status": "injected",
                             "login_ok": True}

    # ── 装前回读 ──
    pst, rows = read_builtin_providers(c)
    ids = [r["id"] for r in rows]
    report["pre"] = {"providers_status": pst, "count": len(rows), "ids": ids,
                     "market_present": MARKET_PROVIDER in ids, "fork_present": PROVIDER in ids}
    print("装前 builtin HTTP %s n=%d fork_present=%s market_present=%s"
          % (pst, len(rows), PROVIDER in ids, MARKET_PROVIDER in ids))
    prst, prinst = read_installed_plugins(c)
    report["pre_installed_plugins"] = prinst
    report["pre_market_installation"] = pick_plugin(prinst, MARKET_PLUGIN_ID)
    report["pre_fork_installation"] = pick_plugin(prinst, PLUGIN_ID)
    print("装前 plugin/list HTTP %s total=%s market_installation_id=%s fork_installation_id=%s"
          % (prst, prinst.get("total"), (report["pre_market_installation"] or {}).get("installation_id"),
             (report["pre_fork_installation"] or {}).get("installation_id")))
    if MARKET_PROVIDER not in ids:
        report["verdict"] = "market_plugin_missing_stop"
        archive_failure(args.evidence_dir, "apply.json", report, pw)
        print("异常：市场插件不在列表（红线要求并存）——停手上报")
        return 1

    # ── 上传 + 安装（若尚未安装，或 --force-install）──
    if (PROVIDER not in ids) or args.force_install:
        try:
            with open(PKG_PATH, "rb") as fh:
                payload = fh.read()
        except OSError as e:
            print("拒绝：读不到包 %s（%s）" % (PKG_PATH, e))
            return 2
        ust, uresp, utext = c.post_multipart(ROUTE_UPLOAD_PKG, "pkg", os.path.basename(PKG_PATH), payload)
        report["upload"] = {"route": ROUTE_UPLOAD_PKG, "field": "pkg", "status": ust,
                            "response": scrub(uresp, pw)}
        print("upload/pkg HTTP %s" % ust)
        if ust != 200:
            report["upload"]["body_truncated"] = scrub(utext, pw)
            report["upload"]["body_len"] = len(utext or "")
            try:
                detect_signature_gate(ust, utext or uresp)
                detect_forbidden(ust, utext or uresp)
            except BlockedSignature as e:
                # ★ BLOCKED（不是 FAIL）：包与契约都没问题，是部署侧的密码学闸。
                report["verdict"] = "blocked_by_signature_verification"
                report["blocked"] = {"kind": "signature_verification", "exit_code": 6,
                                     "exception": "BlockedSignature", "message": str(e),
                                     "raw_body_truncated": scrub(utext, pw)}
                report["diagnosis"] = diagnosis_signature_blocked(
                    pkg_sig, {FORCE_VERIFYING_SIGNATURE_ENV: read_env_value(FORCE_VERIFYING_SIGNATURE_ENV, env)})
                archive_failure(args.evidence_dir, "apply.json", report, pw)
                print("停止（BLOCKED，非 FAIL）：%s" % e)
                print("判断：本部署强制插件签名校验；本地构建的包没有也无法获得 Dify 的 RSA 签名。")
                print("已存档响应体与机制诊断，停手上报；不绕过、不改部署配置、不重启容器。")
                return 6
            except Forbidden as e:
                report["verdict"] = "blocked_by_official_only_gate"
                report["blocked"] = {"kind": "official_only", "exit_code": 6,
                                     "exception": "Forbidden", "message": str(e),
                                     "raw_body_truncated": scrub(utext, pw)}
                archive_failure(args.evidence_dir, "apply.json", report, pw)
                print("停止（BLOCKED，非 FAIL）：%s" % e)
                print("判断：该部署把插件安装限制在官方/市场来源。不试绕过，停手上报。")
                return 6
            report["verdict"] = "upload_failed"
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            print("上传失败（HTTP %s），已存档响应体，停手" % ust)
            return 1
        detect_forbidden(ust, uresp)
        try:
            uid = parse_unique_identifier(uresp)
        except Refused as e:
            report["verdict"] = "unique_identifier_unusable"
            report["upload"]["error"] = str(e)
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            print("上传响应无法取用：%s" % e)
            return 1
        report["unique_identifier"] = uid
        print("unique_identifier = %s" % uid)

        ist, iresp = install_pkg(c, uid)
        report["install"] = {"route": ROUTE_INSTALL_PKG, "status": ist,
                             "body_sent": {"plugin_unique_identifiers": [uid]},
                             "response": scrub(iresp, pw)}
        print("install/pkg HTTP %s %s" % (ist, json.dumps(scrub(iresp, pw), ensure_ascii=False)[:300]))
        if ist not in (200, 202):
            detect_forbidden(ist, iresp)
            report["verdict"] = "install_start_failed"
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            print("安装请求失败（HTTP %s），已存档响应体，停手" % ist)
            return 1
        tid = extract_task_id(iresp)
        report["install"]["task_id"] = tid
        if not tid:
            report["verdict"] = "no_task_id"
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            print("安装响应里没有 task_id：%s" % sorted(iresp.keys()))
            return 1
        # ★ 判据来源：5xx 后由「只读权威回读」补证（uid 逐字符比对，见 readback_install_state）
        verdict, detail, polls = wait_task(c, tid, timeout_s=args.task_timeout,
                                           interval=args.task_interval,
                                           readback=lambda: readback_install_state(c, uid))
        report["install"]["task_verdict"] = verdict
        report["install"]["task_detail"] = detail
        report["install"]["polls"] = polls
        if isinstance(detail, dict):
            if "task_route_error" in detail:
                report["install"]["task_route_error"] = detail["task_route_error"]
            if "readback_evidence" in detail:
                report["install"]["readback_evidence"] = detail["readback_evidence"]
        print("安装任务 %s -> %s（轮询 %d 次）" % (tid, verdict, len(polls)))
        if verdict == VERDICT_SUCCESS_VIA_READBACK:
            ev = report["install"].get("readback_evidence") or {}
            print("判据来源切换：任务接口 HTTP %s（%s）⇒ 改用只读回读补证；all_true=%s"
                  % (detail.get("http"), (detail.get("task_route_error") or {}).get("marker"),
                     ev.get("all_true")))
            for k in sorted((ev.get("criteria") or {}).keys()):
                print("   回读判据 %s ok=%s" % (k, ev["criteria"][k].get("ok")))
        if not task_verdict_allows_continue(verdict):
            report["verdict"] = "install_task_%s" % verdict
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            print("安装任务未成功（%s），已存档，停手" % verdict)
            return 1
    else:
        report["install"] = {"skipped": "provider 已存在于 builtin 列表（幂等命中）"}
        print("幂等命中：fork provider 已在 builtin 列表中，跳过 upload/install")

    # ── 装后回读：provider + 工具面 form ──
    ast, arows = read_builtin_providers(c)
    aids = [r["id"] for r in arows]
    report["post_providers"] = {"status": ast, "count": len(arows), "ids": aids,
                                "fork_present": PROVIDER in aids, "market_present": MARKET_PROVIDER in aids}
    print("装后 builtin HTTP %s n=%d fork_present=%s market_present=%s"
          % (ast, len(arows), PROVIDER in aids, MARKET_PROVIDER in aids))
    if PROVIDER not in aids:
        report["verdict"] = "provider_not_registered_after_install"
        archive_failure(args.evidence_dir, "apply.json", report, pw)
        return 1
    if MARKET_PROVIDER not in aids:
        report["verdict"] = "market_plugin_disappeared_stop"
        archive_failure(args.evidence_dir, "apply.json", report, pw)
        print("异常：装完后市场插件消失（红线要求并存）——停手上报")
        return 1

    # ★ 下游（W9 重绑 binding.yaml）需要的四个字段：uid / installation_id / credential id / provider+tool name
    inst_st, inst = read_installed_plugins(c)
    report["installed_plugins"] = inst
    report["fork_installation"] = pick_plugin(inst, PLUGIN_ID)
    report["market_installation"] = pick_plugin(inst, MARKET_PLUGIN_ID)
    print("装后 plugin/list HTTP %s total=%s" % (inst_st, inst.get("total")))
    print("fork   installation: %s" % json.dumps(report["fork_installation"], ensure_ascii=False))
    print("market installation: %s" % json.dumps(report["market_installation"], ensure_ascii=False))
    if report["fork_installation"] is None:
        report["verdict"] = "fork_installation_not_in_plugin_list"
        archive_failure(args.evidence_dir, "apply.json", report, pw)
        print("异常：provider 出现了但 plugin/list 里没有 fork 的 installation_id —— 停手上报")
        return 1
    # uid 交叉核对：upload 响应 vs plugin/list 回读（两者不一致必须暴露，不许挑一个好看的写）
    uid_upload = report.get("unique_identifier")
    uid_readback = (report["fork_installation"] or {}).get("plugin_unique_identifier")
    report["unique_identifier_crosscheck"] = {"from_upload_response": uid_upload,
                                             "from_plugin_list_readback": uid_readback,
                                             "match": (uid_upload == uid_readback) if uid_upload else None}
    if uid_upload and uid_readback and uid_upload != uid_readback:
        print("警告：upload 响应 uid 与 plugin/list 回读 uid 不一致 —— 如实记录，不挑好看的写")
    print("uid 交叉核对: %s" % json.dumps(report["unique_identifier_crosscheck"], ensure_ascii=False))

    tst, tools = read_tool_face(c, PROVIDER_QUOTED)
    ok_forms, detail = judge_forms(tools)
    report["fork_tool_face"] = {"status": tst, "tools": tools}
    report["fork_provider_name"] = PROVIDER
    report["fork_tool_names"] = [t["name"] for t in tools]
    report["form_judgement"] = detail
    report["form_judgement_ok"] = ok_forms
    for t in tools:
        print("   tool=%s" % t["name"])
        for p in t["parameters"]:
            print("      %-22s form=%r required=%r default=%r"
                  % (p["name"], p["form"], p["required"], p["default"]))
    print("★ form 判据: %s %s" % ("PASS" if ok_forms else "FAIL", detail["errors"]))

    mst, mtools = read_tool_face(c, pb.quoted_provider())
    report["market_tool_face"] = {"status": mst, "tools": mtools}
    print("市场插件工具面 HTTP %s tools=%s" % (mst, [t["name"] for t in mtools]))

    # ── 凭据 ──
    cst, pre = read_credentials(c, PROVIDER_QUOTED, pw)
    state, hit = judge_credentials(pre)
    report["cred_pre"] = {"status": cst, "count": pre["count"], "slots": slots(pre), "state": state,
                          "mask_evidence": None if not pre["credentials"] else pre["credentials"][0]["sanitized"]}
    print("凭据改前：HTTP %s count=%s state=%s" % (cst, pre["count"], state))

    if state == "converged" and not args.force:
        report["cred_action"] = {"op": "noop_already_converged"}
        report["cred_post"] = report["cred_pre"]
        print("幂等命中：凭据已收敛，未发起写入（--force 可强制重写）")
    else:
        st_c, rsp, act, el, retried = write_credential(c, creds, hit, do_write=print)
        report["cred_action"] = {"op": "POST /%s" % act, "route": (ROUTE_ADD if act == "add" else ROUTE_UPDATE) % "<provider>",
                                 "status": st_c, "elapsed_s": el, "cold_start_retry": retried,
                                 "credential_id_from_write_response":
                                     (rsp.get("id") or rsp.get("credential_id")) if isinstance(rsp, dict) else None,
                                 "response": scrub(pb.redact_credentials_block(rsp) if isinstance(rsp, dict) else rsp, pw)}
        print("凭据写入 %s -> HTTP %s elapsed=%ss retried=%s" % (act, st_c, el, retried))
        if st_c != 200:
            report["verdict"] = "credential_write_failed"
            archive_failure(args.evidence_dir, "apply.json", report, pw)
            return 1

    pst2, post = read_credentials(c, PROVIDER_QUOTED, pw)
    pstate, phit = judge_credentials(post)
    report["cred_post"] = {"status": pst2, "count": post["count"], "slots": slots(post), "state": pstate,
                           "credential_id": (phit or {}).get("id"),
                           "credential_name": (phit or {}).get("name"),
                           "credential_type": (phit or {}).get("credential_type"),
                           "visibility": (phit or {}).get("visibility"),
                           "password_len": src.get("password_len"),
                           "submitted": "<provided>" if post["credentials"] else None}
    print("凭据改后：HTTP %s count=%s state=%s credential_id=%s"
          % (pst2, post["count"], pstate, (phit or {}).get("id")))
    print("slots: %s" % json.dumps(report["cred_post"]["slots"], ensure_ascii=False))

    report["cred_diff"] = {
        "count": "%s -> %s" % (report["cred_pre"]["count"], report["cred_post"]["count"]),
        "state": "%s -> %s" % (state, pstate),
        "names": "%s -> %s" % (report["cred_pre"]["slots"]["names"], report["cred_post"]["slots"]["names"]),
        "urls": "%s -> %s" % (report["cred_pre"]["slots"]["urls"], report["cred_post"]["slots"]["urls"]),
        "password_lens": "%s -> %s" % (report["cred_pre"]["slots"]["password_lens"],
                                       report["cred_post"]["slots"]["password_lens"]),
    }
    print("凭据 diff: %s" % json.dumps(report["cred_diff"], ensure_ascii=False))

    report["honest_boundaries"] = [
        "回读被服务端掩码 ⇒ 「凭据条目存在且非空」可证，口令逐字一致不可证。",
        "掩码为 len-preserving 且保留首尾各 2 字符 ⇒ 掩码值本身含口令片段，本脚本只登记形态/长度/星号数。",
        "仍然沿用超级用户 neo4j 账号（已登记偏离：shared/model/kgt-tools.yaml read_only_role_required: true）。",
        "max_records 硬上限 1000 与 allow_write_queries=false 由 form: form 保证 LLM 不可见；",
        "写保护由上游 tools/neo4j_cypher_query.py 的 EXPLAIN preflight 兜底（allow_write=False 只许 r/s）。",
        "query 长度上限 2000 字符（上游 _validate_query_basic），未改动。",
        "「装好且 form 正确」≠「Agent 能自主查询」：需 App 重绑 + 冒烟，属后续独立步骤。",
    ]
    if ok_forms and pstate == "converged":
        report["verdict"] = "converged"
    elif not ok_forms:
        report["verdict"] = "form_criterion_failed"
    else:
        report["verdict"] = "written_but_not_converged"
    if args.evidence_dir:
        pb.write_evidence(args.evidence_dir, "apply.json", report, pw)
    if not ok_forms:
        return 4
    return 0 if pstate == "converged" else 1


# ────────────────────────────────── main ──────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="安装本地 fork 插件 cc-ops/neo4j_cypher_query 0.1.1 并建凭据（只走 Console API）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--selftest", action="store_true", help="离线负控自检（夹具 + 假 session，绝不联网）")
    g.add_argument("--check", action="store_true", help="只读：核已装 + 核 form 判据 + 核凭据收敛")
    g.add_argument("--apply", action="store_true", help="幂等写入：upload -> install -> 回读 -> 建凭据")
    ap.add_argument("--force", action="store_true", help="已收敛时仍强制重写凭据")
    ap.add_argument("--force-install", action="store_true", help="provider 已在时也重跑 upload/install")
    ap.add_argument("--skip-install", action="store_true", help="跳过 upload/install，只处理凭据")
    ap.add_argument("--evidence-dir", default=None, help="证据目录（UTF-8 无 BOM，LF；含 history/ 归档）")
    ap.add_argument("--password-env", default=None, help="从该环境变量取口令（覆盖默认来源）")
    ap.add_argument("--task-timeout", type=float, default=300.0, help="安装任务轮询超时秒数")
    ap.add_argument("--task-interval", type=float, default=3.0, help="安装任务轮询间隔秒数")
    # ★ R-59：包选择（默认 0.1.1 不变）。装 0.1.0 与 binding/kgt-tools 的声明 uid 一致。
    ap.add_argument("--plugin-version", default=None,
                    help="选 dist 下 cc-ops-neo4j_cypher_query-<版本>.difypkg（默认 %s）" % PLUGIN_VERSION)
    ap.add_argument("--pkg", default=None, help="显式指定 .difypkg 路径（优先于 --plugin-version）")
    a = ap.parse_args(argv)
    if a.selftest:
        return cmd_selftest(a)
    if a.check:
        return cmd_check(a)
    return cmd_apply(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Forbidden as exc:
        print("停止（拒绝绕过）：%s" % exc)
        sys.exit(2)
    except pb.Refused as exc:
        print("拒绝：%s" % exc)
        sys.exit(2)
    except Refused as exc:
        print("拒绝：%s" % exc)
        sys.exit(2)
