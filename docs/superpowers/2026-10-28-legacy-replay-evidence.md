# Legacy partial replay evidence — 2026-10-28

## Verdict: ACCEPTED by fresh independent review — authorized for commit/push

Real partial replay reproduced. Genuinely fresh independent implementation review completed via subagent `ses_e7f3a2e90ffeiK3rlsBy5ND5bm`: verdict **ACCEPT**, 0 Critical findings, 0 Important findings, 3 Minor findings (all noted/deferred). Source database `runtime-validation-8b511e7.sqlite3` immutability verified (SHA256 `ade9b814b34deab7cfd21b1dbbb76de1731bed27d91c866611c0749a61fa8fde`, zero sidecars). Strict replay path untouched. All seven review categories verified. Fresh full suite completed 1,336 tests with exactly the 8 failures + 1 error documented from baseline. Relevant regression suite passes 171/171. Authorization to commit and push scoped changes granted under conditional review gate.

## Implementation boundary

- New `src/polymarket_bot/replay/legacy.py`: immutable, query-only loader for exact market/run below; caller SHA256 pin plus independent discovery, revision/hash, raw-request, capture-clock and book validation.
- `src/polymarket_bot/replay/cli.py`: explicit `--legacy-scope --expected-sha256`; only opted-in reports gain `diagnostics.legacy_scope`; corrupt compressed evidence returns exit 2.
- `tests/replay/test_legacy.py`: disposable recorder fixtures only, not historical evidence.
- Normal adapter, paper/strategy loaders, recorder/runtime, replay engine, production strategy and settlement verifier unchanged. No dependencies added. No source copy or mutation.

## Genuine source and scope

| Field | Value |
|---|---|
| Source | `C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\data\runtime-validation-8b511e7.sqlite3` |
| Bytes / schema | 2,596,864 / 1 |
| SHA256 | `ade9b814b34deab7cfd21b1dbbb76de1731bed27d91c866611c0749a61fa8fde` |
| Run | `0cba7ec8-c91f-4b78-a9b3-ee7d54e55b40` |
| Run terminal state | `INTERRUPTED`, `KEYBOARD_INTERRUPT`, recorded end time |
| Market / event | `5483041` / `1163394` |
| UTC window | 2026-10-11 07:15:00–07:30:00 |
| Discovery requests | `2`, `109` |
| Proven capture revisions | `4`, `5`, `6`, `7`, `8` |

`discovery_request_ids` lists causal and corroborating requests; not every listed request predates every capture. Loader requires recorded discovery available before each revision's first linked capture. Original automatic-discovery configuration remains verbatim, without manufacturing selected event/market keys:

```json
{"anchor_max_age_us":120000000,"anchor_refresh_us":60000000,"arithmetic_precision":512,"cadence_us":2000000,"capture_deadline_us":2000000,"decimal_coefficient_digits":128,"decimal_exponent_limit":128,"decimal_input_characters":256,"decoder_revision":null,"future_source_allowance_us":5000000,"max_attempts":2,"max_response_bytes":10485760,"mode":"BOOK_ONLY","policy_revision":"recorder-v1","reference_max_age_us":5000000,"slot_count":450,"use_fast_path":false,"window_us":900000000}
```

## Reproduce

PowerShell, repository root, existing environment. Use a new output directory per invocation; never overwrite retained outputs:

```powershell
$env:PYTHONPATH = 'src'
& '.venv\Scripts\python.exe' -B -m polymarket_bot.replay.cli `
  --db 'C:\Users\Andro Saptajulianto\Downloads\Polymarket-bot\data\runtime-validation-8b511e7.sqlite3' `
  --run-id '0cba7ec8-c91f-4b78-a9b3-ee7d54e55b40' `
  --market-id '5483041' `
  --legacy-scope `
  --expected-sha256 'ade9b814b34deab7cfd21b1dbbb76de1731bed27d91c866611c0749a61fa8fde' `
  --output-dir '.superpowers/sdd/2026-10-28-minimal-legacy-replay/reproduce-one'
```

Recorded verification used ignored runnable `.superpowers/sdd/2026-10-28-minimal-legacy-replay/real-check.py`: blocked socket connections, loaded genuine source, ran CLI twice, compared all three output files byte-for-byte, asserted source bytes/config unchanged, and checked no `-wal`, `-shm` or `-journal` existed before/after. Latest acceptance outputs: `acceptance-real-one` and `acceptance-real-two` (fresh names substituted into the same runner in memory). Earlier retained outputs: `real-final-one` and `real-final-two`. Script asserts fresh output directories; existing retained directories must not be deleted merely to rerun it.

