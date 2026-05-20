"""Unit-test-scoped fixtures.

Routes ``shared.dual_write`` through the handler-under-test's
``_get_dynamodb`` accessor so the existing test-mock pattern
(``@patch("...handler._get_dynamodb")`` + ``mock_dynamodb.return_value
.Table.return_value = mock_table``) intercepts dual_write's writes
without requiring per-test rewrites.

Background: handlers that previously wrote with ``table.put_item`` /
``batch_writer`` now route through ``dual_write.ddb_put_item`` /
``ddb_batch_put_items``, which by default constructs its own boto3
resource (``dual_write._dynamodb``). The autouse fixture below
replaces ``dual_write._ddb`` with a resolver that, at call time,
returns one of:

1. ``dual_write._dynamodb`` if a test explicitly populated it via
   ``dual_write._set_clients_for_tests(dynamodb=...)`` — the canonical
   pattern used by ``test_audit_writer_routing`` and
   ``test_shared_audit_writer``.
2. The result of calling ``_get_dynamodb`` on the immediate caller's
   module — i.e., whichever handler module is currently invoking
   ``dual_write.ddb_*``. Test-files like ``test_workspace_management``,
   ``test_api_keys``, ``test_mcp_workspace`` load the workspace handler
   multiple times under the same module name; ``sys.modules`` only
   tracks the latest instance, but stack inspection finds the actual
   caller regardless.
3. The default lazy boto3 resource — caught by the parent
   ``conftest.py``'s autouse moto wrap.

``REPLICATION_EVENT_BUS_NAME`` is also unset so dual_write's
EventBridge emit becomes a no-op during tests.
"""

from __future__ import annotations

import inspect

import pytest


def _walk_stack_for(dual_write, accessors: tuple[str, ...], resources: tuple[str, ...]):
    """Walk the call stack outward looking for handler-supplied
    boto3 wrappers in frame `f_globals`.

    ``accessors`` are names of zero-arg callables that return the
    resource (``_get_dynamodb`` etc.); when found, they are CALLED.
    ``resources`` are names that hold the resource/client object
    directly (``dynamodb``, ``s3_client``); when found, they are
    returned AS-IS.

    Reads `f_globals` directly to bypass `sys.modules` (which can
    only hold one instance per module name even when multiple test
    files have loaded the same handler under that name).
    """
    import inspect
    for frame_info in inspect.stack()[1:]:
        g = frame_info.frame.f_globals
        if g is dual_write.__dict__:
            continue
        for name in accessors:
            fn = g.get(name)
            if fn is not None:
                return fn()
        for name in resources:
            obj = g.get(name)
            if obj is not None:
                return obj
    return None


@pytest.fixture(autouse=True)
def _route_dual_write_through_handler(monkeypatch):
    from shared import dual_write

    def _ddb_resolver():
        # 1. Honor explicit `_set_clients_for_tests(dynamodb=...)`.
        if dual_write._dynamodb is not None:
            return dual_write._dynamodb

        # 2. Walk the call stack outward and look at each frame's
        #    `f_globals` for a handler-supplied DynamoDB resource.
        #    Reading `f_globals` directly bypasses `sys.modules`,
        #    which matters because each test file loads its OWN
        #    instance of the same handler module under the same
        #    `sys.modules` name, and only the most-recently-loaded
        #    one is in `sys.modules` even when an older one is
        #    currently executing. `frame.f_globals` is the actual
        #    loaded module dict for the function on that frame, so
        #    `@patch` on the test's loaded instance — or a test that
        #    overwrites a `dynamodb` module global directly — is
        #    visible regardless.
        #
        #    Handlers expose the resource in one of two shapes:
        #     - `_get_dynamodb()` accessor (workspace-management,
        #       most integration handlers).
        #     - `dynamodb` module global (sync workers, file-ingestion).
        resolved = _walk_stack_for(
            dual_write,
            accessors=("_get_dynamodb",),
            resources=("dynamodb",),
        )
        if resolved is not None:
            return resolved

        # 3. Default: lazy boto3 (intercepted by autouse moto wrap).
        import boto3

        dual_write._dynamodb = boto3.resource("dynamodb")
        return dual_write._dynamodb

    def _s3_resolver():
        # 1. Honor explicit `_set_clients_for_tests(s3=...)`.
        if dual_write._s3 is not None:
            return dual_write._s3

        # 2. Stack-walk for an `s3_client` (file-ingestion's pattern).
        resolved = _walk_stack_for(
            dual_write,
            accessors=(),
            resources=("s3_client",),
        )
        if resolved is not None:
            return resolved

        # 3. Default: lazy boto3 (intercepted by autouse moto wrap).
        import boto3

        dual_write._s3 = boto3.client("s3")
        return dual_write._s3

    monkeypatch.setattr(dual_write, "_ddb", _ddb_resolver)
    monkeypatch.setattr(dual_write, "_s3_client", _s3_resolver)
    # dual_write reads REPLICATION_EVENT_BUS_NAME at import time and
    # short-circuits `_emit` when it's empty. Patch the module global
    # (the env-var alone is too late post-import) so tests never try
    # to PutEvents against a real EventBridge bus.
    monkeypatch.setattr(dual_write, "REPLICATION_EVENT_BUS_NAME", "")
    dual_write._reset_clients_for_tests()
    yield
    dual_write._reset_clients_for_tests()
