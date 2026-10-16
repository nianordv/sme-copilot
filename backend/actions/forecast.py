"""The Green cash-forecast action: books and saved curves in, summaries out."""

from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field

from backend.actions.lookups import find_customer, no_single_customer
from backend.actions.registry import Action, ActionError, register
from backend.bank.db import connect, reporting_date
from backend.bank.ledger import balance, obligations
from backend.actions.lookups import open_invoices
from backend.forecast.cash import SIMULATIONS, SEED, calculate
from backend.forecast.inputs import FORECAST, current_curves, future_sales


class ExtraOutflow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    date: date
    amount: float = Field(ge=0, le=1e12, allow_inf_nan=False)
    label: str = Field(min_length=1, max_length=100)


class AssumePaid(BaseModel):
    model_config = ConfigDict(extra='forbid')
    customers: list[str] = Field(default_factory=list, max_length=20)
    invoices: list[str] = Field(default_factory=list, max_length=100)


def forecast_cash(days=30, extra_outflows=None, assume_paid=None, _funding_plan=None):
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
        snapshot = reporting_date(conn)
        last = snapshot + timedelta(days=days)
        company = balance(conn)
        invoices = open_invoices(conn, snapshot)
        curves, metadata = current_curves(conn, FORECAST, meta, invoices, snapshot)
        due = obligations(conn)
        payments = [{'date': max(snapshot+timedelta(days=1), date.fromisoformat(row['due_date'])),
                     'amount':row['amount'], 'label':row['label'], 'category':row['category']}
                    for row in due if row['due_date'] <= last.isoformat()]
        overdue_amount = round(sum(row['amount'] for row in due if row['due_date']<=snapshot.isoformat()),2)
        for item in extras:
            if not snapshot < item.date <= last:
                required_days = (item.date - snapshot).days
                raise ActionError(
                    f'The extra payment date {item.date} is outside the {days}-day forecast '
                    f'from {snapshot}. A forecast through that date needs {required_days} days; '
                    'the supported range is 1 to 60 days. Do not omit the extra payment.')
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
    if _funding_plan is not None:
        from backend.bank.ledger import cents, money, month_date, repayment_schedule
        selected = set(_funding_plan['invoice_ids'])
        if not selected.issubset({row['invoice_id'] for row in invoices}):
            raise ActionError('An invoice in this plan is no longer outstanding. Compare options again.')
        gross = sum(cents(row['amount']) for row in invoices if row['invoice_id'] in selected)
        if gross != cents(_funding_plan['invoice_amount']):
            raise ActionError('Invoice balances changed. Compare options again.')
        paid_indices = [i for i, row in enumerate(invoices) if row['invoice_id'] in selected]
        # calculate() adds the full value of assumed-paid invoices. Deduct the
        # discount once, and add only net loan proceeds, to get actual what-if cash.
        company['balance'] = money(cents(company['balance']) - cents(_funding_plan['discount_cost']))
        borrowing = _funding_plan['borrowing']
        if borrowing:
            company['balance'] = money(cents(company['balance']) + cents(borrowing['principal']) - cents(borrowing['fee']))
            if borrowing['product'] == 'overdraft':
                payments.append({'date': last, 'amount': money(cents(borrowing['principal']) + cents(borrowing['interest'])),
                                 'label': 'Hypothetical overdraft repayment', 'category': 'loan'})
            else:
                schedule = repayment_schedule(borrowing['principal'], borrowing['annual_rate'], borrowing['months'],
                                              month_date(snapshot, 1).isoformat(), 'amortizing')
                payments.extend({'date': date.fromisoformat(row['due_date']),
                                 'amount': money(row['principal_cents'] + row['interest_cents']),
                                 'label': 'Hypothetical loan repayment', 'category': 'loan'}
                                for row in schedule if row['due_date'] <= last.isoformat())
    scenarios = calculate(snapshot, days, company['balance'], amounts, curves, payments, future, paid_indices)
    return {
        'as_of': snapshot.isoformat(), 'days': days, 'currency': meta['currency'],
        'opening_balance': company['balance'],
        'recorded_cash_initialized':company['initialized'],
        'overdue_obligations':overdue_amount,
        'overdue_assumption':'Unpaid overdue obligations are assumed due tomorrow, not silently removed.',
        'company_numbers_are_placeholders': meta.get('company_numbers_are_placeholders') == '1',
        'predictor': metadata['model_used'], 'baseline_fallback': metadata['fallback'],
        'outstanding_invoices': len(invoices),
        'assumed_paid_today': {'invoice_count': len(paid_indices), 'amount': round(sum(amounts[i] for i in paid_indices), 2)},
        'expected_open_invoice_receipts': round(sum(amount * curves[i, days - 1] for i, amount in enumerate(amounts) if i not in paid_indices), 2),
        'future_invoice_assumption': sales_assumption,
        'scheduled_and_extra_outflows': round(sum(item['amount'] for item in payments), 2),
        'simulations': SIMULATIONS, 'random_seed': SEED,
        'scenario_definition': 'Likely: daily median. Cautious: daily 10th percentile, not a 90% guarantee for the whole path. Worst: no remaining or future invoice pays.',
        'limitation': 'Open-invoice payment dates are sampled independently; shared customer or economic shocks are not modeled. Partial payments reduce the balance; their effect on payment timing is not validated.',
        **scenarios,
    }


