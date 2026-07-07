"""Fetch a pinned external corpus snapshot for the value study.

Each corpus is a real third-party library, pinned to an exact release **tarball** so the study is
reproducible without committing thousands of vendored files. We download the tag's source archive
over HTTPS (stdlib only — no ``git`` at all), pin it by the **sha256 of the archive bytes**
recorded in ``CORPUS.<name>.lock``, and extract it into ``_snapshot/<name>/``.

**The third-party code is strictly read-only.** This script only downloads and extracts an
archive — there is no clone, no remote, no git history, and therefore nothing that could ever
push to the upstream repo. The snapshot dir is gitignored, so it also can never be staged into
a Cartogate commit.

The registry (:data:`CORPORA`) holds more than one corpus so the study spans a CLI app (``click``)
*and* a plain library (``jmespath``) — the latter's direct unit tests are where static
test-selection (V7) should fare far better than on click's dynamic ``runner.invoke`` dispatch.

Run: ``python -m evaluation.corpus.fetch_corpus [name]``
"""

from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Corpus:
    """A pinned third-party corpus: where to fetch it and how it is laid out."""

    name: str  # registry key + the importable package name (the dir Cartogate indexes)
    owner: str  # GitHub org
    repo: str  # GitHub repo (can differ from the package name, e.g. ``jmespath.py``)
    tag: str  # release tag, pinned
    package_subdir: str  # path within the archive to the importable package
    tests_subdir: str  # path within the archive to the test suite
    license: str
    kind: str  # "cli" | "library" — descriptive, surfaced in the report

    @property
    def url(self) -> str:
        return f"https://codeload.github.com/{self.owner}/{self.repo}/tar.gz/refs/tags/{self.tag}"

    @property
    def archive_prefix(self) -> str:
        return f"{self.repo}-{self.tag}/"  # GitHub names the top dir ``<repo>-<tag>/``

    @property
    def package(self) -> str:
        return self.package_subdir.rsplit("/", 1)[-1]  # "src/click" -> "click"


#: The pinned corpora. ``click`` (CLI) is the headline; ``jmespath`` (library) is the second,
#: non-CLI corpus where direct unit tests let static test-selection (V7) shine.
CORPORA: dict[str, Corpus] = {
    "click": Corpus(
        name="click", owner="pallets", repo="click", tag="8.1.7",
        package_subdir="src/click", tests_subdir="tests", license="BSD-3-Clause", kind="cli",
    ),
    "jmespath": Corpus(
        name="jmespath", owner="jmespath", repo="jmespath.py", tag="1.0.1",
        package_subdir="jmespath", tests_subdir="tests", license="MIT", kind="library",
    ),
}

DEFAULT_CORPUS = "click"

#: Backward-compatible module-level handles for the default (click) corpus.
CORPUS_NAME = DEFAULT_CORPUS
CORPUS_TAG = CORPORA[DEFAULT_CORPUS].tag

HERE = Path(__file__).resolve().parent


def _snapshot_dir(name: str) -> Path:
    return HERE / "_snapshot" / name


def _lock_file(name: str) -> Path:
    return HERE / f"CORPUS.{name}.lock"


def fetch(name: str = DEFAULT_CORPUS, force: bool = False) -> Path:
    """Download + extract the pinned corpus archive; return the importable package path.

    The archive's sha256 is verified against ``CORPUS.<name>.lock`` (created on first fetch), so
    the snapshot is byte-for-byte pinned. Nothing here can write to the upstream repository.
    """
    corpus = CORPORA[name]
    snapshot = _snapshot_dir(name)
    pkg = snapshot / corpus.package_subdir
    if pkg.exists() and not force:
        return pkg
    if snapshot.exists():
        shutil.rmtree(snapshot)

    blob = _download(corpus.url)
    _verify_lock(name, hashlib.sha256(blob).hexdigest())
    _extract(blob, snapshot, corpus.archive_prefix)
    if not pkg.exists():
        raise SystemExit(f"expected package at {pkg} after extraction; archive layout changed?")
    return pkg


def tests_dir(name: str = DEFAULT_CORPUS) -> Path:
    """The corpus's extracted test-suite directory (fetch first)."""
    return _snapshot_dir(name) / CORPORA[name].tests_subdir


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 (https, pinned host)
        return bytes(resp.read())


def _extract(blob: bytes, snapshot: Path, prefix: str) -> None:
    snapshot.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            dest = (snapshot / rel).resolve()
            # Defense-in-depth against path traversal in a crafted archive.
            if snapshot.resolve() not in dest.parents and dest != snapshot.resolve():
                raise SystemExit(f"archive member escapes snapshot dir: {name}")
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                extracted = tar.extractfile(member)
                if extracted is not None:
                    dest.write_bytes(extracted.read())


def _verify_lock(name: str, digest: str) -> None:
    lock = _lock_file(name)
    if lock.exists():
        pinned = lock.read_text(encoding="utf-8").strip()
        if pinned and pinned != digest:
            raise SystemExit(
                f"corpus {name} sha256 {digest} != locked {pinned}; delete {lock.name} to re-pin."
            )
    else:
        lock.write_text(digest + "\n", encoding="utf-8")


if __name__ == "__main__":
    target = next((a for a in sys.argv[1:] if not a.startswith("-")), DEFAULT_CORPUS)
    pkg = fetch(target, force="--force" in sys.argv)
    lock = _lock_file(target)
    sha = lock.read_text(encoding="utf-8").strip() if lock.exists() else "?"
    corpus = CORPORA[target]
    print(f"corpus ready: {pkg}  ({corpus.name} {corpus.tag}, sha256 {sha[:12]})")
