"""Phase-gated resume tailoring CLI.

Flow:
  0. Select JD from jds/
  1. Gap Analysis     — LLM maps JD requirements to content bank
  2. Clarification    — answer LLM questions about gaps
  3. Tailoring        — LLM produces a tailored highlight selection + profile
  4. Apply + Render
  5. Recruiter Review — LLM simulates a recruiter scanning the rendered CV
  6. Revision         — LLM revises tailoring based on recruiter feedback (optional)
"""

import base64
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from anthropic import Anthropic
from pypdf import PdfReader

from bank import ContentBank, HighlightEntry
from settings import Settings

MODEL = "claude-sonnet-4-6"
JDS_DIR = Path("jds")
TAILORED_DIR = Path("tailored")
LOGS_DIR = Path("logs")

_log_file: Path | None = None


def _log(phase: str, content: str) -> None:
    if _log_file is None:
        return
    with _log_file.open("a") as f:
        f.write(f"\n{'=' * 60}\n[{phase}]\n{'=' * 60}\n{content}\n")


# ── Terminal helpers ──────────────────────────────────────────────────────────

def section(title: str) -> None:
    pad = max(0, 55 - len(title))
    print(f"\n── {title} {'─' * pad}")


def ask(prompt: str) -> str:
    try:
        return input(f"  {prompt}> ").strip()
    except EOFError:
        sys.exit(0)


# ── Phase 0: JD selection ─────────────────────────────────────────────────────

def select_jd() -> Path | None:
    """Return a JD path to tailor against, or None to render an existing YAML."""
    jds = sorted(p for ext in ("*.pdf", "*.md") for p in JDS_DIR.glob(ext))
    if not jds:
        print(f"No JD files found in {JDS_DIR}/")
        sys.exit(1)

    section("Select Job Description")
    for i, jd in enumerate(jds, 1):
        print(f"  {i}. {jd.stem}")
    render_opt = len(jds) + 1
    print(f"  {render_opt}. Render an existing tailored YAML")

    while True:
        try:
            idx = int(ask("")) - 1
            if 0 <= idx < len(jds):
                return jds[idx]
            if idx == len(jds):
                return None
        except ValueError:
            pass
        print("  Enter a number from the list.")


def select_tailored_yaml() -> Path:
    yaml_files = sorted(TAILORED_DIR.glob("*.yaml"))
    if not yaml_files:
        print(f"  No tailored YAML files found in {TAILORED_DIR}/")
        sys.exit(1)

    section("Select Tailored YAML")
    for i, y in enumerate(yaml_files, 1):
        print(f"  {i}. {y.stem}")

    while True:
        try:
            idx = int(ask("")) - 1
            if 0 <= idx < len(yaml_files):
                return yaml_files[idx]
        except ValueError:
            pass
        print("  Enter a number from the list.")


def _extract_pdf_image_blocks(path: Path) -> list[dict]:
    """Return Claude image content blocks for every image XObject in the PDF."""
    _JPEG_MAGIC = b"\xff\xd8"
    _PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    reader = PdfReader(path)
    blocks: list[dict] = []
    for page in reader.pages:
        resources = page.get("/Resources", {})
        xobjects = resources.get("/XObject", {})
        for name in xobjects:
            xobj = xobjects[name].get_object()
            if xobj.get("/Subtype") != "/Image":
                continue
            data = xobj.get_data()
            if data[:2] == _JPEG_MAGIC:
                media_type = "image/jpeg"
            elif data[:8] == _PNG_MAGIC:
                media_type = "image/png"
            else:
                continue
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode(),
                },
            })
    return blocks


