#!/usr/bin/env node

const fs = require('fs')
const path = require('path')
const crypto = require('crypto')
const { spawnSync } = require('child_process')

const HOME = process.env.HOME || '/Users/toooa'
const COCKPIT_DIR = process.env.COCKPIT_DIR || path.join(HOME, '.antigravity_cockpit')
const ACCOUNTS_DIR = path.join(COCKPIT_DIR, 'codex_accounts')
const INDEX_PATH = path.join(COCKPIT_DIR, 'codex_accounts.json')
const STORAGE_KEY_PATH = path.join(COCKPIT_DIR, 'secure-account-storage.key')
const STATE_PATH = process.env.COCKPIT_TOKEN_SYNC_STATE || path.join(COCKPIT_DIR, 'sub2api-token-sync-state.json')
const BACKUP_DIR = process.env.COCKPIT_TOKEN_SYNC_BACKUPS || '/Volumes/MacData/09_tmp/cockpit-token-sync-backups'
const SSH_KEY = process.env.SUB2API_SYNC_SSH_KEY || path.join(HOME, '.ssh/id_ed25519_lab')
const SERVER_HOST = process.env.SUB2API_SYNC_HOST || '104.36.67.199'
const SERVER_USER = process.env.SUB2API_SYNC_USER || 'root'
const SERVER_DEPLOY_DIR = process.env.SUB2API_SYNC_DEPLOY_DIR || '/opt/sub2api/deploy'
const SERVER_STATE_PATH = path.posix.join(SERVER_DEPLOY_DIR, '.blue-green-active')
const ACCOUNT_PAGE_SIZE = 200
const MAX_ACCOUNT_PAGES = 100
const DEFAULT_ACCOUNT_CONCURRENCY = 10
const DEFAULT_ACCOUNT_PRIORITY = 2
const RATE_LIMIT_PROBE_INTERVAL_MS = 60 * 1000
const REMOTE_REQUEST_TIMEOUT_MS = positiveIntegerEnv('SUB2API_SYNC_REQUEST_TIMEOUT_MS', 40 * 1000)
const REMOTE_CURL_TIMEOUT_SECONDS = positiveIntegerEnv('SUB2API_SYNC_CURL_TIMEOUT_SECONDS', 15)
const SYNC_PASS_TIMEOUT_MS = positiveIntegerEnv('SUB2API_SYNC_PASS_TIMEOUT_MS', 90 * 1000)
const SYNC_PASS_STARTED_AT = Date.now()
const TOKEN_KEYS = ['access_token', 'refresh_token', 'id_token']
const OPTIONAL_CREDENTIAL_KEYS = [
  'client_id',
  'scope',
  'token_type',
]
const TIMESTAMP_CREDENTIAL_KEYS = ['expires_at', 'subscription_expires_at']

const dryRun = process.argv.includes('--dry-run')

function positiveIntegerEnv(name, fallback) {
  const parsed = Number(process.env[name])
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback
}

function log(message) {
  process.stdout.write(`[${new Date().toISOString()}] ${message}\n`)
}

function fail(message) {
  throw new Error(message)
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

function writeJson0600(filePath, value) {
  const tempPath = `${filePath}.tmp-${process.pid}`
  fs.writeFileSync(tempPath, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 })
  fs.chmodSync(tempPath, 0o600)
  fs.renameSync(tempPath, filePath)
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

function decodeJwtPayload(token) {
  if (typeof token !== 'string') return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    return JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
  } catch {
    return null
  }
}

function accessTokenExpiresAt(token) {
  const payload = decodeJwtPayload(token)
  const exp = Number(payload && payload.exp)
  if (!Number.isFinite(exp) || exp <= 0) return null
  return new Date(Math.floor(exp) * 1000).toISOString()
}

function accessTokenIssuedAt(token) {
  const payload = decodeJwtPayload(token)
  const iat = Number(payload && payload.iat)
  return Number.isFinite(iat) && iat > 0 ? Math.floor(iat) : null
}

