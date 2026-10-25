"""Folding@home account client: REST API + node WebSocket + machine sessions.

How the official web app (app.foldingathome.org) reaches machines, and what
this module re-implements:

1. REST login at ``api.foldingathome.org`` with email + derived password hash
   -> session id (sent as the ``Authorization`` header).
2. ``GET /account/secret`` -> account RSA private key (wrapped, decrypted
   locally with the passphrase-derived key; the passphrase is never stored).
3. ``GET /account`` -> account public key, node host, machine list.
4. WebSocket ``wss://<node>/ws/account``, ``login`` signed with the account
   key.  The node forwards every linked machine's ``connect`` message which
   carries an AES session key encrypted to the account key.
5. Per machine: ``session-open`` then ``message``s, AES-256-CBC encrypted.
   The decrypted ``content`` is exactly the local client API (snapshot +
   list patches, ``state`` / ``config`` / ``wus`` commands).

No Home Assistant imports, so it can be tested standalone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from . import fahcrypto as fc

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.foldingathome.org"
NODE_SCHEME = "wss"
CONNECT_TIMEOUT = 20
HEARTBEAT = 30
RECONNECT_MIN = 5
RECONNECT_MAX = 120
# Largest WebSocket message accepted from the node (a full client snapshot
# with log lines is well under this). Protects against memory exhaustion.
MAX_MESSAGE_SIZE = 32 * 1024 * 1024
# Same rule the web client uses for the account's node host name, plus an
# optional port. Stops a bad value from steering the connection elsewhere
# (paths, credentials, query strings ...).
NODE_HOST_RE = re.compile(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)+(:\d{1,5})?$", re.IGNORECASE)
REDACT_KEYS = {"passkey", "token", "key", "secret", "password", "signature", "payload"}


# ------------------------------------------------------------------ errors
class FahError(Exception):
    """Base error."""


class FahAuthError(FahError):
    """Wrong email / passphrase, or session no longer valid."""


class FahConnectionError(FahError):
    """API / node not reachable."""


class FahApiError(FahError):
    """API returned an error."""


def redact(obj: Any) -> Any:
    """Copy of ``obj`` with secrets masked (for logging / diagnostics)."""
    if isinstance(obj, dict):
        return {
            k: ("**REDACTED**" if k in REDACT_KEYS and v not in (None, "") else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def apply_update(state: dict[str, Any], update: list[Any]) -> None:
    """Apply a list patch to ``state`` in place (port of web-client Updatable)."""
    if not isinstance(update, list) or len(update) < 2:
        return
    obj: Any = state
    i = 0
    while i < len(update) - 2:
        key = update[i]
        i += 1
        next_is_int = isinstance(update[i], int)
        if isinstance(obj, dict) and key not in obj:
            obj[key] = [] if next_is_int else {}
        elif isinstance(obj, list) and isinstance(key, int) and key in (-1, len(obj)):
            key = len(obj)
            obj.append([] if next_is_int else {})
        obj = obj[key]
    key = update[i]
    value = update[i + 1]
    if isinstance(obj, list):
        if key == -1:
            obj.append(value)
        elif key == -2:
            obj.extend(value)
        elif key >= len(obj):
            obj.append(value)
        elif value is None:
            obj.pop(key)
        else:
            obj[key] = value
    elif value is None:
        obj.pop(key, None)
    else:
        obj[key] = value


def parse_version(version: str | None) -> tuple[int, ...]:
    """Parse ``8.5.6`` into a tuple."""
    try:
        return tuple(int(p) for p in str(version).split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)


def iso_now_ms() -> str:
    """``new Date().toISOString()``."""
    now = dt.datetime.now(dt.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# --------------------------------------------------------------- REST API
@dataclass
class AccountCredentials:
    """What we keep after login (the passphrase itself is discarded)."""

    email: str
    sid: str
    private_key_b64: str
    account: dict[str, Any] = field(default_factory=dict)

    @property
    def account_id(self) -> str:
        """F@h account id (hash of the account public key)."""
        return fc.pubkey_id(fc.public_from_b64(self.account["pubkey"]))


class FahAccountApi:
    """Calls to api.foldingathome.org."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str | None = None) -> None:
        self._session = session
        self.base_url = (base_url or API_URL).rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        sid: str | None = None,
        params: dict[str, str] | None = None,
        data: Any = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if sid:
            headers["Authorization"] = sid
        try:
            async with asyncio.timeout(30):
                resp = await self._session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    json=data if data is not None else None,
                    headers=headers,
                )
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            # Never include the URL: it can carry the password hash
            raise FahConnectionError(type(err).__name__) from err
        if resp.status in (401, 403):
            raise FahAuthError(f"HTTP {resp.status}")
        if resp.status >= 400:
            msg = body.get("error") if isinstance(body, dict) else None
            raise FahApiError(f"HTTP {resp.status}: {msg or resp.reason}")
        return body

    async def login(self, email: str, passphrase: str) -> AccountCredentials:
        """Log in and unlock the account key."""
        email = email.strip()
        salt = fc.login_salt(email)
        # PBKDF2 with 100k iterations: keep it off the event loop
        key = await asyncio.get_running_loop().run_in_executor(
            None, fc.derive_key, passphrase, salt
        )
        password = fc.password_hash(key)
        try:
            login = await self._request("GET", "/login", params={"email": email, "password": password})
        except FahApiError as err:
            # The API answers bad credentials with 4xx + error text
            raise FahAuthError(str(err)) from err
        sid = login.get("id") if isinstance(login, dict) else None
        if not sid:
            raise FahAuthError("Login did not return a session")
        secret = await self._request("GET", "/account/secret", sid=sid, params={"password": password})
        if not isinstance(secret, dict) or not secret.get("secret"):
            raise FahAuthError(
                "This account has no stored key yet. Log in once at app.foldingathome.org first."
            )
        private = fc.unwrap_secret(secret["secret"], key, salt)
        account = await self.get_account(sid)
        creds = AccountCredentials(
            email=email, sid=sid, private_key_b64=fc.private_to_b64(private), account=account
        )
        # Sanity check: unlocked key belongs to this account
        if fc.public_to_b64(private.public_key()) != fc.public_to_b64(
            fc.public_from_b64(account["pubkey"])
        ):
            raise FahAuthError("Account key does not match the account")
        return creds

    async def get_account(self, sid: str) -> dict[str, Any]:
        """Account data (pubkey, node, machines, user, team ...)."""
        account = await self._request("GET", "/account", sid=sid)
        if not isinstance(account, dict) or not account.get("pubkey"):
            raise FahAuthError("Not logged in")
        if isinstance(account.get("config"), str):
            try:
                account["config"] = json.loads(account["config"])
            except ValueError:
                pass
        return account

    async def put_account(self, sid: str, data: dict[str, Any]) -> None:
        """Save account settings (user / team / passkey / cause ...)."""
        await self._request("PUT", "/account", sid=sid, data=data)

    async def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        """Public endpoints (stats, project info)."""
        return await self._request("GET", path, params=params)


