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

createApp(App).use(createPinia()).use(router).mount('#app')
