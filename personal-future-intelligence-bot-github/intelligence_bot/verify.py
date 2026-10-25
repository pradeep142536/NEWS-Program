from __future__ import annotations

import html
import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .collector import ArticlePage, retrieve_article
from .models import Classification, FeedItem, Source, Verification
from .net import FetchError, UnsafeURL, host_matches

AMOUNT_RE = re.compile(
    r"(?i)(?:₹\s?[\d,]+(?:\.\d+)?|INR\s?[\d,]+(?:\.\d+)?|(?:USD|US\$|EUR|GBP|\$)\s?[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s?(?:rupees?|INR|USD|dollars?|euros?|pounds?))"
)
DATE_RE = re.compile(
    r"(?i)\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2},?\s+\d{4})\b"
)
NOISE_RE = re.compile(
    r"(?i)\b(?:celebrity|gossip|viral|murder|killed|shooting|crime|political row|slams|clashes with|shocking|you won't believe|miracle cure)\b"
)
MONEY_CLAIM_RE = re.compile(r"(?i)(?:₹|\bINR\b|\bUSD\b|\bUS\$|\bEUR\b|\bGBP\b|\$\s?\d|\bguaranteed (?:money|cash|income|benefit)\b|\bfree money\b|\b(?:cash|prize) award\b)")
CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("money", re.compile(r"(?i)\b(?:scholarship|grant|stipend|fellowship|internship|funding|financial aid|prize money|cash prize|subsidy|fee waiver|tuition waiver|bursary|student benefit|application window|startup support|competition award|research award)\b")),
    ("ai", re.compile(r"(?i)\b(?:artificial intelligence|\bAI\b|generative AI|GenAI|machine learning|AI model|foundation model|frontier model|large language models?|\bLLMs?\b|open[- ]source model|open weights|coding agent|AI agents?|developer API|free API|AI tool|AI feature|AI capability|model training|inference|multimodal model|generative model)\b")),
    ("students", re.compile(r"(?i)\b(?:education|educational|teaching|learning|virtual classroom|technical education|vocational training|student inventor|student-built|student project|young inventor|school student|college student|university team|undergraduate|postgraduate|doctoral student|PhD student|science fair|student competition|student contest|student challenge|hackathon|olympiad|student innovation|prototype|career opportunity|early-career|postdoctoral|postdoc)\b")),
    ("cybersecurity", re.compile(r"(?i)\b(?:cyber[- ]?security|zero[- ]day|security vulnerability|malware|ransomware|phishing|security patch|data breach|critical vulnerability|cyberattack|cyber attack|TLS|encryption|hijacked|credential theft|DDoS)\b")),
    ("science_technology", re.compile(r"(?i)\b(?:robot|robotics|engineering|spacecraft|satellite|NASA|ISRO|ESA|astronomy|scientific discovery|research paper|materials science|battery|renewable energy|cyber[- ]?security|open[- ]source|computing|technology|science|scientists?|researchers?|physics|quantum|biotechnology|semiconductor|fusion energy|space mission|Mars|lunar|Moon|planetary|climate research|experiment|invention|infrastructure|software|developer tools?|SDK|API)\b")),
    ("government", re.compile(r"(?i)\b(?:government scheme|ministry|public programme|public program|government announces|government launches|official notification|policy change|public benefits|state-backed|national programme|national program)\b")),
)


