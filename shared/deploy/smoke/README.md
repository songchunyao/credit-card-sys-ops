# M4 / W7 · 端到端冒烟（模型真被调用 + 工具真被执行）

本目录是 **M4 期最后一块验收证据（W7）** 的施工面。目标**不是**测准确率，而是回答两个
可以用退出码回答的问题：

1. **模型真被调用了吗？** —— 响应元数据里的模型名 + token 用量 + 非空回答。
2. **工具真被执行了吗？** —— 必须**同时**在 Dify 侧与载体侧两个独立平面留下痕迹。

四个 App 的 uuid、绑定工具集、载体端口**都以真机回读为准**，脚本里写死的只是「去哪儿读」。

## 1. 文件

| 文件 | 作用 |
|---|---|
| [e2e_smoke.py](e2e_smoke.py) | 运行器。`--selftest` / `--dry-run` / 真机三态。三态退出码 `PASS=0 / FAIL=1 / BLOCKED=2`。 |
| （本文件） | 用法、判据、红线与诚实边界。 |

证据不落本目录，落 `tmp/m4_w7_evidence/`（见 §5）。

## 2. 用法

```powershell
$env:PYTHONIOENCODING = "utf-8"          # 必需：输出含中文与 ⇒ 等字符
$S = "<repo>\shared/deploy/smoke"

python "$S\e2e_smoke.py" --selftest              # 离线，13 例，无任何网络请求
python "$S\e2e_smoke.py" --dry-run               # 只打印将执行的调用清单
python "$S\e2e_smoke.py" --apps cc-kgt-agent     # 真机只跑一个 App
python "$S\e2e_smoke.py"                         # 真机跑全部四个 App
```

真机常用开关：`--calls-per-app N`（默认 1）、`--query "<提问原文>"`（默认见 `DEFAULT_QUERY`）、
`--timeout 300`、`--evidence-dir <dir>`、`--no-ssh`。

退出码：`0` = PASS；`1` = FAIL（测到了、是坏的）；`2` = BLOCKED（**没测到**）。
优先级 `BLOCKED(2) > FAIL(1) > PASS(0)`：只要有一项 BLOCKED，整体即 BLOCKED。
缺 Dify 凭据（`DIFY_BASE_URL` / `DIFY_EMAIL` / `DIFY_PASSWORD`）时 **fail-closed**
立即退出 2 并打印 `★ BLOCKED（≠ FAIL）`，不会静默降级成 PASS。

凭据来源（**值不落盘、不打印**）：`<workspace>\upstream-ops-agent\.env` → `<cwd>\.env`。

## 3. 判据（写死在代码里，**只改声明不改判据**）

每次调用逐项记录：App uuid、提问原文、HTTP 状态、`response_mode`、会话/消息 id、回答文本、
模型名与 token 用量、SSE 工具事件（`tool` / `tool_input` / `observation` 摘要）、
以及**该次调用前后对应载体审计链的行数差**。

* **平面 0（模型真被调用）** —— HTTP 200，且响应元数据里的模型名/token 用量 + 非空回答。
  若 token 字段缺失，**如实记录缺失**，不补造。
* **平面 1（Dify 侧工具真被调用）** —— SSE 出现 `agent_thought` 且 `tool` 非空，
  且 `tool` ∈ 该 App **真机回读**的绑定工具集。只有 `tool` 没有 `observation` 的
  「执行前决策」条**不算**（Dify 对每次工具调用发两条 `agent_thought`，只统计带结果的那条）。
* **平面 2（载体侧工具真被执行）** —— 该次调用时间窗内，对应载体（`:8301` / `:8302` / `:8303`）
  的审计链 JSONL **新增 ≥1 行**（`wc -l` 差值，不是 `/health` 的内存环形缓冲计数）。
* **平面 3（仅 `cc-kgt-free-agent`）** —— 插件直连 Neo4j、绕过载体，审计链不适用。
  依次尝试 (a) 主机 `dify-plugin_daemon-1` 该窗口的 `dispatch/tool/invoke`，
  (b) Neo4j 侧读数，(c) 用只读凭据直接查 Neo4j 与回答对齐。三者全灭 ⇒ BLOCKED。

### ★ 平面 2 的判定顺序是 fail-closed 的

**先判「平面取不到」，再判「不需要该平面」**。真实调用路径在 `audit_before` / `audit_after`
读不到时会同时给出 `(audit_delta=None, audit_plane_ok=False)`；若先判 `delta is None`
就会把「取不到」误当「不适用」⇒ **假绿（fail-open）**。自检用例
「阻塞-审计链平面不可读」专门钉住这一点（`expect=BLOCKED`）。

### ★ 平面 3 只认 `dispatch/tool/invoke`

