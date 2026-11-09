"""Finite, observable strategy runs. Shadow mode never constructs a signer."""
import argparse
from dataclasses import asdict, replace
import json
import signal
import time
from pathlib import Path
import yaml

from .config import Config
from .domain import Book, D, Level, Precision, Result
from .engine import Engine
from .feed import MarketFeed
from .paper import PaperVenue
from .store import Store, AccountLock, encode
from .venue import LiveVenue, PairMetadata, SnapshotChanged, SubmissionNotSent, discover_pair


def now_ms(): return time.time_ns() // 1_000_000


def dispatch(venue, action):
    try:
        return venue.submit(action)
    except SubmissionNotSent as exc:
        # This typed exception is raised only before a mutation reaches the network.
        return Result('rejected',error=str(exc))


def pending_expired(engine, current_ms):
    return bool(engine.state.pending and current_ms-engine.state.pending_since_ms > min(10000,engine.config.dust_wait_ms))


def status_payload(engine, snapshot=None, feed=None):
    st=engine.state
    result={'time_ms':now_ms(),'mode':engine.config.mode,'capital_usdc':str(engine.config.capital_usdc),
            'halted_reason':st.halted_reason,'spot_btc':str(st.spot_qty),'perp_btc':str(st.perp_qty),
            'unhedged_btc':str(st.spot_qty+st.perp_qty),'completed_cycles':st.completed_cycles,
            'realized_pnl_usdc':str(st.realized_pnl),'pending':st.pending.to_dict() if st.pending else None,
            'actions_created':st.sequence,'fills_seen':len(st.seen_fills),
            'execution_evidence':'paper scenarios, not actual fills' if engine.config.mode=='shadow' else 'operator-launched live run'}
    if snapshot:
        result.update(equity_usdc=str(snapshot.account.equity),spot_bid=str(snapshot.spot.bid),
                      spot_ask=str(snapshot.spot.ask),perp_bid=str(snapshot.perp.bid),perp_ask=str(snapshot.perp.ask),
                      spot_age_ms=snapshot.now_ms-snapshot.spot.time_ms,perp_age_ms=snapshot.now_ms-snapshot.perp.time_ms,
                      open_orders=len(snapshot.orders))
    if feed: result.update(feed_connected=feed.connected,feed_messages=feed.messages,feed_error=feed.error)
    return result


def write_status(directory, payload):
    target=Path(directory)/'status.json'; temp=target.with_suffix('.tmp')
    temp.write_text(json.dumps(payload,indent=2))
    temp.replace(target)


def load_config(path, command):
    raw=yaml.safe_load(Path(path).read_text()) if path else {}
    if not isinstance(raw,dict): raise ValueError('Config must be a mapping')
    config=Config.from_dict(raw)
    if command=='shadow': return replace(config,mode='shadow')
    if command=='live' and config.mode!='live':
        raise ValueError('Live mode also requires mode: live in an explicitly selected config')
    return config


