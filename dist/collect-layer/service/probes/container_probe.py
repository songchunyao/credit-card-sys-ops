# -*- coding: utf-8 -*-
"""container 层探针（M1.6 新增，2026-10-12 重写为**裸 docker 容器**通道）

职责：把 **被测信用卡应用所在容器** 的运行态与身份采成一层的 facts + 拓扑段。

================================================================================
★ 为什么重写（2026-10-12 真机实测）
================================================================================
上一版按 **k8s** 假设写的：身份取自 Prometheus 的 `kube_pod_container_info`、
状态取自 `kube_pod_container_status_running/waiting/terminated`。
但 L2 载体已经改成 **192.0.2.10 上的裸 docker 容器**
（`credit-card-sim-cardsim-1`，见 `<workspace>\\cardsim\\docs\\RUNTIME-INVENTORY.md`），
节点上**不存在 kube-state-metrics** ⇒ 实测 `container` 层 `fact_count=0`、
`section_errors.container_topology = "取不到容器 id（kube_pod_container_info 无结果）"`，
`freeze.py` 的 H1 闸门（每层事实数必须 > 0）直接拒绝写出快照。

================================================================================
★ 新通道（三条，全部只读；2026-10-12 用一次性容器以 cco 身份实测通过）
================================================================================
1. **Docker Engine API over unix socket**（`/var/run/docker.sock`，宿主挂载进本容器）
   容器身份 / 状态 / 资源限额 / 镜像 / Env —— 这是容器元数据的权威来源。
   需要容器进程在宿主的 `docker` 组里（compose 侧 `group_add`）。
   用**纯标准库** `socket.AF_UNIX` + `http.client`，不引入 docker-py。

2. **宿主 cgroup v2 文件**（`/proc/<宿主pid>/cgroup` 定位，挂在 `/host/sys/fs/cgroup`）
   `memory.events` 的 `oom_kill`、`cpu.stat` 的 `nr_throttled` 是
   `FM-CTR-OOM-KILLED` / `FM-CTR-CPU-THROTTLED` 的**直接证据源**（RUNTIME-INVENTORY §2.3 明示）。
   ★ cgroup 路径必须用**完整 64 位容器 id**：实测短 id 路径不存在。

3. **宿主 /proc**（挂 `/host/proc`）
   仅用于兜底定位 cgroup 路径与宿主标识；`status`/`stat`/`cmdline` 是 0444 世界可读，
   `environ`/`fd` 是 0400 需 root —— 本探针**不依赖**后者（容器 Env 走 Docker API）。

================================================================================
输出契约（不变）
================================================================================
  · facts：`container.*` 共 6 个指标码（见 shared/model/metrics.yaml），
    obj_type 一律 `container`，obj_id = **完整 64 位容器 id**（与 `docker inspect .Id` 同源，
    也是 app 层 `RUNS_IN` 的 containerId 取值）
  · raw.container_topology = {"hosts": [...], "containers": [...]}
    字段名逐字等于 shared/mapping/v3-ccf-map.yaml 的 `app_derive.keys.*`
    （hosts: hostId/hostname/ip/role/osVersion/kernelVersion/cpuCores/memTotalGb
      containers: containerId/hostId/image/imageDigest/state/restartCount/memLimitMb/cpuLimitCores）
    多出来的字段（dockerState / paused / pidsCurrent / cpuUsageMethod …）**不是** CCF 属性
    ⇒ 会被显式忽略（不建属性、不猜测），仅留在 gather 信封里供审计。

================================================================================
★ 窗口基线（delta）契约 —— M2.10，本次改为**采集侧状态文件**
================================================================================
Prometheus 版靠 `increase(<series>[300s])` 拿窗口增量（服务端存历史）。
裸 docker 的 cgroup 文件只有**当前累计值**，没有历史 ⇒ 本探针用一个**状态文件**
（`CC_PROBE_STATE`，默认 `/tmp/cc-container-probe-state.json`）保存上一次读数，
在下次采集时做差得到增量。

· 窗口长度：`deltaWindowS` = **实际两次采样的间隔秒数**（整数、≥1），
  不再谎称 300 —— 报出真实窗口才可解释。
· 上一次采样距今超过 `DELTA_WINDOW_S` ⇒ 基线过期，**不产出 delta**
  （宁可让 `predicate: increased` 判成 unjudgeable，也不产出语义不完整的增量）。
· 只对**单调不减的累计型计数器**产出 delta：
  `container.cpu_throttled_periods` / `container.oom_kill_total` / `container.restart_total`。
  比率型（cpu_usage_ratio）与瞬时型（mem_working_set_mb）/ 分类型（status）**绝不**带 delta。
· 状态文件读写失败一律**降级为「无 delta」**，不影响其余事实产出（fail-soft，不 fail-closed）。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from . import BaseProbe, ProbeResult, register_probe

# ── 配置（全部可经环境变量覆盖；不出现裸字面量以外的魔法数字）──────────────
#
# ★ 数值键**必须容忍空串**（2026-10-12 事故的另一面）：
#   `.env` 里写 `KEY=` 会让 `os.environ.get(K, default)` 返回 **""** 而不是 default，
#   直接 `float("")` 会在**模块导入期**抛 ValueError ⇒ 整层探针注册失败。
#   `GAUSSDB_SSH_MUX_PERSIST` 就是这么把 os 层打瘫的（ssh 收到空的 ControlPersist）。
#   故本文件所有数值键一律经 `_env_num()` 取值。
def _env_num(key: str, default: float) -> float:
    """读一个数值型环境变量；缺省 / 空串 / 非法值一律回落 default（不抛异常）。"""
    raw = os.environ.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


DOCKER_SOCK = os.environ.get("CC_DOCKER_SOCK", "/var/run/docker.sock")
APP_CONTAINER = os.environ.get("CC_APP_CONTAINER", "credit-card-sim-cardsim")
HOST_CGROUP = os.environ.get("CC_HOST_CGROUP", "/host/sys/fs/cgroup").rstrip("/")
HOST_PROC = os.environ.get("CC_HOST_PROC", "/host/proc").rstrip("/")
HOST_IP = os.environ.get("CC_HOST_IP", "").strip()
#: 宿主 init 的 pid（用于取宿主 netns 视图；见 `_host_ip` 的坑①）
HOST_INIT_PID = os.environ.get("CC_HOST_INIT_PID", "1").strip() or "1"
STATE_PATH = os.environ.get("CC_PROBE_STATE", "/tmp/cc-container-probe-state.json")
DOCKER_TIMEOUT = _env_num("CC_DOCKER_TIMEOUT", 10.0)

#: 宿主的 docker 组 gid（compose 的 group_add 会带上；这里只用于诊断信息）
_MB = 1024 * 1024
_CLK_TCK = 100.0  # 仅用于兜底：从 /proc/<pid>/stat 反推启动时刻


def _delta_window_s() -> int:
    """窗口基线的**最大**容忍间隔（秒）。0 / 缺省 / 非法 ⇒ 0 = 关闭（不产出 delta）。

    ★ 与 app 层同名同义（P11 共享参数）；写成代码自己的默认值 300，
      但**绝不允许 .env 注入空串**（空串会让 os.environ.get 拿到 ""，
      2026-10-12 已在 GAUSSDB_SSH_MUX_PERSIST 上踩过同类事故）。
    """
    raw = os.environ.get("DELTA_WINDOW_S", "300")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0
    return int(round(v)) if v > 0 else 0


DELTA_WINDOW_S = _delta_window_s()


# ══════════════════════════════════════════════════════════════════════════
# Docker Engine API（unix socket，纯标准库）
# ══════════════════════════════════════════════════════════════════════════

class _UnixHTTP(http.client.HTTPConnection):
    """走 unix socket 的 HTTPConnection（Docker API 用的就是普通 HTTP over UDS）。"""

    def __init__(self, sock_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self):                                        # noqa: D102
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _docker_get(path: str) -> Any:
    """GET 一个 Docker API 端点，返回解析后的 JSON（失败抛异常，由调用方归纳）。"""
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
    """列出名字匹配 `CC_APP_CONTAINER` 的容器（含已停止的，便于识别 Terminated）。"""
    flt = json.dumps({"name": [APP_CONTAINER]})
    q = "/containers/json?all=1&filters=" + _urlquote(flt)
    return list(_docker_get(q) or [])


def _urlquote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


# ══════════════════════════════════════════════════════════════════════════
# 宿主 cgroup v2
# ══════════════════════════════════════════════════════════════════════════

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _cgroup_dir(full_cid: str, host_pid: Optional[int]) -> Optional[str]:
    """定位容器在**宿主** cgroup 树里的目录。

    ★ 顺序要紧：先用 `/host/proc/<宿主pid>/cgroup` 反查**真实相对路径**，
      再退回按完整容器 id 拼 `/system.slice/docker-<id>.scope`。
      实测短 id（12 位）拼出来的路径**不存在** —— 这是上一版没踩到、本版必须避开的坑。
    """
    if host_pid:
        rel = _read_text("%s/%d/cgroup" % (HOST_PROC, host_pid))
        if rel:
            # 形如 `0::/system.slice/docker-<64hex>.scope`
            for line in rel.splitlines():
                if "::" in line:
                    p = line.split("::", 1)[1].strip()
                    if p and p != "/":
                        cand = HOST_CGROUP + p
                        if os.path.isdir(cand):
                            return cand
    cand = "%s/system.slice/docker-%s.scope" % (HOST_CGROUP, full_cid)
    return cand if os.path.isdir(cand) else None


def _cgroup_kv(cg_dir: str, name: str) -> Dict[str, float]:
    """把 `cpu.stat` / `memory.events` 这类「每行一个 key value」的文件读成字典。"""
    txt = _read_text(os.path.join(cg_dir, name))
    out: Dict[str, float] = {}
    for line in (txt or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                continue
    return out


def _parse_cpu_max(txt: Optional[str]) -> Optional[float]:
    """`cpu.max` = `<quota> <period>`（cgroup v2），折算成**可用核数**。"""
    if not txt:
        return None
    parts = txt.split()
    if len(parts) < 2 or parts[0] == "max":
        return None
    try:
        return float(parts[0]) / float(parts[1])
    except (ValueError, ZeroDivisionError):
        return None


def _host_ip() -> str:
    """宿主主 IP。

    ★★ 两个实测坑（2026-10-12），都必须绕开 ★★
      ① `/proc/net`（含 fib_trie / route）跟随**读取进程自身的 netns**，
         不是「被读 /proc 的那个进程」的 netns。所以经宿主 /proc 挂载去读
         `/host/proc/net/fib_trie`，拿到的是**采集容器自己**的地址
         （实测首个命中 `192.0.2.18` = ccops 容器 IP），根本不是宿主业务 IP。
         正解是读 `/proc/<pid>/net/...` —— 那才是「该 pid 所在 netns」的视图。
         取宿主 init（PID 1，必在宿主 netns）即得宿主视图（实测返回 `192.0.2.10`）。
      ② fib_trie 的 `/32 host LOCAL` 里混着**全部 docker 网桥**（172.17–172.25 各一个），
         「取第一个」会写进错误的值 ⇒ 必须按保留网段过滤（见 `_is_usable_host_ip`）。
    一个可用地址都没有 ⇒ **留空**（不猜、不填占位）。
    """
    if HOST_IP:
        return HOST_IP
    for pid in (HOST_INIT_PID, "self"):
        txt = _read_text("%s/%s/net/fib_trie" % (HOST_PROC, pid))
        if not txt:
            continue
        lines = txt.splitlines()
        for i, line in enumerate(lines):
            if "/32 host LOCAL" not in line or i == 0:
                continue
            prev = lines[i - 1].strip()
            if not prev.startswith("|-- "):
                continue
            ip = prev[4:].strip()
            if _is_usable_host_ip(ip):
                return ip
    return ""


def _is_usable_host_ip(ip: str) -> bool:
    """排除回环 / link-local / docker 默认网桥池（192.0.2.16/12）。

    docker 的默认 bridge 地址池就在 172.16–172.31 之间（本机实测宿主上同时存在
    172.17–172.25 共 9 个网桥地址）⇒ 这些是**容器网络**地址，不是宿主业务地址。
    """
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast:
        return False
    for net in ("192.0.2.16/12", "169.254.0.0/16"):
        try:
            if a in ipaddress.ip_network(net):
                return False
        except ValueError:
            pass
    return True


# ══════════════════════════════════════════════════════════════════════════
# 状态文件（窗口基线）
# ══════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _attach_delta(fact: Dict[str, Any], delta: Any, win: int) -> Dict[str, Any]:
    """把 delta + deltaWindowS 挂到事实 dict 上（纯新增可选键，不改任何既有键）。

    ★ 两者同进同出：没有窗口长度就无法解释增量
      （ccf.schema.json $defs.observation 的 if/then 已锁定该约束）。
    """
    if delta is None or win <= 0:
        return fact
    fact["delta"] = delta
    fact["deltaWindowS"] = int(win)
    return fact


# ══════════════════════════════════════════════════════════════════════════
# 状态映射：docker → CCF 契约 enum
# ══════════════════════════════════════════════════════════════════════════
#
# ★ 契约取值域是**结构性封闭**的 4 值 {Running, Waiting, Terminated, Unknown}
#   （shared/model/metrics.yaml 的 container.status.enum，并由
#    scheme2-onto/shapes/shacl-constraints.ttl:152 的 sh:in 强制）。
#
# ★ 裸 docker 的状态域是 {created, running, paused, restarting, removing, exited, dead}，
#   与 k8s 词表**不完全同构**。本探针做**包含式**映射（subsumption），不发明新值：
#       running                → Running
#       paused                 → Waiting   ← 语义最贴近：「未在运行、也未终止」
#                                            （精确的 paused 证据留在信封的 dockerState/paused 字段）
#       created / restarting   → Waiting
#       exited / dead / removing → Terminated
#       其它                   → Unknown
# ★ 为什么 paused 不映射成 Unknown：Unknown 的语义是「**无法判定**」，
#   而 paused 是**明确可判定**的；把它塞进 Unknown 是另一种失真。
# ★ 后续建议（**契约变更，本次刻意不做**）：把 Paused 加进 enum。
#   它会连带改动 metrics.yaml / topology.yaml / shacl-constraints.ttl /
#   cc-topology.ttl / v3-fact-inventory.json + 多份 docs 里 pin 的 sha256，
#   属应当单独立项的一次契约演进，不应夹在「重写探针」里悄悄做。
_DOCKER_STATE_MAP = {
    "running": "Running",
    "paused": "Waiting",
    "created": "Waiting",
    "restarting": "Waiting",
    "exited": "Terminated",
    "dead": "Terminated",
    "removing": "Terminated",
}


def _map_state(docker_state: str) -> str:
    return _DOCKER_STATE_MAP.get((docker_state or "").lower(), "Unknown")


# ══════════════════════════════════════════════════════════════════════════
# 探针
# ══════════════════════════════════════════════════════════════════════════

class ContainerProbe(BaseProbe):
    """裸 docker 容器层探针：Docker API + 宿主 cgroup（只读）"""

    @property
    def identity(self) -> str:
        return "container"

    # ── 宿主身份（Docker /info：不需要额外挂载）──
    def _host_record(self, info: dict) -> dict:
        name = str(info.get("Name") or "")
        mem = info.get("MemTotal")
        return {
            "hostId": name,
            "hostname": name,
            "ip": _host_ip(),
            "role": "app-node",
            "osVersion": str(info.get("OperatingSystem") or "")[:120],
            "kernelVersion": str(info.get("KernelVersion") or "")[:120],
            "cpuCores": int(info["NCPU"]) if info.get("NCPU") else None,
            "memTotalGb": round(mem / (1024 ** 3), 1) if mem else None,
        }

    # ── ★ 2026-10-12：应用节点的**本机** OS 指标（不需要 SSH）────────────
    # 背景：`os` 探针是 **SSH 式**的，其目标来自 `GAUSSDB_NODES`（3 个 DB 节点），
    #   **从不包含应用节点**。而应用节点（build-host = 192.0.2.10）与 DB 节点
    #   （192.0.2.182-184）**不同网段**，SSH 到它此前实测认证失败。
    # ⇒ 结果：`Host` 对象 `build-host` 建得出来（容器探针的 hosts[]），但 `host.*` 指标
    #   **一条都没有** —— 这正是「OS(应用节点) host:build-host 未取到指标」的根因。
    # ★ 解法：采集容器**已挂载** `/host/proc`（宿主命名空间的 proc）⇒ 本地直读即可，
    #   无需 SSH、无需凭据、无需改契约（下面这些 path **早已有**到 host.* 的映射）。
    #   · `host.cpu_usage`      → host.cpu_usage_ratio（比率，需两次采样）
    #   · `os.mem.free_kb/total_kb` → 派生 host.mem_available_ratio
    #   · `os.load.avg1/avg5`   → host.load1 / host.load5
    #   · `host.disk_util`      → host.disk_util（%）
    #   · `node.disk_io_wait`   → host.disk_io_wait（%）
    # ★ 纪律：取不到就不发该事实（**绝不发 0**）；比率型需基线，首次采样不发。
    def _local_host_os_facts(self, host_id: str, state: dict, now: float,
                             win_max: int) -> List[dict]:
        out: List[dict] = []
        key = "_host:" + host_id

        def emit(path: str, value: Any, obj_type: str = "node") -> None:
            if value is None:
                return
            out.append(self._make_fact(path, value, host_id, obj_type, "host:/proc(local)"))

        # 1) /host/proc/meminfo —— MemTotal / MemFree（kB）
        mi = _read_text(os.path.join(HOST_PROC, "meminfo")) or ""
        mem = {}
        for line in mi.splitlines():
            m = re.match(r"^(MemTotal|MemFree|MemAvailable):\s+(\d+)\s*kB", line.strip())
            if m:
                mem[m.group(1)] = int(m.group(2))
        if mem.get("MemTotal"):
            emit("os.mem.total_kb", mem["MemTotal"])
            # ★ 优先用 MemAvailable（内核给出的**真正可用**）；没有才退回 MemFree。
            #   契约的派生用的是 free_kb（保守代理），这里同一条 path 不变。
            emit("os.mem.free_kb", mem.get("MemAvailable", mem.get("MemFree")))

        # 2) /host/proc/loadavg —— load1 / load5
        la = (_read_text(os.path.join(HOST_PROC, "loadavg")) or "").split()
        if len(la) >= 2:
            try:
                emit("os.load.avg1", float(la[0]))
                emit("os.load.avg5", float(la[1]))
            except ValueError:
                pass

        # 3) /host/proc/stat —— CPU 使用率 + iowait 占比（需与上次采样做差）
        st = _read_text(os.path.join(HOST_PROC, "stat")) or ""
        cpu_line = next((l for l in st.splitlines() if l.startswith("cpu ")), "")
        if cpu_line:
            try:
                v = [float(x) for x in cpu_line.split()[1:]]
                # user nice system idle iowait irq softirq steal [guest guest_nice]
                total = sum(v[:8])
                idle_all = v[3] + (v[4] if len(v) > 4 else 0.0)
                iowait = v[4] if len(v) > 4 else 0.0
            except (ValueError, IndexError):
                total = idle_all = iowait = None
            if total:
                prev = state.get(key) or {}
                pt, pi, pw, pts = (prev.get("cpu_total"), prev.get("cpu_idle"),
                                   prev.get("cpu_iowait"), prev.get("ts"))
                if (isinstance(pt, (int, float)) and isinstance(pts, (int, float))
                        and win_max > 0 and 0 < (now - pts) <= win_max and total > pt):
                    dtot, didle = total - pt, idle_all - float(pi)
                    if dtot > 0:
                        emit("host.cpu_usage", round(max(0.0, min(1.0, 1.0 - didle / dtot)), 4))
                        if isinstance(pw, (int, float)):
                            emit("node.disk_io_wait",
                                 round(max(0.0, (iowait - float(pw)) / dtot) * 100.0, 3))

        # 4) /host/proc/diskstats —— 整盘 I/O 忙碌占比（%）
        ds = _read_text(os.path.join(HOST_PROC, "diskstats")) or ""
        io_ms = 0.0
        whole = 0
        for line in ds.splitlines():
            f = line.split()
            if len(f) < 14:
                continue
            name = f[2]
            if name.startswith(("loop", "ram", "dm-", "sr")):
                continue
            if re.search(r"\d$", name) and not re.search(r"nvme\d+n\d+$", name):
                continue                     # 分区（sda1/vda2…）不计，避免与整盘重复
            try:
                io_ms += float(f[12])        # 字段 13 = 做 I/O 的毫秒数
                whole += 1
            except (ValueError, IndexError):
                continue
        if whole:
            prev = state.get(key) or {}
            pio, pts = prev.get("disk_io_ms"), prev.get("ts")
            if (isinstance(pio, (int, float)) and isinstance(pts, (int, float))
                    and 0 < (now - pts) <= win_max and now > pts):
                wall_ms = (now - pts) * 1000.0
                if wall_ms > 0:
                    emit("host.disk_util",
                         round(max(0.0, min(100.0, (io_ms - float(pio)) / wall_ms * 100.0)), 3))

        # 5) 写回本机基线
        nxt = state.setdefault(key, {})
        nxt["ts"] = now
        if cpu_line:
            try:
                v = [float(x) for x in cpu_line.split()[1:]]
                nxt["cpu_total"] = sum(v[:8])
                nxt["cpu_idle"] = v[3] + (v[4] if len(v) > 4 else 0.0)
                nxt["cpu_iowait"] = v[4] if len(v) > 4 else 0.0
            except (ValueError, IndexError):
                pass
        if whole:
            nxt["disk_io_ms"] = io_ms
        return out

    def collect(self, **kwargs) -> ProbeResult:
        t0 = time.time()
        facts: List[Dict[str, Any]] = []
        raw: Dict[str, Any] = {}
        alerts: List[Dict[str, Any]] = []
        failed: List[str] = []

        # ── 1) 宿主信息 ────────────────────────────────────────────────
        try:
            info = _docker_get("/info") or {}
        except Exception as e:                                # noqa: BLE001
            msg = "Docker API 不可达（%s）：%s" % (DOCKER_SOCK, str(e)[:160])
            raw["container_topology__error__"] = msg
            return ProbeResult(layer="container", facts=[], raw=raw, alerts=alerts,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               error=None, mode="probe_docker")

        # ── 2) 找出被测容器 ────────────────────────────────────────────
        try:
            items = _list_app_containers()
        except Exception as e:                                # noqa: BLE001
            msg = "列举容器失败：%s" % str(e)[:160]
            raw["container_topology__error__"] = msg
            return ProbeResult(layer="container", facts=[], raw=raw, alerts=alerts,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               error=None, mode="probe_docker")

        if not items:
            msg = ("按名字过滤 %r 未匹配到任何容器（CC_APP_CONTAINER 配错？"
                   "容器名可经 docker ps 核对）" % APP_CONTAINER)
            raw["container_topology__error__"] = msg
            return ProbeResult(layer="container", facts=[], raw=raw, alerts=alerts,
                               elapsed_ms=int((time.time() - t0) * 1000),
                               error=None, mode="probe_docker")

        host = self._host_record(info)
        state = _load_state()
        now = time.time()
        win_max = DELTA_WINDOW_S

        containers: List[dict] = []
        cpu_methods: Dict[str, str] = {}

        for it in items:
            cid = str(it.get("Id") or "")
            if not cid:
                continue
            try:
                insp = _docker_get("/containers/%s/json" % cid) or {}
            except Exception as e:                            # noqa: BLE001
                failed.append("inspect:%s" % str(e)[:60])
                continue

            st = insp.get("State") or {}
            hcfg = insp.get("HostConfig") or {}
            cfg = insp.get("Config") or {}
            nets = (insp.get("NetworkSettings") or {}).get("Networks") or {}

            docker_state = str(st.get("Status") or "")
            paused = bool(st.get("Paused"))
            status = _map_state("paused" if paused and docker_state == "running" else docker_state)
            restarts = int(insp.get("RestartCount") or 0)
            nano = int(hcfg.get("NanoCpus") or 0)
            cpu_limit = round(nano / 1e9, 3) if nano else None
            mem_limit = int(hcfg.get("Memory") or 0)

            rec = {
                "containerId": cid,
                "hostId": host["hostId"],
                "image": str(cfg.get("Image") or ""),
                "imageDigest": str(insp.get("Image") or "")[:120],
                "state": status,
                "restartCount": restarts,
                "memLimitMb": int(round(mem_limit / _MB)) if mem_limit else None,
                "cpuLimitCores": cpu_limit,
                # ↓ 以下不是 CCF 属性：留在信封里供审计（app_derive 会忽略未声明字段）
                "dockerState": docker_state,
                "paused": paused,
                "containerHostname": str(cfg.get("Hostname") or ""),
                "ip": str((list(nets.values())[0] if nets else {}).get("IPAddress") or ""),
                "startedAt": str(st.get("StartedAt") or ""),
                "hostPid": int(st.get("Pid") or 0),
                "name": str(insp.get("Name") or "").lstrip("/"),
            }
            containers.append(rec)

            def fact(path: str, value: Any, delta: Any = None, win: int = 0) -> None:
                f = self._make_fact(path, value, cid, "container", "container:docker")
                facts.append(_attach_delta(f, delta, win))

            # ── 3) cgroup（限流 / OOM / 内存 / CPU 的**直接证据源**）────
            cg = _cgroup_dir(cid, rec["hostPid"] or None)
            if not cg:
                failed.append("cgroup_dir")
                rec["cgroupDir"] = ""
                # 容器身份仍可用 ⇒ 至少把状态类事实发出去，不整层失败
                fact("container.status", status)
                fact("container.restart_total", restarts,
                     *self._counter_delta(state, cid, "restart_total", restarts, now, win_max))
                continue
            rec["cgroupDir"] = cg

            cpu = _cgroup_kv(cg, "cpu.stat")
            events = _cgroup_kv(cg, "memory.events")
            mem_stat = _cgroup_kv(cg, "memory.stat")
            cpu_max_cores = _parse_cpu_max(_read_text(os.path.join(cg, "cpu.max"))) or cpu_limit

            # 3.1 CPU 使用率（比率型：**不带** delta）
            usage = cpu.get("usage_usec")
            if usage is not None:
                prev = state.get(cid) or {}
                prev_usage = prev.get("usage_usec")
                prev_ts = prev.get("ts")
                ratio = None
                method = ""
                if (isinstance(prev_usage, (int, float)) and isinstance(prev_ts, (int, float))
                        and win_max > 0 and (now - prev_ts) <= win_max and now > prev_ts):
                    dt = now - prev_ts
                    used = max(usage - float(prev_usage), 0.0)
                    if cpu_max_cores:
                        ratio = round((used / 1e6) / dt / cpu_max_cores, 4)
                        method = "window_rate"
                if ratio is None and cpu_max_cores:
                    # 兜底：自容器启动以来的**长时均值**（诚实标注，见 raw.cpuUsageMethod）
                    started = rec["startedAt"]
                    try:
                        import datetime as _dt
                        s = started.replace("Z", "+00:00")
                        st_epoch = _dt.datetime.fromisoformat(s).timestamp()
                        up = now - st_epoch
                        if up > 0:
                            ratio = round((usage / 1e6) / up / cpu_max_cores, 4)
                            method = "since_start_average"
                    except (ValueError, TypeError):
                        pass
                if ratio is not None:
                    fact("container.cpu_usage_ratio", ratio)
                    cpu_methods[cid] = method
            else:
                failed.append("cpu.stat")

            # 3.2 CPU 限流周期（累计型：delta）
            thr = cpu.get("nr_throttled")
            if thr is not None:
                d, w = self._counter_delta(state, cid, "nr_throttled", thr, now, win_max)
                fact("container.cpu_throttled_periods", int(thr), d, w)
            else:
                failed.append("nr_throttled")

            # 3.3 内存工作集（瞬时型：**不带** delta）
            #     working set = memory.current - inactive_file（cgroup v2 口径）
            cur = _read_text(os.path.join(cg, "memory.current"))
            try:
                cur_v = float(cur) if cur is not None else None
            except ValueError:
                cur_v = None
            if cur_v is not None:
                inactive = mem_stat.get("inactive_file")
                ws = cur_v - inactive if (inactive is not None and inactive <= cur_v) else cur_v
                fact("container.mem_working_set_mb", round(ws / _MB, 2))
            else:
                failed.append("memory.current")

            # 3.4 OOM 击杀（累计型：delta）—— FM-CTR-OOM-KILLED 的唯一证据源
            oom = events.get("oom_kill")
            if oom is not None:
                d, w = self._counter_delta(state, cid, "oom_kill", oom, now, win_max)
                fact("container.oom_kill_total", int(oom), d, w)
            else:
                failed.append("memory.events")

            # 3.5 重启次数（累计型：delta；Docker 侧来源）
            d, w = self._counter_delta(state, cid, "restart_total", restarts, now, win_max)
            fact("container.restart_total", restarts, d, w)

            # 3.6 状态（分类型）
            fact("container.status", status)

            # 更新基线
            nxt = state.setdefault(cid, {})
            nxt["ts"] = now
            if usage is not None:
                nxt["usage_usec"] = float(usage)
            if thr is not None:
                nxt["nr_throttled"] = float(thr)
            if oom is not None:
                nxt["oom_kill"] = float(oom)
            nxt["restart_total"] = float(restarts)
            nxt["cgroupDir"] = cg

        # ── 4) ★ 应用节点本机 OS 指标（/host/proc；详见 _local_host_os_facts）──
        #   必须在 _save_state 之前调用，否则本机基线不落盘、永远算不出比率。
        try:
            hf = self._local_host_os_facts(host["hostId"], state, now, win_max)
            facts.extend(hf)
            raw["local_host_os_fact_count"] = len(hf)
        except Exception as e:                                # noqa: BLE001
            failed.append("local_host_os:%s" % str(e)[:80])

        _save_state(state)

        raw["container_topology"] = {"hosts": [host], "containers": containers}
        raw["docker_sock"] = DOCKER_SOCK
        raw["host_cgroup"] = HOST_CGROUP
        raw["app_container_filter"] = APP_CONTAINER
        raw["delta_window_s"] = win_max
        raw["cpu_usage_method"] = cpu_methods
        raw["docker_queries_failed"] = failed

        if failed:
            alerts.append(self._make_alert(
                "warning", "容器探针部分读数缺失",
                "以下读数为空（该指标不可判，而非为 0）：%s" % failed[:8],
                APP_CONTAINER))

        return ProbeResult(layer="container", facts=facts, raw=raw, alerts=alerts,
                           elapsed_ms=int((time.time() - t0) * 1000),
                           error=None, mode="probe_docker")

    # ── 累计型计数器 → 窗口增量 ────────────────────────────────────────
    @staticmethod
    def _counter_delta(state: dict, cid: str, key: str, cur: float,
                       now: float, win_max: int) -> Tuple[Optional[float], int]:
        """返回 (delta, deltaWindowS)；基线缺失/过期/计数器回退 ⇒ (None, 0)。

        ★ 宁缺勿造（与 M2.10 同一条纪律）：
          · 无上一次读数 ⇒ 不产出（首次采集）
          · 间隔 > DELTA_WINDOW_S ⇒ 基线过期，不产出
          · 计数变小（容器被重建）⇒ 不产出，也**不**取绝对值
        """
        if win_max <= 0:
            return None, 0
        if not isinstance(cur, (int, float)) or isinstance(cur, bool):
            return None, 0
        prev = state.get(cid) or {}
        pv, pts = prev.get(key), prev.get("ts")
        if not isinstance(pv, (int, float)) or isinstance(pv, bool):
            return None, 0
        if not isinstance(pts, (int, float)) or now <= pts:
            return None, 0
        gap = now - pts
        if gap > win_max:
            return None, 0
        d = float(cur) - float(pv)
        if d < 0:
            return None, 0
        win = int(round(gap))
        if win < 1:
            return None, 0
        return d, win


register_probe(ContainerProbe())
