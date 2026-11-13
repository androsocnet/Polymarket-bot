# Passive Spot Research Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain authentic spot evidence and define testable prospective receipt-time research eligibility without changing production timing or historical findings.

**Architecture:** Extend existing approximate observations with optional raw evidence; persist linked typed events atomically using existing zlib codec and JSON-safe base64. Add one small research-only timing validator and protocol document; no new collector, schema, engine integration or historical reclassification.

**Tech Stack:** Python >=3.12, stdlib unittest/SQLite/zlib/base64, installed httpx; focused WSL tests.

**Spec:** User-approved chat scope: receipt-time research protocol plus additive raw evidence retention; no clock-precision or production timing changes. Original task requires verification, commit/publication, and preparation only for ONE bounded120s passive capture.

## Global Constraints

- $0; LIVE trading prohibited. No capture, network market-data requests, VPS deployment or service changes.
- No production PAPER engine, strategy, risk or execution timing changes.
- Original databases, prior research, inherited untracked files and Git history untouched.
- New research protocol prospective only. October strict counts remain zero; no model fitting, parameter search or profitability claims.
- Reuse existing schema and encode_evidence/decode_evidence. No database migration or new dependency.
- Stage only task-owned files; publish verified task branch, no force push.

## Review Focus

- Retry overwrites body: retain final-attempt bytes with matching hash, never imply all attempts retained.
- Partial/deadline/no-response: completeness is transport evidence, not parse success; missing bytes distinct from captured empty body.
- Legacy replay hydration: added dataclass defaults must not require nonexistent SQL columns.
- Raw event failure: observation and event roll back together; no detached success with lost evidence.
- Cross-clock overlap: receipt ordering proves availability, not physical exchange-clock synchronization or executable slot certification.

---

### Task 1: Additive raw spot retention

**Files:**
- Modify `src/polymarket_bot/recorder/free_reference.py` and `src/polymarket_bot/recorder/store.py`.
- Modify metadata hydration in `scripts/cp5_analyze.py` and `scripts/cp5_reconstruct.py` only where new dataclass fields require it.
- Extend `tests/test_free_coinbase.py`, `tests/test_free_kraken.py`, `tests/test_free_persistence.py`.

**Interfaces:** Append optional `raw_response: bytes | None = None` and `raw_complete: bool = False` to ApproximateObservation. Provider supplies exact retained final-attempt bytes; absent HTTP response may remain None. Existing metadata-only callers remain supported but cannot qualify for new research protocol. Store emits `APPROXIMATE_RAW_RESPONSE` version1 event linked by run_id/observation_id, containing endpoint identity, parser revision, existing hash/size, completeness, codec version and base64-encoded existing zlib evidence. Empty captured bytes differ from absent evidence. Record event in same transaction as observation. No body in aggregate research publication JSON.

- [x] Write failing exact-byte/completeness tests for successful, malformed, HTTP-error, policy-rejected, partial/oversize, retry and no-response observations; require hashes match retained bytes.
- [x] Run focused provider tests, confirm new expectations fail before implementation.
- [x] Add trailing fields and truthful stream-completion tracking without changing parsing, source acceptance, deadlines, retry count or size cap.
- [x] Write failing store tests: codec round-trip, hash/size/type mismatch rejection, absent legacy evidence explicit, and event-insertion failure atomic rollback.
- [x] Persist bounded raw events using existing codec; validate body exact bytes <=1MiB, completeness type bool, length/hash agreement. Preserve legacy metadata-only fixtures without fabricating bytes.
- [x] Adjust historical metadata hydration to omit new optional evidence fields; do not rewrite historical rows.
- [x] Run provider/persistence/schema and historical replay tests in WSL; verify no schema diff and no source-policy change.

### Task 2: Prospective research timing protocol and preparation

**Files:**
- Create `src/polymarket_bot/recorder/research_timing.py`.
- Create `tests/test_research_timing.py`.
- Create `docs/research/receipt-time-spot-protocol-v1.md`.

**Interfaces:** One pure research-only `assess_receipt_timing(...)` taking same-run identities, source/receipt/cutoff monotonic times, receipt/cutoff UTC intervals, market bounds, response duration and raw-evidence availability. Return explicit eligibility/reasons and source-age interval; never connected to PAPER evaluator. Integer validation fails closed; no synthetic timestamps. Caller must independently validate raw identity/price/depth/parser, anchor provenance and clock continuity.

