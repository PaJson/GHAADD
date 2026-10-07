import os
import random
import sys
import time
from contextlib import nullcontext
from modules.config_manager import (
    get_config_fingerprint,
    get_destination_check_every_n_polls,
    get_polling_settings,
    get_terminal_log_settings,
    load_config,
)
from datetime import datetime
from dotenv import load_dotenv

from modules.app_info import __version__

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT", None)

# Import application modules.
from modules.cli_commands import handle_cli_command, parse_cli_args
from modules.daemon_control import ControlWatcher, reset_log_override_on_startup, reset_paused_on_startup
from modules.daemon_lock import acquire_daemon_lock, update_daemon_status
from modules.db_manager import get_next_pending_job, open_database
from modules.asset_downloader import check_folder_limits, clear_all_resolved_limit_warnings, download_release
from modules.dry_run_mode import is_dry_run, set_dry_run
from modules.lifecycle_logger import log_cycle_summary
from modules.log_files import RollingLogFile
from modules.mapping_manager import ensure_mapping_file, warn_about_missing_mapped_destinations
from modules.queue_worker import process_queue_once, run_ingest_and_queue_cycle, run_single_cycle


def _ensure_standard_streams():
    """Give a console-less run (pythonw.exe, e.g. from Task Scheduler) real stdout/stderr objects.

    Without a console both are None, which breaks the TeeStream wrapping and any write to them. Output is
    dropped (the terminal log mirrors it when switched on) and errors go to the same file as for a GUI-started
    daemon, so a crash leaves a trace.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        try:
            from modules.daemon_launcher import get_stderr_path

            sys.stderr = open(get_stderr_path(), "a", encoding="utf-8", errors="replace")
        except OSError:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")


_ensure_standard_streams()
_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr


def _configure_output_encoding():
    """Make console output UTF-8 and unable to crash on a character the stream cannot encode.

    The console handles emoji, but a redirected stream (the GUI's Start button, a service, a
    scheduled task, "> file") falls back to the Windows locale encoding (cp1252), where the
    first emoji print raised UnicodeEncodeError and killed the daemon. Output is mirrored to
    UTF-8 log files anyway, so UTF-8 here changes nothing for the console and fixes the rest.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # absent on exotic streams and under pythonw
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


class TeeStream:
    """Mirror writes to terminal and a log file stream."""

    def __init__(self, primary_stream, secondary_stream):
        self.primary_stream = primary_stream
        self.secondary_stream = secondary_stream

    def _disable_secondary_stream(self, exc):
        if self.secondary_stream is None:
            return

        failed_stream = self.secondary_stream
        self.secondary_stream = None
        try:
            failed_stream.close()
        except OSError:
            pass

        print(
            f"Logging disabled after file stream failure: {exc}",
            file=_ORIGINAL_STDERR,
        )

    def write(self, data):
        self.primary_stream.write(data)
        if self.secondary_stream is not None:
            try:
                self.secondary_stream.write(data)
                self.secondary_stream.flush()
            except (OSError, ValueError) as exc:
                self._disable_secondary_stream(exc)
        return len(data)

    def flush(self):
        self.primary_stream.flush()
        if self.secondary_stream is not None:
            try:
                self.secondary_stream.flush()
            except (OSError, ValueError) as exc:
                self._disable_secondary_stream(exc)

    def isatty(self):
        return self.primary_stream.isatty()

    @property
    def encoding(self):
        return getattr(self.primary_stream, "encoding", "utf-8")


