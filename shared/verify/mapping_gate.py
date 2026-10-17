#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M1.5 · **映射表门禁**（只读、零外部依赖、可回归）。

体例与 `shared/verify/contract_gates.py` 一致：`RESULT: PASS (ERROR=n WARN=m)`，
有 ERROR 即非零退出。

为什么需要本门禁（v1.1.0 的两个实测缺陷）：
  * 缺陷 1：投影器用全局口径 `MATCH (n) WHERE n.<prop> IS NOT NULL` 计数指标。
    Neo4j 侧与既有 v2 图**共库共存**，v2 的 `Session`/`RuntimeEntity` 节点恰好带
    `waitEvent` 属性，于是 fixture 上 `db.session.wait_event` 数出 3 而非 1
    （真机恰好无同名属性，所以真机 PASS 纯属运气）。→ 修法：计数一律按
    **(对象类型, 属性)** 双侧限定，口径唯一定义在 `shared/project/ccf_plan.py`。
  * 缺陷 2：指标属性名**不是单射**。70 个指标码只有 68 个不同 `kg_property`：
    `cpuUsageRatio` 由 `container.cpu_usage_ratio` / `db.instance.cpu_usage_ratio` /
    `host.cpu_usage_ratio` 三个码共用。跨对象复用同名属性是**合法**的（LPG 常态、
    OWL 里共享 `ccm:` 属性同样是常态），**同对象撞名才是真缺陷** —— M5 拦它。

检查项：
  M1  schema 的每个 typeCode 都在 types 里；具体类型的 kg_label / onto_class 齐备
  M2  kg_label 带 label_prefix 且唯一
  M3  kg_reltype 带 reltype_prefix 且唯一；from / to 必须是已声明类型
  M4  全部 CURIE 的前缀可在 prefixes 块解析；断言关系、反向关系的 onto_property 各自唯一
  M5  ★ 同一对象类型下两个指标码共用同一 kg_property / onto_property —— ERROR
  M6  指标的 kg_property 不得与该对象自身的 key_attrs / attrs 撞名，也不得占用保留名
  M7  跨对象复用同名属性 —— WARN（合法，但计数必须按对象类型限定；列清单备查）
  M8  prefixes 块必须覆盖 namespaces 里出现的全部前缀 + xsd
  M9  ★ 裁决 13 的机械化：投影器与中性层**不得硬编码载体名**
  M10 observation_projection 三态属性名齐备且前缀可解析
  M11 ★ v3-ccf-map.app_derive.keys：字段名必须逐字等于 topology.yaml 的 attrs 名；
      attrs ⊆ 该类型 attrs；key_attrs == topology 的 key_attrs 且 ⊆ attrs；link_attrs 不得兼作属性
  M12 ★ app_derive.relations：code 在 relationCode enum 里；from_field/to_field 必须落在被遍历
      清单的字段集（id_field ∪ link_attrs）里；to_kind=same 时 to_field 必须等于目标类型的 id_field
  M13 ★ app/container 四个 obj_type 必须显式映射（缺失即整批丢弃）；scope_layers 含 app；
      expected_profiles.l1-l2 的 types / relationsMin 必须覆盖 app_derive 建出的全部类型与关系

