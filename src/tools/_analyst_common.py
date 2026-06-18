"""Shared backend for the sports-analytics agent's single ``run_analysis`` tool.

Underscore-prefixed so the agent runtime's tool discovery (which loads
``tools/*.py`` files NOT starting with ``_`` and registers the first
``FunctionTool`` per file) ignores it. The single ``@tool`` function lives in
``run_analysis.py``.

Design: a PERSISTENT, STATEFUL sandbox + Blob Storage for data in/out, behind
ONE tool. The agent only ever writes pandas code; getting the CSV in and the
dashboard out is handled deterministically in this backend (where the managed
identity lives) rather than as extra agent-facing tools:

  * INPUT  - on the first ``run_analysis`` call of a run, the input CSV is
             pulled from Blob Storage into the warm sandbox and loaded as the
             DataFrame ``df``; its schema is prepended to that call's output.
             Re-loaded automatically when the input blob changes (new ETag).
  * OUTPUT - after each call, if the agent has written ``/workspace/out/body.html``
             (its dashboard narrative), the backend wraps it into a styled,
             self-contained HTML document with the chart embedded as a base64
             PNG and uploads ``dashboard.html`` / ``chart.png`` / ``summary.csv``
             to the output container, then clears ``body.html`` so it is
             published exactly once.

Unlike the code-grader (a fresh throwaway sandbox per submission), this app
keeps ONE long-lived Container Apps sandbox warm across timer runs. The data
stack (pandas / numpy / matplotlib / seaborn) is pip-installed once into the
sandbox's persistent disk and is reused on every run, and - via ``dill``
session persistence - the loaded DataFrame and any intermediate variables
survive from one ``run_analysis`` cell to the next so the agent can iterate
(write code, read stderr, self-correct) like a notebook kernel.

The sandbox holds NO credential: the function app's managed identity reads and
writes blobs and drives the sandbox data plane. Only data (the CSV in, the
results out) crosses the trust boundary.

Wiring (set by infra/main.bicep as app settings):
    SANDBOX_REGION           - region of the sandbox group (e.g. eastus2)
    SANDBOX_SUBSCRIPTION_ID  - subscription containing the sandbox group
    SANDBOX_RESOURCE_GROUP   - resource group containing the sandbox group
    SANDBOX_GROUP_NAME       - Microsoft.App/sandboxGroups resource name
    AZURE_CLIENT_ID          - user-assigned managed identity client id (Azure)
    DATA_BLOB_ENDPOINT       - https://<account>.blob.core.windows.net
    DATA_INPUT_CONTAINER     - container the input CSV is read from (e.g. input)
    DATA_OUTPUT_CONTAINER    - container results are written to (e.g. output)
    INPUT_BLOB_NAME          - fixed blob name to analyse each run (e.g. matches.csv)

Auth uses ``DefaultAzureCredential`` so it works with the function app's managed
identity in Azure and with ``az login`` locally. The identity needs
"Storage Blob Data Owner" on the storage account and "Container Apps
SandboxGroup Data Owner" on the sandbox group (both granted in infra/).
"""

from __future__ import annotations

import base64
import logging
import os

from azure.identity import DefaultAzureCredential
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region

logger = logging.getLogger("sports_analytics.sandbox")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_REGION = os.environ.get("SANDBOX_REGION", "eastus2")
_SUBSCRIPTION_ID = os.environ.get("SANDBOX_SUBSCRIPTION_ID", "")
_RESOURCE_GROUP = os.environ.get("SANDBOX_RESOURCE_GROUP", "")
_GROUP_NAME = os.environ.get("SANDBOX_GROUP_NAME", "")
_MI_CLIENT_ID = os.environ.get("AZURE_CLIENT_ID")  # user-assigned MI in Azure