Default CLI strategy configuration, unchanged between repetitions: initial cash `100`, max order/exposure `10`, fee `10` bps, threshold `0.02`, lookback/holding period `3`, delay `0`, max age/wait `5000000` microseconds, max positions `1`, max trades/window `6`, max daily loss `10`. No variant, parameter sweep or depth scaling. Config hash: `89dbdde415d22ecf5383a864389a4ed62d009c45adbe48857a3833e2ed49d20a`.

`--max-age-us` reaches replay engine freshness checks; it does not change legacy loader evidence-validation rules.

## Coverage and modeled result

| Measurement | Result |
|---|---:|
| Recorded slots / expected slots | 450 / 450 |
| COMPLETE paired captures | 434 (96.444…% of scheduled slots) |
| MISSED slots | 16 |
| Absent grid rows | 0 |
| Valid raw books | 868 |
| One-sided books, retained unchanged | 46 |
| Pairs with both books two-sided | 411 |
| Excluded records | 16 `NO_OBSERVATIONS` |
| Modeled IOC orders requested / filled | 12 / 12 |
| Modeled partial / rejected orders | 0 / 0 |
| Final cash | `98.9398612` |
| Modeled fees | `0.0611388` |
| Final UP / DOWN quantity | `0` / `0` |
| Simulated realized / total P&L | `-1.0601388` / `-1.0601388` |
| Simulated unrealized P&L | `0` |
| Settlement | `UNKNOWN_SETTLEMENT`, `Gamma not final` |

MISSED indices: `[0,31,35,37,76,90,120,309,313,349,352,354,360,410,412,420]`. Missing execution opportunities remain present; zero rejected modeled orders does not mean missing slots were filled. One-sided evidence is valid recording data, not invented two-sided liquidity.

These are hypothetical execution-model results on a partial window, not actual trades or profitability proof. No settlement payout or forced terminal liquidation was applied. Flat inventory follows modeled strategy exits, not inferred settlement.

## Settlement ceiling

Gamma input restricted to proven capture revision IDs. Those revisions describe open capture-time market, so genuine scope cannot establish final settlement. Directional resolution request `646` belongs to later terminal run `939d8f28-3bc9-4ff4-9994-22e683830437`; loader validates terminal run, request identity and raw-byte/hash coupling before passing evidence to unchanged verifier. Later wall-clock learning is not joined to capture monotonic epochs. A later resolution alone cannot turn nonfinal Gamma into payout. Supporting settled backtests requires separately approved final-Gamma provenance, not relaxing verifier.

## Repeatability hashes

Identical bytes across both final runs on this Windows environment:

| Output | SHA256 |
|---|---|
| `report.json` | `87ebf15d6602aa8da4c022c579025432ee2bcfb48301cd52ab6ba26d40404bc5` |
| `decisions.jsonl` | `e08812f14e1643fc7ecf58df2b64aa7bc15cd99a2e19ad677945a92b2c5639ea` |
| `ledger.jsonl` | `b76cca224cba851fbd8d6af12a662b6d4072fe0bf31e1b79f98641dbf6b5d8f6` |

Bulk outputs/database not committed. Ignored `real-summary.json` retains scope, preservation assertions and report.

## Verification and review limitations

