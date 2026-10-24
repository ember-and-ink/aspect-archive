# Security Policy

## Supported versions

This is a personal open-source project. Only the latest release on `main`
receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately via GitHub's
[private vulnerability reporting](https://github.com/ember-and-ink/jai-proxy/security/advisories/new),
which notifies the maintainer directly and keeps the report confidential until
a fix ships.

Expect an initial response within roughly a week. As a single-maintainer
project there is no formal SLA, but reports are taken seriously and credited
in the advisory unless you'd rather stay anonymous.

## Scope

In scope: this repository's source, its GitHub Actions workflows, and the
container image it publishes to `ghcr.io/ember-and-ink/jai-proxy`.

Out of scope: vulnerabilities in third-party dependencies (report those
upstream; Dependabot tracks them here), and issues that require an already
compromised local machine.

Also out of scope, because it is the documented design rather than a defect:
**the server has no authentication and serves its write endpoints with
permissive CORS.** It is built to run on a trusted LAN beside SillyTavern, is
never to be port-forwarded or placed on a public address, and the README says
so. Reports that amount to "an unauthenticated caller can write to the archive"
describe intended behaviour under the intended deployment. A way to reach the
server *from outside* that deployment -- SSRF through the outbound proxy, a path
traversal that escapes the archive directory, code execution from a card's
contents -- is very much in scope.
