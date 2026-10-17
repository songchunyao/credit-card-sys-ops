#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨载体只读护栏一致性门禁（M3.4 越权类 / M3.5 越界类 / 正常态防作弊）。

## 它回答什么问题

方案1（KG，Cypher）与方案2（ONTO，SPARQL）各自实现了一组只读护栏（G1–G10）。
两侧的服务实现文件不同、查询语言不同，因此「护栏是否真的落地」不能靠读代码断言，
必须**用同一份用例清单把两侧都打一遍**，看行为类别是否一致、是否都在期望之内。

## 载体中立怎么做到

同一份 `CASES`，每条声明：`id` / 目标工具 `tool` / 期望 `expect` / 判据 `why`。
- 当入参语言本身不同（Cypher vs SPARQL）时，用例给出两侧等价表达式 `bodies={"kg":…,"onto":…}`；
- **期望类别与判据是载体中立的**（`rejected` / `ok_bounded`）——这就是被比对的东西。
- 极少数只在单侧存在的概念（例如 Cypher 没有命名图子句）标 `carriers=["onto"]`，
  并在汇总里单列 `onto_only` 计数，绝不静默当成两侧都通过。

## 四态而不是二态

    逐条 (载体 × 用例) ∈ {PASS, DIFF, BLOCKED, CARRIER_SPECIFIC}
      PASS             = 观测类别等于期望类别
      DIFF             = 观测类别不等于期望类别（**真不一致**）
      BLOCKED          = 该载体压根连不上，本次**没有观测**（BLOCKED ≠ FAIL）
      CARRIER_SPECIFIC = 该偏离**已被登记为「载体能力边界」**（见 CARRIER_SPECIFIC_CASES）

    退出码：0 = 全部 PASS；1 = 存在 DIFF 或登记表自检失败；2 = 存在 BLOCKED（无法连通）；
            3 = 仅存在已登记的载体特有项（**不是** exit 0）

★ BLOCKED 绝不折算成 FAIL：连不上是「没测」，不是「测出来不对」。
★ CARRIER_SPECIFIC 绝不折算成 PASS：登记是**声明 + 分列**，不是豁免。它单独计数、
  单独打印，既不进 `pass` 计数，也不构成 exit 0 的依据（exit=3）。
  **未登记的偏离一律保持 DIFF**，绝不自动降级。

## 载体能力边界登记表（CARRIER_SPECIFIC_CASES）

用例清单必须**载体中立**，但两侧后端引擎的能力并不总是重合：例如 GraphDB 的 SPARQL
解析器不支持 `{n,m}` 属性路径修饰符，而 Cypher 支持 `[*1..n]`。这种「某侧压根不可达、
既不是对侧缺口、也不是本侧守卫过严」的情形，既不能当 DIFF（会永久污染门禁），
也不能当 PASS（那是绿洗）。正确处理 = **登记 + 分列 + 负控**：

- 登记项必须给出 `case_id` / `carrier` / `reason` / `evidence` / `approved_by`；
  `evidence` 必须含**采集时点**与**引擎原始报文**，否则 ⇒ `REGISTRY_INVALID`。
- 登记的载体与实测偏离的那一侧不符 ⇒ `REGISTRY_INVALID`。
- 登记项在实测中两侧都通过、或两侧都偏离 ⇒ `REGISTRY_STALE`。
- 登记表自检失败 ⇒ 本门禁**不给出 PASS**（退出码 1）。

## 只读纪律

本门禁**只读**：它不写任何服务文件、不改任何契约、不落任何产物（除非显式 `--json-out`）。
它发出的「越权用例」是**会被服务端守卫拒绝的尝试**；服务端不会执行其中的写操作。
它发现的 KG 侧缺口只写进报告，由队长转派（KG 侧由另一位施工者独占）。

## 用法

    python shared/verify/guard_conformance.py                       # 打默认两侧
    python shared/verify/guard_conformance.py --base-onto http://127.0.0.1:8302
    python shared/verify/guard_conformance.py --json
    python shared/verify/guard_conformance.py --selftest            # 离线负控（不联网）
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import urllib.error
import urllib.request

# ★ Windows 控制台默认 GBK：含中文的输出必须先切 UTF-8，否则 print 抛 UnicodeEncodeError。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BASE_KG = "http://127.0.0.1:8301"  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
DEFAULT_BASE_ONTO = "http://127.0.0.1:8302"  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。

CARRIERS = ("kg", "onto")
CARRIER_LABEL = {"kg": "方案1 KG", "onto": "方案2 ONTO"}

# 与两侧服务逐字同名的工具路径（G24 已冻结；本门禁只用它，不读 openapi）
TOOL_PATHS = {
    "cc_guide": "/guide",
    "cc_schema": "/schema",
    "cc_query": "/query",
    "cc_facts": "/facts",
    "cc_trace": "/trace",
    "cc_cause": "/cause",
    "cc_propose": "/propose",
}

# 各工具声明的数组字段（两侧 G24 逐字相同）——用于「行数有界」判据。
LIST_FIELDS = {
    "cc_guide": ("metrics", "objectTypes", "rules"),
    "cc_schema": ("objectTypes", "relationTypes", "attributes"),
    "cc_query": ("rows",),
    "cc_facts": ("objects", "relations", "observations"),
    "cc_trace": ("nodes", "edges"),
    "cc_cause": ("candidates",),
    "cc_propose": ("actions",),
}

BYTE_CAP = 20000
ROW_CAP = 200

REJECTED = "rejected"
OK_BOUNDED = "ok_bounded"
BLOCKED = "blocked"
UNEXPECTED = "unexpected"

# 登记表自检结论码（与逐条判定 PASS/DIFF/BLOCKED/CARRIER_SPECIFIC 并行的独立通道）
REGISTRY_INVALID = "REGISTRY_INVALID"
REGISTRY_STALE = "REGISTRY_STALE"

EXIT_PASS = 0
EXIT_DIFF = 1
EXIT_BLOCKED = 2
EXIT_CARRIER_SPECIFIC = 3

