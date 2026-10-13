import { useRef, useState } from 'react'
import type { FormEvent } from 'react'

export default function PinForm({ label, submitLabel, onSubmit }: {
  label: string
  submitLabel: string
  onSubmit: (pin: string) => Promise<void>
}) {
  const [pin, setPin] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submitting = useRef(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!pin || submitting.current) return
    submitting.current = true
    const given = pin
    setPin('')
    setError('')
    setBusy(true)
    try {
      await onSubmit(given)
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Unable to verify the PIN.')
    } finally {
      submitting.current = false
      setBusy(false)
    }
  }

  return (
    <form className="pin-form" onSubmit={submit}>
      <label>
        {label}
        <input type="password" inputMode="numeric" autoComplete="off"
          value={pin} maxLength={64} required disabled={busy}
          onChange={(event) => setPin(event.target.value)} />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy || !pin}>{busy ? 'Checking…' : submitLabel}</button>
    </form>
  )
}
