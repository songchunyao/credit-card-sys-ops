#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 守卫：**声明为证据的 `tmp/` 文件都必须在检出里存在且已跟踪**。

真事故（不是假想）：

purity 门禁的 **P2 面**声明证据 = `tmp/w28_p2_bt8_a.out.txt`（`shared/model/purity-contract.yaml`
的 `faces.P2.evidence`）。该文件在本机存在，但**没进 `.gitignore` 白名单** ⇒ 新克隆（CI）里没有
⇒ 容器内 `purity_gate` 报 `BLOCKED: P2-evidence-missing`。
根因：生成白名单的正则用了**懒惰匹配**，对 `.out.txt` 这类**双扩展名**会停在 `.out`
（`out` 本身在扩展名候选表里）而取到不存在的路径 ⇒ 该引用被静默丢弃。同类共 **9 个** `tmp/w28_*.out.txt`。

★ 本守卫**只认「声明为证据」的路径**（`gate-registry.yaml` 的 `evidence`、
`purity-contract.yaml` 里所有键名为 `evidence` 的节点），**不**扫文档与 CLI 用法示例
（`tmp/out.json` / `tmp/__missing_a__.json` / `tmp/ccf_live1..live4.json` 之类是**示例**，不是前置）。
—— 一个因**假因**变红的门禁比没有门禁更糟（实测：全量扫会误报 17 条）。

三条检查：
  ① 每条**声明证据**存在；
  ② 每条**声明证据**已被 git 跟踪（无 git 时跳过，CI 检出里「存在」已等价）；
  ③ 白名单（`shared/deploy/port/tmp-evidence-whitelist.txt`）每条都存在。

退出码：0 = 全过；1 = 有缺失（点名）；2 = 前置不可得（读不到注册表/契约）。

☆ 与 `check_offline_gates.py` 一样，**刻意不放在** `shared/verify/` 下（那里有覆盖契约）。
用法：python ci/check_evidence_files.py [--repo-root R]
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WHITELIST = "shared/deploy/port/tmp-evidence-whitelist.txt"
REGISTRY = "shared/model/gate-registry.yaml"
CONTRACT = "shared/model/purity-contract.yaml"


def _norm(v: str) -> str:
    return v.replace("\\", "/").strip()


def collect_declared(repo: pathlib.Path):
    """返回 {相对路径: {来源: 次数}}，只含**声明为证据**的 tmp/ 路径。"""
    import yaml
    declared: dict[str, set[str]] = {}

    def add(val, src):
        if isinstance(val, str):
            v = _norm(val)
            if v.startswith("tmp/"):
                declared.setdefault(v, set()).add(src)
        elif isinstance(val, list):
            for x in val:
                add(x, src)

    reg = repo / REGISTRY
    if not reg.is_file():
        return None
    d = yaml.safe_load(reg.read_text(encoding="utf-8")) or {}
    for e in (d.get("entries") or []):
        add(e.get("evidence"), "%s#%s" % (REGISTRY, e.get("id")))

    con = repo / CONTRACT
    if not con.is_file():
        return None
    c = yaml.safe_load(con.read_text(encoding="utf-8")) or {}

    def walk(node, src):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "evidence":
                    add(v, src)
                else:
                    walk(v, src if k != "evidence" else src)
        elif isinstance(node, list):
            for x in node:
                walk(x, src)

    for face, body in (c.get("faces") or {}).items():
        walk(body, "%s#faces.%s.evidence" % (CONTRACT, face))
    for key in ("counterexample_plan", "classify", "vocab"):
        if key in c:
            walk(c[key], "%s#%s.evidence" % (CONTRACT, key))
    return declared


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="声明证据文件的存在性/跟踪性守卫")
    ap.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parents[1]))
    args = ap.parse_args(argv)
    repo = pathlib.Path(args.repo_root).resolve()

    try:
        import yaml  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print("[BLOCKED] 读不到 PyYAML（%s）⇒ 无法判定，不当作通过" % exc)
        return 2

    declared = collect_declared(repo)
    if declared is None:
        print("[BLOCKED] 读不到注册表或纯粹性契约 ⇒ 无法判定")
        return 2

    have_git = bool(shutil.which("git")) and (repo / ".git").exists()
    tracked = set()
    if have_git:
        r = subprocess.run(["git", "ls-files", "-z"], cwd=str(repo), capture_output=True)
        tracked = {x.decode("utf-8") for x in r.stdout.split(b"\0") if x}

    print("声明为证据的 tmp/ 路径 = %d 条（来源：%s + %s）"
          % (len(declared), REGISTRY, CONTRACT))
    print("git 跟踪检查 = %s" % ("开" if have_git else "关（无 git / 非仓库）"))

    bad = []
    for rel in sorted(declared):
        p = repo / rel
        if not p.is_file():
            bad.append(("缺失", rel, sorted(declared[rel])[0]))
        elif have_git and rel not in tracked:
            bad.append(("未跟踪", rel, sorted(declared[rel])[0]))

    wl_path = repo / WHITELIST
    wl = []
    if wl_path.is_file():
        wl = [_norm(x) for x in wl_path.read_text(encoding="utf-8").split("\n") if x.strip()]
    print("白名单条目 = %d 条" % len(wl))
    for rel in wl:
        if not (repo / rel).is_file():
            bad.append(("白名单指向不存在的文件", rel, WHITELIST))

    # 关键交叉检查：声明为证据的路径**必须**在白名单里（否则新克隆不会有它）
    if wl:
        not_in_wl = sorted(set(declared) - set(wl))
        for rel in not_in_wl:
            bad.append(("声明为证据但不在白名单", rel, sorted(declared[rel])[0]))
        print("声明证据 ∩ 白名单 = %d / %d" % (len(set(declared) & set(wl)), len(declared)))

    if bad:
        print("\n[FAIL] %d 条问题：" % len(bad))
        for kind, rel, src in bad:
            print("  - [%s] %s    ← %s" % (kind, rel, src))
        return 1
    print("\n[PASS] 声明证据全部存在%s，且全部在白名单内"
          % ("且已跟踪" if have_git else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
