import { useEffect, useState, useSyncExternalStore } from 'react'
import type { FormEvent, SetStateAction } from 'react'
import {
  createChat,
  getStorageError,
  loadChatStore,
  saveChatStore,
  subscribeToStorage,
} from './chatStorage'
import type { Message } from './chatStorage'
import './App.css'

function App() {
  const [chatStore, setChatStore] = useState(loadChatStore)
  const [input, setInput] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const storageError = useSyncExternalStore(
    subscribeToStorage,
    getStorageError,
  )

  const activeChat = chatStore.chats.find(
    (chat) => chat.id === chatStore.activeChatId,
  )!

  const messages = activeChat.messages

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
    if (isSending) return

    const chat = createChat()

    setChatStore((previous) => ({
      chats: [chat, ...previous.chats],
      activeChatId: chat.id,
    }))

    setInput('')
    setError(null)
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const content = input.trim()
    if (!content || isSending) return

    const userMessage: Message = {
      id: crypto.randomUUID(),
      role: 'user',
      content,
    }

    const assistantId = crypto.randomUUID()
    const conversation = [...messages, userMessage]

    const history = conversation
      .filter((message) => message.id !== 'welcome')
      .map(({ role, content }) => ({ role, content }))

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
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined

    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: history }),
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
      setMessages((previous) =>
        previous.filter(
          (message) =>
            message.id !== userMessage.id &&
            message.id !== assistantId,
        ),
      )
      setInput(content)

      const detail =
        error instanceof Error
          ? error.message
          : 'Unable to send the message.'

      setError(
        receivedText
          ? `${detail} The incomplete reply was removed; you can retry.`
          : detail,
      )
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
    if (isSending) return

    setChatStore((previous) => ({
      ...previous,
      activeChatId: chatId,
    }))

    setInput('')
    setError(null)
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
            disabled={isSending}
            title={chat.title}
          >
            {chat.title}
          </button>
        ))}
      </nav>
    </aside>

    <main className="chat">
      <header className="chat-header">
        <div>
          <h1>Rightsize</h1>
          <p>Your business finance assistant</p>
        </div>

        <button
          type="button"
          className="new-chat-button"
          onClick={startNewChat}
          disabled={isSending}
        >
          New chat
        </button>
      </header>

      <div className="connection-notice">
        Local AI chat — financial data is not connected yet.
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
              {message.role === 'user' ? 'You' : 'Rightsize'}
            </strong>
            <p>{message.content}</p>
          </article>
        ))}
      </section>

      {isSending && <p role="status">Rightsize is replying…</p>}
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
            disabled={isSending}
          />

          <button
            type="submit"
            disabled={isSending || !input.trim()}
          >
            {isSending ? 'Waiting…' : 'Send'}
          </button>
        </div>
      </form>
    </main>
  </div>
)
}

export default App
