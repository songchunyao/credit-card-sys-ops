# M4.2 / W1 · Dify 工具注册与同步

本目录是 **M4.2（Dify 工具注册与同步）W1 批次**的施工面：把 `shared/model/` 里两个工具契约
（Set B 七工具、方案3 KG-T 四工具）**如实登记**到 Dify 的 API tool provider 上，并给出
**可复跑的零漂移取证**。

单一事实源是 [binding.yaml](binding.yaml)；两个脚本只读它 + 两个模型契约文件，不另设隐含约定。

## 1. 文件

| 文件 | 作用 |
|---|---|
| [binding.yaml](binding.yaml) | 单一事实源：workspace / tool_sources / carriers / providers / apps / fairness / registration_face / whitelist / counts。provider 的 `base`、工具名单、白名单端口、禁用名、文档字段剥离清单全在这里。 |
| [common.py](common.py) | Console API 公共层：登录、读、写、`sha256`/`stable_json`/`file_facts` 等规范化工具。**不含任何凭据字样**，凭据只从环境变量或 `.env` 读。 |
| [register_tools.py](register_tools.py) | 注册器 + 比对器。`--check`（默认）/ `--apply` / `--selftest` / `--snapshot-list` / `--baseline`。 |
| [sync_tool_schemas.py](sync_tool_schemas.py) | **独立的第三方比对器**（不 import 注册器，另写一套投影，改用扁平元组口径），只读，无写路径。 |

## 2. 用法

```powershell
$env:PYTHONIOENCODING = "utf-8"          # 必需：脚本输出含中文与 ⇒ 等字符
$D = "<repo>\shared/deploy/dify"

python "$D\register_tools.py" --selftest      # 离线，25 例，含 16 条负控
python "$D\sync_tool_schemas.py" --selftest   # 离线，10 例
python "$D\register_tools.py" --snapshot-list <out.json>   # 取名单基线
python "$D\register_tools.py" --apply                     # 只增不删
python "$D\register_tools.py" --check --baseline <out.json>  # 必须 exit 0
python "$D\sync_tool_schemas.py" --check                     # 必须 exit 0
```

退出码：`0` = 零漂移；`1` = 有漂移或契约非法（逐条打印）；`2` = 登录/传输等环境错误。

凭据来源（**值不落盘**）：`$M4_DIFY_ENV_FILE` → `<repo>/.env` →
`<repo>/../upstream-ops-agent/.env` → 本目录 `.env`，取 `DIFY_BASE_URL` /
`DIFY_EMAIL` / `DIFY_PASSWORD`（`console/api` 为默认前缀）。

## 3. 判据（写死在代码里，**只改声明不改判据**）

`register_tools.py`：

* **leg A** — 声明 schema 与 Dify **原样存回的 `schema` 文本**逐字比对，**不做任何类型映射**。
* **leg B** — live `tools[]` 的**模型面**（`tool_name` / `description` / 参数 `name,type,required` + `llm_description`）
  与声明模型面对比；对比前对**声明侧**套 `registration_face.type_alias`。
* **leg D** — `cc-kgt-tools` 的注册面不得出现任何 `denied_*` / 禁用工具名。
* 孪生 — `cc-kg-tools` 与 `cc-onto-tools` 的 schema（把 `servers[0].url` 归一为 `<<BASE>>` 后）规范化 sha256 必须**相等**。
* 基线 diff — 与基线名单相比只允许**新增**；出现删除或 id 变更即失败。

`sync_tool_schemas.py`：S1 provider 缺失 / S2 工具集合不符 / S3 逐工具元组或 path 或 operationId 不符 /
**S4 `tools[].server_url == providers[].base + path`（注册器两条腿都不覆盖的独立不变量）** /
S5 存回的 schema 里残留文档字段 / S6 denied 名进注册面 / S7 孪生模型面 sha256 不相等。

### `type_alias` 是有损的，且已被补偿

Dify 会把自己没有的 JSON-Schema 参数类型折叠：`integer → number`、`object → string`
（真机证据：`get_fault_topology.max_hops`、`cc_trace.maxHops`、`get_facts.timeWindow`）。
因此 **leg B 的 `type` 列无法区分声明的 `object` 与 `string`**。

