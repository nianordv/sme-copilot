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
    from backend.bank.db import has_table
    if denominator and has_table(conn, 'cash_entries'):
        for row in conn.execute(
            "SELECT e.amount_cents,e.direction,i.issue_date,COALESCE(o.event_date,e.event_date) paid_day "
            "FROM cash_entries e JOIN invoices i USING(invoice_id) LEFT JOIN cash_entries o ON o.entry_id=e.reversal_of "
            "WHERE e.event_date<=? AND i.issue_date BETWEEN ? AND ?",
            (snapshot.isoformat(), (snapshot-timedelta(days=149)).isoformat(), (snapshot-timedelta(days=60)).isoformat())):
            lag = (date.fromisoformat(row['paid_day'])-date.fromisoformat(row['issue_date'])).days
            if 1 <= lag <= 60:
                mass[lag] += row['amount_cents']/100/denominator * (1 if row['direction']=='in' else -1)
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


# Cached by date, base files and model fingerprint. Actual receipts alter the
# remaining amounts/cohort, not the frozen model's issue-date feature definition.
from functools import lru_cache
import os
import subprocess
import sys
import threading

_live_lock = threading.Lock()
ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=8)
def _score_date(as_of, source_hash, model_hash, manifest_hash):
    executable = os.environ.get('BILLBOARD_ML_PYTHON')
    if not executable:
        local = ROOT / 'ml/.venv/bin/python'
        executable = str(local) if local.exists() else sys.executable
    result = subprocess.run([executable, '-m', 'ml.live_score'], cwd=ROOT,
        input=json.dumps({'as_of':as_of, 'source_sha256':source_hash}), text=True,
        capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise ValueError('Cannot refresh payment curves. Install ml/requirements.txt and LightGBM 4.7.0 in the ML environment, and prepare the saved model/baseline.')
    data = json.loads(result.stdout)
    curves = np.asarray(data['curves'],dtype=float).reshape(len(data['invoice_ids']),60)
    if data['as_of'] != as_of or not np.isfinite(curves).all() or (curves<0).any() or (curves>1).any() or (np.diff(curves,axis=1)<0).any():
        raise ValueError('Invalid refreshed payment curves.')
    return data, curves


def current_curves(conn, folder, meta, invoices, as_of):
    base = conn.execute('SELECT * FROM invoices WHERE issue_date<=? AND (paid_date IS NULL OR paid_date>?) AND (writeoff_date IS NULL OR writeoff_date>?) ORDER BY invoice_id', (meta['snapshot_date'],)*3).fetchall()
    original, metadata = read_curves(folder, meta, base)
    identifiers = [row['invoice_id'] for row in base]
    if as_of.isoformat() != meta['snapshot_date']:
        model = ROOT / 'ml/artifacts/lightgbm/leaves_16_min_100/model.txt'
        manifest = ROOT / 'ml/artifacts/prepared/manifest.json'
        fingerprints = [model, model.with_name('metadata.json'), ROOT/'ml/artifacts/baseline/hazards.npz', ROOT/'ml/artifacts/baseline/metadata.json']
        model_hash = hashlib.sha256(b''.join(path.read_bytes() if path.exists() else b'missing' for path in fingerprints)).hexdigest()
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
        with _live_lock:
            data, original = _score_date(as_of.isoformat(),meta['csv_sha256'],model_hash,manifest_hash)
        identifiers = data['invoice_ids']
        if len(identifiers) != len(set(identifiers)) or set(identifiers) != {r['invoice_id'] for r in base}:
            raise ValueError('Refreshed invoice membership differs from the base books.')
        metadata = {**metadata, 'model_used':data['model_used'],'fallback':data['fallback']}
    index = {key:i for i,key in enumerate(identifiers)}
    return np.asarray([original[index[row['invoice_id']]] for row in invoices]).reshape(len(invoices),60), {**metadata,'prediction_as_of':as_of.isoformat()}