_BLOB_ENDPOINT = os.environ.get("DATA_BLOB_ENDPOINT", "")
_INPUT_CONTAINER = os.environ.get("DATA_INPUT_CONTAINER", "input")
_OUTPUT_CONTAINER = os.environ.get("DATA_OUTPUT_CONTAINER", "output")
_INPUT_BLOB_NAME = os.environ.get("INPUT_BLOB_NAME", "matches.csv")

_WORKDIR = "/workspace"
_DATA_DIR = f"{_WORKDIR}/data"
_OUT_DIR = f"{_WORKDIR}/out"
_DATASET_FILE = f"{_DATA_DIR}/{_INPUT_BLOB_NAME}"
_CHART_FILE = f"{_OUT_DIR}/chart.png"
_SUMMARY_FILE = f"{_OUT_DIR}/summary.csv"
_BODY_FILE = f"{_OUT_DIR}/body.html"
_TITLE_FILE = f"{_OUT_DIR}/title.txt"
_ETAG_MARKER = f"{_WORKDIR}/.dataset_etag"
_SESSION_FILE = f"{_WORKDIR}/_session.pkl"
_CELL_FILE = f"{_WORKDIR}/_cell.py"
_RUNNER_FILE = f"{_WORKDIR}/_runner.py"
_APP_LABEL = "sports-analytics"
_DATASET_LABEL = "cricket"

# Persistent "kernel" runner. Restores the saved interpreter session (if any),
# executes the current cell against the module globals, then saves the session
# back to the persistent disk so variables/imports/DataFrames survive across
# separate ``run_analysis`` calls and across suspend/resume. matplotlib is
# forced onto the headless Agg backend so chart rendering never needs a display.
_RUNNER_SOURCE = '''
import os, sys, io, contextlib, traceback
os.environ.setdefault("MPLBACKEND", "Agg")
os.makedirs("/workspace/out", exist_ok=True)

SESSION = "/workspace/_session.pkl"
CELL = "/workspace/_cell.py"

try:
    import dill
except Exception as exc:  # pragma: no cover - dill is installed at bootstrap
    dill = None
    _dill_err = exc

_restore_err = None
if dill is not None and os.path.exists(SESSION):
    try:
        dill.load_session(SESSION)
    except Exception as exc:
        _restore_err = str(exc)

buf = io.StringIO()
if _restore_err:
    print(f"[analyst] could not restore previous state: {_restore_err}", file=buf)

with open(CELL, "r", encoding="utf-8") as fh:
    code = fh.read()

try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        exec(compile(code, "<cell>", "exec"), globals())
except SystemExit:
    pass
except Exception:
    traceback.print_exc(file=buf)

if dill is not None:
    try:
        dill.dump_session(SESSION)
    except Exception as exc:
        print(f"[analyst] could not save state (some objects may not persist): {exc}", file=buf)
else:
    print(f"[analyst] dill unavailable, state will not persist: {_dill_err}", file=buf)

sys.stdout.write(buf.getvalue())
'''

# One-time bootstrap for a freshly created sandbox. The base image ships WITHOUT
# pip/ensurepip, so pip is installed via apt first, then the data-analysis stack
# (and dill for session persistence) is pip-installed with --break-system-packages
# (the image is an externally-managed PEP 668 environment). These land on the
# persistent disk, so they survive suspend/resume and are never reinstalled for
# the life of the sandbox.
_PIP = "python3 -m pip install --break-system-packages --root-user-action=ignore --quiet"
_BOOTSTRAP_COMMAND = (
    "mkdir -p /workspace/data /workspace/out && "
    "apt-get update -y >/dev/null 2>&1 && "
    "apt-get install -y python3-pip >/dev/null 2>&1 && "
    f"{_PIP} dill pandas numpy matplotlib seaborn"
)

_credential = None
_group_client: SandboxGroupClient | None = None
_blob_service: BlobServiceClient | None = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential(managed_identity_client_id=_MI_CLIENT_ID)
    return _credential


