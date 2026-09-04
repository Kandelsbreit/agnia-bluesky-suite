from __future__ import annotations

import pytest

from app.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")

