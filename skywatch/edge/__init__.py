"""Edge-receiver process: BEAST → decode → transport push.

Run with `python -m skywatch.edge`; one instance per receiver site.
The edge does the heavy decode work locally and ships per-aircraft
state deltas to a central node via the configured transport.
"""
