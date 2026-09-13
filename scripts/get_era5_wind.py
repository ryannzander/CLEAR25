#!/usr/bin/env python3
"""
One-command front door for the ERA5 wind download.

scripts/download_era5_wind.py is the real downloader; this wrapper just removes
every step you would otherwise have to remember by hand:

  1. installs cdsapi if it is missing (into the interpreter you ran this with,
     so it can't land in some other Python),
  2. writes ~/.cdsapirc from a token you paste, if you have no credentials yet,
  3. runs the offline selftest,
  4. pulls ONE month first as a live smoke test (~2 min) so a broken setup
     fails in two minutes instead of three hours,
  5. asks before starting the full multi-year pull.

Usage:
    python3 scripts/get_era5_wind.py                 # guided, asks before the big pull
    python3 scripts/get_era5_wind.py --yes           # no questions, do everything
    python3 scripts/get_era5_wind.py --setup-only    # steps 1-3, no downloading
    python3 scripts/get_era5_wind.py --out-dir "D:/ERA5_Wind" --years 2023 2024

Any other flag is passed straight through to download_era5_wind.py, so
--months 5 6 7 8 9, --chunk month, --area etc. all work here too.

Get the CDS token first (free, one-time): register at
https://cds.climate.copernicus.eu/, accept the "ERA5 hourly data on single
levels" licence in the browser, then copy your Personal Access Token from
https://cds.climate.copernicus.eu/profile
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOWNLOADER = HERE / "download_era5_wind.py"

SMOKE_TEST = ["--years", "2023", "--months", "6"]


def say(msg=""):
    print(msg, flush=True)


def step(n, total, msg):
    say(f"\n[{n}/{total}] {msg}")


def _installed_version(cdsapi_module):
    """Version via the downloader's resolver, which knows cdsapi has no __version__."""
    try:
        sys.path.insert(0, str(HERE))
        from download_era5_wind import cdsapi_version
        return cdsapi_version(cdsapi_module) or "(version unknown)"
    except Exception:
        return "(version unknown)"


def ensure_cdsapi(assume_yes=False):
    """Install cdsapi into THIS interpreter if it isn't importable."""
    try:
        import cdsapi  # noqa: F401
    except ImportError:
        pass
    else:
        # Reported separately: a failure to import OUR helper is not a reason
        # to go and pip-install cdsapi.
        say(f"      cdsapi {_installed_version(cdsapi)} already installed")
        return True

    say(f"      cdsapi is not installed for {sys.executable}")
    if not assume_yes and not ask("      Install it now?", default=True):
        say("      Skipped. Install it yourself with:")
        say(f'        "{sys.executable}" -m pip install "cdsapi>=0.7.7"')
        return False

    cmd = [sys.executable, "-m", "pip", "install", "cdsapi>=0.7.7"]
    say(f"      running: {' '.join(cmd)}")
    if subprocess.call(cmd) != 0:
        say("      pip failed - install cdsapi manually and re-run this script.")
        return False
    try:
        import cdsapi  # noqa: F401,F811
    except ImportError:
        say("      cdsapi still not importable after install - check your Python setup.")
        return False
    say(f"      installed cdsapi {_installed_version(cdsapi)}")
    return True


def ask(question, default=False):
    """Yes/no prompt. A non-interactive stdin takes the default rather than hanging."""
    if not sys.stdin or not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer[0] == "y"


def credentials_present():
    if os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"):
        return "CDSAPI_URL/CDSAPI_KEY environment variables"
    rc = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc"))
    if rc.exists():
        return str(rc)
    return None


def write_credentials(token):
    """Write ~/.cdsapirc, readable only by this user where the OS supports it."""
    rc = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc"))
    rc.write_text("url: https://cds.climate.copernicus.eu/api\n"
                  f"key: {token}\n", encoding="utf-8")
    try:
        os.chmod(rc, 0o600)   # best-effort; Windows ACLs don't map onto this
    except OSError:
        pass
    return rc


def validate_token(token):
    """Cheap sanity checks - catches the mistakes that produce a confusing 401."""
    token = token.strip()
    if not token:
        return None, "empty"
    if token.startswith(("url:", "key:")):
        return None, "paste only the token itself, not the 'key:' line"
    if any(c.isspace() for c in token):
        return None, "contains whitespace"
    if ":" in token:
        return None, ("looks like the retired <uid>:<key> pair - the current CDS "
                      "uses a single Personal Access Token")
    return token, None


