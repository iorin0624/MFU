const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'layer-upload-viewer.js'), 'utf8',
);

function element(properties = {}) {
  const listeners = new Map();
  return {
    ...properties,
    hidden: false,
    disabled: false,
    textContent: '',
    classList: { toggle() {} },
    addEventListener(type, listener) { listeners.set(type, listener); },
    emit(type, event = {}) { return listeners.get(type)?.(event); },
    focus() { this.focused = true; },
    removeAttribute() {},
  };
}

function setup() {
  const links = [
    element({ href: '/image/one', dataset: { replyUuid: 'reply-a', filename: 'one.jpg' } }),
    element({ href: '/image/two', dataset: { replyUuid: 'reply-a', filename: 'two.jpg' } }),
    element({ href: '/image/three', dataset: { replyUuid: 'reply-b', filename: 'three.png' } }),
  ];
  const ids = Object.fromEntries([
    'layer-image-viewer-image', 'layer-image-viewer-image-error',
    'layer-image-viewer-counter', 'layer-image-viewer-reply',
    'layer-image-viewer-filename', 'layer-image-viewer-copy',
    'layer-image-viewer-download', 'layer-image-viewer-status',
    'layer-image-viewer-prev', 'layer-image-viewer-next',
    'layer-image-viewer-close',
  ].map(id => [id, element()]));
  const viewer = element({
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; this.emit('close'); },
  });
  ids['layer-image-viewer'] = viewer;
  const image = ids['layer-image-viewer-image'];
  Object.defineProperty(image, 'src', {
    set(value) { this.source = value; this.emit('load'); },
    get() { return this.source; },
  });
  let clipboardItem;
  const document = {
    querySelectorAll() { return links; },
    getElementById(id) { return ids[id]; },
    createElement(tag) {
      assert.equal(tag, 'canvas');
      return {
        getContext() { return { drawImage() {} }; },
        toBlob(callback) { callback(new Blob(['png'], { type: 'image/png' })); },
      };
    },
    addEventListener(type, listener) { this.keydown = listener; },
  };
  const navigator = {
    clipboard: {
      async write(items) { clipboardItem = items[0]; await clipboardItem.data['image/png']; },
    },
  };
  class ClipboardItem {
    constructor(data) { this.data = data; }
  }
  vm.runInNewContext(source, {
    document, navigator, ClipboardItem, Blob,
    fetch: async () => ({
      ok: true,
      headers: { get: () => 'image/jpeg' },
      blob: async () => new Blob(['jpeg'], { type: 'image/jpeg' }),
    }),
    createImageBitmap: async () => ({ width: 2, height: 2, close() {} }),
  });
  return { links, ids, viewer, document, getClipboardItem: () => clipboardItem };
}

test('viewer moves across reply UUIDs without opening a new tab', () => {
  const { links, ids, viewer } = setup();
  let prevented = false;
  links[1].emit('click', { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(viewer.open, true);
  assert.equal(ids['layer-image-viewer-counter'].textContent, '2 / 3');
  ids['layer-image-viewer-next'].emit('click');
  assert.equal(ids['layer-image-viewer-reply'].textContent, '返信UUID: reply-b');
  assert.equal(ids['layer-image-viewer-filename'].textContent, 'three.png');
  assert.equal(ids['layer-image-viewer-counter'].textContent, '3 / 3');
  ids['layer-image-viewer-next'].emit('click');
  assert.equal(ids['layer-image-viewer-counter'].textContent, '1 / 3');
});

test('Ctrl+C copies the displayed image pixels as PNG', async () => {
  const { links, ids, document, getClipboardItem } = setup();
  links[2].emit('click', { preventDefault() {} });
  let prevented = false;
  document.keydown({
    ctrlKey: true, metaKey: false, key: 'c', target: ids['layer-image-viewer-copy'],
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, true);
  assert.ok(getClipboardItem());
  const copied = await getClipboardItem().data['image/png'];
  assert.equal(copied.type, 'image/png');
});
