<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import FileGrid from './FileGrid.vue';
import FolderTree from './FolderTree.vue';
import XpDialog from './XpDialog.vue';
import { imageViewerApi } from '@/api/client';
import { saveSort, savedSort, useDesktopStore } from '@/stores/desktop';
import { useExplorerStore } from '@/stores/explorer';
import { useNotificationStore } from '@/stores/notifications';
import type { DesktopWindow, MediaItem, ViewSize } from '@/types';

const props = defineProps<{ win: DesktopWindow }>();
const desktop = useDesktopStore();
const explorer = useExplorerStore();
const notice = useNotificationStore();
const uploadInput = ref<HTMLInputElement>();
const fileGrid = ref<InstanceType<typeof FileGrid>>();
const contextMenu = ref<{visible: boolean; x: number; y: number; item?: MediaItem; folder?: string}>({ visible: false, x: 0, y: 0 });
type ExplorerDialog =
  | { kind: 'new-folder'; title: string; value: string }
  | { kind: 'rename'; title: string; value: string; extension: string; item: MediaItem }
  | { kind: 'move' | 'copy'; title: string; destination: string; paths: string[] }
  | { kind: 'delete'; title: string; paths: string[] }
  | { kind: 'rename-folder'; title: string; value: string; folder: string }
  | { kind: 'move-folder'; title: string; destination: string; folder: string }
  | { kind: 'delete-folder'; title: string; folder: string }
  | { kind: 'append-confirm'; title: string; sources: string[]; target: MediaItem }
  | { kind: 'properties'; title: string; item: MediaItem; data: Record<string, unknown> };
const dialog = ref<ExplorerDialog | null>(null);
let versionTimer = 0;
const viewSizes: ViewSize[] = ['xxl', 'xl', 'lg'];
const viewLabels: Record<ViewSize, string> = { xxl: '特大', xl: '大', lg: '中' };

const model = computed(() => props.win.explorer!);
const folderData = computed(() => explorer.dataFor(model.value.folder, model.value.sort));
const items = computed(() => folderData.value.items);
const selectedItems = computed(() => {
  const selected = new Set(model.value.selectedPaths);
  return items.value.filter((item) => selected.has(item.path));
});
const selectedClipboardImage = computed(() =>
  selectedItems.value.length === 1 && selectedItems.value[0]?.mediaType === 'image'
    ? selectedItems.value[0]
    : undefined);
const currentLabel = computed(() => `画像ライブラリ${model.value.folder ? `/${model.value.folder}` : ''}`);
const parentFolder = computed(() => model.value.folder.split('/').slice(0, -1).join('/'));
const folderParent = (folder: string) => folder.split('/').slice(0, -1).join('/');
const folderName = (folder: string) => folder.split('/').at(-1) || folder;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback;
}

async function load(reset = false) {
  try {
    await explorer.load(model.value.folder, model.value.sort, reset);
    props.win.title = `エクスプローラー - ${currentLabel.value}`;
  } catch (error) {
    notice.show(errorMessage(error, '画像一覧を読み込めませんでした。'), true);
  }
}

async function changeFolder(folder: string) {
  model.value.folder = folder;
  model.value.sort = savedSort(folder);
  model.value.selectedPaths = [];
  model.value.anchorPath = '';
  model.value.appendSources = [];
  await load(true);
}

function setSort() {
  model.value.sort = model.value.sort === 'asc' ? 'desc' : 'asc';
  saveSort(model.value.folder, model.value.sort);
  load(true);
}

function setSize(size: ViewSize) {
  model.value.viewSize = size;
  try {
    const values = JSON.parse(localStorage.getItem('mfu.imageViewer.vue.size') || '{}');
    values[model.value.folder] = size;
    localStorage.setItem('mfu.imageViewer.vue.size', JSON.stringify(values));
  } catch { /* storage is optional */ }
}

function createFolder() {
  dialog.value = { kind: 'new-folder', title: '新しいフォルダー', value: '' };
}

async function commitCreateFolder(name: string) {
  if (!name.trim()) return;
  try {
    const result = await imageViewerApi.createFolder(model.value.folder, name.trim());
    dialog.value = null;
    await load(true);
    await changeFolder(result.folder);
    notice.show('フォルダーを作成しました。');
  } catch (error) { notice.show(errorMessage(error, '作成に失敗しました。'), true); }
}

function selectionFromRange(path: string): string[] {
  const end = items.value.findIndex((item) => item.path === path);
  const start = items.value.findIndex((item) => item.path === model.value.anchorPath);
  if (start < 0 || end < 0) return [path];
  return items.value.slice(Math.min(start, end), Math.max(start, end) + 1).map((item) => item.path);
}

