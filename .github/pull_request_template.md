<!--
Keep this pull request focused on one concern. Fill in every section; delete the
comments and any option that does not apply.
-->

## What changed

<!-- A short summary of the change, and the files or contracts it touches. -->

## Why

<!-- The problem this solves, or the requirement it satisfies. Link the issue if there is one. -->

## How it was tested

Paste the commands you actually ran and their results. The offline checks are:

```bash
python deploy/selfcheck.py
python shared/verify/gate_runner.py --offline --allow-partial --timeout 900 --json
python ci/check_offline_gates.py
python ci/check_evidence_files.py
pytest
```

<!--
State the outcome per command (exit code and, for gates, PASS / FAIL / BLOCKED).
`BLOCKED` (exit 2) means an asset or credential was missing and the check was not
measured — that is not a pass. If you ran anything that needs a live system, say
which one and with what kind of credential, and do not paste the credential.
Any command you did not run: say so explicitly rather than leaving it implied.
-->

- [ ] `python deploy/selfcheck.py` — result:
- [ ] `python shared/verify/gate_runner.py --offline --allow-partial --timeout 900 --json` — result:
- [ ] `python ci/check_offline_gates.py` — result:
- [ ] `python ci/check_evidence_files.py` — result:
- [ ] `pytest` — result:
- [ ] Other (module `--selftest`, scripts, manual checks) — describe:

## Checklist

- [ ] The change is limited to one concern, and unrelated reformatting is excluded.
- [ ] No credentials, tokens, private keys, real hostnames or node names, internal IP addresses, machine-specific absolute paths, or snapshots of a real environment were added. Example endpoints use documentation-safe values such as `192.0.2.1` and `https://example.org/`.
- [ ] Contracts and documentation were updated if behaviour changed — including the gate registry (`shared/model/gate-registry.yaml`) if a module under `shared/verify/` was added, since the coverage audit is fail-closed.
- [ ] If a gate was added or its meaning changed, it has been observed both passing and failing (or reporting `BLOCKED` for a missing asset), and its exit codes are documented.
- [ ] Anything that needs a live system is excluded from the default offline path and fails closed when its credential is absent.
- [ ] No existing file was reformatted, reordered, or deleted without a stated reason.
