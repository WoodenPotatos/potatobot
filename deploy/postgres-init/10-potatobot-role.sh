#!/bin/sh
# Runs once, when the official postgres image initialises an empty data
# directory (/docker-entrypoint-initdb.d). It creates the bot's own role and
# database: the role owns the database and nothing else, and is no superuser,
# so the bot's credentials cannot create roles, read other databases or
# change the server. The superuser password (POSTGRES_PASSWORD) stays in the
# database container's own environment file, never in the bot's.
#
# POTATOBOT_DB_PASSWORD comes from that same file and must match PGPASSWORD in
# the bot's /etc/potatobot/potatobot.env (docs/installation.md).
set -eu
: "${POTATOBOT_DB_PASSWORD:?POTATOBOT_DB_PASSWORD must be set}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     --set=password="$POTATOBOT_DB_PASSWORD" <<'SQL'
CREATE ROLE potatobot LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'password';
CREATE DATABASE potatobot OWNER potatobot;
REVOKE ALL ON DATABASE potatobot FROM PUBLIC;
SQL
