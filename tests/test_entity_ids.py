"""The example dashboard and README use the integration's card."""

from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_example_dashboard_uses_card():
    text = (ROOT / "examples" / "dashboard.yaml").read_text()
    assert "type: custom:foldingathome-card" in text


def test_card_file_is_shipped():
    card = ROOT / "custom_components" / "foldingathome" / "frontend" / "foldingathome-card.js"
    src = card.read_text()
    for tag in ("foldingathome-card", "foldingathome-research-card",
                "foldingathome-card-editor", "foldingathome-research-card-editor"):
        assert f'"{tag}":' in src
    assert "customElements.define(tag, cls)" in src
    assert "foldingathome/dashboard" in src and "foldingathome/research" in src
