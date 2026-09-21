<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'
type Status = { active: boolean; token_last4?: string; created_at?: string; last_used_at?: string | null; url?: string; qr_svg?: string }
const status = ref<Status>({ active: false }); const error = ref(''); const copied = ref(false)
const message = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'
async function load() { try { status.value = await api<Status>('/share-token') } catch (value) { error.value = message(value) } }
async function create() {
  if (status.value.active && !confirm('現在の共有URLを直ちに無効にして再発行しますか？')) return
  try { status.value = { active: true, ...(await api<{ url: string; qr_svg: string }>(status.value.active ? '/share-token/rotate' : '/share-token', { method: 'POST' })) }; copied.value = false }
  catch (value) { error.value = message(value) }
}
async function revoke() { if (!confirm('共有URLを無効にしますか？')) return; try { await api('/share-token', { method: 'DELETE' }); await load() } catch (value) { error.value = message(value) } }
async function copy() { if (!status.value.url) return; await navigator.clipboard.writeText(status.value.url); copied.value = true }
onMounted(load)
</script>
<template><section class="panel"><h1>共有URL</h1><p v-if="error" class="error">{{ error }}</p><p>この短縮URLはログイン不要で開けます。予定ごとの公開設定に応じて、匿名閲覧者へ表示する情報を制限します。</p><template v-if="status.active"><p class="notice">有効なURLがあります（末尾 {{ status.token_last4 }}）。再発行すると旧URLは直ちに無効になります。</p><div v-if="status.url" class="share-result"><input :value="status.url" readonly><button class="button" @click="copy">コピー</button><span v-if="copied">コピーしました</span><div v-if="status.qr_svg" class="qr-code" v-html="status.qr_svg"></div></div><p v-else class="helper">旧形式のURLは安全上再表示できません。再発行すると短縮URLとQRを表示できます。</p></template><p v-else class="notice">有効な共有URLはありません。</p><div class="actions"><button class="button" @click="create">{{ status.active ? 'URLを再発行' : 'URLを発行' }}</button><button v-if="status.active" class="button danger" @click="revoke">無効にする</button></div></section></template>
