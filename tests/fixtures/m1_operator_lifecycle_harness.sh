#!/usr/bin/env bash
set -euo pipefail

operator="${1:?usage: m1_operator_lifecycle_harness.sh OPERATE_SH [CASE]}"
selected_case="${2:-all}"
current_release="d83a3ae70fa970893fde0e669864c677ef49392a"
candidate_release="5477c5538248f97fe6db331d7d33cdea384966c1"
legacy_subnet="172.19.0.0/16"
fixed_subnet="172.31.255.0/24"
fixed_ip_range="172.31.255.128/25"

test_root="$(mktemp -d "${TMPDIR:-/tmp}/m4-edge-lifecycle.XXXXXX")"
trap 'rm -rf "$test_root"' EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_equal() {
  expected="$1"
  actual="$2"
  message="$3"
  [ "$actual" = "$expected" ] || fail "$message (expected=$expected actual=$actual)"
}

assert_file_release() {
  release_file="$1"
  expected="$2"
  actual="$(awk -F= '$1 == "AI_INTEL_RELEASE" {print $2}' "$release_file")"
  assert_equal "$expected" "$actual" "unexpected recorded release in $release_file"
}

state_value() {
  state_file="$1"
  key="$2"
  awk -F= -v key="$key" '$1 == key {print $2}' "$state_file"
}

set_state_value() {
  state_file="$1"
  key="$2"
  value="$3"
  awk -F= -v key="$key" -v value="$value" '
    $1 == key {$0 = key "=" value}
    {print}
  ' "$state_file" >"$state_file.new"
  mv "$state_file.new" "$state_file"
}

write_release() {
  release_file="$1"
  release_sha="$2"
  release_dir="$3"
  image_digit="$4"
  cat >"$release_file" <<EOF
AI_INTEL_IMAGE=registry.example/ai-ledger@sha256:$(printf '%064d' 0 | tr 0 "$image_digit")
AI_INTEL_RELEASE=$release_sha
AI_INTEL_RELEASE_DIR=$release_dir
AI_INTEL_DOMAIN=public.example
AI_INTEL_POSTGRES_DATABASE=ai_ledger
AI_INTEL_POSTGRES_USER=ai_ledger
AI_INTEL_SECRETS_DIR=/run/fixture-secrets
AI_INTEL_BACKUP_DIR=/run/fixture-backups
AI_INTEL_OFFSITE_BACKUP_DIR=/run/fixture-offsite
AI_INTEL_ANONYMOUS_RESEARCH_DAILY_LIMIT=3
AI_INTEL_PROVIDER_MONTHLY_BUDGET_CENTS=10000
AI_INTEL_PROVIDER_REQUEST_RESERVATION_CENTS=100
AI_INTEL_SCHEDULE_BACKFILL_LIMIT=5
AI_INTEL_BACKUP_INTERVAL_SECONDS=86400
AI_INTEL_BACKUP_RETENTION_DAYS=14
EOF
}

new_fixture() {
  fixture_name="$1"
  fixture="$test_root/$fixture_name"
  state_dir="$fixture/state"
  fake_bin="$fixture/bin"
  current_dir="$fixture/current-release"
  candidate_dir="$fixture/candidate-release"
  mkdir -p "$state_dir" "$fake_bin" \
    "$current_dir/deploy/m1" "$candidate_dir/deploy/m1" "$fixture/offsite"

  printf '%s\n' "$current_release" >"$current_dir/.fake-head"
  printf '%s\n' "$candidate_release" >"$candidate_dir/.fake-head"
  for release_dir in "$current_dir" "$candidate_dir"; do
    printf '%s\n' 'services: {}' >"$release_dir/deploy/m1/production.compose.yml"
    printf '%s\n' 'public.example { respond 200 }' >"$release_dir/deploy/m1/Caddyfile"
  done

  current_file="$state_dir/current.env"
  candidate_file="$fixture/candidate.env"
  write_release "$current_file" "$current_release" "$current_dir" a
  write_release "$candidate_file" "$candidate_release" "$candidate_dir" b
  runtime_state="$fixture/runtime.state"
  printf 'release=%s\nsubnet=%s\nip_range=none\nowner=compose\nconnected=1\n' \
    "$current_release" "$legacy_subnet" >"$runtime_state"
  docker_log="$fixture/docker.log"
  failure_used="$fixture/failure.used"

  cat >"$fake_bin/git" <<'EOF'
#!/bin/sh
set -eu
test "$1" = "-C"
release_dir="$2"
shift 2
case "$1 $2" in
  "rev-parse HEAD") cat "$release_dir/.fake-head" ;;
  "status --porcelain") exit 0 ;;
  *) printf 'unexpected git invocation: %s\n' "$*" >&2; exit 97 ;;
