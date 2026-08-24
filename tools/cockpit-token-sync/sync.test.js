const assert = require('node:assert/strict')
const test = require('node:test')

const {
  accessTokenIssuedAt,
  credentialDirection,
  credentialsFromLocal,
  hasServerRateLimitState,
  mergeServerCredentials,
  openAIQuotaIsAvailable,
  remoteRequest,
  sameCredentialFields,
  serverCredentialsChanged,
  syncCredentialDirection,
} = require('./sync.js')

function unsignedJwt(payload) {
  return [
    Buffer.from('{}').toString('base64url'),
    Buffer.from(JSON.stringify(payload)).toString('base64url'),
    'signature',
  ].join('.')
}

function localAccount(accessToken = unsignedJwt({ exp: 1_800_000_000 })) {
  return {
    email: 'account@example.com',
    account_id: 'account-id',
    user_id: 'user-id',
    organization_id: 'org-id',
    plan_type: 'pro',
    subscription_active_until: '2026-09-01T00:00:00Z',
    tokens: {
      access_token: accessToken,
      refresh_token: 'refresh-token',
      id_token: 'id-token',
    },
  }
}

test('derives server expires_at from the local access token JWT', () => {
  const credentials = credentialsFromLocal(localAccount())

  assert.equal(credentials.expires_at, '2027-01-15T08:00:00.000Z')
})

test('detects missing or semantically different OAuth metadata', () => {
  const credentials = credentialsFromLocal(localAccount())
  const serverCredentials = {
    ...credentials,
    expires_at: '2027-01-15T08:00:00+00:00',
    subscription_expires_at: '2026-09-01T08:00:00+08:00',
  }

  assert.equal(sameCredentialFields(credentials, serverCredentials), true)
  assert.equal(sameCredentialFields(credentials, { ...serverCredentials, expires_at: undefined }), false)
})

test('preserves server-only OAuth fields while applying local credentials', () => {
  const credentials = credentialsFromLocal(localAccount())
  const merged = mergeServerCredentials({
    credentials: {
      client_id: 'server-client-id',
      _token_version: 123,
      access_token: 'old-access-token',
    },
  }, credentials, 456)

  assert.equal(merged.client_id, 'server-client-id')
  assert.equal(merged._token_version, 456)
  assert.equal(merged.access_token, credentials.access_token)
  assert.equal(merged.expires_at, credentials.expires_at)
})

test('recognizes available upstream quota and server runtime limit state', () => {
  assert.equal(openAIQuotaIsAvailable({ rate_limit: { allowed: true, limit_reached: false } }), true)
  assert.equal(openAIQuotaIsAvailable({ rate_limit: { allowed: false, limit_reached: true } }), false)
  assert.equal(openAIQuotaIsAvailable({ rate_limit: { allowed: true, limit_reached: false }, additional_rate_limits: [
    { rate_limit: { allowed: false, limit_reached: true } },
  ] }), false)
  assert.equal(openAIQuotaIsAvailable({}), false)

  assert.equal(hasServerRateLimitState({ rate_limit_reset_at: '2026-08-26T00:00:00Z' }), true)
  assert.equal(hasServerRateLimitState({ temp_unschedulable_until: '2026-08-26T00:00:00Z' }), true)
  assert.equal(hasServerRateLimitState({ extra: { model_rate_limits: { 'gpt-5': {} } } }), false)
  assert.equal(hasServerRateLimitState({}), false)
})

test('resolves divergent credentials by access-token issue time', () => {
  const older = credentialsFromLocal(localAccount(unsignedJwt({ iat: 100, exp: 200 })))
  const newer = credentialsFromLocal(localAccount(unsignedJwt({ iat: 300, exp: 400 })))

  assert.equal(accessTokenIssuedAt(newer.access_token), 300)
  assert.equal(credentialDirection({ credentials: newer }, { credentials: older }), 'push')
  assert.equal(credentialDirection({ credentials: older }, { credentials: newer }), 'pull')
  assert.equal(credentialDirection({ credentials: older }, { credentials: older }), 'conflict')
})

test('does not treat a generic server record update as a token update', () => {
  const credentials = {
    ...credentialsFromLocal(localAccount(unsignedJwt({ iat: 300, exp: 400 }))),
    _token_version: 123,
  }

  assert.equal(serverCredentialsChanged({ server_token_version: 123 }, { credentials }), false)
  assert.equal(serverCredentialsChanged({ server_token_version: 122 }, { credentials }), true)
})

test('migrates legacy state by token age instead of generic change flags', () => {
  const older = credentialsFromLocal(localAccount(unsignedJwt({ iat: 100, exp: 200 })))
  const newer = credentialsFromLocal(localAccount(unsignedJwt({ iat: 300, exp: 400 })))

  assert.equal(syncCredentialDirection(
    true,
    false,
    { server_updated_at: 'legacy-state-without-token-baseline' },
    { credentials: older },
    { credentials: newer },
  ), 'pull')
})

test('bounds every remote request with SSH and process timeouts', () => {
  let capturedArgs
  let capturedOptions
  const response = remoteRequest('/health', 'GET', '', (_command, args, options) => {
    capturedArgs = args
    capturedOptions = options
    return { status: 0, stdout: '{}' }
  })

  assert.deepEqual(response, {})
  assert.ok(capturedArgs.includes('ServerAliveInterval=5'))
  assert.ok(capturedArgs.includes('ServerAliveCountMax=3'))
  assert.ok(capturedOptions.timeout > 0 && capturedOptions.timeout <= 60_000)
  assert.equal(capturedOptions.killSignal, 'SIGKILL')

  assert.throws(() => remoteRequest('/health', 'GET', '', () => ({
    status: null,
    stdout: '',
    error: Object.assign(new Error('timeout'), { code: 'ETIMEDOUT' }),
  })), /timed out/)
})
