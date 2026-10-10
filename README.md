# ccweb

A personal terminal client for your Claude Code cloud sessions, the ones at claude.ai/code. It opens on a home page like the web's (a greeting, the sessions that need you, and a new-session box), keeps the session list in the web's order, loads conversations, shows when Claude is working, lets you reply, starts new sessions with the settings the web has, opens sessions on the web, and teleports them into a local checkout.

**Unofficial.** Listing sessions, loading conversations, listing environments and creating sessions use endpoints Anthropic doesn't document. They may not match what's below, and they can change without notice. Replying and teleporting go through the documented `claude` CLI, so they keep working even when the rest doesn't.

## What uses what

| Feature | How | Status |
| --- | --- | --- |
| Session list, status, repo | `GET /v1/sessions` | Undocumented; check with `ccweb probe` |
| Conversation | `GET /v1/sessions/{id}/events` | Undocumented; check with `ccweb probe` |
| Reply | `claude -p --cloud <id> --output-format json` (message on stdin) | Documented |
| Environments | `GET /v1/environment_providers` | Undocumented |
| New session | `POST /v1/sessions`, the request `claude --cloud` itself sends | Undocumented, not yet confirmed end to end |
| Teleport | `claude --teleport <id>` from your checkout | Documented |
| PRs, branches | `POST /v1/code/github/get-batch-branch-status`, the call claude.ai's own sidebar makes | Undocumented |
| CI checks, files, diff | `gh pr view` and `gh pr diff`, when the `gh` CLI is installed and signed in | Optional |
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

The app opens on the home page: the sessions that need you (recent unread questions first, then work ready for review) and a box to start a new cloud session. Press `n` or click **+ New session** to return to it from a conversation.

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

**Creating sessions is unconfirmed end to end.** The request is modeled on what the `claude` CLI sends, and the form, payload and error handling are covered by tests. Only a real session can confirm the server accepts it. If it's rejected, the server's message appears in a notification and your prompt stays in the box.

## Pull requests, CI and diffs

Under an open conversation's header, a PR bar lists the pull requests the session opened, and any branch it pushed that has no PR yet:

```
⎇ owner/repo#525  Draft  +323 −29  CI ✓8  approved  Thread-safe bolus progress reporting
```

State, size, and the review decision come from the same batched endpoint the web sidebar uses. The sidebar uses it too: a session you've already seen shows a PR icon instead of the hollow dot (gray for open or draft, purple for merged, red for closed), and a session with a live PR counts as ready for review on the home page. Unread still wins over the PR icon, as on the web.

That endpoint leaves CI results empty, so check counts come from the `gh` CLI. Press `d` for the full view: the PR summary, every check (failures first), the changed files and the whole diff, colored. In that view `o` opens the PR on GitHub, `r` reloads and `Esc` closes. Without `gh` (install it and run `gh auth login`), the bar still shows state and size, and the view says what's missing. The diff is cut at 4,000 lines.

Both are looked up as the sessions refresh (every 30 seconds for the sidebar, every 15 for the open session), and only for your newest 40 sessions. A session that opened PRs in several repositories shows each one.

## Session menu

Press `m` on a session, right-click it (in the sidebar, on the home list, or anywhere in its conversation), or use the `⋮` button next to the title. The menu mirrors the web's, with the same shortcut letters while it's open:

| Item | Key | What it does |
| --- | --- | --- |
| Open in › | | claude.ai in the browser, a terminal (`claude --teleport`), or the session's PRs on GitHub |
| Pin | `P` | Not available (see below) |
| Mark as read | `U` | Clears the unread dot on claude.ai too. Opening a session in ccweb doesn't mark it read |
| Rename | `R` | Changes the title everywhere |
| Share | | Shows who can see the session. Changing that is done on claude.ai |
| Copy link | `C` | Copies `claude.ai/code/<id>` |
| Move to group › | | Not available (see below) |
| Archive | `A` | Asks first. ccweb can archive but not restore; the web can restore |
| Delete | `D` | Asks first, and can't be undone. For a session you only added by link, it forgets it locally instead |

Rename, mark read and archive are the calls the `claude` CLI itself makes. Delete uses the verb the server lists for sessions. **Pin and groups aren't offered** because that state isn't on the session objects or on any route ccweb can reach; they appear to live in account-level settings behind claude.ai's browser check. To add them, someone needs the request the web sends when you pin or move a session.

## Links, selecting and copying

Text that looks like a link is clickable: web URLs, `owner/repo#123`, and a bare `#123` when the session makes clear which repository it means (its PR's, or its only repository). Repository names, branches, the session ID and the PR bar are links too. A click opens it. That works in any terminal. Terminals that handle hyperlinks themselves, such as iTerm2 and WezTerm, also open them on Cmd-click, because the links are real terminal hyperlinks as well. Only `http`, `https` and `mailto` links are ever opened.

Drag to select text anywhere in a conversation, the PR header or the diff, then press `Ctrl+C` to copy. A click that ends a drag doesn't open a link. Copying uses the terminal's clipboard support and also `pbcopy`, `wl-copy` or `xclip` when present, so it works in Terminal.app too.

## Keys

While the prompt box has focus (the home page opens that way), letters are typed into it. Press `Esc` first to use the shortcuts below, or `F1` for help from anywhere.

| Key | Action |
| --- | --- |
| `↑` `↓` | Move between sessions |
| `Enter` | Open the session and jump to the reply box |
| `Esc` | Back to the session list |
| `n` | Home page: start a new session |
| `m` | Session menu (rename, archive, delete, and the rest) |
| `i` | Add a session by its claude.ai/code link |
| `o` | Open on claude.ai |
| `y` | Copy link |
| `d` | Pull request: checks, files and diff |
| `t` | Teleport into the local checkout |
| `c` | Set the local checkout used for teleport |
| `/` | Search |
| `v` | Show or hide archived sessions |
| `r` | Refresh now |
| `x` | Forget a session you added here |
| `?` / `F1` | Help |
| `q` | Quit |

From a script:

- `ccweb ls` prints sessions as tab-separated lines.
- `ccweb send <id> "message"` sends a reply. If you leave out the message, it reads stdin.

## Credentials and privacy

- ccweb reads your existing Claude Code login each time it needs it: from the macOS Keychain item `Claude Code-credentials`, or from `~/.claude/.credentials.json` on Linux.
- The token stays in memory. It is never written to disk or logs.
- macOS may ask once whether to allow Keychain access.
- When the token has expired, ccweb runs `claude auth status` so the CLI refreshes it, then reads it again. ccweb never runs its own sign-in.
- `CCWEB_ACCESS_TOKEN` overrides the token for testing. Don't put it in shell startup files.
- Conversations are never written to disk. The local state file (`~/.local/share/ccweb/state.json`, owner-only) holds only session IDs you added, checkout paths, and your last new-session settings (environment, repositories and branches, mode, model, effort).
- The greeting reads your first name from the local `~/.claude.json`. Nothing is sent anywhere for it.
- Requests identify themselves honestly as `ccweb/0.1` and don't impersonate the CLI.

## Before you rely on it

Anthropic's terms say Claude.ai sign-in is meant for ordinary use of Claude Code and Anthropic's own apps, and that developers may not collect or intermediate Claude.ai credentials. Using your own login from your own client is at your own account's risk. Keep it personal: don't publish it as a tool for others or put it in an organization's public repos.

## Develop

```bash
pip install -e ".[dev]"
pytest
```

The tests run against a fake sessions server and a stand-in `claude` binary, so they need no network or login.
