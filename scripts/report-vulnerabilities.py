"""Report high-severity image scan results in CI and optionally in Mattermost."""
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def findings(report):
    return sorted({(item["VulnerabilityID"], item["PkgName"], item["InstalledVersion"], item.get("FixedVersion", ""))
                   for result in (report.get("Results") or ()) for item in (result.get("Vulnerabilities") or ())
                   if item["Severity"] in {"HIGH", "CRITICAL"}})


def report_image(source):
    title = f"Security scan for ghcr.io/yrmuq/oubi:{source.stem}"
    try:
        entries = findings(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        entries = None
        message = "The scanner did not produce a valid report."
    else:
        message = f"Found {len(entries)} HIGH or CRITICAL findings ({sum(bool(row[3]) for row in entries)} with a fix)."
    rows = [f"- {cve} · {name} {installed} · {fixed or 'No fix listed'}" for cve, name, installed, fixed in entries or ()]
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("\n".join([f"## {title}", "", message, "", *rows, "", ""]))
    return entries, "\n".join([title, message, *rows[:12]])


def main(sources):
    reports = [report_image(source) for source in sources]
    fixable = [(source.stem, *row) for source, (entries, _) in zip(sources, reports)
               for row in entries or () if row[3]]
    if fixable and all(entries is not None for entries, _ in reports):
        fingerprint = hashlib.sha256(json.dumps(sorted(fixable)).encode()).hexdigest()
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            print(f"fingerprint={fingerprint}", file=output)
    problems = [message for entries, message in reports if entries is None or entries]
    if problems:
        hook = os.environ.get("OUBI_SECURITY_WEBHOOK")
        if hook:
            try:
                if urlsplit(hook).scheme != "https":
                    raise ValueError("Expected an HTTPS webhook")
                run = f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
                body = "\n\n".join([*problems, f"[Full report]({run})"])
                request = Request(hook, json.dumps({"text": body}).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=20):
                    pass
            except (OSError, ValueError):
                print("Could not send the security scan notification", file=sys.stderr)
        print("See the security scan results in the job summary.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(list(map(Path, sys.argv[1:]))))
