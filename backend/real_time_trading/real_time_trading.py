from typing import Dict, Optional

import math
from kafka import KafkaConsumer
from kafka import KafkaProducer
from raw_ticker import RawTicker
from ib_insync import Ticker
from ib_insync.contract import Stock

from datetime import datetime, timedelta
import utils
from utc_datetime import UTCDateTime
import constants
from intraday_ticker import IntradayTicker, IntradayEvent
from top_symbol import TopSymbol, TopNSymbols
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

logging.getLogger("kafka").setLevel(logging.DEBUG)

formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)

fh = logging.FileHandler(constants.RTT_LOG_FILE)
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
logger.addHandler(fh)


class RealTimeTrading:
    def __init__(
        self,
        contracts: list[Stock],
        bake_in_minutes: int = 0,
        bake_out_minutes: int = 0,
        n_top_tickers: int = 4,
        n_bottom_tickers: int = 4,
        _bypass_update_window: bool = False,
    ):
        # set up kafka consumer and producer
        self._consumer = KafkaConsumer(
            constants.RAW_TICKER_EVENT,
            bootstrap_servers=constants.KAFKA_BOOTSTRAP_SERVERS,
            key_deserializer=utils.bytes2str,
            value_deserializer=utils.bytes2object,
            auto_offset_reset="earliest",
        )
        # shared producer for all tickers
        self._intraday_producer = KafkaProducer(
            bootstrap_servers=constants.KAFKA_BOOTSTRAP_SERVERS,
            key_serializer=utils.str2bytes,
            value_serializer=utils.object2bytes,
            acks=1,
            retries=3,
            max_in_flight_requests_per_connection=1,
        )

        self._top_symbol_producer = KafkaProducer(
            bootstrap_servers=constants.KAFKA_BOOTSTRAP_SERVERS,
            # key_serializer=utils.str2bytes,
            value_serializer=utils.object2bytes,
            acks=1,
            retries=3,
            max_in_flight_requests_per_connection=1,
        )

        self.tickers: Dict[str, IntradayTicker] = {}
        for contract in contracts:
            self.tickers[contract.symbol] = IntradayTicker(
                contract=contract,
                kafka_producer=self._intraday_producer,
            )

        self.bake_in_minutes = bake_in_minutes
        self.bake_out_minutes = bake_out_minutes
        self.n_top_tickers = n_top_tickers
        self.n_bottom_tickers = n_bottom_tickers
        self.top_n_symbols: Optional[TopNSymbols] = None
        self.bottom_n_symbols: Optional[TopNSymbols] = None
        self._bypass_update_window = _bypass_update_window

    def inside_update_window(self, dt: UTCDateTime) -> bool:
        open_, close = utils.get_market_open_close(dt)
        if open_ is None or close is None:
            return False
        open_after_bake_in = open_ + timedelta(minutes=self.bake_in_minutes)
        close_before_bake_out = close - timedelta(
            minutes=self.bake_out_minutes
        )
        return dt >= open_after_bake_in and dt < close_before_bake_out

    def rank_tickers(self):
        tickers = list(self.tickers.values())
        tickers.sort(key=lambda t: t.gap)
        return tickers

    def get_top_n_tickers(
        self, time: UTCDateTime, tickers: list[IntradayTicker], n: int
    ) -> TopNSymbols:
        """Return top n tickers by gap with a positive gap"""
        return TopNSymbols.from_tickers(
            time, [t for t in tickers if t.gap > 0][:n]
        )

    def get_bottom_n_tickers(
        self, time: UTCDateTime, tickers: list[IntradayTicker], n: int
    ) -> TopNSymbols:
        """Return bottom n tickers by gap with a negative gap"""
        return TopNSymbols.from_tickers(
            time, [t for t in tickers if t.gap < 0][:n]
        )

    def _check_top_symbols(self, time: UTCDateTime):
        tickers = self.rank_tickers()
        top_n_symbols = self.get_top_n_tickers(
            time, tickers, self.n_top_tickers
        )
        if self.top_n_symbols is None or top_n_symbols != self.top_n_symbols:
            self.top_n_symbols = top_n_symbols
            self._top_symbol_producer.send(
                constants.TOP_HIGH_EVENT,
                value=self.top_n_symbols.to_message(),
            )
            logger.info("sending top high event")
        bottom_n_symbols = self.get_bottom_n_tickers(
            time, tickers, self.n_bottom_tickers
        )
        if (
            self.bottom_n_symbols is None
            or bottom_n_symbols != self.bottom_n_symbols
        ):
            self.bottom_n_symbols = bottom_n_symbols
            self._top_symbol_producer.send(
                constants.TOP_LOW_EVENT,
                value=self.bottom_n_symbols.to_message(),
            )
            logger.info("sending top low event")

    def _consume(self, message):
        # TODO: more efficient way to filter out old tickers
        today = utils.datetime2datestr(utils.get_local_now())
        logger.debug("today: %s", today)
        raw_ticker = RawTicker.from_message(message.value)
        logger.debug("raw_ticker time: %s", raw_ticker.time.to_timezone())
        # if utils.datetime2datestr(raw_ticker.time.to_timezone()) < today:
        #     logger.warning("ticker is from previous day: %s", raw_ticker.time)
        #     return
        if not self._bypass_update_window and not self.inside_update_window(
            raw_ticker.time
        ):
            logger.warning(
                "ticker is outside update window: %s", raw_ticker.time
            )
            return
        if math.isnan(raw_ticker.last):
            logger.warning("ticker has nan last price: %s", raw_ticker)
            return
        intraday_ticker = self.tickers.get(raw_ticker.symbol)
        if intraday_ticker is None:
            logger.warning(
                "ticker not found for symbol: %s", raw_ticker.symbol
            )
            return
        updated = intraday_ticker.update(raw_ticker)
        if updated:
            self._check_top_symbols(raw_ticker.time)

    def consume(self):
        for msg in self._consumer:
            logger.debug("consuming message: %s", msg)
            self._consume(msg)


if __name__ == "__main__":
    CONTRACTS = [Stock(**stk) for stk in constants.CONTRACTS]
    rtt = RealTimeTrading(contracts=CONTRACTS, _bypass_update_window=True)
    rtt.consume()
