"""Read-only quality measurements and same-owner physical checkpoints.

Decimal MB/GB projections describe the observed fixed workload. Logical body
bytes supplement the measured file size; they never stand in for file growth.
"""
from dataclasses import dataclass, field
from collections import Counter, defaultdict
from fractions import Fraction
import json
import math

from polymarket_bot.market_data.numeric import parse_decimal
from polymarket_bot.market_data.models import MarketDataError

from .store import RecorderStore, RecorderStoreError


@dataclass(frozen=True)
class MeasurementReport:
    elapsed_seconds: float
    mode: str | None
    physical_growth_bytes: int | None
    mb_per_day: float | None
    gb_per_30_days: float | None
    bytes_per_terminal_slot: float | None
    bytes_per_complete_slot: float | None
    average_depth: float | None
    status_percentages: dict = field(default_factory=dict)
    latency_percentiles: dict = field(default_factory=dict)
    rollover_gaps: dict = field(default_factory=dict)
    reference_coverage: dict = field(default_factory=dict)
    resolution_summary: dict = field(default_factory=dict)
    denominators: dict = field(default_factory=dict)
    evidence_bytes: dict = field(default_factory=dict)
    depth_breakdown: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


def _evidence_bytes(store, start_us=None, end_us=None):
    bounds = "" if start_us is None else " AND ended_wall_us>=? AND ended_wall_us<=?"
    params = () if start_us is None else (start_us, end_us)
    book = store._one("""SELECT COALESCE(SUM(raw_size_bytes),0) AS raw,
        COALESCE(SUM(length(response_raw_z)),0) AS compressed FROM request_attempts
        WHERE operation IN ('BOOK_BATCH','BOOK_SINGLE')""" + bounds, params)
    ref_bounds = "" if start_us is None else " WHERE first_received_wall_us>=? AND first_received_wall_us<=?"
    reference = store._one("""SELECT COALESCE(SUM(raw_size_bytes),0) AS raw,
        COALESCE(SUM(length(raw_report_z)),0) AS compressed FROM reference_reports""" + ref_bounds, params)
    return dict(book_raw_bytes=book["raw"], book_compressed_bytes=book["compressed"],
                reference_raw_bytes=reference["raw"], reference_compressed_bytes=reference["compressed"])


def _physical(store):
    if store._connection.in_transaction:
        raise RecorderStoreError("fatal_invariant", "Checkpoint cannot measure an open transaction.")
    values = {}
    values["db_file_bytes"] = store.path.stat().st_size
    journal = store.path.with_name(store.path.name + "-journal")
    values["journal_bytes"] = journal.stat().st_size if journal.exists() else 0
    for name in ("page_size", "page_count", "freelist_count"):
        values[name] = store._rows("PRAGMA " + name)[0][name]
    return values


def take_checkpoint(store: RecorderStore, *, run_id: str, wall_us: int) -> None:
    """Read outside transactions, including this checkpoint's physical overhead."""
    store._check_writer()
    values = dict(_physical(store),**_evidence_bytes(store))
    values["committed_slot_count"] = store._one(
        "SELECT COUNT(*) AS n FROM sample_slots WHERE status<>'IN_PROGRESS'")["n"]
    values["reference_report_count"] = store._one("SELECT COUNT(*) AS n FROM reference_reports")["n"]
    checkpoint_id = store.save_checkpoint(run_id=run_id, wall_us=wall_us, values=values)
    # Usually the row fits an allocated page. A page split or integer-size
    # transition may grow it; stabilize the measured physical fields boundedly.
    prior = {name:values[name] for name in _physical(store)}
    for _ in range(3):
        physical = _physical(store)
        if physical==prior:
            return
        store.update_checkpoint_physical(checkpoint_id,physical)
        prior = physical
    if _physical(store)!=prior:
        raise RecorderStoreError("storage_failure", "Checkpoint physical readings did not stabilize.")


def nearest_rank_percentiles(samples) -> dict:
    """Nearest rank: sorted sample at ceil(p*n), one based; no interpolation."""
    values = sorted(samples)
    return dict(count=len(values), **{name: values[max(0, math.ceil(p*len(values))-1)]
        if values else None for name,p in (("p50",.5),("p95",.95),("p99",.99),("max",1))})


def _percent(count, denominator):
    return count*100/denominator if denominator else None


