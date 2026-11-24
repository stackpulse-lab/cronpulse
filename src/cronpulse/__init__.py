"""CronPulse — inspect cron expressions without guesswork.

Zero dependencies, standard library only.
"""

__version__ = "0.2.0"

from .expr import CronError, CronExpr, ScanResult, parse  # noqa: F401

__all__ = ["CronError", "CronExpr", "ScanResult", "parse", "__version__"]
