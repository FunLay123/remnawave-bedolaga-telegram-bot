FROM postgres:18-alpine

RUN apk add --no-cache rclone tzdata

COPY backup.sh /usr/local/bin/backup.sh
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
COPY RESTORE.md /usr/local/share/remnawave-backup/RESTORE.md

RUN chmod +x /usr/local/bin/backup.sh /usr/local/bin/healthcheck.sh \
    && /usr/local/bin/backup.sh --self-test

ENTRYPOINT ["/usr/local/bin/backup.sh"]
