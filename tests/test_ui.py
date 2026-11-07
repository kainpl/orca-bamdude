"""The tab's page (spec r2 §8): its sentences in both languages, and nothing a text says becomes markup."""

import pytest

from orca_bamdude import i18n
from orca_bamdude.ui import KEYS, render


@pytest.mark.parametrize("lang", ["en", "uk"])
def test_every_sentence_the_page_shows_exists(lang):
    missing = [key for key in KEYS if i18n.say(lang, key) == key]
    assert missing == []


def test_a_sentence_cannot_close_the_script_or_open_markup():
    html = render(lambda key: '</script><img src=x onerror="alert(1)">&')
    script = html.split("<script>", 1)[1]
    assert script.count("</script>") == 1  # the page's own end, nothing a sentence carried
    assert "<img src=x" not in html


def test_the_page_renders_text_nodes_only_and_loads_nothing_from_outside():
    html = render(lambda key: key)
    assert "innerHTML" not in html and "outerHTML" not in html and "insertAdjacentHTML" not in html
    assert "<script src" not in html and "http://" not in html and "https://" not in html


def test_the_page_speaks_orcas_language():
    assert "Перевірити з'єднання" in render(lambda key: i18n.say("uk", key))
