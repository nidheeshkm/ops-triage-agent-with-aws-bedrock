"""Runbook tools for ops_agent: read one runbook whole from S3, with metadata and gap checks."""
import io  # wraps the downloaded bytes so pypdf can read them like a file
import re  # pulls the title, default severity and review date out of the runbook text
from datetime import date, datetime, timezone  # age check against today's date in UTC

import boto3  # AWS SDK; reads the PDF from S3
from botocore.exceptions import BotoCoreError, ClientError  # AWS and network errors
from pypdf import PdfReader  # extracts text from the PDF, page by page in order
from strands import tool  # turns functions into tools ops_agent can call (Step 4c)

from config import settings  # RUNBOOK_BUCKET from .env, runbook policy from YAML

_RB_CONFIG = settings.environment_config["runbooks"]  # max_age_days, required_sections, s3_prefix

_session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # your profile locally; ops_agent's IAM role on AgentCore
_s3 = _session.client("s3")  # S3 client; used by the loader in Step 4b

_ID_PATTERN = re.compile(r"RB-\d{3}")  # runbook IDs look like RB-003

_HEADINGS = ("Overview", "Alert triggers", "Customer and business impact", "First 5 minutes",
             "Diagnosis", "Mitigation", "Escalation", "Verify recovery", "After the incident")  # every heading a runbook may use
_FOOTER = re.compile(r"^(RB-\d{3} \|.*|Page \d+)$")  # page footer and page-number lines that pypdf mixes into the text


def _sections(text: str) -> dict[str, list[str]]:
    """Split runbook text into {heading: [items]}, rebuilding numbered steps and bullets exactly as written."""
    out, current, items = {}, None, []
    for raw in text.splitlines():
        line = raw.strip()
        if line in _HEADINGS:  # a new section starts
            if current:
                out[current] = items
            current, items = line, []
            continue
        if current is None or _FOOTER.match(line):  # text before the first heading, or a page footer: skip
            continue
        if line.startswith("Note:"):  # the closing note ends the last section
            out[current], current = items, None
            continue
        if re.fullmatch(r"\d+", line):  # pypdf puts each step number on its own line
            items.append(f"{line}.")
        elif not line or (len(line) == 1 and not line.isalnum()):  # a bullet glyph comes out as an empty or 1-character line
            items.append("•")
        elif items and (items[-1] == "•" or re.fullmatch(r"\d+\.", items[-1])):  # first text after a number or bullet
            items[-1] = f"{items[-1]} {line}"
        elif items and re.match(r"(\d+\.|•) ", items[-1]):  # a wrapped continuation of the same step or bullet
            items[-1] = f"{items[-1]} {line}"
        else:  # plain paragraph or table text
            items.append(line)
    if current:
        out[current] = items
    return {name: [i for i in found if i != "•"] for name, found in out.items()}  # drop bullet markers left with no text


