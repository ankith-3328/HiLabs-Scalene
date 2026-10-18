"""NPI type, names, practice locations and specialty from the CMS NPI Registry (NPPES)."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from rate_pipeline.address_linking import location
from rate_pipeline.downloader import FetchError, fetch

API = "https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}"
WORKERS = 8  # parallel lookups, modest enough for a public API
TYPES = {"NPI-1": "Type 1 (individual)", "NPI-2": "Type 2 (organization)"}


def lookup_all(npis):
    """{npi: record} for every NPI; record['note'] explains anything missing."""
    with ThreadPoolExecutor(WORKERS) as pool:
        return dict(zip(sorted(set(npis)), pool.map(lookup, sorted(set(npis))), strict=True))


def lookup(npi):
    if not re.fullmatch(r"\d{10}", npi):
        return {"note": "invalid NPI in rate file (not 10 digits); not looked up"}
    try:
        return parse(json.loads(fetch(API.format(npi=npi))))
    except FetchError as err:
        return {"note": f"NPI Registry lookup failed: {err}", "failed": True}


def parse(doc):
    """Type, names, practice locations and primary taxonomy from an API response."""
    if doc.get("Errors"):
        return {"note": "NPI Registry rejected the NPI: " + "; ".join(e.get("description", "") for e in doc["Errors"])}
    if not doc.get("results"):
        return {"note": "not found in NPI Registry (invalid or deactivated NPI)"}
    r = doc["results"][0]
    notes = []
    basic = r.get("basic", {})
    name = basic.get("organization_name") or " ".join(
        p for p in (basic.get("first_name"), basic.get("middle_name"), basic.get("last_name"), basic.get("credential"))
        if p)

    primary_loc = [a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"]
    locations = [location(a.get("address_1"), a.get("address_2"), a.get("city"), a.get("state"),
                          a.get("postal_code"), f"NPPES {kind} practice location")
                 for kind, found in (("primary", primary_loc), ("secondary", r.get("practiceLocations") or []))
                 for a in found]
    if not locations:
        notes.append("no practice location address in NPI Registry")

    taxonomies = r.get("taxonomies", [])
    primary = [t for t in taxonomies if t.get("primary")]
    chosen = (primary or taxonomies or [None])[0]
    specialty = ""
    if chosen is None:
        notes.append("no taxonomy in NPI Registry")
    else:
        if not primary:
            notes.append("no primary taxonomy flagged; first listed used")
        specialty = chosen.get("desc") or ""
        if not specialty:  # the registry sometimes returns the code with a null description
            specialty = f"taxonomy {chosen.get('code', '')}"
            notes.append("NPI Registry gave the taxonomy code without a description")

    return {
        "npi_type": TYPES.get(r.get("enumeration_type"), r.get("enumeration_type", "")),
        "name": name,
        "names": [n for n in [basic.get("organization_name"), *(o.get("organization_name") for o in
                                                                r.get("other_names", []))] if n],
        "locations": locations,
        "taxonomy": (chosen or {}).get("code", ""),
        "specialty": specialty,
        "last_updated": basic.get("last_updated", ""),
        # people whose name a TIN may carry: the individual, or an organization's authorized official
        "person": " ".join(p for p in (basic.get("first_name"), basic.get("last_name")) if p),
        "official": " ".join(p for p in (basic.get("authorized_official_first_name"),
                                          basic.get("authorized_official_last_name")) if p),
        "note": "; ".join(notes),
    }
