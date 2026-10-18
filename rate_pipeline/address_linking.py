"""TIN -> NPI -> address linking from several public sources (see C5 in deliverables/2_logic.md).

A TIN can have many addresses and a provider can practise at many addresses, for
different TINs. Every source adds candidate (TIN, NPI, address) links with evidence;
a link is kept for a TIN only when some evidence ties the address to that TIN.

Sources:
  NPPES                          NPI -> primary and secondary practice locations, organization names
  CMS Doctors & Clinicians       NPI -> each (group practice, address) a clinician works at, and
                                 every address where Medicare clinicians actively practise
  IRS Exempt Organizations BMF   TIN (EIN) -> legal name and registered address (non-profits)
"""

import csv
import io
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from rate_pipeline.downloader import FetchError, fetch

DAC_DATASET = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/mj5m-pzi6"
IRS_EO = "https://www.irs.gov/pub/irs-soi/eo_{state}.csv"
WORKERS = 8
STALE_DAYS = 730  # an NPPES record not updated for two years is treated as likely stale

# Evidence that ties an address to a specific TIN (not just to the NPI) ...
TIN_EVIDENCE = {"dac_group", "org_name", "person_name", "official_name", "irs_name", "sole_npi", "colocated", "irs"}
# ... and evidence, independent of NPPES, that the address is current.
CURRENT_EVIDENCE = {"dac", "cms_site", "irs", "colocated"}
EVIDENCE_TEXT = {
    "org_name": "the NPI's organization name matches the TIN's business name",
    "person_name": "the provider's name matches the TIN's business name (own practice)",
    "official_name": "the TIN's business name is the organization's authorized official",
    "irs_name": "the IRS legal name of the TIN matches the NPI's organization name",
    "sole_npi": "the TIN bills only with this NPI, and the NPI only under this TIN",
}

_ABBREV = {"STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "DRIVE": "DR", "ROAD": "RD", "LANE": "LN",
           "COURT": "CT", "PLAZA": "PLZ", "PARKWAY": "PKWY", "HIGHWAY": "HWY", "PLACE": "PL", "CIRCLE": "CIR",
           "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "SUITE": "STE"}
_UNIT = {"STE", "UNIT", "APT", "FL", "FLOOR", "RM", "ROOM", "BLDG", "BUILDING"}
_NAME_STOP = {"INC", "LLC", "LLP", "LP", "PC", "PA", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "THE", "OF",
              "AND", "A", "AN", "MD", "DO", "PROFESSIONAL", "MEDICAL", "DBA"}


def location(line1, line2, city, state, zip_code, source, group=""):
    """A practice location: display text plus a match key (street without unit + ZIP5)."""
    zip_code = (zip_code or "").replace("-", "")
    shown_zip = f"{zip_code[:5]}-{zip_code[5:]}" if len(zip_code) == 9 else zip_code
    street = " ".join(p for p in (line1, line2) if p)
    tokens = [_ABBREV.get(t, t) for t in re.sub(r"[^A-Z0-9 ]", " ", (line1 or "").upper().replace("#", " STE ")).split()]
    if any(t in _UNIT for t in tokens):
        tokens = tokens[:next(i for i, t in enumerate(tokens) if t in _UNIT)]
    return {"address": f"{street}, {city or ''}, {state or ''} {shown_zip}".strip(),
            "key": f"{' '.join(tokens)} {zip_code[:5]}", "state": state or "", "source": source, "group": group}


def _words(name, stop=_NAME_STOP):
    return {w for w in re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()).split() if w not in stop}


def _word_in(word, words):
    """Equal, or one is a cut-off form of the other (rate files truncate names: AMER = AMERICA)."""
    return any(w == word or (min(len(w), len(word)) >= 3 and (w.startswith(word) or word.startswith(w)))
               for w in words)


def same_org(a, b):
    """True when two organization names share most of their significant words."""
    ta, tb = _words(a), _words(b)
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return bool(small) and sum(_word_in(w, large) for w in small) / len(small) >= 0.6


