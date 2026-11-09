"""WebSocket market cache. Network callbacks never change trading state."""
import json
import queue
import threading
import time
import websocket
from .domain import Book, Level, D


class MarketFeed:
    def __init__(self, spot_coin, perp_coin='BTC', address=None, url='wss://api.hyperliquid.xyz/ws'):
        self.spot_coin, self.perp_coin = spot_coin, perp_coin
        self.address, self.url = address, url
        self.lock = threading.Lock()
        self.cache = {}
        self.trades = queue.Queue(maxsize=10000)
        self.account_changed = threading.Event()
        self.stop_event = threading.Event()
        self.connected = False
        self.error = ''
        self.ws = None
        self.started_ms = int(time.time()*1000)
        self.messages = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        try:
            self.ws = websocket.create_connection(self.url, timeout=5)
            subs = [{'type':'l2Book','coin':c,'fast':True} for c in (self.spot_coin,self.perp_coin)]
            subs.append({'type':'trades','coin':self.spot_coin})
            if self.address:
                subs += [{'type':'userFills','user':self.address}, {'type':'userEvents','user':self.address}]
            for s in subs:
                self.ws.send(json.dumps({'method':'subscribe','subscription':s}))
            self.connected = True
            while not self.stop_event.is_set():
                try:
                    message = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    self.ws.send('{"method":"ping"}')
                    continue
                self.messages += 1
                received = int(time.time()*1000)
                channel, data = message.get('channel'), message.get('data')
                if channel == 'error':
                    raise RuntimeError('WebSocket subscription rejected')
                if channel == 'l2Book':
                    levels = [tuple(Level(D(x['px']),D(x['sz'])) for x in side) for side in data['levels']]
                    if not all(levels):
                        raise RuntimeError('Empty market book')
                    if any(x.price <= 0 or x.qty < 0 for side in levels for x in side) or levels[0][0].price >= levels[1][0].price:
                        raise RuntimeError('Invalid or crossed book')
                    book = Book(data['coin'],int(data['time']),received,*levels)
                    with self.lock:
                        previous = self.cache.get(book.coin)
                        if previous is None or book.time_ms > previous.time_ms:
                            self.cache[book.coin] = book
                elif channel == 'trades':
                    for trade in data:
                        if int(trade['time']) >= self.started_ms:
                            self.trades.put_nowait(trade)
                elif channel in ('userFills','user'):
                    self.account_changed.set()
        except Exception as exc:
            if not self.stop_event.is_set():
                self.error = type(exc).__name__ + ': public/private feed stopped'
        finally:
            self.connected = False
            if self.ws:
                self.ws.close()

    def books(self):
        with self.lock:
            if self.spot_coin not in self.cache or self.perp_coin not in self.cache:
                raise RuntimeError('Waiting for both market books')
            return self.cache[self.spot_coin], self.cache[self.perp_coin]

    def drain_trades(self):
        out = []
        while True:
            try:
                out.append(self.trades.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self.stop_event.set()
        if self.ws:
            self.ws.close()
        self.thread.join(timeout=6)
