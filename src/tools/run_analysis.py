"""The ``run_analysis`` tool: the agent's single tool.

Stateful Python/pandas execution in a warm Container Apps sandbox, with the
input CSV loaded automatically on first use and the dashboard published as a
side-effect when the agent writes its narrative file. Getting data in and
results out is deterministic (and credentialed) backend work in
``_analyst_common.py`` (underscore-prefixed -> not discovered as a tool), so the
agent never needs a separate load or publish tool.
"""

from __future__ import annotations

import asyncio
import logging

from azure_functions_agents import tool

from _analyst_common import new_telemetry_bucket, run_analysis_sync

# The Functions host only captures logs bound to the active invocation context,
# and ``run_analysis_sync`` runs off that context (under ``asyncio.to_thread``),
# so its sandbox-lifecycle logs never reach Application Insights. We re-emit them
# from the in-context async wrapper below. The logger name must NOT live under
# the ``azure.functions.*`` namespace, which the Python worker suppresses from
# user-log forwarding; ``sports_analytics.sandbox`` propagates to root and is
# captured (same path as the agent_framework / openai logs that do flow).
_telemetry_logger = logging.getLogger("sports_analytics.sandbox")


@tool(
    name="run_analysis",
    description=(
        "Run Python in a persistent, stateful sandbox and get back combined stdout/stderr. "
        "On your FIRST call the input CSV is loaded automatically as the DataFrame `df` and "
        "its schema (columns, dtypes, sample rows, numeric summary) is prepended to the "
        "output - start with a simple cell like print(df.shape) to read it. `df`, your "
        "variables, imports, and functions PERSIST across calls, so iterate: run code, read "
        "the output (including tracebacks), and self-correct. pandas, numpy, matplotlib, and "
        "seaborn are preinstalled and matplotlib already uses the headless Agg backend. "
        "To PUBLISH the dashboard, in a final cell write three files to /workspace/out/: "
        "the chart as 'chart.png' (plt.savefig('/workspace/out/chart.png', "
        "bbox_inches='tight', dpi=120)), an aggregated table as 'summary.csv' "
        "(table.to_csv('/workspace/out/summary.csv', index=False)), and your dashboard "
        "narrative as 'body.html' - an HTML fragment (intro paragraph, <table> leaderboards, "
        "takeaways) containing the literal token {{CHART}} where the chart should appear; "
        "optionally also write a one-line title to 'title.txt'. As soon as body.html exists "
        "the backend assembles and uploads the dashboard, and the call's output confirms it "
        "with a [published] line."
    ),
)
async def run_analysis(code: str) -> str:
    # ``to_thread`` copies the context, so the worker thread fills this same
    # bucket; we drain it here (back in the captured invocation context) and
    # re-emit each event so it lands in Application Insights.
    bucket = new_telemetry_bucket()
    try:
        return await asyncio.to_thread(run_analysis_sync, code)
    finally:
        for evt, fields in bucket:
            detail = " ".join(f"{k}={v}" for k, v in fields.items())
            _telemetry_logger.info("SANDBOX evt=%s %s", evt, detail)
