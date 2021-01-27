#!/bin/bash
# exit if any error
set -e


sudo apt-get install -y sqlite3
mkdir -p /home/pi/db
touch /home/pi/db/pioreactor.sqlite
sqlite3 /home/pi/db/pioreactor.sqlite < sql/create_tables.sql

# checks for duplicates
crontab -l | grep 'backup_database' || (crontab -l 2>/dev/null; echo "0 */12 * * * pio run backup_database") | crontab -