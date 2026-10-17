#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/create_graphdb_repo.py —— 确保 GraphDB 上存在项目专属仓库（幂等）

为什么需要它（**首次真跑 standalone 才暴露的缺口**）
--------------------------------------------------
`scheme2-onto/tools/load_tbox.py` 的 `persist` 模式**假定目标仓库已经存在**：
    load_tbox.py:371  if repo not in out["repositories"]:
    load_tbox.py:372      raise Fail("目标仓库不存在：%s（现有 %s）")
而 `repo_list()`（:267-280）在**仓库列表为空**时直接
    raise Fail("仓库列表为空（应含 coexisting_repositories）")

这些断言的原意是保护**共享载体**上既有的 `gdo*` / `starwars` 仓库 —— 在
192.0.2.10 那种"用别人已经建好的 GraphDB"的场景下完全正确。
但在 **standalone 档**（本项目自己用官方镜像起一个全新的 GraphDB）上，
"仓库列表为空"是**正常状态**、不是故障 ⇒ 契约装载必然失败，
而失败信息还会把人引向"GraphDB 不可达/权限"，方向全错。

⇒ 在跑装载器**之前**先把 `cc-ops` 建出来。这本来就是"完成配置"的必要一步。

GraphDB 10 的建仓接口有两个坑（都已实测踩过，这里按对的写）：
  1. 是 **multipart/form-data**，配置以 **Turtle** 文本作为一个**带 filename 的文件**提交；
     **filename 不可省**，否则 GraphDB 返回 400。
  2. 建好之后不一定**立刻**可查询，需要轮询等它就绪。

用法
----
    python3 deploy/create_graphdb_repo.py --targets <targets.yaml>
    python3 deploy/create_graphdb_repo.py --targets <t.yaml> --base http://127.0.0.1:7200
    python3 deploy/create_graphdb_repo.py --selftest        # 离线自检

退出码：0=PASS 1=FAIL 2=BLOCKED（参数/契约问题）
★ 只用标准库（urllib），因为要能在只有裸 python3 的目标机上跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL, BLOCKED = 0, 1, 2


# ---------------------------------------------------------------------------
def read_targets(path: str) -> dict:
    """只取 graphdb 那一段。不引入 PyYAML —— 用极简解析，够用且零依赖。

    ★ 用极简解析而不是 PyYAML：目标机可能只有裸 python3。
      本文件只读 targets.yaml 里 graphdb 下的 4 个标量键，形状稳定。
    """
    import re
    txt = open(path, "r", encoding="utf-8", errors="replace").read()
    m = re.search(r"^graphdb:\s*$(.*?)(?=^[a-zA-Z])", txt, re.M | re.S)
    if not m:
        raise SystemExit("BLOCKED：%s 里找不到 graphdb: 段" % path)
    seg = m.group(1)
    out = {}
    for key in ("url", "repository", "ruleset", "named_graph_template"):
        mm = re.search(r'^\s+%s:\s*"?([^"\n#]+)"?' % key, seg, re.M)
        if mm:
            out[key] = mm.group(1).strip().rstrip('"')
    # repository 在 isolation 子块下
    if "repository" not in out:
        mm = re.search(r'^\s+repository:\s*"?([^"\n#]+)"?', seg, re.M)
        if mm:
            out["repository"] = mm.group(1).strip().rstrip('"')
    return out


def repo_config_ttl(repo: str, ruleset: str) -> str:
    """与 scheme2-onto/tools/load_tbox.py::temp_repo_config 同形（.ttl，multipart 提交）。"""
    return """@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rep: <http://www.openrdf.org/config/repository#> .
@prefix sr: <http://www.openrdf.org/config/repository/sail#> .
@prefix sail: <http://www.openrdf.org/config/sail#> .
@prefix graphdb: <http://www.ontotext.com/config/graphdb#> .

[] a rep:Repository ;
   rep:repositoryID "%s" ;
   rdfs:label "Credit Card Ops (cc-ops)" ;
   rep:repositoryImpl [
       rep:repositoryType "graphdb:SailRepository" ;
       sr:sailImpl [
           sail:sailType "graphdb:Sail" ;
           graphdb:ruleset "%s" ;
           graphdb:read-only "false"
       ]
   ] .
""" % (repo, ruleset)


