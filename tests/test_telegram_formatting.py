from tools import _to_telegram_html


def test_bold_markers_become_html_tags():
    assert _to_telegram_html("**Hi** there") == "<b>Hi</b> there"


def test_special_chars_are_escaped():
    assert _to_telegram_html("A & B <x>") == "A &amp; B &lt;x&gt;"


def test_bold_with_special_chars_inside():
    assert _to_telegram_html("**a & b**") == "<b>a &amp; b</b>"


def test_plain_text_unchanged():
    assert _to_telegram_html("no bold here") == "no bold here"


def test_multiple_bold_spans():
    assert _to_telegram_html("**a** and **b**") == "<b>a</b> and <b>b</b>"
