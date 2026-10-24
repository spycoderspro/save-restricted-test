# Save Restricted Test Bot

A private file-saving bot using **your existing Telegram library**:
[spycoderspro/telegram-lib](https://github.com/spycoderspro/telegram-lib).
`requirements.txt` installs `easyproto-actions[fast]` directly from that repository,
pinned to commit `80dcb34b0d39e11f1139bb297b6c8fc8986993d9`. No EasyProto source,
Telethon, or Pyrogram client implementation is copied into this repository.

Owners can send `/save <Telegram message link>` or a message link by itself.
The user account downloads the file from a configured source chat; the bot sends
it to that owner's private bot conversation. Only configured owners and source
chat IDs are accepted. Protected-content chats/messages, disappearing media and
files over the configured limit are refused. It does not bypass Telegram controls.

`add_premium.py` is your original, unchanged legacy plugin. It is not imported by
the new bot: its `config`, `shared_client` and `utils.func` modules are absent,
and it requires Pyrogram. Premium commands are not implemented by this app.

## Install

Python 3.10+ is required; Python 3.11+ enables the library's native crypto extra.
The current cloud machine uses Python 3.12.14 and uv:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

For development/testing, install `requirements-dev.txt` instead. Updating your
library means deliberately updating the commit pin in `requirements.txt` and
rerunning the tests. No modifications to `telegram-lib` are needed for this bot.

## Heroku testing

[Deploy this branch to Heroku](https://heroku.com/deploy?template=https://github.com/spycoderspro/save-restricted-test/tree/feat/save-restricted-bot)

See [HEROKU.md](HEROKU.md) for Config Vars and worker startup. Heroku uses Python
3.12 and one worker dyno; no web server is needed. No manual session is required:
start the bot and use `/login` in your private conversation. Optional session
restoration is still supported for dyno replacement.

## Configure and run

Copy `.env.example` to a private `.env` (mode 0600), enter values locally, and
export them into your process environment. The app does not auto-load `.env`.
Never commit real credentials or session files, print their values, or send them
in chat. For a shell-based development run after you have edited `.env`:

```sh
set -a
. ./.env
set +a
.venv/bin/python bot.py --check
.venv/bin/python bot.py --preflight
.venv/bin/python bot.py
```

After startup, open your own bot from a configured owner account and send `/login`.
Follow its phone, code, and optional 2FA prompts, then use `/save`. `/loginstatus`
shows whether the shared source account is ready; `/cancel` stops pending login.

Required configuration:

| Variable | Purpose |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | Your Telegram API application, read by EasyProto |
| `TG_BOT_TOKEN` | Token for your own bot, obtained from BotFather |
| `OWNER_IDS` | Comma-separated positive user IDs allowed to use the bot |
| `SOURCE_CHAT_IDS` | Comma-separated signed numeric IDs of authorized source chats |
| `SESSION_DIR` | Private directory for automatically saved user and bot sessions; example provided |
| `LOGIN_OWNER_ID` | Owner allowed to authorize the shared source account; defaults to the smallest owner ID |
| `LOGIN_TIMEOUT_SECONDS` | Total login timeout, 30–900 seconds; default 300 |
| `TRANSFER_WORKERS` | 1, 2 or 4 chunk workers; default 1 |
| `MAX_CONCURRENT_TRANSFERS` | 1–4 whole-file jobs; default 1, one job per owner |
| `MAX_FILE_BYTES` | Maximum source file size; default 1,999,999,999 bytes (strictly less than 2 GB) |

`TG_SESSION` and `TG_BOT_SESSION` optionally override individual paths. Sessions
must be distinct, outside the checkout, with parent-directory mode 0700. They
contain authorization keys, not portable Telethon session strings. The user
account must already have access to each allowed chat. No chat is joined automatically.

The signed channel/supergroup convention is your library's
`-1000000000000 - channel_id`. For example, `https://t.me/c/7/123` selects source
ID `-1000000000007`, message 123. Public username links resolve to a peer and then
must match a numeric ID in `SOURCE_CHAT_IDS`. Startup loads up to 1,000 dialogs
to rebuild private-chat access hashes; private sources outside that cache may
need a public username or additional resolution support.

`--check` validates configuration without contacting Telegram. `--preflight`
checks direct TCP only and does not establish protocol/account compatibility.
`--login` is an explicit terminal login for your user account, with hidden
phone/code/2FA input. The bot can start without an authorized user
session and accept `/login`; an existing valid native session is reused automatically.
On Heroku, use `/login` again after a dyno replacement unless optional session
restoration is configured. No manual session variable is required. The bot authenticates using EasyProto's raw `auth.importBotAuthorization`;
the small adapter uses the pinned library's private `_open`/`_authorized` hooks
because that version has no public bot-token login convenience method.

Login messages are deleted on a best-effort basis and never echoed or logged.
A bot conversation is not a Secret Chat; use only your own trusted deployment.
Phone/code/password input belongs in that app, never this coding chat. Telegram
may invalidate codes shared in chats; the app does not circumvent that protection.
The direct terminal `bot.py --login` option remains available on a trusted host.

## Commands and behavior

- `/start` or `/help`: usage, in an owner's private bot conversation.
- `/login`: short-lived phone/code/2FA flow for `LOGIN_OWNER_ID` only.
- `/loginstatus`: whether the shared source account is ready.
- `/save https://t.me/channel/123` or `https://t.me/c/7/123`: save one permitted
  document or photo from an allowed source. Topic/album/invite links are unsupported.
- `/cancel`: stop pending login or cancel your active job and drain workers.

Transfers run in bounded background tasks so the command listener remains free
for cancellation. Photos are sent as documents to preserve the downloaded bytes.
The bot downloads the delivered message again and checks SHA-256 before reporting
success. This verifies the downloaded-to-delivered round trip; compare the receipt
hash with a known original file if original-source integrity is required.

Local temporary files are cleaned after success, failure or cancellation. A file
already delivered to Telegram is not automatically deleted when a later step
fails or is cancelled. Inspect the private chat before retrying an uncertain send.
FloodWait stops the job and starts a cooldown; requests are not blindly replayed.
Progress is a start notice and completion receipt; the library has no byte-level
progress callback API. Disconnections or update gaps stop the listener explicitly;
there is no durable queue, automatic replay, or promise of exactly-once delivery.

JSON logs contain transfer size, timing, decimal MB/s, process CPU/peak RSS, status
and exception class, without credentials, links, peer IDs, file names or message
contents. Download timing includes source resolution and hashing; upload timing
includes sending the document. Peak RSS is a process lifetime value, not isolated
per-job memory. Application retries are zero; uninstrumented internal library
migration retries are reported as null. Real Telegram speed remains unmeasured.

## Validation

```sh
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

**71 offline tests pass**, exercising the installed external EasyProto Client,
mocked RPCs, 1/2/4 chunk workers, SHA-256, source/protection checks, cancellation,
FloodWait, owner-only commands, bot-login request construction and session isolation.
Additional login/hosting tests cover phone/code methods, 2FA callbacks, owner-only
inputs, cancellation, expiry, native session export/restore, and SIGTERM cleanup. These are local simulations, not successful
live Telegram operations or a Heroku deployment.

This cloud machine refuses direct Telegram MTProto TCP, and live credentials are
absent. Real login, bot updates, file transfer and full bot behavior remain
**unverified**. See [VALIDATION.md](VALIDATION.md). The two previously identified
upstream session/RPC defects remain in the pinned library; they were not silently
patched or vendored into this bot repository.
