import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """
    Provide a fresh instance of the application module backed by
    an isolated desktop data directory under a temporary folder.
    """
    data_dir = tmp_path / "bg_test_data"
    monkeypatch.setenv("BG_DESKTOP", "1")
    monkeypatch.setenv("APPDATA", str(data_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(data_dir))

    # Ensure module re-import picks up the new environment
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    if "app" in sys.modules:
        module = sys.modules["app"]
        with module.app.app_context():
            module.db.session.remove()
        module = importlib.reload(module)
    else:
        module = importlib.import_module("app")

    # Guarantee a clean database schema for each test
    with module.app.app_context():
        module.db.drop_all()
        module.db.create_all()

        info_path = module.get_info_json_path()
        if info_path.exists():
            info_path.unlink()

    module.ONBOARDING_COMPLETE = True
    return module
