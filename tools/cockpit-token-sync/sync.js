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
const SERVER_API = 'http://127.0.0.1:8080/api/v1'
const ACCOUNT_PAGE_SIZE = 200

const dryRun = process.argv.includes('--dry-run')

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
  const required = ['access_token', 'refresh_token', 'id_token']
  if (required.some(key => typeof tokens[key] !== 'string' || tokens[key].length === 0)) return null
  return Object.fromEntries(required.map(key => [key, tokens[key]]))
}

function credentialsFromLocal(account) {
  const tokens = tokenSet(account)
  if (!tokens) return null
  return {
    ...tokens,
    email: account.email,
    chatgpt_account_id: account.account_id,
    chatgpt_user_id: account.user_id,
    organization_id: account.organization_id,
    plan_type: account.plan_type,
    subscription_expires_at: account.subscription_active_until,
  }
}

function credentialsFromServer(account) {
  const credentials = account && account.credentials
  if (!credentials) return null
  const required = ['access_token', 'refresh_token', 'id_token']
  if (required.some(key => typeof credentials[key] !== 'string' || credentials[key].length === 0)) return null
  return Object.fromEntries(required.map(key => [key, credentials[key]]))
}

function sameTokens(left, right) {
  return left && right && ['access_token', 'refresh_token', 'id_token'].every(key => left[key] === right[key])
}

function serverTokenTimestamp(updatedAt) {
  const time = Date.parse(updatedAt || '')
  return Number.isFinite(time) ? Math.floor(time / 1000) : Math.floor(Date.now() / 1000)
}

