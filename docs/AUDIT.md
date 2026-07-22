# Cartogate Audit Ledger

The audit ledger is a tamper-evident, append-only record of every gate decision Cartogate makes—both blocks and passes. It enables verification, traceability, and a chain-of-custody for code governance decisions.

## The Ledger

**Location:** `.cartogate/ledger.jsonl` (gitignored, local to the repository)

**Format:** One JSON object per line, forming a blake2b hash chain. An edit, deletion, reorder, or truncation of any *already-followed* entry (i.e. any entry that is not the most recent) is detectable by `cartogate audit verify`. The most-recent entry is subject to the **tail bound** described under Security Properties.

**Entry types:**
- `commit_pass` — a commit was allowed through the gate (no duplicates introduced, all advisories checked).
- `commit_block` — a commit was rejected because it introduces duplicates.
- `write_block` — a write-time duplicate was detected and blocked before the file was saved.

Each entry records:
- **Metadata:** sequence number, timestamp, actor identity (asserted: git committer, OS user, or agent name).
- **Decision:** the gate's verdict and the evidence it was based on (duplicate groups, signature matches, etc.).
- **Reproducibility token:** `decision_hash` — a blake2b of the decision type, tree hash, and evidence, so re-running the deterministic gate on the same tree produces an identical token.
- **Chain link:** `prev` (hash of the prior entry) and `hash` (blake2b of this entry), forming an immutable chain.

## Commands

### `cartogate audit verify`

Checks the ledger's integrity and reports gate coverage:

```bash
cartogate audit verify
```

**Exit code:**
- `0` — ledger is intact; hash chain is valid, all entries are authentic.
- `1` — ledger has been tampered with (corrupt JSON, hash mismatch, chain break, or a fabricated commit).

**Output:** Confirms "Ledger intact" with the count of chained decisions. If the ledger can reach git history (read-only check), reports the fraction of recent commits that were stamped by the gate.

### `cartogate audit log`

Pretty-prints every entry:

```bash
cartogate audit log
```

Shows timestamp, entry type, actor, and sequence number for each decision. Useful for audits, compliance reviews, and debugging.

## Security Properties

**Read-only git anchor (coverage report, not a verdict):** Cartogate never writes to git. When `verify` runs, it queries git history (read-only, via `git log`) and reports coverage. It is deliberately **not** an integrity verdict: a `commit_pass` is stamped at pre-commit time with `git write-tree`, i.e. *before* the commit object exists, so a stamp whose tree is not (yet) in history is a **pending or aborted** commit — never treated as tampering. The anchor surfaces:
- **Bypassed commits:** a recent commit whose tree has no `commit_pass` stamp (a `--no-verify` bypass or a commit made without the gate) — reported, never a failure.
- **Pending stamps:** `commit_pass` entries whose tree isn't in history (an aborted commit) — informational.

**Hash-chain integrity (this is what `verify` fails on):** The blake2b chain catches, for any entry that is not the tail:
- **In-place edits:** changing any field in an entry invalidates its hash.
- **Deletions:** removing an entry breaks the chain for all downstream entries.
- **Reorders / truncation:** changing entry order or cutting the middle breaks the chain links.

**Attribution, not authentication:** Actor identities (git committer, OS user, agent name) are recorded in the ledger but are **asserted, not authenticated**. They reflect what the system observed; a malicious actor could lie about their identity in the git or environment configuration. The ledger records what was asserted; `verify` confirms the ledger itself has not been tampered with.

**Honest security bound:** `verify` catches in-place tampering of any *followed* entry (the hash chain) and reports bypasses/pending stamps (the git coverage report). It does **not** protect against:
- **Tail rewrite:** editing-and-rehashing or truncating the *most-recent* entry is not caught by the chain alone, because nothing downstream references the tail. Mitigation: a removed `commit_pass` tail resurfaces as an unverified commit in the coverage report; a `write_block` tail entry has no such backstop. Stronger tail protection — anchoring the chain head into a `refs/notes/cartogate` git note — is a deferred hardening tier.
- **Full-file rebuild:** anyone with write access to the ledger file can rebuild the whole chain into a consistent false story (out of scope for this local, no-git-write tier).
- Attacks on the machine that hosts the repository.
- Credential compromise: a faked git identity or modified environment variable yields lying — but chain-valid — entries. Identities are asserted, not authenticated.

The ledger is honest: if it says a decision was made, that decision was recorded; if the chain is intact, the entries have not been edited.

## Compliance Mapping

The audit ledger addresses three key compliance regimes:

- **HIPAA §164.312(b)** (Audit controls) — requires covered entities to implement mechanisms to record and examine access to ePHI. Cartogate's ledger documents every code-governance decision, who made it, and when, with tamper-evidence via cryptographic hashing.

- **NIST 800-171 § 3.3.1 & CMMC** (Audit logging) — require systems to produce audit records of security-relevant events. The ledger serves as the event log for duplicate-detection gates and code-change blocks, with coverage reporting on gated commits.

- **EU AI Act Art. 12** (Record-keeping) — requires records of the design process, testing, and operational monitoring of high-risk AI systems. When Cartogate gates code written by AI agents (via `--agent` flag), the ledger becomes the system's record of which decisions were gated, by whom, and why.

Each entry's `decision_hash` provides reproducibility: re-running the gate on the same tree re-derives the same hash, proving the decision was deterministic and not arbitrary.
