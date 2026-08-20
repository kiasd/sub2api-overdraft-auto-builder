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
  it('keeps the official single-row desktop toolbar while narrow screens can wrap', () => {
    expect(filtersSource).toContain(
      'flex w-full min-w-0 flex-wrap items-center gap-3 xl:w-auto xl:flex-1 xl:flex-nowrap'
    )
    expect(filtersSource).toContain('flex-[2_1_16rem] xl:max-w-64')
    expect(filtersSource.match(/flex-\[1_1_9rem\]/g)).toHaveLength(5)
    expect(filtersSource.match(/xl:max-w-40/g)).toHaveLength(5)
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
