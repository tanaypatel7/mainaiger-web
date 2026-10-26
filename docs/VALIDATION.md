# Validation and delivery status

Checked on 25 October 2026. This report describes this package, not an installed
MainAIger runtime on a private device.

## Passed

- Created public repository `tanaypatel7/mainaiger-web`, uploaded this package,
  enabled GitHub Actions as the Pages source, and verified successful deployment
  run `40262814140` (attempt 2). The first attempt occurred before Pages was enabled.
- Opened the published HTTPS site at `https://tanaypatel7.github.io/mainaiger-web/`
  and verified its interface and export dialog. Downloaded its `.ipymb` example
  and successfully validated it with the real Python bridge.

- Python document bridge: **10 unit tests passed**. Coverage includes lossless
  notebook JSON round-trip, unknown metadata, Markdown preservation, missing and
  duplicate original cell IDs, invalid references/types, unsupported schema
  versions, incomplete drafts, changed execution order, advanced graph export
  guards, Unicode, and overwrite protection.
- The supplied `.ipymb` example passes the bridge's structural validator.
- `node --check site/app.js` passes.
- The design-plan example is valid JSON; the feature matrix contains 60 unique
  feature IDs and complete fields. The prototype contains 26 grouped interface
  illustrations with behavior and implementation details.
- Browser review on the local preview: landing page, notebook/graph switch,
  attached code/output cards, graph drag, saved-layout feedback, missing-input
  identification, prerequisite simulation, selected-cell simulation, process
  inspector, setup permission page, feature atlas, and documentation navigation.
- Browser layout check at a 390-pixel iframe width: landing page and graph
  navigation/toolbar render and remain usable. This is a responsive-layout check,
  not a physical iPhone/iPad or Safari acceptance test.
- The separate conversation animation was visually checked in notebook and graph
  states, retaining cell titles, code and outputs.
- GitHub Actions YAML parsed; the official action release tags referenced by the
  workflow were verified and the workflow deployed successfully.

## Corrected during review

The initial graph default fitted the whole document at an unreadably small zoom.
It now opens at a readable zoom with an explicit **Fit overview** action. The
**Try a missing dependency** action now performs the simulated preflight check
instead of only selecting its cell.

## Still unverified or unimplemented

- Authoritative DNS settings, custom-domain routing and custom-domain HTTPS
  certificate issuance. Cloudflare returned “Unable to sign in” after the Google
  authentication attempt. No DNS records or custom-domain routing were changed.
  The domain could not be loaded by the web retrieval service; that does not
  prove the domain is down. The GitHub Pages URL remains available.
- Native Python/Jupyter execution, runtime pairing, authentication, shell execution,
  package installation, actual sandboxing, tmux, private networking, telemetry,
  child processes and service lifecycle. These remain design specifications in
  the website. No device setup command was run by the prototype.
- Browser download delivery on every supported browser, physical touch devices,
  all keyboard paths, screen-reader acceptance, large-graph performance, real
  remote failures, and production browser security policies.

The document converter is real code. All website runs, variables, device
properties and resource metrics are demonstrations. Published status must be
updated only after GitHub and domain verification succeed.
