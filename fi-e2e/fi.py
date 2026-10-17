# -*- coding: utf-8 -*-
"""fi-e2e/fi.py —— 端到端故障注入「故障—根因匹配性」测试器。

链路（每个用例一次完整闭环）：
    清基线 → 起流量 → 注入单一故障 → 等待生效 → 冻结 CCF → 双载体投影
    → ①判据层：用契约 detect 规则在 CCF 上机判（载体无关）
    → ②根因层：把判据层检出的症状码喂给两侧 /cause，取候选根因排序
    → 评分 → 清故障

设计原则（与契约对齐，不可简化）：
  * **不可判 ≠ 未命中**：判据指标无观测时按 fault-modes.yaml:36-39 报 UNJUDGEABLE，绝不当 false。
  * **真值只来自 injected 类型**（fi-e2e/fault-map.yaml），并**不**要求被测系统报得出——
    报不出就是被测系统的结论，不是本测试的失败。
  * **闭卷**：被测侧（:8187 采集 → CCF → 图/本体）永不读 :8090 的 /api/*；
    :8090 只被本测试器（A 侧/注入侧）使用。
  * 一切判定可复算：原始 CCF、判据逐子句轨迹、两侧 /cause 原始响应全部落盘。

用法：
    python fi-e2e/fi.py probe                          # 离线：判据可判性核验
    python fi-e2e/fi.py status                          # 只读：注入台状态
    python fi-e2e/fi.py detect --ccf <path>             # 离线：在 CCF 上跑契约判据
    python fi-e2e/fi.py case  --type sql_deadlock       # 单用例全闭环
    python fi-e2e/fi.py suite --types a,b,c --duration 300
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 pyyaml\n")
    raise

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODEL = os.path.join(ROOT, "shared", "model")
OUTROOT = os.path.join(HERE, "out")

# ── 端点（全部在 192.0.2.11；84 是过期栈，禁用） ───────────────────────────
HOST = os.environ.get("FI_HOST", "192.0.2.11")
CONSOLE = "http://%s:8090" % HOST          # cardsim 注入台（A 侧，闭卷：被测侧禁读）
COLLECT = "http://%s:8187" % HOST          # 采集层统一入口（被测侧唯一取数口）
TOOLS = {"KG": "http://%s:8301" % HOST, "ONTO": "http://%s:8302" % HOST}
NEO4J_URI = "bolt://%s:7688" % HOST
GRAPHDB_URL = "http://%s:7200" % HOST
GRAPHDB_REPO = "cc-ops"

PREDICATES = ("increased", "decreased", "non_zero", "is_true", "is_false", "in_enum")
TRUE, FALSE, UNK = "TRUE", "FALSE", "UNK"

# ★ L0 判定必须排除「恒涨计数器」：实测健康态下这些计数器在任何窗口都有增量，
#   拿它们当「注入生效」的证据会给出假阳性（实测：sql_deadlock 的签名指标
#   db.cluster.xact_rollback 变动，但 db.cluster.deadlocks 一动不动）。
BACKGROUND_COUNTERS = {
    "db.cluster.xact_rollback", "db.cluster.xact_commit", "db.cluster.deadlocks",
    "db.cluster.tup_inserted", "db.cluster.tup_updated", "db.cluster.tup_deleted",
    "db.cluster.tup_returned", "db.cluster.tup_fetched", "db.cluster.wal_bytes",
    "db.table.seq_scan", "db.table.idx_scan", "db.table.live_tuples",
    "db.instance.session_count", "db.instance.active_sessions",
}

VERDICTS = ("HIT", "HIT_SYMPTOM_ONLY", "MISS", "UNJUDGEABLE", "NOT_APPLICABLE", "BLOCKED")


# ══════════════════════════════════════════════════════════════════════════
# 0. 基础设施
# ══════════════════════════════════════════════════════════════════════════
def log(msg=""):
    print(msg, flush=True)


def http_json(url, payload=None, method=None, timeout=60, accept=None):
    data = None
    headers = {}
    if accept:
        headers["Accept"] = accept
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:4000]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s :: %s" % (e.code, url, body)) from None
    except Exception as e:
        raise RuntimeError("无法连接 %s :: %s" % (url, e)) from None


def load_yaml(path):
    with io.open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, timeout=1800, cwd=ROOT):
    t0 = time.time()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return {"rc": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or "",
            "elapsed_s": round(time.time() - t0, 1), "cmd": cmd}


# ══════════════════════════════════════════════════════════════════════════
# 1. 注入台客户端（A 侧）
# ══════════════════════════════════════════════════════════════════════════
class Console:
    def __init__(self, base=CONSOLE):
        self.base = base.rstrip("/")

    def snapshot(self):
        return http_json(self.base + "/api/snapshot", timeout=30)

    def fault_list(self):
        return http_json(self.base + "/api/fault", {"action": "list"})

    def inject(self, ftype):
        """★ 契约要求：重复注入同类 → 400。调用方必须先 clear。"""
        return http_json(self.base + "/api/fault", {"action": "inject", "type": ftype})

    def clear(self, ftype=None):
        body = {"action": "clear"}
        if ftype:
            body["type"] = ftype
        return http_json(self.base + "/api/fault", body)

    def clear_all(self):
        return self.clear(None)

    def sim_start(self, duration):
        return http_json(self.base + "/api/sim", {"action": "start", "duration": int(duration)})

    def sim_stop(self):
        return http_json(self.base + "/api/sim", {"action": "stop"})

    def prefs(self, threads=8, flow=0):
        return http_json(self.base + "/api/prefs", {"threads": threads, "flowThreads": flow,
                                                    "_rid": "fi-%d" % int(time.time() * 1000)})

    def scale(self, delta=1):
        return http_json(self.base + "/api/scale", {"delta": int(delta)})

    def spawn(self, container, threads=8):
        return http_json(self.base + "/api/spawn", {"container": container, "threads": int(threads)})

    def containers_api(self):
        return http_json(self.base + "/api/containers", {})

    def run_running(self):
        """★ 运行中的轮次**不在** results[] 里（那里只有已结束的 results/simulation-*.json），
        只出现在 应用层 signal『最新 run #N = RUNNING』。"""
        s = self.snapshot()
        for r in (s.get("results") or []):
            if r.get("status") == "RUNNING":
                return r
        for x in s.get("signals") or []:
            if x.get("layer") == "应用层" and x.get("name", "").startswith("最新 run"):
                if x.get("value") == "RUNNING":
                    return {"runId": x.get("name"), "status": "RUNNING",
                            "raw": x.get("name"), "detail": x.get("detail")}
                return None
        return None

    def standby_containers(self):
        s = self.snapshot()
        return [c["name"] for c in (s.get("containers") or [])
                if c.get("running") and c.get("java_procs")]

    def active(self):
        return (self.fault_list().get("faults") or [])

    def waits_clean(self, timeout=15.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.active():
                return True
            time.sleep(1.0)
        return False


# ══════════════════════════════════════════════════════════════════════════
# 2. 契约判据机（在 CCF 上机判 24 个故障模式）
# ══════════════════════════════════════════════════════════════════════════
class Contract:
    def __init__(self):
        fm = load_yaml(os.path.join(MODEL, "fault-modes.yaml"))
        self.modes = {m["code"]: m for m in fm["fault_modes"]}
        self.thresholds = {t["name"]: t for t in fm.get("thresholds") or []}
        ce = load_yaml(os.path.join(MODEL, "causal-edges.yaml"))
        self.leaves = {l["subtype"]: l for l in (ce.get("leaf_root_causes") or [])
                       if l.get("subtype")}
        self.all_codes = sorted(set(self.modes) | set(self.leaves))
        # ★ /cause 的输入词表 = 因果图的节点集。真机实测（:8302 的 400 报文）：
        #   它是 14 个码，**不等于** fault_modes 的 23+1。
        #   落在图外的码「能检出、但无法表达进因果链」——这是契约内部不一致，必须单列。
        edges = ce.get("edges") or []
        self.cause_vocab = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
        self.not_expressible = sorted(set(self.all_codes) - set(self.cause_vocab))

    def detect_metrics(self, code):
        """该模式 detect 子句引用的全部指标码。"""
        m = self.modes.get(code)
        if not m:
            return []
        return sorted({c["metric"] for _g, c in clauses(m.get("detect")) if c.get("metric")})


def clauses(detect):
    """把 detect 展开成 [(group, clause)]；无分组则视为 single。"""
    if not detect:
        return []
    out = []
    for g in ("all_of", "any_of"):
        for c in detect.get(g) or []:
            out.append((g, c))
    if not out:
        out.append(("single", detect))
    return out


class CCFView:
    """冻结 CCF 的只读视图：观测索引 + 对象属性索引。"""

    def __init__(self, doc):
        self.doc = doc
        self.obs_by_metric = {}
        for o in doc.get("observations") or []:
            self.obs_by_metric.setdefault(o.get("metricCode"), []).append(o)
        self.obj = {o["id"]: o for o in doc.get("objects") or []}

    def metrics_present(self):
        return {k for k, v in self.obs_by_metric.items()
                if any(o.get("status") == "collected" for o in v)}


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return None


def eval_clause(c, obs, view, contract):
    """单子句判定 → (tri-state, trace dict)。"""
    metric = c.get("metric")
    if not metric:
        return UNK, {"reason": "无 metric", "clause": c}
    o = obs
    if o is None:
        return UNK, {"metric": metric, "reason": "该指标在本次快照无观测（探针不伪造 0）"}
    if o.get("status") != "collected":
        return UNK, {"metric": metric, "reason": "观测状态=%s" % o.get("status")}

    if c.get("op"):
        op = c["op"]
        operand = c.get("operand")
        val, ref, detail = None, None, {}
        if isinstance(operand, str) and operand.startswith("threshold:"):
            spec = operand.split(":", 1)[1]
            name, _, tier = spec.partition("#")
            t = contract.thresholds.get(name)
            if not t:
                return UNK, {"metric": metric, "reason": "未知阈值 %s" % name}
            if not tier:
                return UNK, {"metric": metric, "reason": "阈值档位缺失（契约禁止）"}
            ref = t.get(tier)
            detail = {"threshold": name, "tier": tier, "value": ref}
            if ref is None:
                return UNK, {"metric": metric, "reason": "阈值档位 %s#%s 未标定" % (name, tier)}
        elif isinstance(operand, str) and operand.startswith("entity_property:"):
            spec = operand.split(":", 1)[1]
            typ, _, attr = spec.partition(".")
            owner = view.obj.get(o.get("objectId")) or {}
            ref = (owner.get("attrs") or {}).get(attr)
            detail = {"entityProperty": spec, "value": ref}
            if ref is None:
                return UNK, {"metric": metric, "reason": "对象属性 %s 缺失" % spec}
        else:
            return UNK, {"metric": metric, "reason": "无法解析 operand %r" % operand}

        val = _num(o.get("value"))
        rv = _num(ref)
        if val is None or rv is None:
            return UNK, {"metric": metric, "reason": "非数值参与比较",
                         "observed": o.get("value"), "operand": ref}
        ok = {">": val > rv, ">=": val >= rv, "<": val < rv,
              "<=": val <= rv, "==": val == rv, "!=": val != rv}.get(op)
        if ok is None:
            return UNK, {"metric": metric, "reason": "未知 op %s" % op}
        return (TRUE if ok else FALSE), {"metric": metric, "op": op, "observed": val,
                                         "operand": ref, "operandDetail": detail,
                                         "role": c.get("role"), "outcome": bool(ok)}

    pred = c.get("predicate")
    if pred not in PREDICATES:
        return UNK, {"metric": metric, "reason": "未知 predicate %r" % pred}
    if pred in ("increased", "decreased"):
        if o.get("delta") is None:
            return UNK, {"metric": metric, "reason": "delta 缺失 ⇒ 按契约不可判（不得当 false）"}
        d = _num(o.get("delta"))
        if d is None:
            return UNK, {"metric": metric, "reason": "delta 非数值"}
        ok = d > 0 if pred == "increased" else d < 0
        return (TRUE if ok else FALSE), {"metric": metric, "predicate": pred,
                                         "delta": d, "outcome": bool(ok)}
    if pred == "non_zero":
        v = _num(o.get("value"))
        if v is None:
            return UNK, {"metric": metric, "reason": "value 非数值"}
        return (TRUE if v != 0 else FALSE), {"metric": metric, "predicate": pred,
                                             "observed": v, "outcome": v != 0}
    if pred in ("is_true", "is_false"):
        v = o.get("value")
        b = v if isinstance(v, bool) else (str(v).strip().lower() in ("true", "t", "1")
                                           if v is not None else None)
        if b is None:
            return UNK, {"metric": metric, "reason": "value 非布尔"}
        ok = b if pred == "is_true" else (not b)
        return (TRUE if ok else FALSE), {"metric": metric, "predicate": pred,
                                         "observed": v, "outcome": bool(ok)}
    if pred == "in_enum":
        vt = o.get("valueText", o.get("value"))
        vals = c.get("values") or []
        if vt is None:
            return UNK, {"metric": metric, "reason": "valueText 缺失"}
        ok = vt in vals
        return (TRUE if ok else FALSE), {"metric": metric, "predicate": pred,
                                         "observed": vt, "values": vals, "outcome": bool(ok)}
    return UNK, {"metric": metric, "reason": "未覆盖的 predicate"}


def _group_state(states):
    """组内语义：all_of=AND，any_of=OR；UNK 传播（否定优先于未知）。"""
    if not states:
        return TRUE
    if all(s == TRUE for s in states):
        return TRUE
    if any(s == FALSE for s in states):
        return FALSE
    return UNK


def eval_mode(code, view, contract):
    """在 CCF 上机判一个故障模式 → dict(state, perObject, trace)。"""
    m = contract.modes.get(code)
    if not m:
        leaf = contract.leaves.get(code)
        if leaf:
            return {"code": code, "state": UNK, "mode": "exclusion",
                    "reason": "exclusion 叶：排除法判定，不做正向 detect",
                    "trace": [{"role": "exclusion", "detail": leaf.get("exclusion_requirements")}]}
        return {"code": code, "state": UNK, "reason": "契约中无此码"}

    by_group = {"all_of": [], "any_of": [], "single": []}
    for g, c in clauses(m.get("detect")):
        by_group.setdefault(g, []).append(c)

    # 按「参与对象」分别判定：以各子句指标所属观测的 objectId 并集为主体
    subjects = set()
    for c in [c for cs in by_group.values() for c in cs]:
        for o in view.obs_by_metric.get(c.get("metric"), []):
            subjects.add(o.get("objectId"))
    if not subjects:
        trace = [dict(eval_clause(c, None, view, contract)[1], group=g)
                 for g, cs in by_group.items() for c in cs]
        return {"code": code, "state": UNK, "mode": m.get("detection_mode") or "detect",
                "reason": "全部判据指标无观测 ⇒ 结构性不可判", "trace": trace, "perObject": {}}

    per_object, trace = {}, []
    for subj in sorted(x for x in subjects if x):
        gstates, strs = {}, []
        for g, cs in by_group.items():
            if not cs:
                continue
            st = []
            for c in cs:
                cand = [o for o in view.obs_by_metric.get(c.get("metric"), [])
                        if o.get("objectId") == subj]
                s, tr = eval_clause(c, cand[0] if cand else None, view, contract)
                st.append(s)
                trace.append(dict(tr, group=g, objectId=subj, state=s))
            gstates[g] = _group_state(st)
            strs.append(st)
        flat = [s for st in strs for s in st]
        states = [v for v in gstates.values() if v is not None]
        combined = TRUE
        if any(s == FALSE for s in states):
            combined = FALSE
        elif any(s == UNK for s in states):
            combined = UNK
        # ★ 与服务同口径的「部分给分」读数：命中条款数 / 总条款数 / 是否全部有判定。
        #   部署侧 `cc_tools.py:2196-2218` 正是**按对象逐个求值 → 数命中数 → 取最优对象**，
        #   不做严格 AND ⇒ 契约字面（all_of=AND）与服务的排序分数是**两个不同的量**，必须都报。
        per_object[subj] = {
            "state": combined, "groups": gstates,
            "satisfiedClauses": sum(1 for s in flat if s == TRUE),
            "totalClauses": len(flat),
            "undeterminedClauses": sum(1 for s in flat if s == UNK),
            "determined": bool(flat) and not any(s == UNK for s in flat),
        }

    order = {TRUE: 2, FALSE: 1, UNK: 0}
    agg = max((v["state"] for v in per_object.values()), key=lambda s: order[s]) \
        if per_object else UNK
    best_obj = None
    if per_object:
        bid = max(per_object, key=lambda k: (per_object[k]["satisfiedClauses"], k))
        best_obj = dict(per_object[bid], objectId=bid)
    return {"code": code, "state": agg, "mode": m.get("detection_mode") or "detect",
            "layer": m.get("layer"), "componentType": m.get("component_type"),
            "severity": m.get("severity"), "perObject": per_object,
            "bestObject": best_obj, "trace": trace}


def detect_all(view, contract):
    return {code: eval_mode(code, view, contract) for code in contract.all_codes}


def judgeability(contract, present_metrics):
    """每个模式：判据指标是否齐备 → judgeable / partial / unjudgeable。"""
    out = {}
    for code in contract.all_codes:
        need = contract.detect_metrics(code)
        if not need:
            out[code] = {"status": "n/a", "missing": [], "need": []}
            continue
        missing = [m for m in need if m not in present_metrics]
        status = "judgeable" if not missing else ("unjudgeable" if len(missing) == len(need)
                                                 else "partial")
        out[code] = {"status": status, "missing": missing, "need": need}
    return out


# ══════════════════════════════════════════════════════════════════════════
# 3. 真值表
# ══════════════════════════════════════════════════════════════════════════
class FaultMap:
    def __init__(self, path=None):
        d = load_yaml(path or os.path.join(HERE, "fault-map.yaml"))
        self.meta = d.get("meta") or {}
        self.entries = {e["type"]: e for e in d["entries"]}

    def get(self, t):
        return self.entries.get(t)


# ══════════════════════════════════════════════════════════════════════════
# 4. 采集 → 冻结 → 投影 → 查询
# ══════════════════════════════════════════════════════════════════════════
def establish_delta_window(gap=12.0):
    """建立 delta 窗口（increased/decreased 判据的前提）。

    ★ 必须用**与 freeze 不同**的请求体：采集层有防循环护栏（_loop_guard），
    同一 (tool, args) 连续重复会被熔断，返回
    `{"loop_detected":true,"repeat_count":N,"instruction":...}` —— freeze 拿到它就 rc=2 拒绝固化。
    既有做法（tmp/_e2e_step1.py）是**交替层顺序**，且不使用 X-GDO-Internal 豁免头。
    """
    for layers in (["app"], ["gaussdb"]):
        body = {"layers": layers, "sections": [], "verify_sql": "",
                "cluster_oid": "", "instance_id": ""}
        http_json(COLLECT + "/tools/gather", body, timeout=300)
    time.sleep(gap)


def freeze_ccf(out_path, layers="gaussdb,os,container,app", profile="l1-l3"):
    r = run([sys.executable, os.path.join(ROOT, "shared", "collect", "freeze.py"),
             "--collect-url", COLLECT, "--layers", layers,
             "--expected-profile", profile, "--out", out_path])
    return r


def freeze_with_retry(out_path, args, attempts=3):
    """freeze 可能被采集层 _loop_guard 熔断（rc=2）。识别该特征后重新建窗口再试。"""
    last = None
    for i in range(attempts):
        r = freeze_ccf(out_path)
        last = r
        if r["rc"] == 0:
            r["attempts"] = i + 1
            return r
        blob = (r["stdout"] or "") + (r["stderr"] or "")
        if "loop_detected" not in blob and "loop_guard" not in blob and "熔断" not in blob:
            break
        time.sleep(15 + 10 * i)
        establish_delta_window(gap=args.gap + 8)
    last["attempts"] = attempts
    return last


def graphdb_graphs():
    """列出 GraphDB 仓库里的命名图及三元组数 —— 用于**陈旧快照堆积**的卫生检查。

    ★ 为什么必须查：GraphDB 的默认图是**所有命名图的并集**。本项目每投影一次就新建一个
    `.../graph/<snapshotId>` 命名图（设计如此），旧图**不会自动删除** ⇒ 实测堆了
    10 个历史快照（32k 三元组 vs 单快照 4k）。只要服务端**显式限定 scope**（ONTO 侧
    `CcToolsService.scope`）就不会读错；否则会静默读到历史数据而毫无迹象。
    """
    q = ("SELECT ?g (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } } "
         "GROUP BY ?g ORDER BY DESC(?n)")
    r = http_json("%s/repositories/%s?query=%s" % (GRAPHDB_URL, GRAPHDB_REPO,
                                                  urllib.parse.quote(q)),
                  method="GET", timeout=60, accept="application/sparql-results+json")
    out = []
    for b in ((r or {}).get("results") or {}).get("bindings") or []:
        out.append({"graph": b["g"]["value"], "triples": int(b["n"]["value"])})
    return out


def project(ccf_path, outdir, neo4j_password=None, skip=False):
    res = {}
    if skip:
        return {"skipped": True}
    pw = neo4j_password or os.environ.get("CC_NEO4J_PASSWORD", "")
    r1 = run([sys.executable, os.path.join(ROOT, "scheme1-kg", "project", "p1_neo4j.py"),
              "--ccf", ccf_path, "--uri", NEO4J_URI, "--user", "neo4j", "--password", pw,
              "--wipe", "--report", os.path.join(outdir, "p1_report.json")])
    res["kg"] = {"rc": r1["rc"], "tail": r1["stdout"][-600:]}
    r2 = run([sys.executable, os.path.join(ROOT, "scheme2-onto", "project", "p2_graphdb.py"),
              "--ccf", ccf_path, "--url", GRAPHDB_URL, "--repository", GRAPHDB_REPO,
              "--wipe", "--report", os.path.join(outdir, "p2_report.json")])
    res["onto"] = {"rc": r2["rc"], "tail": r2["stdout"][-600:]}
    # ★ 卫生检查：陈旧命名图堆积（只报告，不删——它们是历史证据）
    try:
        gs = graphdb_graphs()
        snaps = [g for g in gs if "/graph/ccf-" in g["graph"]]
        res["graphdbHygiene"] = {
            "namedGraphs": len(gs),
            "snapshotGraphs": len(snaps),
            "totalTriples": sum(g["triples"] for g in gs),
            "warning": ("★ 历史快照命名图堆积 %d 个；若服务端未显式限定 scope 会静默读到历史数据"
                        % len(snaps)) if len(snaps) > 1 else None,
        }
    except Exception as e:  # noqa: BLE001
        res["graphdbHygiene"] = {"error": str(e)[:200]}
    return res


def call_cause(carrier, codes, max_hops=4):
    url = TOOLS[carrier] + "/cause"
    try:
        r = http_json(url, {"symptomCodes": list(codes), "maxHops": max_hops}, timeout=120)
        return {"ok": True, "raw": r}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)[:400]}


