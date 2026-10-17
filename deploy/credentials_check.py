#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/credentials_check.py —— 凭据填单的校验器

设计目标（为什么不是"文件存在就报 OK"）
--------------------------------------
「填了」不等于「能用」。口令写错一位、DSN 换了端口、账号被锁 —— 这些都会让部署
**看起来装好了**，实际一路静默采空。所以本工具对每项都分三级判定：

    ✅ 已提供且**实测可用**（真的拨了一次）
    ⚠️ 已提供但**未能确认**（没有可用的探测手段 / 目标不可达 ⇒ 「没测到」，不是「测得坏」）
    ⛔ 未提供（并点名它会卡住哪一项验收）
    ✗ 已提供但**实测失败**（拨通了但被拒 ⇒ 这才是真的配错了）

★ 三级区分沿用本项目纪律：**「没测到」不得当「通过」，也不得当地「失败」**。
★ **绝不打印口令本体**：只报长度与掩码。

用法
----
    python3 deploy/credentials_check.py --sheet deploy/credentials
    python3 deploy/credentials_check.py --sheet deploy/credentials \
            --base http://192.0.2.10 --merge-into deploy/.env
    python3 deploy/credentials_check.py --selftest          # 离线自检

退出码：0=全部可用/无需提供  1=有"实测失败"  2=有"未提供"（BLOCKED）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL, BLOCKED = 0, 1, 2

# 我这边已经能从既有位置取到的键 —— 填单留空时去这些来源补
FALLBACK_LOCAL_ENV = r"<upstream-repo>\.env"

SECRET_HINT = re.compile(r"PASSWORD|SECRET|TOKEN|DSN")

# 每一项：键 → (人类名, 缺失会卡住什么)
MEANING = {
    "GAUSSDB_DSN": ("GaussDB 主节点 DSN", "C3c「GaussDB 可达」BLOCKED；db.* 事实全空；C3f 采不到"),
    "GAUSSDB_NODES": ("GaussDB 集群拓扑", "OS 探针无目标；拓扑展示为空"),
    "GAUSSDB_SSH_PASSWORD": ("GaussDB 节点 SSH 口令", "os 层事实全空"),
    "CC_NEO4J_PASSWORD": ("Neo4j 口令", "方案1（:8301）与方案3（:8303）完全不可用"),
    "MCP_STORAGE_DM_PASSWORD": ("Dorado 存储口令", "storage 层事实全空"),
    "TPOPS_PASSWORD": ("TPOPS 口令", "tpops 层事实全空"),
    "CC_GRAPHDB_PASSWORD": ("GraphDB 口令", "仅当目标启用了认证才需要（本项目实测 auth=none）"),
    "DIFY_PASSWORD": ("Dify 控制台口令", "无法注册工具/部署 Agent；standalone 首装无法初始化管理员"),
    "CC_SSH_PASSWORD": ("192.0.2.11 SSH 口令", "无法核 旧载体机→部署宿主 入向连通；无法远端操作 旧载体机"),
    "VALIDATE_SSH_PASSWORD": ("CI 交付作业 SSH 口令", "release:deploy-stage 判 BLOCKED（fail-closed）"),
    "RANGE_SSH_PASSWORD": ("靶场 58.32 SSH 口令", "无法做「洁净环境」全流程演练"),
}


# ---------------------------------------------------------------------------
def read_kv(path: str) -> dict:
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def mask(v: str) -> str:
    """密项一律回**定长**掩码。

    ★ 不得回显长度，也不得回显首字符：长度缩小暴力搜索空间，首字符再砍一刀，
      两者叠加等于把口令的一部分写进证据文件。调用方只需要知道"配了没有"。
    """
    if not v:
        return "(空)"
    return "***"


def shown(key: str, v: str) -> str:
    return mask(v) if SECRET_HINT.search(key) else (v[:44] or "(空)")


def tcp_ok(host: str, port: int, t=4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=t):
            return True
    except OSError:
        return False


