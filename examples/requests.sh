#!/usr/bin/env bash
# Request samples for the HTTP range service.
#
# Usage:
#   go run ./cmd/server -addr 127.0.0.1:8080
#   ./examples/requests.sh            # defaults to http://127.0.0.1:8080
#   BASE=http://127.0.0.1:9000 ./examples/requests.sh
#
# Every command prints status line and the range-relevant response headers.
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
ART="$BASE/artifacts/hello.txt"   # 18-byte text artifact
SAMPLE="$BASE/artifacts/sample.bin"

hdr() { curl -s -D - -o /dev/null -w "> status=%{http_code} bytes=%{size_download}\n" "$@"; }

echo "## 0. index of artifacts"
curl -s "$BASE/" ; echo

echo "## 1. full representation (200); note ETag + Accept-Ranges"
hdr "$ART"

echo "## 2. prefix range                 Range: bytes=0-4      -> 206 bytes 0-4/18"
hdr -H "Range: bytes=0-4" "$ART"

echo "## 3. open-ended range             Range: bytes=6-      -> 206 bytes 6-17/18"
hdr -H "Range: bytes=6-" "$ART"

echo "## 4. suffix range                 Range: bytes=-6      -> 206 bytes 12-17/18"
hdr -H "Range: bytes=-6" "$ART"

echo "## 5. suffix larger than resource  Range: bytes=-9999   -> 206 bytes 0-17/18 (clamped)"
hdr -H "Range: bytes=-9999" "$ART"

echo "## 6. last-pos beyond end clamped  Range: bytes=10-9999 -> 206 bytes 10-17/18"
hdr -H "Range: bytes=10-9999" "$ART"

echo "## 7. unsatisfiable range          Range: bytes=100-    -> 416, Content-Range: bytes */18"
hdr -H "Range: bytes=100-" "$ART"

echo "## 8. zero-length artifact         Range: bytes=0-0 on empty.bin -> 416 bytes */0"
hdr -H "Range: bytes=0-0" "$BASE/artifacts/empty.bin"

echo "## 9. multiple ranges              -> 206 multipart/byteranges"
curl -s -H "Range: bytes=0-4,12-17" "$ART"; echo

echo "## 10. overlapping ranges          0-10 and 6-14 are both returned"
curl -s -H "Range: bytes=0-10,6-14" "$ART" | grep -c "Content-Range:"

echo "## 11. malformed Range is ignored  bytes=abc -> 200 full"
hdr -H "Range: bytes=abc" "$ART"

echo "## 12. unsupported range unit      items=0-9 -> 200 full"
hdr -H "Range: items=0-9" "$ART"

ETAG="$(curl -s -D - -o /dev/null "$ART" | awk -F': ' 'tolower($1)=="etag"{gsub("\r","",$2);print $2}')"
echo "## 13. If-Range matching ETag      ($ETAG) -> 206"
hdr -H "Range: bytes=0-4" -H "If-Range: $ETAG" "$ART"

echo "## 14. If-Range mismatched ETag    -> 200 full representation"
hdr -H "Range: bytes=0-4" -H 'If-Range: "00000000000000000000000000000000"' "$ART"

echo "## 15. If-Range fresh HTTP-date    -> 206"
hdr -H "Range: bytes=0-4" -H "If-Range: Wed, 01 Jan 2031 00:00:00 GMT" "$ART"

echo "## 16. If-Range stale HTTP-date    -> 200 full"
hdr -H "Range: bytes=0-4" -H "If-Range: Wed, 01 Jan 2020 00:00:00 GMT" "$ART"

echo "## 17. range set over member limit (6 > 5) -> 200 full (Range ignored)"
hdr -H "Range: bytes=0-0,1-1,2-2,3-3,4-4,5-5" "$ART"

echo "## 18. conditional GET             If-None-Match current ETag -> 304"
hdr -H "If-None-Match: $ETAG" "$ART"

echo "## 19. HEAD with Range -> 206 headers, no body"
curl -s -I -H "Range: bytes=0-4" "$ART" | grep -iE "HTTP/|Content-Range|Content-Length"
