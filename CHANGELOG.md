# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

Initial public release preparation:

- Added the open-source governance files: [LICENSE](LICENSE) (Apache License 2.0),
  [NOTICE](NOTICE), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
  [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and this changelog.
- Added repository and hosting configuration: `.editorconfig`, `.gitattributes`,
  GitHub issue forms and pull-request template, and a Dependabot configuration
  for the `pip` and `github-actions` ecosystems.
- Extended `.gitignore` with the requested environment-file, bytecode-cache,
  virtualenv, temporary-snapshot, and release-bundle patterns. Existing ignore
  rules were left unchanged.
- Licensed the project under the Apache License 2.0, and recorded in `NOTICE` the
  components that are deliberately not part of this repository: the live base
  snapshot `tmp/ccf_live5.json`, the non-public `async_gaussdb` driver, and the
  third-party services (Neo4j, Ontotext GraphDB, Dify) that are used as services
  and images rather than vendored.
- Brought the collect-layer source tree into this repository: `dist/collect-layer/`
  is now part of the public release and is licensed Apache-2.0 with the rest of
  the repository. It was previously developed in a separate repository of the
  same author (revision `8dcdefd`, 2026-09-28); `NOTICE` and `README.md` were
  updated accordingly.
