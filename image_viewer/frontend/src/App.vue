<script setup lang="ts">
import { onMounted } from 'vue';
import { runtimeConfig } from '@/config';
import { useDesktopStore } from '@/stores/desktop';
import { useNotificationStore } from '@/stores/notifications';
import ExplorerWindow from '@/components/ExplorerWindow.vue';
import ImageViewerWindow from '@/components/ImageViewerWindow.vue';
import VideoViewerWindow from '@/components/VideoViewerWindow.vue';
import WindowFrame from '@/components/WindowFrame.vue';
import Taskbar from '@/components/Taskbar.vue';

const desktop = useDesktopStore();
const notice = useNotificationStore();

onMounted(() => {
  if (!desktop.windows.length) desktop.openExplorer('');
});
</script>

<template>
  <main class="vue-desktop">
    <div class="preview-ribbon">Vue Preview <a :href="runtimeConfig.legacyUrl">現行版へ戻る</a></div>
    <button class="desktop-icon" type="button" @dblclick="desktop.openExplorer('')">
      <span>🗂️</span>
      <b>画像ライブラリ</b>
    </button>
    <a class="desktop-icon legacy-icon" :href="runtimeConfig.legacyUrl">
      <span>↩️</span>
      <b>現行版</b>
    </a>

    <WindowFrame
      v-for="win in desktop.windows"
      :key="win.id"
      :win="win"
      :icon="win.kind === 'explorer' ? '🗂️' : win.kind === 'video' ? '🎥' : '🖼️'"
    >
      <ExplorerWindow v-if="win.kind === 'explorer'" :win="win" />
      <ImageViewerWindow v-else-if="win.kind === 'image'" :win="win" />
      <VideoViewerWindow v-else :win="win" />
    </WindowFrame>

    <Transition name="toast">
      <div v-if="notice.message" class="xp-toast" :class="{error: notice.error}" role="status" @click="notice.clear">{{ notice.message }}</div>
    </Transition>
    <Taskbar />
  </main>
</template>
