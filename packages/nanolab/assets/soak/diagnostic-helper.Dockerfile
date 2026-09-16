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
# MAT_URL/MAT_SHA256 pin the released Linux AArch64 Memory Analyzer archive
# (see mat.lock.json). Downloaded and hashed with the stdlib only, verified
# before extraction, and never committed. The digest check is an explicit
# sys.exit, never an `assert`: -O/PYTHONOPTIMIZE strips asserts, and a
# supply-chain check a stray environment variable can disable is not a check.
ARG MAT_URL
ARG MAT_SHA256
RUN python3 -c "import hashlib, sys, urllib.request, zipfile; \
data = urllib.request.urlopen('$MAT_URL').read(); \
actual = hashlib.sha256(data).hexdigest(); \
sys.exit('MAT archive digest mismatch: ' + actual) if actual != '$MAT_SHA256' else None; \
open('/tmp/mat.zip', 'wb').write(data); \
zipfile.ZipFile('/tmp/mat.zip').extractall('/opt')" \
    && chmod +x /opt/mat/ParseHeapDump.sh /opt/mat/MemoryAnalyzer \
    && rm -f /tmp/mat.zip
COPY assets/soak/mat-worker.py /opt/nanolab/mat-worker.py
ENTRYPOINT ["/usr/local/bin/python3", "/opt/nanolab/diagnostic-worker.py", "hold"]