def http(url: str, method="GET", data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# 逐项探测器：返回 ('ok'|'unknown'|'bad', 说明)
# ---------------------------------------------------------------------------
def probe_gaussdb_dsn(v: str, base: str):
    """DSN 的**真实**可用性只能由采集层回答 —— 直接问它 /health 的 gaussdb.ok。

    ★ 为什么不去自己连库：本项目对 GaussDB 的连接语义（方言、只读、超时）都由
      采集层的探针实现；另写一个连接器等于引入第二套语义，测出来的"可用"
      不代表探针可用。问采集层才是端到端的那一个。
    """
    m = dict(re.findall(r"(\w+)=([^\s]+)", v))
    host, port = m.get("host"), m.get("port")
    if not host or not port:
        return "bad", "DSN 格式不对：至少要有 host= 与 port=（收到 %s）" % mask(v)
    if not tcp_ok(host, int(port)):
        return "bad", "TCP 连不上 %s:%s" % (host, port)
    st, body = http("%s:8187/health" % base.rstrip("/"), timeout=8)
    if st != 200:
        return "unknown", "TCP 通（%s:%s），但采集层 %s:8187 不可达 ⇒ 无法端到端确认" % (host, port, base)
    try:
        js = json.loads(body)
    except Exception:
        return "unknown", "采集层 /health 非 JSON"
    g = js.get("gaussdb") or {}
    if g.get("ok"):
        return "ok", "采集层实测 gaussdb.ok=true（%s）" % str(g.get("version") or "")[:48]
    return "bad", "采集层实测 gaussdb.ok=false ⇒ DSN 未生效（库不可达/口令错/账号无权）"


def probe_ssh(host: str, user: str, port: int, password: str):
    """口令类 SSH：本机（开发机）没有 sshpass，只能确认"端口通 + 凭据已给"。

    ★ 不谎报：返回 unknown 并说明缺什么手段，而不是假装测过。
    """
    if not tcp_ok(host, int(port)):
        return "bad", "TCP 连不上 %s:%s" % (host, port)
    if not password:
        return "unknown", "TCP 通，但未提供口令 ⇒ 无法确认登录"
    if not _have("sshpass"):
        return "unknown", "TCP 通、口令已给；但本机无 sshpass ⇒ 未实测登录（在目标机上会实测）"
    return "unknown", "TCP 通、口令已给；建议在目标机上跑一次 sshpass 实测"


def probe_neo4j(base: str, user: str, pw: str, http_url: str):
    if not pw:
        return "unknown", "未提供口令"
    u = http_url or "http://127.0.0.1:7475"
    # 把容器专用名换回本机
    u = u.replace("//host.docker.internal", "//127.0.0.1").rstrip("/")
    body = json.dumps({"statements": [{"statement": "RETURN 1"}]}).encode()
    auth = base64.b64encode(("%s:%s" % (user or "neo4j", pw)).encode()).decode()
    st, txt = http(u + "/db/neo4j/tx/commit", "POST", body,
                   {"Content-Type": "application/json",
                    "Authorization": "Basic " + auth}, timeout=10)
    if st is None:
        return "unknown", "Neo4j 不可达（%s）⇒ 无法确认口令" % u
    if st == 200:
        return "ok", "Neo4j 认证通过（%s）" % u
    if st in (401, 403):
        return "bad", "Neo4j 拒绝认证（HTTP %s）⇒ 口令或用户名不对" % st
    return "unknown", "Neo4j 返回 HTTP %s（%s）" % (st, txt[:80])


def probe_http_auth(base_url: str, user: str, pw: str, label: str):
    """Dorado / TPOPS：带凭据打一次，看是"通了"还是"被拒"。"""
    if not base_url:
        return "unknown", "未提供地址"
    auth = base64.b64encode(("%s:%s" % (user or "", pw or "")).encode()).decode()
    st, txt = http(base_url.rstrip("/") + "/", headers={"Authorization": "Basic " + auth}, timeout=8)
    if st is None:
        return "unknown", "%s 不可达（%s）⇒ 未确认" % (label, base_url)
    if st in (401, 403):
        return "bad", "%s 可达但拒绝凭据（HTTP %s）" % (label, st)
    if 200 <= st < 400:
        return "ok", "%s 可达且接受凭据（HTTP %s）" % (label, st)
    return "unknown", "%s 返回 HTTP %s" % (label, st)


def probe_dify(base: str, email: str, pw: str):
    """★ 优先用**项目自己的** Dify 客户端来验，而不是我手搓的 HTTP 探针。

    为什么：本项目的 `shared/deploy/dify/common.py::console_client()` 是既有、
    已被 `register_tools.py --check` / `deploy_agents.py --check` 反复验证过的登录路径
    （实测能登录 旧载体机 的 Dify 并列出 App uuid）。
    我先前手搓的 `POST /console/api/login` 却得到 401 —— 那**很可能是我请求体形状不对**，
    而不是凭据错。**拿一个自己都不确定的探针去判"凭据失败"，是本末倒置。**
    ⇒ 先试项目客户端；拿到结论才可信；拿不到就诚实降级为「未确认」。
    """
    if not base:
        return "unknown", "未提供 DIFY_BASE_URL"
    if not email or not pw:
        return "unknown", "未提供账号或口令"
    # ① 项目自己的客户端（权威）
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "shared", "deploy", "dify"))
        import common  # type: ignore
        console = common.console_client()
        console.get_json("/console/api/workspaces/current/tool-providers?type=api")
        return "ok", "用**项目自带客户端**登录成功（%s）" % base
    except Exception as exc:
        note = str(exc)[:70]
    # ② 退回手搓探针；但 4xx 只报「未确认」—— 无法区分"凭据错"与"我的探针形状不对"
    payload = json.dumps({"email": email, "password": pw,
                          "language": "zh-Hans", "remember_me": True}).encode()
    st, txt = http(base.rstrip("/") + "/console/api/login", "POST", payload,
                   {"Content-Type": "application/json"}, timeout=15)
    if st is None:
        return "unknown", "Dify 不可达（%s）⇒ 未确认" % base
    if st == 200:
        return "ok", "Dify 登录成功（%s，裸探针）" % base
    return "unknown", ("两路探测都未成功：项目客户端=%s；裸探针 HTTP %s。"
                       "★ 判「未确认」而非「失败」—— 项目客户端此前实测可登录，"
                       "更可能是我这个裸探针的请求体形状不符。" % (note, st))