补偿：**leg A 不做任何映射**，Dify 原样存回的 schema 里 `timeWindow` 就是**声明值本身**
（★ R-54 后为 `{"type": "string"}`；R-54 之前是 `{"type": "object"}`），
所以 yaml 里 `object ↔ string` 之间的任何真实改动仍会在 leg A 被抓住 —— 自检用例 **N16**
专门断言「篡改后 leg A 命中、leg B 不命中」，把这个补偿关系钉住。
★ R-54（2026-10-07）后该控制的方向**已翻转**为 `string` → `object`（原因：声明侧权威类型
本身变成了 `string`；别名折叠 `object`→`string` 的性质不变，故证明力不变）。

## 4. 红线（脚本已内建，不是靠自觉）

* **只增不删**：`--apply` 只有 `add` / `update` / `NOOP` 三条路径，**没有 delete/rename**；更新必带
  `provider` + `original_provider` + `provider_id` 三件套（缺一 Dify 返回 422，自检 N01 覆盖）。
* **白名单**：provider 名、工具名、端口、host 全在 `binding.yaml`；`denied_*` 只在 `denied_scope`
  标明的 provider 上生效（**门面级禁用，不是全局禁用** —— `:8303` 门面的 `FORBIDDEN_PATHS` 里那 6 个
  `cc_*` 名字恰是 `:8301`/`:8302` 的合法工具）。
* **文档字段不入注册面**：`carrier_specific_value_semantics`（kg: Cypher / onto: SPARQL）等 11 个字段
  一律剥离；`cc_query.query` 的模型面描述**只取** `params[].description` 的逐字 `只读知识查询语句`。
  注册面出现这些字段即失败（自检 N06/N09）。KG-T 组按 F5 豁免禁用词扫描（其 `summary` 含「Cypher」属预期）。
* **写后回读**：每次 `add`/`update` 立刻回读并与声明逐字比对，不一致当场 `FAIL`。
* **幂等**：已存在且 schema 与模型面都逐字一致 ⇒ `NOOP` 不写。

## 5. 诚实边界

* `server_url` 取 **`http://192.0.2.10:<port>`**（容器内可达；`host.docker.internal` 与容器内
  `127.0.0.1` 均不可达，已登记在 `binding.yaml` 的 `meta.service_base_evidence`）。
* 未从 `dify-ssrf_proxy-1` 容器内复测（该镜像内无可用 HTTP 客户端）⇒ **该腿未验证**；
  Dify 实际出网腿的可达性以 W2 的真实 App 调用为准。
* `type_alias` 的有损性见 §3；`--check` 能给出的结论止于「Dify 存回与读回的内容与声明一致」，
  **不等于**「模型经 ssrf_proxy 调得通」。
* 本批次只做 provider 注册，**不建 App、不绑工具**（`binding.yaml` 的 `apps` 段**当时**全部标
  `declared_only_not_created_in_w1`）；`neo4j_query` 插件只声明不安装。
* ★ 后续更新（W2，真机创建时点 2026-09-29T00:52:26Z，队长第三路复核 2026-09-29T00:58Z）：
  `cc-kg-agent`（`4b074adc-be8a-4bf9-8c45-4907d5d97d6f`）与 `cc-onto-agent`
  （`d8f67550-3e32-4451-884e-f9e486012fbd`）已真机建成并各绑 7 工具，`binding.yaml` 中这两条的
  `status` 已由 `declared_only_not_created_in_w1` 改为 `created_and_bound_in_w2`（**只在回读校验
  全通过时写**）；`cc-kgt-agent` / `cc-kgt-free-agent` 两条的 uuid 与 status **一字未动**。
  ⇒ 本节第一条的「全部标 …」是对 W1 批次**当时状态**的历史描述，不再是当前文件状态。

---

## ★ FIX-1 更新（零参占位符 `_unused` 从注册面移除）

