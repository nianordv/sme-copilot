import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { apiRequest } from './api'
import PaymentFields from './PaymentFields'

type Field = { name: string; label: string; type?: string; optional?: boolean; choices?: string[] }
const forms: Record<string, { label: string; help: string; fields: Field[] }> = {
  set_opening_balance: { label: 'Enter your starting balance', help: 'How much cash does your business have on your starting date? Bill will use this balance and the payments you record to keep track of your cash. No bank access is needed.', fields: [
    { name: 'amount', label: 'Starting balance (GEL)', type: 'number' }, { name: 'opening_date', label: 'Starting date', type: 'date' }] },
  record_cash_transaction: { label: 'Record a payment', help: 'Record money already received or paid. Choose the related invoice, bill or loan instalment so Bill can update both cash and what is still owed. Bill does not access your bank account.', fields: [
    { name: 'reference', label: 'Bank payment reference (optional)', optional: true }, { name: 'event_date', label: 'Actual payment date', type: 'date' },
    { name: 'direction', label: 'Direction', choices: ['in', 'out'] }, { name: 'amount', label: 'Amount (GEL)', type: 'number' },
    { name: 'category', label: 'Category', choices: ['invoice', 'expense', 'loan', 'adjustment', 'other'] },
    { name: 'invoice_id', label: 'Invoice ID (for a customer receipt)', optional: true },
    { name: 'obligation_id', label: 'Obligation ID (for a bill or loan repayment)', optional: true },
    { name: 'note', label: 'Note / explanation', optional: true },
    { name: 'included_in_opening', label: 'This earlier movement was already counted in opening cash: reconcile it without changing cash', type: 'checkbox' }] },
  record_existing_loan: { label: 'Record an existing loan', help: 'Only record borrowing already received elsewhere. Use remaining principal and a future schedule for an older loan. Fixed annual nominal rate, monthly interest periods; fees are deducted upfront. No daily interest proration. Bill does not apply for or take this loan.', fields: [
    { name: 'reference', label: 'Bank agreement number (optional)', optional: true }, { name: 'principal', label: 'Principal / remaining principal (GEL)', type: 'number' },
    { name: 'annual_rate', label: 'Annual nominal interest (%)', type: 'number' }, { name: 'fees', label: 'Upfront fees (GEL, enter 0 if none)', type: 'number' },
    { name: 'disbursed_on', label: 'Date funds were received', type: 'date' }, { name: 'first_payment_date', label: 'First future repayment date', type: 'date' },
    { name: 'months', label: 'Remaining monthly periods', type: 'number' },
    { name: 'repayment', label: 'Repayment structure', choices: ['amortizing', 'interest_only', 'bullet'] },
    { name: 'included_in_opening', label: 'The proceeds are already included in opening cash (do not add them again)', type: 'checkbox' }] },
  set_reporting_date: { label: 'Advance reporting date', help: 'Use the explicit date of the books. Cash changes only when actual movements are recorded; advancing the date does not pay scheduled bills.', fields: [{ name: 'report_date', label: 'Reporting date', type: 'date' }] },
  reverse_cash_transaction: { label: 'Correct a cash record', help: 'An opposite entry will be saved. The original stays in history. For loan proceeds use Cancel loan record.', fields: [
    { name: 'entry_id', label: 'Original entry ID', type: 'number' }, { name: 'reference', label: 'Unique correction reference' }, { name: 'reason', label: 'Reason' }] },
  cancel_recorded_loan: { label: 'Cancel an incorrect loan record', help: 'This only corrects our books; it does not cancel a real loan. Reverse any recorded repayments first.', fields: [
    { name: 'loan_id', label: 'Loan record ID', type: 'number' }, { name: 'reference', label: 'Unique correction reference' }, { name: 'reason', label: 'Reason' }] },
}

export type PendingUpdate = { confirmation_id: string; summary: string }

