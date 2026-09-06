<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { usePortalStore } from '@/stores/portal';

const store = usePortalStore();
const router = useRouter();
const route = useRoute();
const open = ref(false);
const totalUnread = computed(() => store.session?.unread.total || 0);
const noticeUnread = computed(() => store.session?.unread.notifications || 0);
const chatUnread = computed(() => store.session?.unread.chat || 0);
const navigationItems = computed(() => store.mfuChatAuthenticated ? [
  { id: 'events-admin', label: 'イベント管理', url: '/external-login/admin/events' },
  { id: 'chat', label: 'チャット', url: '/external-login/app/chat?auth_scope=mfu' },
  { id: 'notifications-admin', label: '通知', url: '/mfu-notifications' },
] : (store.session?.navigation || []));

function badgeText(value: number) {
  return value > 99 ? '99+' : String(value);
}

function navAriaLabel(item: { label: string; badge?: 'chat' | 'notifications' }) {
  const value = item.badge === 'chat' ? chatUnread.value : item.badge === 'notifications' ? noticeUnread.value : 0;
  return value ? `${item.label}、未読${value}件` : item.label;
}

function activate(item: { id: string; url: string }) {
  open.value = false;
  if (item.id === 'home' || item.id === 'events') {
    router.push('/');
    return;
  }
  if (item.id === 'notifications') { router.push('/notifications'); return; }
  if (item.id === 'chat') { router.push(store.mfuChatAuthenticated ? { path: '/chat', query: { auth_scope: 'mfu' } } : '/chat'); return; }
  if (item.id === 'account') { router.push('/profile'); return; }
  window.location.assign(item.url);
}

function openBrand() {
  if (store.mfuChatAuthenticated) window.location.assign('/external-login/admin/events');
  else void router.push('/');
}
</script>

<template>
  <header class="app-header">
    <div class="header-inner">
      <button class="brand" type="button" @click="openBrand" aria-label="イベント一覧へ">
        <span class="brand-mark">M</span>
        <span>Mimoria</span>
      </button>
      <button class="menu-button" type="button" :aria-expanded="open" :aria-label="totalUnread ? `メニュー、未読${totalUnread}件` : 'メニュー'" @click="open = !open">
        <span></span><span></span><span></span>
        <b v-if="totalUnread" class="menu-badge" aria-hidden="true">{{ badgeText(totalUnread) }}</b>
      </button>
      <nav :class="['main-nav', { open }]" aria-label="イベント管理ナビゲーション">
        <button
          v-for="item in navigationItems"
          :key="item.id"
          type="button"
          :aria-label="navAriaLabel(item)"
          :class="['nav-link', { active: ((item.id === 'events' || item.id === 'home') && route.name === 'events') || (item.id === 'chat' && String(route.name || '').includes('chat')) || (item.id === 'notifications' && route.name === 'notifications') }]"
          @click="activate(item)"
        >
          {{ item.label }}
          <span v-if="item.badge === 'notifications' && noticeUnread" class="badge">{{ badgeText(noticeUnread) }}</span>
          <span v-if="item.badge === 'chat' && chatUnread" class="badge">{{ badgeText(chatUnread) }}</span>
        </button>
        <button v-if="store.session?.authenticated" type="button" class="nav-link logout" @click="store.logout()">ログアウト</button>
      </nav>
      <div v-if="store.displayName" class="account-chip">
        <img v-if="store.session?.profile?.avatarUrl" :src="store.session.profile.avatarUrl" alt="" referrerpolicy="no-referrer">
        <span>{{ store.displayName }}</span>
      </div>
    </div>
  </header>
</template>
