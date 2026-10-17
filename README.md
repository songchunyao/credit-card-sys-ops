# Cross-Layer Fault Diagnosis for a Credit-Card Transaction Stack

An LLM-agent operations and diagnosis framework for a credit-card transaction stack — a Kubernetes-hosted application tier backed by GaussDB, with Neo4j and GraphDB as the two carrier stores — built around **three comparable tool-face schemes** that are deliberately measured against one another rather than merely described.

---

## 1. What problem this solves

A single credit-card incident — transactions timing out, say — usually crosses layers: the application sees a saturated connection pool, the container is CPU-throttled, and the database runs a slow query that holds sessions. Pointing at one layer yields a root cause that is true and useless.

This repository holds the part of that work that is **engineerable and comparable**: a neutral fact model of the stack, a set of queryable tool faces over it, and a gate layer that keeps those faces actually comparable.

The research question is: *given the same frozen set of facts, does the choice of knowledge representation change diagnostic quality?* To make that answerable, the same case can be expressed against three schemes — the third with two variants:

| Arm | Name | Representation | Role |
|---|---|---|---|
| Scheme 1 | **KG-Q** (port `8301`) | Neo4j property graph: typed labels, typed relationships, Cypher | Graph traversal and evidence tracing over an explicit topology plus a fault/cause graph |
| Scheme 2 | **ONTO-Q** (port `8302`) | OWL ontology + SHACL shapes, SPARQL | The same facts with formal semantics: axioms, domains and ranges, inverse relations, shape validation |
| Scheme 3 | **KG-T** (port `8303`) | Direct KG reference — declarative reads over the same graph, **no rule layer** | A control arm: the agent sees graph structure but is not handed the curated diagnosis tool set |
| Scheme 3b | **KG-T-dyn** (port `8305`) | Direct KG reference, free-form query | The same control arm with the query surface opened up |

The arms are **A/B-comparable by construction**, not by convention. Everything that makes the comparison fair is mechanised:

* **Tool-face parity.** `shared/model/tools-setb.yaml` is the single declaration of the shared tool face, and `shared/verify/toolface_diff.py` diffs both implementations against it. `shared/verify/toolset_parity_gate.py` and `shared/verify/f5_kgt_gate.py` guard the KG-T face; `shared/verify/parity.py` and `shared/verify/t10_native_parity.py` are the cross-arm comparison gates.
* **Fairness gates.** `shared/deploy/check_fairness.py` enforces that the arms get the same budget, the same case set and the same answer shape.
* **Purity gates.** `shared/model/purity-contract.yaml` plus `shared/verify/purity_gate.py` assert six faces of separation — dependency, runtime endpoint, semantics, vocabulary, shared-layer neutrality and measurement — so an arm cannot silently borrow the other carrier's reasoning.
* **Layering.** `tests/static/test_layer_boundary.py` pins the one-way data flow with an AST check, because a reverse dependency is nearly invisible at runtime but destroys comparability.

---

## 2. Architecture

```
  Orchestration layer -- Dify agents
    main arms   cc-kg-agent, cc-onto-agent      (providers cc-kg-tools / cc-onto-tools)
    KG-T arms   cc-kgt-agent, cc-kgt-dyn-agent
    free arm    cc-kgt-free-agent
    repeat arms cc-kg-r-agent, cc-onto-r-agent
        |  HTTP tool calls, uniform envelope
        v
  Tool faces (five services)
    KG-Q        :8301  property-graph traversal + cause ranking    (Scheme 1)
    ONTO-Q      :8302  OWL/SHACL query + cause ranking             (Scheme 2)
    KG-T        :8303  direct graph reads, curated, no rule layer  (Scheme 3)
    live-facts  :8304  carrier-neutral "read the current reading"
    KG-T-dyn    :8305  direct graph reads, free-form query         (Scheme 3 variant)
        |  neutral Plan (shared/project/)
        v
  Unified fact layer
    CCF -- Common Context Facts: a frozen, content-addressed snapshot
    produced by shared/collect/freeze.py
        |
        v
  Collect layer  :8187  POST /tools/gather
    GaussDB probes, container/host probes, application probes
```

The ports above are the repository's own configured defaults, not deployment specifics, and every one is overridable through the environment (section 5).

Packaging note: **five application images** but **six application services** in compose — `kgt` and `kgt-dyn` share the `kgt` image. `deploy/selfcheck.py` asserts this coverage.

### The fact layer

