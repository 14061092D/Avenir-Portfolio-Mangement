"""Single-process, durable runtime state. Never contains signing keys."""
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from decimal import Decimal


def encode(obj):
    return json.dumps(obj, default=lambda x: str(x) if isinstance(x, Decimal) else vars(x),
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


class AccountLock:
    """One live owner per account/network for this OS user, across state paths."""
    def __init__(self, address, network='hyperliquid-mainnet', root=None):
        directory=Path(root) if root else Path.home()/'.local/state/avenir-mm/account-locks'
        directory.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256((network+':'+address.lower()).encode()).hexdigest()
        self.file=(directory/(key+'.lock')).open('a')
        try:
            fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError('Another live process owns this account on this machine') from None

    def close(self):
        fcntl.flock(self.file,fcntl.LOCK_UN)
        self.file.close()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / 'process.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('Another process owns this state directory') from None
        self.db = sqlite3.connect(self.directory / 'state.sqlite')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, digest TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, time_ms INTEGER, kind TEXT, body TEXT)')
        self.db.commit()

    def load(self):
        row = self.db.execute('SELECT body,digest FROM state WHERE id=1').fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise RuntimeError('State checksum mismatch; refusing a fresh start')
        return json.loads(row[0])

    def save(self, state, time_ms, kind, event=None):
        body = encode(state)
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,digest=excluded.digest', (body,digest))
            self.db.execute('INSERT INTO events(time_ms,kind,body) VALUES(?,?,?)', (time_ms,kind,encode(event or {})))

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()
