import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { ApiError, apiRequest } from '../src/api.ts'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

test('protected requests include the browser cookie', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/me')
    assert.equal(options?.credentials, 'same-origin')
    return new Response(JSON.stringify({ signed_in: true }), { status: 200 })
  }
  assert.equal((await apiRequest('/me')).status, 200)
})

test('an expired login returns the screen to sign-in', async () => {
  let expired = false
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Not signed in.' }), { status: 401 })
  await assert.rejects(apiRequest('/confirm', {}, () => { expired = true }),
    (error) => error instanceof ApiError && error.status === 401)
  assert.equal(expired, true)
})

test('a wrong unlock PIN shows its message without signing out', async () => {
  let expired = false
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Wrong PIN.' }), { status: 403 })
  await assert.rejects(apiRequest('/unlock', {}, () => { expired = true }), /Wrong PIN/)
  assert.equal(expired, false)
})

test('attempt limits preserve the server explanation', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'Too many wrong PINs. Try again in 5 minutes.' }), { status: 429 })
  await assert.rejects(apiRequest('/login'), /Try again in 5 minutes/)
})

test('validation errors do not display a submitted PIN', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ input: 'private-pin' }] }), { status: 422 })
  await assert.rejects(apiRequest('/login'), (error) =>
    error instanceof ApiError && error.message === 'Request failed (422).')
})
