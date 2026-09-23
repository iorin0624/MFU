<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/lib/api'

const route = useRoute()
const document = ref<{ title: string; version: string; content_markdown: string; effective_at: string } | null>(null)
const error = ref('')
async function load() {
  try { document.value = (await api<{ document: typeof document.value }>(`/legal/documents/${route.params.type}`)).document }
  catch { error.value = '文書を読み込めませんでした。' }
}
function printDocument() { window.print() }
onMounted(load); watch(() => route.params.type, load)
</script>

<template><section class="panel legal-document">
  <p v-if="error" class="error">{{ error }}</p>
  <template v-else-if="document">
    <div class="section-heading"><div><h1>{{ document.title }}</h1><p>Version {{ document.version }}</p></div><button class="button button-secondary" @click="printDocument">印刷・PDF保存</button></div>
    <pre>{{ document.content_markdown }}</pre>
  </template>
</section></template>

<style scoped>.legal-document pre{white-space:pre-wrap;font:inherit;line-height:1.8}.section-heading{display:flex;justify-content:space-between;gap:1rem}</style>
