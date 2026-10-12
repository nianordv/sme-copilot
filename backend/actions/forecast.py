"""The Green cash-forecast action: books and saved curves in, summaries out."""

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field

from backend.actions.lookups import find_customer, no_single_customer
from backend.actions.registry import Action, register
from backend.bank.db import connect
from backend.forecast.cash import SIMULATIONS, SEED, calculate
from backend.forecast.inputs import FORECAST, read_curves, future_sales


class ExtraOutflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    date: date
    amount: float = Field(ge=0, le=1e12, allow_inf_nan=False)
    label: str = Field(min_length=1, max_length=100)


class AssumePaid(BaseModel):
    model_config = ConfigDict(extra='forbid')
    customers: list[str] = Field(default_factory=list, max_length=20)
    invoices: list[str] = Field(default_factory=list, max_length=100)


def forecast_cash(days=30, extra_outflows=None, assume_paid=None):
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 60:
        raise ValueError('Days must be an integer between 1 and 60.')
    if extra_outflows is not None and (not isinstance(extra_outflows, list) or len(extra_outflows) > 20):
        raise ValueError('At most 20 extra outflows are allowed.')
    extras = [ExtraOutflow.model_validate(item) for item in (extra_outflows or [])]
    assumed = AssumePaid.model_validate(assume_paid or {})
    with connect() as conn:
        # One read transaction keeps the books consistent during this forecast.
        conn.execute('BEGIN')
        meta = {row['key']: row['value'] for row in conn.execute('SELECT * FROM meta')}
        snapshot = date.fromisoformat(meta['snapshot_date'])
        last = snapshot + timedelta(days=days)
        company = conn.execute('SELECT * FROM company').fetchone()
        if company is None or company['balance_date'] != meta['snapshot_date']:
            raise ValueError('Opening balance is missing or has the wrong snapshot date.')
        invoices = [dict(row) for row in conn.execute(
            'SELECT * FROM invoices WHERE issue_date <= ? AND (paid_date IS NULL OR paid_date > ?) '
            'AND (writeoff_date IS NULL OR writeoff_date > ?) ORDER BY invoice_id',
            (meta['snapshot_date'],) * 3)]
        curves, metadata = read_curves(FORECAST, meta, invoices)
        payments = [{'date': date.fromisoformat(row['due_date']), 'amount': row['amount'],
                     'label': row['label'], 'category': row['category']} for row in conn.execute(
            'SELECT * FROM scheduled_payments WHERE due_date > ? AND due_date <= ? ORDER BY due_date, payment_id',
            (snapshot.isoformat(), last.isoformat()))]
        for item in extras:
            if not snapshot < item.date <= last:
                raise ValueError('Extra outflow dates must be inside the forecast window.')
            payments.append({**item.model_dump(), 'category': 'what_if'})
        customer_ids = set()
        for name in assumed.customers:
            customer, candidates = find_customer(conn, name)
            if customer is None:
                return no_single_customer(name, candidates)
            customer_ids.add(customer['customer_id'])
        invoice_ids = set(assumed.invoices)
        if not invoice_ids.issubset({item['invoice_id'] for item in invoices}):
            raise ValueError('Assumed-paid invoices must be outstanding invoices in these books.')
        paid_indices = [index for index, item in enumerate(invoices)
                        if item['invoice_id'] in invoice_ids or item['customer_id'] in customer_ids]
        future, sales_assumption = future_sales(conn, snapshot, days)
    amounts = [item['amount'] for item in invoices]
    scenarios = calculate(snapshot, days, company['opening_balance'], amounts, curves, payments, future, paid_indices)
    return {
        'as_of': snapshot.isoformat(), 'days': days, 'currency': meta['currency'],
        'opening_balance': company['opening_balance'],
        'company_numbers_are_placeholders': meta.get('company_numbers_are_placeholders') == '1',
        'predictor': metadata['model_used'], 'baseline_fallback': metadata['fallback'],
        'outstanding_invoices': len(invoices),
        'assumed_paid_today': {'invoice_count': len(paid_indices), 'amount': round(sum(amounts[i] for i in paid_indices), 2)},
        'expected_open_invoice_receipts': round(sum(amount * curves[i, days - 1] for i, amount in enumerate(amounts) if i not in paid_indices), 2),
        'future_invoice_assumption': sales_assumption,
        'scheduled_and_extra_outflows': round(sum(item['amount'] for item in payments), 2),
        'simulations': SIMULATIONS, 'random_seed': SEED,
        'scenario_definition': 'Likely: daily median. Cautious: daily 10th percentile, not a 90% guarantee for the whole path. Worst: no remaining or future invoice pays.',
        'limitation': 'Open-invoice payment dates are sampled independently; shared customer or economic shocks are not modeled.',
        **scenarios,
    }


def register_forecast_action():
    register(Action(
        name='forecast_cash',
        description='Forecast cash for the next 1 to 60 days (default 30). Use for payroll, taxes, cash shortages or what-if expenses. Returns likely, cautious and no-payment balances, shortfalls and weekly summaries. Future-invoice receipts are an explicit assumption. Ask which customer when the result has found=false.',
        parameters={'type': 'object', 'properties': {
            'days': {'type': 'integer', 'minimum': 1, 'maximum': 60},
            'extra_outflows': {'type': 'array', 'items': {
                'type': 'object', 'properties': {'date': {'type': 'string', 'format': 'date'},
                    'amount': {'type': 'number', 'minimum': 0}, 'label': {'type': 'string'}},
                'required': ['date', 'amount', 'label'], 'additionalProperties': False}},
            'assume_paid': {'type': 'object', 'properties': {
                'customers': {'type': 'array', 'items': {'type': 'string'}},
                'invoices': {'type': 'array', 'items': {'type': 'string'}}}, 'additionalProperties': False},
        }, 'additionalProperties': False}, level='green', handler=forecast_cash))
