<script setup lang="ts">
/**
 * P2-18：管理端统一自定义下拉。
 *
 * 为什么不用原生 `<select>`（这是两个 UI 问题的根因，不是样式没调好）：
 * 1. **箭头贴右边**：原生 select 的箭头是浏览器画的，`padding-right` 只影响
 *    文字、动不了那个箭头 —— 只有 `appearance: none` + 自绘箭头才能控制位置。
 * 2. **展开的选项面板遮挡触发框**：原生 select 的下拉面板是浏览器原生 UI
 *    （样式、位置都由浏览器决定），前端完全控制不了。
 *    自绘面板是普通 DOM 元素，永远在触发框**下方**展开，不会盖住它。
 *
 * 实现要点：
 * - 面板 `Teleport` 到 body + `position: fixed`：不受任何祖先 `overflow: hidden`
 *   裁剪（管理端表格容器有横向滚动，不放 body 会被裁掉）。
 * - 位置按触发框 `getBoundingClientRect()` 实时计算，滚动/resize 时关闭
 *   （重算比跟随简单且够用 —— 打开状态下用户几乎不会滚动）。
 * - `modelValue` 支持 `string | number`：内部比较统一走 `String()`，
 *   change 事件回传的是**原始类型**的 value（number 的 id 不会变成 string）。
 * - 键盘可达：触发框是 `<button>`，Enter/Space 打开、Esc 关闭。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

export interface AppSelectOption {
  /** `null` 有正当用途：「全部/不限/—」这类空选项（用空串会与真实空串冲突）。 */
  value: string | number | null
  label: string
}

const props = withDefaults(
  defineProps<{
    modelValue: string | number | null | undefined
    options: AppSelectOption[]
    placeholder?: string
    disabled?: boolean
    /** 触发框内是否右对齐一个自绘箭头（默认开）。 */
    arrow?: boolean
  }>(),
  { placeholder: '请选择', disabled: false, arrow: true },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | number | null): void
  (e: 'change', value: string | number | null): void
}>()

const open = ref(false)
const btnRef = ref<HTMLButtonElement | null>(null)
const panelStyle = ref<Record<string, string>>({})

const current = computed(
  () => props.options.find((o) => String(o.value) === String(props.modelValue ?? '')),
)
const currentLabel = computed(() => current.value?.label ?? '')

function place() {
  const el = btnRef.value
  if (!el) return
  const r = el.getBoundingClientRect()
  panelStyle.value = {
    left: `${r.left}px`,
    top: `${r.bottom + 4}px`,
    minWidth: `${r.width}px`,
    maxHeight: `${Math.min(280, window.innerHeight - r.bottom - 12)}px`,
  }
}

async function toggle() {
  if (props.disabled) return
  if (open.value) {
    open.value = false
    return
  }
  open.value = true
  await nextTick(() => place())
}

function choose(opt: AppSelectOption) {
  open.value = false
  if (String(opt.value) === String(props.modelValue ?? '')) return
  emit('update:modelValue', opt.value)
  emit('change', opt.value)
}

defineExpose({ close: () => (open.value = false) })

function onDocClick(e: MouseEvent) {
  const t = e.target as HTMLElement
  if (btnRef.value?.contains(t)) return
  if (t.closest('.app-select-panel')) return
  open.value = false
}

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') open.value = false
}

function onScrollLike() {
  if (open.value) open.value = false
}

watch(open, (v) => {
  if (v) {
    document.addEventListener('click', onDocClick, true)
    document.addEventListener('keydown', onKey)
    window.addEventListener('scroll', onScrollLike, true)
    window.addEventListener('resize', onScrollLike)
  } else {
    document.removeEventListener('click', onDocClick, true)
    document.removeEventListener('keydown', onKey)
    window.removeEventListener('scroll', onScrollLike, true)
    window.removeEventListener('resize', onScrollLike)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick, true)
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('scroll', onScrollLike, true)
  window.removeEventListener('resize', onScrollLike)
})
</script>

<template>
  <button
    ref="btnRef"
    type="button"
    class="app-select"
    :class="{ open, 'has-value': currentLabel !== '' }"
    :disabled="disabled"
    :aria-expanded="open"
    aria-haspopup="listbox"
    @click="toggle"
  >
    <span class="app-select__label" :class="{ placeholder: !currentLabel }">
      {{ currentLabel || placeholder }}
    </span>
    <span v-if="arrow" class="app-select__arrow" aria-hidden="true">
      <svg viewBox="0 0 12 12" width="11" height="11">
        <path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </span>
  </button>

  <Teleport to="body">
    <div
      v-if="open"
      class="app-select-panel"
      :style="panelStyle"
      role="listbox"
    >
      <button
        v-for="opt in options"
        :key="String(opt.value)"
        type="button"
        class="app-select__option"
        :class="{ selected: String(opt.value) === String(modelValue ?? '') }"
        role="option"
        :aria-selected="String(opt.value) === String(modelValue ?? '')"
        @click="choose(opt)"
      >
        {{ opt.label }}
      </button>
      <div v-if="!options.length" class="app-select__empty">暂无可选项</div>
    </div>
  </Teleport>
</template>

<style>
/* 全局样式（面板 Teleport 到 body，scoped 够不到）。
   ⚠️ 箭头与文字间距在这里定死：右侧留 26px（自绘箭头 11px + 呼吸空隙），
   这就是「箭头不再贴着右边框」的那一行。 */
.app-select {
  position: relative; /* 箭头 absolute 相对它定位 */
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 26px 8px 12px; /* ← 右侧 26px 是给自绘箭头的呼吸位（原来原生箭头贴边） */
  border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
  background: var(--bg-app);
  color: var(--text-1);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  outline: none;
}

.app-select:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-soft);
}

.app-select:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.app-select__label.placeholder {
  color: var(--text-3);
}

.app-select__arrow {
  position: absolute;
  right: 9px;
  color: var(--text-3);
  display: inline-flex;
  transition: transform 0.15s ease;
}

.app-select.open .app-select__arrow {
  transform: rotate(180deg);
}

.app-select-panel {
  position: fixed;
  z-index: 80;
  background: var(--surface, #171b24);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  overflow-y: auto;
  padding: 4px;
  display: flex;
  flex-direction: column;
}

.app-select__option {
  border: none;
  background: none;
  color: var(--text-1);
  text-align: left;
  font-size: 13px;
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}

.app-select__option:hover {
  background: var(--bg-hover);
}

.app-select__option.selected {
  color: var(--primary);
  font-weight: 600;
}

.app-select__empty {
  padding: 10px;
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
}
</style>
