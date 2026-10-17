# `shared/deploy/prompts` · 提示词单一事实源（主对比组 + 方案3 家族）

| 项 | 值 |
| --- | --- |
| 适用 App | 主对比组：`cc-kg-agent`、`cc-onto-agent`；方案3：`cc-kgt-agent`、`cc-kgt-free-agent` |
| 交付物 | `agent_system.md`（**基准**，唯一正文）、`kgt_agent_system.md`（KG-T 变体）、`kgt_free_agent_system.md`（KG-T-free 变体）、`README.md`（本文件） |
| 批次 | M4 · W4 —— 实施队长裁定 **R-38「提示词单一事实源」**（取代 M4.4/P1 的「不点名工具」写法） |
| 上游依据 | `tmp/_m4_taskbook.md` §0.2 R-38、§3 红线；`tmp/_m4_worklist.md` §G；`docs/05-编排层设计（Dify）.md` §5.1 / §5.2 / §5.3；`docs/03-方案1设计（知识图谱+LLM）.md` §7.5；`docs/09-方案3设计（知识图谱直引+LLM）.md` §6.2 |
| 下游消费方 | `shared/deploy/dify/binding.yaml` 的 `apps[].prompt:`、W2 的部署脚本与 `shared/deploy/check_fairness.py`（主组 F1）；`shared/model/kgt-prompt-variants.yaml` v1.2 的 `baseline.prompt_file` / `variants.<arm>.prompt_file` 与 F5 门禁 `shared/verify/f5_kgt_gate.py` v2 |

## 1. R-38 之后的单一事实源关系

R-38 之前的形态是「提示词正文内嵌在 `kgt-prompt-variants.yaml` 的 `lines:` 里」，正文与真实落盘
文件可能漂移，冻结差异只是散文。R-38 改为**文件为单一事实源 + 变体落盘 + 门禁机械比对文件差异**：

```
docs/05 §5.1（设计正文，只读）
        │  逐字抄录（15 行围栏块）
        ▼
shared/deploy/prompts/agent_system.md ──────────────► 主对比组两个 App 的 pre_prompt（逐字节相同）
        │  基准（唯一正文，5055 B / 76 LF / sha256 9c737b97…）
        │
        ├─ + 恰 4 条冻结差异（D-1…D-4） ─► shared/deploy/prompts/kgt_agent_system.md      ─► cc-kgt-agent
        │                                   （5154 B / 76 LF / sha256 0fc389b5…）
        └─ + 恰 7 条冻结差异（D-12 + D-5…D-9 + D-11） ─► shared/deploy/prompts/kgt_free_agent_system.md ─► cc-kgt-free-agent
                                            （5874 B / 78 LF / sha256 a3c3fc05…）

        ▲ 三个路径 + 字节数 + LF 数 + sha256 + 逐条差异清单
        └── 全部登记在 shared/model/kgt-prompt-variants.yaml（v1.3）
                                        │
                                        ▼  机械比对（行级 difflib）
                            shared/verify/f5_kgt_gate.py（F5 门禁 v2，177 项断言）
```

四条判据（任一条不成立即 F5 FAIL）：

1. **基准 = 设计正文**：基准文件必须逐字包含 `docs/05` §5.1 的 15 行（角色行 / 两步 / 5 个工具名 /
   硬性要求 1–4），且满足 §5.3 的 6 个锚点；禁用词（§5.2 正则）零命中。
2. **变体 = 基准 + 恰 N 条差异**：门禁用 `difflib` 对「基准文件 ↔ 变体文件」做行级差异（增 / 删 / 改），
   断言差异集合**逐条等于** yaml 的 `frozen_diff` 清单（`kind` / `baseline_line` / `variant_line` /
   `variant_text` 全等：多一行、少一行、改一字都失败）。
3. **未声明处不得改动**：用「基准 + 声明差异」**重建**变体文本，断言与变体文件逐行相同。
4. **正文不漂移**：三份文件的 bytes / LF 数 / sha256 必须等于 yaml 登记值。

## 2. 为什么主组两个 App 必须共用同一份文件（F1）

