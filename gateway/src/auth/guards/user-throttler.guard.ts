/**
 * 限流守卫 —— 按「登录用户」限流，未登录时退化为按 IP。
 *
 * 为什么不能只用默认的「按 IP」：
 * 家庭/公司/校园网出口 IP 是共享的（NAT），按 IP 限流会误伤同网段的其他用户；
 * 更麻烦的是这会让「谁在被限流」变得无从解释。登录用户之后按 userId 计数，
 * 语义明确、用户可解释（「你这个账号请求太频繁」而不是「你们整栋楼都被限了」）。
 *
 * 为什么要限流：LLM 额度是**真金白银**。没有网关之前，任何拿到地址的人
 * 都能直接刷爆你的硅基流动余额。限流是这条资金链路的第一道闸。
 *
 * 存储：默认内存（单实例够用）。多实例部署需要换共享存储
 * （@nestjs/throttler 官方提供 Redis storage），否则每个副本各算各的，
 * 实际限额被放大 N 倍 —— 这一点写在 README 的「已知边界」里。
 */
import { Injectable } from '@nestjs/common';
import { ThrottlerGuard } from '@nestjs/throttler';
import { AuthenticatedUser } from '../strategies/jwt.strategy';

@Injectable()
export class UserThrottlerGuard extends ThrottlerGuard {
  protected async getTracker(req: Record<string, unknown>): Promise<string> {
    const user = req.user as AuthenticatedUser | undefined;
    if (user?.userId) return `user:${user.userId}`;
    // req.ip 可能是 IPv6 映射形式（::ffff:1.2.3.4），归一到 IPv4 避免同一客户端被算成两个键
    const raw = (req.ip as string) ?? 'unknown';
    return `ip:${raw.replace(/^::ffff:/, '')}`;
  }
}
