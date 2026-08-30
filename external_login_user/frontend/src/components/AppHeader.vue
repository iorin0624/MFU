<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { usePortalStore } from '@/stores/portal';

const store = usePortalStore();
const router = useRouter();
const route = useRoute();
const open = ref(false);
const unread = computed(() => store.session?.unread.notifications || 0);

function activate(item: { id: string; url: string }) {
  open.value = false;
  if (item.id === 'home' || item.id === 'events') {
    router.push('/');
    return;
  }
  if (item.id === 'notifications') { router.push('/notifications'); return; }
  if (item.id === 'account') { router.push('/profile'); return; }
  window.location.assign(item.url);
}
</script>

<template>
  <header class="app-header">
    <div class="header-inner">
      <button class="brand" type="button" @click="router.push('/')" aria-label="イベント一覧へ">
        <span class="brand-mark">M</span>
        <span>Mimoria</span>
      </button>
      <button class="menu-button" type="button" :aria-expanded="open" aria-label="メニュー" @click="open = !open">
        <span></span><span></span><span></span>
      </button>
      <nav :class="['main-nav', { open }]" aria-label="イベント管理ナビゲーション">
        <button
          v-for="item in store.session?.navigation || []"
          :key="item.id"
          type="button"
          :class="['nav-link', { active: (item.id === 'events' || item.id === 'home') && route.name === 'events' }]"
          @click="activate(item)"
        >
          {{ item.label }}
          <span v-if="item.badge === 'notifications' && unread" class="badge">{{ unread > 99 ? '99+' : unread }}</span>
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
