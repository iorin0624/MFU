<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import type { DateGroup, MediaItem, ViewSize } from '@/types';

const props = defineProps<{
  items: MediaItem[];
  selected: string[];
  viewSize: ViewSize;
  loading: boolean;
  hasMore: boolean;
  appendMode: boolean;
  total: number;
  offset: number;
  groups: DateGroup[];
  collapsedGroups: string[];
}>();
const emit = defineEmits<{
  select: [event: MouseEvent, item: MediaItem];
  open: [item: MediaItem];
  context: [event: MouseEvent, item: MediaItem];
  upload: [files: File[]];
  marquee: [paths: string[], additive: boolean];
  keyboard: [item: MediaItem, options: { extend: boolean; toggle: boolean; focusOnly: boolean }];
  loadCenter: [index: number];
  toggleGroup: [key: string];
}>();

const grid = ref<HTMLElement>();
const marquee = ref({ visible: false, left: 0, top: 0, width: 0, height: 0 });
const columns = ref(1);
const rowStride = ref(220);
const columnWidth = ref(160);
const groupHeaderHeight = 38;
const activeGroupKey = ref('');
const stickyGroupVisible = ref(false);
const pendingKeyboardTarget = ref<{
  index: number;
  options: { extend: boolean; toggle: boolean; focusOnly: boolean };
} | null>(null);
let resizeObserver: ResizeObserver | null = null;
let scrollFrame = 0;
let suppressScrollUntil = 0;
let lastRequestedIndex = -1;

const loadedEnd = computed(() => Math.min(props.total, props.offset + props.items.length));
const groupLayouts = computed(() => {
  if (!props.groups.length) return [];
  let top = 0;
  return props.groups.map((group) => {
    const collapsed = props.collapsedGroups.includes(group.key);
    const rows = collapsed ? 0 : Math.ceil(group.count / columns.value);
    const layout = {
      ...group, collapsed, top, itemsTop: top + groupHeaderHeight,
      bottom: top + groupHeaderHeight + (rows * rowStride.value),
    };
    top = layout.bottom;
    return layout;
  });
});
const totalRows = computed(() => Math.ceil(Math.max(0, props.total) / columns.value));
const canvasHeight = computed(() => Math.max(0,
  groupLayouts.value.length
    ? (groupLayouts.value.at(-1)?.bottom || 0)
    : (totalRows.value * rowStride.value - 12),
));

function layoutForIndex(globalIndex: number) {
  return groupLayouts.value.find((group) =>
    globalIndex >= group.start && globalIndex < group.start + group.count);
}

function itemStyle(localIndex: number) {
  const globalIndex = props.offset + localIndex;
  const group = layoutForIndex(globalIndex);
  if (group?.collapsed) return { display: 'none' };
  const relativeIndex = group ? globalIndex - group.start : globalIndex;
  const column = relativeIndex % columns.value;
  const row = Math.floor(relativeIndex / columns.value);
  return {
    left: `${column * (columnWidth.value + 12)}px`,
    top: `${group ? group.itemsTop + (row * rowStride.value) : row * rowStride.value}px`,
    width: `${columnWidth.value}px`,
  };
}

function globalIndexAtY(y: number) {
  if (!groupLayouts.value.length) {
    const row = Math.max(0, Math.floor(y / Math.max(1, rowStride.value)));
    return Math.max(0, Math.min(props.total - 1, (row * columns.value) + Math.floor(columns.value / 2)));
  }
  let group = groupLayouts.value.find((entry) => y < entry.bottom) || groupLayouts.value.at(-1);
  if (!group) return 0;
  if (group.collapsed) {
    group = groupLayouts.value.find((entry) => !entry.collapsed && entry.top >= group!.top)
      || [...groupLayouts.value].reverse().find((entry) => !entry.collapsed);
    if (!group) return 0;
  }
  if (y < group.itemsTop) return Math.min(props.total - 1, group.start);
  const row = Math.max(0, Math.floor((y - group.itemsTop) / Math.max(1, rowStride.value)));
  return Math.max(group.start, Math.min(group.start + group.count - 1,
    group.start + (row * columns.value) + Math.floor(columns.value / 2)));
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

function reportScrollCenter(force = false) {
  const root = grid.value;
  if (!root || (!force && props.loading) || props.total <= props.items.length || (!force && performance.now() < suppressScrollUntil)) return;
  // The spacer rows represent the complete folder, so derive the visible global
  // index from the actual row geometry.  A scroll-height ratio is unstable when
  // a different 1000-item window is mounted and used to make adjacent windows
  // repeatedly replace each other.
  const center = globalIndexAtY(root.scrollTop + (root.clientHeight / 2));
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

function updateStickyGroup() {
  const root = grid.value;
  if (!root || !groupLayouts.value.length) {
    activeGroupKey.value = '';
    stickyGroupVisible.value = false;
    return;
  }
  const canvasY = Math.max(0, root.scrollTop - 12);
  const current = groupLayouts.value.find((group) => canvasY < group.bottom)
    || groupLayouts.value.at(-1);
  activeGroupKey.value = current?.key || '';
  // The floating copy only appears after the real heading has completely left
  // the viewport. This prevents the first row from being displayed twice.
  stickyGroupVisible.value = Boolean(current && canvasY >= current.top + groupHeaderHeight);
}

function scrollToTop() {
  const root = grid.value;
  if (!root) return;
  suppressScrollUntil = performance.now() + 250;
  lastRequestedIndex = -1;
  root.scrollTop = 0;
  updateStickyGroup();
}

function groupHasLoadedItems(group: {start: number; count: number; collapsed: boolean}) {
  return group.collapsed || (group.start < loadedEnd.value && group.start + group.count > props.offset);
}

async function toggleDateGroup(key: string) {
  const root = grid.value;
  const before = groupLayouts.value.find((group) => group.key === key);
  const viewportOffset = root && before ? before.top + 12 - root.scrollTop : 0;
  emit('toggleGroup', key);
  await nextTick();
  const after = groupLayouts.value.find((group) => group.key === key);
  if (root && after) root.scrollTop = Math.max(0, after.top + 12 - viewportOffset);
  updateStickyGroup();
  lastRequestedIndex = -1;
  reportScrollCenter(true);
}

function onScroll() {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    updateStickyGroup();
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

defineExpose({ navigate, scrollToTop });

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
  updateStickyGroup();
});
watch(() => [props.items, props.viewSize], async () => {
  await nextTick();
  measureGrid();
}, { deep: false });
watch(() => [props.groups, props.collapsedGroups], () => {
  void nextTick(() => {
    updateStickyGroup();
    lastRequestedIndex = -1;
    reportScrollCenter(true);
  });
}, { deep: false, immediate: true });
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
    <div v-if="stickyGroupVisible && activeGroupKey" class="sticky-date-group">
      {{ groupLayouts.find((group) => group.key === activeGroupKey)?.label }}
    </div>
    <div class="virtual-canvas" :style="{ height: `${canvasHeight}px` }">
      <button
        v-for="group in groupLayouts"
        :key="`header-${group.key}`"
        type="button"
        class="date-group-header"
        :style="{ top: `${group.top}px` }"
        @click="toggleDateGroup(group.key)"
      >
        <span>{{ group.collapsed ? '▶' : '▼' }} {{ group.label }}</span>
        <span>{{ group.count }}件</span>
      </button>
      <div
        v-for="group in groupLayouts.filter((entry) => !groupHasLoadedItems(entry))"
        :key="`loading-${group.key}`"
        class="date-group-loading"
        :style="{ top: `${group.itemsTop + 4}px` }"
      >画像を読み込み中…</div>
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
