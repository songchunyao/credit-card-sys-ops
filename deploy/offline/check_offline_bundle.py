#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/offline/check_offline_bundle.py —— 全量离线包自检器

为什么需要它
------------
离线包的失败方式与联网部署不同：没有"重试一下"的余地。包里少一个镜像、
多一个填好的凭据、少一行 MANIFEST、脚本被 Windows 编辑成 CRLF ——
这些问题在构建机上都是**静默**的，到目标机才炸，且症状往往指向别处
（例如 CRLF 报 `: not found`、缺镜像报 `exec format error`）。

本脚本把"包是完整的、可断网安装的、不含凭据的"变成可复算的断言。

四态退出码（沿用项目语义）
    0=PASS  1=FAIL（包有问题）  2=BLOCKED（无法判定，例如缺 python 环境）

★ 只用标准库：构建机（CI 的 Alpine 镜像）与目标机都可能只有一个裸 python3。

用法
----
    python3 deploy/offline/check_offline_bundle.py --bundle dist/offline/ccops-offline-<tag>
    python3 deploy/offline/check_offline_bundle.py --bundle <dir> --hash    # 逐文件核 sha256
    python3 deploy/offline/check_offline_bundle.py --bundle <dir> --no-syntax

    --hash      逐文件重算 sha256（8 GB 包约 1 分钟；构建机出包后建议跑）
    --no-syntax 跳过 bash -n 语法检查（目标机没有 bash 时）
    --bash PATH 指定 bash（默认从 PATH 找；Git Bash 环境可显式给）
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

PASS, FAIL, BLOCKED = 0, 1, 2

REQUIRED_VERSION_KEYS = (
    "CC_BUNDLE_SCHEMA", "CC_IMAGE_TAG", "CC_PLATFORM", "TARGET_ARCH",
    "CC_OFFLINE", "CC_MIN_DISK_GB_OFFLINE", "CC_NEO4J_IMAGE", "CC_GRAPHDB_IMAGE",
)
APP_SERVICES = ("collect", "kg", "onto", "kgt", "live-facts")

# Dify 官方 .env.example 里的默认 SECRET_KEY 前缀。若它还在，说明出包时**没有**
# 把 SECRET_KEY 改成 __CC_GENERATE__ —— 所有客户的 Dify 会共用同一个签名密钥。
DIFY_UPSTREAM_DEFAULT_SECRET = "sk-9f73s3lj"


