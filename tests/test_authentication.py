import asyncio
from pathlib import Path

import pytest

from migaku_api.browser import MigakuBrowser, WorkerError
from migaku_api.credentials import CredentialsError, load_credentials
from tests.test_service import run


def write_credentials(path, password="example=password"):
    path.write_text(f"username=test@example.invalid\npassword={password}\n")
    path.chmod(0o600)
    return path


def test_credentials_preserve_equals_and_never_appear_in_repr(tmp_path):
    credentials = load_credentials(write_credentials(tmp_path / "CREDS"))
    assert credentials.email == "test@example.invalid"
    assert credentials.password == "example=password"
    assert credentials.email not in repr(credentials)
    assert credentials.password not in repr(credentials)
    assert load_credentials(tmp_path / "absent") is None


@pytest.mark.parametrize("kind", ["public", "symlink", "directory", "invalid", "oversized"])
def test_unsafe_credentials_are_rejected_without_disclosing_contents(tmp_path, kind):
    path = write_credentials(tmp_path / "CREDS")
    if kind == "public":
        path.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.unlink()
        path.mkdir(mode=0o700)
    elif kind == "invalid":
        path.write_text("test@example.invalid:example=password")
    else:
        path.write_text("password=" + "example=password" * 2000)
    with pytest.raises(CredentialsError) as error:
        load_credentials(path)
    assert "test@example.invalid" not in str(error.value)
    assert "example=password" not in str(error.value)


class AuthBrowser(MigakuBrowser):
    def __init__(self, path):
        super().__init__(path.parent, extension=Path("unused"))
        self.credentials_file = path
        self.context = self
        self.core = {"version": "test", "module": "core.js", "export": "core"}
        self.page = self
        self.extension_id = "test"
        self.authenticated = False
        self.initial_sync_done = True
        self.attempts = 0
        self.success = True
        self.crash = False
        self.auth_check_error = False
        self.passwords = []
        self.login_page_closed = False

    async def call(self, method, params=None, timeout=45):
        if method == "Firebase_waitForAuthCheckDone":
            if self.auth_check_error:
                raise WorkerError("browser_error", "Browser unavailable")
            return self.authenticated
        assert method == "Syncer_isInitialPullSyncDone"
        return self.initial_sync_done

    async def new_page(self):
        self.attempts += 1
        self.login_page_closed = False
        return self

    async def goto(self, url):
        assert url.startswith("chrome-extension://test/")

    def locator(self, selector):
        browser = self
        class Locator:
            async def fill(self, value, timeout):
                if selector == 'input[type="password"]':
                    browser.passwords.append(value)
                else:
                    assert selector == 'input[type="email"]'
                    assert value == "test@example.invalid"

            async def click(self, timeout):
                assert selector == 'button[type="submit"]'

            async def wait_for(self, state, timeout):
                assert selector == 'input[type="email"]' and state == "hidden"
                await browser.finish_login()
        return Locator()

    async def finish_login(self):
        await asyncio.sleep(0)
        if self.crash:
            raise RuntimeError(f"sensitive upstream error: {self.passwords}")
        if not self.success:
            raise TimeoutError("Login form still open")
        self.authenticated = self.success

    async def close(self):
        self.login_page_closed = True


def test_concurrent_recovery_signs_in_once_and_recovers_again_after_expiry(tmp_path):
    browser = AuthBrowser(write_credentials(tmp_path / "CREDS"))
    async def scenario():
        statuses = await asyncio.gather(*(browser.status() for _ in range(8)))
        assert all(status["ready"] and status["authenticated"] for status in statuses)
        assert browser.attempts == 1
        assert browser.login_page_closed
        browser.authenticated = False
        assert (await browser.status())["ready"]
        assert browser.attempts == 2
    run(scenario())


def test_failed_login_is_throttled_and_rereads_changed_credentials(tmp_path):
    path = write_credentials(tmp_path / "CREDS")
    browser = AuthBrowser(path)
    browser.success = False
    async def scenario():
        statuses = await asyncio.gather(*(browser.status() for _ in range(8)))
        assert all(status["error"] == "login_failed" for status in statuses)
        assert browser.attempts == 1
        assert browser.login_page_closed
        write_credentials(path, "replacement-password")
        browser.success = True
        browser.auth_retry_at = 0
        assert (await browser.status())["ready"]
        assert browser.passwords == ["example=password", "replacement-password"]
    run(scenario())


def test_login_exceptions_are_redacted_and_initial_sync_is_required(tmp_path):
    browser = AuthBrowser(write_credentials(tmp_path / "CREDS"))
    browser.crash = True
    async def scenario():
        result = await browser.status()
        assert result["error"] == "login_failed"
        assert "test@example.invalid" not in str(result)
        assert "example=password" not in str(result)
        assert browser.login_page_closed
        browser.crash = False
        browser.auth_retry_at = 0
        browser.initial_sync_done = False
        result = await browser.status()
        assert result["authenticated"] and not result["ready"]
        with pytest.raises(WorkerError, match="initial sync"):
            await browser.require_ready()
    run(scenario())


def test_absent_credentials_keep_manual_login_and_browser_errors_do_not_trigger_login(tmp_path):
    browser = AuthBrowser(tmp_path / "absent")
    assert run(browser.status())["error"] == "login_required"
    assert browser.attempts == 0
    browser = AuthBrowser(write_credentials(tmp_path / "CREDS"))
    browser.auth_check_error = True
    assert run(browser.status())["error"] == "browser_error"
    assert browser.attempts == 0


def test_configurable_credentials_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGAKU_CREDENTIALS_FILE", str(tmp_path / "private-login"))
    assert MigakuBrowser(tmp_path).credentials_file == tmp_path / "private-login"