`shared/collect/freeze.py` freezes an observation set into a **CCF** snapshot with a canonical content hash, so every consumer reads the exact same bytes. `shared/project/` parses a CCF into a **neutral Plan** that both projectors consume — that shared parse is what makes the two carriers comparable at all. The shape is declared by `shared/model/ccf.schema.json`, `shared/model/v3-ccf-map.yaml` and `shared/collect/v3-fact-inventory.json`; `shared/scope/cc_scope.py` holds the scope guard.

---

## 3. Quick start (offline — no cluster, no database, no credentials)

Requirements: **Python 3.12 or newer**. The offline tier needs only `PyYAML` and `rdflib`; `pytest` is needed for the unit-test step alone. Dependency groups are declared in [`pyproject.toml`](pyproject.toml), so one command installs the offline tier in full:

```bash
pip install ".[core,test]"
```

Nothing below touches the network or a database. Commands are shown for a POSIX shell; on Windows, use the same interpreter and paths.

```bash
# 0) configuration template (see section 5). Most commands below need no
#    configuration at all, but a few selftests read deploy/.env, so create it:
cp deploy/.env.example deploy/.env      # .env is gitignored; never commit a real one

# 1) static release self-check
python deploy/selfcheck.py

# 2) layering guard (AST-based, offline)
python tests/static/test_layer_boundary.py

# 3) offline tool-face selftests
python scheme1-kg/service/cc_tools.py --selftest            # KG-Q face
python scheme2-onto/service/cc_tools_onto.py --selftest     # ONTO-Q face
python scheme1-kg/service/cc_tools_kgt.py --selftest        # KG-T face
python shared/collect/live_facts.py --selftest              # fact layer, no network

# 4) gate layer and fairness
python shared/verify/gate_runner.py --selftest
python shared/deploy/check_fairness.py --selftest
python shared/verify/toolface_diff.py --selftest
python shared/verify/guard_conformance.py --selftest

# 5) orchestration and end-to-end smoke (offline, fake transport)
python shared/deploy/smoke/m5_run.py --selftest
python shared/deploy/smoke/e2e_smoke.py --selftest
python shared/verify/m5_judge.py --selftest

# 6) the offline unit tests (~4 min, 72 tests)
python -m pytest -q
```

Prefer the bare `python -m pytest`: [`pyproject.toml`](pyproject.toml) declares `testpaths` and, more importantly, `--ignore`s the two online integrity suites, which would otherwise abort collection (section 6). Naming test files explicitly on the command line still works, but you must then remember the exclusions yourself.

`--selftest` is the project's standard "prove the tool face is self-consistent without a database" entry point. Exit codes are three-state throughout the project: **`0` = PASS, `1` = FAIL, `2`/`3` = BLOCKED**, where BLOCKED means *not measured* — never *failed*.

### Honest status of the above in this export

Most of it passes here. A few entries need inputs that are **not** shipped — see section 8 — and this table says so rather than pretending otherwise. Measured in this tree:

| Command | Result here |
|---|---|
| `python deploy/selfcheck.py` | **PASS** (exit 0): `断言 22 条，FAIL=0`. The collect-layer `COPY` sources are present and checked; the script classifies the one source still absent (`tmp/ccf_live5.json`, real-environment data) as a release-time input, so its absence is reported, not counted as failure |
| `python shared/collect/live_facts_service.py --selftest` | **BLOCKED** (exit 3): `OK=37 BAD=0 BLOCKED=1` — BLOCKED ≠ FAIL |
| `python shared/verify/purity_gate.py --selftest` | FAIL (exit 1): `selftest S05 假设失效：沙箱里没有任何拨号见证证据副本` — its dial-out witness evidence is not shipped |
| `python shared/verify/m5_cases_gate.py --selftest` | FAIL (exit 1): `ok=3 ng=47` — its 47 negative controls expect fixtures under `tmp/` that are not shipped |
| `python shared/collect/m5_inject_build.py --selftest` | FAIL (exit 1): `基底不存在：tmp/ccf_live5.json` — the live base snapshot is not shipped |
| `python shared/scope/cc_scope.py --selftest` | USAGE (exit 2): `--base` is **required**, e.g. `python shared/scope/cc_scope.py --base shared/model --selftest` |
| `python shared/deploy/dify/deploy_agents.py --selftest` | FAIL (exit 1): `缺少环境变量 DIFY_BASE_URL` — this is the **online** arm by design |
| `python ci/check_offline_gates.py` | BLOCKED (exit 2) unless `gate_runner.py … --json` has run first: it reads `run_manifest.json` |

