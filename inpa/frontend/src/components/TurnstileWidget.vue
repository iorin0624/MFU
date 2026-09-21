<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

const emit = defineEmits<{ token: [value: string] }>()
const container = ref<HTMLElement>()
const siteKey = import.meta.env.VITE_TURNSTILE_SITE_KEY as string | undefined
let widgetId: string | undefined

declare global {
  interface Window {
    turnstile?: { render: (element: HTMLElement, options: { sitekey: string; callback: (token: string) => void; 'expired-callback': () => void }) => string; remove: (id: string) => void }
  }
}

onMounted(() => {
  if (!siteKey || !container.value) return
  const render = () => {
    if (container.value && window.turnstile) {
      widgetId = window.turnstile.render(container.value, { sitekey: siteKey, callback: (token) => emit('token', token), 'expired-callback': () => emit('token', '') })
    }
  }
  if (window.turnstile) render()
  else {
    const script = document.createElement('script')
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
    script.async = true
    script.onload = render
    document.head.appendChild(script)
  }
})

onBeforeUnmount(() => { if (widgetId && window.turnstile) window.turnstile.remove(widgetId) })
</script>

<template>
  <div ref="container" aria-label="人間であることの確認"></div>
  <p v-if="!siteKey" class="helper">開発環境では Turnstile を省略できます。本番ではサイトキーを設定してください。</p>
</template>
