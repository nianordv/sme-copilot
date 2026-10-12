"""Calculate cash paths without changing the books or executing payments."""

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

import numpy as np

SIMULATIONS = 2000
SEED = 2026


def cents(value):
    number = Decimal(str(value))
    if not number.is_finite() or abs(number) > Decimal('1000000000000'):
        raise ValueError('Invalid monetary amount.')
    return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def summarize(path, snapshot, checkpoints):
    lowest = int(np.argmin(path))
    negative = np.flatnonzero(path < 0)
    first_spell_days = 0
    if len(negative):
        for balance in path[negative[0]:]:
            if balance >= 0:
                break
            first_spell_days += 1
    dates = lambda index: (snapshot + timedelta(days=int(index) + 1)).isoformat()
    weeks = sorted(set([*range(6, len(path), 7), len(path) - 1]))
    return {
        'lowest_balance': round(float(path[lowest]) / 100, 2),
        'lowest_date': dates(lowest),
        'first_day_below_zero': dates(negative[0]) if len(negative) else None,
        'shortfall': round(max(0, -float(path[lowest])) / 100, 2),
        'days_below_zero': int(len(negative)),
        'first_shortfall_duration_days': first_spell_days,
        'obligation_balances': [
            {'date': dates(index), 'label': label, 'balance': round(float(path[index]) / 100, 2)}
            for index, label in checkpoints
        ],
        'weekly_balances': [{'date': dates(index), 'balance': round(float(path[index]) / 100, 2)} for index in weeks],
    }


def calculate(snapshot, days, opening_balance, amounts, curves, outflows,
              future_receipts, paid_indices=()):
    """Pointwise median/10th percentile; neither is a whole-path guarantee."""
    if type(days) is not int or not 1 <= days <= 60:
        raise ValueError('Forecast days must be between 1 and 60.')
    curves = np.asarray(curves, dtype=float)
    if curves.shape != (len(amounts), 60) or not np.isfinite(curves).all():
        raise ValueError('Invalid payment-curve dimensions or values.')
    if (curves < 0).any() or (curves > 1).any() or (np.diff(curves, axis=1) < 0).any():
        raise ValueError('Payment curves must increase from zero toward one.')
    amounts = np.array([cents(value) for value in amounts], dtype=np.int64)
    if (amounts <= 0).any():
        raise ValueError('Invoice amounts must be positive.')
    future = np.array([cents(value) for value in future_receipts], dtype=np.int64)
    if len(future) != days or (future < 0).any():
        raise ValueError('Invalid future-invoice receipts.')
    initial = cents(opening_balance)
    paid = set(paid_indices)
    if any(type(index) is not int or not 0 <= index < len(amounts) for index in paid):
        raise ValueError('Invalid assumed-paid invoice index.')
    initial += sum(int(amounts[index]) for index in paid)
    costs = np.zeros(days, dtype=np.int64)
    checkpoints = []
    for payment in outflows:
        offset = (payment['date'] - snapshot).days - 1
        amount = cents(payment['amount'])
        if not 0 <= offset < days or amount < 0:
            raise ValueError('Outflows must be nonnegative and inside the forecast window.')
        costs[offset] += amount
        if payment.get('category') in {'salaries', 'taxes'}:
            checkpoints.append((offset, payment['label']))
    receipts = np.zeros((SIMULATIONS, days), dtype=np.int64)
    rng = np.random.default_rng(SEED)
    samples = np.arange(SIMULATIONS)
    for index, amount in enumerate(amounts):
        if index in paid:
            continue
        # A draw beyond day_60 means no payment within the forecast window.
        payment_day = np.searchsorted(curves[index], rng.random(SIMULATIONS), side='right')
        inside = payment_day < days
        np.add.at(receipts, (samples[inside], payment_day[inside]), amount)
    paths = initial + np.cumsum(receipts + future - costs, axis=1)
    likely = np.quantile(paths, 0.5, axis=0)
    cautious = np.quantile(paths, 0.1, axis=0)
    worst = initial - np.cumsum(costs)  # No remaining or future invoice pays.
    return {
        'likely': summarize(likely, snapshot, checkpoints),
        'cautious': summarize(cautious, snapshot, checkpoints),
        'worst': summarize(worst, snapshot, checkpoints),
        'likely_cautious_gap_at_end': round(float(likely[-1] - cautious[-1]) / 100, 2),
        'maximum_likely_cautious_gap': round(float(np.max(likely - cautious)) / 100, 2),
    }
