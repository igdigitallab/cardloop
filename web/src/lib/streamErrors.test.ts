// The bubble text for a broken chat stream (lib/streamErrors.ts).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { CONNECTION_LOST_MSG, isConnectionDrop, streamErrorText } from './streamErrors'

test('browser transport failures read as a lost connection, not a dead agent', () => {
  for (const m of ['network error', 'Failed to fetch', 'Load failed', 'The network connection was lost.',
    'NetworkError when attempting to fetch resource.', 'Error in input stream',
    'net::ERR_CONNECTION_RESET', 'net::ERR_INTERNET_DISCONNECTED']) {
    assert.equal(isConnectionDrop(new TypeError(m)), true, m)
    assert.equal(streamErrorText(new TypeError(m)), CONNECTION_LOST_MSG, m)
  }
})

test('a bug in our own code keeps its real message', () => {
  const bug = new TypeError("Cannot read properties of undefined (reading 'text')")
  assert.equal(isConnectionDrop(bug), false)
  assert.equal(streamErrorText(bug), bug.message)
  assert.equal(streamErrorText(new Error('HTTP 502')), 'HTTP 502')
  assert.equal(streamErrorText('plain'), 'plain')
  assert.equal(isConnectionDrop('network error'), false)
})
