import { useEffect, useRef, useState } from 'react'
import { apiRequest } from './api'
import BookkeepingForm from './BookkeepingForm'
import type { PendingUpdate } from './BookkeepingForm'
import FundingPanel from './FundingPanel'
import { CashChart, ShareBar } from './Charts'
import { activityTitle, activityOutcome, activityTime } from './activity'

interface SavedList<T> { items: T[]; not_listed: number }
interface Scenario {
  lowest_balance: number; lowest_date: string; first_day_below_zero: string | null
  shortfall: number; days_below_zero: number; first_shortfall_duration_days: number; weekly_balances: { date: string; balance: number }[]
}
interface DashboardData {
  cash: {
    as_of: string
    balance: { balance: number; currency: string; account_holder: string }
    receivables: {
      owed: { amount: number; invoices: number; customers: number }
      of_which_overdue: { amount: number; invoices: number; customers: number }
      overdue_by_days_late: Record<string, { amount: number; invoices: number }>
    }
    upcoming_outflows: { total: number; days: number }
  }
  forecast: { available: boolean; reason?: string; result?: {
    likely: Scenario; cautious: Scenario; worst: Scenario
    days: number; opening_balance: number; as_of: string; scenario_definition: string; limitation: string; company_numbers_are_placeholders: boolean
    future_invoice_assumption: { warning?: string }
  } }
  work: {
    waiting: SavedList<{ session_id: string; action: string; summary: string; expires_at: string }>
    reminders: SavedList<{ message_id: number; customer: string; saved_on: string; text: string }>
    alerts: SavedList<{ alert_id: number; date: string | null; balance_below: number | null; note: string | null }>
    alert_checks: { alert_id: number; triggered: boolean; reasons: string[] }[]
    plan: { saved_on: string; text: string } | null
    credit_request_drafts: SavedList<{ draft_id: number; product_name: string; amount: number; status: string }>
  }
  log: {
    check: { intact: boolean; rows: number }
    rows: { log_id: number; time: string; action: string; level: string; decision: string; result: string; arguments: string }[]
  }
}

interface Workspace {
  ledger: {
    cash: { initialized: boolean; as_of: string; balance: number; opening_date?: string; source: string }
    entries: { entry_id: number; reference: string; event_date: string; direction: string; amount: number; category: string; note: string; reversal_of: number | null; included_in_opening: number }[]
    obligations: { obligation_id: string; due_date: string; label: string; amount: number; category: string }[]
    loans: { loan_id: number; reference: string; principal: number; outstanding_principal: number; annual_rate: string; cancelled: number; schedule: { due_date: string; total: number; principal: number; interest: number }[] }[]
  }
  risk: { warnings: string[]; policy_note: string; top_customers: { customer_id: string; customer: string; share_percent: number; owed: number; concentration_warning: boolean }[] }
  payments: { available: boolean; reason?: string; as_of?: string; predictor?: string; interpretation?: string; not_listed?: number; items?: {
    invoice_id: string; name: string; customer_id: string; amount: number; due_date: string; probabilities: Record<string, number>; expected_receipts_30d: number
  }[] }
}
const pages = ['Overview', 'Cash forecast', 'Invoices & customers', 'Transactions & loans', 'Funding options', 'Activity'] as const

