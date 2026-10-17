# 附录 A：ORSD（Ontology Requirements Specification Document）+ CQ 集（6 族）

> **本文件是可复算产物**，不是手写文档：CQ 表由 `scheme2-onto/tests/cq_probes.py`
> （CQ 探针唯一真源）+ `shared/model/*.yaml` 契约生成；**生成器随内部交付物分发，不在本仓**（本文件本身已随仓分发，可直接阅读）。
> 对应 `docs/07-实施计划与验收.md` 的 **M2.4**；配套 `M2.5`（`cc-*.ttl` ×7）、`M2.6`（SHACL）、`M2.7`（`r01..r07.rq`）。

复算命令：

```powershell
# 1) 跑 CQ 探针（真机 live5 夹具 + 契约完备夹具两轮）
python scheme2-onto/tests/cq_probes.py --fixture --out tmp/cq_probe_report_live5.txt
# 2) 由契约 + 探针生成本文件 —— 生成器不在本仓分发（`tmp/` 不入库），本仓只能复算第 1、3 步
# 3) 本体校验（T1–T12 的静态部分）
python shared/verify/onto_tbox_check.py
```

## 1. ORSD 必需字段（LOT 规定，逐字对应 `docs/04:63-72`）

| 字段 | 本项目内容 |
|---|---|
| Purpose | 为「信用卡系统跨层运维诊断」提供可推理、可审计的形式化语义层 |
| Scope | 四层一条链：`CreditCardApp → Container → Host → (JDBC) → GaussDB(Cluster→Instance→Pool→Session→Query→Table)` + 故障模式 + 因果 + 指标 |
| Implementation language | OWL 2 RL / Turtle（`scheme2-onto/ontology/cc-*.ttl` ×7） |
| Intended end-users | 运维 Agent（主）、一线工程师（次） |
| Intended uses | ①跨层根因定位 ②变更前影响面推导 ③审计回放 |
| 非功能需求 | 推理**确定性**（同 ABox → 同结论）；推理延迟 P95 < 2s；单次查询结果 ≤ 200 行 / ≤ 20000 B |
| 功能需求 = CQ 集 | 见 §3（6 族；CQ-F 每个故障模式一条） |
| 词汇表 | 从 `shared/model\{topology,metrics,fault-modes,causal-edges}.yaml` 抽取：14 类型（13 具体 + 1 派生注释型 `Evidence`）+ 15 关系（14 业务 + 1 派生 `HAS_EVIDENCE`）+ 58 指标 + 14 故障模式 + 18 因果边（见 §4） |

## 2. CQ 集总览（6 族；条数由契约派生）

| 族 | 条数 | 对应用例族 | 探针实测（真机 ABox） |
|---|---|---|---|
| `CQ-T` | 14 | 基础能力（用例的前置条件） | 14/14 PASS |
| `CQ-X` | 12 | C2 跨层定界 | 11/12 PASS |
| `CQ-F` | 23 | C1 单层定位 | 23/23 PASS |
| `CQ-C` | 10 | C2、C3 | 10/10 PASS |
| `CQ-D` | 8 | C3 同症状区分、C4 否定性证据 | 8/8 PASS |
| `CQ-A` | 4 | 对照维度（审计性） | 4/4 PASS |
| **合计** | **71** | `docs/06` §1 的 50 条用例 + 12 条内部校验 | **70/71 PASS** |

★ 真机 ABox 上未通过的 CQ 全部是 **ABox 投影缺口**（见 §6.2），不是本体表达力缺口：缺口清单与 SHACL 违例一一对应。

## 3. CQ 逐条清单（条数由契约派生）

每条 CQ 四要素：**CQ 文本** / **对应场景** / **需要哪些类-关系-指标** / **由哪条规则或查询回答**，
并附可执行 SPARQL 探针与最新实测结果。

### 3.1 拓扑能力（14 条）

#### `CQ-T-01`

