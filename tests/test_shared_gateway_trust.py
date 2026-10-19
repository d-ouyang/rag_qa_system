"""
共享办公网关信任（additive）—— verify_gateway_proof 认「RAG 自己网关密钥」+「共享办公网关密钥」。

背景：RAG 与 office 助手等共用一套 API 网关（office 仓《共享网关迁移设计》，认证放 B）。
该网关转发到 RAG 后端时带 X-Internal-Auth，后端验过才认身份头。这里把可接受的网关证明
从一把扩为两把（OFFICE_GATEWAY_SHARED_SECRET 可独立轮换），**additive + fail-closed**：
  · 两把都配 → 各自被接受；
  · 只配 RAG 自己 / 没配 office → office 不被信任（不悄悄放行）；
  · 一把都没配 / 证明不匹配 / 缺头 → 一律 401。

纯函数级（不连库）：只验「证明过不过」，身份解析（要库 + 身份对齐）属 office 侧。
运行：.venv/bin/python tests/test_shared_gateway_trust.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from config.settings import settings  # noqa: E402
from core import identity as ident  # noqa: E402

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


def expect_pass(name: str, proof) -> None:
    try:
        ident.verify_gateway_proof(proof)
        check(name, True)
    except Exception as e:  # noqa: BLE001
        check(name, False, f"{type(e).__name__}: {e}")


def expect_401(name: str, proof) -> None:
    try:
        ident.verify_gateway_proof(proof)
        check(name, False, "未抛异常（应拒绝）")
    except HTTPException as e:
        check(name, e.status_code == 401, f"status={e.status_code}")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"抛了非 HTTPException：{type(e).__name__}: {e}")


RAG_SECRET = "rag-own-gateway-secret-xyz"
OFFICE_SECRET = "office-shared-gateway-secret-abc"
ORIG_RAG = settings.INTERNAL_SHARED_SECRET
ORIG_OFFICE = settings.OFFICE_GATEWAY_SHARED_SECRET

try:
    print("=" * 66)
    print("  共享办公网关信任（additive 信任 + fail-closed）")
    print("=" * 66)

    # 场景 1：两把都配 → 各自被接受；错的/缺的 → 401
    settings.INTERNAL_SHARED_SECRET = RAG_SECRET
    settings.OFFICE_GATEWAY_SHARED_SECRET = OFFICE_SECRET
    expect_pass("RAG 自己网关密钥仍被接受（老路不回归）", RAG_SECRET)
    expect_pass("共享办公网关密钥被接受（additive 信任）", OFFICE_SECRET)
    expect_401("错误密钥 → 401（fail-closed）", "wrong-secret")
    expect_401("缺证明头 → 401（fail-closed）", None)

    # 场景 2：只配 RAG 自己、没配 office → office 不被信任
    settings.INTERNAL_SHARED_SECRET = RAG_SECRET
    settings.OFFICE_GATEWAY_SHARED_SECRET = ""
    expect_pass("未配 office 时老路仍过", RAG_SECRET)
    expect_401("未配 office → office 密钥不被信任（不悄悄放行）", OFFICE_SECRET)

    # 场景 3：一把都没配 = 部署错误 → 一律拒
    settings.INTERNAL_SHARED_SECRET = ""
    settings.OFFICE_GATEWAY_SHARED_SECRET = ""
    expect_401("一把都没配 → 任何证明都拒（fail-closed）", "anything")
finally:
    settings.INTERNAL_SHARED_SECRET = ORIG_RAG
    settings.OFFICE_GATEWAY_SHARED_SECRET = ORIG_OFFICE

print("\n" + "=" * 66)
print(f"  共享网关信任结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
