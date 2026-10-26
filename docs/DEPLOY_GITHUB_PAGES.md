# Publish MainAIger on GitHub Pages

This project publishes the contents of `site/`. The repository also contains design documents and a deployment workflow; those are not uploaded to the website artifact. No Node build or Python backend is required for this static prototype.

Intended repository: `tanaypatel7/mainaiger-web`.

Intended final address: `https://mainaiger.org/`.

Treat the repository and domain configuration as pending unless a separate deployment report confirms success. A web fetch cannot establish the current authoritative DNS configuration. Do not infer that the domain is broken from a failed fetch.

## 1. Review and test the local website

Extract the package into its own directory. Keep this website project separate from an existing MainAIger application checkout. In Terminal, change into the extracted directory that contains `site/`, `docs/`, and `.github/`:

```bash
cd /path/to/mainaiger-web
python3 -m http.server 8080 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:8080/`. Check the notebook-to-graph animation, navigation, canvas actions, and mobile layout. Stop the preview with Ctrl+C. This command serves files; it does not start a Python execution backend.

Before upload, review the repository contents. Public source and published assets must contain no account secrets, local tokens, SSH keys, private experiment files, or personal device configuration. Prototype runtime and telemetry labels should remain visibly marked as demonstrations.

## 2. Create and upload the new repository

The commands below assume Git and GitHub CLI are installed and this extracted folder has no existing Git repository. They create a public website-source repository. Run them in the project folder, not in the existing MainAIger application folder.

```bash
git init -b main
git add .
git status --short
git commit -m "Add MainAIger website design prototype"
gh auth status
```

If GitHub CLI is not authenticated, run `gh auth login` and complete its browser login. Confirm the active GitHub account can create `tanaypatel7/mainaiger-web`. Then:

```bash
gh repo create tanaypatel7/mainaiger-web --public --source=. --remote=origin --push
```

GitHub CLI's `--source` uses the current local repository and `--push` sends its commits. [CLI reference](https://cli.github.com/manual/gh_repo_create)

If the repository has already been created and is empty, do not run the create command again. Inspect `git remote -v`, then add its remote if none exists:

```bash
git remote add origin https://github.com/tanaypatel7/mainaiger-web.git
git push -u origin main
```

If that remote already contains commits, clone it into a separate directory and copy these website files into that checkout; review the diff before committing. Do not force-push over existing work.

## 3. Enable Pages and run the deployment

1. Open the repository's **Settings → Pages**.
2. Under **Build and deployment → Source**, select **GitHub Actions**.
3. Open **Actions → Deploy MainAIger website → Run workflow**, select `main`, and run it. An earlier run may have failed before Pages was enabled; rerun after enabling it.
4. Wait for the deployment job to finish successfully. Open the URL recorded by its `github-pages` environment. Before assigning a custom domain, the expected project URL is `https://tanaypatel7.github.io/mainaiger-web/`; use the URL GitHub actually reports.

Equivalent commands after enabling Pages:

```bash
gh workflow run pages.yml --ref main
gh run list --workflow pages.yml --limit 5
```