- **CQ 文本**：给定一份 ABox，能否列出全部 `CreditCardApp` 实例，并给出其契约键属性（`cc:appCode`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:CreditCardApp`、`cc:appCode`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-01（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:CreditCardAppShape`
- **期望**：`coverage`（覆盖率 100%：cc:appCode）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:CreditCardApp . OPTIONAL { ?s cc:appCode ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 1 个，键属性覆盖 1/1

#### `CQ-T-02`

- **CQ 文本**：给定一份 ABox，能否列出全部 `AppInstance` 实例，并给出其契约键属性（`cc:instanceId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:AppInstance`、`cc:instanceId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-02（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:AppInstanceShape`
- **期望**：`coverage`（覆盖率 100%：cc:instanceId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:AppInstance . OPTIONAL { ?s cc:instanceId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 1 个，键属性覆盖 1/1

#### `CQ-T-03`

- **CQ 文本**：给定一份 ABox，能否列出全部 `AppEndpoint` 实例，并给出其契约键属性（`cc:path`、`cc:protocol`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:AppEndpoint`、`cc:path`、`cc:protocol`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-03（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:AppEndpointShape`
- **期望**：`coverage`（覆盖率 100%：cc:path、cc:protocol）
- **探针**：

```sparql
SELECT ?s ?k1 ?k2 WHERE { ?s a cc:AppEndpoint . OPTIONAL { ?s cc:path ?k1 } . OPTIONAL { ?s cc:protocol ?k2 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 2 个，键属性覆盖 4/4

#### `CQ-T-04`

- **CQ 文本**：给定一份 ABox，能否列出全部 `JdbcDataSource` 实例，并给出其契约键属性（`cc:dsCode`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:JdbcDataSource`、`cc:dsCode`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-04（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:JdbcDataSourceShape`
- **期望**：`coverage`（覆盖率 100%：cc:dsCode）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:JdbcDataSource . OPTIONAL { ?s cc:dsCode ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 1 个，键属性覆盖 1/1

#### `CQ-T-05`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Container` 实例，并给出其契约键属性（`cc:containerId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:Container`、`cc:containerId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-05（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:ContainerShape`
- **期望**：`coverage`（覆盖率 100%：cc:containerId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:Container . OPTIONAL { ?s cc:containerId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 1 个，键属性覆盖 1/1

#### `CQ-T-06`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Host` 实例，并给出其契约键属性（`cc:hostname`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:Host`、`cc:hostname`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-06（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:HostShape`
- **期望**：`coverage`（覆盖率 100%：cc:hostname）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:Host . OPTIONAL { ?s cc:hostname ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 3 个，键属性覆盖 3/3

#### `CQ-T-07`

- **CQ 文本**：给定一份 ABox，能否列出全部 `GaussDBCluster` 实例，并给出其契约键属性（`cc:clusterName`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:GaussDBCluster`、`cc:clusterName`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-07（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:GaussDBClusterShape`
- **期望**：`coverage`（覆盖率 100%：cc:clusterName）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:GaussDBCluster . OPTIONAL { ?s cc:clusterName ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 1 个，键属性覆盖 1/1

#### `CQ-T-08`

- **CQ 文本**：给定一份 ABox，能否列出全部 `GaussDBInstance` 实例，并给出其契约键属性（`cc:instanceId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:GaussDBInstance`、`cc:instanceId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-08（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:GaussDBInstanceShape`
- **期望**：`coverage`（覆盖率 100%：cc:instanceId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:GaussDBInstance . OPTIONAL { ?s cc:instanceId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 2 个，键属性覆盖 2/2

#### `CQ-T-09`

- **CQ 文本**：给定一份 ABox，能否列出全部 `ConnectionPool` 实例及其所属层？（契约中该类型 `abstract=True`，允许空集）
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:ConnectionPool`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-09（计数式，空集也是确定答案）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT (COUNT(?s) AS ?n) WHERE { ?s a cc:ConnectionPool }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行
- **备注**：abstract 类型允许空集

#### `CQ-T-10`

- **CQ 文本**：给定一份 ABox，能否列出全部 `DatabaseSessionPool` 实例，并给出其契约键属性（`cc:poolId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:DatabaseSessionPool`、`cc:poolId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-10（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:DatabaseSessionPoolShape`
- **期望**：`coverage`（覆盖率 100%：cc:poolId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:DatabaseSessionPool . OPTIONAL { ?s cc:poolId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 2 个，键属性覆盖 2/2

#### `CQ-T-11`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Session` 实例，并给出其契约键属性（`cc:sessionId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:Session`、`cc:sessionId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-11（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:SessionShape`
- **期望**：`coverage`（覆盖率 100%：cc:sessionId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:Session . OPTIONAL { ?s cc:sessionId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 19 个，键属性覆盖 19/19

#### `CQ-T-12`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Query` 实例，并给出其契约键属性（`cc:queryId`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:Query`、`cc:queryId`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-12（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:QueryShape`
- **期望**：`coverage`（覆盖率 100%：cc:queryId）
- **探针**：

```sparql
SELECT ?s ?k1 WHERE { ?s a cc:Query . OPTIONAL { ?s cc:queryId ?k1 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 4 个，键属性覆盖 4/4

#### `CQ-T-13`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Table` 实例，并给出其契约键属性（`cc:schemaName`、`cc:tableName`）与所属层？
- **对应场景**：拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）
- **需要哪些类-关系-指标**：`cc:Table`、`cc:schemaName`、`cc:tableName`、`cc:ccfLayer`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-13（`scheme2-onto/tests/cq_probes.py`）；形状 `ccs:TableShape`
- **期望**：`coverage`（覆盖率 100%：cc:schemaName、cc:tableName）
- **探针**：

```sparql
SELECT ?s ?k1 ?k2 WHERE { ?s a cc:Table . OPTIONAL { ?s cc:schemaName ?k1 } . OPTIONAL { ?s cc:tableName ?k2 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 30 个，键属性覆盖 60/60

#### `CQ-T-14`

- **CQ 文本**：给定一份 ABox，能否列出全部 `Evidence` 个体及其 8 个溯源属性（recordId / metricCode / status / rawValue / rawUnit / reason / probe / capturedAt）？
- **对应场景**：审计前置能力（C5 审计链、`CQ-A-01` 的前置）
- **需要哪些类-关系-指标**：`cc:Evidence`、`cc:recordId`、`cc:metricCode`、`cc:status`、`cc:rawValue`、`cc:rawUnit`、`cc:reason`、`cc:probe`、`cc:capturedAt`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-T-14；无 SHACL 形状（派生类，docs/04:295-297 明示不建）
- **期望**：`coverage`（覆盖率 100%：cc:recordId、cc:metricCode、cc:status、cc:rawValue、cc:rawUnit、cc:reason、cc:probe、cc:capturedAt）
- **探针**：

```sparql
SELECT ?s ?k1 ?k2 ?k3 ?k4 ?k5 ?k6 ?k7 ?k8 WHERE { ?s a cc:Evidence . OPTIONAL { ?s cc:recordId ?k1 } . OPTIONAL { ?s cc:metricCode ?k2 } . OPTIONAL { ?s cc:status ?k3 } . OPTIONAL { ?s cc:rawValue ?k4 } . OPTIONAL { ?s cc:rawUnit ?k5 } . OPTIONAL { ?s cc:reason ?k6 } . OPTIONAL { ?s cc:probe ?k7 } . OPTIONAL { ?s cc:capturedAt ?k8 } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 实例 191 个，键属性覆盖 1312/1528；条件可选 216 处未绑定（obs-000001.cc:reason）
- **备注**：`cc:reason` / `cc:rawUnit` 是**条件必填**（契约 §D3：`reason` 仅 `status ∈ {notCollected, collectedEmpty}`；`rawUnit` 仅无量纲指标缺失）⇒ 列入 `coverage_optional`，缺失只报数不计失败

### 3.2 跨层可达（12 条）

#### `CQ-X-01`

- **CQ 文本**：给定一个 `CreditCardApp`，能否列出它对外暴露的全部 `AppEndpoint`？（列出该应用对外的全部端点）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:CreditCardApp`、`cc:exposes`、`cc:AppEndpoint`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-01（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:CreditCardApp ; cc:exposes ?t . ?t a cc:AppEndpoint }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-X-02`

- **CQ 文本**：给定一个 `CreditCardApp`，能否列出它的全部 `AppInstance`？（列出该应用的全部实例）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:CreditCardApp`、`cc:hasAppInstance`、`cc:AppInstance`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-02（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:CreditCardApp ; cc:hasAppInstance ?t . ?t a cc:AppInstance }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-03`

- **CQ 文本**：给定一个 `AppInstance`，能否定位到它使用的 `JdbcDataSource`？（定位应用实例用的数据源）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:AppInstance`、`cc:usesDataSource`、`cc:JdbcDataSource`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-03（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:AppInstance ; cc:usesDataSource ?t . ?t a cc:JdbcDataSource }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-04`

- **CQ 文本**：给定一个 `AppInstance`，能否定位到它运行所在的 `Container`？（定位应用实例所在容器）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:AppInstance`、`cc:runsIn`、`cc:Container`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-04（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:AppInstance ; cc:runsIn ?t . ?t a cc:Container }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-05`

- **CQ 文本**：给定一个 `Container`，能否定位到它部署所在的 `Host`？（定位容器所在宿主机）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:Container`、`cc:deployedOn`、`cc:Host`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-05（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:Container ; cc:deployedOn ?t . ?t a cc:Host }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-06`

- **CQ 文本**：给定一个 `JdbcDataSource`，能否定位到它连接的 `GaussDBCluster`？（定位数据源连的集群）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:JdbcDataSource`、`cc:connectsTo`、`cc:GaussDBCluster`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-06（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:JdbcDataSource ; cc:connectsTo ?t . ?t a cc:GaussDBCluster }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-07`

- **CQ 文本**：给定一个 `JdbcDataSource`，能否定位到它实际路由到的 `GaussDBInstance`？（定位数据源实际落的实例）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:JdbcDataSource`、`cc:resolvesTo`、`cc:GaussDBInstance`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-07（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:JdbcDataSource ; cc:resolvesTo ?t . ?t a cc:GaussDBInstance }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-X-08`

- **CQ 文本**：给定一个 `GaussDBCluster`，能否列出它下面的全部 `GaussDBInstance`？（列出集群下的实例）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:GaussDBCluster`、`cc:hasDbInstance`、`cc:GaussDBInstance`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-08（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:GaussDBCluster ; cc:hasDbInstance ?t . ?t a cc:GaussDBInstance }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-X-09`

- **CQ 文本**：给定一个 `GaussDBInstance`，能否列出它上面的全部 `DatabaseSessionPool`？（列出实例上的会话池）
- **对应场景**：C2 跨层定界（`B-*` 用例）
- **需要哪些类-关系-指标**：`cc:GaussDBInstance`、`cc:hasSessionPool`、`cc:DatabaseSessionPool`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-09（单跳可达性）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?t WHERE { ?s a cc:GaussDBInstance ; cc:hasSessionPool ?t . ?t a cc:DatabaseSessionPool }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-X-10`

- **CQ 文本**：给定一个 `CreditCardApp`，能否经 `JdbcDataSource → GaussDBCluster → GaussDBInstance → DatabaseSessionPool` 定位到它实际消费的会话池？（docs/04 §2.2 `CQ-X-05` 原文示例）
- **对应场景**：C2 跨层定界（`B-*` 用例；文档给出的复合路径示例）
- **需要哪些类-关系-指标**：`cc:CreditCardApp`、`cc:hasAppInstance`、`cc:usesDataSource`、`cc:connectsTo`、`cc:resolvesTo`、`cc:hasDbInstance`、`cc:hasSessionPool`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-10（5 跳复合可达性；`connectsTo` 与 `resolvesTo` 两条落库路径取并）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?app ?pool WHERE { ?app a cc:CreditCardApp ; cc:hasAppInstance ?ai . ?ai cc:usesDataSource ?ds . { ?ds cc:connectsTo ?cl } UNION { ?ds cc:resolvesTo ?cl } . ?cl cc:hasDbInstance ?dbi . ?dbi cc:hasSessionPool ?pool }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-X-11`

- **CQ 文本**：给定一个 `DatabaseSessionPool`，能否经 `Session → Query → Table` 定位到它承载的会话所执行的语句及这些语句访问的表？（3 跳复合可达性）
- **对应场景**：C2 跨层定界 + C1 单层定位（`B-*` / `A-*` 用例）
- **需要哪些类-关系-指标**：`cc:DatabaseSessionPool`、`cc:hasSession`、`cc:runsQuery`、`cc:accesses`、`cc:Table`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-X-11（该 3 跳在真机 ABox 上为空 —— 见 ORSD §5 投影缺口 D-2）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?pool ?tbl WHERE { ?pool a cc:DatabaseSessionPool ; cc:hasSession ?s . ?s cc:runsQuery ?q . ?q cc:accesses ?tbl }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**FAIL** — 0 行

#### `CQ-X-12`

- **CQ 文本**：给定一个 `AppInstance`，能否用 OWL 属性链公理 `cc:indirectlyHostedOn`（`runsIn ∘ deployedOn`）直接推出它所在的 `Host`（而非手写两跳查询）？
- **对应场景**：C2 跨层定界（`B-*` 用例）+ 载体能力对照（OWL 2 RL 独有：属性链）
- **需要哪些类-关系-指标**：`cc:AppInstance`、`cc:indirectlyHostedOn`、`cc:runsIn`、`cc:deployedOn`、`cc:Host`
- **由哪条规则或查询回答**：`cc-topology.ttl` 的 `owl:propertyChainAxiom ( cc:runsIn cc:deployedOn )`（真机 owl2-rl 物化实测见 `tmp/_live_tbox_report.txt` 阶段 4）+ 探针 CQ-X-12
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?ai ?h WHERE { cc:indirectlyHostedOn owl:propertyChainAxiom ?chain . ?chain rdf:first cc:runsIn ; rdf:rest ?rest . ?rest rdf:first cc:deployedOn . ?ai a cc:AppInstance ; cc:runsIn ?c . ?c cc:deployedOn ?h . ?h a cc:Host }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行
- **备注**：探针同时断言「公理在 TBox 上」与「该 ABox 上两跳可达」；OWL 属性链的**推理物化**由 GraphDB owl2-rl 承担（rdflib 不推理，故探针不依赖推理，见 H-2）

### 3.3 故障判据（23 条）

#### `CQ-F-01`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-APP-DS-CONNECT-FAIL`（数据源连接建立失败）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-DS-CONNECT-FAIL`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-01
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-APP-DS-CONNECT-FAIL ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-02`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-APP-ENDPOINT-SLOW`（应用端点响应劣化）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-ENDPOINT-SLOW`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-02
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-APP-ENDPOINT-SLOW ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-03`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-APP-HEAP-PRESSURE`（应用堆内存压力）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-HEAP-PRESSURE`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-03
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-APP-HEAP-PRESSURE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-04`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-APP-INSTANCE-RESTART`（应用实例重启）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-INSTANCE-RESTART`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-04
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-APP-INSTANCE-RESTART ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-05`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-APP-POOL-EXHAUSTED`（应用侧连接池耗尽）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-POOL-EXHAUSTED`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r01.rq`（载体 `ccp:Rule-r01`，由 `ccp:Exec-r01` 承载）+ 探针 CQ-F-05
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-APP-POOL-EXHAUSTED ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 3 行

#### `CQ-F-06`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-CTR-CPU-THROTTLED`（容器被 CPU 限流）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-CTR-CPU-THROTTLED`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r03.rq`（载体 `ccp:Rule-r03`，由 `ccp:Exec-r03` 承载）+ 探针 CQ-F-06
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-CTR-CPU-THROTTLED ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-07`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-CTR-NET-UNREACHABLE`（容器网络不可达）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-CTR-NET-UNREACHABLE`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r06.rq`（载体 `ccp:Rule-r06`，由 `ccp:Exec-r06` 承载）+ 探针 CQ-F-07
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-CTR-NET-UNREACHABLE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-08`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-CTR-OOM-KILLED`（容器 OOM 被杀）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-CTR-OOM-KILLED`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r04.rq`（载体 `ccp:Rule-r04`，由 `ccp:Exec-r04` 承载）+ 探针 CQ-F-08
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-CTR-OOM-KILLED ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-09`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-DEADLOCK`（数据库死锁）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-DEADLOCK`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-09
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-DEADLOCK ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-10`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-IDLE-IN-TX`（持锁不提交堆积（idle in transaction））的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-IDLE-IN-TX`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-10
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-IDLE-IN-TX ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-11`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-INSTANCE-DOWN`（数据库实例故障/切换）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-INSTANCE-DOWN`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r07.rq`（载体 `ccp:Rule-r07`，由 `ccp:Exec-r07` 承载）+ 探针 CQ-F-11
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-INSTANCE-DOWN ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-12`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-LOCK-WAIT`（数据库锁等待）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-LOCK-WAIT`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-12
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-LOCK-WAIT ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-13`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-REPLICATION-LAG`（主备复制延迟/中断）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-REPLICATION-LAG`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-13
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-REPLICATION-LAG ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-14`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-ROLLBACK-STORM`（事务回滚风暴）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-ROLLBACK-STORM`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-14
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-ROLLBACK-STORM ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-15`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-SEQ-SCAN`（全表扫描（索引失效））的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-SEQ-SCAN`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-15
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-SEQ-SCAN ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-16`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-SESSION-POOL-FULL`（数据库会话池耗尽）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-SESSION-POOL-FULL`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-16
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-SESSION-POOL-FULL ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-17`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-SLOW-QUERY`（数据库慢查询）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-SLOW-QUERY`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：规则文件 `scheme2-onto/rules/r02.rq`（载体 `ccp:Rule-r02`，由 `ccp:Exec-r02` 承载）+ 探针 CQ-F-17
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-SLOW-QUERY ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-18`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-DB-UNUSED-INDEX`（索引未使用（索引失效））的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-UNUSED-INDEX`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-18
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-DB-UNUSED-INDEX ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-19`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-HOST-CPU-CONTENTION`（宿主机 CPU 争抢）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-CPU-CONTENTION`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-19
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-HOST-CPU-CONTENTION ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-F-20`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-HOST-DISK-FULL`（宿主磁盘空间耗尽）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-DISK-FULL`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-20
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-HOST-DISK-FULL ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-21`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-HOST-DISK-IO-ERROR`（宿主磁盘 I/O 错误）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-DISK-IO-ERROR`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-21
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-HOST-DISK-IO-ERROR ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-22`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-HOST-MEM-PRESSURE`（宿主机内存紧张）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-MEM-PRESSURE`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-22
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-HOST-MEM-PRESSURE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

#### `CQ-F-23`

- **CQ 文本**：给定一组观测事实，能否判定 `FM-HOST-NET-IFACE-DOWN`（宿主网卡 down）的判据是否全部满足，并列出满足 / 未满足的判据？
- **对应场景**：C1 单层定位（`A-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-NET-IFACE-DOWN`、`ccf:judgedBy`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccf:jPredicate`、`ccf:jThresholdReference`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs/04 §6 只给 7 条规则）+ 探针 CQ-F-23
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:FM-HOST-NET-IFACE-DOWN ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . OPTIONAL { ?judge ccf:jPredicate ?predicate } OPTIONAL { ?judge ccf:jThresholdReference ?tier } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行

### 3.4 因果收敛（10 条）

#### `CQ-C-01`

- **CQ 文本**：给定症状 `FM-APP-POOL-EXHAUSTED`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F1，直接上游：`FM-APP-ENDPOINT-SLOW`、`FM-DB-SLOW-QUERY`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-POOL-EXHAUSTED`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-01（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行
- **备注**：链 F1 的终端症状 = FM-APP-POOL-EXHAUSTED

#### `CQ-C-02`

- **CQ 文本**：给定症状 `FM-APP-DS-CONNECT-FAIL`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F2，直接上游：`FM-CTR-NET-UNREACHABLE`、`FM-DB-INSTANCE-DOWN`、`FM-DB-REPLICATION-LAG`、`FM-DB-SESSION-POOL-FULL`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-DS-CONNECT-FAIL`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-02（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-DS-CONNECT-FAIL } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-DS-CONNECT-FAIL } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-DS-CONNECT-FAIL } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-DS-CONNECT-FAIL } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 14 行
- **备注**：链 F2 的终端症状 = FM-APP-DS-CONNECT-FAIL

#### `CQ-C-03`

- **CQ 文本**：给定症状 `FM-APP-ENDPOINT-SLOW`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F3，直接上游：`FM-APP-DS-CONNECT-FAIL`、`FM-APP-INSTANCE-RESTART`、`FM-APP-POOL-EXHAUSTED`、`FM-CTR-CPU-THROTTLED`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-ENDPOINT-SLOW`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-03（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行
- **备注**：链 F3 的终端症状 = FM-APP-ENDPOINT-SLOW

#### `CQ-C-04`

- **CQ 文本**：给定症状 `FM-DB-INSTANCE-DOWN`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F4，直接上游：`FM-HOST-DISK-FULL`、`FM-HOST-DISK-IO-ERROR`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-INSTANCE-DOWN`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-04（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-DB-INSTANCE-DOWN } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-DB-INSTANCE-DOWN } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-DB-INSTANCE-DOWN } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-DB-INSTANCE-DOWN } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行
- **备注**：链 F4 的终端症状 = FM-DB-INSTANCE-DOWN

