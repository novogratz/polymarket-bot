from datetime import datetime, timedelta
from polymarket_bot.models import Candidate
from polymarket_bot.quant_bitcoin import _is_btc_5m_market, _is_btc_market, _is_btc_micro_market, analyze_quant_bitcoin, QuantBtcSnapshot
from polymarket_bot.config import Settings

def test_is_btc_micro_market():
    # Test cases for micro markets
    cases = [
        ("Will Bitcoin be above $60k at 5:00 PM?", "bitcoin-price-60k-5pm", "bitcoin-price-up-down", True),
        ("Bitcoin Price 5m", "btc-5m", "", True),
        ("BTC Up/Down", "btc-up-down", "btc-micro", True),
        ("Bitcoin 5-minute price move", "bitcoin-5-min", "bitcoin-micro", True),
        ("Will Ethereum be above $3k?", "ethereum-price-3k", "", False),
        ("Will BTC be $100k by 2025?", "btc-100k-2025", "long-term", False),
    ]
    
    for q, s, e, expected in cases:
        c = Candidate(
            market_id='1', question=q, slug=s, event_slug=e, 
            end_date=datetime.now(), hours_to_close=1, 
            liquidity=1000, volume=1000, outcome='UP', 
            price=0.5, token_id='1', score=1.0, url=''
        )
        assert _is_btc_micro_market(c) == expected, f"Failed for {q}"

def test_analyze_quant_bitcoin():
    settings = Settings(quant_btc_enabled=True)
    
    # Mock snapshot: BTC up 0.5%
    snapshot = QuantBtcSnapshot(
        symbol="BTCUSDT", spot=60300, open_5m=60000, close_5m=60300, 
        move_pct=0.005, spot_binance=60300, spot_coinbase=60300, spot_kraken=60300,
        exchange_divergence=False, exchange_count=3,
        tradingview="BULL", cryptoquant="BULL", graph_bias="BULL",
        bull_nodes=64, bear_nodes=36, edges=180
    )
    
    candidate = Candidate(
        market_id='m1', question="Will Bitcoin be above $100k by 2026?", slug="bitcoin-2026", event_slug="bitcoin-2026",
        end_date=datetime.now() + timedelta(minutes=4), hours_to_close=0.06,
        liquidity=5000, volume=5000, outcome="UP", price=0.51,
        token_id="t1", score=10.0, url="http://pm.com/bitcoin-2026",
        best_bid=0.55, best_ask=0.56, tick_size=0.01, accepts_orders=True
    )
    
    report = analyze_quant_bitcoin([candidate], settings, snapshot)
    
    assert report.selected is not None
    assert report.selected.candidate.market_id == 'm1'
    assert report.selected.direction == "BULL"
    assert abs(report.selected.expected_probability - 0.56) < 0.001
    assert abs(report.selected.edge - 0.01) < 0.001
    assert "0.55 threshold" in report.selected.to_dict()["selection_reason"]

def test_analyze_quant_bitcoin_rejects_below_threshold():
    settings = Settings(quant_btc_enabled=True)
    snapshot = QuantBtcSnapshot(
        symbol="BTCUSDT", spot=60300, open_5m=60000, close_5m=60300,
        move_pct=0.005, spot_binance=60300, spot_coinbase=60300, spot_kraken=60300,
        exchange_divergence=False, exchange_count=3,
        tradingview="BULL", cryptoquant="BULL", graph_bias="BULL",
        bull_nodes=64, bear_nodes=36, edges=180
    )
    candidate = Candidate(
        market_id='m2', question="Will Bitcoin be above $100k by 2026?", slug="bitcoin-2026", event_slug="bitcoin-2026",
        end_date=datetime.now() + timedelta(minutes=4), hours_to_close=0.06,
        liquidity=5000, volume=5000, outcome="UP", price=0.51,
        token_id="t1", score=10.0, url="http://pm.com/bitcoin-2026",
        best_bid=0.54, best_ask=0.55, tick_size=0.01, accepts_orders=True
    )

    report = analyze_quant_bitcoin([candidate], settings, snapshot)

    assert report.selected is None
    assert report.rejected["price_too_low"] == 1

def test_is_btc_market_accepts_longer_bitcoin_bets():
    c = Candidate(
        market_id="1",
        question="Will Bitcoin be above $100k by 2026?",
        slug="bitcoin-2026",
        event_slug="bitcoin-2026",
        end_date=datetime.now(),
        hours_to_close=1,
        liquidity=1000,
        volume=1000,
        outcome="UP",
        price=0.5,
        token_id="1",
        score=1.0,
        url="",
    )

    assert _is_btc_market(c)

def test_is_btc_5m_market_excludes_longer_updown_series():
    five_minute = Candidate(
        market_id='1', question='Bitcoin Up or Down - May 12, 9:15AM-9:20AM ET',
        slug='btc-updown-5m-1778591700', event_slug='',
        end_date=datetime.now(), hours_to_close=1,
        liquidity=1000, volume=0, outcome='DOWN',
        price=0.5, token_id='1', score=1.0, url=''
    )
    fifteen_minute = Candidate(
        market_id='2', question='Bitcoin Up or Down - May 12, 8:15AM-8:30AM ET',
        slug='btc-updown-15m-1778588100', event_slug='',
        end_date=datetime.now(), hours_to_close=1,
        liquidity=1000, volume=0, outcome='DOWN',
        price=0.5, token_id='2', score=1.0, url=''
    )

    assert _is_btc_5m_market(five_minute)
    assert not _is_btc_5m_market(fifteen_minute)

if __name__ == "__main__":
    test_is_btc_micro_market()
    test_analyze_quant_bitcoin()
    print("All quant strategy tests passed!")
