from datetime import date

from rate_pipeline.file_quality import Grader, grade_index, npi_valid
from rate_pipeline.rate_file import collector

TODAY = date(2026, 10, 17)


def part(npi, code, rate, rate_type="negotiated", expires="9999-12-31", arrangement="ffs", cls="professional"):
    return {"provider": {"npi": npi, "tin.value": "11-1"},
            "service": {"billing_code_type": "CPT", "billing_code": code, "negotiation_arrangement": arrangement},
            "price": {"negotiated_rate": rate, "negotiated_type": rate_type, "expiration_date": expires,
                      "billing_class": cls, "service_code": "11"}}


def test_npi_check_digit():
    assert npi_valid("1770688129") and npi_valid("1003207002")
    assert not npi_valid("1770688128") and not npi_valid("0")


def metrics_by_name(metrics):
    return {m["metric"]: m for m in metrics}


def test_rate_file_metrics_count_each_problem():
    grader, groups = Grader(TODAY), {}
    collect = collector(groups, {}, TODAY)

    class Both:  # feed every row to the grader and the rate collector, as the pipeline does
        def add(self, p):
            grader.add(p)
            collect(p)

    g = Both()
    for i in range(8):
        g.add(part("1770688129", "99213", str(100 + i)))         # 8 normal rates
    g.add(part("1770688129", "99213", "100"))                    # exact duplicate of the first
    g.add(part("1770688129", "99213", "0"))                      # $0
    g.add(part("1770688129", "99213", "100000"))                 # outlier
    g.add(part("1770688129", "99213", "99", expires="2020-01-01"))  # expired
    g.add(part("1770688129", "36415", "9"))                      # a code with too few peers
    spec_proc = {"PCP": {"99213": 3.0, "99214": 1.0}}
    m = metrics_by_name(grader.finish({"last_updated_on": "2026-10-06"}, groups, {"1770688129": "PCP"},
                                 {"PCP": 1.0}, spec_proc, {"1770688129": {"npi_type": "Type 1 (individual)"}}))
    assert m["freshness"]["score"] == 100.0
    assert m["$0, negative or unreadable rates"]["value"] == "7.69%"   # 1 of 13
    assert m["duplicate prices"]["value"].startswith("7.69% repeat") and m["duplicate prices"]["value"].endswith(
        "7.69% are identical in every field")
    assert m["expired rates"]["value"] == "7.69%"
    # 100000 is over 10x the typical 99213 price (median 104); the lone 36415 rate has too few peers
    assert m["outlier rates"]["value"] == "10.00% of 10 CPT/HCPCS provider rates"
    assert m["conflicting rates"]["value"].startswith("50.00% of 2")  # 99213 has several rates
    assert m["code coverage (HiLabs weights)"]["score"] == 75.0  # 99213 priced (weight 3 of 4)
    assert m["provider validity"]["score"] == 100.0
    assert 0 < m["FILE SCORE"]["score"] < 100


def test_index_metrics():
    doc = {"last_updated_on": "2026-09-30", "reporting_structure": [{
        "in_network_files": [{"location": "u"}],
        "reporting_plans": [
            {"plan_name": "A", "plan_id": "952789166", "plan_id_type": "ein", "plan_market_type": "group",
             "issuer_name": "HN"},
            {"plan_name": "A", "plan_id": "952789166", "plan_id_type": "ein", "plan_market_type": "group",
             "issuer_name": "HN"},                                   # duplicate
            {"plan_name": "B", "plan_id": "67138CA073", "plan_id_type": "ein", "plan_market_type": "group",
             "issuer_name": "HN"},                                   # HIOS id labelled EIN
            {"plan_name": "C", "plan_id": "1", "plan_market_type": "group"},  # incomplete
        ]}]}
    m = metrics_by_name(grade_index(doc, TODAY))
    assert m["field completeness"]["value"].startswith("75.00%")
    assert m["duplicate plan entries"]["value"] == "25.00%"
    assert m["plan ID consistency"]["value"] == "50.00%"
    assert m["freshness"]["score"] == 100.0
