export const supportedMediaDomains = [
  'x.com', 'twitter.com', 'instagram.com', 'threads.com', 'threads.net',
];

export function extractSupportedMediaUrls(text: string): string[] {
  const matches = String(text || '').match(/https?:\/\/[^\s<>"']+/gi) || [];
  const values: string[] = [];
  const seen = new Set<string>();
  matches.forEach((raw) => {
    const candidate = raw.replace(/[.,;:!?\)\]\}>、。！？】」』]+$/u, '');
    try {
      const parsed = new URL(candidate);
      const hostname = parsed.hostname.replace(/\.$/, '').toLowerCase();
      const supported = supportedMediaDomains.some(
        (domain) => hostname === domain || hostname.endsWith(`.${domain}`),
      );
      if (supported && !seen.has(candidate)) {
        seen.add(candidate);
        values.push(candidate);
      }
    } catch { /* ignore non-URLs */ }
  });
  return values;
}

export function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}
