#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""由 SHACL 形状文件反推生成 **合成 ABox 夹具**（正对照 + 负对照）。

## 这个脚本读什么
* `scheme2-onto/shapes/shacl-constraints.ttl` —— `ccs:*Shape` 的全部 `sh:property` 约束
  （`sh:minCount` / `sh:in` / `sh:datatype` / `sh:class` / `sh:or`）。
* `shared/mapping/type-map.yaml` —— 仅用于给每个类取 `layer`（用于填 `cc:ccfLayer`）。
* **不读真机 ABox、不读任何快照 id、不连图库**。本夹具是**契约派生**的合成数据：
  它不是真机投影，也不冒充真机 ABox；它的价值在于「形状可满足性」的**真对照**
  （正件必须 conforms，负件必须恰好报出注入的 N 个违例），与真机数据正交。

## 这个脚本写什么（默认路径）
* `<out-dir>/cc_shacl_ok.ttl` —— 正对照夹具。由形状反推构造，满足全部 NodeShape 的必填约束。
  这是 `scheme2-onto/tests/cq_probes.py` 的 `ABOX_FIXTURE` 与 `test_integrity.py` 的
  `ABOX_FULL` 的组成件之一，因此**字节必须稳定**：同一份形状 + 同一份 type-map ⇒ 同一份字节。
