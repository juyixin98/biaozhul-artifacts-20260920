#!/usr/bin/env bash
# End-to-end acceptance: tests + every scenario + SQLite HMAC verify + tamper.
# Runs fully without ROS or hardware. Exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== 1/5 unit/integration tests =="
python3 -m pytest tests/ -q

echo "== 2/5 generate HMAC key (real os.urandom key material) =="
mkdir -p build
python3 -m time_alignment.cli genkey --out build/hmac.key

echo "== 3/5 replay every scenario into SQLite + JSONL evidence =="
fail=0
for f in examples/*.json; do
  n=$(basename "$f" .json)
  echo "--- $n"
  python3 -m time_alignment.cli replay "$f" \
      --db "build/$n.db" --key build/hmac.key --export "build/$n.jsonl"
done

echo "== 4/5 verify HMAC chains (must all be VALID) =="
for db in build/*.db; do
  case "$(basename "$db")" in tampered*) continue;; esac
  python3 -m time_alignment.cli verify "$db" --key build/hmac.key
done

echo "== 5/5 tamper resistance (verification MUST fail) =="
python3 - <<'PY'
import json, shutil, sqlite3
shutil.copy("build/burst.db", "build/tampered.db")
c = sqlite3.connect("build/tampered.db")
r = c.execute("SELECT seq,payload FROM records "
              "WHERE payload LIKE '%expired_unpaired%' LIMIT 1").fetchone()
p = json.loads(r[1]); p["status"] = "matched"
c.execute("UPDATE records SET payload=? WHERE seq=?",
          (json.dumps(p, sort_keys=True, separators=(",", ":")), r[0]))
c.commit(); c.close()
print("tampered build/tampered.db seq", r[0])
PY
if python3 -m time_alignment.cli verify build/tampered.db --key build/hmac.key; then
  echo "SECURITY FAILURE: tampered DB verified as valid" >&2
  exit 1
else
  echo "OK: tamper detected as expected"
fi

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
