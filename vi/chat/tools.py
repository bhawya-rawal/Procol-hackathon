"""Tools the vendor chatbot may call. Each is scoped to ONE vendor, returns only that vendor's own or
relative data, and is passed through the ConfidentialityGuard before the engine ever sees it.

Both engines (offline router, Claude tool-use) call exactly these functions — there is no other data path.
"""
from __future__ import annotations

import json
import sqlite3

from ..db import one, rows
from ..guard import ConfidentialityGuard
from ..postmortem.service import build_post_mortem
from ..priceband import winning_price_band
from ..vendor_views import vendor_events, vendor_habits, vendor_profile_card

TOOL_SPECS = [
    {"name": "my_events", "description": "List this vendor's recent closed events with outcome (won / lost / no bid), buyer, category, and whether a negotiation window was missed. Optional filters.",
     "input_schema": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": ["won", "lost", "no_bid", "any"]},
         "category": {"type": "string"}, "buyer": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "why_did_i_lose", "description": "Post-mortem for one event: price gap to L1 (relative only), timing, technical (if the buyer allows), terms, plus a next-time action. Needs trade_request_id from my_events.",
     "input_schema": {"type": "object", "properties": {"trade_request_id": {"type": "integer"}}, "required": ["trade_request_id"]}},
    {"name": "price_band", "description": "Win rate by %-gap-to-L1 bucket per category, from this vendor's own bids only. Answers 'what price should I quote'.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}}}},
    {"name": "habits", "description": "Participation habits: invites, mail open rate, bid rate, response time, counter-offer response/lateness, short-lead vs long-lead behaviour, decline reasons.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "delivery", "description": "On-time delivery rate, QC pass rate, number of orders, 90d-vs-365d trend.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "technical", "description": "Own technical evaluation averages and weakest section (only where buyers' policy allows).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "profile", "description": "Profile completeness, missing documents, buyers mapped, pending onboardings, categories supplied.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "buyer_summary", "description": "This vendor's record with ONE buyer: invites, bid rate, win rate, gap to L1, feedback policy in effect.",
     "input_schema": {"type": "object", "properties": {"buyer": {"type": "string"}}, "required": ["buyer"]}},
    {"name": "event_detail", "description": "Everything about ONE event from this vendor's side: the items and quantities asked, the buyer's target price per item, this vendor's own quoted unit prices and totals per bid round, payment/delivery terms asked vs quoted, invite/open/bid timeline, counter-offer and best-offer requests and how the vendor responded, outcome, and % gap to L1. Never includes other vendors.",
     "input_schema": {"type": "object", "properties": {"trade_request_id": {"type": "integer"}}, "required": ["trade_request_id"]}},
    {"name": "category_summary", "description": "This vendor's record per category (all buyers): invites, bid rate, win rate, avg gap to L1, avg rank, last active. Use for 'which category am I strongest in'.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "compare_periods", "description": "This vendor's key metrics for the last 90 days vs last 365 days vs all time. Use for 'am I improving', 'trend', 'this quarter'.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "my_buyers", "description": "Buyers this vendor is mapped to, mapping status, vendor code, and each buyer's feedback policy.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "demand_by_month", "description": "Month-by-month buying activity per product category over the last N months, built from the events THIS vendor was invited to. Answers 'when do my buyers buy', seasonality, and what to stock. Not total market demand.",
     "input_schema": {"type": "object", "properties": {
         "category": {"type": "string"}, "months": {"type": "integer", "description": "How many months back, 3-36. Default 12."}}}},
    {"name": "bid_revision_behaviour", "description": "First bid vs final bid per event for this vendor: how much they moved, how many revisions they made, and their win rate at 0 / 1 / 2+ revisions. Answers 'does revising help me', 'do I quote once and leave'.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "description": "How many recent events to list. Default 15."}}}},
]


class VendorTools:
    def __init__(self, conn: sqlite3.Connection, vendor: int):
        self.c, self.v = conn, vendor
        me = one(conn, "SELECT name FROM companies WHERE id=?", (vendor,))
        if not me:
            raise LookupError(f"vendor {vendor} not found")
        self.name = me["name"]
        others = [r["name"] for r in rows(conn, "SELECT name FROM companies WHERE category='vendor' AND id<>?", (vendor,))]
        self.guard = ConfidentialityGuard(vendor, self.name, "", others)
        self.policy = self._wide_policy()
        self.audit: list[dict] = []

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict | None = None) -> dict:
        args = args or {}
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        out = fn(**{k: v for k, v in args.items() if v not in (None, "")})
        # technical/why_did_i_lose already enforce buyer policy at the row level; everything else is relative/own data
        res = self.guard.apply(out, "relative_plus_technical" if name in ("technical", "why_did_i_lose", "event_detail") else "relative_only")
        self.audit.extend(res.audit)
        return res.payload

    def _wide_policy(self) -> str:
        ps = [r["p"] for r in rows(self.c, """SELECT DISTINCT eg.vendor_feedback_policy AS p FROM audiences a
              JOIN trade_requests tr ON tr.id=a.trade_request_id JOIN event_groups eg ON eg.id=tr.event_group_id
              WHERE a.vendor_company_id=?""", (self.v,))]
        return "relative_plus_technical" if ps and all(p == "relative_plus_technical" for p in ps) else "relative_only"

    # ------------------------------------------------------------------ tools
    def t_my_events(self, outcome: str = "any", category: str | None = None, buyer: str | None = None, limit: int = 10) -> dict:
        evs = vendor_events(self.c, self.v)
        out = []
        for e in evs:
            o = "won" if e["won"] else "lost" if e["bid"] else "no_bid"
            if outcome != "any" and o != outcome:
                continue
            if category and category.lower() not in (e["category"] or "").lower():
                continue
            if buyer and buyer.lower() not in (e["buyer"] or "").lower():
                continue
            out.append({"trade_request_id": e["trade_request_id"], "title": e["title"], "buyer": e["buyer"],
                        "category": e["category"], "rfx_mode": e["rfx_mode"], "closed_at": e["closed_at"][:10],
                        "outcome": o, "missed_negotiation_window": bool(e["missed_window"]),
                        "feedback_available": e["policy"] != "none"})
        total = {"won": sum(1 for e in evs if e["won"]), "lost": sum(1 for e in evs if e["bid"] and not e["won"]),
                 "no_bid": sum(1 for e in evs if not e["bid"])}
        return {"events": out[: int(limit)], "matched": len(out), "totals_all_time": total}

    def t_why_did_i_lose(self, trade_request_id: int) -> dict:
        ok = one(self.c, "SELECT 1 FROM audiences WHERE trade_request_id=? AND vendor_company_id=?", (int(trade_request_id), self.v))
        if not ok:
            return {"error": "You were not invited to that event, so there is nothing to show."}
        pm = build_post_mortem(self.c, self.v, int(trade_request_id), force=True)
        self.audit.extend(pm["guard_audit"])
        if not pm["available"]:
            return {"title": pm["title"], "available": False,
                    "message": f"{pm['title']}: this buyer does not share event feedback with vendors (policy = none)."}
        return {"title": pm["title"], "category": pm["category"], "won": pm["won"], "available": True,
                "narration": pm["narration"], "next_action": pm["next_action"],
                "checks": {k: {"severity": v["severity"], "signal": v["signal"]} for k, v in pm["checks"].items()}}

    def t_price_band(self, category: str | None = None) -> dict:
        pb = winning_price_band(self.c, self.v)
        cats = pb["categories"]
        if category:
            cats = [c for c in cats if category.lower() in c["category"].lower()]
        return {"categories": [{"category": c["category"], "total_bids": c["total_bids"], "insight": c["insight"],
                                "buckets": [{"gap_to_l1": b["bucket"], "bids": b["bids"], "wins": b["wins"], "win_rate": b["win_rate"]}
                                            for b in c["buckets"]]} for c in cats]}

    def t_habits(self) -> dict:
        h = vendor_habits(self.c, self.v)
        h.pop("technical", None); h.pop("delivery", None)
        return h

    def t_delivery(self) -> dict:
        return vendor_habits(self.c, self.v)["delivery"]

    def t_technical(self) -> dict:
        """Own technical scores, but ONLY from events whose buyer allows technical feedback and whose scores
        are unrestricted. Each buyer's policy is respected at the row level, so a vendor with one
        'none' buyer still learns from the buyers who do share."""
        secs = rows(self.c, """
            SELECT e.section_key, ROUND(AVG(e.score/e.max_score)*100,1) AS pct, COUNT(*) AS n,
                   COUNT(DISTINCT eg.company_id) AS buyers
            FROM evaluation_scores e JOIN trade_requests tr ON tr.id=e.trade_request_id
            JOIN event_groups eg ON eg.id=tr.event_group_id
            WHERE e.vendor_company_id=? AND e.score_type='unrestricted'
              AND eg.vendor_feedback_policy='relative_plus_technical'
            GROUP BY 1 ORDER BY pct""", (self.v,))
        if not secs:
            return {"available": False, "message": "No buyer who shares technical feedback has evaluated you yet."}
        avg = round(sum(s["pct"] for s in secs) / len(secs), 1)
        return {"available": True, "avg_pct": avg, "weakest_section": secs[0]["section_key"], "weakest_pct": secs[0]["pct"],
                "sections": secs, "note": "Only events from buyers who share technical feedback are included."}

    def t_profile(self) -> dict:
        p = vendor_profile_card(self.c, self.v)
        return {"completeness_pct": p["completeness_pct"], "missing": [k for k, v in p["fields"].items() if not v],
                "buyers": [{"buyer": b["buyer"], "status": b["status"]} for b in p["buyers"]],
                "pending_onboarding": p["pending_onboarding"], "categories": p["categories"]}

    def t_buyer_summary(self, buyer: str) -> dict:
        b = one(self.c, "SELECT id, name FROM companies WHERE category='buyer' AND lower(name) LIKE ?", (f"%{buyer.lower()}%",))
        if not b:
            return {"error": f"No buyer matching '{buyer}'."}
        p = one(self.c, "SELECT * FROM vendor_profiles WHERE buyer_company_id=? AND vendor_company_id=? AND category_id=0 AND window='all'", (b["id"], self.v))
        pol = one(self.c, "SELECT MAX(vendor_feedback_policy) AS p FROM event_groups WHERE company_id=?", (b["id"],))["p"]
        if not p:
            return {"buyer": b["name"], "message": "No history with this buyer yet.", "feedback_policy": pol}
        return {"buyer": b["name"], "feedback_policy": pol, "invites": p["invites"], "bid_rate": p["bid_rate"],
                "win_rate": p["win_rate"], "events_bid": p["events_bid"], "avg_gap_to_l1_pct": p["avg_gap_to_l1_pct"],
                "late_counter_offer_rate": p["late_counter_offer_rate"], "on_time_delivery_rate": p["on_time_delivery_rate"],
                "last_active_at": p["last_active_at"]}


# ---------------------------------------------------------------------- extra tools (own-data only)
def _event_detail(self: "VendorTools", trade_request_id: int) -> dict:
    tr = int(trade_request_id)
    inv = one(self.c, """SELECT a.invited_at, a.mail_opened_at, tr.rfx_mode, tr.bid_start_time, tr.bid_end_time, tr.closed_at,
                                eg.title, bc.name AS buyer, eg.vendor_feedback_policy AS policy
                         FROM audiences a JOIN trade_requests tr ON tr.id=a.trade_request_id
                         JOIN event_groups eg ON eg.id=tr.event_group_id JOIN companies bc ON bc.id=eg.company_id
                         WHERE a.trade_request_id=? AND a.vendor_company_id=?""", (tr, self.v))
    if not inv:
        return {"error": "You were not invited to that event."}
    items = rows(self.c, """SELECT tp.id AS trade_product_id, p.name AS item, p.unit, tp.quantity, tp.target_price AS buyer_target_price,
                                   tp.asked_payment_terms_days, tp.asked_delivery_days
                            FROM trade_products tp JOIN products p ON p.id=tp.product_id WHERE tp.trade_request_id=?""", (tr,))
    bids = rows(self.c, """SELECT b.id, b.created_at, b.bid_tag AS round, b.status FROM bids b
                           WHERE b.trade_request_id=? AND b.vendor_company_id=? ORDER BY b.created_at""", (tr, self.v))
    for b in bids:
        b["lines"] = rows(self.c, """SELECT p.name AS item, btp.price AS my_unit_price, btp.quantity, btp.payment_terms_days, btp.delivery_days
                                     FROM bid_trade_products btp JOIN trade_products tp ON tp.id=btp.trade_product_id
                                     JOIN products p ON p.id=tp.product_id WHERE btp.bid_id=?""", (b["id"],))
        b["my_total"] = round(sum(l["my_unit_price"] * l["quantity"] for l in b["lines"]), 2)
        b.pop("id")
    negotiation = rows(self.c, """SELECT change_type, created_at, ends_at, resolution, resolved_at FROM additional_requests
                                  WHERE trade_request_id=? AND vendor_company_id=? ORDER BY created_at""", (tr, self.v))
    from ..postmortem.checks import POSITION_SQL
    pos = one(self.c, POSITION_SQL, {"tr": tr, "vendor": self.v}) or {}
    won = one(self.c, "SELECT 1 FROM proposals WHERE trade_request_id=? AND vendor_company_id=? AND status='selected'", (tr, self.v))
    out = {**inv, "trade_request_id": tr, "items_asked": items, "my_bids": bids, "negotiation_requests": negotiation,
           "outcome": "won" if won else ("lost" if bids else "no_bid"),
           "my_position": {"gap_to_l1_pct": round(pos["gap_to_l1_pct"], 1) if pos.get("gap_to_l1_pct") is not None else None,
                           "rank_position": pos.get("rank_position"), "n_bidders": pos.get("n_bidders")} if pos else None}
    if inv["policy"] == "none":
        out["my_position"] = None
        out["note"] = "This buyer does not share event feedback; relative position is withheld."
    return out


def _category_summary(self: "VendorTools") -> dict:
    return {"categories": rows(self.c, """
        SELECT pc.name AS category, vp.invites, vp.events_bid, vp.bid_rate, vp.win_rate, vp.avg_gap_to_l1_pct, vp.avg_rank, vp.last_active_at
        FROM vendor_profiles vp JOIN product_categories pc ON pc.id=vp.category_id
        WHERE vp.buyer_company_id=0 AND vp.vendor_company_id=? AND vp.window='all' AND vp.category_id<>0
        ORDER BY vp.invites DESC""", (self.v,))}


def _compare_periods(self: "VendorTools") -> dict:
    keys = ["invites", "events_bid", "bid_rate", "win_rate", "avg_gap_to_l1_pct", "late_counter_offer_rate",
            "on_time_delivery_rate", "avg_response_hours"]
    out = {}
    for w in ("90d", "365d", "all"):
        r = one(self.c, "SELECT * FROM vendor_profiles WHERE buyer_company_id=0 AND vendor_company_id=? AND category_id=0 AND window=?", (self.v, w))
        out[w] = {k: r[k] for k in keys} if r else None
    return out


def _my_buyers(self: "VendorTools") -> dict:
    return {"buyers": rows(self.c, """
        SELECT bc.name AS buyer, m.status AS mapping_status, m.vendor_code,
               (SELECT MAX(vendor_feedback_policy) FROM event_groups eg WHERE eg.company_id=bc.id) AS feedback_policy,
               (SELECT COUNT(*) FROM audiences a JOIN trade_requests tr ON tr.id=a.trade_request_id
                 JOIN event_groups eg ON eg.id=tr.event_group_id WHERE eg.company_id=bc.id AND a.vendor_company_id=m.dealing_with_company_id) AS invites
        FROM buyer_seller_company_mappings m JOIN companies bc ON bc.id=m.client_company_id
        WHERE m.dealing_with_company_id=? ORDER BY invites DESC""", (self.v,))}


def _demand_by_month(self: "VendorTools", category: str | None = None, months: int = 12) -> dict:
    """Month-by-month buying activity in the categories this vendor supplies, built ONLY from the events this
    vendor was invited to. That keeps it own-data (no other vendor is involved) while still answering
    'when do my buyers actually buy' for stocking decisions."""
    months = max(3, min(int(months), 36))
    anchor = one(self.c, """SELECT MAX(tr.bid_start_time) AS d FROM trade_requests tr
                            JOIN audiences a ON a.trade_request_id=tr.id AND a.vendor_company_id=?""", (self.v,))
    if not anchor or not anchor["d"]:
        return {"categories": [], "message": "You have not been invited to any events yet, so there is no demand history to show."}
    cells = rows(self.c, """
        SELECT strftime('%Y-%m', tr.bid_start_time) AS month, pc.name AS category, p.unit AS unit,
               COUNT(DISTINCT tr.id) AS events, ROUND(SUM(tp.quantity), 2) AS quantity,
               COUNT(DISTINCT CASE WHEN EXISTS (SELECT 1 FROM bids b WHERE b.trade_request_id=tr.id
                                                AND b.vendor_company_id=:v) THEN tr.id END) AS events_you_bid
        FROM audiences a
        JOIN trade_requests tr ON tr.id=a.trade_request_id
        JOIN trade_products tp ON tp.trade_request_id=tr.id
        JOIN products p ON p.id=tp.product_id
        JOIN product_categories pc ON pc.id=p.product_category_id
        WHERE a.vendor_company_id=:v
          AND tr.bid_start_time >= date(:anchor, 'start of month', '-' || :back || ' months')
        GROUP BY 1, 2, 3 ORDER BY 1, 2""",
        {"v": self.v, "anchor": anchor["d"][:10], "back": months - 1})
    return _shape_demand(cells, anchor["d"][:7], months, category)


def _shape_demand(cells: list[dict], to_month: str, months: int, category: str | None) -> dict:
    by_cat: dict[str, dict] = {}
    for c in cells:
        if category and category.lower() not in c["category"].lower():
            continue
        e = by_cat.setdefault(c["category"], {"category": c["category"], "units": set(), "events": 0,
                                              "quantity": 0.0, "events_you_bid": 0, "by_month": {}})
        e["units"].add(c["unit"])
        e["events"] += c["events"]
        e["quantity"] += c["quantity"] or 0.0
        e["events_you_bid"] += c["events_you_bid"]
        m = e["by_month"].setdefault(c["month"], {"month": c["month"], "events": 0, "quantity": 0.0, "events_you_bid": 0})
        m["events"] += c["events"]
        m["quantity"] += c["quantity"] or 0.0
        m["events_you_bid"] += c["events_you_bid"]

    out = []
    for e in sorted(by_cat.values(), key=lambda x: -x["events"]):
        ms = sorted(e["by_month"].values(), key=lambda m: m["month"])
        for m in ms:
            m["quantity"] = round(m["quantity"], 2)
        peak = max(ms, key=lambda m: (m["quantity"], m["events"])) if ms else None
        out.append({"category": e["category"], "units": sorted(e["units"]), "events": e["events"],
                    "total_quantity": round(e["quantity"], 2), "events_you_bid": e["events_you_bid"],
                    "months_with_demand": len(ms), "months_covered": months,
                    "avg_events_per_active_month": round(e["events"] / len(ms), 2) if ms else 0,
                    "peak_month": peak["month"] if peak else None,
                    "peak_month_quantity": peak["quantity"] if peak else None,
                    "by_month": ms,
                    "confidence": "low" if len(ms) < 6 or e["events"] < 8 else "medium"})
    return {"window_months": months, "through_month": to_month, "categories": out,
            "scope": "Counts only events you were invited to, in the categories you supply. This is your own demand "
                     "pipeline, not total market demand.",
            "note": "Quantities mix units where a category uses more than one; check `units` before stocking to a number. "
                    "Months with no invitation are absent, not zero-demand."}


def _bid_revision_behaviour(self: "VendorTools", limit: int = 15) -> dict:
    """First bid vs final bid per event, how often this vendor revises, and what revising is worth to them."""
    evs = rows(self.c, """
        WITH totals AS (
            SELECT b.id AS bid_id, b.trade_request_id AS tr, b.created_at, b.bid_tag,
                   SUM(btp.price * btp.quantity) AS total
            FROM bids b JOIN bid_trade_products btp ON btp.bid_id = b.id
            WHERE b.vendor_company_id = :v GROUP BY b.id),
        seq AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY tr ORDER BY created_at) AS rn,
                      COUNT(*)     OVER (PARTITION BY tr)                     AS n
            FROM totals)
        SELECT f.tr AS trade_request_id, eg.title, pc.name AS category, tr.rfx_mode, tr.closed_at,
               f.n - 1 AS revisions, ROUND(f.total, 2) AS first_bid_total, ROUND(l.total, 2) AS final_bid_total,
               ROUND(l.total - f.total, 2) AS delta_amount,
               CASE WHEN f.total > 0 THEN ROUND((l.total - f.total) / f.total * 100, 2) END AS delta_pct,
               l.bid_tag AS final_round,
               CASE WHEN EXISTS (SELECT 1 FROM proposals pr WHERE pr.trade_request_id = f.tr
                                 AND pr.vendor_company_id = :v AND pr.status = 'selected')
                    THEN 1 ELSE 0 END AS won
        FROM seq f
        JOIN seq l ON l.tr = f.tr AND l.rn = l.n
        JOIN trade_requests tr ON tr.id = f.tr
        JOIN event_groups eg ON eg.id = tr.event_group_id
        LEFT JOIN product_categories pc ON pc.id = (
            SELECT p.product_category_id FROM trade_products tp JOIN products p ON p.id = tp.product_id
            WHERE tp.trade_request_id = tr.id LIMIT 1)
        WHERE f.rn = 1 AND tr.status = 'closed'
        ORDER BY tr.closed_at DESC""", {"v": self.v})
    if not evs:
        return {"events_bid": 0, "message": "You have not placed a bid on any closed event yet, so there is nothing to compare."}

    buckets: dict[int, dict] = {}
    for e in evs:
        key = min(e["revisions"], 2)
        b = buckets.setdefault(key, {"revisions": key, "events": 0, "wins": 0, "_drop": []})
        b["events"] += 1
        b["wins"] += e["won"]
        if e["delta_pct"] is not None:
            b["_drop"].append(-e["delta_pct"])
    by_rev = []
    for b in sorted(buckets.values(), key=lambda x: x["revisions"]):
        drops = b.pop("_drop")
        by_rev.append({**b, "win_rate": round(b["wins"] / b["events"], 3),
                       "avg_price_drop_pct": round(sum(drops) / len(drops), 2) if drops else 0.0,
                       "label": "quoted once, never revised" if b["revisions"] == 0
                                else f"revised {b['revisions']}x" + ("+" if b["revisions"] == 2 else "")})
    revised = [e for e in evs if e["revisions"] > 0]
    once = next((b for b in by_rev if b["revisions"] == 0), None)
    more = [b for b in by_rev if b["revisions"] > 0]
    if once and more:
        w_more = sum(b["wins"] for b in more) / sum(b["events"] for b in more)
        lift = round((w_more - once["win_rate"]) * 100, 1)
        insight = (f"You revised on {round(len(revised) / len(evs) * 100)}% of the events you bid on. "
                   f"Win rate when you revised: {round(w_more * 100)}%, versus {round(once['win_rate'] * 100)}% when you "
                   f"quoted once and left — {'+' if lift >= 0 else ''}{lift} points.")
    elif once:
        insight = "You have never revised a bid. Every event you bid on, you quoted once and left."
    else:
        insight = "You revised on every event you bid on."
    return {"events_bid": len(evs), "events_revised": len(revised),
            "revision_rate": round(len(revised) / len(evs), 3),
            "by_revisions": by_rev, "insight": insight,
            "events": [dict({k: e[k] for k in ("trade_request_id", "title", "category", "rfx_mode", "closed_at",
                                               "revisions", "first_bid_total", "final_bid_total", "delta_amount",
                                               "delta_pct", "final_round")},
                            outcome="won" if e["won"] else "lost") for e in evs[: int(limit)]],
            "note": "All figures are your own bids only. A negative delta_pct means your final bid was lower than your first."}


VendorTools.t_event_detail = _event_detail
VendorTools.t_category_summary = _category_summary
VendorTools.t_compare_periods = _compare_periods
VendorTools.t_my_buyers = _my_buyers
VendorTools.t_demand_by_month = _demand_by_month
VendorTools.t_bid_revision_behaviour = _bid_revision_behaviour


def context_summary(tools: VendorTools) -> dict:
    """Compact always-on context for the system prompt / greeting."""
    ev = tools.call("my_events", {"limit": 12})
    hab = tools.call("habits")
    prof = tools.call("profile")
    buyers = tools.call("my_buyers")
    return {"vendor": tools.name, "today": "2026-09-11", "totals": ev.get("totals_all_time"),
            "recent_events": ev.get("events"),
            "habits": {k: hab.get(k) for k in ("invites", "mail_open_rate", "bid_rate", "win_rate", "avg_gap_to_l1_pct",
                                                "counter_offers", "late_counter_offer_rate", "avg_response_hours")},
            "buyers": buyers.get("buyers"), "categories": prof.get("categories"),
            "profile_completeness_pct": prof.get("completeness_pct")}


def dumps(o) -> str:
    return json.dumps(o, default=str)
