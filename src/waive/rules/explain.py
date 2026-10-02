"""Plain-language messages for seniors and caregivers (spec sections 9 and 11)."""

from waive.rules.eligibility import EligibilityResult, Tier

DISCLAIMER = "This is an estimate. The hospital makes the final decision."


def senior_message(result: EligibilityResult, hospital_name: str) -> str:
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
    if result.tier is Tier.NOT_ELIGIBLE:
        return (
            f"This bill likely does not qualify under {hospital_name}'s rules. "
            "Your helper can look at other options with you."
        )
    if result.tier is Tier.NEEDS_INFO:
        return "We need one more thing to check this bill. Your helper will ask you."
    return f"We are still learning {hospital_name}'s rules. We will tell you soon."


def caregiver_summary(result: EligibilityResult, hospital_name: str) -> str:
    headline = {
        Tier.FREE: "Likely free care",
        Tier.DISCOUNT: f"Likely {result.discount_percent}% discount",
        Tier.NOT_ELIGIBLE: "Likely not eligible",
        Tier.NEEDS_INFO: "More information needed",
        Tier.UNKNOWN: "Policy not yet known",
    }[result.tier]
    lines = [f"{headline} at {hospital_name}."]
    for reason in result.reasons:
        lines.append(reason.text)
        if reason.quote:
            lines.append(f'Policy says: "{reason.quote}"')
    if result.missing:
        lines.append("Still needed: " + ", ".join(result.missing) + ".")
    lines.extend(result.warnings)
    lines.append(DISCLAIMER)
    return "\n".join(lines)
