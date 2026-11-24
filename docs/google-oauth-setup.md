# Google OAuth Setup Guide for JARVIS

## Step 1: Create Google Cloud Project
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or select existing)
3. Enable these APIs:
   - Gmail API
   - Calendar API
   - Tasks API
4. Go to "APIs & Services" → "Credentials"
5. Create OAuth 2.0 Client ID (Desktop App)

## Step 2: Configure OAuth Consent Screen
1. Add application name: "JARVIS"
2. Add support email
3. Add authorized domains: `localhost`
4. Add redirect URIs: `http://localhost:8080/oauth2callback`
5. Add JavaScript origins: `http://localhost:8080`
6. Save and get your Client ID and Client Secret

## Step 3: Update google-oauth-client.json
Replace the placeholder values in `google-oauth-client.json` with your actual credentials:
```json
{
  "web": {
    "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
    "client_secret": "YOUR_CLIENT_SECRET",
    "redirect_uris": ["http://localhost:8080/oauth2callback"],
    "javascript_origins": ["http://localhost:8080"]
  }
}
```

## Step 4: Test the Flow
Run this to initiate OAuth:
```bash
python -c "import openjarvis.skills.google_daily as gd; print(gd.GoogleDailyConnector()._credentials())"
```
This will open a browser for authorization. Copy the authorization code back to the terminal.

## Step 5: Verify Integration
Run:
```bash
python -c "import openjarvis.skills.google_daily as gd; print(gd.GoogleDailyConnector().fetch_daily())"
```
You should see email, calendar, and tasks data.

## Current State (Working Without Google)
- SQLite and deadline tracking are fully functional
- Voice loop and wake word work
- Code assistant integration complete
- Proactive deadline alerts in digest (mock data)

## Next Steps
Once you have Google credentials:
1. Update `google-oauth-client.json`
2. Run the test command above
3. JARVIS will then fetch real email/calendar/tasks data for the morning digest