# cc-tools-audit 轮转部署说明（R-29 落地）

适用对象：`shared/deploy/logrotate/cc-tools-audit.conf`（安装目标 `/etc/logrotate.d/cc-tools-audit`），
覆盖三个审计链 JSONL 文件：

| 服务 | 监听 | 审计文件（`CC_AUDIT_LOG`） |
|---|---|---|
| KG-Q（方案1 主臂） | `127.0.0.1:8301` | `/opt/cc-tools-kg/cc_tools_audit.jsonl` |
| ONTO-Q（方案2 主臂） | `127.0.0.1:8302` | `/opt/cc-tools-onto/cc_tools_onto_audit.jsonl` |
| KG-T（方案1 第二套门面） | `127.0.0.1:8303` | `/opt/cc-tools-kg/kgt/cc_tools_kgt_audit.jsonl` |

> **先读 §5。** 本文档**不代表已部署**，也**没有**在任何主机上验证过轮转。

---

## 0. 本文档是什么 / 不是什么

* **是**：R-29 第 ② 条（外部轮转 + 保留基线）的落地依据、安装与验证清单、
  M5 评测「新开一代」的操作规程，以及 `rename + create` 对这两个服务为何安全的
  **源码行号级证据**。
* **不是**：部署记录、真机实测报告、或任何形式的「已验证」声明。
  本仓库不含真机的 `ls -li` / `/proc/<pid>/fd` / `logrotate -d` 实测输出。

---

## 1. R-29 的要求 → 本文档/配置如何满足

R-29 权威文本 = `tmp/_m3_worklist.md` 的 §L。三条与本文件相关的条款：

### ① 服务本身只追加，永不自行截断 / 轮转

| 落地位置 | 证据 |
|---|---|
| ONTO：`scheme2-onto/service/cc_tools_onto.py:539` | `with open(path, "a", encoding="utf-8", newline="\n") as handle:` —— `"a"` 即追加 |
| ONTO：`scheme2-onto/service/cc_tools_onto.py:547-549` | 落盘异常只记入 `_AUDIT_STATE["error"]` 并 `return None`，**不**截断、**不**冒泡、**不**改工具面 |
| KG：`scheme1-kg/service/cc_tools.py:450` | `with open(AUDIT_FILE, "a", encoding="utf-8", newline="\n") as fh:` |
| KG：`scheme1-kg/service/cc_tools.py:439-440` | docstring 逐字：``一行 = 一次工具调用；`open(..., "a")` 是追加语义 ⇒ 服务重启后继续追加，绝不清空/截断既有内容。`` |
| 两侧 grep 实测 | 文件截断语义的调用 **0** 命中（`os.truncate` / `.truncate(` / `os.stat` / `getsize` / `st_ino` / `atexit` / `FileHandler` 全部 0；`truncated` 只是响应字段名）；两侧自检各有一条「追加而非截断」正控（ONTO 见 §2.2 末，KG 见 `scheme1-kg/service/cc_tools.py:439`） |

⇒ 服务**没有**任何自行轮转/截断的路径，轮转只能由外部执行 ⇒ 本配置是唯一的轮转执行者。

### ② 轮转与保留交外部 logrotate：`rename + create`、**禁** `copytruncate`、服务不得长期持有 fd

| 要求 | 本配置的可核对落点 |
|---|---|
| 用 `rename + create` 语义 | 只有 `create`，**没有** `copytruncate`（`copytruncate` 是「就地截断」语义，且 man 页说明它 `implies norenamecopy`、`create` 对它无效） |
| 禁止 `copytruncate` | `shared/verify/auditrot_gate.py` 的两条**必需**不变式强制：`no-copytruncate`（指令空间无独立 token）+ `no-copytruncate-literal`（原文无字面出现，连注释都不许） |
| 服务不得长期持有 fd | 见 **§2** 的行号级证据 |
| 基线：单文件 64 MiB 轮转 | 每个 block 的 `maxsize 64M`；门禁 `block-size-cap` 要求 `maxsize` 解析值 ≤ 64 MiB，且**禁止**用 `size`（`size` 与时间间隔互斥，会静默关掉 `daily`） |
| 基线：保留 ≥ 90 天 | 每个 block 的 `daily` + `rotate 90`；门禁 `block-rotate` 要求 `rotate ≥ 90`、`block-daily` 要求存在 `daily`。**注意 `rotate 90` 是「90 个世代」而不是「90 天」** ⇒ 见 §5.3 |

