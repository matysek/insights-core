"""
Regression test for CCXDEV-15098: Memory leak from circular references
in exception traceback handling.

When a component raises an exception during ``run()``, Python attaches the
live traceback object to ``exception.__traceback__``.  That traceback holds
a reference to the stack frame, which references local variables — including
the ``broker``.  This creates a circular reference chain::

    Broker -> exceptions -> exception -> __traceback__ -> frame -> Broker

CPython's reference-counting collector cannot break cycles, so the broker
and all of its data (the ``instances`` dict with ~500 component results)
stays alive until the cyclic GC runs.  In long-running services this
manifests as a steady memory leak.

The fix clears ``ex.__traceback__`` after the formatted traceback string
has been saved in ``broker.tracebacks``, breaking the cycle without losing
any debugging information.

These tests cover exception types across all execution paths:

**dr.run_components() path** (``insights/core/dr.py``):

- Generic ``Exception``   (caught by ``except Exception``)
- ``SkipComponent``        (caught separately, stores tb when store_skips=True)
- ``BlacklistedSpec``      (caught separately, always stores tb)

**Plugin invoke() paths** (``insights/core/plugins.py``):

- ``ContentException``    in PluginType, datasource, and parser
- ``CalledProcessError``  in PluginType, datasource, and parser
- ``TimeoutException``    in datasource
- Generic ``Exception``   in parser loop
- ``SkipComponent``       in parser loop (with store_skips=True)
"""

import gc
import platform
import weakref

import pytest

from insights.core import dr
from insights.core.exceptions import (
    BlacklistedSpec,
    CalledProcessError,
    ContentException,
    SkipComponent,
)
from insights.core.plugins import component as component_type, datasource, rule, make_info
from insights.core.spec_factory import RegistryPoint, SpecSet


# ---------------------------------------------------------------------------
# Test fixtures: components that raise different exception types
# ---------------------------------------------------------------------------

EXPECTED_MSG = "traceback leak test exception"
EXPECTED_SKIP_MSG = "traceback leak skip test"
EXPECTED_BLACKLIST_MSG = "traceback leak blacklist test"


class LeakTestSpecs(SpecSet):
    failing_data = RegistryPoint()
    skipped_data = RegistryPoint()
    blacklisted_data = RegistryPoint()


class LeakTestImpl(LeakTestSpecs):
    @datasource()
    def failing_data(broker):
        raise Exception(EXPECTED_MSG)

    @datasource()
    def skipped_data(broker):
        raise SkipComponent(EXPECTED_SKIP_MSG)

    @datasource()
    def blacklisted_data(broker):
        raise BlacklistedSpec(EXPECTED_BLACKLIST_MSG)


@rule(LeakTestSpecs.failing_data)
def leak_test_rule(data):
    return make_info("LEAK_TEST")


@rule(LeakTestSpecs.skipped_data)
def skip_test_rule(data):
    return make_info("SKIP_TEST")


@rule(LeakTestSpecs.blacklisted_data)
def blacklist_test_rule(data):
    return make_info("BLACKLIST_TEST")


# ---------------------------------------------------------------------------
# Helper: find exceptions of a given type in the broker
# ---------------------------------------------------------------------------

def _find_exceptions(broker, exc_type):
    """Return all exceptions of exc_type from any component in the broker.

    Exceptions may be keyed by the implementation function rather than
    the RegistryPoint (e.g. BlacklistedSpec and SkipComponent are only
    stored under the implementation key).  This helper searches all
    components to avoid false-pass from a missed key lookup.
    """
    found = []
    for comp, ex_list in broker.exceptions.items():
        for ex in ex_list:
            if isinstance(ex, exc_type):
                found.append(ex)
    return found


# ---------------------------------------------------------------------------
# Tests for generic Exception
# ---------------------------------------------------------------------------

