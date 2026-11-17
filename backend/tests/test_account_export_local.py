"""Local mode has no accounts, so no "take a copy of my data": the route does not exist there
(the hosted twin is tests/hosted/test_account_export_postgres.py; the app's buttons are hosted
only, frontend/src/shelf/Account.tsx)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "api"))
from test_read_map import map_env as map_env  # noqa: E402,F401


def test_local_mode_offers_no_copy_of_an_account(map_env):
    """The break it catches: the hosted export mounted in local mode, where there is no reader to
    sign in and nothing would scope it to one."""
    api, _bid, app, _settings = map_env
    assert not [path for path in app.openapi()["paths"] if path.startswith("/api/account")]
    assert api.get("/api/account/export").status_code == 404
