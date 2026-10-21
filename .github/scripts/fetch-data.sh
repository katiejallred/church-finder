#!/usr/bin/env bash
# Clone the private data repository (contacts, WarmySender ledger, outreach
# copy) into the given folder. The data lives in its own private repo so this
# code repo can be public; DATA_REPO names it (owner/name).
set -euo pipefail
dir="${1:-data}"
repo="${DATA_REPO:-katiejallred/church-finder-data}"
# In Actions, DATA_REPO_TOKEN (a fine-grained token with read and write access
# to Contents on the data repo) is passed in so cloning and pushing work; the
# workflow's own GITHUB_TOKEN cannot reach another repository.
if [ -n "${DATA_REPO_TOKEN:-}" ]; then
  remote="https://x-access-token:${DATA_REPO_TOKEN}@github.com/${repo}.git"
else
  remote="https://github.com/${repo}.git"
fi
git clone --quiet --single-branch --depth 1 "$remote" "$dir"
echo "Data folder ready at $dir"
ls "$dir"
