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

The initial bot suite passed **33 tests**. Tests use the actual installed external Client and
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
and source lists, and reachable Telegram TCP. The bot can start without an
authorized source user session and accept owner-only `/login`. Offline setup does not establish live or production readiness.

## Heroku testing configuration

`Procfile` starts one worker with `python -u bot.py`; `app.json` declares the
Python buildpack, Heroku-24, private configuration requirements, and one Basic
worker. `.python-version` selects supported Python 3.12. No web server, external
Telegram client, or copied EasyProto implementation was added.

The hosting changes brought the suite to **50 offline tests**. Hosting tests exercise private
native-session export/restore from Config Vars, corruption/size/auth checks,
preserving newer local state and other accounts, live lock exclusion, fresh-dyno
requirements, and graceful SIGTERM cleanup. Production requirements and CLI help
were also checked in a clean Python 3.12 environment with pip.

No Heroku app/build/dyno or live Telegram operation was validated. The Heroku CLI
is not installed here and no Heroku account credentials were used. Session
restoration retains authorization across dyno replacement; it does not persist
transfers, replay missed updates, or fix the external library limitations above.
See HEROKU.md for user-controlled deployment and testing.

## Owner login feature

The current suite passes **71 offline tests**. The bot starts before source-user
authorization, offers `/login`, `/loginstatus`, and `/cancel`, and restricts a
single short-lived login to a configured login owner in a private conversation.
The flow calls the external library's existing `Client.start` and its code/2FA
callbacks; no Telegram library source or new runtime dependency was added.

New tests cover real installed Client phone/code method construction with mocked
RPC responses, native 2FA callback routing, unauthorized/group/other-operator
rejection, configurable login owner, no duplicate login, invalid input, expiry,
cancellation, secret-free errors, best-effort input deletion, FloodWait abort and
starting with an absent or expired user session. No live login was performed.

`TG_USER_SESSION_B64` is now optional in app.json, and startup no longer requires
a manual session seed. Successful login saves the native private session locally.
Heroku dyno replacement discards local files, so a new `/login` is required unless
the existing optional seed mechanism is used. Login does not create a new durable
external session store or alter Heroku Config Vars automatically.

Inputs are not logged or echoed and are deleted best effort, not guaranteed
erased from Telegram cloud chats. Telegram may invalidate codes sent in chats;
there is no obfuscation or bypass of that safeguard. Native direct terminal login
remains an alternative. Additional email/paid verification flows remain unsupported
upstream. A cancelled flow does not revoke an already accepted sign-in.
