#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「契约全投影合成 ABox」的补丁件 `cc_proj_fix.ttl`。

## 这个脚本读什么
* `scheme2-onto/tests/fixtures/abox-live5.ttl` —— **耐久真机投影夹具**（默认输入，可用 `--in` 改）。
  它导出自 GraphDB 仓 `cc-ops` 的命名图
  `https://credit-card-ops.ai/onto/graph/ccf-20260928T155516Z-937b79`（**快照 id `ccf-20260928T155516Z-937b79`**），
  2965 三元组，CONSTRUCT 全图导出，随快照冻结。
* **不连图库、不读 `tmp/`**。历史版本读 `tmp/cc_ops_live2.ttl`（2026-09-28 的旧投影，已废止）——
  本迁移版把默认输入改到耐久夹具。

## 这个脚本写什么
* `<out>/cc_proj_fix.ttl` —— 只补投影器漏掉的**契约必填事实**，不新增任何契约外事实、
  不改阈值、不改规则：
    * `D-1` `cc:Table` 缺 `cc:schemaName` → 补 `"public"`
    * `D-3` `cc:Host` 缺 `cc:hostsContainer` → 由 `Container cc:deployedOn Host` 反推
    * `D-4` `ccm:status` 大小写不符 → 按 `metrics.yaml` 的 enum `[running, waiting, terminated]` 小写化
    * `D-2` 会话链 3 跳（`hasSession` / `runsQuery` / `accesses`）由 `cc_shacl_ok.ttl` 覆盖，不在此重复补
  输出按 `(subject, predicate, object)` 字典序逐行写，UTF-8 无 BOM + LF ⇒ **字节可复现**。

## ⚠ 已知落差（不要静默「修好」，交架构裁决）
盘上的 `scheme2-onto/tests/fixtures/cc_proj_fix.ttl`（32 条）是由**已废止的旧投影**
`tmp/cc_ops_live2.ttl` 生成的，其中 30 条 `cc:schemaName "public"` 挂在旧命名的
`…/inst/table/tbl-<name>` 上，而耐久夹具 `abox-live5.ttl` 的 Table IRI 是
`…/inst/table/tbl-<schema>.<name>`（带 schema 前缀）——两套 IRI **交集为空**，且那 30 个
subject 在合并图里**根本不是 `cc:Table`**。因此它们对该夹具是**悬空三元组**（既不生效也不报错）。
用本脚本的**默认耐久输入**重生成会得到 **2 条**（`D-3`/`D-4` 各 1；`D-1` = 0，因为 live5 的
30 个 Table 已 100% 带 `cc:schemaName`）。`--check` 在这种情况下会报 `MISMATCH` —— 这是**真实落差**，
不是本脚本的 bug；是否替换盘上夹具由架构/队长裁决，本脚本不替它做决定。

## ⚠ M2.27 弃用声明（本脚本产物已不建议再使用）
* 本脚本的 `D-4` 规则产出的是**历史小写补丁**：它把 `ccm:status` 的值 `.lower()` 成 `running`/`waiting`/`terminated`。该口径已被 **M2.27 推翻** —— 契约取值域现为采集面真实 4 值
  `Running`/`Waiting`/`Terminated`/`Unknown`（结构性封闭，见 `shared/model/metrics.yaml` 的 `container.status`
  与 `scheme2-onto/shapes/shacl-constraints.ttl` 的 `sh:in`），**无需任何小写化 / 值变换**。
  上面 D-4 一行里的 `[running, waiting, terminated]` 属旧契约文本，勿再引用。
* 盘上的 `scheme2-onto/tests/fixtures/cc_proj_fix.ttl` 是**旧投影遗留**：由已废止的旧投影
  `tmp/cc_ops_live2.ttl` 生成，含 30 条悬空三元组，**不可再生**（默认耐久输入重生成只得 2 条）。
  队长已裁定该夹具**保持字节不变**，仅作历史遗留证据保留。
* ⇒ **不要再用本脚本重生成 / 替换该夹具**；如需清理，先由架构/队长裁决（本脚本不替它做决定）。

## 什么条件下必须重跑
1. 重新冻结 `abox-live5.ttl`（换快照）之后；
2. `shared/model/metrics.yaml` 的枚举值变动，或 `cc:Table` / `cc:Host` 键属性契约变动；
3. `--check` 报 `MISMATCH`（说明盘上件与默认输入不再同源）。

## 模式
* 默认（`--write`）：重新生成并写盘。
* `--check`：**不写盘**，与盘上件逐字节比对，打印 sha256 与判定，末行 `RESULT: PASS` / `RESULT: FAIL`。

用法：
    python scheme2-onto/tests/tools/gen_proj_fix.py --check
    python scheme2-onto/tests/tools/gen_proj_fix.py --out tmp/cc_proj_fix_regen.ttl
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys

import rdflib

HERE = os.path.dirname(os.path.abspath(__file__))      # …/scheme2-onto/tests/tools
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # 仓库根（上溯 3 层）
DEFAULT_IN = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'abox-live5.ttl')
DEFAULT_OUT = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'cc_proj_fix.ttl')