_CAUSE_VOCAB_CACHE = {}


def cause_vocabulary(carrier):
    """★ 不猜词表：用哨兵码打一次 /cause，从 400 报文里读服务**实际**接受的码集。

    真机实测：服务只接受 14 个码，而 `causal-edges.yaml` 的图节点是 23 个
    ⇒ 「契约因果图 ⊋ 服务 /cause 词表」是**契约与实现不一致**的真实事实，
    必须由服务回答，不能由契约推导（推导出来的 23 会让调用 400）。
    """
    if carrier in _CAUSE_VOCAB_CACHE:
        return _CAUSE_VOCAB_CACHE[carrier]
    sentinel = "FM-SENTINEL-PROBE"
    try:
        http_json(TOOLS[carrier] + "/cause", {"symptomCodes": [sentinel], "maxHops": 1}, timeout=60)
        vocab, err = None, "服务未拒绝哨兵码（无法发现词表）"
    except RuntimeError as e:
        msg = str(e)
        vocab, err = None, msg
        i, j = msg.find("["), msg.rfind("]")
        # 报文中「本环境接受 [...]」是第二个方括号列表
        k = msg.find("本环境接受")
        if k > 0:
            seg = msg[k:]
            a, b = seg.find("["), seg.find("]")
            if a > 0 and b > a:
                vocab = sorted({x.strip().strip("'\"") for x in seg[a + 1:b].split(",") if x.strip()})
    _CAUSE_VOCAB_CACHE[carrier] = vocab
    return vocab


