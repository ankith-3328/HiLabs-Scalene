# Logic: Centene California commercial group PPO

**Scope:** Centene, commercial group, California, PPO network. Both individual (Type 1) and organization (Type 2) NPIs.

**Contents**
- [Sources](#sources)
- [C1: Parse the index files](#c1-parse-the-index-files)
- [C2: Find the PPO network](#c2-find-the-ppo-network) (part a of this deliverable)
- [C3: Grade the files](#c3-grade-the-files)
- [C4: Choose one rate](#c4-choose-one-rate)
- [C5: Map TIN → NPI → address](#c5-map-tin--npi--address) (part b of this deliverable)
- [C6: Build the rate file](#c6-build-the-rate-file)
- [Conflicts and how they were handled](#conflicts-and-how-they-were-handled)
- [Checks](#checks)
- [Possible improvements](#possible-improvements)

## Sources

| # | Source | What we take from it | Used in |
|---|---|---|---|
| S1 | Centene price transparency page (`centene.com/price-transparency-files.html`) | links to the index files, found fresh on each run | C1 |
| S2 | Centene's 11 index files | plans, plan IDs, market type, and each plan's rate file | C1, C2 |
| S3 | Health Net California in-network rate file (`…/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json`, 702 MB) | NPIs, TINs, TIN business names, billing codes, rates | C4, C5 |
| S4 | Health Net broker flyers: [EOA][eoa], [Salud][salud] | what the plan name codes "EOA" and "SAL" mean | C2 |
| S5 | CMS NPI Registry (NPPES) API | NPI type, names, practice addresses, taxonomy, date the record was last updated | C5 |
| S6 | CMS Doctors & Clinicians file (data.cms.gov, monthly, from Medicare enrollment) | where each clinician practises, and every active practice address (327,028) | C5 |
| S7 | IRS Exempt Organizations file | legal name and registered address of non-profit TINs (152 found) | C5 |
| S8 | NUCC taxonomy code set | taxonomy code → specialty name | C5 |
| S9 | HiLabs workbook (`network_mapping/data/hilabs_rate_benchmarking_weights.xlsx`) | taxonomy → HiLabs specialty; specialty and procedure weights | C3, C5 |

The Centene files are dated 2026-09-28 to 2026-10-06. Every run downloads the sources fresh; the same source data always gives the same outputs.

## C1: Parse the index files

- The 11 index files hold **3,275 plan entries**: brand, plan name, plan ID, market type and rate file link.
- **5 entries are exact repeats**, because two QualChoice files are identical. Removing them leaves **3,270 plans**.
- The 3,270 plans point to **33 rate files**, because many plans share one.

Output: `c1_index_plans.csv`.

## C2: Find the PPO network

### Step 1: keep commercial group plans

Keep a plan only if its `plan_market_type` is `group`. This removes Ambetter and Fidelis, which are all individual (Marketplace) plans.

### Step 2: keep California plans

No field gives the state, so two signals are used:
- the state in the rate file name (`…healthnet-ca_in-network.json`);
- the state inside HIOS plan IDs (`67138CA073`).

A name prefix such as `CA L …` is used only when neither exists. If signals disagree, the plan is left out. This never happened: every California plan had two signals that agreed.

QualChoice (Arkansas) and Health Net Oregon drop out, leaving **1,312 Health Net California commercial group plans**.

### Step 3: find each plan's network type

The index files have no network type field, and the rate file's network name is empty, so the **plan name** is the only evidence. The rules are checked in order; the first match wins.

| Order | Name contains | Network type | Plans | Why |
|---|---|---|---:|---|
| 1 | `PPO` | **PPO** | **455** | Preferred Provider Organization. Includes 3 names that also say EPO (e.g. `…IRONWORKERS FWP EPO$15`): as agreed with the team, a name with PPO and another type counts as PPO |
| 2 | `PPO` joined to another word | **PPO** | **1** | `CA L CUSPPO 15/250/20% RAP` is a typo of "CUS PPO" |
| 3 | `POS` | POS | 6 | Point of Service |
| 4 | `HMO` | HMO | 719 | Health Maintenance Organization |
| 5 | `EOA` | HMO | 127 | "Elect Open Access", an HMO-based plan (S4) |
| 6 | `SAL … Mex` | HMO | 3 | "Salud Mexico", care through a Mexican HMO-style plan (S4) |
| 7 | `SAL` | HMO | 1 | "Salud con Health Net", Health Net's Salud HMO (S4) |
| – | none of these | UNKNOWN | 0 | never assumed to be PPO |

A plan is ruled out only when its name gives another type and no PPO. A PPO plan whose name also mentions another type says so in its reason column.

### Result

- **456 PPO plans** (`c2_ppo_plans.csv`).
- **856 plans ruled out:** 850 HMO and 6 POS.
- All 456 PPO plans use **the same rate file** (S3). Every later step uses this file.

## C3: Grade the files

Every index file and the PPO rate file get a 0–100 quality score. The metrics, how each is calculated and why, and the scores are in [3_file_quality_metrics.md](3_file_quality_metrics.md). The per-file report is `c3_file_quality.md`.

## C4: Choose one rate

The rate file can list several rates for the same NPI + TIN + billing code. One rate is kept for each:

1. **Remove bad rates:** not a number, \$0 or negative, or expired. (None in this file.)
2. **Remove outliers:** a procedure (CPT/HCPCS) rate more than **10× or under one-tenth of the typical price** for the same service.
   - The typical price is the median of all providers' rates for the same code, billing class and modifier. At least 3 rates are needed.
   - Example: an ECG reading at \$475 when the typical price is \$17.
   - Normal differences between providers (2–3×) are kept.
   - Hospital revenue codes are not checked. A revenue code covers many services (RC 300 covers every lab test, \$5 to \$8,000), so it has no typical price.
3. **One rate left:** keep it.
4. **Several rates left** (3,011 of 21,858 keys): pick one in this order:
   1. **Standard version of the service:** rates with no modifier first, then the most common billing class and setting for that code.
   2. **Most common rate:** the rate used in the most of that provider's contracts.
   3. **Tie:** the lower median of the tied rates.

**Why not a plain median?**
- **Every rate is a real published price.** A median of \$80 and \$100 is \$90, which no contract has. Every rate we keep can be found in Centene's file.
- Many "different" rates are different versions of a service. Code 71045 alone is the full fee; 71045-26 is only the doctor's part. Choosing the standard version first avoids mixing them.
- **The most common rate is the provider's standard price.** A rate used in most contracts is the fee schedule; a rate in one contract is the exception.

**Min and max rate:** next to the chosen rate, `Min Rate` and `Max Rate` are the lowest and highest of the key's rates after steps 1 and 2 (bad rates and outliers removed). They show how far the provider's rates for the code spread around the chosen one. With one rate left, all three are the same.

Output: `c4_ppo_rates.csv` (NPI, TIN, Billing Code, Rate, Min Rate, Max Rate): 21,750 rows.

## C5: Map TIN → NPI → address

**The problem:** one TIN can have many addresses, and one doctor can work at many addresses for different TINs. NPPES addresses are often out of date.

**The approach:** each source suggests addresses, and each address keeps the evidence behind it. Two questions are asked:
1. Does the address belong to **this TIN**, not just to the NPI?
2. Is the address **current**?

### Step 1: TIN → NPI

The rate file is the only source that links TINs to NPIs. Each provider group is one TIN, with its business name and the NPIs that bill under it. This gives **3,735 NPIs and 2,888 TINs**. One NPI is Centene's placeholder `0`, leaving **3,734 valid NPIs**. An NPI billing under two TINs is treated as two separate TIN–NPI pairs.

### Step 2: collect candidate addresses

| Source | Addresses |
|---|---|
| NPPES (S5) | the NPI's primary and secondary practice addresses (never the mailing address) |
| CMS Doctors & Clinicians (S6) | each address where the clinician practises, with the group they work for |
| IRS (S7) | the TIN's registered address |

Addresses are compared on street and 5-digit ZIP, with standard abbreviations and without the suite number. So "1625 Stockton Boulevard, Suite 208" matches "1625 STOCKTON BLVD #210".

### Step 3: check the address belongs to the TIN

| Evidence | Rule | Links |
|---|---|---:|
| One-to-one | the TIN bills only with this NPI, and the NPI only under this TIN | 3,889 |
| Organization name | the NPI's organization name in NPPES matches the TIN's business name | 2,380 |
| Authorized official | the TIN is named after the organization's authorized official (e.g. TIN "WU, M.D." → the clinic Dr Wu runs) | 907 |
| IRS name | the TIN's IRS legal name matches the NPI's organization name | 578 |
| Own practice | the TIN is a short name containing the doctor's surname (e.g. "TAYLOR, D.P.M.") | 427 |
| Shared address | two or more NPIs under the TIN have this address | 378 |
| IRS address | the address is the TIN's IRS registered address | 80 |
| CMS group | CMS shows the clinician working here for a group whose name matches the TIN | 12 |

**Name matching:** legal suffixes (INC, LLC) and credentials (MD) are ignored; a word cut short in the rate file still matches ("AMER" = "AMERICA"); most of the shorter name's words must match. A surname inside a long group name does not count (SMITH in "SMITH & JONES ORTHOPEDIC").

### Step 4: check the address is current

NPPES alone can't prove this, because providers often don't update it. An address counts as current when a source other than NPPES confirms it:

| Evidence | Why it counts | Links |
|---|---|---:|
| CMS active practice site | Medicare clinicians are enrolled to practise at this address now | 3,694 |
| CMS clinician record | CMS lists this clinician at this address | 363 |
| Shared address | several providers' records agree | 378 |
| IRS address | the TIN's registered address | 80 |

With no such confirmation, the only signal is when the NPPES record was last updated. Updated within **2 years** counts as recent; older counts as **likely stale**.

### Step 5: keep and score the addresses

An address is kept for a TIN only if some evidence ties it to that TIN. If none does, the NPI's NPPES primary address is kept as low confidence. So an address where a doctor works for a different group is not attached to this TIN.

| Confidence | Rule | Links |
|---|---|---:|
| **High** | belongs to the TIN **and** confirmed current by a source other than NPPES | 3,992 |
| **Medium** | belongs to the TIN; found only in NPPES, but the record was updated within 2 years | 474 |
| **Low** | belongs to the TIN but the NPPES record is over 2 years old (667), or not tied to the TIN (169) | 836 |

A **verified address** means high or medium confidence.

**Coverage:**
- **3,049 of 3,734 NPIs (81.7%)** have a verified address.
- **2,473 of 2,888 TINs (85.6%)** have a verified address.
- 99.8% of NPIs and 99.9% of TINs have some address.

Many-to-many is common: 680 TINs have more than one address, and 1,466 links come from NPPES secondary addresses.

### Specialty

Specialty comes from the NPI's primary taxonomy in NPPES, looked up in this order:
1. the HiLabs taxonomy-specialty crosswalk (S9): 2,136 NPIs;
2. the NUCC name (S8), mostly organizations such as "General Acute Care Hospital", since the crosswalk covers individuals only: 1,590 NPIs;
3. NPPES's own description.

9 NPIs are not in the NPI Registry, so they have no specialty.

Output: `c5_ppo_rates_by_address.csv` (NPI, TIN, Billing Code, Address, Specialty, Rate, Address Confidence): 34,967 rows, one per address. The **Address Confidence** column gives each address's confidence (high, medium or low, from Step 5). It is only in this file, not in the final rate file.

## C6: Build the rate file

The final file has one row per **NPI + TIN + address + billing code**, with 6 columns: **NPI, TIN, Billing Code, Address, Specialty, Rate**.

It has the same rows as the C5 file, without the confidence column. A provider who practises at several addresses for the same TIN has one row per address, each with the same rate. Rows with an invalid NPI, no address, or no usable rate are removed.

Output: `output/1_ppo_rate_file.xlsx`, written by each run. The submitted copy is `deliverables/1_ppo_rate_file.xlsx`: 34,967 rows.

## Conflicts and how they were handled

| Conflict | How often | What we did |
|---|---:|---|
| Several rates for the same NPI + TIN + code | 3,011 keys | picked one: standard version, then most common rate, then lower median (C4) |
| Rate over 10× or under one-tenth of the typical price | 106 rates | excluded before picking a rate; 134 rows left with no rate were removed |
| One billing code under many descriptions (e.g. a home-health code under 43) | common for revenue codes | grouped under the one code |
| Sources give different addresses | — | each address is checked on its own; only those tied to the TIN are kept, each with a confidence |
| NPPES address likely stale | 667 links | kept as low confidence unless CMS or a shared address confirms it |
| Doctor's other addresses belong to other groups | — | not attached to this TIN |
| Address outside California (national labs, out-of-state systems) | 226 links | kept, because Health Net California contracts with them |
| NPI not in the NPI Registry (8) or placeholder `0` (1) | 9 NPIs | removed from C4–C6: a row needs a real NPI |
| Plan market or state signals disagree | 0 | the plan would be left out, not guessed |
| Plan name with no network type | 0 | it would be UNKNOWN, never PPO |

## Checks

- **Rate file counts:** 1,500,288 prices and 2,825 provider references, matching a separate count of the raw file.
- **Provider links:** every rate's provider reference points to a provider group in the file.
- **Unique rows:** C4 is unique on NPI + TIN + code; C5 on NPI + TIN + code + address; C6 on NPI + TIN + address + code.
- **Same input, same output:** re-running on the same data gives the same files (tested).
- **51 automated tests** cover downloads, plan and network rules, rate extraction, the rate rule, every quality metric, NPPES parsing, address linking and the end-to-end outputs.

## Possible improvements

| Addition | What it would add |
|---|---|
| HiLabs MCheck/R3 address validation | standardised addresses and a check that each one is real and deliverable |
| Health Net's provider directory API | the payer's own provider–address–network records, which would also confirm PPO membership |
| CMS Provider of Services files | current addresses for hospitals, labs and home health agencies (most Type 2 NPIs) |
| Medical Board of California licences | addresses for doctors not enrolled in Medicare |

[eoa]: https://healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/hn-eoa-oe-flyer-2025.pdf
[salud]: https://media.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/salud/hn-broker-salud-flyer-2025.pdf
