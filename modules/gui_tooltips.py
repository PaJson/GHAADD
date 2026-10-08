"""Every hover tooltip the GUI shows, in one place.

Edit the texts here and restart the GUI (`python main_gui.py`) to see how they fit. Nothing else needs
to change: the widgets in main_gui.py look the texts up by the keys below. A "\n" starts a new line, long
lines wrap on their own (the hover box is at most about 640 pixels wide).

Not in this file: the status icons themselves (main_gui.STATUS_ICONS), and the hover text that shows the
full content of a table cell that was cut off with "..." (that is the cell's own text).
"""

# --- Mappings tab: the editor's field titles. Keys match MappingsTab.FORM_LAYOUT, plus the buttons below.
FIELD_HELP = {
    "name": "The repository on GitHub (owner/repo). It identifies this entry and cannot be changed here. "
            "For a renamed repository, add the new name and remove the old entry.",
    "github": "Open this repository's page on GitHub in your web browser.",
    "folder": "Name of this repository's folder inside the destination (mapping.json: folder). "
                  "Empty = the default, \"repo (owner)\".",
    "destination": "The folder that holds this repository's folder. Finished downloads go to "
                   "<destination>/<folder name>[/<subfolder>]. Empty = the default download folder from Settings.",
    "subfolder": "Optional folders below the folder name, separated by / or \\ (for example Nightly/x64). "
                 "Downloads go there instead of straight into the folder name.",
    "release_folders": "The release types whose folders count towards the Limit, comma separated "
                       "(for example Release, Pre-release; mapping.json: limit_release_type_folders). "
                       "Empty = detect Release and Pre-release automatically. "
                       "It does not change what is downloaded; use Skiplist for that.",
    "limit": "Warn (Folder limits tab) when more than this many release folders exist for this repository. "
             "0 = no warning. The warning never deletes anything.",
    "recheck": "Minutes to wait between re-checks of a new release, comma separated (for example 3, 10, 30, 120). "
               "Empty = use the default shown next to it.",
    "default_recheck": "The re-check schedule used when Recheck is empty (set in Settings).",
    "skiplist": "Release types to ignore for this repository, comma separated: Release, Pre-release. "
                "A matching notification is logged as a warning and nothing is downloaded. Empty = download both.",
    "sanity_check": "Warns (Warnings tab) when a finished release has a different number of files than the previous "
                    "one.\n"
                    "Compare with previous release: the previous successful release of this repository, whatever its "
                    "tag (the default).\n"
                    "Compare same tag only: only a release with the same tag. Only useful for a repository that "
                    "re-uses tags (a rolling \"nightly\" next to versioned releases, or one re-published tag per "
                    "platform). If every release has a new tag, it never finds anything to compare with and so "
                    "never warns.\n"
                    "Off: never warn.\n"
                    "Each repository is compared only with its own earlier releases, so several repositories using "
                    "the same tag do not affect each other.",
    "last_seen": "When the last GitHub notification for this repository arrived (set by the daemon).",
    "last_finalized": "When a release was last moved to its destination (set by the daemon).",
    "last_filecount": "Number of files in the newest release (what the Sanity check compares). "
                      "- = not known yet.",
    "shared_destination": "Tick this when this repository shares its destination folder with other repositories on "
                          "purpose (for example one repository per platform that all go to the same folder). "
                          "The shared-folder notice in the Doctor and --mapping-validate is then not shown for it "
                          "(it is hidden once every repository of the group is ticked). mapping.json: shared_destination.",
    "active": "Switched on: new releases of this repository are downloaded.\n"
              "Switched off: new notifications are ignored and their emails stay unread in the mailbox; jobs "
              "already queued still finish (mapping.json: active).",
    "open_folder": "Open this repository's folder in the file manager: destination + folder name + subfolder. "
                   "If that folder does not exist yet, the closest existing parent opens. "
                   "Double-clicking a row in the table does the same.",
}

# --- Mappings tab: the table's column headings. Keys match MappingsTab.TABLE_COLUMNS ("icon" is the legend below).
COLUMN_HELP = {
    "folder": "The repository's folder name (from mapping.json). Empty = the default, \"repo (owner)\".",
    "repo": "The GitHub repository (owner/repo).",
    "destination": "The folder that holds this repository's folder (the destination in mapping.json).",
    "tag": "Tag of the newest release for this repository.\n(R) = Release, (P) = Pre-release.",
    "last_check": "When this repository's newest release was last worked on (a check or a download).",
    "step": "Re-check step of a new release, e.g. 2 / 5: the release is checked again at the intervals from "
            "Settings (or this repository's own) in case files are added later.\n"
            "\"(+2)\" = two more jobs of this repository are waiting (for example older builds that are still being "
            "re-checked); hover the cell to list them. The step and the next check shown are the first one's.\n"
            "- = nothing is waiting.",
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
    "Inactive": "This repository is switched off (active is unchecked); new notifications are ignored.",
    "Failed": "The last job failed.",
}
STATUS_LEGEND_TITLE = "Status"

