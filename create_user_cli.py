"""
create_user_cli.py — script interactif pour créer un compte à la main.

Pas de route d'inscription publique : les comptes (notamment tech_lead) se créent
en local avec ce script. Usage : python create_user_cli.py
"""

import getpass

import db

db.init_db()

username = input("Nom d'utilisateur : ").strip()
password = getpass.getpass("Mot de passe : ")
role = input("Rôle (user / tech_lead) [user] : ").strip() or "user"

if role not in ("user", "tech_lead"):
    print(f"Rôle invalide : {role!r}. Attendu 'user' ou 'tech_lead'.")
else:
    try:
        db.create_user(username, password, role)
        print(f"Utilisateur '{username}' créé avec le rôle '{role}'.")
    except Exception as e:
        print(f"Échec de la création (nom déjà pris ?) : {e}")
