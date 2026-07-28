Memory Profiling with Memray
============================

Long-running services that use insights-core (e.g. ``insights-client`` daemon
mode, CCX data pipeline workers) can accumulate memory over time.  This guide
shows how to use `memray <https://bloomberg.github.io/memray/>`_ to find and
fix memory leaks.

Install
-------

.. code-block:: bash

   pip install memray

Basic profiling
---------------

Profile a single insights-core run and generate a flamegraph:

.. code-block:: bash

   # Record allocations
   memray run -o profile.bin python -c "
   from insights.core import dr
   from insights import specs  # noqa: import triggers component registration
   broker = dr.Broker()
   broker = dr.run(broker=broker)
   "

   # Generate an interactive flamegraph (opens in browser)
   memray flamegraph profile.bin -o flamegraph.html

Finding leaks
-------------

The ``--leaks`` flag shows only allocations that are still alive when the
process exits — the most useful view for hunting memory leaks:

.. code-block:: bash

   memray flamegraph --leaks profile.bin -o leaks.html

For a text summary of the top allocators:

.. code-block:: bash

   memray summary profile.bin
   memray stats profile.bin

Profiling a long-running service
--------------------------------

Attach to an already-running process to capture a live snapshot:

.. code-block:: bash

   # Attach to a running insights worker (records until Ctrl-C)
   memray attach <PID>

   # Or run the service under memray from the start
   memray run --live -o profile.bin python -m insights.worker

Comparing before and after
--------------------------

To verify that a fix actually reduces memory usage:

.. code-block:: bash

   # Record baseline
   memray run -o before.bin python your_workload.py

   # Apply fix, then record again
   memray run -o after.bin python your_workload.py

   # Side-by-side comparison
   memray compare before.bin after.bin

What to look for
----------------

When profiling insights-core specifically, watch for these patterns:

1. **Broker.instances growth** — The broker stores all component results
   (~500 entries per run).  In long-running services, check that brokers are
   freed after each evaluation cycle.

2. **Exception traceback retention** — Python attaches a live traceback to
   ``exception.__traceback__``, creating circular references back to the
   broker.  The fix clears ``__traceback__`` after capturing the formatted
   string.  If you see ``Broker`` objects surviving in the leak flamegraph,
   check that all ``add_exception()`` call sites clear ``__traceback__``.

3. **Parser/content provider data** — Large parsed file contents stay in
   memory as long as the broker's ``instances`` dict references them.  After
   serialization to disk, these are no longer needed but are not automatically
   evicted.

4. **Module-level caches** — ``insights.core.filters._CACHE`` and
   ``insights.core.dr.DELEGATES`` grow monotonically during component
   registration.  These are bounded by the number of registered components
   and are generally not a concern, but worth checking in plugin-heavy
   deployments.

Scripted leak detection
-----------------------

For CI or automated testing, use ``memray`` programmatically with ``gc``
and ``weakref`` to assert that brokers are collected:

.. code-block:: python

   import gc
   import weakref
   from insights.core import dr

   gc.collect()
   gc.disable()   # force reference-counting only

   broker = dr.run(your_rule)
   ref = weakref.ref(broker)
   del broker

   assert ref() is None, "Broker not collected — likely circular reference"
   gc.enable()

See ``insights/tests/test_traceback_leak.py`` for the full regression test
suite.
