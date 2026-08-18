from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from sqlalchemy.engine import Engine

from ai_intel_agent.collection import (
    SystemClock,
    collect_feed_source_definitions,
)
from ai_intel_agent.domain import SourceDefinitionCollectionStatus, StoryReviewState, Topic
from ai_intel_agent.editorial import EditorialStateError, StoryInspection
from ai_intel_agent.extraction_benchmark import (
    BenchmarkConfigurationError,
    run_document_extraction_benchmark,
)
from ai_intel_agent.feed_acquisition import (
    HttpFeedFetcher,
    SampleFeedFetcher,
    load_approved_feed_source_definitions,
    load_sample_feed_source_definitions,
)
from ai_intel_agent.gemini_collection import (
    DeepSeekGeminiDraftProvider,
    GeminiCollectionError,
    HttpGeminiReleaseNotesFetcher,
    collect_gemini_release_notes,
    deepseek_api_key_from_environment,
)
from ai_intel_agent.model_routing_evaluation import (
    HttpModelEvaluationClient,
    ModelEvaluationConfigurationError,
    ModelEvaluationCredentials,
    load_candidate_configuration,
    load_protocol_configuration,
    run_model_routing_evaluation,
)
from ai_intel_agent.multisource_collection import (
    HttpArticleAdapter,
    HttpFeedDiscoveryAdapter,
    collect_source_profiles,
    load_source_profiles,
    scheduled_operation_key,
)
from ai_intel_agent.persistence import (
    EditorialRepository,
    MultiSourceCollectionRepository,
    PersistentMeteredProviderBudget,
    SchedulerStatusRepository,
    create_database_engine,
    database_url_from_environment,
    upgrade_database,
)
from ai_intel_agent.pipeline import publish_sample_digest
from ai_intel_agent.research import DeepSeekResearchProvider, ResearchError
from ai_intel_agent.retrieval_calibration import (
    FastEmbedCalibrationRuntime,
    RetrievalCalibrationConfigurationError,
    load_retrieval_candidate_configuration,
    load_retrieval_corpus,
    require_human_approved_retrieval_corpus,
    run_retrieval_calibration,
)
from ai_intel_agent.runtime import (
    DockerComposeDatabase,
    GeminiScheduler,
    LocalMvpConfiguration,
    LocalMvpRuntime,
    LocalMvpState,
    M1ProviderConfiguration,
    M1SchedulerConfiguration,
    M1WebConfiguration,
    MvpChildProcesses,
    PostgresCollectionLease,
    PostgresSchedulerLease,
    SchedulerStopController,
    configure_structured_logging,
    production_database_url,
)
from ai_intel_agent.runtime_benchmark import (
    HttpRuntimeProbeClient,
    PricingObservation,
    RuntimeBenchmarkConfigurationError,
    compare_hong_kong_runtime_results,
    load_runtime_benchmark_configuration,
    run_hong_kong_runtime_probe,
)
from ai_intel_agent.sample import FixedClock
from ai_intel_agent.source_audit import run_source_definition_activation_audit

app = typer.Typer(help="Run the deterministic AI intelligence workflow.")
story_app = typer.Typer(help="Inspect and review persisted Stories.")
digest_app = typer.Typer(help="Preview and publish persisted Digests.")
runtime_benchmark_app = typer.Typer(
    help="Capture and compare fixed Hong Kong runtime probes."
)
operator_app = typer.Typer(help="Private production operator commands.")
app.add_typer(story_app, name="story")
app.add_typer(digest_app, name="digest")
app.add_typer(runtime_benchmark_app, name="benchmark-runtime")
app.add_typer(operator_app, name="operator")
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


def _operator_database_url(production: bool) -> str:
    return (
        production_database_url(os.environ)
        if production
        else database_url_from_environment()
    )


def _persistent_provider_budget(
    engine: Engine,
    configuration: M1ProviderConfiguration,
) -> PersistentMeteredProviderBudget:
    return PersistentMeteredProviderBudget(
        engine,
        monthly_limit_cents=configuration.monthly_budget_cents,
        request_reservation_cents=configuration.request_reservation_cents,
    )


