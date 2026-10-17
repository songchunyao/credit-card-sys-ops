#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""build_pkg.py — 把 src\\ 打成确定性的 .difypkg（zip）。

确定性手段（四点都要，缺一不可复现 sha256）：
  1) 条目按 relpath 排序写入；
  2) 每个条目固定 date_time = 1980-01-01 00:00:00（zip 纪元下限）；
  3) 每个条目显式 create_system = 0（Windows 上 ZipInfo 默认 0、POSIX 默认 3，不写死就跨机不可复现）；
  4) external_attr 固定 0o644<<16，compresslevel 固定 9。

用法：
  python build_pkg.py                 # 构建并打印 bytes/sha256/条目数
  python build_pkg.py --check         # 重打包到临时文件并与已存在包比对 sha256（只读，不覆盖）
  python build_pkg.py --evidence P    # 额外把结果 JSON 写到 P
退出码：0 = 成功/一致；1 = 断言失败或 --check 不一致；2 = 参数错误。
"""
import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
DIST = os.path.join(HERE, "dist")
PKG_BASENAME = "cc-ops-neo4j_cypher_query-0.1.1.difypkg"
OUT = os.path.join(DIST, PKG_BASENAME)

EXPECTED_ENTRIES = 15
FORBIDDEN_DIRS = {".venv", "venv", "__pycache__", "dist", ".git", ".pytest_cache", ".mypy_cache"}
FORBIDDEN_SUFFIX = (".pyc", ".pyo")
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
FIXED_ATTR = 0o644 << 16
COMPRESSLEVEL = 9


class BuildError(Exception):
    pass


def collect():
    """返回排序后的 relpath（POSIX 分隔）列表，并在此处守门。"""
    if not os.path.isdir(SRC):
        raise BuildError("src 目录不存在: %s" % SRC)
    names = []
    for root, dirs, files in os.walk(SRC):
        rel_root = os.path.relpath(root, SRC)
        parts = [] if rel_root == "." else rel_root.replace("\\", "/").split("/")
        bad = FORBIDDEN_DIRS.intersection(parts)
        if bad:
            raise BuildError("src 树内出现禁止目录 %s（位置 %s）" % (sorted(bad), rel_root))
        dirs[:] = sorted(d for d in dirs if d not in FORBIDDEN_DIRS)
        for fn in files:
            if fn.endswith(FORBIDDEN_SUFFIX):
                raise BuildError("src 树内出现禁止文件: %s" % os.path.join(rel_root, fn))
            rel = fn if rel_root == "." else "%s/%s" % (rel_root.replace("\\", "/"), fn)
            names.append(rel)
    names.sort()
    if "manifest.yaml" not in names:
        raise BuildError("根目录没有 manifest.yaml（Dify 要求包根放 manifest.yaml）")
    if len(names) != EXPECTED_ENTRIES:
        raise BuildError("条目数 = %d，期望 %d" % (len(names), EXPECTED_ENTRIES))
    return names


def build(out_path, names):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=COMPRESSLEVEL) as zf:
        for rel in names:
            src_path = os.path.join(SRC, rel.replace("/", os.sep))
            with open(src_path, "rb") as fh:
                payload = fh.read()
            zi = zipfile.ZipInfo(rel, date_time=FIXED_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 0          # 不写死 → Windows(0)/POSIX(3) 不一致，sha256 不可复现
            zi.external_attr = FIXED_ATTR
            zi.internal_attr = 0
            zf.writestr(zi, payload)
    return out_path


def facts(path):
    b = open(path, "rb").read()
    with zipfile.ZipFile(io.BytesIO(b)) as zf:
        entries = zf.namelist()
        bad_crc = zf.testzip()
    return {
        "path": path,
        "bytes": len(b),
        "sha256": hashlib.sha256(b).hexdigest(),
        "n_entries": len(entries),
        "root_has_manifest_yaml": "manifest.yaml" in entries,
        "entries": entries,
        "testzip_bad_member": bad_crc,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build deterministic .difypkg")
    ap.add_argument("--check", action="store_true",
                    help="重打包到临时文件并与已存在包比对 sha256（不覆盖已存在包）")
    ap.add_argument("--out", default=OUT, help="输出 .difypkg 路径（默认 dist/%s）" % PKG_BASENAME)
    ap.add_argument("--evidence", default=None, help="把结果 JSON 写到该路径")
    args = ap.parse_args(argv)

    result = {"mode": "check" if args.check else "build", "src": SRC, "expected_entries": EXPECTED_ENTRIES}
    try:
        names = collect()
    except BuildError as e:
        result["status"] = "failed"
        result["error"] = str(e)
        print("BUILD FAILED: %s" % e)
        if args.evidence:
            with open(args.evidence, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 1

    result["src_files"] = names
    print("src 树条目 %d 个（排序后）:" % len(names))
    for n in names:
        print("  " + n)

    if args.check:
        if not os.path.exists(args.out):
            result["status"] = "failed"
            result["error"] = "已存在包不存在，无法 --check: %s" % args.out
            print("CHECK FAILED: %s" % result["error"])
            return 1
        existing = facts(args.out)
        with tempfile.TemporaryDirectory() as td:
            tmp = os.path.join(td, PKG_BASENAME)
            build(tmp, names)
            rebuilt = facts(tmp)
        same = rebuilt["sha256"] == existing["sha256"]
        result.update({"status": "passed" if same else "failed",
                       "existing": existing, "rebuilt": rebuilt,
                       "sha256_match": same})
        print("\n-- 已存在包: %s\n   bytes=%d sha256=%s entries=%d" %
              (existing["path"], existing["bytes"], existing["sha256"], existing["n_entries"]))
        print("-- 重打包  : bytes=%d sha256=%s entries=%d" %
              (rebuilt["bytes"], rebuilt["sha256"], rebuilt["n_entries"]))
        print("\nSHA256 MATCH: %s" % same)
        if args.evidence:
            with open(args.evidence, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return 0 if same else 1

    build(args.out, names)
    f = facts(args.out)
    result.update({"status": "passed", "package": f})
    print("\n-- 产出: %s" % f["path"])
    print("   bytes=%d" % f["bytes"])
    print("   sha256=%s" % f["sha256"])
    print("   entries=%d" % f["n_entries"])
    print("   root_has_manifest_yaml=%s" % f["root_has_manifest_yaml"])
    print("   testzip_bad_member=%s" % f["testzip_bad_member"])
    if args.evidence:
        with open(args.evidence, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