esac
EOF
  cat >"$fake_bin/mountpoint" <<'EOF'
#!/bin/sh
exit 0
EOF
  cat >"$fake_bin/flock" <<'EOF'
#!/bin/sh
exit 0
EOF
  cat >"$fake_bin/docker" <<'EOF'
#!/bin/sh
set -eu

state_get() {
  awk -F= -v key="$1" '$1 == key {print $2}' "$FAKE_DOCKER_STATE"
}

state_set() {
  key="$1"
  value="$2"
  awk -F= -v key="$key" -v value="$value" \
    '$1 == key {print key "=" value; found=1; next} {print} END {if (!found) print key "=" value}' \
    "$FAKE_DOCKER_STATE" >"${FAKE_DOCKER_STATE}.new"
  mv "${FAKE_DOCKER_STATE}.new" "$FAKE_DOCKER_STATE"
}

fail_once() {
  point="$1"
  if [ "${FAKE_FAIL_POINT:-}" = "$point" ] && [ ! -f "$FAKE_FAILURE_USED" ]; then
    : >"$FAKE_FAILURE_USED"
    return 0
  fi
  return 1
}

ensure_network_for_release() {
  if [ "$(state_get owner)" = foreign ]; then
    return 60
  fi
  if [ "$(state_get subnet)" != "none" ]; then
    return 0
  fi
  if [ "$release_sha" = "$FAKE_CANDIDATE_RELEASE" ]; then
    state_set subnet "$FAKE_FIXED_SUBNET"
    if fail_once target-ip-range; then
      state_set ip_range 172.31.255.64/26
    else
      state_set ip_range "$FAKE_FIXED_IP_RANGE"
    fi
    state_set owner compose
  else
    state_set subnet 172.20.0.0/16
    state_set ip_range none
    state_set owner compose
  fi
}

release_file=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "--env-file" ]; then
    release_file="$argument"
    break
  fi
  previous="$argument"
done
release_sha=""
if [ -n "$release_file" ] && [ -f "$release_file" ]; then
  release_sha="$(awk -F= '$1 == "AI_INTEL_RELEASE" {print $2}' "$release_file")"
fi

