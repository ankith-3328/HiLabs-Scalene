"""C3: grade the quality and completeness of price transparency files (see deliverables/3_file_quality_metrics.md).

Every metric is a share of good (or bad) records turned into a 0-100 score that
falls linearly to 0 at a stated tolerance, then weighted into one file score.
Rate files are graded while they stream (Grader.add), so they are never held in memory.
"""

import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

from rate_pipeline.rate_file import is_outlier, service_medians

QUALITY_COLUMNS = ["file", "file_type", "metric", "value", "score", "weight", "how it is calculated"]
NO_EXPIRY = "9999-12-31"
CODE = re.compile(r"^(\d{5}|[A-Z]\d{4}|\d{4}[A-Z])$")  # CPT / HCPCS / CPT category II-III


def npi_valid(npi):
    """NPI check digit (Luhn over the '80840' prefix + 9 digits), as defined by CMS."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = sum(d if i % 2 else (d * 2 if d * 2 < 10 else d * 2 - 9) for i, d in enumerate(reversed(digits)))
    return (10 - total % 10) % 10 == int(npi[9])


def age_days(iso_date, today=None):
    try:
        return ((today or date.today()) - date.fromisoformat(iso_date)).days
    except (TypeError, ValueError):
        return None


def _score(bad_share, tolerance):
    return round(100 * max(0.0, 1 - bad_share / tolerance), 1)


def _freshness(days):
    """100 when published within 35 days (monthly cycle), falling to 0 at one year."""
    return 0.0 if days is None else round(100 * min(1, max(0, (365 - days) / (365 - 35))), 1)


def _metric(name, value, score, weight, how):
    return {"metric": name, "value": value, "score": score, "weight": weight, "how it is calculated": how}


def _total(metrics):
    return round(sum(m["score"] * m["weight"] for m in metrics) / sum(m["weight"] for m in metrics), 1)


def _pct(n, d):
    return f"{n / d:.2%}" if d else "n/a"


class Grader:
    """Accumulates rate-file quality counters from flatten()'s on_row parts."""

    def __init__(self, today=None):
        self.today = (today or date.today()).isoformat()
        self.rows = self.invalid = self.expired = self.duplicates = self.exact = 0
        self.seen, self.seen_exact = set(), set()
        self.codes = set()
        self.npi_codes = {}

    def add(self, parts):
        p, s, price = parts["provider"], parts["service"], parts["price"]
        self.rows += 1
        npi, tin = p.get("npi", ""), p.get("tin.value", "")
        code_type, code = s.get("billing_code_type", ""), s.get("billing_code", "")
        try:
            rate = Decimal(price.get("negotiated_rate", ""))
        except InvalidOperation:
            rate = None
        if rate is None or rate <= 0:
            self.invalid += 1
        expires = price.get("expiration_date", "")
        if expires and expires != NO_EXPIRY and expires < self.today:
            self.expired += 1
        key = hash((npi, tin, code_type, code, price.get("billing_class"), price.get("setting"),
                    price.get("billing_code_modifier"), price.get("service_code"),
                    price.get("additional_information"), price.get("negotiated_rate")))
        if key in self.seen:
            self.duplicates += 1
            exact = hash((key, s.get("description"), s.get("name")))
            if exact in self.seen_exact:
                self.exact += 1
            self.seen_exact.add(exact)
        else:
            self.seen.add(key)
            self.seen_exact.add(hash((key, s.get("description"), s.get("name"))))
        found = {c for c in (code, price.get("additional_information", "")) if c and CODE.match(c)}
        self.codes |= found
        self.npi_codes.setdefault(npi, set()).update(found)

    def finish(self, header, groups, provider_spec, spec_weights, spec_proc, providers):
        """Metrics for the file. provider_spec: {npi: HiLabs spec code} for NPIs with one."""
        n = self.rows or 1
        days = age_days(header.get("last_updated_on"), date.fromisoformat(self.today))
        conflicts = sum(1 for g in groups.values() if len({rate for rate, _ in g["rates"]}) > 1)
        medians = service_medians(groups)
        provider_rates = [(rate, svc) for g in groups.values() for rate, svc in g["rates"] if svc is not None]
        outliers = sum(1 for rate, svc in provider_rates if is_outlier(rate, medians.get(svc)))
        npis = [x for x in self.npi_codes if x]
        valid = [x for x in npis if npi_valid(x) and providers.get(x, {}).get("npi_type")]

        # Specialty-weighted code coverage: for each HiLabs specialty with providers in the file,
        # the weighted share of its key procedures priced for those providers.
        cover, weight = 0.0, 0.0
        for spec, procs in spec_proc.items():
            billed = set().union(*(self.npi_codes.get(x, set()) for x, sp in provider_spec.items() if sp == spec))
            if not billed or not spec_weights.get(spec):
                continue
            share = sum(w for c, w in procs.items() if c in billed) / (sum(procs.values()) or 1)
            cover += spec_weights[spec] * share
            weight += spec_weights[spec]
        coverage = cover / weight if weight else 0.0
        anywhere = sum(spec_weights.get(s, 0) * sum(w for c, w in procs.items() if c in self.codes) /
                       (sum(procs.values()) or 1) for s, procs in spec_proc.items()) / (sum(spec_weights.values()) or 1)

        metrics = [
            _metric("freshness", f"{days} days since last_updated_on {header.get('last_updated_on')}",
                    _freshness(days), 10, "100 if published within 35 days, linear to 0 at 365 days"),
            _metric("expired rates", _pct(self.expired, n), _score(self.expired / n, 0.10), 5,
                    "share of rate rows whose expiration_date is before the run date; 0 at 10%"),
            _metric("$0, negative or unreadable rates", _pct(self.invalid, n), _score(self.invalid / n, 0.05), 20,
                    "share of rate rows with negotiated_rate <= 0 or not a number; 0 at 5%"),
            _metric("outlier rates", f"{_pct(outliers, len(provider_rates))} of {len(provider_rates):,} "
                    "CPT/HCPCS provider rates",
                    _score(outliers / (len(provider_rates) or 1), 0.10), 15,
                    "share of providers' distinct CPT/HCPCS rates over 10x, or under 1/10, of the median rate for "
                    "the same code, billing class and modifier (>= 3 rates); revenue codes not judged; 0 at 10%"),
            _metric("duplicate prices", f"{_pct(self.duplicates, n)} repeat a price already listed for the same "
                    f"provider and service; {_pct(self.exact, n)} are identical in every field",
                    _score(self.duplicates / n, 0.10), 5,
                    "share of rows repeating NPI, TIN, code, class, setting, modifier, place of service, extra "
                    "code and rate (description text ignored); 0 at 10%"),
            _metric("conflicting rates", f"{_pct(conflicts, len(groups))} of {len(groups):,} NPI+TIN+code keys",
                    _score(conflicts / (len(groups) or 1), 0.25), 15,
                    "share of NPI+TIN+billing code keys with more than one distinct rate; 0 at 25%"),
            _metric("code coverage (HiLabs weights)",
                    f"{coverage:.1%} specialty-matched; {anywhere:.1%} of all specialties' key codes priced anywhere",
                    round(100 * coverage, 1), 20,
                    "for each HiLabs specialty with providers in the file, the Spec-Proc-weighted share of its key "
                    "procedures priced for those providers, averaged with Specialty Weights"),
            _metric("provider validity", f"{_pct(len(valid), len(npis))} of {len(npis):,} NPIs",
                    round(100 * len(valid) / (len(npis) or 1), 1), 10,
                    "share of NPIs with a valid check digit that are found in the NPI Registry"),
        ]
        return metrics + [_metric("FILE SCORE", "", _total(metrics), "", "weighted average of the scores above")]


