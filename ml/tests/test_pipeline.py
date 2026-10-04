import unittest

import numpy as np
import pandas as pd

from ml.generate_invoices_ge import build_history_features, georgia_holidays
from ml.prepare_data import calendar_features, daily_rows, fit_categories, static_features


def fixture():
    inv = pd.DataFrame({
        "invoice_id": ["A", "B", "C", "D"],
        "client_id": ["CL1"] * 4,
        "client_segment": ["Cafe"] * 4, "client_city": ["Tbilisi"] * 4,
        "issue_date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-05", "2025-01-06"]),
        "due_date": pd.to_datetime(["2025-01-16", "2025-01-16", "2025-01-20", "2025-01-21"]),
        "paid_date": pd.to_datetime(["2025-01-03", None, "2025-01-10", None]),
        "writeoff_date": pd.to_datetime([None, "2025-01-05", None, None]),
        "amount": [100., 200., 300., 400.], "payment_terms": [15] * 4,
        "issue_month": [1] * 4,
    })
    inv["days_late"] = (inv.paid_date - inv.due_date).dt.days
    return inv


class PipelineTests(unittest.TestCase):
    def test_history_same_day_and_writeoff_visibility(self):
        h = build_history_features(fixture())
        self.assertEqual(h.client_other_open_invoices.tolist(), [0, 0, 1, 1])
        self.assertEqual(h.client_total_outstanding.tolist(), [0., 0., 200., 300.])
        self.assertEqual(h.client_num_past_invoices.tolist(), [0, 0, 1, 1])

    def test_history_is_row_order_invariant(self):
        inv = fixture()
        expected = build_history_features(inv).set_axis(inv.invoice_id)
        shuffled = inv.sample(frac=1, random_state=7).reset_index(drop=True)
        actual = build_history_features(shuffled).set_axis(shuffled.invoice_id)
        pd.testing.assert_frame_equal(expected.sort_index(), actual.sort_index())

    def test_future_events_cannot_change_issue_features(self):
        inv = fixture()
        before = build_history_features(inv)
        inv.loc[2, "paid_date"] = pd.Timestamp("2026-01-01")
        inv["days_late"] = (inv.paid_date - inv.due_date).dt.days
        pd.testing.assert_frame_equal(before, build_history_features(inv))

    def test_censoring_event_day_and_no_post_payment_rows(self):
        index, candidate, _, y = daily_rows(fixture(), "2025-01-07")
        self.assertEqual(y.sum(), 1)
        self.assertEqual(candidate[index == 0].max(), np.datetime64("2025-01-03"))
        self.assertTrue((candidate <= np.datetime64("2025-01-07")).all())
        self.assertEqual(y[index == 2].sum(), 0)  # Payment after cutoff is hidden.
        self.assertEqual((index == 3).sum(), 1)

    def test_tail_cap(self):
        inv = fixture().iloc[[1]].reset_index(drop=True)
        _, candidate, _, y = daily_rows(inv, "2027-01-01")
        self.assertEqual(candidate.max(), np.datetime64("2026-01-16"))
        self.assertEqual(y.sum(), 0)

    def test_holiday_history(self):
        from datetime import date
        self.assertNotIn(date(2023, 5, 17), georgia_holidays(2023))
        self.assertIn(date(2024, 5, 17), georgia_holidays(2024))

    def test_calendar_salary_rollover(self):
        f = calendar_features(pd.to_datetime(["2025-01-15", "2025-01-16", "2025-12-31"]), "candidate")
        self.assertEqual(f.candidate_days_to_salary_day.tolist(), [0, 30, 15])

    def test_predictor_exclusions_and_unknown_category(self):
        inv = fixture()
        history = build_history_features(inv)
        inv = pd.concat([inv, history], axis=1)
        categories = fit_categories(inv)
        inv.loc[0, "client_city"] = "Unseen city"
        f = static_features(inv, categories)
        self.assertEqual(f.client_city.iloc[0], -1)
        forbidden = {"invoice_id", "client_id", "status", "paid_date", "writeoff_date", "days_late", "paid_late", "days_to_pay"}
        self.assertFalse(forbidden.intersection(f.columns))
        self.assertFalse(any(c.startswith("paid_") for c in f.columns))


if __name__ == "__main__":
    unittest.main()
