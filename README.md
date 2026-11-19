# crash-digest

A daily Firebase Crashlytics review for **Fiuu VT**, posted to a Microsoft Teams channel every morning at 09:00 Malaysia time.

- **The numbers come from the Firebase Crashlytics API** (`firebasecrashlytics.googleapis.com/v1alpha`, the same API the Firebase CLI and MCP server use). No BigQuery export needed.
- **The words come from Claude**: a short summary and "look at these first".
- **Claude only sees an allow-listed payload**: counts, app/Android versions, device models, exception class names, and the top 5 stack frames. It never sees user IDs, installation IDs, custom keys, logs, breadcrumbs, issue subtitles or exception messages. The API reader (`crashlytics_api.py`) copies only named fields out of each response, and the payload builder copies only named fields again and scrubs long digit runs, emails and UUIDs.

> ⚠️ `v1alpha` is not a documented public API. If Google changes it, the job fails with an error card and a non-zero exit. It won't post wrong numbers.

```
Cloud Scheduler (09:00 MYT)
   └─► Cloud Run job  python -m crash_digest --product vt
          1. Crashlytics API: appSummary, topIssues, then per shown issue:
             topVersions, topAndroidDevices, topOperatingSystems, one sample event
          2. build allow-listed payload
          3. Claude API → summary + up to 3 priorities (forced tool call, validated)
          4. Adaptive Card → Teams Workflows webhook (3 retries)
          5. write "posted" marker to GCS (no double posts)
```

## What the card shows

