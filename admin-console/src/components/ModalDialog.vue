<script setup lang="ts">
/**
 * 通用模态框。见 `@/components/ui.ts` 文件头里「模态框必须做的三件事」。
 */
import { computed } from 'vue'
import { useScrollLock } from '@/components/ui'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    /** 窄一点的表单弹窗 vs 宽一点的确认弹窗 */
    width?: number
    /** 传 false 表示「这个弹窗不该被轻易关掉」（例如一次性展示的临时密码） */
    dismissible?: boolean
  }>(),
  { width: 420, dismissible: true },
)

const emit = defineEmits<{ (e: 'close'): void }>()

useScrollLock(() => props.open)

function close() {
  if (!props.dismissible) return
  emit('close')
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}

const style = computed(() => ({ width: `${props.width}px` }))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="mask" @click.self="close" @keydown="onKeydown" tabindex="-1">
      <div class="dialog card" :style="style" role="dialog" aria-modal="true">
        <header class="head">
          <h3>{{ title }}</h3>
          <button v-if="dismissible" class="btn btn-ghost btn-sm close" @click="close">✕</button>
        </header>
        <div class="body">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="foot">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  background: rgba(6, 9, 15, 0.66);
  display: grid;
  place-items: center;
  z-index: 60;
  padding: 24px;
}
.dialog {
  max-width: 100%;
  max-height: 86vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-pop);
}
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
}
.head h3 {
  font-size: 15px;
  font-weight: 600;
}
.close {
  padding: 2px 6px;
}
.body {
  padding: 18px;
  overflow: auto;
}
.foot {
  padding: 12px 18px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
