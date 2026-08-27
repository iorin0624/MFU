<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue';

defineProps<{ title: string; width?: number }>();
defineEmits<{ close: [] }>();
const body = ref<HTMLElement>();

function focusableElements() {
  return Array.from(body.value?.querySelectorAll<HTMLElement>(
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  ) || []).filter((element) => element.offsetParent !== null);
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') return;
  if (event.key !== 'Tab') return;
  const controls = focusableElements();
  if (!controls.length) return;
  const current = controls.indexOf(document.activeElement as HTMLElement);
  const next = event.shiftKey
    ? (current <= 0 ? controls.length - 1 : current - 1)
    : (current < 0 || current === controls.length - 1 ? 0 : current + 1);
  event.preventDefault();
  controls[next].focus();
}

onMounted(async () => {
  await nextTick();
  const controls = focusableElements();
  const preferred = body.value?.querySelector<HTMLElement>('[autofocus]');
  (preferred || controls[0])?.focus();
});
</script>

<template>
  <Teleport to="body">
    <div class="xp-dialog-backdrop" @pointerdown.self="$emit('close')">
      <section class="xp-dialog" :style="{ width: `${width || 440}px` }" role="dialog" aria-modal="true" @keydown="handleKeydown" @keydown.esc="$emit('close')">
        <header class="xp-dialog-titlebar">
          <span>{{ title }}</span>
          <button type="button" title="閉じる" @click="$emit('close')">×</button>
        </header>
        <div ref="body" class="xp-dialog-body"><slot /></div>
      </section>
    </div>
  </Teleport>
</template>
