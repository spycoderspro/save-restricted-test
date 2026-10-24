"""A private, allowlisted file-saving bot using the external easyproto package."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from easyproto import AuthorizationRequired, Client, FloodWaitError, Message, RPCError, TLObject, types
from easyproto.client import peer_id
from easyproto.session import Session
from easyproto.transport import IntermediateTransport
from session_config import SessionConfigError, restore_config_sessions
from login_flow import LoginFlowError, OwnerLogin

ROOT = Path(__file__).resolve().parent
MAX_ALLOWED_FILE_BYTES = 1_999_999_999  # Strictly less than 2 GB (decimal).
HELP = ('Use /login to sign in your source account, and /loginstatus to check it. '
        'Send /save https://t.me/chat/message_id, or a private t.me/c link. '
        'Only configured source chats are accepted. /cancel stops your current transfer. '
        'Protected and disappearing content is refused.')


class RequestError(ValueError):
    """A fixed, safe-to-display validation error, without account data."""


def log_event(event, **fields):
    # Never log exception messages, tokens, session keys, links, file names or text.
    print(json.dumps({'event': event, **fields}, sort_keys=True), flush=True)


def peak_rss_kib():
    try:
        import resource
    except ImportError:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024 if sys.platform == 'darwin' else value


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    owners: frozenset[int]
    sources: frozenset[int]
    user_session: Path
    bot_session: Path
    workers: int = 1
    max_bytes: int = MAX_ALLOWED_FILE_BYTES
    max_jobs: int = 1
    login_owner: int | None = None
    login_timeout: int = 300

    @classmethod
    def from_env(cls, *, login=False):
        needed = ['TG_API_ID', 'TG_API_HASH', 'OWNER_IDS', 'SOURCE_CHAT_IDS']
        if not login:
            needed.append('TG_BOT_TOKEN')
        missing = [name for name in needed if not os.getenv(name)]
        if missing:
            raise RequestError('Missing configuration: ' + ', '.join(missing))
        try:
            def identifiers(name):
                return frozenset(int(x.strip()) for x in os.environ[name].split(','))
            owners, sources = identifiers('OWNER_IDS'), identifiers('SOURCE_CHAT_IDS')
            api_id = int(os.environ['TG_API_ID'])
            workers = int(os.getenv('TRANSFER_WORKERS', '1'))
            maximum = int(os.getenv('MAX_FILE_BYTES', str(MAX_ALLOWED_FILE_BYTES)))
            jobs = int(os.getenv('MAX_CONCURRENT_TRANSFERS', '1'))
            login_owner = int(os.getenv('LOGIN_OWNER_ID') or min(owners))
            login_timeout = int(os.getenv('LOGIN_TIMEOUT_SECONDS') or '300')
        except ValueError:
            raise RequestError('IDs and transfer limits must be integers') from None
        if api_id <= 0 or not owners or any(x <= 0 for x in owners) or not sources or 0 in sources:
            raise RequestError('Configure positive owner/API IDs and nonzero signed source chat IDs')
        if workers not in (1, 2, 4) or not 1 <= jobs <= 4 or not 1 <= maximum <= MAX_ALLOWED_FILE_BYTES:
            raise RequestError('Workers must be 1/2/4, jobs 1..4, and size limit 1..1999999999 (less than 2 GB)')
        if login_owner not in owners or not 30 <= login_timeout <= 900:
            raise RequestError('LOGIN_OWNER_ID must be an owner; LOGIN_TIMEOUT_SECONDS must be 30..900')
        folder = Path(os.getenv('SESSION_DIR', '~/.local/share/save-restricted-test')).expanduser()
        user = Path(os.getenv('TG_SESSION', str(folder / 'user'))).expanduser().resolve()
        bot = Path(os.getenv('TG_BOT_SESSION', str(folder / 'bot'))).expanduser().resolve()
        # Session.load treats a .json suffix specially; compare normalized final paths.
        user_path, bot_path = Session.load(user).path, Session.load(bot).path
        if user_path == bot_path:
            raise RequestError('User and bot require separate session files')
        if user.is_relative_to(ROOT) or bot.is_relative_to(ROOT):
            raise RequestError('Keep sessions outside the checkout')
        return cls(api_id, os.environ['TG_API_HASH'], os.getenv('TG_BOT_TOKEN', ''),
                   owners, sources, user, bot, workers, maximum, jobs, login_owner, login_timeout)

    def prepare_sessions(self):
        for path in (self.user_session, self.bot_session):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.parent.stat().st_mode & 0o077:
                raise RequestError('Session parent directories must have mode 0700')


def parse_link(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RequestError('Use a Telegram message link') from None
    if (parsed.scheme != 'https' or parsed.hostname not in ('t.me', 'telegram.me')
            or parsed.username or parsed.password or port or parsed.query or parsed.fragment):
        raise RequestError('Use a plain HTTPS Telegram message link')
    pieces = parsed.path.strip('/').split('/')
    if len(pieces) == 3 and pieces[0] == 'c' and pieces[1].isdigit():
        target = -1_000_000_000_000 - int(pieces[1])
        message_id = pieces[2]
        if int(pieces[1]) <= 0:
            raise RequestError('Invalid private chat link')
    elif len(pieces) == 2 and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', pieces[0]):
        target, message_id = pieces
    else:
        raise RequestError('Use a single message link; invites, albums and topic links are unsupported')
    if not message_id.isdigit() or not 0 < int(message_id) < 2**31:
        raise RequestError('Invalid message ID')
    return target, int(message_id)


class BotClient(Client):
    """Bot-token adapter over EasyProto's pinned raw MTProto API, not another client."""

    async def connect_token(self, token):
        prefix, separator, secret = token.partition(':')
        if not separator or not prefix.isdigit() or not secret:
            raise RequestError('TG_BOT_TOKEN must be a bot token from BotFather')
        try:
            # The pinned library has no public bot-login convenience method.
            # _open establishes MTProto without its user-account login flow.
            await self._open()
            try:
                account = await self.me()
            except RPCError as exc:
                if exc.code != 401:
                    raise
                result = await self.raw.auth.importBotAuthorization(
                    flags=0, api_id=self.api_id, api_hash=self._api_hash, bot_auth_token=token)
                account = self._authorized(result)
            if not account.get('bot'):
                raise RequestError('TG_BOT_SESSION belongs to a user; use a separate bot session')
            if account.id != int(prefix):
                raise RequestError('TG_BOT_TOKEN does not match the stored bot session')
            return self
        except BaseException:
            await self.close()
            raise


