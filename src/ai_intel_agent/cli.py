from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
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
from ai_intel_agent.pipeline import persist_sample_story
from ai_intel_agent.runtime_benchmark import (
    HttpRuntimeProbeClient,
    PricingObservation,
    RuntimeBenchmarkConfigurationError,
    compare_hong_kong_runtime_results,
    load_runtime_benchmark_configuration,
    run_hong_kong_runtime_probe,
from ai_intel_agent.retrieval_calibration import (
    FastEmbedCalibrationRuntime,
    RetrievalCalibrationConfigurationError,
    load_retrieval_candidate_configuration,
    load_retrieval_corpus,
    require_human_approved_retrieval_corpus,
    run_retrieval_calibration,
)
from ai_intel_agent.source_audit import run_source_definition_activation_audit

app = typer.Typer(help="Run the deterministic AI intelligence workflow.")
runtime_benchmark_app = typer.Typer(
    help="Capture and compare fixed Hong Kong runtime probes."
)
app.add_typer(runtime_benchmark_app, name="benchmark-runtime")
console = Console()
DEFAULT_OUTPUT = Path("reports/daily.md")
DEFAULT_SOURCE_AUDIT_OUTPUT = Path("reports/source-activation-audit.md")
DEFAULT_EXTRACTION_BENCHMARK_OUTPUT = Path("reports/document-extraction-benchmark.md")
DEFAULT_MODEL_ROUTING_OUTPUT = Path("reports/model-routing-evaluation.md")
DEFAULT_RUNTIME_BENCHMARK_OUTPUT = Path("reports/hong-kong-runtime-benchmark.md")
DEFAULT_RETRIEVAL_CALIBRATION_OUTPUT = Path("reports/retrieval-calibration.md")
DEFAULT_RETRIEVAL_PROFILE_OUTPUT = Path("reports/retrieval-profile.v1.json")


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


@runtime_benchmark_app.command("probe")
def probe_hong_kong_runtime(
    candidate: Annotated[
        str,
        typer.Option("--candidate", help="Configured Hong Kong runtime candidate identifier."),
    ],
    target_url: Annotated[
        str,
        typer.Option("--target-url", help="Public URL of the fixed benchmark workload."),
    ],
    observer: Annotated[
        str,
        typer.Option("--observer", help="Stable label for the fixed mainland observer."),
    ],
    monthly_cost_usd: Annotated[
        str,
        typer.Option("--monthly-cost-usd", help="Current observed monthly node price in USD."),
    ],
    price_observed_at: Annotated[
        str,
        typer.Option("--price-observed-at", help="Price observation date in YYYY-MM-DD form."),
    ],
    price_source: Annotated[
        str,
        typer.Option("--price-source", help="Official HTTPS evidence for the observed price."),
    ],
    workload_image_sha256: Annotated[
        str,
        typer.Option(
            "--workload-image-sha256",
            help="Local Docker image ID from docker image inspect.",
        ),
    ],
    database_image_sha256: Annotated[
        str,
        typer.Option(
            "--database-image-sha256",
            help="PostgreSQL Docker image ID from docker image inspect.",
        ),
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
) -> None:
    """Capture the fixed probe set for one Hong Kong candidate."""
    try:
        pricing = PricingObservation(
            monthly_cost_usd=Decimal(monthly_cost_usd),
            observed_at=date.fromisoformat(price_observed_at),
            source=price_source,
        )
        configuration = load_runtime_benchmark_configuration()
        load_dotenv()
        workload_token = os.environ.get("RUNTIME_BENCHMARK_TOKEN", "")
        if not workload_token:
            raise RuntimeBenchmarkConfigurationError(
                "set RUNTIME_BENCHMARK_TOKEN in the environment or untracked .env"
            )
        client = HttpRuntimeProbeClient(
            target_url,
            configuration=configuration,
            workload_token=workload_token,
        )
        try:
            result = run_hong_kong_runtime_probe(
                output,
                candidate_identifier=candidate,
                target_url=target_url,
                observer=observer,
                pricing=pricing,
                workload_image_sha256=workload_image_sha256,
                database_image_sha256=database_image_sha256,
                client=client,
                configuration=configuration,
            )
        finally:
            client.close()
    except (InvalidOperation, ValueError, RuntimeBenchmarkConfigurationError) as error:
        raise typer.BadParameter(str(error)) from error

    console.print(
        f"[green]Captured fixed Hong Kong runtime probes for {candidate}:[/] {output} "
        f"({'PASS' if result['passed'] else 'FAIL'})"
    )


@runtime_benchmark_app.command("compare")
def compare_hong_kong_runtimes(
    inputs: Annotated[
        list[Path],
        typer.Option("--input", "-i", help="Candidate JSON result; provide one per node."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write the reproducible comparison report here."),
    ] = DEFAULT_RUNTIME_BENCHMARK_OUTPUT,
) -> None:
    """Compare the complete fixed-protocol evidence from all candidate nodes."""
    try:
        comparison = compare_hong_kong_runtime_results(inputs, output)
    except RuntimeBenchmarkConfigurationError as error:
        raise typer.BadParameter(str(error)) from error

    recommendation = comparison["recommendation"] or "none"
    console.print(f"[green]Recommended Hong Kong runtime: {recommendation}[/] ({output})")
@app.command("calibrate-retrieval")
def calibrate_retrieval(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write the retrieval calibration report here."),
    ] = DEFAULT_RETRIEVAL_CALIBRATION_OUTPUT,
    profile_output: Annotated[
        Path,
        typer.Option(
            "--profile-output",
            help="Export the selected versioned Retrieval Profile here.",
        ),
    ] = DEFAULT_RETRIEVAL_PROFILE_OUTPUT,
) -> None:
    """Calibrate candidates and export one versioned Retrieval Profile."""

    def progress(completed: int, total: int, label: str) -> None:
        console.print(f"[cyan]Retrieval calibration:[/] {completed}/{total} ({label})")

    try:
        corpus = load_retrieval_corpus()
        require_human_approved_retrieval_corpus(corpus)
        configuration = load_retrieval_candidate_configuration()
        calibration = run_retrieval_calibration(
            output,
            profile_output,
            runtime=FastEmbedCalibrationRuntime(threads=configuration.runtime.threads),
            corpus=corpus,
            configuration=configuration,
            progress=progress,
        )
    except RetrievalCalibrationConfigurationError as error:
        raise typer.BadParameter(str(error)) from error

    console.print(
        f"[green]Calibrated {len(calibration.measurements)} Retrieval Profile candidates:[/] "
        f"{output}"
    )
    console.print(f"[green]Exported Retrieval Profile:[/] {profile_output}")


if __name__ == "__main__":
    app()