class TerminalLog:
    """Switchable mirror of the console output to a timestamped log file.

    stdout/stderr always go through TeeStream; the file is attached on start()
    and detached on stop(), so logging can be turned on and off while the
    daemon runs (each start() opens a new file).
    """

    def __init__(self, settings, tees):
        self.settings = settings
        self._tees = tees
        self._log_file = None

    @property
    def active(self):
        return self._log_file is not None and all(tee.secondary_stream is not None for tee in self._tees)

    def start(self):
        """Open a new log file and mirror output into it; return True if logging is on."""
        if self.active:
            return True
        self.stop()

        log_dir = self.settings["directory"] or ""
        log_dir = os.path.expandvars(os.path.expanduser(log_dir))
        try:
            os.makedirs(log_dir, exist_ok=True)
            self._log_file = RollingLogFile(
                log_dir,
                max_bytes=self.settings["max_file_mb"] * 1024 * 1024,
                keep_files=self.settings["keep_files"],
            )
        except OSError as exc:
            print(f"Logging setup failed: {exc}", file=sys.stderr)
            self._log_file = None
            return False

        for tee in self._tees:
            tee.secondary_stream = self._log_file
        print(f"Logging enabled. Writing terminal output to: {self._log_file.path}")
        return True

    def stop(self):
        """Stop mirroring and close the log file (no-op when logging is off)."""
        if self._log_file is None:
            return
        log_file, self._log_file = self._log_file, None
        for tee in self._tees:
            tee.secondary_stream = None
        try:
            log_file.flush()
            log_file.close()
        except (OSError, ValueError):
            pass

    def prune(self):
        """Apply the retention limit; returns how many old files were removed."""
        return self._log_file.prune() if self._log_file is not None else 0

    def apply_override(self, override):
        """Follow a live switch: True/False force logging on/off, None returns to config.json."""
        want = self.settings["enabled"] if override is None else override
        if want and not self.active:
            if self.start():
                print("📝 Terminal logging switched on" + (" (config default)." if override is None else "."))
                self.prune()
        elif not want and self.active:
            print("📝 Terminal logging switched off" + (" (config default)." if override is None else "."))
            self.stop()


def setup_terminal_logging(config):
    """Route console output through TeeStream. The log file itself starts later, once main() knows
    it is entering a run mode (so --help and the read-only commands do not leave log files behind)."""
    tees = (TeeStream(_ORIGINAL_STDOUT, None), TeeStream(_ORIGINAL_STDERR, None))
    sys.stdout, sys.stderr = tees
    return TerminalLog(get_terminal_log_settings(config), tees)


def run_internal_smoke_tests():
    """Run internal smoke tests for baseline release-download behavior."""
    print("🚀 Starting Internal Smoke Tests...")
    
    # Test graceful failure handling with a non-existent repository.
    print("\n--- Test 1: Verifying Failure Baseline (Invalid Repo) ---")
    fail_result = download_release("github/this-repo-does-not-exist", "v99.9.9")
    print(f"Result (Expected 'SKIP' or False): {fail_result}")
    
    # Test successful download and state database updates.
    print("\n--- Test 2: Verifying Success Baseline (Official GitHub Repo) ---")
    # Use a stable official repository and tag for predictable test behavior.
    # This release includes a representative mix of assets and source archives.
    success_result = download_release("cli/cli", "v2.30.0") 
    print(f"Result (Expected True): {success_result}")
    
    # Test duplicate guarding by repeating a previously successful request.
    print("\n--- Test 3: Verifying Duplicate Guard (Re-running Success Path) ---")
    repeat_result = download_release("cli/cli", "v2.30.0")
    print(f"Result (Expected True with 'Skipping' console logs): {repeat_result}")
    
    print("\n🎉 Smoke tests complete.")


