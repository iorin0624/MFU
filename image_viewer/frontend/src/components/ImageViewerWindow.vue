<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { imageViewerApi } from '@/api/client';
import { runtimeConfig } from '@/config';
import { useDesktopStore } from '@/stores/desktop';
import { useNotificationStore } from '@/stores/notifications';
import type { DesktopWindow, MediaItem } from '@/types';
import { writeImageToClipboard } from '@/utils/imageClipboard';
import {
  DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS,
  loadWheelNavigationCooldown,
  saveWheelNavigationCooldown,
  VIEWER_SETTINGS_CHANGED_EVENT,
} from '@/utils/viewerSettings';
import XpDialog from './XpDialog.vue';
import CropDialog from './CropDialog.vue';

const props = defineProps<{ win: DesktopWindow }>();
const desktop = useDesktopStore();
const notice = useNotificationStore();
const fit = ref(true);
const zoom = ref(100);
const rotation = ref(0);
const moving = ref(false);
const settingsOpen = ref(false);
const cropOpen = ref(false);
const cropBusy = ref(false);
const hasOriginal = ref(false);
const wheelNavigationCooldownMs = ref(loadWheelNavigationCooldown());
const wheelNavigationCooldownDraft = ref(wheelNavigationCooldownMs.value);
let wheelNavigationAllowedAt = 0;
let versionTimer = 0;
let refreshingSequence = false;
const copying = ref(false);
const transferBusy = ref(false);
const menuOpen = ref(false);
const menuId = `image-viewer-transfer-${props.win.id}`;
const menuTrigger = ref<HTMLButtonElement>();
const menuPanel = ref<HTMLElement>();
const menuPosition = ref({ left: 0, top: 0 });

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
const canCrop = computed(() => !item.value.readOnly && /\.(jpe?g|png|webp)$/i.test(item.value.name));
watch(() => item.value.path, async (path) => {
  hasOriginal.value = false;
  if (!path || !canCrop.value) return;
  try {
    hasOriginal.value = (await imageViewerApi.cropStatus(path)).hasOriginal;
  } catch { /* Restoration status can be refreshed after the next edit. */ }
}, { immediate: true });

function downloadCurrent() {
  closeMenu(true);
  const url = new URL(item.value.url, window.location.origin);
  url.searchParams.set('download', '1');
  const link = document.createElement('a');
  link.href = `${url.pathname}${url.search}`;
  link.download = item.value.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function copyCurrent() {
  if (copying.value) return;
  copying.value = true;
  try {
    await writeImageToClipboard(item.value.url);
    notice.show('画像をクリップボードにコピーしました。');
  } catch (error) {
    notice.show(error instanceof Error ? error.message : '画像をコピーできませんでした。', true);
  } finally {
    copying.value = false;
  }
}

async function transferCurrent() {
  if (!runtimeConfig.isAdmin || transferBusy.value) return;
  const { path, name } = item.value;
  closeMenu(true);
  transferBusy.value = true;
  try {
    const payload = await imageViewerApi.transferSmb([path]);
    const result = payload.results[0];
    if (!result?.ok) throw new Error(result?.error || '転送結果を確認できませんでした。');
    notice.show(`ラズパイへ転送しました：${result.name || name}`);
  } catch (error) {
    notice.show(`転送に失敗しました：${error instanceof Error ? error.message : name}`, true);
  } finally {
    transferBusy.value = false;
  }
}

async function toggleMenu() {
  if (menuOpen.value) { closeMenu(); return; }
  menuOpen.value = true;
  await nextTick();
  const panel = menuPanel.value;
  const anchor = menuTrigger.value?.getBoundingClientRect();
  if (!anchor || !panel) return;
  menuPosition.value = {
    left: Math.max(8, Math.min(anchor.left, window.innerWidth - panel.offsetWidth - 8)),
    top: anchor.bottom + panel.offsetHeight + 8 <= window.innerHeight
      ? anchor.bottom + 2 : Math.max(8, anchor.top - panel.offsetHeight - 2),
  };
  panel.querySelector<HTMLButtonElement>('button')?.focus({ preventScroll: true });
}

function closeMenu(restoreFocus = false) {
  menuOpen.value = false;
  if (restoreFocus) menuTrigger.value?.focus({ preventScroll: true });
}

function dismissMenuOnPointer(event: PointerEvent) {
  if (!menuOpen.value || !(event.target instanceof Node)) return;
  if (!menuPanel.value?.contains(event.target) && !menuTrigger.value?.contains(event.target)) closeMenu();
}

function dismissMenuOnScroll(event: Event) {
  if (!menuOpen.value) return;
  if (!(event.target instanceof Node) || !menuPanel.value?.contains(event.target)) closeMenu();
}

function dismissMenuOnResize() { closeMenu(); }

function menuKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); closeMenu(true); return; }
  if (event.key === 'Tab') { closeMenu(true); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const entries = Array.from(menuPanel.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []);
  if (!entries.length) return;
  const index = entries.findIndex((button) => button === document.activeElement);
  const target = event.key === 'Home' ? 0 : event.key === 'End' ? entries.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : entries.length - 1)) % entries.length;
  entries[target]?.focus({ preventScroll: true });
}

