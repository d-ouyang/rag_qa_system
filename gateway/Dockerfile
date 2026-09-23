# 网关镜像（NestJS）：构建期编译 TS，运行期只带 dist + 生产依赖。
#
# 为什么运行阶段要重新 `npm ci --omit=dev` 而不是直接复用构建阶段的 node_modules：
# devDependencies 里是 typescript / @nestjs/cli 这类构建工具，进生产镜像既占体积
# 又扩大攻击面。两个阶段的 node_modules 分开装，运行镜像里只有真正被 require 的包。

# ---------- 构建阶段 ----------
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ---------- 运行阶段 ----------
FROM node:20-alpine

WORKDIR /app

# NODE_ENV=production：网关据此调整错误体（不回抛堆栈，见 all-exceptions.filter）
# 与 JWT 密钥的强校验（缺密钥时生产环境直接拒绝启动，而不是用弱默认值）。
ENV NODE_ENV=production \
    TZ=Asia/Shanghai

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/dist ./dist

EXPOSE 3000

# ⚠️ 用 `node dist/main.js` 而不是 `npm run start`（= nest start）。
# nest start 会走 CLI 的编译与监听逻辑：一是启动慢（本机实测完整起到监听约 31s，
# --watch 模式约 95s），二是它在启动时要清 dist 目录，在只读或受限环境下会卡死。
# 直接 node 起产物，启动是秒级的，行为也更可预测。
CMD ["node", "dist/main.js"]
