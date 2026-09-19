import os
import random
import sys
import time
from contextlib import nullcontext
from config_manager import (
    get_default_download_dir,
    get_destination_check_every_n_polls,
    get_polling_settings,
    get_terminal_log_settings,
    load_config,
)
from datetime import datetime
from dotenv import load_dotenv

__version__ = "1.0-RC3"

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Import application modules.
from cli_commands import handle_cli_command, parse_cli_args
from daemon_lock import acquire_daemon_lock
from db_manager import get_next_pending_job, open_database
from asset_downloader import download_release
from dry_run_mode import set_dry_run
from lifecycle_logger import log_cycle_summary
from mapping_manager import warn_about_missing_mapped_destinations
from queue_worker import process_queue_once, run_ingest_and_queue_cycle, run_single_cycle


_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr


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


def setup_terminal_logging(config):
    """Enable terminal-output mirroring to a timestamped per-run log file."""
    settings = get_terminal_log_settings(config)
    if not settings["enabled"]:
        return None

    log_dir = settings["directory"] or get_default_download_dir(config)
    log_dir = os.path.expandvars(os.path.expanduser(log_dir))

    try:
        os.makedirs(log_dir, exist_ok=True)
        filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".log"
        log_path = os.path.join(log_dir, filename)
        log_stream = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"Logging setup failed: {exc}", file=sys.stderr)
        return None

    sys.stdout = TeeStream(_ORIGINAL_STDOUT, log_stream)
    sys.stderr = TeeStream(_ORIGINAL_STDERR, log_stream)
    print(f"Logging enabled. Writing terminal output to: {log_path}")
    return log_stream


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


def run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds):
    """Run processing continuously with randomized jitter between cycles."""
    if jitter_min_seconds > jitter_max_seconds:
        jitter_min_seconds, jitter_max_seconds = jitter_max_seconds, jitter_min_seconds

    print(
        f"Polling enabled. Base interval: {interval_seconds}s, jitter: {jitter_min_seconds}-{jitter_max_seconds}s."
    )
    print("Press Ctrl+C to stop.\n")

    destination_check_every_n_polls = get_destination_check_every_n_polls()

    cycle = 1
    with open_database() as connection:
        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== Poll cycle {cycle} @ {started} ===")
            run_ingest_and_queue_cycle(connection, GITHUB_TOKEN)

            if destination_check_every_n_polls > 0 and cycle % destination_check_every_n_polls == 0:
                print(f"🔎 Periodic check ({destination_check_every_n_polls}-poll interval): verifying mapped destinations still exist...")
                missing_count = warn_about_missing_mapped_destinations()
                if missing_count == 0:
                    print("   ✅ All mapped destinations are present.")

            jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
            sleep_seconds = interval_seconds + jitter
            next_poll_at = datetime.fromtimestamp(time.time() + sleep_seconds).strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter) @ {next_poll_at}.\n"
            )
            time.sleep(sleep_seconds)
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
    print(f"GHAADD {__version__} is starting...")
    config = load_config()
    log_stream = setup_terminal_logging(config)
    try:
        parsed_args = parse_cli_args(sys.argv[1:], __version__)
        set_dry_run(parsed_args.dry_run)
        if parsed_args.dry_run:
            print("🧪 Dry-run mode enabled: no writes/deletes will be performed.\n")

        if handle_cli_command(parsed_args, run_internal_smoke_tests):
            return

        polling_settings = get_polling_settings(config)

        # Only mutating run modes contend for the daemon lock; dry-run has no
        # real side effects, so it behaves like a read-only command and never
        # blocks on (or is blocked by) another running instance.
        lock_context = nullcontext() if parsed_args.dry_run else acquire_daemon_lock()
        with lock_context:
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
                or polling_settings["enabled"]
            )
            if once_mode or not poll_enabled:
                with open_database() as connection:
                    run_ingest_and_queue_cycle(connection, GITHUB_TOKEN)
                return

            interval_seconds = polling_settings["interval_seconds"]
            jitter_min_seconds = polling_settings["jitter_min_seconds"]
            jitter_max_seconds = polling_settings["jitter_max_seconds"]

            try:
                run_polling_loop(interval_seconds, jitter_min_seconds, jitter_max_seconds)
            except KeyboardInterrupt:
                print("\nPolling stopped by user.")
    finally:
        if log_stream is not None:
            sys.stdout = _ORIGINAL_STDOUT
            sys.stderr = _ORIGINAL_STDERR
            log_stream.flush()
            log_stream.close()

if __name__ == "__main__":
    main()