function decryptAccount(filePath) {
  const envelope = readJson(filePath)
  const key = Buffer.from(fs.readFileSync(STORAGE_KEY_PATH, 'utf8').trim(), 'base64')
  const encrypted = Buffer.from(envelope.ciphertext, 'base64')
  const decipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(envelope.nonce, 'base64'))
  decipher.setAuthTag(encrypted.subarray(-16))
  const value = JSON.parse(Buffer.concat([
    decipher.update(encrypted.subarray(0, -16)),
    decipher.final(),
  ]).toString('utf8'))
  return { envelope, value }
}

function encryptAccount(value, previousEnvelope) {
  const key = Buffer.from(fs.readFileSync(STORAGE_KEY_PATH, 'utf8').trim(), 'base64')
  const nonce = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', key, nonce)
  const encrypted = Buffer.concat([
    cipher.update(Buffer.from(JSON.stringify(value), 'utf8')),
    cipher.final(),
    cipher.getAuthTag(),
  ])
  return {
    version: previousEnvelope.version,
    kind: previousEnvelope.kind,
    algorithm: previousEnvelope.algorithm,
    key_id: previousEnvelope.key_id,
    nonce: nonce.toString('base64'),
    ciphertext: encrypted.toString('base64'),
    encrypted_at: Date.now(),
  }
}

function tokenSet(account) {
  const tokens = account.tokens || {}
  if (TOKEN_KEYS.some(key => typeof tokens[key] !== 'string' || tokens[key].length === 0)) return null
  return Object.fromEntries(TOKEN_KEYS.map(key => [key, tokens[key]]))
}

function credentialsFromLocal(account) {
  const tokens = tokenSet(account)
  if (!tokens) return null
  const credentials = {
    ...tokens,
    email: account.email,
    chatgpt_account_id: account.account_id,
    chatgpt_user_id: account.user_id,
    organization_id: account.organization_id,
    plan_type: account.plan_type,
    subscription_expires_at: account.subscription_active_until,
  }
  const expiresAt = accessTokenExpiresAt(tokens.access_token)
  if (expiresAt) credentials.expires_at = expiresAt
  for (const key of OPTIONAL_CREDENTIAL_KEYS) {
    if (typeof account[key] === 'string' && account[key].length > 0) {
      credentials[key] = account[key]
    }
  }
  return Object.fromEntries(Object.entries(credentials).filter(([, value]) => value !== undefined && value !== null && value !== ''))
}

function credentialsFromServer(account) {
  const credentials = account && account.credentials
  if (!credentials) return null
  if (TOKEN_KEYS.some(key => typeof credentials[key] !== 'string' || credentials[key].length === 0)) return null
  const selected = Object.fromEntries(TOKEN_KEYS.map(key => [key, credentials[key]]))
  for (const key of ['expires_at', ...OPTIONAL_CREDENTIAL_KEYS, 'email', 'chatgpt_account_id', 'chatgpt_user_id', 'organization_id', 'plan_type', 'subscription_expires_at']) {
    if (credentials[key] !== undefined && credentials[key] !== null && credentials[key] !== '') {
      selected[key] = credentials[key]
    }
  }
  return selected
}

function sameTokens(left, right) {
  return left && right && TOKEN_KEYS.every(key => left[key] === right[key])
}

function comparableCredentialValue(key, value) {
  if (TIMESTAMP_CREDENTIAL_KEYS.includes(key)) {
    const parsed = Date.parse(String(value || ''))
    if (Number.isFinite(parsed)) return Math.floor(parsed / 1000)
  }
  return value
}

function sameCredentialFields(expected, actual, includeTokens = true) {
  if (!expected || !actual) return false
  return Object.entries(expected)
    .filter(([key]) => includeTokens || !TOKEN_KEYS.includes(key))
    .every(([key, value]) => comparableCredentialValue(key, value) === comparableCredentialValue(key, actual[key]))
}

function credentialMetadataSnapshot(credentials) {
  if (!credentials) return {}
  return Object.fromEntries(Object.entries(credentials).filter(([key]) => !TOKEN_KEYS.includes(key)))
}

