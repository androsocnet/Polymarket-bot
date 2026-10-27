"""Strategy variants: prespecified parameter grid for offline evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Variant:
    """Strategy parameter variant for backtesting."""
    name: str
    hypothesis: str
    parameters: dict[str, Any] = field(default_factory=dict)


def get_variant_grid() -> dict[str, list[Variant] | Variant]:
    """Return the complete prespecified parameter grid from Phase 3A hypotheses."""
    grid: dict[str, list[Variant] | Variant] = {}

    # Baseline: reference parameters matching Settings defaults
    grid['baseline'] = Variant(
        name='baseline',
        hypothesis='Reference PaperEngine momentum strategy with default parameters',
        parameters={
            'initial_cash': '100',
            'max_order': '10',
            'max_exposure': '10',
            'fee_bps': '10',
            'threshold': '0.02',
            'lookback': 3,
            'holding_period': 3,
            'delay_us': 0,
            'max_wait_us': 5_000_000,
            'max_age_us': 5_000_000,
            'max_positions': 1,
            'max_trades_per_window': 6,
        }
    )

    # Entry price cap: max_entry_price in {0.5, 0.6, 0.7, 0.75, 0.8}
    grid['entry_price_cap'] = [
        Variant(
            name=f'entry_cap_{cap}',
            hypothesis=f'Late entries capped at ask <= {cap} leave room to cover spread',
            parameters={'max_entry_price': cap}
        )
        for cap in ('0.5', '0.6', '0.7', '0.75', '0.8')
    ]

    # Min time remaining: min_remaining_seconds in {30, 60, 120, 300, 600}
    grid['min_remaining'] = [
        Variant(
            name=f'min_remaining_{sec}s',
            hypothesis=f'Skip entries with < {sec}s remaining to avoid forced expiration',
            parameters={'min_remaining_seconds': sec}
        )
        for sec in (30, 60, 120, 300, 600)
    ]

    # Holding period short: {1, 2}
    grid['hold_short'] = [
        Variant(
            name=f'hold_{h}_slots',
            hypothesis=f'Shallow holding period ({h} slots) cuts losses faster',
            parameters={'holding_period': h}
        )
        for h in (1, 2)
    ]

    # Holding period long: {5, 10, 20}
    grid['hold_long'] = [
        Variant(
            name=f'hold_{h}_slots',
            hypothesis=f'Long holding period ({h} slots) allows convergence to resolution',
            parameters={'holding_period': h}
        )
        for h in (5, 10, 20)
    ]

    # Reversal confirmation
    grid['reversal_confirm'] = [
        Variant(
            name='reversal_confirm',
            hypothesis='Require 2 consecutive opposite signals before reversing position',
            parameters={'require_reversal_confirmation': True}
        )
    ]

    # Fee sensitivity: fee_bps in {5, 10, 20, 50}
    grid['fee_sensitivity'] = [
        Variant(
            name=f'fee_{bps}bps',
            hypothesis=f'Cost sensitivity evaluation at {bps} bps',
            parameters={'fee_bps': bps}
        )
        for bps in ('5', '10', '20', '50')
    ]

    # Liquidity sensitivity: depth_scale in {0.5, 1.0, 2.0}
    grid['liquidity_sensitivity'] = [
        Variant(
            name=f'depth_scale_{scale}',
            hypothesis=f'Liquidity sensitivity scaled by {scale}x book sizes',
            parameters={'depth_scale': scale}
        )
        for scale in (0.5, 1.0, 2.0)
    ]

    return grid


def get_variant(name: str) -> Variant:
    """Retrieve a single variant by name."""
    grid = get_variant_grid()
    if name in grid and isinstance(grid[name], Variant):
        return grid[name]
    for category, variants in grid.items():
        if isinstance(variants, list):
            for v in variants:
                if v.name == name:
                    return v
    raise KeyError(f'Unknown variant: {name}')


def evaluate_variants(
    windows,
    variants: list[Variant],
    *,
    train_only: bool = True,
    split=None,
) -> list[dict]:
    """Evaluate strategy variants across windows.

    Safety: defaults to train_only=True, which requires a chronological split
    and refuses (fails closed) any window that is not inside the train split.
    Pass train_only=False explicitly for holdout evaluation.
    """
    if train_only:
        if split is None:
            raise ValueError('split is required when train_only=True')
        train_ids = {w['market_id'] for w in split.train}
        for w in windows:
            if w.market_id not in train_ids:
                raise ValueError(f'window outside train split: {w.market_id}')

    results = []
    from .engine import run_replay

    for v in variants:
        # Merge variant parameters with baseline defaults
        baseline = get_variant('baseline')
        params = dict(baseline.parameters)
        params.update(v.parameters)

        reports = []
        for w in windows:
            report = run_replay(
                snapshots=w.snapshots,
                paper_recording=w.paper_recording,
                excluded=w.excluded,
                opportunities=w.opportunities,
                settlement_evidence=w.settlement_evidence,
                **params
            )
            reports.append(report)

        results.append({
            'variant': v.name,
            'hypothesis': v.hypothesis,
            'windows_evaluated': len(windows),
            'train_only': train_only,
            'reports': reports,
        })

    return results
