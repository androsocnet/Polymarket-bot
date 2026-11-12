# PAPER Execution Evidence Implementation Plan

> Execute inline with test-driven development and independent review.

**Goal:** Atomically preserve actual execution-side depth and account effects for PAPER attempts without changing execution or risk rules.

**Architecture:** Record evidence in SQLite within the existing execution transaction, linked to the order and freshness event. Preserve unavailable evidence explicitly; never reconstruct historic depth. Keep estimator and recorder separate.

**Tech Stack:** Python, SQLite, unittest.

**Spec:** User request and approved in-chat design.

## Global Constraints

No strategy, scheduling, execution-window, freshness, risk, settlement or isolation changes. No production databases or services. No new dependencies.

## Review Focus

- Crashes or insertion errors must roll back fill and evidence together.
- Duplicate-slot snapshots must capture the exact book actually used.
- Rejections, absent books, and stale/uncertain/late data must not fabricate depth or fills.
- Multiple orders and liquidity reuse must be reported honestly, not presented as proven exchange liquidity.
- Existing databases and direct store users must remain compatible; fees and account changes must reconcile.

## Tasks

- [ ] Inspect schema, transactions, call sites and exact execution branches.
- [ ] Write focused failing tests for exact depth, partial/rejection, accounting, causality and atomic rollback; run RED in WSL.
- [ ] Add minimal additive evidence storage in same transaction as execution result; link engine freshness and slot. Preserve explicit UNVERIFIED when unavailable.
- [ ] Test full/partial BUY/SELL, repeated snapshots, missing/stale/late evidence, risk protections, legacy schema and transaction failure in WSL.
- [ ] Independently review diff and tests; document limitations, including liquidity reuse and separate replay defect.
- [ ] Run targeted tests, check diff and secrets, commit on new branch and push fast-forward without disturbing untracked research.
