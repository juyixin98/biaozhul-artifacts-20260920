#!/usr/bin/env bash
# Request samples for the histogram-merge backend.
# Usage: ./examples/requests.sh [host:port]   (default 127.0.0.1:28081)
set -euo pipefail
BASE="http://${1:-127.0.0.1:28081}"

echo '== seed synthetic histograms (deterministic seed) =='
curl -s -XPOST "$BASE/v1/synth?seed=42" ; echo

echo '== list stored histograms =='
curl -s "$BASE/v1/histograms" ; echo

echo '== fetch one histogram =='
curl -s "$BASE/v1/histograms/svc-a.http_duration" ; echo

echo '== merge two histograms with identical boundaries =='
curl -s -XPOST "$BASE/v1/merge" \
  -d '{"names":["svc-a.http_duration","svc-b.http_duration"]}' ; echo

echo '== merge three histograms; svc-c has coarser boundaries,'
echo '   so all sides shrink to the common boundary set =='
curl -s -XPOST "$BASE/v1/merge" \
  -d '{"names":["svc-a.http_duration","svc-b.http_duration","svc-c.http_duration"]}' ; echo

echo '== p95 quantile: interval + interpolated estimate =='
curl -s "$BASE/v1/quantile?name=svc-a.http_duration&q=0.95" ; echo

echo '== histogram with observations in the +Inf bucket =='
curl -s -XPOST "$BASE/v1/histograms" \
  -d '{"name":"with-inf","bounds":[1,2,"+Inf"],"counts":[8,9,10]}' ; echo
curl -s "$BASE/v1/quantile?name=with-inf&q=0.95" ; echo

echo '== empty histogram: stored fine, quantile rejected (422) =='
curl -s -XPOST "$BASE/v1/histograms" \
  -d '{"name":"empty","bounds":[1,"+Inf"],"counts":[0,0]}' ; echo
curl -s -w '\nHTTP %{http_code}\n' "$BASE/v1/quantile?name=empty&q=0.5"

echo '== invalid cumulative counts rejected (400) =='
curl -s -w '\nHTTP %{http_code}\n' -XPOST "$BASE/v1/histograms" \
  -d '{"name":"bad","bounds":[1,"+Inf"],"counts":[9,5]}'

echo '== missing +Inf last bound rejected (400) =='
curl -s -w '\nHTTP %{http_code}\n' -XPOST "$BASE/v1/histograms" \
  -d '{"name":"bad2","bounds":[1,2],"counts":[1,2]}'