| Section | Source |
|---|---|
| Crashes, ANRs, users affected (vs previous 24h) | `appSummary` report, filtered to FATAL / ANR |
| Crash-free users % (vs previous 24h) | `appSummary` FATAL-only `crashFreeUsersPercentage` (matches the console card) |
| 🤖 Look at first | Claude (left out with a note if the AI fails) |
| 🆕 New issues | `topIssues`, Crashlytics' own `SIGNAL_FRESH` flag |
| 🔁 Regressed: closed, then came back | `topIssues`, Crashlytics' own `SIGNAL_REGRESSED` flag |
| 🔥 Top 5 by users affected | `topIssues` |
| ▶️ Google Play vitals (optional) | Play Developer Reporting API: user-perceived crash and ANR rates (day, change vs the day before, 28-day average with a ⚠️ over Play's 1.09% / 0.47% limits), Play users, top Play crash/ANR issues |

The Play section is on when `play_package_name` is set. It uses the same API calls as the
[play-console-mcp](https://github.com/OrellBuehler/play-console-mcp) server, ported to Python.
It differs from the Crashlytics numbers on purpose:
- **Its own day.** Play data is daily in US Pacific days and lags about a day, so the section shows Play's latest full day, not the 24h window.
- **All app versions.** Play's limits are judged app-wide.
- **Different users.** Play only counts users who share usage data with Google, so counts won't match Crashlytics.
- **Not sent to Claude.** The AI payload is Crashlytics-only. Play issue causes are cut to the exception class name; raw Play error reports are never fetched.
- **Fails soft.** If Play errors, the card still posts with a note, the job logs an ERROR (which trips the Cloud Monitoring alert), and the exit code stays 0 so a re-run doesn't double post.

Edge cases:
- **No data at all** → a warning card. It doesn't post a reassuring zero.
- **API error** (after 3 tries on 429/5xx) → an error card, and the job exits non-zero so Cloud Monitoring alerts you.
- **Teams post fails 3×** → the job exits non-zero and no marker is written, so a re-run will post.
- **AI fails, or returns issue IDs that aren't in the data** → those parts are dropped and the numbers still post.

---

## 1. Enable the Crashlytics API (once per project)

```bash
gcloud services enable firebasecrashlytics.googleapis.com --project <project-id>
```
Already done for VT (`razer-vt-bbdd6`).

## 2. Fill in `config.yaml`

All three values are in Firebase console → **Project settings → General**:
```yaml
products:
  vt:
    gcp_project: razer-vt-bbdd6                 # project ID
    project_number: "585083471171"              # project number
    app_id: "1:585083471171:android:58d17f1fd0165f48185bf6"   # under "Your apps"
```
Also set `settings.state_location` to the bucket you create in step 4.

### Optional: Google Play vitals section
1. Enable the API in the same project:
   ```bash
   gcloud services enable playdeveloperreporting.googleapis.com --project <project-id>
   ```
2. Play Console → **Users and permissions** → **Invite new users** → the `crash-digest` service account email → **App permissions** → add the app with only **View app information and download bulk reports (read-only)**. No account-level permissions. It can take up to 24h to start working.
3. Set `play_package_name` for the product in `config.yaml` (e.g. `com.example.app`).
4. For a local dry run, log in with the Play scope too (user logins ignore scopes asked for in code):
   ```bash
   gcloud auth application-default login \
     --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/playdeveloperreporting
   ```
   Your own Google account also needs access to the app in Play Console.

## 3. Dry run against real data (posts nothing, calls no AI)

```bash
pip install -r requirements-dev.txt
gcloud auth application-default login
PYTHONPATH=src python -m crash_digest --product vt --dry-run --out out/
# out/vt_payload.json is EXACTLY what Claude would receive. Read it.
# out/vt_card.json can be pasted into https://adaptivecards.io/designer to preview it.
```
Compare the numbers with the Crashlytics dashboard for the same 24h window.

## 4. GCP setup (once)

```bash
PROJECT=razer-vt-bbdd6
REGION=asia-southeast1
SA=crash-digest@$PROJECT.iam.gserviceaccount.com

gcloud iam service-accounts create crash-digest --project $PROJECT

# Read-only Crashlytics access, plus permission to bill API calls to this project
for role in roles/firebasecrashlytics.viewer roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role $role
done

# "Already posted" markers
gcloud storage buckets create gs://$PROJECT-crash-digest-state --project $PROJECT --location $REGION
gcloud storage buckets add-iam-policy-binding gs://$PROJECT-crash-digest-state \
  --member serviceAccount:$SA --role roles/storage.objectAdmin

# Secrets
printf %s "<anthropic api key>"  | gcloud secrets create anthropic-api-key   --project $PROJECT --data-file=-
printf %s "<teams workflow url>" | gcloud secrets create teams-webhook-vt    --project $PROJECT --data-file=-
for s in anthropic-api-key teams-webhook-vt; do
  gcloud secrets add-iam-policy-binding $s --project $PROJECT \
    --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
done
```

### Teams webhook (Workflows)
The old "Connectors → Incoming Webhook" was retired in May 2026, so use **Workflows** instead:
Teams → the target channel → **⋯ → Workflows → "Post to a channel when a webhook request is received"** → pick the team and channel → copy the URL.
- Make it in a **private test channel first** and run there for about a week.
- The flow runs under the account of whoever created it. If that person leaves, it stops. Use a shared/service account if your tenant allows it.
- Treat the URL as a secret: anyone who has it can post into the channel.

## 5. Deploy

```bash
gcloud run jobs deploy crash-digest --project $PROJECT --region $REGION \
  --source . \
  --service-account $SA \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest,TEAMS_WEBHOOK_URL_VT=teams-webhook-vt:latest \
  --args=--product,vt \
  --max-retries 0 --task-timeout 10m

# Manual run (also your demo button)
gcloud run jobs execute crash-digest --project $PROJECT --region $REGION

# Daily at 09:00 Malaysia time
gcloud run jobs add-iam-policy-binding crash-digest --project $PROJECT --region $REGION \
  --member serviceAccount:$SA --role roles/run.invoker
gcloud scheduler jobs create http crash-digest-daily --project $PROJECT --location $REGION \
  --schedule "0 9 * * *" --time-zone "Asia/Kuala_Lumpur" \
  --uri "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/crash-digest:run" \
  --http-method POST --oauth-service-account-email $SA
```

**Failure alert:** Cloud Monitoring → Alerting → create a policy on the log-based condition
`resource.type="cloud_run_job" AND resource.labels.job_name="crash-digest" AND severity>=ERROR` → notify your email.

## Alternative: run as a scheduled Claude routine (instead of Cloud Run)

A Claude Code cloud routine clones this repo daily at 09:00 MYT (`0 1 * * *` UTC) and:
1. `python -m crash_digest --product vt --dry-run --no-ai --out out/` fetches the data and writes `out/vt_payload.json` + `out/vt_results.json`
2. Claude reads **only** `out/vt_payload.json` and writes `out/triage.json` (`{summary, priorities}`), following `SYSTEM_PROMPT` in `ai.py`
3. `python -m crash_digest --product vt --fixture out/vt_results.json --triage-file out/triage.json` validates the triage (invented issue IDs dropped), builds the card from the saved numbers and posts it

No Anthropic API key is needed. The routine's cloud environment needs:
- env `GOOGLE_SERVICE_ACCOUNT_JSON`: the full JSON key of the `crash-digest` service account (step 4 roles + the marker bucket)
- env `TEAMS_WEBHOOK_URL_VT`: the Workflows URL
- network access to `pypi.org`, `files.pythonhosted.org`, `oauth2.googleapis.com`, `firebasecrashlytics.googleapis.com`, `storage.googleapis.com`, `playdeveloperreporting.googleapis.com` (if the Play section is on) and your Teams webhook host

Privacy note: in this mode Claude has a shell and the Google credential, so "Claude only sees the payload" is enforced by the routine prompt, not by code.

## Command reference

```bash
python -m crash_digest --product vt                    # real run
python -m crash_digest --product vt --dry-run          # print payload + card, post nothing
python -m crash_digest --product vt --dry-run --with-ai  # also call Claude (rehearsal)
python -m crash_digest --product vt --fixture tests/fixtures/sample_rows.json --dry-run --with-ai
python -m crash_digest --product vt --date 2026-11-18 --force   # re-post a specific day
python -m crash_digest --product vt --no-ai            # numbers only
python -m crash_digest --product vt --fixture out/vt_results.json --triage-file out/triage.json  # post saved numbers + Claude-written triage
```
(Run with `PYTHONPATH=src` locally. The container already sets it.)

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
Covers: the Play reader (latest full day from freshness, rates, paging, NON_FATAL dropped, messages cut from causes, non-Play links dropped), the Play card section and its error note, a Play failure still posting the Crashlytics card, the API reader's mapping (headline, crash-free %, new/regressed signals, root-cause exception, top frames), sensitive fields never copied out of API responses, the quota-project header, retry on 5xx but not on 403, the payload allow-list and scrubbing, invented issue IDs dropped, cards with and without AI, no-data and clean-day cards, no double post, a failed Teams post failing the job, and an API error posting an error card.

## Adding VT+ / CloudECR later
Enable the Crashlytics API in their projects (step 1), fill in their `products:` entries, set `enabled: true`, add a `TEAMS_WEBHOOK_URL_*` secret, and either deploy one job per product or drop `--args` to run all enabled products in one job. If the job runs in VT's project, grant the service account the two roles from step 4 on each other project too.

The previous BigQuery-based version is kept in `~/Downloads/crash-digest-bigquery-version/`.

## Demo outline (My AI Adoption Sharing, about 6 min)
1. Show yesterday's card in Teams.
2. `gcloud run jobs execute crash-digest` and watch the new card arrive. With `--force`, if today's has already posted.
3. Open `out/vt_payload.json`: "this is everything the AI is allowed to see" (rule #5).
4. Click a flagged issue, then dig into it with the Firebase Crashlytics MCP in Claude Code against the razervt repo.