async function selectItem(event: MouseEvent, item: MediaItem) {
  contextMenu.value.visible = false;
  if (model.value.appendSources.length) {
    if (model.value.appendSources.includes(item.path)) {
      notice.show('後付け先には、選択元とは別のファイルを指定してください。', true);
      return;
    }
    dialog.value = {
      kind: 'append-confirm',
      title: '後付け連番の確認',
      sources: model.value.appendSources.slice(),
      target: item,
    };
    return;
  }

  if (event.shiftKey && model.value.anchorPath) {
    const range = selectionFromRange(item.path);
    model.value.selectedPaths = event.ctrlKey || event.metaKey
      ? Array.from(new Set([...model.value.selectedPaths, ...range]))
      : range;
  } else if (event.ctrlKey || event.metaKey) {
    model.value.selectedPaths = model.value.selectedPaths.includes(item.path)
      ? model.value.selectedPaths.filter((path) => path !== item.path)
      : [...model.value.selectedPaths, item.path];
    model.value.anchorPath = item.path;
  } else {
    model.value.selectedPaths = [item.path];
    model.value.anchorPath = item.path;
  }
}

function marquee(paths: string[], additive: boolean) {
  model.value.selectedPaths = additive
    ? Array.from(new Set([...model.value.selectedPaths, ...paths]))
    : paths;
  model.value.anchorPath = paths.at(-1) || '';
}

function keyboardSelect(item: MediaItem, options: { extend: boolean; toggle: boolean; focusOnly: boolean }) {
  if (options.focusOnly) return;
  if (options.extend && model.value.anchorPath) {
    model.value.selectedPaths = selectionFromRange(item.path);
    return;
  }
  if (options.toggle) {
    model.value.selectedPaths = model.value.selectedPaths.includes(item.path)
      ? model.value.selectedPaths.filter((path) => path !== item.path)
      : [...model.value.selectedPaths, item.path];
  } else {
    model.value.selectedPaths = [item.path];
  }
  model.value.anchorPath = item.path;
}

function openItem(item: MediaItem) {
  if (item.mediaType === 'image') {
    desktop.openMedia(item, items.value, {
      folder: model.value.folder,
      sort: model.value.sort,
      offset: folderData.value.offset,
      total: folderData.value.total,
    });
    return;
  }
  const sequence = items.value.filter((entry) => entry.mediaType === 'video');
  desktop.openMedia(item, sequence);
}

function openFromKeyboard(item: MediaItem) {
  if (model.value.selectedPaths.includes(item.path) && selectedItems.value.length > 1) openSelected();
  else openItem(item);
}

function openSelected() {
  selectedItems.value.forEach(openItem);
}

async function movePaths(paths = model.value.selectedPaths, destination?: string) {
  if (!paths.length) return;
  if (destination === undefined) {
    dialog.value = { kind: 'move', title: 'ファイルの移動', destination: model.value.folder, paths: paths.slice() };
    return;
  }
  const target = destination;
  try {
    await imageViewerApi.move(paths, target);
    dialog.value = null;
    model.value.selectedPaths = [];
    await Promise.all([explorer.refreshFolder(model.value.folder), explorer.refreshFolder(target)]);
    notice.show(`${paths.length}件を移動しました。`);
  } catch (error) { notice.show(errorMessage(error, '移動に失敗しました。'), true); }
}

function remapFolderPath(value: string, source: string, replacement: string) {
  if (value === source) return replacement;
  if (value.startsWith(`${source}/`)) return `${replacement}${value.slice(source.length)}`;
  return value;
}

async function syncOpenExplorerFolders(source: string, replacement: string) {
  const loads = new Map<string, 'asc' | 'desc'>();
  desktop.windows.forEach((win) => {
    if (!win.explorer) return;
    const next = remapFolderPath(win.explorer.folder, source, replacement);
    if (next !== win.explorer.folder) {
      win.explorer.folder = next;
      win.explorer.sort = savedSort(next);
      win.explorer.selectedPaths = [];
      win.explorer.anchorPath = '';
      win.explorer.appendSources = [];
    }
    loads.set(win.explorer.folder, win.explorer.sort);
  });
  await Promise.all(Array.from(loads, ([folder, sort]) => explorer.load(folder, sort, true)));
}

function openFolderInWindow(folder: string) {
  contextMenu.value.visible = false;
  desktop.openExplorer(folder);
}

function renameFolder(folder: string) {
  if (!folder) return;
  dialog.value = { kind: 'rename-folder', title: 'フォルダー名の変更', value: folderName(folder), folder };
}

async function commitRenameFolder(folder: string, name: string) {
  const cleanName = name.trim();
  if (!cleanName || cleanName === folderName(folder)) { dialog.value = null; return; }
  try {
    const result = await imageViewerApi.renameFolder(folder, cleanName);
    dialog.value = null;
    await syncOpenExplorerFolders(folder, result.path);
    notice.show('フォルダー名を変更しました。');
  } catch (error) { notice.show(errorMessage(error, 'フォルダー名を変更できませんでした。'), true); }
}