def _physical_complete_count(store, first, last, terminal_count):
    """Infer COMPLETE counts only when endpoint counters determine tied membership.

    Schema v1 has no COMPLETE checkpoint counter or cross-table commit sequence.
    A mixed-status subset at a tied wall timestamp therefore supplies bounds,
    not an invented exact COMPLETE denominator.
    """
    prefixes = []
    for checkpoint in (first,last):
        wall = checkpoint["observed_wall_us"]
        counts = store._one("""SELECT COALESCE(SUM(committed_wall_us<?),0) AS before_count,
            COALESCE(SUM(committed_wall_us<? AND status='COMPLETE'),0) AS before_complete,
            COALESCE(SUM(committed_wall_us=?),0) AS tie_count,
            COALESCE(SUM(committed_wall_us=? AND status='COMPLETE'),0) AS tie_complete
            FROM sample_slots WHERE status<>'IN_PROGRESS' AND committed_wall_us<=?""",
            (wall,wall,wall,wall,wall))
        included = checkpoint["committed_slot_count"]-counts["before_count"]
        if not 0<=included<=counts["tie_count"]:
            return None,None,"CHECKPOINT_COUNTER_TIME_ORDER_UNAVAILABLE"
        low = max(0,included-(counts["tie_count"]-counts["tie_complete"]))
        high = min(included,counts["tie_complete"])
        prefixes.append((counts["before_complete"]+low,counts["before_complete"]+high))
    bounds = (max(0,prefixes[1][0]-prefixes[0][1]),
              min(terminal_count,prefixes[1][1]-prefixes[0][0]))
    if bounds[0]>bounds[1]:
        return None,None,"CHECKPOINT_COUNTER_TIME_ORDER_UNAVAILABLE"
    if bounds[0]!=bounds[1]:
        return None,bounds,"ENDPOINT_TIMESTAMP_TIE"
    return bounds[0],bounds,None


def _union_duration(intervals):
    total, prior_end = 0, None
    for start, end in sorted((s,e) for s,e in intervals if e>s):
        total += end-max(start,prior_end if prior_end is not None else start) if prior_end is None or end>prior_end else 0
        prior_end = max(end,prior_end if prior_end is not None else end)
    return total