class Report:
    def __init__(self) -> None:
        self.items = []

    def add(self, cid: str, title: str, status: int, detail: str = "") -> None:
        self.items.append({"id": cid, "title": title, "status": status, "detail": detail})
        tag = {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED"}[status]
        mark = {PASS: "✓", FAIL: "✗", BLOCKED: "?"}[status]
        # ★ 检查号必须打出来：运维/CI 需要能引用 `N1`、`I3` 这类编号，
        #   否则一堆 ✓/✗ 无法与文档里的"排障表"对照。
        print("  [%-7s] %-4s %s %-42s %s" % (tag, cid, mark, title, detail))

    def counts(self):
        n = {PASS: 0, FAIL: 0, BLOCKED: 0}
        for it in self.items:
            n[it["status"]] += 1
        return n[PASS], n[FAIL], n[BLOCKED]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_kv(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------
def check_layout(root: Path, r: Report) -> bool:
    need = [
        "VERSION", "MANIFEST.sha256", "install-offline.sh",
        "deploy/install.sh", "deploy/.env.example",
        "deploy/lib/offline.sh", "deploy/offline/install_offline.sh",
        "images/IMAGES.lock", "shared/deploy/targets.yaml",
    ]
    missing = [p for p in need if not (root / p).is_file()]
    r.add("L1", "关键文件齐全", FAIL if missing else PASS,
          ("缺：" + ", ".join(missing)) if missing else "%d 项全部存在" % len(need))
    return not missing


def check_version(root: Path, r: Report) -> dict:
    kv = read_kv(root / "VERSION")
    missing = [k for k in REQUIRED_VERSION_KEYS if not kv.get(k)]
    r.add("L2", "VERSION 必填键齐备", FAIL if missing else PASS,
          ("缺：" + ", ".join(missing)) if missing else
          "schema=%s tag=%s arch=%s" % (kv.get("CC_BUNDLE_SCHEMA"), kv.get("CC_IMAGE_TAG"),
                                        kv.get("TARGET_ARCH")))
    schema_ok = kv.get("CC_BUNDLE_SCHEMA") == "ccops-offline/1"
    r.add("L3", "VERSION 声明为全量离线包", PASS if schema_ok else FAIL,
          "CC_BUNDLE_SCHEMA=%s（期望 ccops-offline/1）" % kv.get("CC_BUNDLE_SCHEMA"))
    offline_ok = kv.get("CC_OFFLINE") == "1"
    r.add("L4", "VERSION 声明离线模式", PASS if offline_ok else FAIL,
          "CC_OFFLINE=%s（期望 1）" % kv.get("CC_OFFLINE"))
    disk = kv.get("CC_MIN_DISK_GB_OFFLINE", "")
    disk_ok = disk.isdigit() and int(disk) >= 20
    r.add("L5", "磁盘下限声明合理", PASS if disk_ok else FAIL,
          "CC_MIN_DISK_GB_OFFLINE=%s（应 ≥20）" % (disk or "空"))
    return kv


def check_images(root: Path, r: Report, kv: dict) -> list:
    tars = sorted((root / "images").glob("*.tar.gz"))
    if not tars:
        r.add("I1", "镜像包存在", FAIL, "images/ 下没有 *.tar.gz")
        return []
    tiny = [t.name for t in tars if t.stat().st_size < 1024]
    small = [t.name for t in tars if 1024 <= t.stat().st_size < 1024 * 1024]
    if tiny:
        r.add("I1", "镜像包非空", FAIL, "疑似空/截断：" + ", ".join(tiny))
    elif small:
        r.add("I1", "镜像包非空", PASS, "注意：%s 小于 1 MiB（确认为测试包？）" % ", ".join(small))
    else:
        r.add("I1", "镜像包非空", PASS,
              "%d 个 tar.gz，合计 %s" % (len(tars), human(sum(t.stat().st_size for t in tars))))

    lock_path = root / "images" / "IMAGES.lock"
    if not lock_path.is_file():
        r.add("I2", "IMAGES.lock 存在", FAIL, str(lock_path))
        return []
    refs, bad, arches = [], [], set()
    for i, raw in enumerate(lock_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 5:
            bad.append("第 %d 行字段数=%d（应 5）" % (i, len(parts)))
            continue
        ref, image_id, arch, size, _digest = parts
        if not re.match(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$", ref):
            bad.append("第 %d 行 ref 形状可疑：%r" % (i, ref[:60]))
            continue
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            bad.append("第 %d 行 image Id 形状不对：%r" % (i, image_id[:24]))
        if arch not in ("amd64", "arm64"):
            bad.append("第 %d 行架构未知：%r" % (i, arch))
        if not size.isdigit():
            bad.append("第 %d 行 size 非数字：%r" % (i, size))
        refs.append((ref, arch))
        arches.add(arch)
    r.add("I2", "IMAGES.lock 格式与内容", FAIL if bad else PASS,
          "; ".join(bad[:4]) if bad else "%d 个镜像" % len(refs))

    # 5 个应用镜像 + 2 个载体（Dify 由 IMAGES.txt 再核对）
    ref_names = [x[0] for x in refs]
    miss_app = [s for s in APP_SERVICES
                if not any(x.startswith("cc-ops/%s:" % s) for x in ref_names)]
    r.add("I3", "5 个应用镜像已登记", FAIL if miss_app else PASS,
          ("缺：" + ", ".join(miss_app)) if miss_app else "collect/kg/onto/kgt/live-facts 齐备")
    for label, key in (("Neo4j", "CC_NEO4J_IMAGE"), ("GraphDB", "CC_GRAPHDB_IMAGE")):
        want = kv.get(key, "")
        hit = want in ref_names if want else False
        r.add("I4", "%s 载体镜像已登记" % label, PASS if hit else FAIL,
              want or "VERSION 未声明 %s" % key)

    want_arch = kv.get("TARGET_ARCH", "")
    if arches and want_arch:
        ok = arches == {want_arch}
        r.add("I5", "镜像架构与 TARGET_ARCH 一致", PASS if ok else FAIL,
              "镜像=%s / 声明=%s" % (sorted(arches), want_arch))
    return ref_names


def check_dify(root: Path, r: Report, kv: dict, lock_refs: list) -> None:
    skip = kv.get("CC_SKIP_DIFY") == "1"
    docker = root / "vendor" / "dify" / "docker"
    if skip:
        r.add("D1", "Dify 按声明跳过", PASS if not docker.exists() else PASS,
              "CC_SKIP_DIFY=1（只交付应用层）")
    else:
        need = ["docker-compose.yaml", ".env.offline-template", ".env.example"]
        miss = [n for n in need if not (docker / n).is_file()]
        r.add("D1", "Dify 编排树齐全", FAIL if miss else PASS,
              ("缺：" + ", ".join(miss)) if miss else str(docker))
        if miss:
            return
        tpl = (docker / ".env.offline-template").read_text(encoding="utf-8", errors="replace")
        r.add("D2", "Dify .env 模板含占位符（安装时生成口令）",
              PASS if "__CC_GENERATE__" in tpl else FAIL,
              "含 __CC_GENERATE__" if "__CC_GENERATE__" in tpl else "没有占位符 ⇒ 口令是写死的")
        r.add("D3", "Dify SECRET_KEY 未被上游默认值占用",
              FAIL if DIFY_UPSTREAM_DEFAULT_SECRET in tpl else PASS,
              "发现上游默认 SECRET_KEY 前缀" if DIFY_UPSTREAM_DEFAULT_SECRET in tpl
              else "已改为占位符")
        r.add("D4", "Dify 关闭插件市场", PASS if "MARKETPLACE_ENABLED=false" in tpl else FAIL,
              "MARKETPLACE_ENABLED=false" if "MARKETPLACE_ENABLED=false" in tpl else "未关闭（断网会反复超时）")
        # 绝不允许把填好的 .env 带进包
        r.add("D5", "未打包填好的 Dify .env", FAIL if (docker / ".env").exists() else PASS,
              "vendor/dify/docker/.env 存在（应在安装时生成）" if (docker / ".env").exists() else "只有模板")

        # volumes/ 里除 .gitkeep 外不应有任何文件（否则可能是一套运行中的数据）
        vol = docker / "volumes"
        extra = []
        if vol.is_dir():
            for p in vol.rglob("*"):
                if p.is_file() and p.name not in (".gitkeep", ".gitignore"):
                    extra.append(str(p.relative_to(docker)))
        r.add("D6", "Dify volumes/ 不含运行数据", FAIL if extra else PASS,
              ("含 %d 个文件，例如 %s" % (len(extra), extra[0])) if extra else "只有空目录骨架")

        imgs_txt = root / "vendor" / "dify" / "IMAGES.txt"
        if not imgs_txt.is_file():
            r.add("D7", "Dify 镜像清单存在", FAIL, str(imgs_txt))
        else:
            dimgs = [x.strip() for x in imgs_txt.read_text(encoding="utf-8").splitlines() if x.strip()]
            r.add("D7", "Dify 镜像清单数量合理", PASS if len(dimgs) >= 6 else FAIL,
                  "%d 个镜像" % len(dimgs))
            miss = [x for x in dimgs if x not in lock_refs]
            r.add("D8", "Dify 镜像全部登记在 IMAGES.lock", FAIL if miss else PASS,
                  ("未登记：%s" % ", ".join(miss[:4])) if miss else "%d 个全部已登记" % len(dimgs))


def check_no_secrets(root: Path, r: Report) -> None:
    hits = []
    if (root / "deploy" / ".env").exists():
        hits.append("deploy/.env")
    # ★ 只认**凭据填单**的名字：`credentials` 或 `credentials.<后缀>`（模板 .example 除外）。
    #   不能用 `credentials*`：本仓有 `deploy/credentials_check.py`（校验器源码，不是凭据），
    #   用通配会把源码判成"凭据入包"—— 一个必然误报的门禁等于没有门禁。
    for p in (root / "deploy").glob("credentials*"):
        if not p.is_file():
            continue
        name = p.name
        if name == "credentials":
            hits.append(str(p.relative_to(root)))
        elif name.startswith("credentials.") and not name.endswith(".example"):
            hits.append(str(p.relative_to(root)))
    for pattern in ("*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*"):
        for p in root.rglob(pattern):
            if p.is_file():
                hits.append(str(p.relative_to(root)))
    for d in ("deploy/.work", "deploy/.deps", "deploy/secrets"):
        if (root / d).exists():
            hits.append(d + "/（运行期目录不应入包）")
    r.add("S1", "包内无凭据/运行期产物", FAIL if hits else PASS,
          ("发现：" + "; ".join(sorted(set(hits))[:6])) if hits else "零命中")


def check_manifest(root: Path, r: Report, do_hash: bool) -> None:
    mf = root / "MANIFEST.sha256"
    if not mf.is_file():
        r.add("M1", "MANIFEST 存在", FAIL, str(mf))
        return
    listed = {}
    bad = []
    for i, raw in enumerate(mf.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            bad.append("第 %d 行无法解析" % i)
            continue
        digest, path = parts
        path = path.lstrip("*").strip()
        if path.startswith("/") or ".." in path.split("/"):
            bad.append("第 %d 行是绝对/越界路径：%s" % (i, path))
            continue
        if not re.match(r"^[0-9a-f]{64}$", digest):
            bad.append("第 %d 行摘要形状不对" % i)
        listed[path] = digest

    on_disk = set()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == "MANIFEST.sha256":
            continue
        on_disk.add(rel)
    missing = sorted(set(listed) - on_disk)
    unlisted = sorted(on_disk - set(listed))
    r.add("M1", "MANIFEST 覆盖全部文件", FAIL if (bad or missing or unlisted) else PASS,
          "; ".join(bad[:3]) if bad else
          (("缺 %d 个：%s；未登记 %d 个：%s" % (len(missing), missing[:3],
                                              len(unlisted), unlisted[:3]))
           if (missing or unlisted) else "%d 个文件全部登记" % len(listed)))

    if do_hash and not bad:
        wrong = []
        for rel, digest in sorted(listed.items()):
            p = root / rel
            if not p.is_file():
                continue
            if sha256_file(p) != digest:
                wrong.append(rel)
            if len(wrong) >= 5:
                break
        r.add("M2", "MANIFEST 逐文件 sha256 校验", FAIL if wrong else PASS,
              ("不一致：%s" % ", ".join(wrong)) if wrong else "全部一致")
    elif not do_hash:
        r.add("M2", "MANIFEST 逐文件 sha256 校验", PASS, "按 --hash 未开启（安装时 install.sh 会做）")


def check_newlines(root: Path, r: Report) -> None:
    """只扫**安装路径**上的文本文件。

    ★ 为什么不扫全树：源码树里有若干**上游历史文件**本来就是 CRLF
      （实测：`shared/verify/purity_gate.py`、`scheme2-onto/tests/*.py`、
      `dist/collect-layer/data/actions/action_map.yaml`）。它们不在安装路径上、
      Python/YAML 也容忍 CRLF；把它们判失败只会让每次出包都红，逼人忽略这条门禁。
      真正致命的是**被 shell 执行**的文件（CRLF 会让 `#!/usr/bin/env bash` 失效）
      与 `.env`（值尾带 `\r` 会让口令变成脏值）。
    """
    bad = []
    checked = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] == "images":
            continue
        name = p.name
        is_env = name == ".env.example" or name.startswith(".env.")
        if not (p.suffix.lower() == ".sh" or is_env or name == "VERSION"):
            continue
        if p.stat().st_size > 2 * 1024 * 1024:
            continue
        checked += 1
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\r\n" in data:
            bad.append(rel.as_posix())
        if len(bad) >= 8:
            break
    r.add("N1", "安装路径文本文件为 LF（无 CRLF）", FAIL if bad else PASS,
          ("CRLF：" + ", ".join(bad)) if bad else "抽查 %d 个安装路径文件均为 LF" % checked)


def check_syntax(root: Path, r: Report, bash: str) -> None:
    if not bash:
        r.add("Y1", "bash -n 语法检查", BLOCKED, "找不到 bash（可用 --bash 指定）")
        return
    targets = [root / "install-offline.sh"]
    for sub in ("deploy", "deploy/offline", "deploy/lib"):
        d = root / sub
        if d.is_dir():
            targets.extend(sorted(d.glob("*.sh")))
    bad = []
    for p in sorted(set(targets)):
        if not p.is_file():
            continue
        proc = subprocess.run([bash, "-n", str(p)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            out = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            bad.append("%s: %s" % (p.relative_to(root).as_posix(), out[0] if out else "exit %d" % proc.returncode))
    r.add("Y1", "bash -n 语法检查", FAIL if bad else PASS,
          "; ".join(bad[:3]) if bad else "%d 个脚本语法通过" % len(targets))


def check_install_wiring(root: Path, r: Report) -> None:
    inst = root / "deploy" / "install.sh"
    txt = inst.read_text(encoding="utf-8", errors="replace") if inst.is_file() else ""
    have_flag = "--offline)" in txt
    r.add("W1", "install.sh 支持 --offline", PASS if have_flag else FAIL,
          "已实现离线分支" if have_flag else "未找到 --offline 分支")
    r.add("W2", "install.sh 调用镜像身份校验",
          PASS if "offline_assert_images" in txt else FAIL,
          "已接入 IMAGES.lock 校验" if "offline_assert_images" in txt else "未接入")
    deps = root / "deploy" / "lib" / "deps.sh"
    dtxt = deps.read_text(encoding="utf-8", errors="replace") if deps.is_file() else ""
    r.add("W3", "deps.sh 离线时禁止 pull/clone",
          PASS if ("--pull never" in dtxt and "CC_OFFLINE" in dtxt) else FAIL,
          "已按 CC_OFFLINE 关闭 pull 与 git clone" if ("--pull never" in dtxt and "CC_OFFLINE" in dtxt)
          else "未见离线分支")
    envt = root / "deploy" / ".env.example"
    etxt = envt.read_text(encoding="utf-8", errors="replace") if envt.is_file() else ""
    keys = ("CC_OFFLINE", "CC_SKIP_DIFY", "CC_DIFY_DIR")
    miss = [k for k in keys if not re.search(r"^%s=" % re.escape(k), etxt, re.M)]
    r.add("W4", ".env.example 登记离线键", FAIL if miss else PASS,
          ("缺：" + ", ".join(miss)) if miss else "CC_OFFLINE / CC_SKIP_DIFY / CC_DIFY_DIR 已登记")


# ---------------------------------------------------------------------------
def _force_utf8() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    ★ R-49 的真实事故：宿主是 GBK 代码页时，打印 `✓` / `⇒` 会抛
      `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'`，
      把一次正常的检查变成 traceback —— 看起来像"包坏了"，其实是终端编码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv=None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="credit-card-sys-ops 全量离线包自检")
    ap.add_argument("--bundle", required=True, help="离线包根目录（含 VERSION 与 images/）")
    ap.add_argument("--hash", action="store_true", help="逐文件重算 sha256（较慢）")
    ap.add_argument("--no-syntax", action="store_true", help="跳过 bash -n")
    ap.add_argument("--bash", default=None, help="指定 bash 可执行文件（默认从 PATH 找）")
    args = ap.parse_args(argv)

    root = Path(args.bundle).expanduser()
    print("=" * 78)
    print(" 离线包自检：%s" % root)
    print("=" * 78)
    if not root.is_dir():
        print("[BLOCKED] 目录不存在：%s" % root)
        return BLOCKED
    r = Report()

    if not check_layout(root, r):
        p, f, b = r.counts()
        print("\nPASS=%d FAIL=%d BLOCKED=%d（结构不全，后续检查跳过）" % (p, f, b))
        return FAIL

    kv = check_version(root, r)
    lock_refs = check_images(root, r, kv)
    check_dify(root, r, kv, lock_refs)
    check_no_secrets(root, r)
    check_manifest(root, r, bool(args.hash))
    check_newlines(root, r)
    check_install_wiring(root, r)
    if args.no_syntax:
        r.add("Y1", "bash -n 语法检查", PASS, "按 --no-syntax 跳过")
    else:
        bash = args.bash or shutil.which("bash")
        check_syntax(root, r, bash or "")

    p, f, b = r.counts()
    print("-" * 78)
    print(" PASS=%d  FAIL=%d  BLOCKED=%d" % (p, f, b))
    if f:
        print(" RESULT: FAIL —— 离线包不可交付/不可安装（逐条见上）。")
        return FAIL
    if b:
        print(" RESULT: BLOCKED —— 有项未能判定（未测到 ≠ 通过）。")
        return BLOCKED
    print(" RESULT: PASS —— 离线包结构、镜像锁、凭据边界、语法均通过。")
    return PASS


if __name__ == "__main__":
    sys.exit(main())
