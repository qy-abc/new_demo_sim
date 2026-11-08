"""Build the files the live dashboard (docs/index.html) loads.

    .venv\\Scripts\\python.exe scripts\\build_dashboard.py

GitHub Pages serves only docs/, so the dashboard cannot import closer/ from the repo.
This writes, next to the page:

    docs/engine/engine.zip        closer/ (as deploy.py ships it), tools/_state.py and the six
                                  agent-visible files of data/golden/. Never answer_key.json.
                                  The page unpacks it into Pyodide and calls
                                  closer.dashboard.snapshot, so its figures come from the same
                                  code as the nyp_ tools.
    docs/engine/demo_states.json  the dashboard for each step of the demo script, worked out
                                  here. The page uses it when the engine cannot load, and for
                                  the presenter's step keys.

Rerun it after changing closer/, tools/_state.py or the data; tests/test_dashboard.py fails
while the committed files are out of date. Run `python -m closer.cli generate` first if
data/golden/ is missing.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from closer.dashboard.snapshot import dispute_view, snapshot  # noqa: E402

OUT = ROOT / "docs" / "engine"
GOLDEN = ROOT / "data" / "golden"
AGENT_FILES = ("august_payouts.json", "bank.csv", "crew_messages.json",
               "pos_orders.json", "roadshow.csv", "settlements.csv")
# Same exclusions as deploy.py, plus the dashboard has no use for the event log.
SKIP_NAMES = {"cli.py", "generator.py"}
SKIP_DIRS = {"eval", "__pycache__"}
FIXED_TIME = (2026, 9, 30, 0, 0, 0)  # every member gets this, so the zip is byte-for-byte repeatable

DISPUTED_ORDER = "1043"
RULED_SPLIT = {"S04": 50, "S05": 50}


def engine_files(golden: Path = GOLDEN) -> dict[str, Path]:
    """Archive name -> source file, in a fixed order."""
    files: dict[str, Path] = {}
    for path in sorted((ROOT / "closer").rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or path.suffix not in {".py", ".yaml", ".csv"}:
            continue
        if path.name in SKIP_NAMES or SKIP_DIRS & set(rel.parts):
            continue
        files[rel.as_posix()] = path
    files["tools/__init__.py"] = ROOT / "tools" / "__init__.py"
    files["tools/_state.py"] = ROOT / "tools" / "_state.py"
    missing = [f for f in AGENT_FILES if not (golden / f).is_file()]
    if missing:
        sys.exit(f"data/golden is missing {missing}; run `python -m closer.cli generate` first.")
    for name in AGENT_FILES:
        files[f"data/golden/{name}"] = golden / name
    return files


def engine_zip(golden: Path = GOLDEN) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in engine_files(golden).items():
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            data = path.read_bytes()
            if not name.startswith("data/"):
                # Git stores the sources with LF; a Windows checkout (core.autocrlf) rewrites them
                # with CRLF. Pack LF so the zip is the same whichever checkout built it.
                data = data.replace(b"\r\n", b"\n")
            zf.writestr(info, data)
    return buf.getvalue()


def demo_steps(golden: Path = GOLDEN) -> list[dict]:
    """The close state after each line of the demo script on docs/index.html."""
    base = snapshot(data_dir=golden)
    all_matches = {str(l["line_no"]): l["candidates"][0] for l in base["bank"]["lines"] if len(l["candidates"]) == 1}
    if len(all_matches) != base["bank"]["needs_person"]:
        sys.exit("Some bank line has no single candidate; the demo's 'tick all' step is not defined.")
    ruled = {DISPUTED_ORDER: RULED_SPLIT}
    S = lambda m, d, o, p: {"approved_matches": m, "open_disputes": d, "split_overrides": o, "payout": p}  # noqa: E731
    return [
        {"label": "Nothing run yet", "state": None, "events": []},
        {"label": "Month loaded, 8 bank lines to confirm", "state": S({}, [], {}, "not drafted"), "events": []},
        {"label": "Bank lines confirmed, commission worked out", "state": S(all_matches, [], {}, "not drafted"), "events": []},
        {"label": "Ravi disputes order 1043", "state": S(all_matches, [DISPUTED_ORDER], {}, "not drafted"), "events": []},
        {"label": "Ravi tries to rule: refused", "state": S(all_matches, [DISPUTED_ORDER], {}, "not drafted"),
         "events": [{"kind": "refused", "who": "Ravi (S04)", "text": "Only sales managers may rule: Jason (S02)."}]},
        {"label": "Jason rules 50/50", "state": S(all_matches, [], ruled, "not drafted"), "events": []},
        {"label": "Payout drafted", "state": S(all_matches, [], ruled, "draft"), "events": []},
        {"label": "Jason tries to approve: refused", "state": S(all_matches, [], ruled, "draft"),
         "events": [{"kind": "refused", "who": "Jason (S02)", "text": "Only the owner may: Mr Lim (S01)."}]},
        {"label": "Mr Lim approves", "state": S(all_matches, [], ruled, "approved"), "events": []},
    ]


def demo_states(golden: Path = GOLDEN) -> dict:
    steps = demo_steps(golden)
    for step in steps:
        st = step["state"]
        if st is None:
            continue
        args = (st["approved_matches"], st["open_disputes"], st["split_overrides"])
        step["snapshot"] = snapshot(*args, data_dir=golden)
        if step["snapshot"]["error"]:
            sys.exit(f"Demo step '{step['label']}' is refused by the engine: {step['snapshot']['error']}")
        touched = set(st["open_disputes"]) | set(st["split_overrides"])
        step["disputes"] = {oid: dispute_view(oid, *args, data_dir=golden) for oid in sorted(touched)}
    return {"month": "2026-09", "steps": steps}


def demo_states_json(golden: Path = GOLDEN) -> str:
    return json.dumps(demo_states(golden), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "engine.zip").write_bytes(engine_zip())
    (OUT / "demo_states.json").write_text(demo_states_json(), encoding="utf-8", newline="\n")
    final = json.loads((OUT / "demo_states.json").read_text("utf-8"))["steps"][-1]["snapshot"]["totals"]
    print(f"wrote {OUT / 'engine.zip'} and {OUT / 'demo_states.json'}; "
          f"approved total S${final['total_cents'] / 100:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