def health(carrier):
    try:
        return http_json(TOOLS[carrier] + "/health", timeout=20)
    except RuntimeError:
        return {}


# ══════════════════════════════════════════════════════════════════════════
# 5. 评分
# ══════════════════════════════════════════════════════════════════════════
def root_candidates(cause_resp):
    """从 /cause 响应取候选根因（按 rank 升序）→ 含 determined / 命中条款数。

    ★ 实测：候选列表常常把**几乎整个词表**都列出来（多数 undetermined、satisfiedClauses=0），
    因此 top1 是弱信号。必须同时上报列表规模与「有判定结果的候选数」，
    否则会把「确定性地返回同一个第一名」误读成「诊断准确」。
    """
    if not cause_resp or not cause_resp.get("ok"):
        return []
    cands = (cause_resp.get("raw") or {}).get("candidates") or []
    out = []
    for c in cands:
        out.append({"code": c.get("faultCode"), "isLeaf": bool(c.get("isLeafRootCandidate")),
                    "rank": c.get("rank"), "determined": c.get("determined"),
                    "satisfied": c.get("satisfiedClauses"), "total": c.get("totalClauses"),
                    # ★ 载体差异比对要用的四个字段**必须全部归一化带上**。
                    #   曾经只带 determined，导致 totalClauses / undeterminedClauses /
                    #   relationPriority 在两侧都被读成 None ⇒ **分歧被静默吞掉**
                    #   （实测：分类器报「未声明 0 处」，而直接比对同一份响应有 1 处）。
                    "satisfiedClauses": c.get("satisfiedClauses"),
                    "totalClauses": c.get("totalClauses"),
                    "undeterminedClauses": c.get("undeterminedClauses"),
                    "componentInstance": c.get("componentInstance"),
                    "relationPriority": c.get("relationPriority"),
                    "viaRelation": c.get("viaRelation"),
                    "path": c.get("path")})
    out.sort(key=lambda x: (x["rank"] if x["rank"] is not None else 999))
    return out


def cause_stats(cands, expect):
    """候选列表的可信度统计：规模、有判定的条数、命中条款>0 的条数、期望码是否在其中。"""
    n = len(cands)
    det = [c for c in cands if c.get("determined")]
    sat = [c for c in cands if (c.get("satisfied") or 0) > 0]
    return {
        "nCandidates": n,
        "nDetermined": len(det),
        "nWithSatisfiedClauses": len(sat),
        "expectInCandidates": sorted(set(expect or []) & {c["code"] for c in cands}),
        "expectInDetermined": sorted(set(expect or []) & {c["code"] for c in det}),
    }


# ── 载体「已声明差异」登记表（machine-checked；见 fi-e2e/carrier-divergences.yaml） ──
DIVERGENCE_FIELDS = ("determined", "totalClauses", "undeterminedClauses", "relationPriority",
                     # ★ 2026-10-13 补：componentInstance 是**上游**差异 —— 两侧给同一码选了不同实例，
                     #   直接决定判据能否求值（CD-19）。漏掉它会让「ONTO 侧 satisfiedClauses 恒 0」
                     #   这种致命差异**不被探测器发现**。
                     "componentInstance")


def load_declared_divergences(path=None):
    p = path or os.path.join(HERE, "carrier-divergences.yaml")
    if not os.path.exists(p):
        return {"entries": [], "index": {}, "path": None}
    d = load_yaml(p)
    idx = {(e.get("code"), e.get("field")): e for e in (d.get("entries") or [])}
    return {"entries": d.get("entries") or [], "index": idx, "path": p,
            "meta": d.get("meta") or {}}


def classify_divergences(res_kg, res_onto, declared):
    """逐码比对两侧候选的四个可机读量；命中登记表 ⇒ `declared`，否则 ⇒ `undeclared`。

    ★ 这台机器的用途：**把「已知的载体差异」与「新引入的缺陷」区分开**。
    没有它，每次 A/B 运行都会把同一批已知差异当成新问题重新发现一遍；
    有了它，新增分歧会立刻以 `undeclared` 暴露（fail-loud），据此可做回归探测。
    """
    idx = (declared or {}).get("index") or {}

    def by_code(resp):
        return {c["code"]: c for c in root_candidates(resp)}

    kg, on = by_code(res_kg), by_code(res_onto)
    dec, und = [], []
    for code in sorted(set(kg) | set(on)):
        k, o = kg.get(code) or {}, on.get(code) or {}
        for field in DIVERGENCE_FIELDS:
            kv, ov = k.get(field), o.get(field)
            if kv == ov:
                continue
            row = {"code": code, "field": field, "kg": kv, "onto": ov}
            hit = idx.get((code, field))
            if hit:
                row["declaredId"] = hit.get("id")
                row["disposition"] = hit.get("disposition")
                # ★ 登记语义（2026-10-13 更正）：登记的是「**该 (码,字段) 存在分歧**」这一**事实**，
                #   取值是**证据**而非**断言** —— 因为分歧取值随**症状集**变化（实测：
                #   同一对载体换一组症状码，同一 (码,字段) 的 kg/onto 取值就变）。
                #   故默认 `value_check: kind_only`：只要 (码,字段) 已登记即算 declared；
                #   想让某条也钉住取值，可在登记条目里显式写 `value_check: exact`。
                expect_exact = (hit.get("value_check") or "kind_only") == "exact"
                row["valueCheck"] = "exact" if expect_exact else "kind_only"
                row["matchesDeclared"] = (not expect_exact) or (
                    hit.get("kg") == kv and hit.get("onto") == ov)
                if expect_exact and not row["matchesDeclared"]:
                    row["note"] = ("登记 kg=%s/onto=%s，实测 kg=%s/onto=%s ⇒ 登记已过期，须更新"
                                   % (hit.get("kg"), hit.get("onto"), kv, ov))
                dec.append(row)
            else:
                und.append(row)
    return {"declared": dec, "undeclared": und, "nDeclared": len(dec), "nUndeclared": len(und),
            "declaredIds": sorted({r.get("declaredId") for r in dec if r.get("declaredId")})}


