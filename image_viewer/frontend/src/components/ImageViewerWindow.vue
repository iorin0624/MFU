<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { imageViewerApi } from '@/api/client';
import { useDesktopStore } from '@/stores/desktop';
import { useNotificationStore } from '@/stores/notifications';
import type { DesktopWindow, MediaItem } from '@/types';
import {
  DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS,
  loadWheelNavigationCooldown,
  saveWheelNavigationCooldown,
  VIEWER_SETTINGS_CHANGED_EVENT,
} from '@/utils/viewerSettings';
import XpDialog from './XpDialog.vue';

const props = defineProps<{ win: DesktopWindow }>();
const desktop = useDesktopStore();
const notice = useNotificationStore();
const fit = ref(true);
const zoom = ref(100);
const rotation = ref(0);
const moving = ref(false);
const settingsOpen = ref(false);
const wheelNavigationCooldownMs = ref(loadWheelNavigationCooldown());
const wheelNavigationCooldownDraft = ref(wheelNavigationCooldownMs.value);
let wheelNavigationAllowedAt = 0;

const item = computed(() => props.win.media!);
const sequence = computed(() => props.win.sequence || []);
const currentIndex = computed(() => sequence.value.findIndex((entry) => entry.path === item.value.path));
const globalIndex = computed(() => {
  const context = props.win.sequenceContext;
  return context && currentIndex.value >= 0 ? context.offset + currentIndex.value : currentIndex.value;
});
const counterTotal = computed(() => props.win.sequenceContext?.total || sequence.value.length);
const imageStyle = computed(() => ({
  width: fit.value ? undefined : `${zoom.value}%`,
  height: fit.value ? undefined : 'auto',
  transform: `rotate(${rotation.value}deg)`,
}));

function show(entry?: MediaItem) {
  if (!entry) return;
  props.win.media = entry;
  props.win.title = entry.name;
  fit.value = true;
  zoom.value = 100;
  rotation.value = 0;
}

function adjacentImage(entries: MediaItem[], start: number, direction: -1 | 1) {
  for (let index = start; index >= 0 && index < entries.length; index += direction) {
    if (entries[index]?.mediaType === 'image') return entries[index];
  }
  return undefined;
}

async function loadAdjacentWindow(direction: -1 | 1) {
  const context = props.win.sequenceContext;
  if (!context || moving.value) return;
  let currentGlobal = globalIndex.value;
  let wanted = currentGlobal + direction;
  if (wanted < 0 || wanted >= context.total) return;
  moving.value = true;
  try {
    for (let attempt = 0; attempt < 4 && wanted >= 0 && wanted < context.total; attempt += 1) {
      const payload = await imageViewerApi.list(
        context.folder, context.sort, 1, 1000, wanted,
        context.groupBy || 'none', context.groupUnit || 'day',
      );
      const entries = payload.images || [];
      const offset = Number(payload.pagination?.offset || 0);
      const candidate = direction > 0
        ? entries.find((entry, index) => offset + index > currentGlobal && entry.mediaType === 'image')
        : [...entries].reverse().find((entry, reverseIndex) => {
          const index = entries.length - 1 - reverseIndex;
          return offset + index < currentGlobal && entry.mediaType === 'image';
        });
      if (candidate) {
        props.win.sequence = entries;
        props.win.sequenceContext = {
          ...context,
          offset,
          total: Number(payload.pagination?.total ?? context.total),
        };
        show(candidate);
        return;
      }
      if (!entries.length) return;
      wanted = direction > 0 ? offset + entries.length : offset - 1;
      currentGlobal = direction > 0 ? offset + entries.length - 1 : offset;
    }
  } catch (error) {
    notice.show(error instanceof Error ? error.message : '次の画像を読み込めませんでした。', true);
  } finally {
    moving.value = false;
  }
}

function move(delta: number) {
  if (!sequence.value.length || moving.value) return;
  const direction: -1 | 1 = delta < 0 ? -1 : 1;
  const next = adjacentImage(sequence.value, currentIndex.value + direction, direction);
  if (next) {
    show(next);
    return;
  }
  void loadAdjacentWindow(direction);
}

