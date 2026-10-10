"""
Synthetic invoice-payment dataset for a Georgian (GE) supplier selling to restaurants, cafes, hotels etc.
One row per invoice. Amounts are in GEL.  Run from repository root: python3 ml/generate_invoices_ge.py

COLUMN GUIDE
  identifiers : invoice_id, client_id, client_segment, client_city, issue_date, due_date, paid_date, writeoff_date, status
  invoice     : amount, payment_terms (15/30/60), issue_month, amount_vs_client_usual
  time (due date, known at issue):
                due_weekday (0=Mon), due_is_weekend, due_is_holiday (Georgian public holiday),
                due_is_month_end (last 3 days of month), due_is_salary_day (15th),
                due_days_to_salary_day, due_in_payroll_window (12th-15th),
                due_gap_to_business_day (days until banks are open again)
  client history (ONLY invoices already paid before this invoice was issued):
                client_num_past_invoices, client_avg_delay, client_median_delay, client_delay_spread,
                last_delay, avg_of_last_3_delays, delay_trend, client_pct_paid_late
  context (at issue date): client_other_open_invoices, client_total_outstanding
  payment-day calendar (OUTCOME side, do not use as predictors):
                paid_weekday, paid_is_weekend, paid_is_holiday, paid_is_month_end, paid_is_salary_day
  targets     : days_to_pay (= days since issued at payment), days_late (= days since due at payment),
                paid_late (1 if days_late > 0)
"""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import date, timedelta

import numpy as np
import pandas as pd
from dateutil.easter import easter, EASTER_ORTHODOX

SEED = 2026

TODAY = pd.Timestamp("2026-10-03")        # snapshot ("as of") date
START = pd.Timestamp("2022-01-03")
END = pd.Timestamp("2026-09-30")          # last invoice issue date
N_CLIENTS = 500
SALARY_DAY = 15                           # clients run payroll on the 15th
PAYROLL_WINDOW = (12, 15)                 # cash is tied up in payroll these days
PAYROLL_DELAY_DAYS = 3.0                  # extra delay if the due date sits in the payroll window
PAYROLL_PUSH_P = 0.30                     # chance a payment planned for the 12th-15th slips to the 16th
PAY_BEFORE_BREAK_P = 0.30                 # chance a payment due on a weekend/holiday is made the working day before
NONWORKING_DAY_PAY_P = 0.03               # conditional chance a planned nonworking-day payment stays there

OUT_DIR = Path(__file__).resolve().parent / "data"
WRITEOFF_AFTER_DAYS = 90  # Synthetic accounting policy; not a legal requirement.


# ------------------------------------------------------------------ Georgian calendar
def georgia_holidays(year):
    h = {
        date(year, 1, 1): "New Year's Day",
        date(year, 1, 2): "New Year holiday",
        date(year, 1, 7): "Orthodox Christmas",
        date(year, 1, 19): "Epiphany",
        date(year, 3, 3): "Mother's Day",
        date(year, 3, 8): "International Women's Day",
        date(year, 4, 9): "Day of National Unity",
        date(year, 5, 9): "Victory Day",
        date(year, 5, 12): "St. Andrew's Day",
        date(year, 5, 26): "Independence Day",
        date(year, 8, 28): "Mariamoba (St. Mary's Day)",
        date(year, 10, 14): "Svetitskhovloba",
        date(year, 11, 23): "St. George's Day",
    }
    if year >= 2024:
        h[date(year, 5, 17)] = "Day of Family Purity"
    e = easter(year, EASTER_ORTHODOX)
    h[e - timedelta(days=2)] = "Orthodox Good Friday"
    h[e - timedelta(days=1)] = "Orthodox Holy Saturday"
    h[e] = "Orthodox Easter"
    h[e + timedelta(days=1)] = "Orthodox Easter Monday"
    return h


