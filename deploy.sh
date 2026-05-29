#!/bin/bash
# Sync source + .env to the remote and rebuild the container.
#
# The remote must already have the mega client cert + key in
# $REMOTE_DIR/certs/. Run once before the first deploy:
#
#   source .env
#   ssh "$SSH_HOST" mkdir -p "$REMOTE_DIR/certs"
#   scp certs/mega_chain.pem certs/mega.key "$SSH_HOST:$REMOTE_DIR/certs/"
#
# Subsequent deploys (this script) ship source code + .env only; the
# certs in $REMOTE_DIR/certs/ are preserved.
set -e

if [ ! -f .env ]; then
    echo "Error: .env file not found. Copy .env.example to .env and configure it."
    exit 1
fi

# shellcheck disable=SC1091
source .env

: "${SSH_HOST:?SSH_HOST not set in .env}"
: "${REMOTE_DIR:?REMOTE_DIR not set in .env}"

echo "Deploying to ${SSH_HOST}:${REMOTE_DIR}…"
ssh "${SSH_HOST}" mkdir -p "${REMOTE_DIR}"

# Source code — explicit allowlist instead of an excludelist. Anything
# else in the repo (research files, certs, logs, the .git dir) stays
# local.
COPYFILE_DISABLE=1 tar cz \
    main.py \
    samsung_dryer/ \
    Dockerfile \
    docker-compose.yml \
    requirements.txt \
    deploy.sh \
    README.md \
    .env.example \
    .gitignore \
  | ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && tar xz && find . -name '._*' -delete"

# Ship .env separately and lock it down on the remote.
scp .env "${SSH_HOST}:${REMOTE_DIR}/.env"
ssh "${SSH_HOST}" "chmod 600 ${REMOTE_DIR}/.env"

# Verify certs are present on the remote — they have to be uploaded
# once before the first build.
if ! ssh "${SSH_HOST}" "test -s ${REMOTE_DIR}/certs/mega_chain.pem && test -s ${REMOTE_DIR}/certs/mega.key"; then
    echo
    echo "WARNING: ${REMOTE_DIR}/certs/mega_chain.pem and mega.key not"
    echo "found on the remote. The container will start but fail to"
    echo "connect to the dryer until you upload them, e.g.:"
    echo "  ssh ${SSH_HOST} mkdir -p ${REMOTE_DIR}/certs"
    echo "  scp certs/mega_chain.pem certs/mega.key ${SSH_HOST}:${REMOTE_DIR}/certs/"
    echo
fi

echo "Rebuilding container…"
ssh "${SSH_HOST}" "cd ${REMOTE_DIR} && docker compose up -d --build"

echo "Done."
echo "Logs:  ssh ${SSH_HOST} 'cd ${REMOTE_DIR} && docker compose logs -f'"
