import csv
import sys
import httpx

CSV_FILE = "tournaments.csv"
API_ENDPOINT = "http://localhost:8000/api/v1/tournaments/import"
# A full import makes 1 + rounds + debates upstream requests (see
# app.services.tabbycat.import_tournament), which can take minutes for a large
# tournament — httpx's 5s default timeout was firing on real, still-succeeding imports.
REQUEST_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


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
                    response = httpx.post(
                        API_ENDPOINT,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=REQUEST_TIMEOUT,
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

                except httpx.RequestError as e:
                    # httpx timeout/connect errors often stringify to "" — name the
                    # exception type so "timed out" doesn't look identical to a DNS/refused error.
                    print(
                        f"-> NETWORK ERROR ({type(e).__name__}): Could not reach endpoint: {e or '(no detail)'}"
                    )
                    sys.exit(1)

    except FileNotFoundError:
        print(f"Error: The file {CSV_FILE} was not found.")
    except KeyError as e:
        print(
            f"Error: Missing column in CSV: {e}. Ensure headers are 'base_url' and 'slug'."
        )


if __name__ == "__main__":
    import_tournaments()