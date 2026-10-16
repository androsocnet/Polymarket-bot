# CP5.5 opening blocker remediation checkpoint — 2026-10-16

## Fix round 1 — H1/M1 implementation evidence

Read exact independent review H1/M1. H1 observed RED: normal reference stop and interrupt reopen persisted[1] instead of[1,2]; audit-failure path incorrectly called finish_run. Removed CP5 preflush/finally shutdown override; existing base shutdown now calls owner final-persistence hook after both worker stop/drains and before recovery/SHUTDOWN/finish/checkpoint. Normal reference tests use actual thread completing during join and disposable SQLite read-only reopen; audit failure forbids recovery/finish/checkpoint. Existing exceptional/writer-failure cleanup tests retained. No blanket finally flush.

M1 observed RED after fixture corrections: durable arm+1 accepted with no exception. Store record_event now serializes complete payload using existing `_json`, checks UTF-8 bytes for PREPARED_ARM/PREPARED_ACTIVATION only against unchanged1,048,576, and raises invalid_metadata with EVIDENCE_BOUND_EXCEEDED before transaction/insert. No general event-cap change. Arm remains unpublished on failure; activation existing outer transaction prevents successor publication on failure. Real store boundary test queries BLOB byte lengths at−1/equality/+1 for both types; oversize inserts nothing and does not mark writer failed. Scheduler arm boundary uses actual complete envelope and sanitized failure, not inner fragment budget. Activation boundary coverage is store-level complete payload, not separate scheduler activation exact-boundary matrix; parent reviewer should evaluate this limitation explicitly.

Targeted69 PASS,0 errors/failures. Final socket-guarded full764 PASS647.172s; expanded focused517 PASS597.547s (previous21 modules plus recorder store). All changed Python19 current/18 baseline against untouched6e1f582:265 diagnostics each, exact mapped added[],removed[],equaltrue,0 warnings/informations. Evidence `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-deeufrcd`. Initial7 test typing additions corrected; earlier full764/638.625s passed before typing correction. LintN/A; diffcheck exit0 CRLF notices only. Test construction had multiple syntax/fixture/admission errors before meaningful M1 RED; not counted as product defects. Existing atomic activation test tail and session-lifecycle test name restored after editing error; final discovery retains both tests.

No network/live/subagents/commits/historicalDB writes, gate/clock/research/Chainlink changes. HEAD unchanged9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0. H1/M1 corrections implemented; no self-awarded H0/M0. Parent independent rereview required; live unconditionally forbidden.

## Implementation continuation — historical A–S (superseded by final checkpoint below)

The research A–S below remain historical evidence. This continuation supersedes their research-only scope, blanket owner-approval requirement and unfixed-code verdict; it does not rewrite historical runtime failure.

### A. Mission and authority

User explicitly authorized bounded metadata correction, sanitized durable reasons, exceptional audit correction and offline disposable tests. No network/live/subagents/commits. Starting and final HEAD `9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0`; existing uncommitted Task8 retained. Tasks1–7 not restarted. Systematic debugging, TDD and verification skills invoked.

### B. Policy decision

Reviewed actual supplement lines41–45 and acceptance review lines21,24,33. Selected original event evidence within existing1MiB event JSON, complete bounded enumeration and returned-ID detail/config are required. Complete raw list provenance is not an explicit requirement to duplicate every unselected raw page in prepared/activation JSON. Exact raw attempts still flow to existing control-attempt storage. Bounded selected event plus per-page SHA256/size/completeness/selectors therefore preserves that provenance without new metadata authority, route or schema. No material authority blocker established; no arbitrary ceiling increase.

### C. Metadata correction

`acquire_next_market` no longer rejects successful complete `/events` merely because raw list exceeds1MiB. Observer retains bounded per-page descriptors, forwards original attempt unchanged, retains returned-ID detail and config and original event. Combined selected-event/descriptor serialization remains capped at1,048,576 bytes. No full raw list copies in prepared metadata. Arm and activation persist `list_evidence`; regression caught and corrected their stale `list_raw` lookup.

### D. Unchanged trust boundaries

Strict complete numeric JSON parsing, identity-encoded10MiB transport ceiling, five100-row pages, complete enumeration and unique successor checks unchanged. No prefix/partial parsing, synthesized event flags, guessed IDs, pagination weakening, clock/fallback/cutoff/research/Chainlink/trading edits. Hash identifies bytes, not independent proof of unretained competitors; descriptor explicitly says `selected_event_only`. Existing durable control raw evidence remains available independently.

### E. Specific reasons

`MarketDataError.reason` classifies cause chain without persisting exception text: RESPONSE_TOO_LARGE, JSON_DECODE_ERROR, NUMERIC_PARSE_ERROR, EVENT_SCHEMA_INVALID, NO_VALID_SUCCESSOR, AMBIGUOUS_SUCCESSOR, EVIDENCE_BOUND_EXCEEDED. Existing parent codes preserved, including invalid_metadata compatibility. Both preparation and revalidation persist `failure_reason` beside existing `reason` in existing event JSON. No credentials/body/error-message interpolation.

### F. RED evidence

Observed before metadata production edit: valid large fixture raises `Successor list evidence exceeds bound.`. Five specific-reason subcases initially failed None-versus-required reason. Durable phase trace initially failed missing NO_VALID_SUCCESSOR. Prior continuation observed real SQLite final-audit missing-row regression. Failed-writer metadata exit initially returned2/opening_blocked instead of1/audit_persistence_failed. Durable arm regression initially returned no list_evidence. All these now pass. Ambiguity/evidence-bound and additional matrix controls were extended after shared classifier existed; do not claim separately observed RED for every subcase.

