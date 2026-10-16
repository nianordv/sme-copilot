"""Score the frozen selected predictor at a new reporting date; never retrain.

Used by the local server as a fixed command with JSON on stdin/stdout. Private
financial records are not sent to a network service. The model retains its
training definition of customer history frozen at invoice issue.
"""

import contextlib
import io
import json
import sys

import numpy as np
import pandas as pd

from .score_open_invoices import (
    SOURCE,
    PREPARED,
    RUN,
    BASELINE,
    load_model,
    outstanding_at,
    validate_artifact,
    validate_curves,
)
from .prepare_data import load_invoices
from .evaluate_overdue import predict_model_curves, predict_baseline_curves
from .train import file_hash


def score_at(as_of, source_sha256):
    manifest_path = PREPARED / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if file_hash(SOURCE) != source_sha256 or manifest['source_sha256'] != source_sha256:
        raise ValueError('Live scoring source differs from the imported books.')
    base_date = pd.Timestamp(manifest['snapshot_date'])
    anchor = pd.Timestamp(as_of)
    if anchor < base_date:
        raise ValueError('The reporting date precedes the imported snapshot.')
    invoices = load_invoices(SOURCE)
    cohort = outstanding_at(invoices, base_date)
    anchors = pd.Series(anchor, index=cohort.index)
    model, metadata = load_model(RUN, manifest, manifest_path)
    if model is not None:
        curves = predict_model_curves(cohort, anchors, model, manifest)
        predictor = RUN.name
    else:
        path = BASELINE / 'hazards.npz'
        meta = json.loads((BASELINE / 'metadata.json').read_text())
        validate_artifact(meta, manifest, manifest_path, path, 'hazards_sha256')
        with np.load(path, allow_pickle=False) as baseline:
            curves = predict_baseline_curves(cohort, anchors, baseline)
        predictor = 'empirical_baseline'
    validate_curves(curves, len(cohort))
    return {
        'as_of': str(anchor.date()),
        'model_used': predictor,
        'fallback': model is None,
        'invoice_ids': cohort.invoice_id.tolist(),
        'curves': curves.tolist(),
    }


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            result = score_at(request['as_of'], request['source_sha256'])
        json.dump(result, sys.stdout, allow_nan=False)
    except Exception as problem:
        print(type(problem).__name__ + ': live scoring failed', file=sys.stderr)
        sys.exit(1)
