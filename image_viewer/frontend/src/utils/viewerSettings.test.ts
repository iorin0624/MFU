import { beforeEach, describe, expect, it } from 'vitest';
import {
  DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS,
  loadWheelNavigationCooldown,
  normalizeWheelNavigationCooldown,
  saveWheelNavigationCooldown,
  VIEWER_SETTINGS_STORAGE_KEY,
} from './viewerSettings';

describe('viewer settings', () => {
  beforeEach(() => localStorage.clear());

  it('uses 300ms when no setting exists', () => {
    expect(loadWheelNavigationCooldown()).toBe(DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS);
  });

  it('saves and restores the wheel cooldown', () => {
    expect(saveWheelNavigationCooldown(450)).toBe(450);
    expect(loadWheelNavigationCooldown()).toBe(450);
  });

  it('keeps the value in the supported range', () => {
    expect(normalizeWheelNavigationCooldown(-1)).toBe(0);
    expect(normalizeWheelNavigationCooldown(5000)).toBe(2000);
    localStorage.setItem(VIEWER_SETTINGS_STORAGE_KEY, '{broken');
    expect(loadWheelNavigationCooldown()).toBe(DEFAULT_WHEEL_NAVIGATION_COOLDOWN_MS);
  });
});
