import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

import bot as application
from bot import SaveBot, Settings
from easyproto import AuthorizationRequired, Client, FloodWaitError, Message, RPCError, TLObject, types
from easyproto.session import Session


class FakeUser:
    def __init__(self, *, twofa=False, error=None):
        self.twofa, self.error = twofa, error
        self.start_calls = 0
        self.phone = self.code = self.password = None
        self.close = AsyncMock()
        self.session = Session(None)
        self.connect = AsyncMock()

    async def start(self, *, phone, code_callback, password):
        self.start_calls += 1
        self.phone = phone
        self.code = await code_callback()
        if self.twofa:
            self.password = await password()
        if self.error:
            raise self.error
        return self

    async def me(self): return TLObject('user', id=42)

    async def chats(self, **kwargs):
        if False: yield None


def event(client, text, *, owner=42, group=False):
    return Message(client, TLObject('message', id=10, message=text,
        from_id=types.peerUser(user_id=owner),
        peer_id=types.peerChat(chat_id=7) if group else types.peerUser(user_id=owner)),
        types.peerUser(user_id=owner))


@pytest.fixture
async def fixture(tmp_path):
    settings = Settings(12345, 'offline-hash', '999:offline-token', frozenset({42, 43}),
        frozenset({-7}), tmp_path/'user', tmp_path/'bot')
    user, candidate = FakeUser(), FakeUser()
    bot = MagicMock()
    bot.resolve = AsyncMock(side_effect=lambda owner: types.inputPeerUser(user_id=owner, access_hash=0))
    bot.send, bot.delete = AsyncMock(), AsyncMock()
    service = SaveBot(settings, user, bot, user_ready=False)
    service.login.client_factory = MagicMock(return_value=candidate)
    try:
        yield service, candidate
    finally:
        await service.close()
        await service.user.close()


async def wait_step(service, step):
    async def wait():
        while service.login.step != step:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


async def start_login(service):
    await service.handle(event(service.bot, '/login'))
    task = service.login.task
    await wait_step(service, 'phone')
    return task


async def enter_phone_and_code(service):
    await service.handle(event(service.bot, '+10000000000'))
    await wait_step(service, 'code')
    await service.handle(event(service.bot, '12345'))


async def test_owner_login_saves_session_without_manual_seed(fixture):
    service, candidate = fixture
    task = await start_login(service)
    await enter_phone_and_code(service)
    await asyncio.wait_for(task, 2)
    assert service.user_ready and service.user is candidate
    assert candidate.phone == '+10000000000' and candidate.code == '12345'
    assert service.login.task is None and service.login.waiter is None and service.login.owner is None
    assert service.bot.delete.await_count == 2
    assert all(call.kwargs['revoke'] is True for call in service.bot.delete.call_args_list)
    candidate.close.assert_not_awaited()


async def test_twofa_password_callback_does_not_echo_or_log_secrets(fixture, capsys):
    service, candidate = fixture
    candidate.twofa = True
    task = await start_login(service)
    await enter_phone_and_code(service)
    await wait_step(service, 'password')
    password_event = event(service.bot, '  synthetic private password  ')
    await service.handle(password_event)
    await asyncio.wait_for(task, 2)
    assert candidate.password == '  synthetic private password  '
    assert password_event.text == ''
    output = capsys.readouterr().out
    outgoing = '\n'.join(call.args[1] for call in service.bot.send.call_args_list)
    for secret in ['+10000000000', '12345', 'synthetic private password']:
        assert secret not in output and secret not in outgoing
    assert service.bot.delete.await_count == 3


@pytest.mark.parametrize('owner,group', [(100, False), (42, True)])
async def test_nonowners_and_groups_cannot_start_or_submit_login(fixture, owner, group):
    service, _ = fixture
    await service.handle(event(service.bot, '/login', owner=owner, group=group))
    service.bot.send.assert_not_awaited()
    service.bot.delete.assert_not_awaited()
    assert service.login.task is None


async def test_other_authorized_operator_cannot_authorize_shared_account(fixture):
    service, candidate = fixture
    await service.handle(event(service.bot, '/login', owner=43))
    assert service.login.task is None and candidate.start_calls == 0
    service.login.client_factory.assert_not_called()


