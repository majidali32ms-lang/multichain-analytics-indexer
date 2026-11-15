# The command line with the postgres and serve extras, run as an unprivileged user.
# Base image pinned by digest; Dependabot proposes updates.

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
RUN pip install --no-cache-dir uv==0.13.8
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY multichain_indexer ./multichain_indexer
RUN uv sync --locked --no-dev --no-editable --extra postgres --extra serve

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
RUN useradd --create-home --uid 10001 indexer
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MCI_DATABASE=/home/indexer/data/indexer.db
USER indexer
WORKDIR /home/indexer
RUN mkdir data
ENTRYPOINT ["multichain-indexer"]
CMD ["--help"]