function remoteRequest(apiPath, method = 'GET', input = '') {
  const url = `${SERVER_API}${apiPath}`
  const remoteScript = [
    'set -eu',
    'envs="$(docker inspect sub2api --format "{{range .Config.Env}}{{println .}}{{end}}")"',
    'email="$(printf "%s\\n" "$envs" | sed -n "s/^ADMIN_EMAIL=//p")"',
    'password="$(printf "%s\\n" "$envs" | sed -n "s/^ADMIN_PASSWORD=//p")"',
    'body="$(printf "{\\"email\\":\\"%s\\",\\"password\\":\\"%s\\"}" "$email" "$password")"',
    'login="$(curl -fsS -H "Content-Type: application/json" --data "$body" http://127.0.0.1:8080/api/v1/auth/login)"',
    'jwt="$(printf "%s" "$login" | sed -n "s/.*\\"access_token\\":\\"\\([^\\"]*\\)\\".*/\\1/p")"',
    'test -n "$jwt"',
    `payload="$(cat)"`,
    `if [ ${shellQuote(method)} = POST ]; then`,
    `  response="$(curl -sS -H "Authorization: Bearer $jwt" -H "Content-Type: application/json" --data-binary "$payload" ${shellQuote(url)})"`,
    'else',
    `  response="$(curl -sS -H "Authorization: Bearer $jwt" ${shellQuote(url)})"`,
    'fi',
    'printf "%s" "$response"',
  ].join('\n')

  const result = spawnSync('/usr/bin/ssh', [
    '-i', SSH_KEY,
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=10',
    '-o', 'ConnectionAttempts=1',
    '-o', 'StrictHostKeyChecking=yes',
    `${SERVER_USER}@${SERVER_HOST}`,
    remoteScript,
  ], {
    input,
    encoding: 'utf8',
    maxBuffer: 30 * 1024 * 1024,
  })

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

function listServerAccounts() {
  const response = remoteRequest(`/admin/accounts?page=1&page_size=${ACCOUNT_PAGE_SIZE}`)
  const payload = unwrap(response)
  return Array.isArray(payload.items) ? payload.items : []
}

function exportServerAccounts(ids = []) {
  const query = ids.length > 0
    ? `ids=${ids.join(',')}&include_proxies=false`
    : 'include_proxies=false'
  const response = remoteRequest(`/admin/accounts/data?${query}`)
  const payload = unwrap(response)
  return Array.isArray(payload.accounts) ? payload.accounts : []
}

function applyServerCredentials(id, credentials) {
  const response = remoteRequest(
    `/admin/accounts/${encodeURIComponent(id)}/apply-oauth-credentials`,
    'POST',
    JSON.stringify({ type: 'oauth', credentials }),
  )
  if (response && response.code !== undefined && response.code !== 0) {
    fail(`server rejected OAuth credentials for account ${id}`)
  }
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
  if (!fs.existsSync(STATE_PATH)) return { version: 1, accounts: {} }
  try {
    const state = readJson(STATE_PATH)
    return state && state.version === 1 && state.accounts ? state : { version: 1, accounts: {} }
  } catch {
    log('state file is invalid; starting a guarded re-adoption')
    return { version: 1, accounts: {} }
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
    token_updated_at: serverTokenTimestamp(serverMeta.updated_at),
    token_source_mode: 'managed',
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

function stateFor(local, serverMeta) {
  return {
    server_id: serverMeta.id,
    server_updated_at: serverMeta.updated_at || '',
    local_token_updated_at: Number(local.value.token_updated_at) || 0,
    local_file_mtime_ms: fs.statSync(local.filePath).mtimeMs,
    initialized: true,
    last_sync_at: new Date().toISOString(),
  }
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
    if (!serverMeta) continue
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
          state.accounts[email] = stateFor(local, serverMeta)
          changed = true
        }
        log(`${dryRun ? 'would initialize' : 'initialized'} ${email}`)
      } else {
        log(`CONFLICT ${email}: local and server tokens differ; no overwrite on first adoption`)
      }
      continue
    }

    const localChanged = (
      Number(local.value.token_updated_at) !== Number(previous.local_token_updated_at) ||
      local.fileMtimeMs > Number(previous.local_file_mtime_ms || 0) + 1
    )
    const serverChanged = (serverMeta.updated_at || '') !== (previous.server_updated_at || '')
    if (!localChanged && !serverChanged) continue

    const serverAccount = getExport([serverMeta.id]).find(account => account.name === email)
    const serverTokens = credentialsFromServer(serverAccount)
    if (!serverTokens) {
      log(`blocked ${email}: server OAuth credentials are incomplete`)
      continue
    }

    if (sameTokens(local.credentials, serverTokens)) {
      if (!dryRun) {
        state.accounts[email] = stateFor(local, serverMeta)
        changed = true
      }
      if (dryRun) log(`would reconcile ${email}`)
      continue
    }

    if (localChanged && serverChanged) {
      log(`CONFLICT ${email}: both sides changed since last sync; no overwrite`)
      continue
    }

    if (serverChanged) {
      if (dryRun) {
        log(`would pull ${email}: server -> Cockpit`)
      } else {
        writeLocalFromServer(local, serverAccount, serverMeta)
        const updatedLocal = loadLocalAccounts().get(email)
        state.accounts[email] = stateFor(updatedLocal, serverMeta)
        changed = true
        log(`pulled ${email}: server -> Cockpit`)
      }
      continue
    }

    if (localChanged) {
      if (dryRun) {
        log(`would push ${email}: Cockpit -> server`)
      } else {
        applyServerCredentials(serverMeta.id, local.credentials)
        const refreshedMeta = listServerAccounts().find(account => account.id === serverMeta.id) || serverMeta
        state.accounts[email] = stateFor(local, refreshedMeta)
        changed = true
        log(`pushed ${email}: Cockpit -> server`)
      }
    }
  }

  if (!dryRun && changed) saveState(state)
  log(`completed: local=${localByEmail.size}, server_oauth=${serverMetas.length}, dry_run=${dryRun}`)
}

try {
  main()
} catch (error) {
  log(`ERROR: ${error instanceof Error ? error.message : 'sync failed'}`)
  process.exitCode = 1
}