async def test_configured_login_owner_can_start_login(fixture):
    service, _ = fixture
    service.settings = replace(service.settings, login_owner=43)
    await service.handle(event(service.bot, '/login', owner=43))
    await wait_step(service, 'phone')
    assert service.login.owner == 43
    assert service.bot.send.call_args.args[0].user_id == 43


async def test_other_operator_cannot_supply_credentials_to_active_login(fixture):
    service, candidate = fixture
    task = await start_login(service)
    await service.handle(event(service.bot, '+10000000000', owner=43))
    assert service.login.step == 'phone' and candidate.start_calls == 0
    service.bot.delete.assert_not_awaited()
    assert not task.done()


async def test_cancel_login_closes_candidate_and_clears_waiters(fixture):
    service, candidate = fixture
    task = await start_login(service)
    await service.handle(event(service.bot, '/cancel'))
    assert task.cancelled() and not service.user_ready
    candidate.close.assert_awaited_once()
    assert service.login.task is None and service.login.waiter is None and service.login.step is None


async def test_cancel_before_login_task_starts_is_safe(fixture):
    service, _ = fixture
    await service.handle(event(service.bot, '/login'))
    await service.handle(event(service.bot, '/cancel'))
    assert service.login.task is None and service.login.owner is None


async def test_login_timeout_clears_candidate_and_state(fixture):
    service, candidate = fixture
    service.settings = replace(service.settings, login_timeout=0.01)
    task = await start_login(service)
    await asyncio.wait_for(task, 2)
    assert not service.user_ready and service.login.waiter is None and service.login.task is None
    candidate.close.assert_awaited_once()
    assert any('expired' in call.args[1] for call in service.bot.send.call_args_list)


async def test_repeated_login_does_not_send_multiple_codes(fixture):
    service, candidate = fixture
    task = await start_login(service)
    await service.handle(event(service.bot, '/login'))
    assert service.login.task is task
    await enter_phone_and_code(service)
    await task
    assert candidate.start_calls == 1


async def test_invalid_phone_and_code_stay_in_expected_step(fixture):
    service, candidate = fixture
    task = await start_login(service)
    await service.handle(event(service.bot, 'invalid phone'))
    assert service.login.step == 'phone' and candidate.start_calls == 0
    await service.handle(event(service.bot, '+10000000000'))
    await wait_step(service, 'code')
    await service.handle(event(service.bot, 'invalid code'))
    assert service.login.step == 'code'
    await service.handle(event(service.bot, '12345'))
    await task
    assert service.user_ready


async def test_login_failure_logs_only_exception_class_and_never_retries(fixture, capsys):
    service, candidate = fixture
    candidate.error = RPCError(400, 'SYNTHETIC_SECRET_ERROR_BODY')
    task = await start_login(service)
    await enter_phone_and_code(service)
    await task
    assert not service.user_ready and candidate.start_calls == 1
    candidate.close.assert_awaited_once()
    assert 'SYNTHETIC_SECRET_ERROR_BODY' not in capsys.readouterr().out
    assert all('SYNTHETIC_SECRET_ERROR_BODY' not in call.args[1] for call in service.bot.send.call_args_list)


async def test_floodwait_cancels_login_without_retry(fixture):
    service, candidate = fixture
    candidate.error = FloodWaitError(420, 'FLOOD_WAIT_30', 30)
    task = await start_login(service)
    await enter_phone_and_code(service)
    await task
    assert service.cooldown_until > 0 and candidate.start_calls == 1
    assert not service.user_ready
    candidate.close.assert_awaited_once()


async def test_rate_limited_input_cleanup_cancels_pending_login(fixture):
    service, candidate = fixture
    task = await start_login(service)
    service.bot.delete.side_effect = FloodWaitError(420, 'FLOOD_WAIT_30', 30)
    await service.handle(event(service.bot, '+10000000000'))
    assert task.cancelled() and candidate.start_calls == 0
    assert service.login.waiter is None and service.login.owner is None
    candidate.close.assert_awaited_once()


