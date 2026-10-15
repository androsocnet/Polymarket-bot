# CP5.4 provenance forensics — 2026-10-15

## Scope and evidence custody

Research only. No production/test edits, commits, network, subagents, runner execution, writable SQLite connection, reset/restore/stash/clean or artifact deletion. Tasks 1–7 remain DONE; no redispatch. Gates unchanged. This report is sole intended new workspace artifact.

Native executable `C:\Program Files\Git\cmd\git.exe` returned HEAD `e9f254a5eb5251c5d99d4c9b6ee2304ee1ec2824`, branch `feat/cp5-data-quality`. Bare `git.exe` invocation initially produced wrapped/compressed status rather than requested native identity; absolute executable resolved ambiguity.

Ledger read completely: `.superpowers/sdd/2026-10-14-cp5-3-research-quality-plan/progress.md` (375 lines), located using directory reads, including ignored directory. Failed report read completely: `docs/superpowers/cp5.3-task9-final-report-2026-10-14.md`.

DB `data/cp5_3_task9_once120_20261014.sqlite3`. SHA256 before and after diagnosis: `cae48b3e05f446f02a2ac77b9f460d2b7385d1bee42f0451f6207925017a823f`. WAL absent before/after. Every diagnostic connection used `sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1', uri=True)`; no writable connection. Integrity query: `ok`.

## Findings

1. **Producer retention defect, not five lost fetches.** There are 57 durable version-2 fetches, 57 completion events, 57 references, 114 observations, 336 scheduler opportunities. All 114 observations and 57 references have unique current-fetch ownership. Exactly 52 fetches have complete padded durable scheduler membership; five omit one old skipped opportunity each. All five current fetches and their two providers completed and persisted. `persistence_status=COMPLETE` describes publication, not complete scheduler provenance or sufficient quality.
2. **Exact loss boundary:** `service.py:326-330` drops non-submitted/non-completed opportunities older than owner NOW minus 60 seconds when adding next opportunity. `service.py:450-460` separately prunes owner history at report receipt minus 60 seconds, retaining request-owned opportunities. Replay and live cadence validation require the preceding 2-second pad (`service.py:501-506`; `scripts/cp5_analyze.py:299-306`). Skipped rows have no requests to extend their retention. They disappear from in-memory evidence while remaining durable. Forward-only producer validation (`service.py:353-361`) does not detect omitted durable rows before publication. Analyzer correctly rejects the mismatch.
3. **Separate persistent quality failure:** recalibration changes wall-grid projection. Anchor 1 maps slot 305 target to `25813562000000`; anchor 2 maps slot 306 to `25814929500000`, interval `1367500000` rather than `2000000000` ns. Both anchors are VALID; no discontinuity event exists. Exact monotonic cadence validator marks bounds unavailable for references 28–57; the transition remains in final 60-second padded window. This independently explains final `INCOMPLETE_PROVENANCE` with 30 occupied cells and zero missing requests. This is producer scheduling/validation contract mismatch, not analyzer inventing missing data. No fix proposed or threshold relaxed.

## Exact forensic rows

All rows share run `e12ff78d-8ac3-4c93-9ea1-4c20729873f8`, market `5644209`, sampling origin `25753328000000`. Fetch, original opportunity, completion and actual-send metadata are version 2. Current opportunities are durable `submitted`, embedded `completed`; completion disposition `completed`. Current slot status COMPLETE / BOOKS_CAPTURED. IDs below are separate namespaces: event IDs are not slot IDs.

### Current completed worker fetch identity

`submit wall us` is original opportunity event `started_us`, sampled during submission before worker starts; exact thread-start/submission monotonic timestamp is not persisted. `first send ns` is first provider wrapper/actual transport start, not substituted submission time. `drain wall us` is completion-event `started_us`, sampled by owner during persistence, not worker completion or exact drain entry. Exact drain monotonic time is not persisted.

| Fetch event | Original opportunity | slot ID/index | target ns | submit wall us | first send ns | observation IDs | reference ID | receipt ns | completion ns | completion event | drain wall us |
|---|---|---|---|---|---|---|---|---|---|---|---|
|600|598|305/304|25811562000000|1791960908794525|25811640000000|51,52|26|25811687000000|25811703000000|601|1791960909088454|
|694|615|307/306|25814929500000|1791960912218882|25815046000000|55,56|28|25815437000000|25815437000000|695|1791960914171481|
|699|697|308/307|25816929500000|1791960914187314|25817015000000|57,58|29|25817078000000|25817078000000|700|1791960914447233|
|708|704|309/308|25818929500000|1791960916117182|25818953000000|59,60|30|25819218000000|25819218000000|709|1791960918171759|
|713|711|310/309|25820929500000|1791960918187890|25821015000000|61,62|31|25821062000000|25821078000000|714|1791960918450803|