def numeric_peer(client, peer):
    return client.session.user_id if peer.name == 'inputPeerSelf' else peer_id(peer)


async def fetch_message(client, peer, identifier):
    ids = [types.inputMessageID(id=identifier)]
    if peer.name == 'inputPeerChannel':
        result = await client.raw.channels.getMessages(
            channel=types.inputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash), id=ids)
    else:
        result = await client.raw.messages.getMessages(id=ids)
    expected = numeric_peer(client, peer)
    for raw in result.get('messages', []):
        actual = raw.get('peer_id')
        if (raw.name == 'message' and raw.id == identifier and isinstance(actual, TLObject)
                and peer_id(actual) == expected):
            return Message(client, raw, peer)
    raise RequestError('Message unavailable in the selected chat')


def file_info(raw, maximum):
    if raw.get('noforwards') or raw.get('ttl_period'):
        raise RequestError('Protected or disappearing messages cannot be saved')
    media = raw.get('media')
    if not isinstance(media, TLObject) or media.get('ttl_seconds'):
        raise RequestError('Use a non-expiring document or photo')
    if media.name == 'messageMediaDocument' and media.get('document'):
        doc = media.document
        size = doc.size
        name = next((a.file_name for a in doc.attributes if a.name == 'documentAttributeFilename'), 'file.bin')
    elif media.name == 'messageMediaPhoto' and media.get('photo'):
        sizes = [s for s in media.photo.sizes if s.name in ('photoSize', 'photoSizeProgressive')]
        if not sizes:
            raise RequestError('Photo has no supported downloadable size')
        size = max(s.get('size', max(s.get('sizes', [0]))) for s in sizes)
        name = 'photo.jpg'
    else:
        raise RequestError('Only documents and photos are supported')
    if not isinstance(size, int) or not 0 < size <= maximum:
        raise RequestError('File is empty or exceeds MAX_FILE_BYTES')
    name = re.sub(r'[^A-Za-z0-9._-]', '_', str(name).replace('\\', '/').split('/')[-1])[:120]
    return size, name if name not in ('', '.', '..') else 'file.bin'


async def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
            await asyncio.sleep(0)
    return value.hexdigest()


