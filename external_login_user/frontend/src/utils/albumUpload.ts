export const MAX_UPLOAD_FILES = 80;
export const MAX_UPLOAD_BYTES = 350 * 1024 * 1024;
export function buildUploadBatches<T extends { size: number }>(files: T[]): T[][] {
  const result: T[][] = [];
  let current: T[] = [];
  let bytes = 0;
  for (const file of files) {
    if (current.length && (current.length >= MAX_UPLOAD_FILES || bytes + file.size > MAX_UPLOAD_BYTES)) {
      result.push(current); current = []; bytes = 0;
    }
    current.push(file); bytes += file.size;
  }
  if (current.length) result.push(current);
  return result;
}
export function uploadPercent(completed: number, current: number, total: number) {
  return total > 0 ? Math.min(99, Math.max(0, Math.floor((completed + current) / total * 100))) : 0;
}
export const CHILD_NAME_TEMPLATES = ['', '【構図】', '【オフショ】', '【動画】', '【加工回し】'];
export function childTemplateMode(template: string): 'normal' | 'movie' | 'process' {
  return template === '【動画】' ? 'movie' : template === '【加工回し】' ? 'process' : 'normal';
}