* `<aux-dir>/cc_shacl_bad.ttl` —— 负对照夹具（在正件上注入 D1..D8 共 8 个可控缺陷，
  期望违例数 = 缺陷数）。**它不属于 tests/fixtures/**（fixtures 只放被测试直接引用的件），
  故默认写进 `tmp/`，可用 `--aux-dir` 改。
* `<aux-dir>/_shacl_fixture_defects.txt` —— 上面两个件的三元组数、必填关系链、缺陷清单。
  报告内只出现**纯文件名**、不出现路径 ⇒ 与本脚本的 `--out-dir`/`--aux-dir` 选择无关，
  同一份形状 + 同一份 type-map 在任何输出目录下都产出**逐字节相同**的报告。
  （历史 `tmp/` 副本里带 "tmp" + 反斜杠 的路径前缀，属旧版写法；本迁移版刻意去掉了该前缀。）

## 什么条件下必须重跑
1. `scheme2-onto/shapes/shacl-constraints.ttl` 变动（增删形状、改 `sh:minCount`/`sh:in`/`sh:or`）；
2. `shared/mapping/type-map.yaml` 的 `types.*.layer` 变动，或类型清单增删；
3. 校验发现 `--check` 报 MISMATCH（说明盘上夹具与形状不再同源）；
4. `sh:in` 取值域被收敛或改写（例：M2.27 把 `ccs:ContainerShape` 的 `ccm:status` 从 7 值收敛为采集面真实 4 值
   `Running`/`Waiting`/`Terminated`/`Unknown`）—— 正件的 `ccm:status` 取的是 `sh:in[0]`，首值一变，夹具字节即变。

## 模式
* 默认（`--write`）：重新生成并写盘。
* `--check`：**不写盘**，把重新生成的结果与盘上文件逐字节比对，打印逐件 sha256 与判定，
  末行 `RESULT: PASS` / `RESULT: FAIL`（fail-closed，退出码 1）。用于「夹具是否可再生成」的门禁。

用法：
    python scheme2-onto/tests/tools/gen_shacl_fixtures.py --check
    python scheme2-onto/tests/tools/gen_shacl_fixtures.py --write
"""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys

import rdflib
import yaml
from rdflib import Graph, Literal, Namespace
from rdflib.collection import Collection

HERE = os.path.dirname(os.path.abspath(__file__))      # …/scheme2-onto/tests/tools
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # 仓库根（上溯 3 层）
SHAPES = os.path.join(ROOT, 'scheme2-onto', 'shapes', 'shacl-constraints.ttl')
TYPEMAP = os.path.join(ROOT, 'shared', 'mapping', 'type-map.yaml')
DEFAULT_OUT_DIR = os.path.join(ROOT, 'scheme2-onto', 'tests', 'fixtures')
DEFAULT_AUX_DIR = os.path.join(ROOT, 'tmp')

CC = Namespace('https://credit-card-ops.ai/onto/topology#')
CCM = Namespace('https://credit-card-ops.ai/onto/metric#')
CCS = Namespace('https://credit-card-ops.ai/onto/shapes#')
INST = Namespace('https://credit-card-ops.ai/onto/inst/')
SH = Namespace('http://www.w3.org/ns/shacl#')
XSD = Namespace('http://www.w3.org/2001/XMLSchema#')

VAL = {str(XSD.string): 'v', str(XSD.integer): 1, str(XSD.decimal): 1.0, str(XSD.boolean): True,
       str(XSD.float): 1.0, str(XSD.double): 1.0, str(XSD.dateTime): '2026-09-28T13:48:33Z'}


def sha(b):
    return hashlib.sha256(b).hexdigest().upper()


def node(cls):
    return INST['ok-' + cls]


def target_class(shapes, shape):
    tc = shapes.value(shape, SH.targetClass)
    return str(tc).rsplit('#', 1)[-1] if tc else None


def build_positive(shapes, layer):
    """按形状反推构造正对照图，返回 (graph, made, chain)。"""
    g = Graph()
    g.bind('cc', CC)
    g.bind('ccm', CCM)
    g.bind('inst', INST)
    g.bind('xsd', XSD)

    shapes_by_class = {}
    for shape in shapes.subjects(rdflib.RDF.type, SH.NodeShape):
        shapes_by_class[target_class(shapes, shape)] = shape

    made = set()
    chain = []

    def build(cls, stack=()):
        if cls in made:
            return node(cls)
        made.add(cls)
        me = node(cls)
        g.add((me, rdflib.RDF.type, CC[cls]))
        g.add((me, CC.ccfId, Literal('ok-' + cls, datatype=XSD.string)))
        g.add((me, CC.ccfType, Literal(cls, datatype=XSD.string)))
        g.add((me, CC.ccfSnapshotId, Literal('fixture-shacl', datatype=XSD.string)))
        g.add((me, CC.ccfLayer, Literal(layer.get(cls) or 'App')))
        shape = shapes_by_class.get(cls)
        if shape is None:
            return me
        for ps in shapes.objects(shape, SH.property):
            path = shapes.value(ps, SH.path)
            mn = shapes.value(ps, SH.minCount)
            en = shapes.value(ps, SH['in'])
            dt = shapes.value(ps, SH.datatype)
            cl = shapes.value(ps, SH['class'])
            if path is None:
                continue
            if mn is not None and int(mn) >= 1:
                if dt is not None:
                    g.add((me, path, Literal(VAL.get(str(dt), 'v'), datatype=dt)))
                elif cl is not None:
                    tgt = str(cl).rsplit('#', 1)[-1]
                    if tgt not in stack:
                        g.add((me, path, build(tgt, stack + (cls,))))
                        chain.append('%s -%s-> %s' % (cls, str(path).rsplit('#', 1)[-1], tgt))
            elif en is not None:
                # 正件取 sh:in 的**首值**（sh:in[0]）：这就是 cc_shacl_ok.ttl 里 ccm:status 的来源。
                # M2.27 起该 sh:in 只含采集面真实 4 值，首值 = "Running"（与真机 ABox 一致）。
                vals = [str(v) for v in Collection(shapes, en)]
                g.add((me, path, Literal(vals[0])))
            elif cl is not None and path is not None:
                tgt = str(cl).rsplit('#', 1)[-1]
                if tgt not in stack and tgt not in made and path in (CC.hosts, CC.hostsContainer):
                    pass
        # sh:or：Host 用 cc:hosts 满足
        ors = shapes.value(shape, SH['or'])
        if ors is not None:
            first = list(Collection(shapes, ors))[0]
            p = shapes.value(first, SH.path)
            c = shapes.value(first, SH['class'])
            tgt = str(c).rsplit('#', 1)[-1]
            if not list(g.objects(me, p)):
                g.add((me, p, build(tgt, stack + (cls,))))
                chain.append('%s -%s-> %s（sh:or 分支1）' % (cls, str(p).rsplit('#', 1)[-1], tgt))
        return me

    # 先按依赖顺序建图（CreditCardApp 为根）；其余类按**类名字典序**补齐，
    # 使构造顺序与 rdflib 的集合迭代序（随 PYTHONHASHSEED 变）解耦。
    build('CreditCardApp')
    for c in sorted([x for x in shapes_by_class if x], key=str):
        if c not in made and c != 'ConnectionPool':
            build(c)
    return g, made, chain


def build_negative(g):
    """在正件上注入 D1..D8，返回 (bad graph, defects list)。"""
    bad = Graph()
    for t in g:
        bad.add(t)
    defects = []

    def inj(desc, fn):
        fn()
        defects.append(desc)

    def d_table_schema():
        bad.remove((node('Table'), CC.schemaName, None))

    def d_container_status():
        for o in list(bad.objects(node('Container'), CCM.status)):
            bad.remove((node('Container'), CCM.status, o))
        bad.add((node('Container'), CCM.status, Literal('Paused')))

    def clone(src, newid):
        dst = INST[newid]
        for p, o in g.predicate_objects(src):
            bad.add((dst, p, o))
        for o in list(bad.objects(dst, CC.containerId)):
            bad.remove((dst, CC.containerId, o))
        bad.add((dst, CC.containerId, Literal(newid, datatype=XSD.string)))

    def d_two_runsin():
        clone(node('Container'), 'ok-Container2')
        bad.add((node('AppInstance'), CC.runsIn, INST['ok-Container2']))

    def d_bogus_prop():
        bad.add((node('Host'), CC.bogusAttribute, Literal('x')))

    def d_session_no_query():
        for o in list(bad.objects(node('Session'), CC.runsQuery)):
            bad.remove((node('Session'), CC.runsQuery, o))

    def d_pool_no_session():
        for o in list(bad.objects(node('DatabaseSessionPool'), CC.hasSession)):
            bad.remove((node('DatabaseSessionPool'), CC.hasSession, o))

    def d_query_no_table():
        for o in list(bad.objects(node('Query'), CC.accesses)):
            bad.remove((node('Query'), CC.accesses, o))

    def d_host_neither():
        bad.add((node('Host-extra'), rdflib.RDF.type, CC.Host))
        bad.add((node('Host-extra'), CC.hostname, Literal('host-extra', datatype=XSD.string)))
        for k, v in ((CC.ccfId, 'ok-Host-extra'), (CC.ccfType, 'Host'),
                     (CC.ccfSnapshotId, 'fixture-shacl'), (CC.ccfLayer, 'Runtime')):
            bad.add((node('Host-extra'), k, Literal(v)))

    inj('D1 Table 缺键属性 cc:schemaName（minCount 1）', d_table_schema)
    inj('D2 Container ccm:status="Paused" 违反契约枚举', d_container_status)
    inj('D3 AppInstance 有 2 条 cc:runsIn（maxCount 1）', d_two_runsin)
    inj('D4 Host 带契约外属性 cc:bogusAttribute（sh:closed）', d_bogus_prop)
    inj('D5 Session 缺 cc:runsQuery（遍历可达性 minCount 1）', d_session_no_query)
    inj('D6 DatabaseSessionPool 缺 cc:hasSession（minCount 1）', d_pool_no_session)
    inj('D7 Query 缺 cc:accesses（minCount 1）', d_query_no_table)
    inj('D8 新增 Host-extra 既无 cc:hosts 又无 cc:hostsContainer（sh:or）', d_host_neither)
    return bad, defects


def generate(out_dir, aux_dir):
    """生成三件产物，返回 {相对名: bytes}（不落盘）。"""
    shapes = Graph()
    shapes.parse(SHAPES, format='turtle')
    tm = yaml.safe_load(io.open(TYPEMAP, encoding='utf-8'))
    layer = {c: s.get('layer') for c, s in tm['types'].items()}

    g, made, chain = build_positive(shapes, layer)
    ok = g.serialize(format='turtle')
    bad, defects = build_negative(g)
    # 【可复现性】rdflib 的 Turtle 序列化器按「命名空间首次被遇到的顺序」自动分配 ns1/ns2/…
    #   前缀标签，而「遇到顺序」来自图迭代序 ⇒ 同一份图偶尔会把 topology# 与 metric# 的标签
    #   对调（实测 20 次里出现 1 次），使负件字节抖动。这里**显式绑定**负件的两个命名空间，
    #   钉死标签映射；只影响序列化外观，不增删任何三元组。
    #   正件 cc_shacl_ok.ttl 是**冻结夹具**（字节不得变），故刻意不动其自动分配路径 ——
    #   它的插入序完全由 build_positive 决定，实测跨 6 个 hash seed 字节稳定。
    bad.bind('ns1', CC)
    bad.bind('ns2', CCM)
    txt = bad.serialize(format='turtle')
    rep = []
    rep.append('正夹具 cc_shacl_ok.ttl：%d 三元组，实例类 %d 个（由 ccs:*Shape 的 minCount/in/or 反推构造）\n'
               % (len(g), len(made)))
    rep.append('  必填关系链：\n')
    for c in chain:
        rep.append('    %s\n' % c)
    rep.append('\n负夹具 cc_shacl_bad.ttl：%d 三元组，注入缺陷 %d 个，期望违例数 = %d（一缺陷一违例）\n'
               % (len(bad), len(defects), len(defects)))
    for d in defects:
        rep.append('  %s\n' % d)

    out = {}
    for name, text in (('cc_shacl_ok.ttl', ok), ('cc_shacl_bad.ttl', txt),
                       ('_shacl_fixture_defects.txt', ''.join(rep))):
        b = text.encode('utf-8')
        assert b[:3] != b'\xef\xbb\xbf', '%s 不应带 BOM' % name
        assert b'\r' not in b, '%s 应为 LF' % name
        out[name] = b
    return out, {'ok_triples': len(g), 'bad_triples': len(bad),
                 'classes': len(made), 'defects': len(defects)}


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description='由 SHACL 形状反推生成合成 ABox 夹具')
    ap.add_argument('--out-dir', default=DEFAULT_OUT_DIR, help='正对照夹具输出目录（默认 tests/fixtures）')
    ap.add_argument('--aux-dir', default=DEFAULT_AUX_DIR, help='负对照与报告输出目录（默认 tmp）')
    ap.add_argument('--check', action='store_true', help='不写盘，只比对盘上件与再生成件是否逐字节一致')
    ap.add_argument('--write', action='store_true', help='写盘（默认动作）')
    args = ap.parse_args()

    gen, stat = generate(args.out_dir, args.aux_dir)
    print('读入形状 : %s' % os.path.relpath(SHAPES, ROOT))
    print('读入契约 : %s' % os.path.relpath(TYPEMAP, ROOT))
    print('生成统计 : 正件 %d 三元组 / 负件 %d 三元组 / 实例类 %d / 缺陷 %d'
          % (stat['ok_triples'], stat['bad_triples'], stat['classes'], stat['defects']))
    print()

    targets = {'cc_shacl_ok.ttl': args.out_dir, 'cc_shacl_bad.ttl': args.aux_dir,
               '_shacl_fixture_defects.txt': args.aux_dir}
    ok_all = True
    print('%-32s %-9s %-12s %s' % ('产物', '模式', 'bytes', 'sha256(再生成)'))
    for name in ('cc_shacl_ok.ttl', 'cc_shacl_bad.ttl', '_shacl_fixture_defects.txt'):
        b = gen[name]
        path = os.path.join(targets[name], name)
        rel = os.path.relpath(path, ROOT)
        if args.check:
            if not os.path.exists(path):
                print('%-32s %-9s %-12d %s  [MISSING]' % (rel, 'CHECK', len(b), sha(b)))
                ok_all = False
                continue
            disk = open(path, 'rb').read()
            same = (disk == b)
            ok_all &= same
            print('%-32s %-9s %-12d %s  %s' % (rel, 'CHECK', len(b), sha(b),
                                               'OK' if same else '★MISMATCH(盘上 %d B %s)' % (len(disk), sha(disk))))
        else:
            os.makedirs(targets[name], exist_ok=True)
            open(path, 'wb').write(b)
            print('%-32s %-9s %-12d %s' % (rel, 'WRITE', len(b), sha(b)))
    print()
    if args.check:
        print('RESULT: %s' % ('PASS' if ok_all else 'FAIL'))
        return 0 if ok_all else 1
    print('RESULT: PASS（已写盘）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
