# AGENTS.md

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on the `fork` remote, `SrihariLegend/Stockfish` — never upstream, which `origin` points at. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, label strings unchanged (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Branch context

`master` mirrors upstream `origin/master` and carries no local commits. Live work is on `policy-research`; the instrumentation seam it adds lives under `src/policy_research/`, with research tooling under `tools/policy_research/`.

`tools/policy_research/verify.sh` is the landing gate — both builds, both C++ suites, bench parity, tool tests. A rebase is not verified until it passes; a static diff has already passed a rebase that did not compile.

Keep live work on durable disk: `/tmp` is a 2 GB tmpfs on this box and a reboot wiped a day of uncommitted work there. The gate refuses to build from a volatile workspace.