printf '%s\n' "$*" >>"$FAKE_DOCKER_LOG"
case " $* " in
  *" compose "*" config --quiet ")
    printf 'compose:%s:config-quiet\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    ;;
  *" compose "*" config caddy ")
    printf 'compose:%s:caddy-image\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    printf '%s\n' 'services:' '  caddy:' '    image: caddy:fixture@sha256:cccc'
    ;;
  *" compose "*" run --rm --no-deps caddy "*)
    printf 'compose:%s:caddy-validate-project\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    state_set connected 0
    printf '%s\n' 'container fixture is not connected to network ai-ledger-m1_edge' >&2
    exit 41
    ;;
  *" run --rm --network none "*)
    case "$*" in
      *" caddy:fixture@sha256:cccc "*) ;;
      *) printf '%s\n' 'isolated validation did not select the Caddy image' >&2; exit 42 ;;
    esac
    case "$*" in
      *" --read-only --cap-drop ALL --cap-add NET_BIND_SERVICE "*) ;;
      *) printf '%s\n' 'isolated validation did not grant only NET_BIND_SERVICE after dropping all capabilities' >&2; exit 43 ;;
    esac
    printf '%s\n' 'standalone:caddy-validate' >>"$FAKE_EVENT_LOG"
    ;;
  *" image inspect "*)
    case "$*" in
      *"$FAKE_CANDIDATE_IMAGE"*) printf '%s\n' "$FAKE_CANDIDATE_RELEASE" ;;
      *) printf '%s\n' "$FAKE_CURRENT_RELEASE" ;;
    esac
    ;;
  *" compose "*" pull "*)
    printf 'compose:%s:pull\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    ;;
  *" compose "*" run --rm --no-deps --env AI_INTEL_BACKUP_ONCE=1 backup "*)
    printf 'compose:%s:backup\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    if fail_once backup; then exit 51; fi
    ;;
  *" compose "*" --profile ops run --rm --no-deps migrate "*)
    printf 'compose:%s:migrate\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    if fail_once migrate; then exit 52; fi
    ;;
  *" compose "*" rm --stop --force caddy web scheduler backup postgres "*)
    printf 'compose:%s:cleanup-initial\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    state_set release none
    state_set connected 0
    ;;
  *" compose "*" rm --stop --force caddy web scheduler "*)
    printf 'compose:%s:detach-edge\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    state_set connected 0
    if fail_once detach; then exit 53; fi
    ;;
  *" network inspect "*)
    if fail_once edge-inspect; then exit 57; fi
    subnet="$(state_get subnet)"
    ip_range="$(state_get ip_range)"
    [ "$subnet" != "none" ] || exit 1
    case "$*" in
      *".Id"*) printf '%s\n' "edge-id-$(state_get owner)" ;;
      *".Labels"*)
        if [ "$(state_get owner)" = compose ]; then
          printf '%s\n' 'ai-ledger-m1|edge'
        else
          printf '%s\n' 'foreign|foreign'
        fi
        ;;
      *".IPRange"*)
        if [ "$ip_range" = none ]; then ip_range="invalid Prefix"; fi
        printf '%s|%s\n' "$subnet" "$ip_range"
        ;;
      *"--format"*) printf '%s\n' "$subnet" ;;
      *) printf '%s\n' '[]' ;;
    esac
    ;;
  *" network create "*" ai-ledger-m1_edge "*)
    case "$*" in
      *"--driver bridge"*"--subnet $FAKE_LEGACY_SUBNET"*"--label com.docker.compose.project=ai-ledger-m1"*"--label com.docker.compose.network=edge"*) ;;
      *) printf '%s\n' 'legacy recovery network lacks the required contract' >&2; exit 59 ;;
    esac
    printf '%s\n' 'network:restore-legacy' >>"$FAKE_EVENT_LOG"
    [ "$(state_get subnet)" = none ] || exit 58
    state_set subnet "$FAKE_LEGACY_SUBNET"
    state_set ip_range none
    state_set owner compose
    state_set connected 0
    printf '%s\n' 'restored-edge-id'
    ;;
  *" network rm ai-ledger-m1_edge "*)
    printf '%s\n' 'network:remove-edge' >>"$FAKE_EVENT_LOG"
    if fail_once edge-rm; then exit 54; fi
    state_set subnet none
    state_set ip_range none
    state_set owner none
    state_set connected 0
    printf '%s\n' 'ai-ledger-m1_edge'
    ;;
  *" compose "*" up --detach --wait postgres ")
    printf 'compose:%s:up-postgres\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    ensure_network_for_release
    ;;
  *" compose "*" up --detach --wait postgres web scheduler backup caddy ")
    printf 'compose:%s:up-services\n' "$release_sha" >>"$FAKE_EVENT_LOG"
    ensure_network_for_release
    if [ "$release_sha" = "$FAKE_CANDIDATE_RELEASE" ]; then
      if fail_once candidate-up; then exit 55; fi
    else
      if fail_once rollback-up; then exit 56; fi
    fi
    state_set release "$release_sha"
    state_set connected 1
    ;;
  *)
    printf 'unexpected docker invocation: %s\n' "$*" >&2
    exit 97
    ;;
