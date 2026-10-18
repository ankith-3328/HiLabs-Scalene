import csv
import io
import json

from network_mapping import npi_registry
from network_mapping.rate_file import (
    choose_rate,
    collector,
    flatten,
    is_outlier,
    iter_top_level,
    rate_rows,
    service_medians,
)

DOC = {
    "reporting_entity_name": "Centene", "version": "2.0.0",
    "provider_references": [
        {"provider_group_id": 1, "network_name": ["PPO", "HMO"],
         "provider_groups": [{"npi": [1111111111, 2222222222],
                              "tin": {"type": "ein", "value": "68-0273974", "business_name": "Sutter PT"}}]},
        {"provider_group_id": 2, "new_ref_field": "r",
         "provider_groups": [{"npi": [1111111111], "tin": {"type": "ein", "value": "68-0273974"}}]},
    ],
    "in_network": [
        {"negotiation_arrangement": "ffs", "billing_code_type": "CPT", "billing_code": "99213",
         "description": "OFFICE VISIT", "bundled_codes": [{"billing_code": "x"}],
         "negotiated_rates": [
             {"provider_references": [1], "negotiated_prices": [
                 {"negotiated_rate": 100.00, "service_code": ["11", "22"], "brand_new_field": "z"}]},
             # same NPI and TIN under another contract with a different rate: a conflict
             {"provider_references": [2], "negotiated_prices": [{"negotiated_rate": 80.00}]},
             {"provider_references": [404], "negotiated_prices": [{"negotiated_rate": 1}]},
             {"provider_groups": [{"npi": [4444444444], "tin": {"value": "11-1"}}],
              "negotiated_prices": [{"negotiated_rate": 2}]},
         ]},
    ],
}


def run(tmp_path, doc=None):
    src = tmp_path / "rate.json"
    src.write_text(json.dumps(DOC if doc is None else doc))
    groups, names = {}, {}
    stats = flatten(src, tmp_path / "rate.csv", on_row=collector(groups, names))
    return stats, list(csv.DictReader(open(tmp_path / "rate.csv", encoding="utf-8"))), groups, names


def test_iter_top_level_streams_items():
    raw = b'{"a": 1, "arr": [{"x": [1, {"y": 2}]}, 3], "obj": {"k": "v"}}'
    assert list(iter_top_level(io.BytesIO(raw))) == [
        ("a", None, 1), ("arr", 0, {"x": [1, {"y": 2}]}), ("arr", 1, 3), ("obj", None, {"k": "v"})]


def test_flatten_keeps_every_field_one_row_per_price_per_npi(tmp_path):
    stats, rows, _, names = run(tmp_path)
    assert list(rows[0]) == [
        "reporting_entity_name", "version",
        "provider_group_id", "network_name", "tin.type", "tin.value", "tin.business_name", "npi", "new_ref_field",
        "negotiation_arrangement", "billing_code_type", "billing_code", "description", "bundled_codes",
        "provider_references", "negotiated_rate", "service_code", "brand_new_field"]
    # 2 NPIs (ref 1) + 1 NPI (ref 2) + 1 unresolved ref + 1 inline group
    assert stats["rows"] == len(rows) == 5 and stats["prices"] == 4
    first = rows[0]
    assert (first["npi"], first["tin.business_name"], first["network_name"]) == ("1111111111", "Sutter PT", "PPO|HMO")
    assert (first["negotiated_rate"], first["service_code"], first["brand_new_field"]) == ("100.0", "11|22", "z")
    assert json.loads(first["bundled_codes"]) == [{"billing_code": "x"}]
    assert (rows[3]["provider_group_id"], rows[3]["npi"]) == ("404", "")  # unresolved, kept
    assert stats["unresolved_provider_references"] == ["404"]
    assert (rows[4]["npi"], rows[4]["tin.value"]) == ("4444444444", "11-1")  # inline group
    assert names == {"68-0273974": {"Sutter PT"}}


def test_same_field_at_two_levels_is_prefixed(tmp_path):
    doc = {"provider_references": [{"provider_group_id": 1, "description": "g",
                                    "provider_groups": [{"npi": [1], "tin": {}}]}],
           "in_network": [{"description": "s", "negotiated_rates": [
               {"provider_references": [1], "negotiated_prices": [{"negotiated_rate": 1}]}]}]}
    _, [row], _, _ = run(tmp_path, doc)
    assert (row["provider.description"], row["service.description"]) == ("g", "s")


def test_rate_rows_one_per_npi_tin_address_code_with_conflicts_summarised(tmp_path):
    _, _, groups, _ = run(tmp_path)
    providers = {"1111111111": {"npi_type": "Type 2 (organization)", "name": "SUTTER", "specialty": "Lab",
                                "note": ""}}
    two_sites = [{"Address": a, "Confidence": "high", "Evidence": "e", "Note": ""}
                 for a in ("1 MAIN ST, SACRAMENTO, CA 95816", "9 OAK ST, DAVIS, CA 95616")]
    rows = rate_rows(groups, providers, {("68-0273974", "1111111111"): two_sites}, service_medians(groups))
    visits = [r for r in rows if r["NPI"] == "1111111111"]
    assert [r["Address"] for r in visits] == [site["Address"] for site in two_sites]  # one row per address
    v = visits[0]
    # $100 and $80 in one contract each: a tie, so the lower median, a published rate (not $90)
    assert v["Rate"] == "80.0"
    assert v["Rate Choice"] == "tie between 2 rates: lower median"
    assert (v["Min Rate"], v["Max Rate"]) == ("80.0", "100.0")
    assert (v["Specialty"], v["Billing Code"], v["Address Confidence"]) == ("Lab", "99213", "high")
    by_npi = {r["NPI"]: r for r in rows}
    # the typical price for 99213 here is $80: the $1 and $2 rates are under 1/10 of it, so excluded
    assert (by_npi[""]["Rate"], by_npi["4444444444"]["Rate"]) == ("", "")
    assert (by_npi["4444444444"]["Min Rate"], by_npi["4444444444"]["Max Rate"]) == ("", "")
    assert "1 outlier" in by_npi[""]["Note"]
    assert (by_npi["2222222222"]["Address"], by_npi["2222222222"]["Address Confidence"]) == ("", "none")


