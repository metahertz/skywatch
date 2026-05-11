# Skywatch container, built from the project root.  Reused by the
# pi-feeder compose stack (so the manager UI can run skywatch as
# just-another-container alongside dump1090, dumpvdl2, etc).

FROM python:3.12-slim-bookworm AS build
WORKDIR /src
COPY pyproject.toml ./
COPY skywatch ./skywatch
COPY data ./data
COPY web ./web
RUN pip install --no-cache-dir --root-user-action=ignore .[mongo]

FROM python:3.12-slim-bookworm
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /src/skywatch /opt/skywatch/skywatch
COPY --from=build /src/data    /opt/skywatch/data
COPY --from=build /src/web     /opt/skywatch/web
WORKDIR /opt/skywatch
EXPOSE 8080 8765
CMD ["python3", "-m", "skywatch", "--http", "0.0.0.0:8080", "--ws", "0.0.0.0:8765"]
