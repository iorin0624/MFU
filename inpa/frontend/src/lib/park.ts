const PARK_LABELS: Record<string, string> = {
  land: '🏰TDL',
  sea: '🌍TDS',
  both: '🏰TDL・🌍TDS',
  undecided: '未定',
}

export function parkLabel(value: string | null | undefined): string {
  return PARK_LABELS[value ?? ''] ?? '未定'
}

export function parkScopeLabel(value: string | null | undefined): string {
  if (value === 'all') return '全パーク（🏰TDL・🌍TDS）'
  return PARK_LABELS[value ?? ''] ?? String(value ?? '')
}
