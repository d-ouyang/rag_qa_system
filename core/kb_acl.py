"""
知识库写权限的**唯一判定处**（P2-14a）。

--------------------------------------------------------------------------
这个模块为什么必须存在（而不是在路由里 if 一下）
--------------------------------------------------------------------------
判据散落在四个路由里，就等于没有判据 —— 因为**新增路由时一定会忘**，
而这次要防的失效恰恰是「不报错」的：

    实测基线（2026-10-08，PLAN §P2-14 §14.0）：
        普通员工 chen.jie → POST   /api/v1/documents/upload    → 202 + doc_id=587
        普通员工 chen.jie → DELETE /api/v1/documents/587       → {"record_removed": true}

四条写路由（`upload` / `upload/batch` / `reparse` / `delete`）当时
**一条身份依赖都没有**。它们不报错、测试全绿、前端也没有任何异常提示 ——
普通员工安静地把全公司共用的知识库改了。

理由与 `core/password_policy.py` 完全一致（11b立的规矩）：
**判定规则要有单一出处**，且最好**不起库**就能钉死边界与文案。
本模块是纯函数，不碰数据库 —— `kb_role` 由调用方传进来。

--------------------------------------------------------------------------
五档的语义，以及为什么「重灌索引」要单独一档
--------------------------------------------------------------------------
    none        默认。只读：能搜、能看引用、不能改任何东西
    ops         运营。日常维护：可上传、可删除
    qa          测试。可上传、可删除、可重灌索引
    dev         开发。同qa
    superadmin  超管。同 qa，**当前唯一启用的一档**

`reindex`（重建整库向量索引）之所以要单独一档、且 `ops` 刻意不给：
它是**唯一会让全公司答错**的操作。改切分参数 = 整库重建，而切片 id
是派生值（`chunk_id = "<doc_id>:<chunk_index>"`，见 PLAN §4）——
切分参数一变，全库的历史 `chunk_id` 都会解析出错误正文。

上传 / 删错了能改回来（重新传一份），重灌错了要等一次全库重建。
把两者放在同一档，等于让「日常维护知识库的人」持有「弄坏全公司知识库」的能力。

--------------------------------------------------------------------------
为什么当前只启用 superadmin
--------------------------------------------------------------------------
用户 2026-10-08 拍板：「知识库的写权限一般有运营人员、测试、开发、超管，
目前我这个项目只给 admin 的超级管理员。」

所以另外三档**只建判定逻辑、不发给任何人**。这不是「先搭好架子」，
而是刻意的 fail-closed：默认 `none`，要授权必须有人明确动手。
若反过来默认给 `ops`，那么「每个新员工都要记得撤销」会成为一条
实际上不会被遵守的规矩。

--------------------------------------------------------------------------
与 role 的关系：**正交，不是替代**
--------------------------------------------------------------------------
`user.role` 三档（`admin` / `hr` / `user`）管「能不能进管理端」，
判据在 `core/identity.py` 的 `STAFF_ROLES` / `is_admin()`。本模块**只管知识库**。

15 种组合全部合法，其中特别重要的是这两个：

    role=hr   + kb_role=none       人事同事，只看知识库
    role=user + kb_role=ops        运营同事，能维护知识库但进不了管理端

⚠️ 代价是「谁能改知识库」与「谁是管理员」不再有推导关系 ——
一个 `kb_role=superadmin` 但 `role=user` 的人能删知识库但进不了管理端。
**这是刻意的**（见迁移 0006 的文件头），但必须写进 `audit_log`（14c 会落）
才不至于出现「谁干的」查不到的情况。
"""
from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# 取值集合（唯一出处：改这里 + 改 schema.py 的列注释 + 改迁移的字面副本）
# --------------------------------------------------------------------------- #
KB_ROLE_NONE = "none"
KB_ROLE_OPS = "ops"
KB_ROLE_QA = "qa"
KB_ROLE_DEV = "dev"
KB_ROLE_SUPERADMIN = "superadmin"

KB_ROLES = frozenset({
    KB_ROLE_NONE,
    KB_ROLE_OPS,
    KB_ROLE_QA,
    KB_ROLE_DEV,
    KB_ROLE_SUPERADMIN,
})

#: 兜底值。**任何拿不到 `kb_role` 的情况都落到这里**（而不是抛异常或当超管）——
#: fail-closed 的具体形态。理由见模块文件头：判不出身份时「什么都不给」，
#: 而「给全部」才是真漏洞。
DEFAULT_KB_ROLE = KB_ROLE_NONE