function sameCredentialMetadata(left, right) {
  const leftSnapshot = credentialMetadataSnapshot(left)
  const rightSnapshot = credentialMetadataSnapshot(right)
  return sameCredentialFields(leftSnapshot, rightSnapshot, false) &&
    sameCredentialFields(rightSnapshot, leftSnapshot, false)
}

function mergeServerCredentials(serverAccount, localCredentials, tokenVersion = Date.now()) {
  return {
    ...((serverAccount && serverAccount.credentials) || {}),
    ...localCredentials,
    _token_version: tokenVersion,
  }
}

function hasStaleReauthState(local) {
  return local.value.requires_reauth === true || Boolean(local.value.reauth_reason)
}

function serverCredentialVersion(serverAccount) {
  const value = Number(serverAccount && serverAccount.credentials && serverAccount.credentials._token_version)
  return Number.isFinite(value) && value > 0 ? value : null
}

function credentialIssuedAt(credentials) {
  return accessTokenIssuedAt(credentials && credentials.access_token)
}

function credentialDirection(local, serverAccount) {
  const localIssuedAt = credentialIssuedAt(local && local.credentials)
  const serverCredentials = credentialsFromServer(serverAccount)
  const serverIssuedAt = credentialIssuedAt(serverCredentials)
  if (localIssuedAt !== null && serverIssuedAt !== null && localIssuedAt !== serverIssuedAt) {
    return localIssuedAt > serverIssuedAt ? 'push' : 'pull'
  }
  return 'conflict'
}

function serverCredentialsChanged(previous, serverAccount) {
  const currentVersion = serverCredentialVersion(serverAccount)
  const previousVersion = Number(previous && previous.server_token_version)
  if (currentVersion !== null && Number.isFinite(previousVersion) && previousVersion > 0) {
    return currentVersion !== previousVersion
  }

  const currentIssuedAt = credentialIssuedAt(credentialsFromServer(serverAccount))
  const previousIssuedAt = Number(previous && previous.server_token_issued_at)
  if (currentIssuedAt !== null && Number.isFinite(previousIssuedAt) && previousIssuedAt > 0) {
    return currentIssuedAt !== previousIssuedAt
  }
  return false
}

function syncCredentialDirection(localChanged, serverChanged, previous, local, serverAccount) {
  const hasVersionBaseline = Number(previous && previous.server_token_version) > 0
  const hasIssuedAtBaseline = Number(previous && previous.server_token_issued_at) > 0
  if (!hasVersionBaseline && !hasIssuedAtBaseline) {
    return credentialDirection(local, serverAccount)
  }
  if (localChanged && !serverChanged) return 'push'
  if (serverChanged && !localChanged) return 'pull'
  return credentialDirection(local, serverAccount)
}

function serverTokenTimestamp(serverAccount) {
  const issuedAt = credentialIssuedAt(credentialsFromServer(serverAccount))
  if (issuedAt !== null) return issuedAt
  const version = serverCredentialVersion(serverAccount)
  return version !== null ? Math.floor(version / 1000) : Math.floor(Date.now() / 1000)
}

