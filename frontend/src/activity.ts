// Display labels only. The server keeps the full audit record.
const actions: Record<string, string> = {
  forecast_cash: 'Cash forecast',
  get_receivables: 'Unpaid invoices',
  get_customer: 'Customer details',
  get_balance: 'Cash balance',
  get_upcoming_outflows: 'Upcoming expenses',
  compare_options: 'Funding comparison',
  preview_funding_plan: 'Funding plan cash preview',
  draft_reminder: 'Payment reminder draft',
  send_reminder: 'Reminder saved to outbox',
  set_alert: 'Cash alert',
  save_plan: 'Saved plan',
  prepare_credit_request: 'Credit request draft',
  set_opening_balance: 'Starting balance',
  record_cash_transaction: 'Money received or paid',
  reverse_cash_transaction: 'Transaction correction',
  set_reporting_date: 'Reporting date',
  record_existing_loan: 'Existing loan record',
  cancel_recorded_loan: 'Loan record correction',
  get_cash_records: 'Transactions and loans',
  get_cash_risk: 'Customer risk review',
  get_payment_probabilities: 'Invoice payment estimates',
  transfer_money: 'Money transfer request',
  take_loan: 'Borrowing request',
  repay_loan: 'Loan repayment request',
  submit_credit_request: 'Credit submission request',
  sign_in: 'Sign-in',
  sign_out: 'Sign-out',
  unlock: 'Chat unlock',
  delete_chat: 'Conversation deleted',
}

const decisions: Record<string, string> = {
  ran: 'Completed',
  waiting: 'Waiting for your approval — not carried out',
  approved: 'Approval recorded',
  declined: 'Declined — not carried out',
  refused: 'Blocked',
  expired: 'Approval expired — not carried out',
  error: 'Could not complete',
  signed_in: 'Signed in',
  signed_out: 'Signed out',
  unlocked: 'Chat unlocked',
}

export function activityTitle(action: string) {
  return actions[action] ?? 'Activity request'
}

export function activityOutcome(decision: string) {
  return decisions[decision] ?? 'Recorded'
}

export function activityTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}
