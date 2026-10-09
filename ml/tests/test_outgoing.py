import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from ml.generate_invoices_ge import END, START, TODAY, is_business_day
from ml.generate_outgoing_invoices_ge import (CAT, COLUMNS, INVOICES_CSV, META_NAME, OUT_NAME, OVERHEAD_SHARE,
                                              STOCK_SHARE, generate, sales_profile)


def sales_csv(folder, daily_amount=lambda day: 30000.0):
    """A stand-in for the incoming CSV: one sale on every working day."""
    days = [d for d in pd.date_range(START, END, freq="D") if is_business_day(d)]
    path = Path(folder) / "sales.csv"
    pd.DataFrame({"issue_date": [d.date() for d in days], "amount": [daily_amount(d) for d in days]}).to_csv(path, index=False)
    return path


def run(folder, sales):
    with contextlib.redirect_stdout(io.StringIO()):
        generate(folder, sales)
    return pd.read_csv(Path(folder) / OUT_NAME, parse_dates=["issue_date", "due_date", "paid_date"])


class OutgoingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        cls.sales = sales_csv(cls.folder)
        cls.out = run(cls.folder, cls.sales)
        cls.total_sales = pd.read_csv(cls.sales).amount.sum()

    def test_columns_and_ids(self):
        self.assertEqual(list(self.out.columns), COLUMNS)
        self.assertTrue(self.out.invoice_id.is_unique)
        self.assertTrue(self.out.invoice_id.str.fullmatch(r"BILL-\d{6}").all())
        self.assertTrue(self.out.issue_date.is_monotonic_increasing)

    def test_dates_are_consistent(self):
        out = self.out
        self.assertTrue((out.due_date == out.issue_date + pd.to_timedelta(out.payment_terms, unit="D")).all())
        self.assertTrue(out.issue_date.between(START, END).all())
        self.assertTrue(out.issue_date.map(is_business_day).all())
        paid = out.dropna(subset=["paid_date"])
        self.assertTrue((paid.paid_date > paid.issue_date).all())
        self.assertTrue(paid.paid_date.map(is_business_day).all())   # Banks do not move money on days off.

    def test_nothing_after_the_snapshot_is_known(self):
        self.assertTrue((self.out.paid_date.dropna() <= TODAY).all())

    def test_status_follows_from_the_dates(self):
        out = self.out
        unpaid = out.paid_date.isna()
        self.assertTrue((out.status[~unpaid] == "paid").all())
        self.assertTrue((out.status[unpaid & (out.due_date >= TODAY)] == "open_not_due").all())
        self.assertTrue((out.status[unpaid & (out.due_date < TODAY)] == "open_overdue").all())
        self.assertEqual(set(out.status), {"paid", "open_not_due", "open_overdue"})

    def test_amounts_are_money(self):
        self.assertTrue((self.out.amount >= 20).all())
        self.assertTrue(((self.out.amount * 100).round() / 100 == self.out.amount).all())

    def test_each_category_is_its_share_of_sales(self):
        billed = self.out.groupby("supplier_category").amount.sum()
        for cat, c in CAT.items():
            self.assertAlmostEqual(billed[cat] / self.total_sales, c["share"], places=4, msg=cat)
        by_type = self.out.groupby("expense_type").amount.sum() / self.total_sales
        self.assertAlmostEqual(by_type["stock"], STOCK_SHARE, places=3)
        self.assertAlmostEqual(by_type["overhead"], OVERHEAD_SHARE, places=3)

    def test_supplier_keeps_one_category_city_and_terms(self):
        per_supplier = self.out.groupby("supplier_id")[["supplier_category", "supplier_city", "payment_terms"]].nunique()
        self.assertEqual(per_supplier.max().tolist(), [1, 1, 1])
        self.assertEqual(self.out.supplier_id.nunique(), sum(c["n"] for c in CAT.values()))
        self.assertLessEqual(set(self.out.payment_terms), {15, 30, 60})

    def test_bills_are_not_piled_onto_mondays(self):
        monday = (self.out[self.out.expense_type == "stock"].issue_date.dt.weekday == 0).mean()
        self.assertLess(monday, 0.25)

    def test_rent_and_utilities_are_never_paid_late(self):
        strict = self.out[self.out.supplier_category.isin(["Rent", "Utilities"])].dropna(subset=["paid_date"])
        self.assertTrue((strict.paid_date <= strict.due_date).all())

    def test_one_rent_bill_a_month(self):
        rent = self.out[self.out.supplier_category == "Rent"]
        self.assertEqual(rent.issue_date.dt.to_period("M").value_counts().max(), 1)
        self.assertEqual(len(rent), len(pd.period_range(START, END, freq="M")))

    def test_bills_due_around_own_salaries_and_taxes_are_paid_later(self):
        paid = self.out[~self.out.supplier_category.isin(["Rent", "Utilities"])].dropna(subset=["paid_date"])
        late = (paid.paid_date - paid.due_date).dt.days
        tight = paid.due_date.dt.day.between(8, 15)
        self.assertGreater(late[tight].mean() - late[~tight].mean(), 1.5)

    def test_most_payments_leave_in_the_twice_weekly_batches(self):
        paid = self.out[~self.out.supplier_category.isin(["Rent", "Utilities"])].dropna(subset=["paid_date"])
        self.assertGreater(paid.paid_date.dt.weekday.isin([1, 4]).mean(), 0.75)

    def test_same_inputs_give_the_same_file(self):
        again = Path(tempfile.mkdtemp())
        run(again, self.sales)
        first, second = (self.folder / OUT_NAME).read_bytes(), (again / OUT_NAME).read_bytes()
        self.assertEqual(first, second)

    def test_metadata_describes_this_file_and_its_source(self):
        metadata = json.loads((self.folder / META_NAME).read_text())
        self.assertEqual(metadata["csv_sha256"], hashlib.sha256((self.folder / OUT_NAME).read_bytes()).hexdigest())
        self.assertEqual(metadata["incoming_csv_sha256"], hashlib.sha256(self.sales.read_bytes()).hexdigest())
        self.assertEqual((metadata["rows"], metadata["snapshot_date"]), (len(self.out), str(TODAY.date())))

    def test_incoming_files_are_left_alone(self):
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), sorted([OUT_NAME, META_NAME, "sales.csv"]))