def person_in(person, business_name):
    """True when a person's surname (all its parts) appears in a business name, e.g. 'TAYLOR, D.P.M.'."""
    parts = re.sub(r"[^A-Z ]", " ", (person or "").upper()).split()
    business = _words(business_name, set())
    # a person-style business name is short once credentials are removed
    short = len(business - _NAME_STOP - {"DR", "DPM", "MFT", "LCSW", "PHD", "PSYD", "DDS", "OD", "DC", "NP"}) <= 4
    return bool(parts) and len(parts[-1]) >= 2 and short and _word_in(parts[-1], business)


def cms_practice_sites(npis):
    """Stream the CMS Doctors & Clinicians national file (~900 MB, refreshed monthly from Medicare
    enrollment) once, without loading it.

    Returns ({npi: [location]} for the given NPIs, {address key} of every active practice site).
    The file's download URL changes each month, so it is read from CMS's dataset catalogue.
    """
    meta = json.loads(fetch(DAC_DATASET))
    url = next(d.get("data", d).get("downloadURL") for d in meta["distribution"])
    rows, sites = {}, set()
    with tempfile.TemporaryDirectory() as tmp:
        path = fetch(url, Path(tmp) / "dac.csv")
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for r in csv.DictReader(f):
                loc = location(r["adr_ln_1"], r["adr_ln_2"], r["City/Town"], r["State"], r["ZIP Code"],
                               "CMS Doctors & Clinicians", group=r["Facility Name"])
                sites.add(loc["key"])
                if r["NPI"] in npis:
                    rows.setdefault(r["NPI"], []).append(loc)
    return rows, sites


def irs_organizations(tins, states, errors):
    """{tin: {"name", "location"}} for TINs in the IRS Exempt Organizations files of the given states."""
    by_digits = {t.replace("-", ""): t for t in tins}

    def one(state):
        try:
            text = fetch(IRS_EO.format(state=state.lower())).decode("latin-1")
        except FetchError as err:
            errors.append(f"IRS exempt organizations file for {state}: {err}")
            return []
        return [r for r in csv.DictReader(io.StringIO(text)) if r["EIN"] in by_digits]

    out = {}
    with ThreadPoolExecutor(WORKERS) as pool:
        for rows in pool.map(one, sorted(states)):
            for r in rows:
                out[by_digits[r["EIN"]]] = {"name": r["NAME"], "location": location(
                    r["STREET"], "", r["CITY"], r["STATE"], r["ZIP"], "IRS registered address")}
    return out


def _age_days(iso_date):
    """Days since an ISO date; unknown dates count as very old."""
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except (TypeError, ValueError):
        return 10**6


LINK_COLUMNS = ["TIN", "TIN Name", "NPI", "NPI Type", "Provider Name", "Address", "Confidence", "Evidence", "Note"]


