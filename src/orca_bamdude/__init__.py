"""OrcaBamDude -- an OrcaSlicer plugin that sends sliced plates to a BamDude farm over its HTTP API.

The modules here are host-independent and import only the standard library, so they run and are tested outside
OrcaSlicer. The host adapter (printer agent, tab, camera relay wiring) imports ``orca`` and is loaded only where
``orca`` exists: inside OrcaSlicer, or a test's fake of it.
"""

__version__ = "0.1.0"

try:
    import orca  # noqa: F401 -- present only inside OrcaSlicer (or a test's fake)
except ImportError:
    orca = None

if orca is not None:
    from orca_bamdude import orca_adapter  # noqa: F401,E402 -- registers the plugin class