### ③ 证据来源纯净度：每代必须有声明式来源起点

起点标记的字段与格式见 **§4.2**；**关键约束：起点标记不得写进审计 JSONL 本身**
（那会让「手工产生的行」混进服务证据，正是 R-29 ③ 要禁止的）。

附加的混合来源条款（R-29 ④）：`:8303` 的 `/opt/cc-tools-kg/kgt/cc_tools_kgt_audit.jsonl`
在 R-27 之前混入过探针手工产生的行。按 ④ 的要求，这些行**按 `requestId` / 时点切分登记，
不物理删除**；本配置对它**不做**任何差别对待（同一套保留策略），切分登记在评测报告侧完成。

---

## 2. 为什么 `rename + create` 对这两个服务是安全的

> **证据基线（源码版本）**：下列行号对应本次取证时的文件内容 ——
> `scheme2-onto/service/cc_tools_onto.py` = 180007 B / 3853 行 /
> sha256 `a1f1679dc547548a076f4d124303b6979fff82e512717977b3b851e349e0d9fd`；
> `scheme1-kg/service/cc_tools.py` = 123526 B / 2768 行 /
> sha256 `e14062bf5ec7af2839dd301e0647e0d5108498093fb28f9f7cf291fb0737690d`。
> 两侧源码在当前迭代中**仍可能被改动** ⇒ 复核时请用 `grep -n` 按**关键代码行**定位
> （例如 `persist_audit_record` / `_append_audit_file` / `with open(path, "a"`），
> 而不要只认行号。

### 2.1 机制：安全的前提是「服务不缓存 fd」

logrotate 默认（rename）路径的时序是：**rename 旧文件 → 执行 `postrotate` → 由 `create`
重建同路径空文件**。因此：

* **服务每次调用才 `open(path, "a")`、用完即 `close`** ⇒ rename 之后的下一次写，
  `open` 命中的是 `create` 刚建好的**新 inode** ⇒ 新行进新文件、旧行留在 `.1`，
  审计链在文件名层面**连续且可切分**。
* **服务若长期持有一个 fd** ⇒ rename 之后它仍指向**旧 inode**，后续新行会继续写进
  已改名的 `.1`（甚至 `.1.gz` 被压缩后仍在写）⇒ 审计链错位、`requestId` 序号与文件名
  不对应，且**看不到任何报错**。这正是 R-29 ② 要求「服务不得长期持有文件描述符」的原因。

所以本节的证据目标只有一个：**证明两侧都是「每次调用 open 一次、with 退出即 close」。**

### 2.2 ONTO 侧 —— `scheme2-onto/service/cc_tools_onto.py`

| 行号 | 关键代码（逐字） | 说明 |
|---|---|---|
| `:509` | `def audit_path() -> Optional[str]:` | 路径解析入口 |
| `:510` | `"""审计 JSONL 落盘路径（R-27；逐次读环境变量，便于测试与在线改配置）。` | 「**逐次**读环境变量」——不缓存 |
| `:515` | `value = os.environ.get("CC_AUDIT_LOG")` | 每次调用现读环境变量 |
| `:516` | `return value or None` | 未设 ⇒ 不落盘 |
| `:519` | `_AUDIT_STATE: Dict[str, Any] = {"lines": 0, "error": None, "lastLineAt": None}` | 模块级审计状态里只有**计数器与时间戳**，**没有**文件对象 |
| `:522` | `def persist_audit_record(record: Dict[str, Any]) -> Optional[str]:` | 落盘入口 |
| `:532` | `path = audit_path()` | **每次调用**重新取路径 |
| `:539` | `with open(path, "a", encoding="utf-8", newline="\n") as handle:` | **每次调用 open 一次；`with` 退出即 close** |
| `:540` | `handle.write(_dumps(record) + "\n")` | 一条记录 = 一行 |
| `:541` | `handle.flush()` | 无跨调用用户态缓冲 |
| `:542` | `os.fsync(handle.fileno())` | 每行落盘（`fileno()` 只在 `with` 内取，无跨调用泄漏） |
| `:547-549` | `except Exception as exc:  # pragma: no cover - 审计写盘失败不得冒泡` → `_AUDIT_STATE["error"] = "%s: %s" % (type(exc).__name__, exc)` / `return None` | 轮转竞态导致的写失败不冒泡、不改工具面 |
| `:674-676`（`audit_log()` 尾部） | `AUDIT_CHAIN.append(record)` / `persist_audit_record(record)` | 内存环与文件**各写一处**，一次调用恰好一行 |

