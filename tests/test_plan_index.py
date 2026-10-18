import pytest

from network_mapping.plan_index import (
    brand,
    index_links,
    network_type,
    parse_index,
    state,
)

PAGE = "https://www.centene.com/price-transparency-files.html"


def test_index_links_resolve_relative_json_links_once():
    html = """<a href="/dam/2026-09-28_ambetter_index.json">Ambetter</a>
              <a href="https://www.cigna.com/mrf">CIGNA</a>
              <a href="/dam/2026-09-28_ambetter_index.json">again</a>"""
    assert index_links(html, PAGE) == ["https://www.centene.com/dam/2026-09-28_ambetter_index.json"]


def test_brand_from_index_file_name():
    assert brand("https://h/Centene%20Corporate/2026-09-28_qualchoice_alf_plan_index.json") == "qualchoice"
    assert brand("https://h/2026-09-30_healthnet_index.json") == "healthnet"


def test_parse_index_one_row_per_plan_and_file_with_blanks_kept():
    doc = {"reporting_structure": [
        {"reporting_plans": [{"plan_name": "CA L PPO 30", "plan_id": 952789166, "plan_market_type": "group"},
                             {"plan_name": "No id"}],
         "in_network_files": [{"location": "u1"}, {"location": "u2"}]},
        {"reporting_plans": [{"plan_name": "No files", "plan_id": "1"}]},
    ]}
    rows = parse_index(doc, "https://h/2026-09-30_healthnet_index.json")
    assert [(r["plan_name"], r["plan_id"], r["rate_file_url"]) for r in rows] == [
        ("CA L PPO 30", "952789166", "u1"), ("CA L PPO 30", "952789166", "u2"),
        ("No id", "", "u1"), ("No id", "", "u2"), ("No files", "1", "")]
    assert rows[0]["brand"] == "healthnet" and rows[0]["market_type"] == "group"


def _plan(plan_id="", url="", name=""):
    return {"plan_id": plan_id, "rate_file_url": url, "plan_name": name}


@pytest.mark.parametrize("plan, expected", [
    (_plan("67138CA073", "https://h/x_healthnet-ca_in-network.json"), "CA"),   # HIOS + file agree
    (_plan("952789166", "https://h/x_healthnet-ca_in-network.json", "CA L PPO"), "CA"),
    (_plan("62505OK013", "https://h/x_ambetter-ca_in-network.json"), ""),      # conflict: not guessed
    (_plan("1", "https://h/qualchoice_in-network.json", "CA L HMO"), "CA"),    # name only as fallback
    (_plan("1", "https://h/x_healthnet-or_in-network.json", "CA L HMO"), "OR"),  # structured wins
    (_plan("25303EP005", "https://h/x_fidelis-ex_in-network.json", "ER plan"), ""),  # not states
])
def test_state(plan, expected):
    assert state(plan) == expected


@pytest.mark.parametrize("name, expected", [
    ("CA L PPO 30/500/4000/30%", "PPO"),
    ("Gold PPO 500/20 INF", "PPO"),
    ("CA L CUSPPO 15/250/20% RAP", "PPO"),
    ("CA L SC HMO 50/1500d/7500", "HMO"),
    ("CA L EC EOA 30/30%/3500", "HMO"),
    ("CA L SAL Mex 5/0/1500", "HMO"),
    ("CA L&T CUS SAL CEMENT MASONS CA H&W GF+", "HMO"),
    ("CA L CUS POS COUNTY OF SANTA CLARA", "POS"),
    ("CA L CUS PPO CA IRONWORKERS FWP EPO$15", "PPO"),  # PPO + another type counts as PPO
    ("CA L CUS PPO SACRAMENTO CITY USD- EPO", "PPO"),
    ("CA L EPO 20/500", "EPO"),
    ("HMO PPO HYBRID", "PPO"),
    ("SALINAS VALLEY PLAN", "UNKNOWN"),
])
def test_network_type(name, expected):
    kind, reason = network_type(name)
    assert kind == expected and reason


def test_ppo_with_another_type_says_why():
    kind, reason = network_type("CA L CUS PPO CA IRONWORKERS FWP-EPO$10")
    assert kind == "PPO" and "also mentions EPO" in reason
    assert network_type("CA L PPO 30/500")[1] == "plan name says PPO"
