# Changelog

KBC Artifact Hub is one web address where you publish a document and
collaborate on it with your team, secured by the Keboola account you already
have.

## 0.9.0 — Security review follow-up (2026-09-01)

**If you run a webhook receiver, read the first item — it needs a change on
your side.**

- **Each webhook receiver is now signed with its own key.** Previously every
  receiver verified deliveries with one key shared across the whole service,
  so anyone running a single webhook could forge a delivery that looked
  genuine for any other receiver and any document. Keys are now bound to the
  document and the receiver URL. **Existing receivers will stop verifying:**
  read your new key from `GET /api/artifacts/{id}/webhooks` (owner only) and
  configure the receiver with it.
- Verifying a password now refuses a stored record that asks for an
  implausible amount of work, so a corrupted or tampered record can neither
  tie up the service nor quietly weaken the check.
- A comment thread's "resolved" mark and a guest invitation's "revoked" mark
  are now read strictly. A malformed record can no longer close a thread that
  is open, and an invitation whose revocation cannot be read is treated as
  revoked rather than still valid.
- Comparing two versions now requires the older one first. Asking for 5..3
  used to answer with additions and removals swapped under labels claiming
  the opposite; it is a clear error now.
- Pages no longer pass their address on to anywhere they link or load from,
  so a document's link cannot leak through a font request or an outbound
  click.
- The API reference now describes what the review page really does for a
  password-protected document — it serves the page and asks for the password
  in place, rather than the redirect it used to promise.
- Documentation now says plainly that pending proposals are subject to their
  own retention cap, instead of claiming they are kept forever.
- The deployed image no longer ships a networking library with five known
  vulnerabilities. Every release now fails automatically if a dependency with
  a published advisory reaches the production dependency list.
- A push notification is no longer sent to a destination the service could not
  check. A name that fails to resolve at delivery time now stops the delivery
  instead of being sent anyway, closing a way to redirect a notification to an
  internal address.
- Push notifications now carry an event id and a delivery id, so a receiver can
  tell a retry from something new and avoid acting twice.
- A receiver that stops answering can no longer make the service accumulate
  pending notifications without limit; past a configurable ceiling
  (`HUB_WEBHOOK_QUEUE_MAX`) the newest is dropped, and publishing is never held
  up waiting for one.
- Two requests changing the same document at the same moment can no longer
  act on a state that has just stopped being true. Every change to a document
  now waits for the previous one to finish: two promotions of one proposal
  fire once, two deletions cannot remove the last live version between them,
  a document cannot be finalised underneath a submission already in flight,
  and nothing can land in a document while it is being erased.
- A deleted version's number is retired with it. Deleting the newest version
  used to hand its number to whatever was submitted next, so every link,
  comment and comparison that named it silently pointed at different content
  — including after a restart.
- The service now states its deployment shape plainly and watches for it
  being broken: one hub is one container, serving one organisation. Its index,
  locks and state snapshots are built for exactly one writer, so if a second
  instance ever writes state, the log says so in as many words instead of the
  two instances quietly overwriting each other.
- Authorization is documented as it was always meant: any token from the
  owning Keboola project has full owner authority, because the project is the
  team. The security review had flagged this as a gap; it is the design.
- Finalising a document now freezes its discussion too. Resolving, reopening
  and withdrawing a comment used to keep working on a document marked final or
  moved to the trash, so a "finished" record kept changing. The owner can still
  delete a comment thread — a comment that has to come off a finished document
  must stay removable.
- A comment thread now has a size ceiling of its own: 500 replies and 2 MB.
  Individual comments were capped, but nothing capped the thread they pile up
  in, and a thread is rewritten whole on every reply and re-read on every
  listing.
- A comment, a resolve or a guest-invitation revocation that is refused no
  longer shows up as if it had been accepted. A revocation that failed to save
  used to read as revoked on the server that handled it while the guest kept
  working everywhere else, and came back entirely after a restart.
- Publishing from git now says plainly that `git_ref` takes a branch or a tag.
  A commit id was documented as accepted but always failed inside git; it is
  now refused with a message that says to tag the commit instead.
- Permanently erasing a document can now actually be retried when it fails
  partway. It previously removed the document before its comments, so a
  failure left comments behind that nothing could reach or erase, and the
  retry the error asked for reported the document as already gone. Comments
  go first now, and the document stays until everything else is confirmed
  erased.

## 0.8.0 — Documents that update themselves (2026-09-01)

- **A document open in your browser now updates itself when a new version
  lands.** No more reloading to find out whether something changed — the
  document page, the review page and the admin studio all notice on their
  own, within about ten seconds.
- Nothing is ever pulled out from under you. The page swaps in a new version
  only when you are still at the top and have nothing typed; otherwise it
  offers you a banner and lets you decide. A specific version you opened on
  purpose never changes under you, and a half-written comment is never lost.
- Watching pauses while the tab is in the background, and stops entirely when
  a link has been rotated away.

## 0.7.5 — The review page opens for a locked document (2026-09-01)

- A password-protected document sent to the review page now actually opens
  it, showing its own unlock panel. Previously the review page redirected to
  the standalone password form, which navigated away — and for an invited
  guest that dropped the invitation out of the address bar, leaving them with
  no way back in.
- The page served while locked carries no document content and no credential;
  everything it shows is still fetched through the gated endpoints.

## 0.7.4 — Unlock a protected document from the review page (2026-09-01)

- An invited guest reaching a password-protected document can now supply the
  password on the review page itself. Before this, the document would not
  load and a typed comment failed at submit with a bare "password required".
- A comment refused because of the password is replayed after unlocking, so
  what you typed is not lost. A wrong password and a rate-limited attempt
  each get their own message.