All five have NULL validated window bounds. All ten provider attempts returned HTTP 200 without provider error. Coinbase then Kraken identities bind original opportunity, run, market, slot ID/index and target exactly.

| Fetch | Coinbase actual event/attempt/start/end ns | Kraken actual event/attempt/start/end ns | slot reserve start/end ns | slot commit wall us |
|---|---|---|---|---|
|600|602/79/25811640000000/25811687000000|603/81/25811687000000/25811687000000|25811562000000/25811843000000|1791960909020055|
|694|617/88/25815046000000/25815234000000|696/90/25815234000000/25815421000000|25814937000000/25815250000000|1791960912420459|
|699|701/91/25817015000000/25817062000000|702/93/25817062000000/25817078000000|25816937000000/25817218000000|1791960914381036|
|708|706/94/25818953000000/25819031000000|710/96/25819031000000/25819218000000|25818937000000/25819140000000|1791960916323997|
|713|715/97/25821015000000/25821046000000|716/99/25821046000000/25821062000000|25820937000000/25821218000000|1791960918387865|

### Missing memberships, not missing provider fetches

These opportunities were intentionally skipped. No submission, worker identity, observation, reference, fetch event or completion belongs to them. All request outcomes across all 57 fetches were checked for these opportunity IDs: none. Their slot IDs 275–279 have zero request_attempts and zero book_observations. Their complete persistence path is durable skipped opportunity plus MISSED slot; they must still remain in later window membership where required.

| Missing scheduled event | slot ID/index | target ns | scheduled wall us | disposition/reason | event wall us | MISSED commit wall us | immediate scheduled neighbors |
|---|---|---|---|---|---|---|---|
|277|275/274|25751562000000|1791960848000000|skipped/BEFORE_START|1791960857376419|1791960857392330|276,278|
|278|276/275|25753562000000|1791960850000000|skipped/BEFORE_START|1791960857392330|1791960857408331|277,279|
|279|277/276|25755562000000|1791960852000000|skipped/PROCESSING_OVERRUN|1791960857423992|1791960857423992|278,280|
|280|278/277|25757562000000|1791960854000000|skipped/PROCESSING_OVERRUN|1791960857440015|1791960857455803|279,281|
|281|279/278|25759562000000|1791960856000000|skipped/PROCESSING_OVERRUN|1791960857455803|1791960857471629|280,285|

Neighbor 276: skipped BEFORE_START, slot index 273, target `25749562000000`. Neighbor 285: first submitted opportunity, slot 280/index279, target `25761562000000`. Subsequent opportunity 292: slot281/index280, target `25763562000000`.

### Retention proof

For each missing row: replay lower bound < omitted target <= nominal current target minus 60 seconds. Actual submission occurs after reservation, making owner-now cutoff later than nominal target. For 600/277 nominal cutoff equals omitted target; original opportunity and first actual send occur after target. Do not mistake inclusive nominal equality for survival at actual owner-now cutoff.

| Fetch/missing | replay exclusive lower ns | omitted target ns | current nominal target minus 60s ns | preceding fetch / receipt ns | omitted ID present in preceding embedded evidence |
|---|---|---|---|---|---|
|600/277|25751328000000|25751562000000|25751562000000|595 / 25809828000000|yes|
|694/278|25753437000000|25753562000000|25754929500000|609 / 25813812000000|yes|
|699/279|25755078000000|25755562000000|25756929500000|694 / 25815437000000|yes|
|708/280|25757218000000|25757562000000|25758929500000|699 / 25817078000000|yes|
|713/281|25759062000000|25759562000000|25760929500000|708 / 25819218000000|yes|

277 survives previous receipt prune then is removed at submission for fetch600. 278 is already below reference27 receipt cutoff `25753812000000` at preceding drain. 279/280/281 survive preceding receipt cutoffs `25755437000000`, `25757078000000`, `25759218000000`, respectively, then are removed by next submission's owner-now cutoff. Nothing reloads old durable scheduler events into owner history.

Current neighboring opportunities: 591/index303/25809562000000; 598/index304/25811562000000; 605/index305/25813562000000; 615/index306/25814929500000; 697/index307/25816929500000; 704/index308/25818929500000; 711/index309/25820929500000; 718/index310/25822929500000. All submitted; no busy or unsubmitted opportunities in this run.

## Normal versus failed lifecycle trace

