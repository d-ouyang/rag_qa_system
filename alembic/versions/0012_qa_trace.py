"""qa_trace：问答流程轨迹的分析镜像（v4.0.0 P4-5 二期）

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16

--------------------------------------------------------------------------
这张表为什么存在
--------------------------------------------------------------------------
P4-5 一期把每轮问答的完整轨迹落进了 `chat_message.turn_meta.qa_trace`
（JSON 列，面板数据源）。但 JSON 列没法按 decision / intent / 日期建索引，
「近 30 天多少轮零召回、拒答差多少分、哪种意图最多」这类**跨会话**问题
只能全表扫 JSON —— 查不动。

本表是同一份数据的**查询优化镜像**：
    · 列 = 过滤 / 聚合维度 + 详情渲染所需标量；
    · candidates（候选清单）存 JSON，只给详情抽屉渲染，不建索引；
    · 写入点唯一（core/rag_chain.py 三处写记忆后各镜像一次），fail-open
      —— 分析库挂了不能挡问答，症状是溯源页缺数据而不是问答不可用。

不建 message 外键：会话快照（Redis / MySQL session store）不回传
message id，自包含才能让溯源页不依赖会话存储的实现细节。
session_id / owner_id 同理不建 FK —— 会话与账号都可能被清理，轨迹留存。

列注释与 core/schema.py 的 qa_trace_table **逐字相同**
（compare_metadata 守着，diff 必须为 0）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

TABLE_KW = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def upgrade() -> None:
    op.create_table(
        "qa_trace",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True,
                  comment="自增主键；按时间近似有序，溯源页默认按 id 倒序 = 按时间倒序"),
        sa.Column("session_id", sa.String(64), nullable=False,
                  comment="会话 id；与 chat_message.session_id 同源。不建 FK——会话可能被清理，轨迹留存"),
        sa.Column("owner_id", sa.Integer(), nullable=True,
                  comment="提问者 user.id；可空——内部调用可能无归属。不建 FK 同 session_id"),
        sa.Column("decision", sa.String(20), nullable=False,
                  comment="终端状态四选一：cache_hit/chitchat/zero_recall/rag_qa（core/qa_flow.py 唯一定义处）"),
        sa.Column("intent", sa.String(40), nullable=False,
                  comment="意图（IntentType 值）；缓存命中轮取被缓存那次回答的意图"),
        sa.Column("route", sa.String(40), nullable=False,
                  comment="路由：rag_qa | chitchat"),
        sa.Column("intent_source", sa.String(20), nullable=False, server_default="",
                  comment="意图判定来源：rule/llm/cache；空串=未知（老数据）"),
        sa.Column("question", sa.Text(), nullable=False,
                  comment="用户原始问题（缓存命中轮 = 本轮实际输入，不一定是当初被缓存的那句）"),
        sa.Column("standalone_question", sa.Text(), nullable=True,
                  comment="改写后的独立问句；闲聊轮与缓存命中轮为 NULL"),
        sa.Column("rewritten_query", sa.Text(), nullable=True,
                  comment="Query 改写产物；未开启改写或无变化时为 NULL"),
        sa.Column("vector_query", sa.Text(), nullable=True,
                  comment="HyDE 假设答案（向量召回用它）；未启用 HyDE 时为 NULL"),
        sa.Column("candidates", sa.JSON(), nullable=True,
                  comment="重排前候选清单（含来源/分数）；闲聊与缓存命中轮为 NULL。JSON 列不建索引，仅详情渲染"),
        sa.Column("threshold", mysql.DOUBLE(), nullable=True,
                  comment="重排分数阈值；未重排/未跑检索为 NULL。与 top1_before_filter 配合算「差多少分」"),
        sa.Column("top1_before_filter", mysql.DOUBLE(), nullable=True,
                  comment="阈值过滤前的最高分；未跑检索为 NULL。min_top1_gap 过滤 = threshold - 本列 >= X"),
        sa.Column("filtered_count", sa.Integer(), nullable=True,
                  comment="被阈值过滤掉的候选数；未跑检索为 NULL"),
        sa.Column("docs_returned", sa.Integer(), nullable=False, server_default="0",
                  comment="过滤后返回的文档数；闲聊=0"),
        sa.Column("sources_count", sa.Integer(), nullable=False, server_default="0",
                  comment="答案引用的来源数（sources 列表长度）"),
        sa.Column("bm25_hits", sa.Integer(), nullable=True,
                  comment="BM25 关键词召回命中数；混合检索未开启为 NULL（「没开」≠「开了没命中」）"),
        sa.Column("bm25_only", sa.Integer(), nullable=True,
                  comment="仅 BM25 命中、向量未命中的候选数；混合未开启为 NULL"),
        sa.Column("rrf_top", sa.String(64), nullable=True,
                  comment="RRF 融合后排首位的 chunk_id（如 \"822:0\"）；混合未开启为 NULL。⚠️ 是 ID 不是分数——首版误建 DOUBLE 导致镜像必失败（P4-5c 修订）"),
        sa.Column("hyde_used", sa.Boolean(), nullable=False, server_default=sa.text("0"),
                  comment="本轮向量召回是否用了 HyDE 假设答案"),
        sa.Column("elapsed_ms", mysql.DOUBLE(), nullable=False, server_default="0",
                  comment="整轮耗时（毫秒）；缓存命中轮通常接近 0"),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.text("0"),
                  comment="是否拒答（decision=zero_recall 的派生布尔，冗余存储换过滤便利）"),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False,
                  comment="落表时间（now_db() 口径）；日期过滤与「按天聚合」都查它"),
        sa.Index("ix_qa_trace_decision", "decision"),
        sa.Index("ix_qa_trace_intent", "intent"),
        sa.Index("ix_qa_trace_session", "session_id"),
        sa.Index("ix_qa_trace_created", "created_at"),
        comment="问答流程轨迹（P4-5 二期）：turn_meta.qa_trace 的分析镜像，跨会话按状态/意图/日期筛选；写入点唯一在 rag_chain，fail-open",
        **TABLE_KW,
    )


def downgrade() -> None:
    op.drop_table("qa_trace")