def _snippet(text: str, expression: re.Pattern[str], width: int = 360) -> str | None:
    match = expression.search(text)
    if not match:
        return None
    start = max(0, match.start() - width // 3)
    end = min(len(text), start + width)
    return text[start:end].strip()


def extract_claims(text: str) -> dict[str, object]:
    amounts = list(dict.fromkeys(match.group(0).strip() for match in AMOUNT_RE.finditer(text)))[:12]
    dates = list(dict.fromkeys(match.group(0).strip() for match in DATE_RE.finditer(text)))[:12]
    eligibility = _snippet(text, re.compile(r"(?i)\b(?:eligible|eligibility|who can apply|open to|applicants? must|students? from|Indian applicants?)\b"))
    deadline = _snippet(text, re.compile(r"(?i)\b(?:deadline|closing date|last date|apply by|applications? close|due date)\b"))
    geography = _snippet(text, re.compile(r"(?i)\b(?:India|Indian|nationwide|international applicants?|citizens? of|residents? of|country|region|geographic(?:al)? restrictions?)\b"))
    conditions = _snippet(text, re.compile(r"(?i)\b(?:subject to|terms and conditions|selected applicants?|competitive|reimbursement|repayment|loan|conditional|eligibility criteria|conditions apply)\b"))
    creator = _snippet(text, re.compile(r"(?i)\b(?:created by|built by|developed by|students at|team at|researchers at|university team|project by)\b"))
    method = _snippet(text, re.compile(r"(?i)\b(?:built with|built using|works by|prototype uses|uses a|powered by|components include|based on)\b"))
    pricing_access = _snippet(text, re.compile(r"(?i)\b(?:free|open source|open-source|trial|paid plan|pricing|usage limit|quota|available in|availability|waitlist)\b"))
    cost_funding = _snippet(text, re.compile(r"(?i)\b(?:costs?\s+(?:about|approximately|\$|₹|INR)|funded by|received funding|project cost|funding of|budget of)\b"))
    status = "unknown"
    if re.search(r"(?i)\b(?:applications? (?:are )?open|apply now|accepting applications|call for applications)\b", text):
        status = "announced_open"
    if re.search(r"(?i)\b(?:cancelled|canceled|withdrawn|postponed indefinitely)\b", text):
        status = "cancelled_or_withdrawn"
    if re.search(r"(?i)\b(?:applications? (?:are )?closed|no longer accepting|expired|deadline has passed)\b", text):
        status = "closed"
    past_dates: list[str] = []
    for raw in dates:
        parsed = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
            try:
                parsed = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if parsed and parsed < datetime.now(timezone.utc).date():
            past_dates.append(raw)
    if past_dates and status == "announced_open":
        status = "date_conflict_check_required"
    elif past_dates and status == "unknown":
        status = "past_date_found_status_unresolved"
    return {
        "amount_mentions": amounts,
        "date_mentions": dates,
        "past_date_mentions": past_dates,
        "eligibility_excerpt": eligibility,
        "deadline_excerpt": deadline,
        "geographic_scope_excerpt": geography,
        "conditions_excerpt": conditions,
        "creator_excerpt": creator,
        "method_excerpt": method,
        "pricing_access_excerpt": pricing_access,
        "cost_funding_excerpt": cost_funding,
        "financial_terms_found": [term for term in ("grant", "loan", "reimbursement", "tax benefit", "investment", "competitive award", "stipend", "scholarship", "subsidy", "prize") if re.search(r"(?i)\b" + re.escape(term) + r"\b", text)],
        "application_status_language": status,
    }


def verify_primary_page(item: FeedItem, source: Source, max_bytes: int = 2_000_000) -> tuple[Verification, ArticlePage | None]:
    try:
        page = retrieve_article(item.url, source, max_bytes)
    except (FetchError, UnsafeURL, ValueError) as exc:
        return Verification("insufficient_evidence", item.url, "", reason=str(exc)), None
    host = urlsplit(page.url).hostname or ""
    if not host_matches(host, source.trusted_domains):
        return Verification("rejected_untrusted_domain", page.url, "", reason="Original page is outside the source trust list"), None
    if len(page.text) < 80:
        return Verification("insufficient_evidence", page.url, page.text[:500], page.title, reason="Retrieved page has too little readable text to support a useful summary"), page
    claims = extract_claims(page.text)
    headline_amounts = list(dict.fromkeys(match.group(0).strip() for match in AMOUNT_RE.finditer(item.title)))
    body_amounts = set(claims["amount_mentions"])
    missing_amounts = [amount for amount in headline_amounts if amount not in body_amounts]
    if missing_amounts:
        return Verification(
            "contradicted_by_primary_page", page.url, page.text[:900], page.title, page.image_url,
            claims, "Headline amount was not found in the retrieved original page",
        ), page
    if re.search(r"(?i)\b(?:open|accepting applications|apply now)\b", item.title) and claims["application_status_language"] in ("closed", "cancelled_or_withdrawn"):
        return Verification(
            "contradicted_by_primary_page", page.url, page.text[:900], page.title, page.image_url,
            claims, "Headline implies an open opportunity but the original page says it is closed or cancelled",
        ), page
    money_like = "money" in source.categories and bool(CATEGORY_RULES[0][1].search(" ".join((item.title, item.summary, page.text[:3000]))))
    if money_like and claims["application_status_language"] in ("closed", "cancelled_or_withdrawn"):
        status = "expired_or_cancelled"
        reason = "The original page explicitly says the opportunity is closed, cancelled, or withdrawn"
    elif money_like and claims["application_status_language"] in ("past_date_found_status_unresolved", "date_conflict_check_required"):
        status = "official_announcement_details_unresolved"
        reason = "The page contains an old date or conflicting application-status wording; current status must not be described as open"
    elif money_like and claims["application_status_language"] == "unknown":
        status = "official_announcement_details_unresolved"
        reason = "The original page does not establish that applications are currently open"
    else:
        status = "official_primary_page"
        reason = "Publisher host and original page were retrieved; claims beyond the retrieved page remain unchecked"
    evidence = page.text[:900]
    return Verification(status, page.url, evidence, page.title, page.image_url, claims, reason), page


def is_government_domain(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    government_suffixes = (
        "gov", "gov.in", "nic.in", "gov.uk", "gov.au", "govt.nz", "gov.sg",
        "gc.ca", "gouv.fr", "gob.mx", "gob.es", "gob.ar", "gob.cl", "gob.br",
        "go.jp", "go.kr", "go.id", "go.th",
    )
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in government_suffixes)


def classify(item: FeedItem, source: Source, evidence_text: str, tracked_topics: tuple[str, ...] = ()) -> Classification:
    haystack = " ".join((item.title, item.summary, evidence_text))
    if NOISE_RE.search(item.title):
        return Classification(False, "excluded", 0, ("Headline matched an excluded-news rule",))
    if source.trust_level == "primary" and "money" in source.categories and MONEY_CLAIM_RE.search(haystack) and not is_government_domain(source.domain):
        return Classification(False, "money", 0, ("Financial claims require a configured government or programme-issuer domain",))
    matched: list[str] = []
    for category, pattern in CATEGORY_RULES:
        if pattern.search(haystack) and (category in source.categories or category == "ai" and "science_technology" in source.categories):
            matched.append(category)
    tracked = tuple(topic for topic in tracked_topics if topic.strip() and topic.casefold() in haystack.casefold())
    if tracked and not matched:
        matched.append("tracked")
    if not matched:
        return Classification(False, "not_relevant", 0, ("No practical-interest category matched",))
    reasons = [f"Matched category: {category}" for category in matched]
    score = 2 + 2 * len(matched)
    if len(item.summary) >= 80:
        score += 1
        reasons.append("Feed supplied a readable summary")
    if tracked:
        score += 2
        reasons.append("Matched a subscriber-tracked topic")
    category = matched[0]
    if source.trust_level == "primary" and category == "money" and not is_government_domain(source.domain):
        return Classification(False, "money", score, tuple(reasons + ["Financial claims require a configured government or programme-issuer domain"]))
    if score < 4:
        return Classification(False, category, score, tuple(reasons + ["Below usefulness threshold"]))
    return Classification(True, category, score, tuple(reasons))


def verify_reputable_reporting_page(item: FeedItem, source: Source, max_bytes: int = 2_000_000) -> tuple[Verification, ArticlePage | None]:
    """Retrieve a configured newsroom page and keep its claims attributed to that newsroom."""
    try:
        page = retrieve_article(item.url, source, max_bytes)
    except (FetchError, UnsafeURL, ValueError) as exc:
        return Verification("insufficient_evidence", item.url, "", reason=str(exc)), None
    host = urlsplit(page.url).hostname or ""
    if not host_matches(host, source.trusted_domains):
        return Verification("rejected_untrusted_domain", page.url, "", reason="Article page is outside the newsroom trust list"), None
    if len(page.text) < 80:
        return Verification("insufficient_evidence", page.url, page.text[:500], page.title, reason="Retrieved report has too little readable text to support a useful summary"), page
    claims = extract_claims(page.text)
    headline_amounts = list(dict.fromkeys(match.group(0).strip() for match in AMOUNT_RE.finditer(item.title)))
    body_amounts = set(claims["amount_mentions"])
    if any(amount not in body_amounts for amount in headline_amounts):
        return Verification(
            "contradicted_by_reporting_page", page.url, page.text[:900], page.title, page.image_url,
            claims, "A financial amount in the headline was not found in the retrieved reporting page",
        ), page
    return Verification(
        "reputable_reporting_page", page.url, page.text[:900], page.title, page.image_url,
        claims, "The independent publisher article was retrieved; the story remains attributed to that publisher.",
    ), page


def _reporting_candidate(
    item: FeedItem,
    source: Source,
    tracked_topics: tuple[str, ...],
    max_bytes: int,
    evidence_sources: list[Source],
) -> tuple[Verification, Classification]:
    verification, page = verify_reputable_reporting_page(item, source, max_bytes)
    if verification.status != "reputable_reporting_page" or page is None:
        return verification, Classification(False, "excluded", 0, (verification.reason or "Reporting page could not be verified",))
    evidence_text = verification.evidence_excerpt
    classification = classify(item, source, evidence_text, tracked_topics)
    financial_claim = "money" in source.categories and (
        classification.category == "money" or bool(MONEY_CLAIM_RE.search(" ".join((item.title, item.summary, page.text[:5000]))))
    )
    if not financial_claim:
        claims = dict(verification.claims)
        claims["reporting_publisher"] = source.publisher
        return replace(verification, claims=claims), classification

    allowed_domains = source.primary_evidence_domains or tuple(
        domain
        for candidate in evidence_sources
        if candidate.enabled and candidate.trust_level == "primary"
        for domain in candidate.trusted_domains
    )
    for link in page.links:
        host = (urlsplit(link).hostname or "").lower()
        if not any(host_matches(host, (domain,)) for domain in allowed_domains):
            continue
        target = next((
            candidate for candidate in evidence_sources
            if candidate.enabled and candidate.trust_level == "primary" and host_matches(host, candidate.trusted_domains)
        ), None)
        if target is None:
            continue
        primary_verification, primary_page = verify_primary_page(replace(item, url=link), target, max_bytes)
        if primary_verification.status != "official_primary_page" or primary_page is None:
            continue
        combined_source = replace(target, categories=tuple(dict.fromkeys(source.categories + target.categories)))
        evidence_item = replace(item, url=link, summary=primary_verification.evidence_excerpt)
        verified_classification = classify(evidence_item, combined_source, primary_verification.evidence_excerpt, tracked_topics)
        if not verified_classification.eligible:
            continue
        if verified_classification.category == "money" and primary_verification.claims.get("application_status_language") != "announced_open":
            continue
        claims = dict(primary_verification.claims)
        claims.update({"reporting_url": item.url, "reporting_publisher": source.publisher, "evidence_publisher": target.publisher})
        return replace(primary_verification, claims=claims), verified_classification

    reason = "A financial or opportunity claim from an independent report needs a retrievable linked issuer/official source"
    return Verification(
        "reputable_report_missing_primary_evidence", page.url, page.text[:900], page.title,
        claims={**verification.claims, "reporting_publisher": source.publisher}, reason=reason,
    ), Classification(False, classification.category, classification.score, classification.reasons + (reason,))


def candidate_for(
    item: FeedItem,
    source: Source,
    tracked_topics: tuple[str, ...] = (),
    max_bytes: int = 2_000_000,
    evidence_sources: list[Source] | None = None,
):
    preliminary = classify(item, source, "", tracked_topics)
    if not preliminary.eligible:
        return Verification("not_fetched_irrelevant", item.url, "", reason="Rejected from the feed title and summary before requesting the article page"), preliminary
    if source.trust_level == "reputable_reporting":
        return _reporting_candidate(item, source, tracked_topics, max_bytes, evidence_sources or [source])
    if source.trust_level == "discovery_only":
        try:
            discovery_page = retrieve_article(item.url, source, max_bytes)
        except (FetchError, UnsafeURL, ValueError) as exc:
            return Verification("discovery_unavailable", item.url, "", reason=str(exc)), Classification(False, preliminary.category, preliminary.score, preliminary.reasons + ("Discovery source page could not be checked",))
        allowed_domains = source.primary_evidence_domains
        for link in discovery_page.links:
            host = (urlsplit(link).hostname or "").lower()
            if not any(host_matches(host, (domain,)) for domain in allowed_domains):
                continue
            target = next((
                candidate for candidate in (evidence_sources or [])
                if candidate.enabled and candidate.trust_level == "primary" and host_matches(host, candidate.trusted_domains)
            ), None)
            if target is None:
                continue
            verification, primary_page = verify_primary_page(replace(item, url=link), target, max_bytes)
            if verification.status != "official_primary_page" or primary_page is None:
                continue
            combined_source = replace(target, categories=tuple(dict.fromkeys(source.categories + target.categories)))
            evidence_item = replace(item, title=verification.page_title or item.title, url=link, summary=verification.evidence_excerpt)
            classification = classify(evidence_item, combined_source, verification.evidence_excerpt, tracked_topics)
            if not classification.eligible:
                continue
            claims = dict(verification.claims)
            claims.update({"discovery_url": item.url, "discovery_publisher": source.publisher, "evidence_publisher": target.publisher})
            return replace(verification, claims=claims), classification
        return Verification("discovery_only", item.url, discovery_page.text[:300], discovery_page.title, reason="No retrievable primary source from the configured evidence domains was found"), Classification(False, preliminary.category, preliminary.score, preliminary.reasons + ("Unofficial source is discovery-only until an original source is verified",))
    verification, page = verify_primary_page(item, source, max_bytes)
    if verification.status == "insufficient_evidence" and "HTTP 403" in verification.reason and preliminary.category != "money" and len(item.summary.strip()) >= 80:
        verification = Verification(
            "official_feed_only", item.url, item.summary[:900], item.title, item.image_url,
            extract_claims(item.summary), "The official RSS entry is available but its linked page returned HTTP 403; details are limited to the feed text.",
        )
    classification = classify(item, source, verification.evidence_excerpt, tracked_topics)
    if verification.status not in ("official_primary_page", "official_feed_only") or classification.category == "money" and verification.status != "official_primary_page":
        classification = Classification(False, classification.category, classification.score, classification.reasons + (verification.reason,))
    return verification, classification


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def format_alert(item: FeedItem, source: Source, verification: Verification, category: str, score: int, notice_kind: str = "new") -> str:
    emoji = {"money": "💰", "ai": "🤖", "students": "🎓", "cybersecurity": "🛡️", "science_technology": "🔬", "government": "🏛️", "tracked": "📌"}.get(category, "📰")
    title = escape(item.title[:280])
    summary = item.summary.strip() or verification.evidence_excerpt.strip()
    summary = summary[:520]
    category_label = {
        "money": "Funding & benefits", "ai": "AI & developer tools", "students": "Education & student opportunities",
        "cybersecurity": "Cybersecurity", "science_technology": "Science, engineering & technology",
        "government": "Government announcements", "tracked": "Research & technology",
    }.get(category, "Useful news")
    evidence_url = html.escape(verification.evidence_url[:1000], quote=True)
    publisher = escape(str(verification.claims.get("evidence_publisher") or source.publisher)[:160])
    parts = [f"{emoji} <b>{title}</b>"]
    if notice_kind != "new":
        parts.append("<b>Source update</b>")
    if summary:
        parts.append(escape(summary))
    parts.append(f"<b>Category:</b> {category_label}")
    if item.published:
        parts.append("<b>Published:</b> " + escape(item.published[:100]))
    if category in ("money", "students", "government"):
        eligibility = verification.claims.get("eligibility_excerpt")
        deadline = verification.claims.get("deadline_excerpt")
        if eligibility:
            parts.append("<b>Eligibility:</b> " + escape(str(eligibility)[:260]))
        if deadline:
            parts.append("<b>Deadline:</b> " + escape(str(deadline)[:260]))
    reporting_url = verification.claims.get("reporting_url") or verification.claims.get("discovery_url")
    reporting_publisher = verification.claims.get("reporting_publisher") or verification.claims.get("discovery_publisher")
    if reporting_url and reporting_publisher:
        report_link = html.escape(str(reporting_url)[:1000], quote=True)
        report_name = escape(str(reporting_publisher)[:160])
        parts.append(f'<b>Report:</b> <a href="{report_link}">{report_name}</a>')
    parts.append(f'<b>Source:</b> <a href="{evidence_url}">{publisher}</a>')
    return "\n\n".join(parts)
