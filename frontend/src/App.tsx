import { useState } from 'react'
import type { FormEvent } from 'react'
import './App.css'

type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
}

const welcomeMessage: Message = {
  id: 'welcome',
  role: 'assistant',
  content:
    'Hello, I’m Rightsize, your business finance assistant. Financial data is not connected yet.',
}

function App() {
  const [messages, setMessages] = useState<Message[]>([welcomeMessage])
  const [input, setInput] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const content = input.trim()
    if (!content) return

    setMessages((previous) => [
      ...previous,
      {
        id: crypto.randomUUID(),
        role: 'user',
        content,
      },
    ])
    setInput('')
  }

  return (
    <main className="chat">
      <header className="chat-header">
        <h1>Rightsize</h1>
        <p>Your business finance assistant</p>
      </header>

      <div className="connection-notice">
        Interface preview — AI replies are not connected yet.
      </div>

      <section
        className="messages"
        aria-label="Conversation"
        aria-live="polite"
        aria-relevant="additions"
      >
        {messages.map((message) => (
          <article
            key={message.id}
            className={`message message-${message.role}`}
          >
            <strong>{message.role === 'user' ? 'You' : 'Rightsize'}</strong>
            <p>{message.content}</p>
          </article>
        ))}
      </section>

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
          />
          <button type="submit" disabled={!input.trim()}>
            Send
          </button>
        </div>
      </form>
    </main>
  )
}

export default App