#!/usr/bin/env bash
# Certbot deploy hook: reload only after this site's certificate was renewed.
set -Eeuo pipefail
[[ ${RENEWED_LINEAGE:-} == /etc/letsencrypt/live/aver.example.com ]] || exit 0
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx.service