def extract_jd_text(path: Path, client: Anthropic | None = None) -> str:
    if path.suffix == ".md":
        return path.read_text()
    reader = PdfReader(path)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if text.strip() or client is None:
        return text
    # Image-only PDF: fall back to Claude vision transcription
    print("  PDF has no text layer — using Claude vision to transcribe...")
    blocks = _extract_pdf_image_blocks(path)
    if not blocks:
        return text
    blocks.append({
        "type": "text",
        "text": (
            "These images are pages from a job description PDF. "
            "Transcribe all text exactly as it appears, preserving structure with newlines. "
            "Output only the transcribed text, nothing else."
        ),
    })
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": blocks}],
    )
    _log("jd_transcription", resp.content[0].text)
    return resp.content[0].text


# ── LLM helpers ───────────────────────────────────────────────────────────────

def parse_json(text: str, phase: str = "") -> dict:
    _log(f"{phase}.raw_response", text)
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        _log(f"{phase}.parse_error", f"{exc}\n\nCleaned text:\n{text}")
        raise


def bank_context(bank: ContentBank) -> str:
    lines = []
    for h in bank.highlights:
        lines.append(
            f"[{h.id}] status={h.status} | {h.company} — {h.position}\n"
            f"  tags: {', '.join(h.tags)}\n"
            f"  text: {h.text}"
        )
    return "\n\n".join(lines)


# ── Phase 1: Gap Analysis ─────────────────────────────────────────────────────

_GAP_SCHEMA = """\
{
  "jd_summary": {
    "role": "<role title>",
    "company": "<company name>",
    "key_requirements": ["<requirement>", ...],
    "emphasis": "<one sentence on the role's core priority>"
  },
  "coverage": {
    "strong_matches": ["<highlight-id>", ...],
    "recommend_activate": ["<highlight-id>", ...],
    "recommend_deactivate": ["<highlight-id>", ...],
    "gaps": ["<JD requirement not covered by any highlight>", ...]
  },
  "profile_assessment": "<is the current profile a good fit, and what direction should a rewrite take>",
  "questions_for_user": [
    {"id": "q1", "question": "<specific question whose answer fills a gap>"}
  ]
}"""


def run_gap_analysis(client: Anthropic, jd_text: str, bank: ContentBank, resume_yaml: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=(
            "You are an expert resume strategist and senior analytics engineering hiring specialist. "
            "Analyse a job description against a candidate's content bank and return JSON only — no other text."
        ),
        messages=[{"role": "user", "content": (
            f"JOB DESCRIPTION:\n{jd_text}\n\n"
            f"CURRENT RESUME (YAML):\n{resume_yaml}\n\n"
            f"CONTENT BANK:\n{bank_context(bank)}\n\n"
            f"Return JSON matching this schema exactly:\n{_GAP_SCHEMA}"
        )}],
    )
    return parse_json(resp.content[0].text, "gap_analysis")


def display_gap_analysis(a: dict) -> None:
    s, c = a["jd_summary"], a["coverage"]
    print(f"\n  Role:     {s['role']} @ {s['company']}")
    print(f"  Emphasis: {s['emphasis']}")
    print(f"\n  Key requirements:")
    for req in s["key_requirements"]:
        print(f"    • {req}")
    print(f"\n  Strong matches:       {', '.join(c['strong_matches']) or 'none'}")
    print(f"  Recommend activate:   {', '.join(c['recommend_activate']) or 'none'}")
    if c["recommend_deactivate"]:
        print(f"  Recommend deactivate: {', '.join(c['recommend_deactivate'])}")
    if c["gaps"]:
        print("\n  Gaps (not covered by any highlight):")
        for gap in c["gaps"]:
            print(f"    • {gap}")
    print(f"\n  Profile: {a['profile_assessment']}")


# ── Phase 2: Clarification ────────────────────────────────────────────────────

def run_clarification(analysis: dict) -> dict[str, str]:
    questions = analysis.get("questions_for_user", [])
    if not questions:
        print("\n  No clarification needed — proceeding to tailoring.")
        return {}

    print(f"\n  {len(questions)} question(s) to fill gaps before tailoring:\n")
    answers: dict[str, str] = {}
    for q in questions:
        print(f"  {q['question']}")
        answers[q["id"]] = ask("")
        print()
    return answers


