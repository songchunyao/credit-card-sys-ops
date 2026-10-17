#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G17 · 文档表格完整性门禁（只读、纯标准库、退出码 0/1）。

背景（为什么要机械化）：本项目的 Markdown 表格被多处门禁按「**未转义**竖线数」
判定列数（如 `mapping_gate` 的台账、`docs\\07` 的里程碑表）。历史事故：
给 `docs\\07` 补 ✅ 时误加第 4 个单元格 ⇒ 表格列数不一致，但没有任何门禁能发现。

口径（与既有门禁一致）：
- 表格块 = 以 `|` 开头的一行 + 紧随的 `|---|:--|` 分隔行 + 其后所有以 `|` 开头的行；
- 每行的列数 = **未转义**竖线数，即 `len(re.findall(r"(?<!\\\\)\\|", line))`（`\\|` 是**转义**竖线，不计）；
- 块内所有行的列数必须一致，多数派即期望值；少数派行 ⇒ ERROR（报行号与两侧列数）。

★ 反「恒真门禁」设计（C18 教训：正则漏字符 ⇒ 分支永不命中却一路 PASS）：
  断言**扫描到的表格块总数 ≥ --min-tables**（默认 50），否则判 ERROR——
  正则若因写法变化而失配，本门禁必须**失败**而不是静默通过。

用法：
  python shared\\verify\\doc_table_gate.py [--docs-dir docs] [--min-tables 50] [--quiet]
"""
import argparse
import os
import re
import sys

PIPE = re.compile(r"(?<!\\)\|")
SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def check_file(path):
    """返回 (块数, [(行号, 该行列数, 多数派列数), ...])。"""
    with open(path, "rb") as f:
        raw = f.read().decode("utf-8")
    lines = raw.replace("\r\n", "\n").split("\n")
    bad = []
    blocks = 0
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and SEP.match(lines[i + 1]):
            j = i
            rows = []
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append((j + 1, len(PIPE.findall(lines[j]))))
                j += 1
            blocks += 1
            counts = sorted(set(c for _, c in rows))
            if len(counts) > 1:
                from collections import Counter
                major = Counter(c for _, c in rows).most_common(1)[0][0]
                for ln, c in rows:
                    if c != major:
                        bad.append((ln, c, major))
            i = j
        else:
            i += 1
    return blocks, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-dir", default=None, help="默认 <repo>/docs")
    ap.add_argument("--min-tables", type=int, default=50,
                    help="扫描到的表格块总数下限；低于此值即判 ERROR（反恒真门禁）")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    docs = a.docs_dir or os.path.join(root, "docs")
    if not os.path.isdir(docs):
        print("[ERROR] docs 目录不存在：%s" % docs)
        return 1
    files = sorted(f for f in os.listdir(docs) if f.endswith(".md"))
    if not files:
        print("[ERROR] %s 下没有 .md 文件" % docs)
        return 1

    total_blocks = 0
    total_bad = 0
    for name in files:
        blocks, bad = check_file(os.path.join(docs, name))
        total_blocks += blocks
        total_bad += len(bad)
        if bad:
            print("[FAIL] %s  表格 %d 块，列数不一致 %d 行" % (name, blocks, len(bad)))
            for ln, c, major in bad:
                print("        L%-5d 未转义竖线 %d（该表多数为 %d）" % (ln, c, major))
        elif not a.quiet:
            print("[ OK ] %s  表格 %d 块，列数全部自洽" % (name, blocks))

    print("-" * 70)
    if total_blocks < a.min_tables:
        print("[ERROR] 只扫描到 %d 个表格块（< --min-tables %d）⇒ 扫描口径可能已失配，拒绝 PASS"
              % (total_blocks, a.min_tables))
        print("RESULT: FAIL（文档表格完整性门禁 G17）")
        return 1
    if total_bad:
        print("RESULT: FAIL（文档表格完整性门禁 G17：%d 行列数不一致 / %d 个表格块）"
              % (total_bad, total_blocks))
        return 1
    print("RESULT: PASS（文档表格完整性门禁 G17：%d 份文档 / %d 个表格块，列数全部自洽）"
          % (len(files), total_blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
