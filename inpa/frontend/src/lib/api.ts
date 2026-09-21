export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message) }
}

function cookie(name: string): string {
  const part = document.cookie.split('; ').find((value) => value.startsWith(`${name}=`))
  return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : ''
}

export async function api<T>(path: string, options: {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'; body?: unknown
} = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const headers: Record<string, string> = {}
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (method !== 'GET') headers['X-CSRF-Token'] = cookie('inpa_csrf')
  const response = await fetch(`/api/v1${path}`, {
    method, credentials: 'same-origin', headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => ({})) as { error?: { message?: string } } & T
  if (!response.ok) throw new ApiError(response.status, data.error?.message ?? '通信に失敗しました。')
  return data
}