排除项（对 `scheme2-onto/service/cc_tools_onto.py` 全文 grep 实测）：
`os.truncate` / `.truncate(` / `os.stat` / `getsize` / `st_ino` / `atexit` / `FileHandler` /
`_AUDIT_FH` **全部 0 命中**。`:563` 的 `class AuditChain:` 是纯内存环形缓冲
（deque + lock），`docstring` 逐字声明「**本类不做任何 IO**」。全文件 `open(` 的写模式
只有两处与本链路无关/自检相关：`:494`（写临时文件）、`:3221`（写自检输出）。

另外，该服务**自己的自检**里就有一条针对本性质的**正控**：
`:3487` 注释逐字 `# 正控：既有行保留（从不 truncate）+ 逐调用 +1 行`，
`:3505` `check("r27/append-not-truncate（既有行保留且逐调用 +1 行）", ...)`
⇒ 「追加而非截断」是被测过的行为，不只是文档承诺。

**结论**：ONTO 侧的 fd 生命周期 = 单条 `with open(...)` 语句。任意两次工具调用之间
（即 logrotate 实际运行的时刻）进程**不持有**该 jsonl 的任何 fd ⇒ `rename + create` 安全。

### 2.3 KG 侧 —— `scheme1-kg/service/cc_tools.py`

| 行号 | 关键代码（逐字） | 说明 |
|---|---|---|
| `:394` | `AUDIT_CHAIN = AuditChain()` | 内存环 |
| `:395` | `AUDIT_FILE = os.environ.get("CC_AUDIT_LOG", "")` | 模块级只保存**路径字符串**，不是句柄 |
| `:399-402` | `def set_audit_file(path: str) -> None:` / `global AUDIT_FILE` / `AUDIT_FILE = path or ""` | 「设置路径」改的是字符串 |
| `:436` | `def _append_audit_file(record: Dict[str, Any]) -> None:` | 落盘入口 |
| `:439-440` | ``一行 = 一次工具调用；`open(..., "a")` 是追加语义 ⇒ 服务重启后继续追加，`` / `绝不清空/截断既有内容。` | docstring 逐字 |
| `:444-445` | `if not AUDIT_FILE:` / `return` | 空串 ⇒ 不落盘 |
| `:447-449` | `parent = os.path.dirname(AUDIT_FILE)` / `if parent:` / `os.makedirs(parent, exist_ok=True)` | 目录自建 |
| `:450` | `with open(AUDIT_FILE, "a", encoding="utf-8", newline="\n") as fh:` | **每次调用 open 一次；`with` 退出即 close** |
| `:451` | `fh.write(_dumps(record) + "\n")` | 一条记录 = 一行（无 `flush`/`fsync`，见 §5.6） |
| `:452-459` | `except Exception as exc:` → `sys.stderr.write("AUDIT_FILE_ERROR %s: %s\n" % (AUDIT_FILE, exc))` | 失败**留痕**、不冒泡、不谎报成功 |
| `:485` | `AUDIT_CHAIN.append(record)` | 内存环 |
| `:487` | `sys.stderr.write("AUDIT " + _dumps({"audit": record}) + "\n")` | stderr 审计面 |
| `:491` | `_append_audit_file(record)` | 文件面（与 `:485` 同一个 `record` 字典） |
| `:338-343` | `class AuditChain:` / `def __init__(self, capacity: int = 500) -> None:` / `self._records: "collections.deque" = collections.deque(maxlen=self.capacity)` | 纯内存，不涉及文件 |

排除项（对 `scheme1-kg/service/cc_tools.py` 全文 grep 实测）：全文件 `open(` **只**出现在
`:450`（审计追加）、`:2365` / `:2546`（读拓扑 / 清单）、`:2598`（写 manifest）——
即**没有任何**模块级或长生命周期地打开审计文件的位置；`atexit` / `FileHandler` /
`_AUDIT_FH` / `st_ino` 全部 0 命中。

**结论**：KG 侧同样是「每次调用 open 一次、with 退出即 close」⇒ `rename + create` 安全。

