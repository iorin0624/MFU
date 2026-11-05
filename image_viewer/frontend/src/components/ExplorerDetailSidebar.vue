<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { imageViewerApi } from '@/api/client';
import { useNotificationStore } from '@/stores/notifications';
import type { MediaItem, StyleAssignment, StyleJob } from '@/types';

const props = defineProps<{item?: MediaItem}>();
const emit = defineEmits<{close: []; rename: []; properties: []}>();
const notice = useNotificationStore();
const details = ref<Record<string, unknown>>({});
const major = ref<StyleAssignment['major'] | ''>('');
const middleRows = ref<{name: string; smallText: string}[]>([]);
const gpuJob = ref<StyleJob | null>(null);
const loading = ref(false);
const saving = ref(false);
const error = ref('');
const editHint = ref('');
let loadSequence = 0;
let saveTimer = 0;
let pending: {path: string; assignment: StyleAssignment | null} | null = null;
let saveChain: Promise<void> = Promise.resolve();

const sourceUrl = computed(() => {
  const value = String(details.value.sourceUrl || props.item?.sourceUrl || '').trim();
  return /^https?:\/\//i.test(value) ? value : '';
});
const dimensions = computed(() => {
  const width = Number(details.value.width || 0);
  const height = Number(details.value.height || 0);
  return width > 0 && height > 0 ? `${width.toLocaleString()} × ${height.toLocaleString()} px` : '―';
});

function formatBytes(size: number) {
  if (!Number.isFinite(size) || size < 0) return '―';
  if (size < 1024) return `${size} B`;
  const unit = size < 1024 ** 2 ? 'KB' : size < 1024 ** 3 ? 'MB' : 'GB';
  const divisor = unit === 'KB' ? 1024 : unit === 'MB' ? 1024 ** 2 : 1024 ** 3;
  return `${(size / divisor).toFixed(2)} ${unit}`;
}

function errorText(cause: unknown) {
  return cause instanceof Error ? cause.message : '分類タグを保存できませんでした。';
}

function flushSave(): Promise<void> {
  window.clearTimeout(saveTimer);
  const current = pending;
  pending = null;
  if (!current) return saveChain;
  saveChain = saveChain.catch(() => {}).then(async () => {
    if (props.item?.path === current.path) saving.value = true;
    try {
      if (current.assignment) await imageViewerApi.saveStyleTags(current.path, current.assignment);
      else await imageViewerApi.clearStyleTags(current.path);
      if (props.item?.path === current.path) {
        error.value = '';
        editHint.value = '保存しました';
      }
    } catch (cause) {
      if (props.item?.path === current.path) error.value = errorText(cause);
      notice.show(errorText(cause), true);
    } finally {
      if (props.item?.path === current.path) saving.value = false;
    }
  });
  return saveChain;
}

function queueSave(delay = 450) {
  const path = props.item?.path;
  if (!path || loading.value) return;
  if (major.value && middleRows.value.some((row) => !row.name.trim())) {
    window.clearTimeout(saveTimer);
    pending = null;
    editHint.value = '中分類名を入力すると自動保存します';
    return;
  }
  const assignment: StyleAssignment | null = major.value ? {
    major: major.value,
    middleTags: middleRows.value.map((row) => ({
      name: row.name.trim(),
      smallTags: row.smallText.split(/[、,，\n]/).map((part) => part.trim()).filter(Boolean),
    })),
  } : null;
  pending = {path, assignment};
  editHint.value = '保存待ち…';
  window.clearTimeout(saveTimer);
  if (delay) saveTimer = window.setTimeout(() => { void flushSave(); }, delay);
  else void flushSave();
}

function changeMajor() {
  if (!major.value) middleRows.value = [];
  queueSave(0);
}

function addMiddle() {
  if (middleRows.value.length >= 12 || !major.value) return;
  middleRows.value.push({name: '', smallText: ''});
  editHint.value = '中分類名を入力すると自動保存します';
}

function removeMiddle(index: number) {
  middleRows.value.splice(index, 1);
  queueSave(0);
}

function applySuggestion() {
  const assignment = gpuJob.value?.suggestion?.assignment;
  if (!assignment) return;
  major.value = assignment.major;
  middleRows.value = assignment.middleTags.map((row) => ({name: row.name, smallText: row.smallTags.join('、')}));
  queueSave(0);
}

