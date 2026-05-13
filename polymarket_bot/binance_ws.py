import asyncio
import json
import logging
import threading
import time
import websockets
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class BinanceTicker:
    symbol: str
    price: float = 0.0
    timestamp: float = 0.0

class BinanceWsClient:
    """Real-time Binance price tracker using WebSockets.
    
    Runs in a background thread to maintain sub-100ms price updates
    without blocking the main strategy loop.
    """
    def __init__(self, symbol: str = "BTCUSDT"):
        self.symbol = symbol.lower()
        self.ticker = BinanceTicker(symbol=symbol.upper())
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._listen())

    async def _listen(self):
        uri = f"wss://stream.binance.com:9443/ws/{self.symbol}@ticker"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(uri) as websocket:
                    while not self._stop_event.is_set():
                        msg = await websocket.recv()
                        data = json.loads(msg)
                        self.ticker.price = float(data.get("c", 0))
                        self.ticker.timestamp = time.time()
            except Exception as e:
                logger.error(f"Binance WS error: {e}")
                if not self._stop_event.is_set():
                    await asyncio.sleep(5)

    def get_latest_price(self) -> float:
        return self.ticker.price

    def get_latency_ms(self) -> float:
        if self.ticker.timestamp == 0:
            return float('inf')
        return (time.time() - self.ticker.timestamp) * 1000.0
