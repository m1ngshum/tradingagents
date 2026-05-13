"""Tests for the Polymarket market classifier."""

import pytest

from tradingagents.dataflows.market_classifier import (
    classify_market,
    is_extreme_price,
    score_market_for_discovery,
)


class TestClassifyMarket:
    """Pattern matching for market categorization."""

    @pytest.mark.parametrize("question,expected_category,expected_fit", [
        # Crypto random walks → BAD
        ("Bitcoin Up or Down - May 12, 4:00PM-8:00PM ET", "crypto_price", "bad"),
        ("Will Bitcoin hit $150,000 by June 30?", "crypto_price", "bad"),
        ("Will Ethereum cross $5,000 in May?", "crypto_price", "bad"),

        # Stock/commodity prices → BAD
        ("Will Airbnb, Inc. (ABNB) hit (LOW) $128 in May?", "stock_price", "bad"),
        ("Will Silver (SI) settle at $50-$60 in June?", "commodity_price", "bad"),
        ("Will Gold hit $3,000 by year end?", "commodity_price", "bad"),

        # Weather → BAD
        ("Will the highest temperature in Tel Aviv be 25°C on May 14?", "weather", "bad"),
        ("Highest temperature in Karachi on May 14?", "weather", "bad"),

        # Sports games → BAD
        ("Yokohama F·Marinos vs. Kashiwa Reysol: O/U 4.5", "sport_team_game", "bad"),
        ("JEF United Ichihara Chiba vs. Kashima Antlers: O/U 3.5", "sport_team_game", "bad"),
        ("Will Chengdu Rongcheng FC win on 2026-05-15?", "sport_team_game", "bad"),
        ("NBA Playoffs: Who Will Win Series? - Spurs vs. Timberwolves", "sport_team_game", "bad"),

        # Esports → BAD
        ("LoL: Karmine Corp vs Movistar KOI - Game 1 Winner", "esports_game", "bad"),
        ("Counter-Strike: paiN vs FUT Esports (BO3)", "esports_game", "bad"),

        # Talent shows → BAD
        ("Will Kyle Fraser win Survivor Season 50?", "talent_show_winner", "bad"),
        ("Will Keyla Richardson win American Idol Season 24?", "talent_show_winner", "bad"),
        ("Will Austria come in last place at Eurovision 2026?", "talent_show_winner", "bad"),
        ("Will Portugal advance through the first Eurovision Semi-Final?", "talent_show_winner", "bad"),

        # Celebrity moves → BAD
        ("Will Karrigan retire by June 30?", "celebrity_move", "bad"),
        ("Will Trump visit Pakistan by May 31?", "celebrity_move", "bad"),
        ("Trump renames ICE to NICE by June 30?", "celebrity_move", "bad"),
    ])
    def test_bad_fit_categorization(self, question, expected_category, expected_fit):
        cls = classify_market(question)
        assert cls.category == expected_category, (
            f"'{question}' → expected {expected_category}, got {cls.category} ({cls.reason})"
        )
        assert cls.bot_fit == expected_fit

    @pytest.mark.parametrize("question,expected_category,expected_fit", [
        # Elections → GOOD
        ("Will the Democratic Party win the NJ-01 House seat?", "election", "good"),
        ("Will the Republican Party win the IL-05 House seat?", "election", "good"),
        ("Will Mark Johnston be the Democratic nominee for NE-02?", "election", "good"),
        ("Will Dominic Fritz be the next Prime Minister of Romania?", "appointment_outcome", "good"),
        ("Will Pete Ricketts be the Republican nominee for Senate in Nebraska?", "election", "good"),

        # Tournament participation → GOOD
        ("Will Iran Play in the 2026 FIFA World Cup?", "tournament_participation", "good"),
        ("Will Italy qualify for the 2026 World Cup?", "tournament_participation", "good"),

        # Geopolitical → GOOD
        ("NATO article 5 before 2027?", "geopolitical", "good"),

        # Concrete events → GOOD
        ("Will Citrea launch a token by September 30, 2026?", "concrete_event", "good"),
        ("Will Predict.fun launch a token by June 30, 2027?", "concrete_event", "good"),
    ])
    def test_good_fit_categorization(self, question, expected_category, expected_fit):
        cls = classify_market(question)
        assert cls.category == expected_category, (
            f"'{question}' → expected {expected_category}, got {cls.category} ({cls.reason})"
        )
        assert cls.bot_fit == expected_fit

    def test_unknown_questions_are_neutral(self):
        cls = classify_market("Will something happen that we cannot pattern-match?")
        assert cls.category == "other"
        assert cls.bot_fit == "neutral"

    def test_empty_question_safe(self):
        cls = classify_market("")
        assert cls.category == "other"
        assert cls.bot_fit == "neutral"


class TestIsExtremePrice:
    @pytest.mark.parametrize("price,expected", [
        (0.00, True),
        (0.01, True),
        (0.04, True),
        (0.05, False),
        (0.50, False),
        (0.95, False),
        (0.96, True),
        (1.00, True),
    ])
    def test_default_bounds(self, price, expected):
        assert is_extreme_price(price) == expected

    def test_custom_bounds(self):
        assert is_extreme_price(0.10, lower=0.15, upper=0.85)
        assert not is_extreme_price(0.50, lower=0.15, upper=0.85)
        assert is_extreme_price(0.90, lower=0.15, upper=0.85)


class TestScoreMarketForDiscovery:
    def test_low_liquidity_skipped(self):
        ok, reason = score_market_for_discovery(
            "Will the Democratic Party win the NJ-01 House seat?",
            yes_price=0.5, liquidity=1000.0,
        )
        assert not ok
        assert "liquidity" in reason

    def test_extreme_price_skipped(self):
        ok, reason = score_market_for_discovery(
            "Will the Democratic Party win the NJ-01 House seat?",
            yes_price=0.99, liquidity=10_000.0,
        )
        assert not ok
        assert "extreme" in reason

    def test_bad_category_skipped(self):
        ok, reason = score_market_for_discovery(
            "Bitcoin Up or Down - May 12, 4:00PM-8:00PM ET",
            yes_price=0.5, liquidity=10_000.0,
        )
        assert not ok
        assert "crypto_price" in reason

    def test_good_category_passes(self):
        ok, reason = score_market_for_discovery(
            "Will the Democratic Party win the NJ-01 House seat?",
            yes_price=0.5, liquidity=10_000.0,
        )
        assert ok
        assert "election" in reason
        assert "good" in reason

    def test_neutral_category_passes_by_default(self):
        ok, reason = score_market_for_discovery(
            "Will SpaceX achieve orbital refueling by 2027?",
            yes_price=0.3, liquidity=10_000.0,
        )
        assert ok
        assert "neutral" in reason

    def test_bad_can_be_kept_via_flag(self):
        ok, _ = score_market_for_discovery(
            "Bitcoin Up or Down - May 12",
            yes_price=0.5, liquidity=10_000.0,
            skip_bad_fit=False,
        )
        assert ok
