import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from easyproto import Client, FloodWaitError, Message, RPCError, TLObject, types
from easyproto.media import PART_SIZE
from bot import BotClient, RequestError, SaveBot, Settings, parse_link, relay


@pytest.fixture
def settings(tmp_path):
    return Settings(12345, 'offline-api-hash', '999:offline-token', frozenset({42}),
                    frozenset({-1000000000007}), tmp_path/'user', tmp_path/'bot')


@pytest.mark.parametrize('link,target,identifier', [
    ('https://t.me/test_chat/123', 'test_chat', 123),
    ('https://telegram.me/test_chat/1', 'test_chat', 1),
    ('https://t.me/c/7/123', -1000000000007, 123),
])
def test_links(link, target, identifier):
    assert parse_link(link) == (target, identifier)


@pytest.mark.parametrize('link', [
    'http://t.me/test_chat/1', 'https://evil.example/test_chat/1',
    'https://t.me@evil.example/test_chat/1', 'https://t.me/+invite',
    'https://t.me/c/0/1', 'https://t.me/test_chat/0', 'https://t.me/test_chat/-1',
    'https://t.me/test_chat/2147483648', 'https://t.me/test_chat/1?single',
    'https://t.me/c/7/99/1', 'https://t.me:443/test_chat/1',
])
def test_bad_links(link):
    with pytest.raises(RequestError): parse_link(link)


class FilePeer:
    """Simulated Telegram RPCs; actual EasyProto Client and file pipeline run."""
    def __init__(self, data, bot=False):
        self.data, self.bot = data, bot
        self.files = {10: data}
        self.parts = {}
        self.calls = []
        self.noforwards = False
        self.message_protected = False
        self.expiring = False
        self.wrong_peer = False
        self.corrupt = False
        self.failure = None
        self.block = False
        self.ready = asyncio.Event()
        self.active = self.peak = 0

    def message(self, identifier, doc_id, size):
        peer = types.peerUser(user_id=42) if self.bot else types.peerChannel(channel_id=7)
        if self.wrong_peer: peer = types.peerChannel(channel_id=8)
        return TLObject('message', id=identifier, peer_id=peer, noforwards=self.message_protected,
            media=TLObject('messageMediaDocument', ttl_seconds=10 if self.expiring else None,
                document=TLObject('document', id=doc_id, access_hash=2, file_reference=b'fixture',
                    dc_id=2, size=size, attributes=[types.documentAttributeFilename(file_name='../../fixture.bin')])))

    async def invoke(self, request):
        self.calls.append(request)
        if request.name == 'channels.getChannels':
            return TLObject('messages.chats', chats=[TLObject('channel', id=7, access_hash=9, noforwards=self.noforwards)])
        if request.name.endswith('.getMessages'):
            doc_id = 20 if self.bot else 10
            return TLObject('messages.messages', messages=[self.message(request.id[0].id, doc_id, len(self.files[doc_id]))])
        if request.name.startswith('upload.'):
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.ready.set()
            try:
                if self.failure: raise self.failure
                if self.block: await asyncio.Event().wait()
                await asyncio.sleep(0.001)
                if request.name == 'upload.getFile':
                    data = self.files[request.location.id]
                    if self.corrupt: data = bytes(len(data))
                    return TLObject('upload.file', bytes=data[request.offset:request.offset+request.limit])
                self.parts.setdefault(request.file_id,{})[request.file_part] = request.bytes
                return True
            finally:
                self.active -= 1
        if request.name == 'messages.sendMedia':
            file = request.media.file
            data = b''.join(self.parts[file.id][i] for i in range(file.parts))
            assert hashlib.md5(data).hexdigest() == file.md5_checksum
            self.files[20] = data
            return TLObject('updateShortSentMessage', id=8, date=1)
        if request.name == 'messages.sendMessage':
            return TLObject('updateShortSentMessage', id=9, date=1)
        raise AssertionError(request.name)