function replaceCurrent(entry: MediaItem) {
  props.win.media = entry;
  props.win.sequence = sequence.value.map((previous) => previous.path === entry.path ? entry : previous);
  props.win.title = entry.name;
  hasOriginal.value = true;
}

async function saveCrop(rectangle: {x: number; y: number; width: number; height: number}) {
  if (cropBusy.value || !canCrop.value) return;
  cropBusy.value = true;
  try {
    const result = await imageViewerApi.crop(item.value.path, rectangle);
    replaceCurrent(result.entry);
    cropOpen.value = false;
    notice.show('画像を切り抜きました。元画像はいつでも復元できます。');
  } catch (error) {
    notice.show(error instanceof Error ? error.message : '切り抜きに失敗しました。', true);
  } finally {
    cropBusy.value = false;
  }
}

async function restoreOriginal() {
  if (cropBusy.value || !canCrop.value || !hasOriginal.value) return;
  cropBusy.value = true;
  try {
    const result = await imageViewerApi.restoreOriginal(item.value.path);
    replaceCurrent(result.entry);
    notice.show('元画像に戻しました。');
  } catch (error) {
    notice.show(error instanceof Error ? error.message : '元画像を復元できませんでした。', true);
  } finally {
    cropBusy.value = false;
  }
}

function show(entry?: MediaItem) {
  if (!entry) return;
  closeMenu();
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

function nearestImage(entries: MediaItem[], start: number) {
  if (!entries.length) return undefined;
  const safeStart = Math.min(entries.length - 1, Math.max(0, start));
  if (entries[safeStart]?.mediaType === 'image') return entries[safeStart];
  for (let distance = 1; distance < entries.length; distance += 1) {
    const after = entries[safeStart + distance];
    if (after?.mediaType === 'image') return after;
    const before = entries[safeStart - distance];
    if (before?.mediaType === 'image') return before;
  }
  return undefined;
}

async function refreshSequenceIfChanged(force = false) {
  const context = props.win.sequenceContext;
  if (!context || document.hidden || moving.value || refreshingSequence) return;
  refreshingSequence = true;
  try {
    const revision = await imageViewerApi.version(context.folder);
    if (!force && context.version && revision.version === context.version) return;

    const oldLocalIndex = Math.max(0, currentIndex.value);
    const oldGlobalIndex = Math.max(0, context.offset + oldLocalIndex);
    const currentPath = item.value.path;
    const payload = await imageViewerApi.list(
      context.folder, context.sort, 1, 1000, oldGlobalIndex,
      context.groupBy || 'none', context.groupUnit || 'day',
    );
    const entries = payload.images || [];
    const offset = Number(payload.pagination?.offset || 0);
    const total = Number(payload.pagination?.total ?? entries.length);
    const retained = entries.find((entry) => entry.path === currentPath);

    props.win.sequence = entries;
    props.win.sequenceContext = {
      ...context,
      offset,
      total,
      version: String(payload.version || revision.version || ''),
    };

    if (retained) {
      // Keep the same image selected while refreshing its metadata and URL.
      props.win.media = retained;
      props.win.title = retained.name;
      return;
    }

    // The displayed file was removed. Continue from the closest remaining
    // position instead of leaving the viewer on a stale, unnavigable item.
    const replacement = nearestImage(entries, oldGlobalIndex - offset);
    if (replacement) show(replacement);
  } catch {
    // A transient network failure is retried by the next poll. Avoid covering
    // the image with repeated error notifications while the viewer is open.
  } finally {
    refreshingSequence = false;
  }
}

function visibilityChanged() {
  if (!document.hidden) void refreshSequenceIfChanged();
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
  if (cropOpen.value || settingsOpen.value || menuOpen.value) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c') {
    const target = event.target as HTMLElement | null;
    if (target?.matches('input, textarea, [contenteditable="true"]') || window.getSelection()?.toString()) return;
    event.preventDefault();
    void copyCurrent();
    return;
  }
  if (event.key === 'ArrowLeft') move(-1);
  if (event.key === 'ArrowRight') move(1);
  if (event.key === '+' || event.key === '=') setZoom(zoom.value + 10);
  if (event.key === '-') setZoom(zoom.value - 10);
  if (event.key === '0') fit.value = true;
  if (event.key.toLowerCase() === 'r') rotation.value = (rotation.value + 90) % 360;
}

