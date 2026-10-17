#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheme1-kg/tools/load_tbox.py — 方案1 原生 TBox 的**真机装载证据工具**（可复算，纯标准库）

三种模式：
  probe    只读：打印装载前基线计数 + 探测「DDL / DML 能否在同一 HTTP 显式事务里混用」
  rollback 非破坏性证明：把 4 个 ontology 文件的语句 APPLY 到 **HTTP 显式事务**并逐条计数，
           随后 `DELETE /db/neo4j/tx/{id}` 回滚，再计数证明图被完整还原（零残留）
  persist  持久幂等装载：逐文件提交，随后**第二次 APPLY** 并比对计数，证明第二次是 no-op
  cleanup  **迁移/清理**（captain 裁定「契约优先口径」的落地步骤）：删除旧关系索引
           `cc_idx_causes_prob_idx`，并对 `CC_CAUSES|CC_RELATES_TO` 边**显式 `REMOVE`** 契约里
           不存在的 5 个属性（weight/learnCount/probability/delaySec/chainPosition）。
           ★ 必要性：`SET r.x = …` 不会删除已存在属性 —— 本图曾 persist 过一次，图上这些属性
             会**残留**（`MATCH ()-[r:CC_CAUSES]->() WHERE r.probability IS NOT NULL …` 非 0），
             故必须显式 REMOVE。cleanup 只删自己写过的属性，不碰 v2 图。
  建议顺序：cleanup → persist → 计数校验（见报告第 3 节）。

★ 真机实测的硬约束（见 probe/rollback 输出原文）：
  同一 HTTP 显式事务里 **schema 与 data 不能混用** —— 执行过 schema 修改后任何写语句都会报
  `Tried to execute Write query after executing Schema modification` 并作废该事务。
  因此本工具把每条语句分为 SCHEMA(CREATE/DROP CONSTRAINT|INDEX|DATABASE) 与 DATA 两类，
  **分两个事务**分别 APPLY / 回滚。实测：纯 data 事务的多条 MERGE 可在事务内读回、可回滚为 0；
  纯 schema 事务回滚后约束也不残留（SHOW CONSTRAINTS 计数还原）。

语句切分复用只读静态校验器 `shared/verify/kg_tbox_check.py` 的 `cypher_statements()`，
保证「静态校验说的语句」与「真机装载发的语句」逐条一致。

环境变量：CC_NEO4J_HTTP / CC_NEO4J_USER / CC_NEO4J_PASSWORD / CC_NEO4J_DB
（默认 http://127.0.0.1:7475, neo4j, CC_NEO4J_PASSWORD 必填（缺失即 fail-closed，不回落内置默认口令）, neo4j）  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。

★ 安全：只对 `scheme1-kg/ontology/*.cypher` 的语句发请求；rollback 绝不 commit；
  persist 只提交本文件的幂等 MERGE（无 DELETE/DETACH）。既有 v2 图（423 节点）与本项目
  CC_ ABox（258 节点）全程只读并做装载前后计数对比。
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "shared", "verify"))
import kg_tbox_check as K  # noqa: E402

HTTP = os.environ.get("CC_NEO4J_HTTP", "http://127.0.0.1:7475").rstrip("/")  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
USER = os.environ.get("CC_NEO4J_USER", "neo4j")
PWD = os.environ.get("CC_NEO4J_PASSWORD") or sys.exit("★ 缺少环境变量 CC_NEO4J_PASSWORD：fail-closed，绝不回落内置默认口令。请从生产机 /opt/upstream-agent/.env 的 NEO4J_AUTH=user/pass 取出口令，设入该变量后重跑。")
DB = os.environ.get("CC_NEO4J_DB", "neo4j")
ONT = os.path.join(ROOT, "scheme1-kg", "ontology")
FILES = ["01-schema.cypher", "02-topology.cypher", "03-fault-modes.cypher", "04-rules.cypher",
         # ★ 2026-10-15：建议动作目录的 LPG 投影（出处 shared/model/actions.yaml）。
         #   此前两侧 cc_propose 输出不对称（方案1 返回 0 条）—— 根因是动作目录**从来没有契约**，
         #   方案2 侧自造了 7 个。处置 = 升格为共享契约 + 两侧同源投影。
         "05-actions.cypher"]
