<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import XpDialog from './XpDialog.vue';
import {
  createCropRectangle, fitCropRatio, moveCropRectangle, resizeCropRectangle,
  type CropHandle, type CropPoint, type CropRectangle,
} from '@/utils/cropGeometry';

type RatioChoice = 'free' | '1:1' | '4:3' | '3:2';
type Orientation = 'landscape' | 'portrait';
type Interaction = {
  kind: 'create' | 'move' | 'resize';
  start: CropPoint;
  before: CropRectangle;
  handle?: CropHandle;
};

const props = defineProps<{src: string; name: string; busy: boolean}>();
const emit = defineEmits<{close: []; save: [rectangle: CropRectangle]}>();
const image = ref<HTMLImageElement | null>(null);
const selection = ref<HTMLDivElement | null>(null);
const ratio = ref<RatioChoice>('free');
const orientation = ref<Orientation>('landscape');
const rectangle = ref<CropRectangle>({x: .1, y: .1, width: .8, height: .8});
const handles: CropHandle[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const handleLabels: Record<CropHandle, string> = {
  nw: '左上', n: '上', ne: '右上', e: '右', se: '右下', s: '下', sw: '左下', w: '左',
};
let interaction: Interaction | null = null;

const wantedRatio = computed<number | null>(() => {
  if (ratio.value === 'free') return null;
  if (ratio.value === '1:1') return 1;
  const landscapeRatio = ratio.value === '4:3' ? 4 / 3 : 3 / 2;
  return orientation.value === 'landscape' ? landscapeRatio : 1 / landscapeRatio;
});
const displayedRatio = computed(() => {
  if (ratio.value === 'free' || ratio.value === '1:1') return ratio.value === 'free' ? '自由' : '1:1';
  return orientation.value === 'landscape' ? ratio.value : ratio.value.split(':').reverse().join(':');
});
const boxStyle = computed(() => ({
  left: `${rectangle.value.x * 100}%`, top: `${rectangle.value.y * 100}%`,
  width: `${rectangle.value.width * 100}%`, height: `${rectangle.value.height * 100}%`,
}));
const valid = computed(() => Boolean(image.value &&
  rectangle.value.width * image.value.naturalWidth >= 2 &&
  rectangle.value.height * image.value.naturalHeight >= 2 &&
  rectangle.value.x >= 0 && rectangle.value.y >= 0 &&
  rectangle.value.x + rectangle.value.width <= 1.000001 &&
  rectangle.value.y + rectangle.value.height <= 1.000001));

function adjustRatio() {
  const img = image.value;
  if (img?.naturalWidth && img.naturalHeight && wantedRatio.value !== null) {
    rectangle.value = fitCropRatio(rectangle.value, wantedRatio.value, img.naturalWidth, img.naturalHeight);
  }
}
watch([ratio, orientation], adjustRatio);

function imageLoaded() {
  const img = image.value;
  if (!img) return;
  orientation.value = img.naturalHeight > img.naturalWidth ? 'portrait' : 'landscape';
  adjustRatio();
}

function position(event: PointerEvent): CropPoint {
  const bounds = image.value!.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
    y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
  };
}

function pointerDown(event: PointerEvent) {
  const img = image.value;
  if (props.busy || !img?.naturalWidth || !img.naturalHeight ||
      (event.pointerType === 'mouse' && event.button !== 0)) return;
  const target = event.target as HTMLElement;
  const handleElement = target.closest<HTMLButtonElement>('[data-crop-handle]');
  const handle = handleElement?.dataset.cropHandle as CropHandle | undefined;
  const kind = handle ? 'resize' : target.closest('.crop-selection') ? 'move' : 'create';
  interaction = {kind, start: position(event), before: {...rectangle.value}, handle};
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  if (kind === 'create') {
    rectangle.value = {...interaction.start, width: 0, height: 0};
  } else if (kind === 'move') {
    selection.value?.focus({preventScroll: true});
  } else {
    handleElement?.focus({preventScroll: true});
  }
  event.preventDefault();
}

