# dep-agent

> Dependency update agent — reads changelogs, assesses risk, opens PRs automatically.

## What it does

Scans your lockfiles for outdated packages, fetches release notes, assesses breaking change risk with LLM analysis, and opens a PR per update with a summary of what changed and what to test.

## Quickstart

```bash
pip install dep-agent-ai
```

## Usage

```bash
dep-agent scan --repo . --open-prs --risk-threshold low
```

## Part of

This repo is listed in [awesome-agents](https://github.com/naveenkumarbaskaran/awesome-agents) — a curated collection of 60+ AI agent apps you can actually run.

## License

MIT

