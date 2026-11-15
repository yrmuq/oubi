#!/bin/sh
set -eu

git fetch origin main
main=$(git rev-parse origin/main)
if git ls-remote --exit-code --heads origin refs/heads/deps >/dev/null; then
    git fetch origin deps
else
    status=$?
    [ "$status" -eq 2 ] || exit "$status"
    git push origin "$main:refs/heads/deps"
    exit
fi

if git merge-base --is-ancestor "$main" origin/deps; then
    exit
fi
if git merge-base --is-ancestor origin/deps "$main"; then
    git push origin "$main:refs/heads/deps"
    exit
fi

head=$(git symbolic-ref --short -q HEAD || git rev-parse HEAD)
git switch --detach origin/deps
if ! git merge --no-ff --no-commit "$main"; then
    git rev-parse --verify MERGE_HEAD >/dev/null
fi
git restore --source="$main" --staged --worktree -- VERSION
if [ -n "$(git ls-files --unmerged)" ]; then
    git status --short >&2
    git merge --abort
    git checkout "$head"
    exit 1
fi
git commit -m "Merge branch 'main' into deps"
git push origin HEAD:refs/heads/deps
git checkout "$head"