CC = rdflib.Namespace('https://credit-card-ops.ai/onto/topology#')
CCM = rdflib.Namespace('https://credit-card-ops.ai/onto/metric#')

HEADER = (
    '# 契约全投影合成 ABox 修复件（T5/T11 门禁用；由 scheme2-onto/tests/tools/gen_proj_fix.py 生成）\n'
    '# 只补投影器漏掉的契约必填事实：D-1 Table.schemaName / D-3 Host.hostsContainer / '
    'D-4 ccm:status 大小写\n'
)


def sha(b):
    return hashlib.sha256(b).hexdigest().upper()


def generate(in_path):
    """返回 (bytes, stats)。stats 含 D-1/D-3/D-4 计数与输入三元组数。"""
    g = rdflib.Graph()
    g.parse(in_path, format='turtle')
    add = rdflib.Graph()

    n1 = 0
    for t in sorted(g.subjects(rdflib.RDF.type, CC.Table), key=str):
        if not list(g.objects(t, CC.schemaName)):
            add.add((t, CC.schemaName, rdflib.Literal('public')))
            n1 += 1

    n3 = 0
    for c in sorted(g.subjects(rdflib.RDF.type, CC.Container), key=str):
        for h in g.objects(c, CC.deployedOn):
            if (h, CC.hostsContainer, c) not in add and (h, CC.hostsContainer, c) not in g:
                add.add((h, CC.hostsContainer, c))
                n3 += 1

    n4 = 0
    for s, p, v in list(g.triples((None, CCM.status, None))):
        if isinstance(v, rdflib.Literal):
            low = str(v).strip().lower()
            if low != str(v):
                add.add((s, p, rdflib.Literal(low)))
                n4 += 1

    buf = io.StringIO()
    buf.write(HEADER)
    buf.write('@prefix cc:  <https://credit-card-ops.ai/onto/topology#> .\n')
    buf.write('@prefix ccm: <https://credit-card-ops.ai/onto/metric#> .\n\n')
    for s, p, o in sorted(add, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))):
        buf.write('%s %s %s .\n' % (s.n3(), p.n3(), o.n3()))

    b = buf.getvalue().encode('utf-8')
    assert b[:3] != b'\xef\xbb\xbf', '产物不应带 BOM'
    assert b'\r' not in b, '产物应为 LF'
    return b, {'in_triples': len(g), 'patched': len(add),
               'D-1': n1, 'D-3': n3, 'D-4': n4}


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description='生成 cc_proj_fix.ttl（契约全投影合成 ABox 补丁）')
    ap.add_argument('--in', dest='inp', default=DEFAULT_IN, help='输入 ABox（默认耐久真机夹具 abox-live5.ttl）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出路径（默认 tests/fixtures/cc_proj_fix.ttl）')
    ap.add_argument('--check', action='store_true', help='不写盘，只比对盘上件与再生成件是否逐字节一致')
    ap.add_argument('--write', action='store_true', help='写盘（默认动作）')
    args = ap.parse_args()

    b, st = generate(args.inp)
    print('读入 ABox : %s（%d 三元组）' % (os.path.relpath(args.inp, ROOT), st['in_triples']))
    print('补丁计数  : D-1 Table.schemaName=%d  D-3 Host.hostsContainer=%d  D-4 ccm:status 小写化=%d'
          '  ⇒ 合计 %d 条' % (st['D-1'], st['D-3'], st['D-4'], st['patched']))
    print()

    if args.check:
        if not os.path.exists(args.out):
            print('盘上件    : %s  [MISSING]' % os.path.relpath(args.out, ROOT))
            print()
            print('RESULT: FAIL')
            return 1
        disk = open(args.out, 'rb').read()
        same = (disk == b)
        print('盘上件    : %s  %d B  %s' % (os.path.relpath(args.out, ROOT), len(disk), sha(disk)))
        print('再生成件  : %d B  %s' % (len(b), sha(b)))
        print('逐字节一致: %s' % same)
        if not same:
            dg = rdflib.Graph()
            dg.parse(data=disk.decode('utf-8'), format='turtle')
            rg = rdflib.Graph()
            rg.parse(data=b.decode('utf-8'), format='turtle')
            only_disk = sorted(dg - rg, key=lambda t: (str(t[0]), str(t[1]), str(t[2])))
            only_regen = sorted(rg - dg, key=lambda t: (str(t[0]), str(t[1]), str(t[2])))
            print('  只在盘上  : %d 条' % len(only_disk))
            for t in only_disk[:5]:
                print('    - %s' % (t,))
            if len(only_disk) > 5:
                print('    … 其余 %d 条同型（挂在 abox-live5 中不存在的旧命名 Table IRI 上）' % (len(only_disk) - 5))
            print('  只在再生成: %d 条' % len(only_regen))
            for t in only_regen[:5]:
                print('    + %s' % (t,))
        print()
        print('RESULT: %s' % ('PASS' if same else 'FAIL'))
        return 0 if same else 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, 'wb').write(b)
    print('已写出    : %s（%d B，%d 条补丁，sha256 %s）'
          % (os.path.relpath(args.out, ROOT), len(b), st['patched'], sha(b)))
    print()
    print('RESULT: PASS（已写盘）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