### G. Large-body result

Updated runnable A–F repro retains exact generated body hashes documented below. A1689540 bytes now prepares1002 through `/events`, returned `/markets/1002`, returned condition config; forwards all three attempts including complete1689540-byte original. Compact B also prepares. Malformed/numeric C/D retain invalid_metadata and complete failed attempt; E/F remain no_next_market/ambiguous_market. Updated repro SHA256 `373b73ceaba65b6f8bb849fe9b57ebd03bdae2a0357069be2a793969dadf327e`,6139 bytes. Discoverable unittest cases independently cover behavior.

### H. Metadata matrix

`test_successor_failure_reasons`: malformed syntax, extreme numeric exponent, invalid event shape, empty list, multiple successors and transport oversize; callback restored and attempt forwarded. `test_successor_evidence_exact_boundary`: serialized selected evidence1MiB−1/exact/+1, exact accepted,+1 rejected. Existing identity/revalidation tests retain original indices/detail/config, changed/swapped/missing tokens, stale identity, flags, uniqueness and clock/admission behavior. Selected event oversize remains fail closed. No fabricated metadata.

### I. Durable reason checks

`test_failed_preparation_has_durable_phase_trace` uses real scheduler preparation and SQLite event storage; checks parent code plus sanitized classification across all seven requested reason categories. Actual parser cases separately use real PublicGet/Gamma/strict decoder. Exception messages absent from phase evidence.

### J. Audit correction

Writable MarketDataError branch now stops both workers before owner `_final_persistence` barrier and before terminal finish/store close. CP5Recorder barrier uses existing idempotent audit/timing flush. Base hook does no work for non-CP5 recorder. Existing nested finally cleanup preserved. No unconditional flush after writer failure. Already-failed writer plus metadata exit reports audit_persistence_failed exit1 without recovery/finish/flush writes.

### K. Real-store N/N−1 regression

`test_exceptional_final_audit_real_store`: real disposable v2 SQLite; opening_blocked/invalid_metadata/transport_error semantic exits. One initial completed audit plus real pending thread completing second audit during stop/join. Read-only reopen sees attempts[1,2] exactly once. Repeated flush after close produces no additional write because both IDs already persisted. Previous research double G now persists1 before close and repeated explicit flush remains idempotent.

### L. Failure and cleanup matrix

New audit flush OSError regression requires explicit exception, no finish_run, worker stop and store close. Writer invariant regression requires no flush/recover/finish writes and close. Existing child client-close/store-close/transport-close failures, stream-close evidence, shutdown timing-flush failure and pending-worker owner-retention tests retained and freshly exercised in full/focused suites. Live worker retains owners for watchdog rather than unsafe closure. Store-close errors remain nonzero. No weakened lifecycle cleanup.

### M. Fresh full verification

After last production edit: socket-guarded unittest discovery (`defaultTestLoader.discover('tests', top_level_dir='.')`) **760 tests PASS,640.375s**, errors[],failures[]. Interpreter `.venv/Scripts/python.exe`, PYTHONPATH=src. Guard patches socket.socket.connect with AssertionError `Offline tests: network forbidden`. Initial run760/630.296s had one harness-message mismatch in test_offline_suite_blocks_network, not production failure; corrected guard text and reran. Earlier760/634.750s passed before final durable-arm correction; final640.375s supersedes it. Expected negative CLI/lifecycle output is fixture evidence.

### N. Fresh focused verification

After last production edit: **491 tests PASS,577.985s**,errors[],failures[]. Combined both plan module sets: CP5 safety; free schema/reference/persistence/mode isolation/metrics/Kraken/engine/Coinbase/CLI; CP3 regression/lifecycle/review; recorder service/schedule/boundary/metadata; discovery/parsing/batch_books/read_only_boundary. Same interpreter and socket guard. Earlier491/611.594s preceded final arm-key correction, retained as historical only. Repro A–G freshly passed afterward.

### O. Pyright and lint

All16 changed tracked Python files plus repro checked against untouched read-only `git archive 6e1f582` baseline using identical Pyright1.1.414/interpreter/options. Current17 files177 errors; baseline16 files177 errors; warnings/informations0. Exact unchanged-line-mapped full range/column/severity/rule/message/multiplicity identities: added[],removed[],equal=true. Not globally clean. Evidence `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-c6b48klv`, current-pyright.json/baseline-pyright.json/comparison.json; harness cp55_compare.py reuses existing cp54_compare.py algorithm. Initial two new test import-binding errors corrected before final runs. Lint N/A per existing explicit ledger ruling.

### P. Artifact preservation

Fresh read-only file hashing: evidence DB1368064 bytes SHA256 `859524e5ea081354f6f2e95c58612c41be9192fe73e4a479338ec61ce4766f3a`; raw runtime181031 bytes SHA256 `8fbab4f224e7b867e09706c033643efa23dafc6058a684909f802bff78d62039`; retained analysis179060 bytes SHA256 `72a176c6de68a2de823541ae7b97e93e6472ea3dc422a682dc7da02e96025aa4`. Paths remain historical A. No evidence DB writer opened, repair or artifact deletion. Original missing /events body still unavailable; no uniquely known historical body-specific cause claimed.

### Q. Diff and scope

Final tracked binary diff SHA256 `8700873cad86ef7a5c613cffd53e0b48206c2f1d4a0c5cc41aa0fe2f71cb187b`. Fresh git diff --check exit0, only existing CRLF notices. Existing15-file Task8 diff retained; models.py adds reason classifier, yielding16 tracked modified Python files. CP5.5 edits only existing models/metadata/service/runner and existing tests/repro plus requested checkpoint/ignored ledger. No new dependencies/schema/comments. No staging/commits/destructive Git.

