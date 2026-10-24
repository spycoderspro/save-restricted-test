import asyncio
import base64
import json
import os
import signal
from pathlib import Path

import pytest

import bot
from easyproto.session import Session
from session_config import SessionConfigError, export_session, restore_config_sessions, restore_session


@pytest.fixture
def encoded(tmp_path):
    session = Session.load(tmp_path / 'source')
    session.auth_key = bytes(range(256))  # Synthetic offline key, never a real account.
    session.user_id = 42
    session.save()
    return base64.b64encode(session.path.read_bytes()).decode()


def test_restore_native_authorized_session_privately(encoded, tmp_path):
    destination = tmp_path / 'private' / 'user'
    restore_session(encoded, destination)
    restored = Session.load(destination)
    assert restored.user_id == 42 and restored.auth_key == bytes(range(256))
    assert restored.path.stat().st_mode & 0o777 == 0o600
    assert restored.path.parent.stat().st_mode & 0o777 == 0o700
    assert not list(destination.parent.glob('.session-seed-*'))


def test_export_roundtrip_never_prints_credentials(encoded, tmp_path, capsys):
    restore_session(encoded, tmp_path / 'original')
    output = tmp_path / 'export.b64'
    export_session(tmp_path / 'original', output)
    assert output.stat().st_mode & 0o777 == 0o600
    restore_session(output.read_text(), tmp_path / 'restored')
    assert Session.load(tmp_path / 'restored').auth_key == bytes(range(256))
    assert capsys.readouterr().out == ''


def test_export_does_not_overwrite_existing_file(encoded, tmp_path):
    restore_session(encoded, tmp_path / 'original')
    output = tmp_path / 'existing.b64'
    output.write_text('existing user file')
    with pytest.raises(SessionConfigError, match='already exists'):
        export_session(tmp_path / 'original', output)
    assert output.read_text() == 'existing user file'


@pytest.mark.parametrize('encoded', ['not base64!', 'e30=', '', 'A' * 22000])
def test_invalid_seed_fails_without_leaking_or_leaving_files(encoded, tmp_path, capsys):
    with pytest.raises(SessionConfigError):
        restore_session(encoded, tmp_path / 'user')
    assert not list(tmp_path.glob('.session-seed-*'))
    assert not (tmp_path / 'user.easyproto.json').exists()
    assert capsys.readouterr().out == ''


def test_seed_rejects_unauthorized_or_test_session(tmp_path):
    session = Session.load(tmp_path / 'unauthorized')
    session.save()
    with pytest.raises(SessionConfigError):
        restore_session(base64.b64encode(session.path.read_bytes()).decode(), tmp_path / 'target')
    test = Session.load(tmp_path / 'test', test_mode=True)
    test.auth_key = bytes(range(256)); test.user_id = 42
    test.save()
    with pytest.raises(SessionConfigError):
        restore_session(base64.b64encode(test.path.read_bytes()).decode(), tmp_path / 'target')


def test_restarts_retain_fresher_local_session(encoded, tmp_path):
    target = tmp_path / 'user'
    restore_session(encoded, target)
    current = Session.load(target)
    current.salt = 99
    current.save()
    restore_session(encoded, target)
    assert Session.load(target).salt == 99


def test_existing_different_account_is_preserved(encoded, tmp_path):
    target = tmp_path / 'existing'
    current = Session.load(target)
    current.auth_key = bytes(reversed(range(256))); current.user_id = 99
    current.save()
    before = current.path.read_bytes()
    with pytest.raises(SessionConfigError, match='does not match'):
        restore_session(encoded, target)
    assert current.path.read_bytes() == before
    assert not list(tmp_path.glob('.session-seed-*'))


def test_restore_respects_live_session_lock(encoded, tmp_path):
    owner = Session.load(tmp_path / 'user')
    owner.acquire()
    try:
        with pytest.raises(RuntimeError, match='already in use'):
            restore_session(encoded, tmp_path / 'user')
    finally:
        owner.release()
    assert not list(tmp_path.glob('.session-seed-*'))


def test_session_paths_in_checkout_rejected(encoded, monkeypatch, tmp_path):
    import session_config
    monkeypatch.setattr(session_config, 'ROOT', tmp_path)
    with pytest.raises(SessionConfigError, match='outside'):
        restore_session(encoded, tmp_path / 'user')


def test_fresh_heroku_dyno_can_start_without_a_manual_session(monkeypatch, tmp_path):
    from types import SimpleNamespace
    for name in ['TG_USER_SESSION_B64', 'TG_BOT_SESSION_B64']:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DYNO', 'worker.1')
    settings = SimpleNamespace(user_session=tmp_path / 'user', bot_session=tmp_path / 'bot')
    restore_config_sessions(settings)
    assert not Session.load(settings.user_session).path.exists()


def test_config_vars_restore_separate_sessions(encoded, monkeypatch, tmp_path):
    from types import SimpleNamespace
    settings = SimpleNamespace(user_session=tmp_path / 'user', bot_session=tmp_path / 'bot')
    monkeypatch.setenv('TG_USER_SESSION_B64', encoded)
    monkeypatch.delenv('TG_BOT_SESSION_B64', raising=False)
    monkeypatch.setenv('DYNO', 'worker.1')
    restore_config_sessions(settings)
    assert Session.load(settings.user_session).user_id == 42
    assert not Session.load(settings.bot_session).path.exists()


async def test_sigterm_drains_worker_before_exit(monkeypatch, capsys):
    started, cleaned = asyncio.Event(), asyncio.Event()
    callbacks = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'add_signal_handler', lambda sig, callback: callbacks.setdefault(sig, callback))
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda sig: callbacks.pop(sig))
    async def running(settings):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
    monkeypatch.setattr(bot, 'run', running)
    task = asyncio.create_task(bot.worker(None))
    await asyncio.wait_for(started.wait(), 2)
    callbacks[signal.SIGTERM]()
    callbacks[signal.SIGTERM]()  # A repeated signal must not interrupt cleanup.
    await asyncio.wait_for(task, 2)
    assert cleaned.is_set() and not callbacks
    assert '"status": "completed"' in capsys.readouterr().out


async def test_unexpected_worker_failure_is_not_hidden(monkeypatch):
    async def failure(settings): raise ConnectionError('simulated connection failure')
    monkeypatch.setattr(bot, 'run', failure)
    with pytest.raises(ConnectionError): await bot.worker(None)


def test_heroku_manifest_worker_and_required_configuration():
    root = Path(bot.__file__).parent
    manifest = json.loads((root / 'app.json').read_text())
    assert (root / 'Procfile').read_text().strip() == 'worker: python -u bot.py'
    assert (root / '.python-version').read_text().strip() == '3.12'
    assert manifest['formation'] == {'worker': {'quantity': 1, 'size': 'basic'}}
    assert manifest['env']['TG_USER_SESSION_B64']['required'] is False
    assert manifest['env']['MAX_FILE_BYTES']['value'] == str(bot.MAX_ALLOWED_FILE_BYTES)
    assert manifest['env']['SESSION_DIR']['value'].startswith('/tmp/')
