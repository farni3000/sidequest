FROM eclipse-temurin:21-jre

ARG BROUTER_VERSION=1.7.10

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/brouter.zip \
        "https://github.com/abrensch/brouter/releases/download/v${BROUTER_VERSION}/brouter-${BROUTER_VERSION}.zip" \
    && unzip -q /tmp/brouter.zip -d /opt \
    && mv "/opt/brouter-${BROUTER_VERSION}" /opt/brouter \
    && rm /tmp/brouter.zip \
    && mkdir -p /tmp/customprofiles /segments

ENV JAVA_XMX=2g \
    MAX_THREADS=4 \
    BIND_ADDRESS=0.0.0.0

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 17777
ENTRYPOINT ["/entrypoint.sh"]
