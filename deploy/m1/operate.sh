#!/bin/sh
set -eu

state_dir="${AI_INTEL_STATE_DIR:-/etc/ai-ledger-m1/state}"
current_release="${state_dir}/current.env"
previous_release="${state_dir}/previous.env"
operation="${1:-}"
edge_network="ai-ledger-m1_edge"
legacy_edge_subnet="172.19.0.0/16"
expected_edge_subnet="172.31.255.0/24"
expected_edge_ip_range="172.31.255.128/25"
legacy_edge_contract="${legacy_edge_subnet}|"
expected_edge_contract="${expected_edge_subnet}|${expected_edge_ip_range}"
edge_migration_required=0
edge_migration_started=0
prepared_release=""
initial_edge_present=0

case "$operation" in
  "validate"|"start"|"upgrade"|"") ;;
  *)
    if [ -f "$current_release" ]; then
      recorded_dir="$(awk -F= '$1 == "AI_INTEL_RELEASE_DIR" {print substr($0, index($0, "=") + 1)}' "$current_release")"
      recorded_operator="${recorded_dir}/deploy/m1/operate.sh"
      if [ -f "$recorded_operator" ] && [ "$(realpath "$0")" != "$(realpath "$recorded_operator")" ]; then
        exec sh "$recorded_operator" "$@"
      fi
    fi
    ;;
esac

mkdir -p "$state_dir"
chmod 700 "$state_dir"
exec 9>"${state_dir}/operate.lock"
flock 9

usage() {
  printf '%s\n' 'usage: operate.sh validate RELEASE_ENV | start RELEASE_ENV | stop | restart | upgrade RELEASE_ENV | rollback | retrieval-rebuild | accept-retrieval QUERY SAMPLES MAX_P95_MS MAX_RSS_MB | status | logs | backup | restore-isolated BACKUP_BASENAME | operator COMMAND... | audit-no-secrets'
}

