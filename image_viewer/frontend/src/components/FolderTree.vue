<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { isReplyFolder, replyRoot } from '@/utils/replyFolders';

const props = defineProps<{ folders: string[]; current: string; labels?: Record<string, string> }>();
const emit = defineEmits<{
  select: [folder: string];
  move: [paths: string[], folder: string];
  context: [event: MouseEvent, folder: string];
}>();

function label(folder: string) {
  return props.labels?.[folder] || (folder ? folder.split('/').at(-1) || folder : 'uploads');
}

function depth(folder: string) {
  if (isReplyFolder(folder)) return folder.split('/').length - 1;
  return folder ? folder.split('/').length : 0;
}

const expanded = ref(new Set<string>(['']));
const parentOf = (folder: string) => !folder || folder === replyRoot ? null : folder.split('/').slice(0, -1).join('/');
const parents = computed(() => new Set(props.folders.map(parentOf)));
const hasChildren = (folder: string) => parents.value.has(folder);

function expandAncestors(folder: string) {
  const next = new Set(expanded.value);
  let current = parentOf(folder);
  if (folder === replyRoot) next.add(replyRoot);
  while (current !== null) {
    next.add(current);
    current = parentOf(current);
  }
  expanded.value = next;
}

function toggle(folder: string) {
  if (!hasChildren(folder)) return;
  const next = new Set(expanded.value);
  if (next.has(folder)) next.delete(folder);
  else next.add(folder);
  expanded.value = next;
}

const visibleFolders = computed(() => props.folders.filter((folder) => {
  let parent = parentOf(folder);
  while (parent !== null) {
    if (!expanded.value.has(parent)) return false;
    parent = parentOf(parent);
  }
  return true;
}));

watch(() => props.current, expandAncestors, { immediate: true });

function onDrop(event: DragEvent, folder: string) {
  event.preventDefault();
  if (isReplyFolder(folder)) return;
  try {
    const parsed = JSON.parse(event.dataTransfer?.getData('application/x-mfu-paths') || '[]');
    if (Array.isArray(parsed) && parsed.length) emit('move', parsed.map(String), folder);
  } catch { /* Ignore drags from other applications. */ }
}
</script>

<template>
  <nav class="folder-tree" aria-label="フォルダー">
    <button
      v-for="folder in visibleFolders"
      :key="folder || '__root__'"
      type="button"
      class="folder-row"
      :class="{ active: folder === current }"
      :style="{ paddingInlineStart: `${6 + depth(folder) * 18}px` }"
      @click="emit('select', folder)"
      @contextmenu.prevent.stop="emit('context', $event, folder)"
      @dblclick="toggle(folder)"
      @dragover.prevent
      @drop="onDrop($event, folder)"
    >
      <span
        class="folder-twisty"
        :class="{ empty: !hasChildren(folder) }"
        @click.stop="toggle(folder)"
      >{{ hasChildren(folder) ? (expanded.has(folder) ? '⌄' : '›') : '' }}</span>
      <span class="folder-glyph">📁</span><span class="folder-label" :title="label(folder)">{{ label(folder) }}</span>
    </button>
  </nav>
</template>