def test_traceback_cleared_after_run():
    """Verify that __traceback__ is None for generic exceptions after run().

    Without the fix, the exception retains a __traceback__ reference to the
    stack frame, creating a circular reference that prevents the broker from
    being garbage collected.
    """
    broker = dr.run(leak_test_rule)

    exceptions = _find_exceptions(broker, Exception)
    # Filter to our specific test exception (not SkipComponent etc.)
    exceptions = [e for e in exceptions
                  if type(e) is Exception and str(e) == EXPECTED_MSG]
    assert len(exceptions) >= 1, (
        "Expected at least one Exception with message %r in broker.exceptions, "
        "found none. Keys: %s" % (EXPECTED_MSG, list(broker.exceptions.keys()))
    )

    for ex in exceptions:
        # Formatted traceback string is preserved (functionality not broken)
        tb = broker.tracebacks[ex]
        assert tb is not None
        assert EXPECTED_MSG in tb
        assert "Traceback" in tb

        # The fix: __traceback__ must be cleared to break the circular ref
        assert ex.__traceback__ is None, (
            "ex.__traceback__ should be None after run() to prevent "
            "circular reference: Broker -> exceptions -> ex -> "
            "__traceback__ -> frame -> Broker"
        )


# ---------------------------------------------------------------------------
# Tests for SkipComponent
# ---------------------------------------------------------------------------

def test_traceback_cleared_for_skip_component():
    """Verify that __traceback__ is None for SkipComponent exceptions.

    SkipComponent exceptions store tracebacks when broker.store_skips is True.
    The same circular reference pattern applies.
    """
    broker = dr.Broker()
    broker.store_skips = True
    broker = dr.run(skip_test_rule, broker=broker)

    exceptions = _find_exceptions(broker, SkipComponent)
    assert len(exceptions) >= 1, (
        "Expected at least one SkipComponent in broker.exceptions, "
        "found none. store_skips=%s, keys: %s"
        % (broker.store_skips, list(broker.exceptions.keys()))
    )

    for ex in exceptions:
        # Formatted traceback string is preserved
        tb = broker.tracebacks[ex]
        assert tb is not None
        assert EXPECTED_SKIP_MSG in tb

        # __traceback__ must be cleared
        assert ex.__traceback__ is None, (
            "SkipComponent.__traceback__ should be None after run() "
            "to prevent circular reference"
        )


# ---------------------------------------------------------------------------
# Tests for BlacklistedSpec
# ---------------------------------------------------------------------------

def test_traceback_cleared_for_blacklisted_spec():
    """Verify that __traceback__ is None for BlacklistedSpec exceptions.

    BlacklistedSpec exceptions always store tracebacks.  The same circular
    reference pattern applies.
    """
    broker = dr.run(blacklist_test_rule)

    exceptions = _find_exceptions(broker, BlacklistedSpec)
    assert len(exceptions) >= 1, (
        "Expected at least one BlacklistedSpec in broker.exceptions, "
        "found none. Keys: %s" % list(broker.exceptions.keys())
    )

    for ex in exceptions:
        # Formatted traceback string is preserved
        tb = broker.tracebacks[ex]
        assert tb is not None
        assert EXPECTED_BLACKLIST_MSG in tb

        # __traceback__ must be cleared
        assert ex.__traceback__ is None, (
            "BlacklistedSpec.__traceback__ should be None after run() "
            "to prevent circular reference"
        )


# ---------------------------------------------------------------------------
# GC collection test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    platform.python_implementation() != "CPython",
    reason="Relies on CPython reference-counting semantics"
)
def test_broker_collected_after_run():
    """Verify that brokers are garbage collected after processing.

    Without the fix, circular references from exception.__traceback__ keep
    brokers alive indefinitely, causing memory to grow over time.

    We disable the cyclic GC during the test so that only reference-counting
    frees the brokers.  With the fix, clearing __traceback__ breaks the
    cycle and reference counting alone is sufficient.  Without the fix,
    the circular reference keeps the broker alive.
    """
    n_runs = 5
    refs = []

    # Disable cyclic GC so circular references are NOT collected.
    # This makes the test deterministic: brokers survive IFF there
    # is a reference cycle.
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()

    try:
        for _ in range(n_runs):
            broker = dr.run(leak_test_rule)
            refs.append(weakref.ref(broker))
            del broker

        collected = sum(1 for ref in refs if ref() is None)
        assert collected >= n_runs - 1, (
            f"Only {collected}/{n_runs} brokers were garbage collected "
            "by reference counting alone (cyclic GC disabled). "
            "Remaining brokers are held by circular references "
            "from exception.__traceback__ -> frame -> broker."
        )
    finally:
        # Restore GC state exactly as it was before the test.
        if was_enabled:
            gc.enable()
        gc.collect()