Everything else in the quick start passes here: the layering guard (`L1–L7`, 0 problems), the three tool faces (`25/25`, `111/111`, `132/132`), the fact layer (`58/58`), `gate_runner.py` (`29/29`), `check_fairness.py` (`BAD=0`), `toolface_diff.py` (`23/23`), `guard_conformance.py` (`20/20`), `m5_run.py` (`99/99`), `m5_judge.py` (`FAIL=0 BLOCKED=0`), `e2e_smoke.py`, `register_tools.py` (`41/41`), and the two `deploy/` helpers.

---

## 4. Containerised packaging and deployment

The full runbook lives in [`deploy/README.md`](deploy/README.md): containerised packaging, host prerequisites, the one-click installer and the verifier. In summary:

* **Five application images** built from the top-level `Dockerfile.*` files, plus `ci/Dockerfile` for the CI runner image.
* **An offline bundle.** `deploy/make_bundle.sh` produces a directory holding the gzipped image set and a **`MANIFEST.sha256`**, verifiable end to end with `sha256sum -c MANIFEST.sha256` on an air-gapped host.
* **Two deployment modes.** `deploy/install.sh --bundle <dir> --mode attached` attaches to carriers already running on the deployment host; `--mode standalone` brings its own carrier containers via `deploy/compose.standalone.yml`. `deploy/compose.yml` is the attached-mode file, with an `extra_hosts` mapping so containers can reach those carriers.
* **The acceptance matrix.** `deploy/verify_deploy.py` returns **PASS / FAIL / BLOCKED**, where BLOCKED means *not measured* — a project-wide rule: a missing credential or an unreachable host must never be reported as a failure of the thing under test.
* `deploy/credentials_check.py` pre-flights credentials and `deploy/create_graphdb_repo.py` provisions the ontology store.

### Continuous integration

CI mirrors the same split, and only the offline half is portable. The public workflow is [`.github/workflows/ci.yml`](.github/workflows/ci.yml), which runs on `ubuntu-latest` with Python 3.12 against public PyPI, with no Docker, no secrets and no self-hosted runner:

| Job | Blocking? | What it runs |
|---|---|---|
| `offline-registry-gates` | Yes | `gate_runner.py --offline --allow-partial --json`, then `ci/check_offline_gates.py` |
| `service-selftests` | Yes | The offline `--selftest` entry points, as a matrix |
| `selftest-drift-advisory` | No (advisory) | Selftests whose offline status can drift, reported without gating |
| `pytest-offline` | Yes | `python -m pytest` |
| `release-consistency` | Yes | `python deploy/selfcheck.py` |

`ci/README.md` documents the wider, non-public tier split: a blocking offline tier, plus an **online tier** that runs manually and never blocks because it needs real carrier hosts and console credentials. The online jobs and the release/deploy stages are **deliberately not ported** here — they need internal hosts, a private registry and credentials, so a public clone could neither run them nor should it contain them. The same reason excludes `ci/check_evidence_files.py`: it asserts that evidence paths declared in `shared/model/gate-registry.yaml` exist, and this export intentionally ships no `tmp/`.

`ci/check_offline_gates.py` *is* ported, and it is the piece that matters most: it compares the registry's `class: online` list against the runner's `not_evaluated` list word for word, so a gate silently reclassified out of the offline tier turns the pipeline red instead of quietly disappearing.

---

## 5. Configuration

All runtime configuration is environment-driven, templated by [`deploy/.env.example`](deploy/.env.example), with a credentials template at `deploy/credentials.example`. Main key groups:

| Group | Examples | Notes |
|---|---|---|
| Carrier endpoints | `CC_NEO4J_HTTP_URL`, `CC_NEO4J_URI`, `CC_GRAPHDB_URL` | Graph and ontology stores |
| Credentials | `CC_NEO4J_PASSWORD`, `CC_SSH_PASSWORD`, `GAUSSDB_SSH_PASSWORD` | Supplied by the operator; no values are inventoried here |
| Credential sourcing | `CC_NEO4J_ENV_FILE` | Path to an existing environment file to read a password from |
| Orchestration | `DIFY_BASE_URL`, `DIFY_EMAIL`, `DIFY_PASSWORD` | Dify console; used only by the online tier |
| Feature flags and knobs | `M5_CASE_ID`, `CC_KGT_DIR`, `CC_AUDIT_LOG`, `CC_TOOLS_PYTHON` | Case selection, per-face run directory, audit sink, interpreter |
| Ports | `8301`–`8305`, `8187` | Contract defaults; override per deployment |

> **Never commit real values.** `.env` and `deploy/credentials` are gitignored; only the `.example` templates belong in version control. `deploy/selfcheck.py` assertion `S1` checks that `.dockerignore` keeps `.env` out of the images, and `S6` scans the release artefacts for hard-coded secrets.

