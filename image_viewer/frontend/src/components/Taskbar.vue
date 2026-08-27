<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { useDesktopStore } from '@/stores/desktop';

const desktop = useDesktopStore();
const now = ref(new Date());
const fullscreen = ref(Boolean(document.fullscreenElement));
let timer = 0;

function toggleWindow(id: string) {
  if (desktop.activeId === id) desktop.minimize(id);
  else desktop.activate(id);
}

async function toggleFullscreen() {
  if (document.fullscreenElement) await document.exitFullscreen();
  else await document.documentElement.requestFullscreen();
}

function fullscreenChanged() { fullscreen.value = Boolean(document.fullscreenElement); }

function windowIcon(kind: string) {
  if (kind === 'explorer') return '🗂️';
  if (kind === 'video') return '🎥';
  if (kind === 'image') return '🖼️';
  if (kind === 'video-downloader') return 'VD';
  return 'IG';
}

onMounted(() => {
  timer = window.setInterval(() => { now.value = new Date(); }, 1000);
  document.addEventListener('fullscreenchange', fullscreenChanged);
});
onBeforeUnmount(() => {
  window.clearInterval(timer);
  document.removeEventListener('fullscreenchange', fullscreenChanged);
});
</script>

<template>
  <footer class="xp-taskbar">
    <button class="start-button" type="button" @click="desktop.openExplorer('')"><span class="start-logo">◆</span> スタート</button>
    <div class="taskbar-launchers">
      <button type="button" title="Instagram / Threads / X 画像取得" @click="desktop.openUtility('image-downloader')">IG</button>
      <button type="button" title="Instagram / Threads / X 動画取得" @click="desktop.openUtility('video-downloader')">VD</button>
    </div>
    <div class="task-buttons">
      <button
        v-for="win in desktop.windows"
        :key="win.id"
        type="button"
        :class="{active: desktop.activeId === win.id && !win.minimized}"
        @click="toggleWindow(win.id)"
      >{{ windowIcon(win.kind) }} {{ win.title }}</button>
    </div>
    <time>{{ now.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' }) }}</time>
    <button class="taskbar-fs" type="button" :title="fullscreen ? 'フルスクリーンを終了' : 'フルスクリーン'" @click="toggleFullscreen">{{ fullscreen ? '戻る' : 'FS' }}</button>
  </footer>
</template>
