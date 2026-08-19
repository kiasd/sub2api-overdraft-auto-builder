import { describe, expect, it } from 'vitest'
import {
  OFFICIAL_RELEASES_PAGE,
  compareOfficialVersions,
  officialBaseVersion,
  parseOfficialRelease
} from './officialVersion'

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
