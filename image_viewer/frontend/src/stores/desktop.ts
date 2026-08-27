import { defineStore } from 'pinia';
import { computed, ref } from 'vue';
import type { DesktopWindow, MediaItem, SortDirection, ViewSize } from '@/types';

export function savedSort(folder: string): SortDirection {
  try {
    const values = JSON.parse(localStorage.getItem('mfu.imageViewer.vue.sort') || '{}');
    return values[folder] === 'desc' ? 'desc' : 'asc';
  } catch { return 'asc'; }
}

export function saveSort(folder: string, sort: SortDirection) {
  try {
    const values = JSON.parse(localStorage.getItem('mfu.imageViewer.vue.sort') || '{}');
    values[folder] = sort;
    localStorage.setItem('mfu.imageViewer.vue.sort', JSON.stringify(values));
  } catch { /* storage is optional */ }
}

function savedSize(folder: string): ViewSize {
  try {
    const values = JSON.parse(localStorage.getItem('mfu.imageViewer.vue.size') || '{}');
    return ['xxl', 'xl', 'lg'].includes(values[folder]) ? values[folder] : 'xl';
  } catch { return 'xl'; }
}

export const useDesktopStore = defineStore('image-viewer-desktop', () => {
  const windows = ref<DesktopWindow[]>([]);
  const activeId = ref('');
  let nextExplorer = 1;
  let nextViewer = 1;
  let topZ = 10;

  const activeWindow = computed(() => windows.value.find((entry) => entry.id === activeId.value));

  function activate(id: string) {
    const win = windows.value.find((entry) => entry.id === id);
    if (!win) return;
    win.z = ++topZ;
    win.minimized = false;
    activeId.value = id;
  }

  function openExplorer(folder = '') {
    const id = `vue-explorer-${nextExplorer++}`;
    const offset = (nextExplorer % 6) * 28;
    windows.value.push({
      id, kind: 'explorer', title: 'エクスプローラー',
      x: 56 + offset, y: 42 + offset, width: 980, height: 650,
      z: ++topZ, minimized: false, maximized: false,
      explorer: {
        folder, sort: savedSort(folder), viewSize: savedSize(folder),
        selectedPaths: [], anchorPath: '', numbering: true, appendSources: [],
      },
    });
    activeId.value = id;
    return id;
  }

  function openMedia(
    item: MediaItem,
    sequence: MediaItem[],
    sequenceContext?: DesktopWindow['sequenceContext'],
  ) {
    const kind = item.mediaType === 'video' ? 'video' : 'image';
    const id = `vue-${kind}-${nextViewer++}`;
    const offset = (nextViewer % 7) * 26;
    windows.value.push({
      id, kind, title: item.name,
      x: 120 + offset, y: 58 + offset,
      width: kind === 'video' ? 820 : 760,
      height: kind === 'video' ? 580 : 620,
      z: ++topZ, minimized: false, maximized: false,
      media: item, sequence: sequence.slice(),
      sequenceContext: sequenceContext ? { ...sequenceContext } : undefined,
    });
    activeId.value = id;
  }

  function close(id: string) {
    windows.value = windows.value.filter((entry) => entry.id !== id);
    if (activeId.value === id) {
      const next = [...windows.value].filter((entry) => !entry.minimized).sort((a, b) => b.z - a.z)[0];
      activeId.value = next?.id || '';
    }
  }

  function minimize(id: string) {
    const win = windows.value.find((entry) => entry.id === id);
    if (!win) return;
    win.minimized = true;
    const next = [...windows.value].filter((entry) => !entry.minimized && entry.id !== id).sort((a, b) => b.z - a.z)[0];
    activeId.value = next?.id || '';
  }

  function toggleMaximize(id: string) {
    const win = windows.value.find((entry) => entry.id === id);
    if (!win) return;
    win.maximized = !win.maximized;
    activate(id);
  }

  function updateGeometry(id: string, values: Partial<Pick<DesktopWindow, 'x'|'y'|'width'|'height'>>) {
    const win = windows.value.find((entry) => entry.id === id);
    if (win) Object.assign(win, values);
  }

  return {
    windows, activeId, activeWindow,
    activate, openExplorer, openMedia, close, minimize, toggleMaximize, updateGeometry,
  };
});
