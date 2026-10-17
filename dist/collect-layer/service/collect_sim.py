# -*- coding: utf-8 -*-
"""采集面打桩（故障模拟）—— 出口层 mock，供全量模拟演练

## 为什么打在「出口层」

演练的目的是「改代码后不需要真实故障注入就能全量跑一遍」。那么**被跳过的代码就是盲区**。
历史上有三种做法，本模块明确只选第三种：

| 做法 | 探针代码 | 判定 |
|---|---|---|
| 事实层注入（`legacy/.../collector/mock.py` 的老做法） | **完全不执行** | ✗ 段名归一 / SQL 遍历 / 字段映射 / D2 段级失败 / opt-in 过滤全部未覆盖 |
| 假采集服务（另起一个假 :8187） | **完全不执行** | ✗ 只测到 v3 侧编排 |
| **出口层**（本模块） | **全执行** | ✓ 除"外部世界的应答"外，一切真实跑过 |

## 五处出口（2026-09-20 实测清点）

| 层 | 出口 | 段数 | 本模块的 tap |
|---|---|---|---|
| gaussdb | `async_gaussdb.connect()` → `conn.fetch(sql)` | 21 | `connect_async()` |
| os | `subprocess.check_output(_ssh_cmd(host, cmd), shell=True)` | 13 | `os_check_output()` |
| storage | `DoradoCollector._session.request/post/delete` | 11 个 `collect_*` | `http()` |
| tpops | `_http()` ＋ `login()` 走裸 `http.client`（**双出口**） | 见探针 | `http()` ＋ `raw_http_headers()` |
| ES | `es_log_search._es_request` ＋ `collect_api` 的 `requests.post`（**双出口**） | 2 | `http()` |

## 三种模式（沿用本仓库 `GDO_*` 开关约定）

    GDO_COLLECT_SOURCE = live | record | replay      # 默认 live
    GDO_SIM_DIR        = var/sim                      # 数据根目录
    GDO_SIM_RUN        = <录制批次名>                  # record 时用
    GDO_SIM_SCENARIO   = <场景名>                      # replay 时用

- `live`：**零介入**。所有 tap 直接调用真实实现，不读盘、不算指纹、import 不加载存储。
- `record`：真实采集 **并** 把每次出口响应落盘（JSONL）。
- `replay`：**不触碰任何真实出口**，只从场景存储应答。

## fail-closed 三件套（安全底线）

1. **未命中即报错**（`SimMiss`），**绝不返回空**。返回空会让演练全绿而掩盖真实缺口 ——
   本项目 V14/V15/V18 三条门禁都是为抓这类静默失效而建。
2. **replay 下 tap 永不调用真实实现**，另加 `install_network_guard()` 在 socket 层再拦一道
   （防"漏打桩的出口悄悄走真实环境"）。
3. **来源可辨**：`provenance()` 供 `/health` 与响应体标注，使演练数据**不可能被误当真实数据**。

## 指纹用「段标识 + 内容摘要」而不是次序

按次序回放会在探针调整顺序或并发时**静默错位**（把 A 段的响应喂给 B 段），
这是最难发现的一类假绿。按指纹匹配则顺序无关；且**探针改了 SQL/命令 ⇒ 指纹变 ⇒ 演练立刻红**，
这本身就是一条高价值的契约门禁（`docss/60` Q7 采纳的背景）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════════
class SimMiss(RuntimeError):
    """演练模式下某个出口**没有**对应录制数据。

    ★ 这是**刻意**的硬失败，不是缺省值。理由：这条出口在当前场景下"是什么"是未知的，
      用空值顶替就把「不知道」伪装成「本来就是空」—— 演练会全绿，而生产会踩坑。
      正确做法：要么补录这一出口，要么在场景里用 `declare_empty` 显式声明它确实为空。
    """


class SimConfigError(RuntimeError):
    """演练配置本身有问题（如 replay 未指定场景名）"""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# ═══════════════════════════════════════════════════════════════════
#  运行时开关（生产可控开口）—— `<SIM_DIR>/runtime.json`
# ═══════════════════════════════════════════════════════════════════
# 为什么需要它（docs/62 §三）：
#   生产上切换采集源**不能让服务重启** —— 靠改环境变量就得重启，那是停机操作。
#   故把「采集源」从"启动时固定"提升为"运行时可变"，由控制平面写一个状态文件，
#   数据平面每次出口调用前读它。
#
# ★ fail-safe 方向必须明确：**任何异常一律回落 live**（真实采集）。
#   反方向（异常时停在模拟）在生产上是灾难 —— 值班会以为在看真实数据。
#
# ★ 安全八条护栏中由本模块承担的部分：
#   ① 默认 live；文件缺失/损坏/越界值 ⇒ live
#   ② replay 必须带场景名，否则不生效（回落 live）并记录原因
#   ④ 落盘审计（谁/何时/从什么切到什么/原因）
#   ⑤ 自动回落（到期由读侧惰性判定并写回 live，不需要后台线程）
RUNTIME_FILE = "runtime.json"
AUDIT_FILE = "audit.jsonl"
_MODES = ("live", "record", "replay")

_RT: Dict[str, Any] = {"sig": None, "data": {}}
_RT_LOCK = threading.Lock()


def _runtime_path() -> str:
    return os.path.join(sim_dir(), RUNTIME_FILE)


def read_runtime(force: bool = False) -> dict:
    """读运行时状态。**任何异常都回落 live**（fail-safe）。

    按 `(mtime_ns, size)` 缓存 —— 与 `empty_exits.json` 同一套机制
    （只按路径缓存会读不到"文件刚被改"这件事，见 docs/61 §2.4b）。
    """
    p = _runtime_path()
    try:
        st = os.stat(p)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {}                       # 无文件 ⇒ 用环境变量/默认
    with _RT_LOCK:
        if not force and _RT.get("sig") == sig:
            return dict(_RT.get("data") or {})
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("runtime.json 不是对象")
    except Exception as e:
        # 损坏 ⇒ 回落 live，并把原因记进状态（不静默）
        d = {"mode": "live", "_error": f"runtime.json 解析失败: {type(e).__name__}"}
    with _RT_LOCK:
        _RT["sig"] = sig
        _RT["data"] = dict(d)
    return dict(d)


def _valid(mode: str, scen: str) -> Tuple[bool, str]:
    """护栏②：replay 必须带场景名；模式必须在白名单内"""
    m = (mode or "").strip().lower()
    if m not in _MODES:
        return False, f"模式非法: {mode!r}（只允许 {'/'.join(_MODES)}）"
    if m == "replay" and not (scen or "").strip():
        return False, "replay 必须显式指定场景（空场景会让所有出口未命中）"
    return True, ""


def effective() -> Tuple[str, str, dict]:
    """解析**实际生效**的模式与场景，返回 (mode, scenario, raw_state)

    优先级：runtime.json > 环境变量 > live
    自动回落（护栏⑤）在此**惰性判定**：到期即写回 live 并返回 live。
    """
    rt = read_runtime()
    m = str(rt.get("mode") or "").strip().lower()
    sc = str(rt.get("scenario") or "").strip()
    if m:
        ok, why = _valid(m, sc)
        if not ok:
            return "live", "", dict(rt, _fallback=why)
        # 自动回落
        if m != "live":
            at = rt.get("auto_revert_at")
            if at:
                try:
                    if datetime.now() >= datetime.fromisoformat(str(at)):
                        set_runtime("live", by="auto-revert",
                                    reason=f"到达自动回落时间 {at}（原模式 {m}）")
                        return "live", "", {"mode": "live", "_auto_reverted": True,
                                            "_from": m, "_at": at}
                except Exception:
                    pass                # 时间解析失败 ⇒ 不回落（宁可不自动，也不乱切）
        return m, sc, rt
    # 回落环境变量
    em = _env("GDO_COLLECT_SOURCE", "live").lower()
    if em not in _MODES:
        em = "live"
    return em, _env("GDO_SIM_SCENARIO"), {"mode": "", "_from_env": em}


def set_runtime(mode: str, scenario: str = "", *, by: str = "", reason: str = "",
                auto_revert_minutes: Optional[float] = None) -> dict:
    """写运行时状态（**控制平面调用**）。返回写入后的状态 dict。"""
    m = (mode or "").strip().lower()
    ok, why = _valid(m, scenario)
    if not ok:
        raise SimConfigError(why)
    prev = read_runtime()
    at = None
    if auto_revert_minutes is not None:
        try:
            mins = float(auto_revert_minutes)
            if mins > 0:
                at = (datetime.now() + timedelta(minutes=mins)).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            at = None
    if m == "live":
        at = None                       # 回 live 不需要回落时间
    state = {
        "mode": m,
        "scenario": scenario if m == "replay" else "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_by": by or "unknown",
        "reason": reason or "",
        "auto_revert_at": at,
    }
    d = sim_dir()
    os.makedirs(d, exist_ok=True)
    p = _runtime_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)                  # 原子替换：数据平面永远不会读到半个文件
    read_runtime(force=True)
    append_audit({"event": "switch",
                  "from": {"mode": prev.get("mode") or "(env)", "scenario": prev.get("scenario")},
                  "to": {"mode": m, "scenario": state["scenario"]},
                  "by": state["updated_by"], "reason": state["reason"],
                  "auto_revert_at": at})
    return state


def clear_runtime(*, by: str = "", reason: str = "") -> dict:
    """删除运行时状态 ⇒ 回到环境变量/live 的默认行为"""
    p = _runtime_path()
    prev = read_runtime()
    try:
        os.unlink(p)
        existed = True
    except OSError:
        existed = False
    read_runtime(force=True)
    append_audit({"event": "clear", "from": prev, "to": {"mode": "(env/default)"},
                  "by": by or "unknown", "reason": reason or ""})
    return {"removed": existed}


def append_audit(entry: dict) -> None:
    """护栏④：审计落盘（失败不影响主流程，但会留痕在 last_audit_error）"""
    try:
        d = sim_dir()
        os.makedirs(d, exist_ok=True)
        e = dict(entry)
        e["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(d, AUDIT_FILE), "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    except Exception as ex:
        _RT["audit_error"] = f"{type(ex).__name__}: {ex}"[:200]


def read_audit(limit: int = 50) -> List[dict]:
    p = os.path.join(sim_dir(), AUDIT_FILE)
    if not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        return []
    return out[-max(1, int(limit)):]


def paths_selfcheck() -> dict:
    """护栏 R2/R3：两侧路径自检 —— 数据平面解析到的**绝对路径**必须可读/可建"""
    d = sim_dir()
    info = {"sim_dir": d, "exists": os.path.isdir(d), "runtime_file": _runtime_path(),
            "writable": False, "scenarios": 0, "recordings": 0}
    try:
        os.makedirs(d, exist_ok=True)
        info["exists"] = True
        probe = os.path.join(d, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        info["writable"] = True
    except Exception as e:
        info["write_error"] = f"{type(e).__name__}: {e}"[:160]
    for sub, key in (("scenarios", "scenarios"), ("recordings", "recordings")):
        p = os.path.join(d, sub)
        if os.path.isdir(p):
            info[key] = len([x for x in os.listdir(p)
                             if os.path.isdir(os.path.join(p, x))])
    return info


def mode() -> str:
    return effective()[0]


def scenario() -> str:
    return effective()[1]


def sim_dir() -> str:
    d = _env("GDO_SIM_DIR")
    if d:
        return os.path.abspath(d)
    # 默认 <repo>/var/sim（本文件在 <repo>/service/ 下）
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo, "var", "sim")


# 时间模式：录制时间戳如何处理
TIME_AS_RECORDED = "as_recorded"
TIME_REBASE = "rebase"


# ═══════════════════════════════════════════════════════════════════
#  指纹
# ═══════════════════════════════════════════════════════════════════
_WS = re.compile(r"\s+")


def _canon(text: str) -> str:
    """规范化文本用于指纹：折叠空白、去首尾。

    ★ 只做**无损**规范化（不改大小写、不去标点）—— 指纹必须只依赖内容，
      任何"看起来更整洁"的加工都会让两个不同的调用撞成同一个键。
    """
    return _WS.sub(" ", (text or "").strip())


def _h(text: str, n: int = 16) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:n]


def fingerprint_sql(sql: str) -> str:
    return "sql:" + _h(_canon(sql))


def fingerprint_cmd(cmd: str) -> str:
    return "cmd:" + _h(_canon(cmd))


def fingerprint_http(method: str, url: str, body: Any = None) -> str:
    """HTTP 指纹 = 方法 + 规范化 URL +（有 body 时）body 摘要

    ★ body 必须进指纹：分页/过滤靠 body 区分，忽略它会把
      "第1页"的响应喂给"第2页"（静默错位）。

    ★ 但**敏感字段必须先脱敏**（实测踩出）：登录体形如
      `{"username": "admin", "password": "<真实密码>", "scope": 0}` ——
      原样进指纹有两个后果：
        ① 场景**必须知道密码才能命中** ⇒ 换密码/换环境后所有存储场景失效
        ② 指纹（虽不可逆）间接编码了凭据，且录制里会留下它
      故对 `password/token/secret/passwd/key` 之类的键**只保留键名、值替换为占位符**。
      脱敏不影响分页区分（分页字段不是敏感字段）。
    """
    key = "%s %s" % (method.upper(), _canon(url))
    if body is not None:
        try:
            key += " " + json.dumps(_redact(body), sort_keys=True,
                                    ensure_ascii=False, default=str)
        except Exception:
            key += " " + str(_redact(body))
    return "http:" + _h(key)


# 敏感字段名（命中即脱敏）：登录/令牌/密钥类
_SENSITIVE = ("password", "passwd", "pwd", "token", "secret", "apikey",
              "api_key", "authorization", "credential", "private_key")
_REDACTED = "***REDACTED***"


def _redact(obj: Any) -> Any:
    """递归把敏感键的值替换为占位符（**键名保留**，故不影响结构性区分）"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SENSITIVE):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


