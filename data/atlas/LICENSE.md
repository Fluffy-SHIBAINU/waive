# Waive atlas data — CC BY 4.0

The files in this folder (`*.json`, one per state) and the JSON served at `/atlas/{ccn}.json` are
Waive's open atlas of hospital financial-assistance procedure sheets. They are licensed under the
Creative Commons Attribution 4.0 International license:

https://creativecommons.org/licenses/by/4.0/

You may copy, share and adapt the data for any purpose, including commercially, as long as you
give credit. Suggested attribution:

> Waive atlas, CC BY 4.0, [USER FILLS: repository URL]

What the data is: for every hospital, each documented field carries the value, an exact quote
from the hospital's own document, the source document's URL, its SHA-256 and the date it was
fetched; reported fields carry a count of distinct cases instead of a quote. The quoted policy
text belongs to the hospital that published it and is reproduced as a short citation.

What the data is not: legal advice, or a promise that a hospital will decide a particular way.
Policies change; check `checked_on` and the source link, and tell the hospital what you found if
they disagree with their own document.

Sources: hospital websites (via Tavily), state repositories (mass.gov), the CMS Hospital General
Information dataset (public domain) for the registry rows. The fictional demo hospital
(CCN 229999) is never exported.