# ── Phase 3: Tailoring ────────────────────────────────────────────────────────

_TAILORING_SCHEMA = """\
{
  "active_highlight_ids": ["<id in priority order — covers all roles>", ...],
  "profile": "<rewritten profile text, or null to keep current>",
  "highlight_rewrites": {"<highlight-id>": "<rewritten text>"},
  "reasoning": "<2-3 sentences on key decisions>"
}"""

_TAILORING_RULES = """\
- Maximum 5 highlights per role
- Order highlights within each role by relevance to this specific JD, strongest first
- Select highlights across all roles, not only the most recent
- Only include a highlight_rewrite if rewording meaningfully improves the match
- Rewrite the profile to open with the JD's core emphasis"""


def run_tailoring(
    client: Anthropic,
    analysis: dict,
    answers: dict[str, str],
    bank: ContentBank,
    resume_yaml: str,
) -> dict:
    answers_text = (
        "\n".join(f"  Q({qid}): {ans}" for qid, ans in answers.items())
        if answers
        else "  No clarification questions were asked."
    )
    current_profile = yaml.safe_load(resume_yaml)["cv"]["sections"]["profile"][0]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=(
            "You are an expert resume strategist. "
            "Produce a tailored resume configuration from a gap analysis and candidate answers. "
            f"Return JSON only — no other text.\n\nRules:\n{_TAILORING_RULES}"
        ),
        messages=[{"role": "user", "content": (
            f"GAP ANALYSIS:\n{json.dumps(analysis, indent=2)}\n\n"
            f"USER ANSWERS:\n{answers_text}\n\n"
            f"CONTENT BANK:\n{bank_context(bank)}\n\n"
            f"CURRENT PROFILE:\n{current_profile}\n\n"
            f"Return JSON matching this schema exactly:\n{_TAILORING_SCHEMA}"
        )}],
    )
    return parse_json(resp.content[0].text, "tailoring")


def display_tailoring(tailoring: dict, bank: ContentBank) -> None:
    print(f"\n  {tailoring['reasoning']}\n")

    # Group selected IDs by role for display
    by_role: dict[str, list[str]] = {}
    for hid in tailoring["active_highlight_ids"]:
        h = next((h for h in bank.highlights if h.id == hid), None)
        if h:
            label = f"{h.company} — {h.position}"
            by_role.setdefault(label, []).append(hid)

    for role, ids in by_role.items():
        print(f"  {role}:")
        for hid in ids:
            rewritten = hid in (tailoring.get("highlight_rewrites") or {})
            marker = " (rewritten)" if rewritten else ""
            print(f"    • {hid}{marker}")

    if tailoring.get("profile"):
        print("\n  Profile: rewritten")


# ── Phase 4: Apply + Render ───────────────────────────────────────────────────