`docs/05` §1.3 的 F1 = 两个实验组的 `pre_prompt` **逐字相同**，且不含禁用词。任务书 §1 给出的
`binding.yaml` 声明里，两条 App 记录的提示词字段指向**同一路径**：

```
- { app: cc-kg-agent,  carrier: kg,   provider: cc-kg-tools,   prompt: shared/deploy/prompts/agent_system.md }
- { app: cc-onto-agent, carrier: onto, provider: cc-onto-tools, prompt: shared/deploy/prompts/agent_system.md }
```

由此得到的部署纪律（W2 必须遵守，本批次只落文件）：

1. 部署脚本按 `prompt:` 整文件读入，**逐字节**写入两个 App 的 `pre_prompt`；
2. 读取过程中不得做任何文本加工——不得去空行、不得 trim、不得替换词、不得拼前缀/后缀；
3. 两侧比较器**不得放宽**（任务书 §3 红线）：F1 判据是字节相同，不是语义等价。

**为什么不允许“各写一份、意思一样”**：任何一处哪怕同义改写都会让 F1 当场失败，而且改写点本身
（例如只在一侧多写一句“先看清单”）会变成隐性实验变量，把两组差异从“载体”挪到“提示词”。

★ 现状诚实说明：`shared/deploy/dify/binding.yaml` 截至本批次施工时**在磁盘上尚不存在**（W1 在飞），
上表两条声明引自任务书 §1 对它的引用；本文件不代它落盘。

## 3. 载体中性：R-34 的扫描面与禁用词

### 3.1 扫描面定义

R-34 的扫描面 = 主对比组的**全部模型面字符串** = `pre_prompt` ∪ 每个已绑工具的
`tool_name` / `description` / 参数名 / 参数描述；`binding.yaml` 里的投影口径写作
`model_face_projection: agent.tools[*].(tool_name, description, parameters)`，
`forbid_scope: ["pre_prompt", "agent.tools[*] 的模型面字符串"]`。

本目录负责其中 **`pre_prompt` 这一项**。工具面字符串来自
`shared/model/tools-setb.yaml`（本批次 inScope 之外、只读），由 W2 的
`shared/deploy/check_fairness.py`（同样尚未落盘）覆盖 F0/F2/F3/F4。

★ R-38 澄清：**工具名两侧逐字相同，不构成泄漏**。§5.1 的模板本来就点名
`cc_guide` / `cc_facts` / `cc_cause` / `cc_trace` / `cc_propose`，因此基准点名它们不是载体泄漏；
被禁止的是**载体名 / 语言族 / 图库名**（即下方禁用词）。

### 3.2 禁用词表（R-34，大小写不敏感）

```
cypher | sparql | 图 | 本体 | 三元组 | property graph | RDF | OWL | 知识图谱 | 图谱
```

★ 陷阱：`图` 是**单字**禁用，因此「拓扑图 / 图谱 / 绘图 / 图表」以及**包含该字的常用词**
（试图 / 意图 / 蓝图 / 地图）全部不可用。本目录采用的替代表述：

| 想写 | 改写成 |
| --- | --- |
| 拓扑图 / 图谱 | 拓扑 / 关系 / 层级 / 链路 |
| 图表、画图 | 表格 / 条目 |
| 试图、意图 | 尝试 / 打算 / 目的 |
| 主体结构 | 结构 / 层级（不用“本体”一词） |

### 3.3 并集纪律与豁免组

`docs/05` §5.2 的 CI 正则比 R-34 多出 `neo4j | graphdb | turtle | ttl | shacl | 推理机 | reasoner | 属性图`。
**基准与 KG-T 变体**按两者并集回避，扫描证据如下（并集同样 0 命中）。

`kgt_free_agent_system.md`（KG-T-free）是**唯一豁免组**：该臂直连查询，必须能书写查询语言与结构
词汇才能工作，`kgt-prompt-variants.yaml` 里记 `banned_words_policy: exempt` 并给出理由
（裁决 12 取代裁决 10 对本组的适用）。豁免**不外溢**到 KG-T / KG-T-dyn；门禁对豁免组改为断言
「命中集合恰等于声明集合」（比 v1 的「跳过」更严）。