esac
EOF
  chmod +x "$fake_bin/git" "$fake_bin/mountpoint" "$fake_bin/flock" "$fake_bin/docker"

  export PATH="$fake_bin:/usr/bin:/bin"
  export AI_INTEL_STATE_DIR="$state_dir"
  export FAKE_DOCKER_STATE="$runtime_state"
  export FAKE_DOCKER_LOG="$docker_log"
  export FAKE_EVENT_LOG="$fixture/events.log"
  export FAKE_FAILURE_USED="$failure_used"
  export FAKE_CURRENT_RELEASE="$current_release"
  export FAKE_CANDIDATE_RELEASE="$candidate_release"
  export FAKE_CURRENT_IMAGE="registry.example/ai-ledger@sha256:$(printf '%064d' 0 | tr 0 a)"
  export FAKE_CANDIDATE_IMAGE="registry.example/ai-ledger@sha256:$(printf '%064d' 0 | tr 0 b)"
  export FAKE_LEGACY_SUBNET="$legacy_subnet"
  export FAKE_FIXED_SUBNET="$fixed_subnet"
  export FAKE_FIXED_IP_RANGE="$fixed_ip_range"
  export FAKE_FAIL_POINT=""
  : >"$FAKE_EVENT_LOG"
}

run_operator() {
  sh "$operator" "$@"
}

case_validate_no_side_effect() {
  new_fixture validate
  before="$(cat "$runtime_state")"
  set +e
  run_operator validate "$candidate_file"
  result=$?
  set -e
  if [ "$result" -ne 0 ]; then
    fail "validate exited $result after changing connected from 1 to $(state_value "$runtime_state" connected)"
  fi
  assert_equal "$before" "$(cat "$runtime_state")" "validate changed the current runtime"
  grep -qx 'standalone:caddy-validate' "$FAKE_EVENT_LOG" || fail "validate did not use isolated Caddy validation"
  ! grep -q 'caddy-validate-project' "$FAKE_EVENT_LOG" || fail "validate used the project network"
}

case_upgrade_success() {
  new_fixture upgrade-success
  run_operator upgrade "$candidate_file"
  assert_equal "$candidate_release" "$(state_value "$runtime_state" release)" "candidate did not become active"
  assert_equal "$fixed_subnet" "$(state_value "$runtime_state" subnet)" "edge network was not pinned"
  assert_equal "$fixed_ip_range" "$(state_value "$runtime_state" ip_range)" "edge dynamic range was not pinned"
  assert_equal 1 "$(state_value "$runtime_state" connected)" "candidate is disconnected"
  assert_file_release "$current_file" "$candidate_release"
  assert_file_release "$state_dir/previous.env" "$current_release"
  events="$(cat "$FAKE_EVENT_LOG")"
  case "$events" in
    *"compose:$candidate_release:pull"*"compose:$current_release:backup"*"compose:$current_release:detach-edge"*"network:remove-edge"*"compose:$candidate_release:migrate"*"compose:$candidate_release:up-services"*) ;;
    *) fail "upgrade did not validate/pull, back up, migrate edge, migrate DB, then activate in order" ;;
  esac
}

case_preflight_failure_no_side_effect() {
  for failure_mode in edge-inspect unexpected-subnet missing-ip-range unexpected-ip-range legacy-ip-range; do
    new_fixture "preflight-$failure_mode"
    before_runtime="$(cat "$runtime_state")"
    before_current="$(cat "$current_file")"
    if [ "$failure_mode" = edge-inspect ]; then
      export FAKE_FAIL_POINT=edge-inspect
    else
      case "$failure_mode" in
        unexpected-subnet) set_state_value "$runtime_state" subnet 10.99.0.0/16 ;;
        missing-ip-range) set_state_value "$runtime_state" subnet "$fixed_subnet" ;;
        unexpected-ip-range)
          set_state_value "$runtime_state" subnet "$fixed_subnet"
          set_state_value "$runtime_state" ip_range 172.31.255.64/26
          ;;
        legacy-ip-range) set_state_value "$runtime_state" ip_range 172.19.128.0/17 ;;
      esac
      before_runtime="$(cat "$runtime_state")"
    fi
    if run_operator upgrade "$candidate_file"; then
      fail "upgrade unexpectedly accepted $failure_mode"
    fi
    assert_equal "$before_runtime" "$(cat "$runtime_state")" "preflight changed runtime for $failure_mode"
    assert_equal "$before_current" "$(cat "$current_file")" "preflight changed current record for $failure_mode"
    [ ! -e "$state_dir/previous.env" ] || fail "preflight recorded previous for $failure_mode"
    for forbidden_event in backup detach-edge remove-edge up-postgres up-services; do
      ! grep -q "$forbidden_event" "$FAKE_EVENT_LOG" || fail "preflight reached $forbidden_event for $failure_mode"
    done
  done
}

