"""IT-21 CORNER-066: the demo script follows route order — normal → vitals
abnormal → person absent, cycling past 3 seats; 1–2 seats take the head of it.
"""
from __future__ import annotations

import pytest

from common_types import StepAction
from routers.patrol import build_patrol_steps
from services.demo_data import scenario_for

N, A, X = "normal", "abnormal", "absent"


@pytest.mark.parametrize("seats, expected", [
    (1, [N]),
    (2, [N, A]),
    (3, [N, A, X]),
    (4, [N, A, X, N]),
    (7, [N, A, X, N, A, X, N]),
])
def test_scenario_follows_route_order(seats, expected):
    assert [scenario_for(i) for i in range(seats)] == expected


@pytest.mark.parametrize("seats", [1, 2, 3, 4, 7])
def test_demo_steps_carry_the_scenario_in_route_order(seats):
    beds = [{"bed_key": f"S{i}", "location_id": f"loc-{i}"} for i in range(seats)]
    steps = build_patrol_steps(beds, shelf_id="S_04", mode="demo")
    scans = [s for s in steps if s.action == StepAction.DEMO_SCAN.value]
    assert [s.params["scenario"] for s in scans] == [scenario_for(i) for i in range(seats)]
    assert [s.params["bed_key"] for s in scans] == [b["bed_key"] for b in beds]


def test_skipped_invalid_beds_do_not_consume_a_scenario():
    beds = [
        {"bed_key": "S1", "location_id": "loc-1"},
        {"bed_key": "", "location_id": "loc-x"},   # dropped by the builder
        {"bed_key": "S2", "location_id": "loc-2"},
    ]
    steps = build_patrol_steps(beds, shelf_id="S_04", mode="demo")
    scans = [s for s in steps if s.action == StepAction.DEMO_SCAN.value]
    assert [s.params["scenario"] for s in scans] == [N, A]
