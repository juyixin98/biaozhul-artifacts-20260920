#!/usr/bin/env bash
set -euo pipefail

# ForensicCore container entrypoint.
#   serve (default)  - wait for MySQL, optionally seed samples, run the API
#   seed             - generate sample images into SAMPLES_DIR and exit
#   <other>          - exec the given command

ACTION="${1:-serve}"

if [ "$ACTION" = "seed" ]; then
    exec /app/genseed "${SAMPLES_DIR:-/data/samples}"
fi

if [ "$ACTION" != "serve" ]; then
    exec "$@"
fi

# Generate bundled sample images when the samples directory is empty so the
# stack is usable out of the box. Real evidence mounts simply skip this step.
SAMPLES_DIR="${SAMPLES_DIR:-/data/samples}"
if [ ! -f "$SAMPLES_DIR/sample_1mb.dd" ]; then
    echo "[entrypoint] generating bundled sample images in $SAMPLES_DIR"
    /app/genseed "$SAMPLES_DIR" || true
fi

# config.Load resolves every whitelist root with EvalSymlinks at startup, so
# each configured root must exist. Create any missing root on the writable
# data volume; read-only bind mounts are expected to already exist.
IFS=':' read -r -a _roots <<< "${WHITELIST_DIRS:-$SAMPLES_DIR}"
for _d in "${_roots[@]}"; do
    [ -n "$_d" ] && mkdir -p "$_d" 2>/dev/null || true
done

# Wait for MySQL to accept TCP connections (sqlite needs no waiting).
if [ "${DB_DRIVER:-mysql}" = "mysql" ]; then
    host="${MYSQL_HOST:-mysql}"
    port="${MYSQL_PORT:-3306}"
    echo "[entrypoint] waiting for MySQL at ${host}:${port} ..."
    for _ in $(seq 1 90); do
        if (exec 3<>"/dev/tcp/${host}/${port}") 2>/dev/null; then
            echo "[entrypoint] MySQL is reachable"
            exec 3>&- 3<&- 2>/dev/null || true
            break
        fi
        sleep 1
    done
fi

exec /app/server
