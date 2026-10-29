# Container image for scheduled runs (ECS scheduled task, EKS CronJob, CodeBuild, ...).
#   docker build -t ebs-audit .
#   docker run --rm -e AWS_PROFILE -v ~/.aws:/home/app/.aws:ro -v "$PWD/reports:/reports" ebs-audit \
#     scan --output-dir /reports
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /src

RUN useradd --create-home --uid 10001 app && mkdir /reports && chown app /reports
USER app
WORKDIR /reports

ENTRYPOINT ["ebs-audit"]
CMD ["--help"]
