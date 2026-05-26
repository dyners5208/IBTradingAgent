"""
Gem research — AI-powered stock discovery for the gem universe.

Called by the dashboard when the user types a research theme. Runs an
agentic Claude loop with web search to identify 3-5 candidates, then
returns them as plain dicts WITHOUT saving to the DB (the dashboard lets
the user approve/reject before committing).

Public API:
    research_gems(theme: str, api_key: str) -> list[dict]

Each dict has: stock_code, name, sector, macro_theme, thesis, conviction (1-5),
plus optional structured fields: moat, pe_ratio, pe_sector_avg, pb_ratio,
pb_sector_avg, ps_ratio, ps_sector_avg, revenue_growth_yoy_pct,
gross_margin_pct, debt_to_equity, debt_to_equity_sector_avg, metrics_note.
"""

_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

_PROPOSE_GEM_TOOL = {
    "name": "propose_gem",
    "description": (
        "Propose a stock as a gem candidate after fully researching its fundamentals, "
        "moat, and valuation metrics. Call this once per approved stock."
    ),
    "input_schema": {
        "type": "object",
        "required": ["stock_code", "name", "sector", "macro_theme", "thesis", "conviction"],
        "properties": {
            # ── Core fields ────────────────────────────────────────────────
            "stock_code": {
                "type": "string",
                "description": (
                    "Moomoo ticker code. US stocks: 'US.<TICKER>' (e.g. US.RKLB). "
                    "HK stocks: 'HK.<4-digit-code>' (e.g. HK.9880)."
                ),
            },
            "name":        {"type": "string", "description": "Full company name"},
            "sector":      {"type": "string", "description": "Industry sector"},
            "macro_theme": {
                "type": "string",
                "description": "One-line macro driver (e.g. 'New Space Economy — commercial launch infrastructure')",
            },
            "thesis": {
                "type": "string",
                "description": (
                    "3-5 sentence investment rationale covering: revenue growth trajectory, "
                    "path to or quality of profitability, key catalyst, and primary risk."
                ),
            },
            "conviction": {
                "type": "integer", "minimum": 1, "maximum": 5,
                "description": "1=early-stage bet, 2=speculative, 3=moderate, 4=strong, 5=high conviction",
            },
            # ── Moat ───────────────────────────────────────────────────────
            "moat": {
                "type": "string",
                "description": (
                    "Start with the moat TYPE on its own line (choose the single best fit: "
                    "Network Effects | Switching Costs | Cost Advantage | Intangible Assets/IP | "
                    "Efficient Scale | Vertical Integration | Regulatory Moat). "
                    "Then 2-3 sentences explaining WHY it is defensible with specific evidence "
                    "(e.g. churn rate, market share %, patent count, switching cost estimate, "
                    "regulatory barrier). Be concrete, not generic."
                ),
            },
            # ── Valuation ratios ───────────────────────────────────────────
            "pe_ratio": {
                "type": "number",
                "description": "Trailing 12-month P/E ratio. Omit entirely if company is loss-making.",
            },
            "pe_sector_avg": {
                "type": "number",
                "description": "Sector median trailing P/E. Search '<sector> average P/E ratio 2025 2026'.",
            },
            "pb_ratio": {
                "type": "number",
                "description": "Price-to-Book ratio (market cap / book value of equity).",
            },
            "pb_sector_avg": {
                "type": "number",
                "description": "Sector median P/B ratio.",
            },
            "ps_ratio": {
                "type": "number",
                "description": "Price-to-Sales ratio (market cap / TTM revenue).",
            },
            "ps_sector_avg": {
                "type": "number",
                "description": "Sector median P/S ratio.",
            },
            # ── Financial health ───────────────────────────────────────────
            "revenue_growth_yoy_pct": {
                "type": "number",
                "description": "Most recent full-year YoY revenue growth as a percentage (e.g. 38.0 for 38%).",
            },
            "gross_margin_pct": {
                "type": "number",
                "description": "Most recent gross margin as a percentage (e.g. 43.0 for 43%).",
            },
            "debt_to_equity": {
                "type": "number",
                "description": "Current D/E ratio (total debt divided by total shareholders' equity).",
            },
            "debt_to_equity_sector_avg": {
                "type": "number",
                "description": "Sector median D/E ratio for comparison.",
            },
            "metrics_note": {
                "type": "string",
                "description": (
                    "Explain any metrics that were omitted and why "
                    "(e.g. 'P/E omitted — company is pre-profit; D/E not available for HK-listed entity'). "
                    "Also note the approximate date of the data found."
                ),
            },
        },
    },
}

_RESEARCH_PROMPT = """\
You are a senior fundamental equity analyst with deep expertise in identifying high-quality, \
high-conviction stocks for a long-only portfolio.

Research theme: {theme}

STEP 1 — DISCOVER candidates (use web_search):
  Find 3-5 publicly traded companies that are the best pure-play or near-pure-play exposures to \
this theme. Prefer:
  • Clear revenue momentum (>15% YoY) or a credible near-term inflection
  • A defensible competitive moat
  • Upcoming catalyst (product launch, contract win, regulatory approval, index inclusion)
  • Not mega-cap (where the thesis is already priced in)
  Include at least one HK-listed stock (HK.XXXX) if a compelling candidate exists.

STEP 2 — DEEP RESEARCH each candidate individually (use web_search for each):
  For every stock you plan to propose, run all of these searches before calling propose_gem:
  a) "<ticker> moat competitive advantage 2025 2026" — identify the specific moat type and evidence
  b) "<ticker> P/E ratio price to earnings 2025" — find the trailing P/E
  c) "<ticker> price to book P/B ratio 2025" — find the P/B
  d) "<ticker> price to sales P/S ratio TTM 2025" — find the P/S
  e) "<sector> average P/E P/B P/S sector median valuation 2025 2026" — find sector benchmarks
  f) "<ticker> revenue growth gross margin 2025 annual results" — find growth and margins
  g) "<ticker> debt to equity ratio balance sheet 2025" — find leverage
  h) "<sector> average debt to equity sector leverage 2025" — find sector D/E benchmark

STEP 3 — FILTER: only propose stocks where you found sufficient data. If a metric is unavailable \
(e.g. company is pre-profit so no P/E), omit that field and explain in metrics_note.

STEP 4 — CALL propose_gem for each approved candidate with ALL fields populated.
  • moat: name the TYPE first, then specific evidence (not generic statements)
  • All ratio fields must be plain numbers, not strings
  • thesis: focus on the investment case — growth, profitability path, catalyst, risk

Do NOT propose a stock unless you have searched for it individually.
"""


def research_gems(theme: str, api_key: str) -> list[dict]:
    """Run an agentic Claude loop to research gem candidates for the given theme.

    Returns a list of candidate dicts (NOT saved to DB — caller decides what to commit).
    Raises RuntimeError on API failure.
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": _RESEARCH_PROMPT.format(theme=theme)}]
    candidates: list[dict] = []

    for _ in range(20):  # higher cap — more searches per stock now
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8096,
            tools=[_WEB_SEARCH_TOOL, _PROPOSE_GEM_TOOL],
            messages=messages,
        )

        # Collect any propose_gem calls from this turn
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "propose_gem":
                candidates.append(dict(block.input))

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            # Build tool results for every propose_gem call; web_search is server-side
            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Candidate recorded.",
                }
                for block in response.content
                if getattr(block, "type", None) == "tool_use"
                and block.name == "propose_gem"
            ]
            if not tool_results:
                break
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return candidates
