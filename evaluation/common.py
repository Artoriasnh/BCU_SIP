# Copyright (c) 2026 Hao Ni. All rights reserved.
"""Shared helpers for the evaluation scripts: report assembly, figures, metadata.

Each evaluation writes a self-contained ``REPORT.md`` (with figures and CSV
tables next to it) so that results can be read on GitHub without running code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import platform
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Final, TypeVar

import matplotlib

matplotlib.use("Agg")  # headless rendering; must precede the pyplot import

import matplotlib.pyplot as plt
import pandas as pd

from dss.config import PROJECT_ROOT

RESULTS_ROOT: Final[Path] = PROJECT_ROOT / "evaluation" / "results"
SEED: Final[int] = 20261027

# Locations used across the evaluations (decimal degrees).
BRIEF_ALENTEJO: Final[tuple[float, float]] = (38.015, -7.865)
LISBON: Final[tuple[float, float]] = (38.722, -9.139)

_PACKAGES: Final[tuple[str, ...]] = (
    "geopandas", "shapely", "pyproj", "pyogrio", "pandas", "numpy",
    "requests", "matplotlib",
)  # fmt: skip

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Check:
    """Outcome of one acceptance check."""

    name: str
    passed: bool
    detail: str


class Report:
    """Collects sections, tables, figures and checks, then writes REPORT.md."""

    def __init__(self, task: str, title: str, purpose: str) -> None:
        """Create a report.

        Args:
            task: Folder name under ``evaluation/results`` (e.g. ``task1``).
            title: Report title.
            purpose: One-paragraph statement of what the evaluation establishes.
        """
        self.directory = RESULTS_ROOT / task
        self.directory.mkdir(parents=True, exist_ok=True)
        self._title = title
        self._purpose = purpose
        self._sections: list[str] = []
        self._checks: list[Check] = []
        self._data_files: list[Path] = []

    def add_section(self, heading: str, body: str) -> None:
        """Append a section (Markdown body)."""
        self._sections.append(f"## {heading}\n\n{body.strip()}\n")

    def check(self, name: str, passed: bool, detail: str) -> bool:
        """Record an acceptance check and return its outcome."""
        self._checks.append(Check(name, bool(passed), detail))
        return bool(passed)

    def register_data_file(self, path: Path) -> None:
        """Add an input file whose MD5 checksum is listed in the report."""
        self._data_files.append(path)

    def save_figure(self, fig: plt.Figure, name: str, caption: str) -> str:
        """Save a figure as PNG and return its Markdown embedding."""
        fig.savefig(self.directory / f"{name}.png", dpi=150, bbox_inches="tight")
        plt.close(fig)
        return f"![{caption}]({name}.png)\n\n*{caption}*"

    def save_csv(self, frame: pd.DataFrame, name: str) -> str:
        """Save a table as CSV and return a Markdown link to it."""
        frame.to_csv(self.directory / f"{name}.csv", index=False)
        return f"[`{name}.csv`]({name}.csv)"

    def write(self) -> Path:
        """Write REPORT.md and return its path."""
        n_passed = sum(check.passed for check in self._checks)
        lines = [
            f"# {self._title}",
            "",
            "Copyright (c) 2026 Hao Ni. All rights reserved.",
            "",
            self._purpose.strip(),
            "",
            f"**Checks passed: {n_passed} / {len(self._checks)}**",
            "",
            markdown_table(
                pd.DataFrame(
                    {
                        "Check": [c.name for c in self._checks],
                        "Result": [
                            "PASS" if c.passed else "FAIL" for c in self._checks
                        ],
                        "Detail": [c.detail for c in self._checks],
                    }
                )
            ),
            "",
            *self._sections,
            "## Reproducibility",
            "",
            markdown_table(environment_table(self._data_files)),
            "",
        ]
        path = self.directory / "REPORT.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def markdown_table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub Markdown table (no extra dependency).

    Floats of magnitude >= 1000 are shown as integers with thousands separators,
    smaller ones with four significant digits.
    """

    def cell(value: Any) -> str:
        if isinstance(value, float):
            return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:.4g}"
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{value:,}"
        return str(value).replace("|", "\\|")

    header = "| " + " | ".join(map(str, frame.columns)) + " |"
    rule = "|" + "|".join("---" for _ in frame.columns) + "|"
    rows = [
        "| " + " | ".join(cell(v) for v in row) + " |"
        for row in frame.itertuples(index=False)
    ]
    return "\n".join([header, rule, *rows])


def demo_site() -> tuple[float, float]:
    """Well location selected by the Task 1 evaluation (``demo_site.csv``)."""
    path = RESULTS_ROOT / "task1" / "demo_site.csv"
    if not path.exists():
        raise SystemExit("Run `python -m evaluation.task1_spatial` first.")
    row = pd.read_csv(path).iloc[0]
    return float(row["lat"]), float(row["lon"])


def timed(func: Callable[[], T]) -> tuple[T, float]:
    """Run ``func`` and return its result and wall-clock duration in seconds."""
    start = time.perf_counter()
    result = func()
    return result, time.perf_counter() - start


def md5sum(path: Path) -> str:
    """MD5 checksum of a file, read in chunks."""
    digest = hashlib.md5()  # integrity check, not a security use
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment_table(data_files: Iterable[Path] = ()) -> pd.DataFrame:
    """Software versions, git commit, timestamp and input checksums."""
    rows: list[tuple[str, str]] = [
        ("Generated (UTC)", f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M}"),
        ("Python", platform.python_version()),
        ("Platform", platform.platform()),
        ("Git commit", _git_commit()),
        ("Random seed", str(SEED)),
    ]
    for package in _PACKAGES:
        try:
            rows.append((package, metadata.version(package)))
        except metadata.PackageNotFoundError:
            rows.append((package, "not installed"))
    for path in data_files:
        rows.append((f"MD5 {path.name}", md5sum(path) if path.exists() else "missing"))
    return pd.DataFrame(rows, columns=["Item", "Value"])


def _git_commit() -> str:
    """Short hash of the current commit, or 'unknown' outside a git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip()