平面 3 的判据是模块级常量 `PLANE3_DISPATCH_MARK = "dispatch/tool/invoke"`，
分类逻辑在纯函数 `plane3_from_logs(dispatch_lines, context_lines, logs_readable)`：

* 日志不可读 ⇒ `blocked=True` ⇒ **BLOCKED**；
* 有 `dispatch/tool/invoke` 行 ⇒ `ok=True`；
* **只有** `management/tool?provider=…` 这类**解析**行 ⇒ `ok=False, blocked=False` ⇒ **FAIL**。

**为什么必须这么严**：实测 `cc-kgt-free-agent` 的窗口里，`02:01:26.203` 有一条
`GET …/management/tool?provider=neo4j_query&plugin_id=…` 返回 200（provider 能解析），
而整个窗口 `dispatch/tool/invoke` 为 **0** —— 工具从未被派发。
早期版本用 `grep -E 'dispatch/tool/invoke|neo4j_query'`，会把上面那条**解析**日志
读成「工具已派发」；只要窗口多宽 1 秒就返回非空 ⇒ **假 PASS**。
自检用例「负控-平面3 只有解析行(非派发)」钉住这一点（`expect=FAIL`）。

## 4. 红线（脚本已内建，不是靠自觉）

* **临时凭据用完即删并回读确认**：每个 App 现建 api-key，`finally` 里 DELETE，
  再 `GET /api-keys` 回读确认列表里已消失（记 `removed_confirmed`）。
* **凭据字面量绝不落盘**：api-key / 口令只写 `len` 与 `sha256[:12]`，连掩码形态都不写。
* **不修改任何 App / provider / binding 配置**：除 Console `login` 与 api-key 建删外全只读。
* **原始响应落盘**：每次调用的原始 SSE 原样写 `{evidence_dir}/{app}.call{n}.sse.txt`，
  便于第三方复算，不依赖脚本自己的解析结论。
* **不把失败包装成通过**：任何一处没拿到证据 ⇒ 进「诚实边界」，判定如实 FAIL 或 BLOCKED。

### 已知的 Dify 侧限制（不是本脚本的缺陷）

`POST /console/api/apps/{uuid}/api-keys` 的签名是 `post(self, session, current_tenant_id, resource_id)`，
**不接受任何请求体** ⇒ **无法给临时 key 起带 UTC 时间戳的名字**。报告里改用返回的 `id`
与创建时刻标识。返回码 **201**。

## 5. 产物

| 路径 | 内容 |
|---|---|
| `tmp/m4_w7_evidence/smoke/<run>/*.call<N>.sse.txt` | 每次调用的**原始 SSE**，逐字 |
| `tmp/m4_w7_evidence/smoke/<run>/smoke_results.json` | 逐调用结构化结果（含 judge 的 reasons/failed/blocked） |
| `tmp/m4_w7_evidence/smoke/<run>.*.out.txt` / `*.err.txt` | stdout / stderr 落盘（用 `Start-Process -RedirectStandardOutput` 取真实退出码） |

## 6. 诚实边界

* **`/health` 的 `auditChain.length` 是内存环形缓冲长度，与磁盘 JSONL 行数不等**
  （实测 212/168/43 vs 215/236/51）。因此它在 `--no-ssh` 降级路径下只作**弱平面**用，
  报告必须标注；正式判定一律用 SSH `wc -l`。
* **载体审计链里的非 `ok` 状态不是载体故障**：`INVALID_PARAM` / `FORBIDDEN_TOKEN` /
  `QUERY_FORM_REJECTED` 等是护栏**正常拒绝**，归因时必须与「载体坏了」区分开。
* **平面 1 无法区分 `cc-kg-agent` 与 `cc-onto-agent`**：两者工具名完全相同
  （`cc_guide,cc_schema,cc_query,cc_facts,cc_trace,cc_cause,cc_propose`），
  只有平面 2 的 `:8301` / `:8302` 审计链能区分「到底打了哪个载体」。
* **`CC_NEO4J_PASSWORD` 缺失时平面 3 的 (b)(c) 两路不可用**，只剩 (a) 插件日志；
  脚本会打印提示，不会静默把它算成通过。
* **响应元数据里没有模型名**：实测 `message_end.metadata` 只有
  `['reasoning','retriever_resources','usage']`，`model` 与 `model_provider` 均为 `None`，
  整份 SSE 里 `deepseek` 字面命中 0 次。⇒ 「模型真被调用」的**响应侧证据只有 token 用量
  与非空回答**；App 配置里的 `deepseek_v4` **不是**响应证据，报告不得混用。
* **`--selftest` 只证明判定逻辑本身正确**（正控 + 负控 + 优先级），
  **不等于**「真机上模型与工具都正常」；真机结论只能由一次真实运行给出。
