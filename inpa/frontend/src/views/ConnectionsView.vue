<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ApiError, api } from '@/lib/api'

type Person = { public_id: string; connection_id: string; display_name: string; mutual?: boolean; following?: boolean; follows_me?: boolean; x_handle?: string; instagram_handle?: string }
const searchQuery = ref(''); const foundPeople = ref<Person[]>([]); const searched = ref(false)
const follows = ref<Person[]>([]); const followers = ref<Person[]>([]); const blocks = ref<Person[]>([])
const error = ref(''); const notice = ref('')
const message = (value: unknown) => value instanceof ApiError ? value.message : '通信に失敗しました。'

async function load() {
  try {
    const [followData, followerData, blockData] = await Promise.all([api<{ people: Person[] }>('/follows'), api<{ people: Person[] }>('/followers'), api<{ people: Person[] }>('/blocks')])
    follows.value = followData.people; followers.value = followerData.people; blocks.value = blockData.people
  } catch (value) { error.value = message(value) }
}
async function findPerson() {
  error.value = ''; foundPeople.value = []; searched.value = false
  const query = searchQuery.value.trim()
  try { foundPeople.value = (await api<{ people: Person[] }>(`/people/search?q=${encodeURIComponent(query)}`)).people; searched.value = true }
  catch (value) { error.value = message(value) }
}
function displayConnectionId(value: string) { return value.length === 8 ? `${value.slice(0, 4)}-${value.slice(4)}` : value }
async function follow(person: Person) {
  try { await api(`/follows/${person.public_id}`, { method: 'POST' }); notice.value = `${person.display_name}さんをフォローしました。`; if (foundPeople.value.some((item) => item.public_id === person.public_id)) await findPerson(); await load() }
  catch (value) { error.value = message(value) }
}
async function unfollow(person: Person) {
  try { await api(`/follows/${person.public_id}`, { method: 'DELETE' }); notice.value = 'フォローを解除しました。'; if (foundPeople.value.some((item) => item.public_id === person.public_id)) await findPerson(); await load() }
  catch (value) { error.value = message(value) }
}
async function block(person: Person) {
  if (!confirm(`${person.display_name}さんをブロックしますか？ 双方のフォローも解除されます。`)) return
  try { await api(`/blocks/${person.public_id}`, { method: 'POST' }); foundPeople.value = foundPeople.value.filter((item) => item.public_id !== person.public_id); notice.value = 'ブロックしました。'; await load() }
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
    <h1>つながり</h1><p class="helper">つながりID、またはプロフィールで公開されているX・Instagram IDで相手を探せます。SNS IDは完全一致で検索します。</p>
    <p v-if="error" class="error">{{ error }}</p><p v-if="notice" class="notice">{{ notice }}</p>
    <form class="actions" @submit.prevent="findPerson"><label class="field grow">つながりID／X ID／Instagram ID<input v-model="searchQuery" required maxlength="31" placeholder="ABCD-EFGH または @example" autocapitalize="none" autocomplete="off" spellcheck="false"></label><button class="button">検索</button></form>
    <div v-if="foundPeople.length" class="visit-list"><article v-for="person in foundPeople" :key="person.public_id" class="visit-card"><h2><RouterLink class="person-link" :to="`/people/${person.public_id}`">{{ person.display_name }}</RouterLink></h2><p>つながりID {{ displayConnectionId(person.connection_id) }}</p><p v-if="person.x_handle">X @{{ person.x_handle }}</p><p v-if="person.instagram_handle">Instagram @{{ person.instagram_handle }}</p><p v-if="person.follows_me">あなたをフォローしています</p><div class="actions"><button v-if="!person.following" class="button" @click="follow(person)">フォローする</button><button v-else class="button secondary" @click="unfollow(person)">解除</button><button class="button danger" @click="block(person)">ブロック</button></div></article></div>
    <p v-else-if="searched" class="helper">該当する利用者は見つかりませんでした。</p>
    <h2>フォロワー</h2><div class="visit-list"><article v-for="person in followers" :key="person.public_id" class="visit-card"><h3><RouterLink class="person-link" :to="`/people/${person.public_id}`">{{ person.display_name }}</RouterLink> <small v-if="person.mutual">相互フォロー</small></h3><p>つながりID {{ displayConnectionId(person.connection_id) }}</p><div class="actions"><button v-if="!person.following" class="button" @click="follow(person)">フォローする</button><button v-else class="button secondary" @click="unfollow(person)">解除</button><button class="button danger" @click="block(person)">ブロック</button></div></article><p v-if="!followers.length" class="helper">フォロワーはいません。</p></div>
    <h2>フォロー中</h2><div class="visit-list"><article v-for="person in follows" :key="person.public_id" class="visit-card"><h3><RouterLink class="person-link" :to="`/people/${person.public_id}`">{{ person.display_name }}</RouterLink> <small v-if="person.mutual">相互フォロー</small></h3><p>つながりID {{ displayConnectionId(person.connection_id) }}</p><button class="button secondary" @click="unfollow(person)">解除</button></article><p v-if="!follows.length" class="helper">フォロー中の利用者はいません。</p></div>
    <h2>ブロック中</h2><div class="visit-list"><article v-for="person in blocks" :key="person.public_id" class="visit-card"><h3>{{ person.display_name }}</h3><button class="button secondary" @click="unblock(person)">ブロック解除</button></article><p v-if="!blocks.length" class="helper">ブロック中の利用者はいません。</p></div>
  </section>
</template>
