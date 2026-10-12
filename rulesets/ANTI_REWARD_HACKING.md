# Preventing Reward Hacking — read before authoring any task

Every claim here was measured on a live container in this repo, not reasoned about. Where
a control is listed, the probe that proves it is listed with it. **Verify by execution.**

A task that can be reward-hacked does not produce a wrong number — it produces a
*plausible* number. Nothing in `reward.json` distinguishes a model that did the work from
one that wrote the answer into SQLite. That is why this is checked at authoring time and
re-checked before every campaign.

---

## 0. The rule that does the most work

> **Design the answer so it is DERIVED, never STORED.**

If the answer exists in no record, full database access still does not hand it over — the
model must do the reasoning anyway. This is free, it is a *design* choice rather than a
control, and it is the only defence that cannot be bypassed.

Worked example, `tasks/nexus-screen-refresh`: the answer is `CO`, obtained as
`{states at/over threshold} − {states registered}`. Neither set is enumerated anywhere;
the difference is written in no table. A model with the whole database still has to
compute it.

**Anti-example:** a task whose answer is a row you can `SELECT`. Those need every control
below and are still one missed surface from being void.

Before writing the instruction, ask: *if the model could read every table, would it know
the answer?* If yes, change the question.

---

## 1. Threat model — what the model can actually do

The agent (`harbor` + `computer_1`) gets: `screenshot`, `click`, `key`, `type`, `scroll`,
`wait`. There is **no `bash` tool** — it is opt-in (`_SUPPORTED_EXTRA_TOOLS`) and off, and
the prompt never mentions it.

**That default protects nothing.** Every exploit measured on this stack used only
`click`, `key` and `type`:

| Route | Evidence |
|---|---|
| `key` CTRL+ALT+T → terminal → `type` is shell input, as root | trial `UbMrna9`: terminal at step 200, then 26 commands (curl, python, urllib) |
| `key` F12 → DevTools → `fetch()` loops over the REST API, then `.click()` the finish control | shipped t-004 runs |
| `click` address bar → `type` an API URL → read graded data | trial `Ce5NS8Z` — **scored 1.0; that solve is void** |
| GUI file manager → Open-With → arbitrary command | `thunar` was present in our image until hardened |
| Settings GUI → rebind a keyboard shortcut to an arbitrary command → press it | `xfce4-settings-manager` was live until hardened |

**The surface is the desktop, not the tool list.** No harness choice removes it: driving a
GUI *is* click/key/type. Choosing a different agent moves you sideways at best — the
BrowseComp reference tasks ran `codex exec --dangerously-bypass-approvals-and-sandbox
--enable unified_exec`, i.e. a full shell with the sandbox explicitly disabled.

---

## 2. Defences, ranked by leverage

1. **Answer absent from the environment** (§0) — free, complete, design-time only.
2. **Grade provenance, not just end state.** Require evidence the work came through the
   UI. The Excel app writes `cell_versions` / `workbook_versions` rows that a raw `INSERT`
   does not; apps carry `dbchangelog` audit triggers. Forging end state then fails on its
   own terms. App-specific and the most work — spend it when §0 is impossible.
3. **Air-gap the verifier.** `environment_mode = "shared"` runs the verifier *inside* the
   agent's environment, so the grader and the evaluator credential both exist where the
   agent is. Moving grading outside means there is nothing there to find.
4. **Remove the capability** (§3) — mechanical, checkable, and what the rest of this
   document is about.

---

## 3. Environment lockdown checklist

Bake all of it **into the image**. See §5 for why mounts alone are worse than nothing.

### 3.1 State endpoints
- [ ] `/api/state` **and** `/api/v2/state` require the evaluator credential **at the app
      layer**, not only in nginx. The apps' own middleware deliberately exempts them —
      its source says *"These are public (no auth): /api/state, /api/v2/state"*. Add both
      to `PROTECTED_PATHS`.
- [ ] nginx on agent-facing ports 404s the admin surface, allowing an optional version
      segment so `/api/vN/state` cannot walk around it:
      `^/api/(v[0-9]+/)?(state|state-v2|seed|sql|grade|dbchangelog|schema|docs|openapi|redoc)([./]|$)`
- [ ] Any app with **no** auth middleware is listed explicitly as accepted-open, with the
      reason it cannot carry an answer. Build fails otherwise.

### 3.2 Credentials
- [ ] `auth_keys` are **replaced at boot** with a per-run credential. The repos ship
      base64 of `admin:secret123`, `admin:admin`, `openai:voyager` — public and guessable.
- [ ] `AUTH_KEY` (singular) is **never set**. It is a *global* bypass:
      `if is_valid_auth_key: return await call_next(request)` admits every caller
      regardless of headers.
- [ ] The hub **fails closed**. No credential configured must mean *serve nobody*, not
      "auth disabled for development".