@operator_app.command("migrate")
def operator_migrate(
    production: Annotated[
        bool,
        typer.Option("--production", help="Load the M1 Docker-secret contract."),
    ] = False,
) -> None:
    """Apply every database migration to the sole Alembic head."""
    try:
        database_url = _operator_database_url(production)
        upgrade_database(database_url)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    console.print_json(data={"database": "migrated"})


@operator_app.command("status")
def operator_status(
    production: Annotated[
        bool,
        typer.Option("--production", help="Load the M1 Docker-secret contract."),
    ] = False,
) -> None:
    """Report database readiness and recent Scheduler status without secrets."""
    try:
        database_url = _operator_database_url(production)
        engine = create_database_engine(database_url)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            snapshot = SchedulerStatusRepository(engine).snapshot()
        finally:
            engine.dispose()
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error

    scheduler = None
    if snapshot is not None:
        scheduler = {
            "state": snapshot.state,
            "next_run_at": (
                snapshot.next_run_at.isoformat() if snapshot.next_run_at is not None else None
            ),
            "last_started_at": (
                snapshot.last_started_at.isoformat()
                if snapshot.last_started_at is not None
                else None
            ),
            "last_completed_at": (
                snapshot.last_completed_at.isoformat()
                if snapshot.last_completed_at is not None
                else None
            ),
            "last_result": snapshot.last_result,
            "updated_at": snapshot.updated_at.isoformat(),
        }
    console.print_json(data={"database": "ready", "scheduler": scheduler})


@operator_app.command("source-status")
def operator_source_status(
    production: Annotated[
        bool,
        typer.Option("--production", help="Load the M1 Docker-secret contract."),
    ] = False,
) -> None:
    """Report current five-profile result, cursor, health, and pending drafts."""
    try:
        database_url = _operator_database_url(production)
        profiles = load_source_profiles()
        engine = create_database_engine(database_url)
        try:
            snapshots = MultiSourceCollectionRepository(engine).source_statuses(
                {profile.id for profile in profiles}
            )
        finally:
            engine.dispose()
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error

    snapshots_by_id = {
        snapshot.source_definition_id: snapshot for snapshot in snapshots
    }
    sources: list[dict[str, object]] = []
    for profile in profiles:
        snapshot = snapshots_by_id.get(profile.id)
        sources.append(
            {
                "host": profile.host,
                "publisher": profile.publisher,
                "recent_result": (
                    snapshot.recent_result if snapshot is not None else None
                ),
                "cursor": snapshot.cursor_value if snapshot is not None else None,
                "health": snapshot.health if snapshot is not None else "unknown",
                "consecutive_failures": (
                    snapshot.consecutive_failures if snapshot is not None else 0
                ),
                "last_collection_run_id": (
                    str(snapshot.last_collection_run_id)
                    if snapshot is not None
                    else None
                ),
                "updated_at": (
                    snapshot.updated_at.isoformat() if snapshot is not None else None
                ),
                "pending_drafts": (
                    snapshot.pending_drafts if snapshot is not None else 0
                ),
            }
        )
    console.print_json(
        data={
            "sources": sources
        }
    )


@operator_app.command("scheduler-health")
def operator_scheduler_health(
    production: Annotated[
        bool,
        typer.Option("--production", help="Load the M1 Docker-secret contract."),
    ] = False,
) -> None:
    """Exit successfully only while the persistent Scheduler status is active."""
    try:
        database_url = _operator_database_url(production)
        engine = create_database_engine(database_url)
        try:
            snapshot = SchedulerStatusRepository(engine).snapshot()
        finally:
            engine.dispose()
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    if snapshot is None or snapshot.state not in {"waiting", "running"}:
        raise typer.BadParameter("Production Scheduler is not active")
    if (
        snapshot.state == "waiting"
        and snapshot.next_run_at is not None
        and snapshot.next_run_at < datetime.now(UTC) - timedelta(minutes=5)
    ):
        raise typer.BadParameter("Production Scheduler next run is stale")
    console.print_json(data={"scheduler": "active", "state": snapshot.state})