case_upgrade_failure_recovery() {
  for failure_point in detach edge-rm migrate candidate-up; do
    new_fixture "upgrade-failure-$failure_point"
    before_current="$(cat "$current_file")"
    export FAKE_FAIL_POINT="$failure_point"
    if run_operator upgrade "$candidate_file"; then
      fail "upgrade unexpectedly succeeded at $failure_point"
    fi
    assert_equal "$current_release" "$(state_value "$runtime_state" release)" "current release not restored after $failure_point"
    assert_equal "$legacy_subnet" "$(state_value "$runtime_state" subnet)" "legacy edge not restored after $failure_point"
    assert_equal none "$(state_value "$runtime_state" ip_range)" "legacy edge range not restored after $failure_point"
    assert_equal 1 "$(state_value "$runtime_state" connected)" "connectivity not restored after $failure_point"
    assert_equal "$before_current" "$(cat "$current_file")" "current release record changed after $failure_point"
    [ ! -e "$state_dir/previous.env" ] || fail "previous release was recorded after failed $failure_point"
  done
}

case_rollback_and_rerun() {
  new_fixture rollback-rerun
  run_operator upgrade "$candidate_file"
  first_remove_count="$(grep -c '^network:remove-edge$' "$FAKE_EVENT_LOG")"
  run_operator rollback
  assert_file_release "$current_file" "$current_release"
  assert_file_release "$state_dir/previous.env" "$candidate_release"
  assert_equal "$current_release" "$(state_value "$runtime_state" release)" "rollback did not activate previous release"
  assert_equal "$fixed_subnet" "$(state_value "$runtime_state" subnet)" "rollback replaced the fixed edge"
  assert_equal "$fixed_ip_range" "$(state_value "$runtime_state" ip_range)" "rollback changed the fixed dynamic range"
  run_operator upgrade "$candidate_file"
  assert_file_release "$current_file" "$candidate_release"
  assert_equal "$candidate_release" "$(state_value "$runtime_state" release)" "repeat upgrade did not reactivate candidate"
  assert_equal "$first_remove_count" "$(grep -c '^network:remove-edge$' "$FAKE_EVENT_LOG")" "repeat upgrade rebuilt an already-fixed edge"
}

case_ipam_failure_retry() {
  new_fixture ipam-failure-retry
  before_current="$(cat "$current_file")"
  export FAKE_FAIL_POINT=target-ip-range
  if run_operator upgrade "$candidate_file"; then
    fail "upgrade unexpectedly accepted the wrong target dynamic range"
  fi
  assert_equal "$current_release" "$(state_value "$runtime_state" release)" "current release not restored after target IPAM failure"
  assert_equal "$legacy_subnet" "$(state_value "$runtime_state" subnet)" "legacy subnet not restored after target IPAM failure"
  assert_equal none "$(state_value "$runtime_state" ip_range)" "legacy range not restored after target IPAM failure"
  assert_equal 1 "$(state_value "$runtime_state" connected)" "connectivity not restored after target IPAM failure"
  assert_equal "$before_current" "$(cat "$current_file")" "current record changed after target IPAM failure"
  [ ! -e "$state_dir/previous.env" ] || fail "previous release was recorded after target IPAM failure"

  rm -f "$FAKE_FAILURE_USED"
  export FAKE_FAIL_POINT=""
  run_operator upgrade "$candidate_file"
  assert_file_release "$current_file" "$candidate_release"
  assert_equal "$candidate_release" "$(state_value "$runtime_state" release)" "IPAM retry did not activate candidate"
  assert_equal "$fixed_subnet" "$(state_value "$runtime_state" subnet)" "IPAM retry did not pin subnet"
  assert_equal "$fixed_ip_range" "$(state_value "$runtime_state" ip_range)" "IPAM retry did not pin dynamic range"
}

