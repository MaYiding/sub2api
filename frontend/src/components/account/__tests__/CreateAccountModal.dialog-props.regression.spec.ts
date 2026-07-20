import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Regression: ISSUE-003 — the Gemini help BaseDialog received an unsupported
// max-width attribute, producing a Vue runtime warning on every Accounts load.
// Found by /qa on 2026-07-20.
// Report: .gstack/qa-reports/qa-report-localhost-2026-07-20.md
describe('CreateAccountModal dialog props', () => {
  it('uses the supported BaseDialog width prop for the Gemini help dialog', () => {
    const source = readFileSync(
      resolve(process.cwd(), 'src/components/account/CreateAccountModal.vue'),
      'utf8'
    )
    const helpDialog = source.match(
      /<BaseDialog[\s\S]*?:show="showGeminiHelpDialog"[\s\S]*?<\/BaseDialog>/
    )?.[0]

    expect(helpDialog).toBeDefined()
    expect(helpDialog).toContain('width="wide"')
    expect(helpDialog).not.toContain('max-width=')
  })
})
