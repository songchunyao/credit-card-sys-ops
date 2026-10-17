#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auditrot_gate —— R-29 审计链轮转配置门禁（离线，零网络、零真机）

用途
====
校验 `shared/deploy/logrotate/cc-tools-audit.conf`（安装目标
`/etc/logrotate.d/cc-tools-audit`）是否满足 R-29 第 ② 条的轮转/保留基线，
以及任务书对轮转配置的最小要求。**只读**：不安装、不轮转、不触碰真机。

依据
====
* R-29（权威文本 `tmp/_m3_worklist.md` §L）：
  ①服务只追加、永不自行截断；②轮转/保留交外部，**rename + create**、
  服务不得长期持有 fd，基线 = 单文件 64 MiB 轮转 / 保留 >= 90 天；
  ③④⑤为来源纯净度与评测代纪律（本门禁不管，见 README.md §4）。
* logrotate(8)（man7.org，2026-08-04 抓取的本页版本 3.22.0.37）：
  - `create mode owner group` / `create owner group`：
    「Any of the log file attributes may be omitted, in which case those
     attributes for the new file will use the same values as the original log
     file for the omitted attributes.」
  - `maxsize size`：超过该字节数即轮转，**不**取消时间间隔；
  - `size size`：与时间间隔**互斥**（后写者优先）⇒ 用 `size` 会静默关掉 `daily`；
  - 注释规则：「comments may appear anywhere in the config file as long as the
    first non-whitespace character on the line is a #」⇒ 只有**整行注释**，
    因此本配置里所有注释都独占整行，本解析器也只按此规则剥注释。

三态（与全仓门禁口径一致：**BLOCKED ≠ FAIL**）
=============================================
* `PASS`    exit 0 —— 全部必需不变式成立。
* `FAIL`    exit 1 —— 能建立判定模型，且有必需不变式不成立（配置真的有问题）。
* `BLOCKED` exit 2 —— **无法判定**：配置文件不存在 / 不是普通文件 / 读失败 /
  解码失败 / 解析失败（括号不配对等）⇒ 此时**不**报 FAIL，也不冒充 PASS。

不变式
======
块级（每个 block 各自必须成立，不依赖任何顶层全局指令）：
  block-frequency-min  : 有 `daily` 或 `size` 或 `maxsize` 之一（任务书最低要求）
  block-daily          : 有 `daily`（R-29 基线；缺它就只有尺寸触发）
  block-size-cap       : 有 `maxsize` 且解析值 <= 64 MiB；**不得**出现 `size`
                         （`size` 与时间间隔互斥，会静默关掉 `daily`）
  block-rotate         : 有 `rotate N`，N >= 90
  block-create         : 有 `create`，首个参数是八进制模式，且**不得** group/other 可写
  block-compress / block-delaycompress / block-missingok / block-notifempty
文件级：
  paths-covered        : 三个审计路径**全部**出现在 block 头（按解析结果，不按原始文本）
  paths-known          : 每个 block 头路径都在允许集合内（本文件专用于这三个审计文件）
  paths-unique         : 同一路径不得被两个 block 声明
  no-copytruncate       : **指令空间**内不存在独立 token `copytruncate`
                          （`\\bcopytruncate\\b`；注意 `nocopytruncate` 是**相反**的
                          指令，含该子串但不算命中，见下）
  no-copytruncate-literal : **原始文本**里不存在字面 `copytruncate`
                          （正则 `(?<!no)copytruncate`：注释里出现也算命中，
                          因为 R-29 的措辞是「绝对不得出现」；解释性文字归 README.md）

★ 两条 copytruncate 检查为什么这么写：字面子串检查若不做 `(?<!no)` 豁免，就会把
  合法且**相反**的 `nocopytruncate` 判成违规（假阳性）。本配置两个词都不用，
  所以无论按 token 口径还是按朴素 `grep copytruncate` 口径，命中数都是 0。

用法
====
    python shared/verify/auditrot_gate.py                  # 默认校验仓库内配置
    python shared/verify/auditrot_gate.py --json           # 同上，输出 JSON
    python shared/verify/auditrot_gate.py --config PATH    # 校验指定文件
    python shared/verify/auditrot_gate.py --selftest       # 自检（含负控）
    python shared/verify/auditrot_gate.py --selftest --json

