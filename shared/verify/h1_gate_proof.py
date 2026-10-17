# -*- coding: utf-8 -*-
"""M1.7 门禁验证：证明 H1 类静默失败**会被拒绝写出**，且门禁具有判别力（不是一律拒绝）。

背景（docs/02-统一事实层与采集层设计.md §7 H1，★ 本项目的第 1 号工程风险）：
    探针在**模块导入时**把 env 求值成连接常量并冻结
    （service/probes/gaussdb_probe.py:496-497 → :1464 register_probe(GaussdbProbe())），
    而 collect_api 在自己的导入期重新加载 .env 并另算一份 _conn_kwargs
    （service/collect_api.py:51-52,54,73）。两者分叉 ⇒ 探针连 127.0.0.1:8000 失败
    ⇒ 异常被吞成 ProbeResult(error=...)（service/probes/__init__.py:189-195）
    ⇒ **该层 0 事实而 /health.gaussdb.ok=true**。

    查证要点（2026-10-05，subagent 调查 + Captain 亲测复现，见 tmp/H1-调查-采集层事故机制.md）：
      * /health **没有顶层 ok 字段**（顶层键 = service/version/ts/collect_source/gaussdb/collect_cache/rules）；
        事故里的「ok=true」是 info["gaussdb"]["ok"]；
      * health.gaussdb.ok=true 只证明 collect_api 自己那份 _conn_kwargs 能连库 + SELECT version() 成功，
        **不读探针注册表、不读任何层事实数** ⇒ 对「某层 0 事实」完全不可见。
    ⇒ 因此本门禁**不得**以 /health 为事实数判据，必须以
      (a) gather 的 layer_details[层].fact_count > 0、(b) layer_errors/section_errors 为空 为准。

本脚本用途：把上述形态**离线**喂给 shared/collect/freeze.py，断言其 fail-closed。
    不连任何网络、不 import 采集层、不碰真机；只用冻结器的 --health-json / --gather-json 离线入口。

用法：
    python shared/verify/h1_gate_proof.py
退出码：0 = 全部预期成立；1 = 有反例（门禁失效）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FREEZE = os.path.join(ROOT, "shared", "collect", "freeze.py")
SCHEMA = os.path.join(ROOT, "shared", "model", "ccf.schema.json")
TOPO = os.path.join(ROOT, "shared", "model", "topology.yaml")
PY = sys.executable

# ---------------------------------------------------------------- 离线夹具
# 夹具映射表**只服务本门禁测试**：门禁是被测对象，映射正确性由 M1.1 的正式映射表负责。
FIXTURE_MAP = """\
meta:
  version: "0.0.1-h1-gate-fixture"
  owner_doc: shared/verify/h1_gate_proof.py
objtype_to_type:
  node: {type: GaussDBInstance, provenance: gaussdb}
path_to_metric:
  host.cpu_usage: {metric: host.cpu_usage_ratio, unit: ratio, kind: number, raw_unit: percent}
expected_profiles:
  h1-gate-test:
    types: [GaussDBInstance]
    metrics: [host.cpu_usage_ratio]
    objectsMin: {GaussDBInstance: 1}