# ---------------------------------------------------------------------------
# 用例清单（载体中立）
# ---------------------------------------------------------------------------
CASES = [
    # ---- A. M3.4 越权类：期望一律 rejected -------------------------------
    {
        "id": "A01-write-insert",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "CREATE (n:Probe {id: 'x'}) RETURN n"},
            "onto": {"query": "INSERT DATA { <urn:a> <urn:b> <urn:c> }"},
        },
        "expect": REJECTED,
        "why": "写操作（新建/插入）必须被只读守卫拒绝：KG 走 FORBIDDEN_TOKEN，"
               "ONTO 走 FORBIDDEN_TOKEN（G2）。",
    },
    {
        "id": "A02-write-delete",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) DELETE n"},
            "onto": {"query": "DELETE WHERE { ?s ?p ?o }"},
        },
        "expect": REJECTED,
        "why": "删除类写操作必须被拒（G2）。",
    },
    {
        "id": "A03-write-drop",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) DETACH DELETE n"},
            "onto": {"query": "DROP GRAPH <urn:g>"},
        },
        "expect": REJECTED,
        "why": "破坏性结构操作（DETACH DELETE / DROP）必须被拒（G2）。",
    },
    {
        "id": "A04-write-upsert",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MERGE (n:Probe {id: 'x'}) RETURN n"},
            "onto": {"query": "CLEAR GRAPH <urn:g>"},
        },
        "expect": REJECTED,
        "why": "幂等写（MERGE）与清空（CLEAR）必须被拒（G2）。",
    },
    {
        "id": "A05-write-update",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) SET n.probe = 1 RETURN n"},
            "onto": {"query": "INSERT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
        },
        "expect": REJECTED,
        "why": "属性改写（SET / INSERT…WHERE）必须被拒（G2）。",
    },
    {
        "id": "A06-external-io",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "CALL apoc.load.json('http://evil.example/x') YIELD value "
                             "RETURN value"},
            "onto": {"query": "SELECT * WHERE { SERVICE <http://evil.example/sparql> "
                              "{ ?s ?p ?o } }"},
        },
        "expect": REJECTED,
        "why": "外部 IO / 联邦查询（apoc.load.* / SERVICE）必须被拒，防 SSRF（G3）。",
    },
    {
        "id": "A07-load-remote-file",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "LOAD CSV FROM 'http://evil.example/x.csv' AS row RETURN row"},
            "onto": {"query": "LOAD <http://evil.example/x.ttl> INTO GRAPH <urn:g>"},
        },
        "expect": REJECTED,
        "why": "远程文件装载必须被拒（G2/G3）。",
    },
    {
        "id": "A08-admin-probe",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "CALL dbms.security.listUsers() YIELD * RETURN *"},
            "onto": {"query": "SELECT * WHERE { GRAPH "
                              "<http://www.openrdf.org/config/repository#Repository> "
                              "{ ?s ?p ?o } }"},
        },
        "expect": REJECTED,
        "why": "非只读管理面/仓库配置探测必须被拒：KG 用过程白名单拒绝非只读过程，"
               "ONTO 用命名图禁令拒绝仓库配置图（G9）。",
    },
    {
        "id": "A09-multi-statement",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) RETURN n; MATCH (m) RETURN m"},
            "onto": {"query": "SELECT * WHERE { ?s ?p ?o } ; SELECT * WHERE { ?a ?b ?c }"},
        },
        "expect": REJECTED,
        "why": "多语句请求必须被拒（分号夹在语句中部 ⇒ MULTI_STATEMENT_REJECTED）。",
    },
    {
        "id": "A10-non-readonly-form",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "FOREACH (x IN [1] | SET x.probe = 1)"},
            "onto": {"query": "MODIFY <urn:g> { ?s ?p ?o }"},
        },
        "expect": REJECTED,
        "why": "查询形式不在只读白名单内必须被拒（QUERY_FORM_REJECTED，G1）。",
    },
    {
        "id": "A11-path-unbounded",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH p = (a)-[*]-(b) RETURN p"},
            "onto": {"query": "SELECT * WHERE { ?a <urn:p>* ?b }"},
        },
        "expect": REJECTED,
        "why": "无上界属性路径必须被拒（成环图会爆炸；G4）。",
    },
    {
        "id": "A12-path-plus",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH p = (a)-[*1..]-(b) RETURN p"},
            "onto": {"query": "SELECT * WHERE { ?a <urn:p>+ ?b }"},
        },
        "expect": REJECTED,
        "why": "一无上界重复（[*1..] / `+`）必须被拒（G4）。",
    },
    {
        "id": "A13-path-over-depth",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH p = (a)-[*1..9]-(b) RETURN p"},
            "onto": {"query": "SELECT * WHERE { ?a <urn:p>{1,9} ?b }"},
        },
        "expect": REJECTED,
        "why": "路径上界 > 硬上限 4 必须被拒（G8）。",
    },
    {
        "id": "A14-path-open-low-bound",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH p = (a)-[*..5]-(b) RETURN p"},
            "onto": {"query": "SELECT * WHERE { ?a <urn:p>{,5} ?b }"},
        },
        "expect": REJECTED,
        "why": "省略下界的区间（`{,n}` 等价 `{0,n}`）同样受上界约束，n>4 必须被拒（G4/G8）；"
               "这一条是 M3.4 本轮补上的真实缺口。",
    },
    {
        "id": "A15-cross-graph",
        "tool": "cc_query",
        "bodies": {
            "onto": {"query": "SELECT * WHERE { GRAPH <urn:g> { ?s ?p ?o } }"},
        },
        "carriers": ["onto"],
        "expect": REJECTED,
        "why": "跨命名图访问必须被拒（G9）。★ Cypher 无命名图子句 ⇒ 本用例只在 ONTO 侧可测，"
               "单列 onto_only 计数，不冒充两侧一致。",
    },
    # ---- B. M3.5 越界类：期望 ok_bounded（有界返回，不拒合法查询） ---------
    {
        "id": "B01-limit-large",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) RETURN n LIMIT 100000"},
            "onto": {"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 100000"},
        },
        "expect": OK_BOUNDED,
        "why": "调用方声明的超大 LIMIT 不得穿透服务端载荷上限：返回必须 ≤ 20000 B，"
               "若截断则必须回传 truncated/droppedRows/droppedBytes（M3.5，契约 C6）。",
    },
    {
        "id": "B02-limit-at-row-cap",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) RETURN n LIMIT 200"},
            "onto": {"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 200"},
        },
        "expect": OK_BOUNDED,
        "why": "恰好在行上限上的合法查询必须被接受且返回有界（防「一律拒」的作弊解）。",
    },
    {
        "id": "B03-no-limit-injected",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) RETURN n"},
            "onto": {"query": "SELECT * WHERE { ?s ?p ?o }"},
        },
        "expect": OK_BOUNDED,
        "why": "未声明 LIMIT 的查询由服务端注入行上限（G7）后仍必须返回有界载荷。",
    },
    {
        "id": "B04-wide-payload",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 200"},
            "onto": {"query": "SELECT * WHERE { ?a ?r ?b } LIMIT 200"},
        },
        "expect": OK_BOUNDED,
        "why": "宽三元组/多列返回同样不得突破 20000 B 字节上限（M3.5）。",
    },
    # ---- C. 正常态（防「什么都拒」的作弊解）-------------------------------
    {
        "id": "C01-normal-guide",
        "tool": "cc_guide",
        "body": {},
        "expect": OK_BOUNDED,
        "why": "正常态：无参工具必须可用（若被拒，说明护栏被写成了「一律拒绝」）。",
    },
    {
        "id": "C02-normal-schema",
        "tool": "cc_schema",
        "body": {},
        "expect": OK_BOUNDED,
        "why": "正常态：结构清单必须可用，且返回有界。",
    },
    {
        "id": "C03-normal-select",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH (n) RETURN n LIMIT 5"},
            "onto": {"query": "SELECT ?s WHERE { ?s ?p ?o } LIMIT 5"},
        },
        "expect": OK_BOUNDED,
        "why": "正常态：最小的合法只读查询必须被接受。",
    },
    {
        "id": "C04-normal-bounded-path",
        "tool": "cc_query",
        "bodies": {
            "kg": {"query": "MATCH p = (a)-[*1..3]-(b) RETURN p LIMIT 5"},
            "onto": {"query": "SELECT * WHERE { ?a <urn:p>{1,3} ?b } LIMIT 5"},
        },
        "expect": OK_BOUNDED,
        "why": "正常态：**有界**属性路径（上界 ≤ 4）必须被接受——这是与 A11–A14 的对照，"
               "防止把「拒一切路径」当成护栏达标。",
    },
]


