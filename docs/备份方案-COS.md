# 备份方案：整站备份到 COS（P2-10b 手把手）

> 服务器：腾讯云轻量 4C4G 上海（42.192.111.82）。备份脚本与定时单元已在仓库：
> `scripts/backup_to_cos.sh`、`scripts/systemd/rag-backup.{service,timer}`。
> 本文档回答三件事：控制台怎么点、服务器怎么配、怎么验收。

## 1. 备什么、不备什么（真相源分工决定）

| 对象 | 方式 | 理由 |
|---|---|---|
| MySQL（`mysql_data` 卷） | `mysqldump --single-transaction` 逻辑导出 | 命名卷不直接拷；单事务不锁表 |
| `upload/` | tar | 原始文件，解析链路源头 |
| `chroma_data/` | tar | 向量库；虽可从 upload 重灌，直接备体积小 |
| `redis_data` 卷 | ❌ 不备 | AOF 非强持久，任务真相在 MySQL `document.status`（P1-5 定调） |
| `models/` | ❌ 不备 | 可重新下载，约 2GB 纯占空间 |

保留策略放在 **COS 生命周期规则**（云端自动删），不在脚本里删——脚本没跑、密钥换了都不影响旧备份。

## 2. 控制台操作（三步）

### 第 1 步：轻量服务器「对象存储」页签 → 点「授权」

轻量控制台（Hermes Agent-kqQR）顶部「对象存储」标签第一次进去会提示
「该服务需创建角色」→ 点**授权**。这是给轻量服务器创建 COS 服务角色，
授权后这个页签就能直接建桶、管桶（它操作的就是 COS 控制台里同一批桶）。

### 第 2 步：建桶（就在这个页签里建，或去 COS 控制台）

| 项 | 填什么 | 为什么 |
|---|---|---|
| 名称 | `qa-backup`（系统自动补全为 `qa-backup-1259425324`） | 桶名全服唯一；建完的完整桶名填进 `.env` 的 `COS_BUCKET` |
| 地域 | **上海** | ⚠️ 必须与服务器同地域：内网传输免外网下行流量费，上传本身免费 |
| 访问权限 | **私有读写** | 备份含用户数据，绝不能公共读 |
| 多 AZ / 版本控制 | 都不开 | 备份已按天分层，开版本控制双倍存储费没有增益 |
| CDN 加速 | 不开 | 用不到 |

### 第 3 步：建 API 密钥（CAM 子用户，**不要用主账号密钥**）

控制台搜索「访问管理 CAM」→ 用户 → 新建用户：

1. 用户类型：**自定义创建** → 可访问资源并接收消息（编程访问）；
2. 用户名：`qa-backup-bot`；访问方式只勾 **编程访问**（不勾控制台登录）；
3. 权限：先不选任何预设策略，建完用户后**单独写一条策略**绑定（最小权限）：

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["cos:PutObject", "cos:GetObject", "cos:ListBucket", "cos:AbortMultipartUpload"],
      "resource": ["qcs::cos:ap-shanghai:uid/1259425324:qa-backup-1259425324/*",
                    "qcs::cos:ap-shanghai:uid/1259425324:qa-backup-1259425324/"]
    }
  ]
}
```

4. 建完**立刻**下载或复制 `SecretId` / `SecretKey`（只显示这一次）。

### 第 4 步：桶上配生命周期规则（保留策略）

COS 控制台 → 该桶 → 基础配置 → 生命周期 → 添加规则：
- 作用范围：`qa-backup/` 前缀；
- 现行版本：创建 **30 天**后删除（保留天数自己拍板，30 天=每月约 30 份增量）。

> 成本量级：当前数据规模（dump 几 MB + upload/向量打包）每天一份、留 30 天，
> 标准存储月费用预计 **1 元以内**；按量计费不用买资源包。

## 3. 服务器操作（四步）

```bash
# 1) 装 coscli（腾讯云官方 CLI）
curl -fsSL https://github.com/tencentyun/coscli/releases/latest/download/coscli-linux -o /usr/local/bin/coscli
chmod +x /usr/local/bin/coscli

# 2) .env 追加四个变量（密钥不进 git）
cat >> /opt/qa/.env << 'EOF'
COS_BUCKET=qa-backup-1259425324
COS_REGION=ap-shanghai
COS_SECRET_ID=<SecretId>
COS_SECRET_KEY=<SecretKey>
EOF
chmod 600 /opt/qa/.env   # 本来就该是

# 3) 手动跑一轮（CI/CD 已把脚本带到服务器；等不及可先 git pull）
cd /opt/qa && sudo bash scripts/backup_to_cos.sh

# 4) 装定时器（每天 03:20，错过开机补跑）
sudo cp scripts/systemd/rag-backup.service scripts/systemd/rag-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rag-backup.timer
systemctl list-timers rag-backup.timer   # 看下次触发时间
```

⚠️ 脚本默认走**内网 endpoint**（`cos.ap-shanghai.internal.tencentcos.com`）——
只在服务器上可达；本地调试加 `--public`。

## 4. 验收清单（10b 收官判据）

- [ ] 手动跑 `backup_to_cos.sh`，输出 `✅ 完成 | mysql=xMB data=yMB`
- [ ] `coscli ls cos://$COS_BUCKET/qa-backup/2026/10/11/` 见两个文件
- [ ] 桶里下载 `mysql-*.sql.gz`，`zcat | head -30` 能看到 `CREATE TABLE`（dump 没坏）
- [ ] `systemctl list-timers` 显示下次 03:20 触发；`journalctl -u rag-backup` 无报错
- [ ] 第二天桶里自动出现新日期前缀（timer 真的在跑）
- [ ] 生命周期规则生效（30 天后最早一份自动消失，届时抽查）

## 5. 故障排查速查

| 症状 | 先查 |
|---|---|
| 脚本报 `COS_BUCKET 未配置` | `.env` 没加或没在 /opt/qa 下跑 |
| 403 / AccessDenied | CAM 策略 resource 的 APPID/桶名没对上；或 SecretKey 复制带空格 |
| 内网 endpoint 连不通 | 桶地域不是 ap-shanghai，或解析不到 internal 域名（临时用 `--public` 验证） |
| dump 为空 | `docker compose --profile full exec mysql env \| grep MYSQL` 看容器内变量 |
| timer 没触发 | `systemctl status rag-backup.timer`；日期是否被 Persistent 补跑机制覆盖 |
