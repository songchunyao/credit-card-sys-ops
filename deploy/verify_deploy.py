#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/verify_deploy.py —— 容器化部署 · 验收矩阵执行器

为什么单独写一个验收器，而不是"起栈成功就算交付"
--------------------------------------------------
`docker compose up` 成功只证明**容器能启动**，不证明：载体连得上、工具面同形、
审计链真的在落盘、取数面能返回事实。本脚本把这些变成**可复算的断言**。

★ 只用标准库（urllib / subprocess / json）：install.sh 在**目标机**上直接调它，
  目标机可能只有一个裸 python3，没有 requests。装依赖才能跑验收本身就是坏设计。

四态退出码（沿用本项目既有语义，与 shared/verify/gate_runner.py 一致）
    0=PASS  1=FAIL（有项不达标）  2=BLOCKED（环境不满足，**不是**代码缺陷）
    优先级：BLOCKED > FAIL > PASS
  ★「没测到」与「测得坏结果」必须可区分 —— 这是本项目反复强调的纪律。

用法
----
    python3 deploy/verify_deploy.py --deploy-dir deploy --mode standalone
    python3 deploy/verify_deploy.py --base <目标机IP> --mode attached
    python3 deploy/verify_deploy.py --deploy-dir deploy --with-restart   # 危险的额外项
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

PASS, FAIL, BLOCKED = 0, 1, 2

# 契约端口 → 服务名（来源：shared/deploy/targets.yaml 与各 start_*.sh）
SERVICES = {
    "collect": 8187,
    "kg": 8301,
    "onto": 8302,
    "kgt": 8303,
    "live-facts": 8304,
    # :8305 方案3 补充臂（KG-T-dyn）。binding.yaml 的 provider_names 声明了它，
    # 容器编排也已提供（compose 的 kgt-dyn 服务）⇒ 验收矩阵必须覆盖它，
    # 否则"声明了 6 个 provider 而只验 5 个"会漏掉一个真实端点。
    "kgt-dyn": 8305,
}
CONTAINERS = {name: "cc-ops-" + name for name in SERVICES}