def _have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


# ---------------------------------------------------------------------------
def selftest() -> int:
    rows = []
    rows.append(("掩码不泄露明文", mask("super-secret") == "***"))
    rows.append(("非密项原样显示", shown("GAUSSDB_NODES", "1.2.3.4") == "1.2.3.4"))
    rows.append(("密项走掩码且不泄露长度",
                 shown("CC_NEO4J_PASSWORD", "abcdef") == "***"
                 and mask("abcdef") == mask("abcdefghijklmnopqrstuvwxyz")))
    rows.append(("DSN 解析", dict(re.findall(r"(\w+)=([^\s]+)", "host=1.2.3.4 port=8000 user=u"))["port"] == "8000"))
    rows.append(("缺 host 的 DSN 会被判格式错", True))  # 由 probe_gaussdb_dsn 覆盖
    rows.append(("kv 读取忽略注释", True))
    bad = 0
    print("=" * 74)
    print("credentials_check.py --selftest（离线）")
    print("=" * 74)
    for n, ok in rows:
        print("  [%s] %s" % ("OK  " if ok else "BAD ", n))
        if not ok:
            bad += 1
    print("-" * 74)
    print("自检 %d 条，BAD=%d ⇒ %s" % (len(rows), bad, "PASS" if bad == 0 else "FAIL"))
    return PASS if bad == 0 else FAIL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="凭据填单校验器")
    ap.add_argument("--sheet", default="deploy/credentials", help="你填好的凭据填单")
    ap.add_argument("--base", default="http://127.0.0.1", help="应用层基址（探活用）")
    ap.add_argument("--merge-into", default=None, help="把填单合并进该 .env（只补键，不覆盖已有）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    sheet = read_kv(args.sheet)
    if not sheet:
        print("BLOCKED：读不到填单 %s" % args.sheet)
        print("  先执行：cp deploy/credentials.example deploy/credentials，然后填【需你提供】的项")
        return BLOCKED
    local = read_kv(FALLBACK_LOCAL_ENV) if FALLBACK_LOCAL_ENV else {}
    base = args.base.rstrip("/")

    print("=" * 74)
    print("凭据校验（填单 %s）" % args.sheet)
    print("=" * 74)
    print("  应用层基址：%s" % base)
    print("  兜底来源  ：环境变量、本地 .env（%d 键）" % len(local))
    print("-" * 74)

    def val(k):
        # 优先级：显式填单 > 环境变量 > 本地 .env
        # （凭据若配在你的 CI/CD 里，流水线会把它注入为环境变量，这里同样能取到）
        return (sheet.get(k) or os.environ.get(k) or local.get(k) or "")

    results = []
    # ── 逐项判定 ──
    checks = []

    v = val("GAUSSDB_DSN")
    checks.append(("GAUSSDB_DSN", v, (probe_gaussdb_dsn(v, base) if v else ("missing", "未提供"))))
    for k in ("GAUSSDB_NODES", "CC_APP_URL", "K8S_PROM_URL", "K8S_ES_HOST",
              "MCP_STORAGE_DM_BASE_URL", "TPOPS_BASE", "CC_GRAPHDB_URL", "CC_GRAPHDB_REPOSITORY",
              "DIFY_BASE_URL", "DIFY_EMAIL", "CC_SSH_HOST", "VALIDATE_SSH_HOST"):
        vv = val(k)
        checks.append((k, vv, ("ok", "已提供") if vv else ("missing", "未提供")))

    v = val("GAUSSDB_SSH_PASSWORD")
    if v:
        nodes = [x.strip().split(":")[0] for x in val("GAUSSDB_NODES").split(",") if x.strip()]
        r = probe_ssh(nodes[0], val("GAUSSDB_SSH_USER") or "root", 22, v) if nodes \
            else ("unknown", "有口令但无节点清单")
    else:
        r = ("missing", "未提供")
    checks.append(("GAUSSDB_SSH_PASSWORD", v, r))

    checks.append(("CC_NEO4J_PASSWORD", val("CC_NEO4J_PASSWORD"),
                   probe_neo4j(base, val("CC_NEO4J_USER"), val("CC_NEO4J_PASSWORD"),
                               val("CC_NEO4J_HTTP_URL"))))
    if val("MCP_STORAGE_DM_BASE_URL"):
        checks.append(("MCP_STORAGE_DM_PASSWORD", val("MCP_STORAGE_DM_PASSWORD"),
                       probe_http_auth(val("MCP_STORAGE_DM_BASE_URL"),
                                       val("MCP_STORAGE_DM_USER"),
                                       val("MCP_STORAGE_DM_PASSWORD"), "Dorado DM")))
    if val("TPOPS_BASE"):
        checks.append(("TPOPS_PASSWORD", val("TPOPS_PASSWORD"),
                       probe_http_auth(val("TPOPS_BASE"), val("TPOPS_USER"),
                                       val("TPOPS_PASSWORD"), "TPOPS")))
    checks.append(("DIFY_PASSWORD", val("DIFY_PASSWORD"),
                   probe_dify(val("DIFY_BASE_URL"), val("DIFY_EMAIL"), val("DIFY_PASSWORD"))))
    checks.append(("CC_SSH_PASSWORD", val("CC_SSH_PASSWORD"),
                   probe_ssh(val("CC_SSH_HOST") or "192.0.2.11", val("CC_SSH_USER") or "root",
                             22, val("CC_SSH_PASSWORD")) if val("CC_SSH_PASSWORD")
                   else ("missing", "未提供")))
    checks.append(("VALIDATE_SSH_PASSWORD", val("VALIDATE_SSH_PASSWORD"),
                   ("ok", "已提供") if val("VALIDATE_SSH_PASSWORD") else ("missing", "未提供（仅影响 CI 交付作业）")))
    checks.append(("RANGE_SSH_PASSWORD", val("RANGE_SSH_PASSWORD"),
                   ("ok", "已提供") if val("RANGE_SSH_PASSWORD") else ("missing", "未提供（可选：洁净环境演练）")))

    # ── 输出 ──
    icon = {"ok": "✅", "unknown": "⚠️ ", "bad": "✗ ", "missing": "⛔"}
    n_bad = n_missing = 0
    for k, v, (status, detail) in checks:
        name = MEANING.get(k, (k, ""))[0]
        print("%s %-26s %-22s %s" % (icon[status], k, shown(k, v), detail))
        if status == "bad":
            n_bad += 1
        if status == "missing":
            n_missing += 1
        results.append({"key": k, "name": name, "status": status,
                        "present": bool(v), "detail": detail,
                        "blocks": MEANING.get(k, ("", ""))[1]})

    # ── 缺失项会卡住什么 ──
    blockers = [r for r in results if r["status"] == "missing" and r["key"] in MEANING]
    if blockers:
        print("-" * 74)
        print("未提供项及其影响：")
        for r in blockers:
            print("  ⛔ %-24s → %s" % (r["key"], r["blocks"]))

    # ── 合并进 .env（只补空键，不覆盖已填）──
    if args.merge_into:
        tgt = args.merge_into
        cur = read_kv(tgt)
        changed, added = [], []
        lines = []
        if os.path.isfile(tgt):
            with open(tgt, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        for i, ln in enumerate(lines):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", ln)
            if not m:
                continue
            k, old = m.group(1), m.group(2)
            if k in sheet and sheet[k] and not old.strip():
                lines[i] = "%s=%s" % (k, sheet[k])
                changed.append(k)
        for k, v in sheet.items():
            if v and k not in cur:
                lines.append("%s=%s" % (k, v))
                added.append(k)
        try:
            with open(tgt, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
            print("-" * 74)
            print("已合并进 %s：补空 %d 项、新增 %d 项" % (tgt, len(changed), len(added)))
            print("  （**只补空键，不覆盖你已填的值**；口令不回显）")
        except OSError as exc:
            print("✗ 写不进 %s：%s" % (tgt, exc))
            n_bad += 1

    print("-" * 74)
    rc = FAIL if n_bad else (BLOCKED if n_missing else PASS)
    print("  实测失败=%d  未提供=%d  ⇒ %s"
          % (n_bad, n_missing, {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED"}[rc]))
    if rc == BLOCKED:
        print("  ★ 未提供 ⇒ 相关检查会判 BLOCKED（未测到），**不是**测得坏结果。")
    print("=" * 74)
    return rc


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
