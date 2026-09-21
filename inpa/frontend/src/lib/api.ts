export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message)
  }
}

export async function api<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    method: body ? 'POST' : 'GET',
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json().catch(() => ({})) as { error?: { message?: string } } & T
  if (!response.ok) throw new ApiError(response.status, data.error?.message ?? '通信に失敗しました。')
  return data
}
