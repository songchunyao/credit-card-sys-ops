#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shared/deploy/port/pack_collect_layer.py —— 采集层（:8187 数据面）打包器。

用途
----
把本仓自带的采集层（源码树 = `dist/collect-layer/`），按
`shared/deploy/port/collect-layer.manifest.yaml` **可校验地**装进一个自包含 bundle：
逐文件核对 bytes + sha256，通过后再装载，并写 bundle 内的 `BUNDLE-MANIFEST.json`。

三种模式
--------
    python pack_collect_layer.py --source dist/collect-layer --verify
        只读校验：清单里每个文件的 bytes/sha256 是否与源树一致（不动任何文件）。

    python pack_collect_layer.py --source dist/collect-layer --out dist/collect-layer
        校验通过后装载到 <out>/，并写 <out>/BUNDLE-MANIFEST.json（bundle 内逐文件 sha256）。

    python pack_collect_layer.py --selftest
        离线自检（不需要源树、不需要网络）：含 5 条负控。

退出码：0=PASS 1=FAIL（校验不过/自检失败） 2=BLOCKED（缺清单/缺源树/参数错）
★ BLOCKED ≠ FAIL：源树不在（用户只拷了部分文件）是「没测到」，不是「测得坏」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 PyYAML", file=sys.stderr)
    raise SystemExit(2)

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "collect-layer.manifest.yaml"

PASS, FAIL, BLOCKED = 0, 1, 2


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def check(man: dict, source: Path, problems: list) -> list:
    """返回逐文件读数 [(rel, bytes_ok, sha_ok, actual_bytes, actual_sha)]。"""
    rows = []
    for item in man.get("required_files") or []:
        rel = item["path"]
        src = source / rel
        if not src.is_file():
            problems.append("缺文件：%s" % rel)
            rows.append((rel, False, False, None, None))
            continue
        b = src.stat().st_size
        s = sha256_file(src)
        ok_b = (b == item.get("bytes"))
        ok_s = (s == item.get("sha256"))
        if not ok_b:
            problems.append("bytes 不符：%s（声明 %s，实测 %s）" % (rel, item.get("bytes"), b))
        if not ok_s:
            problems.append("sha256 不符：%s（声明 %s…，实测 %s…）"
                            % (rel, str(item.get("sha256"))[:16], s[:16]))
        rows.append((rel, ok_b, ok_s, b, s))

    # 排除面：知识面文件**不得**出现在源树将要装载的集合里
    for rel in man.get("excluded_knowledge_plane") or []:
        if rel in [i["path"] for i in (man.get("required_files") or [])]:
            problems.append("知识面文件 %s 被误列入必需清单" % rel)
    return rows


def validate_manifest_shape(man: dict, problems: list) -> None:
    for key in ("meta", "required_files", "excluded_knowledge_plane",
                "layout_constraint", "runtime_deps"):
        if key not in man:
            problems.append("清单缺顶层键：%s" % key)
    if not (man.get("required_files") or []):
        problems.append("required_files 为空 —— 打包器不能在没有清单时声称成功")
    for item in man.get("required_files") or []:
        for k in ("path", "role", "bytes", "sha256"):
            if k not in item:
                problems.append("required_files 条目缺 %s：%r" % (k, item.get("path")))


