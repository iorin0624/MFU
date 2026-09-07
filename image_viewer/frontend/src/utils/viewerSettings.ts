export const DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS = 300;
export const MIN_WHEEL_NAVIGATION_COOLDOWN_MS = 0;
export const MAX_WHEEL_NAVIGATION_COOLDOWN_MS = 2000;
export const VIEWER_SETTINGS_STORAGE_KEY = 'mfu.imageViewer.vue.viewerSettings';
export const VIEWER_SETTINGS_CHANGED_EVENT = 'mfu:image-viewer-settings-changed';

export function normalizeWheelNavigationCooldown(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS;
  return Math.round(Math.min(
    MAX_WHEEL_NAVIGATION_COOLDOWN_MS,
    Math.max(MIN_WHEEL_NAVIGATION_COOLDOWN_MS, parsed),
  ));
}

export function loadWheelNavigationCooldown(): number {
  try {
    const settings = JSON.parse(localStorage.getItem(VIEWER_SETTINGS_STORAGE_KEY) || '{}');
    return normalizeWheelNavigationCooldown(settings.wheelNavigationCooldownMs);
  } catch {
    return DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS;
  }
}

export function saveWheelNavigationCooldown(value: unknown): number {
  const normalized = normalizeWheelNavigationCooldown(value);
  try {
    const settings = JSON.parse(localStorage.getItem(VIEWER_SETTINGS_STORAGE_KEY) || '{}');
    settings.wheelNavigationCooldownMs = normalized;
    localStorage.setItem(VIEWER_SETTINGS_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Storage is optional; the active viewer still uses the selected value.
  }
  return normalized;
}
