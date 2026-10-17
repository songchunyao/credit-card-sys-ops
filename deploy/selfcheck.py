#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/selfcheck.py —— 发布一致性静态自检（**离线、零外部依赖**）

为什么要它
----------
本项目的容器化踩过、也最容易再踩的一类坑是**静默失效**：
  · compose 里 `${VAR}` 写了个 .env 里没有的键 → compose 不报错，值走空串
    → "容器起来了但采集全空"；
  · 采集层代码读的键名与模板里的键名不一致 → 同上（上游 docs/49 §1.6 实测：
    代码引用 107 个变量，模板只列 14 个，且 `DORADO_API_BASE` 与代码读的
    `DORADO_HOST`+`DORADO_PORT` 根本不匹配）；
  · Dockerfile `COPY` 的源路径因目录改名而失效 → 构建才炸，或更糟：静默少带文件。

⇒ 把这三类都变成**可在本地和 CI 跑的断言**。零外部依赖（只用标准库 + AST），
  这样它能在 CI 的任意镜像里跑，不需要装任何包。

用法
----
    python3 deploy/selfcheck.py            # 人读输出
    python3 deploy/selfcheck.py --json     # 机读输出

退出码：0=PASS 1=FAIL
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = 0, 1


class Checks:
    def __init__(self) -> None:
        self.rows: list = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append({"name": name, "ok": bool(ok), "detail": detail})
        print("  [%s] %-52s %s" % ("OK  " if ok else "FAIL", name, detail))

    def bad(self) -> int:
        return sum(1 for r in self.rows if not r["ok"])


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\}")


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def env_template_keys(p: Path) -> set:
    keys = set()
    if not p.is_file():
        return keys
    for line in read_text(p).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def compose_env_refs(p: Path) -> dict:
    """返回 {VAR: 是否有 :- 默认值}。正则扫描，不依赖 PyYAML（CI 里更稳）。

    ★ 必须**先剥掉整行注释**：本项目的 compose 文件里大量注释会举 `${VAR}` 当例子
      （"所有 ${VAR} 会静默走默认值"），不过滤就会造出一个名叫 VAR 的假变量
      ⇒ 假红。**一个因假因变红的门禁比没有门禁更糟。**
    """
    refs: dict = {}
    if not p.is_file():
        return refs
    code = "\n".join(ln for ln in read_text(p).splitlines()
                     if not ln.lstrip().startswith("#"))
    for m in ENV_REF.finditer(code):
        name, default = m.group(1), m.group(2)
        refs[name] = refs.get(name, True) and bool(default)
    return refs


