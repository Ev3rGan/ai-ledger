#!/bin/sh
set -eu

state_dir="${AI_INTEL_STATE_DIR:-/etc/ai-ledger-m1/state}"
current_release="${state_dir}/current.env"
previous_release="${state_dir}/previous.env"
operation="${1:-}"
edge_network="ai-ledger-m1_edge"
legacy_edge_subnet="172.19.0.0/16"
expected_edge_subnet="172.31.255.0/24"
edge_migration_required=0
edge_migration_started=0
prepared_release=""

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
  printf '%s\n' 'usage: operate.sh validate RELEASE_ENV | start RELEASE_ENV | stop | restart | upgrade RELEASE_ENV | rollback | status | logs | backup | restore-isolated BACKUP_BASENAME | operator COMMAND... | audit-no-secrets'
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

validate_candidate_contract() {
  release_file="$1"
  grep -Eq '^AI_INTEL_SCHEDULE_BACKFILL_LIMIT=[1-5]$' "$release_file" || {
    printf '%s\n' 'AI_INTEL_SCHEDULE_BACKFILL_LIMIT must be between 1 and 5' >&2
    exit 2
  }
}

validate_candidate() {
  release_file="$1"
  validate_candidate_contract "$release_file"
  validate_release "$release_file"
  compose "$release_file" pull
  validate_image_revision "$release_file"
}

current_edge_subnet() {
  edge_network_inspection="$(docker network inspect \
    --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' \
    "$edge_network" 2>/dev/null)" || return 1
  printf '%s\n' "$edge_network_inspection" | awk 'NF {print; exit}'
}

preflight_edge_network() {
  observed_edge_subnet="$(current_edge_subnet)" || {
    printf '%s\n' "cannot inspect ${edge_network}; refusing upgrade" >&2
    return 1
  }
  case "$observed_edge_subnet" in
    "$expected_edge_subnet") edge_migration_required=0 ;;
    "$legacy_edge_subnet") edge_migration_required=1 ;;
    *)
      printf '%s\n' "${edge_network} is not the verified legacy or fixed network; refusing upgrade" >&2
      return 1
      ;;
  esac
}

migrate_legacy_edge_network() {
  release_file="$1"
  observed_edge_subnet="$(current_edge_subnet)" || {
    printf '%s\n' "cannot re-inspect ${edge_network}; refusing edge mutation" >&2
    return 1
  }
  if [ "$edge_migration_required" = "0" ] && [ "$observed_edge_subnet" = "$expected_edge_subnet" ]; then
    return 0
  fi
  if [ "$edge_migration_required" != "1" ] || [ "$observed_edge_subnet" != "$legacy_edge_subnet" ]; then
    printf '%s\n' "${edge_network} changed after preflight; refusing edge mutation" >&2
    return 1
  fi

  edge_migration_started=1
  printf '%s\n' "migrating ${edge_network} from ${legacy_edge_subnet} to ${expected_edge_subnet}" >&2
  compose "$release_file" rm --stop --force caddy web scheduler || return 1
  docker network rm "$edge_network" >/dev/null || return 1
}

restore_current_runtime() {
  candidate_file="$1"
  recovery_failed=0

  compose "$candidate_file" rm --stop --force caddy web scheduler || recovery_failed=1
  if [ "$edge_migration_started" = "1" ] && docker network inspect "$edge_network" >/dev/null 2>&1; then
    docker network rm "$edge_network" >/dev/null || recovery_failed=1
  fi
  compose "$current_release" up --detach --wait postgres web scheduler backup caddy || recovery_failed=1

  if [ "$recovery_failed" -ne 0 ]; then
    printf '%s\n' 'current release recovery did not complete cleanly' >&2
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
  activate_release "$release_file" 1
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

case "$operation" in
  "validate")
    test "$#" -eq 2 || { usage; exit 2; }
    validate_candidate "$2"
    ;;
  "start")
    test "$#" -eq 2 || { usage; exit 2; }
    start_release "$2"
    record_current "$2"
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
