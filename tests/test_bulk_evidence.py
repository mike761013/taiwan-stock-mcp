from datetime import date
import pytest
from stock_db.bulk_evidence import parse_revenue_html, compare_official_archive, validate_ownership

HTML="""上市公司115年8月份 單位：千元<table><tr><td>公司代號</td><td>公司名稱</td><td>當月營收</td></tr><tr><td>2355</td><td>敬鵬</td><td>1,000</td><td>800</td><td>500</td><td>25</td><td>100</td><td>0</td><td>0</td><td>0</td></tr></table>"""

def test_month_units_and_derived_growth():
    row=parse_revenue_html(HTML,date(2026,8,1),"sii")[0]
    assert row[:5]==("2355",date(2026,8,1),1000000,25,100)

def test_wrong_month_and_schema_rejected():
    with pytest.raises(ValueError):parse_revenue_html(HTML,date(2026,7,1),"sii")
    with pytest.raises(ValueError):parse_revenue_html(HTML.replace("公司代號","代號"),date(2026,8,1),"sii")

def test_archive_verification_requires_matching_official_values():
    rows={str(s):dict(snapshotDate="2026-10-02",under100LotsPercent=20,over400LotsPercent=70,holderCount=300) for s in range(1000,2000)}
    assert compare_official_archive(rows,rows)["matchedSymbols"]==1000
    bad={s:dict(v,over400LotsPercent=40) for s,v in rows.items()}
    with pytest.raises(ValueError):compare_official_archive(rows,bad)
    with pytest.raises(ValueError):compare_official_archive({},rows)

def test_ownership_stale_and_impossible_percentages_rejected():
    head="資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
    csv=head+"".join(f"20261002,{s},1,1,1,80\n20261002,{s},12,1,1,70\n" for s in range(1000,2000))
    with pytest.raises(ValueError):validate_ownership(csv,date(2026,9,24))
    with pytest.raises(ValueError):validate_ownership(csv,date(2026,10,2))
