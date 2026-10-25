"""Fixtures."""

from unittest.mock import patch

import pytest

from .fah_cloud_mock import MockCloud

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@pytest.fixture
async def cloud(socket_enabled):
    """Fake api.foldingathome.org + node with two linked machines online."""
    mock = MockCloud()
    await mock.start()
    rig1 = mock.add_machine("rig-1")
    rig2 = mock.add_machine("rig-2", gpu=False)
    rig1.online = rig2.online = True
    with (
        patch("custom_components.foldingathome.account.API_URL", f"http://127.0.0.1:{mock.port}"),
        patch("custom_components.foldingathome.account.NODE_SCHEME", "ws"),
    ):
        yield mock
    await mock.stop()
