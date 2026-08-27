<script setup lang="ts">
import { onMounted } from 'vue';
import { useDesktopStore } from '@/stores/desktop';
import { useNotificationStore } from '@/stores/notifications';
import ExplorerWindow from '@/components/ExplorerWindow.vue';
import ImageViewerWindow from '@/components/ImageViewerWindow.vue';
import VideoViewerWindow from '@/components/VideoViewerWindow.vue';
import ImageDownloaderWindow from '@/components/ImageDownloaderWindow.vue';
import VideoDownloaderWindow from '@/components/VideoDownloaderWindow.vue';
import InstagramAuthWindow from '@/components/InstagramAuthWindow.vue';
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
    <button class="desktop-icon" type="button" @dblclick="desktop.openExplorer('')">
      <span>🗂️</span>
      <b>画像ライブラリ</b>
    </button>
    <button class="desktop-icon image-download-icon" type="button" @dblclick="desktop.openUtility('image-downloader')">
      <span class="desktop-letter-icon">IG</span>
      <b>Instagram/X<br>画像取得</b>
    </button>
    <button class="desktop-icon video-download-icon" type="button" @dblclick="desktop.openUtility('video-downloader')">
      <span class="desktop-letter-icon">VD</span>
      <b>動画DL</b>
    </button>

    <WindowFrame
      v-for="win in desktop.windows"
      :key="win.id"
      :win="win"
      :icon="win.kind === 'explorer' ? '🗂️' : win.kind === 'video' ? '🎥' : win.kind === 'image' ? '🖼️' : win.kind === 'video-downloader' ? 'VD' : 'IG'"
    >
      <ExplorerWindow v-if="win.kind === 'explorer'" :win="win" />
      <ImageViewerWindow v-else-if="win.kind === 'image'" :win="win" />
      <VideoViewerWindow v-else-if="win.kind === 'video'" :win="win" />
      <ImageDownloaderWindow v-else-if="win.kind === 'image-downloader'" />
      <VideoDownloaderWindow v-else-if="win.kind === 'video-downloader'" />
      <InstagramAuthWindow v-else-if="win.kind === 'instagram-auth'" />
    </WindowFrame>

    <Transition name="toast">
      <div v-if="notice.message" class="xp-toast" :class="{error: notice.error}" role="status" @click="notice.clear">{{ notice.message }}</div>
    </Transition>
    <Taskbar />
  </main>
</template>
