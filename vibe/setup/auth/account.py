from __future__ import annotations

from vibe.core.config import ProviderConfig
from vibe.setup.auth.api_key_persistence import remove_api_key
from vibe.setup.auth.auth_state import AuthState, assess_auth_state


class AccountSignOutUnavailableError(RuntimeError):
    def __init__(self, state: AuthState) -> None:
        self.state = state
        super().__init__(f"Sign out is unavailable for auth state: {state.kind.value}")


def sign_out_account(provider: ProviderConfig) -> None:
    state = assess_auth_state(provider)
    if not state.sign_out_available:
        raise AccountSignOutUnavailableError(state)
    remove_api_key(provider)


__all__ = ["AccountSignOutUnavailableError", "sign_out_account"]
