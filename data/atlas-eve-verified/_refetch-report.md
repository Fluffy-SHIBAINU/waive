# Refetch and verify

1340 sheets. Sources refetched with Waive's downloader and the full verifier run.

| Metric | Count |
|---|---|
| income_rule_verified | 1035 |
| all_sources_fetched | 1219 |
| no_source_fetched | 23 |
| sources_hash_match | 2747 |
| sources_hash_changed | 780 |
| accepted_fields | 10119 |
| rejected_fields | 2217 |

## Rejection reasons

| Reason | Fields |
|---|---|
| quote not found in source | 711 |
| quote does not support the asset-test value | 576 |
| value not in quote | 411 |
| source text missing | 179 |
| quote does not say whether insured patients are covered | 93 |
| quote does not state the ceiling as a percentage of the poverty level | 87 |
| quote does not say the hospital's financial assistance reaches it | 86 |
| quote states the percentage as a floor (above, over, at least), not a ceiling | 32 |
| quote does not state the free-care limit as an income limit | 18 |
| quote states the amounts-generally-billed cap, not who may apply | 11 |
| quote states the patient's share, not a discount | 4 |
| quote is about another program or a payment plan | 3 |
| quote gives the limit as the top of a later band, not the free band | 2 |
| quote states an eligibility ceiling for any assistance, not a free-care band | 2 |
| quote gives the discount as a range or a floor, not a fixed percentage | 2 |
