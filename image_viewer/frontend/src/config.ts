import type { RuntimeConfig } from './types';

export function readRuntimeConfig(): RuntimeConfig {
  const element = document.getElementById('image-viewer-config');
  if (!element?.textContent) {
    throw new Error('画像ビュアーの設定を読み込めませんでした。');
  }
  return JSON.parse(element.textContent) as RuntimeConfig;
}

export const runtimeConfig = readRuntimeConfig();
