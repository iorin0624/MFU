<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'

type Person = { public_id: string; connection_id: string; display_name: string; mutual?: boolean; following?: boolean; follows_me?: boolean; x_handle?: string; instagram_handle?: string }
const connectionId = ref(''); const found = ref<Person | null>(null)
const follows = ref<Person[]>([]); const blocks = ref<Person[]>([])
const error = ref(''); const notice = ref('')
const message = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

async function load() {
  try {
    const [followData, blockData] = await Promise.all([api<{ people: Person[] }>('/follows'), api<{ people: Person[] }>('/blocks')])
    follows.value = followData.people; blocks.value = blockData.people
  } catch (value) { error.value = message(value) }
}
async function findPerson() {
  error.value = ''; found.value = null
  const normalized = connectionId.value.replace(/[-\s]/g, '').toUpperCase()
  try { found.value = (await api<{ person: Person }>(`/people/by-connection-id/${encodeURIComponent(normalized)}`)).person }
  catch (value) { error.value = message(value) }
}
function displayConnectionId(value: string) { return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4)}` : value }
async function follow(person: Person) {
  try { await api(`/follows/${person.public_id}`, { method: 'POST' }); notice.value = `${person.display_name}さんを登録しました。`; await findPerson(); await load() }
  catch (value) { error.value = message(value) }
}
async function unfollow(person: Person) {
  try { await api(`/follows/${person.public_id}`, { method: 'DELETE' }); notice.value = '登録を解除しました。'; if (found.value?.public_id === person.public_id) await findPerson(); await load() }
  catch (value) { error.value = message(value) }
}
async function block(person: Person) {
  if (!confirm(`${person.display_name}さんをブロックしますか？ 双方の登録も解除されます。`)) return
  try { await api(`/blocks/${person.public_id}`, { method: 'POST' }); found.value = null; notice.value = 'ブロックしました。'; await load() }
  catch (value) { error.value = message(value) }
}
async function unblock(person: Person) {
  try { await api(`/blocks/${person.public_id}`, { method: 'DELETE' }); notice.value = 'ブロックを解除しました。'; await load() }
  catch (value) { error.value = message(value) }
}
onMounted(load)
</script>

<template>
  <section class="panel">
    <h1>つながり</h1><p class="helper">共有画面に表示される8文字のつながりIDで相手を探せます。</p>
    <p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <form class="actions" @submit.prevent="findPerson"><label class="field grow">つながりID<input v-model="connectionId" required maxlength="9" placeholder="ABCD-EFGH" autocapitalize="characters"></label><button class="button">確認</button></form>
    <article v-if="found" class="visit-card"><h2>{{ found.display_name }}</h2><p>つながりID {{ displayConnectionId(found.connection_id) }}</p><p v-if="found.follows_me">あなたを登録しています</p><div class="actions"><button v-if="!found.following" class="button" @click="follow(found)">登録する</button><button v-else class="button secondary" @click="unfollow(found)">登録解除</button><button class="button danger" @click="block(found)">ブロック</button></div></article>
    <h2>登録中</h2><div class="visit-list"><article v-for="person in follows" :key="person.public_id" class="visit-card"><h3>{{ person.display_name }} <small v-if="person.mutual">相互登録</small></h3><p>つながりID {{ displayConnectionId(person.connection_id) }}</p><button class="button secondary" @click="unfollow(person)">登録解除</button></article><p v-if="!follows.length" class="helper">登録中の利用者はいません。</p></div>
    <h2>ブロック中</h2><div class="visit-list"><article v-for="person in blocks" :key="person.public_id" class="visit-card"><h3>{{ person.display_name }}</h3><button class="button secondary" @click="unblock(person)">ブロック解除</button></article><p v-if="!blocks.length" class="helper">ブロック中の利用者はいません。</p></div>
  </section>
</template>
