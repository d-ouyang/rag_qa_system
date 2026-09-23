# 前端镜像：multi-stage，构建产物只有一堆静态文件，运行只需要 nginx。
#
# 为什么必须 multi-stage：
#   构建阶段要装 vite / typescript / vue-tsc（几百 MB 的 devDependencies），
#   运行阶段只需要 nginx（20MB）。不拆的话，这些构建依赖会白占镜像体积，
#   而且 node_modules 里的原生模块在 alpine 运行时上还有 ABI 风险。

# ---------- 构建阶段 ----------
FROM node:20-alpine AS build

WORKDIR /app

# 先只拷 lock 文件再 npm ci：依赖没变时这一层走缓存。
# ⚠️ 用 `npm ci` 而不是 `npm install` —— ci 严格按 lock 装且不允许改 lock，
# 「构建可复现」这条只能靠它保证；install 会顺手升版本，产物就不可复现了。
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# build = vue-tsc 类型检查 + vite build。类型检查开着，构建期就能拦住类型错误。
RUN npm run build

# ---------- 运行阶段 ----------
FROM nginx:alpine

# 构建产物拷到 nginx 默认根目录
COPY --from=build /app/dist /usr/share/nginx/html
# 覆盖默认 server 配置（默认那份只服务静态文件，没有 /api 反代）
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# 前端是纯静态托管，不需要健康检查钩子；真要探活直接 GET / 即可，
# 由 compose 的 healthcheck 用 wget（alpine 自带）完成。
CMD ["nginx", "-g", "daemon off;"]
