#!/usr/bin/env bash
set -euo pipefail

mode="${1:?usage: m1_edge_ipam_harness.sh baseline|candidate [NORMALIZED_COMPOSE_YAML]}"
normalized_compose="${2:-}"
image='caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
test_subnet='10.255.254.0/24'
test_dynamic_range='10.255.254.128/25'
test_static_ip='10.255.254.2'
container_security_args=(--read-only --cap-drop ALL --security-opt no-new-privileges)
network_name="m4-ipam-isolated-${RANDOM}-$$"
dynamic_id=""
static_id=""
network_created=0
test_root="$(mktemp -d "${TMPDIR:-/tmp}/m4-edge-ipam.XXXXXX")"

cleanup_round() {
  if [ -n "$static_id" ]; then docker rm --force "$static_id" >/dev/null 2>&1 || true; fi
  if [ -n "$dynamic_id" ]; then docker rm --force "$dynamic_id" >/dev/null 2>&1 || true; fi
  if [ "$network_created" = 1 ]; then docker network rm "$network_name" >/dev/null 2>&1 || true; fi
  static_id=""
  dynamic_id=""
  network_created=0
}

cleanup() {
  cleanup_round
  rm -rf "$test_root"
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

docker image inspect "$image" >/dev/null
if docker network ls --quiet | xargs -r docker network inspect \
  --format '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' | grep -Fx "$test_subnet" >/dev/null; then
  fail "isolated test subnet is already in use"
fi

start_dynamic() {
  dynamic_id="$(docker run --detach --network "$network_name" \
    "${container_security_args[@]}" \
    --entrypoint /bin/sh "$image" -c 'while :; do sleep 30; done')"
}

container_ip() {
  docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"
}

network_contract() {
  docker network inspect \
    --format '{{range .IPAM.Config}}{{printf "%s|%s\n" .Subnet .IPRange}}{{end}}' \
    "$network_name" |
    awk -F'|' 'NF {if ($2 == "invalid Prefix") $2 = ""; print $1 "|" $2}'
}

if [ "$mode" = baseline ]; then
  docker network create --internal --subnet "$test_subnet" "$network_name" >/dev/null
  network_created=1
  test "$(network_contract)" = "${test_subnet}|" ||
    fail "baseline network did not expose an empty dynamic range"
  start_dynamic
  dynamic_ip="$(container_ip "$dynamic_id")"
  test "$dynamic_ip" = "$test_static_ip" ||
    fail "baseline dynamic container did not take the first .2 address (actual=$dynamic_ip)"
  set +e
  docker run --rm --network "$network_name" --ip "$test_static_ip" \
    "${container_security_args[@]}" \
    --entrypoint /bin/sh "$image" -c true >"$test_root/static.log" 2>&1
  result=$?
  set -e
  test "$result" -ne 0 || fail "baseline static .2 unexpectedly succeeded"
  grep -F 'Address already in use' "$test_root/static.log" >/dev/null || {
    sed -n '1,20p' "$test_root/static.log" >&2
    fail "baseline did not reproduce the production address conflict"
  }
  printf 'RED: dynamic=%s static=%s conflict=Address already in use\n' \
    "$dynamic_ip" "$test_static_ip" >&2
  exit 1
fi

test "$mode" = candidate || fail "mode must be baseline or candidate"
test -f "$normalized_compose" || fail "candidate requires normalized Compose YAML"
normalized_value() {
  key="$1"
  awk -v key="${key}:" '
    $1 == key {print $2}
    $1 == "-" && $2 == key {print $3}
  ' "$normalized_compose"
}

production_contract="$(normalized_value subnet)|$(normalized_value ip_range)|$(normalized_value ipv4_address)"
test "$production_contract" = '172.31.255.0/24|172.31.255.128/25|172.31.255.2' ||
  fail "normalized Compose does not reserve the exact Caddy boundary (actual=$production_contract)"

for attempt in 1 2 3; do
  docker network create --internal --subnet "$test_subnet" \
    --ip-range "$test_dynamic_range" "$network_name" >/dev/null
  network_created=1
  candidate_contract="$(network_contract)"
  test "$candidate_contract" = "${test_subnet}|${test_dynamic_range}" ||
    fail "candidate network did not expose the requested dynamic range (actual=$candidate_contract)"
  start_dynamic
  dynamic_ip="$(container_ip "$dynamic_id")"
  case "$dynamic_ip" in
    10.255.254.12[8-9]|10.255.254.1[3-9][0-9]|10.255.254.2[0-4][0-9]|10.255.254.25[0-4]) ;;
    *) fail "dynamic address escaped its range (actual=$dynamic_ip)" ;;
  esac
  test "$dynamic_ip" != "$test_static_ip" || fail "dynamic address took the static address"
  static_id="$(docker run --detach --network "$network_name" --ip "$test_static_ip" \
    "${container_security_args[@]}" \
    --entrypoint /bin/sh "$image" -c 'while :; do sleep 30; done')"
  test "$(container_ip "$static_id")" = "$test_static_ip"
  printf 'ATTEMPT_%s=dynamic:%s,static:%s\n' "$attempt" "$dynamic_ip" "$test_static_ip"
  cleanup_round
done

recovery_project="m4-ipam-recovery-${RANDOM}-$$"
docker network create --driver bridge --subnet "$test_subnet" \
  --label "com.docker.compose.project=$recovery_project" \
  --label com.docker.compose.network=edge "$network_name" >/dev/null
network_created=1
recovery_contract="$(network_contract)"
test "$recovery_contract" = "${test_subnet}|" ||
  fail "explicit recovery network did not preserve the legacy contract (actual=$recovery_contract)"
cat >"$test_root/recovery.compose.yml" <<EOF
services:
  probe:
    image: $image
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges
    entrypoint:
      - /bin/true
    networks:
      - edge
networks:
  edge:
    name: $network_name
EOF
docker compose --project-name "$recovery_project" \
  --file "$test_root/recovery.compose.yml" run --rm --no-deps probe
cleanup_round
printf '%s\n' 'GREEN: explicit legacy network is accepted by an unpinned Compose edge'
printf '%s\n' 'GREEN: normalized Compose dynamic range preserves the static Caddy address'
