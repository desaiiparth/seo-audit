# Python SEO Audit Agent (with GSC + URL Inspection + PageSpeed)

Project rules live in `AGENTS.md` at the repository root.

This app audits every URL in your sitemap and combines:
- Crawl data
- Google Search Console data
- URL Inspection evidence
- Google PageSpeed Insights metrics
- AI evidence-based analysis (OpenAI API)

## What it collects per URL

- SEO crawl data: title, meta description, H1, H2s, canonical, robots meta, status code, word count, internal links
- Extended crawl data: indexability, external links, structured data hints, images missing alt text
- Search Console: clicks, impressions, CTR, average position, top queries
- URL Inspection: indexed status, coverage/indexing state, Google-selected canonical, user-declared canonical, robots/indexing state, last crawl time
- PageSpeed: performance score, LCP, INP, CLS, mobile issues, desktop issues, opportunities/diagnostics

## Step-by-step setup (non-technical)

### 1) Create a Google Cloud project
1. Open https://console.cloud.google.com/
2. Click **Select project** > **New Project**.
3. Create the project.

### 2) Enable APIs
In **APIs & Services > Library**, enable:
1. **Google Search Console API**
2. **Google Search Console URL Inspection API**
3. **PageSpeed Insights API**

### 3) Create service account JSON (for Search Console + URL Inspection)
1. Go to **APIs & Services > Credentials**.
2. Click **Create credentials** > **Service account**.
3. Open the service account > **Keys** > **Add key** > **Create new key** > **JSON**.
4. Download the JSON file.

### 4) Give this service account access in Search Console
1. Open https://search.google.com/search-console
2. Select your property.
3. Go to **Settings > Users and permissions**.
4. Add the service account email from the JSON as a user.


### OAuth login setup (alternative to service account)
1. Go to **Google Cloud Console**: https://console.cloud.google.com/
2. Open **APIs & Services > Credentials**.
3. Click **Create credentials** > **OAuth client ID**.
4. Choose **Desktop app**.
5. Download the JSON file.
6. Rename it to `oauth_client.json`.
7. Upload it to your Codespaces/project root.
8. Run the script with `--oauth-client-file oauth_client.json`.
9. Complete the browser login flow; the script stores your token in `token.json` for reuse.

### Codespaces-friendly manual OAuth (`--oauth-manual`)
Use this when automatic localhost callback handling is not available in Codespaces/remote terminals.

1. Run with both `--oauth-client-file` and `--oauth-manual`.
2. The script prints the Google authorization URL.
3. Open that URL manually in your browser and approve access.
4. Google redirects to a localhost URL in your browser.
5. Copy the **full localhost URL** from the browser address bar.
6. Paste that full URL back into the terminal when prompted.
7. The script extracts the authorization code, exchanges it for tokens, and saves `token.json`.
8. Future runs automatically reuse `token.json` until refresh/re-auth is needed.

### 5) Create an API key (for PageSpeed)
1. In Google Cloud, go to **APIs & Services > Credentials**.
2. Click **Create credentials** > **API key**.
3. Copy this key (used with `--pagespeed-api-key`).

### 6) Place credentials file in this project
- Put your service account JSON in this folder (example name: `gsc_credentials.json`).

### 7) Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 seo_audit.py \
  --sitemap "https://yourdomain.com/sitemap.xml" \
  --site-url "https://yourdomain.com/" \
  --credentials-file "gsc_credentials.json" \
  --pagespeed-api-key "YOUR_PAGESPEED_API_KEY" \
  --openai-api-key "YOUR_OPENAI_API_KEY" \
  --openai-model "gpt-4.1" \
  --inspection-limit 100 \
  --output "reports"
```

OAuth example command:

```bash
python3 seo_audit.py \
  --sitemap "https://elitewebsolutions.co/sitemap.xml" \
  --site-url "https://elitewebsolutions.co/" \
  --oauth-client-file "oauth_client.json" \
  --pagespeed-api-key "$PAGESPEED_API_KEY" \
  --pagespeed-limit 5 \
  --inspection-limit 43 \
  --output "reports"
```

This creates the folder automatically (if missing) and writes:
- `reports/seo_audit_results_YYYY-MM-DD_HHMM.csv`
- `reports/seo_audit_report_YYYY-MM-DD_HHMM.md`

Timestamped filenames prevent scheduled or repeated audits from overwriting older exports.

## Safe basic test run (no API keys)

```bash
python3 seo_audit.py \
  --sitemap "https://yourdomain.com/sitemap.xml" \
  --output "reports"
```

With this command, crawl + on-page extraction still runs, while Google Search Console, URL Inspection, PageSpeed, and AI analysis are skipped.

## CSV output columns
Includes all crawl + GSC + inspection fields, plus PageSpeed fields:
- `performance_score`
- `lcp`
- `inp`
- `cls`
- `mobile_performance_issues` (explicit mobile CWV/performance problems)
- `desktop_performance_issues` (explicit desktop CWV/performance problems)
- `opportunities_diagnostics`
- `indexability`
- `external_links_count`
- `external_links`
- `structured_data`
- `images_missing_alt_count`
- `images_missing_alt`

## Notes
- If URL Inspection quota is smaller than your URL count, the script inspects top-priority URLs first.
- If `--pagespeed-api-key` is missing, PageSpeed data is marked as unavailable.
- If `--openai-api-key` is set, the report includes an AI analysis section with evidence-tied recommendations and `unconfirmed` labels where proof is missing.
- AI section is asked to prioritize each action by **impact** and **effort** and keep recommendations understandable for non-technical owners.
