import { Module } from '@nestjs/common';

import { QuotaModule } from '../quota/quota.module';
import { ProxyController } from './proxy.controller';

@Module({
  imports: [QuotaModule],
  controllers: [ProxyController],
})
export class ProxyModule {}
