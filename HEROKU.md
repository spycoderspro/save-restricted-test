# Test on Heroku

Deploy one worker, configure the bot, and use **`/login` in your own bot's private
chat**. A manually exported session is no longer required. The app uses Python
3.12 and your external `telegram-lib` dependency. The manifest selects one Basic
worker, subject to your Heroku account's billing; this repository does not create
or charge an app on your behalf.

## 1. Deploy and configure

[Deploy this branch to Heroku](https://heroku.com/deploy?template=https://github.com/spycoderspro/save-restricted-test/tree/feat/save-restricted-bot)

The link uses `feat/save-restricted-bot`; GitHub `main` has not been merged.
Provide these Config Vars in the Heroku form or Dashboard Settings:

| Config Var | Value |
|---|---|
| `TG_API_ID` | Your Telegram API application ID |
| `TG_API_HASH` | Your private Telegram API hash |
| `TG_BOT_TOKEN` | Your own bot's private token |
| `OWNER_IDS` | Comma-separated numeric user IDs permitted to use the bot |
| `SOURCE_CHAT_IDS` | Comma-separated signed IDs of authorized source chats |
| `LOGIN_OWNER_ID` | Optional owner ID allowed to log in the shared source account; defaults to the smallest `OWNER_IDS` entry |
| `LOGIN_TIMEOUT_SECONDS` | `300` by default; allowed range 30–900 |
| `SESSION_DIR` | `/tmp/save-restricted-test-sessions` |
| `TRANSFER_WORKERS` | `1` initially |
| `MAX_CONCURRENT_TRANSFERS` | `1` initially |
| `MAX_FILE_BYTES` | `1999999999`, strictly less than 2 GB |

Leave `TG_USER_SESSION_B64` and `TG_BOT_SESSION_B64` empty for the new login flow.
API credentials and the bot token still belong in private Config Vars, never in
Git or a chat with a third party. Keep the worker at **one dyno**. There is no web
process or HTTP endpoint; the Heroku app URL is not a health check.

Alternatively, with your own Heroku CLI account:

```sh
heroku create YOUR_APP --stack heroku-24 --buildpack heroku/python
```

Set the required Config Vars in the Dashboard, including the `/tmp` session
directory, then deploy the existing branch:

```sh
heroku git:remote --app YOUR_APP
git push heroku feat/save-restricted-bot:main
heroku ps:scale worker=1:basic --app YOUR_APP
heroku logs --tail --dyno worker --app YOUR_APP
```

This deploys to Heroku's remote, not GitHub `main`, and needs no force-push.

## 2. Log in through your bot

From `LOGIN_OWNER_ID`'s own Telegram account, open a **private conversation with
your own trusted bot**:

1. Send `/start`, then `/login`.
2. Send your own account's phone number in international format, starting with `+`.
3. Send the numeric login code Telegram requested when the bot prompts for it.
4. If Telegram requests 2FA, provide the password in response to that prompt.
5. Wait for **Login complete**, then use `/loginstatus` or save a permitted file.

The bot uses the existing library's `Client.start`, `send_code`, `sign_in`, and
`check_password` flow. It does not sign up new accounts or replace the Telegram
client. Only one login can run, only the configured login owner can supply its
inputs, and the default five-minute timeout applies to the complete flow. The
shared source account is used by configured owners, not separate accounts per user.

`/cancel` stops pending login or your active transfer. Pending callback state is
cleared, connections are closed on failure/cancellation, and requests are never
automatically retried after bad codes, wrong passwords or FloodWait. A completed
Telegram sign-in may already have produced a native session when cancellation
arrives; cancellation does not revoke an already-authorized account session.

Login inputs are never echoed or logged. The bot attempts to delete each input
message and clears its local message text; deletion is best effort and does not
make a Telegram bot conversation end-to-end encrypted. Use only a deployment you
own and trust. Never provide these credentials to another bot or this coding chat.
Telegram may reject or invalidate codes sent in chats. If it does, the app reports
failure rather than bypassing that protection; use the existing direct terminal
`bot.py --login` flow on a trusted host instead. Extra email/paid verification and
signup remain unsupported by the pinned library.

## 3. Test saving and restarts

1. Check `/loginstatus` reports the source account is logged in.
2. Send `/save <link>` for a small, unprotected document in an allowed source chat.
3. Compare the delivered file and SHA-256 receipt with your original fixture.
4. Try `/cancel` during another transfer and inspect the destination before retrying.

The native session is saved automatically in the private session directory. On a
persistent host it can be reused at startup. Heroku replaces temporary files on a
dyno replacement, so **use `/login` again after that replacement** unless optional
session restoration is configured. No manual session Config Var is required to
start or use the login feature. Restarts lose in-flight jobs and do not recover
missed updates. SIGTERM triggers transfer/login cancellation and client cleanup;
Heroku's shutdown deadline can still interrupt work.

## Optional: restore an existing session across dyno replacement

This is optional compatibility support, not a prerequisite for `/login`. An owner
who prefers it can export an already-authorized native EasyProto session on a
trusted machine:

```sh
.venv/bin/python session_config.py \
  --session /private/path/to/user \
  --output /private/path/to/user-session.b64
```

The parent directory must be mode 0700. The utility creates a new mode-0600 file,
refuses overwrites, and never prints the value. Put its contents in the private
Heroku `TG_USER_SESSION_B64` Config Var. Base64 is encoding, not encryption: that
value grants account access. Never commit, log, screenshot or paste it in chat.
An optional bot session export works through `TG_BOT_SESSION_B64`; otherwise the
bot token authorizes a fresh bot session. Other clients' session strings and
Telegram test-network sessions are incompatible. Remove an obsolete seed when
switching accounts; existing different account files are not silently overwritten.

## Troubleshooting and verification limits

- Missing configuration: fill only the variable names reported in startup logs.
- Bot online, source logged out: use `/login` from the configured login owner's
  private conversation. `/save` is unavailable until login completes.
- Invalid/expired code or wrong 2FA: use `/login` again after resolving the cause.
  No request is replayed, code obfuscation is not implemented, and FloodWait starts
  a cooldown.
- TCP refused/timeout: EasyProto needs direct IPv4 MTProto to Telegram DCs on port
  443. An HTTPS proxy does not supply this protocol.
- Protected content or unknown source: use an allowed, permitted source. There is
  no bypass, auto-join or fallback account.
- Large file failure: the size gate does not guarantee Telegram permissions,
  account limits, or dyno disk. Start with small fixtures and inspect uncertain sends.

**71 offline tests pass**, including actual installed EasyProto phone/code method
calls against mocked RPCs, 2FA callbacks, private-owner checks, cancellation,
timeouts, secret-free errors, optional session restore and SIGTERM cleanup.
Production requirements install and both CLI imports were checked with Python
3.12.14. No live Telegram login, bot/file operation, Heroku build, or deployment
was verified here. A startup `listening` event alone is not a successful login or
save. The bot-login adapter still uses pinned private library hooks.

References: [Heroku's Python buildpack](https://github.com/heroku/heroku-buildpack-python),
[Procfile](https://devcenter.heroku.com/articles/procfile),
[app.json](https://devcenter.heroku.com/articles/app-json-schema), and
[dyno filesystem](https://devcenter.heroku.com/articles/dynos#ephemeral-filesystem).
