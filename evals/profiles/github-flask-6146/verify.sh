python - <<'PY'
from pathlib import Path
import re
import sys
import unittest

try:
    text = Path("docs/deploying/index.rst").read_text(encoding="utf-8")
except (OSError, UnicodeError) as exc:
    print(f"Verification setup error: {exc}", file=sys.stderr)
    raise SystemExit(2)

url = "https://developers.cloudflare.com/workers/languages/python/packages/flask/"
heading = re.search(r"(?m)^Hosting Platforms\n-+\s*$", text)
hosting = text[heading.end():] if heading else ""
next_heading = re.search(r"(?m)^\S[^\n]*\n[-=~^]{3,}\s*$", hosting)
if next_heading:
    hosting = hosting[:next_heading.start()]
links = re.findall(r"(?m)^-\s+`([^`<>]+?)\s*<(https://[^>\s]+)>`_{1,2}", hosting)


class HostingDocumentation(unittest.TestCase):
    def test_cloudflare_flask_guide_in_hosting_platforms(self):
        self.assertIsNotNone(heading, "Hosting Platforms section is missing")
        matches = [(label, target) for label, target in links if target.rstrip("/") == url.rstrip("/")]
        self.assertEqual(len(matches), 1, "Add one inline bullet link to the Cloudflare Flask guide under Hosting Platforms")
        self.assertIn("cloudflare", matches[0][0].casefold(), "The link label must identify Cloudflare")

    def test_existing_hosting_platforms_remain(self):
        expected = {
            "https://help.pythonanywhere.com/pages/Flask/",
            "https://cloud.google.com/appengine/docs/standard/python3/building-app",
            "https://cloud.google.com/run/docs/quickstarts/build-and-deploy/deploy-python-service",
            "https://docs.aws.amazon.com/elasticbeanstalk/latest/dg/create-deploy-python-flask.html",
            "https://docs.microsoft.com/en-us/azure/app-service/quickstart-python",
        }
        actual = {target for _, target in links}
        self.assertFalse(expected - actual, f"Existing platform links removed: {sorted(expected - actual)}")

    def test_self_hosted_options_remain(self):
        prefix = text[:heading.start()] if heading else ""
        self.assertIn("Self-Hosted Options", prefix)
        for page in ("gunicorn", "waitress", "mod_wsgi", "uwsgi", "gevent", "asgi", "proxy_fix", "nginx", "apache-httpd"):
            self.assertRegex(prefix, rf"(?m)^\s+{re.escape(page)}\s*$", f"Existing self-hosted page removed: {page}")


unittest.main(verbosity=2)
PY
