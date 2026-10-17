# -*- coding: utf-8 -*-
"""M5 数据面封装层（S1）：采集 / 基底 / 注入 / 归一化 / 工作集 / 有界聚合。

本模块把数据面收成一个单文件、可单测的封装层，全部只读语义：

  1. ``fetch_live()``         真机采集入口（POST /tools/gather）—— 唯一会出网的函数；
  2. ``load_base()``          读入一份已冻结的基底，给出同源锚 ``canonical_sha256``；
  3. ``apply_injections()``   在**归一化之后**按用例差分扰动 CCF 工作集（fail-closed）；
  4. ``normalize()``          复用 ``shared/collect/freeze.py`` 的 ``build()`` 产出 CCF 三段；
  5. ``build_working_set()``  基底 →（覆盖闸门）→ 注入 → 工作集的纯函数，两次调用逐字相同；
  6. ``aggregate()``          把工作集压成有界表示，并把丢弃量原样回传。

设计约束（``tmp/_m5_dataplane_plan.md`` §2-§7）：

* 判决模式不联网：``--selftest`` / ``--equivalence`` 全程只走内存夹具与磁盘基底；
* 归一化层与 ``freeze.py`` 是**同一实现**（import 复用 ``build()``，不复制映射逻辑），
  因此不存在「两套归一化慢慢漂移」的风险；本模块只负责编排与准入；
* 注入寻址键 = ``(obj_type, obj_id, path)``，``obj_id`` 用 **CCF id**、``path`` 用
  **观测 metricCode**，**不使用数组下标/行号**（D44：注入面在归一化之后的 CCF 层，
  唯一顺序 = 基底 →（覆盖门禁只作用于基底）→ 注入 → 工作集，不保留 v1.1 的第二条顺序）；
* 基底不可变：注入只改工作集拷贝，``base["raw"]`` 永不就地修改；
* 无模块级可变缓存、无跨调用状态：任何 memo 都必须在调用方按
  ``(case_id, working_set_sha256)`` 持有，本模块不代持。

命令：
    python shared/collect/live_facts.py --selftest
    python shared/collect/live_facts.py --equivalence --base tmp/ccf_live5.json
    python shared/collect/live_facts.py --fetch --layers gaussdb,os --out tmp/gather_x.json
"""

from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

__all__ = [
    "LiveFactsError",
    "InjectionRefused",
    "BaseBlocked",
    "fetch_live",
    "load_base",
    "apply_injections",
    "normalize",
    "build_working_set",
    "aggregate",
    "coverage_evidence",
    "require_coverage_evidence",
    "working_set_diff",
    "verify_declared_diff",
    "refresh_ccf_integrity",
    "canonical_json",
    "sha256_text",
    "fingerprint_file",
    "selftest",
    "main",
]

# ----------------------------------------------------------------- 路径与常量

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
FREEZE_PATH = os.path.join(_HERE, "freeze.py")
MAP_PATH = os.path.join(ROOT, "shared", "model", "v3-ccf-map.yaml")
SCHEMA_PATH = os.path.join(ROOT, "shared", "model", "ccf.schema.json")
TOPO_PATH = os.path.join(ROOT, "shared", "model", "topology.yaml")

INPUT_FACTS_SCHEMA = "m5-input-facts/1"

#: ``fetch_live()`` 的默认采集面基址。
#:
#: ★ R-56（2026-10-12）：原值是**硬编码的另一台主机** ``http://192.0.2.10:8187``。
#:   容器化后 ``deploy/compose.yml`` 只给 live-facts 注入了 ``CC_GATHER_URL``
#:   （当时本函数**不读**这个名字），而 ``GDO_COLLECT_URL`` 为空
#:   ⇒ ``fetch_live`` **静默回落**到那台机器上的旧采集面（其库是备机
#:   ⇒ ``gaussdb.slow_sql`` 段必失败）⇒ ``read_facts`` 一律
#:   ``BLOCKED_BASE(409)``（带上游段错误）或第 3 次起 ``UPSTREAM_UNAVAILABLE(502)``
#:   （被上游 ``_loop_guard`` 熔断）。用户侧现象 = 「采集接口并未打通」。
#:   现改为与 ``freeze.py --collect-url`` / ``DEFAULT_PROVENANCE_URL`` 同一个保守默认
#:   （``127.0.0.1:8187``）：**「没配」只落到本机，绝不静默指向别的机器**。
#:   真正的部署取值由 ``collect_base_url()`` 从 env 读，默认值只在离线/单机直跑时生效。
DEFAULT_COLLECT_URL = "http://127.0.0.1:8187"

#: 采集面基址的环境变量名（**按序**取第一个非空值）。
#: ``GDO_COLLECT_URL`` 是本模块的历史名；``CC_GATHER_URL`` 是 ``deploy/compose.yml``
#: 实际注入给 live-facts 容器的那一个 —— 两边此前对不上，正是 R-56 的根因。
#: 两个名字都接受 ⇒ 契约名与部署名不再各说各话。
COLLECT_URL_ENV_VARS = ("GDO_COLLECT_URL", "CC_GATHER_URL")

#: ``normalize()`` 写进 CCF ``collector.collectUrl`` 的默认：与 freeze CLI 的默认逐字一致，
#: 这样在本地冻结出来的基底可以被逐字节复现。
DEFAULT_PROVENANCE_URL = os.environ.get("GDO_COLLECT_URL") or "http://127.0.0.1:8187"


