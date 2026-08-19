<template>
  <div ref="rootRef" class="relative">
    <template v-if="isAdmin">
      <button
        type="button"
        class="flex max-w-full items-center gap-1.5 rounded-lg bg-gray-100 px-2 py-1 text-xs text-gray-600 transition-colors hover:bg-gray-200 dark:bg-dark-800 dark:text-dark-400 dark:hover:bg-dark-700"
        :class="hasUpdate ? 'text-amber-700 dark:text-amber-400' : ''"
        :title="checked && hasUpdate ? t('version.updateAvailable') : t('version.currentVersion')"
        @click.stop="dropdownOpen = !dropdownOpen"
      >
        <span v-if="fullVersion" class="truncate font-medium">v{{ fullVersion }}</span>
        <span v-else class="h-3 w-12 animate-pulse rounded bg-gray-200 dark:bg-dark-600"></span>
        <span v-if="checked && hasUpdate" class="relative flex h-2 w-2 flex-shrink-0">
          <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75"></span>
          <span class="relative inline-flex h-2 w-2 rounded-full bg-amber-500"></span>
        </span>
      </button>

      <transition name="dropdown">
        <div
          v-if="dropdownOpen"
          class="absolute left-0 z-50 mt-2 w-72 overflow-hidden whitespace-normal rounded-xl border border-gray-200 bg-white shadow-lg dark:border-dark-700 dark:bg-dark-800"
        >
          <div class="flex items-center justify-between border-b border-gray-100 px-4 py-3 dark:border-dark-700">
            <span class="text-sm font-medium text-gray-700 dark:text-dark-300">
              {{ t('version.currentVersion') }}
            </span>
            <button
              type="button"
              class="rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600 disabled:cursor-wait disabled:opacity-50 dark:hover:bg-dark-700 dark:hover:text-dark-200"
              :disabled="loading"
              :title="t('version.refresh')"
              @click="refreshOfficialRelease"
            >
              <Icon
                name="refresh"
                size="sm"
                :stroke-width="2"
                :class="{ 'animate-spin': loading }"
              />
            </button>
          </div>

          <div class="space-y-3 p-4">
            <div class="text-center">
              <div class="inline-flex items-center gap-2">
                <span class="text-2xl font-bold text-gray-900 dark:text-white">
                  {{ officialCurrentVersion ? `v${officialCurrentVersion}` : '--' }}
                </span>
                <span
                  v-if="checked && !hasUpdate"
                  class="flex h-5 w-5 items-center justify-center rounded-full bg-green-100 dark:bg-green-900/30"
                >
                  <Icon name="check" size="xs" :stroke-width="2.5" class="text-green-600 dark:text-green-400" />
                </span>
              </div>
              <div v-if="isFusionBuild" class="mt-2">
                <span class="inline-flex max-w-full items-center rounded-md bg-primary-50 px-2 py-1 text-[11px] font-medium text-primary-700 dark:bg-primary-900/20 dark:text-primary-300">
                  <span class="truncate">{{ customBuildLabel }} · v{{ fullVersion }}</span>
                </span>
              </div>
              <p v-if="checked" class="mt-2 text-xs text-gray-500 dark:text-dark-400">
                {{ hasUpdate ? `${t('version.latestVersion')}: v${latestVersion}` : t('version.upToDate') }}
              </p>
            </div>

            <div
              v-if="errorMessage"
              class="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600 dark:border-red-800/50 dark:bg-red-900/20 dark:text-red-400"
            >
              {{ errorMessage }}
            </div>

            <a
              :href="releaseUrl"
              target="_blank"
              rel="noopener noreferrer"
              class="flex items-center justify-center gap-2 py-2 text-sm text-gray-500 transition-colors hover:text-gray-700 dark:text-dark-400 dark:hover:text-dark-200"
            >
              <Icon name="link" size="sm" :stroke-width="2" />
              {{ t('version.viewRelease') }}
              <Icon name="externalLink" size="xs" :stroke-width="2" />
            </a>
          </div>
        </div>
      </transition>
    </template>

    <span v-else-if="fullVersion" class="text-xs text-gray-500 dark:text-dark-400">
      v{{ fullVersion }}
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '@/stores'
import Icon from '@/components/icons/Icon.vue'
import {
  OFFICIAL_RELEASES_API,
  OFFICIAL_RELEASES_PAGE,
  compareOfficialVersions,
  officialBaseVersion,
  parseOfficialRelease
} from './officialVersion'

const props = defineProps<{
  version?: string
}>()

const { locale, t } = useI18n()
const authStore = useAuthStore()
const rootRef = ref<HTMLElement | null>(null)
const dropdownOpen = ref(false)
const loading = ref(false)
const checked = ref(false)
const latestVersion = ref('')
const releaseUrl = ref(OFFICIAL_RELEASES_PAGE)
const errorMessage = ref('')

const isAdmin = computed(() => authStore.isAdmin)
const fullVersion = computed(() => (props.version?.trim() || '').replace(/^v/i, ''))
const officialCurrentVersion = computed(() => officialBaseVersion(fullVersion.value))
const isFusionBuild = computed(
  () => !!fullVersion.value && fullVersion.value !== officialCurrentVersion.value
)
const hasUpdate = computed(
  () =>
    compareOfficialVersions(officialCurrentVersion.value, latestVersion.value) === -1
)
const customBuildLabel = computed(() =>
  String(locale.value).toLowerCase().startsWith('zh') ? '自用构建' : 'Custom build'
)
const checkFailedLabel = computed(() =>
  String(locale.value).toLowerCase().startsWith('zh')
    ? '检查官方版本失败'
    : 'Official version check failed'
)

async function refreshOfficialRelease(): Promise<void> {
  if (!isAdmin.value || loading.value) return
  loading.value = true
  errorMessage.value = ''

  try {
    const response = await fetch(OFFICIAL_RELEASES_API, {
      cache: 'no-store',
      headers: { Accept: 'application/vnd.github+json' }
    })
    if (!response.ok) throw new Error(`GitHub HTTP ${response.status}`)
    const release = parseOfficialRelease(await response.json())
    if (!release) throw new Error('Invalid official release response')
    latestVersion.value = release.version
    releaseUrl.value = release.url
    checked.value = true
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : String(error)
    errorMessage.value = `${checkFailedLabel.value}: ${detail}`
  } finally {
    loading.value = false
  }
}

function handleClickOutside(event: MouseEvent): void {
  const target = event.target
  if (target instanceof Node && rootRef.value && !rootRef.value.contains(target)) {
    dropdownOpen.value = false
  }
}

onMounted(() => {
  if (isAdmin.value) void refreshOfficialRelease()
  document.addEventListener('click', handleClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', handleClickOutside)
})
</script>

<style scoped>
.dropdown-enter-active,
.dropdown-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.dropdown-enter-from,
.dropdown-leave-to {
  opacity: 0;
  transform: scale(0.96) translateY(-4px);
}
</style>
