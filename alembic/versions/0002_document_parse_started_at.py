"""P0-3：document 表新增 parse_started_at（解析任务防僵尸）

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23

--------------------------------------------------------------------------
这一列解决的是哪个具体问题
--------------------------------------------------------------------------
P0-3 的解析状态机是：

    pending ──抢任务──> parsing ──成功──> success
                            └──异常──> fail

「抢任务」是一次原子 UPDATE：

    UPDATE document SET status='parsing', attempt_count=attempt_count+1
    WHERE id=? AND status IN ('pending','fail')

这个条件里**没有 parsing**，所以只要一条记录停在 parsing，它就永远
抢不回来了。而 parsing 恰恰是最容易被留下的状态：

    worker 被 `kill -9` / 容器被 OOM killer 干掉 / 部署时进程被替换 ——
    这些情况进程没有任何机会执行清理代码，status 就冻在 parsing。

结果就是「文件明明在磁盘上、MySQL 里却永远是『解析中』」，而且没有任何
自动路径能救回来（只能人工改库）。这正是 P0-3 验收标准第 3 条
（kill Worker 后重启，pending 任务继续被消费）要盖住的场景。

--------------------------------------------------------------------------
为什么加一列，而不是「worker 启动时把所有 parsing 重置为 pending」
--------------------------------------------------------------------------
那个方案更省事（不用迁移），但它有一个在多 worker 下致命的缺陷：
**一个 worker 启动会打断另一个 worker 正在跑的任务**。B 正在解析某个
大文件（比如已经跑了 5 分钟），此时 A 启动，把 B 的任务重置成 pending，
另一个 worker 抢走后两边同时往 Chroma 写同一份内容 —— 结果不算错
（写入前会先按 doc_id 删，最后写的胜出），但白烧一遍 CPU 和嵌入算力，
而且日志上看不出异常。

有了「解析开始时间」这一列，判断就变成**按单条记录的时间戳**而不是
全局重置：只有「正在 parsing 且已经 parsing 太久」的才算孤儿。
多 worker 下天然正确，不需要任何跨进程协调。

--------------------------------------------------------------------------
为什么是 NULL 而不是 DEFAULT CURRENT_TIMESTAMP
--------------------------------------------------------------------------
parse_started_at 的语义是「**当前**是否有解析在跑」。对于 pending /
success / fail 的记录，这个值没有意义，必须是 NULL。
若给个 DEFAULT CURRENT_TIMESTAMP，所有历史行会拿到一个假的开始时间，
`IS NOT NULL` 这个判断就再也区分不出「真在跑」和「从没跑过」。

--------------------------------------------------------------------------
为什么是 DATETIME(6) 而不是秒精度
--------------------------------------------------------------------------
孤儿判定是 `parse_started_at < NOW(6) - INTERVAL stale SECOND`。
测试里会把 stale 设成很小的值（几秒）来验证这条路径；
秒精度下亚秒差被截断，边界用例会时灵时不灵。与
0001 里 `last_active_at` 用 DATETIME(6) 是同一个理由。

--------------------------------------------------------------------------
为什么 downgrade 敢直接 drop
--------------------------------------------------------------------------
这一列是纯新增的可空列，删掉不丢任何不可再生信息（它记录的是「上次解析
何时开始」，属于运行态观测值而非业务资产）。回退后唯一的后果是
「parsing 僵尸记录需要人工处理」—— 那是回退到 P0-3 之前，本来就没有
自动处理能力。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN_COMMENT = "本次解析开始时间；仅 status=parsing 时非空，用于识别被 kill 留下的僵尸任务"


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column("parse_started_at", mysql.DATETIME(fsp=6), nullable=True, comment=_COLUMN_COMMENT),
    )


def downgrade() -> None:
    op.drop_column("document", "parse_started_at")
