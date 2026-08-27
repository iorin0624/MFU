import { describe, expect, it } from 'vitest';
import { extractSupportedMediaUrls } from './mediaUrls';

describe('extractSupportedMediaUrls', () => {
  it('accepts Instagram, Threads, X and legacy Twitter URLs', () => {
    expect(extractSupportedMediaUrls([
      'https://www.instagram.com/p/example/',
      'https://www.threads.com/@user/post/example',
      'https://x.com/user/status/123',
      'https://twitter.com/user/status/456',
    ].join('\n'))).toHaveLength(4);
  });

  it('ignores unrelated hosts and strips Japanese punctuation', () => {
    expect(extractSupportedMediaUrls('https://example.com/a https://x.com/a/status/1。')).toEqual([
      'https://x.com/a/status/1',
    ]);
  });

  it('deduplicates the same URL', () => {
    expect(extractSupportedMediaUrls('https://instagram.com/p/a https://instagram.com/p/a')).toEqual([
      'https://instagram.com/p/a',
    ]);
  });
});