@app.command("serve")
def serve(
    host: Annotated[
        str, typer.Option("--host", help="Interface on which the Web service listens.")
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="TCP port on which the Web service listens.",
        ),
    ] = 8000,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Require the M1 Docker-secret and anonymous-allowance contract.",
        ),
    ] = False,
) -> None:
    """Start the supported database-backed Web service."""
    if production:
        try:
            service_configuration = M1WebConfiguration.from_environment(os.environ)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        database_url = service_configuration.database.database_url
        api_key = service_configuration.provider.api_key
        configure_structured_logging()
    else:
        try:
            database_url = database_url_from_environment()
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        service_configuration = None
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    import uvicorn

    from ai_intel_agent.web import create_app

    if not api_key:
        uvicorn.run(create_app(database_url), host=host, port=port)
        return

    budget_engine = create_database_engine(database_url) if production else None
    provider_budget = (
        _persistent_provider_budget(budget_engine, service_configuration.provider)
        if budget_engine is not None and service_configuration is not None
        else None
    )
    with httpx.Client(timeout=60.0) as client:
        try:
            if provider_budget is None:
                research_provider = DeepSeekResearchProvider(client, api_key=api_key)
            else:
                research_provider = DeepSeekResearchProvider(
                    client,
                    api_key=api_key,
                    budget=provider_budget,
                )
        except ResearchError as error:
            if budget_engine is not None:
                budget_engine.dispose()
            raise typer.BadParameter(str(error)) from error
        web_app = create_app(
            database_url,
            research_provider=research_provider,
            anonymous_research_daily_limit=(
                service_configuration.anonymous_research_daily_limit
                if service_configuration is not None
                else None
            ),
            anonymous_identity_salt=(
                service_configuration.anonymous_identity_salt
                if service_configuration is not None
                else None
            ),
        )
        try:
            if production:
                uvicorn.run(web_app, host=host, port=port, log_config=None)
            else:
                uvicorn.run(web_app, host=host, port=port)
        finally:
            if budget_engine is not None:
                budget_engine.dispose()


@app.command("schedule-gemini")
def schedule_gemini(
    backfill_days: Annotated[
        int,
        typer.Option(
            "--backfill-days",
            min=1,
            max=3650,
            help="Collect dated sections in this many prior calendar days.",
        ),
    ] = 10,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Require M1 secret files, persistent status, and the singleton lease.",
        ),
    ] = False,
) -> None:
    """Collect Gemini at 06:00 and 18:00 Asia/Shanghai until interrupted."""
    if production:
        try:
            service_configuration = M1SchedulerConfiguration.from_environment(os.environ)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        database_url = service_configuration.database.database_url
        engine = create_database_engine(database_url)
        provider_budget = _persistent_provider_budget(
            engine,
            service_configuration.provider,
        )
        configure_structured_logging()
        status = SchedulerStatusRepository(engine)
        try:
            with PostgresSchedulerLease(engine) as lease, SchedulerStopController() as stopped:
                if lease.guarded_wait(stopped.wait, 5.0):
                    return
                with lease.monitor(lambda: os._exit(75)):
                    console.print_json(
                        data={
                            "event": "scheduler-active",
                            "schedule": "06:00,18:00 Asia/Shanghai",
                        }
                    )
                    GeminiScheduler(
                        collect=lambda: _run_gemini_collection(
                            backfill_days,
                            database_url=database_url,
                            api_key=service_configuration.provider.api_key,
                            provider_budget=provider_budget,
                            structured_output=True,
                        ),
                        now=lambda: datetime.now(UTC),
                        wait=stopped.wait,
                        status=status,
                    ).run()
        except RuntimeError as error:
            raise typer.BadParameter(str(error)) from error
        finally:
            engine.dispose()
        return

    _local_mvp_configuration()
    console.print("Gemini scheduler active at 06:00 and 18:00 Asia/Shanghai.")
    with SchedulerStopController() as stopped:
        GeminiScheduler(
            collect=lambda: collect_gemini(backfill_days),
            now=lambda: datetime.now(UTC),
            wait=stopped.wait,
        ).run()