def run_polling_loop(
    interval_seconds, jitter_min_seconds, jitter_max_seconds, terminal_log=None, config_fingerprint=None, idle=False
):
    """Run processing continuously with randomized jitter between cycles.

    With `idle` (polling.enabled is false, started with --daemon) nothing is polled on a schedule: the daemon
    waits and does one cycle per "Poll now" request (--poll-now / the GUI button), then waits again.
    """
    if jitter_min_seconds > jitter_max_seconds:
        jitter_min_seconds, jitter_max_seconds = jitter_max_seconds, jitter_min_seconds

    if idle:
        print("Polling is switched off (polling.enabled is false): idle. The mailbox is polled only on Poll now (--poll-now).")
    else:
        print(
            f"Polling enabled. Base interval: {interval_seconds}s, jitter: {jitter_min_seconds}-{jitter_max_seconds}s."
        )
    print("Press Ctrl+C to stop.\n")

    destination_check_every_n_polls = get_destination_check_every_n_polls()

    # Dry-run holds no daemon lock, so it neither reads the control state nor writes the status file.
    control_enabled = not is_dry_run()

    announced_paused = False

    def log_is_active():
        return bool(terminal_log is not None and terminal_log.active)

    def apply_log_override(override):
        """Follow a live log switch, then publish whether the file really is open (the GUI shows it)."""
        if terminal_log is None:  # nothing to switch (never registered then, but keeps the types honest)
            return
        terminal_log.apply_override(override)
        if control_enabled:
            update_daemon_status(log_active=log_is_active())

    def publish_wait_state(paused, next_poll_at):
        nonlocal announced_paused
        if control_enabled:
            update_daemon_status(paused=paused, next_poll_at=next_poll_at, log_active=log_is_active())
        if paused != announced_paused:
            announced_paused = paused
            if paused:
                print("⏸️  Polling paused (countdown frozen). Use --resume to continue.")
            else:
                print("▶️  Polling resumed.")

    def run_folder_checks(reason: str) -> None:
        """Destinations still there, folder counts and limit warnings (never stops the daemon)."""
        print(f"🔎 Folder check ({reason}): verifying mapped destinations and folder limits...")
        try:
            if warn_about_missing_mapped_destinations() == 0:
                print("   ✅ All mapped destinations are present.")
        except Exception as exc:
            print(f"   ⚠️ Could not verify the mapped destinations: {exc}")
        try:
            if check_folder_limits() == 0:
                print("   ✅ No new folder-limit warnings.")
        except Exception as exc:
            print(f"   ⚠️ Could not check folder limits: {exc}")

    cycle = 1
    with open_database() as connection:
        if control_enabled:
            reset_paused_on_startup(connection)
            reset_log_override_on_startup(connection)
            update_daemon_status(
                paused=False, next_poll_at=None, last_forced_poll_handled=None, current_job=None,
                config_fingerprint=config_fingerprint, log_active=log_is_active(), polling_idle=bool(idle),
            )
        watcher = ControlWatcher(
            connection=connection,
            enabled=control_enabled,
            on_log_override=apply_log_override if terminal_log is not None else None,
            on_check_folders=lambda: run_folder_checks("requested"),
        )
        if destination_check_every_n_polls > 0:
            run_folder_checks("at start")  # counts and warnings are there right away, not after N polls

        def announce_requested_poll(wait_result):
            """Say which poll was asked for; and note that a pause goes on afterwards."""
            what = "Single poll requested (one notification, one queue item)" if wait_result == "single" else "Poll now requested"
            print(f"⏩ {what}." + (" Polling stays paused afterwards." if watcher.forced_while_paused else ""))
            if control_enabled:
                update_daemon_status(last_forced_poll_handled=watcher.last_handled_request)

        def wait_for_poll_now():
            """Idle mode: wait (no countdown) until Poll now / Single poll or Stop; the request, or None on Stop."""
            wait_result = watcher.wait(0, on_change=publish_wait_state, idle=True)
            if wait_result == "stop":
                print("⏹️  Stop requested; exiting.")
                return None
            announce_requested_poll(wait_result)
            return wait_result

        single_next = False  # the coming cycle was asked for as a Single poll
        if idle:
            requested = wait_for_poll_now()
            if requested is None:
                return
            single_next = requested == "single"

        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== {'Single poll' if single_next else 'Poll cycle'} {cycle} @ {started} ===")
            watcher.begin_cycle()
            if single_next:
                run_single_cycle(connection, GITHUB_TOKEN)  # at most one notification and one queue item
                single_next = False
            else:
                run_ingest_and_queue_cycle(connection, GITHUB_TOKEN, should_pause=watcher.checkpoint)

            if watcher.stop_requested:
                print("⏹️  Stop requested; exiting after the work in progress.")
                return

            try:
                clear_all_resolved_limit_warnings()  # a cleaned-up folder clears its own warnings
            except Exception as exc:  # housekeeping must never stop the daemon
                print(f"   ⚠️ Could not re-check folder-limit warnings: {exc}")

            if destination_check_every_n_polls > 0 and cycle % destination_check_every_n_polls == 0:
                run_folder_checks(f"every {destination_check_every_n_polls} polls")

            if idle and not watcher.cycle_interrupted:
                print("Poll finished. Idle until the next Poll now.\n")
                requested = wait_for_poll_now()
                if requested is None:
                    return
                single_next = requested == "single"
                cycle += 1
                continue
            if watcher.cycle_interrupted:
                # Work was left over: no countdown, poll again as soon as polling resumes.
                sleep_seconds = 0
                print("Cycle interrupted by pause; polling again as soon as it is resumed.\n")
            else:
                jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
                sleep_seconds = interval_seconds + jitter
                next_poll_at = datetime.fromtimestamp(time.time() + sleep_seconds).strftime("%Y-%m-%d %H:%M:%S")
                print(
                    f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter) @ {next_poll_at}.\n"
                )
            wait_result = watcher.wait(sleep_seconds, on_change=publish_wait_state)
            if wait_result == "stop":
                print("⏹️  Stop requested; exiting.")
                return
            if wait_result in ("forced", "single"):
                announce_requested_poll(wait_result)
                single_next = wait_result == "single"
            cycle += 1