class FollowsSalesTests(unittest.TestCase):
    def test_purchases_double_when_sales_double(self):
        folder = Path(tempfile.mkdtemp())
        halfway = START + (END - START) / 2
        out = run(folder, sales_csv(folder, lambda day: 20000.0 if day < halfway else 40000.0))
        stock = out[out.expense_type == "stock"]
        early, late = stock[stock.issue_date < halfway].amount.sum(), stock[stock.issue_date >= halfway].amount.sum()
        self.assertAlmostEqual(late / early, 2.0, delta=0.2)
        # Rent does not follow sales. It only rises a little each year.
        rent = out[out.supplier_category == "Rent"].sort_values("issue_date").amount
        self.assertLess(rent.iloc[-1] / rent.iloc[0], 1.3)

    def test_busy_weeks_average_out_to_one(self):
        folder = Path(tempfile.mkdtemp())
        total, busy = sales_profile(sales_csv(folder))
        self.assertAlmostEqual(busy.mean(), 1.0, places=6)
        self.assertEqual(total, pd.read_csv(folder / "sales.csv").amount.sum())


@unittest.skipUnless(INVOICES_CSV.exists(), "generate the incoming invoices first: python -m ml.generate_invoices_ge")
class RealSalesTests(unittest.TestCase):
    def test_bills_are_the_stated_share_of_real_sales(self):
        out = run(Path(tempfile.mkdtemp()), INVOICES_CSV)
        sales = pd.read_csv(INVOICES_CSV, usecols=["amount"]).amount.sum()
        self.assertAlmostEqual(out.amount.sum() / sales, STOCK_SHARE + OVERHEAD_SHARE, places=3)


if __name__ == "__main__":
    unittest.main()
