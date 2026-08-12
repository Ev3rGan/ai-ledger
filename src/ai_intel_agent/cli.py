from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ai_intel_agent.persistence import database_url_from_environment
from ai_intel_agent.pipeline import persist_sample_story
from ai_intel_agent.source_audit import run_source_definition_activation_audit

app = typer.Typer(help="Run the deterministic AI intelligence workflow.")
console = Console()
DEFAULT_OUTPUT = Path("reports/daily.md")
DEFAULT_SOURCE_AUDIT_OUTPUT = Path("reports/source-activation-audit.md")


@app.callback()
def main() -> None:
    """AI intelligence command line interface."""


@app.command("run")
def run_pipeline(
    sample: Annotated[bool, typer.Option("--sample", help="Use fixed deterministic data.")] = False,
    output: Annotated[Path, typer.Option("--output", "-o")] = DEFAULT_OUTPUT,
) -> None:
    if not sample:
        raise typer.BadParameter("Only deterministic --sample mode is available in this slice")

    try:
        database_url = database_url_from_environment()
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    sample_story = persist_sample_story(database_url)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(sample_story.to_markdown(), encoding="utf-8")
    console.print(
        "[green]Persisted sample Story:[/] "
        f"{sample_story.story.id} (Evidence Span {sample_story.evidence_span.id})"
    )
    console.print(f"[green]Wrote sample report:[/] {output}")


@app.command("audit-sources")
def audit_source_definitions(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write the first-wave activation audit here."),
    ] = DEFAULT_SOURCE_AUDIT_OUTPUT,
) -> None:
    audit = run_source_definition_activation_audit(output)
    console.print(
        f"[green]Audited {len(audit.source_definitions)} first-wave Source Definitions:[/] {output}"
    )


if __name__ == "__main__":
    app()