- The password is held in the tab only and sent on writes; it never goes into
  a URL, browser storage or a log.

## 0.7.3 — A live demo, and a clear signal when a document is frozen (2026-09-01)

- Public responses now say whether the *document* is accepting new versions,
  not just what state a given version is in. A machine consumer could
  previously not tell that a document had been marked final.
- The landing page links a published showcase document when one is
  configured.

## 0.7.2 — Comment anchoring and guest credit (2026-09-01)

- An inline comment whose quote spans a line break now anchors correctly.
  Quotes sent through the API — the route an AI assistant takes — used to
  have to reproduce the source's own newlines and indentation exactly, and
  otherwise rendered as "quote not found on this version".
- The Obsidian vault export now credits a guest commenter as "Name (guest)"
  instead of "unknown project".

## 0.7.1 — Security follow-up (2026-09-01)

- Documents opened through the machine-readable link now render inside the
  same isolated sandbox as the normal view, closing a cross-site scripting
  risk that only affected that one path.
- Password-protected documents now stay protected for invited guests too —
  an invitation is a way to comment, not a way around the password.
- Rotating a document's link now fully cuts off the old one for comments as
  well as viewing, so revoking a leaked link revokes it everywhere.
- Stronger protection against push notifications being redirected to
  internal systems.
- Notification signing and document-password protection now use separate
  keys, instead of sharing one.
- Clearer guidance for AI assistants working with this service: document
  content, comments, and proposals are data to read, never commands to
  follow.

## 0.7.0 — Control, guests, and push notifications (2026-09-01)

- **Revoke a shared link in one click.** Mint a new link for a document and
  the old one stops working immediately, for everyone — the way to take back
  access after a link went somewhere it shouldn't have.
- **Deleted documents go to a recycle bin first.** Deleting a document no
  longer erases it on the spot: it moves to the trash, where it stays fully
  recoverable — bring it back, exactly as it was, on the same link, whenever
  you like. A separate, clearly-labeled "erase forever" action is there for
  when you really do mean permanent.
- **Get a Slack message the moment a colleague proposes a change or
  comments.** Connect a document to a Slack channel (or your own system) and
  stop refreshing the page to see what happened — a notification arrives the
  instant a version is proposed or promoted, a comment or reply lands, or the
  document is finalized, trashed, restored, or its link is rotated.
- **Invite reviewers by name — no Keboola account needed.** Send a private
  link to a colleague, client, or outside reviewer and they can comment right
  away, without signing up for anything. Revoke any one person's access at
  any time without touching anybody else's.
- **See whether your document is actually being read.** Every document now
  reports how many times it's been opened, broken down by day and by how
  people viewed it — the rendered page, the raw file, or a specific version.
- **Compare two versions the way a reader actually sees them.** A new
  side-by-side view renders both versions as real pages, scrolling in sync —
  ideal for reports and dashboards where the visual result matters as much as
  the text.
- **A proposed change now tells you when it's out of date.** If a colleague
  suggests a change and somebody else's edit lands first, the suggestion is
  now flagged so you know to take a fresh look before approving it.

## 0.6.0 — Security hardening

- An independent security review examined every part of the service; all
  forty findings it raised have been fixed.
- Documents now render inside an isolated sandbox, so a shared document can
  never reach the part of the page that holds your Keboola sign-in — even a
  document designed to try.

## 0.5.0 — Changelog and clearer guidance (2026-09-01)

- This page. A plain-English summary of what changed, so you don't have to
  read engineering release notes to know what's new.
- The step-by-step instructions we hand to AI assistants and technical teams
  now walk through full team workflows, not just individual actions — faster
  setup, fewer questions bounced back to you.

## 0.4.0 — Team collaboration ("project brain")

- Colleagues can highlight any passage in a document and leave a comment
  right there, with replies — a document becomes an ongoing discussion
  instead of a one-way memo.
- A dedicated review page lets anyone open the document in their browser,
  highlight text, and comment on it — no special software or training
  required.
- Download the complete history of a document — every version, who proposed
  it, every comment, and how each was resolved — as a self-contained,
  permanent record you can browse and search later.
- Decide exactly who is allowed to contribute changes or comments, from
  "anyone with the link" to a specific named list of collaborators.
- Mark a document "Final" once your team reaches agreement, locking it
  against further changes.

## 0.3.0 — Easier moderation and onboarding

- A new Admin page lets a document's owner approve or decline a colleague's
  suggested change with a single click — no technical steps required.
- New team members, and their AI assistants, can install a ready-made helper
  that already knows how to use the service, cutting setup time to minutes.
- The full API reference is now complete and kept accurate automatically,
  so any engineering team you bring in can integrate faster.

## 0.2.1 — Reliability fix

- Fixed an issue where links opened from behind the company network could
  point to an internal address instead of the public one. Links now always
  resolve correctly, wherever they're opened from.

## 0.2.0 — Document versions

- Every update to a document is saved as a new version, so nothing is ever
  lost — the full history is always available.
- Compare any two versions side by side to see exactly what changed.
- Colleagues can propose changes to a document; the owner reviews and
  approves them before they go live.
- Choose which version your readers see — always the newest one, or a
  specific version you've locked in.
- Publish documents directly from your team's private code repositories,
  fitting into the workflow you already use.

## 0.1.0 — Launch

- Publish any document — a web page, a formatted write-up, or content
  straight from a code repository — and get a unique, hard-to-guess public
  link to share.
- Add an optional password so only the people you've shared the link with
  can open the document.
- Your content is stored securely in your own Keboola account — you keep
  full ownership and control of it.
- Documents support rich formatting out of the box: diagrams, tables, and
  charts, with no extra setup.

---

Technical release notes: github.com/padak/kbc_ai_artifact/releases