**Prospective policy decisions:** Receipt/cutoff monotonic ordering establishes availability within same run; last successful observation selection remains fixed, no older fallback after rejection. Receipt age/response elapsed/worst-case nominal source age each <=5s. Source beyond receipt upper bound rejected; overlap permitted only labeled SOURCE_ORDER_UNCERTAIN. UTC interval width <=5s: uncertainty span cannot exceed declared freshness budget, not claim precision improved. Source-clock comparability explicitly conditional on nominal exchange UTC; independent Coinbase offset unmeasured. Source timestamp and entire receipt/cutoff intervals inside market; missing/reversed/discontinuous timing fails closed. No two-second slot requirement for forecast sample; PAPER unchanged. Full raw body, parser revision and supported identities mandatory for new qualified evidence. First-target +30s through+32s, same run/market, no replacement after clock rejection; backward feature selection unchanged.

- [x] Write failing tests for valid monotonic availability despite cross-clock overlap, future receipt/source, missing/stale/invalid timing, excessive interval width, different run, market-boundary ambiguity and missing/incomplete raw evidence.
- [x] Run tests to confirm missing validator failure, then implement minimal pure validator and rerun.
- [x] Document all six time concepts separately: source, local wall receipt, monotonic receipt, uncertainty interval, forecast cutoff, execution slot. Explain availability vs physical freshness assumption and keep October strict finding unchanged.
- [x] Document capture preparation only: fresh disposable DB, explicit --duration-seconds120, public-only existing recorder, same-market interior with30s warmup and32s tail, existing deadline/shutdown checks, raw event and failure ledger checks, no execution or automatic extension. Do not run command. Record startup/discovery may fail interior coverage; reject rather than backfill.
- [x] Document quality result independent from legacy60s approximate-reference cadence gate; neither bypasses other.120s only tests eligibility, not statistical sufficiency.

### Task 3: Review, verify, commit, publish

**Files:** Task files above; add concise final evidence/report to protocol document.

- [x] Isolate task branch/worktree from inherited untracked research before implementation; preserve original workspace and databases.
- [x] Run focused WSL unittest suites for raw providers/store/schema/recorder publication/replay and new timing validator; record exact counts and failures.
- [x] Request independent review of raw event safety, legacy compatibility and protocol assumptions. Fix important findings; rerun affected tests.
- [x] Check task diff for engine/strategy/schema changes, secret/personal-data leakage, whitespace and accidental generated/DB files.
- [x] Recheck original DB SHA-256 and initial workspace Git preservation. Record limitations, readiness and no-capture status.
- [ ] Commit only task-owned changes, publish branch without force, verify remote SHA equals local commit. Report actual failure if publication unavailable; never claim unverified publish.

## Verification record

Focused WSL241tests OK/3skips after final endpoint-constant reuse (52.554s); independent review Important metrics finding fixed with RED→GREEN regression. Completed full discovery:1581tests,1failure/6errors/30skips (2610.206s), NOT green. All seven issues reproduced on unchanged base c7a9eacc: missing retained October fixture DB; four WindowsJob tests unsupported on WSL; existing fee-doc URL AST violation; existing infrastructure patch.stopall removes offline socket guard (guard test alone passes, ordered pair fails). New store endpoint literals added two URL violations hidden behind baseline failure; RED inventory comparison detected these, provider constants replace duplication, GREEN inventory equals baseline. No boundary test weakened. Separate full rerun completed:1584tests in2621.100s,1failure/6errors/30skips, exit1; same seven issues. This run predates final constant reuse; fresh focused241test run covers that change. No capture run. Initial publication stop superseded by explicit2026-11-14 user decision: accept seven reproduced existing issues for research-only task; require final independent review including endpoint fix and focused verification, not another full-suite run. Fresh final-code focused run241tests/48.094s, OK/3documented skips, exit0. Final independent review2026-11-14: READY, no Critical/Important findings; minor stale staged publication wording aligned with revised approval. Seven failures match baseline evidence and do not originate from or materially undermine scoped changes; baseline test-isolation limitation is not production safety approval. No essential safety control or test changed. Final publication outcome recorded in final report.

## Execution choice

Recommended: native execution with final independent review. Two closely coupled small changes; no independent implementation agents needed. No capture authorization implied by code approval.