def _get_group_client() -> SandboxGroupClient:
    """Lazily build a process-wide data-plane client for the sandbox group."""
    global _group_client
    if _group_client is None:
        if not (_SUBSCRIPTION_ID and _RESOURCE_GROUP and _GROUP_NAME):
            raise RuntimeError(
                "Sandbox configuration missing. Ensure SANDBOX_SUBSCRIPTION_ID, "
                "SANDBOX_RESOURCE_GROUP, and SANDBOX_GROUP_NAME app settings are set."
            )
        _group_client = SandboxGroupClient(
            endpoint_for_region(_REGION),
            _get_credential(),
            subscription_id=_SUBSCRIPTION_ID,
            resource_group=_RESOURCE_GROUP,
            sandbox_group=_GROUP_NAME,
        )
    return _group_client


def _get_blob_service() -> BlobServiceClient:
    """Lazily build a process-wide Blob service client for the data account."""
    global _blob_service
    if _blob_service is None:
        if not _BLOB_ENDPOINT:
            raise RuntimeError(
                "Blob configuration missing. Ensure DATA_BLOB_ENDPOINT is set."
            )
        _blob_service = BlobServiceClient(_BLOB_ENDPOINT, credential=_get_credential())
    return _blob_service


def _get_or_create_sandbox():
    """Return a running SandboxClient for this app, creating one if needed.

    The sandbox is keyed by stable labels so every run reuses the same warm,
    long-lived sandbox. A newly created one is bootstrapped with the data stack
    once; an existing one is simply resumed if it had auto-suspended.
    """
    group = _get_group_client()
    labels = {"app": _APP_LABEL, "dataset": _DATASET_LABEL}

    existing = None
    for sbx in group.list_sandboxes(labels=labels):
        state = (sbx.state or "").lower()
        if state not in ("deleting", "failed"):
            existing = sbx
            break

    if existing is not None:
        client = group.get_sandbox_client(existing.id)
        client.ensure_running(timeout=300)
        return client, False

    client = group.begin_create_sandbox(
        disk="ubuntu",
        cpu="1000m",
        memory="2048Mi",
        auto_suspend_seconds=600,
        auto_suspend_mode="Memory",
        labels=labels,
    ).result()
    client.ensure_running(timeout=300)
    logger.info("SANDBOX CREATED sandbox_id=%s (persistent, warm)", client.sandbox_id)
    boot = client.exec(_BOOTSTRAP_COMMAND)
    if boot.exit_code not in (0, None):
        raise RuntimeError(
            "Sandbox bootstrap failed (could not install the data stack). "
            f"exit={boot.exit_code} stderr={(boot.stderr or '').strip()[:500]}"
        )
    return client, True


def _exec_cell(client, code: str) -> str:
    """Run one cell in the warm sandbox and return combined stdout/stderr text."""
    client.write_file(_RUNNER_FILE, _RUNNER_SOURCE, create_dirs=True)
    client.write_file(_CELL_FILE, code, create_dirs=True)
    result = client.exec(f"cd {_WORKDIR} && MPLBACKEND=Agg python3 {_RUNNER_FILE}")

    parts: list[str] = []
    output = (result.stdout or "").rstrip()
    if output:
        parts.append(output)
    stderr = (result.stderr or "").strip()
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if result.exit_code not in (0, None):
        parts.append(f"[exit code: {result.exit_code}]")
    return "\n".join(parts) if parts else "(no output)"


# ---------------------------------------------------------------------------
# Tool backends
# ---------------------------------------------------------------------------

# Cell run on first load to define ``df`` in the persistent session and print a
# schema profile the agent can reason over (prepended to that call's output).
_PROFILE_CELL = f'''
import pandas as pd
df = pd.read_csv("{_DATASET_FILE}")
print("rows:", df.shape[0], "columns:", df.shape[1])
print("column_names:", list(df.columns))
print("dtypes:")
print(df.dtypes.to_string())
print("head:")
print(df.head(8).to_string(index=False))
print("numeric_summary:")
print(df.describe(include="number").to_string())
'''