ORG = {"results": [{
    "enumeration_type": "NPI-2", "basic": {"organization_name": "SUTTER VALLEY MEDICAL FOUNDATION"},
    "addresses": [
        {"address_purpose": "MAILING", "address_1": "PO BOX 1", "city": "SACRAMENTO", "state": "CA"},
        {"address_purpose": "LOCATION", "address_1": "1625 STOCKTON BLVD", "address_2": "STE 208",
         "city": "SACRAMENTO", "state": "CA", "postal_code": "958167092"}],
    "taxonomies": [{"desc": "Pathology", "primary": False},
                   {"desc": "Clinical Medical Laboratory", "primary": True}]}]}


def test_nppes_uses_practice_locations_and_primary_taxonomy():
    p = npi_registry.parse({"results": [{**ORG["results"][0],
                                  "other_names": [{"organization_name": "SUTTER LAB"}],
                                  "practiceLocations": [{"address_1": "9 OAK ST", "city": "DAVIS", "state": "CA",
                                                         "postal_code": "95616"}]}]})
    assert (p["npi_type"], p["name"], p["specialty"], p["note"]) == (
        "Type 2 (organization)", "SUTTER VALLEY MEDICAL FOUNDATION", "Clinical Medical Laboratory", "")
    assert p["names"] == ["SUTTER VALLEY MEDICAL FOUNDATION", "SUTTER LAB"]
    assert [(loc["address"], loc["source"]) for loc in p["locations"]] == [
        ("1625 STOCKTON BLVD STE 208, SACRAMENTO, CA 95816-7092", "NPPES primary practice location"),
        ("9 OAK ST, DAVIS, CA 95616", "NPPES secondary practice location")]  # mailing address not used


def test_nppes_edge_cases_are_noted():
    individual = npi_registry.parse({"results": [{
        "enumeration_type": "NPI-1", "basic": {"first_name": "JANE", "last_name": "DOE", "credential": "MD"},
        "addresses": [{"address_purpose": "LOCATION", "address_1": "1 A ST", "city": "RENO", "state": "NV"}],
        "taxonomies": [{"code": "2086S0129X", "desc": None, "primary": True}]}]})
    assert (individual["npi_type"], individual["name"], individual["specialty"]) == (
        "Type 1 (individual)", "JANE DOE MD", "taxonomy 2086S0129X")
    assert "without a description" in individual["note"] and individual["locations"][0]["state"] == "NV"
    assert "not found" in npi_registry.parse({"results": []})["note"]
    assert "rejected" in npi_registry.parse({"Errors": [{"description": "NPI must be 10 digits"}]})["note"]
    assert "invalid NPI" in npi_registry.lookup("0")["note"]  # never sent to the API


def test_outlier_rule():
    from decimal import Decimal as D
    assert is_outlier(D("1500"), D("100")) and is_outlier(D("9"), D("100"))
    assert not is_outlier(D("1000"), D("100")) and not is_outlier(D("10"), D("100"))  # exactly 10x is kept
    assert not is_outlier(D("5"), None)  # no typical price (fewer than 3 peers): never flagged


def test_revenue_code_rates_are_never_judged_as_outliers(tmp_path):
    rc = {"billing_code_type": "RC", "billing_code": "300", "description": "LAB", "negotiated_rates": [
        {"provider_references": [1], "negotiated_prices": [{"negotiated_rate": r}]} for r in (5, 100, 110, 8000)]}
    doc = {"provider_references": [{"provider_group_id": 1, "provider_groups": [
        {"npi": [1111111111], "tin": {"value": "11-1"}}]}], "in_network": [rc]}
    _, _, groups, _ = run(tmp_path, doc)
    assert service_medians(groups) == {}  # a revenue code is a cost centre, not one service
    [row] = rate_rows(groups, {}, {}, {})
    assert "outlier" not in row["Note"]


def test_choose_rate_steps():
    from decimal import Decimal as D
    svc = ("CPT", "71045", "professional", "")
    std = ("professional", "outpatient")

    def v(rate, modifier="", cls="professional", setting="outpatient"):
        return (D(rate), svc, modifier, cls, setting)

    # one rate
    assert choose_rate({v("40"): {"1", "2"}}, std) == (D("40"), "only rate")
    # 1. the standard version: the global fee wins over the professional-component (-26) fee
    assert choose_rate({v("40"): {"1"}, v("12", modifier="26"): {"2", "3"}}, std)[0] == D("40")
    # 1. then the code's usual class and setting
    assert choose_rate({v("40"): {"1"}, v("900", cls="institutional", setting="inpatient"): {"2"}}, std)[0] == D("40")
    # 2. the rate found in the most contracts
    assert choose_rate({v("40"): {"1", "2", "3"}, v("55"): {"4"}}, std) == (D("40"), "most common rate (in 3 of 4 contracts)")
    # 3. a tie: the lower median of the tied rates, always a published rate
    assert choose_rate({v("40"): {"1"}, v("50"): {"2"}, v("60"): {"3"}, v("70"): {"4"}}, std) == (
        D("50"), "tie between 4 rates: lower median")