@app.command("schedule-sources")
def schedule_sources(
    backfill_limit: Annotated[
        int,
        typer.Option(
            "--backfill-limit",
            min=1,
            max=100,
            help="Collect at most this many unseen entries per source and run.",
        ),
    ] = 5,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Require M1 secret files, persistent status, and the singleton lease.",
        ),
    ] = False,
) -> None:
    """Collect the five approved Source Profiles twice daily until interrupted."""
    if production:
        try:
            service_configuration = M1SchedulerConfiguration.from_environment(os.environ)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error
        database_url = service_configuration.database.database_url
        engine = create_database_engine(database_url)
        provider_budget = _persistent_provider_budget(
            engine,
            service_configuration.provider,
        )
        configure_structured_logging()
        status = SchedulerStatusRepository(engine)
        try:
            with PostgresSchedulerLease(engine) as lease, SchedulerStopController() as stopped:
                if lease.guarded_wait(stopped.wait, 5.0):
                    return
                with lease.monitor(lambda: os._exit(75)):
                    console.print_json(
                        data={
                            "event": "multisource-scheduler-active",
                            "schedule": "06:00,18:00 Asia/Shanghai",
                        }
                    )
                    GeminiScheduler(
                        collect=lambda: _run_multisource_collection(
                            backfill_limit,
                            database_url=database_url,
                            api_key=service_configuration.provider.api_key,
                            operation_key=scheduled_operation_key(datetime.now(UTC)),
                            provider_budget=provider_budget,
                            structured_output=True,
                        ),
                        now=lambda: datetime.now(UTC),
                        wait=stopped.wait,
                        status=status,
                    ).run()
        except RuntimeError as error:
            raise typer.BadParameter(str(error)) from error
        finally:
            engine.dispose()
        return

    configuration = _local_mvp_configuration()
    console.print("Five-source scheduler active at 06:00 and 18:00 Asia/Shanghai.")
    with SchedulerStopController() as stopped:
        GeminiScheduler(
            collect=lambda: _run_multisource_collection(
                backfill_limit,
                database_url=configuration.database_url,
                api_key=deepseek_api_key_from_environment(),
                operation_key=scheduled_operation_key(datetime.now(UTC)),
                structured_output=False,
            ),
            now=lambda: datetime.now(UTC),
            wait=stopped.wait,
        ).run()


def _local_mvp_configuration() -> LocalMvpConfiguration:
    try:
        return LocalMvpConfiguration.from_environment(os.environ)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


@app.command("start-local")
def start_local(
    host: Annotated[
        str, typer.Option("--host", help="Loopback interface for the Web service.")
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="TCP port for the Web service.",
        ),
    ] = 8000,
) -> None:
    """Start PostgreSQL, migrate, then run the Web service and scheduler."""
    if host not in {"127.0.0.1", "localhost"}:
        raise typer.BadParameter("start-local only supports a loopback Web host")
    configuration = _local_mvp_configuration()

    project_root = Path(__file__).resolve().parents[2]
    process_environment = dict(os.environ)
    runtime = LocalMvpRuntime(
        database=DockerComposeDatabase(
            project_root=project_root,
            compose_environment=configuration.compose_environment,
        ),
        migrate=lambda: upgrade_database(configuration.database_url),
        processes=MvpChildProcesses(
            project_root=project_root,
            host=host,
            port=port,
            environment=process_environment,
        ),
        state_changed=lambda state: _print_local_mvp_state(state, host, port),
    )
    try:
        runtime.run()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise typer.BadParameter(f"Local MVP failed: {error}") from error


def _print_local_mvp_state(state: LocalMvpState, host: str, port: int) -> None:
    if state is LocalMvpState.SCHEDULER_AND_WEB_RUNNING:
        console.print(
            f"Local MVP running at http://{host}:{port}; press Ctrl+C to stop safely."
        )
        return
    console.print(f"Local MVP state: {state.value}")


@contextmanager
def _editorial_repository() -> Iterator[EditorialRepository]:
    try:
        database_url = database_url_from_environment()
        engine = create_database_engine(database_url)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    try:
        yield EditorialRepository(engine)
    finally:
        engine.dispose()


def _print_story(story: StoryInspection) -> None:
    console.print(f"Story: {story.headline}", markup=False)
    console.print(f"Stable key: {story.stable_key}", markup=False)
    console.print(f"Review state: {story.review_state.value}", markup=False)
    console.print(f"Publisher: {story.publisher}", markup=False)
    if story.original_published_at is not None:
        console.print(
            f"Original publication time: {story.original_published_at.isoformat()}",
            markup=False,
        )
    if story.summary is not None:
        console.print(f"Summary: {story.summary}", markup=False)
    if story.why_it_matters is not None:
        console.print(f"Why it matters: {story.why_it_matters}", markup=False)
    if story.primary_topic is not None:
        console.print(f"Primary topic: {story.primary_topic.value}", markup=False)
    for position, claim in enumerate(story.claims, start=1):
        console.print(f"Claim {position}: {claim.text}", markup=False)
        for evidence_span in claim.evidence_spans:
            console.print(
                f"Evidence Span: {evidence_span.exact_text}",
                markup=False,
                soft_wrap=True,
            )
            console.print(
                f"Evidence Role: {evidence_span.role.value}", markup=False
            )
            console.print(
                f"Evidence Relation: {evidence_span.relation.value}", markup=False
            )
            console.print(f"Publisher: {evidence_span.publisher}", markup=False)
            console.print(
                f"Canonical source: {evidence_span.canonical_url}", markup=False
            )


