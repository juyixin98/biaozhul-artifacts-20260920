#!/usr/bin/env bash
# Generate a demo dataset dominated by one HOT key, sized so that a single
# key group is far larger than a small memory budget while the result stays
# manageable (default: 1000 hot rows/side -> 1e6 hot-key pairs).
#
# Writes $JOIN_DATA_DIR/gen/{left,right}.jsonl (default ./data/gen).
#
# Params: ./gen-large.sh [rowsPerSide=20000] [hotPercent=5] [payloadWidth=24]
set -euo pipefail
cd "$(dirname "$0")/.."

N="${1:-20000}"
HOT="${2:-5}"          # percent of rows on the hot key k=1
WIDTH="${3:-24}"
COLD_KEYS=2000
OUT="${JOIN_DATA_DIR:-./data}/gen"
mkdir -p "$OUT"

gen() {
  local file="$1" side="$2"
  awk -v n="$N" -v hot="$HOT" -v w="$WIDTH" -v cold="$COLD_KEYS" -v side="$side" '
    function pad(x,   s,i) {
      s=""; for (i=0;i<w;i++) s=s substr("abcdefghij", (x+i)%10+1, 1); return s;
    }
    BEGIN {
      srand(length(side)*7919 + n);
      for (i=0;i<n;i++){
        r = int(rand()*100);
        if (r < hot) k=1; else k = 2 + int(rand()*cold);
        if (r == hot+1) # ~1% of rows: key column absent -> treated as NULL
          printf "{\"id\":%d,\"side\":\"%s\",\"p\":\"%s\"}\n", i, side, pad(i);
        else if (r == hot+2) # ~1% of rows: explicit null
          printf "{\"id\":%d,\"k\":null,\"side\":\"%s\",\"p\":\"%s\"}\n", i, side, pad(i);
        else
          printf "{\"id\":%d,\"k\":%d,\"side\":\"%s\",\"p\":\"%s\"}\n", i, k, side, pad(i);
      }
    }' > "$file"
}

gen "$OUT/left.jsonl" L
gen "$OUT/right.jsonl" R
echo "wrote $OUT/left.jsonl and $OUT/right.jsonl"
echo "  rows/side=$N  hotKey=1 (~${HOT}%)  payloadWidth=$WIDTH  coldKeys=$COLD_KEYS"
wc -l -c "$OUT/left.jsonl" "$OUT/right.jsonl"
