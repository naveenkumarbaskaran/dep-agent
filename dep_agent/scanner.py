"""DepScanner: parses lockfiles and detects outdated packages via PyPI / npm APIs."""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Iterator

import requests

try:
    from packaging.version import Version, InvalidVersion
except ImportError:  # pragma: no cover
    Version = None  # type: ignore[assignment,misc]
    InvalidVersion = Exception  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_newer(current: str, latest: str) -> bool:
    """Return True if *latest* is strictly newer than *current*."""
    if Version is None:
        return current != latest
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return current != latest


def _pypi_latest(package: str) -> str | None:
    """Return the latest stable version from PyPI, or None on error."""
    try:
        resp = requests.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["info"]["version"]
    except Exception:  # noqa: BLE001
        return None


def _npm_latest(package: str) -> str | None:
    """Return the latest version from the npm registry, or None on error."""
    try:
        # npm scoped packages use %2F encoding in the registry URL
        encoded = package.replace("/", "%2F")
        resp = requests.get(
            f"https://registry.npmjs.org/{encoded}/latest",
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json().get("version")
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_requirements_txt(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (package, pinned_version) pairs from a requirements.txt file.

    Only lines with an exact pin (== or ===) are considered.
    """
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Strip inline comments
        line = line.split("#")[0].strip()
        # Match   package==1.2.3   or   package===1.2.3
        m = re.match(r"^([A-Za-z0-9._-]+)===?([^,;\s]+)", line)
        if m:
            pkg = m.group(1).lower().replace("_", "-")
            ver = m.group(2)
            yield pkg, ver


def _parse_pyproject_toml(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (package, pinned_version) from pyproject.toml [project].dependencies.

    Only handles exact == pins; intentionally ignores ^, ~=, >=, etc.
    """
    try:
        # Use stdlib tomllib (Python 3.11+) or fall back to tomli
        try:
            import tomllib  # type: ignore[import]
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[import,no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]

        if tomllib is None:
            # Last resort: crude regex-based extraction (no nested tables)
            text = path.read_text(encoding="utf-8")
            for raw in re.findall(r'"([A-Za-z0-9._-]+)==([^"]+)"', text):
                yield raw[0].lower().replace("_", "-"), raw[1]
            return

        with path.open("rb") as fh:
            data = tomllib.load(fh)

        deps: list[str] = []
        # PEP 621 / pyproject.toml [project].dependencies
        project = data.get("project", {})
        deps.extend(project.get("dependencies", []))
        # Poetry [tool.poetry.dependencies]
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
        for pkg, spec in poetry_deps.items():
            if isinstance(spec, str):
                deps.append(f"{pkg}=={spec.lstrip('^~>=<!')}")
            elif isinstance(spec, dict):
                ver = spec.get("version", "")
                deps.append(f"{pkg}=={ver.lstrip('^~>=<!') }")

        for dep in deps:
            m = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([^,;\s]+)", dep)
            if m:
                pkg = m.group(1).lower().replace("_", "-")
                ver = m.group(2)
                yield pkg, ver
    except Exception:  # noqa: BLE001
        return


def _parse_package_json(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (package, pinned_version) from package.json dependencies.

    Only exact pins (no leading ^, ~, *, >=, etc.) are returned.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return

    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for pkg, spec in (data.get(section) or {}).items():
            # Only exact pins: e.g. "1.2.3" — not "^1.2.3" or ">=1.2.3"
            m = re.match(r"^(\d+\.\d+\.\d+[^\s]*)$", str(spec))
            if m:
                yield pkg, m.group(1)


# ---------------------------------------------------------------------------
# DepScanner
# ---------------------------------------------------------------------------


class DepScanner:
    """Scans a repository for outdated packages across multiple lockfile formats."""

    def __init__(self, repo_path: str | Path = ".") -> None:
        self.repo = Path(repo_path).resolve()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> list[dict[str, str]]:
        """Return a list of outdated package dicts.

        Each dict has keys: 'package', 'current', 'latest', 'ecosystem'.
        """
        outdated: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for entry in self._collect_pinned():
            pkg, current, ecosystem = entry
            key = (pkg, ecosystem)
            if key in seen:
                continue
            seen.add(key)

            latest = self._fetch_latest(pkg, ecosystem)
            if latest is None:
                continue
            if _is_newer(current, latest):
                outdated.append(
                    {
                        "package": pkg,
                        "current": current,
                        "latest": latest,
                        "ecosystem": ecosystem,
                    }
                )

        return outdated

    def scan_files(self) -> list[str]:
        """Return paths of recognised lockfiles found in the repo."""
        found: list[str] = []
        for candidate in (
            self.repo / "requirements.txt",
            self.repo / "requirements-dev.txt",
            self.repo / "requirements-test.txt",
            self.repo / "pyproject.toml",
            self.repo / "package.json",
        ):
            if candidate.exists():
                found.append(str(candidate))
        return found

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_pinned(self) -> Iterator[tuple[str, str, str]]:
        """Yield (package, version, ecosystem) from all lockfiles in the repo."""
        lockfiles_pypi = [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements-base.txt",
        ]
        for name in lockfiles_pypi:
            p = self.repo / name
            if p.exists():
                for pkg, ver in _parse_requirements_txt(p):
                    yield pkg, ver, "pypi"

        p = self.repo / "pyproject.toml"
        if p.exists():
            for pkg, ver in _parse_pyproject_toml(p):
                yield pkg, ver, "pypi"

        p = self.repo / "package.json"
        if p.exists():
            for pkg, ver in _parse_package_json(p):
                yield pkg, ver, "npm"

    @staticmethod
    def _fetch_latest(package: str, ecosystem: str) -> str | None:
        if ecosystem == "pypi":
            return _pypi_latest(package)
        if ecosystem == "npm":
            return _npm_latest(package)
        return None
