#!/usr/bin/env python3
"""
enable_port_operator.py - enable "Switch Port Operator" edits on selected ports.

Sets  no_local_overwrite: false  on every switch port that uses one of the
selected port profiles, so users holding the Switch Port Operator RBAC role
can edit those ports from the Mist UI or API.

Per the Juniper Mist OpenAPI spec (junos_port_config):
    no_local_overwrite - "Prevent helpdesk to override the port config"
                         (default: true)

Process (each step shown with progress output):
  1. Resolve the API token (.env / $MIST_API_TOKEN / --token) and confirm it
     against GET /api/v1/self; resolve and confirm the organisation.
  2. Select the target site (interactive, --site, or --all-sites).
  3. Back up every affected device to backups/<timestamp>/<site>_<name>_<id>.json
     (full device JSON - restore with disable_port_operator.py).
  4. List port profiles (org network templates + device-level port_usages)
     and ask which profiles to unlock.
  5. PUT {"port_config": {"<port>": {"no_local_overwrite": false}, ...}} to
     each affected switch (device-level PUT - partial update semantics).
  6. GET each changed device back and verify the new value took effect.
  7. Print a summary and write a CSV report.

Examples:
    python3 enable_port_operator.py                      # fully interactive
    python3 enable_port_operator.py --site "HQ Lab" --profiles default
    python3 enable_port_operator.py --all-sites --yes --profiles default ap
    python3 enable_port_operator.py --dry-run            # no writes at all

Requires Python 3.11+ and a Mist ORG API token scoped to exactly one org.
The token is never printed. This toolkit is provided under the MIT license,
as-is, with no support.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mist_common as mc

SCRIPT_NAME = "enable_port_operator"
TARGET_NO_LOCAL_OVERWRITE = False   # unlock value set on selected ports


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args() -> argparse.Namespace:
    parser = mc.build_common_parser(
        "Unlock ports (no_local_overwrite=false) for the Switch Port "
        "Operator role, per port profile.")
    parser.add_argument(
        "--profiles", nargs="+", metavar="PROFILE",
        help="port profile name(s) to unlock (skips the profile prompt)")
    parser.epilog = ("Exit codes: 0 = success, 1 = fatal error, "
                     "2 = completed with per-port failures.")
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Profile selection
# --------------------------------------------------------------------------- #

def prompt_profile_selection(profiles: dict[str, dict],
                             args_profiles: list[str] | None,
                             assume_yes: bool) -> set[str]:
    """Decide which port profiles to unlock.

    Priority: --profiles list > interactive prompt > --yes (all profiles).
    """
    names = sorted(profiles)
    if not names:
        mc.die("No port profiles found in org network templates or on the "
               "selected switches - nothing to unlock.")

    if args_profiles:
        wanted = [p.strip() for p in args_profiles if p.strip()]
        unknown = [p for p in wanted if p not in profiles]
        if unknown:
            mc.die(f"Unknown profile(s): {', '.join(unknown)}. "
                   f"Available: {', '.join(names)}")
        return set(wanted)

    if assume_yes:
        mc.log("--yes given: selecting ALL port profiles "
               f"({len(names)} profile(s)).")
        return set(names)

    mc.log("")
    mc.log("Port profiles available:")
    for idx, name in enumerate(names, start=1):
        sources = ", ".join(profiles[name]["sources"][:3])
        extra = "" if len(profiles[name]["sources"]) <= 3 else " ..."
        mc.log(f"  {idx:>2}) {name}   (defined in: {sources}{extra})")
    mc.log("")
    mc.log("Which profiles should be unlocked for Switch Port Operators?")
    mc.log("Enter numbers (e.g. 1,3), profile names, 'all', or 'none' to abort.")
    while True:
        try:
            answer = input("Profiles to unlock: ").strip()
        except EOFError:
            mc.die("No interactive input available; use --profiles or --yes.")
        if not answer:
            continue
        if answer.lower() in ("none", "q", "quit", "abort"):
            mc.die("Aborted by user - no changes made.", code=0)
        chosen: set[str] = set()
        valid = True
        for token in answer.replace(",", " ").split():
            if token.lower() == "all":
                chosen = set(names)
                break
            if token.isdigit() and 1 <= int(token) <= len(names):
                chosen.add(names[int(token) - 1])
            elif token in profiles:
                chosen.add(token)
            else:
                mc.log(f"  [WARN]  '{token}' is not a valid profile or number.")
                valid = False
        if valid and chosen:
            return chosen


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover_switches(client: mc.MistClient, org: dict, site_id: str,
                      site_name: str) -> list[dict]:
    """List switches in scope, with per-site progress output."""
    if site_name == mc.ALL_SITES_LABEL:
        sites = mc.list_org_sites(client, org["id"])
        mc.log(f"Discovering switches across {len(sites)} site(s) ...")
        all_switches: list[dict] = []
        for idx, site in enumerate(sites, start=1):
            s_id = str(site.get("id"))
            s_name = str(site.get("name") or s_id)
            mc.log(f"  [{idx}/{len(sites)}] site '{s_name}': listing devices ...",
                   end="")
            try:
                found = mc.fetch_switches(client, s_id)
            except mc.MistApiError as exc:
                mc.warn(f"skipping site '{s_name}': {exc}")
                continue
            mc.log(f" {len(found)} switch(es)")
            for sw in found:
                sw["__site_id"] = s_id
                sw["__site_name"] = s_name
            all_switches.extend(found)
        return all_switches

    mc.log(f"Listing switches at site '{site_name}' ...")
    switches = mc.fetch_switches(client, site_id)
    for sw in switches:
        sw["__site_id"] = site_id
        sw["__site_name"] = site_name
    return switches


def build_plan(switches: list[dict],
               selected_profiles: set[str]) -> dict[str, dict]:
    """Map device_id -> per-device change plan (ports needing the unlock).

    Ports already unlocked are skipped and counted for the report.
    """
    plan: dict[str, dict] = {}
    for sw in switches:
        targets = mc.find_target_ports(sw, selected_profiles)
        to_change = [(p, cfg) for p, cfg in targets if mc.port_needs_change(cfg)]
        already = len(targets) - len(to_change)
        if not to_change:
            continue
        plan[str(sw.get("id"))] = {
            "device": sw,
            "ports": [p for p, _ in to_change],
            "already_ok": already,
            "site_id": str(sw.get("__site_id")),
            "site_name": str(sw.get("__site_name") or sw.get("__site_id")),
        }
    return plan


def show_plan(plan: dict[str, dict], selected: set[str]) -> None:
    """Print a human-readable change plan."""
    total_ports = sum(len(entry["ports"]) for entry in plan.values())
    mc.log("")
    mc.log("=" * 72)
    mc.log(f"CHANGE PLAN - unlock {total_ports} port(s) on "
           f"{len(plan)} switch(es):")
    mc.log(f"  profiles: {', '.join(sorted(selected))}")
    mc.log("  change  : no_local_overwrite true/default -> false")
    for entry in plan.values():
        dev = entry["device"]
        mc.log(f"  - {dev.get('name') or dev.get('id')} "
               f"[{dev.get('model') or 'switch'}] @ site '{entry['site_name']}'")
        mc.log(f"      ports: {', '.join(entry['ports'])}")
        if entry["already_ok"]:
            mc.log(f"      ({entry['already_ok']} matching port(s) already "
                   "unlocked - skipped)")
    mc.log("=" * 72)


def confirm_changes(plan: dict[str, dict], assume_yes: bool) -> bool:
    """Final yes/no confirmation before any write (skipped with --yes)."""
    if assume_yes:
        mc.log("--yes given: proceeding without confirmation prompt.")
        return True
    while True:
        try:
            answer = input("Proceed with the changes above? [y/N]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no", ""):
            return False
        mc.log("Please answer 'y' or 'n'.")


# --------------------------------------------------------------------------- #
# Change execution (step 5) + verification (step 6)
# --------------------------------------------------------------------------- #

def make_row(entry: dict, org_id: str, port: str, result: str, detail: str,
             new_value: bool | None) -> dict:
    """One CSV summary row for a port change."""
    dev = entry["device"]
    return {
        "timestamp_utc": mc.utc_ts(),
        "org_id": org_id,
        "site_id": entry["site_id"],
        "site_name": entry["site_name"],
        "device_id": str(dev.get("id")),
        "device_name": dev.get("name") or "",
        "device_model": dev.get("model") or "",
        "port": port,
        "new_no_local_overwrite": "" if new_value is None else new_value,
        "result": result,
        "detail": detail,
    }


def apply_changes(client: mc.MistClient, plan: dict[str, dict], org_id: str,
                  rows: list[dict], dry_run: bool) -> bool:
    """PUT the unlock payload to every planned device. Returns all_ok."""
    all_ok = True
    for idx, (device_id, entry) in enumerate(plan.items(), start=1):
        dev = entry["device"]
        label = dev.get("name") or device_id
        site_id = entry["site_id"]
        payload = {"port_config": {
            port: {"no_local_overwrite": TARGET_NO_LOCAL_OVERWRITE}
            for port in entry["ports"]
        }}
        if dry_run:
            mc.log(f"[{idx}/{len(plan)}] DRY-RUN {label}: would PUT "
                   f"{len(entry['ports'])} port(s) -> no_local_overwrite=false")
            for port in entry["ports"]:
                rows.append(make_row(entry, org_id, port, "dry-run",
                                     "would unlock", TARGET_NO_LOCAL_OVERWRITE))
            continue
        mc.log(f"[{idx}/{len(plan)}] PUT {label} "
               f"({len(entry['ports'])} port(s)) ...", end="")
        try:
            mc.put_device(client, site_id, device_id, payload)
        except mc.MistApiError as exc:
            mc.log(" FAILED")
            mc.dbg(f"PUT failed for {device_id}: status={exc.status} "
                   f"detail={exc.detail}", True)
            mc.warn(f"{label}: {exc}")
            for port in entry["ports"]:
                rows.append(make_row(entry, org_id, port, "failed",
                                     str(exc), None))
            all_ok = False
            continue
        mc.log(" done")
        for port in entry["ports"]:
            rows.append(make_row(entry, org_id, port, "success",
                                 "PUT accepted", TARGET_NO_LOCAL_OVERWRITE))
    return all_ok


def verify_changes(client: mc.MistClient, plan: dict[str, dict], org_id: str,
                   rows: list[dict], dry_run: bool) -> int:
    """GET each changed device and confirm no_local_overwrite is now False.

    Returns the number of ports that verified successfully.
    """
    if dry_run:
        return 0
    verified = 0
    mc.log("")
    mc.log("Verifying changes (GET each device) ...")
    for idx, (device_id, entry) in enumerate(plan.items(), start=1):
        dev = entry["device"]
        label = dev.get("name") or device_id
        try:
            fresh = mc.get_device(client, entry["site_id"], device_id)
        except mc.MistApiError as exc:
            mc.warn(f"could not re-read {label}: {exc}")
            for port in entry["ports"]:
                rows.append(make_row(entry, org_id, port, "verify_error",
                                     str(exc), None))
            continue
        cfg = mc.get_port_config(fresh)
        for port in entry["ports"]:
            value = cfg.get(port, {}).get("no_local_overwrite")
            if value is False:
                verified += 1
                mc.log(f"  [{idx}/{len(plan)}] {label} {port}: "
                       "no_local_overwrite=False OK")
            else:
                mc.warn(f"{label} {port}: expected no_local_overwrite=False "
                        f"but found {value!r}")
                rows.append(make_row(entry, org_id, port, "verify_failed",
                                     f"found {value!r}", None))
    return verified


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    args = parse_args()

    client, me, env = mc.make_client_from_args(args, SCRIPT_NAME)
    ctx = mc.confirm_org_scope(client, me, env, args.debug)
    org = ctx["org"]
    org_id = str(org["id"])

    # Step 2 - site selection
    if args.all_sites:
        site_id, site_name = mc.ALL_SITES_LABEL, mc.ALL_SITES_LABEL
        mc.log("--all-sites given: every site visible to this token is in scope.")
    else:
        site_id, site_name = mc.select_site(client, org, env,
                                            args.site, args.yes)

    # Step 3 - backup every switch in scope BEFORE any change
    switches = discover_switches(client, org, site_id, site_name)
    if not switches:
        mc.die("No switches found in scope - nothing to do.")
    backup_dir = Path(args.backup_dir)
    mc.log(f"Backing up {len(switches)} switch(es) to '{backup_dir}/' ...")
    backup_folder = mc.new_backup_folder(str(backup_dir),
                                         prefix=f"{SCRIPT_NAME}_{mc.sanitize_filename(site_name)}")
    for sw in switches:
        mc.backup_device(sw, backup_folder,
                         str(sw.get("__site_name") or site_name))
    mc.log(f"Backups complete: {len(switches)} file(s) in {backup_folder}")

    # Step 4 - list port profiles and choose which to unlock
    profiles = mc.list_port_profiles(client, org_id, switches)
    selected = prompt_profile_selection(profiles, args.profiles, args.yes)
    mc.log(f"Selected profiles: {', '.join(sorted(selected))}")

    # Build the per-device plan from the CURRENT device JSON
    plan = build_plan(switches, selected)
    if not plan:
        mc.log("")
        mc.log("No ports need changing - every port using the selected "
               "profile(s) is already unlocked. Nothing to do.")
        mc.write_csv_summary(csv_path(args), [])
        return 0

    show_plan(plan, selected)
    if not confirm_changes(plan, args.yes):
        mc.log("Aborted by user - no changes made.")
        mc.write_csv_summary(csv_path(args), [])
        return 0

    # Step 5 - PUT no_local_overwrite=false on each selected port
    rows: list[dict] = []
    all_ok = apply_changes(client, plan, org_id, rows, args.dry_run)

    # Step 6 - GET validation
    verified = verify_changes(client, plan, org_id, rows, args.dry_run)

    # Step 7 - CSV summary + exit code
    total = sum(len(e["ports"]) for e in plan.values())
    summary_lines = [
        "",
        "=" * 72,
        "SUMMARY",
        f"  org           : {org.get('name', '?')} ({org_id})",
        f"  site(s)       : {site_name}",
        f"  profiles      : {', '.join(sorted(selected))}",
        f"  switches      : {len(plan)}",
        f"  ports planned : {total}",
        f"  PUT ok        : {sum(1 for r in rows if r['result'] == 'success')}",
        f"  PUT failed    : {sum(1 for r in rows if r['result'] == 'failed')}",
    ]
    if not args.dry_run:
        summary_lines.append(f"  verified      : {verified}/{total}")
    summary_lines.append("=" * 72)
    for line in summary_lines:
        mc.log(line)

    path = csv_path(args)
    mc.write_csv_summary(path, rows)
    return 0 if (all_ok and (args.dry_run or verified == total)) else 2


def csv_path(args: argparse.Namespace) -> Path:
    """Resolve the CSV output path (default: reports/<script>_<ts>.csv)."""
    if args.csv:
        return Path(args.csv)
    reports = Path("reports")
    return reports / f"{SCRIPT_NAME}_{mc.utc_ts()}.csv"


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