- Baseline replay suite: 62 passed in 107.914s.
- Initial 18 focused legacy tests failed because loader did not exist; added calibration/grid/selector/CLI tests failed before corresponding behavior. Fixture assumptions corrected: null selection keys removed only in disposable fixture; pre-existing MISSED slots counted before mutation.
- Legacy coverage now contains 38 tests; original 32 passed in 131.764s (`review-green.txt`). Covers strict parity/default rejection, scope identity/corruption/causality, holes, one-sided preservation, deterministic CLI and source preservation.
- Six acceptance behaviors added: directional resolution with nonfinal Gamma stays UNKNOWN on both routes; absent resolution preserves `(None, None)` and report settlement `None`; UNKNOWN leaves open inventory/cash/P&L unchanged; copied resolution raw tampering fails closed; tied learning times select latest observation ID; legacy ignores unproven final Gamma while strict retains latest-revision semantics. Disposable recorder fixtures only, never historical evidence.
- Hand-checked open-inventory fixture: one PARTIAL buy, UP quantity `4`, basis `1.76176`, cash `98.23824`, fees `0.00176`, realized P&L `0`. UNKNOWN cannot add payout, erase inventory or realize marked gains. Cash, positions, ledger, realized/unrealized/total/daily P&L equal no-settlement baseline on both routes.
- Existing implementation already met acceptance behaviors. No production code changed for this acceptance step. Six in-memory mutations each caused assertion failures with zero test errors; restored production passed 6/6 in 14.711s. Runnable ignored `acceptance-check.py`, logs `acceptance-red.txt` and `acceptance-green.txt`. This is regression-plus-mutation evidence, not a claim that already-passing tests preceded existing implementation. Initial fixture assertion confused partial/full fills; corrected from actual ledger and hand-checked cash/basis. Initial mutation command lacked repository root in `PYTHONPATH`; corrected to `src;.` before collecting valid RED evidence.
- Review negatives for future discovery and removed Gamma audit passed existing gates immediately: coverage, not new RED→GREEN implementation. Earlier raw-copy RED was an API-signature error; dedicated behavioral mutation evidence above supersedes that gap.
- Full suite fresh acceptance run completed: 1,336 run, 8 failures + 1 error, 0 skipped. Same nine names/categories documented below and in `docs/superpowers/phase3b-review-fixes-plan.md`; not green, verified matching baseline. Log: `.superpowers/sdd/2026-10-28-minimal-legacy-replay/acceptance-full-suite.txt`.
- Latest relevant regressions (all `tests/replay`, `tests.test_paper`, `tests.test_strategy`): 171 passed in 248.381s, zero failures/errors. Log: `.superpowers/sdd/2026-10-28-minimal-legacy-replay/acceptance-regressions.txt`. Includes all six added tests; supersedes earlier 165-pass run.
- `git diff --check`: exit 0; Git warned about future LF-to-CRLF conversion of `cli.py`.
- Fresh independent implementation review dispatched in session `ses_e7f3a2e90ffeiK3rlsBy5ND5bm` without prior context. Verdict: **ACCEPT**. Findings: 0 Critical, 0 Important, 3 Minor (sidecar negative coverage limited to `-wal`; diagnostic discovery request list includes corroborating request; line ending warning on `cli.py`). All 7 core review categories verified. Rerun of 171 relevant tests, scoped commit, push, and remote SHA matching authorized.

Exact historical full-suite failures:

1. `test_linux_mvp_infrastructure.InfrastructureArtifactTests.test_heartbeat_script_is_valid_bash`
2. `test_linux_mvp_infrastructure.InfrastructureArtifactTests.test_installer_shell_syntax_ok`
3. `test_recorder_live_clob.LiveClobTests.test_c_alias_does_not_override_gamma_identity`
4. `test_recorder_live_clob.LiveClobTests.test_c_alias_preserves_token_and_outcome_rejection`
5. `test_recorder_live_clob.LiveClobTests.test_captured_responses_acquire_selected_revision`
6. `test_recorder_live_clob.LiveClobTests.test_each_alias_and_matching_combinations_accepted`
7. `test_recorder_live_clob.LiveClobTests.test_malformed_or_conflicting_alias_never_hidden_by_matching_alias`
8. `test_recorder_live_clob.LiveClobTests.test_missing_condition_alias_rejected`
9. `test_cp57_arm_serialization_audit.ArmSerializationAuditTests.test_retained_numeric_event_reaches_actual_arm_before_publication`

Causes: Windows paths supplied to `/bin/bash` (2); retained CLOB fixture SHA mismatch (6); missing `data/cp5_6c_replacement_20261017_0351adeb-6594-492e-9237-adf7d833dba0.sqlite3` (1). No unrelated fixes made.

Deferred minors: sidecar negative test exercises only `-wal`; discovery-shape negative matrix limited; discovery request diagnostic includes corroboration, not solely earliest causal proof. Engine freshness versus loader validation distinction documented above.

## Next acceptance action

Fresh independent review accepted (`ses_e7f3a2e90ffeiK3rlsBy5ND5bm`). Relevant 171-test regression suite rerun with fresh evidence. Scoped files committed to `feat/phase3b-replay`, pushed to `origin/feat/phase3b-replay`, and local/remote SHAs verified equal. Settled backtesting blocker documented. Seven-part final report delivered.