# --------------------------------------------------------------------------- #
# 三个动作 → 各自需要哪一档
# --------------------------------------------------------------------------- #
#: 写操作的动作名。用字符串常量而不是裸串，是为了让「有哪些动作」在
#: 导入本模块时就是一个可枚举的集合 —— 14e 的结构断言会拿它对着路由核对，
#: **漏一条路由时立刻转红**（这正是「从零补守卫」这件事唯一可靠的验法）。
ACTION_UPLOAD = "upload"
ACTION_DELETE = "delete"
ACTION_REINDEX = "reindex"
ACTIONS = frozenset({ACTION_UPLOAD, ACTION_DELETE, ACTION_REINDEX})

#: 动作 → 允许执行它的 `kb_role` 集合。
#:
#: ⚠️ 这里刻意写成「集合」而不是「有序门槛」（`min_kb_role` 那种）——
#: 五档之间**不是**全序关系：`qa` 与 `dev` 权限完全相同，把它们排成一条线
#: 会让读代码的人以为「dev 比 qa 强」。事实是它们是并列的两档，
#: 只有 `superadmin` 因为「唯一被启用的一档」而在事实上最常用。
#: 集合的代价是加一档中间档要改 N 处 —— 但每档只出现在 1~2 个动作里，
#: 且 14e 的结构断言会对着这个字典核对。
MIN_KB_ROLE_BY_ACTION: dict[str, frozenset[str]] = {
    ACTION_UPLOAD: frozenset({KB_ROLE_OPS, KB_ROLE_QA, KB_ROLE_DEV, KB_ROLE_SUPERADMIN}),
    ACTION_DELETE: frozenset({KB_ROLE_OPS, KB_ROLE_QA, KB_ROLE_DEV, KB_ROLE_SUPERADMIN}),
    ACTION_REINDEX: frozenset({KB_ROLE_QA, KB_ROLE_DEV, KB_ROLE_SUPERADMIN}),
}

# --------------------------------------------------------------------------- #
# 中文标签（管理端下拉用）
# --------------------------------------------------------------------------- #
KB_ROLE_LABELS: dict[str, str] = {
    KB_ROLE_NONE: "只读（不能上传 / 删除）",
    KB_ROLE_OPS: "运营（可上传 / 删除）",
    KB_ROLE_QA: "测试（可上传 / 删除 / 重灌索引）",
    KB_ROLE_DEV: "开发（可上传 / 删除 / 重灌索引）",
    KB_ROLE_SUPERADMIN: "超级管理员（可上传 / 删除 / 重灌索引）",
}


def normalize(kb_role: Any) -> str:
    """
    把任意输入规整成一个合法 `kb_role`。

    **为什么未知值一律降级成 `none` 而不是抛异常**：

    判据是「这个人能不能删知识库」，输入却可能来自
    ①库里那一列（管理员写错了）②JWT 载荷（老 token 没有这个字段）
    ③网关注入的身份头（14b 才有）。这三处都可能给出预期外的东西，
    而「意外获得删除全公司知识库的能力」是最坏的结果。
    所以：**猜不到就当只读**，并让调用方去日志里看（见 `can_*` 的 warn）。

    大小写与空白被规整掉，是因为这些值经手工 SQL 灌进库里时
    很容易带上（`'Ops '`），而 `'ops'` 与`'Ops '` 在库里是两条不同的数据。
    """
    if not isinstance(kb_role, str):
        return DEFAULT_KB_ROLE
    v = kb_role.strip().lower()
    return v if v in KB_ROLES else DEFAULT_KB_ROLE


def is_valid_kb_role(kb_role: Any) -> bool:
    """取值是否合法（**宽松**：接受大小写与空白变体）。给仓储层 `set_kb_role()` 用。"""
    return isinstance(kb_role, str) and kb_role.strip().lower() in KB_ROLES


def is_canonical_kb_role(kb_role: Any) -> bool:
    """取值是否**就是规范形态**（严格：必须逐字等于某一档）。

    --------------------------------------------------------------------------
    为什么需要严格版（14f 加的，写路径专用）
    --------------------------------------------------------------------------
    `is_valid_kb_role` 的宽松是为**读**路径准备的：库里可能躺着 `'Ops '`
    （手工 SQL 灌进去的），判定前不规整就会失配 —�� 那会让一个人莫名失去权限，
    而且**不报错**。所以 `normalize()` 必须宽松。

    但**写**路径不能宽松。管理端下拉框给的是规范值（14f 规定前端不接受自由
    输入），宽松在这条路上没有正当来源，却会「静默改写别人的权限」：

        管理员发来`'SUPERADMIN'` → 库里存成 `superadmin`
        → 审计detail 的 from/to 记的也是规范化后的值
        → 「我明明选了 A，它存成了 B」在任何一界都看不出来

    宁可让接口回400、让人重选一次。这与 `admin_service.set_role()` 的
    `role in repo.ROLES`（精确匹配、不做strip/lower）是同一条规格。

    ⚠️ 代价要说清：如果哪天管理端改成允许自由输入，就必须换成宽松那条，
    同时前端得把「输入的值」当成最终值回显 —— 否则用户会看到自己填的
    和系统存的不是一回事。
    """
    return isinstance(kb_role, str) and kb_role in KB_ROLES