def _references(store, eligible, slots, markets, runs, end_us):
    """Count original timely live receipt separately from verified boundaries."""
    rows = store._rows("""SELECT a.*,r.decode_status,r.verification_status,
        r.decoded_price_text,r.decoder_revision,r.first_received_wall_us,
        r.first_received_server_us,r.valid_from_seconds,r.observations_seconds,
        r.expires_at_seconds,f.origin AS fetch_origin,f.purpose AS fetch_purpose,
        q.ended_server_us AS fetch_received_server_us,q.ended_wall_us AS fetch_received_wall_us,
        q.operation AS fetch_operation,q.run_id AS fetch_run_id FROM reference_associations a
        JOIN reference_reports r USING(report_id)
        JOIN reference_fetches f ON f.fetch_id=a.fetch_id AND f.feed_id=r.feed_id
        JOIN request_attempts q ON q.request_id=f.request_id WHERE a.learned_wall_us<=?
        AND r.first_received_wall_us<=? AND q.ended_wall_us<=?
        ORDER BY a.learned_wall_us,a.association_id""",(end_us,end_us,end_us))
    live, boundaries = set(), {}
    by_id = {slot["slot_id"]:slot for slot in slots.values()}
    by_market = {row["market_id"]:row for row in markets}
    for row in rows:
        market = by_market.get(row["market_id"])
        if (market is None or row["status"]!="AVAILABLE" or row["decode_status"]!="DECODED"
                or row["verification_status"]!="VERIFIED" or not row["decoder_revision"]):
            continue
        try:
            if parse_decimal(row["decoded_price_text"],field="reference metric price")<=0:
                continue
        except MarketDataError:
            continue
        if row["role"] in ("OPENING","CLOSING"):
            if row["selection_policy_revision"] and row["target_us"]==market[
                    "window_start_us" if row["role"]=="OPENING" else "window_end_us"]:
                boundaries[(row["market_id"],row["role"])] = row
            continue
        slot = by_id.get(row["slot_id"])
        if slot is None or (slot["market_id"],slot["slot_index"]) not in eligible:
            continue
        run = runs.get(slot["run_id"])
        if run is None or run["mode"]=="BOOK_ONLY":
            continue
        receipt, learned, target = row["fetch_received_server_us"],row["learned_server_us"],slot["scheduled_us"]
        if (row["origin"]!="LIVE_LATEST" or not row["available_live"] or row["target_us"]!=target
                or receipt is None or learned is None or receipt>learned
                or row["fetch_origin"]!="LIVE_LATEST" or row["fetch_purpose"]!="SLOT_CURRENT"
                or row["fetch_operation"]!="REFERENCE_LATEST" or row["fetch_run_id"]!=slot["run_id"]
                or row["fetch_received_wall_us"]>row["learned_wall_us"]
                or row["first_received_wall_us"]>row["fetch_received_wall_us"]
                or row["first_received_server_us"] is None or row["first_received_server_us"]>receipt
                or not target<=receipt<min(target+2_000_000,market["window_end_us"])
                or not target<=learned<min(target+2_000_000,market["window_end_us"])
                or any(row[name] is None for name in ("valid_from_seconds","observations_seconds","expires_at_seconds"))
                or not row["valid_from_seconds"]*1_000_000<=learned<=row["expires_at_seconds"]*1_000_000
                or not 0<=learned-row["observations_seconds"]*1_000_000<=5_000_000):
            continue
        live.add(slot["slot_id"])
    complete = {s["slot_id"] for key,s in slots.items() if key in eligible and s["status"]=="COMPLETE"}
    covered_windows = 0
    for market in markets:
        rows_for_market = [s for s in slots.values() if s["market_id"]==market["market_id"]]
        if (len({s["slot_index"] for s in rows_for_market if s["slot_id"] in live})==450
                and (market["market_id"],"OPENING") in boundaries
                and (market["market_id"],"CLOSING") in boundaries):
            covered_windows += 1
    return dict(live_slots=len(live),live_percent=_percent(len(live),len(eligible)),
                paired_slots=len(live & complete),paired_denominator=len(complete),
                paired_percent=_percent(len(live & complete),len(complete)),
                opening_count=sum(role=="OPENING" for _,role in boundaries),
                closing_count=sum(role=="CLOSING" for _,role in boundaries),
                complete_market_windows=covered_windows)


def _resolutions(store, markets, end_us):
    expired = {m["market_id"] for m in markets if m["window_end_us"]<=end_us}
    rows = store._rows("SELECT * FROM resolution_observations WHERE learned_wall_us<=? ORDER BY learned_wall_us,resolution_id",(end_us,))
    finals, conflicts, delays, unknown = {}, set(), [], 0
    for row in rows:
        market_id = row["market_id"]
        if market_id not in expired or row["classification"] not in ("RESOLVED_DIRECTIONAL","RESOLVED_OTHER"):
            continue
        payouts = json.loads(row["payouts_raw_json"],parse_int=str,parse_float=str) if row["payouts_raw_json"] is not None else None
        if payouts and len(payouts)==2:
            values = tuple(Fraction(parse_decimal(str(value),field="resolution metric payout")) for value in payouts)
            total = sum(values)
            signature = (row["classification"],row["mapped_outcome"],tuple(v/total for v in values) if total else values)
        else:
            signature = (row["classification"],row["mapped_outcome"],row["payouts_raw_json"])
        if market_id in finals and finals[market_id]!=signature:
            conflicts.add(market_id)
        finals.setdefault(market_id,signature)
        if row["learned_server_us"] is None or row["reported_resolved_us"] is None:
            unknown += 1
        elif row["learned_server_us"]>=row["reported_resolved_us"]:
            delays.append((row["learned_server_us"]-row["reported_resolved_us"])/1_000_000)
        else:
            unknown += 1
    directional = sum(v[0]=="RESOLVED_DIRECTIONAL" for key,v in finals.items() if key not in conflicts)
    other = sum(v[0]=="RESOLVED_OTHER" for key,v in finals.items() if key not in conflicts)
    return dict(tracked_expired_markets=len(expired),directional=directional,other=other,
                pending=len(expired-finals.keys()),conflict=len(conflicts),
                success_percent=_percent(directional+other,len(expired)),
                learning_delay_seconds=nearest_rank_percentiles(delays),unknown_learning_delay_count=unknown)