### 3.3 Browser
- [ ] Managed Chromium policy at `/etc/chromium/policies/managed/`:
      `DeveloperToolsAvailability: 2`, `IncognitoModeAvailability: 1`.
- [ ] `URLBlocklist` covers `view-source:*`, `file://*`, `chrome://downloads`, and every
      backend port on `localhost`, `127.0.0.1` **and** `[::1]`.
- [ ] nginx **Sec-Fetch-Dest allowlist** on `/api/`, allowlist not deny-list:
      ```nginx
      map $http_sec_fetch_dest $api_not_xhr { default 1; "" 0; "empty" 0; }
      location /api/ { if ($api_not_xhr) { return 404; } ... }
      ```
      The app's XHR sends `empty`; address-bar navigation sends `document`; curl, the
      healthcheck and the verifier send nothing. Only the first and last pass.

### 3.4 Desktop
- [ ] No terminal emulator, no file manager, no application finder, no settings manager.
      Neuter the **binaries**, not the keybindings — the shortcut, the finder, the
      right-click menu and the Applications menu all resolve to the same executables.
      Minimum set: `xfce4-terminal`, `xfce4-terminal.wrapper`, `xterm`,
      `x-terminal-emulator`, `xfce4-appfinder`, `xfrun4`, `exo-open`, `xdg-open`,
      `thunar`, `xfce4-settings-manager`, `xfce4-keyboard-settings`.
- [ ] `/usr/share/applications/*.desktop` stripped — a launcher menu that offers nothing
      cannot launch anything.
- [ ] Keep `Xvfb`, `xdotool`, `chromium`, `startxfce4`, `xfsettingsd` and **`imagemagick`**
      — computer-1 shells out to `import` for every screenshot; without it every trial dies
      on its first action with `Runtime action 'screenshot' failed (127)`.

### 3.5 Do not bother
Interpreters (`python3`, `node`, `perl`, `curl`, `sqlite3`) **cannot be removed** — the
backends and frontends are written in them. They are only dangerous once something can
launch them, so spend the effort on §3.4, not on chasing binaries.

---

## 4. Verify by execution

Bring up the task image with **no mounts** — that is the floor a lost compose override
leaves you on — and run these. Expected results in the right column.

```sh
# read paths, from a shell inside the container
curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8001/api/state          # 401
curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8001/api/v2/state       # 401
curl -s -o /dev/null -w '%{http_code}' -u admin:admin 127.0.0.1:8001/api/state   # 401

# address bar vs the app's own XHR
curl -H 'Sec-Fetch-Dest: document' -o /dev/null -w '%{http_code}' 127.0.0.1:5001/api/<any>  # 404
curl -H 'Sec-Fetch-Dest: empty'    -o /dev/null -w '%{http_code}' 127.0.0.1:5001/api/<any>  # 200

# hub
curl -s -o /dev/null -w '%{http_code}' 127.0.0.1/api/state               # 401
curl -s -o /dev/null -w '%{http_code}' -u <cred> 127.0.0.1/api/state     # 200

# desktop: every launcher absent or a 2-line stub
for b in thunar xdg-open exo-open xfce4-terminal xterm xfce4-appfinder \
         xfrun4 xfce4-settings-manager; do command -v $b; done
find /usr/share/applications -name '*.desktop' | wc -l                   # 0

# browser policy present and parsed
python3 -c "import json;d=json.load(open('/etc/chromium/policies/managed/lockdown.json'));\
print(d['DeveloperToolsAvailability'], len(d['URLBlocklist']))"          # 2 <n>

# the apps must still work
for p in 5001 5002 5003; do curl -sf 127.0.0.1:$p/api/health >/dev/null && echo ok; done
```

Then the task's own gates: **seed-zero 0.0**, **golden solution 1.0**, and the full
discrimination suite. A lockdown that breaks the app is not a lockdown.

### Still untested on this stack
`javascript:` URIs and bookmarklets, `chrome://` pages beyond the blocklist, blocklist
evasion by alternate loopback spelling (`0.0.0.0`, `127.1`, `[::ffff:127.0.0.1]`), GTK
file-chooser dialogs used as a filesystem browser, and abuse of the apps' own
import/formula/filter features. **Do not claim completeness while these are open.**

---

## 5. Anti-patterns, with evidence

**Mount-only controls.** Every control arriving as a docker-compose bind mount means that
if harbor ignores the override, uses a different service name, or the target path is
wrong, the container comes up with no policy, no nginx guard and live terminals — and
*nothing in the run says so*. The scores still look real. **A control that can silently
vanish is worse than no control.** Bake into the image; keep mounts as belt-and-braces.

**nginx-only deny.** Denying `/api/state` on the agent ports does nothing about the
backends on `:8001+`. Measured before hardening: **HTTP 200, 27 MB, every table**.

