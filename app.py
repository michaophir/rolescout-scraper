#!/usr/bin/env python3
"""
Simple Flask web UI for the Company Jobs Consolidator.
Run:  python app.py
Then open http://localhost:5000
"""

from flask import Flask, request, render_template_string

from jobs import Job, fetch_jobs

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Sample demo data (mirrors jobs.py --demo)
# ---------------------------------------------------------------------------

DEMO_DATA: dict[str, tuple[list[Job], str]] = {
    "Stripe": ([
        Job("Software Engineer, Payments", "Stripe", "San Francisco, CA", "https://stripe.com/jobs/1", "Payments", "Full-time"),
        Job("Staff Engineer, Infrastructure", "Stripe", "Remote", "https://stripe.com/jobs/2", "Infrastructure", "Full-time"),
        Job("Product Manager, Revenue & Finance", "Stripe", "New York, NY", "https://stripe.com/jobs/3", "Product", "Full-time"),
    ], "Greenhouse"),
    "Notion": ([
        Job("Senior Frontend Engineer", "Notion", "San Francisco, CA", "https://notion.so/jobs/1", "Engineering", "Full-time"),
        Job("Data Scientist", "Notion", "Remote", "https://notion.so/jobs/2", "Data", "Full-time"),
        Job("Technical Recruiter", "Notion", "New York, NY", "https://notion.so/jobs/3", "People", "Full-time"),
        Job("Enterprise Account Executive", "Notion", "Austin, TX", "https://notion.so/jobs/4", "Sales", "Full-time"),
    ], "Greenhouse"),
    "Linear": ([
        Job("Backend Engineer", "Linear", "Remote", "https://linear.app/jobs/1", "Engineering", "Full-time"),
        Job("Designer", "Linear", "Remote", "https://linear.app/jobs/2", "Design", "Full-time"),
    ], "Ashby"),
}

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Company Jobs Consolidator</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f5f5f7;
      color: #1d1d1f;
      min-height: 100vh;
      padding: 2rem 1rem;
    }

    header {
      text-align: center;
      margin-bottom: 2rem;
    }
    header h1 { font-size: 1.8rem; font-weight: 700; }
    header p  { color: #555; margin-top: .4rem; font-size: .95rem; }

    .card {
      background: #fff;
      border-radius: 12px;
      box-shadow: 0 2px 12px rgba(0,0,0,.08);
      padding: 1.5rem;
      max-width: 860px;
      margin: 0 auto 2rem;
    }

    form { display: flex; flex-direction: column; gap: 1rem; }

    label { font-weight: 600; font-size: .9rem; }

    input[type="text"] {
      width: 100%;
      padding: .65rem .9rem;
      border: 1.5px solid #d1d1d6;
      border-radius: 8px;
      font-size: 1rem;
      transition: border-color .15s;
    }
    input[type="text"]:focus { outline: none; border-color: #0071e3; }

    .row { display: flex; align-items: center; gap: .6rem; }

    input[type="checkbox"] { width: 1.1rem; height: 1.1rem; accent-color: #0071e3; cursor: pointer; }

    button {
      align-self: flex-start;
      background: #0071e3;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: .6rem 1.4rem;
      font-size: .95rem;
      font-weight: 600;
      cursor: pointer;
      transition: background .15s;
    }
    button:hover { background: #0077ed; }
    button:active { background: #005bb5; }

    /* Summary bar */
    .summary {
      max-width: 860px;
      margin: 0 auto 1rem;
      font-size: .9rem;
      color: #555;
    }
    .summary strong { color: #1d1d1f; }

    /* Results table */
    .table-wrap {
      max-width: 860px;
      margin: 0 auto;
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 2px 12px rgba(0,0,0,.08);
      font-size: .88rem;
    }

    thead th {
      background: #f0f0f5;
      padding: .7rem 1rem;
      text-align: left;
      font-weight: 600;
      white-space: nowrap;
    }

    tbody tr:nth-child(even) { background: #fafafa; }
    tbody tr:hover           { background: #eef4fd; }

    td { padding: .65rem 1rem; vertical-align: top; }

    td.company { font-weight: 600; white-space: nowrap; }

    td.title a {
      color: #0071e3;
      text-decoration: none;
      font-weight: 500;
    }
    td.title a:hover { text-decoration: underline; }

    .badge {
      display: inline-block;
      background: #e8f0fe;
      color: #1a56db;
      border-radius: 4px;
      padding: .1rem .45rem;
      font-size: .78rem;
      white-space: nowrap;
    }

    .empty { text-align: center; color: #888; padding: 2rem; }

    .demo-banner {
      max-width: 860px;
      margin: 0 auto 1rem;
      background: #fff8e1;
      border: 1px solid #ffe082;
      border-radius: 8px;
      padding: .6rem 1rem;
      font-size: .88rem;
      color: #795548;
    }
  </style>
</head>
<body>

<header>
  <h1>Company Jobs Consolidator</h1>
  <p>Search open positions across Greenhouse, Lever &amp; Ashby job boards</p>
</header>

<div class="card">
  <form method="post" action="/">
    <label for="companies">Companies (comma-separated)</label>
    <input
      type="text"
      id="companies"
      name="companies"
      placeholder="e.g. Stripe, Notion, Linear"
      value="{{ query }}"
      autofocus
    >
    <div class="row">
      <input type="checkbox" id="demo" name="demo" {% if demo %}checked{% endif %}>
      <label for="demo" style="font-weight:400">Demo mode (sample data, no network)</label>
    </div>
    <button type="submit">Search</button>
  </form>
</div>

{% if searched %}
  {% if demo %}
    <div class="demo-banner">Demo mode — showing sample data, no real network calls made.</div>
  {% endif %}

  <div class="summary">
    Found <strong>{{ total }}</strong> position(s) across <strong>{{ results|length }}</strong> company/companies.
  </div>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Title</th>
          <th>Department</th>
          <th>Location</th>
          <th>Type</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {% for company, jobs, source in results %}
          {% if jobs %}
            {% for job in jobs %}
              <tr>
                <td class="company">{{ job.company }}</td>
                <td class="title">
                  {% if job.url %}
                    <a href="{{ job.url }}" target="_blank" rel="noopener">{{ job.title }}</a>
                  {% else %}
                    {{ job.title }}
                  {% endif %}
                </td>
                <td>{{ job.department or "" }}</td>
                <td>{{ job.location or "" }}</td>
                <td>{{ job.employment_type or "" }}</td>
                <td><span class="badge">{{ source }}</span></td>
              </tr>
            {% endfor %}
          {% else %}
            <tr>
              <td class="company">{{ company }}</td>
              <td colspan="5" style="color:#888;">No positions found ({{ source }})</td>
            </tr>
          {% endif %}
        {% endfor %}
        {% if total == 0 and results|length == 0 %}
          <tr><td colspan="6" class="empty">No results.</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>
{% endif %}

</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    searched = False
    query = ""
    demo = False
    results = []   # list of (company, jobs, source)
    total = 0

    if request.method == "POST":
        searched = True
        query = request.form.get("companies", "").strip()
        demo = "demo" in request.form

        # Parse comma-separated input
        raw = [c.strip() for c in query.split(",") if c.strip()]
        # Deduplicate preserving order
        seen: set[str] = set()
        companies: list[str] = []
        for c in raw:
            if c.lower() not in seen:
                seen.add(c.lower())
                companies.append(c)

        if demo:
            for company, (jobs, source) in DEMO_DATA.items():
                results.append((company, jobs, source))
                total += len(jobs)
        else:
            for company in companies:
                jobs, source = fetch_jobs(company)
                results.append((company, jobs, source))
                total += len(jobs)

    return render_template_string(
        TEMPLATE,
        searched=searched,
        query=query,
        demo=demo,
        results=results,
        total=total,
    )


if __name__ == "__main__":
    app.run(debug=True)
