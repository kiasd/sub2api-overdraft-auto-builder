export const OFFICIAL_REPOSITORY = 'Wei-Shaw/sub2api'
export const OFFICIAL_RELEASES_API = `https://api.github.com/repos/${OFFICIAL_REPOSITORY}/releases/latest`
export const OFFICIAL_RELEASES_PAGE = `https://github.com/${OFFICIAL_REPOSITORY}/releases`

export interface OfficialReleaseSummary {
  version: string
  url: string
}

const VERSION_PREFIX = /^v?(\d+)\.(\d+)\.(\d+)(?=$|[-+])/i

function versionParts(value: string): [number, number, number] | null {
  const match = VERSION_PREFIX.exec(value.trim())
  if (!match) return null
  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

export function officialBaseVersion(value: string | null | undefined): string {
  if (!value) return ''
  const parts = versionParts(value)
  return parts ? parts.join('.') : ''
}

export function compareOfficialVersions(current: string, latest: string): number | null {
  const currentParts = versionParts(current)
  const latestParts = versionParts(latest)
  if (!currentParts || !latestParts) return null

  for (let index = 0; index < currentParts.length; index += 1) {
    if (currentParts[index] < latestParts[index]) return -1
    if (currentParts[index] > latestParts[index]) return 1
  }
  return 0
}

export function parseOfficialRelease(payload: unknown): OfficialReleaseSummary | null {
  if (!payload || typeof payload !== 'object') return null
  const tag = (payload as { tag_name?: unknown }).tag_name
  if (typeof tag !== 'string') return null
  const normalizedTag = tag.trim()
  const version = officialBaseVersion(normalizedTag)
  if (!version) return null

  return {
    version,
    url: `${OFFICIAL_RELEASES_PAGE}/tag/${encodeURIComponent(normalizedTag)}`
  }
}
