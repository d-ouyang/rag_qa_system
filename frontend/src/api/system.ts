/** 系统配置接口（对应后端 api/routes/system.py） */
import { get } from './http'
import type { SystemSettings } from '@/types'

export const getSystemSettings = () => get<SystemSettings>('/api/v1/system/settings')

export const getSystemHealth = () =>
  get<{ status: string; name: string; version: string }>('/api/v1/system/health')