def ensure_credentials(assume_yes=False):
    where = credentials_present()
    if where:
        say(f"      credentials found: {where}")
        return True
    if assume_yes or not (sys.stdin and sys.stdin.isatty()):
        say("      No CDS credentials found, and no terminal to ask on.")
        say("      Create ~/.cdsapirc with:")
        say("          url: https://cds.climate.copernicus.eu/api")
        say("          key: <your Personal Access Token>")
        return False

    say("      No CDS credentials found.")
    say("      Get a token: https://cds.climate.copernicus.eu/profile")
    say("      (register first, and accept the ERA5 single-levels licence in the browser)")
    for _ in range(3):
        try:
            raw = input("      Paste your Personal Access Token (blank to skip): ")
        except EOFError:
            return False
        if not raw.strip():
            return False
        token, problem = validate_token(raw)
        if token:
            rc = write_credentials(token)
            say(f"      wrote {rc}")
            return True
        say(f"      That doesn't look right ({problem}). Try again.")
    return False


def run_downloader(extra, label):
    cmd = [sys.executable, str(DOWNLOADER)] + list(extra)
    say(f"      running: {' '.join(cmd)}")
    code = subprocess.call(cmd)
    if code != 0:
        say(f"      {label} FAILED (exit {code}) - nothing else will be started.")
    return code == 0


def selftest():
    """Offline checks for this wrapper's own logic."""
    assert DOWNLOADER.exists(), "download_era5_wind.py must sit next to this script"
    assert validate_token(" abc123 ")[0] == "abc123"
    for bad, _hint in (("", "empty"), ("key: abc", "key line"), ("a b", "space"),
                       ("1234:abcd", "uid:key")):
        token, problem = validate_token(bad)
        assert token is None and problem, bad
    # A non-interactive stdin must take the default, never block.
    assert ask("unused", default=True) is True or sys.stdin.isatty()
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(
        description="Set up and run the ERA5 wind download in one command.",
        epilog="Unknown flags are forwarded to download_era5_wind.py.")
    ap.add_argument("--yes", action="store_true", help="don't ask anything, do everything")
    ap.add_argument("--setup-only", action="store_true",
                    help="install + credentials + selftest, then stop")
    ap.add_argument("--skip-smoke-test", action="store_true",
                    help="go straight to the full pull, no one-month trial")
    ap.add_argument("--selftest", action="store_true", help="offline checks, then exit")
    args, passthrough = ap.parse_known_args()

    if args.selftest:
        selftest()
        return 0

    total = 3 if args.setup_only else 5
    say("ERA5 wind download - guided setup")
    say(f"repo script: {DOWNLOADER}")

    step(1, total, "Checking cdsapi")
    if not ensure_cdsapi(args.yes):
        return 1

    step(2, total, "Checking CDS credentials")
    have_credentials = ensure_credentials(args.yes)

    step(3, total, "Running the downloader's offline selftest")
    if not run_downloader(["--selftest"], "selftest"):
        return 1

    if args.setup_only:
        say("\nSetup finished." if have_credentials
            else "\nSetup finished, but credentials are still missing.")
        return 0 if have_credentials else 1
    if not have_credentials:
        say("\nCannot download without credentials. Re-run once ~/.cdsapirc exists.")
        return 1

    if not args.skip_smoke_test:
        step(4, total, "Live smoke test: June 2023 only (~2 minutes, ~31 MB)")
        if not run_downloader(SMOKE_TEST + passthrough, "smoke test"):
            return 1
        say("      smoke test OK - credentials, licence and network all work")
    else:
        step(4, total, "Smoke test skipped")

    step(5, total, "Full download")
    say("      The default is 2018-2025, roughly 3 GB and a few hours of")
    say("      server-side queueing. It is resumable: finished years are skipped,")
    say("      so you can stop it and re-run this command any time.")
    if not (args.yes or ask("      Start the full download now?", default=False)):
        say("\n      Not started. When you want it:")
        say(f'        "{sys.executable}" "{DOWNLOADER}" ' + " ".join(passthrough))
        return 0
    return 0 if run_downloader(passthrough, "download") else 1


if __name__ == "__main__":
    sys.exit(main())
