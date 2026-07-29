import os
import stat
from pathlib import Path

import pytest

from jobsearch_mcp_server.repository import SQLiteRepository


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable")
def test_repository_restricts_personal_data_permissions(tmp_path: Path) -> None:
    data_dir = tmp_path / "personal-data"
    repository = SQLiteRepository(data_dir)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(repository.path.stat().st_mode) == 0o600
