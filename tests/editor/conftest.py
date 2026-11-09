"""Fixtures for the TF Editor protocol tests."""

import pytest
from editor_fakes import SimEditor, build_dump, default_mixing_values, mixing_tree

from tf_daemon.console.editor.client import TFEditorClient
from tf_daemon.console.editor.codec import DESC_MD5
from tf_daemon.console.editor.dump import Dump


@pytest.fixture
def mixing():
    return Dump(build_dump(mixing_tree(), default_mixing_values(), md5=DESC_MD5["Mixing"]))


@pytest.fixture
def console():
    fake = SimEditor()
    yield fake
    fake.close()


@pytest.fixture
def client_for():
    clients = []

    def make(fake, **kw):
        c = TFEditorClient("127.0.0.1", fake.port, **kw)
        clients.append(c)
        c.start()
        return c

    yield make
    for c in clients:
        c.stop()