def check_kb_role(kb_role: str) -> str:
    """
    校验并返回规范值，非法直接 `ValueError`。**只在写入路径调用**。

    与 `normalize()` 的分工：
      · `normalize` 用于**读**（判定前把输入规整好），永不抛；
      · `check_kb_role` 用于**写**（管理员下发授权），非法就拒。
    读用抛异常的版本 → 一个空格就能让全公司登不上；写用静默版本 →
    管理员打了 `boss` 却显示成功，两边都错。
    """
    if not is_valid_kb_role(kb_role):
        raise ValueError(f"kb_role 只能是 {sorted(KB_ROLES)}，收到 {kb_role!r}")
    return str(kb_role).strip().lower()


def can(kb_role: Any, action: str) -> bool:
    """
    核心判定：`kb_role` 能不能执行 `action`。**纯函数，不起库。**

    ⚠️ 未知的 `action` 一律**拒绝**（而不是拒绝「不认识的动作」这件事抛异常）：
    调用方拼错动作名的症状会是 500，而权限系统不该因为拼错就整个挂掉。
    拒绝 + 日志是更好的形状 —— 写错的人会从日志里看到「action=xxx 未知」。
    """
    allowed = MIN_KB_ROLE_BY_ACTION.get(action)
    if allowed is None:
        return False
    return normalize(kb_role) in allowed


def can_upload(kb_role: Any) -> bool:
    return can(kb_role, ACTION_UPLOAD)


def can_delete(kb_role: Any) -> bool:
    return can(kb_role, ACTION_DELETE)


def can_reindex(kb_role: Any) -> bool:
    return can(kb_role, ACTION_REINDEX)


def describe(action: str) -> str:
    """
    拒权时给用户看的理由。**对外文案不含「你的 kb_role 是什么」**。

    理由与 11b 的防枚举一致：这类接口的错误文案会被脚本反复试探，
    所以只说「不能」，不说「你的是什么档」。管理员要查档位去管理端看。

    ⚠️ 参数是 `action`（被拒的动作），**不是** `kb_role`。
    第一版这里写成了 `describe(kb_role)` 而函数体里用的是 `action`
    —— 于是每次拒权都变成 500 `NameError`：

        知识库写操作被拒 | action=upload | actor=chen.jie     ← 判定是对的
        NameError: name 'action' is not defined               ← 响应却是 500

    这个 bug 的形状值得记住：**权限判定生效了，但错误路径本身抛异常**，
    症状是「用户看到 500 而不是 403」。若那时只看「普通员工的写操作没成功」
    就收工，会以为守卫已经做完 —— 实际上前端拿到 500 完全不知道是权限问题。
    「拒权路径本身要能正常返回 403」是一条独立于「权限判得对不对」的断言。
    """
    action_hint = {
        ACTION_UPLOAD: "上传知识库文档",
        ACTION_DELETE: "删除知识库文档",
        ACTION_REINDEX: "重建知识库索引",
    }.get(action, "该操作")
    return f"没有权限执行「{action_hint}」，请联系管理员开通知识库维护权限"


def capabilities(kb_role: Any) -> dict[str, bool]:
    """
    某一档能干什么 —— 管理端列表与前端入口收敛都用它，**避免各写一套 if**。

    14d 的前端按钮显隐、14f 的管理端下拉，都从这一个函数派生。
    写成「三处各自维护一份」的话，加一档中间档时一定会有一处漏改。
    """
    role = normalize(kb_role)
    return {
        "upload": can_upload(role),
        "delete": can_delete(role),
        "reindex": can_reindex(role),
    }


__all__ = [
    "ACTION_DELETE",
    "ACTION_REINDEX",
    "ACTION_UPLOAD",
    "ACTIONS",
    "DEFAULT_KB_ROLE",
    "KB_ROLES",
    "KB_ROLE_DEV",
    "KB_ROLE_LABELS",
    "KB_ROLE_NONE",
    "KB_ROLE_OPS",
    "KB_ROLE_QA",
    "KB_ROLE_SUPERADMIN",
    "MIN_KB_ROLE_BY_ACTION",
    "can",
    "can_delete",
    "can_reindex",
    "can_upload",
    "capabilities",
    "check_kb_role",
    "describe",
    "is_valid_kb_role",
    "normalize",
]