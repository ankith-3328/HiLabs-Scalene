# Centene provider rate pipeline

Finds Centene's PPO network in its price transparency files, grades the files' quality, and maps every provider to a practising address. The result is a provider-level rate file at the NPI, TIN, billing code and address level.

**Scope:** Centene, commercial group, California, PPO network, Type 1 and Type 2 NPIs.

## Run it

```bash
uv sync                      # install (once)
uv run main.py               # full run (~16 minutes): index files, rate file, NPI Registry, CMS, IRS
uv run main.py --index-only  # only C1/C2 and the index file grades (seconds)
uv run pytest                # run the tests
```

The scope is a command-line option, so the same code maps any state, market or network, e.g. `uv run main.py --state OR --network HMO`. The defaults are `--state CA --market group --network PPO`. There are no hardcoded paths: outputs go to `--out` (default `output/`), and the HiLabs supplement is read from `network_mapping/data/`.

## Outputs

### Deliverables: `deliverables/`

These are the files from our submitted run. The pipeline **never writes to this folder**: a re-run writes its own rate file to `output/` and prints its metrics to the console, so both can be compared with the submitted ones.

| # | Deliverable | File |
|---|---|---|
| 1 | Rate file (Excel) | [`1_ppo_rate_file.xlsx`](deliverables/1_ppo_rate_file.xlsx): `NPI, TIN, Billing Code, Address, Specialty, Rate`, one row per NPI + TIN + address + billing code |
| 2 | Logic file | [`2_logic.md`](deliverables/2_logic.md): how the PPO network was identified, how TIN → NPI → address and specialty were linked, the sources used, and how conflicts were handled |
| 3 | File quality metrics | [`3_file_quality_metrics.md`](deliverables/3_file_quality_metrics.md): every file quality metric, how it is calculated and why |
| 4 | Summary file | [`4_summary.md`](deliverables/4_summary.md): high-level metrics of the submitted run |
| 6 | Code repo | this repository; re-run with the commands above |

### Challenge outputs: `output/`

Each run downloads fresh data and overwrites these:

| File | Challenge | What it is |
|---|---|---|
| `c1_index_plans.csv` | C1 | every plan in every Centene index file: `brand, plan_name, plan_id, market_type, rate_file_url` |
| `c2_ppo_plans.csv` | C2 | the California commercial group **PPO** plans, with the reason and their rate file |
| `c3_file_quality.md` | C3 | quality report: the score of every file used, then each metric's value, score, weight and calculation |
| `c4_ppo_rates.csv` | C4 | `NPI, TIN, Billing Code, Rate`: the rate chosen (most frequent) for each NPI + TIN + billing code |
| `c5_ppo_rates_by_address.csv` | C5 | `NPI, TIN, Billing Code, Address, Specialty, Rate, Address Confidence`: the C4 rates at every address linked to the NPI and TIN, with the address's confidence (high, medium or low) |
| `1_ppo_rate_file.xlsx` | C6 | this run's rate file (deliverable 1) |

## Data sources

| Source | Used for |
|---|---|
| Centene price transparency page and index files | plans, networks, rate file links (C1, C2) |
| Health Net California in-network rate file | NPIs, TINs, codes, rates (C4) |
| CMS NPI Registry (NPPES) API | NPI type, names, primary and secondary practice locations, taxonomy, record update date |
| CMS Doctors & Clinicians (data.cms.gov) | the practice addresses and group affiliations of Medicare-enrolled clinicians; an address source independent of NPPES |
| IRS Exempt Organizations Business Master File | TIN (EIN) → legal name and registered address for non-profit organizations |
| NUCC taxonomy code set | taxonomy code → specialty name |
| HiLabs supplement (`network_mapping/data/hilabs_rate_benchmarking_weights.xlsx`) | taxonomy → HiLabs specialty, specialty weights, specialty-procedure weights (specialty, code coverage) |

## Code

| File | What it does |
|---|---|
| `main.py` | entry point: runs the challenges in order and writes the CSVs |
| `network_mapping/downloader.py` | downloads, with timeouts and retries |
| `network_mapping/plan_index.py` | index parsing, state rules, network-type rules (C1, C2) |
| `network_mapping/file_quality.py` | file quality metrics and scores (C3) |
| `network_mapping/rate_file.py` | streams the rate file into the rate CSVs; the rate selection rule (C4, C6) |
| `network_mapping/npi_registry.py` | NPI Registry lookups |
| `network_mapping/address_linking.py` | TIN → NPI → address linking across sources (C5) |
| `network_mapping/specialty_lookup.py` | HiLabs supplement and NUCC taxonomy |
| `network_mapping/data/` | the HiLabs supplement workbook |

