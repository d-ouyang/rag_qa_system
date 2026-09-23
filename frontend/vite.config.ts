import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  /**
   * 开发期代理目标：默认指向 **NestJS 鉴权网关(3000)**，由网关再转发到 FastAPI(8000)。
   *
   * 为什么前端不再直连 8000：
   * 直连就绕过了网关的鉴权与限流，等于「本地开发跑一条链路、线上跑另一条」，
   * 而「本地一切正常、上线就 401」正是这类问题里最难查的一种。
   * 统一走网关后，本地与线上的请求路径完全一致：浏览器 → 网关(校验 token) → 后端。
   *
   * 前端侧仍是同源请求（/api/...），所以浏览器不需要处理跨域。
   * 网关自身配了 5173 的 CORS 白名单，作为「不经代理直连网关」时的双保险。
   */
  const apiTarget = env.VITE_DEV_API_TARGET || 'http://127.0.0.1:3000'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          // 流式问答（NDJSON 逐 token）最怕中间层攒批：
          // 显式要求上游不要压缩（identity），保证 chunk 一到就透传给浏览器，
          // 否则会出现「一次性吐出整段答案」而不是逐字打印。
          headers: { 'Accept-Encoding': 'identity' },
        },
      },
    },
  }
})
