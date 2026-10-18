import json
from datetime import date, timedelta

from rate_pipeline import address_linking
from rate_pipeline.address_linking import link, location, person_in, same_org


def test_location_key_ignores_unit_and_spelling():
    a = location("1625 Stockton Boulevard, Suite 208", "", "Sacramento", "CA", "958167092", "s")
    b = location("1625 STOCKTON BLVD #210", "", "SACRAMENTO", "CA", "95816", "s")
    assert a["key"] == b["key"] == "1625 STOCKTON BLVD 95816"
    assert a["address"] == "1625 Stockton Boulevard, Suite 208, Sacramento, CA 95816-7092"


def test_name_matching():
    assert same_org("Change Within Reach,Inc.", "CHANGE WITHIN REACH INC")
    assert same_org("LABORATORY CORPORATION OF AMERICA", "LABORATORY CORPORATION OF AMER")  # truncated
    assert not same_org("Sutter Physical & Hand Therapy - Elk Grove", "SUTTER VALLEY MEDICAL FOUNDATION")
    assert person_in("DOUGLAS TAYLOR", "TAYLOR, D.P.M.")
    assert person_in("SHEANIA COLEMAN-DORSEY", "COLEMAN DORSEY, SHEANIA")
    assert not person_in("JOHN SMITH", "SMITH & JONES ORTHOPEDIC SURGERY MEDICAL GROUP OF NORTHERN CALIFORNIA")


RECENT = (date.today() - timedelta(days=30)).isoformat()


def loc(street, source, group="", state="CA", zip_code="90001"):
    return location(street, "", "LOS ANGELES", state, zip_code, source, group)


def test_one_provider_many_addresses_each_kept_only_for_its_own_tin():
    # A doctor practises for two groups (two TINs) at two addresses, per CMS Doctors & Clinicians.
    providers = {"1111111111": {"npi_type": "Type 1 (individual)", "name": "JANE DOE",
                                "locations": [loc("1 MAIN ST", "NPPES primary practice location")]}}
    dac = {"1111111111": [loc("1 MAIN ST", "CMS Doctors & Clinicians", "MAIN STREET CLINIC INC"),
                          loc("9 OAK AVE", "CMS Doctors & Clinicians", "OAK SURGERY CENTER LLC")]}
    tin_names = {"11-1": {"Main Street Clinic"}, "22-2": {"Oak Surgery Center"}}
    rows, _ = link(tin_names, {("11-1", "1111111111"), ("22-2", "1111111111")}, providers, dac, {}, "CA")
    got = {(r["TIN"], r["Address"].split(",")[0], r["Confidence"]) for r in rows}
    # each TIN gets only the address where CMS shows the doctor working for that group
    assert got == {("11-1", "1 MAIN ST", "high"), ("22-2", "9 OAK AVE", "high")}


def test_one_tin_many_addresses_and_colocation():
    # Two NPIs under one TIN: a shared site corroborates the TIN; an organization NPI whose
    # name matches the TIN confirms its own sites; IRS gives the TIN's registered address.
    providers = {
        "1111111111": {"npi_type": "Type 1 (individual)",
                       "locations": [loc("5 ELM ST", "NPPES primary practice location")]},
        "2222222222": {"npi_type": "Type 2 (organization)", "names": ["ACME HEALTH INC"], "last_updated": RECENT,
                       "locations": [loc("5 ELM ST", "NPPES primary practice location"),
                                     loc("7 PINE ST", "NPPES secondary practice location")]},
    }
    irs = {"33-3": {"name": "ACME HEALTH", "location": loc("100 HQ BLVD", "IRS registered address")}}
    rows, tin_rows = link({"33-3": {"Acme Health, Inc."}}, {("33-3", "1111111111"), ("33-3", "2222222222")},
                          providers, {}, irs, "CA")
    got = {(r["NPI"], r["Address"].split(",")[0], r["Confidence"]) for r in rows}
    assert got == {("1111111111", "5 ELM ST", "high"),     # two NPIs agree: tied and current
                   ("2222222222", "5 ELM ST", "high"),
                   ("2222222222", "7 PINE ST", "medium")}  # tied by name, NPPES only (recent record)
    assert "shared by 2 NPIs" in next(r["Evidence"] for r in rows if r["NPI"] == "1111111111")
    assert [(r["TIN"], r["Address"].split(",")[0]) for r in tin_rows] == [("33-3", "100 HQ BLVD")]


