FROM python:3.12.11-slim-bookworm

WORKDIR /opt/runtime-benchmark
COPY src/ai_intel_agent/runtime_workload.py ./runtime_workload.py

USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python", "runtime_workload.py"]
