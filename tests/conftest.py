import logging

import pytest

from offer_finder.storage import db


@pytest.fixture
def conn():
    """A fresh in-memory database with the schema applied."""
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _restore_logging():
    """runner.setup_logging() changes the root logger; undo that after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
