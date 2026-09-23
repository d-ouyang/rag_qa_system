/**
 * `@Public()` 装饰器 —— 把某个路由从「必须登录」里豁免出来。
 *
 * 为什么用装饰器标记「公开」而不是标记「需登录」：
 * 全局守卫默认拒绝（fail-closed），只有显式声明的少数几个路由放行。
 * 反过来做（默认放行、逐个标记需登录）的话，新增接口时忘了标记 = 直接裸奔，
 * 而忘了标记「公开」的后果只是「多校验一次，访问被拒」，能被测试立刻发现。
 */
import { SetMetadata } from '@nestjs/common';

export const IS_PUBLIC_KEY = 'isPublic';

export const Public = () => SetMetadata(IS_PUBLIC_KEY, true);
