<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError, api } from '@/lib/api'
import { formatJapaneseDate } from '@/lib/date'
type Shared = { profile: { display_name: string; connection_id: string; x_handle?: string; instagram_handle?: string }; visits: Array<Record<string, string | null>> }
const route = useRoute(); const data = ref<Shared | null>(null); const error = ref('')
function displayConnectionId(value: string) { return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4)}` : value }
onMounted(async () => { try { data.value = await api<Shared>(`/share/${encodeURIComponent(String(route.params.token))}`) } catch (value) { error.value = value instanceof ApiError ? value.message : '読み込めませんでした。' } })
</script>
<template><section class="panel"><p v-if="error" class="error">{{ error }}</p><template v-if="data"><h1>{{ data.profile.display_name }}さんの予定</h1><p><strong>つながりID {{ displayConnectionId(data.profile.connection_id) }}</strong></p><p v-if="data.profile.x_handle"><a :href="`https://x.com/${data.profile.x_handle}`" rel="noopener">X @{{ data.profile.x_handle }}</a></p><p v-if="data.profile.instagram_handle"><a :href="`https://instagram.com/${data.profile.instagram_handle}`" rel="noopener">Instagram @{{ data.profile.instagram_handle }}</a></p><div class="visit-list"><article v-for="(visit, index) in data.visits" :key="index" class="visit-card"><h2 v-if="visit.visit_date">{{ formatJapaneseDate(visit.visit_date) }}</h2><p v-if="visit.park">パーク {{ visit.park }}</p><p v-if="visit.costume">服装 {{ visit.costume }}</p><p v-if="visit.memo">{{ visit.memo }}</p></article><p v-if="!data.visits.length" class="notice">公開中の予定はありません。</p></div></template></section></template>
