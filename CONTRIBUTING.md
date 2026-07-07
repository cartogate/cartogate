# Contributing to Cartogate

Thanks for your interest! A note on how this project works:

**Development happens in a private repository.** This public repository is the distribution
channel — it receives one commit per release, and its `main`/`stable` branches and version
tags are protected and immutable, because they are what `pipx install` trusts.

- **Bug reports and feature requests**: please open an **issue** here — they are triaged
  directly into the development backlog, and fixes ship in the next release. A report that
  includes the output of `cartogate doctor` and, when relevant, the commit-gate output is
  usually enough to reproduce.
- **Pull requests**: because `main` only receives release commits, PRs cannot be merged here
  directly. If you open one, an accepted change is ported into the development repository with
  credit (`Co-authored-by`) and ships in the next release — your diff and your attribution
  both survive; only the merge mechanics differ.
- **Security issues**: please use GitHub's private vulnerability reporting rather than a
  public issue.

Releases follow [`docs/RELEASING.md`](./docs/RELEASING.md): `stable` always points at
the latest tagged release, and tags are never moved or deleted.