★ 边界说明：本 README **不是**模型面字符串，因此本节为了可复核而逐字引用了禁用词；
扫描目标恒为三份 `.md` 本体，从未把 README 计入扫描面。

## 4. 自检证据（在本工作目录本机执行）

### 4.1 禁用词扫描（基准与 KG-T 命中数必须为 0）

```powershell
$p = 'shared/deploy/prompts/agent_system.md'
$s = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes((Resolve-Path $p)))
# R-34 正则（大小写不敏感）
([regex]::Matches($s, 'cypher|sparql|图|本体|三元组|property graph|RDF|OWL|知识图谱|图谱', 'IgnoreCase')).Count
# docs/05 §5.2 的额外词（并集纪律）
([regex]::Matches($s, 'neo4j|graphdb|turtle|ttl|shacl|推理机|reasoner|属性图', 'IgnoreCase')).Count
```

实测结果（W4 批，见 `tmp/_m4_w4_report.md` §3）：

| 口径 | `agent_system.md`（基准） | `kgt_agent_system.md`（KG-T） | `kgt_free_agent_system.md`（豁免） |
| --- | --- | --- | --- |
| gate.banned_words（10 词） | **0** | **0** | 3（cypher / 图 / schema，已声明） |
| docs/05 §5.2 正则 | **0** | **0** | 3（cypher / neo4j / 知识图谱，已声明） |

### 4.2 文件指纹与编码（LF-only、无 BOM）

```powershell
$p = 'shared/deploy/prompts/agent_system.md'
$b = [System.IO.File]::ReadAllBytes((Resolve-Path $p))
"BYTES=$($b.Length)"
"LF=$(($b | Where-Object {$_ -eq 10}).Count)"
"CR=$(($b | Where-Object {$_ -eq 13}).Count)"
"BOM=$($b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF)"
(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLower()
```

实测结果（三个文件均 CR=0 / BOM=False / 以 LF 结尾）：

| 文件 | 字节数 | 行数（字节级 LF 计数） | sha256 |
| --- | --- | --- | --- |
| `agent_system.md`（基准） | **5055** | **76** | `9c737b97566f0fb3a34b568c27ee00751051044804f5dbef44c214ad86ca029b` |
| `kgt_agent_system.md`（KG-T） | **5154** | **76** | `0fc389b5368e036112764e56d66f7c5b071f3a313cf58b7ee71b5fbd44cf9a78` |
| `kgt_free_agent_system.md`（KG-T-free） | **5874** | **78** | `a3c3fc05a5bfc940e344d79d120baf4cfde3348d647c80566c67fadf035293d6` |

★ 坑：不要用 `(Get-Content …).Count` 或 `Measure-Object -Line` 当行数口径——本机 pwsh 对
「UTF-8 无 BOM 的中文文件」会给出偏小伪值（首稿实测 46 / 36，而字节级 LF 与编辑器行号都是 97）。
行数口径统一取**字节级 LF 计数**。

### 4.3 提示词里点名的字段名都要有来源（★ R-38 第 5 条判决）

判据：基准点名的每个返回字段必须在 `tools-setb.yaml` **与** `kgt-tools.yaml` 都核得到；
只在 Set B 有、KG-T 没有的字段，必须从基准移除或由某条冻结差异显式处理（冻结差异数恒为 4/5，
故无法用差异承载 ⇒ 一律**移除字段名、保留语义**）。

