# Receipt-time Coinbase / Polymarket protocol v1

Status: prospective research-only protocol, approved2026-11-13; frozen before any new capture. $0. NO LIVE trading. Capture NOT started. No model, edge or profitability established.

## Question and historical boundary

Does latest causally received Coinbase30s log return add information about subsequently received UP-contract midpoint change beyond current midpoint and remaining market time?

October audit remains unchanged:114attempts,52receipt-based joins,23same-window diagnostic features,20standalone book targets(19spot-joined),7intersections,0strictly qualified rows. This protocol is NOT retrospective permission to relabel them. Historical strict requirement source<=receipt lower bound remains recorded. Newly collected receipt-time cohort has a different explicit estimand/label and must be reported separately.

## Six time concepts

| Time | Meaning / use |
|---|---|
| Source timestamp | Coinbase last-trade time or CLOB book timestamp; supplied by remote system, not local arrival or necessarily book-generation instant. Coinbase last trade not simultaneous bid/ask. |
| Local receipt wall time | Local UTC clock sampled on receipt; retain for audit, never assume synchronized or replace calibration with it. |
| Monotonic receipt | Same-process availability ordering and elapsed time. No cross-run/host comparisons. |
| UTC clock interval | CLOB whole-second /time anchor projected by monotonic elapsed; ±1second quantization plus half RTT. No invented precision, new calibration or threshold reduction. |
| Forecast cutoff | Same-run monotonic instant when selected inputs have arrived; batch response end can be availability cutoff, not claimed actual persistence/strategy decision. |
| PAPER slot | Separate execution requirement: entire calibrated decision interval must occupy one existing2second slot and market. Research eligibility never certifies or bypasses it. |

Root cause: all226successful historical book source timestamps lie above receipt lower bound but inside bracket. Strict source ordering unproven, not definitely future or stale. Anchor intervals2.187–2.516seconds cannot certify one2second slot. Smaller RTT alone cannot remove whole-second clock floor. Original raw spot bytes discarded after hashing, preventing independent parsing/source reconstruction.

## Research-only timing gate

`src/polymarket_bot/recorder/research_timing.py:assess_receipt_timing` is pure and NOT wired into recorder acceptance, approximate-reference selection, PAPER, strategy or execution.

1. Caller validates same run/process epoch, exact market/condition/token/window identity from authoritative metadata, Coinbase BTC/USD endpoint/parser revision, positive finite numeric price, authentic complete raw response/hash/size and book depth. No arbitrary metadata-only fixture qualifies.
2. Caller verifies attached anchor run/identity, calibration completed before associated receipt/cutoff, VALID status, no discontinuity across feature/target horizon, age<=120seconds at each use; provide corresponding nominal UTC intervals. Clock intervals at receipt/cutoff must overlap when projected by monotonic elapsed. Local wall/monotonic contradictions are disqualifying, not repaired by adjusting clocks.
3. Latest successful receipt at/before cutoff, never older fallback to rescue rejected source. Receipt and request-start monotonic timestamps nonnegative bounded integers; response duration<=5seconds and receipt age<=5seconds. Missing/error price or timestamp excluded. Missing/incomplete raw evidence excluded.
4. Source definitely beyond receipt or cutoff UPPER bound rejected as DEFINITE_FUTURE_SOURCE. Source<=receipt lower bound labeled SOURCE_NOT_AFTER_RECEIPT. Source within receipt bracket labeled SOURCE_ORDER_UNCERTAIN; acceptable ONLY for this receipt-time estimand, not physical source-order proof.
5. Maximum nominal source age cutoff_upper−source<=5seconds. Do not clamp negative lower source ages to zero. Each UTC interval width<=5seconds, fixed against same freshness budget; this bounds tolerated uncertainty, does NOT claim accurate synchronization. Production thresholds unchanged.
6. Source and entire receipt/cutoff intervals must lie inside authoritative same market window[start,end). No source/receipt boundary borrowing. Missing, malformed/reversed clocks, excessive uncertainty, stale observations and ambiguous boundaries fail closed.

