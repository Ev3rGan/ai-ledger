#!/bin/sh
set -eu

case "${AI_INTEL_RESTORE_FILE:-}" in
  ""|*/*|*..*)
    printf '%s\n' '{"event":"restore-refused","reason":"invalid-backup-name"}' >&2
    exit 2
    ;;
esac

backup_path="/backups/${AI_INTEL_RESTORE_FILE}"
test -f "$backup_path"
PGPASSWORD="$(tr -d '\r\n' </run/secrets/database-password)"
export PGPASSWORD
pg_restore --list "$backup_path" >/dev/null
pg_restore --clean --if-exists --no-owner --exit-on-error --dbname="$PGDATABASE" "$backup_path"
psql --no-psqlrc --tuples-only --command='SELECT 1' >/dev/null
unset PGPASSWORD
printf '{"event":"restore-complete","target":"restore-postgres","file":"%s"}\n' "$AI_INTEL_RESTORE_FILE"
