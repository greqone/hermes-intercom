"""Maintainer review: public delivery only, scope isolation and preserved safeguards."""
from __future__ import annotations

import ast
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_intercom_plugin import delivery, store

spec = importlib.util.spec_from_file_location('_public_helpers', Path(__file__).with_name('test_round5.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
pair, send = helpers._pair, helpers._send


@pytest.mark.parametrize('surface', ['cli', '', None, ' CLI '])
def test_unsafe_surfaces_never_inject(tmp_path, surface):
    home, ctx = pair(tmp_path)
    ctx.session_surface = lambda: surface
    result = send(ctx, target='local-target-0001', message='do not misroute')
    assert result['error'] == 'surface_cannot_route'
    assert not ctx.injected and not delivery._OPERATIONS


def test_unavailable_surface_fails_closed(tmp_path):
    home, ctx = pair(tmp_path)
    def broken():
        raise RuntimeError('host unavailable')
    ctx.session_surface = broken
    assert send(ctx, target='local-target-0001', message='test')['error'] == 'surface_cannot_route'
    assert not ctx.injected


@pytest.mark.parametrize('surface', ['desktop', 'tui', 'acp', 'discord', 'telegram'])
def test_public_injection_uses_exact_target_and_scheduling_status(tmp_path, surface):
    home, ctx = pair(tmp_path)
    ctx.session_surface = lambda: surface
    result = send(ctx, target='local-target-0001', message='hello', request_id='one')
    assert result['ok'] and result['status'] == 'scheduled'
    assert result['delivery_method'] == 'inject_message'
    assert result['mounted_dormant'] is False
    assert ctx.injected[0]['session_key'] == result['target_session_key']
    assert len(ctx.injected) == 1


@pytest.mark.parametrize('unrouted', [False, True])
@pytest.mark.parametrize('legacy_settings', [False, True])
def test_dormant_target_has_no_private_fallback(tmp_path, unrouted, legacy_settings):
    home, ctx = pair(tmp_path)
    ctx._allow_inject = False
    ctx.allow_gateway_injection = True
    ctx._settings.update(auto_mount_dormant=legacy_settings, mount_timeout_seconds=1)
    if unrouted:
        with sqlite3.connect(home/'state.db') as con:
            con.execute('DELETE FROM gateway_routing')
    first = send(ctx, target='local-target-0001', message='wake?', request_id='refused')
    second = send(ctx, target='local-target-0001', message='wake?', request_id='refused')
    assert first['error'] == second['error'] == ('route_unverified' if unrouted else 'injection_refused')
    assert not ctx.injected and not delivery._OPERATIONS and not delivery._RECENT_SENDS
    assert not hasattr(delivery, 'submit_via_gateway_ws')
    assert not hasattr(delivery, 'mount_dormant_local_session')


def test_host_permission_decides_without_reading_yaml(tmp_path, monkeypatch):
    home, ctx = pair(tmp_path)
    (home/'config.yaml').write_text('not: [valid yaml', encoding='utf-8')
    original = Path.read_text
    def guarded(path, *a, **kw):
        assert path.name != 'config.yaml', 'plugin must not parse host config'
        return original(path, *a, **kw)
    monkeypatch.setattr(Path, 'read_text', guarded)
    assert send(ctx, target='local-target-0001', message='host grants')['ok']
    ctx._allow_inject = False
    assert send(ctx, target='local-target-0001', message='host denies')['error'] == 'injection_refused'
    assert len(ctx.injected) == 1


def test_profile_home_uses_public_scope_never_context_private_fields(tmp_path):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    class PoisonContext:
        @property
        def _manager(self):
            raise AssertionError('private manager read')
        @property
        def home_path(self):
            raise AssertionError('undocumented context home read')
    for name in ['one', 'two']:
        home = tmp_path/name
        token = set_hermes_home_override(str(home))
        try:
            assert store.resolve_home(PoisonContext()) == home.resolve()
        finally:
            reset_hermes_home_override(token)


def _compress(home, sid, count, boundary=None):
    """Real SQLite lineage with the original routing owner retained."""
    current = sid
    with sqlite3.connect(home/'state.db') as con:
        for i in range(count):
            child = f'{sid}-compressed-{i}'
            reason = boundary if i == count//2 and boundary else 'compression'
            con.execute('UPDATE sessions SET ended_at=2, end_reason=? WHERE id=?', (reason,current))
            con.execute('INSERT INTO sessions (id,source,model,parent_session_id,started_at,message_count,title,archived,hidden,session_key,last_activity_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (child,'local','m',current,3+i,1,'Continuation',0,0,f'agent:main:local:dm:{sid}',10+i))
            current = child
    return current


@pytest.mark.parametrize('count', [1, 8, 30])
def test_retry_survives_sender_compression_without_second_injection(tmp_path, count):
    home, ctx = pair(tmp_path)
    first = send(ctx, target='local-target-0001', message='same', request_id='sender-compression')
    assert first['ok']
    sender = first['from_session_id']
    current = _compress(home, sender, count)
    result = json.loads(delivery.tool_session_send({'target':'local-target-0001','message':'same','request_id':'sender-compression'},ctx=ctx,session_id=current))
    assert result['ok'] and result['replayed']
    assert len(ctx.injected) == 1


@pytest.mark.parametrize('count', [8, 30])
def test_deep_target_compression_replays_and_new_send_routes(tmp_path, count):
    home, ctx = pair(tmp_path)
    first = send(ctx, target='local-target-0001', message='same', request_id='target-compression')
    assert first['ok']
    current = _compress(home, 'local-target-0001', count)
    retry = send(ctx, target=current, message='same', request_id='target-compression')
    assert retry['ok'] and retry['replayed']
    new = send(ctx, target=current, message='new', request_id='target-new')
    assert new['ok'] and new['target_session_id'] == current
    assert len(ctx.injected) == 2


@pytest.mark.parametrize('boundary', ['session_reset', 'new_session', 'session_switch'])
@pytest.mark.parametrize('which', ['sender', 'target'])
def test_retry_does_not_cross_reset_boundary(tmp_path, boundary, which):
    home, ctx = pair(tmp_path)
    first = send(ctx, target='local-target-0001', message='same', request_id='boundary')
    sid = first['from_session_id'] if which == 'sender' else 'local-target-0001'
    current = _compress(home, sid, 8, boundary)
    result = json.loads(delivery.tool_session_send({'target': current if which == 'target' else 'local-target-0001', 'message':'same','request_id':'boundary'},ctx=ctx,session_id=current if which == 'sender' else first['from_session_id']))
    assert not result['ok'] and not result.get('replayed')
    assert len(ctx.injected) == 1


def test_replay_does_not_change_original_envelope_after_rename(tmp_path):
    home, ctx = pair(tmp_path)
    first = send(ctx, target='local-target-0001', message='hello', request_id='stable')
    with sqlite3.connect(home/'state.db') as con:
        con.execute("UPDATE sessions SET title='Renamed'")
    retry = send(ctx, target='local-target-0001', message='hello', request_id='stable')
    assert retry == dict(first, replayed=True)
    assert len(ctx.injected) == 1


def test_concurrent_retry_has_one_injection(tmp_path):
    import threading
    home, ctx = pair(tmp_path)
    entered, release = threading.Event(), threading.Event()
    inject = ctx.inject_message
    outcomes = []
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return inject(*args, **kwargs)
    ctx.inject_message = blocked
    worker = threading.Thread(target=lambda: outcomes.append(send(ctx, target='local-target-0001', message='same', request_id='concurrent')))
    worker.start()
    try:
        assert entered.wait(5)
        duplicate = send(ctx, target='local-target-0001', message='same', request_id='concurrent')
        assert duplicate['error'] == 'request_in_flight'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and outcomes[0]['ok']
    assert len(ctx.injected) == 1


@pytest.mark.parametrize('mode', ['branch', 'cycle', 'bound'])
def test_compression_identity_fails_closed(tmp_path, mode):
    home, ctx = pair(tmp_path)
    count = store._LINEAGE_MAX + 1 if mode == 'bound' else 8
    current = _compress(home, 'local-target-0001', count)
    with sqlite3.connect(home/'state.db') as con:
        if mode == 'branch':
            con.execute('ALTER TABLE sessions ADD COLUMN model_config TEXT')
            con.execute('UPDATE sessions SET model_config=? WHERE id=?',
                        (json.dumps({'_branched_from': 'local-target-0001-compressed-6'}), current))
        elif mode == 'cycle':
            con.execute("UPDATE sessions SET parent_session_id=?, ended_at=2, end_reason='compression' WHERE id=?", (current, 'local-target-0001-compressed-0'))
    assert not store.compression_line(home, current, 'local-target-0001')


def test_runtime_has_no_private_delivery_or_yaml_gate():
    root = Path(__file__).resolve().parents[1]
    for filename in ['delivery.py', 'store.py', '__init__.py']:
        source = (root/filename).read_text(encoding='utf-8')
        tree = ast.parse(source)
        assert not any(isinstance(n,ast.Attribute) and n.attr in {'_manager','_cli_ref','rpc'} for n in ast.walk(tree))
        for forbidden in ['hermes_cli.gateway_', 'set_hermes_home_override', 'connect_gateway', 'prompt.submit', 'session.resume', 'session.detach']:
            assert forbidden not in source
    source = (root/'delivery.py').read_text(encoding='utf-8')
    assert 'config.yaml' not in source
