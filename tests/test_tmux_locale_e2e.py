"""tmux listings parse under a non-UTF-8 client locale (#881), on a real server.

A tmux client whose locale is not UTF-8 prints every tab of a ``-F`` reply as
``_``, so a tab-joined listing row came back as one field with the rest empty:
windows with no name and no tag, and no fault reported. The backend now spawns
every verb with ``-u``.

Linux only, zero tokens: the windows run ``sleep``. The server runs on a private
``TMUX_TMPDIR``, and the autouse ``_isolate_mux_registry`` fixture removes
``TMUX``, which a client would otherwise follow to the operator's own socket.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from conftest import real_mux_e2e

from bmad_loop.adapters.tmux_backend import TmuxMultiplexer

HAVE_TMUX = sys.platform == "linux" and shutil.which("tmux") is not None


@real_mux_e2e
@pytest.mark.skipif(not HAVE_TMUX, reason="requires Linux with tmux on PATH")
@pytest.mark.parametrize("locale", ["C", None], ids=["lc-all-c", "no-locale"])
def test_e2e_list_windows_parses_under_a_non_utf8_client_locale(tmp_path, monkeypatch, locale):
    socket_dir = tmp_path / "tmux"
    socket_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_dir))
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    if locale is not None:
        monkeypatch.setenv("LC_ALL", locale)
        monkeypatch.setenv("LANG", locale)

    mux = TmuxMultiplexer()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "loc", "-n", "run-x", "sleep 60"], check=True
    )
    try:
        mux.set_window_option("=loc:run-x", "@bmad_tag", "café tag")
        # Ablation: drop TmuxMultiplexer._CLIENT_FLAGS and each row comes back
        # as one field, the window id with the rest of the row behind `_`s.
        rows = mux.list_windows("loc", ["window_id", "window_name", "@bmad_tag"])
        assert [row[1:] for row in rows] == [("run-x", "café tag")]
        assert rows[0][0].startswith("@")
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
