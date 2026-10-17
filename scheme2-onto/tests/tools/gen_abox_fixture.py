#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把**真机 ABox 全图导出**固化成耐久夹具 `abox-live5.ttl`（= 溯源注释头 + 导出物原始字节）。

## 这个脚本读什么
* `--in <raw.ttl>` —— **必填**：GraphDB 仓 `cc-ops` 的一次 CONSTRUCT 全图导出物。
  导出方式（与夹具头注释声明一致）：
      CONSTRUCT { ?s ?p ?o } WHERE { GRAPH <https://credit-card-ops.ai/onto/graph/<快照 id>> { ?s ?p ?o } }
  端点 `http://192.0.2.10:7200/repositories/cc-ops`（匿名只读）。
* **不连图库、不依赖 `tmp/`**：导出物在哪由 `--in` 指路，脚本只做「加头 + 校验 + 落盘」。
  `--in` **没有默认值**，就是为了不把「导出物放在 tmp/」这个历史习惯固化进工具。

## 这个脚本写什么
* `<out>/abox-live5.ttl` —— 15 行 `#` 溯源注释头（来源端点 / 命名图 / 快照 id / 三元组数 /
  导出方式 / 导出时间 / 原始导出物的 bytes+sha256 / 用途 / 冻结声明）
  **接在导出物原始字节之前**。Turtle 的注释必须落在前缀声明之外的头部，故顺序不可调换。
  除注释头外**不加一个字节**：正文 = 导出物的逐字节副本。
* 头部里记录的是**当时那一次导出**的事实（尤其 `导出时间` = 导出物的文件 mtime、
  `sha256` = 导出物的哈希），因此**头部随导出物一起变化**，这正是溯源的本意。

## 什么条件下必须重跑
1. 真机重新投影出新快照、需要换夹具时（同时要更新 `--snapshot` 与调用方的快照 id）；
2. 发现夹具正文与当前目标快照图不一致时（用 `tmp/_m223_live_probe.py` 之类的探针复核
   目标命名图的 `COUNT(*)`，再决定是否换快照）。

## 模式
* 默认（`--write`）：生成并写盘。
* `--check`：**不写盘**，逐字节比对盘上夹具与「同一导出物 + 同一元数据」的再生成结果，
  末行 `RESULT: PASS` / `RESULT: FAIL`（fail-closed，退出码 1）。

用法：
    python scheme2-onto/tests/tools/gen_abox_fixture.py --check --in tmp/m211_live5_abox.ttl
    python scheme2-onto/tests/tools/gen_abox_fixture.py --write --in <新导出物.ttl> --snapshot <新快照 id>
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sys

import rdflib

HERE = os.path.dirname(os.path.abspath(__file__))      # …/scheme2-onto/tests/tools
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # 仓库根（上溯 3 层）
DEFAULT_OUT = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'abox-live5.ttl')

ENDPOINT = 'http://192.0.2.10:7200'
REPO = 'cc-ops'
DEFAULT_SNAPSHOT = 'ccf-20260928T155516Z-937b79'
DEFAULT_TRIPLES = 2965


def sha(b):
    return hashlib.sha256(b).hexdigest().upper()


def build_header(raw, snapshot, triples, export_time, src_label):
    graph = 'https://credit-card-ops.ai/onto/graph/' + snapshot
    return f"""# ==============================================================================
# 夹具快照（fixture snapshot）—— **不是活数据**，禁止当作实时 ABox 使用。
#
# 来源      ：GraphDB 真机 {ENDPOINT} 仓库 {REPO}（匿名只读）
# 命名图    ：{graph}
# 快照 id   ：{snapshot}（M2.11 P2 非破坏投影产物，最新一张）
# 三元组数  ：{triples}
# 导出方式  ：CONSTRUCT {{ ?s ?p ?o }} WHERE {{ GRAPH <命名图> {{ ?s ?p ?o }} }}（全图导出）
# 导出时间  ：{export_time}（原始导出物 {src_label} 的 mtime）
# 原始证据  ：{src_label}（{len(raw)} B，sha256 {sha(raw)}）—— 本文件是它的逐字节副本 + 本注释头
# 用途      ：scheme2-onto\\tests\\test_integrity.py 的 ABOX_LIVE / ABOX_FULL 真机夹具。
#             替代 2026-09-28 的旧投影 tmp\\cc_ops_live2.ttl（早于 M2.11 的 Table 身份修复，
#             缺 cc:Table 键属性 ⇒ T11b 会假报 30 条 topology#schemaName 违例）。
# 冻结声明  ：本文件 = 上述命名图在该时点的全图导出，随快照冻结；真机图后续变化不回流。
# ==============================================================================
"""


