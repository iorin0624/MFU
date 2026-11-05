<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { imageViewerApi } from '@/api/client';
import type { DesktopWindow, StyleJob } from '@/types';

const props = defineProps<{win: DesktopWindow}>();
const jobs = computed(() => [...(props.win.styleJobs || [])].sort((a, b) => a.createdAt - b.createdAt));
const list = ref<HTMLUListElement | null>(null);
const currentJobId = computed(() => jobs.value.find((job) => job.status === 'running')?.id
  || jobs.value.find((job) => job.status === 'pending')?.id
  || jobs.value.filter((job) => job.status === 'done' || job.status === 'error')
    .reduce<StyleJob | undefined>((latest, job) => !latest || job.updatedAt >= latest.updatedAt ? job : latest, undefined)?.id
  || '');
const loading = ref(false);
const error = ref('');
const completed = computed(() => jobs.value.filter((job) => job.status === 'done').length);
const failed = computed(() => jobs.value.filter((job) => job.status === 'error').length);
const active = computed(() => jobs.value.filter((job) => job.status === 'running').length);
const pending = computed(() => jobs.value.filter((job) => job.status === 'pending').length);
const settled = computed(() => completed.value + failed.value);
const percent = computed(() => jobs.value.length ? Math.round(settled.value / jobs.value.length * 100) : 0);
let timer = 0;
let stopped = false;

async function scrollToCurrentJob(behavior: ScrollBehavior = 'smooth') {
  const id = currentJobId.value;
  if (!id || props.win.minimized) return;
  await nextTick();
  const container = list.value;
  const row = [...(container?.querySelectorAll<HTMLLIElement>('[data-job-id]') || [])]
    .find((entry) => entry.dataset.jobId === id);
  if (!container || !row || !container.clientHeight) return;
  const listRect = container.getBoundingClientRect();
  const rowRect = row.getBoundingClientRect();
  container.scrollTo({
    top: container.scrollTop + rowRect.top - listRect.top - (container.clientHeight - rowRect.height) / 2,
    behavior,
  });
}

watch(() => [currentJobId.value, props.win.minimized], () => { void scrollToCurrentJob(); });

function fileName(path: string) {
  return path.split(/[\\/]/).pop() || path;
}

function statusText(job: StyleJob) {
  return {pending: '待機中', running: '分類中', done: '完了', error: '失敗'}[job.status];
}

async function refresh() {
  if (loading.value || stopped) return;
  window.clearTimeout(timer);
  loading.value = true;
  try {
    const existingIds = (props.win.styleJobs || []).map((job) => job.id);
    const updates = new Map<string, Pick<StyleJob, 'id' | 'status' | 'error'>>();
    for (let index = 0; index < existingIds.length; index += 300) {
      const result = await imageViewerApi.styleJobStatuses(existingIds.slice(index, index + 300));
      result.jobs.forEach((job) => updates.set(job.id, job));
    }
    const result = await imageViewerApi.recentStyleJobs();
    if (stopped) return;
    const merged = new Map((props.win.styleJobs || []).map((job) => [job.id, {...job, ...(updates.get(job.id) || {})}]));
    result.jobs.forEach((job) => merged.set(job.id, job));
    props.win.styleJobs = [...merged.values()];
    error.value = '';
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '進捗を取得できませんでした。';
  } finally {
    loading.value = false;
    if (!stopped) timer = window.setTimeout(refresh, active.value || pending.value ? 5000 : 15000);
  }
}

onMounted(async () => {
  await refresh();
  await scrollToCurrentJob('auto');
});
onBeforeUnmount(() => { stopped = true; window.clearTimeout(timer); });
</script>

<template>
  <div class="style-progress-window">
    <header class="progress-heading">
      <div>
        <h2>GPU分類の進捗</h2>
        <p>このウィンドウを開いたまま、画像一覧を操作できます。閉じても分類は続きます。</p>
      </div>
      <button type="button" :disabled="loading" @click="refresh">{{ loading ? '確認中…' : '更新' }}</button>
    </header>
    <div class="progress-counts">
      <strong>{{ settled }} / {{ jobs.length }} 枚処理済み</strong>
      <span>分類中 {{ active }}　待機中 {{ pending }}　完了 {{ completed }}　失敗 {{ failed }}</span>
    </div>
    <div class="progress-track" role="progressbar" :aria-valuenow="percent" aria-valuemin="0" aria-valuemax="100" :aria-label="`GPU分類 ${percent}%`">
      <div :style="{width: `${percent}%`}"></div>
    </div>
    <p v-if="error" class="progress-error">{{ error }}</p>
    <p v-if="!jobs.length && !loading" class="progress-empty">進行中または最近の分類ジョブはありません。</p>
    <ul v-else ref="list" class="progress-list">
      <li v-for="job in jobs" :key="job.id" :data-job-id="job.id" :class="`status-${job.status}`">
        <span class="job-name" :title="job.path">{{ fileName(job.path) }}</span>
        <span class="job-status">{{ statusText(job) }}</span>
        <small v-if="job.error" :title="job.error">{{ job.error }}</small>
      </li>
    </ul>
    <footer>候補は画像のプロパティで確認・適用できます。分類結果は自動適用されません。</footer>
  </div>
</template>

<style scoped>
.style-progress-window { display: flex; flex-direction: column; height: 100%; min-height: 0; padding: 16px; gap: 12px; box-sizing: border-box; color: #1b2d4a; background: #f5f8ff; }
.progress-heading { display: flex; justify-content: space-between; align-items: start; gap: 12px; }
.progress-heading h2 { margin: 0 0 4px; font-size: 19px; }
.progress-heading p, footer { margin: 0; font-size: 12px; color: #536887; }
.progress-heading button { white-space: nowrap; }
.progress-counts { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; font-size: 13px; }
.progress-counts strong { font-size: 17px; }
.progress-track { height: 14px; border-radius: 8px; background: #dbe5f3; overflow: hidden; }
.progress-track > div { height: 100%; background: #2e6de9; transition: width .3s; }
.progress-list { flex: 1; min-height: 0; overflow: auto; list-style: none; margin: 0; padding: 0; border: 1px solid #cdd9e9; border-radius: 8px; background: white; }
.progress-list li { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding: 9px 12px; border-bottom: 1px solid #e5ebf4; }
.progress-list li:last-child { border-bottom: 0; }
.job-name { flex: 1; min-width: 100px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job-status { min-width: 52px; text-align: right; font-weight: 700; }
.status-done .job-status { color: #147c43; }
.status-error .job-status, .progress-error { color: #b42626; }
.status-running .job-status { color: #205cc7; }
.progress-list small { flex-basis: 100%; color: #b42626; overflow-wrap: anywhere; }
.progress-empty { margin: 20px 0; text-align: center; color: #536887; }
</style>
