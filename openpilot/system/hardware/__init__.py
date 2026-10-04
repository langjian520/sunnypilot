# sunnyconf compatibility shim — see ./hw.py for why this exists.
#
# openpilot.system.hardware.HARDWARE moved to openpilot.common.hardware.HARDWARE with the new
# openpilot/ layout. This directory had no __init__.py before (implicit namespace package); adding one
# only affects code importing this package, and everything under it remains importable as before.
from openpilot.common.hardware import HARDWARE  # noqa: F401
