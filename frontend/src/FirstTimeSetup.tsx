import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { apiRequest } from './api'
import BookkeepingForm from './BookkeepingForm'
import type { PendingUpdate } from './BookkeepingForm'

export default function FirstTimeSetup({ children, onSignedOut }: {
  children: ReactNode; onSignedOut: () => void
}) {
  const [cash, setCash] = useState<{ initialized: boolean; as_of: string } | null>(null)
  const [pending, setPending] = useState<PendingUpdate | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [consumedId, setConsumedId] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [sessionId] = useState(() => crypto.randomUUID())
  const usedConfirmations = useRef(new Set<string>())

  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await apiRequest('/workspace', { signal: controller.signal }, onSignedOut)
        const data = await response.json()
        if (typeof data.ledger?.cash?.initialized !== 'boolean' || !data.ledger.cash.as_of) {
          throw new Error('Unable to check your starting balance.')
        }
        if (!controller.signal.aborted) { setCash(data.ledger.cash); setError('') }
      } catch (problem) {
        if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : 'Unable to load setup.')
      }
    }
    void load()
    return () => controller.abort()
  }, [attempt, onSignedOut])

  async function confirm(approved: boolean) {
    if (!pending || busy || usedConfirmations.current.has(pending.confirmation_id)) return
    usedConfirmations.current.add(pending.confirmation_id)
    setConsumedId(pending.confirmation_id)
    setBusy(true); setError('')
    try {
      const response = await apiRequest('/confirm', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmation_id: pending.confirmation_id, approved, session_id: sessionId }),
      }, onSignedOut)
      const result = await response.json()
      if (approved && result.status !== 'ok') {
        throw new Error(result.message ?? result.reason ?? 'Starting balance was not saved. Please review it again.')
      }
      setPending(null)
      if (approved) setAttempt((value) => value + 1)
    } catch (problem) {
      // Never retry an approval automatically: it may already have succeeded.
      setError(problem instanceof Error ? problem.message : 'Could not confirm. Check your setup before trying again.')
    } finally { setBusy(false) }
  }

  if (cash?.initialized) return children
  return <main className="setup-card">
    <img src="/billboard-logo.png" alt="" width="64" height="64" />
    <h1>Welcome to BillBoard</h1>
    <p>Let’s set up your business before you start.</p>
    {error && <p role="alert">{error}</p>}
    {!cash && !error && <p role="status">Checking your setup…</p>}
    {cash && !pending && <BookkeepingForm setup initialized={false} asOf={cash.as_of}
      sessionId={sessionId} onSignedOut={onSignedOut} onPending={setPending} />}
    {pending && <section className="dashboard-panel">
      <h2>Confirm your starting balance</h2>
      <p>{pending.summary}</p>
      <p>This is a one-time starting point. Future receipts and payments will update your cash.</p>
      <button className="new-chat-button" disabled={busy || consumedId === pending.confirmation_id}
        onClick={() => void confirm(true)}>Confirm balance</button>
      <button className="new-chat-button" disabled={busy || consumedId === pending.confirmation_id}
        onClick={() => void confirm(false)}>Go back</button>
    </section>}
    {error && <button className="new-chat-button" disabled={busy} onClick={() => {
      setPending(null); setAttempt((value) => value + 1)
    }}>Check setup again</button>}
  </main>
}
