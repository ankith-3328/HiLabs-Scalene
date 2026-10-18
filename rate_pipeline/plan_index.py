"""Index files → plan rows, plus the California and network-type rules."""

import re
from urllib.parse import unquote, urljoin, urlsplit

# USPS codes for states, DC and territories: anything else (e.g. Fidelis "EP") is not a state.
US_STATES = frozenset("""
    AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV
    NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI
""".split())

# Network type from the plan name, checked in order (first match wins). PPO comes first:
# a name mentioning PPO counts as PPO even if it also mentions another type (team rule).
# Health Net codes (Health Net CA broker materials): EOA = Elect Open Access, an
# HMO-based plan; SAL = Salud con Health Net HMO line; SAL Mex = Salud Mexico (SIMNSA).
NETWORK_RULES = [
    ("PPO", r"\bPPO\b", "plan name says PPO"),
    ("PPO", r"PPO\b", "plan name says PPO (joined to another word, e.g. 'CUSPPO' = 'CUS PPO')"),
    ("EPO", r"\bEPO", "plan name says EPO"),
    ("POS", r"\bPOS\b", "plan name says POS"),
    ("HMO", r"\bHMO\b", "plan name says HMO"),
    ("HMO", r"\bEOA\b", "EOA = Health Net Elect Open Access, an HMO-based plan"),
    ("HMO", r"\bSAL\b.*\bMEX\b", "SAL Mex = Salud Mexico (SIMNSA), not a PPO"),
    ("HMO", r"\bSAL(UD)?\b", "SAL = Salud con Health Net, an HMO product line"),
]
OTHER_TYPES = r"\b(EPO|POS|HMO|EOA)"


def index_links(page_html, page_url):
    """Absolute URLs of every .json file linked from the page, in page order."""
    hrefs = re.findall(r'href="([^"]+\.json)"', page_html, flags=re.IGNORECASE)
    return list(dict.fromkeys(urljoin(page_url, h.strip()) for h in hrefs))


def _file_name(url):
    return unquote(urlsplit(url).path).rsplit("/", 1)[-1].lower()


def brand(index_url):
    """'.../2026-09-30_healthnet_index.json' -> 'healthnet' (first token after the date)."""
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}_|\.json$", "", _file_name(index_url))
    return stem.split("_", 1)[0]


def parse_index(doc, index_url):
    """One row per plan x in-network file. Missing values stay blank; nothing is dropped."""
    rows = []
    for structure in doc.get("reporting_structure") or [{}]:
        urls = [f.get("location", "") for f in structure.get("in_network_files") or []] or [""]
        for plan in structure.get("reporting_plans") or [{}]:
            for url in urls:
                rows.append({
                    "brand": brand(index_url),
                    "plan_name": str(plan.get("plan_name", "")).strip(),
                    "plan_id": str(plan.get("plan_id", "")).strip(),
                    "market_type": str(plan.get("plan_market_type", "")).strip(),
                    "rate_file_url": url,
                })
    return rows


def state(plan):
    """The plan's state, or '' when unknown or contradictory.

    Structured signals decide: the state inside a HIOS-format plan_id (67138CA073)
    and the state token in the rate file name (..._healthnet-ca_in-network.json).
    A leading state code in the plan name is used only when neither exists.
    """
    structured = set()
    m = re.fullmatch(r"\d{5}([A-Z]{2})\d{3}.*", plan["plan_id"])
    if m and m.group(1) in US_STATES:
        structured.add(m.group(1))
    m = re.search(r"-([a-z]{2})_in-network\.json$", _file_name(plan["rate_file_url"]))
    if m and m.group(1).upper() in US_STATES:
        structured.add(m.group(1).upper())
    if structured:
        return structured.pop() if len(structured) == 1 else ""
    m = re.match(r"([A-Z]{2})\s", plan["plan_name"])
    return m.group(1) if m and m.group(1) in US_STATES else ""


def network_type(plan_name):
    """(network type, reason) from the plan name; UNKNOWN when no rule matches."""
    name = plan_name.upper()
    for kind, pattern, reason in NETWORK_RULES:
        if re.search(pattern, name):
            other = re.search(OTHER_TYPES, name) if kind == "PPO" else None
            if other:  # e.g. "CUS PPO ... EPO$15"
                reason += f" (also mentions {other.group(1)}; PPO with any other type counts as PPO)"
            return kind, reason
    return "UNKNOWN", "plan name has no network type"