function money(value: number, currency = 'GEL') {
  return `${currency} ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function MoreItems({ count }: { count: number }) {
  return count > 0 ? <p className="dashboard-muted">{count} older items not shown.</p> : null
}

export default function Dashboard({ active, revision, onSignedOut, onAskBill, sessionId, onPending, onNotice }: {
  active: boolean; revision: number; onSignedOut: () => void; onAskBill: (question: string) => void; sessionId: string; onPending: (update: PendingUpdate) => void; onNotice: (message: string) => void
}) {
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState('')
  const [workspace, setWorkspace] = useState<Workspace | null>(null)
  const [page, setPage] = useState<(typeof pages)[number]>('Overview')
  const [search, setSearch] = useState('')
  const [days, setDays] = useState(30)
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const fetchedRevision = useRef(-1)

  useEffect(() => {
    if (!active && revision === fetchedRevision.current) return
    fetchedRevision.current = revision
    const controller = new AbortController()
    async function load() {
      setLoading(true)
      setError('')
      try {
        const [response, records] = await Promise.all([
          apiRequest(`/dashboard?days=${days}`, { signal: controller.signal }, onSignedOut),
          apiRequest('/workspace', { signal: controller.signal }, onSignedOut),
        ])
        const next = await response.json()
        const book = await records.json()
        if (!book.ledger?.cash || !book.risk || !book.payments) throw new Error('The cash records response is incomplete.')
        if (!next.cash?.balance || !next.cash?.receivables || !next.cash?.upcoming_outflows
          || !next.forecast || !next.work || !next.log?.check || !Array.isArray(next.log.rows)) {
          throw new Error('The dashboard response is incomplete.')
        }
        if (!controller.signal.aborted) {
          setData(next); setWorkspace(book)
          const gap = next.forecast.result?.cautious
          const alert = next.work.alert_checks?.find((r: { triggered: boolean }) => r.triggered)
          const notice = !book.ledger.cash.initialized ? 'Set up opening cash in the dashboard before recording transactions. Current figures use seeded demo cash.'
            : gap?.shortfall > 0 ? `Cash warning: the cautious forecast shows a ${money(gap.shortfall)} shortfall, first below zero on ${gap.first_day_below_zero}. Open the dashboard for details.`
              : !next.forecast.available ? next.forecast.reason
                : alert ? `Alert ${alert.alert_id}: ${alert.reasons.join(' ')}` : book.risk.warnings[0] ?? ''
          onNotice(notice)
        }
      } catch (problem) {
        if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : 'Cannot load the dashboard.')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [active, revision, refresh, onSignedOut, onNotice, days])

  const forecast = data?.forecast.result
  return (
    <section className="dashboard" hidden={!active} aria-label="Business dashboard" aria-busy={loading}>
      <div className="dashboard-heading">
        <div><h2>Your business at a glance</h2>{data && <p>Books as of {data.cash.as_of}</p>}</div>
        <button className="new-chat-button" type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
      </div>
      {loading && <p role="status">Loading the dashboard…</p>}
      {error && <p role="alert">{error} {data && 'The figures below are from the previous successful load.'}</p>}
      <nav className="dashboard-tabs" aria-label="Dashboard pages">{pages.map((name) =>
        <button key={name} type="button" aria-current={page === name ? 'page' : undefined} onClick={() => setPage(name)}>{name}</button>)}</nav>
      {data && workspace && <>
        {workspace.ledger.cash.initialized && page === 'Transactions & loans' && <BookkeepingForm initialized
          asOf={workspace.ledger.cash.as_of} sessionId={sessionId} onSignedOut={onSignedOut} onPending={onPending} />}
        {page === 'Overview' && <section className="dashboard-panel risk-banner">
          <h3>What needs attention</h3>
          {workspace.risk.warnings.map((text) => <p className="log-warning" key={text}>{text}</p>)}
          {forecast && <p className={forecast.cautious.shortfall > 0 ? 'log-warning' : 'log-intact'}>
            {forecast.cautious.shortfall > 0 ? `Cautious shortfall: ${money(forecast.cautious.shortfall)}. First below zero: ${forecast.cautious.first_day_below_zero}.` : 'Likely and cautious forecasts stay above zero; no borrowing need is projected under these assumptions.'}
          </p>}
          {data.work.alert_checks.filter((r) => r.triggered).map((r) => <p className="log-warning" key={r.alert_id}>Alert {r.alert_id}: {r.reasons.join(' ')}</p>)}
          <p className="dashboard-muted">Warnings are checked when data is opened or refreshed, not by a background notification service.</p>
        </section>}
        {page === 'Invoices & customers' && <div className="dashboard-grid">
          <section className="dashboard-panel"><h3>Customer concentration</h3><p>{workspace.risk.policy_note}</p>
            {workspace.risk.top_customers.map((r) => <ShareBar key={r.customer_id} label={r.customer} value={r.share_percent} detail={`${r.share_percent}% · ${money(r.owed)}`} warning={r.concentration_warning} />)}
          </section>
          <section className="dashboard-panel"><h3>Payment probabilities by invoice</h3>
            <p>See the estimated chance of receiving payment for each unpaid invoice within the next 7, 14, 30 or 60 days.</p>
            <p className="dashboard-muted">These are estimates, not guaranteed payment dates. For partially paid invoices, the estimate applies to the remaining balance.</p>
            {!workspace.payments.available ? <p role="alert">{workspace.payments.reason}</p> : <>
              <p className="dashboard-muted">As of {workspace.payments.as_of}</p>
              <label>Search invoice or customer<input value={search} onChange={(e) => setSearch(e.target.value)} /></label>
              {(workspace.payments.items ?? []).filter((r) => `${r.invoice_id} ${r.name} ${r.customer_id}`.toLowerCase().includes(search.toLowerCase())).slice(0,30).map((r) => <article className="saved-item" key={r.invoice_id}>
                <strong>{r.name} · {r.invoice_id}</strong><p>Remaining {money(r.amount)} · due {r.due_date}</p>
                {Object.entries(r.probabilities).map(([d,p]) => <ShareBar key={d} label={`Within ${d} days`} value={p} detail={`${p}%`} />)}
                <p>Expected receipts within 30 days: {money(r.expected_receipts_30d)}</p>
                <button className="ask-bill" type="button" onClick={() => onAskBill(`Explain the payment forecasts for ${r.name}.`)}>Ask Bill →</button>
              </article>)}<p className="dashboard-muted">Showing up to 30 matching invoices. Search to narrow the results. {workspace.payments.not_listed ? `${workspace.payments.not_listed} more invoices are not loaded.` : ''}</p>
            </>}
          </section>
        </div>}
        {page === 'Transactions & loans' && <div className="dashboard-grid">
          <section className="dashboard-panel"><h3>Recorded transactions</h3><p>{workspace.ledger.cash.source}</p>
            <p>Recorded cash: {money(workspace.ledger.cash.balance)}</p>
            {!workspace.ledger.entries.length && <p>No cash movements recorded since setup.</p>}
            {workspace.ledger.entries.map((r) => <article className="saved-item" key={r.entry_id}><strong>#{r.entry_id} · {r.reference}</strong>
              <p>{r.event_date} · {r.direction === 'in' ? 'Received' : 'Paid'} {money(r.amount)} · {r.category}</p>
              {r.included_in_opening ? <p>Already included in opening cash; no additional cash effect.</p> : null}
              {r.reversal_of && <p>Reverses entry #{r.reversal_of}</p>}<pre>{r.note}</pre></article>)}
          </section>
          <section className="dashboard-panel"><h3>Existing loans</h3>
            {!workspace.ledger.loans.length && <p>No loans recorded.</p>}
            {workspace.ledger.loans.map((r) => <details className="saved-item" key={r.loan_id}><summary>#{r.loan_id} · {r.reference} · {r.cancelled ? 'Cancelled record' : money(r.outstanding_principal) + ' principal outstanding'}</summary>
              <p>Original / recorded principal {money(r.principal)} · {r.annual_rate}% annual nominal rate</p>
              {r.schedule.map((p) => <p key={p.due_date}>{p.due_date}: {money(p.total)} ({money(p.principal)} principal, {money(p.interest)} interest)</p>)}
            </details>)}
            <h4>Unpaid scheduled obligations</h4><p>Use the obligation ID when recording a payment. Past-due bills remain unpaid until reconciled.</p>
            {workspace.ledger.obligations.map((r) => <article className="saved-item" key={r.obligation_id}><strong>{r.label} · {money(r.amount)}</strong><p>{r.due_date}</p><code>{r.obligation_id}</code></article>)}
          </section>
        </div>}
        {page === 'Funding options' && <FundingPanel onSignedOut={onSignedOut} />}
        <div className={`dashboard-grid ${page === 'Cash forecast' ? 'dashboard-single' : ''}`}>
        <section className="dashboard-panel" hidden={page !== 'Overview'} aria-labelledby="cash-heading">
          <h3 id="cash-heading">Cash today</h3>
          <p className="dashboard-muted">{data.cash.balance.account_holder}</p>
          <dl className="cash-totals">
            <div><dt>Recorded cash balance</dt><dd>{money(data.cash.balance.balance, data.cash.balance.currency)}</dd></div>
            <div><dt>Customers owe</dt><dd>{money(data.cash.receivables.owed.amount)}</dd></div>
            <div><dt>Of which overdue</dt><dd>{money(data.cash.receivables.of_which_overdue.amount)}</dd></div>
            <div><dt>Payments due in {data.cash.upcoming_outflows.days} days</dt><dd>{money(data.cash.upcoming_outflows.total)}</dd></div>
          </dl>
          <p>{data.cash.receivables.owed.invoices} unpaid invoices across {data.cash.receivables.owed.customers} customers.</p>
          <table><caption>Overdue invoices by age</caption><thead><tr><th>Days late</th><th>Invoices</th><th>Amount</th></tr></thead>
            <tbody>{Object.entries(data.cash.receivables.overdue_by_days_late).map(([band, row]) => <tr key={band}>
              <td>{band === 'over_60' ? 'Over 60' : band}</td><td>{row.invoices}</td><td>{money(row.amount)}</td>
            </tr>)}</tbody></table>
          <button className="ask-bill" type="button" onClick={() => onAskBill('Who owes me money, and who is late?')}>Ask Bill about overdue invoices →</button>
        </section>

        <section className="dashboard-panel" hidden={page !== 'Overview' && page !== 'Cash forecast'} aria-labelledby="forecast-heading">
          <h3 id="forecast-heading">Next {forecast?.days ?? days} days</h3>
          <label>Forecast horizon<select value={days} onChange={(e) => setDays(Number(e.target.value))}><option value="30">30 days</option><option value="60">60 days</option></select></label>
          {!data.forecast.available || !forecast ? <p role="status">{data.forecast.reason ?? 'Forecast unavailable.'}</p> : <>
            <CashChart series={(['likely','cautious','worst'] as const).map((name) => ({ name: name === 'worst' ? 'No receipts' : name, color: name === 'likely' ? '#163b5c' : name === 'cautious' ? '#2563eb' : '#d97706', points: [...new Map([{date:forecast.as_of,balance:forecast.opening_balance}, ...forecast[name].weekly_balances, {date:forecast[name].lowest_date,balance:forecast[name].lowest_balance}].map((p) => [p.date,p])).values()].sort((a,b) => a.date.localeCompare(b.date)) }))} />
            {(['likely', 'cautious', 'worst'] as const).map((name) => <div className={`scenario scenario-${name}`} key={name}>
              <h4>{name === 'worst' ? 'Worst: no payments received' : name === 'likely' ? 'Likely' : 'Cautious'}</h4>
              <p className="scenario-balance">{money(forecast[name].lowest_balance)}</p>
              <p>Lowest balance on {forecast[name].lowest_date}</p>
              <p>First day below zero: {forecast[name].first_day_below_zero ?? 'None in this forecast'}</p>
              <p>Shortfall: {money(forecast[name].shortfall)} · {forecast[name].days_below_zero} days below zero within this window</p>
            </div>)}
            <p className="dashboard-muted">Weekly checkpoints and each scenario’s minimum, connected for readability; the line is not every daily balance.</p>
            <p className="dashboard-muted">{forecast.scenario_definition}</p>
            <p className="dashboard-muted">Future invoice receipts are estimated from historical sales and payment delays.</p>
            {forecast.company_numbers_are_placeholders && <p className="dashboard-muted">Scheduled company payments use demo placeholders.</p>}
            <details><summary>Forecast limitations</summary><p>{forecast.limitation}</p><p>{forecast.future_invoice_assumption.warning}</p></details>
            <button className="ask-bill" type="button" onClick={() => onAskBill('Can I pay salaries on the 10th and taxes on the 15th?')}>Ask Bill about the forecast →</button>
          </>}
        </section>

        <section className="dashboard-panel" hidden={page !== 'Activity'} aria-labelledby="work-heading">
          <h3 id="work-heading">Bill’s work</h3>
          <h4>Waiting for your answer</h4>
          {!data.work.waiting.items.length && <p className="dashboard-muted">No requests waiting.</p>}
          {data.work.waiting.items.map((item, index) => <div className="saved-item" key={`${item.session_id}-${index}`}>
            <strong>{item.action.replaceAll('_', ' ')}</strong><pre>{item.summary}</pre>
            <p>Expires {item.expires_at}</p><p>Conversation: {item.session_id}</p><p>Answer this request in its chat.</p>
          </div>)}
          <MoreItems count={data.work.waiting.not_listed} />
          <h4>Reminder outbox</h4>
          <p className="dashboard-muted">Saved reminders; nothing is sent outside this demo.</p>
          {data.work.reminders.items.map((item) => <details className="saved-item" key={item.message_id}>
            <summary>{item.customer} · {item.saved_on}</summary><pre>{item.text}</pre>
          </details>)}
          {!data.work.reminders.items.length && <p className="dashboard-muted">No reminders saved.</p>}
          <MoreItems count={data.work.reminders.not_listed} />
          <h4>Alerts</h4>
          {data.work.alerts.items.map((item) => <div className="saved-item" key={item.alert_id}>
            {item.date && <p>Date: {item.date}</p>}
            {item.balance_below !== null && <p>Balance below {money(item.balance_below)}</p>}
            {item.note && <pre>{item.note}</pre>}
          </div>)}
          {!data.work.alerts.items.length && <p className="dashboard-muted">No alerts saved.</p>}
          <MoreItems count={data.work.alerts.not_listed} />
          <h4>Latest plan</h4>
          {data.work.plan ? <div className="saved-item"><p>Saved {data.work.plan.saved_on}</p><pre>{data.work.plan.text}</pre></div> : <p className="dashboard-muted">No plan saved.</p>}
          <h4>Credit request drafts</h4>
          {data.work.credit_request_drafts.items.map((item) => <div className="saved-item" key={item.draft_id}>
            <strong>{item.product_name} · {money(item.amount)}</strong><p>Status: {item.status}</p>
            <p>Draft. Only you can submit it, in the bank app.</p>
          </div>)}
          {!data.work.credit_request_drafts.items.length && <p className="dashboard-muted">No credit requests drafted.</p>}
          <MoreItems count={data.work.credit_request_drafts.not_listed} />
        </section>

        <section className="dashboard-panel" hidden={page !== 'Activity'} aria-labelledby="log-heading">
          <h3 id="log-heading">Activity log</h3>
          <p className={data.log.check.intact ? 'log-intact' : 'log-warning'} role={data.log.check.intact ? undefined : 'alert'}>
            {data.log.check.intact ? 'Activity history verified' : 'Warning: activity history could not be verified'} · {data.log.check.rows} entries
          </p>
          <p className="dashboard-muted">Your 20 most recent requests and account events. Changes require your approval. Bill cannot move money.</p>
          {!data.log.rows.length && <p>No activity recorded.</p>}
          {data.log.rows.map((row) => <article key={row.log_id} className={`activity-row level-${['green', 'yellow', 'red'].includes(row.level) ? row.level : 'owner'}`}>
            <strong>{activityTitle(row.action)}</strong>
            <span className="activity-decision">{activityOutcome(row.decision)}</span>
            <p><time dateTime={row.time}>{activityTime(row.time)}</time></p>
          </article>)}
        </section>
      </div></>}
    </section>
  )
}
