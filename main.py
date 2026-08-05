import os
import random
import sys
import time
from config_manager import get_polling_settings
from datetime import datetime
from dotenv import load_dotenv

__version__ = "0.6.6-beta"

# Load environment variables from .env.
load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_PAT")

# Import application modules.
from cli_commands import handle_cli_command, parse_cli_args
from db_manager import open_database
from downloader import download_release
from queue_processor import run_ingest_and_queue_cycle


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

    cycle = 1
    with open_database() as connection:
        while True:
            started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"=== Poll cycle {cycle} @ {started} ===")
            run_ingest_and_queue_cycle(connection, GITHUB_TOKEN)

            jitter = random.randint(jitter_min_seconds, jitter_max_seconds)
            sleep_seconds = interval_seconds + jitter
            print(f"Next poll in {sleep_seconds}s ({interval_seconds}s + {jitter}s jitter).\n")
            time.sleep(sleep_seconds)
            cycle += 1


def main():
    """Run the main orchestration flow for ingest and queue processing."""
    parsed_args = parse_cli_args(sys.argv[1:], __version__)
    if handle_cli_command(parsed_args, run_internal_smoke_tests):
        return

    polling_settings = get_polling_settings()

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

if __name__ == "__main__":
    main()