function folderMoveDestinations(source: string) {
  return explorer.allFolders.filter((folder) => folder !== source && !folder.startsWith(`${source}/`));
}

async function moveFolder(source: string, destination?: string) {
  if (!source) return;
  if (destination === undefined) {
    dialog.value = {
      kind: 'move-folder', title: 'フォルダーの移動',
      destination: folderParent(source), folder: source,
    };
    return;
  }
  if (destination === source || destination.startsWith(`${source}/`)) {
    notice.show('フォルダーを自分自身の配下へ移動できません。', true);
    return;
  }
  try {
    const result = await imageViewerApi.moveFolder(source, destination);
    dialog.value = null;
    await syncOpenExplorerFolders(source, result.path);
    notice.show(`「${folderName(source)}」を移動しました。`);
  } catch (error) { notice.show(errorMessage(error, 'フォルダーを移動できませんでした。'), true); }
}

function deleteFolder(folder: string, confirmed = false) {
  if (!folder) return;
  if (!confirmed) {
    dialog.value = { kind: 'delete-folder', title: 'フォルダー削除の確認', folder };
    return;
  }
  void (async () => {
    try {
      await imageViewerApi.deleteFolder(folder);
      const destination = folderParent(folder);
      dialog.value = null;
      await syncOpenExplorerFolders(folder, destination);
      notice.show(`空フォルダー「${folderName(folder)}」を削除しました。`);
    } catch (error) { notice.show(errorMessage(error, 'フォルダーを削除できませんでした。'), true); }
  })();
}

async function copyPaths(paths = model.value.selectedPaths, destination?: string) {
  if (!paths.length) return;
  if (destination === undefined) {
    dialog.value = { kind: 'copy', title: 'ファイルのコピー', destination: model.value.folder, paths: paths.slice() };
    return;
  }
  try {
    await imageViewerApi.copy(paths, destination);
    dialog.value = null;
    await explorer.refreshFolder(destination);
    notice.show(`${paths.length}件をコピーしました。`);
  } catch (error) { notice.show(errorMessage(error, 'コピーに失敗しました。'), true); }
}

async function deletePaths(paths = model.value.selectedPaths, confirmed = false) {
  if (!paths.length) return;
  if (!confirmed) {
    dialog.value = { kind: 'delete', title: '削除の確認', paths: paths.slice() };
    return;
  }
  try {
    await imageViewerApi.delete(paths);
    dialog.value = null;
    model.value.selectedPaths = [];
    await load(true);
    notice.show(`${paths.length}件を削除しました。`);
  } catch (error) { notice.show(errorMessage(error, '削除に失敗しました。'), true); }
}

function renameSelected() {
  const item = selectedItems.value[0];
  if (!item) return;
  const parts = filenameParts(item.name);
  dialog.value = { kind: 'rename', title: '名前の変更', value: parts.stem, extension: parts.extension, item };
}

function filenameParts(name: string) {
  const index = name.lastIndexOf('.');
  if (index <= 0) return { stem: name, extension: '' };
  return { stem: name.slice(0, index), extension: name.slice(index + 1) };
}

async function commitRename(item: MediaItem, stem: string, keepOpen = false) {
  const cleanStem = stem.trim();
  if (!cleanStem) return;
  const currentStem = filenameParts(item.name).stem;
  if (cleanStem === currentStem) { if (!keepOpen) dialog.value = null; return; }
  try {
    const result = await imageViewerApi.renameStem(item.path, cleanStem);
    const entry = result.entry as MediaItem | undefined;
    const oldPath = item.path;
    if (entry) Object.assign(item, entry);
    else {
      const extension = filenameParts(item.name).extension;
      item.name = `${cleanStem}${extension ? `.${extension}` : ''}`;
      item.path = String(result.path || item.path);
    }
    if (dialog.value?.kind === 'properties') {
      dialog.value.item = item;
      dialog.value.data = result;
      dialog.value.title = `${item.name} のプロパティ`;
    } else if (!keepOpen) dialog.value = null;
    model.value.selectedPaths = [];
    await load(true);
    const updatedPath = String(result.path || entry?.path || item.path);
    model.value.selectedPaths = updatedPath ? [updatedPath] : [];
    if (oldPath !== updatedPath) model.value.anchorPath = updatedPath;
    notice.show('名前を変更しました。');
  } catch (error) { notice.show(errorMessage(error, '名前変更に失敗しました。'), true); }
}

async function showProperties() {
  const item = selectedItems.value[0];
  if (!item) return;
  try {
    const data = await imageViewerApi.properties(item.path);
    dialog.value = { kind: 'properties', title: `${item.name} のプロパティ`, item, data };
  } catch (error) { notice.show(errorMessage(error, 'プロパティを取得できませんでした。'), true); }
}