### R. Review boundary and limitations

Offline correction implemented and verified; independent review remains required, not self-certified H0/M0. No live availability/opening/rollover/pilot success inferred. Historical raw80/persisted79 remains untouched. Runtime authority gaps cannot be repaired by synthetic tests. Hash descriptors alone do not independently replay competitors; exact control raw attempts remain separate existing storage authority. Per-subcase strict RED limitation in F is explicit.

### S. Current verdict

DONE for bounded offline implementation and fresh regression/type-delta verification; ready for independent review, not live authorization. Historical CP5 opening remains BLOCKED/NOT VALIDATED; live rerun and pilot NOT RUN. No external authority blocker proven.


## A. Evidence and authority

Research ONLY: exactly two missions, deeper `invalid_metadata` diagnosis and exceptional final-audit loss. No production edits, network, live run, commits, subagents, quality-gate/uncertainty/Chainlink changes, artifact deletion or destructive Git. Tasks 1–7 not restarted. Existing uncommitted Task 8 preserved. `AGENTS.md` empty; existing plan ledger and latest rollover/runtime/final evidence/readiness rereview read. Systematic debugging invoked. Interrupted session already created requested offline fixture/repro; resumed session inspected and reused it.

Verified branch `feat/cp5-data-quality`, HEAD `9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0`. Native tracked binary diff SHA256 before and after research: `8d81ad77b99a0495e0054f277bfb0fac7a1ee7b571e46c5681c3ca156f5be527` (matches frozen runtime evidence).

Evidence DB: `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality\data\cp5_3_rollover_20261016_readiness.sqlite3`, 1,368,064 bytes. SHA256 before first inspection, after initial inspection, before resumed inspection and after final SQL: `859524e5ea081354f6f2e95c58612c41be9192fe73e4a479338ec61ce4766f3a`. Every SQLite connection used `Path.resolve().as_uri()+'?mode=ro', uri=True`. No writer reacquisition or evidence repair. Integrity `ok`, FK `[]`.

Raw runtime authority: `C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_141bd7fe9001Rkx2CRpB2tVEdH`, 181,031 bytes, SHA256 `8fbab4f224e7b867e09706c033643efa23dafc6058a684909f802bff78d62039`.

Retained analysis: `C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_141bdca160016I3ImQeOo3pYk8`, 179,060 bytes, SHA256 `72a176c6de68a2de823541ae7b97e93e6472ea3dc422a682dc7da02e96025aa4`. These hashes cover complete artifact files, not truncated Read displays or a Gamma response body.

## B. Production call map and boundary ordering

`Recorder._prepare_next` (`src/polymarket_bot/recorder/service.py:850-898`) invokes `_control` then `acquire_next_market`. `_control` at :666-674 replaces `public.on_attempt` with `attempts.append`, restores it in finally, then persists received attempts through `store.save_control_attempts` (:1226-1252).

`metadata.acquire_next_market` (:127-161) replaces that callback with `observe`. `discovery.discover_next` (:73-130) calls `GammaClient.list_events` (`gamma.py:17-23`), which calls `PublicGet.get_json` (`http.py:84-118`) then `_read_json` (:130-243). Send admission checks shared deadline; `_receive_body` (:246-264) accepts at most 10 MiB of identity-encoded complete response. `load_numeric_json` (`numeric.py:67-77`) parses complete body before a successful JsonRead return. `_read_json` finally invokes observer with complete raw bytes (:208-217), before returning JsonRead to Gamma/discovery. Therefore observer exception cancels successful return before Gamma shape/future validation.

Observer checks cumulative successful `/events` raw lengths against 1,048,576 at `metadata.py:135-138`, BEFORE forwarding original callback (:141-142). This bound is evidence retention, not transport bound or JSON validity. Valid body can pass transport/parser and fail evidence callback. Finally restores callback (:147).

After callback succeeds, Gamma validates list shape; discovery validates complete enumeration, event IDs/children, exact future window, uniqueness, predecessor separation, returned detail ID/event identity and unchanged signature. Metadata requires retained detail, second aggregate serialized evidence ceiling, original outcomes/config, readiness/clock/deadline. Revalidation repeats same acquisition and compares identity/config. Preparation stores only `exc.code`, not message/type/cause (:885-887, :917-919).

## C. Runnable offline fixtures and actual outputs

File `tests/cp55_opening_repro.py`; command:

```powershell
$env:PYTHONPATH='src'; & '.venv\Scripts\python.exe' -m tests.cp55_opening_repro
```

Actual production `acquire_next_market`/Gamma/PublicGet/strict parser used with HTTPX MockTransport; fixture previous market acquired through real `acquire_market`. No sockets, DB or new dependencies. Assertions check outcomes, callback restoration, exact raw forwarding and positive successor capture. A uses valid trailing JSON whitespace, so A and B have identical complete parsed semantics, including valid future row. Not a replay of historical response. Fixtures generated deterministically in memory.

| Case | Bytes | Actual outcome / paths / forwarded attempts |
|---|---:|---|
| A valid >1 MiB | 1689540 | `invalid_metadata`, `Successor list evidence exceeds bound.`; `/events` only; zero forwarded attempts |
| B same semantic <1 MiB | 639 | Prepared market1002; `/events`, `/markets/1002`, returned condition config; 3 successes retained (639/502/27 bytes) |
| C malformed JSON `[{` | 2 | `invalid_metadata`, `Invalid JSON from gamma GET /events.`; failed complete raw attempt forwarded |
| D invalid numeric exponent | 28 | Same outer invalid_metadata message; failed complete raw attempt forwarded |
| E empty list | 2 | `no_next_market`, `No unique successor.`; successful complete list attempt forwarded |
| F two eligible successors | 1366 | `ambiguous_market`, `No unique successor.`; successful complete list attempt forwarded |

