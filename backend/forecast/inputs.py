"""Read B4 curves and estimate a separately disclosed future-sales assumption."""

import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np

FORECAST = Path(__file__).resolve().parents[2] / 'ml/artifacts/forecast'


def read_curves(folder, meta, invoices):
    folder = Path(folder)
    metadata = json.loads((folder / 'metadata.json').read_text())
    path = folder / 'open_invoice_curves.csv'
    if metadata['snapshot_date'] != meta['snapshot_date'] or metadata['source_csv_sha256'] != meta['csv_sha256']:
        raise ValueError('Forecast and database disagree. Rebuild the books and B4 forecast.')
    if metadata['currency'] != meta['currency'] or metadata['horizon_days'] != 60:
        raise ValueError('Forecast currency or horizon does not match the books.')
    if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['forecast_csv_sha256']:
        raise ValueError('Saved forecast changed. Run python -m ml.score_open_invoices.')
    with path.open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    by_id = {row['invoice_id']: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != {row['invoice_id'] for row in invoices}:
        raise ValueError('Forecast invoice membership does not match outstanding books.')
    if metadata['invoice_count'] != len(rows):
        raise ValueError('Forecast row count does not match its metadata.')
    curves = []
    for invoice in invoices:
        row = by_id[invoice['invoice_id']]
        if row['client_id'] != invoice['customer_id'] or abs(float(row['amount']) - invoice['amount']) > 0.005:
            raise ValueError('Forecast customer or invoice amount differs from the ledger.')
        curves.append([float(row[f'day_{day}']) for day in range(1, 61)])
    return np.array(curves, dtype=float).reshape(len(invoices), 60), metadata


def future_sales(conn, snapshot, days):
    """Use recent sales and fully followed historical cohorts, not future events.

    A 90-day cohort ending 60 days ago avoids treating recent unpaid invoices
    as permanent failures. Only its payments within 60 days contribute to the
    delay distribution; remaining mass stays unpaid within our 60-day forecast.
    """
    start = (snapshot - timedelta(days=89)).isoformat()
    sales = conn.execute('SELECT COALESCE(SUM(amount), 0) FROM invoices WHERE issue_date BETWEEN ? AND ?',
                         (start, snapshot.isoformat())).fetchone()[0]
    daily_sales = float(sales) / 90
    history = conn.execute(
        'SELECT issue_date, paid_date, amount FROM invoices WHERE issue_date BETWEEN ? AND ?',
        ((snapshot - timedelta(days=149)).isoformat(), (snapshot - timedelta(days=60)).isoformat())).fetchall()
    denominator = sum(row['amount'] for row in history)
    mass = np.zeros(61)
    if denominator:
        for row in history:
            if row['paid_date'] is None:
                continue
            lag = (date.fromisoformat(row['paid_date']) - date.fromisoformat(row['issue_date'])).days
            if 1 <= lag <= 60:
                mass[lag] += row['amount'] / denominator
    # Constant expected sales on future days 1..N, shifted by payment delay.
    receipts = daily_sales * np.cumsum(mass)[np.arange(days)]
    return receipts.tolist(), {
        'method': 'Constant average daily sales over the last 90 days, shifted by historical payment delays.',
        'average_daily_sales': round(daily_sales, 2),
        'delay_cohort_invoices': len(history),
        'historical_paid_within_60d_share': round(float(mass.sum()), 4),
        'expected_receipts': round(float(receipts.sum()), 2),
        'warning': 'Assumption, not ML output. Future sales and their uncertainty are not simulated; excluded from the no-payment case.',
    }
