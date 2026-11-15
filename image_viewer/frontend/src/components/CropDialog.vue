<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import XpDialog from './XpDialog.vue';

type Rectangle = {x: number; y: number; width: number; height: number};
const props = defineProps<{src: string; name: string; busy: boolean}>();
const emit = defineEmits<{close: []; save: [rectangle: Rectangle]}>();
const image = ref<HTMLImageElement>();
const ratio = ref('free');
const rectangle = ref<Rectangle>({x: 0.1, y: 0.1, width: 0.8, height: 0.8});
let start: {x: number; y: number} | null = null;
const boxStyle = computed(() => ({
  left: `${rectangle.value.x * 100}%`, top: `${rectangle.value.y * 100}%`,
  width: `${rectangle.value.width * 100}%`, height: `${rectangle.value.height * 100}%`,
}));
const valid = computed(() => Boolean(image.value &&
  rectangle.value.width * image.value.naturalWidth >= 2 &&
  rectangle.value.height * image.value.naturalHeight >= 2));

function centerRectangle() {
  const img = image.value;
  if (!img?.naturalWidth || !img.naturalHeight) return;
  if (ratio.value === 'free') {
    rectangle.value = {x: 0.1, y: 0.1, width: 0.8, height: 0.8};
    return;
  }
  const wanted = Number(ratio.value);
  const availableWidth = img.naturalWidth * 0.8;
  const availableHeight = img.naturalHeight * 0.8;
  const widthPx = Math.min(availableWidth, availableHeight * wanted);
  const heightPx = widthPx / wanted;
  const width = widthPx / img.naturalWidth;
  const height = heightPx / img.naturalHeight;
  rectangle.value = {x: (1 - width) / 2, y: (1 - height) / 2, width, height};
}
watch(ratio, centerRectangle);

function position(event: PointerEvent) {
  const bounds = image.value!.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
    y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
  };
}
function pointerDown(event: PointerEvent) {
  if (props.busy || !image.value?.naturalWidth) return;
  start = position(event);
  (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  rectangle.value = {x: start.x, y: start.y, width: 0, height: 0};
}
function pointerMove(event: PointerEvent) {
  if (!start || !image.value) return;
  const end = position(event);
  let dx = end.x - start.x;
  let dy = end.y - start.y;
  if (ratio.value !== 'free') {
    const wanted = Number(ratio.value);
    const img = image.value;
    const signX = dx < 0 ? -1 : 1;
    const signY = dy < 0 ? -1 : 1;
    const maxWidth = (signX > 0 ? 1 - start.x : start.x) * img.naturalWidth;
    const maxHeight = (signY > 0 ? 1 - start.y : start.y) * img.naturalHeight;
    const requestedWidth = Math.max(Math.abs(dx) * img.naturalWidth, Math.abs(dy) * img.naturalHeight * wanted);
    const widthPx = Math.min(requestedWidth, maxWidth, maxHeight * wanted);
    dx = signX * widthPx / img.naturalWidth;
    dy = signY * widthPx / wanted / img.naturalHeight;
  }
  rectangle.value = {
    x: Math.min(start.x, start.x + dx), y: Math.min(start.y, start.y + dy),
    width: Math.abs(dx), height: Math.abs(dy),
  };
}
function pointerUp(event: PointerEvent) {
  if (!start) return;
  pointerMove(event);
  start = null;
  if (!valid.value) centerRectangle();
}
</script>

<template>
  <XpDialog :title="`切り抜き：${name}`" :width="920" @close="emit('close')">
    <p>範囲をドラッグして選択してください。保存後も「元画像に戻す」で復元できます。</p>
    <label class="crop-ratio-label">縦横比
      <select v-model="ratio" :disabled="busy">
        <option value="free">自由</option><option value="1">1:1</option>
        <option value="1.3333333333333333">4:3</option><option value="1.5">3:2</option>
      </select>
    </label>
    <div class="crop-stage">
      <div class="crop-image-wrap">
        <img ref="image" :src="src" :alt="name" draggable="false" @load="centerRectangle">
        <div class="crop-hit-area" @pointerdown="pointerDown" @pointermove="pointerMove" @pointerup="pointerUp" @pointercancel="pointerUp">
          <div class="crop-selection" :style="boxStyle"></div>
        </div>
      </div>
    </div>
    <div class="xp-dialog-actions">
      <button type="button" :disabled="busy || !valid" @click="emit('save', rectangle)">{{ busy ? '保存中…' : '切り抜きを保存' }}</button>
      <button type="button" :disabled="busy" @click="emit('close')">キャンセル</button>
    </div>
  </XpDialog>
</template>