def register_forecast_action():
    from backend.funding_plans import preview
    register(Action(
        name='preview_funding_plan',
        description='Preview a collection plan for amount, days and optional quoted_annual_rate. Prefer product=term_loan for discount plus loan, overdraft for discount plus overdraft, or discount_only. The server resolves the current plan so no ID needs to be guessed. Alternatively use a plan_id returned by compare_options in this turn. Returns original and hypothetical cash forecasts. Assumes customers pay today and credit arrives today; not guaranteed. No records change.',
        parameters={'type': 'object', 'properties': {
            'amount': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1000000000},
            'days': {'type': 'integer', 'minimum': 1, 'maximum': 60},
            'plan_id': {'type': 'string', 'pattern': '^[a-f0-9]{24}$'},
            'product': {'type': 'string', 'enum': ['term_loan', 'overdraft', 'discount_only']},
            'quoted_annual_rate': {'type': 'number', 'minimum': 0, 'maximum': 100},
        }, 'required': ['amount', 'days'], 'additionalProperties': False},
        level='green', handler=preview))
    register(Action(
        name='forecast_cash',
        description='Use to answer whether the owner can pay salaries or taxes, afford an expense, or needs borrowing. Forecast cash for the next 1 to 60 days (default 30). Returns likely, cautious and no-payment balances, including balances after salaries and taxes in obligation_balances, shortfalls and weekly summaries. Use cautious.shortfall and cautious.days_below_zero for a follow-up compare_options call if there is a gap. Future-invoice receipts are an explicit assumption. Ask which customer when the result has found=false.',
        parameters={'type': 'object', 'properties': {
            'days': {'type': 'integer', 'minimum': 1, 'maximum': 60},
            'extra_outflows': {'type': 'array', 'items': {
                'type': 'object', 'properties': {'date': {'type': 'string', 'format': 'date'},
                    'amount': {'type': 'number', 'minimum': 0}, 'label': {'type': 'string'}},
                'required': ['date', 'amount', 'label'], 'additionalProperties': False}},
            'assume_paid': {'type': 'object', 'description': 'Only for a hypothetical what-if payment. For example {"customers": ["Racha Guesthouse"]}. Does not record a payment. An actual receipt requires record_cash_transaction and owner approval.', 'properties': {
                'customers': {'type': 'array', 'description': 'Names or IDs of customers hypothetically settling all outstanding invoices.', 'items': {'type': 'string'}},
                'invoices': {'type': 'array', 'items': {'type': 'string'}}}, 'additionalProperties': False},
        }, 'additionalProperties': False}, level='green', handler=forecast_cash))
