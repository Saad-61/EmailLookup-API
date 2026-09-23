"""
social_finder.py
----------------
Discovers and scores candidate accounts on LinkedIn, Instagram, TikTok, Pinterest,
Twitter/X, Facebook, and GitHub using:
1. High-concurrency direct OpenGraph / crawler probing across 5 platforms.
2. Clean single-site search queries via SearXNG (Yandex + Startpage).
3. Smart compound name parsing (e.g. nomanghaffar074 -> Noman Ghaffar).
4. Multi-anchor scoring with Jaro-Winkler similarity, surname disambiguation,
   and company/location corroboration.
"""

import asyncio
import os
import random
import re
import html
import urllib.parse
import sys
import unicodedata
from typing import List, Optional, Dict, Any, Tuple
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"), override=True)


# ==========================================
# 1. STRING SIMILARITY & JARO-WINKLER
# ==========================================
def jaro_similarity(s1: str, s2: str) -> float:
    """Compute standard Jaro similarity between two strings."""
    if not s1 or not s2:
        return 1.0 if s1 == s2 else 0.0
    if s1 == s2:
        return 1.0

    len1, len2 = len(s1), len(s2)
    match_distance = max(len1, len2) // 2 - 1
    if match_distance < 0:
        match_distance = 0

    s1_matches = [False] * len1
    s2_matches = [False] * len2

    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j]:
                continue
            if s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    transpositions //= 2
    return (matches / len1 + matches / len2 + (matches - transpositions) / matches) / 3.0