#### `CQ-C-05`

- **CQ 文本**：给定症状 `FM-APP-ENDPOINT-SLOW`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F5，直接上游：`FM-APP-DS-CONNECT-FAIL`、`FM-APP-INSTANCE-RESTART`、`FM-APP-POOL-EXHAUSTED`、`FM-CTR-CPU-THROTTLED`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-ENDPOINT-SLOW`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-05（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行
- **备注**：链 F5 的终端症状 = FM-APP-ENDPOINT-SLOW

#### `CQ-C-06`

- **CQ 文本**：给定症状 `FM-APP-ENDPOINT-SLOW`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F6，直接上游：`FM-APP-DS-CONNECT-FAIL`、`FM-APP-INSTANCE-RESTART`、`FM-APP-POOL-EXHAUSTED`、`FM-CTR-CPU-THROTTLED`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-ENDPOINT-SLOW`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-06（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行
- **备注**：链 F6 的终端症状 = FM-APP-ENDPOINT-SLOW

#### `CQ-C-07`

- **CQ 文本**：给定症状 `FM-APP-ENDPOINT-SLOW`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 F7，直接上游：`FM-APP-DS-CONNECT-FAIL`、`FM-APP-INSTANCE-RESTART`、`FM-APP-POOL-EXHAUSTED`、`FM-CTR-CPU-THROTTLED`）？
- **对应场景**：C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-ENDPOINT-SLOW`、`ccc:cause`、`ccc:linkKind`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-07（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；跳数上限 4 = G8 因果深度门禁）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?mode WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-ENDPOINT-SLOW } . ?root a ccf:FaultMode ; ccf:faultModeCode ?mode }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行
- **备注**：链 F7 的终端症状 = FM-APP-ENDPOINT-SLOW

#### `CQ-C-08`

- **CQ 文本**：给定症状，能否按「因果强度（`ccc:linkKind`：`causes` > `conditional_causes` > `relates_to`）+ 跳数」给出**有序**的根因集合？
- **对应场景**：C3 同症状区分（`D-*` 用例；需要排序证据）
- **需要哪些类-关系-指标**：`ccc:linkKind`、`ccc:cause`、`ccc:effect`、`ccc:CausalLink`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-08（1/2/3+ 跳分层 UNION + `linkKind` 排序）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?root ?kind ?rank WHERE { { ?root ^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED . BIND(1 AS ?rank) } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED . BIND(2 AS ?rank) } UNION { ?root ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ccf:FM-APP-POOL-EXHAUSTED . BIND(3 AS ?rank) } . ?l0 a ccc:CausalLink ; ccc:cause ?root ; ccc:linkKind ?kind } ORDER BY ?rank ?kind
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 22 行
- **备注**：契约 `causal-edges.yaml` 无「强度」数值字段（只有 linkKind 枚举）⇒ 排序口径 = 枚举序 + 跳数（自定，见诚实清单）

#### `CQ-C-09`

- **CQ 文本**：能否检测因果图中的环，并判定检测到的环 **⊆ 契约白名单**（`causal-edges.yaml#meta.cycles`）？
- **对应场景**：内部校验（`docs/04` §8 `T6`：检测到的环 ⊆ 白名单）
- **需要哪些类-关系-指标**：`ccc:cause`、`ccc:CausalLink`、`ccc:chainCode`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-09（经 `ccc:CausalLink` 物化个体的双向对检测，等价契约 §8 T6；契约字面写法 `ccc:cause{2,4}` 自反匹配在 n 元物化下恒假 ⇒ X-9）+ 契约白名单 `[['FM-APP-POOL-EXHAUSTED', 'FM-APP-ENDPOINT-SLOW']]`
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?m1 ?m2 WHERE { ?l1 ccc:cause ?m1 ; ccc:effect ?m2 . ?l2 ccc:cause ?m2 ; ccc:effect ?m1 . FILTER(STR(?m1) < STR(?m2)) }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行

#### `CQ-C-10`

- **CQ 文本**：能否给出**因果深度 ≤4** 的全部可达对（G8 深度门禁的可机检形式）？
- **对应场景**：内部校验（G8 因果深度 ≤4，`docs/04` §7 G8）
- **需要哪些类-关系-指标**：`ccc:cause`、`ccc:CausalLink`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-C-10（1..4 跳可达对枚举，等价 `ccc:cause{1,4}`；≥5 跳溢出反例由 `test_integrity.py` T6 断言为空）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT DISTINCT ?s ?t WHERE { { ?s ^ccc:cause/ccc:effect ?t } UNION { ?s ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ?t } UNION { ?s ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ?t } UNION { ?s ^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect/^ccc:cause/ccc:effect ?t } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 94 行

### 3.5 区分与排除（8 条）

#### `CQ-D-01`

- **CQ 文本**：给定同症状「应用连接获取失败」，能否用契约 discriminator 指标 `db.pool.session_used_ratio` 把 `FM-APP-POOL-EXHAUSTED` 与 `FM-DB-SESSION-POOL-FULL` 区分开（FM-APP-POOL-EXHAUSTED 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-POOL-EXHAUSTED`、`ccm:m_db_pool_session_used_ratio`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-APP-POOL-EXHAUSTED` 与 `FM-DB-SESSION-POOL-FULL`；`ccc:discriminatorMetric` = `db.pool.session_used_ratio`）+ 探针 CQ-D-01
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric ?judge ?metricCode ?operandKind WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-APP-POOL-EXHAUSTED , ccf:FM-DB-SESSION-POOL-FULL ; ccc:discriminatorMetric ?metric . ccf:FM-APP-POOL-EXHAUSTED ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 3 行
- **备注**：区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）

#### `CQ-D-02`

- **CQ 文本**：给定同症状「应用连接获取失败」，能否用契约 discriminator 指标 `db.pool.session_used_ratio` 把 `FM-APP-POOL-EXHAUSTED` 与 `FM-DB-SESSION-POOL-FULL` 区分开（FM-DB-SESSION-POOL-FULL 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-DB-SESSION-POOL-FULL`、`ccm:m_db_pool_session_used_ratio`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-DB-SESSION-POOL-FULL` 与 `FM-APP-POOL-EXHAUSTED`；`ccc:discriminatorMetric` = `db.pool.session_used_ratio`）+ 探针 CQ-D-02
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric ?judge ?metricCode ?operandKind WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-DB-SESSION-POOL-FULL , ccf:FM-APP-POOL-EXHAUSTED ; ccc:discriminatorMetric ?metric . ccf:FM-DB-SESSION-POOL-FULL ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行
- **备注**：区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）

#### `CQ-D-03`

- **CQ 文本**：给定同症状「应用连不上数据库」，能否用契约 discriminator 指标 `app.datasource.connect_latency_ms` 把 `FM-CTR-NET-UNREACHABLE` 与 `FM-APP-AUTH-INCOMPATIBLE` 区分开（FM-CTR-NET-UNREACHABLE 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-CTR-NET-UNREACHABLE`、`ccm:m_app_datasource_connect_latency_ms`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-CTR-NET-UNREACHABLE` 与 `FM-APP-AUTH-INCOMPATIBLE`；`ccc:discriminatorMetric` = `app.datasource.connect_latency_ms`）+ 探针 CQ-D-03
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric ?judge ?metricCode ?operandKind WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-CTR-NET-UNREACHABLE , ccf:FM-APP-AUTH-INCOMPATIBLE ; ccc:discriminatorMetric ?metric . ccf:FM-CTR-NET-UNREACHABLE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行
- **备注**：区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）

#### `CQ-D-04`

- **CQ 文本**：给定同症状「应用连不上数据库」，能否用契约 discriminator 指标 `app.datasource.connect_latency_ms` 把 `FM-CTR-NET-UNREACHABLE` 与 `FM-APP-AUTH-INCOMPATIBLE` 区分开（FM-APP-AUTH-INCOMPATIBLE 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-AUTH-INCOMPATIBLE`、`ccm:m_app_datasource_connect_latency_ms`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-APP-AUTH-INCOMPATIBLE` 与 `FM-CTR-NET-UNREACHABLE`；`ccc:discriminatorMetric` = `app.datasource.connect_latency_ms`）+ 探针 CQ-D-04
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-APP-AUTH-INCOMPATIBLE , ccf:FM-CTR-NET-UNREACHABLE ; ccc:discriminatorMetric ?metric .  }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行
- **备注**：★ 契约缺陷 X-10：`FM-APP-AUTH-INCOMPATIBLE` 不在 `fault-modes.yaml` 的 14 模式词表内 ⇒ 该侧无判据个体可比对，探针降级为「区分器个体确实命名了该对 + 指向契约 discriminator 指标」