@story_app.command("list")
def list_stories(
    publisher: Annotated[
        str | None,
        typer.Option("--source", help="Filter by exact Publisher name."),
    ] = None,
    publication_date: Annotated[
        str | None,
        typer.Option("--date", help="Filter by original publication date."),
    ] = None,
    state: Annotated[
        StoryReviewState | None,
        typer.Option("--state", help="Filter by Story review state."),
    ] = None,
) -> None:
    """List persisted Stories for operator review."""
    with _editorial_repository() as repository:
        stories = repository.stories(
            publisher=publisher,
            publication_date=(
                _parse_iso_date(publication_date) if publication_date is not None else None
            ),
            review_state=state,
        )
    if not stories:
        console.print("No persisted Stories.")
        return
    for story in stories:
        console.print(
            f"{story.stable_key}\t{story.review_state.value}\t{story.publisher}\t"
            f"{story.original_published_at.date().isoformat() if story.original_published_at else '-'}\t"
            f"{story.headline}",
            markup=False,
            soft_wrap=True,
        )


@story_app.command("show")
def show_story(stable_key: str) -> None:
    """Show a persisted Story with its Claims and exact Evidence Spans."""
    with _editorial_repository() as repository:
        story = repository.story(stable_key)
    if story is None:
        raise typer.BadParameter(f"Story {stable_key!r} does not exist")
    _print_story(story)


