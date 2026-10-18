import csv
import io
import json
import ssl
import tempfile
import urllib.error
from datetime import date, timedelta

import openpyxl
import pytest

import main
from rate_pipeline import downloader, npi_registry
from rate_pipeline.downloader import FetchError, fetch

BASE = "https://www.centene.com/dam/"
NPPES = "https://npiregistry.cms.hhs.gov/api/?version=2.1&number="


def _index(plans):
    return json.dumps({"reporting_structure": [
        {"reporting_plans": [{"plan_name": name, "plan_id": pid, "plan_market_type": market}],
         "in_network_files": [{"location": url}]} for name, pid, market, url in plans]}).encode()


SITE = {
    main.PAGE_URL: b'<a href="/dam/2026-09-28_brand_index.json">x</a> <a href="/dam/2026-09-28_gone_index.json">y</a>',
    BASE + "2026-09-28_brand_index.json": _index([
        ("CA L PPO 30", "952789166", "group", BASE + "x_brand-ca_in-network.json"),
        ("CA L HMO 20", "611670591", "group", BASE + "x_brand-ca_in-network.json"),
        ("Marketplace Gold", "62505CA013", "individual", BASE + "x_market-ca_in-network.json"),
        ("OR plan", "10940OR029", "group", BASE + "x_brand-or_in-network.json"),
    ]),
    BASE + "x_brand-ca_in-network.json": json.dumps({
        "version": "2.0.0",
        "provider_references": [{"provider_group_id": 1, "provider_groups": [
            {"npi": [1770688129], "tin": {"type": "ein", "value": "68-0273974", "business_name": "Sutter"}}]},
            # Centene's placeholder NPI 0
            {"provider_group_id": 2, "provider_groups": [
                {"npi": [0], "tin": {"type": "ein", "value": "57-3865868", "business_name": "PPO: Los Angeles County"}}]}],
        "in_network": [{"billing_code_type": "CPT", "billing_code": "10040", "description": "ACNE SURGERY",
                        "negotiated_rates": [{"provider_references": [1, 2],
                                              "negotiated_prices": [{"negotiated_rate": 150.61}]}]}],
    }).encode(),
    "https://www.irs.gov/pub/irs-soi/eo_ca.csv":
        b"EIN,NAME,STREET,CITY,STATE,ZIP\n680273974,SUTTER HEALTH,2200 RIVER PLAZA DR,SACRAMENTO,CA,95833\n",
    "https://www.nucc.org/images/stories/CSV/nucc_taxonomy_261.csv":
        b"Code,Classification,Specialization,Display Name\n291U00000X,Clinical Medical Laboratory,,Clinical Medical Laboratory\n",
    NPPES + "1770688129": json.dumps({"results": [{
        "enumeration_type": "NPI-2",
        "basic": {"organization_name": "SUTTER", "last_updated": (date.today() - timedelta(days=30)).isoformat()},
        "addresses": [{"address_purpose": "LOCATION", "address_1": "1 MAIN ST", "city": "SACRAMENTO",
                       "state": "CA", "postal_code": "95816"}],
        "taxonomies": [{"code": "291U00000X", "desc": "Clinical Medical Laboratory", "primary": True}]}]}).encode(),
}


class FakeResp(io.BytesIO):
    status = 200

    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _http(code):
    return urllib.error.HTTPError("u", code, "err", None, None)


def _serve(monkeypatch, responses):
    """Patch urlopen: responses is a dict (url -> body) or a list of outcomes in call order."""
    calls = []

    def urlopen(req, timeout, context=None):
        calls.append(req.full_url)
        if isinstance(responses, dict):
            if req.full_url not in responses:
                raise _http(404)
            return FakeResp(responses[req.full_url])
        out = responses.pop(0)
        if isinstance(out, Exception):
            raise out
        return out if isinstance(out, FakeResp) else FakeResp(out)

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(downloader.time, "sleep", lambda s: None)
    return calls


def test_fetch_retries_transient_failures(tmp_path, monkeypatch):
    calls = _serve(monkeypatch, [urllib.error.URLError("reset"), _http(503), b"{}"])
    assert fetch("https://h/x.json") == b"{}" and len(calls) == 3


def test_fetch_retries_incomplete_download(tmp_path, monkeypatch):
    calls = _serve(monkeypatch, [FakeResp(b"{", length=9), b"{}"])
    assert fetch("https://h/x.json", tmp_path / "x.json").read_bytes() == b"{}" and len(calls) == 2


def test_fetch_fails_fast_on_404_and_gives_up_after_retries(monkeypatch):
    calls = _serve(monkeypatch, [_http(404)])
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch("https://h/x.json")
    assert len(calls) == 1
    _serve(monkeypatch, [_http(500), _http(429), TimeoutError()])
    with pytest.raises(FetchError, match="failed after 3 attempts"):
        fetch("https://h/x.json")


