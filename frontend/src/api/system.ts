/** 系统配置接口（对应后端 api/routes/system.py 与 admin_config.py） */
import { get, patchJson } from './http'
import type { SystemSettings } from '@/types'

export const getSystemSettings = () => get<SystemSettings>('/api/v1/system/settings')

export const getSystemHealth = () =>
  get<{ status: string; name: string; version: string }>('/api/v1/system/health')

// --------------------------------------------------------------------------- //
// 运行时配置（P4-2）：业务开关的读/改，superadmin 专属（403 = 前端只读模式）
// ---------------------------------------------------------------------------

/** 一个可热更新配置项（后端 core/config_flags.FLAG_REGISTRY 的投影） */
export interface RuntimeFlag {
  key: string
  label: string
  card: string
  value_type: 'bool' | 'int' | 'float'
  description: string
  value: boolean | number
  default: boolean | number
  min: number | null
  max: number | null
}

export const getRuntimeConfig = () =>
  get<{ can_edit: boolean; flags: RuntimeFlag[] }>('/api/v1/admin/config')

/** 改一项配置：后端校验类型/边界，落库后**下一个请求即生效**（无需重启） */
export const patchRuntimeConfig = (key: string, value: boolean | number) =>
  patchJson<{ key: string; value: boolean | number; value_type: string }>(
    `/api/v1/admin/config/${key}`,
    { value },
  )