def measure(store: RecorderStore, *, start_us: int, end_us: int) -> MeasurementReport:
    """Measure checkpoint-bounded physical growth; absent checkpoints stay unavailable."""
    if type(start_us) is not int or type(end_us) is not int or end_us < start_us:
        raise ValueError("Measurement interval requires ordered integer microseconds.")
    checkpoints = store._rows("SELECT * FROM storage_checkpoints WHERE observed_wall_us>=? AND observed_wall_us<=? ORDER BY observed_wall_us,checkpoint_id",
                              (start_us, end_us))
    elapsed = (end_us-start_us)/1_000_000
    growth = None
    if len(checkpoints) >= 2:
        elapsed = (checkpoints[-1]["observed_wall_us"]-checkpoints[0]["observed_wall_us"])/1_000_000
        growth = checkpoints[-1]["db_file_bytes"]-checkpoints[0]["db_file_bytes"]
    all_runs = store._rows("SELECT * FROM recording_runs WHERE started_wall_us<=? ORDER BY started_wall_us",(end_us,))
    runs = {r["run_id"]:r for r in all_runs}
    relevant_runs = [r for r in all_runs if (r["ended_wall_us"] or end_us)>=start_us]
    first_start = min((r["started_wall_us"] for r in all_runs),default=end_us)
    effective_start = max(start_us,first_start)
    markets = store._rows("SELECT * FROM markets WHERE first_seen_us<=?",(end_us,))
    all_slots = store._rows("SELECT * FROM sample_slots WHERE scheduled_us>=? AND scheduled_us<?",(start_us,end_us))
    slots = {(s["market_id"],s["slot_index"]):s for s in all_slots}
    grid, prefirst, inprogress = set(), 0, 0
    for market in markets:
        for index in range(450):
            scheduled = market["window_start_us"]+index*2_000_000
            if start_us<=scheduled<end_us:
                if scheduled<first_start:
                    prefirst += 1
                    continue
                key = (market["market_id"],index)
                slot = slots.get(key)
                if slot and (slot["status"]=="IN_PROGRESS" or slot["committed_wall_us"] is None or slot["committed_wall_us"]>end_us):
                    inprogress += 1
                else:
                    grid.add(key)
    statuses = Counter(slots[key]["status"] for key in grid if key in slots)
    terminal = sum(statuses.values())
    denominators = dict(eligible_scheduled_slots=len(grid),terminal_slots=terminal,
        complete_slots=statuses["COMPLETE"],in_progress_slots=inprogress,pre_first_start_slots=prefirst,
        unrecorded_eligible_slots=len(grid)-terminal)
    known_duration = _union_duration((max(effective_start,m["window_start_us"]),min(end_us,m["window_end_us"])) for m in markets)
    unassigned = max(0,end_us-effective_start-known_duration)
    denominators["unassigned_gap_seconds"] = unassigned/1_000_000
    percentages = {status:_percent(statuses[status],len(grid)) for status in ("COMPLETE","PARTIAL","MISSED","FAILED","INTERRUPTED")}
    books = store._rows("""SELECT b.* FROM book_observations b JOIN sample_slots s USING(slot_id)
        WHERE s.scheduled_us>=? AND s.scheduled_us<? AND s.committed_wall_us<=? AND b.status='VALID'""",(effective_start,end_us,end_us))
    valid_depth = [b for b in books if b["bid_level_count"] is not None and b["ask_level_count"] is not None]
    mean_depth = sum(b["bid_level_count"]+b["ask_level_count"] for b in valid_depth)/len(valid_depth) if valid_depth else None
    depth_breakdown = {outcome:dict(valid_books=sum(b["outcome"]==outcome for b in valid_depth),
        bid=nearest_rank_percentiles(b["bid_level_count"] for b in valid_depth if b["outcome"]==outcome),
        ask=nearest_rank_percentiles(b["ask_level_count"] for b in valid_depth if b["outcome"]==outcome)) for outcome in ("UP","DOWN")}
    for outcome in ("UP","DOWN"):
        outcome_books = [b for b in valid_depth if b["outcome"]==outcome]
        for side in ("bid","ask"):
            depth_breakdown[outcome]["mean_"+side+"_levels"] = (sum(b[side+"_level_count"]
                for b in outcome_books)/len(outcome_books) if outcome_books else None)
    depth_breakdown["valid_books"] = len(valid_depth)
    depth_breakdown["excluded_invalid_books"] = store._one("""SELECT COUNT(*) n FROM book_observations b JOIN sample_slots s USING(slot_id)
        WHERE s.scheduled_us>=? AND s.scheduled_us<? AND s.committed_wall_us<=? AND b.status<>'VALID'""",(effective_start,end_us,end_us))["n"]
    attempts = store._rows("SELECT operation,result,latency_ns FROM request_attempts WHERE started_wall_us>=? AND ended_wall_us<=?",(start_us,end_us))
    latency = defaultdict(lambda:defaultdict(list))
    for attempt in attempts:
        latency[attempt["operation"]][attempt["result"]].append(attempt["latency_ns"])
    latency = {op:{result:nearest_rank_percentiles(values) for result,values in groups.items()} for op,groups in latency.items()}
    events = store._rows("SELECT * FROM recorder_events WHERE started_us<? AND COALESCE(ended_us,?)>=?",(end_us,end_us,start_us))
    rollover,discovery = [],[]
    uncomparable_gaps = 0
    for event in events:
        if event["kind"]!="DISCOVERY_GAP":
            continue
        domain = json.loads(event["detail_json"]).get("time_domain","SERVER")
        gap_end = event["ended_us"]
        end_evidence = "EVENT_END" if gap_end is not None else "REQUESTED_END_UNRESOLVED"
        if gap_end is None and domain=="SERVER":
            acquisitions = [m["first_seen_us"] for m in markets
                            if event["started_us"]<m["first_seen_us"]<=end_us]
            if acquisitions:
                gap_end = min(acquisitions)
                end_evidence = "VALIDATED_MARKET_ACQUISITION"
        gap_end = min(end_us,gap_end if gap_end is not None else end_us)
        gap_start = max(start_us,event["started_us"])
        if gap_end<=gap_start:
            continue
        discovery.append(dict(started_us=gap_start,ended_us=gap_end,time_domain=domain,end_evidence=end_evidence))
        if domain!="SERVER":
            # Market windows/acquisitions are calibrated server evidence. An
            # unconverted local-wall event cannot define their rollover duration.
            uncomparable_gaps += 1
            continue
        boundaries = {m["window_end_us"] for m in markets if event["started_us"]<=m["window_end_us"]<gap_end}
        for boundary in sorted(boundaries):
            rollover.append(dict(started_us=max(gap_start,boundary),ended_us=gap_end,
                                 prior_window_end_us=boundary,time_domain=domain,end_evidence=end_evidence))
    modes = {r["mode"] for r in relevant_runs}
    anchors = store._rows("SELECT uncertainty_us FROM clock_anchors WHERE request_end_wall_us>=? AND request_end_wall_us<=?",(start_us,end_us))
    baselines = store._rows("""SELECT c.* FROM storage_checkpoints c WHERE c.checkpoint_id=(
        SELECT c2.checkpoint_id FROM storage_checkpoints c2 WHERE c2.run_id=c.run_id
        ORDER BY c2.observed_wall_us,c2.checkpoint_id LIMIT 1) AND c.observed_wall_us<=?""",(end_us,))
    diagnostics = dict(requested_interval_us=(start_us,end_us),quality_interval_us=(effective_start,end_us),
        physical_interval_us=(checkpoints[0]["observed_wall_us"],checkpoints[-1]["observed_wall_us"]) if len(checkpoints)>=2 else None,
        missing_checkpoints=len(checkpoints)<2,missing_start_checkpoint=not checkpoints or checkpoints[0]["observed_wall_us"]!=start_us,
        missing_end_checkpoint=not checkpoints or checkpoints[-1]["observed_wall_us"]!=end_us,
        startup_baselines=tuple(baselines),schema_version=store._rows("PRAGMA user_version")[0]["user_version"],
        app_revisions=tuple(sorted({r["app_revision"] for r in relevant_runs})),
        configurations=tuple(json.loads(r["config_json"]) for r in relevant_runs),
        clock_uncertainty_us=nearest_rank_percentiles(a["uncertainty_us"] for a in anchors),
        interruptions=sum(r["state"] in ("INTERRUPTED","FAILED") and r["ended_wall_us"] is not None and r["ended_wall_us"]<=end_us for r in relevant_runs),
        restart_downtime_seconds=_union_duration((max(start_us,a["ended_wall_us"]),min(end_us,b["started_wall_us"])) for a,b in zip(all_runs,all_runs[1:]) if a["ended_wall_us"] is not None)/1_000_000,
        whole_period_coverage_established=unassigned==0 and effective_start==start_us and not inprogress and len(grid)>0 and terminal==len(grid),
        projection_label="Fixed observed workload projection; not a guarantee",
        time_domain="Requested/checkpoint times use local wall; slots use calibrated server time; see clock uncertainty",
        formulae=dict(mb_per_day="growth_bytes / elapsed_seconds * 86400 / 1000000 (decimal MB)",
            gb_per_30_days="growth_bytes / elapsed_seconds * 2592000 / 1000000000 (decimal GB)",
            bytes_per_terminal_slot="growth_bytes / newly terminal scheduled slots; includes shared overhead",
            bytes_per_complete_slot="growth_bytes / demonstrable newly COMPLETE book slots in checkpoint interval; includes shared overhead; unavailable if endpoint order ambiguous"),
        pages=tuple(dict(observed_wall_us=c["observed_wall_us"],allocated_bytes=c["page_size"]*c["page_count"],
            used_bytes=c["page_size"]*(c["page_count"]-c["freelist_count"]),freelist_count=c["freelist_count"],
            journal_bytes=c["journal_bytes"],db_file_bytes=c["db_file_bytes"]) for c in checkpoints))
    reference = _references(store,grid,slots,markets,runs,end_us)
    discovery_by_domain = {domain:_union_duration((gap["started_us"],gap["ended_us"])
        for gap in discovery if gap["time_domain"]==domain)/1_000_000
        for domain in {gap["time_domain"] for gap in discovery}}
    diagnostics["discovery_gaps"] = dict(count=len(discovery),duration_seconds=sum(discovery_by_domain.values()),
        duration_seconds_by_time_domain=discovery_by_domain,intervals=tuple(discovery))
    physical_counts = {} if len(checkpoints)>=2 else None
    if physical_counts is not None:
        physical_counts["terminal"] = checkpoints[-1]["committed_slot_count"]-checkpoints[0]["committed_slot_count"]
        if physical_counts["terminal"]<0:
            raise RecorderStoreError("fatal_invariant", "Cumulative terminal checkpoint count decreased.")
        complete,bounds,reason = _physical_complete_count(store,checkpoints[0],checkpoints[-1],physical_counts["terminal"])
        physical_counts["complete"] = complete
        diagnostics["physical_complete_order_ambiguous"] = complete is None
        diagnostics["physical_complete_count_bounds"] = bounds
        diagnostics["physical_complete_unavailable_reason"] = reason
    else:
        diagnostics["physical_complete_order_ambiguous"] = False
        diagnostics["physical_complete_count_bounds"] = None
        diagnostics["physical_complete_unavailable_reason"] = "MISSING_CHECKPOINTS"
    denominators["physical_terminal_slots"] = physical_counts["terminal"] if physical_counts else None
    denominators["physical_complete_slots"] = physical_counts["complete"] if physical_counts else None
    rate = growth/elapsed if growth is not None and elapsed > 0 else None
    return MeasurementReport(elapsed, next(iter(modes)) if len(modes)==1 else ("MIXED" if modes else None),
        growth, growth*86400/elapsed/1_000_000 if rate is not None else None,
        growth*2592000/elapsed/1_000_000_000 if rate is not None else None,
        growth/physical_counts["terminal"] if rate is not None and physical_counts["terminal"] else None,
        growth/physical_counts["complete"] if rate is not None and physical_counts["complete"] else None,
        mean_depth, status_percentages=percentages,latency_percentiles=latency,
        rollover_gaps=dict(count=len(rollover),duration_seconds=_union_duration(
            (gap["started_us"],gap["ended_us"]) for gap in rollover)/1_000_000,
            intervals=tuple(rollover),time_domain="SERVER",excluded_time_domain_count=uncomparable_gaps),
        reference_coverage=reference,resolution_summary=_resolutions(store,markets,end_us),
        denominators=denominators,depth_breakdown=depth_breakdown,diagnostics=diagnostics,
        evidence_bytes=dict(_evidence_bytes(store,start_us,end_us),reference_report_count=store._one(
            "SELECT COUNT(*) n FROM reference_reports WHERE first_received_wall_us>=? AND first_received_wall_us<=?",(start_us,end_us))["n"]))
