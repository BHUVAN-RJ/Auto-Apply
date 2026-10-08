"""Apollo people search and business-email enrichment.

Search is kept separate from enrichment so only the contact the person wants
to email spends a credit. Personal email and phone reveals are never requested.
"""

from __future__ import annotations

import os
import re
from typing import Optional

import httpx

from . import store
from .models import Contact

API_KEY = "AUTOPILOT_APOLLO_API_KEY"
BASE_URL = "https://api.apollo.io/api/v1"
SEARCH_URL = f"{BASE_URL}/mixed_people/api_search"
ENRICH_URL = f"{BASE_URL}/people/match"
TIMEOUT = 30

TITLES = {
    "recruiter": [
        "technical recruiter", "recruiter", "talent acquisition partner",
        "talent acquisition specialist",
    ],
    "hiring_manager": [
        "engineering manager", "software engineering manager",
        "machine learning manager", "data science manager",
        "director of engineering", "director of data science",
    ],
}


class ApolloError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get(API_KEY, "").strip())


def _headers() -> dict[str, str]:
    key = os.environ.get(API_KEY, "").strip()
    if not key:
        raise ApolloError("Apollo is not connected. Add its API key from the Outreach setup.")
    return {"X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json"}


def _words(value: str) -> set[str]:
    ignored = {"inc", "llc", "ltd", "corp", "corporation", "company", "co", "the"}
    return {word for word in re.findall(r"[a-z0-9]+", value.lower()) if word not in ignored}


def _same_company(wanted: str, found: str) -> bool:
    left, right = _words(wanted), _words(found)
    return bool(left and right and (left <= right or right <= left or len(left & right) >= 2))


def _person_company(person: dict) -> str:
    organization = person.get("organization")
    if isinstance(organization, dict):
        return str(organization.get("name") or "")
    return str(person.get("organization_name") or "")


def search(company: str, role: str, limit: int = 4,
           client: Optional[httpx.Client] = None) -> list[Contact]:
    if role not in TITLES:
        raise ApolloError(f"unknown contact role {role!r}")
    cache_key = f"{company}:{role}"
    cached = store.cache_get("apollo-search", cache_key)
    if cached and isinstance(cached.get("contacts"), list):
        return [Contact.model_validate(row) for row in cached["contacts"]]

    payload = {
        "person_titles": TITLES[role],
        "include_similar_titles": True,
        "q_keywords": company,
        "page": 1,
        "per_page": max(limit * 3, 10),
    }
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.post(SEARCH_URL, headers=_headers(), json=payload)
    except httpx.HTTPError as exc:
        raise ApolloError(f"Apollo search failed: {exc}") from exc
    finally:
        if own:
            client.close()
    if response.status_code in (401, 403):
        raise ApolloError("Apollo refused the key or this plan does not allow people search")
    if response.status_code >= 400:
        raise ApolloError(f"Apollo search answered {response.status_code}: {response.text[:160]}")

    rows = response.json().get("people") or []
    contacts = []
    for person in rows:
        found_company = _person_company(person)
        if found_company and not _same_company(company, found_company):
            continue
        name = _person_name(person)
        linkedin = str(person.get("linkedin_url") or "").strip()
        apollo_id = str(person.get("id") or "").strip()
        if not name:
            continue
        contacts.append(Contact.create(
            role=role,
            name=name,
            title=str(person.get("title") or ""),
            company=found_company or company,
            linkedin_url=linkedin,
            apollo_id=apollo_id,
            source="apollo",
            verification="manual",
            email_status="available" if person.get("has_email") else "",
        ))
        if len(contacts) >= limit:
            break
    store.cache_put(
        "apollo-search", cache_key,
        {"contacts": [row.model_dump(mode="json") for row in contacts]},
    )
    return contacts


def _person_name(person: dict) -> str:
    name = str(person.get("name") or "").strip()
    if name:
        return name
    first = str(person.get("first_name") or "").strip()
    last = str(person.get("last_name") or person.get("last_name_obfuscated") or "").strip()
    return " ".join(part for part in (first, last) if part)


def enrich(contact: Contact, client: Optional[httpx.Client] = None) -> Contact:
    cache_key = contact.linkedin_url or contact.apollo_id
    if not cache_key and not (contact.name and contact.company):
        raise ApolloError("Apollo enrichment needs a name and company, a LinkedIn URL, or an Apollo id")
    if cache_key:
        cached = store.cache_get("apollo-enrich", cache_key)
        if cached:
            return Contact.model_validate(cached)

    params = {
        "reveal_personal_emails": "false",
        "reveal_phone_number": "false",
    }
    if contact.linkedin_url:
        params["linkedin_url"] = contact.linkedin_url
    elif contact.apollo_id:
        params["id"] = contact.apollo_id
    else:
        params["name"] = contact.name
        params["organization_name"] = contact.company
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        response = client.post(ENRICH_URL, headers=_headers(), params=params)
    except httpx.HTTPError as exc:
        raise ApolloError(f"Apollo enrichment failed: {exc}") from exc
    finally:
        if own:
            client.close()
    if response.status_code in (401, 403):
        raise ApolloError("Apollo refused the key or this plan does not allow enrichment")
    if response.status_code >= 400:
        raise ApolloError(f"Apollo enrichment answered {response.status_code}: {response.text[:160]}")

    person = response.json().get("person") or {}
    email = str(person.get("email") or "").strip()
    status = str(person.get("email_status") or "").strip().lower()
    if not email:
        raise ApolloError(f"Apollo has no business email for {contact.name}")
    enriched = contact.model_copy(update={
        "name": str(person.get("name") or contact.name).strip() or contact.name,
        "title": str(person.get("title") or contact.title).strip() or contact.title,
        "linkedin_url": str(person.get("linkedin_url") or contact.linkedin_url).strip(),
        "apollo_id": str(person.get("id") or contact.apollo_id).strip(),
        "email": email,
        "source": "apollo",
        "verification": "verified" if status == "verified" else "manual",
        "email_status": status or "returned",
    })
    store.cache_put("apollo-enrich", cache_key or email, enriched.model_dump(mode="json"))
    return enriched


def discover(company: str, per_role: int = 3,
             client: Optional[httpx.Client] = None) -> list[Contact]:
    contacts = []
    for role in ("recruiter", "hiring_manager"):
        contacts.extend(search(company, role, per_role, client=client))
    return contacts