| 基准可能点名的字段 | `tools-setb.yaml`（主组） | `kgt-tools.yaml`（KG-T / KG-T-free） | 处置 |
| --- | --- | --- | --- |
| `truncated` | `:143`（`return_envelope`，applies_to: all_tools） | `:92`（get_object_topology）、`:136`（get_fault_topology） | **保留**（两侧可核） |
| `hint` | `:137` | **无** | 撤回字段名，语义（空结果两种含义）以字段无关措辞保留 |
| `droppedRows` | `:149` | **无** | 同上 |
| `droppedBytes` | `:155` | **无** | 同上 |
| `rowCount` | `:455`（cc_query 审计三字段之一） | **无**（`:218` 明说插件不回传逐调用审计字段） | 从未写入基准 |
| `executed` | `:645`（cc_propose） | **无** | 撤回字段名，只读/干跑语义保留 |
| `dryRun` | `:651`（cc_propose） | **无** | 同上 |
| 上限 200 行 / 20000 字节 | `:163` `row_cap` / `:164` `byte_cap` | `:284-287` 声明该口径「在插件路径上不适用」 | 撤回**数值**，只保留「有界返回 + 截断要记账」的纪律 |
| `hopsUsed` | `:556`（cc_trace） | `:93`（get_object_topology） | 两侧可核，但 v1/v2 均未点名（不新增，避免改变两臂提示词丰度） |

★ 语义未砍：被撤回的只是**字段标识符**。这些字段名仍随工具自身的 `description` 进入模型面
（`tools-setb.yaml` 的 `:227` / `:269` / `:318` / `:468` / `:524` / `:578` / `:618`），
KG-T 侧工具描述则自带 `truncated`；空结果两种含义、截断记账、只读干跑三条纪律在基准里逐条保留。

## 5. 基准正文覆盖的纪律（对照表）

| 要求 | 出处 | 落点（`agent_system.md` v2） |
| --- | --- | --- |
| 照抄 §5.1：角色行 + 两步 + 5 个工具名 + 硬性要求 1–4 | `docs/05` §5.1（R-38 第 ② 条） | 文件顶部围栏块（行 3–17） |
| 先规划再调用、循环保护 | 任务书 R-34 上下文 / M4.4 要求 3 | §1 |
| 证据绑定到对象 id 与字段取值、不得编造 | `docs/05` §5.1 硬性要求 ① | §2、围栏块硬性要求 1 |
| 空结果的两种含义 | 任务书要求 3；`docs/05` §2.2 | §3.1（不点名 `hint`，改为如实转述工具给出的提示文本） |
| 截断记账（`truncated`） | 任务书；`docs/05` §2.2 与 §1.3 的 C6 口径 | §3.2、§5 |
| 不确定就写“不足以判定 / 无法定位”并列出缺口 | `docs/05` §5.1 硬性要求 ②、§5.3「无法定位」锚点 | §3.3、围栏块硬性要求 2 |
| 动作只读 / 干跑 | 任务书；`docs/05` §5.1 硬性要求 ④ | §3.4（不点名 `executed`/`dryRun`，保留「不假设动作已执行」语义） |
| 多根因按可能性排序 + 是否可能同时成立 | `docs/05` §5.1 硬性要求 ③、§5.3「多根因」锚点 | 围栏块硬性要求 3、§4 第 1 条 |
| 输出格式：结论 → 证据 → 不确定项/缺口 → 建议动作；不得平铺原始 JSON | 任务书要求 4；`docs/05` §5.3「输出格式」锚点 | §4 |
| 工具返回约定（有界返回、截断字段、空结果提示、错误码如实转述），不含具体实现或语言族 | 任务书要求 6 | §5 |
| 载体中性、不暗示哪组更强 | 任务书要求 1–2 | 全文未出现任何载体名、语言族、查询语言名；未写“A 不行就用 B”式兜底 |

## 6. 方案3 与基准的关系（F5 管辖面）

方案3 的三个臂**都**以本目录的基准文件为唯一基准，各自只差冻结差异：

| 臂 | App / 插件 | 提示词 | 差异 | 工具面 |
| --- | --- | --- | --- | --- |
| 主对比组 | `cc-kg-agent` / `cc-onto-agent` | `agent_system.md`（**直接**） | — | 5 个 `cc_*` 工具 |
| KG-T | `cc-kgt-agent` | `kgt_agent_system.md` | D-1…D-4（恰 4 条） | 4 个 `get_*` 工具 |
| KG-T-dyn | `cc-kgt-dyn-agent` | 由 KG-T 派生（`derived_from: KG-T` + 1 条 `collect_now` 派生差异，**不落文件**） | D-dyn-1 | 5 个（KG-T + `collect_now`） |
| KG-T-free | `cc-kgt-free-agent` | `kgt_free_agent_system.md` | D-5…D-9 + D-11（恰 6 条） | 1 个（`neo4j_query` 插件） |

