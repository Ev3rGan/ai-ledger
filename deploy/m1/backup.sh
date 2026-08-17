#!/bin/sh
set -eu

backup_once() {
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  partial="/backups/ai-ledger-${timestamp}.dump.partial"
  completed="/backups/ai-ledger-${timestamp}.dump"
  PGPASSWORD="$(tr -d '\r\n' </run/secrets/database-password)"
  export PGPASSWORD
  pg_dump --format=custom --no-owner --file="$partial"
  pg_restore --list "$partial" >/dev/null
  chmod 600 "$partial"
  mv "$partial" "$completed"
  offsite_partial="/offsite-backups/$(basename "$completed").partial"
  offsite_completed="/offsite-backups/$(basename "$completed")"
  cp "$completed" "$offsite_partial"
  pg_restore --list "$offsite_partial" >/dev/null
  chmod 600 "$offsite_partial"
  mv "$offsite_partial" "$offsite_completed"
  unset PGPASSWORD
  printf '{"event":"backup-complete","file":"%s","offsite_copy":"verified"}\n' "$(basename "$completed")"
}

retention_days="${AI_INTEL_BACKUP_RETENTION_DAYS:-14}"
interval_seconds="${AI_INTEL_BACKUP_INTERVAL_SECONDS:-86400}"

while :; do
  backup_once
  find /backups -type f -name 'ai-ledger-*.dump' -mtime "+${retention_days}" -delete
  find /offsite-backups -type f -name 'ai-ledger-*.dump' -mtime "+${retention_days}" -delete
  if [ "${AI_INTEL_BACKUP_ONCE:-0}" = "1" ]; then
    exit 0
  fi
  sleep "$interval_seconds"
done