Actual SHA256 for generated body bytes:

- A `dab4db2458ee4fac579f45012f5927e834a312a0b7f9ec6ed08c5c89ca4ea399`
- B `e4a7121f2b341c3a79e89ad8b0eff78b4147160cb2c9bed9d555cb6a7bd5b06e`
- C `8b351adb56355ffc40a4986ab30267eecae89710c63d09cba764a35adfda3ef5`
- D `10eb7ec08e02724bedd6ad953875929dba0b6712c17b4b3697ed92842b247e23`
- E `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`
- F `87c76753c2197c5003d678cdb03618587ee2f29a134a39dbfb50d1cd8d1e5661`

## D. Root proof and historical limitations

Proven reachable deterministic defect: callback conflates successful valid metadata with oversized retained evidence and throws before durable attempt callback. A/B isolate body size with identical semantics; A exactly matches historical transport byte count, NOT historical body.

Historical attempt37 `/events`: HTTP200, 1,689,540 bytes, complete/closed, no transport error, mono38627968000000..38628500000000. NEW preparation starts38627937000000, ends38628531000000 with only `invalid_metadata`; no successor detail/config follows. No `/events` request_attempts row, zero error_code rows in all29 attempts, one predecessor revision. Decompressed retained request blobs contain books/time/current metadata, not missing events response. Raw artifact contains audit counts/header presence/timing and final sends, no response payload. No actual retained `/events` raw/parsed body or original exception message recovered from reviewed evidence. Response SHA256 was not captured; must not invent one.

Absence of forwarded failed attempt makes successful-parse callback rejection strongly consistent with observed storage pattern: malformed/numeric failures normally forward a complete failed attempt (C/D). But historical callback/exception execution and absent body are not an exact replay; do not uniquely certify historical evidence-size exception, malformed JSON, numeric field or unpublished successor. Exact historical body cause remains unknown. What is proven: valid 1,689,540-byte list is deterministically rejected by current preparation path and loses control attempt before persistence.

## E. Transport and numeric exception wrapping

`http.py:187-190` catches `MarketDataError` from complete-body decoder and raises `MarketDataError('invalid_metadata', 'Invalid JSON from gamma GET /events.', http_status=200) from exc`.

C chain: `MarketDataError invalid_metadata / Invalid JSON from gamma GET /events.`; cause `MarketDataError invalid_numeric / Invalid or unsupported numeric JSON payload.`; cause `JSONDecodeError / Expecting property name enclosed in double quotes: line 1 column 3 (char 2)`.

D chain: same outer MarketDataError; cause `MarketDataError invalid_numeric / Invalid or unsupported numeric JSON decimal.`. Strict numeric rules reject nonfinite constants, integers outside signed64, unsupported decimal coefficient/exponent/length. This applies to all parsed JSON numbers, including irrelevant vendor fields. No weaker parser proposed.

A chain: `MarketDataError invalid_metadata / Successor list evidence exceeds bound.`; no explicit cause. Body parsed successfully before observer; payload/raw live only in cancelled return/frame; original callback receives nothing. HTTP error_code stays success locally before observer raises, but no corresponding HttpAttempt reaches owner.

## F. Gamma/discovery invalid_metadata inventory

`gamma.py:22,28`: list must be array of objects; details object. `discovery.py:10,14`: current-event markets array/row object. Current-list :41,45,48,54: page limit, event ID, repeated-event conflict, repeated-market conflict; :69 refresh eligibility/identity. Current fast path catches invalid_metadata/unsupported_identifier_shape at :33-35 and may fall back; successor path does not use guessed slug.

Successor :82 outcomes unavailable; :88 page limit; :91 event ID; :95 conflicting event; :100 missing markets; :103 nonobject row. Future signature conflict/detail changes/predecessor overlap use `identity_conflict`, not invalid_metadata. Zero/multiple candidates use `no_next_market`/`ambiguous_market`; five full pages use `discovery_incomplete`. Page enumeration stays max5 x100 and fail closed; filtering never implies remote absence from transport success.

## G. Parser invalid_metadata inventory

`parsing.py:7-33`: common invalid_metadata constructor, missing/oversized IDs/slugs/text, aware UTC timestamp failures, wrong two-entry JSON-array shape (causes retained in process). :62 active/closed boolean requirement; :77 oversized slug; :83 invalid bounded slug timestamp; :90 malformed condition; :93 invalid outcome labels; :96 not exactly Up/Down. Nested outcomes decoding error wrapped by `_array`; token arrays explicitly use `unsupported_identifier_shape`. outcomePrices failures become warning/None, not fatal metadata invalidation (:102-109). Family/archived/nonmatching window returns None. Future parsing requires exact current.window_end and now before expected_start (:69-73), no relaxed time/identity validation.

## H. Metadata invalid_metadata inventory

`metadata.py:17,21`: original selected event row uniqueness/raw identity; :28,32 original outcome mapping unavailable/ambiguous; :38,45,48 malformed config/token mapping; :123 missing current successful raw observation; :137 cumulative list evidence ceiling; :149 absent returned detail evidence; :154 absent event/list or aggregate serialized evidence ceiling; :158 missing successor CLOB mapping; :160 not ready/expired phase/clock; :226 refresh active/closed missing. Known CLOB numeric errors become warnings/removed normalized fields and fail capture readiness, rather than widening parser rules. Refresh errors may be converted to warning strings (:200-202,:250-251). Revalidation mismatches use identity_conflict. `_selected_market`/`_index` direct load_numeric_json may also propagate invalid_numeric without HTTP wrapper.

