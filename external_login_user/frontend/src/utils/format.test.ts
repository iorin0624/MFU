import { describe, expect, it } from 'vitest';
import { formatBytes, formatMoney, membershipLabel } from './format';

describe('participant display formatting', () => {
  it('formats money and bytes for the Japanese UI', () => {
    expect(formatMoney(3500)).toBe('¥3,500');
    expect(formatBytes(1536)).toBe('1.5 KB');
  });

  it('uses an explicit canceled label before the membership state', () => {
    expect(membershipLabel('approved', true)).toBe('キャンセル済み');
    expect(membershipLabel('approved', false)).toBe('参加承認済み');
  });
});
