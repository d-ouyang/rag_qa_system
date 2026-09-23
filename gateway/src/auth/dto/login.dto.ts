import { IsNotEmpty, IsString, MaxLength } from 'class-validator';

/**
 * 登录请求体。
 *
 * 每条校验都要写中文 message：class-validator 默认吐英文
 * （"password must be a string"），而这条 message 会直接展示在登录页上。
 * 漏一个 message，用户就会在中文界面里看到一句英文 —— 这类细节漏了很难自查，
 * 所以约定是「本项目所有校验器都带 message」。
 *
 * 校验的性质是「结构校验」而不是「安全防线」：
 * 防爆破靠限流（@Throttle）+ bcrypt 的慢哈希，不靠长度限制。
 * 长度上限的价值在于挡住「把 1MB 垃圾塞进 password 字段」这类廉价攻击
 * —— 否则 bcrypt 会对超大输入做一次昂贵的哈希，等于给攻击者一个 CPU 放大器。
 */
export class LoginDto {
  @IsString({ message: '用户名格式不正确' })
  @IsNotEmpty({ message: '用户名不能为空' })
  @MaxLength(64, { message: '用户名不能超过 64 个字符' })
  username!: string;

  @IsString({ message: '密码格式不正确' })
  @IsNotEmpty({ message: '密码不能为空' })
  @MaxLength(128, { message: '密码不能超过 128 个字符' })
  password!: string;
}
