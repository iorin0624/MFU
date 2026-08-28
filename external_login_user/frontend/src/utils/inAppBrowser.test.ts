import { afterEach, describe, expect, it } from 'vitest';
import { isInAppBrowser } from './inAppBrowser';

const normalUserAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1';

function setUserAgent(value: string) {
  Object.defineProperty(window.navigator, 'userAgent', { configurable: true, value });
}

afterEach(() => setUserAgent(normalUserAgent));

describe('isInAppBrowser', () => {
  it('detects the LINE in-app browser', () => {
    setUserAgent(`${normalUserAgent} Line/15.14.0`);
    expect(isInAppBrowser()).toBe(true);
  });

  it('does not block a normal Safari browser', () => {
    setUserAgent(normalUserAgent);
    expect(isInAppBrowser()).toBe(false);
  });
});
