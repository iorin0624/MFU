<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '@/lib/api'
import { formatJapaneseDateTime } from '@/lib/date'

type Release = {
  public_id: string; version: string; change_type: 'major' | 'feature' | 'fix';
  title: string; content_markdown: string; published_at: string
}
const releases = ref<Release[]>([])
const error = ref('')
const labels = { major: 'メジャー', feature: '機能追加', fix: '修正' }

onMounted(async () => {
  try { releases.value = (await api<{ releases: Release[] }>('/releases')).releases }
  catch { error.value = 'アップデート情報を読み込めませんでした。' }
})
</script>

<template>
  <section class="panel updates-panel">
    <h1>アップデート情報</h1>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-else class="update-list">
      <article v-for="release in releases" :key="release.public_id" class="update-card">
        <div class="update-heading"><div><span class="update-kind">{{ labels[release.change_type] }}</span><h2>v{{ release.version }} {{ release.title }}</h2></div><time>{{ formatJapaneseDateTime(release.published_at) }}</time></div>
        <p class="update-content">{{ release.content_markdown }}</p>
      </article>
      <p v-if="!releases.length" class="helper">公開済みのアップデート情報はありません。</p>
    </div>
  </section>
</template>
