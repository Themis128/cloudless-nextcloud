# cloudless-nextcloud

Self-hosted Nextcloud implementation for **cloud.cloudless.gr** (plus `office.cloudless.gr` Collabora and `signal.cloudless.gr` Talk HPB), running as a plain Docker Compose "workspace" stack on the OMV host — extracted with full git history from `cu130-slim`, `omv-infrastructure`, and `cloudless.gr`.

## Layout

| Path | Source repo | Contents |
|---|---|---|
| `deploy/omv-workspace/` | cu130-slim | Compose stack (nextcloud, nextcloud-cron, notify-push, talk-hpb, talk-recording, collabora, cloudflared, redis, postgres), host scripts, php/apache overrides |
| `.devin/skills/omv-nextcloud-ops/`, `.cursor/skills/omv-nextcloud-ops/` | cu130-slim | Ops skill: SSH/deploy/runbook for the workspace |
| `docs/superpowers/guides/19-nextcloud-client-portals.md` | cu130-slim | Client portal + Talk room guide |
| `n8n-workflows/nextcloud-drop-to-draft.json` | cu130-slim | n8n workflow touching Nextcloud |
| `omv/nextcloud/` | omv-infrastructure | Host-side config: apache/php conf.d, backup service/timer, trusted_proxies + tunnel docs |
| `omv/install-nextcloud-talk-turn-host.py`, `omv/tests/` | omv-infrastructure | TURN host installer + test |
| `infrastructure/omv/nextcloud/` | cloudless.gr | Same host-side config codified in the site repo |
| `infrastructure/workspace-agent-ready/` | cloudless.gr | Agent-readiness discovery docs for the workspace |

## Ops quick reference

- Host: OMV `192.168.1.128` (omv), SSH via deploy key only
- Compose sources live on the OMV host under `/srv/dev-disk-by-uuid-*/compose-files/workspace/` + `workspace/`
- `occ`: `sudo docker exec -u www-data nextcloud php occ …`
- Public endpoints via `social-cloudflared` tunnel: `cloud.` / `office.` / `signal.cloudless.gr`
