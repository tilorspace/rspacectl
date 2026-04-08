"""Shared fixtures for rspacectl tests."""

import pytest
from unittest.mock import MagicMock

from rspacectl.context import AppContext, set_context
from rspacectl.output import OutputFormat


@pytest.fixture()
def mock_eln():
    return MagicMock()


@pytest.fixture()
def mock_inv():
    return MagicMock()


@pytest.fixture()
def app_context(mock_eln, mock_inv):
    """Initialise a test AppContext with mock clients."""
    ctx = AppContext(eln=mock_eln, inv=mock_inv, output=OutputFormat.TABLE)
    set_context(ctx)
    return ctx


@pytest.fixture()
def sample_documents():
    return [
        {
            "id": 1,
            "globalId": "SD1",
            "name": "My Experiment",
            "form": {"globalId": "FM1"},
            "lastModified": "2024-01-15T10:30:00.000Z",
            "createdBy": "alice",
        },
        {
            "id": 2,
            "globalId": "SD2",
            "name": "Lab Notes",
            "form": {"globalId": "FM2"},
            "lastModified": "2024-02-20T14:00:00.000Z",
            "createdBy": "bob",
        },
    ]


@pytest.fixture()
def sample_samples():
    return [
        {
            "id": 10,
            "globalId": "SA10",
            "name": "Buffer A",
            "quantity": {"numericValue": 50.0, "unitId": 3},
            "created": "2024-03-01T09:00:00.000Z",
            "owner": {"username": "alice"},
        },
        {
            "id": 11,
            "globalId": "SA11",
            "name": "Enzyme Mix",
            "quantity": {"numericValue": 10.0, "unitId": 3},
            "created": "2024-03-05T11:00:00.000Z",
            "owner": {"username": "alice"},
        },
    ]


@pytest.fixture()
def sample_containers():
    return [
        {
            "id": 100,
            "globalId": "IC100",
            "name": "Freezer A",
            "cType": "GRID",
            "created": "2024-01-01T00:00:00.000Z",
            "owner": {"username": "alice"},
        },
        {
            "id": 101,
            "globalId": "IC101",
            "name": "Shelf 1",
            "cType": "LIST",
            "created": "2024-01-02T00:00:00.000Z",
            "owner": {"username": "bob"},
        },
    ]
