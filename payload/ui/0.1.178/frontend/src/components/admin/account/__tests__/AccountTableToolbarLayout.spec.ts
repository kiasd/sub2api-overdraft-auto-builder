import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const testDirectory = dirname(fileURLToPath(import.meta.url))
const filtersSource = readFileSync(resolve(testDirectory, '../AccountTableFilters.vue'), 'utf8')
const accountsViewSource = readFileSync(
  resolve(testDirectory, '../../../../views/admin/AccountsView.vue'),
  'utf8'
)

describe('account table toolbar layout', () => {
  it('gives the search field more room while equal filters grow and wrap', () => {
    expect(filtersSource).toContain('flex w-full min-w-0 flex-wrap')
    expect(filtersSource).toContain('flex-[2_1_16rem]')
    expect(filtersSource.match(/flex-\[1_1_9rem\]/g)).toHaveLength(5)
    expect(filtersSource).not.toContain('class="w-40"')
  })

  it('keeps the upstream wrapping contract used by the responsive filters', () => {
    expect(accountsViewSource).toContain(
      'flex flex-wrap-reverse items-start justify-between gap-3'
    )
    expect(accountsViewSource.indexOf('<AccountTableFilters')).toBeLessThan(
      accountsViewSource.indexOf('<AccountTableActions')
    )
  })
})