async def relay(user, bot, target, identifier, destination, settings):
    started, cpu = time.perf_counter(), time.process_time()
    peer = await user.resolve(target)
    if numeric_peer(user, peer) not in settings.sources:
        raise RequestError('Source chat is not in SOURCE_CHAT_IDS')
    entity = await user.user(peer)
    # Refresh chat protection rather than relying only on a dialog cache.
    if peer.name == 'inputPeerChannel':
        result = await user.raw.channels.getChannels(id=[types.inputChannel(
            channel_id=peer.channel_id, access_hash=peer.access_hash)])
        entity = next((chat for chat in result.chats if peer_id(chat) == numeric_peer(user, peer)), None)
    elif peer.name == 'inputPeerChat':
        result = await user.raw.messages.getChats(id=[peer.chat_id])
        entity = next((chat for chat in result.chats if peer_id(chat) == numeric_peer(user, peer)), None)
    if entity is None:
        raise RequestError('Source chat is unavailable')
    if entity.get('noforwards'):
        raise RequestError('Protected-content chats cannot be saved')
    message = await fetch_message(user, peer, identifier)
    size, name = file_info(message.raw, settings.max_bytes)
    with tempfile.TemporaryDirectory(prefix='save-restricted-') as folder:
        path = await user.download(message, Path(folder) / name, workers=settings.workers)
        if path.stat().st_size != size:
            raise RequestError('Downloaded file size does not match Telegram metadata')
        expected = await digest(path)
        download_seconds = time.perf_counter() - started
        upload_start = time.perf_counter()
        # Always send a document to preserve the original downloaded bytes.
        sent = await bot.media(destination, path, caption='Saved file', workers=settings.workers)
        upload_seconds = time.perf_counter() - upload_start
        stored = await fetch_message(bot, destination, sent.id)
        output = await bot.download(stored, Path(folder) / 'verify.bin', workers=settings.workers)
        if await digest(output) != expected:
            raise RequestError('Delivered file integrity check failed')
    log_event('transfer', status='passed', size_bytes=size, workers=settings.workers,
              download_seconds=download_seconds, upload_seconds=upload_seconds,
              download_mb_per_second=size / 1_000_000 / download_seconds,
              upload_mb_per_second=size / 1_000_000 / upload_seconds,
              duration_seconds=time.perf_counter() - started, cpu_seconds=time.process_time() - cpu,
              process_peak_rss_kib=peak_rss_kib(),
              integrity='sha256_matched', application_retries=0, library_retries=None)
    return size, expected


class SaveBot:
    def __init__(self, settings, user, bot, *, user_ready=True):
        self.settings, self.user, self.bot = settings, user, bot
        self.user_ready = user_ready
        self.jobs = {}
        self.cooldown_until = 0.0
        self.login = OwnerLogin(self, lambda: Client(settings.api_id, settings.api_hash,
            session=settings.user_session, transfer_workers=settings.workers, flood_sleep_threshold=0))

    def login_log(self, status, **fields):
        log_event('login', status=status, **fields)

    async def activate_user(self, client):
        if (await client.me()).get('bot'):
            raise LoginFlowError('The source account must be a user account, not a bot.')
        async for _ in client.chats(limit=1000):
            pass
        self.user, self.user_ready = client, True

    def flooded(self, error):
        self.cooldown_until = max(self.cooldown_until, time.monotonic() + error.seconds)
        log_event('rate_limit', seconds=error.seconds, application_retries=0)

    async def tell(self, destination, text):
        if time.monotonic() < self.cooldown_until:
            return False
        try:
            await self.bot.send(destination, text)
            return True
        except FloodWaitError as exc:
            self.flooded(exc)
        except Exception as exc:
            log_event('notification', status='failed', error_type=type(exc).__name__)
        return False

    async def work(self, owner, target, identifier, destination):
        try:
            size, checksum = await relay(self.user, self.bot, target, identifier, destination, self.settings)
            await self.tell(destination, f'Saved {size} bytes. SHA-256: {checksum}')
        except asyncio.CancelledError:
            log_event('transfer', status='cancelled', application_retries=0)
            raise
        except FloodWaitError as exc:
            self.flooded(exc)  # No replay after a rejected or unknown mutation outcome.
        except RequestError as exc:
            await self.tell(destination, str(exc))
        except Exception as exc:
            log_event('transfer', status='failed', error_type=type(exc).__name__, application_retries=0)
            await self.tell(destination, 'Transfer failed. Check the controlled chat before retrying; it may have been delivered.')
        finally:
            self.jobs.pop(owner, None)

    async def handle(self, event):
        raw_peer = event.raw.get('peer_id')
        owner = event.sender_id
        if (owner not in self.settings.owners or not isinstance(raw_peer, TLObject)
                or raw_peer.name != 'peerUser' or raw_peer.user_id != owner or event.raw.get('out')):
            return
        try:
            destination = await self.bot.resolve(owner)
        except ValueError:
            # MTProto bot peers may omit an access hash in private updates.
            destination = types.inputPeerUser(user_id=owner, access_hash=0)
        parts = event.text.strip().split()
        if not parts:
            return
        command = parts[0].split('@', 1)[0].lower()
        if command == '/cancel':
            login_active = self.login.active_for(owner)
            if login_active:
                await self.login.close()
            job = self.jobs.get(owner)
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                self.jobs.pop(owner, None)
            if login_active and self.user_ready and not job:
                await self.tell(destination, 'Login already completed; the source account remains logged in.')
            else:
                await self.tell(destination, 'Cancelled.' if job or login_active else 'No transfer or login is running.')
            return
        if self.login.active_for(owner) and command not in ('/start', '/help', '/login', '/loginstatus'):
            await self.login.submit(event, destination)
            return
        if time.monotonic() < self.cooldown_until:
            return
        if command in ('/start', '/help'):
            await self.tell(destination, HELP)
            return
        if command == '/login':
            await self.login.begin(owner, destination)
            return
        if command == '/loginstatus':
            await self.tell(destination, 'Source account is logged in.' if self.user_ready else 'Source account needs /login.')
            return
        if not self.user_ready:
            await self.tell(destination, 'The source account is not logged in. Ask the login owner to use /login.')
            return
        value = parts[1] if command == '/save' and len(parts) == 2 else parts[0] if len(parts) == 1 else ''
        try:
            target, identifier = parse_link(value)
        except RequestError as exc:
            await self.tell(destination, str(exc))
            return
        if owner in self.jobs or len(self.jobs) >= self.settings.max_jobs:
            await self.tell(destination, 'Busy. Wait for completion or use /cancel.')
            return
        await self.tell(destination, 'Checking the source and saving the file.')
        if time.monotonic() < self.cooldown_until:
            return
        # The listener must remain free to receive /cancel while a transfer runs.
        self.jobs[owner] = asyncio.create_task(self.work(owner, target, identifier, destination))

    async def close(self):
        await self.login.close()
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()