async def test_already_logged_in_does_not_replace_account(fixture):
    service, candidate = fixture
    service.user_ready = True
    await service.handle(event(service.bot, '/login'))
    assert candidate.start_calls == 0 and service.login.task is None


async def test_save_is_blocked_until_login(fixture):
    service, candidate = fixture
    await service.handle(event(service.bot, '/save https://t.me/chat/1'))
    assert not service.jobs and candidate.start_calls == 0
    assert 'not logged in' in service.bot.send.call_args.args[1]


async def test_login_uses_real_external_client_phone_code_methods(fixture):
    service, _ = fixture
    client = Client(12345, 'offline-hash', session=':memory:')
    connection = MagicMock()
    connection.closed = asyncio.Event()
    connection.close = AsyncMock()
    connection.invoke = AsyncMock(side_effect=[
        RPCError(401, 'AUTH_KEY_UNREGISTERED'),
        TLObject('auth.sentCode', phone_code_hash='synthetic-code-hash'),
        TLObject('auth.authorization', user=TLObject('user', id=42)),
        [TLObject('user', id=42)],
        TLObject('messages.dialogs', dialogs=[], messages=[], users=[], chats=[]),
    ])
    client._connection = connection
    service.login.client_factory = lambda: client
    task = await start_login(service)
    await enter_phone_and_code(service)
    await asyncio.wait_for(task, 2)
    assert service.user_ready and service.user is client
    requests = [call.args[0] for call in connection.invoke.call_args_list]
    assert [r.name for r in requests] == ['users.getUsers', 'auth.sendCode', 'auth.signIn',
                                         'users.getUsers', 'messages.getDialogs']
    assert requests[2].phone_code == '12345'


async def test_real_external_client_requests_twofa_callback(fixture):
    service, _ = fixture
    client = Client(12345, 'offline-hash', session=':memory:')
    connection = MagicMock()
    connection.closed = asyncio.Event()
    connection.close = AsyncMock()
    connection.invoke = AsyncMock(side_effect=[
        RPCError(401, 'AUTH_KEY_UNREGISTERED'),
        TLObject('auth.sentCode', phone_code_hash='synthetic-code-hash'),
        RPCError(401, 'SESSION_PASSWORD_NEEDED'),
        [TLObject('user', id=42)],
        TLObject('messages.dialogs', dialogs=[], messages=[], users=[], chats=[]),
    ])
    client._connection = connection
    client.check_password = AsyncMock(return_value=TLObject('user', id=42))
    service.login.client_factory = lambda: client
    task = await start_login(service)
    await enter_phone_and_code(service)
    await wait_step(service, 'password')
    await service.handle(event(service.bot, 'synthetic password'))
    await asyncio.wait_for(task, 2)
    client.check_password.assert_awaited_once_with('synthetic password')
    assert service.user_ready


@pytest.mark.parametrize('expired', [False, True])
async def test_worker_starts_bot_and_offers_login_without_usable_user_session(monkeypatch, tmp_path, expired):
    settings = Settings(12345, 'offline-hash', '999:offline-token', frozenset({42}),
                        frozenset({-7}), tmp_path/'user', tmp_path/'bot')
    user, bot = FakeUser(), MagicMock()
    if expired:
        user.session = Session(None, auth_key=bytes(range(256)), user_id=42)
        user.connect = AsyncMock(side_effect=AuthorizationRequired())
    bot.connect_token = AsyncMock()
    bot.close = AsyncMock()
    registered = []
    bot.on_message.return_value = lambda handler: registered.append(handler)
    async def listen():
        assert registered and not registered[0].__self__.user_ready
    bot.listen = listen
    monkeypatch.setattr(application, 'Client', lambda *a, **kw: user)
    monkeypatch.setattr(application, 'BotClient', lambda *a, **kw: bot)
    monkeypatch.setattr(application, 'restore_config_sessions', lambda settings: None)
    await application.run(settings)
    if not expired: user.connect.assert_not_awaited()
    bot.connect_token.assert_awaited_once()
    bot.close.assert_awaited_once()
    user.close.assert_awaited_once()
