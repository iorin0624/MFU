export function formatDateTime(value?: string | null): string {
  if (!value) return '日時未定';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric', month: 'long', day: 'numeric', weekday: 'short',
    hour: '2-digit', minute: '2-digit',
  }).format(date);
}

export function formatMoney(value?: number | null): string {
  if (value == null) return '無料・未設定';
  return `¥${Number(value).toLocaleString('ja-JP')}`;
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '-';
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 ** 2).toFixed(1)} MB`;
}

export function membershipLabel(status?: string, canceled = false): string {
  if (canceled) return 'キャンセル済み';
  return ({ approved: '参加承認済み', pending: '承認待ち', rejected: '参加不可' } as Record<string, string>)[status || ''] || '状態確認中';
}