def http(url: str, method: str = "GET", data=None, headers=None, timeout: float = 60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, str(exc)


def list_repos(base: str):
    st, body = http(base.rstrip("/") + "/rest/repositories", timeout=30)
    if st != 200:
        return None, "HTTP %s：%s" % (st, body[:200])
    try:
        doc = json.loads(body)
    except Exception as exc:
        return None, "响应不可解析：%s" % exc
    return [str(x.get("id")) for x in doc if isinstance(x, dict) and x.get("id")], ""


def create_repo(base: str, repo: str, ruleset: str):
    """POST /rest/repositories —— multipart，配置作为**带 filename 的** Turtle 文件。"""
    boundary = "----ccRepoSyncBoundary%d" % int(time.time() * 1000)
    body = b""
    body += ("--%s\r\n" % boundary).encode()
    body += b'Content-Disposition: form-data; name="config"; filename="config.ttl"\r\n'
    body += b"Content-Type: text/turtle\r\n\r\n"
    body += repo_config_ttl(repo, ruleset).encode("utf-8")
    body += ("\r\n--%s--\r\n" % boundary).encode()
    st, txt = http(base.rstrip("/") + "/rest/repositories", "POST", body,
                   {"Content-Type": "multipart/form-data; boundary=%s" % boundary},
                   timeout=180)
    return st, txt


def wait_queryable(base: str, repo: str, secs: int = 60) -> bool:
    """建仓后等它**真的能查询**。

    ★ 这里踩过一次坑：最初用 `GET /repositories/<repo>`（不带 query）判就绪，
      期待 200 —— 但 GraphDB 的 SPARQL 端点**没有 query 参数就返回 400**，
      于是明明建好了也永远等不到 200，报出
      「仓库已创建但 60s 内不可查询」的**假失败**
      （下一次运行又显示"仓库已存在 ⇒ 幂等"，自相矛盾，很误导）。
      ⇒ 改为发一条**真实的最小 SPARQL 查询**，200 才算就绪。
    """
    q = urllib.parse.quote("SELECT * WHERE { ?s ?p ?o } LIMIT 1")
    url = "%s/repositories/%s?query=%s" % (base.rstrip("/"), urllib.parse.quote(repo), q)
    for _ in range(secs):
        st, _t = http(url, "GET", headers={"Accept": "application/sparql-results+json"},
                      timeout=15)
        if st == 200:
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
def selftest() -> int:
    """离线自检：不连任何服务。"""
    rows = []
    ttl = repo_config_ttl("cc-ops", "owl2-rl")
    rows.append(("TTL 含 repositoryID", 'rep:repositoryID "cc-ops"' in ttl))
    rows.append(("TTL 含 ruleset", 'graphdb:ruleset "owl2-rl"' in ttl))
    rows.append(("TTL 以 . 结尾", ttl.rstrip().endswith(".")))
    # 负控：空仓库名必须体现出来（调用方应拦住）
    rows.append(("空仓库名不会生成合法 ID", 'rep:repositoryID ""' in repo_config_ttl("", "x")))
    bad = 0
    print("=" * 70)
    print("create_graphdb_repo.py --selftest（离线）")
    print("=" * 70)
    for name, ok in rows:
        print("  [%s] %s" % ("OK  " if ok else "BAD ", name))
        if not ok:
            bad += 1
    print("-" * 70)
    print("自检 %d 条，BAD=%d ⇒ %s" % (len(rows), bad, "PASS" if bad == 0 else "FAIL"))
    return PASS if bad == 0 else FAIL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="确保 GraphDB 存在项目专属仓库（幂等）")
    ap.add_argument("--targets", help="targets.yaml（可用 install.sh 生成的叠加契约）")
    ap.add_argument("--base", default=None, help="覆盖 GraphDB 基址，如 http://127.0.0.1:7200")
    ap.add_argument("--repository", default=None, help="覆盖仓库名")
    ap.add_argument("--ruleset", default=None, help="覆盖 ruleset（默认取契约）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    base = args.base
    repo = args.repository
    ruleset = args.ruleset or "owl2-rl"
    if args.targets:
        try:
            cfg = read_targets(args.targets)
        except SystemExit as exc:
            print(exc)
            return BLOCKED
        base = base or cfg.get("url")
        repo = repo or cfg.get("repository")
        ruleset = args.ruleset or cfg.get("ruleset") or "owl2-rl"
    if not base or not repo:
        print("BLOCKED：需要 --base 与 --repository（或提供一个含 graphdb 段的 --targets）")
        return BLOCKED

    print("GraphDB 基址   ：%s" % base)
    print("目标仓库       ：%s（ruleset=%s）" % (repo, ruleset))

    ids, err = list_repos(base)
    if ids is None:
        print("FAIL：GraphDB 不可达 → %s" % err)
        return FAIL
    print("现有仓库（%d）：%s" % (len(ids), ", ".join(ids) or "（空）"))

    if repo in ids:
        print("OK：仓库 %s 已存在 ⇒ 幂等，不做任何改动" % repo)
        return PASS

    print("仓库 %s 不存在 ⇒ 创建（multipart Turtle 配置，filename 不可省）" % repo)
    st, txt = create_repo(base, repo, ruleset)
    if st is None or st // 100 != 2:
        print("FAIL：创建仓库失败 HTTP %s：%s" % (st, txt[:300]))
        return FAIL
    if not wait_queryable(base, repo, 60):
        print("FAIL：仓库 %s 已创建但 60s 内不可查询" % repo)
        return FAIL

    ids2, _ = list_repos(base)
    print("OK：仓库 %s 创建成功并可查询；现有仓库：%s" % (repo, ", ".join(ids2 or [])))
    return PASS


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
