# Evaluation results

No published run yet.

To produce one:

1. Trigger the Eval workflow manually (Actions → Eval → Run workflow), or run `lights-out eval` locally.
2. Download the `eval-results` artifact.
3. Run `lights-out eval-report <results> --baseline eval/baseline.json --markdown eval/RESULTS.md`.
4. Commit `eval/RESULTS.md`. To set a new baseline, copy `results.json` to `eval/baseline.json`.

CI never pushes to the repository; a human commits the results.