### 2.4 为什么**不能**用 `copytruncate`（以及本配置为什么连注释都不写这个词）

logrotate(8) 对 `copytruncate` 的描述要点：它**先复制**现有日志再**就地截断**原文件，
「there is a very small time slice between copying the file and truncating it, so some
logging data might be lost」；它 `implies norenamecopy`（即不会走 rename 路径），
并且 `create` 对它**无效**。

对本项目的后果是**直接致命**的：可能被丢掉的正是**审计行**，而「完整的事故后审计链」
是银行合规硬要求、也是 R-29 ①③ 的前提。所以：

* 配置里**没有** `copytruncate`；
* 门禁里两条必需不变式（`no-copytruncate` / `no-copytruncate-literal`）把它挡住，
  连**注释里出现**都会判 FAIL —— 解释性文字因此放在本文档，不放在配置里；
* 也**不用** `nocopytruncate`：它是**相反**的指令（用于抵消上层全局配置里的
  `copytruncate`），但本配置的 block 是自包含的、不依赖任何全局指令，加它只会
  引入一个含 `copytruncate` 子串的词。若担心 `/etc/logrotate.conf` 或其它
  `logrotate.d` 片段里的全局指令串进来，请在**那里**核查，而不是在本文件里做抵消。

### 2.5 残余风险（诚实记录，未在真机验证）

1. **`open()` 与 rename 之间的极窄竞态**：若 logrotate 的 rename 恰好发生在服务的
   `open(...)` 返回之后、`write(...)` 之前，那一行会写进已改名的 `.1`。
   窗口 ≈ 单次 `open→write`（微秒级）÷ logrotate 运行间隔（小时级）。
   表现为「某一行落在 .1 的头部」，**不丢数据**，但会让切分点需要按 `requestId` 而非
   行号确定（R-29 ④ 的做法本就如此）。
2. **`rename` 与 `create` 之间的极短窗口**：若服务恰在此窗口 `open(..., "a")`，
   服务自己会按自身 umask **新建**该文件，随后 logrotate 的 `create` 可能遇到
   「文件已存在」。这一竞态本仓库**未复现、未在真机观测**；处置建议（收紧服务 umask、
   或显式 `create 0640 <运行用户> <运行组>`）同样未验证。

---

## 3. 安装与验证步骤

> ★ 本节全部是**生产机变更操作**，必须经用户（人类）确认后由**人工**执行。
> 本仓库不提供、也不应存在任何自动化投放路径。

### 3.0 安装前：在仓库内先过离线门禁

```bash
python shared/verify/auditrot_gate.py            # 期望 PASS（exit 0）
python shared/verify/auditrot_gate.py --selftest # 期望 SELFTEST: 19/19 PASS
```

门禁校验的是**配置文本**的不变式（三个路径全覆盖 / `daily` / `maxsize ≤ 64 MiB` /
`rotate ≥ 90` / 有 `create` 且不 group/other 可写 / 无 `copytruncate` / 可解析），
**不能**替代真机验证。

### 3.1 安装

```bash
sudo install -m 0644 -o root -g root \
    shared/deploy/logrotate/cc-tools-audit.conf \
    /etc/logrotate.d/cc-tools-audit
ls -l /etc/logrotate.d/cc-tools-audit        # 期望 -rw-r--r-- root root
```

* `logrotate` 会**拒绝**加载 group/world 可写的配置 ⇒ 模式固定 `0644`（或 `0600`），不要放宽。
* 安装后与仓库内比对（人工）：`sudo diff /etc/logrotate.d/cc-tools-audit shared/deploy/logrotate/cc-tools-audit.conf`。

### 3.2 dry-run（`logrotate -d`）

```bash
sudo logrotate -d /etc/logrotate.d/cc-tools-audit          # 纯 dry-run，不轮转
sudo logrotate -d -f /etc/logrotate.d/cc-tools-audit       # 强制走完动作链，但仍不落盘
```

* `-d` 是 debug/dry-run，**不**修改任何文件。
* 期望：三个路径各出现一行 `considering log /opt/...`，并给出
  `rotating log ...` / `log does not need rotating` 等判定；输出里**不得**出现 `error:`。
* 若出现 `skipping ... because parent directory has insecure permissions`，
  按提示收紧目录权限后再重跑（这属于生产机现状问题，不是本配置的问题）。