def score_case(entry, detect, cause_by_carrier, contract, jstatus, chronic=None, baseline=None,
               not_expressible=None, cause_vocab=None):
    expect = list(entry.get("expect_codes") or [])
    symptoms = list(entry.get("expect_symptoms") or [])
    kind = entry.get("match_kind")
    chronic = set(chronic or [])
    not_exp = set(not_expressible or [])
    cvocab = set(cause_vocab or [])

    if kind == "not_applicable":
        return {"verdict": "NOT_APPLICABLE", "reason": entry.get("note", "")}

    detected_true = {c for c, v in detect.items() if v.get("state") == TRUE}
    detected_unk = {c for c, v in detect.items() if v.get("state") == UNK}
    # ★ 差分判定：慢性结论（健康态本就为真，如 FM-DB-UNUSED-INDEX）不算本故障的信号
    incremental = detected_true - chronic

    # 阴性对照：只报「背景结论」与「有无新信号」
    if kind == "baseline":
        return {
            "verdict": "BASELINE_CLEAN" if not incremental else "BASELINE_SIGNAL",
            "match_kind": "baseline",
            "chronic_true": sorted(detected_true),
            "incremental_true": sorted(incremental),
            "detect_unk_count": len(detected_unk),
            "perCarrier": {},
        }

    # ── L0 注入有效性：签名指标是否真的动了（区别于「系统判错」）──
    eff = efficacy(entry, detect, baseline)

    # 期望码的判据是否可判
    exp_unjudgeable = [c for c in expect
                       if jstatus.get(c, {}).get("status") == "unjudgeable"]
    exp_partial = [c for c in expect if jstatus.get(c, {}).get("status") == "partial"]

    # 判据层（绝对 + 增量）
    detect_hit = sorted(set(expect) & detected_true) if expect else \
        sorted(set(symptoms) & detected_true)
    detect_hit_inc = sorted(set(expect) & incremental) if expect else \
        sorted(set(symptoms) & incremental)

    # 根因层
    per_carrier = {}
    for carrier, resp in (cause_by_carrier or {}).items():
        cands = root_candidates(resp)
        codes = [c["code"] for c in cands]
        leaf = [c["code"] for c in cands if c["isLeaf"]]
        top1 = codes[0] if codes else None
        st = cause_stats(cands, expect)
        per_carrier[carrier] = {
            "reached": resp.get("ok", False),
            "candidates": codes[:8],
            "leafCandidates": leaf[:8],
            "top1": top1,
            "top1_in_expect": top1 in expect if expect else None,
            "expect_hit_k": sorted(set(expect) & set(codes)) if expect else [],
            "symptom_hit_k": sorted(set(symptoms) & set(codes)),
            "false_claim": sorted(set(codes) - set(expect) - set(symptoms)) if expect else [],
            "stats": st,
        }

    # 载体一致性
    tops = {c: v["top1"] for c, v in per_carrier.items() if v.get("top1")}
    parity = (len(set(tops.values())) <= 1) if len(tops) >= 2 else None

    # 综合判词
    any_top1 = any(v.get("top1_in_expect") for v in per_carrier.values())
    any_k = any(v.get("expect_hit_k") for v in per_carrier.values())
    any_sym = any(v.get("symptom_hit_k") for v in per_carrier.values())
    unreached = [c for c, v in per_carrier.items() if not v.get("reached")]

    if unreached and len(unreached) == len(per_carrier or {"x": 1}):
        verdict = "BLOCKED"
    elif not expect:
        verdict = "HIT_SYMPTOM_ONLY" if (any_sym or detect_hit) else \
            ("UNJUDGEABLE" if not detected_true else "MISS")
    elif any_top1:
        verdict = "HIT"
    elif any_k or detect_hit:
        verdict = "HIT_PARTIAL"
    elif exp_unjudgeable and not exp_partial:
        verdict = "UNJUDGEABLE"
    elif any_sym:
        verdict = "HIT_SYMPTOM_ONLY"
    elif expect and cvocab and set(expect) <= not_exp and (set(expect) & detected_true):
        # 期望码被判据层检出，但**不在 /cause 词表内** ⇒ 根因接口结构性表达不了
        verdict = "NOT_EXPRESSIBLE"
    elif eff.get("available") and not eff.get("effective"):
        # 可判、却连签名指标都没动 ⇒ 故障没真发生，不能记成 MISS
        verdict = "NO_EFFECT"
    else:
        verdict = "MISS"

    return {
        "verdict": verdict,
        "match_kind": kind,
        "expect_codes": expect,
        "expect_symptoms": symptoms,
        "efficacy": eff,
        "expect_not_expressible": sorted(set(expect) & not_exp),
        "judgeability": {c: jstatus.get(c) for c in expect},
        "expect_unjudgeable": exp_unjudgeable,
        "expect_partial": exp_partial,
        "detect_true": sorted(detected_true),
        "detect_incremental": sorted(incremental),
        "detect_unk_count": len(detected_unk),
        "detect_expected_hit": detect_hit,
        "detect_expected_hit_incremental": detect_hit_inc,
        "perCarrier": per_carrier,
        "carrier_parity_top1": parity,
    }


# ══════════════════════════════════════════════════════════════════════════
# 6. 用例闭环
# ══════════════════════════════════════════════════════════════════════════
BASELINE_ENTRY = {
    "type": "baseline",
    "label": "清洁基线（阴性对照，不注入）",
    "match_kind": "baseline",
    "mechanism": "不注入任何故障；用于取得「健康态背景结论集」chronic，供各用例做差分判定",
    "expect_codes": [],
    "expect_symptoms": [],
    "signature_metrics": [],
}


