"""The hospital ledger (Phase 10): free federal files joined to the atlas by CCN.

Three metrics per hospital, each with its source and vintage: the stated-versus-realised
free-care gap, senior exposure, and collection aggressiveness. Tables in `tables`, Pydantic
views in `models`, database access in `repo`, cached downloads in `download`; the loaders
(`crosswalk`, `hcris`, `irs990`, `exposure`), the computation (`metrics`, `explain`, `view`),
the CLI (`cli`) and the public pages (`web`) build on those. Public data only: no model calls,
no Tavily credits; downloads land under `var/ledger/` and never in `src/`."""
