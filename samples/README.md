# Request samples

Raw HTTP/1.1 request bytes. Each file contains **exact wire bytes**
(`CRLF` line endings, no trailing text) and can be piped straight at
the service:

```bash
# one file
nc 127.0.0.1 8080 < samples/01_get_minimal.http

# all files, bulk or one byte per TCP segment
scripts/send_samples.sh 127.0.0.1:8080
scripts/send_samples.sh 127.0.0.1:8080 --bytewise
```

| File | Expected result |
|---|---|
| `01_get_minimal.http` | `200`, no body |
| `02_fixed_length.http` | `200`, 11-byte body framed by `Content-Length` |
| `03_chunked_trailer.http` | `200`, chunked body decoded (`Wikipedia`), 2 trailers |
| `04_pipeline.http` | three `200` responses on one connection |
| `05_reject_cl_te.http` | `400 te-with-content-length` (CL then TE) |
| `06_reject_te_cl.http` | `400 te-with-content-length` (TE then CL) |
| `07_reject_duplicate_cl.http` | `400 duplicate-content-length` |
| `08_reject_obs_fold.http` | `400 obsolete-line-folding` |
| `09_reject_bad_chunk.http` | `400 chunk-terminator` (missing CRLF after chunk data) |
| `10_reject_ambiguous_ws.http` | `400 invalid-header-name` (space before colon) |
| `11_reject_bare_lf.http` | `400 bad-line-ending` |
| `12_truncated.http` | `400 incomplete` (server sees EOF mid-body) |

The reject samples deliberately include the bytes a naive parser would
treat as a *second* pipelined request (`GET /admin …`); this server
never frames them because the head is rejected first and the
connection is closed after the single 4xx.