---

## 6. Tests: what runs offline vs what needs a live stack

| Tier | Scope | Runs offline? |
|---|---|---|
| Static layering guard | `tests/static/test_layer_boundary.py` (L1–L7) | Yes |
| Contract / mapping / parity / purity gates | `shared/verify/*.py` marked `class: offline` in `shared/model/gate-registry.yaml` | Yes |
| Tool-face unit tests | `scheme1-kg/tests/test_cc_tools*.py`, `scheme2-onto/tests/test_cc_tools_onto.py` | Yes |
| Selftests | The `--selftest` flags in section 3 | Yes — they assert that they make no network calls |
| Cross-arm graph integrity | `scheme1-kg/tests/test_integrity.py`, `scheme2-onto/tests/test_integrity.py` | **No** |
| Live end-to-end and fault injection | `fi-e2e/` (`fi.py`), live mode of `shared/deploy/smoke/e2e_smoke.py` | **No** |

### `test_integrity.py` is intentionally fail-closed

Both `test_integrity.py` files read `CC_NEO4J_PASSWORD` at **import time** and call `sys.exit(...)` when it is unset, rather than falling back to a built-in default password. Two consequences:

1. They **must be excluded from default pytest collection**, and [`pyproject.toml`](pyproject.toml) does exactly that, with `--ignore=scheme1-kg/tests/test_integrity.py` and `--ignore=scheme2-onto/tests/test_integrity.py` in `addopts`. The exclusion has to be `--ignore` rather than a marker or `--deselect`: both of those act *after* collection, whereas this `sys.exit` fires at *import* time — the file is imported and the process is gone before pytest can read a marker or a node id.
2. They belong to the CI **online** tier, never to the blocking offline tier. The rule is: *a missing credential is always BLOCKED, never PASS.*

`shared/model/gate-registry.yaml` is the declaration surface for all of this: every `shared/verify/*.py` must have exactly one `kind: gate` or `kind: library` entry, and `gate_runner.py --audit-coverage` fails closed on an unregistered file. Move a gate from `class: offline` to `class: online` and `ci/check_offline_gates.py` turns the pipeline red and names it — tier changes are visible, not silent.

---

## 7. Repository layout

| Path | Contents |
|---|---|
| `scheme1-kg/` | Scheme 1 (KG-Q): ontology and Cypher assets, projectors, the `cc_tools.py` tool face and its tests |
| `scheme2-onto/` | Scheme 2 (ONTO-Q): OWL/SHACL assets, the GraphDB projector, the `cc_tools_onto.py` tool face and its tests |
| `shared/` | Everything both schemes share — see below |
| `deploy/` | Containerised packaging, `.env` template, installer, bundle builder, acceptance verifier, runbook |
| `tests/` | Cross-cutting tests; `tests/static/test_layer_boundary.py` is the layering guard |
| `ci/` | Pipeline tier definitions, offline-tier consistency checks, runner image |
| `fi-e2e/` | End-to-end fault-injection harness: inject a known fault, then score whether the diagnosis matched the injected root cause |
| `docs/` | The three design documents included in this export (see section 9) |
| `.github/` | Issue forms, pull-request template, Dependabot configuration, and the offline workflow in `.github/workflows/ci.yml` |
| `Dockerfile.collect`, `Dockerfile.kg`, `Dockerfile.onto`, `Dockerfile.kgt`, `Dockerfile.live-facts` | The five application images |
| `pyproject.toml` | Dependency groups (`core`, `test`, `gaussdb`) and the pytest configuration |
| `LICENSE`, `NOTICE`, `SECURITY.md` | Apache-2.0 text, third-party attribution and exclusions, security policy |
| `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md` | Contribution guide, community standards, release history |

Inside `shared/`: `collect/` (fact gathering, CCF freezing), `project/` (the neutral Plan parser), `model/` (YAML and JSON contracts), `verify/` (the gates), `deploy/` (Dify orchestration, prompts, systemd units, log rotation), `mapping/` (generated mapping tables) and `scope/` (the scope guard).

Two naming disciplines are load-bearing and are enforced mechanically:

* `shared/` may never import either scheme, and the schemes may never import each other — otherwise the arms stop being independent and the comparison collapses (`test_layer_boundary.py` L2/L3).
* Project-owned namespace literals (the `CC_` labels, the ontology IRI base) may live only in `shared/mapping/`; everything else must read them from data (`test_layer_boundary.py` L6) — *the mapping is data, not code*.

---

## 8. Not included in this repository

This is a trimmed export, not the full working repository. The following are absent unless noted otherwise:

