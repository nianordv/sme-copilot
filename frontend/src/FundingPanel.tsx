import { useState } from 'react'
import type { FormEvent } from 'react'
import { apiRequest } from './api'
import { CashChart } from './Charts'

type Option = { option: string; cost: number; yearly_rate_percent: number; note: string; recommended: boolean; fallback: boolean; covers_amount: boolean; subject_to_approval?: boolean; rate_basis?: string; cost_basis: string }
type LoanScenario = { yearly_rate_percent: number; principal: number; term_months: number; monthly_payment: number; interest: number; fee: number; cost: number }
type CollectionPlan = { plan_id: string; customers: string[]; invoice_ids: string[]; invoice_amount: number; discount_percent: number; discount_cost: number; collection_net: number; remaining_gap: number; total_cost: number;
  borrowing: null | { product: string; principal: number; fee: number; interest: number; cost: number; annual_rate: number; monthly_payment?: number } }
type CashPath = { shortfall: number; lowest_balance: number; lowest_date: string; weekly_balances: { date: string; balance: number }[] }
type Preview = { plan: CollectionPlan; assumption: string; baseline: { cautious: CashPath }; with_plan: { cautious: CashPath } }
const cash = (amount: number) => `GEL ${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
export default function FundingPanel({ onSignedOut }: { onSignedOut: () => void }) {
  const [result, setResult] = useState<{ options: Option[]; note?: string; shortfall: number; days: number; loan_rate_scenarios?: LoanScenario[]; loan_scenario_note?: string; quoted_annual_rate?: number | null; collection_plans?: CollectionPlan[]; collection_note?: string } | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function compare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const form = new FormData(event.currentTarget)
    setBusy(true); setError(''); setPreview(null); setResult(null)
    try {
      const response = await apiRequest('/funding-comparison', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: Number(form.get('amount')), days: Number(form.get('days')),
          ...(form.get('quoted_rate') ? { quoted_annual_rate: Number(form.get('quoted_rate')) } : {}) }) }, onSignedOut)
      setResult(await response.json())
    } catch (problem) { setError(problem instanceof Error ? problem.message : 'Comparison failed.') }
    finally { setBusy(false) }
  }
  async function previewPlan(plan: CollectionPlan) {
    if (!result || busy) return
    setBusy(true); setError(''); setPreview(null)
    try {
      const response = await apiRequest('/funding-preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: result.shortfall, days: result.days, plan_id: plan.plan_id,
          ...(result.quoted_annual_rate == null ? {} : { quoted_annual_rate: result.quoted_annual_rate }) }) }, onSignedOut)
      setPreview(await response.json())
    } catch (problem) { setError(problem instanceof Error ? problem.message : 'Unable to preview this plan.') }
    finally { setBusy(false) }
  }
  const max = Math.max(1, ...(result?.options.map((r) => r.cost) ?? []))
  return <section className="dashboard-panel funding-panel"><h3>Ways to cover a cash gap</h3>
    <p>Compare direct costs. Reminders and discounts depend on customers paying; credit depends on bank approval. This page does not request borrowing.</p>
    <form className="funding-form" onSubmit={compare}>
      <label>Shortfall (GEL)<input name="amount" type="number" min="0.01" max="1000000000" step="0.01" required /></label>
      <label>Days to cover<input name="days" type="number" min="1" max="365" step="1" required /></label>
      <label>Annual loan rate quoted to you (%, optional)<input name="quoted_rate" type="number" min="0" max="100" step="0.01" /></label>
      <button className="new-chat-button" disabled={busy}>{busy ? 'Comparing…' : 'Compare costs'}</button>
    </form>
    {error && <p role="alert">{error}</p>}
    {result && <><h4>GEL {result.shortfall.toLocaleString('en-US')} for {result.days} days</h4>
      <p className="dashboard-muted">{result.note}</p>
      {!!result.collection_plans?.length && <section>
        <h4>Collect first, borrow less</h4>
        <p>{result.collection_note}</p>
        {result.collection_plans.map((plan) => <article className="saved-item" key={plan.plan_id}>
          <h4>{plan.borrowing ? 'Discount + ' + (plan.borrowing.product === 'overdraft' ? 'overdraft' : 'term loan') : 'Discount without borrowing'}</h4>
          <p>Ask <strong>{plan.customers.join(', ')}</strong> to settle {plan.invoice_ids.join(', ')} today.</p>
          <p>Outstanding: {cash(plan.invoice_amount)} · Discount: {plan.discount_percent}% ({cash(plan.discount_cost)})</p>
          <p>You would receive <strong>{cash(plan.collection_net)}</strong>, leaving <strong>{cash(plan.remaining_gap)}</strong> of the stated gap.</p>
          <p>Selected by invoice amount and direct cost. Customer agreement and payment are not guaranteed.</p>
          {plan.borrowing && <>
            <p>Borrow {cash(plan.borrowing.principal)} at {plan.borrowing.annual_rate}% per year. Interest: {cash(plan.borrowing.interest)} · Fees: {cash(plan.borrowing.fee)}.</p>
            {plan.borrowing.monthly_payment !== undefined && <p>Regular monthly repayment: {cash(plan.borrowing.monthly_payment)}; the last payment may differ by rounding.</p>}
            <p>Credit requires bank approval. Loan minimums and upfront fees can make the borrowing larger than the remaining gap.</p>
          </>}
          <p>Total discount and borrowing cost: <strong>{cash(plan.total_cost)}</strong></p>
          <button className="new-chat-button" type="button" disabled={busy || result.days > 60} onClick={() => void previewPlan(plan)}>
            {busy ? 'Calculating…' : 'Preview cash impact'}
          </button>
          {result.days > 60 && <p>Cash previews cover up to 60 days.</p>}
          {preview?.plan.plan_id === plan.plan_id && <section>
            <p>{preview.assumption}</p>
            <CashChart series={[
              { name: 'Original cautious forecast', color: '#64748b', points: preview.baseline.cautious.weekly_balances },
              { name: 'With this plan (conditional)', color: '#2563eb', points: preview.with_plan.cautious.weekly_balances },
            ]} />
            <p>Original cautious shortfall: {cash(preview.baseline.cautious.shortfall)}. With the plan: <strong>{cash(preview.with_plan.cautious.shortfall)}</strong>.</p>
            <p>With the plan, lowest cautious balance: {cash(preview.with_plan.cautious.lowest_balance)} on {preview.with_plan.cautious.lowest_date}.</p>
          </section>}
        </article>)}
      </section>}
      {result.loan_rate_scenarios && <section className="saved-item">
        <h4>How the loan rate changes your cost</h4>
        <p>{result.loan_scenario_note}</p>
        {result.loan_rate_scenarios.map((loan) => <article className="saved-item" key={loan.yearly_rate_percent}>
          <h4>{loan.yearly_rate_percent}% per year{loan.yearly_rate_percent === result.quoted_annual_rate ? ' · Your quoted rate' : ' · Rate scenario'}</h4>
          <p>Borrow GEL {loan.principal.toLocaleString()} over {loan.term_months} months.</p>
          <p>Monthly repayment: <strong>GEL {loan.monthly_payment.toFixed(2)}</strong></p>
          <p>Interest: GEL {loan.interest.toFixed(2)} · Upfront fee: GEL {loan.fee.toFixed(2)}</p>
          <p>Total borrowing cost: <strong>GEL {loan.cost.toFixed(2)}</strong></p>
        </article>)}
      </section>}
      {result.options.map((row) => <article key={row.option} className="saved-item">
        <h4>{row.option.replaceAll('_', ' ')} {row.recommended ? '· Lowest direct cost that could cover the gap' : row.fallback ? '· Funding fallback' : ''}</h4>
        <div className="bar-track cost-bar"><div style={{ width: `${row.cost / max * 100}%` }} /></div>
        <p><strong>GEL {row.cost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong> total direct cost · {row.yearly_rate_percent}% annual rate</p>
        <p>{row.rate_basis ?? 'Nominal annual product rate, not an effective APR.'}</p><p>{row.note}</p><p>{row.cost_basis}</p>
        {!row.covers_amount && <p className="log-warning">Does not cover the requested shortfall.</p>}
        {row.subject_to_approval && <p>Subject to bank approval. No credit is approved by Bill.</p>}
      </article>)}
    </>}
  </section>
}
