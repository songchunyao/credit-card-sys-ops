#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M5 正本校验器（S4a）：只用**磁盘上的固化正本**重放并复核，不构造、不落盘、不连网。

校验链（全部 fail-closed）
--------------------------
1. 正本存在且可解析；``schema == m5-input-facts/1``；
2. ``meta.base_ref`` 可解析（相对正本目录）且**基底指纹**与 ``meta.canonical_sha256`` 同源；
3. 每例：从正本里**逐字重放**注入 ⇒ ``working_set_sha256`` 必须与固化值逐字相同；
4. 每例：用 fault-modes.yaml 的 ``detect`` 复算**可确认现象集合**，必须等于 mapping 里登记集合；
5. ``m5-cases.recorded.yaml`` 的 50 条与正本 ``cases[]`` 一一对应（id / source_injection_id / snapshot_id）；
6. ``expect_objects`` 必须是**该例工作集里真实存在的 12 类对象 id**（不是猜的）；
7. ``evidence_facts`` 必须是**该例工作集里真实存在的观测 id**；
8. ``expect_root_cause``/``expect_path``/``expect_layer`` 与 mapping 定义性派生的真值一致；
9. 三个哈希齐备（基底 canonical / 工作集 / 固化文件自身 digest）且格式合法。

退出码：0=PASS，1=FAIL（有不一致），2=BLOCKED（缺件/读不到基底，无法判定）。

用法::

    python shared/verify/m5/m5_facts_check.py
    python shared/verify/m5/m5_facts_check.py --ledger <path> --cases <path>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
