# ci/Dockerfile —— offline CI job image (all offline-tier dependencies preinstalled)
#
# Why not `python:3.12-slim` + a per-job `pip install`: the gate runner, the service
# selftests and the pytest suite together need every dependency pinned below; baking them
# into an image keeps job startup deterministic and removes install flakiness/latency from
# the critical path.
#
# Build:
#   docker build -t cc-ops-ci:py312 -f ci/Dockerfile .
# Then point the job image at it (locally, or in whatever runner can pull from your registry).
#
# ★ Pinned on purpose: exact versions avoid "green today, red tomorrow" drift.
FROM python:3.12-slim

# ★ Index override: on some networks a mirror is several times faster than pypi.org.
#   Default is the public index; pass `--build-arg PIP_INDEX=...` to use a mirror.
ARG PIP_INDEX=https://pypi.org/simple

# Every third-party dependency of the offline tier (derived by import scan, see ci/README.md):
#   yaml / rdflib  → gates (rdflib is only used by the ontology TBox check)
#   pytest         → offline unit tests
#   fastapi/pydantic/uvicorn/starlette/httpx/requests → service selftests
#                    (cc_tools* use FastAPI + starlette.testclient; httpx is a
#                    runtime dependency of TestClient)
#   paramiko       → shared/deploy/smoke/e2e_smoke.py --selftest (needed at import time)
#   neo4j          → gates that talk to a graph database. When no database is reachable
#                    they must report BLOCKED (unmeasured), not FAIL (measured bad).
RUN pip install --no-cache-dir -i "$PIP_INDEX" \
        "PyYAML==6.0.3" \
        "rdflib==7.6.0" \
        "pytest==9.1.1" \
        "fastapi==0.136.3" \
        "pydantic==2.13.4" \
        "uvicorn==0.49.0" \
        "starlette==1.2.1" \
        "httpx==0.28.1" \
        "requests==2.34.2" \
        "paramiko==5.0.0" \
        "neo4j==6.2.0" \
    && python -c "import yaml,rdflib,pytest,fastapi,pydantic,uvicorn,starlette,httpx,requests,paramiko; print('deps ok')"