onMounted(() => {
  document.addEventListener('keydown', keydown);
  document.addEventListener('pointerdown', dismissMenuOnPointer);
  document.addEventListener('scroll', dismissMenuOnScroll, true);
  window.addEventListener('resize', dismissMenuOnResize);
  document.addEventListener('visibilitychange', visibilityChanged);
  window.addEventListener(VIEWER_SETTINGS_CHANGED_EVENT, syncSettings);
  versionTimer = window.setInterval(() => { void refreshSequenceIfChanged(); }, 5000);
});
onBeforeUnmount(() => {
  document.removeEventListener('keydown', keydown);
  document.removeEventListener('pointerdown', dismissMenuOnPointer);
  document.removeEventListener('scroll', dismissMenuOnScroll, true);
  window.removeEventListener('resize', dismissMenuOnResize);
  document.removeEventListener('visibilitychange', visibilityChanged);
  window.removeEventListener(VIEWER_SETTINGS_CHANGED_EVENT, syncSettings);
  window.clearInterval(versionTimer);
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
      <button type="button" class="viewer-compact-action" @click="rotation = (rotation + 90) % 360">回転</button>
      <button type="button" class="viewer-compact-action" aria-label="コピー（📎）" title="画像をクリップボードにコピー（Ctrl+C）" :disabled="copying" @click="copyCurrent">📎</button>
      <button ref="menuTrigger" type="button" class="viewer-compact-action" aria-label="DL/転送" title="DL/転送" aria-haspopup="menu" :aria-controls="menuId" :aria-expanded="menuOpen" :class="{pressed: menuOpen}" @click="toggleMenu">DL ▾</button>
      <button v-if="!item.readOnly" type="button" :disabled="!canCrop || cropBusy" @click="cropOpen = true">切り抜き</button>
      <button v-if="!item.readOnly" type="button" :disabled="!hasOriginal || cropBusy" @click="restoreOriginal">元画像に戻す</button>
      <button type="button" class="viewer-compact-action" title="画像ビューアー設定" @click="openSettings">設定</button>
      <span class="viewer-counter">{{ globalIndex + 1 }} / {{ counterTotal }}</span>
    </nav>
    <Teleport to="body">
      <div v-if="menuOpen" :id="menuId" ref="menuPanel" class="toolbar-dropdown" role="menu" aria-label="DL/転送"
        :style="{left: `${menuPosition.left}px`, top: `${menuPosition.top}px`}" @pointerdown.stop @keydown.stop="menuKeydown">
        <button type="button" role="menuitem" @click="downloadCurrent">画像DL</button>
        <button v-if="runtimeConfig.isAdmin" type="button" role="menuitem" :disabled="transferBusy" @click="transferCurrent">{{ transferBusy ? 'ラズパイへ転送中…' : 'ラズパイに転送' }}</button>
      </div>
    </Teleport>
    <div class="image-stage" @wheel="wheel">
      <img :src="item.url" :alt="item.name" :class="{fit}" :style="imageStyle" draggable="false">
    </div>
    <footer class="viewer-status">{{ item.name }}</footer>
    <CropDialog v-if="cropOpen" :src="item.url" :name="item.name" :busy="cropBusy" @close="cropOpen = false" @save="saveCrop" />
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