def jaro_winkler_similarity(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """
    Compute Jaro-Winkler similarity with prefix bonus.
    Accounts for common transliterations and minor spelling variations.
    """
    s1_clean = (s1 or "").strip().lower()
    s2_clean = (s2 or "").strip().lower()
    if not s1_clean or not s2_clean:
        return 1.0 if s1_clean == s2_clean else 0.0
    if s1_clean == s2_clean:
        return 1.0

    j_sim = jaro_similarity(s1_clean, s2_clean)

    prefix_len = 0
    for c1, c2 in zip(s1_clean[:4], s2_clean[:4]):
        if c1 == c2:
            prefix_len += 1
        else:
            break

    return min(1.0, j_sim + (prefix_len * prefix_weight * (1.0 - j_sim)))


# ==========================================
# 2. PROXY CONFIGURATION
# ==========================================
def get_proxy_ips() -> List[str]:
    """Load proxy IP list dynamically from PROXY_IPS in .env."""
    raw = os.getenv("PROXY_IPS", "").strip()
    if raw:
        return [ip.strip() for ip in raw.split(",") if ip.strip()]
    return []


PROXY_IPS = get_proxy_ips()


def get_random_proxy_url() -> Optional[str]:
    """Construct randomized proxy URL from PROXY_USERNAME, PROXY_PASSWORD, and PROXY_IPS."""
    ips = get_proxy_ips()
    if not ips:
        return None
    ip = random.choice(ips)
    user = os.getenv("PROXY_USERNAME", "").strip()
    pwd = os.getenv("PROXY_PASSWORD", "").strip()
    if user and pwd:
        return f"http://{user}:{pwd}@{ip}"
    return f"http://{ip}"


# ==========================================
# 3. COMPOUND NAME PARSER & HANDLE GENERATION
# ==========================================
COMMON_FIRST_NAMES = {
    "noman", "nouman", "nauman", "saad", "ahtisham", "atisam", "ali", "muhammad", 
    "mohammad", "ahmed", "ahmad", "dameesha", "momina", "hamza", "usman", "bilal",
    "hassan", "hussain", "zain", "omer", "umar", "faisal", "farhan", "kashif",
    "patrick", "john", "david", "michael", "sarah", "emma", "alex", "daniel",
    "sharafat", "haseeb", "asif", "dilawar", "rauf", "hameed", "collison", "ghaffar"
}


def split_compound_name(local_part: str) -> Tuple[str, str]:
    """
    Splits compound local-part (e.g. nomanghaffar074 -> Noman Ghaffar, saadasif78656 -> Saad Asif).
    """
    clean = re.sub(r'[\d._+-]+', '', local_part).lower()
    if not clean:
        return "", ""

    if len(clean) >= 5 and clean[0] == 'r' and clean[1:] in COMMON_FIRST_NAMES:
        return clean[1:].capitalize(), ""

    for fn in sorted(COMMON_FIRST_NAMES, key=len, reverse=True):
        if clean.startswith(fn) and len(clean) > len(fn):
            rem = clean[len(fn):]
            if len(rem) >= 2:
                return fn.capitalize(), rem.capitalize()

    return clean.capitalize(), ""


def generate_handle_variations(
    email: str,
    name: Optional[str] = None,
    gh_username: Optional[str] = None,
) -> Tuple[List[str], List[str]]:
    """Generate prioritized handle variations from email local-part and name."""
    specific = []
    stems = []
    local = email.split("@")[0].lower().strip() if "@" in email else ""

    if local:
        specific.append(local)
        clean_no_sep = re.sub(r"[._+-]", "", local)
        if clean_no_sep != local:
            specific.append(clean_no_sep)
        clean_no_num = re.sub(r"\d+", "", clean_no_sep)
        if len(clean_no_num) >= 3:
            stems.append(clean_no_num)

        chunks = [c for c in re.split(r"[._+-]", local) if len(c) >= 3 and not c.isdigit()]
        for c in chunks:
            c_no_num = re.sub(r"\d+", "", c)
            if len(c_no_num) >= 3:
                stems.append(c_no_num)

        if len(chunks) >= 2:
            stems.append("".join(chunks))
            stems.append(f"{chunks[1]}{chunks[0]}")
            stems.append(f"{chunks[0]}.{chunks[1]}")
            stems.append(f"{chunks[0]}_{chunks[1]}")

    if gh_username:
        gh_clean = gh_username.lower().strip()
        if gh_clean not in specific:
            specific.append(gh_clean)
        gh_no_sep = re.sub(r"[._+-]", "", gh_clean)
        if gh_no_sep != gh_clean and gh_no_sep not in specific:
            specific.append(gh_no_sep)

    if not name and local:
        fn, ln = split_compound_name(local)
        if fn and ln:
            name = f"{fn} {ln}"
        elif fn:
            name = fn

    if name:
        parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", name)]
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            concat = "".join(parts)
            rev_concat = f"{last}{first}"
            # Add forward and reverse two-token permutations (EXCLUDE bare single surname 'last')
            for term in (
                f"{first}.{last}", f"{last}.{first}",
                f"{first}_{last}", f"{last}_{first}",
                f"{first}-{last}", f"{last}-{first}",
                concat, rev_concat,
                f"{first[0]}{last}", f"{last[0]}{first}"
            ):
                if term not in specific and term not in stems:
                    stems.append(term)
            if first in stems:
                stems.remove(first)
            stems.insert(0, first)
        elif len(parts) == 1:
            first = parts[0]
            if first in stems:
                stems.remove(first)
            stems.insert(0, first)

    generic = {
        "admin", "info", "support", "sales", "contact", "help",
        "billing", "team", "hello", "official", "mail", "user", "test",
        "gmail", "yahoo", "hotmail", "outlook", "profile", "account", "dev",
    }
    filtered_specific = [v for v in dict.fromkeys(specific) if len(v) >= 3 and v not in generic]
    filtered_stems = [v for v in dict.fromkeys(stems) if len(v) >= 3 and v not in generic]
    return filtered_specific, filtered_stems


def expand_social_probe_handles(
    specific_handles: List[str],
    stem_handles: List[str],
    name: Optional[str] = None,
) -> List[str]:
    """Generate targeted handle permutations for direct social probing across platforms."""
    probes = []
    
    # Identify bare surname to strictly prevent probing it alone (AGENTS.md Rule 3.3)
    bare_surname = ""
    if name and len(name.split()) >= 2:
        bare_surname = name.split()[-1].lower().strip()

    for h in (specific_handles + stem_handles):
        clean = h.strip().lower()
        if clean and clean not in probes and len(clean) >= 3 and clean != bare_surname:
            probes.append(clean)

    seeds = []
    if name:
        parts = [p.lower() for p in re.findall(r"[a-zA-Z]+", name)]
        if parts:
            first = parts[0]
            if len(first) >= 3 and first not in seeds and first != bare_surname:
                seeds.append(first)

    for h in stem_handles:
        clean = re.sub(r"\d+", "", h).strip("._-").lower()
        if clean and len(clean) >= 3 and clean not in seeds and len(clean) <= 12 and clean != bare_surname:
            seeds.append(clean)

    for h in specific_handles:
        clean = re.sub(r"\d+", "", h).strip("._-").lower()
        if clean and len(clean) >= 3 and clean not in seeds and clean != bare_surname:
            seeds.append(clean)

    # High-signal handle templates ({clean}_, _{clean}, momina0_, _momina0, dameesha_09, ahtisham.v2)
    variation_templates = [
        "{clean}_",
        "_{clean}",
        "{clean}0_",
        "_{clean}0",
        "{clean}_0",
        "{clean}_09",
        "{clean}09",
        "{clean}.v2",
        "{clean}_v2",
        "{clean}_01",
    ]

    for s in seeds[:3]:
        for tmpl in variation_templates:
            v = tmpl.format(clean=s)
            if v not in probes:
                probes.append(v)

    return probes


# ==========================================
# 4. PARSER & TITLE / NAME CLEANERS
# ==========================================
def parse_social_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a social media URL into platform, canonical profile URL, and handle."""
    if not url:
        return None

    clean = url.split("?")[0].rstrip("/")

    # Twitter / X
    tw_match = re.search(r"https?://(?:[a-z0-9-]+\.)?(?:x\.com|twitter\.com)/([a-zA-Z0-9_]{1,25})$", clean, re.IGNORECASE)
    if tw_match:
        handle = tw_match.group(1)
        if handle.lower() not in ("home", "explore", "search", "notifications", "messages", "settings", "i", "privacy", "tos"):
            return {
                "platform": "twitter",
                "platform_label": "X / Twitter",
                "handle": handle,
                "url": f"https://x.com/{handle}",
            }

    # Instagram
    ig_match = re.search(r"https?://(?:[a-z0-9-]+\.)?instagram\.com/([a-zA-Z0-9_.]{1,30})/?$", clean, re.IGNORECASE)
    if ig_match:
        handle = ig_match.group(1)
        if handle.lower() not in ("p", "reel", "reels", "stories", "explore", "direct", "accounts", "about", "developer", "legal"):
            return {
                "platform": "instagram",
                "platform_label": "Instagram",
                "handle": handle,
                "url": f"https://www.instagram.com/{handle}",
            }

    # Facebook
    fb_people_match = re.search(r"https?://(?:[a-z0-9-]+\.)?facebook\.com/people/([^/?#]+)/(\d+)", clean, re.IGNORECASE)
    if fb_people_match:
        p_name = fb_people_match.group(1).replace("-", " ")
        p_id = fb_people_match.group(2)
        return {
            "platform": "facebook",
            "platform_label": "Facebook",
            "handle": p_name,
            "url": f"https://www.facebook.com/people/{fb_people_match.group(1)}/{p_id}/",
        }

    fb_match = re.search(r"https?://(?:[a-z0-9-]+\.)?facebook\.com/([a-zA-Z0-9_.]{3,50})/?$", clean, re.IGNORECASE)
    if fb_match:
        handle = fb_match.group(1)
        if handle.lower() not in ("sharer", "share", "login", "recover", "help", "policies", "privacy", "pages", "groups", "events", "watch", "photo", "photos", "video", "videos", "reel", "reels", "posts"):
            return {
                "platform": "facebook",
                "platform_label": "Facebook",
                "handle": handle,
                "url": f"https://www.facebook.com/{handle}",
            }

    # LinkedIn
    if "linkedin.com/in/" in clean:
        li_match = re.search(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([a-zA-Z0-9_/%-]+)", clean, re.IGNORECASE)
        if li_match:
            slug = li_match.group(1).split("?")[0].rstrip("/")
            if slug.lower() not in ("dir", "pub", "feed", "jobs", "company", "school", "pulse", "posts", "learning"):
                return {
                    "platform": "linkedin",
                    "platform_label": "LinkedIn",
                    "handle": slug,
                    "url": f"https://www.linkedin.com/in/{slug}",
                }

    # TikTok
    tt_match = re.search(r"https?://(?:[a-z0-9-]+\.)?tiktok\.com/@([a-zA-Z0-9_.]{2,30})/?$", clean, re.IGNORECASE)
    if tt_match:
        handle = tt_match.group(1)
        if handle.lower() not in ("explore", "direct", "trending", "about", "discover", "login", "live", "tag"):
            return {
                "platform": "tiktok",
                "platform_label": "TikTok",
                "handle": handle,
                "url": f"https://www.tiktok.com/@{handle}",
            }

    # Pinterest
    pin_match = re.search(r"https?://(?:[a-z0-9-]+\.)?pinterest\.com/([a-zA-Z0-9_.]{2,30})/?$", clean, re.IGNORECASE)
    if pin_match:
        handle = pin_match.group(1)
        if handle.lower() not in ("explore", "pin", "ideas", "business", "help", "about", "login", "today", "shop", "news"):
            return {
                "platform": "pinterest",
                "platform_label": "Pinterest",
                "handle": handle,
                "url": f"https://www.pinterest.com/{handle}/",
            }

    # GitHub
    gh_match = re.search(r"https?://(?:[a-z0-9-]+\.)?github\.com/([a-zA-Z0-9_-]{1,39})/?$", clean, re.IGNORECASE)
    if gh_match:
        handle = gh_match.group(1)
        if handle.lower() not in ("features", "business", "explore", "marketplace", "pricing", "topics", "collections", "events"):
            return {
                "platform": "github",
                "platform_label": "GitHub",
                "handle": handle,
                "url": f"https://github.com/{handle}",
            }

    return None


def format_handle_to_name(handle: str, resolved_name: Optional[str] = None) -> str:
    """Format a social handle into a clean human display name."""
    if not handle:
        return ""
    clean = handle.lstrip("@").strip()
    name_clean = re.sub(r"\d+$", "", clean).strip("._-")
    parts = [p.capitalize() for p in re.split(r"[._-]+", name_clean) if len(p) >= 2]
    if len(parts) >= 2:
        return " ".join(parts)
    elif len(parts) == 1:
        if resolved_name and parts[0].lower() in resolved_name.lower():
            return resolved_name
        return parts[0]
    return clean


def clean_display_name(raw_title: str, handle: str, platform: str, resolved_name: Optional[str] = None) -> str:
    """Extract and unescape authentic display name from title tag, stripping entity garbage and generic boilerplate."""
    if not raw_title:
        return format_handle_to_name(handle, resolved_name)

    t = unicodedata.normalize('NFKD', html.unescape(raw_title)).strip()

    if platform == "linkedin" or "linkedin" in t.lower():
        t = re.sub(r"\s*\|\s*LinkedIn.*$", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*-\s*LinkedIn.*$", "", t, flags=re.IGNORECASE)
        segments = re.split(r"\s*[-–|•]\s*", t)
        if segments and len(segments[0].strip()) >= 2:
            name_part = segments[0].strip()
            name_part = re.sub(r",\s*(?:MBA|PHD|PMP|MD|CPA|ESQ|SHRM-[A-Z]+|BSc|MSc).*$", "", name_part, flags=re.IGNORECASE)
            return name_part.strip()

    # Strip platform trailers & generic TikTok/Instagram titles
    t = re.sub(r"\s*[-–|•·]\s*(?:Instagram|X|Twitter|Facebook|TikTok|Pinterest|Photos and videos|Profile).*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+on\s+(?:Instagram|Twitter|X|Facebook|TikTok|Pinterest)\s*:?.*$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:Photos?|Reels?|Videos?|Posts?)\s+by\s+", "", t, flags=re.IGNORECASE)

    # Strip handle in parentheses e.g. "Babar Dilawar (@dilawar)" -> "Babar Dilawar"
    t = re.sub(r"\s*\(@?[a-zA-Z0-9._-]+\)", "", t)
    t = re.sub(r"^@?[a-zA-Z0-9._-]+$", "", t)

    if "|" in t:
        t = t.split("|")[0].strip()
    if "-" in t:
        t = t.split("-")[0].strip()

    t = re.sub(r'[\"\'“”#]', '', t).strip(' -–|•·:/')
    if len(t) > 35:
        t = t[:35].strip()

    tl = t.lower()
    reject_patterns = (
        "link to", "facebook", "instagram", "twitter", "linkedin", "page not found",
        "welcome back", "log in", "visit tiktok to discover profiles", "discover profiles",
        "watch trending", "tiktok", "pinterest", "see what", "profile"
    )
    if not t or any(rej in tl for rej in reject_patterns) or len(t) < 2:
        return format_handle_to_name(handle, resolved_name)

    return t


def clean_bio_snippet(raw_snippet: str, platform: str, handle: str) -> str:
    """Clean and unescape bio snippets, removing unescaped HTML entities and raw tags."""
    if not raw_snippet:
        return f"{platform.title()} profile for @{handle.lstrip('@')}"
    s = unicodedata.normalize('NFKD', html.unescape(raw_snippet)).strip()
    s = re.sub(r"\s+", " ", s)
    if any(bad in s.lower() for bad in ("the site owner hides", "link to facebook", "link to instagram", "welcome back", "log in", "unsupported browser")):
        return f"{platform.title()} profile for @{handle.lstrip('@')}"
    return s


def parse_ddg_html_response(html_text: str) -> List[Dict[str, str]]:
    """Legacy parser preserved for backward-compatibility with lookup_engine."""
    results = []
    if not html_text:
        return results
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for res in soup.find_all("div", class_="result"):
            a_tag = res.find("a", class_="result__url") or res.find("a", class_="result__snippet")
            t_tag = res.find("a", class_="result__title")
            s_tag = res.find("a", class_="result__snippet") or res.find("div", class_="result__snippet")
            if a_tag and a_tag.get("href"):
                link = a_tag["href"]
                title = t_tag.get_text(strip=True) if t_tag else ""
                snippet = s_tag.get_text(strip=True) if s_tag else ""
                results.append({"link": link, "title": title, "snippet": snippet})
    except Exception:
        pass
    return results


# ==========================================
# 5. MULTI-ANCHOR SCORING & DISAMBIGUATION
# ==========================================
def score_candidate(
    platform_info: dict,
    title: str,
    snippet: str,
    all_variations: List[str],
    resolved_name: Optional[str],
    resolved_location: Optional[str],
    gh_username: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Tuple[int, List[str]]:
    """Score a candidate from 0 to 95 with Jaro-Winkler string similarity and disambiguation rules."""
    score = 0
    reasons = []
    handle = platform_info["handle"].lower()
    title_l = html.unescape(title or "").lower()
    snippet_l = html.unescape(snippet or "").lower()
    combined_text = f"{title_l} {snippet_l}"
    handle_norm = re.sub(r"[._-]", "", handle)

    # 1. Verified GitHub handle match
    if gh_username:
        gh_clean = gh_username.lower().strip()
        gh_norm = re.sub(r"[._-]", "", gh_clean)
        if handle == gh_clean or handle_norm == gh_norm:
            score += 85
            reasons.append(f"Direct match with verified GitHub handle (@{gh_username})")
        elif len(gh_norm) >= 4 and (gh_norm in handle_norm or handle_norm in gh_norm):
            score += 70
            reasons.append(f"Stem match with verified GitHub handle (@{gh_username})")

    # 2. Handle matching
    for v in all_variations:
        vl = v.lower()
        vl_norm = re.sub(r"[._-]", "", vl)
        if handle == vl or handle_norm == vl_norm:
            if any(c.isdigit() for c in vl) or len(vl) >= 7:
                score += 75
                reasons.append(f"Distinctive exact handle match (@{handle})")
            else:
                score += 55
                reasons.append(f"Exact handle match (@{handle})")
            break
        elif len(vl_norm) >= 4 and (vl_norm in handle_norm or handle_norm in vl_norm):
            score += 45
            reasons.append(f"Root stem handle match (@{handle})")
            break

    # 3. LinkedIn vanity URL match
    if platform_info.get("platform") == "linkedin":
        slug_norm = re.sub(r"[-_.]", "", handle)
        if resolved_name and len(resolved_name.split()) >= 2:
            name_parts = [p.lower() for p in resolved_name.split() if len(p) >= 2]
            first, last = name_parts[0], name_parts[-1]
            if slug_norm.startswith(f"{first}{last}") or slug_norm.startswith(f"{last}{first}"):
                score += 80
                reasons.append(f"Direct LinkedIn vanity URL match (in/{handle})")

    # 4. Name Matching (Exact, Inverted, and Jaro-Winkler >= 88%)
    target_name = resolved_name
    cand_extracted_name = clean_display_name(title, handle, platform_info.get("platform", ""), resolved_name)
    
    if target_name and len(target_name.split()) >= 2:
        name_parts = [p.lower() for p in target_name.split() if len(p) >= 2]
        first, last = name_parts[0], name_parts[-1]
        rev_name = f"{last} {first}".lower()

        if target_name.lower() in title_l or rev_name in title_l:
            score += 40
            reasons.append(f"Full name match in title ({target_name})")
        elif first in title_l and last in title_l:
            score += 35
            reasons.append(f"First and last name in title ({first.title()} {last.title()})")
        elif rev_name in combined_text or target_name.lower() in combined_text:
            score += 30
            reasons.append(f"Full name match in bio ({target_name})")
        elif cand_extracted_name and len(cand_extracted_name.split()) >= 2:
            cand_parts = [p.lower() for p in cand_extracted_name.split() if len(p) >= 2]
            c_first, c_last = cand_parts[0], cand_parts[-1]
            jw_f = jaro_winkler_similarity(c_first, first)
            jw_l = jaro_winkler_similarity(c_last, last)
            jw_f_inv = jaro_winkler_similarity(c_first, last)
            jw_l_inv = jaro_winkler_similarity(c_last, first)

            if (jw_f >= 0.88 and jw_l >= 0.88) or (jw_f_inv >= 0.88 and jw_l_inv >= 0.88):
                score += 25
                best_sim = max((jw_f + jw_l) / 2.0, (jw_f_inv + jw_l_inv) / 2.0)
                reasons.append(f"Fuzzy name match (Jaro-Winkler {best_sim:.0%}: '{cand_extracted_name}' ~ '{target_name}')")

    # 5. Workplace / Company Corroboration
    if company_name and company_name.lower() in combined_text:
        score += 25
        reasons.append(f"Company corroboration ({company_name})")

    # 6. Location Corroboration
    if resolved_location:
        loc_tokens = [tok.strip().lower() for tok in re.split(r"[,/]", resolved_location) if len(tok.strip()) >= 3]
        matched_locs = [l for l in loc_tokens if l in combined_text]
        if matched_locs:
            score += 15
            reasons.append(f"Location match ({', '.join([l.title() for l in matched_locs])})")

    # 7. Disambiguation Rules: Conflicting Given Name & Contradictory Surname Penalties
    if target_name and len(target_name.split()) >= 2 and cand_extracted_name and len(cand_extracted_name.split()) >= 2:
        name_parts = [p.lower() for p in target_name.split() if len(p) >= 2]
        cand_parts = [p.lower() for p in cand_extracted_name.split() if len(p) >= 2]
        first, last = name_parts[0], name_parts[-1]
        c_first, c_last = cand_parts[0], cand_parts[-1]

        # Rule A: Conflicting given name (Haseeb Hameed vs Atisam Hameed) -> cap at 15
        surname_match = (c_last == last or jaro_winkler_similarity(c_last, last) >= 0.90)
        given_conflict = (c_first != first and jaro_winkler_similarity(c_first, first) < 0.65 and len(c_first) >= 3 and len(first) >= 3)
        if surname_match and given_conflict:
            score = min(score, 15)
            reasons.append(f"Conflicting given name penalty ({c_first.title()} vs {first.title()})")

        # Rule B: Contradictory Surname penalty (Ahtisham Khan vs Ahtisham Dilawar) -> cap at 35
        first_match = (c_first == first or jaro_winkler_similarity(c_first, first) >= 0.90)
        surname_conflict = (c_last != last and jaro_winkler_similarity(c_last, last) < 0.65 and len(c_last) >= 3 and len(last) >= 3)
        if first_match and surname_conflict:
            score = min(score, 35)
            reasons.append(f"Contradictory surname penalty ({c_last.title()} vs {last.title()})")

    score = min(score, 95)
    return max(0, score), reasons


# ==========================================
# 6. DIRECT 5-PLATFORM PROBERS
# ==========================================
CRAWLER_HEADERS = {
    "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TWITTER_HEADERS = {
    "User-Agent": "Twitterbot/1.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


async def probe_instagram_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("p", "reel", "reels", "explore", "direct", "accounts", "about", "developer"):
        return None
    url = f"https://www.instagram.com/{clean}/"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=2.5, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Sorry, this page isn't available" in text or "The link you followed may be broken" in text:
                return None
            
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "static.xx.fbcdn" not in raw_img and "fb_icon" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "instagram", clean)

            display_name = clean_display_name(raw_title, clean, "instagram")

            print(f"[Prober] [INSTAGRAM] @{clean} -> ✓ Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "instagram",
                "platform_label": "Instagram",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_tiktok_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("explore", "direct", "trending", "about", "discover", "login", "live"):
        return None
    url = f"https://www.tiktok.com/@{clean}"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Couldn't find this account" in text or "not found" in text.lower():
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "static" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "tiktok", clean)

            display_name = clean_display_name(raw_title, clean, "tiktok")

            print(f"[Prober] [TIKTOK] @{clean} -> ✓ Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "tiktok",
                "platform_label": "TikTok",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_pinterest_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("explore", "pin", "ideas", "business", "help", "about", "login", "today"):
        return None
    url = f"https://www.pinterest.com/{clean}/"
    try:
        resp = await client.get(url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "Profile not found" in text or "resource not found" in text.lower():
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "default_280" not in raw_img and "default_open_graph" not in raw_img) else None

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "pinterest", clean)

            display_name = clean_display_name(raw_title, clean, "pinterest")

            print(f"[Prober] [PINTEREST] @{clean} -> ✓ Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "pinterest",
                "platform_label": "Pinterest",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_twitter_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9_]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("home", "explore", "search", "notifications", "settings", "i", "tos"):
        return None
    url = f"https://x.com/{clean}"
    try:
        resp = await client.get(url, headers=TWITTER_HEADERS, timeout=3.0, follow_redirects=True)
        if resp.status_code == 200:
            text = resp.text
            if "This account doesn’t exist" in text or "account has been suspended" in text or "page doesn’t exist" in text:
                return None
            soup = BeautifulSoup(text, "html.parser")
            og_title = soup.find("meta", property="og:title")
            raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
            
            og_img = soup.find("meta", property="og:image")
            raw_img = og_img.get("content") if og_img else None
            avatar_url = html.unescape(raw_img) if (raw_img and "pbs.twimg.com" in raw_img) else f"https://unavatar.io/x/{clean}"

            og_desc = soup.find("meta", property="og:description")
            raw_desc = og_desc.get("content") if og_desc else ""
            bio = clean_bio_snippet(raw_desc, "twitter", clean)

            display_name = clean_display_name(raw_title, clean, "twitter")

            print(f"[Prober] [TWITTER] @{clean} -> ✓ Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
            return {
                "platform": "twitter",
                "platform_label": "X / Twitter",
                "handle": clean,
                "name": display_name,
                "url": url,
                "avatar_url": avatar_url,
                "snippet": bio,
                "title": raw_title,
                "discovery_method": "probing"
            }
    except Exception:
        pass
    return None


async def probe_facebook_profile(handle: str, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
    clean = re.sub(r'[^a-zA-Z0-9._]', '', handle).lstrip("@").strip()
    if not clean or len(clean) < 3 or clean in ("sharer", "share", "login", "recover", "help", "policies", "privacy"):
        return None
    url = f"https://www.facebook.com/{clean}"
    try:
        async with client.stream("GET", url, headers=CRAWLER_HEADERS, timeout=3.0, follow_redirects=True) as resp:
            if resp.status_code == 200:
                chunks = []
                bytes_count = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    bytes_count += len(chunk)
                    if b"</head>" in chunk or bytes_count > 35000:
                        break
                text = b"".join(chunks).decode("utf-8", errors="ignore")
                if "This content isn't available right now" in text or "Page Not Found" in text:
                    return None
                soup = BeautifulSoup(text, "html.parser")
                og_title = soup.find("meta", property="og:title")
                raw_title = og_title.get("content") if og_title else (soup.title.string if soup.title else "")
                
                og_img = soup.find("meta", property="og:image")
                raw_img = og_img.get("content") if og_img else None
                avatar_url = html.unescape(raw_img) if (raw_img and "fb_icon" not in raw_img and "static.xx" not in raw_img) else None

                og_desc = soup.find("meta", property="og:description")
                raw_desc = og_desc.get("content") if og_desc else ""
                bio = clean_bio_snippet(raw_desc, "facebook", clean)

                display_name = clean_display_name(raw_title, clean, "facebook")

                print(f"[Prober] [FACEBOOK] @{clean} -> ✓ Confirmed (Name: '{display_name}', Avatar: {'YES' if avatar_url else 'NO'})", flush=True)
                return {
                    "platform": "facebook",
                    "platform_label": "Facebook",
                    "handle": clean,
                    "name": display_name,
                    "url": url,
                    "avatar_url": avatar_url,
                    "snippet": bio,
                    "title": raw_title,
                    "discovery_method": "probing"
                }
    except Exception:
        pass
    return None


# ==========================================
# 7. SEARXNG SEARCH QUERY RUNNER
# ==========================================
async def execute_clean_searxng_query(query: str, client: httpx.AsyncClient) -> List[Dict[str, str]]:
    """Execute clean single-site query against SearXNG using Yandex + Startpage."""
    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8888/search")
    desktop_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    items = []
    seen_links = set()
    try:
        resp = await client.get(
            searxng_url,
            params={"q": query, "format": "json", "engines": "yandex,startpage"},
            headers=desktop_headers,
            timeout=4.0
        )
        if resp.status_code == 200:
            data = resp.json()
            raw_res = data.get("results", [])
            for r in raw_res:
                l = r.get("url", "")
                if l and l not in seen_links:
                    if parse_social_url(l) or any(dom in l.lower() for dom in ("instagram.com", "facebook.com", "x.com", "twitter.com", "linkedin.com", "tiktok.com", "pinterest.com", "github.com")):
                        seen_links.add(l)
                        items.append({
                            "link": l,
                            "title": html.unescape(r.get("title", "")),
                            "snippet": html.unescape(r.get("content", "")),
                        })
            print(f"[SearXNG] 🔍 Query: '{query}' -> ✓ {len(items)} social items found (Yandex + Startpage)", flush=True)
    except Exception as e:
        print(f"[SearXNG] ✗ Query error for '{query}': {e}", flush=True)
    return items


# ==========================================
# 8. MASTER HYBRID DISCOVERY ENGINE
# ==========================================
async def search_social_candidates(
    email: str,
    resolved_name: Optional[str] = None,
    resolved_location: Optional[str] = None,
    gh_username: Optional[str] = None,
    company_name: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    has_verified_linkedin: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """
    Master candidate discovery engine combining:
    1. 5-platform high-speed direct probing with OpenGraph extraction.
    2. 5 clean single-site search queries via SearXNG (Yandex + Startpage).
    3. Multi-anchor scoring with Jaro-Winkler string similarity and surname disambiguation.
    """
    if client is None or getattr(client, "is_closed", False):
        limits = httpx.Limits(max_connections=60, max_keepalive_connections=25)
        async with httpx.AsyncClient(timeout=8.0, limits=limits, verify=False) as local_client:
            return await search_social_candidates(
                email=email,
                resolved_name=resolved_name,
                resolved_location=resolved_location,
                gh_username=gh_username,
                company_name=company_name,
                client=local_client,
                has_verified_linkedin=has_verified_linkedin,
            )

    local_part = email.split("@")[0].lower().strip() if "@" in email else ""

    # Infer compound name from email local-part (e.g. hassanrashid55 -> Hassan Rashid)
    compound_name = None
    if local_part:
        fn, ln = split_compound_name(local_part)
        if fn and ln:
            compound_name = f"{fn} {ln}"
        elif fn:
            compound_name = fn

    if not resolved_name or len(resolved_name.split()) < 2:
        if compound_name:
            resolved_name = compound_name

    specific_handles, stem_handles = generate_handle_variations(email, resolved_name, gh_username)
    probe_seeds = expand_social_probe_handles(specific_handles, stem_handles, resolved_name)[:18]
    all_variations = specific_handles + stem_handles + probe_seeds

    print(f"\n[Social Discovery] ───────────────────────────────────────────────────", flush=True)
    print(f"[Social Discovery] Initiating Hybrid Social Discovery for: {email}", flush=True)
    print(f"[Social Discovery] Inferred Target Name: '{resolved_name or 'N/A'}'", flush=True)
    print(f"[Social Discovery] 📡 Launching 5-Platform Probing ({len(probe_seeds) * 5} direct probes across Instagram, TikTok, Pinterest, Twitter, Facebook)...", flush=True)

    # 1. Build Direct Probe Tasks with Pooled Clients (Deduplicated per platform character rules)
    ig_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip() for s in probe_seeds if len(s) >= 3))
    tt_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip() for s in probe_seeds if len(s) >= 3))
    pin_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip() for s in probe_seeds if len(s) >= 3))
    tw_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9_]', '', s).lstrip("@").strip() for s in probe_seeds if len(s) >= 3))
    fb_seeds = list(dict.fromkeys(re.sub(r'[^a-zA-Z0-9._]', '', s).lstrip("@").strip() for s in probe_seeds if len(s) >= 3))

    probe_tasks = []
    for s in ig_seeds:
        probe_tasks.append(probe_instagram_profile(s, client))
    for s in tt_seeds:
        probe_tasks.append(probe_tiktok_profile(s, client))
    for s in pin_seeds:
        probe_tasks.append(probe_pinterest_profile(s, client))
    for s in tw_seeds:
        probe_tasks.append(probe_twitter_profile(s, client))
    for s in fb_seeds:
        probe_tasks.append(probe_facebook_profile(s, client))

    # 2. Build Focused Search Queries (One high-signal query per platform using Full Name)
    search_queries = []
    query_target = resolved_name if (resolved_name and len(resolved_name.split()) >= 2) else local_part

    # Q1: LinkedIn (if not already corroborated via direct lookup)
    if not has_verified_linkedin:
        search_queries.append(("linkedin", f'site:linkedin.com/in "{query_target}"'))

    # Q2: Instagram (broad query to catch vanity accounts where real name is in bio)
    search_queries.append(("instagram", f'site:instagram.com {query_target}'))

    # Q3: Facebook (query full name e.g. "Atisam Hameed")
    search_queries.append(("facebook", f'site:facebook.com "{query_target}"'))

    # Q4: TikTok (query full name e.g. "Atisam Hameed")
    search_queries.append(("tiktok", f'site:tiktok.com "{query_target}"'))

    # Q5: Pinterest (query full name e.g. "Atisam Hameed")
    search_queries.append(("pinterest", f'site:pinterest.com "{query_target}"'))

    print(f"[Social Discovery] 🔍 Launching {len(search_queries)} focused SearXNG queries for '{query_target}' (Yandex + Startpage)...", flush=True)

    async def run_query(plat_tag: str, q_str: str):
        searx_hits = await execute_clean_searxng_query(q_str, client)
        return plat_tag, searx_hits

    query_tasks = [run_query(p, q) for p, q in search_queries]

    # 3. Execute all Probes and Search Queries simultaneously
    probe_results_raw, *query_results_raw = await asyncio.gather(
        asyncio.gather(*probe_tasks, return_exceptions=True),
        *query_tasks
    )

    candidates_map: Dict[str, Dict[str, Any]] = {}

    # Ingest Probe Hits
    for p_cand in probe_results_raw:
        if not isinstance(p_cand, dict) or not p_cand.get("url"):
            continue
        plat = p_cand["platform"]
        h_clean = p_cand["handle"].lstrip("@")
        parsed = {
            "platform": plat,
            "platform_label": p_cand["platform_label"],
            "handle": h_clean,
            "url": p_cand["url"],
        }
        score, reasons = score_candidate(
            parsed,
            p_cand.get("title", ""),
            p_cand.get("snippet", ""),
            all_variations,
            resolved_name,
            resolved_location,
            gh_username,
            company_name
        )
        if score >= 15:
            dedup_key = f"{plat}:{h_clean.lower()}"
            candidates_map[dedup_key] = {
                "platform": plat,
                "platform_label": p_cand["platform_label"],
                "handle": f"@{h_clean}",
                "name": p_cand.get("name") or h_clean,
                "url": p_cand["url"],
                "snippet": p_cand.get("snippet", ""),
                "score": score,
                "confidence_badge": "",
                "confidence_level": "strong" if score >= 70 else "potential",
                "reasons": reasons,
                "avatar_url": p_cand.get("avatar_url"),
                "discovery_method": "probing"
            }

    # Ingest Query Hits
    for q_res in query_results_raw:
        if not isinstance(q_res, tuple) or len(q_res) != 2:
            continue
        plat_tag, items = q_res
        for it in items:
            link = it.get("link", "")
            title = it.get("title", "")
            snippet = it.get("snippet", "")
            parsed = parse_social_url(link)
            if not parsed:
                continue

            plat = parsed["platform"]
            h_clean = parsed["handle"].lstrip("@")
            dedup_key = f"{plat}:{h_clean.lower()}"

            score, reasons = score_candidate(
                parsed,
                title,
                snippet,
                all_variations,
                resolved_name,
                resolved_location,
                gh_username,
                company_name
            )
            if score < 15:
                continue

            display_name = clean_display_name(title, h_clean, plat, resolved_name)
            bio_clean = clean_bio_snippet(snippet, plat, h_clean)

            if dedup_key not in candidates_map or candidates_map[dedup_key]["score"] < score:
                existing_avatar = candidates_map.get(dedup_key, {}).get("avatar_url")
                candidates_map[dedup_key] = {
                    "platform": plat,
                    "platform_label": parsed["platform_label"],
                    "handle": f"@{h_clean}",
                    "name": display_name or h_clean,
                    "url": parsed["url"],
                    "snippet": bio_clean,
                    "score": score,
                    "confidence_badge": "",
                    "confidence_level": "strong" if score >= 70 else "potential",
                    "reasons": reasons,
                    "avatar_url": existing_avatar,
                    "discovery_method": "querying"
                }

    # Sort all candidates
    all_candidates = sorted(candidates_map.values(), key=lambda x: -x["score"])

    # Group by platform (up to 10 per platform)
    by_platform = {
        "linkedin": [c for c in all_candidates if c["platform"] == "linkedin"][:10],
        "instagram": [c for c in all_candidates if c["platform"] == "instagram"][:10],
        "twitter": [c for c in all_candidates if c["platform"] == "twitter"][:10],
        "facebook": [c for c in all_candidates if c["platform"] == "facebook"][:10],
        "tiktok": [c for c in all_candidates if c["platform"] == "tiktok"][:10],
        "pinterest": [c for c in all_candidates if c["platform"] == "pinterest"][:10],
        "github": [c for c in all_candidates if c["platform"] == "github"][:10],
    }

    total_count = sum(len(v) for v in by_platform.values())
    print(f"[Social Discovery] ✓ Discovery Complete! Total Unique Ranked Candidates: {len(all_candidates)}", flush=True)
    if all_candidates:
        top = all_candidates[0]
        print(f"[Social Discovery] 🏆 Top Match: [{top['platform'].upper()}] {top['handle']} ({top['name']}) -> Score: {top['score']}%", flush=True)
    print(f"[Social Discovery] ───────────────────────────────────────────────────\n", flush=True)

    return all_candidates[:40], by_platform
