"""v1.13.0: markdown tables are posted as lists, since the Talk phone apps
print a table as raw pipes. No network."""

from user import textstyle

TWO_COL = """Tested from nor:

| Check | Result |
|---|---|
| turn.example.com DNS | resolves to 174.62.181.125 (NAM WAN) |
| TURN allocate over UDP 3478 | OK, relay at 174.62.181.125:62061 |

Done."""


def test_two_column_table_becomes_bold_list():
    out = textstyle.untable(TWO_COL)
    assert "|" not in out
    assert "- **turn.example.com DNS**: resolves to 174.62.181.125 (NAM WAN)" in out
    assert "- **TURN allocate over UDP 3478**: OK, relay at 174.62.181.125:62061" in out
    assert out.startswith("Tested from nor:") and out.endswith("Done.")


def test_wide_table_gets_header_lines():
    text = "| Host | CPU | Disk |\n|:--|--:|---|\n| nor | 12% | 81% |\n| chi | 5% |  |"
    out = textstyle.untable(text)
    assert out.splitlines() == ["**nor**", "  CPU: 12%", "  Disk: 81%", "**chi**", "  CPU: 5%"]


def test_code_fences_and_plain_pipes_untouched():
    fenced = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    assert textstyle.untable(fenced) == fenced
    assert textstyle.untable("run `a | b` now") == "run `a | b` now"


def test_inline_code_pipe_stays_in_its_cell():
    out = textstyle.untable("| cmd | use |\n|---|---|\n| `ps | grep x` | find x |")
    assert out == "- **`ps | grep x`**: find x"


def test_keep_switch(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_TABLES", "keep")
    assert textstyle.untable(TWO_COL) == TWO_COL


def test_tidy_does_both_and_body_only_tables_the_message():
    assert textstyle.tidy("| a | b |\n|---|---|\n| x | 5–10 |") == "- **x**: 5-10"
    body = textstyle.undash_body({"message": "| a | b |\n|---|---|\n| x | y |", "title": "t"})
    assert body["message"] == "- **x**: y"
