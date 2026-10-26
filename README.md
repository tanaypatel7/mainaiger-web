# MainAIger website and notebook-to-graph design prototype

Live prototype: https://tanaypatel7.github.io/mainaiger-web/

GitHub Pages deployment is verified. Connecting `mainaiger.org` is still pending:
Cloudflare rejected the Google sign-in attempt, and no DNS records were changed.

This repository is the first reviewable website milestone for `mainaiger.org`.
It contains a static interactive design prototype, a complete implementation
plan, a GitHub Pages workflow, and a real dependency-free notebook file converter.

## Start here

1. Open `site/index.html` in a browser, or run `python3 -m http.server 8000 --directory site`
   and visit `http://localhost:8000`.
2. Choose **Notebook → graph**, then **Unfold notebook**. Inspect the same code/output
   cells in both views. Drag cells, fit/arrange, inspect variables, and try the demo run.
3. Use **Connect a device** to review the setup screens. These screens do not execute
   commands or change your devices.
4. Read `docs/DESIGN_PLAN.md` and `docs/FEATURE_MATRIX.csv` for the full design,
   corrections, source-file map, and acceptance criteria.
5. Read `docs/DEPLOY_GITHUB_PAGES.md` to update this repository and connect your
   domain. The repository and GitHub Pages site already exist; DNS setup remains.

## What works in this package

- Static interface navigation, guided setup simulation, notebook/graph animation,
  graph interactions, design explanations, and sample exports.
- Python CLI conversion between notebook JSON and a versioned `.ipymb` wrapper.
  This converter does not execute code or capture a running process.
- GitHub Actions configuration to deploy the contents of `site/`.

## What is still a design specification

Real Python execution in the website, a JupyterLab extension, account authentication,
native installation, environment creation, tmux, device pairing, OS isolation,
remote data transfers, and live telemetry. The website labels all runtime values
and execution as simulated.

The existing MainAIger Lab archive has a custom Python runtime and useful graph
execution features; this package does not patch or install that archive. The design
plan maps future work to its actual source files.

## Files

| Path | Purpose |
| --- | --- |
| `site/index.html` | Application shell and design screens |
| `site/styles.css` | MainAIger visual styles and responsive layout |
| `site/app.js` | Local prototype interactions; no backend connection |
| `site/design-plan.html` | Readable design plan and deployment guide |
| `docs/DESIGN_PLAN.md` | Full design specification and implementation sequence |
| `docs/FEATURE_MATRIX.csv` | Requirements mapped to controls, phases, and acceptance checks |
| `docs/DEPLOY_GITHUB_PAGES.md` | GitHub Pages, domain, HTTPS, and rollback instructions |
| `tools/notebook_bridge.py` | Real `.ipynb` / `.ipymb` file conversion |
| `tests/test_notebook_bridge.py` | Document conversion and rejection checks |
| `examples/` | Small input/output conversion examples |
| `.github/workflows/pages.yml` | Deploy `site/` to GitHub Pages |
| `scripts/render_docs.py` | Dependency-free documentation HTML generator |

## Checks

Try the real document bridge (Python 3.10 or newer):

```sh
python3 tools/notebook_bridge.py import examples/signal-demo.ipynb /tmp/signal-demo.ipymb
python3 tools/notebook_bridge.py validate /tmp/signal-demo.ipymb
python3 tools/notebook_bridge.py export /tmp/signal-demo.ipymb /tmp/signal-roundtrip.ipynb
```

These commands preserve notebook JSON data, not the original whitespace. Advanced
graph semantics cannot be represented by ordinary notebook order; export refuses
them unless `--allow-lossy` is explicitly supplied. Existing output files are
protected unless `--force` is supplied.

```sh
python3 -m unittest discover -s tests -v
node --check site/app.js
python3 scripts/render_docs.py
```

See `docs/VALIDATION.md` for checks performed in the creation environment and
unverified deployment steps. A successful local check is not proof of a live
deployment, real device connection, or Jupyter compatibility.

## Public/private boundary

Publish only this website package. Keep private device addresses, credentials,
chat history, workspaces, logs, and runtime data out of the public repository.
The public website can later open the user's authenticated local/private lab.
It cannot install software or create native Python environments by itself.
