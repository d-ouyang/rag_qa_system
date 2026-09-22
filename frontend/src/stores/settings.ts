/** 系统配置 store：设置页读取一次，全局展示 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getSystemHealth, getSystemSettings } from '@/api/system'
import type { SystemSettings } from '@/types'

export const useSettingsStore = defineStore('settings', () => {
  const settings = ref<SystemSettings | null>(null)
  const health = ref<{ status: string; name: string; version: string } | null>(null)
  const loading = ref(false)
  const loadError = ref('')

  /** 完整刷新（设置页挂载/点刷新时调用）：配置 + 健康状态 */
  async function refresh() {
    loading.value = true
    loadError.value = ''
    try {
      const [s, h] = await Promise.all([getSystemSettings(), getSystemHealth()])
      settings.value = s
      health.value = h
    } catch (e) {
      loadError.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  /**
   * 轻量健康探测（边栏状态灯轮询用）。
   * 只打 /system/health，不带 /settings —— settings 会扫描向量库全部元数据，
   * 每次轮询都拉一遍纯属浪费，配置数据在设置页进入时才需要。
   */
  async function pingHealth() {
    try {
      health.value = await getSystemHealth()
    } catch {
      health.value = null
      throw new Error('health check failed')
    }
  }

  return { settings, health, loading, loadError, refresh, pingHealth }
})