## I. Where evidence/message detail disappears

HttpAttempt fields retain status, method/path/selectors, timing, result/code, complete raw bytes; no original exception message/type/chain/parsed payload. `_control` buffers then store compresses exact received bytes (`store.py:1238-1247`). A rejects before that buffer; parser C/D reach it with raw, outer code only. Gamma/parser validation errors occur after successful attempt, so owner normally retains successful raw but not parse exception chain. `_phase_trace` persists reason=exc.code only; final outer opening cutoff records `opening_blocked`, not original preparation exception. AuditTransport captures byte count/status/timing/header presence, not response body/hash/parsed object. These are separate losses; no phase label can reconstruct missing body.

## J. Evidence policy option A — align existing full retention ceilings

Smallest compatibility correction candidate: align preparation full-list retention with existing 10MiB per-response transport allowance, with a deterministic cumulative cap across all <=5 pages and combined serialized event/list evidence. Keep raw bytes once rather than decode/re-encode repeatedly. Persist original attempt before evidence-specific rejection and preserve bounded exception diagnostics. Transport bound remains10MiB; no unbounded acceptance. However lifting 1MiB frozen prepared-evidence contract and storing larger arm/activation event JSON changes accepted design budget. Not implemented; requires owner choice, not arbitrary larger constant. Full list duplication in `prepared_event` plus list_raw and serialized JSON must be budgeted, not merely increase first guard. Up to5 pages cannot silently consume50MiB plus copies.

## K. Evidence policy option B — hashes plus authoritative selected fragments

Preferred bounded durable evidence policy if owner approves changed evidence contract: retain response SHA256/byte size/completeness and bounded request selectors/pagination metadata for every actual page; retain full selected event/market fragments only within explicit total byte budget, returned detail/config raw and original outcomes. Deterministic page order, complete strict parsing and uniqueness enumeration still mandatory before selection; retain validation result/count/conflict disposition for full enumeration. Selected fragment is an explicitly derived complete JSON object, never a prefix presented as full response. Hash cannot independently replay unretained competitors or prove enumeration; explicitly label that limitation. If full-response offline enumeration replay is required, B is insufficient; choose A or bounded external raw retention instead.

Memory stays bounded by existing10MiB input per page plus parser object/copies and fixed cumulative evidence limit; parser expansion is not equal to byte cap and needs explicit object/depth budget if strict practical ceiling demanded. Reuse current strict parser; no partialJSON, stream-prefix parsing, numeric weakening, new schema or new dependency. Streaming digest during receive optional only if already consuming complete body; does not eliminate current full parse memory. Stop before material architecture change; approval needed for fragment-vs-full raw authority, budgets and replay guarantees.

## L. Evidence policy option C — retain current ceiling and fail closed transparently

Keep1MiB preparation ceiling: bounded evidence-unavailable result with response count/hash/size and original bounded diagnostic, forwarded request evidence before callback rejection under its own accepted cap. This does not fix availability for valid large responses, but avoids claiming invalid metadata and preserves honest blocker. Reducing requested page limit may lower individual bytes but is not sufficient: cumulative full enumeration may still exceed1MiB; five-page completeness contract cannot silently become incomplete. No server selector may assume unpublished/future identity. Do not truncate raw JSON or accept only selected prefix.

Recommendation: first smallest diagnostic/ownership correction (always preserve bounded attempt descriptor and precise failure before policy exception); owner chooses C if1MiB remains contractual, A for full replay within explicit larger aggregate budget, B for smaller durable evidence with reduced raw replay authority. No policy or production change authorized here.

## M. Historical final audit reconciliation

Run `de53822e-eccb-44b8-800f-dbf0e6b9c3d9`: raw stdout has80 actual_send records; final result carries sends. DB has79 ACTUAL_SEND_ATTEMPT rows, max persistence event743; terminal event744 INTENDED_ENDPOINT. Exact missing set by attempt ID = {80}. Attempt80: successful GET `clob.polymarket.com/time`, response10 bytes, complete=true, stream_closed=true, error=null, mono38655953000000..38656140000000; wall1792104297709599..1792104297900338. Raw public requests: time2/current slug1/predecessor config1/events1/books25/Coinbase25/Kraken25. No repaired DB.

request_attempts calibration rows1 and29 both success and raw10 bytes; second matches attempt80 timing exactly. This is not failed transport, absent calibration or failed writer; it is missing transport-audit flush after successful final control IO. Last persisted provider/book audit through79 precedes final calibration; INTENDED_ENDPOINT decision38656156000000 follows calibration.

## N. Exact flush gap and lifecycle ownership

`AuditStream.close` (`scripts/cp5_run.py:134-144`) calls AuditTransport.finish (:239-243), which appends/emits completed send and optionally prints raw evidence. Recorder owner persists audit via `CP5Recorder._flush_audit` (:64-73), invoked after `_approximate_drain` (:75-78) or `_shutdown` (:80-85).

Final `_calibrate` calls `_control` (`service.py:686-690`); `_control` persists request_attempts but does not flush transport deque. Later opening cutoff `_check_opening_deadline` (:823-831) raises MarketDataError. `_execute_started_run` exception branch (:1171-1176) recovers/finishes FAILED directly, bypassing virtual `_shutdown`; finally stops workers and closes store (:1177-1199). `_execute_run` also closes when worker inactive (:1085-1097). Thus no post-calibration drain/shutdown callback persists pending attempt80 before store close. Resource closure and durable audit completeness are separate. Existing writer-failure branch intentionally forbids further writes (:1166-1170); do not defeat it with unconditional finally flush.

