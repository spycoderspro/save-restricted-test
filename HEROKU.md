# Test on Heroku

This repository has a worker-only `Procfile`, `app.json`, and a Python 3.12 pin.
It installs your external `telegram-lib` from `requirements.txt`. No library source
or alternate Telegram client is bundled. Use **one worker dyno** and start with a
small permitted test file. The app manifest selects a Basic worker, which uses
your Heroku account's billing; this repository does not create or charge an app.

## 1. Prepare your authorized EasyProto user session

Do this on a trusted machine that can reach Telegram MTProto, using this repository
and its installed requirements. Configure your API application and the owner/source
IDs as described in README.md. If you do not already have a native EasyProto session:

```sh
.venv/bin/python bot.py --login
```

Then export that existing session into a new private file **outside the checkout**:

```sh
.venv/bin/python session_config.py \
  --session "$TG_SESSION" \
  --output "$HOME/.local/share/save-restricted-test/user-session.b64"
```

If you use `SESSION_DIR` instead of setting `TG_SESSION`, pass the actual user
session path (for example `$HOME/.local/share/save-restricted-test/user`). The
session/export parent directory must be private, mode 0700. The utility creates a
0600 file, refuses to overwrite existing exports, and never prints the value.
Stop a running client before exporting its locked session.

The exported value is a **credential granting access to the Telegram account**.
Base64 is encoding, not encryption. Keep it out of Git, logs, screenshots and chat.
Use its contents only in your private Heroku Config Var `TG_USER_SESSION_B64`.
Telethon/Pyrogram session strings and Telegram test-network sessions are incompatible.

## 2. Deploy the current branch

[Deploy this branch to Heroku](https://heroku.com/deploy?template=https://github.com/spycoderspro/save-restricted-test/tree/feat/save-restricted-bot)

The link deliberately uses `feat/save-restricted-bot`; the repository's `main`
branch has not been merged. Choose an app name and provide these Config Vars in
the Heroku setup form or Dashboard Settings (Reveal Config Vars):

| Config Var | Value |
|---|---|
| `TG_API_ID` | Your Telegram API application ID |
| `TG_API_HASH` | Your private Telegram API hash |
| `TG_BOT_TOKEN` | Your own bot's private token |
| `OWNER_IDS` | Comma-separated numeric user IDs permitted to use the bot |
| `SOURCE_CHAT_IDS` | Comma-separated signed IDs of authorized source chats |
| `TG_USER_SESSION_B64` | Contents of the private session export from step 1 |
| `SESSION_DIR` | `/tmp/save-restricted-test-sessions` (manifest default) |
| `TRANSFER_WORKERS` | `1` initially |
| `MAX_CONCURRENT_TRANSFERS` | `1` initially |
| `MAX_FILE_BYTES` | `1999999999`, strictly less than 2 GB |

`TG_BOT_SESSION_B64` is optional. It accepts the same export format for an existing
native bot session. If absent, the bot token authorizes a new bot session on a fresh
dyno. User and bot sessions must belong to separate identities and use separate
file paths. Do not set a checkout-local session path on Heroku (`/app/...`).

After building, ensure Resources shows **worker = 1**. There is no web process or
HTTP endpoint, so a Heroku app URL is not a bot health check. Do not add a web dyno
or a fake web server to keep this worker running.

Alternatively, if you already use the Heroku CLI:

```sh
heroku create YOUR_APP --stack heroku-24 --buildpack heroku/python
```

Set the required private Config Vars in the Dashboard **before** starting the
worker, including `SESSION_DIR=/tmp/save-restricted-test-sessions`, then:

```sh
heroku git:remote --app YOUR_APP
git push heroku feat/save-restricted-bot:main
heroku ps:scale worker=1:basic --app YOUR_APP
heroku logs --tail --dyno worker --app YOUR_APP
```

This pushes to the Heroku deployment remote, not GitHub `main`. It does not require
a force-push. Heroku CLI/API access is supplied by your own account; do not put
Heroku API tokens in this repository or send them in chat.

## 3. Verify the deployment

A fresh dyno restores the configured native user session privately before opening
MTProto connections. Startup is noninteractive. Look for the JSON event
`{"event": "startup", "status": "listening"}`. That event alone does not prove a
successful save. From a configured owner's private conversation with your bot:

1. Send `/start` and check the usage reply.
2. Send `/save <link>` for a small, unprotected document in an allowed source chat.
3. Check the delivered file and SHA-256 receipt against your original fixture.
4. Try `/cancel` during a subsequent transfer and inspect the chat before retrying.
5. Restart the dyno and repeat `/start` to exercise session restoration.

Keep the worker at one dyno. Dynos have separate temporary filesystems; local file
locks cannot coordinate multiple dynos using the same account session. A restart
loses in-flight jobs and temporary files. The seed restores authorization, not a
durable queue or update-gap recovery. On SIGTERM the worker cancels jobs and closes
clients; Heroku's shutdown deadline can still end unfinished work. A delivered
message can remain even if later verification or cleanup was interrupted.

## Troubleshooting

- **Missing configuration:** fill the variable names reported in startup logs.
- **Fresh dyno has no session:** set `TG_USER_SESSION_B64` from an authorized native
  session. A file created in a one-off dyno is not shared with the worker.
- **Invalid session / account mismatch:** export the correct production session;
  existing local account files are not silently overwritten. If the authorization
  was revoked, log in again on your trusted machine and replace the Config Var.
- **TCP refused / timeout:** the library requires direct IPv4 MTProto to Telegram
  DCs on port 443. HTTPS proxy configuration does not provide this protocol.
- **Bot-token session mismatch:** use the token/session for the same bot, or omit
  the optional bot session seed and let that token authorize a fresh bot session.
- **Protected content / unknown source:** use a permitted source in the allowlist;
  there is no bypass, auto-join or fallback account.
- **Large file failure:** the application size gate is not a live guarantee of
  Telegram permissions, account limits, or available dyno disk. Initial tests
  should use small fixtures; temporary storage and interrupted transfers are local
  to the dyno. Never replay an uncertain upload without inspecting the destination.

## What was checked here

50 offline tests passed: the 33 bot tests plus session export/restore, permissions,
corrupt seeds, repeated startup, lock/account preservation, SIGTERM cleanup and
worker manifest checks. A clean pip install of production requirements and both
CLI entrypoints were checked on Python 3.12.14. This is not a Heroku build or live
Telegram validation. No Heroku credentials were used, app created, dyno started,
or real Telegram login/message/file operation verified by this task.

References: [Heroku's Python buildpack](https://github.com/heroku/heroku-buildpack-python),
[Procfile](https://devcenter.heroku.com/articles/procfile),
[app.json](https://devcenter.heroku.com/articles/app-json-schema), and
[dyno filesystem](https://devcenter.heroku.com/articles/dynos#ephemeral-filesystem).
The Python buildpack's official README confirms `.python-version`, Python 3.12
support, and root `requirements.txt` installation.