F5 门禁 = `shared/verify/f5_kgt_gate.py`（v2）。方案3 两组不参与 F0/F1（F5 豁免），
但 KG-T / KG-T-dyn 的 `pre_prompt` 仍受禁用词 == 0 约束。

★ 编号漂移（本批次只记录、不改文档）：`docs/09-方案3设计（知识图谱直引+LLM）.md` `:530-532` 的
D-7/D-8/D-9 与 `kgt-prompt-variants.yaml` 的编号映射不一致（docs：D-7 共现 / D-8 只读纪律 /
D-9 截断声明；yaml：D-7 查询语言只读 / D-8 变长路径与 4 跳 / D-9 共现）。文档回写属后续独立批次；
本批次以 `docs/09` `:263-273` 的**工具面差异文本**为权威 `variant_text` 来源。

## 7. wiring（本目录如何被消费）

1. W1 在 `shared/deploy/dify/binding.yaml` 的 `apps[].prompt:` 里指向基准路径（单一事实源）；
2. W2 的部署脚本按该字段整文件读入 → 写入两个 App 的 `pre_prompt`；方案3 三臂按
   `kgt-prompt-variants.yaml` 的 `variants.<arm>.prompt_file` 读取各自文件；
3. W2 的 `shared/deploy/check_fairness.py` 回读两侧 `pre_prompt` 做字节比较（F1），并跑 F0–F4；
   F5 回读用 `shared/verify/f5_kgt_gate.py --dify-readback <json>`。

★ 本批次**只交付文件**：截至现在，各 App 的 `pre_prompt` 尚未写入，也没有任何真机回读证据。
**不得**把本 README 或这些文件当作“提示词已在 Dify 生效”的证据。

## 8. v1 → v2 差异说明（本目录内）

| 文件 | v1（M4.4 / P1） | v2（M4 · W4 / R-38） |
| --- | --- | --- |
| `agent_system.md` | 5440 B / 77 LF / sha256 `bebbbb44…`；§1 总则 + §2 两步（**能力类别措辞，不点名任何工具**）+ §3 证据纪律 + §4 结论边界（点名 `hint`/`droppedRows`/`droppedBytes`/`executed`/`dryRun` 与 200 行/20000 字节）+ §5 输出格式 + §6 返回约定 | 4934 B / 75 LF / sha256 `62843bb5…`；**顶部逐字加入 §5.1 的 15 行**（角色行 / 两步 / 5 个工具名 / 硬性要求 1–4），并保留 v1 中两侧同值的增量（总则 / 证据纪律 / 结论边界 / 输出格式 / 返回约定，章节重编号为 §1–§5）；**撤回** Set B 独有字段名与 200/20000 数值，语义保留 |
| `kgt_agent_system.md` | 不存在（正文内嵌在 yaml 的 `lines:` + 散文 `identical_parts`） | 新建，KG-T 权威全文 = 基准 + 恰 4 条冻结差异 |
| `kgt_free_agent_system.md` | 不存在（同上） | 新建，KG-T-free 权威全文 = 基准 + 恰 6 条冻结差异（D-5…D-9 + D-11） |
| `kgt-prompt-variants.yaml` | v1.1：11948 B；`baseline.lines` 三行内嵌（且用全角引号，与 `docs/03` §7.5 的 ASCII 引号**不一致**＝既有漂移）；`identical_parts` 散文 6 条 | v1.2：**17317 B / 232 LF**（★ 2026-09-29 队长亲改复核实测；W41 中间值 17246 B / 232 LF = `kgt-prompt-variants.yaml:8` 注释就地编辑「恰 5 条」→「恰 6 条」前的时点值，与 v1.2 初版登记 14653 B / 219 LF **均仅存沿革**）；`baseline` 改**文件引用**（路径 + 字节数 + LF 数 + sha256），`variants.<arm>` 记 `prompt_file` + `expected_tools` + 逐条 `frozen_diff`；删 `identical_parts`；`gate.assertions` 由 12 条同步改写为 **29 条**（★ 2026-09-29 W41 实测：v1.2 初版为 26 条） |
| `f5_kgt_gate.py` | v1：356 行，66 项断言，只做 yaml 内嵌字符串包含 | v2：174 项断言（`--selftest` 183 项），从**文件**读正文并做行级差异 + 重建 + 指纹比对；新增 `--selftest`（5 个负控 + 1 个正控）与 `--baseline` / `--prompts-dir` / `--prompts-yaml` 路径注入 |