def _ensure_dataset_loaded(client) -> str:
    """Pull the input CSV into the warm sandbox and define ``df`` when needed.

    Idempotent within a run and across the sandbox's life: it compares the input
    blob's ETag against a marker stored in the sandbox and only (re)loads when
    the data is new. Returns text to prepend to the current call's output - the
    freshly-read schema profile when a load happened, a one-time notice when no
    input CSV exists, or an empty string when ``df`` is already current.
    """
    service = _get_blob_service()
    blob = service.get_blob_client(_INPUT_CONTAINER, _INPUT_BLOB_NAME)
    prev_marker = _read_sandbox_file(client, _ETAG_MARKER)
    prev = prev_marker.decode("utf-8", "replace").strip() if prev_marker else ""

    try:
        props = blob.get_blob_properties()
    except ResourceNotFoundError:
        if prev == "no-data":
            return ""
        client.write_file(_ETAG_MARKER, "no-data", create_dirs=True)
        logger.info(
            "NO INPUT blob=%s/%s sandbox_id=%s",
            _INPUT_CONTAINER, _INPUT_BLOB_NAME, client.sandbox_id,
        )
        return (
            f"[no input] Blob '{_INPUT_BLOB_NAME}' was not found in container "
            f"'{_INPUT_CONTAINER}', so there is no DataFrame to analyse this run. "
            "Publish a short dashboard noting there was nothing to analyse, then stop.\n"
        )

    etag = (props.etag or "").strip('"')
    if etag and etag == prev:
        return ""  # df already loaded for this version of the input

    csv_bytes = blob.download_blob().readall()
    client.write_file(_DATASET_FILE, csv_bytes, create_dirs=True)
    client.write_file(_ETAG_MARKER, etag or "loaded", create_dirs=True)
    logger.info(
        "DATASET LOADED blob=%s/%s bytes=%d sandbox_id=%s",
        _INPUT_CONTAINER, _INPUT_BLOB_NAME, len(csv_bytes), client.sandbox_id,
    )
    profile = _exec_cell(client, _PROFILE_CELL)
    return (
        f"[dataset loaded] '{_INPUT_BLOB_NAME}' ({len(csv_bytes)} bytes) is available as the "
        f"DataFrame `df`.\n{profile}\n"
    )


def run_analysis_sync(code: str) -> str:
    """The single tool's backend: load (if needed) -> run code -> publish (if ready).

    On the first call of a run the input CSV is loaded as ``df`` and its schema
    is prepended. The agent's ``code`` runs in the warm, stateful sandbox, where
    ``df``, variables, imports and functions persist across calls. If the code
    has written ``/workspace/out/body.html``, the dashboard is assembled from
    the saved chart/summary and uploaded to Blob Storage as a side-effect.
    """
    client, _ = _get_or_create_sandbox()
    prefix = _ensure_dataset_loaded(client)
    output = _exec_cell(client, code)
    published = _maybe_publish(client)
    return f"{prefix}{output}{published}"


def _read_sandbox_file(client, path: str) -> bytes | None:
    try:
        data = client.read_file(path)
        return data or None
    except Exception:
        return None


