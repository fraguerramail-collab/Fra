"""Registro dei "profili" (reparti): ogni profilo ha il proprio database SQLite
indipendente, cosi' le modifiche fatte su un reparto non toccano gli altri.
"""

import json
import os
import re
import threading

_lock = threading.Lock()


def _registry_path(instance_path):
    return os.path.join(instance_path, "profiles.json")


def _slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "reparto"


def load_profiles(instance_path):
    path = _registry_path(instance_path)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_profiles(instance_path, profiles):
    os.makedirs(instance_path, exist_ok=True)
    path = _registry_path(instance_path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)


def get_profile(instance_path, slug):
    for p in load_profiles(instance_path):
        if p["slug"] == slug:
            return p
    return None


def add_profile(instance_path, name):
    with _lock:
        profiles = load_profiles(instance_path)
        base_slug = _slugify(name)
        slug = base_slug
        existing_slugs = {p["slug"] for p in profiles}
        n = 2
        while slug in existing_slugs:
            slug = f"{base_slug}-{n}"
            n += 1
        profile = {"slug": slug, "name": name.strip()}
        profiles.append(profile)
        save_profiles(instance_path, profiles)
        return profile


def ensure_default_profile(instance_path, default_name="Reparto 1"):
    with _lock:
        profiles = load_profiles(instance_path)
        if profiles:
            return profiles
        profile = {"slug": _slugify(default_name), "name": default_name}
        save_profiles(instance_path, [profile])
        return [profile]
