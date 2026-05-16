"""Polymarket market classifier and bot-fit scorer.

Pattern-based classifier that segments markets into categories and scores their
fit for the TradingAgents bull/bear pipeline.  Used by market discovery to skip
markets where the bot has no edge (random walks, weather, individual celebrity
decisions) and surface markets where it does (elections, concrete events,
geopolitical outcomes with strong base rates).

Why pattern-based, not LLM-based:
    - Cheap (no API calls)
    - Deterministic and unit-testable
    - Easy to inspect/audit categorization
    - Most markets fall into clear patterns from the question text alone
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Category = Literal[
    "election",
    "tournament_participation",
    "concrete_event",
    "geopolitical",
    "regulatory",
    "appointment_outcome",
    "talent_show_winner",
    "individual_sport_game",
    "sport_team_game",
    "esports_game",
    "crypto_price",
    "stock_price",
    "commodity_price",
    "weather",
    "celebrity_move",
    "short_term_price",
    "speech_keyword",
    "other",
]

BotFit = Literal["good", "neutral", "bad"]


@dataclass(frozen=True)
class Classification:
    category: Category
    bot_fit: BotFit
    reason: str


# ---------------------------------------------------------------------------
# Pattern rules — order matters: first match wins
# ---------------------------------------------------------------------------

# BAD-fit patterns (skip these — bot has no edge)

_RANDOM_WALK_PATTERNS = [
    # Crypto price moves
    (r"\b(bitcoin|btc|ethereum|eth|sol|solana|xrp|doge|dogecoin)\s+up\s+or\s+down\b",
     "crypto_price", "5-min crypto direction is a random walk"),
    (r"\b(bitcoin|btc|ethereum|eth|sol|solana|xrp|doge)\b.*\b(hit|reach|cross|touch|dip|rise|fall|drop|spike)\b.*\$",
     "crypto_price", "crypto price target = random walk"),
    (r"\bprice\s+of\s+(bitcoin|btc|ethereum|eth|sol|solana|xrp|doge)\b.*(\$|between)",
     "crypto_price", "crypto price target = random walk"),
    (r"\b(bitcoin|btc|ethereum|eth|sol|solana|xrp|doge)\b.*\bbetween\s+\$",
     "crypto_price", "crypto price range = random walk"),

    # Commodity prices (check before generic stock-ticker pattern)
    (r"\b(silver|gold|oil|copper|wheat|corn|platinum|palladium|natural\s+gas)\b",
     "commodity_price", "commodity price = random walk"),

    # Eurovision — order-independent
    (r"\beurovision\b",
     "talent_show_winner", "Eurovision = popular vote"),

    # Talent shows / popularity competitions on specific contestant
    (r"\bsurvivor\s+season\s+\d+",
     "talent_show_winner", "individual contestant in talent/reality show"),
    (r"\b(american\s+idol|big\s+brother|the\s+voice|x[\s-]factor|got\s+talent|dancing\s+with|amazing\s+race|love\s+island)\b",
     "talent_show_winner", "individual contestant in talent/reality show"),
    (r"\bbachelor(ette)?\s+season\b",
     "talent_show_winner", "individual contestant"),

    # Esports (check BEFORE generic 'X vs Y' sports pattern)
    (r"\b(lol|league\s+of\s+legends|counter[\s-]strike|dota|valorant|iem|esl)\b",
     "esports_game", "esports match — high variance"),
    (r"\b(karmine|movistar\s+koi|fnatic|t1\b|skt\b|g2\s+esports|natus\s+vincere|navi\s+|fut\s+esports)\b",
     "esports_game", "esports team match"),

    # Stock ticker price target — e.g. "Airbnb, Inc. (ABNB)"
    (r"\([A-Z]{2,5}\)\s*(?:hit|reach|settle|cross|close|touch|drop|rise)",
     "stock_price", "stock ticker price target"),
    (r"\b(?:will|does)\s+\w+(?:,?\s+inc\.?)?\s+\([A-Z]{2,5}\)",
     "stock_price", "named stock ticker price target"),

    # Short-term price targets (with dollar amounts and month boundaries)
    (r"\$[\d,]+(?:\.\d+)?k?\b.*\b(?:by|on|in)\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
     "short_term_price", "short-term price target = random walk"),

    # Weather
    (r"\bhighest\s+temperature\b",
     "weather", "weather prediction = stochastic"),
    (r"\btemperature\s+in\b.*°[CF]",
     "weather", "weather prediction = stochastic"),

    # Sports leagues
    (r"\b(nba|nfl|mlb|nhl|epl|la\s+liga|bundesliga|nrl|afl|kbo|npb|serie\s+a|ligue\s+1)\b",
     "sport_team_game", "single sports game = high variance"),
    (r"\bO/U\s+\d",
     "sport_team_game", "sports over/under = variance prop"),
    (r"\b(?:moneyline|spread|first\s+half|halftime)\b",
     "sport_team_game", "sports prop bet"),
    (r"\b\w+\s+vs\.?\s+\w+\b.*\b(?:game\s+\d|score|series)",
     "sport_team_game", "sports head-to-head match"),
    (r"\bplayoffs?\b.*\bseries\b",
     "sport_team_game", "playoff series — variance"),
    (r"\bwill\s+[\w\s.·]+?(?:\s+fc)?\s+win\s+on\s+\d{4}-\d{2}-\d{2}",
     "sport_team_game", "specific dated sports match"),
    (r"\b(yokohama|kashiwa|kashima|chengdu|beijing\s+guoan|jef\s+united)\b",
     "sport_team_game", "football match"),
    (r"\b(braves|yankees|dodgers|red\s+sox|mets|cubs|astros|phillies)\b.*\b(nl|al)\s+(east|west|central)\s+title",
     "sport_team_game", "MLB division title"),
    (r"^set\s+\d+\s+winner\b",
     "sport_team_game", "tennis set winner"),
    # "X vs. Y" or "X vs Y" patterns — almost always sports/esports
    (r"\bvs\.?\s+\w",
     "sport_team_game", "head-to-head matchup — likely sport"),
    # "end in a draw" - football draw markets
    (r"\bend\s+in\s+a\s+draw\b",
     "sport_team_game", "football draw market"),
    # Common sports team suffixes
    (r"\b\w+\s+(fc|sc|cf|ac|cd|ec|sk|hsv|ssv|vfb|vfl)\b",
     "sport_team_game", "team name with sports suffix"),
    (r"\b(united|city|wanderers|rovers|athletic|olympique)\b.*\b(vs|win|draw)\b",
     "sport_team_game", "football club"),

    # Speech-keyword markets — "Will X say 'Y' during Z?" patterns.
    # Why: historically lose as a correlated cluster (7-of-7 on Trump-Xi event,
    # 2026-05-14). The LLM analyzes each keyword independently and can't see
    # they're a single thesis split across many tickets.
    (r"\bsay\s+\"[^\"]+\"",
     "speech_keyword", "specific-word utterance market = correlated cluster, no edge"),
    (r"\bwill\s+\w+\s+say\s+\"",
     "speech_keyword", "specific-word utterance market = correlated cluster, no edge"),

    # Celebrity / random individual decisions
    (r"\bwill\s+\w+\s+retire\b",
     "celebrity_move", "individual retirement decision = unpredictable"),
    (r"\bvisit\s+(pakistan|china|cuba|russia|iran|north\s+korea|venezuela)\b",
     "celebrity_move", "specific country visit = political theater"),
    (r"\bmarry\b|\bdivorce\b|\bget\s+engaged\b",
     "celebrity_move", "personal relationship decision"),
    (r"\brenames?\s+(ice|fbi|cia|nasa|doj|dod)\b",
     "celebrity_move", "random political naming theater"),
]

# GOOD-fit patterns (prioritize these — bot has fundamentals/base-rate edge)

_GOOD_FIT_PATTERNS = [
    # Elections — political party wins
    (r"\bwill\s+the\s+(democratic|republican|labor|conservative|tory|labour)\s+party\s+win\b",
     "election", "political party win — strong base rates"),
    (r"\bwin\s+the\s+most\s+seats?\s+in\s+the\b",
     "election", "party plurality / most seats — base rates apply"),
    (r"\bwill\s+\w+\s+(win|carry)\s+the\s+\d+\s+(presidential|gubernatorial|senate|congressional|house)\b",
     "election", "election outcome"),
    (r"\b(house|senate|gubernatorial)\s+seat\b",
     "election", "legislative seat election"),
    (r"\bwin\s+the\s+(20\d{2}|next)\s+.*\s+(primary|general|election|race)\b",
     "election", "specific election outcome"),
    (r"\bbe\s+the\s+(democratic|republican)\s+nominee\b",
     "election", "party nominee — clear criteria"),
    (r"\bbe\s+the\s+next\s+(prime\s+minister|president|chancellor|premier|governor|mayor|senator)\b",
     "appointment_outcome", "executive appointment — base rates apply"),
    (r"\bbe\s+(confirmed|appointed)\s+as\b",
     "appointment_outcome", "confirmation/appointment"),

    # Tournament/event participation (binary, base rates work)
    (r"\bplay\s+in\s+the\s+\d+\s+(fifa\s+world\s+cup|olympics|world\s+cup|euros?|copa)\b",
     "tournament_participation", "tournament qualification — fundamentals matter"),
    (r"\bqualify\s+for\s+the\s+\d+\s+(fifa\s+world\s+cup|olympics|world\s+cup|euros?)\b",
     "tournament_participation", "tournament qualification"),

    # Geopolitical with clear criteria
    (r"\bnato\s+article\s+5\b",
     "geopolitical", "NATO triggers — strong historical base rates"),
    (r"\b(ceasefire|truce|peace\s+agreement|treaty)\b.*\b(announced|signed|reached)\b",
     "geopolitical", "diplomatic outcome with concrete criteria"),
    (r"\binvad(e|es|ed)\b.*\bby\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})",
     "geopolitical", "military action with deadline"),

    # Regulatory / legal outcomes
    (r"\b(sec|fdic|cftc|fed|federal\s+reserve)\s+(approves?|rules?|decides?|votes?)\b",
     "regulatory", "regulatory decision — clear binary"),
    (r"\b(approve|block|reject)\s+the\s+\w+\s+(merger|acquisition|deal)\b",
     "regulatory", "regulatory approval"),
    (r"\bsupreme\s+court\b.*(rule|decide|hear)",
     "regulatory", "SCOTUS decision — bounded outcome"),

    # Concrete event happens by date
    (r"\blaunch\s+(a|its)\s+token\s+by\b",
     "concrete_event", "product launch deadline — concrete criteria"),
    (r"\b(announce|recogni[zs]e|declare)\b.*\bby\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})",
     "concrete_event", "official announcement deadline"),
    (r"\bresign\s+(by|before)\b",
     "concrete_event", "resignation deadline — base rates"),
    (r"\bsigned?\s+into\s+law\b",
     "concrete_event", "legislation signing"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_market(question: str) -> Classification:
    """Classify a market by its question text.

    First pass: check BAD patterns (skip these markets).
    Second pass: check GOOD patterns (prioritize these).
    Default: 'other' / 'neutral' — let the LLM decide.
    """
    if not question:
        return Classification("other", "neutral", "empty question")

    q = question.lower().strip()

    # Check BAD patterns first — skip these regardless of LLM signal
    for pat, cat, reason in _RANDOM_WALK_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return Classification(cat, "bad", reason)

    # Check GOOD patterns — prioritize for analysis
    for pat, cat, reason in _GOOD_FIT_PATTERNS:
        if re.search(pat, q, re.IGNORECASE):
            return Classification(cat, "good", reason)

    return Classification("other", "neutral", "no pattern match — fall back to LLM analysis")


def is_extreme_price(yes_price: float, lower: float = 0.05, upper: float = 0.95) -> bool:
    """Return True if yes_price is too close to 0 or 1 for economic trading.

    BUY_YES near 1.00: no upside even when correct (BUY 0.99c → max 1¢ profit).
    BUY_NO when yes near 0: same problem on the NO side.
    Default bounds correspond to the executor's _MIN_PRICE/_MAX_PRICE guards.
    """
    return yes_price < lower or yes_price > upper


def score_market_for_discovery(
    question: str,
    yes_price: float,
    liquidity: float,
    *,
    min_liquidity: float = 5000.0,
    skip_bad_fit: bool = True,
) -> tuple[bool, str]:
    """Discovery-time gate: should we spend an LLM call on this market?

    Returns (should_analyze, reason).
    """
    if liquidity < min_liquidity:
        return False, f"liquidity ${liquidity:,.0f} < ${min_liquidity:,.0f}"

    if is_extreme_price(yes_price):
        return False, f"price {yes_price:.3f} is extreme (outside 0.05-0.95)"

    cls = classify_market(question)
    if skip_bad_fit and cls.bot_fit == "bad":
        return False, f"category={cls.category}: {cls.reason}"

    return True, f"category={cls.category}, fit={cls.bot_fit}: {cls.reason}"