用法：python shared/verify/mapping_gate.py [--mapping-dir ...] [--model-dir ...]
"""

import ast
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

F = []
CURIE_RE = re.compile(r"([A-Za-z][A-Za-z0-9_.-]*):")
HARDCODE_LABEL_RE = re.compile(r"\bCC_[A-Za-z0-9_]")

# 不得硬编码载体名的“无载体名区”
NO_CARRIER_DIRS = ("scheme1-kg/project", "scheme2-onto/project", "shared/project")


def add(sev, code, msg):
    F.append((sev, code, msg))


def read_yaml(path):
    import yaml
    with io.open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def string_literals(path):
    """返回**非文档字符串**的字符串字面量 [(lineno, value)]。

    注释天然不在 AST 里；模块/类/函数的 docstring 显式排除 —— 文档里出现 `CC_`
    是允许的（那是在解释前缀政策），代码里出现才是硬编码。
    """
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in doc_ids):
            out.append((node.lineno, node.value))
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mapping-dir", default=os.path.join(ROOT, "shared", "mapping"))
    ap.add_argument("--model-dir", default=os.path.join(ROOT, "shared", "model"))
    args = ap.parse_args(argv)

    tm = read_yaml(os.path.join(args.mapping_dir, "type-map.yaml"))
    rm = read_yaml(os.path.join(args.mapping_dir, "rel-map.yaml"))
    schema = json.load(io.open(os.path.join(args.model_dir, "ccf.schema.json"),
                               encoding="utf-8"))

    types = tm["types"]
    metrics = tm["metrics"]
    prefixes = tm.get("prefixes") or {}
    common = tm.get("common_properties") or {}
    ns = tm["namespaces"]
    obs = tm.get("observation_projection") or {}
    lprefix = ns["kg"]["label_prefix"]
    rprefix = ns["kg"]["reltype_prefix"]
    type_codes = schema["$defs"]["typeCode"]["enum"]
    metric_codes = schema["$defs"]["metricCode"]["enum"]

    def resolve(curie):
        p, _, local = curie.partition(":")
        return prefixes.get(p), local

    # ---- M1 ----
    for code in type_codes:
        spec = types.get(code)
        if spec is None:
            add("ERROR", "M1", "typeCode %s 不在 type-map.types 里" % code)
            continue
        for key in ("kg_label", "onto_class", "layer", "abstract"):
            if key not in spec:
                add("ERROR", "M1", "类型 %s 缺 %s" % (code, key))
        base, _local = resolve(spec.get("onto_class", "?"))
        if base is None:
            add("ERROR", "M1", "类型 %s 的 onto_class 前缀不可解析：%s"
                % (code, spec.get("onto_class")))
    for code, spec in types.items():
        if spec.get("abstract"):
            # 抽象类型（ConnectionPool）按设计不进实例 typeCode enum：
            # topology.yaml 声明 13 个类型，ccf.schema.json 的 typeCode enum 只有 12 个具体类型。
            continue
        if spec.get("annotation"):
            # ★ M1.5c：派生型（Evidence）不是 CCF 的对象类型 —— 它由 observations[] 派生，
            #   不参与 objects[]，故按设计**不进** typeCode enum；反过来还要校验它确实标注了
            #   annotation 且 layer 为 null（见下）。
            if spec.get("layer") is not None:
                add("ERROR", "M1", "派生型 %s 的 layer 必须是 null" % code)
            continue
        if code not in type_codes:
            add("ERROR", "M1", "type-map.types 的 %s 不在 ccf.schema.json 的 typeCode enum 里"
                % code)

    # ---- M2 ----
    seen = {}
    for code, spec in types.items():
        label = spec.get("kg_label", "")
        if not label.startswith(lprefix):
            add("ERROR", "M2", "类型 %s 的 kg_label=%s 未带 label_prefix=%s"
                % (code, label, lprefix))
        if label in seen:
            add("ERROR", "M2", "kg_label %s 同时映射到 %s 与 %s" % (label, seen[label], code))
        seen[label] = code

    # ---- M3 ----
    rseen = {}
    for code, spec in rm["relations"].items():
        rt = spec.get("kg_reltype", "")
        if not rt.startswith(rprefix):
            add("ERROR", "M3", "关系 %s 的 kg_reltype=%s 未带 reltype_prefix=%s"
                % (code, rt, rprefix))
        if rt in rseen:
            add("ERROR", "M3", "kg_reltype %s 同时映射到 %s 与 %s" % (rt, rseen[rt], code))
        rseen[rt] = code
        for role in ("from", "to"):
            val = spec.get(role)
            if val == "*":
                # ★ M1.5c：域不受限（HAS_EVIDENCE.from）——OWL 侧 cc:hasEvidence
                #   不声明 rdfs:domain，LPG 侧无约束，故 "*" 是合法取值。
                continue
            if val not in types:
                add("ERROR", "M3", "关系 %s 的 %s=%s 不是已声明类型"
                    % (code, role, val))

    # ---- M4 ----
    for code, spec in rm["relations"].items():
        base, _l = resolve(spec.get("onto_property", "?"))
        if base is None:
            add("ERROR", "M4", "关系 %s 的 onto_property 前缀不可解析：%s"
                % (code, spec.get("onto_property")))
    for group, name in ((rm["relations"], "断言关系"), (rm["inverse_relations"], "反向关系")):
        props = {}
        for code, spec in group.items():
            p = spec.get("onto_property")
            if p in props:
                add("ERROR", "M4", "%s 的 onto_property %s 被 %s 与 %s 共用"
                    % (name, p, props[p], code))
            props[p] = code
    for code, spec in rm["inverse_relations"].items():
        if spec.get("inverse_code") not in rm["relations"]:
            add("ERROR", "M4", "反向关系 %s 的 inverse_code=%s 不是断言关系"
                % (code, spec.get("inverse_code")))
        if spec.get("asserted") is not False:
            add("ERROR", "M4", "反向关系 %s 必须 asserted: false（否则两侧实例数无法对账）" % code)

    # ---- M5 / M6 / M7 ----
    reserved = set()
    for side in ("kg", "onto"):
        for _k, v in (common.get(side) or {}).items():
            reserved.add(v.split(":")[-1])
    kg_group, onto_group = {}, {}
    for code in metric_codes:
        spec = metrics.get(code)
        if spec is None:
            add("ERROR", "M5", "指标 %s 不在 type-map.metrics 里" % code)
            continue
        obj = spec["object"]
        if obj not in types or types[obj].get("abstract"):
            add("ERROR", "M5", "指标 %s 的 object=%s 不是具体类型" % (code, obj))
            continue
        for key, group, what in (("kg_property", kg_group, "kg_property"),
                                 ("onto_property", onto_group, "onto_property")):
            k = (obj, spec.get(key))
            if k in group:
                add("ERROR", "M5", "★ 同一对象 %s 下两个指标码共用 %s=%s：%s 与 %s"
                    % (obj, what, spec.get(key), group[k], code))
            group[k] = code
    # 跨对象复用（合法）→ WARN
    for what, group in (("kg_property", kg_group), ("onto_property", onto_group)):
        by_name = {}
        for (obj, prop), code in group.items():
            by_name.setdefault(prop, []).append((obj, code))
        for prop, items in sorted(by_name.items()):
            if len(items) > 1:
                add("WARN", "M7", "跨对象复用 %s=%s：%s —— 合法（LPG 常态 / OWL 共享属性），"
                    "但指标计数必须按 (对象类型, 属性) 限定"
                    % (what, prop, ", ".join("%s/%s" % it for it in sorted(items))))
    # M6：属性名与指标名不得同对象撞名
    for code in metric_codes:
        spec = metrics.get(code)
        if spec is None:
            continue
        obj = spec["object"]
        if obj not in types:
            continue
        tspec = types[obj]
        own = set(tspec.get("key_attrs") or [])
        for name in (tspec.get("attrs") or {}):
            own.add(name)
        prop = spec.get("kg_property")
        if prop in own:
            add("ERROR", "M6", "指标 %s 的 kg_property=%s 与对象 %s 自身属性撞名"
                % (code, prop, obj))
        if prop in reserved:
            add("ERROR", "M6", "指标 %s 的 kg_property=%s 占用了保留属性名" % (code, prop))
    # 类型属性本身：key_attrs 自身不得重复，且必须是 attrs 的子集（key_attrs ⊆ attrs 是设计）
    for code, tspec in types.items():
        ka = list(tspec.get("key_attrs") or [])
        at = set((tspec.get("attrs") or {}))
        if len(ka) != len(set(ka)):
            add("ERROR", "M6", "类型 %s 的 key_attrs 自身重复：%s" % (code, ka))
        if not set(ka) <= at:
            add("ERROR", "M6", "类型 %s 的 key_attrs %s 未被 attrs 覆盖"
                % (code, sorted(set(ka) - at)))
        for name in ka + sorted(at):
            if name in reserved:
                add("ERROR", "M6", "类型 %s 的属性 %s 占用了保留属性名" % (code, name))
    # M6b：指标属性与类型属性必须落在不同的 IRI 前缀（ONTO 侧靠命名空间分隔，
    # KG 侧无命名空间，只有靠 kg_property ≠ 任何 attr 键这一条保证）
    tpfx = {resolve(spec.get("onto_class", "?"))[0] for spec in types.values()}
    tpfx.discard(None)
    mpfx = {resolve(spec.get("onto_property", "?"))[0] for spec in metrics.values()}
    mpfx.discard(None)
    if tpfx & mpfx:
        add("ERROR", "M6", "指标属性与类型属性共用 IRI 前缀 %s —— ONTO 侧将无法区分属性与指标"
            % sorted(tpfx & mpfx))

    # ---- M8 ----
    # 只把形如 `pfx:rest` 且不含 `//` 的值当作前缀引用：
    # `id_separator_from: ":"`（首字符非字母）与 `base: "https://…"`（scheme，含 `//`）
    # 都不是前缀引用，不得参与 M8。
    pre_re = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:")
    used = set(prefixes)
    for coll in (ns.get("kg") or {}, ns.get("onto") or {}):
        for v in coll.values():
            if isinstance(v, str) and pre_re.match(v) and "//" not in v:
                used.add(v.split(":")[0])
    for p in sorted(used):
        if p not in prefixes:
            add("ERROR", "M8", "prefixes 块缺前缀 %s（namespaces 里用到）" % p)
    for p in sorted(prefixes):
        if not prefixes[p].endswith(("#", "/", ":")):
            add("WARN", "M8", "前缀 %s 的 IRI 不以 # / : 结尾：%s" % (p, prefixes[p]))

    # ---- M9 ----
    carriers = set()
    for spec in types.values():
        carriers.add(spec.get("kg_label"))
        carriers.add(spec.get("onto_class"))
    for group in (rm["relations"], rm["inverse_relations"]):
        for spec in group.values():
            carriers.add(spec.get("kg_reltype"))
            carriers.add(spec.get("onto_property"))
    for spec in metrics.values():
        carriers.add(spec.get("onto_property"))
    carriers = {c for c in carriers if c}
    n_scanned = 0
    for rel in NO_CARRIER_DIRS:
        d = os.path.join(ROOT, rel)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(d, fn)
            n_scanned += 1
            rel_path = os.path.relpath(path, ROOT)
            for lineno, val in string_literals(path):
                if HARDCODE_LABEL_RE.search(val):
                    add("ERROR", "M9", "%s:%d 非文档字符串里硬编码了标签前缀：%r"
                        % (rel_path, lineno, val[:80]))
                for c in carriers:
                    if c in val:
                        add("ERROR", "M9", "%s:%d 硬编码载体名 %s：%r"
                            % (rel_path, lineno, c, val[:80]))
    if n_scanned == 0:
        add("WARN", "M9", "未扫描到任何投影器源码（%s）" % ", ".join(NO_CARRIER_DIRS))

    # ---- M10 ----
    for side, what in (("kg", "kg"), ("onto", "onto")):
        blk = obs.get(side) or {}
        for key in ("not_collected", "collected_empty"):
            if not blk.get(key):
                add("ERROR", "M10", "observation_projection.%s 缺 %s" % (what, key))
                continue
            if side == "onto":
                base, _l = resolve(blk[key])
                if base is None:
                    add("ERROR", "M10", "observation_projection.onto.%s 前缀不可解析：%s"
                        % (key, blk[key]))

    # ---- M11 / M12 / M13（M1.6：采集面 app_derive 的字段与关系契约）----
    vmap = read_yaml(os.path.join(args.model_dir, "v3-ccf-map.yaml"))
    topo = read_yaml(os.path.join(args.model_dir, "topology.yaml"))
    topo_attrs = {t["code"]: set(t.get("attrs") or {}) for t in (topo.get("types") or [])}
    topo_keys = {t["code"]: list(t.get("key_attrs") or []) for t in (topo.get("types") or [])}
    rel_codes = schema["$defs"]["relationCode"]["enum"]
    app = vmap.get("app_derive") or {}
    akeys = app.get("keys") or {}
    type_to_list = {}
    for _lname, _kspec in sorted(akeys.items()):
        _t = _kspec.get("type")
        if _t in type_to_list:
            add("ERROR", "M11", "app_derive.keys 里类型 %s 被 %s 与 %s 两个清单同时定义"
                % (_t, type_to_list[_t], _lname))
        type_to_list[_t] = _lname

    if not app:
        add("WARN", "M11", "v3-ccf-map.yaml 无 app_derive 块（L1/L2 采集面未启用）")

    for lname, kspec in sorted(akeys.items()):
        tcode = kspec.get("type")
        idf = kspec.get("id_field")
        kats = list(kspec.get("key_attrs") or [])
        lats = list(kspec.get("link_attrs") or [])
        attrs = list(kspec.get("attrs") or [])
        if tcode not in type_codes:
            add("ERROR", "M11", "app_derive.keys.%s.type=%r 不在 ccf.schema typeCode enum 里"
                % (lname, tcode))
            continue
        if not idf:
            add("ERROR", "M11", "app_derive.keys.%s 缺 id_field" % lname)
        known = topo_attrs.get(tcode) or set()
        if not known:
            add("ERROR", "M11", "app_derive.keys.%s.type=%s 不在 topology.yaml 的类型表里"
                % (lname, tcode))
        for name in attrs:
            if name not in known:
                add("ERROR", "M11", "app_derive.keys.%s（%s）的属性 %s 不在 topology.yaml 的 attrs 里"
                    % (lname, tcode, name))
        for name in kats:
            if name not in known:
                add("ERROR", "M11", "app_derive.keys.%s 的 key_attr %s 不在 topology.yaml 的 attrs 里"
                    % (lname, name))
            if name not in attrs:
                add("ERROR", "M11", "app_derive.keys.%s 的 key_attr %s 未列在 attrs 里"
                    % (lname, name))
        if kats and kats != topo_keys.get(tcode):
            add("ERROR", "M11", "app_derive.keys.%s 的 key_attrs=%s ≠ topology.yaml 的 %s"
                % (lname, kats, topo_keys.get(tcode)))
        for name in lats:
            if name in attrs:
                add("ERROR", "M11", "app_derive.keys.%s 的 link_attr %s 同时被当成 CCF 属性"
                    % (lname, name))

    for rspec in (app.get("relations") or []):
        code = rspec.get("code")
        lst = rspec.get("list")
        if code not in rel_codes:
            add("ERROR", "M12", "app_derive.relations 的 code=%r 不在 ccf.schema relationCode enum 里"
                % (code,))
        if lst not in akeys:
            add("ERROR", "M12", "app_derive.relations %s 的 list=%r 不是 app_derive.keys 的清单"
                % (code, lst))
            continue
        lspec = akeys[lst]
        fields = set([lspec.get("id_field")] + list(lspec.get("link_attrs") or []))
        ff = rspec.get("from_field")
        if ff not in fields:
            add("ERROR", "M12", "app_derive.relations %s 的 from_field=%r 不在清单 %s 的字段集 %s 里"
                % (code, ff, lst, sorted(fields)))
        ftype = rspec.get("from_type")
        if ftype not in type_to_list:
            add("ERROR", "M12", "app_derive.relations %s 的 from_type=%r 没有对应的 keys 清单"
                % (code, ftype))
        elif ff == lspec.get("id_field") and ftype != lspec.get("type"):
            add("ERROR", "M12",
                "app_derive.relations %s 用 %s 自有 id 作 from_field，但 from_type=%s ≠ %s"
                % (code, lst, ftype, lspec.get("type")))
        kind = rspec.get("to_kind") or "same"
        if kind not in ("same", "cluster", "instance_by_ip"):
            add("ERROR", "M12", "app_derive.relations %s 的 to_kind=%r 非法" % (code, kind))
            continue
        tf = rspec.get("to_field")
        if kind == "same":
            ttype = rspec.get("to_type")
            if ttype not in type_to_list:
                add("ERROR", "M12", "app_derive.relations %s 的 to_type=%r 没有对应的 keys 清单"
                    % (code, ttype))
                continue
            tid = (akeys[type_to_list[ttype]] or {}).get("id_field")
            if tf != tid:
                add("ERROR", "M12", "app_derive.relations %s 的 to_field=%r 必须等于 %s 的 id_field=%r"
                    % (code, tf, ttype, tid))
            if tf not in fields:
                add("ERROR", "M12", "app_derive.relations %s 的 to_field=%r 不在清单 %s 的字段集 %s 里"
                    % (code, tf, lst, sorted(fields)))
        elif kind == "instance_by_ip":
            if tf not in fields:
                add("ERROR", "M12", "app_derive.relations %s 的 to_field=%r 不在清单 %s 的字段集 %s 里"
                    % (code, tf, lst, sorted(fields)))
        elif tf:
            add("ERROR", "M12", "app_derive.relations %s 的 to_kind=cluster 不该有 to_field=%r"
                % (code, tf))

    layers = list((vmap.get("meta") or {}).get("scope_layers") or [])
    for need in ("gaussdb", "os"):
        if need not in layers:
            add("ERROR", "M13", "v3-ccf-map.scope_layers 缺 %s" % need)
    if app and "app" not in layers:
        add("ERROR", "M13", "有 app_derive 块但 scope_layers 里没有 app")
    ott = vmap.get("objtype_to_type") or {}
    for ot, want in sorted({"container": "Container", "app_instance": "AppInstance",
                            "app_endpoint": "AppEndpoint",
                            "jdbc_datasource": "JdbcDataSource"}.items()):
        spec = ott.get(ot)
        if not spec or spec.get("drop"):
            add("ERROR", "M13", "objtype_to_type 缺 %s ⇒ 该层事实会被整批丢弃" % ot)
        elif spec.get("type") != want:
            add("ERROR", "M13", "objtype_to_type.%s.type=%r 应为 %s" % (ot, spec.get("type"), want))
    if app:
        p12 = (vmap.get("expected_profiles") or {}).get("l1-l2") or {}
        p12_types = list(p12.get("types") or [])
        p12_rels = dict(p12.get("relationsMin") or {})
        for tcode in sorted(type_to_list):
            if tcode not in p12_types:
                add("ERROR", "M13", "expected_profiles.l1-l2.types 缺 app_derive 建的类型 %s" % tcode)
        for rspec in (app.get("relations") or []):
            if rspec.get("code") not in p12_rels:
                add("ERROR", "M13", "expected_profiles.l1-l2.relationsMin 缺关系 %s"
                    % rspec.get("code"))

    # ---- 输出 ----
    errs = [f for f in F if f[0] == "ERROR"]
    warns = [f for f in F if f[0] == "WARN"]
    print("=" * 78)
    print("映射表门禁  type-map=%s" % os.path.join(args.mapping_dir, "type-map.yaml"))
    print("  types=%d  metrics=%d  relations=%d  inverse=%d  prefixes=%d"
          % (len(types), len(metrics), len(rm["relations"]),
             len(rm["inverse_relations"]), len(prefixes)))
    print("=" * 78)
    for sev, code, msg in F:
        print("[%s] [%s] %s" % (sev, code, msg))
    print("-" * 78)
    print("RESULT: %s (ERROR=%d WARN=%d)" % ("FAIL" if errs else "PASS",
                                             len(errs), len(warns)))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())