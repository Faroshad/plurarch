# Supabase setup (one time, about 15 minutes)

This sets up the internet backend for Plurarch: the database, the access rules, the
facilitator login and the keys. You need a web browser and a password manager. Nothing is
installed on your computer.

Dashboard wording changes from time to time. Where a button or page may have another name,
the other name is given in brackets.

---

## 1. Create the project

1. Go to <https://supabase.com/dashboard> and sign in (for example **Continue with GitHub**).
2. If you are asked to create an **organization**, name it (e.g. `Plurarch`), choose type
   **Personal** and plan **Free**, then click **Create organization**.
3. Click **New project**.
4. Fill in the form:
   - **Name**: `plurarch`
   - **Database password**: click **Generate a password**, then **Copy**, and save it in your
     password manager now as "Supabase plurarch DB password". Plurarch never needs it, but
     you cannot see it again later.
   - **Region**: choose **East US (North Virginia)** (`us-east-1`), the closest to Texas.
     **East US (Ohio)** is also fine. (Newer dashboards may first ask for a broad area:
     pick **Americas**, then the specific region if offered.)
   - If a **Security options** or **Advanced configuration** section is shown, keep the
     defaults: **Data API + Connection String** and **Use public schema for Data API**.
     Do **not** choose "Only Connection String": the web app needs the Data API.
5. Click **Create new project** and wait until the status stops saying **Setting up project**
   (1 to 3 minutes). The project home page then shows **Project Status: Healthy** or similar.

## 2. Create the tables and access rules

1. In the left sidebar, click **SQL Editor** (the `>_` icon).
2. Click **+ New query** (or **New SQL snippet**, or the **+** next to the snippet list).
3. Open `supabase/schema.sql` from this repository in VS Code, select everything
   (Ctrl+A), copy it (Ctrl+C), and paste it into the editor (Ctrl+V).
4. Click **Run** (bottom right; or press Ctrl+Enter).
   - If a dialog warns about **destructive operations** (it sees `DROP POLICY IF EXISTS` /
     `DROP VIEW IF EXISTS`), click **Run this query** (or **Confirm**). That is expected.
   - The result should be **Success. No rows returned**. If you get an error instead, nothing
     was changed (the script runs in one transaction). Copy the error text and fix or report it.
5. Optional check: click **Table Editor** in the sidebar. You should see the tables
   `agent_events`, `decisions`, `facilitators`, `questions`, `rounds`, `sessions`, `votes`,
   and none should carry an **RLS disabled** / **Unrestricted** warning.

You can run `schema.sql` again at any time (for example after an update). It is written to
be re-runnable.

Note: **Advisors → Security Advisor** will list **Security Definer View: public.session_status**.
That is intentional: the public status view must show round numbers and decision ids to
anonymous phones without giving them access to the tables behind it (explained in
`schema.sql`, section 6). Other warnings about Plurarch objects are worth a look.

## 3. Turn off public sign-ups

1. In the sidebar, click **Authentication**.
2. Open **Sign In / Providers** (older dashboards: **Providers**, and the sign-up switch sits
   in **Authentication → Settings** or inside the **Email** provider).
3. Under **User Signups**:
   - **Allow new users to sign up**: switch **OFF**.
   - **Allow anonymous sign-ins**: leave **OFF**. (Participants do not sign in at all; the
     public key is enough.)
   - **Confirm email**: either setting works. The facilitator account is created already
     confirmed in step 4. You may switch it off to keep things simple.
4. Under **Auth Providers**, make sure **Email** is **Enabled** (it is by default).
5. Click **Save changes** (each section has its own save button; check each one you changed).

## 4. Create the facilitator account

1. **Authentication → Users**.
2. Click **Add user** → **Create new user**.
3. Email: `YOUR-EMAIL@example.edu`. Password: generate a strong one in your password manager
   and save it there as "Plurarch console".
4. Tick **Auto Confirm User?** and click **Create user**.

## 5. Put the account on the facilitator allowlist

1. **SQL Editor → + New query**.
2. Paste and **Run**:

   ```sql
   insert into public.facilitators (email) values ('YOUR-EMAIL@example.edu') on conflict do nothing;
   ```

3. Optional check: `select * from public.facilitators;` shows one row.

Only accounts listed in `facilitators` can read votes and agent events and open or close
rounds. Any other signed-in account gets no more access than an anonymous phone.

## 6. (Optional) A non-facilitator test account

`tests/test_rls.py` can prove that a signed-in account which is *not* a facilitator cannot
read votes (the second access test in the spec). Sign-ups are off, so create the account
yourself:

1. **Authentication → Users → Add user → Create new user**.
2. Any e-mail you control that is not the facilitator's, e.g. `plurarch-test@example.com`,
   and a password. Tick **Auto Confirm User?**, then **Create user**.
3. Do **not** add it to `facilitators`.
4. Put both into `.env` (step 8) as `TEST_NON_FACILITATOR_EMAIL` and
   `TEST_NON_FACILITATOR_PASSWORD`.

## 7. Site URL