# ---------------------------------------------------------------------------
# 载体能力边界登记表（CARRIER_SPECIFIC 的唯一来源；数据常量，便于 grep 审计）
# ---------------------------------------------------------------------------
_EVIDENCE_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

REGISTRY_FIELDS = ("case_id", "carrier", "reason", "evidence", "approved_by")

CARRIER_SPECIFIC_CASES = [
    {
        "case_id": "C04-normal-bounded-path",
        "carrier": "onto",
        "reason": (
            "用例期望「**有界**属性路径（上界 ≤ 4）必须被接受」，但 GraphDB 的 SPARQL 解析器"
            "**不支持 `{n,m}` 属性路径修饰符**（SPARQL 1.1 路径的区间形式），"
            "⇒ 该用例在 ONTO 侧**不可达**：本侧守卫 guard_query() 已放行，后端在解析期报错、"
            "由服务包装成 QUERY_FORM_REJECTED 返回。"
            "这既不是 KG 侧的缺口，也不是 ONTO 侧守卫过严，而是 ONTO 后端引擎的能力边界；"
            "Cypher 侧 `[*1..3]` 受支持 ⇒ 该边界只出现在 ONTO 一侧。"
            "★ 本登记不改变该用例的期望类别，也不把 ONTO 侧结果算作合格："
            "ONTO 侧仍然是「没有观测到期望行为」，只是它已被声明。"
        ),
        "evidence": (
            "采集时点 2026-09-29T03:40:59+08:00（生产机 192.0.2.10:8302；服务 PID 1583273；"
            "部署文件 sha256=1cb0ef3ee3363a2f7914c08e37d0770adb43caad0c4029db1297df0ca613fb05）。"
            "可复现命令：POST http://192.0.2.10:8302/query "
            "{\"query\": \"SELECT * WHERE { ?a <urn:p>{1,3} ?b } LIMIT 5\"} ⇒ HTTP 400，"
            "error.code=QUERY_FORM_REJECTED，"
            "error.detail.engineMessage 首行逐字为 "
            "MALFORMED QUERY: Encountered \" \"{\" \"{ \"\" at line 15, column 28."
            "（同一响应里 error.message 是服务的中文包装文案，engineMessage 是 GraphDB 原始报错；"
            "报错位置落在服务包裹后的用户查询区内）。"
            "对照 1：本侧 guard_query() 对 {1,3}/{1,4}/{,4} 放行（离线单测 "
            "test_m34_open_low_range_bound 覆盖该边界）。"
            "对照 2：门禁真机复跑 python shared/verify/guard_conformance.py ⇒ "
            "KG=ok_bounded(2415 B) / ONTO=rejected(QUERY_FORM_REJECTED)。"
            "对照 3：Cypher 侧 MATCH p=(a)-[*1..3]-(b) RETURN p LIMIT 5 正常返回 ⇒ "
            "该边界只出现在 ONTO 一侧。"
        ),
        "approved_by": "captain:R-21",
    },
]


def entry_for(case_id, registry=None):
    """按 case_id 取登记项（未登记返回 None）。"""
    for entry in (CARRIER_SPECIFIC_CASES if registry is None else registry):
        if isinstance(entry, dict) and entry.get("case_id") == case_id:
            return entry
    return None


def validate_registry(registry=None, cases=None):
    """登记表**结构**自检（不看实测）：返回问题清单，空 = 干净。

    问题码 ∈ {REGISTRY_INVALID, REGISTRY_STALE}。
    """
    registry = CARRIER_SPECIFIC_CASES if registry is None else registry
    cases = CASES if cases is None else cases
    known = {c["id"] for c in cases}
    issues = []
    for index, entry in enumerate(registry):
        where = "CARRIER_SPECIFIC_CASES[%d]" % index
        if not isinstance(entry, dict):
            issues.append({"code": REGISTRY_INVALID, "entry": where, "field": "*",
                           "detail": "登记项不是对象"})
            continue
        case_id = entry.get("case_id")
        tag = "%s(%s)" % (where, case_id or "?")
        for field in REGISTRY_FIELDS:
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append({"code": REGISTRY_INVALID, "entry": tag, "field": field,
                               "detail": "登记项字段 %s 缺失或为空" % field})
        carrier = entry.get("carrier")
        if isinstance(carrier, str) and carrier.strip() and carrier not in CARRIERS:
            issues.append({"code": REGISTRY_INVALID, "entry": tag, "field": "carrier",
                           "detail": "carrier=%r 不在 %s 之内" % (carrier, list(CARRIERS))})
        evidence = entry.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            if not _EVIDENCE_TS_RE.search(evidence):
                issues.append({"code": REGISTRY_INVALID, "entry": tag, "field": "evidence",
                               "detail": "evidence 缺少可核对的采集时点"
                                         "（须形如 YYYY-MM-DDTHH:MM）"})
        if isinstance(case_id, str) and case_id.strip() and case_id not in known:
            issues.append({"code": REGISTRY_STALE, "entry": tag, "field": "case_id",
                           "detail": "登记项 %r 不在本次用例清单内（登记失效或已改名）"
                                     % case_id})
    return issues


