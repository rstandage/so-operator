#!/usr/bin/env python3
"""
disable_port_operator.py - rollback script for enable_port_operator.py.

Restores switch configurations from the per-device backups created by
enable_port_operator.py, putting no_local_overwrite back to its previous
state (usually true/default), which re-locks ports against the
Switch Port Operator role.

Backups live in  backups/<run-folder>/<site>_<devicename>_<id6>.json  and hold
the COMPLETE device JSON as captured before the change, so rollback also
reverses anything else that changed in that config in the meantime when
--full-config is used.

Restore modes:
  * default      - surgical: PUT only the backed-up "port_config" object.
                   This reverses exactly the port-level change and does not
                   touch any other device settings.
  * --full-config - PUT the entire backed-up device config (read-only fields
                   are stripped). Use only when you want the switch config
                   rolled back to the backup in full.

Process:
  1. Resolve the API token (.env / $MIST_API_TOKEN / --token) and confirm it
     against GET /api/v1/self; resolve and confirm the organisation.
  2. Load the chosen backup folder (see --backup-dir; defaults to the most
     recent folder under backups/).
  3. Show which ports differ from the backup and confirm.
  4. PUT the restore payload per device (progress + error handling).
  5. GET each device back and verify no_local_overwrite is restored.
  6. Print a summary and write a CSV report.

Examples:
    python3 disable_port_operator.py                          # newest backup
    python3 disable_port_operator.py --backup-dir backups/enable_port_operator_HQ_Lab_20260102-030405Z
    python3 disable_port_operator.py --dry-run --device "Lab-SW"
    python3 disable_port_operator.py --full-config --yes

Requires Python 3.11+ and the same ORG API token used for the change (or one
for the same org). The token is never printed. MIT license, no support.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import mist_common as mc

SCRIPT_NAME = "disable_port_operator"

# Read-only / server-managed fields removed before a --full-config PUT.
# The Mist API ignores most unknown or read-only properties, but stripping
# these makes the restore payload explicit and clean.
RESTORE_EXCLUDE_KEYS = {
    "id", "org_id", "site_id", "created_time", "modified_time",
    "last_seen", "last_activity", "uptime", "status", "ip", "ip_addresses",
    "mac", "serial", "model", "role", "stats", "map_id",
}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    parser = mc.build_common_parser(
        "Rollback no_local_overwrite changes from backups created by "
        "enable_port_operator.py.")
    parser.add_argument(
        "--restore-from", metavar="FOLDER",
        help="backup folder to restore from (default: newest folder in --backup-dir)")
    parser.add_argument(
        "--device", metavar="NAME_OR_ID",
        help="only restore devices whose name or id contains this substring")
    parser.add_argument(
        "--ports-only", dest="ports_only", action="store_true", default=True,
        help="restore only the port_config object (default)")
    parser.add_argument(
        "--full-config", dest="ports_only", action="store_false",
        help="restore the entire backed-up device config (read-only fields "
             "are stripped) instead of just port_config")
    parser.epilog = ("Exit codes: 0 = success, 1 = fatal error, "
                     "2 = completed with per-device failures.")
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Backup loading
# --------------------------------------------------------------------------- #

def newest_backup_folder(base_dir: str) -> Path | None:
    """The most recently created run folder directly under base_dir."""
    base = Path(base_dir)
    if not base.is_dir():
        return None
    folders = [p for p in base.iterdir() if p.is_dir()]
    if not folders:
        return None
    return max(folders, key=lambda p: p.stat().st_mtime)


def load_backups(backup_path: Path) -> list[dict]:
    """Parse every backup file in the folder; die on unusable files."""
    if backup_path.is_file():
        files = [backup_path]
    elif backup_path.is_dir():
        files = sorted(backup_path.glob("*.json"))
    else:
        mc.die(f"Backup path '{backup_path}' does not exist.")
    if not files:
        mc.die(f"No .json backup files found in '{backup_path}'.")

    backups: list[dict] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            mc.die(f"Backup file {path.name} is not valid JSON: {exc}")
        if not isinstance(payload, dict) or not payload.get("device"):
            mc.die(f"Backup file {path.name} is missing a 'device' object - "
                   "was it created by enable_port_operator.py?")
        if payload.get("backup_version") != 1:
            mc.warn(f"{path.name}: unknown backup_version "
                    f"{payload.get('backup_version')!r} - attempting restore anyway.")
        payload["__path"] = path
        backups.append(payload)
    return backups


# --------------------------------------------------------------------------- #
# Restore planning
# --------------------------------------------------------------------------- #

def plan_from_backups(backups: list[dict], org_id: str) -> dict[str, dict]:
    """Map device_id -> restore plan entry from the loaded backups."""
    plan: dict[str, dict] = {}
    for payload in backups:
        device = payload.get("device") or {}
        device_id = str(payload.get("device_id") or device.get("id") or "")
        site_id = str(payload.get("site_id") or device.get("site_id") or "")
        if not device_id or not site_id:
            mc.warn(f"skipping backup {Path(payload['__path']).name}: "
                    "missing device id or site id")
            continue
        plan[device_id] = {
            "device_id": device_id,
            "backup": payload,
            "backup_path": Path(payload["__path"]),
            "site_id": site_id,
            "site_name": str(payload.get("site_name")
                             or device.get("site_id") or site_id),
            "org_id": org_id,
        }
    return plan


def diff_report(plan: dict[str, dict], client: mc.MistClient) -> None:
    """Show what would change: backed-up port_config vs the live one."""
    mc.log("")
    mc.log("Port-level differences (backup -> live):")
    found_diff = False
    for entry in plan.values():
        backup_cfg = (entry["backup"].get("device", {}).get("port_config") or {})
        try:
            live = mc.get_device(client, entry["site_id"], entry["device_id"])
        except mc.MistApiError as exc:
            mc.warn(f"cannot read live config for {entry['device_id']}: {exc}")
            continue
        live_cfg = mc.get_port_config(live)
        name = entry["backup"].get("device_name") or entry["device_id"]
        for port, want in sorted(backup_cfg.items()):
            have = live_cfg.get(port)
            have_nlo = have.get("no_local_overwrite", "default(true)") \
                if isinstance(have, dict) else have
            want_nlo = want.get("no_local_overwrite", "default(true)")
            if have != want:
                found_diff = True
                mc.log(f"  {name} {port}:")
                mc.log(f"      backup: no_local_overwrite={want_nlo}")
                mc.log(f"      live  : no_local_overwrite={have_nlo}")
    if not found_diff:
        mc.log("  (no port-level differences - nothing to restore)")


# --------------------------------------------------------------------------- #
# Restore execution + verification
# --------------------------------------------------------------------------- #

def build_restore_payload(entry: dict, full_config: bool) -> dict:
    """The PUT body restoring this device from its backup.

    * default      : {"port_config": <the complete backed-up port_config>}
    * --full-config: the whole backed-up config with read-only fields removed
    """
    backup_device = entry["backup"].get("device") or {}
    if full_config:
        body = {k: v for k, v in backup_device.items()
                if k not in RESTORE_EXCLUDE_KEYS}
        return body
    return {"port_config": backup_device.get("port_config") or {}}


def restore_row(entry: dict, org_id: str, result: str, detail: str) -> dict:
    """One CSV row for a device-level restore."""
    backup = entry["backup"]
    return {
        "timestamp_utc": mc.utc_ts(),
        "org_id": org_id,
        "site_id": entry["site_id"],
        "site_name": entry["site_name"],
        "device_id": entry["device_id"],
        "device_name": backup.get("device_name") or "",
        "result": result,
        "detail": detail,
    }


def execute_restore(client: mc.MistClient, plan: dict[str, dict],
                    rows: list[dict], full_config: bool,
                    dry_run: bool) -> tuple[int, int]:
    """PUT the restore payload per device. Returns (ok_count, fail_count)."""
    ok = fail = 0
    total = len(plan)
    for idx, (device_id, entry) in enumerate(plan.items(), start=1):
        name = entry["backup"].get("device_name") or device_id
        payload = build_restore_payload(entry, full_config)
        mode = "full config" if full_config else "port_config"
        if dry_run:
            mc.log(f"[{idx}/{total}] DRY-RUN {name}: would restore "
                   f"({mode}, {len(payload)} top-level key(s))")
            rows.append(restore_row(entry, entry["org_id"], "dry-run",
                                    f"would restore ({mode})"))
            ok += 1
            continue
        mc.log(f"[{idx}/{total}] PUT restore {name} ({mode}) ...", end="")
        try:
            mc.put_device(client, entry["site_id"], device_id, payload)
        except mc.MistApiError as exc:
            mc.log(" FAILED")
            mc.dbg(f"restore failed for {device_id}: status={exc.status} "
                   f"detail={exc.detail}", True)
            mc.warn(f"{name}: {exc}")
            rows.append(restore_row(entry, entry["org_id"], "failed", str(exc)))
            fail += 1
            continue
        mc.log(" done")
        rows.append(restore_row(entry, entry["org_id"], "success",
                                "restore PUT accepted"))
        ok += 1
    return ok, fail


def verify_restore(client: mc.MistClient, plan: dict[str, dict],
                   rows: list[dict], dry_run: bool) -> tuple[int, int]:
    """GET each device and confirm port_config matches the backup.

    Returns (verified_count, mismatch_count) across backed-up ports.
    """
    if dry_run:
        return 0, 0
    verified = mismatch = 0
    mc.log("")
    mc.log("Verifying restores (GET each device) ...")
    for entry in plan.values():
        name = entry["backup"].get("device_name") or entry["device_id"]
        try:
            live = mc.get_device(client, entry["site_id"], entry["device_id"])
        except mc.MistApiError as exc:
            mc.warn(f"could not re-read {name}: {exc}")
            rows.append(restore_row(entry, entry["org_id"], "verify_error",
                                    str(exc)))
            continue
        backup_cfg = (entry["backup"].get("device", {}).get("port_config") or {})
        live_cfg = mc.get_port_config(live)
        for port, want in sorted(backup_cfg.items()):
            have = live_cfg.get(port)
            if have == want:
                verified += 1
                mc.log(f"  {name} {port}: matches backup OK")
            else:
                mismatch += 1
                mc.warn(f"{name} {port}: still differs from backup")
                rows.append(restore_row(entry, entry["org_id"], "verify_failed",
                                        f"port {port} still differs"))
    return verified, mismatch


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def csv_path(args: argparse.Namespace) -> Path:
    """Resolve the CSV output path (default: reports/<script>_<ts>.csv)."""
    if args.csv:
        return Path(args.csv)
    reports = Path("reports")
    return reports / f"{SCRIPT_NAME}_{mc.utc_ts()}.csv"


def main() -> int:
    args = parse_args()

    client, me, env = mc.make_client_from_args(args, SCRIPT_NAME)
    ctx = mc.confirm_org_scope(client, me, env, args.debug)
    org = ctx["org"]
    org_id = str(org["id"])

    # Step 2 - locate and load backups
    base_dir = Path(args.backup_dir)
    if args.restore_from:
        backup_path = Path(args.restore_from)
    else:
        newest = newest_backup_folder(str(base_dir))
        if newest is None:
            mc.die(f"No backup folders found under '{base_dir}'. Run "
                   "enable_port_operator.py first.")
        backup_path = newest
    mc.log(f"Restoring from: {backup_path}")
    backups = load_backups(backup_path)

    # --device filter
    if args.device:
        needle = args.device.strip().lower()
        backups = [b for b in backups
                   if needle in str(b.get("device_name") or "").lower()
                   or needle in str(b.get("device_id") or "").lower()]
        if not backups:
            mc.die(f"--device '{args.device}' matched no backup files.")

    mc.log(f"Loaded {len(backups)} backup file(s).")
    plan = plan_from_backups(backups, org_id)
    if not plan:
        mc.die("No restorable devices found in the backups.")

    # Step 3 - show what would change and confirm
    diff_report(plan, client)
    if args.dry_run:
        mc.log("DRY-RUN mode - no changes will be written.")
    if not args.yes and not args.dry_run:
        if not confirm_restore(len(plan), args.full_config):
            mc.log("Aborted by user - no changes made.")
            return 0

    # Step 4 - PUT restore per device
    rows: list[dict] = []
    ok, fail = execute_restore(client, plan, rows, args.full_config, args.dry_run)

    # Step 5 - GET validation
    verified, mismatch = verify_restore(client, plan, rows, args.dry_run)

    # Step 6 - summary + CSV
    summary_lines = [
        "",
        "=" * 72,
        "SUMMARY",
        f"  org            : {org.get('name', '?')} ({org_id})",
        f"  backup folder  : {backup_path}",
        f"  devices        : {len(plan)}",
        f"  restore ok     : {ok}",
        f"  restore failed : {fail}",
    ]
    if not args.dry_run:
        summary_lines.append(f"  ports verified : {verified} "
                             f"(mismatched: {mismatch})")
    summary_lines.append("=" * 72)
    for line in summary_lines:
        mc.log(line)

    mc.write_csv_summary(csv_path(args), rows)
    return 0 if fail == 0 and (args.dry_run or mismatch == 0) else 2


def confirm_restore(device_count: int, full_config: bool) -> bool:
    """Yes/no gate before restoring (skipped with --yes or --dry-run)."""
    mode = "FULL config (entire device)" if full_config else "port_config only"
    while True:
        try:
            answer = input(f"Restore {device_count} device(s) ({mode})? [y/N]: "
                           ).strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        mc.log("Please answer 'y' or 'n'.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        mc.log("")
        mc.log("[ERROR] Interrupted by user.")
        sys.exit(130)
    except Exception:
        mc.log_exception()
        sys.exit(1)