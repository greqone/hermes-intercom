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
    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == _ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("hermes-intercom-repo-root"):
        config.pluginmanager.register(_RepoRootIsNotATestPackage(), "hermes-intercom-repo-root")