def clients(data):
    user = Client(12345, 'offline-api-hash', session=':memory:')
    bot = Client(12345, 'offline-api-hash', session=':memory:')
    user._connection = FilePeer(data)
    bot._connection = FilePeer(b'',bot=True)
    user._cache_result(TLObject('channel',id=7,access_hash=9,username='test_chat'))
    return user, bot


def destination(): return types.inputPeerUser(user_id=42, access_hash=1)


def assert_clean(user, bot, tmp_path):
    assert user._connection.active == bot._connection.active == 0
    assert not list(tmp_path.glob('save-restricted-*'))
    assert not any(t.get_name() == 'easyproto-transfer' and not t.done() for t in asyncio.all_tasks())


@pytest.mark.parametrize('workers', [1,2,4])
async def test_external_client_roundtrip_and_integrity(settings, tmp_path, monkeypatch, workers, capsys):
    import bot as application
    monkeypatch.setattr(application.tempfile,'tempdir',str(tmp_path))
    data = bytes(range(256))*(3*PART_SIZE//256) + b'end'
    user, bot = clients(data)
    size, checksum = await relay(user,bot,'test_chat',123,destination(),replace(settings,workers=workers))
    assert size == len(data) and checksum == hashlib.sha256(data).hexdigest()
    assert bot._connection.files[20] == data
    assert user._connection.peak == workers
    assert_clean(user,bot,tmp_path)
    output = capsys.readouterr().out
    assert 'sha256_matched' in output
    assert 'fixture' not in output and 'offline-api-hash' not in output


@pytest.mark.parametrize('case',['chat_protected','message_protected','expiring','wrong_chat','too_big','not_allowed'])
async def test_scope_and_protection_refusal(settings, case):
    user,bot=clients(b'fixture')
    peer=user._connection
    if case=='chat_protected': peer.noforwards=True
    if case=='message_protected': peer.message_protected=True
    if case=='expiring': peer.expiring=True
    if case=='wrong_chat': peer.wrong_peer=True
    if case=='too_big': settings=replace(settings,max_bytes=1)
    if case=='not_allowed': settings=replace(settings,sources=frozenset({-1}))
    with pytest.raises(RequestError): await relay(user,bot,'test_chat',123,destination(),settings)
    assert not any(r.name.startswith('upload.') for r in peer.calls)
    assert not bot._connection.calls


async def test_destination_corruption_fails_and_cleans(settings,tmp_path,monkeypatch):
    import bot as application
    monkeypatch.setattr(application.tempfile,'tempdir',str(tmp_path))
    user,bot=clients(b'fixture')
    bot._connection.corrupt=True
    with pytest.raises(RequestError,match='integrity'):
        await relay(user,bot,'test_chat',123,destination(),settings)
    assert_clean(user,bot,tmp_path)


async def test_cancellation_drains_external_library_workers(settings,tmp_path,monkeypatch):
    import bot as application
    monkeypatch.setattr(application.tempfile,'tempdir',str(tmp_path))
    user,bot=clients(b'x'*(4*PART_SIZE))
    user._connection.block=True
    task=asyncio.create_task(relay(user,bot,'test_chat',123,destination(),replace(settings,workers=4)))
    await asyncio.wait_for(user._connection.ready.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert_clean(user,bot,tmp_path)


async def test_floodwait_is_not_retried(settings,tmp_path,monkeypatch):
    import bot as application
    monkeypatch.setattr(application.tempfile,'tempdir',str(tmp_path))
    user,bot=clients(b'fixture')
    user._connection.failure=FloodWaitError(420,'FLOOD_WAIT_30',30)
    with pytest.raises(FloodWaitError): await relay(user,bot,'test_chat',123,destination(),settings)
    assert sum(r.name=='upload.getFile' for r in user._connection.calls)==1
    assert not bot._connection.calls
    assert_clean(user,bot,tmp_path)


def event(client,text='/start',sender=42,group=False):
    return Message(client,TLObject('message',id=1,message=text,from_id=types.peerUser(user_id=sender),
        peer_id=types.peerChat(chat_id=12) if group else types.peerUser(user_id=sender)),types.peerUser(user_id=sender))


async def test_unauthorized_users_and_groups_are_ignored(settings):
    user,bot=clients(b'fixture')
    service=SaveBot(settings,user,bot)
    await service.handle(event(bot,sender=100))
    await service.handle(event(bot,group=True))
    assert not bot._connection.calls and not service.jobs


async def test_commands_background_job_and_cancel_remain_responsive(settings,monkeypatch):
    import bot as application
    user,bot=clients(b'fixture')
    service=SaveBot(settings,user,bot)
    entered=asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(application,'relay',blocked)
    await service.handle(event(bot,'/save https://t.me/test_chat/123'))
    await asyncio.wait_for(entered.wait(),2)
    assert 42 in service.jobs
    await service.handle(event(bot,'/save https://t.me/test_chat/123'))
    assert len(service.jobs)==1
    await service.handle(event(bot,'/cancel'))
    assert not service.jobs


async def test_cancel_before_job_starts_does_not_leave_busy_state(settings):
    user,bot=clients(b'fixture')
    service=SaveBot(settings,user,bot)
    await service.handle(event(bot,'/save https://t.me/test_chat/123'))
    await service.handle(event(bot,'/cancel'))
    assert not service.jobs


async def test_shutdown_cancels_jobs(settings):
    user,bot=clients(b'fixture')
    service=SaveBot(settings,user,bot)
    task=asyncio.create_task(asyncio.Event().wait())
    service.jobs[42]=task
    await service.close()
    assert task.cancelled() and not service.jobs


async def test_bot_login_uses_own_library_raw_api_and_closes_on_error():
    bot=BotClient(12345,'offline-api-hash',session=':memory:')
    bot._open=AsyncMock()
    bot.close=AsyncMock()
    connection=MagicMock()
    connection.invoke=AsyncMock(side_effect=[RPCError(401,'AUTH_KEY_UNREGISTERED'),
        TLObject('auth.authorization',user=TLObject('user',id=999,bot=True))])
    bot._connection=connection
    assert await bot.connect_token('999:offline-token') is bot
    request=connection.invoke.call_args.args[0]
    assert request.name=='auth.importBotAuthorization'
    assert request.bot_auth_token=='999:offline-token'
    assert bot.session.user_id==999
    bot.me=AsyncMock(return_value=TLObject('user',id=998,bot=True))
    with pytest.raises(RequestError,match='does not match'): await bot.connect_token('999:offline-token')
    bot.close.assert_awaited_once()


def test_configuration_names_and_secret_repr(settings,monkeypatch):
    for name in ['TG_API_ID','TG_API_HASH','TG_BOT_TOKEN','OWNER_IDS','SOURCE_CHAT_IDS']:
        monkeypatch.delenv(name,raising=False)
    with pytest.raises(RequestError,match='Missing configuration'): Settings.from_env()
    assert settings.api_hash not in repr(settings) and settings.bot_token not in repr(settings)


def test_same_or_checkout_sessions_rejected(settings,monkeypatch,tmp_path):
    import bot as application
    for key,value in {'TG_API_ID':'12345','TG_API_HASH':'offline-api-hash','TG_BOT_TOKEN':'999:offline-token',
                      'OWNER_IDS':'42','SOURCE_CHAT_IDS':'-1000000000007'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setenv('TG_SESSION',str(tmp_path/'account'))
    monkeypatch.setenv('TG_BOT_SESSION',str(tmp_path/'account.easyproto.json'))
    with pytest.raises(RequestError,match='separate'): Settings.from_env()
    monkeypatch.setenv('TG_BOT_SESSION',str(tmp_path/'bot'))
    monkeypatch.setenv('TG_SESSION',str(application.ROOT/'account'))
    with pytest.raises(RequestError,match='outside'): Settings.from_env()
