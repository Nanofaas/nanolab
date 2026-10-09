FROM python:3.12-slim
COPY artifact_contract.py /app/artifact_contract.py
USER 65534:65534
ENTRYPOINT ["python3", "/app/artifact_contract.py"]
