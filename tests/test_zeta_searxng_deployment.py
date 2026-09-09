from pathlib import Path

import yaml


SETTINGS_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "zeta-searxng" / "settings.yml"
)


def test_zeta_searxng_overlay_is_private_non_secret_and_searchable():
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    settings = yaml.safe_load(text)

    assert settings["use_default_settings"] is True
    assert settings["server"]["bind_address"] == "127.0.0.1"
    assert settings["server"]["port"] == 8888
    assert "secret_key" not in settings["server"]
    assert "SEARXNG_SECRET=" not in text
    assert "json" in settings["search"]["formats"]

    engines = {engine["name"]: engine for engine in settings["engines"]}
    assert engines["yahoo"]["disabled"] is False
    assert engines["bing"]["disabled"] is True
    assert engines["google"]["disabled"] is True
    assert engines["karmasearch"]["disabled"] is True
