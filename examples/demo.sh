#!/usr/bin/env bash
# End-to-end acceptance walkthrough. Requires: npm run build-free fixtures first.
#   npm run gen-fixtures && npm start
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:3000/api/v1}
META=bafybeia7j5bioii6xrfmzprrpkwsy3wm3pu6ena7ys6z37bkqgoy54gseq
IMGDIR=bafybeigqiurqgh4w6gx4u77memvpsol22mt5ljxau3jhpg4pmqr2p6bbg4
ANIM=bafybeidkcqjgfetm63zeuubvsvo3unvynsyaaa6d35uodkku6s544r7vfm

echo "== ingest all blocks =="
node -e 'const b=require("./examples/blocks/all-blocks.json");(async()=>{for(const[cid,dataBase64]of Object.entries(b)){const r=await fetch(process.env.BASE+"/blocks",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({cid,dataBase64})});if(!r.ok&&r.status!==200){console.error(await r.text());process.exit(1)}}console.log("blocks ingested")})()'

echo "== full metadata verification (all layers) =="
curl -fsS -X POST "$BASE/verify/metadata" -H 'content-type: application/json' \
  -d "{\"cid\":\"$META\"}" | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const j=JSON.parse(d);console.log("ok=",j.ok," layers=",j.report.layers.length," media=",j.report.media.map(m=>m.media?.mime));if(!j.ok)process.exit(1)})'

echo "== propose revision at chain height 100 =="
curl -fsS -X PUT "$BASE/tokens/demo-token-123/metadata" -H 'content-type: application/json' \
  -d "{\"cid\":\"$META\",\"height\":100,\"blockHash\":\"0xaaaa1111\"}" | head -c 400; echo

echo "== advance tip to 102 — proposal still unconfirmed (3 confirmations) =="
curl -fsS -X POST "$BASE/chain/tip" -H 'content-type: application/json' -d '{"height":102,"blockHash":"0xbbbb2222"}' >/dev/null

echo "== reorg at height 100 — unconfirmed update must be undone =="
curl -fsS -X POST "$BASE/chain/reorg" -H 'content-type: application/json' \
  -d '{"height":100,"oldHash":"0xaaaa1111","newHash":"0xcccc3333"}'

echo
echo "== re-propose at 100, advance to 103 — revision becomes effective =="
curl -fsS -X PUT "$BASE/tokens/demo-token-123/metadata" -H 'content-type: application/json' \
  -d "{\"cid\":\"$META\",\"height\":100,\"blockHash\":\"0xcccc3333\"}" >/dev/null
curl -fsS -X POST "$BASE/chain/tip" -H 'content-type: application/json' -d '{"height":103,"blockHash":"0xdddd4444"}' >/dev/null
curl -fsS "$BASE/tokens/demo-token-123" | node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{const j=JSON.parse(d);console.log("current=",j.current?.version,j.current?.cid.slice(0,20));if(!j.current)process.exit(1)})'