def _build_dashboard_html(title: str, body_html: str, chart_b64: str | None) -> str:
    """Wrap the agent's narrative in a styled, self-contained HTML document."""
    import datetime
    import re

    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    chart_block = (
        f'<img class="chart" alt="chart" src="data:image/png;base64,{chart_b64}">'
        if chart_b64
        else '<p class="muted">(no chart was produced this run)</p>'
    )
    # Accept the chart placeholder however the model writes it: {{CHART}} or
    # {CHART}, any casing or inner spacing. If none is present, append the chart.
    token = re.compile(r"\{\{?\s*chart\s*\}\}?", re.IGNORECASE)
    if token.search(body_html):
        body = token.sub(chart_block, body_html)
    else:
        body = f"{body_html}\n{chart_block}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         margin: 0; padding: 2rem; line-height: 1.55; max-width: 960px;
         margin-inline: auto; color: #1b1b1f; background: #fafafa; }}
  h1 {{ font-size: 1.7rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: 1.2rem; margin-top: 2rem; border-bottom: 2px solid #e2e2e6; padding-bottom: .3rem; }}
  .muted {{ color: #6b6b70; }}
  .meta {{ color: #6b6b70; font-size: .9rem; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .95rem; }}
  th, td {{ border: 1px solid #e2e2e6; padding: .5rem .7rem; text-align: left; }}
  th {{ background: #f0f0f4; }}
  tr:nth-child(even) td {{ background: #f6f6f9; }}
  img.chart {{ max-width: 100%; height: auto; border: 1px solid #e2e2e6; border-radius: 8px;
              margin: 1rem 0; background: #fff; }}
  footer {{ margin-top: 2.5rem; color: #8a8a8f; font-size: .8rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">Generated {generated} &middot; headless serverless agent</p>
{body}
<footer>Produced automatically by a timer-triggered Azure Functions agent. Data analysed in an isolated Azure Container Apps sandbox.</footer>
</body>
</html>
"""


def _maybe_publish(client) -> str:
    """Publish the dashboard iff the agent wrote ``/workspace/out/body.html``.

    Reads the narrative the agent saved (``body.html``), an optional one-line
    ``title.txt``, plus ``chart.png`` and ``summary.csv``; embeds the chart into
    a self-contained HTML document; and uploads ``dashboard.html`` / ``chart.png``
    / ``summary.csv`` to the output container. Clears ``body.html`` afterwards so
    a later call in the same run does not republish. Returns a short note (or an
    empty string when nothing was published).
    """
    body_bytes = _read_sandbox_file(client, _BODY_FILE)
    if body_bytes is None:
        return ""
    body_html = body_bytes.decode("utf-8", "replace")
    title_bytes = _read_sandbox_file(client, _TITLE_FILE)
    title = (title_bytes.decode("utf-8", "replace").strip() if title_bytes else "") or "Analytics Dashboard"

    chart_bytes = _read_sandbox_file(client, _CHART_FILE)
    summary_bytes = _read_sandbox_file(client, _SUMMARY_FILE)
    chart_b64 = base64.b64encode(chart_bytes).decode("ascii") if chart_bytes else None

    html = _build_dashboard_html(title, body_html, chart_b64)

    service = _get_blob_service()
    container = service.get_container_client(_OUTPUT_CONTAINER)
    try:
        container.create_container()
    except ResourceExistsError:
        pass

    uploaded: list[str] = []
    container.upload_blob(
        "dashboard.html",
        html.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="text/html; charset=utf-8"),
    )
    uploaded.append("dashboard.html")
    if chart_bytes:
        container.upload_blob(
            "chart.png",
            chart_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="image/png"),
        )
        uploaded.append("chart.png")
    if summary_bytes:
        container.upload_blob(
            "summary.csv",
            summary_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type="text/csv; charset=utf-8"),
        )
        uploaded.append("summary.csv")

    # Clear the narrative so the dashboard is published exactly once per run.
    client.write_file(_BODY_FILE, "", create_dirs=True)

    logger.info(
        "DASHBOARD PUBLISHED container=%s blobs=%s sandbox_id=%s",
        _OUTPUT_CONTAINER,
        ",".join(uploaded),
        client.sandbox_id,
    )
    base = _BLOB_ENDPOINT.rstrip("/")
    urls = "\n".join(f"  - {base}/{_OUTPUT_CONTAINER}/{name}" for name in uploaded)
    note = (
        f"\n[published] {len(uploaded)} file(s) to the '{_OUTPUT_CONTAINER}' container:\n"
        f"{urls}"
    )
    if not chart_bytes:
        note += "\n[published] note: no chart.png was found in /workspace/out/ this run."
    return note