### 3.3 验证轮转真的发生了（待执行清单，四步）

> 前置：`logrotate -f` 会**真的轮转**并消耗一个世代号。**建议先做 §4 的「新开一代」**
> 再做本节，否则会把 M5 的起点标记搅乱。

以 KG-Q 为例（另外两条路径同样重复）：

**(a) 轮转前后 inode 必须变化 —— 这是 `rename + create`（而非就地截断）的直接证据**

```bash
F=/opt/cc-tools-kg/cc_tools_audit.jsonl
ls -li "$F"
sudo logrotate -f /etc/logrotate.d/cc-tools-audit
ls -li "$F"
ls -li /opt/cc-tools-kg/            # 应出现 cc_tools_audit.jsonl.1（或 .1.gz）
```

* 预期：两次 `ls -li` 的 **inode 号不同**，目录里出现 `.1`。
* 若 inode **相同** ⇒ 实际是截断语义或 `create` 没生效 ⇒ **停下来排查**，
  不得继续按本配置轮转。

**(b) 轮转后服务仍往新文件追加（调用一次看新文件长一行）**

```bash
wc -l < "$F"        # 轮转后先记下新文件行数
# 触发一次工具调用（按 M3 既有的探针/门面调用口径）
wc -l < "$F"        # 预期 = 前一次 + 1
```

* 预期：新文件**长一行**，且这一行由**服务**写出（不是人工造的）。
* R-29 ③ 提醒：这一行只用于验证「追加到了新文件」。若它是探针手工产生的，
  **不得**登记为评测代的来源证据。

**(c) `/proc/<pid>/fd` 里没有该 jsonl 的长期 fd**

```bash
PID=$(systemctl show -p MainPID --value cc-tools-kg)     # 或 ps -ef | grep cc_tools
sudo ls -l /proc/"$PID"/fd/ | grep -F cc_tools_audit.jsonl || echo "NO_LONG_LIVED_FD (expected)"
```

* 预期：**无输出**（`grep` 退出码 1）⇒ 没有长期 fd ⇒ `rename + create` 安全。
* 若列出该文件 ⇒ 服务缓存了 fd ⇒ **不得**启用本配置的轮转（先改服务）。
* 轮转发生后若显示 `cc_tools_audit.jsonl (deleted)` 形态，说明该进程正持有**旧 inode**。

**(d) 三条路径逐一重复 (a)–(c)**，并把每一条的真实输出留档。
（G17 / G20 / mdcheck 与真机门禁**不在本文档范围**，由队长统一跑。）

### 3.4 门禁与真机的关系

`shared/verify/auditrot_gate.py` 的 `PASS` 只说「这份配置文本满足不变式」，
**不能**证明 `/etc/logrotate.d/cc-tools-audit` 已安装、内容一致、或已被调度。

---

## 4. M5 新开一代（generation）与起点标记

R-29 ③⑤：每次评测代必须有**声明式来源起点**（时间 + PID + 首个 `requestId` + 起始行数 0），
且 M5 开跑前必须**新开一代**审计文件并记录起点标记。

### 4.1 「新开一代」≠「清空文件」

服务侧只追加、绝不自行截断（§1①、§2）。所以「新开一代」的正确做法**不是**让服务清空，
而是**由人把当前文件改名归档**，让服务在**原路径**上从 0 行重新开始。两种做法：

* **做法 A（推荐；不动服务配置、不重启）**
  ```bash
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  sudo mkdir -p /opt/cc-tools-kg/archive
  sudo mv /opt/cc-tools-kg/cc_tools_audit.jsonl \
          /opt/cc-tools-kg/archive/cc_tools_audit.pre-m5."$TS".jsonl
  sudo sha256sum /opt/cc-tools-kg/archive/cc_tools_audit.pre-m5."$TS".jsonl   # 登记归档哈希
  ```
  服务的下一次调用会 `open(..., "a")` 在**原路径**新建空文件、从第 1 行开始
  ⇒ 「起始行数 0」成立。归档文件**不删除**（R-29 ④）。
* **做法 B（显式换代）**：把该服务的 `CC_AUDIT_LOG` 指到带代号的新路径
  （如 `.../cc_tools_audit.g5.jsonl`）并重启服务，旧路径不动。
  改环境变量需要重启 ⇒ **属变更操作**、会打断 `:8301` / `:8302` / `:8303` 的可用性，
  须在评测窗口外做。

