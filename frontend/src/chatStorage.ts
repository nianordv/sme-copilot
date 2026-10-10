export type ActionStep = {
  name: string
  status: string
  label: string
}

export type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  actions?: ActionStep[]
  confirmation?: { confirmationId: string; summary: string; outcome?: string }
}

export type Chat = {
  id: string
  title: string
  messages: Message[]
}

export type ChatStore = {
  chats: Chat[]
  activeChatId: string
}

export const CHAT_STORAGE_KEY = 'rightsize.chats.v1'

const OLD_STORAGE_KEY = 'rightsize.current-chat.v1'

// Storage failures are external-system status, separate from chat request errors.
let storageError: string | null = null
const storageListeners = new Set<() => void>()

export function subscribeToStorage(listener: () => void) {
  storageListeners.add(listener)
  return () => {
    storageListeners.delete(listener)
  }
}

export function getStorageError() {
  return storageError
}

export function saveChatStore(store: ChatStore) {
  let nextError: string | null = null

  try {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(store))
  } catch {
    nextError = 'Chat history could not be saved in this browser.'
  }

  if (nextError !== storageError) {
    storageError = nextError
    storageListeners.forEach((listener) => listener())
  }
}

export function createChat(): Chat {
  return {
    id: crypto.randomUUID(),
    title: 'New chat',
    messages: [
      {
        id: 'welcome',
        role: 'assistant',
        content:
          'Hello, I’m Bill, your BillBoard cash assistant. I can demonstrate actions using clearly fictional data. Real financial data is not connected yet.',
      },
    ],
  }
}

function isMessage(value: unknown): value is Message {
  return (
    typeof value === 'object' &&
    value !== null &&
    'id' in value &&
    typeof value.id === 'string' &&
    'role' in value &&
    (value.role === 'user' || value.role === 'assistant') &&
    'content' in value &&
    typeof value.content === 'string'
    && (!('actions' in value) || (Array.isArray(value.actions) && value.actions.every(
      (step: unknown) => typeof step === 'object' && step !== null
        && 'name' in step && typeof step.name === 'string'
        && 'status' in step && typeof step.status === 'string'
        && 'label' in step && typeof step.label === 'string',
    )))
    && (!('confirmation' in value) || (typeof value.confirmation === 'object'
      && value.confirmation !== null
      && 'confirmationId' in value.confirmation && typeof value.confirmation.confirmationId === 'string'
      && 'summary' in value.confirmation && typeof value.confirmation.summary === 'string'
      && (!('outcome' in value.confirmation) || typeof value.confirmation.outcome === 'string')))
  )
}

function isChat(value: unknown): value is Chat {
  return (
    typeof value === 'object' &&
    value !== null &&
    'id' in value &&
    typeof value.id === 'string' &&
    'title' in value &&
    typeof value.title === 'string' &&
    'messages' in value &&
    Array.isArray(value.messages) &&
    value.messages.length > 0 &&
    value.messages.every(isMessage)
  )
}

export function loadChatStore(): ChatStore {
  try {
    const saved = localStorage.getItem(CHAT_STORAGE_KEY)

    if (saved) {
      const data: unknown = JSON.parse(saved)

      if (
        typeof data === 'object' &&
        data !== null &&
        'chats' in data &&
        Array.isArray(data.chats) &&
        data.chats.length > 0 &&
        data.chats.every(isChat) &&
        'activeChatId' in data &&
        typeof data.activeChatId === 'string'
      ) {
        const ids = new Set(data.chats.map((chat) => chat.id))

        if (ids.size === data.chats.length) {
          return {
            chats: data.chats,
            activeChatId: ids.has(data.activeChatId)
              ? data.activeChatId
              : data.chats[0].id,
          }
        }
      }
    }

    // Preserve the conversation saved by our previous version.
    const oldSaved = localStorage.getItem(OLD_STORAGE_KEY)

    if (oldSaved) {
      const messages: unknown = JSON.parse(oldSaved)

      if (
        Array.isArray(messages) &&
        messages.length > 0 &&
        messages.every(isMessage)
      ) {
        const chat = createChat()
        chat.messages = messages

        const firstUserMessage = messages.find(
          (message) => message.role === 'user',
        )

        chat.title = firstUserMessage?.content.slice(0, 40) || 'New chat'

        return { chats: [chat], activeChatId: chat.id }
      }
    }
  } catch {
    // Start a fresh conversation if saved data cannot be read.
  }

  const chat = createChat()
  return { chats: [chat], activeChatId: chat.id }
}