export default function BookkeepingForm({ initialized, asOf, sessionId, onSignedOut, onPending, setup = false }: {
  initialized: boolean; asOf: string; sessionId: string; onSignedOut: () => void; onPending: (update: PendingUpdate) => void; setup?: boolean
}) {
  const [action, setAction] = useState(initialized ? 'record_cash_transaction' : 'set_opening_balance')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submitted = useRef(false)
  const generatedReferences = useRef(new Map<string, string>())
  const definition = forms[action]

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitted.current) return
    if (event.currentTarget.querySelector<HTMLSelectElement>('select[data-payment-link]')?.disabled) {
      setError('Wait for the invoice or bill list to load before continuing.')
      return
    }
    const form = new FormData(event.currentTarget)
    const args: Record<string, unknown> = {}
    for (const field of definition.fields) {
      const value = form.get(field.name)
      if (field.type === 'checkbox') args[field.name] = value === 'on'
      else if (value !== null && value !== '') args[field.name] = field.type === 'number' ? Number(value) : value
    }
    if (action === 'record_cash_transaction' &&
      ((args.category === 'invoice' && !args.invoice_id) || (args.category === 'loan' && !args.obligation_id))) {
      setError('Choose the invoice or loan instalment that was paid before continuing.')
      return
    }
    if (['record_existing_loan', 'record_cash_transaction'].includes(action) && !args.reference) {
      // Keep the same reference if this form is submitted again after a lost
      // response. A second approval must not add the same proceeds twice.
      const details = action + JSON.stringify(args)
      let reference = generatedReferences.current.get(details)
      if (!reference) {
        reference = `${action === 'record_existing_loan' ? 'loan' : 'payment'}-${crypto.randomUUID()}`
        generatedReferences.current.set(details, reference)
      }
      args.reference = reference
    }
    submitted.current = true
    setBusy(true)
    setError('')
    try {
      const response = await apiRequest('/bookkeeping', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, arguments: args, session_id: sessionId }) }, onSignedOut)
      const result = await response.json()
      if (result.status !== 'needs_confirmation') throw new Error(result.message ?? result.reason ?? 'No approval request was created.')
      onPending(result)
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Unable to prepare this record.')
    } finally {
      submitted.current = false
      setBusy(false)
    }
  }

  return <section className="record-form dashboard-panel">
    <h3>{setup ? 'Enter your starting balance' : 'Update your books'}</h3>
    {!setup && <>
      <button className="new-chat-button" type="button" disabled={busy} aria-pressed={action === 'record_existing_loan'}
        onClick={() => { setAction('record_existing_loan'); setError('') }}>Add a loan</button>
      <label>What would you like to record?<select value={action} disabled={busy} onChange={(e) => { setAction(e.target.value); setError('') }}>
        {Object.entries(forms).filter(([key]) => key !== 'set_opening_balance').map(([key, item]) =>
          <option key={key} value={key}>{item.label}</option>)}
      </select></label>
    </>}
    <p>{definition.help}</p>
    <form key={action} onSubmit={submit}>
      {action === 'record_cash_transaction' ? <PaymentFields asOf={asOf} busy={busy} onSignedOut={onSignedOut} /> : definition.fields.map((field) => <label className={field.type === 'checkbox' ? 'checkbox-label' : ''} key={field.name}>
        {field.type === 'checkbox' && <input name={field.name} type="checkbox" disabled={busy} />}
        {field.label}
        {field.choices ? <select name={field.name} required disabled={busy}>
          <option value="">Choose…</option>{field.choices.map((choice) => <option key={choice}>{choice}</option>)}
        </select> : field.type !== 'checkbox' && <input name={field.name} type={field.type ?? 'text'} required={!field.optional}
          step={['months', 'entry_id', 'loan_id'].includes(field.name) ? '1' : '0.01'}
          defaultValue={field.type === 'date' && field.name !== 'first_payment_date' && field.name !== 'disbursed_on' ? asOf : undefined}
          maxLength={field.name === 'note' ? 1000 : 120} disabled={busy} />}
      </label>)}
      {error && <p role="alert">{error}</p>}
      <button className="new-chat-button" type="submit" disabled={busy}>{busy ? 'Preparing…' : setup ? 'Review starting balance' : 'Review in chat'}</button>
      <p className="dashboard-muted">{setup ? 'You will review and confirm this balance before it is saved.' : 'Nothing is saved to the cash books until you approve the exact update in chat.'}</p>
    </form>
  </section>
}