退出码：PASS 0 / FAIL 1 / BLOCKED 2；`--selftest` 全通 0、否则 1
（自检不复用被测文件的退出码）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:  # Windows 控制台默认 GBK：中文输出前先切 UTF-8（沿用全仓门禁做法）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

GATE_NAME = "auditrot_gate"
GATE_VERSION = "1.0.0"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2

VERDICT_EXIT = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "BLOCKED": EXIT_BLOCKED}

DEFAULT_CONFIG = (
    Path(__file__).resolve().parents[1] / "deploy" / "logrotate" / "cc-tools-audit.conf"
)

# R-29 覆盖的三个审计文件（顺序即报告顺序；按解析出的 block 头比对，不做全文搜索）
REQUIRED_PATHS = (
    "/opt/cc-tools-kg/cc_tools_audit.jsonl",
    "/opt/cc-tools-onto/cc_tools_onto_audit.jsonl",
    "/opt/cc-tools-kg/kgt/cc_tools_kgt_audit.jsonl",
)

MIB = 1024 * 1024
MAXSIZE_LIMIT = 64 * MIB          # R-29 基线：单文件 64 MiB 轮转
MIN_ROTATE = 90                   # R-29 基线：保留 >= 90 个世代

# logrotate 的脚本块关键字：其内容是按 /bin/sh 执行的正文，不是指令
SCRIPT_KEYWORDS = ("prerotate", "postrotate", "firstaction", "lastaction", "preremove")

SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 * 1024, "g": 1024 * 1024 * 1024}
SIZE_RE = re.compile(r"^([0-9]+)([kKmMgG]?)$")
MODE_RE = re.compile(r"^0?[0-7]{3,4}$")
FORBIDDEN_TOKEN_RE = re.compile(r"\bcopytruncate\b")
FORBIDDEN_LITERAL_RE = re.compile(r"(?<!no)copytruncate")


class ParseError(Exception):
    """配置无法建成判定模型（⇒ BLOCKED，不是 FAIL）。"""


