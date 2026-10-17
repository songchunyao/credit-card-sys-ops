"""分层边界静态检查 —— 锁定「采集层 → CCF → 投影器 → 知识服务」的单向依赖（硬约束 C2）

依据：`docs/00-总体设计与裁决记录.md:132` 约束 C2 原文
    「**禁止反向依赖**：知识服务不得被采集层调用；投影器不得被知识服务调用」
    「沿用 `upstream-ops-agent` 的 `tests/static/test_layer_boundary.py` 分层守卫」

为什么必须静态检查（承继上游同名守卫的理由）：
    反向依赖在**运行期几乎不可见** —— 服务照样起得来、图照样写得进，
    但「同一份 CCF 分别投影到两库、两侧可比」这个前提已经悄悄破了
    （例如投影器偷读对方载体、采集层直接依赖某个载体驱动、方案之间互相引用）。
    所以只能用 AST 把依赖方向钉死。

本文件锁定七件事：
  L1 中性层 / 采集层不得 import 载体驱动（neo4j / rdflib / SPARQL 客户端）
  L2 `shared/` 任何模块不得 import `scheme1-kg` / `scheme2-onto`（禁止 shared → scheme）
  L3 两个方案不得互相 import（方案间必须完全独立，否则对比不成立）
  L4 跨层 import 边必须**全部**在下方 ALLOWED_EDGES 里申报（新增边必须显式申报）
  L5 知识服务层不得被采集层 / 投影器 import（C2 的第一句）
  L6 本项目自有的命名空间（标签 / 关系 / IRI 基址）不得写进中性层与投影器
     —— `docs/01:18`「映射是数据不是代码」，只许活在 `shared/mapping/` 的产物与生成器里
  L7 数据流单向：采集层不得 import 投影器，投影器不得 import 采集层

诚实边界（不做的事）：
  * 只看源码里的 `import` 语句与字面量，**不追动态导入**（`importlib.import_module(x)` 变量拼接）；
  * 不检查数据内容与契约一致性 —— 那是 `shared/verify/contract_gates.py`、
    `shared/verify/mapping_gate.py`、`shared/verify/parity.py` 的职责；
  * L6 只覆盖**本项目自有**的命名空间字面量（`CC_` / `cc_uq_` / `credit-card-ops.ai`），
    不覆盖载体连接默认值（如默认库名 `neo4j`），那属于部署参数。

运行：
    python tests/static/test_layer_boundary.py       # 独立运行，非 0 退出 = 失败
    pytest tests/static/test_layer_boundary.py -q    # 也可由 pytest 驱动
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                      # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 层次声明：工作区相对目录 → 层名 ────────────────────────────────────
LAYER_DIRS = {
    "shared/collect": "collect",        # 采集层（CCF 固化）
    "shared/project": "plan",           # 中性解析层（CCF → 中性 Plan）
    "shared/mapping": "codegen",        # 映射表生成器（数据源，不是运行时）
    "shared/verify": "verify",          # 门禁与自检（只读）
    "scheme1-kg/project": "project",    # 投影器（Neo4j / LPG）
    "scheme2-onto/project": "project",  # 投影器（GraphDB / RDF）
    "scheme1-kg/service": "service",    # 知识服务（尚未实现）
    "scheme2-onto/service": "service",  # 知识服务（尚未实现）
}

# 以裸模块名导入（靠 sys.path.insert 而不是包路径）时的层归属。
# ★ 新加一个裸模块名到任何目录，都必须在这里登记，否则 L4 会把它当未知模块放过。
BARE_MODULES = {
    "ccf_plan": "plan",
    "freeze": "collect",
    "gen_maps": "codegen",
    "equivalence": "verify",
    "contract_gates": "verify",
    "mapping_gate": "verify",
    "parity": "verify",
    "f5_kgt_gate": "verify",
    "g8_info_parity": "verify",
    "h1_gate_proof": "verify",
}

# ── 允许的跨层依赖边（**白名单**：未申报的边一律判失败）──────────────────
ALLOWED_EDGES = {
    ("project", "plan"):
        "投影器必须经中性解析层读 CCF（两个投影器共用同一个 Plan，才有可比性）",
    ("verify", "plan"):
        "门禁复用中性层唯一口径（§6.3 计数口径定义在 ccf_plan）",
    ("collect", "verify"):
        "★ 全项目**唯一**一处下层依赖上层的边：freeze 用 equivalence 的 "
        "canonical_snapshot_sha() 算 canonicalSha256，并用同一套检查做 fail-closed 自检。"
        "注释：canonical 定义必须与验证侧同源，否则写进 CCF 的哈希没有意义。",
}

# ── L1：载体驱动（中性层与采集层都不得出现）────────────────────────────
CARRIER_DRIVERS = {
    "neo4j": "Neo4j 驱动（LPG 载体专属）",
    "rdflib": "RDF 库（RDF 载体专属）",
    "SPARQLWrapper": "SPARQL 客户端（RDF 载体专属）",
    "py2neo": "Neo4j 驱动（LPG 载体专属）",
}
NEUTRAL_LAYERS = ("collect", "plan")

# ── L6：本项目自有命名空间字面量（只许在 shared/mapping 里定义）──────────
OWN_NAMESPACE_LITERALS = {
    r"\bCC_[A-Za-z0-9_]": "KG 侧标签 / 关系类型前缀（应取自 namespaces.kg.*）",
    r"\bcc_uq_[a-z0-9_]": "KG 侧约束名前缀（应取自 namespaces.kg.constraint_prefix）",
    r"credit-card-ops\.ai": "本体 IRI 基址（应取自 namespaces.onto.base）",
}
L6_SCAN_DIRS = ("shared/project", "scheme1-kg/project", "scheme2-onto/project")


# ══════════════════════════════════════════════════════════════════ 工具
def iter_py(*rel_dirs):
    """遍历给定工作区相对目录下的 .py（跳过工作区内的临时目录）。"""
    for rel in rel_dirs:
        base = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in ("tmp", "__pycache__", ".git")]
            for f in sorted(fn):
                if f.endswith(".py"):
                    yield os.path.join(dp, f)


def read(path):
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def layer_of(path):
    r = rel(path)
    best = None
    for pre, lay in LAYER_DIRS.items():
        if r.startswith(pre + "/"):
            if best is None or len(pre) > len(best[0]):
                best = (pre, lay)
    return best[1] if best else None


def target_layer(module):
    """把一个被导入的模块名解析成层名；解析不出来 = 标准库 / 第三方 / 未知。"""
    top = (module or "").split(".")[0]
    if not top:
        return None
    if top in BARE_MODULES:
        return BARE_MODULES[top]
    for pre, lay in LAYER_DIRS.items():
        key = pre.split("/")[-1]
        if top in (key, key.replace("-", "_")):
            return lay
    return None


def imports_of(tree):
    """产出 (层名或 None, 被导入名, 行号)；只取绝对导入与裸导入。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield (node.module or ""), node.lineno