def collect_base_url(url=None, environ=None):
    """解析采集面**基址**：显式入参 > 环境变量（``COLLECT_URL_ENV_VARS`` 依序）> 默认值。

    ★ 归一化（R-56）：部署侧给的可能已经是**完整端点**
      （``http://collect:8187/tools/gather``），而调用方要的是基址
      （本模块自己会拼 ``/tools/gather``）⇒ 统一剥掉 ``/tools/gather`` / ``/tools``
      与尾部斜杠，避免拼成 ``.../tools/gather/tools/gather``（那会静默变成 404，
      而不是一个响亮的失败）。
    """
    env = os.environ if environ is None else environ
    raw = url
    if not raw:
        for name in COLLECT_URL_ENV_VARS:
            value = (env.get(name) or "").strip()
            if value:
                raw = value
                break
    base = str(raw or DEFAULT_COLLECT_URL).strip().rstrip("/")
    for suffix in ("/tools/gather", "/tools"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
            break
    return base


DEFAULT_LAYERS = ("gaussdb", "os")
AGG_CAP_VALUES, AGG_CAP_OBJS, AGG_MAX_STR = 8, 4, 120

_MISSING = object()

_DEFAULT_HEALTH = {
    "collect_source": {"collect_source": "live", "simulated": False, "unmatched_exits": []},
    "gaussdb": {"ok": True},
}


class LiveFactsError(Exception):
    """本模块的 fail-closed 异常基类。"""


class InjectionRefused(LiveFactsError):
    """注入被拒绝：``value_before`` 与基底取值不一致，或寻址不到目标（fail-closed）。"""


class BaseBlocked(LiveFactsError):
    """基底进不了判决模式（覆盖证据缺失/不自洽）⇒ 该例/该批 **BLOCKED**（D44，fail-closed）。

    覆盖门禁只作用于**基底**：基底必须自带「覆盖已通过」的证据（``freeze.py`` 写出的
    ``integrity.expectedCoverage``）才能继续；缺证据就 BLOCKED，**不得**用 ``--force-write``
    之类的开关绕过（覆盖的权威判据永远是 ``shared/collect/freeze.py``）。
    """


# --------------------------------------------------- CCF 层寻址与注入声明表（D44）
#
# 全部是**声明式数据**：不按命名习惯猜类型、不用数组下标/行号寻址。
# D44（``tmp/_m5_dataplane_plan.md`` §4/§14）：注入面已下沉到**归一化之后**，
# 寻址键 = ``(obj_type, obj_id, path)``，其中 ``obj_id`` 是 **CCF id**（``prefix:local``，
# 与用例 ``expect_objects`` 同一 id 空间），``path`` 是 CCF 观测的 ``metricCode``。
# 「采集层原生载体」这套概念随 v1.1 的顺序一起删除，本模块不再保留。

#: CCF typeCode -> layerCode（照真实基底各类型实测值；schema ``$defs.layerCode.enum``
#: 只有 App / Runtime / Data 三类）。
TYPE_LAYER = {
    "CreditCardApp": "App",
    "AppInstance": "App",
    "AppEndpoint": "App",
    "JdbcDataSource": "App",
    "Container": "Runtime",
    "Host": "Runtime",
    "GaussDBCluster": "Data",
    "GaussDBInstance": "Data",
    "DatabaseSessionPool": "Data",
    "Session": "Data",
    "Query": "Data",
    "Table": "Data",
}

#: CCF typeCode -> 承载该对象「局部 id」的 key 字段。值为 ``None`` ⇒ 铸 id **必须**显式给
#: ``id_local``：真实基底里 AppEndpoint 的 key 只有 path/protocol、Table 只有
#: schemaName/tableName、CreditCardApp 只有 appCode，光凭 key 推不出 id，宁可 fail-closed 也不猜。
LOCAL_ID_FROM_KEY = {
    "CreditCardApp": "appCode",
    "AppInstance": "instanceId",
    "AppEndpoint": None,
    "JdbcDataSource": "dsCode",
    "Container": "containerId",
    "Host": "hostname",
    "GaussDBCluster": "clusterName",
    "GaussDBInstance": "instanceId",
    "DatabaseSessionPool": "poolId",
    "Session": "sessionId",
    "Query": "queryId",
    "Table": None,
}

#: 观测里代表「事实取值」的字段（改值只改这些；``status`` 走单独一套准入）。
VALUE_FIELDS = ("value", "valueText")

#: schema ``$defs.observation`` 的 status 枚举（照抄，不自己发明取值）。
STATUS_ENUM = ("collected", "notCollected", "collectedEmpty")

#: 覆盖证据必须齐备的键（``freeze.py:1341-1347`` 写出的就是这些）。
COVERAGE_KEYS = ("ok", "missingTypes", "missingMetrics", "underMinTypes")

#: 注入 op 名。``set_fact`` 覆盖「事实值」与「状态」两类，靠 ``field`` 区分；
#: 其余四个是对象/关系存在性。
INJECTION_OP_NAMES = ("set_fact", "remove_object", "add_object", "remove_relation", "add_relation")



# ----------------------------------------------------------------- 摘要与规范化工具


def sha256_text(s):
    """与 ``freeze.sha256_text`` 逐字等价（utf-8 编码后取 sha256）。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def canonical_json(obj):
    """确定性 JSON：键排序 + 紧凑分隔符 + 不转义非 ASCII。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _read_text_utf8(path):
    """按 freeze 的读法读文本（``io.open(..., "r")`` 会做换行归一）。"""
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fingerprint_file(path):
    """返回 ``{bytes, lf, sha256}``；``lf`` 是字节级数 ``0x0A``（不用行数）。"""
    with open(path, "rb") as f:
        blob = f.read()
    return {"bytes": len(blob), "lf": blob.count(b"\n"), "sha256": hashlib.sha256(blob).hexdigest()}


def _load_freeze():
    """import 现役 ``freeze.py``（复用，不复制它的映射逻辑）。"""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import freeze  # noqa: PLC0415  (延迟导入：不在 import 期产生副作用)

    return freeze


def _load_equivalence():
    _load_freeze()
    import equivalence  # noqa: PLC0415

    return equivalence


def _dump_temp_json(obj, tmpdir, name):
    """把对象写进系统临时目录（**绝不写仓库树**），返回文件路径。"""
    path = os.path.join(tmpdir, name)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
    return path


# ----------------------------------------------------------------- 1) 真实采集


def fetch_live(layers, sections=None, url=None, timeout=60.0):
    """``POST {base}/tools/gather``，返回**原始信封**（不做裁剪、不做归一化）。

    这是本模块唯一会出网的函数。**只读采集**：请求体与 freeze.py:1103 逐字一致，
    不携带任何写操作字段。

    ★ R-56（2026-10-12）两处修正，均由「Dify 上 read_facts 恒报不可用」暴露：

    ① **基址**取值链与归一化交给 :func:`collect_base_url`
       （``GDO_COLLECT_URL`` **或** ``CC_GATHER_URL``；完整端点也接受），
       默认值不再指向别的机器。
    ② **发** ``X-GDO-Internal`` 头：采集面的 ``_loop_guard``（``common.py``，窗口 600s）
       把「同参数连续调用第 3 次起」判成 LLM 循环，**丢弃数据体**换成
       ``{"loop_detected": true, ...}``。而本函数在运行模式下**每次都用完全相同的
       参数**调 gather —— 这正是 read_facts 的契约（每次请求真采一次，无跨请求状态）
       ⇒ 第 3 次起必然被熔断，read_facts 恒 502 ``UPSTREAM_UNAVAILABLE``。

       该头是采集面**自带的**服务间调用豁免（``install_internal_call_middleware``），
       源码注释对其目的的表述逐字就是「护栏的本意是防 **LLM** 循环，
       不应拦服务间确定性调用」—— live-facts 正是这种确定性调用方，且**不在 LLM 工具面上**：
       LLM 经 :8301/:8302/:8303 门面只能提供 JSON 入参，**无法自设 HTTP 头**。
       ⇒ 本豁免**不削弱任何 LLM 循环保护** —— 那条保护在编排层（Dify 的迭代上限）
          与门面自身的端点上，不在这条「live-facts → collect」的内部链路上。
       ★ 由此**收窄** docs/02 的 H16 结论：H16 反对的是「本项目**各服务普遍**沿用该头」
         （那才会把门面的 LLM 循环也一起放过）；此处仅**唯一一处**内部取数轮询使用它。
    """
    base = collect_base_url(url)

    if isinstance(layers, str):
        layers = [x.strip() for x in layers.split(",") if x.strip()]
    layers = [str(x) for x in (layers or [])]
    if not layers:
        raise LiveFactsError("fetch_live 需要至少一个 layer")
    payload = {
        "layers": layers,
        "sections": list(sections or []),
        "verify_sql": "",
        "cluster_oid": "",
        "instance_id": "",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + "/tools/gather",
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "m5-live-facts/1.0",
            # ★ R-56：声明「服务间确定性调用」⇒ 采集面 _loop_guard 不介入
            #   （不计同参数次数、不改写数据体）。理由与边界见本函数 docstring。
            "X-GDO-Internal": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise LiveFactsError("HTTP %s %s/tools/gather：%s" % (exc.code, base, body))
    except urllib.error.URLError as exc:
        raise LiveFactsError("无法连接 %s/tools/gather：%s" % (base, exc))
    try:
        envelope = json.loads(text)
    except ValueError as exc:
        raise LiveFactsError("采集回执不是合法 JSON：%s" % exc)
    _require_envelope(envelope, where="fetch_live")
    return envelope


def _require_envelope(envelope, where):
    if not isinstance(envelope, dict):
        raise LiveFactsError("%s：采集回执不是对象（%s）" % (where, type(envelope).__name__))
    if envelope.get("mode") != "probes_v6" or "all_facts" not in envelope:
        raise LiveFactsError(
            "%s：回执不是采集面正常响应（可能被 _loop_guard 熔断替换）：mode=%r keys=%s"
            % (where, envelope.get("mode"), sorted(envelope)[:14])
        )
    if not isinstance(envelope.get("all_facts"), list):
        raise LiveFactsError("%s：all_facts 不是列表" % where)


# ----------------------------------------------------------------- 2) 基底读入


def _ref_get(ref_doc, keys):
    """从参照 CCF 里按嵌套键取值；``ref_doc`` 为 None 或缺键 ⇒ None。"""
    cur = ref_doc
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def load_base(path, expected_profile=None, health_path=None, snapshot_id=None,
              collect_url=None, upstream=None, parallel=None, ref=None):
    """读入一份已冻结的基底。

    支持三种落点：

    * **CCF JSON**（含 ``objects`` + ``integrity``）：``canonical_sha256`` 取自
      ``integrity.canonicalSha256``，并用 ``equivalence.canonical_snapshot_sha`` 复算校验
      （不一致 ⇒ fail-closed，基底已被改动）；
    * **/tools/gather 原始信封**（含 ``all_facts``）：``raw`` 可用，并**当场归一化成 CCF**
      （``ccf`` 非空）——D44 之后注入面在 CCF 层，``raw`` 只用于归一化，不再直接注入。
      ``canonical_sha256`` 由本模块现算，而 ``snapshotId`` / ``capturedAt`` / ``collector`` /
      ``payloadPolicy`` 全都**参与**等价性自检，所以想与既有冻结基底的同源锚**逐字对齐**，必须给出
      ``ref``（指向那份既有 CCF：``snapshotId``、``collector.collectUrl`` /
      ``collector.upstream`` / ``collector.layers``、``payloadPolicy.parallel`` 全部从它继承）
      以及 ``health_path``（指回同一份 health 回执）。逐个显式给
      ``snapshot_id`` / ``collect_url`` / ``upstream`` / ``parallel`` / ``layers`` 也等价。
      完全不给时改用内容摘要派生一个确定性 id —— 重建仍然纯函数，但锚值不会等于任何既有基底。
      **已知陷阱**：``/tools/gather`` 信封自带的 ``layers`` 是**请求里的层列表**
      （实测为 ``['gaussdb', 'os', 'container', 'app']``），而既有基底冻结时用的是
      ``--layers gaussdb,os``（``collector.layers = ['gaussdb','os']``）⇒ 只跟 ``ref`` 对齐
      `snapshotId`/`parallel` 而漏掉 ``layers``，锚值仍会不同且**不会报错**。想逐字对齐一律用
      ``ref=``；判决模式下由 ``m5-input-facts`` 的 ``meta.canonical_sha256`` 兜底复核（不一致即拒绝）；
    * **``m5-input-facts/1`` YAML**：``meta.base_ref`` 指向上述任一种，``meta.snapshot_id`` /
      ``meta.captured_at`` / ``meta.canonical_sha256`` 逐个下传并复核（不一致 ⇒ fail-closed），
      ``cases`` 原样带回。

    返回 ``{kind, path, fingerprint, canonical_sha256, snapshot_id, raw, ccf, cases, ...}``。
    """
    apath = os.path.abspath(path)
    if not os.path.isfile(apath):
        raise LiveFactsError("基底不存在：%s" % apath)
    fp = fingerprint_file(apath)
    ext = os.path.splitext(apath)[1].lower()

    if ext in (".yaml", ".yml"):
        doc = _load_yaml(apath)
        if not isinstance(doc, dict) or doc.get("schema") != INPUT_FACTS_SCHEMA:
            raise LiveFactsError(
                "YAML 基底的 schema 必须是 %r，实为 %r" % (INPUT_FACTS_SCHEMA, (doc or {}).get("schema"))
            )
        meta = doc.get("meta") or {}
        base_ref = meta.get("base_ref") or meta.get("ref")
        if not base_ref:
            raise LiveFactsError("m5-input-facts 缺 meta.base_ref：无法定位来源快照")
        ref_path = base_ref if os.path.isabs(base_ref) else os.path.join(os.path.dirname(apath), base_ref)
        inner = load_base(
            os.path.normpath(ref_path),
            expected_profile=expected_profile,
            health_path=health_path or meta.get("health_ref"),
            snapshot_id=snapshot_id or meta.get("snapshot_id"),
            collect_url=collect_url or meta.get("collect_url"),
            upstream=upstream or meta.get("upstream"),
            parallel=parallel if parallel is not None else meta.get("parallel"),
        )
        inner = dict(inner)
        inner["kind"] = "m5-input-facts"
        inner["path"] = apath
        inner["fingerprint"] = fp
        inner["cases"] = list(doc.get("cases") or [])
        inner["declared_canonical_sha256"] = meta.get("canonical_sha256")
        if meta.get("canonical_sha256") and meta["canonical_sha256"] != inner["canonical_sha256"]:
            raise LiveFactsError(
                "m5-input-facts 声明的 canonical_sha256=%s 与来源快照实算 %s 不一致（fail-closed）"
                % (meta.get("canonical_sha256"), inner["canonical_sha256"])
            )
        if meta.get("snapshot_id") and meta["snapshot_id"] != inner["snapshot_id"]:
            raise LiveFactsError(
                "m5-input-facts 声明的 snapshot_id=%s 与来源快照 %s 不一致（fail-closed）"
                % (meta.get("snapshot_id"), inner["snapshot_id"])
            )
        if expected_profile:
            inner["expected_profile"] = expected_profile
        return inner

    doc = _read_json(apath)
    if not isinstance(doc, dict):
        raise LiveFactsError("基底不是 JSON 对象：%s" % apath)

    if "all_facts" in doc and "objects" not in doc:
        _require_envelope(doc, where="load_base")
        prof = expected_profile or "l3-only"
        ref_doc = None
        if ref is not None:
            rpath = ref if os.path.isabs(ref) else os.path.abspath(ref)
            if not os.path.isfile(rpath):
                raise LiveFactsError("ref 指向的参照基底不存在：%s" % rpath)
            ref_doc = _read_json(rpath)
            if not isinstance(ref_doc, dict) or "objects" not in ref_doc or "integrity" not in ref_doc:
                raise LiveFactsError(
                    "ref 必须是既有 CCF（含 objects+integrity）才能继承 provenance：%s" % rpath
                )
        snap = snapshot_id or _ref_get(ref_doc, ("snapshotId",)) or _derive_snapshot_id(doc)
        url = collect_url or _ref_get(ref_doc, ("collector", "collectUrl")) or DEFAULT_PROVENANCE_URL
        up = upstream or _ref_get(ref_doc, ("collector", "upstream")) or _load_freeze().UPSTREAM
        lyr = _ref_get(ref_doc, ("collector", "layers")) or doc.get("layers") or list(DEFAULT_LAYERS)
        par = parallel if parallel is not None else _ref_get(ref_doc, ("payloadPolicy", "parallel"))
        ccf, _ = _normalize_full(
            doc,
            snapshot_id=snap,
            captured_at=doc.get("collected_at"),
            expected_profile=prof,
            collect_url=url,
            upstream=up,
            layers=lyr,
            parallel=par,
            health_path=health_path,
        )
        return {
            "kind": "gather-envelope",
            "path": apath,
            "fingerprint": fp,
            "canonical_sha256": ccf["integrity"]["canonicalSha256"],
            "snapshot_id": snap,
            "raw": doc,
            "ccf": ccf,
            "cases": [],
            "expected_profile": prof,
            "collect_url": url,
            "upstream": up,
            "layers": list(lyr),
            "parallel": par_flag(par),
            "health_path": health_path,
        }

    if "objects" not in doc or "integrity" not in doc:
        raise LiveFactsError("基底既不是采集信封也不是 CCF（缺 all_facts / objects+integrity）：%s" % apath)
    declared = (doc.get("integrity") or {}).get("canonicalSha256")
    if not declared:
        raise LiveFactsError("CCF 基底缺 integrity.canonicalSha256：无法作为同源锚")
    eq = _load_equivalence()
    actual = eq.canonical_snapshot_sha(copy.deepcopy(doc))
    if actual != declared:
        raise LiveFactsError(
            "CCF 基底的 canonicalSha256 复算不一致（fail-closed，基底已被改动）："
            "声明值=%s 实算值=%s" % (declared, actual)
        )
    collector = doc.get("collector") or {}
    return {
        "kind": "ccf",
        "path": apath,
        "fingerprint": fp,
        "canonical_sha256": declared,
        "snapshot_id": doc.get("snapshotId"),
        "raw": None,
        "ccf": doc,
        "cases": [],
        "expected_profile": expected_profile,
        "collect_url": collector.get("collectUrl") or DEFAULT_PROVENANCE_URL,
        "upstream": collector.get("upstream") or _load_freeze().UPSTREAM,
        "layers": list(collector.get("layers") or DEFAULT_LAYERS),
        "parallel": bool((doc.get("payloadPolicy") or {}).get("parallel")),
        "health_path": None,
    }


def _load_yaml(path):
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise LiveFactsError("需要 PyYAML 才能读 %s：%s" % (path, exc))
    with io.open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _derive_snapshot_id(raw):
    """原始信封没有 snapshotId ⇒ 用内容摘要派生一个**确定性** id（同输入同 id）。"""
    stamp = str(raw.get("collected_at") or "")
    compact = stamp.replace("-", "").replace(":", "").replace(".", "").replace("+", "")[:15]
    return "ccf-%sZ-%s" % (compact or "unknown", sha256_text(canonical_json(raw))[:12])


# ----------------------------------------------------------------- 3) 注入层（CCF 层，D44）
#
# ★ D44（``tmp/_m5_dataplane_plan.md`` §4/§14）：注入面**下沉到归一化之后**。
#   唯一顺序 = ``基底 CCF →（覆盖门禁只作用于基底）→ 注入（CCF 层差分）→ 工作集``；
#   v1.1 的「注入在归一化之前」已被取代，本模块**不保留**第二条顺序
#   （把采集层原生事实交给 ``apply_injections()`` 会被明确拒绝，见 ``_as_ccf_doc()``）。
#
# 三类注入面，寻址键 ``(obj_type, obj_id, path)``：
#   ① 事实值/状态：``target{obj_type,obj_id,path}`` + ``field``(value|valueText|status)
#      + ``value_before`` + ``value_after``（``field`` 省略时默认 ``value``；
#      给了 ``status_before``/``status_after`` 或 ``field="status"`` 即走状态面）；
#   ② 对象存在性：``op="remove_object" | "add_object"``；
#   ③ 关系存在性：``op="remove_relation" | "add_relation"``。
#
# 传播规则（显式声明、逐条可追溯，D44 §4）：
#   * 删对象 ⇒ 连带删该对象名下的观测 + 以它为端点的关系（按 CCF id 判端点）；
#   * 删关系 ⇒ 只删该关系；改值/状态 ⇒ 只改该观测的声明字段；
#   * 新增对象 ⇒ 按 ``ccf.schema.json#x-id-prefix-by-type`` 铸 ``prefix:local``，
#     **不隐式派生任何关系**（需要边就显式再加一条 ``add_relation``）；
#   * 任何注入都**不改写既存对象的字节**（``verify_declared_diff()`` 机检 ``*_changed`` 为空）。
#
# 三道 fail-closed 闸门（固定顺序）：
#   1) 覆盖证据闸门：只作用于**基底**（``require_coverage_evidence()``，权威判据是 freeze.py）；
#   2) ``value_before`` 闸门：与基底取值逐字不符 ⇒ 拒，并打印「期望值 vs 基底实际值」；
#   3) 差分闸门：工作集必须逐字等于「基底 + 已声明差分」，出现未声明差分 ⇒ 拒。


def _load_schema():
    """现读 ``ccf.schema.json``（不做模块级缓存：避免跨调用状态，见文件头约束）。"""
    return _read_json(SCHEMA_PATH)


def _schema_prefixes(schema=None):
    """``x-id-prefix-by-type``：铸 id 的唯一权威来源。"""
    return dict((schema if schema is not None else _load_schema()).get("x-id-prefix-by-type") or {})


def _schema_enum(name, sub=None, schema=None):
    """取 ``$defs.<name>`` 下的枚举；``sub`` 支持 ``properties.source`` 这类下钻。"""
    sch = schema if schema is not None else _load_schema()
    node = (sch.get("$defs") or {}).get(name) or {}
    for part in ((sub or "").split(".") if sub else []):
        node = (node or {}).get(part) if isinstance(node, dict) else None
    if isinstance(node, dict) and isinstance(node.get("enum"), list):
        return list(node["enum"])
    return []


def _object_id_pattern(schema=None):
    """``$defs.objectId.pattern``（铸出来的 id 必须过它，否则整份工作集过不了 schema）。"""
    sch = schema if schema is not None else _load_schema()
    node = (sch.get("$defs") or {}).get("objectId") or {}
    pat = node.get("pattern")
    if not pat and isinstance(node, dict):
        pat = (node.get("properties") or {}).get("pattern")
    return pat or r"^[a-z]+:[A-Za-z0-9._:-]+$"


def _fmt(value):
    """把任意取值渲染成一行稳定文本（fail-closed 报错要逐字打印双方取值）。"""
    if value is _MISSING:
        return "<缺失>"
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        return repr(value)


def _refuse_value_before(target, field, expected, actual, extra=""):
    """``value_before`` 与基底取值不符 ⇒ fail-closed，且消息里必须同时出现期望值与基底实际值。"""
    raise InjectionRefused(
        "注入被拒（fail-closed）：value_before 与基底取值不一致。\n"
        "  寻址 = (obj_type=%s, obj_id=%s, path=%s)\n"
        "  字段 = %s\n"
        "  期望值(value_before) = %s\n"
        "  基底实际值             = %s%s"
        % ((target or {}).get("obj_type"), (target or {}).get("obj_id"), (target or {}).get("path"),
           field, _fmt(expected), _fmt(actual), ("\n  " + extra) if extra else "")
    )


def _addr_of(inj, idx):
    """解析寻址块 ``target{obj_type,obj_id,path}``（``type``/``id``/``metricCode`` 作同义键）。"""
    target = inj.get("target")
    if not isinstance(target, dict):
        raise InjectionRefused(
            "第 %d 条注入缺 target{obj_type,obj_id,path}：寻址键必须用 CCF id，"
            "不得用数组下标/行号" % idx)
    obj_type = target.get("obj_type", target.get("type"))
    obj_id = target.get("obj_id", target.get("id"))
    path = target.get("path", target.get("metricCode"))
    if not obj_type or not obj_id:
        raise InjectionRefused("第 %d 条注入的 target 缺 obj_type/obj_id：%s" % (idx, _fmt(target)))
    return {"obj_type": obj_type, "obj_id": obj_id, "path": path}


def _type_matches(given, obj_type, prefix):
    """``target.obj_type`` 允许写 CCF typeCode（Container）或 schema 前缀（ctr）。"""
    return str(given) in (str(obj_type), str(prefix))


def _resolve_object(doc, target, where="注入"):
    """按 CCF id 定位对象，并核验 ``obj_type``；寻址不到或有歧义 ⇒ fail-closed。"""
    objects = doc.get("objects") or []
    prefixes = _schema_prefixes()
    wanted = str(target.get("obj_id"))
    cands = [o for o in objects if str(o.get("id")) == wanted]
    if not cands:
        cands = [o for o in objects if str(o.get("id", "")).endswith(":" + wanted)]
    if len(cands) > 1:
        raise InjectionRefused(
            "%s：obj_id=%r 匹配到 %d 个对象，要求收窄到唯一 CCF id：%s"
            % (where, wanted, len(cands), [o.get("id") for o in cands][:6]))
    if not cands:
        raise InjectionRefused(
            "%s：寻址不到任何对象 (obj_type=%s, obj_id=%s)；CCF 里可用 id 前 8 个 = %s"
            % (where, target.get("obj_type"), wanted, [o.get("id") for o in objects][:8]))
    obj = cands[0]
    prefix = prefixes.get(obj.get("type"))
    if not _type_matches(target.get("obj_type"), obj.get("type"), prefix):
        raise InjectionRefused(
            "%s：obj_type 不符。期望(用例) = %r，基底实际 = %r（schema 前缀 %r）；obj_id=%s"
            % (where, target.get("obj_type"), obj.get("type"), prefix, obj.get("id")))
    return obj


def _resolve_observations(doc, target, where="注入"):
    """定位 ``(对象, metricCode)`` 命中的观测；一条都没有 ⇒ fail-closed。"""
    obj = _resolve_object(doc, target, where)
    oid = obj["id"]
    path = target.get("path")
    own = [o for o in (doc.get("observations") or []) if o.get("objectId") == oid]
    if path is None:
        raise InjectionRefused(
            "%s：target 缺 path（= 观测的 metricCode），CCF 层无法定位观测；对象 %s 名下有 %d 条观测"
            % (where, oid, len(own)))
    hits = [o for o in own if o.get("metricCode") == path]
    if not hits:
        raise InjectionRefused(
            "%s：寻址不到任何观测 (objectId=%s, metricCode=%s)；该对象名下的 metricCode = %s"
            % (where, oid, path, sorted({o.get("metricCode") for o in own})[:12]))
    return obj, hits


# ---------------------------------------------------------------- 覆盖证据（只作用于基底）

def coverage_evidence(doc):
    """读基底**自带**的覆盖证据（``integrity.expectedCoverage``）并给出机检读数。

    覆盖的**权威判据永远是 ``shared/collect/freeze.py``**（``:1334`` 只认 collected、
    ``:1339`` 才算 relationsMin、``:1342`` 汇总 ``ok``）；本函数只判「证据在不在、
    自不自洽」，不重算覆盖（重算是 verify 侧的事，本模块不改 verify 侧）。
    """
    out = {
        "present": False, "ok": None, "missing": [], "problems": [],
        "coverage": None, "counts": None, "counts_consistent": None,
        "expected_types": 0, "expected_metrics": 0,
        "relations_min_present": False, "relations_min_keys": [],
    }
    integ = doc.get("integrity") if isinstance(doc, dict) else None
    if not isinstance(integ, dict):
        out["missing"].append("integrity")
        out["problems"].append("基底没有 integrity 块（CCF 必须是 freeze.build 写出的）")
        return out
    exp = doc.get("expected") if isinstance(doc.get("expected"), dict) else {}
    if not (exp.get("types") or exp.get("metrics")):
        out["missing"].append("expected.types/metrics")
        out["problems"].append("基底 expected 声明为空（x-ccf-rules R7：空 expected 视为无效快照）")
    out["expected_types"] = len(exp.get("types") or [])
    out["expected_metrics"] = len(exp.get("metrics") or [])
    rel_min = exp.get("relationsMin")
    out["relations_min_present"] = bool(rel_min)
    out["relations_min_keys"] = sorted(str(k) for k in (rel_min or {}))
    ec = integ.get("expectedCoverage")
    if not isinstance(ec, dict):
        out["missing"].append("integrity.expectedCoverage")
        out["problems"].append("基底没有 integrity.expectedCoverage：覆盖证据缺失 ⇒ 判决模式 BLOCKED")
        return out
    out["present"] = True
    for key in COVERAGE_KEYS:
        if key not in ec:
            out["missing"].append("integrity.expectedCoverage.%s" % key)
    if rel_min and "underMinRelations" not in ec:
        out["missing"].append("integrity.expectedCoverage.underMinRelations")
    if ec.get("ok") is not True:
        out["problems"].append("integrity.expectedCoverage.ok=%r（不是逐字的 true）" % (ec.get("ok"),))
    empty = []
    for key in ("missingTypes", "missingMetrics", "underMinTypes"):
        val = ec.get(key)
        if val:
            empty.append("%s=%s" % (key, _fmt(val)))
    if empty:
        out["problems"].append("覆盖证据的缺失清单非空：%s" % "；".join(empty))
    if ec.get("ok") is True and empty:
        out["problems"].append("覆盖证据自相矛盾：ok=true 但缺失清单非空")
    out["ok"] = ec.get("ok")
    out["coverage"] = {k: ec.get(k) for k in COVERAGE_KEYS + ("underMinRelations",)}
    counts = integ.get("counts") or {}
    out["counts"] = counts
    actual = {"objects": len(doc.get("objects") or []),
              "relations": len(doc.get("relations") or []),
              "observations": len(doc.get("observations") or [])}
    bad = ["%s=%s≠%d" % (k, counts.get(k), n) for k, n in sorted(actual.items()) if counts.get(k) != n]
    if bad:
        out["counts_consistent"] = False
        out["problems"].append("integrity.counts 与实际条数不符：%s" % ", ".join(bad))
    else:
        out["counts_consistent"] = True
    return out


def require_coverage_evidence(doc, where="判决模式"):
    """覆盖证据闸门：证据缺失/不自洽 ⇒ ``BaseBlocked``（BLOCKED，不得用 force_write 绕过）。"""
    ev = coverage_evidence(doc)
    if not ev["present"] or ev["missing"] or ev["problems"] or ev["ok"] is not True:
        raise BaseBlocked(
            "%s：基底覆盖证据不成立 ⇒ 该例/该批 BLOCKED（D44，fail-closed）。\n"
            "  缺失字段 = %s\n  问题 = %s\n"
            "  expected.types=%d expected.metrics=%d relationsMin=%s counts=%s\n"
            "  覆盖的权威判据 = shared/collect/freeze.py:1334-1362"
            "（覆盖门禁只作用于基底，不得用 --force-write 绕过）"
            % (where, ev["missing"] or "-", "；".join(ev["problems"]) or "-",
               ev["expected_types"], ev["expected_metrics"],
               ev["relations_min_keys"] or "-", _fmt(ev["counts"])))
    return ev


# ---------------------------------------------------------------- 差分：基底 -> 工作集

def _by_id(items):
    return {str(it.get("id")): it for it in (items or [])}


def working_set_diff(base_doc, ws_doc):
    """机检工作集相对基底**实际**发生了哪些差分（逐类列 增/删/改）。"""
    actual = {}
    for cls in ("objects", "relations", "observations"):
        b = _by_id(base_doc.get(cls))
        w = _by_id(ws_doc.get(cls))
        actual[cls + "_removed"] = sorted(set(b) - set(w))
        actual[cls + "_added"] = sorted(set(w) - set(b))
        changed = {}
        for oid in sorted(set(b) & set(w)):
            if canonical_json(b[oid]) != canonical_json(w[oid]):
                keys = sorted(set(b[oid]) | set(w[oid]))
                changed[oid] = [k for k in keys
                                if canonical_json(b[oid].get(k)) != canonical_json(w[oid].get(k))]
        actual[cls + "_changed"] = changed
    return actual


def _declared_empty():
    return {"objects_removed": [], "objects_added": [],
            "relations_removed": [], "relations_added": [],
            "observations_removed": [], "observations_added": [],
            "observations_changed": {}}


def _merge_declared(dst, src):
    """把逐条注入的「声明差分」并进总账（去重、保序，最后统一排序）。"""
    for key, val in (src or {}).items():
        if key == "observations_changed":
            for oid, fields in (val or {}).items():
                cur = dst.setdefault(key, {}).setdefault(oid, [])
                for f in fields:
                    if f not in cur:
                        cur.append(f)
        else:
            cur = dst.setdefault(key, [])
            for item in (val or []):
                if item not in cur:
                    cur.append(item)
    return dst


def _diff_counts(declared):
    d = declared or {}
    return {"objects_removed": len(d.get("objects_removed") or []),
            "objects_added": len(d.get("objects_added") or []),
            "relations_removed": len(d.get("relations_removed") or []),
            "relations_added": len(d.get("relations_added") or []),
            "observations_removed": len(d.get("observations_removed") or []),
            "observations_added": len(d.get("observations_added") or []),
            "observations_changed": len(d.get("observations_changed") or {})}


def verify_declared_diff(base_doc, ws_doc, declared, label="working-set"):
    """机检两条 D44 性质：①活着的对象字节逐字不变；②工作集 = 基底 + 已声明差分。

    顺带用 ``equivalence.check_snapshot`` 机检工作集本身（计数/引用完整性/覆盖/枚举），
    并把错误码回传；**只允许覆盖族 {X8-coverage, G5-coverage} 出错**——覆盖描述的是
    「采集完整性」，不是场景属性，所以场景把它打穿是允许的，但其它任何错误都是实现缺陷。

    观测字段的比对是**单向严格**的：工作集里任何实际改写过的字段都必须在声明里（否则
    「未声明差分」）。反向不成立——同一个 case 里的多条注入可以合法抵消（例如先把某观测
    翻成非 collected、再接一条把状态与取值复原的注入），于是「声明过但最终与基底逐字相同」
    是允许的，记入返回值 ``canceled_fields`` 供审计，不算失败。对象/关系的**增删**仍严格
    双向比对：删了但还在、声明删了却仍在，都说明实现没落地。
    """
    actual = working_set_diff(base_doc, ws_doc)
    problems = []
    for cls in ("objects", "relations"):
        if actual[cls + "_changed"]:
            problems.append("未声明差分：既存 %s 的字节被改写（D44 禁止改存活对象）：%s"
                            % (cls, _fmt(actual[cls + "_changed"])[:240]))
    for key in ("objects_removed", "objects_added", "relations_removed", "relations_added",
                "observations_removed", "observations_added"):
        declared_set = set(declared.get(key) or [])
        actual_set = set(actual.get(key) or [])
        for item in sorted(actual_set - declared_set):
            problems.append("未声明差分：%s 里出现 %s" % (key, item))
        for item in sorted(declared_set - actual_set):
            problems.append("声明了但未落地：%s 里的 %s" % (key, item))
    dec_changed = declared.get("observations_changed") or {}
    act_changed = actual.get("observations_changed") or {}
    for oid, fields in sorted(act_changed.items()):
        if oid not in dec_changed:
            problems.append("未声明差分：观测 %s 的字段 %s 被改写" % (oid, fields))
            continue
        extra = [f for f in fields if f not in dec_changed[oid]]
        if extra:
            problems.append("未声明差分：观测 %s 的字段 %s 被改写（声明里只有 %s）"
                            % (oid, extra, dec_changed[oid]))
    canceled = {}
    for oid, fields in sorted(dec_changed.items()):
        if oid not in act_changed:
            canceled[oid] = list(fields)
            continue
        gone = [f for f in fields if f not in act_changed[oid]]
        if gone:
            canceled[oid] = gone
    ref_int = (ws_doc.get("integrity") or {}).get("referenceIntegrity") or {}
    if ref_int.get("ok") is not True:
        problems.append("工作集出现悬空引用（端点/观测引用必须都存在）：%s" % _fmt(ref_int))
    eq = _load_equivalence()
    mark = len(eq.F)
    eq.check_snapshot(ws_doc, label, eq.load_schema(SCHEMA_PATH), False)
    entries = eq.F[mark:]
    codes = sorted({c for (sev, c, _msg) in entries if sev == "ERROR"})
    messages = [msg for (sev, c, msg) in entries if sev == "ERROR"]
    outside = [c for c in codes if c not in ("X8-coverage", "G5-coverage")]
    if outside:
        problems.append("工作集过不了 schema 机检：错误码 %s（只允许覆盖族 X8-coverage/G5-coverage）"
                        "\n    " + "\n    ".join(messages[:6]))
    if problems:
        raise InjectionRefused("工作集不是「基底 + 已声明差分」（fail-closed，D44）：\n  - "
                               + "\n  - ".join(problems[:12]))
    return {"actual": actual, "codes": codes, "messages": messages, "schema_clean": not codes,
            "canceled_fields": canceled}


def refresh_ccf_integrity(doc):
    """把**工作集副本**的 ``integrity`` 重算成自洽的（基底永远只读，本函数不得作用于基底）。

    公式照 ``freeze.py:1300-1362`` 对齐（覆盖仍是 collected-only + relationsMin），
    这样 ``check_snapshot`` 的计数/引用完整性/覆盖三族才对工作集成立。返回机检读数。
    """
    eq = _load_equivalence()
    objects = doc.get("objects") or []
    relations = doc.get("relations") or []
    observations = doc.get("observations") or []
    ids = {str(o.get("id")) for o in objects}
    dangling_rel = sorted(str(r.get("id")) for r in relations
                          if str(r.get("from")) not in ids or str(r.get("to")) not in ids)
    dangling_obs = sorted(str(o.get("id")) for o in observations
                          if str(o.get("objectId")) not in ids)
    by_type = {}
    for o in objects:
        by_type[o.get("type")] = by_type.get(o.get("type"), 0) + 1
    by_status = {}
    for o in observations:
        by_status[o.get("status")] = by_status.get(o.get("status"), 0) + 1
    expected = doc.get("expected") if isinstance(doc.get("expected"), dict) else {}
    observed = {o.get("metricCode") for o in observations if o.get("status") == "collected"}
    missing_types = sorted(set(expected.get("types") or []) - set(by_type))
    missing_metrics = sorted(set(expected.get("metrics") or []) - observed)
    under_min = sorted(t for t, mn in (expected.get("objectsMin") or {}).items()
                       if by_type.get(t, 0) < int(mn))
    under_rel = sorted(c for c, mn in (expected.get("relationsMin") or {}).items()
                       if sum(1 for r in relations if r.get("type") == c) < int(mn))
    ok = not (missing_types or missing_metrics or under_min or under_rel)
    coverage = {"ok": ok, "missingTypes": missing_types, "missingMetrics": missing_metrics,
                "underMinTypes": under_min}
    if expected.get("relationsMin"):
        coverage["underMinRelations"] = under_rel
    integ = doc.setdefault("integrity", {})
    integ["counts"] = {"objects": len(objects), "relations": len(relations),
                       "observations": len(observations),
                       "byType": dict(sorted(by_type.items())),
                       "byObservationStatus": dict(sorted(by_status.items()))}
    integ["referenceIntegrity"] = {"ok": not (dangling_rel or dangling_obs),
                                  "danglingRelationEnds": dangling_rel,
                                  "danglingObservationRefs": dangling_obs}
    integ["expectedCoverage"] = coverage
    integ["canonicalSha256"] = eq.canonical_snapshot_sha(doc)
    return {"counts": integ["counts"], "referenceIntegrity": integ["referenceIntegrity"],
            "expectedCoverage": coverage, "canonicalSha256": integ["canonicalSha256"],
            "scenario_coverage_ok": ok}


# ---------------------------------------------------------------- 逐条注入的落地

def _inj_set_fact(doc, inj, idx):
    """事实值/状态注入：只改被寻址观测的**声明字段**。"""
    where = "注入#%d" % idx
    target = _addr_of(inj, idx)
    status_mode = ("status_before" in inj) or ("status_after" in inj) or str(inj.get("field")) == "status"
    field = str(inj.get("field") or ("status" if status_mode else "value"))
    if field == "status":
        before = inj.get("status_before", inj.get("value_before", _MISSING))
        after = inj.get("status_after", inj.get("value_after", _MISSING))
    else:
        if field not in VALUE_FIELDS:
            raise InjectionRefused("%s：field=%r 不支持（只支持 %s 或 status）"
                                   % (where, field, "/".join(VALUE_FIELDS)))
        before = inj.get("value_before", _MISSING)
        after = inj.get("value_after", _MISSING)
    if after is _MISSING:
        raise InjectionRefused("%s：缺 %s（不得只声明基底取值而不给目标取值）"
                               % (where, "status_after" if field == "status" else "value_after"))
    obj, hits = _resolve_observations(doc, target, where=where)
    for h in hits:
        cur = h.get(field, _MISSING)
        if cur is _MISSING:
            raise InjectionRefused("%s：寻址到的观测 %s 没有字段 %r（观测自带键 = %s）"
                                   % (where, h.get("id"), field, sorted(h.keys())))
        if before is _MISSING or canonical_json(cur) != canonical_json(before):
            _refuse_value_before(target, field, before, cur, extra="obs_id=%s" % h.get("id"))
    changed = {}
    for h in hits:
        fields = [field]
        if field == "status":
            if str(after) not in STATUS_ENUM:
                raise InjectionRefused("%s：status_after=%r 不在 schema 枚举 %s 里"
                                       % (where, after, list(STATUS_ENUM)))
            h["status"] = after
            if str(after) == "collected":
                restore = inj.get("restore_value", _MISSING)
                if restore is not _MISSING:
                    if isinstance(restore, str):
                        h.pop("value", None)
                        h["valueText"] = restore
                        fields.append("valueText")
                    else:
                        h.pop("valueText", None)
                        h["value"] = restore
                        fields.append("value")
                if "value" not in h and "valueText" not in h:
                    raise InjectionRefused(
                        "%s：把观测 %s 置回 collected 必须同时给出取值（schema observation.allOf[0]："
                        "collected ⇒ 必须有 value 或 valueText；可用 restore_value 补）"
                        % (where, h.get("id")))
                if "reason" in h:
                    h.pop("reason")
                    fields.append("reason")
            else:
                # 状态翻成非 collected 时只摘 schema 约束的那两个取值字段
                # （$defs.observation.allOf[0]：collected ⇒ 必须有 value/valueText，否则必须有 reason）。
                # rawValue 保留：这样 notCollected → collected 的往返才是精确可逆的，
                # 不会留下无法复原的残差字段（否则工作集里会出现「未声明差分」）。
                for key in ("value", "valueText"):
                    if key in h:
                        h.pop(key)
                        fields.append(key)
                h["reason"] = str(inj.get("reason_after") or inj.get("reason")
                                  or ("注入置为 %s" % after))
                fields.append("reason")
        else:
            h[field] = after
            if field == "value" and "rawValue" in h and inj.get("sync_raw_value", True):
                h["rawValue"] = after
                fields.append("rawValue")
        changed[h["id"]] = sorted(set(fields))
    return {"declared": {"observations_changed": changed},
            "detail": {"op": "set_fact", "field": field, "target": target,
                       "object": obj.get("id"), "observations": sorted(changed),
                       "value_after": after}}


def _inj_remove_object(doc, inj, idx):
    """删对象 ⇒ 连带删它名下的观测 + 以它为端点的关系（D44 传播规则）。"""
    where = "注入#%d" % idx
    target = _addr_of(inj, idx)
    obj = _resolve_object(doc, target, where=where)
    oid = obj["id"]
    obs_gone = sorted(o["id"] for o in (doc.get("observations") or []) if o.get("objectId") == oid)
    rel_gone = sorted(r["id"] for r in (doc.get("relations") or [])
                      if r.get("from") == oid or r.get("to") == oid)
    doc["objects"] = [o for o in doc["objects"] if o.get("id") != oid]
    gone_obs = set(obs_gone)
    doc["observations"] = [o for o in doc["observations"] if o.get("id") not in gone_obs]
    gone_rel = set(rel_gone)
    doc["relations"] = [r for r in doc["relations"] if r.get("id") not in gone_rel]
    return {"declared": {"objects_removed": [oid], "observations_removed": obs_gone,
                         "relations_removed": rel_gone},
            "detail": {"op": "remove_object", "object": oid, "type": obj.get("type"),
                       "observations_removed": len(obs_gone),
                       "relations_removed": len(rel_gone)}}


def _inj_add_object(doc, inj, idx):
    """新增对象 ⇒ 按 schema 前缀铸 id；**不隐式派生任何关系**。"""
    where = "注入#%d" % idx
    schema = _load_schema()
    prefixes = _schema_prefixes(schema)
    spec = inj.get("object") if isinstance(inj.get("object"), dict) else inj
    type_code = str(spec.get("type") or inj.get("obj_type") or "")
    if type_code not in prefixes:
        raise InjectionRefused("%s：type=%r 不在 schema x-id-prefix-by-type 里（可铸 id 的类 = %s）"
                               % (where, type_code, sorted(prefixes)))
    key = spec.get("key")
    if not isinstance(key, dict) or not key:
        raise InjectionRefused("%s：新增对象必须给出 key 字典（schema $defs.object.required 含 key）" % where)
    layer = spec.get("layer") or TYPE_LAYER.get(type_code)
    if layer not in ("App", "Runtime", "Data"):
        raise InjectionRefused("%s：layer=%r 不在 schema layerCode.enum（App/Runtime/Data）里"
                               % (where, layer))
    local = spec.get("id_local", inj.get("id_local", _MISSING))
    if local is _MISSING:
        key_field = LOCAL_ID_FROM_KEY.get(type_code)
        if not key_field or key_field not in key:
            raise InjectionRefused(
                "%s：%s 无法从 key 推出局部 id（LOCAL_ID_FROM_KEY=%r 且 %r 不在 key 里）⇒ "
                "必须显式给 id_local（不猜）"
                % (where, type_code, key_field, key_field))
        local = key[key_field]
    local = str(local)
    if not local or ":" in local:
        raise InjectionRefused("%s：id_local=%r 非法（局部 id 不得为空、不得含冒号）" % (where, local))
    oid = "%s:%s" % (prefixes[type_code], local)
    pattern = _object_id_pattern(schema)
    if not re.match(pattern, oid):
        raise InjectionRefused("%s：铸出的 id=%r 不满足 schema objectId.pattern %s" % (where, oid, pattern))
    if any(str(o.get("id")) == oid for o in (doc.get("objects") or [])):
        raise InjectionRefused("%s：对象 %s 已存在（新增不得覆盖既存对象）" % (where, oid))
    source = str((spec.get("provenance") or {}).get("source") or inj.get("source") or "derived")
    allowed = _schema_enum("provenance", "properties.source", schema) or ["derived"]
    if source not in allowed:
        raise InjectionRefused("%s：provenance.source=%r 不在 schema 枚举 %s 里"
                               % (where, source, allowed))
    stamp = str(inj.get("constructed_at") or doc.get("capturedAt") or "1970-01-01T00:00:00Z")
    obj = {"id": oid, "type": type_code, "layer": layer,
           "key": copy.deepcopy(key), "attrs": copy.deepcopy(spec.get("attrs") or {}),
           "provenance": {"source": source,
                          "probe": str(inj.get("probe") or "m5/injection:constructed"),
                          "collectedAt": stamp}}
    doc["objects"].append(obj)
    return {"declared": {"objects_added": [oid]},
            "detail": {"op": "add_object", "object": oid, "type": type_code, "layer": layer,
                       "minted_prefix": prefixes[type_code]}}


def _resolve_endpoint(doc, value, where):
    """把关系端点解析成 CCF id：接受 CCF id 字符串或 ``{obj_type,obj_id}`` 寻址块。

    端点解析失败一律带上「关系端点」上下文，便于一眼看出是 from/to 的问题而不是注入
    目标本身的问题（寻址块分支会重抛带上下文的异常，保留原始可用 id 清单）。
    """
    if isinstance(value, dict):
        target = {"obj_type": value.get("obj_type", value.get("type")),
                  "obj_id": value.get("obj_id", value.get("id")),
                  "path": value.get("path")}
        if not target["obj_type"] or not target["obj_id"]:
            raise InjectionRefused("%s：关系端点寻址块缺 obj_type/obj_id：%s" % (where, _fmt(value)))
        try:
            return _resolve_object(doc, target, where=where)["id"]
        except InjectionRefused as exc:
            raise InjectionRefused("%s：关系端点寻址失败（%s）：%s" % (where, _fmt(target), exc))
    text = str(value or "")
    if not text:
        raise InjectionRefused("%s：关系端点为空（from/to 必填）" % where)
    ids = {str(o.get("id")) for o in (doc.get("objects") or [])}
    if text in ids:
        return text
    cands = sorted(i for i in ids if i.endswith(":" + text))
    if len(cands) == 1:
        return cands[0]
    raise InjectionRefused("%s：关系端点 %r 在 CCF 里不存在或有多解（不得制造悬空端点）；可用 id 前 6 个 = %s"
                           % (where, text, sorted(ids)[:6]))


def _inj_relation(doc, inj, idx, mode):
    """关系存在性注入（删/增）：只动该关系本身。"""
    where = "注入#%d" % idx
    rels = doc.get("relations") or []
    if mode == "remove_relation":
        rel_id = inj.get("relation_id")
        if rel_id:
            matches = [r for r in rels if str(r.get("id")) == str(rel_id)]
        else:
            code = inj.get("relation") or inj.get("relation_code") or inj.get("type")
            if not code:
                raise InjectionRefused("%s：remove_relation 需要 relation（relationCode）或 relation_id"
                                       % where)
            raw_from = inj.get("from", inj.get("source"))
            raw_to = inj.get("to", inj.get("target_end"))
            from_id = _resolve_endpoint(doc, raw_from, where) if raw_from is not None else None
            to_id = _resolve_endpoint(doc, raw_to, where) if raw_to is not None else None
            matches = [r for r in rels if str(r.get("type")) == str(code)
                       and (from_id is None or r.get("from") == from_id)
                       and (to_id is None or r.get("to") == to_id)]
        if not matches:
            raise InjectionRefused("%s：寻址不到任何关系（CCF 里没有匹配的关系）；现有关系类型 = %s"
                                   % (where, sorted({r.get("type") for r in rels})))
        if len(matches) > 1:
            raise InjectionRefused("%s：匹配到 %d 条关系，要求收窄到唯一一条（补 from/to 或直接给 "
                                   "relation_id）：%s" % (where, len(matches),
                                                         [r.get("id") for r in matches][:6]))
        hit = matches[0]
        doc["relations"] = [r for r in rels if r.get("id") != hit.get("id")]
        return {"declared": {"relations_removed": [hit.get("id")]},
                "detail": {"op": "remove_relation", "relation": hit.get("id"),
                           "type": hit.get("type"), "from": hit.get("from"), "to": hit.get("to")}}
    code = inj.get("relation") or inj.get("relation_code") or inj.get("type")
    allowed = _schema_enum("relationCode")
    if not code or (allowed and str(code) not in allowed):
        raise InjectionRefused("%s：relation=%r 不在 schema relationCode 枚举里（可加的关系码 = %s）"
                               % (where, code, sorted(allowed)[:24]))
    from_id = _resolve_endpoint(doc, inj.get("from", inj.get("source")), where)
    to_id = _resolve_endpoint(doc, inj.get("to", inj.get("target_end")), where)
    if any(str(r.get("type")) == str(code) and r.get("from") == from_id and r.get("to") == to_id
           for r in rels):
        raise InjectionRefused("%s：关系 %s(%s -> %s) 已存在（新增不得重复）"
                               % (where, code, from_id, to_id))
    used = set()
    for r in rels:
        m = re.match(r"^rel-(\d+)$", str(r.get("id")))
        if m:
            used.add(int(m.group(1)))
    nxt = (max(used) + 1) if used else 1
    rel_id = "rel-%06d" % nxt
    while any(str(r.get("id")) == rel_id for r in rels):
        nxt += 1
        rel_id = "rel-%06d" % nxt
    stamp = str(inj.get("constructed_at") or doc.get("capturedAt") or "1970-01-01T00:00:00Z")
    doc["relations"].append({"id": rel_id, "type": str(code), "from": from_id, "to": to_id,
                             "provenance": {"source": "derived",
                                            "probe": str(inj.get("probe") or "m5/injection:relation"),
                                            "collectedAt": stamp}})
    return {"declared": {"relations_added": [rel_id]},
            "detail": {"op": "add_relation", "relation": rel_id, "type": str(code),
                       "from": from_id, "to": to_id}}


def _inj_remove_relation(doc, inj, idx):
    return _inj_relation(doc, inj, idx, "remove_relation")


def _inj_add_relation(doc, inj, idx):
    return _inj_relation(doc, inj, idx, "add_relation")


_INJECTION_OPS = {
    "set_fact": _inj_set_fact,
    "set_value": _inj_set_fact,
    "set_status": _inj_set_fact,
    "remove_object": _inj_remove_object,
    "add_object": _inj_add_object,
    "remove_relation": _inj_remove_relation,
    "add_relation": _inj_add_relation,
}


# ---------------------------------------------------------------- 入口：基底 + 用例 -> 工作集

def _as_ccf_doc(base):
    """把入参收敛成一份 **CCF 文档**；采集层原始事实一律拒绝（D44 只有一条顺序）。"""
    if isinstance(base, dict) and isinstance(base.get("ccf"), dict):
        return base["ccf"], {"via": "load_base", "kind": base.get("kind"), "path": base.get("path")}
    if isinstance(base, dict) and "objects" in base and "integrity" in base and "all_facts" not in base:
        return base, {"via": "ccf-doc", "kind": "ccf", "path": None}
    kind = base.get("kind") if isinstance(base, dict) else None
    has_raw = isinstance(base, dict) and base.get("raw") is not None
    raise LiveFactsError(
        "注入面已在归一化之后的 CCF 层（D44）：apply_injections() 只接受 CCF 基底"
        "（load_base() 的返回值且 ccf 非空，或一份 CCF 文档本身）。"
        "拿到的 kind=%r%s ⇒ v1.1 的「归一化前注入」已被取代，本模块不保留第二条顺序；"
        "请先 load_base() 归一化成 CCF 再注入。" % (kind, "，且 raw 非空" if has_raw else ""))


def apply_injections(base, injections):
    """在 **CCF 层**按用例差分扰动基底（D44）。返回 ``(ws_doc, report)``。

    * ``base`` 只接受 CCF（见 ``_as_ccf_doc()``）：``load_base()`` 的返回值（``ccf`` 非空）
      或一份 CCF 文档；**基底只读**，返回的是深拷贝；
    * 每条注入的落地写进 ``report["applied"]``，并把「声明差分」并入 ``report["declared_diff"]``；
    * 覆盖闸门**不在**这里：覆盖只作用于基底，由 ``build_working_set()`` 先判后注入。
    """
    injections = list(injections or [])
    ccf, meta = _as_ccf_doc(base)
    for key in ("objects", "relations", "observations"):
        if not isinstance(ccf.get(key), list):
            raise LiveFactsError("基底不是合法 CCF：%r 不是列表" % key)
    base_sha = sha256_text(canonical_json(ccf))
    ws = copy.deepcopy(ccf)
    report = {"injections_total": len(injections), "applied": [],
              "declared_diff": _declared_empty(), "base_sha256": base_sha, "base_source": meta}
    for idx, inj in enumerate(injections):
        if not isinstance(inj, dict):
            raise InjectionRefused("第 %d 条注入不是对象：%s" % (idx, _fmt(inj)))
        op = str(inj.get("op") or "set_fact")
        handler = _INJECTION_OPS.get(op)
        if handler is None:
            raise InjectionRefused("第 %d 条注入的 op=%r 未知（支持：%s）"
                                   % (idx, op, sorted(_INJECTION_OPS)))
        out = handler(ws, inj, idx)
        _merge_declared(report["declared_diff"], out.get("declared"))
        entry = {"index": idx, "op": op}
        entry.update(out.get("detail") or {})
        report["applied"].append(entry)
    for key, val in report["declared_diff"].items():
        if key != "observations_changed":
            report["declared_diff"][key] = sorted(val)
    report["base_sha256_after"] = sha256_text(canonical_json(ccf))
    report["base_unchanged"] = report["base_sha256_after"] == base_sha
    if not report["base_unchanged"]:
        raise LiveFactsError("基底被就地改写了（内部错误：注入只允许改工作集拷贝）")
    report["integrity"] = refresh_ccf_integrity(ws)
    report["ws_counts"] = report["integrity"]["counts"]
    report["declared_diff_counts"] = _diff_counts(report["declared_diff"])
    return ws, report



# ----------------------------------------------------------------- 4) 归一化层


def normalize(raw, **kwargs):
    """产出 CCF 归一化三段：``{objects, relations, observations}``。

    实现上**直接复用** ``freeze.build()``（同一实现、同一映射表、同一排序与编号规则），
    本函数只负责：准入（信封形状 / 确定性时间戳）与中转入参。
    """
    doc, _ = _normalize_full(raw, **kwargs)
    return {
        "objects": doc["objects"],
        "relations": doc["relations"],
        "observations": doc["observations"],
    }


_COVERAGE_MARKERS = ("G5-coverage", "X8-coverage")


def _translate_freeze_refusal(exc, profile):
    """把 ``freeze.build`` 的拒绝转译成可读的 fail-closed 异常（保留 freeze 原文）。

    覆盖门禁类的拒绝单独点明，因为它**不受 ``force_write`` 影响**：那两条来自等价性
    自检规则（``shared/verify/equivalence.py`` 的 G5-coverage / X8-coverage），
    而 ``force_write`` 只能压住 ``freeze.py`` 自己追加的那条 G5 覆盖错误。
    """
    msg = str(exc)
    if any(marker in msg for marker in _COVERAGE_MARKERS):
        return LiveFactsError(
            "归一化被覆盖门禁拒绝（fail-closed）：这次注入把 expected_profile=%r 声明的某个对象类型 / "
            "某条 metric / 某条关系打到了零点，而冻结侧要求它们全部被观测到。该拒绝来自等价性自检规则"
            "（G5-coverage / X8-coverage），**不受 force_write 影响**：freeze 的覆盖判据只认 "
            "status==collected，而 X8 复算认任意 status、且完全不看 relationsMin，覆盖被打破时两者必然互相"
            "报告不一致。处置：改用不含该类型/metric/关系的 expected_profile，或让该类型/metric/关系至少"
            "保留一条命中。freeze 原文：%s" % (profile, msg)
        )
    return LiveFactsError("归一化被 freeze.build 拒绝（fail-closed）：%s" % msg)


def _normalize_full(
    raw,
    *,
    snapshot_id=None,
    captured_at=None,
    expected_profile=None,
    collect_url=None,
    upstream=None,
    layers=None,
    parallel=None,
    health=None,
    health_text=None,
    health_path=None,
    expected=None,
):
    """编排 ``freeze.build()``，返回 ``(doc, extra)``。

    中转文件全部写在**系统临时目录**（``tempfile.mkdtemp``），绝不写仓库树；
    ``finally`` 清理。``health_path`` 原样透传给 freeze（不做拷贝），
    这样 ``x.healthSha256`` 与磁盘上的原始回执逐字一致 ⇒ canonicalSha256 可复现。
    """
    _require_envelope(raw, where="normalize")
    captured = captured_at or raw.get("collected_at")
    if not captured:
        raise LiveFactsError(
            "原始信封缺 collected_at（fail-closed）：观测时间会被 wall clock 污染，"
            "同一输入两次调用就不再逐字相同。请在信封里带 collected_at，或显式传 captured_at。"
        )
    snap = snapshot_id or _derive_snapshot_id(raw)
    lyr = list(layers or raw.get("layers") or DEFAULT_LAYERS)
    url = collect_url or DEFAULT_PROVENANCE_URL
    up = upstream or _load_freeze().UPSTREAM
    prof = expected_profile or "l3-only"
    par = "true" if (parallel is True or par_flag(parallel)) else "false"

    freeze = _load_freeze()
    tmpdir = tempfile.mkdtemp(prefix="_m5_live_facts_")
    try:
        gather_path = _dump_temp_json(raw, tmpdir, "gather.json")
        if health_path:
            hpath = health_path
        else:
            text = health_text if health_text is not None else json.dumps(
                health if health is not None else _DEFAULT_HEALTH, ensure_ascii=False, sort_keys=True
            )
            hpath = os.path.join(tmpdir, "health.json")
            with io.open(hpath, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        args = argparse.Namespace(
            collect_url=url,
            layers=",".join(str(x) for x in lyr),
            parallel=par,
            expected_profile=prof,
            expect_simulated="false",
            snapshot_id=snap,
            out=None,
            map=None,
            schema=None,
            topology=None,
            upstream=up,
            inventory=None,
            allow_upstream_drift=True,
            health_json=hpath,
            gather_json=gather_path,
            timeout=60,
            dry_run=True,
            force_write=True,
        )
        try:
            doc, drop, obj_list, rel_objs, obs_sorted = freeze.build(args)
        except freeze.FreezeError as exc:
            raise _translate_freeze_refusal(exc, prof)
        except Exception as exc:  # noqa: BLE001
            raise LiveFactsError("归一化失败（fail-closed）：%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if expected is not None and doc.get("expected") != expected:
        raise LiveFactsError(
            "归一化产出的 expected 与请求不一致（fail-closed）：期望 %s 实得 %s"
            % (canonical_json(expected), canonical_json(doc.get("expected")))
        )
    extra = {
        "snapshot_id": doc["snapshotId"],
        "captured_at": doc["capturedAt"],
        "canonical_sha256": doc["integrity"]["canonicalSha256"],
        "expected_profile": prof,
        "dropped_facts": doc["integrity"]["droppedFacts"]["total"],
        "counts": doc["integrity"]["counts"],
    }
    return doc, extra


def par_flag(value):
    return str(value).lower() == "true"


# ----------------------------------------------------------------- 5) 工作集


def build_working_set(source, case):
    """``基底 →（覆盖闸门）→ 注入 → 工作集`` 的**纯函数**（D44 唯一顺序）。

    * ``source``：``load_base()`` 的返回值（``ccf`` 非空即可）或基底文件路径；
      采集信封由 ``load_base()`` 先经 ``freeze.build()`` 归一化成 CCF，覆盖门禁只看这份 CCF；
    * ``case``：``{"case_id":..., "injections":[...]}``，或直接给注入列表。

    ★ 覆盖门禁只作用于**基底**（``require_coverage_evidence()``）：证据缺失/不自洽 ⇒ ``BaseBlocked``
      （该例 BLOCKED，不得用 ``--force-write`` 绕过）；注入之后允许把 profile 声明项打到零点
      —— 覆盖描述的是「采集完整性」，不是场景属性。

    两次调用同一组输入 ⇒ ``working_set_sha256`` 逐字相同；``meta.canonical_sha256`` 永远是
    **基底**的同源锚（不随注入变化），``meta.working_set_canonical_sha256`` 才是工作集的锚。
    """
    if isinstance(source, str):
        base = load_base(source)
    elif isinstance(source, dict) and ("ccf" in source or ("objects" in source and "integrity" in source)):
        base = source
    else:
        raise LiveFactsError("build_working_set 的 source 需要 load_base() 结果或基底路径")

    if isinstance(case, dict):
        case_id = case.get("case_id")
        injections = list(case.get("injections") or [])
    elif isinstance(case, list):
        case_id, injections = None, list(case)
    elif case is None:
        case_id, injections = None, []
    else:
        raise LiveFactsError("build_working_set 的 case 需要对象或列表")

    # ① 覆盖门禁（只作用于基底，D44）
    base_doc, bmeta = _as_ccf_doc(base)
    cov_ev = require_coverage_evidence(base_doc, where="build_working_set(%s)" % (case_id,))

    # ② CCF 层注入（基底只读：apply_injections() 深拷贝后只改工作集）
    base_for_report = dict(base)
    base_for_report["case_id"] = case_id
    ws_doc, report = apply_injections(base_for_report, injections)

    # ③ 差分闸门：工作集必须逐字等于「基底 + 已声明差分」，且过得了 schema 机检
    diff = verify_declared_diff(base_doc, ws_doc, report["declared_diff"],
                                label="m5-working-set:%s" % (case_id,))
    cov = (ws_doc.get("integrity") or {}).get("expectedCoverage") or {}
    meta = {
        "snapshot_id": base.get("snapshot_id"),
        "canonical_sha256": base.get("canonical_sha256"),
        "working_set_sha256": sha256_text(canonical_json({
            "objects": ws_doc["objects"], "relations": ws_doc["relations"],
            "observations": ws_doc["observations"]})),
        "working_set_canonical_sha256": (ws_doc.get("integrity") or {}).get("canonicalSha256"),
        "case_id": case_id,
        "injection_count": len(injections),
        "injections_applied": len(report["applied"]),
        "base_kind": base.get("kind"),
        "base_path": base.get("path"),
        "base_via": bmeta.get("via"),
        "schema_error_codes": diff["codes"],
        "scenario_coverage_ok": cov.get("ok"),
        "declared_diff_counts": report["declared_diff_counts"],
    }
    source_block = {
        "via": bmeta.get("via"),
        "kind": base.get("kind"),
        "path": base.get("path"),
        "ccf_version": ws_doc.get("ccfVersion"),
        "snapshot_id": ws_doc.get("snapshotId"),
        "canonical_sha256": base.get("canonical_sha256"),
        "coverage_checked": True,
        "coverage_present": cov_ev["present"],
        "coverage_ok": cov_ev["ok"],
        "missingTypes": list((cov_ev["coverage"] or {}).get("missingTypes") or []),
        "missingMetrics": list((cov_ev["coverage"] or {}).get("missingMetrics") or []),
        "underMinTypes": list((cov_ev["coverage"] or {}).get("underMinTypes") or []),
        "underMinRelations": list((cov_ev["coverage"] or {}).get("underMinRelations") or []),
        "coverage_missing_fields": cov_ev["missing"],
        "coverage_problems": cov_ev["problems"],
        "coverage": cov_ev["coverage"],
        "coverage_counts_consistent": cov_ev["counts_consistent"],
        "expected_types": cov_ev["expected_types"],
        "expected_metrics": cov_ev["expected_metrics"],
        "relations_min_present": cov_ev["relations_min_present"],
        "relations_min_keys": cov_ev["relations_min_keys"],
    }
    return {
        "objects": ws_doc["objects"],
        "relations": ws_doc["relations"],
        "observations": ws_doc["observations"],
        "integrity": ws_doc.get("integrity"),
        "meta": meta,
        "source": source_block,
        "diff": {"declared": report["declared_diff"],
                 "declared_counts": report["declared_diff_counts"],
                 "actual": diff["actual"],
                 "schema_error_codes": diff["codes"],
                 # 声明过、但被同 case 后续注入合法抵消（最终与基底逐字相同）的观测字段；
                 # 不是失败项，但必须留在审计面里，避免「声明了却没变化」被静默吞掉。
                 "canceled_fields": diff.get("canceled_fields") or {},
                 "scenario_coverage": cov},
        "applied_report": report,
    }


# ----------------------------------------------------------------- 6) 有界聚合


def aggregate(working_set, cap_values=AGG_CAP_VALUES, cap_objs=AGG_CAP_OBJS, max_str=AGG_MAX_STR):
    """把工作集压成有界表示，返回 ``(by_path, meta)``，并把丢弃量原样回传。

    键空间用 CCF 的 ``metricCode``（CCF 观测不保留原始事实 path，metricCode 是它的同义物），
    条目形态与参考实现的 ``compress_facts`` 对齐：
    ``{"n","v","o","ot","s","t"}`` = 条数 / 不同取值 / 对象 / 对象类型 / 来源 / 时间窗。

    **只压表示、不丢事实**：``observations_out == observations_in``，
    因此 ``facts_dropped`` 恒为 0；真正的丢弃量是 ``values_truncated`` /
    ``objs_truncated`` / ``types_truncated`` / ``sources_truncated`` / ``strings_truncated``，
    且满足 ``retained_*_total + *_truncated == distinct_*_total``（可机检）。

    与参考实现 ``compress_facts`` 的**口径对齐与有意分歧**（差分实测见
    ``tmp\\_m5_s1_report.md``；对真实基底 191 条观测：path 键集/每路径 n/保留条数/
    截断前后取值全集/上限均一致）：

    - 对齐：``v`` 的**选取**按首次出现顺序取前 ``cap_values`` 个，与参考实现同一批取值
      （早期版本按排序选取，会在 3/42 条路径上选出不同的一批，已改）。
    - 分歧①：``v`` 元素是「取值令牌」``{status,value|valueText,unit}`` 而非裸取值 ——
      CCF 观测带单位与状态，丢掉它们会削弱诊断；代价是不能与参考实现逐字比对取值元素。
    - 分歧②：``v`` 保留集合输出前做规范排序（选取已在①前完成），保证纯函数性与可比对性。
    - 分歧③：字符串**硬截到 ``max_str``** 并由 ``strings_truncated`` 记账；参考实现改为在
      串尾追加 ``…(+N)``，结果长度反而超过 ``max_str``（本模块认为超限更不可取）。
    - 分歧④：``values_truncated`` 是**不同取值**口径；参考实现同名字段是**出现次数（事实条数）**
      口径 —— 真实基底上两者分别是 57 与 63，不是同一个数。
    - 分歧⑤：``facts_dropped`` 恒为 0（不丢事实），总丢弃量走 ``dropped_total``；
      参考实现的 ``facts_dropped`` 是「事实数 − 路径数」，语义不同。
    """
    obs = list((working_set or {}).get("observations") or [])
    strings = [0]
    groups = collections.OrderedDict()
    for o in obs:
        if not isinstance(o, dict):
            continue
        key = str(o.get("metricCode") or "")
        groups.setdefault(key, []).append(o)

    by_path = collections.OrderedDict()
    values_truncated = objs_truncated = types_truncated = sources_truncated = 0
    distinct_values_total = distinct_objs_total = retained_values_total = retained_objs_total = 0
    for key in sorted(groups):
        items = groups[key]
        tokens = collections.OrderedDict()
        for o in items:
            tok = canonical_json(_value_token(o))
            tokens.setdefault(tok, json.loads(tok))
        tok_list = [tokens[k] for k in tokens]  # 首次出现顺序 ⇒ 与 compress_facts 的选取口径一致
        objs = sorted({str(o.get("objectId") or "") for o in items})
        otypes = sorted({_type_of(working_set, o) for o in items})
        srcs = sorted({_source_of(o) for o in items})
        times = sorted({str(o.get("observedAt") or "") for o in items})

        distinct_values_total += len(tok_list)
        distinct_objs_total += len(objs)
        keep_tok = sorted(tok_list[:cap_values], key=canonical_json)  # 先按参考口径选，再做规范排序
        keep_obj = objs[:cap_objs]
        keep_type = otypes[:cap_objs]
        keep_src = srcs[:cap_objs]
        values_truncated += len(tok_list) - len(keep_tok)
        objs_truncated += len(objs) - len(keep_obj)
        types_truncated += len(otypes) - len(keep_type)
        sources_truncated += len(srcs) - len(keep_src)
        retained_values_total += len(keep_tok)
        retained_objs_total += len(keep_obj)
        by_path[key] = {
            "n": len(items),
            "v": keep_tok,
            "o": keep_obj,
            "ot": keep_type,
            "s": keep_src,
            "t": [times[0], times[-1]] if times else [],
        }

    by_path = _trunc_deep({"paths": by_path}, max_str, strings)["paths"]
    meta = {
        "key_field": "metricCode",
        "paths": len(by_path),
        "observations_in": len(obs),
        "observations_out": sum(e["n"] for e in by_path.values()),
        "facts_dropped": 0,
        "values_truncated": values_truncated,
        "objs_truncated": objs_truncated,
        "types_truncated": types_truncated,
        "sources_truncated": sources_truncated,
        "strings_truncated": strings[0],
        "dropped_total": values_truncated + objs_truncated + types_truncated + sources_truncated + strings[0],
        "distinct_values_total": distinct_values_total,
        "retained_values_total": retained_values_total,
        "distinct_objs_total": distinct_objs_total,
        "retained_objs_total": retained_objs_total,
        "cap_values": cap_values,
        "cap_objs": cap_objs,
        "max_str": max_str,
    }
    return by_path, meta


def _value_token(o):
    if o.get("status") == "collected":
        if "value" in o:
            return {"status": "collected", "value": o.get("value"), "unit": o.get("unit")}
        return {"status": "collected", "valueText": o.get("valueText"), "unit": o.get("unit")}
    return {"status": o.get("status"), "reason": o.get("reason")}


def _type_of(working_set, o):
    oid = o.get("objectId")
    for obj in (working_set.get("objects") or []):
        if isinstance(obj, dict) and obj.get("id") == oid:
            return str(obj.get("type") or "")
    return ""


def _source_of(o):
    prov = o.get("provenance") or {}
    return "%s/%s" % (prov.get("source") or "", prov.get("probe") or "")


def _trunc_deep(node, max_str, counter):
    if isinstance(node, dict):
        return {k: _trunc_deep(v, max_str, counter) for k, v in node.items()}
    if isinstance(node, list):
        return [_trunc_deep(v, max_str, counter) for v in node]
    if isinstance(node, str) and len(node) > max_str:
        counter[0] += 1
        return node[:max_str]
    return node


# ----------------------------------------------------------------- 夹具（仅 selftest / --equivalence 用）


def _fixture_facts():
    """内置 raw 夹具的事实表：``(layer, path, value, obj_id, obj_type, source, ts)``。"""
    rows = [
        ("gaussdb", "cluster.connection_max", 200, "cluster", "cluster", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "cluster.connection_total", 50, "cluster", "cluster", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "gaussdb.session.waiting", 1, "cluster", "cluster", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "node.buffer_hit_ratio", 99.5, "cluster", "cluster", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "replication.replay_lag_mb", 2.5, "dn_6002", "replication", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "session.state", "active", "sess-1", "session", "gaussdb.collect", "2026-01-01T00:00:01"),
        ("gaussdb", "table.schema_name", "public", "public.t_order", "table", "gaussdb.collect", "2026-01-01T00:00:02"),
        ("gaussdb", "table.n_live_tup", 1200, "public.t_order", "table", "gaussdb.collect", "2026-01-01T00:00:02"),
        ("gaussdb", "table.dead_tuple_ratio", 0.02, "public.t_order", "table", "gaussdb.collect", "2026-01-01T00:00:02"),
        ("gaussdb", "table.size_bytes", 1048576, "public.t_order", "table", "gaussdb.collect", "2026-01-01T00:00:02"),
        ("os", "host.cpu_usage", 12.5, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "os.load.avg1", 0.4, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "os.load.avg5", 0.5, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "host.disk_util", 3.0, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "node.disk_io_wait", 0.1, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "os.mem.free_kb", 1048576, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("os", "os.mem.total_kb", 4194304, "10.0.0.1", "node", "os.ssh", "2026-01-01T00:00:03"),
        ("container", "container.cpu_usage_ratio", 0.0016, "ctr-cc-pay-1", "container", "container:prom", "2026-01-01T00:00:04"),
        ("container", "container.cpu_throttled_periods", 137, "ctr-cc-pay-1", "container", "container:prom", "2026-01-01T00:00:04"),
        ("container", "container.mem_working_set_mb", 164.73, "ctr-cc-pay-1", "container", "container:prom", "2026-01-01T00:00:04"),
        ("container", "container.status", "running", "ctr-cc-pay-1", "container", "container:prom", "2026-01-01T00:00:04"),
        ("app", "app.uptime_s", 7861, "appinst-cc-pay-1", "app_instance", "app:http", "2026-01-01T00:00:05"),
        ("app", "app.heap_used_ratio", 0.0413, "appinst-cc-pay-1", "app_instance", "app:http", "2026-01-01T00:00:05"),
        ("app", "app.endpoint.p99_ms", 2.671, "ep-cc-pay", "app_endpoint", "app:http", "2026-01-01T00:00:05"),
        ("app", "app.endpoint.qps", 0.248, "ep-cc-pay", "app_endpoint", "app:http", "2026-01-01T00:00:05"),
        ("app", "app.datasource.active_connections", 0, "ds-cc-pay", "jdbc_datasource", "app:http", "2026-01-01T00:00:05"),
        ("app", "app.datasource.idle_connections", 5, "ds-cc-pay", "jdbc_datasource", "app:http", "2026-01-01T00:00:05"),
    ]
    return rows


def fixture_envelope():
    """内置的确定性 raw 信封夹具（**无网络、无磁盘依赖**）。"""
    rows = _fixture_facts()
    facts = [
        {"path": p, "value": v, "obj_id": oid, "obj_type": ot, "source": src, "ts": ts}
        for (_layer, p, v, oid, ot, src, ts) in rows
    ]
    per_layer = collections.Counter(r[0] for r in rows)
    return {
        "mode": "probes_v6",
        "layers": ["gaussdb", "os", "container", "app"],
        "collected_at": "2026-01-01T00:00:06.000000",
        "elapsed_ms": 1234,
        "layer_details": {k: {"fact_count": int(v)} for k, v in per_layer.items()},
        "layer_errors": {},
        "section_errors": {},
        "total_facts": len(facts),
        "total_alerts": 0,
        "all_facts": facts,
        "all_alerts": [],
        "topology": {
            "cluster_id": "fixcluster000000000000000000000000in14",
            "cluster_name": "fixture-cluster",
            "primary_ip": "10.0.0.1",
            "version": "fixture",
            "nodes": [
                {
                    "node_id": "dn_6001",
                    "host_id": "host-dn_6001",
                    "host_ip": "10.0.0.1",
                    "host_name": "fix-node-1",
                    "port": 8000,
                    "node_role": "Primary",
                    "node_status": "Normal",
                    "datanode_id": "1",
                },
                {
                    "node_id": "dn_6002",
                    "host_id": "host-dn_6002",
                    "host_ip": "10.0.0.2",
                    "host_name": "fix-node-2",
                    "port": 8000,
                    "node_role": "Standby",
                    "node_status": "Normal",
                    "datanode_id": "2",
                },
            ],
        },
        "ip_index": {
            "10.0.0.1": {
                "cluster_id": "fixcluster000000000000000000000000in14",
                "cluster_name": "fixture-cluster",
                "node_id": "dn_6001",
                "node_role": "Primary",
                "is_primary": True,
            },
            "10.0.0.2": {
                "cluster_id": "fixcluster000000000000000000000000in14",
                "cluster_name": "fixture-cluster",
                "node_id": "dn_6002",
                "node_role": "Standby",
                "is_primary": False,
            },
        },
        "container_topology": {
            "hosts": [
                {
                    "hostId": "host-dn_6001",
                    "hostname": "fix-node-1",
                    "ip": "10.0.0.9",
                    "role": "worker",
                    "osVersion": "fixture-os",
                    "kernelVersion": "5.10",
                    "cpuCores": 8,
                    "memTotalGb": 16,
                }
            ],
            "containers": [
                {
                    "containerId": "ctr-cc-pay-1",
                    "hostId": "host-dn_6001",
                    "name": "cc-pay",
                    "image": "fixture/cc-pay:1.0",
                    "imageDigest": "sha256:fixture",
                    "state": "running",
                    "restartCount": 0,
                    "memLimitMb": 4096,
                    "cpuLimitCores": 2,
                }
            ],
        },
        "app_topology": {
            "apps": [
                {
                    "appId": "app-cc",
                    "appCode": "creditcard",
                    "appName": "fixture-app",
                    "version": "1.0.0",
                    "techStack": "java",
                    "sloP99Ms": 100,
                }
            ],
            "app_instances": [
                {
                    "instanceId": "appinst-cc-pay-1",
                    "appId": "app-cc",
                    "containerId": "ctr-cc-pay-1",
                    "jvmVersion": "17",
                    "pid": 1234,
                    "startTime": "2026-01-01T00:00:00",
                }
            ],
            "app_endpoints": [
                {
                    "endpointId": "ep-cc-pay",
                    "appId": "app-cc",
                    "path": "/pay",
                    "protocol": "http",
                    "sloP99Ms": 100,
                }
            ],
            "datasources": [
                {
                    "datasourceId": "ds-cc-pay",
                    "appInstanceId": "appinst-cc-pay-1",
                    "dsCode": "gaussdb",
                    "dbHost": "10.0.0.1",
                    "jdbcUrlMasked": "jdbc:gaussdb://10.0.0.1:8000/cc",
                    "maxPoolSize": 20,
                    "minIdle": 2,
                    "poolImpl": "HikariCP",
                    "driverName": "org.postgresql.Driver",
                    "driverVersion": "42.7",
                    "authMode": "password",
                    "sslEnabled": True,
                    "connectionTimeoutMs": 3000,
                    "idleTimeoutMs": 60000,
                    "validationQuery": "select 1",
                }
            ],
        },
    }


def fixture_base():
    """把内置夹具包成 ``load_base()`` 形状的基底（内存对象，不落盘）。"""
    env = fixture_envelope()
    snap = _derive_snapshot_id(env)
    ccf, _ = _normalize_full(env, snapshot_id=snap, captured_at=env["collected_at"], expected_profile="l3-only")
    return {
        "kind": "gather-envelope",
        "path": "<fixture>",
        "fingerprint": None,
        "canonical_sha256": ccf["integrity"]["canonicalSha256"],
        "snapshot_id": snap,
        "raw": env,
        "ccf": ccf,
        "cases": [],
        "expected_profile": "l3-only",
        "collect_url": DEFAULT_PROVENANCE_URL,
        "upstream": _load_freeze().UPSTREAM,
        "layers": ["gaussdb", "os", "container", "app"],
        "parallel": False,
    }


# ----------------------------------------------------------------- 7) 自检

class _Selftest(object):
    """极简控制项收集器：``OK`` / ``BAD`` / ``BLOCKED`` 三态。"""

    def __init__(self):
        self.items = []

    def check(self, name, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            self.items.append((name, False, "%s: %s" % (type(exc).__name__, exc)))
        else:
            self.items.append((name, True, ""))

    def expect_raise(self, name, fn, *needles):
        try:
            fn()
        except LiveFactsError as exc:
            text = str(exc)
            miss = [n for n in needles if n and n not in text]
            if miss:
                self.items.append((name, False, "异常文案缺 %r：%s" % (miss, text[:400])))
            else:
                self.items.append((name, True, ""))
        except Exception as exc:  # noqa: BLE001
            self.items.append((name, False, "异常类型不对：%s: %s" % (type(exc).__name__, exc)))
        else:
            self.items.append((name, False, "本应抛 LiveFactsError 却没有抛"))

    def blocked(self, name, why):
        self.items.append((name, None, why))


def _report_selftest(st, t0, verbose):
    """打印逐项读数并给出退出码：全绿 0、任一项 BAD 1、出现 BLOCKED 3。"""
    bad = [x for x in st.items if x[1] is False]
    blk = [x for x in st.items if x[1] is None]
    if verbose:
        for name, ok, why in st.items:
            mark = "OK  " if ok is True else ("BLK " if ok is None else "BAD ")
            print("%s %s%s" % (mark, name, (" <- " + why) if why else ""))
        print("elapsed = %.2fs" % (time.time() - t0))
        print("SELFTEST %s %d/%d BLOCKED=%d"
              % ("PASS" if not bad and not blk else "FAIL",
                 len(st.items) - len(bad) - len(blk), len(st.items), len(blk)))
    if blk:
        return 3
    return 0 if not bad else 1


def selftest(verbose=True):
    """控制项：全绿返回 0，任一项 BAD 返回 1，出现 BLOCKED 返回 3。**全程不联网。**"""
    st = _Selftest()
    calls = []
    orig_fetch = globals().get("fetch_live")
    orig_connect = socket.socket.connect

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("selftest 期间不得联网：fetch_live 被调用")

    def _no_connect(self, *args, **kwargs):
        raise AssertionError("selftest 期间不得发起网络连接：socket.connect%r" % (args,))

    globals()["fetch_live"] = _boom
    socket.socket.connect = _no_connect
    t0 = time.time()
    try:
        base = fixture_base()
        ccf = base["ccf"]
        if not isinstance(ccf, dict) or "objects" not in ccf:
            st.blocked("00 fixture_ccf_available",
                       "fixture_base() 没有给出 CCF（ccf=%r）⇒ 覆盖证据与 CCF 层注入都无法验证"
                       % (type(ccf).__name__,))
            st.blocked("99 remaining_controls",
                       "夹具 CCF 缺失 ⇒ 其余控制项整体 BLOCKED（不伪装成通过）")
            return _report_selftest(st, t0, verbose)
        CTR = "ctr:ctr-cc-pay-1"
        HOST = "host:host-dn_6001"
        SESS = "session:sess-1"
        APPINST = "appinst:appinst-cc-pay-1"
        POOL2 = "dbpool:dn_6002-pool"
        CLUSTER = [o["id"] for o in ccf["objects"] if o["type"] == "GaussDBCluster"][0]

        # ---- 三类注入的用例（寻址键一律是 CCF id + metricCode）----------------
        inj_value = [{
            "target": {"obj_type": "Container", "obj_id": CTR,
                       "path": "container.cpu_usage_ratio"},
            "value_before": 0.0016, "value_after": 0.97,
        }]
        # db.cluster.replication_lag_mb 不在 l3-only 的 expected.metrics 里 ⇒ 状态翻转不碰覆盖
        inj_status = [{
            "target": {"obj_type": "GaussDBCluster", "obj_id": CLUSTER,
                       "path": "db.cluster.replication_lag_mb"},
            "field": "status", "status_before": "collected", "status_after": "notCollected",
        }]
        # db.session.state 在 expected.metrics 里且只有一条观测 ⇒ D44 下这是**允许**的场景注入
        inj_status_profile = [{
            "target": {"obj_type": "Session", "obj_id": SESS, "path": "db.session.state"},
            "field": "status", "status_before": "collected", "status_after": "notCollected",
        }]
        inj_rm_obj = [{"op": "remove_object", "target": {"obj_type": "Container", "obj_id": CTR}}]
        inj_rm_host = [{"op": "remove_object", "target": {"obj_type": "Host", "obj_id": HOST}}]
        inj_add_obj = [{
            "op": "add_object",
            "object": {"type": "AppEndpoint", "id_local": "ep-cc-query",
                       "key": {"path": "/query", "protocol": "http"}, "attrs": {"sloP99Ms": 200}},
            "constructed_at": "2026-01-01T00:00:09Z",
        }]
        inj_rm_rel = [{
            "op": "remove_relation", "relation_code": "RUNS_IN",
            "from": {"obj_type": "AppInstance", "obj_id": APPINST},
            "to": {"obj_type": "Container", "obj_id": CTR},
        }]
        inj_add_rel = [{
            "op": "add_relation", "relation_code": "HAS_SESSION",
            "from": {"obj_type": "DatabaseSessionPool", "obj_id": POOL2},
            "to": {"obj_type": "Session", "obj_id": SESS},
            "constructed_at": "2026-01-01T00:00:09Z",
        }]

        def tamper(mut):
            doc = copy.deepcopy(ccf)
            mut(doc)
            return doc

        # ---- 00-09 基线、覆盖证据、纯函数性 ---------------------------------
        def t00():
            assert isinstance(ccf, dict) and ccf.get("objects") and ccf.get("integrity"), "夹具 CCF 不完整"
            assert ccf["integrity"]["expectedCoverage"]["ok"] is True, "夹具基底的覆盖证据不成立"

        st.check("00 fixture_base_has_ccf", t00)

        def t01():
            ev = coverage_evidence(ccf)
            assert ev["present"] and ev["ok"] is True, "夹具基底覆盖证据不成立：%r" % (ev,)
            assert ev["coverage"]["missingTypes"] == [] and ev["coverage"]["missingMetrics"] == []
            assert ev["coverage"]["underMinTypes"] == []
            assert not ev["missing"] and not ev["problems"], "覆盖读数有问题：%r" % (ev,)
            assert ev["relations_min_present"] is False, "l3-only 不该有 relationsMin"
            assert ev["counts_consistent"] is True
            assert ev["expected_types"] == 6, "expected.types 计数错：%d" % ev["expected_types"]

        st.check("01 coverage_evidence_readings", t01)

        def t02():
            a = build_working_set(base, {"case_id": "c1", "injections": inj_value})
            b = build_working_set(base, {"case_id": "c1", "injections": inj_value})
            assert a["meta"]["working_set_sha256"] == b["meta"]["working_set_sha256"], "两次重建不一致"
            src = a["source"]
            assert src["coverage_ok"] is True and src["coverage_checked"] is True, "出参 source 缺覆盖读数"
            assert src["missingTypes"] == [] and src["missingMetrics"] == []
            assert src["underMinTypes"] == [] and src["expected_types"] == 6
            assert src["relations_min_present"] is False
            assert src["canonical_sha256"] == base["canonical_sha256"], "覆盖读数弄丢了基底锚"

        st.check("02 pure_same_case_same_sha_and_source_readings", t02)

        def t03():
            a1 = build_working_set(base, {"case_id": "A", "injections": inj_value})
            build_working_set(base, {"case_id": "B", "injections": inj_rm_obj})
            a2 = build_working_set(base, {"case_id": "A", "injections": inj_value})
            assert a1["meta"]["working_set_sha256"] == a2["meta"]["working_set_sha256"], "A 被 B 污染"

        st.check("03 no_cross_call_pollution", t03)

        def t04():
            a = build_working_set(base, {"case_id": "c2", "injections": inj_value})
            b = build_working_set(base, {"case_id": "c3", "injections": []})
            vals = [o.get("value") for o in a["observations"]
                    if o.get("metricCode") == "container.cpu_usage_ratio"]
            raws = [o.get("rawValue") for o in a["observations"]
                    if o.get("metricCode") == "container.cpu_usage_ratio"]
            assert vals == [0.97] and raws == [0.97], "值注入没传导（含 rawValue）：%r/%r" % (vals, raws)
            assert a["meta"]["working_set_sha256"] != b["meta"]["working_set_sha256"], "注入未改变工作集"
            assert a["meta"]["working_set_canonical_sha256"] != b["meta"]["working_set_canonical_sha256"]

        st.check("04 inject_value_reaches_observation_and_rawvalue", t04)

        def t05():
            a = build_working_set(base, {"case_id": "c4", "injections": inj_status})
            hit = [o for o in a["observations"]
                   if o.get("metricCode") == "db.cluster.replication_lag_mb"]
            assert hit and hit[0]["status"] == "notCollected", "status 注入无效：%r" % (hit,)
            assert hit[0].get("reason"), "notCollected 必须带 reason"
            assert "value" not in hit[0] and "valueText" not in hit[0], "notCollected 不得带取值"
            # rawValue 属于审计证据，状态翻转时保留（见 _inj_set_fact：往返必须精确可逆）
            assert "rawValue" in hit[0], "rawValue 应保留供审计：%r" % (hit[0],)
            fields = a["applied_report"]["declared_diff"]["observations_changed"][hit[0]["id"]]
            assert "status" in fields and "reason" in fields and "value" in fields, "差分字段不全：%r" % (fields,)

        st.check("05 inject_status_notcollected", t05)

        def t06():
            a = build_working_set(base, {"case_id": "c5", "injections": inj_status_profile})
            hit = [o for o in a["observations"] if o.get("metricCode") == "db.session.state"]
            assert hit and hit[0]["status"] == "notCollected"
            cov = a["applied_report"]["integrity"]["expectedCoverage"]
            assert cov["ok"] is False and "db.session.state" in cov["missingMetrics"], \
                "场景打穿声明面时覆盖读数应转 false：%r" % (cov,)
            assert a["diff"]["schema_error_codes"] == ["X8-coverage"], \
                "只允许覆盖族报错，实为 %r" % (a["diff"]["schema_error_codes"],)

        st.check("06 scenario_zeroing_profile_is_allowed_d44", t06)

        def t07():
            b1 = coverage_evidence(tamper(lambda d: d["integrity"]["expectedCoverage"].__setitem__("ok", False)))
            assert b1["ok"] is False and b1["problems"], "ok=false 未被读出：%r" % (b1,)
            b2 = coverage_evidence(tamper(lambda d: d["integrity"].pop("expectedCoverage")))
            assert not b2["present"] and "integrity.expectedCoverage" in b2["missing"], "缺块未报：%r" % (b2,)
            b3 = coverage_evidence(tamper(lambda d: d["integrity"]["expectedCoverage"].pop("missingMetrics")))
            assert "integrity.expectedCoverage.missingMetrics" in b3["missing"], "缺键未报：%r" % (b3,)
            b4 = coverage_evidence(tamper(lambda d: d["integrity"]["counts"].__setitem__("objects", 99)))
            assert b4["counts_consistent"] is False, "counts 不自洽未报：%r" % (b4,)
            b5 = coverage_evidence(tamper(lambda d: d.__setitem__("expected", {})))
            assert "expected.types/metrics" in b5["missing"], "空 expected 未报：%r" % (b5,)

        st.check("07 coverage_evidence_reads_all_defects", t07)

        def t08():
            st.expect_raise("08 coverage_gate_ok_false_blocked",
                            lambda: require_coverage_evidence(
                                tamper(lambda d: d["integrity"]["expectedCoverage"].__setitem__("ok", False))),
                            "BLOCKED", "expectedCoverage.ok", "freeze.py")

        t08()

        def t09():
            st.expect_raise("09 coverage_gate_block_missing_blocked",
                            lambda: require_coverage_evidence(
                                tamper(lambda d: d["integrity"].pop("expectedCoverage"))),
                            "BLOCKED", "integrity.expectedCoverage")

        t09()

        def t10():
            st.expect_raise("10 coverage_gate_counts_inconsistent_blocked",
                            lambda: require_coverage_evidence(
                                tamper(lambda d: d["integrity"]["counts"].__setitem__("relations", 0))),
                            "BLOCKED", "counts 与实际条数不符")

        t10()

        def t11():
            st.expect_raise("11 build_working_set_blocks_on_bad_base",
                            lambda: build_working_set(
                                dict(base, ccf=tamper(
                                    lambda d: d["integrity"]["expectedCoverage"].__setitem__("ok", False))),
                                {"case_id": "c6", "injections": inj_value}),
                            "BLOCKED", "覆盖门禁只作用于基底")

        t11()

        # ---- 12-20 三类注入面 + 传播规则 -------------------------------------
        def t12():
            a = build_working_set(base, {"case_id": "c7", "injections": inj_rm_obj})
            ids = {o["id"] for o in a["objects"]}
            assert CTR not in ids, "删对象没传导到 objects"
            assert not any(r["type"] == "DEPLOYED_ON" for r in a["relations"]), "以它为端点的关系应消失"
            assert not any(r["type"] == "RUNS_IN" for r in a["relations"]), "它是 RUNS_IN 的 to 端点，应连带消失"
            assert not any(o["objectId"] == CTR for o in a["observations"]), "它名下的观测应消失"
            c = a["applied_report"]["declared_diff_counts"]
            assert c["objects_removed"] == 1 and c["observations_removed"] == 4 and c["relations_removed"] == 2, (
                "传导计数错（容器名下有 4 条观测：cpu_throttled_periods/cpu_usage_ratio/"
                "mem_working_set_mb/status，2 条关系以它为端点：DEPLOYED_ON.from、RUNS_IN.to）：%r" % (c,))

        st.check("12 remove_object_propagates_obs_and_relations", t12)

        def t13():
            a = build_working_set(base, {"case_id": "c8", "injections": inj_rm_host})
            ids = {o["id"] for o in a["objects"]}
            assert HOST not in ids, "删 Host 没传导"
            assert not any(o["objectId"] == HOST for o in a["observations"]), "Host 名下观测应消失"
            assert a["source"]["coverage_ok"] is True, "基底覆盖读数不该被场景改写"

        st.check("13 remove_object_host_cascades_observations", t13)

        def t14():
            a = build_working_set(base, {"case_id": "c9", "injections": inj_add_obj})
            ids = {o["id"]: o for o in a["objects"]}
            assert "endpoint:ep-cc-query" in ids, "增对象没进 objects：%r" % (sorted(ids)[:4],)
            new = ids["endpoint:ep-cc-query"]
            assert new["type"] == "AppEndpoint" and new["layer"] == "App", "铸造对象字段错：%r" % (new,)
            assert new["provenance"]["source"] == "derived", "source 必须落在 schema 枚举里"
            assert not any(r.get("to") == "endpoint:ep-cc-query" or r.get("from") == "endpoint:ep-cc-query"
                           for r in a["relations"]), "D44 禁止隐式派生关系"

        st.check("14 add_object_mints_id_without_implicit_relation", t14)

        def t15():
            st.expect_raise("15 add_object_needs_id_local",
                            lambda: apply_injections(base, [{
                                "op": "add_object",
                                "object": {"type": "AppEndpoint", "key": {"path": "/x", "protocol": "http"}}}]),
                            "id_local", "无法从 key 推出局部 id")

        t15()

        def t16():
            st.expect_raise("16 add_object_unknown_type",
                            lambda: apply_injections(base, [{
                                "op": "add_object",
                                "object": {"type": "Widget", "id_local": "w1", "key": {"a": 1}}}]),
                            "x-id-prefix-by-type", "Widget")
            st.expect_raise("16b add_object_duplicate_id",
                            lambda: apply_injections(base, [{
                                "op": "add_object",
                                "object": {"type": "Host", "key": {"hostname": "host-dn_6001"}}}]),
                            "已存在")
            st.expect_raise("16c add_object_bad_provenance_source",
                            lambda: apply_injections(base, [{
                                "op": "add_object",
                                "object": {"type": "Host", "id_local": "host-x1", "key": {"hostname": "h-x1"},
                                           "provenance": {"source": "constructed"}}}]),
                            "provenance.source", "constructed")

        t16()

        def t17():
            a = build_working_set(base, {"case_id": "c10", "injections": inj_rm_rel})
            assert not any(r["type"] == "RUNS_IN" for r in a["relations"]), "删关系没传导到 relations"
            assert a["applied_report"]["declared_diff_counts"]["relations_removed"] == 1
            assert len(a["objects"]) == len(ccf["objects"]), "删关系不该动对象"

        st.check("17 remove_relation_only_touches_that_relation", t17)

        def t18():
            a = build_working_set(base, {"case_id": "c11", "injections": inj_add_rel})
            got = [r for r in a["relations"] if r["type"] == "HAS_SESSION" and r["from"] == POOL2
                   and r["to"] == SESS]
            assert len(got) == 1, "增关系没传导到 relations：%r" % (got,)
            assert got[0]["id"] == "rel-000016", "新关系 id 应沿用 rel-%06d 铸法：%r" % (got[0]["id"],)
            assert a["applied_report"]["declared_diff_counts"]["relations_added"] == 1

        st.check("18 add_relation_mints_rel_id", t18)

        def t19():
            st.expect_raise("19 add_relation_bad_code",
                            lambda: apply_injections(base, [{
                                "op": "add_relation", "relation_code": "FROBS",
                                "from": {"obj_type": "Session", "obj_id": SESS},
                                "to": {"obj_type": "Container", "obj_id": CTR}}]),
                            "relationCode")
            st.expect_raise("19b add_relation_duplicate",
                            lambda: apply_injections(base, [{
                                "op": "add_relation", "relation_code": "RUNS_IN",
                                "from": {"obj_type": "AppInstance", "obj_id": APPINST},
                                "to": {"obj_type": "Container", "obj_id": CTR}}]),
                            "已存在")
            st.expect_raise("19c add_relation_dangling_endpoint",
                            lambda: apply_injections(base, [{
                                "op": "add_relation", "relation_code": "HAS_SESSION",
                                "from": {"obj_type": "DatabaseSessionPool", "obj_id": POOL2},
                                "to": {"obj_type": "Container", "obj_id": "ctr-nope"}}]),
                            "端点", "ctr-nope")
            st.expect_raise("19d remove_relation_missing",
                            lambda: apply_injections(base, [{
                                "op": "remove_relation", "relation_code": "REPLICATES_TO",
                                "from": {"obj_type": "GaussDBInstance", "obj_id": "dbinst:dn_6002"},
                                "to": {"obj_type": "GaussDBInstance", "obj_id": "dbinst:dn_6001"}}]),
                            "寻址不到任何关系")

        t19()

        # ---- 20-27 fail-closed：寻址、取值、状态枚举、差分 ---------------------
        def t20():
            bad = [dict(inj_value[0], value_before=0.5)]
            st.expect_raise("20 value_before_mismatch_names_both_sides",
                            lambda: apply_injections(base, bad), "0.5", "0.0016", "基底实际值")

        t20()

        def t21():
            st.expect_raise("21 address_miss_lists_available",
                            lambda: apply_injections(base, [{
                                "target": {"obj_type": "Container", "obj_id": "ctr-nope",
                                           "path": "container.cpu_usage_ratio"},
                                "value_before": 1, "value_after": 2}]),
                            "ctr-nope", "寻址不到任何对象")
            st.expect_raise("21b metric_miss_lists_available",
                            lambda: apply_injections(base, [{
                                "target": {"obj_type": "Container", "obj_id": CTR, "path": "container.nope"},
                                "value_before": 1, "value_after": 2}]),
                            "container.nope", "metricCode")
            st.expect_raise("21c obj_type_mismatch",
                            lambda: apply_injections(base, [{
                                "target": {"obj_type": "Host", "obj_id": CTR,
                                           "path": "container.cpu_usage_ratio"},
                                "value_before": 0.0016, "value_after": 2}]),
                            "obj_type 不符", "Host")

        t21()

        def t22():
            st.expect_raise("22 status_before_mismatch",
                            lambda: apply_injections(base, [dict(inj_status[0], status_before="notCollected")]),
                            "notCollected", "collected")
            st.expect_raise("22b status_after_not_in_schema_enum",
                            lambda: apply_injections(base, [dict(inj_status[0], status_after="broken")]),
                            "broken", "schema 枚举")

        t22()

        def t23():
            # 先把唯一一条观测翻成 notCollected（取值被摘下），再要求置回 collected 却不补取值 ⇒ 必须拒
            SESS_OBS = {"obj_type": "Session", "obj_id": SESS, "path": "db.session.state"}
            st.expect_raise("23 collected_requires_value",
                            lambda: apply_injections(base, [
                                {"target": dict(SESS_OBS), "field": "status",
                                 "status_before": "collected", "status_after": "notCollected"},
                                {"target": dict(SESS_OBS), "field": "status",
                                 "status_before": "notCollected", "status_after": "collected"}]),
                            "collected", "valueText", "restore_value")

        t23()

        def t23b():
            SESS_OBS = {"obj_type": "Session", "obj_id": SESS, "path": "db.session.state"}
            a = build_working_set(base, {"case_id": "c22", "injections": [
                {"target": dict(SESS_OBS), "field": "status",
                 "status_before": "collected", "status_after": "notCollected"},
                {"target": dict(SESS_OBS), "field": "status",
                 "status_before": "notCollected", "status_after": "collected",
                 "restore_value": "active"}]})
            hit = [o for o in a["observations"] if o.get("metricCode") == "db.session.state"]
            assert hit and hit[0]["status"] == "collected" and hit[0].get("valueText") == "active", \
                "restore_value 没落地：%r" % (hit,)
            assert "reason" not in hit[0], "回到 collected 后 reason 应被摘掉"
            assert a["diff"]["scenario_coverage"]["ok"] is True, "恢复后覆盖应回到 ok"
            # 这一对注入最终与基底逐字相同 ⇒ 没有任何净差分，但声明过 ⇒ 记入 canceled_fields
            assert a["diff"]["canceled_fields"].get("obs-000028"), \
                "往返抵消应留在审计面 canceled_fields 里：%r" % (a["diff"]["canceled_fields"],)
            assert a["diff"]["actual"]["observations_changed"] == {}, \
                "往返抵消后不应有净字段差分：%r" % (a["diff"]["actual"]["observations_changed"],)

        st.check("23b collected_restore_value_round_trip", t23b)

        def t24():
            ws, report = apply_injections(base, inj_value)
            assert report["base_unchanged"] is True, "基底被就地改写"
            wobble = copy.deepcopy(ws)
            wobble["objects"][0]["attrs"]["injected_without_declaration"] = 1
            st.expect_raise("24 undeclared_object_change_refused",
                            lambda: verify_declared_diff(ccf, wobble, report["declared_diff"]),
                            "未声明差分")
            extra = copy.deepcopy(ws)
            extra["relations"].append({"id": "rel-000099", "type": "RUNS_IN",
                                       "from": APPINST, "to": CTR,
                                       "provenance": {"source": "derived", "probe": "x",
                                                      "collectedAt": "2026-01-01T00:00:00Z"}})
            st.expect_raise("24b undeclared_relation_add_refused",
                            lambda: verify_declared_diff(ccf, extra, report["declared_diff"]),
                            "未声明差分", "relations_added")
            # 「声明了但未落地」：声明的删除确实写进了 declared_diff，但工作集里那条关系还在
            # （把已删的关系原样放回去模拟「实现没落地」）⇒ 必须报。
            ws2, rep2 = apply_injections(base, inj_rm_rel)
            assert not any(r["type"] == "RUNS_IN" for r in ws2["relations"]), "前置：关系应已删掉"
            landed = copy.deepcopy(ws2)
            landed["relations"].extend(copy.deepcopy(
                [r for r in ccf["relations"] if r["type"] == "RUNS_IN"]))
            st.expect_raise("24c declared_but_not_landed_refused",
                            lambda: verify_declared_diff(ccf, landed, rep2["declared_diff"]),
                            "声明了但未落地", "relations_removed")

        t24()

        def t25():
            before = sha256_text(canonical_json(ccf))
            apply_injections(base, inj_rm_obj)
            apply_injections(base, inj_value)
            apply_injections(base, inj_add_obj)
            after = sha256_text(canonical_json(ccf))
            assert before == after, "基底只读被破坏"
            assert base["canonical_sha256"] == (
                _load_equivalence().canonical_snapshot_sha(copy.deepcopy(ccf))), "基底锚被改"

        st.check("25 base_immutable_across_injections", t25)

        def t26():
            st.expect_raise("26 raw_facts_input_refused_d44",
                            lambda: apply_injections({"kind": "gather-envelope", "raw": fixture_envelope()},
                                                     inj_value),
                            "归一化之后的 CCF 层", "load_base")
            st.expect_raise("26b source_without_ccf_refused",
                            lambda: build_working_set({"kind": "x", "raw": fixture_envelope()},
                                                      {"case_id": "c12", "injections": []}),
                            "source 需要 load_base() 结果")

        t26()

        def t27():
            st.expect_raise("27 unknown_op_refused",
                            lambda: apply_injections(base, [{"op": "drop_everything"}]),
                            "op=", "未知")
            st.expect_raise("27b target_missing_refused",
                            lambda: apply_injections(base, [{"value_before": 1, "value_after": 2}]),
                            "target", "寻址键")

        t27()

        # ---- 28-32 归一化与 schema 结构 -------------------------------------
        def t28():
            ws = build_working_set(base, {"case_id": "c13", "injections": []})
            prefixes = _schema_prefixes()
            allowed = set(prefixes.values())
            for o in ws["objects"]:
                pfx = str(o["id"]).split(":")[0]
                assert pfx in allowed, "objectId 前缀非法：%s" % o["id"]
                assert prefixes.get(o["type"]) == pfx, "type 与前缀不匹配：%s/%s" % (o["type"], o["id"])
            assert all(o.get("id") for o in ws["observations"])

        st.check("28 id_prefix_conforms_to_schema", t28)

        def t29():
            a = build_working_set(base, {"case_id": "c14", "injections": inj_value})
            ic = a["applied_report"]["integrity"]
            assert ic["counts"]["objects"] == len(a["objects"])
            assert ic["counts"]["observations"] == len(a["observations"])
            assert ic["referenceIntegrity"] == {"ok": True, "danglingRelationEnds": [],
                                                "danglingObservationRefs": []}
            assert a["meta"]["working_set_canonical_sha256"] == ic["canonicalSha256"], "工作集锚不自洽"

        st.check("29 ws_integrity_refreshed_consistently", t29)

        def t30():
            _compare_with_freeze(base, label="内置夹具")

        st.check("30 equivalence_vs_freeze_build", t30)

        def t31():
            _, e1 = _normalize_full(base["raw"], snapshot_id=base["snapshot_id"],
                                    captured_at=base["raw"]["collected_at"], expected_profile="l3-only")
            _, e2 = _normalize_full(base["raw"], snapshot_id=base["snapshot_id"],
                                    captured_at=base["raw"]["collected_at"], expected_profile="l3-only")
            assert e1["canonical_sha256"] == e2["canonical_sha256"] == base["canonical_sha256"], \
                "canonical 不可复现"

        st.check("31 canonical_reproducible", t31)

        def t32():
            a = build_working_set(base, {"case_id": "c15", "injections": inj_value})
            b = build_working_set(base, {"case_id": "c15", "injections": inj_value})
            assert a["diff"]["declared_counts"] == b["diff"]["declared_counts"]
            assert a["diff"]["declared_counts"]["observations_changed"] == 1
            assert _diff_counts(a["diff"]["actual"]) == a["diff"]["declared_counts"], "声明与实测差分不一致"

        st.check("32 declared_diff_matches_actual", t32)

        # ---- 33-38 聚合 ------------------------------------------------------
        def t33():
            ws = _synth_working_set(values=[{"status": "collected", "value": i, "unit": "x"}
                                            for i in range(11)])
            by_path, meta = aggregate(ws)
            assert len(by_path["m.demo"]["v"]) == 8, "cap_values 未生效"
            assert meta["values_truncated"] == 3, "值截断量错：%r" % (meta["values_truncated"],)

        st.check("33 aggregate_cap_values", t33)

        def t34():
            ws = _synth_working_set(objs=["obj-%d" % i for i in range(6)])
            by_path, meta = aggregate(ws)
            assert len(by_path["m.demo"]["o"]) == 4, "cap_objs 未生效"
            assert meta["objs_truncated"] == 2, "对象截断量错"

        st.check("34 aggregate_cap_objs", t34)

        def t35():
            ws = _synth_working_set(values=[{"status": "collected", "valueText": "x" * 300, "unit": "y"}])
            by_path, meta = aggregate(ws)
            got = by_path["m.demo"]["v"][0]["valueText"]
            assert len(got) == 120, "max_str 未生效：%d" % len(got)
            assert meta["strings_truncated"] >= 1

        st.check("35 aggregate_max_str", t35)

        def t36():
            ws = _synth_working_set(values=[{"status": "collected", "value": i, "unit": "x"}
                                            for i in range(11)],
                                    objs=["obj-%d" % i for i in range(6)])
            by_path, meta = aggregate(ws)
            assert meta["observations_out"] == meta["observations_in"], "聚合丢了事实"
            assert meta["facts_dropped"] == 0
            assert meta["retained_values_total"] + meta["values_truncated"] == meta["distinct_values_total"]
            assert meta["retained_objs_total"] + meta["objs_truncated"] == meta["distinct_objs_total"]

        st.check("36 aggregate_conservation", t36)

        def t37():
            ws = build_working_set(base, {"case_id": "c16", "injections": inj_value})
            by_path, meta = aggregate(ws)
            assert meta["observations_out"] == len(ws["observations"]) == meta["observations_in"]
            assert by_path, "聚合结果为空"
            assert len(by_path) == len({o["metricCode"] for o in ws["observations"]})

        st.check("37 aggregate_on_working_set", t37)

        def t38():
            # aggregate() 不校验 cap 参数（实读 :1398-1492）⇒ 固定住 cap_values=0 的边界语义：
            # v 为空、但全部取值都计入 values_truncated（不得静默丢弃）
            by_path, meta = aggregate(_synth_working_set(
                values=[{"status": "collected", "value": i} for i in range(3)]), cap_values=0)
            assert by_path["m.demo"]["v"] == [], "cap_values=0 时 v 应为空：%r" % (by_path["m.demo"]["v"],)
            assert meta["values_truncated"] == 3, "cap_values=0 时截断量应为 3：%r" % (meta["values_truncated"],)

        st.check("38 aggregate_zero_cap_edge", t38)

        # ---- 39-44 网络、信封基底、审计面 -------------------------------------
        def t39():
            assert not calls, "selftest 期间 fetch_live 被调用：%r" % (calls,)

        st.check("39 no_network_fetch_not_called", t39)

        def t40():
            tmpd = tempfile.mkdtemp(prefix="_m5_lf_selftest_")
            try:
                gp = os.path.join(tmpd, "g.json")
                hp = os.path.join(tmpd, "h.json")
                env = fixture_envelope()
                with io.open(gp, "w", encoding="utf-8", newline="") as f:
                    f.write(json.dumps(env, ensure_ascii=False))
                htxt = json.dumps({"collect_source": {"collect_source": "live", "simulated": False,
                                                      "unmatched_exits": []},
                                   "gaussdb": {"ok": True},
                                   "note": "selftest 变体：与内置最小 health 逐字不同，用于证明锚随回执走"},
                                  ensure_ascii=False, sort_keys=True)
                with io.open(hp, "w", encoding="utf-8", newline="") as f:
                    f.write(htxt)
                b1 = load_base(gp, expected_profile="l3-only", health_path=hp)
                b2 = load_base(gp, expected_profile="l3-only", health_path=hp)
                assert b1["snapshot_id"] == b2["snapshot_id"] == _derive_snapshot_id(env), "snapshot_id 不确定"
                assert b1["canonical_sha256"] == b2["canonical_sha256"], "同 raw + 同 health 锚不稳定"
                assert b1["canonical_sha256"] != base["canonical_sha256"], "health 回执未参与同源锚"
                assert isinstance(b1["ccf"], dict) and b1["ccf"].get("objects"), "信封基底必须保留 CCF"
                ws = build_working_set(b1, {"case_id": "c17", "injections": inj_value})
                assert ws["meta"]["canonical_sha256"] == b1["canonical_sha256"], "工作集弄丢了基底锚"
                assert ws["source"]["coverage_ok"] is True, "信封基底的覆盖读数缺失"
                hit = [o for o in ws["observations"]
                       if o.get("metricCode") == "container.cpu_usage_ratio"]
                assert hit and hit[0].get("value") == 0.97, "信封基底上的注入没落地"
            finally:
                shutil.rmtree(tmpd, ignore_errors=True)

        st.check("40 envelope_base_keeps_ccf_and_health_anchor", t40)

        def t41():
            a = build_working_set(base, {"case_id": "c18", "injections": inj_add_obj + inj_rm_rel})
            kinds = sorted(r["op"] for r in a["applied_report"]["applied"])
            assert kinds == ["add_object", "remove_relation"], "applied 审计面不全：%r" % (kinds,)
            assert all("index" in r and "op" in r for r in a["applied_report"]["applied"])
            assert a["applied_report"]["declared_diff"]["relations_removed"], "声明差分没记关系"

        st.check("41 applied_report_audit_surface", t41)

        def t42():
            a = build_working_set(base, {"case_id": "c19", "injections": inj_value})
            by_path, meta = aggregate(a)
            assert meta["observations_in"] == len(a["observations"])
            assert a["meta"]["injection_count"] == 1 and a["meta"]["injections_applied"] == 1
            assert a["meta"]["base_kind"] == "gather-envelope" and a["meta"]["case_id"] == "c19"

        st.check("42 meta_audit_fields", t42)

        def t43():
            a = build_working_set(base, {"case_id": "c20", "injections": inj_status})
            cov = a["diff"]["scenario_coverage"]
            assert cov["ok"] is True, "该场景不该打穿覆盖：%r" % (cov,)
            assert a["diff"]["schema_error_codes"] == [], "干净的场景不该报 schema 错误：%r" % (
                a["diff"]["schema_error_codes"],)

        st.check("43 clean_scenario_no_schema_errors", t43)

        def t44():
            ids = [r["id"] for r in ccf["relations"]]
            assert ids == sorted(ids), "基底关系 id 未排序"
            a = build_working_set(base, {"case_id": "c21", "injections": inj_add_rel})
            nids = [r["id"] for r in a["relations"]]
            assert nids == sorted(nids), "新铸关系 id 破坏排序（x-ccf-rules R1）"

        st.check("44 relation_ids_sorted_after_mint", t44)
    finally:
        globals()["fetch_live"] = orig_fetch
        socket.socket.connect = orig_connect
    return _report_selftest(st, t0, verbose)


def _synth_working_set(values=None, objs=None):
    """合成一个最小工作集，专供 aggregate 的边界控制项使用。"""
    values = values or [{"status": "collected", "value": 1, "unit": "x"}]
    objs = objs or ["obj-0"]
    obs = []
    for i, obj in enumerate(objs):
        for v in values:
            e = {"id": "obs-%06d" % len(obs), "objectId": obj, "metricCode": "m.demo",
                 "observedAt": "2026-01-01T00:00:0%d" % (i % 10),
                 "provenance": {"source": "derived", "probe": "fixture", "collectedAt": "2026-01-01T00:00:00"}}
            e.update(v)
            obs.append(e)
    return {"objects": [], "relations": [], "observations": obs, "meta": {}}


# ----------------------------------------------------------------- 8) 与 freeze 的等价机检


def _compare_with_freeze(base_or_raw, label="", gather_path=None, health_path=None):
    """对同一份 raw 分别跑「本模块装配」与「直接 freeze.build」，逐项比对。

    比对项：``objects``/``relations`` 的 id 集合与 type 完全一致；``observations`` 逐条同键同值。
    """
    freeze = _load_freeze()
    if isinstance(base_or_raw, dict) and "raw" in base_or_raw:
        base = base_or_raw
        raw = base["raw"]
        snap = base["snapshot_id"]
        captured = (raw or {}).get("collected_at")
        prof = base.get("expected_profile") or "l3-only"
        url = base.get("collect_url")
        up = base.get("upstream")
        layers = base.get("layers")
        par = base.get("parallel")
    else:
        raw = base_or_raw
        snap = _derive_snapshot_id(raw)
        captured = raw.get("collected_at")
        prof = "l3-only"
        url = DEFAULT_PROVENANCE_URL
        up = freeze.UPSTREAM
        layers = raw.get("layers") or list(DEFAULT_LAYERS)
        par = False

    mine, extra = _normalize_full(raw, snapshot_id=snap, captured_at=captured, expected_profile=prof,
                                  collect_url=url, upstream=up, layers=layers, parallel=par,
                                  health_path=health_path)

    tmpdir = tempfile.mkdtemp(prefix="_m5_live_facts_")
    try:
        gp = gather_path or _dump_temp_json(raw, tmpdir, "gather.json")
        if health_path:
            hp = health_path
        else:
            hp = os.path.join(tmpdir, "health.json")
            with io.open(hp, "w", encoding="utf-8", newline="") as f:
                f.write(json.dumps(_DEFAULT_HEALTH, ensure_ascii=False, sort_keys=True))
        args = argparse.Namespace(
            collect_url=url, layers=",".join(str(x) for x in layers),
            parallel="true" if par is True or par_flag(par) else "false",
            expected_profile=prof, expect_simulated="false", snapshot_id=snap,
            out=None, map=None, schema=None, topology=None, upstream=up,
            inventory=None, allow_upstream_drift=True, health_json=hp, gather_json=gp,
            timeout=60, dry_run=True, force_write=True,
        )
        ref, _drop, _o, _r, _s = freeze.build(args)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    problems = []
    mine_obj = {o["id"]: o["type"] for o in mine["objects"]}
    ref_obj = {o["id"]: o["type"] for o in ref["objects"]}
    if set(mine_obj) != set(ref_obj):
        problems.append("objects id 集合不同：only_mine=%s only_ref=%s"
                        % (sorted(set(mine_obj) - set(ref_obj))[:6], sorted(set(ref_obj) - set(mine_obj))[:6]))
    else:
        diff = [k for k in mine_obj if mine_obj[k] != ref_obj[k]]
        if diff:
            problems.append("objects type 不同：%s" % diff[:6])

    mine_rel = {(r["type"], r["from"], r["to"]) for r in mine["relations"]}
    ref_rel = {(r["type"], r["from"], r["to"]) for r in ref["relations"]}
    if mine_rel != ref_rel:
        problems.append("relations 边集合不同：only_mine=%s only_ref=%s"
                        % (sorted(mine_rel - ref_rel)[:6], sorted(ref_rel - mine_rel)[:6]))

    if len(mine["observations"]) != len(ref["observations"]):
        problems.append("observations 条数不同：mine=%d ref=%d"
                        % (len(mine["observations"]), len(ref["observations"])))
    else:
        for a, b in zip(mine["observations"], ref["observations"]):
            if canonical_json(a) != canonical_json(b):
                problems.append("observations 逐条不同：\n  mine=%s\n  ref =%s"
                                % (canonical_json(a)[:300], canonical_json(b)[:300]))
                break
    if problems:
        raise LiveFactsError("与 freeze.build 归一化不等价（%s）：\n  - %s" % (label, "\n  - ".join(problems)))
    return {
        "label": label,
        "objects": len(mine_obj),
        "relations": len(mine_rel),
        "observations": len(mine["observations"]),
        "canonical_sha256": extra["canonical_sha256"],
    }


def equivalence_cli(base_path, gather_json=None, health_json=None, verbose=True):
    """对**磁盘上真实的冻结基底**跑等价检查（第 ④ 项的磁盘版）。"""
    base = load_base(base_path)
    if base["kind"] == "gather-envelope":
        if verbose:
            print("基底 %s 是原始信封：直接对同一份 raw 做等价比对" % base_path)
        info = _compare_with_freeze(base, label=base_path, gather_path=base_path, health_path=health_json)
        if verbose:
            print("objects=%d relations=%d observations=%d canonical=%s"
                  % (info["objects"], info["relations"], info["observations"], info["canonical_sha256"]))
            print("EQUIVALENCE PASS（%s）" % base_path)
        return 0

    ccf = base["ccf"]
    if not gather_json or not health_json:
        found_g, found_h = _discover_raw(ccf, os.path.dirname(os.path.abspath(base_path)))
        gather_json = gather_json or found_g
        health_json = health_json or found_h
    if not gather_json:
        print("EQUIVALENCE BLOCKED：基底 %s 的 canonical=%s 需要同一采集的 /tools/gather 原始回执才能复算，"
              "在 %s 里没有找到内容匹配的回执；请用 --gather-json 显式给出"
              % (base_path, base["canonical_sha256"][:16], os.path.dirname(os.path.abspath(base_path))))
        return 3
    if not health_json:
        print("EQUIVALENCE BLOCKED：找不到 health 回执（基底 x.healthSha256=%s）；请用 --health-json 显式给出"
              % (ccf.get("x", {}).get("healthSha256")))
        return 3

    prof = _match_profile(ccf.get("expected"))
    if not prof:
        print("EQUIVALENCE BLOCKED：基底 expected 与 v3-ccf-map.yaml 的任何 expected_profile 都不等，无法复算")
        return 3
    collector = ccf.get("collector") or {}
    kw = {
        "snapshot_id": ccf.get("snapshotId"),
        "captured_at": ccf.get("capturedAt"),
        "expected_profile": prof,
        "collect_url": collector.get("collectUrl"),
        "upstream": collector.get("upstream"),
        "layers": collector.get("layers"),
        "parallel": (ccf.get("payloadPolicy") or {}).get("parallel"),
        "health_path": health_json,
    }
    raw = _read_json(gather_json)
    _require_envelope(raw, where="equivalence")
    doc, extra = _normalize_full(raw, **kw)
    declared = ccf["integrity"]["canonicalSha256"]
    ok_canon = extra["canonical_sha256"] == declared

    # 追加一关：走公开入口 load_base(gather, ref=<CCF>) ⇒ 锚必须逐字复现
    # （这条同时覆盖 ref 继承 snapshotId / collector.* / payloadPolicy.parallel 的能力）
    problems = []
    ref_base = None
    try:
        ref_base = load_base(gather_json, expected_profile=prof, health_path=health_json, ref=base_path)
        ok_ref = ref_base["canonical_sha256"] == declared
    except LiveFactsError as exc:  # noqa: BLE001
        ok_ref = False
        problems.append("load_base(gather, ref=基底) 抛错，无法复现同源锚：%s" % exc)
    if ref_base is not None and not ok_ref:
        problems.append(
            "load_base(gather, ref=基底) 算出的 canonicalSha256=%s ≠ 基底声明值 %s"
            % (ref_base["canonical_sha256"], declared)
        )

    mine_obj = {o["id"]: o["type"] for o in doc["objects"]}
    ref_obj = {o["id"]: o["type"] for o in ccf["objects"]}
    if set(mine_obj) != set(ref_obj):
        problems.append("objects id 集合不同：only_mine=%s only_ref=%s"
                        % (sorted(set(mine_obj) - set(ref_obj))[:8], sorted(set(ref_obj) - set(mine_obj))[:8]))
    else:
        diff = [k for k in mine_obj if mine_obj[k] != ref_obj[k]]
        if diff:
            problems.append("objects type 不同：%s" % diff[:8])
    mine_rel = {(r["type"], r["from"], r["to"]) for r in doc["relations"]}
    ref_rel = {(r["type"], r["from"], r["to"]) for r in ccf["relations"]}
    if mine_rel != ref_rel:
        problems.append("relations 边集合不同：only_mine=%s only_ref=%s"
                        % (sorted(mine_rel - ref_rel)[:8], sorted(ref_rel - mine_rel)[:8]))
    ref_obs = {o["id"]: o for o in ccf["observations"]}
    mine_obs = {o["id"]: o for o in doc["observations"]}
    if set(ref_obs) != set(mine_obs):
        problems.append("observations id 集合不同：only_mine=%s only_ref=%s"
                        % (sorted(set(mine_obs) - set(ref_obs))[:8], sorted(set(ref_obs) - set(mine_obs))[:8]))
    else:
        diff = [k for k in sorted(ref_obs) if canonical_json(ref_obs[k]) != canonical_json(mine_obs[k])]
        if diff:
            k = diff[0]
            problems.append("observations 逐条不同（首个 %s）：\n    base=%s\n    mine=%s"
                            % (k, canonical_json(ref_obs[k])[:300], canonical_json(mine_obs[k])[:300]))

    if verbose:
        print("基底             = %s" % base_path)
        print("基底 kind        = %s（bytes=%d lf=%d sha256=%s）"
              % (base["kind"], base["fingerprint"]["bytes"], base["fingerprint"]["lf"],
                 base["fingerprint"]["sha256"]))
        print("gather 回执      = %s" % gather_json)
        print("health 回执      = %s" % health_json)
        print("expected_profile = %s（由基底 expected 反查得出）" % prof)
        print("snapshotId       = %s" % ccf.get("snapshotId"))
        print("canonical（基底） = %s" % declared)
        print("canonical（复算） = %s  match=%s" % (extra["canonical_sha256"], ok_canon))
        print("canonical(ref 入口) = %s  match=%s"
              % ((ref_base or {}).get("canonical_sha256"), ok_ref))
        print("objects          = mine %d / base %d" % (len(mine_obj), len(ref_obj)))
        print("relations        = mine %d / base %d" % (len(mine_rel), len(ref_rel)))
        print("observations     = mine %d / base %d" % (len(mine_obs), len(ref_obs)))
        if problems:
            for p in problems:
                print("  - %s" % p)
            print("EQUIVALENCE FAIL")
        else:
            print("EQUIVALENCE PASS（objects/relations/observations 逐条一致%s）"
                  % ("，canonicalSha256 也逐字一致" if ok_canon else "；★ 但 canonicalSha256 不一致"))
    if problems:
        return 1
    return 0 if ok_canon else 1


def _match_profile(expected):
    if not isinstance(expected, dict):
        return None
    doc = _load_yaml(MAP_PATH) or {}
    for name, prof in (doc.get("expected_profiles") or {}).items():
        cand = {"types": list(prof.get("types") or []),
                "metrics": list(prof.get("metrics") or []),
                "objectsMin": dict(prof.get("objectsMin") or {})}
        if prof.get("relationsMin"):
            cand["relationsMin"] = {str(k): int(v) for k, v in prof["relationsMin"].items()}
        if cand == expected:
            return name
    return None


def _discover_raw(ccf_doc, folder, max_candidates=12):
    """在基底同目录里找同源回执：health 用 ``x.healthSha256`` 逐字比对，gather 用 canonical 比对。"""
    if not os.path.isdir(folder):
        return None, None
    want_health = (ccf_doc.get("x") or {}).get("healthSha256")
    health = None
    files = sorted(os.listdir(folder))
    for name in files:
        p = os.path.join(folder, name)
        if not os.path.isfile(p) or os.path.getsize(p) > 4 * 1024 * 1024:
            continue
        try:
            text = _read_text_utf8(p)
        except (OSError, UnicodeDecodeError):
            continue
        if want_health and sha256_text(text) == want_health:
            health = p
            break
    candidates = []
    for name in files:
        p = os.path.join(folder, name)
        if not os.path.isfile(p) or p == health:
            continue
        if os.path.getsize(p) < 50 * 1024 or os.path.getsize(p) > 32 * 1024 * 1024:
            continue
        try:
            with io.open(p, "r", encoding="utf-8") as f:
                head = f.read(4096)
        except (OSError, UnicodeDecodeError):
            continue
        if '"all_facts"' not in head:
            continue
        candidates.append(p)
    if health is None:
        return None, None
    declared = ccf_doc["integrity"]["canonicalSha256"]
    prof = _match_profile(ccf_doc.get("expected"))
    collector = ccf_doc.get("collector") or {}
    if not prof:
        return None, health
    for p in candidates[:max_candidates]:
        try:
            raw = _read_json(p)
            _require_envelope(raw, where="discover")
            doc, _ = _normalize_full(
                raw,
                snapshot_id=ccf_doc.get("snapshotId"),
                captured_at=ccf_doc.get("capturedAt"),
                expected_profile=prof,
                collect_url=collector.get("collectUrl"),
                upstream=collector.get("upstream"),
                layers=collector.get("layers"),
                parallel=(ccf_doc.get("payloadPolicy") or {}).get("parallel"),
                health_path=health,
            )
        except LiveFactsError:
            continue
        if doc["integrity"]["canonicalSha256"] == declared:
            return p, health
    return None, health


# ----------------------------------------------------------------- 9) CLI


def build_parser():
    ap = argparse.ArgumentParser(
        prog="live_facts.py",
        description="M5 数据面封装层：采集 / 基底 / 注入 / 归一化 / 工作集 / 有界聚合（默认只读、默认不联网）",
    )
    ap.add_argument("--selftest", action="store_true", help="跑内置控制项（全程不联网）")
    ap.add_argument("--equivalence", action="store_true",
                    help="对磁盘上的冻结基底跑「与 freeze.py 归一化等价」机检")
    ap.add_argument("--base", default=None, help="--equivalence 的基底文件（CCF JSON 或原始信封）")
    ap.add_argument("--gather-json", default=None, help="显式指定同源 /tools/gather 回执")
    ap.add_argument("--health-json", default=None, help="显式指定同源 /health 回执")
    ap.add_argument("--fetch", action="store_true", help="真机只读采集一次（唯一出网路径）")
    ap.add_argument("--layers", default="gaussdb,os")
    ap.add_argument("--sections", default="")
    ap.add_argument("--url", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", default=None)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.selftest:
        return selftest()
    if args.equivalence:
        if not args.base:
            print("--equivalence 需要 --base <文件>")
            return 2
        try:
            return equivalence_cli(args.base, args.gather_json, args.health_json)
        except LiveFactsError as exc:
            print("EQUIVALENCE ERROR: %s" % exc)
            return 2
    if args.fetch:
        sections = [x.strip() for x in str(args.sections).split(",") if x.strip()]
        env = fetch_live(args.layers, sections=sections, url=args.url, timeout=args.timeout)
        text = json.dumps(env, ensure_ascii=False, indent=1)
        if args.out:
            with io.open(args.out, "w", encoding="utf-8", newline="") as f:
                f.write(json.dumps(env, ensure_ascii=False))
            print("已写出 %s（facts=%s）" % (args.out, env.get("total_facts")))
        else:
            print(text[:2000])
        return 0
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