async function reload() {
  const item = props.item;
  const sequence = ++loadSequence;
  await flushSave();
  if (!item || props.item?.path !== item.path || sequence !== loadSequence) return;
  loading.value = true;
  details.value = {};
  major.value = '';
  middleRows.value = [];
  gpuJob.value = null;
  error.value = '';
  editHint.value = '';
  try {
    const [propertyData, styleData] = await Promise.all([
      imageViewerApi.properties(item.path),
      item.mediaType === 'image' ? imageViewerApi.styleTags(item.path) : Promise.resolve(null),
    ]);
    if (sequence !== loadSequence || props.item?.path !== item.path) return;
    details.value = propertyData;
    major.value = styleData?.assignment?.major || '';
    middleRows.value = (styleData?.assignment?.middleTags || []).map((row) => ({
      name: row.name, smallText: row.smallTags.join('、'),
    }));
    gpuJob.value = styleData?.gpuJob || null;
  } catch (cause) {
    if (sequence === loadSequence) error.value = errorText(cause);
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}

watch(() => props.item?.path, () => { void reload(); }, {immediate: true});
onBeforeUnmount(() => { loadSequence++; void flushSave(); });
</script>

<template>
  <aside class="explorer-details" aria-label="選択画像の詳細と分類タグ">
    <header class="details-header"><strong>詳細と分類</strong><button type="button" aria-label="右サイドバーを閉じる" @click="emit('close')">×</button></header>
    <div v-if="!item" class="details-empty">画像を1枚選択すると、情報と分類タグを表示します。</div>
    <div v-else class="details-content">
      <div class="details-actions"><button type="button" :disabled="loading" @click="reload">再読み込み</button><button type="button" @click="emit('properties')">すべてのプロパティ</button></div>
      <p v-if="loading" class="details-state">読み込み中…</p>
      <p v-if="error" class="details-error" role="alert">{{ error }}</p>
      <dl class="details-properties">
        <dt>名前</dt><dd><span class="details-wrap">{{ item.name }}</span><button type="button" @click="emit('rename')">変更</button></dd>
        <dt>取得元URL</dt><dd class="details-wrap"><a v-if="sourceUrl" :href="sourceUrl" target="_blank" rel="noopener noreferrer">{{ sourceUrl }}</a><span v-else>―</span></dd>
        <dt>サイズ</dt><dd>{{ formatBytes(Number(details.size ?? item.size)) }}</dd>
        <dt>画像サイズ</dt><dd>{{ item.mediaType === 'image' ? dimensions : '―' }}</dd>
      </dl>
      <section v-if="item.mediaType === 'image'" class="details-tags">
        <h3>分類タグ</h3>
        <p class="details-help">変更すると自動保存します。</p>
        <label>大分類
          <select v-model="major" :disabled="loading" @change="changeMajor">
            <option value="">未分類</option>
            <option value="lolita">ロリータ系</option>
            <option value="jirai">地雷系</option>
            <option value="casual">私服系</option>
          </select>
        </label>
        <div v-for="(row, index) in middleRows" :key="index" class="details-tag-row">
          <label>中分類<input v-model="row.name" maxlength="50" :disabled="loading" placeholder="例: クラシカル" @input="queueSave()" @blur="flushSave()"></label>
          <label>小分類（「、」区切り）<input v-model="row.smallText" :disabled="loading" placeholder="例: 青、レース" @input="queueSave()" @blur="flushSave()"></label>
          <button type="button" :disabled="loading" @click="removeMiddle(index)">この中分類を削除</button>
        </div>
        <button type="button" :disabled="loading || !major || middleRows.length >= 12" @click="addMiddle">中分類を追加</button>
        <div v-if="gpuJob?.status === 'done' && gpuJob.suggestion" class="details-suggestion">
          <span>GPU候補: {{ {lolita: 'ロリータ系', jirai: '地雷系', casual: '私服系'}[gpuJob.suggestion.assignment.major] }}</span>
          <button type="button" @click="applySuggestion">候補を適用</button>
        </div>
        <p v-if="saving || editHint" class="details-state" role="status">{{ saving ? '保存中…' : editHint }}</p>
      </section>
    </div>
  </aside>
</template>

<style scoped>
.explorer-details { min-width: 0; min-height: 0; display: flex; flex-direction: column; border-left: 1px solid #b3bfd1; background: #f8faff; color: #17233b; box-shadow: -2px 0 5px #142b581c; }
.details-header { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; border-bottom: 1px solid #b3bfd1; background: linear-gradient(#fff, #dce8fa); color: #173d7f; }
.details-header button { min-width: 28px; min-height: 28px; }
.details-content { flex: 1; min-height: 0; overflow: auto; padding: 12px; }
.details-empty { padding: 16px 12px; color: #536887; font-size: 13px; line-height: 1.6; }
.details-actions { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 14px; }
.details-properties { margin: 0; }
.details-properties dt { margin: 12px 0 3px; color: #4a6389; font-size: 12px; font-weight: 700; }
.details-properties dd { margin: 0; display: flex; align-items: start; gap: 6px; font-size: 13px; }
.details-properties a { overflow-wrap: anywhere; }
.details-wrap { min-width: 0; overflow-wrap: anywhere; }
.details-properties dd button { flex: 0 0 auto; }
.details-tags { margin-top: 18px; padding-top: 12px; border-top: 1px solid #d2ddec; }
.details-tags h3 { margin: 0; font-size: 15px; }
.details-help, .details-state, .details-error { margin: 6px 0 12px; font-size: 12px; }
.details-help, .details-state { color: #536887; }
.details-error { color: #b42626; overflow-wrap: anywhere; }
.details-tags label { display: grid; gap: 4px; margin: 10px 0; font-size: 12px; font-weight: 700; color: #395479; }
.details-tags input, .details-tags select { width: 100%; min-width: 0; box-sizing: border-box; padding: 5px; font: inherit; color: #17233b; }
.details-tag-row { margin: 12px 0; padding: 8px; border: 1px solid #d2ddec; border-radius: 5px; background: white; }
.details-tag-row button { font-size: 12px; }
.details-suggestion { display: grid; gap: 5px; margin-top: 15px; padding: 8px; border: 1px solid #a7c5f4; border-radius: 5px; background: #eaf2ff; font-size: 12px; }
</style>
