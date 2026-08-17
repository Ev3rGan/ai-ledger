#!/bin/sh
set -eu

state_dir="${AI_INTEL_STATE_DIR:-/etc/ai-ledger-m1/state}"
current_release="${state_dir}/current.env"
previous_release="${state_dir}/previous.env"
operation="${1:-}"

case "$operation" in
  "validate"|"start"|"") ;;
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
  printf '%s\n' 'usage: operate.sh validate RELEASE_ENV | start RELEASE_ENV | stop | restart | upgrade RELEASE_ENV | rollback | status | logs | backup | restore-isolated BACKUP_BASENAME | audit-no-secrets'
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
  compose "$release_file" run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
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
  docker compose --env-file "$release_file" --file "${release_dir}/deploy/m1/production.compose.yml" "$@"
}

migrate() {
  release_file="$1"
  compose "$release_file" --profile ops run --rm --no-deps migrate operator migrate --production
}

activate_release() {
  release_file="$1"
  run_migrations="$2"
  validate_release "$release_file" || return 1
  compose "$release_file" pull || return 1
  compose "$release_file" up --detach --wait postgres || return 1
  if [ "$run_migrations" = "1" ]; then
    migrate "$release_file" || return 1
  fi
  compose "$release_file" up --detach --wait postgres web scheduler backup caddy || return 1
}

start_release() {
  release_file="$1"
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
    validate_release "$2"
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
    if start_release "$candidate"; then
      install -m 600 "$current_release" "$previous_release"
      record_current "$candidate"
    else
      compose "$current_release" up --detach --wait postgres web scheduler backup caddy
      exit 1
    fi
    ;;
  "rollback")
    require_current
    test -f "$previous_release" || { printf '%s\n' 'no previous release is recorded' >&2; exit 2; }
    activate_release "$previous_release" 0
    temporary="${state_dir}/rollback.env"
    install -m 600 "$current_release" "$temporary"
    install -m 600 "$previous_release" "$current_release"
    mv "$temporary" "$previous_release"
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