1. **Authentication → URL Configuration**.
2. **Site URL**: `https://faroshad.github.io/plurarch/` → **Save**.
3. Under **Redirect URLs**, click **Add URL** and add `https://faroshad.github.io/plurarch/**`
   → **Save URLs**. (Password sign-in does not redirect, but a password-reset e-mail would.)

## 8. Copy the URL and keys

1. Click **Connect** at the top of the project page (or go to **Project Settings → Data API**):
   copy the **Project URL**. It looks like `https://abcdefghijklmnop.supabase.co`.
2. **Project Settings → API Keys** (older dashboards: **Project Settings → API**). There are
   two kinds of keys, and either kind works with Plurarch:

   | Use | New keys (recommended) | Legacy keys (tab **Legacy API Keys**) |
   |---|---|---|
   | Public, goes into the website | **Publishable key** `sb_publishable_…` | **anon** `public` (a long `eyJ…` JWT) |
   | Secret, only on your laptop | **Secret key** `sb_secret_…` (click **Reveal**, or **+ New secret key** if none exists) | **service_role** `secret` (`eyJ…`) |

   Use one kind for both keys. Supabase is phasing out the legacy keys by the end of 2026,
   so prefer the new ones if the dashboard offers them. Both work with supabase-js v2 (the
   site pins 2.117.2) and with PostgREST. The orchestrator sends new-style keys only in the
   `apikey` header, as Supabase requires, and legacy keys also as `Authorization: Bearer`.

3. **Website**: edit `site/config.js`:

   ```js
   window.PLURARCH_CONFIG = {
     backend: "supabase",
     supabaseUrl: "https://abcdefghijklmnop.supabase.co",
     supabaseAnonKey: "sb_publishable_...",   // or the legacy anon key
     useCase: "live_presentation"
   };
   ```

   This key is public by design: every phone receives it. Security comes from the rules in
   `schema.sql`.

4. **Laptop**: copy `.env.example` to `.env` in the project root (same folder as `SPEC.md`)
   and fill in:

   ```ini
   SUPABASE_URL=https://abcdefghijklmnop.supabase.co
   SUPABASE_ANON_KEY=sb_publishable_...
   SUPABASE_SERVICE_ROLE_KEY=sb_secret_...
   # optional, for tests/test_rls.py
   TEST_NON_FACILITATOR_EMAIL=plurarch-test@example.com
   TEST_NON_FACILITATOR_PASSWORD=...
   TEST_FACILITATOR_EMAIL=YOUR-EMAIL@example.edu
   TEST_FACILITATOR_PASSWORD=...
   ```

   **Never commit `.env`** (it is in `.gitignore`) and never put the secret / service_role
   key into `site/` or anything that is pushed. It bypasses every access rule. The deploy
   workflow refuses to publish a file that contains one. If it ever leaks, go to
   **Project Settings → API Keys**, create a new secret key and delete the old one (for
   legacy keys: roll the JWT secret).

## 9. Test the rules

From the project root in a terminal:

```powershell
.venv\Scripts\python.exe tests\test_rls.py
```

Every line should say `PASS` (the facilitator and non-facilitator checks say `SKIP` until you
add their credentials to `.env`). The script creates a temporary session, tests reading and
writing as an anonymous phone, as a signed-in non-facilitator and as the facilitator, and
deletes everything it created. Do not run it during a live session (it refuses if another
session is active, unless you pass `--force`).

## 10. GitHub Pages (one time)

In the GitHub repository: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
Every push to `main` then publishes `site/` (plus `config/`, without `config/local.json`) to
`https://faroshad.github.io/plurarch/` through `.github/workflows/deploy.yml`. You can also run
it by hand: **Actions → Deploy site to GitHub Pages → Run workflow**.

---

## Free-plan limits that matter for a live session

- **Pausing.** A free project is paused after about **7 days without activity**. A paused
  project answers with HTTP **540**, or its `…supabase.co` address stops resolving, and every
  phone would show "reconnecting". To restore it: open the dashboard, open the project, click
  **Restore project**, and wait until it is healthy again (usually a few minutes). Paused
  projects can be restored from the dashboard for about 90 days. **Before a session:** open
  the dashboard the day before, and run `orchestrator.py health-check` on the day. It
  reports "paused" explicitly.
- **Realtime: 200 concurrent connections.** Only the facilitator console (and optionally the
  orchestrator) use realtime; phones only poll the tiny `session_status` view, so a few
  hundred participants stay far below the limit.
- **Max rows: 1000 per request** (Project Settings → Data API → Max rows). The orchestrator
  and the console page through votes, so leave it at the default.
- **Two free projects** per account; a paused one still counts.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `schema.sql` fails with `relation "auth.users" does not exist` | You are not in a Supabase project's SQL editor. |
| Console login says "not a facilitator" | Step 5 was skipped, or the e-mail differs. Run `select * from public.facilitators;`. |
| Phones say "reconnecting" and `health-check` says `paused` | Restore the project (see above). |
| `health-check` or the test says `auth` / HTTP 401 | Wrong key in `.env` or `site/config.js` (e.g. publishable and secret swapped, or keys from another project). |
| `http_404 … schema cache` | `schema.sql` has not been run in this project, or the API has not reloaded yet. Wait 10 s and retry. |