function setZoom(next: number) {
  fit.value = false;
  zoom.value = Math.min(400, Math.max(25, next));
}

function openSettings() {
  wheelNavigationCooldownDraft.value = wheelNavigationCooldownMs.value;
  settingsOpen.value = true;
}

function saveSettings() {
  wheelNavigationCooldownMs.value = saveWheelNavigationCooldown(wheelNavigationCooldownDraft.value);
  wheelNavigationAllowedAt = 0;
  settingsOpen.value = false;
  window.dispatchEvent(new CustomEvent(VIEWER_SETTINGS_CHANGED_EVENT));
}

function resetSettings() {
  wheelNavigationCooldownDraft.value = DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS;
}

function syncSettings() {
  wheelNavigationCooldownMs.value = loadWheelNavigationCooldown();
  wheelNavigationAllowedAt = 0;
}

function wheel(event: WheelEvent) {
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    setZoom(zoom.value + (event.deltaY < 0 ? 10 : -10));
    return;
  }
  event.preventDefault();
  const now = performance.now();
  if (now < wheelNavigationAllowedAt) return;
  wheelNavigationAllowedAt = now + wheelNavigationCooldownMs.value;
  move(event.deltaY > 0 ? 1 : -1);
}

function keydown(event: KeyboardEvent) {
  if (desktop.activeId !== props.win.id) return;
  if (event.key === 'ArrowLeft') move(-1);
  if (event.key === 'ArrowRight') move(1);
  if (event.key === '+' || event.key === '=') setZoom(zoom.value + 10);
  if (event.key === '-') setZoom(zoom.value - 10);
  if (event.key === '0') fit.value = true;
  if (event.key.toLowerCase() === 'r') rotation.value = (rotation.value + 90) % 360;
}

onMounted(() => {
  document.addEventListener('keydown', keydown);
  window.addEventListener(VIEWER_SETTINGS_CHANGED_EVENT, syncSettings);
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', keydown);
  window.removeEventListener(VIEWER_SETTINGS_CHANGED_EVENT, syncSettings);
});
</script>

<template>
  <div class="viewer-layout image-viewer">
    <nav class="viewer-toolbar">
      <button type="button" title="前の画像" :disabled="moving" @click="move(-1)">前</button>
      <button type="button" title="次の画像" :disabled="moving" @click="move(1)">次</button>
      <button type="button" @click="fit = true">全体表示</button>
      <button type="button" @click="setZoom(100)">等倍</button>
      <button type="button" @click="setZoom(zoom - 10)">−</button>
      <span class="zoom-label">{{ fit ? '全体' : `${zoom}%` }}</span>
      <button type="button" @click="setZoom(zoom + 10)">+</button>
      <button type="button" @click="rotation = (rotation + 90) % 360">回転</button>
      <button type="button" title="画像ビューアー設定" @click="openSettings">設定</button>
      <span class="viewer-counter">{{ globalIndex + 1 }} / {{ counterTotal }}</span>
    </nav>
    <div class="image-stage" @wheel="wheel">
      <img :src="item.url" :alt="item.name" :class="{fit}" :style="imageStyle" draggable="false">
    </div>
    <footer class="viewer-status">{{ item.name }}</footer>
    <XpDialog v-if="settingsOpen" title="画像ビューアー設定" @close="settingsOpen = false">
      <form class="xp-form" @submit.prevent="saveSettings">
        <label>ホイール画像送り間隔（ミリ秒）
          <input
            v-model.number="wheelNavigationCooldownDraft"
            type="number"
            min="0"
            max="2000"
            step="50"
            inputmode="numeric"
            autofocus
          >
        </label>
        <p class="dialog-hint">0～2000msで設定できます。標準は300msです。0にすると待ち時間なしになります。</p>
        <div class="xp-dialog-actions">
          <button type="submit">保存</button>
          <button type="button" @click="resetSettings">標準に戻す</button>
          <button type="button" @click="settingsOpen = false">キャンセル</button>
        </div>
      </form>
    </XpDialog>
  </div>
</template>
