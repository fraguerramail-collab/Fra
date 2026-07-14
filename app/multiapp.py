"""Serve piu' profili (reparti), ciascuno con la propria app Flask isolata
(proprio database), sotto un unico processo/porta. Ogni profilo vive sotto
/p/<slug>/; la radice "/" mostra la scelta del reparto.
"""

import os
import threading
from urllib.parse import urlencode

from flask import Flask, redirect, render_template_string, request

from . import create_app
from .registry import (
    add_profile,
    delete_profile,
    ensure_default_profile,
    get_profile,
    load_profiles,
    rename_profile,
)

PICKER_TEMPLATE = """
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>Scegli reparto - Gestione Turni</title>
<style>
  body { font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #f5f6fa;
         margin: 0; padding: 2rem; color: #1f2430; }
  .container { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.4rem; }
  .card { background: #fff; border: 1px solid #e3e5eb; border-radius: 10px; padding: 1.25rem;
          margin-bottom: 1rem; }
  a.profile-link { display: block; padding: 0.6rem 0; text-decoration: none; color: #1f2430;
          font-weight: 600; flex: 1; min-width: 180px; }
  a.profile-link:hover { color: #4f7cff; }
  .profile-row { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
          flex-wrap: wrap; margin-bottom: 0.6rem; }
  .profile-row form { display: flex; gap: 0.4rem; }
  input[type=text] { padding: 0.45rem 0.6rem; border: 1px solid #e3e5eb; border-radius: 6px; }
  button { background: #4f7cff; color: #fff; border: none; padding: 0.5rem 1rem; border-radius: 6px;
           cursor: pointer; }
  button.danger { background: #e5484d; }
  .flash-ok { background: #e3f7ec; color: #1a9c5c; font-weight: 700; padding: 0.7rem 1rem;
          border-radius: 8px; margin-bottom: 1rem; border-left: 4px solid #1a9c5c; }
</style>
</head>
<body>
<div class="container">
  <h1>Scegli il reparto</h1>
  <p>Ogni reparto ha turni, dipendenti e regole propri, completamente separati dagli altri.</p>
  {% if msg %}<div class="flash-ok">✓ {{ msg }}</div>{% endif %}
  {% for p in profiles %}
  <div class="card profile-row">
    <a class="profile-link" href="/p/{{ p.slug }}/">{{ p.name }}</a>
    <form method="post" action="/profili/{{ p.slug }}/rinomina">
      <input type="text" name="name" value="{{ p.name }}" style="width:11rem">
      <button type="submit">Rinomina</button>
    </form>
    <form method="post" action="/profili/{{ p.slug }}/elimina"
          onsubmit="return confirm('Eliminare definitivamente il reparto \'{{ p.name }}\' e TUTTI i suoi dati (dipendenti, turni, pianificazioni)? Questa azione non si puo\' annullare.');">
      <button type="submit" class="danger">Elimina</button>
    </form>
  </div>
  {% endfor %}
  <div class="card">
    <h2 style="font-size:1rem;margin-top:0">Nuovo reparto</h2>
    <form method="post" action="/profili">
      <input type="text" name="name" placeholder="Nome del reparto, es. Reparto XL" required>
      <button type="submit">Crea</button>
    </form>
  </div>
</div>
</body>
</html>
"""


def create_root_app(instance_path, profile_apps_cache):
    root = Flask(__name__)
    ensure_default_profile(instance_path)

    @root.route("/")
    def picker():
        profiles = load_profiles(instance_path)
        return render_template_string(PICKER_TEMPLATE, profiles=profiles, msg=request.args.get("msg"))

    @root.route("/profili", methods=["POST"])
    def create_profile():
        name = request.form.get("name", "").strip()
        if name:
            profile = add_profile(instance_path, name)
            return redirect(f"/p/{profile['slug']}/")
        return redirect("/")

    @root.route("/profili/<slug>/rinomina", methods=["POST"])
    def rename_profile_route(slug):
        name = request.form.get("name", "").strip()
        if name:
            rename_profile(instance_path, slug, name)
            profile_apps_cache.pop(slug, None)
            return redirect("/?" + urlencode({"msg": f"Reparto rinominato in '{name}'."}))
        return redirect("/")

    @root.route("/profili/<slug>/elimina", methods=["POST"])
    def delete_profile_route(slug):
        profile = get_profile(instance_path, slug)
        delete_profile(instance_path, slug)
        profile_apps_cache.pop(slug, None)
        name = profile["name"] if profile else "reparto"
        return redirect("/?" + urlencode({"msg": f"Reparto '{name}' eliminato."}))

    return root


class MultiProfileDispatcher:
    """WSGI dispatcher: crea/riusa un'app Flask isolata per ogni profilo,
    creandola al volo alla prima richiesta (nessun riavvio necessario per
    un reparto appena creato)."""

    def __init__(self, instance_path):
        self.instance_path = instance_path
        self._profile_apps = {}
        self.root_app = create_root_app(instance_path, self._profile_apps)
        self._lock = threading.Lock()

    def _get_profile_app(self, slug):
        if slug in self._profile_apps:
            return self._profile_apps[slug]
        with self._lock:
            if slug not in self._profile_apps:
                profile = get_profile(self.instance_path, slug)
                if profile is None:
                    return None
                self._profile_apps[slug] = create_app(
                    profile_slug=slug, profile_name=profile["name"], instance_path=self.instance_path,
                )
        return self._profile_apps[slug]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path.startswith("/p/"):
            rest = path[len("/p/"):]
            slug, _, sub_path = rest.partition("/")
            app = self._get_profile_app(slug)
            if app is not None:
                environ = environ.copy()
                environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + f"/p/{slug}"
                environ["PATH_INFO"] = "/" + sub_path
                return app.wsgi_app(environ, start_response)
        return self.root_app.wsgi_app(environ, start_response)