"""

HEALTH_H1 = {
    "service": "gdo-collect", "version": "6.2", "ts": "2026-10-05T00:00:00",
    "collect_source": {"collect_source": "live", "simulated": False, "scenario": None},
    "gaussdb": {"ok": True, "version": "GaussDB Kernel 507.0", "now": "2026-10-05T00:00:00"},
    "collect_cache": {"cached": False, "age_s": 0.0},
    "rules": {},
}

H1_ERR = ('connection to server at "127.0.0.1", port 8000 failed: Connection refused'
          ' (H1: 探针冻结的 host=None)')

TOPO_BLOCK = {
    "cluster_id": "cl-fixture-1", "cluster_name": "cc-fixture",
    "primary_ip": "192.0.2.12", "version": "GaussDB Kernel 507.0",
    "nodes": [{"node_id": "dn_6001", "node_type": "DN", "node_role": "Primary",
               "node_status": "Normal", "host_ip": "192.0.2.21", "host_id": "host-1",
               "sync_state": None}],
}
IP_INDEX = {"192.0.2.12": {"cluster_id": "cl-fixture-1", "cluster_name": "cc-fixture",
                             "node_id": "dn_6001", "node_role": "Primary",
                             "node_status": "Normal", "is_primary": True}}
GOOD_FACT = {"path": "host.cpu_usage", "value": 0.42, "obj_id": "dn_6001",
             "obj_type": "node", "source": "os.ssh", "ts": "2026-10-05T00:00:00"}


def gather(total_facts, details, facts, layer_errors=None, section_errors=None, mode="probes_v6"):
    d = {"mode": mode, "collected_at": "2026-10-05T00:00:00", "elapsed_ms": 1234,
         "layer_details": details, "total_facts": total_facts,
         "all_facts": facts, "all_alerts": [], "topology": TOPO_BLOCK, "ip_index": IP_INDEX,
         "slow_sql_detail": [], "fix_proposals": [], "sql_verify": {"enabled": False, "result": None}}
    if layer_errors is not None:
        d["layer_errors"] = layer_errors
    if section_errors is not None:
        d["section_errors"] = section_errors
    return d


def case_a_h1():
    """★ H1 本尊：/health.gaussdb.ok=true 与「gaussdb 层 0 事实」同时成立。"""
    return ("A H1 本尊（health ok=true + total_facts=0）",
            HEALTH_H1,
            gather(0,
                   {"gaussdb": {"mode": "normal", "fact_count": 0, "alert_count": 0,
                                "elapsed_ms": 0, "sections_failed": []},
                    "os": {"mode": "normal", "fact_count": 0, "alert_count": 0,
                           "elapsed_ms": 0, "sections_failed": []}},
                   [], layer_errors={"gaussdb": H1_ERR}),
            False, "total_facts=0")


def case_b_layer_only():
    """H1 变体：整体不空（os 层有事实），但 gaussdb 层整层为 0 ⇒ 按层门禁必须抓住。"""
    return ("B 单层空（os 有 12 条 / gaussdb 0 条）",
            HEALTH_H1,
            gather(12,
                   {"gaussdb": {"mode": "normal", "fact_count": 0, "alert_count": 0,
                                "elapsed_ms": 0, "sections_failed": []},
                    "os": {"mode": "normal", "fact_count": 12, "alert_count": 0,
                           "elapsed_ms": 800, "sections_failed": []}},
                   [GOOD_FACT], layer_errors={"gaussdb": H1_ERR}),
            False, "层 gaussdb 事实数为 0")


def case_c_loop_guard():
    """_loop_guard 熔断替换：响应不是采集面正常形态 ⇒ 必须拒绝（不得当成空结果）。"""
    return ("C 熔断替换（mode 非 probes_v6）",
            HEALTH_H1,
            {"loop_detected": True, "tool": "gather", "window_s": 600},
            False, "采集面正常响应")


def case_d_positive():
    """★ 阴性对照（判别力）：各层事实数都 > 0 且事实可映射 ⇒ 门禁**不得**拒绝。"""
    details = {"gaussdb": {"mode": "normal", "fact_count": 1, "alert_count": 0,
                           "elapsed_ms": 120, "sections_failed": []}}
    return ("D 阳性对照（正常采集，期望通过）",
            HEALTH_H1, gather(1, details, [GOOD_FACT]), True, None)


def run_case(name, health, g, expect_pass, expect_substr, tmp):
    hp = os.path.join(tmp, "health.json")
    gp = os.path.join(tmp, "gather.json")
    mp = os.path.join(tmp, "map.yaml")
    with io.open(hp, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(health, ensure_ascii=False, indent=2))
    with io.open(gp, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(g, ensure_ascii=False, indent=2))
    with io.open(mp, "w", encoding="utf-8", newline="") as f:
        f.write(FIXTURE_MAP)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [PY, FREEZE,
           "--schema", SCHEMA, "--topology", TOPO, "--map", mp,
           "--health-json", hp, "--gather-json", gp,
           "--expect-simulated", "false", "--expected-profile", "h1-gate-test",
           "--layers", "gaussdb", "--dry-run"]
    pr = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        env=env, timeout=180)
    out = pr.stdout.decode("utf-8", "replace")
    refused = "FREEZE REFUSED" in out
    ok = (pr.returncode == 0) if expect_pass else (pr.returncode == 2 and refused)
    if not expect_pass and expect_substr and expect_substr not in out:
        ok = False
    print("-" * 74)
    print("[%s] %s" % ("PASS" if ok else "FAIL", name))
    print("    expect = %s ; exit = %s ; refused = %s"
          % ("PASS(写出/自检通过)" if expect_pass else "REFUSED(exit 2)", pr.returncode, refused))
    if expect_substr:
        print("    断言子串 = %r ; 命中 = %s" % (expect_substr, expect_substr in out))
    for line in out.strip().splitlines()[:8]:
        print("      | " + line[:160])
    return ok


def main():
    print("=" * 74)
    print("M1.7 H1 门禁验证（离线夹具；不连网络、不 import 采集层）")
    print("被测对象 = %s" % FREEZE)
    print("=" * 74)
    with tempfile.TemporaryDirectory(prefix="h1gate-") as tmp:
        results = [
            run_case(*case_a_h1(), tmp=tmp),
            run_case(*case_b_layer_only(), tmp=tmp),
            run_case(*case_c_loop_guard(), tmp=tmp),
            run_case(*case_d_positive(), tmp=tmp),
        ]
    print("=" * 74)
    n = sum(1 for r in results if r)
    print("H1 GATE RESULT: %s (%d/%d)" % ("PASS" if n == len(results) else "FAIL", n, len(results)))
    print("=" * 74)
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())