"""Read-only risk, invoice probabilities and the owner's workspace."""


from backend.bank.db import connect, reporting_date, has_table
from backend.bank import ledger
from backend.actions.lookups import open_invoices, by_customer, largest
from backend.forecast.inputs import current_curves, FORECAST

CONCENTRATION_THRESHOLD = 25.0  # Product policy, not a learned or universal cutoff.


def risk_summary(conn):
    as_of = reporting_date(conn)
    invoices = open_invoices(conn, as_of)
    total = sum(ledger.cents(r['amount']) for r in invoices)
    customers = largest(by_customer(invoices), 'owed', 10)
    for row in customers:
        row['share_percent'] = round(ledger.cents(row['owed']) / total * 100, 2) if total else 0
        row['concentration_warning'] = row['share_percent'] >= CONCENTRATION_THRESHOLD
    overdue = [r for r in ledger.obligations(conn) if r['due_date'] <= as_of.isoformat()]
    warnings = [
        f"{r['customer']} holds {r['share_percent']:.2f}% of outstanding receivables."
        for r in customers
        if r['concentration_warning']
    ]
    if overdue:
        warnings.append(
            f"Unpaid obligations total GEL {sum(r['amount'] for r in overdue):,.2f}; the forecast assumes payment tomorrow."
        )
    return {
        'as_of': as_of.isoformat(),
        'outstanding_amount': ledger.money(total),
        'top_customers': customers,
        'concentration_threshold_percent': CONCENTRATION_THRESHOLD,
        'policy_note': '25% is a demo policy flag, not a probability of default.',
        'warnings': warnings,
    }


def payment_view(conn, limit=100):
    as_of = reporting_date(conn)
    meta = dict(conn.execute('SELECT key,value FROM meta').fetchall())
    invoices = open_invoices(conn, as_of)
    curves, metadata = current_curves(conn, FORECAST, meta, invoices, as_of)
    rows = []
    for i, row in enumerate(invoices):
        rows.append(
            {
                **row,
                'probabilities': {
                    str(d): round(float(curves[i, d - 1]) * 100, 2) for d in (7, 14, 30, 60)
                },
                'expected_receipts_30d': round(row['amount'] * float(curves[i, 29]), 2),
            }
        )
    rows.sort(key=lambda r: (-r['amount'], r['invoice_id']))
    return {
        'available': True,
        'as_of': as_of.isoformat(),
        'predictor': metadata['model_used'],
        'fallback': metadata['fallback'],
        'items': rows[:limit],
        'not_listed': max(0, len(rows) - limit),
        'interpretation': 'Invoice payment probability conditional on remaining unpaid. For partial invoices the same timing curve is applied to the remaining amount; that assumption is not separately validated.',
    }


def evaluate_alerts(conn, forecast):
    if not has_table(conn, 'alerts'):
        return []
    as_of = reporting_date(conn).isoformat()
    cash = ledger.balance(conn)['balance']
    result = []
    for row in conn.execute('SELECT * FROM alerts ORDER BY alert_id DESC LIMIT 20'):
        reasons = []
        if row['alert_date'] and row['alert_date'] <= as_of:
            reasons.append('The alert date has been reached.')
        if row['balance_below'] is not None:
            if cash < row['balance_below']:
                reasons.append('Recorded cash is below the threshold.')
            elif forecast and forecast['cautious']['lowest_balance'] < row['balance_below']:
                reasons.append('The cautious forecast falls below the threshold.')
        result.append({'alert_id': row['alert_id'], 'triggered': bool(reasons), 'reasons': reasons})
    return result


def workspace():
    with connect() as conn:
        conn.execute('BEGIN')
        result = {'ledger': ledger.read_ledger(conn), 'risk': risk_summary(conn)}
        try:
            result["payments"] = payment_view(conn, 2000)
        except Exception:
            result['payments'] = {
                'available': False,
                'reason': 'Payment probabilities unavailable. Check the saved forecast and ML environment; the cash records remain available.',
            }
    return result


def get_risk():
    with connect() as conn:
        return risk_summary(conn)


def get_payment_probabilities(customer=None):
    from backend.actions.lookups import find_customer, no_single_customer

    with connect() as conn:
        customer_id = None
        if customer:
            match, candidates = find_customer(conn, customer)
            if match is None:
                return no_single_customer(customer, candidates)
            customer_id = match['customer_id']
        result = payment_view(conn, 100000)
    rows = result['items']
    if customer_id:
        rows = [r for r in rows if r['customer_id'] == customer_id]
    return {**result, 'items': rows[:10], 'not_listed': max(0, len(rows) - 10)}


def register_insight_actions():
    from backend.actions.registry import Action, register

    register(
        Action(
            'get_cash_risk',
            'Read customer concentration shares and unpaid obligations. The concentration flag is an explicit demo policy, not ML default risk.',
            {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'green',
            get_risk,
        )
    )
    register(
        Action(
            'get_payment_probabilities',
            'Show invoice payment probabilities within 7,14,30 and 60 days, and expected 30-day receipts. Optional exact customer name or ID. Do not interpret these as certainty.',
            {
                'type': 'object',
                'properties': {'customer': {'type': 'string', 'maxLength': 120}},
                'additionalProperties': False,
            },
            'green',
            get_payment_probabilities,
        )
    )