# ---------------------------------------------------------------------------
# 解析器（logrotate 不是严格 ini：路径 + `{`…`}` 块、可选的 `=`、整行注释、
# 脚本块正文、以及块外的全局指令）
# ---------------------------------------------------------------------------
def tokenize(line: str) -> list[str]:
    """按 logrotate 的取值规则切分：空白与 `=` 都是分隔符，支持 ' " 引号。

    `{` / `}` 单独成 token（路径与 `{` 之间可以没有空格）。
    行内 `#` **不是**注释起始（logrotate 只认整行注释）⇒ 这里保留它，
    由本配置的写作纪律保证不给指令加行尾注释。
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in line:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "{}":
            flush()
            out.append(ch)
        elif ch.isspace() or ch == "=":
            flush()
        else:
            buf.append(ch)
    flush()
    return out


class Block:
    """一个 `path [path...] { ... }` 块。"""

    def __init__(self, paths: list[str], line_no: int) -> None:
        self.paths = paths
        self.line_no = line_no
        self.entries: list[tuple[str, list[str], int]] = []

    def get(self, name: str) -> list[list[str]]:
        return [args for token, args, _ in self.entries if token == name]

    def has(self, name: str) -> bool:
        return any(token == name for token, _, _ in self.entries)

    def names(self) -> list[str]:
        return [token for token, _, _ in self.entries]


class ParsedConfig:
    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.globals: list[tuple[str, list[str], int]] = []

    def global_get(self, name: str) -> list[list[str]]:
        return [args for token, args, _ in self.globals if token == name]


def parse_config(text: str) -> ParsedConfig:
    """把 logrotate 配置解析成结构化模型；无法解析时抛 ParseError。

    解析失败 = 无法建立判定模型 ⇒ 上层判 BLOCKED（**不**判 FAIL）。
    """
    parsed = ParsedConfig()
    current: Block | None = None
    in_script = False
    script_line = 0

    for idx, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):          # logrotate：整行注释
            continue
        if in_script:
            # 脚本正文（prerotate/postrotate/... 到 endscript）：不当作指令
            if stripped == "endscript":
                in_script = False
            continue
        if stripped == "endscript":
            raise ParseError("第 %d 行出现孤立的 endscript（没有对应的脚本起始关键字）" % idx)

        tokens = tokenize(stripped)
        if not tokens:
            continue
        if "}" in tokens:
            if tokens != ["}"]:
                raise ParseError("第 %d 行：`}` 必须单独成行" % idx)
            if current is None:
                raise ParseError("第 %d 行出现多余的 `}`（没有打开的块）" % idx)
            parsed.blocks.append(current)
            current = None
            continue
        if "{" in tokens:
            if tokens[-1] != "{":
                raise ParseError("第 %d 行：`{` 必须是该行最后一个 token" % idx)
            if current is not None:
                raise ParseError("第 %d 行：logrotate 配置不支持嵌套块" % idx)
            header = tokens[:-1]
            if not header:
                raise ParseError("第 %d 行：`{` 前没有路径" % idx)
            current = Block(header, idx)
            continue

        name, args = tokens[0], tokens[1:]
        if name in SCRIPT_KEYWORDS:
            if current is None:
                raise ParseError("第 %d 行：脚本块只能出现在路径块内" % idx)
            in_script = True
            script_line = idx
            continue
        if current is not None:
            current.entries.append((name, args, idx))
        else:
            parsed.globals.append((name, args, idx))

    if in_script:
        raise ParseError("第 %d 行打开的脚本块没有 endscript" % script_line)
    if current is not None:
        raise ParseError("第 %d 行打开的块没有 `}`（花括号不配对）" % current.line_no)
    return parsed


# ---------------------------------------------------------------------------
# 不变式
# ---------------------------------------------------------------------------
def parse_size(token: str) -> int | None:
    m = SIZE_RE.match(token)
    if not m:
        return None
    return int(m.group(1)) * SIZE_UNITS[m.group(2).lower()]


def human_bytes(n: int) -> str:
    if n % MIB == 0:
        return "%d MiB" % (n // MIB)
    return "%d B" % n


def _new_check(check_id: str, scope: str, ok: bool, detail: str) -> dict:
    return {"id": check_id, "scope": scope, "level": "required", "ok": bool(ok), "detail": detail}


def evaluate_blocks(parsed: ParsedConfig) -> list[dict]:
    checks: list[dict] = []
    for block in parsed.blocks:
        scope = "block@%d:%s" % (block.line_no, ",".join(block.paths))

        # ① 轮转触发：任务书最低要求 = daily / size / maxsize 之一
        has_daily = block.has("daily")
        has_size = block.has("size")
        has_maxsize = block.has("maxsize")
        checks.append(_new_check(
            "block-frequency-min", scope, has_daily or has_size or has_maxsize,
            "触发条件 daily=%s size=%s maxsize=%s（至少要有其一）"
            % (has_daily, has_size, has_maxsize)))

        # ② R-29 基线：必须有 daily
        checks.append(_new_check(
            "block-daily", scope, has_daily,
            "必须有 daily（R-29 保留基线按「天」定义；只有尺寸触发不满足）」"
            if not has_daily else "daily 存在"))

        # ③ 单文件 64 MiB 上限：maxsize <= 64M，且不得用 size（与时间间隔互斥）
        size_detail: list[str] = []
        ok_size = True
        if has_size:
            ok_size = False
            size_detail.append("出现了 `size`：logrotate(8) 明确 `size` 与时间间隔互斥"
                               "（后写者优先）⇒ 会静默关掉 daily，禁止使用")
        maxsize_args = block.get("maxsize")
        if not maxsize_args:
            ok_size = False
            size_detail.append("缺少 `maxsize`（单文件 64 MiB 轮转基线无法建立）")
        else:
            for args in maxsize_args:
                if len(args) != 1:
                    ok_size = False
                    size_detail.append("`maxsize` 参数个数应为 1，实得 %r" % (args,))
                    continue
                value = parse_size(args[0])
                if value is None:
                    ok_size = False
                    size_detail.append("`maxsize %s` 无法解析为尺寸" % args[0])
                elif value > MAXSIZE_LIMIT:
                    ok_size = False
                    size_detail.append("`maxsize %s` = %s > 上限 %s"
                                       % (args[0], human_bytes(value), human_bytes(MAXSIZE_LIMIT)))
                else:
                    size_detail.append("maxsize %s = %s <= %s（合格）"
                                       % (args[0], human_bytes(value), human_bytes(MAXSIZE_LIMIT)))
        checks.append(_new_check("block-size-cap", scope, ok_size, "；".join(size_detail)))

        # ④ 保留世代数 >= 90
        rotate_args = block.get("rotate")
        ok_rotate = False
        if not rotate_args:
            detail = "缺少 `rotate`（保留基线无法建立）"
        else:
            detail = "; ".join("rotate=%r" % (a,) for a in rotate_args)
            ok_rotate = True
            for args in rotate_args:
                if len(args) != 1 or not args[0].isdigit():
                    ok_rotate = False
                    detail += " 参数必须是非负整数"
                    continue
                if int(args[0]) < MIN_ROTATE:
                    ok_rotate = False
                    detail += "（%s < %d，不满足「保留 >= %d 天」基线）" % (args[0], MIN_ROTATE, MIN_ROTATE)
        checks.append(_new_check("block-rotate", scope, ok_rotate, detail))

        # ⑤ create 存在，且模式八进制、group/other 不可写；owner/group 可省略
        create_args = block.get("create")
        ok_create = False
        if not create_args:
            detail = "缺少 `create`（rename 后必须由 logrotate 重建空文件）"
        else:
            ok_create = True
            detail_parts = []
            for args in create_args:
                if len(args) not in (1, 3):
                    ok_create = False
                    detail_parts.append("`create` 参数应为 [mode] 或 [mode owner group]，实得 %r" % (args,))
                    continue
                mode_tok = args[0]
                if not MODE_RE.match(mode_tok):
                    ok_create = False
                    detail_parts.append("首个参数 %r 不是八进制模式（必须显式写模式，如 0640）" % mode_tok)
                    continue
                mode = int(mode_tok, 8)
                if mode & 0o022:
                    ok_create = False
                    detail_parts.append("模式 %s 允许 group/other 写（审计文件不得如此）" % mode_tok)
                else:
                    detail_parts.append("create %s（group/other 不可写）%s"
                                        % (mode_tok, "，owner/group 省略=沿用原文件属主" if len(args) == 1 else ""))
            detail = "；".join(detail_parts)
        checks.append(_new_check("block-create", scope, ok_create, detail))

        # ⑥ 其余基线指令逐条检查
        for check_id, directive, why in (
            ("block-compress", "compress", "旧世代必须压缩（保留 >= 90 个世代的磁盘前提）"),
            ("block-delaycompress", "delaycompress", "延后一轮压缩，避免与刚结束的写入竞争"),
            ("block-missingok", "missingok", "审计文件不存在时不得让 logrotate 报错中断"),
            ("block-notifempty", "notifempty", "空文件不轮转（避免产生无内容的空世代）"),
        ):
            present = block.has(directive)
            checks.append(_new_check(
                check_id, scope, present,
                "%s 存在" % directive if present else "缺少 `%s`：%s" % (directive, why)))
    return checks


def evaluate_global(parsed: ParsedConfig, raw_text: str) -> tuple[list[dict], dict]:
    checks: list[dict] = []
    info: dict = {}

    declared: list[str] = []
    for block in parsed.blocks:
        declared.extend(block.paths)

    # ① 三个路径全覆盖（只看解析出的 block 头 —— 注释里提到不算覆盖）
    present = [p for p in REQUIRED_PATHS if p in declared]
    missing = [p for p in REQUIRED_PATHS if p not in declared]
    checks.append(_new_check(
        "paths-covered", "file", not missing,
        "三个审计路径全部出现在 block 头（%d/%d）" % (len(present), len(REQUIRED_PATHS))
        if not missing else "以下审计路径没有被任何 block 覆盖：%s" % (missing,)))

    # ② 每个 block 头路径都必须是本文件允许的集合（本文件专用于这三个审计文件）
    unknown = sorted({p for p in declared if p not in REQUIRED_PATHS})
    checks.append(_new_check(
        "paths-known", "file", not unknown,
        "全部 block 头路径都在允许集合内" if not unknown
        else "出现允许集合外的路径（本文件专用于三个审计文件）：%s" % (unknown,)))

    # ③ 同一路径不得被两个 block 声明
    dupes = sorted({p for p in declared if declared.count(p) > 1})
    checks.append(_new_check(
        "paths-unique", "file", not dupes,
        "无重复声明的路径" if not dupes else "路径被多个 block 重复声明：%s" % (dupes,)))

    # ④ 指令空间里不得出现独立 token copytruncate（nocopytruncate 是相反指令，不算）
    directive_tokens: list[str] = []
    for token, _, _ in parsed.globals:
        directive_tokens.append(token)
    for block in parsed.blocks:
        directive_tokens.extend(block.names())
    token_hits = [t for t in directive_tokens if FORBIDDEN_TOKEN_RE.search(t)]
    checks.append(_new_check(
        "no-copytruncate", "file", not token_hits,
        "指令空间无 `copytruncate` token（R-29 要求 rename+create，禁止就地截断）"
        if not token_hits else "指令空间出现被禁止的 token：%s" % token_hits))

    # ⑤ 原始文本里也不得出现字面 copytruncate（`(?<!no)` 豁免 nocopytruncate）
    literal_hits = FORBIDDEN_LITERAL_RE.findall(raw_text)
    raw_all = raw_text.count("copytruncate")
    info["copytruncate_raw_occurrences"] = raw_all
    info["copytruncate_literal_hits"] = len(literal_hits)
    checks.append(_new_check(
        "no-copytruncate-literal", "file", not literal_hits,
        "原文无字面 `copytruncate`（含注释；总子串出现 %d 次，其中 nocopytruncate 形式 %d 次）"
        % (raw_all, raw_all - len(literal_hits))
        if not literal_hits
        else "原文出现字面 `copytruncate` %d 次（注释里也算，解释性文字请写进 README.md）"
        % len(literal_hits)))
    return checks, info


def evaluate(config_path: Path | None) -> dict:
    """对配置文件求值。永不抛异常：无法判定 ⇒ verdict=BLOCKED。"""
    result: dict = {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "config_path": str(config_path) if config_path is not None else None,
        "verdict": "BLOCKED",
        "exit_code": EXIT_BLOCKED,
        "config_exists": False,
        "config_bytes": None,
        "config_lines": None,
        "config_sha256": None,
        "required_paths": list(REQUIRED_PATHS),
        "blocks": [],
        "checks": [],
        "failed_checks": [],
        "blocked_reason": None,
        "copytruncate_raw_occurrences": None,
        "copytruncate_literal_hits": None,
    }

    if config_path is None:
        result["blocked_reason"] = "未指定配置文件路径"
        return result

    if not config_path.exists():
        result["blocked_reason"] = "配置文件不存在：%s" % config_path
        return result
    if not config_path.is_file():
        result["blocked_reason"] = "配置路径不是普通文件：%s" % config_path
        return result

    try:
        blob = config_path.read_bytes()
    except OSError as exc:
        result["blocked_reason"] = "配置文件读取失败（%s: %s）" % (type(exc).__name__, exc)
        return result

    result["config_exists"] = True
    result["config_bytes"] = len(blob)
    result["config_sha256"] = hashlib.sha256(blob).hexdigest()

    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        result["blocked_reason"] = "配置文件不是合法 UTF-8（%s）" % exc
        return result

    result["config_lines"] = len(text.splitlines())

    try:
        parsed = parse_config(text)
    except ParseError as exc:
        result["checks"] = [_new_check("parse-ok", "file", False, str(exc))]
        result["failed_checks"] = ["parse-ok"]
        result["blocked_reason"] = "解析失败，无法建立判定模型：%s" % exc
        return result

    checks = [_new_check("parse-ok", "file", True,
                         "解析成功：%d 个 block / %d 条块外全局指令"
                         % (len(parsed.blocks), len(parsed.globals)))]
    checks.extend(evaluate_blocks(parsed))
    global_checks, info = evaluate_global(parsed, text)
    checks.extend(global_checks)

    result["checks"] = checks
    result["failed_checks"] = [c["id"] for c in checks if not c["ok"]]
    result["blocks"] = [
        {"paths": b.paths, "line": b.line_no, "directives": b.names()} for b in parsed.blocks
    ]
    result["copytruncate_raw_occurrences"] = info["copytruncate_raw_occurrences"]
    result["copytruncate_literal_hits"] = info["copytruncate_literal_hits"]

    if result["failed_checks"]:
        result["verdict"] = "FAIL"
    else:
        result["verdict"] = "PASS"
    result["exit_code"] = VERDICT_EXIT[result["verdict"]]
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def print_result(result: dict) -> None:
    if result["config_exists"]:
        print("%s %s | 配置 %s" % (GATE_NAME, result["verdict"], result["config_path"]))
        print("  字节数 %s / 行数 %s / sha256 %s"
              % (result["config_bytes"], result["config_lines"], result["config_sha256"]))
        print("  block 数 %d" % len(result["blocks"]))
        for block in result["blocks"]:
            print("    :%-6d %s" % (block["line"], ", ".join(block["paths"])))
    else:
        print("%s %s | 配置 %s" % (GATE_NAME, result["verdict"], result["config_path"]))

    if result["blocked_reason"]:
        print("  BLOCKED：%s" % result["blocked_reason"])
        print("  （BLOCKED ≠ FAIL：这里说的是「无法判定」，不是「配置有问题」）")
        return

    for check in result["checks"]:
        flag = "[ OK ]" if check["ok"] else "[FAIL]"
        print("  %s %-24s %-56s %s" % (flag, check["id"], check["scope"], check["detail"]))
    if result["verdict"] == "PASS":
        print("\nPASS: 轮转配置满足 R-29 第 ② 条基线与任务书最小要求")
    else:
        print("\nFAIL: %d 条必需不变式不成立 → %s" % (len(result["failed_checks"]), result["failed_checks"]))


def dump_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# 自检（含负控）
# ---------------------------------------------------------------------------
def _block_span(text: str, path: str) -> tuple[int, int] | None:
    """返回指定路径 block 覆盖的 [起始字符下标, 结束字符下标)（含其上的注释横幅）。"""
    lines = text.splitlines(keepends=True)
    offsets, acc = [], 0
    for line in lines:
        offsets.append(acc)
        acc += len(line)
    offsets.append(acc)

    start = None
    for i, line in enumerate(lines):
        if line.strip().endswith("{") and path in line:
            start = i
            break
    if start is None:
        return None
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == "}":
            end = j
            break
    if end is None:
        return None
    # 往上吃掉紧邻的注释横幅（本配置的写法：注释 → 空行 → block）
    while start > 0 and lines[start - 1].strip().startswith("#"):
        start -= 1
    return offsets[start], offsets[end + 1]


def _remove_block(text: str, path: str) -> str:
    span = _block_span(text, path)
    if span is None:
        raise AssertionError("负控构造失败：找不到 %s 的 block" % path)
    return text[: span[0]] + text[span[1] :]


def _drop_line(text: str, needle: str, count: int = 1) -> str:
    out, removed = [], 0
    for line in text.splitlines(keepends=True):
        if removed < count and line.strip() == needle:
            removed += 1
            continue
        out.append(line)
    if removed != count:
        raise AssertionError("负控构造失败：期望删掉 %d 行 %r，实际 %d" % (count, needle, removed))
    return "".join(out)


def _append_text(text: str, payload: str) -> str:
    """在文件末尾追加**顶层**内容（不是插进某个块里）。"""
    if not text.endswith("\n"):
        text += "\n"
    return text + payload


def _inject_into_block(text: str, path: str, payload: str) -> str:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip().endswith("{") and path in line:
            for j in range(i, len(lines)):
                if lines[j].strip() == "}":
                    lines.insert(j, payload)
                    return "".join(lines)
    raise AssertionError("负控构造失败：找不到 %s 的 block" % path)


def _replace_in_block(text: str, path: str, old: str, new: str) -> str:
    span = _block_span(text, path)
    if span is None:
        raise AssertionError("负控构造失败：找不到 %s 的 block" % path)
    head, body, tail = text[: span[0]], text[span[0] : span[1]], text[span[1] :]
    if old not in body:
        raise AssertionError("负控构造失败：block 内找不到 %r" % old)
    return head + body.replace(old, new, 1) + tail


def _expect(records: list[dict], name: str, result: dict, want_verdict: str,
            want_failed: tuple[str, ...] = ()) -> None:
    ok = result["verdict"] == want_verdict
    note = ""
    if ok and want_failed:
        missing = [cid for cid in want_failed if cid not in result["failed_checks"]]
        if missing:
            ok = False
            note = "；但失败项里缺少 %s（实得 %s）" % (missing, result["failed_checks"])
        else:
            note = "；命中 %s" % (list(result["failed_checks"]),)
    elif not ok:
        note = "；blocked=%s failed=%s" % (result["blocked_reason"], result["failed_checks"])
    records.append({
        "name": name,
        "expected": "%s(%d)" % (want_verdict, VERDICT_EXIT[want_verdict]),
        "got": "%s(%d)" % (result["verdict"], result["exit_code"]),
        "ok": ok,
        "note": note,
    })
    flag = " OK " if ok else "FAIL"
    print("  [%s] %-34s 期望 %-10s 实得 %-10s%s"
          % (flag, name, records[-1]["expected"], records[-1]["got"], note))


def selftest(default_config: Path) -> dict:
    print("== auditrot_gate --selftest ==")
    print("真实配置：%s" % default_config)
    real_result = evaluate(default_config)
    records: list[dict] = []

    if not default_config.exists():
        print("  [FAIL] 真实配置不存在，自检无法进行：%s" % default_config)
        return {"checks_run": 0, "checks_passed": 0, "controls": [], "ok": False,
                "real_config_result": real_result}

    base = default_config.read_text(encoding="utf-8")
    kg = REQUIRED_PATHS[0]
    onto = REQUIRED_PATHS[1]

    with tempfile.TemporaryDirectory(prefix="auditrot_selftest_") as tmp:
        tmpdir = Path(tmp)
        counter = {"n": 0}

        def write_variant(text: str) -> Path:
            counter["n"] += 1
            p = tmpdir / ("variant_%02d.conf" % counter["n"])
            p.write_text(text, encoding="utf-8", newline="\n")
            return p

        # ---- ① 正控：真实配置必须 PASS ----
        _expect(records, "pos/real-config", real_result, "PASS")

        # ---- ② 负控：人为加 copytruncate ⇒ FAIL ----
        variant = write_variant(_inject_into_block(base, kg, "    copytruncate\n"))
        _expect(records, "neg/copytruncate-inserted", evaluate(variant), "FAIL",
                ("no-copytruncate", "no-copytruncate-literal"))

        # ---- ③ 负控：删掉其中一个审计路径 ⇒ FAIL ----
        variant = write_variant(_remove_block(base, onto))
        _expect(records, "neg/onto-path-removed", evaluate(variant), "FAIL",
                ("paths-covered",))

        # ---- ④ 负控：rotate 30 ⇒ FAIL ----
        variant = write_variant(_replace_in_block(base, kg, "rotate 90", "rotate 30"))
        _expect(records, "neg/rotate-30", evaluate(variant), "FAIL", ("block-rotate",))

        # ---- ⑤ 负控：配置不存在 ⇒ BLOCKED(exit 2) ----
        _expect(records, "neg/config-missing", evaluate(tmpdir / "nope.conf"), "BLOCKED")

        # ---- 附加控制 ----
        variant = write_variant(_drop_line(base, "create 0640"))
        _expect(records, "neg/create-missing", evaluate(variant), "FAIL", ("block-create",))

        variant = write_variant(_drop_line(base, "daily"))
        # 只删掉第一个 block 的 daily；该 block 仍有 maxsize ⇒ block-frequency-min 仍成立，
        # 因此这里的唯一预期失败项就是 block-daily（R-29 按「天」定义的保留基线）。
        _expect(records, "neg/daily-missing", evaluate(variant), "FAIL", ("block-daily",))

        # 负控：一个 block 连频率触发都没有（daily/maxsize/size 全无）⇒ 两条一起命中
        variant = write_variant(
            _drop_line(_replace_in_block(base, kg, "maxsize 64M", ""), "daily"))
        _expect(records, "neg/frequency-min-missing", evaluate(variant), "FAIL",
                ("block-frequency-min", "block-daily"))

        variant = write_variant(_replace_in_block(base, kg, "maxsize 64M", "maxsize 128M"))
        _expect(records, "neg/maxsize-128M", evaluate(variant), "FAIL", ("block-size-cap",))

        variant = write_variant(_replace_in_block(base, kg, "maxsize 64M", "size 64M"))
        _expect(records, "neg/size-instead-of-maxsize", evaluate(variant), "FAIL",
                ("block-size-cap",))

        variant = write_variant(_drop_line(base, "notifempty"))
        _expect(records, "neg/notifempty-missing", evaluate(variant), "FAIL", ("block-notifempty",))

        variant = write_variant(_replace_in_block(base, kg, "create 0640", "create 0666"))
        _expect(records, "neg/create-0666", evaluate(variant), "FAIL", ("block-create",))

        variant = write_variant(_append_text(
            base,
            "\n/var/log/cc-tools-unrelated.log {\n"
            "    daily\n    rotate 90\n    maxsize 64M\n    compress\n"
            "    delaycompress\n    missingok\n    notifempty\n    create 0640\n}\n"))
        _expect(records, "neg/unrelated-path-block", evaluate(variant), "FAIL", ("paths-known",))

        variant = write_variant(_append_text(
            base,
            "\n" + onto + " {\n"
            "    daily\n    rotate 90\n    maxsize 64M\n    compress\n"
            "    delaycompress\n    missingok\n    notifempty\n    create 0640\n}\n"))
        _expect(records, "neg/duplicate-path-block", evaluate(variant), "FAIL", ("paths-unique",))

        # 附加：块内被塞进一个 `{`（logrotate 不支持嵌套块）⇒ 模型建不起来 ⇒ BLOCKED
        variant = write_variant(_inject_into_block(base, kg, "    nested {\n"))
        _expect(records, "neg/nested-block", evaluate(variant), "BLOCKED")

        # 花括号不配对 ⇒ BLOCKED（能读到文件、能解码，但无法建立模型）
        variant = write_variant(base.replace("    create 0640\n}\n", "    create 0640\n", 1))
        _expect(records, "neg/unbalanced-brace", evaluate(variant), "BLOCKED")

        # 正控：块内出现 postrotate/endscript 脚本正文时仍应 PASS（证明正文不被误当指令）
        variant = write_variant(_inject_into_block(
            base, kg, "    postrotate\n        /bin/true\n    endscript\n"))
        _expect(records, "pos/script-body-parsed", evaluate(variant), "PASS")

        # 负控：注释里出现字面 copytruncate 也算命中（R-29「绝对不得出现」）
        variant = write_variant(
            base.replace("# 依据裁决 R-29", "# 本文件绝不使用 copytruncate\n# 依据裁决 R-29", 1))
        _expect(records, "neg/copytruncate-in-comment", evaluate(variant), "FAIL",
                ("no-copytruncate-literal",))

        # 负控：BLOCKED 的攻击面 —— 目录而不是文件（能存在但不能当配置判定）
        _expect(records, "neg/config-is-a-directory", evaluate(tmpdir), "BLOCKED")

    passed = sum(1 for r in records if r["ok"])
    print("\nSELFTEST: %d/%d PASS" % (passed, len(records)))
    if passed != len(records):
        print("失败控制：%s" % [r["name"] for r in records if not r["ok"]])
    payload = {
        "gate": GATE_NAME,
        "version": GATE_VERSION,
        "mode": "selftest",
        "real_config": str(default_config),
        "real_config_verdict": real_result["verdict"],
        "real_config_sha256": real_result["config_sha256"],
        "checks_run": len(records),
        "checks_passed": passed,
        "controls": records,
        "ok": passed == len(records),
    }
    return payload


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="auditrot_gate",
        description="R-29 审计链轮转配置门禁（PASS 0 / FAIL 1 / BLOCKED 2）")
    ap.add_argument("--config", default=None,
                    help="待校验的 logrotate 配置路径（默认 %s）" % DEFAULT_CONFIG)
    ap.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="自检（含负控），不复用被测文件退出码")
    args = ap.parse_args(argv)

    config = Path(args.config) if args.config else DEFAULT_CONFIG

    if args.selftest:
        payload = selftest(config)
        if args.as_json:
            dump_json(payload)
        return EXIT_PASS if payload["ok"] else EXIT_FAIL

    result = evaluate(config)
    if args.as_json:
        dump_json(result)
    else:
        print_result(result)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
