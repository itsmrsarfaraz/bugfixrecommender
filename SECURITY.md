# Security Policy

## Reporting a Vulnerability

This project clones third-party Git repositories and runs a local FastAPI
server. Relevant attack surfaces: subprocess git invocations in
`src/downloader/repo_downloader.py`, and the `/recommend` HTTP endpoint in
`api/server.py`.

If you find a vulnerability (e.g. command injection via repo metadata, path
traversal in checkpoint files, SSRF via `serverUrl` config), please **do not**
open a public issue. Instead, report it privately via GitHub's
"Report a vulnerability" feature on this repository's Security tab.

Include:
- A description of the issue and potential impact
- Steps to reproduce
- Affected file(s)/commit

You should receive an acknowledgement within a few days. Please allow time for
a fix before public disclosure.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| main    | ✅        |

This is a research/thesis project — only the `main` branch receives fixes.

## Known Scope Notes

- The API server binds to `127.0.0.1` and is intended for **local use only**.
  Do not expose it on a public network without adding authentication.
- `GITHUB_TOKEN` must never be committed (`config/secrets.yaml` and `.env` are
  gitignored). Rotate immediately if leaked.
