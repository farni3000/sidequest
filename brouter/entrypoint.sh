#!/usr/bin/env sh
set -eu

JAR="$(ls /opt/brouter/brouter-*-all.jar | head -n 1)"

exec java -Xmx"${JAVA_XMX:-2g}" -cp "$JAR" btools.server.RouteServer \
    /segments \
    /opt/brouter/profiles2 \
    /tmp/customprofiles \
    17777 \
    "${MAX_THREADS:-4}" \
    "${BIND_ADDRESS:-0.0.0.0}"
