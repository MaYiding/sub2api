const assert = require('node:assert/strict')
const test = require('node:test')

const {
  accessTokenIssuedAt,
  credentialDirection,
  credentialMetadataSnapshot,
  credentialsFromLocal,
  createServerAccount,
  hasServerRateLimitState,
  listServerAccounts,
  mergeServerCredentials,
  openAIQuotaIsAvailable,
  recoverServerRuntimeState,
  remoteRequest,
  sameCredentialFields,
  sameCredentialMetadata,
  serverAccountCreatePayload,
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

test('stores credential metadata without token material', () => {
  const credentials = credentialsFromLocal(localAccount())
  const snapshot = credentialMetadataSnapshot(credentials)

  assert.equal(snapshot.email, credentials.email)
  assert.equal(snapshot.expires_at, credentials.expires_at)
  assert.equal(snapshot.access_token, undefined)
  assert.equal(snapshot.refresh_token, undefined)
  assert.equal(snapshot.id_token, undefined)
})

test('compares server credential metadata symmetrically and ignores timestamp formatting', () => {
  const metadata = {
    email: 'account@example.com',
    expires_at: '2027-01-15T08:00:00.000Z',
  }

  assert.equal(sameCredentialMetadata(metadata, {
    email: 'account@example.com',
    expires_at: '2027-01-15T16:00:00+08:00',
  }), true)
  assert.equal(sameCredentialMetadata(metadata, { email: 'account@example.com' }), false)
  assert.equal(sameCredentialMetadata({ email: 'account@example.com' }, metadata), false)
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
  assert.equal(hasServerRateLimitState({ status: 'error' }), true)
  assert.equal(hasServerRateLimitState({ status: 'active', schedulable: false }), true)
  assert.equal(hasServerRateLimitState({ status: 'active', schedulable: true }), false)
  assert.equal(hasServerRateLimitState({ extra: { model_rate_limits: { 'gpt-5': {} } } }), false)
  assert.equal(hasServerRateLimitState({}), false)
})

test('paginates the server account list until the final partial page', () => {
  const requests = []
  const firstPage = Array.from({ length: 200 }, (_, index) => ({ id: index + 1 }))
  const accounts = listServerAccounts((path) => {
    requests.push(path)
    const page = new URL(`http://sync.test${path}`).searchParams.get('page')
    return { data: { items: page === '1' ? firstPage : [{ id: 201 }] } }
  })

  assert.equal(accounts.length, 201)
  assert.deepEqual(requests, [
    '/admin/accounts?page=1&page_size=200',
    '/admin/accounts?page=2&page_size=200',
  ])
})

test('builds a complete server OAuth account payload from local credentials', () => {
  const local = localAccount()
  const normalizedLocal = { ...local, credentials: credentialsFromLocal(local) }
  const payload = serverAccountCreatePayload(normalizedLocal, 456)

  assert.deepEqual(payload, {
    name: normalizedLocal.email,
    platform: 'openai',
    type: 'oauth',
    credentials: {
      ...normalizedLocal.credentials,
      _token_version: 456,
    },
    extra: {},
    concurrency: 10,
    priority: 2,
    rate_multiplier: 1,
  })
})

test('creates a missing server account with the OAuth payload', () => {
  const local = localAccount()
  const normalizedLocal = { ...local, credentials: credentialsFromLocal(local) }
  let captured
  const id = createServerAccount(normalizedLocal, (path, method, body) => {
    captured = { path, method, payload: JSON.parse(body) }
    return { data: { id: 73 } }
  })

  assert.equal(id, 73)
  assert.equal(captured.path, '/admin/accounts')
  assert.equal(captured.method, 'POST')
  assert.equal(captured.payload.name, normalizedLocal.email)
  assert.equal(captured.payload.credentials.access_token, normalizedLocal.credentials.access_token)
})

test('uses the server runtime recovery endpoint without touching credentials', () => {
  let captured
  const result = recoverServerRuntimeState(42, (path, method, body) => {
    captured = { path, method, body }
    return { data: { recovered: true } }
  })

  assert.deepEqual(captured, {
    path: '/admin/accounts/42/recover-state',
    method: 'POST',
    body: '{}',
  })
  assert.deepEqual(result, { recovered: true })
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