def authoritative_env_keys() -> dict:
    """AST 扫描运行期代码里真正读取的环境变量名 → {key: {来源文件}}。

    与 collect-layer.manifest.yaml 同一思路：**以代码为准**，不以文档为准。
    """
    targets = []
    cl = ROOT / "dist" / "collect-layer"
    if cl.is_dir():
        targets += list(cl.rglob("*.py"))
    targets += [
        ROOT / "scheme1-kg" / "service" / "cc_tools.py",
        ROOT / "scheme1-kg" / "service" / "cc_tools_kgt.py",
        ROOT / "scheme1-kg" / "service" / "topology_reader.py",
        ROOT / "scheme1-kg" / "tools" / "load_tbox.py",
        ROOT / "scheme2-onto" / "service" / "cc_tools_onto.py",
        ROOT / "shared" / "collect" / "live_facts_service.py",
        ROOT / "shared" / "collect" / "live_facts.py",
        ROOT / "shared" / "collect" / "freeze.py",
    ]
    found: dict = {}
    for f in targets:
        if not f.is_file():
            continue
        try:
            tree = ast.parse(read_text(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            key = None
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in ("get", "getenv"):
                    owner = fn.value
                    is_environ = (isinstance(owner, ast.Attribute) and owner.attr == "environ") \
                        or (isinstance(owner, ast.Name) and owner.id == "getenv")
                    if is_environ and node.args and isinstance(node.args[0], ast.Constant) \
                            and isinstance(node.args[0].value, str):
                        key = node.args[0].value
            elif isinstance(node, ast.Subscript):
                owner = node.value
                if isinstance(owner, ast.Attribute) and owner.attr == "environ":
                    sl = node.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        key = sl.value
            if key:
                found.setdefault(key, set()).add(str(f.relative_to(ROOT)).replace("\\", "/"))
    return found


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------
def s1_dockerignore(c: Checks) -> None:
    p = ROOT / ".dockerignore"
    c.check("S1 .dockerignore 存在", p.is_file())
    if not p.is_file():
        return
    txt = read_text(p)
    # live-facts 镜像必须带 tmp/ccf_live5.json ⇒ 该例外必须在
    c.check("S1 dockerignore 放回 tmp/ccf_live5.json",
            "!tmp/ccf_live5.json" in txt,
            "缺它 ⇒ live-facts 镜像构建后启动即 409 BLOCKED_BASE")
    # .env 绝不能进镜像（密钥）
    c.check("S1 dockerignore 排除 .env",
            re.search(r"^\.env$", txt, re.M) is not None,
            "镜像内零密钥")


def s2_dockerfile_copy(c: Checks) -> None:
    dockerfiles = sorted(ROOT.glob("Dockerfile.*"))
    c.check("S2 找到镜像定义（≥5 个）", len(dockerfiles) >= 5,
            "%d 个：%s" % (len(dockerfiles), ", ".join(d.name for d in dockerfiles)))
    # 采集层源码树（dist/collect-layer/）**随本仓分发**，所以它的 COPY 源必须真的在。
    # 唯一被刻意裁掉的是：
    #   · tmp/ccf_live5.json —— 真机采集到的基底快照（含现网拓扑与地址，不随源码分发）。
    # 缺它 ⇒ live-facts 镜像的 COPY 源必然缺失，属刻意裁剪，不是配置漂移。
    # 判定一律"按文件在不在"，所以本地完整仓里行为不变（照样逐条真查）。
    allowed_missing = []
    if not (ROOT / "tmp" / "ccf_live5.json").exists():
        allowed_missing.append("tmp/")
    missing = []
    skipped = []
    for df in dockerfiles:
        for line in read_text(df).splitlines():
            line = line.strip()
            if not line.upper().startswith("COPY"):
                continue
            if "--from=" in line:          # 阶段间复制，不对应源码路径
                continue
            parts = line.split()[1:]
            if len(parts) < 2:
                continue
            for src in parts[:-1]:
                s = src.lstrip("/")
                if (ROOT / s).exists():
                    continue
                if any(s.startswith(p) for p in allowed_missing):
                    skipped.append("%s: %s" % (df.name, src))
                    continue
                missing.append("%s: %s" % (df.name, src))
    c.check("S2 每个 Dockerfile 的 COPY 源都存在", not missing,
            "; ".join(missing) if missing else
            ("全部命中" if not skipped else
             "全部命中（%d 项基底快照类 COPY 源因未随源码分发而跳过，见 NOTICE）" % len(skipped)))


def s3_compose_refs(c: Checks) -> None:
    tmpl = env_template_keys(ROOT / "deploy" / ".env.example")
    c.check("S3 环境模板存在且非空", len(tmpl) > 20,
            "deploy/.env.example 共 %d 键" % len(tmpl))
    for name in ("compose.yml", "compose.standalone.yml"):
        refs = compose_env_refs(ROOT / "deploy" / name)
        undef = sorted(k for k, has_def in refs.items() if not has_def and k not in tmpl)
        c.check("S3 %s 的 ${VAR} 都有来源" % name, not undef,
                ("未定义且无默认值：%s" % ", ".join(undef)) if undef
                else "%d 个变量引用全部有来源" % len(refs))


def s4_service_env_coverage(c: Checks) -> None:
    """运行期代码读的环境变量，必须在模板里有登记（防 docs/49 §1.6 那类漂移）。"""
    auth = authoritative_env_keys()
    tmpl = env_template_keys(ROOT / "deploy" / ".env.example")
    # 少数变量刻意**不写进 .env 模板**，因为它们不该由部署者填写，而应由编排写死：
    #   · 容器内部契约（PYTHONPATH / 端口 / 服务根目录）—— 由 Dockerfile 的 ENV 给定；
    #   · CC_AUDIT_LOG —— 由 compose.yml 为每个服务**分别**写死到挂载卷上的固定路径
    #     （R-27 要求审计链路径跨重启不变）。允许部署者改它，等于允许把审计链写到
    #     容器可写层里 ⇒ 一重建容器审计链就丢。
    intentionally_unlisted = {
        "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
        "SVC_PORT", "SERVICE_HOST", "SERVICE_PORT",
        "CC_LIVE_FACTS_ROOT", "CC_KGT_HOST", "CC_KGT_PORT",
        "CC_TOOLS_HOST", "CC_TOOLS_PORT", "CC_TOOLS_ONTO_HOST", "CC_TOOLS_ONTO_PORT",
        "CC_LIVE_FACTS_HOST", "CC_LIVE_FACTS_PORT",
        "CC_AUDIT_LOG",
    }
    undocumented = sorted(k for k in auth
                          if k not in tmpl and k not in intentionally_unlisted)
    c.check("S4 代码读取的环境变量都在模板中登记", not undocumented,
            ("未登记：%s" % ", ".join(undocumented)) if undocumented
            else "实测 %d 个运行期环境变量，全部已登记" % len(auth))
    return auth


def s5_ports(c: Checks) -> None:
    """端口必须在 compose / 验收器 / 模板注解三处一致。"""
    expect = {"8187": "collect", "8301": "kg", "8302": "onto",
              "8303": "kgt", "8304": "live-facts"}
    cy = read_text(ROOT / "deploy" / "compose.yml")
    bad = []
    for port, svc in expect.items():
        # compose 里该服务应发布这个端口
        if not re.search(r"%s:%s" % (re.escape(port), re.escape(port)), cy):
            bad.append("%s 未在 compose.yml 发布 %s" % (svc, port))
    vd = read_text(ROOT / "deploy" / "verify_deploy.py")
    for port, svc in expect.items():
        if '"%s": %s' % (svc, port) not in vd.replace("'", '"'):
            bad.append("verify_deploy.py 的 SERVICES 与 %s:%s 不一致" % (svc, port))
    c.check("S5 契约端口在 compose/验收器一致", not bad,
            "; ".join(bad) if bad else "8187/8301/8302/8303/8304 三处一致")


def s6_no_secrets(c: Checks) -> None:
    """发布物里不得出现硬编码口令/令牌。

    ★ 模式必须**只匹配真正的赋值形态**。先前版本把这两种都误报成"硬编码口令"：
      · `NEO4J_AUTH: "neo4j/${CC_NEO4J_PASSWORD:?...}"` —— compose 的 `:?` 必填语法；
      · 散文里的「Neo4j/GraphDB」—— 被 `neo4j/<值>` 模式当成了凭据。
      两者都是**假红**，会训练人忽略这个门禁 ⇒ 已按"值不得以 ${ / ? / 占位词开头"收窄。
    """
    pats = [
        # 赋值形态的口令/令牌，且值不是占位词、不是变量插值、不是空
        re.compile(r"(?i)\b(?:password|passwd|pwd|token|secret|api_key|apikey)"
                   r"\s*[:=]\s*[\"']?"
                   r"(?!CHANGE_ME|change_me|true|false|null|\$\{|\?|$)"
                   r"[A-Za-z0-9!@#$%^&*_\-+]{6,}"),
        # GitLab 个人访问令牌
        re.compile(r"glpat-[A-Za-z0-9_\-]{10,}"),
        # NEO4J_AUTH=neo4j/<真实口令>（排除变量插值与占位词）
        re.compile(r"(?i)NEO4J_AUTH\s*[:=]\s*[\"']?neo4j/"
                   r"(?!\$\{|CHANGE_ME|change_me)[A-Za-z0-9!@#$%^&*_\-+]{5,}"),
    ]
    hits = []
    for rel in ["deploy/compose.yml", "deploy/compose.standalone.yml",
                "deploy/install.sh", "deploy/make_bundle.sh", "deploy/.env.example"]:
        p = ROOT / rel
        if not p.is_file():
            continue
        for i, line in enumerate(read_text(p).splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for pat in pats:
                m = pat.search(line)
                if m:
                    hits.append("%s:%d %s" % (rel, i, m.group(0)[:48]))
                    break
    c.check("S6 发布物无硬编码口令", not hits,
            "; ".join(hits[:6]) if hits else "扫描 5 个发布文件，零命中")


def s7_verify_services(c: Checks) -> None:
    """验收器覆盖的服务集合必须与 compose 的应用服务集合一致。

    ★ 必须先**切出 `services:` 块**再找服务名。先前版本在全文件上按"两空格缩进
      的键"匹配，把 `volumes:` 的子键（ccops-audit）和锚点里的 `logging:` 也
      当成了服务名 ⇒ 假红。YAML 的缩进结构不能靠全局正则猜。
    """
    cy = read_text(ROOT / "deploy" / "compose.yml")
    m = re.search(r"^services:\s*$(.*?)(?=^[A-Za-z])", cy, re.M | re.S)
    block = m.group(1) if m else ""
    svcs = set(re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", block, re.M))
    vd = read_text(ROOT / "deploy" / "verify_deploy.py")
    m2 = re.search(r"SERVICES\s*=\s*\{(.*?)\}", vd, re.S)
    verified = set(re.findall(r'"([a-z][a-z0-9-]*)"\s*:', m2.group(1))) if m2 else set()
    diff = sorted(svcs - verified)
    c.check("S7 验收器覆盖 compose 的全部应用服务", bool(svcs) and not diff,
            ("compose 服务=%s；验收器未覆盖：%s" % (sorted(svcs), ", ".join(diff))) if diff
            else "%d 个服务全部被验收器覆盖：%s" % (len(svcs), ", ".join(sorted(svcs))))

    # ★ 由 2026-10-12 端到端验证的 F3 新增：**登记名字 ≠ 真的探到**。
    #   上面那条断言曾经通过，而 `kgt-dyn: 8305` 其实只被 CONTAINERS 消费（供 C1 的
    #   docker inspect 用）：C6 只探了主臂 :8303，验收矩阵里**没有**任何一处访问 :8305。
    #   于是"镜像 CMD 把 --port 写回 8303"这类错误在矩阵上全绿（healthcheck 打的是
    #   容器内 127.0.0.1:8305，端口装配错位它照样 healthy）。这条断言把每个登记端口
    #   钉到"文件正文里必须存在 :PORT 的探针"，让名字登记不再算数。
    pairs = re.findall(r'"([a-z][a-z0-9-]*)"\s*:\s*(\d+)', m2.group(1)) if m2 else []
    body = vd.replace(m2.group(0), "") if m2 else vd
    unprobed = sorted("%s:%d" % (n, int(p)) for n, p in pairs if (":%d" % int(p)) not in body)
    c.check("S7b 验收器真的探到每个登记端口（不是只登记名字）",
            bool(pairs) and not unprobed,
            ("登记在 SERVICES 却没有任何 HTTP 探针：%s" % ", ".join(unprobed)) if unprobed
            else "%d 个端口都有 HTTP 探针：%s"
                 % (len(pairs), ", ".join(sorted("%s:%s" % (n, p) for n, p in pairs))))


def s8_numeric_env_defaults(c: Checks) -> None:
    """★ 由真实事故新增（2026-10-11 联调首跑）：

    `collect_api.py:1160` 写的是 `int(os.environ.get("LOG_MSG_MAX", "200"))`。
    `os.environ.get(K, default)` **只在键不存在时**返回 default；一旦键**存在但为空串**，
    返回的就是 `""`，`int("")` 直接 `ValueError` ⇒ **容器 crash-loop**。

    而 compose 的常见写法 `${VAR:-}` 恰好把"用户留空"变成"注入空串"：
        .env 里 `LOG_MSG_MAX=`  →  容器内 os.environ["LOG_MSG_MAX"] == ""
    ⇒ 「设成空」比「不设」更危险。本断言把这类键钉死：**必须带非空默认值**。

    实现：AST 扫出代码里所有 `int(...)`/`float(...)` 包着 os.environ.get 的键，
    再到 compose 的 environment 里要求这些键的取值形如 `${K:-<非空>}` 或字面非空。
    """
    numeric: dict = {}
    targets = []
    cl = ROOT / "dist" / "collect-layer"
    if cl.is_dir():
        targets += list(cl.rglob("*.py"))
    targets += [
        ROOT / "scheme1-kg" / "service" / "cc_tools.py",
        ROOT / "scheme1-kg" / "service" / "cc_tools_kgt.py",
        ROOT / "scheme1-kg" / "service" / "topology_reader.py",
        ROOT / "scheme2-onto" / "service" / "cc_tools_onto.py",
        ROOT / "shared" / "collect" / "live_facts_service.py",
        ROOT / "shared" / "collect" / "live_facts.py",
        ROOT / "shared" / "collect" / "freeze.py",
    ]
    for f in targets:
        if not f.is_file():
            continue
        try:
            tree = ast.parse(read_text(f))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id in ("int", "float")):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                g = sub.func
                if not (isinstance(g, ast.Attribute) and g.attr in ("get", "getenv")):
                    continue
                owner = g.value
                isenv = (isinstance(owner, ast.Attribute) and owner.attr == "environ") or \
                        (isinstance(owner, ast.Name) and owner.id == "getenv")
                if isenv and sub.args and isinstance(sub.args[0], ast.Constant):
                    k = sub.args[0].value
                    d = sub.args[1].value if len(sub.args) > 1 and isinstance(sub.args[1], ast.Constant) else None
                    numeric.setdefault(k, {"default": d, "where": str(f.relative_to(ROOT))})

    cy = read_text(ROOT / "deploy" / "compose.yml")
    cy_nocomment = "\n".join(ln for ln in cy.splitlines() if not ln.lstrip().startswith("#"))
    bad = []
    for k, meta in sorted(numeric.items()):
        m = re.search(r"^\s+%s:\s*(.+?)\s*$" % re.escape(k), cy_nocomment, re.M)
        if not m:
            # 不注入 ⇒ 键不存在 ⇒ 代码默认值生效 ⇒ 安全
            continue
        val = m.group(1).strip().strip('"').strip("'")
        # 形如 ${K:-<非空>} 才算安全
        mm = re.match(r"^\$\{%s:-([^}]*)\}$" % re.escape(k), val)
        has_nonempty_default = bool(mm and mm.group(1).strip())
        literal_ok = bool(val) and not val.startswith("${")
        if not (has_nonempty_default or literal_ok):
            bad.append("%s（compose 取值 %s，代码默认 %r）" % (k, val, meta["default"]))

    c.check("S8 数值型环境变量在 compose 里带非空默认值", not bad,
            ("会注入空串导致 int('')/float('') 崩溃：%s" % "; ".join(bad)) if bad
            else "代码用 int()/float() 解析的 %d 个键，compose 取值全部安全" % len(numeric))


def s9_bundle_hygiene(c: Checks) -> None:
    """★ 由 2026-10-12 真机 E2E 实测的两个 P1 缺陷新增（F1 凭据填单随包外发 / F2 基底快照缺包）。

    这两条都是**打包脚本与部署脚本之间的接缝**，静态看两个文件各自都"对"，只有把
    「README 推荐的流程」串起来跑一次才暴露 ⇒ 正是本自检存在的理由（见文件头）。

    F1：离线包夹带了**填好的凭据单** `deploy/credentials`（实测 854 B / 22 个非空键，
        含 GaussDB DSN 与各口径口令），而且被 MANIFEST.sha256 登记成发布物的一部分。
        `.gitignore` 挡得住 git，挡不住 `make_bundle.sh` 打出来的包；而部署文档推荐的
        流程恰恰是「填完 deploy/credentials 再打包」。
        ⇒ 断言：打包脚本必须清掉它，且对漏网的凭据类文件 **fail-closed**（拒绝出包）。

    F2：离线包里没有基底快照 `tmp/ccf_live5.json` ⇒ install.sh ④.0b 静默跳过「快照命名图」
        投影 ⇒ 干净载体上 `load_tbox.py persist` 拒绝写入、`cc_tools_onto.py` 启动即崩
        （onto crash-loop，报错里不提"你还没投影"）。旧树有 GraphDB 卷里的历史快照图，
        把这个缺口掩盖过去了。
        ⇒ 断言：打包脚本必须把它拷进包；install.sh 宿主缺件时必须回退到 live-facts 镜像。
    """
    mb = read_text(ROOT / "deploy" / "make_bundle.sh")
    ins = read_text(ROOT / "deploy" / "install.sh")
    gi = read_text(ROOT / ".gitignore")

    c.check("S9 打包脚本删掉凭据填单 deploy/credentials",
            bool(re.search(r'rm -f\s+"\$STAGE/deploy/credentials"', mb)),
            "命中删除规则" if re.search(r'rm -f\s+"\$STAGE/deploy/credentials"', mb)
            else "make_bundle.sh 里没有删除 deploy/credentials 的语句 ⇒ 填好的单子会进包")
    c.check("S9 打包脚本对漏网凭据 fail-closed（拒绝出包）",
            "凭据填单" in mb and re.search(r"凭据填单[\s\S]{0,400}?exit 1", mb) is not None,
            "越界的凭据类文件 ⇒ 直接 exit 1，不出包")
    c.check("S9 基底快照入包 tmp/ccf_live5.json",
            "ccf_live5.json" in mb and "$STAGE/tmp" in mb,
            "打包脚本把 tmp/ccf_live5.json 拷进包" if "ccf_live5.json" in mb
            else "make_bundle.sh 不含基底快照 ⇒ ④.0b 投影会被跳过")
    c.check("S9 install.sh 回退到 live-facts 镜像里的快照",
            "live-facts cat /app/tmp/ccf_live5.json" in ins
            or "cat /app/tmp/ccf_live5.json" in ins,
            "宿主缺快照时不再静默跳过投影" if "cat /app/tmp/ccf_live5.json" in ins
            else "install.sh 只认宿主路径 ⇒ 包里没有 tmp/ 时静默跳过投影")
    c.check("S9 .gitignore 忽略凭据填单", "deploy/credentials" in gi,
            "deploy/credentials 已忽略（不入库）")


def s10_install_lists(c: Checks) -> None:
    """install.sh 的「镜像清单」与「服务清单」必须分开且各自自洽。

    ★ 由 2026-10-12 真机复核 F4 时新增 **一个真实失败** 催生：
      把 `kgt-dyn` 加进 `CC_SERVICES`（为了让它进 ⑥ 的健康门限）后，② 的
      "镜像就绪"循环也去吃这份清单，于是要求 `cc-ops/kgt-dyn:<tag>` 存在 ——
      而 kgt-dyn 是**第二个 kgt 实例**（同镜像、另一条臂、另一端口），根本没有
      这个镜像。真机现象：`[失败] 缺少镜像： cc-ops/kgt-dyn:e2e-main-e8c3fdd`
      ⇒ `die "缺少必需镜像，拒绝继续"`，**整台机器装不上去**。
    教训：一个变量同时代表"镜像"和"容器"两件事，加一个服务就会打穿装机器径。
    本断言把两件事钉死：每个镜像项都要有 Dockerfile，每个服务项都要能由某个
    镜像项解释（自己就是镜像名，或 compose 里 image: 指向某个镜像名）。
    """
    ins = read_text(ROOT / "deploy" / "install.sh")
    m_img = re.search(r'^CC_IMAGES="([^"]*)"', ins, re.M)
    m_svc = re.search(r'^CC_SERVICES="([^"]*)"', ins, re.M)
    images = (m_img.group(1).split() if m_img else [])
    services = (m_svc.group(1).split() if m_svc else [])

    no_dockerfile = [i for i in images if not (ROOT / ("Dockerfile.%s" % i)).is_file()]
    c.check("S10 镜像清单每项都有 Dockerfile", bool(images) and not no_dockerfile,
            ("没有 Dockerfile 的镜像项：%s" % ", ".join(no_dockerfile)) if no_dockerfile
            else "%d 个镜像项都有 Dockerfile：%s" % (len(images), ", ".join(images)))

    # compose 应用服务 → image 的基名（`cc-ops/kgt:${CC_IMAGE_TAG…}` ⇒ kgt）
    cy = read_text(ROOT / "deploy" / "compose.yml")
    m = re.search(r"^services:\s*$(.*?)(?=^[A-Za-z])", cy, re.M | re.S)
    block = m.group(1) if m else ""
    svc_images = dict(re.findall(r"^  ([a-z][a-z0-9-]*):\s*$.*?^\s+image:\s*cc-ops/([a-z][a-z0-9-]*)",
                                 block, re.M | re.S))
    unknown = []
    for svc in services:
        if svc in images:
            continue
        if svc_images.get(svc) in images:
            continue
        unknown.append("%s⇒cc-ops/%s" % (svc, svc_images.get(svc, "(compose 里没有 image:)")))
    c.check("S10 服务清单每项都能由镜像清单解释", bool(services) and not unknown,
            ("无法解释的服务项：%s" % "；".join(unknown)) if unknown
            else "%d 个容器服务 = %d 个镜像%s" % (
                len(services), len(images),
                "（多实例：%s）" % ", ".join(sorted(
                    "%s=%s" % (s, svc_images.get(s)) for s in services if s not in images)) or ""))

    # compose 的服务集合必须全在服务清单里（否则起得来但没人等它健康）
    compose_svcs = set(re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", block, re.M))
    missed = sorted(s for s in compose_svcs if s not in services)
    c.check("S10 compose 的服务都在 install.sh 服务清单里", not missed,
            ("未进服务清单（起得来但不等健康）：%s" % ", ".join(missed)) if missed
            else "%d 个 compose 服务都在清单里" % len(compose_svcs))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="发布一致性静态自检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("deploy/selfcheck.py —— 发布一致性静态自检（离线）")
    print("=" * 78)
    c = Checks()
    s1_dockerignore(c)
    s2_dockerfile_copy(c)
    s3_compose_refs(c)
    s4_service_env_coverage(c)
    s5_ports(c)
    s6_no_secrets(c)
    s7_verify_services(c)
    s8_numeric_env_defaults(c)
    s9_bundle_hygiene(c)
    s10_install_lists(c)
    print("-" * 78)
    bad = c.bad()
    print("  断言 %d 条，FAIL=%d ⇒ %s" % (len(c.rows), bad, "PASS" if bad == 0 else "FAIL"))
    print("=" * 78)

    if args.json:
        print(json.dumps({"schema": "ccops-selfcheck/1", "fail": bad,
                          "items": c.rows}, ensure_ascii=False, indent=2))
    return PASS if bad == 0 else FAIL


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
