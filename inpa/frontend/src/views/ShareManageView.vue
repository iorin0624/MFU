<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'
type Status = { active: boolean; token_last4?: string; created_at?: string; last_used_at?: string | null }
const status = ref<Status>({ active: false }); const url = ref(''); const error = ref(''); const copied = ref(false)
const message = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'
async function load() { try { status.value = await api<Status>('/share-token') } catch (value) { error.value = message(value) } }
async function create() { if (status.value.active && !confirm('現在の共有URLを無効にして再発行しますか？')) return; try { url.value = (await api<{ url: string }>(status.value.active ? '/share-token/rotate' : '/share-token', { method: 'POST' })).url; await load() } catch (value) { error.value = message(value) } }
async function revoke() { if (!confirm('共有URLを無効にしますか？')) return; try { await api('/share-token', { method: 'DELETE' }); url.value = ''; await load() } catch (value) { error.value = message(value) } }
async function copy() { await navigator.clipboard.writeText(url.value); copied.value = true }
onMounted(load)
</script>
<template><section class="panel"><h1>共有URL</h1><p v-if="error" class="error">{{ error }}</p><p>共有URLを知っている人に、予定ごとの公開設定に応じて情報を表示します。</p><p v-if="status.active" class="notice">有効なURLがあります（末尾 {{ status.token_last4 }}）。安全のためURL本体は再表示できません。</p><p v-else class="notice">有効な共有URLはありません。</p><div v-if="url" class="share-result"><input :value="url" readonly><button class="button" @click="copy">コピー</button><span v-if="copied">コピーしました</span></div><div class="actions"><button class="button" @click="create">{{ status.active ? 'URLを再発行' : 'URLを発行' }}</button><button v-if="status.active" class="button danger" @click="revoke">無効にする</button></div></section></template>