def _review_story(
    stable_key: str,
    decision: StoryReviewState,
    actor: str,
    *,
    summary: str | None = None,
    why_it_matters: str | None = None,
    primary_topic: Topic | None = None,
) -> None:
    with _editorial_repository() as repository:
        try:
            story = repository.review(
                stable_key,
                decision,
                actor_identifier=actor,
                occurred_at=datetime.now(UTC),
                summary=summary,
                why_it_matters=why_it_matters,
                primary_topic=primary_topic,
            )
        except (EditorialStateError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
    console.print(
        f"Story {story.stable_key} is {story.review_state.value}.", markup=False
    )


@story_app.command("accept")
def accept_story(
    stable_key: str,
    summary: Annotated[
        str,
        typer.Option("--summary", help="Operator-authored reader summary."),
    ],
    why_it_matters: Annotated[
        str,
        typer.Option(
            "--why-it-matters",
            help="Operator-authored explanation of reader significance.",
        ),
    ],
    topic: Annotated[
        Topic,
        typer.Option("--topic", help="Primary reader-facing Topic."),
    ],
    actor: Annotated[
        str, typer.Option("--actor", help="Identifier recorded in the audit event.")
    ] = "local-operator",
) -> None:
    """Accept one unreviewed Story."""
    _review_story(
        stable_key,
        StoryReviewState.ACCEPTED,
        actor,
        summary=summary,
        why_it_matters=why_it_matters,
        primary_topic=topic,
    )


@story_app.command("reject")
def reject_story(
    stable_key: str,
    actor: Annotated[
        str, typer.Option("--actor", help="Identifier recorded in the audit event.")
    ] = "local-operator",
) -> None:
    """Reject one unreviewed Story."""
    _review_story(stable_key, StoryReviewState.REJECTED, actor)


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter("Date must use YYYY-MM-DD form") from error


def _digest_date(publication_date: str | None) -> date:
    if publication_date is None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return _parse_iso_date(publication_date)


@digest_app.command("preview")
def preview_digest(
    publication_date: Annotated[
        str | None,
        typer.Option("--date", help="Digest publication date in YYYY-MM-DD form."),
    ] = None,
    story: Annotated[
        list[str] | None,
        typer.Option(
            "--story",
            help="Story stable key; repeat in the intended Digest order.",
        ),
    ] = None,
) -> None:
    """Preview accepted Stories that are not already in a Digest."""
    with _editorial_repository() as repository:
        try:
            preview = repository.preview_digest(
                _digest_date(publication_date),
                story_keys=tuple(story) if story is not None else None,
            )
        except (EditorialStateError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
    console.print(f"Digest preview: {preview.publication_date.isoformat()}", markup=False)
    if not preview.stories:
        console.print("No accepted Stories are eligible.")
        return
    for item in preview.stories:
        console.print(
            f"{item.stable_key}\t{item.headline}", markup=False, soft_wrap=True
        )


@digest_app.command("publish")
def publish_digest_command(
    publication_date: Annotated[
        str,
        typer.Option("--date", help="Digest publication date in YYYY-MM-DD form."),
    ],
    introduction: Annotated[
        str,
        typer.Option(
            "--introduction",
            help="Operator-authored daily Digest introduction.",
        ),
    ],
    story: Annotated[
        list[str] | None,
        typer.Option(
            "--story",
            help="Story stable key; repeat in the intended Digest order.",
        ),
    ] = None,
    actor: Annotated[
        str, typer.Option("--actor", help="Identifier recorded in audit events.")
    ] = "local-operator",
) -> None:
    """Compose and publish a Digest from eligible accepted Stories."""
    with _editorial_repository() as repository:
        try:
            digest = repository.publish_digest(
                _digest_date(publication_date),
                introduction=introduction,
                story_keys=tuple(story or ()),
                actor_identifier=actor,
                published_at=datetime.now(UTC),
            )
        except (EditorialStateError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error
    console.print(
        f"Digest {digest.publication_date.isoformat()} published with "
        f"{len(digest.story_ids)} Stories. {introduction.strip()}",
        markup=False,
    )


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


@app.command("collect-feeds")
def collect_feeds(
    sample: Annotated[
        bool,
        typer.Option("--sample", help="Use fixed RSS, Atom, and failing Feed fixtures."),
    ] = False,
    retry_of: Annotated[
        UUID | None,
        typer.Option("--retry-of", help="Link this retry to an existing Collection Run."),
    ] = None,
) -> None:
    """Collect approved RSS and Atom Source Definitions."""
    try:
        database_url = database_url_from_environment()
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    if sample:
        run = collect_feed_source_definitions(
            database_url,
            source_definitions=load_sample_feed_source_definitions(),
            fetcher=SampleFeedFetcher(),
            clock=FixedClock(),
            retry_of_run_id=retry_of,
        )
    else:
        with httpx.Client() as client:
            run = collect_feed_source_definitions(
                database_url,
                source_definitions=load_approved_feed_source_definitions(),
                fetcher=HttpFeedFetcher(client),
                clock=SystemClock(),
                retry_of_run_id=retry_of,
            )

    succeeded = sum(
        result.status is SourceDefinitionCollectionStatus.SUCCEEDED
        for result in run.source_definition_results
    )
    console.print(
        f"[green]Completed Collection Run {run.id}:[/] {run.status.value}; "
        f"{succeeded}/{len(run.source_definition_results)} Source Definitions succeeded"
    )


@app.command("collect-gemini")
def collect_gemini(
    backfill_days: Annotated[
        int,
        typer.Option(
            "--backfill-days",
            min=1,
            max=3650,
            help="Collect dated sections in this many prior calendar days.",
        ),
    ] = 10,
) -> None:
    """Collect Gemini API Release Notes and prepare DeepSeek draft Stories."""
    try:
        if os.getenv("DEEPSEEK_API_KEY_FILE"):
            configuration = M1SchedulerConfiguration.from_environment(os.environ)
            engine = create_database_engine(configuration.database.database_url)
            try:
                provider_budget = _persistent_provider_budget(
                    engine,
                    configuration.provider,
                )
                _run_gemini_collection(
                    backfill_days,
                    database_url=configuration.database.database_url,
                    api_key=configuration.provider.api_key,
                    provider_budget=provider_budget,
                    structured_output=False,
                )
            finally:
                engine.dispose()
        else:
            _run_gemini_collection(
                backfill_days,
                database_url=database_url_from_environment(),
                api_key=deepseek_api_key_from_environment(),
                structured_output=False,
            )
    except (GeminiCollectionError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _run_gemini_collection(
    backfill_days: int,
    *,
    database_url: str,
    api_key: str,
    provider_budget: PersistentMeteredProviderBudget | None = None,
    structured_output: bool,
) -> None:
    with (
        httpx.Client(timeout=30, trust_env=False) as source_client,
        httpx.Client(timeout=180) as provider_client,
    ):
        summary = collect_gemini_release_notes(
            database_url,
            fetcher=HttpGeminiReleaseNotesFetcher(source_client),
            provider=DeepSeekGeminiDraftProvider(
                provider_client,
                api_key=api_key,
                budget=provider_budget,
            ),
            clock=SystemClock(),
            backfill_days=backfill_days,
        )
    if structured_output:
        console.print_json(
            data={
                "event": "gemini-collection-complete",
                "collection_run_id": str(summary.collection_run_id),
                "sections_collected": summary.sections_collected,
                "document_versions_created": summary.document_versions_created,
                "drafts_created": summary.drafts_created,
            }
        )
        return
    console.print(
        f"[green]Completed Gemini Collection Run {summary.collection_run_id}:[/] "
        f"sections_collected={summary.sections_collected}; "
        f"document_versions_created={summary.document_versions_created}; "
        f"drafts_created={summary.drafts_created}"
    )


@app.command("collect-sources")
def collect_sources(
    backfill_limit: Annotated[
        int,
        typer.Option(
            "--backfill-limit",
            min=1,
            max=100,
            help="Collect at most this many unseen entries per source and run.",
        ),
    ] = 5,
    operation_key: Annotated[
        str | None,
        typer.Option(
            "--operation-key",
            help="Replay-safe operation key; a unique manual key is generated by default.",
        ),
    ] = None,
) -> None:
    """Collect the five approved sources and prepare budgeted DeepSeek drafts."""
    requested_key = operation_key or f"m2-manual:{uuid4()}"
    try:
        if os.getenv("DEEPSEEK_API_KEY_FILE"):
            configuration = M1SchedulerConfiguration.from_environment(os.environ)
            engine = create_database_engine(configuration.database.database_url)
            try:
                provider_budget = _persistent_provider_budget(
                    engine,
                    configuration.provider,
                )
                _run_multisource_collection(
                    backfill_limit,
                    database_url=configuration.database.database_url,
                    api_key=configuration.provider.api_key,
                    operation_key=requested_key,
                    provider_budget=provider_budget,
                    structured_output=False,
                )
            finally:
                engine.dispose()
        else:
            _run_multisource_collection(
                backfill_limit,
                database_url=database_url_from_environment(),
                api_key=deepseek_api_key_from_environment(),
                operation_key=requested_key,
                structured_output=False,
            )
    except (RuntimeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _run_multisource_collection(
    backfill_limit: int,
    *,
    database_url: str,
    api_key: str,
    operation_key: str,
    provider_budget: PersistentMeteredProviderBudget | None = None,
    structured_output: bool,
) -> None:
    profiles = load_source_profiles()
    lease_engine = create_database_engine(database_url)
    try:
        with (
            PostgresCollectionLease(lease_engine) as lease,
            lease.monitor(lambda: os._exit(75)),
            httpx.Client(timeout=30, trust_env=False) as source_client,
            httpx.Client(timeout=180) as provider_client,
        ):
            summary = collect_source_profiles(
                database_url,
                profiles=profiles,
                feed_adapter=HttpFeedDiscoveryAdapter(source_client),
                article_adapter=HttpArticleAdapter(source_client),
                provider=DeepSeekGeminiDraftProvider(
                    provider_client,
                    api_key=api_key,
                    budget=provider_budget,
                ),
                clock=SystemClock(),
                operation_key=operation_key,
                backfill_limit=backfill_limit,
            )
    finally:
        lease_engine.dispose()
    payload = {
        "event": "multisource-collection-complete",
        "collection_run_id": str(summary.collection_run_id),
        "status": summary.status.value,
        "source_results": summary.source_results,
        "candidates_processed": summary.candidates_processed,
        "document_versions_created": summary.document_versions_created,
        "drafts_created": summary.drafts_created,
        "replayed": summary.replayed,
    }
    if structured_output:
        console.print_json(data=payload)
        return
    console.print_json(data=payload)


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
