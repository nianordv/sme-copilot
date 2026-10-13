export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

// The browser sends the HttpOnly cookie. We never read or store its token.
export async function apiRequest(
  path: string,
  options: RequestInit = {},
  onSignedOut?: () => void,
) {
  const response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin' })
  if (response.ok) return response
  if (response.status === 401) onSignedOut?.()

  let message = `Request failed (${response.status}).`
  try {
    const data = await response.json()
    if (typeof data.detail === 'string') message = data.detail
  } catch {
    // A proxy or connection failure may not have a JSON body.
  }
  throw new ApiError(message, response.status)
}
