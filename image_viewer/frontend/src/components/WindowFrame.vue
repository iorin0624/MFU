<script setup lang="ts">
import { computed } from 'vue';
import { useDesktopStore } from '@/stores/desktop';
import type { DesktopWindow } from '@/types';

const props = defineProps<{ win: DesktopWindow; icon: string }>();
const desktop = useDesktopStore();

const windowStyle = computed(() => props.win.maximized ? {
  zIndex: props.win.z,
} : {
  left: `${props.win.x}px`, top: `${props.win.y}px`,
  width: `${props.win.width}px`, height: `${props.win.height}px`, zIndex: props.win.z,
});

function startDrag(event: PointerEvent) {
  if (props.win.maximized || (event.target as HTMLElement).closest('button')) return;
  desktop.activate(props.win.id);
  const startX = event.clientX;
  const startY = event.clientY;
  const originX = props.win.x;
  const originY = props.win.y;
  const move = (next: PointerEvent) => desktop.updateGeometry(props.win.id, {
    x: Math.max(0, originX + next.clientX - startX),
    y: Math.max(0, originY + next.clientY - startY),
  });
  const done = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', done);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', done, { once: true });
}

function startResize(event: PointerEvent) {
  if (props.win.maximized) return;
  event.preventDefault();
  desktop.activate(props.win.id);
  const startX = event.clientX;
  const startY = event.clientY;
  const width = props.win.width;
  const height = props.win.height;
  const move = (next: PointerEvent) => desktop.updateGeometry(props.win.id, {
    width: Math.max(360, width + next.clientX - startX),
    height: Math.max(260, height + next.clientY - startY),
  });
  const done = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', done);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', done, { once: true });
}
</script>

<template>
  <article
    class="xp-window"
    :class="{ active: desktop.activeId === win.id, maximized: win.maximized, minimized: win.minimized }"
    :style="windowStyle"
    @pointerdown="desktop.activate(win.id)"
  >
    <header class="xp-titlebar" @pointerdown="startDrag" @dblclick="desktop.toggleMaximize(win.id)">
      <span class="xp-title-icon">{{ icon }}</span>
      <span class="xp-title-text">{{ win.title }}</span>
      <span class="xp-window-actions">
        <button type="button" title="最小化" @click.stop="desktop.minimize(win.id)">_</button>
        <button type="button" title="最大化" @click.stop="desktop.toggleMaximize(win.id)">□</button>
        <button class="close" type="button" title="閉じる" @click.stop="desktop.close(win.id)">×</button>
      </span>
    </header>
    <div class="xp-window-content"><slot /></div>
    <span class="xp-resize-handle" @pointerdown.stop="startResize"></span>
  </article>
</template>
