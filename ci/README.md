# CI

This directory holds the pieces that are specific to continuous integration:

| File | Purpose |
|---|---|
| `check_offline_gates.py` | Contract check between the gate **registry** and an actual `gate_runner.py --offline` run (see below) |
| `check_evidence_files.py` | Checks that every path declared as *evidence* exists and is version-controlled. **Not part of the public CI** — see "What is deliberately not ported" |
| `Dockerfile` | Optional job image with the whole offline-tier dependency set preinstalled |

The CI that actually runs on this repository is **GitHub Actions**: `.github/workflows/ci.yml`
(jobs: `offline-registry-gates`, `service-selftests`, `selftest-drift-advisory`,
`pytest-offline`, `release-consistency`).

---

## 1. The gate-registry contract

The authority for what is a gate, and in which tier it belongs, is the **`class` field** of
`shared/model/gate-registry.yaml` — it is declaration-driven, and nothing in CI hardcodes a
gate list. Two tiers exist:

| Tier | Content | Blocks a merge? | Why |
|---|---|---|---|
| **offline** | registered gates with `class: offline`, the offline selftests, and the unit tests that need no credentials | ✅ yes | No network, no credentials, **deterministic** (same input ⇒ same verdict). Merge gates should only rest on deterministic criteria |
| **online** | `class: online` gates plus the `test_integrity.py` suites | ❌ no | They depend on real infrastructure (live service ports, graph databases, SSH, an orchestration console). Unreachable infrastructure is **normal**, and `BLOCKED ≠ FAIL` — gating merges on it would turn "the environment is down" into "the code is bad" |

### 1.1 Why `--allow-partial` is required, and why the checker exists anyway

`gate_runner.py --offline` evaluates only the offline gates; the rest land in *not evaluated*,
and the runner's default strict mode counts any unevaluated gate as overall exit code **2**.
Without `--allow-partial` the offline CI job could never be green.

That creates a **silent failure mode**: `--allow-partial` also accepts a *partial* run, so if
somebody moves a gate's `class` from `offline` to `online`, the gate quietly disappears from CI
while the exit code stays 0.

`ci/check_offline_gates.py` closes that hole: it compares the registry's `class: online` id set
against the run manifest's `not_evaluated` list, and requires `fail == 0`, `blocked == 0` and
`evaluated == <number of offline gates>`. It is deliberately three-valued:
`0` consistent / `1` inconsistent (and it names the difference) / `2` precondition unavailable
(registry or manifest unreadable).

**Changing a gate's `class` therefore turns CI red and names the gate.** That is intended:
a tier change must be visible.

---

## 2. Running the same checks locally

```bash
# dependency check + offline gate registry contract
python shared/verify/gate_runner.py --offline --allow-partial --timeout 900 --json
python ci/check_offline_gates.py

# unit tests (the two fail-closed test_integrity.py suites are excluded, see pyproject.toml)
python -m pytest

# offline selftests — each prints its own verdict; BLOCKED is not FAIL
python scheme1-kg/service/cc_tools.py --selftest
python scheme1-kg/service/cc_tools_kgt.py --selftest
python scheme2-onto/service/cc_tools_onto.py --selftest
python shared/collect/live_facts_service.py --selftest
python shared/verify/m5_judge.py --selftest
python shared/deploy/smoke/m5_run.py --selftest
python shared/deploy/smoke/e2e_smoke.py --selftest
python shared/deploy/check_fairness.py --selftest
python shared/deploy/dify/register_tools.py --selftest

# release consistency (packaging/deployment declarations)
python deploy/selfcheck.py
```

`ci/Dockerfile` builds an image with all of the above preinstalled:

```bash
docker build -t cc-ops-ci:py312 -f ci/Dockerfile .
```

---

## 3. What is deliberately not ported

- **`ci/check_evidence_files.py`** — its contract is "every `tmp/` path declared as evidence must
  exist and be tracked", checked against `shared/deploy/port/tmp-evidence-whitelist.txt`. This
  distribution ships no `tmp/` evidence on purpose, so the check cannot be green here. The script
  is kept for users who run a full private-style workspace; it is intentionally **not** wired into
  the public CI rather than being silenced with `continue-on-error`.
- **Online jobs** (live graph databases, SSH to real hosts, orchestration-console credentials) —
  they need infrastructure this repository does not ship.
- **`shared/deploy/dify/deploy_agents.py --selftest`** — it logs into a real orchestration
  console, so it is an *online* test. Note that "is this self-check offline?" must be decided in an
  environment with no `.env` file: the shared env loader auto-loads candidate `.env` files, which
  can silently put a self-declared-offline test on its online branch.

---

## 4. Known boundaries (read together with any green result)

1. **Green CI ≠ evaluation results.** The offline tier proves that the gates are self-consistent,
   that the declared surface matches the measured surface, and that selftests/unit tests pass. It
   does **not** prove diagnostic accuracy — that requires a full multi-arm evaluation run.
2. The offline gate run is a statement about the **declared, static and replay** surfaces; only the
   online tier produces readings from real infrastructure.
3. Three-state discipline throughout: **`BLOCKED` (unmeasured) is never reported as `PASS`, and
   never as `FAIL`.**
4. Evidence files are byte-copies of gate stdout, so their bytes are **platform-dependent**
   (CRLF on Windows, LF on Linux) and their hashes are **not comparable across platforms**. The
   registry records them as observations, not as pinned hashes.