**Scalability:**
- **Streaming:** the rate file is read with `ijson` one item at a time and never loaded whole. It's graded while it streams. Its fields are discovered from the file itself, so a file with extra or different fields is still extracted in full.
  The CMS Doctors & Clinicians national file (~900 MB) is also streamed once, row by row.
- **Parallel processing:**
  - index files, NPI Registry lookups and IRS files are fetched in parallel;
  - when a network has several rate files, each one is processed in its own worker process.
- **Downloads:** the rate file and the CMS file are downloaded to temporary folders that are deleted automatically.

## C1: Parse the index files

- Centene's page `centene.com/price-transparency-files.html` links to **11 index files**: Ambetter, Fidelis, Health Net, QualChoice and 7 QualChoice sub-indexes.
- Each index file lists plans and the rate file each plan uses.
- Together they hold 3,275 plan entries.
- **Duplicates:** 5 entries were exact repeats, from two QualChoice sub-indexes that are identical files. Removing them leaves **3,270 plans**.
- **Shared rate files:** the 3,270 plans point to only **33 unique rate files**, because many plans share a file. Each plan's rate file is in `c1_index_plans.csv`.

| Brand | Plans | Market type |
|---|---:|---|
| Health Net | 1,693 | group (CA and OR) |
| Ambetter | 1,120 | individual (Marketplace) |
| QualChoice | 363 | group (Arkansas) |
| Fidelis | 94 | individual (New York) |

## C2: Find the PPO network

### Step 1: narrow down to California commercial group

- **Market:** keep plans whose `plan_market_type` is `group`. This drops Ambetter and Fidelis, which are all `individual`.
- **State:** keep plans that are in California. The files have no state field, so state comes from:
  - the rate file name (`…_healthnet-ca_in-network.json`), and
  - the state code inside HIOS-style plan IDs (`67138CA073`).
- **What's left:** QualChoice (Arkansas) and Health Net Oregon drop out. That leaves **1,312 Health Net California plans**.

### Step 2: work out each plan's network type from its name

The files have no network-type field, so the plan name is the only clue. Each name is checked against these rules **in order**, and the first match wins:

| # | If the name contains | Type | Plans | Why |
|---|---|---|---:|---|
| 1 | `PPO` | **PPO** | **455** | Preferred Provider Organization. Includes 3 names that also mention EPO (Exclusive Provider Organization), e.g. `…IRONWORKERS FWP EPO$15`; PPO combined with any other type counts as PPO. |
| 2 | `…PPO` joined to another word | **PPO** | **1** | Preferred Provider Organization; `CA L CUSPPO 15/250/20% RAP` is a typo of "CUS PPO". |
| 3 | `POS` | POS | 6 | Point of Service plan. |
| 4 | `HMO` | HMO | 719 | Health Maintenance Organization. |
| 5 | `EOA` | HMO | 127 | "Elect Open Access": an HMO-based plan; members pick an HMO doctor group ([EOA flyer][eoa]). |
| 6 | `SAL … Mex` | HMO | 3 | "Salud Mexico": care through SIMNSA, a Mexican HMO-style plan ([Salud flyer][salud]). |
| 7 | `SAL` | HMO | 1 | "Salud con Health Net": Health Net's Salud HMO line ([Salud flyer][salud]). |
| – | none of the above | UNKNOWN | 0 | No network type in the name: listed for review, never assumed PPO. None in this data. |

PPO is checked **first**: a name that mentions PPO counts as PPO even if it also mentions another type. Plans ruled out are those whose name names another type (HMO, EOA, Salud, POS) and not PPO.


### Result

- **456 PPO plans**, all Health Net California commercial group. They are in `c2_ppo_plans.csv`.
- **856 plans ruled out:** 850 HMO and 6 POS.

### The PPO rate file

All **456 PPO plans** in the index files link to the **same rate file**:

```
https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json
```

This is the rate file used for all further tasks: the chosen rates (`c4_ppo_rates.csv`), the rates by address (`c5_ppo_rates_by_address.csv`) and the rate file deliverable (`deliverables/1_ppo_rate_file.xlsx`).

## C3: Grade the PTD files

**PTD (Price Transparency Data)** files are the files health plans publish about their prices. We grade the two kinds we use:
- the **rate file**, which holds the prices;
- the **index files**, which list the plans.

### Rate file: Health Net California

| Metric | What it measures | Result | Score | Weight |
|---|---|---|---:|---:|
| Data freshness | How recently was the file updated? | last updated on 2026-10-06 | 100 | 10% |
| Expired rates | What percentage of rates have expired? | 0% | 100 | 5% |
| Price validity | Are rates valid, readable, and greater than zero? | No invalid rates | 100 | 20% |
| Price outliers | What percentage of procedure (CPT/HCPCS) rates are more than 10× higher or lower than the typical price for the same service? | 0.75% | 93 | 15% |
| Price duplication | How often are identical rates repeated? | 97.6% | 0 | 5% |
| Price consistency | How often does a provider have conflicting rates for the same service? | 13.8% | 45 | 15% |
| Service coverage | How well does the file cover important services across specialties? | 50.4% | 50 | 20% |
| Provider validity | What percentage of provider NPIs are valid? | 99.8% | 100 | 10% |