★ **S2b 追加（2026-10-05，D47/D48：中性取数能力）**：上表「v2」列的指纹已被 S2b 再次推进 ——
`agent_system.md` = **5055 B / 76 LF / `9c737b97…`**、`kgt_agent_system.md` = **5154 B / 76 LF / `0fc389b5…`**、
`kgt_free_agent_system.md` = **5874 B / 78 LF / `a3c3fc05…`**（三份文件均在第 4 行插入 1 行中性取数能力宣告；
基准与 KG-T 逐字相同，免费臂用声明式别名 `read_live_facts`）；
`kgt-prompt-variants.yaml` = **19101 B / 250 LF**（v1.3，`frozen_diff_count_kgt_free` 6→7 + 新增 D-12）；
`f5_kgt_gate.py` = **177 项断言**（`--selftest` **186 项**）。
本表 v1/v2 两列的数值自此**仅存沿革**，当前权威值以 §4.2 的指纹表与本注为准。

★ R-38 明确**不采纳** P1 的「不点名工具」写法：`docs/05` §5.1 的模板与 `docs/03` §7.5 的基准三行
都点名工具，且 `kgt-prompt-variants.yaml` 的冻结差异 D-1/D-2 正是对那三行里工具名的替换；
若基准不点名工具，D-1/D-2 会变成空转，方案3 的提示词（约 5 行）与主对比组（77 行）体量悬殊，
会把「归因主体」的差异混入「提示词丰度」的混淆项。

## 9. 已知边界（诚实清单）

1. 本目录只保证**提示词层**公平（F1 的一半：`pre_prompt` 侧）；工具面公平由 W2 的
   `shared/deploy/check_fairness.py` 负责，该脚本**尚未落盘**。
2. **基准即方案3 的基准**（R-38 之后）：`agent_system.md` 同时是主对比组的正文与 KG-T 家族的唯一基准，
   方案3 的提示词与主对比组的差异已收敛为「恰 N 条冻结差异」，不再是 M4.4 时代的整篇重写。
3. §5.3 的「工具调用指引」锚点在两份变体里**不可能逐字齐备**：基准里那句 `先调用 cc_guide …`
   正是被 D-1 / D-5 替换、且 `cc_*` 在该两臂被禁的工具名。门禁因此**按臂判定**该锚点
   （正则匹配到的工具名必须 ⊆ 本臂 `expected_tools`），其余 5 个锚点仍要求逐字齐备。
   这是 R-38 下的固有取舍，记录在此供复核。
4. 基准不再写死「最多 200 行 / 20000 字节」，因为 `kgt-tools.yaml` `:284-287` 声明该口径
   在插件路径上不适用；KG-T 侧的实际载荷上限由契约（`query_max_chars` / `max_records_default`）承担。
5. 提示词不再点名 `executed` / `dryRun`（KG-T 侧无此字段）；§3.4 的只读纪律改以「不假设任何动作
   已被执行」表述，与围栏块硬性要求 4 同源。
6. `kgt_agent_system.md` / `kgt_free_agent_system.md` 的正文**不由本 README 维护**：它们由
   冻结差异定义，任何改动都必须同步 `kgt-prompt-variants.yaml` 的清单与指纹，否则 F5 立即失败。
7. 本 README 的指纹与扫描结果是**本机离线自证**；Dify 侧的真机一致性属 W2，本批次未验证。
8. `docs/09` `:530-532` 的 D-7/D-8/D-9 编号与 yaml 不一致（见 §6 末尾）：本批次**未改文档**，
   待文档回写批次处理；在此之前以 yaml 为门禁权威。
