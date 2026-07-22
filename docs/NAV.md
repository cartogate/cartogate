# Navigation maps (`cartogate.nav`)

**Deterministic UI navigation for coding agents.** A navigation map is a checked-in JSON file
that declares an app's states, the landmarks that identify each one, the affordances that move
between them, and the flows an agent follows. An agent drives the app through the map instead of
guessing from the DOM — so it reaches a state by construction, and can never claim it reached a
state it didn't.

This is the same idea as the code gate, applied to the browser: **every navigation rests on a
declared, extracted fact, and the map refuses to confirm a state whose landmarks aren't actually
on the page.** No exploration, no heuristics, no "looks about right."

`cartogate.nav` is an optional extra — the gate and the rest of Cartogate work without it.

```bash
pip install 'cartogate[nav]'            # Playwright adapter (default)
pip install 'cartogate[nav-selenium]'   # Selenium adapter (optional second)
# pipx users inject the dep directly:
#   pipx inject cartogate 'playwright>=1.49'   (or 'selenium>=4.27')
```

After installing the `[nav]` extra, fetch a browser once: `playwright install chromium`.

Two properties are load-bearing, exactly as in the code gate:

- **Only declared facts drive.** The Navigator never explores. It follows the map's declared
  transitions and verifies a state only when the state's landmarks are present on the live page.
  A state it cannot verify is reported `LOST`, never silently accepted.
- **Determinism.** The same map against the same app build produces the same path and the same
  evidence, across processes and machines.

---

## The loop: extract → seed → crawl → check

You don't hand-author a map from nothing. Cartogate extracts what it can from your source, you
enrich the draft, a live crawl verifies it, and from then on the map is checkable in CI.

```
  cartogate navmap .            # 1. SEED   — extract routes → a DRAFT map
  (edit the draft)              # 2. ENRICH — add a landmark to each state; author a flow
  cartogate nav crawl --map …   # 3. CRAWL  — verify states live; propose landmarks/affordances
  cartogate nav check --map …   # 4. CHECK  — traverse a flow deterministically (CI-able)
```

### 1. Seed — `cartogate navmap`

Extracts framework route declarations (Next.js `app/`/`pages/`, React Router, Vue Router) into a
DRAFT map. The draft is deliberately incomplete: it carries the states it could prove from source
and refuses to validate until you add the one thing extraction can't know — a **landmark** per
state (a heading, a checked control: the evidence that "you are here").

```bash
cartogate navmap . --out navmap.draft.json
```

Only **extracted** routes appear — a path that traces to a file or a string literal in source.
Computed paths are skipped, never guessed. A `.suggestions.json` sidecar lists `links_to`-derived
transition candidates for you to promote by hand.

### 2. Enrich

Open the draft and give each state at least one landmark, then author a `flow` — the ordered
sequence of states an agent should be able to walk. The draft won't validate until states have
landmarks; `nav check` needs a flow to check.

### 3. Crawl — `cartogate nav crawl`

Drives a real browser to **verify** the states you declared: it visits each one, checks the
landmarks are present, and **proposes** additional landmarks/affordances it observed. Proposals
are quarantined to `<map>.proposed.json` (with a `crawled` provenance tag) — the live map is never
written. Merging a proposal is a human act.

```bash
cartogate nav crawl --map navmap.json --base-url http://localhost:3000
```

### 4. Check — `cartogate nav check`

Traverses a flow deterministically and fails if any state can't be reached or verified. This is
the CI-able gate — it runs on both adapters in Cartogate's own pipeline on every PR.

```bash
cartogate nav check --map navmap.json --flow checkout --base-url http://localhost:3000
```

`cartogate nav capture --map … --state … --out dir/` screenshots a single state and writes a
sealed evidence manifest — the artifact an agent presents as proof it reached a state.

---

## Frontier discovery (`nav crawl --discover`)

Discovery proposes **new** states beyond the declared map by driving the browser through the app's
navigation affordances. It is powerful and therefore tightly bounded — it drives a real browser
against your running app. Five controls make it safe:

1. **Non-GET requests are aborted** in flight (mechanical, context-scoped — survives popups), so
   the app's server never receives a POST/PUT/DELETE/PATCH.
2. **Loopback only** — discovery refuses any non-loopback origin (`localhost`, `127.0.0.0/8`,
   `::1`, `0.0.0.0`), and re-checks the *landed* origin after every navigation, so a redirect off
   your machine (SSO, external auth) is a dead-end, never crawled. **There is no override flag.**
3. **Navigation-semantic clicks only** — links/buttons/radios/tabs/menuitems. Never a form fill,
   never a submit.
4. **Explicit budgets** — states / depth / actions / seconds, and every budget hit is reported.
5. **Quarantined output** — proposals land in `<map>.proposed.json` and a transitions sidecar;
   nothing touches the live map.

```bash
cartogate nav crawl --map navmap.json --discover \
  --base-url http://localhost:3000 \
  --max-states 30 --max-depth 5 --max-actions 200 --max-seconds 120
```

**One caveat discovery cannot enforce:** requests are GET-only, but a *link* whose GET has side
effects (`/logout`, `/items/5/delete`) will still fire. Run discovery against a dev app you're
willing to poke, not production data. (Discovery requires the Playwright adapter; verify+propose
mode works on both.)

---

## Using the map from Python

```python
from pathlib import Path
from cartogate.nav import Navigator, load, PlaywrightDriver

navmap = load(Path("navmap.json"))
nav = Navigator(navmap, PlaywrightDriver("http://localhost:3000"))

nav.goto("checkout")                 # walk the declared path to a state
assert nav.where() == "checkout"     # verified against live landmarks, or LOST
nav.capture("checkout", Path("out")) # sealed screenshot + evidence manifest
```

The curated public API (`from cartogate.nav import …`): `Navigator`, `LOST`, `NavigationError`;
`load`, `parse_navmap`, and the map model (`NavMap`, `State`, `Landmark`, `Affordance`,
`Transition`, `Flow`, `NavMapError`); the driver seam (`Driver`, `Target`, `Wait`, `DriverError`);
and the adapters (`PlaywrightDriver`, `SeleniumDriver`, `FakeDriver`). Importing the package never pulls
in Playwright or Selenium — only instantiating an adapter needs its extra.

### Testing without a browser

`FakeDriver` is an in-memory `Driver` that ships in the package, so you can unit-test your own
navigation maps with no browser at all:

```python
from cartogate.nav import Navigator, FakeDriver

driver = FakeDriver(
    pages={"http://localhost/": {"link:Checkout"}, "http://localhost/checkout": set()},
    wiring={("http://localhost/", "link:Checkout"): "http://localhost/checkout"},
)
nav = Navigator(navmap, driver)
```

---

## Known limits

- **Extraction is source-only.** Routes assembled at runtime, or in a framework Cartogate doesn't
  parse, won't seed — add those states to the draft by hand; the crawl still verifies them.
- **Discovery equivalence is route-based.** Discovered URLs collapse onto the seed map's route
  patterns; two states that differ only by URL fragment (`#v=a` vs `#v=b`) are treated as one.
- **The request guard is HTTP(S) method-scoped.** It aborts non-GET HTTP requests; it does not
  intercept WebSocket traffic, and it cannot stop a GET with server-side side effects (above).
- **Selenium has no accessibility tree.** The Selenium adapter resolves `role+name` targets
  through a documented role→XPath mapping and prefers a declared `css` fallback; it ships to prove
  the driver seam is honest, and Playwright remains the reference adapter.
