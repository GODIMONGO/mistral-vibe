from __future__ import annotations

from unittest.mock import Mock

import pytest

from vibe.core.config import ProviderConfig
from vibe.setup.auth import account
from vibe.setup.auth.account import AccountSignOutUnavailableError
from vibe.setup.auth.auth_state import AuthState, AuthStateKind


def _state(kind: AuthStateKind, *, sign_out_available: bool) -> AuthState:
    return AuthState(
        kind=kind,
        can_use_active_provider=kind is not AuthStateKind.SIGNED_OUT,
        sign_out_available=sign_out_available,
        env_key="MISTRAL_API_KEY",
    )


def test_sign_out_account_removes_owned_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ProviderConfig(
        name="mistral",
        api_base="https://api.mistral.ai/v1",
        api_key_env_var="MISTRAL_API_KEY",
    )
    remove = Mock()
    monkeypatch.setattr(
        account,
        "assess_auth_state",
        Mock(return_value=_state(AuthStateKind.OS_KEYRING, sign_out_available=True)),
    )
    monkeypatch.setattr(account, "remove_api_key", remove)

    account.sign_out_account(provider)

    remove.assert_called_once_with(provider)


def test_sign_out_account_rejects_parent_process_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ProviderConfig(
        name="mistral",
        api_base="https://api.mistral.ai/v1",
        api_key_env_var="MISTRAL_API_KEY",
    )
    remove = Mock()
    state = _state(AuthStateKind.PROCESS_ENV, sign_out_available=False)
    monkeypatch.setattr(account, "assess_auth_state", Mock(return_value=state))
    monkeypatch.setattr(account, "remove_api_key", remove)

    with pytest.raises(AccountSignOutUnavailableError) as error:
        account.sign_out_account(provider)

    assert error.value.state is state
    remove.assert_not_called()
