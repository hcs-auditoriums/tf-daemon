"""Fixtures for the TF Editor protocol tests."""

import pytest
from editor_fakes import build_dump, default_mixing_values, mixing_tree

from tf_daemon.console.editor.codec import DESC_MD5
from tf_daemon.console.editor.dump import Dump


@pytest.fixture
def mixing():
    return Dump(build_dump(mixing_tree(), default_mixing_values(), md5=DESC_MD5["Mixing"]))
