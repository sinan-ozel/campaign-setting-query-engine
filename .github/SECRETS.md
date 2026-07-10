# Required GitHub Secrets

Set these at **Settings → Secrets and variables → Actions → New repository secret**.

---

## `DOCKERHUB_USERNAME`

Your Docker Hub username. Used to tag and push the image:
```
<DOCKERHUB_USERNAME>/campaign-setting-query-engine:<version>
```

## `DOCKERHUB_TOKEN`

A Docker Hub **access token** (not your account password).

To create one:
1. Log in to [hub.docker.com](https://hub.docker.com)
2. Go to **Account Settings → Personal access tokens → Generate new token**
3. Give it **Read & Write** scope
4. Copy the token — it is only shown once

---

## `GITHUB_TOKEN`

Automatically provided by GitHub Actions. No setup required.

Used for:
- Committing reformatted code back to branches
- Pushing git tags on stable releases
- Creating GitHub Releases
- Deploying docs to GitHub Pages

---

## Notes

- The publish job only runs on pushes to `main` when code in `server/` or `README` has changed since the last tag.
- To trigger a **stable** release: bump `__version__` in `server/__init__.py` above the last git tag, then merge to `main`.
- Without `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` the publish job will fail. The reformat, lint, test, and validate-docs jobs are unaffected.

---

# AWS deployment secrets (provision / teardown / deploy / delete workflows)

Used by `.github/workflows/{provision,teardown,deploy,delete}.yaml` — see
[Deploying on AWS with a GPU node](../docs/helm.md#deploying-on-aws-with-a-gpu-node).
None of these are required unless you're using those workflows.

## `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`

The least-privilege provisioner IAM user's credentials, printed by running
`k3s: First-time setup (AWS)` (`k3s-anywhere`'s `ACTION=setup`) locally, once
per AWS account, with your own admin credentials. Never run `ACTION=setup`
in CI — it's deliberately excluded from every workflow here.

## `PULUMI_CONFIG_PASSPHRASE`

Any strong passphrase you choose — encrypts the Pulumi stack state that
`k3s-anywhere` stores in `STATE_BUCKET_NAME`.

## `STATE_BUCKET_NAME`

The Pulumi state bucket name, also printed by `k3s: First-time setup (AWS)`.

## `SOPS_AGE_KEY`

The `AGE-SECRET-KEY-1...` private key from:
```bash
docker run --rm --entrypoint age-keygen sinanozel/k3s-anywhere:0.1.9
```
Decrypts the cluster output artifact (kubeconfig + backup bucket
credentials) that `provision.yaml`/`teardown.yaml` upload. The matching
public key (`age1...`) goes into `provision.yaml`'s/`teardown.yaml`'s
`sops_age_recipient:` — replace the `age1_REPLACE_WITH_YOUR_AGE_PUBLIC_KEY`
placeholder there with it.

## `LETSENCRYPT_EMAIL`

Email address registered with the ACME account cert-manager uses to issue
the Let's Encrypt certificate for the nip.io Ingress hosts.
