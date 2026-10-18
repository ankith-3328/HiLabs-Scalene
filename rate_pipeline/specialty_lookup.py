"""Reference data: HiLabs specialty supplement and the NUCC taxonomy code set."""

import csv
import io

import openpyxl

from rate_pipeline.downloader import fetch

NUCC_URL = "https://www.nucc.org/images/stories/CSV/nucc_taxonomy_261.csv"


def load_supplement(path):
    """HiLabs 'Rate Benchmarking Individual Weights' workbook.

    Returns (xwalk, spec_weights, spec_proc):
      xwalk         {taxonomy code: (spec code, specialty)}       individual taxonomies only
      spec_weights  {spec code: weight}                           how much each specialty matters
      spec_proc     {spec code: {procedure code: weight}}         each specialty's key procedures
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    def rows(sheet):
        it = wb[sheet].iter_rows(values_only=True)
        header = [str(h).strip() for h in next(it)]
        return [dict(zip(header, r, strict=False)) for r in it if any(v is not None for v in r)]

    xwalk = {str(r["Taxonomy Code"]).strip(): (str(r["SPEC_CD"]).strip(), str(r["SPEC_DESCR"]).strip())
             for r in rows("Taxonomy-Spec Xwalk")}
    spec_weights = {str(r["Spec Code"]).strip(): float(r["Specialty Weight"] or 0) for r in rows("Specialty Weights")}
    spec_proc = {}
    for r in rows("Spec-Proc Weights"):
        spec_proc.setdefault(str(r["SPEC_CD"]).strip(), {})[str(r["PROC_CD"]).strip()] = float(r["SPEC_PROC_WEIGHT"] or 0)
    return xwalk, spec_weights, spec_proc


def load_nucc(url=NUCC_URL):
    """{taxonomy code: display name} from the NUCC Health Care Provider Taxonomy code set."""
    text = fetch(url).decode("latin-1")
    return {r["Code"].strip(): (r.get("Display Name") or r.get("Classification") or "").strip()
            for r in csv.DictReader(io.StringIO(text))}


def specialty(provider, xwalk, nucc):
    """(spec code, specialty, source) for a provider record from nppes.parse.

    The primary taxonomy decides: the HiLabs crosswalk first (the specialties the
    weights are defined for), then the NUCC display name, then NPPES's own text.
    """
    code = provider.get("taxonomy", "")
    if code in xwalk:
        return *xwalk[code], "HiLabs taxonomy-specialty crosswalk"
    if nucc.get(code):
        return "", nucc[code], "NUCC taxonomy"
    if provider.get("specialty"):
        return "", provider["specialty"], "NPPES taxonomy description"
    return "", "", ""