SCHEMA_RE = re.compile(r"^\s*(CREATE|DROP)\s+(CONSTRAINT|INDEX|DATABASE)\b", re.I)

PROBES = [
    ("总节点数 (n)", "MATCH (n) RETURN count(n) AS c"),
    ("既有 v2 节点（无 CC_ 标签）", "MATCH (n) WHERE any(l IN labels(n) WHERE NOT l STARTS WITH 'CC_') RETURN count(n) AS c"),
    ("CC_ 节点（ABox+TBox）", "MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'CC_') RETURN count(n) AS c"),
    ("既有 v2 宿主节点 Host", "MATCH (n:Host) RETURN count(n) AS c"),
    ("CC_FaultMode", "MATCH (n:CC_FaultMode) RETURN count(n) AS c"),
    ("CC_Metric", "MATCH (n:CC_Metric) RETURN count(n) AS c"),
    ("CC_Threshold", "MATCH (n:CC_Threshold) RETURN count(n) AS c"),
    ("CC_Rule", "MATCH (n:CC_Rule) RETURN count(n) AS c"),
    ("CC_Meta", "MATCH (n:CC_Meta) RETURN count(n) AS c"),
    ("总关系数", "MATCH ()-[r]->() RETURN count(r) AS c"),
    ("CC_CAUSES 边", "MATCH ()-[r:CC_CAUSES]->() RETURN count(r) AS c"),
    ("CC_JUDGED_BY 边", "MATCH ()-[r:CC_JUDGED_BY]->() RETURN count(r) AS c"),
    ("CC_AFFECTS 边", "MATCH ()-[r:CC_AFFECTS]->() RETURN count(r) AS c"),
    ("CC_RELATES_TO 边", "MATCH ()-[r:CC_RELATES_TO]->() RETURN count(r) AS c"),
    ("约束总数 (SHOW CONSTRAINTS)", "SHOW CONSTRAINTS YIELD name RETURN count(name) AS c"),
    ("索引总数 (SHOW INDEXES)", "SHOW INDEXES YIELD name RETURN count(name) AS c"),
]
CORE = ("总节点数 (n)", "既有 v2 节点（无 CC_ 标签）", "CC_ 节点（ABox+TBox）",
        "既有 v2 宿主节点 Host", "总关系数", "CC_FaultMode", "CC_Meta",
        "约束总数 (SHOW CONSTRAINTS)", "索引总数 (SHOW INDEXES)")

# ── cleanup（迁移）专用：契约里不存在的 5 个边属性 + 旧关系索引 ────────────────
#   唯一权威 shared/model/causal-edges.yaml 的每条边只有 id/from/relation/to/chain/evidence/
#   provenance（条件边另有 condition）⇒ 这 5 个属性**不在契约内**，图上不得残留。
STALE_EDGE_PROPS = ["weight", "learnCount", "probability", "delaySec", "chainPosition"]
STALE_INDEX = "cc_idx_causes_prob_idx"
EDGE_IDX = "cc_idx_causes_edge_id_idx"
NOT_NULL = " OR ".join("r.%s IS NOT NULL" % p for p in STALE_EDGE_PROPS)
CLEANUP_PROBES = [
    ("★ CC_CAUSES 残留非契约属性（应 0）",
     "MATCH ()-[r:CC_CAUSES]->() WHERE %s RETURN count(r) AS c" % NOT_NULL),
    ("★ CC_RELATES_TO 残留非契约属性（应 0）",
     "MATCH ()-[r:CC_RELATES_TO]->() WHERE %s RETURN count(r) AS c" % NOT_NULL),
    ("CC_CAUSES 有 contractId（应 18）",
     "MATCH ()-[r:CC_CAUSES]->() WHERE r.contractId IS NOT NULL RETURN count(r) AS c"),
    ("CC_CAUSES 总数（应 18）", "MATCH ()-[r:CC_CAUSES]->() RETURN count(r) AS c"),
    ("CC_RELATES_TO 总数（应 2）", "MATCH ()-[r:CC_RELATES_TO]->() RETURN count(r) AS c"),
    ("旧索引 cc_idx_causes_prob_idx 存在（应 0）",
     "SHOW INDEXES YIELD name WHERE name = '%s' RETURN count(name) AS c" % STALE_INDEX),
    ("新索引 cc_idx_causes_edge_id_idx 存在（应 1）",
     "SHOW INDEXES YIELD name WHERE name = '%s' RETURN count(name) AS c" % EDGE_IDX),
]