def link(tin_names, pairs, providers, dac, irs, home_state, cms_sites=frozenset()):
    """Return (pair_rows, tin_rows): one row per linked (TIN, NPI, address), and one row
    per TIN with an IRS registered address.

    tin_names: {tin: set of business names in the rate file}; pairs: {(tin, npi)}.
    """
    npis_of, tins_of = {}, {}
    for tin, npi in pairs:
        npis_of.setdefault(tin, set()).add(npi)
        tins_of.setdefault(npi, set()).add(tin)
    cands = {}  # (tin, npi) -> {address key: {"loc", "evidence": {code: text}}}

    def add(tin, npi, loc, codes):
        c = cands.setdefault((tin, npi), {}).setdefault(loc["key"], {"loc": loc, "evidence": {}})
        for code, text in codes.items():
            c["evidence"].setdefault(code, text)

    for tin, npi in pairs:
        p, names = providers.get(npi, {}), tin_names.get(tin, set())
        ties = {}
        if p.get("npi_type", "").startswith("Type 2") and any(same_org(a, b) for a in p.get("names", []) for b in names):
            ties["org_name"] = EVIDENCE_TEXT["org_name"]
        if p.get("npi_type", "").startswith("Type 1") and any(person_in(p.get("person"), b) for b in names):
            ties["person_name"] = EVIDENCE_TEXT["person_name"]
        if p.get("npi_type", "").startswith("Type 2") and any(person_in(p.get("official"), b) for b in names):
            ties["official_name"] = EVIDENCE_TEXT["official_name"]
        if tin in irs and any(same_org(irs[tin]["name"], a) for a in p.get("names", [])):
            ties["irs_name"] = EVIDENCE_TEXT["irs_name"]
        if npi and npis_of[tin] == {npi} and tins_of[npi] == {tin}:  # one-to-one in the rate file
            ties["sole_npi"] = EVIDENCE_TEXT["sole_npi"]

        updated = f" (record updated {p['last_updated']})" if p.get("last_updated") else ""
        for loc in p.get("locations", []):
            code = "nppes_primary" if "primary" in loc["source"] else "nppes_secondary"
            add(tin, npi, loc, {code: loc["source"] + updated, **ties})
        for loc in dac.get(npi, []):
            codes = {"dac": f"CMS Doctors & Clinicians: practises here ({loc['group'] or 'own practice'})"}
            if loc["group"] and any(same_org(loc["group"], b) for b in names):
                codes["dac_group"] = f"CMS Doctors & Clinicians group '{loc['group']}' matches the TIN"
            elif not loc["group"] and ties.keys() & {"person_name", "sole_npi"}:
                codes.update(ties)  # an individual's own practice, and the TIN is theirs
            add(tin, npi, loc, codes)

    npis_at = {}
    for (tin, npi), by_key in cands.items():
        for key in by_key:
            npis_at.setdefault((tin, key), set()).add(npi)
    for (tin, _), by_key in cands.items():
        for key, c in by_key.items():
            if len(npis_at[(tin, key)]) > 1:
                c["evidence"]["colocated"] = f"address shared by {len(npis_at[(tin, key)])} NPIs billing under this TIN"
            if tin in irs and irs[tin]["location"]["key"] == key:
                c["evidence"]["irs"] = "IRS: the TIN's registered address"
            if key in cms_sites and "dac" not in c["evidence"]:
                c["evidence"]["cms_site"] = "CMS Doctors & Clinicians: an active practice site (clinicians practise here)"

    rows = []
    for tin, npi in sorted(pairs):
        p, by_key = providers.get(npi, {}), cands.get((tin, npi), {})
        kept = [c for c in by_key.values() if c["evidence"].keys() & TIN_EVIDENCE] or \
               [c for c in by_key.values() if "nppes_primary" in c["evidence"]] or list(by_key.values())
        base = {"TIN": tin, "TIN Name": " | ".join(sorted(tin_names.get(tin, set()))), "NPI": npi,
                "NPI Type": p.get("npi_type", ""), "Provider Name": p.get("name", "")}
        if not kept:
            rows.append({**base, "Address": "", "Confidence": "none", "Evidence": "",
                         "Note": p.get("note") or "no address in any source"})
        for c in sorted(kept, key=lambda c: c["loc"]["key"]):
            codes = c["evidence"].keys()
            tied, current = codes & TIN_EVIDENCE, codes & CURRENT_EVIDENCE
            fresh = _age_days(p.get("last_updated")) <= STALE_DAYS
            confidence = "high" if tied and current else "medium" if tied and fresh else "low"
            notes = [p["note"]] if p.get("note") else []
            if not tied:
                notes.append("address not tied to this TIN by any source")
            elif not current and not fresh:
                notes.append(f"only source is an NPPES record last updated {p.get('last_updated')}: likely stale")
            if c["loc"]["state"] and c["loc"]["state"] != home_state:
                notes.append(f"outside {home_state} ({c['loc']['state']})")
            rows.append({**base, "Address": c["loc"]["address"], "Confidence": confidence,
                         "Evidence": "; ".join(c["evidence"].values()), "Note": "; ".join(notes)})
    tin_rows = []
    for tin, org in sorted(irs.items()):
        tin_rows.append({"TIN": tin, "TIN Name": org["name"], "NPI": "", "NPI Type": "", "Provider Name": "",
                         "Address": org["location"]["address"], "Confidence": "high",
                         "Evidence": "IRS Exempt Organizations Business Master File (registered address)",
                         "Note": ""})
    return rows, tin_rows
