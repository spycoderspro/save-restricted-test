# Bot validation, 2026-10-24

The application is in `spycoderspro/save-restricted-test`, branch
`feat/save-restricted-bot`. The original `add_premium.py` is unchanged.
Only bot code, configuration, dependency declarations, documentation and bot tests
were added. The attempted EasyProto source import was removed before any target
commit or push. No library source or old release/session artifact is included.

`requirements.txt` installs the user's external `easyproto-actions[fast]` from
`https://github.com/spycoderspro/telegram-lib.git` at
`80dcb34b0d39e11f1139bb297b6c8fc8986993d9`. Its dependency metadata reports
PyCryptodome 4.1.0 and cryptg 0.6.0. No Telethon or Pyrogram runtime is installed.
The earlier changes in the library repository were reverted and the revert pushed:
`b1428e57193ba71cc6850075b930465bcd360308`. Library files match their original
`80dcb34` state; the separate library's `main` branch was never changed.

## Offline evidence

Python 3.12.14, pytest 9.1.1, pytest-asyncio 1.4.0. Commands from this repository:

```sh
UV_CACHE_DIR=/workspace/.cache/uv uv venv .venv
UV_CACHE_DIR=/workspace/.cache/uv uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python bot.py --help
.venv/bin/python bot.py --check
.venv/bin/python bot.py --preflight
```

The suite passed **33 tests**. Tests use the actual installed external Client and
media pipeline with simulated RPCs. They cover message-link parsing, authorized
source matching, refreshed protected-chat flags, expiring/protected/wrong-chat
refusal, 1/2/4 chunk workers, delivered-file SHA-256, corruption detection,
FloodWait abort without retry, cancellation/temporary cleanup, owner/private-only
commands, responsive cancellation, bounded jobs, shutdown, raw bot authorization
and stored bot identity matching, secret-free settings representation, and distinct
session paths outside the checkout.

`--help` runs successfully. `--check` fails with the expected missing-variable
names in this unconfigured instance. `--preflight` fails with
`ConnectionRefusedError` before MTProto authentication. These are unmet runtime
prerequisites, not passing live tests. No live test is counted among the 33 tests.

The default file limit is 1,999,999,999 bytes, strictly less than 2 GB (decimal).
Metadata-only boundary checks accept that size and reject 2,000,000,000 bytes
and above; environment configuration cannot raise the limit to 2 GB or higher.
These checks allocate no large files and do not verify a live 2 GB transfer.

## Still unverified or incomplete

No real user login/2FA, bot-token login, bot update, message save, DC migration,
reconnection, throughput, CPU/RSS or FloodWait frequency was measured against
Telegram. Credentials were neither used nor requested in chat. The library's
bot-login adapter uses private hooks; raw request construction is tested offline,
not established as compatible with Telegram servers. Direct IPv4 MTProto TCP to
Telegram DCs on port 443 is required; HTTPS proxy allowlists do not supply it.

The upstream library has no automatic reconnect/update-gap recovery, CDN download,
expired file-reference refresh, or byte-level progress API. Two verified upstream
issues from the prior investigation (stale session snapshot before lock acquisition
and malformed compressed RPC response leaving its future pending) remain in the
external pin because the user requested that library changes be undone. This bot
constructs clients immediately before opening and does not automatically retry
uncertain mutations; this does not fix or conceal those upstream limitations.

The original premium handler cannot run alone: its Pyrogram dependency and local
`config`, `shared_client`, and `utils.func` implementations are absent. It was not
rewritten or connected to the new bot. No protected-content bypass is implemented.

Runtime startup requires securely configured API credentials, bot token, owner
and source lists, a separately authorized private user session, and reachable
Telegram TCP. Offline setup does not establish live or production readiness.