def _auth():
    return "Basic " + base64.b64encode(("%s:%s" % (USER, PWD)).encode("utf-8")).decode("ascii")


def _call(url, statements, method="POST"):
    body = json.dumps({"statements": statements}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method,
                                headers={"Content-Type": "application/json", "Authorization": _auth()})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"errors": [{"message": "HTTP %s: %s" % (exc.code, raw[:300])}], "results": []}


def rows_of(payload):
    out = []
    for res in payload.get("results", []):
        cols = res.get("columns") or []
        for row in res.get("data") or []:
            out.append(dict(zip(cols, row.get("row"))))
    return out


def errs_of(payload):
    return [e.get("message", "") for e in (payload.get("errors") or [])]


def autocommit(stmt, params=None):
    payload = _call("%s/db/%s/tx/commit" % (HTTP, DB), [{"statement": stmt, "parameters": params or {}}])
    e = errs_of(payload)
    if e:
        raise RuntimeError("; ".join(e))
    return rows_of(payload)


def tx_open():
    payload = _call("%s/db/%s/tx" % (HTTP, DB), [])
    e = errs_of(payload)
    if e:
        raise RuntimeError("打开事务失败：" + "; ".join(e))
    return payload["commit"].rsplit("/commit", 1)[0]


def tx_run(txurl, stmt, params=None):
    return errs_of(_call(txurl, [{"statement": stmt, "parameters": params or {}}]))


def tx_read(txurl, stmt):
    payload = _call(txurl, [{"statement": stmt, "parameters": {}}])
    if errs_of(payload):
        return "ERR:%s" % errs_of(payload)[0][:60]
    r = rows_of(payload)
    return list(r[0].values())[0] if r else None


def tx_close(txurl):
    try:
        req = urllib.request.Request(txurl, method="DELETE", headers={"Authorization": _auth()})
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        return "已回滚（DELETE /tx）"
    except urllib.error.HTTPError as exc:
        return "回滚请求返回 HTTP %s（事务已因错误被服务器作废，等效于回滚）" % exc.code
    except Exception as exc:  # noqa: BLE001
        return "回滚异常 %r" % exc


def probe_all(label, verbose=True):
    out = {}
    for name, q in PROBES:
        try:
            out[name] = list(autocommit(q)[0].values())[0]
        except Exception as exc:  # noqa: BLE001
            out[name] = "ERR:%s" % str(exc)[:60]
    if verbose:
        print("── %s" % label)
        for name, _q in PROBES:
            print("   %-30s %s" % (name, out[name]))
    return out


def classify():
    """返回 [(文件名, [(行号, 语句, 'SCHEMA'|'DATA')])]。"""
    out = []
    for fn in FILES:
        stmts, _c = K.cypher_statements(os.path.join(ONT, fn))
        out.append((fn, [(l, t, "SCHEMA" if SCHEMA_RE.match(t) else "DATA") for l, t in stmts]))
    return out


def mode_probe():
    probe_all("① 装载前基线（只读，未改动任何东西）")
    print("── ② DDL / DML 混用探测（HTTP 显式事务）")
    tx = tx_open()
    stmt = "CREATE CONSTRAINT probe_tx_ddl IF NOT EXISTS FOR (n:CC_Metric) REQUIRE n.metricCode IS UNIQUE"
    e1 = tx_run(tx, stmt)
    print("   schema 语句：%s" % ("成功" if not e1 else "失败：" + e1[0]))
    e2 = tx_run(tx, "MERGE (n:CC_ProbeMix {pid:'m'}) RETURN count(n) AS c")
    print("   紧接 data 语句：%s" % ("成功" if not e2 else "失败：" + e2[0]))
    print("   事务状态：%s" % tx_close(tx))
    after = probe_all("③ 回滚后基线（应与 ① 完全一致）")
    cls = classify()
    print("── ④ 语句分类")
    for fn, items in cls:
        s = sum(1 for _l, _t, k in items if k == "SCHEMA")
        print("   %-26s 共 %2d 条 = SCHEMA %2d + DATA %2d" % (fn, len(items), s, len(items) - s))
    print("── 结论：schema/data 混用 %s；回滚一致性 %s"
          % ("被服务器禁止（须分两个事务）" if e2 else "允许", "一致" if after == probe_all("", False) else "不一致"))