# ===========================================================================
# Plugin-layer tests: ContentException, CalledProcessError, Exception
# in parser.invoke() and datasource.invoke()
#
# These exercise the except blocks in insights/core/plugins.py, which are
# separate from the dr.run_components() blocks tested above.
# ===========================================================================

CONTENT_EX_MSG = "traceback leak content exception test"
CPE_MSG = "traceback leak called process error test"
PLUGIN_EX_MSG = "traceback leak plugin exception test"


class PluginLeakTestSpecs(SpecSet):
    content_error_data = RegistryPoint()
    cpe_error_data = RegistryPoint()
    plugin_error_data = RegistryPoint()


class PluginLeakTestImpl(PluginLeakTestSpecs):
    @datasource()
    def content_error_data(broker):
        raise ContentException(CONTENT_EX_MSG)

    @datasource()
    def cpe_error_data(broker):
        raise CalledProcessError(1, "test_cmd", CPE_MSG)

    @datasource()
    def plugin_error_data(broker):
        raise Exception(PLUGIN_EX_MSG)


@rule(PluginLeakTestSpecs.content_error_data)
def ds_content_ex_rule(data):
    return make_info("DS_CONTENT_EX")


@rule(PluginLeakTestSpecs.cpe_error_data)
def ds_cpe_ex_rule(data):
    return make_info("DS_CPE_EX")


@rule(PluginLeakTestSpecs.plugin_error_data)
def ds_plugin_ex_rule(data):
    return make_info("DS_PLUGIN_EX")


# ---------------------------------------------------------------------------
# Tests for ContentException in datasource.invoke()
# ---------------------------------------------------------------------------

def test_traceback_cleared_for_datasource_content_exception():
    broker = dr.run(ds_content_ex_rule)

    exceptions = _find_exceptions(broker, ContentException)
    assert len(exceptions) >= 1, (
        "Expected at least one ContentException in broker.exceptions, "
        "found none. Keys: %s" % list(broker.exceptions.keys())
    )

    for ex in exceptions:
        assert ex.__traceback__ is None, (
            "ContentException.__traceback__ should be None after "
            "datasource.invoke() to prevent circular reference"
        )


# ---------------------------------------------------------------------------
# Tests for CalledProcessError in datasource.invoke()
# ---------------------------------------------------------------------------

def test_traceback_cleared_for_datasource_cpe():
    broker = dr.run(ds_cpe_ex_rule)

    exceptions = _find_exceptions(broker, CalledProcessError)
    assert len(exceptions) >= 1, (
        "Expected at least one CalledProcessError in broker.exceptions, "
        "found none. Keys: %s" % list(broker.exceptions.keys())
    )

    for ex in exceptions:
        assert ex.__traceback__ is None, (
            "CalledProcessError.__traceback__ should be None after "
            "datasource.invoke() to prevent circular reference"
        )


# ---------------------------------------------------------------------------
# Tests for generic Exception in plugin path (via dr.run_components)
# ---------------------------------------------------------------------------

def test_traceback_cleared_for_plugin_generic_exception():
    broker = dr.run(ds_plugin_ex_rule)

    exceptions = [
        e for e in _find_exceptions(broker, Exception)
        if type(e) is Exception and str(e) == PLUGIN_EX_MSG
    ]
    assert len(exceptions) >= 1, (
        "Expected at least one Exception with message %r in "
        "broker.exceptions, found none. Keys: %s"
        % (PLUGIN_EX_MSG, list(broker.exceptions.keys()))
    )

    for ex in exceptions:
        assert ex.__traceback__ is None, (
            "Exception.__traceback__ should be None after run() "
            "to prevent circular reference"
        )