## O. Offline exceptional ownership reproduction

Same repro file caseG invokes actual `_execute_started_run` MarketDataError branch on CP5Recorder with minimal in-memory Store double and no workers, pending `/time` audit event, substituted record operation raising `opening_blocked`. Actual output: pending1/persisted0, calls `[recover, (finish, FAILED, opening_blocked), close]`, result exit2. Separate explicit `_flush_audit` control called twice persists exactly1, proving idempotence and bypass rather than unusable audit buffer. Store double intentionally permits control call after close; this is a method reachability/idempotence check, not proof real SQLite accepts writes after closure. No DB/network created. Synthetic BOOK_ONLY isolates common exception ownership; historical approximate-mode source confirms stop does not call drain on this branch. No full worker/storage-failure matrix claimed.

## P. Minimum audit correction recommendation

After writers/workers are safely quiesced and while store remains writable/open, exceptional MarketDataError finalization must execute same owner-controlled final audit persistence barrier as normal termination, before finish/close. Smallest scoped candidate is writable exceptional-path hook into runner's existing audit flush; do not call entire shutdown blindly: it also drains/recoveries/checkpoints and can change error priority. Preserve original semantic error, forbid writes after writer invariant/storage failure, surface audit persistence failure honestly, do not add network. Needs offline real disposable DB regression for post-control opening failure, pending worker completion, flush failure and repeated-finalization idempotence before production correction accepted. No production fix in this research.

## Q. Verification and preservation

A–F andG/control executed successfully before interruption, after resume, and after fixture typing correction. Repro SHA256 final `34117c4e25966e481b429ecdd4172580cf994e2fb21019210eb73a4e913ef2cb`. Typecheck command `pyright --pythonpath '.venv\Scripts\python.exe' tests/cp55_opening_repro.py`: initial8 attributable Store-double annotation diagnostics; fixture-only `cast(Any, ...)` corrected test injection boundary; final0 errors/0 warnings/0 informations. Production untouched. Lint N/A inherited ledger ruling. Full753/focused344 historical, not rerun/relabelled; no all-project fresh typecheck claim. Native `git diff --check HEAD` exit0 (existing CRLF notices). SQL integrity/FK and raw count extraction are read-only evidence reconciliation, not live execution or production fix verification.

## R. Scope and remaining approval boundary

Only new requested repro and checkpoint plus append-only plan ledger changed by this research. Existing15 tracked Task8 Python edits and all earlier artifacts remain. No evidenceDB modification; hashes unchanged. No material architecture implemented. Owner approval required before changing full-raw evidence contract/budgets or exception-finalization integration. No assertion of historical successor publication/identity, malformed body, exact numeric field, successful opening/activation or pilot readiness. More raw recovery would require a separately retained original response; no network refetch can reconstruct historical bytes.

## S. Verdict

Research findings established: deterministic valid-large-list rejection and pre-owner-callback evidence loss; separate exact exceptional final-audit flush bypass explaining raw80/persisted79, supported by historical timing/storage plus offline branch reproduction. Historical invalid_metadata exact body-specific cause remains unknown. CP5 remains BLOCKED; production fixes, live rerun, rollover and pilot NOT RUN. Resume from these two findings only, not Tasks1–7.

# CP5.5 OPENING BLOCKER REMEDIATION CHECKPOINT

Final current report — 2026-10-16. This single A–S report supersedes all historical status/authority statements above; their chronology and evidence remain preserved. Offline finalization only. No network, live execution or subagents.

## A. Starting HEAD

Branch `feat/cp5-data-quality`; starting HEAD `9d26a7dd56a25daf0af3d02b6ad6f38b25c95fc0`. Actual status, full HEAD-relative source/test diff and last ten commits inspected before staging. Starting state: 18 modified tracked Python files and 30 untracked files, including requested repro/report/reviews and unrelated historical reports. Existing Task8 context preserved, not mislabeled as new CP5.5 work. No machine-readable pre-CP5.5 patch reconstructed; historical diff hash is not a baseline commit.

## B. Blocked DB hash and raw artifact preservation

Fresh read-only byte hashing, no SQLite open or writer reacquisition:

| Exact path | Bytes | SHA256 |
|---|---:|---|
| `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\.worktrees\cp5-data-quality\data\cp5_3_rollover_20261016_readiness.sqlite3` | 1368064 | `859524e5ea081354f6f2e95c58612c41be9192fe73e4a479338ec61ce4766f3a` |
| `C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_141bd7fe9001Rkx2CRpB2tVEdH` | 181031 | `8fbab4f224e7b867e09706c033643efa23dafc6058a684909f802bff78d62039` |
| `C:\Users\Andro Saptajulianto\.local\share\opencode\tool-output\tool_141bdca160016I3ImQeOo3pYk8` | 179060 | `72a176c6de68a2de823541ae7b97e93e6472ea3dc422a682dc7da02e96025aa4` |

All match historical retained hashes. Raw80/persisted79 and missing audit attempt80 remain unchanged; no retrospective repair.

## C. Actual call path

`Recorder._prepare_next` (`service.py:850`) uses `_control` around `acquire_next_market` (`metadata.py:127`), then `discover_next` (`discovery.py:73`), Gamma, PublicGet complete strict numeric JSON parser and observer. Observer now forwards original attempts and retains page hash/size/completeness/selectors instead of copying every unselected list body into prepared JSON. Returned `/markets/1002` and returned condition config remain authority. Revalidation repeats acquisition. Arm persistence (`service.py:929-937`) precedes usable arm publication; activation event (`:963-979`) commits inside transaction before active revision publication. Complete store serializer guard (`store.py:926-937`) enforces durable bound.

