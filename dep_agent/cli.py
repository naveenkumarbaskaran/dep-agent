"""CLI entry points for dep-agent.

Commands
--------
scan  --repo .  [--risk-threshold medium] [--json]
    Scan a repository for outdated packages and produce an analysis report.

report  --output deps-report.md  [--repo .] [--risk-threshold low]
    Run a scan and write a Markdown report to a file.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from .scanner import DepScanner
from .agent import DepAgent

console = Console()

RISK_COLOURS = {"LOW": "green", "MEDIUM": "yellow", "HIGH": "red"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _risk_colour(risk: str) -> str:
    return RISK_COLOURS.get(risk.upper(), "white")


def _render_table(results: list[dict[str, Any]]) -> None:
    """Render analysis results as a Rich table."""
    if not results:
        console.print("[green]No outdated packages found above the risk threshold.[/green]")
        return

    table = Table(title="Dependency Analysis", show_lines=True)
    table.add_column("Package", style="cyan", no_wrap=True)
    table.add_column("Current", style="dim")
    table.add_column("Latest", style="bold")
    table.add_column("Risk", justify="center")
    table.add_column("Breaking Changes", max_width=50)
    table.add_column("PR Title", max_width=40)

    for r in results:
        risk = str(r.get("risk", "?")).upper()
        colour = _risk_colour(risk)
        table.add_row(
            r.get("package", ""),
            r.get("current_version", ""),
            r.get("latest_version", ""),
            f"[{colour}]{risk}[/{colour}]",
            r.get("breaking_changes", ""),
            r.get("pr_title", ""),
        )

    console.print(table)


def _build_markdown_report(
    results: list[dict[str, Any]],
    repo: str,
    risk_threshold: str,
    outdated_count: int,
) -> str:
    """Build a Markdown report string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# Dependency Update Report",
        "",
        f"**Repository:** `{repo}`  ",
        f"**Generated:** {now}  ",
        f"**Risk threshold:** {risk_threshold.upper()}  ",
        f"**Outdated packages scanned:** {outdated_count}  ",
        f"**Packages in report:** {len(results)}",
        "",
    ]

    if not results:
        lines.append("_No packages meet the risk threshold. Nothing to update._")
        return "\n".join(lines)

    # Summary table
    lines += [
        "## Summary",
        "",
        "| Package | Current | Latest | Risk | Breaking Changes |",
        "|---------|---------|--------|------|------------------|",
    ]
    for r in results:
        risk = str(r.get("risk", "?")).upper()
        lines.append(
            f"| `{r.get('package')}` "
            f"| `{r.get('current_version')}` "
            f"| `{r.get('latest_version')}` "
            f"| **{risk}** "
            f"| {r.get('breaking_changes', '')} |"
        )

    # Detailed sections
    lines += ["", "## Details", ""]
    for r in results:
        pkg = r.get("package", "unknown")
        risk = str(r.get("risk", "?")).upper()
        lines += [
            f"### `{pkg}` ({r.get('current_version')} -> {r.get('latest_version')})",
            "",
            f"**Risk:** {risk}  ",
            f"**Breaking changes:** {r.get('breaking_changes', 'None identified')}  ",
            "",
            f"**PR Title:** {r.get('pr_title', '')}",
            "",
            "**PR Body:**",
            "",
            r.get("pr_body", ""),
            "",
            "---",
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.group()
@click.version_option()
def cli() -> None:
    """dep-agent: AI-powered dependency update agent."""


@cli.command()
@click.option(
    "--repo",
    default=".",
    show_default=True,
    help="Path to the repository root to scan.",
)
@click.option(
    "--risk-threshold",
    default="low",
    show_default=True,
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    help="Minimum risk level to include in results.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Print results as JSON instead of a Rich table.",
)
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    show_default=True,
    help="Claude model to use for analysis.",
)
def scan(
    repo: str,
    risk_threshold: str,
    output_json: bool,
    model: str,
) -> None:
    """Scan a repository for outdated packages and analyse upgrade risk."""
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        console.print(f"[red]ERROR:[/red] '{repo}' is not a directory.")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[red]ERROR:[/red] ANTHROPIC_API_KEY environment variable is not set."
        )
        sys.exit(1)

    console.print(f"[bold]Scanning[/bold] {repo_path} ...")

    scanner = DepScanner(repo_path)
    lockfiles = scanner.scan_files()
    if not lockfiles:
        console.print(
            "[yellow]No recognised lockfiles found.[/yellow] "
            "(requirements.txt, pyproject.toml, package.json)"
        )
        sys.exit(0)

    console.print("Found lockfiles:")
    for lf in lockfiles:
        console.print(f"  [dim]{lf}[/dim]")

    with console.status("Checking latest versions ..."):
        outdated = scanner.scan()

    if not outdated:
        console.print("[green]All packages are up to date.[/green]")
        sys.exit(0)

    console.print(f"Found [bold]{len(outdated)}[/bold] outdated package(s). Analysing with Claude ...")

    agent = DepAgent(api_key=api_key, model=model)
    with console.status("Running Claude analysis (this may take a moment) ..."):
        results = agent.analyse(outdated, risk_threshold=risk_threshold)

    if output_json:
        click.echo(json.dumps(results, indent=2))
    else:
        _render_table(results)
        console.print(
            f"\n[dim]Showing {len(results)} result(s) at risk >= {risk_threshold.upper()}[/dim]"
        )


@cli.command()
@click.option(
    "--repo",
    default=".",
    show_default=True,
    help="Path to the repository root to scan.",
)
@click.option(
    "--output",
    default="deps-report.md",
    show_default=True,
    help="Output file path for the Markdown report.",
)
@click.option(
    "--risk-threshold",
    default="low",
    show_default=True,
    type=click.Choice(["low", "medium", "high"], case_sensitive=False),
    help="Minimum risk level to include in report.",
)
@click.option(
    "--model",
    default="claude-sonnet-4-6",
    show_default=True,
    help="Claude model to use for analysis.",
)
def report(
    repo: str,
    output: str,
    risk_threshold: str,
    model: str,
) -> None:
    """Run a full scan and write a Markdown report."""
    repo_path = Path(repo).resolve()
    if not repo_path.is_dir():
        console.print(f"[red]ERROR:[/red] '{repo}' is not a directory.")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print(
            "[red]ERROR:[/red] ANTHROPIC_API_KEY environment variable is not set."
        )
        sys.exit(1)

    console.print(f"[bold]Scanning[/bold] {repo_path} ...")

    scanner = DepScanner(repo_path)
    lockfiles = scanner.scan_files()
    if not lockfiles:
        console.print(
            "[yellow]No recognised lockfiles found.[/yellow] "
            "(requirements.txt, pyproject.toml, package.json)"
        )
        sys.exit(0)

    with console.status("Checking latest versions ..."):
        outdated = scanner.scan()

    console.print(f"Found [bold]{len(outdated)}[/bold] outdated package(s).")

    agent = DepAgent(api_key=api_key, model=model)
    with console.status("Running Claude analysis ..."):
        results = agent.analyse(outdated, risk_threshold=risk_threshold)

    md = _build_markdown_report(
        results=results,
        repo=str(repo_path),
        risk_threshold=risk_threshold,
        outdated_count=len(outdated),
    )

    out_path = Path(output)
    out_path.write_text(md, encoding="utf-8")
    console.print(f"[green]Report written to[/green] {out_path.resolve()}")
    console.print(f"[dim]{len(results)} package(s) included in report.[/dim]")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
