<template>
  <div class="anime-auth-shell">
    <div class="anime-auth-grid" aria-hidden="true"></div>

    <div class="anime-auth-frame">
      <section class="anime-auth-visual" aria-label="Sub2API visual operator">
        <div class="anime-auth-visual-index" aria-hidden="true">
          <span>SUB2API</span>
          <b>02</b>
        </div>

        <div class="anime-auth-visual-copy">
          <span><i></i> RELAY CONTROL / ONLINE</span>
          <h2>连接已就绪</h2>
          <p>今天也一起把每一条请求送到正确的位置。</p>
        </div>

        <img :src="relayOperator" alt="Sub2API 二次元控制台角色" class="anime-auth-character" />

        <div class="anime-auth-status" aria-hidden="true">
          <header><span><i></i> LIVE SIGNAL</span><b>READY</b></header>
          <div>
            <span><small>ROUTING</small><strong>ACTIVE</strong></span>
            <span><small>GATEWAY</small><strong>ONLINE</strong></span>
          </div>
        </div>
      </section>

      <section class="anime-auth-panel">
        <div class="anime-auth-panel-inner">
          <div class="anime-auth-brand">
            <div class="anime-auth-logo">
              <img :src="siteLogo || '/logo.svg'" alt="Logo" />
            </div>
            <div>
              <h1>{{ siteName }}</h1>
              <p>{{ siteSubtitle }}</p>
            </div>
          </div>

          <div class="anime-auth-card card-glass">
            <slot />
          </div>

          <div class="anime-auth-footer">
            <slot name="footer" />
          </div>

          <div class="anime-auth-copyright">
            &copy; {{ currentYear }} {{ siteName }}. All rights reserved.
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useAppStore } from '@/stores'
import { sanitizeUrl } from '@/utils/url'
import relayOperator from '@/assets/anime/relay-operator.png'

const appStore = useAppStore()

const siteName = computed(() => appStore.siteName || 'Sub2API')
const siteLogo = computed(() => sanitizeUrl(appStore.siteLogo || '', { allowRelative: true, allowDataUrl: true }))
const siteSubtitle = computed(() => appStore.cachedPublicSettings?.site_subtitle || 'Subscription to API Conversion Platform')
const currentYear = computed(() => new Date().getFullYear())

onMounted(() => {
  appStore.fetchPublicSettings()
})
</script>
