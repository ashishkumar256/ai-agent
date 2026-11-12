#!/bin/sh
REPO_URL=$(git remote get-url origin | sed -e 's/\.git$//' -e 's#^git@\([^:]*\):#https://\1/#')
COMMIT_SHA=$(git rev-parse HEAD)

cat << ENV > .env.git
REPO_URL=$REPO_URL
COMMIT_SHA=$COMMIT_SHA
COMMIT_URL=$REPO_URL/commit/$COMMIT_SHA
ENV
