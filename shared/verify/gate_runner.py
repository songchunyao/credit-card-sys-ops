#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gate_runner.py —— shared/verify 门禁的**聚合 runner**（纯标准库，零第三方依赖）

补 HB7 缺口：shared/verify/ 此前没有聚合 runner —— G24（工具面契约）只在队长临时脚本
tmp/_m3_exit_regress.py 里被覆盖过 36 例，而该临时脚本**不是交付物**。

声明面 = shared/model/gate-registry.yaml（数据，不是代码）。本脚本只做两件事：
  (1) 执行：subprocess 调 [sys.executable, <gate path>] + argv
  (2) 聚合：三态退出码 0=PASS / 1=FAIL / 2=BLOCKED，优先级 **BLOCKED > FAIL > PASS**

用法（全部离线可跑）：
  python shared/verify/gate_runner.py --list
  python shared/verify/gate_runner.py --audit-coverage
  python shared/verify/gate_runner.py --offline
  python shared/verify/gate_runner.py --offline --only ccf_instance_gate,ref_gate
  python shared/verify/gate_runner.py --selftest
  python shared/verify/gate_runner.py --offline --json

退出码（状态表，与 --help 的 epilog 同步）：
  0 = PASS                 全 PASS
  1 = FAIL                 有门禁判 FAIL（且无 BLOCKED、无未评估、无已声明偏离）
  2 = BLOCKED              有门禁超时/判 BLOCKED、契约或参数错误、严格模式下存在 NOT-EVALUATED
  3 = DECLARED-DEVIATION   有门禁返回**已声明**的偏离码（注册表 declared_deviation_exit_codes）
  总码优先级：BLOCKED/未评估(严格) ⇒ 2  >  FAIL ⇒ 1  >  DECLARED-DEVIATION ⇒ 3  >  全 PASS ⇒ 0

★ DECLARED-DEVIATION（第四态）—— 依据 shared/verify/guard_conformance.py 自己的口径：
  :25「CARRIER_SPECIFIC = 该偏离**已被登记为「载体能力边界」」、:31「★ CARRIER_SPECIFIC 绝不
  折算成 PASS：登记是**声明 + 分列**，不是豁免」、:120 EXIT_CARRIER_SPECIFIC = 3、
  :758「RESULT: CARRIER_SPECIFIC（非 PASS 项全部是**已登记**的载体能力边界…）」。
  ⇒ 只有注册表**显式声明** declared_deviation_exit_codes: [3] 的门禁才允许把 3 记为偏离；
    **未声明即按 FAIL（点名原始退出码）**，不给任何门禁兜底。偏离单列计数：既不并入 PASS，
    也不并入 FAIL，并把该门禁自己的 RESULT/汇总行**原样照抄**为证据（不改写其措辞）。

★ 严格模式（默认）：只要有任何一个**已登记**门禁未被评估，总退出码 = 2，并打印
  NOT-EVALUATED 横幅逐条列出。只有显式 --allow-partial 才允许部分运行也 return 0，
  且此时报告标题写成「部分运行（不等于全套通过）」。默认不许部分通过冒充全绿。

