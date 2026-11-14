import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import FolderTree from './FolderTree.vue';

describe('FolderTree', () => {
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
