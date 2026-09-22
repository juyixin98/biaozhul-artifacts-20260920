#!/bin/sh
# Entrypoint runs as root (needed because a fresh named volume is root-owned),
# fixes /data ownership once, then drops privileges to the unprivileged vfx
# user. On docker run --user or a bind mount this is still safe.
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/assets /data/outputs
    chown -R 10001:10001 /data
    exec su-exec vfx /app/vfxqueue "$@"
fi

exec /app/vfxqueue "$@"
