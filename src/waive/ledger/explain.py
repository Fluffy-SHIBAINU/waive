"""The ledger in words: every headline, sentence, short line and piece of advice a senior reads
on the atlas page, in the JSON key and on the result screen.

Pure functions over plain values, so the view can call them with whatever it has and the
tests can pin each sentence verbatim. A number that is missing drops its clause rather than
printing a blank or a placeholder; a rank becomes a fraction of hospitals, never a decimal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from waive.ledger.models import ExposureLabel, GapLabel

NOT_ENOUGH = "Not enough data yet"

# The words for what the page shows when a metric cannot be computed, keyed by the input.
MISSING = {
    "stated": "no free-care limit on file (no published policy sheet and no matched tax filing)",
    "cost_report": "no cost report on file",
    "schedule_h": "no tax filing matched to this hospital",
    "inpatient": "CMS does not publish this hospital's Medicare inpatient counts (fewer than 11 cases)",
    "service_area": "no ZIP code with 11 or more of its Medicare cases",
    # The cost report is on file but the line is blank: 1,868 of 6,109 real 2024 rows leave the
    # charity-care cell empty, so this is a common state and not the same as no report.
    "charity_cell": "its cost report leaves the charity-care line blank",
    "bad_debt_cell": "its cost report leaves the bad-debt line blank",
}

# (rank floor, fraction) from the top down; the first floor the rank reaches names it.
_ABOVE = ((0.9, "9 in 10"), (0.8, "4 in 5"), (0.75, "3 in 4"), (0.67, "2 in 3"), (0.55, "half of"))
_BELOW = ((0.33, "half of"), (0.25, "2 in 3"), (0.2, "3 in 4"), (0.1, "4 in 5"))


def _below_fraction(rank: float) -> str:
    for floor, words in _BELOW:
        if rank >= floor:
            return words
    return "9 in 10"


def rank_phrase(rank: float | None, noun: str = "nonprofit hospitals") -> str:
    """A percentile as a senior reads it: "above 4 in 5 nonprofit hospitals"."""
    if rank is None:
        return ""
    for floor, words in _ABOVE:
        if rank >= floor:
            return f"above {words} {noun}"
    if rank > 0.45:
        return f"about the middle of {noun}"
    return f"below {_below_fraction(rank)} {noun}"


def _number(value: float) -> str:
    """One decimal, with a whole number shown whole: 0.9, 2.6, 3."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def percent(value: float) -> str:
    return f"{_number(value)}%"


def fpl(value: float) -> str:
    return f"{_number(value)}% of the poverty level"


def month_year(d: date) -> str:
    return d.strftime("%B %Y")


def count(n: float) -> str:
    """A count rounded the way a person would say it: to the nearest 10 above a thousand and
    the nearest 100 above ten thousand, with thousands separators."""
    step = 100 if n > 10_000 else 10 if n > 1_000 else 1
    return f"{int(round(n / step) * step):,}"


