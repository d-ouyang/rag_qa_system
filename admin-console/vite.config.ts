import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 管理端开发服务器 —— 端口固定 **127.0.0.1:5174**（strictPort）。
 *
 * 为什么 5174：5173 是主应用、5180 是个人站。端口这件事一旦允许漂移，
 * 就会出现「以为起的是管理端，其实打开了别的页面的缓存」，而这种异常
 * 不会报错，只会让人对着一个长得差不多的界面操作半天。
 * strictPort=true 是配套的：被占用直接报错退出，而不是悄悄换一个端口。
 *
 * 代理目标与**主应用完全一致**（网关 3000），理由也一致：
 * 本地必须跑「浏览器 → 网关 → 后端」这条完整链路，
 * 直连 8000 会绕过鉴权与限流，于是「本地一切正常、上线就 401」。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_DEV_API_TARGET || 'http://127.0.0.1:3000'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: '127.0.0.1',
      port: 5174,
      strictPort: true,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          headers: { 'Accept-Encoding': 'identity' },
        },
      },
    },
  }
})
