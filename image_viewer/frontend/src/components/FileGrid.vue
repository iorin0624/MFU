<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { MediaItem, ViewSize } from '@/types';

const props = defineProps<{
  items: MediaItem[];
  selected: string[];
  viewSize: ViewSize;
  loading: boolean;
  hasMore: boolean;
  appendMode: boolean;
  total: number;
  offset: number;
}>();
const emit = defineEmits<{
  select: [event: MouseEvent, item: MediaItem];
  open: [item: MediaItem];
  context: [event: MouseEvent, item: MediaItem];
  upload: [files: File[]];
  marquee: [paths: string[], additive: boolean];
  keyboard: [item: MediaItem, options: { extend: boolean; toggle: boolean; focusOnly: boolean }];
  loadCenter: [index: number];
}>();

const grid = ref<HTMLElement>();
const marquee = ref({ visible: false, left: 0, top: 0, width: 0, height: 0 });
const columns = ref(1);
const rowStride = ref(220);
const columnWidth = ref(160);
const pendingKeyboardTarget = ref<{
  index: number;
  options: { extend: boolean; toggle: boolean; focusOnly: boolean };
} | null>(null);
let resizeObserver: ResizeObserver | null = null;
let scrollFrame = 0;
let suppressScrollUntil = 0;
let lastRequestedIndex = -1;

const loadedEnd = computed(() => Math.min(props.total, props.offset + props.items.length));
const totalRows = computed(() => Math.ceil(Math.max(0, props.total) / columns.value));
const canvasHeight = computed(() => Math.max(0, totalRows.value * rowStride.value - 12));

function itemStyle(localIndex: number) {
  const globalIndex = props.offset + localIndex;
  const column = globalIndex % columns.value;
  const row = Math.floor(globalIndex / columns.value);
  return {
    left: `${column * (columnWidth.value + 12)}px`,
    top: `${row * rowStride.value}px`,
    width: `${columnWidth.value}px`,
  };
}

function measureGrid() {
  const root = grid.value;
  if (!root) return;
  const style = getComputedStyle(root);
  const wantedWidth = Number.parseFloat(style.getPropertyValue('--card-width')) || 160;
  const usableWidth = Math.max(wantedWidth, root.clientWidth - 24);
  columns.value = Math.max(1, Math.floor((usableWidth + 12) / (wantedWidth + 12)));
  columnWidth.value = Math.max(wantedWidth, (usableWidth - ((columns.value - 1) * 12)) / columns.value);
  const card = root.querySelector<HTMLElement>('.file-card');
  const thumb = Number.parseFloat(style.getPropertyValue('--thumb-height')) || 170;
  rowStride.value = Math.max(80, card?.getBoundingClientRect().height || thumb + 48) + 12;
}

function reportScrollCenter() {
  const root = grid.value;
  if (!root || props.loading || props.total <= props.items.length || performance.now() < suppressScrollUntil) return;
  // The spacer rows represent the complete folder, so derive the visible global
  // index from the actual row geometry.  A scroll-height ratio is unstable when
  // a different 1000-item window is mounted and used to make adjacent windows
  // repeatedly replace each other.
  const visibleRow = Math.max(0, Math.floor((root.scrollTop + (root.clientHeight / 2)) / Math.max(1, rowStride.value)));
  const center = Math.max(0, Math.min(props.total - 1, (visibleRow * columns.value) + Math.floor(columns.value / 2)));
  // Start moving the retained window well before the viewport can reach its
  // edge.  The next/previous cached windows are normally already available,
  // so the user never scrolls into an unloaded white band.
  const edgeMargin = Math.min(450, Math.max(120, Math.floor(props.items.length * 0.35)));
  if (center >= props.offset + edgeMargin && center < loadedEnd.value - edgeMargin) {
    lastRequestedIndex = -1;
    return;
  }
  if (center === lastRequestedIndex) return;
  lastRequestedIndex = center;
  emit('loadCenter', center);
}

function onScroll() {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    reportScrollCenter();
  });
}

function dragStart(event: DragEvent, item: MediaItem) {
  const paths = props.selected.includes(item.path) ? props.selected : [item.path];
  event.dataTransfer?.setData('application/x-mfu-paths', JSON.stringify(paths));
  event.dataTransfer?.setData('text/plain', paths.join('\n'));
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'copyMove';
}

function dropFiles(event: DragEvent) {
  event.preventDefault();
  const files = Array.from(event.dataTransfer?.files || []);
  if (files.length) emit('upload', files);
}

function keyboardTargetIndex(key: string, localIndex: number): number {
  const current = Math.max(0, Math.min(props.total - 1, props.offset + localIndex));
  const columnCount = Math.max(1, columns.value);
  const pageRows = Math.max(1, Math.floor((grid.value?.clientHeight || rowStride.value) / rowStride.value));
  const movement: Record<string, number> = {
    ArrowLeft: -1,
    ArrowRight: 1,
    ArrowUp: -columnCount,
    ArrowDown: columnCount,
    PageUp: -(pageRows * columnCount),
    PageDown: pageRows * columnCount,
  };
  if (key === 'Home') return 0;
  if (key === 'End') return Math.max(0, props.total - 1);
  return Math.max(0, Math.min(props.total - 1, current + (movement[key] || 0)));
}

async function focusKeyboardTarget(
  globalIndex: number,
  options: { extend: boolean; toggle: boolean; focusOnly: boolean },
) {
  const localIndex = globalIndex - props.offset;
  const targetItem = props.items[localIndex];
  if (!targetItem) {
    pendingKeyboardTarget.value = { index: globalIndex, options };
    emit('loadCenter', globalIndex);
    return;
  }
  pendingKeyboardTarget.value = null;
  await nextTick();
  const targetCard = Array.from(grid.value?.querySelectorAll<HTMLElement>('.file-card') || [])[localIndex];
  if (!targetCard) return;
  targetCard.focus();
  targetCard.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  emit('keyboard', targetItem, options);
}