function remoteRequest(apiPath, method = 'GET', input = '', run = spawnSync) {
  const passRemainingMs = SYNC_PASS_TIMEOUT_MS - (Date.now() - SYNC_PASS_STARTED_AT)
  if (passRemainingMs <= 0) fail(`sync pass timed out after ${SYNC_PASS_TIMEOUT_MS}ms`)
  const requestTimeoutMs = Math.min(REMOTE_REQUEST_TIMEOUT_MS, passRemainingMs)
  const curlTimeoutArgs = `--connect-timeout 5 --max-time ${REMOTE_CURL_TIMEOUT_SECONDS}`
  const remoteScript = [
    'set -eu',
    `state_file=${shellQuote(SERVER_STATE_PATH)}`,
    'container=sub2api',
    'port=8080',
    'if [ -r "$state_file" ]; then',
    '  container="$(sed -n "s/^active_container=//p" "$state_file")"',
    '  port="$(sed -n "s/^active_port=//p" "$state_file")"',
    '  test -n "$container" && test -n "$port"',
    'fi',
    'case "$container" in',
    '  sub2api|sub2api-blue|sub2api-green) ;;',
    '  *) exit 1 ;;',
    'esac',
    'case "$port" in',
    '  *[!0-9]*|"") exit 1 ;;',
    'esac',
    'runtime_status="$(docker inspect "$container" --format "{{.State.Status}}")"',
    'runtime_health="$(docker inspect "$container" --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}")"',
    'test "$runtime_status" = running',
    'test "$runtime_health" = healthy || test "$runtime_health" = none',
    'envs="$(docker inspect "$container" --format "{{range .Config.Env}}{{println .}}{{end}}")"',
    'email="$(printf "%s\\n" "$envs" | sed -n "s/^ADMIN_EMAIL=//p")"',
    'password="$(printf "%s\\n" "$envs" | sed -n "s/^ADMIN_PASSWORD=//p")"',
    'body="$(printf "{\\"email\\":\\"%s\\",\\"password\\":\\"%s\\"}" "$email" "$password")"',
    'base_url="http://127.0.0.1:${port}/api/v1"',
    `login="$(curl -fsS ${curlTimeoutArgs} -H "Content-Type: application/json" --data "$body" "$base_url/auth/login)"`,
    'jwt="$(printf "%s" "$login" | sed -n "s/.*\\"access_token\\":\\"\\([^\\"]*\\)\\".*/\\1/p")"',
    'test -n "$jwt"',
    `payload="$(cat)"`,
    `endpoint="$(printf "%s%s" "$base_url" ${shellQuote(apiPath)})"`,
    `if [ ${shellQuote(method)} = POST ]; then`,
    '  response="$(curl -fsS ${curlTimeoutArgs} -H "Authorization: Bearer $jwt" -H "Content-Type: application/json" --data-binary "$payload" "$endpoint")"',
    'else',
    '  response="$(curl -fsS ${curlTimeoutArgs} -H "Authorization: Bearer $jwt" "$endpoint")"',
    'fi',
    'printf "%s" "$response"',
  ].join('\n')

  const result = run('/usr/bin/ssh', [
    '-i', SSH_KEY,
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=10',
    '-o', 'ConnectionAttempts=1',
    '-o', 'ServerAliveInterval=5',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'NumberOfPasswordPrompts=0',
    '-o', 'StrictHostKeyChecking=yes',
    `${SERVER_USER}@${SERVER_HOST}`,
    remoteScript,
  ], {
    input,
    encoding: 'utf8',
    maxBuffer: 30 * 1024 * 1024,
    timeout: requestTimeoutMs,
    killSignal: 'SIGKILL',
  })

  if (result.error && result.error.code === 'ETIMEDOUT') {
    fail(`remote ${method} ${apiPath} timed out after ${requestTimeoutMs}ms`)
  }
  if (result.error || result.status !== 0) fail(`remote ${method} ${apiPath} failed`)
  try {
    return JSON.parse(result.stdout)
  } catch {
    fail(`remote ${method} ${apiPath} returned invalid JSON`)
  }
}

function unwrap(response) {
  return response && response.data !== undefined ? response.data : response
}

function listServerAccounts(request = remoteRequest) {
  const accounts = []
  for (let page = 1; page <= MAX_ACCOUNT_PAGES; page++) {
    const response = request(`/admin/accounts?page=${page}&page_size=${ACCOUNT_PAGE_SIZE}`)
    const payload = unwrap(response)
    const items = Array.isArray(payload && payload.items) ? payload.items : []
    accounts.push(...items)
    if (items.length < ACCOUNT_PAGE_SIZE) return accounts
  }
  fail(`server account pagination exceeded ${MAX_ACCOUNT_PAGES} pages`)
}

function exportServerAccounts(ids = []) {
  const query = ids.length > 0
    ? `ids=${ids.join(',')}&include_proxies=false`
    : 'include_proxies=false'
  const response = remoteRequest(`/admin/accounts/data?${query}`)
  const payload = unwrap(response)
  return Array.isArray(payload.accounts) ? payload.accounts : []
}