async function reloadProperties() {
  if (dialog.value?.kind !== 'properties') return;
  try {
    const data = await imageViewerApi.properties(dialog.value.item.path);
    dialog.value.data = data;
  } catch (error) { notice.show(errorMessage(error, 'プロパティを再読み込みできませんでした。'), true); }
}

function propertyStem() {
  if (dialog.value?.kind !== 'properties') return '';
  return String(dialog.value.data.stem || filenameParts(dialog.value.item.name).stem);
}

function setPropertyStem(value: string) {
  if (dialog.value?.kind === 'properties') dialog.value.data.stem = value;
}

function propertyExtension() {
  if (dialog.value?.kind !== 'properties') return '';
  return String(dialog.value.data.extension || `.${filenameParts(dialog.value.item.name).extension}`);
}

function propertyNameChanged() {
  if (dialog.value?.kind !== 'properties') return false;
  return propertyStem().trim() !== filenameParts(dialog.value.item.name).stem;
}

async function applyPropertyRename(closeAfter: boolean) {
  if (dialog.value?.kind !== 'properties') return;
  if (propertyNameChanged()) await commitRename(dialog.value.item, propertyStem(), true);
  if (closeAfter) dialog.value = null;
}

function formatDateTime(value: unknown) {
  const date = new Date(Number(value || 0) * 1000);
  if (Number.isNaN(date.getTime())) return '―';
  const two = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}/${two(date.getMonth() + 1)}/${two(date.getDate())} ${two(date.getHours())}:${two(date.getMinutes())}:${two(date.getSeconds())}`;
}

function formatNumber(value: unknown) {
  return Number(value || 0).toLocaleString('ja-JP');
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(2)} GB`;
}

function uploadBatches(files: File[]) {
  const batches: File[][] = [];
  let current: File[] = [];
  let bytes = 0;
  files.forEach((file) => {
    if (current.length >= 20 || (current.length && bytes + file.size > 80 * 1024 * 1024)) {
      batches.push(current); current = []; bytes = 0;
    }
    current.push(file); bytes += file.size;
  });
  if (current.length) batches.push(current);
  return batches;
}

async function uploadFiles(files: File[], paste = false) {
  if (!files.length) return;
  try {
    for (const batch of uploadBatches(files)) {
      await imageViewerApi.upload(batch, model.value.folder, model.value.numbering, paste);
    }
    await load(true);
    notice.show(`${files.length}件を追加しました。`);
  } catch (error) { notice.show(errorMessage(error, 'アップロードに失敗しました。'), true); }
}

function pasteHandler(event: ClipboardEvent) {
  if (desktop.activeId !== props.win.id) return;
  const files = Array.from(event.clipboardData?.files || []).filter((file) => file.type.startsWith('image/'));
  if (!files.length) return;
  event.preventDefault();
  uploadFiles(files, true);
}

function clipboardImageExtension(type: string) {
  const subtype = type.split('/')[1]?.toLowerCase() || 'png';
  if (subtype === 'jpeg') return 'jpg';
  if (subtype === 'svg+xml') return 'svg';
  return subtype.replace(/[^a-z0-9]/g, '') || 'png';
}

async function pasteFromClipboard() {
  try {
    if (!navigator.clipboard?.read) {
      throw new Error('このブラウザーでは貼付ボタンからクリップボード画像を読み取れません。Ctrl+Vをお試しください。');
    }
    const clipboardItems = await navigator.clipboard.read();
    const files: File[] = [];
    for (const item of clipboardItems) {
      const type = item.types.find((candidate) => candidate.startsWith('image/'));
      if (!type) continue;
      const blob = await item.getType(type);
      const extension = clipboardImageExtension(type);
      files.push(new File([blob], `clipboard-${Date.now()}-${files.length + 1}.${extension}`, { type }));
    }
    if (!files.length) throw new Error('クリップボードに貼り付け可能な画像がありません。');
    await uploadFiles(files, true);
  } catch (error) {
    notice.show(errorMessage(error, 'クリップボード画像を貼り付けできませんでした。'), true);
  }
}

function startAppend() {
  if (model.value.appendSources.length) {
    cancelAppendMode();
    return;
  }
  if (!model.value.selectedPaths.length) return;
  const byPath = new Map(items.value.map((item) => [item.path, item.name]));
  const collator = new Intl.Collator('ja', { numeric: true, sensitivity: 'base' });
  model.value.appendSources = model.value.selectedPaths.slice().sort((left, right) => {
    const leftName = byPath.get(left) || left.split('/').at(-1) || left;
    const rightName = byPath.get(right) || right.split('/').at(-1) || right;
    return collator.compare(leftName, rightName) || collator.compare(left, right);
  });
  model.value.selectedPaths = [];
  notice.show('後付け先にするファイルをクリックしてください。Escで中止できます。', false, 6000);
}

