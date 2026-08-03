"""Guards the location of the global classification vault.

``get_global_vault_file_path()`` walked two directories up from
``app/services/media_store.py``, which lands on ``app/`` - not the project root.
Every read therefore opened a non-existent ``app/local_storage/cleaner_vault.json``
and returned zero classifications, so the Cleaner's category pills all read (0)
and every photo fell through to "Other / Misc" while a fully populated vault sat
in ``memoryweaver/local_storage/``.

The path must stay in lockstep with ``StorageHelper.local_base`` - they are two
independent walks to the same project root, and the bug was them disagreeing.
"""

import os

from app.app_utils.storage import StorageHelper
from app.services.media_store import get_global_vault_file_path


def test_vault_lives_beside_the_default_session_storage_root():
    vault_dir = os.path.dirname(get_global_vault_file_path())
    assert vault_dir == StorageHelper("default").local_base


def test_vault_is_not_nested_inside_the_app_package():
    app_package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vault_path = get_global_vault_file_path()

    assert os.path.basename(vault_path) == "cleaner_vault.json"
    assert os.sep + "app" + os.sep not in vault_path
    assert os.path.dirname(vault_path) != os.path.join(app_package, "local_storage")