# ---------------------------------------------------------------------------
# 结果收集
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.items: list = []
        self.readings: dict = {}

    def add(self, cid: str, title: str, status: int, detail: str = "") -> None:
        self.items.append({"id": cid, "title": title,
                           "status": {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED"}[status],
                           "detail": detail})
        tag = {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED"}[status]
        mark = {PASS: "✓", FAIL: "✗", BLOCKED: "?"}[status]
        print("  [%-7s] %s %-38s %s" % (tag, mark, title, detail))

    def note(self, key: str, value) -> None:
        self.readings[key] = value

    def summary(self) -> tuple:
        n_fail = sum(1 for i in self.items if i["status"] == "FAIL")
        n_blk = sum(1 for i in self.items if i["status"] == "BLOCKED")
        n_pass = sum(1 for i in self.items if i["status"] == "PASS")
        if n_blk:
            rc = BLOCKED
        elif n_fail:
            rc = FAIL
        else:
            rc = PASS
        return n_pass, n_fail, n_blk, rc


# ---------------------------------------------------------------------------
# HTTP / Docker 小工具
# ---------------------------------------------------------------------------
def http(method: str, url: str, body=None, timeout: float = 8.0, headers=None):
    """返回 (status, text)。网络层失败抛 OSError，由调用方决定 PASS/FAIL/BLOCKED。"""
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:               # 有响应但 4xx/5xx
        return exc.code, exc.read().decode("utf-8", "replace")


def http_json(method: str, url: str, body=None, timeout: float = 8.0):
    status, text = http(method, url, body, timeout)
    try:
        return status, json.loads(text)
    except Exception:
        return status, None


def sh(cmd: list, timeout: float = 20.0):
    """跑外部命令；返回 (rc, stdout+stderr)。不抛异常（除可执行文件缺失）。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, "%s: not found" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timeout after %ss" % timeout


def have_docker() -> bool:
    return sh(["docker", "version", "--format", "{{.Server.Version}}"], 10)[0] == 0


def container_state(name: str) -> str:
    """healthy | running | exited | <空>"""
    rc, out = sh(["docker", "inspect", "-f",
                  "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                  name], 10)
    return out.strip() if rc == 0 else ""


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------
def c1_containers(rep: Report, docker_ok: bool) -> None:
    if not docker_ok:
        rep.add("C1", "容器健康", BLOCKED, "docker 不可用 ⇒ 未能测量（不是测得坏结果）")
        return
    bad, states = [], {}
    for name, cname in CONTAINERS.items():
        st = container_state(cname)
        states[name] = st or "(不存在)"
        if st not in ("healthy", "running"):
            bad.append("%s=%s" % (name, st or "(不存在)"))
    rep.note("container_states", states)
    # ★ 个数**从 CONTAINERS 推**，不写字面量：kgt-dyn 加进来后，这里一直写着"5 个"，
    #   与 selfcheck S7「6 个服务全部被验收器覆盖」自相矛盾（E2E 的 F4）。
    _n = len(CONTAINERS)
    if bad:
        rep.add("C1", "容器健康（%d 个）" % _n, FAIL, "异常：" + ", ".join(bad))
    else:
        rep.add("C1", "容器健康（%d 个）" % _n, PASS, "全部 healthy/running")


def c2_arch(rep: Report, docker_ok: bool) -> None:
    host = platform.machine()
    rep.note("host_arch", host)
    if not docker_ok:
        rep.add("C2", "镜像架构与本机一致", BLOCKED, "docker 不可用")
        return
    # ★ 不要再硬编码 `cc-ops/kg`（无 tag）—— 联调实测踩过：
    #   镜像实际打在 **CC_IMAGE_TAG**（如 ci-arm64-v1）上，`cc-ops/kg:latest` 根本不存在
    #   ⇒ 报 `No such image: cc-ops/kg:latest` 的**假红**。
    #   改成从**正在跑的容器**反查镜像 ID，与 tag 无关，也不会随 tag 规则变化而失效。
    rc, out = sh(["docker", "inspect", "-f", "{{.Image}}", CONTAINERS["kg"]], 10)
    img_ref = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
    if not img_ref:
        # 容器没跑（可能是纯"只验镜像"的场景）⇒ 退回按 .env 的 tag 拼
        tag = "latest"
        envf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        try:
            with open(envf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("CC_IMAGE_TAG="):
                        tag = line.split("=", 1)[1].strip() or "latest"
                        break
        except OSError:
            pass
        img_ref = "cc-ops/kg:%s" % tag
    rc2, out2 = sh(["docker", "image", "inspect", img_ref, "-f", "{{.Architecture}}"], 10)
    if rc2 != 0 or not out2.strip():
        rep.add("C2", "镜像架构与本机一致", FAIL,
                "读不到镜像架构（%s）：%s" % (img_ref, out2.strip()[:80]))
        return
    img_arch = out2.strip().splitlines()[0]
    norm = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
    rep.note("image_arch", img_arch)
    rep.note("image_ref_checked", img_ref)
    if norm.get(host, host) == norm.get(img_arch, img_arch):
        rep.add("C2", "镜像架构与本机一致", PASS,
                "host=%s image=%s（%s）" % (host, img_arch, img_ref))
    else:
        # 架构不一致的镜像**跑不起来**（exec format error）⇒ 是部署阻断，不是"没测到"
        rep.add("C2", "镜像架构与本机一致", FAIL,
                "host=%s 但 image=%s（%s）—— 离线包与目标机架构不符" % (host, img_arch, img_ref))


def _svc_health(rep: Report, cid: str, name: str, port: int, base: str) -> dict:
    """通用 /health 探活，返回 health JSON（失败时 {}）。"""
    url = "%s:%d/health" % (base, port)
    try:
        status, js = http_json("GET", url, timeout=8)
    except OSError as exc:
        rep.add(cid, "%s /health 可达" % name, FAIL, "%s 不可达：%s" % (url, exc))
        return {}
    if status != 200 or not isinstance(js, dict):
        rep.add(cid, "%s /health 可达" % name, FAIL, "HTTP %s（非 200 或非 JSON）" % status)
        return {}
    rep.add(cid, "%s /health 可达" % name, PASS, "HTTP 200")
    return js


def c3_collect(rep: Report, base: str, with_gather: bool = False) -> None:
    """采集层数据面（:8187）。

    ★★ 一次真机实测推翻了我最初的写法，记录在此以免后人重犯 ★★
    最初我断言 `/health.collect_contract.probe_sections` 非空 —— 那是照上游
    `docs/49` §1.3 写的，而 §1.3 描述的是**分层前的 monolith**。
    真机 `:8187/health` 的顶层键实测只有：
        collect_cache / collect_source / gaussdb / rules / service / ts / version
    **没有 collect_contract**，而且这不是故障，是**设计**：
      · `app_collect.py:8-10` 自述「/health 的 neo4j / rules / collect_contract 块为
        『未加载』提示；不 import service.knowledge_api → 不引入 Neo4j 依赖」；
      · `collect_api.py:1355-1360` 自述这三块「由知识面（service.knowledge_api）注册」。
    ⇒ 在「数据面单独部署」这个**正是本项目要交付的形态**上，缺 `collect_contract`
      是**正确的**。断言它等于断言一个已被架构演进删掉的东西（假红）。

    改后的断言反而更有鉴别力：它同时校验**分层边界**与**采集源模式**。
    """
    js = _svc_health(rep, "C3", "采集层 :8187", 8187, base)
    if not js:
        return

    # ── 数据面端点面：证明采集路由（探针入口）确实加载了 ──
    paths = _tool_paths(rep, base, 8187, "C3b", "采集面", prefix="/tools/")
    extra = ["/collect", "/verify"]
    face = sorted(set(paths) | {p for p in extra})
    rep.note("collect_tool_paths", paths)
    if len(paths) >= 6:
        rep.add("C3b", "采集面端点面非空", PASS,
                "%d 个 /tools/* 端点：%s" % (len(paths), ", ".join(x.split("/")[-1] for x in paths)))
    else:
        rep.add("C3b", "采集面端点面非空", FAIL,
                "只发现 %d 个 /tools/* 端点（期望 ≥6）⇒ 采集路由未完整加载：%s" % (len(paths), face))

    # ── GaussDB 连通（数据能不能真取到的前提）──
    gaussdb = (js.get("gaussdb") or {})
    rep.note("collect_gaussdb", {k: gaussdb.get(k) for k in ("ok", "version")})
    if gaussdb.get("ok"):
        rep.add("C3c", "GaussDB 可达", PASS,
                str(gaussdb.get("version") or "")[:64] or "gaussdb.ok=true")
    else:
        # 载体/数据库不可达属"环境未通"，不是代码缺陷 ⇒ BLOCKED，但必须点名
        rep.add("C3c", "GaussDB 可达", BLOCKED,
                "gaussdb.ok 非真 ⇒ DSN 未配或数据库不可达；数据面将为空")

    # ── 分层边界：数据面**必须**没加载知识面（docs/50 §4.1 的验收点）──
    #   真机实测：rules.ok=false 且 error 明写「service.knowledge_api 未导入」。
    #   这不是缺陷，而是"分层成功"的**正面证据**。反过来若它变成 true，
    #   说明有人把知识面又缠回数据面了 —— 那才是要拦的。
    rules = (js.get("rules") or {})
    rep.note("collect_rules", {"ok": rules.get("ok"), "error": str(rules.get("error") or "")[:120]})
    if rules.get("ok") is False:
        rep.add("C3d", "分层边界（数据面未缠知识面）", PASS,
                "rules.ok=false，理由：%s" % (str(rules.get("error") or "")[:70]))
    elif rules.get("ok") is True:
        rep.add("C3d", "分层边界（数据面未缠知识面）", FAIL,
                "rules.ok=true ⇒ 数据面加载了知识面，分层被破坏（docs/50 §4.1）")
    else:
        rep.add("C3d", "分层边界（数据面未缠知识面）", BLOCKED, "未暴露 rules 块，无法判定")

    # ── 采集源模式：生产**不得**处于模拟模式 ──
    src = (js.get("collect_source") or {})
    rep.note("collect_source", {k: src.get(k) for k in ("collect_source", "simulated", "from_env")})
    if src.get("simulated") is True:
        rep.add("C3e", "采集源非模拟模式", FAIL,
                "collect_source.simulated=true ⇒ 正在用模拟数据，本次读数**不可**作为生产验收依据")
    elif src:
        rep.add("C3e", "采集源非模拟模式", PASS,
                "collect_source=%s simulated=%s" % (src.get("collect_source"), src.get("simulated")))
    else:
        rep.add("C3e", "采集源非模拟模式", BLOCKED, "未暴露 collect_source 块，无法判定")

    # ── 可选：真做一次采集，断言事实非空（最强的"探针真的能采"证据）──
    if not with_gather:
        return
    # ★ 入参形状取自**线上 openapi.json** 的 ToolsGatherRequest：
    #   `layers` 是 **array[string]**（default ["gaussdb"]），不是字符串。
    #   传字符串会得到 FastAPI 的 422（真机实测踩过），而 422 看起来像"服务坏了"，
    #   实际只是**我传错了形状**。
    try:
        status, body = http("POST", "%s:8187/tools/gather" % base,
                            {"layers": ["gaussdb"], "sections": []}, timeout=120)
        parsed = json.loads(body) if body else None
    except OSError as exc:
        rep.add("C3f", "真实采集事实非空（--with-gather）", BLOCKED, "gather 不可达：%s" % exc)
        return
    except Exception:
        parsed = None
    if status == 422:
        rep.add("C3f", "真实采集事实非空（--with-gather）", BLOCKED,
                "HTTP 422：请求体形状与 ToolsGatherRequest 不符（检查器自身的问题，非服务故障）")
        return
    if status != 200 or not isinstance(parsed, dict):
        rep.add("C3f", "真实采集事实非空（--with-gather）", FAIL,
                "POST /tools/gather HTTP %s" % status)
        return
    # ★★ 必须识别「工具输出护栏」的应答，否则会**假红** ★★
    #   真机实测：连续第 3 次用**完全相同**的参数打 /tools/gather，返回体不是事实，而是
    #       {"instruction":"STOP。你已用完完全相同的参数连续调…",
    #        "loop_detected":true, "repeat_count":4, "tool":"gather"}
    #   这是 collect 的 `_loop_guard`（所有端点共用，护栏窗口 600s）在按设计工作，
    #   **不是探针故障**。若照旧去读 all_facts 就会读到"缺失 ⇒ 0 ⇒ FAIL"，
    #   把一个正确行为判成故障 —— 而且它恰好只在"连续跑验收"时出现，最易误导。
    #   ⇒ 判 BLOCKED（未测到）：本次读数由**护栏**决定，不由探针决定。
    if parsed.get("loop_detected") is True:
        rep.note("gather_loop_detected", parsed)
        rep.add("C3f", "真实采集事实非空（--with-gather）", BLOCKED,
                "被工具输出护栏拦下（loop_detected=true, repeat_count=%s）："
                "同一参数在护栏窗口（600s）内重复调用。这是**设计行为**，"
                "不是探针故障 ⇒ 判「未测到」。请隔 ≥600s 重跑，且不要连续重复验收。"
                % parsed.get("repeat_count"))
        return
    facts = parsed.get("all_facts") or parsed.get("facts") or []
    n = len(facts) if isinstance(facts, list) else -1
    rep.note("gather_all_facts", n)
    if n > 0:
        rep.add("C3f", "真实采集事实非空（--with-gather）", PASS,
                "layers=gaussdb 采到 %d 条事实" % n)
    else:
        rep.add("C3f", "真实采集事实非空（--with-gather）", FAIL,
                "gather 成功但 all_facts=%s 为空 ⇒ 探针未产出（不是「没测到」）" % n)

    # ── C3g / C3h：★ 2026-10-12 因一次真实事故新增（本矩阵当时"全绿"漏掉了它）──
    # 事故：`collect` 镜像缺 `service/collect_sim.py`（清单 W1 误判为"软依赖"），
    #   而 gaussdb 探针在 **live 路径**上必经 `_sim.connect_async(...)`
    #   ⇒ **gaussdb 层全部探针段产出为 0**（实测 total_facts=8，应为 505），
    #   报错被收进 `layer_errors`，而 `/health` 的 `gaussdb.ok` **仍然是 true**
    #   （它走 `_real_topology()` 那条独立路径）⇒ 旧矩阵的 C3c 只断言 `gaussdb.ok`，
    #   **在采集面 100% 失效的情况下依然判 PASS** —— 这是本矩阵最严重的一次漏判。
    # ⇒ 补两条独立断言，堵的就是"ok 为真但探针没产出"这一种。
    #
    # C3g：`layer_errors` 必须为空 —— 它直接携带探针层的异常原文
    #      （本次事故里就是 "'NoneType' object has no attribute 'connect_async'"）。
    errs = parsed.get("layer_errors")
    rep.note("gather_layer_errors", errs)
    if errs:
        rep.add("C3g", "采集层无层级报错（layer_errors）", FAIL,
                "layer_errors 非空 ⇒ 有层整体失败：%s"
                % json.dumps(errs, ensure_ascii=False)[:220])
    else:
        rep.add("C3g", "采集层无层级报错（layer_errors）", PASS, "layer_errors 为空")

    # C3h：必须采到**业务段**事实，而不是只有拓扑身份。
    #      身份事实（node./cluster.id/cluster.primary_ip）由 _real_topology() 产出，
    #      它在探针全挂时**照样有值** —— 正是它让"8 条事实"看起来不像故障。
    IDENTITY = ("node.host", "node.ip", "cluster.id", "cluster.primary_ip")
    biz = [f for f in facts
           if str(f.get("path") or "") not in IDENTITY]
    rep.note("gather_business_facts", len(biz))
    if n > 20 and biz:
        rep.add("C3h", "采集面产出业务段事实（非仅拓扑身份）", PASS,
                "事实 %d 条，其中业务段 %d 条（例：%s）"
                % (n, len(biz), ", ".join(sorted({str(f.get("path") or "")[:22]
                                                for f in biz})[:4])))
    else:
        rep.add("C3h", "采集面产出业务段事实（非仅拓扑身份）", FAIL,
                "事实仅 %d 条、业务段 %d 条 ⇒ 探针未真正产出（拓扑身份事实在探针全挂时"
                "也会有值，不能当作采集可用的证据）" % (n, len(biz)))


# 基础设施路由：不算工具面（FastAPI 自动生成的文档路由 + 健康探针）
INFRA_PATHS = {"/health", "/openapi.json", "/docs", "/redoc",
               "/docs/oauth2-redirect", "/"}


def _tool_paths(rep: Report, base: str, port: int, cid: str, name: str,
                prefix: str = "") -> list:
    """从 /openapi.json 取工具面路由。

    ★★ 这里踩过一次坑，必须写明 ★★
    最初我按 `path.startswith("/tools/")` 过滤，结果 KG 与 ONTO 各只匹配到 **1** 条
    （只有 read_facts 在 /tools/ 下）。其余七个工具注册在 **顶层**：
        真机实测 :8301 / :8302 各有 9 条 path：
        /cause /facts /guide /health /propose /query /schema /tools/read_facts /trace
    于是「工具面同形」这条断言**只比了 1 条路由**，几乎恒真 —— 正是本项目
    反复警告的「毫无鉴别力」（会通过，但什么都没证明）。
    ⇒ 改为枚举**全部非基础设施路由**，让同形断言真正覆盖 8 个工具面。
    """
    url = "%s:%d/openapi.json" % (base, port)
    try:
        status, js = http_json("GET", url, timeout=8)
    except OSError as exc:
        rep.add(cid, "%s /openapi.json" % name, BLOCKED, "不可达：%s" % exc)
        return []
    if status != 200 or not isinstance(js, dict):
        rep.add(cid, "%s /openapi.json" % name, BLOCKED, "HTTP %s" % status)
        return []
    paths = sorted(p for p in (js.get("paths") or {})
                   if p not in INFRA_PATHS and (not prefix or p.startswith(prefix)))
    return paths


def c4_kg(rep: Report, base: str) -> None:
    js = _svc_health(rep, "C4", "方案1 KG-Q :8301", 8301, base)
    if not js:
        return
    paths = _tool_paths(rep, base, 8301, "C4b", "KG-Q")
    rep.note("kg_tool_paths", paths)
    if not paths:
        rep.add("C4c", "KG-Q 工具面非空", FAIL, "未发现 /tools/* 路由")
    else:
        rep.add("C4c", "KG-Q 工具面非空", PASS, "%d 个工具：%s" % (len(paths), ", ".join(x.rsplit("/", 1)[-1] for x in paths)))


def c5_onto(rep: Report, base: str, graphdb_url: str) -> None:
    js = _svc_health(rep, "C5", "方案2 ONTO-Q :8302", 8302, base)
    paths = _tool_paths(rep, base, 8302, "C5b", "ONTO-Q") if js else []
    rep.note("onto_tool_paths", paths)
    if js:
        if not paths:
            rep.add("C5c", "ONTO-Q 工具面非空", FAIL, "未发现 /tools/* 路由")
        else:
            rep.add("C5c", "ONTO-Q 工具面非空", PASS, "%d 个工具" % len(paths))

    # GraphDB 载体与项目专属仓库
    if not graphdb_url:
        rep.add("C5d", "GraphDB 仓库 cc-ops 存在", BLOCKED, "未提供 --graphdb-url")
        return
    url = graphdb_url.rstrip("/") + "/rest/repositories"
    try:
        status, js2 = http_json("GET", url, timeout=8)
    except OSError as exc:
        rep.add("C5d", "GraphDB 仓库 cc-ops 存在", FAIL, "GraphDB 不可达：%s" % exc)
        return
    if status != 200 or not isinstance(js2, list):
        rep.add("C5d", "GraphDB 仓库 cc-ops 存在", FAIL, "HTTP %s（非 200/非列表）" % status)
        return
    ids = [str(r.get("id")) for r in js2 if isinstance(r, dict)]
    rep.note("graphdb_repositories", ids)
    if "cc-ops" in ids:
        rep.add("C5d", "GraphDB 仓库 cc-ops 存在", PASS,
                "仓库 %d 个，含 cc-ops（既有 gdo*/starwars 原样保留）" % len(ids))
    else:
        rep.add("C5d", "GraphDB 仓库 cc-ops 存在", FAIL,
                "未找到 cc-ops；现有仓库：%s" % ", ".join(ids))


def c6_kgt(rep: Report, base: str) -> None:
    js = _svc_health(rep, "C6", "方案3 KG-T :8303", 8303, base)
    if not js:
        return
    url = "%s:8303/tools" % base
    try:
        status, js2 = http_json("GET", url, timeout=8)
    except OSError as exc:
        rep.add("C6b", "KG-T 工具面", FAIL, "GET /tools 不可达：%s" % exc)
        return
    # 真机实测形状：{"arm","count","forbiddenTools","inMainComparison",
    #                "supplementary","tools":[{"name","path",...}, ...]}
    tools = []
    count = None
    if isinstance(js2, dict):
        tools = js2.get("tools") or []
        count = js2.get("count")
    elif isinstance(js2, list):
        tools = js2
    # ★ 只取 name：早期版本把整个工具 dict str() 出来，一次打印了几千字符
    names = [t.get("name") if isinstance(t, dict) else str(t) for t in tools]
    rep.note("kgt_tools", names)
    rep.note("kgt_tool_count", count)
    if status == 200 and names:
        rep.add("C6b", "KG-T 工具面", PASS,
                "%s 个工具：%s" % (count if count is not None else len(names),
                                  ", ".join(str(n) for n in names)))
    else:
        rep.add("C6b", "KG-T 工具面", FAIL, "HTTP %s，tools=%r" % (status, names))


def c6c_kgt_dyn(rep: Report, base: str) -> None:
    """补充臂 :8305 的 **HTTP 级**断言 —— 容器健康 ≠ 这一条臂在服务。

    ★★ 由 2026-10-12 端到端验证的 F3 补上（证据与复现命令见 docs/14 §F3）★★
    在此之前 `SERVICES` 里虽然声明了 `kgt-dyn: 8305`，但**只有** `CONTAINERS`
    消费这个名字（verify_deploy.py:51），于是 8305 仅被 C1 的 `docker inspect`
    间接覆盖 —— 那只能证明"镜像里那条 healthcheck 过了"，而该 healthcheck 打的是
    容器内 127.0.0.1:8305：
      · 若有人把镜像 CMD 的 `--port` 写回 8303（本分支 eb9e1e2 修的正是这个），
        容器会在 8303 上健康、8305 上**拒连**，而 C1 依旧全绿；
      · 补充臂的三件身份（`arm=KG-T-dyn` / `supplementary=true` /
        `inMainComparison=false`）和"比主臂多一个 collect_now"的工具面，
        在发布验收里**一个字都没断言**，而 selfcheck S7 却写着"6 个服务全部被验收器覆盖"。
    这两件事恰是 :8305 存在的全部理由（binding.yaml 声明 6 个 provider；
    kgt-tools.yaml#arms.KG-T-dyn 声明 service_port 8305 / in_main_comparison false /
    expected_tool_counts 6 / collect_now `only_for_arms: [KG-T-dyn]`）。
    """
    js = _svc_health(rep, "C6c", "补充臂 KG-T-dyn :8305", 8305, base)
    if not js:
        return
    ht = [str(t) for t in (js.get("tools") or [])]
    rep.note("kgt_dyn_health_arm", js.get("arm"))
    rep.note("kgt_dyn_health_supplementary", js.get("supplementary"))
    rep.note("kgt_dyn_health_in_main_comparison", js.get("inMainComparison"))
    rep.note("kgt_dyn_health_port", js.get("port"))
    rep.note("kgt_dyn_health_tools", ht)
    # ① 身份：/health 自报是哪条臂 —— "6 个 provider"里唯一可机器核对的归属证据
    if (js.get("arm") == "KG-T-dyn" and js.get("supplementary") is True
            and js.get("inMainComparison") is False):
        rep.add("C6d", "补充臂身份（arm/supplementary/是否进主判据）", PASS,
                "arm=%s supplementary=%s inMainComparison=%s port=%s"
                % (js.get("arm"), js.get("supplementary"),
                   js.get("inMainComparison"), js.get("port")))
    else:
        rep.add("C6d", "补充臂身份（arm/supplementary/是否进主判据）", FAIL,
                "期望 arm=KG-T-dyn / supplementary=true / inMainComparison=false，"
                "实际 arm=%s supplementary=%s inMainComparison=%s port=%s"
                " ⇒ 8305 上跑的多半不是补充臂（臂名/端口装配错了）"
                % (js.get("arm"), js.get("supplementary"),
                   js.get("inMainComparison"), js.get("port")))
    # ② 工具面：对主臂取**超集**，且差值必须含 collect_now（KG-T-dyn 的唯一增项）
    url = "%s:8305/tools" % base
    try:
        status, js2 = http_json("GET", url, timeout=8)
    except OSError as exc:
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL, "GET /tools 不可达：%s" % exc)
        return
    tools = []
    count = None
    if isinstance(js2, dict):
        tools = js2.get("tools") or []
        count = js2.get("count")
    elif isinstance(js2, list):
        tools = js2
    names = [t.get("name") if isinstance(t, dict) else str(t) for t in tools]
    rep.note("kgt_dyn_tools", names)
    rep.note("kgt_dyn_tool_count", count)
    if status != 200 or not names:
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL, "HTTP %s，tools=%r" % (status, names))
        return
    if isinstance(js2, dict) and js2.get("arm") not in (None, "KG-T-dyn"):
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL,
                "GET /tools 自报 arm=%s（与 /health 的 %s 不一致）⇒ 端口与臂装订错位"
                % (js2.get("arm"), js.get("arm")))
        return
    main_names = [str(n) for n in (rep.readings.get("kgt_tools") or [])]
    if not main_names:
        rep.add("C6e", "补充臂工具面（:8305/tools）", BLOCKED,
                "主臂 :8303 工具面未取到（C6b 未通过）⇒ 无法比对超集关系；"
                "本臂自报 %s 个：%s"
                % (count if count is not None else len(names),
                   ", ".join(str(n) for n in names)))
        return
    missing = sorted(set(main_names) - set(names))
    extra = sorted(set(names) - set(main_names))
    if missing:
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL,
                "缺少主臂已有的工具：%s ⇒ 补充臂不是主臂的超集"
                "（契约 kgt-tools.yaml#arms.KG-T-dyn 是「在主实验组之上追加 collect_now」）"
                % ", ".join(missing))
        return
    if "collect_now" not in names:
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL,
                "无 collect_now ⇒ 这不是 KG-T-dyn（契约里 collect_now "
                "`only_for_arms: [KG-T-dyn]`）；本臂 %d 个：%s"
                % (len(names), ", ".join(str(n) for n in names)))
        return
    if count is not None and count != len(names):
        rep.add("C6e", "补充臂工具面（:8305/tools）", FAIL,
                "count=%s 与 tools 列表长度 %d 不一致 ⇒ 自报数与实际装订的工具面不符"
                % (count, len(names)))
        return
    rep.add("C6e", "补充臂工具面（:8305/tools）", PASS,
            "%s 个工具 = 主臂 %d 个 + %s"
            % (count if count is not None else len(names), len(main_names),
               ", ".join(extra) if extra else "（无差集）"))
    # ③ 数量与契约对齐：不对齐就得改契约或改实现，不许"差不多就行"
    expected = 6
    if len(names) != expected:
        rep.add("C6f", "补充臂工具数与契约一致", FAIL,
                "实际 %d 个，契约 kgt-tools.yaml#arms.expected_tool_counts "
                "声明 KG-T-dyn=%d" % (len(names), expected))
    else:
        rep.add("C6f", "补充臂工具数与契约一致", PASS,
                "%d 个（与契约 expected_tool_counts 一致）" % expected)


def c7_live_facts(rep: Report, base: str) -> None:
    js = _svc_health(rep, "C7", "取数面 :8304", 8304, base)
    if not js:
        return
    url = "%s:8304/tools/read_facts" % base
    # ★ 入参键名与值域**逐字**取自契约 shared/model/live-facts-tools.yaml#read_facts：
    #   是 **snake_case**（max_records / max_bytes），且该契约声明
    #   `additional_properties: false` + `error_discipline`「未知入参一律 UNKNOWN_PARAM(400)，
    #   不得静默忽略」⇒ 传 camelCase（maxRecords）会**必然 400**。
    #   （这不是假设：本检查器最初就写成了 camelCase，对着真机跑才暴露。）
    payload = {"max_records": 5, "max_bytes": 4000}
    try:
        status, body = http("POST", url, payload, timeout=20)
    except OSError as exc:
        rep.add("C7b", "read_facts 可调用且非空壳", FAIL, "POST 不可达：%s" % exc)
        return
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = None
    rep.note("read_facts_status", status)
    if status != 200 or not isinstance(parsed, dict):
        code = ""
        if isinstance(parsed, dict):
            code = str((parsed.get("error") or {}).get("code") or "")
        # ★ 服务端 fail-closed 的 `BLOCKED_*` 是**设计行为**（基底/用例/覆盖不可用），
        #   不是接口故障。联调实测：缺 GaussDB_DSN 时 live 模式归一化被 freeze.build 拒绝
        #   ⇒ HTTP 409 `BLOCKED_BASE`。把它判 FAIL 等于把「环境没配好」说成「服务坏了」。
        #   ⇒ 映射为 BLOCKED（未测到），并点名真实原因。
        if code.startswith("BLOCKED_"):
            msg = str((parsed.get("error") or {}).get("message") or "")[:150]
            rep.add("C7b", "read_facts 可调用且非空壳", BLOCKED,
                    "HTTP %s %s ⇒ 服务端 fail-closed（环境未就绪，非接口故障）：%s"
                    % (status, code, msg))
            return
        # ★★ 第二类必须识别的「非故障」应答：被**工具输出护栏**替换的回执 ★★
        #   联调实测（真凭证那轮）：HTTP 502 `UPSTREAM_UNAVAILABLE`，服务端自己写明
        #       fetch_live：回执不是采集面正常响应（可能被 _loop_guard 熔断替换）
        #   根因是**本矩阵自身**的调用顺序：C3f 先做了一次 /tools/gather，
        #   紧接着 C7 的 read_facts 在 live 模式又要做一次**参数相同**的采集
        #   ⇒ collect 的 `_loop_guard`（窗口 600s，防 LLM 循环）按设计把它熔断。
        #   ⇒ 判 BLOCKED（未测到）：本次读数由**护栏**决定，不由取数面决定。
        #     顺带提示规避法（改用判决模式，或隔窗口再跑），而不是让人去查取数面。
        msg = str((parsed.get("error") or {}).get("message") or "")
        if code == "UPSTREAM_UNAVAILABLE" and "_loop_guard" in msg:
            rep.add("C7b", "read_facts 可调用且非空壳", BLOCKED,
                    "被工具输出护栏熔断（live 模式下的重复采集与 C3f 撞车）⇒ 未测到。"
                    "规避：设 M5_CASE_ID 走判决模式（用镜像内基底快照），或隔 600s 再跑")
            return
        rep.add("C7b", "read_facts 可调用且非空壳", FAIL,
                "HTTP %s %s；响应前 160 字：%s"
                % (status, code, body[:160].replace("\n", " ")))
        return

    # 线上返回体 = tool_level ∪ envelope 的**平铺**八名（契约 envelope_note 明确）
    sections = {k: parsed.get(k) for k in ("objects", "relations", "observations")}
    counts = {k: (len(v) if isinstance(v, list) else None) for k, v in sections.items()}
    total = sum(c for c in counts.values() if isinstance(c, int))
    rep.note("read_facts_section_counts", counts)
    rep.note("read_facts_source", parsed.get("source"))
    missing = [k for k, v in sections.items() if not isinstance(v, list)]
    if missing:
        rep.add("C7b", "read_facts 可调用且非空壳", FAIL,
                "响应缺三段之一 %s（实际顶层键：%s）"
                % (", ".join(missing), ", ".join(sorted(parsed)[:12])))
        return
    # ★ 不停在"有返回"：还要断言它不是空壳。
    #   本项目踩过这个坑——旧验收只断言"两边都有返回"，hit_count=0 也算过，
    #   毫无鉴别力，会把"没采集所以没事实"掩盖成"无异常"。
    if total > 0:
        rep.add("C7b", "read_facts 可调用且非空壳", PASS,
                "HTTP 200；objects=%s relations=%s observations=%s"
                % (counts["objects"], counts["relations"], counts["observations"]))
    else:
        rep.add("C7b", "read_facts 可调用且非空壳", FAIL,
                "HTTP 200 但三段全空（objects/relations/observations 均为 0）"
                " ⇒ 取数面通了但没数据；请查基底快照 tmp/ccf_live5.json 与 M5_CASE_ID")


def c8_toolface_parity(rep: Report) -> None:
    """硬约束 C3：KG 侧与 ONTO 侧工具面**同形**（唯一允许差异是 cc_query 的语言）。"""
    kg = rep.readings.get("kg_tool_paths") or []
    onto = rep.readings.get("onto_tool_paths") or []
    if not kg or not onto:
        rep.add("C8", "KG/ONTO 工具面同形", BLOCKED,
                "两侧 /openapi.json 未同时取得 ⇒ 未能测量")
        return
    only_kg = sorted(set(kg) - set(onto))
    only_onto = sorted(set(onto) - set(kg))
    rep.note("toolface_only_kg", only_kg)
    rep.note("toolface_only_onto", only_onto)
    if only_kg or only_onto:
        rep.add("C8", "KG/ONTO 工具面同形", FAIL,
                "仅 KG：%s；仅 ONTO：%s" % (only_kg or "无", only_onto or "无"))
    else:
        rep.add("C8", "KG/ONTO 工具面同形", PASS, "%d 条工具面路由逐条一致" % len(kg))


def c9_audit(rep: Report, base: str, docker_ok: bool) -> None:
    """R-27：审计链必须**真的落盘**。做法是打一次真实工具调用，断言审计行数增长。"""
    if not docker_ok:
        rep.add("C9", "审计链落盘（R-27）", BLOCKED, "docker 不可用 ⇒ 未能测量")
        return
    audit = "/var/lib/cc-ops/audit/cc_tools_audit.jsonl"

    def lines() -> int:
        rc, out = sh(["docker", "exec", CONTAINERS["kg"], "sh", "-c",
                      "test -f %s && wc -l < %s || echo 0" % (audit, audit)], 15)
        m = re.search(r"(\d+)", out)
        return int(m.group(1)) if rc == 0 and m else -1

    before = lines()
    if before < 0:
        rep.add("C9", "审计链落盘（R-27）", FAIL,
                "读不到 %s ⇒ 审计链未落盘（R-27 要求：一行 = 一次工具调用）" % audit)
        return

    # 选一个**真正会成功**的工具来触发调用：找不到就报 BLOCKED，绝不静默跳过。
    # ★ 真机实测的教训：工具面里的路由大多**有必填入参**（/cause 要 rootCause、
    #   /facts 要 objectType…），对它们 POST {} 会得到 400。若拿 400 的调用去断言
    #   "审计行数必须增长"，就会因为"服务不审计被拒的请求"而**假红**。
    #   ⇒ 按候选顺序逐个试，取第一个 2xx 的；全都非 2xx 就判 BLOCKED（未测到）。
    paths = rep.readings.get("kg_tool_paths") or []
    preferred = ["/schema", "/guide", "/metrics", "/trace", "/query", "/cause",
                 "/facts", "/propose", "/tools/read_facts"]
    cand = [p for p in preferred if p in paths] or paths
    if not cand:
        rep.add("C9", "审计链落盘（R-27）", BLOCKED,
                "审计文件存在（%d 行）但工具面为空 ⇒ 无法触发调用" % before)
        return

    triggered, last_status, loop_hit = None, None, False
    for tool in cand:
        try:
            status, body = http("POST", "%s:8301%s" % (base, tool), {}, timeout=20)
        except OSError as exc:
            rep.add("C9", "审计链落盘（R-27）", BLOCKED, "触发调用失败：%s" % exc)
            return
        last_status = status
        # 护栏应答也是 2xx，但它**不产生真正的工具执行** ⇒ 不能拿它断言审计链
        try:
            if isinstance(json.loads(body), dict) and json.loads(body).get("loop_detected") is True:
                loop_hit = True
                continue
        except Exception:
            pass
        if 200 <= status < 300:
            triggered = tool
            break
    if triggered is None:
        extra = "（含工具输出护栏应答 loop_detected=true）" if loop_hit else ""
        rep.add("C9", "审计链落盘（R-27）", BLOCKED,
                "试遍 %d 个工具均未触发有效执行（末次 HTTP %s）%s ⇒「未测到」而非「测得坏结果」"
                % (len(cand), last_status, extra))
        return

    time.sleep(1.0)
    after = lines()
    rep.note("audit_lines_before", before)
    rep.note("audit_lines_after", after)
    rep.note("audit_trigger_tool", triggered)
    if after > before:
        rep.add("C9", "审计链落盘（R-27）", PASS,
                "%s 一次调用后审计行 %d → %d（追加语义成立）" % (triggered, before, after))
    else:
        rep.add("C9", "审计链落盘（R-27）", FAIL,
                "%s 调用成功（2xx）但审计行仍为 %d ⇒ 审计链未追加" % (triggered, before))


def c10_restart(rep: Report, docker_ok: bool, compose_args: list) -> None:
    """可选且**危险**：重启后 1–9 仍须全过。默认不跑。"""
    if not docker_ok:
        rep.add("C10", "重启存活", BLOCKED, "docker 不可用")
        return
    print("  [信息] --with-restart：重启应用层容器（不动载体数据）…")
    rc, out = sh(["docker", "compose"] + compose_args + ["restart"], 180)
    if rc != 0:
        rep.add("C10", "重启存活", FAIL, "restart 失败：%s" % out.strip()[:160])
        return
    time.sleep(20)
    bad = [n for n, c in CONTAINERS.items() if container_state(c) not in ("healthy", "running")]
    if bad:
        rep.add("C10", "重启存活", FAIL, "重启后异常：%s" % ", ".join(bad))
    else:
        rep.add("C10", "重启存活", PASS, "重启后 %d 个容器均健康" % len(CONTAINERS))


# ---------------------------------------------------------------------------
def read_env(path: str) -> dict:
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="容器化部署验收矩阵")
    ap.add_argument("--deploy-dir", default="deploy", help="含 .env / compose.yml 的目录")
    ap.add_argument("--base", default="http://127.0.0.1", help="应用层基址（默认本机）")
    ap.add_argument("--mode", default="attached", choices=["attached", "standalone"])
    ap.add_argument("--graphdb-url", default=None,
                    help="宿主侧可达的 GraphDB 地址（默认按 --mode 推导）")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    ap.add_argument("--with-restart", action="store_true",
                    help="【危险】额外跑重启存活项")
    ap.add_argument("--with-gather", action="store_true",
                    help="额外做一次**真实采集**（POST /tools/gather，会真的查库/SSH），"
                         "断言 all_facts 非空 —— 最强的「探针真能采」证据")
    args = ap.parse_args(argv)

    env = read_env(os.path.join(args.deploy_dir, ".env"))
    base = args.base.rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base

    # GraphDB 宿主侧地址的推导顺序（真机踩过：默认值把"本机"当成了"目标机"）：
    #   ① --graphdb-url 显式给；
    #   ② .env 的 CC_GRAPHDB_URL（把容器专用 host.docker.internal 换回 127.0.0.1）；
    #   ③ 都没有 ⇒ 取 **--base 的主机** + 契约端口 7200。
    # ★ ③ 这一步很关键：远端验收时（--base http://<目标机IP>）若默认回 127.0.0.1，
    #   就会去连**跑脚本这台机器**的 7200，得到一个"GraphDB 不可达"的**假故障**。
    gdb = args.graphdb_url
    if not gdb:
        raw = env.get("CC_GRAPHDB_URL", "")
        if raw:
            gdb = raw.replace("//host.docker.internal", "//127.0.0.1")
        else:
            gdb = "http://%s:7200" % (base.split("//", 1)[-1].split(":")[0].split("/")[0])
    if args.mode == "standalone" and not args.graphdb_url:
        # standalone 档的载体就在目标机上，走宿主契约端口
        gdb = "http://%s:7200" % base.split("//", 1)[-1].split(":")[0].split("/")[0]

    compose_args = ["-f", os.path.join(args.deploy_dir, "compose.yml")]
    if args.mode == "standalone":
        compose_args += ["-f", os.path.join(args.deploy_dir, "compose.standalone.yml")]

    print("=" * 78)
    print("credit-card-sys-ops 容器化部署 · 验收矩阵")
    print("=" * 78)
    print("  应用层基址 : %s" % base)
    print("  档位       : %s" % args.mode)
    print("  GraphDB    : %s" % gdb)
    print("  部署目录   : %s" % os.path.abspath(args.deploy_dir))
    print("-" * 78)

    docker_ok = have_docker()
    if not docker_ok:
        print("  [警告] 本机无可用 docker ⇒ C1/C2/C9/C10 将记为 BLOCKED（未测到，非测坏）")
        print("-" * 78)

    rep = Report()
    c1_containers(rep, docker_ok)
    c2_arch(rep, docker_ok)
    c3_collect(rep, base, with_gather=args.with_gather)
    c4_kg(rep, base)
    c5_onto(rep, base, gdb)
    c6_kgt(rep, base)
    c6c_kgt_dyn(rep, base)
    c7_live_facts(rep, base)
    c8_toolface_parity(rep)
    c9_audit(rep, base, docker_ok)
    if args.with_restart:
        c10_restart(rep, docker_ok, compose_args)

    n_pass, n_fail, n_blk, rc = rep.summary()
    print("-" * 78)
    print("  PASS=%d  FAIL=%d  BLOCKED=%d  ⇒ %s"
          % (n_pass, n_fail, n_blk, {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED"}[rc]))
    if n_blk:
        print("  ★ 存在 BLOCKED：有检查项**未测到**（环境未通），不是测得坏结果。")
        print("    本项目的纪律：「没测到」不得当「通过」，也不得当「失败」。")
    print("=" * 78)

    out = args.out or os.path.join(args.deploy_dir, ".work", "verify.json")
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"schema": "ccops-verify/1",
                       "base": base, "mode": args.mode,
                       "host_arch": platform.machine(),
                       "exit_code": rc,
                       "counts": {"pass": n_pass, "fail": n_fail, "blocked": n_blk},
                       "items": rep.items, "readings": rep.readings},
                      fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print("  结果 JSON：%s" % out)
    except OSError as exc:
        print("  [警告] 写不出结果 JSON（%s）—— 不影响退出码" % exc)

    return rc


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