def strip_docstrings(src):
    """去掉模块/类/函数 docstring，只留可执行代码 —— 避免文档里的举例误报。

    ★ 与上游同名守卫的区别：这里**不**去普通字符串字面量。
      L6 要抓的正是「把载体名硬编码成字符串」，去掉字面量等于把检查废掉。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    return ast.unparse(tree)


# ══════════════════════════════════════════════════════════════════ L1
def check_L1_neutral_layers_avoid_carrier_drivers():
    bad = []
    for path in iter_py("shared/collect", "shared/project"):
        lay = layer_of(path)
        if lay not in NEUTRAL_LAYERS:
            continue
        for mod, lineno in imports_of(ast.parse(read(path))):
            top = mod.split(".")[0]
            if top in CARRIER_DRIVERS:
                bad.append("%s:%d import %s —— %s"
                           % (rel(path), lineno, mod, CARRIER_DRIVERS[top]))
    return bad


# ══════════════════════════════════════════════════════════════════ L2
def check_L2_shared_never_imports_a_scheme():
    """`shared/` 是两方案共用的下层 —— 它一旦 import 方案，方案就不再独立。

    判据只用两件事：① import 语句；② `sys.path.insert/append` 的实参里出现方案目录。
    注释 / docstring / 帮助文本里**提到**方案路径不算依赖（例如「改契约须重跑
    `shared/mapping/gen_maps.py`，投影器不要手改」这类说明文字）。
    """
    bad = []
    for path in iter_py("shared"):
        tree = ast.parse(read(path))
        for mod, lineno in imports_of(tree):
            top = mod.split(".")[0]
            if "scheme" in top or re.match(r"^(p1_|p2_)", top):
                bad.append("%s:%d import %s" % (rel(path), lineno, mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("insert", "append"):
                continue
            inner = node.func.value
            if not (isinstance(inner, ast.Attribute) and inner.attr == "path"):
                continue
            for a in node.args:
                try:
                    text = ast.unparse(a)
                except Exception:                       # pragma: no cover
                    continue
                if "scheme" in text:
                    bad.append("%s:%d sys.path 指向方案目录：%s"
                               % (rel(path), node.lineno, text))
    return sorted(set(bad))


# ══════════════════════════════════════════════════════════════════ L3
def check_L3_schemes_are_mutually_independent():
    bad = []
    pairs = (("scheme1-kg", "scheme2-onto"), ("scheme2-onto", "scheme1-kg"))
    for me, other in pairs:
        for path in iter_py(me):
            for mod, lineno in imports_of(ast.parse(read(path))):
                if other.replace("-", "_") in mod or other in mod:
                    bad.append("%s:%d import %s（方案之间必须互不依赖）"
                               % (rel(path), lineno, mod))
            for m in re.finditer(re.escape(other), read(path)):
                line = read(path)[:m.start()].count("\n") + 1
                bad.append("%s:%d 源码提到对侧方案目录「%s」"
                           % (rel(path), line, other))
    return sorted(set(bad))


# ══════════════════════════════════════════════════════════════════ L4
def cross_layer_edges():
    """现算全项目跨层 import 边：{(from_layer, to_layer): {例子}}。"""
    edges = {}
    for pre in LAYER_DIRS:
        for path in iter_py(pre):
            src_layer = layer_of(path)
            for mod, lineno in imports_of(ast.parse(read(path))):
                tgt = target_layer(mod)
                if tgt and src_layer and tgt != src_layer:
                    edges.setdefault((src_layer, tgt), set()).add(
                        "%s:%d -> %s" % (rel(path), lineno, mod))
    return edges


def check_L4_every_cross_layer_edge_is_declared():
    bad = []
    for (a, b), examples in sorted(cross_layer_edges().items()):
        if (a, b) in ALLOWED_EDGES:
            continue
        bad.append("未申报的跨层依赖 %s -> %s：%s"
                   % (a, b, "；".join(sorted(examples)[:4])))
    return bad


# ══════════════════════════════════════════════════════════════════ L5
def check_L5_service_layer_not_depended_on_from_below():
    bad = []
    for path in iter_py("shared/collect", "shared/project", "scheme1-kg/project",
                        "scheme2-onto/project"):
        for mod, lineno in imports_of(ast.parse(read(path))):
            if target_layer(mod) == "service":
                bad.append("%s:%d import %s（知识服务不得被下层调用，C2）"
                           % (rel(path), lineno, mod))
    return bad


# ══════════════════════════════════════════════════════════════════ L6
def check_L6_namespace_literals_live_in_mapping_only():
    bad = []
    for path in iter_py(*L6_SCAN_DIRS):
        code = strip_docstrings(read(path))
        for pat, why in OWN_NAMESPACE_LITERALS.items():
            for m in re.finditer(pat, code):
                line = code[:m.start()].count("\n") + 1
                bad.append("%s(AST 第 %d 行附近) 硬编码「%s」—— %s"
                           % (rel(path), line, m.group(0), why))
    return bad


# ══════════════════════════════════════════════════════════════════ L7
def check_L7_data_flow_is_one_way():
    bad = []
    forbidden = {("collect", "project"), ("project", "collect")}
    for (a, b), examples in sorted(cross_layer_edges().items()):
        if (a, b) in forbidden:
            bad.append("数据流反向：%s -> %s：%s" % (a, b, "；".join(sorted(examples)[:4])))
    return bad


CHECKS = (
    ("L1", "中性层 / 采集层不含载体驱动", check_L1_neutral_layers_avoid_carrier_drivers),
    ("L2", "shared/ 不 import 方案目录", check_L2_shared_never_imports_a_scheme),
    ("L3", "两个方案互不依赖", check_L3_schemes_are_mutually_independent),
    ("L4", "跨层依赖边全部已申报", check_L4_every_cross_layer_edge_is_declared),
    ("L5", "知识服务不被下层调用", check_L5_service_layer_not_depended_on_from_below),
    ("L6", "命名空间字面量只活在映射表", check_L6_namespace_literals_live_in_mapping_only),
    ("L7", "数据流单向", check_L7_data_flow_is_one_way),
)


def main():
    print("=" * 78)
    print("分层边界静态检查（硬约束 C2）  root=%s" % ROOT)
    print("=" * 78)
    n_bad = 0
    for code, title, fn in CHECKS:
        try:
            problems = fn()
        except Exception as exc:                       # fail-closed：检查自身炸了也算失败
            problems = ["检查自身异常：%r（源码不可解析或目录结构变化）" % (exc,)]
        n_bad += len(problems)
        print("[%s] %s —— %s" % (code, title, "OK" if not problems else
                                 "FAIL(%d)" % len(problems)))
        for p in problems:
            print("     %s" % p)
    print("-" * 78)
    print("已申报的跨层依赖边：")
    for (a, b) in sorted(ALLOWED_EDGES):
        print("  %s -> %s   %s" % (a, b, ALLOWED_EDGES[(a, b)]))
    print("-" * 78)
    print("RESULT: %s（L1–L7 共 7 项，问题 %d 条）"
          % ("PASS" if n_bad == 0 else "FAIL", n_bad))
    return 0 if n_bad == 0 else 1


# ── pytest 入口（同一套断言，便于并入 CI 的 pytest 阶段）────────────────
def test_L1(): assert not check_L1_neutral_layers_avoid_carrier_drivers()
def test_L2(): assert not check_L2_shared_never_imports_a_scheme()
def test_L3(): assert not check_L3_schemes_are_mutually_independent()
def test_L4(): assert not check_L4_every_cross_layer_edge_is_declared()
def test_L5(): assert not check_L5_service_layer_not_depended_on_from_below()
def test_L6(): assert not check_L6_namespace_literals_live_in_mapping_only()
def test_L7(): assert not check_L7_data_flow_is_one_way()


if __name__ == "__main__":
    sys.exit(main())