Receipt-time causality establishes information actually available locally; it does NOT establish exchange-event lead, source authenticity beyond retained public response, subsecond latency or executable arbitrage. Cross-venue UTC comparison is CONDITIONAL on nominal exchange UTC timestamps; no independent Coinbase-clock offset calibration. Protocol cannot certify physical5second freshness independent of that assumption. Report nominal ages and uncertainty labels, never describe source ordering as proven when overlapping.

## Fixed feature and target

- x=log(S_t/S_(t−30s)), latest successful Coinbase receipt at/before each cutoff; both satisfy gates, same run/window and raw provenance. No Kraken fallback, interpolation or internet backfill. Repeated trade/source IDs remain last-known prices, not independent new events.
- y=m_UP(future)−m_UP(t). Require identity-correct VALID two-sided positive noncrossed paired books and actual displayed sizes; current midpoint=(bid+ask)/2. First such received quote at/after t+30seconds through t+32seconds; otherwise missing. Select first by receipt, THEN timing/source checks; never replace rejected quote. Retain actual offset.
- Every selected current/past/target receipt, source and complete clock interval inside same market; no horizon boundary crossing. Target data never enters earlier input/fee selection.
- No fit/score now. Any future model protocol requires separate approval, market/date-block splits, purging overlapping horizons and independent holdout.120second quality check does not establish statistical sufficiency.

## Raw-response evidence

ApproximateObservation appends optional raw_response/raw_complete. Provider retains bounded final-attempt bytes(<=1MiB), not reserialized JSON. Successful parsing, malformed payload, HTTP rejection and future-source rejection preserve consumed bytes. Deadline/oversize prefixes remain incomplete. No response is None; captured empty HTTP body is b'' and can be complete. Parser policy unchanged.

Store atomically writes observation and recorder_events.kind=DISCOVERY_GAP JSON type=APPROXIMATE_RAW_RESPONSE/version1, linked run_id/observation_id. Marked diagnostic_only=true and time_domain=LOCAL_WALL so existing metrics do not interpret raw evidence as discovery/rollover outage. Contains provider/pair/endpoint, parser revision, timestamps, SHA-256/retained length, HTTP status, raw_complete, codec_version and raw_z_base64. Decode strict base64 then existing decode_evidence(blob,size,codec); verify SHA-256 and correspondence to observation row. Decode before considering research eligibility. Missing legacy bytes explicitly null; never reconstruct them. Existing DB schema unchanged; historical replay uses metadata-only hydration and cannot claim raw verification.

Only final-attempt body retained, matching existing observation hash semantics; intermediate retry bodies not retained. Response duration for future analysis comes from final actual request attempt in APPROXIMATE_FETCH request_attempts evidence, not guessed from slot cadence. For no attempt/ambiguous mapping reject. Pair raw event with existing fetch anchor/opportunity ownership; raw event alone not market/anchor proof. Bytes/hash/completeness describe received evidence, not proof public exchange data is economically correct. JSON base64 adds bounded disk cost; no credentials/headers retained. Unknown providers have endpoint null and cannot satisfy Coinbase identity gate.

## ONE120-second passive quality capture — PREPARATION ONLY

No command executed. Requires separate explicit capture authorization and reviewed host resource/egress/stop budget. Existing public-only audited runner and fresh disposable research DB; never existing DB or PAPER service. Do not deploy or repoint services.

