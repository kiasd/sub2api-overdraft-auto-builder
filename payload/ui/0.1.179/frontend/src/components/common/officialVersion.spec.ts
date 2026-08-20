import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OfficialVersionBadge from './OfficialVersionBadge.vue'
import {
  OFFICIAL_RELEASES_PAGE,
  compareOfficialVersions,
  officialBaseVersion,
  parseOfficialRelease
} from './officialVersion'

const stores = vi.hoisted(() => ({
  auth: { isAdmin: true },
  app: {
    siteVersion: '',
    fetchPublicSettings: vi.fn()
  }
}))

vi.mock('@/stores', () => ({
  useAuthStore: () => stores.auth,
  useAppStore: () => stores.app
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    locale: { value: 'zh-CN' },
    t: (key: string) => key
  })
}))

beforeEach(() => {
  stores.auth.isAdmin = true
  stores.app.siteVersion = ''
  stores.app.fetchPublicSettings.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('official version helpers', () => {
  it('extracts the official baseline from fusion build versions', () => {
    expect(officialBaseVersion('0.1.178-overdraft.1')).toBe('0.1.178')
    expect(officialBaseVersion('v0.1.179+fusion.abcdef')).toBe('0.1.179')
    expect(officialBaseVersion('not-a-version')).toBe('')
  })

  it('compares only official semantic version components', () => {
    expect(compareOfficialVersions('0.1.178-overdraft.9', 'v0.1.178')).toBe(0)
    expect(compareOfficialVersions('0.1.178-overdraft.9', 'v0.1.179')).toBe(-1)
    expect(compareOfficialVersions('0.2.0-overdraft.1', 'v0.1.999')).toBe(1)
    expect(compareOfficialVersions('invalid', 'v0.1.179')).toBeNull()
  })

  it('builds release links only for the official repository', () => {
    expect(
      parseOfficialRelease({
        tag_name: 'v0.1.179',
        html_url: 'https://example.invalid/untrusted-release'
      })
    ).toEqual({
      version: '0.1.179',
      url: `${OFFICIAL_RELEASES_PAGE}/tag/v0.1.179`
    })
    expect(parseOfficialRelease({ tag_name: '0.1.180' })).toEqual({
      version: '0.1.180',
      url: `${OFFICIAL_RELEASES_PAGE}/tag/0.1.180`
    })
    expect(parseOfficialRelease({ tag_name: 'overdraft-latest' })).toBeNull()
  })
})

describe('OfficialVersionBadge', () => {
  it('loads an empty current version from public settings before comparing releases', async () => {
    let resolveSettings: (value: { version: string }) => void = () => undefined
    stores.app.fetchPublicSettings.mockReturnValue(
      new Promise<{ version: string }>((resolve) => {
        resolveSettings = resolve
      })
    )
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ tag_name: 'v0.1.178' })
    })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(OfficialVersionBadge, { props: { version: '' } })
    await nextTick()
    await wrapper.get('button').trigger('click')
    expect(wrapper.text()).toContain('正在加载版本信息')
    expect(stores.app.fetchPublicSettings).toHaveBeenCalledWith(true)
    expect(fetchMock).not.toHaveBeenCalled()

    resolveSettings({ version: '0.1.178-overdraft.1' })
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('v0.1.178-overdraft.1')
    expect(wrapper.text()).toContain('version.upToDate')
    expect(wrapper.find('[data-testid="version-up-to-date"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a current-version failure without an up-to-date indicator', async () => {
    stores.app.fetchPublicSettings.mockResolvedValue(null)
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(OfficialVersionBadge, { props: { version: '' } })
    await flushPromises()
    await wrapper.get('button').trigger('click')

    expect(wrapper.text()).toContain('无法获取当前版本')
    expect(wrapper.text()).not.toContain('version.upToDate')
    expect(wrapper.find('[data-testid="version-up-to-date"]').exists()).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not show an up-to-date indicator for an invalid official release', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ tag_name: 'invalid-release' })
    })
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(OfficialVersionBadge, {
      props: { version: '0.1.178-overdraft.1' }
    })
    await flushPromises()
    await wrapper.get('button').trigger('click')

    expect(stores.app.fetchPublicSettings).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('检查官方版本失败')
    expect(wrapper.text()).not.toContain('version.upToDate')
    expect(wrapper.find('[data-testid="version-up-to-date"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
