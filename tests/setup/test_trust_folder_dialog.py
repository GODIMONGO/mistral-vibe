from __future__ import annotations

from pathlib import Path

import pytest

from vibe.setup.trusted_folders.trust_folder_dialog import TrustFolderDialog


@pytest.mark.parametrize("offer_repo_trust", [False, True])
def test_trust_dialog_defaults_to_decline(offer_repo_trust: bool) -> None:
    dialog = TrustFolderDialog(
        cwd=Path("/workspace/project"),
        repo_root=Path("/workspace") if offer_repo_trust else None,
        detected_files=["AGENTS.md"],
        offer_repo_trust=offer_repo_trust,
    )

    selected_decision, _ = dialog._options[dialog.selected_option]

    assert selected_decision == "decline"