def registry_issues_for(results, registry=None, cases=None):
    """登记表 × 实测对照。

    - 登记项实测两侧均等于期望类别 ⇒ REGISTRY_STALE（载体边界已消失，登记失效）
    - 登记 carrier 与实测偏离的那一侧不符 ⇒ REGISTRY_INVALID
    - 实测两侧都偏离 ⇒ REGISTRY_INVALID（两侧分歧不得登记为单侧载体特有）
    """
    registry = CARRIER_SPECIFIC_CASES if registry is None else registry
    issues = list(validate_registry(registry, cases))
    by_id = {row["id"]: row for row in results}
    for index, entry in enumerate(registry):
        if not isinstance(entry, dict):
            continue
        case_id = entry.get("case_id")
        declared = entry.get("carrier")
        row = by_id.get(case_id)
        if row is None or row["verdict"] == "BLOCKED":
            continue
        tag = "CARRIER_SPECIFIC_CASES[%d](%s)" % (index, case_id)
        if declared not in row["observed"]:
            # 本次运行（例如 --only）没有测到登记的那一侧 ⇒ 无法判定登记是否仍成立，
            # 跳过实测对照；结构自检已在 validate_registry 里照常执行。
            continue
        deviating = sorted(c for c, item in row["observed"].items()
                           if item["kind"] != row["expect"])
        if not deviating:
            issues.append({"code": REGISTRY_STALE, "entry": tag, "field": "case_id",
                           "detail": "登记项 %s 实测两侧均等于期望类别 ⇒ "
                                     "载体能力边界已消失，登记失效" % case_id})
            continue
        if declared not in deviating:
            issues.append({"code": REGISTRY_INVALID, "entry": tag, "field": "carrier",
                           "detail": "登记 carrier=%s 与实测偏离的一侧 %s 不符"
                                     % (declared, deviating)})
        elif len(deviating) > 1:
            issues.append({"code": REGISTRY_INVALID, "entry": tag, "field": "carrier",
                           "detail": "实测两侧均偏离（%s）⇒ "
                                     "两侧分歧不得登记为单侧载体特有" % deviating})
    return issues


def cases_for(carrier):
    """该载体适用的用例（默认全部；显式 carriers 的按其声明）。"""
    out = []
    for case in CASES:
        allowed = case.get("carriers") or list(CARRIERS)
        if carrier in allowed:
            out.append(case)
    return out


def body_for(case, carrier):
    """取该载体上的请求体（语言无关的 body 优先，其次 bodies[carrier]）。"""
    if case.get("bodies"):
        return case["bodies"].get(carrier)
    return case.get("body", {})


# ---------------------------------------------------------------------------
# 观测：唯一做 IO 的地方
# ---------------------------------------------------------------------------
class Transport:
    """HTTP 探针（只发只读工具调用；不做任何文件写入）。"""

    def __init__(self, bases, timeout=10.0):
        self.bases = dict(bases)
        self.timeout = float(timeout)

    def __call__(self, carrier, tool, payload):
        base = (self.bases.get(carrier) or "").rstrip("/")
        if not base:
            return {"kind": BLOCKED, "detail": "未提供 %s 基址" % carrier}
        url = base + TOOL_PATHS[tool]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:  # 4xx/5xx：读回响应体（守卫拒绝走 400）
            try:
                raw = exc.read()
            except Exception:
                raw = b""
            status = exc.code
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            return {"kind": BLOCKED, "detail": "无法连通 %s：%s" % (url, exc)}
        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
        return {"kind": "response", "url": url, "status": status,
                "bytes": len(raw), "json": parsed}


# ---------------------------------------------------------------------------
# 判据：观测 → 类别
# ---------------------------------------------------------------------------
def classify(obs, case):
    """把一次观测归到 carrier-neutral 的类别。

    返回 (kind, reason)；kind ∈ {REJECTED, OK_BOUNDED, BLOCKED, UNEXPECTED}。
    """
    if not isinstance(obs, dict):
        return BLOCKED, "探针未返回观测（视为不可达，BLOCKED ≠ FAIL）"
    if obs.get("kind") == BLOCKED:
        return BLOCKED, obs.get("detail") or "不可达"
    payload = obs.get("json")
    if not isinstance(payload, dict):
        return UNEXPECTED, "响应不是 JSON 对象（status=%s）" % obs.get("status")
    error = payload.get("error")
    if obs.get("status") != 200 or isinstance(error, dict):
        if not isinstance(error, dict):
            return UNEXPECTED, "非 200 但没有 error 对象（status=%s）" % obs.get("status")
        code = error.get("code")
        if not isinstance(code, str) or not code:
            return UNEXPECTED, "error 缺 code"
        return REJECTED, "error.code=%s" % code

    # 200 ⇒ 必须是有界载荷
    size = obs.get("bytes")
    if size is None or size > BYTE_CAP:
        return UNEXPECTED, "返回 %s B > 上限 %d B" % (size, BYTE_CAP)
    truncated = payload.get("truncated")
    dropped_rows = payload.get("droppedRows")
    dropped_bytes = payload.get("droppedBytes")
    if truncated is True and not (
            (isinstance(dropped_rows, int) and dropped_rows > 0)
            or (isinstance(dropped_bytes, int) and dropped_bytes > 0)):
        return UNEXPECTED, "truncated=true 但 droppedRows/droppedBytes 均为 0（截断未记账）"
    for field in LIST_FIELDS.get(case["tool"], ()):
        value = payload.get(field)
        if isinstance(value, list) and len(value) > ROW_CAP:
            return UNEXPECTED, "%s 返回 %d 行 > 行上限 %d" % (field, len(value), ROW_CAP)
    return OK_BOUNDED, "%d B，truncated=%s" % (size, truncated)