# --- Buttons and small controls.
CONTROL_HELP = {
    "queue_counts": "Jobs waiting in the queue.\n"
                    "Due now: their check time has come, so the next poll (or Poll now) processes them. This is the "
                    "number the daemon prints as \"Processing N due queue job(s)\".\n"
                    "Due later: re-checks of recent releases whose time has not come yet.",
    "choose_warnings": "Choose which kinds of warning may pop up a notification. The Warnings tab and its unread "
                       "counter still show every warning. By default API (a release that was replaced or removed) "
                       "and LIMIT (which has its own notification) are silent. The checklist has its own Save and Cancel: "
                       "Save applies at once, whatever you do with this window's Save.",
    "poll_now": "Poll the mailbox now and process everything that is due, then restart the countdown.\n"
                "Also works while paused (one poll, then it stays paused) and while polling is switched off.",
    "poll_one": "Poll one item: take at most one new notification and process at most one queue item "
                "(the same as --single, but in the running daemon), then restart the countdown.\n"
                "Handy to try something out without working through everything that is waiting.",
    "polling_enabled": "Poll the mailbox on a schedule (config.json: polling.enabled).\n"
                       "Off: the daemon (started from the GUI, the \"GHAADD daemon\" shortcut or at login) still runs, "
                       "but stays idle and polls only when you press Poll now.\n"
                       "On the command line, \"python main.py --poll\" always polls, and a plain \"python main.py\" "
                       "does a single run and exits when this is off. Applies the next time the daemon starts.",
    "tray": "Show a GHAADD icon in the system tray (the notification area). Its menu shows or hides the window and "
            "controls the daemon, and its colour shows the daemon's state. Needs the optional packages "
            "(pip install -r requirements-optional.txt). Applies the next time the GUI starts.",
    "notifications": "Pop up a notification for new warnings, failed jobs, a stopped daemon, new folder-limit warnings and "
                     "new unmapped repositories. Only while the window is hidden or minimized. Applies the next time the GUI starts.",
    "minimize_to_tray": "Minimizing hides the window in the tray instead of leaving it on the taskbar. "
                        "Off by default on Linux, where many desktops show no tray icon. Applies the next time the GUI starts.",
    "close_to_tray": "The window's close button hides it in the tray instead of ending the GUI. "
                     "Use the tray menu's Quit to close it for real. Applies the next time the GUI starts.",
    "start_minimized": "Open GHAADD minimized instead of showing the window, the way the minimize button leaves it: "
                       "hidden in the tray when \"Show the tray icon\" and \"Minimize to the tray\" are on, "
                       "otherwise on the taskbar. Applies the next time the GUI starts.",
    "start_daemon": "When the GUI starts and no daemon is running, start it (the same as the Start button).\n"
                    "A daemon that is already running is left alone, and the GUI never stops it on close.\n"
                    "Applies the next time the GUI starts.",
    "open_config": "Open config.json in your default editor, for the settings this window has no fields for\n"
                   "(the gui section: refresh time, tray, notifications ...). Missing optional settings are added first,\n"
                   "with their default values. Unsaved changes in this window are lost.",
    "create_shortcuts": "Make GHAADD shortcuts in a folder you choose.\n"
                        "Windows: \"GHAADD\" (opens the window) and \"GHAADD daemon\" (starts the daemon in the background);\n"
                        "the first one makes Windows name the notifications GHAADD. The Start menu folder is suggested.\n"
                        "The two \"Start ...\" boxes below count as they are ticked now (saved or not): the GUI shortcut then "
                        "starts with --minimized and/or --start-daemon, whatever the saved settings say later.\n"
                        "Linux: an application-menu launcher (\"ghaadd.desktop\"). It happens at once, not on Save.",
    "autostart": "Start the polling daemon by itself when you log in, with no window.\n"
                 "Windows: a scheduled task that also restarts it after a failure (or the Run key if the task cannot be made).\n"
                 "Linux: a systemd user unit; it starts when you log in, or already at boot if you run "
                 "\"loginctl enable-linger $USER\" once. macOS: a launchd agent.\n"
                 "A normal Stop is respected; nothing is started until the next login.",
    "restart": "Settings changed since the daemon started.\n"
               "Click to restart it: the job in progress finishes, the daemon stops,\n"
               "then starts again with the new settings.",
    "check_folders": "Check now that every mapped destination exists, count the folders of each repository with a "
                     "limit (the \"12 / 15\" in the table) and warn about repositories over their limit. "
                     "It does not poll the mailbox.",
    "doctor": "Check that the installation is ready: the Gmail login (.env), config.json, mapping.json, "
              "path styles and that every mapped destination exists (the same checks as --doctor).",
    "stats": "Statistics: how many repositories are mapped and active, how many jobs ran per day, week, month, year and "
             "in total, the busiest and the biggest repositories and how old the history is (the same as --stats).",
    "open_repo_folder": "Open the selected repository's folder in the file manager (the same as \"Open folder\" in the "
                        "Mappings tab: destination + name + subfolder, or the nearest part of it that exists).",
    "doctor_attention": "This looks like a first run, or something needed is missing. Click to see what to set up.",
    "clear_filter": "Clear the filter (Esc)",
    "open_log": "Open the log file this tab is following in your default program for .log files "
                "(usually a text editor).",
    "clear_tab": "Permanently delete ALL events of this kind from the database, including older ones the tab "
                 "does not show (it lists the newest 500). Downloads and the job queue are not touched.",
    "clear_repo": "Delete every warning of the selected row's repository. They also clear themselves once the "
                  "folder is back under its limit (checked every poll).",
}
