"""Proportions with intervals, because a bare rate hides its own sample size.

One first-timer out of twelve merges is 8%, and it means almost nothing. Twenty out of
two hundred and forty is the same 8% and settles the question. A verdict that reads the
point estimate cannot tell those apart, so nothing here reports a proportion without the
interval around it.

Wilson score rather than the normal approximation: the normal one breaks down exactly
where this data lives - at proportions near zero, where it happily returns a negative
lower bound and an interval far too narrow to be honest.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 95%. Two-sided.
Z = 1.959963984540054


@dataclass(frozen=True)
class Proportion:
    """`successes` out of `total`, with a Wilson score interval."""

    successes: int
    total: int
    lower: float
    upper: float

    @property
    def point(self) -> float:
        return self.successes / self.total if self.total else 0.0

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def undetermined(self) -> bool:
        """The interval is too wide to support any conclusion. 20 points is the line:
        an interval of 2%-30% tells you the repository might be closed or wide open."""
        return self.total == 0 or self.width > 0.20

    def describe(self) -> str:
        if not self.total:
            return "no sample"
        return (
            f"{self.point:.1%} ({self.successes}/{self.total}), "
            f"95% CI {self.lower:.1%}-{self.upper:.1%}"
        )


def wilson(successes: int, total: int, z: float = Z) -> Proportion:
    """Wilson score interval for a binomial proportion.

    The property that matters here: zero successes does not produce a zero-width
    interval. Zero out of a hundred gives an upper bound near 3.6% - not enough to
    convict. Zero out of a thousand gives 0.4% - enough. The sample size does the
    arguing, which is what the old absolute-count threshold could never do.
    """
    if total <= 0:
        return Proportion(successes, 0, 0.0, 1.0)

    phat = successes / total
    denominator = 1 + z**2 / total
    centre = (phat + z**2 / (2 * total)) / denominator
    margin = (
        z / denominator * math.sqrt(phat * (1 - phat) / total + z**2 / (4 * total**2))
    )
    return Proportion(
        successes=successes,
        total=total,
        lower=max(0.0, centre - margin),
        upper=min(1.0, centre + margin),
    )