## D. Offline A–F

Fresh socket-guarded `runpy.run_module('tests.cp55_opening_repro', run_name='__main__')` PASS. Exact runnable file `tests/cp55_opening_repro.py`, not historical response replay:

| Case | Bytes | Result |
|---|---:|---|
| A valid large | 1689540 | prepared1002; all three original complete attempts forwarded |
| B same parsed semantics | 639 | prepared1002; returned detail/config requested |
| C malformed | 2 | invalid_metadata, JSONDecodeError cause; complete failed raw forwarded |
| D extreme exponent | 28 | invalid_metadata, invalid_numeric cause; complete failed raw forwarded |
| E empty | 2 | no_next_market; successful raw forwarded |
| F ambiguous | 1366 | ambiguous_market; successful raw forwarded |

Fresh generated hashes exactly match historical A–F hashes above. G additionally persists one audit before close and repeated flush remains idempotent. G double is not proof of SQLite writes after close; real-store tests provide durable evidence.

## E. Root and historical unknown

Valid-large-list callback rejection before owner forwarding is proven reachable by synthetic RED/GREEN. Equal byte count is not equal historical body/hash. Original historical HTTP200 `/events` body and original exception message remain unavailable in reviewed artifacts; exact body-specific invalid_metadata cause remains unknown. Malformed/numeric/schema alternatives cannot be uniquely ruled out. No historical successor identity, publication or opening success inferred.

## F. Bounded decision

Selected original event plus bounded aggregate page descriptors within unchanged1,048,576-byte durable event budget; original full attempts still flow to existing control-attempt storage. Complete strict parsing, identity encoding/10MiB transport, five100-row pages, full enumeration/uniqueness and returned-ID detail/config unchanged. No guessed slug, partial JSON, truncation, ceiling increase, schema/dependency changes or policy relaxation. Hash alone does not replay unretained competitors; `selected_event_only` remains explicit.

## G. Durable reasons

Existing parent codes preserved. Sanitized `failure_reason`: RESPONSE_TOO_LARGE, JSON_DECODE_ERROR, NUMERIC_PARSE_ERROR, EVENT_SCHEMA_INVALID, NO_VALID_SUCCESSOR, AMBIGUOUS_SUCCESSOR, EVIDENCE_BOUND_EXCEEDED. Preparation/revalidation use same classifier without persisting exception messages, credentials or body text. Real parser tests plus durable scheduler preparation checks cover seven categories; no claim of separate durable seven-category revalidation matrix.

## H. Audit RED

Historical research demonstrated pending1/persisted0 on exceptional semantic exit; real-store continuation reproduced missing final completion. Independent H1 found normal reference shutdown persisted[1] instead of[1,2]. Fix-round meaningful RED also showed audit failure incorrectly finishing run. These are retained observed RED records, not newly manufactured failures during finalization.

## I. Audit fix and GREEN

`CP5Recorder._final_persistence` (`scripts/cp5_run.py:75-77`) flushes audit/timing. Common normal shutdown stops/drains both workers before final barrier and terminal recovery/finish/checkpoint. Writable semantic exception stops both before barrier/finish; its existing recovery call precedes worker stops (`service.py:1176`), so do not claim all exceptional recovery moved after barrier. Failed writer/invariant branches forbid further audit/finish writes; flush/join failures remain explicit failures. No blanket finally flush. Fresh real-store normal stop/interrupt and three semantic-error subcases reopen read-only with exact ordered IDs[1,2]; no duplicates. Explicit flush/writer-failure controls PASS.

## J. Metadata RED/GREEN

Retained large valid list RED: evidence callback rejected before forwarding; durable complete arm+1 RED: accepted with no exception. Current store guard serializes whole PREPARED_ARM/PREPARED_ACTIVATION with existing `_json`, rejects above unchanged1MiB before insert. Both event types tested at−1/exact/+1 using queried SQLite BLOB lengths, no oversize row or failed-writer flag. Actual scheduler arm envelope and atomic/single-use/stale/late/storage activation tests freshly PASS. Dedicated scheduler activation exact-boundary matrix absent; store-level both-type boundary plus transactional integration accepted as nonblocking coverage limit by rereview. Fixture syntax/admission mistakes are not product RED. Not every classifier subcase had separate observed RED.

## K. Coverage matrix

| Area | Evidence and limit |
|---|---|
| A–F/root distinction | Fresh runnable repro; historical body unknown |
| Provenance descriptors | Large-list hash/size/raw-forwarding tests in fresh full/focused |
| Durable1MiB | Both store event types exact boundaries; scheduler arm integration |
| Complete strict JSON/10MiB | Parser/numeric/schema/oversize controls; no prefix acceptance |
| Full enumeration | Discovery incomplete/conflict/zero/two controls; five-page bound unchanged |
| Detail/config authority | Returned IDs, original indices/config identity controls |
| Future identity/tokens | Contiguous window, actual now, predecessor disjointness controls |
| Stale ownership/activation | Late/stale/invalidated/clock/storage/atomic/single-use; pending old owner integration |
| Sanitized reasons | Real parser plus durable preparation; revalidation matrix limit disclosed |
| Audit exact-once | Real normal/semantic exits, completion during join, read-only reopen |
| Failure paths | Writer no-writes, flush failure, no false finish/checkpoint |
| Cleanup/scope | Existing close/transport/timing/worker retention controls; all full/focused executed |

