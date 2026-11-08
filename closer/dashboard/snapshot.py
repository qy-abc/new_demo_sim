"""Everything the live dashboard shows, for one close state. No LLM, no I/O beyond the data files.

The dashboard page runs this module in the browser (Pyodide) with the close state it reads
from the bot's State line, so every figure on screen comes from the same functions the
nyp_ tools call: load_period, check_matches, apply_split_overrides and compute_month.

A close state is the three values the agent carries in the chat (see tools/_state.py):
  approved_matches  bank line_no (str or int) -> order_id
  open_disputes     order_ids held for a dispute
  split_overrides   order_id -> {staff_id: percent} from a manager's ruling
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from closer.data.staff import load_staff
from closer.rules.commission import MonthResult, compute_month, round_bp
from closer.rules.plan import load_plans, plan_for
from closer.sources.loader import Period, load_period, suggest_matches
from tools._state import apply_split_overrides, check_matches

MONTH = "2026-09"

# The seeded cases, as named in closer/data/generator.py (tests check they still match).
UNAPPROVED_DISCOUNT_ORDER = "1027"
SPLIT_ORDERS = ("1018", "1061")
TEAM_SPLIT_ORDER = "1020"
FAILED_DELIVERY_ORDER = "1052"
RETURNED_ORDER = "1012"
PRIOR_MONTH_ORDER = "0987"
LATE_PAID_ORDER = "0991"


def reason_group(reason: str) -> str:
    """A hold reason without its amount, so holds can be counted by kind."""
    if reason.startswith("balance due"):
        return "Balance still due"
    return {
        "no salesperson recorded": "No salesperson",
        "not delivered this month": "Not delivered",
        "earned in an earlier month": "Earned in an earlier month",
        "paid in full after this month": "Paid after this month",
        "refunded in the month it was earned": "Refunded in the same month",
        "dispute open": "Dispute open",
        "refunded, but no earlier payout on record to claw back": "Refund with nothing to claw back",
    }.get(reason, reason)


def _state(approved_matches, open_disputes, split_overrides):
    approved = {int(k): str(v) for k, v in (approved_matches or {}).items()}
    disputes = sorted({str(d) for d in (open_disputes or [])})
    overrides = {str(k): {str(s): v for s, v in split.items()} for k, split in (split_overrides or {}).items()}
    return approved, disputes, overrides


def _compute(period: Period, disputes: list[str], overrides: Mapping[str, Mapping[str, float]]) -> tuple[list, MonthResult]:
    orders = apply_split_overrides(period.orders, overrides, disputes)
    result = compute_month(orders, load_plans(), load_staff(), period.month, period.prior_payouts,
                           open_disputes=set(disputes) or None)
    return orders, result


def _people(result: MonthResult, staff, holds_by_staff: Counter) -> list[dict[str, Any]]:
    tier = load_plans()[-1].tier_threshold_cents
    rows = []
    for s in staff:
        if not s.commission_eligible:
            continue
        mine = [line for line in result.lines if line.staff_id == s.staff_id]
        kinds = Counter()
        for line in mine:
            kinds[line.kind] += line.amount_cents
        base = sum(line.base_cents for line in mine if line.kind == "commission")
        rows.append({
            "staff_id": s.staff_id, "name": s.name,
            "total_cents": sum(line.amount_cents for line in mine),
            "commission_cents": kinds["commission"], "bonus_cents": kinds["promo_bonus"],
            "clawback_cents": kinds["clawback"],
            "sales_cents": base, "above_tier_cents": max(0, base - tier),
            "held_count": holds_by_staff[s.staff_id],
        })
    return rows


def _involved(order) -> list[str]:
    ids = list(order.split) or ([order.salesperson] if order.salesperson else [])
    return sorted(set(ids))


def _cases(orders: Mapping[str, Any], result: MonthResult, names: Mapping[str, str]) -> list[dict[str, Any]]:
    """The seeded tricky cases and what the engine did with each, read from its own output."""
    lines_for = lambda oid: [l for l in result.lines if l.order_id == oid]  # noqa: E731
    hold_for = {h.order_id: h.reason for h in result.holds}
    who = lambda ids: ", ".join(names.get(i, i) for i in ids)  # noqa: E731

    def amount(oid: str, kind: str) -> int:
        return sum(l.amount_cents for l in lines_for(oid) if l.kind == kind)

    def effect(oid: str) -> tuple[str, str]:
        if oid in hold_for:
            return "held", "Held, earns nothing"
        claw = amount(oid, "clawback")
        penalised = [l for l in lines_for(oid) if l.kind == "commission" and "without approval" in l.note]
        if penalised:
            bp = plan_for(load_plans(), orders[oid].created_on).discount_penalty_bp
            cut = sum(round_bp(l.base_cents * bp) for l in penalised)
            return "penalty", f"Rate cut {bp / 100:g}pt: −S${cut / 100:,.2f}"
        if claw:
            return "clawback", f"Clawback −S${-claw / 100:,.2f}"
        paid = sum(l.amount_cents for l in lines_for(oid))
        return ("paid", f"Paid S${paid / 100:,.2f}") if paid else ("none", "No commission")

    cases = []
    bonuses = [l for l in result.lines if l.kind == "promo_bonus"]
    this_month = {oid for oid, o in orders.items() if o.created_on.strftime("%Y-%m") == result.month}
    v1 = sorted({l.order_id for l in bonuses if l.note.endswith("plan v1") and l.order_id in this_month})
    v2 = sorted({l.order_id for l in bonuses if l.note.endswith("plan v2") and l.order_id in this_month})
    cases.append({"orders": (v1[:1] + v2[:1]), "title": "Plan change 15 Sep",
                  "text": f"Slumber mattress bonus S$50 before 15 Sep, S$80 from 15 Sep, by order date. "
                          f"{len(v1)} order(s) on v1, {len(v2)} on v2.",
                  "kind": "info", "effect": "Order date decides"})

    def add(oid: str, title: str, text: str) -> None:
        o = orders.get(oid)
        if o is None:
            return
        kind, eff = effect(oid)
        cases.append({"orders": [oid], "title": title, "text": text, "kind": kind, "effect": eff})

    o = orders.get(UNAPPROVED_DISCOUNT_ORDER)
    if o:
        add(o.order_id, "Discount >10%, unapproved",
            f"{o.discount_pct}% off without a manager's approval, so the rate drops by 1 point. "
            f"Seller: {who(_involved(o))}.")
    split_text = []
    for oid in (*SPLIT_ORDERS, TEAM_SPLIT_ORDER):
        if oid in orders:
            sp = orders[oid].split
            split_text.append(f"{oid}: " + " / ".join(f"{names.get(s, s)} {p}%" for s, p in sorted(sp.items(), key=lambda x: -x[1])))
    cases.append({"orders": [*SPLIT_ORDERS, TEAM_SPLIT_ORDER], "title": "Split deals",
                  "text": ". ".join(split_text) + ".", "kind": "info", "effect": "Paid by split %"})
    add(FAILED_DELIVERY_ORDER, "Failed delivery", "The crew chat reports a failed delivery, so nothing is earned yet.")
    add(RETURNED_ORDER, "Returned in 14 days", "Refunded in the month it was earned, so it never pays out.")
    add(PRIOR_MONTH_ORDER, "Refund after payout", "Paid out in August, refunded in September: August's commission comes back.")
    add(LATE_PAID_ORDER, "Aug order paid in Sep", "Earned when the balance is cleared, so it lands in September.")
    return cases


def snapshot(
    approved_matches: Mapping[Any, str] | None = None,
    open_disputes: Iterable[str] | None = None,
    split_overrides: Mapping[str, Mapping[str, float]] | None = None,
    data_dir: str | Path = "data/golden",
    month: str = MONTH,
) -> dict[str, Any]:
    """The whole dashboard for one close state. A state the tools would refuse comes back as error."""
    data_dir = Path(data_dir)
    approved, disputes, overrides = _state(approved_matches, open_disputes, split_overrides)
    staff = load_staff()
    names = {s.staff_id: s.name for s in staff}
    base = load_period(month, data_dir)
    waiting = suggest_matches(base)
    try:
        check_matches(month, data_dir, approved)
        period = load_period(month, data_dir, approved)
        orders, result = _compute(period, disputes, overrides)
    except ValueError as exc:
        return {"month": month, "error": str(exc)}

    by_id = {o.order_id: o for o in orders}
    holds = []
    holds_by_staff: Counter = Counter()
    for h in result.holds:
        o = by_id.get(h.order_id)
        people = _involved(o) if o else []
        for sid in people:
            holds_by_staff[sid] += 1
        holds.append({"order_id": h.order_id, "reason": h.reason, "group": reason_group(h.reason),
                      "staff": [names.get(s, s) for s in people], "net_cents": o.net_total_cents if o else 0})
    groups = Counter(h["group"] for h in holds)
    unconfirmed = sorted(line.line_no for line in period.unmatched_lines)
    credits = [l for l in base.bank_lines if l.credit_cents]
    totals = {k: v for k, v in result.totals().items() if v}
    clawbacks = [{"order_id": l.order_id, "staff_id": l.staff_id, "name": names.get(l.staff_id, l.staff_id),
                  "amount_cents": l.amount_cents} for l in result.lines if l.kind == "clawback"]

    return {
        "month": month,
        "error": None,
        "staff": [{"staff_id": s.staff_id, "name": s.name, "role": s.role,
                   "commission_eligible": s.commission_eligible} for s in staff],
        "state": {"approved_matches": {str(k): v for k, v in sorted(approved.items())},
                  "open_disputes": disputes, "split_overrides": overrides},
        "intake": {
            "orders": len(base.orders),
            "pos": sum(1 for o in base.orders if o.source == "pos"),
            "roadshow": sum(1 for o in base.orders if o.source == "roadshow"),
            "duplicates": [{"row": d.get("row"), "customer": d.get("customer_name"), "amount": d.get("amount"),
                            "salesperson": names.get(d.get("salesperson") or "", d.get("salesperson"))}
                           for d in base.skipped_duplicates],
            "no_salesperson": sorted(o.order_id for o in base.orders if o.salesperson is None),
            "prior_payout_orders": len(base.prior_payouts),
        },
        "bank": {
            "credits": len(credits),
            "needs_person": len(waiting),
            "auto_matched": len(credits) - len(waiting),
            "lines": [{"line_no": s.line_no, "day": s.day.isoformat(), "description": s.description,
                       "amount_cents": s.credit_cents, "candidates": list(s.candidates), "reason": s.reason,
                       "confirmed_order": approved.get(s.line_no),
                       "salesperson": names.get(by_id[approved[s.line_no]].salesperson or "", "")
                       if s.line_no in approved and approved[s.line_no] in by_id else ""}
                      for s in waiting],
            "unconfirmed": unconfirmed,
        },
        "ready": not unconfirmed,
        "people": _people(result, staff, holds_by_staff),
        "totals": {"total_cents": sum(totals.values()),
                   "clawback_cents": sum(c["amount_cents"] for c in clawbacks),
                   "hold_count": len(result.holds), "line_count": len(result.lines),
                   "held_value_cents": sum(h["net_cents"] for h in holds)},
        "holds": holds,
        "hold_groups": [{"group": g, "count": n} for g, n in groups.most_common()],
        "clawbacks": clawbacks,
        "cases": _cases(by_id, result, names),
        "statements": {
            s.staff_id: [{"order_id": l.order_id, "kind": l.kind, "amount_cents": l.amount_cents, "note": l.note}
                         for l in result.lines if l.staff_id == s.staff_id]
            for s in staff if s.commission_eligible
        },
    }


def dispute_view(
    order_id: str,
    approved_matches: Mapping[Any, str] | None = None,
    open_disputes: Iterable[str] | None = None,
    split_overrides: Mapping[str, Mapping[str, float]] | None = None,
    data_dir: str | Path = "data/golden",
    month: str = MONTH,
) -> dict[str, Any]:
    """One disputed order: its record, and each affected person's total before, while held and after a ruling."""
    data_dir = Path(data_dir)
    approved, disputes, overrides = _state(approved_matches, open_disputes, split_overrides)
    names = {s.staff_id: s.name for s in load_staff()}
    try:
        check_matches(month, data_dir, approved)
        period = load_period(month, data_dir, approved)
    except ValueError as exc:
        return {"order_id": order_id, "error": str(exc)}
    order = next((o for o in period.orders if o.order_id == order_id), None)
    if order is None:
        return {"order_id": order_id, "error": f"Order {order_id} does not exist in {month}."}

    others_d = [d for d in disputes if d != order_id]
    others_o = {k: v for k, v in overrides.items() if k != order_id}
    variants = {"before": (others_d, others_o), "held": ([*others_d, order_id], others_o)}
    ruled = overrides.get(order_id)
    if ruled is not None:
        variants["after"] = (others_d, {**others_o, order_id: ruled})
    totals = {k: _compute(period, d, o)[1].totals() for k, (d, o) in variants.items()}
    people = sorted(set(order.split) | set(ruled or {}))
    return {
        "order_id": order_id, "error": None,
        "status": "ruled" if ruled is not None else "held" if order_id in disputes else "none",
        "customer": order.customer_name, "net_cents": order.net_total_cents,
        "created_on": order.created_on.isoformat(),
        "recorded_split": {names.get(s, s): p for s, p in order.split.items()},
        "ruled_split": {names.get(s, s): p for s, p in (ruled or {}).items()} or None,
        "people": [{"staff_id": s, "name": names.get(s, s),
                    **{k: totals[k].get(s, 0) for k in totals}} for s in people],
        "all": {k: sum(t.values()) for k, t in totals.items()},
    }
