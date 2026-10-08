<script setup lang="ts">
/**
 * P2-15d：主应用顶部的额度提醒横幅。
 *
 * 🔴 **只提醒，不阻断**（用户 2026-10-07 拍板）—— 这条横幅的三个刻意边界：
 *
 * 1. **文案由后端给**（`GET /qa/quota/me` 的 `banner` 字段），
 *    前端只显示、不自己拼 —— `banner_text()` 保证「只描述事实、不劝阻、不威胁」，
 *    且明说「不会限制你继续使用」。前端拼文案 = 两份清单各自漂（13d 的坑）。
 * 2. **拉不到就静默不显示**。横幅不是关键路径：接口挂了、网络断了、
 *    版本旧了没有这个端点 —— 都不该在页面顶上摆一块「加载失败」。
 *    fail-open 在这里是**对的**（它只是个提醒），与鉴权 fail-closed 方向相反。
 * 3. **空字符串 = 不打扰**（未设额度或用量未到阈值），不是「没加载出来」。
 *
 * 不做轮询：横幅数据在登录时拉一次。额度在月中被人改小的场景
 * 下次登录自然可见 —— 为一个提醒横幅加轮询，代价大于收益。
 */
import { onMounted, ref } from 'vue'
import { fetchMyQuota } from '@/api/qa'

const banner = ref('')
const status = ref<'ok' | 'warn' | 'over'>('ok')

onMounted(async () => {
  try {
    const me = await fetchMyQuota()
    banner.value = me.banner ?? ''
    status.value = me.status ?? 'ok'
  } catch {
    // 静默：见文件头第 2 条
  }
})
</script>

<template>
  <div v-if="banner" class="quota-banner" :class="`quota-banner--${status}`" role="status">
    <span class="quota-banner__icon">{{ status === 'over' ? '◉' : '◔' }}</span>
    <span>{{ banner }}</span>
  </div>
</template>

<style scoped>
.quota-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 18px;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
  user-select: none;
}

/* warn 与 over 的配色刻意柔和：这是提醒，不是报警（见 STATUS_COLORS 注释） */
.quota-banner--warn {
  background: color-mix(in srgb, #f0b13c 12%, transparent);
  color: #d9a13c;
}

.quota-banner--over {
  background: color-mix(in srgb, #f0616d 12%, transparent);
  color: #e5737d;
}

.quota-banner__icon {
  font-size: 12px;
}
</style>