async def run(settings):
    settings.prepare_sessions()
    restore_config_sessions(settings)
    # Construct clients immediately before opening, avoiding stale pre-login snapshots.
    user = Client(settings.api_id, settings.api_hash, session=settings.user_session,
                  transfer_workers=settings.workers, flood_sleep_threshold=0)
    bot = BotClient(settings.api_id, settings.api_hash, session=settings.bot_session,
                    transfer_workers=settings.workers, flood_sleep_threshold=0)
    service = SaveBot(settings, user, bot, user_ready=False)
    try:
        await bot.connect_token(settings.bot_token)
        if user.session.auth_key and user.session.user_id:
            try:
                await user.connect()  # Reuse an existing valid session without prompting.
            except AuthorizationRequired:
                pass  # The owner can authorize a fresh session through /login.
            else:
                await service.activate_user(user)
        bot.on_message(incoming=True)(service.handle)
        log_event('startup', status='listening', user_login_required=not service.user_ready)
        await bot.listen()  # Gaps/disconnections fail explicitly; no blind request replay.
    finally:
        await service.close()
        await asyncio.gather(bot.close(), service.user.close())


async def worker(settings):
    """Give Heroku's SIGTERM a chance to drain jobs and close both clients."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run(settings))
    stopping = False
    registered = False

    def stop():
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, stop)
            registered = True
        except (NotImplementedError, RuntimeError):
            pass  # Non-POSIX development terminals still support Ctrl+C.
        try:
            await task
        except asyncio.CancelledError:
            if not stopping:
                raise
        if stopping:
            log_event('shutdown', status='completed')
    finally:
        if registered:
            loop.remove_signal_handler(signal.SIGTERM)


async def login(settings):
    settings.prepare_sessions()
    user = Client(settings.api_id, settings.api_hash, session=settings.user_session)
    try:
        await user.start(phone=getpass.getpass('Telegram phone (hidden): '),
                         code_callback=lambda: getpass.getpass('Login code (hidden): '),
                         password=lambda: getpass.getpass('2FA password (hidden): '))
        log_event('login', status='passed')
    finally:
        await user.close()


async def preflight():
    transport = IntermediateTransport('149.154.167.51', timeout=5)
    try:
        await transport.connect()
        log_event('preflight', status='tcp_connected', mtproto='unverified')
    finally:
        await transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login', action='store_true', help='Explicit interactive user-account login')
    parser.add_argument('--check', action='store_true', help='Validate environment configuration without network access')
    parser.add_argument('--preflight', action='store_true', help='Check direct Telegram TCP without credentials')
    args = parser.parse_args()
    # Avoid upstream callback tracebacks containing account message data.
    logging.getLogger('easyproto').disabled = True
    logging.getLogger('easyproto.client').disabled = True
    try:
        if args.preflight:
            asyncio.run(preflight())
            return 0
        settings = Settings.from_env(login=args.login)
        if args.check:
            log_event('configuration', status='passed', telegram='unverified')
            return 0
        asyncio.run(login(settings) if args.login else worker(settings))
        return 0
    except KeyboardInterrupt:
        return 130
    except (RequestError, SessionConfigError, LoginFlowError) as exc:
        log_event('startup', status='failed', reason=str(exc))
    except Exception as exc:
        log_event('startup', status='failed', error_type=type(exc).__name__)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
