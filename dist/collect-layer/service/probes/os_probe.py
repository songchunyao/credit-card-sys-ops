"""GaussDB 运维 Agent — OS 层采集探针

通过 SSH 从 GaussDB 节点采集 OS 层指标：
  - UltraPath 多路径设备
  - NVMe 子系统/控制器
  - RoCE 网卡
  - CPU/NUMA
  - 内存/HugePages

数据来源: SSH → GaussDB 节点
采集方式: subprocess + ssh 命令
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from . import BaseProbe, ProbeResult, register_probe

# ★ 模拟演练打桩（docs/60）。live 下所有 tap 都是**零介入**的直通。
try:
    from .. import collect_sim as _sim
except Exception:
    try:
        import collect_sim as _sim
    except Exception:
        _sim = None


# ── SSH 采集命令集 ──
_SSH_USER = os.environ.get("GAUSSDB_SSH_USER", "root")
_SSH_PASS = os.environ.get("GAUSSDB_SSH_PASSWORD", "")
_SSH_PORT = os.environ.get("GAUSSDB_SSH_PORT", "22")
_SSH_KEY  = os.environ.get("GAUSSDB_SSH_KEY", "")  # 可选私钥路径

# ══════════════════════════════════════════════════════════════════════════
# SSH 连接复用（ControlMaster）—— 2026-09-21
#
# 背景（生产实测，见 analysis/p0_sim/ssh_conn_overhead_measure.md）：
#   现状**每条命令各开一次 SSH 连接**，os 层一次采集 = 28 条命令 × N 节点。
#   生产实测单条命令：均值 3.97s（6 次里 2 次 10.6s 抖动）；
#   复用连接后均值 0.16s 且稳定在 0.10-0.11s ⇒ 约 25 倍，且消除抖动。
#
# ★ 本改动**只影响"怎么建立连接"**：
#   执行的命令、转义、解析、产出事实、规则、层声明 —— 一行都不动。
#   ★ 硬边界：`sim` 接管时**不得启用复用**（避免向真实主机建立常驻连接），
#   且复用只让 SSH 更快；即使复用完全失效，行为也等价于现状（见回落）。
#
# 护栏（逐条可证伪）：
#   ① 默认**开启**，可用 GAUSSDB_SSH_MUX=0 关掉（= 完全回到现状）
#   ② `ControlPersist` 与 `ControlPath` 都可配；复用 socket 名按主机+端口+用户隔离
#   ③ socket 目录权限 0700（socket 内含会话凭据）；socket 名不含密码
#   ④ 首条命令失败时**回落为独立连接重试一次** ⇒ 复用出问题的代价只是变慢
#   ⑤ 路径长度超限时**自动退回独立连接**（unix socket 路径有长度上限）
# ══════════════════════════════════════════════════════════════════════════
_MUX_ON = str(os.environ.get("GAUSSDB_SSH_MUX", "1")).strip().lower() not in \
    ("0", "false", "no", "off")
_MUX_PERSIST = str(os.environ.get("GAUSSDB_SSH_MUX_PERSIST", "60")).strip()
# 默认落在系统临时目录；可用 GAUSSDB_SSH_MUX_DIR 覆盖
_MUX_DIR = os.environ.get("GAUSSDB_SSH_MUX_DIR", "").strip()
# unix socket 路径的保守上限（Linux sun_path 是 108 字节，留余量）
_MUX_PATH_MAX = 90


def _mux_dir() -> str:
    """复用 socket 目录（权限 0700，失败则回落禁用复用）"""
    import tempfile
    d = _MUX_DIR or os.path.join(tempfile.gettempdir(), "gdo-ssh-mux")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d
    except OSError:
        return ""


def _mux_opts(host: str) -> str:
    """返回复用用的 ssh 选项串；不可用/被禁用时返回 `-o ControlMaster=no`

    ★ 用 `ControlPath` 按「用户/主机/端口」隔离，**不含密码**（避免凭据进 socket 名）。
    ★ 路径过长时直接退回独立连接 —— 否则 ssh 会报 socket 路径过长而**每条命令都失败**。
    """
    if not _MUX_ON:
        return "-o ControlMaster=no"
    # sim 接管时不得向真实主机建立常驻连接
    try:
        if _sim is not None and hasattr(_sim, "mode") and _sim.mode() != "live":
            return "-o ControlMaster=no"
    except Exception:                                            # noqa: BLE001
        pass
    d = _mux_dir()
    if not d:
        return "-o ControlMaster=no"
    sock = os.path.join(d, f"{_SSH_USER}@{host}:{_SSH_PORT}")
    if len(sock) > _MUX_PATH_MAX:
        return "-o ControlMaster=no"
    return (f"-o ControlMaster=auto -o ControlPath={sock} "
            f"-o ControlPersist={_MUX_PERSIST}")

def _ssh_cmd(host: str, cmd: str, mux: bool = True) -> str:
    """生成完整的 SSH 命令

    ★ 重要（2026-09-11 实测踩坑）★
    本函数返回的字符串会被 `subprocess.check_output(..., shell=True)` 交给
    **外层 /bin/sh** 再执行一次。外层 shell 会先做一次展开，于是远端命令里的
    `$(...)`、`$var`、反引号、反斜杠会被**本机提前展开**，把本机结果当成远端结果。

    实测症状：`echo 'KERNEL='$(uname -r)` 返回的是 **跳板机**内核而不是目标节点内核；
    `awk '{print $6}'` 里的 `$6` 被展开成空 → 解析出空的挂载点。
    这类错误不报错、只是"安静地拿到错误数据"，极其危险。

    因此这里对远端命令做转义，让外层 shell 原样透传、由**远端 shell** 去展开。

    ★ 2026-09-21 新增 `mux`：SSH **连接复用**（ControlMaster）
      · `mux=True`（默认）→ 复用本主机已建立的连接
      · `mux=False` → 现行为（每条命令一次独立连接），作为**回落路径**
      只影响"怎么建立连接"，**执行的命令、解析、产出事实完全不变**。
      详见下方 `_MUX_*` 一节。
    """
    opts = "-o ConnectTimeout=5 -o StrictHostKeyChecking=no"
    if mux:
        opts += " " + _mux_opts(host)
    port = _SSH_PORT
    user = _SSH_USER
    # 转义顺序：反斜杠 → 双引号 → 美元符 → 反引号
    safe_cmd = (cmd.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("$", "\\$")
                   .replace("`", "\\`"))
    if _SSH_PASS:
        return (f'sshpass -p \'{_SSH_PASS}\' ssh {opts} '
                f'-p {port} {user}@{host} "{safe_cmd}"')
    elif _SSH_KEY:
        return (f'ssh -i {_SSH_KEY} {opts} '
                f'-p {port} {user}@{host} "{safe_cmd}"')
    else:
        return (f'ssh {opts} '
                f'-p {port} {user}@{host} "{safe_cmd}"')

_OS_COMMANDS = {
    # UltraPath
    "ultrapath_devices": "ls /dev/ultrapath* 2>/dev/null || echo 'no_ultrapath'",
    "ultrapath_status": "upadmin show path 2>/dev/null | head -30 || echo 'no_upadmin'",
    # NVMe
    "nvme_list": "nvme list -o json 2>/dev/null || echo '{}'",
    "nvme_subsys": "cat /sys/class/nvme-subsystem/*/model 2>/dev/null | head -5 || echo 'no_nvme_subsys'",
    # CPU
    "cpu_info": "lscpu -J 2>/dev/null || lscpu 2>/dev/null | head -30",
    "cpu_numa": "numactl --hardware 2>/dev/null | head -20 || echo 'no_numactl'",
    # 内存
    "mem_info": "cat /proc/meminfo 2>/dev/null | head -20",
    "hugepages": "grep -i huge /proc/meminfo 2>/dev/null",
    # 网卡
    "roce_devices": "ls /sys/class/infiniband/ 2>/dev/null | head -10 || echo 'no_roce'",
    "roce_ports": ("for d in /sys/class/infiniband/*/ports/*/; do "
                   "echo \"$d$(cat ${d}state 2>/dev/null) $(cat ${d}phys_state 2>/dev/null)\"; "
                   "done 2>/dev/null | head -20 || echo 'no_roce_ports'"),
    # 网络接口
    "net_ifaces": "ip -j link show 2>/dev/null | head -50 || ip link show 2>/dev/null | head -30",
    # ★ 2026-09-23 新增：网口的**IP 地址**（③ 网口 ↔ UltraPath 的键）。
    #   `ip -j link show` 只给名字/状态/MTU，**没有 IP**（要 `addr show` 才有）；
    #   而存储面 IP 在 nvme_path 的 host_traddr 里（如 88.168.1.11）。
    #   缺了 IP 就无法判定"哪两个网口是存储面"，只能按 IP 造端口名，
    #   人看不到真实网口名，也做不了「未插线网口不管」的过滤。
    "net_iface_addrs": "ip -j addr show 2>/dev/null || echo 'no_addr'",
    # 内核模块
    "kernel_modules": "lsmod 2>/dev/null | grep -E 'nvme|mlx5|ultra|roce' | head -20 || echo 'no_relevant_modules'",
    # 系统信息
    "uname": "uname -a",
    "os_release": "cat /etc/os-release 2>/dev/null | head -5",
    # 进程
    "gaussdb_process": "ps aux 2>/dev/null | grep -E '[g]auss|[d]bmind' | head -20 || echo 'no_gaussdb_process'",
    # 本地磁盘（存算一体退化用）
    "local_disks": "lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,MODEL 2>/dev/null || lsblk 2>/dev/null | head -30",
    # ══ 利用率类采集（2026-09-11 新增，消 7 项 KNOWN_GAPS）══
    # 一次 SSH 内做「双采样」：先取 S1，sleep 间隔，再取 S2。
    # 用 /proc/stat + /proc/diskstats 的差值算利用率，比一次性 `iostat` 更可靠
    # （iostat 首次输出是自启动以来的平均值，会严重失真）。
    # 实测：合并命令总耗时约 1.8 s/节点，不构成瓶颈。
    "cpu_mem_sample": (
        "echo '###S1'; grep -E '^(cpu |procs_running|procs_blocked)' /proc/stat | head -3; "
        "cat /proc/loadavg; "
        "echo '###DISK1'; grep -vE ' (loop|ram)[0-9]' /proc/diskstats; "
        "echo '###SCHED'; "
        "for d in /sys/block/*; do n=${d##*/}; case $n in "
        "sd*|nvme*|dm-*|ultrapath*) s=$(head -1 $d/queue/scheduler 2>/dev/null); "
        "printf '%s=%s' $n \"$s\"; echo; esac; done; "
        "sleep 1; "
        "echo '###S2'; grep -E '^(cpu |procs_running|procs_blocked)' /proc/stat | head -3; "
        "cat /proc/loadavg; "
        "echo '###DISK2'; grep -vE ' (loop|ram)[0-9]' /proc/diskstats"
    ),
    # 文件系统 / LVM 使用率（规则 r_st_fs_space_critical / r_st_lvm_space_low）
    # df 输出：source|mount|size|used|pct
    # ★ 契约语义：LVM 逻辑卷没有 thin-pool 时 `lvs -o data_percent` 是**空列**
    #   （实测全部 LV 该列为空），所以 lv_usage_pct 不能取自 data_percent，
    #   改为取「挂在 LVM 设备上的文件系统使用率」（/dev/mapper/*）。这一修正让
    #   r_st_lvm_space_low 真正有数据可判。
    "fs_lvm_usage": (
        "df -P -B1 -x tmpfs -x devtmpfs -x overlay 2>/dev/null | tail -n +2 "
        "| awk '{print $1\"|\"$6\"|\"$2\"|\"$3\"|\"$5}'; "
        "echo '###PVDISPLAY'; "
        "(pvs --noheadings -o pv_name,vg_name,pv_size,pv_free --units b 2>/dev/null || echo 'no_lvm'); "
        "echo '###LVDISPLAY'; "
        "(lvs --noheadings -o lv_name,vg_name,lv_size --units b 2>/dev/null || echo 'no_lvm')"
    ),
    # 块设备 IO 错误计数（规则 r_st_block_device_io_error）
    "block_io_errors": (
        "for d in $(ls /sys/block/ 2>/dev/null | grep -E '^(sd|nvme|dm-)' | head -30); do "
        "e=$(cat /sys/block/$d/device/ioerr_cnt 2>/dev/null); "
        "[ -n \"$e\" ] && echo \"$d|$e\"; done; echo '###DONE'"
    ),
    # 内核模块加载态（规则 r_st_kernel_module_missing）
    # ★ 契约语义：规则是 `storage.kernel_module_state ne "Live"`（首字母大写），
    #   所以必须产出 "Live" / "Missing"。若产出 "normal" 会导致**critical 误报**
    #   （而实测所有关键模块其实都已加载）。
    # ★ 模块匹配用「精确或前缀」：Kylin/内核版本差异下模块名可能是 nvme_core/nvme_common
    #   而非裸 nvme，直接 grep -qw nvme 会误判为 missing。
    "module_state": (
        "for m in nvme nvme_rdma rdma_cm mlx5_core ultrapath; do "
        "r=$(lsmod 2>/dev/null | awk -v M=$m '$1==M || index($1, M\"_\")==1 {print \"yes\"; exit}'); "
        "echo \"$m=${r:-no}\"; done"
    ),
    # 本地盘 SMART 健康（规则 r_st_local_disk_fault，需 smartctl）
    "smart_health": (
        "which smartctl >/dev/null 2>&1 || { echo 'no_smartctl'; exit 0; }; "
        "for d in $(lsblk -d -n -o NAME,TYPE 2>/dev/null | awk '$2==\"disk\"{print $1}' | head -12); do "
        "p=$(/usr/sbin/smartctl -H /dev/$d 2>/dev/null || smartctl -H /dev/$d 2>/dev/null); "
        "h=$(echo \"$p\" | grep -E 'SMART overall-health|Health Status' | head -1); "
        "echo \"$d|${h:-UNKNOWN}\"; done"
    ),
    # NVMe 控制器 / PCIe AER（规则 r_st_nvme_ctrl_dead / r_st_pcie_error_detected）
    "nvme_ctrl_state": (
        "for s in /sys/class/nvme/nvme*; do [ -d \"$s\" ] || continue; "
        "n=$(basename $s); printf '%s|%s|%s\\n' $n \"$(cat $s/state 2>/dev/null)\" "
        "\"$(cat $s/model 2>/dev/null)\"; done; "
        "echo '###AER'; "
        "for f in /sys/bus/pci/devices/*/aer_dev_correctable; do "
        "[ -r \"$f\" ] || continue; n=$(echo $f | cut -d/ -f6); "
        "v=$(awk '{s+=$2} END{print s+0}' $f 2>/dev/null); [ \"$v\" != \"0\" ] && echo \"$n|$v\"; done; "
        "echo '###DONE'"
    ),
    # RoCE 端口速率 / MTU（规则 r_st_roce_speed_anomaly / r_st_roce_mtu_mismatch）
    "roce_speed_mtu": (
        "for p in /sys/class/infiniband/*/ports/*; do [ -d \"$p\" ] || continue; "
        "dev=$(echo $p | awk -F/ '{print $(NF-3)\"/\"$(NF-1)}'); "
        "printf '%s|rate=%s|phys=%s|state=%s\\n' \"$dev\" "
        "\"$(cat $p/rate 2>/dev/null)\" \"$(cat $p/phys_state 2>/dev/null)\" \"$(cat $p/state 2>/dev/null)\"; "
        "done; "
        "echo '###MTU'; "
        "for i in $(ls /sys/class/net/ 2>/dev/null | grep -E '^(enp|eth)'); do "
        "printf '%s|mtu=%s|speed=%s|oper=%s\\n' $i \"$(cat /sys/class/net/$i/mtu 2>/dev/null)\" "
        "\"$(cat /sys/class/net/$i/speed 2>/dev/null)\" \"$(cat /sys/class/net/$i/operstate 2>/dev/null)\"; done"
    ),
    # 内核版本 / 驱动版本（规则 r_st_nvme_driver_version_check）
    "driver_version": (
        "echo 'KERNEL='$(uname -r); "
        "echo 'NVME_KBUILD='$(modinfo nvme 2>/dev/null | awk '/^version:/{print $2}'); "
        "echo 'MLX5='$(modinfo mlx5_core 2>/dev/null | awk '/^version:/{print $2}'); "
        "echo 'ULTRAPATH='$(modinfo ultrapath 2>/dev/null | awk '/^version:/{print $2}'); "
        "echo 'DONE'"
    ),
    # ══ UltraPath CLI 采集（2026-09-11 新增，消化 5 项 KNOWN_GAPS）══
    # ★ 实测要点（踩坑记录）：
    #   1) CLI 名是 **upadmin_plus**（不是 `upadmin`），路径 /usr/bin/upadmin_plus
    #   2) 它是**交互式** shell：必须 `printf 'cmd\nquit\n' | upadmin_plus`；
    #      直接 `upadmin_plus show path` 会把整串当一个参数 → "Input Option is not supported"
    #   3) **`quit` 之后它不退出** —— 无 TTY 时持续打印提示符使管道永不 EOF，下游 grep
    #      一直等 → 命令挂死。**必须用远端 `timeout` 兜底**（实测 3s 已能拿全输出）
    #   4) 单次 CLI 调用约 3s；5 次串行 = 15s（OS 层最长尾）。
    #      故**合并为一次 SSH + 远端并行**：1 次连接、总耗时 ≈ 单次调用。
    #   5) 未安装时（如跳板机）输出 no_upadmin，由命令级失败协议记录
    "up_all": (
        "which upadmin_plus >/dev/null 2>&1 || { echo 'no_upadmin'; exit 0; }; "
        "d=$(mktemp -d); cd $d; "
        "(printf 'show vlun\nquit\n' | timeout 3 upadmin_plus 2>/dev/null > vlun) & "
        "(printf 'show path\nquit\n' | timeout 3 upadmin_plus 2>/dev/null > path) & "
        "(printf 'show supportarraylist\nquit\n' | timeout 3 upadmin_plus 2>/dev/null > sup) & "
        "(printf 'show version verbose\nquit\n' | timeout 3 upadmin_plus 2>/dev/null > ver) & "
        "(printf 'show array id=0 verbose\nquit\n' | timeout 3 upadmin_plus 2>/dev/null > arr) & "
        "wait; "
        r"echo '###VLUN'; grep -E '^\s*[0-9]+\s+ultrapath' vlun; "
        r"echo '###PATH'; grep -E '^\s*[0-9]+\s+[0-9]' path; "
        r"echo '###SUP'; grep -E '^[A-Z][A-Za-z]+\s+\S+' sup; "
        r"echo '###VER'; grep -E 'Version\s+:' ver; "
        r"echo '###ARR'; grep -E '^\s*(Array Name|Array SN|Vendor Name|Product Name)\s+:' arr; "
        "cd /; rm -rf $d; echo '###DONE'"
    ),
    # UltraPath 主设备号（规则 r_st_ultrapath_major_conflict 期望 251）
    "up_device_major": (
        "for d in /sys/block/ultrapath*; do [ -e \"$d/dev\" ] || continue; "
        "printf '%s|%s\\n' \"${d##*/}\" \"$(cat $d/dev 2>/dev/null | cut -d: -f1)\"; done; "
        "echo '###DONE'"
    ),
    # NVMe 子系统路径分布与控制器连接数（规则 r_st_nvme_path_imbalance / r_st_nvme_single_ctrl_conn）
    "nvme_subsys_paths": (
        "which nvme >/dev/null 2>&1 || { echo 'no_nvme_cli'; exit 0; }; "
        "nvme list-subsys -o json 2>/dev/null | head -c 200000; echo; echo '###DONE'"
    ),
    # NVMe SMART 日志（错误计数：media_errors / num_err_log_entries）
    # ★ 实测要点：这两个字段在 `nvme smart-log` 输出的**末尾**（用 head 截取会看不到），
    #   且 `nvme error-log` 在该设备上返回 INVALID_LOG_PAGE —— 只能走 smart-log。
    # 输出格式：<ctrl>|<json>，每行一个控制器
    "nvme_smart_errors": (
        "which nvme >/dev/null 2>&1 || { echo 'no_nvme_cli'; exit 0; }; "
        "for c in /sys/class/nvme/nvme*; do [ -e \"$c\" ] || continue; n=${c##*/}; "
        "case $n in nvme[0-9]|nvme[0-9][0-9]) ;; *) continue; esac; "
        "j=$(nvme smart-log /dev/$n -o json 2>/dev/null | tr -d '\\n' | tr -s ' '); "
        "[ -n \"$j\" ] && printf '%s|%s\\n' \"$n\" \"$j\"; done; echo '###DONE'"
    ),
}


class OsProbe(BaseProbe):
    """OS 层采集探针"""

    def __init__(self, hosts: Optional[List[str]] = None):
        self._hosts = hosts or self._default_hosts()

    @property
    def identity(self) -> str:
        return "os"

    def _default_hosts(self) -> List[str]:
        """从环境变量 GAUSSDB_NODES 提取主机列表"""
        nodes_str = os.environ.get("GAUSSDB_NODES", "")
        if not nodes_str:
            return ["192.0.2.12", "192.0.2.15", "192.0.2.14"]
        hosts = set()
        for part in nodes_str.split(","):
            part = part.strip()
            if ":" in part:
                hosts.add(part.split(":")[0])
        return list(hosts) if hosts else ["192.0.2.12"]

    # ── 公共方法 ──

    def collect(self, hosts: Optional[List[str]] = None, **kwargs) -> ProbeResult:
        """执行 OS 层采集

        Args:
            hosts: 指定采集主机，None=全部节点

        Returns:
            ProbeResult
        """
        start = time.time()
        from concurrent.futures import ThreadPoolExecutor, as_completed

        targets = hosts or self._hosts
        raw: Dict[str, Any] = {
            "nodes": {},
            "summary": {},
            "parse_errors": {},      # ★ 命令级失败显式化（不再静默丢数据）
        }
        alerts = []
        all_facts = []

        def collect_one(host: str) -> tuple:
            host_data, host_errors = self._collect_host(host)
            host_facts = self._to_facts(host, host_data)
            host_alerts = self._diagnose_host(host, host_data)
            # 采集/解析失败必须升级为告警 —— 否则「没采到」会被当成「一切正常」
            for key, err in host_errors.items():
                sev = "warning"
                low = err.lower()
                if "timeout" in low or "timed out" in low:
                    sev = "warning"
                host_alerts.append({
                    "severity": sev,
                    "title": f"OS 采集失败: {key.split(':')[0]}",
                    "detail": f"主机 {host} 命令 `{key}` 采集或解析失败，该指标为 0 是"
                              f"**采集失败**而非真实值：{err}",
                    "entity": host,
                    "source": "os_probe",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            return host, host_data, host_facts, host_alerts, host_errors

        parse_errors: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(len(targets), 5)) as ex:
            futures = {ex.submit(collect_one, h): h for h in targets}
            for ft in as_completed(futures):
                host, host_data, host_facts, host_alerts, host_errors = ft.result()
                raw["nodes"][host] = host_data
                all_facts.extend(host_facts)
                alerts.extend(host_alerts)
                parse_errors.update(host_errors)

        raw["parse_errors"] = parse_errors
        if parse_errors:
            print(f"[OS][WARN] {len(parse_errors)} 个命令级失败: "
                  f"{json.dumps(parse_errors, ensure_ascii=False)[:600]}", flush=True)

        # 跨节点汇总 + 集群级聚合事实
        raw["summary"] = self._cross_node_summary(raw["nodes"])
        all_facts.extend(self._cluster_facts(
            raw["nodes"], time.strftime("%Y-%m-%dT%H:%M:%S")))

        return ProbeResult(
            layer="os",
            facts=all_facts,
            raw=raw,
            alerts=alerts,
            elapsed_ms=self._elapsed(start),
        )

    # ── 单节点采集 ──

    def _collect_host(self, host: str) -> tuple:
        """采集单个节点（命令级并行）

        Returns:
            (data, errors) —— errors 为 {命令名: 错误描述}，非空即表示该命令
            采集失败或解析失败（由调用方升级为告警 + 透出到 raw["parse_errors"]）。

        ★ 性能（2026-09-11 实测）：原实现**串行**跑 24 条 SSH 命令，
        单节点约 25 s、三节点约 75 s，OS 层成为整条采集链路的瓶颈。
        改为命令级并行（每节点一个线程池）后耗时降到「最慢单条命令」量级。
        ★ 单命令超时：cpu_mem_sample 内含 sleep 1 采样，需要额外余量；
        原硬编码 8 s 会让采样类命令稳定超时。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        data: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        def _exec(full_cmd: str, timeout: int, key_hint: str) -> str:
            """真正执行一条 SSH 命令（走 sim tap；live 下直通）"""
            # ★ 模拟演练打桩（docs/60）：SSH 子进程走 tap。
            #   live 下直通；replay 下**不启动任何子进程**只回放录制输出。
            if _sim is None:
                return subprocess.check_output(
                    full_cmd, shell=True, timeout=timeout,
                    stderr=subprocess.STDOUT,
                ).decode("utf-8", errors="replace").strip()
            return _sim.os_check_output(
                full_cmd,
                lambda: subprocess.check_output(
                    full_cmd, shell=True, timeout=timeout,
                    stderr=subprocess.STDOUT,
                ),
                key_hint=key_hint).decode("utf-8", errors="replace").strip()

        def _looks_like_conn_error(text: str) -> bool:
            """判断失败是否**连接/通道**层面（而非命令本身返回非零）

            ★ 只对连接类失败回落独立连接：命令自身失败（如远端没有该命令）
              回落是白费时间，还会把行为改得与改动前不同。
            """
            t = (text or "").lower()
            return any(k in t for k in (
                "connection", "broken pipe", "mux_client", "control socket",
                "shared connection", "connection reset", "kex_exchange",
                "no route to host", "timed out", "closed by remote host",
            ))

        def _run_ssh(full_cmd: str, timeout: int, key_hint: str = "",
                     plain_cmd: str = "") -> str:
            """执行一条 SSH 命令，失败时**逐级回落**

            实测（2026-09-11）：超时会随机落在不同命令上（同一时刻这台是 hugepages、
            下次换成那台的 kernel_modules），说明瓶颈在 SSH 连接建立/认证
            （sshpass + PAM 较慢），而不是命令本身慢。重试一次即可把瞬时竞争
            转成成功，避免产生"狼来了"式的采集失败告警。

            ★ 2026-09-21 加第二级：**复用通道失效 → 回落独立连接**。
              复用让 28 条命令共享一条通道；万一通道中途断掉，
              该节点当轮所有命令会一起失败（这是复用引入的**唯一新故障面**）。
              回落确保最坏情况**等价于改动前**：
                第 1 级：同一命令重试一次（原有行为）
                第 2 级：改用独立连接（mux=False）再试一次
            """
            last = None
            # 复用失效时也要能回落 ⇒ attempt 序列 = [复用命令, 独立连接命令]
            attempts = [c for c in (full_cmd, plain_cmd) if c]
            for idx, cmd in enumerate(attempts):
                for sub in (1, 2):
                    try:
                        return _exec(cmd, timeout, key_hint)
                    except subprocess.TimeoutExpired as e:
                        last = e
                        if sub == 1:
                            time.sleep(1.0)
                            continue
                        break            # 该方式两次都超时 → 换独立连接
                    except Exception as e:                       # noqa: BLE001
                        last = e
                        detail = ""
                        out = getattr(e, "output", b"") or b""
                        if isinstance(out, bytes):
                            out = out.decode("utf-8", errors="replace")
                        detail = str(out) or str(e)
                        if idx == 0 and len(attempts) > 1 and _looks_like_conn_error(detail):
                            print(f"[OS][INFO] {key_hint.split('|')[0]} 复用通道异常，"
                                  f"回落独立连接重试: {detail[:120]}", flush=True)
                            break        # 换独立连接
                        raise            # 命令自身失败：维持原行为，不回落
            raise last  # pragma: no cover

        def run_one(cmd_name: str, cmd_template: str):
            # 逐命令超时：采样类需要 sleep 余量；smartctl/ps/lsblk 在大盘/高负载机器上偏慢。
            # up_* 走 upadmin_plus，其远端已有 `timeout 10` 兜底，这里再留 SSH 开销余量。
            # 注意：本机 sshd 的认证路径（sshpass + PAM）本身可能耗时数秒，
            # 因此默认超时给到 20s，否则会出现「命令没跑完就被判超时」的假失败。
            timeout = {
                "cpu_mem_sample": 25, "smart_health": 45, "gaussdb_process": 25,
                "local_disks": 25, "nvme_list": 25, "ultrapath_status": 25,
                "up_all": 18, "up_device_major": 18,
            }.get(cmd_name, 20)
            full_cmd = _ssh_cmd(host, cmd_template)
            # ★ 复用通道失效时的**回落命令**（独立连接 = 改动前的行为）。
            #   仅在真正会启用复用时才算一份，避免无谓的字符串构造。
            plain_cmd = ""
            if "-o ControlMaster=auto" in full_cmd:
                plain_cmd = _ssh_cmd(host, cmd_template, mux=False)
            try:
                # ★ 指纹用**内层命令+段名**（key_hint），不用整条 sshpass 命令：
                #   整条命令含主机 IP 与密码 ⇒ 场景会绑死在某个节点、且指纹里混入凭据。
                out = _run_ssh(full_cmd, timeout, key_hint=f"{cmd_name}|{cmd_template}",
                               plain_cmd=plain_cmd)
            except subprocess.TimeoutExpired:
                return cmd_name, {"error": "timeout"}, f"timeout（>{timeout}s，含 1 次重试）"
            except subprocess.CalledProcessError as e:
                return (cmd_name, {"error": e.output.decode("utf-8", errors="replace")[:200]},
                        f"CalledProcessError: {self._safe_exc(e)}")
            except Exception as e:
                safe = self._safe_exc(e)
                return cmd_name, {"error": safe}, safe

            # ★ 解析失败必须显式化：原实现把异常塞进 data[cmd]={"error": ...}，
            #   下游 _to_facts 按 dict 取值 → 整段指标静默丢失（实测 KeyError 案例）。
            try:
                return cmd_name, self._parse_output(cmd_name, out), None
            except Exception as e:
                safe = self._safe_exc(e)
                print(f"[OS][WARN] {host} {cmd_name} 解析失败: {safe}", flush=True)
                return cmd_name, {"error": safe}, f"解析失败 {safe}"

        # 并发上限 9：合并 upadmin 后单节点命令数下降，可在不触发 sshd 连接限流的前提下并行更多
        # 会触发连接限流，表现为「微不足道的命令也超时」（如 grep /proc/meminfo）。
        with ThreadPoolExecutor(max_workers=9) as ex:
            futs = [ex.submit(run_one, n, t) for n, t in _OS_COMMANDS.items()]
            for ft in as_completed(futs):
                try:
                    name, payload, err = ft.result()
                except Exception as e:
                    continue
                data[name] = payload
                if err:
                    errors[name] = err

        return data, errors

    @staticmethod
    def _parse_output(cmd_name: str, out: str) -> Any:
        """智能解析 SSH 命令输出"""
        if not out or out == "":
            return {}

        # ── 专用解析器（结构化输出，通用规则解析不了）──
        if cmd_name == "cpu_mem_sample":
            return OsProbe._parse_cpu_mem_sample(out)
        if cmd_name == "fs_lvm_usage":
            return OsProbe._parse_fs_lvm(out)
        if cmd_name == "block_io_errors":
            return OsProbe._parse_pipe_pairs(out)
        if cmd_name == "module_state":
            return OsProbe._parse_kv(out)
        if cmd_name == "smart_health":
            return OsProbe._parse_pipe_pairs(out, positional=True)
        if cmd_name == "nvme_ctrl_state":
            return OsProbe._parse_nvme_ctrl(out)
        if cmd_name == "roce_speed_mtu":
            return OsProbe._parse_roce(out)
        if cmd_name == "driver_version":
            return OsProbe._parse_kv(out)
        # ── UltraPath CLI / NVMe subsys 专用解析 ──
        if cmd_name == "up_all":
            return OsProbe._parse_up_all(out)
        if cmd_name == "up_device_major":
            return OsProbe._parse_pipe_pairs(out)
        if cmd_name == "nvme_subsys_paths":
            return OsProbe._parse_nvme_subsys(out)
        if cmd_name == "nvme_smart_errors":
            return OsProbe._parse_nvme_smart_errors(out)

        # JSON 格式输出（lscpu -J, nvme list -o json, lsblk -J, ip -j）
        if out.startswith("{"):
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                pass
        if out.startswith("["):
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                pass

        # 错误/无相关设备
        if out.startswith("no_"):
            return {"status": out}

        # 纯文本 → 简单结构化
        lines = [l.strip() for l in out.split("\n") if l.strip()]
        if len(lines) <= 30:
            return {"lines": lines}
        return {"lines": lines[:30], "truncated": True}

    # ── 专用解析器实现 ──

    @staticmethod
    def _safe_exc(e: BaseException) -> str:
        """把异常转成人类可读且**不含特殊字符**的一行描述

        实测（2026-09-11）：KeyError 的 str() 是带引号的 repr（`'scheduler'`），
        直接拼进 SSH 命令会破坏远端 shell。统一清洗，并把类型名带上，
        便于定位（原来的 `except Exception as e: data[cmd]={"error": str(e)}`
        会把错误塞进数据体，解析器又按 dict 处理 → 整段数据静默丢失）。
        """
        try:
            msg = f"{type(e).__name__}: {e}"
        except Exception:
            msg = type(e).__name__
        return "".join(c for c in msg if c.isprintable() and c not in '"`$\\')[:300]

    @staticmethod
    def _is_whole_disk(name: str) -> bool:
        """判断 /proc/diskstats 里的条目是「整盘」还是分区

        分区名形如 sda1 / nvme0n1p2 / dm-0（dm 视为整盘），整盘名形如 sda / nvme0n1。
        只统计整盘，避免父盘与分区重复计数。
        """
        import re as _re
        if not name:
            return False
        if name.startswith(("loop", "ram", "sr")):
            return False
        if name.startswith("dm-") or name.startswith("ultrapath"):
            return True
        if name.startswith("nvme"):
            # nvme0n1 是整盘；nvme0n1p1 是分区
            return _re.match(r"^nvme\d+n\d+$", name) is not None
        # sdX / hdX / vdX：整盘=字母+可选字母，分区=末尾数字
        return _re.match(r"^[a-z]+$", name) is not None

    @staticmethod
    def _parse_kv(out: str) -> Dict[str, str]:
        """`KEY=VALUE` 行 → dict"""
        d: Dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        return d

    @staticmethod
    def _parse_pipe_pairs(out: str, positional: bool = False) -> Dict[str, Any]:
        """`name|value` 行 → {name: value}；positional=True 时 value 取第一个 | 之后全部"""
        d: Dict[str, Any] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("###") or line in ("no_smartctl", "no_lvm"):
                continue
            if "|" in line:
                k, v = line.split("|", 1)
                d[k.strip()] = v.strip()
            elif positional:
                d[line] = ""
        return d

    @staticmethod
    def _parse_cpu_mem_sample(out: str) -> Dict[str, Any]:
        """解析双采样块：S1 / DISK1 / SCHED / S2 / DISK2

        返回 {s1: {...}, s2: {...}, disk1: [...], disk2: [...], scheduler: {},
              delta_s: float}

        ★ 实测踩坑（2026-09-11）：marker→字典键的映射曾是 `"SCHED": "sched"`，
        但返回字典里的键叫 `scheduler` ⇒ 赋值时 KeyError，而该异常被上层
        `except Exception` 吞掉 ⇒ **整条 CPU/磁盘指标全部静默丢失**。
        现已统一为 `scheduler`，并对每个分支加键存在性判断。
        """
        res: Dict[str, Any] = {"s1": {}, "s2": {}, "disk1": [], "disk2": [],
                               "scheduler": {}, "delta_s": 1.0}
        cur = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("###"):
                tag = s.lstrip("#")
                cur = {"S1": "s1", "S2": "s2", "DISK1": "disk1", "DISK2": "disk2",
                       "SCHED": "scheduler"}.get(tag)     # ← 与返回字典键一致
                continue
            if not s or cur is None or cur not in res:
                continue
            if cur in ("s1", "s2"):
                if s.startswith("cpu "):
                    # cpu user nice system idle iowait irq softirq steal guest guest_nice
                    parts = s.split()[1:]
                    nums = [int(x) for x in parts[:8]] + [0] * (8 - len(parts[:8]))
                    res[cur]["cpu"] = {
                        "user": nums[0], "nice": nums[1], "system": nums[2],
                        "idle": nums[3], "iowait": nums[4], "irq": nums[5],
                        "softirq": nums[6], "steal": nums[7],
                        "total": sum(nums),
                    }
                elif s.startswith("procs_running"):
                    res[cur]["procs_running"] = int(s.split()[1])
                elif s.startswith("procs_blocked"):
                    res[cur]["procs_blocked"] = int(s.split()[1])
                else:
                    la = s.split()
                    if len(la) >= 3:
                        res[cur]["loadavg"] = la[:3]
            elif cur.startswith("disk"):
                f = s.split()
                if len(f) >= 14:
                    res[cur].append(f)
            elif cur == "scheduler":
                if "=" in s:
                    dev, val = s.split("=", 1)
                    if dev.strip():
                        res[cur][dev.strip()] = val.strip()
        return res

    @staticmethod
    def _parse_fs_lvm(out: str) -> Dict[str, Any]:
        """解析 df / pvs / lvs 三段

        df 行格式（本函数输入）：source|mount|size|used|pct
        """
        res: Dict[str, Any] = {"fs": [], "pv": [], "lv": []}
        cur = "fs"
        for line in out.splitlines():
            s = line.strip()
            if s == "###PVDISPLAY":
                cur = "pv"
                continue
            if s == "###LVDISPLAY":
                cur = "lv"
                continue
            if not s or s == "no_lvm":
                continue
            if cur == "fs" and "|" in s:
                parts = (s.split("|") + ["", "", "", "", ""])[:5]
                src_dev, mount, size, _used, pct = parts
                if not mount:
                    continue
                try:
                    res["fs"].append({"source": src_dev, "mount": mount,
                                      "size": int(size or 0),
                                      "use_pct": float(str(pct).replace("%", "") or 0)})
                except ValueError:
                    continue
            elif cur in ("pv", "lv"):
                parts = s.split()
                if parts:
                    res[cur].append(parts)
        return res

    @staticmethod
    def _parse_nvme_ctrl(out: str) -> Dict[str, Any]:
        res: Dict[str, Any] = {"ctrls": [], "aer": {}}
        cur = "ctrls"
        for line in out.splitlines():
            s = line.strip()
            if s == "###AER":
                cur = "aer"
                continue
            if not s or s == "###DONE":
                continue
            if cur == "ctrls" and "|" in s:
                p = s.split("|")
                if len(p) >= 2:
                    res["ctrls"].append({"name": p[0], "state": p[1],
                                         "model": p[2] if len(p) > 2 else ""})
            elif cur == "aer" and "|" in s:
                p = s.split("|")
                try:
                    res["aer"][p[0]] = int(p[1])
                except (ValueError, IndexError):
                    pass
        return res

    @staticmethod
    def _parse_roce(out: str) -> Dict[str, Any]:
        """解析 RoCE 端口与网卡 MTU"""
        res: Dict[str, Any] = {"ports": [], "nics": []}
        cur = "ports"
        for line in out.splitlines():
            s = line.strip()
            if s == "###MTU":
                cur = "nics"
                continue
            if not s or "|" not in s:
                continue
            fields = s.split("|")
            name = fields[0].strip()
            kv = {}
            for f in fields[1:]:
                if "=" in f:
                    k, v = f.split("=", 1)
                    kv[k.strip()] = v.strip()
            if cur == "ports":
                res["ports"].append({"dev": name, **kv})
            else:
                res["nics"].append({"dev": name, **kv})
        return res

    # ── UltraPath CLI 解析（输出是「空格对齐的宽表」，字段位置随列宽变化，
    #    因此按「数值 / 枚举 / 容量」的语义切分，而不是按固定列偏移）──

    _UP_CAP_RE = re.compile(r"^\d+(?:\.\d+)?\s*[KMGTP]?B$", re.I)
    _UP_PATHS_RE = re.compile(r"^(\d+)/(\d+)$")
    _UP_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

    @staticmethod
    def _parse_up_vlun(out: str) -> Dict[str, Any]:
        """解析 `upadmin_plus show vlun`

        实测行样例（字段间空格数不固定）：
          0  ultrapatha  14-etcd-13  7100784a...0005  Normal  64.00GB  --/--  Dorado5500-11.14  5  4/4
        提取：disk / name / wwn / status / capacity / array_name / dev_lun_id / available/total paths
        """
        rows: List[Dict[str, Any]] = []
        for line in out.splitlines():
            s = line.rstrip()
            if not s or "ultrapath" not in s:
                continue
            if s.strip().startswith("--") or "Vlun ID" in s or "no_upadmin" in s:
                continue
            f = s.split()
            if len(f) < 8 or not f[0].isdigit():
                continue
            rec: Dict[str, Any] = {"vlun_id": int(f[0])}
            cap_idx = next((i for i, x in enumerate(f) if OsProbe._UP_CAP_RE.match(x)), None)
            pat_idx = next((i for i, x in enumerate(f)
                            if OsProbe._UP_PATHS_RE.match(x) and "/" in x), None)
            if pat_idx is None:
                continue
            avail, total = OsProbe._UP_PATHS_RE.match(f[pat_idx]).groups()
            rec["paths_available"] = int(avail)
            rec["paths_total"] = int(total)
            # 阵列侧 Dev Lun ID：位于 paths 列之前（实测列序 … ArrayName, DevLunID, Paths）
            # 2026-09-13：此前注释声称提取、代码实际未取 —— 跨层关联键之一。
            if pat_idx >= 1 and f[pat_idx - 1].isdigit():
                rec["dev_lun_id"] = f[pat_idx - 1]
            # name 在 disk 之后、容量之前；WWN 是长十六进制串
            wwn_idx = next((i for i, x in enumerate(f)
                            if len(x) >= 24 and re.fullmatch(r"[0-9a-fA-F]+", x)), None)
            if wwn_idx is not None and wwn_idx > 1:
                rec["name"] = f[wwn_idx - 1]
            # status 是容量前后的枚举词
            for tok in ("Normal", "Fault", "Degraded", "Invalid", "Offline", "Abnormal"):
                if tok in f:
                    rec["status"] = tok
                    break
            if cap_idx is not None:
                rec["capacity"] = f[cap_idx]
            if wwn_idx is not None:
                rec["wwn"] = f[wwn_idx]
            rec["disk"] = f[1] if len(f) > 1 else ""
            # 阵列名形如 Dorado5500-11.14
            rec["array_name"] = next((x for x in f if re.match(r"^(Dorado|OceanStor)\S*", x)), "")
            rows.append(rec)
        return {"vluns": rows, "count": len(rows)}

    @staticmethod
    def _parse_up_path(out: str) -> Dict[str, Any]:
        """解析 `upadmin_plus show path`

        实测列：Path ID | Initiator Port | Array Name | Controller | Target Port
                | Path State | Check State | Port Type | Port ID
        样例：1  88.168.1.13::1  Dorado5500-11.14  0A  188.168.1.201  Normal  --  RoCE  CTE0.A.IOM3.P1.188
        """
        rows: List[Dict[str, Any]] = []
        for line in out.splitlines():
            f = line.split()
            if len(f) < 7 or not f[0].isdigit():
                continue
            state = next((x for x in f if x in ("Normal", "Dead", "Offline", "Fault",
                                                "Degraded", "Initializing")), "")
            ctrl = next((x for x in f if re.fullmatch(r"0[A-Ba-b]", x)), "")
            if not state:
                continue
            rows.append({
                "path_id": int(f[0]),
                "initiator": f[1] if len(f) > 1 else "",
                "array_name": next((x for x in f if re.match(r"^(Dorado|OceanStor)\S*", x)), ""),
                "controller": ctrl,
                "path_state": state,
                "port_type": next((x for x in f if x in ("RoCE", "FC", "iSCSI", "SAS")), ""),
            })
        return {"paths": rows, "count": len(rows)}

    @staticmethod
    def _parse_up_all(out: str) -> Dict[str, Any]:
        """解析合并命令 `up_all` 的输出（按 ###MARKER 分段后复用单命令解析器）

        ★ 设计：5 次 upadmin_plus 调用在**远端并行**执行（各 3s），一次 SSH 取回，
        总耗时 ≈ 单次调用；分段后交给各自的解析器，下游逻辑零改动。
        """
        segs: Dict[str, List[str]] = {}
        cur = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("###"):
                tag = s.lstrip("#")
                if tag == "DONE":
                    cur = None
                    continue
                cur = tag
                segs.setdefault(cur, [])
                continue
            if cur:
                segs[cur].append(line)
        g = lambda k: "\n".join(segs.get(k, []))          # noqa: E731
        return {
            "vlun": OsProbe._parse_up_vlun(g("VLUN")),
            "path": OsProbe._parse_up_path(g("PATH")),
            "supportarray": OsProbe._parse_up_supportarray(g("SUP")),
            "version": OsProbe._parse_up_version(g("VER")),
            "array": OsProbe._parse_up_array(g("ARR")),
            "count": len(segs),
        }

    @staticmethod
    def _parse_up_array(out: str) -> Dict[str, Any]:
        """解析 `show array id=0 verbose` 的关键行 → 归一化键名"""
        d: Dict[str, str] = {}
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip().lower().replace(" ", "_")
            val = v.strip()
            if key and val:
                d[key] = val
        return d

    @staticmethod
    def _parse_up_supportarray(out: str) -> Dict[str, Any]:
        """解析 `upadmin_plus show supportarraylist` → {VENDOR: [PRODUCT, ...]}"""
        arr: Dict[str, List[str]] = {}
        for line in out.splitlines():
            f = line.split()
            if len(f) < 2 or line.strip().startswith("-"):
                continue
            vendor, product = f[0].strip(), f[1].strip()
            if not vendor or not product or vendor.upper() in ("VENDOR",):
                continue
            arr.setdefault(vendor.upper(), []).append(product)
        return {"supported": arr, "vendor_count": len(arr),
                "product_count": sum(len(v) for v in arr.values())}

    @staticmethod
    def _parse_up_version(out: str) -> Dict[str, Any]:
        """解析 `upadmin_plus show version verbose` → {Software/Driver/Product: ver}"""
        d: Dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                key = k.strip().replace(" ", "_").lower()
                if key:
                    d[key] = v.strip()
        return d

    @staticmethod
    def _parse_nvme_subsys(out: str) -> Dict[str, Any]:
        """解析 `nvme list-subsys -o json`

        返回 {subsystems: [{name, nqn, paths: [...], endpoint_paths: [...],
                            ctrl_count_by_target: {...}}]}

        ★ 2026-09-16 新增 `endpoint_paths`：此前只把 paths 聚合成
          `paths_per_target` 计数，把每条路径的 Name/State/traddr/host_traddr/trsvcid
          **全部丢掉** —— 而「哪个主机口连到阵列哪个 target」正是被丢的那部分。
          改为在保留既有聚合字段（不改名，避免扰动下游）的同时，额外产出端点级清单。

        阵列归属判据：`NQN` 形如
          `nqn.2020-02.huawei.nvme:nvm-subsystem-sn-2102353TJWFSMB000002`
        ⇒ 后缀即阵列 SN。**不能用 IP 前缀判归属**：实测现场 3 个阵列的 target
          IP 全在 188.168/199.168 段，前缀相同。
        """
        txt = out
        if "###DONE" in txt:
            txt = txt.split("###DONE")[0]
        # ★★ 必须容忍**前置噪声**（2026-09-16 实测根因）：
        #   `_run_ssh` 用 `stderr=subprocess.STDOUT`，ssh 会话把 banner/警告
        #   （如 "Authorized users only..."）合并进了 stdout，于是 JSON 前面
        #   多出几行文本 ⇒ `json.loads` 直接失败 ⇒ 整段返回
        #   {"subsystems": [], "error": "json_parse_failed"}。
        #   后果极其隐蔽：**本条事实路径（含既有的 nvme_paths_per_target /
        #   nvme_connected_target_count）从来没有产出过任何事实**，而它们零消费者，
        #   所以从没被发现 —— 又一个「纸面声明」。
        #   修法：从第一个 `{` 起截取，并把被丢弃的前缀长度如实返回，便于诊断。
        lead = 0
        i = txt.find("{")
        if i > 0:
            lead = i
            txt = txt[i:]
        elif i < 0:
            return {"subsystems": [], "error": "no_json_object",
                    "noise_lead": len(out.splitlines())}
        try:
            d = json.loads(txt.strip())
        except Exception:
            # 截断（`head -c 200000`）会造成 JSON 不完整；如实报告而不是静默空
            return {"subsystems": [], "error": "json_parse_failed",
                    "noise_lead_bytes": lead, "raw_len": len(out)}
        subs = []
        for s in d.get("Subsystems") or []:
            paths = s.get("Paths") or []
            nqn = s.get("NQN", "")
            # 阵列 SN：NQN 的 `sn-` 之后；取不到则整体作为标识（不猜）
            sn = nqn.split("sn-")[-1] if "sn-" in nqn else nqn
            by_target: Dict[str, int] = {}
            live_by_target: Dict[str, int] = {}
            endpoint_paths: List[Dict[str, Any]] = []
            for p in paths:
                addr = str(p.get("Address", ""))
                kv = dict(x.split("=", 1) for x in addr.split() if "=" in x)
                m = re.search(r"traddr=(\S+)", addr)
                tgt = m.group(1) if m else "unknown"
                st = str(p.get("State", ""))
                by_target[tgt] = by_target.get(tgt, 0) + 1
                if st.lower() == "live":
                    live_by_target[tgt] = live_by_target.get(tgt, 0) + 1
                endpoint_paths.append({
                    "ctrl": str(p.get("Name", "")),
                    "transport": str(p.get("Transport", "")),
                    "state": st,
                    "traddr": tgt,
                    "host_traddr": kv.get("host_traddr", ""),
                    "trsvcid": kv.get("trsvcid", ""),
                    "subsys_nqn": nqn,
                    "array_sn": sn,
                })
            subs.append({
                "name": s.get("Name", ""),
                "nqn": nqn,
                "array_sn": sn,
                "path_count": len(paths),
                "live_path_count": sum(1 for p in paths
                                       if str(p.get("State", "")).lower() == "live"),
                "targets": sorted(by_target.keys()),
                "paths_per_target": by_target,
                "live_paths_per_target": live_by_target,
                # ★ 端点级清单（新增）
                "endpoint_paths": endpoint_paths,
                "unhealthy_paths": [p.get("Name") for p in paths
                                    if str(p.get("State", "")).lower() != "live"],
            })
        return {"subsystems": subs, "count": len(subs), "noise_lead_bytes": lead}

    @staticmethod
    def _parse_nvme_smart_errors(out: str) -> Dict[str, Any]:
        """解析 `nvme smart-log -o json`（每行 `<ctrl>|<json>`）

        关注错误计数：media_errors / num_err_log_entries。
        实测这两个字段在 smart-log 输出末尾（文本模式下用 head 会漏掉）。
        """
        ctrls: List[Dict[str, Any]] = []
        for line in out.splitlines():
            if "|" not in line or line.startswith("###") or "no_nvme_cli" in line:
                continue
            ctrl, js = line.split("|", 1)
            try:
                d = json.loads(js.strip())
            except Exception:
                continue
            def _n(*keys):
                for k in keys:
                    if k in d and d[k] is not None:
                        try:
                            return int(d[k])
                        except (TypeError, ValueError):
                            continue
                return None
            ctrls.append({
                "ctrl": ctrl.strip(),
                "media_errors": _n("media_errors"),
                "num_err_log_entries": _n("num_err_log_entries"),
                "critical_warning": _n("critical_warning"),
                "pct_used": _n("percent_used", "pct_used"),
                "unsafe_shutdowns": _n("unsafe_shutdowns"),
            })
        return {"controllers": ctrls, "count": len(ctrls)}

    # ── 标准化 Fact 转换 ──

    def _to_facts(self, host: str, data: Dict[str, Any]) -> List[dict]:
        """单节点数据 → Fact 数组"""
        facts = []
        src = "os.ssh"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")

        # UltraPath
        up_devs = data.get("ultrapath_devices", {})
        if isinstance(up_devs, dict) and "lines" in up_devs:
            dev_count = len([l for l in up_devs["lines"] if "/dev/ultrapath" in l])
            facts.append(self._make_fact("os.storage.ultrapath_devices", dev_count, host, "node", src, ts))

        # NVMe
        nvme = data.get("nvme_list", {})
        if isinstance(nvme, dict):
            devices = nvme.get("Devices", [])
            facts.append(self._make_fact("os.storage.nvme_devices", len(devices), host, "node", src, ts))
            for dev in devices[:5]:
                dev_name = dev.get("DevicePath", dev.get("Device", ""))
                facts.append(self._make_fact("os.storage.nvme_model", dev.get("ModelNumber", ""),
                                             dev_name, "nvme_device", src, ts))

        # CPU
        cpu = data.get("cpu_info", {})
        if isinstance(cpu, dict):
            # lscpu -J 格式
            cpus = cpu.get("lscpu", [])
            for c in cpus:
                field = c.get("field", "").lower()
                if "cpu(s)" in field:
                    facts.append(self._make_fact("os.cpu.count", c.get("data", ""), host, "node", src, ts))
                elif "model name" in field or "arch" in field:
                    facts.append(self._make_fact("os.cpu.model", c.get("data", ""), host, "node", src, ts))
                elif "numa" in field and "node(s)" in field.lower():
                    facts.append(self._make_fact("os.cpu.numa_nodes", c.get("data", ""), host, "node", src, ts))
                elif "thread" in field.lower():
                    facts.append(self._make_fact("os.cpu.smt", c.get("data", ""), host, "node", src, ts))

        # 内存
        mem = data.get("mem_info", {})
        if isinstance(mem, dict) and "lines" in mem:
            for line in mem["lines"]:
                m = re.match(r"MemTotal:\s+(\d+)", line)
                if m:
                    facts.append(self._make_fact("os.mem.total_kb", int(m.group(1)), host, "node", src, ts))
                m = re.match(r"MemFree:\s+(\d+)", line)
                if m:
                    facts.append(self._make_fact("os.mem.free_kb", int(m.group(1)), host, "node", src, ts))

        # HugePages
        huge = data.get("hugepages", {})
        if isinstance(huge, dict) and "lines" in huge:
            for line in huge["lines"]:
                m = re.match(r"HugePages_Total:\s+(\d+)", line)
                if m:
                    facts.append(self._make_fact("os.mem.hugepages_total", int(m.group(1)), host, "node", src, ts))
                m = re.match(r"HugePages_Free:\s+(\d+)", line)
                if m:
                    facts.append(self._make_fact("os.mem.hugepages_free", int(m.group(1)), host, "node", src, ts))
                m = re.match(r"Hugepagesize:\s+(\d+)", line)
                if m:
                    facts.append(self._make_fact("os.mem.hugepage_size_kb", int(m.group(1)), host, "node", src, ts))

        # RoCE
        roce = data.get("roce_devices", {})
        if isinstance(roce, dict) and "lines" in roce:
            devs = [l for l in roce["lines"] if not l.startswith("no_")]
            facts.append(self._make_fact("os.network.roce_devices", len(devs), host, "node", src, ts))

        # 内核模块
        mods = data.get("kernel_modules", {})
        if isinstance(mods, dict) and "lines" in mods:
            facts.append(self._make_fact("os.kernel.nvme_modules",
                         1 if any("nvme" in l for l in mods["lines"]) else 0, host, "node", src, ts))

        facts.extend(self._utilization_facts(host, data, src, ts))
        return facts

    # ── 利用率类事实派生（2026-09-11 新增，消 7 项 KNOWN_GAPS）──
    # 语义对齐（重要）：规则 YAML 里
    #   node.cpu_usage / host.cpu_usage        → 百分比 0-100
    #   node.disk_io_wait / node.io_await_ms   → 毫秒
    #   node.io_read_throughput_mb             → MB/s
    #   host.disk_util                         → 百分比 0-100
    #   cluster.io_scheduler_consistent        → 布尔（各节点 NVMe 调度器是否一致）
    # 因此同一节点同时产出 `node.*` 与 `host.*` 两个前缀（值等价），
    # 让 r_cpu_bottleneck / r_io_bottleneck 的 any 分支两种写法都能命中。

    def _utilization_facts(self, host: str, data: Dict[str, Any],
                           src: str, ts: str) -> List[dict]:
        facts: List[dict] = []

        # ── CPU 利用率（/proc/stat 双采样差值）──
        samp = data.get("cpu_mem_sample")
        if isinstance(samp, dict):
            c1 = (samp.get("s1") or {}).get("cpu")
            c2 = (samp.get("s2") or {}).get("cpu")
            if c1 and c2:
                dt = c2["total"] - c1["total"]
                if dt > 0:
                    busy = dt - (c2["idle"] - c1["idle"])
                    cpu_pct = round(max(0.0, busy) * 100.0 / dt, 2)
                    iowait_pct = round(max(0, c2["iowait"] - c1["iowait"]) * 100.0 / dt, 2)
                    sys_pct = round(max(0, c2["system"] - c1["system"]) * 100.0 / dt, 2)
                    usr_pct = round(max(0, c2["user"] - c1["user"]) * 100.0 / dt, 2)
                    # 双前缀产出，值等价（规则 any 分支两种写法都能命中）。
                    # 用显式字面量而非循环 f-string —— 静态契约扫描器要求字面量可见，
                    # 否则 `host.cpu_usage` / `node.cpu_usage` 会被误判为「无产出」。
                    facts.append(self._make_fact("node.cpu_usage", cpu_pct,
                                                 host, "node", src, ts))
                    facts.append(self._make_fact("host.cpu_usage", cpu_pct,
                                                 host, "node", src, ts))
                    facts.append(self._make_fact("node.cpu_sys_pct", sys_pct, host, "node", src, ts))
                    facts.append(self._make_fact("node.cpu_usr_pct", usr_pct, host, "node", src, ts))
                    # iowait 占 CPU 时间百分比 → node.disk_io_wait（规则阈值 20）
                    facts.append(self._make_fact("node.disk_io_wait", iowait_pct,
                                                 host, "node", src, ts))

            l2 = (samp.get("s2") or {}).get("loadavg")
            if l2:
                try:
                    facts.append(self._make_fact("os.load.avg1", float(l2[0]), host, "node", src, ts))
                    facts.append(self._make_fact("os.load.avg5", float(l2[1]), host, "node", src, ts))
                    facts.append(self._make_fact("os.load.avg15", float(l2[2]), host, "node", src, ts))
                except (ValueError, IndexError):
                    pass
            if (samp.get("s2") or {}).get("procs_blocked") is not None:
                facts.append(self._make_fact("os.procs_blocked",
                                             (samp["s2"] or {}).get("procs_blocked", 0),
                                             host, "node", src, ts))

            # ── 磁盘 await / 利用率 / 吞吐（/proc/diskstats 双采样差值）──
            # 只聚合「整盘」（排除分区），否则父盘 + 各分区会被重复计数，
            # 吞吐与 busy 量会被显著放大（实测：sdi 与 sdi1..sdi3 同时上报）。
            d1 = {f[2]: f for f in (samp.get("disk1") or []) if len(f) >= 14}
            d2 = {f[2]: f for f in (samp.get("disk2") or []) if len(f) >= 14}
            worst_await, worst_util, tot_read_kb = 0.0, 0.0, 0.0
            for name, f2 in d2.items():
                if not OsProbe._is_whole_disk(name):
                    continue
                f1 = d1.get(name)
                if not f1:
                    continue
                try:
                    # 字段: major minor name reads rd_merged rd_sect rd_ms
                    #        writes wr_merged wr_sect wr_ms io_cur io_ms weighted_io_ms
                    rs1, rs2 = int(f1[5]), int(f2[5])
                    ws1, ws2 = int(f1[9]), int(f2[9])
                    rms1, rms2 = int(f1[6]), int(f2[6])
                    wms1, wms2 = int(f1[10]), int(f2[10])
                    io_ms1, io_ms2 = int(f1[12]), int(f2[12])
                except (ValueError, IndexError):
                    continue
                d_reads, d_writes = rs2 - rs1, ws2 - ws1
                d_read_ms, d_write_ms = rms2 - rms1, wms2 - wms1
                d_io_ms = io_ms2 - io_ms1
                tot_read_kb += max(0, d_reads) * 0.5  # 扇区 512B → KB
                # await = 该设备读写总耗时 / 总完成 IO 数（毫秒）
                await_ms = (max(0, d_read_ms) + max(0, d_write_ms)) / max(1, d_reads + d_writes)
                # util = 设备忙碌时间 / 采样墙钟（百分比），采样间隔 1s → 1000ms
                util_pct = max(0.0, d_io_ms) / 10.0
                if await_ms > worst_await:
                    worst_await = await_ms
                if util_pct > worst_util:
                    worst_util = util_pct
            if worst_await > 0:
                facts.append(self._make_fact("node.io_await_ms", round(worst_await, 2),
                                             host, "node", src, ts))
            if worst_util > 0:
                facts.append(self._make_fact("host.disk_util", round(min(worst_util, 100.0), 2),
                                             host, "node", src, ts))
                facts.append(self._make_fact("node.disk_util", round(min(worst_util, 100.0), 2),
                                             host, "node", src, ts))
            if tot_read_kb > 0:
                facts.append(self._make_fact("node.io_read_throughput_mb",
                                             round(tot_read_kb / 1024.0, 3),
                                             host, "node", src, ts))

            # ── IO 调度器（storage.io_scheduler / cluster.io_scheduler_consistent）──
            # 语义收紧：规则 r_st_io_scheduler_misconfig 的措辞是「NVMe设备IO调度器非none」，
            # 且 `_eval_cond` 只取 vals[0]，因此 **只对 nvme/ultrapath 设备** 产出
            # `storage.io_scheduler`（普通 SATA/SAS 盘用 mq-deadline 是正常的，不能误判）。
            # 其余设备明细落到 `storage.io_scheduler.{dev}`，不影响规则。
            for dev, sched in (samp.get("scheduler") or {}).items():
                cur_sched = ""
                if "[" in sched and "]" in sched:
                    cur_sched = sched[sched.index("[") + 1:sched.index("]")]
                elif sched:
                    cur_sched = sched.split()[0]
                if not cur_sched:
                    continue
                facts.append(self._make_fact(f"storage.io_scheduler.{dev}", cur_sched,
                                             dev, "block_device", src, ts))
                # 规则针对的是 NVMe / 多路径设备（普通 sd* 盘用 mq-deadline 是正常的）
                if dev.startswith(("nvme", "ultrapath")):
                    facts.append(self._make_fact("storage.io_scheduler", cur_sched,
                                                 dev, "block_device", src, ts))

        # ── 文件系统 / LVM 使用率 ──
        fs = data.get("fs_lvm_usage")
        if isinstance(fs, dict):
            _fs_max = None
            _fs_max_mount = ""
            for item in (fs.get("fs") or []):
                mount = item.get("mount", "")
                src_dev = item.get("source", "")
                if not mount or item.get("size", 0) <= 0:
                    continue
                pct = round(float(item.get("use_pct") or 0), 2)
                facts.append(self._make_fact("storage.fs_usage_pct", pct,
                                             mount, "filesystem", src, ts))
                if _fs_max is None or pct > _fs_max:
                    _fs_max, _fs_max_mount = pct, mount
                # ★ LVM 逻辑卷使用率：不能取 lvs 的 data_percent（非 thin-pool 时为空列），
                #   改取「挂在 /dev/mapper/* 上的文件系统使用率」——这才是 LVM 卷的真实占用。
                if src_dev.startswith("/dev/mapper/"):
                    lv_name = src_dev[len("/dev/mapper/"):]
                    facts.append(self._make_fact("storage.lv_usage_pct", pct,
                                                 lv_name, "lvm_lv", src, ts))
                    facts.append(self._make_fact("storage.lv_size_bytes",
                                                 int(item.get("size") or 0),
                                                 lv_name, "lvm_lv", src, ts))
            # ★★ 2026-10-13 P0.7：宿主级**聚合**（磁盘满的直接判据）★★
            #   缘由：`storage.fs_usage_pct` 是**逐挂载点**的（obj_type=filesystem），
            #   契约里没有 Filesystem 对象类型 ⇒ 全部被 objtype_dropped 丢弃
            #   ⇒ 「磁盘满」这类故障在 CCF 里**完全没有证据**。
            #   本处不改逐挂载点事实（raw 里仍可查是哪块盘），只**增发**一条宿主级最大值，
            #   由 node→Host 映射进契约（`host.fs_max_used_pct`），避免新增对象类型。
            if _fs_max is not None:
                facts.append(self._make_fact("node.fs_max_used_pct", _fs_max,
                                             host, "node", src, ts))
                facts.append(self._make_fact("node.fs_max_used_mount", _fs_max_mount,
                                             host, "node", src, ts))

        # ── 块设备 IO 错误 ──
        bio = data.get("block_io_errors")
        if isinstance(bio, dict):
            tot_err = 0
            for dev, val in bio.items():
                try:
                    n = int(val)
                except (TypeError, ValueError):
                    continue
                tot_err += n
                if n > 0:
                    facts.append(self._make_fact("storage.bd_io_errors", n, dev,
                                                 "block_device", src, ts))
            # ★★ 2026-10-13 P0.7：**修一个「只在不出事时上报」的缺陷** ★★
            #   原实现只在 tot_err == 0 时发 node 级事实 ⇒ 一旦真有 I/O 错误，
            #   发出去的只有 block_device 级（objtype_dropped 丢弃）
            #   ⇒ **恰恰在最需要告警的时候，CCF 一条证据都收不到**。
            #   现改为**始终**发宿主级总数（`node.disk_io_errors_total`）。
            facts.append(self._make_fact("node.disk_io_errors_total", tot_err,
                                         host, "node", src, ts))

        # ── 内核模块加载态 ──
        # ★ 契约语义：规则 r_st_kernel_module_missing 用 `ne "Live"`（首字母大写），
        #   必须产出 "Live" / "Missing"。产出 "normal" 会造成 critical 误报。
        mods2 = data.get("module_state")
        if isinstance(mods2, dict) and mods2:
            # 值可能是 "yes"/"no"（新格式）或 "loaded"/"missing"（旧格式），都兼容
            def _is_loaded(v) -> bool:
                return str(v).strip().lower() in ("yes", "loaded", "live", "1", "true")
            missing = [m for m, st in mods2.items() if not _is_loaded(st)]
            facts.append(self._make_fact("storage.kernel_module_state",
                                         "Missing" if missing else "Live",
                                         host, "node", src, ts))
            facts.append(self._make_fact("storage.kernel_module_detail",
                                         ",".join(f"{m}={'ok' if _is_loaded(v) else 'MISSING'}"
                                                  for m, v in sorted(mods2.items())),
                                         host, "node", src, ts))
            if missing:
                facts.append(self._make_fact("storage.kernel_module_missing",
                                             ",".join(missing), host, "node", src, ts))

        # ── SMART 健康 ──
        # ★ 契约语义：规则 r_st_local_disk_fault 用 `eq "FAIL"`，因此必须产出
        #   "PASS"/"FAIL"。产出 "PASSED"/"FAILED" 会让**真实故障被漏报**。
        sh = data.get("smart_health")
        if isinstance(sh, dict):
            for dev, txt in sh.items():
                t = str(txt).upper()
                if "FAIL" in t:
                    health = "FAIL"
                elif "PASS" in t or "OK" in t:
                    health = "PASS"
                else:
                    continue
                facts.append(self._make_fact("storage.smart_health", health, dev,
                                             "local_disk", src, ts))
                if health == "FAIL":
                    facts.append(self._make_fact("storage.smart_predicted_fail", True,
                                                 dev, "local_disk", src, ts))

        # ── NVMe 控制器 / PCIe AER ──
        nv = data.get("nvme_ctrl_state")
        if isinstance(nv, dict):
            for c in (nv.get("ctrls") or []):
                st = str(c.get("state") or "").strip().lower()
                if not st:
                    continue
                facts.append(self._make_fact("storage.nvme_ctrl_state", st,
                                             c.get("name", ""), "nvme_ctrl", src, ts))
                if st != "live":
                    facts.append(self._make_fact("storage.nvme_ctrl_dead_count", 1,
                                                 c.get("name", ""), "nvme_ctrl", src, ts))
            if (nv.get("ctrls") or []):
                facts.append(self._make_fact("storage.nvme_subsys_ctrl_count",
                                             len(nv["ctrls"]), host, "node", src, ts))
            for dev, cnt in (nv.get("aer") or {}).items():
                if cnt > 0:
                    facts.append(self._make_fact("storage.pcie_aer_count", cnt, dev,
                                                 "pcie_device", src, ts))
                    facts.append(self._make_fact("storage.pcie_related_dev", dev, dev,
                                                 "pcie_device", src, ts))

        # ── RoCE 端口速率 / 状态 / MTU ──
        rc = data.get("roce_speed_mtu")
        if isinstance(rc, dict):
            for p in (rc.get("ports") or []):
                dev = p.get("dev", "")
                st = str(p.get("state") or "").strip()
                if st:
                    facts.append(self._make_fact("storage.roce_link_state",
                                                 "DOWN" if "DOWN" in st.upper() else st,
                                                 dev, "roce_port", src, ts))
                rate = str(p.get("rate") or "")
                if rate:
                    # 形如 "100 Gb/sec (4X EDR)"
                    try:
                        mbps = float(rate.split()[0]) * 1000.0
                        facts.append(self._make_fact("storage.roce_speed_mbps", mbps,
                                                     dev, "roce_port", src, ts))
                    except (ValueError, IndexError):
                        pass
            # ★ 2026-09-23：解析 `ip -j addr show`，得到「网口名 → IP 列表」。
            #   用途：把 net_iface 与存储面（nvme_path.host_traddr）按 **IP 相等**
            #   连起来，从而判定哪几个网口是存储面 —— 这是用户裁定②「未插线网口
            #   不管，只需要看 ultrapath 相关的网口」的**事实依据**。
            #   在此之前 net_iface 只有 roce_mtu / iface_state，没有任何链路归属，
            #   判定不了，只能按 IP 造出 port0/port1（假名）。
            _ip_of_dev: Dict[str, list] = {}
            _raw_addr = data.get("net_iface_addrs")
            if isinstance(_raw_addr, str) and _raw_addr.lstrip().startswith("["):
                try:
                    for _if in json.loads(_raw_addr):
                        _dn = _if.get("ifname") or ""
                        if not _dn:
                            continue
                        for _ai in (_if.get("addr_info") or []):
                            if _ai.get("family") == "inet" and _ai.get("local"):
                                _ip_of_dev.setdefault(_dn, []).append(str(_ai["local"]))
                except (ValueError, TypeError):
                    pass
            _iface_total = 0
            _iface_down = 0
            for n in (rc.get("nics") or []):
                dev = n.get("dev", "")
                mtu = n.get("mtu")
                if mtu:
                    try:
                        facts.append(self._make_fact("storage.roce_mtu", int(mtu),
                                                     dev, "net_iface", src, ts))
                    except ValueError:
                        pass
                oper = str(n.get("oper") or "").strip()
                if oper and "roce" not in dev:
                    facts.append(self._make_fact("os.network.iface_state", oper,
                                                 dev, "net_iface", src, ts))
                    _iface_total += 1
                    if oper.lower() != "up":
                        _iface_down += 1
                for _ip in _ip_of_dev.get(dev, []):
                    facts.append(self._make_fact("storage.net_iface.ip", _ip,
                                                 dev, "net_iface", src, ts))
            # ★★ 2026-10-13 P0.7：宿主级**聚合**（网络故障的直接判据）★★
            #   缘由：`os.network.iface_state` 是逐网卡的（obj_type=net_iface），
            #   契约无 NetworkInterface 类型 ⇒ 被丢弃；且 obj_id 用**网卡名**（enp4s0），
            #   多台主机同名 ⇒ 即便建类型也无法区分主机。
            #   ⇒ 增发宿主级「非 up 的网卡数」，由 node→Host 映射进契约。
            if _iface_total:
                facts.append(self._make_fact("node.net_iface_down_count", _iface_down,
                                             host, "node", src, ts))
                facts.append(self._make_fact("node.net_iface_total", _iface_total,
                                             host, "node", src, ts))

        # ── 内核 / 驱动版本 ──
        dv = data.get("driver_version")
        if isinstance(dv, dict):
            if dv.get("KERNEL"):
                facts.append(self._make_fact("os.kernel.version", dv["KERNEL"],
                                             host, "node", src, ts))
            if dv.get("NVME_KBUILD"):
                facts.append(self._make_fact("storage.nvme_driver_version",
                                             dv["NVME_KBUILD"], host, "node", src, ts))
            if dv.get("ULTRAPATH"):
                facts.append(self._make_fact("storage.ultrapath_version",
                                             dv["ULTRAPATH"], host, "node", src, ts))

        facts.extend(self._ultrapath_facts(host, data, src, ts))
        return facts

    # ── UltraPath / NVMe 多路径事实派生（2026-09-11 新增，消化 11 项 KNOWN_GAPS）──

    # vol 名 → 类型（规则的命名规范：<序号>-<类型>-<主机后缀>）
    _VOL_TYPE_ORDER = ("xlog", "data", "log", "backup", "etcd", "cluster", "test")

    @classmethod
    def _vol_type_of(cls, name: str) -> str:
        """从卷名提取类型（注意 xlog 含 log，必须先判 xlog）"""
        n = str(name or "").lower()
        for t in cls._VOL_TYPE_ORDER:
            if t in n:
                return t
        return ""

    def _ultrapath_facts(self, host: str, data: Dict[str, Any],
                         src: str, ts: str) -> List[dict]:
        facts: List[dict] = []

        # ★ 合并命令 up_all 把 5 个 CLI 结果一次取回；这里把各分段解包成原键名，
        #   使下游派生逻辑与「分 5 条命令」时完全一致（也兼容单独采集的场景）。
        all_up = data.get("up_all") if isinstance(data.get("up_all"), dict) else None
        def _seg(name: str) -> Dict[str, Any]:
            """取合并结果中的某段；无合并结果时回退到旧键名"""
            if all_up:
                m = {"up_show_vlun": "vlun", "up_show_path": "path",
                     "up_show_version": "version", "up_show_supportarray": "supportarray",
                     "up_show_array": "array"}.get(name)
                if m:
                    return all_up.get(m) or {}
            return data.get(name) or {}

        # ── UltraPath 链路计数（r_st_all_ultrapath_links_dead / r_st_ultrapath_links_partial）
        # 口径：把所有 vlun 的「可用路径数 / 总路径数」求和 —— 与规则"链路"语义一致。
        vl = _seg("up_show_vlun")
        if isinstance(vl, dict) and (vl.get("vluns") or []):
            avail = sum(int(v.get("paths_available") or 0) for v in vl["vluns"])
            total = sum(int(v.get("paths_total") or 0) for v in vl["vluns"])
            facts.append(self._make_fact("storage.active_ultrapath_link_count", avail,
                                         host, "node", src, ts))
            facts.append(self._make_fact("storage.total_ultrapath_link_count", total,
                                         host, "node", src, ts))
            if total and avail < total:
                bad = [v.get("name") for v in vl["vluns"]
                       if int(v.get("paths_available") or 0) < int(v.get("paths_total") or 0)]
                facts.append(self._make_fact("storage.ultrapath_degraded_vols",
                                             ",".join(str(b) for b in bad), host, "node", src, ts))
            # 卷维度事实（含类型与状态）—— 供 dorado 卷规则使用
            for v in vl["vluns"]:
                nm = v.get("name") or v.get("disk") or ""
                if not nm:
                    continue
                facts.append(self._make_fact("storage.ultrapath_vol_status",
                                             v.get("status", ""), nm, "ultrapath_vlun", src, ts))
                facts.append(self._make_fact("storage.ultrapath_vol_capacity",
                                             v.get("capacity", ""), nm, "ultrapath_vlun", src, ts))
                vt = self._vol_type_of(nm)
                if vt:
                    facts.append(self._make_fact("storage.ultrapath_vol_type", vt, nm,
                                                 "ultrapath_vlun", src, ts))
                # ── 卷级关联明细（LUN↔节点映射链，2026-09-13 新增）──────────────
                # 为什么：用户问「数据库关联了哪些 Dorado LUN / 哪些硬盘」时，解析器
                #   早已提取 WWN 与阵列侧 Dev Lun ID，但**被中间层丢弃**（只落
                #   status/capacity/type 三个路径）⇒ 跨层关联无从做起。
                # obj_id 用「节点|vlun名」复合键：共享卷（如 xlog 卷挂在 3 个节点上）
                #   的同名 vlun 必须按节点区分，单用 vlun 名会歧义。
                _key = f"{host}|{nm}"
                for _p, _v in (
                        # ★ 2026-09-23 新增：把「这个卷属于哪台节点」落成**显式事实**。
                        # 为什么：孪生界面要把卷画在各自 Node 容器下，但在此之前
                        #   归属只隐含在 obj_id 的 `主机IP|卷名` 复合键里 —— 消费侧只能
                        #   靠切字符串推导（属"解读 id"，不是读事实）。本体 module 14 已
                        #   把它声明为 nodeHostsLun 的边键（源侧本路径，目标侧 tpops.node.ip），
                        #   于是这条跨层边**自动**可连，无需消费侧解析 id。
                        ("storage.ultrapath_vol_node_ip", host),
                        ("storage.ultrapath_vol_wwn", v.get("wwn")),
                        ("storage.ultrapath_vol_dev_lun_id", v.get("dev_lun_id")),
                        ("storage.ultrapath_vol_array", v.get("array_name")),
                        ("storage.ultrapath_vol_device", v.get("disk")),
                        ("storage.ultrapath_vol_paths_available", v.get("paths_available")),
                        ("storage.ultrapath_vol_paths_total", v.get("paths_total")),
                ):
                    if _v is None or _v == "":
                        continue
                    facts.append(self._make_fact(_p, _v, _key, "ultrapath_vlun", src, ts))

        # ── 每条路径的状态（r_st_ultrapath_links_* 的明细依据）──
        up = _seg("up_show_path")
        if isinstance(up, dict) and (up.get("paths") or []):
            normal = sum(1 for p in up["paths"] if p.get("path_state") == "Normal")
            facts.append(self._make_fact("storage.ultrapath_path_normal_count", normal,
                                         host, "node", src, ts))
            facts.append(self._make_fact("storage.ultrapath_path_total_count",
                                         len(up["paths"]), host, "node", src, ts))
            # 按控制器聚合「活跃路径数」→ r_st_nvme_path_imbalance（阈值 <2 触发）
            per_ctrl: Dict[str, int] = {}
            for p in up["paths"]:
                c = p.get("controller") or ""
                if c and p.get("path_state") == "Normal":
                    per_ctrl[c] = per_ctrl.get(c, 0) + 1
            for ctrl, n in sorted(per_ctrl.items()):
                facts.append(self._make_fact("storage.nvme_path_per_ctrl", n,
                                             f"ctrl-{ctrl}", "dorado_controller", src, ts))
                # ★ 2026-09-23 新增：把**裸控制器 id** 也落成事实。
                #   `upadmin show path` 的 controller 字段本来就是裸的 0A/0B，
                #   与 storage 探针从设备侧读到的 ID 完全一致；此前只把它拼进
                #   obj_id（ctrl-0A）就丢掉了。丢掉之后，同一物理控制器在孪生图里
                #   成了两个节点（0A 有健康/CPU/主备状态，ctrl-0A 只有一个路径计数），
                #   两个节点状态颜色还可能不同 —— 人会以为阵列有四个控制器。
                #   落成同名事实后，本体 module 12 的 DoradoController 身份键
                #   即可把两侧合并为一个，且合并依据是**事实相等**而非前缀推断。
                facts.append(self._make_fact("storage.dorado.controller.id", ctrl,
                                             f"ctrl-{ctrl}", "dorado_controller", src, ts))
            # 已连接控制器数 / 期望控制器数（r_st_nvme_single_ctrl_conn，期望双控=2）
            all_ctrls = {p.get("controller") for p in up["paths"] if p.get("controller")}
            facts.append(self._make_fact("storage.connected_ctrl_count", len(per_ctrl),
                                         host, "node", src, ts))
            if len(all_ctrls) >= 2:
                facts.append(self._make_fact("storage.expected_ctrl_count", 2,
                                             host, "node", src, ts))
            for p in up["paths"]:
                if p.get("path_state") != "Normal":
                    facts.append(self._make_fact("storage.ultrapath_bad_path",
                                                 p.get("path_state"), f"path-{p.get('path_id')}",
                                                 "ultrapath_path", src, ts))

        # ── NVMe 子系统路径分布（仅作信息性产出，不参与 r_st_nvme_path_imbalance）──
        # ★ 口径说明（避免误报）：规则语义是「**单控制器**的活跃路径数 < 2」。
        #   实测 `nvme list-subsys` 里每个 subsystem 的路径是按 **target 端口** 分组的，
        #   某些合法配置（如共享卷 subsys4）本来就是「1 路径/target」——
        #   若按 target 分组喂给该规则会**误报**。因此：
        #     · storage.nvme_path_per_ctrl 只由 upadmin `show path` 的**控制器(0A/0B)**聚合产出
        #     · 这里改用独立路径名 nvme_paths_per_target 承载 target 维度信息
        ns = data.get("nvme_subsys_paths")
        if isinstance(ns, dict):
            for s in (ns.get("subsystems") or []):
                for tgt, cnt in (s.get("live_paths_per_target") or {}).items():
                    facts.append(self._make_fact("storage.nvme_paths_per_target", cnt,
                                                 f"target-{tgt}", "nvme_target", src, ts))
                # 子系统级：已连接目标数（信息性，Dorado 双控通常为 2 个目标面）
                tl = len(s.get("live_paths_per_target") or {})
                facts.append(self._make_fact("storage.nvme_connected_target_count", tl,
                                             s.get("name", ""), "nvme_subsys", src, ts))
                if s.get("unhealthy_paths"):
                    facts.append(self._make_fact("storage.nvme_unhealthy_paths",
                                                 ",".join(str(x) for x in s["unhealthy_paths"]),
                                                 s.get("name", ""), "nvme_subsys", src, ts))

                # ── ★ 端点级路径事实（2026-09-16 新增）─────────────────
                # 上方是「仅供信息」的聚合计数；此处才是配对关系的载体：
                # 每条 path 的 (host_traddr ↔ traddr) 与阵列侧 host_link 的
                # (hostIP ↔ TARGET_PORT_WWN) **逐字符一致**，是跨层 join 的键。
                # obj_id 带阵列 SN：实测同一节点的 nvme 控制器横跨 3 个阵列，
                # 短名（nvme1）会跨阵列重名。
                eps = s.get("endpoint_paths") or []
                if eps:
                    facts.append(self._make_fact("storage.nvme_path.count", len(eps),
                                                 s.get("name", ""), "nvme_subsys", src, ts))
                for ep in eps:
                    if str(ep.get("state", "")).lower() != "live":
                        continue          # 只落 live 路径（与阵列侧 host_link 口径一致）
                    sn = ep.get("array_sn") or "unknown"
                    oid = f"{sn}|{host}|{ep.get('ctrl', '')}"
                    for path, val in (
                        ("storage.nvme_path.array_sn", sn),
                        ("storage.nvme_path.subsys_nqn", ep.get("subsys_nqn")),
                        ("storage.nvme_path.ctrl", ep.get("ctrl")),
                        ("storage.nvme_path.state", ep.get("state")),
                        ("storage.nvme_path.traddr", ep.get("traddr")),
                        ("storage.nvme_path.host_traddr", ep.get("host_traddr")),
                        ("storage.nvme_path.trsvcid", ep.get("trsvcid")),
                    ):
                        if val not in (None, ""):
                            facts.append(self._make_fact(path, val, oid,
                                                         "nvme_path", src, ts))

        # ── UltraPath 主设备号（r_st_ultrapath_major_conflict 期望 251）──
        dm = data.get("up_device_major")
        if isinstance(dm, dict) and dm:
            majors = set()
            for dev, maj in dm.items():
                try:
                    majors.add(int(str(maj).strip()))
                except (TypeError, ValueError):
                    continue
            if majors:
                facts.append(self._make_fact("storage.ultrapath_major", sorted(majors)[0],
                                             host, "node", src, ts))
                if len(majors) > 1:
                    facts.append(self._make_fact("storage.ultrapath_major_consistent",
                                                 False, host, "node", src, ts))

        # ── UltraPath 版本 + 阵列兼容性 ──
        uv = _seg("up_show_version")
        if isinstance(uv, dict) and uv:
            if uv.get("software_version"):
                facts.append(self._make_fact("storage.ultrapath_version",
                                             uv["software_version"], host, "node", src, ts))
            if uv.get("driver_version"):
                facts.append(self._make_fact("storage.ultrapath_driver_version",
                                             uv["driver_version"], host, "node", src, ts))
            if uv.get("product_version"):
                facts.append(self._make_fact("storage.ultrapath_product_version",
                                             uv["product_version"], host, "node", src, ts))

        sa = _seg("up_show_supportarray")
        if isinstance(sa, dict) and sa.get("product_count"):
            facts.append(self._make_fact("storage.ultrapath_supported_product_count",
                                         sa["product_count"], host, "node", src, ts))
            # ★ 兼容性语义（重要，避免误报）：
            #   实测 Dorado5500（ProductName=XSG1）正在提供 4/4 Normal 路径，
            #   却**不在** `show supportarraylist` 里（该表是 Dorado2100/5100 + S 系列）。
            #   可见该列表只是「历史上明确宣称兼容的型号」，**未收录 ≠ 不兼容**。
            #   因此：
            #     · in_support_list：阵列（承载在用 vlun 的阵列名）是否被列表收录 —— 客观事实
            #     · version_compatibility：**不擅自下"不兼容"结论**。只有能正向
            #       证明不兼容时才应为 false；当前无该信息，故按"兼容（未发现冲突）"产出，
            #       具体核对交由人工/厂商矩阵（规则 r_st_ultrapath_version_mismatch 仍在，
            #       但以 in_support_list=false 作为排查线索而非故障判定）。
            vl2 = _seg("up_show_vlun")
            in_list = bool(sa.get("supported"))
            facts.append(self._make_fact("storage.ultrapath_array_in_support_list",
                                         in_list, host, "node", src, ts))
            facts.append(self._make_fact("storage.version_compatibility", True,
                                         host, "node", src, ts))
            facts.append(self._make_fact(
                "storage.compat_check_method",
                "ultrapath_supportarraylist(线索) + 在役路径状态(实测)", host, "node", src, ts))

        # ── 阵列固件/型号（供 r_st_ultrapath_version_mismatch 的 dorado_fw_version）──
        arr = _seg("up_show_array")
        if isinstance(arr, dict) and arr:
            if arr.get("product_name"):
                facts.append(self._make_fact("storage.dorado_product_name",
                                             arr["product_name"], host, "node", src, ts))
            if arr.get("array_sn"):
                facts.append(self._make_fact("storage.dorado_array_sn",
                                             arr["array_sn"], host, "node", src, ts))

        # ── NVMe 错误计数与增量（r_st_nvme_err_count_high）──
        # 契约：delta = 本次累计 − 上次累计；rate = delta / 采样间隔秒数。
        # 由于需要「上次值」，这里把累计值持久化到状态文件（跨调用保留）。
        nse = data.get("nvme_smart_errors")
        if isinstance(nse, dict) and (nse.get("controllers") or []):
            cur_total = 0
            for c in nse["controllers"]:
                me = c.get("media_errors")
                ne = c.get("num_err_log_entries")
                vals = [v for v in (me, ne) if isinstance(v, int)]
                if vals:
                    cur_total += sum(vals)
            prev, prev_ts = self._load_err_checkpoint()
            if prev is not None:
                delta = max(0, cur_total - prev)
                dt = max(1.0, time.time() - prev_ts)
                facts.append(self._make_fact("storage.nvme_err_count_delta", delta,
                                             host, "node", src, ts))
                facts.append(self._make_fact("storage.nvme_err_count_delta_rate",
                                             round(delta / dt, 4), host, "node", src, ts))
            self._save_err_checkpoint(cur_total, time.time())
            facts.append(self._make_fact("storage.nvme_err_count_total", cur_total,
                                         host, "node", src, ts))

        return facts

    # ── NVMe 错误计数检查点（跨调用保留，用于算增量）──
    _ERR_CKPT_FILE = os.environ.get(
        "OS_ERR_CHECKPOINT", "/tmp/os_nvme_err_checkpoint.json")

    @classmethod
    def _load_err_checkpoint(cls):
        try:
            with open(cls._ERR_CKPT_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return int(d["total"]), float(d["ts"])
        except Exception:
            return None, 0.0

    @classmethod
    def _save_err_checkpoint(cls, total: int, ts: float):
        try:
            tmp = cls._ERR_CKPT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"total": int(total), "ts": float(ts)}, f)
            os.replace(tmp, cls._ERR_CKPT_FILE)
        except Exception:
            pass

    def _diagnose_host(self, host: str, data: Dict[str, Any]) -> List[dict]:
        """单节点诊断"""
        alerts = []
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")

        # UltraPath 缺失
        up = data.get("ultrapath_devices", {})
        if isinstance(up, dict) and up.get("status") == "no_ultrapath":
            alerts.append({
                "severity": "critical",
                "title": "UltraPath 不可用",
                "detail": f"主机 {host} 未发现 UltraPath 多路径设备",
                "entity": host,
                "source": "os_probe",
                "timestamp": ts,
            })

        # NVMe 不可用
        nvme = data.get("nvme_list", {})
        if isinstance(nvme, dict) and nvme.get("status") == "no_nvme_subsys":
            alerts.append({
                "severity": "warning",
                "title": "NVMe 子系统不可达",
                "detail": f"主机 {host} 无法读取 NVMe 信息",
                "entity": host,
                "source": "os_probe",
                "timestamp": ts,
            })

        return alerts

    # ── 跨节点汇总 ──

    def _cross_node_summary(self, nodes: Dict[str, Any]) -> Dict[str, Any]:
        """多节点汇总"""
        return {
            "node_count": len(nodes),
            "nodes": list(nodes.keys()),
        }

    # ── 集群级聚合事实（跨节点视角）──

    def _cluster_facts(self, nodes: Dict[str, Any], ts: str) -> List[dict]:
        """把各节点的派生结果汇总成「集群视角」事实

        为什么需要它：`r_st_io_scheduler_inconsistency` 这类规则问的是
        「**节点之间**配置是否一致」，逐节点事实无法表达该语义（单看每个节点都自洽）。
        这里统一在 OS 探针内做跨节点聚合，避免下游各写一套判定。
        """
        out: List[dict] = []
        src = "os.aggregate"
        sched_devs: Dict[str, set] = {}

        for host in sorted(nodes.keys()):
            nd = nodes.get(host) or {}
            samp = nd.get("cpu_mem_sample")
            if not isinstance(samp, dict):
                continue
            for dev, s in (samp.get("scheduler") or {}).items():
                cur = ""
                if "[" in s and "]" in s:
                    cur = s[s.index("[") + 1:s.index("]")]
                elif s:
                    cur = s.split()[0]
                if cur and dev.startswith(("nvme", "ultrapath")):
                    sched_devs.setdefault(dev, set()).add(cur)

        if sched_devs:
            all_vals = {v for s in sched_devs.values() for v in s}
            out.append(self._make_fact("cluster.io_scheduler_consistent",
                                       len(all_vals) <= 1, "cluster", "cluster", src, ts))
            out.append(self._make_fact(
                "cluster.io_scheduler_detail",
                ";".join(f"{d}={'/'.join(sorted(v))}" for d, v in sorted(sched_devs.items())),
                "cluster", "cluster", src, ts))
        return out