function cancelAppendMode(showNotice = true) {
  model.value.appendSources = [];
  if (dialog.value?.kind === 'append-confirm') dialog.value = null;
  if (showNotice) notice.show('後付け連番を中止しました。');
}

function appendPreview(sources: string[], target: MediaItem) {
  const targetStem = filenameParts(target.name).stem;
  const sequenceMatch = targetStem.match(/^(.+)_([0-9]+)$/);
  const sequenceStem = sequenceMatch?.[1] || targetStem;
  const sequenceNumber = Number(sequenceMatch?.[2] || 0);
  return [target.path, ...sources].map((path, index) => {
    const source = items.value.find((item) => item.path === path);
    const sourceName = source?.name || path.split('/').at(-1) || path;
    const extension = filenameParts(sourceName).extension;
    const nextNumber = sequenceNumber + index + (sequenceMatch ? 0 : 1);
    return {
      sourceName,
      nextName: sequenceMatch && index === 0
        ? sourceName
        : `${sequenceStem}_${nextNumber}${extension ? `.${extension}` : ''}`,
      isTarget: index === 0,
    };
  });
}

async function confirmAppend(sources: string[], target: MediaItem) {
  try {
    await imageViewerApi.appendSequence(sources, target.path);
    dialog.value = null;
    model.value.appendSources = [];
    model.value.selectedPaths = [];
    await load(true);
    notice.show('後付け連番を適用しました。');
  } catch (error) { notice.show(errorMessage(error, '後付け連番に失敗しました。'), true); }
}

function keyHandler(event: KeyboardEvent) {
  if (desktop.activeId !== props.win.id) return;
  if (dialog.value) return;
  const target = event.target as HTMLElement | null;
  if (target?.matches('input, textarea, select, [contenteditable="true"]')) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c') {
    event.preventDefault();
    const item = selectedItems.value.find((entry) => entry.path === model.value.anchorPath)
      || selectedItems.value[0];
    if (item?.mediaType === 'image') void copyImageToClipboard(item);
    else notice.show('クリップボードへコピーする画像を1枚選択してください。', true);
    return;
  }
  const navigationKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End', 'PageUp', 'PageDown'];
  if (navigationKeys.includes(event.key) && !target?.closest('.file-card')) {
    event.preventDefault();
    void fileGrid.value?.navigate(event.key, model.value.anchorPath, {
      extend: event.shiftKey,
      toggle: false,
      focusOnly: event.ctrlKey || event.metaKey,
    });
    return;
  }
  if (event.key === 'Escape' && model.value.appendSources.length) {
    cancelAppendMode();
  }
  if (event.key === 'Delete' && model.value.selectedPaths.length) deletePaths();
  if (event.key === 'F2' && selectedItems.value.length === 1) {
    event.preventDefault();
    renameSelected();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a') {
    event.preventDefault();
    model.value.selectedPaths = items.value.map((item) => item.path);
  }
}

async function rebuildThumbnails() {
  try {
    const result = await imageViewerApi.startThumbnails(model.value.folder, true);
    let job: Record<string, unknown> = {};
    do {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      job = await imageViewerApi.thumbnailJob(result.jobId);
      notice.show(`サムネイル生成中 ${Number(job.completed || job.processed || 0)} / ${Number(job.total || 0)}`, false, 1200);
    } while (!['done', 'error'].includes(String(job.status || '')));
    await load(true);
    notice.show('サムネイル生成が完了しました。');
  } catch (error) { notice.show(errorMessage(error, 'サムネイル生成に失敗しました。'), true); }
}

function showContext(event: MouseEvent, item: MediaItem) {
  if (!model.value.selectedPaths.includes(item.path)) {
    model.value.selectedPaths = [item.path]; model.value.anchorPath = item.path;
  }
  contextMenu.value = { visible: true, x: event.clientX, y: event.clientY, item };
}

function showFolderContext(event: MouseEvent, folder: string) {
  contextMenu.value = { visible: true, x: event.clientX, y: event.clientY, folder };
}

function closeContext() { contextMenu.value.visible = false; }

async function copyImageToClipboard(item?: MediaItem) {
  if (!item || item.mediaType !== 'image') return;
  try {
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
      throw new Error('このブラウザーは画像のクリップボードコピーに対応していません。');
    }
    const response = await fetch(item.url, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const bitmap = await createImageBitmap(await response.blob());
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('画像を変換できません。');
    context.drawImage(bitmap, 0, 0);
    bitmap.close();
    const png = await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('画像をPNGに変換できません。')), 'image/png'));
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
    notice.show('画像をクリップボードにコピーしました。');
  } catch (error) {
    notice.show(errorMessage(error, '画像をクリップボードにコピーできませんでした。'), true);
  }
}

async function loadAt(index: number) {
  try {
    await explorer.loadCenter(model.value.folder, model.value.sort, index);
  } catch (error) {
    notice.show(errorMessage(error, '画像一覧の読み込みに失敗しました。'), true);
  }
}

