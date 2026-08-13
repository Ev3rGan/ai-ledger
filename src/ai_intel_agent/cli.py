from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ai_intel_agent.extraction_benchmark import (
    BenchmarkConfigurationError,
    run_document_extraction_benchmark,
)
from ai_intel_agent.model_routing_evaluation import (
    HttpModelEvaluationClient,
    ModelEvaluationConfigurationError,
    ModelEvaluationCredentials,
    load_candidate_configuration,
    load_protocol_configuration,
    run_model_routing_evaluation,
)
from ai_intel_agent.persistence import database_url_from_environment
from ai_intel_agent.pipeline import publish_sample_digest
from ai_intel_agent.source_audit import run_source_definition_activation_audit

app = typer.Typer(help="Run the deterministic AI intelligence workflow.")
console = Console()
DEFAULT_OUTPUT = Path("reports/daily.md")
DEFAULT_SOURCE_AUDIT_OUTPUT = Path("reports/source-activation-audit.md")
DEFAULT_EXTRACTION_BENCHMARK_OUTPUT = Path("reports/document-extraction-benchmark.md")
DEFAULT_MODEL_ROUTING_OUTPUT = Path("reports/model-routing-evaluation.md")


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

    publication = publish_sample_digest(database_url)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(publication.to_markdown(), encoding="utf-8")
    console.print(
        "[green]Reviewed sample Stories and published Digest:[/] "
        f"{publication.digest.id} ({len(publication.digest.story_ids)} accepted Story)"
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


@app.command("benchmark-extraction")
def benchmark_document_extraction(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write the Document extraction benchmark here."),
    ] = DEFAULT_EXTRACTION_BENCHMARK_OUTPUT,
    attempts: Annotated[
        int,
        typer.Option(
            "--attempts",
            min=2,
            help="Run each URL and extraction path this many times.",
        ),
    ] = 2,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            min=1,
            help="Maximum number of extraction attempts in flight.",
        ),
    ] = 12,
) -> None:
    def progress(completed: int, total: int, label: str) -> None:
        if completed == total or completed % 20 == 0:
            console.print(f"[cyan]Benchmark progress:[/] {completed}/{total} ({label})")

    try:
        benchmark = run_document_extraction_benchmark(
            output,
            attempts=attempts,
            concurrency=concurrency,
            progress=progress,
        )
    except BenchmarkConfigurationError as error:
        raise typer.BadParameter(str(error)) from error
    console.print(
        "[green]Benchmarked "
        f"{len(benchmark.corpus)} fixed corpus URLs across "
        f"{len(benchmark.extraction_paths)} extraction paths:[/] {output}"
    )


@app.command("evaluate-model-routes")
def evaluate_model_routes(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write the versioned routing evaluation here."),
    ] = DEFAULT_MODEL_ROUTING_OUTPUT,
) -> None:
    """Evaluate DeepSeek and Kimi task routes on the frozen corpus."""

    def progress(completed: int, total: int, label: str) -> None:
        console.print(f"[cyan]Model evaluation:[/] {completed}/{total} ({label})")

    try:
        configuration = load_candidate_configuration()
        protocol = load_protocol_configuration()
        credentials = ModelEvaluationCredentials.from_environment(
            configuration=configuration
        )
        evaluation = run_model_routing_evaluation(
            output,
            client=HttpModelEvaluationClient(
                credentials=credentials,
                protocol=protocol,
            ),
            configuration=configuration,
            protocol=protocol,
            progress=progress,
        )
    except ModelEvaluationConfigurationError as error:
        raise typer.BadParameter(str(error)) from error

    eligible = sum(route is not None for route in evaluation.recommendations.values())
    console.print(
        f"[green]Evaluated DeepSeek and Kimi routes for {eligible}/5 task classes:[/] "
        f"{output}"
    )


if __name__ == "__main__":
    app()
