"""Frozen identity for the outcome-blind official spot REST book reservoir."""

from types import MappingProxyType

PROTOCOL_ID = "q15-rti-spot-rest-top-book-reservoir-v2"
PROTOCOL_SHA256 = "b4e3e342ae73c94679becb917a680020eabf9ee6cd3a80fa14b0781d2eb92a17"
PROTOCOL_RELATIVE_PATH = "config/q15_rti_spot_rest_top_book_reservoir_v2_protocol.json"
DATABASE_RELATIVE_PATH = "data/q15_rti_spot_rest_top_book_v2.sqlite3"
SCHEMA_VERSION = "official-spot-rest-top-book-v1"
PROSPECTIVE_AFTER_CLOSE_TIME = 1785619800.0
FIRST_ELIGIBLE_CLOSE_TIME = 1785620700.0
MAX_REQUEST_START_OFFSET_SECONDS = 2.0
MAX_RESPONSE_LATENCY_SECONDS = 2.0
MAX_RECEIVE_OFFSET_SECONDS = 4.0
MAX_EXCHANGE_CLOCK_LEAD_SECONDS = 5.0

SOURCE_IDENTITIES = MappingProxyType({
    "BNB": ("okx", "BNB-USDT", "USDT"),
    "BTC": ("coinbase", "BTC-USD", "USD"),
    "DOGE": ("coinbase", "DOGE-USD", "USD"),
    "ETH": ("coinbase", "ETH-USD", "USD"),
    "HYPE": ("okx", "HYPE-USDT", "USDT"),
    "SOL": ("coinbase", "SOL-USD", "USD"),
    "XRP": ("coinbase", "XRP-USD", "USD"),
})
REQUEST_CONTRACTS = MappingProxyType({
    "coinbase": (
        "GET",
        "https://api.exchange.coinbase.com/products/{symbol}/book",
        (("level", "1"),),
    ),
    "okx": (
        "GET",
        "https://www.okx.com/api/v5/market/books",
        (("instId", "{symbol}"), ("sz", "1")),
    ),
})

OUTCOME_ACCESS_ALLOWED = False
MODEL_FIT_ALLOWED = False
PROBABILITY_SCORING_ALLOWED = False
NOTIFICATION_ELIGIBLE = False
REAL_TRADING_ALLOWED = False