### Index files:


| Metric | What it measures | Result | Score | Weight |
|---|---|---|---:|---:|
| Metadata completeness | Are the required plan details present (name, ID, ID type, market type, issuer, rate file link)? | 100% complete in all 11 files | 100 | 40% |
| Duplicate plans | Is any plan listed twice within a file? | none | 100 | 20% |
| Data freshness | How recently was the index updated? | last updated on 2026-09-28 (Health Net: 2026-09-30) | 100 | 20% |
| Plan ID validity | Do plan IDs match their stated ID type? | 100% in 10 files; 90.9% in Health Net's | 91–100 | 20% |

- For metric definitions, scoring formulas, and validation logic, see [`3_file_quality_metrics.md`](deliverables/3_file_quality_metrics.md).
- Individual file scores are available in [`output/c3_file_quality.md`](output/c3_file_quality.md).

## C4: Extract Rates

The 702 MB rate file is streamed one record at a time to minimize memory usage.  
Every published price is read; the rate chosen for each NPI + TIN + billing code is written to `c4_ppo_rates.csv`.

For each NPI + TIN + billing code, one rate is kept:

- **Prioritize standard rates:** Prefer rates without modifiers, using the most common billing class and setting for the code.
- **Select the most frequent rate:** Choose the rate appearing in the most provider contracts.
- **Break ties using the lower median:** If multiple rates are equally frequent, select the lowest median.

**Rationale:**
- **Traceability:** Every selected rate is an actual published price.
- **Representativeness:** The most frequent rate reflects the provider's standard contracted price.

### Result

- **1,500,288 prices** read from the rate file.
- **106 outlier rates** excluded (over 10× or under one-tenth of the typical price).
- **3,011 of 21,858** NPI + TIN + code keys had several rates; one was picked for each.
- **21,750 rows** in `c4_ppo_rates.csv`: `NPI, TIN, Billing Code, Rate`.

## C5: Map TIN → NPI → address

### Step 1: TIN → NPI

The rate file is the only source that links TINs to NPIs. It gives **3,734 valid NPIs** (672 individuals, 3,054 organizations, 9 not in the NPI Registry) and **2,888 TINs**.

### Step 2: collect candidate addresses

| Source | Addresses |
|---|---|
| NPI Registry (NPPES) | each NPI's primary and secondary practice addresses |
| CMS Doctors & Clinicians | where each Medicare clinician practises, and for which group |
| IRS | the TIN's registered address (non-profits) |

### Step 3: keep addresses that belong to the TIN

An address is kept for a TIN only if something ties it to that TIN, for example:
- the NPI's organization name matches the TIN's business name;
- the TIN bills only with this NPI;
- several NPIs under the TIN share the address;
- it is the TIN's IRS registered address.

### Step 4: score each address

| Confidence | Rule |
|---|---|
| **High** | belongs to the TIN **and** confirmed current by CMS, the IRS or a shared address |
| **Medium** | belongs to the TIN; found only in NPPES, but the record was updated within 2 years |
| **Low** | the NPPES record is over 2 years old (likely stale), or the address isn't tied to the TIN |

A **verified address** means high or medium.

### Step 5: add the specialty

From the NPI's primary taxonomy: the HiLabs crosswalk first (2,136 NPIs), then the NUCC name (1,590 NPIs).

### Result

- **3,049 of 3,734 NPIs (81.7%)** have a verified address.
- **2,473 of 2,888 TINs (85.6%)** have a verified address.
- **34,967 rows** in `c5_ppo_rates_by_address.csv`: `NPI, TIN, Billing Code, Address, Specialty, Rate, Address Confidence`.

## C6: Build the rate file

The final rate file has one row per **NPI + TIN + address + billing code**, with 6 columns: `NPI, TIN, Billing Code, Address, Specialty, Rate`. It has the same rows as the C5 file, without the confidence column.

Rows that can't be used are removed:

| Reason | Rows |
|---|---:|
| Centene's placeholder NPI `0` | 5 |
| NPI has no address in any source | 49 |
| only rates were outliers | 134 |

### Result

- **34,967 rows** in `deliverables/1_ppo_rate_file.xlsx` (a re-run writes `output/1_ppo_rate_file.xlsx`).
- The headline numbers are in [`4_summary.md`](deliverables/4_summary.md); how each step was done, and why, is in [`2_logic.md`](deliverables/2_logic.md).

[eoa]: https://healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/hn-eoa-oe-flyer-2025.pdf
[salud]: https://media.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/salud/hn-broker-salud-flyer-2025.pdf