function pointerMove(event: PointerEvent) {
  const current = interaction;
  const img = image.value;
  if (!current || !img) return;
  const end = position(event);
  const deltaX = (end.x - current.start.x) * img.naturalWidth;
  const deltaY = (end.y - current.start.y) * img.naturalHeight;
  if (current.kind === 'create') {
    rectangle.value = createCropRectangle(
      current.start, end, wantedRatio.value, img.naturalWidth, img.naturalHeight,
    );
  } else if (current.kind === 'move') {
    rectangle.value = moveCropRectangle(
      current.before, deltaX, deltaY, img.naturalWidth, img.naturalHeight,
    );
  } else if (current.handle) {
    rectangle.value = resizeCropRectangle(
      current.before, current.handle, deltaX, deltaY,
      wantedRatio.value, img.naturalWidth, img.naturalHeight,
    );
  }
}

function pointerUp(event: PointerEvent) {
  if (!interaction) return;
  const kind = interaction.kind;
  const before = interaction.before;
  pointerMove(event);
  interaction = null;
  const hitArea = event.currentTarget as HTMLElement;
  if (hitArea.hasPointerCapture(event.pointerId)) hitArea.releasePointerCapture(event.pointerId);
  if (!valid.value) rectangle.value = before;
  else if (kind === 'create') selection.value?.focus({preventScroll: true});
}

function pointerCancel(event: PointerEvent) {
  if (interaction) rectangle.value = interaction.before;
  interaction = null;
  const hitArea = event.currentTarget as HTMLElement;
  if (hitArea.hasPointerCapture(event.pointerId)) hitArea.releasePointerCapture(event.pointerId);
}

function nudge(event: KeyboardEvent) {
  if (props.busy || !image.value) return;
  const step = event.shiftKey ? 10 : 1;
  const movement: Record<string, [number, number]> = {
    ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
  };
  const delta = movement[event.key];
  if (!delta) return;
  event.preventDefault();
  event.stopPropagation();
  const handle = (event.target as HTMLElement).dataset.cropHandle as CropHandle | undefined;
  rectangle.value = handle
    ? resizeCropRectangle(rectangle.value, handle, delta[0], delta[1],
        wantedRatio.value, image.value.naturalWidth, image.value.naturalHeight)
    : moveCropRectangle(rectangle.value, delta[0], delta[1],
        image.value.naturalWidth, image.value.naturalHeight);
}
</script>

<template>
  <XpDialog :title="`切り抜き：${name}`" :width="920" @close="emit('close')">
    <p>枠内をドラッグして移動、辺・角をドラッグして大きさを調整できます。保存後も「元画像に戻す」で復元できます。</p>
    <div class="crop-ratio-row">
      <label class="crop-ratio-label">縦横比
        <select v-model="ratio" :disabled="busy">
          <option value="free">自由</option><option value="1:1">1:1</option>
          <option value="4:3">4:3</option><option value="3:2">3:2</option>
        </select>
      </label>
      <button type="button" :disabled="busy || ratio === 'free' || ratio === '1:1'" @click="orientation = orientation === 'landscape' ? 'portrait' : 'landscape'">縦横を切替</button>
      <span>現在：{{ displayedRatio }}</span>
    </div>
    <div class="crop-stage">
      <div class="crop-image-wrap">
        <img ref="image" :src="src" :alt="name" draggable="false" @load="imageLoaded">
        <div class="crop-hit-area" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="pointerCancel" @keydown="nudge">
          <div ref="selection" class="crop-selection" :style="boxStyle" tabindex="0" role="group" aria-label="切り抜き枠。矢印キーで移動できます">
            <button v-for="handle in handles" :key="handle" type="button" class="crop-handle" :class="`crop-handle-${handle}`" :data-crop-handle="handle" :aria-label="`${handleLabels[handle]}の枠を調整`" :disabled="busy"></button>
          </div>
        </div>
      </div>
    </div>
    <p class="crop-keyboard-hint">枠またはハンドルを選択し、矢印キーで元画像の1pxずつ、Shift＋矢印キーで10pxずつ調整できます。</p>
    <div class="xp-dialog-actions">
      <button type="button" :disabled="busy || !valid" @click="emit('save', rectangle)">{{ busy ? '保存中…' : '切り抜きを保存' }}</button>
      <button type="button" :disabled="busy" @click="emit('close')">キャンセル</button>
    </div>
  </XpDialog>
</template>