for _p in (os.path.join(ROOT, "shared", "collect"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_facts as LF          # noqa: E402
import yaml                      # noqa: E402
import m5_inject_build as MB     # noqa: E402

LEDGER_REL = "shared/model/m5-input-facts.yaml"
CASES_REL = "shared/model/m5-cases.recorded.yaml"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Blocked(Exception):
    pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="M5 正本校验器（只读）")
    ap.add_argument("--ledger", default=os.path.join(ROOT, LEDGER_REL))
    ap.add_argument("--cases", default=os.path.join(ROOT, CASES_REL))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    errs, blocked = [], []

    def rd(path, what):
        if not os.path.isfile(path):
            blocked.append("B-CHECK-MISSING %s 不存在（%s）" % (path, what))
            return None
        with open(path, "r", encoding="utf-8") as fh:
            try:
                return yaml.safe_load(fh)
            except Exception as exc:  # noqa: BLE001
                blocked.append("B-CHECK-PARSE %s 解析失败：%s" % (path, exc))
                return None

    ledger = rd(args.ledger, "固化事实正本")
    cases_doc = rd(args.cases, "判决用例正本")
    if ledger is None or cases_doc is None:
        for b in blocked:
            print("[BLOCKED] %s" % b)
        print("RESULT: BLOCKED  (ledger=%s cases=%s)" % (args.ledger, args.cases))
        return 2

    if ledger.get("schema") != LF.INPUT_FACTS_SCHEMA:
        errs.append("E-CHECK-SCHEMA 正本 schema=%r，应为 %r"
                    % (ledger.get("schema"), LF.INPUT_FACTS_SCHEMA))
    meta = ledger.get("meta") or {}
    base_ref = meta.get("base_ref")
    if not base_ref:
        blocked.append("B-CHECK-BASE-REF 正本 meta.base_ref 缺失 ⇒ 无法重放")
    else:
        base_path = os.path.normpath(os.path.join(os.path.dirname(args.ledger), base_ref))
        if not os.path.isfile(base_path):
            blocked.append("B-CHECK-BASE-REF base_ref=%r 解析为 %s，文件不存在" % (base_ref, base_path))
    if blocked:
        for b in blocked:
            print("[BLOCKED] %s" % b)
        print("RESULT: BLOCKED")
        return 2

    base = LF.load_base(base_path)
    fp = LF.fingerprint_file(base_path)
    if base.get("snapshot_id") != meta.get("snapshot_id"):
        errs.append("E-CHECK-BASE-SNAPSHOT 基底 snapshot_id=%r ≠ 正本 meta.snapshot_id=%r"
                    % (base.get("snapshot_id"), meta.get("snapshot_id")))
    if base.get("canonical_sha256") != meta.get("canonical_sha256"):
        errs.append("E-CHECK-BASE-CANON 基底 canonical_sha256=%r ≠ 正本 meta.canonical_sha256=%r"
                    % (base.get("canonical_sha256"), meta.get("canonical_sha256")))
    if not HEX64.match(str(meta.get("frozen_digest_sha256") or "")):
        errs.append("E-CHECK-DIGEST 正本 meta.frozen_digest_sha256 缺失或非 64 位十六进制")
    # 固化文件自身 digest 复算（先移除自身两键，与 m5_inject_build 写入时口径一致）
    recalc = dict(ledger)
    rmeta = dict(recalc.get("meta") or {})
    rmeta.pop("frozen_digest_sha256", None)
    rmeta.pop("frozen_digest_policy", None)
    recalc["meta"] = rmeta
    got_digest = hashlib.sha256(json.dumps(
        recalc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()
    if got_digest != meta.get("frozen_digest_sha256"):
        errs.append("E-CHECK-DIGEST 固化文件自身 digest 不复现：重算 %s ≠ 记录 %s"
                    % (got_digest, meta.get("frozen_digest_sha256")))

    modes, thresholds = MB.load_fault_modes()
    mode_by_code = {m["code"]: m for m in modes}
    cases = cases_doc.get("cases") or []
    cmeta = cases_doc.get("meta") or {}
    if cmeta.get("fault_injection_ledger") != LEDGER_REL:
        errs.append("E-CHECK-LEDGER-REF 用例正本 meta.fault_injection_ledger=%r ≠ %r"
                    % (cmeta.get("fault_injection_ledger"), LEDGER_REL))
    if cmeta.get("snapshot_ids") != [meta.get("snapshot_id")]:
        errs.append("E-CHECK-SNAPSHOT meta.snapshot_ids=%r ≠ [%r]"
                    % (cmeta.get("snapshot_ids"), meta.get("snapshot_id")))

    by_id = {str(c["id"]): c for c in cases}
    ledger_cases = {str(c["case_id"]): c for c in (ledger.get("cases") or [])}
    mapping = {str(m["case_id"]): m for m in (ledger.get("mapping") or [])}
    if set(by_id) != set(ledger_cases):
        errs.append("E-CHECK-1TO1 用例 id 集合与正本 cases[] 不一致：仅用例 %s ／ 仅正本 %s"
                    % (sorted(set(by_id) - set(ledger_cases))[:5],
                       sorted(set(ledger_cases) - set(by_id))[:5]))
    if set(mapping) != set(ledger_cases):
        errs.append("E-CHECK-MAPPING 正本 mapping 覆盖 %d 例 ≠ cases %d 例"
                    % (len(mapping), len(ledger_cases)))

    replayed = 0
    for cid in sorted(ledger_cases):
        rec = ledger_cases[cid]
        inj = [MB.replay_injection(i) for i in (rec.get("injections") or [])]
        try:
            ws = LF.build_working_set(base, {"case_id": cid, "injections": inj})
        except Exception as exc:  # noqa: BLE001
            errs.append("E-CHECK-REPLAY %s 重放被拒：%s" % (cid, str(exc)[:200]))
            continue
        got = ws["meta"]["working_set_sha256"]
        if got != rec.get("working_set_sha256"):
            errs.append("E-CHECK-WSSHA %s 重放 working_set_sha256=%s ≠ 固化值 %s"
                        % (cid, got, rec.get("working_set_sha256")))
        conf = sorted(MB.confirmable(ws, modes, thresholds))
        m = mapping.get(cid) or {}
        if conf != sorted(m.get("injected") or []):
            errs.append("E-CHECK-ORACLE %s 复算可确认集合 %s ≠ 登记注入集合 %s"
                        % (cid, conf, sorted(m.get("injected") or [])))
        replayed += 1

        c = by_id.get(cid)
        if c is None:
            continue
        if c.get("source_injection_id") != rec.get("source_injection_id"):
            errs.append("E-CHECK-ANCHOR %s source_injection_id=%r ≠ 正本 %r"
                        % (cid, c.get("source_injection_id"), rec.get("source_injection_id")))
        if c.get("snapshot_id") != meta.get("snapshot_id"):
            errs.append("E-CHECK-CASE-SNAP %s snapshot_id=%r ≠ %r"
                        % (cid, c.get("snapshot_id"), meta.get("snapshot_id")))
        real_obj = {str(o["id"]) for o in ws["objects"]}
        real_obs = {str(o["id"]) for o in ws["observations"]}
        prefixes = set((c.get("expect_objects") or []))
        for oid in prefixes:
            if str(oid) not in real_obj:
                errs.append("E-CHECK-EXPECT-OBJ %s expect_objects 项 %r 不在该例工作集对象集合里"
                            "（D29：必须是真实存在的 12 类 id）" % (cid, oid))
        for ev in (c.get("evidence_facts") or []):
            if str(ev) not in real_obs:
                errs.append("E-CHECK-EVIDENCE %s evidence_facts 项 %r 不在该例工作集观测集合里"
                            % (cid, ev))
        # 真值一致性（定义性派生：mapping 由同一复算写出）
        if m:
            if c.get("expect_root_cause") != m.get("expect_root_cause"):
                errs.append("E-CHECK-ROOT %s expect_root_cause=%r ≠ 派生真值 %r"
                            % (cid, c.get("expect_root_cause"), m.get("expect_root_cause")))
            if c.get("expect_layer") != m.get("expect_layer"):
                errs.append("E-CHECK-LAYER %s expect_layer=%r ≠ 派生真值 %r"
                            % (cid, c.get("expect_layer"), m.get("expect_layer")))
            if list(c.get("expect_path") or []) != list(m.get("expect_path") or []):
                errs.append("E-CHECK-PATH %s expect_path=%r ≠ 派生真值 %r"
                            % (cid, c.get("expect_path"), m.get("expect_path")))
            if sorted(c.get("expect_objects") or []) != sorted(m.get("expect_objects") or []):
                errs.append("E-CHECK-OBJ-DERIVE %s expect_objects=%r ≠ 派生真值 %r"
                            % (cid, c.get("expect_objects"), m.get("expect_objects")))
            root = c.get("expect_root_cause")
            if root != "NONE" and root in mode_by_code:
                if m.get("expect_layer") != mode_by_code[root]["layer"]:
                    errs.append("E-CHECK-LAYER-DERIVE %s 层别与 fault-modes.yaml 的 %s.layer 不一致"
                                % (cid, root))
            if root != "NONE":
                codes = [str(x) for x in (c.get("injected") or [])]
                if root not in codes:
                    errs.append("E-CHECK-ROOT-DECL %s expect_root_cause=%r 不在 injected=%r 里"
                                % (cid, root, codes))

    print("[check] 基底   %s" % base_path)
    print("[check] 基底   指纹 bytes=%d lf=%d sha256=%s" % (fp["bytes"], fp["lf"], fp["sha256"]))
    print("[check] 基底   canonical_sha256=%s snapshot=%s"
          % (meta.get("canonical_sha256"), meta.get("snapshot_id")))
    print("[check] 正本   固化 digest=%s" % meta.get("frozen_digest_sha256"))
    print("[check] 重放   成功 %d / %d 例（逐例 working_set_sha256 + 现象复算）"
          % (replayed, len(ledger_cases)))
    for e in errs:
        print("[FAIL] %s" % e)
    if errs:
        print("RESULT: FAIL  (ERROR=%d replayed=%d cases=%d)" % (len(errs), replayed, len(cases)))
        return 1
    print("RESULT: PASS  (ERROR=0 replayed=%d cases=%d ledger_cases=%d)"
          % (replayed, len(cases), len(ledger_cases)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