#### `CQ-D-05`

- **CQ 文本**：给定同症状「容器 OOM 被杀」，能否用契约 discriminator 指标 `host.mem_available_ratio` 把 `FM-HOST-MEM-PRESSURE` 与 `FM-APP-HEAP-PRESSURE` 区分开（FM-HOST-MEM-PRESSURE 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-HOST-MEM-PRESSURE`、`ccm:m_host_mem_available_ratio`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-HOST-MEM-PRESSURE` 与 `FM-APP-HEAP-PRESSURE`；`ccc:discriminatorMetric` = `host.mem_available_ratio`）+ 探针 CQ-D-05
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric ?judge ?metricCode ?operandKind WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-HOST-MEM-PRESSURE , ccf:FM-APP-HEAP-PRESSURE ; ccc:discriminatorMetric ?metric . ccf:FM-HOST-MEM-PRESSURE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 1 行
- **备注**：区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）

#### `CQ-D-06`

- **CQ 文本**：给定同症状「容器 OOM 被杀」，能否用契约 discriminator 指标 `host.mem_available_ratio` 把 `FM-HOST-MEM-PRESSURE` 与 `FM-APP-HEAP-PRESSURE` 区分开（FM-APP-HEAP-PRESSURE 侧判据）？
- **对应场景**：C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）
- **需要哪些类-关系-指标**：`ccf:FM-APP-HEAP-PRESSURE`、`ccm:m_host_mem_available_ratio`、`ccf:jMetricCode`、`ccf:jOperandKind`、`ccc:Discriminator`、`ccc:discriminatorMetric`、`ccc:discriminates`
- **由哪条规则或查询回答**：`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `FM-APP-HEAP-PRESSURE` 与 `FM-HOST-MEM-PRESSURE`；`ccc:discriminatorMetric` = `host.mem_available_ratio`）+ 探针 CQ-D-06
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?disc ?metric ?judge ?metricCode ?operandKind WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:FM-APP-HEAP-PRESSURE , ccf:FM-HOST-MEM-PRESSURE ; ccc:discriminatorMetric ?metric . ccf:FM-APP-HEAP-PRESSURE ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 2 行
- **备注**：区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）

#### `CQ-D-07`

- **CQ 文本**：能否用「某指标正常」作为**否定性证据**排除某个根因（契约的排除要求）？
- **对应场景**：C4 否定性证据（`N-*` 用例；F5 排除法）
- **需要哪些类-关系-指标**：`ccf:exclusionRequirementText`、`ccf:jRole`、`ccf:judgedBy`
- **由哪条规则或查询回答**：`ccf:J-*` 判据个体上的 `ccf:exclusionRequirementText`（真机 4 条）+ 探针 CQ-D-07
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?judge ?text WHERE { ?judge ccf:exclusionRequirementText ?text }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 4 行

#### `CQ-D-08`

- **CQ 文本**：给定一个症状，能否枚举**全部**可解释它的候选故障模式及其判据数（供互斥排除）？
- **对应场景**：C3 同症状区分（`D-*` 用例；候选集枚举）
- **需要哪些类-关系-指标**：`ccf:FaultMode`、`ccf:judgedBy`、`ccf:faultModeCode`、`ccf:fmComponentType`
- **由哪条规则或查询回答**：SPARQL 探针 CQ-D-08（按模式分组计数判据）
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?mode (COUNT(?j) AS ?n) WHERE { ?m a ccf:FaultMode ; ccf:faultModeCode ?mode ; ccf:judgedBy ?j } GROUP BY ?mode
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 23 行

### 3.6 审计与溯源（4 条）

#### `CQ-A-01`

- **CQ 文本**：给定一条 `Finding`，能否回溯到它的规则、规则输入指标与产出的动作提案（决策链可复算）？
- **对应场景**：对照维度「审计性」+ 内部校验（`docs/04` §8 `T5` 的审计链部分）
- **需要哪些类-关系-指标**：`ccf:Finding`、`ccp:RuleExecution`、`ccp:producedFinding`、`ccp:hasRuleInput`、`ccp:proposedAction`、`ccp:appliesRule`
- **由哪条规则或查询回答**：`cc-audit.ttl` 的 7 个 `ccp:Exec-r01..r07` + 探针 CQ-A-01
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?exec ?rule ?f ?metric ?action WHERE { ?exec a ccp:RuleExecution ; ccp:appliesRule ?rule ; ccp:producedFinding ?f ; ccp:hasRuleInput ?metric . OPTIONAL { ?exec ccp:proposedAction ?action } }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 16 行

#### `CQ-A-02`

- **CQ 文本**：给定一份 ABox，能否把任一实体上的一条观测结论回溯到它的 CCF 记录 id、采集来源与采集时间？
- **对应场景**：对照维度「审计性」+ 银行审计的确定性要求（`docs/04:123`）
- **需要哪些类-关系-指标**：`cc:hasEvidence`、`cc:Evidence`、`cc:recordId`、`cc:probe`、`cc:capturedAt`
- **由哪条规则或查询回答**：`ccp:` / PROV-O 对称落地的 `cc:hasEvidence` + 探针 CQ-A-02
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?s ?ev ?rid ?probe ?t WHERE { ?s cc:hasEvidence ?ev . ?ev cc:recordId ?rid ; cc:probe ?probe ; cc:capturedAt ?t }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 191 行

#### `CQ-A-03`

- **CQ 文本**：给定一条因果边，能否给回它在契约中的出处（`causal-edges.yaml#edges[id=Ex]`）？
- **对应场景**：内部校验（`docs/04` §8 `T4`：因果边 provenance 的 IRI ∈ 契约）
- **需要哪些类-关系-指标**：`ccc:CausalLink`、`ccc:provenance`、`ccc:ContractProvenance`、`ccc:provenanceEdgeId`、`ccc:provenanceDocument`
- **由哪条规则或查询回答**：18 个 `ccc:ContractProvenance`（`prov-E1..E18`，携带 `ccc:provenanceEdgeId` / `ccc:provenanceDocument` / `ccc:provenanceToken`）+ 探针 CQ-A-03
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?link ?prov ?edgeId ?doc WHERE { ?link a ccc:CausalLink ; ccc:provenance ?prov . ?prov a ccc:ContractProvenance ; ccc:provenanceEdgeId ?edgeId ; ccc:provenanceDocument ?doc }
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 26 行

#### `CQ-A-04`

- **CQ 文本**：能否证明「每条规则都有执行记录、每条执行都有产出、每个产出都归到一条规则」（发布后可审计的闭环）？
- **对应场景**：对照维度「审计性」+ 内部校验（`docs/04` §8 `T2`/`T8`）
- **需要哪些类-关系-指标**：`ccp:Rule`、`ccp:RuleExecution`、`ccf:Finding`、`ccp:associatedRule`、`cca:RiskLevel`
- **由哪条规则或查询回答**：`cc-audit.ttl`（7 规则 / 7 执行 / 7 发现）+ 探针 CQ-A-04
- **期望**：`nonempty`
- **探针**：

```sparql
SELECT ?rule (COUNT(?f) AS ?n) WHERE { ?rule a ccp:Rule . ?f a ccf:Finding ; ccp:associatedRule ?rule } GROUP BY ?rule
```

- **实测（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）**：**PASS** — 7 行

## 4. 词汇表（契约派生，逐条可追溯 `code`）

### 4.1 类型（14 = 13 具体 + 派生 `Evidence`）

| # | 契约 code | 层 | abstract | 契约键属性 | 本体类 | 本体属性数 |
|---|---|---|---|---|---|---|
| 1 | `AppEndpoint` | App | 否 | `path`、`protocol` | `cc:AppEndpoint` | 5 |
| 2 | `AppInstance` | App | 否 | `instanceId` | `cc:AppInstance` | 5 |
| 3 | `ConnectionPool` | Data | 是 | — | `cc:ConnectionPool` | 0 |
| 4 | `Container` | Runtime | 否 | `containerId` | `cc:Container` | 8 |
| 5 | `CreditCardApp` | App | 否 | `appCode` | `cc:CreditCardApp` | 6 |
| 6 | `DatabaseSessionPool` | Data | 否 | `poolId` | `cc:DatabaseSessionPool` | 4 |
| 7 | `Evidence` | None | 否 | `recordId` | `cc:Evidence` | 12 |
| 8 | `GaussDBCluster` | Data | 否 | `clusterName` | `cc:GaussDBCluster` | 5 |
| 9 | `GaussDBInstance` | Data | 否 | `instanceId` | `cc:GaussDBInstance` | 6 |
| 10 | `Host` | Runtime | 否 | `hostname` | `cc:Host` | 8 |
| 11 | `JdbcDataSource` | App | 否 | `dsCode` | `cc:JdbcDataSource` | 13 |
| 12 | `Query` | Data | 否 | `queryId` | `cc:Query` | 5 |
| 13 | `Session` | Data | 否 | `sessionId` | `cc:Session` | 5 |
| 14 | `Table` | Data | 否 | `schemaName`、`tableName` | `cc:Table` | 4 |
| — | （派生）`Evidence` | 审计 | 否 | 8 个溯源属性 | `cc:Evidence`（⊑ `cco:Continuant`） | 8 |

