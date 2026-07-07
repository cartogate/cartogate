"""Cartogate: local-first, CPG-shaped typed code knowledge graph + deterministic gate engine.

See README.md for scope and docs/dev/ROADMAP.md for the roadmap.
"""

#: The installed distribution's version — derived from git at build time (hatch-vcs), so every
#: commit yields a unique version (0.1.1.devN+g<hash>) and a running build always identifies its
#: exact source. Falls back for source-tree imports that were never installed.
try:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _dist_version

    __version__ = _dist_version("cartogate")
except PackageNotFoundError:  # an uninstalled source tree still imports fine
    __version__ = "0.0.0+uninstalled"
