<template>
  <div class="anime-console min-h-screen bg-gray-50 dark:bg-dark-950">
    <!-- Background Decoration -->
    <div class="pointer-events-none fixed inset-0 bg-mesh-gradient"></div>

    <!-- Sidebar -->
    <AppSidebar />

    <!-- Main Content Area -->
    <div
      class="relative min-h-screen transition-all duration-300"
      :class="[sidebarCollapsed ? 'lg:ml-[72px]' : 'lg:ml-64']"
    >
      <!-- Header -->
      <AppHeader />

      <!-- Main Content -->
      <main class="anime-console-main p-4 md:p-6 lg:p-8">
        <div class="anime-console-content">
          <section v-if="showCommandBanner" class="anime-command-banner">
            <div class="anime-command-copy">
              <span class="anime-command-kicker"><i></i> SUB2API / CONTROL DESK</span>
              <h1>{{ isAdmin ? '管理员控制台' : '欢迎回来' }}</h1>
              <p>{{ isAdmin ? '全局请求与账户状态，正在这里安静运行。' : '今天也一起认真工作吧。' }}</p>
              <div class="anime-command-signal"><i></i><span>CONTROL PLANE</span><strong>ONLINE</strong></div>
            </div>
            <div class="anime-command-visual" aria-hidden="true">
              <div class="anime-command-rings"></div>
              <img :src="supportMascot" alt="" />
              <div class="anime-command-note">
                <span>小助手</span>
                <strong>状态确认完成</strong>
                <small>SUB2API SYSTEM</small>
              </div>
            </div>
          </section>
          <slot />
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import '@/styles/onboarding.css'
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores'
import { useAuthStore } from '@/stores/auth'
import { useOnboardingTour } from '@/composables/useOnboardingTour'
import { useOnboardingStore } from '@/stores/onboarding'
import AppSidebar from './AppSidebar.vue'
import AppHeader from './AppHeader.vue'
import supportMascot from '@/assets/anime/support-mascot.png'

const appStore = useAppStore()
const authStore = useAuthStore()
const route = useRoute()
const sidebarCollapsed = computed(() => appStore.sidebarCollapsed)
const isAdmin = computed(() => authStore.user?.role === 'admin')
const showCommandBanner = computed(() => route.path === '/dashboard' || route.path === '/admin/dashboard')

const { replayTour } = useOnboardingTour({
  storageKey: isAdmin.value ? 'admin_guide' : 'user_guide',
  autoStart: true
})

const onboardingStore = useOnboardingStore()

onMounted(() => {
  onboardingStore.setReplayCallback(replayTour)
})

defineExpose({ replayTour })
</script>
