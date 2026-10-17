# -*- coding: utf-8 -*-
"""P4：IO 等待中继 → 事实（条件边的判据面）

## 为什么要有它（用户实测主诉 + 本轮实测推翻原方案）

用户主诉：「慢 SQL 可能是 SQL 本身问题（全表扫描）耗时在 CPU 侧，而不是 io_wait」。
P1 已给出 CPU 侧出口（`sql.cpu_time_ratio`）。P4 要补的是**当慢 SQL 真是 IO 类时，
能否证明它落到了存储链路** —— 这需要一条**可观测的中继**，不能凭「阵列有异常」就归因存储。

原方案写的是 `dorado_disk_slow ∧ io_wait_elevated ⇒ 慢SQL(IO类)`。
**实测推翻了前半个判据**（2026-09-24，生产 DeviceManager 只读）：
  · `performance` / `disk/0/performance` / `perfstatinfo` 返回**空**
  · `diskperformance` / `diskiostat` / `iostat` ⇒ **404**
  · `disk/0` 的 48 个字段里只有静态项（磨损度/带宽/转速），**无实时时延或 IOPS**
⇒ 盘级慢**从阵列侧观测不到**。照原方案写，`dorado_disk_slow` 就是一条
「声明了但产出无」的判据（本项目已犯过两次：`node.status`、慢 SQL 的 CPU 侧）。
⇒ 判据换血为**两侧都在采集面内**的组合：数据侧占比（P1）+ **IO 中继**（本模块）。

## ★ 中继为什么用「累计口径」而不是「活跃等待快照」

同一件事，两个口径给出**相反印象**（实测）：
  · 活跃等待快照（`pg_thread_wait_status` / ASP 的 `wait io`）：24h 内仅 **5** 条
    ⇒ 看起来「IO 没问题」
  · 累计口径（`dbe_perf.wait_events`）：`ShareStorageWalWrite` 累计 **1.34e10 μs**、
    峰值 14.2ms ⇒ IO 等待其实很大
本轮查证时我先用快照口径，差点得出「WAL/IO 中继不可观测」的结论。
**正是这个口径差**让存储规则当初能吃掉慢 SQL 的归因 —— 值得记在案上。

## ★ 三条腿：持续（avg）+ 峰值（max）+ 近期（ASP 采样）

- `avg_wait_time`：**持续**指标，但注意它是**实例生命周期均值**（非滑动窗口）⇒ 移动慢。
  这是本读数的**已知局限**，已在缺口登记里写明。
- `max_wait_time`：**峰值**，反映是否曾经严重卡顿（一次性事件也会留下痕迹）。
- ASP 采样计数：**近期**证据，补 avg 的「不敏感」。

## ★ 两条由实测逼出的排除纪律（2026-09-24）

### ① 固定周期等待必须排除出判据（否则是假阳性机器）

实测 `CkptWaitPageWriterSync`：`min_wait_time=100038` / `avg=100144` / `max=100869`（μs）。
**min 钉在 100ms** ⇒ 这是**固定周期超时等待**，不是负载导致的时延。
负载型延迟的 min 应接近 0（对照：`WALFileSync` min=66/avg=93；`DataFileRead` min=1/avg=22）。
⇒ 若把它算进「IO 中继升高」，该读数会**恒为 100ms**，规则永远命中 —— 典型假阳性机器。
判据：`min/avg >= 0.9 且 avg >= 50ms` ⇒ 判定为固定周期，**排除出判据读数**（仍产出以便看见）。

★ 已知局限（如实登记）：一台**持续均匀慢**的盘也可能满足该形态而被误排除。
   按本项目「宁可不报，也别报错因」的取舍接受该假阴性。

### ② 盘级平均 IO 只在**有实际 IO 量**的文件上统计

实测 284 个文件里 avgiotim 降序前几名**全是 `phyrds=1`** 的行（单次采样 ⇒ 无统计意义）。
按全量取 max 会得到 2364μs 这种被单次采样污染的数。
⇒ 只统计 `phyrds + phywrts >= 阈值` 的文件，并同时产出「参与统计的文件数」与「IO 次数」，
使读数**可复核**。实测有真 IO 的文件：avg **9–53μs**、峰值 13–16ms（企业级 SSD 正常）。

## ★ 缺席 ≠ 0（fail-closed）

`dbe_perf.wait_events` 只含**发生过**的事件。某个事件行不存在时，
若读成 0 就会把「没发生过/没采到」当成「等待为 0」= 没问题。
⇒ 本模块显式产出 `present` 与 `missing` 清单，消费侧据 `missing` 判断读数是否完整。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── 存储链路 IO 事件（判据面的**显式白名单**）──────────────────────────────
# ★ 为什么用白名单而不是模糊匹配：等待事件名是设备/内核命名，模糊匹配（含 io/wal 等）
#   会随版本引入的事件名而漂移，且会把非存储类事件（如 `BufHashTableSearch`）误收进来。
# 分两类对应 P4 方案里的两种传导因果：
#   A 写入等待（WAL 组提交/共享存储写）：慢盘 ⇒ 提交变慢
#   B 读放大（数据文件读/双写）：慢盘 ⇒ 读变慢、命中率下降
#   （C 类 CPU 密集与存储无关，由 P1 的 sql.cpu_time_ratio 承担，**不进本表**）
WRITE_CHAIN = (
    "ShareStorageWalWrite",     # 共享存储 WAL 写（存算分离的核心等待）
    "WALFileSync",              # WAL 文件 fsync
    "WALWrite",                 # WAL 写
    "ShareStorageCtlInfoWrite", # 共享存储控制信息写
    "CkptWaitPageWriterSync",   # 检查点等待 page writer 同步
    "CkptWaitPageWriterFlush",  # 检查点等待 page writer 刷盘
)
READ_CHAIN = (
    "DataFileRead",             # 数据文件读
    "DataFileWrite",            # 数据文件写
    "ShareStorageCtlInfoRead",  # 共享存储控制信息读
    "DoubleWriteFileWrite",     # 双写（防断裂页）
)
ALL_CHAIN = WRITE_CHAIN + READ_CHAIN

# 单位：实测 avg/max/total_wait_time 均为**微秒**（例：WALFileSync avg=93/peak=12919；
# CkptWaitPageWriterSync avg=100144 ⇒ 100ms；DataFileRead peak=17017 ⇒ 17ms）
US_PER_MS = 1000.0

# ★ 固定周期等待的判定阈值（见模块文档①）：min/avg >= 0.9 且 avg >= 50ms
#   实测 CkptWaitPageWriterSync: min=100038/avg=100144 (比值 0.999) ⇒ 命中，被排除
FIXED_PERIOD_MIN_RATIO = 0.9
FIXED_PERIOD_MIN_US = 50000.0

# ★ 盘级统计的最小 IO 次数（见模块文档②）：低于此的文件不予统计（单次采样无意义）
DISK_MIN_IO = 100


def _u(v: Any) -> Optional[float]:
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f


def io_relay_facts(wait_rows: List[dict], file_rows: List[dict],
                   recent_io_samples: int, recent_window_min: int,
                   ts: str, src: str) -> List[dict]:
    """IO 中继 → 事实

    Args:
        wait_rows: `dbe_perf.wait_events` 行（含 event / avg_wait_time / max_wait_time /
                   total_wait_time / type）
        file_rows: `dbe_perf.summary_file_iostat` 行（含 avgiotim / maxiowtm / phyrds / phywrts）
        recent_io_samples: 近窗口内 ASP 的 IO 类采样条数（**近期**证据）
        recent_window_min: 上面那个窗口的分钟数（一并产出，否则读数无口径）
        ts / src: 采集时刻与出处
    """
    out: List[dict] = []
    by_event: Dict[str, dict] = {}
    for r in (wait_rows or []):
        if isinstance(r, dict):
            ev = str(r.get("event") or "").strip()
            if ev:
                by_event[ev] = r

    def _emit_chain(chain, tag, P):
        """P: 该链的路径字面量字典

        ★ 路径必须**字面量出现**在源码里：动态拼接（`"io.relay.%s" % tag`）会让
        V14/V18/V27 的产出面检测搜不到字面量（判为「声明了但不产出」），
        而且动态路径本身不可审计。本仓纪律：判据面必须真实存在且可搜索。
        """
        seen, miss, fixed = [], [], []
        best_avg = best_peak = None
        best_avg_ev = best_peak_ev = None
        total_us = 0.0
        for ev in chain:
            r = by_event.get(ev)
            if r is None:
                miss.append(ev)
                continue
            seen.append(ev)
            a = _u(r.get("avg_wait_time"))
            pk = _u(r.get("max_wait_time"))
            t = _u(r.get("total_wait_time"))
            mn = _u(r.get("min_wait_time"))
            if t is not None:
                total_us += t
            # ★ 固定周期识别（见模块文档①）：min/avg 接近 1 且量级 ≥50ms ⇒ 定时器而非 IO 时延
            if a and mn is not None and a > 0 and (mn / a) >= FIXED_PERIOD_MIN_RATIO \
                    and a >= FIXED_PERIOD_MIN_US:
                fixed.append(ev)
                continue          # 排除出判据读数（仍登记在 fixed 清单里，可看见）
            if a is not None and (best_avg is None or a > best_avg):
                best_avg, best_avg_ev = a, ev
            if pk is not None and (best_peak is None or pk > best_peak):
                best_peak, best_peak_ev = pk, ev
        oid = "io:relay:" + tag

        def _f(path, value, literal=False):
            row = {"path": path, "value": value, "obj_id": oid,
                   "obj_type": "io_relay", "ts": ts, "source": src}
            if literal:
                row["literal"] = True
            out.append(row)

        if best_avg is not None:
            _f(P["max_avg"], round(best_avg, 1))
            _f(P["worst_avg"], best_avg_ev or "", literal=True)
        if best_peak is not None:
            _f(P["max_peak"], round(best_peak, 1))
            _f(P["worst_peak"], best_peak_ev or "", literal=True)
        _f(P["total"], round(total_us / US_PER_MS, 1))
        _f(P["present"], len(seen))
        _f(P["expected"], len(chain))
        if miss:
            _f(P["missing"], ",".join(miss), literal=True)
        if fixed:
            _f(P["fixed"], ",".join(fixed), literal=True)
        return {"avg": best_avg, "peak": best_peak, "avg_ev": best_avg_ev,
                "peak_ev": best_peak_ev, "seen": len(seen)}



    # ★ 路径**字面量**写在调用处（理由见 _emit_chain 的说明）
    w = _emit_chain(WRITE_CHAIN, "write_chain", {
        "max_avg": "io.relay.write_chain.max_avg_us",
        "worst_avg": "io.relay.write_chain.worst_avg_event",
        "max_peak": "io.relay.write_chain.max_peak_us",
        "worst_peak": "io.relay.write_chain.worst_peak_event",
        "total": "io.relay.write_chain.total_ms",
        "present": "io.relay.write_chain.present_count",
        "expected": "io.relay.write_chain.expected_count",
        "missing": "io.relay.write_chain.missing",
        "fixed": "io.relay.write_chain.fixed_period",
    })
    r = _emit_chain(READ_CHAIN, "read_chain", {
        "max_avg": "io.relay.read_chain.max_avg_us",
        "worst_avg": "io.relay.read_chain.worst_avg_event",
        "max_peak": "io.relay.read_chain.max_peak_us",
        "worst_peak": "io.relay.read_chain.worst_peak_event",
        "total": "io.relay.read_chain.total_ms",
        "present": "io.relay.read_chain.present_count",
        "expected": "io.relay.read_chain.expected_count",
        "missing": "io.relay.read_chain.missing",
        "fixed": "io.relay.read_chain.fixed_period",
    })
    # ── 总览：一条阈值判据用的读数（取两侧最大的持续值）──
    cand = [(x["avg"], x["avg_ev"], tag) for x, tag in ((w, "write"), (r, "read"))
            if x and x["avg"] is not None]
    if cand:
        top_avg, top_ev, top_kind = max(cand, key=lambda z: z[0])
        out.append({"path": "io.relay.max_avg_us", "value": round(top_avg, 1),
                    "obj_id": "io:relay:all", "obj_type": "io_relay", "ts": ts, "source": src})
        out.append({"path": "io.relay.worst_event", "value": top_ev or "",
                    "obj_id": "io:relay:all", "obj_type": "io_relay", "ts": ts, "source": src,
                    "literal": True})
        out.append({"path": "io.relay.worst_kind", "value": top_kind,
                    "obj_id": "io:relay:all", "obj_type": "io_relay", "ts": ts, "source": src,
                    "literal": True})
    peak_c = [(x["peak"], x["peak_ev"]) for x in (w, r)
              if x and x["peak"] is not None]
    if peak_c:
        out.append({"path": "io.relay.max_peak_us",
                    "value": round(max(p[0] for p in peak_c), 1),
                    "obj_id": "io:relay:all", "obj_type": "io_relay", "ts": ts, "source": src})
    out.append({"path": "io.relay.event_count", "value": len(by_event),
                "obj_id": "io:relay:all", "obj_type": "io_relay", "ts": ts, "source": src})

    # ── 盘级平均 IO 时间（主机侧看到的多路径卷平均 IO 耗时）──
    # ★ 这是原方案里「慢盘」判据的**可得替代**：阵列侧取不到盘级时延，
    #   但主机侧有 `summary_file_iostat.avgiotim`（实测 465–563μs，属企业级 SSD 正常区间）。
    io_rows = [x for x in (file_rows or []) if isinstance(x, dict)]
    # ★ 只统计有实际 IO 量的文件（见模块文档②）：单次采样（phyrds=1）会把 max 污染成无意义的数
    def _io_count(x):
        a, b = _u(x.get("phyrds")), _u(x.get("phywrts"))
        return (a or 0) + (b or 0)
    active = [x for x in io_rows if _io_count(x) >= DISK_MIN_IO]
    out.append({"path": "io.disk.active_file_count", "value": len(active),
                "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})
    out.append({"path": "io.disk.min_io_filter", "value": DISK_MIN_IO,
                "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})
    avgs = [(_u(x.get("avgiotim")), x) for x in active]
    avgs = [(a, x) for a, x in avgs if a is not None]
    if avgs:
        top, row = max(avgs, key=lambda z: z[0])
        out.append({"path": "io.disk.max_avg_io_us", "value": round(top, 1),
                    "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})
        out.append({"path": "io.disk.worst_file", "value": str(row.get("filenum") or ""),
                    "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src,
                    "literal": True})
        peaks = [_u(x.get("maxiowtm")) for x in active]
        peaks = [p for p in peaks if p is not None]
        if peaks:
            out.append({"path": "io.disk.max_peak_io_us", "value": round(max(peaks), 1),
                        "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})
        out.append({"path": "io.disk.file_count", "value": len(active),
                    "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})
        # 该文件的实际 IO 次数（使读数可复核：avg 背后有多少次 IO）
        out.append({"path": "io.disk.worst_file_io_count",
                    "value": int(_io_count(row)), "obj_id": "io:disk:all",
                    "obj_type": "io_disk", "ts": ts, "source": src})
    else:
        # 取不到时**不产出读数**（缺判据 ⇒ 规则不命中），但要能看见「为什么没有」
        out.append({"path": "io.disk.file_count", "value": 0,
                    "obj_id": "io:disk:all", "obj_type": "io_disk", "ts": ts, "source": src})

    # ── 近期证据（ASP 采样计数；补 avg 的「不敏感」）──
    out.append({"path": "io.recent.io_wait_samples", "value": int(recent_io_samples or 0),
                "obj_id": "io:recent", "obj_type": "io_recent", "ts": ts, "source": src})
    out.append({"path": "io.recent.window_min", "value": int(recent_window_min or 0),
                "obj_id": "io:recent", "obj_type": "io_recent", "ts": ts, "source": src})
    return out
