from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


try:
    import server
except ModuleNotFoundError as exc:
    print(f"[FAIL] Missing Python package: {exc.name}")
    print("Run: python -m pip install -r requirements.txt")
    raise SystemExit(2) from exc


sample = """資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
20260814,2330,1,100,"50,000",1.5
20260814,2330,2,50,"150,000",2.5
20260814,2330,9,20,"900,000",3.0
20260814,2330,10,10,"1,500,000",4.0
20260814,2330,15,2,"20,000,000",80.0
20260814,2330,16,0,0,0
20260814,2330,17,182,"22,600,000",100.0
20260814,0050,1,5,"2,500",0.1
"""

table = server._parse_tdcc_distribution_csv(("\ufeff" + sample).encode("utf-8"))
if "2330" not in table or "0050" not in table:
    fail("CSV parser did not preserve stock symbols.")

summary = server._summarize_tdcc_distribution("2026-08-14", table["2330"])
if summary["under100LotsPercent"] != 7.0:
    fail(f"Expected under-100-lots ratio 7.0, got {summary['under100LotsPercent']}")
if summary["totalPeople"] != 182:
    fail(f"Expected total people 182, got {summary['totalPeople']}")
if any(item["levelCode"] in {16, 17} for item in summary["levels"]):
    fail("Adjustment or total row was counted as a holding tier.")


async def verify_contract() -> dict:
    with (
        patch.object(
            server,
            "_get_tdcc_distribution_table",
            new=AsyncMock(return_value=table),
        ),
        patch.object(
            server,
            "_remember_tdcc_distribution",
            new=AsyncMock(return_value={"under100LotsPercent": 8.25}),
        ),
    ):
        return await server._get_distribution_data("2330", 120)


result = asyncio.run(verify_contract())
if result["under100LotsPercentChange"] != -1.25:
    fail("Previous-period change calculation is incorrect.")
if "TDCC" not in result["source"] or "無需 Token" not in result["access"]:
    fail("Output source/access fields are incorrect.")

print("[PASS] TDCC CSV parser")
print("[PASS] Under-100-lots calculation")
print("[PASS] Level 16/17 exclusion")
print("[PASS] Existing V12 output contract")
print("ALL TDCC FALLBACK TESTS PASSED")
