#!/bin/sh
# Restart the direct EZSP TCP bridge if it ever exits.
LOG=/var/log/ezsp_gateway.log
echo "ezsp_start: starting" > $LOG
while [ 1 ]
do
    /bin/ezsp_gateway >> $LOG 2>&1
    echo "ezsp_start: bridge exited; restarting in 2 seconds" >> $LOG
    sleep 2
done