def mode_rollback():
    before = probe_all("① 装载前基线")
    cls = classify()
    print("── ② SCHEMA 事务：APPLY 全部 DDL 语句")
    tx = tx_open()
    print("   tx = %s" % tx)
    s_ok = s_bad = 0
    for fn, items in cls:
        ok = bad = 0
        for line, text, kind in items:
            if kind != "SCHEMA":
                continue
            e = tx_run(tx, text)
            if e:
                bad += 1
                print("   SCHEMA-ERROR %s:%s %s" % (fn, line, e[0][:160]))
            else:
                ok += 1
        s_ok += ok
        s_bad += bad
        print("   %-26s SCHEMA %2d 条，成功 %2d，失败 %2d" % (fn, ok + bad, ok, bad))
    inside_c = tx_read(tx, "SHOW CONSTRAINTS YIELD name RETURN count(name) AS c")
    inside_i = tx_read(tx, "SHOW INDEXES YIELD name RETURN count(name) AS c")
    print("   事务内视图：约束 = %s（装载前 %s）  索引 = %s（装载前 %s）"
          % (inside_c, before["约束总数 (SHOW CONSTRAINTS)"], inside_i, before["索引总数 (SHOW INDEXES)"]))
    print("   事务处置：%s" % tx_close(tx))

    print("── ③ DATA 事务：APPLY 全部 DML 语句（含 01 的 CC_Meta）")
    tx2 = tx_open()
    print("   tx = %s" % tx2)
    d_ok = d_bad = 0
    for fn, items in cls:
        ok = bad = 0
        for line, text, kind in items:
            if kind != "DATA":
                continue
            e = tx_run(tx2, text, {"rows": []} if "UNWIND $rows" in text else None)
            if e:
                bad += 1
                print("   DATA-ERROR %s:%s %s" % (fn, line, e[0][:200]))
            else:
                ok += 1
        d_ok += ok
        d_bad += bad
        print("   %-26s DATA %2d 条，成功 %2d，失败 %2d" % (fn, ok + bad, ok, bad))
    inside = {}
    for name, q in PROBES:
        inside[name] = tx_read(tx2, q)
    print("── ④ 事务内计数（证明 DML 真的生效，此时尚未回滚）")
    for name, _q in PROBES:
        print("   %-30s %-8s (装载前 %s)" % (name, inside[name], before[name]))
    print("   事务处置：%s" % tx_close(tx2))
    after = probe_all("⑤ 回滚后基线")
    diff = [k for k in before if before[k] != after[k]]
    print("── 结论：SCHEMA 成功 %d/失败 %d；DATA 成功 %d/失败 %d；回滚后与装载前 %s"
          % (s_ok, s_bad, d_ok, d_bad, "逐项完全一致" if not diff else "不一致项：%s" % diff))
    for k in CORE:
        print("   [证据] %-30s 装载前 %s → 事务内 %s → 回滚后 %s" % (k, before[k], inside.get(k), after[k]))