def run_drain_queue_loop(github_token):
    """Process only due queue jobs (no email ingestion) until the queue is fully empty."""
    print("Drain-queue mode enabled. Email ingestion is skipped.\n")

    cycle = 1
    with open_database() as connection:
        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== Drain cycle {cycle} @ {started} ===")
            queue_stats = process_queue_once(connection, github_token)
            summary_message = log_cycle_summary(None, queue_stats)
            print(f"📋 {summary_message}")

            next_pending_job = get_next_pending_job(connection)
            if next_pending_job is None:
                print("\nQueue is empty. All pending jobs are completed/failed. Exiting.")
                return

            next_check_time = float(next_pending_job["next_check_time"])
            next_check_time_readable = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_check_time))
            sleep_seconds = max(0.0, next_check_time - time.time())
            print(
                f"Next pending job: {next_pending_job['repo']} {next_pending_job['tag']} "
                f"@ {next_check_time_readable} (sleeping {int(sleep_seconds)}s)\n"
            )
            time.sleep(sleep_seconds)
            cycle += 1


def main():
    """Run the main orchestration flow for ingest and queue processing."""
    _configure_output_encoding()
    if "--json" not in sys.argv[1:]:  # keep stdout pure JSON for --doctor/--queue-status/--perf-report --json
        print(f"GHAADD {__version__} is starting...")
    config = load_config()
    terminal_log = setup_terminal_logging(config)
    try:
        parsed_args = parse_cli_args(sys.argv[1:], __version__)
        set_dry_run(parsed_args.dry_run)
        if parsed_args.dry_run:
            print("🧪 Dry-run mode enabled: no writes/deletes will be performed.\n")

        if handle_cli_command(parsed_args, run_internal_smoke_tests):
            return

        # Past the one-shot commands: this is a real run (daemon, --once, --single, --drain-queue), so
        # mirror the output to a log file when terminal_log.enabled says so.
        if terminal_log.settings["enabled"]:
            terminal_log.start()

        polling_settings = get_polling_settings(config)

        # Only mutating run modes contend for the daemon lock; dry-run has no
        # real side effects, so it behaves like a read-only command and never
        # blocks on (or is blocked by) another running instance.
        lock_context = nullcontext() if parsed_args.dry_run else acquire_daemon_lock()
        with lock_context:
            if ensure_mapping_file():
                print("Created an empty mapping.json (fresh install).")

            if parsed_args.drain_queue:
                try:
                    run_drain_queue_loop(GITHUB_TOKEN)
                except KeyboardInterrupt:
                    print("\nDrain-queue mode stopped by user.")
                return

            if parsed_args.single:
                with open_database() as connection:
                    run_single_cycle(connection, GITHUB_TOKEN)
                return

            # Force single-run mode with --once, even when polling is enabled in config.
            once_mode = parsed_args.once
            poll_enabled = not once_mode and (
                parsed_args.poll
                or parsed_args.daemon
                or polling_settings["enabled"]
            )
            # --daemon (what the GUI, the shortcut and the start-at-login entries run) honours a switched-off
            # polling.enabled by staying idle; --poll still forces polling like it always did.
            start_idle = bool(parsed_args.daemon and not parsed_args.poll and not polling_settings["enabled"])
            if once_mode or not poll_enabled:
                with open_database() as connection:
                    run_ingest_and_queue_cycle(connection, GITHUB_TOKEN)
                return

            interval_seconds = polling_settings["interval_seconds"]
            jitter_min_seconds = polling_settings["jitter_min_seconds"]
            jitter_max_seconds = polling_settings["jitter_max_seconds"]

            if terminal_log.active:
                removed_logs = terminal_log.prune()
                if removed_logs:
                    print(f"Removed {removed_logs} old log file(s) (terminal_log.keep_files).")

            try:
                run_polling_loop(
                    interval_seconds, jitter_min_seconds, jitter_max_seconds, terminal_log,
                    config_fingerprint=get_config_fingerprint(config), idle=start_idle,
                )
            except KeyboardInterrupt:
                print("\nPolling stopped by user.")
    finally:
        sys.stdout = _ORIGINAL_STDOUT
        sys.stderr = _ORIGINAL_STDERR
        terminal_log.stop()

if __name__ == "__main__":
    main()
