# ccweb

A personal terminal client for your Claude Code cloud sessions, the ones at claude.ai/code. It opens on a home page like the web's (a greeting, the sessions that need you, and a new-session box), keeps the session list in the web's order, loads conversations, shows when Claude is working, lets you reply, starts new sessions with the settings the web has, opens sessions on the web, and teleports them into a local checkout.

**Unofficial.** Listing sessions, loading conversations, listing environments and creating sessions use endpoints Anthropic doesn't document. They may not match what's below, and they can change without notice. Replying and teleporting go through the documented `claude` CLI, so they keep working even when the rest doesn't.

## What uses what

| Feature | How | Status |
| --- | --- | --- |
| Session list, status, repo | `GET /v1/sessions` | Undocumented; confirmed working. `ccweb probe` re-checks it |
| Conversation | `GET /v1/sessions/{id}/events` | Undocumented; confirmed working. `ccweb probe` re-checks it |
| Reply | `claude -p --cloud <id> --output-format json` (message on stdin) | Documented |
| Environments | `GET /v1/environment_providers` | Undocumented |
| New session | `POST /v1/sessions`, the request `claude --cloud` itself sends | Undocumented; confirmed working |
| Teleport | `claude --teleport <id>` from your checkout | Documented |
| PRs, branches | `POST /v1/code/github/get-batch-branch-status`, the call claude.ai's own sidebar makes | Undocumented |
| CI checks, files, diff | `gh pr view` and `gh pr diff`, when the `gh` CLI is installed and signed in | Optional |
| Changes and Files panels | `gh api` compare, commits, git trees and contents, when `gh` is installed and signed in | Optional |
| Plan panel | The conversation's `TaskCreate` / `TaskUpdate` calls, replayed | Derived from the events above |
| Working indicator | `status_bucket` and `session_status`; the open session is re-read every few seconds | Undocumented |
| Open on web, copy link | `https://claude.ai/code/<id>` | — |

When reading fails, a banner says why. Sessions you created here or added by link stay in the list, so you can keep replying and teleporting.

## Install

Needs Python 3.11+ and the `claude` CLI signed in with your claude.ai account. `claude auth status` should show `"authMethod": "claude.ai"`.

```bash
uv tool install ./ccweb      # or: pipx install ./ccweb
ccweb
```

## First run: check the endpoints

```bash
ccweb probe
```

This prints each request's HTTP status and the *shape* of the response: field names and types, not your content. A good result looks like `HTTP 200` and `Parsed 12 sessions`, with IDs and statuses listed. Use `--session <id>` to check a specific conversation.

If you see 404, 400, or `Parsed 0`, record what the CLI itself calls:

```bash
pip install mitmproxy
mitmdump -q -s tools/mitm_capture.py
# in another terminal:
HTTPS_PROXY=http://127.0.0.1:8080 \
NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem \
claude --teleport            # open the picker, pick a session, then quit
```

`ccweb-capture.jsonl` then lists each `*.anthropic.com` request with its method, path, headers and response shape. Model traffic is skipped and credentials are redacted. Copy the method and path you need into `~/.config/ccweb/config.toml` (start from `config.example.toml`), then run `ccweb probe` again. If a response nests fields differently than expected, the parsers in `src/ccweb/api.py` are the place to adjust.

## Home page and new sessions

The app opens on the home page: the sessions that need you (recent unread questions first, then work ready for review) and a box to start a new cloud session. Press `Ctrl+N` or click **+ New session** to return to it from a conversation.

The chips around the box are the web's settings. Tab to one and press enter, or click it:

| Chip | Choices |
| --- | --- |
| `☁ environment` | Your cloud environments. Remote Control bridges are left out because they can't host new sessions |
| `</> repo` `⎇ branch` `×` | Any number of repositories, each with its own starting branch. Pick a repo you've used, or type `owner/repo` or a GitHub URL. Branches come from the `gh` CLI when it's installed and signed in (type to search); otherwise type one. A blank branch means the repo's default |
| `+` | Add another repository |
| `Auto` | Permission mode: Auto, Ask permissions, Accept edits, Plan mode |
| `Sonnet 5.5` | Model: the list in your config plus every model your existing sessions use, or type an ID |
| `Medium` | Effort: Default, Low, Medium, High, XHigh, Max |

With nothing saved, the form starts from your most recent session's environment and repositories. After you change something, it remembers your last choices. They live in the local `state.json` and never include the task text.

The sidebar is ordered by last activity, newest first, as on the web. A filled dot means unread (amber when Claude is asking you something, blue otherwise) and a hollow one means seen. A spinner means Claude is working, and an open conversation shows "Claude is working…" under the transcript.

Creating sessions uses the request the `claude` CLI itself sends, and has been confirmed to work against a real account. Like the other undocumented calls, it could change without notice. If the server ever rejects it, its message appears in a notification and your prompt stays in the box.

## Pull requests, CI and diffs

Just above the reply box, as on the web, a strip shows what the session is working on: its environment and repositories (with the branch each started from), the branch Claude pushes to, and the pull requests it opened, or a pushed branch that has no PR yet:

