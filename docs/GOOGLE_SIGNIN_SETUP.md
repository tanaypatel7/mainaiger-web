# Enable Google sign-in

Google sign-in is prepared in the website, but remains disabled until the Google
Cloud OAuth client and Supabase provider are configured. Email-code sign-in stays
available. These instructions do not enable public account creation.

1. In [Google Cloud Console](https://console.cloud.google.com/), select or create
   the project used for MainAIger. Your personal Google account can administer
   the project; website users do not gain access to that account.
2. In **Google Auth Platform**, configure branding and an **External** audience.
   While the app is in testing, add the Google accounts you want to test under
   **Audience → Test users**. Use identity scopes only (`openid`, `email`,
   `profile`); no Gmail, Drive or repository permission is needed for sign-in.
3. Under **Clients**, create an OAuth client of type **Web application**. Add:
   - Authorized JavaScript origin: `https://mainaiger.org`
   - Authorized redirect URI:
     `https://vfkkexciaenmkodwqgbq.supabase.co/auth/v1/callback`
4. In Supabase, open project `mainaiger-auth` → **Authentication → Sign In / Providers → Google**.
   Enable Google, paste the Google **Client ID** and **Client secret** into the
   provider's private settings, and save. Do not put the secret in the website,
   GitHub, a chat or this document.
5. In Supabase **Authentication → URL Configuration**, retain the existing
   site URL and any working email redirects. Add these website callback URLs to
   the redirect allowlist:
   - `https://mainaiger.org/`
   - `https://mainaiger.org/index.html`
   - `https://mainaiger.org/directory/`
   - `https://mainaiger.org/directory/index.html`
6. Keep the existing invite-only policy. Ensure each test account is already
   invited/registered; Google sign-in with an unregistered identity can fail
   while signup is disabled. A verified Google email matching an existing
   account may be linked by Supabase; verify the resulting identity in your
   actual project before relying on this behavior.
7. After the provider is configured, change `googleEnabled:false` to
   `googleEnabled:true` in `site/auth-config.js`, commit and deploy. This flag
   controls the buttons only; it does not configure Google or Supabase.
8. Test with an invited account from the homepage and the directory, including
   returning from Google, reloading, moving between pages in the same tab, and
   signing out. Confirm the Google account shown is the intended account.

The website uses an authorization code with PKCE and verifies the resulting
Supabase identity before treating the visitor as signed in. Its tab-scoped
session stores the access token temporarily in browser session storage and ends
on token expiry or tab closure. A new tab or browser restart may require another
login. Google sign-in has unit tests but was not live-tested while the provider
was disabled. Website login alone does not grant access to any user's device.
