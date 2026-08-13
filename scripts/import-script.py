import csv
import sys
import requests

CSV_FILE = "tournaments.csv"
API_ENDPOINT = "http://localhost:8000/api/v1/tournaments/import"


def import_tournaments():
    try:
        with open(CSV_FILE, mode="r", encoding="utf-8") as file:
            reader = csv.DictReader(file)

            for row in reader:
                base_url = row["base_url"].strip()
                slug = row["slug"].strip()

                payload = {
                    "base_url": base_url,
                    "slug": slug,
                    "include_ballots": True,
                }

                print(f"Importing tournament: {slug} ({base_url})...")

                try:
                    response = requests.post(
                        API_ENDPOINT,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )

                    if response.status_code == 201:
                        print(f"-> SUCCESS: {slug} imported (HTTP 201).\n")
                    else:
                        print(
                            f"-> ERROR: Import failed for {slug}. "
                            f"HTTP Status: {response.status_code}"
                        )
                        print(f"Response: {response.text}")
                        sys.exit(1)

                except requests.RequestException as e:
                    print(f"-> NETWORK ERROR: Could not reach endpoint: {e}")
                    sys.exit(1)

    except FileNotFoundError:
        print(f"Error: The file {CSV_FILE} was not found.")
    except KeyError as e:
        print(
            f"Error: Missing column in CSV: {e}. Ensure headers are 'base_url' and 'slug'."
        )


if __name__ == "__main__":
    import_tournaments()