★ v1 不 pin 任何门禁文件的 sha256（此刻有并行施工者在改 shared/deploy/smoke/e2e_smoke.py、
  shared/verify/purity_gate.py、shared/verify/m5_judge.py、shared/verify/m5/*.py、
  shared/model/m5-cases.yaml）。注册表 sha256 允许为空 = 只观测不判死；运行后如实打印
  观测到的 sha256。

★ 证据只留 sha256[:12] + 字节数；stdout/stderr 落盘前做凭据脱敏（凭据类内容不写进证据）。

★ 脱敏规则 v2（D43 修复）：只有「凭据键名 + 凭据值形态」才改写；`PASS=23` / `PASS: selftest` /
  `FAIL=0` / `BLOCKED=0` / `EXIT=` / `COUNT` / `RESULT` / `cases=` / `min-refs=` 这类**结论键与
  计数键逐字保留**。旧实现（`SECRET_VALUE_RE`：不区分大小写的 pass/passwd/secret/token/api_key
  加「键名 + 分隔符 + 非空白值」）把 `PASS` 也当成凭据键 ⇒ 证据里的 `PASS=23` 改写成 `PASS=<redacted>`（+8 B），
  证据因此**不是**门禁的逐字输出 = 静默篡改（W46 实测）。

★ 证据自述：每次执行都在证据目录写 `redaction_manifest.json` + `REDACTION.md`，写明本次用的是哪条
  脱敏规则、每个证据文件的字节/LF/sha256 与脱敏命中数（命中 0 ⇒ 该文件是门禁输出的逐字副本）。
  ★ 选择「目录级 manifest」而**不是**给 `.stdout.txt` 加头行：证据本体不加任何字节，才能断言
  「evidence.stdout.txt == 直接运行门禁的 stdout」逐字节相等；加头行会让每个证据文件先天多一行、
  并污染 sha256/字节数对比。

★ 本 runner **不改变**任何既有门禁的严格性：它只消费门禁自己的退出码，不重解释判据。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # pragma: no cover - 极端环境下退化
            pass

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2
EXIT_DECLARED_DEVIATION = 3

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_DECLARED_DEVIATION = "DECLARED-DEVIATION"

# 状态表（--help / 报告 / 文档共用一处，避免口径漂移）
STATE_TABLE = (
    "0=PASS 1=FAIL 2=BLOCKED 3=DECLARED-DEVIATION"
    "（优先级 BLOCKED/未评估(严格) > FAIL > DECLARED-DEVIATION > PASS）"
)

VALID_KINDS = ("gate", "library")
VALID_CLASSES = ("offline", "online")
# 已声明偏离码只允许非三态码（0/1/2 已有固定语义，声明它们属自相矛盾 ⇒ 契约错误）
STATE_CODES = (EXIT_PASS, EXIT_FAIL, EXIT_BLOCKED)
VALID_DEVIATION_CODES = tuple(c for c in range(0, 256) if c not in STATE_CODES)


def declared_codes(entry):
    """读取并归一化 declared_deviation_exit_codes ⇒ set[int]（缺省 = 空集）。
    ★ YAML 里的 [3] 经本文件的行内列表解析器可能拿到字符串 '3'，统一 int() 归一化。"""
    raw = entry.get("declared_deviation_exit_codes")
    if raw is None:
        return set()
    seq = raw if isinstance(raw, list) else [raw]
    out = set()
    for x in seq:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue  # 非法值由 validate_entries 报错，这里只做安全跳过
    return out

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "shared" / "model" / "gate-registry.yaml"
DEFAULT_SCAN_DIR = REPO_ROOT / "shared" / "verify"
DEFAULT_EVIDENCE_DIR = REPO_ROOT / "tmp" / "gate_runner_evidence"
DEFAULT_TIMEOUT = 900.0

BAR = "=" * 78
SUB = "-" * 78

# --------------------------------------------------------------------------
# 证据脱敏层（规则 gate_runner/redact-v2；D43 修复 = 「只紧不松」）
#   旧实现 `SECRET_VALUE_RE = (?i)(pass(?:word|wd)?|secret|token|api[_-]?key)\s*[=:]\s*\S+`
#   ⇒ 把计数/结论键 `PASS=23`、`PASS: selftest` 也当凭据改写（`PASS=23` → `PASS=<redacted>`，
#     +8 B），证据因此不是门禁的逐字输出 = 静默篡改（W46 实测，D43）。
#   新实现三层判据，**缺一不改**：
#     ① 键名黑名单：结论键/计数键（PASS/FAIL/BLOCKED/EXIT/COUNT/RESULT/cases/min-refs…）
#        归一化后精确命中 ⇒ 永不脱敏（黑名单**优先**，凭据白名单不得把它盖回来）；
#     ② 键名白名单：归一化后按整段命中凭据词（password|secret|token|api_key|auth…）；
#     ③ 值形态阈值：去引号后 len>=4（与环境变量实值路径同一门槛）且不是「计数/占位」形态
#        （纯数字、true/false、null/none、n/a、tbd/todo、`...`、`***`、`<...>`、`-`）。
#   掩码固定 `<redacted:KEY len=N>`（键名不是秘密，保留键名便于审计；值一律不进证据）。
# --------------------------------------------------------------------------
REDACTION_RULE_ID = "gate_runner/redact-v2"
REDACTION_RULE_SUMMARY = (
    "只对「凭据键名（password/passwd/secret/token/api_key/access_key/private_key/credential/auth）"
    "+ 凭据值形态（去引号后 len>=4 且非纯数字/布尔/占位符）」的「键=值」片段脱敏，"
    "掩码 <redacted:KEY len=N>；结论键与计数键（PASS/FAIL/BLOCKED/EXIT/COUNT/RESULT/cases/min-refs…）"
    "逐字保留；另有两条独立规则：环境变量实值、URL 内嵌口令")
REDACTION_KEEP_VERBATIM = (
    "PASS= / PASS: / FAIL= / FAIL: / BLOCKED= / BLOCKED: / EXIT(exit=) / COUNT / RESULT / "
    "cases= / min-refs= / WARN= / SKIP= 等结论键与计数键（键名黑名单命中即不改写）")
REDACTION_MANIFEST_NAME = "redaction_manifest.json"
REDACTION_README_NAME = "REDACTION.md"

# 规则①：环境变量实值（真正的凭据主要靠这条；值长度门槛与旧实现一致）
_CRED_ENV_VARS = ("CC_NEO4J_PASSWORD", "CC_GRAPHDB_PASSWORD", "NEO4J_PASSWORD",
                  "CCD_PASSWORD", "CC_TOKEN")
_MIN_SECRET_VALUE_LEN = 4

# 键名黑名单（归一化后精确匹配）—— 结论键/计数键：它们不是凭据，改写它们就是篡改证据
_REDACT_KEY_DENY = frozenset((
    "pass", "passed", "passes", "fail", "failed", "fails",
    "blocked", "block", "skip", "skipped", "warn", "warning", "warnings",
    "ok", "ng", "error", "errors", "err",
    "exit", "exitcode", "exit_code", "code", "returncode", "return_code",
    "result", "results", "summary", "verdict", "status", "state",
    "count", "counts", "total", "totals", "sum",
    "cases", "case", "minrefs", "min_refs", "minreferences", "min_references",
    "deviation", "deviations", "declared_deviation", "declared_deviations",
    "duration", "duration_ms", "timeout", "bytes", "size", "sha", "sha256",
))

# 规则②：键名白名单（归一化后按**整段**匹配）—— 只有这些键名才允许进入脱敏
_CRED_KEY_RE = re.compile(
    r"(?:^|_)(?:pass(?:word|words|wd|phrase|phrases)?|pwd|"
    r"secret|secrets|token|tokens|api_?keys?|apikey|access_?key|secret_?key|"
    r"private_?key|credential|credentials|auth|bearer)(?:_|$)",
    re.I)

# 「密钥名 = 值」候选片段（键名/值都允许被引号包裹；旧实现只认裸键名 + \S+ 值）
_CRED_KV_RE = re.compile(
    r'(?P<quote>["\']?)(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)(?P=quote)\s*(?P<sep>=|:)\s*'
    r'(?P<val>"[^"\n]*"|\'[^\'\n]*\'|\S+)')

# 规则③的值形态负例：计数/布尔/空值/占位符 ⇒ 不像凭据（挡住 `token_count=7` 这类计数键）
_VALUE_NEGATIVE_RE = re.compile(
    r"(?i)^(?:[-+]?\d+(?:\.\d+)?%?|0x[0-9a-f]+|true|false|none|null|nil|yes|no|"
    r"ok|ng|pass|fail|blocked|skip|n/?a|tbd|todo|-+|\.{2,}|\*+|_+|<[^>]*>)$")

URL_CRED_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^/\s:@]+):[^/\s@]+@")


def _key_norm(key):
    """键名归一化：小写 + 非字母数字折叠为 '_'（`CC_NEO4J_PASSWORD` ⇒ `cc_neo4j_password`）。"""
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def _strip_quotes(v):
    if len(v) >= 2 and v[0] in "\"'`" and v[-1] == v[0]:
        return v[1:-1].strip()
    return v


def _looks_like_secret_value(val):
    """值形态判据（v2）：长度达阈值且不是「计数/占位」形态 ⇒ 按凭据处理（fail-closed）。"""
    v = _strip_quotes(val.strip())
    if not v or v.startswith("<redacted"):
        return False                      # 空值 / 已是掩码（重复脱敏必须幂等）
    if len(v) < _MIN_SECRET_VALUE_LEN:
        return False                      # 长度不足 ⇒ 按占位符处理（与 env 路径同一门槛）
    if _VALUE_NEGATIVE_RE.match(v):
        return False                      # 纯数字/布尔/占位符 —— 那是指标，不是凭据
    return True


def _bump(stats, key, n=1):
    """脱敏命中计数（仅在调用方传入 stats 时累加）。"""
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


class RegistryError(Exception):
    """注册表契约错误 ⇒ 调用方统一 exit 2（参数/契约错误，绝不静默）。"""


# --------------------------------------------------------------------------
# 极小 YAML 子集解析器（只吃本仓注册表用的受限形状）
#   顶层:   key: value
#   列表:   entries:\n  - id: X\n    path: Y\n    argv: ["--a", "b"]
# 不支持: 锚点 / 多文档 / 块标量 | / 嵌套 map / 行内 map。遇到一律 raise。
# --------------------------------------------------------------------------
def _parse_scalar(raw):
    s = raw.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        body = s[1:-1]
        if s[0] == '"':
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    if s == "[]":
        return []
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if inner == "":
            return []
        out = []
        for part in inner.split(","):
            v = part.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if v == "":
                raise RegistryError("行内列表含空元素：%r" % raw)
            out.append(v)
        return out
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if any(ch in s for ch in ("{", "}", "|", "&", "*")):
        raise RegistryError("不支持的 YAML 形状：%r" % raw)
    return s


def parse_registry_text(text):
    doc = {"entries": []}
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            raise RegistryError("存在 TAB 缩进（本格式要求空格）：%r" % raw)
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        if s.startswith("- "):
            if indent == 0:
                raise RegistryError("列表项缩进异常：%r" % raw)
            cur = {}
            doc.setdefault("entries", []).append(cur)
            s = s[2:].strip()
            if s == "":
                continue
        elif s == "-":
            raise RegistryError("空列表项：%r" % raw)
        elif indent == 0:
            if ":" not in s:
                raise RegistryError("顶层行缺 ':'：%r" % raw)
            k, v = s.split(":", 1)
            k = k.strip()
            if k == "entries":
                val = v.strip()
                if val not in ("", "[]"):
                    raise RegistryError("entries 必须是块列表")
                cur = None
                continue
            doc[k] = _parse_scalar(v)
            cur = None
            continue
        if cur is None:
            raise RegistryError("缩进层级异常（不在任何列表项内）：%r" % raw)
        if ":" not in s:
            raise RegistryError("列表项行缺 ':'：%r" % raw)
        k, v = s.split(":", 1)
        cur[k.strip()] = _parse_scalar(v)
    return doc


def load_registry(path):
    p = Path(path)
    if not p.exists():
        raise RegistryError("注册表不存在：%s" % p)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise RegistryError("注册表读取失败：%s: %s" % (p, exc))
    doc = parse_registry_text(text)
    entries = doc.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RegistryError("注册表缺少非空 entries 列表：%s" % p)
    return doc, entries


def validate_entries(entries):
    """返回错误串列表；空列表 = 契约合法。任何一条 ⇒ 调用方 exit 2。"""
    problems = []
    seen = {}
    for idx, e in enumerate(entries):
        tag = "entries[%d]" % idx
        if not isinstance(e, dict):
            problems.append("%s 不是映射" % tag)
            continue
        eid = e.get("id")
        if not isinstance(eid, str) or eid.strip() == "":
            problems.append("%s 缺少 id" % tag)
            eid = "<无 id @%d>" % idx
        else:
            eid = eid.strip()
            if eid in seen:
                problems.append("重复 id：%s（已出现在 entries[%d]）" % (eid, seen[eid]))
            else:
                seen[eid] = idx
        kind = e.get("kind")
        if kind not in VALID_KINDS:
            problems.append("%s(%s) kind 非法：%r（须为 gate/library）" % (tag, eid, kind))
        path = e.get("path")
        if not isinstance(path, str) or path.strip() == "":
            problems.append("%s(%s) 缺少 path" % (tag, eid))
        if kind == "gate":
            cls = e.get("class")
            if cls not in VALID_CLASSES:
                problems.append("%s(%s) class 非法：%r（须为 offline/online）" % (tag, eid, cls))
            argv = e.get("argv")
            if argv is None:
                problems.append("%s(%s) 缺少 argv（gate 必须有，可为 []）" % (tag, eid))
            elif not isinstance(argv, list):
                problems.append("%s(%s) argv 不是 list：%r" % (tag, eid, argv))
            else:
                for j, a in enumerate(argv):
                    if not isinstance(a, str):
                        problems.append("%s(%s) argv[%d] 不是字符串：%r" % (tag, eid, j, a))
            # 可选：declared_deviation_exit_codes（第四态声明；缺省 = 空 = 不接纳任何偏离码）
            if "declared_deviation_exit_codes" in e:
                dd = e.get("declared_deviation_exit_codes")
                lst = dd if isinstance(dd, list) else [dd]
                norm = []
                for x in lst:
                    try:
                        norm.append(int(x))
                    except (TypeError, ValueError):
                        problems.append("%s(%s) declared_deviation_exit_codes 含非整数：%r"
                                        % (tag, eid, x))
                if len(set(norm)) != len(norm):
                    problems.append("%s(%s) declared_deviation_exit_codes 含重复：%r" % (tag, eid, dd))
                for c in norm:
                    if c in STATE_CODES:
                        problems.append("%s(%s) declared_deviation_exit_codes 含三态码 %d"
                                        "（0/1/2 语义已固定，声明即自相矛盾）" % (tag, eid, c))
                    elif c not in VALID_DEVIATION_CODES:
                        problems.append("%s(%s) declared_deviation_exit_codes 越界：%d（须 0-255）"
                                        % (tag, eid, c))
        elif kind == "library":
            reason = e.get("reason")
            if not isinstance(reason, str) or reason.strip() == "":
                problems.append("%s(%s) library 缺少具体 reason" % (tag, eid))
            if "declared_deviation_exit_codes" in e:
                problems.append("%s(%s) library 不得声明 declared_deviation_exit_codes"
                                "（library 没有退出码语义）" % (tag, eid))
    return problems


# --------------------------------------------------------------------------
# 覆盖审计
# --------------------------------------------------------------------------
def _norm_path(path, repo_root):
    """统一归一化：相对仓库根解析，返回 resolve() 后的 POSIX 绝对路径串。
    ★ Windows 坑：tempfile 可能给出 8.3 短名（ADMINI~1），必须两侧都走 resolve() 才能对齐。"""
    p = Path(str(path))
    if not p.is_absolute():
        p = repo_root / p
    try:
        return p.resolve().as_posix()
    except OSError:  # pragma: no cover
        return p.as_posix()


def scan_py_files(scan_dir):
    d = Path(scan_dir)
    if not d.is_dir():
        raise RegistryError("扫描目录不存在：%s" % d)
    return sorted(_norm_path(p, REPO_ROOT) for p in d.rglob("*.py"))


def audit_coverage(entries, scan_dir, repo_root):
    """返回 (exit_code, 未登记列表, 登记但缺文件列表, 扫描到的文件列表)"""
    scanned = scan_py_files(scan_dir)
    registered = {}
    missing = []
    for e in entries:
        p = e.get("path")
        key = _norm_path(p, repo_root)
        registered[key] = e.get("id")
        if not Path(key).exists():
            missing.append((e.get("id"), p))
    unregistered = [r for r in scanned if r not in registered]
    code = EXIT_FAIL if (unregistered or missing) else EXIT_PASS
    return code, unregistered, missing, scanned


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def _redact(text, stats=None):
    """落盘前脱敏（规则 = REDACTION_RULE_ID）：凭据类内容不进证据；其余**逐字**保留。

    只改写三类片段，缺一不改（见文件头「证据脱敏层」）：
      ① 环境变量实值（_CRED_ENV_VARS，len>=_MIN_SECRET_VALUE_LEN）⇒ `<redacted:VAR len=N>`
      ② URL 内嵌口令 `scheme://user:pass@` ⇒ `user:<redacted>@`
      ③ 「凭据键名 = 值」且值形态像凭据 ⇒ `<redacted:KEY len=N>`
    ★ 结论键/计数键（PASS=23、PASS: selftest、FAIL=0、BLOCKED=0、EXIT=、COUNT、RESULT、
      cases=、min-refs=…）命中键名黑名单 ⇒ **逐字返回**（旧 SECRET_VALUE_RE 会改写它们 = D43）。
    stats：可选 dict，按规则累加命中次数（用于证据目录的脱敏自述）。
    """
    out = text
    for var in _CRED_ENV_VARS:
        val = os.environ.get(var)
        if val and len(val) >= _MIN_SECRET_VALUE_LEN:
            n = out.count(val)
            if n:
                out = out.replace(val, "<redacted:%s len=%d>" % (var, len(val)))
                _bump(stats, "env_value", n)

    def _url_sub(m):
        _bump(stats, "url_credential")
        return m.group(1) + ":<redacted>@"

    out = URL_CRED_RE.sub(_url_sub, out)

    def _kv_sub(m):
        key, val = m.group("key"), m.group("val")
        if _key_norm(key) in _REDACT_KEY_DENY:        # ① 结论键/计数键：禁止改写（黑名单优先）
            return m.group(0)
        if not _CRED_KEY_RE.search(_key_norm(key)):   # ② 键名不像凭据键：不碰
            return m.group(0)
        if not _looks_like_secret_value(val):         # ③ 值形态不像凭据：不碰
            return m.group(0)
        _bump(stats, "credential_kv")
        head = m.group(0)[:m.start("val") - m.start(0)]
        return "%s<redacted:%s len=%d>" % (head, key, len(_strip_quotes(val)))

    return _CRED_KV_RE.sub(_kv_sub, out)


def _redaction_readme(report):
    """证据目录的人类可读「脱敏自述」（与 redaction_manifest.json 同源）。"""
    lines = [
        "# 证据脱敏自述（由 shared/verify/gate_runner.py 自动生成，勿手改）",
        "",
        "本目录的 `<gate-id>.stdout.txt` / `<gate-id>.stderr.txt` 是门禁输出**落盘前**经脱敏的副本。",
        "",
        "- 规则 id：`%s`" % report["rule_id"],
        "- 规则摘要：%s" % report["rule_summary"],
        "- 逐字保留：%s" % report["kept_verbatim"],
        "- 掩码形态：%s" % report["mask_format"],
        "- 证据本体口径：%s" % report["policy"],
        "- 生成时间（UTC）：%s" % report["generated_at"],
        "- 统计：证据文件 %d 个；脱敏命中 %d 处；逐字文件 %d 个" % (
            report["total_files"], report["total_hits"], report["verbatim_files"]),
        "",
        "| 证据文件 | 字节 | LF | sha256 | 脱敏命中 | 是否逐字 |",
        "| --- | ---: | ---: | --- | ---: | --- |",
    ]
    for name in sorted(report["files"]):
        f = report["files"][name]
        lines.append("| `%s` | %d | %d | `%s` | %d | %s |" % (
            name, f["bytes"], f["lf"], f["sha256"], f["redaction_hits"],
            "是（= 门禁输出逐字副本）" if f["verbatim"] else "**否（见命中数）**"))
    lines.append("")
    return "\n".join(lines) + "\n"


def _write_redaction_manifest(evidence_dir, records):
    """在证据目录写「脱敏自述」：用的是哪条规则 + 每个证据文件是否逐字 + 字节/LF/sha256。

    ★ 不加证据文件头行的理由见文件头：保证 `evidence.stdout.txt` 可与门禁自己的 stdout 逐字节对齐。
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    total_hits = 0
    verbatim_files = 0
    for rec in records:
        red = rec.get("redaction") or {}
        for stream, hits_key in (("stdout", "stdout_hits"), ("stderr", "stderr_hits")):
            name = "%s.%s.txt" % (rec.get("id"), stream)
            p = evidence_dir / name
            if not p.is_file():
                continue
            b = p.read_bytes()
            hits = int(red.get(hits_key) or 0)
            total_hits += hits
            if hits == 0:
                verbatim_files += 1
            files[name] = {
                "gate": rec.get("id"),
                "stream": stream,
                "bytes": len(b),
                "lf": b.count(b"\n"),
                "cr": b.count(b"\r"),
                "sha256": _sha256(b),
                "redaction_hits": hits,
                "verbatim": hits == 0,
                "hits_by_rule": (red.get("hits_by_rule") or {}).get(stream, {}),
            }
    report = {
        "runner": "shared/verify/gate_runner.py",
        "rule_id": REDACTION_RULE_ID,
        "rule_summary": REDACTION_RULE_SUMMARY,
        "kept_verbatim": REDACTION_KEEP_VERBATIM,
        "mask_format": "<redacted:KEY len=N>（KEY = 环境变量名或凭据键名）；URL 口令掩码为 <redacted>",
        "policy": "证据文件本体不加任何头行/尾行：redaction_hits=0 即该文件是门禁输出的逐字副本",
        "applied_before_write": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_files": len(files),
        "total_hits": total_hits,
        "verbatim_files": verbatim_files,
        "files": files,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (evidence_dir / REDACTION_MANIFEST_NAME).write_bytes(payload.encode("utf-8"))
    (evidence_dir / REDACTION_README_NAME).write_bytes(
        _redaction_readme(report).encode("utf-8"))
    return report


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_one(entry, args, repo_root, evidence_dir):
    eid = entry["id"]
    rel = str(entry["path"]).replace("\\", "/")
    target = (repo_root / rel).resolve()
    argv = list(entry.get("argv") or [])
    rec = {
        "id": eid,
        "path": rel,
        "class": entry.get("class"),
        "argv": argv,
        "exit": None,
        "verdict": None,
        "duration_ms": None,
        "stdout_sha256": None,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "timed_out": False,
        "start_error": None,
        "unexpected_exit": False,
        "stderr_first_line": None,
        "file_sha256": None,
        "declared_deviation_exit_codes": sorted(declared_codes(entry)),
        "declared_deviation": False,
        "deviation_line": "",
    }
    if not target.exists() or not target.is_file():
        rec["verdict"] = VERDICT_FAIL
        rec["start_error"] = "脚本不存在：%s" % rel
        return rec
    try:
        rec["file_sha256"] = _sha256(target.read_bytes())
    except Exception as exc:  # noqa: BLE001
        rec["file_sha256"] = None
        rec["start_error"] = "读取脚本失败：%s" % exc

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    cmd = [sys.executable, str(target)] + argv
    t0 = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(repo_root), env=env, capture_output=True,
            timeout=args.timeout, shell=False,
        )
        out_b, err_b, code = proc.stdout or b"", proc.stderr or b"", proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out_b = exc.stdout or b""
        err_b = exc.stderr or b""
        code = None
    except Exception as exc:  # noqa: BLE001 —— 启动失败 ⇒ FAIL（fail-closed）
        rec["verdict"] = VERDICT_FAIL
        rec["start_error"] = "启动失败：%s: %s" % (type(exc).__name__, exc)
        rec["duration_ms"] = int((time.perf_counter() - t0) * 1000)
        return rec
    rec["duration_ms"] = int((time.perf_counter() - t0) * 1000)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_stats, err_stats = {}, {}
    out_txt = _redact(out_b.decode("utf-8", "replace"), out_stats)
    err_txt = _redact(err_b.decode("utf-8", "replace"), err_stats)
    out_bytes = out_txt.encode("utf-8")
    err_bytes = err_txt.encode("utf-8")
    (evidence_dir / ("%s.stdout.txt" % eid)).write_bytes(out_bytes)
    (evidence_dir / ("%s.stderr.txt" % eid)).write_bytes(err_bytes)

    # 证据自述（逐条）：本门禁证据是否逐字 = 脱敏命中 0；规则见证据目录 redaction_manifest.json
    rec["redaction"] = {
        "rule": REDACTION_RULE_ID,
        "stdout_hits": sum(out_stats.values()),
        "stderr_hits": sum(err_stats.values()),
        "hits_by_rule": {"stdout": dict(sorted(out_stats.items())),
                         "stderr": dict(sorted(err_stats.items()))},
        "verbatim": not out_stats and not err_stats,
    }
    rec["stdout_bytes"] = len(out_bytes)
    rec["stderr_bytes"] = len(err_bytes)
    rec["stdout_sha256"] = _sha256(out_bytes)[:12]
    first = err_txt.splitlines()
    rec["stderr_first_line"] = first[0].strip()[:200] if first else ""
    rec["stdout_fail_line"] = ""
    for line in out_txt.splitlines():
        if any(k in line for k in ("FAIL", "ERROR", "✗")):
            rec["stdout_fail_line"] = line.strip()[:200]
            break
    # 第四态证据：照抄该门禁自己的 RESULT/汇总行（不得改写其措辞）
    #   优先级：① 以 "RESULT:" 开头的汇总行（这才是门禁自己的结论行）
    #           ② 含 "RESULT:" 的行
    #           ③ 含 CARRIER_SPECIFIC 的行（例如用例表行，兜底）
    rec["deviation_line"] = ""
    for line in out_txt.splitlines():
        if line.strip().startswith("RESULT:") and "CARRIER_SPECIFIC" in line:
            rec["deviation_line"] = line.strip()[:300]
            break
    if not rec["deviation_line"]:
        for line in out_txt.splitlines():
            if "RESULT:" in line:
                rec["deviation_line"] = line.strip()[:300]
                break
    if not rec["deviation_line"]:
        for line in out_txt.splitlines():
            if "CARRIER_SPECIFIC" in line:
                rec["deviation_line"] = line.strip()[:300]
                break

    if timed_out:
        rec["exit"] = None
        rec["timed_out"] = True
        rec["verdict"] = VERDICT_BLOCKED  # 超时 ⇒ 没测完 ≠ 失败
        return rec

    rec["exit"] = code
    if code == 0:
        rec["verdict"] = VERDICT_PASS
    elif code == 1:
        rec["verdict"] = VERDICT_FAIL
    elif code == 2:
        rec["verdict"] = VERDICT_BLOCKED
    elif code in declared_codes(entry):
        # 第四态：注册表**显式声明**的偏离码 ⇒ DECLARED-DEVIATION
        # （登记 = 声明 + 分列，不是豁免；既不折 PASS，也不折 FAIL）
        rec["verdict"] = VERDICT_DECLARED_DEVIATION
        rec["declared_deviation"] = True
    else:
        # 其它任何退出码（含负数、>2、未声明的 3）⇒ FAIL，且必须点名原始退出码与 stderr 首行
        rec["verdict"] = VERDICT_FAIL
        rec["unexpected_exit"] = True
    return rec


def _print_registry_table(entries):
    print(BAR)
    print("gate-registry · 声明面清单（%d 条；--list 只打印，不执行）" % len(entries))
    print(BAR)
    print("%-22s %-7s %-8s %-42s %s" % ("id", "kind", "class", "path", "g_ids[/dev=已声明偏离码]"))
    print(SUB)
    for e in entries:
        gids = ",".join(e.get("g_ids") or []) if isinstance(e.get("g_ids"), list) else ""
        dcodes = ""
        if e.get("kind") == "gate" and sorted(declared_codes(e)):
            dcodes = "  dev=%s" % ",".join(str(c) for c in sorted(declared_codes(e)))
        print("%-22s %-7s %-8s %-42s %s%s" % (
            str(e.get("id")), str(e.get("kind")), str(e.get("class") or "-"),
            str(e.get("path")), gids, dcodes))
    print(BAR)
    ng = sum(1 for e in entries if e.get("kind") == "gate")
    nl = sum(1 for e in entries if e.get("kind") == "library")
    n_off = sum(1 for e in entries if e.get("kind") == "gate" and e.get("class") == "offline")
    n_on = sum(1 for e in entries if e.get("kind") == "gate" and e.get("class") == "online")
    print("gate=%d（offline=%d / online=%d）  library=%d  合计=%d" % (ng, n_off, n_on, nl, len(entries)))
    print(BAR)


def _select(entries, args):
    # ★ 严格模式的分母 = **全部已登记 gate**（不是 --only/--offline 过滤后的集合）：
    #   这样 --only 与 --offline 都不能悄悄把「没测的门禁」变成绿。
    univ = [e for e in entries if e.get("kind") == "gate"]
    selected = univ
    if args.only:
        only = []
        for x in args.only.split(","):
            x = x.strip()
            if x and x not in only:
                only.append(x)
        known = {e["id"] for e in entries}
        missing = [x for x in only if x not in known]
        if missing:
            print("参数错误：--only 指定了注册表里不存在的 id：%s" % ", ".join(missing), file=sys.stderr)
            print("注册表已知 id：%s" % ", ".join(sorted(known)), file=sys.stderr)
            print("⇒ exit 2（绝不静默忽略）", file=sys.stderr)
            return None, None, None
        selected = [e for e in univ if e["id"] in only]
    if args.offline:
        todo = [e for e in selected if e.get("class") == "offline"]
    else:
        todo = list(selected)
    done_ids = {e["id"] for e in todo}
    not_evaluated = [e for e in univ if e["id"] not in done_ids]
    return todo, univ, not_evaluated


def _aggregate(records, not_evaluated, args):
    any_blocked = any(r["verdict"] == VERDICT_BLOCKED for r in records)
    any_fail = any(r["verdict"] == VERDICT_FAIL for r in records)
    any_declared = any(r["verdict"] == VERDICT_DECLARED_DEVIATION for r in records)
    holes = len(not_evaluated) > 0 and not args.allow_partial
    # 优先级（与 guard_conformance.py:1023 的 0/1/2/3 口径一致）：
    #   任一 BLOCKED/未评估(严格) ⇒ 2  >  任一 FAIL ⇒ 1  >  任一 DECLARED-DEVIATION ⇒ 3  >  全 PASS ⇒ 0
    if any_blocked or holes:
        total = EXIT_BLOCKED
    elif any_fail:
        total = EXIT_FAIL
    elif any_declared:
        total = EXIT_DECLARED_DEVIATION
    else:
        total = EXIT_PASS
    label = {
        EXIT_PASS: VERDICT_PASS,
        EXIT_FAIL: VERDICT_FAIL,
        EXIT_BLOCKED: VERDICT_BLOCKED,
        EXIT_DECLARED_DEVIATION: VERDICT_DECLARED_DEVIATION,
    }[total]
    return total, label


def execute(entries, args, repo_root):
    todo, univ, not_evaluated = _select(entries, args)
    if todo is None:
        return EXIT_BLOCKED
    partial = bool(not_evaluated) and bool(args.allow_partial)
    evidence_dir = Path(args.evidence_dir)
    if not evidence_dir.is_absolute():
        evidence_dir = (repo_root / evidence_dir).resolve()

    title = "部分运行（不等于全套通过）" if partial else "严格模式"
    print(BAR)
    print("gate_runner · 门禁聚合执行报告 —— %s" % title)
    print(BAR)
    print("registry  : %s（entries=%d）" % (args.registry, len(entries)))
    print("repo root : %s" % repo_root)
    print("evidence  : %s" % evidence_dir)
    mode = "--offline（只跑 class=offline）" if args.offline else "全量（含 class=online）"
    if args.only:
        mode += " · --only=%s" % args.only
    if args.allow_partial:
        mode += " · --allow-partial"
    print("mode      : %s" % mode)
    print("select    : 待跑 %d 条 / 已登记 gate %d 条 / 未评估 %d 条" % (len(todo), len(univ), len(not_evaluated)))
    print(SUB)

    records = []
    for e in todo:
        started = time.perf_counter()
        rec = run_one(e, args, repo_root, evidence_dir)
        records.append(rec)
        if not args.quiet:
            print("[%-19s] %-22s exit=%-5s %6sms  out=%dB sha=%s" % (
                rec["verdict"], rec["id"], "None" if rec["exit"] is None else rec["exit"],
                rec["duration_ms"] if rec["duration_ms"] is not None else -1,
                rec["stdout_bytes"], rec["stdout_sha256"]))
            print("            path=%s  class=%s" % (rec["path"], rec["class"]))
            if rec["argv"]:
                print("            argv=%s" % json.dumps(rec["argv"], ensure_ascii=False))
            if rec["file_sha256"]:
                print("            file_sha256=%s（v1 只观测，不 pin）" % rec["file_sha256"])
            else:
                print("            file_sha256=<读取失败>")
            if rec["timed_out"]:
                print("            ★ 超时 > %ss ⇒ BLOCKED（没测完 ≠ 失败）" % args.timeout)
            if rec["verdict"] == VERDICT_DECLARED_DEVIATION:
                print("            ★ 已声明偏离退出码 %s ∈ declared_deviation_exit_codes=%s"
                      " ⇒ DECLARED-DEVIATION"
                      % (rec["exit"],
                         json.dumps(rec.get("declared_deviation_exit_codes") or [],
                                    ensure_ascii=False)))
                print("              登记 = 声明 + 分列，不是豁免：既不折 PASS，也不折 FAIL。")
                print("              该门禁自述证据（原样照抄，未改写其措辞）：%s"
                      % (rec["deviation_line"] or "<未捕获到 RESULT/汇总行>"))
            if rec["unexpected_exit"]:
                print("            ★ 非预期退出码 %s（只允许 0/1/2 或该门禁已声明的偏离码）⇒ FAIL；"
                      "stderr 首行：%s" % (rec["exit"], rec["stderr_first_line"] or "<空>"))
            if rec["start_error"]:
                print("            ★ %s ⇒ FAIL" % rec["start_error"])
            if rec["verdict"] == VERDICT_FAIL and rec["exit"] == 1:
                if rec["stderr_first_line"]:
                    print("            stderr 首行：%s" % rec["stderr_first_line"])
                if rec["stdout_fail_line"]:
                    print("            stdout 首个 FAIL/ERROR 行：%s" % rec["stdout_fail_line"])
            if rec["verdict"] == VERDICT_FAIL and rec["exit"] is None and rec["stdout_fail_line"]:
                print("            stdout 首个 FAIL/ERROR 行：%s" % rec["stdout_fail_line"])
    print(SUB)

    # 证据自述（目录级）：写 redaction_manifest.json + REDACTION.md，并打印一行摘要
    redaction_report = _write_redaction_manifest(evidence_dir, records)
    print("redaction : %s（规则 %s；命中 %d 处；逐字证据 %d/%d 文件）" % (
        evidence_dir / REDACTION_MANIFEST_NAME, REDACTION_RULE_ID,
        redaction_report["total_hits"], redaction_report["verbatim_files"],
        redaction_report["total_files"]))

    if not_evaluated:
        print("NOT-EVALUATED（未评估 %d 条；BLOCKED > FAIL > PASS ⇒ 未测即不得算绿）：" % len(not_evaluated))
        for e in not_evaluated:
            print("  - %-22s class=%-8s %s" % (e["id"], str(e.get("class")), e.get("path")))
        if args.allow_partial:
            print("  ★ --allow-partial 已显式启用 ⇒ 允许部分运行也 return 0；")
            print("    但本次结论**仅覆盖已评估集合**，不等于全套通过。")
        else:
            print("  ★ 严格模式未启用 --allow-partial ⇒ 总退出码 = 2。")
        print(SUB)

    total, label = _aggregate(records, not_evaluated, args)
    print("汇总：PASS=%d FAIL=%d BLOCKED=%d DECLARED-DEVIATION=%d 未评估=%d" % (
        sum(1 for r in records if r["verdict"] == VERDICT_PASS),
        sum(1 for r in records if r["verdict"] == VERDICT_FAIL),
        sum(1 for r in records if r["verdict"] == VERDICT_BLOCKED),
        sum(1 for r in records if r["verdict"] == VERDICT_DECLARED_DEVIATION),
        len(not_evaluated)))
    print("RESULT: %s" % label)
    print("状态表：%s" % STATE_TABLE)
    print("        0=PASS 1=FAIL 2=BLOCKED（BLOCKED > FAIL > PASS）"
          "；3=DECLARED-DEVIATION（已声明的载体能力边界，单列：不并入 PASS、不并入 FAIL）")
    print("总退出码: %d" % total)
    print(BAR)

    if args.json:
        manifest = {
            "runner": "shared/verify/gate_runner.py",
            "registry": str(args.registry),
            "repo_root": str(repo_root),
            "mode": {
                "offline": bool(args.offline),
                "only": args.only,
                "allow_partial": bool(args.allow_partial),
                "timeout_s": args.timeout,
                "title": title,
            },
            "gates": records,
            "not_evaluated": [
                {"id": e["id"], "path": e["path"], "class": e.get("class")} for e in not_evaluated
            ],
            "counts": {
                "evaluated": len(records),
                "universe": len(univ),
                "pass": sum(1 for r in records if r["verdict"] == VERDICT_PASS),
                "fail": sum(1 for r in records if r["verdict"] == VERDICT_FAIL),
                "blocked": sum(1 for r in records if r["verdict"] == VERDICT_BLOCKED),
                "declared_deviation": sum(
                    1 for r in records if r["verdict"] == VERDICT_DECLARED_DEVIATION),
            },
            "state_table": STATE_TABLE,
            "total_exit": total,
            "result": label,
            # 证据脱敏自述（D43）：本次用的规则 + 每个证据文件是否逐字 + 命中数
            "redaction": redaction_report,
        }
        mf = evidence_dir / "run_manifest.json"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
        print("run_manifest: %s" % mf)
    return total


# --------------------------------------------------------------------------
# 自检（离线；正负控都有；不碰真实门禁）
# --------------------------------------------------------------------------
FAKE = {
    "fake_pass.py": "import sys\nsys.stdout.write('fake-gate PASS\\n')\nsys.exit(0)\n",
    "fake_fail.py": "import sys\nsys.stderr.write('fake-gate FAIL first-stderr-line\\n')\nsys.exit(1)\n",
    "fake_blocked.py": "import sys\nsys.stdout.write('fake-gate BLOCKED\\n')\nsys.exit(2)\n",
    "fake_weird.py": "import sys\nsys.stderr.write('fake-gate unexpected-code-7 first line\\n')\nsys.exit(7)\n",
    "fake_sleep.py": "import sys, time\nsys.stdout.write('sleeping\\n')\nsys.stdout.flush()\ntime.sleep(30)\nsys.exit(0)\n",
    "fake_online_pass.py": "import sys\nsys.stdout.write('fake-online PASS\\n')\nsys.exit(0)\n",
    "fake_carrier.py": ("import sys\n"
                        "sys.stdout.write('RESULT: CARRIER_SPECIFIC（非 PASS 项全部是已登记的载体能力边界）\\n')\n"
                        "sys.exit(3)\n"),
    "fake_declared_seven.py": ("import sys\n"
                               "sys.stderr.write('fake-declared-but-7\\n')\n"
                               "sys.exit(7)\n"),
    "new_unregistered_gate.py": "import sys\nsys.exit(0)\n",
    # ↓ D43 夹具：只打印计数/结论键（旧 _redact() 会把 PASS=23 改写成 PASS=<redacted>）
    "fake_counts.py": ("import sys\n"
                       "sys.stdout.write('PASS=23 FAIL=0 BLOCKED=0\\n')\n"
                       "sys.stdout.write('PASS: selftest\\n')\n"
                       "sys.stdout.write('RESULT: PASS (PASS=23 WARN=0 min-refs=20 cases=0)\\n')\n"
                       "sys.exit(0)\n"),
    # ↓ D43 夹具：真凭据形态 + 计数键同行/同文件（证明「脱敏真凭据」与「逐字保留计数键」并存）
    "fake_leak.py": ("import sys\n"
                     "sys.stdout.write('CC_NEO4J_PASSWORD=sup3rS3cr3tValue\\n')\n"
                     "sys.stdout.write('PASS=23\\n')\n"
                     "sys.stderr.write('api_key = \"sk-live-9f8e7d6c5b4a\"\\n')\n"
                     "sys.exit(0)\n"),
}


def _emit_registry(entries, path: Path):
    lines = ["schema: gate-registry/v1", "generated_by: shared/verify/gate_runner.py", "entries:"]
    keys = ("path", "kind", "class", "argv", "expect_exit", "g_ids",
            "declared_deviation_exit_codes", "evidence", "sha256", "reason", "note")
    for e in entries:
        lines.append("  - id: %s" % e["id"])
        for k in keys:
            if k not in e:
                continue
            v = e[k]
            if isinstance(v, list):
                parts = []
                for x in v:
                    # ★ 整数元素不引号化（否则回归 YAML 后变字符串，声明码比对会失配）
                    parts.append(str(x) if isinstance(x, int) else '"%s"' % str(x))
                lines.append("    %s: [%s]" % (k, ", ".join(parts)))
            elif isinstance(v, bool):
                lines.append("    %s: %s" % (k, "true" if v else "false"))
            elif isinstance(v, int):
                lines.append("    %s: %d" % (k, v))
            else:
                lines.append('    %s: "%s"' % (k, str(v).replace('"', "'")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _runner_call(reg, extra, evidence_dir):
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--registry", str(reg), "--evidence-dir", str(evidence_dir)] + extra
    p = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, timeout=900)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def selftest(argv=None):
    checks = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))
        print("  [%s] %s%s" % ("OK  " if ok else "NG  ", name, ("  —— " + detail) if detail and not ok else ""))

    print(BAR)
    print("gate_runner --selftest（离线；临时目录生成假门禁，不碰真实门禁）")
    print(BAR)

    root = Path(tempfile.mkdtemp(prefix="gate_runner_selftest_"))
    try:
        gdir = root / "gates"
        gdir.mkdir(parents=True, exist_ok=True)
        for name, src in FAKE.items():
            (gdir / name).write_text(src, encoding="utf-8", newline="\n")
        scan = root / "scandir"
        scan.mkdir(parents=True, exist_ok=True)
        for name in ("fake_pass.py", "fake_fail.py", "fake_blocked.py", "fake_weird.py",
                     "fake_sleep.py", "fake_online_pass.py", "new_unregistered_gate.py"):
            (scan / name).write_bytes((gdir / name).read_bytes())

        def gate(fname, eid, cls="offline", argv=None, kind="gate", where=None, declared=None):
            e = {"id": eid, "path": ((where or gdir) / fname).as_posix(), "kind": kind,
                 "class": cls, "argv": argv if argv is not None else []}
            if kind == "library":
                e["reason"] = "selftest 夹具：无 CLI"
            if declared is not None:
                e["declared_deviation_exit_codes"] = declared
            return e

        ev = root / "ev"

        # ① 全 PASS ⇒ 0
        r1 = root / "r1.yaml"
        _emit_registry([gate("fake_pass.py", "p1"), gate("fake_pass.py", "p2")], r1)
        c, o, e = _runner_call(r1, [], ev)
        check("① 全 PASS ⇒ 0", c == 0 and "RESULT: PASS" in o, "exit=%s" % c)

        # ② 一个 FAIL ⇒ 1
        r2 = root / "r2.yaml"
        _emit_registry([gate("fake_pass.py", "p1"), gate("fake_fail.py", "f1")], r2)
        c, o, e = _runner_call(r2, [], ev)
        check("② 一个 FAIL ⇒ 1", c == 1 and "RESULT: FAIL" in o, "exit=%s" % c)

        # ③ 一个 BLOCKED ⇒ 2
        r3 = root / "r3.yaml"
        _emit_registry([gate("fake_pass.py", "p1"), gate("fake_blocked.py", "b1")], r3)
        c, o, e = _runner_call(r3, [], ev)
        check("③ 一个 BLOCKED ⇒ 2", c == 2 and "RESULT: BLOCKED" in o, "exit=%s" % c)

        # ④ BLOCKED 与 FAIL 同时存在 ⇒ 2（优先级）
        r4 = root / "r4.yaml"
        _emit_registry([gate("fake_fail.py", "f1"), gate("fake_blocked.py", "b1")], r4)
        c, o, e = _runner_call(r4, [], ev)
        check("④ BLOCKED+FAIL ⇒ 2（BLOCKED > FAIL）", c == 2 and "RESULT: BLOCKED" in o,
              "exit=%s" % c)

        # ⑤ 非预期退出码 7 ⇒ FAIL 且点名原始退出码 + stderr 首行
        r5 = root / "r5.yaml"
        _emit_registry([gate("fake_weird.py", "w1")], r5)
        c, o, e = _runner_call(r5, [], ev)
        check("⑤ 退出码 7 ⇒ FAIL(1) 且点名原码与 stderr 首行",
              c == 1 and "非预期退出码 7" in o and "unexpected-code-7 first line" in o,
              "exit=%s" % c)

        # ⑥ --only 不存在的 id ⇒ 2
        c, o, e = _runner_call(r2, ["--only", "no_such_gate"], ev)
        check("⑥ --only 不存在 id ⇒ 2", c == 2, "exit=%s" % c)

        # ⑦ 注册表缺 path / 重复 id / 非法 class / argv 非 list ⇒ 2
        bad1 = root / "bad_missing_path.yaml"
        bad1.write_text('schema: gate-registry/v1\nentries:\n  - id: x\n    kind: gate\n'
                        '    class: offline\n    argv: []\n', encoding="utf-8")
        c1, _, _ = _runner_call(bad1, [], ev)
        bad2 = root / "bad_dup.yaml"
        _emit_registry([gate("fake_pass.py", "dup"), gate("fake_pass.py", "dup")], bad2)
        c2, _, _ = _runner_call(bad2, [], ev)
        bad3 = root / "bad_class.yaml"
        bad3.write_text('schema: gate-registry/v1\nentries:\n  - id: y\n    path: %s\n'
                        '    kind: gate\n    class: online2\n    argv: []\n' % (gdir / "fake_pass.py").as_posix(),
                        encoding="utf-8")
        c3, _, _ = _runner_call(bad3, [], ev)
        bad4 = root / "bad_argv.yaml"
        bad4.write_text('schema: gate-registry/v1\nentries:\n  - id: z\n    path: %s\n'
                        '    kind: gate\n    class: offline\n    argv: "--a"\n' % (gdir / "fake_pass.py").as_posix(),
                        encoding="utf-8")
        c4, _, _ = _runner_call(bad4, [], ev)
        check("⑦ 注册表缺 path/重复 id/非法 class/argv 非 list ⇒ 2",
              c1 == 2 and c2 == 2 and c3 == 2 and c4 == 2,
              "exits=%s/%s/%s/%s" % (c1, c2, c3, c4))

        # ⑧ 超时 ⇒ BLOCKED（不是 FAIL）
        r8 = root / "r8.yaml"
        _emit_registry([gate("fake_sleep.py", "s1")], r8)
        c, o, e = _runner_call(r8, ["--timeout", "1"], ev)
        check("⑧ 超时 ⇒ BLOCKED(2)（没测完 ≠ 失败）",
              c == 2 and "RESULT: BLOCKED" in o and "超时" in o, "exit=%s" % c)

        # ⑨ 未登记的新 .py ⇒ --audit-coverage exit 1（负控）
        reg_cover = root / "cover.yaml"
        _emit_registry([gate("fake_pass.py", "p1", where=scan)], reg_cover)
        c, o, e = _runner_call(reg_cover, ["--audit-coverage", "--scan-dir", str(scan)], ev)
        check("⑨ 未登记新 .py ⇒ --audit-coverage 1（且点名文件）",
              c == 1 and "new_unregistered_gate.py" in o, "exit=%s" % c)

        # ⑩ 全登记 ⇒ --audit-coverage 0（正控）
        reg_all = root / "cover_all.yaml"
        _emit_registry([gate(n, n.replace(".py", ""), where=scan) for n in
                        ("fake_pass.py", "fake_fail.py", "fake_blocked.py", "fake_weird.py",
                         "fake_sleep.py", "fake_online_pass.py", "new_unregistered_gate.py")],
                       reg_all)
        c, o, e = _runner_call(reg_all, ["--audit-coverage", "--scan-dir", str(scan)], ev)
        check("⑩ 全登记 ⇒ --audit-coverage 0（正控）", c == 0, "exit=%s" % c)

        # ⑪ 登记但文件不存在 ⇒ --audit-coverage 1（fail-closed 负控）
        reg_gone = root / "cover_gone.yaml"
        _emit_registry([gate(n, n.replace(".py", ""), where=scan) for n in
                        ("fake_pass.py", "fake_fail.py", "fake_blocked.py", "fake_weird.py",
                         "fake_sleep.py", "fake_online_pass.py", "new_unregistered_gate.py")]
                       + [gate("gone.py", "gone")],
                       reg_gone)
        c, o, e = _runner_call(reg_gone, ["--audit-coverage", "--scan-dir", str(scan)], ev)
        check("⑪ 登记项文件不存在 ⇒ 1（且点名 gone.py）",
              c == 1 and "gone.py" in o and "未登记的 .py" not in o, "exit=%s" % c)

        # ⑫ --offline 有 online 未评估 ⇒ 默认 2 且打 NOT-EVALUATED
        r12 = root / "r12.yaml"
        _emit_registry([gate("fake_pass.py", "p1", "offline"),
                        gate("fake_online_pass.py", "on1", "online")], r12)
        c, o, e = _runner_call(r12, ["--offline"], ev)
        check("⑫ --offline 有 online 未评估 ⇒ 2 + NOT-EVALUATED（防假绿负控）",
              c == 2 and "NOT-EVALUATED" in o and "--allow-partial" in o, "exit=%s" % c)

        # ⑬ --offline --allow-partial ⇒ 0 且标题为「部分运行（不等于全套通过）」
        c, o, e = _runner_call(r12, ["--offline", "--allow-partial"], ev)
        check("⑬ --allow-partial ⇒ 0 且报告标题含「部分运行」",
              c == 0 and "部分运行（不等于全套通过）" in o and "NOT-EVALUATED" in o, "exit=%s" % c)

        # ⑭ --json 生成 run_manifest.json（只记 sha 前缀与长度）
        c, o, e = _runner_call(r2, ["--json"], ev)
        mf = ev / "run_manifest.json"
        ok = c == 1 and mf.exists()
        detail = "exit=%s" % c
        if ok:
            man = json.loads(mf.read_text(encoding="utf-8"))
            g = man["gates"][0]
            ok = (man["total_exit"] == 1 and "stdout_sha256" in g and "stdout_bytes" in g
                  and len(g["stdout_sha256"]) == 12 and isinstance(man["not_evaluated"], list))
            detail = "manifest 字段/长度不合规"
        check("⑭ --json 产出 run_manifest.json（sha 前缀 12 + bytes + 列表）", ok, detail)

        # ⑮ --only 命中 ⇒ 只跑该条
        c, o, e = _runner_call(r2, ["--only", "p1"], ev)
        check("⑮ --only p1 ⇒ 只跑 1 条且余项进 NOT-EVALUATED（严格 ⇒ 2）",
              c == 2 and "待跑 1 条" in o and "f1" in o, "exit=%s" % c)

        # ⑯ library 条目不执行、只登记
        r16 = root / "r16.yaml"
        lib = {"id": "lib1", "path": (gdir / "fake_pass.py").as_posix(), "kind": "library",
               "reason": "selftest 夹具：被别的脚本 import，无 argparse"}
        _emit_registry([lib, gate("fake_pass.py", "p1")], r16)
        c, o, e = _runner_call(r16, ["--list"], ev)
        check("⑯ library 只登记不执行（--list 显示 gate=1 library=1）",
              c == 0 and "gate=1" in o and "library=1" in o, "exit=%s" % c)

        # ⑰ 正控：已声明 declared_deviation_exit_codes: [3] + 实际 exit 3 ⇒ DECLARED-DEVIATION，总码 3
        r17 = root / "r17.yaml"
        _emit_registry([gate("fake_carrier.py", "d1", declared=[3])], r17)
        c, o, e = _runner_call(r17, [], ev)
        check("⑰ 已声明 [3] + exit 3 ⇒ DECLARED-DEVIATION、总码 3、单列不并入 PASS/FAIL、照抄门禁自述行",
              c == 3 and "RESULT: DECLARED-DEVIATION" in o and "DECLARED-DEVIATION=1" in o
              and "PASS=0" in o and "FAIL=0" in o and "CARRIER_SPECIFIC" in o,
              "exit=%s" % c)

        # ⑱ 正控：已声明 [3] 实际 3，但同时另有 FAIL ⇒ 总码 1（FAIL 优先于 DECLARED-DEVIATION）
        r18 = root / "r18.yaml"
        _emit_registry([gate("fake_carrier.py", "d1", declared=[3]),
                        gate("fake_fail.py", "f1")], r18)
        c, o, e = _runner_call(r18, [], ev)
        check("⑱ 已声明 [3] 另有 FAIL ⇒ 总码 1（FAIL > DECLARED-DEVIATION）且偏离仍单列",
              c == 1 and "RESULT: FAIL" in o and "DECLARED-DEVIATION=1" in o, "exit=%s" % c)

        # ⑲ 负控：未声明却返回 3 ⇒ 仍按 FAIL（点名原码 3），总码 1
        r19 = root / "r19.yaml"
        _emit_registry([gate("fake_carrier.py", "d2")], r19)
        c, o, e = _runner_call(r19, [], ev)
        check("⑲ 未声明 3 ⇒ FAIL(1) 且点名原码 3（不给任何门禁兜底）",
              c == 1 and "非预期退出码 3" in o and "RESULT: FAIL" in o, "exit=%s" % c)

        # ⑳ 负控：已声明 [3] 但实际返回 7 ⇒ FAIL(1)（声明只接纳自己声明的码）
        r20 = root / "r20.yaml"
        _emit_registry([gate("fake_declared_seven.py", "d3", declared=[3])], r20)
        c, o, e = _runner_call(r20, [], ev)
        check("⑳ 已声明 [3] 但返回 7 ⇒ FAIL(1) 且点名原码 7",
              c == 1 and "非预期退出码 7" in o, "exit=%s" % c)

        # ㉑ 负控：declared_deviation_exit_codes 声明三态码 / 非整数 ⇒ 注册表契约错误 exit 2
        bad5 = root / "bad_declared.yaml"
        bad5.write_text('schema: gate-registry/v1\nentries:\n  - id: dd\n    path: %s\n'
                        '    kind: gate\n    class: offline\n    argv: []\n'
                        '    declared_deviation_exit_codes: [1]\n'
                        % (gdir / "fake_pass.py").as_posix(), encoding="utf-8")
        c5, _, _ = _runner_call(bad5, [], ev)
        bad6 = root / "bad_declared2.yaml"
        bad6.write_text('schema: gate-registry/v1\nentries:\n  - id: dd2\n    path: %s\n'
                        '    kind: gate\n    class: offline\n    argv: []\n'
                        '    declared_deviation_exit_codes: ["abc"]\n'
                        % (gdir / "fake_pass.py").as_posix(), encoding="utf-8")
        c6, _, _ = _runner_call(bad6, [], ev)
        check("㉑ declared 含三态码 1 / 非整数 ⇒ 契约错误 exit 2（负控）",
              c5 == 2 and c6 == 2, "exits=%s/%s" % (c5, c6))

        # ---------------------------------------------------------------
        # D43 控制项（只增不减）：脱敏不得改写结论键/计数键，但真凭据仍须脱敏
        # ---------------------------------------------------------------
        # ㉒ 正控：结论键/计数键经 _redact() 后**逐字不变**
        kept = [
            "PASS=23",
            "PASS: selftest",
            "FAIL=0 BLOCKED=0",
            "RESULT: BLOCKED  (FAIL=0 BLOCKED=1 WARN=0 PASS=29 min-refs=20 cases=0)",
            "exit=2",
            "EXIT: 2",
            "COUNT=5",
            "cases=0",
            "min-refs=20",
            "汇总：PASS=23 FAIL=0 BLOCKED=0 DECLARED-DEVIATION=0 未评估=25",
        ]
        changed = [s for s in kept if _redact(s) != s]
        check("㉒ 结论键/计数键逐字保留（PASS=23 / PASS: selftest / FAIL=0 BLOCKED=0 / RESULT / "
              "exit= / EXIT: / COUNT= / cases= / min-refs=）",
              not changed, "被改写：%s" % changed)

        # ㉓ 负控：结论键/计数键上**不得**出现掩码
        masked = [s for s in kept if "<redacted" in _redact(s)]
        check("㉓ 结论键/计数键不得出现掩码（\"<redacted\" not in _redact(x)）",
              not masked, "出现掩码：%s" % masked)

        # ㉔ 正控：真凭据形态仍被脱敏（键名白名单 + 值形态；修缺陷 ≠ 删脱敏断言）
        secrets = [
            ("CC_NEO4J_PASSWORD=sup3rS3cr3tValue", "sup3rS3cr3tValue"),
            ("NEO4J_AUTH=neo4j/abcdef123456", "abcdef123456"),
            ('api_key = "sk-live-9f8e7d6c5b4a"', "sk-live-9f8e7d6c5b4a"),
            ("password: hunter2", "hunter2"),
            ("CC_TOKEN=abc123def456", "abc123def456"),
            ("cc_graphdb_password = A1b2C3d4E5", "A1b2C3d4E5"),
            ("secret=qwerty", "qwerty"),
            ('"password": "P@ssw0rd-2026"', "P@ssw0rd-2026"),
        ]
        leaks = []
        masks = []
        for src, val in secrets:
            r = _redact(src)
            if val in r or "<redacted" not in r:
                leaks.append("%s ⇒ %s" % (src, r))
            else:
                masks.append(r)
        check("㉔ 真凭据仍被脱敏（%d 例：全部无原文残留且出现掩码）" % len(secrets),
              not leaks, "泄漏/未脱敏：%s" % leaks)

        # ㉔b 掩码格式固定（这样才能对「脱敏后的形态」下精确断言，而不只是「非空」）
        check("㉔b 掩码格式固定为 <redacted:KEY len=N>（示例 %s）" % (masks[0] if masks else "-"),
              bool(masks) and masks[0] == "CC_NEO4J_PASSWORD=<redacted:CC_NEO4J_PASSWORD len=16>"
              and masks[2] == "api_key = <redacted:api_key len=20>",
              "actual=%s" % masks[:3])

        # ㉕ 环境变量实值路径仍脱敏，且同一行的结论键仍逐字
        _saved_pw = os.environ.get("CC_NEO4J_PASSWORD")
        r_env = ""
        os.environ["CC_NEO4J_PASSWORD"] = "EnvSecret12345"
        try:
            r_env = _redact("PASS=23  CC_NEO4J_PASSWORD=EnvSecret12345")
        finally:
            if _saved_pw is None:
                os.environ.pop("CC_NEO4J_PASSWORD", None)
            else:
                os.environ["CC_NEO4J_PASSWORD"] = _saved_pw
        check("㉕ env 实值路径（CC_NEO4J_PASSWORD）仍脱敏 + 同行 PASS=23 逐字保留",
              "EnvSecret12345" not in r_env
              and "<redacted:CC_NEO4J_PASSWORD len=14>" in r_env
              and "PASS=23" in r_env, "⇒ %s" % r_env)

        # ㉖ URL 内嵌口令仍脱敏
        r_url = _redact("connect neo4j://svcuser:s3cr3tP4ss@127.0.0.1:7687 ok")
        check("㉖ URL 内嵌口令仍脱敏（scheme://user:pass@ ⇒ user:<redacted>@）",
              "s3cr3tP4ss" not in r_url and "svcuser:<redacted>@" in r_url, "⇒ %s" % r_url)

        # ㉗ 端到端：假门禁只打印计数/结论键 ⇒ 落盘证据**逐字**，且证据目录自述脱敏规则
        r27 = root / "r27.yaml"
        _emit_registry([gate("fake_counts.py", "c1")], r27)
        ev27 = root / "ev27"
        c, o, e = _runner_call(r27, ["--json"], ev27)
        so27 = ev27 / "c1.stdout.txt"
        rm27 = ev27 / REDACTION_MANIFEST_NAME
        ok27 = (c == 0 and so27.is_file() and rm27.is_file()
                and (ev27 / REDACTION_README_NAME).is_file())
        detail27 = "exit=%s files=%s" % (
            c, sorted(p.name for p in ev27.glob("*")) if ev27.is_dir() else [])
        if ok27:
            body = so27.read_bytes().decode("utf-8")
            man = json.loads(rm27.read_text(encoding="utf-8"))
            ent = (man.get("files") or {}).get("c1.stdout.txt") or {}
            ok27 = ("PASS=23" in body and "PASS: selftest" in body
                    and "PASS=<redacted>" not in body and "<redacted" not in body
                    and man.get("rule_id") == REDACTION_RULE_ID
                    and ent.get("verbatim") is True and ent.get("redaction_hits") == 0)
            detail27 = "证据=%r manifest_entry=%r" % (body[:70], ent)
        check("㉗ 端到端逐字：假门禁 PASS=23/PASS: selftest 的证据逐字 + 目录自述脱敏规则", ok27, detail27)

        # ㉘ 端到端脱敏：假门禁真打印凭据 ⇒ 证据里凭据消失、PASS=23 逐字、manifest 如实记 hits
        r28 = root / "r28.yaml"
        _emit_registry([gate("fake_leak.py", "l1")], r28)
        ev28 = root / "ev28"
        c, o, e = _runner_call(r28, ["--json"], ev28)
        so28 = ev28 / "l1.stdout.txt"
        se28 = ev28 / "l1.stderr.txt"
        rm28 = ev28 / REDACTION_MANIFEST_NAME
        ok28 = c == 0 and so28.is_file() and se28.is_file() and rm28.is_file()
        detail28 = "exit=%s" % c
        if ok28:
            ob = so28.read_bytes().decode("utf-8")
            eb = se28.read_bytes().decode("utf-8")
            man = json.loads(rm28.read_text(encoding="utf-8"))
            f_o = man["files"]["l1.stdout.txt"]
            f_e = man["files"]["l1.stderr.txt"]
            ok28 = ("sup3rS3cr3tValue" not in ob and "PASS=23" in ob and "<redacted" in ob
                    and "sk-live-9f8e7d6c5b4a" not in eb and "<redacted" in eb
                    and f_o["verbatim"] is False and f_o["redaction_hits"] >= 1
                    and f_e["verbatim"] is False and f_e["redaction_hits"] >= 1)
            detail28 = "out=%r err=%r" % (ob[:70], eb[:70])
        check("㉘ 端到端脱敏：真凭据消失 + PASS=23 保留 + manifest 如实记 hits/verbatim",
              ok28, detail28)

    finally:
        shutil.rmtree(root, ignore_errors=True)

    total = len(checks)
    ok_n = sum(1 for _, ok, _ in checks if ok)
    print(SUB)
    for name, ok, detail in checks:
        if not ok:
            print("FAILED: %s  %s" % (name, detail))
    print("checks=%d/%d" % (ok_n, total))
    print("RESULT: %s" % ("PASS" if ok_n == total else "FAIL"))
    print(BAR)
    return EXIT_PASS if ok_n == total else EXIT_FAIL


# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="gate_runner.py",
        description="shared/verify 门禁聚合 runner（声明面 shared/model/gate-registry.yaml；"
                    "纯标准库；状态表 " + STATE_TABLE + "）",
        epilog="状态表（聚合语义，优先级从高到低）：\n"
               "  2 = BLOCKED             门禁超时/判 BLOCKED；契约或参数错误；严格模式下存在未评估门禁\n"
               "  1 = FAIL                有门禁判 FAIL（含非预期退出码与未声明的偏离码）\n"
               "  3 = DECLARED-DEVIATION  有门禁返回注册表**已声明**的偏离码"
               "（declared_deviation_exit_codes）；单列，不并入 PASS、不并入 FAIL\n"
               "  0 = PASS                全 PASS\n"
               "默认严格模式：任何已登记门禁未被评估 ⇒ 总退出码 2；--allow-partial 显式放开。\n"
               "★ 未声明即 FAIL：门禁返回 3 但注册表未声明 [3] ⇒ 按 FAIL 处理并点名原始退出码。",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY), help="注册表路径")
    ap.add_argument("--scan-dir", default=str(DEFAULT_SCAN_DIR),
                    help="--audit-coverage 扫描目录（默认 shared/verify）")
    ap.add_argument("--evidence-dir", default=str(DEFAULT_EVIDENCE_DIR), help="证据输出目录")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="单门禁超时秒数（默认 900；超时 ⇒ BLOCKED）")
    ap.add_argument("--offline", action="store_true", help="只跑 class=offline 的门禁")
    ap.add_argument("--allow-partial", action="store_true",
                    help="显式允许部分运行也 return 0（报告标题会写明「部分运行」）")
    ap.add_argument("--only", default=None, help="只跑这些 id（逗号分隔）；不存在 ⇒ exit 2")
    ap.add_argument("--list", action="store_true", help="只打印注册表，不执行")
    ap.add_argument("--json", action="store_true", help="额外产出 run_manifest.json")
    ap.add_argument("--audit-coverage", action="store_true",
                    help="列出未登记的 .py（有 ⇒ exit 1）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（正负控）")
    ap.add_argument("--quiet", action="store_true", help="不打印逐门禁明细")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.selftest:
        return selftest(argv)

    try:
        doc, entries = load_registry(args.registry)
        problems = validate_entries(entries)
        if problems:
            print("注册表契约错误（%d 条）⇒ exit 2：" % len(problems), file=sys.stderr)
            for p in problems:
                print("  - %s" % p, file=sys.stderr)
            return EXIT_BLOCKED
    except RegistryError as exc:
        print("注册表错误 ⇒ exit 2：%s" % exc, file=sys.stderr)
        return EXIT_BLOCKED

    if args.timeout <= 0:
        print("参数错误：--timeout 必须 > 0 ⇒ exit 2", file=sys.stderr)
        return EXIT_BLOCKED

    if args.list:
        _print_registry_table(entries)
        return EXIT_PASS

    if args.audit_coverage:
        try:
            code, unreg, missing, scanned = audit_coverage(entries, args.scan_dir, REPO_ROOT)
        except RegistryError as exc:
            print("覆盖审计错误 ⇒ exit 2：%s" % exc, file=sys.stderr)
            return EXIT_BLOCKED
        print(BAR)
        print("gate_runner --audit-coverage · 覆盖完整性审计（fail-closed）")
        print(BAR)
        print("扫描目录 : %s" % args.scan_dir)
        print("扫描命中 : %d 个 .py" % len(scanned))
        print("登记条目 : %d（gate=%d library=%d）" % (
            len(entries), sum(1 for x in entries if x.get("kind") == "gate"),
            sum(1 for x in entries if x.get("kind") == "library")))
        if unreg:
            print(SUB)
            print("★ 未登记的 .py（%d）—— 新门禁加入而未登记 ⇒ FAIL：" % len(unreg))
            for r in unreg:
                print("  - %s" % r)
        if missing:
            print(SUB)
            print("★ 已登记但文件不存在（%d）⇒ FAIL：" % len(missing))
            for eid, p in missing:
                print("  - %s  %s" % (eid, p))
        print(SUB)
        print("RESULT: %s" % ("PASS" if code == EXIT_PASS else "FAIL"))
        print("退出码: %d" % code)
        print(BAR)
        return code

    try:
        return execute(entries, args, REPO_ROOT)
    except RegistryError as exc:
        print("运行错误 ⇒ exit 2：%s" % exc, file=sys.stderr)
        return EXIT_BLOCKED
    except KeyboardInterrupt:  # pragma: no cover
        print("中断 ⇒ exit 2（未测完 ≠ 通过）", file=sys.stderr)
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
