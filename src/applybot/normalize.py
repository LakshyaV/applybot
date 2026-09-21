"""URL canonicalization, ATS detection, dedupe keys, and location → country mapping."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_.*|ref|referrer|source|src|gh_src|lever-source.*|lever-origin|s|trk|trackingid|"
    r"jobboard|jobsource|iis|iisn|mode|codes|sourcetype)$",
    re.I,
)
# Link shorteners / list trackers whose target must be resolved by following redirects.
TRACKER_HOSTS = ("zapply.jobs", "jobright.ai", "simplify.jobs", "bit.ly", "lnkd.in")


def canonical_url(url: str) -> str:
    url = html.unescape(url.strip())
    parts = urlsplit(url)
    host = parts.netloc.lower()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(query), ""))


def is_tracker(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in TRACKER_HOSTS)


def host_matches(url: str, hosts: list[str]) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def detect_ats(url: str) -> tuple[str, str, str]:
    """Return (ats, board, job_id). Unknown portals are ('custom', host, path-ish id)."""
    parts = urlsplit(url)
    host, path = parts.netloc.lower(), parts.path
    query = dict(parse_qsl(parts.query))
    seg = [s for s in path.split("/") if s]

    if "greenhouse.io" in host:
        if "for" in query and "token" in query:  # embed/job_app?for=board&token=id
            return "greenhouse", query["for"].lower(), query["token"]
        if len(seg) >= 3 and seg[1] == "jobs":
            return "greenhouse", seg[0].lower(), re.sub(r"\D.*$", "", seg[2])
        return "greenhouse", seg[0].lower() if seg else host, query.get("gh_jid", "")
    if "gh_jid" in query:  # Greenhouse embedded in a company site (or a front-end like careerpuck)
        return "greenhouse_embed", host, query["gh_jid"]
    if host.endswith("lever.co") and len(seg) >= 2:
        return "lever", seg[0].lower(), seg[1]
    if host.endswith("ashbyhq.com") and len(seg) >= 2:
        return "ashby", seg[0].lower(), seg[1]
    if host.endswith("myworkdayjobs.com") or host.endswith("myworkdaysite.com"):
        tenant = host.split(".")[0]
        rest = [s for s in seg if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]  # drop locale
        site = rest[0] if rest else ""
        last = seg[-1] if seg else ""
        if last == "apply" and len(seg) >= 2:
            last = seg[-2]
        job_id = last.rsplit("_", 1)[-1] if "_" in last else last
        return "workday", f"{tenant}/{site}", job_id
    if host.endswith("smartrecruiters.com") and len(seg) >= 2:
        return "smartrecruiters", seg[0].lower(), re.sub(r"\D.*$", "", seg[1]) or seg[1]
    if host.endswith("icims.com") or query.get("icims") == "1":
        m = re.search(r"/jobs/(\d+)", path)
        return "icims", host, m.group(1) if m else path
    if host.endswith("oraclecloud.com"):
        m = re.search(r"/sites/([^/]+)/(?:job|requisitions/preview)/(\d+)", path)
        return "oracle", f"{host.split('.')[0]}/{m.group(1)}" if m else host, m.group(2) if m else path
    if host.endswith("eightfold.ai"):
        m = re.search(r"/job/(\d+)", path)
        return "eightfold", host.split(".")[0], m.group(1) if m else query.get("pid", path)
    if host.endswith("workable.com") and "j" in seg:
        return "workable", seg[0].lower(), seg[seg.index("j") + 1] if seg.index("j") + 1 < len(seg) else ""
    if host.endswith("jobvite.com") and "job" in seg:
        return "jobvite", seg[0].lower(), seg[-1]
    if host.endswith("rippling.com") and len(seg) >= 3:
        return "rippling", seg[0].lower(), seg[-1]
    if host.endswith("bamboohr.com"):
        return "bamboohr", host.split(".")[0], seg[-1] if seg else ""
    if host.endswith("successfactors.com") or host.endswith("successfactors.eu"):
        return "successfactors", host, query.get("jobId", path)
    return "custom", host, (path + ("?" + parts.query if parts.query else "")).strip("/")


def job_key(url: str) -> tuple[str, str, str, str]:
    """(key, ats, board, job_id) for a canonical URL."""
    ats, board, job_id = detect_ats(url)
    if ats == "ashby":  # /application suffix is the same posting
        job_id = job_id.lower()
    return f"{ats}:{board}:{job_id}".lower(), ats, board, job_id


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def secondary_key(company: str, title: str, locations: list[str]) -> str:
    company = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|group)\b", "", _slug(company))
    return f"{_slug(company)}|{_slug(title)}|{_slug(locations[0]) if locations else ''}"


# --- locations -------------------------------------------------------------------------------

CA_PROVINCES = {"ON", "BC", "QC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "PEI", "YT", "NT", "NU"}
US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
CA_WORDS = re.compile(
    r"\b(canada|ontario|quebec|québec|british columbia|alberta|manitoba|saskatchewan|nova scotia|"
    r"toronto|vancouver|montreal|montréal|ottawa|waterloo|kitchener|calgary|edmonton|mississauga|markham|"
    r"winnipeg|halifax|burnaby|kanata|victoria, bc|london, on|hamilton, on|guelph)\b",
    re.I,
)
US_WORDS = re.compile(
    r"\b(usa|u\.s\.a?\.?|united states|us|sf|nyc|new york|san francisco|bay area|seattle|boston|chicago|austin|"
    r"los angeles|san jose|mountain view|palo alto|sunnyvale|menlo park|santa clara|redmond|bellevue|denver|"
    r"atlanta|dallas|houston|san diego|pittsburgh|philadelphia|washington|cupertino|irvine|miami|"
    r"california|texas|massachusetts|illinois|virginia|florida|michigan|ohio|colorado|georgia)\b",
    re.I,
)

# Bare country / city names with no comma ("Singapore", "Hong Kong") that are unambiguously outside North America.
INTL_WORDS = re.compile(
    r"\b(singapore|hong kong|united kingdom|uk|england|london|ireland|dublin|germany|berlin|munich|france|paris|"
    r"netherlands|amsterdam|switzerland|zurich|zürich|spain|madrid|barcelona|sweden|stockholm|poland|warsaw|"
    r"india|bangalore|bengaluru|hyderabad|mumbai|japan|tokyo|china|shanghai|beijing|shenzhen|taiwan|taipei|"
    r"south korea|seoul|australia|sydney|melbourne|israel|tel aviv|brazil|mexico|uae|dubai)\b",
    re.I,
)


def location_country(location: str) -> str:
    text = location.strip()
    remote = bool(re.search(r"\bremote\b", text, re.I))
    tokens = set(re.findall(r"\b[A-Z]{2,3}\b", text))
    # zapply-style "MD-ANNAPOLIS"
    m = re.match(r"^([A-Z]{2})-[A-Z .]+$", text)
    if m:
        tokens.add(m.group(1))
    country = None
    if CA_WORDS.search(text) or tokens & (CA_PROVINCES - {"CA"}):
        country = "CA"
    elif US_WORDS.search(text) or tokens & US_STATES:
        country = "US"
    if remote:
        return country or "REMOTE"
    if country:
        return country
    if not text or re.fullmatch(r"(multiple|various|n/?a|see posting|tbd).*", text, re.I):
        return "UNKNOWN"
    # "Hong Kong +2": the other locations are not named, so the posting's countries are not known
    if re.search(r"\+\s*\d+\b", text):
        return "UNKNOWN"
    if INTL_WORDS.search(text):
        return "OTHER"
    # "City, Country" with no North-American marker → treat as international
    return "OTHER" if "," in text else "UNKNOWN"


def countries_of(locations: list[str]) -> list[str]:
    found = {location_country(loc) for loc in locations} or {"UNKNOWN"}
    if len(found) > 1:
        found.discard("UNKNOWN")
    return sorted(found)
