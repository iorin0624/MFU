import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import CropDialog from './CropDialog.vue';

const dialogStub = { template: '<div><slot /></div>' };

describe('CropDialog', () => {
  it('shows eight resize handles and switches 4:3 between landscape and portrait', async () => {
    const wrapper = mount(CropDialog, {
      props: {src: '/image_viewer/files/photo.jpg', name: 'photo.jpg', busy: false},
      global: {stubs: {XpDialog: dialogStub}},
    });
    const image = wrapper.get('img').element;
    Object.defineProperty(image, 'naturalWidth', {value: 1200});
    Object.defineProperty(image, 'naturalHeight', {value: 900});
    await wrapper.get('img').trigger('load');
    expect(wrapper.findAll('.crop-handle')).toHaveLength(8);
    await wrapper.get('select').setValue('4:3');
    expect(wrapper.text()).toContain('現在：4:3');
    await wrapper.get('.crop-ratio-row > button').trigger('click');
    expect(wrapper.text()).toContain('現在：3:4');
    wrapper.unmount();
  });
});
