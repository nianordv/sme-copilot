"""Check the saved forecast's cohort, probability math and fallback."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from ml import score_open_invoices as scoring
from ml.evaluate_overdue import (
    predict_baseline, predict_baseline_curves, predict_model, predict_model_curves,
)
from ml.prepare_data import fit_categories, static_features, calendar_features
from ml.generate_invoices_ge import build_history_features
from ml.tests.test_pipeline import fixture


class ConstantHazardModel:
    def __init__(self, names):
        self.names = names

    def feature_name(self):
        return self.names

    def predict(self, x, num_threads):
        return np.full(len(x), 0.1)


class ForecastTests(unittest.TestCase):
    def test_snapshot_cohort_excludes_paid_written_off_and_future_invoices(self):
        invoices = fixture()
        cohort = scoring.outstanding_at(invoices, pd.Timestamp("2025-01-05"))
        self.assertEqual(cohort.invoice_id.tolist(), ["C"])
        # A payment later than the anchor does not remove the invoice early.
        self.assertEqual(scoring.outstanding_at(invoices, pd.Timestamp("2025-01-02")).invoice_id.tolist(), ["A", "B"])

    def test_model_curves_follow_conditional_hazard_math(self):
        invoices = fixture()
        for column, values in build_history_features(invoices).items():
            invoices[column] = values
        categories = fit_categories(invoices)
        names = list(static_features(invoices, categories).columns)
        names += ["invoice_age", "days_past_due", *calendar_features(pd.to_datetime(["2025-01-06"]), "candidate").columns]
        manifest = {"categories": categories, "feature_names": names}
        model = ConstantHazardModel(names)
        anchors = pd.Series(pd.Timestamp("2025-01-06"), index=invoices.index)
        curves = predict_model_curves(invoices, anchors, model, manifest)
        scoring.validate_curves(curves, len(invoices))
        np.testing.assert_allclose(curves[0], 1 - 0.9 ** np.arange(1, 61))
        horizons = predict_model(invoices, anchors, model, manifest)
        for horizon, values in horizons.items():
            np.testing.assert_array_equal(values, curves[:, horizon - 1])
        empty = predict_model_curves(invoices.iloc[:0], anchors.iloc[:0], model, manifest)
        self.assertEqual(empty.shape, (0, 60))

    def test_baseline_curves_use_invoice_age_and_unknown_client_fallback(self):
        invoices = fixture().iloc[[0, 2]].reset_index(drop=True)
        invoices.loc[1, "client_id"] = "new"
        anchors = pd.Series(pd.Timestamp("2025-01-06"), index=invoices.index)
        baseline = {"terms": np.array([15]), "clients": np.array(["CL1"]),
                    "term_hazard": np.full((1, 100), 0.2),
                    "client_hazard": np.full((1, 1, 100), 0.1)}
        curves = predict_baseline_curves(invoices, anchors, baseline)
        np.testing.assert_allclose(curves[0], 1 - 0.9 ** np.arange(1, 61))
        np.testing.assert_allclose(curves[1], 1 - 0.8 ** np.arange(1, 61))
        for horizon, values in predict_baseline(invoices, anchors, baseline).items():
            np.testing.assert_array_equal(values, curves[:, horizon - 1])
        self.assertEqual(predict_baseline_curves(invoices.iloc[:0], anchors.iloc[:0], baseline).shape, (0, 60))

    def test_invalid_curves_are_rejected(self):
        for curves in (np.full((1, 60), np.nan), np.full((1, 60), 1.1),
                       np.full((1, 60), -0.1), np.linspace(1, 0, 60)[None, :], np.zeros((1, 59))):
            with self.assertRaises(ValueError):
                scoring.validate_curves(curves, 1)

    def test_missing_model_falls_back_but_changed_model_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            self.assertEqual(scoring.load_model(run, {}, run / "manifest.json"), (None, None))
            (run / "model.txt").write_text("changed")
            (run / "metadata.json").write_text(json.dumps({
                "source_sha256": "old", "prepared_manifest_sha256": "old", "model_sha256": "old"}))
            with self.assertRaises(ValueError):
                scoring.load_model(run, {"source_sha256": "new"}, run / "manifest.json")

    def test_saved_table_membership_and_baseline_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invoices.csv"
            source.write_text("fixture")
            source_hash = scoring.file_hash(source)
            dataset = {"csv_sha256": source_hash, "snapshot_date": "2025-01-05", "currency": "GEL", "synthetic": True}
            (root / "dataset_metadata.json").write_text(json.dumps(dataset))
            (root / "manifest.json").write_text(json.dumps({"source_sha256": source_hash, "snapshot_date": "2025-01-05"}))
            baseline_dir = root / "baseline"
            baseline_dir.mkdir()
            baseline_path = baseline_dir / "hazards.npz"
            np.savez(baseline_path, terms=np.array([15]), clients=np.array(["CL1"]),
                     term_hazard=np.full((1, 100), 0.1), client_hazard=np.full((1, 1, 100), 0.1))
            (baseline_dir / "metadata.json").write_text(json.dumps({
                "source_sha256": source_hash, "prepared_manifest_sha256": scoring.file_hash(root / "manifest.json"),
                "hazards_sha256": scoring.file_hash(baseline_path), "fitted_partition": "train"}))
            invoices = fixture().iloc[:3].copy()
            with patch.object(scoring, "load_invoices", return_value=invoices):
                # Snapshot exports cannot contain future payment events.
                with self.assertRaises(ValueError):
                    scoring.score(source, root, root / "missing", baseline_dir, root / "forecast")
                invoices.loc[invoices.paid_date > pd.Timestamp("2025-01-05"), "paid_date"] = pd.NaT
                table, metadata = scoring.score(source, root, root / "missing", baseline_dir, root / "forecast")
            self.assertEqual(table.invoice_id.tolist(), ["C"])
            self.assertTrue(metadata["fallback"])
            self.assertEqual(metadata["invoice_count"], 1)
            saved = pd.read_csv(root / "forecast" / "open_invoice_curves.csv")
            self.assertEqual(saved.invoice_id.tolist(), ["C"])
            self.assertEqual(saved.shape, (1, 63))
            scoring.validate_curves(saved.filter(regex=r"^day_").to_numpy(), 1)
            self.assertAlmostEqual(metadata["expected_receipts_30d"], 300 * (1 - 0.9 ** 30))


if __name__ == "__main__":
    unittest.main()
