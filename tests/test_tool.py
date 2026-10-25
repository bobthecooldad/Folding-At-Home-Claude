"""The standalone account checker works against the fake cloud."""

import argparse
import importlib.util
from pathlib import Path

from .fah_cloud_mock import EMAIL, PASSKEY, PASSPHRASE


def _load():
    path = Path(__file__).parent.parent / "tools" / "fah_account_check.py"
    spec = importlib.util.spec_from_file_location("fah_account_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_tool(cloud, capsys):
    tool = _load()
    args = argparse.Namespace(
        email=EMAIL, wait=5, command="pause", machine="rig-1",
        api_url=f"http://127.0.0.1:{cloud.port}", node_scheme="ws",
    )
    assert await tool.run(args, PASSPHRASE) == 0
    out = capsys.readouterr().out
    assert "2 linked machine(s): rig-1, rig-2" in out
    assert "rig-1: folding" in out and "rig-2: folding" in out
    assert "sent 'pause' to rig-1; status now: paused" in out
    assert PASSKEY not in out and PASSPHRASE not in out and cloud.sid not in out
    args.command = None
    assert await tool.run(argparse.Namespace(**{**vars(args)}), "wrong") == 2
