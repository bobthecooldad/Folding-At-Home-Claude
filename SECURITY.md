# Security review

Scope: `custom_components/foldingathome` v1.1.0 and `tools/fah_account_check.py`. Reviewed on 2026-10-25 by reading the code, running the bandit scanner, and running the test suite (29 tests, including hostile-input cases).

## Network exposure

| Direction | What | Notes |
|---|---|---|
| **Inbound** | **Nothing.** | The integration opens no ports, registers no HA web views, webhooks or HTTP endpoints, and needs no port forwarding. |
| Outbound | `https://api.foldingathome.org` | Login, account settings, donor stats, project descriptions. TLS certificates are verified (HA's default HTTP session). |
| Outbound | `wss://<your node>/ws/account` | Account WebSocket to the F@h node. TLS verified. The node host name comes from your account and must be a plain host name; anything with a path, credentials or query is refused. |

Nothing on your LAN is contacted. The machines also only make outbound connections to the node, so the F@h clients no longer need to listen on the network.

## Who can control your machines

* **Through the node:** every message to or from a machine is AES-256 encrypted with a session key that only the machine and the holder of your account key know. The node relays the messages but can't read or forge them.
* **Machine announcements** (`connect`) are accepted only when the machine's RSA signature is valid and the message names your account id. Forged ones, and ones for other accounts, are logged and ignored (tested).
* **Account broadcasts** are acted on only if they carry a valid signature from your account key, and all they can trigger is an account refresh.
* **Replays** are blocked: each message IV may be used only once, and messages must carry this session's id.
* **Data from the network is treated as data:** only JSON is parsed. There is no `eval`, shell, file access or dynamic import. Project ids are only used in URLs if they are all digits, and the user name is URL-encoded.
* **Resource limits:** WebSocket messages are capped at 32 MB and gzip output at 64 MB, so a "zip bomb" from the network is refused (tested).
* **Inside HA:** anyone who can use your Home Assistant can press Fold/Pause or change the user name and team, like any other HA entity. Protect HA itself (see below).

## Secrets

| Secret | Where it lives |
|---|---|
| Account **passphrase** | Never stored. Used once (in memory) to derive the login hash and unlock the account key. |
| **Account private key** + **API session id** | Stored in the config entry (`/config/.storage/core.config_entries`), the same as the web app keeps them in browser storage. They are never logged, never placed in entity states or attributes, and redacted from diagnostics (tested). |
| **Passkey** | Stored only in your F@h account (and on the machines). It can't be set through an entity or action, so it never lands in HA's history database (tested). |
| Login hash | Sent to `api.foldingathome.org` only, over HTTPS. This is the F@h API's own scheme. |

The key and session in `.storage` are as sensitive as being logged in to app.foldingathome.org. Anyone holding them can control your machines and change account settings. So:

* Keep HA backups encrypted (the default since HA 2025.1) and don't share the `.storage` folder.
* If you think they leaked: change your passphrase at app.foldingathome.org (this replaces the account key), then use **Re-authenticate** in HA.

## Scanner results

`bandit -r custom_components tools`: **0 high, 0 medium**, and 6 low-severity notes. Those are 4 `assert` statements on internal invariants and 2 deliberate `try/except: pass` around best-effort refreshes. No third-party Python packages are added; the integration only uses what HA itself ships (`aiohttp`, `cryptography`).

## Recommendations for your setup

1. **Don't expose Home Assistant directly to the internet.** No port forward on 8123. Use Nabu Casa, a VPN (WireGuard/Tailscale) or a properly configured reverse proxy.
2. Turn on **multi-factor authentication** for HA users (Profile → Multi-factor authentication).
3. If you followed the earlier LAN-mode instructions, **undo them on each F@h machine**: remove the `<http-addresses v="0.0.0.0:7396"/>` and `<allow …/>` lines from `config.xml` and close firewall port 7396. The local API has no password, and it's no longer needed.
4. Keep your account passphrase long. The web app's generated BIP39 passphrase is a good choice.
5. Never commit credentials to the GitHub repo. `.gitignore` excludes `*Passkey*.txt`. The keys in `tests/webcrypto/vectors.json` and the test mock are synthetic test data and don't belong to any real account.