def _load_runbook(runbook_id: str, max_age_days: int | None = None) -> dict:
    """Read one runbook PDF from S3; return its full text plus metadata and gap checks."""
    if not _ID_PATTERN.fullmatch(runbook_id):  # accept exactly RB-NNN, nothing that could widen the S3 prefix
        raise ValueError(f"Invalid runbook id {runbook_id!r}; expected RB- and three digits")
    prefix = f"{_RB_CONFIG['s3_prefix']}{runbook_id}-"  # e.g. runbooks/RB-003-
    listing = _s3.list_objects_v2(Bucket=settings.RUNBOOK_BUCKET, Prefix=prefix)  # find the file by its ID
    keys = [obj["Key"] for obj in listing.get("Contents", []) if obj["Key"].endswith(".pdf")]
    if not keys:
        raise ValueError(f"No runbook file found for {runbook_id}")
    body = _s3.get_object(Bucket=settings.RUNBOOK_BUCKET, Key=keys[0])["Body"].read()  # the PDF bytes
    reader = PdfReader(io.BytesIO(body))  # open the bytes as a PDF
    text = "\n".join(page.extract_text() or "" for page in reader.pages)  # all pages, in order

    lines = [line.strip() for line in text.splitlines() if line.strip()]  # non-empty lines; the first is the title
    severity = re.search(r"Default severity\s+(SEV\d)", text)  # header field, e.g. SEV2
    reviewed = re.search(r"Last reviewed\s+(\d{4}-\d{2}-\d{2})", text)  # header field, e.g. 2026-11-14
    age_days = None
    if reviewed:
        age_days = (datetime.now(timezone.utc).date() - date.fromisoformat(reviewed.group(1))).days  # days since review, UTC
    limit = _RB_CONFIG["max_age_days"] if max_age_days is None else max_age_days  # override only for testing
    missing = [  # required headings that don't appear as a line of their own
        name for name in _RB_CONFIG["required_sections"]
        if not re.search(rf"^\s*{re.escape(name)}\s*$", text, re.M)
    ]
    marker = f"Runbook {runbook_id}"  # header line printed under the title, e.g. "Runbook RB-003"
    title_index = next((i for i, line in enumerate(lines) if line == marker), 0)  # 0 if the marker isn't found
    if title_index > 0:
        title = lines[title_index - 1]  # the title sits on the line just above the marker
    else:
        title = keys[0].rsplit("/", 1)[-1][len(runbook_id) + 1:-4].replace("-", " ")  # fallback: from the file name
    return {
        "runbook_id": runbook_id,
        "title": title,
        "file": keys[0],
        "default_severity": severity.group(1) if severity else None,
        "last_reviewed": reviewed.group(1) if reviewed else None,
        "age_days": age_days,
        "outdated": age_days is None or age_days > limit,  # no review date at all also counts as outdated
        "missing_sections": missing,
        "text": text,
        "sections": _sections(text),  # {heading: [steps]} built by code, so no step can be dropped
    }

def _summary(rb: dict) -> str:
    """Compact fact block shared by both tools, so the model always sees the same wording."""
    age = f"{rb['age_days']} days ago" if rb["age_days"] is not None else "age unknown"
    status = "OUTDATED" if rb["outdated"] else "current"
    return (
        f"Runbook: {rb['runbook_id']} - {rb['title']}\n"
        f"Default severity: {rb['default_severity'] or 'not stated'}\n"
        f"Last reviewed: {rb['last_reviewed'] or 'not stated'} ({age}; {status}; limit {_RB_CONFIG['max_age_days']} days)\n"
        f"Missing required sections: {', '.join(rb['missing_sections']) or 'none'}"
    )


@tool
def get_runbook_info(runbook_id: str) -> str:
    """Get a runbook's title, default severity, review date and gap checks, WITHOUT its body text.

    Call this during normal alert triage, after search_runbooks has identified the runbook,
    to learn its default severity. It is cheap: only a few lines come back.

    Args:
        runbook_id: Runbook ID such as RB-003.
    """
    try:
        return _summary(_load_runbook(runbook_id.strip().upper()))  # accept "rb-003 " as RB-003
    except (ValueError, ClientError, BotoCoreError) as err:  # bad ID, missing file, AWS or network error
        return f"Runbook lookup failed: {err}"


@tool
def get_runbook(runbook_id: str) -> str:
    """Get the FULL text of one runbook, plus its title, default severity, review date and gap checks.

    Call this ONLY when asked for next steps or detailed instructions for an alert.
    It returns the whole runbook, so it uses far more tokens than get_runbook_info.

    Args:
        runbook_id: Runbook ID such as RB-003.
    """
    try:
        rb = _load_runbook(runbook_id.strip().upper())
    except (ValueError, ClientError, BotoCoreError) as err:
        return f"Runbook lookup failed: {err}"
    return _summary(rb) + "\n\nFULL RUNBOOK TEXT:\n" + rb["text"]  # facts first, then the runbook in page order