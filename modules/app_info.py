"""Single source for the application name and version (used by main.py and the GUI)."""

APP_NAME = "GHAADD"
__version__ = "2.5.5"
# Version of the mapping.json key names (2 = repository/folder/.../active). A daemon publishes the format it
# understands in its status file, so a newer GUI/CLI does not upgrade mapping.json underneath an older daemon.
MAPPING_FORMAT = 2
# Windows identity of the GUI process (taskbar grouping, notification name); the GHAADD shortcut carries the same id.
APP_USER_MODEL_ID = "GHAADD.GUI"
