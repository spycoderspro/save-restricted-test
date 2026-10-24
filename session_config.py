"""Restore native EasyProto sessions from private hosting configuration.

The encoded values are credentials. This module never prints session contents.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import os
import tempfile
from pathlib import Path

from easyproto.session import Session

ROOT = Path(__file__).resolve().parent
MAX_SESSION_BYTES = 16_384


class SessionConfigError(ValueError):
    """A safe error without session data or decoder exception contents."""


def _authorized(session):
    if (len(session.auth_key) != 256 or isinstance(session.user_id, bool)
            or not isinstance(session.user_id, int) or session.user_id <= 0 or session.test_mode):
        raise SessionConfigError('Use an authorized production EasyProto session')


def _private_parent(path):
    if path.resolve().is_relative_to(ROOT):
        raise SessionConfigError('Session and export files must stay outside the checkout')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise SessionConfigError('Session and export parent directories must have mode 0700')


def restore_session(encoded, destination):
    if len(encoded) > 4 * ((MAX_SESSION_BYTES + 2) // 3) + 2:
        raise SessionConfigError('Encoded session exceeds the size limit')
    try:
        payload = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, binascii.Error):
        raise SessionConfigError('Invalid encoded EasyProto session') from None
    if not payload or len(payload) > MAX_SESSION_BYTES:
        raise SessionConfigError('Invalid encoded EasyProto session size')
    target = Session.load(destination).path
    if target is None:
        raise SessionConfigError('Hosted sessions require a private file path')
    _private_parent(target)
    fd, temporary = tempfile.mkstemp(prefix='.session-seed-', suffix='.json', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
        try:
            restored = Session.load(temporary)
            _authorized(restored)
        except SessionConfigError:
            raise
        except Exception:
            raise SessionConfigError('Invalid native EasyProto session; other library formats are unsupported') from None
        guard = Session.load(destination)
        guard.acquire()
        try:
            if target.exists():
                current = Session.load(destination)
                _authorized(current)
                if current.user_id != restored.user_id:
                    raise SessionConfigError('Configured session does not match the existing account')
                # Keep fresher local salts, keys and DC state until the dyno is replaced.
                return
            restored.path = target
            restored.save()
        finally:
            guard.release()
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_config_sessions(settings):
    for variable, path in [('TG_USER_SESSION_B64', settings.user_session),
                           ('TG_BOT_SESSION_B64', settings.bot_session)]:
        value = os.getenv(variable)
        if value:
            try:
                restore_session(value, path)
            except SessionConfigError:
                raise
            except Exception:
                raise SessionConfigError('Could not restore the configured private session') from None


def export_session(source, output):
    output = Path(output).expanduser().resolve()
    _private_parent(output)
    guard = Session.load(source)
    if guard.path is None or not guard.path.exists():
        raise SessionConfigError('An authorized EasyProto session file is required')
    guard.acquire()
    try:
        session = Session.load(source)
        _authorized(session)
        encoded = base64.b64encode(session.path.read_bytes()) + b'\n'
        try:
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SessionConfigError('Export output already exists; choose a new private file') from None
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    finally:
        guard.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True, help='Authorized native EasyProto session path')
    parser.add_argument('--output', required=True, help='New private output file outside the checkout')
    args = parser.parse_args()
    try:
        export_session(args.session, args.output)
    except SessionConfigError as exc:
        print(str(exc))
        return 1
    except Exception:
        print('Session export failed; check private file paths and session permissions')
        return 1
    print('Session export saved privately. Treat the file as an account credential; never commit or log it.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
