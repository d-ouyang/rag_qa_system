#!/usr/bin/env node
/**
 * 生成 GATEWAY_USERS 用的 bcrypt 哈希。
 *
 * 用法：
 *   cd gateway && npm run hash -- 你的密码
 *
 * 为什么会需要这个脚本：GATEWAY_USERS 只接受 bcrypt 哈希、拒绝明文口令
 * （环境变量会被 docker inspect / 进程列表 / 监控采集看到，明文等于公开）。
 * 但手写哈希不现实，所以给一条命令。
 */
const bcrypt = require('bcryptjs');

const password = process.argv[2];

if (!password) {
  console.error('用法：npm run hash -- <密码>');
  console.error('示例：npm run hash -- my-strong-password');
  process.exit(1);
}

if (password.length < 6) {
  console.error('密码太短：至少 6 位（登录接口是系统唯一的入口，别用弱口令）');
  process.exit(1);
}

const ROUNDS = 10; // 10 轮 ≈ 100ms/次，登录体验与抗爆破的平衡点
const hash = bcrypt.hashSync(password, ROUNDS);

console.log('\n把下面这一行填进 gateway/.env 的 GATEWAY_USERS（多个账号用英文逗号分隔）：\n');
console.log(`GATEWAY_USERS=admin:${hash}`);
console.log('\n提示：密码本身不会被保存，遗失只能重新生成哈希。\n');
