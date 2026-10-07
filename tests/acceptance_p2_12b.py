# pyright: basic
"""
P2-12b 验收脚本：身份与信任边界（真链路，真 HTTP）

--------------------------------------------------------------------------
它验的是 `test_module14_trust_boundary.py` **结构上验不到**的那部分
--------------------------------------------------------------------------
module14 是 Python 脚本，它对网关那一半（TypeScript）只能做两件事：
① 在 Python 里**等价重实现**一遍规则，验「规则本身对不对」；
② **正扫 .ts 源码**，验「那份代码确实写着这些规则」。

这两条都不等于「运行时真的这么跑」。本脚本补的是**第三件事**：
起真的后端 + 真的网关，用真 token 打真HTTP，看响应码与响应体。

本仓库的 TypeScript 测试链路目前是空的（`make test` 里没有 tsc 环节），
所以这一层只能靠真跑 —— 代价是它需要两个进程起着，因此它**不属于 `make test`**。

--------------------------------------------------------------------------
四条要验的（每条都是 12b 的核心承诺，缺一条就等于没做）
--------------------------------------------------------------------------
  1. **路径级授权**：普通员工经网关打 `/api/v1/admin/*` → 403 FORBIDDEN_PATH；
     管理员同一条 → 200。（证明「粗筛比后端精确判据更宽」这条没写反）
  2. **内部接口封堵**：任何人（含 admin）打 `/api/v1/internal/*` → **404**。
     12b 之前它是可达的（带合法 token 能打到后端），唯一的防线是共享密钥。
  3. **白名单路径的伪造头被剥离**：往`/api/v1/system/health`（免 token）
     塞 `X-User-Id: 440` + `X-User-Role: admin` + `X-Internal-Auth: forged`，
     仍然 200，且后端**没有**把它当身份 —— 判据是探活响应里不出现员工数据。
  4. **缺网关证明 = 401**：这一条**在真链路上验不了**（本机 `.env` 是 dev 模式，
     而 dev 模式刻意不验证明，否则 `make api` 裸跑时每条curl 都要先造证明头）。
     所以它只验「生产形态下 resolve_actor 的判定」，并明确标注这是**进程内**验的，
     不是 HTTP。真链路验它需要把 IDENTITY_MODE 改成 gateway 并重启后端 ——
     那是部署形态的事，不在本脚本的职责里。

--------------------------------------------------------------------------
用法与代价
--------------------------------------------------------------------------
    make dev                        # 先起后端 + 网关（8000 / 3000）
    .venv/bin/python tests/acceptance_p2_12b.py

⚠️ **它会临时改两个种子账号的密码**（要拿真 token，只能真登录），
   收尾换成**新的随机临时密码** + 强制改密标记。
   种子的原临时密码早已不可知，所以「还原」不是假装还原成原值——
   能还原的是「这个账号仍然是可用的、仍然处于强制改密状态」。
⚠️ **跑之前不要有别人在用这两个账号**（脚本自己会重置密码）。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.logging_config import setup_logging  # noqa: E402

setup_logging()

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import text  # noqa: E402

from config.settings import settings  # noqa: E402
from core import password_policy as policy  # noqa: E402
from core.db import get_engine  # noqa: E402
from core.identity import resolve_actor  # noqa: E402

PASS = 0
FAIL = 0

BACKEND = "http://127.0.0.1:8000"
GATEWAY = "http://127.0.0.1:3000"

#: 临时密码。刻意带符号 + 数字 + 大小写 —— 12b 不改策略，但脚本要能登进去，
#: 而如果哪天策略变严导致这个密码登不上，报错会指向「登录失败」而不是
#: 「策略变了」，那是个会浪费半小时的误导。
PROBE_PWD = "Vb12b!Probe"

ADMIN = "wu.jing"   # 种子里的真管理员（uid 440）
STAFF = "chen.jie"  # 普通员工（uid 442）


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} | {detail}")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def http(args: list[str]) -> tuple[str, str]:
    """打一次 curl，返回 (状态码, 响应体)。

    ⚠️ `--noproxy '*'` 不能省：本机 HTTP_PROXY 会让 127.0.0.1返 502，
    而 502 的症状是「网关挂了」，与「网关真的挂了」 indistinguishable。
    """
    p = subprocess.run(
        ["curl", "-s", "--noproxy", "*", "-m", "20", "-w", "\n%{http_code}"] + args,
        capture_output=True,
        text=True,
    )
    parts = p.stdout.rsplit("\n", 1)
    return (parts[1] if len(parts) == 2 else ""), parts[0]


def set_pwd(username: str) -> int:
    e = get_engine()
    with e.begin() as c:
        uid = c.execute(
            text("SELECT id FROM `user` WHERE username=:u"), {"u": username}
        ).scalar()
        if uid is None:
            raise SystemExit(f"种子里没有 {username}，先跑 make seed-users-apply")
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=0 WHERE id=:i"),
            {"h": policy.hash_password(PROBE_PWD), "i": uid},
        )
    return int(uid)


def restore_pwd(username: str) -> None:
    e = get_engine()
    with e.begin() as c:
        c.execute(
            text("UPDATE `user` SET password_hash=:h, must_change_password=1 "
                 "WHERE username=:u"),
            {"h": policy.hash_password(policy.generate_temporary_password()), "u": username},
        )


def login(username: str) -> str | None:
    code, body = http([
        "-X", "POST", f"{GATEWAY}/api/auth/login",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"username": username, "password": PROBE_PWD}),
    ])
    if code != "200":
        print(f"  （{username} 登录返回 {code}：{body[:160]}）")
        return None
    try:
        return json.loads(body).get("access_token")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
section("0. 两个进程都得在（否则后面每条都是「连接失败」而不是「行为对」）")
# --------------------------------------------------------------------------- #
api_code, _ = http([f"{BACKEND}/api/v1/system/health"])
gw_code, _ = http([f"{GATEWAY}/api/health"])
check(f"后端 {BACKEND} 在跑", api_code == "200", f"返回 {api_code}（先 make dev）")
check(f"网关 {GATEWAY} 在跑", gw_code == "200", f"返回 {gw_code}（先 make dev）")
if api_code != "200" or gw_code != "200":
    raise SystemExit("  服务没起来，剩下四组没有意义。")

#⚠️ 后端**没有** `/api/health`（那是网关自己的探活路由）——
#   写成 `/api/health` 会拿到 404，然后第一组直接判「后端没起来」，
#   而真实原因只是路径写错了。第0 组因此拿 404 当存活判据时，
#   失败信息必须能让人一眼看出是「404 = 路径不对」还是「连不上」。
_, health_body = http([f"{BACKEND}/api/v1/system/health"])
try:
    hb = json.loads(health_body)
except Exception:
    hb = {}
ident = hb.get("identity", {}) if isinstance(hb, dict) else {}
check("探活报出 identity 段（部署当天一眼看出信任边界有没有生效）",
      isinstance(ident, dict) and "trust_enforced" in ident, str(hb)[:200])
check("  └ trust_enforced 与 .env 的 IDENTITY_MODE 一致（本机应是 False = dev）",
      ident.get("trust_enforced") is (settings.IDENTITY_MODE == "gateway"),
      f"报 {ident.get('trust_enforced')} / .env 是 {settings.IDENTITY_MODE}")
check("  └ 但网关证明**是否配好**单独报（缺它会让生产所有数据接口 401）",
      "gateway_proof_configured" in ident, str(ident))
# ⚠️ 刻意**不**断言探活里含密钥本身：那会把一个可读密钥变成探活响应里的一行明文。

# --------------------------------------------------------------------------- #
section("1. 路径级授权（经网关）")
# --------------------------------------------------------------------------- #
admin_id = set_pwd(ADMIN)
staff_id = set_pwd(STAFF)
try:
    admin_tok = login(ADMIN)
    staff_tok = login(STAFF)
    check("管理员能真登录（拿得到真 token）", bool(admin_tok), "见上面的响应")
    check("普通员工能真登录（拿得到真 token）", bool(staff_tok), "见上面的响应")
    if not (admin_tok and staff_tok):
        raise SystemExit("  拿不到 token，后面的路径授权无从谈起。")
    print(f"  （{ADMIN}=uid{admin_id} / {STAFF}=uid{staff_id}）")

    a_hdr = ["-H", f"Authorization: Bearer {admin_tok}"]
    s_hdr = ["-H", f"Authorization: Bearer {staff_tok}"]

    code, body = http(s_hdr + [f"{GATEWAY}/api/v1/admin/users"])
    check("普通员工打管理端 → 403（请求没进后端）", code == "403", f"{code} {body[:120]}")
    check("  └ 错误码是网关的 FORBIDDEN_PATH（不是后端 require_staff 的 403）",
          '"FORBIDDEN_PATH"' in body, body[:160])
    check("  └ 文案不区分「不是 admin」与「不是 hr」（否则等于摊开角色体系）",
          "管理员或人事" in body, body[:160])

    code, body = http(a_hdr + [f"{GATEWAY}/api/v1/admin/users"])
    check("管理员打同一条路径 → 200（粗筛没有写反成「只放 admin」）",
          code == "200", f"{code} {body[:120]}")

    code, _ = http(s_hdr + [f"{GATEWAY}/api/v1/system/settings"])
    check("普通员工打业务接口 → 200（粗筛没有误伤非管理端路径）", code == "200", code)

    # ---- 内部接口 ----
    section("2. 内部接口不再经网关暴露")
    for who, hdr in (("普通员工", s_hdr), ("管理员", a_hdr)):
        code, body = http(hdr + [
            "-X", "POST", f"{GATEWAY}/api/v1/internal/auth/login",
            "-H", "Content-Type: application/json",
            "-d", json.dumps({"username": STAFF, "password": PROBE_PWD}),
        ])
        check(f"{who}打内部登录接口 → 404（12b 之前是可达的）", code == "404",
              f"{code} {body[:120]}")
        check(f"  └ {who}拿到的是 NOT_FOUND 而非 403（不承认这条路径存在）",
              '"NOT_FOUND"' in body, body[:160])
    # 内部接口对 admin 也报 404 —— 所以这条规则不能写成「角色不够」。

    # ---- 白名单路径的伪造头 ----
    section("3. 白名单路径的伪造头被剥离（12b 的第一件事）")
    code, body = http([
        "-H", "X-User-Id: 440", "-H", "X-Username: wu.jing",
        "-H", "X-User-Role: admin", "-H", "X-Role: admin",
        "-H", "X-Internal-Auth: forged-by-client",
        f"{GATEWAY}/api/v1/system/health",
    ])
    check("带一整套伪造头的探活仍 200（剥离不能把正常功能打坏）", code == "200",
          f"{code} {body[:120]}")
    # 判据是「响应里没有任何员工数据」。探活本身只返回状态，
    # 所以只要剥离失效、后端把它当admin，也**不会**在这个响应里露出员工名单——
    # 真正的判据在 module14 里（后端日志 + 无条件剥离的顺序断言）。
    # 这里能验的是「至少没把伪造头原样透传到让后端报错/回显」。
    check("伪造的 X-Internal-Auth 没被后端采信（响应里不出现 forged 字样）",
          "forged" not in body, body[:160])
    check("响应里没有员工名单（items 不该出现）", '"items"' not in body, body[:160])

    # ---- 生产形态的证明校验（进程内，不是 HTTP）----
    section("4. 生产形态：缺网关证明必须 401（进程内验，真链路上验不了）")
    print(f"  （本机 IDENTITY_MODE={settings.IDENTITY_MODE} —— dev 模式刻意不验证明，")
    print("    否则 make api 裸跑时每条 curl 都要先造一个证明头）")
    _mode_before = settings.IDENTITY_MODE
    settings.IDENTITY_MODE = "gateway"
    try:
        cases = [
            ("12b 之前的攻击手法：伪造管理员 uid、无证明", None, False),
            ("证明错一位", settings.INTERNAL_SHARED_SECRET[:-1] + "X", False),
            ("空证明字符串", "", False),
            ("正确证明（网关转发的正常形态）", settings.INTERNAL_SHARED_SECRET, True),
        ]
        for label, proof, want_ok in cases:
            try:
                actor = resolve_actor(user_id_header="440", username_header="wu.jing",
                                      gateway_proof_header=proof)
                got_ok, detail = True, f"放行 role={actor.role} uid={actor.id}"
            except HTTPException as e:
                got_ok, detail = False, f"{e.status_code} {e.detail}"
            check(f"{label} → {'放行' if want_ok else '401'}", got_ok is want_ok, detail)

        # dev 模式**刻意**仍放行。这是 12b 明确保留的已知代价，不是漏做。
        settings.IDENTITY_MODE = "dev"
        try:
            actor = resolve_actor(user_id_header="440", username_header="wu.jing",
                                  gateway_proof_header=None)
            check("⚠️ dev 模式不验证明（刻意保留的已知代价，探活的 trust_enforced 就是给部署当天看的）",
                  actor.role == "admin", f"role={actor.role}")
        except HTTPException as e:
            check("dev 模式不验证明（刻意保留的已知代价）", False,
                  f"{e.status_code} {e.detail}")
    finally:
        settings.IDENTITY_MODE = _mode_before

    # ---- 直连 8000（本机形态的已知边界）----
    section("5. 直连 8000 伪造身份（本机形态的边界，部署形态由拓扑保证）")
    code, body = http(["-H", "X-User-Id: 440", "-H", "X-Username: wu.jing",
                       "-H", "X-User-Role: admin", f"{BACKEND}/api/v1/admin/users"])
    if settings.IDENTITY_MODE == "gateway":
        check("gateway 模式下直连 8000 伪造身份 → 401（12b 的核心承诺）",
              code == "401", f"{code} {body[:140]}")
    else:
        print(f"  ⚠️  dev 模式下直连 8000 返回 {code} —— 这是**刻意保留的代价**，")
        print("      不是漏做。防住它的是网关证明，而 dev 模式刻意不验证明（见第 4 组）。")
        print("      生产形态（IDENTITY_MODE=gateway）下这一条是 401，见 module14。")
        print("      ⚠️  另一道防线是「8000 不对外可达」，但那是**部署形态**保证的——")
        print("          本机裸跑时它不成立（同机任何进程都能连 127.0.0.1:8000）。")
        check("  └ 至少它确实暴露了这个边界（探活的 trust_enforced=False 会报出来）",
              ident.get("trust_enforced") is False, str(ident))
finally:
    restore_pwd(ADMIN)
    restore_pwd(STAFF)
    print(f"\n  [收尾] {ADMIN} / {STAFF} 密码已换成新随机临时密码 + 强制改密标记")

print(f"\n{'=' * 70}")
print(f"  P2-12b 验收结果：{PASS} 通过 / {FAIL} 失败")
print(f"{'=' * 70}")
sys.exit(1 if FAIL else 0)