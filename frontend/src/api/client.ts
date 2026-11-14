/** 统一请求封装：拼后端地址、带上操作人身份、抛网络错误、给页脚留一句可读的说明。 */
import { useSessionStore } from '@/stores/session'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

function identityHeaders(): Record<string, string> {
  const session = useSessionStore()
  // HTTP 头只保证 latin-1，中文操作人/角色统一百分号编码；后端会还原。
  return {
    'X-Operator': encodeURIComponent(session.operator),
    'X-Role': encodeURIComponent(session.role),
  }
}

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...identityHeaders(),
      ...(init?.headers ?? {}),
    },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

/** 读取返回体；后端用 409 表达「模板版本/层级冲突」，要把结构化原因带出来。 */
export async function readPayload(response: Response): Promise<any> {
  const fallback = response.ok ? '数据未更新' : `接口返回 ${response.status}`
  let payload: any = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }
  if (!response.ok) {
    const detail = payload?.detail
    if (typeof detail === 'string') {
      throw new ApiError(detail, response.status, payload)
    }
    if (detail && typeof detail === 'object') {
      throw new ApiError(detail.message || fallback, response.status, detail)
    }
    throw new ApiError(fallback, response.status, payload)
  }
  return payload
}

export class ApiError extends Error {
  status: number
  payload: any

  constructor(message: string, status: number, payload: any) {
    super(message)
    this.status = status
    this.payload = payload
  }
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  return (await readPayload(response)) as T
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await readPayload(response)) as T
}
