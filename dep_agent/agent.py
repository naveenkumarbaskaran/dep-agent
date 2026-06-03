"""DepAgent: uses Claude claude-sonnet-4-6 to analyse outdated packages, read changelogs,
assess risk, and open GitHub PRs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests
import anthropic

# ---------------------------------------------------------------------------
# Tool implementations (called by the agent loop)
# ---------------------------------------------------------------------------


def _read_file(path: str) -> str:
    """Return the text content of a local file."""
    p = Path(path)
    if not p.exists():
        return f"ERROR: file not found: {path}"
    if not p.is_file():
        return f"ERROR: not a regular file: {path}"
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"ERROR reading file: {exc}"


def _fetch_pypi_info(package: str, version: str) -> str:
    """Fetch release metadata + changelog URL from PyPI for a given package/version."""
    try:
        resp = requests.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return f"ERROR fetching PyPI info for {package}: {exc}"

    data = resp.json()
    info = data.get("info", {})
    latest = info.get("version", "unknown")
    summary = info.get("summary", "")
    home_page = info.get("home_page") or info.get("project_url") or ""
    project_urls: dict[str, str] = info.get("project_urls") or {}

    changelog_url = (
        project_urls.get("Changelog")
        or project_urls.get("CHANGELOG")
        or project_urls.get("Changes")
        or project_urls.get("Release Notes")
        or ""
    )

    # Collect classifiers for Python version compatibility hints
    classifiers = info.get("classifiers", [])
    python_requires = info.get("requires_python") or ""

    # Find release notes for the requested version if present
    releases = data.get("releases", {})
    version_info = "not found in releases"
    if version in releases:
        files = releases[version]
        if files:
            upload_time = files[0].get("upload_time", "unknown")
            version_info = f"released {upload_time}"

    return json.dumps(
        {
            "package": package,
            "requested_version": version,
            "version_info": version_info,
            "latest_version": latest,
            "summary": summary,
            "home_page": home_page,
            "changelog_url": changelog_url,
            "python_requires": python_requires,
            "python_classifiers": [c for c in classifiers if "Python" in c][:10],
        },
        indent=2,
    )


def _fetch_github_releases(owner: str, repo: str) -> str:
    """Fetch the 10 most recent GitHub releases for a repo."""
    token = os.getenv("GITHUB_TOKEN")
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/releases",
            params={"per_page": 10},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return f"ERROR fetching GitHub releases for {owner}/{repo}: {exc}"

    releases = resp.json()
    if not isinstance(releases, list):
        return f"ERROR: unexpected response format: {releases}"

    simplified = [
        {
            "tag": r.get("tag_name"),
            "name": r.get("name"),
            "published_at": r.get("published_at"),
            "prerelease": r.get("prerelease"),
            "body": (r.get("body") or "")[:2000],  # trim large release notes
        }
        for r in releases
    ]
    return json.dumps(simplified, indent=2)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read the text content of a local file. Useful for reading lockfiles, "
            "changelogs, or any project file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "fetch_pypi_info",
        "description": (
            "Fetch package metadata and changelog URL from PyPI for a given package "
            "and version. Returns latest version, summary, homepage, and changelog URL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "package": {
                    "type": "string",
                    "description": "PyPI package name, e.g. 'requests'.",
                },
                "version": {
                    "type": "string",
                    "description": "The currently installed / pinned version.",
                },
            },
            "required": ["package", "version"],
        },
    },
    {
        "name": "fetch_github_releases",
        "description": (
            "Fetch the most recent GitHub releases for a repository. Useful for "
            "reading release notes and identifying breaking changes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "GitHub organisation or user name, e.g. 'psf'.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository name, e.g. 'requests'.",
                },
            },
            "required": ["owner", "repo"],
        },
    },
]


def _dispatch_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Route a tool call to the correct Python function."""
    if name == "read_file":
        return _read_file(tool_input["path"])
    if name == "fetch_pypi_info":
        return _fetch_pypi_info(tool_input["package"], tool_input["version"])
    if name == "fetch_github_releases":
        return _fetch_github_releases(tool_input["owner"], tool_input["repo"])
    return f"ERROR: unknown tool '{name}'"


# ---------------------------------------------------------------------------
# DepAgent
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are dep-agent, an expert software-dependency analyst.

You will be given a list of outdated packages discovered in a project.
For each package you should:
1. Use the available tools to gather PyPI metadata and GitHub release notes.
2. Identify breaking changes between the current version and the latest version.
3. Assess upgrade risk as one of: LOW, MEDIUM, or HIGH.
   - LOW  : patch/minor bumps, no API changes, no breaking notes.
   - MEDIUM: minor version with potential API changes or deprecation notices.
   - HIGH : major version bump, confirmed breaking changes, security impact.
4. Write a concise PR description suitable for a GitHub pull request.

Return your analysis as a JSON array. Each element must have these fields:
  package         : string
  current_version : string
  latest_version  : string
  risk            : "LOW" | "MEDIUM" | "HIGH"
  breaking_changes: string  (brief summary, or "None identified")
  pr_title        : string
  pr_body         : string  (markdown, max ~400 words)

Return ONLY the JSON array — no surrounding prose or markdown fences.
"""


class DepAgent:
    """Wraps the Claude API agentic loop for dependency analysis."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-6") -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        self.model = model
        self.max_tokens = 8096

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyse(
        self, outdated: list[dict[str, str]], risk_threshold: str = "low"
    ) -> list[dict[str, Any]]:
        """Run the agentic analysis loop and return structured results.

        Args:
            outdated: List of dicts with keys 'package', 'current', 'latest',
                      and optionally 'ecosystem'.
            risk_threshold: Minimum risk level to include in results
                            ('low', 'medium', 'high').

        Returns:
            List of analysis dicts filtered by risk_threshold.
        """
        if not outdated:
            return []

        user_message = self._build_user_message(outdated)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        # Agentic loop ---------------------------------------------------
        while True:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # Append assistant turn to conversation history
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                # Unexpected stop (max_tokens, refusal, etc.) — exit loop
                break

            # Execute all tool calls and collect results
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "tool_use":
                    result_text = _dispatch_tool(block.name, block.input)  # type: ignore[arg-type]
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

            if not tool_results:
                break  # no tool calls found — should not happen, but guard

            messages.append({"role": "user", "content": tool_results})

        # Extract text response -----------------------------------------
        raw_text = ""
        for block in response.content:
            if block.type == "text":
                raw_text += block.text

        results = self._parse_results(raw_text)
        return self._filter_by_risk(results, risk_threshold)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(outdated: list[dict[str, str]]) -> str:
        lines = ["Please analyse the following outdated packages:\n"]
        for pkg in outdated:
            lines.append(
                f"- {pkg['package']}  current={pkg['current']}  "
                f"latest={pkg['latest']}  ecosystem={pkg.get('ecosystem', 'pypi')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_results(text: str) -> list[dict[str, Any]]:
        """Extract the JSON array from the model's response."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
        cleaned = cleaned.strip("`")
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass

        # Fallback: try to find a JSON array anywhere in the text
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return []

    @staticmethod
    def _filter_by_risk(
        results: list[dict[str, Any]], threshold: str
    ) -> list[dict[str, Any]]:
        order = {"low": 0, "medium": 1, "high": 2}
        min_level = order.get(threshold.lower(), 0)
        return [
            r
            for r in results
            if order.get(str(r.get("risk", "low")).lower(), 0) >= min_level
        ]