- Confirm verified published commit/environment and no credentials/private routes. Existing `scripts/cp5_run.py` audited transport and supervisor remain unchanged; inspect their CLI help OFFLINE before constructing final command. Audited supervisor preparation template: `PYTHONPATH=src .venv-wslhiro/bin/python scripts/cp5_run.py --db <fresh-research-path> --seconds 120` (no --mode; no --child). Existing supervisor bound165seconds includes shutdown allowance, never extra collection. CLI inspected offline; no help/network invocation needed. Lower-level recorder CLI instead requires explicit `--mode approximate-reference --duration-seconds 120 --db <fresh-research-path>`; omission of duration is unbounded and prohibited. Prefer audited supervisor for authorized run. Do not run either template now.
- Choose naturally discovered authoritative market with enough interior margin for whole capture and startup; no guessed slug/backfill. Discovery/rollover can lose coverage; reject run rather than fabricate observations. Aligning startup alone does not guarantee opening slot, unnecessary here.
- Keep first30seconds for backward warmup and final32seconds for target tail. Capture starts/stops bound actual eligible interior; never extend to1200seconds or loop automatically.
- Keep unchanged request/deadline/size/retry and shutdown safeguards. Require supervisor exit0, no watchdog kill, no active worker/thread, clients/store closed, run STOPPED and writer reacquirable; STOPPED row alone insufficient.
- Verify copy hash/integrity/FK, every new provider observation's raw event or explicit absence, strict codec/hash/size/completeness, independent parser/source identity, anchor and actual-request membership, missing/failure ledger, eligible joins and fixed target availability. Record separate strict and prospective receipt-time results; do not loosen gates if zero.
- Existing60second approximate-reference mean/cadence smoke gate stays intact and reported independently. This30second receipt-time gate neither overrides it nor asserts engine/reference approval. No trading decisions/fills/model fitting.
- If quality fails stop. If quality passes, separately authorize multi-market/date evidence; no automatic longer collection or profitability inference.

## Change boundary and verification

Only passive provider observation evidence, recorder persistence, historical metadata hydration, new research-only validator/tests/docs changed. No clock.py/freshness.py/PAPER/strategy/risk/schema edits. Production execution eligibility remains separately unestablished.

Focused WSL verification:241tests, OK,3skips (two Windows-only ownership tests; legacy Git-history test unavailable through Windows worktree pointer). Existing SQLite ResourceWarning output recorded; not suppressed. Independent review found one Important metrics interaction: raw events lacked diagnostic_only and fabricated discovery outages. Regression reproduced10second false outage, then passed after marker fix;101affected persistence/metrics/timing tests passed. Historical57reports per recovered copy replay exactly; original/copy SHA-256 unchanged. `git diff --check` clean; no engine/clock/freshness/schema/deploy diff. Fresh focused run after endpoint constant reuse:241tests in52.554s, OK/3skips. Completed full discovery:1581tests in2610.206s,1failure/6errors/30skips; NOT green. Same seven issues reproduced on unchanged base c7a9eacc: retained October fixture DB absent; four WindowsJob tests unsupported on WSL; existing fee-document URL rejected by AST boundary; infrastructure patch.stopall removes offline socket guard (alone passes, ordered pair fails). Added store endpoint literals also violated URL inventory; replaced with existing provider constants, then exact URL inventory matched base. No guard/allowlist/fixture relaxed. Full failure names and matched evidence recorded in plan/ledger. Initial publication stop superseded by explicit2026-11-14 approval accepting seven reproduced baseline issues for this research-only task, subject to final independent review and focused verification. Fresh final-code focused run241tests/48.094s, OK/3documented skips, exit0. Final independent review2026-11-14: READY, no Critical/Important findings, including endpoint reuse; minor stale staged status wording aligned with approval. Seven issues match baseline and do not originate from or materially undermine scoped changes; baseline network-guard test isolation remains a limitation, not production safety approval. No essential safety control or test relaxed. Final commit/publication outcome recorded in final report. Separate earlier full rerun completed:1584tests in2621.100s,1failure/6errors/30skips, exit1; same seven issues. It predates final endpoint reuse; fresh focused241test run covers that change. No capture executed; readiness is offline quality-check preparation only, conditional on separately authorized capture and its actual eligibility result.