function applyServerCredentials(id, credentials, serverAccount = null) {
  const mergedCredentials = mergeServerCredentials(serverAccount, credentials)
  const response = remoteRequest(
    `/admin/accounts/${encodeURIComponent(id)}/apply-oauth-credentials`,
    'POST',
    JSON.stringify({ type: 'oauth', credentials: mergedCredentials }),
  )
  if (response && response.code !== undefined && response.code !== 0) {
    fail(`server rejected OAuth credentials for account ${id}`)
  }
  return { ...(serverAccount || {}), credentials: mergedCredentials }
}

function serverAccountCreatePayload(local, tokenVersion = Date.now()) {
  return {
    name: local.email,
    platform: 'openai',
    type: 'oauth',
    credentials: mergeServerCredentials(null, local.credentials, tokenVersion),
    extra: {},
    concurrency: DEFAULT_ACCOUNT_CONCURRENCY,
    priority: DEFAULT_ACCOUNT_PRIORITY,
    rate_multiplier: 1,
  }
}

function createServerAccount(local, request = remoteRequest) {
  const response = request(
    '/admin/accounts',
    'POST',
    JSON.stringify(serverAccountCreatePayload(local)),
  )
  if (response && response.code !== undefined && response.code !== 0) {
    fail(`server rejected account creation for ${local.email}`)
  }
  const payload = unwrap(response)
  const id = Number(payload && payload.id)
  if (!Number.isInteger(id) || id <= 0) {
    fail(`server account creation returned no account id for ${local.email}`)
  }
  return id
}

function recoverServerRuntimeState(id, request = remoteRequest) {
  const response = request(
    `/admin/accounts/${encodeURIComponent(id)}/recover-state`,
    'POST',
    '{}',
  )
  if (response && response.code !== undefined && response.code !== 0) {
    fail(`server rejected runtime recovery for account ${id}`)
  }
  return unwrap(response)
}

function hasServerRateLimitState(account) {
  return Boolean(
    account && (
      account.status === 'error' ||
      account.schedulable === false ||
      account.rate_limited_at ||
      account.rate_limit_reset_at ||
      account.overload_until ||
      account.temp_unschedulable_until
    ),
  )
}

function openAIQuotaIsAvailable(response) {
  const usage = unwrap(response)
  const rateLimit = usage && usage.rate_limit
  if (!rateLimit || rateLimit.allowed !== true || rateLimit.limit_reached === true) return false
  for (const additional of Array.isArray(usage.additional_rate_limits) ? usage.additional_rate_limits : []) {
    const additionalRateLimit = additional && additional.rate_limit
    if (additionalRateLimit && (additionalRateLimit.allowed !== true || additionalRateLimit.limit_reached === true)) {
      return false
    }
  }
  return true
}

function queryServerOpenAIQuota(id) {
  return remoteRequest(`/admin/openai/accounts/${encodeURIComponent(id)}/quota`)
}

function loadLocalAccounts() {
  const index = readJson(INDEX_PATH)
  const result = new Map()
  for (const item of Array.isArray(index.accounts) ? index.accounts : []) {
    if (!item || typeof item.email !== 'string' || !item.email.includes('@')) continue
    const filePath = path.join(ACCOUNTS_DIR, `${item.id}.json`)
    if (!fs.existsSync(filePath)) continue
    try {
      const { envelope, value } = decryptAccount(filePath)
      if (value.auth_mode && value.auth_mode !== 'oauth') continue
      const credentials = credentialsFromLocal(value)
      if (!credentials) continue
      result.set(value.email, {
        id: item.id,
        email: value.email,
        filePath,
        envelope,
        value,
        credentials,
        fileMtimeMs: fs.statSync(filePath).mtimeMs,
      })
    } catch (error) {
      log(`skip unreadable local account ${item.email}`)
    }
  }
  return result
}

function readState() {
  if (!fs.existsSync(STATE_PATH)) return { version: 1, accounts: {}, rate_limit_probes: {} }
  try {
    const state = readJson(STATE_PATH)
    if (!state || state.version !== 1 || !state.accounts) return { version: 1, accounts: {}, rate_limit_probes: {} }
    if (!state.rate_limit_probes || typeof state.rate_limit_probes !== 'object') state.rate_limit_probes = {}
    return state
  } catch {
    log('state file is invalid; starting a guarded re-adoption')
    return { version: 1, accounts: {}, rate_limit_probes: {} }
  }
}