### 4.2 起点标记格式（三个审计文件各一份）

在归档/换代完成之后、M5 第一次工具调用之前，记录下列字段：

```
# ---- generation start marker ----
generation:                m5
service:                   kg-q                 # kg-q | onto-q | kg-t
endpoint:                  127.0.0.1:8301
audit_file:                /opt/cc-tools-kg/cc_tools_audit.jsonl
started_at_utc:            2026-10-07T02:11:45Z # 记录时刻（date -u +%Y-%m-%dT%H:%M:%SZ）
service_pid:               27183                # ps -o pid= / systemd MainPID
first_request_id:          20261007T021149Z-000001   # M5 第一次调用落在首行的 requestId（实取）
start_line_count:          0                    # 起点行数必须为 0
file_sha256_at_start:      e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
                                                # 空文件的 sha256（常量；仍须实测复核）
pre_rotation_file:         /opt/cc-tools-kg/archive/cc_tools_audit.pre-m5.20261007T021100Z.jsonl
pre_rotation_file_sha256:  <sha256sum 实测值>
pre_rotation_line_count:   <wc -l 实测值>
recorded_by:               <执行人>
```

* 字段含义（逐条对应 R-29 ③）：**时间** = `started_at_utc`；**PID** = `service_pid`；
  **首个 `requestId`** = `first_request_id`（两侧格式均为
  `<YYYYMMDDThhmmssZ>-<6 位进程内序号>`，见 `scheme1-kg/service/cc_tools.py:405-407`）；
  **起始行数 0** = `start_line_count`；**文件 sha256** = `file_sha256_at_start`。
* `first_request_id` 的取法：M5 第一次调用后 `head -1 <audit_file>`，从该行 JSON 里读 `requestId`。
* **起点标记不得写进审计 JSONL 本体**（会污染服务证据，正是 R-29 ③ 要禁止的）。
  建议落在 `tmp/_m5_genstart/<service>.json` 或工作清单的记录区。

### 4.3 开代后的即时复核

```bash
F=/opt/cc-tools-kg/cc_tools_audit.jsonl
sudo wc -l < "$F"                                  # 期望 0
sudo sha256sum "$F"                                # 期望 e3b0c442…b7852b855（空文件）
ls -l "$F"                                         # 期望属主/组 = 服务运行用户，模式不留 group/other 写
```
非 0 行 ⇒ 起点不纯净，**重新归档再开一代**，不要将就。

### 4.4 每代收尾

记录 `end_line_count` / `end_file_sha256` / 该代区间内产生的轮转世代清单
（`ls -1 <dir>` + `sha256sum`），与本代的起点标记一起归档，供 M5 报告引用。
按 R-29 ④，混装文件（尤其 `:8303` 的 kgt 文件）以 `requestId` / 时点切分登记，
**不得物理删除**。

---

## 5. 诚实边界（必读）

1. **本文档不代表已部署。** `shared/deploy/logrotate/cc-tools-audit.conf` 目前只存在于
   本仓库，**没有**安装到任何主机，**没有**任何主机在用它轮转。安装属变更操作，
   须用户确认后由人工执行（§3.1）。
2. **未做过真机轮转验证。** §3.3 的四步（inode 变化 / 新文件继续追加 / 无长期 fd /
   三路径逐一）是**待执行清单**，不是已获得的结果。本文档不含任何真机
   `ls -li`、`/proc/<pid>/fd`、`logrotate -d` 的实测输出。
3. **保留 ≥ 90 天的边界：`rotate 90` 是「90 个世代」，不等于「90 天」。**
   `maxsize 64M` 会在高流量下**提前**触发轮转，一天可能产生多个世代，
   于是 90 个世代可能远少于 90 天。设某服务每天产生 N 个 64 MiB 世代，
   则保留时长 ≈ 90/N 天：**N ≥ 2 就不足 90 天。**
   * **当前实际形态下不触界**：存量（`tmp/_m3_worklist.md` §M，采集 2026-10-06）为
     `:8301` 1944 B / 4 行、`:8302` 10593 B / 14 行、`:8303` 7445 B / 10 行，
     离 64 MiB 有 4 个数量级 ⇒ `daily` 是唯一实际触发条件，90 个世代 ≈ 90 天，满足基线。
   * **缓解方式**（按需择一，均属变更操作）：
     (a) 提高 `rotate`（如 `rotate 180`）；或
     (b) 另建长保留归档（压缩 + 定期同步到冷存储），把在线世代数与长期保留解耦；或
     (c) 接受「`maxsize` 作保护性上限」与「90 天」的取舍，并在 M5 报告里**显式声明**
     实际保留天数与算法。
   * **不可用的缓解**：为了凑够 90 天而放宽单文件 64 MiB —— 那是 R-29 的硬基线。
