<script setup lang="ts">
/**
 * 提示条。
 *
 * 为什么不用 alert()：它会阻塞渲染、样式无法控制，而且在批量操作时
 * 连续弹几个 alert 是一种很糟的体验。这里走「右上角浮层 + 自动消失」。
 */
import type { ToastItem } from '@/components/ui'

defineProps<{ items: ToastItem[] }>()
const emit = defineEmits<{ (e: 'dismiss', id: number): void }>()
</script>

<template>
  <div class="toasts">
    <div
      v-for="t in items"
      :key="t.id"
      class="toast"
      :class="t.kind"
      @click="emit('dismiss', t.id)"
    >
      {{ t.text }}
    </div>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  top: 16px;
  /* P2-21c：中间顶部。用 left/right:0 + margin auto 居中 ——
     不用 translateX(-50%)（实测 transform 被页面其他规则覆盖成 identity，
     盒子停在左半边；margin auto 居中不受 transform 影响）。 */
  left: 0;
  right: 0;
  margin: 0 auto;
  width: fit-content;
  z-index: 80;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  max-width: 380px;
}
.toast {
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-pop);
  cursor: pointer;
  line-height: 1.5;
}
.toast.ok {
  border-color: rgba(62, 207, 142, 0.4);
  background: var(--success-soft);
}
.toast.warn {
  border-color: rgba(240, 177, 60, 0.4);
  background: var(--warn-soft);
}
.toast.error {
  border-color: rgba(240, 97, 109, 0.45);
  background: var(--danger-soft);
}
</style>