# ---------------------------------------------------------------------------
# 门禁主体
# ---------------------------------------------------------------------------
def run_gate(transport, carriers=CARRIERS, cases=None, registry=None):
    """跑完所有 (载体 × 用例)，返回结果字典（不打印、不写文件）。"""
    cases = CASES if cases is None else cases
    registry = CARRIER_SPECIFIC_CASES if registry is None else registry
    results = []
    per_carrier_blocked = {carrier: False for carrier in carriers}
    for case in cases:
        per_case = {}
        for carrier in carriers:
            allowed = case.get("carriers") or list(CARRIERS)
            if carrier not in allowed:
                continue
            payload = body_for(case, carrier)
            if payload is None:
                per_case[carrier] = {"kind": UNEXPECTED, "reason": "用例缺该载体的入参"}
                continue
            obs = transport(carrier, case["tool"], payload)
            kind, reason = classify(obs, case)
            if kind == BLOCKED:
                per_carrier_blocked[carrier] = True
            per_case[carrier] = {
                "kind": kind,
                "reason": reason,
                "status": obs.get("status"),
                "bytes": obs.get("bytes"),
            }
        verdict = verdict_for(case, per_case, registry)
        row = {"id": case["id"], "tool": case["tool"], "expect": case["expect"],
               "why": case["why"], "observed": per_case, "verdict": verdict}
        if verdict == "CARRIER_SPECIFIC":
            entry = entry_for(case["id"], registry) or {}
            row["registry"] = {"case_id": case["id"],
                               "carrier": entry.get("carrier"),
                               "reason": entry.get("reason"),
                               "approved_by": entry.get("approved_by")}
        results.append(row)

    carriers_used = [c for c in carriers if any(c in r["observed"] for r in results)]
    unreachable = sorted({c for c in carriers_used if per_carrier_blocked[c]})
    cs_rows = [r for r in results if r["verdict"] == "CARRIER_SPECIFIC"]
    registry_issues = registry_issues_for(results, registry, cases)
    issue_codes = {issue["code"] for issue in registry_issues}
    summary = {
        "cases_total": len(results),
        "pairs_total": sum(len(r["observed"]) for r in results),
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "diff": sum(1 for r in results if r["verdict"] == "DIFF"),
        "blocked": sum(1 for r in results if r["verdict"] == "BLOCKED"),
        "carrier_specific": len(cs_rows),
        "carrier_specific_items": [
            {"case_id": r["id"],
             "carrier": (r.get("registry") or {}).get("carrier"),
             "approved_by": (r.get("registry") or {}).get("approved_by")}
            for r in cs_rows],
        "onto_only": sum(1 for r in results
                         if set(r["observed"]) == {"onto"}),
        "kg_only": sum(1 for r in results if set(r["observed"]) == {"kg"}),
        "carriers": carriers_used,
        "unreachable_carriers": unreachable,
        "normal_cases": sum(1 for r in results if r["id"].startswith("C")),
        "registry_issues": registry_issues,
        "registry_status": ("invalid" if REGISTRY_INVALID in issue_codes
                            else "stale" if REGISTRY_STALE in issue_codes else "ok"),
    }
    # 判定优先级：没测到（BLOCKED）> 登记表自检失败 > 真不一致 > 已声明载体边界 > 全绿
    if unreachable:
        summary["verdict"], summary["exit_code"] = "BLOCKED", EXIT_BLOCKED
    elif REGISTRY_INVALID in issue_codes:
        summary["verdict"], summary["exit_code"] = REGISTRY_INVALID, EXIT_DIFF
    elif REGISTRY_STALE in issue_codes:
        summary["verdict"], summary["exit_code"] = REGISTRY_STALE, EXIT_DIFF
    elif summary["diff"]:
        summary["verdict"], summary["exit_code"] = "DIFF", EXIT_DIFF
    elif summary["carrier_specific"]:
        summary["verdict"], summary["exit_code"] = ("CARRIER_SPECIFIC",
                                                    EXIT_CARRIER_SPECIFIC)
    else:
        summary["verdict"], summary["exit_code"] = "PASS", EXIT_PASS
    return {"summary": summary, "results": results}


def verdict_for(case, per_case, registry=None):
    """逐条判定；非 PASS 时先看是否落在**已登记且与实测一致**的载体特有项上。

    CARRIER_SPECIFIC 只在「恰好一侧偏离、且就是登记的那一侧」时成立；
    其余任何偏离（未登记、登记侧不符、两侧都偏离）一律保持 DIFF。
    """
    registry = CARRIER_SPECIFIC_CASES if registry is None else registry
    observed = list(per_case.values())
    if not observed:
        return "BLOCKED"
    if any(item["kind"] == BLOCKED for item in observed):
        return "BLOCKED"
    if all(item["kind"] == case["expect"] for item in observed):
        return "PASS"
    entry = entry_for(case["id"], registry)
    if entry is not None:
        declared = entry.get("carrier")
        deviating = sorted(c for c, item in per_case.items()
                           if item["kind"] != case["expect"])
        if deviating == [declared]:
            return "CARRIER_SPECIFIC"
    return "DIFF"


