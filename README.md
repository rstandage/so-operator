# Mist Switch Port Operator Toolkit

Stdlib-only Python 3.11+ toolkit for the Juniper Mist API that unlocks (or
re-locks) switch ports so that users holding the **Switch Port Operator**
RBAC role can edit them.

Mist's `junos_port_config` object has a per-port flag:

    no_local_overwrite - "Prevent helpdesk to override the port config"
                         (default: true)

When `no_local_overwrite` is `true` (the default), Switch Port Operator
users cannot change the port. Setting it to `false` on selected ports lets
that role manage those ports, while the rest of the switch stays locked
down. This toolkit flips that flag at scale, with backups, verification,
and an easy rollback.

## Contents

| File | Purpose |
|---|---|
| `mist_common.py` | Shared library: auth, retries, pacing, sites, backups, CSV |
| `enable_port_operator.py` | Unlock ports (`no_local_overwrite: false`) per port profile |
| `disable_port_operator.py` | Rollback from the backups the enable run creates |
| `.env.example` | Template for credentials - copy to `.env` and fill in |
| `LICENSE` | MIT license |

No third-party packages are required - everything runs on the Python
standard library.

## Requirements

* Python 3.11 or newer
* A **Mist API token** (org-scoped) with write access to the target site(s)
* Network access to `https://api.mist.com` (or your regional cloud)

## Setup

```console
cp .env.example .env
# then edit .env and paste your API token:
#   MIST_API_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
```

Optional `.env` settings:

| Key | Meaning |
|---|---|
| `MIST_API_TOKEN` | Org API token (never printed or logged) |
| `MIST_BASE_URL` | Regional cloud, e.g. `https://api.eu.mist.com` (default: api.mist.com) |
| `MIST_ORG_ID` | Org id to operate on (skips the org confirmation prompt if it matches the token) |
| `MIST_SITE_ID` | Pre-selects a site (equivalent to answering the site prompt) |

Credentials are looked up in this order: `--token` flag → `$MIST_API_TOKEN`
→ `.env`. The token is resolved against `GET /api/v1/self` before anything
is changed, and the org scope is confirmed with you before work starts.

## Usage - enable_port_operator.py

Unlock ports for the Switch Port Operator role, per port profile.

```console
# 1. See what would change first (no writes at all)
python3 enable_port_operator.py --dry-run

# 2. Apply for real, prompting for site + profiles
python3 enable_port_operator.py

# 3. Fully scripted run
python3 enable_port_operator.py --site "HQ Lab" --profiles default \
    --yes
```

Process:

1. Confirm token + org (`GET /api/v1/self` + privileges).
2. Pick site (`--site`/`--all-sites`/interactive; `MIST_SITE_ID` preselects).
3. Back up EVERY switch in scope to `backups/<run-folder>/<site>_<name>_<id6>.json`
   (full device config, taken before any change).
4. List port profiles (org network templates + device-level `port_usages`),
   prompt (or take `--profiles`), build the change plan.
5. `PUT {"port_config": {port: {<port's current config>, "no_local_overwrite": false}}}`
   per switch. Mist overwrites each named port object on PUT rather than
   merging into it, so the port's existing config is resent with only the one
   flag changed - otherwise `usage`, `description`, VLANs, PoE, STP, etc. on
   that port would be dropped.
6. GET each changed switch and verify the flag took effect **and** that every
   resent key survived the write.
7. Summary + CSV in `reports/`.

### Flags

| Flag | Meaning |
|---|---|
| `--profiles NAME [NAME...]` | profile(s) to unlock (skips the prompt) |
| `--site` / `--all-sites` | scope; interactive if neither |
| `--backup-dir DIR` | where run folders are created (default `backups`) |
| `--csv PATH` | CSV summary path (default `reports/<script>_<ts>.csv`) |
| `--yes` | non-interactive, skip confirmations |
| `--dry-run` | show plan, write nothing |
| `--debug` | URLs, payloads, HTTP bodies |
| `--version` | toolkit version |

Exit codes: `0` ok, `1` fatal error, `2` completed but some PUT/verify failed.

## Usage - disable_port_operator.py (rollback)

Restores from the backups the enable run created - nothing else needed.

```console
# Restore everything from the most recent backup folder
python3 disable_port_operator.py

# Restore from a specific run folder
python3 disable_port_operator.py --restore-from backups/enable_port_operator_HQ_Lab_20260102-030405Z

# Preview / single device
python3 disable_port_operator.py --dry-run
python3 disable_port_operator.py --device "Lab-SW"
```

Two restore modes:

| Mode | What it PUTs |
|---|---|
| default | Only the backed-up `port_config` object (surgical, recommended) |
| `--full-config` | The entire backed-up device config, with read-only fields stripped |

Before writing, the script prints a backup-vs-live diff of `no_local_overwrite`
per port, asks for confirmation (skip with `--yes`), then PUTs, re-GETs every
device, and verifies each backed-up port matches. Exit codes mirror the enable
script (`0`/`1`/`2`).

## Safety and behaviour

* **Backups first.** The enable run captures the full config of every switch
  in scope before any change, so rollback is always possible.
* **Surgical writes.** Only the targeted ports' `port_config` entries are PUT,
  and each is resent with its existing config plus the single flipped flag -
  nothing else on the device, and no other port, is touched.
* **Token safety.** The token is never printed, logged, or written to CSV.
* **Rate limits.** Requests are paced; 429/5xx responses back off and retry,
  honouring `Retry-After` when present.
* **Verification.** Every write is read back and checked; shortfalls exit 2.
* **Artifacts.** `backups/` (JSON), `logs/` (run logs), `reports/` (CSV).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `MIST_API_TOKEN is not set` | Add it to `.env` or export it / pass `--token` |
| `401` / `403` on startup | Token invalid or lacks this org - check `--base-url` for regional clouds |
| `--device` matched nothing | Names must match the backup contents (substring, case-insensitive) |
| Verification reports mismatches | Another admin changed the port after the run; re-run or restore manually |
| Restore says "no port-level differences" | Ports already match the backup - nothing to do |

## Development notes

* Python 3.11+, standard library only (`urllib.request`, `json`, `csv`,
  `argparse`, `pathlib`).
* Run a syntax/CLI check after edits:
  `python3 -m py_compile *.py && python3 enable_port_operator.py --help`
* Test against a **lab org** first: `--dry-run`, then a real run on one site.

## License

MIT - see `LICENSE`. Provided as-is, no support, use at your own risk.
Always verify changes in the Mist UI or API after running.