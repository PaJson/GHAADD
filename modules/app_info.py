"""Single source for the application name and version (used by main.py and the GUI)."""

APP_NAME = "GHAADD"
__version__ = "2.0-RC9.1"
# Version of the mapping.json key names (2 = repository/folder/.../active). A daemon publishes the format it
# understands in its status file, so a newer GUI/CLI does not upgrade mapping.json underneath an older daemon.
MAPPING_FORMAT = 2