### 4.2 对象属性（本体实际落地，含域/值域/逆）

| # | 属性 | rdfs:domain | rdfs:range | owl:inverseOf | 备注 |
|---|---|---|---|---|---|
| 1 | `cca:actionOnFault` | `cca:Action` | `_(ccf:FaultMode ∪ ccf:RootCause)` | — | — |
| 2 | `cca:actionProposals` | `cca:Action` | `cca:ActionProposal` | — | — |
| 3 | `cca:actionRisk` | `cca:Action` | `cca:RiskLevel` | — | — |
| 4 | `cca:findingProposals` | `ccf:Finding` | `cca:ActionProposal` | — | — |
| 5 | `cca:proposalAction` | `cca:ActionProposal` | `cca:Action` | cca:actionProposals | — |
| 6 | `cca:proposalForFinding` | `cca:ActionProposal` | `ccf:Finding` | cca:findingProposals | — |
| 7 | `ccc:cause` | `ccc:CausalLink` | `ccf:FaultMode` | — | — |
| 8 | `ccc:condEntityProperty` | `ccc:Condition` | `http://www.w3.org/2002/07/owl#DatatypeProperty` | — | — |
| 9 | `ccc:condMetric` | `ccc:Condition` | `ccm:Metric` | — | — |
| 10 | `ccc:condThreshold` | `ccc:Condition` | `ccm:Threshold` | — | — |
| 11 | `ccc:condition` | `ccc:CausalLink` | `ccc:Condition` | ccc:conditionOf | — |
| 12 | `ccc:conditionOf` | `ccc:Condition` | `ccc:CausalLink` | — | — |
| 13 | `ccc:discriminates` | `ccc:Discriminator` | `_(ccf:FaultMode ∪ ccf:RootCause)` | — | — |
| 14 | `ccc:discriminatorMetric` | `ccc:Discriminator` | `ccm:Metric` | — | — |
| 15 | `ccc:effect` | `ccc:CausalLink` | `ccf:FaultMode` | — | — |
| 16 | `ccc:hasLink` | `ccc:Chain` | `ccc:CausalLink` | — | — |
| 17 | `ccc:inChain` | `ccc:CausalLink` | `ccc:Chain` | ccc:hasLink | — |
| 18 | `ccc:provenance` | `ccc:CausalLink` | `ccc:ContractProvenance` | — | — |
| 19 | `ccc:relatesTo` | `_(ccf:FaultMode ∪ ccf:RootCause)` | `_(ccf:FaultMode ∪ ccf:RootCause)` | — | — |
| 20 | `ccf:affects` | `ccf:FaultMode` | `http://www.w3.org/2002/07/owl#Class` | — | — |
| 21 | `ccf:fmComponentType` | `ccf:FaultMode` | `http://www.w3.org/2002/07/owl#Class` | — | — |
| 22 | `ccf:hasDistinguishingEvidence` | `ccf:FaultMode` | `ccf:Judgment` | — | — |
| 23 | `ccf:hasJudgment` | `ccf:JudgmentGroup` | `ccf:Judgment` | ccf:judgmentGroupOf | — |
| 24 | `ccf:hasJudgmentGroup` | `ccf:FaultMode` | `ccf:JudgmentGroup` | — | — |
| 25 | `ccf:hasRootCause` | `ccf:FaultMode` | `ccf:RootCause` | — | — |
| 26 | `ccf:jMetric` | `ccf:Judgment` | `ccm:Metric` | — | — |
| 27 | `ccf:jOperandEntityProperty` | `ccf:Judgment` | `http://www.w3.org/2002/07/owl#DatatypeProperty` | — | — |
| 28 | `ccf:jThreshold` | `ccf:Judgment` | `ccm:Threshold` | — | — |
| 29 | `ccf:judgedBy` | `ccf:FaultMode` | `ccf:Judgment` | ccf:judges | — |
| 30 | `ccf:judges` | `ccf:Judgment` | `ccf:FaultMode` | — | — |
| 31 | `ccf:judgmentGroupOf` | `ccf:Judgment` | `ccf:JudgmentGroup` | — | — |
| 32 | `ccf:rootCauseOf` | `ccf:RootCause` | `ccf:FaultMode` | ccf:hasRootCause | — |
| 33 | `ccf:rootCauseRequirement` | `ccf:RootCause` | `ccf:Judgment` | — | — |
| 34 | `ccm:criticalTier` | `ccm:Threshold` | `ccm:CriticalThreshold` | — | — |
| 35 | `ccm:hasThreshold` | `ccm:Metric` | `ccm:Threshold` | ccm:thresholdMetric | — |
| 36 | `ccm:metricObjectType` | `ccm:Metric` | `http://www.w3.org/2002/07/owl#Class` | — | — |
| 37 | `ccm:metricProperty` | `ccm:Metric` | `http://www.w3.org/2002/07/owl#DatatypeProperty` | — | — |
| 38 | `ccm:observedEntity` | `ccm:MetricObservation` | `cco:Entity` | — | — |
| 39 | `ccm:observedMetric` | `ccm:MetricObservation` | `ccm:Metric` | — | — |
| 40 | `ccm:thresholdMetric` | `ccm:Threshold` | `ccm:Metric` | — | — |
| 41 | `ccm:tierOf` | `ccm:Threshold` | `ccm:Threshold` | — | — |
| 42 | `ccm:warningTier` | `ccm:Threshold` | `ccm:WarningThreshold` | — | — |
| 43 | `ccp:appliesRule` | `ccp:RuleExecution` | `ccp:Rule` | — | — |
| 44 | `ccp:associatedRule` | `ccf:Finding` | `ccp:Rule` | — | — |
| 45 | `ccp:detectsFaultMode` | `ccp:Rule` | `ccf:FaultMode` | — | — |
| 46 | `ccp:findingFaultMode` | `ccf:Finding` | `ccf:FaultMode` | — | — |
| 47 | `ccp:findingFromExecution` | `ccf:Finding` | `ccp:RuleExecution` | — | — |
| 48 | `ccp:hasRuleInput` | `ccp:RuleExecution` | `ccm:Metric` | — | — |
| 49 | `ccp:producedFinding` | `ccp:RuleExecution` | `ccf:Finding` | ccp:findingFromExecution | — |
| 50 | `ccp:proposedAction` | `ccp:RuleExecution` | `cca:ActionProposal` | — | — |
| 51 | `ccp:ruleProducesFinding` | `ccp:Rule` | `ccf:Finding` | — | — |
| 52 | `ccp:unjudgeableFor` | `cco:InformationEntity` | `cco:Entity` | — | — |
| 53 | `ccp:unjudgeableJudgment` | `cco:InformationEntity` | `ccf:Judgment` | — | — |
| 54 | `ccp:unjudgeableRule` | `cco:InformationEntity` | `ccp:Rule` | — | — |
| 55 | `cc:acceptsConnectionFrom` | `cc:GaussDBCluster` | `cc:JdbcDataSource` | — | — |
| 56 | `cc:accesses` | `cc:Query` | `cc:Table` | — | — |
| 57 | `cc:connectsTo` | `cc:JdbcDataSource` | `cc:GaussDBCluster` | cc:acceptsConnectionFrom | — |
| 58 | `cc:deployedOn` | `cc:Container` | `cc:Host` | cc:hostsContainer | — |
| 59 | `cc:exposes` | `cc:CreditCardApp` | `cc:AppEndpoint` | — | — |
| 60 | `cc:hasAppInstance` | `cc:CreditCardApp` | `cc:AppInstance` | cc:instanceOf | — |
| 61 | `cc:hasDbInstance` | `cc:GaussDBCluster` | `cc:GaussDBInstance` | — | — |
| 62 | `cc:hasEvidence` | — | `cc:Evidence` | — | — |
| 63 | `cc:hasSession` | `cc:DatabaseSessionPool` | `cc:Session` | — | — |
| 64 | `cc:hasSessionPool` | `cc:GaussDBInstance` | `cc:DatabaseSessionPool` | — | — |
| 65 | `cc:hosts` | `cc:Host` | `cc:GaussDBInstance` | — | — |
| 66 | `cc:hostsContainer` | `cc:Host` | `cc:Container` | — | — |
| 67 | `cc:indirectlyHostedOn` | `cc:AppInstance` | `cc:Host` | — | propertyChain |
| 68 | `cc:instanceOf` | `cc:AppInstance` | `cc:CreditCardApp` | — | — |
| 69 | `cc:replicatesTo` | `cc:GaussDBInstance` | `cc:GaussDBInstance` | — | — |
| 70 | `cc:resolvesTo` | `cc:JdbcDataSource` | `cc:GaussDBInstance` | — | — |
| 71 | `cc:runs` | `cc:Container` | `cc:AppInstance` | — | — |
| 72 | `cc:runsIn` | `cc:AppInstance` | `cc:Container` | cc:runs | — |
| 73 | `cc:runsQuery` | `cc:Session` | `cc:Query` | — | — |
| 74 | `cc:usesDataSource` | `cc:AppInstance` | `cc:JdbcDataSource` | — | — |

契约的 15 条关系（14 业务 + 派生 `HAS_EVIDENCE`）在内，另有本模块补充的对象属性（如 `cc:indirectlyHostedOn` 属性链、`cc:hasEvidence` 对称落地）。 ★ 命名空间口径：本表属性名与定义域/值域一律取 `shared/mapping/type-map.yaml` 的前缀表；表内 ⇒ `前缀:局部名`；表外但有 IRI 的（本表 6 格：`http://www.w3.org/2002/07/owl#Class`、`#DatatypeProperty`）⇒ 完整 IRI，不臆造前缀；4 格为 `owl:unionOf` 匿名类（类本身无 IRI）⇒ 展开其成员列表 `_(ccf:FaultMode ∪ ccf:RootCause)`（∪ 连接、成员按字典序，与 `scheme2-onto/ontology/cc-action.ttl`、`cc-causal.ttl` 的 `owl:unionOf` 逐项一致；建议契约侧把 `owl` 补进前缀表）。

### 4.3 指标（58，`ccm:` 命名空间，逐条对应 `metrics.yaml`）

