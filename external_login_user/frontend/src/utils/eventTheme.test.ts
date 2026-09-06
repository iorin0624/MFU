import { describe, expect, it } from 'vitest';
import { DEFAULT_EVENT_THEME_COLOR, eventThemeStyle, normalizeEventThemeColor } from './eventTheme';

describe('event theme', () => {
  it('accepts only a complete hex color and canonicalizes it', () => {
    expect(normalizeEventThemeColor('#a1b2c3')).toBe('#A1B2C3');
    expect(normalizeEventThemeColor('red')).toBe(DEFAULT_EVENT_THEME_COLOR);
    expect(normalizeEventThemeColor('#123')).toBe(DEFAULT_EVENT_THEME_COLOR);
  });

  it('derives readable dark and light theme tokens', () => {
    const style = eventThemeStyle('#F5D000');
    expect(style['--blue']).toBe('#F5D000');
    expect(style['--event-color-soft']).toMatch(/^#[0-9A-F]{6}$/);
    expect(style['--event-on-color']).toBe('#14213A');
  });
});
