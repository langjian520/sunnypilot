# sunnyconf compatibility shim.
#
# In sunnypilot <= 2026.002 this data lived at openpilot/system/hardware/hw.py; it moved to
# openpilot/common/hardware/hw.py with the new openpilot/ layout. The sunnyconf daemon is an external
# submodule, so we cannot patch its imports there without forking it (its files are replaced on every
# `git submodule update`). Re-export from here instead: these files live in this fork and survive updates.
from openpilot.common.hardware.hw import Paths  # noqa: F401