| # | 指标 code | 本体属性 | xsd | 阈值档（`threshold_ref`）|
|---|---|---|---|---|
| 1 | `app.datasource.acquire_wait_ms` | `ccm:acquireWaitMs` | xsd:decimal | `threshold:acquire_wait_ms_light` |
| 2 | `app.datasource.active_connections` | `ccm:activeConnections` | xsd:integer | — |
| 3 | `app.datasource.connect_fail_total` | `ccm:connectFailTotal` | xsd:integer | — |
| 4 | `app.datasource.connect_latency_ms` | `ccm:connectLatencyMs` | xsd:decimal | — |
| 5 | `app.datasource.idle_connections` | `ccm:idleConnections` | xsd:integer | — |
| 6 | `app.datasource.pending_threads` | `ccm:pendingThreads` | xsd:integer | — |
| 7 | `app.datasource.timeout_total` | `ccm:timeoutTotal` | xsd:integer | — |
| 8 | `app.endpoint.error_rate` | `ccm:errorRate` | xsd:decimal | — |
| 9 | `app.endpoint.fail_total` | `ccm:failTotal` | xsd:integer | — |
| 10 | `app.endpoint.max_ms` | `ccm:maxMs` | xsd:decimal | — |
| 11 | `app.endpoint.p50_ms` | `ccm:p50Ms` | xsd:decimal | — |
| 12 | `app.endpoint.p95_ms` | `ccm:p95Ms` | xsd:decimal | — |
| 13 | `app.endpoint.p99_ms` | `ccm:p99Ms` | xsd:decimal | — |
| 14 | `app.endpoint.qps` | `ccm:qps` | xsd:decimal | `threshold:qps_high` |
| 15 | `app.endpoint.success_total` | `ccm:successTotal` | xsd:integer | — |
| 16 | `app.endpoint.tps` | `ccm:tps` | xsd:decimal | — |
| 17 | `app.gc_pause_ms` | `ccm:gcPauseMs` | xsd:decimal | `threshold:gc_pause_ms_light` |
| 18 | `app.heap_used_ratio` | `ccm:heapUsedRatio` | xsd:decimal | `threshold:heap_used_ratio_light` |
| 19 | `app.thread_count` | `ccm:threadCount` | xsd:integer | — |
| 20 | `app.uptime_s` | `ccm:uptimeS` | xsd:integer | — |
| 21 | `container.cpu_throttled_periods` | `ccm:cpuThrottledPeriods` | xsd:integer | — |
| 22 | `container.cpu_usage_ratio` | `ccm:cpuUsageRatio` | xsd:decimal | — |
| 23 | `container.mem_working_set_mb` | `ccm:memWorkingSetMb` | xsd:integer | — |
| 24 | `container.oom_kill_total` | `ccm:oomKillTotal` | xsd:integer | — |
| 25 | `container.restart_total` | `ccm:restartTotal` | xsd:integer | — |
| 26 | `container.status` | `ccm:status` | xsd:string | — |
| 27 | `db.cluster.alert_active_count` | `ccm:alertActiveCount` | xsd:integer | — |
| 28 | `db.cluster.deadlocks` | `ccm:deadlocks` | xsd:integer | — |
| 29 | `db.cluster.down_nodes` | `ccm:downNodes` | xsd:integer | — |
| 30 | `db.cluster.is_standby` | `ccm:isStandby` | xsd:boolean | — |
| 31 | `db.cluster.replay_lag_bytes_max` | `ccm:replayLagBytesMax` | xsd:integer | — |
| 32 | `db.cluster.replication_flush_pending_bytes` | `ccm:replicationFlushPendingBytes` | xsd:integer | — |
| 33 | `db.cluster.replication_lag_mb` | `ccm:replicationLagMb` | xsd:decimal | — |
| 34 | `db.cluster.replication_not_streaming_count` | `ccm:replicationNotStreamingCount` | xsd:integer | — |
| 35 | `db.cluster.replication_pending_bytes` | `ccm:replicationPendingBytes` | xsd:integer | — |
| 36 | `db.cluster.replication_slot_active_count` | `ccm:replicationSlotActiveCount` | xsd:integer | — |
| 37 | `db.cluster.replication_slot_count` | `ccm:replicationSlotCount` | xsd:integer | — |
| 38 | `db.cluster.slow_query_count` | `ccm:slowQueryCount` | xsd:integer | — |
| 39 | `db.cluster.switchover_total` | `ccm:switchoverTotal` | xsd:integer | — |
| 40 | `db.cluster.temp_file_written` | `ccm:tempFileWritten` | xsd:boolean | — |
| 41 | `db.cluster.temp_files` | `ccm:tempFiles` | xsd:integer | — |
| 42 | `db.cluster.up_nodes` | `ccm:upNodes` | xsd:integer | — |
| 43 | `db.cluster.walsender_count` | `ccm:walsenderCount` | xsd:integer | — |
| 44 | `db.cluster.xact_rollback` | `ccm:xactRollback` | xsd:integer | — |
| 45 | `db.instance.active_sessions` | `ccm:activeSessions` | xsd:integer | — |
| 46 | `db.instance.buffer_hit_ratio` | `ccm:bufferHitRatio` | xsd:decimal | — |
| 47 | `db.instance.cpu_usage_ratio` | `ccm:cpuUsageRatio` | xsd:decimal | — |
| 48 | `db.instance.idle_in_xact` | `ccm:idleInXact` | xsd:integer | — |
| 49 | `db.instance.io_wait_pct` | `ccm:ioWaitPct` | xsd:decimal | — |
| 50 | `db.instance.lock_waits` | `ccm:lockWaits` | xsd:integer | — |
| 51 | `db.instance.long_txn_age_s` | `ccm:longTxnAgeS` | xsd:integer | — |
| 52 | `db.instance.max_connections` | `ccm:maxConnections` | xsd:integer | — |
| 53 | `db.instance.mem_usage_ratio` | `ccm:memUsageRatio` | xsd:decimal | — |
| 54 | `db.instance.session_count` | `ccm:sessionCount` | xsd:integer | — |
| 55 | `db.instance.up` | `ccm:up` | xsd:boolean | — |
| 56 | `db.instance.waiting_connections` | `ccm:waitingConnections` | xsd:integer | — |
| 57 | `db.pool.conn_rejected_total` | `ccm:connRejectedTotal` | xsd:integer | — |
| 58 | `db.pool.session_used_ratio` | `ccm:sessionUsedRatio` | xsd:decimal | `threshold:session_used_ratio_light`、`threshold:session_used_ratio_severe` |
| 59 | `db.pool.session_waiting` | `ccm:sessionWaiting` | xsd:integer | — |
| 60 | `db.query.exec_count` | `ccm:execCount` | xsd:integer | — |
| 61 | `db.query.max_exec_ms` | `ccm:maxExecMs` | xsd:decimal | — |
| 62 | `db.query.mean_exec_ms` | `ccm:meanExecMs` | xsd:decimal | `threshold:mean_exec_ms_light` |
| 63 | `db.query.returned_rows` | `ccm:returnedRows` | xsd:integer | — |
| 64 | `db.query.seq_scan_ratio` | `ccm:seqScanRatio` | xsd:decimal | — |
| 65 | `db.session.query_elapsed_s` | `ccm:queryElapsedS` | xsd:decimal | — |
| 66 | `db.session.state` | `ccm:state` | xsd:string | — |
| 67 | `db.session.wait_event` | `ccm:waitEvent` | xsd:string | — |
| 68 | `db.session.xact_age_s` | `ccm:xactAgeS` | xsd:integer | `threshold:xact_age_s_light` |
| 69 | `db.table.dead_tuple_ratio` | `ccm:deadTupleRatio` | xsd:decimal | `threshold:dead_tuple_ratio_light` |
| 70 | `db.table.dead_tuples` | `ccm:deadTuples` | xsd:integer | — |
| 71 | `db.table.hot_update_ratio_pct` | `ccm:hotUpdateRatioPct` | xsd:decimal | — |
| 72 | `db.table.idx_scan` | `ccm:idxScan` | xsd:integer | — |
| 73 | `db.table.index_count` | `ccm:indexCount` | xsd:integer | — |
| 74 | `db.table.last_analyze_age_h` | `ccm:lastAnalyzeAgeH` | xsd:decimal | — |
| 75 | `db.table.live_tuples` | `ccm:liveTuples` | xsd:integer | — |
| 76 | `db.table.seq_scan` | `ccm:seqScan` | xsd:integer | — |
| 77 | `db.table.size_mb` | `ccm:sizeMb` | xsd:decimal | — |
| 78 | `db.table.unused_index_count` | `ccm:unusedIndexCount` | xsd:integer | — |
| 79 | `host.conntrack_count` | `ccm:conntrackCount` | xsd:integer | — |
| 80 | `host.cpu_usage_ratio` | `ccm:cpuUsageRatio` | xsd:decimal | `threshold:cpu_usage_ratio_severe` |
| 81 | `host.disk_io_errors_total` | `ccm:diskIoErrorsTotal` | xsd:integer | — |
| 82 | `host.disk_io_wait` | `ccm:diskIoWait` | xsd:decimal | — |
| 83 | `host.disk_util` | `ccm:diskUtil` | xsd:decimal | — |
| 84 | `host.fs_max_used_pct` | `ccm:fsMaxUsedPct` | xsd:decimal | `threshold:fs_usage_pct_light` |
| 85 | `host.load1` | `ccm:load1` | xsd:decimal | `threshold:load1_per_core_light` |
| 86 | `host.load5` | `ccm:load5` | xsd:decimal | — |
| 87 | `host.mem_available_ratio` | `ccm:memAvailableRatio` | xsd:decimal | `threshold:mem_available_ratio_severe` |
| 88 | `host.net_iface_down_count` | `ccm:netIfaceDownCount` | xsd:integer | — |
| 89 | `host.net_iface_total` | `ccm:netIfaceTotal` | xsd:integer | — |
| 90 | `host.net_rx_bytes` | `ccm:netRxBytes` | xsd:integer | — |
| 91 | `host.net_tx_bytes` | `ccm:netTxBytes` | xsd:integer | — |

### 4.4 故障模式（23）与判据

