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


def test_supplement_requires_all_five_categories_and_only_requested_dates():
    from stock_db.bulk_evidence import supplemental_institutional_rows
    names=["Foreign_Investor","Foreign_Dealer_Self","Investment_Trust","Dealer_self","Dealer_Hedging"]
    raw=[dict(stock_id="2355",date="2026-10-08",name=n,buy=100,sell=20) for n in names]
    assert supplemental_institutional_rows(raw,"2355",["2026-10-08"])==[("2355",date(2026,10,8),400)]
    assert supplemental_institutional_rows(raw[:-1],"2355",["2026-10-08"])==[]
    assert supplemental_institutional_rows(raw,"2355",["2026-10-07"])==[]
    raw.append(dict(stock_id="2355",date="2026-10-08",name="Total",buy=99999,sell=0))
    assert supplemental_institutional_rows(raw,"2355",["2026-10-08"])[0][-1]==400


def test_wide_supplement_accepts_explicit_zero_but_rejects_missing_columns():
    from stock_db.bulk_evidence import supplemental_institutional_rows
    names=["Foreign_Investor","Foreign_Dealer_Self","Investment_Trust","Dealer_self","Dealer_Hedging"]
    row=dict(stock_id="1813",date="2026-10-08",**{n+suffix:0 for n in names for suffix in ("_buy","_sell")})
    assert supplemental_institutional_rows([row],"1813",["2026-10-08"])==[("1813",date(2026,10,8),0)]
    row.pop("Investment_Trust_sell")
    assert supplemental_institutional_rows([row],"1813",["2026-10-08"])==[]
