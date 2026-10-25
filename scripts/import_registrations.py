"""Import a year's school registrations from the Google Form's CSV export.

Dry run (default) — plans everything, prints what would happen, writes nothing:
    make import-registrations CSV=reg-2025.csv YEAR=2025

Then resolve any AMBIGUOUS rows with a JSON mapping file of
{"<school name as in the sheet>": <institution id> | "new"} and apply:
    make import-registrations CSV=reg-2025.csv YEAR=2025 MAP=map.json ARGS="--paid-on-submission --apply"
"""

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlmodel import Session

from app.db.session import engine, init_db
from app.services.registrations import (
    RegistrationImportError,
    import_registrations,
    parse_registrations,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path, help="CSV export of the form responses")
    parser.add_argument("--year", type=int, required=True, help="Membership year the registrations are for")
    parser.add_argument("--map", type=Path, help='JSON {"<sheet school name>": <institution id> | "new"}')
    parser.add_argument("--fee", type=Decimal, help="Membership fee (LKR) to record on new memberships")
    parser.add_argument(
        "--paid-on-submission",
        action="store_true",
        help="Mark memberships paid on the form's submission date when a payment proof is attached",
    )
    parser.add_argument("--apply", action="store_true", help="Write the import (default is a dry run)")
    args = parser.parse_args()

    try:
        registrations = parse_registrations(args.csv.read_text(encoding="utf-8"))
        mapping = json.loads(args.map.read_text(encoding="utf-8")) if args.map else {}
        init_db()
        with Session(engine) as session:
            report = import_registrations(
                session,
                registrations,
                args.year,
                mapping=mapping,
                fee_lkr=args.fee,
                paid_on_submission=args.paid_on_submission,
                apply=args.apply,
            )
    except (RegistrationImportError, json.JSONDecodeError, OSError) as exc:
        sys.exit(f"Error: {exc}")

    for plan in report.rows:
        if plan.resolution == "ambiguous":
            options = "; ".join(f"#{s.id} {s.name} ({s.score:.2f})" for s in plan.suggestions)
            target = f"AMBIGUOUS — similar: {options}"
        elif plan.resolution == "new":
            target = f"create “{plan.institution_name}”"
        else:
            target = f"#{plan.institution_id} {plan.institution_name} ({plan.resolution})"
        print(f"row {plan.row:>3}  {plan.sheet_name[:45]:<45}  →  {target}")

    if report.ambiguous:
        print(
            f"\n{len(report.ambiguous)} ambiguous row(s): add them to a --map file "
            '({"<sheet name>": <institution id> | "new"}) and re-run. Nothing was written.'
        )
        sys.exit(1)

    print(
        f"\n{len(report.rows)} rows → {report.schools} schools: "
        f"{report.institutions_created} institutions created, "
        f"{report.memberships_created} memberships created / {report.memberships_updated} updated, "
        f"{report.contacts_created} contacts created / {report.contacts_updated} updated, "
        f"{report.leads_closed} outreach leads closed."
    )
    print("Applied." if report.applied else "Dry run — nothing written. Re-run with --apply to import.")


if __name__ == "__main__":
    main()
