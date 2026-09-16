# WorkBuddy Switch

> One-click account switching and automatic local data sync for the Tencent **WorkBuddy AI** desktop client.
>
> Inspired by [`pjpv/zcode-switch`](https://github.com/pjpv/zcode-switch) (a ZCode multi-account switcher).
> Sync logic references [`xiaoliuzhuan666/workbuddy-account-migrate`](https://github.com/xiaoliuzhuan666/workbuddy-account-migrate).

---

## What it does

WorkBuddy AI isolates data by `user_id`. After you log in with a different account, the previous account's **sessions, long-term memory, connector config and automations** all seem to vanish from the UI — the data is still on disk, just isolated under a different `uid`.

WorkBuddy Switch solves two things:

| Feature | Description |
| --- | --- |
| **One-click switching** | Kill the client → write the target identity snapshot → restore that account's private data → sync local archives → relaunch |
| **Automatic local sync** | Merge 6 categories of data (Sessions / Memory / Connectors / Automations / private storage / identity) from an old account into the current one, with full backup and one-click rollback |

**Zero third-party dependencies** — pure Python standard library (tkinter for the GUI). Nothing to `pip install`.

---

## Quick start

### Requirements

- Windows 10/11
- Python 3.9+
- A Python build that includes **tkinter** (the GUI). Official python.org installers have it; some minimal / Store builds don't — see FAQ

### Run

```bat
:: Launch the GUI
run.bat

:: or
python -m wbswitch.cli gui
```

### CLI

```bat
:: Status and diagnostics
python -m wbswitch.cli state
python -m wbswitch.cli diagnose

:: Save the currently logged-in account into the library
python -m wbswitch.cli capture --name "main"

:: List all accounts
python -m wbswitch.cli list

:: Switch to an account (id prefix / name / uid prefix all work)
python -m wbswitch.cli switch main

:: The reverse direction: push an old account's data into whoever is
:: currently logged in (equivalent to the migrate.py interactive flow)
python -m wbswitch.cli sync old-account

:: Backup / rollback
python -m wbswitch.cli backups
python -m wbswitch.cli backup --label "before switch"
python -m wbswitch.cli rollback 20260916-213000

:: Process control
python -m wbswitch.cli kill
python -m wbswitch.cli launch
```

Add `--lang en` (or `--lang zh`) to any command.

---

## Screenshots

`docs/screenshot.png` — the main window: status dot + toolbar on top, account cards in the middle (the active one gets a highlighted border and an "in use" badge), a run log at the bottom.

Each card has four actions on the right: **Switch** / **Sync** / **Details** / **Rename** / **Delete**.

---

## How it works

### WorkBuddy's data isolation model

First, where does everything live? The single authoritative source of account identity is:

```
~/.workbuddy-ai/storage/skeleton/account-snapshot.json   ->  primary.uid
```

Around that `uid`, data is spread across **6 locations**:

| # | Location | Contents |
| --- | --- | --- |
| 1 | `storage/skeleton/account-snapshot.json` | Identity snapshot (uid / nickname / edition / Pro flag) |
| 2 | `storage/user-{uid}-*/` | Per-account private storage |
| 3 | `sessions` table (SQLite) | Conversation records, isolated by `user_id` |
| 4 | `automations` table | Automation tasks, isolated by `owner_user_id` |
| 5 | `memory/{uid}_memory.md` | Long-term memory file |
| 6 | `connectors/{uid}/` | Connector / MCP config |

The `diagnose` subcommand runs this table for real:

```
[logged in] b2c3d4e5-2222-3333-4444-555566667777
[integrity] ok

uid                                      Sessions   Autos  flags
----------------------------------------------------------------------------------------
b2c3d4e5-2222-3333-4444-555566667777            7       0  CURRENT registered
c3d4e5f6-3333-4444-5555-666677778888           23       2  registered
```

### The sync engine: three traps we hit

`workbuddy-account-migrate` is a solid starting point, but copying it verbatim breaks things. All three traps are handled in `wbswitch/engine.py`:

**Trap 1 — inferring the current account from "the newest session's user_id" is wrong**

The old account's last session can be updated *later* than the new account's, so you infer the wrong target uid and sync data into thin air.

→ This tool **trusts only `primary.uid` in `account-snapshot.json`**, cross-checked against the database. After writing the identity snapshot it also **reads it back to verify** (`_write_identity_verified()`, with retries).

**Trap 2 — naive memory merging corrupts the file**

`memory/{uid}_memory.md` contains structured `RAW_JSON` metadata alongside the actual prose. A naive "dedupe by line and append" drags fragments like `"uid": "..."` and `> Last updated:` into the memory body — it gets dirtier with every migration.

→ This tool parses the `RAW_JSON_START` / `RAW_JSON_END` block first, merges by `memoryBlock` body, then **re-renders the whole file** (with the RAW_JSON rewritten to the correct uid).

**Trap 3 — key material must not be copied**

`connectors/{uid}/.master.key` and the `encryption` / `accountIdentityKey` fields inside `connector-states.json` are **uid-bound key material**. Copying them over breaks the target account's own decryption — every connector stops working.

→ `_strip_forbidden()` + `_CONNECTOR_FORBIDDEN_FILES` strip those keys and files before merging.

On top of that, a `PRAGMA wal_checkpoint(TRUNCATE)` runs both before and after migration — if SQLite's WAL journal isn't flushed, the client can lose data on restart.

### Safety model: three layers

```
(1) Account library   ~/.workbuddy-switch/accounts/{id}.json
      - one record + one private-data snapshot per account
      - id allowlist [A-Za-z0-9-] to block path traversal
      - atomic writes (temp + os.replace)

(2) Full backup       ~/.workbuddy-switch/backups/{tag}/
      - taken automatically before every switch / sync
      - one-click rollback (which itself backs up first)

(3) Dry run
      - toggle in settings; reports what *would* change without writing
```

One hard rule: **never lose an account**. If the currently logged-in account isn't in the library yet, it's captured automatically before any switch (`auto_preserve_current()`).

---

## An important limitation

**WorkBuddy's login session is held by the client itself** (Tencent Cloud OneID), stored internally. No third-party tool can forge or inject those credentials.

So "one-click switching" actually means:

```
kill the client  ->  data in place (target account's sessions/memory/connectors restored)
                 ->  identity snapshot written and verified
                 ->  relaunch the client
```

If the client asks you to **log in again** because a token expired, all your data is already in place once you do — that is the ceiling of what's architecturally possible here, not an implementation gap.

`zcode-switch` can do a pure credential-file swap because ZCode keeps them in plaintext at `~/.zcode/v2/credentials.json`. WorkBuddy does not.

---

## Project layout

```
workbuddy-switch/
├─ wbswitch/
│  ├─ paths.py       Path detection (data root / library / backups / registry lookup)
│  ├─ i18n.py        Chinese + English strings
│  ├─ config.py      Settings dataclass backed by settings.json
│  ├─ profiles.py    Account library (CRUD + private-data snapshots)
│  ├─ engine.py      Sync engine (sessions/memory/connectors/automations + backup/rollback)
│  ├─ client.py      WorkBuddy process control (tasklist / taskkill / launch)
│  ├─ switcher.py    Switch orchestration + state aggregation + history log
│  ├─ cli.py         CLI (16 subcommands)
│  └─ gui.py         tkinter desktop UI (dark theme, zero deps)
├─ tests/selftest.py Sandboxed end-to-end self-test (43 assertions)
├─ tools/render_shot.py  GUI screenshot generator
├─ run.bat
└─ docs/
```

### Environment variables (used for sandboxed testing)

| Variable | Effect |
| --- | --- |
| `WBSWITCH_WORKBUDDY_HOME` | Override the WorkBuddy data root |
| `WBSWITCH_STORE` | Override the library / backup directory |
| `WBSWITCH_SANDBOX=1` | Disable all process operations (kill / launch become no-ops) |
| `WBSWITCH_LANG` | `zh` / `en` |

### Running the self-test

```bat
python tests\selftest.py
```

It copies the necessary parts of the real data into a temp directory as a sandbox, then runs 9 groups / 43 assertions: sandbox setup → capture → sync (memory free of metadata junk, RAW_JSON uid rewritten, deep merge doesn't overwrite, `.master.key` not copied, integrity ok) → idempotency → dry run → rollback → switch (process ops disabled in sandbox). **Real data is never touched.**

```
43 passed / 0 failed
```

---

## Relationship to upstream projects

| | zcode-switch | workbuddy-account-migrate | **WorkBuddy Switch** |
| --- | --- | --- | --- |
| Target | ZCode | WorkBuddy | WorkBuddy |
| Form | Tauri 2 desktop app | Python script | Python desktop app + CLI |
| Switching | Credential file swap | No | Yes — data + identity + relaunch |
| Sync | No | One-shot interactive migration | Repeatable, rollback-able |
| Backup / rollback | Partial | Basic backup | Full + one-click rollback |
| Dependencies | Rust + Node | Stdlib | **Stdlib only** |

This is an independent implementation. It borrows zcode-switch's interaction design and account-migrate's data model, but the sync engine is rewritten and the three traps above are fixed.

---

## FAQ

**Q: After switching, the client asks me to log in again.**

Expected. See "An important limitation" above — the login session belongs to the client. Your data is ready once you log in.

**Q: `No module named 'tkinter'`**

Your Python build lacks tkinter (common with Microsoft Store or minimal builds). Install the official python.org build and tick `tcl/tk and IDLE`. The CLI is unaffected — use `python -m wbswitch.cli`.

**Q: Can this corrupt my data?**

Every switch / sync takes a full backup first, visible via `backups`, restorable via `rollback` (which also backs up first). Run `diagnose` to confirm path detection before your first switch.

**Q: The client path isn't detected.**

Set it manually in Settings. Detection order: configured value → running process → registry uninstall entries / App Paths → common install paths.

**Q: macOS / Linux support?**

The data layer is cross-platform (`Path.home()`), but process control (`tasklist` / `taskkill`) and registry probing are Windows-only. On macOS you'd need to add an adapter in `client.py`.

---

## License

MIT
