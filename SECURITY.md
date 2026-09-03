# Security policy

## Reporting a vulnerability

Report it **privately**, not in a public issue:

- **Email the maintainer: `heresay@bigpoetry.net`.** This is the address for the
  **engine**, and it stays the right one whichever house you found the bug on.
- **Once the repository is published:** a GitHub security advisory on the
  repository.

⚠ **Do not report an engine bug with an in-world command.** `.mail <owner>` reaches
the owner of *that one house* — a person who very likely did not write the code and
cannot fix it, and who is not the maintainer of anything but their own instance. It
is also no use to you if you have no account, or if the bug is what stops you
logging in. Email is the channel; the in-world route is for telling an operator
about their own house.

Please give us a chance to fix it before disclosing.

There is no published response SLA — this is a small project, not a vendor. What
you can expect is that a report is read, answered, and either fixed or explained.

## What is most worth your attention

The covenant means there is very little stored data to leak: room conversation is
never written to disk at all, so there is no transcript to steal. What *is* kept
is small and deliberate, and that is where the value is:

- **`mail.key`** — the Fernet key for mail and `.revtell` buffers. Anything that
  discloses it, or that gets the ciphertext and the key into the same place
  (a backup, a log, an error path), is the highest-value finding here.
- **`accounts.json`** — argon2id password hashes and the one-time recovery-code
  hashes. Anything that reads it out, weakens the hashing, or lets a password be
  set or reset by someone who should not be able to.
- **The owner-account reservation** — the account named by `[moderation] owner`
  holds the top rank at login. Anything that lets it be registered from a network
  door, taken over, renamed, deleted, or reset by a lower rank is privilege
  escalation to the whole house.

Also very welcome: anything that writes **message content** anywhere it should not
go. "The engine never records what was said" is the project's central promise, and
a path that breaks it is a security bug here even where it would be a privacy
footnote elsewhere.

## Scope

This policy covers the engine in this repository. A running house is the operator's:
if you find a misconfigured *instance*, tell that operator.

Out of scope: findings that require an attacker to already have root on the host,
or the operator's own key material. At-rest encryption is honest about this — it
protects backups, casual file reads and a stolen disk, and it is explicitly **not**
end-to-end. Whoever runs the server can read what the server holds, and the
agreement residents consent to says so.

## Supported versions

The latest release on the default branch. This is a single-maintainer project;
there is no long-term-support branch and no backporting.