| # | code | 层 | 组件类型 | severity | 检测方式 | detect 判据数 | 规则归属 |
|---|---|---|---|---|---|---|---|
| 1 | `FM-APP-DS-CONNECT-FAIL` | App | JdbcDataSource | critical | — | 1 | —（契约只给 7 条规则） |
| 2 | `FM-APP-ENDPOINT-SLOW` | App | AppEndpoint | major | — | 1 | —（契约只给 7 条规则） |
| 3 | `FM-APP-HEAP-PRESSURE` | App | AppInstance | warning | — | 2 | —（契约只给 7 条规则） |
| 4 | `FM-APP-INSTANCE-RESTART` | App | AppInstance | major | — | 2 | —（契约只给 7 条规则） |
| 5 | `FM-APP-POOL-EXHAUSTED` | App | JdbcDataSource | critical | — | 3 | `r01.rq` |
| 6 | `FM-CTR-CPU-THROTTLED` | Runtime | Container | major | — | 1 | `r03.rq` |
| 7 | `FM-CTR-NET-UNREACHABLE` | Runtime | Container | critical | — | 2 | `r06.rq` |
| 8 | `FM-CTR-OOM-KILLED` | Runtime | Container | critical | — | 1 | `r04.rq` |
| 9 | `FM-DB-DEADLOCK` | Data | GaussDBCluster | critical | — | 1 | `r02.rq` |
| 10 | `FM-DB-IDLE-IN-TX` | Data | GaussDBInstance | major | — | 1 | `r02.rq` |
| 11 | `FM-DB-INSTANCE-DOWN` | Data | GaussDBInstance | critical | — | 2 | `r07.rq` |
| 12 | `FM-DB-LOCK-WAIT` | Data | Session | major | — | 2 | —（契约只给 7 条规则） |
| 13 | `FM-DB-REPLICATION-LAG` | Data | GaussDBCluster | critical | — | 1 | `r07.rq` |
| 14 | `FM-DB-ROLLBACK-STORM` | Data | GaussDBCluster | major | — | 1 | `r02.rq` |
| 15 | `FM-DB-SEQ-SCAN` | Data | Table | major | — | 1 | `r02.rq` |
| 16 | `FM-DB-SESSION-POOL-FULL` | Data | DatabaseSessionPool | critical | — | 2 | —（契约只给 7 条规则） |
| 17 | `FM-DB-SLOW-QUERY` | Data | Query | major | — | 1 | `r02.rq` |
| 18 | `FM-DB-UNUSED-INDEX` | Data | Table | major | — | 1 | `r02.rq` |
| 19 | `FM-HOST-CPU-CONTENTION` | Runtime | Host | major | — | 2 | —（契约只给 7 条规则） |
| 20 | `FM-HOST-DISK-FULL` | Runtime | Host | critical | — | 1 | `r04.rq` |
| 21 | `FM-HOST-DISK-IO-ERROR` | Runtime | Host | critical | — | 1 | `r04.rq` |
| 22 | `FM-HOST-MEM-PRESSURE` | Runtime | Host | major | — | 1 | —（契约只给 7 条规则） |
| 23 | `FM-HOST-NET-IFACE-DOWN` | Runtime | Host | critical | — | 1 | `r06.rq` |

### 4.5 因果边（18，契约 `causal-edges.yaml`）

| id | relation | cause (`from`) | effect (`to`) | chain | 条件 | provenance |
|---|---|---|---|---|---|---|
| `E1` | causes | `FM-DB-SLOW-QUERY` | `FM-APP-POOL-EXHAUSTED` | F2 | — | authored |
| `E2` | causes | `FM-DB-LOCK-WAIT` | `FM-DB-SLOW-QUERY` | F2 | — | authored |
| `E3` | causes | `FM-DB-SESSION-POOL-FULL` | `FM-APP-DS-CONNECT-FAIL` | F2 | — | authored |
| `E4` | causes | `FM-DB-SLOW-QUERY` | `FM-DB-SESSION-POOL-FULL` | F2 | — | authored |
| `E5` | causes | `FM-APP-POOL-EXHAUSTED` | `FM-APP-ENDPOINT-SLOW` | F1 | — | authored |
| `E6` | conditional_causes | `FM-APP-ENDPOINT-SLOW` | `FM-APP-POOL-EXHAUSTED` | F1 | `app.endpoint.qps > threshold:qps_high#critical` | authored |
| `E7` | conditional_causes | `FM-HOST-CPU-CONTENTION` | `FM-CTR-CPU-THROTTLED` | F3 | `host.cpu_usage_ratio > threshold:cpu_usage_ratio_severe#critical` | authored |
| `E8` | causes | `FM-CTR-CPU-THROTTLED` | `FM-APP-ENDPOINT-SLOW` | F3 | — | authored |
| `E9` | conditional_causes | `FM-HOST-MEM-PRESSURE` | `FM-CTR-OOM-KILLED` | F4 | `host.mem_available_ratio < threshold:mem_available_ratio_severe#critical` | authored |
| `E10` | causes | `FM-CTR-OOM-KILLED` | `FM-APP-INSTANCE-RESTART` | F4 | — | authored |
| `E11` | causes | `FM-APP-INSTANCE-RESTART` | `FM-APP-ENDPOINT-SLOW` | F4 | — | authored |
| `E12` | conditional_causes | `FM-APP-HEAP-PRESSURE` | `FM-CTR-OOM-KILLED` | F4 | `container.mem_working_set_mb > entity_property:Container.memLimitMb` | authored |
| `E13` | causes | `FM-CTR-NET-UNREACHABLE` | `FM-APP-DS-CONNECT-FAIL` | F6 | — | authored |
| `E14` | causes | `FM-DB-INSTANCE-DOWN` | `FM-APP-DS-CONNECT-FAIL` | F7 | — | authored |
| `E15` | causes | `FM-DB-INSTANCE-DOWN` | `FM-DB-SESSION-POOL-FULL` | F7 | — | authored |
| `E16` | relates_to | `FM-DB-SLOW-QUERY` | `FM-DB-LOCK-WAIT` | F2 | — | authored |
| `E17` | relates_to | `FM-APP-HEAP-PRESSURE` | `FM-HOST-MEM-PRESSURE` | F4 | — | authored |
| `E18` | causes | `FM-APP-DS-CONNECT-FAIL` | `FM-APP-ENDPOINT-SLOW` | F5、F6、F7 | — | authored |
| `E19` | causes | `FM-DB-DEADLOCK` | `FM-DB-SLOW-QUERY` | F2 | — | authored |
| `E20` | causes | `FM-DB-IDLE-IN-TX` | `FM-DB-LOCK-WAIT` | F2 | — | authored |
| `E21` | causes | `FM-DB-SEQ-SCAN` | `FM-DB-SLOW-QUERY` | F2 | — | authored |
| `E22` | causes | `FM-DB-ROLLBACK-STORM` | `FM-DB-SLOW-QUERY` | F2 | — | authored |
| `E23` | causes | `FM-HOST-DISK-FULL` | `FM-DB-INSTANCE-DOWN` | F4 | — | authored |
| `E24` | causes | `FM-HOST-DISK-IO-ERROR` | `FM-DB-INSTANCE-DOWN` | F4 | — | authored |
| `E25` | causes | `FM-HOST-NET-IFACE-DOWN` | `FM-CTR-NET-UNREACHABLE` | F6 | — | authored |
| `E26` | causes | `FM-DB-REPLICATION-LAG` | `FM-APP-DS-CONNECT-FAIL` | F7 | — | authored |
| `E27` | causes | `FM-DB-UNUSED-INDEX` | `FM-DB-SLOW-QUERY` | F2 | — | authored |

## 5. 本体模块与 CQ 的对应（`M2.5`）

| 模块 | 覆盖的 CQ 族 | 关键公理（方案2 独有表达力）|
|---|---|---|
| `cc-core.ttl` | 全部（前置） | 顶层类层次 + OntoClean 元属性（`cco:rigidity/identity/unity/dependence`，相容规则 R1–R4 由 T12 自检） |
| `cc-topology.ttl` | CQ-T / CQ-X | `owl:inverseOf`、`owl:disjointWith`、`owl:propertyChainAxiom ( cc:runsIn cc:deployedOn )`、`owl:hasKey` |
| `cc-metric.ttl` | CQ-T / CQ-F / CQ-D | 58 指标 + 12 档阈值档位个体（`ccm:WarningThreshold` / `ccm:CriticalThreshold`）+ `ccm:uncalibrated` 未标定语义 |
| `cc-fault.ttl` | CQ-F / CQ-D | 23 模式 + `ccf:JudgmentGroup`（`ccf:groupKind` 取值 `all_of` / `any_of`，组间 AND）+ `ccf:jOperandKind`（metric_value / entity_property / threshold_reference / derived）|
| `cc-causal.ttl` | CQ-C / CQ-D / CQ-A-03 | n 元物化 `ccc:CausalLink` + `ccc:Condition`（一等公民）+ `ccc:ContractProvenance`（prov-E1..E18）+ `ccc:Chain` / `ccc:Discriminator` |
| `cc-action.ttl` | CQ-A | `cca:ActionProposal`（`executed=false` / `dryRun=true`）+ `cca:RiskLevel` |
| `cc-audit.ttl` | CQ-A | `ccp:RuleExecution ⊑ prov:Activity`、`ccp:ProbeAgent ⊑ prov:Agent`、`ccf:Finding ⊑ prov:Entity`（PROV-O 对齐，无远端 import） |

## 6. 诚实清单：偏差、缺口、与施工者自定项

### 6.1 契约缺陷（**只报告，不就地修改**）

