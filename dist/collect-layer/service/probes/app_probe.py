# -*- coding: utf-8 -*-
"""app 层探针（M1.6 新增，2026-10-12 重写为**裸容器 + 宿主 /proc + GaussDB 归因**通道）

职责：把 **被测信用卡应用**（L1）的身份与运行态采成一层的 facts + 拓扑段。
本项目的 L1 = `java -jar credit-card-simulator.jar` **这个 java 进程** + 它的 **JDBC 连接池**
（用户 2026-10-12 明确纠正：不是泛化的「JVM」概念，也不含 HTTP 端点）。

================================================================================
★ 为什么重写（2026-10-12 真机实测）
================================================================================
上一版假设应用**自己暴露 `GET /health`**（`env/cc-app/CreditCardApp.java`，
k8s NodePort 30085）。但当前 L1 是 `cardsim` 的仿真器：
  · 容器**没有任何端口映射**（`docker inspect .NetworkSettings.Ports` = `map[]`），
    根本没有 HTTP 端点可探 ⇒ 实测 `section_errors.app_topology =
    "MissingSchema: Invalid URL '/health': No scheme supplied"`，app 层 `fact_count=0`。

================================================================================
★ 新通道（三条，全部只读；2026-10-12 实测）
================================================================================
1. **Docker Engine API**（unix socket）
   容器 `Config.Env`（CCSIM_* 仿真参数与 JDBC 配置）、`State.Pid`（**宿主** pid）、
   `Id`（与 container 层同源，`RUNS_IN` 靠它连边）、`Config.Image`（版本号来源）。
   ★ 关键：`/proc/<pid>/environ` 是 `0400 root` 不可读，**但容器 Env 经 Docker API 可读**。

2. **宿主 /proc**（`/host/proc/<宿主pid>/…`，ro 挂载）
   `status`（Threads/VmRSS）、`stat`（starttime）、`cmdline`（入口与 artifact）。
   这三者权限是 0444 世界可读 ⇒ 非 root 的采集进程也能读（已实测）。
   ★ `fd`/`environ` 是 0400 需 root —— 本探针不依赖它们。

3. **GaussDB 侧会话归因**（`pg_stat_activity`，按 `application_name` 分组）
   连接池的 `application_name = "ccsim-" + 容器 hostname`（RUNTIME-INVENTORY §4.2），
   这是**把库侧会话唯一归属到具体容器**的桥梁。
   → `app.datasource.active_connections` / `idle_connections`。

4. **★ 应用实时遥测日志**（P0-B，2026-10-12 新增；应用唯一自报的延迟/TPS 面）
   应用每 `reportInterval`（默认 5s）写一行运行指标到
   `<CC_APP_LOGDIR>/ccsim-<hostname>-<YYYYMMDD>-<HHMMSS>.log`（宿主 bind mount，只读挂载）：
     `[run] phase=MEASURED t=270s tps=1424.85 ok=… fail=… p50=… p95=… p99=… max=…`
     `[WARN] [db] 连接池获取偏慢 #4 (GaussDB) app=ccsim-… 耗时=4748ms`
     `[INFO] [db] HikariCP + GaussDB 连接池 初始化: poolSize=8` / `HikariCP 连接池已关闭`
   → `app.endpoint.{qps,p99_ms,error_rate}`、`app.datasource.acquire_wait_ms`。
   ★ 这是**延迟/TPS 类故障的唯一判据来源**（应用不监听任何端口）。
   ★ 时效性：应用在 `hold` 下平时不跑仿真，最后的 `[run]` 行可能是几小时前的。
     超过 `CC_APP_TELEMETRY_MAX_AGE_S` ⇒ 一律报 **notCollected**（不把陈旧值当现值）。

================================================================================
★★ 仍然没有通道的指标（**不做假值**，如实登记）
================================================================================
  · `app.heap_used_ratio` / `app.gc_pause_ms`
      —— JVM 内部量。**结论已从「架构性无通道」修正为「缺 /tmp」**（2026-10-12 实测）：
         被测镜像**没有 `/tmp`**，导致 JVM 的 attach socket（`/tmp/.java_pid1`）与
         perfdata（`/tmp/hsperfdata_*`）都建不出来 ⇒ `jcmd`/`jstat` 失败。
         实测 `docker run --tmpfs /tmp` 后 **`jcmd -l` / `jstat -gc` / `GC.heap_info` 全部可用**
         （镜像里 jcmd/jstat/jstack/jmap/jinfo/jps 齐全，只是没有 sh/ls）。
         ⇒ 补这条路只需给**被测容器**加一行 `tmpfs: /tmp`（属改被测系统，待裁定）。
  · `app.datasource.pending_threads` / `connect_latency_ms` / `connect_fail_total` / `timeout_total`
      —— HikariCP **池内部计数器**，只存在于 JVM 堆内。`pending_threads` 可在上述
         tmpfs 修好后用 `jcmd Thread.print` 数「卡在 HikariPool.getConnection 的线程」得到。
         ★ `acquire_wait_ms` **已由遥测日志补上**（见通道 4）。

⇒ 本探针**只产出真有通道的事实**，不为凑覆盖门禁而伪造 0 / 假值。

================================================================================
输出契约
================================================================================
  · facts（obj_type 与 v3-ccf-map.yaml 的 objtype_to_type 一致）：
      app_instance    → app.uptime_s / app.thread_count
      app_endpoint    → app.endpoint.qps / p99_ms / error_rate（遥测新鲜时）
      jdbc_datasource → app.datasource.{active,idle}_connections / acquire_wait_ms
  · raw.app_topology = {apps, app_instances, app_endpoints, datasources}
      apps          : appId / appCode / appName / version / techStack / sloP99Ms
      app_instances : instanceId / appId / containerId / jvmVersion / pid / startTime
      app_endpoints : **恒有一个**（`ep-<appId>-sim`，代表该实例的仿真工作负载入口；
                      `path=/sim/run`、`protocol=internal`）⇒ `EXPOSES` 边恒成立。
                      指标读数是否 collected 取决于遥测时效。
      datasources   : datasourceId / appInstanceId / dbHost（RESOLVES_TO 的 instance_by_ip 键）
                      / dsCode / 驱动与池参数（**全部取自容器 Env，不猜**）
  · raw.telemetry：逐实例的遥测解析结果（含 age_s / fresh / pool_create_count /
      pool_close_count），供审计「为什么这个端点是 notCollected」。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import http.client
import json
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from . import BaseProbe, ProbeResult, register_probe

# ── 配置 ──────────────────────────────────────────────────────────────────
#
# ★ 数值键**必须容忍空串**（与 container_probe 同一纪律）：
#   `.env` 写 `KEY=` ⇒ `os.environ.get(K, default)` 返回 ""而不是 default，
#   直接 float("") 会在**导入期**抛异常并让整层探针注册失败。
def _env_num(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


DOCKER_SOCK = os.environ.get("CC_DOCKER_SOCK", "/var/run/docker.sock")
APP_CONTAINER = os.environ.get("CC_APP_CONTAINER", "credit-card-sim-cardsim")
HOST_PROC = os.environ.get("CC_HOST_PROC", "/host/proc").rstrip("/")
DOCKER_TIMEOUT = _env_num("CC_DOCKER_TIMEOUT", 10.0)

APP_CODE = os.environ.get("CC_APP_CODE", "CC-SIM")
APP_NAME = os.environ.get("CC_APP_NAME", "credit-card-simulator")
APP_TECHSTACK = os.environ.get("CC_APP_TECHSTACK", "Java+HikariCP+gsjdbc4")
DB_TIMEOUT = _env_num("CC_APP_DB_TIMEOUT", 8.0)

# ── ★ 2026-10-12 新增：应用实时遥测日志（P0-B）─────────────────────────────
# 被测应用**自己**每 reportInterval（默认 5s）写一行运行指标到
#   <CC_APP_LOGDIR>/ccsim-<容器 hostname>-<YYYYMMDD>-<HHMMSS>.log
# 形如：
#   ...[INFO ] [run] phase=MEASURED t=270s tps=1424.85 ok=377760 fail=0 \
#            approved=313441 declined=6776 p50=5.00 p95=11.66 p99=20.95 max=232.92
#   ...[WARN ] [db] 连接池获取偏慢 #4 (GaussDB) app=ccsim-8abfba00a0d6 耗时=4748ms
#   ...[INFO ] [db] HikariCP + GaussDB 连接池 初始化: poolSize=8 url=jdbc:...
#   ...[INFO ] [db] HikariCP 连接池已关闭
#
# ★ 这是**应用侧延迟/TPS 的唯一可得来源**（应用不监听任何端口，无 HTTP 端点可探）。
# ★ 目录在宿主上，容器内以**只读**方式挂到 CC_APP_LOGDIR（compose 的 volumes）。
# ★ 时效性（关键）：应用在 `hold` 模式下**平时不跑仿真**，日志里最后一条 `[run]` 行
#   可能是几小时前那一轮留下的。把陈旧读数当成「当前延迟」上报是**失真** ⇒
#   超过 CC_APP_TELEMETRY_MAX_AGE_S 的遥测一律报 **notCollected + reason**，不报值。
APP_LOGDIR = os.environ.get("CC_APP_LOGDIR", "/host/ccsim-logs").rstrip("/")
TELEMETRY_MAX_AGE_S = _env_num("CC_APP_TELEMETRY_MAX_AGE_S", 900.0)

#: 遥测行与之匹配的正则（`phase=` 之后的定长字段序列）
_RE_RUNLINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z).*?phase=(\w+)\s+t=(\d+)s\s+tps=([\d.]+)\s+"
    r"ok=(\d+)\s+fail=(\d+)\s+approved=(\d+)\s+declined=(\d+)\s+"
    r"p50=([\d.]+)\s+p95=([\d.]+)\s+p99=([\d.]+)\s+max=([\d.]+)")
#: 「连接池获取偏慢 #N ... 耗时=Xms」
_RE_SLOW_ACQUIRE = re.compile(r"连接池获取偏慢\s+#\d+.*?耗时=(\d+)ms")
#: 池生命周期（建池 / 关池）—— ★ 池实例泄漏的判据来源
_RE_POOL_CREATE = re.compile(r"HikariCP.*连接池\s*初始化")
_RE_POOL_CLOSE = re.compile(r"HikariCP\s*连接池已关闭")


def _slo_p99() -> Optional[float]:
    """SLO 目标（ms）。未配置 ⇒ 不写该属性（**不猜**默认值）。

    ★ 空串陷阱：`os.environ.get(K, "")` 拿到 "" 时 float("") 会炸，
      故显式判空后返回 None（与 2026-10-12 的 GAUSSDB_SSH_MUX_PERSIST 事故同类）。
    """
    raw = os.environ.get("CC_APP_SLO_P99_MS", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


APP_SLO_P99_MS = _slo_p99()

_CLK_TCK = 100.0
_MB = 1024 * 1024


# ══════════════════════════════════════════════════════════════════════════
# Docker API（与 container 探针同一实现；两层各自独立、不共享可变状态）
# ══════════════════════════════════════════════════════════════════════════

class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, sock_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):                                        # noqa: D102
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _docker_get(path: str) -> Any:
    c = _UnixHTTP(DOCKER_SOCK, DOCKER_TIMEOUT)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        if r.status >= 400:
            raise RuntimeError("docker API %s -> HTTP %d %s"
                               % (path, r.status, body[:200].decode("utf-8", "replace")))
        return json.loads(body.decode("utf-8")) if body else None
    finally:
        try:
            c.close()
        except Exception:                                     # noqa: BLE001
            pass


def _list_app_containers() -> List[dict]:
    from urllib.parse import quote
    flt = quote(json.dumps({"name": [APP_CONTAINER]}), safe="")
    return list(_docker_get("/containers/json?all=1&filters=" + flt) or [])


# ══════════════════════════════════════════════════════════════════════════
# 宿主 /proc（只读；0444 世界可读的那几个文件）
# ══════════════════════════════════════════════════════════════════════════

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _host_btime() -> Optional[float]:
    """宿主开机时刻（epoch 秒），来自 /host/proc/stat 的 `btime` 行。"""
    txt = _read_text("%s/stat" % HOST_PROC)
    for line in (txt or "").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _proc_status(hpid: int) -> Dict[str, str]:
    txt = _read_text("%s/%d/status" % (HOST_PROC, hpid))
    out: Dict[str, str] = {}
    for line in (txt or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _proc_start_ticks(hpid: int) -> Optional[float]:
    """`/proc/<pid>/stat` 第 22 字段（starttime，单位 clock tick）。"""
    txt = _read_text("%s/%d/stat" % (HOST_PROC, hpid))
    if not txt:
        return None
    # comm 字段可能含空格与括号 ⇒ 从**最后一个 ')' 之后**开始切，才是稳定做法
    i = txt.rfind(")")
    if i < 0:
        return None
    parts = txt[i + 1:].split()
    # ')' 之后第 1 个字段是 state（原第 3 字段）⇒ starttime（原第 22）在 parts[19]
    if len(parts) <= 19:
        return None
    try:
        return float(parts[19])
    except ValueError:
        return None


def _proc_cmdline(hpid: int) -> str:
    txt = _read_text("%s/%d/cmdline" % (HOST_PROC, hpid)) or ""
    return txt.replace("\x00", " ").strip()


def _artifact_and_jvm(cmdline: str) -> Tuple[str, str]:
    """从命令行里提取 (artifact, jvmVersion)。取不到就给空串（不猜）。"""
    artifact = ""
    m = re.search(r"-jar\s+(\S+)", cmdline)
    if m:
        artifact = m.group(1)
    jvm = ""
    m = re.search(r"/jvm/([^/\s]+)/bin/java", cmdline)
    if m:
        jvm = m.group(1)
    return artifact, jvm


# ══════════════════════════════════════════════════════════════════════════
# GaussDB 侧会话归因（连接池）
# ══════════════════════════════════════════════════════════════════════════

def _dsn_to_kwargs(dsn: str) -> dict:
    """解析 libpq 关键字串 DSN（与 gaussdb_probe._dsn_to_kwargs 同形）。"""
    kv = dict(re.findall(r"(\w+)=(\S+)", dsn or ""))
    return {
        "host": kv.get("host"),
        "port": int(kv.get("port", 8000)),
        "user": kv.get("user"),
        "password": kv.get("password"),
        "database": kv.get("dbname", "postgres"),
        "timeout": int(kv.get("timeout", str(int(DB_TIMEOUT)))),
    }


def _pool_counts_by_state(app_name: str) -> Dict[str, int]:
    """按 `application_name` 归因的连接池会话计数 {state: n}。

    ★ 参数化查询（不拼串）；失败抛异常，由调用方转成段错误 + 告警。
    """
    import async_gaussdb

    kwargs = _dsn_to_kwargs(os.environ.get("GAUSSDB_DSN", ""))

    async def _run() -> Dict[str, int]:
        conn = await async_gaussdb.connect(**kwargs)
        try:
            rows = await conn.fetch(
                "SELECT state, count(*) AS n FROM pg_stat_activity "
                "WHERE application_name = $1 GROUP BY state", app_name)
            return {str(r["state"]): int(r["n"]) for r in rows}
        finally:
            await conn.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


# ══════════════════════════════════════════════════════════════════════════
# 由容器 Env 推导 JDBC 数据源配置（**只读已有值，不猜**）
# ══════════════════════════════════════════════════════════════════════════

#: CCSIM_DB_TYPE → 驱动名。两侧同义，取不到就不写 driverName。
_DRIVER_BY_DBTYPE = {
    "gaussdb": "gsjdbc4",
    "postgresql": "PostgreSQL JDBC Driver",
    "opengauss": "opengauss-jdbc",
}


def _mask_jdbc(url: str) -> str:
    """脱敏：去掉 URL 里的 userinfo（`user:pass@`）与 `password=` 参数。"""
    s = str(url or "")
    s = re.sub(r"//[^/@\s]+@", "//", s)
    s = re.sub(r"([?;&]password=)[^;&]*", r"\1***", s, flags=re.I)
    return s


def _db_host(url: str) -> str:
    """从 `jdbc:<driver>://host:port/db` 里取 host（`RESOLVES_TO` 的 instance_by_ip 键）。"""
    m = re.match(r"jdbc:[A-Za-z0-9._+-]+://([^/:;,?]+)", str(url or ""))
    return m.group(1) if m else ""


def _ds_from_env(env: Dict[str, str], hostname: str, image: str) -> dict:
    """把容器 Env 里的 CCSIM_* 落成 `JdbcDataSource` 的原始记录。

    ★ 逐条有据：每个字段要么取自显式环境变量，要么**留空缺席**（不写默认值）。
      唯一的一处「推导」是 maxPoolSize = CCSIM_CONNECTION_POOL_SIZE ?? CCSIM_THREADS，
      依据是 README「池大小默认 = 线程数」与真机 `poolSize=8 / CCSIM_THREADS=8` 一致（实测）。
    """
    url = env.get("CCSIM_DB_URL") or env.get("CCSIM_GAUSSDB_URL") or ""
    dbtype = (env.get("CCSIM_DB_TYPE") or "").strip().lower()
    pool_size = env.get("CCSIM_CONNECTION_POOL_SIZE") or env.get("CCSIM_THREADS") or ""
    try:
        max_pool = int(pool_size) if str(pool_size).strip() else None
    except ValueError:
        max_pool = None
    cto = env.get("CCSIM_CONNECTION_TIMEOUT_MS", "").strip()
    try:
        cto_ms = int(cto) if cto else None
    except ValueError:
        cto_ms = None

    return {
        "datasourceId": "jdbc-%s" % hostname,
        "dsCode": "jdbc-%s" % hostname,
        "driverName": _DRIVER_BY_DBTYPE.get(dbtype, ""),
        "driverVersion": env.get("CCSIM_DRIVER_VERSION", ""),
        "jdbcUrlMasked": _mask_jdbc(url),
        "authMode": "password" if env.get("CCSIM_DB_USER") or env.get("CCSIM_GAUSSDB_USER") else "",
        "sslEnabled": bool(re.search(r"sslmode=|ssl=true", url, flags=re.I)),
        "poolImpl": "HikariCP",
        "maxPoolSize": max_pool,
        "minIdle": None,              # 固定池 min=max 属**实现细节**，Env 无此键 ⇒ 不猜
        "connectionTimeoutMs": cto_ms,
        "idleTimeoutMs": None,
        "validationQuery": "",
        "dbHost": _db_host(url),
        # ↓ 非 CCF 字段：供建边与审计
        "applicationName": "ccsim-%s" % hostname,
        "_image": image,
    }


# ══════════════════════════════════════════════════════════════════════════
# 应用实时遥测日志（P0-B）
# ══════════════════════════════════════════════════════════════════════════

def _newest_log(hostname: str) -> Optional[str]:
    """找该实例最新的一份遥测日志（按 mtime；无则 None）。

    文件名约定：`ccsim-<容器 hostname>-<YYYYMMDD>-<HHMMSS>.log`。
    实例身份用 hostname（与连接池的 `ccsim-<hostname>` 同源），可 scale 后各自取自己的日志。
    """
    try:
        names = os.listdir(APP_LOGDIR)
    except OSError:
        return None
    pat = "ccsim-%s-" % hostname
    cands = [n for n in names if n.startswith(pat) and n.endswith(".log")]
    if not cands:
        # 退化：只要该目录下有任何 ccsim-*.log 就用最新的（hostname 可能已变）
        cands = [n for n in names if n.startswith("ccsim-") and n.endswith(".log")]
    if not cands:
        return None
    full = [os.path.join(APP_LOGDIR, n) for n in cands]
    try:
        return max(full, key=lambda p: os.path.getmtime(p))
    except OSError:
        return None


def _tail_lines(path: str, max_bytes: int = 262144) -> List[str]:
    """读文件末尾若干字节并切行（日志可能很大，只读尾部就够）。"""
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()          # 丢掉可能被截断的首行
            return f.read().splitlines()
    except OSError:
        return []


def _parse_ts(s: str) -> Optional[float]:
    try:
        return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        try:
            return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            return None


def read_telemetry(hostname: str) -> dict:
    """解析该实例的遥测日志，返回原始读数 + 时效判定。

    返回键：
      ok            是否取到日志
      log           日志文件路径
      age_s         最后一条 [run] 遥测距今秒数（None=没找到该行）
      fresh         age_s 存在且 <= TELEMETRY_MAX_AGE_S
      phase/tps/p50/p95/p99/err_max/ok_count/fail_count/approved/declined
      slow_acquire_count / slow_acquire_max_ms
      pool_create_count / pool_close_count
    """
    out: Dict[str, Any] = {"ok": False, "log": "", "age_s": None, "fresh": False}
    path = _newest_log(hostname)
    if not path:
        return out
    out["ok"] = True
    out["log"] = path
    lines = _tail_lines(path)

    # —— 最后一条 [run] 遥测 ——
    last = None
    for ln in lines:
        m = _RE_RUNLINE.match(ln)
        if m:
            last = m
    if last:
        g = last.groups()
        ts = _parse_ts(g[0])
        out.update({
            "ts": g[0], "phase": g[1], "t": int(g[2]), "tps": float(g[3]),
            "ok_count": int(g[4]), "fail_count": int(g[5]),
            "approved": int(g[6]), "declined": int(g[7]),
            "p50": float(g[8]), "p95": float(g[9]), "p99": float(g[10]),
            "err_max": float(g[11]),
        })
        if ts is not None:
            out["age_s"] = round(max(time.time() - ts, 0.0), 1)
            out["fresh"] = out["age_s"] <= TELEMETRY_MAX_AGE_S

    # —— 池获取偏慢（java_pool_starve 的判据）——
    slow = [_RE_SLOW_ACQUIRE.search(ln) for ln in lines]
    slow_ms = [int(m.group(1)) for m in slow if m]
    out["slow_acquire_count"] = len(slow_ms)
    out["slow_acquire_max_ms"] = max(slow_ms) if slow_ms else None

    # —— 池生命周期（池实例泄漏的判据）——
    out["pool_create_count"] = sum(1 for ln in lines if _RE_POOL_CREATE.search(ln))
    out["pool_close_count"] = sum(1 for ln in lines if _RE_POOL_CLOSE.search(ln))
    return out


# ══════════════════════════════════════════════════════════════════════════
# 探针
# ══════════════════════════════════════════════════════════════════════════

class AppProbe(BaseProbe):
    """裸容器应用层探针：Docker API + 宿主 /proc + GaussDB 归因（全部只读）"""

    @property
    def identity(self) -> str:
        return "app"

    def collect(self, **kwargs) -> ProbeResult:
        t0 = time.time()
        facts: List[Dict[str, Any]] = []
        raw: Dict[str, Any] = {}
        alerts: List[Dict[str, Any]] = []
        failed: List[str] = []

        try:
            items = _list_app_containers()
        except Exception as e:                                # noqa: BLE001
            msg = "Docker API 不可达（%s）：%s" % (DOCKER_SOCK, str(e)[:160])
            raw["app_topology__error__"] = msg
            return ProbeResult(layer="app", facts=[], raw=raw, alerts=alerts,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               error=None, mode="probe_docker_proc")

        if not items:
            msg = "按名字过滤 %r 未匹配到任何容器" % APP_CONTAINER
            raw["app_topology__error__"] = msg
            return ProbeResult(layer="app", facts=[], raw=raw, alerts=alerts,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               error=None, mode="probe_docker_proc")

        btime = _host_btime()
        now = time.time()

        apps: Dict[str, dict] = {}
        instances: List[dict] = []
        datasources: List[dict] = []
        endpoints: List[dict] = []
        telemetry_raw: Dict[str, Any] = {}

        for it in items:
            cid = str(it.get("Id") or "")
            if not cid:
                continue
            try:
                insp = _docker_get("/containers/%s/json" % cid) or {}
            except Exception as e:                            # noqa: BLE001
                failed.append("inspect:%s" % str(e)[:60])
                continue

            cfg = insp.get("Config") or {}
            st = insp.get("State") or {}
            hostname = str(cfg.get("Hostname") or "")
            env = {}
            for kv in (cfg.get("Env") or []):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    env[k.strip()] = v.strip()
            hpid = int(st.get("Pid") or 0)
            image = str(cfg.get("Image") or "")

            # ── 应用身份（version 取自镜像 tag —— 唯一可得的权威版本来源）──
            app_id = APP_CODE.strip().lower()
            version = ""
            if ":" in image:
                version = image.rsplit(":", 1)[1]
            apps[app_id] = {
                "appId": app_id,
                "appCode": APP_CODE,
                "appName": APP_NAME,
                "version": version,
                "techStack": APP_TECHSTACK,
                "sloP99Ms": APP_SLO_P99_MS,
            }

            # ── 实例身份 + 进程级指标（宿主 /proc）──────────────────────
            inst_id = hostname or cid[:12]
            stt = _proc_status(hpid) if hpid else {}
            cmdline = _proc_cmdline(hpid) if hpid else ""
            artifact, jvm_ver = _artifact_and_jvm(cmdline)

            start_iso = ""
            uptime_s = None
            ticks = _proc_start_ticks(hpid) if hpid else None
            if ticks is not None and btime is not None:
                started = btime + ticks / _CLK_TCK
                uptime_s = round(max(now - started, 0.0), 1)
                try:
                    start_iso = _dt.datetime.fromtimestamp(
                        started, _dt.timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    start_iso = ""

            instances.append({
                "instanceId": inst_id,
                "appId": app_id,
                "containerId": cid,          # ★ 与 container 层同源 ⇒ RUNS_IN 能连上
                "jvmVersion": jvm_ver,
                "pid": hpid or None,
                "startTime": start_iso,
                # ↓ 非 CCF 字段（审计用）
                "artifact": artifact,
                "vmRssKb": stt.get("VmRSS", ""),
            })

            def fact(path: str, value: Any, obj_id: str, obj_type: str) -> None:
                facts.append(self._make_fact(path, value, obj_id, obj_type, "app:proc"))

            if uptime_s is not None:
                fact("app.uptime_s", uptime_s, inst_id, "app_instance")
            else:
                failed.append("uptime_s")

            try:
                thr = int(str(stt.get("Threads", "")).strip())
                fact("app.thread_count", thr, inst_id, "app_instance")
            except ValueError:
                failed.append("thread_count")

            # ── 连接池（GaussDB 侧按 application_name 归因）────────────
            ds = _ds_from_env(env, inst_id, image)
            ds["appInstanceId"] = inst_id
            datasources.append(ds)
            app_name = ds["applicationName"]
            try:
                counts = _pool_counts_by_state(app_name)
                # ★ 计数 0 是**真实读数**（idle 态没有 active 会话），不是「采不到」
                fact("app.datasource.active_connections",
                     int(counts.get("active", 0)), ds["datasourceId"], "jdbc_datasource")
                fact("app.datasource.idle_connections",
                     int(counts.get("idle", 0)), ds["datasourceId"], "jdbc_datasource")
            except Exception as e:                            # noqa: BLE001
                failed.append("pool_attribution:%s" % str(e)[:70])

            # ── ★ P0-B：应用实时遥测（延迟/TPS/池获取）──────────────
            #   ★ 端点对象**总是**建出（代表「该实例的仿真工作负载入口」），
            #     这样 EXPOSES 边恒成立、覆盖门禁的类型集不受「当前是否在跑」影响；
            #     而**指标读数**只在遥测新鲜时报值，陈旧时报 notCollected（不撒谎）。
            ep_id = "ep-%s-sim" % app_id
            if not any(e["endpointId"] == ep_id for e in endpoints):
                endpoints.append({
                    "endpointId": ep_id,
                    "appId": app_id,
                    "path": "/sim/run",
                    "protocol": "internal",
                    "sloP99Ms": APP_SLO_P99_MS,
                })

            tel = read_telemetry(inst_id)
            telemetry_raw[inst_id] = {k: tel.get(k) for k in
                                      ("log", "age_s", "fresh", "phase", "tps", "p50", "p95",
                                       "p99", "err_max", "ok_count", "fail_count",
                                       "slow_acquire_count", "slow_acquire_max_ms",
                                       "pool_create_count", "pool_close_count")}
            if not tel.get("ok"):
                failed.append("telemetry_log_not_found")

            def ep_fact(path: str, value: Any) -> None:
                """端点事实（obj_type=app_endpoint）：value=None ⇒ 下游落 notCollected。"""
                facts.append(self._make_fact(path, value, ep_id, "app_endpoint", "app:log"))

            fresh = bool(tel.get("fresh"))
            if fresh:
                ep_fact("app.endpoint.qps", tel.get("tps"))
                # ★ 2026-10-13 P0.6：TPS 与完整延迟分布（原只有 p99 ⇒ 无法区分
                #   「整体变慢」与「长尾变慢」）。tps 与 qps 在本仿真器同源（都会话事务计数），
                #   但**语义不同**（qps=查询、tps=事务），故并列两条而非二选一。
                ep_fact("app.endpoint.tps", tel.get("tps"))
                ep_fact("app.endpoint.p50_ms", tel.get("p50"))
                ep_fact("app.endpoint.p95_ms", tel.get("p95"))
                ep_fact("app.endpoint.p99_ms", tel.get("p99"))
                ep_fact("app.endpoint.max_ms", tel.get("err_max"))
                tot = int(tel.get("ok_count") or 0) + int(tel.get("fail_count") or 0)
                ep_fact("app.endpoint.error_rate",
                        round(int(tel.get("fail_count") or 0) / tot, 6) if tot else None)
            else:
                # 遥测陈旧（应用在 hold 待命、无进行中的 run）⇒ **速率/延迟类**显式 notCollected
                for p in ("app.endpoint.qps", "app.endpoint.tps", "app.endpoint.p50_ms",
                          "app.endpoint.p95_ms", "app.endpoint.p99_ms", "app.endpoint.max_ms",
                          "app.endpoint.error_rate"):
                    ep_fact(p, None)

            # ★★ 2026-10-13 P0.6：**累计量不受时效门限** ★★
            #   缘由：实时诊断里 agent 拿到的是一片 no_value，无从下手。但日志里的 `ok=` / `fail=`
            #   是**累计总数**——它不会因为应用停止跑而失效（它只是「不再增长」，不是「不再为真」）。
            #   ⇒ 陈旧时**照常落值**，让 agent 至少有真实数字可用；速率/延迟才受 freshness 约束。
            #   ★ 这不是放宽纪律：速率过期后当现值用才是造假；累计量的语义本身就不含时效。
            ep_fact("app.endpoint.success_total", tel.get("ok_count"))
            ep_fact("app.endpoint.fail_total", tel.get("fail_count"))

            # ★ 池获取等待属于 **JdbcDataSource**（不是端点）⇒ 必须用 jdbc_datasource 承载，
            #   否则会把一个数据源指标挂到端点上（对象/指标错配）。
            fact("app.datasource.acquire_wait_ms",
                 tel.get("slow_acquire_max_ms") if fresh else None,
                 ds["datasourceId"], "jdbc_datasource")

        raw["app_topology"] = {
            "apps": list(apps.values()),
            "app_instances": instances,
            # ★ P0-B：端点对象恒存在（代表该实例的仿真工作负载入口）；
            #   指标读数是否 collected 取决于遥测时效（见 read_telemetry）。
            "app_endpoints": endpoints,
            "datasources": datasources,
        }
        raw["docker_sock"] = DOCKER_SOCK
        raw["host_proc"] = HOST_PROC
        raw["app_container_filter"] = APP_CONTAINER
        raw["app_logdir"] = APP_LOGDIR
        raw["telemetry_max_age_s"] = TELEMETRY_MAX_AGE_S
        raw["telemetry"] = telemetry_raw
        raw["proc_reads_failed"] = failed

        if failed:
            alerts.append(self._make_alert(
                "warning", "应用探针部分读数缺失",
                "以下读数采集失败（该指标不可判，而非为 0）：%s" % failed[:8],
                APP_CONTAINER))

        return ProbeResult(layer="app", facts=facts, raw=raw, alerts=alerts,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           error=None, mode="probe_docker_proc")


register_probe(AppProbe())
