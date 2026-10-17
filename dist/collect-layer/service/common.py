# -*- coding: utf-8 -*-
"""两栈共用基础设施 —— collect 与 knowledge 层共享的最小内核

包含：
  * .env 加载器（_load_env_file）与路径常量（BASE_DIR / AGENT_DIR）
  * 工具输出护栏（_loop_guard）+ 服务间调用豁免（X-GDO-Internal）
  * 异步桥接（_run_sync）
  * 两个注册表：健康贡献者、Change 提供者（用于反转 collect→knowledge 依赖）

分层原则（docs/50）：本模块只放两栈都必需的机制，不含任何业务知识。
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import sys
import threading
import time as _time_guard
from typing import Any, Dict

def _load_env_file(path: str):
    """极简 .env 读取器（KEY=*** 不引入第三方依赖）"""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for _line in f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip().strip('"').strip("'")  # force set .env wins


# ── 路径常量（两栈共用）──
# BASE_DIR: 本包所在目录（service/）；AGENT_DIR: 仓库根（其上一层）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.dirname(BASE_DIR)



_LOOP_SEEN: Dict[str, Dict[str, Any]] = {}   # 同一时刻只保留最新一个 key
_LOOP_WINDOW = 600.0   # 10 分钟

# ── 服务间调用豁免（2026-09-13）────────────────────────────────
# 现象：v3 本体服务是**确定性编排器**——它跨多次用户交互天然会用完全相同的
#      参数调 gather（如「诊断→采集→执行」都走 {"layers":[...],"sections":[]} 同一形状）。
#      护栏按「同参数连续调用」计数，第 3 次起丢弃数据体 → v3 回落到自身缓存，
#      缓存为空时（重启后）整链失能：用户侧表现为 no_data 循环。
# 判定：护栏的本意是防 **LLM** 循环，不应拦服务间确定性调用。
#      请求带 X-GDO-Internal 头（v3 的 _v2_post 统一携带）→ 护栏完全不介入
#      （不计数、不改写、也不重置其它键的计数）。
import contextvars
_INTERNAL_CALL = contextvars.ContextVar("gdo_internal_call", default=False)



def _loop_guard(tool: str, req_obj: Any, out: Dict[str, Any]) -> Dict[str, Any]:
    if _INTERNAL_CALL.get():
        # 服务间调用（v3 编排）直接放行：确定性调用方自己负责去重与缓存
        return out
    try:
        params = req_obj.model_dump()
    except AttributeError:
        try:
            params = req_obj.dict()
        except AttributeError:
            params = {"_raw": str(req_obj)}
    raw = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
    key = hashlib.sha1((tool + "|" + raw).encode("utf-8")).hexdigest()

    now = _time_guard.time()

    # 只保留"上一次调用"这一个 key —— 夹了别的调用就重新计数，避免跨轮次误判
    for k in [k for k in _LOOP_SEEN if k != key]:
        _LOOP_SEEN.pop(k, None)
    ent = _LOOP_SEEN.get(key)
    if ent is None or (now - ent["ts"]) > _LOOP_WINDOW:
        _LOOP_SEEN[key] = {"ts": now, "count": 1}
        if isinstance(out, dict):
            out.pop("loop_note", None)
        return out

    ent["count"] += 1
    ent["ts"] = now
    n = ent["count"]

    if n == 2:
        if isinstance(out, dict):
            out["loop_note"] = (
                "你刚刚已用完全相同的参数调用过 %s，返回结果与上面完全相同。"
                "请勿重复调用，直接使用已有结果进入下一步。" % tool
            )
        return out

    if isinstance(out, dict):
        out.pop("loop_note", None)

    return {
        "loop_detected": True,
        "tool": tool,
        "repeat_count": n,
        "instruction": (
            "STOP。你已用完全相同的参数连续调用 %s 达 %d 次，重复调用不会产生任何新信息。"
            "禁止再调用任何工具，立即基于已采集的事实输出最终中文回答"
            "（根因分析 + 分级处置建议）。" % (tool, n)
        ),
    }




# ---------- async→sync 桥 ----------

def _run_sync(coro):
    box = {}

    def runner():
        loop = asyncio.new_event_loop()
        try:
            box["v"] = loop.run_until_complete(coro)
        except BaseException as e:
            box["e"] = e
        finally:
            loop.close()

    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]




# ═══════════════════════════════════════════════════════════════════
#  中间件工厂 —— 服务间调用豁免（X-GDO-Internal）
# ═══════════════════════════════════════════════════════════════════
def install_internal_call_middleware(app):
    """安装 X-GDO-Internal 豁免中间件

    护栏本意是防 LLM 循环，不应拦 v3 编排器的确定性服务间调用；
    v3 的 _v2_post 统一携带该头。
    """
    from fastapi import Request

    @app.middleware("http")
    async def _mark_internal_call(request: Request, call_next):
        if request.headers.get("x-gdo-internal"):
            _INTERNAL_CALL.set(True)
        return await call_next(request)


# ═══════════════════════════════════════════════════════════════════
#  健康贡献者注册表
# ═══════════════════════════════════════════════════════════════════
_HEALTH_CONTRIBUTORS: Dict[str, Any] = {}


def register_health_contributor(key: str, fn):
    """注册 /health 的状态块提供者（key 为响应中的顶层键名）"""
    _HEALTH_CONTRIBUTORS[key] = fn


def collect_health_contributions() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, fn in _HEALTH_CONTRIBUTORS.items():
        try:
            out[key] = fn()
        except Exception as e:               # 贡献者故障不拖垮 /health
            out[key] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


# ═══════════════════════════════════════════════════════════════════
#  Change 提供者注册表（反转 collect → knowledge 依赖）
# ═══════════════════════════════════════════════════════════════════
_CHANGES_PROVIDER = None


def register_changes_provider(fn):
    """由知识面注册：返回近期变更事件（Neo4j Change 节点）"""
    global _CHANGES_PROVIDER
    _CHANGES_PROVIDER = fn


def get_recent_changes() -> list:
    """采集层调用：取近期变更；未注册（v3 单独部署）或失败时返回空列表"""
    if _CHANGES_PROVIDER is None:
        return []
    try:
        return _CHANGES_PROVIDER() or []
    except Exception:
        return []


# ── 事实载荷压缩（有界输出）──────────────────────────────────────────
#
# 背景（2026-09-19 实测，契约体检 C2）：
#   gather(gaussdb,tpops)   535 条事实 ≈ 2.9 万 tokens
#   gather(四层)           3167 条事实 ≈ 16.6 万 tokens
#   run_rules 把全量事实塞进**每条**命中规则（evidence_facts），
#   10 条命中 × 3167 条 = **164 万 tokens**，且各规则的证据完全相同
#   （Jaccard=1，零区分度）。
#   ReAct 每轮迭代都会重发历史，所以这不是"大"，而是"必然撑爆上下文"——
#   实测后果是模型网关读超时、Agent 无法完成诊断。
#
# 对策：**逐路径聚合**（同一路径只出一条，附取值列表与条数），
#   并把全局常量（source/ts/obj_type）提到顶层或压缩到值内部。
#   实测 3.4×~10.7× 压缩，之后仍能把上下文控制在预算内。
#
# ⚠ 与"少采"的边界：只压缩**表示**，不减少**信息**——
#   同路径的全部取值、对象 ID、来源与时间都保留（仅对超长字符串截断并**记账**）。
#   判据靠的是"任一值满足即成立"（多值语义），所以**绝不截断值列表**，
#   除非调用方显式放宽上限。
# ── 有界聚合的默认上限（2026-09-19 契约体检 C2 实测标定）──
#   上限不是拍脑袋：四层采集实测 3,166 条事实 / 63,308 字节聚合体，
#   写成 JSON 后 ≈20,112 tokens —— 刚过 20,000 预算线。收紧一档后进预算，
#   且仍保留每个路径的全部取值（cap 只在单路径取值 >12 时才截断并记账）。
_AGG_DEFAULT_CAP_VALUES = 8      # 单路径最多保留多少个不同取值
_AGG_DEFAULT_CAP_OBJS = 4        # 单路径最多保留多少个对象 ID
_AGG_MAX_STR = 120               # 单个字符串值的长度上限（超出截断并标注丢弃量）


def compress_facts(facts, cap_values: int = _AGG_DEFAULT_CAP_VALUES,
                   cap_objs: int = _AGG_DEFAULT_CAP_OBJS,
                   max_str: int = _AGG_MAX_STR):
    """把逐条事实压缩为「逐路径聚合」结构（有界载荷）

    返回 `(facts_by_path, meta)`：
      facts_by_path = {路径: {"n": 条数, "v": [取值…], "o": [对象ID…],
                             "ot": [对象类型…], "s": 来源, "t": 时间}}
      仅在路径内该字段唯一时才写 s/t；全局唯一时由 meta 承载。
    """
    facts = list(facts or [])
    by_path = {}
    order = []
    sources, times = set(), set()
    dropped_values = 0
    for f in facts:
        p = f.get("path", "")
        if p not in by_path:
            by_path[p] = {"n": 0, "v": [], "o": [], "_ot": set(), "_s": set(), "_t": set()}
            order.append(p)
        e = by_path[p]
        e["n"] += 1
        v = f.get("value")
        if isinstance(v, str) and len(v) > max_str:
            v = v[:max_str] + f"…(+{len(v) - max_str})"
        if v not in e["v"]:
            if len(e["v"]) < cap_values:
                e["v"].append(v)
            else:
                dropped_values += 1
        o = f.get("obj_id")
        if o is not None and o not in e["o"] and len(e["o"]) < cap_objs:
            e["o"].append(o)
        e["_ot"].add(str(f.get("obj_type")))
        e["_s"].add(str(f.get("source")))
        e["_t"].add(str(f.get("ts")))
        sources.add(str(f.get("source")))
        times.add(str(f.get("ts")))

    hoist_src = len(sources) == 1
    hoist_ts = len(times) == 1
    out = {}
    for p in order:
        e = by_path[p]
        item = {"n": e["n"], "v": e["v"]}
        if e["o"]:
            item["o"] = e["o"]
        if len(e["_ot"]) > 1:
            item["ot"] = sorted(e["_ot"])
        if not hoist_src and len(e["_s"]) == 1:
            item["s"] = next(iter(e["_s"]))
        if not hoist_ts and len(e["_t"]) == 1:
            item["t"] = next(iter(e["_t"]))
        out[p] = item

    meta = {
        "facts_total": len(facts),
        "paths_returned": len(out),
        "facts_dropped": len(facts) - len(out),
        "aggregated": True,
    }
    if hoist_src and sources:
        meta["source"] = next(iter(sources))
    if hoist_ts and times:
        meta["ts"] = next(iter(times))
    if dropped_values:
        # 显式记账：截断不得静默（否则"没报"会被读成"没问题"）
        meta["values_truncated"] = dropped_values
    return out, meta


def slim_evidence(facts, paths, max_str: int = _AGG_MAX_STR):
    """按事实路径筛证据（用于规则命中：只回与判定相关的路径）

    与 compress_facts 的差别：这里保留**逐条**形状（path/value/obj_id），
    因为调用方要把它喂回规则引擎（多值语义要求逐条取值）。
    """
    want = set(paths or ())
    out = []
    for f in facts or []:
        if f.get("path") not in want:
            continue
        v = f.get("value")
        if isinstance(v, str) and len(v) > max_str:
            v = v[:max_str] + f"…(+{len(v) - max_str})"
        e = {"path": f.get("path"), "value": v}
        if f.get("obj_id") is not None:
            e["obj_id"] = f.get("obj_id")
        out.append(e)
    return out


def fact_paths_in_when(when) -> set:
    """抽出一条规则 `when` 里引用的全部事实路径（含 all/any 递归）"""
    acc = set()
    _walk_when_paths(when, acc)
    return acc


def _walk_when_paths(when, acc: set):
    if not isinstance(when, dict):
        return
    if "fact" in when:
        acc.add(when["fact"])
    for k in ("all", "any"):
        for c in when.get(k) or []:
            _walk_when_paths(c, acc)
