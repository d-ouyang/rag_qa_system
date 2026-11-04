<script setup lang="ts">
/**
 * 会话标题：溢出时右侧渐变遮罩；hover 且溢出时连续跑马灯。
 * 参考 office 侧栏：只在「确实溢出」时滚动，不溢出的标题保持静止。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{ text: string; scrolling: boolean }>()

const outer = ref<HTMLElement | null>(null)
const inner = ref<HTMLElement | null>(null)
const overflow = ref(false)

function measure() {
  const box = outer.value
  const label = inner.value
  if (!box || !label) return
  overflow.value = label.scrollWidth > box.clientWidth + 1
}

watch(
  () => [props.text, props.scrolling] as const,
  () => nextTick(measure),
)
onMounted(() => {
  nextTick(measure)
  // 侧栏宽度会变（窗口缩放、详情面板开合），量出来的溢出结论要跟着更新，
  // 否则会出现「明明已经不溢出了，右侧还挂着一块渐变」
  window.addEventListener('resize', measure)
})
onBeforeUnmount(() => window.removeEventListener('resize', measure))
</script>

<template>
  <span ref="outer" class="session-title" :class="{ 'is-overflow': overflow }">
    <span class="session-title-inner" :class="{ marquee: scrolling && overflow }">
      <span ref="inner">{{ text }}</span>
      <span v-if="scrolling && overflow" aria-hidden="true">{{ text }}</span>
    </span>
  </span>
</template>

<style scoped>
.session-title {
  overflow: hidden;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.session-title.is-overflow {
  mask-image: linear-gradient(to right, #000 calc(100% - 26px), transparent);
  -webkit-mask-image: linear-gradient(to right, #000 calc(100% - 26px), transparent);
}
.session-title-inner {
  display: inline-flex;
  gap: 2em;
  white-space: nowrap;
}
.session-title-inner.marquee {
  animation: session-marquee 8s linear infinite;
}
@keyframes session-marquee {
  from { transform: translateX(0); }
  to { transform: translateX(calc(-50% - 1em)); }
}
</style>
