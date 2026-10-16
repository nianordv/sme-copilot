import { useEffect, useState } from 'react'
import { apiRequest } from './api'

type Invoice = { invoice_id: string; name: string; amount: number; due_date: string }
type Bill = { obligation_id: string; label: string; amount: number; due_date: string; category: string }
const cash = (amount: number) => `GEL ${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function PaymentLink({ kind, day, onSignedOut }: {
  kind: 'invoice' | 'loan' | 'expense'; day: string; onSignedOut: () => void
}) {
  const [search, setSearch] = useState('')
  const [options, setOptions] = useState<{ invoices: Invoice[]; bills: Bill[]; total: number } | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      setLoading(true); setError('')
      try {
        const query = new URLSearchParams({ kind, query: search, payment_date: day })
        const response = await apiRequest(`/payment-record-options?${query}`, { signal: controller.signal }, onSignedOut)
        const data = await response.json()
        if (!controller.signal.aborted) setOptions(data)
      } catch (problem) {
        if (!controller.signal.aborted) { setOptions(null); setError(problem instanceof Error ? problem.message : 'Unable to load payments.') }
      } finally { if (!controller.signal.aborted) setLoading(false) }
    }, 200)
    return () => { clearTimeout(timer); controller.abort() }
  }, [kind, search, day, onSignedOut])
  const required = kind !== 'expense'
  return <>
    <label>Find {kind === 'invoice' ? 'a customer or invoice' : 'a bill or repayment'}
      <input value={search} onChange={(event) => { setLoading(true); setSearch(event.target.value) }} placeholder="Search by name or reference" />
    </label>
    <label>{kind === 'invoice' ? 'Which invoice was paid?' : 'Which bill or instalment was paid?'}
      <select data-payment-link key={`${kind}:${search}:${day}`} name={kind === 'invoice' ? 'invoice_id' : 'obligation_id'} required={required} disabled={loading || !!error}>
        <option value="">{required ? 'Choose…' : 'Separate expense — not a listed bill'}</option>
        {kind === 'invoice' ? options?.invoices.map((row) => <option key={row.invoice_id} value={row.invoice_id}>
          {row.name} · {row.invoice_id} · {cash(row.amount)} remaining · due {row.due_date}
        </option>) : options?.bills.map((row) => <option key={row.obligation_id} value={row.obligation_id}>
          {row.label} · due {row.due_date} · {cash(row.amount)} remaining
        </option>)}
      </select>
    </label>
    {loading && <p role="status">Loading payment choices…</p>}
    {error && <p role="alert">{error}</p>}
    {options && <p className="dashboard-muted">{options.total > 50 ? 'Showing 50 matches. Search to narrow the list.' : `${options.total} matching ${kind === 'invoice' ? 'invoices' : 'bills or instalments'}.`} Record only the amount actually paid; partial payments are supported.</p>}
  </>
}

export default function PaymentFields({ asOf, busy, onSignedOut }: {
  asOf: string; busy: boolean; onSignedOut: () => void
}) {
  const [direction, setDirection] = useState('in')
  const [category, setCategory] = useState('invoice')
  const [day, setDay] = useState(asOf)
  return <fieldset disabled={busy} className="payment-fields">
    <label>Actual payment date<input name="event_date" type="date" value={day} required max={asOf} onChange={(event) => setDay(event.target.value)} /></label>
    <label>Payment type<select name="direction" value={direction} onChange={(event) => {
      setDirection(event.target.value); setCategory(event.target.value === 'in' ? 'invoice' : 'expense')
    }}><option value="in">Money received</option><option value="out">Money paid</option></select></label>
    <label>Amount received or paid (GEL)<input name="amount" type="number" min="0.01" max="1000000000" step="0.01" required /></label>
    <label>Category<select name="category" value={category} onChange={(event) => setCategory(event.target.value)}>
      {direction === 'in' ? <option value="invoice">Customer invoice payment</option> : <>
        <option value="expense">Business expense or bill</option><option value="loan">Loan repayment</option>
      </>}
      <option value="other">Other</option><option value="adjustment">Balance adjustment</option>
    </select></label>
    {['invoice', 'expense', 'loan'].includes(category) && <PaymentLink key={`${direction}:${category}:${day}`} kind={category as 'invoice' | 'expense' | 'loan'} day={day} onSignedOut={onSignedOut} />}
    <label>Note (optional)<input name="note" maxLength={1000} /></label>
    <details className="payment-details"><summary>Additional details</summary>
      <label>Bank payment reference (optional)<input name="reference" maxLength={120} /></label>
      <label className="checkbox-label"><input name="included_in_opening" type="checkbox" />
        This payment was already included in my starting balance. Update the invoice or bill without changing cash again.
      </label>
    </details>
  </fieldset>
}
