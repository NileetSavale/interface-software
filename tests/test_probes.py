"""The guards on what a probe may learn: a detector must be fixed wording that is really on the screen."""
import re

import pytest

from interfaze.discovery.probes import detector, phrase_problem
from interfaze.surface.web import py_pattern

SCREEN = '- cell "No members found for \\"{{inputs.memberId}}\\"." [ref=f5e6]'


def test_fixed_wording_on_screen_is_accepted():
    assert phrase_problem("No members found", SCREEN) is None


@pytest.mark.parametrize("phrase, problem", [
    ("No members found for 99999", "contains a number"),
    ('found for "{{inputs.memberId}}"', "contains a number or an input placeholder"),
    ("Member does not exist", "isn't on the screen"),
    ("No", "too short"),
])
def test_input_specific_or_invented_phrases_are_refused(phrase, problem):
    assert problem in phrase_problem(phrase, SCREEN)


def test_detector_matches_the_message_for_any_input():
    pattern = py_pattern(detector("No members found (for)").text)
    assert isinstance(pattern, re.Pattern)
    assert pattern.search('no members found (for) "88888".')
    assert not pattern.search("No members found for")  # punctuation is matched literally


def test_structure_only_keeps_labels_and_drops_data():
    from interfaze.evidence import structure_only
    snapshot = """- 'cell "Member #:" [ref=f5e9]'
- cell "Rafael Ortiz" [ref=f5e10]
- 'cell "O#Brien" [ref=f5e11]'
- text: Welcome, Rafael
- text: "Balance:"
- columnheader "Balance" [ref=f5e12]
- /url: /account?no=0001-23456-00"""
    out = structure_only(snapshot)
    assert "Member #:" in out and "Balance" in out and "Balance:" in out
    for data in ("Rafael", "Ortiz", "Brien", "23456", "0001"):
        assert data not in out, data
