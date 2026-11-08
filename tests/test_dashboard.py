"""The live dashboard's engine (closer/dashboard/snapshot.py) and the files the page loads."""
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest

from closer.data import generator
from closer.dashboard import snapshot as snap
from closer.data.generator import write_month

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "docs" / "engine"

_spec = importlib.util.spec_from_file_location("build_dashboard", ROOT / "scripts" / "build_dashboard.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    out = tmp_path_factory.mktemp("golden")
    write_month(out)
    return out


@pytest.fixture(scope="module")
def steps(golden):
    return build.demo_states(golden)["steps"]


def _total(step):
    return step["snapshot"]["totals"]["total_cents"]


def test_seeded_case_ids_match_the_generator():
    assert snap.UNAPPROVED_DISCOUNT_ORDER == generator.UNAPPROVED_DISCOUNT_ORDER
    assert set(snap.SPLIT_ORDERS) == set(generator.SPLIT_ORDERS)
    assert snap.TEAM_SPLIT_ORDER == generator.TEAM_SPLIT_ORDER
    assert snap.FAILED_DELIVERY_ORDER == generator.FAILED_DELIVERY_ORDER
    assert snap.RETURNED_ORDER == generator.RETURNED_ORDER
    assert snap.PRIOR_MONTH_ORDER == generator.PRIOR_MONTH_ORDER
    assert snap.LATE_PAID_ORDER == generator.LATE_PAID_ORDER
    assert build.DISPUTED_ORDER == generator.DISPUTED_ORDER


def test_demo_script_ends_on_the_total_the_demo_page_promises(steps):
    assert _total(steps[-1]) == 807233  # "Expected total: S$8,072.33" on docs/index.html


def test_dispute_holds_the_order_and_the_ruling_moves_it(steps):
    confirmed, disputed, ruled = steps[2], steps[3], steps[5]
    people = lambda s: {p["staff_id"]: p["total_cents"] for p in s["snapshot"]["people"]}  # noqa: E731
    assert people(disputed)["S05"] < people(confirmed)["S05"]
    assert people(disputed)["S04"] == people(confirmed)["S04"]
    assert people(ruled)["S04"] > people(disputed)["S04"]
    view = ruled["disputes"]["1043"]
    assert view["status"] == "ruled" and view["ruled_split"] == {"Ravi": 50, "Aisyah": 50}
    assert view["all"]["after"] == _total(ruled)
    assert view["all"]["held"] == _total(disputed)


def test_unconfirmed_bank_lines_block_the_payout(steps):
    loaded = steps[1]["snapshot"]
    assert not loaded["ready"]
    assert len(loaded["bank"]["unconfirmed"]) == loaded["bank"]["needs_person"] == 8
    assert steps[2]["snapshot"]["ready"]


def test_snapshot_totals_match_the_commission_tool_engine(golden, steps):
    """Same state through compute_month directly, as nyp_compute_commission does."""
    from closer.data.staff import load_staff
    from closer.rules.commission import compute_month
    from closer.rules.plan import load_plans
    from closer.sources.loader import load_period
    from tools._state import apply_split_overrides

    st = steps[5]["state"]
    period = load_period("2026-09", golden, {int(k): v for k, v in st["approved_matches"].items()})
    orders = apply_split_overrides(period.orders, st["split_overrides"], st["open_disputes"])
    result = compute_month(orders, load_plans(), load_staff(), "2026-09", period.prior_payouts)
    assert {p["staff_id"]: p["total_cents"] for p in steps[5]["snapshot"]["people"] if p["total_cents"]} \
        == {k: v for k, v in result.totals().items() if v}


def test_a_match_the_tools_refuse_comes_back_as_an_error(golden):
    out = snap.snapshot({"27": "9999"}, data_dir=golden)
    assert "can only be matched to 1021" in out["error"]


def test_a_ruling_naming_nobody_on_the_order_is_an_error(golden, steps):
    out = snap.snapshot(steps[2]["state"]["approved_matches"], [], {"1043": {"S08": 100}}, data_dir=golden)
    assert "recorded salespeople" in out["error"]


def test_engine_zip_is_up_to_date(golden):
    """Rerun scripts/build_dashboard.py after changing closer/, tools/_state.py or the generator."""
    committed = zipfile.ZipFile(ENGINE / "engine.zip")
    fresh = zipfile.ZipFile(io.BytesIO(build.engine_zip(golden)))
    assert committed.namelist() == fresh.namelist()
    stale = [n for n in fresh.namelist() if committed.read(n) != fresh.read(n)]
    assert not stale, f"docs/engine/engine.zip is stale for {stale}: run scripts/build_dashboard.py"


def test_engine_zip_never_ships_the_answer_key():
    names = zipfile.ZipFile(ENGINE / "engine.zip").namelist()
    assert not [n for n in names if "answer_key" in n or n.endswith("generator.py")]


def test_demo_states_are_up_to_date(golden):
    committed = json.loads((ENGINE / "demo_states.json").read_text("utf-8"))
    assert committed == json.loads(build.demo_states_json(golden)), \
        "docs/engine/demo_states.json is stale: run scripts/build_dashboard.py"
