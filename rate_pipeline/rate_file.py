"""In-network rate file -> CSV rows, streamed with ijson (the file is never loaded whole).

flatten()    every field in the file as a column; one row per negotiated price per NPI
rate_rows()  one row per NPI, TIN, address and billing code
"""

import csv
import json
import statistics
from datetime import date
from decimal import Decimal, InvalidOperation

import ijson

LEVELS = ["file", "provider", "service", "negotiated_rate", "price"]

RATE_FILE_COLUMNS = [
    "NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate",
    "NPI Type", "Provider Name", "Billing Code Type", "Description",
    "Rate Choice", "Specialty Source", "Address Confidence", "Address Evidence", "Note",
]
NO_EXPIRY = "9999-12-31"
OUTLIER_FACTOR = 10  # a rate over 10x, or under 1/10, of the typical price for the service is an outlier
MIN_PEERS = 3        # a typical price needs at least this many provider rates for the service
# Only procedure codes define one service whose price can be compared across providers. A hospital
# revenue code (RC) is a cost centre covering many services ($5 to $8,000 tests under RC 300), so
# its rates are never judged as outliers.
COMPARABLE_CODE_TYPES = {"CPT", "HCPCS"}
_EMPTY = {"rates": set(), "entries": {}, "descriptions": set(), "unreadable": 0, "zero": 0, "expired": 0}


def _new_group():
    return {k: v.copy() if isinstance(v, (set, dict)) else v for k, v in _EMPTY.items()}


def iter_top_level(f):
    """Yield (key, idx, value) for each top-level member of a JSON object.

    Array members are yielded one item at a time (idx = position); any other
    value once with idx None. Only one item is held in memory at a time.
    """
    key, in_array, idx, builder, depth = None, False, -1, None, 0
    for prefix, event, value in ijson.parse(f, use_float=False):
        if builder is not None:
            builder.event(event, value)
            depth += event in ("start_map", "start_array")
            depth -= event in ("end_map", "end_array")
            if depth == 0:
                yield key, (idx if in_array else None), builder.value
                builder = None
            continue
        if prefix == "":
            if event == "map_key":
                key, in_array = value, False
        elif prefix == key:
            if event == "start_array":
                in_array, idx = True, -1
            elif event == "end_array":
                in_array = False
            elif event == "start_map":
                builder, depth = ijson.ObjectBuilder(), 1
                builder.event(event, value)
            else:
                yield key, None, value
        elif in_array and prefix == f"{key}.item":
            idx += 1
            if event in ("start_map", "start_array"):
                builder, depth = ijson.ObjectBuilder(), 1
                builder.event(event, value)
            else:
                yield key, idx, value


def _flatten(obj, skip=(), prefix=""):
    """Dotted column -> text. Lists of plain values are joined with '|', lists of objects kept as JSON."""
    out = {}
    for key, value in obj.items():
        if not prefix and key in skip:
            continue
        name = prefix + key
        if isinstance(value, dict):
            out.update(_flatten(value, prefix=name + "."))
        elif isinstance(value, list):
            if all(not isinstance(v, (dict, list)) for v in value):
                out[name] = "|".join("" if v is None else str(v) for v in value)
            else:
                out[name] = json.dumps(value, default=str, ensure_ascii=False)
        else:
            out[name] = "" if value is None else str(value)
    return out


def _provider_rows(ref_fields, groups):
    """One flat row per NPI of each provider group (or one row if a group has no NPI)."""
    rows = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        g = {**ref_fields, **_flatten(group, skip=("npi",))}
        npis = group.get("npi") if isinstance(group.get("npi"), list) else [group.get("npi")]
        rows += [{**g, "npi": "" if npi is None else str(npi)} for npi in npis or [None]]
    return rows or [dict(ref_fields)]


def _columns(seen):
    """[(level, field, column name)]; a field used at two levels is prefixed with its level."""
    levels_of = {}
    for level in LEVELS:
        for field in seen[level]:
            levels_of.setdefault(field, []).append(level)
    return [(level, field, field if len(levels_of[field]) == 1 else f"{level}.{field}")
            for level in LEVELS for field in seen[level]]


