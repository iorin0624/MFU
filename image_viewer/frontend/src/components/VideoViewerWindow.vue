<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useDesktopStore } from '@/stores/desktop';
import type { DesktopWindow, MediaItem } from '@/types';

const props = defineProps<{ win: DesktopWindow }>();
const desktop = useDesktopStore();
const video = ref<HTMLVideoElement>();
const playing = ref(false);
const currentTime = ref(0);
const duration = ref(0);
const volume = ref(Number(localStorage.getItem('mfu.imageViewer.vue.volume') || 0.8));
const muted = ref(false);

const item = computed(() => props.win.media!);
const sequence = computed(() => props.win.sequence || []);
const currentIndex = computed(() => sequence.value.findIndex((entry) => entry.path === item.value.path));

function formatTime(seconds: number) {
  if (!Number.isFinite(seconds)) return '0:00';
  const minute = Math.floor(seconds / 60);
  return `${minute}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
}

function sync() {
  if (!video.value) return;
  playing.value = !video.value.paused;
  currentTime.value = video.value.currentTime;
  duration.value = video.value.duration || 0;
}

function toggle() {
  if (!video.value) return;
  video.value.paused ? video.value.play() : video.value.pause();
}

function seek(seconds: number) {
  if (video.value) video.value.currentTime = Math.max(0, Math.min(video.value.duration || 0, video.value.currentTime + seconds));
}

function stop() {
  if (!video.value) return;
  video.value.pause();
  video.value.currentTime = 0;
  sync();
}

function toggleMute() {
  muted.value = !muted.value;
  if (video.value) video.value.muted = muted.value;
}

function fullscreen() {
  video.value?.requestFullscreen?.();
}

function seekTo(event: Event) {
  const value = Number((event.target as HTMLInputElement).value);
  if (video.value) video.value.currentTime = value;
}

function setVolume(event: Event) {
  volume.value = Number((event.target as HTMLInputElement).value);
  if (video.value) video.value.volume = volume.value;
  localStorage.setItem('mfu.imageViewer.vue.volume', String(volume.value));
}

function show(entry?: MediaItem) {
  if (!entry) return;
  props.win.media = entry;
  props.win.title = entry.name;
  currentTime.value = 0;
  duration.value = 0;
}

function move(delta: number) {
  if (!sequence.value.length) return;
  const next = (currentIndex.value + delta + sequence.value.length) % sequence.value.length;
  show(sequence.value[next]);
}

function keydown(event: KeyboardEvent) {
  if (desktop.activeId !== props.win.id) return;
  if (event.code === 'Space') { event.preventDefault(); toggle(); }
  if (event.key === 'ArrowLeft') seek(-10);
  if (event.key === 'ArrowRight') seek(10);
  if (event.key === 'PageUp') move(-1);
  if (event.key === 'PageDown') move(1);
}

onMounted(() => document.addEventListener('keydown', keydown));
onBeforeUnmount(() => document.removeEventListener('keydown', keydown));
</script>

<template>
  <div class="viewer-layout video-viewer mpc-player">
    <nav class="mpc-menu-bar" aria-label="メニュー">
      <button type="button">ファイル(F)</button><button type="button">表示(V)</button><button type="button" @click="toggle">再生(P)</button>
      <button type="button">操作(N)</button><button type="button">お気に入り(A)</button><button type="button">ヘルプ(H)</button>
    </nav>
    <div class="video-stage">
      <video
        ref="video"
        :key="item.path"
        :src="item.url"
        playsinline
        preload="metadata"
        @loadedmetadata="video && (video.volume = volume); sync()"
        @timeupdate="sync"
        @play="sync"
        @pause="sync"
        @ended="sync"
        @click="toggle"
      ></video>
    </div>
    <div class="mpc-seekbar">
      <input aria-label="再生位置" type="range" min="0" :max="duration || 0" step="0.05" :value="currentTime" @input="seekTo">
    </div>
    <div class="mpc-controls">
      <button type="button" title="再生/一時停止" @click="toggle">{{ playing ? '❚❚' : '▶' }}</button>
      <button type="button" title="停止" @click="stop">■</button>
      <span class="mpc-separator"></span>
      <button type="button" title="前のファイル" @click="move(-1)">|◀</button>
      <button type="button" title="30秒戻る" @click="seek(-30)">⏪30</button>
      <button type="button" title="10秒戻る" @click="seek(-10)">⏪10</button>
      <span class="mpc-seek-gap" aria-hidden="true"></span>
      <button type="button" title="10秒進む" @click="seek(10)">10⏩</button>
      <button type="button" title="30秒進む" @click="seek(30)">30⏩</button>
      <button type="button" title="次のファイル" @click="move(1)">▶|</button>
      <span class="mpc-separator"></span>
      <button type="button" title="全画面" @click="fullscreen">□</button>
      <span class="mpc-spacer"></span>
      <button type="button" title="ミュート" @click="toggleMute">{{ muted ? '🔇' : '🔊' }}</button>
      <input aria-label="音量" class="mpc-volume" type="range" min="0" max="1" step="0.05" :value="volume" @input="setVolume">
    </div>
    <footer class="mpc-statusbar"><span>{{ item.name }}</span><span>{{ formatTime(currentTime) }} / {{ formatTime(duration) }}</span><span>{{ currentIndex + 1 }} / {{ sequence.length }}</span></footer>
  </div>
</template>
