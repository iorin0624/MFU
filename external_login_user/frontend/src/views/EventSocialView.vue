<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import LoadingBlock from '@/components/LoadingBlock.vue';
import { portalApi } from '@/api/client';
import type { EventItem, EventMemberItem } from '@/types';

const route = useRoute();
const router = useRouter();
const event = ref<EventItem | null>(null);
const members = ref<EventMemberItem[]>([]);
const loading = ref(true);
const error = ref('');
const copied = ref('');

function roleLabel(member: EventMemberItem) {
  const costume = member.costumeLabel?.trim();
  if (member.isHost && member.participantRole === 'camera') return '主催＆カメラマン';
  if (member.isHost && member.participantRole === 'assistant') return '主催＆アシスタント';
  if (member.isSubhost && member.participantRole === 'camera') return '副主催＆カメラマン';
  if (member.isSubhost && member.participantRole === 'assistant') return '副主催＆アシスタント';
  if (member.isHost) return costume ? `主催＆${costume}` : '主催';
  if (member.isSubhost) return costume ? `副主催＆${costume}` : '副主催';
  if (member.participantRole === 'camera') return 'カメラマン';
  if (member.participantRole === 'assistant') return 'アシスタント';
  return costume || '衣装';
}

function buildText(platform: 'x' | 'instagram') {
  const tag = event.value?.snsHashtag?.replace(/^#/, '').trim();
  const lines = members.value.map((member) => {
    const handle = (platform === 'x' ? member.xId : member.instagramId)?.replace(/^@/, '').trim();
    return `${roleLabel(member)}　${member.nickname}さん　${handle ? `@${handle}` : ''}`;
  });
  return [...(tag ? [`#${tag}`, ''] : []), ...lines].join('\n');
}

const xText = computed(() => buildText('x'));
const instagramText = computed(() => buildText('instagram'));

async function copy(value: string, key: string) {
  await navigator.clipboard.writeText(value);
  copied.value = key;
  window.setTimeout(() => { copied.value = ''; }, 2000);
}

onMounted(async () => {
  try {
    const uuid = String(route.params.uuid);
    const [eventResponse, memberResponse] = await Promise.all([portalApi.event(uuid), portalApi.eventMembers(uuid)]);
    event.value = eventResponse.event;
    members.value = memberResponse.members;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '参加者情報を取得できませんでした。';
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <button type="button" class="back-link" @click="router.push({ name: 'event', params: { uuid: route.params.uuid } })">← イベント詳細</button>
  <LoadingBlock v-if="loading">参加者のSNS情報を読み込んでいます</LoadingBlock>
  <div v-else-if="error" class="alert error">{{ error }}</div>
  <template v-else-if="event">
    <section class="page-heading"><div><p class="eyebrow">SOCIAL COPY</p><h1>SNS貼付用</h1><p>{{ event.title }}</p></div></section>
    <div class="social-copy-layout">
      <section class="panel social-copy-panel">
        <h2>SNS貼付用</h2>
        <label>Instagram用<textarea :value="instagramText" readonly rows="12"></textarea></label>
        <button type="button" class="button secondary" @click="copy(instagramText, 'instagram')">{{ copied === 'instagram' ? 'コピーしました' : 'Instagram用をコピー' }}</button>
        <label>X用<textarea :value="xText" readonly rows="12"></textarea></label>
        <button type="button" class="button secondary" @click="copy(xText, 'x')">{{ copied === 'x' ? 'コピーしました' : 'X用をコピー' }}</button>
      </section>
    </div>
  </template>
</template>