async function pollVersion() {
  if (document.hidden || folderData.value.loading || !folderData.value.version) return;
  try {
    const result = await imageViewerApi.version(model.value.folder);
    if (result.version && result.version !== folderData.value.version) {
      await explorer.refreshFolder(model.value.folder);
    }
  } catch { /* 一時的な失敗は次回に再試行する */ }
}

function propertyValue(key: string, fallback: unknown = '―') {
  if (dialog.value?.kind !== 'properties') return fallback;
  return dialog.value.data[key] || fallback;
}

function propertySourceUrl() {
  if (dialog.value?.kind !== 'properties') return '';
  const value = String(dialog.value.data.sourceUrl || dialog.value.item.sourceUrl || '').trim();
  return /^https?:\/\//i.test(value) ? value : '';
}

watch(() => model.value.folder, () => { props.win.title = `エクスプローラー - ${currentLabel.value}`; });
onMounted(() => {
  load(true);
  document.addEventListener('paste', pasteHandler);
  document.addEventListener('keydown', keyHandler);
  document.addEventListener('pointerdown', closeContext);
  versionTimer = window.setInterval(pollVersion, 10000);
});
onBeforeUnmount(() => {
  document.removeEventListener('paste', pasteHandler);
  document.removeEventListener('keydown', keyHandler);
  document.removeEventListener('pointerdown', closeContext);
  window.clearInterval(versionTimer);
});
</script>

