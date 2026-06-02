# Required GitHub Secrets

Set these at **Settings → Secrets and variables → Actions → New repository secret**.

---

## `DOCKERHUB_USERNAME`

Your Docker Hub username. Used to tag and push the image:
```
<DOCKERHUB_USERNAME>/<SERVER-NAME>:<version>
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
