"""Recording facts supplied by the owner. These actions never move money."""

from functools import partial

from backend.bank import ledger
from backend.bank.db import connect
from backend.actions.registry import Action, register, quoted

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 120}
DATE = {'type': 'string', 'format': 'date'}
AMOUNT = {'type': 'number', 'minimum': 0, 'maximum': 1000000000}


def read_cash_records():
    with connect() as conn:
        page = ledger.read_ledger(conn)
    return {
        'cash': page['cash'],
        'recent_entries': [
            {k: v for k, v in r.items() if k != 'note'} for r in page['entries'][:5]
        ],
        'loans': [
            {
                'loan_id': r['loan_id'],
                'reference': r['reference'],
                'outstanding_principal': r['outstanding_principal'],
                'cancelled': r['cancelled'],
            }
            for r in page['loans'][:5]
        ],
        'overdue_obligations': [
            r for r in page['obligations'] if r['due_date'] <= page['cash']['as_of']
        ][:10],
    }


def describe(action_name, **arguments):
    titles = {
        'set_opening_balance': 'Save opening cash?',
        'record_cash_transaction': 'Record this completed cash movement?',
        'record_existing_loan': 'Record this existing loan and its repayment schedule?',
        'set_reporting_date': 'Advance the reporting date?',
        'reverse_cash_transaction': 'Reverse this incorrect cash record?',
        'cancel_recorded_loan': 'Cancel this incorrect loan record in our books?',
    }
    labels = {
        'amount': 'Amount (GEL)',
        'opening_date': 'Opening date',
        'reference': 'Reference',
        'event_date': 'Payment date',
        'direction': 'Money in / out',
        'category': 'Category',
        'invoice_id': 'Invoice',
        'obligation_id': 'Bill or loan payment',
        'note': 'Note',
        'principal': 'Principal (GEL)',
        'annual_rate': 'Annual nominal interest (%)',
        'fees': 'Upfront fees (GEL)',
        'disbursed_on': 'Funds received on',
        'first_payment_date': 'First repayment',
        'months': 'Monthly periods',
        'repayment': 'Repayment structure',
        'included_in_opening': 'Already included in opening cash',
        'report_date': 'New reporting date',
        'entry_id': 'Original cash entry',
        'loan_id': 'Loan record',
        'reason': 'Reason',
    }
    lines = [titles[action_name], 'This updates your books only; no money will be moved.']
    for key, value in arguments.items():
        if key == 'included_in_opening':
            value = 'Yes — do not change cash again' if value else 'No'
        elif key == 'repayment':
            value = str(value).replace('_', ' ')
        # The labels are ours. Every supplied value remains quoted data, so a
        # note cannot impersonate an approval instruction or another field.
        lines.append(f'{labels[key]}:\n{quoted(str(value))}')
    return '\n'.join(lines)


def register_bookkeeping_actions():
    specifications = [
        (
            'set_opening_balance',
            ledger.initialize,
            'Set recorded opening cash once, with its date. Ask for amount and date. Only later recorded transactions change cash.',
            {'amount': {**AMOUNT, 'minimum': -1000000000}, 'opening_date': DATE},
            ['amount', 'opening_date'],
        ),
        (
            'record_cash_transaction',
            ledger.record_cash,
            'Record money already received or already paid. Ask for reference, event_date, direction (in/out), amount and category. Link invoice_id for invoice receipts or obligation_id for a bill/loan repayment. Mark included_in_opening=true only to reconcile an earlier movement already counted in the starting cash; this changes invoice/bill status without changing cash. Never use this for predictions, planned payments or transfers to execute.',
            {
                'reference': TEXT,
                'event_date': DATE,
                'direction': {'type': 'string', 'enum': ['in', 'out']},
                'amount': {**AMOUNT, 'exclusiveMinimum': 0},
                'category': TEXT,
                'note': {'type': 'string', 'maxLength': 1000},
                'invoice_id': TEXT,
                'obligation_id': TEXT,
                'included_in_opening': {'type': 'boolean'},
            },
            ['reference', 'event_date', 'direction', 'amount', 'category'],
        ),
        (
            'reverse_cash_transaction',
            ledger.reverse_entry,
            'Correct a mistaken cash record with an opposite entry. Original history remains. Ask for entry_id, reference and reason.',
            {'entry_id': {'type': 'integer', 'minimum': 1}, 'reference': TEXT, 'reason': TEXT},
            ['entry_id', 'reference', 'reason'],
        ),
        (
            'set_reporting_date',
            ledger.advance_date,
            'Advance the reporting date after the owner provides the date. Does not mark bills paid or invent transactions.',
            {'report_date': DATE},
            ['report_date'],
        ),
        (
            'record_existing_loan',
            ledger.record_loan,
            'Record a loan ALREADY received outside Bill. This does not take a loan. Ask for all required terms; do not guess interest, dates, fees or repayment structure. Fees are upfront deductions. For an older loan enter its remaining principal and future schedule and set included_in_opening=true.',
            {
                'reference': TEXT,
                'principal': {**AMOUNT, 'exclusiveMinimum': 0},
                'annual_rate': {'type': 'number', 'minimum': 0, 'maximum': 100},
                'fees': AMOUNT,
                'disbursed_on': DATE,
                'first_payment_date': DATE,
                'months': {'type': 'integer', 'minimum': 1, 'maximum': 360},
                'repayment': {'type': 'string', 'enum': ['amortizing', 'interest_only', 'bullet']},
                'included_in_opening': {'type': 'boolean'},
            },
            [
                'reference',
                'principal',
                'annual_rate',
                'fees',
                'disbursed_on',
                'first_payment_date',
                'months',
                'repayment',
                'included_in_opening',
            ],
        ),
        (
            'cancel_recorded_loan',
            ledger.cancel_loan,
            'Correct an incorrectly recorded loan, not a real bank loan. Only allowed after reversing any recorded repayments. Requires loan_id, a new reference and reason.',
            {'loan_id': {'type': 'integer', 'minimum': 1}, 'reference': TEXT, 'reason': TEXT},
            ['loan_id', 'reference', 'reason'],
        ),
    ]
    for name, handler, description, properties, required in specifications:
        register(
            Action(
                name,
                description,
                {
                    'type': 'object',
                    'properties': properties,
                    'required': required,
                    'additionalProperties': False,
                },
                'yellow',
                handler,
                summarize=partial(describe, name),
            )
        )
    register(
        Action(
            'get_cash_records',
            'Read recorded cash, recent transactions, existing loans and overdue obligations. Not a bank balance.',
            {'type': 'object', 'properties': {}, 'additionalProperties': False},
            'green',
            read_cash_records,
        )
    )
