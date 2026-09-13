# Main builds this from the packages/nanolab context. Both arguments MUST be
# operator-resolved repository@sha256 digests for linux/arm64 on P24.
ARG JDK_BASE
ARG PYTHON_BASE
FROM ${JDK_BASE} AS jdk
FROM ${PYTHON_BASE}
COPY --from=jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH=/opt/java/openjdk/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1
COPY src/nanolab/tasks/soak/processes.py /opt/nanolab/processes.py
COPY assets/soak/diagnostic-worker.py assets/soak/full-gc.jfc /opt/nanolab/
ENTRYPOINT ["/usr/local/bin/python3", "/opt/nanolab/diagnostic-worker.py", "hold"]
