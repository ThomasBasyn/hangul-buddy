"""Gebruikers beheren. Draai op de server vanuit de projectmap:

    docker compose exec hangul-buddy python manage.py add-user Mama
    docker compose exec hangul-buddy python manage.py list
    docker compose exec hangul-buddy python manage.py reset-password Mama   # wachtwoord vergeten
    docker compose exec hangul-buddy python manage.py remove-user Mama --yes
    docker compose exec hangul-buddy python manage.py add-time Thomas 322   # eenmalig spreektijd (seconden) bijboeken
"""

import sys

import main

USAGE = __doc__


def run(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"add-user", "add-time", "list", "reset-password", "reset-token", "remove-user"}:
        print(USAGE)
        return 2
    main.init_db()
    cmd = argv[1]
    try:
        if cmd == "list":
            for u in main.list_users():
                print(f"{u['id']:>3}  {u['name']:<20} {u['words']:>3} woorden  "
                      f"{'actief' if u['active'] else 'wacht op activatie':<18} "
                      f"{'eigen Azure-sleutel' if u['own_key'] else 'gedeelde sleutel'}")
            return 0
        if len(argv) < 3:
            print(USAGE)
            return 2
        name = argv[2]
        if cmd == "add-user":
            token = main.create_user(name)
            print(f"Gebruiker '{name}' aangemaakt.\nUitnodigingscode (bewaar die nu, hij wordt niet opnieuw getoond):\n\n  {token}\n"
                  "Geef die code door: in de app kiest de gebruiker daarmee zelf een gebruikersnaam en wachtwoord.")
        elif cmd in ("reset-password", "reset-token"):
            token = main.reset_token(name)
            if token:
                print(f"Wachtwoord van '{name}' gewist. Nieuwe uitnodigingscode:\n\n  {token}\n")
            else:
                print(f"Wachtwoord van '{name}' gewist. Activeer opnieuw in de app met je APP_TOKEN.")
        elif cmd == "add-time":
            if len(argv) < 4 or not argv[3].replace(".", "", 1).isdigit():
                print("Gebruik: add-time NAAM SECONDEN [--lang ko] [--month JJJJ-MM]")
                return 2
            opts = dict(zip(argv[4::2], argv[5::2]))
            total = main.add_speech_time(name, float(argv[3]), opts.get("--lang", "ko"), opts.get("--month"))
            print(f"Bijgeboekt. Totaal voor '{name}' in die maand: {total:.0f} s.")
        elif cmd == "remove-user":
            if "--yes" not in argv:
                print(f"Dit wist alle voortgang van '{name}'. Voeg --yes toe om door te gaan.")
                return 1
            main.remove_user(name)
            print(f"Gebruiker '{name}' en zijn voortgang zijn verwijderd.")
    except ValueError as exc:
        print(f"Mislukt: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv))
