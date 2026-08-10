from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ai_intel_agent.pipeline import run_daily_report

app = typer.Typer(help="Run the AI intelligence Agent pipeline.")
console = Console()
DEFAULT_OUTPUT = Path("reports/daily.md")


@app.callback()
def main() -> None:
    """AI intelligence Agent command line interface."""


@app.command("run")
def run_pipeline(
    sample: Annotated[bool, typer.Option("--sample", help="Use bundled deterministic sample data.")] = False,
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT,
) -> None:
    report = run_daily_report(sample=sample)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.to_markdown(), encoding="utf-8")
    console.print(f"[green]Wrote draft report:[/] {output}")


if __name__ == "__main__":
    app()