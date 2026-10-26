"""What the user picks for one forecasting run."""

from __future__ import annotations

from dataclasses import dataclass

MIN_HORIZON = 1
MAX_HORIZON = 15
DEFAULT_HORIZON = 5  # minutes


@dataclass(frozen=True)
class RunParams:
    """What the user picked for one run; stored as ``run.json["settings"]``.

    Later stories add fields (#60: inputs and lookback) with defaults.
    """

    horizon: int = DEFAULT_HORIZON

    def __post_init__(self) -> None:
        if not MIN_HORIZON <= self.horizon <= MAX_HORIZON:
            raise ValueError(
                f"horizon must be between {MIN_HORIZON} and {MAX_HORIZON} minutes, "
                f"got {self.horizon}"
            )
