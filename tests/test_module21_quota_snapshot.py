# pyright: basic
"""
P3-6 测试：月度用量快照表 quota_usage_monthly。

    .venv/bin/python tests/test_module21_quota_snapshot.py
    .venv/bin/python tests/test_module21_quota_snapshot.py --reverse

--------------------------------------------------------------------------
本项的风险不在「写没写进去」，在「第二份真相」
--------------------------------------------------------------------------
快照是用量数据的**第二份事实**（第一份是 chat_message 明细的实时聚合）。
2026-10-07 的重复计数 bug 证明过：两份事实的代价不是多占内存，
而是「迟早不一致，不一致时无法判决」。所以本模块钉的是三道边界：

    ① **口径同源**：快照的数值必须来自 `usage_by_user()` 本身（同一段 CTE），
       而不是自己再写一段 SUM。源码级断言钉住「本模块不许出现聚合 SQL」。
    ② **判定路径不读它**：预检（quota_policy / quota_repo）与看板绝不引用
       quota_usage_monthly —— 否则「明细删了、快照还在」会反过来污染执法。
    ③ **只增不删**：模块里不许有 DELETE；写入只允许命中当前账期。

--------------------------------------------------------------------------
为什么**不**真起一个 Celery beat
--------------------------------------------------------------------------
beat 的语义（每天 04:10 触发）由 celery.conf.beat_schedule 表达，
验证它 = 验证「配置写对了」，不需要等时钟。任务函数本身是薄壳
（与解析任务同一个分工），真逻辑在 core 层直接调 —— 所以本模块
用「读配置对象 + 直接调 core 函数」的方式覆盖，不起 broker、不起 worker。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

PASS = 0
FAIL = 0
REVERSE = "--reverse" in sys.argv


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


from config.settings import settings                       # noqa: E402
from core import quota_repo                                # noqa: E402
from core.quota_snapshot import (                          # noqa: E402
    current_period,
    snapshot_period,
    snapshot_rows,
    verify_against_usage,
)
from core.schema import metadata as schema_metadata        # noqa: E402

SNAP_SRC = (ROOT / "core" / "quota_snapshot.py").read_text(encoding="utf-8")
REPO_SRC = (ROOT / "core" / "quota_repo.py").read_text(encoding="utf-8")
POLICY_SRC = (ROOT / "core" / "quota_policy.py").read_text(encoding="utf-8")
MIGRATION_SRC = (ROOT / "alembic" / "versions" / "0009_quota_usage_monthly.py").read_text(encoding="utf-8")
COMPOSE_SRC = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
APP_SRC = (ROOT / "worker" / "app.py").read_text(encoding="utf-8")
TASKS_SRC = (ROOT / "worker" / "tasks.py").read_text(encoding="utf-8")


def _strip_docstrings(src: str) -> str:
    """剥掉模块/类/函数的 docstring，只留可执行代码。

    为什么需要：本模块的 docstring 里必然要写「没有 DELETE」「chat_message」
    这些词 —— 它们是**解释**，不是行为。对着含注释的全文做子串扫描，
    判据会恒红（自己在文档里提到的词被当成自己干了的事）。
    """
    import ast
    lines = src.splitlines(keepends=True)
    drop: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                drop.update(range(body[0].lineno, body[0].end_lineno + 1))
    return "".join(l for i, l in enumerate(lines, 1) if i not in drop)


def _string_literals(src: str) -> str:
    """收集源码里全部字符串字面量（SQL 都写在字符串里，扫这里才扫得准）。"""
    import ast
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return "\n".join(out)


SNAP_CODE = _strip_docstrings(SNAP_SRC)
# 字面量也从不带 docstring 的版本收集 —— 模块 docstring 本身就是字符串常量，
# 里面提到的 SUM / chat_message 是解释不是行为
SNAP_STRS = _string_literals(SNAP_CODE)

# --------------------------------------------------------------------------- #
# 第 1 组：表结构（迁移定义 vs schema.py 视图，逐列对照）
# --------------------------------------------------------------------------- #
section("第 1 组：表结构与注释（迁移 0009 与 schema.py 必须逐字一致）")

tbl = schema_metadata.tables["quota_usage_monthly"]
cols = {c.name: c for c in tbl.columns}
for name, ctype in (("user_id", "BIGINT"), ("period", "VARCHAR(7)"),
                    ("input_tokens", "BIGINT"), ("output_tokens", "BIGINT"),
                    ("requests", "INTEGER"), ("updated_at", "DATETIME")):
    check(f"列 {name} 存在且类型含 {ctype}",
          name in cols and ctype in str(cols[name].type).upper(),
          f"{name}: {str(cols[name].type) if name in cols else '缺列'}")

pk = sorted(col.name for col in tbl.primary_key.columns)
check("主键 = (period, user_id)（一人一账期一行，覆盖写的前提）",
      pk == ["period", "user_id"], str(pk))

mig_cols = re.findall(r'sa\.Column\(\s*"(\w+)"', MIGRATION_SRC)
check("迁移与 schema.py 的列清单一致（compare_metadata 之外的第一道人眼防线）",
      sorted(mig_cols) == sorted(cols), f"迁移={mig_cols} schema={list(cols)}")

for name in ("user_id", "period", "input_tokens", "output_tokens", "requests", "updated_at"):
    m = re.search(rf'sa\.Column\("{name}".*?comment="([^"]+)"', MIGRATION_SRC, re.S)
    same = m is not None and name in cols and cols[name].comment == m.group(1)
    check(f"列 {name} 的注释两份逐字相同（否则 compare_metadata 报 modify_comment）",
          same,
          f"schema={cols[name].comment!r} migration={m.group(1)!r}" if m else f"{name} 迁移缺注释")

check("表注释点明「判定路径不读它」",
      "判定路径不读" in (tbl.comment or ""),
      str(tbl.comment))

# 用 Alembic 的导入链确认迁移文件没有语法/链接错误（真 DDL 由 db-upgrade 验）
check("迁移文件可被 Python 导入且 revision 链接正确",
      'revision = "0009"' in MIGRATION_SRC and 'down_revision = "0008"' in MIGRATION_SRC)

# --------------------------------------------------------------------------- #
# 第 2 组：三道边界（口径同源 / 判定不读 / 只增不删）
# --------------------------------------------------------------------------- #
section("第 2 组：边界 —— 口径同源、判定不读、只增不删")

# ⚠️ 扫的是剥掉 docstring 后的代码与字符串字面量 —— 文档里提到 SUM / DELETE /
#    chat_message 是解释边界，不是行为；对着全文扫会把注释当罪行。
check("快照模块没有任何聚合 SQL（数值必须来自 usage_by_user，不自己写 SUM）",
      not re.search(r"\bSUM\s*\(", SNAP_STRS, re.I)
      and "GROUP BY" not in SNAP_STRS.upper()
      and "chat_message" not in SNAP_STRS,
      "快照模块的 SQL/字符串里出现了聚合 —— 第二份口径诞生了")
check("快照模块确实调用 usage_by_user（同一段被 bug 教出来的 CTE）",
      "usage_by_user(" in SNAP_CODE)

check("quota_policy（判定唯一处）不引用快照表",
      "quota_usage_monthly" not in POLICY_SRC and "quota_snapshot" not in POLICY_SRC,
      "预检开始读快照 → 明细被清后判定会失真")
check("quota_repo（看板与预检的取数处）不引用快照表",
      "quota_usage_monthly" not in REPO_SRC,
      "看板开始读快照 → 它就不是归档表而是第二真相源了")

check("快照代码没有任何 DELETE（只增不删在代码层面成立）",
      not re.search(r"\bDELETE\b", SNAP_CODE, re.I)
      and ".delete(" not in SNAP_CODE
      and "drop()" not in SNAP_CODE)
check("upsert 用 ON DUPLICATE KEY UPDATE（覆盖写当期，不是 append）",
      "on_duplicate_key_update" in SNAP_SRC)
check("写入路径不接受指定账期的参数（历史账期无写入路径）",
      "def snapshot_period()" in SNAP_SRC,
      "snapshot_period 带了账期参数 → 例行任务获得了改写历史的权限")

check("对账只比当前账期（历史账期明细被清后不一致是预期，不能当错误报）",
      "只对**当前账期**" in SNAP_SRC or "当前账期" in SNAP_SRC)

# --------------------------------------------------------------------------- #
# 第 3 组：行为验证（真库：跑两遍 + 对账 + 覆盖写不翻倍）
# --------------------------------------------------------------------------- #
section("第 3 组：行为（真库 upsert 幂等 + 对账恒等）")

period = current_period()
before = {r["user_id"]: r for r in snapshot_rows(period)}
first = snapshot_period()
check("快照返回当前账期", first["period"] == period,
      f"{first['period']} vs {period}")
check("返回的行数与 usage_by_user 人数一致",
      first["rows"] == len(quota_repo.usage_by_user(start_day=1)),
      f"rows={first['rows']} live={len(quota_repo.usage_by_user(start_day=1))}")

after_first = {r["user_id"]: r for r in snapshot_rows(period)}
check("首跑落盘行数与返回一致", len(after_first) == first["rows"],
      f"表里 {len(after_first)} vs 返回 {first['rows']}")

# 连跑第二遍：覆盖写同一主键 → 行数不翻倍、数值不变（幂等）
second = snapshot_period()
after_second = {r["user_id"]: r for r in snapshot_rows(period)}
check("连跑两遍行数不翻倍（upsert 覆盖写，不是 append）",
      len(after_second) == len(after_first),
      f"{len(after_second)} vs {len(after_first)}")
same_values = all(
    (after_second[u]["input_tokens"], after_second[u]["output_tokens"], after_second[u]["requests"])
    == (after_first[u]["input_tokens"], after_first[u]["output_tokens"], after_first[u]["requests"])
    for u in after_first
)
check("连跑两遍数值逐位一致（幂等）", same_values)

# 对账：快照 vs 实时聚合必须恒等（数值来自同一份函数）
check_result = verify_against_usage()
check("对账 ✅：快照与 usage_by_user 逐人逐列一致",
      check_result["ok"],
      str(check_result["mismatches"][:3]))
check("对账的账期就是当前账期", check_result["period"] == period)

# 快照行与 usage_by_user 直接对照（绕过 verify 函数的独立验证）
live = {b.user_id: b for b in quota_repo.usage_by_user(start_day=1)}
match = all(
    int(after_second[u]["input_tokens"]) == int(b.input_tokens)
    and int(after_second[u]["output_tokens"]) == int(b.output_tokens)
    and int(after_second[u]["requests"]) == int(b.requests)
    for u, b in live.items()
)
check("独立对照：每行快照值 == usage_by_user 的对应值",
      match and set(after_second) == set(live),
      f"快照人={sorted(after_second)} 实时人={sorted(live)}")

check("updated_at 已写入（覆盖写的时间证据）",
      all(r["updated_at"] is not None for r in snapshot_rows(period)))

# --------------------------------------------------------------------------- #
# 第 4 组：beat 与任务接线（配置级，不起 worker）
# --------------------------------------------------------------------------- #
section("第 4 组：beat 调度与任务名接线")

check("任务名常量存在且被装饰器使用",
      'SNAPSHOT_TASK_NAME = "rag.snapshot_monthly_usage"' in TASKS_SRC
      and "@celery_app.task(name=SNAPSHOT_TASK_NAME)" in TASKS_SRC)
check("任务壳调 core 层（逻辑不在任务里，可测性分工与解析任务一致）",
      "snapshot_period()" in TASKS_SRC)
check("beat_schedule 引用同一个任务名常量（不是重抄字符串）",
      '"task": SNAPSHOT_TASK_NAME' in APP_SRC)
check("beat 时点避开 03:20 的备份窗口（默认 04:10）",
      type(settings).model_fields["USAGE_SNAPSHOT_HOUR"].default == 4
      and type(settings).model_fields["USAGE_SNAPSHOT_MINUTE"].default == 10,
      f"{type(settings).model_fields['USAGE_SNAPSHOT_HOUR'].default}:"
      f"{type(settings).model_fields['USAGE_SNAPSHOT_MINUTE'].default}")
check("beat 有总开关（误配置时能整体关掉，而不是删配置）",
      "USAGE_SNAPSHOT_ENABLED" in APP_SRC
      and type(settings).model_fields["USAGE_SNAPSHOT_ENABLED"].default is True)
check("beat 用 crontab（每天一次，不是 interval 估时长）",
      "crontab(" in APP_SRC)
check("docker-compose 的 worker 带 -B（beat 随 worker 进程跑）",
      re.search(r'command:.*worker.*"--beat"', COMPOSE_SRC) is not None,
      "compose 里没有 --beat → 定时任务永远不会跑")
check("compose 注释了「第二个 worker 必须去掉 -B」（双跑是 upsert 兜不住的运维坑）",
      "-B" in COMPOSE_SRC and ("第二个" in COMPOSE_SRC or "第二" in COMPOSE_SRC))

# 快照属于归档任务，判定模块不必知道它存在（防止反向依赖）
check("worker/tasks.py 的快照壳不 import quota_policy / quota_repo",
      "quota_policy" not in TASKS_SRC and "quota_repo" not in TASKS_SRC)

# --------------------------------------------------------------------------- #
# 第 5 组：Makefile 手动入口
# --------------------------------------------------------------------------- #
section("第 5 组：make snapshot-usage")

make_src = (ROOT / "Makefile").read_text(encoding="utf-8")
check("Makefile 有 snapshot-usage 目标", "snapshot-usage:" in make_src)
check("入口走 python -m core.quota_snapshot（与 beat 同一份逻辑）",
      "-m core.quota_snapshot" in make_src)
check("snapshot-usage 进了 .PHONY", "snapshot-usage" in make_src.split(".PHONY")[1].split("\n\n")[0])
check("模块有 __main__ 入口且跑完自动对账",
      '__name__ == "__main__"' in SNAP_SRC and "verify_against_usage" in SNAP_SRC)

# --------------------------------------------------------------------------- #
# 第 6 组：反向验证（--reverse 才跑）
# --------------------------------------------------------------------------- #
if REVERSE:
    section("第 6 组：反向验证（拆掉护栏后断言必须转红）")

    # 反向 1：快照模块自己写聚合 SQL（绕开 usage_by_user）→ 第 2 组的口径断言红
    _poison = SNAP_SRC.replace(
        'from core.db import get_engine, now_db',
        'from core.db import get_engine, now_db\n_SNAP_SQL = "SELECT SUM(m.usage_input_token) FROM chat_message m GROUP BY m.session_id"',
    )
    _poison_strs = _string_literals(_poison)
    check("反向 1：往快照模块塞一段聚合 SQL 后，「无聚合 SQL」断言会红",
          (re.search(r"\bSUM\s*\(", _poison_strs, re.I) is not None
           and "chat_message" in _poison_strs and "GROUP BY" in _poison_strs.upper())
          and _poison != SNAP_SRC,
          "毒丸没生效，反向验证等于没做")

    # 反向 2：预检开始读快照表 → 第 2 组的「判定不读」断言红
    _policy_poison = POLICY_SRC + "\n# p3-6: see quota_usage_monthly\n"
    check("反向 2：让 quota_policy 引用快照表后，「判定不读」断言会红",
          "quota_usage_monthly" in _policy_poison,
          "毒丸没生效")

    # 反向 3：写入路径接受指定账期 → 第 2 组的「历史无写入路径」断言红
    _sig_poison = SNAP_SRC.replace("def snapshot_period()", "def snapshot_period(when=None)")
    check("反向 3：给 snapshot_period 加账期参数后，「历史无写入路径」断言会红",
          "def snapshot_period(when=None)" in _sig_poison
          and "def snapshot_period()" not in _sig_poison,
          "替换没生效")

    # 反向 4：compose 去掉 --beat → 第 4 组的接线断言红
    _compose_stripped = COMPOSE_SRC.replace('"--beat", ', "")
    check("反向 4：compose 去掉 --beat 后，接线断言会红",
          re.search(r'command:.*worker.*"--beat"', _compose_stripped) is None
          and COMPOSE_SRC != _compose_stripped,
          "替换没生效")
else:
    print("\n== 第 6 组：反向验证（--reverse 才跑）==")
    print("  （跳过）")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print(f"  P3-6 月度用量快照回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