validate_release() {
  release_file="$1"
  test -f "$release_file"
  grep -Eq '^AI_INTEL_RELEASE=[0-9a-f]{40}$' "$release_file" || {
    printf '%s\n' 'AI_INTEL_RELEASE must be the exact 40-character commit SHA' >&2
    exit 2
  }
  grep -Eq '^AI_INTEL_IMAGE=[^[:space:]]+@sha256:[0-9a-f]{64}$' "$release_file" || {
    printf '%s\n' 'release image must use an immutable @sha256: digest' >&2
    exit 2
  }
  release_dir="$(release_value "$release_file" AI_INTEL_RELEASE_DIR)"
  release_sha="$(release_value "$release_file" AI_INTEL_RELEASE)"
  case "$release_dir" in
    /*) ;;
    *) printf '%s\n' 'AI_INTEL_RELEASE_DIR must be an absolute path' >&2; exit 2;;
  esac
  test -f "${release_dir}/deploy/m1/production.compose.yml"
  test "$(git -C "$release_dir" rev-parse HEAD)" = "$release_sha" || {
    printf '%s\n' 'release checkout HEAD does not match AI_INTEL_RELEASE' >&2
    exit 2
  }
  test -z "$(git -C "$release_dir" status --porcelain --untracked-files=all)" || {
    printf '%s\n' 'release checkout must be clean' >&2
    exit 2
  }
  offsite_dir="$(release_value "$release_file" AI_INTEL_OFFSITE_BACKUP_DIR)"
  mountpoint --quiet "$offsite_dir" || {
    printf '%s\n' 'AI_INTEL_OFFSITE_BACKUP_DIR must be a mounted off-host target' >&2
    exit 2
  }
  compose "$release_file" config --quiet
  caddy_image="$(
    compose "$release_file" config caddy |
      awk '$1 == "caddy:" {in_caddy=1; next} in_caddy && $1 == "image:" {print $2; exit}'
  )"
  test -n "$caddy_image" || {
    printf '%s\n' 'release Compose config does not resolve a Caddy image' >&2
    exit 2
  }
  docker run --rm --network none --read-only --cap-drop ALL --cap-add NET_BIND_SERVICE \
    --tmpfs /config --tmpfs /data \
    --env "AI_INTEL_DOMAIN=$(release_value "$release_file" AI_INTEL_DOMAIN)" \
    --volume "${release_dir}/deploy/m1/Caddyfile:/etc/caddy/Caddyfile:ro" \
    --entrypoint caddy "$caddy_image" validate --config /etc/caddy/Caddyfile
}

release_value() {
  release_file="$1"
  release_key="$2"
  awk -F= -v key="$release_key" '$1 == key {print substr($0, index($0, "=") + 1)}' "$release_file"
}

compose() {
  release_file="$1"
  shift
  release_dir="$(release_value "$release_file" AI_INTEL_RELEASE_DIR)"
  env \
    "COMPOSE_PROJECT_NAME=ai-ledger-m1" \
    "AI_INTEL_IMAGE=$(release_value "$release_file" AI_INTEL_IMAGE)" \
    "AI_INTEL_RELEASE=$(release_value "$release_file" AI_INTEL_RELEASE)" \
    "AI_INTEL_DOMAIN=$(release_value "$release_file" AI_INTEL_DOMAIN)" \
    "AI_INTEL_POSTGRES_DATABASE=$(release_value "$release_file" AI_INTEL_POSTGRES_DATABASE)" \
    "AI_INTEL_POSTGRES_USER=$(release_value "$release_file" AI_INTEL_POSTGRES_USER)" \
    "AI_INTEL_SECRETS_DIR=$(release_value "$release_file" AI_INTEL_SECRETS_DIR)" \
    "AI_INTEL_BACKUP_DIR=$(release_value "$release_file" AI_INTEL_BACKUP_DIR)" \
    "AI_INTEL_OFFSITE_BACKUP_DIR=$(release_value "$release_file" AI_INTEL_OFFSITE_BACKUP_DIR)" \
    "AI_INTEL_ANONYMOUS_RESEARCH_DAILY_LIMIT=$(release_value "$release_file" AI_INTEL_ANONYMOUS_RESEARCH_DAILY_LIMIT)" \
    "AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=$(release_value "$release_file" AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS)" \
    "AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=$(release_value "$release_file" AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS)" \
    "AI_INTEL_SCHEDULE_BACKFILL_LIMIT=$(release_value "$release_file" AI_INTEL_SCHEDULE_BACKFILL_LIMIT)" \
    "AI_INTEL_EMBEDDING_MODEL_DIR=$(release_value "$release_file" AI_INTEL_EMBEDDING_MODEL_DIR)" \
    "AI_INTEL_RERANKER_MODEL_DIR=$(release_value "$release_file" AI_INTEL_RERANKER_MODEL_DIR)" \
    "AI_INTEL_RETRIEVAL_THREADS=$(release_value "$release_file" AI_INTEL_RETRIEVAL_THREADS)" \
    "AI_INTEL_BACKUP_INTERVAL_SECONDS=$(release_value "$release_file" AI_INTEL_BACKUP_INTERVAL_SECONDS)" \
    "AI_INTEL_BACKUP_RETENTION_DAYS=$(release_value "$release_file" AI_INTEL_BACKUP_RETENTION_DAYS)" \
    docker compose --env-file "$release_file" --file "${release_dir}/deploy/m1/production.compose.yml" "$@"
}

validate_image_revision() {
  release_file="$1"
  image_ref="$(release_value "$release_file" AI_INTEL_IMAGE)"
  release_sha="$(release_value "$release_file" AI_INTEL_RELEASE)"
  image_revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_ref")"
  test "$image_revision" = "$release_sha" || {
    printf '%s\n' 'application image revision label does not match AI_INTEL_RELEASE' >&2
    exit 2
  }
}

validate_provider_qualification() {
  release_file="$1"
  qualification_file="$(release_value "$release_file" AI_INTEL_PROVIDER_QUALIFICATION_FILE)"
  release_dir="$(release_value "$release_file" AI_INTEL_RELEASE_DIR)"
  release_sha="$(release_value "$release_file" AI_INTEL_RELEASE)"
  case "$qualification_file" in
    /*) ;;
    *) printf '%s\n' 'AI_INTEL_PROVIDER_QUALIFICATION_FILE must be an absolute path' >&2; exit 2;;
  esac
  test -f "$qualification_file" || {
    printf '%s\n' 'live Research Provider qualification result is missing' >&2
    exit 2
  }
  protocol_file="${release_dir}/src/ai_intel_agent/data/research_protocol.v1.json"
  corpus_file="${release_dir}/src/ai_intel_agent/data/research_provider_qualification.v1.json"
  candidates_file="${release_dir}/src/ai_intel_agent/data/model_routing_candidates.v1.json"
  test -f "$protocol_file" && test -f "$corpus_file" && test -f "$candidates_file" || {
    printf '%s\n' 'release qualification contract files are missing' >&2
    exit 2
  }
  python3 - "$qualification_file" "$release_sha" "$release_dir" "$protocol_file" "$corpus_file" "$candidates_file" <<'PY'
import hashlib
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

(
    report_path,
    release_revision,
    release_root,
    protocol_path,
    corpus_path,
    candidates_path,
) = sys.argv[1:]


def reject(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


try:
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    protocol = json.loads(Path(protocol_path).read_text(encoding="utf-8"))
    corpus = json.loads(Path(corpus_path).read_text(encoding="utf-8"))
    candidates = json.loads(Path(candidates_path).read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    protocol_sha = hashlib.sha256(Path(protocol_path).read_bytes()).hexdigest()
    corpus_sha = hashlib.sha256(Path(corpus_path).read_bytes()).hexdigest()
    qualified_source_paths = corpus["qualified_source_paths"]
    if (
        not isinstance(qualified_source_paths, list)
        or not qualified_source_paths
        or len(set(qualified_source_paths)) != len(qualified_source_paths)
    ):
        reject("release qualification source paths are invalid")
    # Keep this stdlib-only preflight in lockstep with qualified_source_sha256().
    # It must run before the operator pulls or starts application containers.
    qualified_source_digest = hashlib.sha256()
    for relative_path in sorted(qualified_source_paths):
        if not isinstance(relative_path, str):
            reject("release qualification source path is invalid")
        normalized = PurePosixPath(relative_path)
        if (
            normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() != relative_path
        ):
            reject("release qualification source path is unsafe")
        source = Path(release_root).joinpath(*normalized.parts)
        if not source.is_file():
            reject("release qualification source file is missing")
        qualified_source_digest.update(relative_path.encode("utf-8"))
        qualified_source_digest.update(b"\0")
        content = source.read_bytes().replace(b"\r\n", b"\n")
        qualified_source_digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        qualified_source_digest.update(b"\n")
    qualified_source_sha = qualified_source_digest.hexdigest()
    results = report["results"]
    if not isinstance(results, list) or not results:
        reject("live Research Provider qualification contains no observations")
    expected_route = protocol["route_identifier"]
    approved_model = next(
        candidate["model_id"]
        for candidate in candidates["candidates"]
        if candidate["identifier"] == expected_route
    )
    expected_observations = sorted(
        (case["identifier"], repetition, case["expected_status"])
        for case in corpus["cases"]
        for repetition in range(1, int(case["repetitions"]) + 1)
    )
    observed_observations = sorted(
        (
            result["case_identifier"],
            int(result["repetition"]),
            result["expected_status"],
        )
        for result in results
    )
except (AttributeError, KeyError, OSError, StopIteration, TypeError, ValueError) as error:
    reject(f"live Research Provider qualification result is invalid: {type(error).__name__}")

if report.get("schema_version") != "research-provider-qualification-report.v1":
    reject("live Research Provider qualification schema is not approved")
if report.get("status") != "passed" or report.get("execution_mode") != "live-provider":
    reject("live Research Provider qualification did not pass")
if re.fullmatch(r"[0-9a-f]{40}", str(report.get("commit_sha", ""))) is None:
    reject("live Research Provider qualification revision is invalid")
if report.get("qualified_source_sha256") != qualified_source_sha:
    reject("live Research Provider qualification source does not match the release")
if report.get("protocol_sha256") != protocol_sha or report.get("corpus_sha256") != corpus_sha:
    reject("live Research Provider qualification contract hash does not match the release")
if (
    report.get("protocol_version") != protocol.get("version")
    or report.get("corpus_version") != corpus.get("version")
    or report.get("route_identifier") != expected_route
    or report.get("approved_model_id") != approved_model
):
    reject("live Research Provider qualification route or model is not approved")
if generated_at.tzinfo is None:
    reject("live Research Provider qualification timestamps must include a timezone")
maximum_attempts = report.get("maximum_provider_attempts")
reserved_cost = report.get("worst_case_reserved_cost_usd")
if (
    not isinstance(maximum_attempts, int)
    or maximum_attempts < len(results)
    or not isinstance(reserved_cost, (int, float))
    or reserved_cost <= 0
    or reserved_cost > float(corpus.get("maximum_cost_usd", 0))
):
    reject("live Research Provider qualification budget is invalid")
if generated_at > datetime.now(UTC) + timedelta(minutes=5):
    reject("live Research Provider qualification timestamp is in the future")
if observed_observations != expected_observations:
    reject("live Research Provider qualification does not cover the approved corpus")
if any(
    not isinstance(result, dict)
    or result.get("passed") is not True
    or result.get("validated_returned_model_id") != report.get("approved_model_id")
    or not isinstance(result.get("citation_count"), int)
    or (
        result.get("expected_status") == "answered"
        and result.get("citation_count", 0) < 1
    )
    or (
        result.get("expected_status") == "refused"
        and result.get("citation_count", 0) != 0
    )
    for result in results
):
    reject("live Research Provider qualification contains a failed observation")

print(
    json.dumps(
        {
            "event": "provider-qualification",
            "status": "passed",
            "release_commit_sha": release_revision,
            "qualified_commit_sha": report["commit_sha"],
            "qualified_source_sha256": qualified_source_sha,
            "protocol_sha256": protocol_sha,
            "corpus_sha256": corpus_sha,
        },
        separators=(",", ":"),
    )
)
PY
}

validate_candidate_contract() {
  release_file="$1"
  grep -Eq '^AI_INTEL_SCHEDULE_BACKFILL_LIMIT=[1-5]$' "$release_file" || {
    printf '%s\n' 'AI_INTEL_SCHEDULE_BACKFILL_LIMIT must be between 1 and 5' >&2
    exit 2
  }
  grep -Eq '^AI_INTEL_RETRIEVAL_THREADS=([1-9]|[1-5][0-9]|6[0-4])$' "$release_file" || {
    printf '%s\n' 'AI_INTEL_RETRIEVAL_THREADS must be between 1 and 64' >&2
    exit 2
  }
  for model_key in AI_INTEL_EMBEDDING_MODEL_DIR AI_INTEL_RERANKER_MODEL_DIR; do
    model_dir="$(release_value "$release_file" "$model_key")"
    case "$model_dir" in
      /*) ;;
      *) printf '%s\n' "${model_key} must be an absolute path" >&2; exit 2;;
    esac
    test -d "$model_dir" || {
      printf '%s\n' "${model_key} is not an available directory" >&2
      exit 2
    }
  done
  validate_provider_qualification "$release_file"
}

validate_candidate() {
  release_file="$1"
  validate_candidate_contract "$release_file"
  validate_release "$release_file"
  compose "$release_file" pull
  validate_image_revision "$release_file"
}

current_edge_contract() {
  edge_network_inspection="$(docker network inspect \
    --format '{{range .IPAM.Config}}{{printf "%s|%s\n" .Subnet .IPRange}}{{end}}' \
    "$edge_network" 2>/dev/null)" || return 1
  printf '%s\n' "$edge_network_inspection" | awk -F'|' '
    NF {
      if ($2 == "invalid Prefix") $2 = ""
      print $1 "|" $2
    }
  '
}

preflight_edge_network() {
  observed_edge_contract="$(current_edge_contract)" || {
    printf '%s\n' "cannot inspect ${edge_network}; refusing upgrade" >&2
    return 1
  }
  case "$observed_edge_contract" in
    "$expected_edge_contract") edge_migration_required=0 ;;
    "$legacy_edge_contract") edge_migration_required=1 ;;
    *)
      printf '%s\n' "${edge_network} does not match the verified legacy or fixed subnet and dynamic range; refusing upgrade" >&2
      return 1
      ;;
  esac
}

migrate_legacy_edge_network() {
  release_file="$1"
  observed_edge_contract="$(current_edge_contract)" || {
    printf '%s\n' "cannot re-inspect ${edge_network}; refusing edge mutation" >&2
    return 1
  }
  if [ "$edge_migration_required" = "0" ] && [ "$observed_edge_contract" = "$expected_edge_contract" ]; then
    return 0
  fi
  if [ "$edge_migration_required" != "1" ] || [ "$observed_edge_contract" != "$legacy_edge_contract" ]; then
    printf '%s\n' "${edge_network} changed after preflight; refusing edge mutation" >&2
    return 1
  fi

  edge_migration_started=1
  printf '%s\n' "migrating ${edge_network} from ${legacy_edge_subnet} to ${expected_edge_subnet} with dynamic range ${expected_edge_ip_range}" >&2
  compose "$release_file" rm --stop --force caddy web scheduler || return 1
  docker network rm "$edge_network" >/dev/null || return 1
}

validate_target_edge_network() {
  observed_edge_contract="$(current_edge_contract)" || {
    printf '%s\n' "cannot inspect candidate ${edge_network}" >&2
    return 1
  }
  test "$observed_edge_contract" = "$expected_edge_contract" || {
    printf '%s\n' "candidate ${edge_network} does not use the required subnet and dynamic range" >&2
    return 1
  }
}

restore_legacy_edge_network() {
  observed_edge_contract="$(current_edge_contract 2>/dev/null || true)"
  if [ "$observed_edge_contract" = "$legacy_edge_contract" ]; then
    return 0
  fi
  if docker network inspect "$edge_network" >/dev/null 2>&1; then
    docker network rm "$edge_network" >/dev/null || return 1
  fi
  docker network create \
    --driver bridge \
    --subnet "$legacy_edge_subnet" \
    --label com.docker.compose.project=ai-ledger-m1 \
    --label com.docker.compose.network=edge \
    "$edge_network" >/dev/null || return 1
  test "$(current_edge_contract)" = "$legacy_edge_contract"
}

restore_current_runtime() {
  candidate_file="$1"
  recovery_failed=0

  compose "$candidate_file" rm --stop --force caddy web scheduler || recovery_failed=1
  if [ "$edge_migration_started" = "1" ]; then
    restore_legacy_edge_network || recovery_failed=1
  fi
  compose "$current_release" up --detach --wait postgres web scheduler backup caddy || recovery_failed=1

  if [ "$recovery_failed" -ne 0 ]; then
    printf '%s\n' 'current release recovery did not complete cleanly' >&2
    return 1
  fi
}

cleanup_initial_runtime() {
  release_file="$1"
  cleanup_failed=0
  compose "$release_file" rm --stop --force caddy web scheduler backup postgres || cleanup_failed=1
  if [ "$initial_edge_present" = "0" ] && docker network inspect "$edge_network" >/dev/null 2>&1; then
    edge_owner="$(docker network inspect \
      --format '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}' \
      "$edge_network" 2>/dev/null || true)"
    if [ "$edge_owner" = "ai-ledger-m1|edge" ]; then
      docker network rm "$edge_network" >/dev/null || cleanup_failed=1
    else
      printf '%s\n' 'preserving an edge network not owned by this Compose project' >&2
    fi
  fi
  if [ "$cleanup_failed" -ne 0 ]; then
    printf '%s\n' 'failed initial start did not clean up completely' >&2
    return 1
  fi
}

migrate() {
  release_file="$1"
  compose "$release_file" --profile ops run --rm --no-deps migrate operator migrate --production
}

activate_release() {
  release_file="$1"
  run_migrations="$2"
  if [ "$prepared_release" = "$release_file" ]; then
    prepared_release=""
  else
    validate_release "$release_file" || return 1
    compose "$release_file" pull || return 1
    validate_image_revision "$release_file" || return 1
  fi
  compose "$release_file" up --detach --wait postgres || return 1
  if [ "$run_migrations" = "1" ]; then
    migrate "$release_file" || return 1
  fi
  compose "$release_file" up --detach --wait postgres web scheduler backup caddy || return 1
}

start_release() {
  release_file="$1"
  validate_candidate_contract "$release_file"
  activate_release "$release_file" 1 || return 1
  validate_target_edge_network
}

record_current() {
  release_file="$1"
  install -m 600 "$release_file" "${current_release}.new"
  mv "${current_release}.new" "$current_release"
}

require_current() {
  test -f "$current_release" || {
    printf '%s\n' 'no current release is recorded' >&2
    exit 2
  }
}

rebuild_retrieval() {
  compose "$current_release" --profile ops run --rm retrieval-index
}

accept_retrieval() {
  query="$1"
  samples="$2"
  maximum_p95_ms="$3"
  maximum_rss_mb="$4"
  case "$samples" in
    ''|*[!0-9]*) printf '%s\n' 'SAMPLES must be a positive integer' >&2; exit 2;;
  esac
  test "$samples" -ge 5 || {
    printf '%s\n' 'SAMPLES must be at least 5' >&2
    exit 2
  }
  test -f "$previous_release" || {
    printf '%s\n' 'no rollback release is recorded' >&2
    exit 2
  }
  validate_release "$current_release"
  validate_image_revision "$current_release"
  validate_release "$previous_release"
  validate_image_revision "$previous_release"
  compose "$current_release" exec --no-TTY web \
    ai-intel-agent operator retrieval artifacts
  compose "$current_release" exec --no-TTY web python -c \
    "import json,urllib.request; response=urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=5); payload=json.load(response); assert response.status == 200 and payload == {'status': 'ready'}; print(json.dumps({'health':'ready'}, separators=(',', ':')))"
  compose "$current_release" exec --no-TTY web \
    python - "$query" "$samples" "$maximum_p95_ms" <<'PY'
import json
import math
import sys
import time
import urllib.parse
import urllib.request

query, raw_samples, raw_maximum = sys.argv[1:]
samples = int(raw_samples)
maximum = float(raw_maximum)
url = "http://127.0.0.1:8000/browse?" + urllib.parse.urlencode({"q": query})
urllib.request.urlopen(url, timeout=30).read()
observations = []
for _ in range(samples):
    started = time.perf_counter()
    response = urllib.request.urlopen(url, timeout=30)
    response.read()
    if response.status != 200:
        raise SystemExit("Retrieval probe returned a non-200 response")
    observations.append((time.perf_counter() - started) * 1000.0)
observations.sort()
p50 = observations[math.ceil(0.50 * samples) - 1]
p95 = observations[math.ceil(0.95 * samples) - 1]
print(json.dumps({"samples": samples, "p50_ms": p50, "p95_ms": p95}, separators=(",", ":")))
if p95 > maximum:
    raise SystemExit("Retrieval P95 exceeds the acceptance limit")
PY
  compose "$current_release" exec --no-TTY web \
    ai-intel-agent operator retrieval status --production --require-hybrid
  web_container="$(compose "$current_release" ps --quiet web)"
  service_rss_output="$(docker top "$web_container" -eo pid,rss)"
  service_rss_kib="$(printf '%s\n' "$service_rss_output" | awk 'NR > 1 { total += $2; rows += 1 } END { if (rows == 0) exit 1; print total + 0 }')"
  python3 - "$service_rss_kib" "$maximum_rss_mb" <<'PY'
import json
import sys

observed_kib, raw_maximum = sys.argv[1:]
rss_mb = float(observed_kib) / 1024.0
maximum = float(raw_maximum)
print(json.dumps({"service_rss_mb": rss_mb}, separators=(",", ":")))
if rss_mb > maximum:
    raise SystemExit("Web service RSS exceeds the acceptance limit")
PY
}

case "$operation" in
  "validate")
    test "$#" -eq 2 || { usage; exit 2; }
    validate_candidate "$2"
    ;;
  "start")
    test "$#" -eq 2 || { usage; exit 2; }
    test ! -f "$current_release" || {
      printf '%s\n' 'a current release is already recorded; use upgrade' >&2
      exit 2
    }
    if docker network inspect "$edge_network" >/dev/null 2>&1; then
      initial_edge_present=1
    fi
    if start_release "$2"; then
      record_current "$2"
    else
      cleanup_initial_runtime "$2" || true
      exit 1
    fi
    ;;
  "stop")
    require_current
    compose "$current_release" stop --timeout 30 caddy web scheduler backup postgres
    ;;
  "restart")
    require_current
    compose "$current_release" restart caddy web scheduler backup postgres
    compose "$current_release" up --detach --wait postgres web scheduler backup caddy
    ;;
  "upgrade")
    test "$#" -eq 2 || { usage; exit 2; }
    require_current
    candidate="$2"
    validate_candidate "$candidate"
    preflight_edge_network
    compose "$current_release" run --rm --no-deps --env AI_INTEL_BACKUP_ONCE=1 backup
    if ! migrate_legacy_edge_network "$current_release"; then
      if [ "$edge_migration_started" = "1" ]; then
        restore_current_runtime "$candidate" || true
      fi
      exit 1
    fi
    prepared_release="$candidate"
    if start_release "$candidate"; then
      install -m 600 "$current_release" "$previous_release"
      record_current "$candidate"
    else
      restore_current_runtime "$candidate" || true
      exit 1
    fi
    ;;
  "rollback")
    require_current
    test -f "$previous_release" || { printf '%s\n' 'no previous release is recorded' >&2; exit 2; }
    if activate_release "$previous_release" 0; then
      temporary="${state_dir}/rollback.env"
      install -m 600 "$current_release" "$temporary"
      install -m 600 "$previous_release" "$current_release"
      mv "$temporary" "$previous_release"
    else
      printf '%s\n' 'rollback target failed; restoring the recorded current release' >&2
      activate_release "$current_release" 0 || {
        printf '%s\n' 'recorded current release recovery failed' >&2
        exit 1
      }
      exit 1
    fi
    ;;
  "retrieval-rebuild")
    test "$#" -eq 1 || { usage; exit 2; }
    require_current
    rebuild_retrieval
    ;;
  "accept-retrieval")
    test "$#" -eq 5 || { usage; exit 2; }
    require_current
    accept_retrieval "$2" "$3" "$4" "$5"
    ;;
  "status")
    require_current
    compose "$current_release" ps
    compose "$current_release" exec --no-TTY web ai-intel-agent operator status --production
    ;;
  "logs")
    require_current
    compose "$current_release" logs --since 24h caddy web scheduler postgres backup
    ;;
  "backup")
    require_current
    compose "$current_release" run --rm --no-deps --env AI_INTEL_BACKUP_ONCE=1 backup
    ;;
  "restore-isolated")
    test "$#" -eq 2 || { usage; exit 2; }
    require_current
    case "$2" in ""|*/*|*..*) printf '%s\n' 'invalid backup basename' >&2; exit 2;; esac
    compose "$current_release" --profile restore up --detach --wait restore-postgres
    AI_INTEL_RESTORE_FILE="$2" compose "$current_release" --profile restore run --rm --no-deps restore
    ;;
  "operator")
    test "$#" -ge 2 || { usage; exit 2; }
    require_current
    shift
    compose "$current_release" exec --no-TTY web ai-intel-agent "$@"
    ;;
  "audit-no-secrets")
    require_current
    temporary_dir="$(mktemp -d)"
    trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM
    compose "$current_release" logs --no-color >"${temporary_dir}/service.log"
    image_ref="$(awk -F= '$1 == "AI_INTEL_IMAGE" {print substr($0, index($0, "=") + 1)}' "$current_release")"
    secrets_dir="$(awk -F= '$1 == "AI_INTEL_SECRETS_DIR" {print substr($0, index($0, "=") + 1)}' "$current_release")"
    docker image save --output "${temporary_dir}/image.tar" "$image_ref"
    mkdir "${temporary_dir}/image"
    tar -xf "${temporary_dir}/image.tar" -C "${temporary_dir}/image"
    failed=0
    for secret_name in database-password deepseek-api-key anonymous-id-salt; do
      secret_file="${secrets_dir}/${secret_name}"
      test -s "$secret_file"
      if grep -R -a -F -f "$secret_file" "${temporary_dir}/service.log" "${temporary_dir}/image" >/dev/null 2>&1; then
        failed=1
      fi
      project_root="$(release_value "$current_release" AI_INTEL_RELEASE_DIR)"
      if git -C "$project_root" grep -I -l -F -f "$secret_file" -- . >/dev/null 2>&1; then
        failed=1
      fi
    done
    if [ "$failed" -ne 0 ]; then
      printf '%s\n' '{"event":"secret-audit","status":"failed"}' >&2
      exit 1
    fi
    printf '%s\n' '{"event":"secret-audit","status":"passed"}'
    ;;
  *)
    usage
    exit 2
    ;;
esac