def _join(parts: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# Metric 1: the gap between the free care promised and the free care given.


def gap_headline(
    label: GapLabel | None, stated_fpl: float | None, charity_cost_pct: float | None
) -> str:
    if label is GapLabel.PROMISES_MORE:
        return "Promises more than it delivers"
    if label is GapLabel.DELIVERS:
        return "Delivers what it promises"
    if label is GapLabel.PROMISES_LESS:
        return "Promises less, delivers more"
    if stated_fpl is not None:
        return f"Free care promised up to {fpl(stated_fpl)}"
    if charity_cost_pct is not None:
        return f"Spent {percent(charity_cost_pct)} of its costs on free care"
    return NOT_ENOUGH


def gap_sentence(
    stated_fpl: float | None,
    stated_rank: float | None,
    charity_cost_pct: float | None,
    charity_rank: float | None,
    fy_end: date | None,
    charity_pct_990: float | None,
) -> str:
    sentences: list[str] = []
    if stated_fpl is not None:
        words = f"Says free care goes up to {fpl(stated_fpl)}"
        if stated_rank is not None:
            words += f", {rank_phrase(stated_rank)}"
        sentences.append(words + ".")
    if charity_cost_pct is not None:
        words = f"Spent {percent(charity_cost_pct)} of its costs on free care"
        if fy_end is not None:
            words += f" in the year to {month_year(fy_end)}"
        if charity_rank is not None:
            words += f", {rank_phrase(charity_rank)}"
        sentences.append(words + ".")
        if charity_pct_990 is not None:
            sentences.append(
                f"Its tax filing puts financial assistance at {percent(charity_pct_990)} "
                "of expenses."
            )
    elif charity_pct_990 is not None:
        sentences.append(
            f"Its tax filing puts financial assistance at {percent(charity_pct_990)} of expenses."
        )
    return " ".join(sentences)


def gap_short(
    label: GapLabel | None,
    stated_fpl: float | None,
    charity_cost_pct: float | None,
    charity_rank: float | None,
) -> str:
    """One line for the result screen. A "promises more" hospital whose charity rank is still
    above the middle is not told it gives less than half of hospitals do; that would be false."""
    if label is None or stated_fpl is None:
        if stated_fpl is not None:
            return f"Free care is promised up to {fpl(stated_fpl)}."
        if charity_cost_pct is not None:
            return f"This hospital spent {percent(charity_cost_pct)} of its costs on free care."
        return ""
    promise = f"Free care is promised up to {fpl(stated_fpl)}"
    if label is GapLabel.PROMISES_MORE:
        if charity_rank is not None and charity_rank <= 0.45:
            return f"{promise}, but given less often than at {_below_fraction(charity_rank)} hospitals."
        return f"{promise}, but given less often than that promise suggests."
    if label is GapLabel.PROMISES_LESS:
        return f"{promise} and given more often than at most hospitals."
    return f"{promise} and given about as often as elsewhere."


def gap_advice(label: GapLabel | None) -> str:
    if label is GapLabel.PROMISES_MORE:
        return (
            "Apply in writing, keep a copy, and send every document the list asks for the "
            "first time."
        )
    if label is GapLabel.DELIVERS:
        return "Apply as the policy says; most people who qualify get it."
    if label is GapLabel.PROMISES_LESS:
        return (
            "Ask even if the policy looks narrow; this hospital gives more free care than it "
            "advertises."
        )
    return "Apply in writing and keep a copy."


# Metric 2: how many of the hospital's patients are poor seniors.


def exposure_headline(label: ExposureLabel | None) -> str:
    if label is None:
        return NOT_ENOUGH
    return label.value.capitalize()


def exposure_sentence(
    discharges: int | None,
    dual_discharges: float | None,
    benes: int | None,
    dual_benes: int | None,
    poor_seniors_served: float | None,
    year: str | None,
) -> str:
    sentences: list[str] = []
    when = f" in {year}" if year else ""
    if discharges is not None and dual_discharges is not None:
        sentences.append(
            f"About {count(dual_discharges)} of its {count(discharges)} Medicare inpatients"
            f"{when} were also on Medicaid."
        )
    elif discharges is not None:
        sentences.append(f"It had about {count(discharges)} Medicare inpatients{when}.")
    elif benes and dual_benes is not None:
        sentences.append(exposure_short(dual_benes, benes).replace(" are ", " were ", 1))
    if poor_seniors_served is not None:
        sentences.append(
            f"About {count(poor_seniors_served)} people 65 or over living under twice the "
            "poverty line are in the ZIP codes it serves."
        )
    return " ".join(sentences)


def exposure_short(dual_benes: int | None, benes: int | None) -> str:
    if dual_benes is None or not benes:
        return ""
    share = dual_benes / benes
    if share < 0.05:
        return "Fewer than 1 in 10 of its Medicare patients are also on Medicaid."
    if share >= 0.95:
        return "Nearly all of its Medicare patients are also on Medicaid."
    return f"About {max(1, round(share * 10))} in 10 of its Medicare patients are also on Medicaid."


def exposure_advice() -> str:
    return (
        "If you have both Medicare and Medicaid, you cannot be billed for this visit. Waive "
        "writes that letter instead of an application."
    )


# Metric 3: what the hospital does to patients before checking whether they qualify.


@dataclass(frozen=True)
class _Action:
    """One collection flag's words: naming patients, with a pronoun once patients have been
    named, in the past tense for what the filing reported, for the headline, and for the
    result screen's one line."""

    first: str
    later: str
    past: str
    headline: str
    short: str


# Each flag in the order the sentences name them.
_ACTIONS = (
    ("legal_action", _Action(
        "takes legal action against patients", "takes legal action against them",
        "took legal action against them", "takes legal action", "takes legal action",
    )),
    ("lawsuits", _Action("sues patients", "sues them", "sued them", "sues patients", "sues patients")),
    ("liens", _Action(
        "puts liens on patients' homes", "puts liens on their homes", "put liens on their homes",
        "places liens", "places liens on homes",
    )),
    ("credit_reporting", _Action(
        "reports patients to credit agencies", "reports them to credit agencies",
        "reported them to credit agencies", "reports to credit agencies",
        "reports patients to credit agencies",
    )),
    ("sells_debt", _Action(
        "sells patient debt", "sells their debt", "sold their debt", "sells debt",
        "sells patient debt",
    )),
)  # fmt: skip
_PERMITTED = {
    "credit_reporting": "credit reporting",
    "selling_debt": "selling debt",
    "legal_action": "legal action",
    "other": "other actions",
}
_UNKNOWN_PRACTICES = "sues patients, places liens, reports to credit agencies or sells debt"


def _engaged(
    lawsuits: bool | None,
    liens: bool | None,
    credit_reporting: bool | None,
    sells_debt: bool | None,
    legal_action: bool | None,
) -> list[_Action]:
    """The words of every flag that is True, in sentence order."""
    flags = {
        "lawsuits": lawsuits,
        "liens": liens,
        "credit_reporting": credit_reporting,
        "sells_debt": sells_debt,
        # A pre-2016 return answers lawsuits and liens separately and the loader derives the
        # combined flag from them; naming both would say the same act twice.
        "legal_action": legal_action if lawsuits is None and liens is None else False,
    }
    return [action for name, action in _ACTIONS if flags[name]]


def _known(*flags: bool | None) -> bool:
    return any(flag is not None for flag in flags)


def action_words(
    credit_reporting: bool | None,
    sells_debt: bool | None,
    legal_action: bool | None,
    lawsuits: bool | None,
    liens: bool | None,
) -> list[str]:
    """What the hospital does, in the present tense, naming patients in the first phrase and
    "them" after that: ["takes legal action against patients", "sells their debt"]."""
    engaged = _engaged(lawsuits, liens, credit_reporting, sells_debt, legal_action)
    return [a.first if i == 0 else a.later for i, a in enumerate(engaged)]


def collections_headline(
    lawsuits: bool | None,
    liens: bool | None,
    credit_reporting: bool | None,
    sells_debt: bool | None,
    legal_action: bool | None,
    bad_debt_pct: float | None,
) -> str:
    if not _known(lawsuits, liens, credit_reporting, sells_debt, legal_action):
        if bad_debt_pct is not None:
            return f"Practices not reported; bad debt was {percent(bad_debt_pct)} of revenue"
        return "Practices not reported"
    engaged = _engaged(lawsuits, liens, credit_reporting, sells_debt, legal_action)
    if not engaged:
        return "No lawsuits, liens, credit reports or debt sales reported"
    words = ", ".join(a.headline for a in engaged)
    return words[0].upper() + words[1:]


def _bad_debt_sentence(bad_debt_pct: float | None, bad_debt_rank: float | None) -> str:
    if bad_debt_pct is None:
        return ""
    words = f"Bad debt was {percent(bad_debt_pct)} of patient revenue"
    if bad_debt_rank is not None:
        words += f", {rank_phrase(bad_debt_rank)}"
    return words + "."


def collections_sentence(
    tax_year: int | None,
    lawsuits: bool | None,
    liens: bool | None,
    credit_reporting: bool | None,
    sells_debt: bool | None,
    legal_action: bool | None,
    permitted_actions: list[str],
    bad_debt_pct: float | None,
    bad_debt_rank: float | None,
) -> str:
    sentences: list[str] = []
    engaged = _engaged(lawsuits, liens, credit_reporting, sells_debt, legal_action)
    if tax_year is None:
        sentences.append(
            "Waive could not match this hospital to a tax filing, so whether it "
            f"{_UNKNOWN_PRACTICES} is unknown."
        )
    elif not _known(lawsuits, liens, credit_reporting, sells_debt, legal_action):
        sentences.append(f"Its {tax_year} tax filing does not say whether it {_UNKNOWN_PRACTICES}.")
    elif engaged:
        sentences.append(
            f"In its {tax_year} tax filing the hospital reported that, before checking whether "
            f"patients qualified for free care, it {_join([a.past for a in engaged])}."
        )
    else:
        sentences.append(
            f"In its {tax_year} tax filing the hospital reported no lawsuits, liens, credit "
            "reporting or debt sales before checking who qualified for free care."
        )
        permitted = [
            _PERMITTED.get(a, a.replace("_", " ")) for a in permitted_actions if a != "none"
        ]
        if permitted:
            sentences.append(f"Its policy permits {_join(permitted)}.")
    if bad_debt := _bad_debt_sentence(bad_debt_pct, bad_debt_rank):
        sentences.append(bad_debt)
    return " ".join(sentences)


def collections_short(
    lawsuits: bool | None,
    liens: bool | None,
    credit_reporting: bool | None,
    sells_debt: bool | None,
    legal_action: bool | None,
) -> str:
    if not _known(lawsuits, liens, credit_reporting, sells_debt, legal_action):
        return (
            "Whether this hospital sues or reports patients before checking who qualifies is "
            "not on file."
        )
    engaged = _engaged(lawsuits, liens, credit_reporting, sells_debt, legal_action)
    if not engaged:
        return (
            "This hospital reported no lawsuits, liens or credit reporting before checking who "
            "qualifies."
        )
    return f"This hospital {_join([a.short for a in engaged])} before checking who qualifies."


def collections_advice(any_engaged: bool | None) -> str:
    if any_engaged:
        return (
            "Apply within 120 days of the first bill. Waive sets the reminders and writes the "
            "letter if a collector calls."
        )
    if any_engaged is False:
        return (
            "Apply before the first bill is 120 days old; this hospital reported no collection "
            "actions before checking eligibility."
        )
    return "Apply within 120 days of the first bill and keep a copy of everything."
