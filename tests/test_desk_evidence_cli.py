from futures_fund.config import Settings
from scripts import desk_evidence


class _Client:
    def __init__(self):
        self.load_markets_calls = 0

    def load_markets(self):
        self.load_markets_calls += 1


def test_universe_scan_and_evidence_share_one_public_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(desk_evidence, "build_ccxt", lambda settings: client)

    scan_client, exchange = desk_evidence._build_data_clients(Settings())

    assert scan_client is client
    assert exchange.client is client
    assert exchange.keyless is True
    assert client.load_markets_calls == 1
