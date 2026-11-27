# Security policy

Inspectarr sits next to your download client and your *arr apps and holds their
API keys, so security reports are taken seriously.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.** Report it privately
through GitHub's [private vulnerability reporting](https://github.com/o51r15/inspectarr/security/advisories/new)
(the **Report a vulnerability** button on the Security tab).

Include what you found, how to reproduce it, and which version or image tag you
tested. You'll get an answer within a few days; a fix for a confirmed problem is
released as soon as it is ready, and you'll be credited unless you'd rather not be.

## Supported versions

Fixes go into the latest release (`ghcr.io/o51r15/inspectarr:latest`) and `:dev`.
Older releases are not patched; please update.

## What is in scope

- The web UI and its API: authentication, CSRF, anything that exposes a stored
  secret (API keys, passwords, notification tokens are masked and never sent to
  the browser), or lets a request act without permission.
- The container image and how it runs (non-root user, file permissions).
- Anything that could make Inspectarr delete, keep or release the wrong torrent.

Inspectarr is designed for a trusted home network. These are accepted and
documented rather than fixed: no rate limiting on login, and the connection
**Test** buttons can reach any address you type. Don't expose the web UI to the
internet without a reverse proxy and authentication in front of it.
