#!/usr/bin/env bash
# Service invocation examples. Assumes the server is running:
#   npm run dev        (http://127.0.0.1:3000 by default)
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:3000}"

echo "== 1. health =="
curl -s "$BASE/health"; echo

echo "== 2. valid upload: one field + one allowed text file =="
printf 'the file contents\n' > /tmp/note.txt
curl -s -i -X POST "$BASE/uploads" \
  -F "title=hello world" \
  -F "file=@/tmp/note.txt;type=text/plain" | sed -n '1p;/^\r\?$/q'
curl -s -X POST "$BASE/uploads" \
  -F "title=hello world" \
  -F "file=@/tmp/note.txt;type=text/plain"; echo

echo "== 3. rejected: disallowed extension (.sh) -> 400 FILENAME_REJECTED =="
printf 'echo hi\n' > /tmp/evil.sh
curl -s -o /dev/null -w "status=%{http_code}\n" -X POST "$BASE/uploads" \
  -F "file=@/tmp/evil.txt;filename=evil.sh;type=text/plain" || true
curl -s -X POST "$BASE/uploads" \
  -F "file=@/tmp/evil.sh;type=text/plain"; echo

echo "== 4. rejected: missing closing boundary -> 400 MISSING_TERMINATOR =="
# Hand-built body sent with the correct content type but WITHOUT --b-- terminator.
printf -- '--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nx\r\n' \
  | curl -s -X POST "$BASE/uploads" \
      -H "Content-Type: multipart/form-data; boundary=b" --data-binary @-; echo

echo "== 5. list recent submissions =="
curl -s "$BASE/submissions"; echo

echo "== 6. diagnostics: recent runs (error classes are recorded per run) =="
curl -s "$BASE/diagnostics/runs" | head -c 600; echo
