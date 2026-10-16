"""Conditional collection and borrowing examples. These never change the books."""

import hashlib
import json
from decimal import Decimal, ROUND_CEILING

from backend.bank.ledger import cents, money, repayment_schedule
from backend.bank.products import overdraft_cost


def collection_plans(invoices, amount, days, products, loan_terms):
    percent = float(products.get('early_discount', {}).get('percent', 0))
    if not 0 < percent < 100:
        return []
    target = cents(amount)
    minimum_share = float(products.get('early_discount', {}).get('minimum_collection_share', 0.2))
    if not 0 <= minimum_share <= 1:
        raise ValueError('Collection share must be between zero and one.')
    # Consider individual invoices and a few largest-first bundles. This is
    # a bounded search over examples, not a claim of global optimization.
    ordered = sorted(invoices, key=lambda row: (-row['amount'], row['invoice_id']))
    selections = [[row] for row in ordered]
    selections += [ordered[:size] for size in (2, 3) if len(ordered) >= size]
    plans = []
    for selected in selections:
        gross = sum(cents(row['amount']) for row in selected)
        discount = sum(cents(row['amount'] * percent / 100) for row in selected)
        net = gross - discount
        remaining = max(0, target - net)
        common = {
            'customers': list(dict.fromkeys(row['name'] for row in selected)),
            'invoice_ids': [row['invoice_id'] for row in selected],
            'invoice_amount': money(gross), 'discount_percent': percent,
            'discount_cost': money(discount), 'collection_net': money(net),
            'remaining_gap': money(remaining),
        }
        if not remaining:
            plans.append({**common, 'borrowing': None, 'total_cost': money(discount)})
            continue
        if net < target * minimum_share:
            continue
        od = products['overdraft']
        # Fees are assumed deducted on receipt, so gross borrowing must also
        # cover those fees. Round upward, then verify net proceeds in cents.
        for product, terms in [('overdraft', od), ('term_loan', loan_terms)]:
            fee_percent = Decimal(str(terms.get('fee_percent', 0)))
            if not 0 <= fee_percent < 100:
                continue
            principal = int((Decimal(remaining) / (1 - fee_percent / 100)).to_integral_value(rounding=ROUND_CEILING))
            if product == 'term_loan':
                principal = max(principal, cents(terms['min_amount']))
            limit = cents(terms['limit'] if product == 'overdraft' else terms['max_amount'])
            fee = cents(money(principal) * float(fee_percent) / 100)
            if principal > limit or principal - fee < remaining:
                continue
            borrowed = money(principal)
            if product == 'overdraft':
                cost = overdraft_cost(borrowed, days, terms)
                borrowing = {'product': product, 'principal': borrowed, 'fee': cost['fee'],
                             'interest': cost['interest'], 'cost': cost['total'],
                             'annual_rate': terms['yearly_rate_percent'], 'days': days}
            else:
                # Same cent-rounded schedule used to record existing loans.
                schedule = repayment_schedule(borrowed, terms['yearly_rate_percent'],
                                              terms['term_months'], '2000-02-01', 'amortizing')
                interest = money(sum(row['interest_cents'] for row in schedule))
                borrowing = {'product': product, 'principal': borrowed, 'fee': money(fee),
                             'interest': interest, 'cost': round(interest + money(fee), 2),
                             'annual_rate': terms['yearly_rate_percent'], 'months': terms['term_months'],
                             'monthly_payment': money(schedule[0]['principal_cents'] + schedule[0]['interest_cents'])}
            plans.append({**common, 'borrowing': borrowing,
                          'total_cost': round(money(discount) + borrowing['cost'], 2)})
    # Keep one collection-only example and the best example for each credit type.
    best = {}
    for plan in sorted(plans, key=lambda row: (row['total_cost'], row['invoice_ids'])):
        kind = plan['borrowing']['product'] if plan['borrowing'] else 'discount_only'
        if kind not in best:
            plan['plan_id'] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:24]
            best[kind] = plan
    return sorted(best.values(), key=lambda row: row['total_cost'])


def preview(amount, days, plan_id=None, quoted_annual_rate=None, product=None):
    from backend.actions.borrowing import compare_options
    from backend.actions.forecast import forecast_cash
    from backend.actions.registry import ActionError
    options = compare_options(amount, days, quoted_annual_rate)
    if plan_id is None and product not in ('term_loan', 'overdraft', 'discount_only'):
        raise ActionError('Choose discount only, discount plus term loan, or discount plus overdraft.')
    plan = None
    for candidate in options.get('collection_plans', []):
        kind = candidate['borrowing']['product'] if candidate['borrowing'] else 'discount_only'
        if (plan_id is not None and candidate['plan_id'] == plan_id) or (plan_id is None and kind == product):
            plan = candidate
            break
    if plan is None:
        raise ActionError('This plan no longer matches your books or rates. Compare options again.')
    if days > 60:
        raise ActionError('Cash previews support up to 60 days. Compare a shorter period.')
    baseline = forecast_cash(days=days)
    changed = forecast_cash(days=days, _funding_plan=plan)
    return {'plan': plan, 'days': days, 'as_of': baseline['as_of'],
            'baseline': {name: baseline[name] for name in ('likely', 'cautious', 'worst')},
            'with_plan': {name: changed[name] for name in ('likely', 'cautious', 'worst')},
            'assumption': 'What-if only: selected customers agree to the discount and pay today, and any borrowing is received today. No payment, discount or loan is recorded. The selected invoices are removed from later simulated receipts to avoid counting them twice. Term-loan payments start one month later; overdraft principal and interest are repaid at the end of the requested period. The no-receipts case below still assumes these up-front funds arrived.'}
