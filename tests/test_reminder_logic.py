"""Reminder-Entscheidung: nur wenn es zum Wochenziel eng wird."""

from __future__ import annotations

from datetime import date

import pytest

from trainer.jobs.reminder_check import parse_goal, should_remind

MON = date(2026, 8, 31)
WED = date(2026, 9, 2)
FRI = date(2026, 9, 4)
SAT = date(2026, 9, 5)
SUN = date(2026, 9, 6)


@pytest.mark.parametrize(
    "done, goal, today, trained_today, expected",
    [
        (0, 3, MON, False, False),  # 7 Tage für 3 Einheiten — entspannt
        (0, 3, WED, False, False),  # 5 Tage für 3 — noch Puffer
        (0, 3, FRI, False, True),  # 3 Tage für 3 — jetzt wird's eng
        (1, 3, SAT, False, True),  # 2 Tage für 2
        (2, 3, SUN, False, True),  # letzter Tag, eine fehlt
        (2, 3, SUN, True, False),  # heute schon trainiert
        (3, 3, SUN, False, False),  # Ziel erreicht
        (5, 3, SAT, False, False),  # übererfüllt
    ],
)
def test_should_remind(done, goal, today, trained_today, expected):
    assert should_remind(done, goal, today, trained_today) is expected


@pytest.mark.parametrize("raw, expected", [(None, 3), ("4", 4), ("2.0", 2), ("0", 3), ("abc", 3)])
def test_parse_goal(raw, expected):
    assert parse_goal(raw) == expected
