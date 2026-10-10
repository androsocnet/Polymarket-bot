"""Endpoint construction for public Gamma metadata only."""
from datetime import datetime
from urllib.parse import quote
from .http import PublicGet
from .models import MarketDataError

class GammaClient:
    def __init__(self, get: PublicGet):
        self.get = get

    def event_by_slug(self, slug: str, *, deadline: float) -> dict[str, object]:
        return self._object(self.get.get_json("gamma", "/events/slug/"+quote(slug, safe=""), deadline=deadline).payload)

    def market_by_id(self, market_id: str, *, deadline: float) -> dict[str, object]:
        return self._object(self.get.get_json("gamma", "/markets/"+quote(market_id, safe=""), deadline=deadline).payload)

    def list_events(self, *, end_min: datetime, end_max: datetime, offset: int, deadline: float) -> list[dict[str, object]]:
        payload = self.get.get_json("gamma", "/events", params={
            "active":"true", "closed":"false", "end_date_min":end_min.isoformat(), "end_date_max":end_max.isoformat(),
            "limit":100, "offset":offset, "order":"endDate", "ascending":"true"}, deadline=deadline).payload
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise MarketDataError("invalid_metadata", "Gamma event list must be an array of objects.")
        return payload

    @staticmethod
    def _object(payload: object) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise MarketDataError("invalid_metadata", "Gamma details must be an object.")
        return payload