def grade_index(doc, today=None):
    """Metrics for one index file."""
    today = today or date.today()
    plans = [(s, p) for s in doc.get("reporting_structure") or [] for p in s.get("reporting_plans") or []]
    n = len(plans) or 1
    required = ("plan_name", "plan_id", "plan_id_type", "plan_market_type", "issuer_name")
    complete = sum(1 for s, p in plans if all(p.get(f) for f in required)
                   and any(f.get("location") for f in s.get("in_network_files") or []))
    keys = Counter(tuple(sorted((k, str(v)) for k, v in p.items())) for _, p in plans)
    duplicates = sum(c - 1 for c in keys.values())
    consistent = sum(1 for _, p in plans if _id_matches(str(p.get("plan_id", "")), p.get("plan_id_type", "")))
    days = age_days(doc.get("last_updated_on"), today)
    metrics = [
        _metric("field completeness", f"{_pct(complete, n)} of {len(plans):,} plan entries",
                round(100 * complete / n, 1), 40,
                "share of plan entries with plan_name, plan_id, plan_id_type, plan_market_type, issuer_name "
                "and an in-network file"),
        _metric("duplicate plan entries", _pct(duplicates, n), _score(duplicates / n, 0.05), 20,
                "share of plan entries repeated exactly in the same file; 0 at 5%"),
        _metric("freshness", f"{days} days since last_updated_on {doc.get('last_updated_on')}",
                _freshness(days), 20, "100 if published within 35 days, linear to 0 at 365 days"),
        _metric("plan ID consistency", _pct(consistent, n), round(100 * consistent / n, 1), 20,
                "share of plan_id values whose format matches plan_id_type (EIN = 9 digits, HIOS = 5 digits "
                "+ optional state and product)"),
    ]
    return metrics + [_metric("FILE SCORE", "", _total(metrics), "", "weighted average of the scores above")]


def _id_matches(plan_id, id_type):
    if id_type == "ein":
        return bool(re.fullmatch(r"\d{9}", plan_id))
    if id_type == "hios":
        return bool(re.fullmatch(r"\d{5}([A-Z]{2}\d{3}(\d{4})?)?", plan_id))
    return False


def quality_report(rows):
    """Markdown report of the metric rows (QUALITY_COLUMNS): a score table, then each file's metrics."""
    def cell(v):
        return str(v).replace("|", "\\|").replace("$", "\\$").replace("\n", " ")

    def name(url):
        return url.rstrip("/").rsplit("/", 1)[-1].replace("%20", " ")

    files = {}
    for r in rows:
        files.setdefault((r["file"], r["file_type"]), []).append(r)
    lines = ["# C3: file quality", "",
             ("Every metric is scored 0-100; a file's score is the weighted average of its metrics. "
              "How each metric is calculated and why: see deliverables/3_file_quality_metrics.md."), "",
             "| File | Type | Score |", "|---|---|---:|"]
    lines += [f"| {cell(name(f))} | {t} | {m[-1]['score']} |" for (f, t), m in files.items()]
    for (f, t), metrics in files.items():
        lines += ["", f"## {cell(name(f))}", "", f"{t}: <{f}>", "",
                  "| Metric | Value | Score | Weight | How it is calculated |", "|---|---|---:|---:|---|"]
        lines += [f"| {' | '.join(cell(m[c]) for c in QUALITY_COLUMNS[2:])} |" for m in metrics]
    return "\n".join(lines) + "\n"
