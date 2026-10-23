# Phase 2A expired-window investigation for Phase 2B

Read-only inspection of preserved WSL ledger on 2026-10-23. No expiry check weakened.

## Eight expired rejections

| Decision slot | Next observed slot | Creation-to-rejection ms |
|---|---|---:|
| 6034202:6 | 8 | 2953.640 |
| 6034202:15 | 17 | 2978.686 |
| 6034202:21 | 24 | 5908.962 |
| 6034202:45 | 47 | 3118.363 |
| 6034202:48 | 51 | 5935.736 |
| 6034202:63 | 65 | 2951.021 |
| 6034202:69 | 71 | 2971.186 |
| 6034202:81 | 83 | 3004.548 |

All belong to market 6034202; all skipped the immediately following 2-second execution slot. Guard `slot_index > decision_slot + 1` therefore rejected correctly.

## Measurement limits

`orders.updated_us - orders.created_us` measures engine-cycle timestamp delta until simulated fill/rejection. Engine currently captures `at` before collection; this is NOT precise signal-availability-to-fill latency and NOT exchange latency. Eleven filled orders show 2941.543–3073.884 ms by that historical timestamp definition. Rejected orders span 2951.021–5935.736 ms. Snapshot receipt timestamps permit further cycle analysis, but historical ledger lacks full monotonic per-phase tracing.

Service healthy delay is fixed poll interval after collection, while collection performs bounded public network phases. Approximately 3-second observed cadence exceeds 2-second slot spacing. Two ~5.9-second rejections indicate missed extra cycle/slot; exact HTTP/retry attribution cannot be proved from historical rows alone. Do not claim a remote exchange defect.

## Changes/gates

- Preserve immediate-next-slot expiry and source freshness checks.
- New settlement polling bounded to one due condition per cycle; retry schedule persisted with capped exponential delay.
- Avoid synchronous unbounded settlement backlog; it would worsen execution expiry.
- Engine refreshes timestamp after collection and after resolution fetch; settlement daily-loss attribution uses response completion day. New `order_executed` evidence includes `signal_to_terminal_us` and `PAPER_DECISION_TO_TERMINAL_WALL_CLOCK`. This measures paper decision persistence to terminal-cycle timestamp, not remote exchange latency; collection-only monotonic tracing is still absent. Rejected-order delay remains derivable from `updated_us - created_us`. No strategy or execution-window relaxation authorized.
- Potential cadence scheduling change requires separate RED test and measured evidence; shortening a safety window or accepting stale data is not a fix.
