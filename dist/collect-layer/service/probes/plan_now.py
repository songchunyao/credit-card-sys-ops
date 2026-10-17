# -*- coding: utf-8 -*-
"""按需 EXPLAIN：从**真实执行计划**取慢 SQL 的计划面（不依赖历史留存）

## 为什么要它

`sql.plan_type` 等事实原先只来自 `dbe_perf.statement_history.query_plan`。
实测该列**只有 2 条有值** —— 因为 `track_stmt_stat_level = OFF,L0`：只有超过
慢阈值（`log_min_duration_statement = 3000ms`）的语句才带计划。
也就是说「绝大多数慢 SQL 都看不到计划」，而计划恰恰是慢 SQL 归因的核心判据面。

本模块改用**按需 EXPLAIN**：拿 SQL 文本现取计划。判据面因此不再受历史留存限制。

## 两处必须写死的实测约束

① **占位符**：`statement_history.query` 存的是**归一化 SQL**（含 `?`），
   直接 EXPLAIN 会 `syntax error`。实测 9 条慢 SQL 里 **7 条**因此失败。
   ⇒ 必须先把占位符替换成**确定性样本值**（本模块 `substitute_placeholders`）。
   ★ 这是**合成值**，不是真实绑定值（本实例 `track_stmt_parameter = off`，库里没存参数）
     ⇒ 计划**形状**可靠，但**行数/选择性是近似**。必须由 `fidelity` 字段显式声明，
       绝不允许合成值的结果被当成精确结果呈现（opencode_skill 的 sqltune 正是这么要求的）。

② **必须 fail-closed**：替换后仍 EXPLAIN 不了（对象不存在/权限不足/语法仍错）
   ⇒ **不产出任何计划事实**，只记 `sql.plan_unavailable_reason`。
   绝不能"猜一个计划"或"用默认值凑"——本项目最贵的一类后果是把编造值当结论。

## 出处

占位符替换的启发式（`?`/`$N`/`:N`、跳过字面量与注释、按左邻上下文选值）与
确定性风险标注（seq_scan / sort / nestloop+seqscan / hash_join）**吸纳自**
`opendb_skill/opencode_skill-main`（`skills/sqltune/scripts/placeholder.py`、
`skills/explain/scripts/explain.py`）；本模块按其设计重写并加了两点本仓要求：
  · 保真度必须随结果回传（`fidelity`），且合成值场景强制标注；
  · 失败必须 fail-closed 并给出**可读原因**，不得静默返回空。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# ── 确定性风险标注的阈值 ──
# ★ 为什么不用「行数」判定大表：慢 SQL 的计划里 rows 是**估算**（且合成值下更不可信）。
#   这里只做**结构性**判定（出现了什么节点），与 explain 技能同口径 —— 结构不依赖选择性。
LARGE_ROWS_HINT = 1_000_000

_TO_CHAR_FORMAT_RE = re.compile(r"(?i)to_char\s*\(\s*[a-z_][a-z0-9_.]*\s*,\s*$")


# ══════════════════════════ 占位符替换 ══════════════════════════

@dataclass(frozen=True)
class SubInfo:
    position: int
    token: str
    context: str
    value: str
    source: str


# ★ 逗号位与兜底位的**类型不可从文本判定**（同一个 `,` 可能是
#   `string_agg(col, ?)` 的分隔符=字符串，也可能是 `COALESCE(SUM(amount), ?)`
#   的默认值=数值）。实测：给 `'x'` 会 `invalid input syntax for type numeric`。
#   ⇒ 不堆更多启发式，而是**有界重试 + 显式回退**：首猜失败于类型错误时，
#     把这几类**含糊来源**换成另一个候选值重试一次，并如实回报 `retry_used`。
_AMBIGUOUS_SOURCES = ("rule-func-arg", "default")
_ALT_VALUE = {"rule-func-arg": "0", "default": "'x'"}


@dataclass(frozen=True)
class Substitution:
    sql: str
    substitutions: List[SubInfo] = field(default_factory=list)
    count: int = 0
    retried: bool = False

    @property
    def fidelity(self) -> str:
        """exact = 原样可直接执行；synthesized_binds = 用了合成值（行数/选择性近似）"""
        return "synthesized_binds" if self.count else "exact"


def _find_placeholders(sql: str) -> List[Tuple[int, int]]:
    """扫描 `?` / `$N` / `:N`，**跳过**字符串字面量、引号标识符与注释

    ★ 必须跳过字面量：`WHERE note = 'why?'` 里的 `?` 不是占位符；
      不跳过会把它替换掉 ⇒ 改变语义（且不报错）。
    """
    out: List[Tuple[int, int]] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":                       # 单引号字符串（'' 转义）
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == '"':                       # 双引号标识符
            i += 1
            while i < n and sql[i] != '"':
                i += 1
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = i + 2 if i + 1 < n else n
            continue
        if c == "?":
            out.append((i, i + 1))
            i += 1
            continue
        if c == "$" and i + 1 < n and sql[i + 1].isdigit():
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            out.append((i, j))
            i = j
            continue
        if c == ":" and i + 1 < n and sql[i + 1].isdigit():   # :N（不含 ::cast / :name）
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            out.append((i, j))
            i = j
            continue
        i += 1
    return out


def _ends_with_kw(s: str, kw: str) -> bool:
    if not s.endswith(kw):
        return False
    if len(s) == len(kw):
        return True
    return s[len(s) - len(kw) - 1] in " \t\n(,"


def _ends_with_op(s: str, op: str) -> bool:
    if not s.endswith(op):
        return False
    if op == "=" and len(s) >= 2 and s[-2] in "<>!":
        return False
    return True


def _col_token(ctx: str) -> str:
    for t in reversed(ctx.split()):
        t = t.rstrip("=<>!,()")
        if not t:
            continue
        if "." in t:
            t = t[t.rindex(".") + 1:]
        return t
    return ""


def _looks_int_col(ctx: str) -> bool:
    t = _col_token(ctx)
    return bool(t) and (t.endswith("_id") or t == "id" or t.endswith("_no")
                        or t.endswith("_num") or t.endswith("count")
                        or t.endswith("qty") or t.endswith("amount") or t.endswith("price"))


def _looks_date_col(ctx: str) -> bool:
    t = _col_token(ctx)
    return bool(t) and (t.endswith("_date") or t.endswith("_time") or t.endswith("_at")
                        or t in ("date", "time") or "timestamp" in t)


def _choose(left_ctx: str) -> Tuple[str, str]:
    """按左邻上下文选一个**确定性**样本值（同输入必同输出 —— 可复算）"""
    low = left_ctx.lower()
    t = low.rstrip(" \t\n")

    if t.endswith("limit"):
        return "100", "rule-limit"
    if t.endswith("offset"):
        return "0", "rule-offset"
    if t.endswith("interval"):
        return "'1 day'", "rule-interval"
    # 类型化字面量（DATE ? / TIMESTAMP ? / TIME ?）必须给**带引号**的字面量，
    # 给裸数字会 "syntax error near N"。用整词匹配，避免把 order_date 当成 date。
    if _ends_with_kw(t, "timestamp"):
        return "'2024-01-01 00:00:00'", "rule-typed-timestamp"
    if _ends_with_kw(t, "date"):
        return "'2024-01-01'", "rule-typed-date"
    if _ends_with_kw(t, "time"):
        return "'12:00:00'", "rule-typed-time"
    if _ends_with_kw(t, "like") or _ends_with_kw(t, "ilike"):
        return "'%test%'", "rule-like"
    if _TO_CHAR_FORMAT_RE.search(left_ctx):
        return "'YYYY-MM-DD'", "rule-to_char"
    # ★ 函数实参位（左邻以逗号结尾）：绝大多数中文/格式/分隔符参数的实参要**字符串**。
    #   实测：`string_agg(row_text, ? ORDER BY row_text)` 给裸数字会
    #   "syntax error at or near ORDER" ⇒ 必须给带引号字面量。
    if t.endswith(","):
        return "'x'", "rule-func-arg"
    if _ends_with_op(t, "=") or _ends_with_op(t, "<>") or _ends_with_op(t, "!="):
        if _looks_int_col(t):
            return "1", "rule-eq-int"
        if _looks_date_col(t):
            return "'2024-01-01'", "rule-eq-date"
        return "'test'", "rule-eq-text"
    if (_ends_with_op(t, "<=") or _ends_with_op(t, ">=")
            or _ends_with_op(t, "<") or _ends_with_op(t, ">")):
        if _looks_date_col(t):
            return "'2024-01-01'", "rule-cmp-date"
        return "50", "rule-cmp"
    if "in (" in t or "in(" in t:
        return "1" if _looks_int_col(t) else "'test'", "rule-in"
    if _ends_with_kw(t, "between") or _ends_with_kw(t, "and"):
        return "1", "rule-between"
    return "1", "default"


def substitute_placeholders(sql: str, binds: Optional[List[str]] = None,
                            ambiguous_as: str = "primary") -> Substitution:
    """把占位符替换成确定性样本值（`binds` 可覆盖前 N 个）

    ★ 只用**纯文本启发式**，不查库（不引入额外 I/O 与失败面）。
    ★ 同输入必同输出 ⇒ 结果可复算、可比较。
    ★ `ambiguous_as`：含糊来源（逗号位/兜底位）取哪个候选值。
      `"primary"` = 字符串候选；`"alt"` = 数值候选。由 `explain_now` 在
      **类型错误**时自动换一次（见 `_AMBIGUOUS_SOURCES` 的说明）。
    """
    binds = binds or []
    pos = _find_placeholders(sql or "")
    if not pos:
        return Substitution(sql=sql or "", substitutions=[], count=0)
    subs: List[SubInfo] = []
    for i, (start, end) in enumerate(pos):
        left = (sql or "")[max(0, start - 80):start]
        if i < len(binds) and binds[i] != "":
            val, src = binds[i], "bind"
        else:
            val, src = _choose(left)
            if ambiguous_as == "alt" and src in _AMBIGUOUS_SOURCES:
                val = _ALT_VALUE[src]
                src = src + "-retry"
        subs.append(SubInfo(start, (sql or "")[start:end], left.strip(), val, src))
    out = sql or ""
    for i in range(len(subs) - 1, -1, -1):          # 从后往前替换，保持前面的偏移有效
        start, end = pos[i]
        out = out[:start] + subs[i].value + out[end:]
    return Substitution(sql=out, substitutions=subs, count=len(subs),
                        retried=(ambiguous_as == "alt"))


# ══════════════════════════ 计划解析（结构化） ══════════════════════════

@dataclass(frozen=True)
class PlanFeatures:
    total_cost: Optional[float] = None
    node_types: List[str] = field(default_factory=list)
    plan_type: str = ""                       # seq_scan / index_scan / other
    seq_scan_tables: List[str] = field(default_factory=list)
    indexes_used: List[str] = field(default_factory=list)
    has_big_sort: bool = False
    has_big_agg: bool = False
    has_nested_loop: bool = False
    max_rows: float = 0.0


def _json_plan(rows: Any) -> Optional[dict]:
    """从驱动返回的行里取 EXPLAIN (FORMAT JSON) 的顶层 Plan（形状自适应）"""
    if not rows:
        return None
    row = rows[0]
    v = None
    if callable(getattr(row, "get", None)):
        for k in ("QUERY PLAN", "query plan", "QUERY_PLAN"):
            try:
                v = row.get(k)
            except Exception:                              # noqa: BLE001
                v = None
            if v is not None:
                break
    if v is None:
        try:
            v = list(dict(row).values())[0]
        except Exception:                                  # noqa: BLE001
            return None
    try:
        j = v if isinstance(v, (list, dict)) else json.loads(str(v))
    except Exception:                                      # noqa: BLE001
        return None
    if isinstance(j, dict):
        j = [j]
    if not isinstance(j, list) or not j or not isinstance(j[0], dict):
        return None
    p = j[0].get("Plan")
    return p if isinstance(p, dict) else None


def analyze_plan_json(rows: Any) -> Optional[PlanFeatures]:
    """从 EXPLAIN (FORMAT JSON) 的结果提取计划特征（**结构化**，不做文本正则）

    ★ 为什么走 JSON 而不是文本正则：文本里 `Seq Scan` 会同时出现在节点行与
      `Filter`/`Recheck Cond` 说明行；而 `Top Cost` 在嵌套计划里有多个片段。
      JSON 里 Node Type / Relation Name / Index Name / Total Cost 都是无歧义的字段。
    """
    plan = _json_plan(rows)
    if plan is None:
        return None
    nodes: List[str] = []
    seq_tables: List[str] = []
    idx: List[str] = []
    has_sort = has_agg = has_nl = False
    max_rows = 0.0
    stack = [plan]
    guard = 0
    while stack and guard < 5000:
        guard += 1
        n = stack.pop()
        if not isinstance(n, dict):
            continue
        nt = str(n.get("Node Type") or "")
        if nt:
            nodes.append(nt)
            if nt == "Seq Scan":
                t = str(n.get("Relation Name") or n.get("Alias") or "")
                if t and t not in seq_tables:
                    seq_tables.append(t)
            if "Sort" in nt:
                has_sort = True
            if "Aggregate" in nt or nt == "WindowAgg":
                has_agg = True
            if nt == "Nested Loop":
                has_nl = True
            if nt.endswith("Index Scan") or nt == "Bitmap Index Scan":
                iname = str(n.get("Index Name") or "")
                if iname and iname not in idx:
                    idx.append(iname)
        try:
            max_rows = max(max_rows, float(n.get("Plan Rows") or 0))
        except (TypeError, ValueError):
            pass
        for c in (n.get("Plans") or []):
            stack.append(c)
    try:
        cost = float(plan.get("Total Cost"))
    except (TypeError, ValueError):
        cost = None
    ptype = "seq_scan" if "Seq Scan" in nodes else (
        "index_scan" if any("Index Scan" in x for x in nodes) else "other")
    return PlanFeatures(total_cost=cost, node_types=nodes[:24], plan_type=ptype,
                        seq_scan_tables=seq_tables[:12], indexes_used=idx[:12],
                        has_big_sort=(has_sort and max_rows >= LARGE_ROWS_HINT),
                        has_big_agg=has_agg, has_nested_loop=has_nl, max_rows=max_rows)


# ══════════════════════════ 确定性风险标注 ══════════════════════════

def risk_findings(f: PlanFeatures) -> List[Dict[str, str]]:
    """从计划特征给出**确定性**风险标注（只依据结构，不猜选择性）

    吸纳自 explain 技能的 `scan_plan`，但改为在**结构化特征**上判定
    （其原版是对计划文本做前缀匹配，会把说明行也当成节点）。
    """
    out: List[Dict[str, str]] = []
    if f.seq_scan_tables:
        out.append({"kind": "seq_scan", "severity": "warn",
                    "detail": "全表扫描：%s" % ",".join(f.seq_scan_tables[:5]),
                    "advice": "大表上若筛选性高，考虑在过滤/连接列建索引"})
    if f.has_nested_loop and f.seq_scan_tables:
        out.append({"kind": "nestloop_seqscan", "severity": "warn",
                    "detail": "Nested Loop 内层出现全表扫描",
                    "advice": "内层全扫会被循环放大，优先在内层连接键建索引"})
    if f.has_big_sort:
        out.append({"kind": "big_sort", "severity": "warn",
                    "detail": "大结果集排序（估算行数 ≥ %d）" % LARGE_ROWS_HINT,
                    "advice": "考虑与 ORDER BY 匹配的索引；并检查 work_mem 是否溢出到盘"})
    if f.has_big_agg:
        out.append({"kind": "aggregate", "severity": "info",
                    "detail": "存在聚合/窗口聚合",
                    "advice": "若是大表上的聚合或 COUNT(DISTINCT)，可考虑预聚合"})
    if not out and f.total_cost is not None:
        out.append({"kind": "none", "severity": "info",
                    "detail": "未命中确定性风险模式（cost=%.2f）" % f.total_cost,
                    "advice": "结构上未见常见退化，需结合统计信息与等待事件判断"})
    return out


# ══════════════════════════ 编排（fail-closed） ══════════════════════════

_DML_RE = re.compile(r"(?i)^\s*(insert|update|delete|merge|create|alter|drop|truncate|"
                     r"grant|revoke|vacuum|analyze|reindex|copy|call|do|lock)\b")


def is_readonly_stmt(sql: str) -> bool:
    """只对只读语句取计划（EXPLAIN 不执行，但我们仍只认 SELECT/WITH/VALUES/SHOW）"""
    s = re.sub(r"[\s]+", " ", (sql or "").strip()).lstrip()
    return bool(re.match(r"(?i)^(select|with|values|show|table)\b", s))


@dataclass
class ExplainNow:
    ok: bool = False
    sql: str = ""
    fidelity: str = "unknown"        # exact | synthesized_binds | unknown
    features: Optional[PlanFeatures] = None
    risks: List[Dict[str, str]] = field(default_factory=list)
    substituted: int = 0
    retry_used: bool = False         # 是否启用了「换候选值重试」（含糊来源）
    reason: str = ""


# 值类型错误（"invalid input syntax for type …"）⇒ 换一个候选值重试有意义
_TYPE_ERR_RE = re.compile(r"(?i)(invalid input syntax for type|"
                          r"operator does not exist|cannot be cast)")


async def _try_explain(conn, target: str):
    """执行一次 EXPLAIN；返回 (ok, features_or_None, err_text)"""
    try:
        rows = await conn.fetch("EXPLAIN (FORMAT JSON, COSTS TRUE) " + target)
    except Exception as e:                                 # noqa: BLE001
        return False, None, "%s: %s" % (type(e).__name__, str(e)[:200])
    feats = analyze_plan_json(rows)
    if feats is None:
        return False, None, "EXPLAIN 返回了结果但无法解析出计划（形状不符）"
    return True, feats, ""


async def explain_now(conn, sql: str, binds: Optional[List[str]] = None,
                      timeout_ms: int = 15000) -> ExplainNow:
    """对一条 SQL 现取真实计划（只读；失败一律 fail-closed）

    Args:
        conn: 已建立的 async_gaussdb 连接（**由调用方持有**，本函数不开关连接）
        sql:  SQL 文本（可含占位符，会被替换成确定性样本值）
        binds: 真实绑定值（可选，覆盖前 N 个占位符）

    Returns:
        ExplainNow。失败时 `ok=False` 且 `reason` 给出**可读原因**；
        **绝不**在失败时编造 features。

    ★ 有界重试：含糊来源（逗号位/兜底位，类型无法从文本判定）首猜失败且错误是
      **类型错误**时，换另一个候选值重试**一次**。不无限重试 —— 一次即止，
      并如实回报 `fidelity`（仍是 synthesized_binds）。
    """
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        return ExplainNow(ok=False, reason="空 SQL")
    if not is_readonly_stmt(raw):
        return ExplainNow(ok=False, sql=raw,
                          reason="非只读语句，拒绝取计划（EXPLAIN 不执行，但只读纪律要求拒绝）")
    sub = substitute_placeholders(raw, binds)
    try:
        await conn.fetch("SET statement_timeout = %d" % int(timeout_ms))
    except Exception:                                      # noqa: BLE001
        pass

    ok, feats, err = await _try_explain(conn, sub.sql)
    if not ok and sub.count and _TYPE_ERR_RE.search(err or ""):
        sub2 = substitute_placeholders(raw, binds, ambiguous_as="alt")
        if sub2.sql != sub.sql:
            ok2, feats2, err2 = await _try_explain(conn, sub2.sql)
            if ok2:
                sub, ok, feats, err = sub2, ok2, feats2, ""
            else:
                err = err2 or err
    if not ok:
        return ExplainNow(ok=False, sql=sub.sql, fidelity=sub.fidelity,
                          substituted=sub.count, reason="EXPLAIN 失败：" + (err or "未知"),
                          retry_used=sub.retried)
    return ExplainNow(ok=True, sql=sub.sql, fidelity=sub.fidelity, features=feats,
                      risks=risk_findings(feats), substituted=sub.count, reason="",
                      retry_used=sub.retried)
