"""Central process: consumes deltas from edges, merges across receivers,
and serves the UI WebSocket.

Run with `python -m skywatch.central`.  Requires at least one
edge (or several) feeding it via the matching transport.
"""
