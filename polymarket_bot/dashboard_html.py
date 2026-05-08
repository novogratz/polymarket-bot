"""HTML/CSS/JS template for the dashboard.

Extracted into its own module so dashboard.py can stay focused on HTTP
routing and data assembly. The actual UI is replaced in Task 7 — for
now this module exposes the existing template under the new name so
the refactor in Task 6 can land independently.
"""

# NOTE: real content shipped in Task 7. This is a tiny placeholder so
# the routing refactor can land independently.
HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Polymarket Bot Dashboard</title></head>
<body><pre id="out">loading…</pre>
<script>
async function refresh() {
  const r = await fetch('/api/state');
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""