def generate(in_path, snapshot, triples, export_time=None, src_label=None):
    raw = open(in_path, 'rb').read()
    assert raw[:3] != b'\xef\xbb\xbf', '导出物不应带 BOM'
    assert b'\r' not in raw, '导出物应为 LF'
    if triples:
        _g = rdflib.Graph()
        _g.parse(in_path, format='turtle')
        n = len(_g)
        assert n == triples, ('导出物三元组数与 --triples 不符：实际 %d，声明 %d（'
                              '若不是重投影请核对 --triples 与快照 id）' % (n, triples))
    if export_time is None:
        export_time = datetime.datetime.fromtimestamp(os.path.getmtime(in_path)).strftime('%Y-%m-%d %H:%M:%S')
    if src_label is None:
        src_label = os.path.relpath(in_path, ROOT)
    out = build_header(raw, snapshot, triples, export_time, src_label).encode('utf-8') + raw
    assert out[:3] != b'\xef\xbb\xbf', '产物不应带 BOM'
    assert b'\r' not in out, '产物应为 LF'
    return out, {'raw_bytes': len(raw), 'raw_sha256': sha(raw),
                 'header_bytes': len(out) - len(raw), 'triples': triples,
                 'export_time': export_time, 'src_label': src_label}


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description='把真机 ABox 全图导出固化为耐久夹具 abox-live5.ttl')
    ap.add_argument('--in', dest='inp', required=True, help='GraphDB CONSTRUCT 全图导出物（必填）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='输出夹具路径（默认 tests/fixtures/abox-live5.ttl）')
    ap.add_argument('--snapshot', default=DEFAULT_SNAPSHOT, help='快照 id（命名图后缀）')
    ap.add_argument('--triples', type=int, default=DEFAULT_TRIPLES, help='期望三元组数（0 = 不校验）')
    ap.add_argument('--export-time', default=None, help='覆盖「导出时间」（默认取 --in 的 mtime）')
    ap.add_argument('--src-label', default=None, help='覆盖头注释里记录的导出物路径（默认 ROOT 相对路径）')
    ap.add_argument('--check', action='store_true', help='不写盘，只比对盘上夹具与再生成结果')
    ap.add_argument('--write', action='store_true', help='写盘（默认动作）')
    args = ap.parse_args()

    out, st = generate(args.inp, args.snapshot, args.triples, args.export_time, args.src_label)
    print('读入导出物: %s（%d B，sha256 %s）' % (st['src_label'], st['raw_bytes'], st['raw_sha256']))
    print('元数据    : 快照 %s  三元组数 %d  导出时间 %s  注释头 %d B'
          % (args.snapshot, st['triples'], st['export_time'], st['header_bytes']))
    print('再生成件  : %d B  sha256 %s' % (len(out), sha(out)))
    print()

    if args.check:
        if not os.path.exists(args.out):
            print('盘上件    : %s  [MISSING]' % os.path.relpath(args.out, ROOT))
            print()
            print('RESULT: FAIL')
            return 1
        disk = open(args.out, 'rb').read()
        same = (disk == out)
        print('盘上件    : %s  %d B  %s' % (os.path.relpath(args.out, ROOT), len(disk), sha(disk)))
        if not same:
            dl = disk.decode('utf-8', 'replace').splitlines()
            ol = out.decode('utf-8', 'replace').splitlines()
            bad = [(i + 1, a, b) for i, (a, b) in enumerate(zip(dl, ol)) if a != b]
            print('  首个不同的行数 = %d（总行 %d vs %d）' % (len(bad), len(dl), len(ol)))
            for i, a, b in bad[:4]:
                print('    L%d 盘上  : %s' % (i, a))
                print('    L%d 再生成: %s' % (i, b))
        print()
        print('RESULT: %s' % ('PASS' if same else 'FAIL'))
        return 0 if same else 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, 'wb').write(out)
    print('已写出    : %s（%d B，sha256 %s）' % (os.path.relpath(args.out, ROOT), len(out), sha(out)))
    print()
    print('RESULT: PASS（已写盘）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
