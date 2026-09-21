#!/usr/bin/env bash
# Manage the disposable MySQL container used by the integration tests.
# Usage: scripts/test-mysql.sh [up|down|status]
set -euo pipefail

NAME=targetcraft-test-mysql
PORT=13306
ROOT_PASS=rootpass

cmd="${1:-up}"

case "$cmd" in
  up)
    if sudo docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "$NAME already running on port $PORT"
    elif sudo docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
      sudo docker start "$NAME" >/dev/null
      echo "started existing $NAME"
    else
      sudo docker run -d --name "$NAME" \
        -p "$PORT":3306 \
        -e MYSQL_ROOT_PASSWORD="$ROOT_PASS" \
        mysql:8.0 >/dev/null
      echo "created $NAME on port $PORT"
    fi
    echo -n "waiting for mysql"
    for _ in $(seq 1 60); do
      if sudo docker exec "$NAME" mysqladmin ping -h 127.0.0.1 -p"$ROOT_PASS" >/dev/null 2>&1; then
        echo " ready"
        exit 0
      fi
      echo -n "."
      sleep 1
    done
    echo " TIMEOUT" >&2
    exit 1
    ;;
  down)
    sudo docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "removed $NAME"
    ;;
  status)
    sudo docker ps -a --filter "name=$NAME"
    ;;
  *)
    echo "usage: $0 [up|down|status]" >&2
    exit 2
    ;;
esac
