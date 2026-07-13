# Q15 Ubuntu Cloud Deployment

This package runs exactly one Q15 application process on an Ubuntu VM. It keeps
runtime databases under `/var/lib/q15`, configuration under `/etc/q15`, and
verified backups under `/var/backups/q15`.

## Safety model

- `/etc/q15/safety.env` overrides a copied local environment and forces all
  execution paths to dry-run plus kill mode.
- Do not remove that override during infrastructure migration.
- Never run the local and cloud executors at the same time.
- Port 8000 should remain blocked by the cloud firewall. Use an SSH tunnel for
  the dashboard: `ssh -L 8000:127.0.0.1:8000 root@SERVER_IP`.

## DigitalOcean VM

Create one Ubuntu 24.04 Droplet in the nearest region with at least 2 vCPU,
4 GiB RAM, and 80 GiB disk. Add an SSH key, monitoring, and weekly backups.
Apply a cloud firewall that allows inbound SSH only from the administrator's
current public IP. Do not open port 8000.

Paste `digitalocean-cloud-init.yaml` into the Droplet user-data field. It clones
the public repository and installs Q15 without starting it.

## Phone-only provisioning

If the PC browser is not accessible, the manual GitHub Actions workflow can
create the VM without exposing the DigitalOcean token in chat:

1. On a phone, sign in to DigitalOcean, add a payment method, then open
   `https://cloud.digitalocean.com/account/api/tokens`.
2. Generate a token named `q15-github-provision`, choose a one-day expiration,
   and choose Full Access. The token is only needed during provisioning.
3. Open
   `https://github.com/turneraontez-alt/tez/settings/secrets/actions/new` and
   create a repository secret named `DIGITALOCEAN_ACCESS_TOKEN` whose value is
   the DigitalOcean token. Do not put the token in an issue, commit, or chat.
4. Open
   `https://github.com/turneraontez-alt/tez/actions/workflows/provision-q15-digitalocean.yml`,
   choose **Run workflow**, type `CREATE`, keep `nyc3` and `s-2vcpu-4gb`, and
   run it. This confirms creation of the billed VM and weekly backups.
5. After the workflow succeeds, revoke the one-day DigitalOcean token. The
   workflow summary contains the server IP needed for the final migration.

The workflow is idempotent: rerunning it reuses a Droplet named `q15-prod`
instead of creating a duplicate.

## Secrets and state

Copy the local environment outside Git:

```powershell
scp .env.local root@SERVER_IP:/tmp/q15.env
ssh root@SERVER_IP "install -o root -g q15 -m 0640 /tmp/q15.env /etc/q15/q15.env && rm -f /tmp/q15.env"
```

Create the final migration bundle only after the server is ready. This stops the
local runtime first and captures every SQLite database with SQLite's online
backup API and checksum verification:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/cloud/New-Q15CloudMigrationBundle.ps1 -StopLocal
scp work/cloud-migration/q15-data-*.zip root@SERVER_IP:/var/lib/q15/
```

Restore and start the cloud runtime:

```bash
systemctl stop q15-healthcheck.timer q15-backup.timer q15.service
q15-restore /var/lib/q15/q15-data-*.zip --state-dir /var/lib/q15
chown -R q15:q15 /var/lib/q15 /var/backups/q15
rm -f /var/lib/q15/q15-data-*.zip
systemctl start q15.service
sleep 20
q15-healthcheck
systemctl start q15-healthcheck.timer q15-backup.timer
systemctl enable --now q15-learning-export.service
```

Only enable `q15-learning-export.service` when `/etc/q15/q15.env` has a valid
GitHub token. The GitHub relay is intentionally not run on the server; deploy
updates explicitly with `sudo q15-update` so a code update and service restart
happen together.

## Verification

```bash
systemctl status q15.service --no-pager
curl --fail --silent http://127.0.0.1:8000/api/health
journalctl -u q15.service --since "10 minutes ago" --no-pager
systemctl list-timers 'q15-*'
```

After cloud health, websocket connectivity, Telegram routing, ledgers, and
settlement updates are verified, leave the Windows scheduled task disabled.
Changing live execution flags is a separate operational decision.