```
☁ Default   </> TandemKit ⎇ dev   </> Trio ⎇ dev
⎇ claude/compassionate-bell-og9bfb
⎇ owner/TandemKit#525  Draft  +323 −29  CI ✓8  approved  Thread-safe bolus progress reporting
```

The header above the conversation stays short: the title, the status, the model, when it was last active, and either the question Claude is waiting on or its last summary.

Each sidebar row has a second line with the same information in brief: `Draft #525 · CI running 5/8 · 12m`, `Merged #16 · 2h`, `2 commits pushed, no PR`, and, when there's no PR, the question you're being asked or the last summary. `CI passed`, `CI running`, `CI failed` and `CI failing` (something has failed and the rest is still going) are coloured green, amber and red. CI for the sidebar comes from one batched `gh` call per refresh, for live PRs only.

State, size, and the review decision come from the same batched endpoint the web sidebar uses. The sidebar uses it too: a session you've already seen shows a PR icon instead of the hollow dot (gray for open or draft, purple for merged, red for closed), and a session with a live PR counts as ready for review on the home page. Unread still wins over the PR icon, as on the web.

That endpoint leaves CI results empty, so check counts come from the `gh` CLI. Open the PR summary from the **PR** button at the top of the Changes panel, or from `Ctrl+P` (see Keys): every check (failures first), the changed files and the whole diff, colored. In that view `o` opens the PR on GitHub, `r` reloads and `Esc` closes. Without `gh` (install it and run `gh auth login`), the strip still shows state and size, and the view says what's missing. The diff is cut at 4,000 lines.

Both are looked up as the sessions refresh (every 30 seconds for the sidebar, every 15 for the open session), and only for your newest 40 sessions. A session that opened PRs in several repositories shows each one.

## Side panels: Plan, Files and Changes

Like the web's right-hand panel. Toggle them with `Ctrl+G` (Changes), `Ctrl+F` (Files) and `Ctrl+L` (Plan), or the `±`, `▤` and `✓` buttons beside the title (the `✓` shows progress, such as `✓ 5/8`). Any two or three can be open at once, stacked as on the web: Plan on top, then Files, then Changes. `⤢` expands the panel to fill the window and back, and `×` closes one. Drag the thin bar between the conversation and the panel to change how wide it is; ccweb remembers the width.

| Panel | Shows | Comes from |
| --- | --- | --- |
| **Changes** | The session's branch against the branch it started from (or its PR's base): a tree of changed files with `+/−` counts, the commits (pick one to see only its files, or **All changes**), and the selected file's diff with line numbers | `gh api …/compare` and `…/commits` |
| **Files** | The repository at the session's branch, with a search box; Enter opens a file beside the tree | `gh api …/git/trees` and `…/contents` |
| **Plan** | Claude's task list: done (struck through), in progress (with what it's doing now), and pending | Rebuilt from the conversation's `TaskCreate` and `TaskUpdate` calls (and the older `TodoWrite`), because the server doesn't expose the list |

Changes and Files need the `gh` CLI, signed in with access to the repository; Plan needs nothing extra. A session that touched several repositories offers each as a comparison: click the branch name at the top of the panel. A session with no pushed branch or PR says so. Like the rest of the conversation, the text in the panels can be selected and copied.

## Session menu

Press `Ctrl+O` on a session, right-click it (in the sidebar, on the home list, or anywhere in its conversation), or use the `⋮` button next to the title. The menu mirrors the web's, with the same shortcut letters while it's open:

| Item | Key | What it does |
| --- | --- | --- |
| Open in › | | claude.ai in the browser, a terminal (`claude --teleport`), or the session's PRs on GitHub |
| Pin / Unpin | `P` | Moves it to a Pinned section at the top of the sidebar (kept on this machine) |
| Mark as read | `U` | Clears the unread dot on claude.ai too. Opening a session in ccweb doesn't mark it read |
| Rename | `R` | Changes the title everywhere |
| Share | | Shows who can see the session. Changing that is done on claude.ai |
| Copy link | `C` | Copies `claude.ai/code/<id>` |
| Move to group › | | Pick a group by number, make a **New group…**, or remove it from its group (kept on this machine) |
| Archive | `A` | Asks first. ccweb can archive but not restore; the web can restore |
| Delete | `D` | Asks first, and can't be undone. For a session you only added by link, it forgets it locally instead |

Rename, mark read and archive are the calls the `claude` CLI itself makes, and they change the session on claude.ai. Delete uses the verb the server lists for sessions. Pin and Move to group don't touch claude.ai at all; see below.

## Groups and pins

The sidebar groups sessions the way the web's does: a **Pinned** section on top, then each group (`▾ Name  3`), then **Ungrouped**. Inside a group, newest activity comes first. Empty groups are hidden, and a search looks inside collapsed ones.

These are **kept on this machine, not shared with the web**. claude.ai stores your groups and pins in a settings document whose endpoint answers "This endpoint does not accept OAuth access tokens", so the Claude Code login ccweb uses can't read or change them. A group made here won't appear on the web, and the web's groups don't appear here.

