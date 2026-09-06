<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import LoadingBlock from '@/components/LoadingBlock.vue';
import EmptyState from '@/components/EmptyState.vue';
import { portalApi } from '@/api/client';
import type { EventItem, EventMemberItem } from '@/types';
import { eventThemeStyle, useDocumentEventTheme } from '@/utils/eventTheme';

const route = useRoute();
const router = useRouter();
const event = ref<EventItem | null>(null);
useDocumentEventTheme(computed(() => event.value?.themeColor));
const members = ref<EventMemberItem[]>([]);
const loading = ref(true);
const error = ref('');

function roleLabel(member: EventMemberItem) {
  if (member.isHost) return member.participantRole === 'camera' ? '主催・カメラマン' : '主催';
  if (member.isSubhost) return member.participantRole === 'camera' ? '副主催・カメラマン' : '副主催';
  if (member.participantRole === 'camera') return 'カメラマン';
  if (member.participantRole === 'assistant') return 'アシスタント';
  return member.costumeLabel || '衣装・その他';
}

onMounted(async () => {
  try {
    const uuid = String(route.params.uuid);
    const [eventResponse, memberResponse] = await Promise.all([portalApi.event(uuid), portalApi.eventMembers(uuid)]);
    event.value = eventResponse.event;
    members.value = memberResponse.members;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '参加者一覧を取得できませんでした。';
  } finally {
    loading.value = false;
  }
});
</script>

<template>
 <div class="event-theme" :style="eventThemeStyle(event?.themeColor)">
  <button type="button" class="back-link" @click="router.push({ name: 'event', params: { uuid: route.params.uuid } })">← イベント詳細</button>
  <LoadingBlock v-if="loading">参加者一覧を読み込んでいます</LoadingBlock>
  <div v-else-if="error" class="alert error">{{ error }}</div>
  <template v-else-if="event">
    <section class="page-heading"><div><p class="eyebrow">MEMBERS</p><h1>参加者一覧</h1><p>{{ event.title }}</p></div></section>
    <EmptyState v-if="!members.length" icon="👥" title="参加者はいません" text="承認済みの参加者がここに表示されます。" />
    <div v-else class="member-card-grid">
      <article v-for="member in members" :key="member.id" class="member-card">
        <img v-if="member.avatarUrl" :src="member.avatarUrl" alt="" referrerpolicy="no-referrer">
        <div class="member-card-heading">
          <strong><span v-if="member.checkinAt">✅ </span><span v-if="member.isHost">👑👑 </span><span v-else-if="member.isSubhost">👑 </span>{{ member.nickname }}</strong>
          <small>{{ roleLabel(member) }}<template v-if="member.costumeLabel">（{{ member.costumeLabel }}）</template></small>
        </div>
        <dl class="member-social-list">
          <div><dt>X</dt><dd><span v-if="member.xId">@{{ member.xId.replace(/^@/, '') }}</span><span v-else>—</span><a v-if="member.xId" :href="`https://x.com/${member.xId.replace(/^@/, '')}`" target="_blank" rel="noopener">開く</a></dd></div>
          <div><dt>Instagram</dt><dd><span v-if="member.instagramId">@{{ member.instagramId.replace(/^@/, '') }}</span><span v-else>—</span><a v-if="member.instagramId" :href="`https://www.instagram.com/${member.instagramId.replace(/^@/, '')}/`" target="_blank" rel="noopener">開く</a></dd></div>
        </dl>
      </article>
    </div>
  </template>
 </div>
</template>