function saveState(state) {
  fs.mkdirSync(path.dirname(STATE_PATH), { recursive: true })
  writeJson0600(STATE_PATH, state)
}

function writeLocalFromServer(local, serverAccount, serverMeta) {
  const serverTokens = credentialsFromServer(serverAccount)
  if (!serverTokens) fail(`server account ${serverMeta.id} has incomplete OAuth credentials`)

  const serverCredentials = serverAccount.credentials
  const nextValue = {
    ...local.value,
    email: serverCredentials.email || serverMeta.name || local.value.email,
    auth_mode: 'oauth',
    account_id: serverCredentials.chatgpt_account_id || local.value.account_id,
    organization_id: serverCredentials.organization_id || local.value.organization_id,
    user_id: serverCredentials.chatgpt_user_id || local.value.user_id,
    plan_type: serverCredentials.plan_type || local.value.plan_type,
    subscription_active_until: serverCredentials.subscription_expires_at || local.value.subscription_active_until,
    token_generation: (Number(local.value.token_generation) || 0) + 1,
    token_updated_at: serverTokenTimestamp(serverAccount),
    token_source_mode: 'managed',
    requires_reauth: false,
    reauth_reason: '',
    tokens: {
      ...(local.value.tokens || {}),
      ...serverTokens,
    },
  }
  const nextEnvelope = encryptAccount(nextValue, local.envelope)

  fs.mkdirSync(BACKUP_DIR, { recursive: true })
  const backupPath = path.join(BACKUP_DIR, `${local.id}.previous.json`)
  const backupTemp = `${backupPath}.tmp-${process.pid}`
  fs.copyFileSync(local.filePath, backupTemp)
  fs.chmodSync(backupTemp, 0o600)
  fs.renameSync(backupTemp, backupPath)

  const writeTemp = `${local.filePath}.sync-${process.pid}.tmp`
  fs.writeFileSync(writeTemp, `${JSON.stringify(nextEnvelope, null, 2)}\n`, { mode: 0o600 })
  fs.chmodSync(writeTemp, 0o600)
  fs.renameSync(writeTemp, local.filePath)
}

function stateFor(local, serverMeta, serverAccount) {
  return {
    server_id: serverMeta.id,
    server_updated_at: serverMeta.updated_at || '',
    server_token_version: serverCredentialVersion(serverAccount),
    server_token_issued_at: credentialIssuedAt(credentialsFromServer(serverAccount)),
    server_credential_metadata: credentialMetadataSnapshot(serverMeta.credentials),
    local_token_updated_at: Number(local.value.token_updated_at) || 0,
    local_credential_metadata: credentialMetadataSnapshot(local.credentials),
    initialized: true,
    last_sync_at: new Date().toISOString(),
  }
}

function reconcileServerRateLimits(serverMetas, localByEmail, state) {
  let changed = false
  for (const serverMeta of serverMetas) {
    const email = serverMeta.name
    if (!localByEmail.has(email) || !hasServerRateLimitState(serverMeta)) continue

    const lastProbeAt = Date.parse(state.rate_limit_probes[email] || '')
    if (Number.isFinite(lastProbeAt) && Date.now() - lastProbeAt < RATE_LIMIT_PROBE_INTERVAL_MS) continue
    if (!dryRun) {
      state.rate_limit_probes[email] = new Date().toISOString()
      changed = true
    }

    let quota
    try {
      quota = queryServerOpenAIQuota(serverMeta.id)
    } catch (error) {
      log(`rate-limit probe skipped ${email}: ${error instanceof Error ? error.message : 'probe failed'}`)
      continue
    }
    if (!openAIQuotaIsAvailable(quota)) continue
    if (dryRun) {
      log(`would recover server runtime state ${email}: upstream quota is available`)
      continue
    }
    try {
      recoverServerRuntimeState(serverMeta.id)
      log(`recovered server runtime state ${email}: upstream quota is available`)
    } catch (error) {
      log(`server runtime recovery skipped ${email}: ${error instanceof Error ? error.message : 'recovery failed'}`)
    }
  }
  return changed
}

