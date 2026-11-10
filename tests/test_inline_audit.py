"""Executable regressions discovered by the complete inline audit (no live IO)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

_spec = importlib.util.spec_from_file_location('_audit_helpers', Path(__file__).with_name('test_regression_phase2.py'))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_home, _insert, _send = _helpers._home, _helpers._insert, _helpers._send
RecordingContext = _helpers.RecordingContext
_load_plugin = _helpers._load_plugin


def _row(sid, *, key=None, title='Target', ended=None, reason=None, parent=None, activity=5.0):
    return (sid, 'local', 'm', parent, 1.0, ended, reason, 1, title, 0,
            key or f'agent:main:local:dm:{sid}', activity)


@pytest.mark.parametrize('address', ['local-retired-', 'Retired Only'])
def test_reset_predecessor_is_not_a_target_through_prefix_or_title(tmp_path, address):
    home = _home(tmp_path)
    shared = 'agent:main:local:dm:local-current'
    _insert(home, [_row('local-retired-0001', key=shared, title='Retired Only', ended=2.0, reason='session_reset'),
                   _row('local-current', key=shared, title='Current', activity=9.0)])
    plugin = _load_plugin()
    # The live conversation remains addressable; only the old conversation is refused.
    control, _ = plugin.store.resolve_target(home, 'local-current')
    assert control['session_id'] == 'local-current'
    resolved, candidates = plugin.store.resolve_target(home, address)
    assert resolved is None and candidates == []


def test_real_schema_hidden_summary_does_not_reset_hop(tmp_path):
    home = _home(tmp_path)
    _insert(home, [_row('local-parent', ended=2.0, reason='compression'),
                   _row('local-child', parent='local-parent'), _row('local-target')])
    plugin = _load_plugin()
    card = plugin.store.format_intercom_message(sender_card_name='A', sender_session_id='local-peer',
        hop=3, max_hops=3, expect_reply=True, message='stop')
    # The host persists display_kind and _compressed_summary, NOT *_has_user_turn.
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('ALTER TABLE messages ADD COLUMN _compressed_summary INTEGER DEFAULT 0')
        con.execute('ALTER TABLE messages ADD COLUMN display_kind TEXT')
        con.executemany('INSERT INTO messages(session_id,role,content,timestamp,_compressed_summary,display_kind) VALUES (?,\'user\',?,1,?,?)',
            [('local-parent', card, 0, 'internal_notification'),
             ('local-child', '[CONTEXT COMPACTION — REFERENCE ONLY] summary', 1, 'hidden')])
    result = _send(RecordingContext(home), target='local-target', message='onward', caller='local-child')
    assert result.get('error') == 'max_hops_exceeded'


@pytest.mark.parametrize('reason,marker', [('session_reset', '_reset_from'), (None, '_branched_from'), (None, '_delegate_from')])
def test_new_conversation_does_not_inherit_parent_loop_budget(tmp_path, reason, marker):
    home = _home(tmp_path)
    _insert(home, [_row('local-parent', ended=2.0, reason=reason),
                   _row('local-child', parent='local-parent'), _row('local-target')])
    plugin = _load_plugin()
    card = plugin.store.format_intercom_message(sender_card_name='A', sender_session_id='local-peer',
        hop=3, max_hops=3, expect_reply=True, message='stop')
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('ALTER TABLE sessions ADD COLUMN model_config TEXT')
        con.execute('UPDATE sessions SET model_config=? WHERE id=\'local-child\'', (json.dumps({marker:'local-parent'}),))
        con.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES (\'local-parent\',\'user\',?,1)', (card,))
    result = _send(RecordingContext(home), target='local-target', message='new task', caller='local-child')
    assert result['ok'] is True and result['hop'] == 1


def test_unknown_operations_are_bounded_without_forgetting_possible_delivery(tmp_path, monkeypatch):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    ctx = RecordingContext(home, allow_inject=False)
    ctx.allow_gateway_injection = True
    ctx._settings['auto_mount_dormant'] = False
    monkeypatch.setattr(plugin.delivery, '_OPERATION_MAX', 2)
    calls = []
    def submit(*args, **kwargs):
        calls.append(kwargs['request_id'])
        return False, 'TimeoutError'
    monkeypatch.setattr(plugin.delivery, 'submit_via_gateway_ws', submit)
    for n in range(2):
        sent = _send(ctx, target='local-target', message=f'body{n}', request_id=f'pending-{n}')
        assert sent['error'] == 'delivery_unknown'
    third = _send(ctx, target='local-target', message='body3', request_id='pending-3')
    assert third.get('error') == 'operation_capacity'
    assert len(plugin.delivery._OPERATIONS) == 2
    assert calls == ['pending-0', 'pending-1']
    # A known operation can still be reconciled while new admissions are blocked.
    retried = _send(ctx, target='local-target', message='body0', request_id='pending-0')
    assert retried['error'] == 'delivery_unknown'
    assert calls == ['pending-0', 'pending-1', 'pending-0']


def test_cache_pressure_cannot_evict_an_active_inject_claim(tmp_path, monkeypatch):
    import threading
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.delivery, '_OPERATION_MAX', 2)
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Blocking(RecordingContext):
        def inject_message(self, content, **kwargs):
            calls.append(content)
            if content.endswith('first'):
                entered.set()
                assert release.wait(5)
            return super().inject_message(content, **kwargs)
    ctx = Blocking(home)
    results = []
    worker = threading.Thread(target=lambda: results.append(_send(ctx, target='local-target', message='first', request_id='active')))
    worker.start()
    assert entered.wait(5)
    try:
        assert _send(ctx, target='local-target', message='other', request_id='other')['ok']
        repeat = _send(ctx, target='local-target', message='first', request_id='active')
        assert repeat.get('error') == 'request_in_flight'
        assert len(calls) == 2
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0]['ok']


@pytest.mark.parametrize('bad_source,bad_reason', [('tool', None), ('local', 'session_reset')])
def test_compression_alias_follows_only_compression_edges(tmp_path, bad_source, bad_reason):
    home = _home(tmp_path)
    _insert(home, [_row('local-parent', ended=2.0, reason='compression'),
                   _row('local-live', parent='local-parent'),
                   _row('local-other', parent='local-parent', ended=3.0 if bad_reason else None, reason=bad_reason),
                   _row('local-grandchild', parent='local-other')])
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('UPDATE sessions SET source=? WHERE id=\'local-other\'', (bad_source,))
    result, candidates = _load_plugin().store.resolve_target(home, 'local-parent')
    assert candidates == []
    assert result['session_id'] == 'local-live'


def test_shared_route_does_not_hide_ambiguous_compression_siblings(tmp_path):
    home = _home(tmp_path)
    shared = 'agent:main:local:dm:local-parent'
    _insert(home, [_row('local-parent', key=shared, ended=2.0, reason='compression'),
                   _row('local-left', key=shared, parent='local-parent', activity=6.0),
                   _row('local-right', key=shared, parent='local-parent', activity=7.0)])
    result, candidates = _load_plugin().store.resolve_target(home, 'local-parent')
    assert result is None
    assert {r['session_id'] for r in candidates} == {'local-left', 'local-right'}


def test_empty_routing_index_is_not_proof_of_a_live_route(tmp_path, monkeypatch):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    calls = []
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('DELETE FROM gateway_routing')
    # Reproduce the real host's false-positive synchronous admission for an unknown key.
    ctx = RecordingContext(home)
    ctx.allow_gateway_injection = True
    monkeypatch.setattr(plugin.delivery, 'mount_dormant_local_session',
                        lambda *a, **k: calls.append('mount') or (False, 'absent'))
    monkeypatch.setattr(plugin.delivery, 'submit_via_gateway_ws',
                        lambda *a, **k: calls.append('submit') or (False, 'pre_submit:absent'))
    result = _send(ctx, target='local-target', message='ping')
    assert result['ok'] is False
    assert ctx.injected == []
    assert calls == ['mount', 'submit']


def test_authoritative_route_beats_stale_session_metadata(tmp_path):
    home = _home(tmp_path)
    old_key = 'agent:main:local:dm:local-wrong'
    current_key = 'agent:main:local:dm:local-target'
    _insert(home, [_row('local-target', key=old_key), _row('local-wrong', key=old_key)])
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('INSERT INTO gateway_routing(session_key,entry_json) VALUES (?,?)',
                    (current_key, json.dumps({'session_id':'local-target'})))
    ctx = RecordingContext(home)
    result = _send(ctx, target='local-target', message='only target')
    assert result['ok'] is True
    assert result['target_session_key'] == current_key
    assert ctx.injected[0]['session_key'] == current_key


def test_receipt_reference_preserves_the_real_structured_host_contract(tmp_path, monkeypatch):
    import types
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    reference = {'profile_id': 'synthetic-profile', 'session_id': 'local-target'}
    class Client:
        async def rpc(self, method, **kwargs):
            if method == 'session.resume':
                return {'session_id':'local-target', 'subscription_id':'sub'}
            if method == 'prompt.submit':
                return {'admission_id':'adm', 'status':'terminal', 'outcome':'completed', 'ref':reference}
            return {}
    class Connection:
        async def __aenter__(self): return Client()
        async def __aexit__(self, *args): return False
    def attr(parts, name):
        if name == 'discover_gateway_endpoint':
            return lambda *a, **k: types.SimpleNamespace(state='ready', endpoint=object())
        return lambda **k: Connection()
    monkeypatch.setattr(plugin.delivery, '_load_core_attr', attr)
    ctx = RecordingContext(home, settings={'auto_mount_dormant':False}, allow_inject=False)
    ctx.allow_gateway_injection = True
    result = _send(ctx, target='local-target', message='receipt', request_id='real-ref')
    assert result['ok'] and result['outcome'] == 'completed'
    assert result['reference'] == reference


def test_no_submit_starts_after_the_shared_mount_deadline(tmp_path, monkeypatch):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    clock = {'now': 10.0}
    monkeypatch.setattr(plugin.delivery.time, 'monotonic', lambda: clock['now'])
    calls = []
    def mount(*a, **k):
        clock['now'] += 10
        return False, 'TimeoutError'
    monkeypatch.setattr(plugin.delivery, 'mount_dormant_local_session', mount)
    monkeypatch.setattr(plugin.delivery, 'submit_via_gateway_ws', lambda *a, **k: calls.append('submit') or (True, 'adm'))
    ctx = RecordingContext(home, settings={'mount_timeout_seconds':1}, allow_inject=False)
    ctx.allow_gateway_injection = True
    result = _send(ctx, target='local-target', message='timeout')
    assert not result['ok']
    assert calls == []


def test_receipt_refresh_clears_previous_outcome_when_it_is_absent(tmp_path, monkeypatch):
    import types
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    receipts = [{'admission_id':'adm', 'status':'terminal', 'outcome':'cancelled', 'ref':'old'},
                {'admission_id':'adm', 'status':'unknown', 'outcome':None}]
    class Client:
        async def rpc(self, method, **kwargs):
            if method == 'session.resume': return {'session_id':'local-target'}
            return receipts.pop(0)
    class Connection:
        async def __aenter__(self): return Client()
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(plugin.delivery, '_load_core_attr', lambda parts, name:
        (lambda *a, **k: types.SimpleNamespace(state='ready', endpoint=object()))
        if name == 'discover_gateway_endpoint' else (lambda **k: Connection()))
    ctx = RecordingContext(home, settings={'auto_mount_dormant':False}, allow_inject=False)
    ctx.allow_gateway_injection = True
    first = _send(ctx, target='local-target', message='receipt', request_id='refresh')
    assert first['outcome'] == 'cancelled'
    second = _send(ctx, target='local-target', message='receipt', request_id='refresh')
    assert second['status'] == 'unknown'
    assert second['outcome'] == '' and second['reference'] == ''


def test_current_human_hook_resets_a_previous_stored_intercom_budget(tmp_path):
    home = _home(tmp_path)
    _insert(home, [_row('local-caller-0001'), _row('local-target')])
    plugin = _load_plugin()
    exhausted = plugin.store.format_intercom_message(sender_card_name='A', sender_session_id='local-peer',
        hop=3, max_hops=3, expect_reply=True, message='stop')
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES (?,\'user\',?,1)',
                    ('local-caller-0001', exhausted))
    ctx = RecordingContext(home)
    # Current user input is authoritative even before its DB flush, not the older card.
    plugin.delivery.on_pre_llm_call(ctx=ctx, session_id='local-caller-0001', user_message='Please send a new message')
    sent = _send(ctx, target='local-target', message='new request')
    assert sent['ok'] and sent['hop'] == 1


@pytest.mark.parametrize('key,value,expected', [('max_hops',0,1), ('max_messages_per_window',0,1),
    ('burst_window_seconds',-1,1), ('mount_timeout_seconds',0.0,1.0)])
def test_below_minimum_settings_do_not_expand_a_guard(key, value, expected, tmp_path):
    ctx = RecordingContext(tmp_path, settings={key:value})
    assert _load_plugin().delivery.read_settings(ctx)[key] == expected


def test_refused_targets_do_not_leak_bucket_horizons(tmp_path):
    home = _home(tmp_path)
    _insert(home, [_row(f'local-refused-{n}') for n in range(5)])
    plugin = _load_plugin()
    ctx = RecordingContext(home, settings={'auto_mount_dormant':False}, allow_inject=False)
    ctx.allow_gateway_injection = False
    for n in range(5):
        result = _send(ctx, target=f'local-refused-{n}', message='refused')
        assert result['error'] == 'injection_refused'
    assert not plugin.delivery._RECENT_SENDS
    assert not plugin.delivery._BUCKET_HORIZON


def test_routing_read_failure_returns_an_error_and_releases_the_claim(tmp_path, monkeypatch):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    original = plugin.store._connect_ro
    ctx = RecordingContext(home)
    # Fail only the extra delivery-time read, after resolution and hop reads succeeded.
    def connect(path):
        import inspect
        if inspect.currentframe().f_back.f_code.co_name == 'tool_session_send':
            raise sqlite3.OperationalError('synthetic lock')
        return original(path)
    with monkeypatch.context() as scoped:
        scoped.setattr(plugin.store, '_connect_ro', connect)
        result = _send(ctx, target='local-target', message='read race', request_id='db-race')
        assert result['error'] == 'store_unreadable'
        assert ctx.injected == []
    retried = _send(ctx, target='local-target', message='read race', request_id='db-race')
    assert retried['ok']
    assert len(ctx.injected) == 1


def test_unknown_gateway_retry_cannot_lose_its_transport_to_routing_read_failure(tmp_path, monkeypatch):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    plugin = _load_plugin()
    ctx = RecordingContext(home, settings={'auto_mount_dormant':False}, allow_inject=False)
    ctx.allow_gateway_injection = True
    calls = []
    def submit(*a, **k):
        calls.append(k['request_id'])
        return (False, 'TimeoutError') if len(calls) == 1 else (True, 'adm')
    monkeypatch.setattr(plugin.delivery, 'submit_via_gateway_ws', submit)
    first = _send(ctx, target='local-target', message='same', request_id='sticky-ws')
    assert first['error'] == 'delivery_unknown'
    original = plugin.store._connect_ro
    def connect(path):
        import inspect
        if inspect.currentframe().f_back.f_code.co_name == 'tool_session_send':
            raise sqlite3.OperationalError('transient')
        return original(path)
    with monkeypatch.context() as scoped:
        scoped.setattr(plugin.store, '_connect_ro', connect)
        _send(ctx, target='local-target', message='same', request_id='sticky-ws')
    ctx._allow_inject = True
    last = _send(ctx, target='local-target', message='same', request_id='sticky-ws')
    assert last['ok'] and last['delivery_method'] == 'gateway_ws'
    assert ctx.injected == []


def test_hop_recovery_survives_more_than_five_compressions(tmp_path):
    home = _home(tmp_path)
    _insert(home, [_row(f'local-chain-{n}', parent=f'local-chain-{n-1}' if n else None,
                       ended=2.0 if n < 8 else None, reason='compression' if n < 8 else None)
                   for n in range(9)] + [_row('local-target')])
    plugin = _load_plugin()
    card = plugin.store.format_intercom_message(sender_card_name='A', sender_session_id='local-peer',
        hop=3, max_hops=3, expect_reply=True, message='stop')
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES (\'local-chain-0\',\'user\',?,1)', (card,))
    sent = _send(RecordingContext(home), target='local-target', message='onward', caller='local-chain-8')
    assert sent.get('error') == 'max_hops_exceeded'


def test_same_request_id_is_independent_between_profiles(tmp_path):
    contexts = []
    for name in ('first', 'second'):
        (tmp_path / name).mkdir()
        home = _home(tmp_path / name)
        _insert(home, [_row('local-target')])
        contexts.append(RecordingContext(home))
    first = _send(contexts[0], target='local-target', message='same', request_id='shared-id')
    second = _send(contexts[1], target='local-target', message='same', request_id='shared-id')
    assert first['ok'] and second['ok']
    assert len(contexts[0].injected) == len(contexts[1].injected) == 1


@pytest.mark.parametrize('request_id', [' leading', 'trailing ', '\t', 'bad\ud800id'])
def test_request_id_is_validated_without_silent_rewriting(tmp_path, request_id):
    home = _home(tmp_path)
    _insert(home, [_row('local-target')])
    ctx = RecordingContext(home)
    good = _send(ctx, target='local-target', message='valid', request_id='valid-id')
    assert good['ok']
    refused = _send(ctx, target='local-target', message='invalid', request_id=request_id)
    assert refused.get('error') == 'invalid_request_id'
    assert len(ctx.injected) == 1


def test_route_now_owned_by_another_session_is_not_used(tmp_path):
    home = _home(tmp_path)
    old_key = 'agent:main:local:dm:local-other'
    _insert(home, [_row('local-target', key=old_key), _row('local-other', key=old_key)])
    with sqlite3.connect(home / 'state.db') as con:
        con.execute('UPDATE gateway_routing SET entry_json=? WHERE session_key=?',
                    (json.dumps({'session_id':'local-other'}), old_key))
    ctx = RecordingContext(home, settings={'auto_mount_dormant':False})
    ctx.allow_gateway_injection = False
    refused = _send(ctx, target='local-target', message='only target')
    assert not refused['ok']
    assert ctx.injected == []
    # The actual owner is still a valid target.
    control = _send(ctx, target='local-other', message='only owner')
    assert control['ok'] and len(ctx.injected) == 1


def test_gateway_permission_uses_host_yaml_when_pyyaml_is_absent(tmp_path, monkeypatch):
    import builtins
    pytest.importorskip('ruamel.yaml', reason='requires the current Hermes YAML dependency')
    home = _home(tmp_path)
    (home / 'config.yaml').write_text('plugins:\n  entries:\n    hermes-intercom:\n      allow_gateway_injection: true\n', encoding='utf-8')
    plugin = _load_plugin()
    real_import = builtins.__import__
    def without_pyyaml(name, *args, **kwargs):
        if name == 'yaml':
            raise ModuleNotFoundError('PyYAML is not installed in the serving runtime')
        return real_import(name, *args, **kwargs)
    class Context:
        plugin_id = 'hermes-intercom'
    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, '__import__', without_pyyaml)
        assert plugin.delivery._is_gateway_injection_allowed(Context(), home)
    (home / 'config.yaml').write_text('plugins:\n  entries:\n    hermes-intercom:\n      allow_gateway_injection: false\n', encoding='utf-8')
    assert not plugin.delivery._is_gateway_injection_allowed(Context(), home)


def test_gateway_receipt_is_profile_scoped_for_the_same_id(tmp_path, monkeypatch):
    import types
    plugin = _load_plugin()
    profiles = []
    for name in ('one', 'two'):
        (tmp_path / name).mkdir()
        home = _home(tmp_path / name)
        _insert(home, [_row('local-target')])
        ctx = RecordingContext(home, settings={'auto_mount_dormant':False}, allow_inject=False)
        ctx.allow_gateway_injection = True
        profiles.append(ctx)
    class Client:
        async def rpc(self, method, **kwargs):
            if method == 'session.resume': return {'session_id':'local-target'}
            return {'admission_id':'admission', 'status':'terminal', 'outcome':'completed',
                    'ref':{'profile_id':'synthetic', 'session_id':'local-target'}}
    class Connection:
        async def __aenter__(self): return Client()
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(plugin.delivery, '_load_core_attr', lambda parts, name:
        (lambda *a, **k: types.SimpleNamespace(state='ready', endpoint=object()))
        if name == 'discover_gateway_endpoint' else (lambda **k: Connection()))
    first = _send(profiles[0], target='local-target', message='same', request_id='shared')
    assert first['outcome'] == 'completed'
    profiles[1]._allow_inject = True
    second = _send(profiles[1], target='local-target', message='same', request_id='shared')
    assert second['delivery_method'] == 'inject_message'
    assert second['status'] == 'scheduled' and second['outcome'] == '' and second['reference'] == ''
    assert len(profiles[1].injected) == 1
