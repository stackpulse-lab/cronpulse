"""CronPulse — inspect cron expressions without guesswork.

Zero dependencies, standard library only.
"""

__version__ = "0.1.0"

from .expr import CronError, CronExpr, parse  # noqa: F401

__all__ = ["CronError", "CronExpr", "parse", "__version__"]