def flatten(path, out_csv, on_row=None):
    """Write every field of the rate file at path to out_csv, one row per price per NPI.

    Pass 1 discovers every field name and collects provider groups (so the file
    may list them before or after the rates); pass 2 streams the rates. on_row,
    if given, receives each row's parts as {level: {field: value}}.
    """
    seen = {level: {} for level in LEVELS}
    header, providers, other = {}, {}, {}
    with open(path, "rb") as f:
        for key, idx, value in iter_top_level(f):
            if key == "provider_references" and idx is not None and isinstance(value, dict):
                ref = _flatten(value, skip=("provider_groups",))
                rows = _provider_rows(ref, value.get("provider_groups"))
                providers.setdefault(ref.get("provider_group_id", ""), []).extend(rows)
                for r in rows:
                    seen["provider"].update(dict.fromkeys(r))
            elif key == "in_network" and idx is not None and isinstance(value, dict):
                seen["service"].update(dict.fromkeys(_flatten(value, skip=("negotiated_rates",))))
                for neg in value.get("negotiated_rates") or []:
                    if not isinstance(neg, dict):
                        continue
                    seen["negotiated_rate"].update(
                        dict.fromkeys(_flatten(neg, skip=("negotiated_prices", "provider_groups"))))
                    if neg.get("provider_groups"):
                        for r in _provider_rows({}, neg["provider_groups"]):
                            seen["provider"].update(dict.fromkeys(r))
                    for price in neg.get("negotiated_prices") or []:
                        if isinstance(price, dict):
                            seen["price"].update(dict.fromkeys(_flatten(price)))
            elif idx is None:
                header.update(_flatten({key: value}))
            else:
                other[key] = other.get(key, 0) + 1  # unknown top-level array: reported, not guessed at
    seen["file"].update(dict.fromkeys(header))
    columns = _columns(seen)

    rows = prices = 0
    unresolved = set()
    with open(path, "rb") as f, open(out_csv, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow([name for _, _, name in columns])
        for key, idx, value in iter_top_level(f):
            if key != "in_network" or idx is None or not isinstance(value, dict):
                continue
            service = _flatten(value, skip=("negotiated_rates",))
            for entry in value.get("negotiated_rates") or [{}]:
                neg = entry if isinstance(entry, dict) else {}
                rate = _flatten(neg, skip=("negotiated_prices", "provider_groups"))
                members = []
                for ref_id in neg.get("provider_references") or []:
                    found = providers.get(str(ref_id))
                    if found is None:  # kept, with only the id it pointed to
                        unresolved.add(str(ref_id))
                        found = [{"provider_group_id": str(ref_id)}]
                    members += found
                if neg.get("provider_groups"):  # groups given inline instead of by reference
                    members += _provider_rows({}, neg["provider_groups"])
                for price in neg.get("negotiated_prices") or [{}]:
                    price_fields = _flatten(price) if isinstance(price, dict) else {}
                    prices += bool(price_fields)
                    for provider in members or [{}]:
                        parts = {"file": header, "provider": provider, "service": service,
                                 "negotiated_rate": rate, "price": price_fields}
                        writer.writerow([parts[level].get(field, "") for level, field, _ in columns])
                        rows += 1
                        if on_row:
                            on_row(parts)

    return {"rows": rows, "prices": prices, "columns": len(columns), "header": header,
            "unresolved_provider_references": sorted(unresolved), "unknown_top_level_arrays": other}


def collector(groups, tin_names, today=None):
    """on_row callback grouping usable rates by (NPI, TIN, billing code type, billing code) into
    groups, and collecting each TIN's business names into tin_names.

    Rates that are not a number, $0 or negative, or already expired are counted, not used.
    Each usable rate is kept with its service (code, billing class, modifier) so it can be
    compared with other providers' rates for the same service (see service_medians); rates of
    codes that don't define one service get no service and are never compared.
    """
    today = (today or date.today()).isoformat()

    def add(parts):
        p, s, price = parts["provider"], parts["service"], parts["price"]
        if p.get("tin.business_name"):
            tin_names.setdefault(p.get("tin.value", ""), set()).add(p["tin.business_name"])
        key = (p.get("npi", ""), p.get("tin.value", ""), s.get("billing_code_type", ""), s.get("billing_code", ""))
        g = groups.setdefault(key, _new_group())
        g["descriptions"].add(s.get("description", ""))
        expires = price.get("expiration_date", "")
        try:
            rate = Decimal(price.get("negotiated_rate", ""))
        except InvalidOperation:
            g["unreadable"] += 1
            return
        if rate <= 0:
            g["zero"] += 1
        elif expires and expires != NO_EXPIRY and expires < today:
            g["expired"] += 1
        else:
            service = (s.get("billing_code_type", ""), s.get("billing_code", ""), price.get("billing_class", ""),
                       price.get("billing_code_modifier", "")) \
                if s.get("billing_code_type") in COMPARABLE_CODE_TYPES else None
            g["rates"].add((rate, service))
            # the rate's version of the service, and the contracts (provider groups) it appears in
            version = (rate, service, price.get("billing_code_modifier", ""), price.get("billing_class", ""),
                       price.get("setting", ""))
            g["entries"].setdefault(version, set()).add(p.get("provider_group_id", ""))
    return add


def service_medians(groups):
    """{service: typical price}: the median of every provider's distinct rates for the service."""
    peers = {}
    for g in groups.values():
        for rate, service in g["rates"]:
            if service is not None:
                peers.setdefault(service, []).append(rate)
    return {svc: statistics.median(rates) for svc, rates in peers.items() if len(rates) >= MIN_PEERS}


def is_outlier(rate, median):
    """True when a rate is more than 10x, or less than 1/10, of the typical price for its service."""
    return median is not None and (rate > median * OUTLIER_FACTOR or rate * OUTLIER_FACTOR < median)


def merge(groups, more):
    """Merge grouped rates from another rate file into groups."""
    for key, g in more.items():
        target = groups.setdefault(key, _new_group())
        for k, v in g.items():
            if isinstance(v, dict):
                for version, contracts in v.items():
                    target[k].setdefault(version, set()).update(contracts)
            else:
                target[k] = target[k] | v if isinstance(v, set) else target[k] + v


def standard_settings(groups):
    """{(code type, code): (billing class, setting)} most common for each code across the file."""
    counts = {}
    for (_, _, code_type, code), g in groups.items():
        for _, _, _, billing_class, setting in g["entries"]:
            c = counts.setdefault((code_type, code), {})
            c[(billing_class, setting)] = c.get((billing_class, setting), 0) + 1
    return {code: max(c, key=lambda k: (c[k], k)) for code, c in counts.items()}


def choose_rate(entries, standard):
    """Pick one rate from {(rate, service, modifier, class, setting): contracts}; return (rate, how).

    1. the standard version of the service: no modifier, then the code's usual class and setting;
    2. the rate found in the most contracts (the provider's standard fee schedule);
    3. on a tie, the lower median of the tied rates, so the pick is always a published rate.
    """
    if len({e[0] for e in entries}) == 1:
        return next(iter(entries))[0], "only rate"
    versions = list(entries)
    versions = [v for v in versions if not v[2]] or versions
    versions = [v for v in versions if (v[3], v[4]) == standard] or versions
    contracts = {}
    for v in versions:
        contracts.setdefault(v[0], set()).update(entries[v])
    if len(contracts) == 1:
        return next(iter(contracts)), "only rate for the standard version of the service"
    top = max(len(c) for c in contracts.values())
    best = sorted(r for r, c in contracts.items() if len(c) == top)
    total = len(set().union(*contracts.values()))
    if len(best) == 1:
        return best[0], f"most common rate (in {top} of {total} contracts)"
    return best[(len(best) - 1) // 2], f"tie between {len(best)} rates: lower median"


def rate_rows(groups, providers, links, medians):
    """One row per NPI + TIN + linked address + billing code.

    links: {(tin, npi): [link rows from addresses.link]}; medians: from service_medians.
    Outlier rates are left out; one of the remaining rates is picked by choose_rate, with
    how it was picked beside it, and the lowest and highest of them are kept as Min and Max Rate.
    """
    standards = standard_settings(groups)
    rows = []
    for (npi, tin, code_type, code), g in sorted(groups.items()):
        p = providers.get(npi, {})
        outliers = sum(1 for rate, svc in g["rates"] if is_outlier(rate, medians.get(svc)))
        kept = {v: c for v, c in g["entries"].items() if not is_outlier(v[0], medians.get(v[1]))}
        rate, how = choose_rate(kept, standards.get((code_type, code))) if kept else ("", "")
        kept_rates = [v[0] for v in kept]
        descriptions = sorted(g["descriptions"])
        excluded = "; ".join(f"{n} {text} rate(s) excluded" for n, text in
                             ((g["unreadable"], "unreadable"), (g["zero"], "$0 or negative"),
                              (g["expired"], "expired"), (outliers, "outlier (over 10x or under 1/10 of the typical price)"))
                             if n)
        common = {
            "NPI": npi, "TIN": tin, "Billing Code": code, "Specialty": p.get("specialty", ""),
            "Rate": str(rate),
            "Min Rate": str(min(kept_rates)) if kept_rates else "",
            "Max Rate": str(max(kept_rates)) if kept_rates else "",
            "NPI Type": p.get("npi_type", ""), "Provider Name": p.get("name", ""),
            "Billing Code Type": code_type,
            "Description": descriptions[0] if len(descriptions) == 1
            else f"{len(descriptions)} service descriptions share this code",
            "Rate Choice": how,
            "Specialty Source": p.get("specialty_source", ""),
        }
        for link in links.get((tin, npi)) or [{"Address": "", "Confidence": "none", "Evidence": "",
                                                "Note": p.get("note", "")}]:
            rows.append({**common, "Address": link["Address"], "Address Confidence": link["Confidence"],
                         "Address Evidence": link["Evidence"],
                         "Note": "; ".join(n for n in (link["Note"], excluded) if n)})
    return rows
