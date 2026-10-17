# -*- coding: utf-8 -*-
"""方案2 本体 —— CQ（Competency Question）的 SPARQL 探针（唯一真源）。

条数由契约派生：5 个固定族（CQ-T 14 / CQ-X 12 / CQ-C 10 / CQ-D 8 / CQ-A 4 = 48）
加**每个故障模式一条** CQ-F（当前 23 ⇒ 合计 71 条），见 `expected_count()`。

用途：
  * `docs\\04-appendix-ORSD.md`（M2.4 交付物）的 CQ 表由本模块生成（见 `tmp\\_gen_orsd.py`）；
  * `scheme2-onto\\tests\\test_integrity.py` 的 **T5（CQ 全覆盖）** 直接 import 本模块运行。

设计约束：
  * 零硬编码词汇表：类/关系/指标/故障模式/因果边全部现读 `shared\\model\\*.yaml` 与 `shared\\mapping\\type-map.yaml`；
  * 零硬编码阈值：CQ-F 探针只读 `ccf:J-*` 判据个体的 `ccf:jThresholdReference` 档位引用，不出现数字；
  * 探针的期望值分三类：`nonempty`（≥1 行）、`coverage`（键属性/关系覆盖率=100%）、`report`（返回可判定的布尔或清单，行数≥1 即可）。

用法：
  python scheme2-onto\\tests\\cq_probes.py [--out tmp\\_cq_probe_report.txt] [--fixture]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys

import rdflib
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# ★ 2026-10-08（待办批次 3）：本文件原先用 **raw 字符串 + 反斜杠** 写相对路径
#   （例：`os.path.join(root, r'shared/mapping/type-map.yaml')`）⇒ 在 Linux/CI 容器里
#   反斜杠是**普通文件名字符**，路径解析失败 ⇒ `online:integrity-tests` 实测崩在
#   `cq_probes.py:47` FileNotFoundError。全仓同类功能性站点共 **9 处**（cq_probes.py 7 +
#   test_integrity.py 2），已统一改为 **POSIX 风格相对路径**（正斜杠在 Windows 上同样可用）。
ONTO_FILES = ['cc-core', 'cc-topology', 'cc-metric', 'cc-fault', 'cc-causal', 'cc-action', 'cc-audit']
# 【去 tmp 化】CQ 探针的 ABox 全部落在 scheme2-onto/tests/fixtures\（耐久；tmp\ 会被清理）。
#   真机口径 = 真机投影快照 ccf-20260928T155516Z-937b79（2965 三元组，CONSTRUCT 全图导出，随快照冻结）。
#   旧写法 [r'tmp/cc_ops_live2.ttl'] 已弃用：tmp 清理后真机口径不可复现，且与 test_integrity.ABOX_LIVE
#   分裂成 60/62（旧投影，缺 30 条 cc:Table 的 cc:schemaName）vs 61/62 两个口径。
ABOX_DEFAULT = [r'scheme2-onto/tests/fixtures/abox-live5.ttl']
# 契约完备的合成 ABox（SHACL 正向夹具，13 条必填关系链与键属性齐备）+ 规则注入夹具（全部耐久化）
ABOX_FIXTURE = [r'scheme2-onto/tests/fixtures/abox-live5.ttl',
                r'scheme2-onto/tests/fixtures/cc_shacl_ok.ttl',
                r'scheme2-onto/tests/fixtures/cc_probe_inject_a.ttl',
                r'scheme2-onto/tests/fixtures/cc_probe_inject_b.ttl',
                r'scheme2-onto/tests/fixtures/cc_probe_inject_c.ttl',
                r'scheme2-onto/tests/fixtures/cc_probe_inject_d.ttl',
                r'scheme2-onto/tests/fixtures/cc_proj_fix.ttl']


# --------------------------------------------------------------------------- 契约装载
def load_contract(root=ROOT):
    tm = yaml.safe_load(io.open(os.path.join(root, 'shared/mapping/type-map.yaml'), encoding='utf-8'))
    fm = yaml.safe_load(io.open(os.path.join(root, 'shared/model/fault-modes.yaml'), encoding='utf-8'))
    ce = yaml.safe_load(io.open(os.path.join(root, 'shared/model/causal-edges.yaml'), encoding='utf-8'))
    me = yaml.safe_load(io.open(os.path.join(root, 'shared/model/metrics.yaml'), encoding='utf-8'))
    ns = tm['namespaces']['onto']
    prefixes = {k: str(v) for k, v in ns.items()}
    types = tm['types']
    metrics = tm['metrics']

    def modes(obj):
        if isinstance(obj, dict):
            for key in ('fault_modes', 'modes', 'items'):
                if key in obj:
                    return list(obj[key])
            return [v for v in obj.values() if isinstance(v, dict) and 'code' in v] or list(obj.values())
        return obj
    fl = modes(fm)
    return {'tm': tm, 'types': types, 'metrics': metrics, 'fault_modes': fl,
            'edges': ce['edges'], 'prefixes': prefixes, 'causal_raw': ce, 'fault_raw': fm,
            'metrics_raw': me, 'thresholds': fm.get('thresholds') or []}


def iri(prefixes, qname):
    p, local = qname.split(':', 1)
    return rdflib.URIRef(prefixes[p] + local)


# --------------------------------------------------------------------------- 探针构造
def build_probes(c):
    pf = c['prefixes']
    cc, ccf, ccc, ccm, ccp, cca = ('cc:%s' % '', 'ccf:', 'ccc:', 'ccm:', 'ccp:', 'cca:')
    P = []           # 探针列表
    types = c['types']

    # ---------- CQ-T 14 条：拓扑能力（每类「实例 + 契约键属性 + 层」）----------
    concrete = [k for k, v in types.items() if k != 'Evidence']
    order = ['CreditCardApp', 'AppInstance', 'AppEndpoint', 'JdbcDataSource', 'Container', 'Host',
             'GaussDBCluster', 'GaussDBInstance', 'ConnectionPool', 'DatabaseSessionPool', 'Session',
             'Query', 'Table']
    order = [t for t in order if t in types] + [t for t in concrete if t not in order]
    keys_of = {}
    for t in order:
        spec = types[t]
        keys = list(spec.get('key_attrs') or [])
        attrs = spec.get('attrs') or {}
        props = [attrs[k]['onto_property'] for k in keys if k in attrs]
        keys_of[t] = props
    for i, t in enumerate(order, 1):
        props = keys_of[t]
        spec = types[t]
        if not props:      # 抽象类 / 无契约键属性（如 ConnectionPool）→ 可答性由「计数（可为 0）」承载
            P.append(dict(
                id='CQ-T-%02d' % i, family='CQ-T',
                text='给定一份 ABox，能否列出全部 `%s` 实例及其所属层？（契约中该类型 `abstract=%s`，允许空集）'
                     % (t, bool(spec.get('abstract'))),
                uc='拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）',
                needs=['cc:%s' % t, 'cc:ccfLayer'],
                answerer='SPARQL 探针 %s（计数式，空集也是确定答案）' % ('CQ-T-%02d' % i),
                sparql='SELECT (COUNT(?s) AS ?n) WHERE { ?s a cc:%s }' % t,
                expect='nonempty', type_code=t, note='abstract 类型允许空集'))
            continue
        sel = ' '.join('?k%d' % (j + 1) for j in range(len(props)))
        # 全部键属性用 OPTIONAL 绑定：主体锚定在类型三元组上，缺哪个键属性就点名哪个（ORSD §5 投影缺口证据）
        body = ' . '.join('OPTIONAL { ?s cc:%s ?k%d }' % (p.split(':')[1], j + 1)
                          for j, p in enumerate(props))
        P.append(dict(
            id='CQ-T-%02d' % i, family='CQ-T',
            text='给定一份 ABox，能否列出全部 `%s` 实例，并给出其契约键属性（%s）与所属层？'
                 % (t, '、'.join('`%s`' % p for p in props) or '—'),
            uc='拓扑前置能力（C1 `A-*` 与 C2 `B-*` 用例的前置条件）',
            needs=['cc:%s' % t] + props + ['cc:ccfLayer'],
            answerer='SPARQL 探针 %s（`scheme2-onto\\tests\\cq_probes.py`）；形状 `ccs:%sShape`' % ('CQ-T-%02d' % i, t),
            sparql='SELECT ?s %s WHERE { ?s a cc:%s . %s }' % (sel, t, body),
            expect='coverage', coverage=props, coverage_on='?s', type_code=t))
    # 派生注释型 Evidence（docs/04:295-297 不建形状，但 CQ 仍须可答）
    ev_attrs = ['cc:recordId', 'cc:metricCode', 'cc:status', 'cc:rawValue', 'cc:rawUnit',
                'cc:reason', 'cc:probe', 'cc:capturedAt']
    ev_id = 'CQ-T-%02d' % (len(P) + 1)      # 13 个具体/抽象类型已占 CQ-T-01..13 ⇒ Evidence = CQ-T-14
    P.append(dict(
        id=ev_id, family='CQ-T',
        text='给定一份 ABox，能否列出全部 `Evidence` 个体及其 8 个溯源属性（recordId / metricCode / '
             'status / rawValue / rawUnit / reason / probe / capturedAt）？',
        uc='审计前置能力（C5 审计链、`CQ-A-01` 的前置）',
        needs=['cc:Evidence'] + ev_attrs,
        answerer='SPARQL 探针 %s；无 SHACL 形状（派生类，docs\\04:295-297 明示不建）' % ev_id,
        sparql='SELECT ?s ?k1 ?k2 ?k3 ?k4 ?k5 ?k6 ?k7 ?k8 WHERE { ?s a cc:Evidence . '
               + ' . '.join(('OPTIONAL { ?s cc:%s ?k%d }') % (a.split(':')[1], j + 1)
                            for j, a in enumerate(ev_attrs)) + ' }',
        expect='coverage', coverage=ev_attrs, coverage_optional=['cc:reason', 'cc:rawUnit'], coverage_on='?s',
        type_code='Evidence',
        note='`cc:reason` / `cc:rawUnit` 是**条件必填**（契约 §D3：`reason` 仅 `status ∈ {notCollected, collectedEmpty}`；'
             '`rawUnit` 仅无量纲指标缺失）⇒ 列入 `coverage_optional`，缺失只报数不计失败'))

    # ---------- CQ-X 12 条：跨层可达 ----------
    hops = [
        ('CreditCardApp', 'cc:exposes', 'AppEndpoint', '列出该应用对外的全部端点',
         '给定一个 `CreditCardApp`，能否列出它对外暴露的全部 `AppEndpoint`？'),
        ('CreditCardApp', 'cc:hasAppInstance', 'AppInstance', '列出该应用的全部实例',
         '给定一个 `CreditCardApp`，能否列出它的全部 `AppInstance`？'),
        ('AppInstance', 'cc:usesDataSource', 'JdbcDataSource', '定位应用实例用的数据源',
         '给定一个 `AppInstance`，能否定位到它使用的 `JdbcDataSource`？'),
        ('AppInstance', 'cc:runsIn', 'Container', '定位应用实例所在容器',
         '给定一个 `AppInstance`，能否定位到它运行所在的 `Container`？'),
        ('Container', 'cc:deployedOn', 'Host', '定位容器所在宿主机',
         '给定一个 `Container`，能否定位到它部署所在的 `Host`？'),
        ('JdbcDataSource', 'cc:connectsTo', 'GaussDBCluster', '定位数据源连的集群',
         '给定一个 `JdbcDataSource`，能否定位到它连接的 `GaussDBCluster`？'),
        ('JdbcDataSource', 'cc:resolvesTo', 'GaussDBInstance', '定位数据源实际落的实例',
         '给定一个 `JdbcDataSource`，能否定位到它实际路由到的 `GaussDBInstance`？'),
        ('GaussDBCluster', 'cc:hasDbInstance', 'GaussDBInstance', '列出集群下的实例',
         '给定一个 `GaussDBCluster`，能否列出它下面的全部 `GaussDBInstance`？'),
        ('GaussDBInstance', 'cc:hasSessionPool', 'DatabaseSessionPool', '列出实例上的会话池',
         '给定一个 `GaussDBInstance`，能否列出它上面的全部 `DatabaseSessionPool`？'),
    ]
    for i, (src, rel, dst, desc, text) in enumerate(hops, 1):
        P.append(dict(
            id='CQ-X-%02d' % i, family='CQ-X',
            text='%s（%s）' % (text, desc),
            uc='C2 跨层定界（`B-*` 用例）',
            needs=['cc:%s' % src, rel, 'cc:%s' % dst],
            answerer='SPARQL 探针 %s（单跳可达性）' % ('CQ-X-%02d' % i),
            sparql='SELECT ?s ?t WHERE { ?s a cc:%s ; %s ?t . ?t a cc:%s }' % (src, rel, dst),
            expect='nonempty', type_code='%s-%s-%s' % (src, dst, rel.split(':')[1])))
    # ---------- CQ-X 剩余 3 条：复合路径（含文档 §2.2 的 CQ-X-05 原文示例）----------
    P.append(dict(
        id='CQ-X-10', family='CQ-X',
        text='给定一个 `CreditCardApp`，能否经 `JdbcDataSource → GaussDBCluster → GaussDBInstance → '
             'DatabaseSessionPool` 定位到它实际消费的会话池？（docs\\04 §2.2 `CQ-X-05` 原文示例）',
        uc='C2 跨层定界（`B-*` 用例；文档给出的复合路径示例）',
        needs=['cc:CreditCardApp', 'cc:hasAppInstance', 'cc:usesDataSource', 'cc:connectsTo',
               'cc:resolvesTo', 'cc:hasDbInstance', 'cc:hasSessionPool'],
        answerer='SPARQL 探针 CQ-X-10（5 跳复合可达性；`connectsTo` 与 `resolvesTo` 两条落库路径取并）',
        sparql=('SELECT ?app ?pool WHERE { ?app a cc:CreditCardApp ; cc:hasAppInstance ?ai . '
                '?ai cc:usesDataSource ?ds . { ?ds cc:connectsTo ?cl } UNION { ?ds cc:resolvesTo ?cl } . '
                '?cl cc:hasDbInstance ?dbi . ?dbi cc:hasSessionPool ?pool }'),
        expect='nonempty', type_code='App-Pool-composite'))
    P.append(dict(
        id='CQ-X-11', family='CQ-X',
        text='给定一个 `DatabaseSessionPool`，能否经 `Session → Query → Table` 定位到它承载的会话所执行的'
             '语句及这些语句访问的表？（3 跳复合可达性）',
        uc='C2 跨层定界 + C1 单层定位（`B-*` / `A-*` 用例）',
        needs=['cc:DatabaseSessionPool', 'cc:hasSession', 'cc:runsQuery', 'cc:accesses', 'cc:Table'],
        answerer='SPARQL 探针 CQ-X-11（该 3 跳在真机 ABox 上为空 —— 见 ORSD §5 投影缺口 D-2）',
        sparql=('SELECT ?pool ?tbl WHERE { ?pool a cc:DatabaseSessionPool ; cc:hasSession ?s . '
                '?s cc:runsQuery ?q . ?q cc:accesses ?tbl }'),
        expect='nonempty', type_code='Pool-Table-composite'))
    P.append(dict(
        id='CQ-X-12', family='CQ-X',
        text='给定一个 `AppInstance`，能否用 OWL 属性链公理 `cc:indirectlyHostedOn`'
             '（`runsIn ∘ deployedOn`）直接推出它所在的 `Host`（而非手写两跳查询）？',
        uc='C2 跨层定界（`B-*` 用例）+ 载体能力对照（OWL 2 RL 独有：属性链）',
        needs=['cc:AppInstance', 'cc:indirectlyHostedOn', 'cc:runsIn', 'cc:deployedOn', 'cc:Host'],
        answerer='`cc-topology.ttl` 的 `owl:propertyChainAxiom ( cc:runsIn cc:deployedOn )`'
                 '（真机 owl2-rl 物化实测见 `tmp\\_live_tbox_report.txt` 阶段 4）+ 探针 CQ-X-12',
        sparql=('SELECT ?ai ?h WHERE { cc:indirectlyHostedOn owl:propertyChainAxiom ?chain . '
                '?chain rdf:first cc:runsIn ; rdf:rest ?rest . ?rest rdf:first cc:deployedOn . '
                '?ai a cc:AppInstance ; cc:runsIn ?c . ?c cc:deployedOn ?h . ?h a cc:Host }'),
        expect='nonempty', type_code='App-Host-chain',
        note='探针同时断言「公理在 TBox 上」与「该 ABox 上两跳可达」；OWL 属性链的**推理物化**由 GraphDB '
             'owl2-rl 承担（rdflib 不推理，故探针不依赖推理，见 H-2）'))

    # ---------- CQ-F 14 条：故障判据（每个故障模式一条）----------
    modes = c['fault_modes']
    mode_rule = {}       # 故障模式 code → 规则 id（由 cc-audit.ttl 的 Finding 反向取得，非硬编码）
    g_audit = rdflib.Graph()
    g_audit.parse(os.path.join(ROOT, 'scheme2-onto/ontology/cc-audit.ttl'), format='turtle')
    CCF = rdflib.Namespace(pf['ccf']); CCP = rdflib.Namespace(pf['ccp'])
    for f in g_audit.subjects(rdflib.RDF.type, CCF.Finding):
        # ★ Finding 上的形态学/归属属性在 ccp: 命名空间（cc-audit.ttl 实测：ccp:findingFaultMode / ccp:associatedRule）
        m = list(g_audit.objects(f, CCP.findingFaultMode))
        r = list(g_audit.objects(f, CCP.associatedRule))
        if m and r:
            mode_rule[str(m[0]).rsplit('#', 1)[-1]] = str(r[0]).rsplit('#', 1)[-1]
    # ★ 规则文件名以 ccp:ruleFile（cc-audit.ttl 权威声明）为准，不由个体名猜（曾误印 `Rule-r03.rq`）
    rulefile = {}
    for rr in g_audit.subjects(rdflib.RDF.type, CCP.Rule):
        rf = list(g_audit.objects(rr, CCP.ruleFile))
        if rf:
            rulefile[str(rr).rsplit('#', 1)[-1]] = str(rf[0])
    g_fault = rdflib.Graph()
    g_fault.parse(os.path.join(ROOT, 'scheme2-onto/ontology/cc-fault.ttl'), format='turtle')

    def mcode(m):
        return m.get('code') or m.get('neutralCode')

    for i, m in enumerate(sorted(modes, key=mcode), 1):
        code = mcode(m)
        label = m.get('name') or m.get('display_name') or code
        comp = m.get('component_type')
        judges = [s for s in g_fault.subjects(CCF.judges, CCF[code])]
        det = [s for s in judges if str(list(g_fault.objects(s, CCF.jRole))[0]).endswith('detect')] if judges else []
        thr = sorted({str(o) for s in judges for o in g_fault.objects(s, CCF.jThresholdReference)})
        rule = mode_rule.get(code)
        if rule:
            rc = rule.rsplit('-', 1)[-1]          # ccp:Rule-r03 → r03
            rf = rulefile.get(rule, 'scheme2-onto/rules/%s.rq' % rc)
            ans = ('规则文件 `%s`（载体 `ccp:%s`，由 `ccp:Exec-%s` 承载）+ 探针 CQ-F-%02d' % (rf, rule, rc, i))
        else:
            ans = ('TBox 判据个体 `ccf:J-*`（契约该模式无独立规则：docs\\04 §6 只给 7 条规则）+ 探针 CQ-F-%02d' % i)
        P.append(dict(
            id='CQ-F-%02d' % i, family='CQ-F',
            text='给定一组观测事实，能否判定 `%s`（%s）的判据是否全部满足，并列出满足 / 未满足的判据？' % (code, label),
            uc='C1 单层定位（`A-*` 用例）',
            needs=['ccf:%s' % code, 'ccf:judgedBy', 'ccf:jMetricCode', 'ccf:jOperandKind',
                   'ccf:jPredicate', 'ccf:jThresholdReference', 'ccf:fmComponentType'],
            answerer=ans,
            sparql=('SELECT ?judge ?metricCode ?operandKind ?predicate ?tier WHERE { ccf:%s ccf:judgedBy ?judge . '
                    '?judge ccf:jRole "detect" ; ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind . '
                    'OPTIONAL { ?judge ccf:jPredicate ?predicate } '
                    'OPTIONAL { ?judge ccf:jThresholdReference ?tier } }' % code),
            expect='nonempty', type_code=code,
            extra=dict(component=comp, criteria=len(det), tiers=thr, rule=rule)))
    # ---------- 因果图数据（CQ-C / CQ-D 复用），全部取自契约 YAML ----------
    causal = c['causal_raw']
    edges = causal['edges']

    def chains_of(e):
        ch = e['chain']
        return ch if isinstance(ch, list) else [ch]
    chains = sorted({x for e in edges for x in chains_of(e)})
    term = {}
    for ch in chains:
        ee = [e for e in edges if ch in chains_of(e)]
        causes = {e['from'] for e in ee}
        effs = [e['to'] for e in ee if e['to'] not in causes] or [ee[-1]['to']]
        term[ch] = effs[-1]

    # ★ rdflib 不支持 `{n,m}` 路径修饰符（实测 `ccc:cause{1,4}` 直接 ParseException）。
    #   契约 G8 的「深度 ≤4」在探针里显式枚举 1..4 跳（语义等价），规则文件里的 GraphDB 侧仍可用 `{}`。
    def hop(n, forward=True):
        # 因果边在本体里是 n 元物化（docs/04 §4.5）：ccc:CausalLink 个体持 cause/effect 两端的 IRI。
        # 上游→下游一跳 = `^ccc:cause/ccc:effect`；下游（症状）→上游一跳 = `^ccc:effect/ccc:cause`。
        seq = '^ccc:cause/ccc:effect' if forward else '^ccc:effect/ccc:cause'
        return '/'.join([seq] * n)

    def reach4(sub, obj='?root'):
        return ' UNION '.join('{ %s %s %s }' % (obj, hop(n), sub) for n in range(1, 5))

    # ---------- CQ-C 10 条：因果收敛 ----------
    for i, ch in enumerate(chains, 1):
        sym = term[ch]
        ups = sorted({e['from'] for e in edges if e['to'] == sym})
        P.append(dict(
            id='CQ-C-%02d' % i, family='CQ-C',
            text='给定症状 `%s`，能否沿 `ccc:cause` 向上游收敛出 ≤4 跳的根因集合（故障链 %s，直接上游：%s）？'
                 % (sym, ch, '、'.join('`%s`' % u for u in ups) or '—'),
            uc='C2 跨层定界 / C3 同症状区分（`B-13`/`B-14` + `D-01..D-06` 用例）',
            needs=['ccf:%s' % sym, 'ccc:cause', 'ccc:linkKind', 'ccc:CausalLink', 'ccc:chainCode'],
            answerer='SPARQL 探针 %s（沿 `ccc:CausalLink` 物化个体走 1..4 跳，等价契约 `ccc:cause{1,4}` 的语义；'
                     '跳数上限 4 = G8 因果深度门禁）' % ('CQ-C-%02d' % i),
            sparql=('SELECT DISTINCT ?root ?mode WHERE { %s . '
                    '?root a ccf:FaultMode ; ccf:faultModeCode ?mode }' % reach4('ccf:%s' % sym)),
            expect='nonempty', type_code=ch, note='链 %s 的终端症状 = %s' % (ch, sym)))
    _sym0 = term[chains[0]]
    P.append(dict(
        id='CQ-C-%02d' % (len(chains) + 1), family='CQ-C',
        text='给定症状，能否按「因果强度（`ccc:linkKind`：`causes` > `conditional_causes` > `relates_to`）'
             '+ 跳数」给出**有序**的根因集合？',
        uc='C3 同症状区分（`D-*` 用例；需要排序证据）',
        needs=['ccc:linkKind', 'ccc:cause', 'ccc:effect', 'ccc:CausalLink'],
        answerer='SPARQL 探针 CQ-C-%02d（1/2/3+ 跳分层 UNION + `linkKind` 排序）' % (len(chains) + 1),
        sparql=('SELECT DISTINCT ?root ?kind ?rank WHERE { '
                '{ %s . BIND(1 AS ?rank) } UNION '
                '{ %s . BIND(2 AS ?rank) } UNION '
                '{ %s . BIND(3 AS ?rank) } . '
                '?l0 a ccc:CausalLink ; ccc:cause ?root ; ccc:linkKind ?kind } ORDER BY ?rank ?kind'
                % ('?root %s ccf:%s' % (hop(1), _sym0),
                   '?root %s ccf:%s' % (hop(2), _sym0),
                   '?root %s ccf:%s' % (hop(3), _sym0))),
        expect='nonempty', type_code='ordering',
        note='契约 `causal-edges.yaml` 无「强度」数值字段（只有 linkKind 枚举）⇒ 排序口径 = 枚举序 + 跳数（自定，见诚实清单）'))
    P.append(dict(
        id='CQ-C-%02d' % (len(chains) + 2), family='CQ-C',
        text='能否检测因果图中的环，并判定检测到的环 **⊆ 契约白名单**（`causal-edges.yaml#meta.cycles`）？',
        uc='内部校验（`docs\\04` §8 `T6`：检测到的环 ⊆ 白名单）',
        needs=['ccc:cause', 'ccc:CausalLink', 'ccc:chainCode'],
        answerer='SPARQL 探针 CQ-C-%02d（经 `ccc:CausalLink` 物化个体的双向对检测，等价契约 §8 T6；'
                 '契约字面写法 `ccc:cause{2,4}` 自反匹配在 n 元物化下恒假 ⇒ X-9）+ 契约白名单 `%s`'
                 % (len(chains) + 2, causal['meta']['cycles']),
        sparql=('SELECT DISTINCT ?m1 ?m2 WHERE { ?l1 ccc:cause ?m1 ; ccc:effect ?m2 . '
                '?l2 ccc:cause ?m2 ; ccc:effect ?m1 . FILTER(STR(?m1) < STR(?m2)) }'),
        expect='nonempty', type_code='cycles'))
    P.append(dict(
        id='CQ-C-%02d' % (len(chains) + 3), family='CQ-C',
        text='能否给出**因果深度 ≤4** 的全部可达对（G8 深度门禁的可机检形式）？',
        uc='内部校验（G8 因果深度 ≤4，`docs\\04` §7 G8）',
        needs=['ccc:cause', 'ccc:CausalLink'],
        answerer='SPARQL 探针 CQ-C-%02d（1..4 跳可达对枚举，等价 `ccc:cause{1,4}`；≥5 跳溢出反例由 `test_integrity.py` T6 断言为空）'
                 % (len(chains) + 3),
        sparql=('SELECT DISTINCT ?s ?t WHERE { %s }'
                % ' UNION '.join('{ ?s %s ?t }' % hop(n) for n in range(1, 5))),
        expect='nonempty', type_code='depth4'))

    # ---------- CQ-D 8 条：区分与排除 ----------
    disc = causal.get('discriminators') or []
    mode_codes = {mcode(m) for m in modes}
    k = 0
    for d in disc:
        mid = 'ccm:m_%s' % d['discriminator_metric'].replace('.', '_')
        for side in (0, 1):
            k += 1
            mode = d['pair'][side]
            other = d['pair'][1 - side]
            in_vocab = mode in mode_codes
            note = ('区分力来自**否定性证据**：一侧判据未满足 ⇔ 反侧指标正常（`ccc:discriminatorRule`）'
                    if in_vocab else
                    '★ 契约缺陷 X-10：`%s` 不在 `fault-modes.yaml` 的 14 模式词表内 ⇒ 该侧无判据个体可比对，'
                    '探针降级为「区分器个体确实命名了该对 + 指向契约 discriminator 指标」' % mode)
            body = ('ccf:%s ccf:judgedBy ?judge . ?judge ccf:jRole "detect" ; '
                    'ccf:jMetricCode ?metricCode ; ccf:jOperandKind ?operandKind .' % mode
                    if in_vocab else '')
            sel = '?disc ?metric ?judge ?metricCode ?operandKind' if in_vocab else '?disc ?metric'
            P.append(dict(
                id='CQ-D-%02d' % k, family='CQ-D',
                text='给定同症状「%s」，能否用契约 discriminator 指标 `%s` 把 `%s` 与 `%s` 区分开（%s 侧判据）？'
                     % (d.get('symptom_shared', '—'), d['discriminator_metric'], d['pair'][0], d['pair'][1], mode),
                uc='C3 同症状区分 + C4 否定性证据（`D-*` / `N-*` 用例）',
                needs=['ccf:%s' % mode, mid, 'ccf:jMetricCode', 'ccf:jOperandKind', 'ccc:Discriminator',
                       'ccc:discriminatorMetric', 'ccc:discriminates'],
                answerer='`ccc:Discriminator` 个体（`ccc:discriminates` 覆盖 `%s` 与 `%s`；`ccc:discriminatorMetric` '
                         '= `%s`）+ 探针 CQ-D-%02d' % (mode, other, d['discriminator_metric'], k),
                sparql=('SELECT %s WHERE { ?disc a ccc:Discriminator ; ccc:discriminates ccf:%s , ccf:%s ; '
                        'ccc:discriminatorMetric ?metric . %s }' % (sel, mode, other, body)),
                expect='nonempty', type_code='%s|%s' % (d['pair'][0], mode), note=note))
    P.append(dict(
        id='CQ-D-%02d' % (k + 1), family='CQ-D',
        text='能否用「某指标正常」作为**否定性证据**排除某个根因（契约的排除要求）？',
        uc='C4 否定性证据（`N-*` 用例；F5 排除法）',
        needs=['ccf:exclusionRequirementText', 'ccf:jRole', 'ccf:judgedBy'],
        answerer='`ccf:J-*` 判据个体上的 `ccf:exclusionRequirementText`（真机 4 条）+ 探针 CQ-D-%02d' % (k + 1),
        sparql='SELECT ?judge ?text WHERE { ?judge ccf:exclusionRequirementText ?text }',
        expect='nonempty', type_code='exclusion'))
    P.append(dict(
        id='CQ-D-%02d' % (k + 2), family='CQ-D',
        text='给定一个症状，能否枚举**全部**可解释它的候选故障模式及其判据数（供互斥排除）？',
        uc='C3 同症状区分（`D-*` 用例；候选集枚举）',
        needs=['ccf:FaultMode', 'ccf:judgedBy', 'ccf:faultModeCode', 'ccf:fmComponentType'],
        answerer='SPARQL 探针 CQ-D-%02d（按模式分组计数判据）' % (k + 2),
        sparql=('SELECT ?mode (COUNT(?j) AS ?n) WHERE { ?m a ccf:FaultMode ; ccf:faultModeCode ?mode ; '
                'ccf:judgedBy ?j } GROUP BY ?mode'),
        expect='nonempty', type_code='candidates'))

    # ---------- CQ-A 4 条：审计与溯源 ----------
    P.append(dict(
        id='CQ-A-01', family='CQ-A',
        text='给定一条 `Finding`，能否回溯到它的规则、规则输入指标与产出的动作提案（决策链可复算）？',
        uc='对照维度「审计性」+ 内部校验（`docs\\04` §8 `T5` 的审计链部分）',
        needs=['ccf:Finding', 'ccp:RuleExecution', 'ccp:producedFinding', 'ccp:hasRuleInput',
               'ccp:proposedAction', 'ccp:appliesRule'],
        answerer='`cc-audit.ttl` 的 7 个 `ccp:Exec-r01..r07` + 探针 CQ-A-01',
        sparql=('SELECT ?exec ?rule ?f ?metric ?action WHERE { ?exec a ccp:RuleExecution ; ccp:appliesRule ?rule ; '
                'ccp:producedFinding ?f ; ccp:hasRuleInput ?metric . OPTIONAL { ?exec ccp:proposedAction ?action } }'),
        expect='nonempty', type_code='finding-chain'))
    P.append(dict(
        id='CQ-A-02', family='CQ-A',
        text='给定一份 ABox，能否把任一实体上的一条观测结论回溯到它的 CCF 记录 id、采集来源与采集时间？',
        uc='对照维度「审计性」+ 银行审计的确定性要求（`docs\\04:123`）',
        needs=['cc:hasEvidence', 'cc:Evidence', 'cc:recordId', 'cc:probe', 'cc:capturedAt'],
        answerer='`ccp:` / PROV-O 对称落地的 `cc:hasEvidence` + 探针 CQ-A-02',
        sparql=('SELECT ?s ?ev ?rid ?probe ?t WHERE { ?s cc:hasEvidence ?ev . ?ev cc:recordId ?rid ; '
                'cc:probe ?probe ; cc:capturedAt ?t }'),
        expect='nonempty', type_code='evidence-trace'))
    P.append(dict(
        id='CQ-A-03', family='CQ-A',
        text='给定一条因果边，能否给回它在契约中的出处（`causal-edges.yaml#edges[id=Ex]`）？',
        uc='内部校验（`docs\\04` §8 `T4`：因果边 provenance 的 IRI ∈ 契约）',
        needs=['ccc:CausalLink', 'ccc:provenance', 'ccc:ContractProvenance',
               'ccc:provenanceEdgeId', 'ccc:provenanceDocument'],
        answerer='18 个 `ccc:ContractProvenance`（`prov-E1..E18`，携带 `ccc:provenanceEdgeId` / '
                 '`ccc:provenanceDocument` / `ccc:provenanceToken`）+ 探针 CQ-A-03',
        sparql=('SELECT ?link ?prov ?edgeId ?doc WHERE { ?link a ccc:CausalLink ; ccc:provenance ?prov . '
                '?prov a ccc:ContractProvenance ; ccc:provenanceEdgeId ?edgeId ; '
                'ccc:provenanceDocument ?doc }'),
        expect='nonempty', type_code='causal-provenance'))
    P.append(dict(
        id='CQ-A-04', family='CQ-A',
        text='能否证明「每条规则都有执行记录、每条执行都有产出、每个产出都归到一条规则」'
             '（发布后可审计的闭环）？',
        uc='对照维度「审计性」+ 内部校验（`docs\\04` §8 `T2`/`T8`）',
        needs=['ccp:Rule', 'ccp:RuleExecution', 'ccf:Finding', 'ccp:associatedRule', 'cca:RiskLevel'],
        answerer='`cc-audit.ttl`（7 规则 / 7 执行 / 7 发现）+ 探针 CQ-A-04',
        sparql=('SELECT ?rule (COUNT(?f) AS ?n) WHERE { ?rule a ccp:Rule . ?f a ccf:Finding ; '
                'ccp:associatedRule ?rule } GROUP BY ?rule'),
        expect='nonempty', type_code='rule-closure'))
    return P


# --------------------------------------------------------------------------- 运行器
def run(probes, abox_files, root=ROOT, verbose=False):
    g = rdflib.Graph()
    for f in ONTO_FILES:
        g.parse(os.path.join(root, 'scheme2-onto/ontology/%s.ttl' % f), format='turtle')
    abox_triples = 0
    for f in abox_files:
        gg = rdflib.Graph()
        gg.parse(os.path.join(root, f), format='turtle')
        abox_triples += len(gg)
        for t in gg:
            g.add(t)
    res = []
    for p in probes:
        rows = list(g.query(p['sparql']))
        n = len(rows)
        expect = p['expect']
        if expect == 'nonempty':
            ok = n >= 1
            detail = '%d 行' % n
        elif expect == 'coverage':
            subj = {}
            for r in rows:
                subj[str(r[0])] = r
            opt = set(p.get('coverage_optional') or [])
            miss, cond = [], []
            total = 0
            for r in rows:
                for j, prop in enumerate(p['coverage']):
                    total += 1
                    if r[j + 1] is None:
                        (cond if prop in opt else miss).append(
                            '%s.%s' % (str(r[0]).rsplit('/', 1)[-1], prop))
            ok = bool(rows) and not miss
            # 【M2.23 可复现性】miss / cond 的**收集顺序** = 本探针 SPARQL 的**行序**；rdflib 的
            #   Memory store 内层是 set ⇒ 行序随 PYTHONHASHSEED 变。判定变量 `ok` 只用到 len()，
            #   与序无关（故本行排序刻意放在 `ok` **之后**：判定逻辑与阈值零改动）；但报告文本要用
            #   `cond[0]`（条件可选项的样例名）与 `miss[:3]`（缺项清单前 3 个）⇒ 不切前排序时，
            #   同一真机状态连跑会得到不同报告（实测 4 个 hash seed → 4 个不同 sha256，
            #   仅 CQ-T-14 一行不同）。排序是**唯一**改动，不增删任何条目。
            miss, cond = sorted(miss), sorted(cond)
            extra = '' if not cond else '；条件可选 %d 处未绑定（%s）' % (len(cond), cond[0])
            detail = '实例 %d 个，键属性覆盖 %d/%d%s%s' % (
                len(subj), total - len(miss) - len(cond), total,
                '' if not miss else '，缺：' + '、'.join(miss[:3]) + ('…' if len(miss) > 3 else ''), extra)
        else:
            ok = n >= 1
            detail = '清单 %d 行' % n
        res.append(dict(id=p['id'], family=p['family'], ok=ok, rows=n, detail=detail))
        if verbose:
            print('%s %s %s' % (p['id'], 'PASS' if ok else 'FAIL', detail))
    return res, abox_triples


# CQ 条数不是硬编码常量：5 个固定族 + **每个故障模式一条** CQ-F，随契约增长。
# 设计文档里的 14/12/10/8/4 是**族配比**（48 条固定族），不是总数；
# 早期实现把「62」写死在断言与报告头里，故障模式从 14 增到 23 后就成了假失败。
CQ_FIXED_FAMILIES = {'CQ-T': 14, 'CQ-X': 12, 'CQ-C': 10, 'CQ-D': 8, 'CQ-A': 4}


def expected_count(c):
    """期望 CQ 条数 = 固定族之和 + 每故障模式一条 CQ-F（唯一真源：契约 fault_modes）。"""
    return sum(CQ_FIXED_FAMILIES.values()) + len(c['fault_modes'])


def main():
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join('tmp', '_cq_probe_report.txt'))
    ap.add_argument('--fixture', action='store_true', help='用契约完备的合成 ABox 再跑一遍')
    args = ap.parse_args()
    c = load_contract()
    probes = build_probes(c)
    _exp = expected_count(c)
    assert len(probes) == _exp, (
        'CQ 条数必须为 %d（%d 条固定族 + %d 个故障模式各一条 CQ-F），实际 %d'
        % (_exp, sum(CQ_FIXED_FAMILIES.values()), len(c['fault_modes']), len(probes)))
    fam = {}
    for p in probes:
        fam[p['family']] = fam.get(p['family'], 0) + 1
    _out_path = os.path.join(ROOT, args.out)
    # ★ 全新克隆里 `tmp/` 不存在（它不入库）：写报告前先建目录，
    #   否则附录里的复算命令第一步就 FileNotFoundError。
    os.makedirs(os.path.dirname(os.path.abspath(_out_path)) or ROOT, exist_ok=True)
    out = io.open(_out_path, 'w', encoding='utf-8', newline='\n')
    out.write('=== %d 条 CQ 探针执行报告 ===\n' % len(probes))
    out.write('族分布: %s（合计 %d）\n' % (', '.join('%s=%d' % kv for kv in sorted(fam.items())), len(probes)))
    out.write('真机夹具快照: ccf-20260928T155516Z-937b79'
              '（scheme2-onto\\tests\\fixtures\\abox-live5.ttl；CONSTRUCT 全图导出，随快照冻结，非活数据）\n')
    for _f in ABOX_DEFAULT:
        _fp = os.path.join(ROOT, _f)
        out.write('  ABox 文件: %s（%d B，sha256 %s）\n'
                  % (_f, os.path.getsize(_fp),
                     hashlib.sha256(open(_fp, 'rb').read()).hexdigest().upper()))
    res1, t1 = run(probes, ABOX_DEFAULT)
    npass = sum(1 for r in res1 if r['ok'])
    out.write('\n--- ABox = %s（%d 三元组）---\n' % (', '.join(ABOX_DEFAULT), t1))
    for r in res1:
        out.write('%s %s  %s\n' % (r['id'], 'PASS' if r['ok'] else 'FAIL', r['detail']))
    out.write('小计: PASS %d / %d\n' % (npass, len(res1)))
    ok_fix = None
    n2 = 0
    if args.fixture:
        res2, t2 = run(probes, ABOX_FIXTURE)
        n2 = sum(1 for r in res2 if r['ok'])
        ok_fix = (n2 == len(res2))
        out.write('\n--- ABox = 契约完备合成口径（%d 三元组；增量：%s）---\n'
                  % (t2, '、'.join(os.path.basename(x) for x in ABOX_FIXTURE)))
        for r in res2:
            out.write('%s %s  %s\n' % (r['id'], 'PASS' if r['ok'] else 'FAIL', r['detail']))
        out.write('小计: PASS %d / %d\n' % (n2, len(res2)))
    # 退出码口径（与报告内 EXIT 行一致，不再恒 0 掩蔽失败）：
    #   --fixture ⇒ 以夹具口径为准（T5「全部 CQ 全返回期望值」的验收口径）
    #   真机口径 ⇒ 2 条已登记投影缺口（CQ-T-13 / CQ-X-11，见 D-1 / D-2）使 npass=60 ⇒ 退出码 1
    ok_live = (npass == len(res1))
    code = (0 if ok_fix else 1) if args.fixture else (0 if ok_live else 1)
    out.write('\nEXIT=%d\n' % code)
    out.close()
    if args.fixture:
        print('CQ 探针: 真机 ABox %d/%d PASS；契约完备夹具 %d/%d PASS；报告 %s'
              % (npass, len(res1), n2, len(res2), args.out))
    else:
        print('CQ 探针: 真机 ABox %d/%d PASS（已登记缺口内的失败不算回归）；报告 %s'
              % (npass, len(res1), args.out))
    return code


if __name__ == '__main__':
    sys.exit(main())
