import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { FormEvent, SetStateAction } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  createChat,
  getStorageError,
  loadChatStore,
  saveChatStore,
  subscribeToStorage,
} from './chatStorage'
import type { Message } from './chatStorage'
import { ApiError, apiRequest } from './api'
import PinForm from './PinForm'
import Dashboard from './Dashboard'
import './App.css'

function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [checkAttempt, setCheckAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    async function checkOwner() {
      try {
        const response = await apiRequest('/me', { signal: controller.signal })
        const data = await response.json()
        if (typeof data.signed_in !== 'boolean') throw new Error('Invalid sign-in response.')
        if (!controller.signal.aborted) setSignedIn(data.signed_in)
      } catch (problem) {
        if (!controller.signal.aborted) {
          if (problem instanceof ApiError && problem.status === 401) setSignedIn(false)
          else setConnectionError(problem instanceof Error ? problem.message : 'Cannot reach the server.')
        }
      }
    }
    void checkOwner()
    return () => controller.abort()
  }, [checkAttempt])

  async function signIn(pin: string) {
    const response = await apiRequest('/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin }),
    })
    const data = await response.json()
    if (data.signed_in !== true) throw new Error('Sign-in was not completed.')
    setSignedIn(true)
  }

  if (signedIn === true) return <ChatApp onSignedOut={() => setSignedIn(false)} />

  return (
    <main className="sign-in-card">
      <img src="/billboard-logo.png" alt="" width="64" height="64" />
      <h1>BillBoard</h1>
      <p>Sign in to your business cash assistant.</p>
      {signedIn === false ? (
        <PinForm label="Owner PIN" submitLabel="Sign in" onSubmit={signIn} />
      ) : connectionError ? (
        <>
          <p role="alert">{connectionError}</p>
          <button type="button" onClick={() => {
            setConnectionError('')
            setCheckAttempt((attempt) => attempt + 1)
          }}>Try again</button>
        </>
      ) : <p role="status">Checking sign-in…</p>}
    </main>
  )
}

