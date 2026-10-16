"""Readable funding figures from calculated results, without model arithmetic."""


def cash(value):
    return f'GEL {value:,.2f}'


def collection_answer(plan):
    customers = ', '.join(plan['customers'])
    lines = [f'Ask {customers} to pay {cash(plan["invoice_amount"])} of outstanding invoices today with a {plan["discount_percent"]:g}% discount.',
             f'Discount cost: {cash(plan["discount_cost"])}. You would receive {cash(plan["collection_net"])}; the remaining gap is {cash(plan["remaining_gap"])}.']
    loan = plan['borrowing']
    if loan:
        product = 'an overdraft' if loan['product'] == 'overdraft' else 'a term loan'
        lines.append(f'Borrowing amount: {cash(loan["principal"])} as {product} at {loan["annual_rate"]:g}% nominal annual interest.')
        lines.append(f'Borrowing cost: {cash(loan["cost"])} ({cash(loan["interest"])} interest plus {cash(loan["fee"])} fees).')
        if loan['product'] == 'term_loan':
            lines.append(f'Repay over {loan["months"]} months; regular monthly payment {cash(loan["monthly_payment"])}. The final payment may differ by rounding. Costs cover the whole term, not just the cash gap.')
        else:
            lines.append(f'Overdraft principal and interest are repaid after {loan["days"]} days.')
    else:
        lines.append('No borrowing is needed if this discounted payment arrives as assumed.')
    lines.append(f'Total discount and borrowing cost: {cash(plan["total_cost"])}.')
    return '\n'.join(lines)


def comparison_answer(result):
    lines = [f'For a stated gap of {cash(result["shortfall"])} over {result["days"]} days:',
             'These are conditional examples, not confirmed funding or guaranteed customer payments.']
    if result.get('note'):
        lines.append(result['note'])
    for option in result['options']:
        label = option['option'].replace('_', ' ').capitalize()
        mark = ' — lowest direct cost that could cover the gap' if option['recommended'] else ' — borrowing fallback' if option['fallback'] else ''
        lines.extend([f'\n{label}{mark}: {cash(option["cost"])} direct cost.', option['note']])
        if option['option'] in ('overdraft', 'term_loan'):
            lines.append(f'Nominal annual rate: {option["yearly_rate_percent"]:g}%.')
        if not option['covers_amount']:
            lines.append('This does not cover the full stated gap.')
    for plan in result.get('collection_plans', []):
        lines.extend(['\nCollection alternative:', collection_answer(plan)])
    if result.get('loan_rate_scenarios'):
        lines.append('\nLoan rate scenarios (assumptions, not bank offers):')
        for row in result['loan_rate_scenarios']:
            lines.append(f'{row["yearly_rate_percent"]:g}%: borrow {cash(row["principal"])}, {row["term_months"]} months, monthly {cash(row["monthly_payment"])}, full-term interest and fees {cash(row["cost"])}.')
    lines.append('\nCustomer choices use invoice amounts and direct costs; they are not predictions that a customer will accept a discount. Combinations can cost more than borrowing alone. Customer agreement, payment today and bank approval are assumptions. Nothing has been recorded or requested. Ask me to preview a collection plan to check its cash impact before choosing it.')
    return '\n'.join(lines)


def preview_answer(result):
    original = result['baseline']['cautious']
    changed = result['with_plan']['cautious']
    return '\n'.join([
        collection_answer(result['plan']),
        f'\nOriginal cautious shortfall: {cash(original["shortfall"])}.',
        f'Cautious shortfall with this plan: {cash(changed["shortfall"])}.',
        f'Lowest cautious balance with the plan: {cash(changed["lowest_balance"])} on {changed["lowest_date"]}.',
        result['assumption'],
    ])
