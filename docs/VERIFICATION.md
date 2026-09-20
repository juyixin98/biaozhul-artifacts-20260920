# Verification record

Everything below was executed on Linux with Go 1.22, Google Chrome 138
(headless), and MySQL 8.4 (Docker). Timestamps/details were produced by the
runs on 2026-09-20.

## Automated tests

```text
go test -count=1 -timeout 400s ./...
ok  sitevitals/internal/api       1.021s
ok  sitevitals/internal/browser  28.162s   real Chromium, incl. killed-process recovery
ok  sitevitals/internal/budget    0.006s
ok  sitevitals/internal/compare   0.005s
ok  sitevitals/internal/demo      0.010s
ok  sitevitals/internal/report    0.004s
ok  sitevitals/internal/store     2.446s   real MySQL 8.4 (InnoDB / SKIP LOCKED)
ok  sitevitals/internal/whitelist 0.006s
ok  sitevitals/internal/worker     3.277s   MySQL + real Chromium end-to-end
```

Coverage by requested guarantee:

- **Lease competition** — `TestClaim_Concurrent_ExactlyOneWinner`: 12 workers
  race one task through `FOR UPDATE SKIP LOCKED`; exactly one wins, one run row.
- **Crash recovery** — `TestClaim_CrashRecovery_LeaseExpiry`,
  `TestClaim_ReaperThenReclaim`: expired lease is reclaimed, stale run marked
  `abandoned (LEASE_EXPIRED)`, token rotated, attempts incremented.
- **Late executor** — `TestLateCommit_RejectedByToken`: success and failure
  commits under the old token return `ErrLeaseLost`; current result and the
  single report are untouched; a repeated success cannot duplicate the report.
- **Redirect limits** — `TestCollect_RedirectLoop` (`REDIRECT_LIMIT`),
  `TestCollect_RedirectChain` (two allowed hops),
  `TestCollect_RedirectToNonHTTP` and `TestCollect_RedirectOffWhitelist`
  (`POLICY_BLOCKED`).
- **Metric missing** — `TestHarvestStatuses_UnsupportedAreExplicit`,
  `TestHarvestStatuses_SupportedButNoValue`,
  `TestBuild_MissingMetricsShownAsNA_NotZero`,
  `TestCheck_ThresholdsAndMissingMetrics`: NULL + explicit status, never 0;
  budget checks skip missing metrics.
- **Timeout / browser exit / partial resources** —
  `TestCollect_NavigationTimeout` (3 s NAV_TIMEOUT returns in ~3 s),
  `TestCollect_BrowserCrash_KillProcess` (Chrome killed mid-flight returns
  `BROWSER_CRASH`, then Restart and a subsequent collection succeed),
  `TestCollect_PartialResourceFailure` (404/500 subresources recorded, task
  succeeds).
- **Queue isolation** — `TestFailedTask_DoesNotBlockQueue`,
  `TestWorker_EndToEnd_FailureClassifiedThenDead` (dead-port target →
  `NAVIGATION_FAILED`, browser alive, stats uncontaminated),
  `TestStats_FailedRunsExcluded`.
- **Subresource policy** — `TestCollect_StrictSubresources_BlocksOffList`
  (strict mode blocks the off-list image; document still loads).
- **Viewport metrics** — real collections for mobile / tablet / desktop.

## Live run (real binary against real MySQL + real Chromium)

`bin/sitevitals serve` with `ENABLE_DEMO=true`, plus the same image built by
`Dockerfile` (`sitevitals-a:latest`, 1.3 GB), both against MySQL 8.4.

Observed outcomes (task ids from the run):

| Scenario | Result |
|---|---|
| `/normal` × mobile, tablet, desktop (tasks 1–3) | succeeded; nav/FCP/LCP measured, status `ok` |
| `/longtask` (4) | succeeded; 1 long task, 480 ms; window start/end recorded |
| `/cls` (5) | succeeded; CLS 0.0117 |
| second desktop `/normal` (6) | succeeded; auto comparison vs task 3 stored; FCP budget alert (actual vs threshold 1 ms) |
| `/badredirect-file` (7) | dead, `POLICY_BLOCKED` |
| `/redirect/loop/a` (8) | dead, `REDIRECT_LIMIT` |
| `/partial` (9) | succeeded; 2 resources 4xx/5xx listed, task not failed |
| `http://127.0.0.1:1/x` whitelisted dead port (10) | dead, `NAVIGATION_FAILED` |
| `/hang` (11) | dead, `NAVIGATION_TIMEOUT` (load never fired) |
| off-list subresource record mode (13) | succeeded; violation `not_whitelisted/subresource blocked=false` + warning |
| containerized Chromium, `/longtask` mobile (15) | succeeded; owner = container worker (PID 1) |
| enqueue validation | `file:` → 403, off-list http → 403, bad viewport → 400 |
| stats | 7 succeeded only; 3 dead excluded from averages |

The markdown report (`GET /api/tasks/:id/report`) includes the metric table
with statuses, the long-task window statement, budget alert rows, redirect
chain, violations, and the full resource waterfall (document + image with
phase timings).

## Not executed / limitations

- `docker compose up` itself was not run end-to-end (the environment's Docker
  daemon requires sudo and host ports are shared with unrelated containers);
  instead the image it builds was built (`docker build`, success, 1.3 GB) and
  run directly against the same MySQL with host networking, exercising the
  same entrypoint (`/app/sitevitals serve`) and container Chromium.
- FCP/LCP on a cross-origin page without `Timing-Allow-Origin` were verified
  through unit-level status mapping; the demo runs everything on one origin.
- No multi-minute soak of long-lived worker fleets; leases were tested with
  short (30–50 ms) durations deterministically.
- Metrics not exposed by the browser at all cannot be forced on current Chrome;
  that status path is covered by decoding synthetic harvest payloads rather
  than a live page.