## L. Fresh full suite

764 PASS,655.058s,0 failures,0 errors, exit0. `.venv/Scripts/python.exe`, session `PYTHONPATH=src`; `unittest.defaultTestLoader.discover('tests', top_level_dir='.')`, TextTestRunner. `socket.socket.connect` patched to raise `AssertionError('Offline tests: network forbidden')`. Existing negative CLI/lifecycle fixture output expected. Disposable test DBs only. Earlier764/647.172s retained historical, not mislabeled fresh.

## M. Fresh focused and targeted

517 PASS,604.648s,0 failures/errors, exit0, same interpreter/guard. Exact modules under `tests.test_`: `cp5_safety free_schema free_reference free_persistence free_mode_isolation free_metrics free_kraken free_engine free_coinbase free_cli cp3_regression cp3_lifecycle cp3_review recorder_service recorder_schedule recorder_boundary recorder_metadata discovery parsing batch_books read_only_boundary recorder_store` (22 modules).

Fresh targeted10 PASS1.153s: CP5SafetyTests `test_normal_reference_final_audit_after_join`, `test_normal_audit_failure_never_finishes`, `test_exceptional_final_audit_real_store`, `test_failed_writer_metadata_exit_reports_persistence_failure`, `test_writer_failure_never_flushes_audit`, `test_exceptional_audit_flush_failure_closes_store`; StoreTests `test_prepared_complete_event_json_boundary`; RecorderServiceTests `test_arm_activation_durable_boundary`, `test_prepared_activation_atomic_single_use_and_admission`, `test_prepared_activation_rejects_late_stale_invalidated_and_storage_failure`.

Historical targeted69 is retained checkpoint/ledger evidence, exact selector not recovered; NOT claimed freshly rerun or added to full/focused counts. Fresh10 matches rereview selected scope and is overlapping coverage, not additional unique suite tests.

## N. Pyright and lint

Fresh local `cp55_compare.py` rerun (no install/network): Pyright1.1.414, current19 files265 errors; untouched `git archive 6e1f582` baseline18 files265 errors, warnings/informations0. Full unchanged-line-mapped range/column/severity/rule/message/multiplicity comparison `added=[]`, `removed=[]`, `equal=true`. Pyright exits1 for existing diagnostics; zero-added gate PASS, not globally clean.

Exact fresh evidence directory `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp54-k3978tx_`: `current-pyright.json`, `baseline-pyright.json`, `comparison.json`. Harness `C:\Users\ANDROS~1\AppData\Local\Temp\opencode\cp55_compare.py` reuses `cp54_compare.py`; all18 modified tracked Python plus repro included before commit. Prior `cp54-deeufrcd`265 comparison is historical corroboration, not fresh path. Lint N/A per explicit existing ledger ruling.

## O. Diff and commit scope

Fresh `git diff --check` and staged check exit0 before code commit; existing CRLF conversion notices only. Reviewed actual status/diff/log10. One logical reviewed whole Task8+CP5.5 Python commit:18 previously modified files plus requested repro,19 files total. Existing preparation/admission/endpoint/analyzer dependencies are entangled with corrections; unsafe partial hunks avoided. No unrelated artifact, historical report, DB, secret, dependency lock or configuration staged. Tasks1–7 not restarted. No amend/push/merge/history rewrite.

## P. Independent review

Read `docs/superpowers/cp5.5-independent-review-2026-10-16.md` (historical H1/M1), then fresh supplied `docs/superpowers/cp5.5-rereview-2026-10-16.md`: H1 ADDRESSED, M1 ADDRESSED; spec PASS, quality PASS, remaining scoped High0/Medium0, correction-induced High0/Medium0. Rereview independently ran7+3 tests; its full/focused/Pyright were honestly retained evidence. Finalizer independently inspected current source/tests and freshly repeated full/focused/type comparison/selected10, not a new independent reviewer or whole-project certification. No code edits after accepted rereview, only logical commits and requested documentation.

## Q. Commits

Verified local code commit `0fc5ba1464d8bb7ed401054dbfa9aa4dd694cf20` — `fix: bound prepared opening evidence and finalize audit ownership`. Includes reviewed whole Task8+CP5.5 Python scope described in O. Separate requested CP5.5 checkpoint/review documentation commit follows; its actual hash is recorded in ignored progress ledger and final response after commit, avoiding impossible self-referential commit hash. No historical CP5.3 reports/plans included.

## R. Final status

At code commit HEAD `0fc5ba1464d8bb7ed401054dbfa9aa4dd694cf20`, tracked working tree/index clean;29 untracked requested/historical documents preserved. After committing only this checkpoint and two CP5.5 reviews, expected26 unrelated untracked artifacts remain; actual post-document-commit HEAD/status are verified and recorded in ignored ledger/final response. Ignored ledger intentionally retained, not force-staged. No disposable DB/artifact deletion or staging. Historical DB/raw hashes match B; live retry/rollover/pilot NOT RUN.

## S. Risks and final offline verdict

**CP5.5 READY FOR ONE ROLLOVER RETRY**

Offline readiness verdict only, supported by scoped H0/M0 rereview and fresh green offline gates. **NOT authorization. Network/live execution forbidden under current instruction under all conditions; live NOT RUN.** Historical opening remains BLOCKED / NOT VALIDATED. Live metadata availability, successful activation/rollover and pilot remain unproven. Historical body-specific root unknown, missing attempt80 unmodified,265 existing mapped type diagnostics retained. Hash descriptors cannot independently replay unretained competitors; dedicated scheduler activation exact-byte matrix and durable revalidation seven-reason matrix remain disclosed nonblocking coverage limits.