HOLIDAYS = {}
for _y in range(2021, 2029):
    HOLIDAYS.update({pd.Timestamp(d): n for d, n in georgia_holidays(_y).items()})
HOLIDAY_LIST = list(HOLIDAYS)


def is_business_day(ts):
    return ts.weekday() < 5 and ts not in HOLIDAYS


def next_business_day(ts):
    while not is_business_day(ts):
        ts += pd.Timedelta(days=1)
    return ts


def prev_business_day(ts):
    while not is_business_day(ts):
        ts -= pd.Timedelta(days=1)
    return ts


# ------------------------------------------------------------------ client population
SEG = {
    "Restaurant":    dict(share=.30, mu=6.6, n=125, terms=[.30, .55, .15], shift=0.0),
    "Cafe":          dict(share=.25, mu=5.9, n=110, terms=[.45, .45, .10], shift=1.0),
    "Hotel":         dict(share=.15, mu=7.5, n=130, terms=[.10, .50, .40], shift=2.0),
    "Bar/Pub":       dict(share=.10, mu=6.1, n=100, terms=[.45, .45, .10], shift=1.0),
    "Catering":      dict(share=.08, mu=6.9, n=70,  terms=[.20, .55, .25], shift=0.0),
    "Grocery store": dict(share=.12, mu=7.0, n=170, terms=[.15, .55, .30], shift=-1.0),
}
SEG_NAMES = list(SEG)
SEG_P = np.array([SEG[s]["share"] for s in SEG_NAMES])
CITIES = ["Tbilisi", "Batumi", "Kutaisi", "Rustavi", "Telavi", "Zugdidi", "Gori"]
CITY_P = [.50, .16, .10, .07, .06, .05, .06]
CITY_P_HOTEL = [.35, .35, .10, .04, .10, .03, .03]
CITY_SHIFT = {"Tbilisi": 0, "Batumi": 1, "Kutaisi": 2, "Rustavi": 2, "Telavi": 1.5, "Zugdidi": 3, "Gori": 2}
TIERS = ["good", "average", "slow"]
TIER_P = [.35, .40, .25]
TIER_BASE = {"good": (-4, 2), "average": (0, 3), "slow": (8, 4)}      # mean/sd of typical delay (days)
TIER_CHRONIC = {"good": .01, "average": .03, "slow": .08}            # chance of a very late payment
TIER_WRITEOFF = {"good": .0003, "average": .001, "slow": .010}

def build_history_features(inv):
    """Features available before issue date; invariant to input row order."""
    original_index = inv.index
    inv = inv.reset_index(drop=True)
    # ------------------------------------------------------------------ client history + context features
    HIST_COLS = ["client_num_past_invoices", "client_avg_delay", "client_median_delay", "client_delay_spread",
                 "last_delay", "avg_of_last_3_delays", "delay_trend", "client_pct_paid_late",
                 "amount_vs_client_usual", "client_other_open_invoices", "client_total_outstanding"]
    res = np.full((len(inv), len(HIST_COLS)), np.nan)

    for cid, g in inv.sort_values(["issue_date", "invoice_id"]).groupby("client_id", sort=False):
        ix = g.index.to_numpy()                                            # g is already ordered by issue_date
        issue = g["issue_date"].to_numpy("datetime64[ns]")
        paid = g["paid_date"].to_numpy("datetime64[ns]")
        writeoff = g["writeoff_date"].to_numpy("datetime64[ns]")
        amt = g["amount"].to_numpy(float)
        late = g["days_late"].to_numpy(float)
        pool = np.where(~np.isnat(paid))[0]
        pool = pool[np.lexsort((g["invoice_id"].to_numpy()[pool], paid[pool]))]                 # paid invoices in order of payment
        pool_paid, pool_late = paid[pool], late[pool]
        k_arr = np.searchsorted(pool_paid, issue, side="left")             # how many were paid BEFORE each issue date
        for i in range(len(g)):
            r = res[ix[i]]
            k = int(k_arr[i])
            r[0] = k
            if k > 0:
                h = pool_late[:k]
                r[1], r[2] = h.mean(), np.median(h)
                if k >= 2:
                    r[3] = h.std(ddof=1)
                r[4], r[5] = h[-1], h[-3:].mean()
                if k >= 6:
                    r[6] = h[-3:].mean() - h[-6:-3].mean()
                r[7] = (h > 0).mean()
            # Date-only data: exclude every invoice issued on the same date.
            prior = issue < issue[i]
            if prior.sum() >= 3:
                r[8] = amt[i] / np.median(amt[prior])
            settled = (~np.isnat(paid) & (paid < issue[i]))
            closed = (~np.isnat(writeoff) & (writeoff < issue[i]))
            still_open = prior & ~settled & ~closed
            r[9], r[10] = still_open.sum(), amt[still_open].sum()

    hist = pd.DataFrame(res, columns=HIST_COLS, index=inv.index)
    for c_ in ["client_num_past_invoices", "client_other_open_invoices"]:
        hist[c_] = hist[c_].astype("Int64")
    hist.index = original_index
    return hist  # Keep full precision for ML; round only money at generation.