def valid_node_host(host: Any) -> bool:
    """True for a plain host name (optionally with a port)."""
    return isinstance(host, str) and bool(NODE_HOST_RE.match(host))


def check_stored_key(private_key_b64: str, account_pubkey_b64: str) -> None:
    """Raise FahAuthError unless the stored key matches the account."""
    try:
        private = fc.private_from_b64(private_key_b64)
        public = fc.public_from_b64(account_pubkey_b64)
    except Exception as err:  # noqa: BLE001
        raise FahAuthError("Stored account key is unreadable") from err
    if fc.public_to_b64(private.public_key()) != fc.public_to_b64(public):
        raise FahAuthError("Stored account key does not match the account")


# ---------------------------------------------------------- machine session
class FahMachine:
    """One machine reached through the node. Holds its live client state."""

    def __init__(self, node: FahNode, machine_id: str, name: str | None = None) -> None:
        self._node = node
        self.id = machine_id
        self.name = name
        self.key: bytes | None = None  # AES session key from the machine
        self.state: dict[str, Any] = {}
        self.online = False  # machine connected to the node
        self.ready = False  # snapshot received
        self._ivs: set[str] = set()
        self._last_cmd_time: dict[str, dt.datetime] = {}

    # ---- properties
    @property
    def connected(self) -> bool:
        """Usable: online and snapshot received."""
        return self.online and self.ready and self._node.connected

    @property
    def groups(self) -> list[str]:
        """Resource group names ("" is the default group)."""
        return list(self.state.get("groups", {}).keys())

    @property
    def version(self) -> tuple[int, ...]:
        """Client version."""
        return parse_version(self.state.get("info", {}).get("version"))

    # ---- transport
    def _reset(self) -> None:
        self.ready = False
        self._ivs.clear()

    async def _open(self, key: bytes) -> None:
        self._reset()
        self.key = key
        self.online = True
        await self._send_encrypted({"type": "session-open", "session": self._node.session_id})

    async def _send_encrypted(self, inner: dict[str, Any]) -> None:
        if self.key is None:
            raise FahConnectionError(f"No session with machine {self.name or self.id}")
        iv, payload = fc.encrypt_message(self.key, inner)
        self._ivs.add(iv)
        await self._node.send({"type": "message", "id": self.id, "iv": iv, "payload": payload})

    def _receive(self, msg: dict[str, Any]) -> Any:
        """Decrypt a message from the machine; returns its content."""
        if self.key is None:
            raise fc.FahCryptoError("No session key")
        iv = msg.get("iv", "")
        if iv in self._ivs:
            raise fc.FahCryptoError("IV reused")
        self._ivs.add(iv)
        if len(self._ivs) > 1_000_000:
            self._ivs.clear()
        inner = fc.decrypt_message(self.key, iv, msg.get("payload", ""), msg.get("compression"))
        if inner.get("session") != self._node.session_id:
            raise fc.FahCryptoError("Message is not for this session")
        return inner.get("content")

    # ---- commands (same as the local API)
    def _next_time(self, cmd: str) -> str:
        """Monotonic per command; the client drops changes not newer (1s res.)."""
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        last = self._last_cmd_time.get(cmd)
        if last is not None and now <= last:
            now = last + dt.timedelta(seconds=1)
        self._last_cmd_time[cmd] = now
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def send_command(self, cmd: str, **fields: Any) -> None:
        """Send a client command through the encrypted session."""
        if not self.online or self.key is None or not self._node.connected:
            raise FahConnectionError(f"{self.name or self.id} is offline")
        content = {"cmd": cmd, **fields, "time": self._next_time(cmd)}
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("F@h %s -> %s", self.name or self.id, redact(content))
        await self._send_encrypted(
            {"type": "message", "session": self._node.session_id, "content": content}
        )

    async def set_state(self, state: str, group: str | None = None) -> None:
        """fold / pause / finish."""
        if state not in ("fold", "pause", "finish"):
            raise ValueError(state)
        fields: dict[str, Any] = {"state": state}
        if group is not None:
            if group not in self.groups:
                raise ValueError(f"Unknown group '{group}'")
            fields["group"] = group
        await self.send_command("state", **fields)

    async def configure_global(self, **values: Any) -> None:
        """user / team / passkey / cause."""
        allowed = {"user", "team", "passkey", "cause"}
        config = {k: v for k, v in values.items() if k in allowed and v is not None}
        if config:
            await self.send_command("config", config=config)

    async def configure_group(self, group: str, **values: Any) -> None:
        """Group options. Every group must be listed or the client deletes it."""
        if group not in self.groups:
            raise ValueError(f"Unknown group '{group}'")
        groups: dict[str, Any] = {g: {} for g in self.groups}
        groups[group] = {k: v for k, v in values.items() if v is not None}
        await self.send_command("config", config={"groups": groups})


    async def configure_gpu(self, gpu_id: str, enabled: bool) -> None:
        """Enable/disable one GPU (like the web app's GPU table).

        A group's ``gpus`` setting is replaced as a whole, so the full map for
        that group is sent. A GPU being enabled that is not in any group goes
        to the default group "".
        """
        gpus_info = self.state.get("info", {}).get("gpus") or {}
        if gpu_id not in gpus_info:
            raise ValueError(f"Unknown GPU '{gpu_id}'")
        groups_state = self.state.get("groups") or {}
        owner = None
        for name, group in groups_state.items():
            if ((group.get("config") or {}).get("gpus") or {}).get(gpu_id, {}).get("enabled"):
                owner = name
                break
        target = owner if owner is not None else ("" if "" in groups_state else next(iter(groups_state), ""))
        current = ((groups_state.get(target) or {}).get("config") or {}).get("gpus") or {}
        new_map = {gid: {"enabled": bool((current.get(gid) or {}).get("enabled"))} for gid in gpus_info}
        for gid, conf in current.items():  # keep undetected-but-enabled GPUs, as the web app does
            if gid not in new_map and (conf or {}).get("enabled"):
                new_map[gid] = {"enabled": True}
        new_map[gpu_id] = {"enabled": bool(enabled)}
        groups: dict[str, Any] = {g: {} for g in self.groups}
        groups[target] = {"gpus": new_map}
        await self.send_command("config", config={"groups": groups})