function cardKeydown(event: KeyboardEvent, item: MediaItem, index: number) {
  if (event.key === 'Enter') {
    event.preventDefault();
    emit('open', item);
    return;
  }
  if (event.key === ' ') {
    event.preventDefault();
    emit('keyboard', item, {
      extend: event.shiftKey,
      toggle: event.ctrlKey || event.metaKey,
      focusOnly: false,
    });
    return;
  }
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) return;
  event.preventDefault();
  const targetIndex = keyboardTargetIndex(event.key, index);
  void focusKeyboardTarget(targetIndex, {
    extend: event.shiftKey,
    toggle: false,
    focusOnly: event.ctrlKey || event.metaKey,
  });
}

function navigate(
  key: string,
  currentPath: string,
  options: { extend: boolean; toggle: boolean; focusOnly: boolean },
) {
  const localIndex = Math.max(0, props.items.findIndex((item) => item.path === currentPath));
  return focusKeyboardTarget(keyboardTargetIndex(key, localIndex), options);
}

defineExpose({ navigate });

function startMarquee(event: PointerEvent) {
  if (event.button !== 0 || (event.target as HTMLElement).closest('.file-card')) return;
  const root = grid.value;
  if (!root) return;
  const rect = root.getBoundingClientRect();
  const startX = event.clientX - rect.left + root.scrollLeft - 12;
  const startY = event.clientY - rect.top + root.scrollTop - 12;
  marquee.value = { visible: true, left: startX, top: startY, width: 0, height: 0 };
  const move = (next: PointerEvent) => {
    const x = next.clientX - rect.left + root.scrollLeft - 12;
    const y = next.clientY - rect.top + root.scrollTop - 12;
    marquee.value = {
      visible: true,
      left: Math.min(startX, x), top: Math.min(startY, y),
      width: Math.abs(x - startX), height: Math.abs(y - startY),
    };
  };
  const done = (next: PointerEvent) => {
    window.removeEventListener('pointermove', move);
    const box = marquee.value;
    const paths = Array.from(root.querySelectorAll<HTMLElement>('.file-card')).filter((element) => {
      const itemRect = element.getBoundingClientRect();
      const left = itemRect.left - rect.left + root.scrollLeft - 12;
      const top = itemRect.top - rect.top + root.scrollTop - 12;
      return left < box.left + box.width && left + itemRect.width > box.left
        && top < box.top + box.height && top + itemRect.height > box.top;
    }).map((element) => element.dataset.path || '').filter(Boolean);
    marquee.value.visible = false;
    emit('marquee', paths, next.ctrlKey || next.metaKey);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', done, { once: true });
}

onMounted(async () => {
  await nextTick();
  measureGrid();
  resizeObserver = new ResizeObserver(measureGrid);
  if (grid.value) resizeObserver.observe(grid.value);
});
watch(() => [props.items, props.viewSize], async () => {
  await nextTick();
  measureGrid();
}, { deep: false });
watch(() => [props.offset, props.items], async () => {
  // Replacing a virtual window can itself emit a scroll event.  Do not treat
  // that synthetic/layout event as movement back toward the previous window.
  suppressScrollUntil = performance.now() + 180;
  lastRequestedIndex = -1;
  const pending = pendingKeyboardTarget.value;
  if (!pending) return;
  const localIndex = pending.index - props.offset;
  if (localIndex < 0 || localIndex >= props.items.length) return;
  await focusKeyboardTarget(pending.index, pending.options);
}, { deep: false });
onBeforeUnmount(() => {
  resizeObserver?.disconnect();
  if (scrollFrame) cancelAnimationFrame(scrollFrame);
});
</script>

<template>
  <div
    ref="grid"
    class="file-grid"
    :class="`view-${viewSize}`"
    @pointerdown="startMarquee"
    @dragover.prevent
    @drop="dropFiles"
    @scroll.passive="onScroll"
  >
    <div class="virtual-canvas" :style="{ height: `${canvasHeight}px` }">
      <button
        v-for="(item, index) in items"
        :key="item.path"
        type="button"
        class="file-card"
        :class="{ selected: selected.includes(item.path), target: appendMode }"
        :style="itemStyle(index)"
        :data-path="item.path"
        :title="item.path"
        :aria-selected="selected.includes(item.path)"
        draggable="true"
        @click="emit('select', $event, item)"
        @dblclick="emit('open', item)"
        @contextmenu.prevent="emit('context', $event, item)"
        @dragstart="dragStart($event, item)"
        @keydown="cardKeydown($event, item, index)"
      >
        <span
          class="file-thumb"
          :class="{ video: item.mediaType === 'video' && !item.thumbUrl }"
          :style="item.thumbUrl || item.mediaType === 'image' ? { backgroundImage: `url(&quot;${item.thumbUrl || item.url}&quot;)` } : {}"
        ><span v-if="item.mediaType === 'video'" class="play-mark">▶</span></span>
        <span class="file-name">{{ item.name }}</span>
      </button>
      <p v-if="!items.length && !loading" class="empty-folder">このフォルダーに画像・動画がありません。ここへドロップできます。</p>
      <span
        v-if="marquee.visible"
        class="selection-marquee"
        :style="{ left: `${marquee.left}px`, top: `${marquee.top}px`, width: `${marquee.width}px`, height: `${marquee.height}px` }"
      ></span>
    </div>
  </div>
</template>