# ---------------------------------------------------------------------------
# 装载
# ---------------------------------------------------------------------------
def pack(man: dict, source: Path, out: Path) -> list:
    problems = []
    if out.exists():
        shutil.rmtree(out)
    copied = []
    for item in man["required_files"]:
        rel = item["path"]
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dst)
        copied.append(rel)

    # —— 布局硬约束（storage_probe 靠上溯 3 级找 storages/）——
    probe = out / "service" / "probes" / "storage_probe.py"
    stores = out / "storages"
    if not probe.is_file():
        problems.append("布局断言失败：%s 不存在" % probe.relative_to(out))
    if not stores.is_dir():
        problems.append("布局断言失败：storages/ 不存在（storage_probe 将找不到采集封装）")
    if probe.is_file() and stores.is_dir():
        if probe.parents[2] != stores.parent:
            problems.append("布局断言失败：service/ 与 storages/ 不是同级目录")

    # —— 排除面断言：知识面一个都不许进 ——
    for rel in man["excluded_knowledge_plane"]:
        if (out / rel).exists():
            problems.append("知识面文件进了包：%s" % rel)

    # —— 可选软依赖**不得**被带进来（见清单 W1）——
    for item in man.get("optional_soft_deps") or []:
        if (out / item["path"]).exists():
            problems.append("可缺的软依赖被带进包：%s（发布镜像刻意不带软依赖）" % item["path"])

    # —— bundle 清单 ——
    bundle = {
        "schema": "collect-layer-bundle/1",
        "generated_from_manifest_version": (man.get("meta") or {}).get("version"),
        "source_git_head": (man.get("meta") or {}).get("source_git_head"),
        "service_port": (man.get("meta") or {}).get("service_port"),
        "launch": (man.get("meta") or {}).get("launch"),
        "files": [{"path": r,
                   "bytes": (out / r).stat().st_size,
                   "sha256": sha256_file(out / r)} for r in sorted(copied)],
    }
    (out / "BUNDLE-MANIFEST.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return problems


# ---------------------------------------------------------------------------
# 自检（离线）
# ---------------------------------------------------------------------------
def selftest() -> int:
    man = load_manifest(DEFAULT_MANIFEST)
    results = []

    # 0) 清单结构正控
    p0: list = []
    validate_manifest_shape(man, p0)
    results.append(("清单结构自洽（顶层键 + 每条含 path/role/bytes/sha256）", not p0, p0))

    # 1) 正控：清单声明「不得列入」的知识面文件确实不在必需清单里
    req = {i["path"] for i in man["required_files"]}
    bad = req & set(man["excluded_knowledge_plane"])
    results.append(("知识面文件不在必需清单（正控）", not bad, ["交集 %s" % sorted(bad)] if bad else []))

    # 2) 正控：可选软依赖 collect_sim 不在必需清单（W1）
    soft = {i["path"] for i in man.get("optional_soft_deps") or []}
    bad = req & soft
    results.append(("可缺软依赖不在必需清单（W1 正控）", not bad, ["交集 %s" % sorted(bad)] if bad else []))

    # 3) 负控：用**假源树**跑一遍 —— 篡改一个文件 ⇒ 必须报 sha256 不符
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        for item in man["required_files"]:
            p = src / item["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * min(int(item["bytes"]), 64))     # 内容/长度都故意不对
        probs: list = []
        check(man, src, probs)
        results.append(("负控：源树内容不符 ⇒ 报错（不得静默通过）", len(probs) > 0,
                        ["未报任何问题"] if not probs else []))

    # 4) 负控：清单缺 required_files ⇒ 结构校验必须失败
    bad_man = json.loads(json.dumps(man))
    bad_man["required_files"] = []
    p4: list = []
    validate_manifest_shape(bad_man, p4)
    results.append(("负控：required_files 为空 ⇒ 结构校验失败", len(p4) > 0, []))

    # 5) 负控：知识面文件被塞进 required_files ⇒ 校验必须抓出
    bad_man2 = json.loads(json.dumps(man))
    bad_man2["required_files"].append(
        {"path": "service/knowledge_api.py", "role": "x", "bytes": 1, "sha256": "0" * 64})
    p5: list = []
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        check(bad_man2, src, p5)
    results.append(("负控：知识面文件混入必需清单 ⇒ 被抓出", len(p5) > 0, []))

    print("=" * 78)
    print("pack_collect_layer.py --selftest（离线）")
    print("=" * 78)
    bad = 0
    for name, ok, why in results:
        print("[%s] %s%s" % ("OK  " if ok else "BAD ", name,
                             "" if ok else "   ← " + "; ".join(why)))
        if not ok:
            bad += 1
    print("-" * 78)
    print("自检 %d 条，BAD=%d ⇒ %s" % (len(results), bad, "PASS" if bad == 0 else "FAIL"))
    return PASS if bad == 0 else FAIL


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="采集层打包器（清单驱动、可校验）")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--source", help="采集层源码树根（本仓为 dist/collect-layer）")
    ap.add_argument("--out", help="bundle 输出目录，如 dist/collect-layer")
    ap.add_argument("--verify", action="store_true", help="只校验，不装载")
    ap.add_argument("--selftest", action="store_true", help="离线自检")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    mpath = Path(args.manifest)
    if not mpath.is_file():
        print("BLOCKED：找不到清单 %s" % mpath)
        return BLOCKED
    man = load_manifest(mpath)
    problems: list = []
    validate_manifest_shape(man, problems)
    if problems:
        print("FAIL：清单结构不合法")
        for p in problems:
            print("  - %s" % p)
        return FAIL

    if not args.source:
        print("BLOCKED：需要 --source <采集层源码树根>（本仓为 dist/collect-layer；或 --selftest）")
        return BLOCKED
    src = Path(args.source)
    if not (src / "service" / "app_collect.py").is_file():
        print("BLOCKED：%s 看起来不是采集层源码树根（缺 service/app_collect.py）" % src)
        return BLOCKED

    print("清单：%s（%s v%s）" % (mpath, (man["meta"] or {}).get("contract"),
                                  (man["meta"] or {}).get("version")))
    print("采集层树：%s（清单声明的上游提交=%s）" % (src, (man["meta"] or {}).get("upstream_git_head_short")))
    print("必需文件 %d 个（+ 可选软依赖 %d 个，**不打包**）"
          % (len(man["required_files"]), len(man.get("optional_soft_deps") or [])))
    print("-" * 78)

    rows = check(man, src, problems)
    for rel, ok_b, ok_s, b, s in rows:
        print("  [%s] %-44s %s" % ("OK " if (ok_b and ok_s) else "!! ", rel,
                                   ("%d B / %s" % (b, s[:16])) if b is not None else "缺失"))
    if problems:
        print("-" * 78)
        print("FAIL：%d 个问题" % len(problems))
        for p in problems:
            print("  - %s" % p)
        return FAIL

    print("-" * 78)
    print("校验 PASS：%d/%d 个文件的 bytes 与 sha256 全部与清单一致" % (len(rows), len(rows)))

    if args.verify:
        print("--verify：不装载。")
        return PASS

    if not args.out:
        print("提示：加 --out <目录> 才会装载（当前只做了校验）")
        return PASS

    out = Path(args.out)
    pack_problems = pack(man, src, out)
    if pack_problems:
        print("FAIL：装载后自检不过")
        for p in pack_problems:
            print("  - %s" % p)
        return FAIL
    print("已装载 → %s（含 BUNDLE-MANIFEST.json）" % out)
    print("移植后核对：python %s --manifest %s --source <bundle> --verify"
          % (Path(__file__).name, mpath.name))
    return PASS


if __name__ == "__main__":
    raise SystemExit(main())