**本节以上全部内容（含 §1–§5）为历史表述，一字未删。** 凡与本节冲突处，以本节为准。

### 1. 问题
`cc_guide` / `cc_schema` 是两个**真零参**工具，但注册面（提交给 Dify 的 OpenAPI）里被**本项目自己**
注入了一个假参数 `_unused`（`type: string`，描述「本工具无需参数，请传空字符串」）。模型照声明传它，
而两个载体的 body 模型是 `extra=forbid` ⇒ **必然 HTTP 400**：
`INVALID_PARAM` / `validationErrors[0].type == "extra_forbidden"`。

### 2. 被证伪的前提
旧稿（本文件 §3 与 `tools-setb.yaml` 旧注释、`binding.yaml` 旧注释处）写着
「**Dify 硬约束：properties 不能为空**（无参工具注入 `_unused` 占位）」。
**该前提为假**：现网 79 个 API 工具里已有 **6 个真零参工具**（`upstream-ops-onto-consistency/onto_consistency`、
`upstream-ops-onto-symptoms/onto_symptoms`、`upstream-ops-onto-cqs/onto_cqs` 及三个 `-sim` 变体），
其 `parameters == []` 且工作正常。`_unused` **不是 Dify 侧注入**，而是本项目自造。

### 3. 本次改动（只增不删注册面条目）
* `shared/model/tools-setb.yaml`：删除 `cc_guide` / `cc_schema` 两处 `dify_placeholder` 块；
  `source_documents.what` 与 `c3.what_is_not_compared` 里的错误表述改为如实记载。
* `shared/deploy/dify/register_tools.py`：删除 `project_setb` 与 `build_schema` 里的占位符注入分支
  ⇒ 零参工具现产出 `properties: {}` + `required: []`。
* `shared/deploy/dify/sync_tool_schemas.py`：删除 `_placeholder()` 及两处注入。
* `shared/deploy/dify/binding.yaml`：删除 `registration_face.empty_params_placeholder` 键；
  更新 `meta.tool_source_fingerprints` 里 `tools-setb.yaml` 的指纹。
* `whitelist.strip_fields` 中的 `dify_placeholder` **保留未删**（防御性历史条目，实际已无引用）。

### 4. 附带修复（**与占位符移除无关**，如实登记）
* `register_tools.py` 的 N07 / N12 两个负控 fixture 原先**隐式依赖** `cc_guide` 有参数，占位符删除后
  它们分别以 `IndexError` 与「篡改变成 no-op」告败。已改为**不假设「第一个 operation/工具必有参数」**，
  判据语义**未放宽**。
* `register_tools.py` 的 `cmd_apply`：本环境的 `tool-provider/api/get` 实体**不含 `id`**
  （实测顶层键只有 `credentials / custom_disclaimer / description / icon / labels / privacy_policy /
  schema / schema_type / tools` 共 9 个），故 `provider_id` 回退到 `list_api_providers()` 的 `{name: id}`。
  这是**取值来源的补齐**，`assert_update_trio` 仍强制三件套齐全，**未放宽任何判据**。

### 5. 复证结果（2026-09-29T02:03–02:05Z，真机）
* `register_tools.py --apply`：`UPDATE cc-kg-tools` + `UPDATE cc-onto-tools` + `NOOP cc-kgt-tools`，
  三次**写后回读一致**（schema 逐字 + 模型面）；`本次新增：[]` / `本次删除：[]`；live 总数 64 = 64。
* 回读：三 provider 工具数 **7 / 7 / 4**；`cc_guide` / `cc_schema` 两侧 `parameters` 均为 **0 个**；
  KG 与 ONTO 归一化 sha256（`<<BASE>>` 口径）**相等**。
* 真机调用：`POST 8301/guide {}`、`8301/schema {}`、`8302/guide {}`、`8302/schema {}` 全部 **200**；
  防回退控制 `{"bogus_x":1}` 与旧占位符 `{"_unused":""}` 全部 **400 `extra_forbidden`**。
* 详细证据见 `tmp/m4_fix1_report.md` 与 `tmp/m4_fix1_evidence/`。