# ═══════════════════════════════════════════════════════════════════
#  存储（JSONL：一行一条，便于增量追加与人工查看）
# ═══════════════════════════════════════════════════════════════════
class Store:
    """录制/场景存储。线程安全（采集服务在 threadpool 上跑）。"""

    def __init__(self, path: str, writable: bool = False):
        self.path = path
        self.writable = writable
        self._lock = threading.Lock()
        self._data: Optional[Dict[str, dict]] = None
        self._fh = None

    # ── 读 ──
    def load(self) -> Dict[str, dict]:
        if self._data is not None:
            return self._data
        with self._lock:
            if self._data is not None:
                return self._data
            d: Dict[str, dict] = {}
            if os.path.isfile(self.path):
                with open(self.path, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln:
                            continue
                        try:
                            e = json.loads(ln)
                        except Exception:
                            continue        # 单行损坏不影响其余（录制是追加写的）
                        if e.get("key"):
                            d[e["key"]] = e
            self._data = d
            return d

    def get(self, key: str) -> Optional[dict]:
        return self.load().get(key)

    def keys(self) -> List[str]:
        return sorted(self.load())

    # ── 写 ──
    def append(self, entry: dict) -> None:
        if not self.writable:
            return
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if self._fh is None:
                self._fh = open(self.path, "a", encoding="utf-8")
            self._fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
            if self._data is not None:
                self._data[entry["key"]] = entry

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


# ── 进程级单例（每模式一个实例）──
_STATE: Dict[str, Any] = {}
_STATE_LOCK = threading.Lock()


def _store() -> Optional[Store]:
    """按当前模式取存储；live 返回 None（**零介入**）"""
    m = mode()
    if m == "live":
        return None
    # ★ 缓存键必须**含场景名**，不能只用模式。
    #   反面教材：键只有模式 ⇒ 切场景（os_healthy → os_severe）后仍复用旧 Store
    #   ⇒ 健康档场景采出的是**故障档的值**，而且**不报任何错**
    #   （演练结果张冠李戴，是本设施最危险的一类失效）。
    key = (m, scenario() if m == "replay" else _env("GDO_SIM_RUN") or "")
    with _STATE_LOCK:
        st = _STATE.get(key)
        if st is not None:
            return st
        if m == "replay":
            sc = scenario()
            if not sc:
                raise SimConfigError(
                    "GDO_COLLECT_SOURCE=replay 但未指定 GDO_SIM_SCENARIO —— "
                    "拒绝在不知道用哪个场景的情况下启动（fail-closed）")
            st = Store(os.path.join(sim_dir(), "scenarios", sc, "store.jsonl"))
        else:  # record
            run = _env("GDO_SIM_RUN") or datetime.now().strftime("run-%Y%m%dT%H%M%S")
            st = Store(os.path.join(sim_dir(), "recordings", run, "recorded.jsonl"),
                       writable=True)
        # 上层切换时旧 Store 必须显式关闭（否则文件句柄随切换次数泄漏）
        for _k, _old in list(_STATE.items()):
            if _k[0] == m and _k != key:
                try:
                    _old.close()
                except Exception:
                    pass
                _STATE.pop(_k, None)
        _STATE[key] = st
        return st


def reset_state() -> None:
    """测试用：清掉单例与运行时缓存（改环境变量/状态文件后需调用）"""
    with _STATE_LOCK:
        for s in _STATE.values():
            try:
                s.close()
            except Exception:
                pass
        _STATE.clear()
    _NET_GUARD["installed"] = False
    with _RT_LOCK:
        _RT["sig"] = None
        _RT["data"] = {}
    _DECLARED_EMPTY_LOADED.clear()
    _DECLARED_EMPTY.clear()
    _MISSES.clear()
    with _SERVED_LOCK:
        _SERVED.clear()


def provenance() -> dict:
    """当前数据来源 —— 必须出现在 /health 与响应体里，使演练数据不可被误当真实"""
    m, sc, raw = effective()
    return {
        "collect_source": m,
        "simulated": m != "live",
        "scenario": sc or None,
        "run": _env("GDO_SIM_RUN") or None if m == "record" else None,
        "sim_dir": sim_dir() if m != "live" else None,
        # ★ 运行时开关状态：让"这个模式是怎么来的"可追溯
        "switched_at": raw.get("updated_at"),
        "switched_by": raw.get("updated_by"),
        "auto_revert_at": raw.get("auto_revert_at"),
        "from_env": raw.get("_from_env"),
        "fallback": raw.get("_fallback"),
        "auto_reverted": raw.get("_auto_reverted"),
        "runtime_error": raw.get("_error"),
        "audit_error": _RT.get("audit_error"),
    }


# ═══════════════════════════════════════════════════════════════════
#  1) GaussDB：连接对象打桩
# ═══════════════════════════════════════════════════════════════════
class _SimConn:
    """replay 用的假连接：只实现探针真正用到的 `fetch` / `close`"""

    def __init__(self, store: Store):
        self._store = store

    async def fetch(self, sql: str) -> List[dict]:
        resp = _lookup(self._store, fingerprint_sql(sql),
                       kind="sql", detail=_canon(sql))
        if resp is None:
            return []          # 场景显式声明为空
        return resp

    async def close(self) -> None:
        return None


class _RecConn:
    """record 用的包装连接：转发真实 conn，并把每次 fetch 落盘"""

    def __init__(self, real, store: Store):
        self._real = real
        self._store = store

    async def fetch(self, sql: str) -> List[dict]:
        rows = await self._real.fetch(sql)
        try:
            self._store.append({
                "key": fingerprint_sql(sql),
                "kind": "sql",
                "meta": {"sql": _canon(sql), "rows": len(rows or []),
                         "at": datetime.now().isoformat(timespec="seconds")},
                "resp": [dict(r) for r in (rows or [])],
            })
        except Exception:
            pass               # 录制失败不影响采集本身
        return rows

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def close(self) -> None:
        return await self._real.close()


async def connect_async(real_connect: Callable, conn_kwargs: dict):
    """GaussDB 连接的入口 tap。

    live    → 真实连接
    record  → 真实连接 + 录制包装
    replay  → 假连接（不建立任何真实连接）
    """
    m = mode()
    if m == "live":
        return await real_connect(**conn_kwargs)
    st = _store()
    if m == "record":
        return _RecConn(await real_connect(**conn_kwargs), st)
    return _SimConn(st)


# ═══════════════════════════════════════════════════════════════════
#  2) OS：子进程打桩
# ═══════════════════════════════════════════════════════════════════
def os_check_output(full_cmd: str, real_call: Callable[[], Any], *,
                    key_hint: str = "") -> Any:
    """`subprocess.check_output` 的 tap（OS 层 SSH 命令）

    `key_hint` = **内层命令**（不含 sshpass/主机 IP）。

    ★ 为什么必须用 key_hint 而不是整条命令做键（实测踩出）：
      整条命令形如 `sshpass -p '***' ssh ... root@192.0.2.12 <cmd>` ——
      把主机 IP、密码、端口都编进指纹，后果有两层：
        ① 场景**绑死在某个节点 IP** 上：换机/换密码 ⇒ 全部场景 miss（不可移植）
        ② 指纹里混入了**凭据**（密码明文进指纹，虽不可逆但仍不该）
      按内层命令做键后，同一场景可用于任何节点（"某节点磁盘满"与是哪台无关）。

    live   → 真实执行
    record → 真实执行 + 录制 stdout
    replay → 只回放录制 stdout（**不启动任何子进程**）
    """
    m = mode()
    if m == "live":
        return real_call()
    st = _store()
    key = fingerprint_cmd(key_hint or full_cmd)
    if m == "record":
        out = real_call()
        try:
            st.append({
                "key": key, "kind": "cmd",
                "meta": {"cmd": _canon(key_hint or full_cmd),
                         "at": datetime.now().isoformat(timespec="seconds")},
                "resp": out.decode("utf-8", errors="replace")
                if isinstance(out, (bytes, bytearray)) else out,
            })
        except Exception:
            pass
        return out
    resp = _lookup(st, key, kind="cmd", detail=_canon(key_hint or full_cmd))
    if resp is None:
        resp = ""
    # 真实调用返回 bytes（check_output 默认）；保持一致以免调用方解码分支走偏
    return resp.encode("utf-8") if isinstance(resp, str) else resp


# ═══════════════════════════════════════════════════════════════════
#  3) HTTP：headers+body 打桩（storage / tpops / ES 共用）
# ═══════════════════════════════════════════════════════════════════
def http(method: str, url: str, real_call: Callable[[], Any], *,
         body: Any = None, kind: str = "http") -> Any:
    """通用 HTTP tap（低层）。

    `real_call()` 的返回值必须**可 JSON 序列化**（record 要落盘）。
    需要 `requests.Response` 语义的调用方请用 `http_requests()`。

    ★ `url` 一律按**与部署无关的稳定标识**传入（如端点名 `sessions`），
      不要传真实 URL —— 真实 URL 含设备 IP 与 device_id ⇒ 场景会绑死在
      某个阵列上，换 IP/换阵列后全部 miss。调用方负责只传稳定标识
      （即 key_hint）。

    ★ 双出口注意：TPOPS 登录用裸 `http.client`、ES 有两个调用点 ——
      它们都走这里，于是**共用同一份录制与同一套 fail-closed**，
      不留下"某个出口忘了打桩"的缺口。
    """
    m = mode()
    if m == "live":
        return real_call()
    st = _store()
    key = fingerprint_http(method, url, body)
    if m == "record":
        out = real_call()
        try:
            st.append({
                "key": key, "kind": kind,
                "meta": {"method": method.upper(), "url": _canon(url),
                         "body": body,
                         "at": datetime.now().isoformat(timespec="seconds")},
                "resp": out,
            })
        except Exception:
            pass
        return out
    _res = _lookup(st, key, kind=kind, detail="%s %s" % (method.upper(), _canon(url)))
    if _res is None:
        # HTTP 出口的"不存在"在语义上可表达为 404 —— 与 SQL 的"未知行集"不同，
        # 故不抛 SimMiss；但**显式记账**，由 /health 与演练报告暴露（不静默）。
        _record_miss(key, kind, "%s %s" % (method.upper(), _canon(url)))
        return (404, {})
    _audit_served(kind, key)
    return _as_status_payload(_res)


def _as_status_payload(resp: Any) -> Any:
    """把录制里的 `[status, payload]` 归一成 **tuple**

    ★ 为什么必须归一（实测踩出）：录制落盘是 JSON，**tuple 会变 list**；
      而所有调用方都用 `isinstance(x, tuple)` 判断"这是 (状态码, 负载)"。
      不归一 ⇒ 调用方把整个 `[200, {...}]` 当成 payload 往下传 ⇒
      `body.get()` 炸在 list 上（存储层登录静默失败、探针退化 local_disk）。
      这一处不修，**所有 HTTP 出口的回放都是坏的**。
    """
    if isinstance(resp, list) and len(resp) == 2 and isinstance(resp[0], int):
        return (resp[0], resp[1])
    return resp


class FakeResponse:
    """replay 下替身 `requests.Response`：只实现采集代码真正用到的三个成员

    ★ 存在的理由：录制必须可 JSON 序列化，而 `requests.Response` 不行。
      故 record 时把响应归一成 `(status, json_payload)`，replay 时再组装回来。
    """

    def __init__(self, status: int, payload: Any, url: str = ""):
        self.status_code = int(status or 200)
        self._payload = payload
        self.url = url
        try:
            self.text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            self.text = str(payload)

    def json(self) -> Any:
        return self._payload if self._payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            try:
                import requests as _rq
                raise _rq.HTTPError("simulated HTTP %d for %s"
                                    % (self.status_code, self.url))
            except ImportError:
                raise RuntimeError("simulated HTTP %d" % self.status_code)

    # 少量代码会读 resp.headers.get(...)；给个空壳避免 AttributeError
    headers: Dict[str, str] = {}


def _norm_response(resp: Any) -> Tuple[int, Any]:
    """把 `requests.Response` 归一成可序列化的 `(status, json)`"""
    status = getattr(resp, "status_code", 200)
    try:
        payload = resp.json()
    except Exception:
        payload = {"__raw__": str(getattr(resp, "text", ""))[:4000]}
    return int(status), payload


def http_requests(call: Callable[[], Any], method: str, url: str, *,
                  body: Any = None, kind: str = "http",
                  key_hint: str = "") -> Any:
    """给 `requests` 调用方的 tap：`call()` 返回 `requests.Response`

    `key_hint` = **与部署无关的稳定标识**（如端点名 `controller` / `disk`）。

    ★ 为什么必须能传 key_hint（实测踩出）：Storage 的 URL 形如
      `https://<DM_IP>:8088/deviceManager/rest/<device_id>/<endpoint>` ——
      把 DM IP 与 device_id 编进指纹，场景就**绑死在某个阵列**上（换 IP 全 miss）。
      按端点名做键后，「控制器 CPU 高」这个场景与是哪台阵列无关。

    live    → 原样返回真实 Response
    record  → 真实响应落盘（归一成 status+json），返回真实 Response
    replay  → 返回 `FakeResponse`（**不发起任何真实请求**）
    """
    if mode() == "live":
        return call()
    _k = key_hint or url
    out = http(method, _k, lambda: _norm_response(call()), body=body, kind=kind)
    if hasattr(out, "status_code") and hasattr(out, "json") and not isinstance(out, tuple):
        return out                      # record：真实 Response
    status, payload = out if isinstance(out, tuple) else (200, out)
    return FakeResponse(status, payload, url)


def _lookup(store: Store, key: str, *, kind: str, detail: str) -> Optional[Any]:
    e = store.get(key)
    if e is not None:
        _audit_served(kind, key)
        return e.get("resp")
    # 场景显式声明"该出口为空" ⇒ 合法返回空（这是**声明过的**空，不是沉默的空）
    _load_declared_empty(store.path)
    if (store.path, key) in _DECLARED_EMPTY:
        return None
    # SQL/命令出口：未命中就是硬失败
    if kind in ("sql", "cmd"):
        raise SimMiss(
            "演练数据缺失：%s 出口在场景中无对应录制。\n"
            "  key    = %s\n"
            "  内容   = %s\n"
            "  ⇒ 修法二选一：① 补录该出口（重新 record + build）；"
            "② 若本场景下它**确实为空**，在场景声明里加\n"
            "     empty_exits: [\"%s\"]\n"
            "     然后重建场景（会落成 <场景>/empty_exits.json）。\n"
            "  **不允许**用空值顶替：那会把『不知道』伪装成『本来就是空』，"
            "演练全绿而生产踩坑。" % (kind, key, detail[:400], key))
    _record_miss(key, kind, detail)
    return None


# ── 未命中记账（供 /health 与演练报告暴露）──
_MISSES: List[dict] = []


def _record_miss(key: str, kind: str, detail: str) -> None:
    if len(_MISSES) < 200:
        _MISSES.append({"key": key, "kind": kind, "detail": detail[:200],
                        "at": datetime.now().isoformat(timespec="seconds")})


def misses() -> List[dict]:
    return list(_MISSES)


# ── 演练服务记账（护栏④：演练期间"哪些出口被模拟应答过"必须可审计）──
#   ★ 与 misses 分开：misses = "该有的没有"，served = "确实用了模拟数据"。
#     生产演练复盘时最需要的是后者（证明当时确实在模拟、不是在真实采集）。
_SERVED: Dict[str, int] = {}
_SERVED_LOCK = threading.Lock()


def _audit_served(kind: str, key: str) -> None:
    with _SERVED_LOCK:
        _SERVED[kind] = _SERVED.get(kind, 0) + 1
    # 首次出现某个 key 时落一条审计（避免每条出口调用都写盘把日志撑爆）
    if not _SERVED.get("_seen:" + key):
        with _SERVED_LOCK:
            _SERVED["_seen:" + key] = 1
        append_audit({"event": "served", "kind": kind, "key": key,
                      "mode": mode(), "scenario": scenario()})


def served() -> dict:
    """已被模拟应答的出口统计（按 kind 计数 + 明细键）"""
    with _SERVED_LOCK:
        kinds = {k: v for k, v in _SERVED.items() if not k.startswith("_seen:")}
        keys = sorted(k[len("_seen:"):] for k in _SERVED if k.startswith("_seen:"))
    return {"by_kind": kinds, "total_exits": sum(kinds.values()), "keys": keys}


# 场景显式声明"本场景该出口为空"（避免 SimMiss 误报）
#   ★ 存储形态：`<sim_dir>/scenarios/<场景>/empty_exits.json` = {"keys": [...]}
#     由 build_scenario 从场景声明的 `empty_exits:` 落地。
#     **必须由场景声明驱动**，不能只靠代码调 declare_empty —— 否则 SimMiss 的报文
#     会给出一个"实际做不到"的修复建议（那比不提示更坏）。
_DECLARED_EMPTY: set = set()
_DECLARED_EMPTY_LOADED: Dict[str, bool] = {}


def declare_empty(keys: List[str]) -> None:
    """显式登记"这些出口在本场景下确实为空"（避免 SimMiss 误报）"""
    for k in keys or []:
        _DECLARED_EMPTY.add((_scenario_store_path(), k))


def _load_declared_empty(store_path: str = "") -> None:
    """从场景目录读 empty_exits.json

    ★ 缓存键必须是 **(store 路径, 文件 mtime, 文件大小)**：
      · 用"当前场景"当键 ⇒ 同时开两个 Store 会互相污染；
      · 只按路径缓存 ⇒ **场景被重建后仍读旧值**（实测踩过：测试里先建空场景、
        再建带声明的场景，第二次仍报 SimMiss —— 缓存说"已加载"）。
      用 mtime+size 让"文件变了"立刻生效，且不增加复杂度。
    """
    p = store_path or _scenario_store_path()
    if not p:
        return
    f = os.path.join(os.path.dirname(p), "empty_exits.json")
    try:
        st = os.stat(f)
        sig = (p, st.st_mtime_ns, st.st_size)
    except OSError:
        return                      # 文件不存在 ⇒ 没有任何声明
    if _DECLARED_EMPTY_LOADED.get(p) == sig:
        return
    _DECLARED_EMPTY_LOADED[p] = sig
    # 该路径的旧声明先清掉（否则重建场景后旧的空值声明会残留 —— 会放宽 fail-closed）
    for k in [x for x in _DECLARED_EMPTY if x[0] == p]:
        _DECLARED_EMPTY.discard(k)
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        for k in (d.get("keys") or []):
            _DECLARED_EMPTY.add((p, str(k)))
    except Exception:
        pass          # 声明文件坏了 ⇒ 退化为"没有声明"（更严，会报 SimMiss）


def _scenario_store_path() -> str:
    st = _STATE.get("replay")
    return st.path if st is not None else ""


# ═══════════════════════════════════════════════════════════════════
#  4) 网络守卫（replay 下的第二道闸）
# ═══════════════════════════════════════════════════════════════════
_NET_GUARD: Dict[str, Any] = {"installed": False}

_LOOPBACK = ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def install_network_guard(allow_hosts: Optional[List[str]] = None) -> bool:
    """replay/record 下拦截非白名单的真实网络连接

    为什么需要它：tap 只覆盖**已知的五处出口**。将来有人新加一个出口却忘了打桩，
    tap 不会报错，它会**安静地走真实环境** —— 这正是演练体系最危险的失效形态。
    这道守卫让它变成显式异常。

    ★ record 模式**不装**（它本来就要真实采集）；replay 装。
    """
    m = mode()
    if m == "live" or m == "record":
        return False
    if _NET_GUARD["installed"]:
        return True
    allow = set(_LOOPBACK) | {h.strip() for h in (allow_hosts or []) if h.strip()}
    # ★ 知识图库（GraphDB）是**判定链**，不是采集出口 —— 必须放行。
    #   本设施的原则是「只 mock 输入，判定链真实执行」（规则/本体/推理/收敛）。
    #   若不豁免：演练时规则引擎连不上图库 ⇒ 判定 0 条规则命中 ⇒
    #   整个演练失去判据（且会被误读成"没有故障"，比报错更坏）。
    #   只豁免**这一个已知的非采集出口**，不放宽整个守卫。
    for _e in (os.environ.get("GDO_GDB_URL"),):
        if _e:
            try:
                from urllib.parse import urlparse
                _h = urlparse(_e).hostname
                if _h:
                    allow.add(_h)
            except Exception:
                pass
    try:
        import socket
    except Exception:
        return False
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex

    def _allowed(addr) -> bool:
        host = addr[0] if isinstance(addr, (tuple, list)) and addr else str(addr)
        return str(host) in allow

    def _guard_connect(self, address):
        if not _allowed(address):
            raise SimMiss(
                "演练模式（%s）下试图建立真实网络连接 → %r。\n"
                "  ⇒ 该出口**未被打桩**。请为新出口接入 collect_sim 的 tap，"
                "或在 GDO_SIM_ALLOW_HOSTS 里显式放行。" % (m, address))
        return _real_connect(self, address)

    def _guard_connect_ex(self, address):
        if not _allowed(address):
            raise SimMiss("演练模式（%s）下试图建立真实网络连接 → %r" % (m, address))
        return _real_connect_ex(self, address)

    socket.socket.connect = _guard_connect
    socket.socket.connect_ex = _guard_connect_ex
    _NET_GUARD["installed"] = True
    return True


# ═══════════════════════════════════════════════════════════════════
#  5) 场景构建：基线 + 声明式补丁 → 可回放存储
# ═══════════════════════════════════════════════════════════════════
_UNIT_BYTES = {
    "b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4,
}
_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def to_bytes(value: Any) -> Any:
    """把语义量纲（"30MB" / "2GiB" / 1048576）转成字节整数。

    ★ 为什么必须支持这个：GaussDB 侧"复制延迟"这类字段的**量纲修复**正是本项目
      踩过的坑（P0-0d：`replay_lag_ms` 的值实为字节）。写场景时用字节语义值，
      可避免"再写一次错误量纲"。
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    m = _SIZE_RE.match(s)
    if not m:
        return value
    num, unit = m.group(1), m.group(2).lower().rstrip("ib").rstrip("b")
    if not unit:
        return float(num) if "." in num else int(num)
    mult = _UNIT_BYTES.get(unit) or _UNIT_BYTES.get(unit + "b")
    if not mult:
        return value
    v = float(num) * mult
    return int(v) if abs(v - round(v)) < 1e-9 else v


def _match(row: dict, where: dict) -> bool:
    for k, v in (where or {}).items():
        if row.get(k) != v:
            return False
    return True


def _apply_patch(entry: dict, patch: dict) -> Tuple[dict, str]:
    """把一条补丁应用到一条录制条目上；返回 (新条目, 人可读说明)"""
    op = (patch.get("op") or "set").lower()
    resp = entry.get("resp")
    fields = dict(patch.get("fields") or {})
    # 语义量纲转换
    for k, v in list(fields.items()):
        fields[k] = to_bytes(v)

    if op in ("set", "add_row", "drop_row") and isinstance(resp, list):
        where = patch.get("where") or patch.get("key") or {}
        if op == "add_row":
            resp = list(resp) + [dict(fields)]
            note = "新增 1 行（%s）" % ", ".join("%s=%s" % (k, v) for k, v in fields.items())
        else:
            n = 0
            out = []
            for row in resp:
                r = dict(row) if isinstance(row, dict) else row
                if isinstance(r, dict) and _match(r, where):
                    if op == "drop_row":
                        n += 1
                        continue
                    for k, v in fields.items():
                        r[k] = v
                    n += 1
                out.append(r)
            resp = out
            note = ("%s 匹配 %d 行" % ("修改" if op == "set" else "删除", n))
            if n == 0:
                note += " ⚠ 未匹配到任何行（补丁可能写错了 where）"
    elif op == "replace_resp":
        resp = patch.get("resp")
        note = "整体替换响应"
    elif op == "shift_time":
        # 显式时间平移：只动 fields 里点名的字段，且值为 ISO 字符串
        days = float(patch.get("offset_days") or 0)
        n = 0
        if isinstance(resp, list):
            for row in resp:
                if not isinstance(row, dict):
                    continue
                for k in fields:
                    raw = row.get(k)
                    if isinstance(raw, str) and len(raw) >= 19:
                        try:
                            dt = datetime.fromisoformat(raw[:19])
                        except Exception:
                            continue
                        row = row  # 就地修改（resp 已是深拷贝前的原对象，下方统一深拷贝）
                        row[k] = (dt + timedelta(days=days)).isoformat(sep=" ")
                        n += 1
        note = "时间平移 %s 天，命中 %d 个字段" % (days, n)
    else:
        raise ValueError("未知补丁 op: %s" % op)

    new = dict(entry)
    new["resp"] = resp
    new.setdefault("meta", {})
    new["meta"] = dict(new["meta"])
    new["meta"]["patched"] = note
    return new, note


def build_scenario(baseline_path: str, spec: dict, out_path: str) -> dict:
    """把「录制基线 + 声明式补丁」物化成可回放的场景存储

    ★ 为什么在**物化时**应用补丁而不是回放时：回放要尽量纯（查表即答），
      且物化产物可被人工查看与 diff —— 场景错了一眼能看出来。
    """
    store = Store(baseline_path)
    data = store.load()
    patches = list(spec.get("patches") or [])
    notes = []
    # 深拷贝，避免污染基线
    out: Dict[str, dict] = {}
    for k, e in data.items():
        out[k] = json.loads(json.dumps(e, ensure_ascii=False, default=str))

    for p in patches:
        target = p.get("target")            # 精确指定 fingerprint
        if target:
            if target not in out:
                notes.append("✗ 补丁目标不存在：%s" % target)
                continue
            out[target], note = _apply_patch(out[target], p)
            notes.append("%s ← %s" % (target, note))
            continue
        # 按 kind + 内容子串定位（写场景时人可读）
        sel_kind = p.get("kind")
        sel_contains = p.get("contains")
        hit = 0
        for k, e in list(out.items()):
            if sel_kind and e.get("kind") != sel_kind:
                continue
            if sel_contains:
                blob = json.dumps(e.get("meta") or {}, ensure_ascii=False)
                if sel_contains not in blob:
                    continue
            out[k], note = _apply_patch(e, p)
            notes.append("%s ← %s" % (k[:24], note))
            hit += 1
        if hit == 0:
            notes.append("✗ 补丁未命中任何条目（kind=%s contains=%s）"
                         % (sel_kind, sel_contains))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for k in sorted(out):
            f.write(json.dumps(out[k], ensure_ascii=False, default=str) + "\n")
    # ★ 把场景声明的 empty_exits 落成文件 —— 使 SimMiss 报文里给出的修法**真的可执行**
    #   （否则那是"做不到的建议"，比不提示更坏：人会照着做然后发现没用）
    _empty = [str(x) for x in (spec.get("empty_exits") or [])]
    with open(os.path.join(os.path.dirname(out_path), "empty_exits.json"), "w",
              encoding="utf-8") as f:
        json.dump({"keys": _empty}, f, ensure_ascii=False, indent=1)
    meta = {
        "name": spec.get("name"),
        "title": spec.get("title"),
        "based_on": os.path.basename(os.path.dirname(baseline_path)),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "entries": len(out),
        "empty_exits": _empty,
        "patch_notes": notes,
        "expect": spec.get("expect"),
    }
    with open(os.path.join(os.path.dirname(out_path), "meta.json"), "w",
              encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def shape_diff(a_path: str, b_path: str) -> List[str]:
    """比较两份录制的**结构**（键集合 + 行内字段名），不看值。

    用途：重新录制后确认"形状没变"（R1 mock 漂移的首要检查）。
    """
    def _keys(path: str):
        st = Store(path)
        out = {}
        for k, e in st.load().items():
            resp = e.get("resp")
            if isinstance(resp, list):
                cols = set()
                for row in resp[:50]:
                    if isinstance(row, dict):
                        cols |= set(row)
                out[k] = ("rows", tuple(sorted(cols)))
            elif isinstance(resp, dict):
                out[k] = ("obj", tuple(sorted(resp)))
            else:
                out[k] = ("scalar", type(resp).__name__)
        return out

    ka, kb = _keys(a_path), _keys(b_path)
    diffs = []
    for k in sorted(set(ka) | set(kb)):
        if k not in ka:
            diffs.append("新增条目 %s" % k)
        elif k not in kb:
            diffs.append("条目消失 %s" % k)
        elif ka[k] != kb[k]:
            diffs.append("形状变化 %s: %r → %r" % (k, ka[k], kb[k]))
    return diffs
