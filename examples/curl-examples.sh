#!/usr/bin/env bash
# Service-call examples for the multipart/form-data receiver.
# Assumes the server is running: npm run dev  (default http://127.0.0.1:3000)
set -u
BASE="${BASE:-http://127.0.0.1:3000}"

echo "## 1. Valid upload: one UTF-8 field + one PNG file"
curl -sS -X POST "$BASE/upload" \
  -F 'title=hello 世界' \
  -F 'pic=@fixtures/sample/pixel.png;type=image/png'
echo; echo

echo "## 2. RFC 5987 filename* (non-ASCII name) — built with a quoted filename"
curl -sS -X POST "$BASE/upload" \
  -F 'doc=@fixtures/sample/notes.txt;filename=文件.txt;type=text/plain'
echo; echo

echo "## 3. Wrong media type -> 415 UNSUPPORTED_MEDIA_TYPE"
curl -sS -o /dev/null -w 'status=%{http_code}\n' -X POST "$BASE/upload" \
  -H 'Content-Type: application/json' --data '{}'
echo

echo "## 4. Malicious traversal filename -> 400 PATH_TRAVERSAL_FILENAME"
printf -- '--B\r\nContent-Disposition: form-data; name="f"; filename="../../evil.sh"\r\nContent-Type: text/plain\r\n\r\nx\r\n--B--\r\n' \
  | curl -sS -w '\nstatus=%{http_code}\n' -X POST "$BASE/upload" \
      -H 'Content-Type: multipart/form-data; boundary=B' --data-binary @-
echo

echo "## 5. Missing terminating boundary -> 400 MISSING_TERMINATING_BOUNDARY"
printf -- '--B\r\nContent-Disposition: form-data; name="f"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\nabc\r\n--B' \
  | curl -sS -w '\nstatus=%{http_code}\n' -X POST "$BASE/upload" \
      -H 'Content-Type: multipart/form-data; boundary=B' --data-binary @-
echo

echo "## 6. Oversized part -> 413 PART_SIZE_EXCEEDED (field cap default 64 KiB)"
python3 -c "import sys;sys.stdout.buffer.write(b'--B\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n'+b'a'*100000+b'\r\n--B--\r\n')" \
  | curl -sS -w '\nstatus=%{http_code}\n' -X POST "$BASE/upload" \
      -H 'Content-Type: multipart/form-data; boundary=B' --data-binary @-
echo

echo "## 7. Diagnostics: effective limits and replayable run log"
curl -sS "$BASE/diagnostics/limits"; echo
curl -sS "$BASE/diagnostics/runs?tail=5"; echo