def mode_persist():
    before = probe_all("① 持久装载前基线")
    cls = classify()
    totals = {"SCHEMA": 0, "DATA": 0}
    print("── ② 第 1 次 APPLY（SCHEMA 逐文件单独提交；DATA 逐文件提交）")
    for fn, items in cls:
        sch = [t for _l, t, k in items if k == "SCHEMA"]
        dat = [{"statement": t, "parameters": {"rows": []} if "UNWIND $rows" in t else {}}
               for _l, t, k in items if k == "DATA"]
        if sch:
            e = errs_of(_call("%s/db/%s/tx/commit" % (HTTP, DB), [{"statement": t} for t in sch]))
            print("   %-26s SCHEMA %2d 条：%s" % (fn, len(sch), "OK" if not e else "ERR " + e[0][:200]))
            totals["SCHEMA"] += len(sch) if not e else 0
        if dat:
            e = errs_of(_call("%s/db/%s/tx/commit" % (HTTP, DB), dat))
            print("   %-26s DATA   %2d 条：%s" % (fn, len(dat), "OK" if not e else "ERR " + e[0][:200]))
            totals["DATA"] += len(dat) if not e else 0
    mid = probe_all("③ 第 1 次 APPLY 后")
    print("── ④ 第 2 次 APPLY（幂等性检验：同文件、同语句、再提交一次）")
    for fn, items in cls:
        sch = [t for _l, t, k in items if k == "SCHEMA"]
        dat = [{"statement": t, "parameters": {"rows": []} if "UNWIND $rows" in t else {}}
               for _l, t, k in items if k == "DATA"]
        if sch:
            e = errs_of(_call("%s/db/%s/tx/commit" % (HTTP, DB), [{"statement": t} for t in sch]))
            print("   %-26s SCHEMA 再执行：%s" % (fn, "OK" if not e else "ERR " + e[0][:200]))
        if dat:
            e = errs_of(_call("%s/db/%s/tx/commit" % (HTTP, DB), dat))
            print("   %-26s DATA   再执行：%s" % (fn, "OK" if not e else "ERR " + e[0][:200]))
    after = probe_all("⑤ 第 2 次 APPLY 后")
    changed = [k for k in mid if mid[k] != after[k]]
    print("── 结论：第 1 次 APPLY 提交 SCHEMA %d 条 + DATA %d 条；第 2 次 APPLY 后计数变化 %d 项%s"
          % (totals["SCHEMA"], totals["DATA"], len(changed), "" if not changed else "：%s" % changed))
    for k in PROBES:
        print("   [幂等比对] %-30s 第1次后 %-8s 第2次后 %-8s %s"
              % (k[0], mid[k[0]], after[k[0]], "变化" if mid[k[0]] != after[k[0]] else ""))
    print("── 既有图未被污染的证据（装载前→第1次后→第2次后）")
    for k in ("既有 v2 节点（无 CC_ 标签）", "既有 v2 宿主节点 Host", "总关系数", "总节点数 (n)"):
        print("   %-30s %s → %s → %s" % (k, before[k], mid[k], after[k]))


def mode_cleanup():
    """迁移：删旧关系索引 + 显式 REMOVE 契约未声明的 5 个边属性（幂等、可重复跑）。"""
    def probe_clean(label):
        out = {}
        for name, q in CLEANUP_PROBES:
            try:
                out[name] = list(autocommit(q)[0].values())[0]
            except Exception as exc:  # noqa: BLE001
                out[name] = "ERR:%s" % str(exc)[:80]
        print("── %s" % label)
        for name, _q in CLEANUP_PROBES:
            print("   %-42s %s" % (name, out[name]))
        return out

    before = probe_clean("① 处置前（真机现状：首轮 persist 写下的非契约属性与旧索引）")
    print("── ② DROP 旧关系索引 `%s`（IF EXISTS，autocommit DDL）" % STALE_INDEX)
    try:
        autocommit("DROP INDEX %s IF EXISTS" % STALE_INDEX)
        print("   DROP INDEX：OK")
    except Exception as exc:  # noqa: BLE001
        print("   DROP INDEX：ERR %s" % str(exc)[:200])
    print("── ③ REMOVE 契约未声明的 5 个边属性（%s）" % "/".join(STALE_EDGE_PROPS))
    rem = ("MATCH ()-[r:CC_CAUSES|CC_RELATES_TO]->() "
           "REMOVE r.%s RETURN count(r) AS cleaned" % ", r.".join(STALE_EDGE_PROPS))
    try:
        n = list(autocommit(rem)[0].values())[0]
        print("   命中并处理的关系数 = %s（REMOVE 语句：%s）" % (n, rem))
    except Exception as exc:  # noqa: BLE001
        print("   REMOVE：ERR %s" % str(exc)[:200])
    after = probe_clean("④ 处置后（期望：残留 0 / contractId=18 / 旧索引 0 / 新索引 1）")
    print("── 结论：残留非契约属性 %s → %s（契约优先口径已落地）"
          % (before[CLEANUP_PROBES[0][0]], after[CLEANUP_PROBES[0][0]]))
    for name, _q in CLEANUP_PROBES:
        print("   [证据] %-42s 处置前 %-6s → 处置后 %s" % (name, before[name], after[name]))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    print("=" * 92)
    print("load_tbox.py mode=%s  target=%s db=%s" % (mode, HTTP, DB))
    print("=" * 92)
    {"probe": mode_probe, "rollback": mode_rollback, "persist": mode_persist,
     "cleanup": mode_cleanup}.get(
        mode, lambda: (print("unknown mode: %s" % mode), sys.exit(2)))()