def apply_tailoring(tailoring: dict, bank: ContentBank, resume_yaml: str, jd_stem: str) -> Path:
    resume_data = yaml.safe_load(resume_yaml)
    cv = resume_data["cv"]
    rewrites: dict[str, str] = tailoring.get("highlight_rewrites") or {}

    # Group selected highlights by (company, position), preserving priority order
    grouped: dict[tuple[str, str], list[str]] = {}
    for hid in tailoring["active_highlight_ids"]:
        h = next((h for h in bank.highlights if h.id == hid), None)
        if h is None:
            print(f"  Warning: unknown highlight id '{hid}' — skipped")
            continue
        key = (h.company, h.position)
        grouped.setdefault(key, []).append(rewrites.get(h.id, h.text))

    for entry in cv.get("sections", {}).get("Experience", []):
        key = (entry.get("company", ""), entry.get("position", ""))
        if key in grouped:
            entry["highlights"] = grouped[key]

    new_profile = tailoring.get("profile")
    if new_profile:
        cv["sections"]["profile"] = [new_profile]

    TAILORED_DIR.mkdir(exist_ok=True)
    out_path = TAILORED_DIR / f"{jd_stem}_resume.yaml"
    out_path.write_text(
        yaml.dump(resume_data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    )
    return out_path


def render_resume(yaml_path: Path) -> bool:
    result = subprocess.run(
        ["uv", "run", "rendercv", "render", str(yaml_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"\n  rendercv error:\n{result.stderr}")
        return False
    print("  Rendered to rendercv_output/")
    return True


# ── Phase 5: Recruiter Review ─────────────────────────────────────────────────

def find_output_pngs() -> list[Path]:
    return sorted(Path("tailored/rendercv_output").glob("*.png"))


_RECRUITER_SCHEMA = """\
{
  "competencies": {
    "score": <1-5>,
    "verdict": "<assessment>",
    "issues": ["<specific issue>", ...]
  },
  "readability": {
    "score": <1-5>,
    "verdict": "<assessment>",
    "issues": ["<specific issue>", ...]
  },
  "red_flags": ["<red flag>", ...],
  "overall": "<strong | borderline | weak>",
  "priority_fixes": ["<actionable fix>", ...]
}"""


def run_recruiter_review(client: Anthropic, pngs: list[Path], jd_summary: dict) -> dict:
    image_blocks: list[dict] = []
    for png in pngs:
        data = base64.standard_b64encode(png.read_bytes()).decode()
        image_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        })

    image_blocks.append({"type": "text", "text": (
        f"You are a senior recruiter doing an initial CV screen for a "
        f"{jd_summary['role']} position at {jd_summary['company']}.\n"
        f"Key requirements for this role: {', '.join(jd_summary['key_requirements'])}\n\n"
        "You have 30 seconds before deciding whether to read carefully or discard. "
        "Review the CV above and answer these three questions honestly and critically:\n\n"
        "1. Do the key competencies for this role stand out immediately in a first scan?\n"
        "2. Is the CV overwritten or difficult to parse? Are bullets too long or dense?\n"
        "3. Are there any red flags that would make you want to discard this candidate immediately?\n\n"
        f"Return JSON matching this schema exactly:\n{_RECRUITER_SCHEMA}"
    )})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": image_blocks}],
    )
    return parse_json(resp.content[0].text, "recruiter_review")


def display_recruiter_review(review: dict) -> None:
    def bar(n: int) -> str:
        return "█" * n + "░" * (5 - n)

    c, r = review["competencies"], review["readability"]
    print(f"\n  Competencies visible  [{bar(c['score'])}] {c['score']}/5")
    print(f"  {c['verdict']}")
    for issue in c.get("issues") or []:
        print(f"    • {issue}")

    print(f"\n  Readability           [{bar(r['score'])}] {r['score']}/5")
    print(f"  {r['verdict']}")
    for issue in r.get("issues") or []:
        print(f"    • {issue}")

    flags = review.get("red_flags") or []
    print(f"\n  Red flags: {'none' if not flags else ''}")
    for flag in flags:
        print(f"    ⚠  {flag}")

    print(f"\n  Overall: {review['overall'].upper()}")
    for fix in review.get("priority_fixes") or []:
        print(f"    → {fix}")


# ── Phase 6: Revision ─────────────────────────────────────────────────────────

_REVISION_RULES = """\
- Address the recruiter's priority fixes specifically and directly
- Prefer selecting a shorter/punchier highlight from the bank over rewriting where possible
- When rewriting, cut ruthlessly — one clear idea per bullet, active voice
- Maximum 5 highlights per role
- Keep the same JSON schema as the original tailoring"""


