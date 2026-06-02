#!/usr/bin/env bash
set -euo pipefail

BRANCH="gh-pages"

if git show-ref --verify --quiet "refs/heads/${BRANCH}" || \
   git show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
    echo "${BRANCH} already exists — nothing to do."
    exit 0
fi

echo "Creating orphan branch ${BRANCH}..."
EMPTY_TREE=$(git hash-object -t tree /dev/null)
COMMIT=$(git commit-tree "${EMPTY_TREE}" -m "Initial gh-pages branch")
git branch "${BRANCH}" "${COMMIT}"
git push -u origin "${BRANCH}"

echo "Done. ${BRANCH} created and pushed."
