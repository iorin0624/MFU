<script setup lang="ts">
import { computed, ref, watch } from 'vue';

const props = defineProps<{ folders: string[]; current: string }>();
const emit = defineEmits<{
  select: [folder: string];
  move: [paths: string[], folder: string];
}>();

function label(folder: string) {
  return folder ? folder.split('/').at(-1) || folder : 'uploads';
}

function depth(folder: string) {
  return folder ? folder.split('/').length : 0;
}

const expanded = ref(new Set<string>(['']));
const parentOf = (folder: string) => folder.split('/').slice(0, -1).join('/');
const hasChildren = (folder: string) => props.folders.some((candidate) => candidate && parentOf(candidate) === folder);

function expandAncestors(folder: string) {
  const next = new Set(expanded.value);
  let current = parentOf(folder);
  next.add('');
  while (current) {
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
  if (!folder) return true;
  let parent = parentOf(folder);
  while (parent || folder) {
    if (!expanded.value.has(parent)) return false;
    if (!parent) break;
    parent = parentOf(parent);
  }
  return true;
}));

watch(() => props.current, expandAncestors, { immediate: true });

function onDrop(event: DragEvent, folder: string) {
  event.preventDefault();
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
      @dblclick="toggle(folder)"
      @dragover.prevent
      @drop="onDrop($event, folder)"
    >
      <span
        class="folder-twisty"
        :class="{ empty: !hasChildren(folder) }"
        @click.stop="toggle(folder)"
      >{{ hasChildren(folder) ? (expanded.has(folder) ? '⌄' : '›') : '' }}</span>
      <span class="folder-glyph">📁</span><span>{{ label(folder) }}</span>
    </button>
  </nav>
</template>