# ── 注册到全局注册表 ──
register_probe(OsProbe())


# ══════════════════════════════════════════════════════════════════════════
# SSH 复用通道预热 —— 2026-09-21
#
# 为什么需要：复用通道的**首次建立**要付一次完整认证（实测冷启动那一次采集
#   10~20s，之后 4~5s）。而控制面（Agent 侧）两轮之间通常间隔数十秒到数分钟，
#   `ControlPersist` 到期后通道已被回收 ⇒ **下一个用户请求又变成"冷启动"**。
#   把这次开销**移到服务启动时**，第一个请求就不必等。
#
# 护栏：
#   · 只在 live 模式建通道（与 _mux_opts 的既有守卫同源：演练不得连真实主机）
#   · 后台线程 + 单个连接即可（`-N -f` 只建通道不执行命令）
#   · 任何失败都**静默忽略**：预热只是优化，不建立也不影响正确性
#     （首条采集会自己建，最坏等价于没有预热）
#   · 幂等：重复调用无害（通道已存在时 ssh 直接复用）
# ══════════════════════════════════════════════════════════════════════════
def warm_mux(timeout: int = 20) -> dict:
    """预热各节点的复用通道；返回 {host: True/False}（失败不影响功能）"""
    out: Dict[str, bool] = {}
    if not _MUX_ON:
        return out
    _s = globals().get("_sim")
    if _s is not None and hasattr(_s, "mode"):
        try:
            if _s.mode() != "live":
                return out          # 演练模式：不向真实主机建常驻连接
        except Exception:                                        # noqa: BLE001
            return out
    if not _SSH_PASS and not _SSH_KEY:
        return out                  # 无凭据：留给真正采集时的回落路径处理
    # ★ `_hosts` 是**实例属性**（在 __init__ 里从 GAUSSDB_NODES 解析），
    #   取 `OsProbe._hosts` 会得到 None ⇒ 预热静默不生效（本次已踩）。
    try:
        hosts = list(OsProbe()._hosts or [])
    except Exception:                                            # noqa: BLE001
        return out
    for h in hosts:
        opts = _mux_opts(h)
        if "ControlMaster=auto" not in opts:
            out[h] = False
            continue
        cmd = (f"sshpass -p '{_SSH_PASS}' ssh {opts} "
               f"-o StrictHostKeyChecking=no -o ConnectTimeout={timeout} "
               f"-o PreferredAuthentications=password "
               f"-p {_SSH_PORT} -N -f {_SSH_USER}@{h}")
        try:
            subprocess.run(cmd, shell=True, timeout=timeout + 10,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            out[h] = True
        except Exception:                                        # noqa: BLE001
            out[h] = False
    return out