**(a) The collect-layer source tree.** It is part of this repository (formerly developed in a separate repository of the same author); `Dockerfile.collect` copies `dist/collect-layer/` and `shared/deploy/port/collect-layer.manifest.yaml` records its per-file bytes/sha256 fingerprints. The packer `shared/deploy/port/pack_collect_layer.py --verify` re-checks them and reports BLOCKED (not PASS) when the tree is absent.

**(b) Most of the internal design and decision documentation.** Only three of the project's documents are in this export. The numbering the code refers to (`docs/01`, `docs/02`, …) points at a larger set maintained separately.

**(c) Acceptance criteria and execution evidence.** The `fi-e2e/` harness, the gate registry and the CI tier definitions are here, but the recorded run evidence, the acceptance-criteria text and the cross-arm results are not. The gate registry deliberately does not pin gate file hashes, so a green offline tier proves *the gates are self-consistent*, not that diagnosis is accurate.

**(d) The live base snapshot `tmp/ccf_live5.json`.** It is referenced as the base snapshot of `shared/model/m5-input-facts.yaml` and is captured from a real deployment, so it is real-environment data rather than project source. Paths that need it fail closed instead of silently substituting an empty snapshot; capture your own.

**(e) The `async_gaussdb` driver.** The GaussDB collection path imports a non-public asynchronous driver that is not distributed here and has no public package under that name. It is declared as an optional extra in `pyproject.toml`, intentionally with no wheel to install: bring an interface-compatible implementation of your own. Until then the affected collection and gate paths report BLOCKED by design.

**(f) The third-party services themselves.** Neo4j, Ontotext GraphDB and Dify are consumed as container images or as services you run, never vendored — they keep their own licences and terms. The Dify plugin fork under `shared/deploy/plugins/neo4j_cypher_query/` is likewise an upstream work that ships no licence of its own; see [`NOTICE`](NOTICE) for the full list.

**(g) Any credential**, and the evidence files some selftests expect under `tmp/`. Several offline selftests therefore report BLOCKED or FAIL here; the table in section 3 lists exactly which and why.

**To use this repository you must supply** a `deploy/.env` built from `deploy/.env.example`; credentials for your carriers; an `async_gaussdb`-compatible driver if you want the GaussDB collection path; and, beyond the offline tier, a GaussDB instance, two carrier stores and a Dify console.

---

## 9. Chinese-language documentation

The design documentation under `docs/` is written in Chinese. Three documents are included:

| Document | Filename | Content |
|---|---|---|
| 01 · Scenario modelling and scope | [`docs/01-场景建模与范围定义.md`](docs/01-场景建模与范围定义.md) | Naming rules, the four-layer object catalogue (application / runtime / data / derived), the relation catalogue, the fault modes, the causal edges, and the detailed fault-chain use cases |
| 13 · Industry methods and open-source survey | [`docs/13-业界方法与开源项目调研.md`](docs/13-业界方法与开源项目调研.md) | A survey of cross-layer root-cause analysis: modelling paradigms, standards and semantic conventions, agent-era frameworks and benchmarks, plus an arm-by-arm mapping to prior art and a list of pitfalls |
| Appendix A · ORSD | [`docs/04-appendix-ORSD.md`](docs/04-appendix-ORSD.md) | The Ontology Requirements Specification Document: competency questions across six families, the derived vocabulary (types, object properties, metrics, fault modes, causal edges) and an explicit gap list |

The scenario document is the best entry point for the domain vocabulary, and the survey explains *why* the three schemes look the way they do. The remaining documents, and the numbering the source code references, are maintained separately.

---

## 10. License and attribution

* Licensed under the **Apache License 2.0**. See [`LICENSE`](LICENSE).
* Third-party attribution and the components this repository deliberately does not ship are in [`NOTICE`](NOTICE). In particular, the **collect layer is the maintainer's own work**: it was formerly developed in a separate repository of the same author (revision `8dcdefd`, 2026-09-28) and is now part of this repository and of the public release under the same Apache-2.0 terms. See `shared/deploy/port/collect-layer.manifest.yaml` for its provenance and per-file fingerprint record. Neo4j, Ontotext GraphDB and Dify are used as services and images, not redistributed.
* Security policy and private vulnerability reporting are in [`SECURITY.md`](SECURITY.md). Please report suspected vulnerabilities privately.
* [`CONTRIBUTING.md`](CONTRIBUTING.md) covers the development environment and which checks a change is expected to pass; [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) applies to all project spaces; [`CHANGELOG.md`](CHANGELOG.md) records releases.
