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
    result = _send(ctx, target='local-target', message='ping')
    assert result['ok'] is False
    assert ctx.injected == []
    assert calls == []
    assert result['error'] == 'route_unverified'


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


@pytest.mark.parametrize('key,value,expected', [('max_hops',0,3), ('max_messages_per_window',0,10),
    ('burst_window_seconds',-1,600)])
def test_invalid_settings_keep_round7_documented_defaults(key, value, expected, tmp_path):
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
        assert result['error'] == 'store_unavailable'
        assert ctx.injected == []
    retried = _send(ctx, target='local-target', message='read race', request_id='db-race')
    assert retried['ok']
    assert len(ctx.injected) == 1


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


@pytest.mark.parametrize('synthetic', [None, '', '[CONTEXT COMPACTION — REFERENCE ONLY] summary'])
def test_missing_or_synthetic_hook_does_not_reset_exhausted_turn(tmp_path, synthetic):
    home = _home(tmp_path)
    _insert(home, [_row('local-caller-0001'), _row('local-target')])
    plugin = _load_plugin()
    ctx = RecordingContext(home)
    card = plugin.store.format_intercom_message(sender_card_name='Peer',
        sender_session_id='local-peer', hop=3, max_hops=3,
        expect_reply=True, message='stop')
    plugin.delivery.on_pre_llm_call(ctx=ctx, session_id='local-caller-0001', user_message=card)
    plugin.delivery.on_pre_llm_call(ctx=ctx, session_id='local-caller-0001', user_message=synthetic)
    result = _send(ctx, target='local-target', message='onward')
    assert result['error'] == 'max_hops_exceeded'
    assert ctx.injected == []


@pytest.mark.parametrize('cycle', [False, True])
def test_unresolved_hop_history_fails_closed_at_bound_or_cycle(tmp_path, monkeypatch, cycle):
    home = _home(tmp_path)
    _insert(home, [_row(f'local-chain-{n}', parent=f'local-chain-{n-1}' if n else
                       ('local-chain-3' if cycle else None), ended=2.0, reason='compression')
                   for n in range(4)] + [_row('local-target')])
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.store, '_LINEAGE_MAX', 8 if cycle else 2)
    ctx = RecordingContext(home)
    result = _send(ctx, target='local-target', message='onward', caller='local-chain-3')
    assert result['error'] == 'hop_state_unreadable'
    assert ctx.injected == []
