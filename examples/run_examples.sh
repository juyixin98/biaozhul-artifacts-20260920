#!/usr/bin/env bash
# Request samples for the offline HTTP framing service.
#
# Each sample builds a raw HTTP/1.1 request byte stream (with exact CRLF
# endings via printf) and posts it verbatim to /parse or /verify.
# The carrier request is just transport: the server analyzes the body
# bytes offline and never forwards them anywhere.
set -u

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
BASE="http://${HOST}:${PORT}"

post() { # <endpoint> <filename inside tmp dir>
  curl -sS -X POST --data-binary @"$TMP/$2" -H 'Content-Type: application/octet-stream' \
    "${BASE}$1" | python3 -m json.tool 2>/dev/null \
    || curl -sS -X POST --data-binary @"$TMP/$2" -H 'Content-Type: application/octet-stream' "${BASE}$1"
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Exact CRLF raw bytes are generated with printf (\r\n), not heredocs.
write() { printf '%b' "$2" > "$TMP/$1"; }

echo "=== 1. plain Content-Length request -> ok, body parsed"
write cl.bin 'POST /a HTTP/1.1\r\nHost: example.com\r\nContent-Length: 11\r\n\r\nhello world'
post /parse cl.bin

echo; echo "=== 2. chunked request with trailers -> decoded body Wikipedia"
write chunked.bin 'POST /c HTTP/1.1\r\nHost: example.com\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nWiki\r\n5\r\npedia\r\n0\r\nETag: "abc"\r\n\r\n'
post /parse chunked.bin

echo; echo "=== 3. two pipelined requests in one connection -> request_count 2"
write pipe.bin 'GET /1 HTTP/1.1\r\nHost: a\r\n\r\nPOST /2 HTTP/1.1\r\nHost: b\r\nContent-Length: 2\r\n\r\nhi'
post /parse pipe.bin

echo; echo "=== 4. CL + chunked together -> content_length_and_chunked, offset at TE line"
write ambiguous.bin 'POST /x HTTP/1.1\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n'
post /parse ambiguous.bin

echo; echo "=== 5. conflicting Content-Length values -> content_length_conflict"
write conflict.bin 'POST /x HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\n'
post /parse conflict.bin

echo; echo "=== 6. folded header -> folded_header with exact offset"
write folded.bin 'GET / HTTP/1.1\r\nX-A: v\r\n X-B: y\r\n\r\n'
post /parse folded.bin

echo; echo "=== 7. truncated chunk -> chunk_data_truncated offset = total bytes"
write trunc.bin 'POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nabc'
post /parse trunc.bin

echo; echo "=== 8. bare LF -> bare_lf"
write barelf.bin 'GET / HTTP/1.1\nHost: x\r\n\r\n'
post /parse barelf.bin

echo; echo "=== 9. verify: exhaustive byte-split check of sample 2 -> ok, N splits"
post /verify chunked.bin
