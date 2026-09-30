/*
 * Plurarch site configuration: static default for GitHub Pages (Supabase mode).
 *
 * The Supabase anon key is public by design: it ships to every browser. Security comes from
 * Row Level Security (RLS) in the database, not from hiding this key. Never put the service
 * role key here.
 *
 * In local mode the local server (orchestrator.py run) serves its own /config.js with
 * backend: "local" instead of this file, so nothing here needs to change for local sessions.
 */
window.PLURARCH_CONFIG = {
  backend: "supabase",
  supabaseUrl: "https://YOUR-PROJECT.supabase.co",
  supabaseAnonKey: "YOUR-ANON-KEY",
  useCase: "live_presentation"
};