def run_revision(
    client: Anthropic,
    review: dict,
    analysis: dict,
    answers: dict[str, str],
    bank: ContentBank,
    tailoring: dict,
    resume_yaml: str,
) -> dict:
    answers_text = (
        "\n".join(f"  Q({qid}): {ans}" for qid, ans in answers.items())
        if answers else "  None."
    )
    current_profile = yaml.safe_load(resume_yaml)["cv"]["sections"]["profile"][0]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=(
            "You are an expert resume strategist. "
            "Revise a tailored resume based on recruiter feedback. "
            f"Return JSON only — no other text.\n\nRules:\n{_REVISION_RULES}"
        ),
        messages=[{"role": "user", "content": (
            f"RECRUITER FEEDBACK:\n{json.dumps(review, indent=2)}\n\n"
            f"CURRENT TAILORING:\n{json.dumps(tailoring, indent=2)}\n\n"
            f"GAP ANALYSIS:\n{json.dumps(analysis, indent=2)}\n\n"
            f"USER ANSWERS:\n{answers_text}\n\n"
            f"CONTENT BANK:\n{bank_context(bank)}\n\n"
            f"CURRENT PROFILE:\n{current_profile}\n\n"
            f"Return JSON matching this schema exactly:\n{_TAILORING_SCHEMA}"
        )}],
    )
    return parse_json(resp.content[0].text, "revision")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        settings = Settings()
        client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
        bank = ContentBank.load()
        resume_yaml = Path("resume.yaml").read_text()

        # Phase 0: Select JD or render existing
        selected = select_jd()

        if selected is None:
            tailored = select_tailored_yaml()
            section("Rendering")
            print(f"  Rendering {tailored.name}...")
            render_resume(tailored)
            print("\n  Done.\n")
            return

        global _log_file
        LOGS_DIR.mkdir(exist_ok=True)
        _log_file = LOGS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{selected.stem}.log"
        print(f"  Logging to {_log_file}")

        print(f"\n  Parsing {selected.name}...")
        jd_text = extract_jd_text(selected, client)

        # Phase 1: Gap Analysis
        section("Phase 1/5: Gap Analysis")
        print("  Analysing JD against your content bank...")
        analysis = run_gap_analysis(client, jd_text, bank, resume_yaml)
        display_gap_analysis(analysis)

        ask("\nPress Enter to continue to clarification...")

        # Phase 2: Clarification
        section("Phase 2/5: Clarification")
        answers = run_clarification(analysis)

        # Phase 3: Tailoring
        section("Phase 3/5: Tailoring")
        print("  Generating tailored configuration...")
        tailoring = run_tailoring(client, analysis, answers, bank, resume_yaml)
        display_tailoring(tailoring, bank)

        # Phase 4: Apply + Render
        section("Phase 4/5: Apply + Render")
        out_yaml = apply_tailoring(tailoring, bank, resume_yaml, selected.stem)
        print(f"  Written: {out_yaml}")
        rendered = render_resume(out_yaml)

        if not rendered:
            print("\n  Render failed — skipping recruiter review.\n")
            sys.exit(1)

        # Phase 5: Recruiter Review
        section("Phase 5/5: Recruiter Review")
        pngs = find_output_pngs()
        if not pngs:
            print("  No PNG output found in rendercv_output/ — skipping review.")
        else:
            print(f"  Reviewing {len(pngs)}-page CV as a recruiter for {analysis['jd_summary']['role']}...\n")
            review = run_recruiter_review(client, pngs, analysis["jd_summary"])
            display_recruiter_review(review)

            # Phase 6: Revision (optional)
            if review["overall"] != "strong":
                choice = ask("\nRevise and re-render based on recruiter feedback? [Y/n] ")
                if choice.lower() in ("", "y", "yes"):
                    section("Phase 6: Revision")
                    print("  Revising based on recruiter feedback...")
                    revised = run_revision(client, review, analysis, answers, bank, tailoring, resume_yaml)
                    display_tailoring(revised, bank)

                    section("Re-applying")
                    out_yaml = apply_tailoring(revised, bank, resume_yaml, selected.stem)
                    print(f"  Written: {out_yaml}")
                    render_resume(out_yaml)
            else:
                print("\n  Recruiter verdict: strong — no revision needed.")

        print("\n  Done.\n")

    except KeyboardInterrupt:
        print("\n\n  Interrupted.")
        sys.exit(0)


if __name__ == "__main__":
    main()