function ChatApp({ onSignedOut }: { onSignedOut: () => void }) {
  const [chatStore, setChatStore] = useState(loadChatStore)
  const [input, setInput] = useState('')
  const [page, setPage] = useState<'chat' | 'dashboard'>('chat')
  const [dashboardRevision, setDashboardRevision] = useState(0)
  const [isSending, setIsSending] = useState(false)
  const [isConfirming, setIsConfirming] = useState(false)
  const submittedConfirmations = useRef(new Set<string>())
  const [isUnlocking, setIsUnlocking] = useState(false)
  const [showUnlock, setShowUnlock] = useState(false)
  const [isSigningOut, setIsSigningOut] = useState(false)
  const isBusy = isSending || isConfirming || isUnlocking || isSigningOut
  const [error, setError] = useState<string | null>(null)
  const storageError = useSyncExternalStore(
    subscribeToStorage,
    getStorageError,
  )

  const activeChat = chatStore.chats.find(
    (chat) => chat.id === chatStore.activeChatId,
  )!

  const messages = activeChat.messages
  const lockEvent = messages.flatMap((message) => [
    ...(message.actions ?? []).filter((step) =>
      (step.name === 'unlock' && step.status === 'ok') || /session is locked/i.test(step.label)),
    ...(/session is locked/i.test(message.content)
      ? [{ name: 'lock', status: 'locked', label: message.content }] : []),
  ]).at(-1)
  const isLocked = !!lockEvent && lockEvent.name !== 'unlock'

  function ownerRequest(path: string, options: RequestInit = {}) {
    return apiRequest(path, options, onSignedOut)
  }

  async function signOut() {
    if (isBusy) return
    setIsSigningOut(true)
    try {
      await ownerRequest('/logout', { method: 'POST' })
      saveChatStore(chatStore)
      onSignedOut()
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'Unable to sign out.')
    } finally {
      setIsSigningOut(false)
    }
  }

  async function unlock(pin: string) {
    setIsUnlocking(true)
    try {
      const response = await ownerRequest('/unlock', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: activeChat.id, pin }),
      })
      const data = await response.json()
      if (data.status !== 'ok') throw new Error(data.message ?? data.reason ?? 'The chat could not be unlocked.')
      setMessages((previous) => [...previous, {
        id: crypto.randomUUID(), role: 'assistant',
        content: 'This chat is unlocked. The action permission rules still apply.',
        actions: [{ name: 'unlock', status: 'ok', label: 'Chat unlocked after PIN verification.' }],
      }])
      setShowUnlock(false)
    } catch (problem) {
      if (!(problem instanceof ApiError && problem.status === 401)) {
        const detail = problem instanceof Error ? problem.message : 'Unable to unlock the chat.'
        setMessages((previous) => [...previous, {
          id: crypto.randomUUID(), role: 'assistant', content: 'Unlock was not completed.',
          actions: [{ name: 'unlock', status: 'error', label: detail }],
        }])
      }
      throw problem
    } finally {
      setIsUnlocking(false)
    }
  }

  function setMessages(update: SetStateAction<Message[]>) {
    const chatId = activeChat.id

    setChatStore((previous) => ({
      ...previous,
      chats: previous.chats.map((chat) => {
        if (chat.id !== chatId) return chat

        const nextMessages =
          typeof update === 'function'
            ? update(chat.messages)
            : update

        const firstUserMessage = nextMessages.find(
          (message) => message.role === 'user',
        )

        return {
          ...chat,
          title: firstUserMessage?.content.slice(0, 40) || 'New chat',
          messages: nextMessages,
        }
      }),
    }))
  }

  useEffect(() => {
    if (isSending) return

    saveChatStore(chatStore)
  }, [chatStore, isSending])

  function startNewChat() {
    if (isBusy) return

    const chat = createChat()

    setChatStore((previous) => ({
      chats: [chat, ...previous.chats],
      activeChatId: chat.id,
    }))

    setInput('')
    setError(null)
    setShowUnlock(false)
    setPage('chat')
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const content = input.trim()
    if (!content || isBusy || showUnlock) return

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: 'user',
      content,
    }

    const assistantId = crypto.randomUUID()
    const conversation = [...messages, userMessage]

    const history = conversation
      .filter((message) => message.id !== 'welcome')
      .map(({ role, content, confirmation }) => ({
        role,
        content: confirmation?.outcome
          ? `${content}\nApproval result: ${confirmation.outcome}`
          : content,
      }))

    if (history.length > 20) {
      setError('This demo supports 20 messages. Click New chat to start again.')
      return
    }

    setMessages([
      ...conversation,
      { id: assistantId, role: 'assistant', content: '' },
    ])
    setInput('')
    setError(null)
    setIsSending(true)

    let receivedText = false
    let receivedAction = false
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined

    try {
      const response = await ownerRequest('/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history, session_id: activeChat.id }),
      })

      if (!response.ok) {
        throw new Error(`Chat request failed (${response.status}).`)
      }

      if (!response.body) {
        throw new Error('Streaming is unavailable.')
      }

      reader = response.body.getReader()

      const decoder = new TextDecoder()
      let buffer = ''
      let completed = false
      let reply = ''

      function processLine(line: string) {
        if (!line.trim()) return

        const data: unknown = JSON.parse(line)

        if (
          typeof data !== 'object' ||
          data === null ||
          !('type' in data)
        ) {
          throw new Error('Invalid stream event.')
        }

        if (data.type === 'chunk') {
          if (!('content' in data) || typeof data.content !== 'string') {
            throw new Error('Invalid reply fragment.')
          }

          reply += data.content
          receivedText = receivedText || data.content.length > 0

          const currentReply = reply

          setMessages((previous) =>
            previous.map((message) =>
              message.id === assistantId
                ? { ...message, content: currentReply }
                : message,
            ),
          )
        } else if (data.type === 'action') {
          if (!('name' in data) || typeof data.name !== 'string'
            || !('status' in data) || typeof data.status !== 'string'
            || !('label' in data) || typeof data.label !== 'string') {
            throw new Error('Invalid action event.')
          }
          receivedAction = true
          const step = { name: data.name, status: data.status, label: data.label }
          setMessages((previous) => previous.map((message) =>
            message.id === assistantId
              ? { ...message, actions: [...(message.actions ?? []), step] }
              : message,
          ))
        } else if (data.type === 'confirmation') {
          if (!('confirmation_id' in data) || typeof data.confirmation_id !== 'string'
            || !('summary' in data) || typeof data.summary !== 'string') {
            throw new Error('Invalid confirmation event.')
          }
          const confirmation = { confirmationId: data.confirmation_id, summary: data.summary }
          setMessages((previous) => previous.map((message) =>
            message.id === assistantId ? { ...message, confirmation } : message,
          ))
        } else if (data.type === 'done') {
          completed = true
        } else if (data.type === 'error') {
          throw new Error(
            'message' in data && typeof data.message === 'string'
              ? data.message
              : 'The reply failed.',
          )
        } else {
          throw new Error('Unknown stream event.')
        }
      }

      function processBufferedLines() {
        let newline = buffer.indexOf('\n')

        while (newline !== -1 && !completed) {
          const line = buffer.slice(0, newline)
          buffer = buffer.slice(newline + 1)
          processLine(line)
          newline = buffer.indexOf('\n')
        }
      }

      while (!completed) {
        const { value, done } = await reader.read()

        if (done) {
          buffer += decoder.decode()
          processBufferedLines()

          if (!completed && buffer.trim()) {
            processLine(buffer)
          }

          break
        }

        buffer += decoder.decode(value, { stream: true })
        processBufferedLines()
      }

      if (!completed || !reply.trim()) {
        throw new Error('The reply ended before completion.')
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : 'Unable to send the message.'
      if (receivedAction) {
        // Keep execution evidence: a failed reply does not undo an action.
        setMessages((previous) => previous.map((message) =>
          message.id === assistantId
            ? { ...message, content: `${message.content}\nReply interrupted. Review the action steps before trying again.` }
            : message,
        ))
        setError(detail)
      } else {
      setMessages((previous) =>
        previous.filter(
          (message) =>
            message.id !== userMessage.id &&
            message.id !== assistantId,
        ),
      )
      setInput(content)

      setError(
        receivedText
          ? `${detail} The incomplete reply was removed; you can retry.`
          : detail,
      )
      }
    } finally {
      if (reader) {
        try {
          await reader.cancel()
        } catch {
          // The connection may already be closed.
        }
        reader.releaseLock()
      }

      setIsSending(false)
    }
  }

  function selectChat(chatId: string) {
    if (isBusy) return

    setChatStore((previous) => ({
      ...previous,
      activeChatId: chatId,
    }))

    setInput('')
    setError(null)
    setShowUnlock(false)
    setPage('chat')
  }

  async function handleConfirmation(message: Message, approved: boolean) {
    const confirmation = message.confirmation
    if (!confirmation || confirmation.outcome || isBusy
      || submittedConfirmations.current.has(confirmation.confirmationId)) return

    // Claim immediately: two clicks before React renders still send once.
    submittedConfirmations.current.add(confirmation.confirmationId)
    setIsConfirming(true)
    setError(null)
    let outcome = 'Approval response was lost. The action may have run; do not retry it automatically.'
    let status = 'unknown'
    let signedOut = false

    try {
      const response = await ownerRequest('/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirmation_id: confirmation.confirmationId,
          approved,
          session_id: activeChat.id,
        }),
      })
      if (!response.ok) throw new Error(`Approval request failed (${response.status}).`)
      const data = await response.json()
      if (data.status === 'ok' && data.result && typeof data.result === 'object') {
        status = 'ok'
        outcome = data.result.saved_to_outbox === true
          ? 'Reminder saved to the outbox; nothing was sent externally.'
          : data.result.simulated === true
            ? 'Reminder simulated; nothing was sent externally.'
            : typeof data.result.message === 'string'
              ? data.result.message
              : 'Approved action completed.'
      } else if (data.status === 'refused' && typeof data.reason === 'string') {
        status = 'refused'
        outcome = data.reason
      } else if (data.status === 'error' && typeof data.message === 'string') {
        status = 'error'
        outcome = `${data.message} If this request is unavailable, it may have been used or the server restarted.`
      } else {
        throw new Error('Invalid approval response.')
      }
    } catch (error) {
      signedOut = error instanceof ApiError && error.status === 401
      if (signedOut) submittedConfirmations.current.delete(confirmation.confirmationId)
      setError(error instanceof Error ? error.message : 'Unable to confirm the action.')
    } finally {
      const finalOutcome = outcome
      const finalStatus = status
      if (!signedOut) setDashboardRevision((value) => value + 1)
      if (!signedOut) setMessages((previous) => previous.map((item) => item.id === message.id
        ? { ...item, confirmation: { ...confirmation, outcome: finalOutcome },
          actions: [...(item.actions ?? []), { name: 'confirmation', status: finalStatus, label: finalOutcome }] }
        : item))
      setIsConfirming(false)
    }
  }

  return (
  <div className="app-layout">
    <aside className="chat-sidebar" aria-label="Saved conversations">
      <h2>Conversations</h2>

      <nav className="chat-list" aria-label="Select a conversation">
        {chatStore.chats.map((chat) => (
          <button
            key={chat.id}
            type="button"
            className={`chat-list-item ${
              chat.id === chatStore.activeChatId ? 'is-active' : ''
            }`}
            aria-current={
              chat.id === chatStore.activeChatId ? 'true' : undefined
            }
            onClick={() => selectChat(chat.id)}
            disabled={isBusy}
            title={chat.title}
          >
            {chat.title}
          </button>
        ))}
      </nav>
    </aside>

    <main className="chat">
      <header className="chat-header">
        <div className="brand">
          <img className="brand-logo" src="/billboard-logo.png" alt="" width="48" height="48" />
          <div>
            <h1>BillBoard</h1>
            <p>Bill, your business cash assistant</p>
          </div>
        </div>

        <div className="header-controls">
        <button type="button" className="new-chat-button" disabled={isBusy}
          onClick={() => { setShowUnlock(false); setPage(page === 'chat' ? 'dashboard' : 'chat') }}>
          {page === 'chat' ? 'Dashboard' : 'Chat'}
        </button>
        <button type="button" className="new-chat-button" onClick={signOut} disabled={isBusy}>
          {isSigningOut ? 'Signing out…' : 'Sign out'}
        </button>
        <button
          type="button"
          className="new-chat-button"
          onClick={startNewChat}
          disabled={isBusy}
        >
          New chat
        </button>
        </div>
      </header>

      <Dashboard active={page === 'dashboard'} revision={dashboardRevision} onSignedOut={onSignedOut}
        onAskBill={(question) => {
          startNewChat()
          setInput(question)
        }} />
      <div className="chat-body" hidden={page !== 'chat'}>
      <div className="connection-notice">
        Demo actions use fictional data. Real financial data is not connected yet.
      </div>

      <section
        className="messages"
        aria-label="Conversation"
        aria-live="polite"
        aria-relevant="additions"
        aria-busy={isSending}
      >
        {messages.map((message) => (
          <article
            key={message.id}
            className={`message message-${message.role}`}
          >
            <strong>
              {message.role === 'user' ? 'You' : 'Bill'}
            </strong>
            {message.actions && (
              <ul className="action-steps" aria-label="Action steps">
                {message.actions.map((step, index) => (
                  <li key={`${step.name}-${index}`}>{step.label}</li>
                ))}
              </ul>
            )}
            <div className="message-content">
              {message.role === 'assistant' ? (
                <ReactMarkdown
                  skipHtml
                  components={{ img: ({ alt }) => <span>{alt}</span> }}
                >
                  {message.content}
                </ReactMarkdown>
              ) : (
                <p>{message.content}</p>
              )}
            </div>
            {message.confirmation && (
              <div className="confirmation-notice">
                <p>{message.confirmation.summary}</p>
                {message.confirmation.outcome ? (
                  <p>{message.confirmation.outcome}</p>
                ) : (
                  <div className="confirmation-buttons">
                    <button type="button" disabled={isBusy}
                      onClick={() => handleConfirmation(message, true)}>Yes</button>
                    <button type="button" disabled={isBusy}
                      onClick={() => handleConfirmation(message, false)}>No</button>
                  </div>
                )}
              </div>
            )}
          </article>
        ))}
      </section>

      {isLocked && (
        <section className="unlock-notice" aria-label="Unlock this chat">
          <p>This chat is locked. Verify your PIN to unlock it.</p>
          {showUnlock ? (
            <>
              <PinForm key={activeChat.id} label="Owner PIN" submitLabel="Unlock chat" onSubmit={unlock} />
              <button type="button" disabled={isUnlocking} onClick={() => setShowUnlock(false)}>Cancel</button>
            </>
          ) : <button type="button" disabled={isBusy} onClick={() => setShowUnlock(true)}>Unlock</button>}
        </section>
      )}
      {isSending && <p role="status">Bill is working…</p>}
      {isConfirming && <p role="status">Recording your decision…</p>}
      {error && <p role="alert">{error}</p>}
      {storageError && <p role="alert">{storageError}</p>}

      <form className="composer" onSubmit={handleSubmit}>
        <label htmlFor="message">Your message</label>

        <div className="composer-controls">
          <input
            id="message"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Type a message…"
            maxLength={4000}
            autoComplete="off"
            disabled={isBusy || showUnlock}
          />

          <button
            type="submit"
            disabled={isBusy || showUnlock || !input.trim()}
          >
            {isSending ? 'Waiting…' : 'Send'}
          </button>
        </div>
      </form>
      </div>
    </main>
  </div>
)
}

export default App
