"""Read-only exchange discovery, normalized books and complete public history.

All quantities exposed by Book are native underlying units. Instrument.qty_step
remains in the venue's order units, with contract_size supplying the conversion.
The four default assets use exact same-token identities. Equity token mappings
are explicit configuration and retain corporate-action readiness separately.
"""

from dataclasses import replace
from decimal import Decimal
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import BPS, BasisSample, Book, FundingPoint, Instrument, Market, Pair, dec

HOUR = 3_600_000
DAY = 24 * HOUR
AUTO_ASSETS = frozenset(("BTC", "ETH", "PAXG", "XAUT"))
HOSTS = {
    "binance_spot": "https://api.binance.com",
    "binance": "https://fapi.binance.com",
    "bitget": "https://api.bitget.com",
    "okx": "https://www.okx.com",
}


class PublicDataError(RuntimeError):
    """A public source failed or cannot supply a complete, usable snapshot."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class PublicClient:
    def __init__(
        self, timeout=10, *, transport=None, clock_ms=None, metadata_ttl_ms=300000
    ):
        if timeout <= 0 or metadata_ttl_ms <= 0:
            raise ValueError("timeout and metadata lifetime must be positive")
        self.timeout = timeout
        self.transport = transport
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.metadata_ttl_ms = metadata_ttl_ms
        self._instruments = {}
        self._inventory_asof = {}
        self.discovery_rejections = []
        self._intervals = {}
        self._basis_cache = {}
        self._funding_cache = {}

    def _get(self, host, path, **params):
        url = HOSTS[host] + path
        if params:
            url += "?" + urlencode(params)
        try:
            if self.transport is not None:
                payload = self.transport(url)
            else:
                request = Request(
                    url, headers={"User-Agent": "carry-strategy-public/1.0 Mozilla/5.0"}
                )
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
        except HTTPError as error:
            raise PublicDataError(
                f"{host} HTTP {error.code} at {path}; request was not retried"
            ) from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise PublicDataError(
                f"{host} public request failed at {path}: {error}"
            ) from error
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and str(code) not in ("0", "00000", "200"):
                raise PublicDataError(
                    f'{host} rejected {path}: {code} {payload.get("msg", "")}',
                    code=code,
                )
        return payload

    @staticmethod
    def _venue(venue):
        venue = venue.lower()
        if venue not in ("binance", "bitget", "okx"):
            raise ValueError("Supported public venues are binance, bitget and okx")
        return venue

    @staticmethod
    def _binance_instrument(row, kind):
        filters = {item["filterType"]: item for item in row["filters"]}
        lot = filters["LOT_SIZE"]
        minimum = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        return Instrument(
            "binance",
            row["symbol"],
            kind,
            row["baseAsset"],
            row["quoteAsset"],
            dec(filters["PRICE_FILTER"]["tickSize"]),
            dec(lot["stepSize"]),
            dec(lot["minQty"]),
            dec(minimum.get("minNotional", minimum.get("notional", "0"))),
            active=row["status"] == "TRADING"
            and (kind != "spot" or row.get("isSpotTradingAllowed", True)),
        )

    def _binance_spot(self, symbol):
        payload = self._get("binance_spot", "/api/v3/exchangeInfo", symbol=symbol)
        matches = [row for row in payload["symbols"] if row["symbol"] == symbol]
        if len(matches) != 1:
            raise PublicDataError(f"Binance spot identity missing for {symbol}")
        return self._binance_instrument(matches[0], "spot")

    def discover(self, venue):
        """Load public product metadata. Binance spot inventory is bounded to defaults.

        Approved Binance equity spot symbols are fetched individually by
        discover_pairs, avoiding a large unrelated full spot inventory request.
        """
        venue = self._venue(venue)
        observed = self.clock_ms()
        if (
            venue in self._instruments
            and 0 <= observed - self._inventory_asof[venue] < self.metadata_ttl_ms
        ):
            return list(self._instruments[venue].values())
        self.discovery_rejections = [
            row for row in self.discovery_rejections if row["venue"] != venue
        ]
        self._intervals = {
            key: value for key, value in self._intervals.items() if key[0] != venue
        }
        instruments = []
        if venue == "binance":
            payload = self._get("binance", "/fapi/v1/exchangeInfo")
            for row in payload["symbols"]:
                if (
                    row.get("contractType") in ("PERPETUAL", "TRADIFI_PERPETUAL")
                    and row.get("quoteAsset") == "USDT"
                    and row.get("marginAsset") == "USDT"
                ):
                    instruments.append(self._binance_instrument(row, "linear"))
            perp_assets = {item.underlying for item in instruments if item.active}
            for asset in sorted(AUTO_ASSETS & perp_assets):
                try:
                    instruments.append(self._binance_spot(asset + "USDT"))
                except PublicDataError as error:
                    if str(error.code) != "-1121":
                        raise
                    self.discovery_rejections.append(
                        dict(
                            venue=venue, symbol=asset + "USDT", reason="spot_not_listed"
                        )
                    )
        elif venue == "bitget":
            spot = self._get("bitget", "/api/v2/spot/public/symbols")["data"]
            futures = self._get(
                "bitget", "/api/v2/mix/market/contracts", productType="usdt-futures"
            )["data"]
            for row in spot:
                if row["quoteCoin"] != "USDT":
                    continue
                instruments.append(
                    Instrument(
                        venue,
                        row["symbol"],
                        "spot",
                        row["baseCoin"],
                        row["quoteCoin"],
                        Decimal(10) ** -int(row["pricePrecision"]),
                        Decimal(10) ** -int(row["quantityPrecision"]),
                        dec(row.get("minTradeAmount", "0")),
                        dec(row["minTradeUSDT"]),
                        active=row["status"] == "online",
                    )
                )
            for row in futures:
                if (
                    row.get("quoteCoin") != "USDT"
                    or row.get("symbolType") != "perpetual"
                    or "USDT" not in row.get("supportMarginCoins", [])
                ):
                    continue
                instruments.append(
                    Instrument(
                        venue,
                        row["symbol"],
                        "linear",
                        row["baseCoin"],
                        row["quoteCoin"],
                        dec(row["priceEndStep"])
                        * Decimal(10) ** -int(row["pricePlace"]),
                        dec(row["sizeMultiplier"]),
                        dec(row["minTradeNum"]),
                        dec(row["minTradeUSDT"]),
                        active=row["symbolStatus"] == "normal",
                    )
                )
                self._intervals[(venue, row["symbol"])] = int(
                    dec(row["fundInterval"]) * HOUR
                )
        else:
            for kind, inst_type in (("spot", "SPOT"), ("linear", "SWAP")):
                rows = self._get(
                    "okx", "/api/v5/public/instruments", instType=inst_type
                )["data"]
                for row in rows:
                    if kind == "spot":
                        if row.get("quoteCcy") != "USDT":
                            continue
                        underlying, contract_size = row["baseCcy"], Decimal(1)
                    else:
                        if (
                            row.get("ctType") != "linear"
                            or row.get("settleCcy") != "USDT"
                        ):
                            continue
                        underlying = row["uly"].removesuffix("-USDT")
                        if row.get("ctValCcy") != underlying:
                            continue
                        contract_size = dec(row["ctVal"]) * dec(
                            row.get("ctMult") or "1"
                        )
                    instruments.append(
                        Instrument(
                            venue,
                            row["instId"],
                            kind,
                            underlying,
                            "USDT",
                            dec(row["tickSz"]),
                            dec(row["lotSz"]),
                            dec(row["minSz"]),
                            Decimal(0),
                            contract_size,
                            active=row["state"] == "live",
                        )
                    )
        self._instruments[venue] = {
            (item.kind, item.symbol): item for item in instruments
        }
        self._inventory_asof[venue] = observed
        return instruments

    def discover_pairs(self, venue, fees, approved_mappings=None):
        """Return exact-token defaults and explicitly configured share-token mappings.

        fees accepts maker_bps, hedge_bps, fee_in_base, target_notional and
        hedge_margin_fraction. Missing fee values remain None and fail the signal
        layer's cost check. approved_mappings is keyed by spot symbol with
        hedge_symbol, underlying and corporate_actions_ready fields.
        """
        venue = self._venue(venue)
        instruments = self.discover(venue)
        spot = {i.symbol: i for i in instruments if i.kind == "spot" and i.active}
        futures = {i.symbol: i for i in instruments if i.kind == "linear" and i.active}
        mappings = []
        for maker in spot.values():
            if maker.underlying not in AUTO_ASSETS:
                continue
            for hedge in futures.values():
                if maker.underlying == hedge.underlying and maker.quote == hedge.quote:
                    mappings.append((maker, hedge, True))
        for spot_symbol, mapping in (approved_mappings or {}).items():
            if spot_symbol not in spot and venue == "binance":
                item = self._binance_spot(spot_symbol)
                if item.active:
                    spot[spot_symbol] = item
                    self._instruments[venue][("spot", spot_symbol)] = item
            hedge = futures.get(mapping["hedge_symbol"])
            maker = spot.get(spot_symbol)
            if maker is None or hedge is None:
                raise PublicDataError(
                    f'Approved pair is unavailable: {spot_symbol}/{mapping["hedge_symbol"]}'
                )
            underlying = mapping.get("underlying")
            if not underlying:
                raise ValueError(
                    "Approved mapping requires an explicit underlying identity"
                )
            if dec(mapping.get("spot_units_per_share", "1")) != Decimal(1):
                raise ValueError(
                    "Only exchange-displayed share-equivalent stock quantities are supported"
                )
            mappings.append(
                (
                    replace(maker, underlying=underlying),
                    replace(hedge, underlying=underlying),
                    bool(mapping.get("corporate_actions_ready", False)),
                )
            )
        result = {}
        for maker, hedge, actions_ready in mappings:
            pair_id = f"{venue}:{maker.symbol}:{hedge.symbol}"
            pair = Pair(
                pair_id,
                maker,
                hedge,
                dec(fees["maker_bps"]) if fees.get("maker_bps") is not None else None,
                dec(fees["hedge_bps"]) if fees.get("hedge_bps") is not None else None,
                fee_in_base=bool(fees.get("fee_in_base", True)),
                mapping_verified=True,
                corporate_actions_ready=actions_ready,
                maker_route=(
                    "internal_market"
                    if venue == "bitget" and maker.symbol in (approved_mappings or {})
                    else None
                ),
                target_notional=dec(fees.get("target_notional", "100000")),
                hedge_margin_fraction=dec(fees.get("hedge_margin_fraction", "0.5")),
            )
            result[pair_id] = pair
        return list(result.values())

    def book(self, instrument):
        started = self.clock_ms()
        venue, symbol = self._venue(instrument.venue), instrument.symbol
        if venue == "binance":
            host = "binance_spot" if instrument.kind == "spot" else "binance"
            path = "/api/v3/depth" if instrument.kind == "spot" else "/fapi/v1/depth"
            payload = self._get(host, path, symbol=symbol, limit=100)
        elif venue == "bitget":
            if instrument.kind == "spot":
                payload = self._get(
                    venue,
                    "/api/v2/spot/market/orderbook",
                    symbol=symbol,
                    type="step0",
                    limit=100,
                )["data"]
            else:
                payload = self._get(
                    venue,
                    "/api/v2/mix/market/merge-depth",
                    symbol=symbol,
                    productType="usdt-futures",
                    precision="scale0",
                    limit=100,
                )["data"]
        else:
            rows = self._get(venue, "/api/v5/market/books", instId=symbol, sz=100)[
                "data"
            ]
            if len(rows) != 1:
                raise PublicDataError(f"Missing book for {symbol}")
            payload = rows[0]
        levels = {}
        for side in ("bids", "asks"):
            converted = []
            for row in payload[side]:
                price, quantity = dec(row[0]), dec(row[1]) * instrument.contract_size
                if price <= 0 or quantity < 0:
                    raise PublicDataError(f"Invalid {side} level for {symbol}")
                if quantity:
                    converted.append((price, quantity))
            if not converted:
                raise PublicDataError(f"Empty {side} for {symbol}")
            prices = [row[0] for row in converted]
            if prices != sorted(prices, reverse=side == "bids") or len(
                set(prices)
            ) != len(prices):
                raise PublicDataError(
                    f"Unordered or duplicate {side} levels for {symbol}"
                )
            levels[side] = tuple(converted)
        if levels["bids"][0][0] >= levels["asks"][0][0]:
            raise PublicDataError(f"Crossed or locked book for {symbol}")
        exchange_time = payload.get("ts", payload.get("T", started))
        received = min(started, int(exchange_time))
        return Book(levels["bids"], levels["asks"], received)

    def _interval(self, instrument):
        venue = self._venue(instrument.venue)
        key = (venue, instrument.symbol)
        if key not in self._intervals:
            if venue == "binance":
                values = self._get("binance", "/fapi/v1/fundingInfo")
                for row in values:
                    self._intervals[("binance", row["symbol"])] = (
                        int(row["fundingIntervalHours"]) * HOUR
                    )
                self._intervals.setdefault(key, 8 * HOUR)
            elif venue == "bitget":
                rows = self._get(
                    "bitget",
                    "/api/v2/mix/market/contracts",
                    productType="usdt-futures",
                    symbol=instrument.symbol,
                )["data"]
                match = next(
                    (r for r in rows if r["symbol"] == instrument.symbol), None
                )
                if match is None:
                    raise PublicDataError("Funding interval unavailable")
                self._intervals[key] = int(dec(match["fundInterval"]) * HOUR)
            else:
                rows = self._get(
                    "okx", "/api/v5/public/funding-rate", instId=instrument.symbol
                )["data"]
                if len(rows) != 1:
                    raise PublicDataError("Funding interval unavailable")
                self._intervals[key] = int(rows[0]["nextFundingTime"]) - int(
                    rows[0]["fundingTime"]
                )
        interval = self._intervals[key]
        if not 0 < interval <= DAY:
            raise PublicDataError("Invalid funding interval")
        return interval

    def funding_history(self, instrument, start_ms, end_ms):
        if instrument.kind != "linear" or not 0 <= start_ms < end_ms:
            raise ValueError(
                "Funding history requires a linear instrument and increasing window"
            )
        venue, symbol = self._venue(instrument.venue), instrument.symbol
        interval = self._interval(instrument)
        requested_start = max(0, start_ms - interval)
        records = {}
        cursor = requested_start
        for page in range(1, 101):
            if venue == "binance":
                rows = self._get(
                    venue,
                    "/fapi/v1/fundingRate",
                    symbol=symbol,
                    startTime=cursor,
                    endTime=end_ms - 1,
                    limit=1000,
                )
            elif venue == "bitget":
                rows = self._get(
                    venue,
                    "/api/v2/mix/market/history-fund-rate",
                    symbol=symbol,
                    productType="usdt-futures",
                    pageSize=100,
                    pageNo=page,
                )["data"]
            else:
                params = dict(instId=symbol, limit=100)
                if page > 1:
                    params["after"] = cursor
                rows = self._get(
                    venue, "/api/v5/public/funding-rate-history", **params
                )["data"]
            if not rows:
                break
            timestamps = []
            for row in rows:
                timestamp = int(row["fundingTime"])
                timestamps.append(timestamp)
                kind = row.get("rateType", "regular").lower()
                rate = dec(row.get("realizedRate") or row["fundingRate"])
                if requested_start <= timestamp < end_ms:
                    key = (timestamp, kind)
                    if key in records and records[key] != rate:
                        raise PublicDataError("Conflicting funding records")
                    records[key] = rate
            next_cursor = max(timestamps) + 1 if venue == "binance" else min(timestamps)
            if venue == "binance":
                if len(rows) < 1000:
                    break
            elif min(timestamps) <= requested_start or len(rows) < 100:
                break
            if page > 1 and next_cursor == cursor:
                raise PublicDataError("Funding pagination did not advance")
            cursor = next_cursor
        else:
            raise PublicDataError("Funding pagination exceeded its request bound")
        regular = sorted(t for t, kind in records if kind == "regular")
        if (
            not regular
            or regular[0] > start_ms + 1000
            or end_ms - regular[-1] > interval + 1000
        ):
            raise PublicDataError("Funding history does not cover requested window")
        if any(b - a > interval + 1000 for a, b in zip(regular, regular[1:])):
            raise PublicDataError(
                "Funding gap or interval change requires explicit reconciliation"
            )
        previous = None
        result = []
        for (timestamp, kind), rate in sorted(records.items()):
            duration = interval
            if kind == "regular":
                if previous is not None:
                    duration = round((timestamp - previous) / 1000) * 1000
                previous = timestamp
            if timestamp >= start_ms:
                result.append(FundingPoint(timestamp, rate, duration, kind))
        return tuple(result)

    def _hourly_closes(self, instrument, first_open, last_open):
        venue, symbol = self._venue(instrument.venue), instrument.symbol
        records = {}
        batch_size = {"binance": 1000, "bitget": 100, "okx": 300}[venue]
        if (last_open - first_open) // HOUR + 1 > batch_size * 100:
            raise PublicDataError("Candle history exceeds its request bound")
        for batch_first in range(first_open, last_open + HOUR, batch_size * HOUR):
            batch_end = min(last_open + HOUR, batch_first + batch_size * HOUR)
            if venue == "binance":
                host = "binance_spot" if instrument.kind == "spot" else "binance"
                path = (
                    "/api/v3/klines" if instrument.kind == "spot" else "/fapi/v1/klines"
                )
                rows = self._get(
                    host,
                    path,
                    symbol=symbol,
                    interval="1h",
                    startTime=batch_first,
                    endTime=batch_end - 1,
                    limit=batch_size,
                )
            elif venue == "bitget":
                rows = self._get(
                    venue,
                    "/api/v3/market/history-candles",
                    symbol=symbol,
                    category="SPOT" if instrument.kind == "spot" else "USDT-FUTURES",
                    interval="1H",
                    startTime=batch_first,
                    endTime=batch_end,
                    limit=batch_size,
                )["data"]
            else:
                rows = self._get(
                    venue,
                    "/api/v5/market/history-candles",
                    instId=symbol,
                    bar="1H",
                    after=batch_end,
                    limit=batch_size,
                )["data"]
            for row in rows:
                timestamp = int(row[0])
                if timestamp % HOUR:
                    raise PublicDataError("Candle timestamp is not aligned to an hour")
                if batch_first <= timestamp < batch_end:
                    if venue == "okx" and len(row) >= 9 and row[8] != "1":
                        continue
                    price = dec(row[4])
                    if price <= 0:
                        raise PublicDataError("Invalid historical price")
                    if timestamp in records and records[timestamp] != price:
                        raise PublicDataError("Conflicting historical candle")
                    records[timestamp] = price
        expected = set(range(first_open, last_open + HOUR, HOUR))
        if set(records) != expected:
            raise PublicDataError(
                f"{venue} {symbol} missing {len(expected - set(records))} closed hourly candles"
            )
        return records

    def basis_history(self, pair, start_ms, end_ms):
        if not 0 <= start_ms < end_ms:
            raise ValueError("Increasing history window required")
        first_close = ((start_ms + HOUR - 1) // HOUR) * HOUR
        last_close = end_ms // HOUR * HOUR
        if first_close > last_close:
            return ()
        first_open, last_open = first_close - HOUR, last_close - HOUR
        if first_open < 0:
            raise ValueError("History starts before a completed bar exists")
        maker = self._hourly_closes(pair.maker, first_open, last_open)
        hedge = self._hourly_closes(pair.hedge, first_open, last_open)
        return tuple(
            BasisSample(t + HOUR, (hedge[t] / maker[t] - 1) * BPS)
            for t in sorted(maker)
        )

    def market(self, pair, now_ms=None):
        """Read histories first and books last. Evaluate with a fresh clock afterward.

        now_ms sets the historical cutoff. Histories are cached within their
        completed-hour bucket. The returned book timestamps include request
        latency so slow requests become stale instead of appearing fresh.
        """
        now_ms = self.clock_ms() if now_ms is None else now_ms
        hour = now_ms // HOUR
        basis_key = (pair.pair_id, hour)
        funding_key = (pair.hedge.venue, pair.hedge.symbol, hour)
        if basis_key not in self._basis_cache:
            self._basis_cache = {
                key: value for key, value in self._basis_cache.items() if key[1] == hour
            }
            self._basis_cache[basis_key] = self.basis_history(
                pair, now_ms - 32 * DAY, now_ms
            )
        if funding_key not in self._funding_cache:
            self._funding_cache = {
                key: value
                for key, value in self._funding_cache.items()
                if key[2] == hour
            }
            self._funding_cache[funding_key] = self.funding_history(
                pair.hedge, now_ms - 30 * DAY, now_ms
            )
        maker_book, hedge_book = self.book(pair.maker), self.book(pair.hedge)
        return Market(
            pair.pair_id,
            maker_book,
            hedge_book,
            self._basis_cache[basis_key],
            self._funding_cache[funding_key],
        )
