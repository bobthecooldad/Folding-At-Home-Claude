"""Fake Folding@home cloud: REST API + node + machines.

The machine side mirrors ``fah-client-bastet/src/fah/client/Account.cpp``
(login with an AES session key RSA-OAEP encrypted to the account key,
signed with the machine key; encrypted ``session-open`` / ``message``
handling; gzip above 10 kB) and ``Remote.cpp`` / ``App.cpp`` (snapshot on
open, list patches, ``state`` / ``config`` / ``wus`` commands, the
strictly-newer ``time`` rule per command).
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import gzip
import json
from typing import Any

from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa

from custom_components.foldingathome import fahcrypto as fc

from .fah_mock import make_state, wu_record

EMAIL = "Donor@Example.com"
PASSPHRASE = "purple monkey dishwasher 42"
PASSKEY = "0123456789abcdef0123456789abcdef"


def _gen_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class MockMachine:
    """A linked machine."""

    def __init__(self, cloud: MockCloud, name: str, gpu: bool = True) -> None:
        self.cloud = cloud
        self.key = _gen_key()
        self.id = fc.pubkey_id(self.key.public_key())
        self.name = name
        self.state = make_state()
        self.state["info"]["id"] = self.id
        self.state["info"]["mach_name"] = name
        self.state["info"]["account"] = cloud.account_id
        if not gpu:
            self.state["units"] = self.state["units"][:1]
            self.state["info"]["gpus"] = {}
        self.history = [wu_record(f"{name}-old-1", 18251), wu_record(f"{name}-old-2", 17000, "failed")]
        self.session_key = b""
        self.sessions: set[str] = set()
        self.received: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self._last_time: dict[str, int] = {}
        self.online = False
        self.big_snapshot = False

    # ---- Account.cpp onOpen(): login message
    def login_message(self) -> dict[str, Any]:
        self.session_key = fc.random_bytes(32)
        enc = self.cloud.account_key.public_key().encrypt(
            self.session_key,
            asym_padding.OAEP(mgf=asym_padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        payload = {
            "time": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "account": self.cloud.account_id,
            "key": base64.b64encode(enc).decode(),
        }
        sig = self.key.sign(fc.compact_json(payload).encode(), asym_padding.PKCS1v15(), hashes.SHA256())
        return {
            "type": "login",
            "payload": payload,
            # cbang signBase64SHA256 -> standard base64
            "signature": base64.b64encode(sig).decode(),
            "pubkey": fc.public_to_b64(self.key.public_key()),
        }

    # ---- Account::sendEncrypted
    async def send(self, sid: str, content: Any) -> None:
        payload = fc.compact_json({"session": sid, "content": content}).encode()
        msg: dict[str, Any] = {}
        if len(payload) > 10000:
            msg["compression"] = "gzip"
            payload = gzip.compress(payload)
        iv = fc.random_bytes(16)
        msg.update(
            {
                "type": "message",
                "client": self.id,
                "session": sid,
                "iv": fc.urlb64encode(iv),
                "payload": fc.urlb64encode(fc.aes_encrypt(self.session_key, iv, payload)),
            }
        )
        await self.cloud.to_account(msg)

    async def broadcast(self, update: list[Any]) -> None:
        for sid in list(self.sessions):
            await self.send(sid, update)

    async def complete_wu(self, uid: str, project: int) -> None:
        rec = wu_record(uid, project, end=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.history.append(rec)
        await self.broadcast(["wus", -2, [rec]])

    # ---- Account::onEncrypted
    async def on_encrypted(self, msg: dict[str, Any]) -> None:
        inner = fc.decrypt_message(self.session_key, msg["iv"], msg["payload"], None)
        sid = inner["session"]
        if inner["type"] == "session-open":
            self.sessions.add(sid)
            snap = copy.deepcopy(self.state)
            if self.big_snapshot:
                snap["log"] = ["x" * 100] * 200  # > 10 kB -> gzip
            await self.send(sid, snap)
        elif inner["type"] == "message":
            if sid not in self.sessions:
                raise RuntimeError("Session does not exist")
            content = inner["content"]
            self.received.append(content)
            await self._handle(sid, content)

    def _validate_time(self, msg: dict[str, Any]) -> bool:
        t = dt.datetime.strptime(msg["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
        ts = int(t.timestamp())
        if ts <= self._last_time.get(msg["cmd"], 0):
            return False
        self._last_time[msg["cmd"]] = ts
        return True

    async def _handle(self, sid: str, msg: dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if cmd == "wus":
            if msg.get("enable"):
                await self.send(sid, ["wus", copy.deepcopy(self.history)])
            return
        if cmd in ("state", "config") and not self._validate_time(msg):
            self.rejected.append(msg)
            return
        if cmd == "state":
            groups = [msg["group"]] if "group" in msg else list(self.state["groups"])
            for g in groups:
                conf = self.state["groups"].setdefault(g, {"config": {}})["config"]
                if msg["state"] == "pause":
                    conf["paused"], conf["finish"] = True, False
                elif msg["state"] == "fold":
                    conf["paused"], conf["finish"] = False, False
                elif msg["state"] == "finish":
                    conf["finish"] = True
                await self.broadcast(["groups", g, "config", copy.deepcopy(conf)])
        elif cmd == "config":
            config = msg["config"]
            for key in ("user", "team", "passkey", "cause"):
                if key in config:
                    self.state["config"][key] = config[key]
                    await self.broadcast(["config", key, config[key]])
            if "groups" in config:
                for g in list(self.state["groups"]):
                    if g not in config["groups"]:
                        del self.state["groups"][g]
                        await self.broadcast(["groups", g, None])
                for g, values in config["groups"].items():
                    conf = self.state["groups"].setdefault(g, {"config": {}})["config"]
                    conf.update(values)
                    await self.broadcast(["groups", g, "config", copy.deepcopy(conf)])


class MockCloud:
    """api.foldingathome.org + node in one aiohttp app."""

    def __init__(self) -> None:
        self.account_key = _gen_key()
        self.account_pub_b64 = fc.public_to_b64(self.account_key.public_key())
        self.account_id = fc.pubkey_id(self.account_key.public_key())
        salt = fc.login_salt(EMAIL)
        self._key = fc.derive_key(PASSPHRASE, salt)
        self.password = fc.password_hash(self._key)
        self.secret = fc.wrap_secret(self.account_key, self._key, salt)
        self.sid = "SID-123"
        self.sid_valid = True
        self.project_requests: list[str] = []
        self.missing_projects: set[str] = set()
        self.machines: dict[str, MockMachine] = {}
        self.account_ws: list[web.WebSocketResponse] = []
        self.login_ok: list[dict[str, Any]] = []
        self.puts: list[dict[str, Any]] = []
        self.api_calls: list[str] = []
        self.port = 0
        self.runner: web.AppRunner | None = None
        self.account: dict[str, Any] = {
            "user": "tester",
            "team": 7,
            "passkey": PASSKEY,
            "cause": "any",
            "avatar": "",
            "config": {},
            "email": EMAIL.lower(),
            "created": "2024-01-01",
            "token": "ACCOUNT-TOKEN",
        }

    @property
    def node_host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def add_machine(self, name: str, gpu: bool = True) -> MockMachine:
        m = MockMachine(self, name, gpu)
        self.machines[m.id] = m
        return m

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/login", self._login)
        app.router.add_get("/account/secret", self._secret)
        app.router.add_get("/account", self._get_account)
        app.router.add_put("/account", self._put_account)
        app.router.add_get("/user/{user}", self._user)
        app.router.add_get("/project/{project}", self._project)
        app.router.add_get("/ws/account", self._ws_account)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001

    async def stop(self) -> None:
        for ws in list(self.account_ws):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    # ---- REST
    def _auth(self, request: web.Request) -> bool:
        return self.sid_valid and request.headers.get("Authorization") == self.sid

    async def _login(self, request: web.Request) -> web.Response:
        self.api_calls.append("login")
        q = request.query
        if q.get("email", "").lower() != EMAIL.lower() or q.get("password") != self.password:
            return web.json_response({"error": "Invalid login"}, status=401)
        self.sid_valid = True
        return web.json_response({"id": self.sid})

    async def _secret(self, request: web.Request) -> web.Response:
        if not self._auth(request) or request.query.get("password") != self.password:
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({"secret": self.secret})

    def _account_json(self) -> dict[str, Any]:
        return {
            **self.account,
            "pubkey": self.account_pub_b64,
            "node": self.node_host,
            "machines": [{"id": m.id, "name": m.name} for m in self.machines.values()],
        }

    async def _get_account(self, request: web.Request) -> web.Response:
        self.api_calls.append("account")
        if not self._auth(request):
            return web.json_response({"error": "Not logged in"}, status=401)
        return web.json_response(self._account_json())

    async def _put_account(self, request: web.Request) -> web.Response:
        if not self._auth(request):
            return web.json_response({"error": "Not logged in"}, status=401)
        data = await request.json()
        self.puts.append(data)
        self.account.update({k: data[k] for k in ("user", "team", "passkey", "cause") if k in data})
        return web.json_response({})

    async def _user(self, request: web.Request) -> web.Response:
        if request.match_info["user"] != self.account["user"]:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(
            {
                "name": self.account["user"], "score": 123456, "wus": 42, "rank": 1000,
                "teams": [{"team": self.account["team"], "name": "Team", "tscore": 999999, "trank": 5}],
            }
        )

    async def _project(self, request: web.Request) -> web.Response:
        causes = {"18251": "alzheimers", "12345": "cancer", "17000": "influenza"}
        p = request.match_info["project"]
        self.project_requests.append(p)
        if p in self.missing_projects:
            return web.json_response({"error": "DB:1643: Project not found.", "code": 404}, status=404)
        return web.json_response(
            {"cause": causes.get(p, "unknown"), "manager": "Dr X", "institution": "Uni", "description": "<p>About</p>"}
        )

    # ---- node
    async def to_account(self, msg: dict[str, Any]) -> None:
        for ws in list(self.account_ws):
            if not ws.closed:
                await ws.send_str(json.dumps(msg))

    async def machine_online(self, m: MockMachine) -> None:
        m.online = True
        m.sessions.clear()
        await self.to_account({"type": "connect", "client": m.login_message()})

    async def machine_offline(self, m: MockMachine) -> None:
        m.online = False
        m.sessions.clear()
        await self.to_account({"type": "disconnect", "id": m.id})

    async def _ws_account(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        first = await ws.receive()
        login = json.loads(first.data)
        pub = fc.public_from_b64(login["pubkey"])
        ok = (
            login.get("type") == "login"
            and fc.public_to_b64(pub) == self.account_pub_b64
            and fc.verify(pub, login["signature"], fc.compact_json(login["payload"]))
        )
        if not ok:
            await ws.close(code=4001, message=b"bad login")
            return ws
        self.login_ok.append(login["payload"])
        self.account_ws.append(ws)
        for m in self.machines.values():
            if m.online:
                await ws.send_str(json.dumps({"type": "connect", "client": m.login_message()}))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "message":
                    m = self.machines.get(data.get("id"))
                    if m and m.online:
                        await m.on_encrypted(data)
        finally:
            self.account_ws.remove(ws)
        return ws
