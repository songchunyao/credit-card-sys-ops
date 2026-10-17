# Security Policy

## Supported versions

This project is at an early development stage. Security fixes are made on the
default branch and, where a release exists, on the most recent release of the
current minor line.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

If you are running a fork or a vendor snapshot, reproduce the issue against the
current default branch before reporting it.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**. Do not open a public
issue, pull request, or discussion for a security problem, and do not include
exploit details in a public channel.

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the issue in the advisory form.

If private vulnerability reporting is unavailable to you, contact the
maintainers through the repository's issue tracker to ask for a private channel,
without disclosing the details of the vulnerability itself.

A useful report includes:

- What the issue is and where it lives (file, command, or endpoint), with the
  version or commit you tested.
- Why you believe it is a security problem, and what an attacker could achieve.
- The smallest reproduction you can produce, and the exact commands you ran.
- Any known mitigations or workarounds.
- How you would like to be credited, if at all.

Please give the maintainers a reasonable window to investigate and publish a fix
before any public disclosure. We will acknowledge the report and keep you
informed of the outcome.

## What is in scope

The code in this repository: the gate and verification tooling, the schemas and
contracts it enforces, the service and deployment scripts it ships, and the
offline checks that are meant to fail closed when an asset is missing.

## What is out of scope

- **Third-party services and images.** Neo4j, Ontotext GraphDB and Dify are
  separate works used as services or container images; report issues in those
  products to their own maintainers. See [NOTICE](NOTICE).
- **Components that are not shipped here.** Most notably the `async_gaussdb`
  driver, which users must supply themselves. (The collect-layer source tree, by
  contrast, is part of this repository and of the public release.)
- **Your own deployment and data.** Credentials, topology, and data you load into
  a database are yours to manage.

## What must never be committed

This repository ships no credentials by design; the only credential-related files
in the tree are inert `*.example` templates that document the required variable
names and contain no secrets. Every contributor is responsible for keeping it
that way.

Never commit any of the following:

- `.env` files, or any other real environment file. Only the committed example
  template is acceptable, and it must contain placeholders rather than working
  values.
- `deploy/credentials` and any sibling credential bundle, in any form —
  plaintext, base64, encrypted-with-a-committed-key, or otherwise.
- Passwords, API tokens, bearer tokens, session cookies, private keys, SSH keys,
  certificates, keystores, or signing material.
- Connection strings, DSNs, or webhook URLs that embed credentials.
- Snapshots, dumps, or exports captured from a real environment — including live
  inventories, gate evidence, and log files produced against real systems.
- Real hostnames, internal IP addresses, node or cluster names, or absolute paths
  from any machine, including your development machine.

Use documentation-safe placeholders when you need an example: addresses in the
`192.0.2.1`–`192.0.2.254` range and IRIs or domains under `https://example.org/`.

The offline tooling is designed to report `BLOCKED` when a credential or asset is
absent rather than to fall back to a default. Treat a `BLOCKED` result as "not
measured", never as a pass, and never commit an asset to turn a `BLOCKED` result
into a `PASS`.

## If a secret has already been committed

A secret that has entered the repository's history must be treated as compromised,
even if the commit is later removed:

1. Report it privately as described above.
2. Revoke and rotate the credential immediately, at its source.
3. Check whether the credential granted access to anything that needs auditing,
   and review the relevant logs.

Removing the file in a follow-up commit is not sufficient remediation on its own.
