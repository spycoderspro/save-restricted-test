"""Owner-only, short-lived login callbacks for the external EasyProto Client."""
from __future__ import annotations

import asyncio
import re

from easyproto import FloodWaitError


class LoginFlowError(ValueError):
    """A fixed message safe to display without credentials or raw RPC errors."""


class OwnerLogin:
    def __init__(self, service, client_factory):
        self.service = service
        self.client_factory = client_factory
        self.task = None
        self.owner = None
        self.step = None
        self.waiter = None

    @property
    def login_owner(self):
        return self.service.settings.login_owner or min(self.service.settings.owners)

    def active_for(self, owner):
        return self.task is not None and not self.task.done() and self.owner == owner

    async def begin(self, owner, destination):
        if owner != self.login_owner:
            await self.service.tell(destination, 'Only the configured login owner can sign in the source account.')
            return
        if self.service.user_ready:
            await self.service.tell(destination, 'The source account is already logged in.')
            return
        if self.task is not None and not self.task.done():
            await self.service.tell(destination, 'Login is already running. Use /cancel before starting again.')
            return
        if self.service.jobs:
            await self.service.tell(destination, 'Finish or cancel active transfers before logging in.')
            return
        self.owner = owner
        self.task = asyncio.create_task(self._run(destination), name='save-bot-login')

    async def _prompt(self, destination, step, text):
        future = asyncio.get_running_loop().create_future()
        self.waiter, self.step = future, step
        try:
            if not await self.service.tell(destination, text):
                raise LoginFlowError('Login stopped because the prompt could not be delivered.')
            return await future
        finally:
            if self.waiter is future:
                self.waiter, self.step = None, None
            if not future.done():
                future.cancel()

    async def submit(self, event, destination):
        """Called only after SaveBot verifies the sender and private conversation."""
        if not self.active_for(event.sender_id):
            return False
        value = event.text if self.step == 'password' else event.text.strip()
        # Best effort: never echo secrets, and clear the app's event text reference.
        event.raw.fields['message'] = ''
        try:
            await self.service.bot.delete(destination, event.id, revoke=True)
        except FloodWaitError as exc:
            self.service.flooded(exc)
            await self.close()
            return True
        except Exception as exc:
            self.service.login_log('input_cleanup', error_type=type(exc).__name__)
        future, step = self.waiter, self.step
        if future is None or future.done():
            await self.service.tell(destination, 'Please wait for the next login prompt.')
            return True
        if step == 'phone' and not re.fullmatch(r'\+[1-9][0-9]{6,14}', value):
            await self.service.tell(destination, 'Send your phone in international format, starting with +, or /cancel.')
            return True
        if step == 'code' and not re.fullmatch(r'[0-9]{3,10}', value):
            await self.service.tell(destination, 'Send the numeric Telegram login code, or /cancel.')
            return True
        if step == 'password' and not 1 <= len(value) <= 1024:
            await self.service.tell(destination, 'Send your 2FA password as one message, or /cancel.')
            return True
        future.set_result(value)
        return True

    async def _authenticate(self, destination):
        client = None
        completed = False
        try:
            await self.service.user.close()
            client = self.client_factory()
            phone = await self._prompt(destination, 'phone',
                'Login to your own account: send your phone in international format (+countrycode...), '
                'or /cancel. Login messages are deleted on a best-effort basis; this is not an encrypted secret chat.')

            async def code():
                return await self._prompt(destination, 'code',
                    'Send the login code Telegram requested. Never forward it to other bots or people. /cancel stops login.')

            async def password():
                return await self._prompt(destination, 'password',
                    'Telegram requires 2FA. Send your password to this private bot conversation, or /cancel.')

            await client.start(phone=phone, code_callback=code, password=password)
            await self.service.activate_user(client)
            completed = True
            await self.service.tell(destination,
                'Login complete. You can now use /save. On Heroku, use /login again after a dyno replacement '
                'unless you have configured optional session restoration.')
        finally:
            if client is not None and not completed:
                await client.close()

    async def _run(self, destination):
        try:
            await asyncio.wait_for(self._authenticate(destination), timeout=self.service.settings.login_timeout)
        except asyncio.TimeoutError:
            await self.service.tell(destination, 'Login expired. Use /login to start again.')
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            self.service.flooded(exc)
        except LoginFlowError as exc:
            await self.service.tell(destination, str(exc))
        except Exception as exc:
            self.service.login_log('failed', error_type=type(exc).__name__)
            await self.service.tell(destination,
                'Login failed. Check your code/password and Telegram account, then use /login again. '
                'No login request was automatically retried.')
        finally:
            if self.waiter is not None and not self.waiter.done():
                self.waiter.cancel()
            self.waiter = self.step = self.owner = None
            self.task = None

    async def close(self):
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.waiter = self.step = self.owner = None
        self.task = None