# --------------------------------------------------------------- the node
class FahNode:
    """Account WebSocket to the F@h node, multiplexing all machines."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        node_host: str,
        account_pubkey_b64: str,
        private_key_b64: str,
        *,
        on_machine_online: Callable[[FahMachine], None] | None = None,
        on_machine_offline: Callable[[FahMachine], None] | None = None,
        on_machine_content: Callable[[FahMachine, Any], None] | None = None,
        on_broadcast: Callable[[dict[str, Any]], None] | None = None,
        on_connection_change: Callable[[bool], None] | None = None,
    ) -> None:
        if not valid_node_host(node_host):
            raise FahApiError("Invalid node host name")
        self._session = session
        self.node_host = node_host
        self.account_pubkey_b64 = account_pubkey_b64
        self._private = fc.private_from_b64(private_key_b64)
        self._account_pub = fc.public_from_b64(account_pubkey_b64)
        self.account_id = fc.pubkey_id(self._account_pub)
        if fc.public_to_b64(self._private.public_key()) != fc.public_to_b64(self._account_pub):
            raise FahAuthError("Stored account key does not match the account")
        self._on_online = on_machine_online
        self._on_offline = on_machine_offline
        self._on_content = on_machine_content
        self._on_broadcast = on_broadcast
        self._on_connection_change = on_connection_change

        self.machines: dict[str, FahMachine] = {}
        self.session_id = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._connected = asyncio.Event()
        self.last_error: str | None = None

    @property
    def url(self) -> str:
        """Node WebSocket URL."""
        return f"{NODE_SCHEME}://{self.node_host}/ws/account"

    @property
    def connected(self) -> bool:
        """Logged in to the node."""
        return self._ws is not None and not self._ws.closed and self._connected.is_set()

    def machine(self, machine_id: str, name: str | None = None) -> FahMachine:
        """Get or create a machine object."""
        mach = self.machines.get(machine_id)
        if mach is None:
            mach = self.machines[machine_id] = FahMachine(self, machine_id, name)
        elif name:
            mach.name = name
        return mach

    # ---- lifecycle
    def start(self) -> None:
        """Start the connection loop."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="fah-node")

    async def stop(self) -> None:
        """Stop and close."""
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def restart(self, node_host: str | None = None) -> None:
        """Reconnect (e.g. the account's node changed)."""
        if node_host:
            if not valid_node_host(node_host):
                raise FahApiError("Invalid node host name")
            self.node_host = node_host
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def wait_connected(self, timeout: float) -> bool:
        """Wait for the node login."""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while not self._stopping:
            try:
                await self._connect_and_listen()
                delay = RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.last_error = str(err) or type(err).__name__
                _LOGGER.debug("F@h node %s error: %s", self.node_host, self.last_error)
            finally:
                was = self._connected.is_set()
                self._ws = None
                self._connected.clear()
                for mach in self.machines.values():
                    if mach.online:
                        mach.online = False
                        mach._reset()  # noqa: SLF001
                        if self._on_offline:
                            self._on_offline(mach)
                if was and self._on_connection_change:
                    self._on_connection_change(False)
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _connect_and_listen(self) -> None:
        ws = await asyncio.wait_for(
            self._session.ws_connect(self.url, heartbeat=HEARTBEAT, max_msg_size=MAX_MESSAGE_SIZE),
            CONNECT_TIMEOUT,
        )
        async with ws:
            self._ws = ws
            self.session_id = fc.urlb64encode(fc.random_bytes(12))
            payload = {"time": iso_now_ms(), "session": self.session_id}
            await ws.send_str(
                json.dumps(
                    {
                        "type": "login",
                        "payload": payload,
                        "pubkey": self.account_pubkey_b64,
                        "signature": fc.sign(self._private, fc.compact_json(payload)),
                    }
                )
            )
            # The node gives no explicit login ack; being able to talk is it
            self._connected.set()
            self.last_error = None
            _LOGGER.info("Connected to Folding@home node %s", self.node_host)
            if self._on_connection_change:
                self._on_connection_change(True)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle(json.loads(msg.data))
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("F@h node message ignored: %s", err)
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
            if ws.close_code and ws.close_code not in (1000, 1001):
                self.last_error = f"closed with code {ws.close_code}"
            _LOGGER.info("Disconnected from Folding@home node %s", self.node_host)

    async def send(self, msg: dict[str, Any]) -> None:
        """Send a raw node message."""
        if self._ws is None or self._ws.closed:
            raise FahConnectionError("Not connected to the Folding@home node")
        await self._ws.send_str(json.dumps(msg))

    # ---- incoming
    async def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "connect":
            await self._machine_connect(msg.get("client") or {})
        elif mtype == "disconnect":
            mach = self.machines.get(msg.get("id", ""))
            if mach is not None and mach.online:
                mach.online = False
                mach._reset()  # noqa: SLF001
                if self._on_offline:
                    self._on_offline(mach)
        elif mtype == "message":
            mach = self.machines.get(msg.get("client", ""))
            if mach is None:
                return
            content = mach._receive(msg)  # noqa: SLF001
            if isinstance(content, dict) and "info" in content:
                mach.state = content  # full snapshot
                first = not mach.ready
                mach.ready = True
                if first and self._on_online:
                    self._on_online(mach)
            elif isinstance(content, list):
                if not mach.ready:
                    return
                try:
                    apply_update(mach.state, content)
                except (KeyError, IndexError, TypeError):
                    return
            if self._on_content:
                self._on_content(mach, content)
        elif mtype == "broadcast":
            payload = msg.get("payload") or {}
            if not fc.verify(self._account_pub, msg.get("signature", ""), fc.compact_json(payload)):
                return
            if self._on_broadcast:
                self._on_broadcast(payload)

    async def _machine_connect(self, client: dict[str, Any]) -> None:
        """A machine logged in to the node: check it and open a session."""
        pub = fc.public_from_b64(client["pubkey"])
        machine_id = fc.pubkey_id(pub)
        payload = client.get("payload") or {}
        if not fc.verify(pub, client.get("signature", ""), fc.compact_json(payload)):
            _LOGGER.warning("Ignoring machine %s: invalid signature", machine_id[:8])
            return
        if payload.get("account") != self.account_id:
            _LOGGER.warning("Ignoring machine %s: linked to another account", machine_id[:8])
            return
        key = fc.rsa_decrypt(self._private, fc.b64decode(payload["key"]))
        if len(key) != 32:
            _LOGGER.warning("Ignoring machine %s: bad key length", machine_id[:8])
            return
        mach = self.machine(machine_id)
        _LOGGER.debug("Machine %s (%s) online", mach.name, machine_id[:8])
        await mach._open(key)  # noqa: SLF001
        # Ask for the work-unit history (project tracking)
        try:
            await mach.send_command("wus", enable=True)
        except FahConnectionError:
            pass
