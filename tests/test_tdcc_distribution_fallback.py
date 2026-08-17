import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import server


SAMPLE_CSV = """資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
20260814,2330,1,100,"50,000",1.5
20260814,2330,2,50,"150,000",2.5
20260814,2330,9,20,"900,000",3.0
20260814,2330,10,10,"1,500,000",4.0
20260814,2330,15,2,"20,000,000",80.0
20260814,2330,16,0,0,0
20260814,2330,17,182,"22,600,000",100.0
20260814,0050,1,5,"2,500",0.1
"""


class TdccDistributionFallbackTests(unittest.TestCase):
    def test_parse_utf8_bom_and_keep_leading_zero_symbol(self):
        table = server._parse_tdcc_distribution_csv(
            ("\ufeff" + SAMPLE_CSV).encode("utf-8")
        )
        self.assertIn("2330", table)
        self.assertIn("0050", table)
        self.assertEqual(table["2330"][0]["date"], "2026-08-14")
        self.assertEqual(table["2330"][0]["shares"], 50_000)

    def test_summary_excludes_adjustment_and_total_rows(self):
        table = server._parse_tdcc_distribution_csv(SAMPLE_CSV)
        latest = server._summarize_tdcc_distribution(
            "2026-08-14",
            table["2330"],
        )
        # 100 張以下為第 1～9 級；樣本內 1.5 + 2.5 + 3.0。
        self.assertEqual(latest["under100LotsPercent"], 7.0)
        self.assertEqual(latest["under100LotsPeople"], 170)
        self.assertEqual(latest["totalPeople"], 182)
        self.assertEqual(latest["totalShares"], 22_600_000)
        self.assertEqual(
            [item["levelCode"] for item in latest["levels"]],
            [1, 2, 9, 10, 15],
        )

    def test_distribution_output_keeps_old_contract(self):
        table = server._parse_tdcc_distribution_csv(SAMPLE_CSV)
        with (
            patch.object(
                server,
                "_get_tdcc_distribution_table",
                new=AsyncMock(return_value=table),
            ),
            patch.object(
                server,
                "_remember_tdcc_distribution",
                new=AsyncMock(
                    return_value={"under100LotsPercent": 8.25}
                ),
            ),
        ):
            result = asyncio.run(server._get_distribution_data("2330", 120))

        self.assertEqual(result["latest"]["under100LotsPercent"], 7.0)
        self.assertEqual(result["previousUnder100LotsPercent"], 8.25)
        self.assertEqual(result["under100LotsPercentChange"], -1.25)
        self.assertIn("TDCC", result["source"])
        self.assertIn("無需 Token", result["access"])


if __name__ == "__main__":
    unittest.main()
