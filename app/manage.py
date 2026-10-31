"""Gebruikers beheren. Draai op de server vanuit de projectmap:

    docker compose exec hangul-buddy python manage.py add-user Mama
    docker compose exec hangul-buddy python manage.py list
    docker compose exec hangul-buddy python manage.py reset-token Mama
    docker compose exec hangul-buddy python manage.py remove-user Mama --yes
"""

import sys

import main

USAGE = __doc__


def run(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"add-user", "list", "reset-token", "remove-user"}:
        print(USAGE)
        return 2
    main.init_db()
    cmd = argv[1]
    try:
        if cmd == "list":
            for u in main.list_users():
                print(f"{u['id']:>3}  {u['name']:<20} {u['words']:>3} woorden  "
                      f"{'eigen Azure-sleutel' if u['own_key'] else 'gedeelde sleutel'}")
            return 0
        if len(argv) < 3:
            print(USAGE)
            return 2
        name = argv[2]
        if cmd == "add-user":
            token = main.create_user(name)
            print(f"Gebruiker '{name}' aangemaakt.\nToegangscode (bewaar die nu, hij wordt niet opnieuw getoond):\n\n  {token}\n")
        elif cmd == "reset-token":
            token = main.reset_token(name)
            print(f"Nieuwe toegangscode voor '{name}':\n\n  {token}\n")
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
