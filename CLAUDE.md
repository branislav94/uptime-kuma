# CLAUDE.md

Monitoring stack for the ONET hosting brand: Uptime Kuma 2 + `kuma-sync`, which mirrors
WHM/cPanel servers, Proxmox nodes and nameservers into Kuma monitors, groups, tags and
status pages, and sends expiry alerts to ntfy.

Read first: `docs/HISTORY.md` (decisions, current state, open items) and
`docs/GUIDE.md` (operations). The user writes in Serbian (Latin); code, docs and
Kuma-visible names are in English.

## Rules

- Never commit `.env` or put tokens, the Kuma password or ntfy topic names in the repo.
- Deployed on **Dokploy** from `compose.dokploy.yaml` (push to `main` = redeploy).
  `compose.yaml` is the Dockge variant on TrueNAS — keep both working; keep port 3001
  published in both.
- The sync must only touch monitors tagged `cpanel-sync`; never resume a monitor without
  the `auto-paused` tag; never overwrite accepted status codes or a description that the
  sync didn't write.
- Renaming a Kuma-visible name (group, tag, notification) needs an entry in
  `LEGACY_NAMES` so existing installs are renamed in place, not duplicated.
- Test against a real Kuma 2.x (run `server/server.js` from a release tag) with a mock
  WHM; check that a second run reports `no changes`.
- Update `docs/HISTORY.md` when behaviour or deployment changes.
