import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import type { UserDashboardStats as UserStatsType } from '@/api/usage'
import UserDashboardStats from '../UserDashboardStats.vue'

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string) => key
    })
  }
})

const stats = {
  total_api_keys: 123456789,
  active_api_keys: 123456788,
  today_requests: 987654321,
  total_requests: 9876543210,
  today_actual_cost: 98765.4321,
  today_cost: 123456.789,
  total_actual_cost: 987654.321,
  total_cost: 1234567.89,
  today_tokens: 999_999_999_999_999,
  today_input_tokens: 888_888_888_888_888,
  today_output_tokens: 777_777_777_777_777,
  total_tokens: 8_999_999_999_999_999,
  total_input_tokens: 8_888_888_888_888_888,
  total_output_tokens: 7_777_777_777_777_777,
  rpm: 999_999_999_999,
  tpm: 888_888_888_888,
  average_duration_ms: 123456,
  by_platform: []
} as unknown as UserStatsType

describe('UserDashboardStats responsive layout', () => {
  it('uses one column on phones and gives every metric a shrinkable content area', () => {
    const wrapper = mount(UserDashboardStats, {
      props: {
        stats,
        balance: 123_456_789.12,
        isSimple: false,
        platformQuotas: []
      },
      global: {
        stubs: {
          Icon: true
        }
      }
    })

    const grids = wrapper.findAll('.user-stat-grid')
    expect(grids).toHaveLength(2)
    for (const grid of grids) {
      expect(grid.classes()).toContain('grid-cols-1')
      expect(grid.classes()).toContain('sm:grid-cols-2')
      expect(grid.classes()).toContain('lg:grid-cols-4')
      expect(grid.classes()).not.toContain('grid-cols-2')
    }

    const cards = wrapper.findAll('.user-stat-card')
    expect(cards).toHaveLength(8)
    for (const card of cards) {
      expect(card.find('.user-stat-row').classes()).toContain('min-w-0')
      expect(card.find('.user-stat-body').classes()).toContain('min-w-0')
    }

    expect(wrapper.text()).toContain('$123,456,789.12')
    expect(wrapper.text()).toContain('1000000000.0M')
  })

  it('keeps CSS guards for long metric values and the phone breakpoint', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/style.css'), 'utf8')

    expect(css).toContain('.anime-console .user-stat-card')
    expect(css).toContain('overflow-wrap: anywhere')
    expect(css).toMatch(/@media \(max-width: 639px\)[\s\S]*?\.anime-console \.user-stat-grid/)
  })
})