def print_report(out, carriers, bases, stream=None):
    stream = stream or sys.stdout
    summary = out["summary"]
    stream.write("跨载体只读护栏一致性门禁（guard_conformance）\n")
    for carrier in carriers:
        if carrier in bases:
            stream.write("  %-10s %s\n" % (CARRIER_LABEL.get(carrier, carrier),
                                           bases.get(carrier)))
    stream.write("  用例 %d 条 / 比对 %d 对；正常态用例 %d 条\n"
                 % (summary["cases_total"], summary["pairs_total"], summary["normal_cases"]))
    stream.write("\n%-30s %-12s %-10s %-10s %s\n"
                 % ("用例", "期望", "KG", "ONTO", "判定"))
    for row in out["results"]:
        kg = row["observed"].get("kg", {}).get("kind", "-")
        onto = row["observed"].get("onto", {}).get("kind", "-")
        stream.write("%-30s %-12s %-10s %-10s %s\n"
                     % (row["id"], row["expect"], kg, onto, row["verdict"]))
        if row["verdict"] == "CARRIER_SPECIFIC":
            reg = row.get("registry") or {}
            stream.write("      · 已登记载体特有项：carrier=%s approved_by=%s\n"
                         % (reg.get("carrier"), reg.get("approved_by")))
            stream.write("        理由：%s\n" % reg.get("reason"))
        if row["verdict"] != "PASS":
            for carrier, item in row["observed"].items():
                stream.write("      · %s: %s（%s）\n" % (carrier, item["kind"], item["reason"]))
    stream.write("\nPASS %d / DIFF %d / BLOCKED %d / CARRIER_SPECIFIC %d\n"
                 % (summary["pass"], summary["diff"], summary["blocked"],
                    summary["carrier_specific"]))
    stream.write("★ CARRIER_SPECIFIC 单独计数：**不并入 PASS**，也**不构成 exit 0 的依据**"
                 "（它既不是绿也不是红，是已声明的载体能力边界）。\n")
    if summary["carrier_specific_items"]:
        stream.write("已登记的载体特有项（单列）：\n")
        for item in summary["carrier_specific_items"]:
            stream.write("  · %s @ %s（approved_by=%s）\n"
                         % (item["case_id"], item["carrier"], item["approved_by"]))
    if summary["onto_only"] or summary["kg_only"]:
        stream.write("单侧用例：onto_only=%d kg_only=%d（已在汇总单列，未冒充两侧一致）\n"
                     % (summary["onto_only"], summary["kg_only"]))
    for issue in summary["registry_issues"]:
        stream.write("登记表自检 %s：%s [%s] %s\n"
                     % (issue["code"], issue["entry"], issue["field"], issue["detail"]))
    if summary["registry_issues"]:
        stream.write("★ 登记表自检失败 ⇒ 本门禁不给出 PASS（exit=1）："
                     "载体特有项必须可核对、且与实测一致。\n")
    verdict = summary["verdict"]
    if verdict == "BLOCKED":
        stream.write("RESULT: BLOCKED（载体不可达：%s ⇒ 本次没有观测；BLOCKED ≠ FAIL，exit=2）\n"
                     % "、".join(summary["unreachable_carriers"]))
    elif verdict in (REGISTRY_INVALID, REGISTRY_STALE):
        stream.write("RESULT: %s（登记表自检失败 ⇒ 不得判 PASS，exit=1）\n" % verdict)
    elif verdict == "DIFF":
        stream.write("RESULT: DIFF（存在与期望类别不符的观测 ⇒ 真不一致，exit=1）\n")
    elif verdict == "CARRIER_SPECIFIC":
        stream.write("RESULT: CARRIER_SPECIFIC（非 PASS 项全部是**已登记**的载体能力边界；"
                     "既未并入 PASS，也不构成 exit 0 的依据 ⇒ exit=3）\n")
    else:
        stream.write("RESULT: PASS（全部用例的观测类别等于期望类别；"
                     "★ 仅证本次用例集上的行为类别一致，不证语义等价）\n")


# ---------------------------------------------------------------------------
# 自检（离线；含 4 条负控）
# ---------------------------------------------------------------------------
def _stub_transport(plan):
    """plan[carrier][case_id] = 观测；缺省视为不可达（返回 BLOCKED 观测）。"""
    def transport(carrier, tool, payload):
        item = (plan.get(carrier) or {}).get(payload.get("__case__"))
        if item is None:
            return {"kind": BLOCKED, "detail": "桩：%s 未规划该用例（视为不可达）" % carrier}
        return item
    return transport


def _mini_cases():
    return [
        {"id": "N1-reject", "tool": "cc_query",
         "bodies": {"kg": {"__case__": "N1-reject"}, "onto": {"__case__": "N1-reject"}},
         "expect": REJECTED, "why": "两侧都拒 ⇒ PASS"},
        {"id": "N2-normal", "tool": "cc_query",
         "bodies": {"kg": {"__case__": "N2-normal"}, "onto": {"__case__": "N2-normal"}},
         "expect": OK_BOUNDED, "why": "正常态两侧都接受 ⇒ PASS"},
    ]


def _registry_cases():
    """登记表正/负控专用桩：一条**已登记**用例 + 一条**未登记**用例。"""
    return [
        {"id": "C04-normal-bounded-path", "tool": "cc_query",
         "bodies": {"kg": {"__case__": "C04"}, "onto": {"__case__": "C04"}},
         "expect": OK_BOUNDED, "why": "桩：已登记用例（正控）"},
        {"id": "N9-unregistered", "tool": "cc_query",
         "bodies": {"kg": {"__case__": "N9"}, "onto": {"__case__": "N9"}},
         "expect": OK_BOUNDED, "why": "桩：未登记用例（不得自动降级）"},
    ]


def _resp(status, payload=None, size=None):
    raw = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return {"kind": "response", "status": status, "json": payload,
            "bytes": len(raw) if raw is not None else (size or 0)}


def _rejected(code="FORBIDDEN_TOKEN"):
    return _resp(400, {"error": {"code": code, "message": "m", "tool": "cc_query"},
                       "hint": "h", "truncated": False, "droppedRows": 0, "droppedBytes": 0})


def _accepted(rows=1, pad=10):
    body = {"rows": [{"x": "y" * pad} for _ in range(rows)], "rowCount": rows,
            "hint": "", "truncated": False, "droppedRows": 0, "droppedBytes": 0}
    return _resp(200, body)