References below use repository-relative paths. `S` abbreviates `src/polymarket_bot/recorder/service.py` in this section only.

1. Origin/start: S:793-802 starts session/worker; S:919-926 creates run and durable session before discovery. S:316-331 persists each opportunity before appending immutable owner evidence.
2. Scheduler: `schedule.py:20-37` chooses strictly future unarmed grid target; missed slots enumerate separately. S:995-1001 writes skipped opportunity then MISSED slot. S:1002 changes next miss reason from BEFORE_START to PROCESSING_OVERRUN; it does not mutate existing event reasons. Thus first two relevant skips and later three overruns have honest different reasons.
3. Armed/housekeeping/wake: S:1016-1034 arms target, housekeeping may recalibrate, target recomputed, wait then classify; `schedule.py:40-46` allows capture only within armed 2-second interval. `cp5_run.py:24-40,87-99` buffers diagnostic phases. Recalibration can clear timing identity, leaving WAIT/WAKE without slot identity; absence from slot-filtered timing is not absence of execution.
4. Reserve/submit: S:1043-1054 reserves slot transactionally, drains previous result, creates submitted opportunity, captures context and immutable expected-provider membership (S:467-517). Busy returns after durable busy event; unavailable worker/clock/deadline can write unsubmitted then return; anchor/session absent returns without event. None explains these five: all have submitted original plus actual sends, result and completion.
5. Worker: S:78-106 starts one non-daemon thread, stores result/error under lock; `take()` waits until dead, joins, clears handle/result, raises worker error if any. No concurrent second accepted operation while handle exists. Exact start time not stored. Worker fetch identity is original opportunity, not a thread UUID.
6. Provider: `free_reference.py:138-205` captures send/retry/receipt and attempt result; session `:601-611` calls Coinbase then Kraken and converts provider exceptions into error observations. `:612-632` assigns local observation IDs, marks current opportunity completed, validates receipt horizon, reconstructs report, samples worker completion. `cp5_run.py:294-305` captures pending identity at transport entry. Actual send event persistence can occur after fetch event without changing acquisition time.
7. Drain: S:265-280 routes typed result; S:378 reads original pending identity. Discontinuity changes trigger shared reconstruction only (S:380-389). S:390-411 validates existing members/anchors and maps current local IDs into durable observations. S:412 persists reference before fetch authority. S:425-442 writes fetch and completion as separate committed events. S:444-465 merges owner opportunities after publication and prunes aligned histories. That late merge cannot repair already encoded fetch evidence; here missing old rows were already pruned, not merely late-arriving.
8. Store: `store.py:271-283` transaction commits each call, rollback on failure; DatabaseError latches failed writer. `:904-923` event, `:937-958` reserve, `:1278-1338` observation, `:1340-1386` reference are separate transactions. `:1070` finalizes books/slot; `:1147` marks missed. No atomic all-fetch transaction. Exceptions can leave orphan rows, but this DB has no orphan current observations/references and all completion barriers exist.
9. Buffers: `cp5_run.py:42-62` diagnostic timing batch transaction, restores pending batch on exception; `:64-78` audits deduplicate by attempt identity and flush after parent drain; `:277-278` bounded deque4096, only177 sends here. Timing event IDs order persistence, not execution; their mono_ns captures execution. No overflow evidence here.
10. Shutdown: S:889-899 stops/joins then drains before SHUTDOWN and finish_run; S:865-887 retains live-worker ownership before store close. All five mid-run completions exist; shutdown cannot explain selective older skipped-member omission.
11. Replay: `scripts/cp5_analyze.py:293-306` verifies forward ownership and reverse padded durable membership, correctly catches five producer omissions. `:307-310` independently validates cadence/bounds; `:358-371` uses shared reconstruction for assessment/report equality. Whole replay aborts at first membership failure; 52 complete memberships must not be called 52 replay-certified reports.

Normal control 1: fetch287/version2, opportunity285, slot280/index279, reference1, observations1/2, target25761562000000, receipt/completion25762171000000, completion event288 owner wall1791960860160818. Selection timing event347 mono25760296000000; wake351 mono25761578000000; dispatch352 mono25761687000000; finalize353 mono25762937000000. Numeric VALID; WARMING_UP, insufficient exposed pairs/missing scheduled requests, not provenance-qualified usability.

Normal membership control 2 between failures: fetch609/version2, opportunity605, slot306/index305, observations53/54, reference27, target25813562000000, receipt/completion25813812000000, completion event610 wall1791960912165801. Validated bounds `[25753812000000,25813812000000]`, exact padded membership, still insufficient early coverage. This disproves continuous blanket analyzer omission.