- Make or fill a group: `Ctrl+O` on a session, then **Move to group** (or **New group…**).
- On a group header: `Enter` collapses it, and right-click or `Ctrl+O` opens its menu (Collapse, Rename group, Delete group). Deleting a group only returns its sessions to Ungrouped.
- **Show groups or one flat list** (in `Ctrl+P`) switches between the grouped view and one flat list.
- Stored in the state file as group names and session IDs, nothing else. Deleting a session also drops it from any group and from the pins.

## Links, selecting and copying

Text that looks like a link is clickable: web URLs, `owner/repo#123`, and a bare `#123` when the session makes clear which repository it means (its PR's, or its only repository). Repository names, branches, the session ID and the PR bar are links too. A click opens it. That works in any terminal. Terminals that handle hyperlinks themselves, such as iTerm2, also open them on Cmd-click, because the links are real terminal hyperlinks as well. Only `http`, `https` and `mailto` links are ever opened.

Drag to select text anywhere in a conversation, the PR header or the diff, then press `Ctrl+C` to copy. A click that ends a drag doesn't open a link. Copying uses the terminal's clipboard support and also `pbcopy`, `wl-copy` or `xclip` when present, so it works in Terminal.app too.

## Keys

Every shortcut needs `Ctrl`, so a text box can keep focus: the prompt on the home page and the reply box in a conversation take whatever you type, and nothing in them is mistaken for a command. `F1` shows this list from anywhere.

| Key | Action |
| --- | --- |
| `↑` `↓` | Move between sessions (after `Esc`, which leaves the text box) |
| `Enter` | Open the session and jump to the reply box |
| `Esc` | Leave the text box and go back to the session list |
| `Ctrl+N` | Home page: start a new session |
| `Ctrl+O` | Session menu (rename, pin, group, archive, delete, open in, and the rest); on a group header, the group's menu |
| `Ctrl+G` `Ctrl+F` `Ctrl+L` | Open the Changes, Files and Plan panels (again to close) |
| `Ctrl+T` | Teleport into the local checkout |
| `Ctrl+Y` | Copy link |
| `Ctrl+S` | Search sessions |
| `Ctrl+R` | Refresh now |
| `Ctrl+P` | A searchable list of every action (below) |
| `F1` | Help |
| `Ctrl+Q` | Quit |

There are more actions than ctrl keys that a terminal and a text box leave free (`Ctrl+A`, `E`, `D`, `K`, `U` and `W` edit text, `Ctrl+X`, `C` and `V` cut, copy and paste, `Ctrl+I`, `M`, `H` and `J` arrive as Tab, Enter, Backspace and a newline, and `Ctrl+B` is tmux's prefix). So these have no key of their own; press `Ctrl+P` and type a few letters of the name:

| Action | Type |
| --- | --- |
| Open on claude.ai | `open` |
| Add a session by its claude.ai/code link | `add` |
| Pull request summary and CI checks | `pull` |
| Expand or restore the side panel | `expand` |
| Show or hide archived sessions | `archived` |
| Show groups or one flat list | `groups` |
| Set the local checkout used for teleport | `checkout` |
| Forget a session you added here | `forget` |

The pop-ups, the session menu (letters beside each item) and the PR summary (`o`, `r`, `q`), have no text box, so their keys are plain letters.

From a script:

- `ccweb ls` prints sessions as tab-separated lines.
- `ccweb send <id> "message"` sends a reply. If you leave out the message, it reads stdin.

## Replying

A message you send shows in the conversation straight away, marked `sending…` and then `sent · waiting for the session to take it in`. A cloud session that's idle has to wake up before the server records your message, which can take a while, so without this it can look as though nothing happened. When the server's own copy arrives, it replaces yours. If the send fails, the message is removed again and your text goes back in the box. The first prompt of a new session is shown the same way.

## Credentials and privacy

- ccweb reads your existing Claude Code login each time it needs it: from the macOS Keychain item `Claude Code-credentials`, or from `~/.claude/.credentials.json` on Linux.
- The token stays in memory. It is never written to disk or logs.
- macOS may ask once whether to allow Keychain access.
- When the token has expired, ccweb runs `claude auth status` so the CLI refreshes it, then reads it again. ccweb never runs its own sign-in.
- `CCWEB_ACCESS_TOKEN` overrides the token for testing. Don't put it in shell startup files.
- Conversations are never written to disk. The local state file (`~/.local/share/ccweb/state.json`, owner-only) holds only session IDs you added, checkout paths, your last new-session settings (environment, repositories and branches, mode, model, effort), your sidebar groups and pins (names and session IDs), and how wide you dragged the side panel.
- The greeting reads your first name from the local `~/.claude.json`. Nothing is sent anywhere for it.
- Requests identify themselves honestly as `ccweb/0.1` and don't impersonate the CLI.

## Before you rely on it

Anthropic's terms say Claude.ai sign-in is meant for ordinary use of Claude Code and Anthropic's own apps, and that developers may not collect or intermediate Claude.ai credentials. Using your own login from your own client is at your own account's risk.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

The tests run against a fake sessions server and a stand-in `claude` binary, so they need no network or login.
