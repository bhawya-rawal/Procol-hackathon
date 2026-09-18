"""Offline chat engine: deterministic intent routing over the same tools the LLM would call.
Keeps the demo working without an API key. Replies are plain sentences built from real numbers."""
from __future__ import annotations

import re

from .tools import VendorTools

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
CATS = ["steel", "packaging", "chemicals", "electricals", "mro spares", "mro", "logistics services", "logistics"]

HELP = ("I can answer questions about your own record on this platform. Try:\n"
        "• why did I lose the Steel RFQ in July?\n• which events did I lose recently?\n"
        "• what price should I quote in Steel?\n• how do I do on counter-offers?\n"
        "• how is my delivery performance?\n• where am I weak technically?\n"
        "• how am I doing with Apex Steelworks?\n• is my profile complete?\n"
        "• does revising my bid actually help me win?\n• month-by-month demand by category")


def pct(v): return "—" if v is None else f"{round(v * 100)}%"
def n1(v):  return "—" if v is None else f"{v:.1f}"


class OfflineEngine:
    name = "offline"

    def __init__(self, tools: VendorTools):
        self.t = tools
        self.calls: list[dict] = []

    def _call(self, name, args=None):
        out = self.t.call(name, args or {})
        self.calls.append({"tool": name, "args": args or {}})
        return out

    # ------------------------------------------------------------------ entry
    def reply(self, text: str, history: list[dict]) -> str:
        q = text.lower().strip()
        cat = next((c for c in CATS if c in q), None)
        if cat == "mro": cat = "mro spares"
        if cat == "logistics": cat = "logistics services"

        if re.match(r"^\W*(help|what can you|what else can you|options)\b", q):
            return HELP
        if re.search(r"\b(who won|winner|winning vendor|l1 (price|bid|vendor|amount)|lowest (price|bid)|competitor|other vendor|rival)", q):
            return ("I don't have — and will never show — other vendors' prices, names or ranks. What I can tell you is your own "
                    "position: your % gap to the lowest bid and your rank out of the bidders. Ask “why did I lose …” for any event.")
        if re.search(r"\b(demand|inventory|stock\w*|seasonal\w*|month.by.month|monthly|per month|forecast\w*|pipeline|volumes?)\b", q):
            return self._demand(cat, q)
        if re.search(r"\b(revis\w*|re.?bid\w*|first bid|final bid|second bid|counter.?bid|improve\w* my bid|quote once|bid again)\b", q):
            return self._revisions()
        if re.search(r"\b(lose|lost|losing|didn.?t win|why.*(reject|not selected))\b", q):
            return self._why_lost(q, cat, history)
        if re.search(r"\b(quote|price|pricing|band|how much|competitive|cheaper|expensive|gap)\b", q):
            return self._price(cat)
        if re.search(r"\bcounter", q) or "negotiat" in q or "best offer" in q:
            return self._counter()
        if re.search(r"\b(deliver\w*|on.time|quality|qc)\b", q):
            return self._delivery()
        if re.search(r"\b(technical\w*|scores?|sections?|rfps?|certif\w*)\b", q):
            return self._technical()
        if re.search(r"\b(profile|complian\w*|documents?|gst|onboard\w*|complete\w*|missing)\b", q):
            return self._profile()
        m = re.search(r"\b(?:with|for|at)\s+([A-Za-z][A-Za-z ]{2,40}?)(?:\?|$|\.)", text)
        if m and self._buyer_like(m.group(1)):
            return self._buyer(m.group(1).strip())
        if re.search(r"\b(won|wins?|win rate|performance|how am i|summary|overview|doing)\b", q):
            return self._summary()
        if re.search(r"\b(events?|history|recent|invites?|invitations?|open rate|respond\w*|response|participat\w*|habits?|bid rate)\b", q):
            return self._events_or_habits(q, cat)
        return "I'm not sure what you're asking. " + HELP

    # ------------------------------------------------------------------ intents
    def _why_lost(self, q: str, cat, history) -> str:
        evs = self._call("my_events", {"outcome": "lost", "category": cat, "limit": 40})["events"]
        if not evs:
            return f"You have no lost events{' in ' + cat.title() if cat else ''} on record — every event you bid on there, you won, or you did not bid."
        target = self._pick_event(q, evs)
        if target is None:
            lines = [f"You lost {len(evs)} event{'s' if len(evs) != 1 else ''}{' in ' + cat.title() if cat else ''}. Most recent:"]
            lines += [f"• {e['title']} ({e['buyer']}, closed {e['closed_at']}){' — missed a negotiation window' if e['missed_negotiation_window'] else ''}"
                      for e in evs[:5]]
            lines.append("Ask me about one of them, e.g. “why did I lose the " + evs[0]["title"].split(" — ")[0] + "?”")
            return "\n".join(lines)
        pm = self._call("why_did_i_lose", {"trade_request_id": target["trade_request_id"]})
        if not pm.get("available"):
            return pm.get("message") or "Feedback is not available for that event."
        out = [f"**{pm['title']}** — {pm['narration']}", f"Next time: {pm['next_action']}"]
        flagged = [f"{k}: {v['signal']}" for k, v in pm["checks"].items() if v["severity"] in ("high", "medium")]
        if flagged:
            out.append("Flags — " + " | ".join(flagged))
        return "\n".join(out)

    def _pick_event(self, q: str, evs: list[dict]):
        m = re.search(r"#?\b(\d{1,5})\b", q)
        if m:
            hit = [e for e in evs if e["trade_request_id"] == int(m.group(1))]
            if hit: return hit[0]
        mon = next((MONTHS[k] for k in MONTHS if re.search(rf"\b{k}", q)), None)
        mode = next((x for x in ("auction", "rfp", "rfq") if x in q), None)
        cands = evs
        if mon:  cands = [e for e in cands if int(e["closed_at"][5:7]) == mon] or cands
        if mode: cands = [e for e in cands if e["rfx_mode"] == mode] or cands
        if re.search(r"\b(last|latest|recent|most recent)\b", q) or mon or mode:
            return cands[0]
        return None

    def _demand(self, cat, q: str) -> str:
        months = 12
        m = re.search(r"\b(\d{1,2})\s*(?:months?|mo)\b", q)
        if m:
            months = int(m.group(1))
        elif re.search(r"\b(two years|24 months|2 years)\b", q):
            months = 24
        d = self._call("demand_by_month", {"category": cat, "months": months})
        cats = d.get("categories") or []
        if not cats:
            return d.get("message") or f"No buying activity on record{' in ' + cat.title() if cat else ''} in the last {months} months."
        lines = [f"Demand in the events you were invited to, last {d['window_months']} months (through {d['through_month']}):"]
        for c in cats:
            months_str = ", ".join(f"{m['month'][5:]} {m['events']}" for m in c["by_month"])
            unit = "/".join(c["units"])
            lines.append(f"**{c['category']}** — {c['events']} events, {c['total_quantity']:,.0f} {unit} asked; "
                         f"you bid on {c['events_you_bid']}. Busiest month {c['peak_month']} "
                         f"({c['peak_month_quantity']:,.0f} {unit}).")
            lines.append(f"  events by month — {months_str}")
            if c["confidence"] == "low":
                lines.append(f"  (thin history: {c['events']} events over {c['months_with_demand']} active months — treat as a hint, not a forecast)")
        lines.append(d["scope"])
        return "\n".join(lines)

    def _revisions(self) -> str:
        r = self._call("bid_revision_behaviour", {"limit": 5})
        if not r.get("events_bid"):
            return r.get("message", "No bids on record yet.")
        lines = [r["insight"]]
        for b in r["by_revisions"]:
            lines.append(f"• {b['label']}: {b['events']} events, won {b['wins']} ({pct(b['win_rate'])})"
                         + (f", average price cut {b['avg_price_drop_pct']:.1f}%" if b["avg_price_drop_pct"] else ""))
        lines.append("Recent events (first bid → final bid):")
        for e in r["events"]:
            move = "no change" if not e["delta_pct"] else f"{e['delta_pct']:+.1f}%"
            lines.append(f"• {e['closed_at'][:10]} {e['title']} — {e['revisions']} revision(s), {move}, {e['outcome']}")
        return "\n".join(lines)

    def _price(self, cat) -> str:
        pb = self._call("price_band", {"category": cat})["categories"]
        if not pb:
            return f"No bid history{' in ' + cat.title() if cat else ''} to build a price band from yet."
        lines = []
        for c in pb:
            best = [b for b in c["buckets"] if b["bids"] and b["win_rate"] is not None]
            lines.append(f"**{c['category']}** ({c['total_bids']} bids): " + c["insight"])
            if best:
                lines.append("  win rate by gap to L1 — " + ", ".join(f"{b['gap_to_l1']}: {pct(b['win_rate'])} (n={b['bids']})" for b in best))
        lines.append("These are your own results only — I never see other vendors' prices. Stay within 3% of your best recent quote to be in the winning band.")
        return "\n".join(lines)

    def _counter(self) -> str:
        h = self._call("habits")
        n = h.get("counter_offers") or 0
        if not n:
            return "You have not received any counter-offers yet."
        late = h.get("late_counter_offer_rate") or 0
        verdict = ("This is your biggest leak: replies that land after the window are treated as declines."
                   if late > 0.4 else "You mostly reply in time." if late < 0.15 else "Room to tighten this.")
        acc = h.get("counter_offer_accept_rate")
        accepted = f" When you did reply in time, you accepted {pct(acc)}." if acc is not None else ""
        return (f"You received {n} counter-offers. You replied inside the window {pct(h.get('counter_offer_response_rate'))} of the time "
                f"and missed the window {pct(late)} of the time; average reply time {n1(h.get('avg_counter_offer_response_hours'))}h.{accepted} {verdict}")

    def _delivery(self) -> str:
        d = self._call("delivery")
        if not d.get("deliveries"):
            return "No completed deliveries on record yet."
        tr = d.get("trend_90d_vs_365d")
        trend = "" if tr is None else (f" Your last 90 days are {abs(tr):.0f} pp {'worse' if tr < 0 else 'better'} than your 12-month average." if abs(tr) >= 5 else " Stable over the last 90 days.")
        return f"Across {d['deliveries']} delivered orders: on-time {pct(d.get('on_time_rate'))}, QC pass {pct(d.get('qc_pass_rate'))}.{trend}"

    def _technical(self) -> str:
        t = self._call("technical")
        if not t.get("available"):
            return t.get("message", "Technical scores are not available.")
        if t.get("avg_pct") is None:
            return "You have not been technically evaluated yet (no RFPs with scores)."
        secs = ", ".join(f"{s['section_key'].replace('_', ' ')} {s['pct']:.0f}%" for s in t.get("sections", []))
        return (f"Your technical average is {t['avg_pct']:.0f}%. Weakest section: {t['weakest_section'].replace('_', ' ')} at {t['weakest_pct']:.0f}%. "
                f"By section — {secs}. Refresh that section's documentation before your next RFP.")

    def _profile(self) -> str:
        p = self._call("profile")
        miss = ", ".join(m.replace("_", " ") for m in p["missing"]) or "nothing"
        pend = ", ".join(p["pending_onboarding"]) or "none"
        return (f"Profile is {p['completeness_pct']}% complete. Missing: {miss}. Mapped to {len(p['buyers'])} buyers; pending onboarding: {pend}. "
                f"Categories: {', '.join(p['categories'])}.")

    def _buyer_like(self, s: str) -> bool:
        from ..db import one
        return one(self.t.c, "SELECT 1 FROM companies WHERE category='buyer' AND lower(name) LIKE ?", (f"%{s.lower().strip()}%",)) is not None

    def _buyer(self, name: str) -> str:
        b = self._call("buyer_summary", {"buyer": name})
        if b.get("error"): return b["error"]
        if b.get("message"): return f"{b['buyer']}: {b['message']}"
        pol = {"none": "shares no event feedback", "relative_only": "shares relative feedback (no technical scores)",
               "relative_plus_technical": "shares relative and technical feedback"}[b["feedback_policy"]]
        otd = "" if b["on_time_delivery_rate"] is None else f", on-time delivery {pct(b['on_time_delivery_rate'])}"
        return (f"With {b['buyer']}: {b['invites']} invites, bid rate {pct(b['bid_rate'])}, won {pct(b['win_rate'])} of {b['events_bid']} events you bid on, "
                f"average {n1(b['avg_gap_to_l1_pct'])}% above L1, missed {pct(b['late_counter_offer_rate'])} of counter-offer windows{otd}. This buyer {pol}.")

    def _summary(self) -> str:
        h = self._call("habits"); ev = self._call("my_events", {"limit": 1})["totals_all_time"]
        return (f"All time: invited to {h.get('invites')} events, bid on {pct(h.get('bid_rate'))}, won {ev['won']}, lost {ev['lost']}, skipped {ev['no_bid']}. "
                f"Average gap to L1 {n1(h.get('avg_gap_to_l1_pct'))}%. Counter-offer windows missed {pct(h.get('late_counter_offer_rate'))}. "
                f"Last 90 days: {h.get('last_90d', {}).get('invites')} invites, win rate {pct(h.get('last_90d', {}).get('win_rate'))}. Ask me “why did I lose…” for any event.")

    def _events_or_habits(self, q: str, cat) -> str:
        if re.search(r"\b(events?|history|recent)\b", q):
            evs = self._call("my_events", {"category": cat, "limit": 8})["events"]
            return "Your recent events:\n" + "\n".join(f"• {e['closed_at']} {e['title']} — {e['buyer']} — {e['outcome'].replace('_', ' ')}" for e in evs)
        h = self._call("habits"); sl, ll = h.get("short_lead", {}), h.get("long_lead", {})
        dec = ", ".join(f"{k.replace('_', ' ')} ({v})" for k, v in (h.get("decline_reasons") or {}).items()) or "none given"
        return (f"You open {pct(h.get('mail_open_rate'))} of invitations and bid on {pct(h.get('bid_rate'))}; first bid typically {n1(h.get('avg_response_hours'))}h after invite. "
                f"On short-lead events (≤3 days) you bid {pct(sl.get('bid_rate'))} of {sl.get('n')} times vs {pct(ll.get('bid_rate'))} of {ll.get('n')} on longer ones. "
                f"Decline reasons you gave: {dec}. Extensions caused by late bids: {h.get('extensions_caused') or 0}.")
