"""Persistent local daemon: a warm, always-fresh graph over a token-authed local TCP socket.

A long-lived process holds the graph warm and answers the full tool surface, refreshing on
git-detected change (the git-lazy floor). Two modes: the default **structural** daemon serves the
duplicate gate (``check_duplicate``) cheaply; a **``--resolve``** daemon holds the full resolved
graph and serves every tool (``blast_radius`` / ``find_references`` / …) — refreshed incrementally
(F-36). Callers fall back to an in-process index when the daemon isn't running (or isn't resolved),
so the daemon is a pure accelerator. F-37 (watchdog) was dropped (git-lazy fails safe).
"""