def generate(output_dir=OUT_DIR, seed=SEED):
    rng = np.random.default_rng(seed)
    OUT_DIR = Path(output_dir)
    total_days = (END - START).days
    clients = []
    for k in range(N_CLIENTS):
        seg = str(rng.choice(SEG_NAMES, p=SEG_P))
        city = str(rng.choice(CITIES, p=CITY_P_HOTEL if seg == "Hotel" else CITY_P))
        tier = str(rng.choice(TIERS, p=TIER_P))
        b_mu, b_sd = TIER_BASE[tier]
        if rng.random() < 0.70:
            a_start = START + pd.Timedelta(days=int(rng.integers(0, 60)))
        else:
            a_start = START + pd.Timedelta(days=int(rng.integers(60, total_days - 120)))
        a_end = END
        if rng.random() < 0.06:                                            # some clients churn
            a_end = min(END, a_start + pd.Timedelta(days=int(rng.integers(300, 1100))))
        clients.append(dict(
            client_id=f"CL{k + 1:04d}", segment=seg, city=city, tier=tier,
            base_delay=rng.normal(b_mu, b_sd) + SEG[seg]["shift"] + CITY_SHIFT[city],
            delay_sd=float(np.clip(rng.gamma(2.0, 2.2), 1.5, 14)) * (1.3 if tier == "slow" else 1.0),
            chronic_p=TIER_CHRONIC[tier],
            dispute_p=float(np.clip(rng.beta(1, 40), 0, 0.12)),
            drift=rng.normal(0, 1.5),                                      # behaviour drifts (days per year)
            autopay=(tier != "slow") and rng.random() < 0.12,
            scale=float(np.exp(rng.normal(0, 0.5))),                       # client's typical order size factor
            pref_terms=int(rng.choice([15, 30, 60], p=SEG[seg]["terms"])),
            writeoff_p=TIER_WRITEOFF[tier],
            a_start=a_start, a_end=a_end,
        ))

    # ------------------------------------------------------------------ invoices + payment behaviour
    rows = []
    for c in clients:
        seg = c["segment"]
        active_days = (c["a_end"] - c["a_start"]).days
        n_inv = max(3, int(rng.poisson(SEG[seg]["n"] * active_days / total_days * rng.uniform(0.6, 1.4))))
        issue_dates = c["a_start"] + pd.to_timedelta(rng.integers(0, active_days + 1, n_inv), unit="D")
        typical = SEG[seg]["mu"] + np.log(c["scale"])                      # log of client's typical amount
        for d in issue_dates:
            d = next_business_day(d)                                       # invoices are issued on working days
            if d > END:
                continue
            terms = c["pref_terms"] if rng.random() < 0.85 else int(rng.choice([15, 30, 60]))
            amount = max(20.0, round(float(rng.lognormal(typical, 0.55)), 2))
            due = d + pd.Timedelta(days=terms)

            # ---- intended delay vs due date (days)
            years_in = (d - START).days / 365.25
            mu = c["base_delay"] + c["drift"] * years_in
            mu += 2.0 * float(np.clip(np.log(amount) - typical, -1.5, 1.5))   # unusually big invoice -> slower
            mu += 1.0 * np.log10(amount / 800)
            mu += 0.04 * (terms - 30)
            m = due.month
            if seg == "Hotel":                                              # tourist season = cash-rich
                mu += -3 if 6 <= m <= 9 else (4 if m in (11, 12, 1, 2, 3) else 0)
            elif seg in ("Restaurant", "Cafe", "Bar/Pub"):
                mu += {1: 3, 2: 2, 12: -1}.get(m, 0)
            if PAYROLL_WINDOW[0] <= due.day <= PAYROLL_WINDOW[1]:
                mu += PAYROLL_DELAY_DAYS                                    # payroll comes first
            if due.day >= due.days_in_month - 2:
                mu += 1.5                                                   # month-end crunch
            if c["autopay"]:
                delay = rng.normal(-1, 0.8)
            else:
                delay = rng.normal(mu, c["delay_sd"])
                if rng.random() < c["chronic_p"]:
                    delay += rng.exponential(25)
                if rng.random() < c["dispute_p"] * (1.5 if amount > 5000 else 1.0):
                    delay += rng.uniform(20, 90)
            delay = max(int(round(delay)), -15)
            paid = max(due + pd.Timedelta(days=delay), d + pd.Timedelta(days=1))

            # ---- calendar effects
            if (not c["autopay"]) and PAYROLL_WINDOW[0] <= paid.day <= PAYROLL_WINDOW[1] and rng.random() < PAYROLL_PUSH_P:
                paid = paid.replace(day=SALARY_DAY + 1)                     # salaries first, suppliers on the 16th
            if not is_business_day(paid) and rng.random() > NONWORKING_DAY_PAY_P:
                # nobody works weekends/holidays: some clients clear the bill before the break, most pay after it
                before = prev_business_day(paid)
                if rng.random() < PAY_BEFORE_BREAK_P and before > d:
                    paid = before
                else:
                    paid = next_business_day(paid)
                    if rng.random() < 0.35:                                 # backlog after the break
                        paid = next_business_day(paid + pd.Timedelta(days=1))
            written_off = rng.random() < c["writeoff_p"]
            rows.append((c["client_id"], c["segment"], c["city"], d, due, terms, amount, paid, written_off))

    inv = pd.DataFrame(rows, columns=["client_id", "client_segment", "client_city", "issue_date", "due_date",
                                      "payment_terms", "amount", "paid_date", "written_off"])
    inv = inv.sort_values(["issue_date", "client_id"], kind="stable").reset_index(drop=True)
    inv.insert(0, "invoice_id", [f"INV-{i + 1:06d}" for i in range(len(inv))])

    # A latent never-payment draw is private simulation state, never a predictor.
    inv["writeoff_date"] = inv.due_date + pd.Timedelta(days=WRITEOFF_AFTER_DAYS)
    inv.loc[~inv.written_off | (inv.writeoff_date > TODAY), "writeoff_date"] = pd.NaT
    inv.loc[inv.written_off | (inv.paid_date > TODAY), "paid_date"] = pd.NaT
    inv["status"] = np.where(inv.paid_date.notna(), "paid",
                    np.where(inv.writeoff_date.notna(), "written_off",
                    np.where(inv.due_date >= TODAY, "open_not_due", "open_overdue")))
    inv["days_to_pay"] = (inv.paid_date - inv.issue_date).dt.days
    inv["days_late"] = (inv.paid_date - inv.due_date).dt.days
    inv["paid_late"] = np.where(inv.paid_date.notna(), (inv.days_late > 0).astype(float), np.nan)
    inv["issue_month"] = inv.issue_date.dt.month


    # ------------------------------------------------------------------ calendar features
    def calendar_features(s, p):
        ok = s.notna()
        wd = s.dt.weekday
        f = pd.DataFrame({
            f"{p}_weekday": wd,
            f"{p}_is_weekend": (wd >= 5).astype(float),
            f"{p}_is_holiday": s.isin(HOLIDAY_LIST).astype(float),
            f"{p}_is_month_end": (s.dt.day >= s.dt.days_in_month - 2).astype(float),
            f"{p}_is_salary_day": (s.dt.day == SALARY_DAY).astype(float),
        })
        f.loc[~ok] = np.nan
        return f.astype("Int64")


    def days_to_salary_day(ts):
        if ts.day <= SALARY_DAY:
            return SALARY_DAY - ts.day
        nxt = (ts + pd.offsets.MonthBegin(1)).replace(day=SALARY_DAY)
        return (nxt - ts).days


    due_cal = calendar_features(inv.due_date, "due")
    due_cal["due_days_to_salary_day"] = inv.due_date.map(days_to_salary_day)
    due_cal["due_in_payroll_window"] = inv.due_date.dt.day.between(*PAYROLL_WINDOW).astype(int)
    gap_map = {u: (next_business_day(u) - u).days for u in inv.due_date.unique()}
    due_cal["due_gap_to_business_day"] = inv.due_date.map(gap_map)
    paid_cal = calendar_features(inv.paid_date, "paid")

    hist = build_history_features(inv)

    # ------------------------------------------------------------------ assemble + save
    out = pd.concat([
        inv[["invoice_id", "client_id", "client_segment", "client_city", "issue_date", "due_date", "paid_date", "writeoff_date", "status",
             "amount", "payment_terms", "issue_month"]],
        hist[["amount_vs_client_usual"]],
        due_cal,
        hist[["client_num_past_invoices", "client_avg_delay", "client_median_delay", "client_delay_spread",
              "last_delay", "avg_of_last_3_delays", "delay_trend", "client_pct_paid_late",
              "client_other_open_invoices", "client_total_outstanding"]],
        paid_cal,
        inv[["days_to_pay", "days_late", "paid_late"]].astype({"days_to_pay": "Int64", "days_late": "Int64"}),
    ], axis=1)
    out["paid_late"] = out["paid_late"].astype("Int64")
    for col in ["issue_date", "due_date", "paid_date", "writeoff_date"]:
        out[col] = out[col].dt.date

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "invoices_georgia.csv"
    out.to_csv(path, index=False)

    paid_rows = out[out.status == "paid"]
    print(f"{len(out)} invoices, {out.client_id.nunique()} clients, {out.shape[1]} columns -> {path}")
    print(out.status.value_counts().to_string())
    print("late rate (paid):", round(paid_rows.paid_late.astype(float).mean(), 3),
          "| median days_late:", paid_rows.days_late.astype(float).median())

    metadata = {
        "generator_version": 2, "seed": seed, "snapshot_date": str(TODAY.date()),
        "snapshot_semantics": "end_of_day", "currency": "GEL", "synthetic": True,
        "writeoff_after_due_days": WRITEOFF_AFTER_DAYS,
        "rows": len(out), "clients": int(out.client_id.nunique()),
        "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "versions": {"numpy": np.__version__, "pandas": pd.__version__},
        "limitations": ["One simulated supplier", "Full payments only",
                        "No shared economic shocks", "Payroll dates are assumptions",
                        "Not calibrated against real Georgian payment records"],
    }
    (path.parent / "dataset_metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate synthetic Georgian invoices (no real customer data).")
    parser.add_argument("--output-dir", type=Path, default=OUT_DIR)
    generate(parser.parse_args().output_dir)
