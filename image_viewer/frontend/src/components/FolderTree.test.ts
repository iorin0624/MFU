import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import FolderTree from './FolderTree.vue';

describe('FolderTree', () => {
  it('shows reply folders by label while selecting a stable, read-only key', async () => {
    const wrapper = mount(FolderTree, {
      props: { folders: ['', '通常', '..', '../42'], current: '..', labels: {
        '..': '加工バック', '../42': '2026年11月01日　Ｄハロお疲れ様会',
      } },
    });
    const child = wrapper.findAll('.folder-row').find((row) => row.text().includes('Ｄハロお疲れ様会'))!;
    expect(child.text()).toContain('2026年11月01日　Ｄハロお疲れ様会');
    await child.trigger('click');
    expect(wrapper.emitted('select')?.[0]).toEqual(['../42']);
    await child.trigger('drop', { dataTransfer: { getData: () => '["通常/1.jpg"]' } });
    expect(wrapper.emitted('move')).toBeUndefined();
  });

  it('opens the folder context menu for the right-clicked folder', async () => {
    const wrapper = mount(FolderTree, {
      props: { folders: ['', '新規フォルダー', '親', '親/子'], current: '' },
    });
    const row = wrapper.findAll('button.folder-row').find((button) => button.text().includes('新規フォルダー'));
    expect(row).toBeDefined();
    await row!.trigger('contextmenu', { clientX: 120, clientY: 80 });

    const emitted = wrapper.emitted('context');
    expect(emitted).toHaveLength(1);
    expect(emitted?.[0]?.[1]).toBe('新規フォルダー');
    expect((emitted?.[0]?.[0] as MouseEvent).defaultPrevented).toBe(true);
  });
});
