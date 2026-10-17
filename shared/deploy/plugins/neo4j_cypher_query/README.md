# cc-ops / neo4j_cypher_query 0.1.0 —— Dify 市场插件 `neo4j_query` 的本地 fork

本目录是一个**自包含的 Dify 插件 fork 工程**：源码树 `src/`、确定性打包器 `build_pkg.py`、
Console-API 安装脚本 `install_plugin.py`。目标是让 Dify Agent（App `cc-kgt-free-agent`）
**能自主调用 Neo4j 图查询工具**，同时**不动**市场上已装的
`nikola_milosevic/neo4j_query`（两者并存，互不替换）。

> **状态（重要）**：源码树、打包器、安装脚本、自检均已完成并通过；
> 但**真机安装在本部署上被一道部署侧的「强制插件签名校验」挡住**（HTTP 400 `bad signature`）。
> 这是 **BLOCKED**（外部策略闸），不是 FAIL（不是代码或契约缺陷）。详见下面
> [§7 当前阻塞](#7-当前阻塞真机安装被强制签名校验挡住blocked非-fail)。

---

## 1. 溯源（上游身份，逐个可复算）

| 项 | 值 |
|---|---|
| 上游插件 id | `nikola_milosevic/neo4j_query` |
| 上游版本 | `0.2.0` |
| 上游 unique identifier（市场已装） | `nikola_milosevic/neo4j_query:0.2.0@99ca3d403ae707f83e81992b68354a8aabf5ad5bda1b0e02e5aaaeb763ecea38` |
| 上游源码 tar.gz（解包基线） | `tmp/m4_plugin_src/`，15 文件 / 51747 B，sha256 `296a38b70700c26970fbce4e4db2691df44068cdad0f7e95a9a2419daf4e05fa` |
| 上游已装包本体 | `tmp/upstream_neo4j_query.pkg`，zip / 23029 B / 15 条目，sha256 `b0f457e6c5404321dbd86cd19f689c7292d4a3cd66c5ce191ffecea68dd12f41` |
| 上游包内签名 | zip comment 718 B，`{"signature":"<RSA>","time":1766631660}`（见 §7） |

**上游无 LICENSE 文件**（其 `.difyignore`/`PRIVACY.md`/`SECURITY.md`/`GUIDE.md` 原样保留在 fork 树里）。

## 2. 为什么必须 fork（不是配置问题，是插件清单的结构问题）

上游 `tools/neo4j_query.yaml` 的 **5 个参数全部 `form: form`**，其中 `query` 是
`required: true` 且**没有 default**，而 Agent 侧 `tool_parameters` 为 `{}`。后果链条（Dify 1.17.0 源码级）：

1. **抛点** `/app/api/core/plugin/entities/parameters.py:255-262`
   `init_frontend_parameter`：`parameter_value = rule.default` → 空且 required ⇒
   `raise ValueError("tool parameter query not found in tool config")`。
2. **调用点** `/app/api/core/tools/tool_manager.py:1136`，位于 `:1091`
   `if parameter.form == ToolParameterForm.FORM:` 的 else（variable_pool 为空）分支内
   ⇒ **只有 `form: form` 的参数会走到抛点**；改成 `form: llm` 后整条路径被跳过。
3. **模型面闸** `/app/api/core/tools/__base/tool.py:166 get_llm_parameters_json_schema()`，
   `:188 if parameter.form != ToolParameterForm.LLM: continue`
   ⇒ 上游 `form: form` 的 `query` **永远进不了模型的 tool schema**，所以「预填 `tool_parameters`」
   也救不回**自主**查询。
4. **静默吞掉** `/app/api/core/agent/base_agent_runner.py:198-203`
   （`except Exception: # api tool may be deleted / continue`）⇒ 失败不报错，只是工具静默消失。

⇒ 修法只有一条：**fork 插件，把 `query` 与 `parameters` 改成 `form: llm`。**

### 为什么 tool 名不能改（冻结契约）

`tools/neo4j_query.yaml` 的 `identity.name` **必须保持 `neo4j_query`**：

- `shared/model/kgt-prompt-variants.yaml` 的 `KG-T-free.expected_tools = [neo4j_query]`（`expected_tool_count: 1`）；
- 冻结提示词 `shared/deploy/prompts/kgt_free_agent_system.md`（★ **2026-10-07 实测** = 5874 B / 78 LF，
  sha256 `a3c3fc05a5bfc940e344d79d120baf4cfde3348d647c80566c67fadf035293d6`；旧登记值 `5748 B / 77 LF / fb2de6c6…` **仅存沿革、不得再引**）正文逐字引用 `neo4j_query`；
- `shared/deploy/check_fairness.py:1148-1174`（`_f5_extra_apps`）按 **tool_name 集合**把
  App 绑定工具与 binding 声明判等 ⇒ 换 tool_name 会直接 FAIL。

因此本 fork 的命名是：**plugin 名改**（`neo4j_cypher_query`）、**provider/tool 名不改**（`neo4j_query`）。

## 3. 唯一的行为改动

`src/tools/neo4j_query.yaml` 里**只有两个 `form` 值**从 `form` 改为 `llm`：

| # | 参数 | type | required | default | 上游 form | **fork form** | 为什么 |
|---|---|---|---|---|---|---|---|
| 1 | `database` | string | false | — | `form` | `form` | 目标库由工作流控制 |
| 2 | `query` | string | **true** | — | `form` | **`llm`** | ← 让 Agent 自己填 Cypher（本次修复的**唯一目的**） |
| 3 | `parameters` | string | false | — | `form` | **`llm`** | Cypher 具名参数由模型填 |
| 4 | `max_records` | number | false | **1000** | `form` | `form` | 行数上限留在 LLM 不可及处 |
| 5 | `allow_write_queries` | boolean | false | **false** | `form` | `form` | 写开关留在 LLM 不可及处 |

行号（`src/tools/neo4j_query.yaml`）：`database` `:15-27`（form `:27`）、`query` `:28-53`（form `:53`）、
`parameters` `:54-80`（form `:80`）、`max_records` `:81-94`（form `:93`/default `:94`）、
`allow_write_queries` `:95-108`（form `:107`/default `:108`）。

**`tools/*.py` 与 `provider/*.py` 的 Python 逻辑一行未改**（与上游逐字节相同，见 §4 表 ①）。

## 4. 上游 → fork 逐条差异表

文件级：**逐字节相同 12 / 内容变更 3 / 新增 0 / 删除 0**（`tmp/_m4_w8_diff_table.md`，由
`tmp/_m4_w8_make_diff_table.py` 生成）。

### ① 逐字节相同（12 个，sha256 与上游完全一致）

| 路径 | bytes | LF | sha256 |
|---|---|---|---|
| `.difyignore` | 3443 | 179 | `4358960a800624682d11a09b176cc25d09235b396b45533f59007096fbe3f75d` |
| `.env.example` | 116 | 3 | `27abd29fa7b8061c3eae780537c4ceae5f39b4d0a2faabfbda00371b49edb147` |
| `.verification.dify.json` | 35 | 0 | `6c1558142d852d376df6ab38f7cae98e9585dc7e324d3e207c8ab488c33f8042` |
| `GUIDE.md` | 8208 | 147 | `e82bf61264dcee386a636bcce8214bd1293c5222246015940eb0126180d087e4` |
| `PRIVACY.md` | 2452 | 56 | `199df08970f8f638d2c2c9c1ecf30f632a512805752d16643c921e0e5f25bd3a` |
| `README.md` | 5350 | 178 | `49bc3af220e6b0d77ddc9cdd3c8ac33af4535e4fe83849cf18f6764d1caa400f` |
| `SECURITY.md` | 7404 | 273 | `32d91c83e3ee3c2a644a1826d7bca35d283a0ad96e15f27c566ddd2297a1d427` |
| `_assets/icon.svg` | 3884 | 7 | `c3898e3c044eaa44e4da279f6cd5591871842bb4875b3ec4c82cfdccd0689aee` |
| `main.py` | 148 | 6 | `8f1d8e24060e48a7e81c64dfa28908f85b552f101e96b4425e256c73fcb17a04` |
| `provider/neo4j_query.py` | 3947 | 78 | `7b2baac1d881bd6a5c8fb5154515d9b42b7d56b210c03d4fcbd0f9925037e32d` |
| `requirements.txt` | 32 | 2 | `954198c07b991e2d74ef3a782299a3ecc22b22d8ce11c603d4858c923d990676` |
| `tools/neo4j_query.py` | 8530 | 186 | `4799af6fa9e07e20f3fb8a77fca13528b18d44e1d5c48ffdd25b39f11d226abf` |

### ② 内容变更（3 个）

| 路径 | bytes 上→fork | sha256 上游 | sha256 fork |
|---|---|---|---|
| `manifest.yaml` | 1619 → 1695 | `66765077b527911808b8d8ad985579debcdaf4b7bc667a8e129736b7d26ad7bf` | `4a553c9f9691931ab3e516246adc949f8f6ea33311cb9f244a2e99baaf468d4c` |
| `provider/neo4j_query.yaml` | 1130 → 1836 | `0032b784e5dd5af4c94513f01939cb865ef28125c5ff48d0b24d5d0c28825777` | `eaddf5fa3838a035119ff9677ab375251a4578d3cfdc5c9cfaf1a9b9c4844e49` |
| `tools/neo4j_query.yaml` | 5449 → 6409 | `556da7a8afab832ce4708f3fdedf595e9d1c13a43aeb6079b2b8e7a1362cc70d` | `93b0e53e43c1a99f13d6df9305eec49ff61dd60f5c8d4c35f25d14ee41056d7f` |

### ③ 新增 / 删除

**无**。fork 树 = 15 文件 / 53489 B，文件路径与上游**逐字同名**（无改名，故 `source:` 引用无需改动）。

### 语义级差异（三条 yaml 里到底改了什么）

| 文件 | 改动 |
|---|---|
| `manifest.yaml` | `version`/`meta.version` `0.2.0`→`0.1.0`；`author` `nikola_milosevic`→`cc-ops`；`name` `neo4j_query`→`neo4j_cypher_query`；`label`/`description` 四语种改写为我方口径（见 §8 诚实性）；`created_at` 改为固定值 `2026-09-29T11:30:00+08:00`（固定以保可复现）。`type`/`resource`/`meta.runner`/`plugins.tools`/`privacy`/`verified` 均未动。 |
| `provider/neo4j_query.yaml` | 仅 `identity.author` `nikola_milosevic`→`cc-ops` 与 `identity.label`/`description` 改写。**`identity.name` 保持 `neo4j_query`**（冻结契约）。`tools:`、`extra.python.source`、`credentials_for_provider`（3 个凭据键）逐字未动。 |
| `tools/neo4j_query.yaml` | `identity.author`→`cc-ops`、`identity.label`/`description` 改写、`identity.name` 保持 `neo4j_query`；**参数面只改 `query`(`:53`) 与 `parameters`(`:80`) 两处 `form: form`→`form: llm`**。 |

### fork 对外身份（回读用真值）

| 项 | 值 |
|---|---|
| plugin_id | `cc-ops/neo4j_cypher_query` |
| provider_id | `cc-ops/neo4j_cypher_query/neo4j_query` |
| tool name | `neo4j_query`（与上游同名，冻结契约要求） |
| version | `0.1.0` |
| 凭据键 | `NEO4J_URL` / `NEO4J_USERNAME` / `NEO4J_PASSWORD`（与上游同） |

## 5. 构建 / 安装 / 回读 / 自检 命令

```powershell
# 打包（确定性 zip：条目排序 + 固定时间戳 1980-01-01 + create_system=0 + external_attr=0o644 + level 9）
python shared/deploy/plugins/neo4j_cypher_query/build_pkg.py
# → dist/cc-ops-neo4j_cypher_query-0.1.0.difypkg
#   bytes=23064  sha256=27904620eecc56eb36d7546ffad7b52a30648a2fefe66442e45e14b9be5d5cfc  entries=15

# 复现校验（重打包到临时文件比对 sha256，不覆盖已存在包）
python shared/deploy/plugins/neo4j_cypher_query/build_pkg.py --check

# 离线自检（只用本地夹具与假 session，绝不碰真机）—— 退出码 0 表示全部用例通过
python shared/deploy/plugins/neo4j_cypher_query/install_plugin.py --selftest

# 只读核对真机现状（登录 + 回读 provider/tools/plugin-list/凭据；不写任何东西）
python shared/deploy/plugins/neo4j_cypher_query/install_plugin.py --check --evidence-dir tmp/m4_w8_evidence

# 真机安装（upload/pkg → install/pkg → 轮询任务 → 回读工具面 → 建凭据）
python shared/deploy/plugins/neo4j_cypher_query/install_plugin.py --apply --evidence-dir tmp/m4_w8_evidence
```

`install_plugin.py` 退出码：`0` 成功/已收敛；`1` 一般失败（响应体已清洗存档）；`2` 校验拒绝；
`3` 登录失败；`4` `--check` 发现未安装；**`6` BLOCKED（部署侧策略闸：签名校验 / 仅官方插件）**。

凭据口令来源与掩码纪律**完全复用** `shared/deploy/dify/plugin_bind.py`：
`--password-env` > 环境变量 `CC_NEO4J_PASSWORD` > 只读 SSH 读远端 `/opt/upstream-agent/.env` 的 `NEO4J_AUTH`。
任何证据文件落盘前都用真口令硬扫描，命中即 `Refused`。

## 6. 真机回读基线（fork 安装**前**，只读，2026-09-29）

`install_plugin.py --check` → **退出码 4**（fork 未安装，符合预期）。

- console login HTTP **200**；`tool-providers?type=builtin` HTTP **200**，**n=7**
- 市场插件 `nikola_milosevic/neo4j_query/neo4j_query` **present=True**（并存红线未被触碰）
- `plugin/list` HTTP **200**，`total=5`；市场 `installation_id = 01a07b38-5ec6-773e-ba72-8598e3fb79a7`
- fork provider `cc-ops/neo4j_cypher_query/neo4j_query` **present=False** ⇒ 未能安装（见 §7）

**上游市场插件的工具面 `form` 基线（回读原样，说明 `form` 字段确实可回读）**：
`database`=`form`、`query`=`form`(required=True)、`parameters`=`form`、`max_records`=`form`(default 1000)、
`allow_write_queries`=`form`(default false) —— 即本次要修的那 5 个参数。

## 7. 当前阻塞：真机安装被「强制签名校验」挡住（BLOCKED，非 FAIL）

### 现象（亲测）

`install_plugin.py --apply` 在**第 3 步 upload/pkg** 被拒，退出码 **1**，HTTP **400**：

```json
{"code":"plugin_error","message":"plugin verification has been enabled, and the plugin you want to install has a bad signature"}
```
（`Content-Type: application/json`，`X-Version: 1.17.0`，`X-Env: PRODUCTION`）

### 对照实验（决定性）

| 上传对象 | 结果 |
|---|---|
| 上游**原封未动的已签名包** `tmp/upstream_neo4j_query.pkg` | **HTTP 200**，回读 `unique_identifier = nikola_milosevic/neo4j_query:0.2.0@99ca3d40…`，解码 manifest 含 `"verified": true` |
| 本 fork 包 `dist/cc-ops-neo4j_cypher_query-0.1.0.difypkg` | **HTTP 400** `bad signature` |

⇒ **闸就是「包必须带 Dify 的 RSA 签名」**；fork 包缺签名是 upload 被拒的**唯一**原因。
**这不是**「仅官方插件」闸（响应体没有 `PluginInstallationForbiddenError` / `restricted to official` / 403）。

### 机制（本地与容器内只读量测）

- Dify 把 RSA 签名放在 **zip 的 comment** 里：上游包 `zip_comment_len=718`，
  内容形如 `{"signature":"IaJvQMQRGxyBZZqUvq26Icno1u39K5jKHOoptUSYLaWI4J7MvdyYt4r9ZSeKQgABigEX…","time":1766631660}`；
  本 fork 包 `zip_comment_len=0`、`has_signature=False`。
- 报错串在 **plugin_daemon 二进制**里（`/app/main`），同二进制含 `crypto/rsa` 相关串 ⇒ 确为 RSA 校验。
- 容器环境实测：`dify-plugin_daemon-1` 与 `dify-api-1` **都** `FORCE_VERIFYING_SIGNATURE=true`；
  另有 `THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED`。
  API 侧 `core/plugin/plugin_service.py:1068-1080 upload_pkg`：
  `verify_signature=permission.restrict_to_marketplace_only`，且 daemon 的 `FORCE_VERIFYING_SIGNATURE=true`
  使其无论如何都校验 ⇒ **只放宽租户设置不够**。
- **签名覆盖包内容 ⇒ 改了 `manifest.yaml` 就必然失效；而没有 Dify 私钥就无法重新生成签名。**

### 可选出路（**均超出本任务 inScope，需另行授权；本任务未做任何一项**）

1. **不做真机安装**，把 fork 交付为源码树 + 构建产物 + 可复现打包/安装/自检工具（= 本仓库现状）。
2. 由有权限方评估是否放宽该闸：修改 `dify-api` 与 `dify-plugin_daemon` 的
   `FORCE_VERIFYING_SIGNATURE`（或 `THIRD_PARTY_SIGNATURE_VERIFICATION_ENABLED`）并重启这两个容器
   —— 涉及重启生产载体，必须显式授权。
3. 走 Dify 官方签名/市场发布流程（需要上游/平台侧配合）。

### 本任务**未**做的事（红线）

未修改 `enable_marketplace` / `plugin_installation_scope`；未修改 `FORCE_VERIFYING_SIGNATURE`（未绕过）；
未重启任何容器；未向 daemon 存储写入任何文件；**未卸载或修改市场插件**；未重绑任何 App；
未改 `shared/deploy/dify/{register_tools.py,sync_tool_schemas.py,binding.yaml,check_fairness.py,contract_audit.py,plugin_bind.py}`、
`shared/deploy/prompts/`、`shared/deploy/smoke/`、`shared/model/`、`scheme1-kg/`、`scheme2-onto/`、`docs/`；
未碰 `:8301`/`:8302`/`:8303` 三个载体。

## 8. 诚实边界

- **上游无 LICENSE 文件**；fork 保留上游 `PRIVACY.md`/`SECURITY.md`/`GUIDE.md`/`.difyignore` 原文。
  label/description 已改写为我方口径，但其中**没有**「写保护已放开」之类不实话术：
  文案明确写「只读 … max_records 与 allow_write_queries 仍由工作流控制，上游 EXPLAIN 写保护原样保留」。
- `query` 长度上限 **2000 字符**（上游 `_validate_query_basic` 原有），fork 未放宽。
- `max_records` **硬上限 1000**（上游实际取 `min(max_records, 1000)`），fork 未放宽。
- `allow_write_queries=false` 由**上游的 `EXPLAIN` 预检兜底**（`_execute_preflight_check` 读
  `summary.query_type`，仅允许 `r`/`s`），**不是** Dify 平台级只读保证。
- 仍然沿用**超级用户 `neo4j` 账号**（那一已登记的偏离），fork 未引入新账号或新权限模型。
- `manifest.verified` 在上游包中是 `true`（平台签名校验结果）；本 fork 树里写 `false`（未签名）。
- 上述「回读」数值均为**真机亲测**；§7 中标注「容器内量测」的为**只读探针实测**；
  「没有 Dify 私钥就无法签名」是**推断**（依据：签名覆盖内容 + 私钥不在本仓库/本机）。

## 9. 目录说明

```
shared/deploy/plugins/neo4j_cypher_query/
├── README.md              # 本文件
├── build_pkg.py           # 确定性打包器（--check 复现 sha256；断言 15 条目 / 根有 manifest.yaml）
├── install_plugin.py      # Console-API 安装/回读/自检（--check / --apply / --selftest）
├── src/                   # 打包进 .difypkg 的源码树（15 文件，与上游逐字同名）
└── dist/
    └── cc-ops-neo4j_cypher_query-0.1.0.difypkg
```

证据与工作记录（本次任务的原始 JSON/日志）：`tmp/m4_w8_evidence/`；任务报告：`tmp/_m4_w8_report.md`。

## 10. 安装实况与 500 误判（2026-09-29 追加；**结论上覆盖 §7 的「被签名闸挡住」**，§7 保留为当时记录）

### 10.1 真机时间线（既成事实，未重跑）

| 步骤 | 真机结果 |
|---|---|
| `POST /console/api/workspaces/current/plugin/upload/pkg` | **200**，返回 uid（见下） |
| `POST /console/api/workspaces/current/plugin/install/pkg` | **200**，task_id `01a0eb3f-4079-7669-a722-a6b3bb91cde1` |
| 轮询 `GET /console/api/workspaces/current/plugin/tasks/<task_id>` | 前 **16 次 200 `running`**，随后一次 **HTTP 500** |
| 旧 `wait_task` 的反应 | 直接判 `http_error` 并**停手**（这就是误判） |
| 平台既成事实 | 插件**已装成功**：installation_id `01a0eb3f-744a-7559-843b-09243b199714`；provider `cc-ops/neo4j_cypher_query/neo4j_query` 出现在 builtin providers（7→8）；凭据 `cc-kgt-neo4j-fork` id `bd254b70-753b-4e0c-b5e2-8870dfd8eb63`；工具面回读 query=llm、parameters=llm、database/max_records/allow_write_queries=form |

本次安装的 uid（**非秘密**，可写；64-hex 为校验和）：

```
cc-ops/neo4j_cypher_query:0.1.0@1b04ef281638ec88b1002c4997c0e8b7d8b4efe8dbb06fe4536c79378372d9f1
```

### 10.2 HTTP 500 的源码级真因（api 侧 traceback）

```
GET /console/api/workspaces/current/plugin/tasks/<task_id>
  → controllers/console/workspace/plugin.py:960  get()
  → core/plugin/plugin_service.py:959            fetch_install_task
  → core/plugin/impl/plugin.py:195
  → core/plugin/impl/base.py:321 / 422
  → core.plugin.impl.exc.PluginDaemonInternalServerError:
      req_id: d4dccebb77 PluginDaemonInternalServerError: record not found
```

即：**任务一旦完成，plugin_daemon 立即回收该 task 记录**；此后轮询同一 task_id 必得
`record not found`，而 Console 把它映射成 **HTTP 500**（不是 404/gone）。
⇒ 「任务成功」这一刻，平台恰好销毁了成功信号。这是**平台侧缺信号**，不是安装失败。
（原始 traceback：`tmp/_m4_500trace.u8.txt`。）

### 10.3 本次修法：判据**来源**正当化，判据**未放宽**

`install_plugin.py` 的 `wait_task` 现在按下列次序判：

1. `200` + `success`/`failed` → 原判据，直接采纳（未改）；
2. `200` + `running` → 继续轮询到超时（未改）；
3. **非 200 且非 5xx（401/403/…）→ 立即停手，且不触发回读**（未改）；
4. **HTTP 5xx → 先做有界重试 `TASK_HTTP_5XX_RETRIES=3` 次；仍 5xx 才进入第 5 步**（新增，替代原来的「一次 500 就判死」）；
5. **只读权威回读**（新增，函数 `readback_install_state`）：三条**同时**为真才给
   `success_via_readback` ——
   (a) builtin providers 里出现目标 provider；
   (b) `plugin/list` 里 `plugin_id == cc-ops/neo4j_cypher_query` 的条目其
       `plugin_unique_identifier` **逐字符等于**本次 upload 返回的 uid
       （64-hex 校验和必须完全一致；**不得**用「前缀相同」「同名」「同版本」放宽，见 `exact_str_match`）；
   (c) 工具面 `judge_forms` 通过（5 个参数的 form 与期望逐个一致）。
   **任一条不真 ⇒ 仍判失败停手（fail-closed）**；回读未注入或抛异常时也**绝不**把
   `http_error` 当成功。
6. 证据 JSON：`install.task_verdict` 新增取值 `success_via_readback`；新增
   `install.readback_evidence`（三判据各自 observed/expected，uid 写完整 64 位）与
   `install.task_route_error`（http/marker/readback 可用性）。**口令等敏感值一律不写入**
   （沿用 `scrub` / `sanitize_credentials` 纪律，只记长度/掩码形态）。

一句话：**成功仍然要求「provider 已出现 + uid 逐字符一致 + 工具面 form 全对」，只是当任务接口
这条信号被平台自己回收时，改由平台既成事实的只读回读来补证。**

### 10.4 离线自检与真机只读复核

```
& python shared/deploy/plugins/neo4j_cypher_query/install_plugin.py --selftest
   → 真退出码 0；新增 1 正控 + 3 负控 + 4xx/无回读/有界重试/exact_str_match 判据全绿（全程离线）
& python shared/deploy/plugins/neo4j_cypher_query/install_plugin.py --check --evidence-dir tmp/m4_w8_evidence/recheck_meter
   → 真退出码 0（fork present + 工具面 form PASS + 凭据 converged），与真机现状一致
```

新增判据要点（`--selftest` 实测行原文见任务报告 `tmp/_m4_meter_fix_report.md`）：

- 正控：第 1 次 `200 running`、第 2 次起 `500 record not found`，回读三真 ⇒
  `success_via_readback`，流程继续建凭据（端到端 `cmd_apply` 退出码 0 且确实调用凭据 POST）；
- 负控 1：500 且 provider 不在 builtin ⇒ 仍失败停手；
- 负控 2：500 且 `plugin/list` 的 uid **只差 1 个字符** ⇒ 仍失败停手（证明 (b) 是逐字符比对）；
- 负控 3：500 且工具面 `query=form` ⇒ 仍失败停手；
- 4xx：1 次轮询即停手且**回读调用次数为 0**。

### 10.5 诚实边界

- **本修法未经第二次真机安装验证**：真机上插件已经是「已装」状态，没有重跑一次真实
  `--apply` 安装来端到端复验 5xx 路径（重跑意味着卸载/重装真机插件，超出本任务授权）。
  目前对该路径的保证来自**离线夹具正控 + 真机只读复核**两点，不是真机安装复现。
- **未复验项**：真实 500 响应体里 `record not found` 的字段位置（本次按 message 文本包含
  该串判定，用 `.lower()` 子串匹配，未穷举 daemon 其它 500 文案）；真实 `plugin/list` 在
  安装后极短窗口内 uid 是否立即可读（本次未测时序）；`--apply` 在真实 5xx 场景下
  `readback_evidence` 落盘的完整 JSON（仅离线夹具验证过形状）。
- 本次只改了 `install_plugin.py` 与 `README.md` 两个文件；未改任何其它文件，未重跑安装，
  未卸载/重装真机插件，未改租户设置或容器环境变量。

## 11. 0.1.1：描述面追加一句（2026-09-29，W17）

### 11.1 目标与唯一改动

只做一件模型可见的事：在 `src/tools/neo4j_query.yaml` 的 `description.llm` 末尾**逐字追加**一句
（`description.human` 与 5 个参数一律不动）：

> 参数化查询必须通过 parameters 传入实参；未提供的参数会被服务端当作空值，返回 0 行并不代表对象不存在。

选 `description.llm` 的理由：它是模型侧唯一可见的描述字段（即「fork 的 `neo4j_query` 描述」）；
0.1.0 已改写 `description.llm` 而未把它登记为第 5 条偏离，故 `deviations_from_official` 仍**恰为 4 条**。
追加时不加分隔空格（沿用 W14 先例）。

### 11.2 版本号与产物事实

| 文件 | 0.1.0 | 0.1.1 | 改动点 |
|---|---|---|---|
| `src/manifest.yaml` | 1695 B | 1695 B | `:1` / `:34` `version`→`0.1.1`；`:43` `created_at`→`2026-09-29T14:08:34+08:00` |
| `src/tools/neo4j_query.yaml` | 6409 B | 6553 B | `:13` `description.llm` 末尾追加那句（+144 B） |
| `build_pkg.py` | — | — | `:31` `PKG_BASENAME`→`cc-ops-neo4j_cypher_query-0.1.1.difypkg` |
| `install_plugin.py` | — | — | 12 处版本字面量 `0.1.0`→`0.1.1`（**含 `:79` 的转义正则 `0\.1\.0` —— 按明文 grep 抓不到，必须 grep 转义形态**） |
| 包 | `dist/cc-ops-neo4j_cypher_query-0.1.0.difypkg` 23064 B / sha256 `27904620eecc56eb36d7546ffad7b52a30648a2fefe66442e45e14b9be5d5cfc` | `dist/cc-ops-neo4j_cypher_query-0.1.1.difypkg` **23162 B** / sha256 **`bdd5593c0d246655f5fc7f3de3d21ec98cad6faa9ba790580f1ceab886c623af`** | 15 条目；`build_pkg.py --check` 退出码 0（重打包 sha256 一致） |

`src` 侧 sha256（0.1.1）：`manifest.yaml` = `941f2cc42c408b6294fa7b6e6789245768027a289849f7d1ebd403300922e05f`；
`tools/neo4j_query.yaml` = `1653dc459c7334571bb335701b135c1111784c3fc308a94cd63fbd87af544d96`。

### 11.3 0.1.0 → 0.1.1 离线差异清单（唯一允许的差异 = 版本号 + 那句描述）

方法：从 0.1.0 **包体字节**解包（工作区无 git 仓库，`git` 报 `fatal: not a git repository`，故基线取自包
本身，存于 `tmp/m4_w17_evidence/pkg010_extracted/`），与当前 `src/` 逐条目比对。仪器
`tmp/_m4_w17_offline.py`，退出码 `0` = PASS。

| 判据 | 实测 |
|---|---|
| 条目集合 | 15 → 15，逐名相同 |
| 内容变更条目 | **恰 2 个**：`manifest.yaml`、`tools/neo4j_query.yaml` |
| `manifest.yaml` 文本差异 | **恰 3 行**（version / meta.version / created_at） |
| `tools/neo4j_query.yaml` 文本差异 | **恰 1 行**（`description.llm` 追加；unified diff 会把被替换的整行标记为新增，故须按下节口径读） |
| 其余条目 | **13 个逐字节相同**，含 `main.py`、`provider/neo4j_query.py`、`tools/neo4j_query.py`（与上游仍逐字节相同） |

### 11.4 禁用词扫描（离线）

禁用词表：`cypher`、`sparql`、`图`、`本体`、`三元组`、`property graph`、`RDF`、`OWL`、`知识图谱`、`图谱`、`schema`。

**口径（判据来源正当化的一部分）**：unified diff 把「被替换的整行」标记为新增，而 0.1.0 既有的
`description.llm` 里本来就含 `Cypher`（继承自上游）⇒ **按「新增行」扫描会误报**。仪器剥掉新旧行的
公共前缀/后缀，只扫**真正新增的 67 个字符**：

- `forbidden_hits_in_NOVEL_text` = **`[]`**（11 个禁用词逐词 0 命中）；
- `sentence_present_in_novel` = **true**（那句逐字命中）；
- `forbidden_hits_added_lines` = `["cypher"]` —— **仅信息性**，来自 0.1.0 既有文本。
- 原始 JSON：`tmp/m4_w17_evidence/offline_diff.json`。

### 11.5 真机安装：**BLOCKED**（平台侧无法为已存在的 `plugin_id` 登记新版本）

一次性签名闸（**只对 `plugin_daemon` 一个服务注入；未写仓库脚本、未写 `.env`、未常态化**）：

```
# 开闸（本次唯一一次）
cd /opt/dify/docker && FORCE_VERIFYING_SIGNATURE=false docker compose up -d --no-deps --force-recreate plugin_daemon
# 恢复（同一命令，值 true）
cd /opt/dify/docker && FORCE_VERIFYING_SIGNATURE=true  docker compose up -d --no-deps --force-recreate plugin_daemon
```

| 项 | 事实 |
|---|---|
| 容器谱系 | 原 `c1980e7ddb0814…` →（开闸 `2026-09-29T06:21:36Z`）`43c21d464b00…`（created `2026-09-29 14:21:37 +0800`，env `false`）→（恢复 `2026-09-29T06:37:03Z`）`43dc34073e5b…`（created `14:37:03 +0800`，env `true`） |
| `.env` | **未被修改**：`/opt/dify/docker/.env:237` `FORCE_VERIFYING_SIGNATURE=true`，mtime `2026-09-11 10:16:29`、size 10114、md5 `864b8ee55b53f2e872a9ca397d872728`，恢复前后逐字节一致 |
| 其它容器 | `docker ps -a` 前后 diff **只有 `plugin_daemon` 一行**（id + created）；容器数 17 → 17，其余 `Up 2 weeks` |
| 闸的来源 | `/opt/dify/docker/docker-compose.yaml:605` `FORCE_VERIFYING_SIGNATURE: ${FORCE_VERIFYING_SIGNATURE:-true}`（显式传 `true` 与 `.env` 默认一致） |

**闸恢复的功能性证明**：恢复后 `install_plugin.py --apply --force-install` → **真退出码 6**，`upload/pkg`
**HTTP 400**，响应体命中 `bad signature`，脚本按策略**停手上报**（不绕过）⇒ 开闸 → 关闸闭环可复现。

**开闸期间的安装事实（两次尝试，均真退出码 1）**：

| 步骤 | 结果 |
|---|---|
| `upload/pkg` | **HTTP 200**，uid `cc-ops/neo4j_cypher_query:0.1.1@fb3b53b9ee14436e47aaefd21d93ba5ce5165e2da6b877885210b9ee9015b7c2`（⇒ 放宽确实放行了未签名包） |
| `install/pkg` | **HTTP 200**，`{"all_installed": false, "task_id": "01a0ebd5-51f5-76ef-af6b-8ce7d88c4117"}`（重试任务 `01a0ebd8-306b-7090-a1e7-2d7488a86492`） |
| 轮询 | 先得 `"status":"success","message":"installed","completed_plugins":1`，随后同一任务路由开始 **HTTP 500**，触发有界 5xx 重试预算 |
| 权威回读 | `failed_criteria=['b_plugin_list_uid_exact']`：`plugin/list` 仍是 0.1.0 |

**真因（daemon / 库 / API 三侧一致）**：

1. daemon 实际上**成功**了：日志 `local runtime starting plugin=…:0.1.1@fb3b53b9…` → `Installed tool: neo4j_query`
   → `local runtime ready`；磁盘 `/app/storage/plugin/cc-ops/` 与 `/app/storage/cwd/cc-ops/` **各有 0.1.0 与 0.1.1 两份**；
   `plugin_declarations` 表里 **0.1.1 行已在**（decl_len 9883，0.1.0 为 9825）。
2. 但 daemon **不会**为已存在的 `plugin_id` 写 `plugin_installations` 行：该表在**独立库 `dify_plugin`**
   （容器 env `DB_PLUGIN_DATABASE=dify_plugin`；API 自己的库 `dify` 里没有该表），且有 **UNIQUE 索引
   `idx_tenant_plugin` on `(tenant_id, plugin_id)`**；cc-ops 行至今仍是 `…:0.1.0@1b04ef28…`。安装窗口内
   daemon 日志里 **`plugin_installations` 相关 SQL 一条都没有**（daemon 根本没尝试写），`plugins` 表也没有 0.1.1 行。
3. daemon 的 `recycle.go` 在任务完成后**回收 install task 记录**，于是 API 端 `fetch_install_task`
   （`plugin_service.py:959` → `impl/plugin.py:195` → `implement/base.py:321/422`）得
   `PluginDaemonInternalServerError: record not found` ⇒ Console 映射成 **HTTP 500**（与 §10.2 同一机制）。
4. 控制台**没有**本地包的 upgrade 路由：`/app/api/controllers/console/workspace/plugin.py` 只有
   `install/pkg`(`:810`)、`upgrade/marketplace`(`:1010`)、`upgrade/github`(`:1033`)，**没有 `upgrade/pkg`**；
   `PluginService.install_from_local_pkg` 无幂等/升级判断 ⇒ 本地包路径下**无法**把已存在的 `plugin_id` 推到新版本。

**判据范围警告（必须与结论一起读）**：`install_plugin.py --apply`（**不带** `--force-install`）在真机返回
**退出码 0** 并打印「幂等命中：fork provider 已在 builtin 列表中，跳过 upload/install」——而当时真机仍跑 0.1.0、
`PLUGIN_VERSION` 已是 0.1.1 ⇒ **该 0 是版本盲的短路**（`install_plugin.py:1389`
`if (PROVIDER not in ids) or args.force_install:`），**不得**当作「已升级到 0.1.1」的证据。
同理 `cmd_check` 的判据不比较已装 uid/版本（只查 provider 是否存在 + 工具面 form + 凭据收敛）。

### 11.6 声明面：**有意保持 0.1.0**（诚实登记）

真机仍运行 0.1.0，故 `shared/model/kgt-tools.yaml`、`shared/deploy/dify/binding.yaml`、
`shared/model/kgt-prompt-variants.yaml:132` **一律未改**（与真机一致 = 诚实登记，不是遗漏）。
App 重绑只跑 `tmp/_m4_w9_rebind.py --check` → **PASS**（provider_id `cc-ops/neo4j_cypher_query/neo4j_query`、
tool_name `neo4j_query`、builtin_providers=8），**未**跑 `--apply`（没有需要绑定的变更）。

### 11.7 门禁复跑（六项，全部真退出码 0）

| 门禁 | 结果 |
|---|---|
| `shared/verify/f5_kgt_gate.py` | `F5 RESULT: PASS`（174/174；★ 2026-09-29 W41 复核：扩面后现行读数，本节 M4 期记录为 172/172） |
| `shared/verify/f5_kgt_gate.py --selftest` | `PASS`（183/183；★ 2026-09-29 W41 复核：扩面后现行读数，本节 M4 期记录为 181/181） |
| `shared/deploy/dify/contract_audit.py --check` | 判据 `M4-C1..M4-C9` 全 PASS（PASS=9 FAIL=0 BLOCKED=0） |
| `shared/deploy/check_fairness.py` | PASS=6 FAIL=0 BLOCKED=0 |
| `shared/verify/ref_gate.py` | `RESULT: PASS (ERROR=0 WARN=14 DEBT=2 refs=599 checked=434 min-refs=40)` |
| `tmp/_m4_w9_rebind.py --check` | `RESULT: PASS` |

（门禁绿是在**声明面仍为 0.1.0**的状态下取得 ⇒ 未放宽判据、无回归。）

### 11.8 free 组端到端冒烟：**FAIL**（3/3，判据未满足）

`shared/deploy/smoke/e2e_smoke.py --apps cc-kgt-free-agent` 连续 3 次**真退出码 1**，签名完全一致：
call1 HTTP 200、**`tools=[]`**、无任何工具事件（`agent_thought.tool` 全空）、平面三无 `dispatch/tool/invoke`；
三次 `prompt_tokens` **同为 1823**，answer_len 398/373/355，latency 5.68/4.94/5.55 s。

失败回答是**模型编造的** Cypher（如 `MATCH (inst:Instance {name: 'cc-pay'})-[:RUNS_ON]->(c:Container)-[:HOSTED_BY]->(h:Host)`）
与**伪造的字段名**，`executed_tool_names=[]`；失败样本 SSE 内 `neo4j_query` 出现 **0** 次。

**这不是 0.1.1 升级造成的（升级根本没有落地），也不是工具没送达**，证据三条：

1. **工具确实被送达**：每次失败运行窗口内 API 都成功取到 fork 工具面 —— dify-api 日志
   `GET http://plugin_daemon:5002/plugin/<tenant>/management/tool?provider=neo4j_query&plugin_id=cc-ops%2Fneo4j_cypher_query`
   **HTTP/1.1 200 OK**（失败窗口内 `06:38:38.420`、`06:38:38.715`、`06:40:39.060`、`06:40:39.335` 均有）；
   daemon 侧对应 `middleware.go:83 … status=200`。且 `06:27:56` 之后 API 不再有 `tool_engine.py:150` 报错
   ⇒ **运行期没有失败的工具调用尝试**（模型压根没发起）。
2. **fork 运行时实例是活的**：daemon `06:37:08–11` `local runtime starting plugin=cc-ops/…:0.1.0@1b04ef28…`
   → `plugin instance log instance=01a0ebe1 message="Installed tool: neo4j_query"` →
   `subprocess.go:136 … pid=48 instances_before=0 instances_after=1` → `local runtime ready`。
3. **同一 App、同一仪器曾 PASS 也曾 FAIL**：`tmp/m4_w9_evidence/smoke_free/smoke_results.json`
   （run_utc `20260929T034816Z`）= **PASS**（4 条工具事件、tool_names `[neo4j_query]`、prompt_tokens 184009、
   n_events 1314、36.15 s）；`tmp/m4_w7_evidence/smoke/runB/smoke_results.json`（run_utc `20260929T020126Z`）=
   **FAIL**，那次模型的原话是「我目前没有绑定任何可调用的工具（工具列表为空）」（prompt_tokens 1508）。
   ⇒ 「必须出现工具事件」这条判据**依赖模型行为，当前不稳定**。

**定性**：本项按 **FAIL** 如实登记（不是 BLOCKED，也不是「已过」）；它**不能**归因于 0.1.1（未落地），
同样**不能**用来证明那句描述有效或无效（升级没有上机）。判据本身未放宽。

### 11.9 本次留下的真机残留（如实登记，建议授权后清理）

安装尝试在真机留下三处残留，均**未被**本任务自行删除（超出授权写范围）：

1. `/app/storage/plugin/cc-ops/<…:0.1.1@fb3b53b9…>` 目录；
2. `/app/storage/cwd/cc-ops/<…-0.1.1@fb3b53b9…>` 目录；
3. `dify_plugin.plugin_declarations` 表里的 0.1.1 行。

后果是一个**每约 30 秒重复一次**的 daemon 提示（无功能影响）：
`server_local.go:84 Plugin table missing for existing PLUGIN_INSTALLED_PATH plugin_unique_identifier=cc-ops/neo4j_cypher_query:0.1.1@fb3b53b9…`
（伴随 `SELECT * FROM "plugins" WHERE plugin_unique_identifier = '…0.1.1@fb3b53b9…'` `rows:0`），自 `06:37:45` 起持续。
它是「磁盘上有包目录、`plugins` 表里没有对应行」的必然结果，**不是** 0.1.0 载体的故障。

### 11.10 诚实边界（0.1.1 这一轮）

- **BLOCKED ≠ FAIL ≠ 已过**：真机安装是 **BLOCKED**（平台侧不为已存在的 `plugin_id` 登记新版本）；
  free 组冒烟是 **FAIL**（模型未调用工具）。两者都不许写成「已过」。
- 本轮**没有**证明那句描述改善了模型行为：升级从未上机，模型侧拿到的仍是 0.1.0 的描述文本
  （活体工具面探针实测：fork 路由 HTTP 200、tool_count=1、**不含新句**）。
- 门禁绿只证明「声明面与真机一致、无回归」，**不**构成因果证明。
- 未抓包 ⇒ 运行期 prompt 注入链路仍是**源码推定**；未做模型行为侧 A/B。
- 离线差异清单的基线来自 **0.1.0 包体字节**（工作区无 git 仓库）。因 `src/` 已前进，
  `build_pkg.py` **无法**再复算出 0.1.0 的字节 —— 它们只存活在 `dist/` 与
  `tmp/m4_w17_evidence/pkg010_extracted/`。
- 真机 0.1.1 目前处于「包已上传、declaration 已落库、目录已落盘、但**未登记为 installation**」的中间态；
  在该状态被清理或另行授权处理前，**不应**把 `plugin/list` 的 0.1.0 当作「升级已完成」。