| # | 契约问题 | 本体的处置 |
|---|---|---|
| X-1 | `docs/04` §6 前言的 `cct:` / `cco#obs` 前缀不在 `type-map.yaml` 的 `namespaces` 允许集内。 | **已按载体落地**：`docs/04` §6 已改写为只声明映射表允许的前缀，阈值/档位个体落 `ccm:t_<阈值名>` 与 `ccm:t_<阈值名>_warning` / `_critical`（载体 `shared/mapping/type-map.yaml:66-75`；落地件 `scheme2-onto/ontology/cc-metric.ttl`）。 |
| X-2 | `docs/04` §6 的 r02 示例体含数字字面量，并引用非该模式 detect 行的 `db.table.dead_tuple_ratio`。 | **已按载体落地**：`docs/04` §6 的 r02 示例已重写为与 `shared/model/fault-modes.yaml:204-214` 的 `FM-DB-SLOW-QUERY` detect 逐字一致（`all_of` 仅 1 条，零数字字面量，不再引用 `db.table.dead_tuple_ratio`）；实现件 `scheme2-onto/rules/r02.rq` 同为零数字字面量。 |
| X-3 | `docs/04` §4.5 把阈值内联（`ccc:condValue "0.9"`）与引用式（`threshold:...`）两种写法并列。 | **已按载体落地**：`docs/04` §4.5 已改为引用式 —— 条件个体挂 `ccc:condOperand` + `ccc:condTier` + `ccc:condThresholdReference`，并挂 `ccc:condThreshold` 指向档位个体 `ccm:t_cpu_usage_ratio_severe_critical`，零内联数值（载体 `scheme2-onto/ontology/cc-causal.ttl` 的 `ccc:condThreshold` / `ccc:condOperand` / `ccc:condTier` / `ccc:condThresholdReference` 声明处与条件个体 `ccc:Cond-E7`）。 |
| X-4 | `docs/04` §4.5 把 `ccc:CausalLink-E7` 声明为 `owl:Class`（把个体写成类；紧接一行又写 `:E7 a ccc:CausalLink`；全库无 `ccc:CausalLink` 的 `owl:ObjectProperty` 声明）。 | **已按载体落地**：类只有 `ccc:CausalLink`，E7 是它的个体 `ccc:E7`（载体 `scheme2-onto/ontology/cc-causal.ttl` 的 `ccc:CausalLink a owl:Class` 声明与个体 `ccc:E7`）；`docs/04` §4.5 已改为 `ccc:CausalLink a owl:Class` + `:E7 a ccc:CausalLink`。 |
| X-5 | `fault-modes.yaml` 中 3 个模式的 detect 同级并列 `any_of` / `all_of`，组内/组间语义在 v1.0.3 前未定义。 | **已裁定**（`docs/00-总体设计与裁决记录.md` 修订记录 **v2.10** 行「X-5 / X-7 / X-10 契约裁定」）：组内 `all_of` 全真、`any_of` 至少一条真；**组间 AND**；缺省组视为中性成立（禁止组间 OR）。契约依据 = `shared/model/fault-modes.yaml` 的 `meta.detect_semantics`（**v1.0.3 新增**）。实现侧以 `ccf:groupKind`（值 `all_of` / `any_of`，见 `scheme2-onto/ontology/cc-fault.ttl` 的 `ccf:groupKind` 声明）显式标注各组。★ 本行原引的「`docs/03`/`docs/04` 的『任一分支满足即判据成立』表述」经 docs 全库检索**不存在**，属**不实引用**，已删除。 |
| X-6 | `docs/04` §D3 写 `cc:collectionStatus`，而映射表实际给出 `ccm:notCollected` / `ccm:collectedEmpty`。 | **已按载体落地**：`docs/04` §D3 已改写 —— 采集状态是证据个体的 `cc:status`（取值域 `collected` / `notCollected` / `collectedEmpty`，载体 `scheme2-onto/ontology/cc-topology.ttl` 的 `cc:status` 声明）；未采集 / 采到空值按 `shared/mapping/type-map.yaml:98-101` 落成 `ccm:notCollected` / `ccm:collectedEmpty`（只有 `notCollected` 能否定事实）；`cc:collectionStatus` 全库不存在、不建。 |
| X-7 | `predicate:increased\|decreased` 需要基线（prev / delta），而 CCF 观测字段仅有 `id/objectId/metricCode/status/value/unit/rawValue/observedAt/provenance`。 | **已裁定**（`docs/00-总体设计与裁决记录.md` 修订记录 **v2.10** 行「X-5 / X-7 / X-10 契约裁定」）：契约 `shared/model/ccf.schema.json` 的 `$defs.observation` 已新增**可选** `delta`(:433) 与 `deltaWindowS`(:437)（非破坏性）；语义条目在 `shared/model/fault-modes.yaml` 的 `meta.predicate_semantics`（`increased` = delta 为正、`decreased` = delta 为负）；**delta 缺失 ⇒ 该判据 `unjudgeable`**（WARN + 登记 `shared/model/zero-instance-ledger.yaml`），严禁当 `false`。原先「降级为非零累计计数」的实现口径**已废止**。 |
| X-8 | `causal-edges.yaml` 的 `provenance` 只是字面量 `authored`，没有可指向的 IRI。 | **实现侧已物化**：`ccc:ContractProvenance`（`ccc:prov-E1`..`ccc:prov-E18`，见 `scheme2-onto/ontology/cc-causal.ttl` 的 `ccc:ContractProvenance` 个体）携带 `ccc:provenanceToken` / `provenanceDocument` / `provenanceVersion` / `provenanceEdgeId`；T4 断言每条 `ccc:CausalLink` 恰有 1 个 `ccc:provenance` 且 IRI ∈ 契约 18 边物化集合（`scheme2-onto/tests/test_integrity.py` 的 `t4()`）——与载体一致。 |
| X-9 | `docs/04` §8 T6 的字面写法 `ccc:cause{2,4}` 自反匹配，在本体采用的 **n 元物化**（`ccc:CausalLink` 持 cause/effect）下恒为空集 ⇒ 该断言永不触发。 | **实现侧已物化**：T6 改为「经 `ccc:CausalLink` 的**双向链接对**检测」，并**同时**断言字面形式为空集（`scheme2-onto/tests/test_integrity.py` 的 `t6()`；`{2,4}` 限深修饰符在 rdflib 与真机 GraphDB 上实测均为空）——与载体一致。 |
| X-10 | `causal-edges.yaml` 的 discriminator 引用了 `FM-APP-AUTH-INCOMPATIBLE`，该 code 不在 `fault-modes.yaml` 的 14 个 **detect** 模式词表内（它是 `causal-edges.yaml#leaf_root_causes` 的排除法叶子）。 | **已裁定**（`docs/00-总体设计与裁决记录.md` 修订记录 **v2.10** 行「X-5 / X-7 / X-10 契约裁定」）：不是缺陷，但**必须可解析** —— `shared/model/fault-modes.yaml` 已新增 `meta.fault_vocabulary`（`detect_modes: 14` ∪ `exclusion_leaves: [FM-APP-AUTH-INCOMPATIBLE]` = `total_codes: 15`；叶子权威仍在 `causal-edges.yaml#leaf_root_causes[].subtype`），任何跨契约引用必须解析到该词表，由门禁 **G18** 校验（`shared/verify/contract_gates.py:447-665`，含 `G18_MIN_REFS = 10` 反恒真）。本体侧 `ccc:D2` 照契约命名该对 + 指向契约指标的口径**保留**（CQ-D 探针 note 同前）。 |

### 6.2 ABox 投影缺口（CQ 实测 FAIL 的全部原因）

| # | 缺口 | 说明 |
|---|---|---|
| D-1 | `cc:Table` 的 `cc:schemaName` 投影缺口 —— **已闭合**：现役真机夹具上 30/30，CQ-T-13 PASS。 | M2.11 前的 ABox 投影器缺口（旧夹具 `tmp/cc_ops_live2.ttl` 上 0/30，SHACL 在其上点出 30 条违例）；现役夹具 `scheme2-onto/tests/fixtures/abox-live5.ttl`（快照 `ccf-20260928T155516Z-937b79`）上 0 条违例。 |
| D-2 | 会话链投影不完整：`cc:runsQuery` 0/19、`cc:accesses` 0/4、`cc:hasSession` 1/2（CQ-X-11 因此 FAIL）。 | ABox 投影器缺口，非 TBox 缺陷。OWL/RDFS 层对此**静默无错**，只有 SHACL 的 `minCount` 形状能发现 —— 这是 SHACL 兜底的真实价值。 |
| D-3 | `cc:hostsContainer` 0/3 未投影（`Host` 形状的 `sh:or (hosts\|hostsContainer)` 因此 1 条违例）。 | 同上。 |
| D-4 | `ccm:status` 的契约取值域一度与采集面不符：历史契约写小写 `[running, waiting, terminated]`，而真机值与采集面均为首字母大写的 `"Running"`（M2.25 时点 1 条违例）。 | D-4 改判（M2.27）：契约取值域已与采集面一致（4 个真实值 `Running`/`Waiting`/`Terminated`/`Unknown`，结构性封闭），**无需**任何小写化/值变换；历史小写值只出现在旧夹具 `cc_proj_fix.ttl`，正对照夹具已修正为 `"Running"`。 |

### 6.3 契约未给规格、由施工者按契约生成的项

| 项 | 说明 |
|---|---|
| CQ 逐条文本 | `docs/04:74-87` 只规定 6 族的**条数**（14/12/14/10/8/4）与每族一条示例（CQ-F 随故障模式增长，现行实际条数见 §2），未给逐条清单。本 ORSD 的全部 CQ 文本由施工者按契约（类型/关系/指标/故障模式/因果边）生成，**逐条可被 `scheme2-onto/tests/cq_probes.py` 的 SPARQL 探针机械验证**（T5）。 |
| C1–C6 场景归属 | 逐条 CQ 的「对应场景」列由族级映射（见 §2）落到单条；契约只给族级归属（`docs/04:80-85`）。 |
| CQ-D 排序口径 | `causal-edges.yaml` 无「强度」数值字段（只有 `linkKind` 枚举）⇒ CQ-C 的排序口径 = 枚举序（`causes > conditional_causes > relates_to`）+ 跳数。 |
| `cco:` 顶层与 OntoClean 元属性 | `docs/04` §4 D1 只要求「自建顶层 + OntoClean 元属性」，未给具体类名与元属性取值集合 ⇒ `cc-core.ttl` 的 `cco:Entity/Continuant/Occurrent/DependentEntity` 与 `+R/-R,+I/-I,+U/-U,+D/-D` 口径由施工者落地（T12 按其自检）。 |
| 规则文件命名 | `docs/04:372-378` 的规则清单表把文件写作 `r01-app-pool-exhausted.rq` 这类长名；**盘上实际文件名是 `scheme2-onto/rules/r01.rq` … `r07.rq`**（与 `docs/07` M2.7 行一致）。规则个体 `ccp:Rule-rNN` 上的 `ccp:ruleFile` 是权威文件名声明，T10 只比较结论码 `F-00n` 集合、**不比较**规则 id 与文件名 ⇒ 取短名；`docs/04` §6 已于本轮对账为短名（`:326` 标题与 `:372-378` 表第一列，行号不变），本项即为该对账的记录。 |

### 6.4 未纳入的契约项（显式声明，不静默省略）

- `thresholds` 中 `acquire_wait_ms_light` / `load1_light` / `qps_high` / `dead_tuple_ratio_light` 四档的 `warning`/`critical` 在契约里为 `null`（待标定）⇒ 档位个体只有 `ccm:uncalibrated true`、**无** `ccm:thresholdValue`；规则在该档位上不产出结论（不猜值）。
- `F-00x` 结论码与故障链的固定映射来自中立契约 `shared/model/findings.yaml`（v1.0.0）；规则 id 的命名（`r01..r07` 对方案1 的 `R-Fn`）**不比较**（M2.8/T10 只比结论码集合）。

## 7. 计数（自洽性）

- CQ 总数 = **71**（族分布：CQ-T=14、CQ-X=12、CQ-F=23、CQ-C=10、CQ-D=8、CQ-A=4）
- 类型 = 14；指标 = 91；故障模式 = 23；因果边 = 27
- 探针实测 = 70/71 PASS（真机 `scheme2-onto/tests/fixtures/abox-live5.ttl`，快照 `ccf-20260928T155516Z-937b79`）
