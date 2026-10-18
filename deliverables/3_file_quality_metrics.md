# File quality metrics (C3)

These metrics grade every file the pipeline uses: each Centene index file and each in-network rate file.

- **Where the scores are:** every run writes them to the report `output/c3_file_quality.md`: a table of file scores, then for each file every metric's value, its 0–100 score, its weight, and how it was calculated.
- **Where the code is:** `rate_pipeline/file_quality.py`.

## How a score is built

1. **Each metric measures a share** of good or bad rows, e.g. "4% of prices are \$0".
2. **The share becomes a score from 0 to 100.**
   - No problems scores 100.
   - The score drops evenly as problems grow, and reaches 0 at a stated **limit**. Example: the limit for \$0 prices is 5%, so 4% \$0 prices scores 20.
   - A limit is the point where the problem makes the file unusable for that purpose.
3. **The file score is the weighted average** of its metric scores. Weights reflect how much each metric matters to a contracting team.

The rate file is graded while it is read, so even a very large file is never loaded into memory.

## Rate file metrics

| Metric | Weight | How it is calculated | Why it matters |
|---|---:|---|---|
| Freshness | 10 | Days between the file's `last_updated_on` and the run date. The score is 100 within 35 days, falling linearly to 0 at 365 days | The rule requires monthly publication. An old file means old contracts |
| Expired rates | 5 | Share of rate rows whose `expiration_date` is before the run date; `9999-12-31` means no expiry. Tolerance 10% | An expired rate is no longer payable, so it can't be benchmarked |
| \$0, negative or unreadable rates | 20 | Share of rate rows with `negotiated_rate` ≤ 0 or not a number. Tolerance 5% | These are placeholders, not prices, and they corrupt any average |
| Outlier rates | 15 | Share of providers' distinct **CPT/HCPCS** rates that are **more than 10× or less than one-tenth of the typical price**. The typical price is the median of all providers' rates for the same code + billing class + modifier, and needs at least 3 rates. Hospital revenue-code (RC) rates are not judged. Tolerance 10% | Catches data errors: an ECG reading at \$475 when the typical price is \$17, or a blood draw at \$290 vs \$13. Normal price differences between providers (2–3×) are never flagged. A revenue code is a hospital cost centre covering many different services (RC 300 covers every lab test, \$5 to \$8,000), so its rates can't be compared this way. The same rule removes outliers from the rate file (C4) |
| Duplicate prices | 5 | Share of rate rows repeating a price already listed for the same NPI, TIN, code, billing class, setting, modifier, place of service and extra code (description text ignored). The strict count, identical in every field, is reported alongside. Tolerance 10% | Duplicates inflate counts and weight averages unfairly |
| Conflicting rates | 15 | Share of NPI + TIN + billing code keys with more than one distinct rate. Tolerance 25% | When one provider has several prices for the same code, a contracting team can't tell which one applies |
| **Code coverage** (HiLabs weights) | 20 | Each NPI gets a HiLabs specialty, through the Taxonomy-Spec crosswalk. For each specialty with providers in the file, take the **Spec-Proc-weighted share of its key procedures that are priced for those providers**. Then average across specialties using the **Specialty Weights**. A second value reports how many of all specialties' key codes are priced anywhere in the file | A file can be large and still miss the services that drive spend. This measures coverage of what matters, as defined by HiLabs' own weights |
| Provider validity | 10 | Share of NPIs with a valid check digit (CMS Luhn rule over the 80840 prefix) **and** a record in the NPI Registry | A rate for an unidentifiable provider can't be used |

## Index file metrics

| Metric | Weight | How it is calculated | Why it matters |
|---|---:|---|---|
| Field completeness | 40 | Share of plan entries with `plan_name`, `plan_id`, `plan_id_type`, `plan_market_type`, `issuer_name` and an in-network file | Network mapping (C1/C2) depends on these fields |
| Duplicate plan entries | 20 | Share of plan entries repeated exactly within the same file. Tolerance 5% | Duplicates distort plan counts |
| Freshness | 20 | Same rule as for rate files | — |
| Plan ID consistency | 20 | Share of `plan_id` values whose format matches `plan_id_type`: EIN = 9 digits; HIOS = 5-digit issuer, optionally followed by state, product and variant | A mislabelled ID breaks joins to other plan data. For example, Health Net labels HIOS IDs as `ein` |

## Scores

**Health Net California in-network rate file** (the PPO network's file): **75.7 / 100**

| Metric | Value | Score | Weight |
|---|---|---:|---:|
| freshness | last updated on 2026-10-06 | 100 | 10 |
| expired rates | 0.00% | 100.0 | 5 |
| \$0, negative or unreadable rates | 0.00% | 100.0 | 20 |
| outlier rates | 0.75% of 14,178 CPT/HCPCS provider rates | 92.5 | 15 |
| duplicate prices | 97.58% repeat a price already listed for the same provider and service; 43.52% are identical in every field | 0.0 | 5 |
| conflicting rates | 13.78% of 21,858 NPI+TIN+code keys | 44.9 | 15 |
| code coverage (HiLabs weights) | 50.4% specialty-matched; 75.3% of all specialties' key codes priced anywhere | 50.4 | 20 |
| provider validity | 99.76% of 3,735 NPIs | 99.8 | 10 |

**What the score says:**
- **Strengths:** the file is fresh (last updated on 2026-10-06), has no \$0, expired or unreadable rates, and valid providers. Only 0.75% of procedure rates are outliers.
- **Weaknesses:**
  - **Massive repetition:** 97.6% of rows repeat a price already listed for the same provider and service, and 43.5% are identical in every field. For example, one home-health code (RC 23 + HIPPS 1EA11) carries the same price under 43 different descriptions, so the descriptions don't reliably match the codes.
  - **Conflicting rates:** 13.8% of NPI + TIN + code keys have several rates.
  - **Code coverage:** half of the specialty-weighted key procedures are priced for the providers who would bill them (50.4%). The file is 99% institutional revenue-code rates.

**Index files** (all 11): **98.2 – 100 / 100**

| Metric | Value | Score | Weight |
|---|---|---:|---:|
| field completeness | 100.00% of 3,275 plan entries, in every file | 100.0 | 40 |
| duplicate plan entries | 0.00% in every file | 100.0 | 20 |
| freshness | last updated on 2026-09-28 (Health Net: 2026-09-30) | 100 | 20 |
| plan ID consistency | 100.00% in 10 files; 90.90% in Health Net's | 90.9 – 100.0 | 20 |

**Index file scores:** `2026-09-28_ambetter_index.json` 100.0; `2026-09-28_fidelis_index.json` 100.0; `2026-09-28_qualchoice_index.json` 100.0; `2026-09-28_qualchoice_alf_plan_index.json` 100.0; `2026-09-28_qualchoice_arnold_and_blevins_index.json` 100.0; `2026-09-28_qualchoice_big_four_index.json` 100.0; `2026-09-28_qualchoice_city_of_fort_smith_index.json` 100.0; `2026-09-28_qualchoice_city_of_hot_springs_index.json` 100.0; `2026-09-28_qualchoice_dillards_index.json` 100.0; `2026-09-28_qualchoice_jv_manufacturing_index.json` 100.0; `2026-09-30_healthnet_index.json` 98.2.

Health Net's index loses points only on plan ID consistency: 90.90% of plan IDs match their type. 154 HIOS IDs (e.g. `67138CA073`) are labelled `ein`.