def run(args):
    if args.seconds<=0 or (args.command=='live' and args.seconds>1800):
        raise ValueError('Duration must be positive; live pilot is limited to 1800 seconds per supervised run')
    if args.command=='live' and not args.confirm_live:
        raise ValueError('Operator must explicitly pass --confirm-live')
    config=load_config(args.config,args.command)
    from hyperliquid.info import Info
    # Public metadata only; no environment keys are accessed on this path.
    public=Info('https://api.hyperliquid.xyz',skip_ws=True,timeout=5)
    pair=discover_pair(public)
    if config.spot_coin!=pair.spot_coin:
        raise ValueError('Configured spot coin no longer matches verified UBTC metadata')
    directory=args.state_dir or f'runtime/{args.command}'
    store=Store(directory); feed=None; venue=None; engine=None; last=None; account_lock=None
    stop=False; normal_end=False; force=False; last_resolve=0; last_status=0; snapshot_changes=0
    previous_handlers={}
    def interrupt(_sig,_frame):
        nonlocal stop
        stop=True
    try:
        old=store.load()
        if old and old['config']!=config.to_dict():
            raise ValueError('Saved configuration differs; do not reuse another strategy/account state directory')
        engine=Engine(config,pair.precision,old['engine'] if old else None)
        if not engine.state.started_ms: engine.state.started_ms=now_ms()
        if old and engine.state.halted_reason:
            if args.resume_clean_stop and engine.state.halted_reason in ('session_complete','operator_stop'):
                engine.state.halted_reason=''
            else:
                raise ValueError('Saved strategy is halted; inspect status. --resume-clean-stop only resumes a normal supervised stop')
        # Live account address is only read after explicit live selection above.
        import os
        address=os.environ.get('HL_ACCOUNT_ADDRESS') if args.command=='live' else None
        feed=MarketFeed(pair.spot_coin,pair.perp_coin,address=address)
        feed.start(); deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:
                spot,perp=feed.books()
                if max(now_ms()-spot.time_ms,now_ms()-perp.time_ms)<config.max_book_age_ms: break
            except RuntimeError: pass
            if feed.error: raise RuntimeError(feed.error)
            time.sleep(.1)
        else: raise RuntimeError('Could not obtain fresh paired books')
        if args.command=='shadow':
            venue=PaperVenue(config,pair,feed.books,old.get('paper') if old else None)
        else:
            account_lock=AccountLock(address or '')
            venue=LiveVenue.from_env(config,pair,feed.books,operator_confirmed=args.confirm_live)
            if old and old.get('account_address','').lower()!=venue.address.lower():
                raise ValueError('Saved state belongs to another trading account')
            known=set(engine.state.oid_to_cloid.values())
            needed=[a for a in engine.state.owned_actions.values() if a.cloid in known or a==engine.state.pending]
            venue.register_actions(needed,engine.state.oid_to_cloid)
            venue.preflight(engine.state.started_ms)
            armed=venue.refresh_deadman(force=True)
            if armed.status!='accepted': raise RuntimeError(armed.error or 'Deadman could not be confirmed')
        def persist(kind,event=None):
            state={'config':config.to_dict(),'engine':engine.state.to_dict(),
                   'paper':venue.to_dict() if isinstance(venue,PaperVenue) else None,
                   'account_address':getattr(venue,'address','')}
            store.save(state,now_ms(),kind,event)
        persist('startup')
        for sig in (signal.SIGINT,signal.SIGTERM):
            previous_handlers[sig]=signal.signal(sig,interrupt)
        end=time.monotonic()+args.seconds
        print(json.dumps({'event':'started','mode':config.mode,'seconds':args.seconds,'state_dir':str(directory),'spot':pair.spot_coin,'perp':'BTC'}),flush=True)
        while time.monotonic()<end and not stop:
            if not feed.connected or feed.error:
                raise RuntimeError(feed.error or 'Market/account feed disconnected')
            if isinstance(venue,PaperVenue):
                for trade in feed.drain_trades(): venue.observe_trade(trade)
            else:
                feed.drain_trades()  # No fill inference in live mode.
                if feed.account_changed.is_set():
                    feed.account_changed.clear(); force=True
                armed=venue.refresh_deadman()
                if armed.status!='accepted': raise RuntimeError(armed.error)
            if engine.state.pending and now_ms()-last_resolve>=1000:
                action=engine.state.pending
                resolved=venue.resolve(action)
                engine.record_result(action,resolved)
                persist('resolve',asdict(resolved));last_resolve=now_ms()
                force=True
                if pending_expired(engine,now_ms()):
                    raise RuntimeError('Unresolved order/cancellation exceeded its deadline; no blind resubmission is allowed')
            try:
                last=venue.snapshot(engine.state.started_ms,force=force);force=False
                snapshot_changes=0
            except SnapshotChanged:
                snapshot_changes+=1;force=True
                if snapshot_changes>=3: raise RuntimeError('Account could not be reconciled after three changing snapshots')
                time.sleep(.5);continue
            last=replace(last,healthy=feed.connected and not feed.error)
            action=engine.step(last)
            if action:
                persist('intent',action.to_dict())  # Must precede any network mutation.
                result=dispatch(venue,action)
                engine.record_result(action,result)
                persist('result',asdict(result))
                force=True
                print(encode({'event':'action','action':action.to_dict(),'result':asdict(result)}),flush=True)
            elif now_ms()-last_status>=1000:
                persist('checkpoint')
            if now_ms()-last_status>=5000:
                payload=status_payload(engine,last,feed);write_status(directory,payload)
                print(json.dumps({'event':'status',**payload}),flush=True);last_status=now_ms()
            if engine.state.halted_reason and engine.state.pending is None:
                # One final step can still cancel a known maker; do not continue opening.
                if not last.orders: break
            time.sleep(.1)
        normal_end=not engine.state.halted_reason
        if not engine.state.halted_reason:
            engine.state.halted_reason='operator_stop' if stop else 'session_complete'
        persist('stopping')
        # Finite shutdown handles only known cancellations and already-existing hedge debt.
        # It never silently liquidates an intact matched position.
        cleanup_deadline=time.monotonic()+10
        while time.monotonic()<cleanup_deadline:
            if engine.state.pending:
                a=engine.state.pending;r=venue.resolve(a);engine.record_result(a,r);persist('shutdown_resolve',asdict(r))
                if r.status=='unknown': break
            last=venue.snapshot(engine.state.started_ms,force=not isinstance(venue,PaperVenue))
            last=replace(last,healthy=feed.connected and not feed.error)
            a=engine.step(last)
            if not a:
                if engine.state.pending: time.sleep(.25);continue
                break
            persist('shutdown_intent',a.to_dict())
            r=dispatch(venue,a);engine.record_result(a,r);persist('shutdown_result',asdict(r))
            time.sleep(.25)
        payload=status_payload(engine,last,feed);write_status(directory,payload)
        print(json.dumps({'event':'finished',**payload}),flush=True)
        return 0 if normal_end and engine.state.pending is None else 2
    except Exception as exc:
        if engine and venue:
            if not engine.state.halted_reason: engine.state.halted_reason='runtime_error'
            # Durable error state and emergency cancel only. No blind hedge resubmission.
            try:
                current={str(o.oid) for o in last.orders} if last else set()
                if engine.state.pending and engine.state.pending.market=='spot':
                    current.update(o for o,c in engine.state.oid_to_cloid.items() if c==engine.state.pending.cloid)
                for oid in current:
                    cloid=engine.state.oid_to_cloid.get(oid)
                    action=engine.state.owned_actions.get(cloid)
                    if action and action.kind=='place' and action.market=='spot':
                        cancel=replace(action,kind='cancel',oid=oid,reason='runtime_error_cancel')
                        store.save({'config':config.to_dict(),'engine':engine.state.to_dict(),
                                    'paper':venue.to_dict() if isinstance(venue,PaperVenue) else None,
                                    'account_address':getattr(venue,'address','')},now_ms(),'emergency_cancel_intent',cancel.to_dict())
                        dispatch(venue,cancel)
                store.save({'config':config.to_dict(),'engine':engine.state.to_dict(),
                            'paper':venue.to_dict() if isinstance(venue,PaperVenue) else None,
                            'account_address':getattr(venue,'address','')},now_ms(),'error',{'type':type(exc).__name__})
                write_status(directory,status_payload(engine,last,feed))
            except Exception:
                pass  # Confirmed live deadman remains the independent cancellation backstop.
        print(json.dumps({'event':'error','message':str(exc)}),flush=True)
        return 2
    finally:
        for sig,handler in previous_handlers.items(): signal.signal(sig,handler)
        if feed: feed.close()
        if account_lock: account_lock.close()
        store.close()


