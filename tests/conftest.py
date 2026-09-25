import pytest

from offer_finder.storage import db


@pytest.fixture
def conn():
    """A fresh in-memory database with the schema applied."""
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()