def test_unconfirmed_and_missing_addresses_are_flagged_not_dropped():
    providers = {"1111111111": {"npi_type": "Type 1 (individual)", "note": "",
                                "locations": [loc("1 A ST", "NPPES primary practice location", state="NV")]},
                 "0": {"note": "invalid NPI in rate file (not 10 digits); not looked up"}}
    rows, _ = link({}, {("11-1", "1111111111"), ("11-1", "0")}, providers, {}, {}, "CA")
    by_npi = {r["NPI"]: r for r in rows}
    assert by_npi["1111111111"]["Confidence"] == "low"
    assert "not tied to this TIN" in by_npi["1111111111"]["Note"]
    assert "outside CA (NV)" in by_npi["1111111111"]["Note"]
    assert (by_npi["0"]["Address"], by_npi["0"]["Confidence"]) == ("", "none")


def test_cms_national_file_is_streamed_for_npis_and_active_sites(monkeypatch):
    national = ("NPI,Facility Name,adr_ln_1,adr_ln_2,City/Town,State,ZIP Code\n"
                "1111111111,MAIN STREET CLINIC INC,1 MAIN ST,STE 5,LOS ANGELES,CA,900011234\n"
                "9999999999,OTHER GROUP,7 PINE STREET,,LOS ANGELES,CA,90001\n")
    catalogue = {"distribution": [{"data": {"downloadURL": "https://cms/dac.csv"}}]}

    def fake_fetch(url, dest=None):
        if dest is None:
            return json.dumps(catalogue).encode()
        dest.write_text(national)
        return dest

    monkeypatch.setattr(address_linking, "fetch", fake_fetch)
    rows, sites = address_linking.cms_practice_sites({"1111111111"})
    assert [(r["address"], r["group"]) for r in rows["1111111111"]] == [
        ("1 MAIN ST STE 5, LOS ANGELES, CA 90001-1234", "MAIN STREET CLINIC INC")]
    assert sites == {"1 MAIN ST 90001", "7 PINE ST 90001"}  # every practice site, any clinician


def test_active_cms_site_makes_a_stale_nppes_address_current():
    providers = {"2222222222": {"npi_type": "Type 2 (organization)", "names": ["SOME CLINIC"],
                                "last_updated": "2015-01-01",
                                "locations": [loc("7 Pine Street", "NPPES primary practice location")]}}
    rows, _ = link({"22-2": {"Some Clinic"}}, {("22-2", "2222222222")}, providers, {}, {}, "CA",
                   cms_sites={"7 PINE ST 90001"})
    assert rows[0]["Confidence"] == "high"  # tied by name; current because clinicians practise there
    assert "active practice site" in rows[0]["Evidence"]


def test_irs_matches_tins_by_ein(monkeypatch):
    csv_text = ("EIN,NAME,STREET,CITY,STATE,ZIP\n"
                "680273974,SUTTER HEALTH,2200 RIVER PLAZA DR,SACRAMENTO,CA,95833-4134\n"
                "999999999,OTHER,1 X ST,LA,CA,90001\n")
    monkeypatch.setattr(address_linking, "fetch", lambda url: csv_text.encode())
    out = address_linking.irs_organizations({"68-0273974", "11-1111111"}, {"CA"}, [])
    assert list(out) == ["68-0273974"]
    assert out["68-0273974"]["location"]["address"] == "2200 RIVER PLAZA DR, SACRAMENTO, CA 95833-4134"


def test_solo_practice_and_one_to_one_ties_with_staleness():
    providers = {
        # solo practitioner: TIN named after the doctor, NPPES record recent
        "1111111111": {"npi_type": "Type 1 (individual)", "person": "DOUGLAS TAYLOR", "last_updated": RECENT,
                       "locations": [loc("1 A ST", "NPPES primary practice location")]},
        # organization billing one-to-one with its TIN, NPPES record stale
        "2222222222": {"npi_type": "Type 2 (organization)", "names": ["SOME CLINIC"], "last_updated": "2015-01-01",
                       "locations": [loc("2 B ST", "NPPES primary practice location")]},
    }
    rows, _ = link({"11-1": {"TAYLOR, D.P.M."}, "22-2": {"OTHER NAME"}},
                   {("11-1", "1111111111"), ("22-2", "2222222222")}, providers, {}, {}, "CA")
    by_npi = {r["NPI"]: r for r in rows}
    assert by_npi["1111111111"]["Confidence"] == "medium"  # tied by name; NPPES only but recent
    assert "own practice" in by_npi["1111111111"]["Evidence"]
    assert by_npi["2222222222"]["Confidence"] == "low"  # tied one-to-one, but only a stale NPPES record
    assert "likely stale" in by_npi["2222222222"]["Note"]
