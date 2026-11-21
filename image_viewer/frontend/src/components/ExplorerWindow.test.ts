import { afterEach, expect, it, vi } from 'vitest';
import { shallowMount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { useDesktopStore } from '@/stores/desktop';
import ExplorerWindow from './ExplorerWindow.vue';

vi.mock('@/api/client', () => ({
  imageViewerApi: { list: vi.fn().mockResolvedValue({ ok: true, folders: [''], images: [] }) },
}));
vi.mock('@/config', () => ({ runtimeConfig: {} }));

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it('resizes the folder pane with pointer capture and remembers the width', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  const pinia = createPinia();
  setActivePinia(pinia);
  const desktop = useDesktopStore();
  desktop.openExplorer();
  const wrapper = shallowMount(ExplorerWindow, { props: { win: desktop.windows[0] }, global: { plugins: [pinia] } });
  const separator = wrapper.get('[role="separator"]');
  const capture = vi.fn();
  Object.defineProperty(separator.element, 'setPointerCapture', { value: capture });
  const pointer = (name: string, clientX: number) => {
    const event = new MouseEvent(name, { clientX, button: 0, bubbles: true, cancelable: true });
    Object.defineProperty(event, 'pointerId', { value: 1 });
    separator.element.dispatchEvent(event);
  };
  expect(separator.attributes('aria-valuenow')).toBe('205');
  pointer('pointerdown', 205);
  expect(capture).toHaveBeenCalledWith(1);
  pointer('pointermove', 365);
  pointer('pointerup', 365);
  await nextTick();
  expect(separator.attributes('aria-valuenow')).toBe('365');
  expect(localStorage.getItem('mfu.imageViewer.vue.folderWidth')).toBe('365');
  await separator.trigger('keydown', { key: 'ArrowLeft' });
  expect(separator.attributes('aria-valuenow')).toBe('355');
  wrapper.unmount();
});
