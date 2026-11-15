"""The guards on what a probe may learn: a detector must be fixed wording that is really on the screen."""
import re

import pytest

from interface.discovery.probes import detector, phrase_problem
from interface.surface.web import py_pattern

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
    from interface.evidence import structure_only
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


def test_masker_keeps_records_apart_without_their_data():
    from interface.pii import Masker
    m = Masker()
    snapshot = """- table [ref=e1]:
  - rowgroup [ref=e2]:
    - row [ref=e3]:
      - cell "Name:" [ref=e4]
      - cell "Jane Doe" [ref=e5]
    - row [ref=e6]:
      - columnheader "Type" [ref=e7]
      - columnheader "Account" [ref=e8]
      - columnheader "Balance" [ref=e9]
    - row [ref=e10]:
      - cell "Savings" [ref=e11]
      - cell "0001-12345-00" [ref=e12]
      - cell "$4,210.55" [ref=e13]
- text: Call (555) 123-4567 or jane@example.com, SSN 123-45-6789, born 4/1/1980"""
    out = m.snapshot(snapshot)
    for raw in ("Jane Doe", "0001-12345-00", "4,210.55", "555", "jane@", "123-45-6789", "1980"):
        assert raw not in out, raw
    assert 'cell "Savings"' in out and 'cell "Name:"' in out  # labels and categories stay
    # Stable: the same value is the same placeholder later, so the model can still follow one record
    assert m.text("Jane Doe again") == "⟨NAME_1⟩ again"
    # Long identifiers keep last-4 (D16); a short one doesn't, since four digits would nearly give it away
    assert "⟨ACCT_2 ****1234⟩" == m.placeholder("ACCT", "000011111234")
    assert "****" not in m.placeholder("ACCT", "12345")