def _poll(fn, ok, timeout, step=4.0):
    """轮询直到 ok(结果) 为真或超时；返回 (最后结果, 是否达成)。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001
            last = {"error": str(e)[:200]}
        if ok(last):
            return last, True
        time.sleep(step)
    return last, False


def ensure_traffic(console, args):
    """★ cardsim 的容器有「待命」生命周期：跑完一轮 / 被 sim stop 之后容器会退出，
       `POST /api/sim {action:start}` 会返回 400『没有待命容器』。
       必须先 `scale`(建容器) + `spawn`(拉起 JVM) 再广播启动，且**每一步都轮询到就绪**
       （固定 sleep 在负载高的机器上会漏，实测踩过一次 ⇒ 该用例的故障根本没产生）。
       不做这一步，大量 app/db 类故障不会产生（= 假阴性，会被误记为 MISS）。"""
    st = {"steps": []}
    run = console.run_running()
    if run:
        st["runId"] = run.get("raw") or run.get("runId")
        st["detail"] = "已有 RUNNING 轮次 %s，复用" % st["runId"]
        st["reused"] = True
        st["started"] = True          # ★ 复用也算「有流量」，否则会被误判成 BLOCKED
        return st

    def running_containers():
        return [c for c in (console.snapshot().get("containers") or []) if c.get("running")]

    c = running_containers()
    if not c:
        st["steps"].append({"scale": console.scale(1)})
        c, ok = _poll(running_containers, lambda x: bool(x), 45)
        st["steps"].append({"waitingContainer": ok})
    if c and not any(x.get("java_procs") for x in c):
        st["steps"].append({"spawn": console.spawn(c[-1]["name"], args.threads)})
        c, ok = _poll(running_containers, lambda x: any(y.get("java_procs") for y in x), 45)
        st["steps"].append({"waitingJvm": ok})
    st["steps"].append({"sim_start": console.sim_start(args.duration)})
    run, ok = _poll(console.run_running, lambda x: bool(x), 50)
    st["started"] = bool(ok and run)
    st["runId"] = (run or {}).get("raw") or (run or {}).get("runId")
    st["detail"] = ("已起流量 %s" % st["runId"]) if st["started"] else \
        "★ 未能起流量（后续 app/db 类故障不产生 ⇒ 本用例应判 BLOCKED）"
    return st


def run_case(console, contract, fmap, ftype, args, runid, chronic=None, do_inject=True,
             baseline=None, subdir=None):
    entry = fmap.get(ftype)
    if not entry:
        if ftype == "baseline":
            entry = dict(BASELINE_ENTRY)
        else:
            raise SystemExit("未知注入类型：%s" % ftype)
    d = os.path.join(OUTROOT, runid, subdir or ftype)
    os.makedirs(d, exist_ok=True)
    ccf_path = os.path.join(d, "ccf.json")
    rec = {"type": ftype, "runid": runid, "entry": entry,
           "startedAt": datetime.now(timezone.utc).isoformat(),
           "console": CONSOLE, "host": HOST, "chronicUsed": sorted(chronic or [])}
    log("─" * 78)
    log("■ 用例 %s  ·  %s  ·  match_kind=%s" % (ftype, entry.get("label"), entry.get("match_kind")))
    log("  机制：%s" % entry.get("mechanism"))

    try:
        # 0) 清基线
        console.clear_all()
        clean = console.waits_clean()
        rec["cleanBaseline"] = clean
        if not clean:
            rec["error"] = "基线未清空：仍有活动故障 %s" % console.active()
        time.sleep(1.8)  # dashboard 随机注入逻辑用的间隔
        # ★ 排空期：让上一用例的**累计计数器**停止增长后再起本用例的 delta 窗口。
        #   实测教训：cpu_throttle 的 container.cpu_throttled_periods 是累计计数器，
        #   冷却不足会让「限流」在下**一个**用例里仍判 TRUE（跨用例假阳性）。
        if args.drain > 0:
            time.sleep(args.drain)

        # 1) 起流量（无流量则多数 app/db 类故障不产生）
        if not args.no_sim:
            rec["traffic"] = ensure_traffic(console, args)
            log("  流量 → %s" % json.dumps(rec["traffic"].get("detail") or rec["traffic"],
                                          ensure_ascii=False)[:220])
        else:
            rec["traffic"] = {"skipped": True}

        # 2) 注入（基线用例跳过）
        if do_inject and ftype != "baseline":
            inj = console.inject(ftype)
            rec["inject"] = inj
            rec["injectedWallClockUtc"] = datetime.now(timezone.utc).isoformat()
            log("  注入 → %s" % json.dumps(inj, ensure_ascii=False))
        else:
            rec["inject"] = {"skipped": True, "reason": "阴性对照/未注入"}
            log("  （不注入）")
        time.sleep(args.settle)

        # 3) 注入后窗口（建立 delta）+ 冻结
        establish_delta_window(gap=args.gap)
        fr = freeze_with_retry(ccf_path, args)
        rec["freeze"] = {"rc": fr["rc"], "elapsed_s": fr["elapsed_s"], "attempts": fr.get("attempts"),
                         "tail": "\n".join(fr["stdout"].splitlines()[-6:])}
        if fr["rc"] != 0:
            rec["verdict"] = "BLOCKED"
            rec["error"] = "freeze 失败 rc=%s" % fr["rc"]
            log("  ✗ freeze 失败 rc=%s" % fr["rc"])
            log("  " + (fr["stdout"] or "")[-800:])
            return _finish(console, d, rec, args)

        doc = json.load(io.open(ccf_path, encoding="utf-8"))
        view = CCFView(doc)
        rec["snapshotId"] = doc.get("snapshotId")
        rec["ccfSha256"] = sha256_file(ccf_path)
        rec["obsCount"] = len(doc.get("observations") or [])
        log("  冻结 → %s  obs=%d" % (rec["snapshotId"], rec["obsCount"]))

        # 4) 判据层（离线机判，载体无关）
        contract_detect = detect_all(view, contract)
        jstat = judgeability(contract, view.metrics_present())
        with io.open(os.path.join(d, "detect.json"), "w", encoding="utf-8") as f:
            json.dump(contract_detect, f, ensure_ascii=False, indent=1)
        rec["detect"] = {k: {"state": v["state"], "reason": v.get("reason")}
                         for k, v in contract_detect.items()}
        # 供 L0 有效性与阴性对照复算
        rec["detectStates"] = {k: v["state"] for k, v in contract_detect.items()}
        # 供 L0 有效性与阴性对照复算（★ text/enum 类指标的值在 valueText，值合并读取）
        rec["metricValues"] = {o["metricCode"]: (o.get("value") if o.get("value") is not None
                                                else o.get("valueText"))
                               for o in doc.get("observations") or []
                               if o.get("status") == "collected"}
        entry = dict(entry)
        entry["_current_metrics"] = rec["metricValues"]

        # 5) 投影 → 两侧 /cause
        pr = project(ccf_path, d, args.neo4j_password, skip=args.no_project)
        rec["project"] = pr
        if not args.no_project and (pr.get("kg", {}).get("rc") or pr.get("onto", {}).get("rc")):
            rec["verdict"] = "BLOCKED"
            rec["error"] = "投影失败 %s" % pr
            return _finish(console, d, rec, args)

        symptom_codes = sorted(c for c, v in contract_detect.items() if v["state"] == TRUE)
        rec["symptomCodes"] = symptom_codes
        # ★★ C1 前置闸门（硬约束 C1：两侧必须消费**同一 snapshotId**）★★
        # 为什么必须先打一次工具：ONTO 侧在「动态模式」（未 pin CC_GRAPHDB_SNAPSHOT_ID）下
        # 是**懒刷新**——snapshot 只在一次真实工具调用里对齐。实测事故：ONTO 连续 7 个用例
        # 都在用同一个 3.5 小时前的命名图回答，而 KG 每条都是当轮最新 ⇒ 任何「KG vs ONTO」
        # 对比**当场作废**（docs/00 §6.3 明文：「两侧消费的快照 ID 不一致即该次测量作废」）。
        c1 = {"ccf": rec.get("snapshotId"), "carriers": {}, "probes": {}}
        for carrier in (args.carriers or ["KG", "ONTO"]):
            try:
                http_json(TOOLS[carrier] + "/guide", {}, timeout=90)
                c1["probes"][carrier] = "ok"
            except RuntimeError as e:
                c1["probes"][carrier] = "ERR %s" % str(e)[:120]
            c1["carriers"][carrier] = health(carrier).get("snapshotId")
        seen = {v for v in [c1["ccf"]] + list(c1["carriers"].values()) if v}
        c1["consistent"] = (len(seen) == 1) and bool(c1["ccf"])
        c1["distinct"] = sorted(seen)
        rec["c1"] = c1
        if not c1["consistent"]:
            log("  ★★ C1 违反：两侧/CCF 快照不一致 %s ⇒ 本次测量作废（BLOCKED）" % c1["distinct"])
        else:
            log("  C1 OK：两侧与 CCF 同为 %s" % c1["ccf"])
        # ★ /cause 词表**从服务实测发现**（契约图节点数是 23，服务实际只收 14）
        vocabs = {c: cause_vocabulary(c) for c in (args.carriers or ["KG", "ONTO"])}
        rec["causeVocabulary"] = {c: (sorted(v) if v else None) for c, v in vocabs.items()}
        # ★★ 2026-10-14 修正：词表是**每载体各自**的，**不能取并集**后给两侧发同一份。
        #   实测（suite50b）：ONTO 不接受 FM-DB-ROLLBACK-STORM / FM-DB-SEQ-SCAN /
        #   FM-DB-UNUSED-INDEX，而 KG 接受。旧实现取并集 ⇒ ONTO 的 /cause **全部 HTTP 400**
        #   ⇒ ① ONTO 侧数据全废（但我当时没报警，静默继续）；
        #     ② 分歧分类器把 KG 的**真实响应**与 ONTO 的**报错空壳**相比 ⇒
        #        凭空产出 204 行「未声明分歧」，一个纯自造的回归信号。
        #   现改为逐载体过滤；两侧各自的入参与不可表达码分别记录。
        # ★ 可比入参 = **两侧词表的交集** ∩ 检出码（同一输入喂两侧，C1/C3 口径）。
        #   逐载体各发各的虽不再 400，但**两侧入参不同 ⇒ 不可比**；
        #   某一侧收不下的码属于**载体能力差异**，单列记录（见 causeCapsByCarrier），不绕过去。
        _sets = [set(v) for v in vocabs.values() if v]
        _inter = set.intersection(*_sets) if _sets else None
        per_input = {}
        for _c, _v in vocabs.items():
            if _inter is not None:
                per_input[_c] = [x for x in symptom_codes if x in _inter]
            else:
                per_input[_c] = list(symptom_codes)
        rec["causeCapsByCarrier"] = {
            _c: sorted(set(symptom_codes) - set(_v)) if _v else []
            for _c, _v in vocabs.items()}
        _cap_note = {_c: _v for _c, _v in rec["causeCapsByCarrier"].items() if _v}
        if _cap_note:
            log("    ★ 载体输入能力差异（同码未必两侧都收）：%s" %
                "; ".join("%s 拒收 %d 个" % (_c, len(_v)) for _c, _v in _cap_note.items()))
        vset = set(symptom_codes)  # 仅用于「契约 vs 服务」的日志对照
        cause_input = per_input.get((args.carriers or ["KG", "ONTO"])[0], list(symptom_codes))
        rec["causeInput"] = cause_input
        rec["causeInputByCarrier"] = per_input
        rec["notExpressible"] = sorted(set(symptom_codes) - set(vocabs.get("KG") or [])) \
            if vocabs.get("KG") else []
        rec["notExpressibleByCarrier"] = {
            _c: sorted(set(symptom_codes) - set(_v)) if _v else []
            for _c, _v in vocabs.items()}
        log("  判据层检出症状码（%d）：%s" % (len(symptom_codes), ", ".join(symptom_codes) or "（空）"))
        for _c, _v in vocabs.items():
            if _v and sorted(set(_v)) != contract.cause_vocab:
                log("    ★ %s 的 /cause 实测词表 %d 个 ≠ 契约因果图节点 %d 个；差集(契约有、服务无)：%s" %
                    (_c, len(set(_v)), len(contract.cause_vocab),
                     ", ".join(sorted(set(contract.cause_vocab) - set(_v))[:6]) or "—"))
            _ne = rec["notExpressibleByCarrier"].get(_c) or []
            if _ne:
                log("    %s：%d 个检出码不在其词表内（能检出、无法表达进因果链）：%s" %
                    (_c, len(_ne), ", ".join(_ne[:6])))
        if not cause_input:
            rec["causeNote"] = "无任何 TRUE 症状码落在 /cause 词表内 ⇒ 根因层不可测"
        causes = {}
        for carrier in (args.carriers or ["KG", "ONTO"]):
            h = health(carrier)
            ci = per_input.get(carrier, list(symptom_codes))
            causes[carrier] = call_cause(carrier, ci, max_hops=args.max_hops)
            causes[carrier]["causeInput"] = ci
            causes[carrier]["snapshotId"] = h.get("snapshotId")
            # ★★ fail-loud：某一侧 /cause 失败必须**显式记录并报警**，
            #   绝不能与「空候选列表」静默比较（那正是本次自造 204 行假分歧的机制）。
            if not causes[carrier].get("ok"):
                rec.setdefault("causeFailures", {})[carrier] = \
                    str(causes[carrier].get("error"))[:400]
                log("    ★★ %s 的 /cause **失败**（入参 %d 个码）：%s" %
                    (carrier, len(ci), str(causes[carrier].get("error"))[:160]))
            with io.open(os.path.join(d, "cause_%s.json" % carrier), "w", encoding="utf-8") as f:
                json.dump(causes[carrier], f, ensure_ascii=False, indent=1)
        rec["cause"] = {c: {"ok": v.get("ok"), "snapshotId": v.get("snapshotId"),
                            "top1": (root_candidates(v) or [{}])[0].get("code")}
                        for c, v in causes.items()}
        # ★ 载体一致性 + 已声明差异核对（把「已知载体差异」与「新缺陷」分开）
        _both_ok = all((causes.get(_c) or {}).get("ok")
                       for _c in (args.carriers or ["KG", "ONTO"]))
        if not _both_ok:
            rec["divergences"] = {"declared": [], "undeclared": [], "nDeclared": 0,
                                  "nUndeclared": 0, "declaredIds": [],
                                  "skipped": "某一侧 /cause 未成功 ⇒ 不做载体比对（避免比空壳）"}
            log("    ★ 载体分歧比对**跳过**：某一侧 /cause 未成功")
            div = rec["divergences"]
        else:
            div = classify_divergences(causes.get("KG"), causes.get("ONTO"),
                                       load_declared_divergences(getattr(args, "divergences", None)))
        rec["divergences"] = div
        if div["nDeclared"] or div["nUndeclared"]:
            log("    载体字段差异：已声明 %d 处%s，**未声明 %d 处**%s"
                % (div["nDeclared"],
                   ("（%s）" % ",".join(div["declaredIds"])) if div["declaredIds"] else "",
                   div["nUndeclared"],
                   ("  ← " + "; ".join("%s.%s %s≠%s" % (r["code"], r["field"], r["kg"], r["onto"])
                                       for r in div["undeclared"][:4])) if div["nUndeclared"] else ""))

        # 6) 评分（含与阴性对照的差分 + L0 有效性）
        sc = score_case(entry, contract_detect, causes, contract, jstat,
                        chronic=chronic, baseline=baseline,
                        not_expressible=rec.get("notExpressible"),
                        cause_vocab=vset)
        rec["score"] = sc
        log("  ▸ 判词 %s" % sc["verdict"])
        eff = sc.get("efficacy") or {}
        if eff.get("available"):
            log("    L0 注入有效性 = %s" % ("有效" if eff.get("effective") else
                                          "★ 无变化（故障未产生）"))
            for row in eff.get("signature") or []:
                log("       %-42s 基线=%-12s 注入=%-12s %s" %
                    (row["metric"], row.get("baseline"), row.get("fault"),
                     "变动" if row.get("changed") else "未变"))
        else:
            log("    L0 注入有效性 = 不可判（%s）" % eff.get("reason"))
        if chronic is not None:
            log("    背景(chronic)=%s" % (", ".join(sorted(chronic)) or "（空）"))
            log("    增量(incremental)=%s" % (", ".join(sc.get("detect_incremental") or []) or "（无）"))
        for carrier, v in sc.get("perCarrier", {}).items():
            st = v.get("stats") or {}
            log("    %-5s top1=%-28s 期望命中=%s  候选=%d(有判定 %d / 有命中条款 %d)" %
                (carrier, v.get("top1"), v.get("expect_hit_k"),
                 st.get("nCandidates", 0), st.get("nDetermined", 0),
                 st.get("nWithSatisfiedClauses", 0)))
        log("    载体一致性(top1) = %s" % sc.get("carrier_parity_top1"))
        # ★ 没流量 ⇒ app/db 类故障不会产生 ⇒ 该用例「没测到」，不得记 MISS/NO_EFFECT
        if not args.no_sim and not (rec.get("traffic") or {}).get("started"):
            rec["verdictOverride"] = "无流量：本用例未测到（BLOCKED ≠ 未命中）"
            sc["verdict_original"] = sc["verdict"]
            sc["verdict"] = "BLOCKED"
            sc["blockedReason"] = "起流量失败：%s" % (rec.get("traffic") or {})
            log("    ★ 覆盖判词 → BLOCKED（起流量失败）")
        # ★ C1 违反 ⇒ 载体对比无效，判词必须作废
        if not args.no_project and not (rec.get("c1") or {}).get("consistent", True):
            sc.setdefault("verdict_original", sc["verdict"])
            sc["verdict"] = "BLOCKED"
            sc["blockedReason"] = "违反 C1（两侧/CCF 快照不一致）：%s" % (rec.get("c1") or {}).get("distinct")
            log("    ★ 覆盖判词 → BLOCKED（C1 违反）")

        rec["verdict"] = sc["verdict"]
        with io.open(os.path.join(d, "detect_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"judgeability": jstat}, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        rec["verdict"] = "BLOCKED"
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        log("  ✗ 异常 %s" % rec["error"])
    return _finish(console, d, rec, args)


def _finish(console, d, rec, args):
    # 清故障（无论成败）—— 保证不污染后续用例与共享环境
    try:
        cl = console.clear_all()
        rec["clearAfter"] = cl
        rec["clearedOk"] = console.waits_clean()
    except Exception as e:  # noqa: BLE001
        rec["clearAfter"] = {"error": str(e)}
        rec["clearedOk"] = False
    # 停仿真（默认保留：cardsim 跑完一轮容器就不再待命，重建容器代价高；
    # 仿真时长由 --duration 自行封顶）
    if getattr(args, "stop_sim", False):
        try:
            rec["simStop"] = console.sim_stop()
        except Exception as e:  # noqa: BLE001
            rec["simStop"] = {"error": str(e)}
    rec["finishedAt"] = datetime.now(timezone.utc).isoformat()
    with io.open(os.path.join(d, "case.json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    log("  ← 收尾清故障 ok=%s  产物 %s" % (rec.get("clearedOk"), d))
    return rec


# ══════════════════════════════════════════════════════════════════════════
# 7. 子命令
# ══════════════════════════════════════════════════════════════════════════
def cmd_status(args):
    c = Console()
    s = c.snapshot()
    log("注入台 %s" % CONSOLE)
    log("  活动故障：%s" % (json.dumps(c.active(), ensure_ascii=False) or "[]"))
    log("  容器：%s" % ", ".join("%s(%s, cpu=%s%%)" % (x["name"], x["status"], x.get("cpu_pct"))
                                 for x in s.get("containers") or []))
    res = s.get("results") or []
    log("  最近一轮：%s" % (json.dumps(res[0], ensure_ascii=False) if res else "无"))
    for carrier in ("KG", "ONTO"):
        h = health(carrier)
        log("  %-5s %s  snapshotId=%s" % (carrier, TOOLS[carrier], h.get("snapshotId")))
    try:
        ch = http_json(COLLECT + "/health", timeout=20)
        log("  采集层 %s  version=%s collect_source=%s" %
            (COLLECT, ch.get("version"), (ch.get("collect_source") or {}).get("collect_source")))
    except RuntimeError as e:
        log("  采集层不可达：%s" % e)
    if args.json:
        print(json.dumps({"snapshot": s, "faults": c.active()}, ensure_ascii=False))


def cmd_probe(args):
    """离线：判据可判性 + 每类注入的可测性。"""
    contract, fmap = Contract(), FaultMap()
    ccf = args.ccf
    if ccf and os.path.exists(ccf):
        view = CCFView(json.load(io.open(ccf, encoding="utf-8")))
        present = view.metrics_present()
        src = ccf
    else:
        # 无 CCF 时退化为「契约判据指标 × 上次已知观测清单」
        present = set(load_yaml(os.path.join(HERE, "observed-metrics.yaml")).get("metrics") or [])
        src = "fi-e2e/observed-metrics.yaml"
    j = judgeability(contract, present)
    log("判据可判性核验   观测清单来源：%s   观测指标 %d 个" % (src, len(present)))
    log("")
    tally = {}
    for code in contract.all_codes:
        st = j[code]["status"]
        tally[st] = tally.get(st, 0) + 1
        mark = {"judgeable": "OK  ", "partial": "PART", "unjudgeable": "UNK ", "n/a": "n/a "}[st]
        miss = ("  缺: " + ", ".join(j[code]["missing"])) if j[code]["missing"] else ""
        log("  %s %-28s %s%s" % (mark, code, j[code].get("layer") or "", miss))
    log("")
    log("模式可判性统计：%s" % tally)
    log("")
    log("按注入类型（expect_codes 的可判性 ⇒ 本批能否测出根因）：")
    rows = []
    for t, e in fmap.entries.items():
        exp = e.get("expect_codes") or []
        if e["match_kind"] == "not_applicable":
            testable, why = "NOT_APPLICABLE", "本环境 no-op"
        elif not exp:
            testable = "SYMPTOM_ONLY"
            why = "契约无根因码；症状码 %s" % ",".join(e.get("expect_symptoms") or []) or "—"
        else:
            sts = [j.get(c, {}).get("status") for c in exp]
            if all(s == "judgeable" for s in sts):
                testable, why = "TESTABLE", "全部判据齐备"
            elif any(s == "judgeable" for s in sts):
                testable, why = "PARTIAL", "部分判据缺失"
            else:
                testable, why = "UNJUDGEABLE", "判据指标无观测(探针不伪造0)"
        rows.append((t, e["match_kind"], testable, ",".join(exp) or "-", why))
    for t, k, testable, exp, why in sorted(rows, key=lambda r: (r[2], r[0])):
        log("  %-14s %-14s %-22s %-34s %s" % (testable, k, t, exp, why))
    log("")
    tc = {}
    for r in rows:
        tc[r[2]] = tc.get(r[2], 0) + 1
    log("注入类型可测性统计：%s" % tc)
    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as f:
            json.dump({"judgeability": j, "injectionTestability": rows}, f,
                      ensure_ascii=False, indent=1)
        log("已写 %s" % args.out)


def cmd_detect(args):
    contract = Contract()
    doc = json.load(io.open(args.ccf, encoding="utf-8"))
    view = CCFView(doc)
    res = detect_all(view, contract)
    j = judgeability(contract, view.metrics_present())
    log("CCF %s  obs=%d" % (doc.get("snapshotId"), len(doc.get("observations") or [])))
    for code, v in sorted(res.items(), key=lambda kv: kv[1]["state"]):
        if v["state"] == FALSE:
            continue
        log("  %-5s %-28s %s" % (v["state"], code, v.get("reason") or ""))
    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as f:
            json.dump({"detect": res, "judgeability": j}, f, ensure_ascii=False, indent=1)
        log("已写 %s" % args.json)


def _sparql_update(update):
    data = urllib.parse.urlencode({"update": update}).encode()
    req = urllib.request.Request("%s/repositories/%s" % (GRAPHDB_URL, GRAPHDB_REPO), data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status


def _drop_graph(graph_iri):
    """删命名图。**必须走 `/statements`**（SPARQL 1.1 Update 端点）。

    实测踩坑记录：`POST /repositories/{repo}` 是**查询**端点，拿它发 `update=` 会返回
    `400 Missing parameter: query`；GraphDB 的 REST `DELETE /repositories/{repo}/rdf-graphs/{graph}`
    同样 400。可用路径 = `POST /repositories/{repo}/statements` + `update=DROP GRAPH <...>` → 204。
    """
    url = "%s/repositories/%s/statements" % (GRAPHDB_URL, GRAPHDB_REPO)
    data = urllib.parse.urlencode({"update": "DROP GRAPH <%s>" % graph_iri}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace")[:200])


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def cmd_progress(args):
    """批次实时进度（只读磁盘产物，不依赖后台 job 的 stdout）。

    ★ 为什么要有它：批次要跑几十分钟到几小时，若只能靠 job 的 stdout 看进度，
    会话一断/一换窗口就瞎了。落到磁盘的 `case.json` 才是**权威且可复核**的进度源 ——
    它同时携带判词、C1 结果、症状码、候选统计与载体差异，足以判断「跑到哪、跑得对不对」。
    """
    import time as _t
    while True:
        d = os.path.join(OUTROOT, args.runid)
        if not os.path.isdir(d):
            subs = sorted(x for x in os.listdir(OUTROOT)) if os.path.isdir(OUTROOT) else []
            raise SystemExit("无此批次：%s\n现有批次：%s" % (d, ", ".join(subs) or "（无）"))
        planp = os.path.join(d, "suite-plan.json")
        plan = json.load(io.open(planp, encoding="utf-8")) if os.path.exists(planp) else {}
        total = plan.get("n")
        names = sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
        done, inflight, tally, durs, und_rows, und_keys = [], [], {}, [], 0, set()
        for name in names:
            cj = os.path.join(d, name, "case.json")
            if not os.path.exists(cj):
                inflight.append(name)
                continue
            try:
                r = json.load(io.open(cj, encoding="utf-8"))
            except Exception:
                inflight.append(name)
                continue
            tally[r.get("verdict") or "BLOCKED"] = tally.get(r.get("verdict") or "BLOCKED", 0) + 1
            t0, t1 = _parse_iso(r.get("startedAt")), _parse_iso(r.get("finishedAt"))
            if t0 and t1:
                durs.append((t1 - t0).total_seconds())
            dv = r.get("divergences") or {}
            und_rows += dv.get("nUndeclared", 0)
            for row in dv.get("undeclared") or []:
                und_keys.add("%s.%s" % (row["code"], row["field"]))
            done.append((name, r))

        log("═" * 78)
        log("批次 %s · 进度 **%d/%s**%s · 目录 %d 个"
            % (args.runid, len(done), total or "?",
               (" （%.0f%%）" % (100.0 * len(done) / total)) if total else "", len(names)))
        if durs:
            avg = sum(durs) / len(durs)
            left = max(0, (total or len(done)) - len(done))
            log("单例均耗时 %.0f s ｜ 已用 %.1f min ｜ 预计剩余 %.0f min"
                % (avg, sum(durs) / 60.0, avg * left / 60.0))
        log("判词：" + ("  ".join("%s=%d" % kv for kv in sorted(tally.items(), key=lambda x: -x[1]))
                       or "（尚无）"))
        log("载体差异：未声明累计 **%d 行**%s"
            % (und_rows, ("   " + ", ".join(sorted(und_keys)[:6])) if und_keys else "  ✔ 无"))
        log("─" * 78)
        for name, r in done[-args.tail:]:
            dv = r.get("divergences") or {}
            c1 = (r.get("c1") or {}).get("consistent")
            cands = (((r.get("score") or {}).get("perCarrier") or {}).get("KG") or {}).get("stats") or {}
            log("  %-24s %-14s C1=%-2s 症状%-2d 候选%-3s 未声明%-2d"
                % (name[:24], r.get("verdict"),
                   {True: "✔", False: "✘", None: "—"}[c1],
                   len(r.get("symptomCodes") or []),
                   cands.get("nCandidates", "—"), dv.get("nUndeclared", 0)))
        if inflight:
            log("  %-24s ← 进行中（目录已建、尚无 case.json）" % inflight[-1][:24])
        if not args.watch or (total and len(done) >= total):
            return
        _t.sleep(args.watch)
        log("")


def cmd_prune(args):
    """N5：清理 GraphDB 里的**陈旧快照命名图**。

    为什么要清：本项目每投影一次就新建一个 `.../graph/<snapshotId>` 命名图，旧图不自动删除；
    而 GraphDB 的**默认图是所有命名图的并集** ⇒ 实测堆了 10 个历史快照（43k 三元组 vs 单快照 4k）。
    只要服务端显式限定 scope 就不会读错，但堆积会让「误读历史数据」的风险常存，
    也让存储与排障变脏。

    ★ 安全默认：**dry-run**（只列不删）；必须显式 `--yes` 才执行；`--keep N` 保留最近 N 个
      （按 snapshotId 字典序 = 时间序）；**永不触碰 `tbox` 图**。
    """
    graphs = graphdb_graphs()
    snaps = sorted([g for g in graphs if "/graph/ccf-" in g["graph"]], key=lambda g: g["graph"])
    keep = snaps[-args.keep:] if args.keep > 0 else []
    drop = [g for g in snaps if g not in keep]
    log("GraphDB 仓库 %s：命名图 %d 个（其中快照图 %d）" % (GRAPHDB_REPO, len(graphs), len(snaps)))
    log("保留最近 %d 个：" % args.keep)
    for g in keep:
        log("   KEEP  %-70s %d" % (g["graph"][-68:], g["triples"]))
    log("待删 %d 个：" % len(drop))
    for g in drop:
        log("   DROP  %-70s %d" % (g["graph"][-68:], g["triples"]))
    if not drop:
        log("无需清理。")
        return
    if not args.yes:
        log("")
        log("★ DRY-RUN：未删除任何图。确认后加 --yes 执行。")
        return
    for g in drop:
        st, err = _drop_graph(g["graph"])
        log("   DROP HTTP %-4s %s%s" % (st, g["graph"][-60:], ("  ERR " + err) if err else ""))
    left = graphdb_graphs()
    log("清理后：命名图 %d 个 / 三元组 %d" % (len(left), sum(x["triples"] for x in left)))


def efficacy(entry, detect, baseline):
    """L0 注入有效性：签名指标 / 期望码判据在「基线 → 注入后」是否真的动了。

    这一层的存在只为回答一个问题：**故障到底发生了没有**。
    没有它，「系统判错(MISS)」与「故障压根没产生(NO_EFFECT)」会被混为一谈。
    """
    if not baseline:
        return {"available": False, "reason": "未提供阴性对照基线（--chronic-from/--with-baseline）"}
    bm = baseline.get("metrics") or {}
    bdet = baseline.get("detect") or {}
    cur = entry.get("_current_metrics") or {}
    sig, changed, ignored = [], False, []
    for m in entry.get("signature_metrics") or []:
        row = {"metric": m, "baseline": bm.get(m), "fault": cur.get(m)}
        row["changed"] = (row["baseline"] != row["fault"]) if (
            row["baseline"] is not None or row["fault"] is not None) else False
        if m in BACKGROUND_COUNTERS:
            row["backgroundCounter"] = True
            if row["changed"]:
                ignored.append(m)
        elif row["changed"]:
            changed = True
        sig.append(row)
    code_moves = {}
    for c in (entry.get("expect_codes") or []):
        b = bdet.get(c)
        f = (detect.get(c) or {}).get("state")
        code_moves[c] = {"baseline": b, "fault": f, "moved": b != f}
        if b != f:
            changed = True
    return {"available": True, "effective": bool(changed),
            "signature": sig, "expectCodeMoves": code_moves,
            "backgroundChangesIgnored": ignored,
            "baselineSnapshotId": baseline.get("snapshotId")}


def _load_baseline(path):
    """从阴性对照的 case.json 载入 {chronic, metrics, detect, snapshotId}。"""
    if not path:
        return None
    if not os.path.exists(path):
        raise SystemExit("基线文件不存在：%s" % path)
    doc = json.load(io.open(path, encoding="utf-8"))
    if "score" in doc and "chronic_true" not in doc:
        out = {"chronic": set((doc.get("score") or {}).get("chronic_true") or [])}
    else:
        out = {"chronic": set(doc.get("chronic_true") or [])}
    out["metrics"] = doc.get("metricValues") or {}
    out["detect"] = doc.get("detectStates") or {}
    out["snapshotId"] = doc.get("snapshotId")
    if not out["metrics"]:
        ccf = os.path.join(os.path.dirname(path), "ccf.json")
        if os.path.exists(ccf):
            d = json.load(io.open(ccf, encoding="utf-8"))
            out["metrics"] = {o["metricCode"]: (o.get("value") if o.get("value") is not None
                                                       else o.get("valueText"))
                              for o in d.get("observations") or []
                              if o.get("status") == "collected"}
            out["snapshotId"] = out["snapshotId"] or d.get("snapshotId")
    return out


def cmd_case(args):
    contract, fmap, console = Contract(), FaultMap(), Console()
    runid = args.runid or datetime.now().strftime("%Y%m%d-%H%M%S")
    base = _load_baseline(args.chronic_from)
    if args.type == "baseline":
        rec = run_case(console, contract, fmap, "baseline", args, runid, do_inject=False)
        log("")
        log("背景结论集 chronic = %s" % ", ".join((rec.get("score") or {}).get("chronic_true") or []))
        log("判词：%s" % rec.get("verdict"))
        return
    rec = run_case(console, contract, fmap, args.type, args, runid,
                   chronic=(base or {}).get("chronic"), baseline=base)
    log("")
    log("判词：%s" % rec.get("verdict"))


def expand_suite_list(path):
    """把 suite-50.yaml 展开成有序的用例类型列表（`runs` 即重复次数）。

    ★ 为什么用文件而不是命令行长串：**分母要可复算**。批次报告里的 n 必须能
    由这份清单逐条重放；命令行长串抄错一位就再也对不回去。
    """
    d = load_yaml(path)
    out, detail = [], []
    for e in d.get("types") or []:
        t = e.get("type")
        runs = int(e.get("runs") or 1)
        for _ in range(runs):
            out.append(t)
        detail.append({"type": t, "group": e.get("group"), "runs": runs,
                       "expect": e.get("expect"), "symptom": e.get("symptom"),
                       "judgeable": e.get("judgeable"), "injection": e.get("injection")})
    return out, detail, d


def cmd_suite(args):
    contract, fmap, console = Contract(), FaultMap(), Console()
    detail = None
    if args.list:
        types, detail, spec = expand_suite_list(args.list)
        log("用例清单：%s（声明分母 %s，展开 %d 条）"
            % (args.list, (spec.get("meta") or {}).get("denominator"), len(types)))
    else:
        types = [t.strip() for t in (args.types or "").split(",") if t.strip()]
    unknown = [t for t in types if not fmap.get(t) and t != "baseline"]
    if unknown:
        raise SystemExit("未知注入类型：%s" % sorted(set(unknown)))
    runid = args.runid or datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(OUTROOT, runid), exist_ok=True)
    log("═══ 匹配性测试批次 %s ═══  **固定分母 %d** 条，单例仿真 %ds / 静置 %ds"
        % (runid, len(types), args.duration, args.settle))
    log("注入台 %s ｜ 采集层 %s ｜ 载体 %s" % (CONSOLE, COLLECT, TOOLS))
    log("契约码 %d ｜ 契约因果图节点 %d（/cause 实际词表以服务实测为准）" %
        (len(contract.all_codes), len(contract.cause_vocab)))
    if detail:
        with io.open(os.path.join(OUTROOT, runid, "suite-plan.json"), "w", encoding="utf-8") as f:
            json.dump({"list": args.list, "expanded": detail, "n": len(types),
                       "orchestration": {"duration": args.duration, "settle": args.settle,
                                         "gap": args.gap, "drain": args.drain,
                                         "carriers": args.carriers, "maxHops": args.max_hops}},
                      f, ensure_ascii=False, indent=1)

    baseline = _load_baseline(args.chronic_from)
    recs = []
    if args.with_baseline or not baseline:
        log("")
        log("### [0] 阴性对照 baseline（取健康态背景结论集与签名指标基线值）")
        brec = run_case(console, contract, fmap, "baseline", args, runid, do_inject=False)
        recs.append(brec)
        baseline = {"chronic": set((brec.get("score") or {}).get("chronic_true") or []),
                    "metrics": brec.get("metricValues") or {},
                    "detect": brec.get("detectStates") or {},
                    "snapshotId": brec.get("snapshotId")}
        log("  chronic = %s" % (", ".join(sorted(baseline["chronic"])) or "（空）"))
    for i, t in enumerate(types, 1):
        log("")
        log("### [%d/%d] %s" % (i, len(types), t))
        # ★ 同一类型重复出现时目录不能互相覆盖
        sub = ("%02d-%s" % (i, t)) if types.count(t) > 1 else t
        recs.append(run_case(console, contract, fmap, t, args, runid,
                             chronic=(baseline or {}).get("chronic"), baseline=baseline,
                             subdir=sub))
        if i < len(types):
            time.sleep(args.cooldown)
    with io.open(os.path.join(OUTROOT, runid, "results.jsonl"), "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = summarize(recs)
    with io.open(os.path.join(OUTROOT, runid, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    log("")
    log("═══ 批次结果 ═══")
    log(json.dumps(summary, ensure_ascii=False, indent=1))
    log("产物目录：%s" % os.path.join(OUTROOT, runid))


def summarize(recs):
    def bucket(v):
        return v or "BLOCKED"

    by = {}
    for r in recs:
        by.setdefault(bucket(r.get("verdict")), []).append(r["type"])
    pairs = []
    for r in recs:
        if r["type"] == "baseline":
            continue
        sc = r.get("score") or {}
        pc = sc.get("perCarrier") or {}
        eff = sc.get("efficacy") or {}
        pairs.append({
            "type": r["type"],
            "verdict": r.get("verdict"),
            "match_kind": (r.get("entry") or {}).get("match_kind"),
            "expect": sc.get("expect_codes") or (r.get("entry") or {}).get("expect_codes"),
            "injection_effective": eff.get("effective"),
            "symptoms_detected": r.get("symptomCodes"),
            "symptoms_incremental": sc.get("detect_incremental"),
            "not_expressible": r.get("notExpressible"),
            "kg_top1": (pc.get("KG") or {}).get("top1"),
            "onto_top1": (pc.get("ONTO") or {}).get("top1"),
            "kg_stats": (pc.get("KG") or {}).get("stats"),
            "onto_stats": (pc.get("ONTO") or {}).get("stats"),
            "parity": sc.get("carrier_parity_top1"),
        })
    scored = [r for r in recs if r["type"] != "baseline"]
    n = len(scored)
    hit = sum(1 for r in scored if r.get("verdict") in ("HIT", "HIT_PARTIAL"))
    unj = sum(1 for r in scored if r.get("verdict") == "UNJUDGEABLE")
    blk = sum(1 for r in scored if r.get("verdict") == "BLOCKED")
    noe = sum(1 for r in scored if r.get("verdict") == "NO_EFFECT")
    pars = [p["parity"] for p in pairs if p["parity"] is not None]
    base = next((r for r in recs if r["type"] == "baseline"), None)
    und_codes, und_rows = set(), 0
    for r in scored:
        dv = r.get("divergences") or {}
        und_rows += dv.get("nUndeclared", 0)
        for row in dv.get("undeclared") or []:
            und_codes.add("%s.%s" % (row["code"], row["field"]))
    return {
        "n": n,
        "verdicts": {k: {"n": len(v), "types": v} for k, v in by.items()},
        "root_cause_hit_rate": (hit / n) if n else None,
        "hit_denominator_note": "分母=全部用例（含 UNJUDGEABLE/BLOCKED/NO_EFFECT），与 M5 判决集口径一致",
        "carrier_parity_rate": (sum(1 for p in pars if p) / len(pars)) if pars else None,
        "counts": {"hit": hit, "unjudgeable": unj, "blocked": blk, "no_effect": noe},
        "chronic_background": sorted(((base or {}).get("score") or {}).get("chronic_true") or []),
        # ★ 未声明的载体分歧 = 回归信号（0 才算干净）
        "undeclaredCarrierDivergences": {"rows": und_rows, "keys": sorted(und_codes)},
        "cases": pairs,
    }


def build_parser():
    p = argparse.ArgumentParser(description="端到端故障注入 — 故障/根因匹配性测试器")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="只读：注入台与 RCA 栈状态")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("probe", help="离线：判据可判性 + 注入可测性")
    s.add_argument("--ccf", default=None)
    s.add_argument("--out", default=None)
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("detect", help="离线：在 CCF 上跑契约判据")
    s.add_argument("--ccf", required=True)
    s.add_argument("--json", default=None)
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("prune", help="N5：清理 GraphDB 陈旧快照命名图（默认 dry-run）")
    s.add_argument("--keep", type=int, default=2, help="保留最近 N 个快照图（按 snapshotId 字典序）")
    s.add_argument("--yes", action="store_true", help="确认执行删除（不加则只列不删）")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("progress", help="批次实时进度（只读磁盘产物；可 --watch 持续刷新）")
    s.add_argument("--runid", required=True)
    s.add_argument("--tail", type=int, default=12, help="显示最近 N 条用例")
    s.add_argument("--watch", type=int, default=0, help="每 N 秒刷新一次（0=只看一次）")
    s.set_defaults(func=cmd_progress)

    for name, fn in (("case", cmd_case), ("suite", cmd_suite)):
        s = sub.add_parser(name, help="单用例全闭环" if name == "case" else "多用例批次")
        g = s.add_argument_group("用例")
        g.add_argument("--types", required=False,
                       help="逗号分隔的注入类型")
        g.add_argument("--list", default=None, help="用例清单 YAML（如 fi-e2e/suite-50.yaml；与 --types 二选一）")
        if name == "case":
            g.add_argument("--type", required=True)
        g = s.add_argument_group("编排")
        g.add_argument("--duration", type=int, default=300, help="仿真时长(s)")
        g.add_argument("--settle", type=float, default=45.0, help="注入后静置(s)")
        g.add_argument("--sim-warmup", type=float, default=10.0, help="起流量后预热(s)")
        g.add_argument("--gap", type=float, default=12.0, help="delta 窗口(s)")
        g.add_argument("--cooldown", type=float, default=20.0, help="用例间隔(s)")
        g.add_argument("--drain", type=float, default=20.0,
                       help="清故障后的排空期(s)：让上一用例的累计计数器停止增长（防跨用例假阳性）")
        g.add_argument("--max-hops", type=int, default=4)
        g.add_argument("--carriers", default="KG,ONTO")
        g.add_argument("--runid", default=None)
        g.add_argument("--with-baseline", action="store_true",
                       help="批次先跑阴性对照基线，供差分判定（suite）")
        g.add_argument("--chronic-from", default=None,
                       help="从已有 case.json 读背景结论集（case/suite）")
        g.add_argument("--no-sim", action="store_true", help="不起仿真流量")
        g.add_argument("--threads", type=int, default=8, help="spawn JVM 线程数")
        g.add_argument("--no-project", action="store_true", help="跳过投影（只测判据层）")
        g.add_argument("--keep-sim", action="store_true", help="（默认即保留）兼容开关")
        g.add_argument("--stop-sim", action="store_true", help="用例结束后停仿真")
        g.add_argument("--neo4j-password", default=None)
        s.set_defaults(func=fn)
    return p


def main():
    args = build_parser().parse_args()
    if getattr(args, "carriers", None):
        args.carriers = [c.strip() for c in args.carriers.split(",") if c.strip()]
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
