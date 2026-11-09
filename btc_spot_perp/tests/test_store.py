import json
import pytest
from avenir_mm.store import Store,AccountLock


def test_roundtrip_and_exclusive_process_lock(tmp_path):
    s=Store(tmp_path)
    s.save({'pending':'intent-before-network'},1,'intent')
    assert s.load()=={'pending':'intent-before-network'}
    with pytest.raises(RuntimeError,match='Another process'):
        Store(tmp_path)
    s.close()
    reopened=Store(tmp_path)
    assert reopened.load()['pending']=='intent-before-network'
    reopened.close()


def test_corrupt_state_is_not_treated_as_empty(tmp_path):
    s=Store(tmp_path)
    s.save({'held_btc':'0.1'},1,'fill')
    with s.db: s.db.execute("UPDATE state SET body='{}' WHERE id=1")
    with pytest.raises(RuntimeError,match='checksum'):
        s.load()
    s.close()


def test_live_account_lock_cannot_be_bypassed_with_another_state_directory(tmp_path):
    a=AccountLock('0xABC',root=tmp_path)
    with pytest.raises(RuntimeError,match='Another live process'):
        AccountLock('0xabc',root=tmp_path)
    other=AccountLock('0xDEF',root=tmp_path);other.close()
    a.close()
    reopened=AccountLock('0xABC',root=tmp_path);reopened.close()
