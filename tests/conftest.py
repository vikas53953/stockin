"""Hypothesis profiles. `ci` (the default) runs 200 examples per property;
`HYPOTHESIS_PROFILE=deep` runs 2,000 for a slower local sweep."""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("deep", parent=settings.get_profile("ci"), max_examples=2000)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
