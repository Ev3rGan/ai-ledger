#!/usr/bin/env bash
set -euo pipefail

mode="${1:?usage: m1_caddy_validate_security_harness.sh baseline|candidate CADDYFILE}"
caddyfile="${2:?usage: m1_caddy_validate_security_harness.sh baseline|candidate CADDYFILE}"
image='caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d'
test_root="$(mktemp -d "${TMPDIR:-/tmp}/m4-caddy-validate.XXXXXX")"
container_id=""

cleanup() {
  if [ -n "$container_id" ]; then
    docker rm --force "$container_id" >/dev/null 2>&1 || true
  fi
  rm -rf "$test_root"
}
trap cleanup EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

test -f "$caddyfile" || fail "Caddyfile does not exist"

baseline_security_args=(
  --network none
  --read-only
  --cap-drop ALL
)
candidate_security_args=("${baseline_security_args[@]}" --cap-add NET_BIND_SERVICE)
validation_args=(run --rm)
if [ "$mode" = candidate ]; then
  validation_args+=("${candidate_security_args[@]}")
elif [ "$mode" != baseline ]; then
  fail "mode must be baseline or candidate"
else
  validation_args+=("${baseline_security_args[@]}")
fi
validation_args+=(
  --tmpfs /config
  --tmpfs /data
  --env AI_INTEL_DOMAIN=validate.invalid
  --volume "$caddyfile:/etc/caddy/Caddyfile:ro"
  --entrypoint caddy
  "$image"
  validate --config /etc/caddy/Caddyfile
)

if [ "$mode" = baseline ]; then
  if docker "${validation_args[@]}" >"$test_root/baseline.log" 2>&1; then
    fail "baseline unexpectedly executed Caddy after dropping all capabilities"
  fi
  grep -F 'exec /usr/bin/caddy: operation not permitted' "$test_root/baseline.log" >/dev/null || {
    sed -n '1,20p' "$test_root/baseline.log" >&2
    fail "baseline did not reproduce the production exec failure"
  }
  printf '%s\n' 'RED: exec /usr/bin/caddy: operation not permitted' >&2
  exit 1
fi

docker "${validation_args[@]}"

container_id="$(docker run --detach "${candidate_security_args[@]}" \
  --entrypoint /bin/sh "$image" -c 'while :; do sleep 30; done')"

test "$(docker inspect --format '{{.HostConfig.Privileged}}' "$container_id")" = false ||
  fail "security probe is privileged"
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container_id")" = none ||
  fail "security probe joined a network"
test "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")" = true ||
  fail "security probe root filesystem is writable"
actual_cap_drop="$(docker inspect --format '{{json .HostConfig.CapDrop}}' "$container_id")"
actual_cap_add="$(docker inspect --format '{{json .HostConfig.CapAdd}}' "$container_id")"
test "$actual_cap_drop" = '["ALL"]' ||
  fail "security probe did not drop all capabilities (actual=$actual_cap_drop)"
case "$actual_cap_add" in
  '["NET_BIND_SERVICE"]'|'["CAP_NET_BIND_SERVICE"]') ;;
  *) fail "security probe gained an unexpected capability (actual=$actual_cap_add)" ;;
esac

docker exec "$container_id" /bin/sh -eu -c '
  expected=0000000000000400
  for field in CapPrm CapEff CapBnd; do
    actual="$(grep "^${field}:" /proc/1/status | cut -f2 | tr -d " ")"
    test "$actual" = "$expected"
  done
  test "$(ls -1 /sys/class/net)" = lo
  ! touch /m4-caddy-rootfs-write-probe 2>/dev/null
'

printf '%s\n' \
  'GREEN: caddy validate succeeded' \
  'SECURITY: privileged=false network=none rootfs=read-only cap-drop=ALL cap-add=NET_BIND_SERVICE interfaces=lo'
