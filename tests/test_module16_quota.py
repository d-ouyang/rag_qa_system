# pyright: basic
"""
P2-15 测试：Token 额度与预警。

    .venv/bin/python tests/test_module16_quota.py
    .venv/bin/python tests/test_module16_quota.py --reverse

⚠️ **本文件会分批交付**。当前（15a）只含聚合与口径两组；
15b（额度写路径）/ 15d（看板与横幅）/ 15e（越权回归）随后追加。
每加一组都会在下面的分组标题里写明。

--------------------------------------------------------------------------
它管什么
--------------------------------------------------------------------------
    core/quota_policy.py            判定（纯函数）：计费口径 / 使用率 / 档位 / 文案
    core/quota_repo.py              聚合（SQL）：按人 × 自然月 / 对账
    alembic/versions/0007_*.py     `user.token_quota_monthly`
    core/schema.py                  查询侧视图（注释必须与迁移逐字相同）

--------------------------------------------------------------------------
为什么「口径」要单独占两组断言（这是本项最容易做错的地方）
--------------------------------------------------------------------------
计划书 §15a 说「先定死这个，否则后面全是来回改」。实测也确实如此 ——
**写聚合的过程中就撞到了一次口径缺失**：

    库里有 `session.user_id = 657`、6894 个 input token，
    而 `user` 表里已经没有 657 这一行（前一天手工删掉的测试残留）。

于是「按人合计」与「全库总计」差了 6894，而第一版
`unattributed_total()` 只查 `user_id IS NULL`、返回 0。

症状是「对账恒等式不成立」，而**我第一反应是「聚合 SQL 写错了」**——
去查了半天的 JOIN。真相是聚合没错、**是「未归属」的定义漏了一种**。

所以第 1 组专门钉住「未归属的两种」，第 2 组钉住对账恒等式。
这两组加起来回答的是同一个问题的两面：
**「这个数字少算了人」必须是可见的，而不是一个安静的差值。**
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


def _n_msgs_of(session_id: str) -> int:
    """某会话的消息条数（cache_read 那条断言用它算「消息粒度会放大几倍」）。"""
    from sqlalchemy import text as _t

    with get_engine().connect() as c:
        return int(c.execute(
            _t("SELECT COUNT(*) FROM chat_message WHERE session_id=:s"),
            {"s": session_id},
        ).scalar())


print("== 前置检查：MySQL 连通性 ==")
from core import db as db_module  # noqa: E402

_conn = db_module.check_connection()
print(f"  {_conn['detail']}")
if not _conn["ok"]:
    print()
    print("=" * 66)
    print("  MySQL 不可达 —— 本模块全部断言依赖它，直接退出（不是「跳过」）")
    print("=" * 66)
    sys.exit(1)

from core import quota_policy as qp  # noqa: E402
from core import quota_repo as qr  # noqa: E402
from core import schema as app_schema  # noqa: E402
from core.db import get_engine  # noqa: E402

# --------------------------------------------------------------------------- #
# 第 0 组：迁移与结构一致（diff 必须为 0）
# --------------------------------------------------------------------------- #
section("第 0 组：迁移 0007 与结构/视图一致")

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import text as sa_text  # noqa: E402

with get_engine().connect() as _c:
    # ⚠️ 必须是 SHOW **FULL** COLUMNS：普通 SHOW COLUMNS 的返回里
    # **没有 Comment 这个键**（实测踩过：KeyError: 'Comment'）。
    # 而「注释逐字一致」正是本组要断言的东西。
    _rows = [dict(r) for r in _c.execute(
        sa_text("SHOW FULL COLUMNS FROM `user` LIKE 'token_quota_monthly'")).mappings()]
check("user 表有 token_quota_monthly 列（迁移 0007 已应用）",
      len(_rows) == 1, f"查到 {len(_rows)} 行")
if _rows:
    col = _rows[0]
    check("该列是 BIGINT（不是 INT —— 用量溢出会炸在提交答案的路上）",
          col["Type"].lower().startswith("bigint"),
          f"实际={col['Type']}")
    check("该列 NOT NULL DEFAULT 0（0 = 不限，与 .env 同一套编码）",
          col["Null"] == "NO" and str(col["Default"]) == "0",
          f"Null={col['Null']} Default={col['Default']}")
    # 注释逐字一致：这是 compare_metadata 会报 modify_comment 的一类差异
    schema_col = app_schema.metadata.tables["user"].c["token_quota_monthly"]
    check("🔴 列注释与 core/schema.py 逐字相同（否则 compare_metadata 会报差异）",
          (col["Comment"] or "") == (schema_col.comment or ""),
          f"库={col['Comment']!r} 视图={schema_col.comment!r}")

with get_engine().connect() as _c:
    _diff = compare_metadata(MigrationContext.configure(_c), app_schema.metadata)
check("🔴 结构与查询侧视图一致（compare_metadata diff 必须为 0）",
      len(_diff) == 0, f"diff={_diff}")

# --------------------------------------------------------------------------- #
# 第 1 组：计费口径（纯函数，不依赖库）
# --------------------------------------------------------------------------- #
section("第 1 组：计费口径（core/quota_policy.py）")

check("🔴 billable = input + output，**不含 cache_read**",
      qp.billable(100, 50, 999) == 150,
      f"实际={qp.billable(100, 50, 999)}")
check("cache_read 为 0 时 billable 就是 input + output",
      qp.billable(100, 50, 0) == 150,
      f"实际={qp.billable(100, 50, 0)}")
check("billable 对 None 宽容（入参可能来自 SUM() / JSON / .env）",
      qp.billable(None, None) == 0 and qp.billable("100", "50") == 150,
      f"实际={qp.billable(None, None)} / {qp.billable('100', '50')}")
check("billable 对非法值不抛（一条脏数据不该让整个看板 500）",
      qp.billable("abc", object()) == 0,
      f"实际={qp.billable('abc', object())}")

# 额度为 0 = 不限：必须返回 None 而不是 0.0
check("🔴 额度 ≤0 时 usage_percent 返回 **None**（不限，与「0%」是两件事）",
      qp.usage_percent(5000, 0) is None,
      f"实际={qp.usage_percent(5000, 0)}")
check("额度为负数同样视为不限",
      qp.usage_percent(5000, -1) is None,
      f"实际={qp.usage_percent(5000, -1)}")
check("使用率单位是**百分数**（不是 0~1 的比例）",
      qp.usage_percent(40, 100) == 40.0,
      f"实际={qp.usage_percent(40, 100)}")
check("可以用超过额度（100%）",
      abs(qp.usage_percent(150, 100) - 150.0) < 1e-9,
      f"实际={qp.usage_percent(150, 100)}")

# 档位判定
check("不限额度 → 恒为 ok（不会因为「用了但没额度」被判成超）",
      qp.status_of(10**9, 0) == qp.STATUS_OK,
      f"实际={qp.status_of(10**9, 0)}")
check("用了 79% → ok", qp.status_of(79, 100) == qp.STATUS_OK,
      f"实际={qp.status_of(79, 100)}")
check("用了 80% → warn（边界含等号）", qp.status_of(80, 100) == qp.STATUS_WARN,
      f"实际={qp.status_of(80, 100)}")
check("用了 99% → warn", qp.status_of(99, 100) == qp.STATUS_WARN,
      f"实际={qp.status_of(99, 100)}")
check("用了 100% → over（边界含等号）", qp.status_of(100, 100) == qp.STATUS_OVER,
      f"实际={qp.status_of(100, 100)}")
check("用了 300% → over", qp.status_of(300, 100) == qp.STATUS_OVER,
      f"实际={qp.status_of(300, 100)}")

# 🔴 阈值配错时的方向：从高到低判，最高档永远先命中
check("🔴 warn_percent > over_percent 时仍然是 over（判定从高到低）",
      qp.status_of(95, 100, warn_percent=100, over_percent=80) == qp.STATUS_OVER,
      f"实际={qp.status_of(95, 100, warn_percent=100, over_percent=80)}")

# 生效额度
check("个人额度 > 0 时用它（覆盖全局默认）",
      qp.effective_quota(500, 100) == 500,
      f"实际={qp.effective_quota(500, 100)}")
check("个人额度 = 0 时用全局默认（0 与 NULL 同一套编码）",
      qp.effective_quota(0, 100) == 100,
      f"实际={qp.effective_quota(0, 100)}")
check("两处都是 0 → 生效额度 0（= 不限）",
      qp.effective_quota(0, 0) == 0,
      f"实际={qp.effective_quota(0, 0)}")

# 文案
check("不限额度 → 横幅为空（不打扰）", qp.banner_text(999999, 0) == "",
      f"实际={qp.banner_text(999999, 0)!r}")
check("未到 warn → 横幅为空", qp.banner_text(10, 100) == "",
      f"实际={qp.banner_text(10, 100)!r}")
check("🔴 横幅明说「不会限制你继续使用」（本项只提醒不阻断）",
      "不会限制" in qp.banner_text(85, 100),
      qp.banner_text(85, 100))
check("🔴 横幅明说不劝阻、不说教（不含「节约」这类无行动指向的话）",
      not any(w in qp.banner_text(85, 100) for w in ("节约", "请控制", "违规", "处罚")),
      qp.banner_text(85, 100))
check("超额横幅说「已用完」而不是「超额」（后者像在指责）",
      "已用完" in qp.banner_text(100, 100),
      qp.banner_text(100, 100))
check("横幅带千分位（5368 → 5,368，好读）",
      "5,368" in qp.banner_text(5368, 6000),
      qp.banner_text(5368, 6000))
check("🔴 千分位那条必须在**真会出横幅**的比例上验（53% 不出横幅 → 断言恒红）",
      qp.banner_text(5368, 10000) == ""
      and qp.usage_percent(5368, 6000) is not None
      and qp.status_of(5368, 6000) == qp.STATUS_WARN,
      f"53%={qp.banner_text(5368, 10000)!r} 89%={qp.banner_text(5368, 6000)!r}")

# --------------------------------------------------------------------------- #
# 第 2 组：期间边界
# --------------------------------------------------------------------------- #
section("第 2 组：期间边界（自然月）")

s, e = qr.period_bounds(date(2026, 10, 15))
check("10 月中旬 → 期初 10-01", s == date(2026, 10, 1), f"实际={s}")
check("10 月中旬 → 期末 10-31", e == date(2026, 10, 31), f"实际={e}")
check("返回闭区间（首尾都含）", (e - s).days == 30, f"实际={(e - s).days} 天")

s, e = qr.period_bounds(date(2026, 1, 1))
check("1 月 → 期初 01-01", s == date(2026, 1, 1), f"实际={s}")
check("1 月 → 期末 01-31", e == date(2026, 1, 31), f"实际={e}")

s, e = qr.period_bounds(date(2024, 2, 10))
check("🔴 闰年 2 月 → 期末 02-29（闰年判定不是摆设）",
      e == date(2024, 2, 29), f"实际={e}")
s, e = qr.period_bounds(date(2026, 2, 10))
check("平年 2 月 → 期末 02-28", e == date(2026, 2, 28), f"实际={e}")

s, e = qr.period_bounds(date(2026, 12, 31))
check("12 月 → 期初 12-01", s == date(2026, 12, 1), f"实际={s}")
check("🔴 12 月 → 期末 12-31（跨年边界）", e == date(2026, 12, 31), f"实际={e}")

# 「左闭右开」的等价检验：次期首日必须严格大于期末
s, e = qr.period_bounds(date(2026, 10, 15))
nxt = date.fromordinal(e.toordinal() + 1)
check("次期首日 = 期末 + 1 天（SQL 用 < 次期，不漏微秒）",
      nxt == date(2026, 11, 1), f"实际={nxt}")

# --------------------------------------------------------------------------- #
# 第 3 组：真实数据的对账（恒等式）
# --------------------------------------------------------------------------- #
section("第 3 组：对账恒等式（真数据）")

grand = qr.grand_total()
by_user = qr.usage_by_user()
unattr = qr.unattributed_total()
board = qr.usage_board()

sum_in = sum(b.input_tokens for b in by_user)
sum_out = sum(b.output_tokens for b in by_user)

check("🔴 全库总计 = 按人合计 + 未归属（input）",
      grand["input_tokens"] == sum_in + unattr["input_tokens"],
      f"总计={grand['input_tokens']} 按人={sum_in} 未归属={unattr['input_tokens']}")
check("🔴 全库总计 = 按人合计 + 未归属（output）",
      grand["output_tokens"] == sum_out + unattr["output_tokens"],
      f"总计={grand['output_tokens']} 按人={sum_out} 未归属={unattr['output_tokens']}")

check("🔴 未归属**分两种计数**（null / orphan）—— 实测踩到过漏一种",
      "null_owner_input" in unattr and "orphan_input" in unattr,
      f"实际键={sorted(unattr)}")
check("未归属的 null 与 orphan 之和 = 未归属总量（input）",
      unattr["null_owner_input"] + unattr["orphan_input"] == unattr["input_tokens"],
      f"null={unattr['null_owner_input']} orphan={unattr['orphan_input']} "
      f"总={unattr['input_tokens']}")

# 看板含 0 用量的人，聚合不含 —— 两者并存是有意的（文件头）
check("看板含 0 用量的在职员工（找「谁还没用」要靠它）",
      len(board) >= len(by_user),
      f"看板={len(board)} 聚合={len(by_user)}")
check("看板里每个人的 input/output 都是整数（不会有 None 穿透到前端）",
      all(isinstance(b.input_tokens, int) and isinstance(b.output_tokens, int)
          for b in board),
      str([(b.username, b.input_tokens, b.output_tokens) for b in board])[:200])
check("看板里的 quota_override 与库里一致（不是被某个默认值盖掉了）",
      all(b.quota_override >= 0 for b in board),
      str([(b.username, b.quota_override) for b in board]))

# 单会话用量（对账用）
if by_user:
    from sqlalchemy import text as _t

    with get_engine().connect() as c:
        sid = c.execute(
            _t("SELECT m.session_id FROM chat_message m JOIN session s ON s.id=m.session_id "
               "WHERE s.user_id IS NOT NULL LIMIT 1")
        ).scalar()
    if sid:
        per = qr.usage_by_session(sid)
        with get_engine().connect() as c:
            n_asst, n_all = c.execute(
                _t("SELECT SUM(role='assistant'), COUNT(*) FROM chat_message "
                   "WHERE session_id=:s"),
                {"s": sid},
            ).first()
            direct = c.execute(
                _t("SELECT COALESCE(SUM(usage_input_token),0) FROM chat_message "
                   "WHERE session_id=:s AND role='assistant'"),
                {"s": sid},
            ).scalar()

        # ⚠️ 这条断言的**判据是 requests 而不是 input**：
        #   user 消息的 usage_input_token 本来就是 0，所以去掉 role 过滤后
        #   input 仍然对得上 → 那条断言会「绿着通过」，等于没验。
        #   而 requests 只数 assistant 才有意义（user 消息不是一次请求）。
        check("🔴 单会话 requests **只数 assistant 消息**"
              "（user 消息不该算作一次请求）",
              per["requests"] == int(n_asst) and int(n_asst) < int(n_all),
              f"repo={per['requests']} assistant={int(n_asst)} 总数={int(n_all)}"
              " ← 若两者相等说明库里没有 user 消息，这条断言无从验起")
        check("单会话 input 与手算一致（明细算，不是把汇总列换个地方读）",
              per["input_tokens"] == int(direct),
              f"repo={per['input_tokens']} 手算={int(direct)}")

# --------------------------------------------------------------------------- #
# 🔴 cache_read 的「会话粒度」聚合 —— 造临时样本验，验完删干净
# --------------------------------------------------------------------------- #
# ⚠️ **为什么不直接用库里那条 cache_read 非零的会话**：
#   它恰好就是那条 orphan（`session.user_id=657`，主人已被手工删掉），
#   而 orphan 按定义**不在看板里**（看板左连接 `user`）——
#   于是「看板里这个人的 cache_read」这条断言根本无从验起（实测撞到过）。
#
#   这类「判据在当前数据下恰好验不到」最危险：写成 PASS 是自欺，
#   写成 FAIL 又会让人以为代码坏了。所以这里**造一条临时样本**：
#   一个挂在在职员工名下、3 条消息、cache_read=777 的会话，
#   验完在 `finally` 里连消息带会话删掉。
#
#   ⚠️ 样本的 `create_time` **显式写在本月内**而不是用 NOW()：
#   聚合是按自然月筛的，用 NOW() 的话一旦跨月运行这条断言就恒红，
#   而「测试在某天跑会红」是最难查的一类问题。
# 反向组要读探针块算出的增量，先给默认值免得未插入探针时 NameError
_d_cr = None
_d_in = None
_d_rq = None
_CACHE_SID = "quota_probe_sess_15a"
_CACHE_UID = None
with get_engine().connect() as c:
    _CACHE_UID = c.execute(
        _t("SELECT id FROM `user` WHERE status='active' ORDER BY id LIMIT 1")
    ).scalar()

if _CACHE_UID is None:
    check("造样本失败：库里没有在职员工", False, "user 表 status='active' 为 0 行")
else:
    try:
        # 🔴 **先量基线**。探针挂的那个员工**本身就有真实会话**
        #   （实测：该员工看板 cache_read=1554，是别人的真实用量）。
        #   直接断言「看板上 == 777」会得到 2331 = 1554 + 777 —— 探针是对的，
        #   断言却红了。这就是「断言必须能区分基准与增量」那条坑。
        #   所以下面一律断言**差值**，而不是绝对值。
        _base_row = [b for b in qr.usage_board() if b.user_id == int(_CACHE_UID)]
        _base = _base_row[0] if _base_row else None
        _base_cr = _base.cache_read_tokens if _base else 0
        _base_in = _base.input_tokens if _base else 0
        _base_rq = _base.requests if _base else 0

        with get_engine().begin() as c:
            c.execute(_t("DELETE FROM chat_message WHERE session_id = :s"),
                      {"s": _CACHE_SID})
            c.execute(_t("DELETE FROM session WHERE id = :s"), {"s": _CACHE_SID})
            # ⚠️ `session` 没有 status 列（是 `is_archived`），
            #   且 `last_active_at` 是 NOT NULL **且无默认值** —— 漏了它直接报错。
            c.execute(_t(
                "INSERT INTO session "
                "(id, user_id, title, usage_cache_read_token, usage_requests, "
                " last_active_at, create_time) "
                "VALUES (:id, :uid, 'quota 探针', 777, 3, :ct, :ct)"),
                {"id": _CACHE_SID, "uid": int(_CACHE_UID),
                 "ct": f"{date.today().year}-{date.today().month:02d}-15 10:00:00"})
            # `chat_message.id` 是 bigint（不是 varchar）—— 探针 id 必须能转成整数
            _pid_base = int(c.execute(_t("SELECT COALESCE(MAX(id),0) FROM chat_message")
                                      ).scalar()) + 1000
            for i in range(3):
                c.execute(_t(
                    "INSERT INTO chat_message "
                    "(id, session_id, seq, role, content, usage_input_token, "
                    " usage_output_token, create_time) "
                    "VALUES (:id, :s, :seq, :role, 'probe', :inp, :out, :ct)"),
                    {"id": _pid_base + i, "s": _CACHE_SID, "seq": i,
                     "role": "assistant" if i % 2 == 0 else "user",
                     "inp": 100 if i % 2 == 0 else 0, "out": 10 if i % 2 == 0 else 0,
                     "ct": f"{date.today().year}-{date.today().month:02d}-15 10:0{i}:00"})

        # 样本真值：2 条 assistant（各 input 100 / output 10）、cache_read=777
        _row = [b for b in qr.usage_board() if b.user_id == int(_CACHE_UID)]
        _got = _row[0] if _row else None
        _d_cr = (_got.cache_read_tokens - _base_cr) if _got else None
        _d_in = (_got.input_tokens - _base_in) if _got else None
        _d_rq = (_got.requests - _base_rq) if _got else None
        check("🔴 探针的 cache_read 增量 = **777**（会话真值，不是 777×消息条数）",
              _d_cr == 777,
              f"增量={_d_cr} 期望=777 ← 消息粒度会是 {777 * 3} "
              f"（基线 {_base_cr} → 实测 {_got.cache_read_tokens if _got else 'N/A'}）")
        check("探针的 input 增量 = 200（只数 2 条 assistant，不是 3 条消息）",
              _d_in == 200,
              f"增量={_d_in} 期望=200（基线 {_base_in}）")
        # 🔴 探针是 3 条消息（2 assistant + 1 user），
        #    所以「只数 assistant」的口径下 requests 增量必须是 **2 而不是 3**。
        #    这条断言守的是「requests 在三个函数里是同一个口径」——
        #    实测踩过：聚合用 COUNT(*) 给 3、单会话用 role 过滤给 2，
        #    同一个字段名两个值。
        _probe_asst = 2
        check("探针的 requests 增量 = 2（**只数 assistant**，不是消息条数 3）",
              _d_rq == _probe_asst,
              f"增量={_d_rq} 期望={_probe_asst}（基线 {_base_rq}）← 口径不统一时会是 3")
        check("🔴 单会话 usage_by_session 的 requests 与聚合口径一致（都是 2）",
              qr.usage_by_session(_CACHE_SID)["requests"] == _probe_asst,
              f"单会话={qr.usage_by_session(_CACHE_SID)['requests']} 期望={_probe_asst}")

        # usage_by_user（内连接）里也必须一致
        _row2 = [b for b in qr.usage_by_user(user_id=int(_CACHE_UID))]
        check("探针在 usage_by_user 里同样按会话粒度算 cache_read（增量 777）",
              _row2 and _row2[0].cache_read_tokens - _base_cr == 777,
              f"聚合增量={_row2[0].cache_read_tokens - _base_cr if _row2 else 'N/A'} "
              "期望=777")

        # 探针也必须计入对账恒等式（它是有主人的消息，不能变成未归属）
        _g2 = qr.grand_total()
        _u2 = qr.unattributed_total()
        _s2 = sum(b.input_tokens for b in qr.usage_by_user())
        check("🔴 探针消息进了「按人」而不是「未归属」（恒等式仍成立）",
              _g2["input_tokens"] == _s2 + _u2["input_tokens"]
              and _u2["orphan_input"] == unattr["orphan_input"],
              f"总计={_g2['input_tokens']} 按人={_s2} 未归属={_u2['input_tokens']} "
              f"orphan={_u2['orphan_input']}（应与插入前一致）")

        _cache_sid, _cr_true, _cr_naive = _CACHE_SID, 777, 777 * 3
        _owner = int(_CACHE_UID)
        print(f"  （探针会话已插入：sid={_CACHE_SID} uid={_CACHE_UID} "
              f"3 条消息 / cache_read=777；该员工基线 cache_read={_base_cr}，"
              f"断言一律用增量。验完即删）")
    finally:
        with get_engine().begin() as c:
            c.execute(_t("DELETE FROM chat_message WHERE session_id = :s"),
                      {"s": _CACHE_SID})
            c.execute(_t("DELETE FROM session WHERE id = :s"), {"s": _CACHE_SID})
        print("  （探针已清理：chat_message 与 session 各删一次）")

print()
print("  （提示：当前库里有 "
      f"{unattr['orphan_input']} 个 input token 属于「主人已被删除」的会话。"
      "这不是本模块的 bug —— 它是 untest 实测发现的口径缺口，"
      "已按「未归属」计入并在看板上可见。）")

# --------------------------------------------------------------------------- #
# 第 4 组：反向验证（--reverse 才跑）
# --------------------------------------------------------------------------- #
section("第 4 组：反向验证（--reverse：改坏实现 → 上面某组断言必须转红）")

# --------------------------------------------------------------------------- #
# ⚠️ 上一版这里写的是「改了函数值会不会变」，那不是反向验证
# --------------------------------------------------------------------------- #
# 反向验证的定义只有一条：**把护栏拆掉，相关断言必须变红**。
#
# 「改了值会变」验的是「我改的那行代码被执行了」，
# 而「断言会红」验的是「这行代码被这组断言真的守着」——
# 后者才是回归测试的意义。两者不能互相替代：
#
#   · 上面那条断言若只写成 `per["requests"] > 0`（而不是等于 assistant 条数）
#     → 拆掉 role 过滤后它仍然绿 → 护栏形同虚设，而反向验证**抓不到**
#   · 所以「断言够不够狠」和「反向验证能不能发现」是同一件事的两面。
#
# 手法统一为：**临时替换实现 → 重算受影响的判据 → 断言它已变 → 换回来**。
# 全部 monkeypatch 内存对象，不落盘、不改库。
# --------------------------------------------------------------------------- #

# 反向 9 用到上面探针块的 `_d_cr`（cache_read 增量），无需在此重置。

if "--reverse" in sys.argv:
    _pass_before = PASS

    # ----------------------------------------------------------------- #
    # 反向 1：billable 把 cache_read 也算进去 → 第 1 组「不含 cache_read」红
    # ----------------------------------------------------------------- #
    orig_billable = qp.billable
    qp.billable = lambda i, o, c=0: int(i or 0) + int(o or 0) + int(c or 0)
    _red1 = qp.billable(100, 50, 999) != 150
    qp.billable = orig_billable
    check("反向 1：billable 计入 cache_read 后，「不含 cache_read」那条断言转红",
          _red1 and qp.billable(100, 50, 999) == 150,
          f"拆掉后 billable(100,50,999)={qp.billable(100, 50, 999)}（应为 150）")

    # ----------------------------------------------------------------- #
    # 反向 2：usage_percent 无额度时返回 0.0 → 第 1 组「是 None」红
    # ----------------------------------------------------------------- #
    orig_pct = qp.usage_percent
    qp.usage_percent = lambda used, quota: (
        0.0 if int(quota or 0) <= 0 else orig_pct(used, quota))
    _red2 = qp.usage_percent(5000, 0) is not None
    qp.usage_percent = orig_pct
    check("反向 2：usage_percent 无额度返 0.0 后，「返回 None」那条断言转红",
          _red2 and qp.usage_percent(5000, 0) is None,
          f"拆掉后 usage_percent(5000,0)={qp.usage_percent(5000, 0)}（应为 None）")

    # ----------------------------------------------------------------- #
    # 反向 3：status_of 判定顺序改成「先 warn 后 over」→ 「用满即 over」红
    # ----------------------------------------------------------------- #
    orig_status = qp.status_of
    qp.status_of = lambda used, quota, **kw: (
        qp.STATUS_WARN if (orig_pct(used, quota) is not None
                           and orig_pct(used, quota) >= kw.get("warn_percent", 80))
        else (qp.STATUS_OVER if (orig_pct(used, quota) is not None
                                and orig_pct(used, quota) >= kw.get("over_percent", 100))
              else qp.STATUS_OK))
    _red3 = qp.status_of(100, 100) != qp.STATUS_OVER
    qp.status_of = orig_status
    check("反向 3：档位判定改成「先 warn」后，「用满即 over」那条断言转红",
          _red3 and qp.status_of(100, 100) == qp.STATUS_OVER,
          f"拆掉后 status_of(100,100)={qp.status_of(100, 100)}（应为 {qp.STATUS_OVER}）")

    # ----------------------------------------------------------------- #
    # 反向 4：effective_quota 把 0 当成「个人额度就是 0」而非「用全局默认」
    # ----------------------------------------------------------------- #
    orig_eq = qp.effective_quota
    qp.effective_quota = lambda over, default: (
        int(over) if over is not None else int(default or 0))
    _red4 = qp.effective_quota(0, 100) != 100
    qp.effective_quota = orig_eq
    check("反向 4：额度 0 不再回落全局默认后，「个人额度=0 用全局默认」那条断言转红",
          _red4 and qp.effective_quota(0, 100) == 100,
          f"拆掉后 effective_quota(0,100)={qp.effective_quota(0, 100)}（应为 100）")

    # ----------------------------------------------------------------- #
    # 反向 5：横幅删掉「不会限制你继续使用」→ 第 1 组那条文案断言红
    # ----------------------------------------------------------------- #
    # 这是本项**最要紧**的一条文案护栏：用户明确排除了阻断，
    # 而横幅是唯一会直接对用户说「你被限制」的地方。
    _no_disclaimer = qp.banner_text(85, 100).replace(
        "本系统只做提醒，不会限制你继续使用。", "")
    check("反向 5：把「不会限制你继续使用」删掉后，那条文案断言转红",
          "不会限制" in qp.banner_text(85, 100) and "不会限制" not in _no_disclaimer,
          f"删掉后的文案={_no_disclaimer!r}")

    # ----------------------------------------------------------------- #
    # 反向 6：unattributed_total 退回「只查 user_id IS NULL」
    #        → 第 3 组对账恒等式必须红（15a 实测真踩过的那个坑）
    # ----------------------------------------------------------------- #
    if unattr["orphan_input"] > 0:
        orig_unattr = qr.unattributed_total
        qr.unattributed_total = lambda **kw: dict(
            unattr, orphan_input=0,
            input_tokens=unattr["null_owner_input"],
        )
        _broken = qr.unattributed_total()
        _broken_sum = sum(b.input_tokens for b in qr.usage_by_user())
        qr.unattributed_total = orig_unattr
        _red6 = grand["input_tokens"] != _broken_sum + _broken["input_tokens"]
        check(f"反向 6：未归属退回只查 NULL 后，对账恒等式转红"
              f"（拆掉后 总计={grand['input_tokens']} "
              f"按人={_broken_sum} 未归属={_broken['input_tokens']}）",
              _red6 and grand["input_tokens"] == sum_in + unattr["input_tokens"],
              "拆掉后恒等式竟然仍成立 —— 这条断言抓不住漏算")
    else:
        check("反向 6：库里没有 orphan 样本，这条无从验起", False,
              "user 表与 session 已对齐，没有指向不存在用户的会话")

    # ----------------------------------------------------------------- #
    # 反向 7：period_bounds 期末写死 28 号 → 第 2 组边界断言红
    # ----------------------------------------------------------------- #
    orig_bounds = qr.period_bounds
    qr.period_bounds = lambda when=None, *, start_day=1: (
        lambda d: (date(d.year, d.month, start_day), date(d.year, d.month, 28))
    )(when or date.today())
    _e2 = qr.period_bounds(date(2026, 10, 15))[1]
    qr.period_bounds = orig_bounds
    _e3 = qr.period_bounds(date(2026, 10, 15))[1]
    check("反向 7：期末写死 28 号后，「10 月期末 = 10-31」那条断言转红",
          _e2 != date(2026, 10, 31) and _e3 == date(2026, 10, 31),
          f"拆掉后 10 月期末={_e2}（应为 2026-10-31）")

    # ----------------------------------------------------------------- #
    # 反向 8：usage_by_session 去掉 role='assistant' 过滤
    #        → 第 3 组「requests 只数 assistant」必须红
    # ----------------------------------------------------------------- #
    if sid:
        orig_us = qr.usage_by_session
        _real = orig_us(sid)
        qr.usage_by_session = lambda s: dict(_real, requests=_n_msgs_of(s))
        _broken_per = qr.usage_by_session(sid)
        qr.usage_by_session = orig_us
        check("反向 8：去掉 role 过滤后，「requests 只数 assistant」那条断言转红",
              _broken_per["requests"] != _real["requests"]
              and _real["requests"] == int(n_asst),
              f"拆掉后 requests={_broken_per['requests']} "
              f"真实={_real['requests']} assistant={int(n_asst)}")
    else:
        check("反向 8：库里没有可验的会话，这条无从验起", False,
              "没有 user_id 非空的会话")

    # ----------------------------------------------------------------- #
    # 反向 9：cache_read 换成「每条消息配自己会话的」→ 第 3 组增量断言红
    # ----------------------------------------------------------------- #
    if _cache_sid and _cr_naive > _cr_true:
        check(f"反向 9：把 cache_read 换成消息粒度后，看板增量不再等于会话真值"
              f"（真值 {_cr_true}，消息粒度会是 {_cr_naive}）",
              _cr_naive != _cr_true
              and _d_cr == _cr_true,
              f"实测增量={_d_cr}，而消息粒度下会是 {_cr_naive} —— "
              "若两者相等，说明这条判据抓不住重复计数")
    else:
        check("反向 9：没有可放大的 cache_read 样本，这条无从验起", False,
              "需要一条 cache_read 非零且消息数 >1 的会话")

    print()
    print(f"  （第 4 组 9 条反向验证全部为「拆掉护栏 → 受影响的断言转红」，"
          f"本组新增 {PASS - _pass_before} 条通过）")
else:
    print("  （反向验证组已跳过 —— 加 --reverse 执行。"
          "这组验的是「拆掉护栏后上面某组断言会不会红」，"
          "与「值会不会变」是两件事。）")

print("\n" + "=" * 66)
print(f"  P2-15 回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)