## Recalibration and final quality evidence

Anchor1 midpoint25753562000000/server1791960850000000, uncertainty1172000us. Anchor2 midpoint25813929500000/server1791960911000000, uncertainty1101500us. `clock.py:98-101` projects scheduled wall target through current anchor. S:638-640 recalibrates during housekeeping; S:1024 recomputes target afterward.

Slot306 SELECTION event681 armed old target25815562000000 at mono25813828000000; housekeeping event682 starts there and event687 ends25814375000000. Durable submitted opportunity615 uses new target25814929500000. Subsequent 2-second grid resumes on new projection. Previous slot305 target25813562000000 yields 1.3675-second transition. No rollover or market identity change.

Of57 fetches,31 have absent validated bounds: reference26 and references28–57. Reference26 lacks left bracket after277 retention; references28–57 contain cross-anchor cadence transition. Other26 have actual bounds. `free_reference.py:449-451,506-507` makes absent bounds INCOMPLETE_PROVENANCE; cells do not override it. Reference31 and final57 have zero missing requests; final has all30 cells,15 exposed pairs, no empty pairs, VALID Coinbase, FULL, still INSUFFICIENT solely INCOMPLETE_PROVENANCE. All31 FULL selected assessments insufficient. No replay or usability PASS inferred.

## A–L classification

|Class|Verdict for five omissions|Evidence|
|---|---|---|
|A memory-only|Excluded as original evidence loss; owner evidence is memory-pruned|Missing original opportunities are durable events277–281 and MISSED slots; not unpublished fetches|
|B early return|Excluded|All five have submitted originals, provider sends,114/114 overall observation ownership, fetch and completion|
|C durable report first|Present architecture, not cause here|S:406,412,437,439 independent commits; no orphan rows in DB|
|D drain identity|Excluded for five|Original/pending/fetch/completion/transport slot and opportunity identities agree; exact drain entry monotonic not stored|
|E slot mutation|Excluded|Old slots MISSED without books; current slots COMPLETE, distinct IDs; reserve disallows terminal reacquisition|
|F disposition|Relevant retention predicate, not mislabeled fetch|Skipped rows intentionally lack requests and do not receive submitted/completed retention exemption|
|G buffer|Not timing/audit buffer loss; confirmed bounded owner-evidence retention loss|S:326-330,450-460 drop padded authority before serialization; all audit identities for five present|
|H shutdown|Excluded|Five mid-run current completions persisted, shutdown final drain exists|
|I rollover|Excluded for five; separate recalibration effect confirmed|Same market throughout; anchor change, not market rollover, produces cadence discontinuity|
|J dedup|Excluded as causal omission|Distinct event IDs/targets; known-ID merge and actual-send dedup do not conflate these five|
|K mismatch|Confirmed producer retention vs required validation interval; second cadence-contract mismatch|60-second storage horizon versus padded membership; exact2s monotonic expectation versus recalibrated wall grid|
|L replay bug|Not established; membership rejection correct|Independent DB set query reproduced exact five differences; fail-closed reverse ownership matches reviewed gate|

## Verification limits and reproducibility

Read-only queries selected full schema, all recorder event JSON, current observation/reference owners, affected slots, attempts/books, neighboring opportunities, anchors, scheduler timings and completion events. No missing provenance was fabricated. Exact submission mono/thread-start/drain-entry mono are absent from all inspected persistence paths; stored event wall timestamps and transport/capture/timing monotonic bounds remain distinct.

Independent set calculation for each fetch:

`start=max(origin, receipt_ns-60_000_000_000)`

`durable={event_id for same-run APPROXIMATE_OPPORTUNITY if start-2_000_000_000 < target_ns <= receipt_ns}`

`embedded={id for (id,target,disposition) in fetch.evidence.scheduled_opportunities if start-2_000_000_000 < target <= receipt_ns}`

Exactly fetches600,694,699,708,713 have `durable-embedded` equal singleton277,278,279,280,281 respectively; no extra embedded member. Remaining52 sets equal. Read-only assertions also confirmed all omitted IDs have no request outcomes and lie within required padded interval but at/before nominal submission 60-second cutoff. Initial diagnostic assertion used strict `<` at nominal target for600/277, failed because equality holds; corrected bound to `<=` while preserving actual owner-now explanation. No production/test changes made to obtain evidence.

No tests/typecheck/lint executed: research-only, no code edits; lint remains N/A per ledger. No full exact replay certification claimed; prior failed report remains authoritative failure record. No fixes, rerun, threshold changes, opening/rollover/pilot authorization or fabricated provenance.
