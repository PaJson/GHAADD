"""Every hover tooltip the GUI shows, in one place.

Edit the texts here and restart the GUI (`python main_gui.py`) to see how they fit. Nothing else needs
to change: the widgets in main_gui.py look the texts up by the keys below. A "\n" starts a new line, long
lines wrap on their own (the hover box is at most about 640 pixels wide).

Not in this file: the status icons themselves (main_gui.STATUS_ICONS), and the hover text that shows the
full content of a table cell that was cut off with "..." (that is the cell's own text).
"""

# --- Mappings tab: the editor's field titles. Keys match MappingsTab.FORM_COLUMNS, plus the buttons below.
FIELD_HELP = {
    "name": "The repository on GitHub (owner/repo). It identifies this entry and cannot be changed here. "
            "For a renamed repository, add the new name and remove the old entry.",
    "github": "Open this repository's page on GitHub in your web browser.",
    "foldername": "Name of this repository's folder inside the destination. "
                  "Empty = the default, \"repo (owner)\".",
    "destination": "The folder that holds this repository's folder. Finished downloads go to "
                   "<destination>/<folder name>[/<subfolder>]. Empty = the default download folder from Settings.",
    "subfolder": "Optional folders below the folder name, separated by / or \\ (for example Nightly/x64). "
                 "Downloads go there instead of straight into the folder name.",
    "release_folders": "The release type folders that count towards the Limit, comma separated "
                       "(for example Release, Pre-release). Empty = detect Release and Pre-release automatically. "
                       "It does not change what is downloaded; use Skiplist for that.",
    "limit": "Warn (Folder limits tab) when more than this many release folders exist for this repository. "
             "0 = no warning. The warning never deletes anything.",
    "recheck": "Minutes to wait between re-checks of a new release, comma separated (for example 3, 10, 30, 120). "
               "Empty = use the default shown below.",
    "default_recheck": "The re-check schedule used when Recheck is empty (set in Settings).",
    "skiplist": "Release types to ignore for this repository, comma separated: Release, Pre-release. "
                "A matching notification is logged as a warning and nothing is downloaded. Empty = download both.",
    "last_seen": "When the last GitHub notification for this repository arrived (set by the daemon).",
    "last_finalized": "When a release was last moved to its destination (set by the daemon).",
    "paused": "Temporarily stop new downloads for this repository. Notification emails stay unread in the "
              "mailbox; jobs already queued still finish.",
    "open_folder": "Open this repository's folder in the file manager: destination + folder name + subfolder. "
                   "If that folder does not exist yet, the closest existing parent opens. "
                   "Double-clicking a row in the table does the same.",
}

# --- Mappings tab: the table's column headings. Keys match MappingsTab.TABLE_COLUMNS ("icon" is the legend below).
COLUMN_HELP = {
    "foldername": "The repository's folder name (from mapping.json). Empty = the default, \"repo (owner)\".",
    "repo": "The GitHub repository (owner/repo).",
    "destination": "The folder that holds this repository's folder (the destination in mapping.json).",
    "tag": "Tag of the newest release for this repository.\n(R) = a Release, (P) = a Pre-release.",
    "last_check": "When this repository's newest release was last worked on (a check or a download).",
    "step": "Re-check step of a new release, e.g. 2 / 5: the release is checked again at the intervals from "
            "Settings (or this repository's own) in case files are added later.\n- = nothing is waiting.",
    "next_check": "When the next re-check is due.\n- = nothing is waiting.",
    "files": "Files held for the newest release: downloaded plus already present.\n"
             "\"8 / 16\" while some are still missing.",
    "limit": "Release folders counted / the folder limit, e.g. 12 / 15.\n"
             "A warning sign and amber text mean it is over the limit or a warning is on record.\n"
             "Just the limit until the daemon has counted (press Check folders). Empty or 0 = no limit.",
}

# The Limit cell's own hover text once the daemon has counted. {count}, {allowed} and {when} are filled in.
LIMIT_NOTE = "{count} of {allowed} allowed folders (counted {when})"

# --- Mappings tab: the status icon in the first column (hover a cell) and its "?" heading (the legend).
# Keys match main_gui.STATUS_ICONS.
STATUS_HINTS = {
    "Running": "A job is being processed now.",
    "Queued": "A check is due and waiting its turn.",
    "Waiting": "The next recheck is scheduled for later.",
    "Idle": "Nothing pending.",
    "Paused": "This repository is paused in mapping.json.",
    "Failed": "The last job failed.",
}
STATUS_LEGEND_TITLE = "Status"

# --- Buttons and small controls.
CONTROL_HELP = {
    "restart": "Settings changed since the daemon started.\n"
               "Click to restart it: the job in progress finishes, the daemon stops,\n"
               "then starts again with the new settings.",
    "check_folders": "Check now that every mapped destination exists, count the folders of each repository with a "
                     "limit (the \"12 / 15\" in the table) and warn about repositories over their limit. "
                     "It does not poll the mailbox.",
    "clear_filter": "Clear the filter (Esc)",
    "clear_tab": "Permanently delete the events this tab lists from the database.",
    "clear_repo": "Delete every warning of the selected row's repository. They also clear themselves once the "
                  "folder is back under its limit (checked every poll).",
}
