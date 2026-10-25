#!/usr/bin/env python3
"""Check the Folding@home account connection the way Home Assistant uses it.

Logs in to your Folding@home account (email + passphrase, the login for
app.foldingathome.org), connects to your node and lists every linked machine
with its status. Read-only unless you pass --command.

    pip install aiohttp cryptography
    python3 fah_account_check.py --email you@example.com
    python3 fah_account_check.py --email you@example.com --command pause --machine rig-1

The passphrase is asked for interactively (or read from FAH_PASSPHRASE) and is
never printed or saved. Passkey, session id and keys are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import os
import sys
import types
from pathlib import Path

try:
    import aiohttp
    import cryptography  # noqa: F401
except ImportError:  # pragma: no cover
    sys.exit("Please install the requirements first:  pip install aiohttp cryptography")

# Load the integration's own modules without Home Assistant
_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "foldingathome"
_pkg = types.ModuleType("fahha")
_pkg.__path__ = [str(_PKG_DIR)]
sys.modules.setdefault("fahha", _pkg)
account = importlib.import_module("fahha.account")
model = importlib.import_module("fahha.model")


async def run(args: argparse.Namespace, passphrase: str) -> int:
    if args.api_url:
        account.API_URL = args.api_url
    if args.node_scheme:
        account.NODE_SCHEME = args.node_scheme
    async with aiohttp.ClientSession() as session:
        api = account.FahAccountApi(session)
        print("Logging in ...")
        try:
            creds = await api.login(args.email, passphrase)
        except account.FahAuthError as err:
            print(f"!! Login failed: {err}")
            return 2
        except account.FahConnectionError as err:
            print(f"!! Cannot reach the Folding@home API: {err}")
            return 3
        acc = creds.account
        print(f"OK  account id {creds.account_id[:10]}…")
        print(f"    user={acc.get('user')} team={acc.get('team')} cause={acc.get('cause')} "
              f"passkey={'set' if acc.get('passkey') else 'not set'} node={acc.get('node')}")
        linked = acc.get("machines") or []
        print(f"    {len(linked)} linked machine(s): {', '.join(m.get('name', '?') for m in linked) or '-'}")
        if not acc.get("node"):
            print("!! No node set on the account (app.foldingathome.org → Account settings → save)")
            return 4

        online = asyncio.Event()
        expected = {m["id"] for m in linked}

        def on_online(m) -> None:
            if expected and all(node.machines.get(i) and node.machines[i].ready for i in expected):
                online.set()

        node = account.FahNode(
            session, acc["node"], acc["pubkey"], creds.private_key_b64, on_machine_online=on_online
        )
        for m in linked:
            node.machine(m["id"], m.get("name"))
        node.start()
        if not await node.wait_connected(20):
            print(f"!! Cannot connect to node {acc['node']}: {node.last_error}")
            await node.stop()
            return 5
        print(f"OK  connected to node {acc['node']}, waiting for machines ...")
        try:
            await asyncio.wait_for(online.wait(), args.wait)
        except TimeoutError:
            pass
        for mach in node.machines.values():
            if not mach.ready:
                print(f"  -- {mach.name or mach.id[:8]}: offline")
                continue
            view = model.build_machine(mach.state)
            print(f"  OK {mach.name}: {view.status}  v{view.version}  PPD {view.ppd:,}  "
                  f"projects {', '.join(map(str, view.projects)) or '-'}")
            for u in view.units:
                print(f"       {u.slot_name}: {u.status} {u.progress:.1f}%  ETA {u.eta or '-'}  project {u.project}")
        rc = 0
        if args.command:
            target = next(
                (m for m in node.machines.values() if args.machine in (m.name, m.id)), None
            )
            if target is None or not target.connected:
                print(f"!! Machine '{args.machine}' not online")
                rc = 6
            else:
                await target.set_state(args.command)
                await asyncio.sleep(3)
                view = model.build_machine(target.state)
                print(f"OK  sent '{args.command}' to {target.name}; status now: {view.status}")
        await node.stop()
        return rc


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--email", required=True)
    p.add_argument("--wait", type=float, default=15, help="seconds to wait for machines (default 15)")
    p.add_argument("--command", choices=["fold", "pause", "finish"], help="send a command to --machine")
    p.add_argument("--machine", help="machine name (or id) for --command")
    p.add_argument("--api-url", help=argparse.SUPPRESS)
    p.add_argument("--node-scheme", help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.command and not args.machine:
        p.error("--command needs --machine")
    passphrase = os.environ.get("FAH_PASSPHRASE") or getpass.getpass("Folding@home passphrase: ")
    sys.exit(asyncio.run(run(args, passphrase)))


if __name__ == "__main__":
    main()
