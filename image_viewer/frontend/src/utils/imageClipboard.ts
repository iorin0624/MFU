async function loadPng(url: string): Promise<Blob> {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const source = await response.blob();
  if (source.type === 'image/png') return source;

  const bitmap = await createImageBitmap(source);
  const canvas = document.createElement('canvas');
  try {
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('画像を変換できません。');
    context.drawImage(bitmap, 0, 0);
  } finally {
    bitmap.close();
  }
  return new Promise((resolve, reject) =>
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('画像をPNGに変換できません。')), 'image/png'));
}

export async function writeImageToClipboard(url: string): Promise<void> {
  if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
    throw new Error('このブラウザーは画像のクリップボードコピーに対応していません。');
  }
  // Start the write during the click; the file can finish loading afterward.
  const png = loadPng(url);
  void png.catch(() => {});
  await Promise.all([png, navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])]);
}
