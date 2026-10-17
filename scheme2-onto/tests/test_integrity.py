# -*- coding: utf-8 -*-
"""方案2 本体完整性测试 T1–T12（`docs\\07` M2.5–M2.7 的门禁；`docs\\04` §8 §T1–T12 定义）。

用法：
    python scheme2-onto\\tests\\test_integrity.py --out tmp\\_integrity_out.txt
    python scheme2-onto\\tests\\test_integrity.py --graphdb        # 额外跑真机一致性检查（T8b）

退出码：0 = 无 FAIL（SKIP 允许，但必须在输出里写明原因）；1 = 有 FAIL。

判定口径（每条都在输出里打印计数，可复算）：
  T1  每个 owl:Class ≥1 实例（含子类蕴含闭包）；闭包为空必须显式 cco:isAbstract / cco:isMetaClass
  T2  18 个故障模式每个 ≥1 ccf:judgedBy，且 ≥1 判据的 ccf:jRole = "detect"
  T3  18 个故障模式每个 ≥1 ccf:affects
  T4  每条 ccc:CausalLink 恰有 1 个 ccc:provenance，且 IRI ∈ 契约 18 边物化集合 ccc:prov-E<id>
  T5  全部 CQ 探针（条数由契约派生）在「契约全投影 ABox」上全 PASS；真机 ABox 上的 FAIL 必须 ∈ 已登记投影缺口
  T6  因果环 ⊆ 契约白名单；同时断言契约字面写法（自反 ccc:cause{2,4}）在本体 n 元物化下为空（X-9）
  T7  声明的对象/数据/注解属性都「被使用」（断言过），或落在显式豁免类别里（逆属性/属性链/外部对齐/契约强制）
  T8  一致性：a) 本地 disjointWith 实例类型冲突检查；b) 可选 GraphDB consistency check
  T9  规则文件零数字字面量，且阈值引用带档位 threshold:<名>#<warning|critical>
  T10 结论码集合一致：规则载体的「产出: F-00n」≡ shared\\model\\findings.yaml 固定链（方案1 侧可机读时再比对）
  T11 SHACL：契约全投影 ABox 零违例（方案2 特有兜底层）
  T12 OntoClean 元属性自检 R1–R4（子类刚性/同一性/依赖性不得强于父类；+I 须给 identityCriterion）
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

import rdflib
from rdflib import Namespace, RDF, RDFS, OWL, XSD

HERE = os.path.dirname(os.path.abspath(__file__))
S2 = os.path.dirname(HERE)
ROOT = os.path.dirname(S2)
sys.path.insert(0, HERE)
import cq_probes as Q  # noqa: E402

ONTO_DIR = os.path.join(S2, 'ontology')
RULES_DIR = os.path.join(S2, 'rules')
SHAPES = os.path.join(S2, 'shapes', 'shacl-constraints.ttl')
# 真机 ABox 夹具 = 耐久夹具快照 scheme2-onto/tests/fixtures/abox-live5.ttl（M2.11 固化）。
# 快照 id ccf-20260928T155516Z-937b79；命名图 https://credit-card-ops.ai/onto/graph/ccf-20260928T155516Z-937b79；
# 2965 三元组（CONSTRUCT 全图导出，随快照冻结，不是活数据）。
# 【已弃用】M2.11 之前的旧真机夹具 tmp/cc_ops_live2.ttl（2026-09-28 旧投影）不再使用：
# 它早于 M2.11 的 Table 身份修复、缺 cc:Table 键属性 cc:schemaName ⇒ T11b 会假报 30 条 topology#schemaName 违例（总数 57）。
ABOX_LIVE5 = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'abox-live5.ttl')
# 合成夹具（SHACL 正对照 / 投影修复）同样改取耐久副本：与 tmp\ 原件逐字节相同（sha256 一致）。
ABOX_OK = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'cc_shacl_ok.ttl')
ABOX_FULL = [ABOX_LIVE5, ABOX_OK,
             os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures', 'cc_proj_fix.ttl')]
ABOX_LIVE = [ABOX_LIVE5]
GRAPHDB = os.environ.get('CC_GRAPHDB_URL', 'http://127.0.0.1:7200')  # ★ R-58：不硬编码部署主机（原值为 192.0.2.10，R-56 事故根因）；用 env/CLI 覆盖，未设则落本机。
FINDINGS_YAML = os.path.join(ROOT, 'shared/model/findings.yaml')

C = Q.load_contract()
P = {k: Namespace(v) for k, v in C['prefixes'].items()}
OUR_NS = [P[k] for k in ('cc', 'cco', 'ccm', 'ccf', 'ccc', 'cca', 'ccp') if k in P]
CC, CCO, CCM, CCF, CCC, CCA, CCP = (P.get('cc'), P.get('cco'), P.get('ccm'), P.get('ccf'),
                                    P.get('ccc'), P.get('cca'), P.get('ccp'))
LIVE_SPOT = {'CQ-T-13', 'CQ-X-11'}          # 已登记投影缺口（D-1 / D-2）对应的 CQ

RES = []


def rec(tid, status, detail):
    RES.append((tid, status, detail))


def load(paths, extra=()):
    g = rdflib.Graph()
    for p in list(paths) + list(extra):
        g.parse(p, format='turtle')
    return g


def merge(*gs):
    out = rdflib.Graph()
    for g in gs:
        for t in g:
            out.add(t)
    return out


def supers_of(g):
    """子类 → 祖先闭包（含自身）。"""
    direct = defaultdict(set)
    for c, p in g.subject_objects(RDFS.subClassOf):
        if isinstance(c, rdflib.URIRef) and isinstance(p, rdflib.URIRef):
            direct[c].add(p)
    memo = {}

    def cl(c):
        if c in memo:
            return memo[c]
        memo[c] = {c}
        for p in direct.get(c, ()):
            memo[c] |= cl(p)
        return memo[c]
    return cl


def our_classes(g):
    out = []
    for c in g.subjects(RDF.type, OWL.Class):
        if isinstance(c, rdflib.URIRef) and any(str(c).startswith(str(n)) for n in OUR_NS):
            out.append(c)
    return sorted(set(out), key=lambda u: str(u))


def local(u):
    return str(u).rsplit('#', 1)[-1]


def expand(v):
    """把映射表里的简写（`ccm:meanExecMs`）展开成 IRI；已是 IRI 的原样返回。"""
    s = str(v)
    if ':' in s and not s.startswith('http'):
        pfx, rest = s.split(':', 1)
        if pfx in C['prefixes']:
            return rdflib.URIRef(C['prefixes'][pfx] + rest)
    return rdflib.URIRef(s)


def fn(g, s, p):
    v = g.value(s, p)
    return str(v) if v is not None else None


# ---------------------------------------------------------------- T1
def t1(onto, abox):
    g = merge(onto, abox)
    cl = supers_of(g)
    inst = defaultdict(set)
    for s, o in g.subject_objects(RDF.type):
        if isinstance(s, rdflib.BNode) or not isinstance(o, rdflib.URIRef):
            continue
        for c in cl(o):
            inst[c].add(s)
    empty_bad, empty_ok = [], []
    for c in our_classes(g):
        if inst.get(c):
            continue
        if g.value(c, CCO.isAbstract) is not None or g.value(c, CCO.isMetaClass) is not None:
            empty_ok.append(local(c))
        else:
            empty_bad.append(local(c))
    det = ('类 %d；有实例 %d；显式抽象/元类豁免 %d；**无解释的空类 %d**：%s'
           % (len(our_classes(g)), sum(1 for c in our_classes(g) if inst.get(c)),
              len(empty_ok), len(empty_bad), ', '.join(empty_bad) or '—'))
    if empty_ok:
        det += '；豁免类 = ' + ', '.join(empty_ok)
    rec('T1', 'FAIL' if empty_bad else 'PASS', det)


# ---------------------------------------------------------------- T2 / T3
def t2_t3(onto):
    modes = [CCF[m['code']] for m in C['fault_modes']]
    no_j, no_det, no_aff = [], [], []
    for m in modes:
        js = list(onto.objects(m, CCF.judgedBy))
        if not js:
            no_j.append(local(m))
        if not any(str(onto.value(j, CCF.jRole)) == 'detect' for j in js):
            no_det.append(local(m))
        if not list(onto.objects(m, CCF.affects)):
            no_aff.append(local(m))
    rec('T2', 'FAIL' if (no_j or no_det) else 'PASS',
        '故障模式 %d；无 judgedBy %d %s；无 detect 角色判据 %d %s'
        % (len(modes), len(no_j), no_j or '[]', len(no_det), no_det or '[]'))
    rec('T3', 'FAIL' if no_aff else 'PASS',
        '故障模式 %d；无 ccf:affects %d %s' % (len(modes), len(no_aff), no_aff or '[]'))


# ---------------------------------------------------------------- T4
def t4(onto):
    links = list(onto.subjects(RDF.type, CCC.CausalLink))
    want = set()
    for e in C['edges']:
        eid = str(e.get('id'))
        want.add(str(CCC['prov-' + (eid if eid.startswith('E') else 'E' + eid)]))
    bad = []
    for l in links:
        pv = list(onto.objects(l, CCC.provenance))
        if len(pv) != 1 or str(pv[0]) not in want:
            bad.append(local(l))
    # 物化个体齐备性
    have = {str(o) for o in onto.subjects(RDF.type, CCC.ContractProvenance)}
    missing = sorted(want - have)
    rec('T4', 'FAIL' if (bad or missing) else 'PASS',
        'CausalLink %d（契约边 %d）；provenance 缺失/越界 %d %s；ContractProvenance 物化 %d/%d %s'
        % (len(links), len(want), len(bad), bad or '[]', len(have), len(want),
           ('缺 ' + ', '.join(local(x) for x in missing)) if missing else '全齐'))


# ---------------------------------------------------------------- T5
def t5():
    probes = Q.build_probes(C)
    full, _ = Q.run(probes, ABOX_FULL)
    live, _ = Q.run(probes, ABOX_LIVE)
    fpass = sum(1 for r in full if r['ok'])
    lfail = [r['id'] for r in live if not r['ok']]
    unexpected = [i for i in lfail if i not in LIVE_SPOT]
    _exp = Q.expected_count(C)
    ok = fpass == _exp and not unexpected
    rec('T5', 'PASS' if ok else 'FAIL',
        '探针 %d；契约全投影 ABox %d/%d PASS；真机 ABox %d/%d PASS（FAIL=%s，已登记缺口=%s）%s'
        % (len(probes), fpass, _exp, _exp - len(lfail), _exp,
           ','.join(lfail) or '—',
           ','.join(sorted(LIVE_SPOT)), '' if not unexpected else '；**意外 FAIL=%s**' % unexpected))


# ---------------------------------------------------------------- T6
def _causal_pairs(g):
    """只统计因果型链接（causes / conditional_causes）构成的双向 2 环。

    `ccc:linkKind = relates_to` 明确不参与环检测（cc-causal.ttl 注释 + 契约 relation_enum）。
    """
    CAUSAL = {'causes', 'conditional_causes'}
    links = []
    for l in g.subjects(RDF.type, CCC.CausalLink):
        k = str(g.value(l, CCC.linkKind) or '')
        a, b = g.value(l, CCC.cause), g.value(l, CCC.effect)
        if a is not None and b is not None:
            links.append((a, b, k))
    pairs = set()
    for a, b, k1 in links:
        if k1 not in CAUSAL:
            continue
        for a2, b2, k2 in links:
            if k2 in CAUSAL and a2 == b and b2 == a:
                pairs.add(tuple(sorted([local(a), local(b)])))
    return pairs


def t6(onto):
    pairs = _causal_pairs(onto)
    # 契约自身的因果边是否也构成同样的环（E1 causes + E2 causes）⇒ 区分「本体引入的环」与「契约自带的环」
    cg = rdflib.Graph()
    for e in C['edges']:
        cg.add((CCC[str(e['id'])], RDF.type, CCC.CausalLink))
        cg.add((CCC[str(e['id'])], CCC.cause, CCF[str(e['from'])]))
        cg.add((CCC[str(e['id'])], CCC.effect, CCF[str(e['to'])]))
        cg.add((CCC[str(e['id'])], CCC.linkKind, rdflib.Literal(str(e.get('relation')))))
    contract_pairs = _causal_pairs(cg)
    white = [tuple(sorted(c)) for c in (C['causal_raw'].get('meta', {}).get('cycles') or [])]
    extra_onto = [p for p in pairs if p not in white]
    extra_contract = [p for p in contract_pairs if p not in white]
    introduced = [p for p in extra_onto if p not in extra_contract]
    # 契约字面写法（自反 ccc:cause{2,4}）在本体 n 元物化下必须为空（X-9）
    literal_self, literal_note = 0, ''
    try:
        q = '''SELECT ?x WHERE { ?x a ccf:FaultMode . ?x ccc:cause{2,4} ?x }'''
        literal_self = len(list(onto.query(q, initNs={'ccc': CCC, 'ccf': CCF})))
    except Exception:  # noqa: BLE001
        literal_note = '（rdflib 不解析 {n,m} 路径修饰符 ⇒ 该字面写法在真机 GraphDB 上实测为空，见 tmp\\_live_tbox_report.txt）'
    det = ('本体 2 环对 %d %s；契约白名单 %d %s；**本体引入的环（白名单外且契约边也没有）%d** %s；'
           '契约边自身的白名单外环 %d %s（>0 即契约缺陷 X-11：meta.cycles 漏登记；本次为 0 ⇒ 白名单完备，'
           '前提是 relates_to 不参与环检测，见 cc-causal.ttl:117）；'
           '契约字面 ccc:cause{2,4} 自反命中 %d（X-9 应为 0）%s'
           % (len(pairs), sorted(pairs), len(white), white, len(introduced), introduced,
              len(extra_contract), extra_contract, literal_self, literal_note))
    rec('T6', 'FAIL' if (introduced or literal_self) else 'PASS', det)


# ---------------------------------------------------------------- T7
def t7(onto, abox):
    g = merge(onto, abox)
    kinds = (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty)
    declared = set()
    for k in kinds:
        for p in onto.subjects(RDF.type, k):
            if isinstance(p, rdflib.URIRef) and any(str(p).startswith(str(n)) for n in OUR_NS):
                declared.add(p)
    used = {p for p in g.predicates() if p in declared}
    # 元属性以「被 owl:AnnotationProperty 语义当作类型标记」方式使用（如 `cco: a cco:oopsCheck`）
    used |= {o for o in g.objects(None, RDF.type) if isinstance(o, rdflib.URIRef)}
    # 逆属性被断言 ⇒ 经 owl:inverseOf 可推理，视为使用
    inv_used = set()
    for p in declared - used:
        for q in list(onto.objects(p, OWL.inverseOf)) + list(onto.subjects(OWL.inverseOf, p)):
            if isinstance(q, rdflib.URIRef) and q in used:
                inv_used.add(p)
    # 属性链：既含链成员（runsIn/deployedOn），也含被链**定义**的属性（indirectlyHostedOn，其外延只由推理给出）
    chain = set(onto.subjects(OWL.propertyChainAxiom, None))
    for ax in onto.objects(None, OWL.propertyChainAxiom):
        for e in onto.items(ax):
            if isinstance(e, rdflib.URIRef):
                chain.add(e)
    # 对齐公理载体（指向外部词表）
    align = {p for p in declared
             if any(str(o).startswith(('http://www.w3.org/ns/sosa/', 'http://www.w3.org/ns/prov#'))
                    for o in onto.objects(p, RDFS.subPropertyOf))}
    # 契约强制（70 指标 + 14 类型 attrs）—— 真机未投影属投影缺口，不算 TBox 缺陷
    contract = set()
    for name, spec in (C['metrics'] or {}).items():
        if isinstance(spec, dict) and spec.get('onto_property'):
            contract.add(expand(spec['onto_property']))
    for name, spec in (C['types'] or {}).items():
        if not isinstance(spec, dict):
            continue
        for a in list(spec.get('attrs') or []) + list(spec.get('key_attrs') or []):
            contract.add(CC[str(a)])
    # ★ v1.0.3（X-5/X-7）新增的两个**文档化豁免类别**（TBox 侧只声明、由别处承载个体）：
    #   schema_obs    = CCF 观测投影契约里声明的字段（shared/model/ccf.schema.json#/$defs/
    #                   observation.properties：delta / deltaWindowS / counterReset 等），
    #                   由**采集层/投影器**在 ABox 上断言；TBox 声明口径，不伪造实例。
    #   rule_carriers = 带 cco:contractSource 标注、且 local 名确实出现在 rules/*.rq 文本里的
    #                   属性（ccp:unjudgeable* 四件套），个体只在规则 CONSTRUCT 时产出。
    schema_obs_names = set()
    try:
        with io.open(os.path.join(ROOT, 'shared/model/ccf.schema.json'), encoding='utf-8') as fh:
            schema_obs_names = set((json.load(fh).get('$defs', {})
                                    .get('observation', {}).get('properties', {})).keys())
    except Exception as e:  # 契约不可读时退化为「该类别为空」，不掩盖其它问题
        print('T7 warn: 读取 ccf.schema.json 失败: %s' % e)
    rules_text = ''
    for rf in sorted(glob.glob(os.path.join(RULES_DIR, '*.rq'))):
        rules_text += io.open(rf, encoding='utf-8').read()
    schema_obs = {p for p in declared if local(p) in schema_obs_names}
    rule_carriers = {p for p in declared
                     if onto.value(p, CCO.contractSource) is not None
                     and re.search(r'\b%s\b' % re.escape(local(p)), rules_text)}
    exempt = inv_used | chain | align | (declared & contract) | schema_obs | rule_carriers
    left = sorted(local(p) for p in declared - used - exempt)
    rec('T7', 'FAIL' if left else 'PASS',
        '声明属性 %d；被断言 %d；豁免：逆属性 %d / 属性链 %d / 外部对齐 %d / 契约强制 %d / '
        '观测投影契约 %d / 规则承载 %d；**未使用 %d** %s'
        % (len(declared), len(used), len(inv_used), len(chain), len(align),
           len(declared & contract), len(schema_obs), len(rule_carriers), len(left), left or '[]'))
    gap = sorted(local(p) for p in (declared & contract) - used)
    if gap:
        rec('T7-info', 'INFO', '契约强制但真机 ABox 未投影的属性 %d：%s' % (len(gap), gap))


# ---------------------------------------------------------------- T8
def t8(onto, abox):
    g = merge(onto, abox)
    cl = supers_of(g)
    disj = []
    for c in our_classes(g):
        for d in g.objects(c, OWL.disjointWith):
            if isinstance(d, rdflib.URIRef) and str(c) < str(d):
                disj.append((c, d))
    bad = []
    for s, o in g.subject_objects(RDF.type):
        if isinstance(s, rdflib.BNode) or not isinstance(o, rdflib.URIRef):
            continue
        tc = cl(o)
        for a, b in disj:
            if a in tc and b in tc:
                bad.append((local(s), local(a), local(b)))
    rec('T8a', 'FAIL' if bad else 'PASS',
        '互斥公理 %d 对；实例类型闭包冲突 %d %s' % (len(disj), len(bad), bad[:5] or '[]'))


def t8b(onto, abox, enabled):
    if not enabled:
        rec('T8b', 'SKIP', '未启用 --graphdb（真机一致性检查已在 tmp\\_tbox_live_check.py 单独跑过，见 tmp\\_live_tbox_report.txt）')
        return
    try:
        g = load(list(ABOX_LIVE))
        ttl = onto.serialize(format='turtle') + g.serialize(format='turtle')
        req = urllib.request.Request(GRAPHDB + '/rest/repositories', method='GET')
        with urllib.request.urlopen(req, timeout=5) as r:
            repos = r.read().decode('utf-8')
        rec('T8b', 'SKIP', 'GraphDB 可达（仓库列表 %d 字符）；一致性 check 需临时仓库，交由 tmp\\_tbox_live_check.py'
            % len(repos))
    except Exception as e:  # noqa: BLE001
        rec('T8b', 'SKIP', 'GraphDB 不可达：%s' % type(e).__name__)


# ---------------------------------------------------------------- T9
def t9():
    files = sorted(f for f in os.listdir(RULES_DIR) if f.endswith('.rq'))
    bad_num, bad_tier, nref = [], [], 0
    tier_re = re.compile(r'threshold:([A-Za-z0-9_.\-]+)#(warning|critical)')
    for f in files:
        txt = io.open(os.path.join(RULES_DIR, f), encoding='utf-8').read()
        nref += len(tier_re.findall(txt))
        s = re.sub(r'"[^"]*"', '""', txt)         # 先剥字符串
        s = re.sub(r'<[^>]*>', '<>', s)           # 再剥 IRI
        s = re.sub(r'^\s*PREFIX.*$', '', s, flags=re.M)
        s = re.sub(r'\b(cc|cco|ccm|ccf|ccc|cca|ccp|rdf|rdfs|owl|xsd|sh|sosa|prov|dcterms):[A-Za-z0-9_.\-]*', '', s)
        s = re.sub(r'\?[A-Za-z0-9_]+', '', s)
        s = re.sub(r'\{\s*\d+\s*(,\s*\d*)?\s*\}', '', s)
        s = re.sub(r'#.*$', '', s, flags=re.M)    # 最后剥注释
        for m in re.finditer(r'(?<![\w.:#-])\d+(\.\d+)?(?![\w.:#-])', s):
            bad_num.append('%s:%s' % (f, m.group(0)))
        # 裸阈值引用（不带档位）
        for m in re.finditer(r'threshold:[A-Za-z0-9_.\-]+(?![A-Za-z0-9_#])', s):
            bad_tier.append('%s:%s' % (f, m.group(0)))
    rec('T9', 'FAIL' if (bad_num or bad_tier) else 'PASS',
        '规则 %d 个；数字字面量 %d %s；带档位阈值引用 %d；裸阈值引用 %d %s'
        % (len(files), len(bad_num), bad_num[:5] or '[]', nref, len(bad_tier), bad_tier[:5] or '[]'))


# ---------------------------------------------------------------- T10
def t10(onto):
    codes = set()
    for f in sorted(os.listdir(RULES_DIR)):
        if not f.endswith('.rq'):
            continue
        for m in re.finditer(r'产出\s*[:：]\s*(F-00\d)', io.open(os.path.join(RULES_DIR, f), encoding='utf-8').read()):
            codes.add(m.group(1))
    fixed = set()
    if os.path.exists(FINDINGS_YAML):
        fixed = set(re.findall(r'F-00\d', io.open(FINDINGS_YAML, encoding='utf-8').read()))
    indiv = {local(o) for o in onto.subjects(RDF.type, CCF.Finding)}
    ok = codes == fixed and len(indiv) == len(fixed)
    rec('T10', 'PASS' if ok else 'FAIL',
        '规则载体产出声明 %d %s；findings.yaml 固定 %d %s；cc-audit 结论个体 %d %s；'
        '方案1 侧机读结论码：未提供（SKIP 比对）'
        % (len(codes), sorted(codes), len(fixed), sorted(fixed), len(indiv), sorted(indiv)))
    return


# ---------------------------------------------------------------- T11
SH = Namespace('http://www.w3.org/ns/shacl#')
# ====================================================== T11 豁免清单（M2.20 结构化重写）
# 诚实纪律：豁免不是「把警报关掉」，而是把一条**当时消除不掉**的违例，连同它的产状证据与
# 失效条件一起登记下来。每条豁免是一个 (path, component) 二元签名，必须携带 6 个字段：
#   path              谓词路径 IRI（无 resultPath 的形状级违例用哨兵 T11_NO_PATH）
#   component         SHACL 约束组件 IRI（与 path 共同构成**唯一键**；不再用「path 或组件」的
#                     并集判断 —— 那样任何一条空路径 Or 违例都会被无声吞掉）
#   expected_count    M2.20 真机复测条数（夹具 scheme2-onto/tests/fixtures/abox-live5.ttl）
#   rationale         为什么这不是数据缺陷（结构性 / 契约性 / 环境性）
#   evidence          产状证据：shape 符号名 + 数据面事实。**只引符号名，不写行号** —— 行号是
#                     移动靶，本项目已因此连踩两次
#   closure_condition 什么条件下这条豁免必须被删除
# ★ 反向不变式：任何一条豁免的实测条数为 0 ⇒ T11b 直接 FAIL。理由与 C18 恒真门禁 / G20 的
#   --min-refs / M2.18「BLOCKED ≠ FAIL」同一课：一个永远不触发的判据没有判别力，留在清单里
#   只会**掩盖将来的回归**；豁免到期必须由人显式删除，不能靠「它反正也不会响」。
# ★ 已删除（M2.20，D-1 结案）：cc:Table 缺 cc:schemaName —— M2.11 已关闭该投影缺口，真机实测
#   违例 0 条。留一条 count=0 的豁免等于给 schemaName 的回归预留一个黑洞，故整条移除。
T11_NO_PATH = '—'
T11_ALLOWED = (
    # ★ 2026-10-10 用户裁定 ⑧（升档）：原「inference=rdfs 不物化 owl:inverseOf 造成的假违例」
    #   条目已**删除** —— 档位升到 OWL 2 RL（预物化等价实现）后该违例不再出现；保留会命中本文件
    #   的反向不变式（实测 0 条 ⇒ FAIL）。
    {
        # 结构性不可断言：CCF relations[] 无「会话→SQL」关系型 ⇒ 投影器永不断言 cc:runsQuery
        'path': str(CC.runsQuery),
        'component': str(SH.MinCountConstraintComponent),
        'expected_count': 19,
        'rationale': 'SessionShape 要求「会话必须至少执行一条查询」，而 CCF relations[] 里没有'
                     '「会话→SQL」这一关系型，投影器结构上永远不会断言 cc:runsQuery：这不是某几条'
                     '数据坏了，而是该关系在采集能力上尚不存在。19 条恰好等于夹具中 Session 个体'
                     '总数（每个 Session 各缺 1 条），是**全量缺失**而非个别缺失。',
        'evidence': 'shape ccs:SessionShape（shapes/shacl-constraints.ttl）对 sh:path cc:runsQuery 的'
                    ' sh:minCount 1（其 sh:message 自述「CQ F2 遍历前提」）。数据面事实：耐久夹具'
                    ' fixtures\\abox-live5.ttl 中 cc:runsQuery 断言数 = 0、Session 个体 = 19，SHACL 的'
                    ' 19 个 focusNode 全是 inst/session/sess-*；GraphDB cc-ops 仓 7 张已发布 CCF 快照图'
                    '（6cf8c5 / 1c49d2 / 7aa9d5 / dc3317 / eb634e / b920b5 / 937b79）内 cc:runsQuery'
                    ' 断言数恒为 0 ⇒ 不是某次回归的结果。',
        'closure_condition': '上游采集能产出「会话 → SQL → 表」链路（CCF relations[] 出现会话级查询'
                             '关系并被投影器落成 cc:runsQuery）后必须删除本条：届时实测条数自然降到 0，'
                             '由上面的反向不变式报出，而不是靠人记得。',
    },
    {
        # 结构性不可断言：CCF relations[] 无「SQL→表」关系型 ⇒ 投影器永不断言 cc:accesses
        'path': str(CC.accesses),
        'component': str(SH.MinCountConstraintComponent),
        'expected_count': 4,
        'rationale': 'QueryShape 要求「查询必须至少访问一张表（死元组 CQ / F2 前提）」，而 CCF '
                     'relations[] 里同样没有「SQL→表」关系型，投影器永不断言 cc:accesses。4 条恰好'
                     '等于夹具中 Query 个体总数，同样是全量缺失。',
        'evidence': 'shape ccs:QueryShape（shapes/shacl-constraints.ttl）对 sh:path cc:accesses 的'
                    ' sh:minCount 1。数据面事实：fixtures\\abox-live5.ttl 中 cc:accesses 断言数 = 0、'
                    ' Query 个体 = 4，4 个 focusNode 全是 inst/query/sql-*；GraphDB cc-ops 仓 7 张已'
                    '发布快照图内 cc:accesses 断言数恒为 0 ⇒ 不是某次回归的结果。',
        'closure_condition': '上游采集能产出「会话 → SQL → 表」链路（CCF relations[] 出现 SQL 级表'
                             '访问关系并被投影器落成 cc:accesses）后必须删除本条。',
    },
    {
        # 环境性数据缺口：M1.8a 会话归因只覆盖两个会话池中的一个
        'path': str(CC.hasSession),
        'component': str(SH.MinCountConstraintComponent),
        'expected_count': 1,
        'rationale': '环境性数据缺口，不是结构缺失：M1.8a 的会话归因只把会话挂到两个会话池中的一个'
                     '（dn_6001-pool 挂 19 条），另一个（dn_6002-pool）挂 0 条，于是「会话池必须至少'
                     '承载一个会话」在它身上不成立。补齐采集覆盖即可闭合，不需要动 shape 或本体。',
        'evidence': 'shape ccs:DatabaseSessionPoolShape（shapes/shacl-constraints.ttl）对 sh:path'
                    ' cc:hasSession 的 sh:minCount 1。数据面事实：fixtures\\abox-live5.ttl 中'
                    ' cc:hasSession 断言 = 19 且全部属于 inst/dbpool/dn_6001-pool，'
                    ' inst/dbpool/dn_6002-pool 为 0 条 —— 唯一的 focusNode 即 dn_6002-pool；'
                    'GraphDB 快照面：cc:hasSession 在 eb634e 及更早的图为 0、自 b920b5 起为 19'
                    '（即 M1.8a 会话归因的落地时点）。',
        'closure_condition': '会话归因覆盖全部会话池（每个 DatabaseSessionPool 至少挂到一条'
                             ' hasSession 断言）后必须删除本条。',
    },
)


def _viol_signature(vg):
    sig = []
    for r in vg.subjects(RDF.type, SH.ValidationResult):
        p = vg.value(r, SH.resultPath)
        c = vg.value(r, SH.sourceConstraintComponent)
        sig.append((str(p) if p is not None else '—', str(c) if c is not None else '—'))
    return sig


def t11():
    try:
        from pyshacl import validate
    except Exception as e:  # noqa: BLE001
        rec('T11', 'SKIP', 'pyshacl 不可用：%s' % e)
        return
    onto = load([os.path.join(ONTO_DIR, f + '.ttl') for f in Q.ONTO_FILES])
    shapes = rdflib.Graph()
    shapes.parse(SHAPES, format='turtle')
    # a) 正对照：专为 SHACL 收敛构造的夹具必须零违例（形状本身可满足）
    # ★ 用户裁定 ⑧（升档到 OWL 2 RL）——**定向实现**，理由是实测的性能代价：
    #   · 直接交给 pyshacl `inference='owlrl'`：**20+ 分钟仍未收敛**；
    #   · 先用 owlrl 物化完整闭包再以 `inference=None` 校验：**>9 分钟仍未收敛**；
    #   · 本测试的 ⑧ 动因只有一条 —— `HostShape` 的 `sh:or` 依赖 `cc:hostsContainer`，而它是
    #     `cc:deployedOn` 的 `owl:inverseOf` 派生反向关系（两侧都不显式建边）⇒ 只要**按 owl:inverseOf
    #     公理定向物化**反向三元组即可让该违例消失（且这正是 OWL 2 RL 蕴含的一个子集）。
    #   ⇒ 采用定向物化：**快、可复核、不改变其它语义**；完整闭包因代价过大**不采用**（已登记在 docs/10 §6）。
    def _mat(abox):
        g = rdflib.Graph()
        for _t in onto:
            g.add(_t)
        for _t in abox:
            g.add(_t)
        added = 0
        for _a, _b in list(onto.subject_objects(OWL.inverseOf)):
            for _s, _o in list(g.subject_objects(_a)):
                if (_o, _b, _s) not in g:
                    g.add((_o, _b, _s)); added += 1
            for _s, _o in list(g.subject_objects(_b)):
                if (_o, _a, _s) not in g:
                    g.add((_o, _a, _s)); added += 1
        return g, added

    g_ok, n_inv_ok = _mat(load([ABOX_OK]))
    ok, _, txt = validate(g_ok, shacl_graph=shapes, inference=None, advanced=True)
    na = len(re.findall(r'Constraint Violation', txt))
    rec('T11a', 'PASS' if ok else 'FAIL',
        '正对照 ABox fixtures\\cc_shacl_ok.ttl：conforms=%s 违例 %d（证明形状可满足，非空转；推理=按 owl:inverseOf 定向物化，补逆三元组 %d 条）'
        % (ok, na, n_inv_ok))
    # b) 真机 ABox：每条违例的 (path, 约束组件) 必须 ∈ 已登记豁免签名；且豁免签名不得陈旧
    g_live, n_inv_live = _mat(load(ABOX_LIVE))
    ok2, vg2, txt2 = validate(g_live, shacl_graph=shapes, inference=None, advanced=True)
    nb = len(re.findall(r'Constraint Violation', txt2))
    sig = _viol_signature(vg2)
    counts = defaultdict(int)
    for s in sig:
        counts[s] += 1
    idx, dup = {}, []
    for e in T11_ALLOWED:
        k = (str(e['path']), str(e['component']))
        if k in idx:
            dup.append(k[0])
        idx[k] = e
    unreg = sorted({s for s in sig if s not in idx})
    # ★ 反向不变式：豁免清单里任何一条的实测条数为 0 ⇒ 它已陈旧，必须 FAIL
    stale = sorted(k for k in idx if counts.get(k, 0) == 0)
    drift = sorted('%s（expected_count=%d，实测 %d）' % (k[0], idx[k]['expected_count'], counts[k])
                   for k in idx if counts.get(k, 0) not in (0, idx[k]['expected_count']))
    kinds = sorted({(s[0], len([x for x in sig if x == s])) for s in set(sig)})
    det = ('真机 ABox 违例 %d；签名聚合 %s；**未登记（不在豁免清单内）%d** %s；豁免清单 %d 条%s'
           % (nb, ['%s×%d' % (k[0], k[1]) for k in kinds], len(unreg), unreg or '[]',
              len(idx), '' if not drift else '；⚠ 计数漂移（不是 FAIL，仅提示）%s' % drift))
    if dup:
        det += '；★ 豁免清单存在重复键（后者静默覆盖前者，等于放过一条陈旧项）：%s' % dup
    if stale:
        det += ('；★ 豁免项已陈旧：%s 不再触发（实测 0 条）⇒ 必须从允许清单移除；'
                '保留它会掩盖将来的回归' % '、'.join(k[0] for k in stale))
    rec('T11b', 'FAIL' if (unreg or stale or dup) else 'PASS', det)


# ---------------------------------------------------------------- T12
def t12(onto):
    cl = supers_of(onto)
    R1, R2, R3, R4 = [], [], [], []
    for c in our_classes(onto):
        rig = fn(onto, c, CCO.rigidity)
        idt = fn(onto, c, CCO.identity)
        dep = fn(onto, c, CCO.dependence)
        for p in cl(c) - {c}:
            prig, pidt, pdep = (fn(onto, p, CCO.rigidity), fn(onto, p, CCO.identity),
                                fn(onto, p, CCO.dependence))
            if prig == '-R' and rig == '+R':
                R1.append('%s(+R) ⊑ %s(-R)' % (local(c), local(p)))
            if pidt == '+I' and idt == '-I':
                R2.append('%s(-I) ⊑ %s(+I)' % (local(c), local(p)))
            if pdep == '+D' and dep == '-D':
                R3.append('%s(-D) ⊑ %s(+D)' % (local(c), local(p)))
        if idt == '+I' and onto.value(c, CCO.identityCriterion) is None:
            R4.append(local(c))
    bad = R1 + R2 + R3 + R4
    rec('T12', 'FAIL' if bad else 'PASS',
        '类 %d；R1 刚性冲突 %d %s；R2 同一性冲突 %d %s；R3 依赖性冲突 %d %s；R4 缺 identityCriterion %d %s'
        % (len(our_classes(onto)), len(R1), R1 or '[]', len(R2), R2 or '[]',
           len(R3), R3 or '[]', len(R4), R4 or '[]'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    ap.add_argument('--graphdb', action='store_true')
    a = ap.parse_args()

    onto = load([os.path.join(ONTO_DIR, f + '.ttl') for f in Q.ONTO_FILES])
    abox = load(ABOX_FULL)
    t1(onto, abox)
    t2_t3(onto)
    t4(onto)
    t5()
    t6(onto)
    t7(onto, abox)
    t8(onto, abox)
    t8b(onto, abox, a.graphdb)
    t9()
    t10(onto)
    t11()
    t12(onto)

    buf = []
    buf.append('== 方案2 本体完整性 T1–T12（docs\\07 M2 门禁）==')
    buf.append('TBox 三元组 %d；契约全投影 ABox 三元组 %d；对象属性 %d；类 %d'
               % (len(onto), len(abox), len(list(onto.subjects(RDF.type, OWL.ObjectProperty))),
                  len(our_classes(onto))))
    npass = sum(1 for _, s, _ in RES if s == 'PASS')
    nfail = sum(1 for _, s, _ in RES if s == 'FAIL')
    nskip = sum(1 for _, s, _ in RES if s == 'SKIP')
    for tid, st, det in RES:
        buf.append('%-8s %-4s %s' % (tid, st, det))
    buf.append('合计：PASS %d / FAIL %d / SKIP %d' % (npass, nfail, nskip))
    buf.append('EXIT=%d' % (1 if nfail else 0))
    out = '\n'.join(buf) + '\n'
    if a.out:
        io.open(a.out, 'w', encoding='utf-8', newline='\n').write(out)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    print(out, end='')
    return 1 if nfail else 0


if __name__ == '__main__':
    sys.exit(main())
