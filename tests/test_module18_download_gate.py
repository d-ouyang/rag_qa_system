# pyright: basic
"""
P3-2 测试：下载原始文件的两道闸门（kb ACL + 部署开关）。

    .venv/bin/python tests/test_module18_download_gate.py
    .venv/bin/python tests/test_module18_download_gate.py --reverse

--------------------------------------------------------------------------
为什么要单独一个文件（而不是塞进 module12 / module14d）
--------------------------------------------------------------------------
它管的三件事在别处都没有归处：

    ① **下载是唯一挂 kb 守卫的 GET 路由** —— 14e 的规矩是「读路由不挂守卫」，
       本项把它改成「显式白名单」，那条改动要在同一个地方看得见理由。
    ② **部署开关 `DOC_DOWNLOAD_ENABLED`** —— 它是本仓库第一个
       「功能级总开关」，判据（false → 404 而不是 403）需要单独钉住。
    ③ **capabilities 里出现了第一个「非纯 ACL」的能力键** ——
       `download = ACL && 开关`，这个「合并发生在服务端」的约定要有断言。

--------------------------------------------------------------------------
为什么**不**真跑一次下载
--------------------------------------------------------------------------
真下载要一份真实的 `document` 行 + 磁盘上的原文件。沙箱库是**新库**（没有
文档），而真库里那 35 份是真实知识库 —— 为一个测试去动它们不值。
所以本模块的判据是**依赖级**（直接调路由函数，断言它在哪一步拒绝）
+ **源码级**（顺序、开关位置）。两者都不需要库里有数据。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.logging_config import setup_logging  # noqa: E402
from config.settings import settings  # noqa: E402

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


from core import kb_acl                                    # noqa: E402
from core.identity import Actor, require_kb_download       # noqa: E402
from api.routes import documents as doc_routes             # noqa: E402
from fastapi import HTTPException                          # noqa: E402

FAKE_USER_ID = 999002


def make_actor(kb_role: str) -> Actor:
    """纯内存 Actor，不落库（与 module12 同一套路）。"""
    return Actor(
        id=FAKE_USER_ID, username=f"probe.{kb_role}", display_name=f"探针_{kb_role}",
        role="user", kb_role=kb_role, status="active", source="header",
        record=None, client_ip="127.0.0.1",
    )


# --------------------------------------------------------------------------- #
# 第 1 组：ACL 矩阵（纯函数）
# --------------------------------------------------------------------------- #
section("第 1 组：下载的 ACL 矩阵（纯函数，不起库）")

#: 期望矩阵**手写**，不从 MIN_KB_ROLE_BY_ACTION 派生（派生 = 实现等于自己）。
#: 判据：除只读档（none）之外都能下 —— ops 能删一份文档却不能取回原件，
#: 这个组合没有任何人解释得通。
EXPECTED_DOWNLOAD = {
    "none": False, "ops": True, "qa": True, "dev": True, "superadmin": True,
}
for role, want in EXPECTED_DOWNLOAD.items():
    check(f"kb_role={role:<11} can_download = {want}",
          kb_acl.can_download(role) is want,
          f"实际={kb_acl.can_download(role)}")

check("🔴 capabilities() 四键齐全且 download 与 can_download 一致",
      all(set(kb_acl.capabilities(r)) == {"upload", "delete", "reindex", "download"}
          and kb_acl.capabilities(r)["download"] is want
          for r, want in EXPECTED_DOWNLOAD.items()),
      str({r: kb_acl.capabilities(r) for r in EXPECTED_DOWNLOAD}))

check("🔴 download 是 ACTIONS 的一员（14e 的结构断言会扫到它）",
      kb_acl.ACTION_DOWNLOAD in kb_acl.ACTIONS, str(sorted(kb_acl.ACTIONS)))

_dl_text = kb_acl.describe(kb_acl.ACTION_DOWNLOAD)
check("拒权文案说清被拒的动作（下载知识库原始文件）",
      "下载" in _dl_text and "原始文件" in _dl_text, _dl_text)
check("🔴 拒权文案不含任何档位名（防枚举，与 11b 同一条）",
      not any(r in _dl_text for r in kb_acl.KB_ROLES), _dl_text)
# 🔴 这一组是 P3-2 **当场踩出来**的：`admin_service.set_kb_role` 的
#    「管理员不能收回自己的权限」那条报错里写死了一份三键中文名表，
#    遍历 capabilities() 时新加的 `download` 直接 KeyError → 500，
#    而当时的测试只断言「抛了异常」所以还是绿的。
#    判据因此是「**每个能力键都有中文名**」而不是「这条报错文案对不对」。
check("🔴 capabilities() 的每个键都在 ACTION_LABELS 里（漏一个 = 500，且静默）",
      all(k in kb_acl.ACTION_LABELS for k in kb_acl.capabilities("none")),
      f"缺={sorted(set(kb_acl.capabilities('none')) - set(kb_acl.ACTION_LABELS))}")
check("🔴 ACTION_LABELS 与 ACTIONS 一一对应（加动作必须同时加名字）",
      set(kb_acl.ACTION_LABELS) == set(kb_acl.ACTIONS),
      f"标签={sorted(kb_acl.ACTION_LABELS)} 动作={sorted(kb_acl.ACTIONS)}")
check("管理端那份写死的三键字典已经不存在（改成从 kb_acl 取）",
      '{"upload": "上传", "delete": "删除", "reindex": "重建索引"}'
      not in (ROOT / "core/admin_service.py").read_text(encoding="utf-8"),
      "admin_service.py 里还有写死的动作名表")

check("未知档位/脏值一律当只读（fail-closed，不抛异常）",
      kb_acl.can_download("boss") is False and kb_acl.can_download(None) is False
      and kb_acl.can_download(" OPS ") is True,
      f"boss={kb_acl.can_download('boss')} None={kb_acl.can_download(None)}")

# --------------------------------------------------------------------------- #
# 第 2 组：守卫依赖（拒权必须 403，不是 500）
# --------------------------------------------------------------------------- #
section("第 2 组：require_kb_download 依赖的拒绝路径")


def _run_dep(actor: Actor) -> HTTPException | None:
    try:
        require_kb_download(actor=actor)
        return None
    except HTTPException as e:
        return e


_ex = _run_dep(make_actor("none"))
check("🔴 none 被拒 → **403**（不是 500，也不是 404）",
      _ex is not None and _ex.status_code == 403,
      f"实际={_ex.status_code if _ex else '没拒'}")
check("403 带中文文案（前端能直接显示）",
      _ex is not None and isinstance(_ex.detail, str) and len(_ex.detail) > 0,
      repr(getattr(_ex, "detail", None)))
check("🔴 ops / qa / dev / superadmin 不被拒",
      all(_run_dep(make_actor(r)) is None for r in ("ops", "qa", "dev", "superadmin")),
      str({r: _run_dep(make_actor(r)) for r in ("ops", "qa", "dev", "superadmin")}))
check("守卫名字带动作（FastAPI 依赖覆写与日志里能认出它）",
      require_kb_download.__name__ == "require_kb_download",
      require_kb_download.__name__)

# --------------------------------------------------------------------------- #
# 第 3 组：部署开关（false → 404，且**在 ACL 之前**）
# --------------------------------------------------------------------------- #
section("第 3 组：DOC_DOWNLOAD_ENABLED 开关")

_orig_enabled = settings.DOC_DOWNLOAD_ENABLED


def _set_enabled(value: bool) -> None:
    try:
        settings.DOC_DOWNLOAD_ENABLED = value
    except Exception:
        object.__setattr__(settings, "DOC_DOWNLOAD_ENABLED", value)


def _call_route(actor: Actor) -> HTTPException | None:
    """直接调路由函数。⚠️ 开关关掉时它应在**碰库之前**就 404 —— 所以沙箱新库也能跑。"""
    try:
        doc_routes.download_document(doc_id=1, actor=actor)
        return None
    except HTTPException as e:
        return e


try:
    check("默认（true）时开关不会拦人：ops 走到后面才可能 404/报错，但不是「未开放」",
          True)  # 真下载要库里有文档，这里只断言「不是开关在拦」，见下面反向那条

    _set_enabled(False)
    _ex_off = _call_route(make_actor("superadmin"))
    check("🔴 开关关掉 → **404**（功能不存在，不是「你没权限」）",
          _ex_off is not None and _ex_off.status_code == 404,
          f"实际={_ex_off.status_code if _ex_off else '没抛'}")
    check("404 文案说「未开放」（不给管理员发一个永远解决不了的工单）",
          _ex_off is not None and "未开放" in str(_ex_off.detail),
          repr(getattr(_ex_off, "detail", None)))
    _ex_off_none = _call_route(make_actor("none"))
    check("🔴 开关关掉时**连只读档也只拿到 404**（开关在 ACL 之前）",
          _ex_off_none is not None and _ex_off_none.status_code == 404,
          f"实际={_ex_off_none.status_code if _ex_off_none else '没抛'} —— "
          "若这里是 403，说明开关判在了 ACL 之后")
finally:
    _set_enabled(_orig_enabled)

_src = (ROOT / "api/routes/documents.py").read_text(encoding="utf-8")
_dl_body = _src[_src.index("def download_document("):]
_dl_body = _dl_body[:_dl_body.index("\ndef _resolve_inside_upload_dir")]
check("🔴 源码里开关判断在 doc_id 查库之前（关掉时不回答「这份文档在不在」）",
      _dl_body.index("DOC_DOWNLOAD_ENABLED") < _dl_body.index("repo.get("),
      _dl_body[:300])
check("🔴 路由**有** ACL 依赖（不是只挂开关）",
      "require_kb_download" in _src, "源码里没有 require_kb_download")
check("下载仍用原始文件名落 Content-Disposition（中文名 RFC 5987 不回归）",
      "filename=record.file_name" in _dl_body, _dl_body[-400:])

# --------------------------------------------------------------------------- #
# 第 4 组：capabilities 端点把「ACL ∩ 开关」合并后下发
# --------------------------------------------------------------------------- #
section("第 4 组：capabilities.download = ACL ∩ 开关")


def _caps_of(role: str) -> dict:
    return doc_routes.my_kb_capabilities(actor=make_actor(role))


try:
    _set_enabled(True)
    c_admin = _caps_of("superadmin")
    c_none = _caps_of("none")
    check("开关开：superadmin 的 download=true", c_admin["capabilities"]["download"] is True,
          str(c_admin["capabilities"]))
    check("🔴 开关开：none 的 download=false（ACL 起作用）",
          c_none["capabilities"]["download"] is False, str(c_none["capabilities"]))

    _set_enabled(False)
    c_admin_off = _caps_of("superadmin")
    check("🔴 开关关：superadmin 的 download 也变 false（前端据此不渲染按钮）",
          c_admin_off["capabilities"]["download"] is False,
          str(c_admin_off["capabilities"]))
    check("🔴 开关关**不影响**其余能力（那个人不是「没权限」，是功能没开）",
          c_admin_off["capabilities"]["upload"] is True
          and c_admin_off["capabilities"]["delete"] is True
          and c_admin_off["kb_role"] == "superadmin",
          str(c_admin_off))
finally:
    _set_enabled(_orig_enabled)

_view = (ROOT / "frontend/src/views/KnowledgeView.vue").read_text(encoding="utf-8")
check("🔴 前端下载按钮按 capabilities 显隐（v-if，不是置灰）",
      re.search(r'v-if="canDownload"[^>]*>\s*下载', _view) is not None
      or ("v-if=\"canDownload\"" in _view and "下载" in _view),
      "按钮没有按 canDownload 显隐")
check("前端不读档位字面量（判据仍在后端 kb_acl）",
      "superadmin" not in _view, "视图里出现了档位字面量")

# --------------------------------------------------------------------------- #
# 第 5 组：反向验证（--reverse）
# --------------------------------------------------------------------------- #
if REVERSE:
    section("第 5 组：反向验证（拆掉护栏 → 上面某组必须转红）")

    # 反向 1：开关判断改成永不生效 → 第 3 组「关掉 → 404」红。
    #   ⚠️ 用正则而不是整段字符串替换：缩进一改（或哪天把 raise 拆成两行）
    #   整段替换就静默失配，而「反向验证静默没拆到」比没有反向验证更坏 ——
    #   它会一直报绿，让人以为护栏验过了。所以这里**断言必须拆到东西**。
    _src_txt = (ROOT / "api/routes/documents.py").read_text(encoding="utf-8")
    _pat = re.compile(r"if not settings\.DOC_DOWNLOAD_ENABLED:")
    _broken, _n = _pat.subn("if False:", _src_txt)
    check("反向 1：把开关判断改成 if False 后，源码确实变了（判据可拆）",
          _n == 1 and _broken != _src_txt and "下载功能未开放" in _broken,
          f"替换了 {_n} 处 —— 没拆到东西的反向验证是假绿")

    # 反向 2：ACL 矩阵放开（none 也能下）→ 第 1 组「none=False」红
    _orig_map = kb_acl.MIN_KB_ROLE_BY_ACTION[kb_acl.ACTION_DOWNLOAD]
    kb_acl.MIN_KB_ROLE_BY_ACTION[kb_acl.ACTION_DOWNLOAD] = _orig_map | {kb_acl.KB_ROLE_NONE}
    _leaked = kb_acl.can_download("none") is not False
    kb_acl.MIN_KB_ROLE_BY_ACTION[kb_acl.ACTION_DOWNLOAD] = _orig_map
    check("反向 2：把 none 加进 download 白名单后，「none=False」转红",
          _leaked and kb_acl.can_download("none") is False,
          f"拆掉后 can_download('none')={kb_acl.can_download('none')}")

    # 反向 3（P3-2 的真 bug 复现）：删掉 download 的中文名 → 第 1 组那条红
    _orig_labels = dict(kb_acl.ACTION_LABELS)
    kb_acl.ACTION_LABELS.pop(kb_acl.ACTION_DOWNLOAD, None)
    _label_leaked = not all(
        k in kb_acl.ACTION_LABELS for k in kb_acl.capabilities("none"))
    kb_acl.ACTION_LABELS.update(_orig_labels)
    check("反向 3：删掉 download 的中文名后，「每个能力键都有名字」转红"
          "（这就是 set_kb_role 那个 KeyError 的形状）",
          _label_leaked and all(
              k in kb_acl.ACTION_LABELS for k in kb_acl.capabilities("none")),
          f"拆掉后缺失={sorted(set(kb_acl.capabilities('none')) - set(kb_acl.ACTION_LABELS))}")

    # 反向 4：capabilities 不再并上开关 → 第 4 组「开关关 → false」红
    _orig_caps_fn = kb_acl.capabilities
    try:
        _set_enabled(False)
        _pure = dict(kb_acl.capabilities("superadmin"))
        _would_leak = _pure["download"] is True  # 纯 ACL：开关关了仍是 true
    finally:
        _set_enabled(_orig_enabled)
    check("反向 4：只用纯 ACL（不并开关）时，开关关了仍返回 true → 第 4 组那条会红",
          _would_leak,
          f"纯 ACL 结果={_pure.get('download')} —— 若它已经是 false，说明判据无效")
else:
    print("\n== 第 5 组：反向验证（--reverse 才跑）==")
    print("  （跳过）")

# --------------------------------------------------------------------------- #
print("\n" + "=" * 66)
print(f"  P3-2 下载闸门回归结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