**Trusting `operating-notes.md`.** "Don't reach around the UI with internal HTTP/API
calls, database edits, or backend shell commands" is an *instruction*. The point of a
benchmark is that it holds against a model that ignores it.

**End-state-only grading with a stored answer.** These compose badly: the grader cannot
tell real work from a forged row, and the answer is sitting there to be forged with.

**Claiming completeness.** Every time this stack was declared closed, another surface
turned up. State what is verified and what is untested.

---

## 6. Pre-launch gate

Do not start a campaign until all of these hold:

- [ ] §0 applied, or §2.2 provenance grading in place
- [ ] every §3 box ticked, **baked into the image**
- [ ] §4 probes run against a mount-free container, all expected
- [ ] seed-zero 0.0 and golden 1.0 **on the image that will actually run**
- [ ] one smoke trial completed end to end — agent acted, verifier wrote a numeric reward
- [ ] untested surfaces (§4) listed in the task's `rubric.md` as known gaps

---

## 7. Coding tasks — what the executed controls prove

The coding pipeline (`arena-envs-codingtasks-large-dump`) keeps §0: the answer is
DERIVED by running code, never stored. There is no expected value anywhere in
the package — `tests/checks.json` names which hidden tests decide each check
and `tests/grade.py` runs them. What the agent can reach and what it cannot:

| surface | agent | verifier | proof |
|---|---|---|---|
| `/app/repo` (the fixture) | read/write | re-uploaded, copied fresh per check | `nop` 0, `destructive` 0 |
| `/tests` (suites, hidden inputs, grade.py) | absent | built from `tests/` alone | `hidden.disjoint` gate: no hidden input exists in the repo |
| `/opt/deps` (pinned deps) | read-only | relinked by grade.py | `chmod -R a-w`; a `.pth` planted by the agent is inert under `python -I` |
| `conftest.py`, `pytest.ini`, `sitecustomize` in the repo | writable | never on the path (`--rootdir /tests/suites --confcutdir`, no PYTHONPATH) | `test_tamper` on the UNTOUCHED fixture stays 0 with no objective passing |
| an `os._exit(0)` / `process.exit(0)` in a module the tests import | writable | the runner's own report of completed tests decides, never its exit status | `early_exit` plants it into every source file of the untouched fixture; every objective stays failed |
| a forged test report written by imported code before exiting | writable (same process) | the report path and a per-run nonce reach the runner over stdin, never argv or the environment; the report must echo the nonce and cover every selected test | `early_exit` also overwrites every report it can find with a passing suite before exiting; the grader must not count it |
| network | none | none | Harbor egress allowlist; verifier offline |

Rules that follow from the table:

- **Tests import the repository through `ARENA_REPO`**, never a hard-coded
  path, and never import anything the agent could shadow (`sys.path` is the
  suites directory and the interpreter's own).
- **Hidden inputs live under `tests/suites/data/`.** A visible sample in the
  repository is the repro the user saw; the hidden input sits where the visible
  case and the real rule diverge. `hidden.disjoint` refuses a hidden input that
  also exists in the fixture.
- **Guardrails are structural and cheap**: `hash-guard` (bytes of a file the
  user said to leave alone), `tree-guard` (no new files outside an allowlist),
  `size-guard` (a file may not be emptied). A violated guardrail zeroes the
  step. `nop` proves every guardrail passes on the untouched fixture.
- **Every mutant is a plausible wrong fix, executed**: it must fail exactly the
  checks its trap declares, pass at least one objective, and never score 1.0.
  A test no mutant fails while the reference passes it has no executed
  counterfactual and cannot carry an all-fail result (`ALL_FAIL_PROOF_OBLIGATIONS`).
- **The trajectory scan reads every episode**: test access (`/tests`,
  `/app/tests`, checks.json, grade.py, rubric.json, reward files), graded
  writes, symlinks into graded directories, harness tampering (killing the
  verifier, `.pth`/`sitecustomize`, conftest skips, `trap ... EXIT`), and
  undeclared output. The agent's working directory is `/app/repo`, so a
  relative `test.sh` is the repository's own; after a subshell the directory is
  unknown and the command is read as written. Every hit blocks until a human
  adjudicates it.
- **Residual, declared**: code running inside the test process can in principle
  recover the report path and nonce from pytest's own configuration objects
  (`gc`/`sys.modules` introspection) and forge a covering report. No in-process
  measurement can rule that out; the backstops are the trajectory scan, which
  flags any source edit that adds `os._exit(`, `junit`, `arena-report`,
  `sitecustomize` or pytest hook names the fixture did not carry, and human
  adjudication of every hit before release.
- **Verify by execution**: `./arena sandbox <bundle>` runs the whole control
  suite in Docker without a model; `pipeline/tests/native_smoke.py` runs
  oracle and no-op through the real Harbor harness.
