# Sample output

These are the real artifacts the agent wrote to the `output` blob container during a live run against [`sample-data/matches.csv`](../sample-data/matches.csv) — captured here so you can see what the agent produces **without deploying anything**.

| File | What it is |
|------|-----------|
| [`dashboard.html`](dashboard.html) | The self-contained dashboard the backend publishes — narrative + leaderboards (top run-scorers, wicket-takers, team standings) with the chart embedded inline. Open it in any browser. |
| [`chart.png`](chart.png) | The `matplotlib` chart the agent rendered (top run-scorers), embedded into the dashboard. |
| [`summary.csv`](summary.csv) | The top-batters summary table the agent exported. |

How it was produced: the timer-triggered agent loaded the CSV into a warm ACA sandbox, made several `run_analysis` calls (look → compute leaderboards → compute standings → render chart → compose HTML), then the backend assembled `dashboard.html` and uploaded all three files. See the main [README](../README.md) for the full flow.