4. **`delaycompress` 的边界**：被延后压缩的那个 `.1` 在下一轮轮转前保持**未压缩**，
   因此比其它世代多占一份磁盘；若 logrotate 每天只跑一次，`.1` 可能整天不压缩。
5. **`maxsize` 只在 logrotate 运行时被检查。** man 页原文：「It will not modify a log more
   than once in one day unless the criterion for that log is based on the log's size and
   logrotate is being run more than once each day」⇒ 若生产机每天只跑一次 logrotate，
   两次运行之间的单文件**可以超过** 64 MiB。配置头注释已要求把运行频率提到每小时
   （systemd `logrotate.timer` 或 hourly cron）；**这一点未经真机确认。**
6. **`create 0640` 的属主/属组来自被轮转文件本身**（man 页：「Any of the log file
   attributes may be omitted, in which case those attributes for the new file will use the
   same values as the original log file for the omitted attributes.」）⇒ 本仓库**不需要**、
   也不会臆造生产机用户名。若生产机要求显式指定，必须**实测后**改成
   `create 0640 <运行用户> <运行组>`；可选的 `su <运行用户> <运行组>` 同理。
   另注：KG 侧 `scheme1-kg/service/cc_tools.py:450-451` 只 `write` 不 `flush`/`fsync`
   （ONTO 侧 `:541-542` 有 `flush` + `fsync`），因此 KG 侧「最后一行是否已落盘」依赖
   Python 缓冲；这不影响轮转正确性，但影响「轮转时刻最后一行是否已可见」的判断。
7. **`rename` 与 `open()` / `create` 之间的竞态未验证**（§2.5）。
8. **三个路径的属主/权限可能不同。** 本配置因此写成三个**自包含** block（不继承任何
   顶层全局指令），但三处指令**完全相同**。若生产机需要差异化（例如 kgt 目录权限不同），
   那是真机实测后才做的调整，本仓库不做假设。
9. **门禁覆盖的是配置文本，不是真机状态**（§3.4）。
10. **未跑任何真机门禁 / G17 / G20 / mdcheck**：按任务分工由队长统一执行。

---

## 6. 离线门禁：`shared/verify/auditrot_gate.py`

```bash
python shared/verify/auditrot_gate.py                 # 人类可读
python shared/verify/auditrot_gate.py --json          # 机器可读（JSON）
python shared/verify/auditrot_gate.py --config PATH   # 校验指定文件
python shared/verify/auditrot_gate.py --selftest      # 自检（含负控）
python shared/verify/auditrot_gate.py --selftest --json
```

* **三态**：`PASS` exit 0 / `FAIL` exit 1 / `BLOCKED` exit 2。
  `BLOCKED`（配置不存在、不是普通文件、读失败、非 UTF-8、**无法解析**）表示
  「**无法判定**」，**不是**「配置有问题」——这是有意的区分，避免把工具故障伪装成
  配置缺陷。`--selftest` 全通 exit 0、否则 exit 1（不复用被测文件的退出码）。
* **不变式**：见脚本 docstring；要点 = 三个审计路径全覆盖（按**解析出的 block 头**比对，
  注释里提到不算）/ 每 block 有 `daily` / `maxsize` 存在且 ≤ 64 MiB 且不用 `size` /
  `rotate ≥ 90` / 有 `create` 且模式不 group-other 可写 / 有 `compress`、
  `delaycompress`、`missingok`、`notifempty` / 指令空间与原文都无 `copytruncate`。
* 解析器按 logrotate 的真实语法写：整行注释（`#` 为行首首个非空白字符）、
  路径 + `{ … }` 块、`=` 与空白同为分隔符、脚本块正文（`prerotate`/`postrotate`/
  `firstaction`/`lastaction`/`preremove` 到 `endscript`）当作不透明文本跳过。
* 门禁**只读**：不安装、不轮转、不触碰真机。
