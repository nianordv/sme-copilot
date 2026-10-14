import { useEffect, useRef, useState } from 'react'
import { apiRequest } from './api'

interface SavedList<T> { items: T[]; not_listed: number }
interface Scenario {
  lowest_balance: number; lowest_date: string; first_day_below_zero: string | null
  shortfall: number; weekly_balances: { date: string; balance: number }[]
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
    scenario_definition: string; limitation: string; company_numbers_are_placeholders: boolean
    future_invoice_assumption: { warning?: string }
  } }
  work: {
    waiting: SavedList<{ session_id: string; action: string; summary: string; expires_at: string }>
    reminders: SavedList<{ message_id: number; customer: string; saved_on: string; text: string }>
    alerts: SavedList<{ alert_id: number; date: string | null; balance_below: number | null; note: string | null }>
    plan: { saved_on: string; text: string } | null
    credit_request_drafts: SavedList<{ draft_id: number; product_name: string; amount: number; status: string }>
  }
  log: {
    check: { intact: boolean; rows: number }
    rows: { log_id: number; time: string; action: string; level: string; decision: string; result: string; arguments: string }[]
  }
}

function money(value: number, currency = 'GEL') {
  return `${currency} ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function MoreItems({ count }: { count: number }) {
  return count > 0 ? <p className="dashboard-muted">{count} older items not shown.</p> : null
}

export default function Dashboard({ active, revision, onSignedOut, onAskBill }: {
  active: boolean; revision: number; onSignedOut: () => void; onAskBill: (question: string) => void
}) {
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const fetchedRevision = useRef(0)

  useEffect(() => {
    if (!active && revision === fetchedRevision.current) return
    fetchedRevision.current = revision
    const controller = new AbortController()
    async function load() {
      setLoading(true)
      setError('')
      try {
        const response = await apiRequest('/dashboard', { signal: controller.signal }, onSignedOut)
        const next = await response.json()
        if (!next.cash?.balance || !next.cash?.receivables || !next.cash?.upcoming_outflows
          || !next.forecast || !next.work || !next.log?.check || !Array.isArray(next.log.rows)) {
          throw new Error('The dashboard response is incomplete.')
        }
        if (!controller.signal.aborted) setData(next)
      } catch (problem) {
        if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : 'Cannot load the dashboard.')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    void load()
    return () => controller.abort()
  }, [active, revision, refresh, onSignedOut])

  const forecast = data?.forecast.result
  return (
    <section className="dashboard" hidden={!active} aria-label="Business dashboard" aria-busy={loading}>
      <div className="dashboard-heading">
        <div><h2>Your business at a glance</h2>{data && <p>Books as of {data.cash.as_of}</p>}</div>
        <button className="new-chat-button" type="button" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
      </div>
      {loading && <p role="status">Loading the dashboard…</p>}
      {error && <p role="alert">{error} {data && 'The figures below are from the previous successful load.'}</p>}
      {data && <div className="dashboard-grid">
        <section className="dashboard-panel" aria-labelledby="cash-heading">
          <h3 id="cash-heading">Cash today</h3>
          <p className="dashboard-muted">{data.cash.balance.account_holder}</p>
          <dl className="cash-totals">
            <div><dt>Bank balance</dt><dd>{money(data.cash.balance.balance, data.cash.balance.currency)}</dd></div>
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

        <section className="dashboard-panel" aria-labelledby="forecast-heading">
          <h3 id="forecast-heading">Next 30 days</h3>
          {!data.forecast.available || !forecast ? <p role="status">{data.forecast.reason ?? 'Forecast unavailable.'}</p> : <>
            {(['likely', 'cautious', 'worst'] as const).map((name) => <div className={`scenario scenario-${name}`} key={name}>
              <h4>{name === 'worst' ? 'Worst: no payments received' : name === 'likely' ? 'Likely' : 'Cautious'}</h4>
              <p className="scenario-balance">{money(forecast[name].lowest_balance)}</p>
              <p>Lowest balance on {forecast[name].lowest_date}</p>
              <p>First day below zero: {forecast[name].first_day_below_zero ?? 'None in this forecast'}</p>
              <p>Shortfall: {money(forecast[name].shortfall)}</p>
            </div>)}
            <p className="dashboard-muted">{forecast.scenario_definition}</p>
            <p className="dashboard-muted">Future invoice receipts are estimated from historical sales and payment delays.</p>
            {forecast.company_numbers_are_placeholders && <p className="dashboard-muted">Company balance and scheduled payments use demo placeholders.</p>}
            <details><summary>Forecast limitations</summary><p>{forecast.limitation}</p><p>{forecast.future_invoice_assumption.warning}</p></details>
            <button className="ask-bill" type="button" onClick={() => onAskBill('Can I pay salaries on the 10th and taxes on the 15th?')}>Ask Bill about the forecast →</button>
          </>}
        </section>

        <section className="dashboard-panel" aria-labelledby="work-heading">
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

        <section className="dashboard-panel" aria-labelledby="log-heading">
          <h3 id="log-heading">Activity log</h3>
          <p className={data.log.check.intact ? 'log-intact' : 'log-warning'} role={data.log.check.intact ? undefined : 'alert'}>
            {data.log.check.intact ? 'Log intact' : 'Warning: log integrity check failed'} · {data.log.check.rows} recorded rows
          </p>
          <p className="dashboard-muted">Newest 20 entries. Green: lookups; Yellow: approval required; Red: prohibited; Owner: sign-in and verification.</p>
          {!data.log.rows.length && <p>No activity recorded.</p>}
          {data.log.rows.map((row) => <article key={row.log_id} className={`activity-row level-${['green', 'yellow', 'red'].includes(row.level) ? row.level : 'owner'}`}>
            <strong>{row.action.replaceAll('_', ' ')}</strong><span className="activity-decision">{row.level} · {row.decision}</span>
            <p><time>{row.time}</time></p><pre>{row.result}</pre>
            <details><summary>Arguments</summary><pre>{row.arguments}</pre></details>
          </article>)}
        </section>
      </div>}
    </section>
  )
}
