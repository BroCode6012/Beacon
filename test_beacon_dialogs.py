import flet as ft
import pytest

from beacon_app import BeaconApp


class FakePage:
    def __init__(self):
        self.opened = None
        self.closed = None

    def open(self, control):
        self.opened = control

    def close(self, control):
        self.closed = control


def _walk(control):
    yield control
    for child in getattr(control, "controls", []) or []:
        yield from _walk(child)
    content = getattr(control, "content", None)
    if content is not None:
        yield from _walk(content)


def _dialog_app():
    app = BeaconApp.__new__(BeaconApp)
    app.page = FakePage()
    app.ch_filters = {
        "sort": "opportunity_score",
        "min_subs": 1000,
        "video_format": "long",
    }
    app.rt_filters = {"min_avg_views": 50000}
    app.ch_chip = "Custom"
    app.rt_shown = 120
    app._render = lambda: None
    return app


@pytest.mark.parametrize("target", ["channels", "realtime"])
def test_filters_dialog_has_visible_non_mutating_close_paths(target):
    app = _dialog_app()
    original = dict(
        app.ch_filters if target == "channels" else app.rt_filters
    )

    app._open_filters(target=target)

    dialog = app.page.opened
    assert isinstance(dialog, ft.AlertDialog)
    assert dialog.modal is False

    close_button = next(
        control
        for control in _walk(dialog.title)
        if isinstance(control, ft.IconButton)
        and control.tooltip == "Close filters"
    )
    close_button.on_click(None)
    assert app.page.closed is dialog
    assert (
        app.ch_filters if target == "channels" else app.rt_filters
    ) == original

    app.page.closed = None
    cancel_button = next(
        control
        for control in _walk(dialog.actions[0])
        if isinstance(control, ft.TextButton) and control.text == "Cancel"
    )
    cancel_button.on_click(None)
    assert app.page.closed is dialog
    assert (
        app.ch_filters if target == "channels" else app.rt_filters
    ) == original
