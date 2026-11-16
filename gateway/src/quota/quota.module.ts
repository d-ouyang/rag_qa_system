/**
 * 额度预检模块（v4.1.0-p2.1）——见 quota.service.ts 头注释。
 */
import { Module } from '@nestjs/common';

import { QuotaService } from './quota.service';

@Module({
  providers: [QuotaService],
  exports: [QuotaService],
})
export class QuotaModule {}
