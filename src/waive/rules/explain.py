"""Plain-language messages for seniors and caregivers (spec sections 9 and 11)."""

from waive.rules.eligibility import EligibilityResult, Tier

DISCLAIMER = "This is an estimate. The hospital makes the final decision."
SHIP_PHONE = "1-877-839-2675"


def senior_message(
    result: EligibilityResult,
    hospital_name: str,
    *,
    alone: bool = False,
    card: str = "Medicaid",
    payee: str | None = None,
    route: str = "payee_request",
) -> str:
    """`alone`: the reader holds the full link, so there is no helper to send them to; what
    comes next is on their own next page. `card` is the state's Medicaid name (NOT_OWED);
    `payee` and `route` say who sent a bill the policy does not cover (NOT_COVERED)."""
    if result.tier is Tier.NOT_OWED:
        return (
            "This bill is likely not yours to pay. "
            f"People with a {card} or QMB card cannot be charged this by law. "
            "We make a letter that tells the hospital to cancel it."
        )
    if result.tier is Tier.NOT_COVERED:
        # "Example Emergency Physicians, P.A." + "." would print "P.A..".
        who = (payee or "another business").rstrip(".")
        if route == "ship":
            return (
                f"{hospital_name}'s policy does not cover this bill. "
                f"It comes from an ambulance service, {who}. "
                f"Call SHIP, the free Medicare help line, at {SHIP_PHONE}. "
                "They can appeal it through Medicare."
            )
        return (
            f"{hospital_name}'s policy does not cover this bill. "
            f"It comes from {who}. "
            "Ask them for their own financial assistance. "
            "We make that letter for you."
        )
    if result.tier is Tier.FREE:
        return (
            "Good news. You likely do not have to pay this bill. "
            f"{hospital_name} gives free care to people like you. "
            "We can fill in the form for you."
        )
    if result.tier is Tier.DISCOUNT:
        return (
            f"Good news. You likely get {result.discount_percent}% off this bill. "
            f"{hospital_name} gives discounts to people like you. "
            "We can fill in the form for you."
        )
    if result.tier is Tier.ASSISTANCE:
        # The policy states who can get help, not how much: never promise free care here.
        return (
            "Good news. You can likely get help with this bill. "
            f"{hospital_name} decides how much help you get. "
            "We can fill in the form for you."
        )
    if result.tier is Tier.NOT_ELIGIBLE:
        follow_up = (
            "The next page shows what we read."
            if alone
            else "Your helper can look at other options with you."
        )
        return f"This bill likely does not qualify under {hospital_name}'s rules. {follow_up}"
    if result.tier is Tier.NEEDS_INFO:
        follow_up = "You can add it on the next page." if alone else "Your helper will ask you."
        return f"We need one more thing to check this bill. {follow_up}"
    return f"We are still learning {hospital_name}'s rules. We will tell you soon."


def caregiver_summary(result: EligibilityResult, hospital_name: str) -> str:
    headline = {
        Tier.FREE: "Likely free care",
        Tier.DISCOUNT: f"Likely {result.discount_percent}% discount",
        Tier.ASSISTANCE: "Likely eligible for assistance",
        Tier.NOT_OWED: "Likely not owed at all (QMB or Medicaid cost-sharing protection)",
        Tier.NOT_COVERED: f"Not covered by {hospital_name}'s policy",
        Tier.NOT_ELIGIBLE: "Likely not eligible",
        Tier.NEEDS_INFO: "More information needed",
        Tier.UNKNOWN: "Policy not yet known",
    }[result.tier]
    lines = [
        f"{headline}." if result.tier is Tier.NOT_COVERED else f"{headline} at {hospital_name}."
    ]
    for reason in result.reasons:
        lines.append(reason.text)
        if reason.quote:
            lines.append(f'Policy says: "{reason.quote}"')
    if result.missing:
        lines.append("Still needed: " + ", ".join(result.missing) + ".")
    lines.extend(result.warnings)
    lines.append(DISCLAIMER)
    return "\n".join(lines)
