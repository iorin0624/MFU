export function isInAppBrowser(): boolean {
  if (typeof navigator === 'undefined') return false;
  const userAgent = navigator.userAgent || '';
  const referrer = typeof document === 'undefined' ? '' : document.referrer || '';
  return /(Line\/|Instagram|Twitter|FBAN|FBAV)/i.test(userAgent)
    || /^https:\/\/t\.co\//i.test(referrer);
}
