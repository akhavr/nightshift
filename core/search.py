"""Search related issues — tracker-agnostic."""

import re
from collections import Counter
from core.protocols import TrackerIssue, IssueTracker

STOP_WORDS = frozenset({
    "this","that","with","from","have","been","will","would","should","could",
    "about","their","there","which","other","than","then","when","what","into",
    "more","some","very","just","also","only","does","done","each","like",
    "make","made","need","work","used","using","want",
})

def search_related_issues(
    target: TrackerIssue, all_issues: list[TrackerIssue],
    tracker: IssueTracker, min_score: int = 3, max_chars: int = 3000,
) -> str:
    words = re.findall(r"\b[a-z]{4,}\b", f"{target.title} {target.body}".lower())
    keywords = [w for w, _ in Counter(
        w for w in words if w not in STOP_WORDS).most_common(15)]
    if not keywords: return ""
    scored = []
    for issue in all_issues:
        if issue.id == target.id: continue
        comments = tracker.get_comments(issue.id)
        text = (issue.title + " " + " ".join(c.body for c in comments)).lower()
        score = sum(text.count(k) for k in keywords)
        if score >= min_score:
            res = comments[-1].body[:800] if comments else "No resolution"
            scored.append((score, issue, res))
    scored.sort(key=lambda x: x[0], reverse=True)
    parts, total = [], 0
    for score, issue, res in scored:
        e = f"### [{issue.status}] {issue.title} (relevance: {score})\n**Resolution:** {res}\n---"
        if total + len(e) > max_chars: break
        parts.append(e); total += len(e)
    return "\n".join(parts)