function main() {
  const serverMetas = listServerAccounts()
    .filter(account => account.platform === 'openai' && account.type === 'oauth' && account.name)
  const serverByEmail = new Map(serverMetas.map(account => [account.name, account]))
  const localByEmail = loadLocalAccounts()
  const state = readState()
  let exportsCache = null
  let changed = false

  const getExport = (ids = []) => {
    if (ids.length === 0) {
      if (!exportsCache) exportsCache = exportServerAccounts()
      return exportsCache
    }
    return exportServerAccounts(ids)
  }

  for (const [email, local] of localByEmail) {
    const serverMeta = serverByEmail.get(email)
    if (!serverMeta) {
      if (dryRun) {
        log(`would create ${email}: local OAuth account is missing on server`)
      } else {
        const serverId = createServerAccount(local)
        const refreshedMeta = listServerAccounts().find(account => account.id === serverId)
        if (!refreshedMeta) fail(`created server account ${serverId} for ${email} was not found after creation`)
        const serverAccount = getExport([serverId]).find(account => account.name === email)
        if (!serverAccount) fail(`created server account ${serverId} for ${email} has no credential export`)
        state.accounts[email] = stateFor(local, refreshedMeta, serverAccount)
        changed = true
        log(`created ${email}: local OAuth account -> server`)
      }
      continue
    }
    const previous = state.accounts[email]

    if (!previous || previous.server_id !== serverMeta.id || previous.initialized !== true) {
      const serverAccount = getExport([serverMeta.id]).find(account => account.name === email)
      const serverTokens = credentialsFromServer(serverAccount)
      if (!serverTokens) {
        log(`blocked ${email}: server OAuth credentials are incomplete`)
        continue
      }
      if (sameTokens(local.credentials, serverTokens)) {
        if (!dryRun) {
          let adoptedServerAccount = serverAccount
          if (!sameCredentialFields(local.credentials, serverTokens)) {
            adoptedServerAccount = applyServerCredentials(serverMeta.id, local.credentials, serverAccount)
            log(`pushed ${email}: completed server OAuth metadata`)
          }
          state.accounts[email] = stateFor(local, serverMeta, adoptedServerAccount)
          changed = true
        }
        log(`${dryRun ? 'would initialize' : 'initialized'} ${email}`)
      } else {
        const direction = credentialDirection(local, serverAccount)
        if (direction === 'push') {
          if (dryRun) {
            log(`would adopt ${email}: newer Cockpit token -> server`)
          } else {
            const appliedServerAccount = applyServerCredentials(serverMeta.id, local.credentials, serverAccount)
            const refreshedMeta = listServerAccounts().find(account => account.id === serverMeta.id) || serverMeta
            state.accounts[email] = stateFor(local, refreshedMeta, appliedServerAccount)
            changed = true
            log(`adopted ${email}: newer Cockpit token -> server`)
          }
        } else if (direction === 'pull') {
          if (dryRun) {
            log(`would adopt ${email}: newer server token -> Cockpit`)
          } else {
            writeLocalFromServer(local, serverAccount, serverMeta)
            const updatedLocal = loadLocalAccounts().get(email)
            state.accounts[email] = stateFor(updatedLocal, serverMeta, serverAccount)
            changed = true
            log(`adopted ${email}: newer server token -> Cockpit`)
          }
        } else {
          log(`CONFLICT ${email}: local and server tokens differ with equal or unknown age; no overwrite`)
        }
      }
      continue
    }

    const localChanged = Number(local.value.token_updated_at) !== Number(previous.local_token_updated_at)
    const serverCredentialMetadataChanged = !sameCredentialMetadata(
      serverMeta.credentials,
      previous.server_credential_metadata,
    )
    const localMetadataChanged = !sameCredentialFields(
      local.credentials,
      previous.local_credential_metadata || {},
      false,
    )
    if (!localChanged && !serverCredentialMetadataChanged && !localMetadataChanged) continue

    const serverAccount = getExport([serverMeta.id]).find(account => account.name === email)
    const serverCredentials = credentialsFromServer(serverAccount)
    if (!serverCredentials) {
      log(`blocked ${email}: server OAuth credentials are incomplete`)
      continue
    }
    const serverChanged = serverCredentialsChanged(previous, serverAccount)

    if (sameTokens(local.credentials, serverCredentials) && !sameCredentialFields(local.credentials, serverCredentials)) {
      if (dryRun) {
        log(`would push ${email}: Cockpit credentials -> server`)
      } else {
        const appliedServerAccount = applyServerCredentials(serverMeta.id, local.credentials, serverAccount)
        const refreshedMeta = listServerAccounts().find(account => account.id === serverMeta.id) || serverMeta
        state.accounts[email] = stateFor(local, refreshedMeta, appliedServerAccount)
        changed = true
        log(`pushed ${email}: Cockpit credentials -> server`)
      }
      continue
    }

    if (sameTokens(local.credentials, serverCredentials)) {
      if (!dryRun) {
        if (hasStaleReauthState(local)) {
          writeLocalFromServer(local, serverAccount, serverMeta)
          const updatedLocal = loadLocalAccounts().get(email)
          state.accounts[email] = stateFor(updatedLocal, serverMeta, serverAccount)
          log(`cleared stale reauth state ${email}`)
        } else {
          state.accounts[email] = stateFor(local, serverMeta, serverAccount)
        }
        changed = true
      } else if (hasStaleReauthState(local)) {
        log(`would clear stale reauth state ${email}`)
      }
      if (dryRun) log(`would reconcile ${email}`)
      continue
    }

    const direction = syncCredentialDirection(localChanged, serverChanged, previous, local, serverAccount)

    if (direction === 'conflict') {
      log(`CONFLICT ${email}: tokens differ with equal or unknown age; no overwrite`)
      continue
    }

    if (direction === 'pull') {
      if (dryRun) {
        log(`would pull ${email}: server -> Cockpit`)
      } else {
        writeLocalFromServer(local, serverAccount, serverMeta)
        const updatedLocal = loadLocalAccounts().get(email)
        state.accounts[email] = stateFor(updatedLocal, serverMeta, serverAccount)
        changed = true
        log(`pulled ${email}: server -> Cockpit`)
      }
      continue
    }

    if (direction === 'push') {
      if (dryRun) {
        log(`would push ${email}: Cockpit -> server`)
      } else {
        const appliedServerAccount = applyServerCredentials(serverMeta.id, local.credentials, serverAccount)
        const refreshedMeta = listServerAccounts().find(account => account.id === serverMeta.id) || serverMeta
        state.accounts[email] = stateFor(local, refreshedMeta, appliedServerAccount)
        changed = true
        log(`pushed ${email}: Cockpit -> server`)
      }
    }
  }

  if (reconcileServerRateLimits(serverMetas, localByEmail, state)) changed = true

  if (!dryRun && changed) saveState(state)
  log(`completed: local=${localByEmail.size}, server_oauth=${serverMetas.length}, dry_run=${dryRun}`)
}

if (require.main === module) {
  try {
    main()
  } catch (error) {
    log(`ERROR: ${error instanceof Error ? error.message : 'sync failed'}`)
    process.exitCode = 1
  }
}

module.exports = {
  accessTokenIssuedAt,
  accessTokenExpiresAt,
  credentialDirection,
  credentialMetadataSnapshot,
  credentialsFromLocal,
  credentialsFromServer,
  mergeServerCredentials,
  hasServerRateLimitState,
  listServerAccounts,
  openAIQuotaIsAvailable,
  remoteRequest,
  sameCredentialFields,
  sameCredentialMetadata,
  sameTokens,
  serverAccountCreatePayload,
  serverCredentialVersion,
  serverCredentialsChanged,
  recoverServerRuntimeState,
  createServerAccount,
  syncCredentialDirection,
}
