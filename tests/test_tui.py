"""The shell opens, says it is empty, and quits on `q`."""

from __future__ import annotations

from textual.widgets import Static

from proof_headless.tui.app import EMPTY, ProofApp


async def test_the_shell_shows_the_empty_state_and_quits_on_q() -> None:
    app = ProofApp()
    async with app.run_test() as pilot:
        body = app.query_one("#body", Static)
        assert str(body.content) == EMPTY
        await pilot.press("q")
    assert app.return_code == 0