The workflow checks out the project, checks `site/index.html`, configures Pages, uploads only `site/`, and deploys it. Its built-in GitHub token has `contents: read`, `pages: write`, and `id-token: write`; no personal access token belongs in the website. [Pages workflow documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

## 4. Verify domain ownership before routing traffic

In the GitHub account's **Settings → Pages**, add `mainaiger.org` under verified domains. Copy GitHub's exact generated TXT record into the DNS provider; do not invent its value. Verify in GitHub when DNS has propagated, and retain that TXT record. This proves domain control; it does not deploy the website or create MainAIger user authentication. [Domain verification](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/verifying-your-custom-domain-for-github-pages)

Before changing website routing, record the current DNS settings and identify whether another site currently uses the apex or `www`. Preserve all unrelated records, especially MX and mail-related TXT records (SPF, DKIM, DMARC). If an existing website must remain, test this project on its GitHub URL or a separately chosen subdomain first.

## 5. Connect mainaiger.org

Set **repository Settings → Pages → Custom domain** to `mainaiger.org` and save **before** changing its DNS routing. At the authoritative DNS provider, use the records below. Replace only conflicting website-routing records after reviewing their current purpose. Do not change nameservers or email records for this task.

| Type | Name | Value |
|---|---|---|
| A | `@` | `185.199.108.153` |
| A | `@` | `185.199.109.153` |
| A | `@` | `185.199.110.153` |
| A | `@` | `185.199.111.153` |
| AAAA | `@` | `2606:50c0:8000::153` |
| AAAA | `@` | `2606:50c0:8001::153` |
| AAAA | `@` | `2606:50c0:8002::153` |
| AAAA | `@` | `2606:50c0:8003::153` |
| CNAME | `www` | `tanaypatel7.github.io` |

Use the DNS provider's equivalent apex field if it does not accept `@`. Alternatively, a provider-supported apex ALIAS/ANAME can target `tanaypatel7.github.io`; use one apex strategy. Avoid wildcard records. The `www` target contains neither a protocol nor `/mainaiger-web/`.

Custom Actions deployments use the repository's domain setting; a repository `CNAME` file is not required. [Official domain and DNS configuration](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site)

## 6. Check DNS, HTTPS, and the interface

From macOS/Linux, inspect the current public records:

```bash
dig mainaiger.org A +short
dig mainaiger.org AAAA +short
dig www.mainaiger.org CNAME +short
dig mainaiger.org MX +short
dig mainaiger.org TXT +short
```

Compare the website addresses with the table and the mail records with the saved pre-change values. Check that the apex and `www` resolve to the intended site. DNS propagation can take time; do not repeatedly replace correct records because a cache is stale.

In repository Pages settings, wait for a successful DNS check and certificate availability, then select **Enforce HTTPS**. GitHub requests a certificate after the DNS check succeeds. [HTTPS documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/securing-your-github-pages-site-with-https)

```bash
curl -I https://mainaiger.org/
curl -I http://mainaiger.org/
curl -I https://www.mainaiger.org/
```

Open `https://mainaiger.org/` in a normal browser and verify:

- The page is the intended MainAIger prototype and HTTPS has no certificate error.
- HTTP redirects to HTTPS; `www` reaches the configured canonical website.
- Styles, scripts, images, and download links load from both a fresh visit and a refresh.
- Notebook/Graph switching preserves cell content and the animation can be replayed.
- Canvas controls and mobile navigation remain usable.
- Simulated runtime actions cannot be confused with real execution, login, installation, or device connection.

If the project URL works but the custom domain fails, inspect domain settings, authoritative DNS, and certificate state. If both fail, inspect the Actions run and uploaded `site/` entry point first. If HTML loads without assets, check relative asset paths; the project URL includes `/mainaiger-web/` before a custom domain is applied.

## 7. Publish changes and roll back

For routine edits, change files under `site/`, test locally, commit, and push `main`. The workflow deploys that commit. Keep experimental work on a separate branch until reviewed.

```bash
git diff
git add site docs .github
git commit -m "Update MainAIger website"
git push
```

To undo a bad site update, inspect `git log --oneline`, then revert the specific bad commit and push the new revert commit. This redeploys through the same workflow and preserves history. Do not use force-push as the default rollback. To undo a domain migration, restore the recorded previous website-routing settings while preserving email and verification records. If retiring Pages entirely, remove obsolete DNS routing as part of that change rather than leaving an unclaimed domain pointing at Pages.

## What this deployment does not provide

GitHub Pages serves static files; it does not host MainAIger accounts, native Python kernels, shell execution, tmux, or the local device controller. Authentication needs a separately implemented identity service or local-runtime flow. Keep runtime access private and authenticated; do not publish runtime tokens or device administration endpoints through this site. [GitHub Pages capability boundary](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)

## Action versions verified for this package

The workflow uses exact release tags observed in official action repositories on 25 October 2026. GitHub's general documentation examples can lag the action release pages. GitHub-hosted `ubuntu-latest` is used; a future self-hosted runner needs a separate compatibility check.

| Action | Version | Official release |
|---|---|---|
| Checkout | `v7.0.1` | https://github.com/actions/checkout/releases/tag/v7.0.1 |
| Configure Pages | `v6.0.0` | https://github.com/actions/configure-pages/releases/tag/v6.0.0 |
| Upload Pages artifact | `v5.0.0` | https://github.com/actions/upload-pages-artifact/releases/tag/v5.0.0 |
| Deploy Pages | `v5.0.1` | https://github.com/actions/deploy-pages/releases/tag/v5.0.1 |

Workflow structure reference: https://github.com/actions/starter-workflows/blob/main/pages/static.yml
