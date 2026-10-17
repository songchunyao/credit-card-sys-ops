# Contributing to credit-card-sys-ops

Thanks for your interest in this project. This document explains how to set up a
development environment, which checks a change is expected to pass, and what must
never be committed.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Please read [SECURITY.md](SECURITY.md) before reporting anything that looks like a
vulnerability.

## Scope of the repository

This repository is the offline-verifiable part of an LLM-agent operations and
diagnosis framework for a credit-card transaction stack. It ships two carrier
schemes (a knowledge-graph scheme backed by Neo4j, and an ontology scheme backed
by RDF triples and SHACL shapes served through GraphDB) plus a variant of the
knowledge-graph scheme, all sharing one offline gate registry, one evidence-file
contract, and one release-consistency self-check.

Some parts referenced by this repository are deliberately not shipped: see
[NOTICE](NOTICE). In particular the base snapshot `tmp/ccf_live5.json` is
captured from a live environment, and the `async_gaussdb` driver is not publicly
distributable; the collect-layer source tree, by contrast, *is* part of this
repository. A
change that depends on those assets will report `BLOCKED` in a bare checkout —
that is the intended behaviour, not a bug to work around.

## Development environment

Requirements:

- **Python 3.12 or newer.** The project metadata declares
  `requires-python = ">=3.12"`.
- Git, and a shell able to run the commands below.

Create an isolated environment and install the dependency groups:

```bash
python3.12 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install ".[core,test]"
```

Notes on installation:

- This repository has **no installable package structure**. Its top level is a
  set of directories (`shared/`, `scheme1-kg/`, `scheme2-onto/`, `deploy/`,
  `ci/`), not an importable package, so `[project].dependencies` is intentionally
  empty and everyone installs third-party libraries by module name. `pip install
  -e .` therefore installs no project code; use it only if you want pip to
  resolve the extras for you. `pip install ".[core,test]"` is the recommended
  command.
- **Do not** run `pip install ".[gaussdb]"`. The `gaussdb` extra names a driver
  that has no public distribution, so the command fails by design. Supply your own
  interface-compatible driver importable as `async_gaussdb` only if you actually
  need the collection path. Without it, the affected collection and gate paths
  report `BLOCKED` (see the exit-code discipline below).

## Offline checks

These are the checks a pull request is expected to pass. They are offline: they
need no database, no cluster, and no credentials.

```bash
# 1. Release-consistency static self-check (standard library only).
python deploy/selfcheck.py

# 2. The offline gate registry.
python shared/verify/gate_runner.py --offline --allow-partial --timeout 900 --json

# 3. Aggregate offline gate check and evidence-file check.
python ci/check_offline_gates.py
python ci/check_evidence_files.py

# 4. Unit tests (offline subset; online files are excluded by default).
pytest
```

Many modules expose a self-test entry point that exercises that module in
isolation without a live system, for example:

```bash
python scheme1-kg/service/cc_tools.py --selftest
python scheme1-kg/service/cc_tools_kgt.py --selftest
python scheme2-onto/service/cc_tools_onto.py --selftest
python shared/verify/gate_runner.py --selftest
```

One self-test (`shared/verify/m5_judge.py --selftest`) legitimately takes several
minutes of CPU time. Do not lower the gate timeout below a few hundred seconds to
make it finish faster; raise the timeout instead if your machine is slow.

### Exit-code discipline (important)

The tooling uses a tri-state exit convention:

| Exit | Meaning |
| ---- | ------- |
| `0`  | `PASS` — measured and correct |
| `1`  | `FAIL` — measured and wrong |
| `2`  | `BLOCKED` — could not be measured (missing asset, missing credential) |

A missing asset is `BLOCKED`, never `PASS`, and precedence is
`BLOCKED > FAIL > PASS`. Do not "fix" a `BLOCKED` result by fabricating or
committing the asset it needs. If your change introduces a new gate, it must
declare which of the three states it can emit.

### Gate registry coverage contract

`shared/model/gate-registry.yaml` must contain exactly one entry for every Python
module under `shared/verify/`, with a `kind` of either `gate` or `library`.
Adding a module there without registering it makes the coverage audit exit `1`:

```bash
python shared/verify/gate_runner.py --audit-coverage
```

Update the registry in the same pull request that adds the module.

### Tests that need a live system

Two integration tests are excluded from the default `pytest` run because they
require live services and real credentials:

- `scheme1-kg/tests/test_integrity.py` needs a reachable Neo4j and reads
  `CC_NEO4J_PASSWORD` at import time.
- `scheme2-onto/tests/test_integrity.py` needs a reachable GraphDB
  (`CC_GRAPHDB_URL`) and a real dataset.

They are script-style tests: run them directly, with your own credentials and
endpoints, and read the exit code (`0` = `PASS`, `1` = `FAIL`, `2` = `BLOCKED`).
Without the corresponding credential they fail closed rather than silently
passing. Please do not "fix" that by making them skip quietly, and do not enable
them by default — a bare checkout must stay runnable.

## Branch, commit and pull-request conventions

- Branch off the default branch. Use a short, descriptive name with a category
  prefix, e.g. `fix/gate-registry-audit`, `feat/onto-shape-check`,
  `docs/contributing-setup`.
- Write commit subjects in the imperative mood, under ~72 characters, optionally
  with a category prefix (`fix:`, `feat:`, `docs:`, `test:`, `chore:`,
  `refactor:`). Explain the *why* in the body when the change is not obvious.
- Keep a pull request focused on one concern. Unrelated reformatting makes review
  harder and is often rejected.
- Fill in the pull-request template, including the commands you actually ran.
- If your change alters observable behaviour, a contract, a gate's meaning, or a
  documented command, update the corresponding documentation and the
  machine-readable contract in the same pull request. Documentation that
  contradicts the code is treated as a defect.
- New or changed gates should come with evidence that they can both pass and
  fail; a gate that has never been observed failing is not yet a gate.
- Comments and docstrings in this codebase are written in Chinese; documentation
  under `docs/` is Chinese as well, while the top-level README is English. New
  governance and community files are expected to be English. Match the existing
  language of the file you are editing rather than translating it.

## Never commit credentials or internal coordinates

This repository ships no credentials by design. Before you push, make sure your
change contains none of the following:

- `.env` files or any credential file (use the committed `*.example` templates as
  the shape only, and keep your filled-in copy local and untracked).
- Passwords, API tokens, private keys, certificates, SSH material, webhook URLs
  containing secrets, or connection strings with embedded credentials.
- Real hostnames, internal IP addresses, node names, cluster names, or absolute
  paths from any machine — including your own developer machine.
- Snapshots or exports captured from a real environment (data dumps, live
  inventory files, logs that quote real output).

When you need a placeholder, use documentation-safe values: `192.0.2.1`–`192.0.2.254`
for example addresses, and `https://example.org/` for example IRIs and domains.

If you find a credential that has already been committed, treat it as a security
issue: report it as described in [SECURITY.md](SECURITY.md) rather than opening a
public issue, and rotate the credential.

## Reporting security issues

Do **not** open a public issue for a suspected vulnerability. Follow the private
reporting process in [SECURITY.md](SECURITY.md).

## License

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). Unless you explicitly state otherwise, any contribution you
intentionally submit for inclusion in the project is provided under the terms of
that License, without additional terms or conditions (Section 5 of the License).
