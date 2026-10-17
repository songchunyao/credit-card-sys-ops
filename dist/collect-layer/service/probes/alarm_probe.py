# -*- coding: utf-8 -*-
"""P3：阵列告警 → 事实（服务用户主诉的「控制器异常缺根因」）

## 为什么要有这个采集面

用户实测主诉：存储控制器异常能报「异常」，但**说不出为什么**。
现有控制器决策树的兜底分支原文写着：

> 兜底分支无判据。RC1 的证据是**阵列侧硬件告警明细**（电源/风扇/缓存电池），
> 本项目只采健康状态码与资源用量，不含硬件日志 ⇒ 无法机判（登记 CG-ST-07）。

本模块把告警明细采上来，使那条残差分支**可机判**。

## ★ 四条纪律（全部由 2026-09-24 生产实测逼出，不是设计出来的）

### 1. 分页在本设备上**不存在**
实测 `alarm/currentalarm?pageSize=2&pageNo=1` 与 `?pageSize=2&pageNo=2` **返回同一批全量**（29 条）。
⇒ 「用 pageNo 翻页」照写会**反复取同一批**、再按页数把条数放大（看起来采了很多，其实全是重复）。
本模块**单次取全量**。

### 2. 撞响应上限必须标「可能被截断」
`alarm/historyalarm` 单次实测 **10000** 条（正好撞上限）⇒ `scope=paged`。
`full`/`paged`/`none` 三态缺一不可 —— 混为一谈会让「没采到」被读成「阵列健康」。

### 3. 历史告警可能**整体失效**，且必须可检测
实测 historyalarm 的最新记录停在 **2026-04-26**，而采集日是 **2026-09-24** —— 停了 5 个月。
同期的当前告警里有一条「**事件日志存储空间即将用尽**」⇒ 历史告警面（事件日志）已写不进去。
⇒ 本模块产出 `storage.dorado.alarm.latest`（该类告警的**最新时刻**）。
没有这个读数，「历史面是陈的」与「历史面是新的且没有新告警」在图上长得一样。

### 4. 告警身份是 (eventID, 部件, 起始时刻)；且**定位字段要抽对**
- 实测同一 eventID 在不同部件重复出现（`A Port Is Disconnected` 既有 MGMT 口也有 IOM3）
  ⇒ 只按 eventID 归并会把不同部件混成一条。
- 实测 license 类告警的 `eventParam` 是 `129,2026-09-30`（裸码+日期）、`location` 是
  `FeatureName=129,Run-Date=2026-09-30` ⇒ 直接拿 eventParam 当定位会产出「129,2026-09-30」这种垃圾。
  真正的部件名在 `description` 里：`The license of (HyperLink(SOCC)) is about to expire.`
  ⇒ 抽取顺序：**硬件模式（机框/控制器/模块/端口）→ 描述里的括号功能名 → location 的带键项**，
  并显式排除纯数字/日期形状的兜底值。

## 规模口径（历史面为何按分组存）

实测 history 10000 条 → 仅 **670 种 (name, 部件)**（压缩比约 15:1）。
逐条存会产出 5 万条事实且几乎全无分析增量 ⇒ 历史面按 **(name, 部件)** 分组存
（条数 + 最新时刻），当前面逐条存（29 条，是真正的行动依据）。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# 实测见过的级别（**不是白名单**，只用于另打一条「未知级别」而不是丢弃）
_ALARM_LEVEL_KNOWN = ("1", "2", "3", "4", "5", "6")

# 单次响应上限实测 10000（historyalarm 正好 10000）⇒ 撞上限即判「可能被截断」
ALARM_RESPONSE_LIMIT = 10000

# 纯数字 / 日期形状（**不许**当定位用 —— 实测 license 告警会产出 `129,2026-09-30`）
_JUNK_RE = re.compile(r"^[\d\s,.\-:/]+$")
_DATEISH_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_epoch(v: Any) -> Optional[str]:
    """设备给 epoch 秒（字符串）⇒ 转 UTC 时刻（GaussDB 侧统一用 UTC 时刻读图）"""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode(s: Any) -> str:
    """设备把括号转义成 `&#40;`/`&#41;`，还原后再用"""
    t = str(s or "")
    for a, b in (("&#40;", "("), ("&#41;", ")"), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")):
        t = t.replace(a, b)
    return t.strip()


_ENCL_RE = re.compile(r"enclosure\s+(CTE\d+)", re.IGNORECASE)
_CTRL_RE = re.compile(r"controller\s+([A-Za-z])\b", re.IGNORECASE)
# interface 可选：实测描述为 `interface module A.IOM3`，但放宽后可覆盖 `module A.IOM3`
# 这类省略写法（只影响定位串，不参与身份缝合，放宽不会误连）
_MODULE_RE = re.compile(r"(?:interface\s+)?module\s+([A-Za-z0-9.]+)", re.IGNORECASE)
_PORT_RE = re.compile(r"port\s+number\s+([A-Za-z0-9]+)", re.IGNORECASE)
_PAREN_RE = re.compile(r"\(([^()]+(?:\([^()]*\))?[^()]*)\)")


def _locate(desc: str, location: Any, event_param: Any) -> str:
    """抽出**可定位到部件**的标识（没有它就只能说「阵列有告警」）

    抽取顺序（每一级都是实测必需）：
      ① 硬件模式：`Controller Enclosure CTE0` / `controller A` / `interface module A.IOM3` / `port number P1`
      ② 描述里的括号功能名：`The license of (HyperLink(SOCC)) ...` ⇒ `feature=HyperLink(SOCC)`
      ③ location 的带键项（`FeatureName=...`），**排除纯数字/日期形状**
      ④ 都没有 ⇒ `unknown`（不编造）
    """
    parts: List[str] = []
    m = _ENCL_RE.search(desc)
    if m:
        parts.append(m.group(1).upper())
    m = _CTRL_RE.search(desc)
    if m:
        parts.append("controller " + m.group(1).upper())
    m = _MODULE_RE.search(desc)
    if m:
        parts.append("module " + m.group(1))
    m = _PORT_RE.search(desc)
    if m:
        parts.append("port " + m.group(1))
    if parts:
        return " ".join(parts)

    for g in _PAREN_RE.finditer(desc):
        cand = g.group(1).strip()
        if cand and not _JUNK_RE.match(cand):
            return "feature=" + cand[:60]

    for kv in str(location or "").split(","):
        k, _, v = kv.partition("=")
        v = v.strip()
        if k.strip() in ("FrameID", "BoardID", "FeatureName") and v:
            if _JUNK_RE.match(v) or _DATEISH_RE.search(v):
                continue        # ★ 排除裸码与日期（实测垃圾来源）
            return "%s=%s" % (k.strip(), v)
    return "unknown"


def _oid(state: str, eid: str, loc: str, start: Any) -> str:
    """告警身份 = (state, eventID, 部件, 起始时刻)

    ★ 为什么带部件与时刻：同一 eventID 会在不同部件上重复出现，只按 eventID 归并会混成一条。
    """
    return "alarm:%s:%s:%s:%s" % (state, eid or "?", loc.replace(" ", "_") or "?",
                                  str(start or "").strip() or "-")


def alarm_facts(records: List[dict], state: str, scope: str, ts: str,
                source: str) -> List[dict]:
    """告警记录 → 事实

    Args:
        records: 设备返回的记录
        state: `current` / `history`
        scope: `full` / `paged` / `none`（**截断必须如实标注**）
        ts: 采集时刻
        source: 出处（写进事实 source）

    返回：当前面**逐条**（行动依据）；历史面**按 (name,部件) 分组**（10000→670，压缩比 15:1）。
    两类都产 `scope` / `count`；历史面另产 `latest`（最新时刻，用于检测「历史面本身失效」）。
    """
    out: List[Dict[str, Any]] = []
    recs = [a for a in (records or []) if isinstance(a, dict)]

    latest = None
    for a in recs:
        s = _parse_epoch(a.get("startTime"))
        if s and (latest is None or s > latest):
            latest = s

    if state == "current":
        for a in recs:
            eid = str(a.get("eventID") or "").strip()
            loc = _locate(_decode(a.get("description")), a.get("location"), a.get("eventParam"))
            oid = _oid(state, eid, loc, a.get("startTime"))
            name = _decode(a.get("name")) or _decode(a.get("description"))[:60]
            for path, val in (
                ("storage.dorado.alarm.name", name),
                ("storage.dorado.alarm.severity", str(a.get("level") or "").strip()),
                ("storage.dorado.alarm.object", loc),
                ("storage.dorado.alarm.state", "current"),
            ):
                out.append({"path": path, "value": val, "obj_id": oid, "obj_type": "alarm",
                            "ts": ts, "source": source})
            since = _parse_epoch(a.get("startTime"))
            if since:
                out.append({"path": "storage.dorado.alarm.since", "value": since,
                            "obj_id": oid, "obj_type": "alarm", "ts": ts, "source": source})
            lvl = str(a.get("level") or "").strip()
            if lvl and lvl not in _ALARM_LEVEL_KNOWN:
                # 未知级别**不丢**，另打一条（设备固件给了新级别时要看得见）
                out.append({"path": "storage.dorado.alarm.severity_unknown", "value": lvl,
                            "obj_id": oid, "obj_type": "alarm", "ts": ts, "source": source})
    else:
        # 历史面：按 (name, 部件) 分组 —— 逐条存会产出 5 万条事实且几乎无分析增量
        grp: Dict[tuple, dict] = {}
        for a in recs:
            name = _decode(a.get("name")) or _decode(a.get("description"))[:60]
            loc = _locate(_decode(a.get("description")), a.get("location"), a.get("eventParam"))
            g = grp.setdefault((name, loc), {"n": 0, "latest": None, "levels": Counter()})
            g["n"] += 1
            g["levels"][str(a.get("level") or "").strip()] += 1
            s = _parse_epoch(a.get("startTime"))
            if s and (g["latest"] is None or s > g["latest"]):
                g["latest"] = s
        for (name, loc), g in sorted(grp.items(), key=lambda kv: -kv[1]["n"]):
            oid = "alarm:history:%s:%s" % (name[:40].replace(" ", "_"), loc.replace(" ", "_"))
            out.append({"path": "storage.dorado.alarm.group.name", "value": name,
                        "obj_id": oid, "obj_type": "alarm_group", "ts": ts, "source": source})
            out.append({"path": "storage.dorado.alarm.group.object", "value": loc,
                        "obj_id": oid, "obj_type": "alarm_group", "ts": ts, "source": source})
            out.append({"path": "storage.dorado.alarm.group.count", "value": g["n"],
                        "obj_id": oid, "obj_type": "alarm_group", "ts": ts, "source": source})
            top = g["levels"].most_common(1)
            if top:
                out.append({"path": "storage.dorado.alarm.group.severity", "value": top[0][0],
                            "obj_id": oid, "obj_type": "alarm_group", "ts": ts, "source": source})
            if g["latest"]:
                out.append({"path": "storage.dorado.alarm.group.latest", "value": g["latest"],
                            "obj_id": oid, "obj_type": "alarm_group", "ts": ts, "source": source})

    # ── 覆盖面（**必产**，含 none）：使「没采到/被截断」与「确实没有」可区分 ──
    oid = "dorado:alarm:" + state
    out.append({"path": "storage.dorado.alarm.scope", "value": scope, "obj_id": oid,
                "obj_type": "alarm_scope", "ts": ts, "source": source})
    out.append({"path": "storage.dorado.alarm.count", "value": len(recs), "obj_id": oid,
                "obj_type": "alarm_scope", "ts": ts, "source": source})
    # ★ latest：没有它，「历史面是陈的」与「历史面是新的但没新告警」在图上一样
    out.append({"path": "storage.dorado.alarm.latest", "value": latest or "", "obj_id": oid,
                "obj_type": "alarm_scope", "ts": ts, "source": source})
    return out


def fetch_alarms(client, request_fn, ts: str, source: str,
                 endpoints=("alarm/currentalarm", "alarm/historyalarm")) -> List[dict]:
    """从 DeviceManager 取告警

    ★ 单次取全量（实测设备**忽略** pageSize/pageNo，翻页会重复取同一批）。
    撞上限 ⇒ paged；失败 ⇒ none。两类都照常产出 scope 事实。
    """
    out: List[Dict[str, Any]] = []
    for ep in endpoints:
        state = "current" if "current" in ep else "history"
        recs: List[dict] = []
        scope = "none"
        try:
            r = request_fn("GET", ep)
            if isinstance(r, dict):
                r = r.get("data") or r.get("records") or []
            if isinstance(r, list):
                recs = [x for x in r if isinstance(x, dict)]
                scope = "paged" if len(recs) >= ALARM_RESPONSE_LIMIT else "full"
        except Exception:
            recs, scope = [], "none"
        out.extend(alarm_facts(recs, state, scope, ts, source))
    return out


def controller_alert_facts(current_facts: List[dict], controllers: List[dict],
                           ts: str, source: str) -> List[dict]:
    """按**控制器**统计当前告警条数 + 给出「健康异常控制器的告警数」（决策树判据）

    ★ 为什么必须逐控制器给读数（而不是一个总数）：
      总数 >0 只说明「阵列上有告警」，说不出是**哪台**控制器 —— 而健康异常的那台
      可能恰好没有告警；总数为 0 时也分不清「都没告警」与「采集失败」。
      逐台给读数后，某台缺读数 = 它确实没有告警（控制器清单来自同一次采集）。

    ★ 判据面需要的那一个数：`degraded_ctrl_alert_count`
      —— **仅对健康异常的控制器**统计的告警数。
      为什么不给「总数」让规则自己关联：本仓规则/决策树的判据面只能读**单路径**事实
      （无 obj_id 关联能力），所以「健康异常那台的告警数」必须在采集侧算好。
      这正是把 CG-ST-07 从「无法机判」变为「可机判」的那把钥匙。

    ★ 匹配口径**三种**（都是设备自己的命名，不是跨系统约定；也是 `_locate` 实际会产出的形态）：
      ① `controller <NAME>` —— 描述里的写法，实测 `CTE0 controller A port MGMT`
      ② `<NAME>.IOM<n>`      —— 机框内模块槽位前缀，实测 `CTE0 module A.IOM1 port P0`
      ③ `boardid=<NAME>`     —— 无描述时 `_locate` 回落到 location 带键项的产出形态
    ★ 不保留「控制器 LOCATION（如 `CTE0.A`）」这一形态：`_locate` **不会**产出该字符串，
      留着就是一条**永远不成立**的判据（本仓纪律：判据面必须真实存在）。
    ★ 不用 description 全文模糊匹配（随固件改词漂移）；也不单用控制器字母（`A` 会命中一切）。
    """
    import re as _re
    out: List[Dict[str, Any]] = []
    objs = [str(f.get("value") or "") for f in (current_facts or [])
            if f.get("path") == "storage.dorado.alarm.object"]

    def _match_count(nm: str) -> int:
        # ★ 只用「_locate 实际会产出的」三种写法；不用控制器 LOCATION（永不成立）
        pats = ["controller %s" % nm.lower(), "boardid=%s" % nm.lower()]
        mod_re = _re.compile(r"(^|\s)%s\.iom\d" % _re.escape(nm.lower()))
        n = 0
        for o in objs:
            ol = o.lower()
            if any(pt in ol for pt in pats) or mod_re.search(ol):
                n += 1
        return n

    degraded_alert = 0
    degraded_names: List[str] = []
    health_unknown: List[str] = []
    ctrl_total = 0
    for c in (controllers or []):
        if not isinstance(c, dict):
            continue
        nm = str(c.get("NAME") or "").strip()
        if not nm:
            continue
        loc = str(c.get("LOCATION") or "").strip()
        health = str(c.get("HEALTHSTATUS") or "").strip()
        n = _match_count(nm)
        ctrl_total += n
        oid = "dorado:ctrl:%s" % nm
        out.append({"path": "storage.dorado.alarm.controller.alert_count", "value": n,
                    "obj_id": oid, "obj_type": "controller", "ts": ts, "source": source})
        out.append({"path": "storage.dorado.alarm.controller.health_status",
                    "value": health, "obj_id": oid, "obj_type": "controller",
                    "ts": ts, "source": source})
        # ★ 健康异常的控制器：沿用既有规则 r_st_dorado_ctrl_health 的口径（= 2 判异常），
        #   但这里取「非 1 即不是健康」以覆盖 0/空 等其它非健康取值（不因不等于 2 而漏）
        if health and health != "1":
            degraded_alert += n
            degraded_names.append(nm)
        elif not health:
            # ★ 健康码**没采到** ≠ 不健康（本仓「缺失不等于坏」的纪律）。
            #   单列一份读数，使「确实没有异常控制器」与「健康码没读上来」可区分 ——
            #   否则一次字段缺失会让 degraded_count=0 被读成「全部健康」。
            health_unknown.append(nm)
    out.append({"path": "storage.dorado.alarm.controller.total_alert_count",
                "value": ctrl_total, "obj_id": "dorado:ctrl:all",
                "obj_type": "controller", "ts": ts, "source": source})
    out.append({"path": "storage.dorado.alarm.controller.count", "value": len(
        [c for c in (controllers or []) if isinstance(c, dict)]),
        "obj_id": "dorado:ctrl:all", "obj_type": "controller", "ts": ts, "source": source})
    # ★ 这两个读数是**一对**（同一主体：哪几台异常）—— 主体口径必须一致，
    #   否则消费侧按不同 obj_id 取会得到 None（实测自己就取错过一次）。
    deg_subj = "dorado:ctrl:" + ("|".join(degraded_names) if degraded_names else "none")
    out.append({"path": "storage.dorado.alarm.controller.degraded_ctrl_alert_count",
                "value": degraded_alert, "obj_id": deg_subj,
                "obj_type": "controller", "ts": ts, "source": source})
    out.append({"path": "storage.dorado.alarm.controller.degraded_count",
                "value": len(degraded_names), "obj_id": deg_subj,
                "obj_type": "controller", "ts": ts, "source": source})
    # ★ 健康码未采到的台数：与 degraded_count 分开，使「没有异常」与「没读到」可区分
    out.append({"path": "storage.dorado.alarm.controller.health_unknown_count",
                "value": len(health_unknown), "obj_id": deg_subj,
                "obj_type": "controller", "ts": ts, "source": source})
    return out
