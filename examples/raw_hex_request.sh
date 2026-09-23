#!/usr/bin/env bash
# Send the 94-byte "valid" synthetic ClientHello as raw hex over TCP,
# using only bash builtins (/dev/tcp) + xxd. Prints the JSON observation.
#
# Usage: ./raw_hex_request.sh [HOST] [PORT]
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8443}"

HEX="1603010059010000550303424242424242424242424242424242424242424242424242424242424242424200000613011302c02f0100002600000010000e00000b6578616d706c652e636f6d0010000e000c02683208687474702f312e31"

exec 3<>"/dev/tcp/${HOST}/${PORT}"
printf '%s' "$HEX" | xxd -r -p >&3
# half-close the write side is not portable on /dev/tcp; the server's idle
# timeout will finalize the observation instead.
cat <&3
exec 3<&-
