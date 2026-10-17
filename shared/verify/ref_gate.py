#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""引用可解析性门禁 G20（M2.13 新增）：跨文件「路径[:行号]」引用必须真的可解析。

为什么要有这道门禁：
  M1.8a 交付的 shared/model/zero-instance-ledger.yaml 里 consumers / evidence 引用成片失真：
  文件名用了**旧编号体系**（`docs/02-方案2设计（本体+LLM）.md` 实际是 `docs/04-方案2设计（本体+LLM）.md`）、
  行号与主张**语义错配**（`docs/03-方案1设计（知识图谱+LLM）.md:209` 指向空行）、
  被引文件长大后行号整体漂移（`shared/verify/contract_gates.py:49` 实为 `:59`）。
  这类失真**不会被任何既有门禁发现**：引用写在一个文件里、目标在另一个文件里，
  既不是 schema 校验（G3/G4/G5/G7/X 家族），也不是跨契约词表校验（G18），
  更不是零值登记校验（G16）——G16 只看台账「有没有登记」，不看台账里的引用能不能落到东西上。
  G20 把「引用可解析性」变成可机检、可回归的一条硬约束。

扫描对象：
  · <root>/shared/model/*.yaml    （必扫：契约与台账都住在这里）
  · <root>/shared/mapping/*.yaml  （扫；判定规则与 model 完全同一套，报告里标 source=shared/mapping/*）
  只读文本，不做 YAML 解析 ⇒ 纯标准库、不依赖 PyYAML、不依赖真机。

引用语法：<路径>[:<行>[,<行>][-<行>]]，路径后缀 ∈
  .py .md .yaml .yml .json .cypher .ttl .rq .sh .java

判定规则（逐条可复现）：
  ① 只有「以本仓库现有顶层条目开头」的相对路径才被校验（docs/xx.md、shared/model/x.yaml:12）。
     首段不是本仓库顶层条目的相对路径（`tools/neo4j_query.yaml`、`service/probes/container_probe.py`、
     `absorb-wt/x.md`）⇒ 视为「其他仓库 / 真机目录 / 已删除的顶层目录」⇒ **WARN 并逐条列出**
     （附「仓库内同名文件」提示），不判错：本仓库根本没有那个顶层目录，它不构成对**本仓库**
     文件系统的断言。代价（诚实声明）：形如 `docs2/x.md` 的「顶层目录名拼错」也落进这档 ⇒ 只告警。
     本仓库确实有的顶层目录名写错文件名（`docs/02-方案2设计（本体+LLM）.md`）⇒ 仍然 ERROR，这正是本次要抓的缺陷。
  ② **跳过**绝对路径（盘符路径、以 `/` 开头）与 URL（`http(s)://`、`ftp://`）——它们指向
     外部环境（真机 / OS / 第三方仓库），不在本仓库可解析范围内 ⇒ 只计数并在明细里列出，不判错。
  ③ `tmp/` 开头的目标**缺失** ⇒ WARN（tmp/ 里的产物本来就可清理）；目标存在则照常校验行号。
  ④ 其余路径不存在 ⇒ ERROR。
  ⑤ 给了行号：越界（> 文件行数）⇒ ERROR；命中**空行**或**纯分隔行**（连续 3 个及以上的
     - = * ~）⇒ ERROR——这正是 `docs/03:209` 那类「行号漂移到空白处」的典型形态。
  ⑥ 同一条引用里的多个行号（逗号分隔 / 单行 / 区间）逐项校验，任一命中问题即报；
     ★ M2.24 起单行与区间的判定强度**刻意不同**（有理由的定向放宽，只放宽「区间内部」）：
       · 单行 `N`：与 M2.13 **完全一致** —— 越界 / 命中空行 / 命中纯分隔行 ⇒ ERROR（锚点强度不变）。
       · 区间 `A-B`：**只断言行首锚点 A** 真实、非空、非纯分隔行 ⇒ 区间内部 (A+1..B) 的空行
         **一律不报错**。理由：契约「逐字引用一整节散文」时，那一节必然含 Markdown 结构性空行
         （实证 = docs/00-总体设计与裁决记录.md:326-341 即 `### 6.3 等价性自检（必做）` 整节，
         内部 :327/:329/:340 都是空行）⇒ 旧语义逐行判空在**物理上禁止**任何契约引用一整节，
         这是门禁过严，不是契约写错。
       · 区间内非空行数 = 0 ⇒ ERROR（由「锚点必须非空」蕴含，代码里仍**显式断言**一次）。
       · 区间跨度 B-A+1 > `RANGE_SPAN_WARN`（40）⇒ **WARN**（不是 ERROR）：长区间是弱引用，
         建议改引小节标题行 + §号。
       · 代价（诚实声明）：区间**内部**的行号漂移从此不再被捕获 —— 引用精度下降，
         因此锚点判别力与长区间 WARN 必须保留、且必须可被触发。
  ⑦ 裸文件名（不含 `/`，如散文里的 `ccf.schema.json`）**不判错**：无法唯一确定它指的是哪个目录下的
     同名文件 ⇒ 单独列在「未覆盖」明细里（这是本门禁明确的覆盖边界，见 --list）。
  ⑧ 反恒真（不许「检查了 0 条」当通过）：实际检查到的引用条数 < --min-refs（默认 40）⇒ ERROR。
     并把「扫描文件数 / 外部跳过条数 / 裸文件名条数 / WARN 条数 / ERROR 条数 / 行号校验条数」全部打印。
  ⑨ KNOWN_DEBT（已知欠账，report-only）：**只登记「文件由他人持有、本次硬边界禁止改」的既有坏引用**，
     每条在代码里写明持有者与理由，运行时逐条打印成 [G20/DEBT]，并计入 RESULT 行的 DEBT=n —— 
     既不静默放行、也不让 G20 一落地就永远红着失效。`--strict` 忽略该清单（欠账清零后应改用它）。

已知覆盖边界（诚实声明，勿误读为「全绿=引用全对」）：
  G20 只能发现「路径不存在 / 行号越界 / 行号落在空行或分隔行」。它**无法**发现
  ★ M2.24 放宽还多一条覆盖边界：**区间引用只判行首锚点 A**，区间内部（空行 / 纯分隔行 /
  漂移到另一处的内容）一律放行 ⇒「引用一整节」合法化的**代价是引用精度下降**：长区间只能靠
  WARN 提示、不判错，区间内部的行号漂移静默通过。锚点行本身漂移到另一条非空行时，G20 也照样抓不到。
  「行号指向了一个存在且非空的、但与该条主张无关的行」（语义错配）——那类只能靠人工逐条读目标文档。
  本文件同样不覆盖：散文里的裸文件名（规则⑦）、注释里脱离路径的裸行号（如 `v1.2.0 的 :736`）、
  非 .yaml 文件（.py / .md / .json）里的引用。
  另有一条**刻意的分级**：首段不是本仓库顶层条目的相对路径按规则①降为 WARN（列出 + 同名文件提示），
  故 `docs2/x.md` 这类「顶层目录名拼错」不会被判错。
  还有一条**已知欠账**分级（规则⑨）：他人持有的文件里的既有坏引用进 KNOWN_DEBT，report-only，
  但仍逐条打印、并计入 RESULT 的 DEBT=n；`--strict` 一律按 ERROR 处理。

退出码：0 = PASS；1 = ERROR。
用法: python shared/verify/ref_gate.py [--root DIR] [--model-dir DIR] [--mapping-dir DIR]
                                     [--min-refs N] [--list]
  --root / --model-dir / --mapping-dir 仅供判别力自证在不改动真契约的前提下跑突变副本：
  同一份代码、同一套断言，只换读取根，不放宽任何检查（与 contract_gates.py 的 --model-dir 同一约定）。
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(_HERE))          # shared/verify/x.py -> 仓库根

REF_SUFFIXES = ("py", "md", "yaml", "yml", "json", "cypher", "ttl", "rq", "sh", "java")
_EXT_RE = re.compile(r"\.(?:" + "|".join(REF_SUFFIXES) + r")(?![A-Za-z0-9_])")
_LINE_SPEC_RE = re.compile(r":(\d+(?:\s*[-,]\s*\d+)*)")
_SEP_RE = re.compile(r"^(?:-{3,}|={3,}|\*{3,}|~{3,})$")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_EXCLUDE_WALK_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv", "node_modules"}
_LEADSEG_RE = re.compile(r"^[A-Za-z0-9_.\-]+[/\\]")

# ---- ⑥ 区间引用跨度阈值（M2.24 放宽区间语义时新增的唯一常量）--------------------------
# 区间跨度 B-A+1 超过它 ⇒ WARN（弱引用告警，**不是** ERROR）。取 40 的依据：本仓库真在用的最长
# 区间引用是 docs/00-总体设计与裁决记录.md:326-341（16 行 = 一整节「### 6.3 等价性自检（必做）」）；
# 跨度再放大到 > 40 行时，「行首锚点」已不足以定位读者要读的那一段，引用退化成「大致在这一带」。
RANGE_SPAN_WARN = 40

# ---- ⑨ 已知欠账（report-only）-------------------------------------------------
# 只登记「文件由他人持有、M2.13 硬边界禁止改」的既有坏引用；键 = (源文件仓库相对路径, 引用路径原文)。
# 每条必须写明持有者 + 为什么本次不能改。运行时逐条打印 [G20/DEBT]，并计入 RESULT 的 DEBT=n。
# 欠账清零后，CI 应改用 --strict（忽略本清单）。
# ★ 受理范围扩展（2026-09-29，队长裁定）：除「他人持有的既有坏引用」外，本清单**同时**接纳
#   「引用**尚未产出的计划产物**」的前向引用 —— 该产物只能由后续里程碑产出，本轮不可能存在。
#   两类都**不静默放行**：逐条打印、计入 DEBT、`--strict` 一律按 ERROR 处理；且每条必须写明
#   **消解条件**（产物产出后本条必须删除，否则 CI 不得宣称欠账已清）。
KNOWN_DEBT = {
    ("shared/model/v3-ccf-map.yaml", "docs/02-方案2设计（本体+LLM）.md"):
        "旧编号体系（真文件 = docs/04-方案2设计（本体+LLM）.md）；v3-ccf-map.yaml 由其他施工者持有，"
        "M2.13 硬边界明确禁止改 ⇒ 本次只报告（出现在该文件 :4 权威依据 / :63 owner_doc）",
}


def _arg(name, default=None):
    if name in sys.argv:
        k = sys.argv.index(name)
        if k + 1 < len(sys.argv):
            return sys.argv[k + 1]
    return default


def _is_path_char(ch):
    """路径字符集：ASCII 字母数字 + _ . / \\ - + 以及中日韩字符与全角括号（文档名里有「（本体+LLM）」）。

    ★ 全角括号必须在字符集里：`docs/04-方案2设计（本体+LLM）.md` 的 `.md` 紧跟在 `）` 之后，
      若 `）` 不算路径字符，向后扫就会在半路停住、把路径截成 `.md`（⇒ 真引用被误判成裸文件名而漏掉）。
      前导的 `（` 即便被吞进 raw，也会被 _root_boundary 用「顶层条目名 + /」切掉，不产生误判。
    """
    if ch in "_.-/\\+":
        return True
    if ch in "\uff08\uff09":          # （ ） 全角括号
        return True
    if ch.isalnum():
        return True
    return False


def _strip_leading_prose(raw):
    """切掉向后扫时「吞进来」的两类前文——两类都是真出现过的形态：

    ① 无空格分隔的中文散文：`原文见docs/01-x.md` ⇒ 去掉前导非 ASCII。
       （前导全角 `（` 也在此被去掉：`（docs/01-x.md` ⇒ `docs/01-x.md`，与 _root_boundary 互为兜底。）
    ② 全角左括号当定界符时吞进来的前文：`timeout=8（absorb-wt/dify/README.md` ⇒ 从 `（` 之后起算，
       但仅当其后形如 `名字/…`（_LEADSEG_RE）——否则会误切 `docs/04-方案2设计（本体+LLM）.md`。
    """
    i = 0
    while i < len(raw) and not raw[i].isascii():
        i += 1
    if i:
        raw = raw[i:]
    for j, ch in enumerate(raw):
        if ch == "\uff08":
            cand = raw[j + 1:]
            if _LEADSEG_RE.match(cand):
                return cand
    return raw


def _root_boundary(raw, root_entries):
    """返回 raw 中「某个仓库顶层条目名 + /」的最靠前起点；找不到返回 None。"""
    best = None
    for name in root_entries:
        j = raw.find(name + "/")
        if j < 0:
            continue
        if j == 0:
            return 0
        if best is None or j < best:
            best = j
    return best


def _parse_line_items(spec):
    """把行号规格分解成**保留「单行 / 区间」区别**的条目列表（M2.24 规则⑥）。

    `49,57,160` -> [("line",49,49), ("line",57,57), ("line",160,160)]
    `69-72`     -> [("range",69,72)]
    `860,878`   -> 两个单行条目；`773-774` -> 一个区间条目（逗号列表 / 混合形态逐项分解，与 M2.13 同）。
    区间已归一为 a <= b；分解粒度与旧 _expand_lines 完全一致，只是**不再把区间摊平成逐行** ——
    摊平后无法区分「行首锚点」与「区间内部」，而新的规则⑥对这两者强度刻意不同。
    """
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s.strip()), int(b_s.strip())
            if b < a:
                a, b = b, a
            out.append(("range", a, b))
        else:
            a = int(part)
            out.append(("line", a, a))
    return out


def _judge_line_item(kind, a, b, tlines, rel):
    """判一个行号条目（规则⑤越界 + 规则⑥空行/分隔行，M2.24 语义）。

    返回 (findings, notes, is_long_range)：
      findings = [(sev, msg)]，sev ∈ {"ERROR","WARN"}；notes = [(tag, detail)] 供 --list 明细。
    强度**刻意不同**（本函数存在的全部理由）：
      · 单行 N   ⇒ 越界 / 空行 / 纯分隔行 ⇒ ERROR（M2.13 原样，锚点强度不变）。
      · 区间 A-B ⇒ 只判**行首锚点 A**（端点越界 / A 空 / A 为纯分隔行 ⇒ ERROR）；
                    区间内部 (A+1..B) 的空行与分隔行**一律放行**（契约引用一整节散文的必需）。
    """
    out_f = []
    out_n = []
    ends = (a, b) if kind == "range" else (a,)
    hit_oor = False
    for ln in ends:
        if ln < 1 or ln > len(tlines):
            hit_oor = True
            out_f.append(("ERROR", "⑤ 行号越界: %s:%d（目标共 %d 行）" % (rel, ln, len(tlines))))
            out_n.append(("LINE-OOR", "%s:%d (target %d lines)" % (rel, ln, len(tlines))))
    if hit_oor:
        return out_f, out_n, False

    anchor = tlines[a - 1].strip()

    if kind == "line":
        if anchor == "":
            out_f.append(("ERROR", "⑥ 行号命中空行: %s:%d（行号漂移的典型形态）" % (rel, a)))
            out_n.append(("LINE-BLANK", "%s:%d" % (rel, a)))
        elif _SEP_RE.match(anchor):
            out_f.append(("ERROR", "⑥ 行号命中分隔行 %r: %s:%d" % (anchor[:20], rel, a)))
            out_n.append(("LINE-SEP", "%s:%d" % (rel, a)))
        else:
            out_n.append(("LINE-OK", "%s:%d | %s" % (rel, a, anchor[:70])))
        return out_f, out_n, False

    # ---- 区间：只判锚点 ----
    span = b - a + 1
    if anchor == "":
        out_f.append(("ERROR", "⑥ 行号命中空行（区间锚点）: %s:%d（区间 %d-%d；区间起点必须真实 —— "
                               "内部空行允许，锚点不允许）" % (rel, a, a, b)))
        out_n.append(("RANGE-ANCHOR-BLANK", "%s:%d (range %d-%d)" % (rel, a, a, b)))
        return out_f, out_n, False
    if _SEP_RE.match(anchor):
        out_f.append(("ERROR", "⑥ 行号命中分隔行（区间锚点）%r: %s:%d（区间 %d-%d）"
                               % (anchor[:20], rel, a, a, b)))
        out_n.append(("RANGE-ANCHOR-SEP", "%s:%d (range %d-%d)" % (rel, a, a, b)))
        return out_f, out_n, False

    nonblank = 0
    for s in tlines[a - 1:b]:
        s = s.strip()
        if s and not _SEP_RE.match(s):
            nonblank += 1
    if nonblank == 0:
        # ★ 显式断言（规则④）：由「锚点非空」已经蕴含，正常路径不可达；单独写出来是为了让
        #   「区间内无任何非空行」在代码里是一个**看得见的 ERROR**，而不是「靠上面某个分支顺带保证」
        #   的隐含性质 —— 日后若有人只改锚点分支，这条断言会让全空区间立刻变红，不会被静默放宽。
        out_f.append(("ERROR", "⑥ 区间内无任何非空行: %s:%d-%d（区间引用必须至少锚在一行真实内容上）"
                               % (rel, a, b)))
        out_n.append(("RANGE-ALL-BLANK", "%s:%d-%d" % (rel, a, b)))
        return out_f, out_n, False

    if span > RANGE_SPAN_WARN:
        out_f.append(("WARN", "⑥ 长区间（%d 行 > 阈值 %d）是弱引用，建议改引小节标题行 + §号: %s:%d-%d"
                              % (span, RANGE_SPAN_WARN, rel, a, b)))
        out_n.append(("RANGE-LONG", "%s:%d-%d (%d lines)" % (rel, a, b, span)))
        return out_f, out_n, True

    out_n.append(("RANGE-OK", "%s:%d-%d | %s" % (rel, a, b, anchor[:70])))
    return out_f, out_n, False


_BASENAME_INDEX = {}


def _build_index(root):
    """文件名 -> 仓库相对路径列表（用于「你引用的路径不在本仓库，但同名文件在 X」提示）。"""
    idx = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_WALK_DIRS and d != "tmp"]
        for n in filenames:
            idx.setdefault(n, []).append(
                os.path.relpath(os.path.join(dirpath, n), root).replace("\\", "/"))
    return idx


def _hint(rel):
    hits = _BASENAME_INDEX.get(rel.rsplit("/", 1)[-1]) or []
    if not hits:
        return ""
    return "；仓库内同名文件: " + ", ".join(sorted(hits)[:3]) + ("…" if len(hits) > 3 else "")


def _read_lines(path):
    """读文件 -> 行列表（UTF-8，坏字节替换；不因编码问题中断门禁）。"""
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n").split("\n")


def main():
    root = os.path.abspath(_arg("--root", _DEFAULT_ROOT))
    model_dir = os.path.abspath(_arg("--model-dir", os.path.join(root, "shared", "model")))
    mapping_dir = os.path.abspath(_arg("--mapping-dir", os.path.join(root, "shared", "mapping")))
    min_refs = int(_arg("--min-refs", "40"))
    show_list = "--list" in sys.argv
    strict = "--strict" in sys.argv          # ⑨ 忽略 KNOWN_DEBT（欠账清零后应改用它）

    findings = []          # (sev, src, srcline, msg)
    bare_refs = []         # (src, srcline, raw)
    ext_refs = []          # (src, srcline, raw)
    list_lines = []        # --list 明细
    files = []             # (abspath, tag)

    root_entries = []
    if os.path.isdir(root):
        root_entries = sorted(n for n in os.listdir(root) if not n.startswith("."))
    else:
        findings.append(("ERROR", "<arg>", 0, "--root 不是目录: %s" % root))

    for d, tag in ((model_dir, "shared/model"), (mapping_dir, "shared/mapping")):
        if not os.path.isdir(d):
            # tmp/ 下的突变副本可能只带 model 一半 ⇒ mapping 缺失不算错，但要显式说明
            findings.append(("WARN", tag, 0, "扫描目录不存在: %s（该层 0 个文件被扫）" % d))
            continue
        for n in sorted(os.listdir(d)):
            if n.endswith(".yaml") or n.endswith(".yml"):
                p = os.path.join(d, n)
                if os.path.isfile(p):
                    files.append((p, tag))

    global _BASENAME_INDEX
    _BASENAME_INDEX = _build_index(root) if os.path.isdir(root) else {}

    n_refs = n_external = n_bare = n_foreign = n_checked = 0
    n_exists = n_linerefs = n_linenos = n_debt = 0
    n_single = n_range = n_range_long = 0

    for path, tag in files:
        src = (os.path.relpath(path, root).replace("\\", "/")
               if path.startswith(root + os.sep) else path)
        try:
            lines = _read_lines(path)
        except OSError as exc:
            findings.append(("ERROR", src, 0, "读取失败: %s" % exc))
            continue
        text = "\n".join(lines)
        for m in _EXT_RE.finditer(text):
            p_end = m.end()
            i = m.start() - 1
            while i >= 0 and _is_path_char(text[i]):
                i -= 1
            p_start = i + 1
            raw_all = text[p_start:p_end]
            raw = _strip_leading_prose(raw_all)
            p_start = p_end - len(raw)
            if not raw:
                continue
            srcline = text.count("\n", 0, p_start) + 1
            lm = _LINE_SPEC_RE.match(text, p_end)
            spec = lm.group(1) if lm else ""
            pre = text[max(0, p_start - 12):p_start]

            n_refs += 1
            # ---- ② 外部环境：绝对路径 / URL ⇒ 只计数并列出 ----
            if (_WIN_ABS_RE.match(raw) or raw.startswith("/") or raw.startswith("\\\\")
                    or pre.endswith(("http://", "https://", "ftp://", "http:/", "https:/"))):
                n_external += 1
                ext_refs.append((src, srcline, raw + (":" + spec if spec else "")))
                list_lines.append("[G20-LIST] SKIP-EXTERNAL %s:%d -> %s%s"
                                  % (src, srcline, raw, (":" + spec) if spec else ""))
                continue

            # ★ 反斜杠记法（docs/01-场景建模与范围定义.md）与正斜杠同义 ⇒ 先归一
            rawn = raw.replace("\\", "/")
            b = _root_boundary(rawn, root_entries)
            if b is not None:
                rawn = rawn[b:]
            elif "/" not in rawn:
                # ---- ⑦ 裸文件名：覆盖面之外，列明细不判错 ----
                n_bare += 1
                bare_refs.append((src, srcline, raw + (":" + spec if spec else "")))
                list_lines.append("[G20-LIST] NOT-COVERED-BARE %s:%d -> %s%s"
                                  % (src, srcline, raw, (":" + spec) if spec else ""))
                continue
            else:
                # ---- ① 首段不是本仓库顶层条目 ⇒ 其他仓库 / 真机目录 / 已删除的顶层目录 ----
                n_foreign += 1
                findings.append(("WARN", src, srcline,
                                 "⑦ 非本仓库路径（首段不是本仓库顶层条目 ⇒ 其他仓库/真机目录，仅告警）: %s%s%s"
                                 % (rawn, (":" + spec) if spec else "", _hint(rawn))))
                list_lines.append("[G20-LIST] FOREIGN %s:%d -> %s%s"
                                  % (src, srcline, raw, (":" + spec) if spec else ""))
                continue

            rel = rawn
            target = os.path.normpath(os.path.join(root, rel))
            if not (target == root or target.startswith(root + os.sep)):
                n_external += 1
                ext_refs.append((src, srcline, raw))
                list_lines.append("[G20-LIST] SKIP-OUTSIDE-ROOT %s:%d -> %s" % (src, srcline, raw))
                continue

            n_checked += 1
            ref_disp = rel + ((":" + spec) if spec else "")
            if not os.path.exists(target):
                if rel.startswith("tmp/"):
                    findings.append(("WARN", src, srcline,
                                     "① tmp/ 目标缺失（临时产物可清理，仅告警）: %s" % ref_disp))
                elif (src, rel) in KNOWN_DEBT and not strict:
                    n_debt += 1
                    findings.append(("WARN", src, srcline,
                                     "⑨ 已知欠账（report-only，非本次可修面）: %s —— %s"
                                     % (ref_disp, KNOWN_DEBT[(src, rel)])))
                else:
                    findings.append(("ERROR", src, srcline,
                                     "② 引用目标不存在: %s（解析为 %s）" % (ref_disp, target)))
                list_lines.append("[G20-LIST] MISSING %s:%d -> %s" % (src, srcline, ref_disp))
                continue
            if not os.path.isfile(target):
                findings.append(("ERROR", src, srcline, "③ 引用目标不是文件: %s" % ref_disp))
                continue
            n_exists += 1
            if not spec:
                list_lines.append("[G20-LIST] OK %s:%d -> %s" % (src, srcline, ref_disp))
                continue
            try:
                tlines = _read_lines(target)
            except OSError as exc:
                findings.append(("ERROR", src, srcline, "④ 目标读取失败 %s: %s" % (ref_disp, exc)))
                continue
            n_linerefs += 1
            for kind, a, b in _parse_line_items(spec):
                n_linenos += 1
                if kind == "line":
                    n_single += 1
                else:
                    n_range += 1
                item_findings, item_notes, item_is_long = _judge_line_item(kind, a, b, tlines, rel)
                if item_is_long:
                    n_range_long += 1
                for sev, msg in item_findings:
                    findings.append((sev, src, srcline, msg))
                for tag, detail in item_notes:
                    list_lines.append("[G20-LIST] %s %s:%d -> %s" % (tag, src, srcline, detail))

    # ---- ⑧ 反恒真 ----
    if not files:
        findings.append(("ERROR", "<anti-vacuity>", 0,
                         "扫描文件数 = 0 ⇒ 反恒真失败（一个 yaml 都没读到，不得判 PASS）"))
    if n_checked < min_refs:
        findings.append(("ERROR", "<anti-vacuity>", 0,
                         "实际检查引用 %d 条 < --min-refs %d ⇒ 反恒真失败"
                         "（禁止把「检查了 0 条」当通过）" % (n_checked, min_refs)))

    errors = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]

    print("=" * 78)
    print("[G20] 引用可解析性门禁（只读；不依赖 PyYAML / 真机）")
    print("[G20] root=%s" % root)
    print("[G20] 扫描 shared/model=%s / shared/mapping=%s" % (model_dir, mapping_dir))
    print("[G20] 扫描文件 %d 个；引用 %d 条 = 本仓库校验 %d + 裸文件名未覆盖 %d + 非本仓库告警 %d + 外部跳过 %d"
          % (len(files), n_refs, n_checked, n_bare, n_foreign, n_external))
    print("[G20] 实际检查 %d 条（--min-refs %d ⇒ 反恒真%s）：目标存在 %d / 缺失 %d；"
          "带行号引用 %d 条、共校验行号条目 %d 个（单行 %d + 区间锚点 %d；其中长区间 WARN %d 条）"
          % (n_checked, min_refs, "通过" if n_checked >= min_refs else "失败",
             n_exists, n_checked - n_exists, n_linerefs, n_linenos,
             n_single, n_range, n_range_long))
    print("[G20] 判定分级：ERROR %d 条 / WARN %d 条（含非本仓库路径 %d / 已知欠账 %d / tmp 缺失）/ "
          "外部跳过 %d 条 / 裸文件名未覆盖 %d 条"
          % (len(errors), len(warns), n_foreign, n_debt, n_external, n_bare))
    print("-" * 78)
    for sev, src, srcline, msg in sorted(findings):
        print("[G20/%s] %s:%d  %s" % (sev, src, srcline, msg))
    if KNOWN_DEBT:
        print("-" * 78)
        print("[G20] 已知欠账清单（report-only：文件由他人持有、本次硬边界禁止改；--strict 会按 ERROR 处理）：")
        for (_f, _p), _why in sorted(KNOWN_DEBT.items()):
            _seen = "命中 %d 次" % n_debt if n_debt else "本次未命中"
            print("[G20/DEBT] %s  <- %s\n            %s（%s）" % (_p, _f, _why, _seen))
    if ext_refs:
        print("-" * 78)
        print("[G20] 外部跳过明细（绝对路径 / URL：指向真机、OS 或第三方，不在本仓库可解析范围）：")
        for src, srcline, raw in ext_refs:
            print("[G20/SKIP] %s:%d  %s" % (src, srcline, raw))
    if bare_refs:
        print("-" * 78)
        print("[G20] 未覆盖明细（裸文件名，规则⑦不判错；如需校验请补全仓库相对路径）：")
        for src, srcline, raw in bare_refs:
            print("[G20/NOT-COVERED] %s:%d  %s" % (src, srcline, raw))
    if show_list:
        print("-" * 78)
        print("[G20] --list 明细（逐条引用的解析与行号校验结果）：")
        for ln in list_lines:
            print(ln)
    print("-" * 78)
    print("RESULT: %s  (ERROR=%d WARN=%d DEBT=%d refs=%d checked=%d min-refs=%d%s)"
          % ("PASS" if not errors else "FAIL", len(errors), len(warns), n_debt,
             n_refs, n_checked, min_refs, " STRICT" if strict else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