def demo():
    """Deterministic full-cycle example; invented market data, not performance evidence."""
    from types import SimpleNamespace
    config=Config();clock=[1_800_000_000_000]
    def books():
        t=clock[0]
        return (Book('@142',t,t,(Level(D('100'),D('10')),),(Level(D('100.02'),D('10')),)),
                Book('BTC',t,t,(Level(D('100'),D('10')),),(Level(D('100.02'),D('10')),)))
    pair=SimpleNamespace(spot_coin='@142',perp_coin='BTC',precision=Precision(6,5))
    engine=Engine(config,pair.precision)
    venue=PaperVenue(config,pair,books,clock_ms=lambda:clock[0],participation=D('1'))
    trace=[]
    for _ in range(30):
        if engine.state.pending:
            a=engine.state.pending;engine.record_result(a,venue.resolve(a))
        snap=venue.snapshot(0)
        action=engine.step(snap)
        if action:
            result=venue.submit(action);engine.record_result(action,result)
            trace.append({'action':action.to_dict(),'result':asdict(result)})
        if engine.state.completed_cycles:break
        clock[0]+=500
        for o in list(venue.orders.values()):
            venue.observe_trade({'coin':'@142','time':clock[0],'tid':len(trace)*100+clock[0],
                                 'side':'A' if o.side=='buy' else 'B',
                                 'px':str(o.price-D('.01') if o.side=='buy' else o.price+D('.01')),
                                 'sz':str(o.qty)})
    result={'synthetic':True,'completed_cycles':engine.state.completed_cycles,
            'spot_qty':str(engine.state.spot_qty),'perp_qty':str(engine.state.perp_qty),
            'pnl_usdc':str(engine.state.realized_pnl),'halted_reason':engine.state.halted_reason,'trace':trace}
    print(encode(result))
    return 0 if engine.state.completed_cycles==1 and engine.state.spot_qty==0 and engine.state.perp_qty==0 and not engine.state.halted_reason else 2


def main():
    parser=argparse.ArgumentParser(description='Hyperliquid BTC spot/perp pilot; default to shadow')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('shadow','live'):
        p=sub.add_parser(name);p.add_argument('--config');p.add_argument('--seconds',type=int,default=300)
        p.add_argument('--state-dir');p.add_argument('--resume-clean-stop',action='store_true')
        p.add_argument('--confirm-live',action='store_true')
    sub.add_parser('demo')
    p=sub.add_parser('status');p.add_argument('--state-dir',default='runtime/shadow')
    args=parser.parse_args()
    if args.command=='demo': code=demo()
    elif args.command=='status':
        print((Path(args.state_dir)/'status.json').read_text());code=0
    else:
        try: code=run(args)
        except (ValueError,RuntimeError) as exc:
            print(json.dumps({'event':'error','message':str(exc)}));code=2
    raise SystemExit(code)

if __name__=='__main__':main()
