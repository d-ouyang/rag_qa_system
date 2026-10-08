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
import './styles/main.css'

// P2-17：Element Plus 暗色变量。组件按需引入（unplugin resolver），
// 但**主题变量**是全局的 —— 管理端是暗色 dashboard，必须挂 dark class，
// 否则 el-select 的面板/下拉会是白底，和整个页面像两个应用。
import 'element-plus/theme-chalk/dark/css-vars.css'
document.documentElement.classList.add('dark')

createApp(App).use(createPinia()).use(router).mount('#app')