def _csv(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def _xlsx(path):
    header, *rows = openpyxl.load_workbook(path, read_only=True).worksheets[0].iter_rows(values_only=True)
    return [dict(zip(header, r, strict=True)) for r in rows]


def _printed(capsys):
    return dict(line.split(": ", 1) for line in capsys.readouterr().out.splitlines() if ": " in line)


def test_end_to_end_writes_the_outputs_and_reruns_identically(tmp_path, monkeypatch, capsys):
    calls = _serve(monkeypatch, SITE)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    out = tmp_path / "out"
    run = ["--out", str(out)]
    assert main.main(run) == 1  # the "gone" index 404s: reported, not hidden

    assert sorted(p.name for p in out.iterdir()) == [
        "1_ppo_rate_file.xlsx",
        "c1_index_plans.csv", "c2_ppo_plans.csv", "c3_file_quality.md", "c4_ppo_rates.csv",
        "c5_ppo_rates_by_address.csv"]
    assert list((tmp_path / "tmp").iterdir()) == []  # the downloaded rate file was removed

    # C1 and C2
    assert len(_csv(out / "c1_index_plans.csv")) == 4
    assert [(r["plan_name"], r["network_type"]) for r in _csv(out / "c2_ppo_plans.csv")] == [("CA L PPO 30", "PPO")]
    # only the PPO network's rate file is downloaded
    assert BASE + "x_market-ca_in-network.json" not in calls and BASE + "x_brand-or_in-network.json" not in calls

    # C4-C6: the placeholder NPI 0 is left out of all three
    assert _csv(out / "c4_ppo_rates.csv") == [
        {"NPI": "1770688129", "TIN": "68-0273974", "Billing Code": "10040", "Rate": "150.61",
         "Min Rate": "150.61", "Max Rate": "150.61"}]
    row = {"NPI": "1770688129", "TIN": "68-0273974", "Billing Code": "10040",
           "Address": "1 MAIN ST, SACRAMENTO, CA 95816", "Specialty": "Clinical Medical Laboratory", "Rate": "150.61"}
    assert _csv(out / "c5_ppo_rates_by_address.csv") == [{**row, "Address Confidence": "medium"}]
    assert _xlsx(out / "1_ppo_rate_file.xlsx") == [{**row, "Rate": 150.61}]  # Rate is a number in Excel

    # C3 report and the metrics printed to the console
    report = (out / "c3_file_quality.md").read_text(encoding="utf-8")
    assert "| x_brand-ca_in-network.json | in-network rate file |" in report and "| FILE SCORE |" in report
    summary = _printed(capsys)
    assert summary["PPO plans / rate files"].startswith("1 / 1")
    assert summary["NPIs linked to a verified address (high or medium)"] == "1 of 1 (100.0%)"
    assert summary["rate file rows removed as invalid"] == "{'invalid NPI': 1}"
    # tied to the TIN by name and one-to-one, address from a recent NPPES record only
    assert summary["C6 address confidence"] == "{'medium': 1}"

    # re-running gives byte-identical outputs
    # (the workbook stores its save time, so it is compared by content)
    def snapshot():
        return {p.name: _xlsx(p) if p.suffix == ".xlsx" else p.read_bytes()
                for p in out.iterdir()}

    first = snapshot()
    assert main.main(run) == 1
    assert snapshot() == first


def test_unique_keeps_the_first_row_per_key():
    rows = [{"k": 1, "a": "x"}, {"k": 2, "a": "y"}, {"k": 1, "a": "z"}]
    assert main.unique(rows, ["k"]) == [rows[0], rows[1]]
    assert main.unique(rows, ["k", "a"]) == rows


def test_index_only_and_other_scope(tmp_path, monkeypatch):
    _serve(monkeypatch, SITE)
    out = tmp_path / "out"
    main.main(["--out", str(out), "--index-only", "--state", "OR", "--network", "HMO"])
    assert sorted(p.name for p in out.iterdir()) == [
        "c1_index_plans.csv", "c2_hmo_plans.csv", "c3_file_quality.md"]
    assert _csv(out / "c2_hmo_plans.csv") == []  # the OR plan's name gives no network type


def test_several_rate_files_are_processed_in_parallel_processes(tmp_path, monkeypatch):
    import functools
    import http.server
    import threading

    site = tmp_path / "site"
    site.mkdir()
    for name in ("x_a-ca_in-network.json", "x_b-ca_in-network.json"):
        (site / name).write_bytes(SITE[BASE + "x_brand-ca_in-network.json"])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{server.server_port}/"
    (site / "2026-01-01_brand_index.json").write_bytes(_index([
        ("CA L PPO A", "1", "group", root + "x_a-ca_in-network.json"),
        ("CA L PPO B", "2", "group", root + "x_b-ca_in-network.json")]))
    (site / "page.html").write_text('<a href="2026-01-01_brand_index.json">i</a>')
    monkeypatch.setattr(main, "PAGE_URL", root + "page.html")
    record = npi_registry.parse(json.loads(SITE[NPPES + "1770688129"]))
    monkeypatch.setattr(main, "lookup_all", lambda npis: {"1770688129": record})  # keep the test offline
    monkeypatch.setattr(main, "irs_organizations", lambda tins, states, errors: {})
    monkeypatch.setattr(main, "load_nucc", lambda: {})
    monkeypatch.setattr(main, "cms_practice_sites", lambda npis: ({}, set()))
    try:
        assert main.main(["--out", str(tmp_path / "out")]) == 0
    finally:
        server.shutdown()
    assert "| x_a-ca_in-network.json |" in (tmp_path / "out" / "c3_file_quality.md").read_text(encoding="utf-8")
    assert "| x_b-ca_in-network.json |" in (tmp_path / "out" / "c3_file_quality.md").read_text(encoding="utf-8")
    # same NPI/TIN/code in both files: merged into one row of the rate file
    [row] = _xlsx(tmp_path / "out" / "1_ppo_rate_file.xlsx")
    assert (row["NPI"], row["Rate"]) == ("1770688129", 150.61)


def test_fetch_falls_back_to_certifi_on_certificate_errors(monkeypatch):
    used = []

    def urlopen(req, timeout, context=None):
        used.append(context)
        if context is downloader.SSL_CONTEXTS[0]:
            raise urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate in certificate chain"))
        return FakeResp(b"ok")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", urlopen)
    assert downloader.fetch("https://x") == b"ok"
    assert used == downloader.SSL_CONTEXTS

