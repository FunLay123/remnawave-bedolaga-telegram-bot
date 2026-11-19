FROM postgres:18-alpine

# restic does the encrypted, deduplicated repository; rclone supplies the
# Google Drive (or local test) backend.  curl is only for optional pings.
# Keep the capability check rather than pinning APK versions here; a package
# bump must not silently drop the stdin-from-command contract.
RUN apk add --no-cache curl restic rclone tzdata \
    && restic version \
    && restic backup --help | grep -q -- '--stdin-from-command' \
    && rclone version | head -n 1

COPY backup.sh /usr/local/bin/backup.sh
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
COPY RESTORE.md /usr/local/share/remnawave-backup/RESTORE.md

RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/healthcheck.sh \
    && /usr/local/bin/backup.sh --self-test

ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["--loop"]
