import pytest
from avenir_mm.cli import dispatch,load_config,pending_expired
from avenir_mm.venue import SubmissionNotSent


def test_proven_not_sent_is_rejected_but_ambiguous_transport_is_not():
    class Before:
        def submit(self,a): raise SubmissionNotSent('not_sent: quota')
    assert dispatch(Before(),None).status=='rejected'
    class Ambiguous:
        def submit(self,a): raise TimeoutError('unknown transmission')
    with pytest.raises(TimeoutError):dispatch(Ambiguous(),None)


def test_shadow_selection_overrides_live_config_without_reading_keys(tmp_path,monkeypatch):
    monkeypatch.setenv('HL_AGENT_PRIVATE_KEY','not-a-key-must-never-be-read')
    p=tmp_path/'config.yaml';p.write_text('mode: live\n')
    assert load_config(p,'shadow').mode=='shadow'
    p.write_text('mode: shadow\n')
    with pytest.raises(ValueError,match='mode: live'):
        load_config(p,'live')


def test_every_pending_status_has_a_deadline_including_accepted_cancel():
    from types import SimpleNamespace
    from avenir_mm.config import Config
    state=SimpleNamespace(pending=object(),pending_since_ms=1000,pending_status='accepted')
    engine=SimpleNamespace(state=state,config=Config())
    assert not pending_expired(engine,10000)
    assert pending_expired(engine,11001)
    state.pending_status='unknown'
    assert pending_expired(engine,11001)
    state.pending=None
    assert not pending_expired(engine,11001)
