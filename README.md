# dep-agent-ai

An AI-powered dependency update agent that scans your lockfiles for outdated packages, reads changelogs and GitHub release notes, assesses upgrade risk, and drafts pull-request descriptions — all using Claude.

## Features

- Parses `requirements.txt`, `pyproject.toml`, and `package.json`
- Checks PyPI and npm for newer versions
- Uses Claude (`claude-sonnet-4-6` by default) to:
  - Read release notes via PyPI metadata and GitHub Releases API
  - Identify breaking changes between the current and latest version
  - Assess upgrade risk as **LOW**, **MEDIUM**, or **HIGH**
  - Draft a pull-request title and body for each upgrade
- Rich terminal table output or JSON output
- Generates a Markdown report file

## Quick start

```bash
pip install dep-agent-ai
export ANTHROPIC_API_KEY=sk-ant-...

# Scan the current directory, show everything (default: LOW+)
dep-agent scan

# Only show MEDIUM and HIGH risk upgrades
dep-agent scan --risk-threshold medium

# Scan a different repo, output JSON
dep-agent scan --repo /path/to/project --json

# Write a full Markdown report
dep-agent report --output deps-report.md
```

## Installation from source

```bash
git clone https://github.com/example/dep-agent-ai
cd dep-agent-ai
pip install -e .
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | Your Anthropic API key |
| `GITHUB_TOKEN` | No | GitHub PAT — increases rate limits when fetching release notes |

## CLI reference

### `dep-agent scan`

```
Options:
  --repo TEXT                   Path to the repository root  [default: .]
  --risk-threshold [low|medium|high]
                                Minimum risk level to show  [default: low]
  --json                        Print results as JSON
  --model TEXT                  Claude model to use  [default: claude-sonnet-4-6]
```

### `dep-agent report`

```
Options:
  --repo TEXT                   Path to the repository root  [default: .]
  --output TEXT                 Output Markdown file  [default: deps-report.md]
  --risk-threshold [low|medium|high]
                                Minimum risk level to include  [default: low]
  --model TEXT                  Claude model to use  [default: claude-sonnet-4-6]
```

## Python API

```python
from dep_agent import DepScanner, DepAgent

# 1. Find outdated packages
scanner = DepScanner("/path/to/project")
outdated = scanner.scan()
# [{'package': 'requests', 'current': '2.28.0', 'latest': '2.32.3', 'ecosystem': 'pypi'}, ...]

# 2. Analyse with Claude
agent = DepAgent()  # reads ANTHROPIC_API_KEY from env
results = agent.analyse(outdated, risk_threshold="medium")

for r in results:
    print(r["package"], r["risk"], r["pr_title"])
```

## How the agent works

`DepAgent` runs a standard Claude tool-use agentic loop:

1. Sends the list of outdated packages to Claude with a system prompt and three tools:
   - `read_file(path)` — reads any local file (e.g. CHANGELOG.md)
   - `fetch_pypi_info(package, version)` — queries PyPI for metadata and changelog URLs
   - `fetch_github_releases(owner, repo)` — fetches GitHub release notes
2. Claude calls tools as needed, gathering changelog information
3. After all tool calls are resolved (`stop_reason == "end_turn"`), Claude returns structured JSON with risk assessments and PR drafts
4. The agent parses the JSON and filters by `risk_threshold`

## Risk levels

| Level | Criteria |
|---|---|
| **LOW** | Patch or minor bump, no API changes, no breaking-change notes |
| **MEDIUM** | Minor version with potential API changes or deprecations |
| **HIGH** | Major version bump, confirmed breaking changes, or security impact |

## License

MIT
