"""Test bootstrap: load the plugin directory as package ``hermes_intercom_plugin``."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "hermes_intercom_plugin"
_ROOT = Path(__file__).resolve().parents[1]

sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _ROOT]


def load_plugin_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


load_plugin_package()


class _RepoRootIsNotATestPackage:
    @pytest.hookimpl(tryfirst=True, optionalhook=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("hermes-intercom-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "hermes-intercom-repo-root")


def _clear_delivery_state() -> None:
    delivery = importlib.import_module(f"{PACKAGE}.delivery")
    with delivery._LOCK:
        delivery._RECENT_SENDS.clear()
        delivery._TURN_HOP.clear()
        delivery._OPERATIONS.clear()
        delivery._BUCKET_HORIZON.clear()


@pytest.fixture(autouse=True)
def _reset_intercom_delivery_state(monkeypatch: pytest.MonkeyPatch):
    """Isolate per-home dedup buckets, turn-hop memory, and the ambient session key between tests."""
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    _clear_delivery_state()
    yield
    _clear_delivery_state()


@pytest.fixture(autouse=True)
def _emulate_profile_scoped_host_dispatch():
    """Unit doubles run inside the same public home scope as real host dispatch.

    Do not patch resolve_home: it must call the real public get_hermes_home API.
    Real PluginManager integration tests bypass this helper (their contexts have
    no test-only _intercom_test_context flag).
    """
    from functools import wraps
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    delivery = importlib.import_module(f"{PACKAGE}.delivery")
    def scoped(fn):
        @wraps(fn)
        def dispatch(*args, **kwargs):
            ctx = kwargs.get("ctx")
            if not getattr(ctx, "_intercom_test_context", False):
                return fn(*args, **kwargs)
            token = set_hermes_home_override(str(ctx.home_path))
            try:
                return fn(*args, **kwargs)
            finally:
                reset_hermes_home_override(token)
        return dispatch
    with pytest.MonkeyPatch.context() as scope_patch:
        for name in ("tool_session_list", "tool_session_send", "on_pre_llm_call"):
            scope_patch.setattr(delivery, name, scoped(getattr(delivery, name)))
        yield
