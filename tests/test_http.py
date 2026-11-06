from datetime import UTC, datetime
from pathlib import Path

import pytest

from canon_db.http import save_changed, visible
from canon_db.sources import aca, brave, ksw
from canon_db.sources.pfl.fetch import RANKINGS as PFL
from canon_db.sources.ufcstats.client import RANKINGS as UFC

TESTS = Path(__file__).parent
RANKINGS = {"ufcstats": UFC, "pfl": PFL, "brave": brave.RANKINGS, "aca": aca.RANKINGS, "ksw": ksw.RANKINGS}
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _page(source):
    return (TESTS / source / "fixtures" / "rankings.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("source", RANKINGS)
def test_rankings_marker_is_on_the_real_page(source):
    assert RANKINGS[source][1] in visible(_page(source))


def test_save_changed_saves_only_changes(tmp_path):
    page, marker = _page("ksw"), ksw.RANKINGS[1]
    assert save_changed(tmp_path, page, marker, NOW) == "saved"
    # a new token in a script isn't a change; a new name is
    assert save_changed(tmp_path, page.replace("</body>", "<script>t='x'</script></body>"), marker, LATER) == "unchanged"
    assert save_changed(tmp_path, page.replace("Arkadiusz Wrzosek", "Someone Else"), marker, LATER) == "saved"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-11-05-1200.html", "2026-11-06-1200.html"]


def test_save_changed_skips_a_page_without_its_marker(tmp_path):
    assert save_changed(tmp_path, "<html><body>Access denied</body></html>", UFC[1], NOW) == "not the page"
    assert not list(tmp_path.iterdir())