def selftest(stream=None) -> int:
    stream = stream or sys.stdout
    checks = []
    controls = []

    def check(name, condition, detail=""):
        checks.append((name, bool(condition), detail))

    def control(name, condition, expected, actual):
        """负控/正控：**逐条打印期望 vs 实际**（与 check 分开计数，避免重复打印）。"""
        controls.append((name, bool(condition), expected, actual))

    # ①–⑥ 只测基础判定（三态 + 有界性判据），刻意传**空登记表**：否则
    # 「出厂登记项 C04 不在本桩用例清单内」会触发无关的 REGISTRY_STALE，污染这些控制。
    cases = _mini_cases()

    # ① 两侧都拒 ⇒ PASS
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": _accepted()},
            "onto": {"N1-reject": _rejected(), "N2-normal": _accepted()}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("①两侧都拒 ⇒ PASS（不是 DIFF、不是 BLOCKED）",
          out["summary"]["verdict"] == "PASS" and out["summary"]["pass"] == 2,
          json.dumps(out["summary"], ensure_ascii=False))

    # ② 一侧拒 / 一侧放行 ⇒ DIFF（真不一致）
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": _accepted()},
            "onto": {"N1-reject": _accepted(), "N2-normal": _accepted()}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("②一侧拒一侧放行 ⇒ DIFF（真不一致，exit=1）",
          out["summary"]["verdict"] == "DIFF" and out["summary"]["diff"] == 1,
          json.dumps(out["summary"], ensure_ascii=False))

    # ③ 一方不可达 ⇒ BLOCKED 而非 FAIL
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": _accepted()},
            "onto": {}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("③一方不可达 ⇒ BLOCKED（而不是 DIFF/FAIL）",
          out["summary"]["verdict"] == "BLOCKED"
          and out["summary"]["unreachable_carriers"] == ["onto"]
          and out["summary"]["diff"] == 0,
          json.dumps(out["summary"], ensure_ascii=False))

    # ④ 正常态用例被误拒 ⇒ DIFF（防「什么都拒」的作弊解）
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": _rejected()},
            "onto": {"N1-reject": _rejected(), "N2-normal": _rejected()}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("④正常态用例被误拒 ⇒ DIFF（两侧都拒也算 DIFF）",
          out["summary"]["verdict"] == "DIFF" and out["summary"]["diff"] == 1,
          json.dumps(out["summary"], ensure_ascii=False))

    # ⑤ 超字节上限的 200 响应 ⇒ 不算 ok_bounded
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": _accepted(rows=1, pad=BYTE_CAP)},
            "onto": {"N1-reject": _rejected(), "N2-normal": _accepted(rows=1, pad=BYTE_CAP)}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("⑤200 响应超过 20000 B ⇒ DIFF（有界性判据真的在起作用）",
          out["summary"]["verdict"] == "DIFF" and out["summary"]["diff"] == 1,
          json.dumps(out["summary"], ensure_ascii=False))

    # ⑥ truncate=true 但记账为 0 ⇒ 不算有界（截断未记账）
    silent = _resp(200, {"rows": [{"x": "y"}], "rowCount": 1, "hint": "",
                         "truncated": True, "droppedRows": 0, "droppedBytes": 0})
    plan = {"kg": {"N1-reject": _rejected(), "N2-normal": silent},
            "onto": {"N1-reject": _rejected(), "N2-normal": silent}}
    out = run_gate(_stub_transport(plan), cases=cases, registry=[])
    check("⑥truncated=true 但 dropped* 全 0 ⇒ DIFF（静默截断不被接受）",
          out["summary"]["verdict"] == "DIFF" and out["summary"]["diff"] == 1,
          json.dumps(out["summary"], ensure_ascii=False))

    # ⑦ 用例清单形状：越权/越界/正常态三类齐备，且正常态 ≥ 4 条
    ids = [c["id"] for c in CASES]
    check("⑦用例 id 唯一", len(ids) == len(set(ids)), str(len(ids) - len(set(ids))))
    check("⑦三类用例齐备（A 越权 / B 越界 / C 正常态）",
          bool([i for i in ids if i.startswith("A")])
          and bool([i for i in ids if i.startswith("B")])
          and bool([i for i in ids if i.startswith("C")]), "ok")
    check("⑦正常态用例 ≥ 4 条", len([i for i in ids if i.startswith("C")]) >= 4,
          str(len([i for i in ids if i.startswith("C")])))
    check("⑦每个用例都有载体中立期望与判据",
          all(c.get("expect") in (REJECTED, OK_BOUNDED) and (c.get("why") or "").strip()
              for c in CASES), "ok")
    check("⑦两侧都适用的用例占多数（单侧用例不得超过 1/4）",
          sum(1 for c in CASES if not c.get("carriers")) >= len(CASES) * 3 // 4,
          str(sum(1 for c in CASES if not c.get("carriers"))))
    check("⑦只用已声明的工具路径",
          all(c["tool"] in TOOL_PATHS for c in CASES), "ok")
    check("⑦每个双侧用例都给了两侧入参或语言无关 body",
          all((c.get("bodies") and set(c["bodies"]) >= set(c.get("carriers") or CARRIERS))
              or (not c.get("bodies") and c.get("body") is not None)
              for c in CASES), "ok")

    # ---- ⑧–⑪ 载体能力边界登记表的正/负控（逐条打印期望 vs 实际）----------
    reg_cases = _registry_cases()
    c04, n9 = "C04-normal-bounded-path", "N9-unregistered"

    # ⑧ 同一次运行里，同一侧同时偏离「已登记」与「未登记」两条：
    #    未登记的必须仍是 DIFF（登记表绝不自动降级），且真实 DIFF 不被 CARRIER_SPECIFIC 掩盖。
    plan = {"kg": {"C04": _accepted(), "N9": _accepted()},
            "onto": {"C04": _rejected("QUERY_FORM_REJECTED"), "N9": _rejected()}}
    out = run_gate(_stub_transport(plan), cases=reg_cases)
    s = out["summary"]
    per = {row["id"]: row["verdict"] for row in out["results"]}
    control("⑧未登记用例在一侧偏离 ⇒ 仍是 DIFF（无自动降级；同run已登记项才是 CARRIER_SPECIFIC）",
            per.get(n9) == "DIFF" and per.get(c04) == "CARRIER_SPECIFIC"
            and s["carrier_specific"] == 1 and s["diff"] == 1
            and s["exit_code"] == EXIT_DIFF,
            "%s=DIFF / %s=CARRIER_SPECIFIC / carrier_specific=1 / diff=1 / exit=1"
            % (n9, c04),
            "%s=%s / %s=%s / carrier_specific=%d / diff=%d / exit=%d"
            % (n9, per.get(n9), c04, per.get(c04), s["carrier_specific"],
               s["diff"], s["exit_code"]))

    # ⑧b 正控：只有已登记用例在登记侧偏离 ⇒ CARRIER_SPECIFIC（第三态确实会出现）
    plan = {"kg": {"C04": _accepted(), "N9": _accepted()},
            "onto": {"C04": _rejected("QUERY_FORM_REJECTED"), "N9": _accepted()}}
    out = run_gate(_stub_transport(plan), cases=reg_cases)
    s = out["summary"]
    control("⑧b仅已登记用例在登记侧偏离 ⇒ CARRIER_SPECIFIC（不进 pass、diff=0、exit=3）",
            s["verdict"] == "CARRIER_SPECIFIC" and s["carrier_specific"] == 1
            and s["pass"] == 1 and s["diff"] == 0
            and s["registry_issues"] == []
            and s["exit_code"] == EXIT_CARRIER_SPECIFIC,
            "verdict=CARRIER_SPECIFIC / carrier_specific=1 / pass=1 / diff=0 / "
            "registry_issues=[] / exit=3",
            "verdict=%s / carrier_specific=%d / pass=%d / diff=%d / registry_issues=%s / exit=%d"
            % (s["verdict"], s["carrier_specific"], s["pass"], s["diff"],
               json.dumps(s["registry_issues"], ensure_ascii=False), s["exit_code"]))

    # ⑨ 登记项实测两侧都跑通 ⇒ REGISTRY_STALE（登记失效，且绝不判 PASS）
    plan = {"kg": {"C04": _accepted(), "N9": _accepted()},
            "onto": {"C04": _accepted(), "N9": _accepted()}}
    out = run_gate(_stub_transport(plan), cases=reg_cases,
                   registry=[dict(CARRIER_SPECIFIC_CASES[0])])
    s = out["summary"]
    codes = [issue["code"] for issue in s["registry_issues"]]
    control("⑨登记项实测两侧都通过 ⇒ REGISTRY_STALE（登记失效，不得判 PASS）",
            s["verdict"] == REGISTRY_STALE and REGISTRY_STALE in codes
            and s["exit_code"] == EXIT_DIFF,
            "verdict=REGISTRY_STALE / 含 REGISTRY_STALE / exit=1",
            "verdict=%s / codes=%s / exit=%d" % (s["verdict"], codes, s["exit_code"]))

    # ⑩ evidence 为空 / 缺采集时点 ⇒ REGISTRY_INVALID
    for label, bad_evidence in (("evidence 为空", ""),
                                ("evidence 缺采集时点",
                                 "GraphDB 不支持 {n,m} 属性路径（此处故意不写时点）")):
        plan = {"kg": {"C04": _accepted(), "N9": _accepted()},
                "onto": {"C04": _rejected("QUERY_FORM_REJECTED"), "N9": _accepted()}}
        out = run_gate(_stub_transport(plan), cases=reg_cases,
                       registry=[dict(CARRIER_SPECIFIC_CASES[0],
                                      evidence=bad_evidence)])
        s = out["summary"]
        hit = [i for i in s["registry_issues"]
               if i["code"] == REGISTRY_INVALID and i["field"] == "evidence"]
        control("⑩%s ⇒ REGISTRY_INVALID（且不得判 PASS）" % label,
                bool(hit) and s["verdict"] == REGISTRY_INVALID
                and s["exit_code"] == EXIT_DIFF,
                "verdict=REGISTRY_INVALID / field=evidence 命中 / exit=1",
                "verdict=%s / field=evidence命中=%s / exit=%d"
                % (s["verdict"], bool(hit), s["exit_code"]))

    # ⑪ 登记 carrier 与实测偏离侧不符 ⇒ REGISTRY_INVALID（该条仍判 DIFF，不得当载体特有项）
    plan = {"kg": {"C04": _accepted(), "N9": _accepted()},
            "onto": {"C04": _rejected("QUERY_FORM_REJECTED"), "N9": _accepted()}}
    out = run_gate(_stub_transport(plan), cases=reg_cases,
                   registry=[dict(CARRIER_SPECIFIC_CASES[0], carrier="kg")])
    s = out["summary"]
    per = {row["id"]: row["verdict"] for row in out["results"]}
    hit = [i for i in s["registry_issues"]
           if i["code"] == REGISTRY_INVALID and i["field"] == "carrier"]
    control("⑪登记 carrier 与实测偏离侧不符 ⇒ REGISTRY_INVALID（该条仍判 DIFF）",
            bool(hit) and s["verdict"] == REGISTRY_INVALID
            and per.get(c04) == "DIFF" and s["carrier_specific"] == 0
            and s["exit_code"] == EXIT_DIFF,
            "verdict=REGISTRY_INVALID / carrier命中 / %s=DIFF / carrier_specific=0 / exit=1"
            % c04,
            "verdict=%s / carrier命中=%s / %s=%s / carrier_specific=%d / exit=%d"
            % (s["verdict"], bool(hit), c04, per.get(c04), s["carrier_specific"],
               s["exit_code"]))

    # ⑫ 出厂登记表本身必须干净（结构 + 与真实用例清单对照均无问题）
    shipped_issues = validate_registry(CARRIER_SPECIFIC_CASES, CASES)
    check("⑫出厂登记表在真实用例清单上结构自检干净", shipped_issues == [],
          json.dumps(shipped_issues, ensure_ascii=False))

    passed = sum(1 for _, ok, _ in checks if ok) + sum(1 for _, ok, _, _ in controls if ok)
    total = len(checks) + len(controls)
    for name, ok, detail in checks:
        if not ok:
            stream.write("FAIL %s %s\n" % (name, detail))
    stream.write("\n登记表正/负控（逐条打印期望 vs 实际）：\n")
    for name, ok, expected, actual in controls:
        stream.write("%s %s\n    期望：%s\n    实际：%s\n"
                     % ("[PASS]" if ok else "[FAIL]", name, expected, actual))
    stream.write("测试：%d/%d PASS\n" % (passed, total))
    stream.write("RESULT: %s\n" % ("PASS" if passed == total else "FAIL"))
    return EXIT_PASS if passed == total else EXIT_DIFF


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="跨载体只读护栏一致性门禁（M3.4/M3.5）；退出码 "
                    "0=PASS 1=DIFF/登记表自检失败 2=BLOCKED 3=CARRIER_SPECIFIC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-kg", default=DEFAULT_BASE_KG, help="方案1 KG 服务基址")
    parser.add_argument("--base-onto", default=DEFAULT_BASE_ONTO, help="方案2 ONTO 服务基址")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次请求超时（秒）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--selftest", action="store_true", help="离线自检（含负控），不联网")
    parser.add_argument("--only", default=None,
                        help="只跑指定载体（kg / onto），默认两侧都跑")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.selftest:
        return selftest()

    bases = {"kg": args.base_kg, "onto": args.base_onto}
    carriers = [args.only] if args.only else list(CARRIERS)
    for carrier in carriers:
        if carrier not in CARRIERS:
            sys.stderr.write("PARAM_ERROR: --only 只能是 %s\n" % " / ".join(CARRIERS))
            return EXIT_BLOCKED

    out = run_gate(Transport(bases, timeout=args.timeout), carriers=carriers)
    if args.json:
        payload = dict(out)
        payload["bases"] = bases
        payload["generatedBy"] = "shared/verify/guard_conformance.py"
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        print_report(out, carriers, bases)

    # 退出码由 summary 决定：0=PASS / 1=DIFF 或登记表自检失败 / 2=BLOCKED /
    # 3=仅存在**已登记**的载体特有项（★ 不是 exit 0：登记不构成绿的条件）
    return out["summary"]["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