case_initial_start_failure_cleanup() {
  new_fixture initial-start-failure
  rm "$current_file"
  set_state_value "$runtime_state" release none
  set_state_value "$runtime_state" subnet none
  set_state_value "$runtime_state" ip_range none
  set_state_value "$runtime_state" owner none
  set_state_value "$runtime_state" connected 0
  export FAKE_FAIL_POINT=target-ip-range
  if run_operator start "$candidate_file"; then
    fail "initial start unexpectedly accepted the wrong target dynamic range"
  fi
  assert_equal none "$(state_value "$runtime_state" release)" "failed initial start left an active release"
  assert_equal none "$(state_value "$runtime_state" subnet)" "failed initial start left an unmanaged edge network"
  assert_equal none "$(state_value "$runtime_state" ip_range)" "failed initial start left an unmanaged dynamic range"
  assert_equal 0 "$(state_value "$runtime_state" connected)" "failed initial start left services connected"
  [ ! -e "$current_file" ] || fail "failed initial start recorded current release"
  grep -q 'cleanup-initial' "$FAKE_EVENT_LOG" || fail "failed initial start did not clean its runtime"
}

case_initial_start_preserves_preexisting_edge() {
  new_fixture initial-start-foreign-edge
  rm "$current_file"
  set_state_value "$runtime_state" release none
  set_state_value "$runtime_state" subnet 10.88.0.0/16
  set_state_value "$runtime_state" ip_range none
  set_state_value "$runtime_state" owner foreign
  set_state_value "$runtime_state" connected 0
  if run_operator start "$candidate_file"; then
    fail "initial start unexpectedly accepted a foreign edge network"
  fi
  assert_equal 10.88.0.0/16 "$(state_value "$runtime_state" subnet)" "failed initial start deleted a pre-existing foreign edge"
  assert_equal foreign "$(state_value "$runtime_state" owner)" "failed initial start changed foreign edge ownership"
  [ ! -e "$current_file" ] || fail "failed initial start recorded current release"
  ! grep -q '^network:remove-edge$' "$FAKE_EVENT_LOG" || fail "failed initial start removed a foreign edge"
}

case_rollback_failure_recovery() {
  new_fixture rollback-failure
  run_operator upgrade "$candidate_file"
  before_current="$(cat "$current_file")"
  before_previous="$(cat "$state_dir/previous.env")"
  rm -f "$FAKE_FAILURE_USED"
  export FAKE_FAIL_POINT=rollback-up
  if run_operator rollback; then
    fail "rollback unexpectedly succeeded with an unhealthy target"
  fi
  assert_equal "$candidate_release" "$(state_value "$runtime_state" release)" "current candidate not restored after failed rollback"
  assert_equal 1 "$(state_value "$runtime_state" connected)" "connectivity not restored after failed rollback"
  assert_equal "$before_current" "$(cat "$current_file")" "current record changed after failed rollback"
  assert_equal "$before_previous" "$(cat "$state_dir/previous.env")" "previous record changed after failed rollback"
}

run_case() {
  case "$1" in
    validate_no_side_effect) case_validate_no_side_effect ;;
    preflight_failure_no_side_effect) case_preflight_failure_no_side_effect ;;
    upgrade_success) case_upgrade_success ;;
    upgrade_failure_recovery) case_upgrade_failure_recovery ;;
    rollback_and_rerun) case_rollback_and_rerun ;;
    rollback_failure_recovery) case_rollback_failure_recovery ;;
    ipam_failure_retry) case_ipam_failure_retry ;;
    initial_start_failure_cleanup) case_initial_start_failure_cleanup ;;
    initial_start_preserves_preexisting_edge) case_initial_start_preserves_preexisting_edge ;;
    *) fail "unknown case: $1" ;;
  esac
  printf 'PASS: %s\n' "$1"
}

if [ "$selected_case" = all ]; then
  for case_name in \
    validate_no_side_effect \
    preflight_failure_no_side_effect \
    upgrade_success \
    upgrade_failure_recovery \
    rollback_and_rerun \
    rollback_failure_recovery \
    ipam_failure_retry \
    initial_start_failure_cleanup \
    initial_start_preserves_preexisting_edge
  do
    run_case "$case_name"
  done
else
  run_case "$selected_case"
fi
