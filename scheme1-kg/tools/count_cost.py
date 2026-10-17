# -*- coding: utf-8 -*-
"""变更投入成本台账计数器（可复算）。

用法：
    python scheme1-kg/tools/count_cost.py            # 人读表
    python scheme1-kg/tools/count_cost.py --md       # Markdown 表（贴报告用）

口径（本项目的成本主判据 = 一条故障规则的修改量；裁决 8：语句数为主、行数为辅）：
    · 语句数 = 可执行 Cypher 语句条数（由 shared/verify/kg_tbox_check.py:cypher_statements 解析，
      与真机装载的 APPLY 条数同源 ⇒ 可机械核对 tmp/load_persist.log）
    · 行数   = 文件总行数（含注释；注释是契约/缺口的载体，也是成本的一部分）
    · 触达层 = 该 M2 项写入或引用的实体所覆盖的层（L1 App / L2 Runtime / L3 Data）
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "shared", "verify"))
import kg_tbox_check as K  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

M2 = [
    ("M2.1 拓扑/模式层", ["01-schema.cypher", "02-topology.cypher"]),
    ("M2.2 故障模式+因果边", ["03-fault-modes.cypher"]),
    ("M2.3 规则库", ["04-rules.cypher"]),
]

EXTRA = [
    ("（支撑）查询库 Q01–Q08", [os.path.join(ROOT, "scheme1-kg", "queries", n)
                             for n in sorted(os.listdir(os.path.join(ROOT, "scheme1-kg", "queries")))]),
    ("（支撑）T1–T9 测试", [os.path.join(ROOT, "scheme1-kg", "tests", "test_integrity.py")]),
    ("（支撑）装载器", [os.path.join(ROOT, "scheme1-kg", "tools", "load_tbox.py")]),
    ("（门禁）静态校验器", [os.path.join(ROOT, "shared", "verify", "kg_tbox_check.py")]),
]

def _edge_span(reltypes):
    """边 id 列表**从契约推导**（causal-edges.yaml 的 relation 字段）。
    ★ 自纠：本台账原先把 CC_CAUSES 标成「E1–E16」、CC_RELATES_TO 标成「E17–E18」，
      与契约不符 —— 实际 causes/conditional_causes = E1–E15+E18（16 条），relates_to = E16/E17。
      改为运行时从契约取，杜绝手写区间再次漂移。"""
    ids = sorted([e["id"] for e in K.EDGE_LIST if e.get("relation") in reltypes],
                 key=lambda s: int(s[1:]))
    return "%d 边：%s" % (len(ids), ",".join(ids))


CATS = [
    ("CC_FaultMode 写入点（逐个体，=14 节点）", r"MERGE\s*\(\s*\w+\s*:\s*CC_FaultMode\s*\{"),
    ("CC_Threshold 写入点（UNWIND 表，12 节点）", r":\s*CC_Threshold\s*\{"),
    ("CC_Metric 引用/写入点（UNWIND 表，58 节点）", r":\s*CC_Metric\s*\{"),
    ("CC_CAUSES 边写入点（契约 causes+conditional_causes，" + _edge_span(("causes", "conditional_causes")) + "）",
     r":\s*CC_CAUSES\s*\{"),
    ("CC_RELATES_TO 边写入点（契约 relates_to，" + _edge_span(("relates_to",)) + "）",
     r":\s*CC_RELATES_TO\s*\{"),
    ("CC_JUDGED_BY 边写入点（26 边）", r":\s*CC_JUDGED_BY\s*\{"),
    ("CC_AFFECTS 边写入点（逐 FM=14 语句；按目标类型 fan-out 得 42 边）", r":\s*CC_AFFECTS\s*\{"),
]


def stats(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    try:
        stmts = len(K.cypher_statements(path)[0])
    except Exception:  # 非 .cypher（.py）文件
        stmts = None
    import re as _re
    return len(text.encode("utf-8")), lines, stmts, text


def main():
    md = "--md" in sys.argv
    rows = []
    total_stmts = 0
    for label, names in M2:
        stmts = lines = 0
        for n in names:
            b, ln, st, _t = stats(os.path.join(K.ONT, n))
            stmts += st or 0
            lines += ln
            rows.append((label, "scheme1-kg/ontology/" + n, b, ln, st))
        total_stmts += stmts
    for label, paths in EXTRA:
        for p in paths:
            b, ln, st, _t = stats(p)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            rows.append((label, rel, b, ln, st))

    if md:
        print("| 归属 | 文件 | 字节 | 行数 | 可执行语句 |")
        print("|---|---|---:|---:|---:|")
    for label, rel, b, ln, st in rows:
        if md:
            print("| %s | `%s` | %d | %d | %s |" % (label, rel, b, ln, "-" if st is None else st))
        else:
            print("%-22s %-46s %7d 字节 %6d 行  语句 %s" % (label, rel, b, ln, "-" if st is None else st))

    print("")
    print("== 按 M2 项汇总（语句数为主判据）==")
    for label, names in M2:
        stmts = lines = 0
        for n in names:
            _b, ln, st, _t = stats(os.path.join(K.ONT, n))
            stmts += st or 0
            lines += ln
        print("   %-22s 文件 %d 个   可执行语句 %3d   行数 %4d" % (label, len(names), stmts, lines))

    print("")
    print("== 03-fault-modes.cypher 分类明细（M2.2 内部构成）==")
    _b, _ln, _st, text = stats(os.path.join(K.ONT, "03-fault-modes.cypher"))
    import re
    body = "\n".join(b for _l, b in K.cypher_statements(os.path.join(K.ONT, "03-fault-modes.cypher"))[0])
    for name, pat in CATS:
        print("   %-96s %d" % (name, len(re.findall(pat, body))))

    print("")
    print("== 触达层（跨层跨度）==")
    lyr = {}
    for fm in K.FM_LIST:
        lyr.setdefault(fm.get("layer"), []).append(fm["code"])
    for k in sorted(lyr, key=lambda x: str(x)):
        print("   FM 层 %-8s %d 个：%s" % (k, len(lyr[k]), ",".join(lyr[k])))
    print("   拓扑类型层分布：", end="")
    tl = {}
    for t in K.TOPO_TYPES:
        tl.setdefault(t.get("layer"), []).append(t["code"])
    print(" | ".join("%s=%d" % (k, len(v)) for k, v in sorted(tl.items(), key=lambda x: str(x[0]))))

    print("")
    print("== 是否牵动共享模块（应全为 0：只读引用，不修改）==")
    print("   命令：git status --porcelain shared docs | Measure-Object -Line")
    print("   shared/model/*.yaml / shared/mapping/*.yaml / docs/00|02|07 与对侧方案目录：见报告第 5 节证据行"
          "（跨方案对比只写在报告里，源码不引用对侧目录 —— G15/L3 硬约束）")
    print("")
    print("总可执行语句（M2.1+M2.2+M2.3）= %d" % total_stmts)


if __name__ == "__main__":
    main()
