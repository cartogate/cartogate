# Releasing

Cartogate installs come from git, so a release is a tag plus a branch move — no artifact uploads.

## Channels

| Ref | Meaning | Who uses it |
|---|---|---|
| `stable` (branch) | Always the latest tagged release. **Only ever fast-forwards to a tag.** | The README quick start; anyone who wants "current release" without tracking versions |
| `v<major>.<minor>.<patch>` (tags) | Immutable releases. Versions derive from these (hatch-vcs). | Pinned deployments |
| `main` | Development tip. Every commit builds a unique `X.Y.Z.devN+g<hash>` version. | Development and pre-release testing |

## Cutting a release

From a green `main`:

```bash
git checkout main && git pull
git tag v0.2.0                      # the version is DERIVED from this tag — nothing to edit
git push origin v0.2.0
git push origin v0.2.0^{}:stable    # fast-forward the stable branch to the tagged commit
```

Verify from a clean environment:

```bash
pipx install --force "git+https://github.com/cartogate/cartogate.git@stable"
cartogate --version                 # must print exactly 0.2.0
```

Rules:

- `stable` moves **only** to tagged commits, and only forward. Never force-push it; never point it
  at an untagged commit.
- Tags are never moved or deleted once pushed.
- No version numbers live in the source — `pyproject.toml` derives the version from git at build
  time, so tagging *is* the version bump.