<template>
  <div class="explorer-layout" :class="{ busy: folderData.loading }">
    <aside class="explorer-sidebar">
      <div class="sidebar-title">フォルダー</div>
      <FolderTree
        :folders="explorer.allFolders"
        :current="model.folder"
        @select="changeFolder"
        @move="movePaths"
        @move-folder="moveFolder"
        @context="showFolderContext"
      />
    </aside>
    <section class="explorer-main">
      <div class="explorer-toolbar">
        <div class="path-box">{{ currentLabel }}</div>
        <button type="button" @click="load(true)">更新</button>
        <button type="button" :disabled="!model.folder" @click="changeFolder(parentFolder)">上へ</button>
        <button type="button" @click="setSort">{{ model.sort === 'asc' ? '昇順' : '逆順' }}</button>
        <button v-for="size in viewSizes" :key="size" type="button" :class="{pressed: model.viewSize === size}" @click="setSize(size)">{{ viewLabels[size] }}</button>
        <button type="button" @click="desktop.openExplorer(model.folder)">別窓</button>
      </div>
      <div class="explorer-actions">
        <button type="button" @click="createFolder">新規フォルダー</button>
        <button type="button" @click="uploadInput?.click()">追加</button>
        <button type="button" :class="{pressed: model.numbering}" @click="model.numbering = !model.numbering">連番 {{ model.numbering ? 'ON' : 'OFF' }}</button>
        <button type="button" :disabled="!selectedItems.length" @click="openSelected">開く</button>
        <button type="button" :disabled="selectedItems.length !== 1" @click="renameSelected">名前変更</button>
        <button type="button" :disabled="!selectedItems.length" @click="movePaths()">移動</button>
        <button type="button" :disabled="!selectedItems.length" @click="copyPaths()">コピー</button>
        <button type="button" title="選択画像をクリップボードへコピー" :disabled="!selectedClipboardImage" @click="copyImageToClipboard(selectedClipboardImage)">コピー（📎）</button>
        <button type="button" title="クリップボード画像を現在のフォルダーへ貼り付け" @click="pasteFromClipboard">貼付</button>
        <button type="button" :class="{pressed: model.appendSources.length}" :disabled="!selectedItems.length && !model.appendSources.length" @click="startAppend">{{ model.appendSources.length ? '後付け解除' : '後付け' }}</button>
        <button type="button" :disabled="!selectedItems.length" class="danger" @click="deletePaths()">削除</button>
        <button type="button" @click="rebuildThumbnails">サムネイル再生成</button>
        <input ref="uploadInput" type="file" multiple accept="image/*,video/*" hidden @change="uploadFiles(Array.from(($event.target as HTMLInputElement).files || []))">
      </div>
      <div v-if="model.appendSources.length" class="append-banner">{{ model.appendSources.length }}件の後付け先を選択してください。Escで中止</div>
      <FileGrid
        ref="fileGrid"
        :items="items"
        :selected="model.selectedPaths"
        :view-size="model.viewSize"
        :loading="folderData.loading"
        :has-more="folderData.hasMore"
        :append-mode="Boolean(model.appendSources.length)"
        :total="folderData.total"
        :offset="folderData.offset"
        @select="selectItem"
        @open="openFromKeyboard"
        @context="showContext"
        @upload="uploadFiles"
        @marquee="marquee"
        @keyboard="keyboardSelect"
        @load-center="loadAt"
      />
      <footer class="explorer-status">
        <span>{{ folderData.total || items.length }}件</span>
        <span>{{ selectedItems.length }}件選択</span>
      </footer>
    </section>
    <div v-if="contextMenu.visible && contextMenu.item" class="context-menu" :style="{left:`${contextMenu.x}px`,top:`${contextMenu.y}px`}" @pointerdown.stop>
      <button type="button" @click="contextMenu.item && openItem(contextMenu.item); closeContext()">開く</button>
      <button type="button" @click="renameSelected(); closeContext()">名前変更</button>
      <button type="button" @click="movePaths(); closeContext()">移動</button>
      <button type="button" @click="copyPaths(); closeContext()">コピー</button>
      <button type="button" :disabled="contextMenu.item?.mediaType !== 'image'" @click="copyImageToClipboard(contextMenu.item); closeContext()">コピー（クリップボード）</button>
      <button type="button" @click="showProperties(); closeContext()">プロパティ</button>
      <hr>
      <button type="button" class="danger" @click="deletePaths(); closeContext()">削除</button>
    </div>
    <div v-else-if="contextMenu.visible && contextMenu.folder !== undefined" class="context-menu" :style="{left:`${contextMenu.x}px`,top:`${contextMenu.y}px`}" @pointerdown.stop>
      <button type="button" @click="openFolderInWindow(contextMenu.folder)">新しいウィンドウで開く</button>
      <hr>
      <button type="button" :disabled="!contextMenu.folder" @click="renameFolder(contextMenu.folder); closeContext()">名前の変更</button>
      <button type="button" :disabled="!contextMenu.folder" @click="moveFolder(contextMenu.folder); closeContext()">フォルダーの移動</button>
      <hr>
      <button type="button" class="danger" :disabled="!contextMenu.folder" @click="deleteFolder(contextMenu.folder); closeContext()">フォルダーの削除</button>
    </div>

    <XpDialog v-if="dialog?.kind === 'new-folder'" :title="dialog.title" @close="dialog = null">
      <form class="xp-form" @submit.prevent="commitCreateFolder(dialog.value)">
        <label>フォルダー名<input v-model="dialog.value" autofocus></label>
        <div class="xp-dialog-actions"><button type="submit">作成</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'rename'" :title="dialog.title" @close="dialog = null">
      <form class="xp-form" @submit.prevent="commitRename(dialog.item, dialog.value)">
        <label>新しい名前<span class="filename-editor"><input v-model="dialog.value" autofocus><span class="extension-box">{{ dialog.extension || '拡張子なし' }}</span></span></label>
        <p class="dialog-hint">拡張子は変更されません。</p>
        <div class="xp-dialog-actions"><button type="submit">変更</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'rename-folder'" :title="dialog.title" @close="dialog = null">
      <form class="xp-form" @submit.prevent="commitRenameFolder(dialog.folder, dialog.value)">
        <label>新しいフォルダー名<input v-model="dialog.value" autofocus></label>
        <div class="xp-dialog-actions"><button type="submit">変更</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'move-folder'" :title="dialog.title" @close="dialog = null">
      <form class="xp-form" @submit.prevent="moveFolder(dialog.folder, dialog.destination)">
        <p>「{{ folderName(dialog.folder) }}」を別の階層へ移動します。</p>
        <label>移動先フォルダー
          <select v-model="dialog.destination" autofocus>
            <option v-for="folder in folderMoveDestinations(dialog.folder)" :key="folder || '__root__'" :value="folder">{{ folder || '画像ライブラリ' }}</option>
          </select>
        </label>
        <div class="xp-dialog-actions"><button type="submit">移動</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'delete-folder'" :title="dialog.title" @close="dialog = null">
      <form @submit.prevent="deleteFolder(dialog.folder, true)">
        <div class="xp-confirm"><span class="xp-confirm-icon">⚠️</span><p>空フォルダー「{{ folderName(dialog.folder) }}」を削除しますか？<br>ファイルまたはサブフォルダーがある場合は削除できません。</p></div>
        <p class="dialog-hint">実行時に管理者パスキー認証を行います。</p>
        <div class="xp-dialog-actions"><button type="submit" class="danger" autofocus>削除</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'move' || dialog?.kind === 'copy'" :title="dialog.title" @close="dialog = null">
      <form class="xp-form" @submit.prevent="dialog.kind === 'move' ? movePaths(dialog.paths, dialog.destination) : copyPaths(dialog.paths, dialog.destination)">
        <p>{{ dialog.paths.length }}件のファイルを{{ dialog.kind === 'move' ? '移動' : 'コピー' }}します。</p>
        <label>保存先フォルダー
          <select v-model="dialog.destination" autofocus><option v-for="folder in explorer.allFolders" :key="folder" :value="folder">{{ folder || '画像ライブラリ' }}</option></select>
        </label>
        <div class="xp-dialog-actions"><button type="submit">{{ dialog.kind === 'move' ? '移動' : 'コピー' }}</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'delete'" :title="dialog.title" @close="dialog = null">
      <form @submit.prevent="deletePaths(dialog.paths, true)">
        <div class="xp-confirm"><span class="xp-confirm-icon">⚠️</span><p>{{ dialog.paths.length }}件を削除済み領域へ移動しますか？</p></div>
        <div class="xp-dialog-actions"><button type="submit" class="danger" autofocus>削除</button><button type="button" @click="dialog = null">キャンセル</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'append-confirm'" :title="dialog.title" :width="700" @close="cancelAppendMode()">
      <form @submit.prevent="confirmAppend(dialog.sources, dialog.target)">
        <p class="append-confirm-message">次の{{ dialog.sources.length + 1 }}件を、基準ファイルから順番に名前変更します。</p>
        <div class="append-confirm-list" role="table" aria-label="後付け連番の変更内容">
          <div class="append-confirm-header" role="row"><span>現在の名前</span><span></span><span>変更後の名前</span></div>
          <div v-for="row in appendPreview(dialog.sources, dialog.target)" :key="row.sourceName" class="append-confirm-row" role="row">
            <span><b v-if="row.isTarget">基準：</b>{{ row.sourceName }}</span><span aria-hidden="true">→</span><strong>{{ row.nextName }}</strong>
          </div>
        </div>
        <p class="dialog-hint">拡張子は変更されません。実行後は元の名前へ自動では戻せません。</p>
        <div class="xp-dialog-actions"><button type="submit" autofocus>後付け連番を実行</button><button type="button" @click="dialog = null">選択へ戻る</button></div>
      </form>
    </XpDialog>
    <XpDialog v-else-if="dialog?.kind === 'properties'" :title="dialog.title" :width="940" @close="dialog = null">
      <form @submit.prevent="applyPropertyRename(true)">
      <div class="properties-dialog legacy-properties">
        <div class="property-name-row">
          <div class="properties-icon">{{ dialog.item.mediaType === 'video' ? '🎥' : '🖼️' }}</div>
          <input :value="propertyStem()" aria-label="ファイル名" autofocus @input="setPropertyStem(($event.target as HTMLInputElement).value)">
          <span class="extension-box">{{ propertyExtension().replace(/^\./, '') || '拡張子なし' }}</span>
          <button type="button" @click="reloadProperties">再読み込み</button>
        </div>
        <section class="property-section"><dl>
          <dt>ファイルの種類:</dt><dd>{{ dialog.item.mediaType === 'video' ? '動画ファイル' : '画像ファイル' }} ({{ propertyExtension().replace(/^\./, '') }})</dd>
          <dt>場所 (仮想上):</dt><dd>/{{ propertyValue('virtualFolder', dialog.item.folder) }}</dd>
          <dt>仮想パス:</dt><dd>{{ propertyValue('virtualPath', dialog.item.path) }}</dd>
          <dt>実ファイル:</dt><dd>{{ propertyValue('realPath') }}</dd>
          <dt>取得元URL:</dt><dd class="source-url"><a v-if="propertySourceUrl()" :href="propertySourceUrl()" target="_blank" rel="noopener noreferrer">{{ propertySourceUrl() }}</a><span v-else>―</span></dd>
        </dl></section>
        <section class="property-section"><dl>
          <dt>サイズ:</dt><dd>{{ formatBytes(Number(propertyValue('size', dialog.item.size))) }} ({{ formatNumber(propertyValue('size', dialog.item.size)) }} バイト)</dd>
          <dt>{{ dialog.item.mediaType === 'video' ? '動画サイズ:' : '画像サイズ:' }}</dt><dd>{{ propertyValue('width') !== '―' ? `${formatNumber(propertyValue('width'))} × ${formatNumber(propertyValue('height'))} px` : '―' }}</dd>
          <dt>MIMEタイプ:</dt><dd>{{ propertyValue('mimeType', dialog.item.mediaType) }}</dd>
          <dt>SHA-256:</dt><dd class="hash-value">{{ propertyValue('sha256') }}</dd>
        </dl></section>
        <section class="property-section"><dl>
          <dt>作成日時:</dt><dd>{{ formatDateTime(propertyValue('created')) }}</dd>
          <dt>更新日時:</dt><dd>{{ formatDateTime(propertyValue('modified', dialog.item.mtime)) }}</dd>
          <dt>アクセス日時:</dt><dd>{{ formatDateTime(propertyValue('accessed')) }}</dd>
        </dl></section>
      </div>
      <div class="xp-dialog-actions"><button type="submit">OK</button><button type="button" @click="dialog = null">キャンセル</button><button type="button" :disabled="!propertyNameChanged()" @click="applyPropertyRename(false)">適用</button></div>
      </form>
    </XpDialog>
  </div>
</template>
