/**
 * 管理端入口。
 *
 * 与主前端同一套三件套顺序（createApp → pinia → router），
 * 唯一的差别是这两个应用各自有独立的 Pinia 实例与独立的 localStorage key，
 * 所以它们可以在同一个浏览器里并存而不互相顶掉登录态。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
// P2-19：⚠️ 导入顺序有讲究 —— EP 的 dark 变量必须**先于** main.css：
//   我们在 main.css 的 html.dark 里把 --el-* 对齐到项目色板，
//   同特异性下「后 import 的胜出」，顺序反了 EP 会把对齐覆盖回去
//   （实测症状：el-select 触发框/分页与页面出现明显色差）。
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/main.css'

// P2-17：管理端是暗色 dashboard，必须挂 dark class，
// 否则 el-select 的面板/下拉会是白底，和整个页面像两个应用。
document.documentElement.classList.add('dark')

createApp(App).use(createPinia()).use(router).mount('#app')
