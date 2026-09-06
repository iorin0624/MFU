import { onBeforeUnmount, watch, type Ref } from 'vue';

export const DEFAULT_EVENT_THEME_COLOR = '#2563EB';

export function normalizeEventThemeColor(value?: string | null): string {
  const color = String(value || '').trim();
  return /^#[0-9a-f]{6}$/i.test(color) ? color.toUpperCase() : DEFAULT_EVENT_THEME_COLOR;
}

function channels(color: string) {
  const value = normalizeEventThemeColor(color).slice(1);
  return [0, 2, 4].map((index) => Number.parseInt(value.slice(index, index + 2), 16));
}

function mix(color: string, target: [number, number, number], amount: number) {
  const source = channels(color);
  return `#${source.map((channel, index) => Math.round(channel + (target[index] - channel) * amount).toString(16).padStart(2, '0')).join('')}`.toUpperCase();
}

function relativeLuminance(color: string) {
  const values = channels(color).map((channel) => {
    const value = channel / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
}

export function eventThemeStyle(value?: string | null): Record<string, string> {
  const color = normalizeEventThemeColor(value);
  return {
    '--blue': color,
    '--blue-dark': mix(color, [0, 0, 0], 0.28),
    '--event-color': color,
    '--event-color-deep': mix(color, [0, 0, 0], 0.34),
    '--event-color-highlight': mix(color, [255, 255, 255], 0.2),
    '--event-color-soft': mix(color, [255, 255, 255], 0.89),
    '--event-color-border': mix(color, [255, 255, 255], 0.62),
    '--event-on-color': relativeLuminance(color) > 0.52 ? '#14213A' : '#FFFFFF',
  };
}

export function useDocumentEventTheme(color: Ref<string | null | undefined>) {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  const original = meta?.content;
  const stop = watch(color, (value) => {
    if (meta) meta.content = normalizeEventThemeColor(value);
  }, { immediate: true });
  onBeforeUnmount(() => {
    stop();
    if (meta && original != null) meta.content = original;
  });
}
