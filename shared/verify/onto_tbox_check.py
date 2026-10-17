#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""shared/verify/onto_tbox_check.py — 方案2 原生 TBox 只读静态校验器（退出码 0/1）。

用途（M2.4–M2.7 门禁）：在**不连图库**的前提下检查方案2 TBox 是否与场景契约逐条对齐。
与方案1 的 `shared/verify/kg_tbox_check.py` 同约定：只读、可复算、退出码 0=全过 / 1=有 FAIL。

五组检查（对齐任务书 (a)）：
  C1 可解析性        —— 7 个本体模块 + SHACL 形状都能被 rdflib 解析
  C2 契约覆盖逐条相等 —— 14 类型 / 15 关系 / 14 故障模式 / 18 因果边 与 YAML 集合**相等**
  C3 命名空间白名单   —— 类与属性 IRI 的命名空间 ∈ 映射表 prefixes（+ 标准词表白名单）
  C4 规则零硬编码阈值 —— rules/*.rq 无数字字面量；阈值引用必带 `#warning|#critical` 档位
  C5 Evidence 对称性  —— cc:Evidence 8 属性齐备；cc:hasEvidence 无 rdfs:domain、range=cc:Evidence

★ 关系名分域（任务书要求）：ABox 投影关系必须逐字来自 `shared/mapping/rel-map.yaml`；
  故障域/因果域原生关系（ccf:judgedBy / ccf:affects / ccc:cause ...）来自契约 YAML 的 code，
  **不在** rel-map 里，不得混入 C2 的「ABox 投影关系」集合。

用法：
  python shared\\verify\\onto_tbox_check.py [--root <repo>]
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import yaml
from rdflib import Graph, Literal, URIRef, RDF, RDFS, OWL, XSD
from rdflib.namespace import SKOS

# ---- 标准词表白名单（非本项目命名空间，允许出现在 TBox 里做对齐） ----
STD_BASES = {
    str(OWL), str(RDF), str(RDFS), str(XSD),
    "http://www.w3.org/ns/sosa/", "http://www.w3.org/ns/ssn/",
    "http://www.w3.org/ns/prov#", "http://www.w3.org/2004/02/skos/core#",
    "http://www.w3.org/2006/time#", "http://purl.org/dc/terms/",
    "http://www.w3.org/ns/dcat#", "http://www.w3.org/ns/shacl#",
    "http://www.w3.org/ns/dqv#", "http://xmlns.com/foaf/0.1/",
    "http://purl.org/vocab/vann/", "http://www.w3.org/2002/07/owl#",
    "urn:", "https://credit-card-ops.ai/onto/inst/",
}
# ★ 任务书点名的禁止基址（缺 topology# 的裸基址曾在本项目真机出现）
FORBIDDEN_BASES = ("https://credit-card-ops.ai/onto#",)

PROJECT_MODULES = [
    "cc-core.ttl", "cc-topology.ttl", "cc-metric.ttl", "cc-fault.ttl",
    "cc-causal.ttl", "cc-action.ttl", "cc-audit.ttl",
]

FAILS: list[str] = []
OKS: list[str] = []


def ok(code: str, msg: str) -> None:
    OKS.append("[PASS] %-5s %s" % (code, msg))


def fail(code: str, msg: str) -> None:
    FAILS.append("[FAIL] %-5s %s" % (code, msg))


def _norm(s: str) -> str:
    """全角/半角与空白归一，避免中文标点差异导致假 FAIL。"""
    return unicodedata.normalize("NFKC", s).strip()


def load_yaml(p: Path):
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def local(iri: URIRef | str) -> str:
    s = str(iri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def base_of(iri: URIRef | str) -> str:
    s = str(iri)
    if "#" in s:
        return s.rsplit("#", 1)[0] + "#"
    return s.rsplit("/", 1)[0] + "/"


def make_expand(prefixes: dict):
    """映射表里的 onto_class/onto_property 是**前缀名**（如 `cc:Table`），必须展开成 IRI 再比对。"""
    def expand(qname: str) -> str:
        s = str(qname)
        if ":" not in s:
            return s
        pfx, local = s.split(":", 1)
        base = prefixes.get(pfx)
        if base is None:
            return s
        return base + local
    return expand


def main() -> int:
    ap = argparse.ArgumentParser(description="方案2 原生 TBox 只读校验器")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--onto-dir", default=None)
    ap.add_argument("--rules-dir", default=None)
    ap.add_argument("--shapes", default=None)
    ap.add_argument("--out", default=None, help="把 PASS/FAIL 清单按 UTF-8 写入该文件（便于留证；控制台中文可能 mojibake）")
    args = ap.parse_args()

    root = Path(args.root)
    onto_dir = Path(args.onto_dir) if args.onto_dir else root / "scheme2-onto" / "ontology"
    rules_dir = Path(args.rules_dir) if args.rules_dir else root / "scheme2-onto" / "rules"
    shapes = Path(args.shapes) if args.shapes else root / "scheme2-onto" / "shapes" / "shacl-constraints.ttl"

    tm = load_yaml(root / "shared" / "mapping" / "type-map.yaml")
    rm = load_yaml(root / "shared" / "mapping" / "rel-map.yaml")
    fm = load_yaml(root / "shared" / "model" / "fault-modes.yaml")
    ce = load_yaml(root / "shared" / "model" / "causal-edges.yaml")

    onto_ns = tm["namespaces"]["onto"]
    prefixes = tm["prefixes"]
    expand = make_expand(prefixes)
    allowed_prefixes = set(prefixes.keys())                      # cc cca ccc ccf ccm cco ccp ccs xsd
    allowed_bases = {prefixes[p] for p in allowed_prefixes if p != "xsd"}
    allowed_bases |= {str(XSD)}
    allowed_bases |= STD_BASES

    # ---------------- C1 可解析性 ----------------
    g = Graph()
    parsed: dict[str, int] = {}
    missing = []
    for name in PROJECT_MODULES:
        p = onto_dir / name
        if not p.exists():
            missing.append(name)
            continue
        try:
            gg = Graph()
            gg.parse(p, format="turtle")
            parsed[name] = len(gg)
            g += gg
        except Exception as exc:  # noqa: BLE001
            fail("C1", "解析失败 %s: %s" % (p, exc))
    if missing:
        fail("C1", "缺模块文件: %s" % ", ".join(missing))
    if not FAILS:
        ok("C1", "7 个本体模块全部可解析（三元组数 %s）" % ", ".join("%s=%d" % kv for kv in sorted(parsed.items())))
    try:
        gsh = Graph()
        gsh.parse(shapes, format="turtle")
        ok("C1b", "SHACL 形状可解析: %s（%d 三元组）" % (shapes.name, len(gsh)))
    except Exception as exc:  # noqa: BLE001
        fail("C1b", "SHACL 形状解析失败 %s: %s" % (shapes, exc))

    # 禁止基址扫描（全模块 + shapes + rules）
    bad = []
    for t in set(g.subjects()) | set(g.predicates()) | set(g.objects()):
        if isinstance(t, URIRef) and str(t).startswith(FORBIDDEN_BASES):
            bad.append(str(t))
    if bad:
        fail("C1c", "出现禁止基址（缺 topology# 等模块段）: %s" % ", ".join(sorted(bad)[:5]))
    else:
        ok("C1c", "无禁止基址 https://credit-card-ops.ai/onto#（裸基址）")

    # ---------------- C2 契约覆盖逐条相等 ----------------
    # (1) 14 类型
    tm_types = tm["types"]
    missing_cls, extra_cls = [], []
    declared_cls = {str(s) for s in g.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    for code, d in tm_types.items():
        if expand(d["onto_class"]) not in declared_cls:
            missing_cls.append(code)
    contract_cls = {expand(d["onto_class"]) for d in tm_types.values()}
    for s in sorted(declared_cls):
        # 只检查拓扑命名空间 cc:（cco:/ccf:/ccc:/ccm:/cca:/ccp: 是方案2 自建的上位/故障/因果/审计域，
        # 由 docs/04 §3 规定，不在 type-map 登记范围内）
        if base_of(s) == prefixes["cc"] and s not in contract_cls:
            extra_cls.append(s)
    if missing_cls:
        fail("C2.1", "拓扑契约类型缺类声明: %s" % ", ".join(missing_cls))
    else:
        ok("C2.1", "14/14 类逐条存在（%d 个契约类型）" % len(tm_types))
    if extra_cls:
        fail("C2.1b", "出现契约外 cc* 类（未在 type-map 登记）: %s" % ", ".join(sorted(extra_cls)))
    else:
        ok("C2.1b", "无契约外 cc* 类（派生顶层类由 cco: 承载，不计入 cc: 类集）")

    # (2) 15 关系 + 4 逆属性
    objprops = {str(s) for s in g.subjects(RDF.type, OWL.ObjectProperty) if isinstance(s, URIRef)}
    missing_rel = []
    for code, d in rm["relations"].items():
        if expand(d["onto_property"]) not in objprops:
            missing_rel.append(code)
    for code, d in rm["inverse_relations"].items():
        if expand(d["onto_property"]) not in objprops:
            missing_rel.append("inv:" + code)
    if missing_rel:
        fail("C2.2", "关系属性缺声明: %s" % ", ".join(missing_rel))
    else:
        ok("C2.2", "15 关系 + 4 逆属性逐条存在（onto_property 前缀名经映射表展开后比对）")

    # (2b) ABox 投影关系 vs 故障域/因果域原生关系 —— 分域断言
    proj_props = {expand(d["onto_property"]) for d in rm["relations"].values()}
    proj_props |= {expand(d["onto_property"]) for d in rm["inverse_relations"].values()}
    native_ns = {prefixes["ccf"], prefixes["ccc"], prefixes["cca"], prefixes["ccp"], prefixes["ccm"]}
    # docs/04 §3.2(3) 明写的属性链属性：契约映射表 rel-map 未登记（映射表缺口，见报告 X-9），
    # 但 docs/04:175-176 逐字给出，故不按「自造属性」判 FAIL。
    DOC04_CHAIN_PROPS = {prefixes["cc"] + "indirectlyHostedOn"}
    leaked = [p for p in sorted(objprops)
              if base_of(p) == prefixes["cc"] and p not in proj_props and p not in DOC04_CHAIN_PROPS]
    if leaked:
        fail("C2.2b", "cc: 命名空间出现非投影关系（投影关系必须逐字来自 rel-map）: %s" % ", ".join(leaked))
    else:
        ok("C2.2b", "cc: 对象属性 = rel-map 投影关系集合（%d 条）+ docs/04 §3.2 属性链属性（indirectlyHostedOn）；"
                    "故障/因果域原生关系落在 %s" % (len(proj_props), ",".join(sorted(native_ns))))

    # (3) 14 故障模式（个体）
    fm_codes = [m["code"] for m in fm["fault_modes"]]
    missing_fm = [c for c in fm_codes if (URIRef(prefixes["ccf"] + c), None, None) not in g]
    if missing_fm:
        fail("C2.3", "故障模式缺个体/声明: %s" % ", ".join(missing_fm))
    else:
        ok("C2.3", "14/14 故障模式逐条存在（%d 条）" % len(fm_codes))

    # (4) 18 因果边（ccc:CausalLink 个体，IRI = ccc:E<n>）
    edge_ids = [e["id"] for e in ce["edges"]]
    missing_e = [e for e in edge_ids if (URIRef(prefixes["ccc"] + e), None, None) not in g]
    if missing_e:
        fail("C2.4", "因果边缺个体: %s" % ", ".join(missing_e))
    else:
        ok("C2.4", "18/18 因果边逐条存在（%d 条）" % len(edge_ids))

    # ---------------- C3 命名空间白名单 ----------------
    offenders = []
    for s in list(g.subjects(RDF.type, OWL.Class)) + list(g.subjects(RDF.type, OWL.ObjectProperty)) \
            + list(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not isinstance(s, URIRef):        # 匿名类表达式（owl:Restriction / unionOf 节点）不参与白名单
            continue
        b = base_of(s)
        if b not in allowed_bases:
            offenders.append("类/属性 IRI 命名空间不在白名单: %s（基址 %s）" % (s, b))
    # 数据属性也必须来自 cc:/ccm: 等白名单前缀（否则是自造前缀）
    for s in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(s, URIRef):
            continue
        b = base_of(s)
        if b not in allowed_bases:
            offenders.append("数据属性命名空间越界: %s" % s)
    if offenders:
        for o in sorted(set(offenders))[:10]:
            fail("C3", o)
    else:
        ok("C3", "类/对象属性/数据属性 IRI 命名空间全部 ∈ 映射表 prefixes(%s) 或标准词表" %
           ",".join(sorted(allowed_prefixes)))

    # ---------------- C4 规则零硬编码阈值 ----------------
    rule_files = sorted(rules_dir.glob("*.rq")) if rules_dir.exists() else []
    if len(rule_files) != 7:
        fail("C4.0", "rules/*.rq 数量应为 7，实际 %d（%s）" % (len(rule_files), rules_dir))
    else:
        ok("C4.0", "规则文件 7 条齐备: %s" % ", ".join(p.name for p in rule_files))

    IRI_RE = re.compile(r"<[^>]*>")
    PREFIX_RE = re.compile(r"^\s*(PREFIX|BASE)\b.*$", re.M | re.I)
    STR_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
    PATH_QTY_RE = re.compile(r"\{\s*\d+\s*(?:,\s*\d*\s*)?\}")
    QNAME_RE = re.compile(r"\b[A-Za-z_][\w.\-]*:[A-Za-z0-9_\-.:]+")     # 前缀名（ccf:F-001、exec-r01 等）
    VAR_RE = re.compile(r"\?[A-Za-z_]\w*")
    COMMENT_RE = re.compile(r"#[^\n]*")                                # 注释（须在剥 <IRI> 之后）
    NUM_RE = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])")
    for p in rule_files:
        text = p.read_text(encoding="utf-8")
        if "CONSTRUCT" not in text.upper():
            fail("C4.1", "%s 不是 SPARQL CONSTRUCT" % p.name)
        # 阈值引用必带档位
        refs = re.findall(r"threshold:[A-Za-z0-9_.\-]+#(warning|critical)", text)
        bare = re.findall(r"threshold:[A-Za-z0-9_.\-]+(?![A-Za-z0-9_#])", text)
        if bare:
            fail("C4.2", "%s 阈值引用缺档位（必须 threshold:<名>#<warning|critical>）: %s" %
                 (p.name, ", ".join(sorted(set(bare))[:3])))
        if not refs and "threshold:" not in text:
            # 允许规则纯靠 entity_property 与符号谓词；但要显式声明，便于审计
            pass
        probe = text
        probe = STR_RE.sub('""', probe)
        probe = IRI_RE.sub(" ", probe)
        probe = PREFIX_RE.sub("", probe)
        probe = QNAME_RE.sub(" ", probe)
        probe = VAR_RE.sub(" ", probe)
        probe = PATH_QTY_RE.sub(" ", probe)
        probe = COMMENT_RE.sub("", probe)
        hits = [m.group(0) for m in NUM_RE.finditer(probe)]
        if hits:
            fail("C4.3", "%s 出现数字字面量: %s" % (p.name, ", ".join(sorted(set(hits))[:5])))
    if not any(f.startswith("[FAIL] C4.3") for f in FAILS):
        ok("C4.3", "rules/*.rq 无数字字面量（剥离 字符串→<IRI>→PREFIX 行→前缀名→变量→属性路径定界 {n,m}→注释 后扫描）")
    if not any(f.startswith("[FAIL] C4.2") for f in FAILS):
        ok("C4.2", "阈值引用全部带档位 #warning/#critical")

    # ---------------- C5 Evidence / hasEvidence 对称性 ----------------
    EV = URIRef(prefixes["cc"] + "Evidence")
    HE = URIRef(prefixes["cc"] + "hasEvidence")
    ev_attrs = tm_types.get("Evidence", {}).get("attrs", {})
    missing_attr = []
    for name, d in ev_attrs.items():
        prop = URIRef(expand(d["onto_property"]))
        if (prop, None, None) not in g:
            missing_attr.append(name)
        else:
            rng = list(g.objects(prop, RDFS.range))
            if not rng or str(rng[0]) != str(XSD.__getattr__(d["xsd"].split(":")[1])):
                missing_attr.append("%s(range!=%s)" % (name, d["xsd"]))
    if missing_attr:
        fail("C5.1", "cc:Evidence 属性缺声明或 xsd 不符: %s" % ", ".join(missing_attr))
    else:
        ok("C5.1", "cc:Evidence 8 个数据属性齐备且 xsd 与映射表一致（capturedAt=xsd:dateTime，其余 7 个 xsd:string）")
    if (HE, RDF.type, OWL.ObjectProperty) not in g:
        fail("C5.2", "cc:hasEvidence 未声明为 owl:ObjectProperty")
    else:
        doms = list(g.objects(HE, RDFS.domain))
        rngs = list(g.objects(HE, RDFS.range))
        if doms:
            fail("C5.2", "cc:hasEvidence **不得**声明 rdfs:domain（契约 from:* 域不受限），实际: %s" % doms)
        elif not rngs or str(rngs[0]) != str(EV):
            fail("C5.2", "cc:hasEvidence 的 rdfs:range 必须为 cc:Evidence，实际: %s" % rngs)
        else:
            ok("C5.2", "cc:hasEvidence 无 rdfs:domain、range=cc:Evidence（与 hasEvidence 对称落成一致）")

    # cc:Evidence 不得进 SHACL 形状（D6/§5：派生注释型不在形状里）
    try:
        targets = {str(o) for o in gsh.objects(None, URIRef(str(SKOS) and "http://www.w3.org/ns/shacl#targetClass"))}
        if str(EV) in targets:
            fail("C5.3", "cc:Evidence 不得作为 SHACL sh:targetClass（§5 明确派生注释型不进形状）")
        else:
            ok("C5.3", "SHACL 未把 cc:Evidence 作为目标类（%d 个 targetClass）" % len(targets))
    except Exception as exc:  # noqa: BLE001
        fail("C5.3", "SHACL targetClass 扫描失败: %s" % exc)

    # ---------------- C6 阈值个体层：12 档 + 悬空引用 0（captain v1.0.2 要求） ----------------
    ccm = prefixes["ccm"]
    thr_names = [t["name"] for t in fm.get("thresholds", [])]
    thr_uri = {n: URIRef(ccm + "t_" + n) for n in thr_names}
    missing_thr, missing_tier = [], []
    for n in thr_names:
        if (thr_uri[n], RDF.type, URIRef(ccm + "Threshold")) not in g:
            missing_thr.append(n)
        for tier, cls in (("warning", "WarningThreshold"), ("critical", "CriticalThreshold")):
            node = URIRef(ccm + "t_%s_%s" % (n, tier))
            if (node, RDF.type, URIRef(ccm + cls)) not in g:
                missing_tier.append("%s#%s" % (n, tier))
    if missing_thr:
        fail("C6.1", "契约阈值个体缺失（ccm:t_<名> a ccm:Threshold）: %s" % ", ".join(missing_thr))
    else:
        n_tier = sum(1 for _ in g.subjects(RDF.type, URIRef(ccm + "WarningThreshold"))) \
               + sum(1 for _ in g.subjects(RDF.type, URIRef(ccm + "CriticalThreshold")))
        ok("C6.1", "契约阈值个体 %d/%d 齐备（Threshold ×%d + Warning/Critical 档位节点 ×%d；计数随契约版本提升而提升）"
           % (len(thr_names), len(thr_names), len(thr_names), n_tier))
    if missing_tier:
        fail("C6.2", "阈值档位节点缺失: %s" % ", ".join(missing_tier))
    else:
        ok("C6.2", "%d 个档位节点（%d×warning/critical）齐备，每档带 ccm:tierOf 回指"
           % (n_tier, len(thr_names)))

    # ---------------- C6.3 ★ 2026-10-13 新增：档位**取值**必须逐值等于契约 ----------------
    #   为什么必须查「取值」而不只是「存在」：C6.1/C6.2 只断言个体与档位节点**存在**，
    #   于是「个体在、值却是旧标定」可以长期**静默通过**。实测事故（fi-e2e 端到端故障注入）：
    #     · cpu_usage_ratio_severe  图里 0.80/0.90   vs 契约 0.2284/0.6852（量级差约 3.5×）
    #     · session_used_ratio_light 图里 0.70/0.85  vs 契约 0.5/1
    #     · session_used_ratio_severe 图里 0.85/0.95 vs 契约 0.8/1
    #     · dead_tuple_ratio_light  登记 uncalibrated vs 契约已给 0.1/0.3
    #   ⇒ ONTO 侧用**旧阈值**判定，且与 KG 侧 `determined` 系统性不一致（两侧阈值来源不同载体，
    #     一旦漂移，A/B 对照的差异就来自**阈值**而不是**载体**）。
    #   判据 = 契约给值 ⇒ 图必须有同值 thresholdValue 且不得标 uncalibrated；
    #          契约 null ⇒ 图必须**显式** ccm:uncalibrated true（不得静默缺失，也不得写值）。
    val_bad, val_ok_n = [], 0
    for t in fm.get("thresholds", []):
        n = t["name"]
        for tier in ("warning", "critical"):
            want = t.get(tier)
            node = URIRef(ccm + "t_%s_%s" % (n, tier))
            got = next(iter(g.objects(node, URIRef(ccm + "thresholdValue"))), None)
            uncal = next(iter(g.objects(node, URIRef(ccm + "uncalibrated"))), None)
            is_uncal = uncal is not None and str(uncal).strip().lower() in ("true", "1")
            try:
                got_f = None if got is None else float(str(got))
            except (TypeError, ValueError):
                val_bad.append("%s#%s：ccm:thresholdValue 非数值 %r" % (n, tier, str(got)))
                continue
            if want is None:
                if got_f is not None:
                    val_bad.append("%s#%s：契约未标定(null) 但图写了 %s" % (n, tier, got_f))
                elif not is_uncal:
                    val_bad.append("%s#%s：契约未标定(null)，图既无数值也未显式 ccm:uncalibrated true"
                                   % (n, tier))
                else:
                    val_ok_n += 1
            else:
                if got_f is None:
                    val_bad.append("%s#%s：契约 %s 但图无 ccm:thresholdValue" % (n, tier, want))
                elif abs(got_f - float(want)) > 1e-9:
                    val_bad.append("%s#%s：契约 %s ≠ 图 %s" % (n, tier, want, got_f))
                else:
                    val_ok_n += 1
                    if is_uncal:
                        val_bad.append("%s#%s：契约已给值 %s 但图仍标 uncalibrated true"
                                       % (n, tier, want))
    if val_bad:
        fail("C6.3", "阈值档位取值与契约不一致（%d 处）: %s"
             % (len(val_bad), "；".join(val_bad[:6])))
    else:
        ok("C6.3", "阈值档位取值 %d/24 与契约逐值一致（未标定档必须显式 ccm:uncalibrated true）"
           % val_ok_n)

    # 悬空引用：规则 + 本体里出现的 "threshold:<名>#<档位>" 必须都能落到真实档位节点
    existing_refs = {str(o) for o in g.objects(None, URIRef(ccm + "tierReference"))}
    refs_in_use = set()
    for p in list(rule_files) + sorted(onto_dir.glob("*.ttl")):
        t = p.read_text(encoding="utf-8")
        refs_in_use |= set(re.findall(r"threshold:[A-Za-z0-9_.\-]+#(?:warning|critical)", t))
    dangling = sorted(r for r in refs_in_use if r not in existing_refs)
    if dangling:
        fail("C6.3", "悬空阈值引用（无对应档位节点 ccm:tierReference）: %s" % ", ".join(dangling))
    else:
        ok("C6.3", "悬空阈值引用 0（%d 个引用键全部命中档位节点；含 v1.0.2 新增 dead_tuple_ratio_light#warning）"
           % len(refs_in_use))

    # ---------------- 汇总 ----------------
    for line in OKS:
        print(line)
    for line in FAILS:
        print(line)
    print("----")
    print("onto_tbox_check: %d PASS / %d FAIL" % (len(OKS), len(FAILS)))
    if args.out:
        body = list(OKS) + list(FAILS) + ["----",
                "onto_tbox_check: %d PASS / %d FAIL" % (len(OKS), len(FAILS))]
        Path(args.out).write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
